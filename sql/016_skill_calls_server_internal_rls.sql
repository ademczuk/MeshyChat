-- 016: server-internal RLS bypass for control-plane SELECTs on skill_calls.
--
-- Migration 006 put a peer-scoped SELECT policy on skill_calls so a peer sees
-- only calls where it is the caller or the target. That correctly scopes
-- PEER-FACING list/get reads (which additionally app-scope in SQL / via
-- assert_peer_can_see). But the server's OWN control-plane reads are not
-- peer-scoped, and none of them set app.current_peer_id:
--   * idempotency lookup by the globally-unique call_id (idempotency::check),
--   * the concurrent-call count for an OWNER (an owner has many peers),
--   * await_result, the target/caller callbacks (require_target / cancel_call),
--   * the deadline-sweep CTE's `SELECT ... FOR UPDATE`.
-- Under the production meshy_app role (NOSUPERUSER NOBYPASSRLS) the peer-scoped
-- policy evaluates `current_setting('app.current_peer_id', true)` = NULL for
-- every row, so these SELECTs return ZERO rows. That silently defeats
-- idempotency/replay protection and the concurrent_calls cap, and breaks result
-- delivery, callbacks, and the deadline sweeper. It is masked today only
-- because dev + CI connect as a superuser (RLS bypass).
--
-- Fix: add a second PERMISSIVE SELECT policy gated on a server-only,
-- transaction-local GUC (app.server_internal = '1'). PostgreSQL OR-combines
-- permissive policies, so peer-facing reads (which never set this GUC) keep
-- their peer-scoped RLS as defense-in-depth, while the server sets the GUC
-- (via meshy_db::with_server_internal, is_local = true) ONLY around its
-- control-plane reads. An external peer has no SQL session and cannot set it.
CREATE POLICY skill_calls_server_internal_select ON skill_calls
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
