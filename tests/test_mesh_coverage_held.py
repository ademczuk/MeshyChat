"""The coverage pairs are held per process (divergence 107).

3 Nov 2026 (kxxx, production, read-only EXPLAIN ANALYZE as meshychat_app in server_internal):
graph.coverage took 3,665 ms and rode on every inbox, owed, awaiting, brief, unread, fleet-unread
and mentions answer, about 880 an hour; related_reads.coverage took 4,837 ms, 881 of them JIT.
So graph.coverage now counts its numerator with EXISTS (the same count: the join it replaced
matched e.src_id against sm.id, the primary key) and each server process holds the pair
COVERAGE_TTL_S per (database, namespace); related_reads holds its figures COVERAGE_TTL_S too and
runs its statement with JIT off.

4 Nov 2026 (production log): the count's parallel hash join ran out of the container's 64 MB /dev/shm, and
33 mail reads answered 500 over a number they only carry. So the count now runs with no parallel workers,
in a savepoint, and a failed count serves the pair already held.

The holding is tested with a fake connection and a moved clock, never a sleep. The count and the
JIT setting are tested on the run's database, as the application role inside server_internal.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import types

import psycopg
import pytest

from tests.test_mesh_routes import (DSN, NS, P, _edit_row_at, _peer, _row,  # noqa: F401
                                    _sqlmigrate, client, mesh_settings, owner)
from apps.mesh import graph

# The count graph.coverage made before divergence 107, kept here as the reference the EXISTS form must equal.
_JOIN_PAIR = (
    "SELECT "
    "  (SELECT count(DISTINCT e.src_id) FROM mesh_edges e "
    "     JOIN shared_memory sm ON sm.id = e.src_id AND sm.is_head "
    "    WHERE sm.namespace = %s), "
    "  (SELECT count(*) FROM shared_memory "
    "    WHERE namespace = %s AND is_head AND key LIKE 'msg/%%')")


class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sql = sql
        if "count(*)" in sql:
            self._conn.statements.append((sql, params))

    def fetchone(self):
        if "current_setting" in self._sql:
            return ("2",)
        answer = self._conn.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Conn:
    """A connection that answers graph.coverage's count from a script and counts it. An Exception in the
    script is raised where the count would fail."""

    def __init__(self, dbname: str, *answers):
        self.info = types.SimpleNamespace(host="db.test", port=5432, dbname=dbname)
        self.answers = list(answers)
        self.statements: list = []

    def cursor(self, **_kw):
        return _Cursor(self)

    def transaction(self):
        return contextlib.nullcontext()


@pytest.fixture()
def clock(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(graph, "_clock", lambda: now[0])
    return now


def test_the_pair_is_held_for_the_ttl_then_read_again(clock):
    conn = _Conn("db_one", (5, 6), (7, 8))
    assert graph.coverage(conn) == (5, 6)
    clock[0] += graph.COVERAGE_TTL_S - 0.001
    assert graph.coverage(conn) == (5, 6) and len(conn.statements) == 1, "held: no second count"
    clock[0] += 0.002
    assert graph.coverage(conn) == (7, 8) and len(conn.statements) == 2, "expired: counted again"
    assert graph.COVERAGE_TTL_S == 60.0


def test_the_pair_is_held_per_database_and_per_namespace(clock):
    a, b = _Conn("db_a", (1, 1)), _Conn("db_b", (2, 2))
    assert graph.coverage(a) == (1, 1) and graph.coverage(b) == (2, 2)
    assert graph.coverage(a) == (1, 1) and len(a.statements) == 1, "db_b's pair did not replace db_a's"
    other = _Conn("db_a", (3, 3))
    assert graph.coverage(other, namespace="shared/other") == (3, 3), "another namespace has its own pair"
    assert len(graph._coverage_held) == 3


def test_a_zero_ttl_holds_nothing(monkeypatch, clock):
    """The fresh-install runbook's server runs with COVERAGE_TTL_S = 0 (its step 6 reads the pair
    before and right after the edges job): every call counts again."""
    monkeypatch.setattr(graph, "COVERAGE_TTL_S", 0)
    conn = _Conn("db_zero", (1, 2), (3, 4))
    assert graph.coverage(conn) == (1, 2) and graph.coverage(conn) == (3, 4)


def test_forget_drops_every_held_pair(clock):
    conn = _Conn("db_forget", (1, 1), (2, 2))
    assert graph.coverage(conn) == (1, 1)
    graph.forget_coverage()
    assert graph.coverage(conn) == (2, 2) and len(conn.statements) == 2


def test_a_failed_count_serves_the_held_pair_and_holds_it_one_more_ttl(clock):
    conn = _Conn("db_fails", (5, 6), psycopg.errors.DiskFull("no space"), (7, 8))
    assert graph.coverage(conn) == (5, 6)
    clock[0] += graph.COVERAGE_TTL_S + 1
    assert graph.coverage(conn) == (5, 6) and len(conn.statements) == 2, "failed: the held pair is served"
    clock[0] += graph.COVERAGE_TTL_S - 0.001
    assert graph.coverage(conn) == (5, 6) and len(conn.statements) == 2, "one failed count per TTL, not per read"
    clock[0] += 0.002
    assert graph.coverage(conn) == (7, 8) and len(conn.statements) == 3, "the next expiry counts again"


def test_a_failed_count_with_nothing_held_raises(clock):
    conn = _Conn("db_empty", psycopg.errors.DiskFull("no space"), (1, 2))
    with pytest.raises(psycopg.errors.DiskFull):
        graph.coverage(conn)
    assert graph.coverage(conn) == (1, 2), "a failure held nothing"


def test_an_error_that_is_not_the_databases_is_not_swallowed(clock):
    conn = _Conn("db_bug", (5, 6), ValueError("a bug in this code"))
    assert graph.coverage(conn) == (5, 6)
    clock[0] += graph.COVERAGE_TTL_S + 1
    with pytest.raises(ValueError):
        graph.coverage(conn)


def _pairs(conn) -> tuple[tuple[int, int], tuple[int, int]]:
    """(the EXISTS pair graph.coverage counts now, the join pair it counted before), both read fresh."""
    graph.forget_coverage()
    new = graph.coverage(conn)
    old = tuple(int(x) for x in conn.execute(_JOIN_PAIR, (NS, NS)).fetchone())
    return new, old


def test_the_exists_count_equals_the_join_it_replaced(owner, monkeypatch):
    """A projected head counts; an unprojected head does not; an edited message whose old version
    has edges and whose new head has none yet counts on neither side. Both forms are read on the
    application connection inside server_internal, the context every view reads them in, and the
    world's own heads are read as the change from a baseline, since the run's database is shared."""
    from apps.core import dbconn

    monkeypatch.setattr(graph, "COVERAGE_TTL_S", 0)
    p = _peer(owner, "cov")
    # Project first: P.rebuild below projects EVERY head in the run's shared database, so heads an earlier test left
    # unprojected would otherwise move the projected count too (HQB gate mgbatch17: (24, 3) for (1, 3)). After this,
    # only this test's own rows can move the pair.
    P.rebuild(owner)
    conn = dbconn.connect(DSN)
    try:
        with dbconn.request_server_internal(conn) as c:
            base_new, base_old = _pairs(c)
        assert base_new == base_old, (base_new, base_old)

        kept = _row(owner, p, {"to": "x", "body": "projected"})
        edited = _row(owner, p, {"to": "x", "body": "projected, then edited"})
        P.rebuild(owner)
        _row(owner, p, {"to": "x", "body": "written after the projection ran"})
        with owner.transaction():
            owner.execute("SELECT set_config('app.server_internal', '1', true)")
            prior = owner.execute("SELECT id, created_at FROM shared_memory WHERE namespace = %s "
                                  "AND key = %s AND is_head", (NS, edited)).fetchone()
        _edit_row_at(owner, p, edited, {"to": "x", "body": "the edit"}, prior[0],
                     prior[1] + _dt.timedelta(seconds=1))

        with dbconn.request_server_internal(conn) as c:
            new, old = _pairs(c)
    finally:
        conn.close()
    assert new == old, (new, old)
    assert (new[0] - base_new[0], new[1] - base_new[1]) == (1, 3), (base_new, new, kept)


_COUNT_MARK = "EXISTS (SELECT 1 FROM mesh_edges e WHERE e.src_id = sm.id)"


def _workers(conn) -> str:
    row = conn.execute("SHOW max_parallel_workers_per_gather").fetchone()
    return row["max_parallel_workers_per_gather"] if isinstance(row, dict) else row[0]


def test_the_count_runs_with_no_parallel_workers_and_gives_the_setting_back(owner):
    """The setting is read on the same connection right before the count runs, and again after coverage
    returns, so the rest of the read keeps what its transaction had."""
    from apps.core import dbconn

    seen: list[str] = []

    class _ShowWorkersFirst(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            if isinstance(query, str) and _COUNT_MARK in query:
                seen.append(_workers(self.connection))
            return super().execute(query, params, **kw)

    graph.forget_coverage()
    conn = dbconn.connect(DSN)
    try:
        with dbconn.request_server_internal(conn) as c:
            c.execute("SET LOCAL max_parallel_workers_per_gather = 3")
            c.cursor_factory = _ShowWorkersFirst
            graph.coverage(c)
            after = _workers(c)
    finally:
        conn.close()
    assert seen == ["0"], seen
    assert after == "3", after


def test_a_real_failed_count_leaves_the_read_it_rides_on_alive(owner, clock):
    """A real server error, in the real transaction a view reads in: the transaction must still answer, and
    the savepoint must take the setting back with it. Without the savepoint the next statement of the read
    is refused (InFailedSqlTransaction), which is the 500 this replaces."""
    from apps.core import dbconn

    class _FailTheCount(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            if isinstance(query, str) and _COUNT_MARK in query:
                return super().execute("SELECT 1/0")
            return super().execute(query, params, **kw)

    graph.forget_coverage()
    conn = dbconn.connect(DSN)
    try:
        with dbconn.request_server_internal(conn) as c:
            first = graph.coverage(c)
            clock[0] += graph.COVERAGE_TTL_S + 1
            c.execute("SET LOCAL max_parallel_workers_per_gather = 3")
            c.cursor_factory = _FailTheCount
            assert graph.coverage(c) == first
            assert _workers(c) == "3", "the read's transaction still answers, with its own setting"
    finally:
        conn.close()


def test_related_coverage_runs_with_jit_off_and_gives_the_setting_back(owner, monkeypatch):
    """related_reads.coverage turns JIT off for its own statement only: SHOW jit is read on the same
    connection right before that statement runs, and again after coverage returns."""
    from apps.core import dbconn
    from apps.meshsem import related_reads, spaces

    seen: list[str] = []

    def _jit(conn) -> str:
        row = conn.execute("SHOW jit").fetchone()
        return row["jit"] if isinstance(row, dict) else row[0]

    class _ShowJitFirst(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            if isinstance(query, str) and "AS pairs_refused" in query:
                seen.append(_jit(self.connection))
            return super().execute(query, params, **kw)

    related_reads.forget()
    conn = dbconn.connect(DSN)
    try:
        with dbconn.request_server_internal(conn) as c:
            c.execute("SET LOCAL jit = on")
            c.cursor_factory = _ShowJitFirst
            out = related_reads.coverage(c, spaces.get(spaces.DEFAULT_SPACE), gate=False, cal=None)
            after = _jit(c)
    finally:
        conn.close()
    assert seen == ["off"], seen
    assert after == "on", after
    assert set(out) >= {"eligible", "processed", "edges", "measured_at"}, out
