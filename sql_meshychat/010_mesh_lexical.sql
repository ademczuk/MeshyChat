-- MeshyChat divergence 026: a LEXICAL index beside the vectors, so search ranks by
-- reciprocal-rank fusion of a lexical arm and the embedding arm.
--
-- WHY A LEXICAL ARM AT ALL. The A/B of 2026-09-23 (94 real queries over 929 threaded
-- messages, paired bootstrap, thread-membership proxy): plain TF-IDF matched
-- EmbeddingGemma on MRR (0.572 vs 0.529 no prompt / 0.544 retrieval prompt) and beat it on
-- recall@5 by 0.12 to 0.14 (q <= 0.011); fusing TF-IDF with the no-prompt embedding by
-- reciprocal rank (k=60) gave MRR 0.552 and recall@5 0.517, +0.093 recall@5 over the
-- embedding alone (p=0.001). Embedding-only search, what 019 shipped, was the weakest
-- measured arm on recall. Divergence 026 has the numbers and the measurement of THIS
-- implementation.
--
-- WHAT IS STORED. One row per (message, space): the distinct terms of the SAME text the
-- job embeds (the stripped body cut to 1,500 characters, with no prompt) and each term's
-- frequency, as two parallel arrays, plus a digest of that text. The terms are the A/B's
-- tokens, runs of two or more word characters of the lowercased text
-- (apps/meshsem/lexical.terms, named by `derivation`). Postgres's to_tsvector('simple')
-- was the first implementation and measured worse on the same 94 queries (divergence
-- 026); tsvector ranking (ts_rank, ts_rank_cd) also carries no inverse document
-- frequency. The TF-IDF cosine is computed in the serving process, beside the vector
-- index, from these arrays.
--
-- WHY PER SPACE. The embed job's walk is per space (sql_meshychat/006), and this table is
-- written by that walk and nothing else, so a gate change or a restart applies to both at
-- once. The walk lexes every row it READS that has text and passes the gate, without
-- waiting for the embedder (a tokenizer needs no model), so while the embedder is down
-- new messages are still searchable lexically. A code change to the tokenizer is a new
-- `derivation`, which restarts the walk (mesh_embeddings_projection.lexical_derivation
-- below) instead of mixing two derivations in one index.
--
-- A DERIVATIVE OF CONTENT, SO THE SAME RULES AS THE VECTORS. ON DELETE CASCADE from
-- shared_memory (retention deletes the terms with the message, in the same transaction,
-- the reason 006 keeps vectors in Postgres); the embed_policy gate (the job writes no row
-- for a forbidden message while the gate is closed, a close purges them, and every read
-- joins back to shared_memory and filters again); and the projection-writer rule of
-- sql_meshychat/008 (divergence 022): SELECT under app.server_internal, INSERT, UPDATE and
-- DELETE only with app.projection_writer as well, which only the job sets.
--
-- The serving process loads the rows by (space, indexed_at, src_id), the index below,
-- exactly as it loads the vectors. Nothing queries the terms in SQL, so there is no GIN.
--
-- meshy_app receives its grants through the default privileges declared in sql/010.

CREATE TABLE IF NOT EXISTS mesh_lexical (
    src_id       UUID        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    src_key      TEXT        NOT NULL,
    src_version  INTEGER     NOT NULL,
    space        TEXT        NOT NULL,
    derivation   TEXT        NOT NULL,
    -- sha256 of the exact text tokenized. Rows are immutable, so it changes only when the
    -- derivation of the text does; the job rewrites exactly those rows.
    text_sha256  TEXT        NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    -- Distinct terms and their frequencies, index for index. A text with no term (only
    -- punctuation, say) is two empty arrays: indexed, and never matched.
    lexemes      TEXT[]      NOT NULL,
    tfs          INTEGER[]   NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL,
    indexed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (src_id, space),
    CONSTRAINT mesh_lexical_arrays_pair
        CHECK (cardinality(lexemes) = cardinality(tfs) AND 0 < ALL (tfs))
);

CREATE INDEX IF NOT EXISTS mesh_lexical_space_indexed_idx
    ON mesh_lexical (space, indexed_at, src_id);

-- The derivation the CURRENT walk writes. NULL on a state row written before this file:
-- the job then restarts the walk once, so a space whose vectors were all embedded before
-- the lexical arm existed gets its terms (unchanged vectors cost a digest compare).
ALTER TABLE mesh_embeddings_projection
    ADD COLUMN IF NOT EXISTS lexical_derivation TEXT;

COMMENT ON COLUMN mesh_embeddings_projection.lexical_derivation IS
    'How the current walk derives mesh_lexical terms. A different value in code restarts the walk and drops the space''s lexical rows of other derivations (divergence 026).';

ALTER TABLE mesh_lexical ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_lexical FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_lexical_read ON mesh_lexical
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_lexical_insert ON mesh_lexical
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_lexical_update ON mesh_lexical
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_lexical_delete ON mesh_lexical
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');
