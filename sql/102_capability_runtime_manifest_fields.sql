-- 102_capability_runtime_manifest_fields.sql
-- TASK-556.3: declared runtime vocabulary on capabilities.
-- runtime_profile is hash-covered when present (manifest hash rule v1.1, see
-- crates/meshy-skills/src/manifest.rs); runtime_status is operational and never
-- hashed. Both NULL for every existing row: legacy manifests keep
-- byte-identical hashes (absence law, no backfill).
--
-- Slot history: AC#7 reserved 095 at base b2eb378; 095-101 were taken by the
-- time implementation started (re-inventoried 2026-07-19 by sxxxxxxxx,
-- confirmed 2026-07-20 by hxxxxx), so the slot renumbered to 102.

ALTER TABLE skills
    ADD COLUMN IF NOT EXISTS runtime_profile JSONB NULL,
    ADD COLUMN IF NOT EXISTS runtime_status  JSONB NULL;

ALTER TABLE models
    ADD COLUMN IF NOT EXISTS runtime_profile JSONB NULL,
    ADD COLUMN IF NOT EXISTS runtime_status  JSONB NULL;

-- Catalog filters: harness and policy_tags are the two query axes.
CREATE INDEX IF NOT EXISTS skills_runtime_harness_idx
    ON skills ((runtime_profile->>'harness'))
    WHERE runtime_profile IS NOT NULL;
CREATE INDEX IF NOT EXISTS skills_runtime_policy_tags_idx
    ON skills USING GIN ((runtime_profile->'policy_tags') jsonb_path_ops)
    WHERE runtime_profile IS NOT NULL;
