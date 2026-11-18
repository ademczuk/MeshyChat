"""On-demand fleet facts, held for 60 seconds; no turn-state inference or background polling.

Counts and recipient labels share the recipients/presence audience, confirmed in
Oxxx's signed f0bdf1 and relayed by Kxxx's 67804f. No message text is returned.
"""
from __future__ import annotations

import collections
import copy
import datetime as dt
import threading
import time

from apps.context.validation import namespace as context_namespace
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.peers import turn

from . import graph

HOLD_SECONDS = 60.0
ASK_DAYS = 14
SENT_DAYS = 7
HELD_MAX = 4096
LAST_SENT_SQL = ("SELECT max(created_at) FROM shared_memory WHERE namespace = %s AND is_head "
                 "AND key >= %s AND key < 'msg/9' AND created_by_peer_id = %s AND created_at >= %s")
#: MEASURED activity for every recipients answer (oxxxxxx item 5, Tue 10 Nov 2026): the dashboard's #575 rule
#: (apps/dashboard/queries.py ACTIVE_WINDOW_SECS, BEAT_WINDOW_SECS, DAEMON_THREADS; a test holds them equal), so the
#: fleet and a roster built on it say what the dashboard says. A seat is ACTIVE when it SENT a mesh message in the last
#: ACTIVE_WINDOW_SECS (not on a daemon-only thread), or it published state `active`, with a TTL that has not run
#: out, within BEAT_WINDOW_SECS.
#: ONE statement per recipients call, for every peer it reaches (pxx P2-1: the route runs before every send).
#: The window is the DATABASE's clock on both bounds (P3): the msg/<epoch ms> key range is computed from now() in the
#: statement, a constant for its scan, so the (namespace, key) index range stays the plan.
ACTIVE_WINDOW_SECS = 30 * 60
BEAT_WINDOW_SECS = 65 * 60
DAEMON_THREADS = ("wake-alerts",)
ACTIVE_SENT_SQL = ("SELECT created_by_peer_id::text, max(created_at) FROM shared_memory "
                   "WHERE namespace = %s AND is_head "
                   "AND key >= 'msg/' || floor(extract(epoch FROM now() - make_interval(secs => %s)) * 1000)::bigint "
                   "AND key < 'msg/9' AND created_by_peer_id = ANY (%s::uuid[]) "
                   "AND created_at >= now() - make_interval(secs => %s) "
                   "AND NOT (COALESCE(value->>'thread_id', '') = ANY (%s)) "
                   "GROUP BY created_by_peer_id")


def active_sent(conn, peer_ids) -> dict | None:
    """{peer id: newest send in the window} for the peers that sent in it, or None when the read failed (it runs in its
    own savepoint, so a timeout or error leaves the recipients answer and its transaction intact: `active` then reads
    null, never a fabricated false)."""
    ids = list(dict.fromkeys(str(p) for p in peer_ids))
    if not ids:
        return {}
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(ACTIVE_SENT_SQL, (MESH_CHAT_NAMESPACE, ACTIVE_WINDOW_SECS, ids, ACTIVE_WINDOW_SECS,
                                              list(DAEMON_THREADS)))
                return {pid: at for pid, at in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 - optional fact unavailable, never a fabricated answer
        import logging
        logging.getLogger(__name__).warning("recipients active read unavailable: %s", type(exc).__name__)
        return None


def _context_space() -> str | None:
    """The space the context index serves, from the same setting search and the walker read
    (apps/meshsem/store.configured_space). None when it names no known space: no indexed time then, not an error."""
    from apps.meshsem import store
    try:
        return store.configured_space().name
    except KeyError:
        return None


def measured_active(sent: dict | None, peer_id, presence: dict) -> tuple[bool | None, object]:
    """(active, active_sent_at) for one peer from active_sent's answer. None when that read failed and the published
    state does not decide it alone. `presence` is the peer's states_of entry (effective_state is already `unknown`
    past the TTL the seat gave; beat_age_secs is from the published beat)."""
    beat_age = presence.get("beat_age_secs")
    beat_active = (presence.get("effective_state") == "active" and presence.get("state_expires_at") is not None
                   and isinstance(beat_age, (int, float)) and beat_age <= BEAT_WINDOW_SECS)
    if sent is None:
        return (True if beat_active else None), None
    at = sent.get(str(peer_id))
    return (at is not None or beat_active), at


# One backward index range per label, not an aggregate over all indexed history. Keep the head join:
# current flags describe indexed generations; they alone do not establish a live manifest head.
# CURRENT rows of the configured space only (sql_meshychat/059): then 052's partial index
# (space, namespace, seat, time_last) WHERE current serves this read, and 047's full (namespace, seat, time_last)
# index, which the shard probes could pick and walk superseded history through, can go. On production on
# Wed 11 Nov 2026 the newest time per seat was the same with and without `current` (13 seats, 0 differ); where
# they could differ (a newer generation ending earlier) the current row's time is the truthful one.
INDEXED_TURN_SQL = ("SELECT c.time_last FROM mesh_ctx_chunks c "
                    "JOIN shared_memory sm ON sm.id = c.src_id AND sm.is_head "
                    "WHERE c.space = %s AND c.namespace = %s AND c.seat = %s AND c.current "
                    "ORDER BY c.time_last DESC LIMIT 1")
_held = collections.OrderedDict()
_lock = threading.Lock()
_clock = time.monotonic


def forget() -> None:
    """Test reset; no timer or additional reads are installed."""
    with _lock:
        _held.clear()


def snapshot(conn, peer_id: str, labels: list[str]) -> dict:
    """Windowed timestamps/counts for one peer; never cache an unsuccessful query."""
    info = getattr(conn, "info", None)
    ns = context_namespace()
    key = (getattr(info, "host", None), getattr(info, "port", None), getattr(info, "dbname", None),
           str(peer_id), tuple(sorted(set(labels))), ns)
    with _lock:
        held = _held.get(key)
        if held is not None and _clock() - held[0] < HOLD_SECONDS:
            _held.move_to_end(key)
            cached = copy.deepcopy(held[1])
        else:
            cached = None
    if cached is not None:
        return {**cached, **turn.read(conn, peer_id)}
    as_of = dt.datetime.now(dt.timezone.utc)
    since = as_of - dt.timedelta(days=ASK_DAYS)
    sent_since = as_of - dt.timedelta(days=SENT_DAYS)
    # Sender identity is the authenticated writer, not a peer-supplied author label.
    # Bound the (namespace,key) range before filtering by writer, which has no index.
    with conn.cursor() as cur:
        cur.execute(LAST_SENT_SQL, (MESH_CHAT_NAMESPACE, f"msg/{int(sent_since.timestamp() * 1000)}",
                                    peer_id, sent_since))
        last_sent = cur.fetchone()[0]
        # This is the newest *indexed* item time, which can lag the actual turn. It is not a busy/idle signal.
        indexed = None
        space = _context_space()
        for label in sorted(set(labels)) if space is not None else ():
            cur.execute(INDEXED_TURN_SQL, (space, ns, label))
            row = cur.fetchone()
            if row is not None and (indexed is None or row[0] > indexed):
                indexed = row[0]
    owed = graph.owed_counts(conn, labels, since=since)
    # One read supplies both counts and the recipient sample. Do not repeat the heavy awaiting query.
    awaiting = graph.awaiting(conn, labels, states=("open", "active"), since=since, limit=None)
    result = {"last_sent_at": None if last_sent is None else last_sent.isoformat(),
              "indexed_turn_at": None if indexed is None else indexed.isoformat(),
              "owed": {state: owed[state] for state in ("open", "active")},
              "awaiting": {state: sum(row.state == state for row in awaiting) for state in ("open", "active")},
              "work_as_of": as_of.isoformat()}
    result["awaiting"]["recipients"] = list(dict.fromkeys(row.owing_label for row in awaiting))[:2]
    with _lock:
        _held[key] = (_clock(), copy.deepcopy(result))
        _held.move_to_end(key)
        while len(_held) > HELD_MAX:
            _held.popitem(last=False)
    return {**result, **turn.read(conn, peer_id)}
