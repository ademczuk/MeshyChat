"""Cold-shard metadata pre-filtering, narrow lexical PK lookups, and batched text rebuilds
(apps/meshsem/ctxindex.py and apps/meshsem/ctxsearch.py).

Verifies:
  1. Exact hit, score, and coverage parity (PREFILTER_COLD_SHARDS True vs False) across exact and sharded paths,
     mid-month since, until, src, project, duplicate digests, and malformed vectors.
  2. Narrowed cold shards read only matching post-filter vectors instead of all SHARD_MAX vectors and do not evict
     resident full shards from ShardCache under a tight byte budget (#498).
  3. Cached full-shard metadata bounds skip impossible EXISTS probes and invalidate immediately when chunks change.
  4. Narrow lexical subsets reuse exact_chunks or partial current chunk indexes + sorted mesh_ctx_lexical_pkey batches
     with exact saturated TF-IDF parity, avoiding cross-seat GIN -> mesh_ctx_chunks_digest scans.
  5. _texts batches manifest and item reads into O(1) queries and scopes rebuilds to hits[:k] when rerank is off.

Run:  pytest tests/test_ctx_shard_prefilter.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import time

import numpy as np
import psycopg
import pytest

from apps.meshsem import ctxindex, ctxsearch, lexical, spaces
from tests.test_ctx_index_job import (  # noqa: F401
    DSN,
    _batch,
    _row,
    _tick,
    app,
    env,
    world as job_world,
)
from tests.test_ctx_probe_shortcut import PROBE_SQL, _CountingConn
from tests.test_ctx_vector_store import (  # noqa: F401
    DIMS,
    NEEDLE,
    NOW,
    OLD,
    UTC,
    _add,
    _aggregate_fallback,
    _away,
    owner,
    world,
)


class _VectorReadCounter:
    """Wraps a connection and counts vector rows fetched from mesh_ctx_vectors."""

    def __init__(self, conn):
        self.conn = conn
        self.vector_digests_requested = 0
        self.join_queries = 0

    def cursor(self, *a, **kw):
        real = self.conn.cursor(*a, **kw)
        parent = self

        class _Cur:
            def __enter__(self):
                real.__enter__()
                return self

            def __exit__(self, *exc):
                return real.__exit__(*exc)

            def execute(self, sql, params=None):
                s = str(sql)
                if "FROM mesh_ctx_vectors WHERE space = %s AND text_sha256 = ANY" in s and params is not None:
                    parent.vector_digests_requested += len(params[1])
                elif "JOIN mesh_ctx_vectors v ON" in s:
                    parent.join_queries += 1
                return real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(real, name)

        return _Cur()


def _plant_diverse_corpus(w) -> tuple[_dt.datetime, _dt.datetime]:
    month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    early_in_month = month_start + _dt.timedelta(hours=2)
    late_in_month = month_start + _dt.timedelta(hours=12)

    for seat in ("seat-a", "seat-b"):
        for src, proj in (("s1", "p1"), ("s2", "p2")):
            _add(w, seat=seat, src=src, project=proj, when=OLD, gen=1, current=False, vec=_away(w))
            _add(w, seat=seat, src=src, project=proj, when=OLD, gen=2, vec=_away(w))
            _add(w, seat=seat, src=src, project=proj, when=early_in_month, gen=2, vec=_away(w))
            is_needle = (seat, src) == ("seat-b", "s2")
            _add(w, seat=seat, src=src, project=proj, when=late_in_month, gen=2,
                 vec=NEEDLE if is_needle else _away(w))
            w["owner"].execute(
                "INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, %s, %s, 2) "
                "ON CONFLICT (space, namespace, seat, src) DO UPDATE SET gen = EXCLUDED.gen",
                (w["space"].name, w["ns"], seat, src),
            )

    # Duplicate digest occurrence + one wrong-dim vector row to verify deduplicated decode and bad-vector skipping.
    dup_sha = f"{999001:064x}"
    dup_vec = (NEEDLE * 0.8 + np.array([0, 0.6, 0, 0, 0, 0, 0, 0])).astype(np.float32)
    w["owner"].execute(
        "INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, %s, %s, %s)",
        (w["space"].name, dup_sha, DIMS, spaces.encode(dup_vec)),
    )
    for ord_no in (500, 501):
        w["owner"].execute(
            "INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, project, "
            "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256, current) "
            "VALUES (%s, %s, %s, %s, 'seat-a', 's1', 2, 1, 'p1', %s, %s, 0, 5, %s, %s, %s, true)",
            (w["src_id"], w["space"].name, w["ns"], ord_no, ord_no, ord_no, late_in_month, late_in_month, dup_sha),
        )

    bad_sha = f"{999002:064x}"
    w["owner"].execute(
        "INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, %s, %s, %s)",
        (w["space"].name, bad_sha, DIMS + 2, b"\x00" * ((DIMS + 2) * 4)),
    )
    w["owner"].execute(
        "INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, project, "
        "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256, current) "
        "VALUES (%s, %s, %s, 502, 'seat-a', 's1', 2, 1, 'p1', 502, 502, 0, 5, %s, %s, %s, true)",
        (w["src_id"], w["space"].name, w["ns"], late_in_month, late_in_month, bad_sha),
    )
    return month_start, early_in_month + _dt.timedelta(hours=1)


def test_prefilter_parity_across_exact_and_sharded_filters(world, monkeypatch):
    month_start, mid_cut = _plant_diverse_corpus(world)
    try:
        filter_cases = [
            ctxindex.Filters(),
            ctxindex.Filters(seat="seat-a"),
            ctxindex.Filters(seat="seat-b"),
            ctxindex.Filters(src="s1"),
            ctxindex.Filters(project="p2"),
            ctxindex.Filters(since=month_start),
            ctxindex.Filters(since=mid_cut),
            ctxindex.Filters(until=mid_cut),
            ctxindex.Filters(seat="seat-b", since=mid_cut),
            ctxindex.Filters(seat="seat-a", src="s1", since=mid_cut),
        ]
        for exact_max, shard_max, max_shards in ((100, 50, 4), (2, 4, 4), (2, 3, 2)):
            monkeypatch.setattr(ctxindex, "EXACT_MAX", exact_max)
            monkeypatch.setattr(ctxindex, "SHARD_MAX", shard_max)
            monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", max_shards)
            for f in filter_cases:
                ctxindex.forget()
                monkeypatch.setattr(ctxindex, "PREFILTER_COLD_SHARDS", True)
                hits_on, cov_on = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, f, k=10)

                ctxindex.forget()
                monkeypatch.setattr(ctxindex, "PREFILTER_COLD_SHARDS", False)
                hits_off, cov_off = ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, f, k=10)

                assert cov_on.as_dict() == cov_off.as_dict(), (exact_max, shard_max, max_shards, f)
                assert [(h.chunk_id, round(h.score, 6)) for h in hits_on] == [
                    (h.chunk_id, round(h.score, 6)) for h in hits_off
                ], (exact_max, shard_max, max_shards, f)
    finally:
        world["owner"].execute("DELETE FROM ctx_source_gen WHERE namespace = %s", (world["ns"],))


def test_narrowed_cold_shard_reads_only_matching_vectors_and_preserves_resident_cache(world, monkeypatch):
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 40)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 4)

    month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    early = month_start + _dt.timedelta(hours=1)
    cutoff = month_start + _dt.timedelta(hours=6)
    late = month_start + _dt.timedelta(hours=8)

    for _ in range(30):
        _add(world, seat="seat-hot", src="h1", when=early, vec=_away(world))
    for _ in range(27):
        _add(world, seat="seat-cold", src="c1", when=early, vec=_away(world))
    expected = [_add(world, seat="seat-cold", src="c1", when=late, vec=NEEDLE)]
    expected.extend(_add(world, seat="seat-cold", src="c1", when=late, vec=_away(world)) for _ in range(2))

    for seat, src in (("seat-hot", "h1"), ("seat-cold", "c1")):
        world["owner"].execute(
            "INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, %s, %s, 1) "
            "ON CONFLICT DO NOTHING",
            (world["space"].name, world["ns"], seat, src),
        )
    try:
        # Warm seat-hot into a tight cache whose byte budget fits one 30-row shard, not two.
        probe_cache = ctxindex.ShardCache(budget_bytes=10 * 1024 * 1024)
        ctxindex.search(
            world["owner"], world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="seat-hot"), k=5, cache=probe_cache
        )
        hot_bytes = probe_cache.bytes
        assert hot_bytes > 0 and len(probe_cache.keys()) == 1

        tight_cache = ctxindex.ShardCache(budget_bytes=hot_bytes + 512)
        ctxindex.search(
            world["owner"], world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="seat-hot"), k=5, cache=tight_cache
        )
        hot_key = tight_cache.keys()[0]

        counter = _VectorReadCounter(world["owner"])
        hits_on, cov_on = ctxindex.search(
            counter,
            world["space"],
            world["ns"],
            NEEDLE,
            ctxindex.Filters(seat="seat-cold", since=cutoff),
            k=5,
            cache=tight_cache,
        )
        assert cov_on.path == "sharded" and cov_on.scored == 3
        assert hits_on[0].chunk_id == expected[0]
        assert counter.vector_digests_requested == 3, "only the 3 post-since digests should be read from mesh_ctx_vectors"
        assert counter.join_queries == 0
        assert tight_cache.keys() == [hot_key], "narrowed cold shard slice must not evict resident full shard"

        # Control: with PREFILTER_COLD_SHARDS disabled, all 30 cold shard vectors are joined and evict seat-hot.
        tight_control = ctxindex.ShardCache(budget_bytes=hot_bytes + 512)
        ctxindex.search(
            world["owner"],
            world["space"],
            world["ns"],
            NEEDLE,
            ctxindex.Filters(seat="seat-hot"),
            k=5,
            cache=tight_control,
        )
        counter_off = _VectorReadCounter(world["owner"])
        monkeypatch.setattr(ctxindex, "PREFILTER_COLD_SHARDS", False)
        hits_off, cov_off = ctxindex.search(
            counter_off,
            world["space"],
            world["ns"],
            NEEDLE,
            ctxindex.Filters(seat="seat-cold", since=cutoff),
            k=5,
            cache=tight_control,
        )
        assert [h.chunk_id for h in hits_off] == [h.chunk_id for h in hits_on]
        assert cov_off.as_dict() == cov_on.as_dict()
        assert counter_off.join_queries == 1
        assert hot_key not in tight_control.keys(), "control: full cold shard load evicts seat-hot under tight budget"
    finally:
        world["owner"].execute("DELETE FROM ctx_source_gen WHERE namespace = %s", (world["ns"],))


def test_warm_shard_meta_skips_impossible_probes_and_invalidates_on_write(world, monkeypatch):
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 20)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 4)

    month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    t1 = month_start + _dt.timedelta(hours=2)
    for _ in range(5):
        _add(world, seat="seat-a", src="only-src", project="only-proj", when=t1, vec=_away(world))
    for _ in range(5):
        _add(world, seat="seat-b", src="b-src", project="b-proj", when=t1, vec=_away(world))
    for seat, src in (("seat-a", "only-src"), ("seat-b", "b-src")):
        world["owner"].execute(
            "INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, %s, %s, 1) "
            "ON CONFLICT DO NOTHING",
            (world["space"].name, world["ns"], seat, src),
        )
    try:
        ctxindex.forget()
        # Unfiltered search loads both full shards and populates _shard_meta.
        ctxindex.search(world["owner"], world["space"], world["ns"], NEEDLE, k=5)

        conn_probe = _CountingConn(world["owner"])
        hits, cov = ctxindex.search(
            conn_probe, world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="seat-a", src="absent-src"), k=5
        )
        # Note: subset=0 <= EXACT_MAX=1 hits exact path if subset <= 1, so set EXACT_MAX = -1 to force sharded _matching.
        monkeypatch.setattr(ctxindex, "EXACT_MAX", -1)
        conn_probe = _CountingConn(world["owner"])
        hits, cov = ctxindex.search(
            conn_probe, world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="seat-a", src="absent-src"), k=5
        )
        assert cov.path == "sharded" and hits == [] and conn_probe.probes == []

        # Positive control on warm _shard_meta: a matching filter must NOT be excluded by _meta_excludes.
        hits_match, cov_match = ctxindex.search(
            world["owner"], world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="seat-a", src="only-src"), k=5
        )
        assert cov_match.path == "sharded" and len(hits_match) == 5

        conn_since = _CountingConn(world["owner"])
        hits, cov = ctxindex.search(
            conn_since,
            world["space"],
            world["ns"],
            NEEDLE,
            ctxindex.Filters(seat="seat-a", since=t1 + _dt.timedelta(hours=2)),
            k=5,
        )
        assert cov.path == "sharded" and hits == [] and conn_since.probes == []

        # Writing a new matching chunk bumps ctx_chunk_changes and invalidates stale metadata bounds.
        added = _add(world, seat="seat-a", src="absent-src", project="only-proj", when=t1 + _dt.timedelta(hours=3), vec=NEEDLE)
        world["owner"].execute(
            "INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, 'seat-a', 'absent-src', 1) "
            "ON CONFLICT DO NOTHING",
            (world["space"].name, world["ns"]),
        )
        conn_after = _CountingConn(world["owner"])
        hits, cov = ctxindex.search(
            conn_after, world["space"], world["ns"], NEEDLE, ctxindex.Filters(seat="seat-a", src="absent-src"), k=5
        )
        assert hits and hits[0].chunk_id == added
    finally:
        world["owner"].execute("DELETE FROM ctx_source_gen WHERE namespace = %s", (world["ns"],))


class _TracedCursor:
    def __init__(self, real, sink: list[tuple[str, object]], *, match_substr: str | None = None):
        self._real = real
        self._sink = sink
        self._match = match_substr

    def execute(self, sql, params=None):
        s = str(sql)
        if self._match is None or self._match in s:
            self._sink.append((s, params))
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _cleanup_job_world(w) -> None:
    o, sp, ns = w["owner"], w["env"].space.name, w["ns"]
    o.execute("DELETE FROM mesh_ctx_chunks WHERE namespace = %s", (ns,))
    o.execute("DELETE FROM ctx_source_gen WHERE namespace = %s", (ns,))
    o.execute("DELETE FROM shared_memory WHERE namespace = %s", (ns,))


def test_narrow_lexical_arm_matches_sql_scores_and_avoids_cross_seat_join(app, job_world, monkeypatch):
    w = job_world
    o, sp = w["owner"], w["env"].space.name
    try:
        _batch(w, 1, [("user_text", "shardthrash budget calibration alpha"), ("assistant_text", "shardthrash fix")])
        _batch(w, 2, [("user_text", "shardthrash " * 12 + "calibration"), ("assistant_text", "resident memory")])
        _batch(w, 3, [("user_text", "unrelated lunch topic")])
        assert _tick(app, w).manifests_indexed == 3

        # Plant 300 chunks on another seat sharing the same lexical term to model cross-seat GIN contention.
        o.execute(
            "INSERT INTO mesh_ctx_lexical (space, text_sha256, derivation, lexemes, tfs) "
            "SELECT %s, lpad(to_hex(50000 + g), 64, '0'), %s, ARRAY['shardthrash', 'calibration'], ARRAY[2, 1] "
            "FROM generate_series(1, 300) g",
            (sp, lexical.DERIVATION),
        )
        o.execute(
            "INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, project, "
            "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256, current) "
            "SELECT c.src_id, c.space, c.namespace, 5000 + g, 'noisy-peer', 'noisy-src', 1, 1, NULL, "
            "0, 0, 0, 10, c.time_first, c.time_last, lpad(to_hex(50000 + g), 64, '0'), true "
            "FROM (SELECT * FROM mesh_ctx_chunks WHERE space = %s ORDER BY id LIMIT 1) c, generate_series(1, 300) g",
            (sp,),
        )
        o.execute(
            "INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, 'noisy-peer', 'noisy-src', 1) "
            "ON CONFLICT DO NOTHING",
            (sp, w["ns"]),
        )

        f = ctxindex.Filters(seat=w["author"]["fallback"])
        monkeypatch.setattr(ctxindex, "PREFILTER_COLD_SHARDS", True)
        body_on = ctxsearch.search(app, w["env"].space, w["ns"], "shardthrash calibration", f, k=5, rerank=False)

        monkeypatch.setattr(ctxindex, "PREFILTER_COLD_SHARDS", False)
        body_off = ctxsearch.search(app, w["env"].space, w["ns"], "shardthrash calibration", f, k=5, rerank=False)

        assert body_on["arms"]["lexical"] == body_off["arms"]["lexical"]
        assert [(h["batch"], h["first_ord"], h["score"]) for h in body_on["hits"]] == [
            (h["batch"], h["first_ord"], h["score"]) for h in body_off["hits"]
        ]

        # Verify exact _lexical_arm score parity and zero SQL JOIN to mesh_ctx_lexical when exact_chunks is supplied.
        sql_seen: list[tuple[str, object]] = []
        with psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
            with c.cursor() as cur:
                where, args = ctxindex.Filters(namespace=w["ns"], seat=w["author"]["fallback"]).sql_latest(w["env"].space)
                cur.execute(f"SELECT {ctxsearch._LEX_COLS}, c.text_sha256 FROM mesh_ctx_chunks c WHERE {where}", args)
                exact_chunks = cur.fetchall()
                tcur = _TracedCursor(cur, sql_seen)

                monkeypatch.setattr(ctxindex, "PREFILTER_COLD_SHARDS", True)
                hits_pk, det_pk = ctxsearch._lexical_arm(
                    tcur, w["env"].space, w["ns"], "shardthrash calibration", f, exact_chunks=exact_chunks
                )
                pk_queries = list(sql_seen)
                sql_seen.clear()

                monkeypatch.setattr(ctxindex, "PREFILTER_COLD_SHARDS", False)
                hits_sql, det_sql = ctxsearch._lexical_arm(tcur, w["env"].space, w["ns"], "shardthrash calibration", f)

        assert det_pk == det_sql
        assert [(h.chunk_id, pytest.approx(h.score, rel=1e-9)) for h in hits_pk] == [
            (h.chunk_id, h.score) for h in hits_sql
        ]
        assert not any("JOIN mesh_ctx_lexical" in q for q, _ in pk_queries)
        lex_pk_calls = [(q, p) for q, p in pk_queries if "FROM mesh_ctx_lexical" in q]
        assert len(lex_pk_calls) == 1
        assert len(lex_pk_calls[0][1][2]) == len({r["text_sha256"] for r in exact_chunks}) <= 3
    finally:
        o.execute(
            "DELETE FROM mesh_ctx_lexical WHERE space = %s AND text_sha256 IN ("
            "SELECT lpad(to_hex(50000 + g), 64, '0') FROM generate_series(1, 300) g)",
            (sp,),
        )
        _cleanup_job_world(w)


def test_batched_texts_bounds_shared_memory_queries_and_fast_cold_latency(app, job_world, monkeypatch):
    w = job_world
    try:
        for b in range(1, 13):
            _batch(w, b, [("user_text", f"batch {b} latency target benchmark"), ("assistant_text", f"reply {b} details")])
        assert _tick(app, w, max_manifests=20).manifests_indexed == 12

        sm_queries: list[tuple[str, object]] = []
        with psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
            f = ctxindex.Filters(namespace=w["ns"], seat=w["author"]["fallback"])
            qv = np.ones(w["env"].space.dims, dtype=np.float64)
            hits, _cov = ctxindex.search(c, w["env"].space, w["ns"], qv, f, k=12)
            assert len(hits) == 12

            with c.cursor() as cur:
                tcur = _TracedCursor(cur, sm_queries, match_substr="FROM shared_memory")
                rebuilt, kinds = ctxsearch._texts(tcur, w["ns"], hits)

        assert len(rebuilt) == 12 and all(v is not None for v in rebuilt.values())
        assert len(kinds) == 12 and all(v == ["user_text", "assistant_text"] for v in kinds.values())
        assert len(sm_queries) == 2, f"_texts must batch manifests and items into 2 queries, ran {len(sm_queries)}"

        # End-to-end cold search latency check with cold caches.
        ctxindex.forget()
        t0 = time.monotonic()
        body = ctxsearch.search(
            app,
            w["env"].space,
            w["ns"],
            "latency target benchmark",
            ctxindex.Filters(seat=w["author"]["fallback"]),
            k=3,
            rerank=False,
        )
        elapsed_s = time.monotonic() - t0
        assert len(body["hits"]) == 3 and all(h["text_available"] for h in body["hits"])
        assert all(h["item_kinds"] == ["user_text", "assistant_text"] for h in body["hits"])
        assert elapsed_s < 2.0, f"cold filtered search took {elapsed_s:.3f}s"
    finally:
        _cleanup_job_world(w)


def test_batched_texts_caps_live_items_per_manifest_group(app, job_world, monkeypatch):
    w = job_world
    try:
        for b in range(1, 13):
            _batch(w, b, [("user_text", f"cap batch {b} prompt"), ("assistant_text", f"cap batch {b} answer")])
        assert _tick(app, w, max_manifests=20).manifests_indexed == 12

        monkeypatch.setattr(ctxsearch, "ITEM_FETCH_BATCH", 6)
        group_sizes: list[int] = []
        real_rebuild = ctxsearch._rebuild_manifest_group

        def _spy_rebuild(found, group_manifests, by_manifest, out, kinds):
            group_sizes.append(len(found))
            return real_rebuild(found, group_manifests, by_manifest, out, kinds)

        monkeypatch.setattr(ctxsearch, "_rebuild_manifest_group", _spy_rebuild)

        sm_queries: list[tuple[str, object]] = []
        with psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
            f = ctxindex.Filters(namespace=w["ns"], seat=w["author"]["fallback"])
            qv = np.ones(w["env"].space.dims, dtype=np.float64)
            hits, _cov = ctxindex.search(c, w["env"].space, w["ns"], qv, f, k=12)
            assert len(hits) == 12
            with c.cursor() as cur:
                tcur = _TracedCursor(cur, sm_queries, match_substr="FROM shared_memory")
                rebuilt, kinds = ctxsearch._texts(tcur, w["ns"], hits)

        assert len(rebuilt) == 12 and all(v is not None for v in rebuilt.values())
        assert len(kinds) == 12 and all(v == ["user_text", "assistant_text"] for v in kinds.values())
        assert group_sizes == [6, 6, 6, 6], f"expected 4 bounded groups of <=6 items, got {group_sizes}"
        assert max(group_sizes) <= ctxsearch.ITEM_FETCH_BATCH
        assert len(sm_queries) == 5, f"expected 1 manifest query + 4 item group queries, ran {len(sm_queries)}"
    finally:
        _cleanup_job_world(w)


def test_load_shard_filtered_marks_incomplete_when_stamp_moves_mid_load(world, monkeypatch):
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "PLAN_AGGREGATE_FALLBACK", False)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 4)

    month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    early = month_start + _dt.timedelta(hours=1)
    cutoff = month_start + _dt.timedelta(hours=4)
    late = month_start + _dt.timedelta(hours=6)

    for _ in range(3):
        _add(world, seat="seat-s", src="s1", when=early, vec=_away(world))
    target = _add(world, seat="seat-s", src="s1", when=late, vec=NEEDLE)
    extra = _add(world, seat="seat-s", src="s1", when=late, vec=_away(world))
    world["owner"].execute(
        "INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, 'seat-s', 's1', 1) "
        "ON CONFLICT DO NOTHING",
        (world["space"].name, world["ns"]),
    )
    try:
        ctxindex.forget()
        f = ctxindex.Filters(namespace=world["ns"], seat="seat-s", since=cutoff)
        with world["owner"].cursor(row_factory=psycopg.rows.dict_row) as cur:
            plan, updates, stamp = ctxindex._shard_plan_with_stamp(cur, world["space"], f)
            assert len(plan) == 1
            seat, mstart, lo, hi, n = plan[0]

            # Append outside [lo, hi]: stamp advances, updates unchanged, planned range row count unchanged -> complete.
            _add(world, seat="seat-other", src="o1", when=late, vec=_away(world))
            sh_ok = ctxindex._load_shard_filtered(
                cur, world["space"], world["ns"], seat, mstart, lo, hi, n, updates, f, stamp=stamp
            )
            assert sh_ok.complete is True
            assert {r["id"] for r in sh_ok.rows} == {target, extra}

            # Delete inside [lo, hi] after planning: stamp advances and ctx_shard_plan count drops -> incomplete.
            world["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE id = %s", (extra,))
            sh_inc = ctxindex._load_shard_filtered(
                cur, world["space"], world["ns"], seat, mstart, lo, hi, n, updates, f, stamp=stamp
            )
            assert sh_inc.complete is False
    finally:
        world["owner"].execute("DELETE FROM ctx_source_gen WHERE namespace = %s", (world["ns"],))
