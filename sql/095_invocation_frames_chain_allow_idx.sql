-- no-transaction
-- 095: targeted partial index for the meshy-guard misbehavior auditor's per-chain
-- allow-frame scan (crates/meshy-guard/src/auditor.rs audit_chain /
-- audit_chain_with_judge):
--   SELECT id, skill_name FROM invocation_frames
--   WHERE chain_id = $1 AND decision = 'allow' ORDER BY id
--
-- invocation_frames_chain_idx (chain_id, id DESC), from migration 060, already
-- keeps this off a full-table scan (it services the chain_id equality and
-- supplies the id order via a backward scan), but `decision` is not part of
-- it, so a 'deny' frame sharing the chain_id still costs a heap fetch before
-- being filtered out. chain_id is a long-lived root grant (084/085), and the
-- auditor-sweep task (meshy-calls::auditor_sweep) re-runs this exact query for
-- up to 200 chains every 900s tick, so any residual per-row cost multiplies
-- across the sweep. A chain under adversarial probing can also accumulate a
-- disproportionate share of 'deny' frames (a denied call is cheap for a
-- caller to generate), which is exactly the amplification this index
-- removes: with (chain_id, id) WHERE decision = 'allow', the decision filter
-- becomes part of the index condition instead of a post-heap-fetch filter, so
-- deny frames are skipped entirely for this scan.
--
-- CONCURRENTLY (hence the leading -- no-transaction directive, matching 092):
-- invocation_frames is written on every authorize decision (allow AND deny),
-- so a plain in-transaction CREATE INDEX would take a write-blocking lock for
-- the whole build. IF NOT EXISTS keeps this idempotent for an out-of-band
-- apply.

CREATE INDEX CONCURRENTLY IF NOT EXISTS invocation_frames_chain_allow_idx
    ON invocation_frames (chain_id, id)
    WHERE decision = 'allow';
