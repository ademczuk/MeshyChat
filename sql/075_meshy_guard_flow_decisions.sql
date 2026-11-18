-- TASK-4: meshy-guard information-flow decisions for call-chain confused-deputy guard.
--
-- No foreign keys, by design (mirrors tenancy_denials / invocation_frames):
-- a flow denial is made before the downstream skill_calls row exists, and the
-- audit trail must survive later peer/owner/grant deletion.

CREATE TABLE meshy_guard_flow_decisions (
    id                BIGSERIAL PRIMARY KEY,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision          TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
    mode              TEXT NOT NULL CHECK (mode IN ('observe', 'enforce_present', 'require_context')),
    reason_code       TEXT NOT NULL,
    call_id           TEXT NOT NULL,
    chain_id          UUID,
    capability_ref    UUID,
    caller_owner_id   UUID NOT NULL,
    caller_peer_id    UUID NOT NULL,
    target_owner_id   UUID NOT NULL,
    target_peer_id    UUID NOT NULL,
    skill_name        TEXT NOT NULL,
    origin_path       JSONB NOT NULL DEFAULT '[]'::jsonb,
    delegation_path   JSONB NOT NULL DEFAULT '[]'::jsonb,
    args_sha256       TEXT,
    detail            TEXT
);

ALTER TABLE meshy_guard_flow_decisions
    ADD CONSTRAINT meshy_guard_flow_decisions_args_sha256_fmt
    CHECK (args_sha256 IS NULL OR args_sha256 ~ '^[0-9a-f]{64}$');

CREATE INDEX meshy_guard_flow_decisions_caller_time_idx
    ON meshy_guard_flow_decisions (caller_owner_id, occurred_at DESC);

CREATE INDEX meshy_guard_flow_decisions_target_time_idx
    ON meshy_guard_flow_decisions (target_owner_id, occurred_at DESC);

CREATE INDEX meshy_guard_flow_decisions_call_idx
    ON meshy_guard_flow_decisions (call_id);

CREATE INDEX meshy_guard_flow_decisions_reason_time_idx
    ON meshy_guard_flow_decisions (reason_code, occurred_at DESC);
