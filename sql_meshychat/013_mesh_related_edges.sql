-- MeshyChat divergence 032: related messages are PERSISTED as inferred `related_to` edges.
--
-- WHAT WAS WRONG. Every message is embedded and keyword-indexed (006, 010) and projected
-- into the DECLARED graph (004: addressed_to, authored_by, in_thread, replies_to, ...), but
-- semantic relationships were not in the graph at all: /v1/mesh/context computed "related
-- messages from other threads" per request and threw them away. The inferred-edge table of
-- 007 allows only addressed_to / in_thread / replies_to, and no job fills it on a schedule.
--
-- WHAT THIS HOLDS. One row per (space, newer message, older message) that a job scored with
-- HQB's cross-encoder reranker IN BOTH DIRECTIONS and that crossed a CALIBRATED threshold
-- (apps/meshsem/related.py writes, apps/meshsem/relatedcal.py calibrates). Measured on HQB's
-- bge-reranker-v2-m3 (2026-09-23, synthetic text): scores are raw logits (a paraphrase
-- pair 2.5, unrelated pairs about -11) and strongly ASYMMETRIC (A->B +2.545 but B->A
-- -1.342), so the decision is the MEAN of the two directions and both are stored.
--
-- WHY A SIBLING TABLE AND NOT A FOURTH edge_type IN 007's mesh_inferred_edges:
--   1. the target must CASCADE: 007's `dst` is deliberately not a foreign key (a label, a
--      thread id, a key); a related edge points at a message, and a deleted message must
--      take the edge with it, from EITHER end;
--   2. the provenance does not fit: 007 carries a chat model's (model, provider_id,
--      prompt_version); this carries two logits, a pair score, the threshold, the rank,
--      the reranker's canary digest and the embedding space;
--   3. apps/meshaudit/store.coverage counts EVERY edge_type in 007's table into the
--      auditor's denominators, so a related_to type there would change the auditor's
--      numbers;
--   4. the two follow different gates: a related edge is derived from message CONTENT, so
--      the embed_policy gate must purge it; an auditor edge is not under that gate.
-- Nothing that reads mesh_edges or mesh_inferred_edges can see a row of this table (021's
-- wall, one table further out): a reader opts in by naming it, which only
-- apps/meshsem/related_reads.py does.
--
-- NO INFERENCE IS EVIDENCE FOR ANOTHER. The job reads DECLARED mesh_edges, and only to
-- EXCLUDE candidates (the source's own thread, its reply parent and its children). It never
-- reads mesh_inferred_edges, and it never reads this table except to delete a source's own
-- outgoing rows before rewriting them (tests/test_meshsem_related.py T18 scans for it).
--
-- ONE ROW PER PAIR, newer -> older. Candidates are strictly OLDER than their source, so a
-- pair is scored once, by its newer member; the CHECK on (recorded_at, id) below makes the
-- reverse duplicate unrepresentable. Reads treat the edge as undirected.
--
-- A BELOW-THRESHOLD EDGE IS UNREPRESENTABLE: CHECK (pair_score >= threshold), and both
-- logits must be finite. The threshold is copied onto the row with the digest of the
-- calibration that set it (threshold_version), and reads serve only the current version.
--
-- The space is part of the key (PRIMARY KEY (space, src_id, dst_id)): the pool a pair was
-- drawn from depends on the embedding space's vectors, so two spaces are two edge sets. The
-- design sketch keyed (src_id, dst_id) alone; that would make a space switch collide with
-- the previous space's rows.
--
-- recorded_at and dst_recorded_at are shared_memory.created_at copies (004's clock).
-- RLS: 008's four-policy split (divergence 022): SELECT under app.server_internal; INSERT,
-- UPDATE and DELETE only with app.projection_writer as well, which only the job sets. The
-- ON DELETE CASCADE still runs under a request (RLS does not block a referential action,
-- 008's measured test), so retention (sql/061) removes an edge in the same transaction as
-- either of its messages. meshy_app's grants come from sql/010's default privileges.

CREATE TABLE IF NOT EXISTS mesh_related_edges (
    src_id            UUID        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    src_key           TEXT        NOT NULL CHECK (src_key LIKE 'msg/%'),
    src_version       INTEGER     NOT NULL,
    dst_id            UUID        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    dst_key           TEXT        NOT NULL CHECK (dst_key LIKE 'msg/%'),
    dst_version       INTEGER     NOT NULL,
    edge_type         TEXT        NOT NULL DEFAULT 'related_to'
                                  CHECK (edge_type = 'related_to'),
    -- The embedding space whose vectors built the candidate pool (019's identity).
    space             TEXT        NOT NULL,
    -- mesh_embeddings.provider_id of the source's vector. Diagnostic.
    embed_provider_id TEXT        NOT NULL,
    -- The reranker, pinned for the whole tick after its canaries matched the calibration.
    rerank_provider_id TEXT       NOT NULL CHECK (rerank_provider_id <> ''),
    -- Its DECLARED model name. llama-server echoes names, so this is not identity;
    -- rerank_reference (the canary digest it was admitted under) is.
    rerank_model      TEXT        NOT NULL CHECK (rerank_model <> ''),
    rerank_reference  TEXT        NOT NULL CHECK (rerank_reference ~ '^[0-9a-f]{64}$'),
    threshold_version TEXT        NOT NULL CHECK (threshold_version ~ '^[0-9a-f]{16,64}$'),
    -- Raw logits. forward: the source as the query; reverse: the target as the query.
    score_fwd         DOUBLE PRECISION NOT NULL,
    score_rev         DOUBLE PRECISION NOT NULL,
    pair_score        DOUBLE PRECISION NOT NULL,
    threshold         DOUBLE PRECISION NOT NULL,
    -- Among the source's written edges, and the target's place in the fused pool.
    rank              SMALLINT    NOT NULL CHECK (rank BETWEEN 1 AND 20),
    fused_rank        INTEGER     NOT NULL CHECK (fused_rank >= 1),
    -- The arms' own scores; NULL where that arm did not rank the target.
    cosine            REAL,
    lexical_score     REAL,
    recorded_at       TIMESTAMPTZ NOT NULL,
    dst_recorded_at   TIMESTAMPTZ NOT NULL,
    inferred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (space, src_id, dst_id),
    CONSTRAINT mesh_related_edges_rank_unique UNIQUE (space, src_id, rank),
    CONSTRAINT mesh_related_edges_not_self
        CHECK (src_id <> dst_id AND src_key <> dst_key),
    CONSTRAINT mesh_related_edges_newer_to_older
        CHECK ((recorded_at, src_id) > (dst_recorded_at, dst_id)),
    CONSTRAINT mesh_related_edges_above_threshold CHECK (pair_score >= threshold),
    CONSTRAINT mesh_related_edges_finite
        CHECK (score_fwd <> 'NaN'::float8 AND score_rev <> 'NaN'::float8
               AND pair_score <> 'NaN'::float8 AND threshold <> 'NaN'::float8
               AND abs(score_fwd) < 'Infinity'::float8
               AND abs(score_rev) < 'Infinity'::float8
               AND abs(threshold) < 'Infinity'::float8)
);

-- Both ends are read (the edge is undirected on the read side) and both cascade.
CREATE INDEX IF NOT EXISTS mesh_related_edges_dst_idx
    ON mesh_related_edges (dst_id, pair_score DESC);
CREATE INDEX IF NOT EXISTS mesh_related_edges_src_idx
    ON mesh_related_edges (src_id, pair_score DESC);

-- ONE ROW PER (source, space, threshold version) THE JOB HAS DECIDED, whatever it found:
-- the denominator of related coverage, and "processed" for a reader. Without it "scored
-- and none crossed the threshold" is indistinguishable from "never scored". The outcomes:
-- edges; none_above_threshold (pairs were scored both ways and none crossed);
-- no_candidates (an empty pool); targets_gone (pairs crossed, and every chosen target was
-- superseded, gated or deleted before the write); rerank_refused (a non-empty pool none
-- of whose pairs the reranker would score both ways). pairs_refused counts the pairs the
-- reranker refused twice (apps/meshsem/related.py FAILURES): never scored, never an edge.
CREATE TABLE IF NOT EXISTS mesh_related_runs (
    src_id            UUID        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    space             TEXT        NOT NULL,
    threshold_version TEXT        NOT NULL,
    outcome           TEXT        NOT NULL CHECK (outcome IN ('edges', 'none_above_threshold',
                                                              'no_candidates', 'targets_gone',
                                                              'rerank_refused')),
    pool_size         INTEGER     NOT NULL CHECK (pool_size >= 0),
    reverse_scored    INTEGER     NOT NULL CHECK (reverse_scored >= 0),
    edges             INTEGER     NOT NULL CHECK (edges >= 0),
    rerank_provider_id TEXT       NOT NULL,
    pairs_refused     INTEGER     NOT NULL DEFAULT 0 CHECK (pairs_refused >= 0),
    processed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (src_id, space, threshold_version),
    CONSTRAINT mesh_related_runs_outcome_matches_edges CHECK ((outcome = 'edges') = (edges > 0))
);

-- One row per space: the calibration version and embed_policy gate the CURRENT walk ran
-- under, its keyset watermark (created_at, id), counters, and why the last tick ended.
CREATE TABLE IF NOT EXISTS mesh_related_projection (
    space                 TEXT        PRIMARY KEY,
    threshold_version     TEXT,
    forbidden_rows_open   BOOLEAN     NOT NULL DEFAULT FALSE,
    watermark_at          TIMESTAMPTZ,
    watermark_id          UUID,
    rows_seen             BIGINT      NOT NULL DEFAULT 0,
    sources_related       BIGINT      NOT NULL DEFAULT 0,
    sources_none_above    BIGINT      NOT NULL DEFAULT 0,
    sources_no_candidates BIGINT      NOT NULL DEFAULT 0,
    sources_targets_gone  BIGINT      NOT NULL DEFAULT 0,
    sources_rerank_refused BIGINT     NOT NULL DEFAULT 0,
    pairs_refused         BIGINT      NOT NULL DEFAULT 0,
    rows_skipped_forbidden BIGINT     NOT NULL DEFAULT 0,
    rows_skipped_no_vector BIGINT     NOT NULL DEFAULT 0,
    edges_written         BIGINT      NOT NULL DEFAULT 0,
    last_status           TEXT        NOT NULL DEFAULT 'never_run' CHECK (last_status IN (
                              'never_run', 'ok', 'not_calibrated', 'embed_not_run',
                              'projection_not_run', 'rerank_unavailable',
                              'rerank_not_admitted', 'rerank_provider_changed',
                              'concurrent_walk', 'deadline', 'relate_bound')),
    last_status_at        TIMESTAMPTZ,
    last_detail           TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT mesh_related_projection_watermark_pairs
        CHECK ((watermark_at IS NULL) = (watermark_id IS NULL))
);

-- ------------------------------------------------------------ RLS, 008's split (022)
ALTER TABLE mesh_related_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_related_edges FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_related_edges_read ON mesh_related_edges
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_related_edges_insert ON mesh_related_edges
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_related_edges_update ON mesh_related_edges
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_related_edges_delete ON mesh_related_edges
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');

ALTER TABLE mesh_related_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_related_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_related_runs_read ON mesh_related_runs
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_related_runs_insert ON mesh_related_runs
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_related_runs_update ON mesh_related_runs
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_related_runs_delete ON mesh_related_runs
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');

ALTER TABLE mesh_related_projection ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_related_projection FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_related_projection_read ON mesh_related_projection
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_related_projection_insert ON mesh_related_projection
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_related_projection_update ON mesh_related_projection
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
CREATE POLICY mesh_related_projection_delete ON mesh_related_projection
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1'
           AND current_setting('app.projection_writer', true) = '1');

-- ------------------------------------------- the graph route that reads them (018 (d))
-- GET /v1/mesh/neighbours is a new mesh read route, and 005's route set is a CLOSED CHECK:
-- without this its every usage count would fail (logged and counted, never failing the
-- request). apps/mesh/usage.ROUTES carries the same list; tests compare the two.
ALTER TABLE mesh_route_usage DROP CONSTRAINT IF EXISTS mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours'));

COMMENT ON TABLE mesh_related_edges IS
    'Persisted related_to edges between head shared/mesh-chat messages (apps/meshsem/related.py, divergence 032): newer -> older, scored by the reranker in both directions, kept only at or above the calibrated threshold. Served as tier=inferred by /v1/mesh/context and /v1/mesh/neighbours?include_inferred=1. Written only by the mesh_related_edges job.';
COMMENT ON TABLE mesh_related_runs IS
    'One row per (source message, space, threshold_version) the related_to job decided, whatever it found: the denominator of related coverage (divergence 032).';
COMMENT ON TABLE mesh_related_projection IS
    'One row per space: the calibration version and embed_policy gate the related_to walk runs under, its keyset watermark (created_at, id), counters and last status (divergence 032).';
