-- Ed25519 public keys for end-to-end signing (spec §5.2, §8).

CREATE TABLE peer_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    peer_id         UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    kid             TEXT NOT NULL,
    public_key      TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ,
    UNIQUE (peer_id, kid)
);

-- One active key per peer at a time
CREATE UNIQUE INDEX peer_keys_one_active_per_peer
    ON peer_keys (peer_id)
    WHERE active = TRUE AND revoked_at IS NULL;

-- (peer_id, kid) lookups are served by the implicit B-tree index that the
-- inline UNIQUE (peer_id, kid) constraint creates; no explicit index needed.
