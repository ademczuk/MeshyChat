"""Postgres gate: what a REAL session carries from one lease of the connection pool to the next (nothing).

Not part of database-free validation: it needs the migrated gate database. tests/test_db_pool.py holds the pool's
own logic with stand-ins; this file is the part only a server can answer: DISCARD ALL and the role binding on a
live session, a dropped backend, and the driver's prepared statements across leases.
"""
from __future__ import annotations

import psycopg
import pytest

from apps.core import dbconn, dbpool, rls
from tests.test_call_routing import owner_conn  # noqa: F401

SQL = "SELECT %s::int + 1"


@pytest.fixture(autouse=True)
def _pool_on(monkeypatch):
    monkeypatch.setenv(dbpool.POOL_SIZE_ENV, "2")
    dbpool.drain()
    yield
    dbpool.drain()


def _pid(conn) -> int:
    return conn.execute("SELECT pg_backend_pid()").fetchone()[0]


def _show(conn, name: str):
    return conn.execute("SELECT current_setting(%s, true)", (name,)).fetchone()[0]


def test_the_next_lease_gets_the_same_backend_with_nothing_of_the_last_one(owner_conn):  # noqa: F811
    first = dbconn.connect()
    assert isinstance(first, dbpool.Lease)
    pid = _pid(first)
    first.execute("SET statement_timeout = 1234")
    first.execute("SET TimeZone = 'Europe/Vienna'")
    first.execute("SELECT set_config('app.current_peer_id', 'left-behind', false)")       # session level, on purpose
    first.execute("SELECT set_config('app.server_internal', '1', false)")
    first.execute("CREATE TEMP TABLE pool_leftover (x int)")
    first.execute("LISTEN pool_leftover_channel")
    first.execute("SELECT pg_advisory_lock(424242)")
    for n in range(8):                                                     # past the driver's prepare threshold
        assert first.execute(SQL, (n,)).fetchone()[0] == n + 1
    first.close()
    assert first.closed and dbpool.stats()["idle"] == 1

    second = dbconn.connect()
    try:
        assert second is not first and _pid(second) == pid                 # the same session, a new object
        assert second.execute("SELECT current_user").fetchone()[0] == rls.APP_ROLE
        dbconn.assert_serving_connection(second)
        assert second.execute("SELECT setting::bigint FROM pg_settings WHERE name = 'statement_timeout'"
                              ).fetchone()[0] == dbconn.default_statement_timeout_ms()
        assert _show(second, "TimeZone") == "UTC"
        assert _show(second, "app.current_peer_id") in (None, "")
        assert _show(second, "app.server_internal") in (None, "")
        assert _show(second, rls.SIGNED_BYTES_WRITER_SETTING) == "on"
        assert second.execute("SELECT to_regclass('pg_temp.pool_leftover')").fetchone()[0] is None
        assert second.execute("SELECT count(*) FROM pg_listening_channels()").fetchone()[0] == 0
        assert second.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                              ).fetchone()[0] == 0
        for n in range(8):                                                 # the driver prepares again, cleanly
            assert second.execute(SQL, (n,)).fetchone()[0] == n + 1
        with pytest.raises(psycopg.OperationalError):                      # the old object is dead for good
            first.execute("SELECT 1")
    finally:
        second.close()


def test_a_notification_queued_for_the_last_lease_is_not_read_by_the_next(owner_conn):  # noqa: F811
    """Round 2 (the security read of 6f76919e): libpq keeps notifications it has already received; UNLISTEN and
    DISCARD ALL do not take them back. The lease listens, a notification is committed for it, the lease closes without
    reading it; the next lease on the same backend must see none."""
    first = dbconn.connect()
    pid = _pid(first)
    first.execute("LISTEN pool_r2_channel")
    with owner_conn.cursor() as cur:
        cur.execute("SELECT pg_notify('pool_r2_channel', 'for the last lease')")
    owner_conn.commit()
    # Round 3: the precondition must be real. A statement on `first` would let the driver consume the notification
    # itself, and the test would pass with nothing queued. So nothing is executed: the test waits until the
    # notification's bytes are ON THE SOCKET, unread, and closes the lease in that state.
    import select
    readable, _, _ = select.select([first.pgconn.socket], [], [], 10)
    assert readable, "precondition: the notification never reached this session's socket"
    first.close()
    second = dbconn.connect()
    try:
        assert _pid(second) == pid
        assert list(second.notifies(timeout=0.3)) == []
        assert second.execute("SELECT count(*) FROM pg_listening_channels()").fetchone()[0] == 0
    finally:
        second.close()


def test_a_listen_connection_asked_for_with_pool_false_is_a_session_of_its_own(owner_conn):  # noqa: F811
    listen = dbconn.connect(pool=False)
    assert not isinstance(listen, dbpool.Lease)
    pid = _pid(listen)
    listen.close()
    assert dbpool.stats()["idle"] == 0
    other = dbconn.connect()
    try:
        assert _pid(other) != pid
    finally:
        other.close()


def test_close_from_another_thread_waits_for_the_statement_and_the_next_lease_is_clean(owner_conn):  # noqa: F811
    """Round 3, P1, with a real cursor: a statement is running on the lease when another thread closes it."""
    import threading
    import time
    lease = dbconn.connect()
    pid = _pid(lease)
    cur = lease.cursor()
    out = {}

    def statement():
        try:
            out["row"] = cur.execute("SELECT pg_sleep(1.0), 7").fetchone()[1]
        except Exception as exc:  # noqa: BLE001 - reported by the main thread
            out["error"] = exc
    t = threading.Thread(target=statement)
    t.start()
    time.sleep(0.3)                                                        # the statement is in flight
    began = time.monotonic()
    lease.close()                                                          # must wait for it, not reset under it
    waited = time.monotonic() - began
    t.join(10)
    assert out == {"row": 7}, out                                          # the statement finished, undisturbed
    assert waited > 0.4, waited
    with pytest.raises(psycopg.OperationalError):
        cur.execute("SELECT 1")                                            # the old cursor is dead afterwards
    second = dbconn.connect()
    try:
        assert _pid(second) == pid and second.execute("SELECT 1").fetchone()[0] == 1
    finally:
        second.close()


def test_a_statement_longer_than_the_wait_parks_the_session_and_it_is_never_reused(owner_conn, monkeypatch):  # noqa: F811
    import threading
    import time
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.2)
    lease = dbconn.connect()
    pid = _pid(lease)
    cur = lease.cursor()
    out = {}

    def statement():
        try:
            out["row"] = cur.execute("SELECT pg_sleep(1.5), 7").fetchone()[1]
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc
    t = threading.Thread(target=statement)
    t.start()
    time.sleep(0.3)
    lease.close()                                                          # gives up after 0.2 s: parked, not pooled
    assert dbpool.stats()["idle"] == 0 and dbpool.stats()["parked_busy"] >= 1
    other = dbconn.connect()
    try:
        assert _pid(other) != pid                                          # never handed out
    finally:
        other.close()
    t.join(10)
    assert out == {"row": 7}, out                                          # and never closed under its statement
    dbpool._reap()
    with owner_conn.cursor() as c:
        c.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,))
        assert c.fetchone()[0] == 0                                        # finished once its statement had ended
    owner_conn.commit()


def _backend_gone(owner_conn, pid: int, secs: float = 10.0) -> bool:  # noqa: F811
    import time
    end = time.monotonic() + secs
    while True:
        with owner_conn.cursor() as c:
            c.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,))
            n = c.fetchone()[0]
        owner_conn.commit()
        if n == 0 or time.monotonic() >= end:
            return n == 0
        time.sleep(0.1)


def test_a_parked_session_is_finished_by_the_reaper_thread_with_no_pool_call(owner_conn, monkeypatch):  # noqa: F811
    """Round 5 (nxxxxx 85fb2d, P2): an idle process must not keep a parked backend."""
    import threading
    import time
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.2)
    monkeypatch.setattr(dbpool, "REAP_EVERY_S", 0.1)
    lease = dbconn.connect()
    pid = _pid(lease)
    cur = lease.cursor()
    out = {}

    def statement():
        try:
            out["row"] = cur.execute("SELECT pg_sleep(1.0), 7").fetchone()[1]
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc
    t = threading.Thread(target=statement)
    t.start()
    time.sleep(0.3)
    lease.close()
    t.join(10)
    assert out == {"row": 7}, out                                          # the statement ended by itself, unharmed
    assert _backend_gone(owner_conn, pid, 5)                               # and NO pool call was made after it
    assert dbpool.stats()["aborted_parked"] == 0


def test_a_statement_that_outlasts_the_bound_is_ended_at_the_server_and_on_the_client(owner_conn, monkeypatch):  # noqa: F811
    """Round 5, P2: a parked statement that runs on is cancelled at the server and its socket is shut down.
    Round 6 dropped the cancel and this case could not pass: pg_sleep does not read its socket, so the backend slept
    on. Round 7: the cancel request (its handle taken when the lease was made) ends the statement at the server,
    and the lease's own wait raises within a step, also on Windows, where the shut socket wakes nobody."""
    import threading
    import time
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.2)
    monkeypatch.setattr(dbpool, "REAP_EVERY_S", 0.1)
    monkeypatch.setattr(dbpool, "PARK_MAX_S", 0.5)
    lease = dbconn.connect()
    pid = _pid(lease)
    cur = lease.cursor()
    out = {}

    def statement():
        try:
            out["row"] = cur.execute("SELECT pg_sleep(25), 7").fetchone()[1]
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc
    t = threading.Thread(target=statement)
    t0 = time.monotonic()
    t.start()
    time.sleep(0.3)
    lease.close()
    t.join(10)
    assert not t.is_alive() and time.monotonic() - t0 < 10                 # ended long before its 25 s
    assert isinstance(out.get("error"), psycopg.Error), out
    assert _backend_gone(owner_conn, pid, 10)
    assert dbpool.stats()["aborted_parked"] >= 1
    other = dbconn.connect()
    try:
        assert _pid(other) != pid and other.execute(SQL, (1,)).fetchone()[0] == 2
    finally:
        other.close()


def test_a_stream_left_open_does_not_keep_its_backend(owner_conn, monkeypatch):  # noqa: F811
    """Round 5, P2, the paused stream. A stream holds the connection's lock for as long as its generator lives.
    (a) Its OWNER closes the lease with the stream paused. The lock is held by the owner's own generator and is not
    re-entrant, so the close cannot take it: until round 6 it waited CLOSE_WAIT_S for nothing (this text said "taken
    at once", which was wrong; a reviewer 0c867a). Now the lease's lock knows its holder, the close parks AT ONCE,
    and the reaper ends the backend at the bound. (b) ANOTHER thread closes a lease
    whose stream is paused: the lock never comes free, the session is parked, and at the bound its backend is ended
    although the stream is still open and nobody reads it."""
    import threading
    import time
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 5.0)                      # (a) must not wait this out
    monkeypatch.setattr(dbpool, "REAP_EVERY_S", 0.1)
    monkeypatch.setattr(dbpool, "PARK_MAX_S", 0.5)
    # (a)
    lease = dbconn.connect()
    pid = _pid(lease)
    rows = lease.cursor().stream("SELECT generate_series(1, 200000)")
    assert next(rows) == (1,)
    parked_before, t0 = dbpool.stats()["parked_busy"], time.monotonic()
    lease.close()
    assert time.monotonic() - t0 < 1.0, "the owner's close waited for a lock only it could free"
    assert dbpool.stats()["parked_busy"] == parked_before + 1
    assert dbpool.stats()["idle"] == 0 and lease.closed
    assert _backend_gone(owner_conn, pid, 5)
    with pytest.raises(psycopg.Error):
        next(rows)
    # (b)
    monkeypatch.setattr(dbpool, "CLOSE_WAIT_S", 0.2)
    lease = dbconn.connect()
    pid = _pid(lease)
    rows = lease.cursor().stream("SELECT generate_series(1, 200000)")
    assert next(rows) == (1,)
    closer = threading.Thread(target=lease.close)
    closer.start()
    closer.join(10)
    assert not closer.is_alive() and dbpool.stats()["idle"] == 0
    assert _backend_gone(owner_conn, pid, 10)                              # with the stream STILL open and unread
    assert dbpool.stats()["aborted_parked"] >= 1
    with pytest.raises(psycopg.Error):
        next(rows)
    other = dbconn.connect()
    try:
        assert _pid(other) != pid and other.execute(SQL, (1,)).fetchone()[0] == 2
    finally:
        other.close()


def test_batch_execution_then_close_and_the_next_lease_works(owner_conn):  # noqa: F811
    """Round 3, P3-5: executemany uses the driver's internal pipeline; the session must come back usable."""
    first = dbconn.connect()
    pid = _pid(first)
    first.execute("CREATE TEMP TABLE pool_batch (x int)")
    with first.cursor() as cur:
        cur.executemany("INSERT INTO pool_batch VALUES (%s)", [(n,) for n in range(50)])
    assert first.execute("SELECT count(*) FROM pool_batch").fetchone()[0] == 50
    first.close()
    assert dbpool.stats()["idle"] == 1
    second = dbconn.connect()
    try:
        assert _pid(second) == pid
        assert second.execute("SELECT to_regclass('pg_temp.pool_batch')").fetchone()[0] is None
        with second.cursor() as cur:
            cur.executemany("SELECT %s::int", [(n,) for n in range(10)])
        assert second.execute("SELECT 41 + 1").fetchone()[0] == 42
    finally:
        second.close()


def test_a_lease_refuses_cancel(owner_conn):  # noqa: F811
    lease = dbconn.connect()
    try:
        with pytest.raises(psycopg.NotSupportedError):
            lease.cancel()
        with pytest.raises(psycopg.NotSupportedError):
            lease.cancel_safe()
        assert lease.execute("SELECT 1").fetchone()[0] == 1
    finally:
        lease.close()


def test_a_lease_refuses_pipeline_mode_and_stays_usable(owner_conn):  # noqa: F811
    lease = dbconn.connect()
    try:
        with pytest.raises(psycopg.NotSupportedError):
            lease.pipeline()
        assert lease.execute("SELECT 1").fetchone()[0] == 1
    finally:
        lease.close()
    assert dbpool.stats()["idle"] == 1


def test_a_peer_context_ends_with_its_lease(owner_conn):  # noqa: F811
    first = dbconn.connect()
    pid = _pid(first)
    with dbconn.request_peer_context(first, peer_id="11111111-1111-1111-1111-111111111111",
                                     owner_id="0447bf92-25fb-5230-b531-5a0d9c00d8c7"):
        assert rls.context_values(first)["peer_id"] == "11111111-1111-1111-1111-111111111111"
    first.close()
    second = dbconn.connect()
    try:
        assert _pid(second) == pid
        values = rls.context_values(second)
        assert values["peer_id"] in (None, "") and values["owner_id"] in (None, "")
        assert values["role"] == rls.APP_ROLE
    finally:
        second.close()


@pytest.mark.parametrize("leave", ["open", "failed"])
def test_a_lease_closed_inside_a_transaction_is_not_reused(owner_conn, leave):  # noqa: F811
    first = dbconn.connect()
    pid = _pid(first)
    first.execute("BEGIN")
    if leave == "failed":
        with pytest.raises(psycopg.Error):
            first.execute("SELECT 1 / 0")
    first.close()
    assert dbpool.stats()["idle"] == 0 and dbpool.stats()["closed_not_idle"] >= 1
    second = dbconn.connect()
    try:
        assert _pid(second) != pid
    finally:
        second.close()


def test_an_idle_session_the_server_dropped_is_replaced_without_an_error(owner_conn):  # noqa: F811
    first = dbconn.connect()
    pid = _pid(first)
    first.close()
    assert dbpool.stats()["idle"] == 1
    with owner_conn.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
    owner_conn.commit()
    second = dbconn.connect()
    try:
        assert _pid(second) != pid and second.execute("SELECT 1").fetchone()[0] == 1
        assert dbpool.stats()["closed_dead"] >= 1
    finally:
        second.close()


def test_with_the_pool_off_a_connection_is_a_plain_one_and_close_ends_its_session(owner_conn, monkeypatch):  # noqa: F811
    monkeypatch.delenv(dbpool.POOL_SIZE_ENV, raising=False)
    conn = dbconn.connect()
    assert not isinstance(conn, dbpool.Lease)
    pid = _pid(conn)
    conn.close()
    assert dbpool.stats()["idle"] == 0
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,))
        assert cur.fetchone()[0] == 0
    owner_conn.commit()
