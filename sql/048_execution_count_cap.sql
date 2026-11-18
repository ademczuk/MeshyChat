-- 048_execution_count_cap.sql
-- TASK-3: an execution-count revocation cap alongside time-based expiry.
--
-- Meshy revocation is otherwise all time-based (expires_at, the 3600s JWT TTL,
-- the 24h key grace, the deadline janitor). Per Capability Coherence
-- (arXiv:2603.09875) a 60s freshness window permits roughly 6000 unauthorized
-- calls at 100 ops/tick: a count cap bounds exposure independent of agent
-- velocity. This adds a per-grant cap + counter; NULL max_executions preserves
-- the existing unlimited-within-TTL behavior, so every one of the 19 live
-- direct grants is unaffected (they get max_executions = NULL, used = 0).
--
-- The cap is enforced by an ATOMIC increment-and-check (no read-then-write, so
-- no last-slot race), see meshy-grants execution_cap::try_consume_execution:
--   UPDATE skill_grants
--   SET used_executions = used_executions + 1
--   WHERE id = $1
--     AND revoked_at IS NULL
--     AND (expires_at IS NULL OR expires_at > NOW())
--     AND (max_executions IS NULL OR used_executions < max_executions)
--   RETURNING used_executions, max_executions;
-- zero rows returned => the grant is not live OR the cap is reached => deny.
--
-- Additive + inert until authorize() calls the consume path (TASK-3 #2, the
-- walled meshy-calls wiring); an old binary on this schema simply ignores the
-- two new columns.

ALTER TABLE skill_grants
    ADD COLUMN IF NOT EXISTS max_executions  INTEGER,
    ADD COLUMN IF NOT EXISTS used_executions INTEGER NOT NULL DEFAULT 0;

-- A cap, when set, must be positive; the counter is never negative. The atomic
-- UPDATE keeps used_executions <= max_executions, but the CHECK is the
-- backstop against a direct write or a bad mint-time value.
ALTER TABLE skill_grants
    ADD CONSTRAINT skill_grants_max_executions_positive
        CHECK (max_executions IS NULL OR max_executions > 0),
    ADD CONSTRAINT skill_grants_used_executions_nonneg
        CHECK (used_executions >= 0);
