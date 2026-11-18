-- TASK-36 (Path A): retention policy for shared-memory namespaces.
--
-- Adds a 3-state retention_policy so the daemon (namespace.claim + memory.write
-- + the new memory.prune skill) can compact high-churn version chains while the
-- append-only ledger stays the default. keep_all is the safe default: an
-- unmarked namespace is NEVER pruned. The existing `ephemeral` boolean (mig 045)
-- still exists; this column supersedes it as the policy source the daemon reads,
-- and we backfill ephemeral=TRUE -> 'ephemeral' so write-time latest-only
-- behavior is preserved when the daemon switches to reading retention_policy.
ALTER TABLE shared_memory_namespace
    ADD COLUMN IF NOT EXISTS retention_policy text NOT NULL DEFAULT 'keep_all';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'shared_memory_namespace_retention_policy_chk'
    ) THEN
        ALTER TABLE shared_memory_namespace
            ADD CONSTRAINT shared_memory_namespace_retention_policy_chk
            CHECK (retention_policy IN ('keep_all', 'high_churn', 'ephemeral'));
    END IF;
END $$;

-- Preserve existing ephemeral namespaces (mig 045 boolean) under the new model.
UPDATE shared_memory_namespace
    SET retention_policy = 'ephemeral'
    WHERE ephemeral = TRUE AND retention_policy = 'keep_all';

-- Designate the known high-churn heartbeat namespaces (TASK-36 live state:
-- shared/model-health accumulated ~3900 dead non-head versions for 15 heads).
UPDATE shared_memory_namespace
    SET retention_policy = 'high_churn'
    WHERE namespace IN ('shared/model-health', 'shared/agent-usage')
      AND retention_policy = 'keep_all';
