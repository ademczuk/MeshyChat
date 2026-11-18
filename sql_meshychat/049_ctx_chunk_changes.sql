-- MeshyChat, v1.1 step 7 follow-up: a per-space CHANGE COUNTER for mesh_ctx_chunks, the shard plan cache's freshness stamp
-- (codex-pxx-cli on #396, P2-1 and P2-2).
--
-- apps/meshsem/ctxindex caches each (space, namespace)'s shard plan and recomputed it when the space's newest chunk id
-- moved. A delete does not move that id (retention, a head-only replacement, a cascade from shared_memory), so a warm plan
-- kept a deleted chunk's shard resident until PLAN_TTL_S passed (P2-1); and the newest-id read was an ORDER BY over a
-- column no index leads with for a space (P2-2). This counter answers both: ONE primary-key row per space, bumped by
-- every transaction that inserts, updates or deletes a chunk of that space, cascades included.
--
-- `updates` counts only the transactions that UPDATE a chunk of the space (codex-pxx-cli round 2, P2-3): an update can
-- change a resident row's vector or filter columns while its shard keeps the same id range and row count, so ctxindex
-- folds `updates` into each resident shard's version and reloads it. The walk never updates a chunk (it replaces rows
-- under new ids), so the shards stay resident across ordinary writes.
--
-- WHEN IT IS BUMPED: at the writer's COMMIT, by a DEFERRED constraint trigger (023's and 047's shape), and once per
-- (transaction, space): the first row of a space marks the transaction with a transaction-local setting and later rows
-- skip. So a walk tick that writes 1,000 chunks bumps once. A reader that sees the new count sees the rows: the bump
-- commits with them.
--
-- LOCKS: the counter rows are taken only from the commit-time trigger to the commit itself, after every chunk lock the
-- writer holds, so the walk's chunk deletes and retention's cannot deadlock through them (they could if the bump were
-- taken mid-transaction on ctx_index_state). A transaction touching TWO spaces would take two counter rows in its
-- event order, which another could take in the opposite order (codex-pxx-cli round 2): so the first bump of every
-- transaction first takes ONE transaction-scoped advisory lock, (1835233135, 1), and committing chunk writers serialize
-- there, from their first bump to their commit, before any counter row. That is a short window, after all their work.
--
-- WHO WRITES IT: only the SECURITY DEFINER trigger function, as ctx_commit_order (047): RLS enabled, not forced, read under
-- server_internal, and meshychat_app holds no INSERT, UPDATE or DELETE. TRUNCATE fires no row trigger; PLAN_TTL_S still
-- bounds a plan's age for that. Down: down/049.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS ctx_chunk_changes (
    space       text        PRIMARY KEY,
    changes     bigint      NOT NULL DEFAULT 0 CHECK (changes >= 0),
    updates     bigint      NOT NULL DEFAULT 0 CHECK (updates >= 0),
    changed_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE ctx_chunk_changes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ctx_chunk_changes_read ON ctx_chunk_changes;
CREATE POLICY ctx_chunk_changes_read ON ctx_chunk_changes
    FOR SELECT USING (current_setting('app.server_internal', true) = '1');
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ctx_chunk_changes FROM meshychat_app;

CREATE OR REPLACE FUNCTION public.meshy_ctx_chunk_changed() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    sp text;
    upd integer := CASE WHEN TG_OP = 'UPDATE' THEN 1 ELSE 0 END;
    seen_change boolean;
    seen_update boolean;
BEGIN
    FOREACH sp IN ARRAY CASE TG_OP WHEN 'INSERT' THEN ARRAY[NEW.space] WHEN 'DELETE' THEN ARRAY[OLD.space]
                                   ELSE ARRAY[OLD.space, NEW.space] END LOOP
        -- Once per (transaction, space), and once more for its first UPDATE: the settings are transaction-local.
        seen_change := current_setting('meshy.ctx_chunks_' || md5(sp), true) = '1';
        seen_update := upd = 0 OR current_setting('meshy.ctx_chunku_' || md5(sp), true) = '1';
        CONTINUE WHEN seen_change AND seen_update;
        IF current_setting('meshy.ctx_chunks_lock', true) IS DISTINCT FROM '1' THEN
            PERFORM pg_advisory_xact_lock(1835233135, 1);       -- one order for every committing chunk writer
            PERFORM set_config('meshy.ctx_chunks_lock', '1', true);
        END IF;
        PERFORM set_config('meshy.ctx_chunks_' || md5(sp), '1', true);
        IF upd = 1 THEN
            PERFORM set_config('meshy.ctx_chunku_' || md5(sp), '1', true);
        END IF;
        INSERT INTO public.ctx_chunk_changes AS c (space, changes, updates)
        VALUES (sp, CASE WHEN seen_change THEN 0 ELSE 1 END, CASE WHEN seen_update THEN 0 ELSE 1 END)
        ON CONFLICT (space) DO UPDATE
            SET changes = c.changes + CASE WHEN seen_change THEN 0 ELSE 1 END,
                updates = c.updates + CASE WHEN seen_update THEN 0 ELSE 1 END,
                changed_at = now();
    END LOOP;
    RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.meshy_ctx_chunk_changed() FROM PUBLIC;

DROP TRIGGER IF EXISTS mesh_ctx_chunks_changed ON mesh_ctx_chunks;
CREATE CONSTRAINT TRIGGER mesh_ctx_chunks_changed
    AFTER INSERT OR UPDATE OR DELETE ON mesh_ctx_chunks
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION public.meshy_ctx_chunk_changed();
