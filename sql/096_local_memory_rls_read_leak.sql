-- Migration 096: local_memory read-plane peer scoping (closes a day-one read leak).
--
-- local_memory_internal_write (migration 012) was declared FOR ALL USING(true), and
-- Postgres applies a FOR ALL policy to every command including SELECT. Multiple
-- permissive policies for the same command are OR-combined, so the effective SELECT
-- predicate on local_memory collapsed to
--   (peer_id = current_setting('app.current_peer_id')) OR (true)
-- which is always true: any connection can read every peer's supposedly-private local
-- memory, defeating the "per-peer-private, CASCADE-deleted with the peer" invariant
-- this table exists for. This is the exact bug class already diagnosed and fixed for
-- the sibling meshy_memory table in migration 039 ("Rescope reads off USING(true)...
-- closes the day-one read leak") - local_memory was created in the same original
-- migration (012) with the identical shape and was never patched.

DROP POLICY IF EXISTS local_memory_internal_write ON local_memory;

CREATE POLICY local_memory_internal_insert ON local_memory FOR INSERT WITH CHECK (true);
CREATE POLICY local_memory_internal_update ON local_memory FOR UPDATE USING (true) WITH CHECK (true);
CREATE POLICY local_memory_internal_delete ON local_memory FOR DELETE USING (true);

-- local_memory_peer_select (migration 012) is now the SOLE policy governing SELECT.

-- DOWN:
--   DROP POLICY IF EXISTS local_memory_internal_insert ON local_memory;
--   DROP POLICY IF EXISTS local_memory_internal_update ON local_memory;
--   DROP POLICY IF EXISTS local_memory_internal_delete ON local_memory;
--   CREATE POLICY local_memory_internal_write ON local_memory FOR ALL USING (true) WITH CHECK (true);
