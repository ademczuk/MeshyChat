SET LOCAL lock_timeout = '5s';

-- The card list (GET /v1/cards) and the card search (GET /v1/mesh/search with the card namespace) are
-- counted under route names of their own (issue #238 C1-C2, divergence 101). Built on 039's list.
ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice', 'unread',
                     'fleet_unread', 'fleet_unread_peer', 'recipients',
                     'cards', 'card_search'));
