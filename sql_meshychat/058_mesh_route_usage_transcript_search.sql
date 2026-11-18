SET LOCAL lock_timeout = '5s';

-- Transcript-context searches on GET /v1/mesh/search?ns=<context namespace> (TKT-4629), split between
-- interactive searches (`transcript_search`) and automatic client probes (`purpose=brief|consensus|context`,
-- or `transcript_search_unknown` for any other purpose; never uncounted, so the sum across these five routes
-- matches authenticated GET /v1/mesh/search requests for the context namespace). Built on 045's list.
ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice', 'unread',
                     'fleet_unread', 'fleet_unread_peer', 'recipients',
                     'cards', 'card_search',
                     'cards_briefing', 'cards_send', 'cards_write', 'card_search_dialog',
                     'cards_unknown', 'card_search_unknown',
                     'transcript_search', 'transcript_search_brief', 'transcript_search_consensus',
                     'transcript_search_context', 'transcript_search_unknown'));
