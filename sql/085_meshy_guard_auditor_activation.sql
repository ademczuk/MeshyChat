-- TASK-167 ACTIVATION: make the node-misbehavior auditor live (background sweep).
--
-- All statements are idempotent (CREATE ... IF NOT EXISTS / ADD COLUMN IF NOT
-- EXISTS) so an out-of-band apply against the live DB cannot crash-loop boot,
-- matching the 067-084 idempotent siblings.

-- 1. scanned_through_frame_id: the max allow-frame id a scan covered.
--    invocation_frames.chain_id is the long-lived ROOT GRANT id (not a terminal
--    call-chain), so a grant accrues frames over its whole life. This watermark is
--    what lets the sweep RE-AUDIT a chain once it gains allow-frames beyond the
--    last scan, instead of scanning a grant once and excluding it forever. Empty
--    table at the cut, so NOT NULL DEFAULT 0 applies cleanly.
ALTER TABLE meshy_guard_scan_results
    ADD COLUMN IF NOT EXISTS scanned_through_frame_id BIGINT NOT NULL DEFAULT 0;

-- 2. UNIQUE(chain_id, scanned_through_frame_id): one scan row per (grant, watermark).
--    ON CONFLICT (chain_id, scanned_through_frame_id) DO NOTHING dedups two
--    concurrent sweep ticks (or a second replica) at the SAME watermark, while a
--    re-scan at a HIGHER watermark still lands a new row. The 084 chain_id index is
--    non-unique and insert_scan_result was a plain INSERT, so without this guard a
--    TOCTOU on the selection could double-write the same (chain, watermark). (The
--    non-unique 084 index is left in place to keep this migration purely additive.)
CREATE UNIQUE INDEX IF NOT EXISTS meshy_guard_scan_results_chain_wm_uniq
    ON meshy_guard_scan_results (chain_id, scanned_through_frame_id);

-- 3. (created_at, chain_id) covering index on invocation_frames: the sweep's
--    chain-selection bounds candidates by a created_at lookback window and
--    age-gates on MAX(created_at) per chain. invocation_frames_chain_idx
--    (chain_id, id DESC) does NOT include created_at, so without this index PG
--    would full-scan the unbounded append-only table on every tick. This makes the
--    lookback range scan sargable.
CREATE INDEX IF NOT EXISTS invocation_frames_created_chain_idx
    ON invocation_frames (created_at, chain_id);

-- 4. alignment JSONB on scan_results: persist the LLM judge's
--    observation/thought/conclusion so a human reviewing a judge-flagged chain in
--    the flip-review queue sees the rationale, not just the derived finding.
--    Nullable: the taint-only stage (judge dark) leaves it NULL.
ALTER TABLE meshy_guard_scan_results
    ADD COLUMN IF NOT EXISTS alignment JSONB;
