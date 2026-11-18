-- MeshyChat, v1.1 step 6 (docs/design/context-v1.1-transcript-ingest.md): the schema the transcript-context index walks.
-- Code plan and its review: kxxx, pxx GO 241bfc (with three additions, answered below). Nothing here reads or writes
-- an item's text; the walker (apps/meshsem/ctxjob.py, the next PR) is the only writer of the side tables.
--
-- 1. THE COMMIT ORDER of context MANIFESTS, ctx_commit_order. The mechanism is sql_meshychat/023's, in 042's hardened
--    shape: a DEFERRED constraint trigger runs at COMMIT, takes a transaction-scoped advisory lock and only then draws the
--    next seq, so a reader that sees seq N sees every committed seq below N, and a manifest whose transaction started first
--    and committed last is still walked. The lock is slot 2 of the same two-integer key, (1835233133, 2): every existing
--    stamper (023, 027, 029, 042) uses slot 1, so context manifests serialize only among themselves and never behind mail.
--    Only MANIFESTS are stamped (key 'ctxm/%'): a batch is walked when its manifest commits, never item by item.
--
-- 2. WHICH NAMESPACE. pxx: the configured context namespace, not a literal, because indexing carries no operator
--    ruling and the rows 6-7 scratch server (shared/scratch-transcript-context) must index too. A trigger cannot see the
--    server's Python settings, so the namespace is a one-row table, ctx_index_config, seeded with the default; a server
--    configured with another MESHY_CONTEXT_NAMESPACE updates the row (the walker refuses to run when the two disagree).
--    The function compares BEFORE taking the lock, so a write elsewhere never waits on it.
--
-- 3. THE SIDE TABLES.
--    ctx_index_state      one row per embedding space: the walk's watermark (a seq), attempts on the manifest it is stuck
--                         on, and whether the gate was open.
--    mesh_ctx_chunks      one row per chunk OCCURRENCE: which manifest (src_id), its namespace, seat, src, gen, batch, project, chunk_no,
--                         first_ord..last_ord and character offsets, so a hit collapses to its exact items (the provenance
--                         pick). UNIQUE (src_id, space, chunk_no) makes a re-walk idempotent.
--    mesh_ctx_vectors     one vector per (space, text_sha256): a chunk repeated anywhere reuses it, with no GPU call.
--                         NOT mesh_embeddings, although it does hold chunk vectors (graphwalk.py, divergence 060): search's
--                         in-process index loads that table and has a 100,000-vector ceiling, which F3 transcript volume
--                         (63,000 to 86,000 chunks a day) would fill in about two days, taking mail search with it, until
--                         step 6b's out-of-process store exists; it stores a vector per (row, chunk), not per digest; and a
--                         turn-aware chunk spans several items, so its provenance needs mesh_ctx_chunks' columns.
--    ctx_dead_letter      a manifest that failed 5 times for a non-transient reason, written in the SAME transaction that
--                         moves the watermark past it, so the watermark never passes a manifest that is neither embedded
--                         nor dead-lettered. state: dead -> replay (an operator) -> done (the replay loop).
--    Lexical rows wait for step 7 (pxx answer 1).
--
-- 4. pxx's three additions (241bfc), as the schema decides them:
--    a. A TERMINAL MANIFEST for a batch that already had one (the replace_refused overlay): the walker deletes the earlier
--       manifest's chunk rows for the same (src, gen, batch) in the transaction that writes the new one. The index on
--       (src, gen, batch) is what makes that a lookup. Test: after the terminal manifest, no chunk carries the omitted text.
--    b. A SUPERSEDED GEN drops out of RETRIEVAL in step 7's query (latest gen per seat and src); the rows stay, so a
--       re-walk and the audit can still see them. The (seat, src, gen) index serves that filter. Test lives with step 7.
--    c. REMOVAL by reference: a vector goes only when no chunk row in its space still names its digest. The walker deletes
--       orphaned vectors in the same transaction as the chunk rows (one walker per space, held by the watermark check), and
--       ctx_vectors_orphaned() is the census a test and the coverage report read. No ON DELETE CASCADE from chunks to
--       vectors: a digest is shared, so a cascade would delete a vector another occurrence still uses.
--
-- READ SIDE. Every table is readable under app.server_internal only. The commit order and config are written by no
-- request or job (the definer trigger, the owner); the four derived tables take 008's projection shape. Down: down/047.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS ctx_index_config (
    id          smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    namespace   text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO ctx_index_config (id, namespace) VALUES (1, 'shared/transcript-context') ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS ctx_commit_order (
    seq         bigserial   PRIMARY KEY,
    memory_id   uuid        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    kind        text        NOT NULL DEFAULT 'write' CHECK (kind IN ('write')),
    stamped_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (memory_id, kind)
);

CREATE TABLE IF NOT EXISTS ctx_index_state (
    space           text        PRIMARY KEY,
    watermark_seq   bigint,
    stuck_seq       bigint,
    stuck_attempts  integer     NOT NULL DEFAULT 0 CHECK (stuck_attempts >= 0),
    gate_open       boolean     NOT NULL DEFAULT false,
    -- Bumped by every reset of the walk (the gate opening, a lexical rebuild). A walker keeps the epoch it started
    -- with and refuses to commit under another (a reviewer on #386: a position check alone passes when a reset puts
    -- the watermark back where a stale walker happens to be).
    walk_epoch      bigint      NOT NULL DEFAULT 0,
    -- When the orphan sweep last ran for the space: it runs at most every SWEEP_EVERY_S, not on every tick (Codex on
    -- #386: an anti-join over the space's whole vector set each tick).
    swept_at        timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mesh_ctx_chunks (
    id           bigserial   PRIMARY KEY,
    src_id       uuid        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    space        text        NOT NULL,
    -- The namespace the manifest was committed in (pxx P1): every read is bound to the namespace the caller was
    -- authorised for, so an index built under one configured namespace never answers a search on another.
    namespace    text        NOT NULL,
    chunk_no     integer     NOT NULL CHECK (chunk_no >= 0),
    seat         text        NOT NULL,
    src          text        NOT NULL,
    gen          integer     NOT NULL,
    batch        integer     NOT NULL,
    project      text,
    first_ord    integer     NOT NULL,
    last_ord     integer     NOT NULL,
    char_start   integer     NOT NULL CHECK (char_start >= 0),
    char_end     integer     NOT NULL,
    time_first   timestamptz NOT NULL,
    time_last    timestamptz NOT NULL,
    text_sha256  text        NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (src_id, space, chunk_no),
    CHECK (last_ord >= first_ord),
    CHECK (char_end > char_start)
);
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_seat_time ON mesh_ctx_chunks (namespace, seat, time_last);
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_project_time ON mesh_ctx_chunks (namespace, project, time_last);
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_batch ON mesh_ctx_chunks (src, gen, batch);
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_seat_src_gen ON mesh_ctx_chunks (namespace, seat, src, gen);
CREATE INDEX IF NOT EXISTS mesh_ctx_chunks_digest ON mesh_ctx_chunks (space, text_sha256);

CREATE TABLE IF NOT EXISTS mesh_ctx_vectors (
    space        text        NOT NULL,
    text_sha256  text        NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    dim          integer     NOT NULL CHECK (dim > 0),
    vector       bytea       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (space, text_sha256)
);

-- The newest INDEXED generation of each source (pxx): written by the walker in every manifest's commit,
-- whether or not the manifest produced chunks, so a newest gen made only of omissions still retires the older gen
-- from retrieval. Retrieval filters on this, not on max(gen) over the chunks.
CREATE TABLE IF NOT EXISTS ctx_source_gen (
    -- Per embedding space (a reviewer on #386): a candidate space indexing gen 2 must not retire gen 1 in the space
    -- that is serving.
    space       text        NOT NULL,
    namespace   text        NOT NULL,
    seat        text        NOT NULL,
    src         text        NOT NULL,
    gen         integer     NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (space, namespace, seat, src)
);

CREATE TABLE IF NOT EXISTS ctx_dead_letter (
    seq         bigint      NOT NULL,
    memory_id   uuid        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    space       text        NOT NULL,
    reason      text        NOT NULL,
    attempts    integer     NOT NULL CHECK (attempts > 0),
    at          timestamptz NOT NULL DEFAULT now(),
    state       text        NOT NULL DEFAULT 'dead' CHECK (state IN ('dead', 'replay', 'done')),
    -- (space, seq): a re-walk in a new embedding space meets the same poison manifest without colliding (Codex, #384).
    PRIMARY KEY (space, seq)
);

-- The commit order and the config: readable under server_internal, written by NO request and no job. ctx_commit_order's
-- only writer is the definer trigger (023's posture: RLS enabled, not forced, so the definer writes whatever its standing);
-- ctx_index_config is configuration the owner role sets.
DO $ctx_rls_records$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['ctx_index_config', 'ctx_commit_order'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR SELECT '
                       'USING (current_setting(''app.server_internal'', true) = ''1'')', t || '_read', t);
        EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.%I FROM meshychat_app', t);
    END LOOP;
END
$ctx_rls_records$;

-- The derived tables: 008's projection shape (as sql_meshychat/013's mesh_related_runs), FORCED. Read under
-- server_internal; every write needs server_internal AND projection_writer, which only the job modules set, so a request
-- can never write the index.
DO $ctx_rls_projection$
DECLARE
    t text;
    w text := '(current_setting(''app.server_internal'', true) = ''1'' '
              'AND current_setting(''app.projection_writer'', true) = ''1'')';
BEGIN
    FOREACH t IN ARRAY ARRAY['ctx_index_state', 'mesh_ctx_chunks', 'mesh_ctx_vectors', 'ctx_dead_letter',
                             'ctx_source_gen'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR SELECT '
                       'USING (current_setting(''app.server_internal'', true) = ''1'')', t || '_read', t);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR INSERT WITH CHECK %s', t || '_insert', t, w);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR UPDATE USING %s WITH CHECK %s', t || '_update', t, w, w);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR DELETE USING %s', t || '_delete', t, w);
    END LOOP;
END
$ctx_rls_projection$;

CREATE OR REPLACE FUNCTION public.meshy_ctx_commit_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    -- The namespace first, holding nothing: a write outside the context namespace never waits on the lock.
    IF NOT EXISTS (SELECT 1 FROM public.ctx_index_config c WHERE c.id = 1 AND c.namespace = NEW.namespace) THEN
        RETURN NULL;
    END IF;
    -- Held to this transaction's end: the next stamper draws after this one is visible (023's protocol, slot 2).
    PERFORM pg_advisory_xact_lock(1835233133, 2);
    INSERT INTO public.ctx_commit_order (memory_id, kind)
    SELECT NEW.id, 'write' WHERE EXISTS (SELECT 1 FROM public.shared_memory WHERE id = NEW.id)
    ON CONFLICT (memory_id, kind) DO NOTHING;
    RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.meshy_ctx_commit_stamp() FROM PUBLIC;

DROP TRIGGER IF EXISTS shared_memory_ctx_commit_order ON shared_memory;
CREATE CONSTRAINT TRIGGER shared_memory_ctx_commit_order
    AFTER INSERT ON shared_memory
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.key LIKE 'ctxm/%')
    EXECUTE FUNCTION public.meshy_ctx_commit_stamp();

-- The census reference (4c): vectors no chunk occurrence names any more. Read by the coverage report and the tests.
CREATE OR REPLACE FUNCTION public.ctx_vectors_orphaned(p_space text) RETURNS bigint
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT count(*) FROM public.mesh_ctx_vectors v
     WHERE v.space = p_space
       AND NOT EXISTS (SELECT 1 FROM public.mesh_ctx_chunks c
                        WHERE c.space = v.space AND c.text_sha256 = v.text_sha256);
$$;

-- HISTORY: manifests committed before this file, stamped in (created_at, id) order. CREATE TRIGGER took SHARE ROW EXCLUSIVE
-- on shared_memory, so every manifest either committed before it (stamped here) or waits for this file (stamped by the
-- trigger). Only the configured namespace's manifests, which here means the seeded default.
--
-- A SERVER CONFIGURED WITH ANOTHER MESHY_CONTEXT_NAMESPACE (a scratch server; Codex, #384): the walker refuses to run
-- while ctx_index_config disagrees with the server (apps/meshsem/ctxjob.ConfigMismatch), so nothing is indexed under the
-- wrong name. The owner then sets the row and stamps that namespace's existing manifests, in one transaction, under the
-- trigger's lock so no new commit interleaves:
--   BEGIN; SELECT pg_advisory_xact_lock(1835233133, 2);
--   UPDATE ctx_index_config SET namespace = '<ns>', updated_at = now() WHERE id = 1;
--   INSERT INTO ctx_commit_order (memory_id, kind) SELECT id, 'write' FROM shared_memory
--    WHERE namespace = '<ns>' AND key LIKE 'ctxm/%' ORDER BY created_at, id ON CONFLICT (memory_id, kind) DO NOTHING;
--   COMMIT;
INSERT INTO ctx_commit_order (memory_id, kind)
SELECT sm.id, 'write' FROM shared_memory sm, ctx_index_config c
 WHERE c.id = 1 AND sm.namespace = c.namespace AND sm.key LIKE 'ctxm/%'
 ORDER BY sm.created_at, sm.id
ON CONFLICT (memory_id, kind) DO NOTHING;
