-- DOWN for sql_meshychat/049. `sqlmigrate.py --rollback-to meshychat:48` runs it. The counter is a freshness stamp only:
-- dropping it loses nothing, and the code before 049 never reads it.
SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS mesh_ctx_chunks_changed ON mesh_ctx_chunks;
DROP FUNCTION IF EXISTS public.meshy_ctx_chunk_changed();
DROP TABLE IF EXISTS ctx_chunk_changes;
