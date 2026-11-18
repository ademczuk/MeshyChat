-- Per-peer HMAC shared secret for transport auth.
-- Spec §6.4 + §8.1: HMAC-SHA256 over METHOD/PATH/QUERY/BODY/TIMESTAMP using
-- a secret known to both caller and server.
--
-- Storage: TEXT column, base64url-encoded random 32-byte secret. Production
-- layers Postgres encryption-at-rest underneath. Per spec §8.7, the caller
-- stores their copy on disk (0600) or in MESHY_HMAC_SECRET env var.

ALTER TABLE peers
    ADD COLUMN hmac_secret TEXT;

COMMENT ON COLUMN peers.hmac_secret IS
    'Per-peer HMAC-SHA256 shared secret. Base64url-encoded 32 random bytes. Caller stores plaintext locally; server stores plaintext in this column (encrypt-at-rest at the Postgres layer in production).';
