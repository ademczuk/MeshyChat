-- DOWN for sql_meshychat/036. `sqlmigrate.py --rollback-to meshychat:35` runs it. It deletes the
-- counts of the route 036 added first, so no row violates the restored CHECK, then restores
-- 035's route list.
DO $$
DECLARE
    removed integer;
BEGIN
    DELETE FROM mesh_route_usage WHERE route = 'unread';
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE '036 rollback deleted % mesh_route_usage unread rows', removed;
END $$;

ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice'));

DELETE FROM meshychat_migrations WHERE version = 36;
