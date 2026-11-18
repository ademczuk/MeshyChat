-- 111_call_and_frame_retention.sql
--
-- #407: nothing deletes from skill_calls or invocation_frames outside tests,
-- so both grow without bound. invocation_frames was measured at 2.6M rows /
-- 1856 MB, growing about 65,000 rows a day. skill_calls has the same shape
-- (PR #385's read-result pruning clears the JSONB payload after an hour, it
-- never removes the row).
--
-- skill_calls carries no special grant restriction (010 grants meshy_app full
-- CRUD), so its retention is a plain batched DELETE from application code --
-- see CallsRepository::prune_completed_calls. skill_call_events and
-- skill_call_chunks FK ON DELETE CASCADE to skill_calls.call_id (007, 008),
-- so deleting the parent row cleans both children; no orphans.
--
-- invocation_frames is DIFFERENT. Migration 062 REVOKEs UPDATE/DELETE on it
-- from meshy_app specifically so the audit trail is append-only against the
-- app role, defense-in-depth for the hash-chain's tamper-evidence. Retention
-- must not reopen that grant. This migration instead follows the posture 040
-- and 063 already use for controlled mutation of an otherwise-frozen table: a
-- single named, audited SECURITY DEFINER function is the only door, owned by
-- the migration role rather than meshy_app, with EXECUTE (never DELETE)
-- granted narrowly.
--
-- WHOLE-CHAIN-OR-NOTHING, and this is the part worth over-documenting: a
-- chained frame's previous_frame_hash is a hash VALUE, not a foreign key, so
-- nothing at the DB layer stops a naive age-based delete from removing only
-- the oldest frames of an active chain. verify_chain (invocation_frames.rs)
-- selects all rows for a chain_id ordered by id and requires the FIRST row to
-- have previous_frame_hash = NULL (the root). Delete the true root out from
-- under a chain and the new first row still has a non-null
-- previous_frame_hash pointing at a hash that no longer exists -- verify_chain
-- reads that as ChainVerdict::Broken(LinkMismatch), a false tamper alert on
-- every chain retention ever touches. So this function only ever deletes a
-- chain WHOLE, and only once its newest frame has aged past the cutoff.
-- Standalone frames (chain_id IS NULL) have no chain to break and are pruned
-- individually by age.
--
-- Idempotent per tools/ci/MIGRATIONS.md (078+ rule).

CREATE OR REPLACE FUNCTION prune_invocation_frames(
    retain_days INTEGER,
    batch_size  INTEGER
) RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    deleted BIGINT := 0;
    n       BIGINT;
BEGIN
    IF retain_days IS NULL OR retain_days < 1 THEN
        RAISE EXCEPTION 'prune_invocation_frames: retain_days must be >= 1, got %', retain_days
            USING ERRCODE = 'check_violation';
    END IF;
    IF batch_size IS NULL OR batch_size < 1 OR batch_size > 50000 THEN
        RAISE EXCEPTION 'prune_invocation_frames: batch_size must be in [1, 50000], got %', batch_size
            USING ERRCODE = 'check_violation';
    END IF;

    -- Standalone frames: no chain_id, so no chain to break. Age-gated,
    -- bounded, SKIP LOCKED so a concurrent reader/writer never blocks this.
    WITH victims AS (
        SELECT id FROM invocation_frames
        WHERE chain_id IS NULL
          AND created_at < NOW() - make_interval(days => retain_days)
        ORDER BY id
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM invocation_frames WHERE id IN (SELECT id FROM victims);
    GET DIAGNOSTICS n = ROW_COUNT;
    deleted := deleted + n;

    -- Chained frames: batch_size here bounds the number of CHAINS considered
    -- per call, not rows -- a single call can delete more than batch_size
    -- rows if the selected chains are long. That is intentional: the cap
    -- exists to keep any one call's lock window short, not to bound total
    -- work, and the janitor calls this repeatedly.
    --
    -- NO SKIP LOCKED HERE, deliberately, unlike the standalone pass above.
    -- SKIP LOCKED skips PER ROW: if a concurrent transaction (a second
    -- retention sweep racing this one, in production this cannot happen
    -- because the janitor is a single background task, but the function is
    -- exposed to meshy_app generally) holds a lock on ONE frame of a
    -- selected chain, this DELETE would happily remove every OTHER frame of
    -- that chain and leave the locked one behind -- a chain deleted from the
    -- middle out, which is worse than the head-truncation the thirteen
    -- lines above this function already spend explaining. A plain FOR
    -- UPDATE blocks and waits for the whole chain instead, so a contended
    -- chain is deferred to the next call rather than partially destroyed.
    WITH stale_chains AS (
        SELECT chain_id
        FROM invocation_frames
        WHERE chain_id IS NOT NULL
        GROUP BY chain_id
        HAVING MAX(created_at) < NOW() - make_interval(days => retain_days)
        ORDER BY chain_id
        LIMIT batch_size
    ),
    victims AS (
        SELECT f.id
        FROM invocation_frames f
        JOIN stale_chains sc ON sc.chain_id = f.chain_id
        FOR UPDATE OF f
    )
    DELETE FROM invocation_frames WHERE id IN (SELECT id FROM victims);
    GET DIAGNOSTICS n = ROW_COUNT;
    deleted := deleted + n;

    RETURN deleted;
END;
$$;

-- CREATE FUNCTION grants EXECUTE to PUBLIC by default, unlike a table, which
-- carries no such default. Left alone, that default hands EVERY role able to
-- connect to this database -- not just meshy_app -- the ability to call this
-- function and delete audit chains AS THE OWNER, which is exactly the
-- capability 062:54 revoked DELETE to remove. Revoke first, grant narrowly.
REVOKE EXECUTE ON FUNCTION prune_invocation_frames(INTEGER, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION prune_invocation_frames(INTEGER, INTEGER) TO meshy_app;
