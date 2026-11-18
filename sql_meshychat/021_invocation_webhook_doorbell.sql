-- MeshyChat divergence 041, slice S9: the invocation webhook doorbell's three tables.
--
-- NUMBERING, LOUDLY. This is the skill-frames track's THIRD migration, beyond its 016
-- reservation. Checked at build time (2026-09-25): origin/main (8caac46) holds 001 to 014 and
-- 017; 015 is reserved to Biscuit, 016 and 018 are this track's; 019 is TAKEN by Biscuit's
-- second migration (019_capability_revocation.sql, on fin/biscuit, not yet on origin) and 020
-- by fin/w1-grants (020_grants_list_indexes.sql). So this is 021, the next letter-free free
-- number. `sqlmigrate.discover` refuses a duplicate version, not a gap.
--
-- WHAT A DOORBELL IS (design docs/design/skill-frames-port.md 4.11, the approved Q2, Q3, Q9).
-- A call to a target with no socket open waits `queued` (041 S5). If the target registered a
-- URL and proved it controls it, the call worker POSTs a POINTER there, signed with a
-- dedicated per-host Ed25519 key, and the woken target connects its socket; redelivery then
-- hands it the call. The doorbell carries no args and moves no status.
--
-- peer_invocation_webhooks: one registration per peer, written by PUT and DELETE
-- /v1/peers/me/invocation-webhook (apps/peers/views.py, apps/calls/webhooks.py). A PUT
-- stores the URL UNVERIFIED with a fresh 32-byte challenge (43 base64url characters); the
-- worker POSTs the challenge to it and sets `verified_at` only when the 2xx body is the
-- challenge, byte for byte. `disabled_at` is written by the worker BEFORE that POST, as its
-- claim (rule B7): a verification is attempted once per PUT (at most once per 60 s per
-- peer, below), and a URL that fails stays disabled and unverified until the peer PUTs
-- again. Only a row with `verified_at` set and `disabled_at` NULL is ever rung.
--
-- peer_webhook_verifications: the verification's delivery record (the S9 review of a6bb87af,
-- findings F1 and F3). The worker's claim of a registration inserts one row, stamped with the
-- same `claimed_at` it writes to the registration's `disabled_at`, BEFORE its POST, and the
-- outcome (`verified`, `not_echoed`, `superseded`, egress's reason, `internal`, or
-- `abandoned` for a claim taken again) and its HTTP status are written after. Two rules read
-- it. THE FLOOR: a peer is verified at most once per 60 s, whatever it PUTs or DELETEs; a PUT
-- inside the floor is stored and its verification waits (review F1: one peer re-PUTting
-- bought a signed POST per PUT and held every doorbell behind its verifications). The row is
-- keyed to the PEER, not the registration, so DELETE then PUT does not reset it. A CLAIM THAT
-- NEVER FINISHED (a crash, a restart past the worker's join) is taken again once the floor
-- has passed, as a doorbell's is (review F3); a verification that finished unechoed is not
-- retried: the peer PUTs again.
--
-- NOT upstream's `peers.invocation_delivery_webhook` (sql/009), which this server never
-- writes or reads: a Rust server on the same database rings that column UNSIGNED
-- (route.rs:109-115, design 2.11 and 5.5, critique F10), so a URL registered here must be
-- invisible to it.
--
-- peer_webhook_attempts: the delivery record (rule B7). Step 9 inserts a `due` row in the
-- same transaction that makes a call `queued`, when the target has a verified registration
-- and was not rung in the last 60 s (4.4). The partial UNIQUE (peer_id) WHERE state IN ('due',
-- 'claimed') holds at most one doorbell outstanding per peer, whatever inserts it; step 9
-- inserts with ON CONFLICT DO NOTHING on it (critique F11: the first design's UNIQUE
-- (call_id, id) bounded nothing). The worker claims a row (`claimed`, claimed_by, claimed_at)
-- BEFORE it POSTs, and records `sent`, `failed` or `skipped` with the outcome after. A claim
-- older than 30 s is a crashed worker's, and the worker's sweep takes it again. `created_at`
-- is beyond the design's column list, for the audit only.
--
-- POINTERS AT COMMIT. The first two tables NOTIFY `meshy_call_webhook`, which run_call_worker LISTENs
-- on: `{"verify": <peer_id>}` when a registration is written unverified (a PUT), and
-- `{"attempt": <id>}` when an attempt is inserted. pg_notify is transactional, so the worker
-- hears of neither before its commit. A NOTIFY is unauthenticated and the payload is only a
-- wake-up: the worker re-reads the tables and decides. The worker's own writes (verified_at,
-- disabled_at, an attempt's outcome, a verification record) notify nothing. A verification
-- the floor defers is found by the worker's 5 s sweep.
--
-- RLS: every command of every table here requires `app.server_internal = '1'` (4.11 states it for
-- the registrations; the attempts get the same, since a request that could write one could
-- aim a doorbell at a peer). ENABLE and FORCE, one policy per command, the shape of 016. The
-- ON DELETE CASCADEs are referential actions, which RLS does not bind. meshy_app's grants,
-- the sequences' included, come from sql/010's default privileges, so no GRANT appears here.
-- The verification record gets the same four policies.
--
-- DEPLOY ORDER (review F5). Forward: apply this, then roll the code. S9 code on a schema
-- without this file fails step 9 of every invoke to an offline target, doorbells on or off
-- (`apps/calls/route._route_remote` calls `webhooks.insert_due` on every queued edge), and
-- the call stays `pending`; the serving process does not check the schema at start, only
-- `run_jobs` and `run_call_worker` do. Pre-S9 code on this schema never touches these
-- tables. Back: roll the code back, then run the down script,
-- sql_meshychat/down/021_invocation_webhook_doorbell_down.sql, outside the migrate set.

-- A BOUNDED WAIT FOR ITS LOCKS (review F4). The foreign keys below take SHARE ROW EXCLUSIVE on
-- `peers` and `skill_calls`. Queued behind one open writer of either, that request would hold
-- every later writer of the table behind it for as long as the writer runs. Give up after 5 s
-- instead and retry when the table is quiet, as 014 does (after upstream's sql/100:4 and
-- sql/105:82). LOCAL: the runner applies each file in its own transaction, and the timeout
-- ends with it.
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS peer_invocation_webhooks (
    peer_id       uuid        PRIMARY KEY REFERENCES peers(id) ON DELETE CASCADE,
    url           text        NOT NULL CHECK (char_length(url) BETWEEN 1 AND 2048),
    challenge     text        NOT NULL CHECK (challenge ~ '^[A-Za-z0-9_-]{43}$'),
    requested_at  timestamptz NOT NULL DEFAULT now(),
    verified_at   timestamptz,
    disabled_at   timestamptz
);

CREATE TABLE IF NOT EXISTS peer_webhook_attempts (
    id            bigserial   PRIMARY KEY,
    peer_id       uuid        NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    call_id       text        NOT NULL REFERENCES skill_calls(call_id) ON DELETE CASCADE,
    state         text        NOT NULL DEFAULT 'due'
                  CHECK (state IN ('due', 'claimed', 'sent', 'failed', 'skipped')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    claimed_by    text,
    claimed_at    timestamptz,
    finished_at   timestamptz,
    http_status   integer,
    error_class   text
);

CREATE TABLE IF NOT EXISTS peer_webhook_verifications (
    id            bigserial   PRIMARY KEY,
    peer_id       uuid        NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    url           text        NOT NULL CHECK (char_length(url) BETWEEN 1 AND 2048),
    claimed_by    text        NOT NULL,
    claimed_at    timestamptz NOT NULL,
    finished_at   timestamptz,
    outcome       text,
    http_status   integer,
    CHECK ((finished_at IS NULL) = (outcome IS NULL))
);

-- One doorbell outstanding per peer.
CREATE UNIQUE INDEX IF NOT EXISTS peer_webhook_attempts_one_outstanding
    ON peer_webhook_attempts (peer_id) WHERE state IN ('due', 'claimed');
-- The worker's claim scan.
CREATE INDEX IF NOT EXISTS peer_webhook_attempts_open_idx
    ON peer_webhook_attempts (id) WHERE state IN ('due', 'claimed');
-- Step 9's 60 s floor: this peer's last rings.
CREATE INDEX IF NOT EXISTS peer_webhook_attempts_peer_claimed_idx
    ON peer_webhook_attempts (peer_id, claimed_at);
-- The call's ON DELETE CASCADE.
CREATE INDEX IF NOT EXISTS peer_webhook_attempts_call_idx
    ON peer_webhook_attempts (call_id);
-- The verification floor and the unfinished-claim link: this peer's last claims.
CREATE INDEX IF NOT EXISTS peer_webhook_verifications_peer_claimed_idx
    ON peer_webhook_verifications (peer_id, claimed_at);

ALTER TABLE peer_invocation_webhooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE peer_invocation_webhooks FORCE ROW LEVEL SECURITY;
CREATE POLICY peer_invocation_webhooks_read ON peer_invocation_webhooks
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_invocation_webhooks_insert ON peer_invocation_webhooks
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_invocation_webhooks_update ON peer_invocation_webhooks
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_invocation_webhooks_delete ON peer_invocation_webhooks
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1');

ALTER TABLE peer_webhook_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE peer_webhook_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY peer_webhook_attempts_read ON peer_webhook_attempts
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_webhook_attempts_insert ON peer_webhook_attempts
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_webhook_attempts_update ON peer_webhook_attempts
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_webhook_attempts_delete ON peer_webhook_attempts
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1');

ALTER TABLE peer_webhook_verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE peer_webhook_verifications FORCE ROW LEVEL SECURITY;
CREATE POLICY peer_webhook_verifications_read ON peer_webhook_verifications
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_webhook_verifications_insert ON peer_webhook_verifications
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_webhook_verifications_update ON peer_webhook_verifications
    FOR UPDATE
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
CREATE POLICY peer_webhook_verifications_delete ON peer_webhook_verifications
    FOR DELETE
    USING (current_setting('app.server_internal', true) = '1');

CREATE OR REPLACE FUNCTION meshy_webhook_verify_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_catalog.pg_notify(
        'meshy_call_webhook',
        pg_catalog.json_build_object('verify', NEW.peer_id)::text
    );
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION meshy_webhook_attempt_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_catalog.pg_notify(
        'meshy_call_webhook',
        pg_catalog.json_build_object('attempt', NEW.id)::text
    );
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS peer_invocation_webhooks_verify_notify ON peer_invocation_webhooks;
CREATE TRIGGER peer_invocation_webhooks_verify_notify
    AFTER INSERT OR UPDATE ON peer_invocation_webhooks
    FOR EACH ROW
    WHEN (NEW.verified_at IS NULL AND NEW.disabled_at IS NULL)
    EXECUTE FUNCTION meshy_webhook_verify_notify();

DROP TRIGGER IF EXISTS peer_webhook_attempts_notify ON peer_webhook_attempts;
CREATE TRIGGER peer_webhook_attempts_notify
    AFTER INSERT ON peer_webhook_attempts
    FOR EACH ROW
    EXECUTE FUNCTION meshy_webhook_attempt_notify();

COMMENT ON TABLE peer_invocation_webhooks IS
    'Divergence 041 S9: a peer''s invocation doorbell URL, rung only once verified by a challenge echo. Never upstream''s peers.invocation_delivery_webhook, which a Rust server rings unsigned. Every command requires app.server_internal.';
COMMENT ON TABLE peer_webhook_attempts IS
    'Divergence 041 S9: one row per doorbell, claimed before its POST; at most one due or claimed per peer. Every command requires app.server_internal.';
COMMENT ON TABLE peer_webhook_verifications IS
    'Divergence 041 S9 (review F1, F3): one row per verification POST, claimed before it; a peer is verified at most once per 60 s, and a claim that never finished is taken again. Every command requires app.server_internal.';
COMMENT ON FUNCTION meshy_webhook_verify_notify() IS
    'Divergence 041 S9: NOTIFY meshy_call_webhook {verify: peer_id} when a registration is written unverified. A pointer: the worker re-reads.';
COMMENT ON FUNCTION meshy_webhook_attempt_notify() IS
    'Divergence 041 S9: NOTIFY meshy_call_webhook {attempt: id} when a doorbell attempt is inserted. A pointer: the worker re-reads.';
