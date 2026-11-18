-- 076_invocation_frames_flow_guard_mode.sql
-- TASK-59 AC#1: mark every newly-written invocation frame with the
-- FlowGuardMode active when authorize made the decision.
--
-- Nullable by design: frames written before this migration are unmarked. The
-- RequireContext flip plan must drain/age them out or use the one-window
-- grandfather in meshy-guard before treating unmarked roots as invalid.

-- Idempotent: a re-run (or a partial-apply retry after a crash) must not error.
-- This is the class that crash-looped 074. ADD COLUMN IF NOT EXISTS, drop-then-add
-- the named constraint, CREATE INDEX IF NOT EXISTS.

ALTER TABLE invocation_frames
    ADD COLUMN IF NOT EXISTS flow_guard_mode TEXT;

ALTER TABLE invocation_frames
    DROP CONSTRAINT IF EXISTS invocation_frames_flow_guard_mode_ck;
ALTER TABLE invocation_frames
    ADD CONSTRAINT invocation_frames_flow_guard_mode_ck
    CHECK (
        flow_guard_mode IS NULL
        OR flow_guard_mode IN ('observe', 'enforce_present', 'require_context')
    );

CREATE INDEX IF NOT EXISTS invocation_frames_flow_guard_mode_idx
    ON invocation_frames (flow_guard_mode)
    WHERE flow_guard_mode IS NOT NULL;
