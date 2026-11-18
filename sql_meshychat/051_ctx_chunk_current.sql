-- MeshyChat, v1.1 switch-on blocker 5 (pxx, e0cf57; codex-pxx-cli TASK AQ): a CURRENT flag on mesh_ctx_chunks,
-- so the request path reads only the newest indexed generation's rows instead of walking the superseded ones.
--
-- Older generations stay stored (apps/meshsem/ctxindex.py: retention is by age, not by gen). Search serves only the
-- newest indexed gen of each (seat, src), and it decided that row by row (_LATEST_GEN_SQL) AFTER the filters' index had
-- already produced the row, so the subset count, the shard probes (_matching) and the lexical arm read every
-- superseded row in the ranges they walked. `current` is true exactly when a row's gen is its source's newest indexed
-- gen (ctx_source_gen.gen). Partial indexes on current rows let those reads skip the rest. The latest-gen check stays
-- in the predicate beside it, so a row written without the flag set (tests that write chunks directly) is still
-- filtered as before.
--
-- WHO KEEPS IT: apps/meshsem/ctxjob._write_manifest, in the transaction that upserts ctx_source_gen (the only writer of
-- that table, so every path that records a gen, main walk, replay and re-walk included). It flips to false every row of
-- the source whose gen is below the gen the upsert LEAVES (RETURNING gen), which also catches a manifest of an older gen
-- written after a newer one was recorded: its rows go in with DEFAULT true and are flipped in the same transaction. It
-- commits with the chunk writes and the upsert under the space lock, so a crash leaves all of it or none of it, and
-- no repair sweep is needed. The flip is an UPDATE, so 049's `updates` counter moves and resident shards reload.
--
-- BACKFILL: every row whose gen is not its source's newest indexed gen becomes false (ctx_source_gen first, else the
-- greatest gen any chunk carries: _LATEST_GEN_SQL's rule). Idempotent. Production holds no chunks while
-- MESHY_CONTEXT_INDEX is off. Down: down/051.
--
-- DEPLOY AND ROLLBACK ORDER (pxx, codex-pxx-cli TASK AR): code from this file on reads `c.current`, and a running
-- process does not recheck the schema after a migration. So deploy it the way 047 to 050 were: MAINTENANCE set and the
-- server, the job ticker and the call worker STOPPED before the migration, then the new code started (HQD's
-- redeploy_prod_migrate.ps1 does this, and refuses unless the pending set is exactly the approved one). Roll back the
-- same way in reverse: stop everything, run down/051, start code from before 051. Code from before 051 must not run
-- against the migrated schema either: it would insert rows with DEFAULT true and never retire the older ones (the
-- latest-gen check still keeps them from being served, but the read bound would drift).

SET LOCAL lock_timeout = '5s';

ALTER TABLE mesh_ctx_chunks ADD COLUMN IF NOT EXISTS current boolean NOT NULL DEFAULT true;

-- backfill: begin
UPDATE mesh_ctx_chunks c SET current = false
 WHERE c.current
   AND c.gen <> COALESCE(
       (SELECT s.gen FROM ctx_source_gen s WHERE s.space = c.space AND s.namespace = c.namespace AND s.seat = c.seat
           AND s.src = c.src),
       (SELECT max(g.gen) FROM mesh_ctx_chunks g WHERE g.space = c.space AND g.namespace = c.namespace
           AND g.seat = c.seat AND g.src = c.src));
-- backfill: end

-- The filters' two shapes over current rows: (namespace, seat, time_last) for the subset count, the shard probes'
-- time and id ranges and the lexical arm's join; (namespace, seat, src) for a src-filtered request.
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_current_seat_time ON mesh_ctx_chunks (namespace, seat, time_last)
    WHERE current;
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_current_seat_src ON mesh_ctx_chunks (namespace, seat, src, time_last)
    WHERE current;
