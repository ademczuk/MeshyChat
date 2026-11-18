-- 031_refresh_tokens_lineage_id.sql
--
-- Session lineage for refresh tokens, so key-publish re-anchoring can scope to
-- the CALLER's own rotation chain instead of every live token of the peer.
--
-- Background (audit row refresh_reanchor_session_scope, 147883e9): the 030-era
-- re-anchor was peer-wide (WHERE peer_id), so a stolen-but-unrevoked SIBLING
-- refresh token was laundered onto the new active kid and survived rotation -
-- rotation did not evict a stolen sibling. A "session" that must survive key
-- rotation is the LINEAGE: the chain of single-use-rotated tokens descending
-- from one issuance. Stamp a stable lineage id at issuance, inherit it on every
-- rotation, and re-anchor only WHERE lineage_id = the caller's jwt.lin.
--
-- The shape this closes: a peer holds a second token lineage that an earlier
-- rotation never touched, so it survives every later rotation of the first -
-- the sibling-survives shape.

ALTER TABLE refresh_tokens
    ADD COLUMN lineage_id UUID NOT NULL DEFAULT gen_random_uuid();

-- Existing rows each become their own singleton lineage (the DEFAULT fired per
-- row on ADD COLUMN, so every legacy token already has a distinct lineage_id;
-- no backfill UPDATE needed). The DEFAULT also means issuance sites that do NOT
-- mention lineage_id (redeem, onboard) auto-start a FRESH lineage, which is the
-- correct semantics for a brand-new session. Only the rotation path
-- (auth_refresh) overrides the DEFAULT to INHERIT the parent's lineage_id.

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_peer_lineage
    ON refresh_tokens (peer_id, lineage_id)
    WHERE revoked_at IS NULL;
