-- 072_skills_manifest_attestation.sql
-- TASK-49: publisher Ed25519 attestation over the skill manifest.
--
-- The manifest hash-pin binds a grant to WHAT was approved. These nullable
-- columns bind a skill manifest to WHO published it when a publisher supplies an
-- Ed25519 attestation. NULL is the legacy/pre-enforcement sentinel: grant
-- issuance verifies if present, and skips if absent.

ALTER TABLE skills
    ADD COLUMN IF NOT EXISTS manifest_sig BYTEA NULL,
    ADD COLUMN IF NOT EXISTS signing_kid TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'skills_manifest_sig_len'
    ) THEN
        ALTER TABLE skills
            ADD CONSTRAINT skills_manifest_sig_len
                CHECK (manifest_sig IS NULL OR octet_length(manifest_sig) = 64);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'skills_manifest_attestation_pair'
    ) THEN
        ALTER TABLE skills
            ADD CONSTRAINT skills_manifest_attestation_pair
                CHECK (
                    (manifest_sig IS NULL AND signing_kid IS NULL)
                    OR
                    (
                        manifest_sig IS NOT NULL
                        AND signing_kid IS NOT NULL
                        AND length(btrim(signing_kid)) > 0
                    )
                );
    END IF;
END $$;

COMMENT ON COLUMN skills.manifest_sig IS
    'Optional raw 64-byte Ed25519 signature by the publishing peer over the TASK-49 skill manifest hash.';
COMMENT ON COLUMN skills.signing_kid IS
    'peer_keys.kid used for manifest_sig verification; stored so verification resolves the exact key across rotation.';
