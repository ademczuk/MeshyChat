-- 047_grant_delegation_source.sql
--
-- TASK-1 AC#3: enable constant-peer re-delegation by relaxing the IG4 owned-peer
-- coupling (migration 041) from a blanket composite FK to a per-row, source-
-- conditional guard, and introduce the explicit `delegated` grant source.
--
-- Migration 041 added a COMPOSITE FK (granter_peer_id, grantor_owner_id) ->
-- peers(id, owner_id) as the IG4 DB backstop: the granter peer must be OWNED by the
-- grantor. That holds for ROOT grants (the owner grants on their own peer), but a
-- Model-1 re-delegation grants on the CONSTANT root peer the re-delegator does NOT
-- own (it HOLDS a parent grant on that peer), so the composite FK blocks every
-- re-delegation row (23503). This was a DB-level twin of the app-layer GAP1; it
-- only surfaces on insert, caught running the delegation gate corpus in the dev DB.
--
-- Design-of-record (3-round adversarial convergence, kxxx/dxxxxx/bxxxxx): the DB
-- enforces per-row integrity ONLY - existence, owned-peer for ROOT sources, and
-- source_ref coherence for delegated. The CROSS-ROW capability rule (the grantor
-- holds a live, attenuating parent grant) lives in ONE place: issue_grant (write)
-- and authorize()'s chain walk (read), via the shared rule core. It is deliberately
-- NOT re-implemented in the DB: a second DB copy (e.g. a "grantor holds a live grant
-- on this peer" foothold trigger) would drift from find_active_grant's liveness
-- predicate, the exact drift class behind this session's check_chain blind spot.
-- A delegated row's provenance is thus backstopped by TWO independent app gates
-- (the write gate + the read walk), not a third DB gate. Fail-closed on unknown
-- sources so a future source variant cannot silently bypass the IG4 guard.

-- 1. Admit the explicit `delegated` GrantSource value.
ALTER TABLE skill_grants DROP CONSTRAINT skill_grants_source_check;
ALTER TABLE skill_grants ADD CONSTRAINT skill_grants_source_check
    CHECK (source IN ('marketplace_subscription','direct','approved_request',
                      'public_auto','delegated'));

-- 2. A delegated row MUST carry its parent pointer: the read-side walk climbs
--    source_ref, and a delegated row with a NULL source_ref is incoherent and a
--    latent hole. Root rows are unconstrained here (source_ref carries stripe ids,
--    request ids, etc.).
ALTER TABLE skill_grants ADD CONSTRAINT skill_grants_delegated_has_source_ref
    CHECK (source <> 'delegated' OR source_ref IS NOT NULL);

-- 3. Relax the composite FK to a single-column existence FK. The granter peer must
--    still EXIST; ownership is no longer a blanket FK invariant because a delegated
--    row legitimately grants on a peer the grantor does not own.
ALTER TABLE skill_grants DROP CONSTRAINT skill_grants_granter_peer_owner_fkey;
ALTER TABLE skill_grants
    ADD CONSTRAINT skill_grants_granter_peer_id_fkey
    FOREIGN KEY (granter_peer_id) REFERENCES peers(id) ON DELETE CASCADE;

-- 4. The IG4 owned-peer coupling, now source-conditional, as a BEFORE trigger:
--    - ROOT sources  -> the grantor MUST own the granter peer (the 041 backstop,
--      kept non-bypassable for every root-grant kind).
--    - 'delegated'   -> SKIP the ownership check (per-row coherence is the CHECK in
--      step 2; cross-row provenance is the app write gate + the read-side walk).
--    - any OTHER source -> RAISE (fail-closed): an unrecognized source must never
--      reach an INSERT/UPDATE that silently no-ops past the IG4 guard.
CREATE OR REPLACE FUNCTION skill_grants_granter_ownership_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.source = 'delegated' THEN
        RETURN NEW;
    ELSIF NEW.source IN ('marketplace_subscription','direct',
                         'approved_request','public_auto') THEN
        IF NOT EXISTS (
            SELECT 1 FROM peers
            WHERE id = NEW.granter_peer_id AND owner_id = NEW.grantor_owner_id
        ) THEN
            RAISE EXCEPTION
                'skill_grants IG4: granter_peer % is not owned by grantor % (source=%)',
                NEW.granter_peer_id, NEW.grantor_owner_id, NEW.source
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    ELSE
        RAISE EXCEPTION 'skill_grants: unknown grant source % (fail-closed)', NEW.source
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER skill_grants_granter_ownership_guard_trg
    BEFORE INSERT OR UPDATE ON skill_grants
    FOR EACH ROW EXECUTE FUNCTION skill_grants_granter_ownership_guard();
