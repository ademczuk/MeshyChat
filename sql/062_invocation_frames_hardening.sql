-- 062_invocation_frames_hardening.sql
--
-- TASK-10 hardening, from the adversarial re-review (workflow wf_0dfcea97-226).
-- Migration 060 shipped the invocation_frames writer; this adds the DB-enforced
-- invariants the advisory-lock linearity should rest on, makes the table
-- append-only against the app role, and the module doc (invocation_frames.rs)
-- corrects the guarantee language to match what this actually delivers.
--
-- HONEST GUARANTEE STATEMENT (the 060 / module headers said "a denied attempt
-- cannot be erased", which over-claims): the chain is tamper-EVIDENT for in-place
-- MUTATION and REORDERING of RETAINED frames only. It is NOT, on its own:
--   * truncation-resistant -- frame_hash binds only the BACKWARD link
--     (previous_frame_hash), so deleting the newest N frames leaves rows
--     1..len-N still self-consistent. Deny frames (an attacker's own probing) are
--     the easiest tail to drop.
--   * non-repudiable / unforgeable -- the hash is UNKEYED sha256, so it proves
--     internal CONSISTENCY, not AUTHENTICITY; a writer with table access can mint
--     a fresh self-consistent chain.
-- The REVOKE below makes the table append-only against meshy_app (the production
-- app role keeps INSERT + SELECT, loses UPDATE + DELETE) as defense-in-depth, the
-- same posture as the 040 plane-immutability trigger. GENUINE non-repudiation
-- (keyed/signed frame heads under the server key + a monotonic head anchor so
-- truncation is detectable + a periodic verifier job) is a NAMED follow-on
-- (TASK: invocation-frame chain verifier), not shipped here.

-- Defense-in-depth behind the per-chain pg_advisory_xact_lock in record_frame:
-- enforce per-chain LINEARITY at the DB level so a fork is a hard error even if
-- the lock is ever removed/mis-keyed (and so the concurrency test is a real
-- RED-prove: both concurrent inserts can only BOTH succeed under serialization).
--
-- (1) No fork: within a chain, two frames may not share a predecessor.
CREATE UNIQUE INDEX invocation_frames_no_fork_idx
    ON invocation_frames (chain_id, previous_frame_hash)
    WHERE chain_id IS NOT NULL AND previous_frame_hash IS NOT NULL;
-- (2) One root: a chain has at most one genesis frame (the single prev-NULL row).
CREATE UNIQUE INDEX invocation_frames_one_root_idx
    ON invocation_frames (chain_id)
    WHERE chain_id IS NOT NULL AND previous_frame_hash IS NULL;

-- Column consistency: a deny_reason belongs only to a deny frame (an 'allow' with
-- a deny_reason is a miswrite that frame_hash alone would accept as valid). The
-- 052 length-CHECK is the precedent. call_id is intentionally left unconstrained
-- (currently NULL on deny by the writer, but the schema stays flexible for a
-- future "show the denied attempt for call_id X" correlation).
ALTER TABLE invocation_frames
    ADD CONSTRAINT invocation_frames_deny_reason_ck
    CHECK (decision = 'deny' OR deny_reason IS NULL);

-- Append-only against the app role: the writer INSERTs + reads frames; it must not
-- be able to rewrite or erase a recorded decision. 010 grants meshy_app full CRUD
-- (incl. on this later-created table via ALTER DEFAULT PRIVILEGES), so revoke the
-- mutating grants here. meshy_app is a GRANTee, not the table owner (migrations run
-- as the owner), so this is effective when meshy-server connects as meshy_app.
REVOKE UPDATE, DELETE ON invocation_frames FROM meshy_app;
