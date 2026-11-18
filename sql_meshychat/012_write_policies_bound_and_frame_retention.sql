-- MeshyChat divergence 029: bind every UPDATE/DELETE policy to a context, give work_pool's
-- INSERT to the server alone, and make frame retention delete by the frame's own identity,
-- never a live one's.
--
-- WHAT WAS WRONG (measured on a database freshly migrated from c5457ac, pg_policies, 55
-- policies). 001 closed the INSERT side of #149 and local_memory's UPDATE. It left eight
-- write policies that admit every row:
--
--     meshy_memory_internal_update / _delete    USING (true)        sql/039:44-45
--     local_memory_internal_delete             USING (true)        sql/096:19
--     skill_calls_internal_update / _delete    USING (true)        sql/006:70, :74
--     work_pool_internal_update / _delete      USING (true)        sql/103:85, :87
--     work_pool_internal_insert                WITH CHECK (true)   sql/103:83-84
--
-- Postgres adds a table's SELECT policy to an UPDATE/DELETE only when the statement READS a
-- column, so a qualified statement was confined incidentally. The unqualified one was not:
-- as meshy_app, `DELETE FROM meshy_memory` with no context, or bound to owner A, removed both
-- tenants' rows; the same held for every table above. On meshy_memory the qualified form
-- leaked too, between dev-group owners, whose SELECT policy admits each other's rows. And a
-- connection bound to peer A inserted a work_pool row claiming caller B.
--
-- WHAT THIS DOES. Each predicate admits exactly the contexts a traced writer uses (call
-- sites in apps/ and in the Rust server that shares this schema, listed in
-- tests/test_rls_update_delete_scope.py):
--
--   * meshy_memory UPDATE/DELETE and local_memory DELETE take 001's shape for the same
--     table: server_internal OR the row's owner (created_by_owner_id = app.current_owner_id)
--     or peer (peer_id = app.current_peer_id). Exact ownership, not the dev-group read
--     predicate: a group member may READ a row, only its owner may change it.
--   * skill_calls and work_pool UPDATE/DELETE: server_internal ONLY. Every writer in both
--     servers runs there, none runs peer-bound, and each row is two parties' record (the
--     caller's signed args, the target's or claimer's result), so neither peer owns it.
--   * work_pool INSERT: server_internal ONLY as well. Nothing in either server writes the
--     table, and the design computes tenant_key on the server, "never from the request
--     body", and sets caller_owner_id there too. A caller-bound check (001's shape) was
--     the first version; bound to its own peer, a post still chose caller_owner_id,
--     tenant_key (so it could take another tenant's idempotency key: work_pool_idem_uidx)
--     and the claim columns. A future POST runs in server_internal, like every skill_calls
--     insert.
--
-- WHAT A REFUSAL LOOKS LIKE. An INSERT, or an UPDATE whose new row fails WITH CHECK, raises
-- InsufficientPrivilege. An UPDATE/DELETE whose USING is false touches zero rows, silently.
-- Referential actions (ON DELETE CASCADE / SET NULL from peers and dashboard_users) are not
-- subject to RLS and still reach these rows; the test file measures it.
--
-- FRAME RETENTION (STATUS D07). sql/111 ships prune_invocation_frames, SECURITY DEFINER, and
-- grants it to meshy_app (:121). Its chained pass deletes every frame of a chain once the
-- chain's newest frame is past the cutoff, keyed on chain_id, which is the ROOT GRANT id
-- (sql/060:24-27, sql/095:12): an idle, never-expiring grant loses its whole audit history.
-- The production rule is that retention never deletes by a key that is also a live object's
-- identity; production stopped this with a REVOKE on that database only, and every fresh
-- install from this repository re-granted it. This file (1) replaces the body so the only
-- rows it can delete are STANDALONE frames (chain_id IS NULL), selected by their own id
-- under an age bound, and (2) revokes EXECUTE from meshy_app (and re-asserts PUBLIC), so the
-- append-only posture 062 gave the table holds against the app role. A chained frame cannot
-- be deleted by its own id without cutting its chain's head, which verify_chain reads as
-- tampering (111's header), so chained frames have no retention path until a design
-- re-anchors a chain. Nothing under apps/ or tools/ calls the function; the upstream janitor
-- that does now gets permission denied as meshy_app, which is production's posture.
--
-- Never edit an applied file in sql/: 039, 096, 006, 103 and 111 stay byte-identical.

BEGIN;

ALTER POLICY meshy_memory_internal_update ON meshy_memory
    USING (
        current_setting('app.server_internal', true) = '1'
        OR created_by_owner_id::text = current_setting('app.current_owner_id', true)
    )
    WITH CHECK (
        current_setting('app.server_internal', true) = '1'
        OR created_by_owner_id::text = current_setting('app.current_owner_id', true)
    );

ALTER POLICY meshy_memory_internal_delete ON meshy_memory
    USING (
        current_setting('app.server_internal', true) = '1'
        OR created_by_owner_id::text = current_setting('app.current_owner_id', true)
    );

ALTER POLICY local_memory_internal_delete ON local_memory
    USING (
        current_setting('app.server_internal', true) = '1'
        OR peer_id::text = current_setting('app.current_peer_id', true)
    );

ALTER POLICY skill_calls_internal_update ON skill_calls
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER POLICY skill_calls_internal_delete ON skill_calls
    USING (current_setting('app.server_internal', true) = '1');

ALTER POLICY work_pool_internal_insert ON work_pool
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER POLICY work_pool_internal_update ON work_pool
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER POLICY work_pool_internal_delete ON work_pool
    USING (current_setting('app.server_internal', true) = '1');

-- Same signature and return type as sql/111, so the call shape is unchanged; CREATE OR
-- REPLACE keeps the owner and the ACL, which the REVOKEs below then narrow.
CREATE OR REPLACE FUNCTION prune_invocation_frames(
    retain_days INTEGER,
    batch_size  INTEGER
) RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    deleted BIGINT;
BEGIN
    IF retain_days IS NULL OR retain_days < 1 THEN
        RAISE EXCEPTION 'prune_invocation_frames: retain_days must be >= 1, got %', retain_days
            USING ERRCODE = 'check_violation';
    END IF;
    IF batch_size IS NULL OR batch_size < 1 OR batch_size > 50000 THEN
        RAISE EXCEPTION 'prune_invocation_frames: batch_size must be in [1, 50000], got %', batch_size
            USING ERRCODE = 'check_violation';
    END IF;

    -- The frame's own identity (id) under an age bound. Never chain_id, call_id or any other
    -- column that names a live object: a chained frame is never selected here.
    WITH victims AS (
        SELECT id FROM invocation_frames
        WHERE chain_id IS NULL
          AND created_at < NOW() - make_interval(days => retain_days)
        ORDER BY id
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM invocation_frames WHERE id IN (SELECT id FROM victims);
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END;
$$;

REVOKE EXECUTE ON FUNCTION prune_invocation_frames(INTEGER, INTEGER) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION prune_invocation_frames(INTEGER, INTEGER) FROM meshy_app;

COMMIT;
