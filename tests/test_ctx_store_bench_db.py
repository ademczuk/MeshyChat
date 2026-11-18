"""The step 6b scale harness (tools/ctxbench/bench_ctx_store.py) end to end on this run's Postgres, at a few thousand rows.

Every phase runs against the real schema: the load goes through 047's UNIQUE (src_id, space, chunk_no) (pxx's AY-4 on
#410), the exact reference opens its named cursor on the autocommit owner connection (AY-5), and the search phase's
numbers are pinned so a harness that reports what it did not measure goes red: a fixed duration (the clock is driven
here) and truth taken from what the search found (coverage recall must fall below scoring recall when shards are
skipped). Route B's constants are shrunk so the sharded path appears on a few thousand rows.

Run:  pytest tests/test_ctx_store_bench_db.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import itertools
import os
import re
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.meshsem import ctxindex  # noqa: E402
from tools.ctxbench import bench_ctx_store as B  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
UTC = _dt.timezone.utc
DIMS = 8
QUERIES = 3


def _cycle_clock():
    """A clock whose timed spans last 1, 1, 3, 1, 1, 3 ... seconds: a span is two consecutive reads."""
    state = {"t": 0.0, "reads": 0}
    durations = itertools.cycle((1.0, 1.0, 3.0))

    def clock() -> float:
        state["reads"] += 1
        if state["reads"] % 2 == 0:
            state["t"] += next(durations)
        return state["t"]
    return clock


@pytest.fixture()
def run(monkeypatch):
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 60)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 100)
    monkeypatch.setattr(ctxindex, "PLAN_AGGREGATE_FALLBACK", True)      # SHARD_MAX differs from the stored plan's size
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 2)
    monkeypatch.setattr(B, "COMMIT_EVERY", 500)
    # The conftest's per-run database is not named bench*: widen only the NAME rule; the server-address check stays.
    monkeypatch.setattr(B, "SCRATCH_DB", re.compile(r".+"))
    ctxindex.forget()
    run_id = uuid.uuid4().hex[:8]
    r = {"space": f"bench-{DIMS}/{DIMS}/ctxbench-{run_id}", "ns": f"shared/ctxbench-{run_id}",
         "now": _dt.datetime.now(UTC).replace(microsecond=0), "plan": B.plan(4000, 4, 2)}   # the busy seat-month holds > 4 * 200 rows, so m1-s3 exists
    yield r
    B.clean(DSN, r["space"], r["ns"])
    ctxindex.forget()


def test_every_phase_runs_on_the_real_schema_and_reports_what_it_measured(run, monkeypatch):
    loaded = B.load(DSN, run["space"], run["ns"], DIMS, run["plan"], 7, run["now"])
    assert loaded["rows"] == 4000, "the load went through 047's unique key"

    sized = B.disk(DSN, run["space"], 4000, target=40_000)
    m, proj = sized["measured"], sized["projection"]
    assert set(m) == set(B.DISK_TABLES)
    assert m["mesh_ctx_chunks"]["rows_run"] == m["mesh_ctx_vectors"]["rows_run"] == 4000, "one chunk, one vector each"
    assert m["ctx_source_gen"]["rows_run"] == loaded["sources"]
    assert m["mesh_ctx_chunks"]["indexes"] and all(v > 0 for v in m["mesh_ctx_chunks"]["indexes"].values())
    assert m["mesh_ctx_vectors"]["main_bytes"] > 0 and proj["projected_total_bytes"] > 0
    assert proj["tables"]["mesh_ctx_vectors"]["rows_per_vector"] == 1.0
    assert m["mesh_ctx_vectors"]["vector_datum_bytes_run"] >= 4000 * DIMS * 4, "every vector's stored bytes counted"
    assert all("dead_tuples" in m[name] for name in B.DISK_TABLES)

    monkeypatch.setattr(B, "_clock", _cycle_clock())
    out = B.search(DSN, DSN, run["space"], run["ns"], DIMS, run["plan"], QUERIES, 7, run["now"])
    by = {c["class"]: c for c in out["classes"]}
    assert set(by) == {qc.name for qc in B.classes(run["plan"], run["now"])}
    for c in by.values():
        for mode in ("warm_seq", "plan_stale", "cold", "after_write"):
            # Spans run 1, 1, 3, 1, 1, 3 ...: any QUERIES (3) consecutive spans sort to [1, 1, 3].
            assert (c[f"vector_arm_{mode}_p50_s"], c[f"vector_arm_{mode}_p95_s"]) == (1.0, 3.0), \
                (c["class"], mode, "a duration the clock did not give")
        assert c["recall_at_10_scoring_mean"] >= 0.95, (c["class"], "scoring inside what was searched is exact")
        assert len(c["coverage_per_query"]) == QUERIES

    unfiltered = by["unfiltered"]
    assert unfiltered["paths"] == ["sharded"] and unfiltered["queries_truncated"] == QUERIES
    assert unfiltered["recall_at_10_coverage_mean"] < unfiltered["recall_at_10_scoring_mean"], \
        "coverage recall counts the skipped shards; it is not the found set scored against itself"
    assert by["sparse-seat-old-month"]["paths"] == ["exact"], "a small filtered set takes rule 2"
    assert by["busy-seat"]["write_in_searched"] == QUERIES, "each write lands in a shard the next query searches"
    for c in by.values():
        if "skipped" not in c:
            assert c["plans"], (c["class"], "every searched class records the plans of the chunk statements it ran")
            assert all({"statement", "nodes", "chunk_rows_read", "bitmap"} <= set(p) or set(p) == {"statement", "error"}
                       for p in c["plans"]), c["class"]
            assert any(p.get("nodes") for p in c["plans"]), (c["class"], "a chunk scan node is summarised")

    # A second search on the same run, with a search that drops each query's best hit: scoring recall must see the
    # miss (it is not the found set scored against itself), and the repeat's after_write rows must not collide with
    # the first search's (pxx df9d81: the write counter continues).
    real = ctxindex.search

    def worse(*a, **kw):
        hits, cov = real(*a, **kw)
        return hits[1:], cov
    monkeypatch.setattr(ctxindex, "search", worse)
    again = B.search(DSN, DSN, run["space"], run["ns"], DIMS, run["plan"], QUERIES, 8, run["now"])
    monkeypatch.setattr(ctxindex, "search", real)
    for c in again["classes"]:
        if "skipped" not in c:
            assert c["recall_at_10_scoring_mean"] <= 0.9, (c["class"], "a dropped best hit is a scoring miss")

    monkeypatch.setattr(B, "_clock", lambda: 0.0)
    kept = B.retention(DSN, run["space"], run["ns"])
    assert kept["old_rows_before"] > 0 and kept["first_rows"] > 0
    assert kept["nothing_old_tick_rows"] == 0
    assert kept["sweep_vectors"] > 0, "the planted old rows' vectors are past the sweep's grace"
    assert kept["sweep_removed_what_it_reported"] is True
    assert kept["job_ticks_to_clear_eligible"] == -(-kept["orphans_eligible_now"] // 1000)
    assert kept["nothing_old_plan"], "the EXPLAIN of the nothing-old tick is reported"

    cleaned = B.clean(DSN, run["space"], run["ns"])
    written = 2 * QUERIES * len(by)                                         # after_write: one row per query, two searches
    assert cleaned["chunks"] == 4000 + written - kept["first_rows"] - kept["until_empty_rows"]


def test_a_reload_after_a_clean_is_flagged_in_the_disk_projection(run):
    """pxx f90237: load, clean, reload in one database (what a failed first attempt leaves) must not project as a fresh
    load. At 768 dims every vector is TOASTed, as in production. B.clean's connection closes before the measurement,
    which flushes its statistics, so the deletes are counted."""
    big = 768
    B.load(DSN, run["space"], run["ns"], big, run["plan"], 7, run["now"])
    first = B.disk(DSN, run["space"], 4000, target=40_000)["measured"]["mesh_ctx_vectors"]
    B.clean(DSN, run["space"], run["ns"])
    B.load(DSN, run["space"], run["ns"], big, run["plan"], 7, run["now"])
    sized = B.disk(DSN, run["space"], 4000, target=40_000)
    v = sized["measured"]["mesh_ctx_vectors"]
    assert v["rows_run"] == 4000 and v["vector_datum_bytes_run"] >= 4000 * big * 4
    assert v["deleted_since_stats_reset"] - first["deleted_since_stats_reset"] >= 4000, "the clean's deletes counted"
    assert v["toast_deleted_since_stats_reset"] - first["toast_deleted_since_stats_reset"] >= 4000, "and TOAST's"
    assert any(w.startswith("mesh_ctx_vectors:") and "fresh database" in w for w in sized["projection"]["warnings"])
