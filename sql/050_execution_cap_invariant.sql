-- 050_execution_cap_invariant.sql
-- TASK-3 hardening: a deterministic DB-level backstop for the execution cap.
--
-- Migration 048 added max_executions + used_executions and a CHECK that
-- used_executions >= 0. The cap itself (used_executions <= max_executions) is
-- enforced in the application by execution_cap::try_consume_execution's atomic
-- conditional UPDATE (SET used = used + 1 WHERE used < max). That UPDATE is
-- correct and race-free, but the only test guarding it is probabilistic (a
-- multi-connection contention test that was never red-proven against a broken
-- read-then-write implementation). A future non-atomic refactor could therefore
-- over-increment past the cap and slip past the test on a low-contention run.
--
-- This CHECK makes the invariant DETERMINISTIC at the substrate: any write that
-- would push used_executions above a non-NULL max_executions is rejected by
-- Postgres regardless of the application code, so the exactly-once-per-cap
-- guarantee no longer rests on a probabilistic test. The correct atomic UPDATE
-- never triggers it (its WHERE used < max keeps used <= max); the constraint
-- only ever bites a buggy increment. NULL max_executions (uncapped) is exempt.
--
-- Additive + safe on the live data: the 19 live grants are all max_executions
-- NULL / used_executions 0, which the constraint admits trivially.

ALTER TABLE skill_grants
    ADD CONSTRAINT skill_grants_used_within_cap
        CHECK (max_executions IS NULL OR used_executions <= max_executions);
