"""The presence store (`apps/wsgw/hub_presence.py`, 041 S2): T13's presence half.

Design 4.3. Each worker process writes a row per socket it holds into `hub_connections`
(sql_meshychat/016), keeps it fresh with a 10 s beat, and deletes it when the socket closes;
ANY process answers "is this peer online" from the table. The worker's own ConnectionManager
cannot answer that for a socket held by another process, which is the point: T13's two
workers are two connections here, one writing through worker A's code and one reading as
worker B, with nothing registered in this process's registry.

What is NOT tested here: the consumer writing a row and the beat running on the CallBridge
thread (S3, tests/test_call_bridge.py), step 9 reading `is_online` (S5). The prune S2's
review owed S3 is tested here as a store function and there as part of the beat.

Ages are set by the bootstrap role on the DATABASE clock, the clock `is_online` compares
with, so no test sleeps. A row aged past the window stays past it, since time only moves
forward; a row aged just inside it is re-read if the read could have run late (the window
test says how).

Every test here runs inside `hub_connections_clean` (design section 6): the table must be
empty when a test starts and is emptied when it ends.
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import logging
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import dbconn  # noqa: E402
from apps.wsgw import hub_presence  # noqa: E402
from apps.wsgw.connection_manager import MANAGER  # noqa: E402
from tests.fixtures.hub_presence_db import hub_connections_clean  # noqa: E402,F401  (autouse)

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def owner_conn():
    """The bootstrap role: seeds peers, ages rows, and measures. Bypasses RLS."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def worker_a():
    """Worker A's query connection: the serving constructor (meshychat_app, verified)."""
    conn = dbconn.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def worker_b():
    """A SEPARATE connection standing in for worker B, the process that does not hold
    the socket."""
    conn = dbconn.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


def _peer(conn, tag: str) -> uuid.UUID:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane) VALUES (%s,%s,'product')",
                    (owner_id, f"hubp-{tag}-{owner_id.hex[:8]}@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (peer_id, owner_id, f"hubp-{tag}-{peer_id.hex[:8]}"))
    conn.commit()
    return peer_id


def _age(conn, connection_id, seconds: float) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE hub_connections SET seen_at = now() - make_interval(secs => %s) "
                    "WHERE connection_id = %s", (seconds, connection_id))
        assert cur.rowcount == 1, "the row to age must exist"
    conn.commit()


def _rows(conn, *connection_ids):
    with conn.cursor() as cur:
        cur.execute("SELECT connection_id, peer_id, worker_token, connected_at, seen_at, "
                    "now() - seen_at FROM hub_connections WHERE connection_id = ANY(%s) "
                    "ORDER BY connected_at, connection_id", (list(connection_ids),))
        rows = cur.fetchall()
    conn.commit()
    return rows


def _registry_online(peer_id) -> bool:
    return asyncio.run(MANAGER.is_online(peer_id))


# ------------------------------------------------------------ T13, presence half

def test_a_row_worker_a_writes_reads_online_from_worker_b(owner_conn, worker_a, worker_b):
    """T13's presence half. Worker A inserts; worker B, on its own connection and with no
    socket in this process's registry, reads the peer online. A peer with no row reads
    offline on the same connection (control), and so does the row once it is past 30 s."""
    target, stranger = _peer(owner_conn, "t"), _peer(owner_conn, "s")
    token_a, cid = uuid.uuid4(), uuid.uuid4()

    assert not hub_presence.is_online(worker_b, target), "no row yet: offline"
    hub_presence.insert(worker_a, cid, target, token_a)

    assert not _registry_online(target), (
        "precondition: this process's ConnectionManager holds no socket for the target, so "
        "an online answer can only have come from the table")
    assert hub_presence.is_online(worker_b, target) is True
    assert hub_presence.is_online(worker_b, stranger) is False, "control: a peer with no row"
    [(row_cid, row_peer, row_token, _c, _s, _age_now)] = _rows(owner_conn, cid)
    assert (row_cid, row_peer, row_token) == (cid, target, token_a), "worker A's own row"

    _age(owner_conn, cid, 31)
    assert hub_presence.is_online(worker_b, target) is False, (
        "a row past three missed beats (30 s) must read offline")


def test_online_is_thirty_seconds_on_the_database_clock(owner_conn, worker_a, worker_b):
    """The window is 30 s, pinned within a second on each side IN THE QUERY, not only in
    the constant: 29 s old is online, 30.5 s old is not. A 31 s or wider window reads the
    second row online; a 29 s or narrower one reads the first offline.

    30.5 s stays outside for good. 29 s is inside only while the read runs within 1 s of
    the aging, so an offline read is checked: if the row is STILL under 30 s after the read,
    it was under 30 s during it, and offline was wrong. If it is past 30 s the read may
    have run late, and the row is aged again, up to three times."""
    fresh_peer, stale_peer = _peer(owner_conn, "f"), _peer(owner_conn, "o")
    fresh, stale = uuid.uuid4(), uuid.uuid4()
    hub_presence.insert(worker_a, fresh, fresh_peer, uuid.uuid4())
    hub_presence.insert(worker_a, stale, stale_peer, uuid.uuid4())
    _age(owner_conn, stale, 30.5)
    assert hub_presence.is_online(worker_b, stale_peer) is False, "30.5 s old is outside 30 s"
    ages_after = []
    for _attempt in range(3):
        _age(owner_conn, fresh, 29)
        if hub_presence.is_online(worker_b, fresh_peer):
            break
        age_after = _rows(owner_conn, fresh)[0][5].total_seconds()
        ages_after.append(age_after)
        assert age_after >= 30, (
            f"a row {age_after:.3f} s old AFTER the read was under 30 s during it, and read "
            "offline: the query's window is narrower than 30 s")
    else:
        pytest.fail(f"three reads of a 29 s row each ran more than 1 s late: {ages_after}")
    assert hub_presence.ONLINE_WINDOW_SECS == 30
    assert hub_presence.ONLINE_WINDOW_SECS == 3 * hub_presence.BEAT_INTERVAL_SECS


def test_is_online_compares_on_the_database_clock_not_the_callers(owner_conn, worker_a,
                                                                   worker_b):
    """The cutoff is `now()` on the database, the one clock every worker shares; a
    worker's own clock takes no part. A test on one host tells the two apart by moving ONE
    of them: worker B's session resolves `now()` to a function in a schema of this test's,
    one hour ahead of `pg_catalog.now()`, while Python's clock is untouched. A fresh row
    then reads offline, as it must, since the database says it was seen an hour ago; a
    cutoff computed in Python would still read it online. The control is the same read on
    the same connection with the real `now()`."""
    target = _peer(owner_conn, "k")
    cid = uuid.uuid4()
    hub_presence.insert(worker_a, cid, target, uuid.uuid4())
    schema = f"hubclock_{uuid.uuid4().hex[:8]}"
    app_role = hub_presence.rls.APP_ROLE
    with owner_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"CREATE FUNCTION {schema}.now() RETURNS timestamptz LANGUAGE sql STABLE "
                    "AS $$ SELECT pg_catalog.now() + interval '1 hour' $$")
        cur.execute(f"GRANT USAGE ON SCHEMA {schema} TO {app_role}")
        cur.execute(f"GRANT EXECUTE ON FUNCTION {schema}.now() TO {app_role}")
    owner_conn.commit()
    try:
        assert hub_presence.is_online(worker_b, target) is True, "control: the real clock"
        with worker_b.cursor() as cur:
            cur.execute(f"SET search_path = {schema}, public, pg_catalog")
            cur.execute("SELECT now() - pg_catalog.now()")
            assert cur.fetchone()[0] == _dt.timedelta(hours=1), "the instrument: now() moved"
        assert hub_presence.is_online(worker_b, target) is False, (
            "an hour-old row on the database's clock read online: the cutoff did not come "
            "from the database")
    finally:
        with worker_b.cursor() as cur:
            cur.execute("RESET search_path")
        with owner_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        owner_conn.commit()
    assert hub_presence.is_online(worker_b, target) is True, "the clock is restored"


# ------------------------------------------------------------------- the beat

def test_a_beat_writes_a_row_the_insert_never_wrote(owner_conn, worker_a, worker_b):
    """A REAL upsert: a socket whose insert failed (here, never ran) gets its row from the
    next beat, so the peer reads online. An update-only beat would leave it offline."""
    target = _peer(owner_conn, "r")
    cid, token = uuid.uuid4(), uuid.uuid4()
    assert not hub_presence.is_online(worker_b, target)
    assert hub_presence.upsert_beat(worker_a, [(cid, target)], token) == 1
    assert hub_presence.is_online(worker_b, target) is True
    [(row_cid, row_peer, row_token, *_rest)] = _rows(owner_conn, cid)
    assert (row_cid, row_peer, row_token) == (cid, target, token)


def test_a_beat_refreshes_seen_at_and_keeps_connected_at(owner_conn, worker_a, worker_b):
    """An aged row is brought back inside the window by the beat, which moves `seen_at`
    and leaves `connected_at` (when the socket connected) where it was."""
    target = _peer(owner_conn, "b")
    cid, token = uuid.uuid4(), uuid.uuid4()
    hub_presence.insert(worker_a, cid, target, token)
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE hub_connections SET connected_at = now() - interval '1 hour', "
                    "seen_at = now() - interval '1 hour' WHERE connection_id = %s", (cid,))
    owner_conn.commit()
    [(_, _, _, connected_before, _, _)] = _rows(owner_conn, cid)
    assert hub_presence.is_online(worker_b, target) is False

    assert hub_presence.upsert_beat(worker_a, [(cid, target)], token) == 1
    [(_, _, _, connected_after, _, since_seen)] = _rows(owner_conn, cid)
    assert hub_presence.is_online(worker_b, target) is True
    assert connected_after == connected_before, "a beat must not reset connected_at"
    assert since_seen.total_seconds() < 5, since_seen


def test_one_beat_covers_every_socket_the_worker_holds(owner_conn, worker_a, worker_b):
    """Several peers and several sockets per peer in one beat; an empty registry beats
    nothing and raises nothing."""
    p1, p2 = _peer(owner_conn, "m1"), _peer(owner_conn, "m2")
    rows = [(uuid.uuid4(), p1), (uuid.uuid4(), p1), (uuid.uuid4(), p2)]
    token = uuid.uuid4()
    assert hub_presence.upsert_beat(worker_a, rows, token) == 3
    got = _rows(owner_conn, *[c for c, _ in rows])
    assert sorted((r[0], r[1], r[2]) for r in got) == sorted((c, p, token) for c, p in rows)
    assert hub_presence.is_online(worker_b, p1) and hub_presence.is_online(worker_b, p2)
    assert hub_presence.upsert_beat(worker_a, [], token) == 0


def test_insert_after_a_beat_already_wrote_the_row_does_not_fail(owner_conn, worker_a):
    """The beat reads the registry, so it can write a new socket's row before that socket's
    own insert runs. The insert must then refresh the row, not raise UniqueViolation."""
    target = _peer(owner_conn, "i")
    cid, token = uuid.uuid4(), uuid.uuid4()
    hub_presence.upsert_beat(worker_a, [(cid, target)], token)
    hub_presence.insert(worker_a, cid, target, token)
    assert len(_rows(owner_conn, cid)) == 1


def test_a_beat_carrying_one_socket_twice_writes_it_once(owner_conn, worker_a, worker_b):
    """One statement cannot upsert one key twice: Postgres refuses "ON CONFLICT DO UPDATE
    command cannot affect row a second time", and the WHOLE beat would fail. The same pair
    twice is one socket, written once."""
    target = _peer(owner_conn, "dup")
    cid, other, token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert hub_presence.upsert_beat(worker_a, [(cid, target), (other, target), (cid, target)],
                                    token) == 2
    assert sorted(r[0] for r in _rows(owner_conn, cid, other)) == sorted([cid, other])
    assert hub_presence.is_online(worker_b, target) is True


def test_a_beat_naming_two_peers_for_one_socket_is_refused_before_any_sql(owner_conn,
                                                                          worker_a):
    """A connection_id names ONE socket, bound to one peer. A beat that pairs it with two
    peers is a caller bug, and choosing one of them silently would mark a peer online on
    a socket that may not be its own: a presence row is a routing input. So the beat is
    refused, before any row of it is written, including its good pairs."""
    p1, p2 = _peer(owner_conn, "c1"), _peer(owner_conn, "c2")
    cid, good, token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValueError, match="two peers"):
        hub_presence.upsert_beat(worker_a, [(good, p1), (cid, p1), (cid, p2)], token)
    assert _rows(owner_conn, cid, good) == [], "a refused beat writes nothing"
    assert hub_presence.upsert_beat(worker_a, [(good, p1), (cid, p2)], token) == 2, (
        "control: the same sockets, one peer each, are written")


def test_a_conflict_takes_the_writers_peer_and_token(owner_conn, worker_a, worker_b):
    """On conflict the row takes the WRITER's `peer_id` and `worker_token`: the worker that
    holds the socket is the authority on both, and a prune of a dead worker's rows by token
    (owed to S3) depends on the token being the last writer's. `connected_at` stays."""
    p1, p2 = _peer(owner_conn, "w1"), _peer(owner_conn, "w2")
    cid, token_old, token_new = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hub_presence.insert(worker_a, cid, p1, token_old)
    [(_, _, _, connected_before, _, _)] = _rows(owner_conn, cid)
    assert hub_presence.upsert_beat(worker_a, [(cid, p2)], token_new) == 1
    [(row_cid, row_peer, row_token, connected_after, _, _)] = _rows(owner_conn, cid)
    assert (row_cid, row_peer, row_token) == (cid, p2, token_new)
    assert connected_after == connected_before
    assert hub_presence.is_online(worker_b, p2) is True
    assert hub_presence.is_online(worker_b, p1) is False, "the socket is no longer p1's"


def _delete_peer(conn, peer_id) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM peers WHERE id = %s", (peer_id,))
        assert cur.rowcount == 1
    conn.commit()


def test_a_beat_skips_a_socket_whose_peer_was_deleted_and_refreshes_the_rest(
        owner_conn, worker_a, worker_b, caplog):
    """A peer deleted while its socket is still open (the revocation sweep closes it
    within about 10 s, unless the LiveBridge that drives it is down) must not fail the
    beat for every OTHER socket on the worker: one foreign-key violation would leave them
    all unrefreshed, and after 30 s every peer on the worker would read offline. The
    vanished peer's socket is skipped and named in a warning; the others are refreshed."""
    gone, live = _peer(owner_conn, "vg"), _peer(owner_conn, "vl")
    c_gone, c_live, token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hub_presence.insert(worker_a, c_gone, gone, token)
    hub_presence.insert(worker_a, c_live, live, token)
    _age(owner_conn, c_live, 3600)
    _delete_peer(owner_conn, gone)
    assert _rows(owner_conn, c_gone) == [], "precondition: the cascade took the gone row"
    assert hub_presence.is_online(worker_b, live) is False, "precondition: live row is aged"

    with caplog.at_level(logging.WARNING, logger=hub_presence.__name__):
        written = hub_presence.upsert_beat(worker_a, [(c_gone, gone), (c_live, live)], token)
    assert written == 1, "only the live socket is written"
    [(_, _, _, _, _, since_seen)] = _rows(owner_conn, c_live)
    assert since_seen.total_seconds() < 5, "the live socket was refreshed"
    assert hub_presence.is_online(worker_b, live) is True
    assert hub_presence.is_online(worker_b, gone) is False
    assert _rows(owner_conn, c_gone) == []
    warned = [r.getMessage() for r in caplog.records if r.name == hub_presence.__name__]
    assert warned and str(c_gone) in warned[0], warned
    assert str(c_live) not in caplog.text, "the written socket is not reported"


def test_insert_for_a_peer_that_no_longer_exists_writes_nothing_and_says_so(owner_conn,
                                                                           worker_a):
    """The socket's own insert takes the same path: a peer deleted between the upgrade
    and the insert gets no row and no exception, and `insert` returns False; a live peer's
    insert returns True (control)."""
    gone, live = _peer(owner_conn, "ig"), _peer(owner_conn, "il")
    _delete_peer(owner_conn, gone)
    cid, control = uuid.uuid4(), uuid.uuid4()
    assert hub_presence.insert(worker_a, cid, gone, uuid.uuid4()) is False
    assert _rows(owner_conn, cid) == []
    assert hub_presence.insert(worker_a, control, live, uuid.uuid4()) is True
    assert len(_rows(owner_conn, control)) == 1


# ----------------------------------------------------------------- delete

def test_delete_removes_that_socket_only(owner_conn, worker_a, worker_b):
    """A peer with two sockets stays online when one closes, and goes offline with the
    second. Deleting an unknown connection reports nothing deleted."""
    target = _peer(owner_conn, "d")
    c1, c2, token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hub_presence.insert(worker_a, c1, target, token)
    hub_presence.insert(worker_a, c2, target, token)

    assert hub_presence.delete(worker_a, c1) is True
    assert [r[0] for r in _rows(owner_conn, c1, c2)] == [c2], "only c1's row goes"
    assert hub_presence.is_online(worker_b, target) is True, "c2 still holds a socket"
    assert hub_presence.delete(worker_a, c2) is True
    assert hub_presence.is_online(worker_b, target) is False
    assert hub_presence.delete(worker_a, uuid.uuid4()) is False


# ------------------------------------------------------------------ prune (041 S3)

def test_prune_deletes_only_rows_older_than_the_prune_window(owner_conn, worker_a, worker_b):
    """S2 review F6, owed by S3: a crashed worker's rows stop being refreshed and read
    offline after 30 s, and nothing deleted them. A prune deletes a row older than
    PRUNE_AFTER_SECS and keeps one that is merely offline and one that is fresh, whoever
    wrote them: any worker's beat prunes any worker's rows."""
    target = _peer(owner_conn, "prune")
    old, offline, fresh = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for cid in (old, offline, fresh):
        assert hub_presence.insert(worker_a, cid, target, uuid.uuid4()) is True
    _age(owner_conn, old, hub_presence.PRUNE_AFTER_SECS + 1)
    _age(owner_conn, offline, hub_presence.ONLINE_WINDOW_SECS + 1)

    assert hub_presence.prune(worker_b) == 1
    assert {r[0] for r in _rows(owner_conn, old, offline, fresh)} == {offline, fresh}
    assert hub_presence.prune(worker_b) == 0, "a second prune finds nothing"


def test_a_prune_never_reaches_into_the_online_window(worker_a):
    """A row inside the online window is a routing input; a prune that could delete one
    would send an online peer's calls to `queued`. So the window is its floor."""
    assert hub_presence.PRUNE_AFTER_SECS >= hub_presence.ONLINE_WINDOW_SECS
    with pytest.raises(ValueError):
        hub_presence.prune(worker_a, hub_presence.ONLINE_WINDOW_SECS - 1)
    assert hub_presence.prune(worker_a, hub_presence.ONLINE_WINDOW_SECS) == 0, "control"


# ------------------------------------------------------------------ the guards

def test_every_function_runs_under_server_internal(owner_conn):
    """The table refuses every command outside server_internal (016). A store function
    that forgot the context would read offline and write nothing, silently for the reads,
    so each one is called on a plain meshychat_app connection and must WORK there: it enters
    the context itself."""
    target = _peer(owner_conn, "g")
    cid, token = uuid.uuid4(), uuid.uuid4()
    conn = dbconn.connect(DSN)
    try:
        hub_presence.insert(conn, cid, target, token)
        assert len(_rows(owner_conn, cid)) == 1, "insert wrote nothing"
        assert hub_presence.is_online(conn, target) is True, "is_online read nothing"
        _age(owner_conn, cid, 3600)
        assert hub_presence.upsert_beat(conn, [(cid, target)], token) == 1
        assert _rows(owner_conn, cid)[0][5].total_seconds() < 5, "the beat refreshed nothing"
        assert hub_presence.delete(conn, cid) is True
        assert _rows(owner_conn, cid) == [], "delete removed nothing"
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.server_internal', true)")
            assert cur.fetchone()[0] in (None, ""), "the context must end with the call"
    finally:
        conn.close()


@pytest.mark.parametrize("bad", ["not-a-uuid", "", None, 12345])
def test_a_peer_or_connection_id_that_is_not_a_uuid_is_refused(worker_a, bad):
    """Refused before any SQL, by type, beside a valid str form that is admitted."""
    good = uuid.uuid4()
    with pytest.raises((ValueError, TypeError)):
        hub_presence.is_online(worker_a, bad)
    with pytest.raises((ValueError, TypeError)):
        hub_presence.delete(worker_a, bad)
    with pytest.raises((ValueError, TypeError)):
        hub_presence.insert(worker_a, bad, good, good)
    with pytest.raises((ValueError, TypeError)):
        hub_presence.upsert_beat(worker_a, [(good, bad)], good)
    assert hub_presence.is_online(worker_a, str(good)) is False, "control: a str uuid is read"


def test_the_worker_token_is_random_per_process_start():
    """Two process starts, two tokens; within one process the token does not change."""
    code = ("import sys; sys.path.insert(0, r'%s'); from apps.wsgw import hub_presence as h; "
            "import uuid; t = h.WORKER_TOKEN; assert isinstance(t, uuid.UUID) and t.version == 4; "
            "from apps.wsgw import hub_presence as h2; assert h2.WORKER_TOKEN == t; print(t)") % ROOT
    tokens = [subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=120, check=True).stdout.strip() for _ in range(2)]
    assert all(len(t) == 36 for t in tokens), tokens
    assert tokens[0] != tokens[1], f"two process starts gave the same token {tokens}"
    assert hub_presence.WORKER_TOKEN == hub_presence.WORKER_TOKEN

# ----------------------------------------- who may reach the table (review F1, the caller half)
#
# `server_internal` is not "server threads only": request handlers enter it too
# (apps/core/rls.py, apps/calls/route.py). So the RLS on hub_connections stops peer-bound,
# owner-bound and contextless connections (tests/test_hub_connections_rls.py), and it does NOT
# stop server code on a request path that entered server_internal. These tests close that
# half in the code: only the store names the table, and only the modules below may reach
# the store's WRITES. The read, `is_online`, is importable by name anywhere (S5's step 9 needs
# it); importing the store MODULE exposes the writes, so that is refused outside the list.

STORE = "apps.wsgw.hub_presence"

#: {module: {function}}: where the store's writes (insert, delete, upsert_beat, prune) may be
#: CALLED. Empty in S2. S3 wires ONE module: the CallBridge, whose `_insert_row` runs the
#: insert after LISTENING, `_delete_row` the delete, `_beat` the beat and the prune on its
#: thread, and `_drain_departed` the delete of a departed socket's row after a beat's write
#: (S3 review F1). The consumer reaches the insert and the delete only through the bridge's
#: `attach` and `detach`, so it is not on the list and cannot import the writes itself.
PRESENCE_WRITE_SITES: dict[str, frozenset[str]] = {
    "apps.wsgw.call_notify": frozenset({"_insert_row", "_delete_row", "_beat",
                                        "_drain_departed"}),
}

#: Modules that may import the store module or its writes.
PRESENCE_WRITERS: frozenset[str] = frozenset(PRESENCE_WRITE_SITES)

#: The store's functions that write.
STORE_WRITES = frozenset({"insert", "delete", "upsert_beat", "prune"})

#: Names any module may import FROM the store. `online_condition` is the online rule as SQL, for a
#: statement that must apply it in its own guard (apps/calls/heartbeat.py `_fail_expired`); it
#: reads, it writes nothing, and `is_online` is built from the same text.
PRESENCE_READS = frozenset({"is_online", "ONLINE_WINDOW_SECS", "BEAT_INTERVAL_SECS",
                            "online_condition"})

#: (module, string) pairs both scans below admit: a MIGRATION FILE STEM named as data, never
#: SQL, never a module path, never an attribute. Added at integration (integ/r10), when
#: fin/w1-deploy's rollback gate met this branch: `apps/core/rollback.py` freezes the files
#: allowed no down script by stem, and two of them are 016's and 018's, whose names contain
#: `hub_connections` and `call_notify`. Exact pairs, not a pattern: the same stem in any other
#: module, or any other string in rollback.py, is still caught (PLANTED and PLANTED_BRIDGE
#: hold both), and test_the_admitted_stem_mentions_are_real_migration_stems keeps each pair
#: an on-disk migration stem that the module really names.
MIGRATION_STEM_MENTIONS: frozenset[tuple[str, str]] = frozenset({
    ("apps.core.rollback", "016_hub_connections_presence"),
    ("apps.core.rollback", "018_skill_call_notify_triggers"),
})


def _presence_uses(source: str, module: str) -> list[str]:
    """Every way `source` (the module `module`) reaches the table or the store's writes.
    Docstrings are prose and are exempt; every other string constant is not, since SQL is
    a string and so is an `importlib` target. Implicitly concatenated literals are one
    constant to the parser, so "hub_" "connections" is caught; a string built at run time
    from pieces is not, and no scan of source text can catch that."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    package = module.split(".")[:-1]       # a module's package; for __init__, the package
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == STORE or alias.name.startswith(STORE + "."):
                    found.append(f"line {node.lineno}: imports the store module ({alias.name})")
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - (node.level - 1)] if node.level else []
            target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            for alias in node.names:
                if target == STORE and alias.name not in PRESENCE_READS:
                    found.append(f"line {node.lineno}: imports {alias.name} from the store")
                elif f"{target}.{alias.name}" == STORE:
                    found.append(f"line {node.lineno}: imports the store module ({target})")
        elif isinstance(node, ast.Attribute) and node.attr == "hub_presence":
            found.append(f"line {node.lineno}: reaches the store as an attribute")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings
              and (module, node.value) not in MIGRATION_STEM_MENTIONS):
            if "hub_connections" in node.value:
                found.append(f"line {node.lineno}: names the table in a string")
            if "hub_presence" in node.value:
                found.append(f"line {node.lineno}: names the store in a string")
    return found


PLANTED = (
    ("apps.calls.route", "from apps.wsgw import hub_presence\n"),
    ("apps.calls.route", "import apps.wsgw.hub_presence as hp\n"),
    ("apps.grants.service", "from apps.wsgw.hub_presence import upsert_beat as _refresh\n"),
    ("apps.grants.service", "from apps.wsgw.hub_presence import is_online, delete\n"),
    ("apps.grants.service", "from apps.wsgw.hub_presence import *\n"),
    ("apps.wsgw.consumer", "from . import hub_presence\n"),
    ("apps.wsgw.consumer", "from .hub_presence import insert\n"),
    ("apps.wsgw.sub.mod", "from ..hub_presence import insert\n"),
    ("apps.meshguard.views", "SQL = 'DELETE FROM hub_connections'\n"),
    ("apps.meshguard.views", "SQL = ('UPDATE hub_' 'connections SET seen_at = now()')\n"),
    ("apps.meshguard.views",
     "def f(c):\n    '''doc'''\n    c.execute(f'DELETE FROM hub_connections WHERE x = {1}')\n"),
    ("apps.x.y", "import importlib\nm = importlib.import_module('apps.wsgw.hub_presence')\n"),
    ("apps.x.y", "import apps.wsgw\napps.wsgw.hub_presence.insert(None, 1, 2, 3)\n"),
    # MIGRATION_STEM_MENTIONS is exact: the stem outside its module, and SQL inside it.
    ("apps.x.y", 'S = frozenset({"016_hub_connections_presence"})\n'),
    ("apps.core.rollback", "SQL = 'DELETE FROM hub_connections'\n"),
    ("apps.core.rollback", 'S = frozenset({"016_hub_connections_presence; DELETE"})\n'),
)

ADMITTED = (
    ("apps.calls.pipeline", "from apps.wsgw.hub_presence import is_online\n"),
    ("apps.calls.pipeline",
     "from apps.wsgw.hub_presence import is_online as online, ONLINE_WINDOW_SECS\n"),
    ("apps.wsgw.__init__", '"""hub_presence.py writes hub_connections: a docstring."""\n'),
    ("apps.x.y", "def f():\n    '''hub_connections, in a function's docstring'''\n"),
    ("apps.wsgw.consumer", "from . import presence, connection_manager\n"),
    ("apps.core.rollback", 'S = frozenset({"016_hub_connections_presence"})\n'),
)


@pytest.mark.parametrize("module,source", PLANTED,
                         ids=[f"planted{i}" for i in range(len(PLANTED))])
def test_the_caller_scan_catches_every_planted_way_in(module, source):
    """The scanner's own positive controls: each planted line reaches the table or the
    writes, by a different route (aliases, relative imports, `import *`, an attribute
    chain, importlib, split literals, an f-string), and each is caught."""
    assert _presence_uses(source, module), f"missed: {source!r} in {module}"


@pytest.mark.parametrize("module,source", ADMITTED,
                         ids=[f"admitted{i}" for i in range(len(ADMITTED))])
def test_the_caller_scan_admits_the_read_and_prose(module, source):
    """Its negative controls: the read by name, the constants, docstrings, and the
    sibling modules of apps.wsgw are not uses."""
    assert _presence_uses(source, module) == [], (module, source)


def _code_files():
    for sub in ("apps", "meshychat", "tools"):
        for p in sorted((ROOT / sub).rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p


def test_only_the_allowlisted_modules_reach_the_presence_table_or_its_writes():
    """Across apps/, meshychat/ and tools/, no module but the store names `hub_connections`
    or reaches the store's writes, except a module in PRESENCE_WRITERS (the CallBridge). The
    control: the same scan over the store itself finds its SQL, so the walk reads the real
    files and the scan fires on real code."""
    uses, scanned = {}, 0
    for p in _code_files():
        module = ".".join(p.relative_to(ROOT).with_suffix("").parts)
        found = _presence_uses(p.read_text("utf-8"), module)
        scanned += 1
        if found:
            uses[module] = found
    assert scanned > 100, f"the walk read {scanned} files"
    assert STORE in uses and any("names the table" in f for f in uses[STORE]), (
        "control: the store's own SQL must be found", uses.get(STORE))
    outside = {m: f for m, f in uses.items() if m != STORE and m not in PRESENCE_WRITERS}
    assert outside == {}, (
        f"modules outside PRESENCE_WRITERS reach hub_connections or the store's writes: "
        f"{outside}. A request path holds server_internal (apps/core/rls.py), so the RLS "
        "does not stop them; add a module to the allowlist only if it is the CallBridge "
        "or the consumer's disconnect (design 4.3)")


def _store_write_call_sites() -> dict[str, set[str]]:
    """{module: {enclosing function}} for every call `hub_presence.<write>(...)`. It reads
    calls through the module name only; an allowlisted module that aliased the store would
    escape it, which the import scan above cannot see either inside an allowlisted module."""
    found: dict[str, set[str]] = {}
    for p in _code_files():
        module = ".".join(p.relative_to(ROOT).with_suffix("").parts)
        tree = ast.parse(p.read_text("utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in STORE_WRITES
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "hub_presence"):
                    found.setdefault(module, set()).add(fn.name)
    return found


def test_the_store_writes_are_called_only_from_the_bridges_named_functions():
    """S3's wiring, narrowed to functions (as tests/test_projection_writer.py narrows the
    writer context): the insert after LISTENING, the delete, and the beat with its prune.
    The control is the scan finding them at all."""
    sites = _store_write_call_sites()
    assert sites, "control: the scan finds the bridge's calls"
    assert {m: frozenset(f) for m, f in sites.items()} == PRESENCE_WRITE_SITES, sites


# ------------------------------- who may reach the bridge's presence writes (S3 review F6)
#
# The scans above keep the store's writes inside the CallBridge. The bridge's own methods are
# then write surfaces too: `CALL_BRIDGE._insert_row(any_id, any_peer, any_token)` from a
# request path writes a routing input under server_internal exactly as the store would, one
# step removed, and the scans above cannot see it (the review planted that line in
# apps/calls/views.py and 41 tests passed). So: only the consumer may hold the bridge (it
# attaches and detaches its own socket), and only the bridge module calls its private writes.

BRIDGE_MODULE = "apps.wsgw.call_notify"

#: Modules that may import the bridge module or hold the bridge: the module itself, and the
#: consumer, whose connect and disconnect call `attach` and `detach`.
BRIDGE_HOLDERS: frozenset[str] = frozenset({BRIDGE_MODULE, "apps.wsgw.consumer"})

#: Names that reach the production bridge or its class.
BRIDGE_NAMES = frozenset({"CALL_BRIDGE", "DEFAULT_CALL_BRIDGE", "CallBridge", "call_bridge"})

#: The bridge's presence writes that only the bridge module calls.
BRIDGE_PRIVATE_WRITES = frozenset({"_insert_row", "_delete_row", "_beat", "_beat_safely",
                                   "_drain_departed"})

#: The bridge's presence writes the consumer calls.
BRIDGE_PUBLIC_WRITES = frozenset({"attach", "detach"})

_PRIVATE_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(sorted(BRIDGE_PRIVATE_WRITES)) + r")(?![A-Za-z0-9_])")


def _bridge_uses(source: str, module: str) -> list[str]:
    """Every way `source` (the module `module`) reaches the bridge's presence writes that
    `module` may not. The bridge module may do anything; the consumer may hold the bridge and
    call `attach` and `detach`, and nothing private; any other module may do none of it.
    Docstrings are prose; every other string is read, since `getattr` and `importlib` take
    strings. A name built at run time from pieces is not seen, as for the store's scan."""
    if module == BRIDGE_MODULE:
        return []
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    package = module.split(".")[:-1]
    holder = module in BRIDGE_HOLDERS
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in BRIDGE_PRIVATE_WRITES:
            found.append(f"line {node.lineno}: calls the bridge's {node.attr}")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings and _PRIVATE_IN_TEXT.search(node.value)):
            found.append(f"line {node.lineno}: names a bridge write in a string")
        if holder:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == BRIDGE_MODULE or alias.name.startswith(BRIDGE_MODULE + "."):
                    found.append(f"line {node.lineno}: imports the bridge module")
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - (node.level - 1)] if node.level else []
            target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            for alias in node.names:
                if target == BRIDGE_MODULE:
                    found.append(f"line {node.lineno}: imports {alias.name} from the bridge")
                elif f"{target}.{alias.name}" == BRIDGE_MODULE:
                    found.append(f"line {node.lineno}: imports the bridge module")
                elif alias.name in BRIDGE_NAMES:
                    found.append(f"line {node.lineno}: imports the bridge as {alias.name}")
        elif isinstance(node, ast.Attribute):
            if node.attr == "call_notify" or node.attr in BRIDGE_NAMES:
                found.append(f"line {node.lineno}: reaches the bridge as .{node.attr}")
            elif (node.attr in BRIDGE_PUBLIC_WRITES
                  and "bridge" in ast.unparse(node.value).lower()):
                found.append(f"line {node.lineno}: calls a bridge's {node.attr}")
        elif isinstance(node, ast.Name) and node.id in BRIDGE_NAMES:
            found.append(f"line {node.lineno}: names the bridge ({node.id})")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings
              and (module, node.value) not in MIGRATION_STEM_MENTIONS
              and ("call_notify" in node.value or "CALL_BRIDGE" in node.value)):
            found.append(f"line {node.lineno}: names the bridge in a string")
    return found


PLANTED_BRIDGE = (
    # The review's R11, verbatim in substance: a request module writes a row for any peer.
    ("apps.calls.views",
     "def _forge(peer_id):\n    from apps.wsgw.call_notify import CALL_BRIDGE\n"
     "    return CALL_BRIDGE._insert_row(uuid.uuid4(), peer_id, uuid.uuid4())\n"),
    ("apps.calls.route", "from apps.wsgw import call_notify\n"),
    ("apps.calls.route", "import apps.wsgw.call_notify as cn\n"),
    ("apps.grants.service", "from apps.wsgw.consumer import DEFAULT_CALL_BRIDGE as b\n"),
    ("apps.grants.service", "from apps.wsgw.call_notify import *\nCALL_BRIDGE.detach(1)\n"),
    ("apps.wsgw.revocation", "from .call_notify import CallBridge\n"),
    ("apps.wsgw.sub.mod", "from ..call_notify import CALL_BRIDGE\n"),
    ("apps.x.y", "def f(c):\n    return getattr(c, '_insert_row')\n"),
    ("apps.x.y", "import importlib\nm = importlib.import_module('apps.wsgw.call_notify')\n"),
    ("apps.x.y", "def f(consumer):\n    return consumer.call_bridge.attach(1, 2)\n"),
    ("apps.x.y", "def f(b):\n    b._drain_departed(None)\n"),
    # The consumer holds the bridge; it may not reach past attach and detach.
    ("apps.wsgw.consumer",
     "async def f(self):\n    await self.call_bridge._delete_row(1)\n"),
    # MIGRATION_STEM_MENTIONS is exact: the stem outside its module, the module path inside it.
    ("apps.x.y", 'S = frozenset({"018_skill_call_notify_triggers"})\n'),
    ("apps.core.rollback",
     "import importlib\nm = importlib.import_module('apps.wsgw.call_notify')\n"),
)

ADMITTED_BRIDGE = (
    ("apps.wsgw.consumer",
     "from .call_notify import CALL_BRIDGE as DEFAULT_CALL_BRIDGE\n"
     "from .call_notify import CallBridge\n"),
    ("apps.wsgw.consumer",
     "async def f(self):\n    await self.call_bridge.attach(1, 2)\n"
     "    await self.call_bridge.detach(1)\n"),
    ("apps.wsgw.call_notify", "def f(self):\n    self._insert_row(1, 2, 3)\n"),
    ("apps.x.y", '"""The CallBridge (apps/wsgw/call_notify.py): CALL_BRIDGE._insert_row."""\n'),
    ("apps.x.y", "def f(sock):\n    sock.detach()\n"),
    ("apps.x.y", "SQL = 'SELECT last_beat FROM mesh_presence'\n"),
    ("apps.core.rollback", 'S = frozenset({"018_skill_call_notify_triggers"})\n'),
)


@pytest.mark.parametrize("module,source", PLANTED_BRIDGE,
                         ids=[f"planted{i}" for i in range(len(PLANTED_BRIDGE))])
def test_the_bridge_scan_catches_every_planted_way_in(module, source):
    """The bridge scan's positive controls, the review's R11 first."""
    assert _bridge_uses(source, module), f"missed: {source!r} in {module}"


@pytest.mark.parametrize("module,source", ADMITTED_BRIDGE,
                         ids=[f"admitted{i}" for i in range(len(ADMITTED_BRIDGE))])
def test_the_bridge_scan_admits_the_consumer_and_prose(module, source):
    """Its negative controls: the consumer's own wiring, the bridge module itself, prose,
    a `detach` on something that is not a bridge, and `last_beat` in SQL."""
    assert _bridge_uses(source, module) == [], (module, source)


def test_the_admitted_stem_mentions_are_real_migration_stems():
    """Each MIGRATION_STEM_MENTIONS pair names a migration file on disk, and its module really
    holds that string as a constant, so the exemption cannot outlive its reason or admit a
    string that is not a stem. The control: without the exemption the scans flag rollback.py."""
    stems = {q.stem for d in ("sql", "sql_meshychat") for q in (ROOT / d).glob("*.sql")}
    for module, text in MIGRATION_STEM_MENTIONS:
        assert text in stems, f"{text!r} is not a migration file stem"
        src = (ROOT / Path(*module.split("."))).with_suffix(".py").read_text("utf-8")
        consts = {n.value for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert text in consts, f"{module} no longer names {text!r}; drop the pair"
    src = (ROOT / "apps" / "core" / "rollback.py").read_text("utf-8")
    renamed = src.replace('"016_hub_connections_presence"', '"016_hub_connections_presence "')
    renamed = renamed.replace('"018_skill_call_notify_triggers"',
                              '"018_skill_call_notify_triggers "')
    assert _presence_uses(renamed, "apps.core.rollback"), "control: the store scan fires"
    assert _bridge_uses(renamed, "apps.core.rollback"), "control: the bridge scan fires"


def test_only_the_consumer_reaches_the_bridges_presence_writes():
    """Across apps/, meshychat/ and tools/: no module outside BRIDGE_HOLDERS reaches the
    bridge, and the consumer reaches only `attach` and `detach`. The control: the consumer's
    own source, scanned as if it were any other module, is caught, so the walk reads the real
    files and the scan fires on real code."""
    uses, scanned = {}, 0
    for p in _code_files():
        module = ".".join(p.relative_to(ROOT).with_suffix("").parts)
        found = _bridge_uses(p.read_text("utf-8"), module)
        scanned += 1
        if found:
            uses[module] = found
    assert scanned > 100, f"the walk read {scanned} files"
    consumer = (ROOT / "apps" / "wsgw" / "consumer.py").read_text("utf-8")
    assert _bridge_uses(consumer, "apps.x.y"), "control: the consumer's wiring must be found"
    assert uses == {}, (
        f"modules reach the CallBridge's presence writes: {uses}. A request path holds "
        "server_internal, so a bridge write from it writes a routing input; only the "
        "consumer may attach and detach its own socket (design 4.3)")
