"""Mutants for cold-shard pre-filtering, shard metadata bounds, and bounded _texts manifest grouping (#509).

- P1 disables PREFILTER_COLD_SHARDS at module scope (caught by cold narrowed shard vector-read and cache-retention checks).
- P2 skips _meta_excludes in _matching (caught by warm _shard_meta zero-probe assertion).
- P3 makes _meta_excludes fail-open/exclude matching shards unconditionally (caught by parity checks).
- P4 drops the concurrent stamp check in _load_shard_filtered (caught by mid-load delete completeness check).
- P5 disables manifest-group flushing in ctxsearch._texts so `found` accumulates across all manifests (caught by
  test_batched_texts_caps_live_items_per_manifest_group).
"""
CTXINDEX = "apps/meshsem/ctxindex.py"
CTXSEARCH = "apps/meshsem/ctxsearch.py"
TESTS = ["tests/test_ctx_shard_prefilter.py"]

MUTANTS = [
    ("ctx-prefilter:P1 PREFILTER_COLD_SHARDS disabled", CTXINDEX,
     "PREFILTER_COLD_SHARDS = True",
     "PREFILTER_COLD_SHARDS = False",
     TESTS),
    ("ctx-prefilter:P2 _matching ignores _meta_excludes", CTXINDEX,
     "                if meta is not None and _meta_excludes(meta, f):\n                    continue",
     "                if False:\n                    continue",
     TESTS),
    ("ctx-prefilter:P3 _meta_excludes excludes matching shards", CTXINDEX,
     "    if f.project is not None and f.project not in meta.projects:\n        return True\n    return False",
     "    if f.project is not None and f.project not in meta.projects:\n        return True\n    return True",
     TESTS),
    ("ctx-prefilter:P4 _load_shard_filtered skips concurrent stamp check", CTXINDEX,
     "    now_stamp, now_updates = _chunk_changes(cur, space)\n    if now_stamp != stamp:",
     "    now_stamp, now_updates = _chunk_changes(cur, space)\n    if False:",
     TESTS),
    ("ctx-prefilter:P5 _texts accumulates found across all manifest groups", CTXSEARCH,
     "        if group_manifests and len(group_keys | key_set) > ITEM_FETCH_BATCH:",
     "        if False:",
     TESTS),
]
