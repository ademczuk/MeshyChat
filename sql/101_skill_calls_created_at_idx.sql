-- no-transaction
-- 101: created_at-leading index for TASK-578 Arm 1 identity-rate-watch sweep.
--
-- crates/meshy-calls/src/identity_rate_watch.rs aggregates skill_calls by
-- caller_peer_id over a rolling ~8-day created_at window every 900s when
-- MESHY_IDENTITY_RATE_WATCH is on. The only prior index involving created_at is
-- skill_calls_caller_created_idx on (caller_peer_id, created_at DESC), which
-- cannot seek a pure `created_at >= $1` predicate (no caller constraint). This
-- index gives the planner a time-leading path so the sweep does not seq-scan
-- the hot multi-GB table once the flag is enabled in production.
--
-- CONCURRENTLY (hence the leading -- no-transaction directive, sqlx 0.8.6
-- source.rs:127 sql.starts_with): skill_calls is the hot write path, so a plain
-- in-transaction CREATE INDEX would take a write-blocking SHARE lock for the
-- whole build. CONCURRENTLY builds without blocking writes. IF NOT EXISTS keeps
-- it idempotent; a CONCURRENTLY build interrupted on prod leaves an INVALID
-- index that must be dropped and re-run.
--
-- Flag default remains off; the index is safe to land ahead of enablement.

CREATE INDEX CONCURRENTLY IF NOT EXISTS skill_calls_created_at_idx
    ON skill_calls (created_at);
