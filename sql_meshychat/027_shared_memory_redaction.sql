-- MeshyChat divergence 053: redaction of a stored row. Design: docs/design/redaction.md
-- (fin/redaction), reviewed by memory-bxxxxx as non-author; the operator's rulings are its
-- section 7 (ASDLC D-2026-09-29-U9, -U13, -U14).
--
-- WHAT WAS MISSING. shared_memory_enforce_immutability() refuses every UPDATE but head
-- retirement and the high_churn parent unlink (sql_meshychat/014 section 4), and no operation
-- withdraws a row's text. A row that named something it must not name stayed readable for
-- good (ASDLC OI-54).
--
-- WHAT THIS ADDS.
--   1. shared_memory_redaction: one append-only record per redacted version. It is the signed
--      event (who, acting as author or as owner, when, which row) and it is the PERMISSION
--      the trigger below asks for.
--   2. ONE more exemption in shared_memory_enforce_immutability(): value, signed_bytes,
--      tags and the payload inside provenance_jsonb may move together to the tombstone, only when a redaction record for that row
--      exists, which in practice means in the same transaction. Nothing else is relaxed.
--   3. mesh_commit_order.kind: a redaction draws its own position in the commit order, so a
--      listener that was down receives it on return. Every walker joins the order to the row
--      by memory_id and so simply meets the row a second time, now as a tombstone.
--
-- WHAT IT DOES NOT DO. It does not blank derived rows (lexical terms, vectors, inferred
-- edges), stored calls or summaries: the handler does that in the same transaction
-- (apps/memoryskills/redact.py). It does not reach copies outside the database (design
-- section 2).
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 014 and 020-026 have.

SET LOCAL lock_timeout = '5s';

-- ------------------------------------------------------------------ 1. the record

CREATE TABLE IF NOT EXISTS shared_memory_redaction (
    id                            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id                     uuid        NOT NULL UNIQUE
                                              REFERENCES shared_memory(id) ON DELETE CASCADE,
    namespace                     text        NOT NULL,
    key                           text        NOT NULL,
    version                       integer     NOT NULL,
    redacted_by_peer_id           uuid        NOT NULL,
    redacted_by_owner_id          uuid        NOT NULL,
    acted_as                      text        NOT NULL CHECK (acted_as IN ('author', 'owner')),
    reason_code                   text        NOT NULL CHECK (reason_code IN
                                      ('client_name', 'credential', 'personal_data', 'sent_in_error')),
    thread_id_blanked             boolean     NOT NULL DEFAULT false,
    original_payload_jcs_sha256   text,
    original_signed_bytes_sha256  text,
    request_signed_bytes          bytea       NOT NULL,
    request_call_id               text,
    -- The seats told besides the row's own to/cc/broadcast recipients: the authors of the direct
    -- replies at the moment of redaction (the operator's ruling, ASDLC D-2026-09-29-U13). Read by the
    -- addressed wait and by the live push when they build the redaction event; never the text.
    notify_peer_ids               uuid[]      NOT NULL DEFAULT '{}'::uuid[],
    redacted_at                   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS shared_memory_redaction_key_idx
    ON shared_memory_redaction (namespace, key, version);

ALTER TABLE shared_memory_redaction ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_memory_redaction FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS shared_memory_redaction_server_internal ON shared_memory_redaction;
CREATE POLICY shared_memory_redaction_server_internal ON shared_memory_redaction
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

-- Append-only, for every role: a BEFORE trigger fires for the owner and a superuser too.
-- DELETE is refused as well; the ON DELETE CASCADE above still removes a record with its
-- row, because a cascaded delete from the referenced table is not refused by a row trigger
-- that checks pg_trigger_depth().
CREATE OR REPLACE FUNCTION shared_memory_redaction_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
        RETURN OLD;   -- the cascade from shared_memory (a prune, or a delete of the row)
    END IF;
    RAISE EXCEPTION 'E_REDACTION_APPEND_ONLY: shared_memory_redaction rows are never changed or removed (%)',
        TG_OP USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_redaction_append_only_trg ON shared_memory_redaction;
CREATE TRIGGER shared_memory_redaction_append_only_trg
    BEFORE UPDATE OR DELETE ON shared_memory_redaction
    FOR EACH ROW EXECUTE FUNCTION shared_memory_redaction_append_only();

-- ------------------------------------------------------------------ 2. immutability
-- 014's function, verbatim, plus the redaction exemption in front. The trigger binding
-- (shared_memory_immutability) is unchanged.
--
-- THE TOMBSTONE SHAPE the exemption accepts, checked here and not only in the handler:
--   value         an object whose "redacted" is true; whose keys are ALL within
--                 shared_memory_tombstone_keys() (the three the handler adds, and the routing fields it keeps,
--                 whichever namespace the row is in); whose routing fields are OLD's or absent (a field whose
--                 shape was not routing is dropped, never changed), with `thread_id` also allowed to be
--                 'redacted/<the record's id>' when the record says it was blanked; and NOT a row that is
--                 already a tombstone, so one redaction is one rewrite and not a standing permission;
--   record        bound to the row: its owner is the row's created_by_owner_id, the redacting peer's owner
--                 (read from peers, not from the record's own column) is that owner, and its reason_code is the
--                 value's (memory-bxxxxx's review of #32, blocking 2: the first exemption admitted a second
--                 rewrite of a tombstone that re-addressed it, changed `from` or added a content field);
--   signed_bytes  NULL;
--   provenance_jsonb  the old envelope with its "payload" cut down to {"call_id": ...} (the
--                 signed payload is stored there verbatim, body included; call_id stays
--                 because provenance_call_id is generated from it and is NOT NULL), plus
--                 "payload_redacted": true. The signature, the kid and the times stay;
--   tags          exactly {redacted};
--   every other column unchanged, payload_jcs_sha256 included.
-- The keys a tombstone may hold: the three the handler adds and the routing fields it keeps
-- (apps/memoryskills/redact.py ROUTING_FIELDS; tests/test_redaction_db_rule.py pins this list against it). ONE list, so the
-- allowlist below covers every namespace and any content field a future writer invents, with no per-namespace list.
CREATE OR REPLACE FUNCTION shared_memory_tombstone_keys() RETURNS text[]
    LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['redacted', 'reason_code', 'redacted_at',
                 'schema_version', 'from', 'to', 'cc', 'type', 'thread_id', 'in_reply_to', 'ts', 'author_family']
$$;

CREATE OR REPLACE FUNCTION shared_memory_enforce_immutability() RETURNS TRIGGER AS $$
DECLARE
    v_policy text;
    v_high_churn_parent_unlink boolean := FALSE;
    v_redaction boolean := FALSE;
BEGIN
    IF (NEW.value IS DISTINCT FROM OLD.value
        OR NEW.signed_bytes IS DISTINCT FROM OLD.signed_bytes
        OR NEW.tags IS DISTINCT FROM OLD.tags
        OR NEW.provenance_jsonb IS DISTINCT FROM OLD.provenance_jsonb)
       AND NEW.id                  IS NOT DISTINCT FROM OLD.id
       AND NEW.namespace           IS NOT DISTINCT FROM OLD.namespace
       AND NEW.key                 IS NOT DISTINCT FROM OLD.key
       AND NEW.version             IS NOT DISTINCT FROM OLD.version
       AND NEW.parent_id           IS NOT DISTINCT FROM OLD.parent_id
       AND NEW.is_head             IS NOT DISTINCT FROM OLD.is_head
       AND NEW.status              IS NOT DISTINCT FROM OLD.status
       AND NEW.pre_discipline      IS NOT DISTINCT FROM OLD.pre_discipline
       AND NEW.claimed_status      IS NOT DISTINCT FROM OLD.claimed_status
       AND NEW.author_label        IS NOT DISTINCT FROM OLD.author_label
       AND NEW.embed_policy        IS NOT DISTINCT FROM OLD.embed_policy
       AND NEW.provenance_jsonb    IS NOT DISTINCT FROM
               ((OLD.provenance_jsonb - 'payload')
                || jsonb_build_object('payload_redacted', true, 'payload',
                       jsonb_build_object('call_id', OLD.provenance_jsonb -> 'payload' -> 'call_id')))
       AND NEW.payload_jcs_sha256  IS NOT DISTINCT FROM OLD.payload_jcs_sha256
       AND NEW.created_by_peer_id  IS NOT DISTINCT FROM OLD.created_by_peer_id
       AND NEW.created_by_owner_id IS NOT DISTINCT FROM OLD.created_by_owner_id
       AND NEW.created_at          IS NOT DISTINCT FROM OLD.created_at
       AND NEW.signed_bytes IS NULL
       AND NEW.tags = ARRAY['redacted']::text[]
       AND jsonb_typeof(NEW.value) = 'object'
       AND NEW.value -> 'redacted' = 'true'::jsonb
       -- IS DISTINCT FROM, not NOT (... = ...): an OLD with no `redacted` key makes `->` NULL, and NOT (NULL) is NULL, which an IF
       -- reads as false. The first form of this line refused every ordinary redaction.
       AND (OLD.value -> 'redacted') IS DISTINCT FROM 'true'::jsonb
       AND NOT (NEW.value ?| ARRAY['body', 'subject', 'ref', 'session_label'])
       AND NOT EXISTS (SELECT 1 FROM jsonb_object_keys(CASE WHEN jsonb_typeof(NEW.value) = 'object'
                                                            THEN NEW.value ELSE '{}'::jsonb END) AS k
                        WHERE k <> ALL (shared_memory_tombstone_keys()))
       AND EXISTS (
            SELECT 1 FROM shared_memory_redaction r
             WHERE r.memory_id = OLD.id
               AND r.redacted_by_owner_id = OLD.created_by_owner_id
               AND r.redacted_by_owner_id IS NOT DISTINCT FROM (SELECT p.owner_id FROM peers p
                                                                 WHERE p.id = r.redacted_by_peer_id)
               AND r.reason_code = NEW.value ->> 'reason_code'
               AND NOT EXISTS (
                    SELECT 1 FROM unnest(shared_memory_tombstone_keys()) AS f
                     WHERE f NOT IN ('redacted', 'reason_code', 'redacted_at')
                       AND (NEW.value -> f) IS NOT NULL
                       AND (NEW.value -> f) IS DISTINCT FROM (OLD.value -> f)
                       AND NOT (f = 'thread_id' AND r.thread_id_blanked
                                AND NEW.value ->> 'thread_id' = 'redacted/' || r.id::text)))
    THEN
        v_redaction := TRUE;
    END IF;

    IF NOT v_redaction AND OLD.parent_id IS NOT NULL AND NEW.parent_id IS NULL THEN
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

    IF NOT v_high_churn_parent_unlink AND NOT v_redaction THEN
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

-- ------------------------------------------------------------------ 3. the commit order
-- One position per (row, kind). 'write' is what 023 has always stamped; 'redaction' is
-- stamped by meshy_mesh_redaction_stamp() below, under the SAME advisory lock, so the order
-- stays gapless-visible for the walkers exactly as 023 argues for writes.
ALTER TABLE mesh_commit_order ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'write';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mesh_commit_order_kind_chk') THEN
        ALTER TABLE mesh_commit_order ADD CONSTRAINT mesh_commit_order_kind_chk
            CHECK (kind IN ('write', 'redaction'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mesh_commit_order_memory_kind_key') THEN
        ALTER TABLE mesh_commit_order ADD CONSTRAINT mesh_commit_order_memory_kind_key
            UNIQUE (memory_id, kind);
    END IF;
END;
$$;
ALTER TABLE mesh_commit_order DROP CONSTRAINT IF EXISTS mesh_commit_order_memory_id_key;
CREATE INDEX IF NOT EXISTS mesh_commit_order_memory_idx ON mesh_commit_order (memory_id);

-- 023's stamper named the old constraint in its ON CONFLICT; restate it for the new one.
CREATE OR REPLACE FUNCTION meshy_mesh_commit_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(1835233133, 1);
    INSERT INTO mesh_commit_order (memory_id, kind)
    SELECT NEW.id, 'write' WHERE EXISTS (SELECT 1 FROM shared_memory WHERE id = NEW.id)
    ON CONFLICT (memory_id, kind) DO NOTHING;
    RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION meshy_mesh_commit_stamp() FROM PUBLIC;

CREATE OR REPLACE FUNCTION meshy_mesh_redaction_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(1835233133, 1);
    INSERT INTO mesh_commit_order (memory_id, kind)
    SELECT NEW.memory_id, 'redaction'
     WHERE EXISTS (SELECT 1 FROM shared_memory WHERE id = NEW.memory_id)
    ON CONFLICT (memory_id, kind) DO NOTHING;
    RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION meshy_mesh_redaction_stamp() FROM PUBLIC;

DROP TRIGGER IF EXISTS shared_memory_redaction_commit_order ON shared_memory_redaction;
CREATE CONSTRAINT TRIGGER shared_memory_redaction_commit_order
    AFTER INSERT ON shared_memory_redaction
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.namespace = 'shared/mesh-chat')
    EXECUTE FUNCTION meshy_mesh_redaction_stamp();

-- ------------------------------------------------------------------ 4. the push's wake-up
-- The live push (apps/wsgw/mesh_notify.py) learns of a redaction here. The payload is a POINTER, as
-- sql/036's is: the record's id. The bridge reads the record and the tombstone back and builds the event
-- from them, so a forged notification can at worst re-announce a redaction that exists. pg_notify is
-- queued until COMMIT, so a listener never hears of a redaction that rolled back, and it hears after the
-- commit-order position above is visible (both happen at commit).
CREATE OR REPLACE FUNCTION meshy_mesh_redaction_notify() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify('meshy_mesh_redaction', json_build_object('id', NEW.id)::text);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_redaction_notify ON shared_memory_redaction;
CREATE TRIGGER shared_memory_redaction_notify
    AFTER INSERT ON shared_memory_redaction
    FOR EACH ROW
    WHEN (NEW.namespace = 'shared/mesh-chat')
    EXECUTE FUNCTION meshy_mesh_redaction_notify();

-- meshy_app inserts the record and reads it; it never changes or removes one (the trigger
-- above binds every other role).
REVOKE UPDATE, DELETE, TRUNCATE ON shared_memory_redaction FROM meshy_app;

COMMENT ON TABLE shared_memory_redaction IS
  'Divergence 053: one append-only record per redacted shared_memory version. The record is '
  'the permission shared_memory_enforce_immutability() asks for. docs/design/redaction.md.';
