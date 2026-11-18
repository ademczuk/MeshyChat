-- 033: models registry. The Model Registry analog of the skills table (003):
-- where a skill is a callable capability a peer exposes, a model is an
-- inference backend (LLM / vision / audio) a peer advertises it can serve.
-- Mirrors the skills schema so the dashboard data layer and visibility scoping
-- reuse the same patterns; no model content is baked into the schema (rows are
-- registered at runtime by each peer from its own serving inventory).
--
-- No GRANTs needed: migration 010's `ALTER DEFAULT PRIVILEGES IN SCHEMA public
-- ... GRANT ... ON TABLES TO meshy_app` covers tables created by later
-- migrations (same precedent as skills/owner_api_keys).

CREATE TABLE models (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    peer_id               UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    version               TEXT NOT NULL DEFAULT 'v1',
    display_name          TEXT,
    description           TEXT,
    -- "Local", "OpenAI", "Anthropic", "xAI", etc. Free-form; the SPA badges a
    -- few known providers and defaults the rest.
    provider              TEXT,
    -- Free-form capability labels (e.g. text, vision, audio, red-team) the SPA
    -- renders as chips and filters on.
    capabilities          TEXT[] NOT NULL DEFAULT '{}',
    context_window        INTEGER,
    cost_per_1k_input     NUMERIC,
    cost_per_1k_output    NUMERIC,
    -- Optional serving hints sourced from a peer's local pool (nullable, purely
    -- informational; never load-bearing for auth or routing).
    vram_gb               INTEGER,
    port                  INTEGER,
    share_mode            TEXT NOT NULL DEFAULT 'private'
                          CHECK (share_mode IN ('private','grants_only','marketplace_free','marketplace_paid','public')),
    is_current            BOOLEAN NOT NULL DEFAULT TRUE,
    deleted_at            TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (peer_id, name, version)
);

CREATE INDEX models_peer_name_deleted_idx ON models (peer_id, name, deleted_at);
CREATE INDEX models_provider_idx          ON models (provider);
CREATE INDEX models_capabilities_gin      ON models USING GIN (capabilities);

-- One current version per (peer, name) - mirrors skills_one_current_per_name.
CREATE UNIQUE INDEX models_one_current_per_name
    ON models (peer_id, name)
    WHERE is_current = TRUE AND deleted_at IS NULL;

CREATE TRIGGER models_updated_at
    BEFORE UPDATE ON models
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
