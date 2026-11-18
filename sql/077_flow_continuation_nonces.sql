-- 077_flow_continuation_nonces.sql
-- TASK-59 AC#2: server-issued flow continuation nonces.
--
-- Each allow-frame gets a short-lived bearer nonce bound to the parent call and
-- its caller/target tuple. A downstream call presents that nonce in its flow
-- object; authorize consumes it exactly once and derives the parent call_id from
-- this server state.

-- Idempotent: a re-run (or a partial-apply retry after a crash) must not error.
-- ADD COLUMN IF NOT EXISTS, CREATE TABLE/INDEX IF NOT EXISTS. The inline CHECK
-- rides the CREATE TABLE IF NOT EXISTS so it is skipped when the table exists.

ALTER TABLE invocation_frames
    ADD COLUMN IF NOT EXISTS continuation_nonce TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS invocation_frames_continuation_nonce_idx
    ON invocation_frames (continuation_nonce)
    WHERE continuation_nonce IS NOT NULL;

ALTER TABLE skill_calls
    ADD COLUMN IF NOT EXISTS continuation_nonce TEXT;

CREATE TABLE IF NOT EXISTS flow_continuation_nonces (
    nonce               TEXT        PRIMARY KEY,
    parent_call_id      TEXT        NOT NULL,
    caller_owner_id     UUID        NOT NULL,
    caller_peer_id      UUID        NOT NULL,
    target_owner_id     UUID        NOT NULL,
    target_peer_id      UUID        NOT NULL,
    issued_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    consumed_at         TIMESTAMPTZ,
    consumed_by_call_id TEXT,
    CHECK (consumed_at IS NULL OR consumed_by_call_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS flow_continuation_nonces_parent_call_idx
    ON flow_continuation_nonces (parent_call_id);

CREATE INDEX IF NOT EXISTS flow_continuation_nonces_expires_idx
    ON flow_continuation_nonces (expires_at);
