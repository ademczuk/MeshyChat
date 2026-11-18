SET LOCAL lock_timeout = '5s';

-- MeshyChat: drop 047's full mesh_ctx_chunks_seat_time (namespace, seat, time_last).
--
-- The shard probe (apps/meshsem/ctxindex.py, the EXISTS over a seat's month) filters c.space, c.namespace, c.current,
-- c.seat and a time_last range. 052's partial mesh_ctx_chunks_current_space_seat_time (space, namespace, seat,
-- time_last) WHERE current fits it and holds no superseded row; 047's full index fits it too and holds every row. Under
-- some statistics the planner took the full one and filtered superseded rows: tests/test_ctx_vector_store.py's plan
-- guard failed in gate mg598 (Index Scan rows=1 removed=165) and on main 1 run in 13 (pxx). At production
-- size that is a seat-month of history per probe.
--
-- The only other reader of that shape, apps/mesh/work.py INDEXED_TURN_SQL, now reads current rows of the configured
-- space, which 052's partial index serves. Every other seat-and-time reader already carries c.current and a space.
-- So nothing needs the full index, and the planner can no longer choose it.
--
-- LOCK: DROP INDEX takes an ACCESS EXCLUSIVE lock on mesh_ctx_chunks. It is brief, but it queues behind a running read
-- and every new read queues behind it; lock_timeout makes a busy table fail this step cleanly instead of stalling
-- search (pxx). Retry it in a quiet minute.
-- DEPLOY: the migrating route (redeploy_prod_migrate.ps1 -ExpectPending '59'). Code from before this migration still
-- runs against the new schema (its INDEXED_TURN_SQL then has no index of its shape and reads more), so deploy the code
-- and the migration together. Down: down/059.

DROP INDEX IF EXISTS mesh_ctx_chunks_seat_time;
