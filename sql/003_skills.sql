-- spec §5.2: skills table (replaces tool_manifests in legacy schema).
-- Greenfield schema: no backfill, no rename from tool_manifests.

CREATE TABLE skills (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    peer_id               UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    version               TEXT NOT NULL,
    display_name          TEXT,
    description           TEXT,
    mode                  TEXT NOT NULL CHECK (mode IN ('sync','async','stream')),
    share_mode            TEXT NOT NULL
                          CHECK (share_mode IN ('private','grants_only','marketplace_free','marketplace_paid','public')),
    input_schema          JSONB NOT NULL,
    output_schema         JSONB,
    attachments_allowed   BOOLEAN NOT NULL DEFAULT FALSE,
    returns_attachments   BOOLEAN NOT NULL DEFAULT FALSE,
    max_runtime_ms        INTEGER,
    price_cents           INTEGER,
    currency              TEXT,
    tags                  TEXT[] NOT NULL DEFAULT '{}',
    examples              JSONB,
    is_current            BOOLEAN NOT NULL DEFAULT TRUE,
    deleted_at            TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (peer_id, name, version),
    -- Paid marketplace skills MUST carry price + currency.
    CHECK (share_mode <> 'marketplace_paid' OR (price_cents IS NOT NULL AND currency IS NOT NULL))
);

-- Spec-listed indexes (§5.2):
CREATE INDEX skills_peer_name_deleted_idx ON skills (peer_id, name, deleted_at);
CREATE INDEX skills_name_share_idx        ON skills (name, share_mode);
CREATE INDEX skills_tags_gin              ON skills USING GIN (tags);

-- Partial unique guards the "one current version per (peer, name)" invariant.
-- Inserting a new version must first flip the previous current row to FALSE
-- (handled in the meshy-skills repository).
CREATE UNIQUE INDEX skills_one_current_per_name
    ON skills (peer_id, name)
    WHERE is_current = TRUE AND deleted_at IS NULL;

CREATE TRIGGER skills_updated_at
    BEFORE UPDATE ON skills
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
