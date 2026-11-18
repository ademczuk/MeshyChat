-- MeshyChat divergence 037: the request ledger behind thread and backlog summaries.
--
-- WHAT IT IS. One row per summary request that passed REQUEST admission
-- (apps/meshsum/admission.py; docs/design/thread-summaries.md 5.3 and 6.2): who asked, when,
-- what kind of window, how big it was and what was left out of it, whether and when a
-- model call was made, how it ended, and the model's provenance. It is the counter the
-- admission limits read (a per-process limiter multiplies by the number of processes,
-- which is why sql_meshychat/005 made mesh_route_usage a table), and the record the
-- measurement and a later durable-cache decision read (design 6.4, Q8).
--
-- WHAT IT NEVER HOLDS. No message text, no message key, no thread id and no summary
-- text. `covered_digest` (sha256 over sorted (row id, version) pairs) is enough to tell
-- two requests for the same set apart; a thread id is a label a writer chose. Nothing
-- derived from message text is stored anywhere (design 6.1): a durable summary would be a
-- derivative of content that retention deletes (sql/061, sql/111) and a request-written
-- table derived from shared_memory, which divergence 022 forbids.
--
-- WHAT IT IS NOT. A projection: it is a primary record of requests, as mesh_route_usage
-- is, and tests/test_projection_writer.py lists it under NOT_PROJECTIONS with that reason.
--
-- STATES. `admitted` when request admission inserts it; `calling` with `lease_until` =
-- now() + 60 s and `called_at` when model admission lets a call go to HQB; `done` when the
-- request settles. A call the client abandoned at its budget is NOT abandoned by HQB,
-- which keeps generating to max_tokens, so such a row keeps `state = 'calling'` (with its
-- outcome recorded) until its lease passes, and the fleet in-flight cap counts HQB's work
-- rather than the client's patience (critique C15). A crashed process's row stops counting
-- when its lease passes.
--
-- OUTCOMES, a closed set. The design's (6.2): generated, cache_hit, rejected, unavailable,
-- busy, rate_limited (a MODEL budget), model_changed, withheld, no_text, nothing_new; and,
-- so that every way a request that reached the ledger can end has a name: embed_forbidden
-- and not_projected (the typed absences of design 1.3 that make no call), not_found (a
-- thread with no declared or pending message), and error (a fault after admission). A
-- request refused BEFORE request admission (a 4xx on parameters, E_SUMMARY_DISABLED, the
-- request budget itself) writes no row: a peer past its limit cannot grow this table, and
-- mesh_route_usage counts those refusals.
--
-- RLS. ENABLE and FORCE; SELECT, INSERT, UPDATE and DELETE each bound to
-- app.server_internal = '1' (divergence 029: every UPDATE and DELETE policy is bound to a
-- context). The request path and the retention job share that context, so no policy can
-- tell them apart; tests/test_meshsum_admission.py scans apps/meshsum's SQL and holds that
-- the retention function is the only DELETE on this table.
--
-- COST (B5). Every admission count is a range on an index below: per peer, per owner,
-- fleet-wide by time, and the in-flight count on a partial index over `calling` rows.
--
-- RETENTION, stated with the table (E7): rows older than 30 days are deleted by the
-- registered job `mesh_summary_ledger_retention` (apps/jobs/registry.py), as a range on
-- `requested_at`, in bounded batches, never keyed on a live object's identity (A1).

CREATE TABLE IF NOT EXISTS mesh_summary_requests (
    id                        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    peer_id                   UUID        NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    owner_id                  UUID        NOT NULL,
    requested_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    scope_kind                TEXT        NOT NULL CHECK (scope_kind IN ('thread', 'backlog')),
    covered_digest            TEXT,
    covered_count             INTEGER     CHECK (covered_count >= 0),
    excluded_embed_forbidden  INTEGER     NOT NULL DEFAULT 0 CHECK (excluded_embed_forbidden >= 0),
    excluded_no_text          INTEGER     NOT NULL DEFAULT 0 CHECK (excluded_no_text >= 0),
    excluded_cut_to_fit       INTEGER     NOT NULL DEFAULT 0 CHECK (excluded_cut_to_fit >= 0),
    excluded_source_changed   INTEGER     NOT NULL DEFAULT 0 CHECK (excluded_source_changed >= 0),
    state                     TEXT        NOT NULL DEFAULT 'admitted'
                                          CHECK (state IN ('admitted', 'calling', 'done')),
    lease_until               TIMESTAMPTZ,
    called_at                 TIMESTAMPTZ,
    outcome                   TEXT        CHECK (outcome IN (
                                  'generated', 'cache_hit', 'rejected', 'unavailable', 'busy',
                                  'rate_limited', 'model_changed', 'withheld', 'no_text',
                                  'nothing_new', 'embed_forbidden', 'not_projected',
                                  'not_found', 'error')),
    sentences_kept            INTEGER     CHECK (sentences_kept >= 0),
    sentences_dropped         INTEGER     CHECK (sentences_dropped >= 0),
    drop_reasons              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    summary_version           TEXT        NOT NULL,
    model_pin                 TEXT,
    provider_id               TEXT,
    latency_ms                INTEGER     CHECK (latency_ms >= 0),
    prompt_tokens             INTEGER     CHECK (prompt_tokens >= 0),
    completion_tokens         INTEGER     CHECK (completion_tokens >= 0),
    finish_reason             TEXT,
    -- A `calling` row always has a lease: the in-flight cap is `calling AND lease_until >
    -- now()`, and a row with no lease would count forever or never.
    CHECK (state <> 'calling' OR lease_until IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS mesh_summary_requests_peer_idx
    ON mesh_summary_requests (peer_id, requested_at);
CREATE INDEX IF NOT EXISTS mesh_summary_requests_owner_idx
    ON mesh_summary_requests (owner_id, requested_at);
CREATE INDEX IF NOT EXISTS mesh_summary_requests_at_idx
    ON mesh_summary_requests (requested_at);
CREATE INDEX IF NOT EXISTS mesh_summary_requests_calling_idx
    ON mesh_summary_requests (lease_until) WHERE state = 'calling';
-- Model-call counts are ranges on called_at (per peer, per owner, fleet).
CREATE INDEX IF NOT EXISTS mesh_summary_requests_called_idx
    ON mesh_summary_requests (called_at) WHERE called_at IS NOT NULL;

ALTER TABLE mesh_summary_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE mesh_summary_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY mesh_summary_requests_read ON mesh_summary_requests
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_summary_requests_insert ON mesh_summary_requests
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_summary_requests_update ON mesh_summary_requests
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY mesh_summary_requests_delete ON mesh_summary_requests
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1');

COMMENT ON TABLE mesh_summary_requests IS
    'One row per summary request past request admission (apps/meshsum, divergence 037): counts, states, outcome and model provenance. No message text, key, thread id or summary text. A primary record of requests, not a projection. Retention 30 days via the mesh_summary_ledger_retention job.';

-- ------------------------------------------- the two summary routes' usage counts (018 (d))
-- 005's route set is a CLOSED CHECK; apps/mesh/usage.ROUTES carries the same list and a
-- test compares the two (widened by 013 for `neighbours`).
ALTER TABLE mesh_route_usage DROP CONSTRAINT IF EXISTS mesh_route_usage_route_check;
ALTER TABLE mesh_route_usage ADD CONSTRAINT mesh_route_usage_route_check
    CHECK (route IN ('inbox', 'owed', 'thread', 'drift', 'wait', 'neighbours',
                     'summary_thread', 'summary_backlog'));
