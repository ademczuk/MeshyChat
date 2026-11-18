-- TASK-28 AC#1: interactive pause/resume columns on skill_calls.
--
-- A paused call (status input_required / auth_required, added in migration 080)
-- stashes the elicited JSON-Schema + the caller's resume context here. The
-- resume_nonce + resume_spent_at pair is an INDEPENDENT single-use spend ledger:
-- it is deliberately NOT the flow_continuation_nonces table (TASK-62), because
-- those nonces are minted inside authorize() under flow_guard_mode, so a
-- pause/resume that leaned on them would break when flow-guard is Observe/off.
-- The resume continuation_token (server-signed, tpid = the original caller) is
-- verified by the caller; this row is the server's single-use record of whether
-- a given (call_id, resume_nonce) has already been consumed.
--
-- All ADD COLUMN IF NOT EXISTS so a re-apply over an out-of-band object is a
-- no-op (TASK-77 idempotency; the 074 crash-loop class).
ALTER TABLE skill_calls ADD COLUMN IF NOT EXISTS pause_context_id TEXT;
ALTER TABLE skill_calls ADD COLUMN IF NOT EXISTS pause_requested_schema JSONB;
ALTER TABLE skill_calls ADD COLUMN IF NOT EXISTS resume_nonce TEXT;
ALTER TABLE skill_calls ADD COLUMN IF NOT EXISTS resume_spent_at TIMESTAMPTZ;
