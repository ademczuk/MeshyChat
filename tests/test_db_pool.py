"""The request-path connection pool (apps/core/dbpool.py), OFF unless MESHYCHAT_DB_POOL_SIZE is set.
Database-free: libpq's connection is a stand-in, and the reset and the role check are recorded. What a real
session does across a lease (DISCARD ALL, the role, the peer context) is tests/test_db_pool_db.py, on the gate.

Run:  python -m pytest tests/test_db_pool.py -q -p no:cacheprovider --noconftest
"""
from __future__ import annotations

import logging
import socket
import threading
import time

import psycopg
import pytest
from psycopg.pq import ConnStatus, PipelineStatus, TransactionStatus

from apps.core import dbconn, dbpool, rls

ENV = dbpool.POOL_SIZE_ENV
KEY = ("postgresql://u:p@127.0.0.1:1/db", "-c statement_timeout=30000 -c TimeZone=UTC", rls.APP_ROLE)


class FakePG:
    """libpq's connection, as far as the pool looks at it."""

    def __init__(self, name="pg"):
        self.name, self.status, self.transaction_status = name, ConnStatus.OK, TransactionStatus.IDLE
        self.pipeline_status, self.finished, self.queued, self.consumed = PipelineStatus.OFF, 0, [], 0

    def consume_input(self):
        self.consumed += 1

    def notifies(self):
        return self.queued.pop(0) if self.queued else None

    def finish(self):
        self.finished += 1
        self.status = ConnStatus.BAD


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    dbpool.drain()
    for k in dbpool._stats:
        dbpool._stats[k] = 0
    dbpool._said.clear()
    # The stand-ins speak libpq's OLDER cancel (get_cancel) unless a test asks for the cancel connection; and no
    # cancel worker of one test may be alive in the next (round 7).
    monkeypatch.setattr(dbpool, "_has_safe_cancel", lambda: False, raising=False)
    yield
    dbpool.drain()
    end = time.monotonic() + 15
    while getattr(dbpool, "_canceller", [None])[0] is not None and time.monotonic() < end:
        time.sleep(0.01)
    getattr(dbpool, "_cancels", []).clear()


@pytest.fixture()
def resets(monkeypatch):
    seen = []
    monkeypatch.setattr(dbpool, "_reset", lambda pgconn, key: seen.append((pgconn, key)))
    return seen


def ok(lease):
    return None


# ------------------------------------------------------------------------------------------------ the switch

@pytest.mark.parametrize("raw, want", [
    (None, 0), ("0", 0), ("1", 1), ("8", 8), (str(dbpool.MAX_POOL_SIZE), dbpool.MAX_POOL_SIZE), (" 4 ", 4),
    (str(dbpool.MAX_POOL_SIZE + 1), 0), ("-1", 0), ("1.5", 0), ("four", 0), ("", 0), ("４", 0), ("true", 0),
])
def test_the_pool_is_off_unless_the_setting_is_a_whole_number_in_range(monkeypatch, raw, want):
    if raw is not None:
        monkeypatch.setenv(ENV, raw)
    assert dbpool.size() == want


def test_on_a_driver_that_cannot_bound_a_wait_the_pool_stays_off_and_says_so_once(monkeypatch, caplog, resets):
    """The Codex review of 9d0a4f50: requirements.txt allows psycopg 3.1 and 3.2, whose Connection.wait takes no
    `timeout`; the pool's reset passes one, so every return would fail and be closed with a warning."""
    assert dbpool._driver_bounds_a_wait() is True                          # the driver this suite runs on (3.3 or later)

    class Old:
        def wait(self, gen, interval=0.1):                                 # psycopg 3.2's signature
            return None
    monkeypatch.setattr(dbpool.psycopg, "Connection", Old)
    assert dbpool._driver_bounds_a_wait() is False
    monkeypatch.setenv(ENV, "4")
    with caplog.at_level(logging.WARNING, logger="meshychat.db"):
        assert [dbpool.size() for _ in range(5)] == [0] * 5
    said = [r for r in caplog.records if "cannot bound a wait" in r.getMessage()]
    assert len(said) == 1 and "stays off" in said[0].getMessage()
    pg = FakePG()
    dbpool.give_back(pg, KEY)                                              # a return with the pool off: closed, never reset
    assert pg.finished == 1 and resets == [] and dbpool.stats()["idle"] == 0
    monkeypatch.setenv(ENV, "0")
    assert dbpool.size() == 0                                              # 0 is 0 on any driver, with nothing more said
    assert len([r for r in caplog.records if "cannot bound a wait" in r.getMessage()]) == 1


def test_a_setting_that_does_not_parse_is_said_once_and_never_switches_the_pool_on(monkeypatch, caplog):
    monkeypatch.setenv(ENV, "lots")
    with caplog.at_level(logging.WARNING, logger="meshychat.db"):
        assert [dbpool.size() for _ in range(5)] == [0] * 5
    said = [r for r in caplog.records if "connection pool stays off" in r.getMessage()]
    assert len(said) == 1 and "'lots'" in said[0].getMessage()


# ------------------------------------------------------------------------------------------------ give back

def test_a_returned_idle_session_is_reset_and_kept(monkeypatch, resets):
    monkeypatch.setenv(ENV, "2")
    pg = FakePG()
    dbpool.give_back(pg, KEY)
    assert resets == [(pg, KEY)] and pg.finished == 0
    assert dbpool.stats()["idle"] == 1 and dbpool.stats()["returned"] == 1


@pytest.mark.parametrize("change, counter", [
    (lambda pg: setattr(pg, "transaction_status", TransactionStatus.INTRANS), "closed_not_idle"),
    (lambda pg: setattr(pg, "transaction_status", TransactionStatus.INERROR), "closed_not_idle"),
    (lambda pg: setattr(pg, "transaction_status", TransactionStatus.ACTIVE), "closed_not_idle"),
    (lambda pg: setattr(pg, "transaction_status", TransactionStatus.UNKNOWN), "closed_not_idle"),
    (lambda pg: setattr(pg, "status", ConnStatus.BAD), "closed_not_idle"),
])
def test_a_session_that_is_not_idle_and_healthy_is_closed_not_kept(monkeypatch, resets, change, counter):
    monkeypatch.setenv(ENV, "2")
    pg = FakePG()
    change(pg)
    dbpool.give_back(pg, KEY)
    assert pg.finished == 1 and resets == [] and dbpool.stats()["idle"] == 0 and dbpool.stats()[counter] == 1


def test_a_session_whose_reset_fails_is_closed_and_nothing_is_raised(monkeypatch, caplog):
    monkeypatch.setenv(ENV, "2")

    def boom(pgconn, key):
        raise psycopg.OperationalError("server closed the connection unexpectedly")
    monkeypatch.setattr(dbpool, "_reset", boom)
    pg = FakePG()
    with caplog.at_level(logging.WARNING, logger="meshychat.db"):
        dbpool.give_back(pg, KEY)
    assert pg.finished == 1 and dbpool.stats()["idle"] == 0 and dbpool.stats()["closed_reset_failed"] == 1
    assert any("did not reset" in r.getMessage() for r in caplog.records)


def test_the_pool_keeps_no_more_than_its_size_and_a_full_pool_resets_nothing_more(monkeypatch, resets):
    monkeypatch.setenv(ENV, "2")
    pgs = [FakePG("pg%d" % n) for n in range(4)]
    for pg in pgs:
        dbpool.give_back(pg, KEY)
    assert [pg.finished for pg in pgs] == [0, 0, 1, 1] and len(resets) == 2
    assert dbpool.stats()["idle"] == 2 and dbpool.stats()["closed_full"] == 2


def test_a_pool_that_fills_while_a_session_resets_closes_that_session(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    other, late = FakePG("other"), FakePG("late")

    def slow_reset(pgconn, key):
        if pgconn is late:                                                # another thread's session lands meanwhile
            dbpool._idle.append((other, KEY, dbpool.time.monotonic()))
    monkeypatch.setattr(dbpool, "_reset", slow_reset)
    dbpool.give_back(late, KEY)
    assert late.finished == 1 and other.finished == 0 and dbpool.stats()["idle"] == 1


@pytest.mark.parametrize("setting, key", [(None, KEY), ("0", KEY), ("junk", KEY), ("2", None)])
def test_with_the_pool_off_or_no_key_a_returned_session_is_simply_closed(monkeypatch, resets, setting, key):
    if setting is not None:
        monkeypatch.setenv(ENV, setting)
    pg = FakePG()
    dbpool.give_back(pg, key)
    assert pg.finished == 1 and resets == [] and dbpool.stats()["idle"] == 0


def test_a_fault_inside_the_pool_closes_the_session_and_never_reaches_the_caller(monkeypatch, resets):
    monkeypatch.setenv(ENV, "2")
    monkeypatch.setattr(dbpool, "size", lambda: 1 / 0)
    pg = FakePG()
    dbpool.give_back(pg, KEY)                                             # no exception
    assert pg.finished == 1 and dbpool.stats()["idle"] == 0


# ------------------------------------------------------------------------------------------------ take

def test_take_gives_the_newest_idle_session_as_a_new_proven_lease(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    a, b = FakePG("a"), FakePG("b")
    dbpool.give_back(a, KEY)
    dbpool.give_back(b, KEY)
    proven = []
    lease = dbpool.take(KEY, proven.append)
    assert isinstance(lease, dbpool.Lease) and lease.pgconn is b and proven == [lease]
    assert lease._pool_key == KEY and lease.autocommit is True and lease.closed is False
    assert dbpool.stats()["idle"] == 1 and dbpool.stats()["reused"] == 1
    assert dbpool.take(KEY, ok).pgconn is a and dbpool.take(KEY, ok) is None


def test_take_with_nothing_idle_is_none(monkeypatch):
    monkeypatch.setenv(ENV, "4")
    assert dbpool.take(KEY, ok) is None


def test_a_session_kept_for_another_dsn_or_bound_is_closed_not_handed_out(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    other = FakePG()
    dbpool.give_back(other, ("postgresql://elsewhere/db", KEY[1], KEY[2]))
    assert dbpool.take(KEY, ok) is None
    assert other.finished == 1 and dbpool.stats()["closed_other_key"] == 1


def test_a_session_idle_for_too_long_is_closed_not_handed_out(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    clock = [1000.0]
    monkeypatch.setattr(dbpool.time, "monotonic", lambda: clock[0])
    old, young = FakePG("old"), FakePG("young")
    dbpool.give_back(old, KEY)
    clock[0] += dbpool.MAX_IDLE_S + 1
    dbpool.give_back(young, KEY)
    assert dbpool.take(KEY, ok).pgconn is young
    assert dbpool.take(KEY, ok) is None and old.finished == 1 and dbpool.stats()["closed_stale"] == 1
    edge = FakePG("edge")
    dbpool.give_back(edge, KEY)
    clock[0] += dbpool.MAX_IDLE_S                                         # exactly the limit is still reused
    assert dbpool.take(KEY, ok).pgconn is edge


def test_a_session_the_server_dropped_is_thrown_away_and_the_next_one_is_tried(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    good, dead = FakePG("good"), FakePG("dead")
    dbpool.give_back(good, KEY)
    dbpool.give_back(dead, KEY)

    def verify(lease):
        if lease.pgconn is dead:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
    lease = dbpool.take(KEY, verify)
    assert lease.pgconn is good and dead.finished == 1 and dbpool.stats()["closed_dead"] == 1


def test_a_pooled_session_that_fails_the_role_check_is_closed_and_said_as_an_error(monkeypatch, resets, caplog):
    monkeypatch.setenv(ENV, "4")
    bad = FakePG("bad")
    dbpool.give_back(bad, KEY)

    def verify(lease):
        raise dbconn.RlsBypassError("running as 'meshychat_owner', which BYPASSES row-level security")
    with caplog.at_level(logging.ERROR, logger="meshychat.db"):
        assert dbpool.take(KEY, verify) is None
    assert bad.finished == 1 and dbpool.stats()["closed_bypass"] == 1
    assert any("failed its role check" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_a_fault_inside_take_means_connect_as_usual(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    dbpool.give_back(FakePG(), KEY)
    monkeypatch.setattr(dbpool, "_lease", lambda pgconn, key: 1 / 0)
    assert dbpool.take(KEY, ok) is None


# ------------------------------------------------------------------------------------------------ a lease

def test_closing_a_lease_kills_that_object_for_good_and_gives_its_session_back_once(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    cur = lease.cursor()
    lease.close()
    lease.close()                                                        # a second close is nothing
    assert lease.closed is True and len(resets) == 1 and dbpool.stats()["idle"] == 1 and pg.finished == 0
    # the same session goes to someone else: the old object and its cursor stay dead
    again = dbpool.take(KEY, ok)
    assert again is not lease and again.pgconn is pg and again.closed is False
    for use in (lambda: lease.execute("SELECT 1"), lambda: cur.execute("SELECT 1"), lambda: lease.commit(),
                lambda: lease.transaction().__enter__(), lambda: lease.cursor().execute("SELECT 1")):
        with pytest.raises(psycopg.OperationalError):
            use()
    assert lease.closed is True


def test_a_released_lease_never_starts_the_statement_it_was_given(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    lease = dbpool._lease(FakePG(), KEY)
    lease.close()
    started = []

    def gen():
        started.append(1)
        yield

    with pytest.raises(psycopg.OperationalError, match="returned to the pool"):
        lease.wait(gen())
    assert started == []


def test_discard_closes_the_session_for_real(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    lease.discard()
    lease.discard()
    lease.close()
    assert pg.finished == 1 and resets == [] and dbpool.stats()["idle"] == 0 and lease.closed is True


def test_a_lease_whose_session_died_reports_closed(monkeypatch):
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    pg.status = ConnStatus.BAD
    assert lease.closed is True


def test_the_reset_runs_discard_all_then_binds_the_role_again_and_leaves_no_usable_object(monkeypatch):
    ran = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a):
            ran.append(sql)
    workers = []

    class Worker:
        _released, _deadline, pgconn = False, None, None

        def cursor(self):
            return Cur()
    monkeypatch.setattr(dbpool, "_lease", lambda pgconn, key: workers.append(Worker()) or workers[-1])
    dbpool._reset(FakePG(), KEY)
    assert ran == ["DISCARD ALL", 'SET ROLE "%s"' % rls.APP_ROLE, "SET %s = 'on'" % rls.SIGNED_BYTES_WRITER_SETTING]
    assert workers[0]._released is True

    class Failing(Worker):
        def cursor(self):
            raise psycopg.OperationalError("gone")
    monkeypatch.setattr(dbpool, "_lease", lambda pgconn, key: workers.append(Failing()) or workers[-1])
    with pytest.raises(psycopg.OperationalError):
        dbpool._reset(FakePG(), KEY)
    assert workers[-1]._released is True                                  # dead on the failure path too


# ------------------------------------------------------------------------------------------------ dbconn.connect

class Seam:
    """dbconn.connect with every wire call replaced: what it opened, bound and checked."""

    def __init__(self, monkeypatch):
        self.plain, self.leased, self.bound, self.checked = [], [], [], []
        monkeypatch.setattr(dbconn.psycopg, "connect", self._plain)
        monkeypatch.setattr(dbpool.Lease, "connect", classmethod(self._lease))
        monkeypatch.setattr(rls, "set_role", lambda conn, role=rls.APP_ROLE: self.bound.append((conn, role)))
        monkeypatch.setattr(dbconn, "assert_not_bypassing", self.checked.append)
        monkeypatch.setattr(dbconn, "dsn", lambda: KEY[0])

    def _plain(self, target, **kw):
        class Plain:
            closed_ = 0

            def close(self):
                self.closed_ += 1
        self.plain.append((target, kw))
        return Plain()

    def _lease(self, cls, target, **kw):
        self.leased.append((target, kw))
        return dbpool._lease(FakePG("fresh%d" % len(self.leased)), None)


def test_with_the_pool_off_connect_is_what_it_was_and_asks_the_pool_nothing(monkeypatch, resets):
    s = Seam(monkeypatch)
    monkeypatch.setattr(dbpool, "take", lambda *a: 1 / 0)
    monkeypatch.setattr(dbpool, "fresh", lambda: 1 / 0)
    conn = dbconn.connect()
    assert len(s.plain) == 1 and s.leased == [] and not isinstance(conn, dbpool.Lease)
    assert s.bound == [(conn, rls.APP_ROLE)] and s.checked == [conn]
    conn.close()
    assert conn.closed_ == 1 and dbpool.stats()["idle"] == 0


def test_with_the_pool_on_the_second_connect_reuses_the_first_ones_session(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    s = Seam(monkeypatch)
    first = dbconn.connect()
    assert isinstance(first, dbpool.Lease) and len(s.leased) == 1 and s.plain == []
    assert first._pool_key == (KEY[0], s.leased[0][1]["options"], rls.APP_ROLE)
    assert s.bound == [(first, rls.APP_ROLE)] and s.checked == [first] and dbpool.stats()["fresh"] == 1
    pg = first.pgconn
    first.close()
    assert resets == [(pg, first._pool_key)]
    second = dbconn.connect()
    assert second is not first and second.pgconn is pg and len(s.leased) == 1          # no new session opened
    assert s.checked == [first, second]                                   # the reused one was proven too
    assert len(s.bound) == 1                                              # its role came from the reset, not here
    assert first.closed is True and second.closed is False and dbpool.stats()["reused"] == 1


@pytest.mark.parametrize("kw", [
    dict(statement_timeout_ms=5000), dict(statement_timeout_ms=0), dict(application_name="ws:1"),
    dict(connect_timeout=3), dict(role="meshychat_owner"), dict(role=""), dict(verify_role=False),
    dict(target="postgresql://u:p@127.0.0.1:1/another"),
])
def test_a_connection_asked_for_with_any_argument_of_its_own_never_touches_the_pool(monkeypatch, resets, kw):
    monkeypatch.setenv(ENV, "4")
    s = Seam(monkeypatch)
    dbpool.give_back(FakePG("idle"), (KEY[0], "-c statement_timeout=30000 -c TimeZone=UTC", rls.APP_ROLE))
    monkeypatch.setattr(dbpool, "take", lambda *a: 1 / 0)
    kw = dict(kw)
    conn = dbconn.connect(kw.pop("target", None), **kw)
    assert len(s.plain) == 1 and s.leased == [] and not isinstance(conn, dbpool.Lease)
    conn.close()
    assert dbpool.stats()["idle"] == 1                                    # and it is not returned to it either


def test_the_configured_dsn_named_outright_is_the_same_as_naming_none(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    s = Seam(monkeypatch)
    assert isinstance(dbconn.connect(KEY[0]), dbpool.Lease) and len(s.leased) == 1


def test_a_new_session_whose_role_binding_fails_is_closed_for_real_not_pooled(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    s = Seam(monkeypatch)

    def refuse(conn):
        raise dbconn.RlsBypassError("bypasses")
    monkeypatch.setattr(dbconn, "assert_not_bypassing", refuse)
    made, handles = [], []
    real = dbpool._lease
    monkeypatch.setattr(dbpool, "_lease", lambda pg, key: (handles.append(pg), made.append(real(pg, key)))[1] or made[-1])
    with pytest.raises(dbconn.RlsBypassError):
        dbconn.connect()
    assert len(made) == 1 and handles[0].finished == 1 and resets == [] and dbpool.stats()["idle"] == 0
    assert made[0].closed is True and isinstance(made[0].pgconn, dbpool._Gone)


def test_a_statement_bound_changed_in_the_environment_is_another_key(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    s = Seam(monkeypatch)
    first = dbconn.connect()
    pg = first.pgconn
    first.close()
    monkeypatch.setenv(dbconn.STATEMENT_TIMEOUT_ENV, "5000")
    second = dbconn.connect()
    assert second.pgconn is not pg and pg.finished == 1 and len(s.leased) == 2


# ------------------------------------------------------------------------------------------------ many threads

def test_no_session_is_ever_held_by_two_leases_at_once(monkeypatch, resets):
    monkeypatch.setenv(ENV, "3")
    held, errors, lock = set(), [], threading.Lock()

    def worker(n):
        try:
            for i in range(300):
                lease = dbpool.take(KEY, ok) or dbpool._lease(FakePG("t%d-%d" % (n, i)), KEY)
                with lock:
                    assert id(lease.pgconn) not in held, "one session, two leases"
                    held.add(id(lease.pgconn))
                with lock:
                    held.discard(id(lease.pgconn))
                lease.close()
        except BaseException as exc:  # noqa: BLE001 - reported by the main thread
            errors.append(exc)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and dbpool.stats()["idle"] <= 3 and dbpool.stats()["reused"] > 0


# ------------------------------------------------------------------------------------------------ round 2 (nxxxxx d56023)

def test_one_lease_closed_by_many_threads_at_once_returns_its_session_once(monkeypatch):
    """P1: the released test and its set were two steps, so two closers both passed and the backend went back twice."""
    monkeypatch.setenv(ENV, "8")
    returned = []
    monkeypatch.setattr(dbpool, "give_back", lambda pgconn, key: returned.append(pgconn))
    for _round in range(200):
        lease = dbpool._lease(FakePG(), KEY)
        gate = threading.Barrier(6)

        def closer(n):
            gate.wait()
            (lease.close if n % 2 else lease.discard)()
        threads = [threading.Thread(target=closer, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    # each lease: exactly one closer got the handle; a close returns it, a discard finishes it, never both, never twice
    assert len(returned) == len(set(map(id, returned))) and len(returned) <= 200
    assert all(pg.finished == 0 for pg in returned)                       # a returned session was not also finished


def test_the_handle_is_taken_exactly_once_and_the_lease_holds_none_after(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    assert lease._take_handle() == (pg, True)
    assert lease._take_handle() == (None, False) and lease._take_handle() == (None, False)
    assert isinstance(lease.pgconn, dbpool._Gone) and lease.closed is True
    assert lease.pgconn.status == ConnStatus.BAD and lease.pgconn.finish() is None
    with pytest.raises(psycopg.OperationalError):
        lease.pgconn.exec_(b"SELECT 1")                                   # the stand-in runs nothing
    with pytest.raises(psycopg.OperationalError):
        lease.pgconn.socket


def test_a_closed_lease_no_longer_hands_out_the_live_handle(monkeypatch, resets):
    """P1: `lease.pgconn` read after close() was the handle the NEXT lease runs on."""
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    first = dbpool._lease(pg, KEY)
    first.close()
    second = dbpool.take(KEY, ok)
    assert second.pgconn is pg and first.pgconn is not pg and isinstance(first.pgconn, dbpool._Gone)
    discarded = dbpool._lease(FakePG(), KEY)
    live = discarded.pgconn
    discarded.discard()
    assert isinstance(discarded.pgconn, dbpool._Gone) and live.finished == 1


def test_a_session_that_is_already_idle_is_not_kept_twice(monkeypatch, resets, caplog):
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    dbpool.give_back(pg, KEY)
    with caplog.at_level(logging.ERROR, logger="meshychat.db"):
        dbpool.give_back(pg, KEY)
    assert dbpool.stats()["idle"] == 1 and dbpool.stats()["double_return"] == 1 and pg.finished == 0
    assert len(resets) == 1 and any("already idle" in r.getMessage() for r in caplog.records)
    assert dbpool.take(KEY, ok).pgconn is pg and dbpool.take(KEY, ok) is None


def test_no_module_under_apps_touches_the_raw_handle_but_the_pool():
    """The boundary this design cannot hold by itself: a raw libpq handle saved before close() is the object the next
    lease uses. So nothing under apps/ may reach for `.pgconn` at all, except the pool."""
    import ast
    import pathlib
    root = pathlib.Path(dbpool.__file__).resolve().parents[1]
    assert root.name == "apps"
    users = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or path.relative_to(root).as_posix() == "core/dbpool.py":
            continue                                     # the pool itself, by its PATH: no other dbpool.py is excused
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        if any(isinstance(n, ast.Attribute) and n.attr == "pgconn" for n in ast.walk(tree)):
            users.append(path.relative_to(root).as_posix())
    assert users == [], "these modules reach for the raw libpq handle, which a pooled lease cannot revoke: %s" % users


def test_the_pools_own_round_trips_are_bounded_on_the_client(monkeypatch):
    """P2: the proof at checkout and the reset on return had no client-side deadline."""
    seen = []

    def base_wait(self, gen, *args, **kwargs):
        seen.append(kwargs.get("timeout"))
        gen.close()
        return "ran"
    monkeypatch.setattr(psycopg.Connection, "wait", base_wait)

    def gen():
        yield
    lease = dbpool._lease(FakePG(), KEY)
    assert lease.wait(gen()) == "ran" and seen == [None]                  # a caller's own statement: as it always was
    clock = [500.0]
    monkeypatch.setattr(dbpool.time, "monotonic", lambda: clock[0])
    lease._deadline = 503.0
    lease.wait(gen())
    clock[0] = 502.5
    lease.wait(gen())
    clock[0] = 600.0                                                     # past the deadline: still a tiny positive wait
    lease.wait(gen())
    assert seen[1:] == [3.0, 0.5, 0.05]


def test_checkout_proves_under_a_deadline_and_hands_the_lease_out_without_one(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    clock = [1000.0]
    monkeypatch.setattr(dbpool.time, "monotonic", lambda: clock[0])
    dbpool.give_back(FakePG(), KEY)
    during = []
    lease = dbpool.take(KEY, lambda l: during.append(l._deadline))
    assert during == [1000.0 + dbpool.POOL_IO_TIMEOUT_S] and lease._deadline is None


def test_a_session_that_does_not_answer_the_proof_in_time_is_closed_and_the_next_is_tried(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    good, stuck = FakePG("good"), FakePG("stuck")
    dbpool.give_back(good, KEY)
    dbpool.give_back(stuck, KEY)

    def verify(lease):
        if lease.pgconn is stuck:
            raise psycopg.errors._WaitTimeout("timeout expired")
    assert dbpool.take(KEY, verify).pgconn is good and stuck.finished == 1 and dbpool.stats()["closed_dead"] == 1


def test_one_bound_covers_the_whole_checkout_and_what_it_did_not_try_stays_idle(monkeypatch, resets):
    """#650, codex-kxxx-cli 624f66 P2: each popped session got a fresh POOL_IO_TIMEOUT_S, so 16 sessions gone dark
    held one request for 16 proof windows before its own fresh connect. Now every proof shares ONE deadline set when
    take() starts; once it is spent take() returns None (the caller connects) and the session not yet tried is left
    idle, not closed."""
    monkeypatch.setenv(ENV, "4")
    clock = [1000.0]
    monkeypatch.setattr(dbpool.time, "monotonic", lambda: clock[0])
    sessions = [FakePG("a"), FakePG("b"), FakePG("c")]
    for s in sessions:
        dbpool.give_back(s, KEY)
    deadlines = []

    def verify(lease):                              # each dark session costs 3 s of the bound, then times out
        deadlines.append(lease._deadline)
        clock[0] += 3.0
        raise psycopg.errors._WaitTimeout("timeout expired")
    assert dbpool.take(KEY, verify) is None
    assert deadlines == [1000.0 + dbpool.POOL_IO_TIMEOUT_S] * 2, deadlines     # one deadline, two tries in 5 s
    tried = [s for s in sessions if s.finished]
    assert len(tried) == 2 and dbpool.stats()["closed_dead"] == 2
    assert dbpool.stats()["take_budget_spent"] == 1 and len(dbpool._idle) == 1  # the third is still idle


def test_the_reset_is_bounded_drains_queued_notifications_and_leaves_no_handle_behind(monkeypatch):
    """P2: notifications libpq already queued survive UNLISTEN and DISCARD ALL; the next lease must not read them."""
    ran, deadlines = [], []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a):
            ran.append(sql)
            deadlines.append(worker._deadline)

    class Worker:
        _released, _deadline, pgconn = False, None, None

        def cursor(self):
            return Cur()
    worker = Worker()
    monkeypatch.setattr(dbpool, "_lease", lambda pgconn, key: worker)
    clock = [70.0]
    monkeypatch.setattr(dbpool.time, "monotonic", lambda: clock[0])
    pg = FakePG()
    pg.queued = ["n1", "n2", "n3"]
    dbpool._reset(pg, KEY)
    assert ran[0] == "DISCARD ALL" and len(ran) == 3 and deadlines == [70.0 + dbpool.POOL_IO_TIMEOUT_S] * 3
    assert pg.queued == [] and pg.consumed == 2                           # drained, before the reset and after it
    assert worker._deadline is None and worker._released is True and isinstance(worker.pgconn, dbpool._Gone)


@pytest.mark.parametrize("status", [PipelineStatus.ON, PipelineStatus.ABORTED])
def test_a_session_left_in_pipeline_mode_is_closed_not_kept(monkeypatch, resets, status):
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    pg.pipeline_status = status
    dbpool.give_back(pg, KEY)
    assert pg.finished == 1 and resets == [] and dbpool.stats()["idle"] == 0 and dbpool.stats()["closed_not_idle"] == 1


def test_a_lease_refuses_pipeline_mode():
    with pytest.raises(psycopg.NotSupportedError):
        dbpool._lease(FakePG(), KEY).pipeline()


def test_a_fault_in_take_closes_the_session_it_had_popped(monkeypatch, resets):
    """P3: the popped backend was dropped open."""
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    dbpool.give_back(pg, KEY)
    monkeypatch.setattr(dbpool, "_lease", lambda pgconn, key: 1 / 0)
    assert dbpool.take(KEY, ok) is None
    assert pg.finished == 1 and dbpool.stats()["idle"] == 0 and dbpool.stats()["closed_fault"] == 1


def test_a_fault_at_the_very_end_of_take_still_closes_the_session(monkeypatch, resets):
    """Nothing between the pop and the return may leave a session open and unowned: not even the stats line."""
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    dbpool.give_back(pg, KEY)
    monkeypatch.setattr(dbpool, "_maybe_log", lambda: 1 / 0)
    assert dbpool.take(KEY, ok) is None
    assert pg.finished == 1 and dbpool.stats()["idle"] == 0 and dbpool.stats()["closed_fault"] == 1


def test_pool_false_is_a_plain_connection_even_with_the_pool_on(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    s = Seam(monkeypatch)
    dbpool.give_back(FakePG("idle"), (KEY[0], "-c statement_timeout=30000 -c TimeZone=UTC", rls.APP_ROLE))
    monkeypatch.setattr(dbpool, "take", lambda *a: 1 / 0)
    conn = dbconn.connect(pool=False)
    assert len(s.plain) == 1 and s.leased == [] and not isinstance(conn, dbpool.Lease)
    for truthy in (1, "yes", None):                                       # only the value True pools
        assert not isinstance(dbconn.connect(pool=truthy), dbpool.Lease)


def test_the_two_listen_connections_ask_for_a_session_of_their_own():
    """P2: both LISTEN loops opened the default, poolable shape. A source check: these two call sites say pool=False."""
    import pathlib
    apps = pathlib.Path(dbpool.__file__).resolve().parents[1]
    for rel in ("wsgw/mesh_notify.py", "wsgw/call_notify.py"):
        text = (apps / rel).read_text(encoding="utf-8")
        assert text.count("listen = dbconn.connect(self._target(), pool=False)") == 1, rel
        assert "listen = dbconn.connect(self._target())" not in text, rel


def test_the_middleware_discards_a_connection_that_fails_its_serving_guard(monkeypatch):
    """P3: it called close(), which returns a pooled session to the pool."""
    from apps.core import middleware

    class Pooled:
        closed_, discarded = 0, 0

        def close(self):
            self.closed_ += 1

        def discard(self):
            self.discarded += 1

    class Plain:
        closed_ = 0

        def close(self):
            self.closed_ += 1

    def refuse(conn):
        raise dbconn.RlsBypassError("not subject to RLS")
    monkeypatch.setattr(dbconn, "assert_serving_connection", refuse)
    for conn in (Pooled(), Plain()):
        db = middleware.RequestDatabase(lambda conn=conn: conn)
        with pytest.raises(dbconn.RlsBypassError):
            db.connection
        assert db.opened is False
        assert (conn.discarded, conn.closed_) == (1, 0) if isinstance(conn, Pooled) else conn.closed_ == 1
    monkeypatch.setattr(dbconn, "assert_serving_connection", lambda conn: None)
    good = Pooled()
    db = middleware.RequestDatabase(lambda: good)
    assert db.connection is good
    db.close()
    assert (good.closed_, good.discarded) == (1, 0)                       # a served connection is closed, so it pools


# ------------------------------------------------------------------------------------------------ round 3 (nxxxxx 366b69)

def _in_flight(lease):
    """A statement admitted on another thread: the driver holds the connection's own lock around it. Returns
    (release, thread): the lock is held until release.set()."""
    holding, release = threading.Event(), threading.Event()

    def statement():
        with lease.lock:
            holding.set()
            release.wait(30)
    t = threading.Thread(target=statement)
    t.start()
    assert holding.wait(10)
    return release, t


def test_close_waits_for_a_statement_in_flight_and_only_then_returns_the_session(monkeypatch):
    """P1: close() did not take the driver's operation lock, so a cursor that had passed the released check could send
    on the session after it was reset and handed to the next request. Deterministic: the statement's lock is held,
    close() is called from another thread, and nothing may be returned until the statement ends."""
    monkeypatch.setenv(ENV, "4")
    returned = []
    monkeypatch.setattr(dbpool, "give_back", lambda pgconn, key: returned.append(pgconn))
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 30.0)
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    release, stmt = _in_flight(lease)
    closer = threading.Thread(target=lease.close)
    closer.start()
    closer.join(0.4)
    assert closer.is_alive() and returned == [] and lease._released is False     # still the statement's
    assert lease.pgconn is pg                                                    # and its handle is still its own
    release.set()
    stmt.join(10)
    closer.join(10)
    assert not closer.is_alive() and returned == [pg] and lease._released is True and pg.finished == 0


def test_a_statement_that_outlasts_the_wait_parks_the_session_and_it_is_never_pooled_or_closed_under_it(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.1)
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    release, stmt = _in_flight(lease)
    lease.close()                                                        # gives up after 0.1 s
    assert lease._released is True and lease.closed is True              # no new statement is admitted
    # round 4 (memory-bxxxxx af9ea2, on a real server): the RUNNING statement keeps its own handle. Swapping it for
    # the stand-in at close() failed that statement inside the C driver.
    assert lease.pgconn is pg
    with pytest.raises(psycopg.OperationalError):
        lease.wait(x for x in ())                                             # a NEW statement on the closed lease
    assert dbpool.stats()["idle"] == 0 and resets == [] and dbpool.stats()["parked_busy"] == 1
    assert pg.finished == 0                                              # NOT finished under the running statement
    dbpool._reap()
    assert pg.finished == 0 and dbpool.stats()["closed_parked"] == 0     # its lock is still held
    assert lease.pgconn is pg                                            # and the handle is still the statement's
    assert dbpool.take(KEY, ok) is None                                  # and it is not on offer
    release.set()
    stmt.join(10)
    dbpool._reap()
    assert pg.finished == 1 and dbpool.stats()["closed_parked"] == 1 and dbpool._parked == []
    assert isinstance(lease.pgconn, dbpool._Gone)                        # taken only now, under the freed lock
    dbpool._reap()
    assert pg.finished == 1                                              # once


def test_discard_also_waits_or_parks_and_any_pool_call_reaps(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.1)
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    release, stmt = _in_flight(lease)
    lease.discard()
    assert pg.finished == 0 and dbpool.stats()["parked_busy"] == 1
    release.set()
    stmt.join(10)
    dbpool.give_back(FakePG("other"), KEY)                               # any later pool call finishes the parked one
    assert pg.finished == 1 and dbpool.stats()["closed_parked"] == 1


def test_a_checkout_also_finishes_a_parked_session(monkeypatch, resets):
    """A process that only takes (every return going to a full pool, or none yet) must still close parked sessions."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.1)
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    release, stmt = _in_flight(lease)
    lease.close()
    release.set()
    stmt.join(10)
    assert pg.finished == 0 and dbpool.stats()["parked_busy"] == 1
    assert dbpool.take(KEY, ok) is None                                  # nothing idle; the checkout reaps on its way
    assert pg.finished == 1 and dbpool.stats()["closed_parked"] == 1


def test_a_lease_refuses_cancel_and_cancel_safe():
    """P3-1: a cancel request taken before close() could cancel the next lease's statement."""
    lease = dbpool._lease(FakePG(), KEY)
    for call in (lease.cancel, lease.cancel_safe, lambda: lease.cancel_safe(timeout=1.0)):
        with pytest.raises(psycopg.NotSupportedError):
            call()


def test_a_fault_in_takes_own_reporting_cannot_skip_the_cleanup(monkeypatch, resets):
    """P3-2: the handler counted and logged BEFORE it finished the popped session."""
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()
    dbpool.give_back(pg, KEY)
    monkeypatch.setattr(dbpool, "_count", lambda name: 1 / 0)            # both the 'reused' count and the fault count
    monkeypatch.setattr(dbpool.log, "exception", lambda *a, **k: 1 / 0)
    assert dbpool.take(KEY, ok) is None
    assert pg.finished == 1 and len(dbpool._idle) == 0


def test_the_notification_drain_is_bounded(monkeypatch):
    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a):
            return None

    class Worker:
        _released, _deadline, pgconn = False, None, None

        def cursor(self):
            return Cur()
    monkeypatch.setattr(dbpool, "_lease", lambda pgconn, key: Worker())
    monkeypatch.setattr(dbpool, "MAX_NOTIFY_DRAIN", 3)
    pg = FakePG()
    pg.queued = ["n"] * 3
    dbpool._reset(pg, KEY)                                               # exactly the bound drains
    assert pg.queued == []
    pg.queued = ["n"] * 4
    with pytest.raises(psycopg.OperationalError, match="queued notifications"):
        dbpool._reset(pg, KEY)


def test_the_serving_guard_discards_a_pooled_lease_it_refuses_at_every_call_site(monkeypatch, resets):
    """P3-4: only the middleware discarded; five other call sites close() after a refused guard, which returned the
    session to the pool. The guard itself now discards, so their close() is a no-op on a dead lease."""
    monkeypatch.setenv(ENV, "4")

    def refuse(conn, table="shared_memory"):
        raise RuntimeError("not subject to RLS")
    monkeypatch.setattr(rls, "assert_rls_active", refuse)
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    with pytest.raises(dbconn.RlsBypassError):
        dbconn.assert_serving_connection(lease)
    assert pg.finished == 1 and lease.closed is True
    lease.close()                                                        # what every call site then does
    assert resets == [] and dbpool.stats()["idle"] == 0 and pg.finished == 1

    class Plain:                                                         # a plain connection: untouched by the guard
        autocommit, closed_ = True, 0

        def close(self):
            self.closed_ += 1
    plain = Plain()
    with pytest.raises(dbconn.RlsBypassError):
        dbconn.assert_serving_connection(plain)
    assert plain.closed_ == 0
    not_autocommit = dbpool._lease(FakePG("na"), KEY)
    not_autocommit._autocommit = False
    live = not_autocommit.pgconn
    with pytest.raises(dbconn.NonAutocommitError):
        dbconn.assert_serving_connection(not_autocommit)
    assert live.finished == 1
    monkeypatch.setattr(rls, "assert_rls_active", lambda conn, table="shared_memory": None)
    good = dbpool._lease(FakePG("good"), KEY)
    dbconn.assert_serving_connection(good)                               # a lease that passes is left alone
    assert good.closed is False


def test_every_guard_then_close_site_goes_through_the_guard_that_discards():
    """A census of the call sites, so a new one that probes RLS some other way is seen."""
    import pathlib
    apps = pathlib.Path(dbpool.__file__).resolve().parents[1]
    sites = sorted(p.relative_to(apps).as_posix() for p in apps.rglob("*.py")
                   if "tests" not in p.parts and "assert_serving_connection(" in p.read_text(encoding="utf-8")
                   and p.name != "dbconn.py")
    assert sites == ["core/middleware.py", "memoryskills/db.py", "mesh/wake.py", "meshguard/db.py", "wsgw/call_notify.py",
                     "wsgw/mesh_notify.py"], sites


def test_the_doorbell_senders_listen_connection_is_its_own_session(monkeypatch):
    """P3-3: run_call_worker hands dbconn.connect to the doorbell sender, whose LISTEN connection was poolable."""
    from apps.calls import webhooks
    seen = []
    monkeypatch.setattr(dbconn, "connect", lambda *a, **kw: seen.append(kw) or "conn")
    assert webhooks._listen_connection(dbconn.connect) == "conn" and seen == [{"pool": False}]
    assert webhooks._listen_connection(lambda: "stand-in") == "stand-in"  # a test's connect takes no argument


def test_batch_execution_is_not_the_refused_pipeline_entry():
    """P3-5: executemany uses the driver's internal pipeline (_pipeline_nolock), not pipeline(); a lease must keep it."""
    import inspect
    src = inspect.getsource(psycopg.Cursor.executemany)
    assert "_pipeline_nolock" in src and "self._conn.pipeline()" not in src
    assert "_pipeline_nolock" not in dbpool.Lease.__dict__ and "pipeline" in dbpool.Lease.__dict__


# ------------------------------------------------------------------------------------------------ round 5 (nxxxxx 85fb2d)

def _until(cond, secs=10.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture()
def no_reaper(monkeypatch):
    """The steps of these tests are taken by hand: no reaper thread (one left by an earlier test is waited out)."""
    assert _until(lambda: dbpool._reaper[0] is None, 15)
    monkeypatch.setattr(dbpool, "_ensure_reaper", lambda: None)


class SocketPG(FakePG):
    """A stand-in session with a REAL socket, so that the shutdown can be seen from its other end, and a cancel
    handle of either kind that records what is asked of it (round 7: made ONCE, when the lease is armed)."""

    asked_in_order: list = []                            # the names, as their cancel requests were sent

    def __init__(self, name="pg"):
        super().__init__(name)
        self.ours, self.peer = socket.socketpair()
        self.socket = self.ours.fileno()
        self.cancelled = threading.Event()
        self.made, self.cancels, self.freed, self.started = 0, 0, 0, 0
        self.cancel_fault = None                         # an exception the request raises instead of being sent
        self.cancel_gate = None                          # an Event the request waits for (a network that hangs)
        self.inside = threading.Event()                  # set when the request has begun
        self.peer.settimeout(5)

    def _asked(self):
        self.inside.set()
        if self.cancel_gate is not None:
            self.cancel_gate.wait(20)
        self.cancels += 1
        SocketPG.asked_in_order.append(self.name)
        if self.cancel_fault is not None:
            raise self.cancel_fault
        self.cancelled.set()

    def get_cancel(self):
        outer = self
        outer.made += 1

        class Request:
            def cancel(self):
                outer._asked()

            def free(self):
                outer.freed += 1
        return Request()

    def cancel_conn(self):
        outer = self
        outer.made += 1

        class Conn:
            pg = outer

            def start(self):
                outer.started += 1

            def finish(self):
                outer.freed += 1
        return Conn()

    def peer_sees_the_end(self) -> bool:
        self.peer.settimeout(2)
        try:
            return self.peer.recv(1) == b""
        except TimeoutError:                             # nothing came: the socket was NOT shut down
            return False
        except OSError:                                  # a reset is an end too
            return True
        finally:
            self.peer.settimeout(5)

    def peer_sees_nothing(self) -> bool:
        self.peer.settimeout(0.05)
        try:
            self.peer.recv(1)
        except TimeoutError:
            return True
        except OSError:
            return False
        finally:
            self.peer.settimeout(5)
        return False

    def shut(self):
        self.ours.close()
        self.peer.close()


def _parked_lease(monkeypatch, pg):
    """A lease closed under a statement in flight. Returns (lease, release, statement thread)."""
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.05)
    lease = dbpool._lease(pg, KEY)
    lease._arm()                                         # as `take` and `Lease.connect` do; a FakePG has no socket
    release, stmt = _in_flight(lease)
    lease.close()
    return lease, release, stmt


def test_the_reaper_thread_finishes_a_parked_session_with_no_pool_call_and_then_ends(monkeypatch, resets):
    """P2: `_reap` ran only on a later pool call, so an idle process kept its parked sessions for good."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "REAP_EVERY_S", 0.02)
    for name in ("first", "second"):                                         # the second park starts a thread again
        pg = FakePG(name)
        lease, release, stmt = _parked_lease(monkeypatch, pg)
        assert dbpool.stats()["parked"] == 1 and dbpool._reaper[0] is not None and dbpool._reaper[0].is_alive()
        time.sleep(0.2)
        assert pg.finished == 0 and lease.pgconn is pg                       # never under the running statement
        release.set()
        stmt.join(10)
        assert _until(lambda: pg.finished == 1), "no pool call was made; the thread did not finish it"
        assert isinstance(lease.pgconn, dbpool._Gone) and dbpool.stats()["parked"] == 0
        assert _until(lambda: dbpool._reaper[0] is None), "the thread did not end with nothing parked"
    assert dbpool.stats()["closed_parked"] == 2 and dbpool.stats()["aborted_parked"] == 0


def test_a_statement_that_never_lets_go_is_ended_after_the_bound_and_then_forgotten(monkeypatch, resets, no_reaper, caplog):
    """P2: there was no expiry. After PARK_MAX_S the socket is shut down (through the lease's own duplicate, round
    6) and the server is asked to cancel (through the handle taken at arming, round 7), without the handle being
    finished under the statement; PARK_FORGET_S later the lease is no longer tracked."""
    monkeypatch.setenv(ENV, "4")
    pg = SocketPG()
    lease, release, stmt = _parked_lease(monkeypatch, pg)
    try:
        since = dbpool._parked[0].since
        dbpool._reap(since + dbpool.PARK_MAX_S - 0.5)
        assert dbpool.stats()["aborted_parked"] == 0 and pg.peer_sees_nothing() and not pg.cancelled.is_set()
        with caplog.at_level(logging.WARNING, logger=dbpool.log.name):
            dbpool._reap(since + dbpool.PARK_MAX_S)
        assert dbpool.stats()["aborted_parked"] == 1 and pg.peer_sees_the_end()      # the socket, seen from the far end
        assert pg.cancelled.wait(5)                                                 # round 7: and the server is asked
        assert "its connection was shut down" in caplog.text and "more than" not in caplog.text    # by age, not the cap
        assert pg.finished == 0 and lease.pgconn is pg                               # NOT finished under the statement
        assert dbpool.stats()["parked"] == 1 and dbpool.stats()["closed_parked"] == 0
        dbpool._reap(since + dbpool.PARK_MAX_S + 1)
        assert dbpool.stats()["aborted_parked"] == 1                                 # once
        dbpool._reap(since + dbpool.PARK_MAX_S + dbpool.PARK_FORGET_S - 0.5)
        assert dbpool.stats()["parked"] == 1 and dbpool.stats()["forgotten_parked"] == 0
        dbpool._reap(since + dbpool.PARK_MAX_S + dbpool.PARK_FORGET_S)
        assert dbpool.stats()["parked"] == 0 and dbpool.stats()["forgotten_parked"] == 1
        assert pg.finished == 0 and lease.pgconn is pg and lease.closed is True      # still never touched under it
        assert dbpool.take(KEY, ok) is None
    finally:
        release.set()
        stmt.join(10)
        pg.shut()


def test_a_session_ended_at_the_bound_is_finished_as_usual_once_its_statement_lets_go(monkeypatch, resets, no_reaper):
    monkeypatch.setenv(ENV, "4")
    pg = SocketPG()
    lease, release, stmt = _parked_lease(monkeypatch, pg)
    try:
        dbpool._reap(dbpool._parked[0].since + dbpool.PARK_MAX_S)
        assert dbpool.stats()["aborted_parked"] == 1 and pg.finished == 0
        release.set()                                                                # the statement fails and lets go
        stmt.join(10)
        dbpool._reap()
        assert pg.finished == 1 and isinstance(lease.pgconn, dbpool._Gone)
        assert dbpool.stats()["closed_parked"] == 1 and dbpool.stats()["parked"] == 0
        assert dbpool.stats()["forgotten_parked"] == 0
    finally:
        release.set()
        pg.shut()


def test_more_parked_sessions_than_the_cap_ends_the_oldest_at_once(monkeypatch, resets, no_reaper):
    """P2: there was no capacity bound."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 2)
    held = []
    try:
        for name in ("oldest", "second", "third"):
            pg = SocketPG(name)
            held.append((pg,) + _parked_lease(monkeypatch, pg))
        dbpool._reap()                                                               # all three far younger than the bound
        assert dbpool.stats()["aborted_parked"] == 1 and dbpool.stats()["parked"] == 3
        assert held[0][0].peer_sees_the_end() and held[1][0].peer_sees_nothing() and held[2][0].peer_sees_nothing()
        assert [pg.finished for pg, *_ in held] == [0, 0, 0]
        dbpool._reap()
        assert dbpool.stats()["aborted_parked"] == 1                                 # the one over the cap, once
    finally:
        for pg, _lease, release, stmt in held:
            release.set()
            stmt.join(10)
            pg.shut()
    dbpool._reap()
    assert dbpool.stats()["closed_parked"] == 3 and dbpool.stats()["parked"] == 0


def test_putting_unfinished_leases_back_makes_sure_a_reaper_runs(monkeypatch, resets):
    """The Codex review of 5560d93b: a request thread inside `_reap` holds the list outside the lock; the reaper
    thread, looking just then, sees nothing parked and ends; the leases put back had no thread to end them."""
    monkeypatch.setenv(ENV, "4")
    assert _until(lambda: dbpool._reaper[0] is None, 15)
    calls = []
    monkeypatch.setattr(dbpool, "_ensure_reaper", lambda: calls.append(1))
    pg = FakePG()
    lease, release, stmt = _parked_lease(monkeypatch, pg)                # parks: one call
    assert calls == [1] and dbpool.stats()["parked"] == 1
    dbpool._reap()                                                       # still busy: put back, and a reaper asked for
    assert calls == [1, 1] and dbpool.stats()["parked"] == 1
    release.set()
    stmt.join(10)
    dbpool._reap()                                                       # finished: nothing put back, nothing asked
    assert calls == [1, 1] and dbpool.stats()["parked"] == 0 and pg.finished == 1
    dbpool._reap()
    assert calls == [1, 1]


def test_a_reaper_that_ended_while_a_request_held_the_list_is_started_again(monkeypatch, resets):
    """The interleaving itself, with real threads: the reaper has ended, and a `_reap` call that puts a busy lease
    back starts one, which then finishes the session with no further pool call."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "REAP_EVERY_S", 0.02)
    assert _until(lambda: dbpool._reaper[0] is None, 15)
    real = dbpool._ensure_reaper
    monkeypatch.setattr(dbpool, "_ensure_reaper", lambda: None)          # the park starts no thread: as if it had ended
    pg = FakePG()
    lease, release, stmt = _parked_lease(monkeypatch, pg)
    assert dbpool._reaper[0] is None and dbpool.stats()["parked"] == 1
    monkeypatch.setattr(dbpool, "_ensure_reaper", real)
    dbpool._reap()                                                       # the request thread's pass: busy, put back
    assert dbpool._reaper[0] is not None and dbpool._reaper[0].is_alive()
    release.set()
    stmt.join(10)
    assert _until(lambda: pg.finished == 1), "the restarted reaper did not finish it"
    assert _until(lambda: dbpool._reaper[0] is None)


class _FaultyLock:
    """A lock whose acquire raises: the fault inside `_reap` that used to lose the leases behind it."""

    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def acquire(self, blocking=True, timeout=-1):
        self.calls += 1
        raise self.exc


def _two_parked(monkeypatch):
    a, b = FakePG("a"), FakePG("b")
    la, ra, sa = _parked_lease(monkeypatch, a)
    lb, rb, sb = _parked_lease(monkeypatch, b)
    for release, stmt in ((ra, sa), (rb, sb)):
        release.set()
        stmt.join(10)
    return a, la, b, lb


def test_a_fault_on_one_parked_lease_loses_neither_it_nor_the_one_behind_it(monkeypatch, resets, no_reaper):
    """P3-1, the two-lease control: one exception dropped every parked lease `_reap` had not reached yet."""
    monkeypatch.setenv(ENV, "4")
    a, la, b, lb = _two_parked(monkeypatch)
    real = la.lock
    la.lock = _FaultyLock(RuntimeError("a fault while reaping the first"))
    dbpool._reap()                                                                   # raises nothing
    assert la.lock.calls == 1 and a.finished == 0
    assert b.finished == 1 and dbpool.stats()["closed_parked"] == 1                  # the one behind it was still done
    assert [e.lease for e in dbpool._parked] == [la]                                 # and the faulty one is kept
    la.lock = real
    dbpool._reap()
    assert a.finished == 1 and dbpool._parked == [] and dbpool.stats()["closed_parked"] == 2


def test_even_a_fault_that_is_not_an_exception_puts_every_unfinished_lease_back(monkeypatch, resets, no_reaper):
    monkeypatch.setenv(ENV, "4")
    a, la, b, lb = _two_parked(monkeypatch)
    c, lc, d, ld = _two_parked(monkeypatch)              # four in all: the three behind the fault keep their order
    real = la.lock
    la.lock = _FaultyLock(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        dbpool._reap()
    assert [e.lease for e in dbpool._parked] == [la, lb, lc, ld]                     # oldest first, as parked
    assert (a.finished, b.finished, c.finished, d.finished) == (0, 0, 0, 0)
    la.lock = real
    dbpool._reap()
    assert (a.finished, b.finished, c.finished, d.finished) == (1, 1, 1, 1) and dbpool._parked == []


def test_a_lease_that_faults_every_time_is_still_bounded(monkeypatch, resets, no_reaper):
    monkeypatch.setenv(ENV, "4")
    a, la, b, lb = _two_parked(monkeypatch)
    real = la.lock

    class Busy:                                         # never free, as a stream left open holds it
        def acquire(self, blocking=True, timeout=-1):
            return False
    la.lock = Busy()
    since = dbpool._parked[0].since
    dbpool._reap(since + dbpool.PARK_MAX_S)
    dbpool._reap(since + dbpool.PARK_MAX_S + dbpool.PARK_FORGET_S)
    assert dbpool._parked == [] and dbpool.stats()["aborted_parked"] == 1 and dbpool.stats()["forgotten_parked"] == 1
    assert a.finished == 0 and b.finished == 1
    la.lock = real


def test_the_request_database_discards_a_real_lease_that_the_real_guard_refuses(monkeypatch, resets):
    """P3-2: the middleware test used a stand-in with a `discard` method. This one is RequestDatabase, the real
    `assert_serving_connection` and a real Lease: only the guard's SQL is replaced by its refusal."""
    from apps.core import middleware
    monkeypatch.setenv(ENV, "4")

    def refuse(conn, table):
        raise dbconn.RlsBypassError("not subject to RLS")
    monkeypatch.setattr(dbconn, "_serving_checks", refuse)
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    db = middleware.RequestDatabase(lambda: lease)
    with pytest.raises(dbconn.RlsBypassError):
        db.connection
    assert db.opened is False
    assert pg.finished == 1 and lease._released is True and lease.closed is True     # closed for real, once
    assert isinstance(lease.pgconn, dbpool._Gone)
    assert resets == [] and dbpool.stats()["idle"] == 0 and dbpool.stats()["returned"] == 0
    assert dbpool.take(KEY, ok) is None                                              # nothing for the next request
    db.close()                                                                       # the request's own end: no second close
    assert pg.finished == 1
    # the same path with a lease the guard accepts: the request's close() pools it
    monkeypatch.setattr(dbconn, "_serving_checks", lambda conn, table: None)
    pg2 = FakePG("accepted")
    lease2 = dbpool._lease(pg2, KEY)
    db = middleware.RequestDatabase(lambda: lease2)
    assert db.connection is lease2 and db.opened is True
    db.close()
    assert pg2.finished == 0 and resets == [(pg2, KEY)] and dbpool.stats()["idle"] == 1


# ------------------------------------------------------------------------------------------------ round 6 (a reviewer 0c867a)

class Tripwire:
    """A parked lease's handle, as the reaper must leave it: any attribute read is recorded and fails."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError("the reaper read %r from a handle its statement is using" % name)


def test_ending_a_parked_session_never_reads_its_handle_and_cannot_hit_a_recycled_descriptor(monkeypatch, resets, no_reaper):
    """P1-1. `_abort` read pgconn.socket from the reaper thread; the statement's thread could close that descriptor
    in the same instant, the system gave the number to the next socket, and SHUT_RDWR hit a stranger. Here the
    number the handle reports after the park IS a stranger's live socket, and every other read of the handle fails."""
    monkeypatch.setenv(ENV, "4")
    pg = SocketPG()
    stranger, stranger_peer = socket.socketpair()
    stranger_peer.settimeout(0.3)
    lease, release, stmt = _parked_lease(monkeypatch, pg)
    try:
        pg.socket = stranger.fileno()                    # what a recycled descriptor number looks like
        wire = Tripwire()
        lease.pgconn = wire                              # the lock is held: the reaper may not look at it at all
        since = dbpool._parked[0].since
        dbpool._reap(since + dbpool.PARK_MAX_S)
        assert dbpool.stats()["aborted_parked"] == 1 and wire.touched == []
        assert pg.peer_sees_the_end()                    # the session's own socket, through the lease's duplicate
        with pytest.raises(TimeoutError):                # and the stranger's is untouched: nothing arrives, no end
            stranger_peer.recv(1)
        stranger.sendall(b"x")
        assert stranger_peer.recv(1) == b"x"
        assert pg.cancelled.wait(5) and pg.made == 1 and wire.touched == []   # the cancel too: from the armed handle
        dbpool._reap(since + dbpool.PARK_MAX_S + dbpool.PARK_FORGET_S)
        assert dbpool.stats()["forgotten_parked"] == 1 and wire.touched == [] and lease._abort_sock is None
    finally:
        lease.pgconn = pg
        release.set()
        stmt.join(10)
        pg.shut()
        stranger.close()
        stranger_peer.close()


@pytest.fixture()
def dups(monkeypatch):
    """Every duplicate the pool makes of a session's socket, so a test can count the open ones."""
    made = []
    real = socket.fromfd

    def fromfd(fd, family, kind, *a):
        made.append(real(fd, family, kind, *a))
        return made[-1]
    monkeypatch.setattr(dbpool.socket, "fromfd", fromfd)
    return made


def _open(made):
    return [s for s in made if s.fileno() != -1]


def test_the_duplicate_is_made_once_per_caller_lease_and_closed_on_every_way_out(monkeypatch, dups, no_reaper):
    monkeypatch.setenv(ENV, "4")
    held = []

    def arm(name="pg"):
        pg = SocketPG(name)
        held.append(pg)
        lease = dbpool._lease(pg, KEY)
        assert lease._abort_sock is None                 # `_lease` alone makes none (the reset's worker is one)
        lease._arm()
        return pg, lease
    try:
        # a quiet close: returned to the pool, and an idle session holds no duplicate
        monkeypatch.setattr(dbpool, "_reset", lambda pgconn, key: None)
        pg, lease = arm()
        assert len(dups) == 1 and len(_open(dups)) == 1 and lease._abort_sock is dups[0]
        lease.close()
        assert dbpool.stats()["idle"] == 1 and _open(dups) == [] and lease._abort_sock is None and len(dups) == 1
        # `take` arms the lease it hands out, after the proof, once; a failed proof makes none
        def dead(lease):
            raise psycopg.OperationalError("dead")
        assert dbpool.take(KEY, dead) is None and dbpool.stats()["closed_dead"] == 1
        assert len(dups) == 1
        dbpool.give_back(pg2 := SocketPG("idle"), KEY)
        held.append(pg2)
        again = dbpool.take(KEY, ok)
        assert again is not None and len(dups) == 2 and again._abort_sock is dups[1] and len(_open(dups)) == 1
        again.discard()                                  # discard
        assert _open(dups) == [] and pg2.finished == 1
        # a close after the driver's own cancel: finished, never pooled
        pg, lease = arm()
        lease._no_pool = True
        lease.close()
        assert _open(dups) == [] and pg.finished == 1 and dbpool.stats()["closed_cancelled"] == 1
        # parked, then finished by the reaper
        pg, lease = arm()
        monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.05)
        release, stmt = _in_flight(lease)
        lease.close()
        assert len(_open(dups)) == 1                     # still armed while it is parked
        release.set()
        stmt.join(10)
        dbpool._reap()
        assert _open(dups) == [] and pg.finished == 1
        # parked, ended at the bound (the duplicate stays ours), then forgotten
        pg, lease = arm()
        release, stmt = _in_flight(lease)
        lease.close()
        since = dbpool._parked[0].since
        dbpool._reap(since + dbpool.PARK_MAX_S)
        assert len(_open(dups)) == 1 and pg.peer_sees_the_end()
        dbpool._reap(since + dbpool.PARK_MAX_S + dbpool.PARK_FORGET_S)
        assert _open(dups) == [] and dbpool._parked == []
        release.set()
        stmt.join(10)
        assert len(dups) == 5                            # four made here and the one `take` made: one per caller lease
    finally:
        for pg in held:
            pg.shut()


def test_the_reset_makes_no_duplicate_and_a_new_connection_makes_one(monkeypatch, dups):
    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a):
            return None
    monkeypatch.setattr(dbpool.Lease, "cursor", lambda self, *a, **k: Cur())
    monkeypatch.setattr(rls, "set_role", lambda conn, role=rls.APP_ROLE: None)
    pg = SocketPG()
    try:
        dbpool._reset(pg, KEY)                           # the real `_lease`, the real worker
        assert dups == []
        monkeypatch.setattr(psycopg.Connection, "connect", classmethod(lambda cls, *a, **k: cls(pg)))
        lease = dbpool.Lease.connect("postgresql://x")
        assert len(dups) == 1 and lease._abort_sock is dups[0] and isinstance(lease.lock, dbpool._OwnedLock)
        dbpool._disarm(lease)
        dbpool._disarm(lease)                            # twice is once
        assert _open(dups) == []
    finally:
        pg.shut()


def test_a_lease_whose_socket_cannot_be_duplicated_is_not_ended_but_still_forgotten(monkeypatch, resets, no_reaper, caplog):
    monkeypatch.setenv(ENV, "4")
    pg = FakePG()                                        # no socket at all
    lease, release, stmt = _parked_lease(monkeypatch, pg)
    try:
        assert lease._abort_sock is None and dbpool._abort(lease) is False
        since = dbpool._parked[0].since
        with caplog.at_level(logging.WARNING, logger=dbpool.log.name):
            dbpool._reap(since + dbpool.PARK_MAX_S)
        assert dbpool.stats()["aborted_parked"] == 1 and "no duplicate socket" in caplog.text
        dbpool._reap(since + dbpool.PARK_MAX_S + dbpool.PARK_FORGET_S)
        assert dbpool._parked == [] and dbpool.stats()["forgotten_parked"] == 1 and pg.finished == 0
    finally:
        release.set()
        stmt.join(10)


def test_the_drivers_own_cancel_goes_through_and_the_lease_is_then_never_pooled(monkeypatch, resets, caplog):
    """P2-1a. psycopg cancels through `_try_cancel` (an early end of stream(), an interrupt in a wait, a failed
    copy). Refused, an abandoned stream was read to its end. It reaches the driver now, and the session is finished
    at close(), so the request cannot reach a next lease."""
    monkeypatch.setenv(ENV, "4")
    sent = []
    monkeypatch.setattr(psycopg.Connection, "cancel_safe", lambda self, *, timeout=30.0: sent.append((self, timeout)))
    pg = FakePG()
    lease = dbpool._lease(pg, KEY)
    lease._try_cancel(timeout=1.5)
    assert sent == [(lease, 1.5)] and lease._no_pool is True and lease._driver_cancel is False
    for call in (lease.cancel, lease.cancel_safe):       # anyone else's cancel is still refused, before and after
        with pytest.raises(psycopg.NotSupportedError):
            call()
    lease.close()
    assert pg.finished == 1 and dbpool.stats()["idle"] == 0 and resets == []
    assert dbpool.stats()["closed_cancelled"] == 1 and dbpool.stats()["returned"] == 0
    lease._try_cancel()                                  # released: nothing is sent
    assert len(sent) == 1
    # a lease nothing was cancelled on is pooled as before
    other = dbpool._lease(FakePG("other"), KEY)
    other.close()
    assert dbpool.stats()["idle"] == 1 and dbpool.stats()["closed_cancelled"] == 1


def test_the_drivers_cancel_on_an_old_libpq_reaches_the_plain_cancel_and_a_failure_is_only_said(monkeypatch, caplog):
    plain = []
    monkeypatch.setattr(psycopg.Connection, "cancel", lambda self: plain.append(self), raising=False)
    monkeypatch.setattr(psycopg.Connection, "cancel_safe", lambda self, *, timeout=30.0: self.cancel())   # libpq < 17
    lease = dbpool._lease(FakePG(), KEY)
    lease._try_cancel()
    assert plain == [lease] and lease._driver_cancel is False
    with pytest.raises(psycopg.NotSupportedError):
        lease.cancel()
    assert plain == [lease]

    def boom(self, *, timeout=30.0):
        raise psycopg.OperationalError("no route")
    monkeypatch.setattr(psycopg.Connection, "cancel_safe", boom)
    failing = dbpool._lease(FakePG(), KEY)
    with caplog.at_level(logging.WARNING, logger=dbpool.log.name):
        failing._try_cancel()                            # raises nothing
    assert "driver's cancel of a statement failed (OperationalError)" in caplog.text
    assert failing._no_pool is True and failing._driver_cancel is False


def test_a_close_from_the_thread_that_holds_the_lock_parks_at_once(monkeypatch, resets, no_reaper):
    """P2-1b. A stream holds the connection's lock while its generator lives, and that lock is not re-entrant: the
    owner's close() waited CLOSE_WAIT_S for a lock only the owner could free."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 5.0)
    for closing in ("close", "discard"):
        pg = FakePG(closing)
        lease = dbpool._lease(pg, KEY)
        assert lease.lock.acquire() and lease.lock.held_by_me()          # this thread's own paused stream
        before, t0 = dbpool.stats()["parked_busy"], time.monotonic()
        getattr(lease, closing)()
        assert time.monotonic() - t0 < 1.0, "it waited for a lock only it could free"
        assert dbpool.stats()["parked_busy"] == before + 1 and lease._released is True
        assert pg.finished == 0 and lease.pgconn is pg and resets == []  # parked: nothing is finished under the stream
        dbpool._reap()
        assert pg.finished == 0
        lease.lock.release()                                              # the generator is closed
        dbpool._reap()
        assert pg.finished == 1 and isinstance(lease.pgconn, dbpool._Gone)
    assert dbpool._parked == []


def test_the_lock_knows_its_holder_and_is_still_a_lock():
    lock = dbpool._OwnedLock()
    assert lock.locked() is False and lock.held_by_me() is False
    with lock as got:
        assert got is True and lock.locked() and lock.held_by_me()
        seen = []
        t = threading.Thread(target=lambda: seen.extend([lock.held_by_me(), lock.acquire(blocking=False),
                                                         lock.acquire(timeout=0.05)]))
        t.start()
        t.join(10)
        assert seen == [False, False, False] and lock.held_by_me()       # another thread: not its, and not takeable
    assert lock.locked() is False and lock.held_by_me() is False
    took = []
    t = threading.Thread(target=lambda: took.append(lock.acquire()))
    t.start()
    t.join(10)
    assert took == [True] and lock.locked() and lock.held_by_me() is False    # held by a thread that is not this one
    lock.release()                                                            # as a generator closed elsewhere does
    assert lock.locked() is False


def test_a_young_statement_ahead_of_finished_ones_is_not_ended_for_them(monkeypatch, resets, no_reaper):
    """P2-2 (1). The cap was counted over everything parked, finished statements included: one statement 50 ms old
    ahead of MAX_PARKED sessions that were already over was ended."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 2)
    pg = SocketPG("young and busy")
    lease, release, stmt = _parked_lease(monkeypatch, pg)
    try:
        a, la, b, lb = _two_parked(monkeypatch)                          # behind it, both already over
        assert [e.lease for e in dbpool._parked] == [lease, la, lb]
        dbpool._reap()
        assert dbpool.stats()["aborted_parked"] == 0 and pg.peer_sees_nothing()
        assert (a.finished, b.finished) == (1, 1) and [e.lease for e in dbpool._parked] == [lease]
    finally:
        release.set()
        stmt.join(10)
        pg.shut()
    dbpool._reap()
    assert dbpool._parked == [] and dbpool.stats()["closed_parked"] == 3


def test_sessions_already_ended_do_not_count_against_the_cap_again(monkeypatch, resets, no_reaper, caplog):
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 1)
    held = []
    try:
        for name in ("oldest", "second", "third"):
            pg = SocketPG(name)
            held.append((pg,) + _parked_lease(monkeypatch, pg))
        with caplog.at_level(logging.WARNING, logger=dbpool.log.name):
            dbpool._reap()                                               # three running, cap 1: the two oldest end
        assert dbpool.stats()["aborted_parked"] == 2
        assert caplog.text.count("(more than 1 parked); its connection was shut down") == 2    # and the reason is said
        assert held[0][0].peer_sees_the_end() and held[1][0].peer_sees_the_end() and held[2][0].peer_sees_nothing()
        dbpool._reap()                                                   # one still running: within the cap
        assert dbpool.stats()["aborted_parked"] == 2 and held[2][0].peer_sees_nothing()
    finally:
        for pg, _lease, release, stmt in held:
            release.set()
            stmt.join(10)
            pg.shut()
    dbpool._reap()
    assert dbpool._parked == []


class _SlowLock:
    """A parked lease's lock that keeps a `_reap` pass inside it until the test lets go, then says "still busy"."""

    def __init__(self):
        self.inside, self.go = threading.Event(), threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        self.inside.set()
        assert self.go.wait(20)
        return False


def test_one_pass_at_a_time_and_the_parked_stay_counted_while_it_runs(monkeypatch, resets, no_reaper):
    """P2-2 (2). Each `_reap` emptied the list into a local one: `parked` read 0 during a pass, a reaper looking
    then saw nothing and ended, and two passes each saw a part and neither the whole."""
    monkeypatch.setenv(ENV, "4")
    a, la, b, lb = _two_parked(monkeypatch)                              # both over; a's lock will hold the pass
    real = la.lock
    la.lock = slow = _SlowLock()
    first = threading.Thread(target=dbpool._reap)
    first.start()
    try:
        assert slow.inside.wait(10)
        assert dbpool.stats()["parked"] == 2 and len(dbpool._parked) == 2          # true during the pass
        c = FakePG("c")
        lc, rc, sc = _parked_lease(monkeypatch, c)                                 # parked DURING the pass
        rc.set()
        sc.join(10)
        t0 = time.monotonic()
        dbpool._reap()                                                             # a second pass: returns at once
        assert time.monotonic() - t0 < 1.0 and (b.finished, c.finished) == (0, 0)  # and does none of the work
        assert dbpool.stats()["parked"] == 3
    finally:
        slow.go.set()
        first.join(10)
    assert not first.is_alive() and b.finished == 1                                # the first pass did its own
    assert [e.lease for e in dbpool._parked] == [la, lc]                           # a kept, c not lost, order kept
    la.lock = real
    dbpool._reap()
    assert (a.finished, b.finished, c.finished) == (1, 1, 1) and dbpool._parked == []
    assert dbpool._reap_lock.locked() is False


def test_a_pass_that_dies_frees_the_way_for_the_next(monkeypatch, resets, no_reaper):
    monkeypatch.setenv(ENV, "4")
    a, la, b, lb = _two_parked(monkeypatch)
    real = la.lock
    la.lock = _FaultyLock(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        dbpool._reap()
    assert dbpool._reap_lock.locked() is False
    la.lock = real
    dbpool._reap()
    assert (a.finished, b.finished) == (1, 1)


def test_a_flood_of_notifications_is_refused_before_the_reset_runs_a_statement(monkeypatch):
    """P3-1. The drain came after DISCARD ALL, and the driver reads every queued notification into memory while
    it runs a statement, without a bound."""
    ran = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a):
            ran.append(sql)

    class Worker:
        _released, _deadline, pgconn = False, None, None

        def cursor(self):
            return Cur()
    monkeypatch.setattr(dbpool, "_lease", lambda pgconn, key: Worker())
    monkeypatch.setattr(rls, "set_role", lambda conn, role=rls.APP_ROLE: ran.append("SET ROLE"))
    monkeypatch.setattr(dbpool, "MAX_NOTIFY_DRAIN", 3)
    pg = FakePG()
    pg.queued = ["n"] * 4
    with pytest.raises(psycopg.OperationalError, match="queued notifications"):
        dbpool._reset(pg, KEY)
    assert ran == [] and pg.consumed == 1                # nothing was run on it
    pg.queued = ["n"] * 3
    dbpool._reset(pg, KEY)
    assert ran == ["DISCARD ALL", "SET ROLE"] and pg.queued == [] and pg.consumed == 3

    class Late(FakePG):                                  # notifications that arrive DURING the reset
        def consume_input(self):
            super().consume_input()
            if self.consumed == 2:
                self.queued = ["n"] * 4
    with pytest.raises(psycopg.OperationalError, match="queued notifications"):
        dbpool._reset(Late(), KEY)


def test_two_callers_at_once_start_one_reaper(monkeypatch):
    """P3-2. start() ran outside the lock, and a thread is not alive before start() returns: a second caller in
    between started a second reaper."""
    assert _until(lambda: dbpool._reaper[0] is None, 15)
    RealThread = threading.Thread
    started, first_in = [], threading.Event()

    class SlowStart:
        def __init__(self, **kw):
            self.alive = False

        def start(self):
            started.append(self)
            first_in.set()
            time.sleep(0.3)                              # the window: created, not yet alive
            self.alive = True

        def is_alive(self):
            return self.alive
    callers = [RealThread(target=dbpool._ensure_reaper) for _ in range(2)]
    monkeypatch.setattr(dbpool.threading, "Thread", SlowStart)
    try:
        callers[0].start()
        assert first_in.wait(10)
        callers[1].start()
        for c in callers:
            c.join(10)
        assert len(started) == 1 and dbpool._reaper[0] is started[0]
        dbpool._ensure_reaper()
        assert len(started) == 1                         # alive: no other

        class NoStart(SlowStart):
            def start(self):
                raise RuntimeError("can't start new thread")
        dbpool._reaper[0] = None
        monkeypatch.setattr(dbpool.threading, "Thread", NoStart)
        dbpool._ensure_reaper()                          # raises nothing
        assert dbpool._reaper[0] is None
    finally:
        monkeypatch.setattr(dbpool.threading, "Thread", RealThread)
        dbpool._reaper[0] = None


def test_a_released_lease_has_no_file_descriptor(monkeypatch, resets):
    pg = SocketPG()
    try:
        lease = dbpool._lease(pg, KEY)
        assert lease.fileno() == pg.socket
        lease.close()
        with pytest.raises(psycopg.OperationalError, match="closed"):
            lease.fileno()
        parked = dbpool._lease(pg, KEY)                  # parked: released, its handle still the statement's
        parked._released = True
        assert parked.pgconn is pg
        with pytest.raises(psycopg.OperationalError):
            parked.fileno()
    finally:
        pg.shut()


def test_a_driver_with_no_wait_at_all_reads_as_one_that_cannot_bound_it(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    monkeypatch.delattr(psycopg.Connection, "wait")
    assert dbpool._driver_bounds_a_wait() is False and dbpool.size() == 0


# ------------------------------------------------------------------------------------------------ round 7 (a reviewer 02390b)

@pytest.fixture()
def safe_cancel(monkeypatch):
    """libpq 17 and later: the cancel is a connection of its own, driven by a generator within a bound. The stand-in
    generator answers at once, or (gate set) waits on a socket that never speaks."""
    import collections as _c
    monkeypatch.setattr(dbpool, "_has_safe_cancel", lambda: True)
    seen = _c.namedtuple("Seen", "calls")([])

    def cancel(handle):
        seen.calls.append(handle)
        pg = handle.pg
        pg.inside.set()
        if pg.cancel_gate is not None:
            while True:
                yield pg.ours.fileno(), dbpool.waiting.WAIT_R        # nothing ever arrives: only the bound ends it
        pg._asked()
        return
        yield                                                       # (a generator)
    monkeypatch.setattr(dbpool.generators, "cancel", cancel)
    return seen


def _end_now(monkeypatch, pg):
    """A parked lease, ended by one pass. Returns (lease, release, statement thread)."""
    lease, release, stmt = _parked_lease(monkeypatch, pg)
    dbpool._reap(dbpool._parked[-1].since + dbpool.PARK_MAX_S)
    return lease, release, stmt


@pytest.mark.parametrize("kind", ["old", "safe"])
def test_the_cancel_handle_is_taken_when_the_lease_is_made_and_the_handle_is_never_asked_again(kind, monkeypatch, resets, no_reaper, request):
    """P2-1. Round 6 sent no cancel at all; round 5 made one from the statement's handle on the reaper thread. The
    handle is made ONCE, by `_arm`; afterwards the session's handle refuses every question and it does not matter."""
    monkeypatch.setenv(ENV, "4")
    if kind == "safe":
        request.getfixturevalue("safe_cancel")
    pg = SocketPG()
    lease, release, stmt = _parked_lease(monkeypatch, pg)
    try:
        assert pg.made == 1 and lease._cancel_tok is not None and lease._cancel_tok[0] == kind
        wire = Tripwire()
        lease.pgconn = wire                              # from here on the handle may not be read at all
        dbpool._reap(dbpool._parked[0].since + dbpool.PARK_MAX_S - 0.5)
        assert not pg.inside.wait(0.1)                   # not before the bound
        dbpool._reap(dbpool._parked[0].since + dbpool.PARK_MAX_S)
        assert pg.cancelled.wait(5) and wire.touched == [] and pg.made == 1
        assert _until(lambda: dbpool.stats()["cancel_sent"] == 1 and pg.freed == 1)
        assert pg.started == (1 if kind == "safe" else 0) and lease._cancel_tok is None
        assert _until(lambda: dbpool._canceller[0] is None), "the worker did not end with nothing queued"
        dbpool._reap(dbpool._parked[0].since + dbpool.PARK_MAX_S + 1)        # a later pass asks nothing more
        time.sleep(0.2)
        assert pg.cancels == 1 and dbpool.stats()["cancel_sent"] == 1 and wire.touched == []
    finally:
        lease.pgconn = pg
        release.set()
        stmt.join(10)
        pg.shut()


def test_a_lease_that_is_only_closed_frees_its_cancel_handle_and_asks_the_server_nothing(monkeypatch, resets):
    monkeypatch.setenv(ENV, "4")
    for way in ("close", "discard"):
        pg = SocketPG(way)
        lease = dbpool._lease(pg, KEY)
        lease._arm()
        assert pg.made == 1 and pg.freed == 0
        getattr(lease, way)()
        assert pg.freed == 1 and lease._cancel_tok is None and not pg.inside.is_set()
        pg.shut()
    assert dbpool._canceller[0] is None and len(dbpool._cancels) == 0
    assert dbpool.stats()["cancel_sent"] == 0 and dbpool.stats()["cancel_dropped"] == 0


def test_a_lease_with_no_cancel_handle_starts_no_worker_and_is_still_ended(monkeypatch, resets, no_reaper):
    """`_arm` could make no handle (a stand-in without one; a libpq that refuses): nothing is queued, no thread."""
    monkeypatch.setenv(ENV, "4")
    pg = SocketPG()
    pg.get_cancel = None                                 # asking for one fails
    started = []
    real = dbpool._cancel_loop
    monkeypatch.setattr(dbpool, "_cancel_loop", lambda: (started.append(1), real()))
    lease, release, stmt = _end_now(monkeypatch, pg)
    try:
        assert lease._cancel_tok is None and pg.made == 0
        assert dbpool.stats()["aborted_parked"] == 1 and pg.peer_sees_the_end() and lease._aborted is True
        time.sleep(0.2)
        assert started == [] and dbpool._canceller[0] is None and len(dbpool._cancels) == 0
    finally:
        release.set()
        stmt.join(10)
        pg.shut()


def test_the_cancel_queue_is_bounded_one_worker_sends_in_turn_and_a_pass_never_waits_for_it(monkeypatch, resets, no_reaper):
    """P2-1 / round 5's P2-2 (3). A cancel that hangs holds the ONE worker; `_reap` returns at once; what does not
    fit the queue is dropped, counted and freed; when the network lets go the queued ones are sent in order."""
    import collections as _c
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 10)
    monkeypatch.setattr(dbpool, "_cancels", _c.deque(maxlen=2))
    gate = threading.Event()
    held = []
    del SocketPG.asked_in_order[:]
    try:
        for name in ("first", "second", "third", "fourth", "fifth"):
            pg = SocketPG(name)
            pg.cancel_gate = gate
            held.append((pg,) + _parked_lease(monkeypatch, pg))
        t0 = time.monotonic()
        dbpool._abort(held[0][1])                        # the worker takes it and hangs in it
        assert held[0][0].inside.wait(5)
        for h in held[1:]:
            dbpool._abort(h[1])
        took = time.monotonic() - t0
        assert took < 2.0, took                          # nobody waited for the network
        assert len(dbpool._cancels) == 2 and dbpool.stats()["cancel_dropped"] == 2
        assert [h[0].freed for h in held] == [0, 0, 0, 1, 1]                 # the dropped handles are freed, not kept
        assert sum(t.name == "dbpool-cancel" and t.is_alive() for t in threading.enumerate()) == 1
        t1 = time.monotonic()
        dbpool._reap()                                   # a pass while the worker hangs: at once
        assert time.monotonic() - t1 < 1.0 and all(h[0].peer_sees_the_end() for h in held)
        assert not held[1][0].inside.is_set()            # in turn: the second waits for the first
        gate.set()
        assert _until(lambda: dbpool.stats()["cancel_sent"] == 3)
        assert [h[0].cancels for h in held] == [1, 1, 1, 0, 0] and [h[0].freed for h in held] == [1, 1, 1, 1, 1]
        assert SocketPG.asked_in_order == ["first", "second", "third"]      # oldest first
        assert _until(lambda: dbpool._canceller[0] is None)
    finally:
        gate.set()
        for pg, _lease, release, stmt in held:
            release.set()
            stmt.join(10)
            pg.shut()


def test_a_cancel_that_fails_is_counted_and_the_next_one_is_still_sent(monkeypatch, resets, no_reaper):
    monkeypatch.setenv(ENV, "4")
    a, b = SocketPG("a"), SocketPG("b")
    a.cancel_fault = OSError("the server refuses")
    la, ra, sa = _parked_lease(monkeypatch, a)
    lb, rb, sb = _parked_lease(monkeypatch, b)
    try:
        dbpool._reap(dbpool._parked[-1].since + dbpool.PARK_MAX_S)
        assert b.cancelled.wait(5)
        assert _until(lambda: dbpool.stats()["cancel_failed"] == 1 and dbpool.stats()["cancel_sent"] == 1)
        assert (a.freed, b.freed) == (1, 1) and dbpool.stats()["aborted_parked"] == 2
    finally:
        for release, stmt, pg in ((ra, sa, a), (rb, sb, b)):
            release.set()
            stmt.join(10)
            pg.shut()


def test_the_cancel_connection_is_started_and_given_no_more_than_the_pools_bound(monkeypatch, resets, no_reaper, safe_cancel):
    """The safe kind: `start()`, then the driver's generator under POOL_IO_TIMEOUT_S. A server that never answers
    costs the worker that long and no longer."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "POOL_IO_TIMEOUT_S", 0.4)
    pg = SocketPG()
    pg.cancel_gate = threading.Event()                   # the stand-in generator waits on a silent socket
    t0 = time.monotonic()
    lease, release, stmt = _end_now(monkeypatch, pg)
    try:
        assert pg.inside.wait(5) and pg.started == 1
        assert _until(lambda: dbpool.stats()["cancel_failed"] == 1, 5)
        took = time.monotonic() - t0
        assert 0.3 < took < 3.0, took
        assert pg.freed == 1 and pg.cancels == 0 and len(safe_cancel.calls) == 1
    finally:
        release.set()
        stmt.join(10)
        pg.shut()


def _steps_forever(seen):
    """A statement's generator that is never ready, as the driver's execute is against a peer that says nothing."""
    try:
        while True:
            seen.append((yield dbpool.waiting.WAIT_R))
    finally:
        seen.append("closed")


@pytest.mark.parametrize("armed", [True, False])
def test_a_statement_waiting_on_a_silent_peer_is_woken_when_its_lease_is_ended(armed, monkeypatch, resets, no_reaper):
    """P2-2. The peer stays open and says nothing (a black hole; pg_sleep). On Windows the driver's wait does not
    see our shutdown of the socket, so the thread stayed in the wait and kept the lease's lock for good. Now the
    lease's own wait raises at its next step. `armed` False: there is not even a duplicate to shut down."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.05)
    pg = SocketPG()
    lease = dbpool._lease(pg, KEY)
    if armed:
        lease._arm()
    out, seen, inside = {}, [], threading.Event()

    def statement():
        try:
            with lease.lock:
                inside.set()
                out["value"] = lease.wait(_steps_forever(seen), interval=0.05)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc
        out["at"] = time.monotonic()
    t = threading.Thread(target=statement, daemon=True)
    t.start()
    try:
        assert inside.wait(5)
        time.sleep(0.3)
        assert t.is_alive() and seen and set(seen) == {0}                # stepped with "nothing ready", again and again
        lease.close()
        assert dbpool.stats()["parked"] == 1 and t.is_alive()            # parked: the statement is still in its wait
        t0 = time.monotonic()
        dbpool._reap(dbpool._parked[0].since + dbpool.PARK_MAX_S)
        t.join(5)
        assert not t.is_alive(), "the statement's thread is still in its wait"
        assert out["at"] - t0 < 1.0, out["at"] - t0
        assert isinstance(out.get("error"), psycopg.OperationalError) and "has been ended" in str(out["error"])
        assert seen[-1] == "closed"                                      # the driver's generator was closed, not dropped
        assert lease.lock.locked() is False
        if armed:
            assert pg.peer_sees_the_end()
        dbpool._reap()                                                   # the lock is free: finished as usual
        assert pg.finished == 1 and dbpool.stats()["closed_parked"] == 1 and dbpool._parked == []
    finally:
        pg.shut()
        t.join(5)


def test_the_watched_generator_is_the_same_generator_to_the_driver(monkeypatch):
    """Values out, values in, the return value, an exception thrown in and a close all pass through; and an ended
    lease raises before the first step and at any later one, closing the driver's generator each time."""
    lease = dbpool._lease(FakePG(), KEY)
    log_ = []

    def inner():
        try:
            got = yield "a"
            log_.append(got)
            try:
                got = yield "b"
            except KeyError as exc:
                log_.append(("thrown", type(exc).__name__))
                got = yield "after-throw"
            log_.append(got)
            return "the result"
        finally:
            log_.append("closed")
    g = lease._watched(inner())
    assert next(g) == "a" and g.send(1) == "b" and g.throw(KeyError("x")) == "after-throw"
    with pytest.raises(StopIteration) as stop:
        g.send(2)
    assert stop.value.value == "the result" and log_ == [1, ("thrown", "KeyError"), 2, "closed"]

    def empty():
        return "at once"
        yield
    with pytest.raises(StopIteration) as stop:
        next(lease._watched(empty()))
    assert stop.value.value == "at once"
    del log_[:]
    g = lease._watched(inner())                          # an exception the driver's generator does not catch comes out
    next(g)
    with pytest.raises(ZeroDivisionError):
        g.throw(ZeroDivisionError())
    assert log_ == ["closed"]
    del log_[:]
    g = lease._watched(inner())                          # close() closes the driver's generator
    next(g)
    g.close()
    assert log_ == ["closed"]
    del log_[:]
    g = lease._watched(inner())                          # ended between two steps: the next step raises
    next(g)
    lease._aborted = True
    with pytest.raises(psycopg.OperationalError):
        g.send(0)
    assert log_ == ["closed"]
    del log_[:]
    with pytest.raises(psycopg.OperationalError):        # ended before the first: nothing of it is handed out
        next(lease._watched(inner()))
    assert log_ == ["closed"]
    # and the lease's own wait goes through it (the pool's bounded round trips included)
    lease2 = dbpool._lease(FakePG(), KEY)
    monkeypatch.setattr(psycopg.Connection, "wait", lambda self, gen, *a, **k: (type(gen).__name__, list(gen), k))
    lease2._deadline = time.monotonic() + 5
    kind, items, kw = lease2.wait(x for x in ["x"])
    assert kind == "generator" and items == ["x"] and 0 < kw["timeout"] <= 5


def test_more_ended_sessions_than_the_cap_go_after_the_settle_grace_oldest_first(monkeypatch, resets, no_reaper, dups):
    """Round 6's P2-2 (3) and round 7's P3-1. More ENDED sessions than the cap are forgotten before PARK_FORGET_S,
    oldest first; but not in the instant they were ended (round 6 did that, in a second pass that could follow the
    first within a millisecond, before a woken thread had let go)."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 2)
    monkeypatch.setattr(dbpool, "PARK_MAX_S", 0.0)                       # every running one is ended at the first look
    held = []
    try:
        for name in ("first", "second", "third", "fourth", "fifth"):
            pg = SocketPG(name)
            held.append((pg,) + _parked_lease(monkeypatch, pg))
        t = time.monotonic()
        dbpool._reap(t)                                                  # five running: all five are ended
        assert dbpool.stats()["aborted_parked"] == 5 and dbpool.stats()["forgotten_parked"] == 0
        assert len(dbpool._parked) == 5 and all(e.aborted_at == t for e in dbpool._parked)
        dbpool._reap(t + dbpool.SETTLE_GRACE_S - 0.01)                   # five ended, cap 2, inside the grace: none goes
        assert dbpool.stats()["forgotten_parked"] == 0 and len(dbpool._parked) == 5
        dbpool._reap(t + dbpool.SETTLE_GRACE_S)                          # the grace is over: the three oldest go
        assert dbpool.stats()["forgotten_parked"] == 3 and dbpool.stats()["aborted_parked"] == 5
        assert [e.lease for e in dbpool._parked] == [held[3][1], held[4][1]]
        assert [h[1]._abort_sock for h in held[:3]] == [None, None, None]         # their duplicates are closed
        assert all(h[1]._abort_sock is not None for h in held[3:])
        dbpool._reap(t + dbpool.SETTLE_GRACE_S + 1)                      # two ended: within the cap, nothing more
        assert dbpool.stats()["forgotten_parked"] == 3 and len(dbpool._parked) == 2
    finally:
        for pg, _lease, release, stmt in held:
            release.set()
            stmt.join(10)
            pg.shut()
    dbpool._reap()
    assert dbpool._parked == [] and dbpool.stats()["closed_parked"] == 2


def test_past_the_hard_ceiling_the_oldest_ended_go_in_the_pass_that_ended_them(monkeypatch, resets, no_reaper, dups):
    """P3-1. The count of ended sessions is taken AFTER the pass has ended the running ones (before, a burst took
    two passes), and above MAX_PARKED * PARKED_HARD_X no grace is given."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 2)
    monkeypatch.setattr(dbpool, "PARKED_HARD_X", 2)                      # the ceiling is 4
    monkeypatch.setattr(dbpool, "PARK_MAX_S", 0.0)
    held = []
    try:
        for name in ("1", "2", "3", "4", "5", "6", "7"):
            pg = SocketPG(name)
            held.append((pg,) + _parked_lease(monkeypatch, pg))
        dbpool._reap(time.monotonic())                                   # ONE pass: seven ended, and down to the ceiling
        assert dbpool.stats()["aborted_parked"] == 7 and dbpool.stats()["forgotten_parked"] == 3
        assert [e.lease for e in dbpool._parked] == [h[1] for h in held[3:]]      # the oldest three went; four stay
        assert len(_open(dups)) == 4
    finally:
        for pg, _lease, release, stmt in held:
            release.set()
            stmt.join(10)
            pg.shut()


def test_forgetting_a_lease_whose_statement_has_let_go_finishes_it_instead(monkeypatch, resets, no_reaper, dups):
    """P3-1. Dropped for the cap a millisecond before its thread let go, a session was never finished: its handle
    stayed live on the lease until the collector took it. `_forget` looks once more."""
    monkeypatch.setenv(ENV, "4")
    gone, kept = SocketPG("let go"), SocketPG("still held")
    lg, rg, sg = _end_now(monkeypatch, gone)
    lk, rk, sk = _end_now(monkeypatch, kept)
    try:
        rg.set()
        sg.join(10)                                                      # its statement is over; no pass has looked yet
        eg, ek = dbpool._parked
        dbpool._forget(eg)
        assert gone.finished == 1 and isinstance(lg.pgconn, dbpool._Gone) and lg._abort_sock is None
        assert dbpool.stats()["closed_parked"] == 1 and dbpool.stats()["forgotten_parked"] == 0
        dbpool._forget(ek)                                               # still held: forgotten, never finished under it
        assert kept.finished == 0 and lk.pgconn is kept and lk._abort_sock is None
        assert dbpool.stats()["closed_parked"] == 1 and dbpool.stats()["forgotten_parked"] == 1
        assert _open(dups) == []
    finally:
        rk.set()
        sk.join(10)
        gone.shut()
        kept.shut()


class _BrokenLock:
    """A parked lease's lock that raises on every look."""

    def acquire(self, blocking=True, timeout=-1):
        raise RuntimeError("the lock is broken")

    def release(self):
        raise RuntimeError("the lock is broken")

    def locked(self):
        return True


def _faulty_parked(pg):
    lease = dbpool._lease(pg, KEY)
    lease._arm()
    lease.lock = _BrokenLock()
    lease._released = True
    return lease


@pytest.mark.parametrize("where", ["the lock", "settle itself"])
def test_a_lease_that_faults_on_every_look_is_still_ended_and_then_forgotten(where, monkeypatch, resets, no_reaper, dups):
    """P3-2. An exception out of `_settle` put the lease in neither list of the pass: never ended, never forgotten."""
    monkeypatch.setenv(ENV, "4")
    pg = SocketPG()
    lease = _faulty_parked(pg)
    if where == "settle itself":
        def boom(entry, now):
            raise RuntimeError("settle is broken")
        monkeypatch.setattr(dbpool, "_settle", boom)
    try:
        dbpool._parked.append(dbpool._Parked(lease, 1000.0))
        dbpool._reap(1000.0 + dbpool.PARK_MAX_S - 0.5)
        assert dbpool.stats()["aborted_parked"] == 0 and dbpool.stats()["parked"] == 1
        dbpool._reap(1000.0 + dbpool.PARK_MAX_S)
        assert dbpool.stats()["aborted_parked"] == 1 and pg.peer_sees_the_end() and pg.cancelled.wait(5)
        dbpool._reap(1000.0 + dbpool.PARK_MAX_S + dbpool.PARK_FORGET_S - 0.5)
        assert dbpool.stats()["parked"] == 1 and dbpool.stats()["forgotten_parked"] == 0
        dbpool._reap(1000.0 + dbpool.PARK_MAX_S + dbpool.PARK_FORGET_S)
        assert dbpool.stats()["parked"] == 0 and dbpool.stats()["forgotten_parked"] == 1
        assert lease._abort_sock is None and _open(dups) == [] and pg.finished == 0
    finally:
        dbpool._parked[:] = []
        pg.shut()


def test_the_reaper_thread_ends_after_a_lease_whose_lock_faults(monkeypatch, resets, dups):
    """P3-2, with the real thread: it ran every REAP_EVERY_S for good."""
    monkeypatch.setenv(ENV, "4")
    for name, value in (("REAP_EVERY_S", 0.02), ("PARK_MAX_S", 0.1), ("PARK_FORGET_S", 0.1)):
        monkeypatch.setattr(dbpool, name, value)
    assert _until(lambda: dbpool._reaper[0] is None, 15)
    pg = SocketPG()
    lease = _faulty_parked(pg)
    try:
        dbpool._park(lease)
        assert dbpool._reaper[0] is not None
        assert _until(lambda: dbpool.stats()["forgotten_parked"] == 1, 10)
        assert _until(lambda: dbpool._reaper[0] is None, 10), "the reaper thread is still running"
        assert dbpool.stats()["aborted_parked"] == 1 and dbpool._parked == [] and _open(dups) == []
    finally:
        dbpool._parked[:] = []
        pg.shut()


def test_which_cancel_this_libpq_has_is_asked_of_the_driver_and_a_fault_there_means_the_older_kind(monkeypatch):
    monkeypatch.undo()                                   # the suite's own stand-in for this very function
    assert dbpool._has_safe_cancel() is bool(psycopg.capabilities.has_cancel_safe())
    monkeypatch.setattr(psycopg.capabilities, "has_cancel_safe", lambda: True)
    assert dbpool._has_safe_cancel() is True
    monkeypatch.setattr(psycopg.capabilities, "has_cancel_safe", lambda: 1 / 0)
    assert dbpool._has_safe_cancel() is False


def test_the_idle_list_is_trimmed_to_the_largest_pool_there_is_even_if_size_says_more(monkeypatch, resets):
    """The census (tests/test_bounded_caches.py): the idle list's bound is stated at the container. `size()` never
    exceeds MAX_POOL_SIZE, so this is forced here: the oldest go, closed, and the newest stay."""
    monkeypatch.setattr(dbpool, "size", lambda lookup=None: 4)
    monkeypatch.setattr(dbpool, "MAX_POOL_SIZE", 2)
    pgs = [FakePG("pg%d" % n) for n in range(4)]
    for pg in pgs:
        dbpool.give_back(pg, KEY)
    assert len(dbpool._idle) == 2 and [e[0] for e in dbpool._idle] == pgs[2:]
    assert [pg.finished for pg in pgs] == [1, 1, 0, 0] and dbpool.stats()["closed_full"] == 2


def test_the_parked_list_is_capped_at_insertion_the_oldest_ended_and_forgotten(monkeypatch, resets, no_reaper, dups):
    """The census: between two reaper passes a burst parked one lease per request with no bound. `_park` now caps
    the list at MAX_PARKED * PARKED_HARD_X before it appends: the oldest are ended and forgotten, as the reaper's
    own hard ceiling would, and the list never holds more than the ceiling."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 2)
    monkeypatch.setattr(dbpool, "PARKED_HARD_X", 2)                      # the ceiling is 4
    held, sizes = [], []
    try:
        for name in ("1", "2", "3", "4", "5", "6"):
            pg = SocketPG(name)
            held.append((pg,) + _parked_lease(monkeypatch, pg))
            sizes.append(len(dbpool._parked))
        assert sizes == [1, 2, 3, 4, 4, 4]                               # never past the ceiling, no pass needed
        assert [e.lease for e in dbpool._parked] == [h[1] for h in held[2:]]
        assert dbpool.stats()["aborted_parked"] == 2 and dbpool.stats()["forgotten_parked"] == 2
        assert [h[1]._abort_sock for h in held[:2]] == [None, None]     # the shed ones' duplicates are closed
        assert held[0][0].finished == 0 and held[1][0].finished == 0     # never finished under their statements
    finally:
        for pg, _lease, release, stmt in held:
            release.set()
            stmt.join(10)
            pg.shut()


def test_a_shed_lease_whose_statement_let_go_is_finished_not_forgotten(monkeypatch, resets, no_reaper):
    """`_shed` looks once more, as `_forget` does: a statement that is already over is finished properly."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 1)
    monkeypatch.setattr(dbpool, "PARKED_HARD_X", 1)                      # the ceiling is 1
    first, second = FakePG("first"), FakePG("second")
    l1, r1, s1 = _parked_lease(monkeypatch, first)
    r1.set()
    s1.join(10)                                                          # over, but no pass has looked
    l2, r2, s2 = _parked_lease(monkeypatch, second)                      # parking it sheds the first
    try:
        assert first.finished == 1 and isinstance(l1.pgconn, dbpool._Gone)
        assert dbpool.stats()["closed_parked"] == 1 and dbpool.stats()["forgotten_parked"] == 0
        assert dbpool.stats()["aborted_parked"] == 0 and [e.lease for e in dbpool._parked] == [l2]
    finally:
        r2.set()
        s2.join(10)


def test_a_shed_lease_a_reaper_pass_is_still_ending_is_aborted_before_it_is_forgotten(monkeypatch, resets, no_reaper, dups):
    """GitHub Codex on 1f7ef89e (P2): `_end` sets aborted_at BEFORE it calls `_abort`. A shed that met an entry in
    that gap skipped the abort, and `_forget` then closed the duplicate and dropped the cancel token: the backend was
    never cancelled and its socket never shut down. `_shed` now aborts such an entry itself, before it forgets it."""
    monkeypatch.setenv(ENV, "4")
    monkeypatch.setattr(dbpool, "MAX_PARKED", 1)
    monkeypatch.setattr(dbpool, "PARKED_HARD_X", 1)                      # the ceiling is 1
    first, second = SocketPG("mid-end"), SocketPG("next")
    l1, r1, s1 = _parked_lease(monkeypatch, first)
    try:
        dbpool._parked[0].aborted_at = time.monotonic()                  # a reaper pass is between the two steps
        assert first.peer_sees_nothing() and not first.cancelled.is_set()  # control: nothing aborted yet
        l2, r2, s2 = _parked_lease(monkeypatch, second)                  # parking it sheds the first
        assert first.peer_sees_the_end(), "the shed entry's socket was never shut down"
        assert _until(lambda: first.cancelled.is_set()), "the shed entry's cancel was never sent"
        assert l1._abort_sock is None and dbpool.stats()["forgotten_parked"] == 1
        assert [e.lease for e in dbpool._parked] == [l2]
    finally:
        for r, s in ((r1, s1),) + (((r2, s2),) if "r2" in locals() else ()):
            r.set()
            s.join(10)
        first.shut()
        second.shut()
