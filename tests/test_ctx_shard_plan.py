"""sql_meshychat/053: the stored shard plan, kept equal to ctxindex's aggregate by statement triggers (step 6b).

What this file holds, each part against a control:
  * NO DRIFT (memory-bxxxxx d00d7c point 3): after every step of a mixed sequence (single and multi-row inserts in two
    months and two seats, a gen flip, an old-gen manifest that flips its own rows, a gen delete, a retention-style
    delete), ctx_shard_plan equals the aggregate row for row, for this namespace and for a second one in the table.
  * A WRITER WHOSE IDS ARE NOT THE NEWEST: rows with lower ids than the planned ones recompute their group, not append.
  * TRUNCATE empties the plan (pxx f6f471 point 2), in a transaction that rolls back.
  * THE BENCH HARNESS'S OWN LOAD (pxx point 1): tools/ctxbench writes chunks directly, and the plan still matches.
  * NO SCAN ON READ (pxx point 4): reading the stored plan moves mesh_ctx_chunks' tuple counters by zero, with a red
    control: the aggregate moves them under the same probe.
  * THE DOWN SCRIPT names every object 053 creates.
These need Postgres (the HQB gate). Run:  pytest tests/test_ctx_shard_plan.py -q -p no:cacheprovider
"""
from __future__ import annotations

import os
import re
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.meshsem import ctxindex  # noqa: E402
from tests.test_ctx_vector_store import DIMS, DSN, NOW, OLD, ROOT, _add, owner, world  # noqa: E402,F401 (fixtures)

UP = ROOT / "sql_meshychat" / "053_ctx_shard_plan.sql"
DOWN = ROOT / "sql_meshychat" / "down" / "053_ctx_shard_plan_down.sql"
SMALL = 3


def _set_shard_max(conn, n: int) -> None:
    conn.execute("UPDATE ctx_shard_setting SET shard_max = %s WHERE one", (n,))
    conn.execute("SELECT public.meshy_ctx_shard_plan_rebuild()")


@pytest.fixture()
def small(world, monkeypatch):
    """Shards of SMALL rows, cut by the triggers and expected by ctxindex alike."""
    _set_shard_max(world["owner"], SMALL)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", SMALL)
    ctxindex.forget()
    yield world
    _set_shard_max(world["owner"], 50000)
    ctxindex.forget()


def _stored(conn, space, ns):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET app.server_internal = '1'")
        return ctxindex._plan_rows(cur, space, ns)


def _aggregate(conn, space, ns):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET app.server_internal = '1'")
        return ctxindex._plan_rows_aggregate(cur, space, ns)


def _same(w, *namespaces):
    for ns in namespaces or (w["ns"],):
        stored, agg = _stored(w["owner"], w["space"], ns), _aggregate(w["owner"], w["space"], ns)
        assert stored == agg, (ns, stored, agg)


def _copy_rows(w, n: int, seat: str, when) -> None:
    """ONE INSERT statement of n chunks (copies of existing rows under new chunk numbers, seat and time): the trigger
    sees them in one transition table, as it sees a COPY. Chunk numbers continue above the space's highest, so two
    calls never collide on (src_id, space, chunk_no) (pxx 8b35b0 P2-1)."""
    w["owner"].execute(
        "INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, first_ord, last_ord, "
        "char_start, char_end, time_first, time_last, text_sha256) SELECT src_id, space, namespace, "
        "(SELECT max(m.chunk_no) FROM mesh_ctx_chunks m WHERE m.space = %s) + (row_number() OVER (ORDER BY id))::int, "
        "%s, src, gen, batch, first_ord, last_ord, char_start, char_end, %s, %s, text_sha256 FROM mesh_ctx_chunks "
        "WHERE space = %s AND namespace = %s ORDER BY id LIMIT %s",
        (w["space"].name, seat, when, when, w["space"].name, w["ns"], n))
    # The fixture's own counter numbers _add's next chunk: move it past what this took (pxx be5b21 P2-1).
    w["n"] = w["owner"].execute("SELECT max(chunk_no) FROM mesh_ctx_chunks WHERE space = %s",
                                (w["space"].name,)).fetchone()[0]


def test_the_stored_plan_equals_the_aggregate_after_a_mixed_sequence(small):
    """The drift proof over one connection's writes: single and multi-row inserts, flips, deletes, a month change.
    It has no concurrency and no TRUNCATE, so the advisory-lock and TRUNCATE mutants are killed by the two-connection,
    reverse-order and TRUNCATE tests below, not by this one (pxx be5b21)."""
    w = small
    other = f"shared/ctxplan-{uuid.uuid4().hex[:8]}"
    for i in range(7):
        _add(w, seat="seat-a", when=NOW)
    _copy_rows(w, 5, "seat-b", NOW)                      # one statement: 5 rows, two shards of seat-b
    _copy_rows(w, 4, "seat-a", OLD)                      # one statement into an older month
    for _ in range(4):
        _add(w, seat="seat-a", when=NOW, ns=other)
    _same(w, w["ns"], other)
    assert len(_stored(w["owner"], w["space"], w["ns"])) >= 3, "control: the sequence spans several shards"

    # A gen flip: ctxjob's UPDATE of current for one source (src s1 holds every row above).
    for _ in range(4):
        _add(w, seat="seat-a", src="s2", when=NOW, gen=2)
    w["owner"].execute("UPDATE mesh_ctx_chunks SET current = false WHERE space = %s AND namespace = %s AND seat = %s "
                       "AND src = 's2' AND gen < 3 AND current", (w["space"].name, w["ns"], "seat-a"))
    _same(w, w["ns"], other)

    # An old-gen manifest written after a newer one: its rows go in current, then are flipped in the same transaction.
    with w["owner"].transaction():
        for _ in range(2):
            _add(w, seat="seat-b", src="s3", when=NOW, gen=1)
        w["owner"].execute("UPDATE mesh_ctx_chunks SET current = false WHERE space = %s AND src = 's3'",
                           (w["space"].name,))
    _same(w, w["ns"], other)

    # The gen delete (ctxjob's replace of one batch) and a retention-style delete of the old month.
    w["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE space = %s AND namespace = %s AND seat = 'seat-b' "
                       "AND src = 's1'", (w["space"].name, w["ns"]))
    _same(w, w["ns"], other)
    w["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE id IN (SELECT id FROM mesh_ctx_chunks WHERE space = %s "
                       "AND time_last < %s LIMIT 2)", (w["space"].name, NOW.replace(day=1)))
    _same(w, w["ns"], other)
    # A current row whose month changes (an UPDATE of time_last) moves between groups.
    w["owner"].execute("UPDATE mesh_ctx_chunks SET time_last = %s WHERE id = (SELECT max(id) FROM mesh_ctx_chunks "
                       "WHERE space = %s AND namespace = %s AND current)", (OLD, w["space"].name, w["ns"]))
    _same(w, w["ns"], other)


def test_rows_with_lower_ids_than_the_planned_ones_recompute_their_group(small):
    w = small
    first = _add(w)
    # A concurrent writer that committed higher ids first: simulated with an explicit id far above the sequence.
    high = first + 1000
    w["owner"].execute(
        "INSERT INTO mesh_ctx_chunks (id, src_id, space, namespace, chunk_no, seat, src, gen, batch, first_ord, "
        "last_ord, char_start, char_end, time_first, time_last, text_sha256) SELECT %s, src_id, space, namespace, "
        "chunk_no + 1000, seat, src, gen, batch, first_ord, last_ord, char_start, char_end, time_first, time_last, "
        "text_sha256 FROM mesh_ctx_chunks WHERE id = %s", (high, first))
    for _ in range(4):
        _add(w)                                          # ids below `high`: an append would put them after it
    _same(w)
    assert _stored(w["owner"], w["space"], w["ns"])[0][3] == high, "control: the newest shard ends at the high id"


def test_truncate_empties_the_plan(small):
    w = small
    _add(w)
    with psycopg.connect(DSN) as tx:
        tx.execute("SET app.server_internal = '1'")
        tx.execute("TRUNCATE mesh_ctx_chunks CASCADE")
        assert tx.execute("SELECT count(*) FROM ctx_shard_plan").fetchone()[0] == 0
        tx.rollback()
    assert _stored(w["owner"], w["space"], w["ns"]), "control: the rollback kept the plan"


def test_the_bench_harness_load_keeps_the_plan_equal(owner, monkeypatch):
    from tools.ctxbench import bench_ctx_store as B
    monkeypatch.setattr(B, "COMMIT_EVERY", 500)
    monkeypatch.setattr(B, "SCRATCH_DB", re.compile(r".+"))
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 100)
    _set_shard_max(owner, 100)
    run_id = uuid.uuid4().hex[:8]
    space_name, ns = f"bench-{DIMS}/{DIMS}/ctxplan-{run_id}", f"shared/ctxplan-{run_id}"
    try:
        B.load(DSN, space_name, ns, DIMS, B.plan(2000, 4, 2), 7, NOW)
        space = type("S", (), {"name": space_name, "dims": DIMS})()
        stored = _stored(owner, space, ns)
        assert len(stored) > 4 and stored == _aggregate(owner, space, ns)
    finally:
        B.clean(DSN, space_name, ns)
        _set_shard_max(owner, 50000)
        ctxindex.forget()


def _tuples_read(conn) -> int:
    conn.execute("SELECT pg_stat_force_next_flush()")
    conn.execute("SELECT pg_stat_clear_snapshot()")
    row = conn.execute("SELECT coalesce(seq_tup_read, 0) + coalesce(idx_tup_fetch, 0) FROM pg_stat_user_tables "
                       "WHERE relname = 'mesh_ctx_chunks'").fetchone()
    return int(row[0])


def test_reading_the_stored_plan_reads_no_chunk(world):
    w = world
    other = f"shared/ctxplan-{uuid.uuid4().hex[:8]}"
    for _ in range(5):
        _add(w)
        _add(w, ns=other)
    before = _tuples_read(w["owner"])
    _aggregate(w["owner"], w["space"], w["ns"])
    assert _tuples_read(w["owner"]) > before, "red control: the aggregate reads chunks under this probe"
    before = _tuples_read(w["owner"])
    assert _stored(w["owner"], w["space"], w["ns"])
    assert _tuples_read(w["owner"]) == before, "the stored plan must not read mesh_ctx_chunks"


def test_the_down_script_names_every_object_053_creates():
    up = UP.read_text(encoding="utf-8")
    down = DOWN.read_text(encoding="utf-8")
    made = re.findall(r"CREATE (?:OR REPLACE )?(TABLE|FUNCTION|TRIGGER)(?: IF NOT EXISTS)? (?:public\.)?(\w+)", up)
    assert len(made) == 16, ("control: the scan finds 053's objects", made)
    for kind, name in made:
        assert re.search(rf"DROP {kind} IF EXISTS (?:public\.)?{name}\b", down), f"down/053 leaves {kind} {name}"


def test_a_mismatched_shard_size_plans_nothing_in_production_and_never_runs_the_aggregate(world, monkeypatch):
    # pxx b80f02 point 1, cfdf14 point 2: the aggregate is the statement that passed 30 s at 15M rows.
    w = world
    for _ in range(3):
        _add(w)
    monkeypatch.setattr(ctxindex, "PLAN_AGGREGATE_FALLBACK", False)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 7)                  # the setting row says 50000
    monkeypatch.setattr(ctxindex, "_plan_rows_aggregate", lambda *a, **kw: pytest.fail("aggregate in the search path"))
    assert _stored(w["owner"], w["space"], w["ns"]) == []
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    _hits, cov = ctxindex.search(w["owner"], w["space"], w["ns"], [1.0] + [0.0] * (DIMS - 1), k=3)
    assert cov.truncated and cov.scored == 0, cov.as_dict()


def test_the_drift_check_finds_and_repairs_an_insert_the_triggers_missed(small):
    # pxx b80f02 point 2: a missed insert leaves no shard short, and past _subset_cap() the search cannot see it.
    w = small
    for _ in range(4):
        _add(w)
    # Each statement in a transaction of its own (the owner connection autocommits): re-enabling the trigger in the
    # insert's transaction raised ObjectInUse while 049's deferred counter events were pending (pxx 8b35b0 P2-2).
    w["owner"].execute("ALTER TABLE mesh_ctx_chunks DISABLE TRIGGER mesh_ctx_chunks_plan_insert")
    try:
        missed = _add(w)
    finally:
        w["owner"].execute("ALTER TABLE mesh_ctx_chunks ENABLE TRIGGER mesh_ctx_chunks_plan_insert")
    assert _stored(w["owner"], w["space"], w["ns"]) != _aggregate(w["owner"], w["space"], w["ns"]), \
        "red control: the raw insert is outside every stored range"
    with w["owner"].transaction():
        drift = ctxindex.check_shard_plan(w["owner"])
    assert any(d[0] == w["space"].name and d[1] == w["ns"] for d in drift), drift
    _same(w)
    assert any(lo <= missed <= hi for _s, _m, lo, hi, _n in _stored(w["owner"], w["space"], w["ns"]))
    with w["owner"].transaction():
        assert [d for d in ctxindex.check_shard_plan(w["owner"]) if d[0] == w["space"].name] == [], "repaired"


def test_a_lower_id_committing_after_a_higher_one_is_recomputed_not_appended(small):
    # pxx cfdf14 point 1: ids are allocated in order, but transactions commit in any order.
    w = small
    for _ in range(2):
        _add(w)
    seq = w["owner"].execute("SELECT pg_get_serial_sequence('mesh_ctx_chunks', 'id')").fetchone()[0]
    early = w["owner"].execute("SELECT nextval(%s)", (seq,)).fetchone()[0]     # T1 holds the lower id
    for _ in range(3):
        _add(w)                                                               # T2's higher ids commit first
    _same(w)
    w["owner"].execute(
        "INSERT INTO mesh_ctx_chunks (id, src_id, space, namespace, chunk_no, seat, src, gen, batch, first_ord, "
        "last_ord, char_start, char_end, time_first, time_last, text_sha256) SELECT %s, src_id, space, namespace, "
        "chunk_no + 5000, seat, src, gen, batch, first_ord, last_ord, char_start, char_end, time_first, time_last, "
        "text_sha256 FROM mesh_ctx_chunks WHERE space = %s ORDER BY id LIMIT 1", (early, w["space"].name))
    _same(w)


def test_two_writers_on_one_seat_month_leave_the_plan_equal(small):
    # Two connections, the first holding its transaction open across a shard boundary: the second's trigger waits on
    # the space lock, then sees the first's committed shard.
    import threading
    w = small
    for _ in range(2):
        _add(w)
    first = psycopg.connect(DSN)
    second = psycopg.connect(DSN)
    try:
        _add(dict(w, owner=first))                       # fills shard 0 (3 rows), uncommitted
        _add(dict(w, owner=first, n=w["n"] + 1000))      # opens shard 1, uncommitted
        done = threading.Event()

        def other():
            _add(dict(w, owner=second, n=w["n"] + 2000))
            second.commit()
            done.set()
        t = threading.Thread(target=other)
        t.start()
        assert not done.wait(1.0), "the second writer must wait for the first's space lock"
        first.commit()
        t.join(30)
        assert done.is_set()
        _same(w)
    finally:
        first.close()
        second.close()


def test_a_recompute_in_a_non_utc_session_keeps_the_utc_month(small):
    # pxx 21a658 P2-1: `mo + interval '1 month'` in a Europe/Vienna session ends October at 01:00 UTC on 1 Nov (the
    # DST change), so October's recompute took November's first hour too.
    import datetime as dt
    w = small
    w["owner"].execute("SET TimeZone = 'Europe/Vienna'")
    try:
        oct_end = dt.datetime(2026, 10, 31, 23, 30, tzinfo=dt.timezone.utc)
        nov_start = dt.datetime(2026, 11, 1, 0, 30, tzinfo=dt.timezone.utc)
        doomed = _add(w, when=oct_end)
        _add(w, when=oct_end)
        _add(w, when=nov_start)
        w["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE id = %s", (doomed,))      # recomputes October
        _same(w)
        months = sorted({m.strftime("%Y-%m") for _s, m, _lo, _hi, _n in _stored(w["owner"], w["space"], w["ns"])})
        assert months == ["2026-10", "2026-11"], months
    finally:
        w["owner"].execute("SET TimeZone = 'UTC'")


def test_retention_takes_the_space_lock_before_it_deletes_a_row(small):
    # pxx 21a658 P2-2: retention used to lock rows and meet the space lock only in the trigger, which could deadlock
    # with ctxjob (lock, then rows). It now waits for the lock first, with no row of the space locked.
    import threading
    import datetime as dt
    from apps.core import dbconn
    w = small
    old = _add(w, when=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200))
    _add(w)
    holder = psycopg.connect(DSN)
    holder.execute("SELECT pg_advisory_xact_lock(1835233134, hashtext(%s))", (w["space"].name,))
    done = threading.Event()

    class Job:
        max_batch = 100

    def run():
        app = dbconn.connect(DSN)
        try:
            ctxindex.run_retention_job(app, Job())
        finally:
            app.close()
            done.set()
    t = threading.Thread(target=run)
    t.start()
    try:
        assert not done.wait(1.5), "retention ran while ctxjob held the space lock"
        locked = w["owner"].execute("SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                                    "WHERE c.relname = 'mesh_ctx_chunks' AND l.mode = 'RowExclusiveLock' "
                                    "AND l.pid <> pg_backend_pid() AND l.pid <> %s",
                                    (holder.info.backend_pid,)).fetchone()[0]
        assert locked == 0, "retention took row locks before the space lock"
    finally:
        holder.rollback()
        holder.close()
    t.join(30)
    assert done.is_set()
    assert w["owner"].execute("SELECT count(*) FROM mesh_ctx_chunks WHERE id = %s", (old,)).fetchone()[0] == 0
    _same(w)


def test_two_connections_committing_in_reverse_id_order_leave_the_plan_equal(small):
    # pxx 8b35b0: the lower id and the overlapping writer together. T1 reserves the lower id and has not yet inserted;
    # T2, on another connection, inserts higher ids and commits first; then T1 inserts its lower id and commits.
    w = small
    for _ in range(2):
        _add(w)
    seq = w["owner"].execute("SELECT pg_get_serial_sequence('mesh_ctx_chunks', 'id')").fetchone()[0]
    t1 = psycopg.connect(DSN)
    t2 = psycopg.connect(DSN)
    try:
        low = t1.execute("SELECT nextval(%s)", (seq,)).fetchone()[0]
        for k in range(3):
            _add(dict(w, owner=t2, n=w["n"] + 3000 + k))
        t2.commit()
        t1.execute(
            "INSERT INTO mesh_ctx_chunks (id, src_id, space, namespace, chunk_no, seat, src, gen, batch, first_ord, "
            "last_ord, char_start, char_end, time_first, time_last, text_sha256) SELECT %s, src_id, space, namespace, "
            "chunk_no + 7000, seat, src, gen, batch, first_ord, last_ord, char_start, char_end, time_first, time_last, "
            "text_sha256 FROM mesh_ctx_chunks WHERE space = %s ORDER BY id LIMIT 1", (low, w["space"].name))
        t1.commit()
        assert max(r[0] for r in w["owner"].execute("SELECT id FROM mesh_ctx_chunks WHERE space = %s",
                                                    (w["space"].name,)).fetchall()) > low, "control: T2's ids are higher"
        _same(w)
    finally:
        t1.close()
        t2.close()


# ---- GitHub Codex on 01a0f9aa (pxx d5d7e9) -------------------------------------------------------------------------

def test_a_mid_month_value_passed_to_the_helper_is_taken_as_its_month(small):
    # P2-2: meshy_ctx_shard_plan_groups is callable by every role; a mid-month timestamp wrote a group under a key no
    # chunk maps to. The helper now normalises it, so the plan stays equal to the aggregate.
    import datetime as dt
    w = small
    for _ in range(4):
        _add(w)
    mid = NOW.replace(day=15, hour=12, minute=0, second=0)
    w["owner"].execute("SELECT meshy_ctx_shard_plan_groups(%s, %s, %s, %s)",
                       ([w["space"].name], [w["ns"]], ["seat-a"], [mid]))
    months = {m for _s, m, _lo, _hi, _n in _stored(w["owner"], w["space"], w["ns"])}
    assert all(m.day == 1 and m.hour == 0 for m in months), months
    _same(w)
    assert dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc) < mid, "control: a real mid-month value"


def test_the_drift_check_is_its_own_job_with_a_lease_that_outlives_its_scan():
    # P2-3: inside retention's 30 s job, a 600 s scan could outlive the 95 s lease and be taken over mid-scan.
    from apps.jobs import registry
    job = registry.by_name("ctx_shard_plan_check")
    assert job.run is ctxindex.run_shard_plan_check_job and job.own_lane
    assert job.timeout_secs == ctxindex.PLAN_CHECK_TIMEOUT_S and job.lease_secs > job.timeout_secs
    import inspect
    assert "check_shard_plan" not in inspect.getsource(ctxindex.run_retention_job), "retention no longer runs it"


def test_the_rebuild_holds_the_table_lock_so_no_writer_runs_beside_it(small):
    # P2-4: run beside ingestion, the rebuild raced the triggers' appends. It now takes SHARE ROW EXCLUSIVE itself.
    w = small
    _add(w)
    rebuilding = psycopg.connect(DSN)
    try:
        rebuilding.execute("SELECT public.meshy_ctx_shard_plan_rebuild()")       # transaction left open
        held = w["owner"].execute(
            "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE c.relname = 'mesh_ctx_chunks' "
            "AND l.mode = 'ShareRowExclusiveLock' AND l.granted AND l.pid = %s",
            (rebuilding.info.backend_pid,)).fetchone()[0]
        assert held == 1, "the rebuild must hold the table lock until it commits"
    finally:
        rebuilding.rollback()
        rebuilding.close()
