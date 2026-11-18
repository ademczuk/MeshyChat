-- MeshyChat divergence 061: a redaction record is held to its row.
--
-- WHY. memory-bxxxxx's suggestion (2026-09-30), after a leak in a test: the commit-order stamp (027's meshy_mesh_redaction_stamp) fires WHEN the RECORD's
-- namespace is shared/mesh-chat, and the live push and the wait build the event from the record's namespace and key. Nothing held those columns, or the
-- version, to the row the record is FOR (memory_id). The handler always writes them right, but a record that names the mail namespace for a row of another
-- namespace drew a 'redaction' position for that row: tests/test_redaction_db_rule.py wrote one, and tests/test_mesh_projection.py's D19 counted it as a
-- row outside the mail when a redaction test ran before it. shared_memory_edit has had this binding since 029 (shared_memory_edit_bound_to_row).
--
-- WHAT THIS ADDS. A BEFORE INSERT trigger on shared_memory_redaction: the record's namespace, key and version are the row's, and redacted_by_owner_id is
-- the owner `peers` gives redacted_by_peer_id (the handler reads it from there, and 027's exemption compares it with the row's owner). An insert by any role
-- meets it, the owner role included. Existing records are untouched (an INSERT trigger). The down script drops the trigger and its function.
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 014 and 020-030 have.

SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE FUNCTION shared_memory_redaction_bound_to_row() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    r_namespace text;
    r_key       text;
    r_version   integer;
    v_owner     uuid;
BEGIN
    SELECT sm.namespace, sm.key, sm.version
      INTO r_namespace, r_key, r_version
      FROM shared_memory sm
     WHERE sm.id = NEW.memory_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'E_REDACTION_RECORD_UNBOUND: the record of a redaction names row %, and there is none', NEW.memory_id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT p.owner_id INTO v_owner FROM peers p WHERE p.id = NEW.redacted_by_peer_id;
    IF NEW.namespace IS DISTINCT FROM r_namespace
            OR NEW.key IS DISTINCT FROM r_key
            OR NEW.version IS DISTINCT FROM r_version
            OR NEW.redacted_by_owner_id IS DISTINCT FROM v_owner THEN
        RAISE EXCEPTION 'E_REDACTION_RECORD_UNBOUND: the record of a redaction of % version % (row %) does not match its row: the namespace, key and version must be the row''s and the owner must be the one peers gives the redacting peer',
            NEW.key, NEW.version, NEW.memory_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_redaction_bound_trg ON shared_memory_redaction;
CREATE TRIGGER shared_memory_redaction_bound_trg
    BEFORE INSERT ON shared_memory_redaction
    FOR EACH ROW EXECUTE FUNCTION shared_memory_redaction_bound_to_row();
