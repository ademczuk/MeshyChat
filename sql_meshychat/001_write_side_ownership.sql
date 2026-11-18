-- MeshyChat divergence 001: close the write side of row-level security.
--
-- WHY. Measured on the inherited schema, as role meshy_app with NO peer context bound:
--
--   SELECT   0 rows visible        -> the READ side fails closed, correctly
--   INSERT   SUCCEEDED, attributing a row to ANOTHER PEER
--   UPDATE   0 rows                -> protected only INCIDENTALLY, because the rows are
--   DELETE   0 rows                   invisible to the read that finds them
--
-- Every INSERT policy on the tenant tables is `WITH CHECK (true)`. So row-level security
-- was protecting reads only: any application bug on a write path crosses tenants and
-- raises nothing: a write path that forgets to set context goes unnoticed, and a
-- permission check that takes no key lets any namespace writer overwrite another
-- peer's row.
--
-- WHAT THIS DOES. Requires an inserted row to be attributed to the peer the connection
-- is actually bound to. A contextless connection can then insert NOTHING, instead of
-- inserting anything it likes. Reads are untouched.
--
-- WHY IT IS A SEPARATE DIRECTORY. `sql/` is the upstream migration set, applied verbatim
-- and checksummed, so the two servers stay schema-identical by construction. Divergences
-- live here, are applied after it, and are listed in docs/DIVERGENCES.md. Never edit an
-- applied upstream file: its checksum is what a running database remembers.
--
-- COMPATIBILITY. The Rust server binds `app.current_peer_id` on the same connections
-- (meshy-db/src/peer_context.rs), so it satisfies these policies unchanged. The one path
-- that does not is a server-internal write, which is why each policy also admits
-- `app.server_internal = '1'` - the same escape the read policies already use.

BEGIN;

-- local_memory: peer-owned rows.
ALTER POLICY local_memory_internal_insert ON local_memory
    WITH CHECK (
        current_setting('app.server_internal', true) = '1'
        OR peer_id::text = current_setting('app.current_peer_id', true)
    );

ALTER POLICY local_memory_internal_update ON local_memory
    USING (
        current_setting('app.server_internal', true) = '1'
        OR peer_id::text = current_setting('app.current_peer_id', true)
    )
    WITH CHECK (
        current_setting('app.server_internal', true) = '1'
        OR peer_id::text = current_setting('app.current_peer_id', true)
    );

-- skill_calls: a call belongs to its caller. The target is written by the server, so the
-- server-internal escape carries that path.
ALTER POLICY skill_calls_internal_insert ON skill_calls
    WITH CHECK (
        current_setting('app.server_internal', true) = '1'
        OR caller_peer_id::text = current_setting('app.current_peer_id', true)
    );

-- meshy_memory: tenant-scoped by OWNER rather than peer, matching its select policy
-- (meshy_same_tenant_or_devgroup on app.current_owner_id).
ALTER POLICY meshy_memory_internal_insert ON meshy_memory
    WITH CHECK (
        current_setting('app.server_internal', true) = '1'
        OR created_by_owner_id::text = current_setting('app.current_owner_id', true)
    );

COMMIT;
