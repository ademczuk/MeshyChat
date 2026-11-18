-- Migration 039: meshy_memory read-plane tenant scoping (closes the day-one read leak).
--
-- THE control is the application-layer gate in get_meshy/list_meshy (meshy-memory
-- service): the live daemon connects as the superuser role `meshy` (rolbypassrls=t),
-- so RLS on meshy_memory is BYPASSED for the live path. This migration ships:
--   1. a single-source-of-truth tenant predicate FUNCTION (so the authorize.rs
--      invoke gate, the meshy_memory read gate, and the dashboard visibility CTE
--      all call ONE definition and cannot drift), and
--   2. a rescope of the SELECT policy off USING(true) - defense-in-depth that
--      becomes load-bearing only when the daemon is moved to the non-super
--      `meshy_app` role (recommended follow-up, separate PR).
-- Pairs with 038 (plane/trust_group_id) and the authorize.rs dev-group clause.

-- Single source of truth. caller may touch target's data IFF same owner, or both
-- are dev-plane sharing one non-null trust group. Product owners are self-isolated.
-- SECURITY DEFINER (owner = meshy) so the function can read dashboard_users even
-- when invoked from the RLS policy under the non-super meshy_app role; pinned
-- search_path closes the classic definer-function hijack. Read-only + fully
-- parameterized (two uuids), so no injection or escalation surface.
CREATE OR REPLACE FUNCTION meshy_same_tenant_or_devgroup(caller uuid, target uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT caller = target
        OR EXISTS (
            SELECT 1
            FROM dashboard_users c, dashboard_users t
            WHERE c.id = caller AND t.id = target
              AND c.plane = 'dev' AND t.plane = 'dev'
              AND c.trust_group_id IS NOT NULL
              AND c.trust_group_id = t.trust_group_id
        );
$$;

-- Rescope reads off USING(true). The old write policy was FOR ALL, which also
-- permitted SELECT; split it so ONLY the scoped policy governs reads.
DROP POLICY IF EXISTS meshy_memory_public_select ON meshy_memory;
DROP POLICY IF EXISTS meshy_memory_internal_write ON meshy_memory;

CREATE POLICY meshy_memory_tenant_select ON meshy_memory FOR SELECT
    USING (meshy_same_tenant_or_devgroup(
        NULLIF(current_setting('app.current_owner_id', true), '')::uuid,
        created_by_owner_id));

CREATE POLICY meshy_memory_internal_insert ON meshy_memory FOR INSERT WITH CHECK (true);
CREATE POLICY meshy_memory_internal_update ON meshy_memory FOR UPDATE USING (true) WITH CHECK (true);
CREATE POLICY meshy_memory_internal_delete ON meshy_memory FOR DELETE USING (true);

-- DOWN:
--   DROP POLICY IF EXISTS meshy_memory_tenant_select ON meshy_memory;
--   DROP POLICY IF EXISTS meshy_memory_internal_insert ON meshy_memory;
--   DROP POLICY IF EXISTS meshy_memory_internal_update ON meshy_memory;
--   DROP POLICY IF EXISTS meshy_memory_internal_delete ON meshy_memory;
--   CREATE POLICY meshy_memory_public_select ON meshy_memory FOR SELECT USING (true);
--   CREATE POLICY meshy_memory_internal_write ON meshy_memory FOR ALL USING (true) WITH CHECK (true);
--   DROP FUNCTION IF EXISTS meshy_same_tenant_or_devgroup(uuid, uuid);
