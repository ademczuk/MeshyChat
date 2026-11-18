-- DOWN for sql_meshychat/037. `sqlmigrate.py --rollback-to meshychat:36` runs it. It deletes the
-- counts of the two routes 037 added first, so no row violates the restored CHECK, then restores
-- 036's route list.
DO $$
DECLARE
    removed integer;
BEGIN
    DELETE FROM mesh_route_usage WHERE route IN ('fleet_unread', 'fleet_unread_peer');
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE '037 rollback deleted % mesh_route_usage fleet rows', removed;
END $$;

ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice', 'unread'));

DELETE FROM meshychat_migrations WHERE version = 37;
