-- 066_owner_human_keys.sql
-- TASK-30 AC#2: owners register trusted human-authorization PUBLIC keys,
-- separately from agent/peer keys.
--
-- A human-authorization key is a DIFFERENT class from the agent peer_keys
-- (migration 002): peer_keys are device/agent self-custody, FK'd to a peer, one
-- active per peer; a human-auth key is OWNER-custody, owner-scoped, and an owner
-- holds SEVERAL (a phone, a hardware key) - so there is deliberately NO
-- one-active-per-owner constraint, unlike peer_keys. The resolver
-- (find_active_human_key(owner_id)) returns all of an owner's non-revoked keys.
--
-- This same key class verifies BOTH the live per-call human consent (TASK-30
-- AC#3) AND the past-intent IntentMandate signature (TASK-29 AC#3): mandate
-- verification was deferred for lack of an owner-key source (meshy-grants
-- service.rs: "verified at the meshy-api seam against the authenticated owner's
-- human-authorization key"), and this table is that source. One key, two gates.
--
-- public_key is the base64url raw 32-byte Ed25519 verifying key (the agent_card /
-- onboard_peers decode_pubkey convention). kid is the did:key / did:wba id.
-- Additive table; existing owners hold no human key, so the gate stays dormant.

CREATE TABLE owner_human_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    kid         TEXT NOT NULL,
    public_key  TEXT NOT NULL,
    label       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ,
    UNIQUE (owner_id, kid)
);

-- The resolver reads an owner's live keys; index the hot path (active, by owner).
CREATE INDEX owner_human_keys_active_idx
    ON owner_human_keys (owner_id)
    WHERE revoked_at IS NULL;
