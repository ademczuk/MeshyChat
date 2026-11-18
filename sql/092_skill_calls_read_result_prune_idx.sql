-- no-transaction
-- 092: partial prune index for the skill_calls read-result retention janitor (TASK-399).
--
-- The janitor victim CTE in crates/meshy-calls/src/repository/writes.rs selects
-- terminal memory.query / memory.read rows whose result payload is still unpruned,
-- ordered by completed_at ASC with a LIMIT and FOR UPDATE SKIP LOCKED. Without a
-- matching index the prune victim query seq-scans and de-TOASTs the whole table as
-- it grows (proven on prod 2026-06-28: the deployed janitor backfill hit its
-- self-timeout with zero rows pruned, so the 18GB read-result bloat had to be drained
-- by a one-shot manual prune). This partial index mirrors the victim's STATIC filters
-- exactly; the leading completed_at column serves the `completed_at < cutoff` range and
-- the `ORDER BY completed_at ASC LIMIT`, and the trailing id covers the selected key.
-- The volatile age cutoff (NOW() - interval) is deliberately NOT in the predicate
-- because index predicates must be IMMUTABLE; it is satisfied by the leading-column range.
--
-- CONCURRENTLY (hence the leading -- no-transaction directive, sqlx 0.8.6
-- source.rs:127 sql.starts_with): skill_calls is the hot, multi-GB write path, so a
-- plain in-transaction CREATE INDEX would take a write-blocking SHARE lock for the whole
-- build. CONCURRENTLY builds without blocking writes. On a fresh or empty DB the build is
-- instant. IF NOT EXISTS keeps it idempotent; a CONCURRENTLY build interrupted on prod
-- leaves an INVALID index that must be dropped and re-run.
--
-- Deploy note (TASK-399, bxxxxx prod/build lane): run EXPLAIN (ANALYZE, BUFFERS) of the
-- victim CTE on the production-sized table after this lands to confirm the planner picks
-- skill_calls_read_result_prune_idx before declaring the prune backfill safe.

CREATE INDEX CONCURRENTLY IF NOT EXISTS skill_calls_read_result_prune_idx
    ON skill_calls (completed_at, id)
    WHERE skill_name IN ('memory.query', 'memory.read')
      AND status IN ('completed', 'failed', 'timeout', 'abandoned')
      AND result IS NOT NULL
      AND completed_at IS NOT NULL
      AND COALESCE(jsonb_extract_path_text(result, 'pruned'), 'false') <> 'true';
