-- MeshyChat divergence 021: edges a model INFERRED for legacy mesh rows, kept apart from
-- the edges their senders DECLARED.
--
-- WHY. sql_meshychat/004 projects what each message's sender wrote: `to`, `cc`,
-- `thread_id`/`thread`, `in_reply_to`/`reply_to`. A row that wrote none of them cannot be
-- placed, and measured on production on 2026-09-21 that is not a corner: 5,525 rows carry
-- NEITHER thread key and 1,655 carry no `to` (apps/mesh/normalize.py). A chat model can
-- read such a row and say which thread it looks like it belongs to. apps/meshaudit asks
-- one (on the LAN only, MUST-8), makes it SELECT among candidates, and refuses every pick
-- that is not an existing fact. What survives lands HERE.
--
-- WHY A SEPARATE TABLE AND NOT A COLUMN ON mesh_edges. "A model thinks this was addressed
-- to txxxx" and "the sender addressed it to txxxx" must never be one row type with a flag:
-- every existing reader of mesh_edges (the inbox, owed acks, the thread walk, the drift
-- numbers, apps/meshsem's context route) would then start answering from inferences the
-- moment one was written, unless each of them remembered to filter. Divergence 008 drew the
-- same wall for the board (`inferred.<verb>` is keyed apart and excluded from
-- `SubjectState.has()`). Here the wall is a table boundary: nothing that reads mesh_edges
-- can see an inference, and a reader opts in by naming this table, which only the
-- `?include_inferred=1` path does. `python -m apps.mesh rebuild` deletes and replays
-- mesh_edges and never touches this table, so a rebuild cannot launder an inference into a
-- declaration either.
--
-- THE CLOSED SET IS SMALLER THAN 004's. addressed_to, in_thread, replies_to. `authored_by`
-- is the authenticated label and is never missing. `copied_to` is not something prose
-- settles. `owes_ack` is deliberately absent: an inferred ack debt would page a peer on a
-- model's word.
--
-- ONE THREAD, ONE REPLY TARGET. A message is in one thread and answers one message, so a
-- second inferred value for either is unrepresentable (the partial unique index below), not
-- merely unlikely. 014 refuses to project two threads for one row on the strength of a
-- writer's hedge; a model's hedge gets the same answer.
--
-- PROVENANCE ON EVERY ROW: which model, through which declared provider, under which prompt
-- (apps/meshaudit/prompt.py PROMPT_VERSION carries a digest of the template). An inference
-- is only as good as the question that produced it, and a reader must be able to tell two
-- wordings apart.
--
-- ON DELETE CASCADE from shared_memory, as 004 and 006: retention deletes an inference with
-- the message it is about. The DESTINATION is not a foreign key (a label, a thread id, a
-- key), exactly as in mesh_edges; reads join the SOURCE back to shared_memory.is_head.
--
-- RLS: ENABLE + FORCE with the app.server_internal policy shape of sql_meshychat/004.
-- meshy_app receives its grants through the default privileges declared in sql/010.

CREATE TABLE IF NOT EXISTS mesh_inferred_edges (
    src_id          UUID        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    src_key         TEXT        NOT NULL,
    src_version     INTEGER     NOT NULL,

    edge_type       TEXT        NOT NULL CHECK (edge_type IN (
                        'addressed_to',   -- message -> a peer label the model selected
                        'in_thread',      -- message -> a thread id that mesh_edges declares
                        'replies_to'      -- message -> an existing, earlier msg/ key
                    )),
    dst             TEXT        NOT NULL CHECK (length(dst) BETWEEN 1 AND 512),

    -- Who inferred it. `model` is the declared model name, `provider_id` the registry
    -- entry it was reached through (apps/modelhost/registry.py); both non-empty.
    model           TEXT        NOT NULL CHECK (model <> ''),
    provider_id     TEXT        NOT NULL CHECK (provider_id <> ''),
    prompt_version  TEXT        NOT NULL CHECK (prompt_version <> ''),

    -- shared_memory.created_at of the source row, copied as 004 copies it, so inferred and
    -- declared hits sort on the same clock.
    recorded_at     TIMESTAMPTZ NOT NULL,
    inferred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (src_id, edge_type, dst),
    CONSTRAINT mesh_inferred_edges_reply_is_a_msg_key
        CHECK (edge_type <> 'replies_to' OR dst LIKE 'msg/%'),
    CONSTRAINT mesh_inferred_edges_no_self_reply
        CHECK (edge_type <> 'replies_to' OR dst <> src_key)
);

-- One thread and one reply target per message.
CREATE UNIQUE INDEX IF NOT EXISTS mesh_inferred_edges_single_valued
    ON mesh_inferred_edges (src_id, edge_type)
    WHERE edge_type IN ('in_thread', 'replies_to');

-- The opt-in reads: "inferred as addressed to X" and "inferred as in thread T", newest
-- first, the same shape as mesh_edges_dst_idx.
CREATE INDEX IF NOT EXISTS mesh_inferred_edges_dst_idx
    ON mesh_inferred_edges (edge_type, dst, recorded_at DESC);

-- ONE ROW PER (message, prompt version) THE AUDITOR HAS ASKED ABOUT, whether or not
-- anything survived validation. Without it a run cannot tell "asked, and the model
-- declined" from "never asked", so every run would re-ask the same rows forever, and a
-- coverage figure would have no denominator. Written only with --write, in the same
-- transaction as the edges it produced.
CREATE TABLE IF NOT EXISTS mesh_inferred_audits (
    src_id          UUID        NOT NULL REFERENCES shared_memory(id) ON DELETE CASCADE,
    prompt_version  TEXT        NOT NULL CHECK (prompt_version <> ''),
    -- 'asked': the model answered (whatever it said, refusals included).
    -- 'no_body': nothing to read, so nobody was asked.
    -- 'nothing_to_ask': the row declares every relation after all (normalize.py agrees
    --                   with the sender, whatever the projection said).
    outcome         TEXT        NOT NULL CHECK (outcome IN ('asked', 'no_body',
                                                            'nothing_to_ask')),
    model           TEXT,
    provider_id     TEXT,
    asked           TEXT[]      NOT NULL DEFAULT '{}',
    accepted        INTEGER     NOT NULL DEFAULT 0 CHECK (accepted >= 0),
    -- reason -> count, apps/meshaudit/validate.py's named refusals.
    refused         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    audited_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (src_id, prompt_version),
    CONSTRAINT mesh_inferred_audits_model_iff_asked
        CHECK ((outcome = 'asked') = (model IS NOT NULL AND provider_id IS NOT NULL))
);

ALTER TABLE mesh_inferred_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_inferred_edges FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_inferred_edges_server_internal ON mesh_inferred_edges
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER TABLE mesh_inferred_audits ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_inferred_audits FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_inferred_audits_server_internal ON mesh_inferred_audits
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

COMMENT ON TABLE mesh_inferred_edges IS
    'Edges a LAN chat model inferred for mesh rows that declare none (apps/meshaudit, divergence 021). Never read by the declared projection; served only under ?include_inferred=1, each hit tier=inferred. Written only by python -m apps.meshaudit derive --write.';
COMMENT ON TABLE mesh_inferred_audits IS
    'One row per (message, prompt_version) the auditor asked about, whatever survived validation: the denominator for inferred coverage and the reason a run does not re-ask.';
