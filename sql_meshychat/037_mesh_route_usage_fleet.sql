SET LOCAL lock_timeout = '5s';

-- The operator's fleet backlog routes (divergence 081), GET /v1/mesh/fleet/unread and
-- GET /v1/mesh/fleet/unread/<peer_id>, are counted under their own route names.
ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting',
                     'brief', 'brief_notice', 'neighbours_notice', 'unread',
                     'fleet_unread', 'fleet_unread_peer'));
