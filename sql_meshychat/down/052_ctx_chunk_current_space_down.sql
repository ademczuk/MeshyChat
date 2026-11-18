-- DOWN for sql_meshychat/052. `sqlmigrate.py --rollback-to meshychat:51` runs it. Derived state only: 051's two
-- partial indexes come back and 052's go. No row changes.
SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_current_seat_time ON mesh_ctx_chunks (namespace, seat, time_last)
    WHERE current;
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_current_seat_src ON mesh_ctx_chunks (namespace, seat, src, time_last)
    WHERE current;

DROP INDEX IF EXISTS mesh_ctx_chunks_current_space_seat_src;
DROP INDEX IF EXISTS mesh_ctx_chunks_current_space_seat_time;
