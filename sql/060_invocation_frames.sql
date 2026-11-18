-- 060_invocation_frames.sql
--
-- TASK-10: per-invocation capability binding as a tamper-evident frame chain.
--
-- TASK-1 invocation binding needs the capability chain recorded per call frame,
-- and a DENIED call has no skill_calls row (authorize precedes the call insert),
-- so deny frames cannot be skill_calls columns. invocation_frames is an
-- append-only audit of EVERY authorize decision (ALLOW + DENY) so a denied
-- attempt cannot be erased (AC#2). Frames sharing a chain_id are linked by
-- previous_frame_hash into a hash chain (AC#3): frame_hash binds the frame's
-- fields plus the previous frame's hash, so altering or reordering any frame
-- breaks every downstream hash. The meshy-server is the single writer, serialized
-- per chain_id by a pg_advisory_xact_lock around read-last + insert, so two
-- concurrent invocations on a chain cannot both point at the same previous frame
-- (a fork the verifier could not disambiguate).
--
-- No foreign keys, by design (mirrors tenancy_denials 043): an audit trail must
-- survive owner/peer/grant deletion, and a deny frame is written before any
-- skill_calls row exists. skill_calls.grant_id is preserved unchanged (AC#4):
-- this table is additive, the richer per-invocation record.
CREATE TABLE invocation_frames (
    id                   BIGSERIAL PRIMARY KEY,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- The resolved capability chain: the ROOT grant id (the anchor a
    -- re-delegation walk climbs to; a direct grant is its own root). NULL when
    -- the decision resolved no grant (a same-tenant allow, or a deny with no
    -- resolved chain): such frames are standalone, with no chain to link.
    chain_id             UUID,
    -- The specific grant that authorized THIS frame (the leaf for a
    -- re-delegation, the grant itself for a direct grant). NULL for grant-free.
    capability_ref       UUID,
    -- Denormalized hash-pointer to the chain ROOT frame (the chain's genesis),
    -- so an offline verifier can bind any frame to its capability-chain origin
    -- without re-walking. Derived (= the root's frame_hash), so it is NOT part
    -- of frame_hash; a verifier recomputes it from the chain. NULL at a root /
    -- grant-free frame.
    parent_hash          TEXT,
    -- Delegation hops from the root grant to the authorizing grant (0 = direct).
    delegation_depth     INTEGER NOT NULL DEFAULT 0,
    -- The call's per-invocation envelope nonce, when the request carried one.
    invocation_nonce     TEXT,
    -- The frame_hash of the immediately-preceding frame sharing this chain_id,
    -- or NULL for the first frame of a chain. The sequential hash-pointer.
    previous_frame_hash  TEXT,
    -- sha256_hex(canonical(frame fields) || previous_frame_hash). Recomputable
    -- offline; altering any bound field or the linkage breaks it (AC#3).
    frame_hash           TEXT NOT NULL,
    -- 'allow' or 'deny'. Both are recorded so a denied attempt is durable (AC#2).
    decision             TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
    -- The denial reason for a deny frame (NULL for allow).
    deny_reason          TEXT,
    -- The authorized call id (set on allow; NULL on deny, which has no call row).
    call_id              TEXT,
    caller_owner_id      UUID NOT NULL,
    caller_peer_id       UUID NOT NULL,
    target_owner_id      UUID NOT NULL,
    target_peer_id       UUID NOT NULL,
    skill_name           TEXT NOT NULL
);

-- Find the previous frame for a chain (newest-first) under the advisory lock.
CREATE INDEX invocation_frames_chain_idx ON invocation_frames (chain_id, id DESC);
-- Audit lookups by call.
CREATE INDEX invocation_frames_call_idx ON invocation_frames (call_id);
