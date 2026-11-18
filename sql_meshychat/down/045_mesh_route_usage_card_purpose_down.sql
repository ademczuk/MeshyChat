-- DOWN for sql_meshychat/045. `sqlmigrate.py --rollback-to meshychat:44` runs it. It deletes the counts of the
-- purpose routes first, so no row violates the restored CHECK, then restores 044's route list.
DO $$
DECLARE
    removed integer;
BEGIN
    DELETE FROM mesh_route_usage WHERE route IN ('cards_briefing', 'cards_send', 'cards_write',
                                                 'card_search_dialog', 'cards_unknown', 'card_search_unknown');
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE '045 rollback deleted % mesh_route_usage card purpose rows', removed;
END $$;

ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice', 'unread',
                     'fleet_unread', 'fleet_unread_peer', 'recipients',
                     'cards', 'card_search'));

DELETE FROM meshychat_migrations WHERE version = 45;
