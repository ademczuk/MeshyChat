-- DOWN for sql_meshychat/047. `sqlmigrate.py --rollback-to meshychat:46` runs it. The index is derived data: every row
-- these tables hold is rebuilt by a re-walk from seq 0, so dropping them loses nothing the namespace itself does not hold.
SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS shared_memory_ctx_commit_order ON shared_memory;
DROP FUNCTION IF EXISTS public.meshy_ctx_commit_stamp();
DROP FUNCTION IF EXISTS public.ctx_vectors_orphaned(text);
DROP TABLE IF EXISTS ctx_dead_letter;
DROP TABLE IF EXISTS ctx_source_gen;
DROP TABLE IF EXISTS mesh_ctx_vectors;
DROP TABLE IF EXISTS mesh_ctx_chunks;
DROP TABLE IF EXISTS ctx_index_state;
DROP TABLE IF EXISTS ctx_commit_order;
DROP TABLE IF EXISTS ctx_index_config;
