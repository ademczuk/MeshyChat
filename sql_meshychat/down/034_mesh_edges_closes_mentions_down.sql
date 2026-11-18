-- DOWN for sql_meshychat/034. `sqlmigrate.py --rollback-to meshychat:33` runs it. It deletes the new rows first, then restores the old closed vocabularies and 027's tombstone key list. Re-applying 034 needs `python -m apps.mesh backfill --types closes,mentions` again.

DELETE FROM mesh_edges WHERE edge_type IN ('closes', 'mentions');

ALTER TABLE mesh_edges DROP CONSTRAINT mesh_edges_edge_type_check;
ALTER TABLE mesh_edges ADD CONSTRAINT mesh_edges_edge_type_check CHECK (edge_type IN (
    'authored_by',
    'addressed_to',
    'copied_to',
    'in_thread',
    'replies_to',
    'owes_ack'
));

DELETE FROM mesh_route_usage WHERE route IN ('mentions', 'awaiting');
ALTER TABLE mesh_route_usage DROP CONSTRAINT mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog', 'memory_query'));

-- 034 replaced 027's tombstone key list to add `closes` and `requires_ack`; put 027's back. 027's file
-- itself is never edited: sqlmigrate refuses checksum drift on an applied version.
CREATE OR REPLACE FUNCTION shared_memory_tombstone_keys() RETURNS text[]
    LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['redacted', 'reason_code', 'redacted_at',
                 'schema_version', 'from', 'to', 'cc', 'type', 'thread_id', 'in_reply_to', 'ts', 'author_family']
$$;

DELETE FROM meshychat_migrations WHERE version = 34;
