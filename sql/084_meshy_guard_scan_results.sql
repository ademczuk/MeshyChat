-- TASK-167: node-misbehavior auditor scan results (deterministic taint stage, P1).
--
-- No foreign keys, by design (mirrors meshy_guard_flow_decisions / invocation_frames):
-- a scan result is emitted pre-execution, before the downstream skill_calls row
-- exists, and the append-only audit trail must survive later peer/owner/grant
-- deletion. Idempotent CREATE TABLE IF NOT EXISTS so an out-of-band apply against
-- the live DB does not crash-loop boot (matches the 067-083 idempotent siblings).
--
-- review_status = 'requires_review' is the flip-review surface: a misaligned
-- verdict lands here for human review. v1 is advisory (deploy-dark), so this is a
-- routing label, not a block.

CREATE TABLE IF NOT EXISTS meshy_guard_scan_results (
    id                BIGSERIAL PRIMARY KEY,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    chain_id          UUID NOT NULL,
    conclusion        TEXT NOT NULL CHECK (conclusion IN ('aligned', 'misaligned')),
    score             REAL NOT NULL,
    review_status     TEXT NOT NULL CHECK (review_status IN ('approved', 'requires_review')),
    findings          JSONB NOT NULL DEFAULT '[]'::jsonb,
    scanner_version   TEXT NOT NULL,
    signature         BYTEA,
    signing_kid       TEXT
);

CREATE INDEX IF NOT EXISTS meshy_guard_scan_results_chain_idx
    ON meshy_guard_scan_results (chain_id);

-- Partial index for the flip-review GET route: fetch only pending human reviews.
CREATE INDEX IF NOT EXISTS meshy_guard_scan_results_pending_idx
    ON meshy_guard_scan_results (occurred_at)
    WHERE review_status = 'requires_review';
