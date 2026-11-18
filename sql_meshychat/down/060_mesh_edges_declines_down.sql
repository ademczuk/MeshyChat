-- DOWN for sql_meshychat/060. `sqlmigrate.py --rollback-to meshychat:59` runs it.
-- Deletes projected `declines` edges first, then restores 034's closed edge vocabulary
-- and 040's tombstone key list. Re-applying 060 needs `python -m apps.mesh backfill --types declines`.

SET LOCAL lock_timeout = '5s';

DELETE FROM mesh_edges WHERE edge_type = 'declines';

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
)) NOT VALID;
ALTER TABLE mesh_edges VALIDATE CONSTRAINT mesh_edges_edge_type_check;

CREATE OR REPLACE FUNCTION shared_memory_tombstone_keys() RETURNS text[]
    LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['redacted', 'reason_code', 'redacted_at',
                 'schema_version', 'from', 'to', 'cc', 'type', 'thread_id', 'in_reply_to', 'closes', 'requires_ack', 'ts', 'author_family',
                 'schema', 'id', 'seat', 'time', 'supersedes']
$$;

DELETE FROM meshychat_migrations WHERE version = 60;
