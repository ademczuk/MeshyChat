-- MeshyChat divergence 028: one writer per mesh message, held by the database.
--
-- WHAT WAS MEASURED (live test server, 2026-09-23, c5457ac). Node P (peer 1baad537)
-- invoked memory.write on shared/mesh-chat with the key of a message node Q (peer
-- 4db41686) had written. The server accepted it and signed the result: version 2 became
-- the head with its body replaced. Version 1 stayed immutable (shared_memory_immutability
-- held), and that was the whole of the protection: the append-only chain let ANY writer
-- append the next version. That is the upstream memory daemon's shared key-value
-- semantics, and shared/wiki's CONFIRM depends on it, so it is changed for mesh messages
-- only.
--
-- WHAT THIS DOES. A BEFORE INSERT row trigger, for namespace 'shared/mesh-chat' and keys
-- LIKE 'msg/%' with version > 1, refuses the row when its created_by_peer_id differs from
-- that of the chain's lowest version (version 1: memory.prune keeps a mesh message's lowest
-- version), or when no earlier version of the key is visible at all.
-- apps/memoryskills/handlers.py refuses the same write first with the typed
-- E_NOT_MESSAGE_AUTHOR.
--
-- WHAT IT HOLDS, EXACTLY: a second writer APPENDING a version, by any code path. Nothing
-- else. It does not hold a version-1 insert (a forged `from` or author label on a new key
-- is the handler's R1 to R3), and it does not hold a DELETE of a key's chain followed by a
-- fresh version 1, which is how the ephemeral policy rewrites a key (the handler's R4 is
-- that path's only guard). A future path that inserts or deletes without the handler gets
-- this one guarantee from the database and must bring the rest itself.
--
-- WHAT IT DOES NOT TOUCH.
--   * existing rows: it fires on INSERT only, never on UPDATE or DELETE, so a chain
--     written before this migration (which can carry a second writer) is not rewritten or
--     refused. Its version-1 author can still write the next version.
--   * shared_memory_immutability (063) and shared_memory_root_parent_trg (064): separate
--     function, separate trigger, neither redefined. The head retirement is an UPDATE and
--     never reaches this function.
--   * version 1 inserts, including the ephemeral policy's latest-only rewrite (DELETE the
--     chain, insert a new version 1). The handler's R4 check is that path's guard; the
--     database cannot tell a rewrite from a new key without a DELETE trigger, and one is
--     deliberately not added (retention and cascades delete rows).
--
-- INVOKER RIGHTS, like the two sibling triggers: the lookup runs as the inserting role.
-- An insert as meshy_app is only possible in the server_internal context (sql/028 policy),
-- where every shared_memory row is visible. A lookup that finds NO earlier version while
-- version > 1 cannot happen through the handler (the root-parent trigger requires a parent
-- and the FK requires it to exist), and is refused rather than waved through: a writer
-- that cannot see the chain is not proven to be its author.
--
-- The (namespace, key, version) unique index serves the lookup.

CREATE OR REPLACE FUNCTION shared_memory_mesh_message_single_writer() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_root_peer    uuid;
    v_root_version integer;
BEGIN
    SELECT created_by_peer_id, version INTO v_root_peer, v_root_version
      FROM shared_memory
     WHERE namespace = NEW.namespace
       AND key = NEW.key
       AND version < NEW.version
     ORDER BY version ASC
     LIMIT 1;
    IF v_root_peer IS NULL THEN
        RAISE EXCEPTION
            'E_NOT_MESSAGE_AUTHOR: no earlier version of mesh message % is visible to this insert of version %; refused',
            NEW.key, NEW.version USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_by_peer_id IS DISTINCT FROM v_root_peer THEN
        RAISE EXCEPTION
            'E_NOT_MESSAGE_AUTHOR: mesh message % was created (version %) by peer %; peer % may not write version % (divergence 028)',
            NEW.key, v_root_version, v_root_peer, NEW.created_by_peer_id, NEW.version
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_mesh_single_writer_trg ON shared_memory;
CREATE TRIGGER shared_memory_mesh_single_writer_trg
    BEFORE INSERT ON shared_memory
    FOR EACH ROW
    WHEN (NEW.namespace = 'shared/mesh-chat' AND NEW.key LIKE 'msg/%' AND NEW.version > 1)
    EXECUTE FUNCTION shared_memory_mesh_message_single_writer();
