-- MeshyChat divergence 022: only a background job may WRITE a projection table.
--
-- WHAT WAS WRONG. Every table derived from shared_memory carried one policy,
--
--     FOR ALL USING (app.server_internal = '1') WITH CHECK (app.server_internal = '1')
--
-- (sql_meshychat/004:128-140, copied by 006 and 007), and the mesh read views already
-- run inside `dbconn.request_server_internal`, because shared_memory itself is readable
-- only in that context. So "no request path writes a projection" (divergence 014) was a
-- CONVENTION: a view that ran an INSERT inside the context it already held would have
-- written mesh_edges, and the database would have agreed. Every derived table added
-- since (the vectors, the inferred edges, each one's watermark row) copied that shape.
--
-- WHAT THIS DOES. For each of the six tables below the single FOR ALL policy becomes
-- four. SELECT keeps the server_internal rule unchanged, so every read path behaves as
-- before. INSERT, UPDATE and DELETE additionally require `app.projection_writer = '1'`,
-- which the code sets in exactly ONE place, `apps.core.rls.projection_writer()`, entered
-- only by the job bodies (apps/mesh/projection.py, apps/meshsem/embedjob.py,
-- apps/meshaudit/derive.py on a --write run). tests/test_projection_writer.py holds all
-- three halves: the database's refusal, the code's single setter, and the call sites.
--
-- WHAT A REFUSAL LOOKS LIKE, because the three commands differ. An INSERT that fails
-- WITH CHECK RAISES (SQLSTATE 42501, "new row violates row-level security policy").
-- An UPDATE or DELETE whose USING is false does not raise: the rows are invisible to
-- it and it touches zero of them. Both are closed; only the insert is loud. A statement
-- trigger could make the other two loud as well, and is deliberately not added: the
-- ON DELETE CASCADE from shared_memory runs a DELETE on these tables, and a trigger that
-- refused it would break retention. RLS does not stop the referential action, so the
-- cascade still works: measured as meshy_app in the REQUEST context, where deleting a
-- message removes its mesh_edges, mesh_embeddings and mesh_inferred_edges rows
-- (tests/test_projection_writer.py test_a_cascade_from_shared_memory_...).
--
-- THE FLAG IS NOT A SECRET, AND THIS IS NOT A PRIVILEGE BOUNDARY AGAINST HOSTILE CODE.
-- Any code holding a meshy_app connection can call set_config. What this changes is
-- that writing a projection from a request now takes a deliberate, greppable act that a
-- test refuses, instead of being the default outcome of the context every read already
-- holds. That is the difference between a convention and a control this repo's design
-- rules ask for (V1-DESIGN-RULES "a convention was trusted to hold something a
-- mechanism should have held").
--
-- NOT CHANGED: mesh_route_usage (005). It is a primary record of requests, not a
-- projection, and the request is its only writer. background_job_leases (002) has no RLS.
--
-- meshy_app's table grants are unchanged (sql/010 default privileges).

-- ------------------------------------------------------------------ mesh_edges (004)
DROP POLICY IF EXISTS mesh_edges_server_internal ON mesh_edges;
CREATE POLICY mesh_edges_read ON mesh_edges
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_edges_insert ON mesh_edges
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_edges_update ON mesh_edges
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_edges_delete ON mesh_edges
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');

-- ------------------------------------------------------- mesh_edges_projection (004)
DROP POLICY IF EXISTS mesh_edges_projection_server_internal ON mesh_edges_projection;
CREATE POLICY mesh_edges_projection_read ON mesh_edges_projection
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_edges_projection_insert ON mesh_edges_projection
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_edges_projection_update ON mesh_edges_projection
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_edges_projection_delete ON mesh_edges_projection
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');

-- ------------------------------------------------------------- mesh_embeddings (006)
DROP POLICY IF EXISTS mesh_embeddings_server_internal ON mesh_embeddings;
CREATE POLICY mesh_embeddings_read ON mesh_embeddings
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_embeddings_insert ON mesh_embeddings
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_embeddings_update ON mesh_embeddings
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_embeddings_delete ON mesh_embeddings
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');

-- -------------------------------------------------- mesh_embeddings_projection (006)
DROP POLICY IF EXISTS mesh_embeddings_projection_server_internal ON mesh_embeddings_projection;
CREATE POLICY mesh_embeddings_projection_read ON mesh_embeddings_projection
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_embeddings_projection_insert ON mesh_embeddings_projection
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_embeddings_projection_update ON mesh_embeddings_projection
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_embeddings_projection_delete ON mesh_embeddings_projection
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');

-- --------------------------------------------------------- mesh_inferred_edges (007)
DROP POLICY IF EXISTS mesh_inferred_edges_server_internal ON mesh_inferred_edges;
CREATE POLICY mesh_inferred_edges_read ON mesh_inferred_edges
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_inferred_edges_insert ON mesh_inferred_edges
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_inferred_edges_update ON mesh_inferred_edges
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_inferred_edges_delete ON mesh_inferred_edges
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');

-- -------------------------------------------------------- mesh_inferred_audits (007)
DROP POLICY IF EXISTS mesh_inferred_audits_server_internal ON mesh_inferred_audits;
CREATE POLICY mesh_inferred_audits_read ON mesh_inferred_audits
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_inferred_audits_insert ON mesh_inferred_audits
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_inferred_audits_update ON mesh_inferred_audits
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_inferred_audits_delete ON mesh_inferred_audits
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');
