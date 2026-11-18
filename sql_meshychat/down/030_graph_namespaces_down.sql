-- DOWN for sql_meshychat/030 (divergence 060, OI-57). `sqlmigrate.py --rollback-to meshychat:29` runs it
-- (the rollback gate, apps/core/rollback.py), newest first. tests/test_rollback_gate.py holds it to the
-- catalog 030 found.
--
-- ORDER: roll the code back first. The graph walker writes chunk_no and namespace; on the pre-030
-- schema its insert fails every tick, which the runner records and backs off from.
--
-- WHAT COMES BACK: the one-vector-per-row key (src_id, space) and both tables without the columns.
-- WHAT IS LOST: every vector and lexical row of a namespace other than shared/mesh-chat, and every
-- chunk past 0. They are derived rows: the pre-030 code never reads them, and a re-applied 030 has the
-- walker rebuild them from the rows they came from.
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 030 has.

SET LOCAL lock_timeout = '5s';

DELETE FROM mesh_embeddings WHERE chunk_no > 0 OR namespace <> 'shared/mesh-chat';
DELETE FROM mesh_lexical WHERE namespace <> 'shared/mesh-chat';

DROP INDEX IF EXISTS mesh_embeddings_space_ns_idx;
DROP INDEX IF EXISTS mesh_lexical_space_ns_idx;
ALTER TABLE mesh_embeddings DROP CONSTRAINT IF EXISTS mesh_embeddings_chunk_no_chk;
ALTER TABLE mesh_embeddings DROP CONSTRAINT mesh_embeddings_pkey;
ALTER TABLE mesh_embeddings ADD CONSTRAINT mesh_embeddings_pkey PRIMARY KEY (src_id, space);
ALTER TABLE mesh_embeddings DROP COLUMN IF EXISTS chunk_no;
ALTER TABLE mesh_embeddings DROP COLUMN IF EXISTS namespace;
ALTER TABLE mesh_lexical DROP COLUMN IF EXISTS namespace;

DELETE FROM meshychat_migrations WHERE version = 30;
