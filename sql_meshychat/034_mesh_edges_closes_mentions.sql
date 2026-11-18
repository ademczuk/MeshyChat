SET LOCAL lock_timeout = '5s';

-- Mesh edge vocabulary for explicit ask settlement and body artifact references.
--
-- The original inline CHECK in 004 received PostgreSQL's real generated name
-- `mesh_edges_edge_type_check`. Keep it named when replacing it: the down migration
-- and schema inspection both need a stable handle, and an open vocabulary would make
-- adding a semantic relation an invisible code-only change.

ALTER TABLE mesh_edges DROP CONSTRAINT mesh_edges_edge_type_check;
ALTER TABLE mesh_edges ADD CONSTRAINT mesh_edges_edge_type_check CHECK (edge_type IN (
    'authored_by',
    'addressed_to',
    'copied_to',
    'in_thread',
    'replies_to',
    'owes_ack',
    'closes',
    'mentions'
));

-- The two routes this change adds are counted like the others (apps/mesh/usage.py), and the
-- usage table's route column is a closed set too. Folded into this one migration rather than
-- one file per route, so the graph additions take a single migration number.
ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query', 'mentions', 'awaiting'));

-- `closes` is a routing pointer like `in_reply_to`: a tombstone retains it so a
-- redacted settlement cannot make incremental and rebuilt graph state disagree.
CREATE OR REPLACE FUNCTION shared_memory_tombstone_keys() RETURNS text[]
    LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['redacted', 'reason_code', 'redacted_at',
                 'schema_version', 'from', 'to', 'cc', 'type', 'thread_id', 'in_reply_to', 'closes', 'requires_ack', 'ts', 'author_family']
$$;
