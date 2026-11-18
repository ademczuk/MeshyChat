SET LOCAL lock_timeout = '5s';

-- Add `declines` to the closed mesh_edges relation vocabulary (`NOT VALID` then
-- `VALIDATE CONSTRAINT` so `ACCESS EXCLUSIVE` is not held during the full-table scan)
-- and retain `declines` on redacted tombstones. `shared_memory_tombstone_keys()`
-- equals `040_card_tombstone_keys.sql`'s list plus `'declines'` (copy from `060`,
-- never `034`, when extending tombstone keys later).

ALTER TABLE mesh_edges DROP CONSTRAINT mesh_edges_edge_type_check;
ALTER TABLE mesh_edges ADD CONSTRAINT mesh_edges_edge_type_check CHECK (edge_type IN (
    'authored_by',
    'addressed_to',
    'copied_to',
    'in_thread',
    'replies_to',
    'owes_ack',
    'closes',
    'declines',
    'mentions'
)) NOT VALID;
ALTER TABLE mesh_edges VALIDATE CONSTRAINT mesh_edges_edge_type_check;

CREATE OR REPLACE FUNCTION shared_memory_tombstone_keys() RETURNS text[]
    LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['redacted', 'reason_code', 'redacted_at',
                 'schema_version', 'from', 'to', 'cc', 'type', 'thread_id', 'in_reply_to', 'closes', 'declines', 'requires_ack', 'ts', 'author_family',
                 'schema', 'id', 'seat', 'time', 'supersedes']
$$;
