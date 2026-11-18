-- DOWN SCRIPT for sql_meshychat/014_shared_memory_signed_bytes.sql (divergence 039, B02 A1).
--
-- NOT A MIGRATION. It lives outside sql_meshychat/'s migrate set (apps/core/sqlmigrate.py
-- applies *.sql in that directory only). Since the rollback gate (apps/core/rollback.py,
-- 2026-09-25) `sqlmigrate.py --rollback-to meshychat:VERSION` runs it, in one transaction
-- with the deletion of ledger row 14; tests/test_signed_bytes_down.py and the round trip in
-- tests/test_rollback_gate.py run it too. Design section 4.3.
--
-- BY HAND, RUN IT IN ONE TRANSACTION, as the owner role:
--     psql "$DSN" --single-transaction -f sql_meshychat/down/014_shared_memory_signed_bytes_down.sql
-- and AFTER rolling the code back past A1. A1 code on the pre-A1 schema fails every write
-- (its INSERT names the column this drops). Pre-A1 code on the A1 schema keeps writing: 014
-- refuses NULL bytes only from a meshy_app session carrying the marker A1's
-- apps.core.rls.set_role sets, which pre-A1 code does not, so its rows land without bytes
-- and are classified like upstream's. So the order is: deploy the pre-A1 code, then apply
-- this (tests/test_signed_bytes_down.py runs both halves).
--
-- WHAT IS KEPT, WHAT IS LOST, AND HOW IT COMES BACK. The verdicts are KEPT: they move to
-- shared_memory_legacy_verdict_retired, owner-only, which no code reads. The job would
-- rebuild the verdicts of rows that still exist once 014 is applied again, but not of rows
-- an ephemeral rewrite or a prune has deleted since, and those are the evidence design Q14
-- keeps (adversarial review of fin/b02-a1, finding 8). A second round trip adds to the
-- retained table; a verdict already there (same row, same classifier version) is not
-- duplicated. The signed_bytes of rows written under A1 are dropped with the column; the
-- classifier recovers them from provenance_jsonb with the default loader and RFC 8785,
-- because A1's seam admitted only I-JSON numbers (the test checks every row it wrote).
--
-- KEEPS: shared_memory_legacy_verdict_retired
-- (the line above is read by apps/core/rollback.py: the round-trip test in
-- tests/test_rollback_gate.py holds this script to the catalog 014 found,
-- except that table, which it keeps on purpose.)
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 014 has: every DROP below on shared_memory or
-- skill_calls takes ACCESS EXCLUSIVE, and queued behind a long reader it would hold every
-- later reader and writer of the table behind it. It gives up after 5 s instead; retry.
--
-- ORDER MATTERS in one place: the immutability function is restored to 063's text BEFORE
-- the column is dropped, since the A1 version names NEW.signed_bytes.

SET LOCAL lock_timeout = '5s';

-- S down: the skill_calls CHECK.
ALTER TABLE skill_calls DROP CONSTRAINT IF EXISTS skill_calls_status_matches_signed_result;

-- C down: the verdicts are copied to the retained table, then the live table (its trigger
-- and policies go with it) and its function are dropped. LIKE copies the columns, their
-- defaults, the CHECKs and the primary key, and no trigger: the bind trigger looks rows up
-- in shared_memory, and a retained verdict's row may be gone. The retained table gets no
-- policy and no grant (sql/010's default privileges would give meshy_app all four), so
-- meshy_app can neither read nor change it.
DO $retain$
BEGIN
    IF to_regclass('shared_memory_legacy_verdict') IS NOT NULL THEN
        CREATE TABLE IF NOT EXISTS shared_memory_legacy_verdict_retired
            (LIKE shared_memory_legacy_verdict
             INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
        INSERT INTO shared_memory_legacy_verdict_retired
            SELECT * FROM shared_memory_legacy_verdict
            ON CONFLICT DO NOTHING;
    END IF;
    IF to_regclass('shared_memory_legacy_verdict_retired') IS NOT NULL THEN
        REVOKE ALL ON shared_memory_legacy_verdict_retired FROM meshy_app, PUBLIC;
        ALTER TABLE shared_memory_legacy_verdict_retired ENABLE ROW LEVEL SECURITY;
        ALTER TABLE shared_memory_legacy_verdict_retired FORCE ROW LEVEL SECURITY;
        COMMENT ON TABLE shared_memory_legacy_verdict_retired IS
            'Verdicts moved here by sql_meshychat/down/014 (divergence 039), kept because '
            'the job cannot rebuild the verdict of a row deleted since (design Q14). '
            'Owner-only; no code reads it.';
    END IF;
END
$retain$;
DROP TABLE IF EXISTS shared_memory_legacy_verdict;
DROP FUNCTION IF EXISTS shared_memory_legacy_verdict_bind();

-- A1 down: the agree-trigger and the rules.
DROP TRIGGER IF EXISTS shared_memory_signed_bytes_agree_trg ON shared_memory;
DROP FUNCTION IF EXISTS shared_memory_signed_bytes_agree();
DROP FUNCTION IF EXISTS sm_derive_tags(jsonb);
DROP FUNCTION IF EXISTS sm_derive_author_label(jsonb);
DROP FUNCTION IF EXISTS sm_signed_value(jsonb);
DROP FUNCTION IF EXISTS sm_derive_claimed_status(jsonb);
DROP FUNCTION IF EXISTS sm_derive_status(jsonb);
DROP FUNCTION IF EXISTS sm_derive_pre_discipline(jsonb);
DROP FUNCTION IF EXISTS sm_signed_payload(bytea);

-- A1 down: sql/063's immutability function, copied from that file verbatim.
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

-- A1 down: the column, and the ledger row, so 014 can be applied again.
ALTER TABLE shared_memory DROP COLUMN IF EXISTS signed_bytes;
DELETE FROM meshychat_migrations WHERE version = 14;
