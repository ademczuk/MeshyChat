-- Spec section 7.2 step 9 middle branch: when the target peer is offline and has a
-- webhook URL configured, the server POSTs the signed envelope to it and marks
-- the call `routed`.

ALTER TABLE peers
    ADD COLUMN invocation_delivery_webhook TEXT,
    ADD COLUMN invocation_delivery_webhook_secret_hash TEXT;

COMMENT ON COLUMN peers.invocation_delivery_webhook IS
    'HTTPS URL the server POSTs signed call envelopes to when the peer is offline. NULL = use offline queue instead.';
COMMENT ON COLUMN peers.invocation_delivery_webhook_secret_hash IS
    'Argon2 hash of the shared secret. Caller computes HMAC over the envelope using the plaintext secret; meshy sends both Authorization: Bearer <hmac> and the envelope. Target verifies HMAC against its locally-stored plaintext secret.';
