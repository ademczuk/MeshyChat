-- MeshyChat divergence 014: a rebuildable edge projection over shared/mesh-chat.
--
-- WHY. Measured on production on 2026-09-21, and every number below is from that
-- measurement rather than a guess:
--
--   memory.query calls, 7 days        175,859     (1,047 per hour, sustained)
--   memory.write calls, 7 days          2,407     (read:write is 75.8 to 1)
--   server-side recipient filter        NONE      (handle_query filters namespace,
--                                                  key_prefix, status, tags, author,
--                                                  since, contains; never `to` or `cc`)
--   hand-rolled `to`/`cc` parsers       between 10 (purpose-built pollers, cited) and
--                                       68 (every file touching the field) in ~/.dxxxxx
--   `to` stored as a STRING            16,181 rows
--   `to` stored as an ARRAY             5,617 rows
--   thread named `thread_id`           13,725 rows    all four of these arriving
--   thread named `thread`               6,166 rows    on the SAME day, 1,960 rows
--   reply named `in_reply_to`             942 rows    carrying both thread keys
--   reply named `reply_to`                674 rows
--
-- So every one of ~176k weekly reads pages the whole namespace and parses recipients
-- client-side, and the parsing is written dozens of times in two languages because the
-- relation it needs is spelled four ways across two types inside an opaque JSONB blob.
-- The event-sourcing literature names this exact state as its first documented regret:
-- "ad hoc read-model logic scattered across consumers instead of one deliberate,
-- rebuildable projection". This file is the deliberate one.
--
-- WHAT THIS IS, AND IS NOT. `shared_memory` is an append-only, immutable, version-chained
-- log: an event store. `mesh_edges` is a READ MODEL derived from it and nothing else. It
-- is never a source of truth, no request path writes it, and it can be TRUNCATEd and
-- rebuilt from the log at any moment with zero loss (apps/mesh/projection.py `rebuild`).
-- That is the discipline that keeps it a projection rather than a second truth, and the
-- test suite exercises the rebuild so the property is measured rather than promised.
--
-- WHY IT IS NOT AN EXPRESSION INDEX. The obvious shortcut is an index over
-- `CASE jsonb_typeof(value->'to') WHEN 'string' THEN jsonb_build_array(...)`. RED-proved
-- refused: `jsonb_build_array` is STABLE, and an index expression must be IMMUTABLE.
-- `sql_meshychat/003` gets away with `value->>'subject'` because a declared act has ONE
-- shape and `->>` is immutable. Mesh messages have five shapes, and normalizing five
-- shapes needs a function call, which is exactly what an index expression forbids. So the
-- normalization lives in Python (apps/mesh/normalize.py) and the result lands in real
-- typed columns that CAN be indexed. A sixth spelling then costs one branch in code, not
-- a migration.
--
-- WHY POSTGRES AND NOT OBJECT STORAGE. docs/V1-DESIGN-RULES.md E8: derived artifacts are
-- "rebuilt by a job", never committed, never gated on. This table is all three. E8's
-- "object storage" clause is about where a REGENERATED FILE lives so it cannot freeze
-- main; a projection read on the request path at 0.3 QPS by the same process that reads
-- shared_memory belongs in the transactional tier, which is the fleet's own ruling
-- (shared-KG wiki/a-warehouse-is-the-wrong-hot-store-for-an-agent-graph: "put shared
-- cross-session context and provenance in a transactional service built for small reads
-- and writes"). The 58 partial indexes already in this schema are derived artifacts in
-- Postgres for the same reason.
--
-- WHY src_field IS A COLUMN. It records which spelling and which type the WRITER used
-- for the edge. That makes the drift measurable from the projection itself
-- (`SELECT src_field, count(*) ... GROUP BY 1`) instead of from ad hoc production
-- queries, so a migration finishing can be watched as a number going to zero rather than
-- believed.
--
-- WHY recorded_at IS COPIED AND value->>'ts' IS NOT. Same reason as sql_meshychat/003:
-- `ts` is the sender's claimed clock, measured future-dated by up to 67 minutes on this
-- fleet. `created_at` is assigned by the database doing the ordering.
--
-- RLS. shared_memory is readable only under app.server_internal = '1' (sql/028:276-281).
-- A projection of it must be at least as restricted, so the same policy is applied here,
-- ENABLE and FORCE, and apps/core/rls.assert_rls_active() can be pointed at this table as
-- a control. meshy_app receives its grants through the default privileges declared in
-- sql/010, so no GRANT appears here.

CREATE TABLE IF NOT EXISTS mesh_edges (
    -- The row this edge was projected from. ON DELETE CASCADE: retention pruning on the
    -- log (sql/061, sql/111) removes the projection with it, so an edge can never point
    -- at a message that no longer exists. That is the referential integrity the wiki's
    -- own citation links lack (measured 85 dangling of 890 on 2026-09-21).
    src_id       UUID        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    src_key      TEXT        NOT NULL,
    src_version  INTEGER     NOT NULL,

    -- The relation. A CLOSED set: adding a kind is a migration, on purpose, because an
    -- open-vocabulary edge type would reintroduce the four-spellings problem one level up.
    edge_type    TEXT        NOT NULL CHECK (edge_type IN (
                     'authored_by',   -- message -> the AUTHENTICATED author label
                     'addressed_to',  -- message -> a `to` recipient label
                     'copied_to',     -- message -> a `cc` recipient label
                     'in_thread',     -- message -> its thread id
                     'replies_to',    -- message -> the msg/ key it answers
                     'owes_ack'       -- message -> a recipient who owes an ack
                 )),
    dst          TEXT        NOT NULL,

    -- Which field name and value shape the writer used. Diagnostic, not semantic: two
    -- edges that differ only here are the same relation spelled differently.
    src_field    TEXT        NOT NULL,

    recorded_at  TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (src_id, edge_type, dst)
);

-- The read the whole projection exists for: "messages addressed to X", newest first.
-- (edge_type, dst) leads so one recipient's edges are contiguous, recorded_at trails
-- so the newest-first order is served from the index rather than by a sort.
CREATE INDEX IF NOT EXISTS mesh_edges_dst_idx
    ON mesh_edges (edge_type, dst, recorded_at DESC);

-- Reverse lookups: everything about one message, and everything in one thread that a
-- thread walk then needs to find by key.
CREATE INDEX IF NOT EXISTS mesh_edges_src_key_idx
    ON mesh_edges (src_key);

-- One row per projection, recording how far the catch-up has read. The keyset is
-- (created_at, id), which is the same ordering memory.query pages on
-- (apps/memoryskills/validation.py CURSOR_FIELD_TYPES: c, i), so the projector and the
-- reader agree on what "after" means.
CREATE TABLE IF NOT EXISTS mesh_edges_projection (
    projection      TEXT        PRIMARY KEY,
    watermark_at    TIMESTAMPTZ,
    watermark_id    UUID,
    rows_projected  BIGINT      NOT NULL DEFAULT 0,
    edges_projected BIGINT      NOT NULL DEFAULT 0,
    rebuilt_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT mesh_edges_projection_watermark_pairs
        CHECK ((watermark_at IS NULL) = (watermark_id IS NULL))
);

ALTER TABLE mesh_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_edges FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_edges_server_internal ON mesh_edges
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER TABLE mesh_edges_projection ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_edges_projection FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_edges_projection_server_internal ON mesh_edges_projection
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

COMMENT ON TABLE mesh_edges IS
    'Rebuildable edge projection over shared/mesh-chat (apps/mesh). A read model, never a source of truth: TRUNCATE and rebuild from shared_memory at will. Only apps/mesh/projection.py writes it.';
COMMENT ON COLUMN mesh_edges.src_field IS
    'The field name and value shape the writer used (e.g. to:string, thread, reply_to). Makes schema drift measurable from the projection itself.';
COMMENT ON TABLE mesh_edges_projection IS
    'Catch-up watermark for mesh_edges, keyset (created_at, id). One row.';
