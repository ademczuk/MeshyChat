-- 032_auth_challenges.sql
--
-- Signature-auth: a peer re-mints a short JWT by signing a server-issued
-- challenge with its permanent active signing key (wiki/signature-auth-endpoint
-- v3, dxxxxx CONFIRMED). Kills the aud-lockout class - a node always
-- self-recovers from its own local key, with no refresh token to revoke or race.
--
-- ISSUE-THEN-CONSUME semantics (dxxxxx BLOCKING-1): the key_publish_pop nonce
-- table is INVERTED - PoP nonces are client-generated and INSERTed at verify
-- time, so presence there means ALREADY-BURNED. A server-issued challenge is
-- stored at issuance, so presence means AVAILABLE. Sharing the table would make
-- a consume-as-PoP reject every legit sign-in. Hence a separate table:
-- issue = INSERT a fresh nonce; consume = UPDATE ... SET consumed_at WHERE
-- consumed_at IS NULL ... RETURNING (atomic single-use).
--
-- NO foreign key to peers(id) (dxxxxx HARDENING-4): an FK would make a
-- challenge for an unknown peer_id fail differently from a known one, an
-- enumeration oracle against the house uniform-response stance. The challenge
-- route responds uniformly for any well-formed peer_id; sign-in gates on the
-- peer actually having an active key (find_active), not on the challenge FK.
--
-- A challenge exists only in the DB that issued it, so cross-server replay is
-- structurally impossible (the dual-server incident class cannot touch this).

CREATE TABLE auth_challenges (
    peer_id     UUID NOT NULL,
    nonce       TEXT NOT NULL,
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    PRIMARY KEY (peer_id, nonce)
);

-- Prune support: a janitor or the issue path can delete expired/consumed rows.
CREATE INDEX auth_challenges_expires_at_idx ON auth_challenges (expires_at);
