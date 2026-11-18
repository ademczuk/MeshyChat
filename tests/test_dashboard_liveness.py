"""Measured liveness on GET /v1/dashboard/api/peers (SD 356, 9 Nov 2026): database-free checks of the SQL the
route runs and of the daemon census `active` depends on. The route itself, against a real database, is
tests/test_dashboard.py::test_peers_roster_shape_and_presence (the gate runs it).

Run:  python -m pytest tests/test_dashboard_liveness.py -q -p no:cacheprovider
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.dashboard import queries  # noqa: E402
from apps.wsgw import hub_presence  # noqa: E402


def test_reachable_uses_the_stores_own_online_rule_with_a_named_window():
    sql = queries.peers_sql("WITH vis_peers AS (SELECT NULL::uuid AS created_by_peer_id)")
    rule = hub_presence.online_condition("p.id").replace("%s", "%(online_secs)s")
    assert rule in sql, "reachable must be hub_presence's rule text, not a copy"
    assert "%s" not in sql, "a positional placeholder in a named-parameter statement"
    assert "redis" not in sql.lower()


def test_active_is_a_recent_send_or_an_unexpired_ttl_never_last_activity():
    sql = queries.peers_sql("WITH vis_peers AS (SELECT NULL::uuid AS created_by_peer_id)")
    act = sql[sql.index("SELECT (ls.last_sent_at IS NOT NULL"):]
    act = act[:act.index(") act ON TRUE")]
    assert "state_expires_at > NOW()" in act and "last_activity" not in act
    assert "mp.state = 'active'" in act, "an idle or asleep beat is not active (pxx d7c18d P2-2)"
    assert "mp.last_beat >= NOW() - make_interval(secs => %d)" % queries.BEAT_WINDOW_SECS in act, \
        "an old beat with a long TTL is not active (pxx d7c18d P2-2), aged from the PUBLISHED beat (b150a0)"
    assert "mp.last_beat IS NOT NULL" in act and "updated_at" not in act, "updated_at is invoke time, not beat time"
    assert queries.BEAT_WINDOW_SECS > 3600, "a seat re-publishing hourly must not drop out between beats"
    assert "liveness', CASE WHEN act.active THEN 'active' WHEN hc.reachable THEN 'reachable_idle' ELSE 'offline'" in sql


def test_the_send_window_is_a_key_range_on_the_head_index():
    """The fleet's own last-send read (apps/mesh/work.py): msg/<epoch ms> keys bound the range before the writer."""
    sql = queries.peers_sql("WITH vis_peers AS (SELECT NULL::uuid AS created_by_peer_id)")
    assert "sm.key >= %(sent_from)s AND sm.key < 'msg/9'" in sql and "sm.is_head" in sql
    params = queries.peers_params({"owner": "o"}, 1_800_000_000_000)
    assert params["sent_from"] == "msg/%d" % (1_800_000_000_000 - queries.ACTIVE_WINDOW_SECS * 1000)
    assert params["owner"] == "o" and params["online_secs"] == hub_presence.ONLINE_WINDOW_SECS
    assert params["mesh_ns"] == "shared/mesh-chat" and params["daemon_threads"] == list(queries.DAEMON_THREADS)


def test_a_published_state_without_a_ttl_goes_stale_and_a_lapsed_ttl_is_stale():
    sql = queries.peers_sql("WITH vis_peers AS (SELECT NULL::uuid AS created_by_peer_id)")
    assert "WHEN mp.state_expires_at IS NULL THEN mp.last_beat < NOW() - make_interval(secs => %d)" \
        % queries.PRESENCE_NO_TTL_STALE_SECS in sql
    assert "WHEN mp.peer_id IS NULL OR mp.last_beat IS NULL THEN NULL" in sql, "a never-published row is not stale"
    assert "ELSE mp.state_expires_at <= NOW()" in sql


#: Every tools/ module that runs `meshy_node send` on its own (no agent at the keyboard), and why its send does or
#: does not count as a seat's session at work. A new daemon sender fails this test until it is placed here: either
#: its thread goes in queries.DAEMON_THREADS, or the reason it is a session's own send is written down.
DAEMON_SENDERS = {
    "tools/postbox/codex_wake_alert.py": "daemon: its thread 'wake-alerts' is in DAEMON_THREADS",
    "tools/postbox/postbox.py": "session: `send drain` delivers a send the session queued itself",
}


def test_every_daemon_sender_is_accounted_for():
    found = set()
    for p in sorted((ROOT / "tools").rglob("*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r'"tools\.meshy_node",\s*"send"', text):
            found.add(p.relative_to(ROOT).as_posix())
    assert found == set(DAEMON_SENDERS), {"new": sorted(found - set(DAEMON_SENDERS)),
                                          "gone": sorted(set(DAEMON_SENDERS) - found)}
    assert "wake-alerts" in queries.DAEMON_THREADS
    src = (ROOT / "tools/postbox/codex_wake_alert.py").read_text(encoding="utf-8")
    assert 'ap.add_argument("--thread", default="wake-alerts")' in src
