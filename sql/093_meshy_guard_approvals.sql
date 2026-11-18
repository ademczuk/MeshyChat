-- TASK-536: durable MeshyGuard approval queue.
--
-- Approval ids are bearer capabilities, but the API still gates every resolve
-- against the approval owner's tenant or dev group. The queue is DB-backed so
-- multi-replica deployments do not lose pending human decisions.

CREATE TABLE IF NOT EXISTS meshy_guard_approvals (
    approval_id        TEXT PRIMARY KEY,
    status             TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'cancelled')),
    owner_id           UUID NOT NULL,
    caller_peer_id     UUID NOT NULL,
    skill_id           UUID,
    call_id            TEXT,
    phase              TEXT NOT NULL
        CHECK (phase IN ('tool_call', 'flow', 'request')),
    policy             TEXT,
    reason_code        TEXT,
    message            TEXT NOT NULL,
    content_preview    TEXT NOT NULL,
    manifest_hash      TEXT,
    sandbox_status     TEXT
        CHECK (sandbox_status IN ('active', 'none', 'advisory')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at         TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '1 day'),
    resolved_at        TIMESTAMPTZ,
    resolver_peer_id   UUID,
    verdict            TEXT CHECK (verdict IN ('accept', 'decline', 'cancel')),
    audit_event_id     TEXT,
    deferred_effects   JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT meshy_guard_approvals_id_format
        CHECK (approval_id ~ '^appr_[0-9a-f]{32,}$'),
    CONSTRAINT meshy_guard_approvals_preview_len
        CHECK (char_length(content_preview) <= 1024),
    CONSTRAINT meshy_guard_approvals_expiry_after_create
        CHECK (expires_at > created_at),
    CONSTRAINT meshy_guard_approvals_status_verdict
        CHECK (
            (status = 'approved' AND verdict = 'accept')
            OR (status = 'denied' AND verdict = 'decline')
            OR (status = 'cancelled' AND verdict = 'cancel')
            OR (status IN ('pending', 'expired') AND verdict IS NULL)
        ),
    CONSTRAINT meshy_guard_approvals_resolution_shape
        CHECK (
            (status IN ('approved', 'denied', 'cancelled')
                AND resolved_at IS NOT NULL
                AND resolver_peer_id IS NOT NULL
                AND verdict IS NOT NULL)
            OR (status IN ('pending', 'expired'))
        )
);

CREATE INDEX IF NOT EXISTS meshy_guard_approvals_owner_pending_idx
    ON meshy_guard_approvals (owner_id, created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS meshy_guard_approvals_pending_expiry_idx
    ON meshy_guard_approvals (expires_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS meshy_guard_approvals_call_idx
    ON meshy_guard_approvals (call_id)
    WHERE call_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS meshy_guard_approvals_caller_idx
    ON meshy_guard_approvals (caller_peer_id, created_at);
