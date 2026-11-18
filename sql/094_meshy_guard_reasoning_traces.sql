-- TASK-167 AC#1: node-submitted reasoning traces (the reasoning half of the
-- node-misbehavior / prompt-injection auditor).
--
-- A node submits its pre-execution reasoning (an ordered observation/thought per
-- planned step) bound to a chain_id, Ed25519-signed with its peer signing key.
-- The auditor then compares this submitted reasoning against the SIGNED tool-call
-- trace (invocation_frames) to detect a reasoning-vs-action MISMATCH: a rogue or
-- prompt-injected node whose fabricated clean reasoning does not match its actual
-- sink-hitting actions is itself the signal. The reasoning is self-submitted, so
-- a mismatch verdict is ADVISORY (routes to flip-review), never a block - the
-- trustworthy anchor stays the signed CallProof trace.
--
-- No foreign keys, by design (mirrors meshy_guard_scan_results / invocation_frames):
-- the reasoning is submitted pre-execution, before the downstream skill_calls row
-- exists, and the append-only audit trail must survive later peer/owner/grant
-- deletion. Idempotent CREATE TABLE IF NOT EXISTS so an out-of-band apply against
-- the live DB does not crash-loop boot (matches the 067-084 idempotent siblings).
--
-- signature + signing_kid are NOT NULL (unlike scan_results, which allows an
-- unsigned advisory result): a reasoning submission is only meaningful if it is
-- signed by the submitting peer, since the signature is what binds the reasoning
-- to that peer for the mismatch check.

CREATE TABLE IF NOT EXISTS meshy_guard_reasoning_traces (
    id                BIGSERIAL PRIMARY KEY,
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    chain_id          UUID NOT NULL,
    submitter_peer_id UUID NOT NULL,
    reasoning         JSONB NOT NULL,
    signature         BYTEA NOT NULL,
    signing_kid       TEXT NOT NULL
);

-- One reasoning submission per chain_id is the replay / overwrite guard: a node
-- must not be able to re-submit a cleaner reasoning after it has seen the audit
-- verdict. The repository insert uses ON CONFLICT DO NOTHING against this index,
-- so a second submission for the same chain returns no row and is rejected.
CREATE UNIQUE INDEX IF NOT EXISTS meshy_guard_reasoning_traces_chain_uniq
    ON meshy_guard_reasoning_traces (chain_id);
