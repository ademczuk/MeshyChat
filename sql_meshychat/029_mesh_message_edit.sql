-- MeshyChat divergence 059: edit of a mail row. Design: docs/design/edit.md (merged as a design record, PR #29),
-- approved by memory-bxxxxx as non-author; the operator's ruling is ASDLC D-2026-09-29-U14 ("New version, history
-- kept"): a seat under the same owner edits a message, the edit is a new signed version, the old version stays and
-- can itself be redacted.
--
-- WHAT THIS ADDS.
--   1. shared_memory_edit: one append-only record per edit. It holds no text (the new row holds the signed request,
--      which holds the new body, and a redaction blanks that copy): who, when, which version it replaced, and the
--      reply authors to tell. The wait and the live push read it to build the `mesh_edit` event. A BEFORE INSERT trigger
--      holds it to its row: its namespace, key, version, editor and the editor's owner are the row's and the peer's, and the
--      row carries a memory.edit request (the record is what `memory.read`, the message frame and the event name an editor from).
--   2. mesh_commit_order.kind gains 'edit': the record draws its own position, so a listener that was down is
--      told, exactly as for a redaction (sql_meshychat/027).
--   3. shared_memory_mesh_message_single_writer() (011) becomes the one rule for a later version of a mail key:
--      REFUSED FOR EVERY PEER, the author included, unless the transaction carries the edit path's mark AND the
--      database's own checks pass. The author's own memory.write of a later version was an unguarded edit path
--      (nothing froze to or cc on it; production held 53 such rows, the last of 2026-09-12, 8 of them re-addressed).
--   4. shared_memory_signed_bytes_agree() (014) admits a memory.edit payload as the row's signed bytes, on the edit
--      path only. Its body is 014's, with one block changed.
--
-- WHAT THE MARK IS AND IS NOT. The edit path sets `app.mesh_edit_owner` in its own transaction. ANY ROLE CAN SET A
-- CUSTOM SETTING, so the mark authorizes nothing; it says which path is inserting. What the database holds is the
-- rest of the rule: the owner of NEW.created_by_peer_id (read from peers, a NULL refuses, and never taken from the row's own
-- owner column, which must then AGREE with it) equals the mark and equals version 1's created_by_owner_id (an immutable, stored
-- fact); the stored request is a memory.edit call; the previous version (looked up as version - 1, not as the head, which the
-- handler has retired) is not redacted; and the new value differs from it in `body` only.
--
-- WHAT IT DOES NOT TOUCH. Existing rows (an INSERT trigger). shared_memory_immutability: an edit retires the head,
-- the one UPDATE it allows. Namespaces other than shared/mesh-chat and keys other than msg/ (the trigger's WHEN).
-- RESTORES: an import or restore that inserts historical versions of mail keys meets the refusal unless it runs
-- with triggers skipped (session_replication_role = replica as a superuser, or pg_restore --disable-triggers), which
-- is a reviewed step in the runbook and not a habit.
--
-- A BOUNDED WAIT FOR ITS LOCKS, as 014 and 020-028 have.

SET LOCAL lock_timeout = '5s';

-- ------------------------------------------------------------------ 1. the record
CREATE TABLE IF NOT EXISTS shared_memory_edit (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id           uuid        NOT NULL UNIQUE REFERENCES shared_memory(id) ON DELETE CASCADE,
    namespace           text        NOT NULL,
    key                 text        NOT NULL,
    version             integer     NOT NULL,
    prior_version       integer     NOT NULL,
    edited_by_peer_id   uuid        NOT NULL,
    edited_by_owner_id  uuid        NOT NULL,
    request_call_id     text,
    -- The seats told besides the row's own to/cc/broadcast recipients: the authors of the direct replies at the moment
    -- of the edit (design/edit.md section 4), fixed here so the wait and the push cannot disagree about a reply
    -- written afterwards. Never text.
    notify_peer_ids     uuid[]      NOT NULL DEFAULT '{}'::uuid[],
    edited_at           timestamptz NOT NULL DEFAULT now(),
    CHECK (version = prior_version + 1)
);

CREATE INDEX IF NOT EXISTS shared_memory_edit_key_idx ON shared_memory_edit (namespace, key, version);

ALTER TABLE shared_memory_edit ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_memory_edit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS shared_memory_edit_server_internal ON shared_memory_edit;
CREATE POLICY shared_memory_edit_server_internal ON shared_memory_edit
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

-- Append-only for every role, as shared_memory_redaction is: a cascaded delete from shared_memory (a prune) is the
-- one removal, told apart by the trigger depth.
CREATE OR REPLACE FUNCTION shared_memory_edit_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
        RETURN OLD;   -- the cascade from shared_memory
    END IF;
    RAISE EXCEPTION 'E_EDIT_APPEND_ONLY: shared_memory_edit rows are never changed or removed (%)', TG_OP
        USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_edit_append_only_trg ON shared_memory_edit;
CREATE TRIGGER shared_memory_edit_append_only_trg
    BEFORE UPDATE OR DELETE ON shared_memory_edit
    FOR EACH ROW EXECUTE FUNCTION shared_memory_edit_append_only();

-- BOUND TO ITS ROW. `memory.read`, the message frame and the `mesh_edit` event take `edited_by` and `edited_at` from this record, so a record that
-- named the wrong row, version or editor would label a version as edited that an edit did not write. Its writer is the handler, in the
-- server-internal context, and the database holds what the handler means: the record's namespace, key, version and editor are the ROW's
-- (memory_id), the editor's owner is the one `peers` gives that peer, and the row carries a memory.edit request as its signed bytes. An
-- insert by the owner role meets it too.
CREATE OR REPLACE FUNCTION shared_memory_edit_bound_to_row() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    r_namespace text;
    r_key       text;
    r_version   integer;
    r_peer      uuid;
    r_skill     text;
    v_owner     uuid;
BEGIN
    SELECT sm.namespace, sm.key, sm.version, sm.created_by_peer_id, sm.provenance_jsonb -> 'payload' ->> 'skill_name'
      INTO r_namespace, r_key, r_version, r_peer, r_skill
      FROM shared_memory sm
     WHERE sm.id = NEW.memory_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'E_EDIT_RECORD_UNBOUND: the record of an edit names row %, and there is none', NEW.memory_id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT p.owner_id INTO v_owner FROM peers p WHERE p.id = NEW.edited_by_peer_id;
    IF NEW.namespace IS DISTINCT FROM r_namespace
            OR NEW.key IS DISTINCT FROM r_key
            OR NEW.version IS DISTINCT FROM r_version
            OR NEW.edited_by_peer_id IS DISTINCT FROM r_peer
            OR NEW.edited_by_owner_id IS DISTINCT FROM v_owner
            OR r_skill IS DISTINCT FROM 'memory.edit' THEN
        RAISE EXCEPTION 'E_EDIT_RECORD_UNBOUND: the record of an edit of % version % (row %) does not match its row: the namespace, key, version, editor and editor owner must be the row and the peer, and the row must carry a memory.edit request',
            NEW.key, NEW.version, NEW.memory_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_edit_bound_trg ON shared_memory_edit;
CREATE TRIGGER shared_memory_edit_bound_trg
    BEFORE INSERT ON shared_memory_edit
    FOR EACH ROW EXECUTE FUNCTION shared_memory_edit_bound_to_row();

-- ------------------------------------------------------------------ 2. the commit order
ALTER TABLE mesh_commit_order DROP CONSTRAINT IF EXISTS mesh_commit_order_kind_chk;
ALTER TABLE mesh_commit_order ADD CONSTRAINT mesh_commit_order_kind_chk
    CHECK (kind IN ('write', 'redaction', 'edit'));

CREATE OR REPLACE FUNCTION meshy_mesh_edit_stamp() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(1835233133, 1);
    INSERT INTO mesh_commit_order (memory_id, kind)
    SELECT NEW.memory_id, 'edit'
     WHERE EXISTS (SELECT 1 FROM shared_memory WHERE id = NEW.memory_id)
    ON CONFLICT (memory_id, kind) DO NOTHING;
    RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION meshy_mesh_edit_stamp() FROM PUBLIC;

DROP TRIGGER IF EXISTS shared_memory_edit_commit_order ON shared_memory_edit;
CREATE CONSTRAINT TRIGGER shared_memory_edit_commit_order
    AFTER INSERT ON shared_memory_edit
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.namespace = 'shared/mesh-chat')
    EXECUTE FUNCTION meshy_mesh_edit_stamp();

-- ------------------------------------------------------------------ 3. the push's wake-up
-- A POINTER, as 036's and 027's payloads are: the record's id. pg_notify is queued until COMMIT, so a listener never
-- hears of an edit that rolled back, and hears after the commit-order position above is visible.
CREATE OR REPLACE FUNCTION meshy_mesh_edit_notify() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify('meshy_mesh_edit', json_build_object('id', NEW.id)::text);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_edit_notify ON shared_memory_edit;
CREATE TRIGGER shared_memory_edit_notify
    AFTER INSERT ON shared_memory_edit
    FOR EACH ROW
    WHEN (NEW.namespace = 'shared/mesh-chat')
    EXECUTE FUNCTION meshy_mesh_edit_notify();

REVOKE UPDATE, DELETE, TRUNCATE ON shared_memory_edit FROM meshy_app;

-- ------------------------------------------------------------------ 4. one door for a later version
-- 011's function, replaced. The trigger binding (shared_memory_mesh_single_writer_trg: BEFORE INSERT, keys msg/% in
-- shared/mesh-chat, version > 1) is unchanged.
CREATE OR REPLACE FUNCTION shared_memory_mesh_message_single_writer() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_setting      text := nullif(current_setting('app.mesh_edit_owner', true), '');
    v_root_peer    uuid;
    v_root_owner   uuid;
    v_root_version integer;
    v_editor_owner uuid;
    v_prev_id      uuid;
    v_prev_value   jsonb;
BEGIN
    -- WITHOUT the edit path's mark (or outside the server-internal context): 011's rule as it was for a second
    -- writer, with 011's own refusal, and a new refusal for the AUTHOR, whose own later version was an unguarded edit
    -- path (nothing froze to or cc on it). Every later version now goes through memory.edit.
    IF v_setting IS NULL OR current_setting('app.server_internal', true) IS DISTINCT FROM '1' THEN
        SELECT created_by_peer_id, version INTO v_root_peer, v_root_version
          FROM shared_memory
         WHERE namespace = NEW.namespace AND key = NEW.key AND version < NEW.version
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
        RAISE EXCEPTION
            'E_USE_MEMORY_EDIT: a later version of mesh message % (version %) is written only by memory.edit (divergence 059)',
            NEW.key, NEW.version USING ERRCODE = 'check_violation';
    END IF;

    -- THE EDIT PATH. The mark and the server-internal context say WHICH PATH is inserting and authorize nothing by
    -- themselves: any role can SET a custom setting. What the database holds is below.
    --
    -- Version 1's owner: a stored fact, written when that row was inserted and unchangeable since. Not the new row's
    -- own owner column, which the inserting code writes.
    SELECT created_by_owner_id, version INTO v_root_owner, v_root_version
      FROM shared_memory
     WHERE namespace = NEW.namespace AND key = NEW.key AND version < NEW.version
     ORDER BY version ASC
     LIMIT 1;
    IF v_root_owner IS NULL THEN
        RAISE EXCEPTION
            'E_NOT_MESSAGE_AUTHOR: no earlier version of mesh message % is visible to this insert of version %; refused',
            NEW.key, NEW.version USING ERRCODE = 'check_violation';
    END IF;

    -- The row is the EDITOR'S memory.edit request, signed. A marked insert whose stored request is a memory.write payload would be a
    -- later version written as an ordinary write, with no record, no event and no edit metadata: the path this function closes,
    -- reopened through the mark (hosted review of #35).
    IF NEW.provenance_jsonb -> 'payload' ->> 'skill_name' IS DISTINCT FROM 'memory.edit' THEN
        RAISE EXCEPTION
            'E_USE_MEMORY_EDIT: a later version of mesh message % on the edit path must carry the memory.edit request its editor signed (divergence 059)',
            NEW.key USING ERRCODE = 'check_violation';
    END IF;

    -- The inserting peer's owner, read from peers (a NULL refuses): the row's owner column is not trusted to NAME the owner, so a row
    -- naming owner X with a peer of owner Y cannot pass, and the column must AGREE with the peer's (memory.redact reads it; hosted
    -- review of #35).
    SELECT owner_id INTO v_editor_owner FROM peers WHERE id = NEW.created_by_peer_id;
    IF v_editor_owner IS NULL THEN
        RAISE EXCEPTION 'E_NOT_MESSAGE_AUTHOR: peer % has no owner in peers; refused', NEW.created_by_peer_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_editor_owner::text IS DISTINCT FROM v_setting
            OR v_editor_owner IS DISTINCT FROM v_root_owner
            OR NEW.created_by_owner_id IS DISTINCT FROM v_editor_owner THEN
        RAISE EXCEPTION
            'E_NOT_MESSAGE_AUTHOR: peer % (owner %) may not write version % of mesh message %, which owner % wrote (divergence 059)',
            NEW.created_by_peer_id, v_editor_owner, NEW.version, NEW.key, v_root_owner
            USING ERRCODE = 'check_violation';
    END IF;

    -- The previous version, looked up as version - 1 and NOT as the head: the handler retires the head before this
    -- insert, so an is_head lookup would find nothing and the comparison below would run against NULL.
    SELECT id, value INTO v_prev_id, v_prev_value
      FROM shared_memory
     WHERE namespace = NEW.namespace AND key = NEW.key AND version = NEW.version - 1;
    IF v_prev_value IS NULL THEN
        RAISE EXCEPTION 'E_NOT_MESSAGE_AUTHOR: mesh message % has no version %; refused', NEW.key, NEW.version - 1
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM shared_memory_redaction r WHERE r.memory_id = v_prev_id) THEN
        RAISE EXCEPTION 'E_MESSAGE_REDACTED: version % of mesh message % is redacted; an edit cannot replace a tombstone',
            NEW.version - 1, NEW.key USING ERRCODE = 'check_violation';
    END IF;

    -- Everything but the body is frozen: to, cc, thread_id, in_reply_to, type, ts, from and the rest are those of the
    -- previous version, so an edit is never a send to anyone new.
    IF jsonb_typeof(NEW.value) IS DISTINCT FROM 'object'
            OR jsonb_typeof(NEW.value -> 'body') IS DISTINCT FROM 'string'
            OR (NEW.value - 'body') IS DISTINCT FROM (v_prev_value - 'body') THEN
        RAISE EXCEPTION 'E_INVALID_ARGS: an edit of mesh message % changes the body only; another field differs from version %',
            NEW.key, NEW.version - 1 USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

-- ------------------------------------------------------------------ 5. the signed bytes of an edit
-- 014's agree function, with ONE block changed: the skill_name check admits memory.edit on the edit path.
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
        -- Divergence 059: a mail edit's row carries the memory.edit request its editor signed. Only the edit path,
        -- which marks its own transaction (app.mesh_edit_owner), may store one: memory.write never sets the mark,
        -- so it can still store nothing but a memory.write payload. The mark authorizes nothing by itself; the
        -- single-writer trigger's owner and frozen-field checks are what the database holds (sql_meshychat/029). And only
        -- for what an edit is: a LATER version of a mail key.
        IF NOT (p ->> 'skill_name' = 'memory.edit'
                AND nullif(current_setting('app.mesh_edit_owner', true), '') IS NOT NULL
                AND NEW.namespace = 'shared/mesh-chat' AND NEW.key LIKE 'msg/%' AND NEW.version > 1) THEN
            RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: skill_name: the signed payload is %, not memory.write',
                coalesce(p ->> 'skill_name', 'absent') USING ERRCODE = 'check_violation';
        END IF;
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

COMMENT ON TABLE shared_memory_edit IS
  'Divergence 059: one append-only record per edit of a mail row (a new signed version by a seat of the same owner). '
  'No text. docs/design/edit.md.';
