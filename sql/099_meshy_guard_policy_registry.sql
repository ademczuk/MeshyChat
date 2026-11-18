-- TASK-556.1: MeshyGuard policy registry.
-- Registry entries name a handler compiled into meshy-guard; attachments bind
-- an entry to a scope with typed params; decisions are the durable audit of
-- policy registry evaluation outcomes.

CREATE TABLE meshy_guard_policies (
    policy_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,
    handler_ref   TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    description   TEXT NOT NULL DEFAULT '',
    internal_only BOOLEAN NOT NULL DEFAULT FALSE,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    params_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT meshy_guard_policies_kind_check
        CHECK (kind IN ('builtin', 'expression')),
    CONSTRAINT meshy_guard_policies_id_shape
        CHECK (policy_id ~ '^rgp_[0-9a-f]{32}$')
);

CREATE TABLE meshy_guard_policy_attachments (
    attachment_id      TEXT PRIMARY KEY,
    policy_id          TEXT NOT NULL REFERENCES meshy_guard_policies(policy_id),
    scope              TEXT NOT NULL,
    owner_id           UUID,
    session_key        TEXT,
    params             JSONB NOT NULL DEFAULT '{}'::jsonb,
    phases             TEXT[] NOT NULL DEFAULT '{tool_call}',
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    ordinal            INTEGER NOT NULL DEFAULT 0,
    ask_timeout_secs   BIGINT,
    created_by_peer_id UUID NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT rgpa_scope_check
        CHECK (scope IN ('server_default', 'owner', 'session')),
    CONSTRAINT rgpa_scope_owner_shape CHECK (
        (scope = 'server_default' AND owner_id IS NULL AND session_key IS NULL)
        OR (scope = 'owner' AND owner_id IS NOT NULL AND session_key IS NULL)
        OR (scope = 'session' AND owner_id IS NOT NULL AND session_key IS NOT NULL)
    ),
    CONSTRAINT rgpa_id_shape CHECK (attachment_id ~ '^rga_[0-9a-f]{32}$'),
    CONSTRAINT rgpa_phases_check CHECK (
        phases <@ ARRAY['tool_call', 'flow', 'request']::TEXT[]
        AND cardinality(phases) > 0
    )
);

CREATE UNIQUE INDEX rgpa_unique_binding
    ON meshy_guard_policy_attachments (
        policy_id,
        scope,
        COALESCE(owner_id, '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(session_key, '')
    );

CREATE INDEX rgpa_eval_idx
    ON meshy_guard_policy_attachments (owner_id, enabled, scope, ordinal)
    WHERE enabled = TRUE;

CREATE INDEX rgpa_server_default_eval_idx
    ON meshy_guard_policy_attachments (enabled, scope, ordinal)
    WHERE enabled = TRUE AND scope = 'server_default';

CREATE TABLE meshy_guard_policy_decisions (
    decision_id     BIGSERIAL PRIMARY KEY,
    policy_id       TEXT,
    attachment_id   TEXT,
    decision        TEXT NOT NULL,
    reason          TEXT,
    phase           TEXT NOT NULL,
    mode            TEXT NOT NULL,
    call_id         TEXT,
    owner_id        UUID NOT NULL,
    caller_peer_id  UUID NOT NULL,
    target_peer_id  UUID,
    skill_name      TEXT,
    session_key     TEXT,
    approval_id     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT rgpd_decision_check
        CHECK (decision IN ('allow', 'deny', 'ask', 'abstain', 'error')),
    CONSTRAINT rgpd_phase_check
        CHECK (phase IN ('tool_call', 'flow', 'request')),
    CONSTRAINT rgpd_mode_check CHECK (mode IN ('observe', 'enforce'))
);

CREATE INDEX rgpd_owner_created_idx
    ON meshy_guard_policy_decisions (owner_id, created_at DESC);

CREATE INDEX rgpd_call_idx
    ON meshy_guard_policy_decisions (call_id)
    WHERE call_id IS NOT NULL;
