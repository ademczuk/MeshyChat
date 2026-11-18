-- Dashboard password + TOTP MFA credentials for owners.
--
-- The dashboard offers password login, change-password, and TOTP two-factor in
-- its UI, but the backend only had SSO (OIDC) + passkeys + the Ed25519
-- owner-challenge. This adds the optional local-credential columns so an owner
-- can set a password and enrol an authenticator app. SSO/passkey owners simply
-- leave these NULL.
--
-- No RLS: dashboard_users is read on the pool directly (see /v1/auth/me); every
-- query here is owner-id-scoped in application code. Columns are additive +
-- idempotent (ADD COLUMN IF NOT EXISTS).

ALTER TABLE dashboard_users
    -- argon2id PHC string; NULL = no password set (SSO/passkey-only owner).
    ADD COLUMN IF NOT EXISTS password_hash TEXT,
    -- base32 TOTP secret, live once enrolled.
    ADD COLUMN IF NOT EXISTS totp_secret TEXT,
    -- base32 TOTP secret staged during setup, promoted to totp_secret on verify.
    ADD COLUMN IF NOT EXISTS totp_pending_secret TEXT,
    ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    -- SHA-256 hashes of single-use recovery codes; consumed entries are removed.
    ADD COLUMN IF NOT EXISTS totp_recovery_hashes TEXT[] NOT NULL DEFAULT '{}';
