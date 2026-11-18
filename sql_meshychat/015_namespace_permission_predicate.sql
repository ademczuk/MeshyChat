-- MeshyChat divergence 040, slice 3 (B07, design docs/design/biscuit-capabilities.md 3.3 and
-- 3.5): ONE namespace predicate, meshy_namespace_permission, called by check_permission and by
-- the two SQL sites that decided the same question on copies of it.
--
-- WHY. docs/V1-DESIGN-RULES.md A2: "One authorisation predicate, implemented as a database
-- function, called by every layer and re-expressed by none." Until this migration the
-- namespace decision was written three times: apps/memoryskills/permissions.py
-- check_permission (two SELECTs and a comparison in Python), apps/memoryskills/handlers.py
-- _list_visible_namespaces (memory.list with no namespace) and apps/mesh/wait.py
-- _PEER_VIS_CTE (/v1/mesh/wait, the plain wait and divergence 031's addressed scan). A change
-- to one of them would have left the other two deciding the old way.
--
-- TWO FORMS OF ONE FUNCTION, AND THE PREDICATE IS WRITTEN ONCE.
--   meshy_namespace_permission(peer)             every namespace the peer owns or holds a
--                                               live grant on, with its answer: THE PREDICATE
--   meshy_namespace_permission(namespace, peer)  one namespace's answer, read from the form
--                                               above; 'unclaimed' is decided here
-- check_permission asks the second; memory.list's enumeration and the wait's visibility ask
-- the first, for the one peer they serve. The first build of this slice had only the second
-- and those two queries called it once per claimed namespace. Measured on the dev Postgres,
-- the wait's visibility went from 1.7 ms to 128 ms at 1,000 claimed namespaces and from 18 ms
-- to 3.8 s at 20,000 (a function that runs queries of its own costs about 0.14 ms a call
-- there), and the wait asks it every 2 s for each waiter. The first form is LANGUAGE sql,
-- STABLE and SECURITY INVOKER, one SELECT with no SET clause, so the planner inlines it into
-- the caller's query as a subquery that reads the two tables itself, as the copies did:
-- 6.3 ms against their 6.5 ms at 5,000 (divergence 040 has the numbers).
--
-- WHAT IT ANSWERS: exactly check_permission's decision before this slice.
--   'unclaimed'              no shared_memory_namespace row that the caller can see:
--                            check_permission raises E_NAMESPACE_UNCLAIMED, never
--                            E_PERMISSION_DENIED (the one-namespace form only)
--   'owner'                  the peer is the namespace's owner_peer_id: implicit admin,
--                            reported via='owner', whatever grant rows it also holds (the
--                            grant branch skips a namespace the peer owns)
--   'read'|'write'|'admin'   otherwise the permission of the NEWEST live grant row: revoked =
--                            FALSE AND (expires_at IS NULL OR expires_at > NOW()), newest by
--                            created_at (DISTINCT ON with ORDER BY created_at DESC, the set
--                            form of check_permission's ORDER BY created_at DESC LIMIT 1);
--                            reported via='grant'
--   NULL                     claimed, not the owner, no live grant row: E_PERMISSION_DENIED.
--                            The set form has no row for such a namespace.
-- The level order (read < write < admin) is NOT in here. Each caller compares the answer with
-- apps/memoryskills/permissions.answers_admitting(need), the one place the answers meet
-- PERM_LEVEL, so an answer this function does not name yet admits nothing anywhere.
--
-- THE NEWEST ROW CANNOT BE TOLD FROM ANY OTHER ON THIS SCHEMA. The partial unique index
-- shared_memory_grant_active_unique (sql/028) allows one row with revoked = FALSE per
-- (namespace, grantee), so at most one row is ever live and the ordering decides nothing
-- today. It is kept because it is the rule the design states and check_permission applied;
-- the test holds it by dropping the index inside a rolled-back transaction
-- (tests/test_namespace_permission_predicate.py).
--
-- SECURITY INVOKER, both forms, stated rather than left to the default: the lookups run as
-- the calling role, under that role's row-level security, as the three copies did. As
-- meshy_app that is sql/028's server_internal policy on both tables: outside
-- rls.server_internal() the function sees no claim and answers 'unclaimed', which is what
-- check_permission's own SELECT saw. SECURITY DEFINER would read as the function's owner, a
-- superuser that bypasses row-level security, and answer across that boundary: a security
-- change that needs its own review, and not this slice's. It would also stop the inlining.
--
-- STABLE: they read tables and NOW(), which is the transaction's start inside a function as
-- in its caller. PARALLEL SAFE: they only read, as the inline SQL they replace did. Not
-- STRICT: a NULL namespace finds no claim and answers 'unclaimed', as `namespace = NULL`
-- found none. No SET search_path: an invoker's function resolves the two tables through the
-- caller's path, as the caller's own SQL did, and a SET clause would stop the inlining. The
-- one-namespace form is plpgsql so that its plan is kept for the session.
--
-- EXECUTE: revoked from PUBLIC (CREATE FUNCTION grants PUBLIC by default) and granted to
-- meshy_app, the way sql/111 grants its meshy_app function. sql/010's default privileges would
-- give meshy_app EXECUTE only on a function created by the role that ran sql/010; the explicit
-- grant does not depend on which role applies this file.

CREATE OR REPLACE FUNCTION meshy_namespace_permission(peer uuid)
RETURNS TABLE (namespace text, permission text)
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY INVOKER
AS $$
    SELECT n.namespace, 'owner'::text
      FROM shared_memory_namespace n
     WHERE n.owner_peer_id = meshy_namespace_permission.peer
    UNION ALL
    (SELECT DISTINCT ON (g.namespace) g.namespace, g.permission
       FROM shared_memory_grant g
       JOIN shared_memory_namespace n ON n.namespace = g.namespace
      WHERE g.grantee_peer_id = meshy_namespace_permission.peer
        AND n.owner_peer_id <> meshy_namespace_permission.peer
        AND g.revoked = FALSE
        AND (g.expires_at IS NULL OR g.expires_at > NOW())
      ORDER BY g.namespace, g.created_at DESC)
$$;

CREATE OR REPLACE FUNCTION meshy_namespace_permission(namespace text, peer uuid)
RETURNS text
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
SECURITY INVOKER
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1
                     FROM shared_memory_namespace n
                    WHERE n.namespace = meshy_namespace_permission.namespace) THEN
        RETURN 'unclaimed';
    END IF;
    RETURN (SELECT a.permission
              FROM meshy_namespace_permission(meshy_namespace_permission.peer) a
             WHERE a.namespace = meshy_namespace_permission.namespace);
END;
$$;

REVOKE EXECUTE ON FUNCTION meshy_namespace_permission(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION meshy_namespace_permission(uuid) TO meshy_app;
REVOKE EXECUTE ON FUNCTION meshy_namespace_permission(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION meshy_namespace_permission(text, uuid) TO meshy_app;
