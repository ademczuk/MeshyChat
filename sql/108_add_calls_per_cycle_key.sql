-- 108: add the billing capability key 'calls_per_cycle' alongside 'calls_per_day'.
--
-- #405. The name lied about the reset cadence. The cap is enforced per BILLING
-- CYCLE, never per day: resolve_cycle_start (crates/meshy-tier/src/repository.rs)
-- pins the cycle to the Stripe period start for active/past_due/trialing
-- subscriptions and to the first of the UTC calendar month otherwise, and the
-- usage counter rolls over at that boundary. Every consumer reading the key saw a
-- daily cap that does not exist, so the string itself is the bug.
--
-- ADDITIVE, NOT A RENAME, and that is a correction to this migration's first
-- version. It ran three plain UPDATEs, which fail two ways.
--
-- 1. ROLLBACK. tools/deploy/deploy-prod.sh restores the previous IMAGE on a failed
--    boot gate and leaves the database as migrated (deploy-prod.sh:178, and its own
--    comment at :94 says the cost of being wrong "grows the moment a migration lands
--    and rollback stops being free"). documents/operations/vps-compile-deploy-runbook.md
--    line 329 states the model: "Prefer rolling the binary back while leaving
--    forward-additive migrations in place." A rename is not forward-additive: the
--    restored binary asks for calls_per_day, every tier-cap row for it is gone, and
--    every reservation fails with UnknownCapability. So both keys ship, and a LATER
--    migration drops calls_per_day once a release carrying only the new key has held.
--
-- 2. COLLISION, and it aborts the whole startup migration rather than one statement.
--    capability is in ALL THREE primary keys: (tier, capability),
--    (owner_id, capability), (owner_id, capability, cycle_start). If any
--    calls_per_cycle row already exists beside its calls_per_day twin, the UPDATE
--    violates the key. That state is REACHABLE, not hypothetical: the admin override
--    handler's own docstring says "Capability strings are NOT validated against a
--    hardcoded list" (crates/meshy-api/src/routes/admin/billing.rs:239), so an
--    administrator can create the destination row before this ever runs.
--    REPRODUCED against the real schema before writing this: seeding one such pair
--    made the previous version abort with
--    'duplicate key value violates unique constraint "tier_capabilities_pkey"'.
--    Note WHICH table failed first. The review named owner_tier_overrides; the abort
--    came from tier_capabilities, because all three are exposed and not just the one.
--
-- PRECEDENCE ON COLLISION IS CHOSEN PER TABLE, since "resolve it explicitly" is the
-- whole point and the safe answer is not the same for a cap as for a counter.
--   caps (tier_capabilities, owner_tier_overrides): THE EXISTING DESTINATION WINS.
--     An operator who deliberately set calls_per_cycle should not have it silently
--     replaced by the legacy value.
--   usage (owner_usage): THE GREATER COUNTER WINS. Consumption must not be lost.
--     Taking the destination blindly, or the lower of the two, hands back quota the
--     owner has already spent.
--
-- IDEMPOTENT: re-running inserts nothing new for the caps, and re-running the usage
-- statement recomputes the same GREATEST. One caveat while both keys are live: a
-- rolled-back binary still incrementing calls_per_day means a later re-run can raise
-- the calls_per_cycle counter to match it. That is the intended direction, since the
-- alternative is forgetting spend, but it is why the drop migration should follow a
-- release rather than a deploy.
--
-- VISIBLE CONSEQUENCE, stated rather than left to be found: endpoints that enumerate
-- capabilities into a map, such as the admin billing summary
-- (crates/meshy-api/src/routes/admin/billing.rs:158 and :169), will list BOTH keys for
-- the duration of the compatibility window. That is cosmetic and it is the price of a
-- rollback-safe cutover.
INSERT INTO tier_capabilities (tier, capability, value)
SELECT tier, 'calls_per_cycle', value
  FROM tier_capabilities
 WHERE capability = 'calls_per_day'
ON CONFLICT (tier, capability) DO NOTHING;

INSERT INTO owner_tier_overrides (owner_id, capability, value, note)
SELECT owner_id, 'calls_per_cycle', value, note
  FROM owner_tier_overrides
 WHERE capability = 'calls_per_day'
ON CONFLICT (owner_id, capability) DO NOTHING;

INSERT INTO owner_usage (owner_id, capability, cycle_start, counter)
SELECT owner_id, 'calls_per_cycle', cycle_start, counter
  FROM owner_usage
 WHERE capability = 'calls_per_day'
ON CONFLICT (owner_id, capability, cycle_start)
DO UPDATE SET counter = GREATEST(owner_usage.counter, EXCLUDED.counter);
