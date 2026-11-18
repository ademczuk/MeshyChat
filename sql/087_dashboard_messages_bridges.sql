-- Dashboard-owned UI state for mesh messages and memory namespace bridges.
-- The message payload itself remains in shared_memory; this table only tracks
-- per-owner dashboard read/archive/delete state.

CREATE TABLE IF NOT EXISTS dashboard_message_state (
    message_id  UUID NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    owner_id    UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    read_at     TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    deleted_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (message_id, owner_id)
);

CREATE INDEX IF NOT EXISTS dashboard_message_state_owner_idx
    ON dashboard_message_state (owner_id)
    WHERE deleted_at IS NULL;

DROP TRIGGER IF EXISTS dashboard_message_state_set_updated_at ON dashboard_message_state;
CREATE TRIGGER dashboard_message_state_set_updated_at
    BEFORE UPDATE ON dashboard_message_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE dashboard_message_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_message_state FORCE ROW LEVEL SECURITY;
CREATE POLICY dashboard_message_state_server_internal ON dashboard_message_state
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

CREATE TABLE IF NOT EXISTS dashboard_memory_bridges (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_namespace_id  TEXT NOT NULL REFERENCES shared_memory_namespace(namespace) ON DELETE NO ACTION,
    target_namespace_id  TEXT NOT NULL REFERENCES shared_memory_namespace(namespace) ON DELETE NO ACTION,
    bridge_type          TEXT NOT NULL CHECK (bridge_type IN ('read', 'write', 'bidirectional')),
    filters              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by_owner_id  UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE NO ACTION,
    created_by_peer_id   UUID NOT NULL REFERENCES peers(id) ON DELETE NO ACTION,
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT dashboard_memory_bridges_distinct_ns
        CHECK (source_namespace_id <> target_namespace_id)
);

CREATE INDEX IF NOT EXISTS dashboard_memory_bridges_owner_idx
    ON dashboard_memory_bridges (created_by_owner_id, active);

CREATE UNIQUE INDEX IF NOT EXISTS dashboard_memory_bridges_active_unique
    ON dashboard_memory_bridges (created_by_owner_id, source_namespace_id, target_namespace_id, bridge_type)
    WHERE active = TRUE;

DROP TRIGGER IF EXISTS dashboard_memory_bridges_set_updated_at ON dashboard_memory_bridges;
CREATE TRIGGER dashboard_memory_bridges_set_updated_at
    BEFORE UPDATE ON dashboard_memory_bridges
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE dashboard_memory_bridges ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_memory_bridges FORCE ROW LEVEL SECURITY;
CREATE POLICY dashboard_memory_bridges_server_internal ON dashboard_memory_bridges
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
