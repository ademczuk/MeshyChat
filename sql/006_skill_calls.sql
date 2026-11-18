-- spec §5.2: unified call record (replaces tool_calls + async_tool_jobs).
-- spec §5.3: RLS policy — peer sees only its own calls.

CREATE TABLE skill_calls (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id               TEXT UNIQUE NOT NULL,
    caller_peer_id        UUID REFERENCES peers(id),                       -- NULL only for dashboard_test
    caller_owner_id       UUID REFERENCES dashboard_users(id),
    target_peer_id        UUID NOT NULL REFERENCES peers(id),
    target_owner_id       UUID NOT NULL REFERENCES dashboard_users(id),
    skill_id              UUID NOT NULL REFERENCES skills(id),              -- pinned to version row
    skill_name            TEXT NOT NULL,
    skill_version         TEXT NOT NULL,
    mode                  TEXT NOT NULL CHECK (mode IN ('sync','async','stream')),
    transport             TEXT NOT NULL CHECK (transport IN ('http','ws','dashboard_test')),
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','routed','queued','running','completed','failed','timeout','abandoned')),
    args                  JSONB NOT NULL,
    args_signature        TEXT,                                             -- NULL only for dashboard_test
    args_signing_kid      TEXT,
    args_attachments      UUID[] NOT NULL DEFAULT '{}',                     -- file_artifacts refs
    result                JSONB,
    result_attachments    UUID[] NOT NULL DEFAULT '{}',
    result_signature      TEXT,
    result_signing_kid    TEXT,
    error_code            TEXT,
    error_message         TEXT,
    grant_id              UUID REFERENCES skill_grants(id),
    webhook_url           TEXT,
    webhook_secret_hash   TEXT,
    deadline_at           TIMESTAMPTZ NOT NULL,
    started_at            TIMESTAMPTZ,
    completed_at          TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Spec-listed indexes (§5.2):
CREATE INDEX skill_calls_caller_created_idx
    ON skill_calls (caller_peer_id, created_at DESC);
CREATE INDEX skill_calls_target_status_deadline_idx
    ON skill_calls (target_peer_id, status, deadline_at);

-- Deadline sweeper (§7.4): partial index over only the in-flight statuses.
CREATE INDEX skill_calls_deadline_sweep_idx
    ON skill_calls (status, deadline_at)
    WHERE status IN ('pending','routed','queued','running');

-- RLS: peer sees only its own calls (§5.3).
-- We FORCE the policy so it applies even to the table owner — necessary
-- because the meshy-server connects as the same role that owns the schema
-- during local dev.
ALTER TABLE skill_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_calls FORCE ROW LEVEL SECURITY;

-- SELECT: scope to caller or target peer.
CREATE POLICY skill_calls_peer_select ON skill_calls
    FOR SELECT
    USING (
        caller_peer_id::text = current_setting('app.current_peer_id', true)
        OR target_peer_id::text = current_setting('app.current_peer_id', true)
    );

-- INSERT/UPDATE/DELETE: server-side internal writes are unrestricted at the
-- DB layer; meshy-api enforces authorization in application code via the
-- rpc::authorize step. Writes stay permissive so service code can populate
-- calls without juggling roles.
CREATE POLICY skill_calls_internal_insert ON skill_calls
    FOR INSERT
    WITH CHECK (true);
CREATE POLICY skill_calls_internal_update ON skill_calls
    FOR UPDATE
    USING (true)
    WITH CHECK (true);
CREATE POLICY skill_calls_internal_delete ON skill_calls
    FOR DELETE
    USING (true);
