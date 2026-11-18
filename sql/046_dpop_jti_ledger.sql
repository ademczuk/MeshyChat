-- 046_dpop_jti_ledger.sql
-- TASK-26 #4: server-side DPoP jti replay ledger.
--
-- The per-request DPoP proof carries a 16-byte `jti`. The freshness-window
-- check (meshy-security verify_dpop_proof / verify_bound_request) bounds replay
-- to `window_secs`, but a proof re-sent INSIDE that window would otherwise pass
-- twice. This ledger closes that gap: the first presentation of a jti inserts a
-- row, a second presentation of the same jti within its lifetime hits the PK and
-- is rejected as a replay.
--
-- Consuming code: the meshy-security DPoP seam (verify_bound_request) wired into
-- the peer-write extractor and the WS upgrade (TASK-26 #2/#3). The table is
-- additive and inert until that integration lands.

CREATE TABLE IF NOT EXISTS dpop_jti_seen (
    jti        BYTEA       PRIMARY KEY,            -- the 16-byte proof id
    peer_id    UUID        NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL               -- proof.timestamp + window; row GC-able after this
);

-- The replay check is one atomic insert (the namespace-claim-ledger pattern):
--   INSERT INTO dpop_jti_seen (jti, peer_id, expires_at)
--   VALUES ($1, $2, $3)
--   ON CONFLICT (jti) DO NOTHING
--   RETURNING jti;
-- zero rows returned => the jti was already seen => replay, reject the request.

-- Expired rows are pruned out-of-band; this index makes the sweep cheap and
-- keeps the table from growing without bound.
CREATE INDEX IF NOT EXISTS dpop_jti_seen_expires_idx ON dpop_jti_seen (expires_at);
