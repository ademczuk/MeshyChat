-- MeshyChat, v1.1 switch-on blocker 5, second half (pxx, on #411): the partial CURRENT indexes lead on space.
--
-- 051's two partial indexes are (namespace, seat[, src], time_last) WHERE current. Every request filter also says
-- c.space = (ctxindex.Filters.sql), and neither index carries space. So when space looks selective to the planner (a
-- table holding more than one space: a candidate space indexing beside the serving one, 047's per-space gen, or a
-- test database full of other tests' spaces), it ANDs in mesh_ctx_chunks_digest, which is (space, text_sha256) and NOT
-- partial (047). A Bitmap Index Scan on that index returns every entry of the space, superseded rows included, before
-- the subset count's LIMIT applies. Reproduced by pxx on PG16 (0706c6): the digest scan read 52 then 172 entries for
-- 40 + 12 then 160 + 12 rows, so 051's "the count never walks superseded rows" held only while the plan stayed off it.
-- With space first, one partial index answers space, namespace and seat, and the planner has no column left to AND in.
--
-- The same two shapes as 051, space first: (space, namespace, seat, time_last) for the subset count, the shard probes'
-- time and id ranges and the lexical arm's join; (space, namespace, seat, src, time_last) for a src-filtered request.
-- 051's indexes are then redundant (every reader filters on space) and are dropped, so a row is not maintained twice.
--
-- BUILD: plain CREATE INDEX inside the migration's transaction, not CONCURRENTLY (which cannot run in one). Measured
-- on production 2026-10-25 21:10Z (ro_query.py): mesh_ctx_chunks holds 0 rows, 80 kB, while the uploaders are off, so
-- the build is instant. Re-measure before deploying: if chunks have arrived, size the lock window first.
-- DEPLOY: the migrating route (redeploy_prod_migrate.ps1 -ExpectPending '52'). The code does not name these indexes,
-- so code from before and after 052 both run against either schema; only plans change. Down: down/052.

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_current_space_seat_time
    ON mesh_ctx_chunks (space, namespace, seat, time_last) WHERE current;
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_current_space_seat_src
    ON mesh_ctx_chunks (space, namespace, seat, src, time_last) WHERE current;

DROP INDEX IF EXISTS mesh_ctx_chunks_current_seat_time;
DROP INDEX IF EXISTS mesh_ctx_chunks_current_seat_src;
