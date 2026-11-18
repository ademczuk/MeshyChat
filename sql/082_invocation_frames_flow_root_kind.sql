-- 082_invocation_frames_flow_root_kind.sql
-- Flow-guard RequireContext flip precondition: distinguish audit-visible root
-- categories on invocation_frames. External-trigger roots are context-less
-- origin frames authorized by a grant carrying a verified IntentMandate.

ALTER TABLE invocation_frames
    ADD COLUMN IF NOT EXISTS flow_root_kind TEXT;

ALTER TABLE invocation_frames
    DROP CONSTRAINT IF EXISTS invocation_frames_flow_root_kind_chk;
ALTER TABLE invocation_frames
    ADD CONSTRAINT invocation_frames_flow_root_kind_chk
        CHECK (
            flow_root_kind IS NULL
            OR flow_root_kind IN ('client_context', 'external_trigger')
        );
