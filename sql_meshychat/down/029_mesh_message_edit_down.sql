-- DOWN for sql_meshychat/029 (divergence 059, edit). Order: this before 027's down.
-- `sqlmigrate.py --rollback-to meshychat:28` runs it (the rollback gate, apps/core/rollback.py), newest first.
--
-- WHAT COMES BACK: 011's single-writer function and 014's agree function, each copied from its file verbatim
-- (tests/test_edit_db_rule.py pins both copies against those files), and the commit order's two kinds.
-- WHAT IS LOST: the edit RECORDS (the table is dropped) and the edit positions in the commit order. WHAT STAYS: the
-- edited versions themselves. They are ordinary rows whose signed payload is a memory.edit request; once 014's agree
-- function is back, no NEW row can carry one, and the rows already stored are not re-checked (an INSERT trigger).
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 029 has.

SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS shared_memory_edit_notify ON shared_memory_edit;
DROP FUNCTION IF EXISTS meshy_mesh_edit_notify();
DROP TRIGGER IF EXISTS shared_memory_edit_commit_order ON shared_memory_edit;
DROP FUNCTION IF EXISTS meshy_mesh_edit_stamp();

DELETE FROM mesh_commit_order WHERE kind = 'edit';
ALTER TABLE mesh_commit_order DROP CONSTRAINT IF EXISTS mesh_commit_order_kind_chk;
ALTER TABLE mesh_commit_order ADD CONSTRAINT mesh_commit_order_kind_chk CHECK (kind IN ('write', 'redaction'));

CREATE OR REPLACE FUNCTION shared_memory_mesh_message_single_writer() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_root_peer    uuid;
    v_root_version integer;
BEGIN
    SELECT created_by_peer_id, version INTO v_root_peer, v_root_version
      FROM shared_memory
     WHERE namespace = NEW.namespace
       AND key = NEW.key
       AND version < NEW.version
     ORDER BY version ASC
     LIMIT 1;
    IF v_root_peer IS NULL THEN
        RAISE EXCEPTION
            'E_NOT_MESSAGE_AUTHOR: no earlier version of mesh message % is visible to this insert of version %; refused',
            NEW.key, NEW.version USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_by_peer_id IS DISTINCT FROM v_root_peer THEN
        RAISE EXCEPTION
            'E_NOT_MESSAGE_AUTHOR: mesh message % was created (version %) by peer %; peer % may not write version % (divergence 028)',
            NEW.key, v_root_version, v_root_peer, NEW.created_by_peer_id, NEW.version
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION shared_memory_signed_bytes_agree() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    p      jsonb;
    a      jsonb;
    v_text text;
    v_tags text[];
BEGIN
    -- Invariant 1, A1 form (design Q12): this build inserts as meshy_app and must carry the
    -- bytes, so a regression that drops the column from its INSERT fails the write instead
    -- of producing an unverifiable row. THE ROLE ALONE DOES NOT SAY WHOSE SESSION IT IS:
    -- upstream's meshy-server also runs as meshy_app (its pool's `SET ROLE`, MESHY_SET_ROLE_TO
    -- in production) and inserts rows with no bytes (the dashboard's memory create, revise
    -- and message send; the identity rate watch), which design 4.1 says A1 leaves working.
    -- So the refusal is keyed to meshy_app AND the marker apps.core.rls.set_role sets on
    -- every session this build opens. Any other session (upstream's server or daemon, a
    -- restore, a test seeding history) is admitted with NULL until A2, and held to nothing
    -- else here: its rows are classified by the legacy verdict job.
    IF NEW.signed_bytes IS NULL THEN
        IF current_user = 'meshy_app'
                AND current_setting('meshy.signed_bytes_writer', true) = 'on' THEN
            RAISE EXCEPTION 'E_SIGNED_BYTES_REQUIRED: meshy_app must store the signed bytes of %/% (divergence 039)',
                NEW.namespace, NEW.key USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- Invariant 2: the bytes are the ones the stored hash commits to.
    IF encode(sha256(NEW.signed_bytes), 'hex') IS DISTINCT FROM NEW.payload_jcs_sha256 THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: payload_jcs_sha256: the column is not the SHA-256 of signed_bytes'
            USING ERRCODE = 'check_violation';
    END IF;

    BEGIN
        p := sm_signed_payload(NEW.signed_bytes);
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: signed_bytes: not UTF-8 JSON (%)', SQLERRM
            USING ERRCODE = 'check_violation';
    END;
    IF jsonb_typeof(p) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: signed_bytes: not a JSON object'
            USING ERRCODE = 'check_violation';
    END IF;

    -- Invariant 3: the stored payload is the signed one.
    IF p IS DISTINCT FROM NEW.provenance_jsonb -> 'payload' THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: provenance_jsonb: payload is not the signed payload'
            USING ERRCODE = 'check_violation';
    END IF;

    -- The envelope binding, REQUIRED in the bytes whatever the path: the signer, the domain
    -- separator and the skill. G2: the board route landed a payload with none of the first two.
    IF jsonb_typeof(p -> 'caller_peer_id') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: caller_peer_id: the signed payload names no caller'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p ->> 'kind' IS DISTINCT FROM 'skill.call' THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: kind: the signed payload is %, not skill.call',
            coalesce(p ->> 'kind', 'absent') USING ERRCODE = 'check_violation';
    END IF;
    IF p ->> 'skill_name' IS DISTINCT FROM 'memory.write' THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: skill_name: the signed payload is %, not memory.write',
            coalesce(p ->> 'skill_name', 'absent') USING ERRCODE = 'check_violation';
    END IF;
    IF p ->> 'caller_peer_id' IS DISTINCT FROM NEW.created_by_peer_id::text THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: created_by_peer_id: % is not the signer %',
            NEW.created_by_peer_id, p ->> 'caller_peer_id' USING ERRCODE = 'check_violation';
    END IF;

    -- Invariant 4: every derived column agrees (design 2.3).
    a := p -> 'args';
    IF jsonb_typeof(a) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: args: the signed args are not an object'
            USING ERRCODE = 'check_violation';
    END IF;
    IF jsonb_typeof(a -> 'namespace') IS DISTINCT FROM 'string'
            OR a ->> 'namespace' IS DISTINCT FROM NEW.namespace THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: namespace: the column says %, the bytes %',
            NEW.namespace, a -> 'namespace' USING ERRCODE = 'check_violation';
    END IF;
    IF jsonb_typeof(a -> 'key') IS DISTINCT FROM 'string'
            OR a ->> 'key' IS DISTINCT FROM NEW.key THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: key: the column says %, the bytes %',
            NEW.key, a -> 'key' USING ERRCODE = 'check_violation';
    END IF;
    IF sm_signed_value(p) IS DISTINCT FROM NEW.value THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: value: the column is not the signed value'
            USING ERRCODE = 'check_violation';
    END IF;
    IF sm_derive_pre_discipline(p) IS DISTINCT FROM NEW.pre_discipline THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: pre_discipline: the column says %, the bytes derive %',
            NEW.pre_discipline, sm_derive_pre_discipline(p) USING ERRCODE = 'check_violation';
    END IF;
    IF sm_derive_status(p) IS DISTINCT FROM NEW.status THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: status: the column says %, the bytes derive %',
            coalesce(NEW.status, 'NULL'), coalesce(sm_derive_status(p), 'NULL')
            USING ERRCODE = 'check_violation';
    END IF;
    IF sm_derive_claimed_status(p) IS DISTINCT FROM NEW.claimed_status THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: claimed_status: the column says %, the bytes derive %',
            coalesce(NEW.claimed_status, 'NULL'), coalesce(sm_derive_claimed_status(p), 'NULL')
            USING ERRCODE = 'check_violation';
    END IF;
    v_text := sm_derive_author_label(p);
    IF v_text IS NOT NULL AND v_text IS DISTINCT FROM NEW.author_label THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: author_label: the column says %, the writer signed %',
            coalesce(NEW.author_label, 'NULL'), v_text USING ERRCODE = 'check_violation';
    END IF;
    v_tags := sm_derive_tags(p);
    IF v_tags IS NOT NULL AND v_tags IS DISTINCT FROM NEW.tags THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: tags: the column says %, the writer signed %',
            NEW.tags, v_tags USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_edit_bound_trg ON shared_memory_edit;
DROP TRIGGER IF EXISTS shared_memory_edit_append_only_trg ON shared_memory_edit;
DROP TABLE IF EXISTS shared_memory_edit;
DROP FUNCTION IF EXISTS shared_memory_edit_bound_to_row();
DROP FUNCTION IF EXISTS shared_memory_edit_append_only();

-- The ledger row, so the migration can be applied again.
DELETE FROM meshychat_migrations WHERE version = 29;
