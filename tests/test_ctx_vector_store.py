"""v1.1 step 6b, route B: the transcript-context vector store (apps/meshsem/ctxindex.py over sql_meshychat/047).

Synthetic chunks and vectors, written as the database owner, in a space of this test's own, so nothing else's rows are in
the subset. The design's constants are shrunk per test (EXACT_MAX, SHARD_MAX, MAX_SHARDS_PER_QUERY) so the rules can be
shown on a handful of rows; the 15-million-vector latency acceptance is a separate HQB measurement, not a unit test.

Run:  pytest tests/test_ctx_vector_store.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import uuid
from pathlib import Path

import numpy as np
import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn, rls  # noqa: E402
from apps.meshsem import ctxindex, spaces  # noqa: E402
from tests import test_mesh_authorship as mesh  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
DIMS = 8
UTC = _dt.timezone.utc
NOW = _dt.datetime.now(UTC).replace(microsecond=0)
OLD = (NOW.replace(day=1) - _dt.timedelta(days=70)).replace(day=10)     # a month at least two months back


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def world(owner):
    space = spaces.Space(name=f"fake-{DIMS}/{DIMS}/ctxstore-{uuid.uuid4().hex[:8]}", family="fake", dims=DIMS,
                         document_prompt="{text}", query_prompt="{text}", canaries=())
    ns = f"shared/ctxstore-{uuid.uuid4().hex[:8]}"
    author = mesh._peer(owner, "cvs")
    mesh._admit(owner, ns, author)
    src_id = sr.insert(owner, sr.row(author["peer_id"], author["owner_id"], ns, f"ctxm/x/{uuid.uuid4().hex}/0001/000001",
                                     {"schema": "context.v1", "kind": "manifest"}, author_label=author["fallback"]))
    ctxindex.forget()
    w = {"space": space, "owner": owner, "src_id": src_id, "n": 0, "rng": np.random.default_rng(7), "ns": ns}
    yield w
    owner.execute("DELETE FROM mesh_ctx_chunks WHERE space = %s", (space.name,))
    owner.execute("DELETE FROM mesh_ctx_vectors WHERE space = %s", (space.name,))
    owner.execute("DELETE FROM ctx_chunk_changes WHERE space = %s", (space.name,))   # after the chunks: they bump it
    ctxindex.forget()


@pytest.fixture(autouse=True)
def _aggregate_fallback(monkeypatch):
    """This module shrinks SHARD_MAX after inserting, so its plans come from the aggregate (sql_meshychat/053's stored
    plan is cut at the setting's size). Production never takes that path (ctxindex.PLAN_AGGREGATE_FALLBACK)."""
    monkeypatch.setattr(ctxindex, "PLAN_AGGREGATE_FALLBACK", True)


@pytest.fixture()
def stored_shard_max(owner, monkeypatch):
    """Set the stored plan's shard size and ctxindex.SHARD_MAX together, BEFORE inserting, so the search reads the stored
    plan: the production path (pxx b80f02 point 3). The setting is restored, and the plan rebuilt, afterwards."""
    def set_to(n: int) -> None:
        owner.execute("UPDATE ctx_shard_setting SET shard_max = %s WHERE one", (n,))
        owner.execute("SELECT public.meshy_ctx_shard_plan_rebuild()")
        monkeypatch.setattr(ctxindex, "SHARD_MAX", n)
        monkeypatch.setattr(ctxindex, "PLAN_AGGREGATE_FALLBACK", False)
        ctxindex.forget()
    yield set_to
    owner.execute("UPDATE ctx_shard_setting SET shard_max = 50000 WHERE one")
    owner.execute("SELECT public.meshy_ctx_shard_plan_rebuild()")
    ctxindex.forget()


def _add(w, *, seat="seat-a", src="s1", when=NOW, vec=None, project=None, gen=1, ns=None, current=True) -> int:
    """One chunk occurrence with its own vector; returns the chunk id."""
    vec = w["rng"].normal(size=DIMS) if vec is None else np.asarray(vec, dtype=np.float64)
    digest = f"{w['n']:064x}"
    w["n"] += 1
    w["owner"].execute("INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, %s, %s, %s)",
                       (w["space"].name, digest, DIMS, spaces.encode(vec.astype(np.float32))))
    return w["owner"].execute(
        "INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, project, first_ord, "
        "last_ord, char_start, char_end, time_first, time_last, text_sha256, current) VALUES (%s, %s, %s, %s, %s, %s, "
        "%s, 1, %s, %s, %s, 0, 1, %s, %s, %s, %s) RETURNING id",
        (w["src_id"], w["space"].name, ns or w["ns"], w["n"], seat, src, gen, project, w["n"], w["n"], when, when,
         digest, current)).fetchone()[0]


NEEDLE = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float64)


def _away(w):
    """A vector far from NEEDLE (negative first component)."""
    v = w["rng"].normal(size=DIMS)
    v[0] = -abs(v[0]) - 3.0
    return v


def test_a_small_filtered_set_is_scored_exactly_and_finds_the_needle(world):
    for _ in range(5):
        _add(world, vec=_away(world))
    needle = _add(world, vec=NEEDLE)
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=3)
    assert hits[0].chunk_id == needle and hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert (cov.path, cov.subset, cov.scored, cov.truncated) == ("exact", 6, 6, False)
    assert hits[0].first_ord == hits[0].last_ord and hits[0].seat == "seat-a", "provenance rides with the hit"


def test_a_sparse_seats_old_needle_is_found_under_its_filter_while_the_unfiltered_shards_miss_it(world, monkeypatch):
    """The design's acceptance for route B rule 2: the filtered set never depends on residency."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 10)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 5)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 1)
    for _ in range(30):
        _add(world, seat="busy", vec=_away(world))
    needle = _add(world, seat="quiet", when=OLD, vec=NEEDLE)
    _add(world, seat="quiet", when=OLD, vec=_away(world))
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=3)
    assert cov.path == "sharded" and cov.subset == ctxindex._subset_cap() and cov.subset_at_least   # 32 > cap 11
    assert needle not in [h.chunk_id for h in hits], "control: one resident shard of the newest month cannot see it"
    assert cov.truncated and len(cov.shards_searched) == 1, cov
    assert any(s[0] == "quiet" for s in cov.shards_skipped), "a skipped shard must be named, never claimed searched"
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="quiet"), k=3)
    assert cov.path == "exact" and hits[0].chunk_id == needle and not cov.truncated


def test_inside_a_shard_the_filter_still_excludes_rows(world, monkeypatch):
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 2)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 50)
    for _ in range(6):
        _add(world, src="other", vec=_away(world))
    _add(world, src="other", vec=NEEDLE)                  # the best vector, but in the wrong src
    mine = [_add(world, src="mine", vec=_away(world)) for _ in range(3)]
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, ctxindex.Filters(src="mine"), k=10)
    assert cov.path == "sharded" and cov.subset == 3 and cov.scored == 3
    assert sorted(h.chunk_id for h in hits) == sorted(mine), "a shard ranked a row the filter excludes"


def test_shards_are_newest_first_and_capped_at_shard_max(world, monkeypatch, stored_shard_max):
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    stored_shard_max(4)                                   # the stored plan: the production path
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 10)
    for _ in range(9):
        _add(world, vec=_away(world))
    for _ in range(2):
        _add(world, when=OLD, vec=_away(world))
    _hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=3)
    months = [s[1] for s in cov.shards_searched]
    assert months == sorted(months, reverse=True), "newest month first"
    assert len([m for m in months if m == NOW.strftime("%Y-%m")]) == 3, "9 rows at SHARD_MAX 4 are 3 shards"
    assert cov.scored == 11 and not cov.truncated and not cov.shards_skipped


def _mk(key, rows=2):
    return ctxindex._Shard(key, [{"id": i, "seat": "s"} for i in range(rows)],
                           np.zeros((rows, 64), dtype=np.float32), (rows,))


def test_the_cache_evicts_least_recently_used_under_its_budget():
    one = _mk("probe").nbytes
    cache = ctxindex.ShardCache(budget_bytes=3 * one + one // 2)          # room for three, not four
    for key in ("a", "b", "c"):
        assert cache.put(_mk(key))
    assert cache.get("a", (2,)) is not None               # a is now the most recently used
    cache.put(_mk("d"))
    assert "b" not in cache.keys() and "a" in cache.keys(), cache.keys()
    assert cache.bytes <= cache.budget


def test_a_shard_larger_than_the_whole_budget_is_not_cached():
    """pxx a00354 P2-4: the budget is a cap; the old loop kept one shard whatever its size."""
    cache = ctxindex.ShardCache(budget_bytes=1)
    assert cache.put(_mk("big")) is False and cache.keys() == [] and cache.bytes == 0


def test_a_resident_shard_counts_what_it_keeps_and_drops_the_raw_vectors(world, monkeypatch):
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    for _ in range(4):
        _add(world, vec=_away(world))
    ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=1)
    shard = next(iter(ctxindex._cache._shards.values()))
    assert all("vector" not in r and "dim" not in r for r in shard.rows), "the raw bytes stayed beside the matrix"
    assert shard.nbytes >= shard.mat.nbytes + sum(__import__("sys").getsizeof(r) for r in shard.rows)


def test_a_row_deleted_between_planning_and_loading_is_reported_truncated_and_nothing_repeats(world, monkeypatch,
                                                                                             stored_shard_max):
    """pxx a00354 P2-3: shards are fixed id ranges; a delete after planning leaves its range short, which must read
    as truncated (and not be cached), and no chunk may appear twice. On the stored plan (sql_meshychat/053)."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    stored_shard_max(2)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 10)
    ids = [_add(world, vec=_away(world)) for _ in range(4)]
    real = ctxindex._load_shard
    calls = []

    def load_after_a_write(cur, space, namespace, seat, month, lo, hi, rows, updates=0, *, deadline=None):
        if not calls:                                     # between the plan and the FIRST load: delete + insert
            world["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE id = %s", (ids[0],))
            _add(world, vec=_away(world))
        calls.append((lo, hi))
        # #538 passes the query's deadline; pass it on, so this still runs the real deadline path (pxx 16591a).
        return real(cur, space, namespace, seat, month, lo, hi, rows, updates, deadline=deadline)

    monkeypatch.setattr(ctxindex, "_load_shard", load_after_a_write)
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=10)
    got = [h.chunk_id for h in hits]
    assert len(got) == len(set(got)), f"a chunk appeared twice: {got}"
    assert ids[0] not in got and cov.truncated, cov.as_dict()
    assert len(ctxindex._cache.keys()) == 1, "the short shard was cached"


def test_a_row_with_the_wrong_dim_is_skipped_not_fatal(world):
    _add(world, vec=NEEDLE)
    digest = "f" * 64
    world["owner"].execute("INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, %s, 3, %s)",
                           (world["space"].name, digest, spaces.encode(np.ones(3, dtype=np.float32))))
    world["owner"].execute(
        "INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, first_ord, last_ord, "
        "char_start, char_end, time_first, time_last, text_sha256) VALUES (%s, %s, %s, 999, 'seat-a', 's1', 1, 1, 0, 0, "
        "0, 1, %s, %s, %s)", (world["src_id"], world["space"].name, world["ns"], NOW, NOW, digest))
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)
    assert len(hits) == 1 and cov.subset == 2 and cov.scored == 1 and cov.truncated


def test_a_grown_shard_is_reloaded_not_served_stale(world, monkeypatch):
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 50)
    for _ in range(3):
        _add(world, vec=_away(world))
    ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=1)
    needle = _add(world, vec=NEEDLE)
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=1)
    assert hits[0].chunk_id == needle and cov.scored == 4


def test_a_since_filter_inside_a_month_still_reaches_every_shard_of_it(world, monkeypatch):
    """The shard plan counts the WHOLE seat-month: numbering shards from the time-filtered count would cut the month
    short. Seven rows, three early and four late; a since filter keeps the four late ones, the last of which sits in
    the month's third shard. By reading, the old plan (counting the filtered subset: two shards of three) misses it;
    not yet run, so the red side is unproven until HQB runs this file against the previous head."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 3)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 10)
    start = NOW.replace(day=1, hour=0, minute=1, second=0)
    for _ in range(3):
        _add(world, when=start, vec=_away(world))
    late = [_add(world, when=NOW, vec=_away(world)) for _ in range(4)]
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE,
                                ctxindex.Filters(since=NOW - _dt.timedelta(minutes=1)), k=10)
    assert sorted(h.chunk_id for h in hits) == sorted(late), cov.as_dict()
    assert cov.scored == 4 and not cov.truncated


def test_a_delete_plus_an_insert_in_one_month_is_not_served_stale(world, monkeypatch):
    """pxx 6b25d0: the count alone stays the same; the greatest id does not."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 50)
    rows = [_add(world, vec=_away(world)) for _ in range(3)]
    ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=1)
    world["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE id = %s", (rows[0],))
    needle = _add(world, vec=NEEDLE)
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=1)
    assert hits[0].chunk_id == needle and cov.scored == 3


def test_the_store_is_read_under_server_internal_only(world):
    _add(world, vec=NEEDLE)
    app = dbconn.connect(DSN)
    try:
        hits, cov = ctxindex.search(app, world["space"], world["ns"], NEEDLE, k=1)
        assert hits == [] and cov.subset == 0, "a request context read the index"
        with rls.server_internal(app):
            hits, _cov = ctxindex.search(app, world["space"], world["ns"], NEEDLE, k=1)
        assert len(hits) == 1, "control: under server_internal it reads"
    finally:
        app.close()


def test_a_zero_query_vector_is_refused(world):
    with pytest.raises(ValueError):
        ctxindex.search(world["owner"], world["space"], world["ns"], np.zeros(DIMS))


def test_retention_deletes_only_chunks_older_than_180_days(world):
    kept = _add(world, vec=_away(world))
    gone = _add(world, when=NOW - _dt.timedelta(days=181), vec=_away(world))

    class Job:
        max_batch = 100
    app = dbconn.connect(DSN)
    try:
        ctxindex.run_retention_job(app, Job())
    finally:
        app.close()
    ids = {r[0] for r in world["owner"].execute("SELECT id FROM mesh_ctx_chunks WHERE space = %s",
                                                (world["space"].name,)).fetchall()}
    assert kept in ids and gone not in ids
    assert world["owner"].execute("SELECT ctx_vectors_orphaned(%s)", (world["space"].name,)).fetchone()[0] == 1, \
        "the old chunk's vector is now an orphan for ctxjob's sweep"


def test_the_retention_job_is_registered_and_bounded():
    from apps.jobs import registry
    job = registry.by_name("ctx_vector_retention")
    assert job.run is ctxindex.run_retention_job and job.max_batch == ctxindex.RETENTION_BATCH


def test_a_search_never_counts_or_returns_another_namespaces_rows(world, monkeypatch):
    """pxx 075b32 P1: same space, two namespaces; a search on one sees nothing of the other, on both paths."""
    _add(world, vec=NEEDLE, ns="shared/ctxstore-elsewhere")
    mine = _add(world, vec=_away(world))
    for exact in (20_000, 0):
        monkeypatch.setattr(ctxindex, "EXACT_MAX", exact)
        hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)
        assert [h.chunk_id for h in hits] == [mine] and cov.subset == 1, (exact, cov.as_dict())
    with pytest.raises(ValueError):
        ctxindex.search(world["owner"], world["space"], "", NEEDLE)


def test_only_the_newest_gen_of_a_source_is_served(world, monkeypatch):
    """pxx 075b32 P2-1: gen 1 and gen 2 of one (seat, src) stored; only gen 2 is counted and returned, on both paths."""
    _add(world, vec=NEEDLE, gen=1)
    new = _add(world, vec=_away(world), gen=2)
    other = _add(world, src="s2", vec=_away(world), gen=1)
    for exact in (20_000, 0):
        monkeypatch.setattr(ctxindex, "EXACT_MAX", exact)
        hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)
        assert sorted(h.chunk_id for h in hits) == sorted([new, other]) and cov.subset == 2, (exact, cov.as_dict())


def test_a_newest_gen_with_no_chunks_retires_the_older_gen(world, monkeypatch):
    """pxx 51fbd8: gen 2 of a source wrote no chunk (only omissions), but ctx_source_gen records gen 2, so gen 1's
    chunks are no longer served, on both paths. Control: another source with no record keeps its newest chunk gen."""
    _add(world, vec=NEEDLE, gen=1)
    other = _add(world, src="s2", vec=_away(world), gen=1)
    world["owner"].execute("INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, 'seat-a', "
                           "'s1', 2)", (world["space"].name, world["ns"]))
    for exact in (20_000, 0):
        monkeypatch.setattr(ctxindex, "EXACT_MAX", exact)
        hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)
        assert [h.chunk_id for h in hits] == [other] and cov.subset == 1, (exact, cov.as_dict())
    world["owner"].execute("DELETE FROM ctx_source_gen WHERE namespace = %s", (world["ns"],))


def test_another_spaces_newer_gen_does_not_retire_this_spaces_gen(world, monkeypatch):
    """a reviewer on #386: a candidate space indexing gen 2 leaves the serving space's gen 1 served."""
    mine = _add(world, vec=NEEDLE, gen=1)
    world["owner"].execute("INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES ('other-space', %s, "
                           "'seat-a', 's1', 2)", (world["ns"],))
    try:
        for exact in (20_000, 0):
            monkeypatch.setattr(ctxindex, "EXACT_MAX", exact)
            hits, _cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)
            assert [h.chunk_id for h in hits] == [mine], exact
    finally:
        world["owner"].execute("DELETE FROM ctx_source_gen WHERE space = 'other-space'")


def test_a_chunk_that_starts_before_until_and_ends_in_a_later_month_is_found(world, monkeypatch):
    """a reviewer on #390: shards are months of time_last; `until` filters time_first, so no month is pruned for it."""
    end = _dt.datetime(2026, 9, 1, 0, 30, tzinfo=_dt.timezone.utc)      # time_last in September
    start = _dt.datetime(2026, 8, 31, 23, 50, tzinfo=_dt.timezone.utc)  # time_first in August
    cid = _add(world, vec=NEEDLE, when=end)
    world["owner"].execute("UPDATE mesh_ctx_chunks SET time_first = %s WHERE id = %s", (start, cid))
    until = _dt.datetime(2026, 9, 1, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 0)                       # the sharded path, where the pruning is
    hits, _cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5,
                                 f=ctxindex.Filters(until=until))
    assert [h.chunk_id for h in hits] == [cid]


def test_the_shard_cache_is_bounded_by_count_as_well_as_bytes(monkeypatch):
    """The process-lifetime cache keeps at most CACHED_SHARDS_MAX shards, least recently used out first."""
    monkeypatch.setattr(ctxindex, "CACHED_SHARDS_MAX", 2)
    cache = ctxindex.ShardCache(budget_bytes=10 ** 9)

    class S:                                              # the cache reads only key, nbytes, version and loaded
        def __init__(self, key):
            self.key, self.nbytes, self.version, self.loaded = key, 10, (1,), ctxindex.time.monotonic()

    for k in ("a", "b", "c"):
        assert cache.put(S(k)) is True
    assert cache.keys() == ["b", "c"] and cache.bytes == 20


def test_a_src_filtered_search_skips_shards_with_no_match_before_spending_its_budget(world, monkeypatch):
    """Codex on #387: above EXACT_MAX, a newer shard holding only OTHER sources must not use up MAX_SHARDS_PER_QUERY.
    CONTROL: with the match probe answering every shard (the code before the fix), the one-shard budget goes to the
    newest month and the old needle is missed."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 5)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 1)
    for _ in range(12):
        _add(world, src="busy", vec=_away(world))
    needle = _add(world, src="quiet", when=OLD, vec=NEEDLE)
    _add(world, src="quiet", when=OLD, vec=_away(world))
    f = ctxindex.Filters(src="quiet")
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, f, k=3)
    assert cov.path == "sharded" and hits and hits[0].chunk_id == needle
    assert len(cov.shards_searched) == 1 and not cov.shards_skipped and not cov.truncated

    ctxindex.forget()
    monkeypatch.setattr(ctxindex, "_matching", lambda cur, space, f, plan: set(range(len(plan))))
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, f, k=3)
    assert needle not in [h.chunk_id for h in hits] and cov.truncated, "control: the unprobed budget misses it"


def test_the_subset_count_stops_one_past_what_a_search_can_score_and_says_so(world, monkeypatch, stored_shard_max):
    """The cap's edge: with two shards of five, a search scores at most 10, so the count stops at 11. Ten chunks are
    counted exactly; eleven are "at least 11", and truncated, as an exact count of 11 would be too. On the stored plan."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 2)
    stored_shard_max(5)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 2)
    assert ctxindex._subset_cap() == 11
    for _ in range(10):
        _add(world, vec=_away(world))
    _hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=3)
    assert (cov.subset, cov.subset_at_least, cov.scored, cov.truncated) == (10, False, 10, False), cov.as_dict()
    _add(world, vec=NEEDLE)
    hits, cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=3)
    assert (cov.subset, cov.subset_at_least) == (11, True) and cov.scored < 11 and cov.truncated, cov.as_dict()  # 5+5+1 shards: the newest two hold 6 (pxx 3bc7b5)
    assert hits and hits[0].score == pytest.approx(1.0, abs=1e-5), "the newest shards are still searched"


def _fresh_statistics(conn) -> None:
    """Measure under statistics taken NOW, as production's autovacuum keeps them. Without this, what a plan reads can
    depend on whether autovacuum happened to ANALYZE the shared table between the small and the large measurement:
    the likely cause of mg369b (836e32dd), where the subset count read 22 -> 528 rows in the full suite only."""
    conn.execute("ANALYZE mesh_ctx_chunks, ctx_source_gen")


def _chunk_scans(conn, sql: str, params) -> dict:
    """The statement's EXPLAIN ANALYZE, read for mesh_ctx_chunks alone. `rows`: what its scans of that table and its
    indexes READ (B5's measure, rows_read, restricted to the table the bound is about: other tests' ctx_source_gen rows
    behind a per-row gen lookup added a constant few hundred, pxx 0706c6). `indexes`: every chunk index a node that
    RAN scanned, and `seq`, (rows returned, rows removed) for each seq scan of the table that ran. A node with 0 loops never executed: the
    latest-gen COALESCE's max(gen) fallback on mesh_ctx_chunks_seat_src_gen is in every plan and runs only for a
    source without a ctx_source_gen row (pxx 4ff2e3). `shape`: every scan node, printed beside a failing bound
    (mg411b: 641 -> 881 had no plan to read)."""
    from tests.test_query_cost_bounds import _SCAN_NODES, _walk_nodes
    names = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'mesh_ctx_chunks'")}
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
        plan = cur.fetchone()[0][0]["Plan"]
    rows, indexes, seq, shape = 0.0, set(), [], []
    for n in _walk_nodes(plan):
        if n["Node Type"] not in _SCAN_NODES:
            continue
        rel, idx = n.get("Relation Name"), n.get("Index Name")
        shape.append(f"{n['Node Type']} {rel or idx} rows={n.get('Actual Rows')} "
                     f"removed={n.get('Rows Removed by Filter', 0)} loops={n.get('Actual Loops')}")
        if (rel != "mesh_ctx_chunks" and idx not in names) or not n.get("Actual Loops"):
            continue
        if idx in names:
            indexes.add(idx)
        if n["Node Type"] == "Seq Scan":
            loops = n.get("Actual Loops", 1)
            seq.append((n.get("Actual Rows", 0) * loops, n.get("Rows Removed by Filter", 0) * loops))
        rows += (n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0)
                 + n.get("Rows Removed by Index Recheck", 0)) * n.get("Actual Loops", 1)
    return {"rows": int(rows), "indexes": indexes, "seq": seq, "shape": "; ".join(shape)}


#: sql_meshychat/052: the partial CURRENT indexes, space first. A request read may use these and nothing else on
#: mesh_ctx_chunks: any other index (mesh_ctx_chunks_digest, 047's non-partial ones) holds superseded rows too.
CURRENT_INDEXES = frozenset({"mesh_ctx_chunks_current_space_seat_time", "mesh_ctx_chunks_current_space_seat_src"})


def test_no_full_index_on_the_chunks_can_serve_the_shard_probe(owner):
    """The schema guard (pxx 07476a point 1): Postgres takes no index hint, so the plan guard below fails only when the
    planner happens to pick a full index (1 run in 13 on main, pxx 6a2895). This one is deterministic: no index on
    mesh_ctx_chunks whose leading columns serve the probe's (seat, time_last) range may lack `WHERE current`, or the
    probe can walk superseded history through it. Fails against 047's mesh_ctx_chunks_seat_time; passes after 059;
    catches anyone adding a full index of that shape back."""
    rows = owner.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                         "AND tablename = 'mesh_ctx_chunks'").fetchall()
    assert any(name == "mesh_ctx_chunks_current_space_seat_time" for name, _ in rows), rows      # positive control
    probe_shapes = ("(namespace, seat, time_last)", "(space, namespace, seat, time_last)")
    bad = [name for name, ddl in rows
           if any(shape in ddl for shape in probe_shapes) and "WHERE current" not in ddl]
    assert not bad, f"full indexes that can serve the shard probe and read superseded rows: {bad}"


def _assert_current_only(scan: dict, what: str) -> None:
    """The plan guard (pxx 0706c6): the read touches mesh_ctx_chunks only through CURRENT_INDEXES. A bound on rows read
    cannot see a plan that is cheap at this size and walks every row of the space at production's.

    One seq scan passes: one that discarded nothing and returned no more than the cap. With only this test's rows in
    the table, ANALYZE makes the planner seq-scan it and stop at the LIMIT (pxx e378df: the file run ALONE, 11 rows,
    0 removed), which reads nothing an index path would not. A seq scan that met a superseded row, or another space's,
    filtered it out, and fails here."""
    cap = ctxindex._subset_cap()
    walked = [(n, gone) for n, gone in scan["seq"] if gone or n > cap]
    assert not walked and scan["indexes"] <= CURRENT_INDEXES, (
        f"{what} reads mesh_ctx_chunks outside the partial current indexes: {sorted(scan['indexes'])}, "
        f"seq scans (rows, removed) past the cap or filtering {walked}; plan [{scan['shape']}]")


def test_a_sharded_requests_count_and_gen_reads_do_not_grow_with_the_namespace(world, monkeypatch):
    """The switch-on scaling condition (pxx ab9411, e2f3ee), B5's method (tests/test_query_cost_bounds.py): what the
    subset count and the latest-gen read READ, by EXPLAIN ANALYZE of the statements a warm sharded request runs, at 40
    chunks and at 160. CONTROL: the statements they replaced (the uncounted count and the namespace-wide GROUP BY),
    measured the same way, read more by at least the 120 added chunks."""
    from tests.test_query_cost_bounds import _Recording, rows_read
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 2)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 5)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 2)
    o, sp, ns = world["owner"], world["space"], world["ns"]
    f = ctxindex.Filters(namespace=ns)
    where, args = f.sql_latest(sp)
    lw, la = ctxindex.Filters(namespace=ns).sql(sp)
    old_count = f"SELECT count(*) AS n FROM mesh_ctx_chunks c WHERE {where}"
    old_latest = (f"SELECT c.seat, c.src, {ctxindex._LATEST_GEN_SQL} AS gen FROM mesh_ctx_chunks c WHERE {lw} "
                  "GROUP BY c.space, c.namespace, c.seat, c.src")

    def measure():
        _fresh_statistics(o)
        ctxindex.search(o, sp, ns, NEEDLE, k=3)                       # warm the plan and the shards
        rec = _Recording(o)
        ctxindex.search(rec, sp, ns, NEEDLE, k=3)
        count = [(s, p) for s, p in rec.statements if "count(*) AS n FROM (SELECT 1" in s]
        latest = [(s, p) for s, p in rec.statements if "unnest(%s::text[], %s::text[])" in s]
        assert len(count) == 1 and len(latest) == 1, rec.statements
        scan = _chunk_scans(o, *count[0])
        _assert_current_only(scan, "the subset count")
        return (scan["rows"], rows_read(o, *latest[0]), rows_read(o, old_count, args),
                rows_read(o, old_latest, la), scan["shape"])

    # As the walker leaves it: every source's newest gen recorded (ctx_source_gen), so the gen read is that row.
    for i in range(4):
        o.execute("INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, 'seat-a', %s, 1)",
                  (sp.name, ns, f"s{i}"))
    try:
        for i in range(40):
            _add(world, src=f"s{i % 4}", vec=_away(world))
        small = measure()
        for i in range(120):
            _add(world, src=f"s{i % 4}", vec=_away(world))
        large = measure()
    finally:
        o.execute("DELETE FROM ctx_source_gen WHERE space = %s", (sp.name,))
    assert large[2] - small[2] >= 120 and large[3] - small[3] >= 120, f"control: the old reads grew {small[:4]} -> {large[:4]}"
    assert large[0] - small[0] <= 5, (f"the subset count grew with the namespace: {small[0]} -> {large[0]}; "
                                      f"plan small [{small[-1]}] large [{large[-1]}]")
    assert large[1] - small[1] <= 5, f"the latest-gen read grew with the namespace: {small[1]} -> {large[1]}"


def test_a_sharded_requests_count_and_probes_do_not_grow_with_superseded_history(world, monkeypatch):
    """Switch-on blocker 5 (sql_meshychat/051; pxx b62e37, e0cf57; codex-pxx-cli TASK AQ). The same 12 CURRENT gen-2
    rows, with 40 and then 160 superseded gen-1 rows (current false) written FIRST, so a walk in insertion order meets
    every one of them before a current row. What the subset count READS (EXPLAIN ANALYZE, B5's instrument) must stay
    within the current rows plus the cap, and both the count and the shard probes must reach mesh_ctx_chunks only through 052's partial indexes, which
    hold no superseded row. CONTROL: the pre-051 predicate (the latest-gen check alone), measured the same way, reads
    more by at least the 120 added rows."""
    from tests.test_query_cost_bounds import _Recording, rows_read
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 2)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 5)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 2)
    # Step 6b (#460) skips the probe for an unfiltered request; this test is the check that the probe, when it runs,
    # reaches mesh_ctx_chunks only through 052's partial indexes, so it keeps the probe on (pxx 9d081b).
    monkeypatch.setattr(ctxindex, "PROBE_SHORTCUT", False)
    o, sp = world["owner"], world["space"]
    # Each measure runs in a namespace of its own (pxx c04d70). Deleting the small measure's rows and vacuuming left
    # their entries in 052's partial index whenever another snapshot was open at that moment (the warm search's, an
    # autovacuum worker's), and a bitmap scan counted them: 35 rows read for 12 current, 2 runs in about 22. With a
    # fresh namespace no dead current entry is in the range at all, and nothing is deleted between the measures.
    small_ns, large_ns = world["ns"] + "-small", world["ns"] + "-large"

    def old_count(ns):
        where, args = ctxindex.Filters(namespace=ns).sql(sp)
        return (f"SELECT count(*) AS n FROM (SELECT 1 FROM mesh_ctx_chunks c WHERE {where} AND c.gen = "
                f"{ctxindex._LATEST_GEN_SQL} LIMIT %s) s"), args

    def measure(ns):
        _fresh_statistics(o)
        ctxindex.search(o, sp, ns, NEEDLE, k=3)                       # warm the plan and the shards
        rec = _Recording(o)
        ctxindex.search(rec, sp, ns, NEEDLE, k=3)
        count = [(s, p) for s, p in rec.statements if "count(*) AS n FROM (SELECT 1" in s]
        probe = [(s, p) for s, p in rec.statements if "WITH ORDINALITY AS p(seat, start_, end_, lo, hi, i)" in s]
        assert len(count) == 1 and len(probe) == 1, rec.statements
        scan, probes = _chunk_scans(o, *count[0]), _chunk_scans(o, *probe[0])
        _assert_current_only(scan, "the subset count")
        _assert_current_only(probes, "the shard probes")
        sql, args = old_count(ns)
        return (scan["rows"], probes["rows"], rows_read(o, sql, [*args, ctxindex._subset_cap()]), scan["shape"])

    for ns in (small_ns, large_ns):
        o.execute("INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, 'seat-a', 's1', 2)",
                  (sp.name, ns))
    try:
        for _ in range(40):
            _add(world, gen=1, current=False, vec=_away(world), ns=small_ns)
        for _ in range(12):
            _add(world, gen=2, vec=_away(world), ns=small_ns)
        small = measure(small_ns)
        for _ in range(160):
            _add(world, gen=1, current=False, vec=_away(world), ns=large_ns)
        for _ in range(12):
            _add(world, gen=2, vec=_away(world), ns=large_ns)
        large = measure(large_ns)
    finally:
        o.execute("DELETE FROM ctx_source_gen WHERE space = %s", (sp.name,))
    assert large[2] - small[2] >= 120, f"control: the pre-051 count read grew {small[:3]} -> {large[:3]}"
    # Bound against what is CURRENT, not small against large: a plain Index Scan stops at the cap (11), a bitmap path
    # over the same partial index reads every current entry first and then the cap's heap rows (12 + 11), and which one
    # the planner picks moves with statistics (pxx 697fd7). Neither may read past the 12 current rows plus the cap:
    # one superseded row read would.
    most = 12 + ctxindex._subset_cap()
    for label, m in (("small", small), ("large", large)):
        assert m[0] <= most, (f"the {label} subset count read {m[0]} chunk rows, more than the 12 current rows plus "
                              f"the cap ({most}); plan [{m[-1]}]")
    # The probes' guard (in measure) is their superseded-history claim: the partial indexes hold current rows only,
    # so no probe can read a superseded one. Their row count is NOT bounded here: it follows the plan's shape over
    # CURRENT rows (one range scan, or one loop per shard range with the id range as a filter: 11 -> 18 at the same 3
    # shards, pxx 4ff2e3), not the history this test adds.


def test_a_cached_shard_stops_serving_a_retired_gen(world, monkeypatch):
    """pxx e0cf57: the flip is an UPDATE, so 049's update count moves and a resident shard holding the retired rows
    reloads; with ctx_source_gen at 2, gen 1 is no longer served. CONTROL: before the new gen, gen 1 is served."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 0)
    monkeypatch.setattr(ctxindex, "_plans", {})
    o, sp, ns = world["owner"], world["space"], world["ns"]
    old = _add(world, gen=1, vec=NEEDLE)
    _add(world, gen=1, vec=_away(world))
    hits, _ = ctxindex.search(o, sp, ns, NEEDLE, k=3)
    assert hits and hits[0].chunk_id == old, "control: gen 1 is served while it is the newest"
    o.execute("INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, 'seat-a', 's1', 2)",
              (sp.name, ns))
    try:
        new = _add(world, gen=2, vec=_away(world))
        o.execute("UPDATE mesh_ctx_chunks SET current = false WHERE space = %s AND gen < 2", (sp.name,))  # the flip
        hits, _ = ctxindex.search(o, sp, ns, NEEDLE, k=3)
        assert old not in [h.chunk_id for h in hits] and [h.chunk_id for h in hits] == [new], hits
    finally:
        o.execute("DELETE FROM ctx_source_gen WHERE space = %s", (sp.name,))


def test_051s_backfill_retires_every_row_that_is_not_its_sources_newest_gen(world):
    """The backfill statement, run from 051's own text (between its markers): rows of an older gen become not current,
    the newest gen's stay current, and a source with no ctx_source_gen row keeps only its greatest gen. CONTROL:
    before it runs, every row is current (DEFAULT true), which is the state a pre-051 index is in."""
    import re
    o, sp, ns = world["owner"], world["space"], world["ns"]
    text = (ROOT / "sql_meshychat" / "051_ctx_chunk_current.sql").read_text(encoding="utf-8")
    backfill = re.search(r"-- backfill: begin\n(.*?)-- backfill: end", text, re.S).group(1)
    o.execute("INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, 'seat-a', 's1', 2)",
              (sp.name, ns))
    try:
        a1, a2 = _add(world, src="s1", gen=1), _add(world, src="s1", gen=2)
        b1, b3 = _add(world, src="s2", gen=1), _add(world, src="s2", gen=3)          # no ctx_source_gen row
        cur = lambda i: o.execute("SELECT current FROM mesh_ctx_chunks WHERE id = %s", (i,)).fetchone()[0]  # noqa
        assert [cur(i) for i in (a1, a2, b1, b3)] == [True] * 4, "control: before the backfill every row is current"
        o.execute(backfill)
        assert [cur(i) for i in (a1, a2, b1, b3)] == [False, True, False, True]
    finally:
        o.execute("DELETE FROM ctx_source_gen WHERE space = %s", (sp.name,))


def test_an_updated_chunk_reloads_its_resident_shard(world, monkeypatch):
    """codex-pxx-cli round 2 P2-3: an UPDATE keeps the shard's id range and row count, so the count alone kept the old
    matrix resident. CONTROL: with the UPDATE count left out of the version, the old score comes back."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 0)
    monkeypatch.setattr(ctxindex, "_plans", {})
    needle = _add(world, vec=NEEDLE)
    other = _add(world, vec=_away(world))
    o = world["owner"]
    hits, _ = ctxindex.search(o, world["space"], world["ns"], NEEDLE, k=5)
    assert hits[0].chunk_id == needle and hits[0].score == pytest.approx(1.0, abs=1e-5)
    o.execute("UPDATE mesh_ctx_chunks SET text_sha256 = (SELECT text_sha256 FROM mesh_ctx_chunks WHERE id = %s) "
              "WHERE id = %s", (other, needle))
    hits, cov = ctxindex.search(o, world["space"], world["ns"], NEEDLE, k=5)
    assert all(h.score < 0.5 for h in hits) and not cov.truncated, "the resident shard kept the pre-UPDATE vector"

    o.execute("UPDATE mesh_ctx_chunks SET text_sha256 = %s WHERE id = %s", (f"{0:064x}", needle))   # the needle again
    ctxindex.forget()
    real = ctxindex._chunk_changes
    monkeypatch.setattr(ctxindex, "_chunk_changes", lambda cur, space: (real(cur, space)[0], 0))
    ctxindex.search(o, world["space"], world["ns"], NEEDLE, k=5)                   # warm, version (rows, 0)
    o.execute("UPDATE mesh_ctx_chunks SET text_sha256 = (SELECT text_sha256 FROM mesh_ctx_chunks WHERE id = %s) "
              "WHERE id = %s", (other, needle))
    hits, _ = ctxindex.search(o, world["space"], world["ns"], NEEDLE, k=5)
    assert hits[0].score == pytest.approx(1.0, abs=1e-5), "control: without the UPDATE count the stale matrix is kept"


def test_the_shard_plan_is_computed_once_per_index_change(world, monkeypatch):
    """pxx ab9411: the plan's whole-namespace aggregate is not paid per request. Two searches with nothing new reuse
    it; a new chunk moves the stamp and the next search replans, and finds the new chunk."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 0)               # the sharded path, which plans
    monkeypatch.setattr(ctxindex, "_plans", {})
    calls = []
    real = ctxindex._plan_rows

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(ctxindex, "_plan_rows", counting)
    _add(world, vec=_away(world))
    ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)
    ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)
    assert len(calls) == 1, "the plan ran per request"
    new = _add(world, vec=NEEDLE)
    hits, _cov = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)
    assert len(calls) == 2 and hits and hits[0].chunk_id == new, "a new chunk did not replan, or was missed"


def test_a_cached_plan_still_applies_the_seat_filter(world, monkeypatch):
    """The cache holds every seat's shards; the seat filter is applied per request."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 0)
    monkeypatch.setattr(ctxindex, "_plans", {})
    a = _add(world, seat="seat-a", vec=NEEDLE)
    b = _add(world, seat="seat-b", vec=NEEDLE)
    hits, _ = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="seat-b"), k=5)
    assert [h.chunk_id for h in hits] == [b]
    hits, _ = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="seat-a"), k=5)
    assert [h.chunk_id for h in hits] == [a]
