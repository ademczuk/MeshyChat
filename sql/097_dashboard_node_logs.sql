-- Dashboard node logs: per-owner log stream reported by linked nodes, viewed
-- in the dashboard's Node Logs app. Rows are retained for a per-owner window
-- (default 7 days) enforced lazily by the API on read/ingest; the tier cap
-- log_retention_days_max bounds how far an owner may raise the window.
--
-- Idempotent per tools/ci/MIGRATIONS.md (078+ rule).

CREATE TABLE IF NOT EXISTS dashboard_node_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    peer_id     UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    level       TEXT NOT NULL DEFAULT 'info'
                CHECK (level IN ('trace', 'debug', 'info', 'warn', 'error')),
    source      TEXT,
    message     TEXT NOT NULL,
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Read path: newest-first per owner (optionally per peer); retention deletes
-- also walk (owner_id, created_at).
CREATE INDEX IF NOT EXISTS dashboard_node_logs_owner_created_idx
    ON dashboard_node_logs (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS dashboard_node_logs_peer_created_idx
    ON dashboard_node_logs (peer_id, created_at DESC);

ALTER TABLE dashboard_node_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_node_logs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dashboard_node_logs_server_internal ON dashboard_node_logs;
CREATE POLICY dashboard_node_logs_server_internal ON dashboard_node_logs
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

-- Per-owner log preferences (retention window). One row per owner, created on
-- first change; absent row = 7-day default.
CREATE TABLE IF NOT EXISTS dashboard_log_settings (
    owner_id        UUID PRIMARY KEY REFERENCES dashboard_users(id) ON DELETE CASCADE,
    retention_days  INT NOT NULL DEFAULT 7 CHECK (retention_days >= 1),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS dashboard_log_settings_set_updated_at ON dashboard_log_settings;
CREATE TRIGGER dashboard_log_settings_set_updated_at
    BEFORE UPDATE ON dashboard_log_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE dashboard_log_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_log_settings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dashboard_log_settings_server_internal ON dashboard_log_settings;
CREATE POLICY dashboard_log_settings_server_internal ON dashboard_log_settings
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

-- Tier cap: the highest retention window each tier may configure. -1 would
-- mean unlimited per the existing convention; we keep enterprise finite.
INSERT INTO tier_capabilities (tier, capability, value) VALUES
    ('free',       'log_retention_days_max', 7),
    ('pro',        'log_retention_days_max', 30),
    ('enterprise', 'log_retention_days_max', 90)
ON CONFLICT (tier, capability) DO NOTHING;
