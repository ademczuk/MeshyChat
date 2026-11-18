SET LOCAL lock_timeout = '5s';

-- A card read the client makes on its own behalf names its `purpose` and is counted under a route of its own,
-- so `cards` and `card_search` keep only the interactive reads (issue #238 C3, divergence 103). Built on 044's list.
ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice', 'unread',
                     'fleet_unread', 'fleet_unread_peer', 'recipients',
                     'cards', 'card_search',
                     'cards_briefing', 'cards_send', 'cards_write', 'card_search_dialog',
                     'cards_unknown', 'card_search_unknown'));
