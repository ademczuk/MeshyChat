-- 063_high_churn_parent_null_exemption.sql
--
-- TASK-36 prune enablement. The memory.prune skill (high_churn compaction)
-- deletes dead non-head version rows. parent_id is a self-FK
-- (shared_memory.parent_id REFERENCES shared_memory(id)) with the default
-- NO ACTION / RESTRICT delete behavior (confdeltype='a' on the live DB), so a
-- row that is still referenced by a RETAINED row (the kept head, or an older
-- kept version) cannot be deleted until that backward link is cleared. The
-- prune therefore UPDATEs the referencing rows' parent_id to NULL first, then
-- DELETEs the dead rows in the SAME transaction.
--
-- That parent_id non-null -> NULL UPDATE is the only mutation the existing
-- immutability trigger does not already permit. This migration relaxes the
-- trigger by EXACTLY that one move, and ONLY for namespaces whose
-- retention_policy is 'high_churn'. keep_all and ephemeral (and any future
-- policy value) stay fully immutable: the entire prior all-column check, the
-- is_head FALSE->TRUE resurrection ban, and the is_head TRUE->FALSE head
-- retirement allowance are preserved verbatim. The parent_id FK is NOT altered
-- (still RESTRICT); the prune nulls the link explicitly.
--
-- The exemption is intentionally narrow:
--   * policy must be 'high_churn' (looked up from shared_memory_namespace).
--   * the changed column must be parent_id, and ONLY parent_id.
--   * the change must be non-NULL -> NULL (link-severing only; you can never
--     re-point a parent_id or set one from NULL to a value via this path).
-- Anything else - parent_id NULL->value, value->different value, any other
-- content/identity column, or an is_head resurrection - still raises.
--
-- SECURITY INVARIANT (must_fix #1, adversarial immutability-bypass lens): the
-- whole safety of the relaxation rests on the premise that a keep_all / audit
-- namespace can never be RE-MARKED 'high_churn' after the fact. Today no
-- runtime path mutates retention_policy (mig 061 is a one-time backfill; the
-- daemon's namespace.claim is bootstrap-gated and idempotent ON CONFLICT DO
-- NOTHING, so a claim never overwrites a policy). But unlike
-- dashboard_users.plane (frozen by a dedicated trigger in mig 040),
-- retention_policy had NO database-level immutability guard - 'immutable post
-- claim' was an emergent property of the current handler set, not an enforced
-- invariant. A future namespace.set_retention admin skill, a daemon bug, or a
-- hand-run UPDATE by the superuser daemon (which bypasses RLS) could flip
-- shared/wiki -> high_churn, and THIS relaxed trigger would then honor the new
-- policy and let a signed, non-repudiation chain become prune-mutable.
-- Therefore the relaxation and the freeze ship in the SAME migration: the
-- companion trigger below makes retention_policy immutable, so the premise the
-- exemption depends on is ENFORCED, not assumed.

-- ---------------------------------------------------------------------------
-- 1. Freeze retention_policy (closes the latent policy-flip hole the
--    relaxation would otherwise make catastrophic). Mirrors mig 040's
--    dashboard_users_freeze_plane_group: reject ONLY a retention_policy change,
--    so every other legitimate UPDATE on shared_memory_namespace (owner change
--    within trust group, claimant_plane recompute in ns_dev_only_trigger) is
--    unaffected. A controlled policy-change path, if ever required, must be a
--    separate audited SECURITY DEFINER function, never a raw UPDATE - exactly
--    the posture mig 040 set for plane.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION shared_memory_namespace_freeze_retention_policy()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.retention_policy IS DISTINCT FROM OLD.retention_policy THEN
        RAISE EXCEPTION
            'shared_memory_namespace.retention_policy is immutable (was %, attempted %)',
            OLD.retention_policy, NEW.retention_policy
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Idempotent: a future meshy-db rebuild re-embeds + re-applies this migration, so
-- drop any prior binding before re-creating (CREATE TRIGGER is not OR REPLACE).
DROP TRIGGER IF EXISTS shared_memory_namespace_freeze_retention_policy_trg ON shared_memory_namespace;
CREATE TRIGGER shared_memory_namespace_freeze_retention_policy_trg
    BEFORE UPDATE ON shared_memory_namespace
    FOR EACH ROW
    EXECUTE FUNCTION shared_memory_namespace_freeze_retention_policy();

-- ---------------------------------------------------------------------------
-- 2. Relax the shared_memory immutability trigger by exactly the high_churn
--    parent_id-unlink move (and nothing else).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION shared_memory_enforce_immutability() RETURNS TRIGGER AS $$
DECLARE
    v_policy text;
    v_high_churn_parent_unlink boolean := FALSE;
BEGIN
    -- TASK-36: the policy lookup is only meaningful for the one mutation the
    -- exemption can ever permit - parent_id going non-NULL -> NULL. Short-circuit
    -- the namespace SELECT for every other UPDATE (the dominant one being the
    -- is_head TRUE->FALSE head retirement on every write), so the hot write path
    -- pays no extra table read (open-question #2 / trigger-correctness finding).
    IF OLD.parent_id IS NOT NULL AND NEW.parent_id IS NULL THEN
        -- namespace is itself immutable (enforced in the non-exempt block below),
        -- so OLD.namespace and NEW.namespace are identical here. Default to
        -- 'keep_all' (fully immutable) if the namespace row is somehow absent -
        -- never widen the exemption on a miss.
        SELECT retention_policy INTO v_policy
        FROM shared_memory_namespace
        WHERE namespace = OLD.namespace;
        IF v_policy IS NULL THEN
            v_policy := 'keep_all';
        END IF;

        -- The ONE new allowance: in a high_churn namespace, severing a parent_id
        -- link (non-NULL -> NULL) so a referenced dead version can be pruned. This
        -- is true ONLY when parent_id actually went non-NULL -> NULL AND no other
        -- content/identity column (and not is_head) changed. Everything else keeps
        -- the prior behavior verbatim.
        --
        -- DRIFT NOTE: the literal 'high_churn' must stay in lock-step with
        -- tools/memory/prune_retention.py RETENTION_HIGH_CHURN and
        -- tools/memory/daemon/memory_handlers.py RETENTION_HIGH_CHURN. The
        -- namespace CHECK constraint (mig 061) is the single source of truth for
        -- the policy vocabulary; if it grows a new prunable policy, this literal
        -- and both Python constants must change together.
        IF v_policy = 'high_churn'
            AND NEW.id                  IS NOT DISTINCT FROM OLD.id
            AND NEW.namespace           IS NOT DISTINCT FROM OLD.namespace
            AND NEW.key                 IS NOT DISTINCT FROM OLD.key
            AND NEW.version             IS NOT DISTINCT FROM OLD.version
            AND NEW.is_head             IS NOT DISTINCT FROM OLD.is_head
            AND NEW.value               IS NOT DISTINCT FROM OLD.value
            AND NEW.status              IS NOT DISTINCT FROM OLD.status
            AND NEW.pre_discipline      IS NOT DISTINCT FROM OLD.pre_discipline
            AND NEW.claimed_status      IS NOT DISTINCT FROM OLD.claimed_status
            AND NEW.tags                IS NOT DISTINCT FROM OLD.tags
            AND NEW.author_label        IS NOT DISTINCT FROM OLD.author_label
            AND NEW.embed_policy        IS NOT DISTINCT FROM OLD.embed_policy
            AND NEW.provenance_jsonb    IS NOT DISTINCT FROM OLD.provenance_jsonb
            AND NEW.payload_jcs_sha256  IS NOT DISTINCT FROM OLD.payload_jcs_sha256
            AND NEW.created_by_peer_id  IS NOT DISTINCT FROM OLD.created_by_peer_id
            AND NEW.created_by_owner_id IS NOT DISTINCT FROM OLD.created_by_owner_id
            AND NEW.created_at          IS NOT DISTINCT FROM OLD.created_at
        THEN
            v_high_churn_parent_unlink := TRUE;
        END IF;
    END IF;

    -- Every content/identity column is immutable. (provenance_call_id is a
    -- generated column and cannot be assigned directly; it follows
    -- provenance_jsonb, which is checked here.) PRESERVED VERBATIM from mig 028;
    -- only skipped when the narrow high_churn parent_id-unlink exemption applies.
    IF NOT v_high_churn_parent_unlink THEN
        IF NEW.id                  IS DISTINCT FROM OLD.id
            OR NEW.namespace           IS DISTINCT FROM OLD.namespace
            OR NEW.key                 IS DISTINCT FROM OLD.key
            OR NEW.version             IS DISTINCT FROM OLD.version
            OR NEW.parent_id           IS DISTINCT FROM OLD.parent_id
            OR NEW.value               IS DISTINCT FROM OLD.value
            OR NEW.status              IS DISTINCT FROM OLD.status
            OR NEW.pre_discipline      IS DISTINCT FROM OLD.pre_discipline
            OR NEW.claimed_status      IS DISTINCT FROM OLD.claimed_status
            OR NEW.tags                IS DISTINCT FROM OLD.tags
            OR NEW.author_label        IS DISTINCT FROM OLD.author_label
            OR NEW.embed_policy        IS DISTINCT FROM OLD.embed_policy
            OR NEW.provenance_jsonb    IS DISTINCT FROM OLD.provenance_jsonb
            OR NEW.payload_jcs_sha256  IS DISTINCT FROM OLD.payload_jcs_sha256
            OR NEW.created_by_peer_id  IS DISTINCT FROM OLD.created_by_peer_id
            OR NEW.created_by_owner_id IS DISTINCT FROM OLD.created_by_owner_id
            OR NEW.created_at          IS DISTINCT FROM OLD.created_at
        THEN
            RAISE EXCEPTION 'shared_memory rows are immutable; only head retirement (is_head TRUE->FALSE) is permitted';
        END IF;
    END IF;

    -- The only legal mutation is retiring a head. A retired head must not be
    -- resurrected. PRESERVED VERBATIM from mig 028 - always enforced (under the
    -- exemption is_head is unchanged, so this never trips for a prune-unlink).
    IF OLD.is_head = FALSE AND NEW.is_head = TRUE THEN
        RAISE EXCEPTION 'shared_memory head cannot be resurrected (is_head FALSE->TRUE forbidden)';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger wiring is unchanged (shared_memory_immutability BEFORE UPDATE ... still
-- points at this function); CREATE OR REPLACE FUNCTION keeps the existing trigger
-- bound, so no DROP/CREATE TRIGGER is needed.
