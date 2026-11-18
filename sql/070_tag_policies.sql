-- 070_tag_policies.sql
-- TASK-34 AC#4 (native): operator-authored tag-policy rules, loaded by
-- load_active_tag_policies and evaluated by meshy_grants::tag_policy::evaluate_with_reason
-- (DENY-ABSOLUTE precedence, subset tag match, fail-closed on a missing constrained
-- param). Stored as explicit columns so an operator can read and author rows directly;
-- only the per-parameter constraints are nested, as a self-describing JSONB map
--   { "<param>": { "op": "<op>", "value": <number|string> } }
-- with op in (num_le, num_lt, num_ge, num_gt, num_eq, str_eq, str_ne). A numeric op needs
-- a JSON number; a string op needs a JSON string; any other shape fails the load.
--
-- Deploy-dark: additive CREATE TABLE only, no rows seeded, and NOTHING reads this table
-- until the request-boundary gate is enabled behind MESHY_TAG_POLICY_ENFORCE (default off).
-- The engine is DEFAULT-DENY (no matching allow rule = deny), so the safety on deploy is
-- the disabled flag, NOT the empty table: an operator must seed allow rules for the
-- functions they gate BEFORE turning enforcement on, or every tag-gated call would deny.
CREATE TABLE IF NOT EXISTS tag_policies (
    policy_id   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    priority    integer     NOT NULL DEFAULT 0,
    caller_tags text[]      NOT NULL DEFAULT '{}',
    target_tags text[]      NOT NULL DEFAULT '{}',
    functions   text[]      NOT NULL DEFAULT '{}',
    constraints jsonb       NOT NULL DEFAULT '{}'::jsonb,
    action      text        NOT NULL CHECK (action IN ('allow', 'deny')),
    active      boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tag_policies_active
    ON tag_policies (priority)
    WHERE active;
