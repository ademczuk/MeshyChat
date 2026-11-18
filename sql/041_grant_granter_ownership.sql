-- 041_grant_granter_ownership.sql
--
-- IG4 DB backstop. skill_grants.granter_peer_id (migration 004) and
-- skill_access_requests.granter_peer_id (migration 005) were plain FKs to
-- peers(id) with NO constraint that the granter peer is owned by the claimed
-- grantor. The application layer already enforces the coupling (meshy-grants
-- service.rs:48-59 and access_requests.rs:49-60 assert granter_peer.owner ==
-- grantor at insert, and the invoke gate keys the consume on the real target
-- peer), so a forged grant is NOT reachable through the API. A direct-DB INSERT
-- could still forge one. This adds the DB-level backstop so the coupling is
-- non-bypassable. Verified zero violating rows on the live DB before adding
-- (skill_grants 13 rows / 0 violating, skill_access_requests 0 / 0).

-- The composite FK needs a matching unique key on the referenced columns.
-- peers.id is already the PK (so (id, owner_id) is trivially unique); this
-- declares it as a referenceable unique key.
ALTER TABLE peers ADD CONSTRAINT peers_id_owner_uq UNIQUE (id, owner_id);

-- skill_grants: tie granter_peer_id to grantor_owner_id at the DB layer.
ALTER TABLE skill_grants DROP CONSTRAINT skill_grants_granter_peer_id_fkey;
ALTER TABLE skill_grants
    ADD CONSTRAINT skill_grants_granter_peer_owner_fkey
    FOREIGN KEY (granter_peer_id, grantor_owner_id)
    REFERENCES peers (id, owner_id) ON DELETE CASCADE;

-- skill_access_requests: the same coupling (granter_peer_id <-> granter_owner_id).
ALTER TABLE skill_access_requests DROP CONSTRAINT skill_access_requests_granter_peer_id_fkey;
ALTER TABLE skill_access_requests
    ADD CONSTRAINT skill_access_requests_granter_peer_owner_fkey
    FOREIGN KEY (granter_peer_id, granter_owner_id)
    REFERENCES peers (id, owner_id) ON DELETE CASCADE;
