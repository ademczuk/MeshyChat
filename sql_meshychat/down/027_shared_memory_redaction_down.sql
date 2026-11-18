-- DOWN for sql_meshychat/027 (divergence 053, redaction). Order: this before 014's down.
-- `sqlmigrate.py --rollback-to meshychat:26` runs it (the rollback gate, apps/core/rollback.py), newest first.
--
-- WHAT COMES BACK: 014's immutability function and 023's commit stamper, each copied from
-- its file verbatim, and mesh_commit_order's one-position-per-row uniqueness.
-- WHAT IS LOST: the redaction RECORDS (the table is dropped) and the redaction positions in
-- the commit order. WHAT DOES NOT COME BACK: redacted text. A row redacted while 027 was live
-- stays a tombstone with NULL signed_bytes, and 014's function then keeps it unchangeable.
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 027 has.

SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS shared_memory_redaction_notify ON shared_memory_redaction;
DROP FUNCTION IF EXISTS meshy_mesh_redaction_notify();
DROP TRIGGER IF EXISTS shared_memory_redaction_commit_order ON shared_memory_redaction;
DROP FUNCTION IF EXISTS meshy_mesh_redaction_stamp();

DELETE FROM mesh_commit_order WHERE kind = 'redaction';
ALTER TABLE mesh_commit_order DROP CONSTRAINT IF EXISTS mesh_commit_order_memory_kind_key;
ALTER TABLE mesh_commit_order DROP CONSTRAINT IF EXISTS mesh_commit_order_kind_chk;
DROP INDEX IF EXISTS mesh_commit_order_memory_idx;
ALTER TABLE mesh_commit_order DROP COLUMN IF EXISTS kind;
ALTER TABLE mesh_commit_order ADD CONSTRAINT mesh_commit_order_memory_id_key UNIQUE (memory_id);

CREATE OR REPLACE FUNCTION meshy_mesh_commit_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    -- Held to this transaction's end: the next stamper draws after this one is visible.
    PERFORM pg_advisory_xact_lock(1835233133, 1);
    INSERT INTO mesh_commit_order (memory_id)
    SELECT NEW.id WHERE EXISTS (SELECT 1 FROM shared_memory WHERE id = NEW.id)
    ON CONFLICT (memory_id) DO NOTHING;
    RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION meshy_mesh_commit_stamp() FROM PUBLIC;

CREATE OR REPLACE FUNCTION shared_memory_enforce_immutability() RETURNS TRIGGER AS $$
DECLARE
    v_policy text;
    v_high_churn_parent_unlink boolean := FALSE;
BEGIN
    IF OLD.parent_id IS NOT NULL AND NEW.parent_id IS NULL THEN
        SELECT retention_policy INTO v_policy
        FROM shared_memory_namespace
        WHERE namespace = OLD.namespace;
        IF v_policy IS NULL THEN
            v_policy := 'keep_all';
        END IF;

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
            AND NEW.signed_bytes        IS NOT DISTINCT FROM OLD.signed_bytes
            AND NEW.created_by_peer_id  IS NOT DISTINCT FROM OLD.created_by_peer_id
            AND NEW.created_by_owner_id IS NOT DISTINCT FROM OLD.created_by_owner_id
            AND NEW.created_at          IS NOT DISTINCT FROM OLD.created_at
        THEN
            v_high_churn_parent_unlink := TRUE;
        END IF;
    END IF;

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
            OR NEW.signed_bytes        IS DISTINCT FROM OLD.signed_bytes
            OR NEW.created_by_peer_id  IS DISTINCT FROM OLD.created_by_peer_id
            OR NEW.created_by_owner_id IS DISTINCT FROM OLD.created_by_owner_id
            OR NEW.created_at          IS DISTINCT FROM OLD.created_at
        THEN
            RAISE EXCEPTION 'shared_memory rows are immutable; only head retirement (is_head TRUE->FALSE) is permitted';
        END IF;
    END IF;

    IF OLD.is_head = FALSE AND NEW.is_head = TRUE THEN
        RAISE EXCEPTION 'shared_memory head cannot be resurrected (is_head FALSE->TRUE forbidden)';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP FUNCTION IF EXISTS shared_memory_tombstone_keys();
DROP TRIGGER IF EXISTS shared_memory_redaction_append_only_trg ON shared_memory_redaction;
DROP TABLE IF EXISTS shared_memory_redaction;
DROP FUNCTION IF EXISTS shared_memory_redaction_append_only();

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 27;
