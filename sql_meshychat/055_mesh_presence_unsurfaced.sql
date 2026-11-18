-- Step C: count and timestamps only. -1 means incomplete evidence (including overflow).
SET lock_timeout = '2s';
ALTER TABLE mesh_presence
    ADD COLUMN unsurfaced INTEGER CHECK (unsurfaced BETWEEN -1 AND 2048),
    ADD COLUMN unsurfaced_oldest_at TIMESTAMPTZ,
    ADD COLUMN unsurfaced_since TIMESTAMPTZ,
    ADD CONSTRAINT mesh_presence_unsurfaced_pair CHECK ((unsurfaced IS NULL AND unsurfaced_since IS NULL OR unsurfaced IS NOT NULL AND unsurfaced_since IS NOT NULL) AND CASE
        WHEN unsurfaced IS NULL THEN unsurfaced_oldest_at IS NULL
        WHEN unsurfaced = 0 THEN unsurfaced_oldest_at IS NULL
        WHEN unsurfaced > 0 THEN unsurfaced_oldest_at IS NOT NULL
        ELSE unsurfaced = -1 END);
