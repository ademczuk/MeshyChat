-- no-transaction
-- MeshyChat divergence 053, part 2: the read-result prune covers memory.list.
--
-- WHY. A memory.list answers with a preview of up to 160 characters of each row it lists
-- (apps/memoryskills/handlers.py, handle_list). The read-result prune replaces a memory.query
-- or memory.read result an hour after the call ends (apps/calls/retention.py); memory.list was
-- never among them, so its stored results kept row text for the whole call retention, 90 days.
-- That is an ordinary copy of row text kept far longer than a read keeps its own, redaction
-- or not (memory-bxxxxx's review of fin/redaction 189140d, 2026-09-29). With list pruned
-- hourly, a redaction has to reach only the list results under an hour old.
--
-- MEASURED on production 2026-09-29: 7,139 memory.list calls, 287 MB of results, the newest
-- from 2026-09-24. That is the prune's first backlog: 72 ticks of 100.
--
-- WHAT THIS IS. sql/092's partial index with memory.list added to its skill list, under a new
-- name. 092's index stays: it is upstream's, and dropping it is a second statement this file
-- cannot carry (CREATE INDEX CONCURRENTLY runs outside a transaction, and the runner sends a
-- file as ONE statement). The prune's victim query names all three skills, so only this index
-- matches it; tests/test_call_retention.py holds the plan.
--
-- ONE STATEMENT, and idempotent: a build interrupted half way leaves an INVALID index, which
-- IF NOT EXISTS then skips. The check is the one 092's header asks for:
--   SELECT indisvalid FROM pg_index WHERE indexrelid = 'skill_calls_result_prune_v2_idx'::regclass;
-- and the repair is DROP INDEX CONCURRENTLY and this file again.
CREATE INDEX CONCURRENTLY IF NOT EXISTS skill_calls_result_prune_v2_idx
    ON skill_calls (completed_at, id)
    WHERE skill_name IN ('memory.query', 'memory.read', 'memory.list')
      AND status IN ('completed', 'failed', 'timeout', 'abandoned')
      AND result IS NOT NULL
      AND completed_at IS NOT NULL
      AND COALESCE(jsonb_extract_path_text(result, 'pruned'), 'false') <> 'true';
