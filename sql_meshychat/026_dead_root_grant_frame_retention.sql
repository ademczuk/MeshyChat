-- MeshyChat divergence 052 (STATUS D08; operator decision D-2026-09-27-20, operator note 61):
-- invocation-frame retention by DEAD ROOT GRANT, with the anchor it retires kept as evidence.
--
-- NUMBERING. Written as 025 on fin/d08-retention, when origin/main (c4d2125) held 001 to 024;
-- renumbered 026 at integration into integ/r20-maint, where B02 A2 holds 025. Renumber freely
-- at integration; `sqlmigrate.discover` refuses a duplicate version, not a gap.
--
-- WHAT WAS THERE. sql/111's chained pass deleted a chain whole once its NEWEST FRAME was 90 days
-- old. `chain_id` is the ROOT GRANT id (sql/060:24-27), so an idle, never-expiring grant lost its
-- whole history while its signed anchor survived (ASDLC incidents 2026-09-21-retention-orphans-
-- chain-anchors.md and -retention-erased-live-grant-audit-history.md: 1,103 frames of three
-- live grants). sql_meshychat/012 cut the function down to standalone frames and revoked it from
-- meshy_app, and nothing calls it, so on this server NOTHING prunes a frame (divergence 029,
-- "Chained frames now have no retention path at all").
--
-- THE RULE (decision 61): a chain is deleted only after its root grant is revoked or expired,
-- plus a window, with an honest disposition of its anchor. Built here as:
--   * DEAD: `LEAST(revoked_at, expires_at)`, the instant the grant stopped satisfying the
--     liveness predicate `revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())`
--     (apps/grants/repository.py:42), is older than the policy's window. A chain whose root
--     grant row does not exist is NEVER eligible: an absent grant is unknown, not dead.
--   * QUIET: the chain's newest frame is older than the window too.
--   * ANCHORED AND CURRENT: the chain has a signed anchor (invocation_chain_heads, sql/071)
--     whose head is the chain's newest frame, with the anchored hash. A chain the anchor job
--     has not caught up with waits; an anchor whose head is missing (the six known orphans,
--     decision 53) is never touched, so retention never acts on a chain the verifier calls
--     truncated. The caller (apps/calls/frame_retention.py) also verifies the anchor's
--     signature first and passes the head and count it verified; a changed anchor is refused.
--   * Every rule above is re-checked HERE, in the SECURITY DEFINER function, not trusted from
--     the caller: meshy_app may name any chain, and only an eligible one is touched.
--
-- THE ANCHOR'S DISPOSITION. In the transaction that takes a chain, its anchor row is COPIED
-- whole (head id, head hash, count, signature, kid) into `invocation_chain_retention` and
-- DELETED from invocation_chain_heads. The copy still verifies under the server key, so the
-- ledger row states "this chain reached N frames up to head H (signed), and retention deleted
-- them because its root grant was dead since D". The chain-head job never sees an anchor whose
-- head retention removed (so it never reports TRUNCATED for one), and while a chain is being
-- pruned (status 'pruning') it anchors nothing for it (apps/calls/chain_head.py, RETIRING). A
-- ledger row cannot be written or changed by meshy_app: only these functions write it.
--
-- NEWEST FIRST, IN BOUNDED BATCHES. A long chain takes several calls (the job's statement
-- timeout, docs/V1-DESIGN-RULES.md B5), and between two of them what remains must still verify:
-- deleting the newest frames first leaves a prefix that links to its root, which the frame
-- audit (D09) reads as intact; deleting the oldest first would leave every remaining frame
-- pointing at a missing root, a false tamper finding per frame. Each batch runs under the
-- chain's writer lock (upstream's `advisory_key`, invocation_frames.rs:262-268: the first 8
-- bytes of the uuid as a big-endian i64), TRIED, never waited on: a chain being written is
-- skipped this tick. A frame past the retired head (a writer that added one) or a root grant
-- that is live again STOPS the chain: what remains is a linked prefix the chain-head job then
-- anchors afresh, and the ledger says why it stopped.
--
-- STANDALONE FRAMES (chain_id IS NULL) belong to no chain, no grant and no anchor, so nothing
-- live is keyed by them. Their rule is separate and OFF by default: when the policy's
-- `standalone_retain_days` is set, a standalone frame older than that is deleted by its own id,
-- oldest first. 012's `prune_invocation_frames` stays revoked from meshy_app, because upstream's
-- janitor calls it by that name with an age it chooses.
--
-- THE POLICY IS THE OWNER'S. One row, `invocation_frame_retention_policy`, readable by meshy_app
-- and writable only by the owning role: the app role runs retention but cannot choose its
-- window. `chain_window_days` defaults to 90 (see divergence 052 for why 90 and not 30), and
-- NULL turns chain retention off, which sql/111 never had (the 2026-09-23 mitigation had to be a
-- REVOKE for that reason). `standalone_retain_days` defaults to NULL: no operator decision
-- covers standalone frames yet.
--
-- THE LIMIT, stated: meshy_app may UPDATE skill_grants (the grants API does), so the app role can
-- make a grant look dead. The ledger then records the grant's state as the function saw it, and
-- the job reports a ledger row whose grant is live again (frame_retention.verify_record), so a
-- deletion made that way is visible, not silent.
--
-- Idempotent (tools/ci/MIGRATIONS.md 078+ rule), because the down script KEEPS both tables.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS invocation_frame_retention_policy (
    singleton              BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    chain_window_days      INTEGER DEFAULT 90
                           CHECK (chain_window_days IS NULL OR chain_window_days >= 1),
    standalone_retain_days INTEGER
                           CHECK (standalone_retain_days IS NULL OR standalone_retain_days >= 1),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO invocation_frame_retention_policy (singleton) VALUES (true)
    ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS invocation_chain_retention (
    chain_id              UUID PRIMARY KEY,
    status                TEXT NOT NULL CHECK (status IN ('pruning', 'complete', 'stopped')),
    -- The root grant as the function read it when it took the chain.
    grant_revoked_at      TIMESTAMPTZ,
    grant_expires_at      TIMESTAMPTZ,
    dead_since            TIMESTAMPTZ NOT NULL,
    window_days           INTEGER NOT NULL CHECK (window_days >= 1),
    newest_frame_at       TIMESTAMPTZ NOT NULL,
    -- The retired anchor, byte for byte. anchor_head_frame_id is also the bound: only frames
    -- of this chain at or below it are ever deleted under this row.
    anchor_head_frame_id  BIGINT NOT NULL,
    anchor_head_hash      TEXT NOT NULL,
    anchor_frame_count    BIGINT NOT NULL,
    anchor_signature      BYTEA NOT NULL,
    anchor_signing_kid    TEXT NOT NULL,
    anchor_updated_at     TIMESTAMPTZ,
    frames_deleted        BIGINT NOT NULL DEFAULT 0 CHECK (frames_deleted >= 0),
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ,
    stop_reason           TEXT,
    CHECK ((status = 'pruning') = (finished_at IS NULL)),
    CHECK ((status = 'stopped') = (stop_reason IS NOT NULL))
);

-- sql/010's default privileges give meshy_app full CRUD on a new table; both are read-only to it.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON invocation_frame_retention_policy FROM meshy_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON invocation_chain_retention FROM meshy_app;
GRANT SELECT ON invocation_frame_retention_policy, invocation_chain_retention TO meshy_app;

-- One call: one chain, at most p_batch frames, in the caller's transaction. Returns what it did.
CREATE OR REPLACE FUNCTION retire_invocation_chain(
    p_chain_id     UUID,
    p_expect_head  BIGINT,
    p_expect_count BIGINT,
    p_batch        INTEGER
) RETURNS TABLE (outcome TEXT, reason TEXT, frames_deleted BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_window  INTEGER;
    v_grant   RECORD;
    v_anchor  RECORD;
    v_newest  RECORD;
    v_ledger  RECORD;
    v_dead    TIMESTAMPTZ;
    v_bound   BIGINT;
    v_n       BIGINT;
    v_have    BOOLEAN;
    v_started BOOLEAN := false;
BEGIN
    IF p_chain_id IS NULL THEN
        RAISE EXCEPTION 'retire_invocation_chain: chain_id must not be NULL'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_batch IS NULL OR p_batch < 1 OR p_batch > 50000 THEN
        RAISE EXCEPTION 'retire_invocation_chain: batch must be in [1, 50000], got %', p_batch
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT chain_window_days INTO v_window FROM invocation_frame_retention_policy;
    IF v_window IS NULL THEN
        RETURN QUERY SELECT 'off'::text, 'chain_window_days is NULL'::text, 0::bigint;
        RETURN;
    END IF;

    -- The writer's per-chain lock (invocation_frames.rs:262-268, :298), tried, not waited on.
    IF NOT pg_try_advisory_xact_lock(
            ('x' || left(replace(p_chain_id::text, '-', ''), 16))::bit(64)::bigint) THEN
        RETURN QUERY SELECT 'busy'::text, 'the chain''s writer lock is held'::text, 0::bigint;
        RETURN;
    END IF;

    -- The root grant, held against a concurrent revival (update_grant) until this commits.
    SELECT revoked_at, expires_at INTO v_grant
      FROM skill_grants WHERE id = p_chain_id FOR SHARE;
    v_have := FOUND;
    v_dead := CASE WHEN v_have THEN LEAST(v_grant.revoked_at, v_grant.expires_at) END;

    SELECT * INTO v_ledger FROM invocation_chain_retention
     WHERE chain_id = p_chain_id FOR UPDATE;

    IF NOT FOUND THEN
        -- A new retirement. Every rule is checked here, whatever the caller checked.
        IF NOT v_have THEN
            RETURN QUERY SELECT 'not_eligible'::text, 'no_root_grant'::text, 0::bigint;
            RETURN;
        END IF;
        IF v_dead IS NULL OR v_dead > now() THEN
            RETURN QUERY SELECT 'not_eligible'::text, 'grant_live'::text, 0::bigint;
            RETURN;
        END IF;
        IF v_dead >= now() - make_interval(days => v_window) THEN
            RETURN QUERY SELECT 'not_eligible'::text, 'grant_dead_within_window'::text, 0::bigint;
            RETURN;
        END IF;
        SELECT * INTO v_anchor FROM invocation_chain_heads
         WHERE chain_id = p_chain_id FOR UPDATE;
        IF NOT FOUND THEN
            RETURN QUERY SELECT 'not_eligible'::text, 'no_anchor'::text, 0::bigint;
            RETURN;
        END IF;
        IF v_anchor.head_frame_id IS DISTINCT FROM p_expect_head
           OR v_anchor.frame_count IS DISTINCT FROM p_expect_count THEN
            RETURN QUERY SELECT 'not_eligible'::text, 'anchor_changed'::text, 0::bigint;
            RETURN;
        END IF;
        SELECT id, frame_hash, created_at INTO v_newest FROM invocation_frames
         WHERE chain_id = p_chain_id ORDER BY id DESC LIMIT 1;
        IF NOT FOUND OR v_newest.id <> v_anchor.head_frame_id
           OR v_newest.frame_hash <> v_anchor.head_hash THEN
            RETURN QUERY SELECT 'not_eligible'::text, 'anchor_not_current'::text, 0::bigint;
            RETURN;
        END IF;
        IF v_newest.created_at >= now() - make_interval(days => v_window) THEN
            RETURN QUERY SELECT 'not_eligible'::text, 'recent_frame'::text, 0::bigint;
            RETURN;
        END IF;

        INSERT INTO invocation_chain_retention (
            chain_id, status, grant_revoked_at, grant_expires_at, dead_since, window_days,
            newest_frame_at, anchor_head_frame_id, anchor_head_hash, anchor_frame_count,
            anchor_signature, anchor_signing_kid, anchor_updated_at)
        VALUES (
            p_chain_id, 'pruning', v_grant.revoked_at, v_grant.expires_at, v_dead, v_window,
            v_newest.created_at, v_anchor.head_frame_id, v_anchor.head_hash,
            v_anchor.frame_count, v_anchor.head_signature, v_anchor.signing_kid,
            v_anchor.updated_at);
        DELETE FROM invocation_chain_heads WHERE chain_id = p_chain_id;
        v_bound := v_anchor.head_frame_id;
        v_started := true;
    ELSIF v_ledger.status <> 'pruning' THEN
        RETURN QUERY SELECT 'finished'::text, v_ledger.status, 0::bigint;
        RETURN;
    ELSE
        v_bound := v_ledger.anchor_head_frame_id;
        -- A grant revived mid-prune (an expiry moved forward) keeps what is left. A grant row
        -- deleted mid-prune (ON DELETE CASCADE from its owner or peer) is not live: go on.
        IF v_have AND (v_dead IS NULL OR v_dead > now()) THEN
            UPDATE invocation_chain_retention
               SET status = 'stopped', finished_at = now(), stop_reason = 'grant_live_again'
             WHERE chain_id = p_chain_id;
            RETURN QUERY SELECT 'stopped'::text, 'grant_live_again'::text, 0::bigint;
            RETURN;
        END IF;
    END IF;

    -- A frame past the retired head: a writer added one. What is left stays, linked.
    IF EXISTS (SELECT 1 FROM invocation_frames
                WHERE chain_id = p_chain_id AND id > v_bound) THEN
        UPDATE invocation_chain_retention
           SET status = 'stopped', finished_at = now(), stop_reason = 'frame_past_retired_head'
         WHERE chain_id = p_chain_id;
        RETURN QUERY SELECT 'stopped'::text, 'frame_past_retired_head'::text, 0::bigint;
        RETURN;
    END IF;

    -- Newest first, by the chain index (chain_id, id DESC), at most p_batch frames.
    DELETE FROM invocation_frames WHERE id IN (
        SELECT id FROM invocation_frames
         WHERE chain_id = p_chain_id AND id <= v_bound
         ORDER BY id DESC LIMIT p_batch);
    GET DIAGNOSTICS v_n = ROW_COUNT;

    UPDATE invocation_chain_retention
       SET frames_deleted = invocation_chain_retention.frames_deleted + v_n
     WHERE chain_id = p_chain_id;

    IF NOT EXISTS (SELECT 1 FROM invocation_frames
                    WHERE chain_id = p_chain_id AND id <= v_bound) THEN
        UPDATE invocation_chain_retention SET status = 'complete', finished_at = now()
         WHERE chain_id = p_chain_id;
        RETURN QUERY SELECT 'complete'::text, NULL::text, v_n;
        RETURN;
    END IF;
    RETURN QUERY SELECT (CASE WHEN v_started THEN 'started' ELSE 'pruning' END)::text,
                        NULL::text, v_n;
END;
$$;

-- Standalone frames, by their own id and age, oldest first on sql/085's (created_at, chain_id)
-- index; the range stops at the LIMIT. NULL when the policy leaves standalone retention off.
CREATE OR REPLACE FUNCTION prune_standalone_invocation_frames(p_batch INTEGER)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_days INTEGER;
    v_n    BIGINT;
BEGIN
    IF p_batch IS NULL OR p_batch < 1 OR p_batch > 50000 THEN
        RAISE EXCEPTION 'prune_standalone_invocation_frames: batch must be in [1, 50000], got %',
            p_batch USING ERRCODE = 'check_violation';
    END IF;
    SELECT standalone_retain_days INTO v_days FROM invocation_frame_retention_policy;
    IF v_days IS NULL THEN
        RETURN NULL;
    END IF;
    WITH victims AS (
        SELECT id FROM invocation_frames
         WHERE created_at < now() - make_interval(days => v_days)
           AND chain_id IS NULL
         ORDER BY created_at
         LIMIT p_batch
         FOR UPDATE SKIP LOCKED
    )
    DELETE FROM invocation_frames WHERE id IN (SELECT id FROM victims);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END;
$$;

-- CREATE FUNCTION grants EXECUTE to PUBLIC; revoke that, then grant the app role narrowly
-- (sql/111:115-121's reasoning).
REVOKE EXECUTE ON FUNCTION retire_invocation_chain(UUID, BIGINT, BIGINT, INTEGER) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION prune_standalone_invocation_frames(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION retire_invocation_chain(UUID, BIGINT, BIGINT, INTEGER) TO meshy_app;
GRANT EXECUTE ON FUNCTION prune_standalone_invocation_frames(INTEGER) TO meshy_app;
