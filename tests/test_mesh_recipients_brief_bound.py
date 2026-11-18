"""The recipients and brief routes' counts run under the unread bound (16 Nov 2026).

On Mon 16 Nov 2026, 07:31 to (time withheld), production answered 500 on /v1/mesh/recipients 16 times and on
/v1/mesh/brief 5 times: one peer's graph.unread (recipients) and graph.awaiting (brief) passed the server's statement
timeout, the QueryCanceled escaped, and the whole answer was lost, for every peer of the call. The unread route has
had its own bound since 8 Nov (tests/test_mesh_unread_bound.py). views._peer_counts and views._brief_awaiting now run
these reads through the same helper: past the bound the count is UNKNOWN (null and a flag), the savepoint is rolled
back, and the rest of the answer goes on. These tests need no database: the connection is a stand-in that records
what it was asked.

Run:  pytest tests/test_mesh_recipients_brief_bound.py -q     (needs no database)
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import sys
import time
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.mesh import views  # noqa: E402
from tools.meshy_node import cli, fleet, recipientwarn  # noqa: E402

SINCE = _dt.datetime(2026, 11, 9, tzinfo=_dt.timezone.utc)
BOUND = "%dms" % views.UNREAD_TIMEOUT_MS


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append((sql, params))

    def fetchone(self):
        return (self.conn.current_ms,)


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.log.append(("SAVEPOINT", None))
        return self

    def __exit__(self, kind, exc, tb):
        self.conn.log.append(("ROLLBACK TO SAVEPOINT" if kind else "RELEASE SAVEPOINT", None))
        return False


class _Conn:
    def __init__(self, current_ms=30000):
        self.log, self.current_ms = [], current_ms

    def cursor(self, **kw):
        return _Cursor(self)

    def transaction(self):
        return _Transaction(self)


class _Unread:
    def __init__(self, total):
        self.total, self.oldest_at, self.items, self.truncated = total, None, (), False


def _sets(conn):
    return [params[0] for sql, params in conn.log if sql and "set_config('statement_timeout'" in sql]


def _marks(conn):
    return [sql for sql, _ in conn.log if "SAVEPOINT" in sql]


def _cancel(*a, **kw):
    raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")


def _far():
    return time.monotonic() + 3600.0


def test_one_peers_count_past_its_bound_is_unknown_and_the_next_peer_is_still_counted(monkeypatch):
    """The 16 Nov failure. MUTANTS: graph.unread called outside the bound (the cancel escapes: a 500 for every peer);
    a timed-out count returned as a zero total."""
    def unread(c, labels, **kw):
        if labels == ["slow"]:
            _cancel()
        return _Unread(3)
    monkeypatch.setattr(views.graph, "unread", unread)
    monkeypatch.setattr(views.graph, "unanswered_from", lambda c, s, r, **kw: 2)
    conn, until = _Conn(30000), _far()
    assert views._peer_counts(conn, ["slow"], "p-slow", ["me"], since=SINCE, count_until=until) is None
    assert conn.log[-1][0] == "ROLLBACK TO SAVEPOINT", "the rollback also undoes the SET LOCAL"
    result, unanswered = views._peer_counts(conn, ["fast"], "p-fast", ["me"], since=SINCE, count_until=until)
    assert (result.total, unanswered) == (3, 2)


def test_both_counts_run_under_the_bound_and_the_requests_own_timeout_is_put_back(monkeypatch):
    """MUTANTS: unanswered_from left outside the bound; the old timeout not put back after a released savepoint (it
    would bind the rest of the request, and a pooled connection after it)."""
    seen = []
    monkeypatch.setattr(views.graph, "unread", lambda c, labels, **kw: seen.append(("unread", _sets(c))) or _Unread(0))
    monkeypatch.setattr(views.graph, "unanswered_from",
                        lambda c, s, r, **kw: seen.append(("unanswered", _sets(c), s, r)) or 0)
    conn = _Conn(30000)
    result, unanswered = views._peer_counts(conn, ["pxx"], "p", ["me"], since=SINCE, count_until=_far())
    assert (result.total, unanswered) == (0, 0), "a real zero stays a zero"
    assert seen == [("unread", [BOUND]), ("unanswered", [BOUND, "30000ms", BOUND], ["me"], ["pxx"])]
    assert _sets(conn) == [BOUND, "30000ms", BOUND, "30000ms"], "set and put back, once for each count"
    assert _marks(conn) == ["SAVEPOINT", "RELEASE SAVEPOINT", "SAVEPOINT", "RELEASE SAVEPOINT"]


def test_unanswered_past_its_bound_is_null_and_the_unread_count_is_kept(monkeypatch):
    monkeypatch.setattr(views.graph, "unread", lambda c, labels, **kw: _Unread(4))
    monkeypatch.setattr(views.graph, "unanswered_from", _cancel)
    conn = _Conn(30000)
    result, unanswered = views._peer_counts(conn, ["pxx"], "p", ["me"], since=SINCE, count_until=_far())
    assert result.total == 4 and unanswered is None
    assert _marks(conn)[-1] == "ROLLBACK TO SAVEPOINT"


def test_a_spent_budget_starts_no_count_and_reads_unknown(monkeypatch):
    """N peers times the per-count bound must not become the route's time. MUTANT: the budget not looked at."""
    asked = []
    monkeypatch.setattr(views.graph, "unread", lambda c, labels, **kw: asked.append(labels) or _Unread(1))
    monkeypatch.setattr(views.graph, "unanswered_from", lambda c, s, r, **kw: 0)
    conn = _Conn(30000)
    assert views._peer_counts(conn, ["pxx"], "p", ["me"], since=SINCE, count_until=time.monotonic() - 0.001) is None
    assert asked == [] and conn.log == [], "nothing was sent to the database"
    assert views.RECIPIENTS_COUNT_BUDGET_S + views.UNREAD_TIMEOUT_MS / 1000 < 30, "ends inside the server's 30 s"


def test_a_budget_spent_by_the_unread_count_starts_no_second_count(monkeypatch):
    """The unread count may start just inside the budget and come back after it. The second count (unanswered_from)
    must then not start: it could run its whole bound past the route's time. The unread count is kept; the second
    is null (unknown), never 0. MUTANT: the budget looked at only once, before the first count."""
    asked = []
    until = time.monotonic() + 0.05

    def slow_unread(c, labels, **kw):
        time.sleep(0.08)                                  # ends after `until`
        return _Unread(4)

    monkeypatch.setattr(views.graph, "unread", slow_unread)
    monkeypatch.setattr(views.graph, "unanswered_from", lambda c, s, r, **kw: asked.append(r) or 0)
    result, unanswered = views._peer_counts(_Conn(30000), ["pxx"], "p", ["me"], since=SINCE, count_until=until)
    assert result.total == 4 and unanswered is None
    assert asked == [], "the second count was not sent"


def test_another_database_error_in_a_count_is_not_taken_for_a_timeout(monkeypatch):
    """Only a cancel is an unknown count; anything else still fails loudly. MUTANT: every psycopg error caught."""
    def broken(c, labels, **kw):
        raise psycopg.errors.UndefinedTable("relation does not exist")
    monkeypatch.setattr(views.graph, "unread", broken)
    with pytest.raises(psycopg.errors.UndefinedTable):
        views._peer_counts(_Conn(30000), ["pxx"], "p", ["me"], since=SINCE, count_until=_far())


def test_the_briefs_awaiting_read_past_its_bound_is_none_and_rolled_back(monkeypatch):
    """The brief's five 500s of 16 Nov were all graph.awaiting. MUTANT: the read left outside the bound."""
    monkeypatch.setattr(views.graph, "awaiting", _cancel)
    monkeypatch.setattr(views.graph, "awaiting_counts", lambda c, labels, **kw: {"open": 0, "active": 0})
    conn = _Conn(30000)
    assert views._brief_awaiting(conn, ["kxxx"], peer_id="p", since=SINCE, limit=10) is None
    assert _marks(conn) == ["SAVEPOINT", "ROLLBACK TO SAVEPOINT"]


def test_the_briefs_awaiting_read_and_its_counts_share_one_bound(monkeypatch):
    """MUTANTS: the counts read outside the savepoint; limit, not limit + 1 (the route could no longer tell
    truncated)."""
    seen = []
    monkeypatch.setattr(views.graph, "awaiting",
                        lambda c, labels, **kw: seen.append(("rows", _sets(c), kw["limit"], kw["states"])) or ["r"])
    monkeypatch.setattr(views.graph, "awaiting_counts",
                        lambda c, labels, **kw: seen.append(("counts", _sets(c))) or {"open": 1, "active": 0})
    conn = _Conn(30000)
    assert views._brief_awaiting(conn, ["kxxx"], peer_id="p", since=SINCE, limit=10) == (["r"], {"open": 1, "active": 0})
    assert seen == [("rows", [BOUND], 11, ("open", "active")), ("counts", [BOUND])]
    assert _sets(conn) == [BOUND, "30000ms"] and _marks(conn) == ["SAVEPOINT", "RELEASE SAVEPOINT"]


def test_the_brief_printer_says_unknown_and_never_zero_for_a_timed_out_awaiting():
    """MUTANT: the flag not read (the section, null, printed "awaiting: 0")."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli._print_brief({"owed": {"items": [], "counts": {"open": 0, "active": 0}, "truncated": False},
                          "awaiting": None, "awaiting_timed_out": True}, None)
    lines = [ln for ln in out.getvalue().splitlines() if ln.startswith("awaiting")]
    assert len(lines) == 1 and lines[0].startswith("awaiting: unknown") and "awaiting: 0" not in out.getvalue()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli._print_brief({"awaiting": {"items": [], "counts": {"open": 0, "active": 0}, "truncated": False},
                          "awaiting_timed_out": False}, None)
    assert "awaiting: 0" in out.getvalue() and "unknown" not in out.getvalue()


def test_the_clients_read_a_null_count_as_unknown_never_as_none_waiting():
    """What the clients of `unread_count` do with the null a timed-out count carries: the fleet table prints "?" in
    both columns, the send-time warning says nothing about a pile it cannot see, and the idle alert has no number to
    alert on. MUTANT: the server sending 0 for an unknown count (the table would print 0: a false "nothing waits")."""
    peer = {"peer_id": "abcdef0123", "effective_state": "active", "note": None, "beat_age_secs": 5,
            "state_expires_at": None, "online": True, "active": True, "active_sent_at": None,
            "unread_count": None, "unread_timed_out": True, "unanswered_from_you": None,
            "unread_oldest_age_secs": None}
    answer = {"as_of": "2026-11-16T07:00:00+00:00", "recipients": [{"label": "pxx", "peers": [peer]}]}
    rows, as_of = fleet.collect(["pxx"], lambda labels: answer)
    row = [r for r in rows if r.get("seat") == "pxx"][0]
    assert row["unread"] is None and row["yours"] is None
    line = [ln for ln in fleet.render(rows, as_of).splitlines() if ln.startswith("pxx")][0]
    assert line.split()[-3:] == ["?", "-", "?"], line      # unread, oldest, yours
    notes = recipientwarn.warnings(answer, body_bytes=10 ** 6)
    assert not [w for w in notes if w.get("warning") in ("recipient_behind", "recipient_unanswered")]
    idle_row = {"seat": "pxx", "state": "idle", "unread": None, "owed": None, "turn_age_secs": 99999}
    alerts, unknown = fleet.idle_over([idle_row], 1)
    # an idle seat whose count is unknown is neither alerted on nor dropped: it is listed apart
    assert alerts == [] and unknown == [idle_row]
    # ... but only where the count would have decided: a seat that is not idle long enough is not listed,
    # a counted zero is not unknown, and an open owed ask still alerts whatever the count
    assert fleet.idle_over([dict(idle_row, turn_age_secs=10)], 1) == ([], [])
    assert fleet.idle_over([dict(idle_row, state="working")], 1) == ([], [])
    assert fleet.idle_over([dict(idle_row, unread=0)], 1) == ([], [])
    owed_row = dict(idle_row, owed={"open": 1})
    assert fleet.idle_over([owed_row], 1) == ([owed_row], [])
