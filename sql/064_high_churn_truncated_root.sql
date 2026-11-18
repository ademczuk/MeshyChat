-- 064_high_churn_truncated_root.sql
--
-- TASK-36 (continues mig 063). The memory.prune compaction severs a kept row's
-- parent_id (non-NULL -> NULL) so a referenced dead version can be deleted; mig
-- 063 relaxed the immutability trigger to permit that one UPDATE move for
-- high_churn namespaces. But shared_memory_root_parent_chk enforces
--   (version = 1 AND parent_id IS NULL) OR (version > 1 AND parent_id IS NOT NULL)
-- so a version>1 row with a NULL parent (the compacted chain's "truncated root")
-- is rejected. A CHECK cannot reference another table's retention_policy, so it
-- cannot be made high_churn-conditional. This migration replaces the static CHECK
-- with a policy-aware trigger.
--
-- CRITICAL (adversarial review, must_fix): the relaxation is scoped to the UPDATE
-- path ONLY. The prune NEVER inserts a version>1/parent-NULL row - it only UPDATEs
-- (severs) an existing parent. The daemon write path always inserts version=1 with
-- a NULL parent or version>1 with the head's id. So NO legitimate path ever INSERTs
-- a truncated root. Allowing it on INSERT (an earlier draft did) would be a pure
-- provenance-forgery surface: a fabricated version=N head with no real ancestor
-- chain, an orphan mid-chain version, etc. Therefore:
--   * INSERT  -> the strict root-parent invariant is enforced for EVERY namespace
--                (identical to the old CHECK; high_churn included). No fabricated
--                truncated roots / orphans.
--   * UPDATE  -> a version>1 row may end up with a NULL parent_id (the truncated
--                root). The parent_id MUTATION itself is already gated by the
--                immutability trigger (mig 063: only a high_churn non-NULL->NULL
--                sever, or an is_head retirement with parent unchanged, is allowed),
--                so the only way to reach v>1/parent-NULL on UPDATE is the legitimate
--                high_churn prune sever. We still forbid a root (version 1) gaining a
--                parent on UPDATE as defense-in-depth.
-- The policy is therefore NOT even looked up here - mig 063 is the policy gate for
-- the parent_id change; this trigger only enforces the structural shape.
--
-- DEPLOY: DROP CONSTRAINT takes ACCESS EXCLUSIVE on the high-write shared_memory
-- table; apply this in a maintenance window with the memory daemon PAUSED (no
-- concurrent writes), per the 2026-06-17 lock-storm incident
-- (memory agentmeshy-substrate-ddl-incident). Idempotent (IF EXISTS / OR REPLACE).

CREATE OR REPLACE FUNCTION shared_memory_enforce_root_parent() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Strict root-parent invariant for EVERY namespace, identical to the old
        -- CHECK. No high_churn INSERT exemption (the prune never inserts a
        -- truncated root; allowing it would be a forgery surface).
        IF NOT ( (NEW.version = 1 AND NEW.parent_id IS NULL)
              OR (NEW.version > 1 AND NEW.parent_id IS NOT NULL) ) THEN
            RAISE EXCEPTION
                'shared_memory INSERT violates root-parent invariant (version=1 <=> NULL parent_id): namespace %, version %, parent %',
                NEW.namespace, NEW.version, NEW.parent_id USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        -- UPDATE. parent_id mutations are gated by shared_memory_enforce_immutability
        -- (mig 063): only a high_churn non-NULL->NULL sever or an is_head retirement
        -- (parent unchanged) is permitted, so a version>1 + NULL-parent state can only
        -- arise from the legitimate prune sever. Defense-in-depth: a root (version 1)
        -- may never gain a parent.
        IF NEW.version = 1 AND NEW.parent_id IS NOT NULL THEN
            RAISE EXCEPTION
                'shared_memory root (version 1) must have NULL parent_id: namespace %, parent %',
                NEW.namespace, NEW.parent_id USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

ALTER TABLE shared_memory DROP CONSTRAINT IF EXISTS shared_memory_root_parent_chk;

DROP TRIGGER IF EXISTS shared_memory_root_parent_trg ON shared_memory;
CREATE TRIGGER shared_memory_root_parent_trg
    BEFORE INSERT OR UPDATE ON shared_memory
    FOR EACH ROW
    EXECUTE FUNCTION shared_memory_enforce_root_parent();
