-- DOWN for sql_meshychat/035. `sqlmigrate.py --rollback-to meshychat:34` runs it. It deletes the
-- counts of the three routes 035 added first, so no row violates the restored CHECK, then restores
-- 034's route list.
DO $$
DECLARE
    removed integer;
BEGIN
    DELETE FROM mesh_route_usage
    WHERE route IN ('brief', 'brief_notice', 'neighbours_notice');
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE '035 rollback deleted % mesh_route_usage brief rows', removed;
END $$;

ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting'));

DELETE FROM meshychat_migrations WHERE version = 35;
