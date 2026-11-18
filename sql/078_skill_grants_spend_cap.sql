-- 078_skill_grants_spend_cap.sql
-- TASK-65 increment 1: deploy-dark cumulative spend cap foundation.
--
-- NULL max_spend_cents means no spend cap. used_spend_cents tracks cumulative
-- consumed spend and starts at zero for existing grants.

ALTER TABLE skill_grants
    ADD COLUMN IF NOT EXISTS max_spend_cents BIGINT,
    ADD COLUMN IF NOT EXISTS used_spend_cents BIGINT NOT NULL DEFAULT 0;

-- Deterministic DB-level backstop, mirroring migration 050 for the execution cap:
-- any write that would push used_spend_cents above a non-NULL max_spend_cents is
-- rejected by Postgres regardless of the application code. try_consume_spend's
-- atomic UPDATE (WHERE used_spend_cents + cost <= max_spend_cents) keeps used <= max,
-- so the constraint never bites a correct consume; it only catches a buggy increment.
-- NULL max_spend_cents (uncapped) is exempt. The live grants all have max_spend_cents
-- NULL / used_spend_cents 0 (this migration just added them), so the constraint
-- validates trivially. DROP-then-ADD keeps the statement idempotent (078+ lint).
ALTER TABLE skill_grants DROP CONSTRAINT IF EXISTS skill_grants_spend_within_cap;
ALTER TABLE skill_grants
    ADD CONSTRAINT skill_grants_spend_within_cap
        CHECK (max_spend_cents IS NULL OR used_spend_cents <= max_spend_cents);
