-- Require key-publish proof-of-possession + active-key co-signature while
-- preserving insert-then-revoke ordering inside the publish transaction.

DROP INDEX IF EXISTS peer_keys_one_active_per_peer;

CREATE INDEX peer_keys_active_per_peer_idx
    ON peer_keys (peer_id)
    WHERE active = TRUE AND revoked_at IS NULL;

CREATE TABLE key_publish_nonces (
    peer_id   UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    nonce     TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (peer_id, nonce)
);

CREATE INDEX key_publish_nonces_seen_at_idx ON key_publish_nonces (seen_at);
