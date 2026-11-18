-- MeshyChat divergence 060 (OI-57, docs/design/wiki-in-graph.md): the derived tables learn which
-- namespace a row came from, and a row may have more than one vector.
--
-- WHY. Until now only shared/mesh-chat heads were embedded and lexed, one vector per row, and
-- every reader loaded a space's rows whole. The wiki is to be embedded too (design 4.1), and 82
-- percent of its heads are longer than one window (design section 2), so a wiki row gets one
-- vector per CHUNK. Two changes follow:
--
--   * mesh_embeddings.chunk_no: the key becomes (src_id, space, chunk_no). A mail row is always
--     chunk 0, so every existing row and every row the mail job writes keeps its identity.
--   * mesh_embeddings.namespace and mesh_lexical.namespace: the in-process indexes the mail
--     search keeps (apps/meshsem/search.VectorIndex, apps/meshsem/lexical.LexicalIndex) load a
--     space's rows up to a ceiling, newest first. Without the column a wiki backfill of about
--     3,500 chunks would fill a mail query's top ranks with rows it then filters away, and
--     could push older mail vectors out of the ceiling. With it, the mail indexes load mail
--     only and answer exactly as before.
--
-- The defaults are the mail namespace, which is true of every row that exists: the only writer
-- before this migration is the mail embed job, which refuses any other namespace. The UPDATEs
-- below correct any row that says otherwise, and on production they change nothing.
--
-- A BOUNDED WAIT FOR ITS LOCKS: the ALTERs take ACCESS EXCLUSIVE on two tables the jobs write
-- every minute; a tick holding them fails this migration rather than queueing every reader
-- behind it.

SET LOCAL lock_timeout = '5s';

ALTER TABLE mesh_embeddings ADD COLUMN IF NOT EXISTS chunk_no smallint NOT NULL DEFAULT 0;
ALTER TABLE mesh_embeddings ADD COLUMN IF NOT EXISTS namespace text NOT NULL DEFAULT 'shared/mesh-chat';
ALTER TABLE mesh_lexical    ADD COLUMN IF NOT EXISTS namespace text NOT NULL DEFAULT 'shared/mesh-chat';

UPDATE mesh_embeddings me SET namespace = sm.namespace
  FROM shared_memory sm WHERE sm.id = me.src_id AND me.namespace IS DISTINCT FROM sm.namespace;
UPDATE mesh_lexical ml SET namespace = sm.namespace
  FROM shared_memory sm WHERE sm.id = ml.src_id AND ml.namespace IS DISTINCT FROM sm.namespace;

ALTER TABLE mesh_embeddings DROP CONSTRAINT mesh_embeddings_pkey;
ALTER TABLE mesh_embeddings ADD CONSTRAINT mesh_embeddings_pkey PRIMARY KEY (src_id, space, chunk_no);

-- A mail row has exactly one vector: the mail search ranks rows by their one vector, and a
-- second chunk of a mail row would be a second, unranked opinion about it.
ALTER TABLE mesh_embeddings ADD CONSTRAINT mesh_embeddings_chunk_no_chk
    CHECK (chunk_no >= 0 AND (chunk_no = 0 OR namespace <> 'shared/mesh-chat'));

-- The indexes' loads: a space's rows of one namespace, newest first (full load) or after a
-- watermark (incremental). And the graph walker's set of derived rows per namespace.
CREATE INDEX IF NOT EXISTS mesh_embeddings_space_ns_idx
    ON mesh_embeddings (space, namespace, embedded_at, src_id);
CREATE INDEX IF NOT EXISTS mesh_lexical_space_ns_idx
    ON mesh_lexical (space, namespace, indexed_at, src_id);

COMMENT ON COLUMN mesh_embeddings.chunk_no IS
  'Divergence 060: 0 for a mail row (always one vector); 0..n-1 for a chunked graph row (apps/meshsem/graphtext.py).';
COMMENT ON COLUMN mesh_embeddings.namespace IS
  'Divergence 060: the namespace of the source row, so an index loads one namespace (apps/meshsem/search.VectorIndex).';
COMMENT ON COLUMN mesh_lexical.namespace IS
  'Divergence 060: the namespace of the source row, so an index loads one namespace (apps/meshsem/lexical.LexicalIndex).';
