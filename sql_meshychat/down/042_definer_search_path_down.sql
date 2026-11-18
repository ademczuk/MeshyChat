-- DOWN for sql_meshychat/042 (divergence 096). `sqlmigrate.py --rollback-to meshychat:41` runs it (the rollback gate,
-- apps/core/rollback.py), newest first.
--
-- WHAT COMES BACK: each of the eight definer functions exactly as it was before 042: its path `pg_catalog, public`, with
-- pg_temp unnamed (so searched first), and for the first seven their relations unqualified. Those seven are copied from
-- their defining files verbatim (tests/test_definer_search_path.py pins each copy against that file); CREATE OR REPLACE
-- keeps the oid, owner, ACL, comment and triggers. file_referencing_calls gets 041's path back by ALTER FUNCTION. Nothing
-- is lost: 042 changes no table and no row.

SET LOCAL lock_timeout = '5s';

-- sql/039_meshy_memory_tenant_rls.sql:20-31, verbatim.
CREATE OR REPLACE FUNCTION meshy_same_tenant_or_devgroup(caller uuid, target uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT caller = target
        OR EXISTS (
            SELECT 1
            FROM dashboard_users c, dashboard_users t
            WHERE c.id = caller AND t.id = target
              AND c.plane = 'dev' AND t.plane = 'dev'
              AND c.trust_group_id IS NOT NULL
              AND c.trust_group_id = t.trust_group_id
        );
$$;

-- sql_meshychat/012_write_policies_bound_and_frame_retention.sql:106-140, verbatim.
CREATE OR REPLACE FUNCTION prune_invocation_frames(
    retain_days INTEGER,
    batch_size  INTEGER
) RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    deleted BIGINT;
BEGIN
    IF retain_days IS NULL OR retain_days < 1 THEN
        RAISE EXCEPTION 'prune_invocation_frames: retain_days must be >= 1, got %', retain_days
            USING ERRCODE = 'check_violation';
    END IF;
    IF batch_size IS NULL OR batch_size < 1 OR batch_size > 50000 THEN
        RAISE EXCEPTION 'prune_invocation_frames: batch_size must be in [1, 50000], got %', batch_size
            USING ERRCODE = 'check_violation';
    END IF;

    -- The frame's own identity (id) under an age bound. Never chain_id, call_id or any other
    -- column that names a live object: a chained frame is never selected here.
    WITH victims AS (
        SELECT id FROM invocation_frames
        WHERE chain_id IS NULL
          AND created_at < NOW() - make_interval(days => retain_days)
        ORDER BY id
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM invocation_frames WHERE id IN (SELECT id FROM victims);
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END;
$$;

-- sql_meshychat/026_dead_root_grant_frame_retention.sql:116-265, verbatim.
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

-- sql_meshychat/026_dead_root_grant_frame_retention.sql:269-299, verbatim.
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

-- sql_meshychat/027_shared_memory_redaction.sql:261-273, verbatim.
CREATE OR REPLACE FUNCTION meshy_mesh_commit_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(1835233133, 1);
    INSERT INTO mesh_commit_order (memory_id, kind)
    SELECT NEW.id, 'write' WHERE EXISTS (SELECT 1 FROM shared_memory WHERE id = NEW.id)
    ON CONFLICT (memory_id, kind) DO NOTHING;
    RETURN NULL;
END;
$$;

-- sql_meshychat/027_shared_memory_redaction.sql:276-289, verbatim.
CREATE OR REPLACE FUNCTION meshy_mesh_redaction_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(1835233133, 1);
    INSERT INTO mesh_commit_order (memory_id, kind)
    SELECT NEW.memory_id, 'redaction'
     WHERE EXISTS (SELECT 1 FROM shared_memory WHERE id = NEW.memory_id)
    ON CONFLICT (memory_id, kind) DO NOTHING;
    RETURN NULL;
END;
$$;

-- sql_meshychat/029_mesh_message_edit.sql:132-145, verbatim.
CREATE OR REPLACE FUNCTION meshy_mesh_edit_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(1835233133, 1);
    INSERT INTO mesh_commit_order (memory_id, kind)
    SELECT NEW.memory_id, 'edit'
     WHERE EXISTS (SELECT 1 FROM shared_memory WHERE id = NEW.memory_id)
    ON CONFLICT (memory_id, kind) DO NOTHING;
    RETURN NULL;
END;
$$;

-- sql_meshychat/041's path.
ALTER FUNCTION public.file_referencing_calls(uuid) SET search_path = pg_catalog, public;

DELETE FROM meshychat_migrations WHERE version = 42;
