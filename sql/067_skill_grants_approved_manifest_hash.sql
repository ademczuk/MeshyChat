-- 067_skill_grants_approved_manifest_hash.sql
-- TASK-35 AC#4: pin the approved skill MANIFEST HASH at grant issuance, recompute
-- + compare at invoke (the rug-pull / CVE-2025-54136 defense). The 32-byte hash is
-- meshy_grants::manifest::manifest_hash over the behavior-defining manifest (peer,
-- name, version, mode, description, input/output schema, attachment flags, tags) -
-- the input_schema/output_schema ARE the canonical input, so pinning the manifest
-- hash satisfies AC#4.
--
-- Additive + backward-compatible, mirrors 052_intent_mandate_binding.sql:
--   * nullable: a grant issued without a resolved pin (the common case, and EVERY
--     existing row) leaves it NULL. NULL = "not pinned" = the verify-at-invoke step
--     is skipped, behavior-identical to pre-067 issuance.
--   * length-guarded: when present it is exactly 32 bytes (matches
--     skill_grants_mandate_hash_len).
-- Pinned ONLY when version_pin IS NOT NULL (enforced in the service, not the DB):
-- an unpinned grant tracks the current version, so pinning its manifest would force
-- re-approval on every legitimate publish.
--
-- skill_grants is a cold/low-write table, so the ADD COLUMN is a metadata-only,
-- low-lock change (no daemon-paused window needed, unlike hot shared_memory DDL).
-- IDEMPOTENT (IF NOT EXISTS + guarded constraint) so a psql pre-apply for the
-- .sqlx cache regen and the later meshy-db rebuild re-apply both succeed.

ALTER TABLE skill_grants
    ADD COLUMN IF NOT EXISTS approved_manifest_hash BYTEA NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'skill_grants_approved_manifest_hash_len'
    ) THEN
        ALTER TABLE skill_grants
            ADD CONSTRAINT skill_grants_approved_manifest_hash_len
                CHECK (approved_manifest_hash IS NULL OR octet_length(approved_manifest_hash) = 32);
    END IF;
END $$;
