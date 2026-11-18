-- 069_agent_tag_credentials.sql
-- TASK-34 AC#4 (native): the live store for admin-attested agent tag credentials
-- (meshy_grants::tag_vc::AgentTagVC). resolve_authorization_tags reads the active
-- (non-revoked, non-expired) rows for a peer, verifies each row's Ed25519 signature under
-- the trusted issuer key (verify_tag_vc), checks the binding + time window (vc_is_valid),
-- and returns the verified APPROVED tags. The revoked_at column is the "live not-revoked
-- read": a withdrawn credential stops authorizing the instant it is set, and the audit row
-- survives (revocation, not deletion).
--
-- Deploy-dark: additive CREATE TABLE only. Nothing reads this table until the
-- request-boundary tag-policy gate is wired behind MESHY_TAG_POLICY_ENFORCE (default off),
-- so existing peers are unaffected on deploy.
CREATE TABLE IF NOT EXISTS agent_tag_credentials (
    vc_id           text        PRIMARY KEY,
    subject_peer_id uuid        NOT NULL,
    issuer_id       uuid        NOT NULL,
    tags            text[]      NOT NULL DEFAULT '{}',
    allowed_callees text[]      NOT NULL DEFAULT '{}',
    signature_b64   text        NOT NULL,
    issued_at       bigint      NOT NULL,
    expires_at      bigint      NOT NULL,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- The resolve path filters by subject + active-ness on every invocation; index the live
-- (non-revoked) rows for that peer lookup.
CREATE INDEX IF NOT EXISTS idx_agent_tag_credentials_active_subject
    ON agent_tag_credentials (subject_peer_id)
    WHERE revoked_at IS NULL;
