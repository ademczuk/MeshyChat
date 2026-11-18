-- Bind SSO identity on the PAIR (issuer, subject) instead of a bare subject.
--
-- Before this migration `dashboard_users.authentik_sub TEXT UNIQUE` was the
-- whole identity, so the subject string alone decided which account a sign-in
-- landed on. That is safe only while exactly one issuer ever mints subjects.
-- Cutting over to Platform Accounts breaks that premise: `sub` values are opaque
-- per-issuer strings, so an Accounts subject that happens to equal a stored
-- Authentik subject would have signed in AS that account (issue #624 §1.3).
--
-- Three changes, in this order:
--   1. add the issuer half and a place to park the pre-cutover binding;
--   2. retire the issuer-blind UNIQUE(subject);
--   3. make the PAIR unique, and require both halves or neither.
--
-- Idempotent per tools/ci/MIGRATIONS.md rule 1: every statement is guarded, and
-- the backfill in step 2 is a no-op on re-run because it only touches rows that
-- still carry a subject with no issuer.

-- 1a. The issuer half of the identity. NULL on a row that has never completed
-- an issuer-bound sign-in.
ALTER TABLE dashboard_users
    ADD COLUMN IF NOT EXISTS accounts_issuer TEXT;

-- 1b. Where a pre-cutover Authentik subject is preserved. This column is
-- never read as an identity — it exists so the cutover destroys no audit trail
-- and an operator can answer "which Authentik principal was this row?" later.
ALTER TABLE dashboard_users
    ADD COLUMN IF NOT EXISTS legacy_authentik_sub TEXT;

-- 1c. authentik_sub -> accounts_subject. Guarded on both sides so re-running
-- over an already-renamed table is a no-op rather than an error.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'dashboard_users'
          AND column_name = 'authentik_sub'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'dashboard_users'
          AND column_name = 'accounts_subject'
    ) THEN
        ALTER TABLE dashboard_users RENAME COLUMN authentik_sub TO accounts_subject;
    END IF;
END $$;

-- Covers a database that somehow has neither spelling.
ALTER TABLE dashboard_users
    ADD COLUMN IF NOT EXISTS accounts_subject TEXT;

-- 2a. Retire the issuer-blind uniqueness. Either name may be present depending
-- on whether the rename above already ran on this database.
ALTER TABLE dashboard_users DROP CONSTRAINT IF EXISTS dashboard_users_authentik_sub_key;
ALTER TABLE dashboard_users DROP CONSTRAINT IF EXISTS dashboard_users_accounts_subject_key;

-- 2b. Park every pre-cutover binding: a subject with no issuer cannot be
-- attributed to an issuer after the fact, and guessing one would be worse than
-- admitting we do not know. Clearing `accounts_subject` makes the row eligible
-- for the ordinary verified-email bind on its owner's first Accounts sign-in
-- (`UsersRepository::resolve_for_sso` branch 2), which is gated on
-- `email_verified == true` AND on the token issuer matching the configured one.
--
-- The security property this buys: after this runs, NO stored subject can be
-- matched by an Accounts sign-in, so the §1.3 collision has no target.
--
-- Idempotent: on a second run these rows already have accounts_subject IS NULL,
-- so the WHERE clause selects nothing. Rows bound AFTER the cutover carry a
-- non-NULL accounts_issuer and are excluded.
UPDATE dashboard_users
   SET legacy_authentik_sub = accounts_subject,
       accounts_subject     = NULL
 WHERE accounts_subject IS NOT NULL
   AND accounts_issuer IS NULL;

-- 3a. Identity is the pair. Partial so the many not-yet-bound rows (both halves
-- NULL) do not contend, and so the index states the invariant it enforces.
CREATE UNIQUE INDEX IF NOT EXISTS dashboard_users_accounts_identity_key
    ON dashboard_users (accounts_issuer, accounts_subject)
    WHERE accounts_issuer IS NOT NULL AND accounts_subject IS NOT NULL;

-- 3b. Half an identity is not an identity. Without this a row could carry a
-- subject with no issuer and slip back into issuer-blind matching.
ALTER TABLE dashboard_users DROP CONSTRAINT IF EXISTS dashboard_users_accounts_identity_ck;
ALTER TABLE dashboard_users
    ADD CONSTRAINT dashboard_users_accounts_identity_ck
    CHECK ((accounts_issuer IS NULL) = (accounts_subject IS NULL));
