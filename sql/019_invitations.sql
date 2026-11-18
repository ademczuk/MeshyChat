-- Invitations: mentor-issued codes that mentees redeem to create a peer +
-- owner pair bound to the issuing mentor via lesson_subscriptions (020).

CREATE TABLE invitations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code                TEXT NOT NULL UNIQUE,
    issuer_peer_id      UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    owner_handle        TEXT,
    expires_at          TIMESTAMPTZ,
    redeemed_at         TIMESTAMPTZ,
    redeemed_by_owner   UUID REFERENCES dashboard_users(id),
    max_uses            INT NOT NULL DEFAULT 1 CHECK (max_uses >= 1),
    uses                INT NOT NULL DEFAULT 0 CHECK (uses >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX invitations_issuer_idx ON invitations (issuer_peer_id);
CREATE INDEX invitations_expires_at_idx ON invitations (expires_at)
    WHERE redeemed_at IS NULL AND expires_at IS NOT NULL;
