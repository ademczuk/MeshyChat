-- 049_envelope_nonce_seen.sql
-- TASK-33 #2/#3: server-side envelope nonce replay ledger.
--
-- The signed envelope's only replay guard is verify's 120-second issued_at skew
-- window (meshy-security ed25519/verify.rs step 5). A captured envelope re-sent
-- INSIDE that window otherwise passes twice. This ledger closes that gap, exactly
-- as dpop_jti_seen (migration 046) does for DPoP proofs: the first presentation of
-- a (peer, nonce) inserts a row, a second presentation of the same pair within
-- its lifetime hits the PK and is rejected as a replay. The nonce is keyed PER
-- PEER (each peer has an independent nonce space), so two peers may use the same
-- nonce string without colliding.
--
-- Consuming code: the meshy-security enforce_nonce seam (ed25519/replay.rs,
-- landed) wired into the SignVerifiedBody extractor after verify_payload
-- succeeds, gated by MESHY_ENVELOPE_NONCE_ENFORCE (default-off). The table is
-- additive and inert until that integration lands + the flag flips - the same
-- deploy-dark-then-flip posture as 046.

CREATE TABLE IF NOT EXISTS envelope_nonce_seen (
    peer_id    UUID        NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    nonce      TEXT        NOT NULL,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,             -- issued_at + window; row GC-able after this
    PRIMARY KEY (peer_id, nonce)
);

-- The replay check is one atomic insert (the dpop_jti_seen / claim_jti_or_reject
-- pattern in jwt_auth.rs):
--   INSERT INTO envelope_nonce_seen (peer_id, nonce, expires_at)
--   VALUES ($1, $2, $3)
--   ON CONFLICT (peer_id, nonce) DO NOTHING
--   RETURNING nonce;
-- zero rows returned => this (peer, nonce) was already presented => replay, reject.

-- Expired rows are pruned by the TASK-33 #3 janitor; this index makes the sweep
-- cheap and bounds table growth.
CREATE INDEX IF NOT EXISTS envelope_nonce_seen_expires_idx ON envelope_nonce_seen (expires_at);
