-- MeshyChat divergence 040, slice 4 (B07, design docs/design/biscuit-capabilities.md 2.5 (a) and
-- (c), Q3, Q8, critique C3 and C18): the capability revocation table and the append-only
-- issuance/revocation event log, whose `issue` rows are the issuance register.
--
-- NUMBER. 019, this track's SECOND migration (015 is slice 3's predicate). Not 016, reserved
-- for the skill frames; not 017, which main has (divergence 037's summaries); not 018, which
-- the skill frames' branch already uses for its second migration. Checked against origin/main
-- at 8caac46 and every local branch on 2026-09-25.
--
-- capability_revocation, the design's (a): one row per revoked BLOCK id. A token's revocation
-- ids are one per block, and a descendant's list starts with its parent's, so revoking the
-- authority id kills the token and everything derived from it, and revoking a delegation
-- block's id kills that branch. The verifier (apps/memoryskills/capability.py) refuses a token
-- if ANY of its ids is here. `not_after` is the token's own expiry, kept so that a prune can be
-- DECIDED later from the row count; there is no prune (Q8: never pruned in v1).
--
-- capability_event, the event log of (a) and the register of (c). An `issue` row is written by
-- the mint (slice 5) and holds what the token says, never the token: issuer, subject, the ONE
-- namespace, the rights as [{"prefix", "action"}], the depth cap, the expiry, the root kid, and
-- the authority revocation id. The verifier's liveness statement requires the presented
-- token's authority id to have an issue row, so a token signed with a leaked root key that this
-- server never minted is refused (critique C3). A `revoke` row records who revoked which block
-- id, and why. The shape of each kind is a CHECK: an issue row lives at most 24 hours (Q3), its
-- depth cap is 0 to 4, its actions are read and write only (`admin` is never in a token), and
-- its actor is its issuer; a revoke row carries no register column. No foreign keys: an audit
-- trail must survive the deletion of a peer it names, as tenancy_denials does (sql/043).
--
-- APPEND-ONLY IS THE DATABASE'S PROPERTY (critique C18, divergence 029's shape): ENABLE and
-- FORCE row-level security, a SELECT and an INSERT policy bound to app.server_internal = '1'
-- (the memory tables' context, apps/memoryskills/db.py), and NO UPDATE or DELETE policy. The app
-- role also loses the UPDATE and DELETE privileges sql/010's default privileges gave it (it never
-- had TRUNCATE), so a rewrite or removal is an ERROR from every context, server_internal
-- included, and not a silent nothing. Nothing retains, prunes or partitions these rows (V1 rule
-- A1: retention never deletes audit history).

CREATE TABLE IF NOT EXISTS capability_revocation (
    revocation_id    BYTEA       PRIMARY KEY CHECK (octet_length(revocation_id) = 64),
    revoked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_by_peer  UUID        NOT NULL REFERENCES peers(id),
    reason           TEXT        NOT NULL,
    not_after        TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_event (
    id                       BIGSERIAL   PRIMARY KEY,
    event                    TEXT        NOT NULL CHECK (event IN ('issue', 'revoke')),
    at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_peer_id            UUID        NOT NULL,
    authority_revocation_id  BYTEA       NOT NULL
                                         CHECK (octet_length(authority_revocation_id) = 64),
    -- the register (issue rows only)
    issuer_peer_id           UUID,
    subject_peer_id          UUID,
    namespace                TEXT,
    rights                   JSONB,
    depth_cap                SMALLINT,
    expires_at               TIMESTAMPTZ,
    kid                      BIGINT,
    -- the revocation (revoke rows only)
    revocation_id            BYTEA,
    reason                   TEXT,
    CONSTRAINT capability_event_issue_shape CHECK (
        event <> 'issue' OR (
            issuer_peer_id IS NOT NULL AND subject_peer_id IS NOT NULL
            AND namespace IS NOT NULL AND rights IS NOT NULL AND depth_cap IS NOT NULL
            AND expires_at IS NOT NULL AND kid IS NOT NULL
            AND revocation_id IS NULL AND reason IS NULL
            AND actor_peer_id = issuer_peer_id
            AND depth_cap BETWEEN 0 AND 4
            AND kid BETWEEN 0 AND 4294967295
            AND expires_at > at AND expires_at <= at + interval '24 hours'
            AND jsonb_typeof(rights) = 'array' AND jsonb_array_length(rights) >= 1
            AND NOT jsonb_path_exists(rights, '$[*] ? (@.type() != "object")')
            AND NOT jsonb_path_exists(rights, '$[*] ? (!exists(@.prefix) || @.prefix.type() != "string")')
            AND NOT jsonb_path_exists(rights, '$[*] ? (!(@.action == "read" || @.action == "write"))')
        )),
    CONSTRAINT capability_event_revoke_shape CHECK (
        event <> 'revoke' OR (
            revocation_id IS NOT NULL AND octet_length(revocation_id) = 64
            AND reason IS NOT NULL
            AND issuer_peer_id IS NULL AND subject_peer_id IS NULL AND namespace IS NULL
            AND rights IS NULL AND depth_cap IS NULL AND expires_at IS NULL AND kid IS NULL
        ))
);

-- One issue row per authority id: the register lookup of the liveness statement.
CREATE UNIQUE INDEX IF NOT EXISTS capability_event_issued_once
    ON capability_event (authority_revocation_id) WHERE event = 'issue';
CREATE INDEX IF NOT EXISTS capability_event_authority_idx
    ON capability_event (authority_revocation_id, at);

ALTER TABLE capability_revocation ENABLE ROW LEVEL SECURITY;
ALTER TABLE capability_revocation FORCE ROW LEVEL SECURITY;
CREATE POLICY capability_revocation_read ON capability_revocation
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY capability_revocation_insert ON capability_revocation
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER TABLE capability_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE capability_event FORCE ROW LEVEL SECURITY;
CREATE POLICY capability_event_read ON capability_event
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY capability_event_insert ON capability_event
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1');

REVOKE UPDATE, DELETE, TRUNCATE ON capability_revocation FROM meshy_app;
REVOKE UPDATE, DELETE, TRUNCATE ON capability_event FROM meshy_app;

COMMENT ON TABLE capability_revocation IS
    'Revoked capability block ids (divergence 040, design 2.5 (a)): checked on every token use. Append-only: no UPDATE or DELETE policy or privilege, never pruned (Q8).';
COMMENT ON TABLE capability_event IS
    'Append-only capability issuance and revocation log (divergence 040, design 2.5 (a), (c)). Issue rows are the issuance register a token must be in; never the token itself.';
