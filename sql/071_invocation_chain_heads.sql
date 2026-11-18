-- TASK-39 Part 2: truncation-resistant signed chain-head anchor.
--
-- invocation_frames (migration 060) hash-chains frames per chain_id, so an
-- in-place edit or a mid-chain deletion breaks the backward link and is caught by
-- the Part 1 verifier (verify_chain). What that does NOT catch is TAIL TRUNCATION:
-- deleting the newest N frames leaves a shorter, self-consistent, still-linked
-- chain. Nothing records "this chain reached at least K frames up to id M".
--
-- This table holds ONE signed row per chain_id, upserted on every grant-bound
-- authorize (the frame-write path), recording the head frame id, the head hash,
-- and the frame count, signed under the server's Ed25519 non-repudiation key
-- (server_signing_key -- a DIFFERENT trust domain from the biscuit grant root).
-- An auditor compares the signed (frame_count, head_frame_id) against the live
-- table: a live count BELOW the signed count means frames were erased. The
-- signature stops an attacker re-writing the anchor down to match a truncated
-- table. The anchor only ever moves forward (the upsert's monotonic guard), so a
-- best-effort reconcile that lags never false-positives a truncation.
--
-- Additive + deploy-dark: no reader until the audit job / endpoint lands (Part 1b).
CREATE TABLE invocation_chain_heads (
    chain_id        UUID PRIMARY KEY,
    head_frame_id   BIGINT NOT NULL,   -- max(invocation_frames.id) for this chain at signing time
    head_hash       TEXT   NOT NULL,   -- the newest frame's frame_hash
    frame_count     BIGINT NOT NULL,   -- count of frames in this chain at signing time
    head_signature  BYTEA  NOT NULL,   -- Ed25519 (64 bytes) over canonical(chain_id, head_frame_id, head_hash, frame_count)
    signing_kid     TEXT   NOT NULL,   -- which server key signed (verify resolves the matching public key)
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
