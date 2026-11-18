-- DOWN for sql_meshychat/051. `sqlmigrate.py --rollback-to meshychat:50` runs it. Derived state: without the flag the
-- request path filters superseded generations row by row again (the code before 051 does).
SET LOCAL lock_timeout = '5s';

DROP INDEX IF EXISTS mesh_ctx_chunks_current_seat_src;
DROP INDEX IF EXISTS mesh_ctx_chunks_current_seat_time;
ALTER TABLE mesh_ctx_chunks DROP COLUMN IF EXISTS current;
