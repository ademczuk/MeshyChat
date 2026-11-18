-- MeshyChat divergence 019: semantic vectors over shared/mesh-chat, per EMBEDDING SPACE.
--
-- WHAT A SPACE IS, AND WHY IT IS THE KEY RATHER THAN A MODEL NAME. A space is (model
-- family, dims, prompt style), e.g. 'embeddinggemma-300m/768/retrieval-v1'. Two
-- vectors are only comparable when they come from the same space, and a model NAME
-- does not establish that: llama-server echoes whatever `model` the client sent
-- (measured 2026-09-23: both embedders on this LAN answered "model":"x" to a request
-- that said "x"), and apps/modelhost's embed probe checks shape and discrimination but
-- never IDENTITY, so a different 768-d model would pass it. A provider is therefore
-- ADMITTED to a space only when its canary vectors match the space's recorded
-- reference at cosine >= 0.999 (apps/meshsem/spaces.py). Measured on the same
-- EmbeddingGemma-300M Q8_0 GGUF, HQB (CUDA) vs HQD (Vulkan), five canaries under both
-- prompts, alone and mixed into 16-document batches: worst of 45 condition pairs
-- 0.999613. The same model WITHOUT the space's prompt scored 0.8538-0.9170 against it,
-- so a prompt-style mismatch is refused as a different space, which it is.
--
-- MUST-8 (sql/028:35-37, 106-108): "v0.2 ships NO embedding ... there is NO embedding /
-- vector column". This file keeps that sentence true of shared_memory: no column is
-- added to it and its writers are untouched (every writer still hard-codes
-- embed_policy = 'forbidden', apps/memoryskills/handlers.py:792-796). The vectors live
-- in a SEPARATE table that a job fills, and the job skips 'forbidden' rows unless the
-- operator opens MESHY_EMBED_FORBIDDEN_ROWS (default closed). Model calls go only to a
-- loopback or RFC1918 host; the guard is in apps/meshsem/providers.py.
--
-- WHY POSTGRES AND NOT OBJECT STORAGE, departing from docs/V1-DESIGN-RULES.md E8
-- ("graphs, embeddings and indexes live in object storage, rebuilt by a job"). Rebuilt
-- by a job: yes, and never committed or gated on. Object storage: no, BECAUSE retention
-- deletes messages (sql/061, sql/111) and ON DELETE CASCADE below guarantees that a
-- deleted message's vector dies in the same transaction. An embedding is a lossy copy
-- of the text; a file of embeddings would keep a derivative of content the retention
-- policy has already deleted, with nothing to notice. The FK is the mechanism, not a
-- convention a cleanup job has to remember.
--
-- WHY recorded_at IS COPIED FROM shared_memory.created_at, as sql_meshychat/004 does:
-- the database's clock, not the sender's claimed `ts`.
--
-- WHY THERE IS NO head-ness HERE, as 004: the only legal mutation on shared_memory is
-- an is_head flip, an UPDATE no watermark sees. Every read joins back to
-- shared_memory.is_head.
--
-- RLS: ENABLE + FORCE, the same app.server_internal policy shape as sql_meshychat/004.
-- meshy_app receives its grants through the default privileges declared in sql/010.

CREATE TABLE IF NOT EXISTS mesh_embeddings (
    -- ON DELETE CASCADE is the reason this table is in Postgres at all (see above).
    src_id       UUID        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    src_key      TEXT        NOT NULL,
    src_version  INTEGER     NOT NULL,

    space        TEXT        NOT NULL,
    dims         INTEGER     NOT NULL CHECK (dims > 0),

    -- sha256 of the EXACT string the model embedded (prompt applied). shared_memory rows
    -- are immutable, so this changes only when the derivation changes (truncation, body
    -- extraction); the job re-embeds exactly those rows and skips the rest.
    text_sha256  TEXT        NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),

    -- float32, little-endian, `dims` of them. The CHECK makes a truncated or
    -- wrong-width vector unrepresentable instead of a decode error on the read path.
    vec          BYTEA       NOT NULL,

    -- Which ADMITTED provider produced it. Diagnostic: failover inside one space is
    -- legal, so a space legitimately holds vectors from more than one provider.
    provider_id  TEXT        NOT NULL,

    recorded_at  TIMESTAMPTZ NOT NULL,
    embedded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (src_id, space),
    CONSTRAINT mesh_embeddings_vec_width CHECK (octet_length(vec) = 4 * dims)
);

-- The in-process index refreshes incrementally by (embedded_at, src_id) per space.
CREATE INDEX IF NOT EXISTS mesh_embeddings_space_embedded_idx
    ON mesh_embeddings (space, embedded_at, src_id);

-- One row per space: its REFERENCE canaries and the job's keyset watermark.
CREATE TABLE IF NOT EXISTS mesh_embeddings_projection (
    space                   TEXT        PRIMARY KEY,
    dims                    INTEGER     CHECK (dims IS NULL OR dims > 0),

    -- The reference: the canary vectors of the FIRST provider admitted to this space,
    -- and a digest of the canary INPUTS they answer. A reader whose canary set differs
    -- (someone edited the sentences without naming a new space) refuses rather than
    -- comparing vectors of different sentences.
    canary_sha256           TEXT        CHECK (canary_sha256 IS NULL
                                               OR canary_sha256 ~ '^[0-9a-f]{64}$'),
    reference_vecs          BYTEA,
    reference_provider      TEXT,
    reference_recorded_at   TIMESTAMPTZ,

    -- The embed_policy gate the CURRENT walk ran under. When the operator flips
    -- MESHY_EMBED_FORBIDDEN_ROWS the job restarts the walk (and on a close, deletes the
    -- vectors of forbidden rows), so a gate change is never half-applied.
    forbidden_rows_open     BOOLEAN     NOT NULL DEFAULT FALSE,

    watermark_at            TIMESTAMPTZ,
    watermark_id            UUID,
    rows_seen               BIGINT      NOT NULL DEFAULT 0,
    rows_embedded           BIGINT      NOT NULL DEFAULT 0,
    rows_skipped_forbidden  BIGINT      NOT NULL DEFAULT 0,
    rows_skipped_empty      BIGINT      NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT mesh_embeddings_projection_watermark_pairs
        CHECK ((watermark_at IS NULL) = (watermark_id IS NULL)),
    CONSTRAINT mesh_embeddings_projection_reference_whole
        CHECK ((reference_vecs IS NULL) = (reference_provider IS NULL)
           AND (reference_vecs IS NULL) = (canary_sha256 IS NULL)
           AND (reference_vecs IS NULL) = (dims IS NULL)
           AND (reference_vecs IS NULL) = (reference_recorded_at IS NULL))
);

-- THE REFERENCE IS WRITE-ONCE. Every stored vector was admitted against it, so a
-- reference that changed underneath them would silently make the space incomparable
-- with itself. Re-referencing a space means deleting its row (and its vectors) on
-- purpose, or naming a new space; an UPDATE that rewrites it is refused here, below
-- every writer, so no code path can drift it by accident.
CREATE OR REPLACE FUNCTION mesh_embeddings_reference_write_once() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.reference_vecs IS NOT NULL AND (
           NEW.reference_vecs        IS DISTINCT FROM OLD.reference_vecs
        OR NEW.reference_provider    IS DISTINCT FROM OLD.reference_provider
        OR NEW.canary_sha256         IS DISTINCT FROM OLD.canary_sha256
        OR NEW.dims                  IS DISTINCT FROM OLD.dims
        OR NEW.reference_recorded_at IS DISTINCT FROM OLD.reference_recorded_at)
    THEN
        RAISE EXCEPTION 'mesh_embeddings_projection: the reference of space % is write-once', OLD.space
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS mesh_embeddings_reference_write_once_trg ON mesh_embeddings_projection;
CREATE TRIGGER mesh_embeddings_reference_write_once_trg
    BEFORE UPDATE ON mesh_embeddings_projection
    FOR EACH ROW EXECUTE FUNCTION mesh_embeddings_reference_write_once();

ALTER TABLE mesh_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_embeddings FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_embeddings_server_internal ON mesh_embeddings
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER TABLE mesh_embeddings_projection ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_embeddings_projection FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_embeddings_projection_server_internal ON mesh_embeddings_projection
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

COMMENT ON TABLE mesh_embeddings IS
    'Per-space vectors of shared/mesh-chat rows (apps/meshsem, divergence 019). A read model: written only by the mesh_embed_catch_up job and its CLI; ON DELETE CASCADE so retention deletes a vector with its message.';
COMMENT ON TABLE mesh_embeddings_projection IS
    'One row per embedding space: write-once reference canaries, the embed_policy gate the walk ran under, and the keyset watermark (created_at, id).';
