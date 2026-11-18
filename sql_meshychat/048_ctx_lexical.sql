-- MeshyChat, v1.1 step 7 follow-up: the LEXICAL arm's terms for the transcript-context index (pxx answer 1:
-- lexical waits for step 7, in a table of its own). Written by apps/meshsem/ctxjob.py beside each chunk's vector, read
-- by apps/meshsem/ctxsearch.py.
--
-- One row per (space, text_sha256): the digest of the exact prompted string the vector arm embeds, so a repeated chunk
-- shares one row, as it shares one vector (mesh_ctx_vectors). The terms are apps/meshsem/lexical.terms of the chunk's
-- TEXT (not the prompt), the mail arm's tokenizer (divergence 026); `derivation` names it, as in mesh_lexical (010).
-- A GIN index on `lexemes` lets a search fetch only the chunks holding a query term (`lexemes && $terms`), inside the
-- caller's namespace and filters, so the arm never loads the namespace into the process.
--
-- Removal is by reference, as for the vectors: ctxjob's orphan sweep deletes rows no chunk occurrence names.
-- 008's forced projection-writer shape. Down: down/048.
--
-- BACKFILL (pxx point 1): this table is created empty, and lexical rows are written only as a manifest is
-- written. So ctx_index_state records which derivation the space's lexical rows were built with. NULL (every space
-- indexed before 048, and a 048 down then up) or a different derivation makes the next tick delete the space's rows of
-- any other derivation and restart the walk from the start, the way opening the embed gate does (29877b24): idempotent,
-- and unchanged digests make no embed call. `lexical_rewalk_until` is the watermark the walk had reached; until the walk
-- passes it again, search reports the lexical arm as partial.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS mesh_ctx_lexical (
    space        text        NOT NULL,
    text_sha256  text        NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    derivation   text        NOT NULL,
    lexemes      text[]      NOT NULL,
    tfs          integer[]   NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (space, text_sha256),
    CONSTRAINT mesh_ctx_lexical_arrays_pair CHECK (cardinality(lexemes) = cardinality(tfs) AND 0 < ALL (tfs))
);
CREATE INDEX IF NOT EXISTS mesh_ctx_lexical_terms ON mesh_ctx_lexical USING gin (lexemes);

ALTER TABLE ctx_index_state ADD COLUMN IF NOT EXISTS lexical_derivation text,
                            ADD COLUMN IF NOT EXISTS lexical_rewalk_until bigint;

ALTER TABLE mesh_ctx_lexical ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_ctx_lexical FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_ctx_lexical_read ON mesh_ctx_lexical
    FOR SELECT USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_ctx_lexical_insert ON mesh_ctx_lexical
    FOR INSERT WITH CHECK (current_setting('app.server_internal', true) = '1'
                           AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_ctx_lexical_update ON mesh_ctx_lexical
    FOR UPDATE USING (current_setting('app.server_internal', true) = '1'
                      AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_ctx_lexical_delete ON mesh_ctx_lexical
    FOR DELETE USING (current_setting('app.server_internal', true) = '1'
                      AND current_setting('app.projection_writer', true) = '1');
