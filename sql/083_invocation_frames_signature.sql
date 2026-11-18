-- TASK-88: keyed/signed invocation-frame hashes.
--
-- Frames written with a configured server signing key carry an Ed25519 signature
-- over a domain-separated frame_hash. Consumers that have a verifying key require
-- a valid signature instead of trusting row presence alone, closing the
-- direct-INSERT frame-forgery path against continuation nonces and flow-guard
-- origin resolution.

ALTER TABLE invocation_frames
    ADD COLUMN IF NOT EXISTS frame_signature BYTEA;

ALTER TABLE invocation_frames
    ADD COLUMN IF NOT EXISTS signing_kid TEXT;

ALTER TABLE invocation_frames
    DROP CONSTRAINT IF EXISTS invocation_frames_signature_kid_ck;
ALTER TABLE invocation_frames
    ADD CONSTRAINT invocation_frames_signature_kid_ck
        CHECK (
            frame_signature IS NULL
            OR (signing_kid IS NOT NULL AND signing_kid <> '')
        );
