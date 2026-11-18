-- Per-owner read/dismiss state for dashboard notifications. Notifications
-- themselves are derived from shared_memory `msg/%` rows (no notification table);
-- this records which the owner has marked read or dismissed so mark-read /
-- read-all / dismiss persist instead of resurrecting on the next fetch.
--
-- Server-internal RLS only (mirrors migration 087 dashboard_message_state): the
-- dashboard data layer reads/writes it under meshy_db::with_server_internal, and
-- every query is additionally scoped by owner_id in application code.

CREATE TABLE IF NOT EXISTS dashboard_notification_state (
    owner_id     UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    message_id   UUID NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    read_at      TIMESTAMPTZ,
    dismissed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, message_id)
);

CREATE INDEX IF NOT EXISTS dashboard_notification_state_owner_idx
    ON dashboard_notification_state (owner_id)
    WHERE dismissed_at IS NULL;

DROP TRIGGER IF EXISTS dashboard_notification_state_set_updated_at ON dashboard_notification_state;
CREATE TRIGGER dashboard_notification_state_set_updated_at
    BEFORE UPDATE ON dashboard_notification_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE dashboard_notification_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_notification_state FORCE ROW LEVEL SECURITY;
CREATE POLICY dashboard_notification_state_server_internal ON dashboard_notification_state
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
