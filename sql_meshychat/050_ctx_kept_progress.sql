-- MeshyChat, v1.1 step 7 follow-up: the orphan sweep leaves an UNFINISHED manifest's kept vectors alone, whatever their
-- age (Codex on #389, deferred to #396 by pxx).
--
-- When the tick's deadline cuts a manifest short, apps/meshsem/ctxjob._keep_vectors stores the vectors already embedded
-- ahead of their chunks, so the next tick resumes. No chunk names them until the manifest is written, so to the sweep
-- they are orphans, and ORPHAN_GRACE_S (one hour) was all that kept it off them: a manifest that needs more than an hour
-- of ticks lost its earliest work to every sweep and could never finish.
--
-- kept_seqs holds EVERY manifest seq with kept work not yet finished (codex-pxx-cli round 2, P2-1: a single seq let a
-- replay's marker replace an unfinished main manifest's), and kept_at when the oldest of them began keeping. A seq stays
-- in the set while it is still to walk: above the watermark with its ctx_commit_order row still present, or a dead
-- letter marked for replay (whose row cascades with its manifest too). Written, dead-lettered or GONE (P2-2: the
-- manifest deleted, its commit-order row cascaded away), it drops out at the next sweep, and when the set is empty both
-- columns clear and the grace alone applies again. While the set is not empty, the sweep spares every unreferenced
-- vector created at or after kept_at: that may spare more than the kept work, never less.
-- Derived state, as the rest of ctx_index_state. Down: down/050.

SET LOCAL lock_timeout = '5s';

ALTER TABLE ctx_index_state ADD COLUMN IF NOT EXISTS kept_seqs bigint[],
                            ADD COLUMN IF NOT EXISTS kept_at timestamptz;
