-- DOWN for sql_meshychat/044. `sqlmigrate.py --rollback-to meshychat:43` runs it. It deletes the card
-- counts first, so no row violates the restored CHECK, then restores 039's route list.
DO $$
DECLARE
    removed integer;
BEGIN
    DELETE FROM mesh_route_usage WHERE route IN ('cards', 'card_search');
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE '044 rollback deleted % mesh_route_usage card rows', removed;
END $$;

ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice', 'unread',
                     'fleet_unread', 'fleet_unread_peer', 'recipients'));

DELETE FROM meshychat_migrations WHERE version = 44;
