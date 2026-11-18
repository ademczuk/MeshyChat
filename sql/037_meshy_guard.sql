-- MeshyGuard deterministic review state for shared skills and meshy-memory.
-- Existing rows are treated as approved during migration so deployment does not
-- hide already-published data. New rows default to quarantined until the
-- application stamps a scanner verdict in the same request.

ALTER TABLE skills
    ADD COLUMN meshy_guard_status TEXT NOT NULL DEFAULT 'approved'
        CHECK (meshy_guard_status IN ('approved','quarantined','blocked')),
    ADD COLUMN meshy_guard_review JSONB NOT NULL DEFAULT '{"status":"approved","scanner_version":"legacy-baseline","findings":[]}'::jsonb,
    ADD COLUMN meshy_guard_reviewed_at TIMESTAMPTZ,
    ADD COLUMN meshy_guard_artifact_hash TEXT;

CREATE INDEX skills_meshy_guard_status_idx
    ON skills(meshy_guard_status)
    WHERE deleted_at IS NULL;

ALTER TABLE skills
    ALTER COLUMN meshy_guard_status SET DEFAULT 'quarantined';

ALTER TABLE meshy_memory
    ADD COLUMN meshy_guard_status TEXT NOT NULL DEFAULT 'approved'
        CHECK (meshy_guard_status IN ('approved','quarantined','blocked')),
    ADD COLUMN meshy_guard_review JSONB NOT NULL DEFAULT '{"status":"approved","scanner_version":"legacy-baseline","findings":[]}'::jsonb,
    ADD COLUMN meshy_guard_reviewed_at TIMESTAMPTZ,
    ADD COLUMN meshy_guard_artifact_hash TEXT;

CREATE INDEX meshy_memory_meshy_guard_status_idx
    ON meshy_memory(meshy_guard_status);

ALTER TABLE meshy_memory
    ALTER COLUMN meshy_guard_status SET DEFAULT 'quarantined';
