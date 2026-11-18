"""The step 6b scale harness (tools/ctxbench/bench_ctx_store.py): its pure parts, with no database.

The harness itself runs on HQB's scratch cluster. These pin what decides whether it may run at all (the scratch-only
guard, before any connection) and what its numbers mean (percentile, recall, the load plan and the query classes).

Run:  pytest tests/test_ctx_store_bench.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.ctxbench import bench_ctx_store as B  # noqa: E402

UTC = _dt.timezone.utc


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@127.0.0.1:5432/meshy_django",          # the dev and production database name
    "postgresql://u:p@127.0.0.1:5432/postgres",
    "postgresql://u:p@127.0.0.1:5432/",                      # no database named
    "postgresql://u:p@127.0.0.1:5432/my_bench",              # "bench" not at the start
    "postgresql://u:p@127.0.0.1:5432/Bench_1",               # case matters: only the lower-case prefix
    "dbname=meshy_django host=127.0.0.1",
    "not a dsn ::: at all",
])
def test_a_database_that_is_not_scratch_is_refused(dsn):
    with pytest.raises(B.Refused, match="REFUSED"):
        B.scratch_only(dsn)


@pytest.mark.parametrize("dsn,name", [
    ("postgresql://u:p@10.0.0.5:5432/bench_ctx", "bench_ctx"),
    ("postgresql://u:p@10.0.0.5:5432/scratch", "scratch"),
    ("dbname=scratch_6b host=10.0.0.5", "scratch_6b"),
])
def test_a_scratch_database_is_accepted(dsn, name):
    assert B.scratch_only(dsn) == name


def test_main_refuses_before_any_connection(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("connected")
    monkeypatch.setattr(B, "_connect", boom)
    monkeypatch.setattr(B, "_setup_django", boom)
    monkeypatch.setattr(B, "_machine", lambda: "hqb-scratch")
    with pytest.raises(B.Refused):
        B.main(["--owner-dsn", "postgresql://u:p@h/bench_x", "--app-dsn", "postgresql://u:p@h/meshy_django"])
    with pytest.raises(B.Refused, match="run-id"):
        B.main(["--owner-dsn", "postgresql://u:p@h/bench_x", "--app-dsn", "postgresql://u:p@h/bench_x",
                "--phase", "search"])


def test_percentile_is_nearest_rank():
    values = [float(v) for v in range(1, 21)]           # 1..20
    assert B.percentile(values, 50) == 10.0
    assert B.percentile(values, 95) == 19.0
    assert B.percentile(values, 100) == 20.0
    assert B.percentile([3.0], 95) == 3.0
    assert B.percentile(list(reversed(values)), 95) == 19.0, "order of input does not matter"
    with pytest.raises(ValueError):
        B.percentile([], 50)


def test_recall_counts_overlap_of_the_top_k():
    assert B.recall_at([1, 2, 3], [1, 2, 3], k=3) == 1.0
    assert B.recall_at([1, 9, 8], [1, 2, 3], k=3) == pytest.approx(1 / 3)
    assert B.recall_at([3, 2, 1], [1, 2, 3], k=3) == 1.0, "order inside the top k does not matter"
    assert B.recall_at([1, 2], [1, 2, 3, 4], k=10) == 0.5, "fewer than k found counts against it"
    assert B.recall_at([], [], k=10) == 1.0
    assert B.recall_at(list(range(20)), list(range(10, 30)), k=10) == 0.0, "only the first k of each count"


@pytest.mark.parametrize("n,seats,months", [(15_000_000, 12, 6), (100_000, 12, 6), (1_000, 4, 1), (73_211, 5, 4)])
def test_the_plan_places_exactly_n_rows_with_one_busy_and_one_sparse_seat(n, seats, months):
    p = B.plan(n, seats, months)
    assert p.total == n
    assert len(p.counts) == seats and all(len(row) == months for row in p.counts)
    assert all(c > 0 for row in p.counts for c in row)
    assert p.seat_total(0) == max(p.seat_total(s) for s in range(seats)), "seat 0 is the busy one"
    assert p.seat_total(seats - 1) == min(p.seat_total(s) for s in range(seats)), "the last seat is the sparse one"
    assert abs(p.seat_total(0) / n - 0.40) < 0.01


def test_at_15_million_the_sparse_old_month_takes_the_exact_path_and_the_busy_seat_does_not():
    from apps.meshsem import ctxindex
    p = B.plan(15_000_000, 12, 6)
    assert p.counts[11][5] <= ctxindex.EXACT_MAX, "the sparse seat's oldest month must test route B rule 2"
    assert p.seat_total(0) > ctxindex.EXACT_MAX * 10, "the busy seat must need the sharded path"


def test_the_plan_refuses_a_shape_it_cannot_fill():
    for args in ((10, 2, 6), (1000, 3, 1), (10, 12, 6), (1000, 12, 0)):
        with pytest.raises(ValueError):
            B.plan(*args)


def test_month_start_wraps_the_year():
    now = _dt.datetime(2026, 2, 14, 9, 0, tzinfo=UTC)
    assert B.month_start(now, 0) == _dt.datetime(2026, 2, 1, tzinfo=UTC)
    assert B.month_start(now, 2) == _dt.datetime(2025, 12, 1, tzinfo=UTC)
    assert B.month_start(now, 14) == _dt.datetime(2024, 12, 1, tzinfo=UTC)


def test_the_query_classes_name_seats_and_srcs_the_plan_loads():
    now = _dt.datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    p = B.plan(100_000, 12, 6)
    qcs = {qc.name: qc for qc in B.classes(p, now)}
    assert set(qcs) == {"unfiltered", "busy-seat", "busy-seat-recent", "busy-seat-30d", "sparse-seat-old-month",
                        "one-src"}
    assert qcs["busy-seat-recent"].draw == "searched" and qcs["busy-seat"].draw == "subset"
    assert qcs["busy-seat"].seat == B.seat_name(0)
    old = qcs["sparse-seat-old-month"]
    assert old.seat == B.seat_name(11) and old.since == B.month_start(now, 5) and old.until == B.month_start(now, 4)
    # one-src names month 1's fourth src of the busy seat, which exists when that seat-month holds 4 * 200 rows
    assert qcs["one-src"].src == "m1-s3" and p.counts[0][1] > 3 * B.CHUNKS_PER_SRC


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@192.168.50.20:5432/bench_ctx",          # production's host, even with a scratch name
    "host=192.168.50.20 dbname=scratch",
    "host=10.0.0.5,192.168.50.20 dbname=bench_x",            # a multi-host DSN that includes it
])
def test_production_host_is_refused_even_for_a_scratch_name(dsn):
    with pytest.raises(B.Refused, match="production"):
        B.scratch_only(dsn)


def test_the_harness_refuses_to_run_on_the_production_machine(monkeypatch):
    with pytest.raises(B.Refused, match="serves production"):
        B.off_production("DESKTOP-3K517Q2")
    B.off_production("hqb-scratch")                           # control: another machine runs
    def boom(*a, **kw):
        raise AssertionError("connected")
    monkeypatch.setattr(B, "_connect", boom)
    monkeypatch.setattr(B, "_setup_django", boom)
    monkeypatch.setattr(B, "_machine", lambda: "desktop-3k517q2")
    with pytest.raises(B.Refused, match="serves production"):
        B.main(["--owner-dsn", "postgresql://u:p@127.0.0.1/bench_x", "--app-dsn", "postgresql://u:p@127.0.0.1/bench_x"])


def test_shard_clause_selects_each_searched_shard_by_seat_month_and_id_range():
    sql, args = B.shard_clause([("seat-a", "2026-10", 5, 9), ("seat-b", "2025-12", 1, 2)])
    assert sql.count(" OR ") == 1 and sql.startswith("(") and sql.endswith(")")
    assert args == ["seat-a", _dt.datetime(2026, 10, 1, tzinfo=UTC), _dt.datetime(2026, 11, 1, tzinfo=UTC), 5, 9,
                    "seat-b", _dt.datetime(2025, 12, 1, tzinfo=UTC), _dt.datetime(2026, 1, 1, tzinfo=UTC), 1, 2]
    assert B.shard_clause([]) == ("FALSE", []), "nothing searched selects nothing, not everything"


def _args(**kw):
    import argparse
    base = {"phase": "load", "run_id": None, "vectors": 100_000, "seats": 12, "months": 6, "dims": 768, "seed": 7}
    return argparse.Namespace(**{**base, **kw})


def test_a_load_records_its_shape_from_the_command_line(tmp_path):
    got = B.run_params(_args(vectors=15_000_000, dims=256), tmp_path / "unused.json")
    assert (got["vectors"], got["dims"], got["seats"], got["months"], got["seed"]) == (15_000_000, 256, 12, 6, 7)
    assert set(got) == set(B.RUN_KEYS) and _dt.datetime.fromisoformat(got["now"]).tzinfo is not None


def test_a_later_phase_reads_the_loads_shape_not_todays_defaults(tmp_path):
    import json
    loaded = {"run_id": "abc12345", "vectors": 15_000_000, "seats": 12, "months": 6, "dims": 768, "seed": 9,
              "now": "2026-09-30T23:59:00+00:00"}
    f = tmp_path / "ctxbench-abc12345.json"
    f.write_text(json.dumps(loaded), encoding="utf-8")
    got = B.run_params(_args(phase="search", run_id="abc12345"), f)      # CLI defaults say 100,000 and seed 7
    assert {k: got[k] for k in B.RUN_KEYS} == loaded
    assert got["server"] is None, "a file with no recorded server is carried as None, which main then refuses"


def test_a_later_phase_refuses_a_missing_foreign_or_partial_run_file(tmp_path):
    import json
    f = tmp_path / "ctxbench-abc12345.json"
    with pytest.raises(B.Refused, match="no run file"):
        B.run_params(_args(phase="retention", run_id="abc12345"), f)
    f.write_text(json.dumps({"run_id": "other999", "vectors": 1, "seats": 4, "months": 1, "dims": 8, "seed": 1,
                             "now": "2026-10-01T00:00:00+00:00"}), encoding="utf-8")
    with pytest.raises(B.Refused, match="not this run"):
        B.run_params(_args(phase="search", run_id="abc12345"), f)
    f.write_text(json.dumps({"run_id": "abc12345", "vectors": 1}), encoding="utf-8")
    with pytest.raises(B.Refused, match="not this run"):
        B.run_params(_args(phase="clean", run_id="abc12345"), f)
    with pytest.raises(B.Refused, match="run-id"):
        B.run_params(_args(phase="search"), f)


class _FakeConn:
    def __init__(self, db, addr):
        self.row, self.closed = (db, addr), False

    def execute(self, sql, *a):
        assert "current_database()" in sql and "inet_server_addr()" in sql
        return self

    def fetchone(self):
        return self.row

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("db,addr", [("bench_ctx", "10.0.0.5"), ("scratch", None)])
def test_the_server_check_accepts_a_scratch_database_off_production(db, addr):
    conn = _FakeConn(db, addr)
    assert B.verify_server(conn) == db and not conn.closed


@pytest.mark.parametrize("db,addr", [
    ("bench_ctx", "192.168.50.20"),          # reached production's cluster through hostaddr, PGHOST or a DNS name
    ("meshy_django", "10.0.0.5"),            # a DSN name that lied: the server says which database it is
    ("meshy_django", None),
])
def test_the_server_check_refuses_and_closes_whatever_the_dsn_said(db, addr):
    conn = _FakeConn(db, addr)
    with pytest.raises(B.Refused, match="REFUSED"):
        B.verify_server(conn)
    assert conn.closed, "the refused connection is closed before any read or write"


def test_hostaddr_naming_production_is_refused_in_the_dsn_check_too():
    with pytest.raises(B.Refused, match="production"):
        B.scratch_only("dbname=bench_x host=localhost hostaddr=192.168.50.20")


def test_every_connection_the_harness_opens_is_checked_on_the_server(monkeypatch):
    import psycopg
    conn = _FakeConn("meshy_django", "192.168.50.20")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: conn)
    with pytest.raises(B.Refused):
        B._connect("dbname=bench_x host=10.0.0.5")          # a DSN that passes the cheap gate
    assert conn.closed


def test_a_run_file_binds_to_the_database_its_load_went_into():
    loaded = {"database": "bench_ctx", "address": "10.0.0.5", "port": 5432, "system_identifier": "7301"}
    assert B.same_server(loaded, dict(loaded))
    for key, other in (("database", "bench_other"), ("address", "10.0.0.6"), ("port", 5433),
                       ("system_identifier", "7302")):
        assert not B.same_server(loaded, {**loaded, key: other}), key
    assert not B.same_server({}, loaded), "a run file from before the binding refuses rather than guesses"
    # The app role cannot read pg_control_system(): one side None is the same server when the rest agrees.
    unread = {**loaded, "system_identifier": None}
    assert B.same_server(loaded, unread) and B.same_server(unread, loaded) and B.same_server(unread, dict(unread))
    assert not B.same_server(loaded, {**unread, "port": 5433}), "a missing identifier excuses nothing else"
    # The run file's check keeps what the load recorded: a later phase must read the identifier again and match it.
    assert not B.same_server(loaded, unread, keep_identifier=True), "a recorded identifier cannot go unread later"
    assert B.same_server(unread, loaded, keep_identifier=True), "a load that could not read it binds on the rest"
    assert B.same_server(loaded, dict(loaded), keep_identifier=True)


def _table(main, toast, idx, total, run):
    return {"main_bytes": main, "toast_and_maps_bytes": toast, "indexes": idx, "rows_total": total, "rows_run": run}


def test_the_disk_projection_scales_each_table_by_its_rows_per_vector():
    measured = {"mesh_ctx_vectors": _table(1000, 3000, {"v_pkey": 400}, 100, 100),     # one row per vector
                "ctx_source_gen": _table(50, 0, {"g_pkey": 10}, 1, 1)}                 # one row per 100 vectors
    out = B.project(measured, run_vectors=100, target=10_000)
    v, g = out["tables"]["mesh_ctx_vectors"], out["tables"]["ctx_source_gen"]
    assert v["projected_heap_and_toast_bytes"] == 400_000 and v["projected_index_bytes"] == {"v_pkey": 40_000}
    assert v["projected_bytes"] == 440_000 and v["bytes_per_row"] == 44.0 and v["rows_per_vector"] == 1.0
    assert g["projected_bytes"] == 6_000 and g["rows_per_vector"] == 0.01
    assert out["projected_total_bytes"] == 446_000 and out["warnings"] == []


def test_the_disk_projection_warns_when_other_runs_share_the_table():
    out = B.project({"mesh_ctx_chunks": _table(800, 0, {}, 200, 100)}, run_vectors=100, target=1000)
    # bytes per row is the whole table's (800 / 200 = 4), applied to this run's one row per vector
    assert out["tables"]["mesh_ctx_chunks"]["projected_bytes"] == 4000
    assert len(out["warnings"]) == 1 and "50%" in out["warnings"][0]


def test_the_disk_projection_skips_a_table_this_run_wrote_nothing_to():
    out = B.project({"ctx_chunk_changes": _table(8192, 0, {"c_pkey": 8192}, 3, 0)}, run_vectors=10, target=100)
    assert out["tables"]["ctx_chunk_changes"]["projected_bytes"] is None and out["projected_total_bytes"] == 0


@pytest.mark.parametrize("vectors,target", [(0, 10), (10, 0)])
def test_the_disk_projection_refuses_a_zero_scale(vectors, target):
    with pytest.raises(ValueError):
        B.project({}, run_vectors=vectors, target=target)


def test_the_disk_projection_warns_when_deleted_rows_still_hold_space():
    clean = B.project({"mesh_ctx_vectors": {**_table(800, 0, {}, 100, 100), "dead_tuples": 1}}, 100, 1000)
    assert clean["warnings"] == [], "1 dead tuple in 100 is noise"
    bloated = B.project({"mesh_ctx_vectors": {**_table(800, 0, {}, 100, 100), "dead_tuples": 99}}, 100, 1000)
    assert len(bloated["warnings"]) == 1 and "fresh database" in bloated["warnings"][0]
    # After a VACUUM the dead count is 0, but the files keep the space: the cumulative delete count still says so.
    vacuumed = B.project({"mesh_ctx_vectors": {**_table(800, 0, {}, 100, 100), "dead_tuples": 0,
                                               "toast_deleted_since_stats_reset": 100}}, 100, 1000)
    assert len(vacuumed["warnings"]) == 1 and "fresh database" in vacuumed["warnings"][0]


def test_the_vector_datum_floor_scales_the_stored_vectors_alone():
    m = {**_table(1000, 9000, {}, 100, 100), "vector_datum_bytes_run": 3076 * 100}
    v = B.project({"mesh_ctx_vectors": m}, run_vectors=100, target=10_000)["tables"]["mesh_ctx_vectors"]
    assert v["projected_vector_datum_bytes"] == 3076 * 10_000
    assert v["projected_bytes"] == 1_000_000, "the file-size line is unchanged beside it: 10,000 B / 100 rows x 10,000"


def test_an_update_counters_dead_versions_do_not_warn_but_its_deletes_do():
    counter = {**_table(8192, 0, {}, 1, 1), "dead_tuples": 71}
    assert B.project({"ctx_chunk_changes": counter}, 1, 10)["warnings"] == [], "a fresh load's counter is not bloat"
    assert B.project({"ctx_chunk_changes": {**counter, "deleted_since_stats_reset": 5}}, 1, 10)["warnings"]


@pytest.mark.parametrize("heap_toast,warns", [(430_000, False), (860_000, True)])
def test_vector_files_far_above_the_stored_bytes_warn_even_with_no_statistics(heap_toast, warns):
    # 100 vectors of 3,072 stored bytes: 4,300 B per row is a fresh load (1.40x), 8,600 B a reload (2.8x).
    m = {**_table(heap_toast, 0, {}, 100, 100), "vector_datum_bytes_run": 3072 * 100}
    out = B.project({"mesh_ctx_vectors": m}, 100, 1000)
    assert bool(out["warnings"]) is warns and out["tables"]["mesh_ctx_vectors"]["heap_and_toast_over_datum"] in (1.4, 2.8)


def test_the_vector_overhead_check_skips_vectors_too_small_to_toast():
    # 8 dims: 33 stored bytes under a 24-byte tuple header and the other columns. 3x here says nothing about bloat.
    m = {**_table(10_000, 0, {}, 100, 100), "vector_datum_bytes_run": 33 * 100}
    assert B.project({"mesh_ctx_vectors": m}, 100, 1000)["warnings"] == []


@pytest.mark.parametrize("vectors,seats,months", [(1000, 4, 1), (1000, 4, 2), (4000, 4, 2), (100_000, 12, 6),
                                                  (15_000_000, 12, 6)])
def test_the_one_src_class_names_a_source_the_load_creates(vectors, seats, months):
    p = B.plan(vectors, seats, months)
    src = B.one_src(p)
    month, idx = int(src[1:src.index("-")]), int(src[src.index("-s") + 2:])
    assert p.counts[0][month] > idx * B.CHUNKS_PER_SRC, (src, p.counts[0])     # the load writes chunk idx*200 there
    if p.counts[0][min(1, months - 1)] > 3 * B.CHUNKS_PER_SRC:
        assert src.endswith("-s3"), "at scale the class is unchanged"


def test_the_search_refuses_an_app_dsn_that_reaches_another_database(monkeypatch):
    from apps.core import dbconn

    class _Owner(_FakeConn):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
    owner, app = _Owner("bench_a", "10.0.0.5"), _FakeConn("bench_b", "10.0.0.5")
    monkeypatch.setattr(B, "_connect", lambda *a, **kw: owner)
    monkeypatch.setattr(dbconn, "connect", lambda *a, **kw: app)
    ids = {id(owner): {"database": "bench_a", "address": "10.0.0.5", "port": 5432, "system_identifier": "1"},
           id(app): {"database": "bench_b", "address": "10.0.0.5", "port": 5432, "system_identifier": "1"}}
    monkeypatch.setattr(B, "server_identity", lambda c: ids[id(c)])
    with pytest.raises(B.Refused, match="another|reaches"):
        B.search("dbname=bench_a", "dbname=bench_b", "s", "ns", 8, B.plan(4000, 4, 2), 1, 7,
                 _dt.datetime(2026, 10, 25, tzinfo=_dt.timezone.utc))
    assert app.closed, "the refused app connection is closed before any search"


@pytest.mark.parametrize("vectors,seats,months", [(1000, 12, 6), (100, 4, 1), (1199, 12, 6)])
def test_the_plan_refuses_a_shape_that_leaves_a_seat_month_empty(vectors, seats, months):
    with pytest.raises(ValueError, match="no rows"):
        B.plan(vectors, seats, months)


@pytest.mark.parametrize("vectors,seats,months", [(1200, 12, 6), (1000, 4, 1), (4000, 4, 2), (15_000_000, 12, 6)])
def test_every_seat_month_of_an_accepted_plan_has_a_row(vectors, seats, months):
    p = B.plan(vectors, seats, months)
    assert all(c > 0 for row in p.counts for c in row), p.counts


# ---- the plans the 6b rerun records (CG-R3 on #436: a bitmap scan read 40 entries for 11 rows) -----------------------

def test_chunk_scans_summarises_the_chunk_nodes_and_flags_a_bitmap_scan():
    plan = {"Node Type": "Aggregate", "Plans": [{"Node Type": "Limit", "Plans": [
        {"Node Type": "Bitmap Heap Scan", "Relation Name": "mesh_ctx_chunks", "Actual Rows": 11, "Actual Loops": 1,
         "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "mesh_ctx_chunks_seat_src_gen", "Actual Rows": 40,
                    "Actual Loops": 1}]},
        {"Node Type": "Index Scan", "Relation Name": "ctx_source_gen", "Actual Rows": 1, "Actual Loops": 11}]}]}
    got = B.chunk_scans(plan)
    assert got["bitmap"] is True and got["chunk_rows_read"] == 51
    assert got["nodes"] == ["Bitmap Heap Scan mesh_ctx_chunks read=11 loops=1",
                            "Bitmap Index Scan mesh_ctx_chunks_seat_src_gen read=40 loops=1"], "ctx_source_gen is not chunks"
    plain = {"Node Type": "Limit", "Plans": [{"Node Type": "Index Scan", "Relation Name": "mesh_ctx_chunks",
                                             "Index Name": "mesh_ctx_chunks_current_space_seat_time",
                                             "Actual Rows": 11, "Actual Loops": 1}]}
    assert B.chunk_scans(plain)["bitmap"] is False, "control: a plain index scan is not flagged"


def test_every_statement_label_still_matches_the_search_it_names():
    # The labels are substrings of ctxindex's SQL: a reworded statement must fail here, not report as "other".
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "apps" / "meshsem" / "ctxindex.py").read_text(encoding="utf-8")
    for name, needle in B.STATEMENT_LABELS:
        if name == "shard_plan" and "ctx_shard_plan" not in src:
            continue                                  # the stored plan arrives with sql_meshychat/053 (#436)
        assert needle in src, (name, needle)
    assert B.label_of("SELECT 1") == "other"


def test_a_failed_explain_is_recorded_for_its_statement_and_the_run_goes_on(monkeypatch):
    # memory-bxxxxx a8d5a1: at 15M an EXPLAIN (ANALYZE) can hit the statement timeout; it must not end the run.
    import contextlib
    import psycopg
    from apps.core import rls
    from apps.meshsem import ctxindex
    count_sql = "SELECT count(*) AS n FROM (SELECT 1 FROM mesh_ctx_chunks c LIMIT 5) s"
    load_sql = "SELECT c.id FROM mesh_ctx_chunks c WHERE c.id BETWEEN %s AND %s AND c.current ORDER BY c.id"
    plan = [{"Plan": {"Node Type": "Index Scan", "Relation Name": "mesh_ctx_chunks", "Actual Rows": 3,
                      "Actual Loops": 1}}]

    class Cur:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.conn.log.append(sql)
            if sql.startswith("EXPLAIN") and "count(*)" in sql:
                raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

        def fetchone(self):
            return {"QUERY PLAN": plan}

    class Conn:
        def __init__(self):
            self.log, self.rolled_back = [], 0

        def cursor(self, *a, **kw):
            return Cur(self)

        def commit(self):
            pass

        def rollback(self):
            self.rolled_back += 1

    def fake_search(conn, sp, ns, q, f, k=50):
        for sql in (count_sql, load_sql):
            with conn.cursor() as cur:
                cur.execute(sql, (1, 2) if "%s" in sql else None)
        return [], None

    monkeypatch.setattr(rls, "server_internal", lambda conn: contextlib.nullcontext())
    monkeypatch.setattr(ctxindex, "search", fake_search)
    app = Conn()
    got = B.search_plans(app, None, "ns", None, None, 10)
    assert got[0] == {"statement": "subset_count",
                      "error": "QueryCanceled: canceling statement due to statement timeout"}
    assert got[1]["statement"] == "shard_load" and got[1]["nodes"] and "error" not in got[1], "the run went on"
    assert app.rolled_back == 1
