-- TASK-28 AC#1: interactive (input-required) call lifecycle.
--
-- A meshy skill call has been single-shot (pending -> routed/queued -> running ->
-- terminal). The A2A shim therefore had nothing to map onto TaskState::InputRequired
-- / AuthRequired (meshy-a2a lib.rs gap note). This adds two NON-terminal, in-flight
-- "paused awaiting external input" states so a mid-call target can request typed
-- input (input_required) or human/credential authorization (auth_required) and the
-- call pauses, then resumes by call_id + context_id.
--
-- Idempotent (DROP IF EXISTS + recreate) so it is safe over an out-of-band column,
-- matching the 074/076/077 idempotency fixes.

-- 1. Widen the status CHECK to admit the two new states.
ALTER TABLE skill_calls DROP CONSTRAINT IF EXISTS skill_calls_status_check;
ALTER TABLE skill_calls ADD CONSTRAINT skill_calls_status_check
    CHECK (status IN (
        'pending', 'routed', 'queued', 'running',
        'input_required', 'auth_required',
        'completed', 'failed', 'timeout', 'abandoned'
    ));

-- 2. A paused call is still in-flight: it must keep respecting its deadline (time
--    out if no input arrives), so the two new states join the deadline-sweep
--    partial index alongside the other in-flight statuses.
DROP INDEX IF EXISTS skill_calls_deadline_sweep_idx;
CREATE INDEX IF NOT EXISTS skill_calls_deadline_sweep_idx
    ON skill_calls (status, deadline_at)
    WHERE status IN (
        'pending', 'routed', 'queued', 'running',
        'input_required', 'auth_required'
    );
