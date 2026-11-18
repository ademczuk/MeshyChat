-- DOWN for sql_meshychat/039. `sqlmigrate.py --rollback-to meshychat:38` runs it. It deletes the
-- recipients counts first, so no row violates the restored CHECK, then restores 037's route list.
DO $$
DECLARE
    removed integer;
BEGIN
    DELETE FROM mesh_route_usage WHERE route = 'recipients';
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE '039 rollback deleted % mesh_route_usage recipients rows', removed;
END $$;

ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice', 'unread',
                     'fleet_unread', 'fleet_unread_peer'));

DELETE FROM meshychat_migrations WHERE version = 39;
