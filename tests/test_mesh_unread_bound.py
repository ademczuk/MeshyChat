"""R6 (oxxxxxx ab9f28): GET /v1/mesh/unread's query has its own statement bound.

On 8 Nov 2026 an 89-day window for a busy seat ran into the server's 30 s statement timeout twice, and the route
answered 500. views._unread_bounded runs graph.unread in a savepoint under UNREAD_TIMEOUT_MS: past it the answer is a
200 that says `timed_out`, with no items and a null total, and the request goes on; on success the request's own
statement_timeout is put back. These tests need no database: the connection is a stand-in that records what it was
asked. tests/test_mesh_unread.py holds the database regression for the route itself.

Run:  pytest tests/test_mesh_unread_bound.py -q     (needs no database)
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.mesh import views  # noqa: E402

SINCE = _dt.datetime(2026, 8, 11, tzinfo=_dt.timezone.utc)


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


def _sets(conn):
    return [params[0] for sql, params in conn.log if sql and "set_config('statement_timeout'" in sql]


def test_the_query_runs_under_its_bound_and_the_requests_own_timeout_is_put_back(monkeypatch):
    """MUTANTS: no bound set; the old timeout not put back after a released savepoint (it would bind the rest of the
    request at UNREAD_TIMEOUT_MS)."""
    conn, seen = _Conn(30000), []
    monkeypatch.setattr(views.graph, "unread", lambda c, labels, **kw: seen.append(_sets(c)) or "RESULT")
    assert views._unread_bounded(conn, ["kxxx"], peer_id="p", since=SINCE, limit=10) == "RESULT"
    assert seen == [["%dms" % views.UNREAD_TIMEOUT_MS]], "the bound is in place while the query runs"
    assert _sets(conn) == ["%dms" % views.UNREAD_TIMEOUT_MS, "30000ms"]
    assert conn.log[0][0] == "SAVEPOINT" and conn.log[-1][0] == "RELEASE SAVEPOINT"


def test_no_timeout_at_all_is_put_back_as_zero_and_a_lower_bound_is_left_alone(monkeypatch):
    monkeypatch.setattr(views.graph, "unread", lambda c, labels, **kw: "RESULT")
    off = _Conn(0)
    views._unread_bounded(off, ["kxxx"], peer_id="p", since=SINCE, limit=10)
    assert _sets(off) == ["%dms" % views.UNREAD_TIMEOUT_MS, "0"]
    low = _Conn(views.UNREAD_TIMEOUT_MS - 1)
    views._unread_bounded(low, ["kxxx"], peer_id="p", since=SINCE, limit=10)
    assert _sets(low) == [], "a bound already lower is never raised"


def test_a_query_past_its_bound_is_none_and_its_savepoint_is_rolled_back(monkeypatch):
    """The 8 Nov failure: a QueryCanceled escaped the route as a 500. MUTANT: QueryCanceled not caught."""
    def canceled(c, labels, **kw):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    monkeypatch.setattr(views.graph, "unread", canceled)
    conn = _Conn(30000)
    assert views._unread_bounded(conn, ["kxxx"], peer_id="p", since=SINCE, limit=10) is None
    assert conn.log[-1][0] == "ROLLBACK TO SAVEPOINT", "the rollback also undoes the SET LOCAL"


def test_another_database_error_is_not_taken_for_a_timeout(monkeypatch):
    """Only a cancel is a timed-out answer; anything else still fails loudly. MUTANT: every psycopg error caught."""
    def broken(c, labels, **kw):
        raise psycopg.errors.UndefinedTable("relation does not exist")
    monkeypatch.setattr(views.graph, "unread", broken)
    with pytest.raises(psycopg.errors.UndefinedTable):
        views._unread_bounded(_Conn(30000), ["kxxx"], peer_id="p", since=SINCE, limit=10)


def test_a_timed_out_answer_says_so_and_holds_no_count():
    """A null total, so meshy-mod's digest (it needs an integer) and reconcile's gate (it needs truncated false) read
    nothing from it. MUTANTS: total 0 (a false empty backlog); truncated false."""
    box = views._unread_box(None)
    assert box["timed_out"] is True and box["truncated"] is True and box["items"] == [] and box["total"] is None
    assert box["oldest_at"] is None and "shorter" in box["reason"]
    class _R:
        items, total, oldest_at, truncated = ({"key": "msg/1-a"},), 1, None, False
    ok = views._unread_box(_R())
    assert ok == {"items": [{"key": "msg/1-a"}], "total": 1, "oldest_at": None, "truncated": False, "timed_out": False}


def test_fleet_unread_bounded_sets_and_restores_timeout_and_catches_only_cancel(monkeypatch):
    conn, seen = _Conn(30000), []
    pairs = [("p1", ["kxxx"]), ("p2", ["pxx"])]
    monkeypatch.setattr(views.graph, "fleet_unread_counts",
                        lambda c, pl, **kw: seen.append((_sets(c), list(pl))) or {"p1": 1, "p2": 0})
    assert views._fleet_unread_bounded(conn, pairs, since=SINCE) == {"p1": 1, "p2": 0}
    assert seen == [(["%dms" % views.UNREAD_TIMEOUT_MS], pairs)]
    assert _sets(conn) == ["%dms" % views.UNREAD_TIMEOUT_MS, "30000ms"]
    assert conn.log[0][0] == "SAVEPOINT" and conn.log[-1][0] == "RELEASE SAVEPOINT"

    def canceled(c, pl, **kw):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    monkeypatch.setattr(views.graph, "fleet_unread_counts", canceled)
    c2 = _Conn(30000)
    assert views._fleet_unread_bounded(c2, pairs, since=SINCE) is None
    assert c2.log[-1][0] == "ROLLBACK TO SAVEPOINT"

    def broken(c, pl, **kw):
        raise psycopg.errors.UndefinedTable("relation does not exist")
    monkeypatch.setattr(views.graph, "fleet_unread_counts", broken)
    with pytest.raises(psycopg.errors.UndefinedTable):
        views._fleet_unread_bounded(_Conn(30000), pairs, since=SINCE)


def test_labels_for_peers_matches_labels_for_in_at_most_two_peer_reads(monkeypatch):
    """Batch label resolution runs at most 2 reads on `peers` (primary-key lookup + one uncontested scan)
    regardless of how many peers are covered, and agrees peer-for-peer with `labels_for` across roster
    overrides, contested duplicate names, and blank fallback names."""
    import json
    import uuid
    from apps.memoryskills import handlers

    p_roster = str(uuid.uuid4())
    p_dup1 = str(uuid.uuid4())
    p_dup2 = str(uuid.uuid4())
    p_blank = str(uuid.uuid4())
    p_unique = str(uuid.uuid4())
    all_rows = [
        (p_roster, "bxxxxx"),
        (p_dup1, "shared-seat"),
        (p_dup2, "Shared-Seat"),
        (p_blank, "   "),
        (p_unique, "nxxxxx"),
    ]

    class _PeersCursor:
        def __init__(self, log):
            self._log = log
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self._log.append((sql, params))
            if "WHERE id = ANY" in sql:
                wanted = set(params[0])
                self._rows = [(pid, name) for pid, name in all_rows if pid in wanted]
            elif "WHERE id = %s" in sql:
                wanted = params[0]
                self._rows = [(name,) for pid, name in all_rows if pid == wanted]
            elif sql == "SELECT id, name FROM peers":
                self._rows = list(all_rows)
            else:
                raise AssertionError(sql)

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _PeersConn:
        def __init__(self):
            self.log = []

        def cursor(self, **kw):
            return _PeersCursor(self.log)

    before_roster = dict(handlers._ROSTER_LABELS)
    try:
        handlers.prime_roster_labels(json.dumps({"peers": {p_roster: "memory-bxxxxx"}}).encode())
        batch_conn = _PeersConn()
        ids = [p_roster, p_dup1, p_dup2, p_blank, p_unique]
        batched = views.labels_for_peers(batch_conn, ids)
        assert len(batch_conn.log) == 2, batch_conn.log
        for pid in ids:
            single = views.labels_for(_PeersConn(), pid)
            assert batched[pid] == single, (pid, batched[pid], single)
        assert [x["label"] for x in batched[p_roster][:2]] == ["memory-bxxxxx", "bxxxxx"]
        assert len(batched[p_dup1]) == 1 and batched[p_dup1][0]["source"] == "fallback"
        assert len(batched[p_dup2]) == 1 and batched[p_dup2][0]["source"] == "fallback"
        assert len(batched[p_blank]) == 1 and batched[p_blank][0]["source"] == "fallback"
        assert batched[p_unique][0] == {"label": "nxxxxx", "source": "peers.name"}
        empty_conn = _PeersConn()
        assert views.labels_for_peers(empty_conn, []) == {}
        assert empty_conn.log == []
    finally:
        handlers.prime_roster_labels(json.dumps({"peers": before_roster}).encode())
