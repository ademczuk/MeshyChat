-- TASK-36: an EPHEMERAL (latest-only) namespace keeps no version history.
--
-- The shared_memory ledger is append-only: every memory.write retires the prior head
-- and inserts a new version, keeping every dead non-head version forever. That is the
-- right discipline for tracked findings, but heavyweight for high-churn HEARTBEAT state
-- (shared/model-health was 2943 rows for 15 live heads, chains to v266; agent-usage
-- v242) where each probe appends a version that is instantly retired and never read
-- again — unbounded growth at ~2192 writes/day.
--
-- A namespace claimed with ephemeral=TRUE is latest-only: each write REPLACES the head
-- (the daemon deletes the prior row and writes a single version-1 head), so an ephemeral
-- namespace holds exactly one row per key. Default FALSE: every existing namespace keeps
-- full history, unchanged. Set once at namespace.claim (immutable thereafter, like owner).
ALTER TABLE shared_memory_namespace
    ADD COLUMN IF NOT EXISTS ephemeral BOOLEAN NOT NULL DEFAULT FALSE;
