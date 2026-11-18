SET LOCAL lock_timeout = '5s';

-- The calls that reference ONE file, read past FORCE RLS by a narrow SECURITY DEFINER function (divergence 095).
--
-- apps/files/repository.find_referencing_calls runs as meshy_app under FORCE RLS on skill_calls (policy:
-- app.server_internal = '1'). Array containment (`@>`, arraycontains) is not LEAKPROOF, so Postgres will not use it as an
-- index condition ahead of the policy: the GIN indexes skill_calls_args_attachments_gin and ..._result_attachments_gin
-- are never used for it (idx_scan 0 on production), and every lookup scans the whole table. Measured 2026-10-10 on a
-- restore copy of production, as meshy_app with server_internal: 27 to 32 s, past the 30 s statement timeout, so every
-- presign by a reader who is not the file's owner answered 500 E_INTERNAL instead of its 404 E_FILE_NOT_FOUND. As meshy
-- (bypassrls) the same query uses both indexes in 16 to 53 ms. This function, owned by the migrating role (meshy), runs
-- the lookup as that role: 45 to 98 ms as meshy_app on the same copy, also as a prepared generic plan.
--
-- The policy no longer filters inside it, so the function is the whole boundary:
--   * it takes exactly one uuid and is STRICT (NULL in, nothing out);
--   * it returns only the four columns readable_row decides on: id, status, caller_peer_id, target_peer_id;
--   * search_path is pinned and every table is schema-qualified, so a caller's path cannot redirect it;
--   * EXECUTE is revoked from PUBLIC (granted by default) and granted to meshy_app alone.
--   * it answers only inside the server_internal binding: the body keeps the skipped policy's own predicate
--     (app.server_internal = '1') as a one-time filter, so meshy_app code outside the binding gets nothing
--     (memory-bxxxxx's review of #214). The GIN indexes are still used with it.
-- tests/test_files.py (the four tests after test_every_skill_calls_access_carries_the_positive_control) pins each of these.
CREATE OR REPLACE FUNCTION public.file_referencing_calls(p_file_id uuid)
RETURNS TABLE (id uuid, status text, caller_peer_id uuid, target_peer_id uuid)
LANGUAGE sql
STABLE
STRICT
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT r.id, r.status, r.caller_peer_id, r.target_peer_id
      FROM (
            SELECT s.id, s.status, s.caller_peer_id, s.target_peer_id
              FROM public.skill_calls s
             WHERE s.args_attachments @> ARRAY[p_file_id]
            UNION
            SELECT s.id, s.status, s.caller_peer_id, s.target_peer_id
              FROM public.skill_calls s
             WHERE s.result_attachments @> ARRAY[p_file_id]
           ) r
     WHERE current_setting('app.server_internal', true) = '1'
     LIMIT 100
$$;

REVOKE ALL ON FUNCTION public.file_referencing_calls(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.file_referencing_calls(uuid) TO meshy_app;
