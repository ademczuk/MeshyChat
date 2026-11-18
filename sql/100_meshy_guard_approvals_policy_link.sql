-- TASK-556.1: link the approval queue to the policy registry and make ASK
-- retry-safe, tenant-namespaced, args-bound, and single-release.

SET lock_timeout = '5s';

ALTER TABLE meshy_guard_approvals
    ADD COLUMN policy_id          TEXT,
    ADD COLUMN attachment_id      TEXT,
    ADD COLUMN elicitation_key    TEXT,
    ADD COLUMN approved_args_hash TEXT,
    ADD COLUMN consumed_at        TIMESTAMPTZ;

CREATE UNIQUE INDEX rga_pending_elicitation_idx
    ON meshy_guard_approvals (owner_id, elicitation_key)
    WHERE status = 'pending' AND elicitation_key IS NOT NULL;

CREATE INDEX rga_release_idx
    ON meshy_guard_approvals (owner_id, elicitation_key, status)
    WHERE elicitation_key IS NOT NULL AND consumed_at IS NULL;

ALTER TABLE meshy_guard_flow_decisions
    DROP CONSTRAINT meshy_guard_flow_decisions_decision_check;

ALTER TABLE meshy_guard_flow_decisions
    ADD CONSTRAINT meshy_guard_flow_decisions_decision_check
        CHECK (decision IN ('allow', 'deny', 'ask'));
