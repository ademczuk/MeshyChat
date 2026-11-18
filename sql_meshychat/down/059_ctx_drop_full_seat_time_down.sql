-- DOWN for sql_meshychat/059. `sqlmigrate.py --rollback-to meshychat:58` runs it. Derived state only: 047's full
-- index comes back. No row changes.
-- BUILD: plain CREATE INDEX inside the migration's transaction. NOT MEASURED at production size (915,418 current and
-- 1,330 superseded rows on Wed 11 Nov 2026): it blocks writes to mesh_ctx_chunks while it builds; measure it on a
-- restore copy before using it on production.
SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_seat_time ON mesh_ctx_chunks (namespace, seat, time_last);
