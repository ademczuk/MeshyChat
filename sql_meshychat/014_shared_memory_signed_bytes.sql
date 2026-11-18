-- MeshyChat divergence 039: a shared_memory row's derived columns are held to the bytes its
-- writer signed (B02, phase A1). Design: docs/design/signed-bytes-status.md on
-- fin/design-signed-bytes (9aab90b), sections 2.2 to 2.4, as approved with its section 8.
--
-- WHAT WAS WRONG. The handler verifies a memory.write envelope and then writes `status`,
-- `pre_discipline`, `claimed_status`, `author_label` and `tags` as separate columns. Nothing
-- held them to what was signed. A row whose status contradicts its own signed args read
-- `signature_verified: true` in the reference client (design probe P4), and E2
-- (docs/V1-DESIGN-RULES.md) says a status column that can contradict its signature must not
-- exist. The bytes themselves were never stored: provenance_jsonb keeps the payload as JSONB,
-- which does not round-trip every number (design 1.5).
--
-- WHAT THIS DOES.
--   1. `signed_bytes BYTEA`: the RFC 8785 bytes verify_strict accepted, written by
--      handle_write. NULL only on rows inserted before this migration, or, until A2, by a
--      session that is not this build's: the upstream daemon inserts as the owner role
--      `meshy`, and upstream's meshy-server as `meshy_app` without this build's session
--      marker (section 3 below).
--   2. The derivation rules of design 2.3 as SQL functions, the twin of
--      apps/memoryskills/signed_bytes.py. tests/test_signed_bytes.py runs both over one
--      corpus, so a drift between the two is a failing test before it is a refused write.
--   3. A BEFORE INSERT trigger that REFUSES, never overwrites: the bytes must hash to
--      payload_jcs_sha256, parse to provenance_jsonb->'payload', name a caller equal to
--      created_by_peer_id, kind skill.call and skill memory.write, and every derived column
--      must agree. A refusal is `check_violation` with `E_SIGNED_BYTES_DISAGREE: <field>` or
--      `E_SIGNED_BYTES_REQUIRED`, the convention sql_meshychat/011 set.
--   4. shared_memory_enforce_immutability redefined with signed_bytes in BOTH of its
--      column lists, or the new column would be mutable (design 1.4). tests/
--      test_signed_bytes_schema.py counts every column of the table against both lists of
--      the function the database is running.
--   5. shared_memory_legacy_verdict: an append-only table of verdicts on rows without
--      bytes, keyed by the row's COPIED identity with no foreign key, written only by the
--      classifier job (apps/memoryskills/legacy_verdict.py).
--   6. skill_calls_status_matches_signed_result, a CHECK added NOT VALID: skill_calls keeps
--      its status and result_payload pair, and a new row cannot make them disagree.
--
-- WHAT THE DATABASE CANNOT DO. It has no Ed25519 (pg_proc has none; pgcrypto is digests,
-- HMAC, crypt and PGP), so it cannot verify the signature: handle_write does, before the
-- insert. What the trigger guarantees is narrower and is what E2 asks for: the stored
-- status is a function of the stored bytes, whoever inserts. It does not bound freshness
-- (issued_at against a clock is an application check).
--
-- WHO IT BINDS. Every BEFORE trigger fires for a superuser too; RLS bypass does not skip
-- it. A session with session_replication_role = replica, `ALTER TABLE ... DISABLE TRIGGER`
-- and `pg_restore --disable-triggers` do skip it. A full pg_dump/pg_restore creates triggers
-- after the data, so it never fires them; a data-only restore into this schema fires it on
-- every row, and admits every NULL-bytes row (a restore is not this build's session).
--
-- ORDER. BEFORE INSERT triggers fire in name order: ..._mesh_single_writer_trg (011),
-- ..._root_parent_trg (064), then ..._signed_bytes_agree_trg. None assigns to NEW, so the
-- order changes nothing this trigger sees.
--
-- COST. Adding a nullable column with no default does not rewrite the table. The trigger
-- costs one SHA-256 and one JSONB parse of the payload per insert; divergence 039 records
-- the bench at a 64 KiB mesh body and a 2.5 MB value.
--
-- Idempotent: IF NOT EXISTS, OR REPLACE, DROP ... IF EXISTS. The down script is
-- sql_meshychat/down/014_shared_memory_signed_bytes_down.sql, outside the migrate set.
--
-- DEPLOY ORDER. Forward: apply this, then roll the code. Pre-A1 code opens its sessions
-- without apps.core.rls's marker, so on this schema its writes still land (without bytes,
-- classified later like upstream's); A1 code on the pre-A1 schema fails every write, its
-- INSERT naming a column that is not there. Back: roll the code back, then the down script.

-- A BOUNDED WAIT FOR ITS LOCKS (adversarial review of fin/b02-a1, finding 5). The ALTERs
-- below take ACCESS EXCLUSIVE on shared_memory and skill_calls. Queued behind a long
-- reader, that request would hold every later reader and writer of both tables behind it
-- for as long as the reader runs. Give up after 5 s instead, as upstream's migrations do
-- (sql/100:4, sql/105:82), and retry when the table is quiet. LOCAL, unlike upstream's
-- session-level SET: the runner applies each file in its own transaction, and the timeout
-- ends with it rather than following the migrator's session into the next file.
SET LOCAL lock_timeout = '5s';

-- ------------------------------------------------------------------ 1. the column
ALTER TABLE shared_memory ADD COLUMN IF NOT EXISTS signed_bytes BYTEA;

COMMENT ON COLUMN shared_memory.signed_bytes IS
  'The RFC 8785 bytes of provenance_jsonb->payload that verify_strict accepted at write time '
  '(divergence 039). NULL on rows inserted before sql_meshychat/014 and, until A2, on rows a '
  'session other than this build''s inserts (the upstream daemon, upstream meshy-server); '
  'those are classified in shared_memory_legacy_verdict.';

-- ------------------------------------------------------------------ 2. the rules
-- STABLE, which is what it is: convert_from is STABLE (pg_proc.provolatile = 's'). A
-- trigger does not need IMMUTABLE; only a generated column or an index expression would, and
-- design Q2 rejects both.
CREATE OR REPLACE FUNCTION sm_signed_payload(b bytea) RETURNS jsonb
    LANGUAGE sql STABLE STRICT
    AS $$ SELECT convert_from(b, 'UTF8')::jsonb $$;

-- args.pre_discipline: absent or null -> false; a JSON boolean -> its value; else refused.
-- handle_write used Python truthiness, so the STRING "false" made a custody row (design G3).
CREATE OR REPLACE FUNCTION sm_derive_pre_discipline(p jsonb) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v jsonb := p -> 'args' -> 'pre_discipline';
BEGIN
    IF v IS NULL OR jsonb_typeof(v) = 'null' THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(v) = 'boolean' THEN
        RETURN v::boolean;
    END IF;
    RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: pre_discipline: the signed arg is a JSON %, not a boolean',
        jsonb_typeof(v) USING ERRCODE = 'check_violation';
END;
$$;

-- args.status: a JSON string, or NULL when absent or null; anything else refused. A custody
-- (pre_discipline) write signs no status. Then the shared/wiki default: NULL, not custody,
-- namespace 'shared/wiki' -> 'OPEN' (handle_write's own order).
CREATE OR REPLACE FUNCTION sm_derive_status(p jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    s  jsonb := p -> 'args' -> 'status';
    st text;
BEGIN
    IF s IS NULL OR jsonb_typeof(s) = 'null' THEN
        st := NULL;
    ELSIF jsonb_typeof(s) = 'string' THEN
        st := s #>> '{}';
    ELSE
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: status: the signed arg is a JSON %, not a string',
            jsonb_typeof(s) USING ERRCODE = 'check_violation';
    END IF;
    IF sm_derive_pre_discipline(p) THEN
        IF st IS NOT NULL THEN
            RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: status: a pre_discipline custody write signs no status (it carries claimed_status)'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NULL;
    END IF;
    IF st IS NULL AND p -> 'args' -> 'namespace' = to_jsonb('shared/wiki'::text) THEN
        RETURN 'OPEN';
    END IF;
    RETURN st;
END;
$$;

-- args.claimed_status, for a custody write only: a JSON string, NULL when absent or null,
-- anything else refused. Not custody: NULL whatever was signed.
CREATE OR REPLACE FUNCTION sm_derive_claimed_status(p jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    c jsonb := p -> 'args' -> 'claimed_status';
BEGIN
    IF NOT sm_derive_pre_discipline(p) THEN
        RETURN NULL;
    END IF;
    IF c IS NULL OR jsonb_typeof(c) = 'null' THEN
        RETURN NULL;
    END IF;
    IF jsonb_typeof(c) = 'string' THEN
        RETURN c #>> '{}';
    END IF;
    RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: claimed_status: the signed arg is a JSON %, not a string',
        jsonb_typeof(c) USING ERRCODE = 'check_violation';
END;
$$;

-- args.value when the key is present, else args.body. Compared with JSONB equality, which is
-- numeric equality ('1.0'::jsonb = '1'::jsonb), the identification RFC 8785 makes.
CREATE OR REPLACE FUNCTION sm_signed_value(p jsonb) RETURNS jsonb
    LANGUAGE sql IMMUTABLE
    AS $$ SELECT CASE WHEN (p -> 'args') ? 'value' THEN p -> 'args' -> 'value'
                      ELSE p -> 'args' -> 'body' END $$;

-- The author the writer SIGNED: args.author when a non-empty string, else args.author_label
-- when a non-empty string, else NULL (the server stamps a roster label or peer-<id8>, which
-- the database cannot reproduce: the roster is not in it). A non-string spelling is refused.
CREATE OR REPLACE FUNCTION sm_derive_author_label(p jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    au jsonb := p -> 'args' -> 'author';
    al jsonb := p -> 'args' -> 'author_label';
BEGIN
    IF au IS NOT NULL AND jsonb_typeof(au) NOT IN ('string', 'null') THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: author_label: args.author is a JSON %, not a string',
            jsonb_typeof(au) USING ERRCODE = 'check_violation';
    END IF;
    IF al IS NOT NULL AND jsonb_typeof(al) NOT IN ('string', 'null') THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: author_label: args.author_label is a JSON %, not a string',
            jsonb_typeof(al) USING ERRCODE = 'check_violation';
    END IF;
    IF jsonb_typeof(au) = 'string' AND au #>> '{}' <> '' THEN
        RETURN au #>> '{}';
    END IF;
    IF jsonb_typeof(al) = 'string' AND al #>> '{}' <> '' THEN
        RETURN al #>> '{}';
    END IF;
    RETURN NULL;
END;
$$;

-- args.tags: absent or null -> '{}'; a list of strings -> those strings, in order; a list
-- with any other element, or any other JSON type, refused. A comma STRING returns NULL: it
-- is normalised by Python's str.strip, which strips Unicode whitespace SQL's btrim does not,
-- so this trigger does not check it (design 2.3 leaves it to an offline verifier). On a row
-- WITHOUT bytes the legacy classifier compares it; on a row with bytes nothing does yet,
-- because the re-verifier for those rows is not built, and a reader derives it from the
-- bytes. So `bytes_bound` does not cover comma-string tags (README, divergence 039).
CREATE OR REPLACE FUNCTION sm_derive_tags(p jsonb) RETURNS text[]
    LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    t jsonb := p -> 'args' -> 'tags';
BEGIN
    IF t IS NULL OR jsonb_typeof(t) = 'null' THEN
        RETURN '{}'::text[];
    END IF;
    IF jsonb_typeof(t) = 'string' THEN
        RETURN NULL;
    END IF;
    IF jsonb_typeof(t) <> 'array' THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: tags: the signed arg is a JSON %, not a list or a string',
            jsonb_typeof(t) USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(t) AS e(x) WHERE jsonb_typeof(e.x) <> 'string') THEN
        RAISE EXCEPTION 'E_SIGNED_BYTES_DISAGREE: tags: a signed list element is not a string'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN ARRAY(SELECT e.x FROM jsonb_array_elements_text(t) WITH ORDINALITY AS e(x, n)
                 ORDER BY e.n);
END;
$$;

-- ------------------------------------------------------------------ 3. the agree-trigger
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

DROP TRIGGER IF EXISTS shared_memory_signed_bytes_agree_trg ON shared_memory;
CREATE TRIGGER shared_memory_signed_bytes_agree_trg
    BEFORE INSERT ON shared_memory
    FOR EACH ROW
    EXECUTE FUNCTION shared_memory_signed_bytes_agree();

-- ------------------------------------------------------------------ 4. immutability
-- sql/063's function, verbatim except for signed_bytes in BOTH column lists: the
-- high_churn parent-unlink exemption requires it unchanged, and the general rule refuses
-- any change to it, NULL to a value included (a backfill in place is design option C,
-- which Q1 rejects). The trigger binding (shared_memory_immutability) is unchanged.
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

-- ------------------------------------------------------------------ 5. legacy verdicts
-- A row with NULL signed_bytes (written before this migration, or since by the upstream
-- daemon or meshy-server) is CLASSIFIED, never rewritten (design 3.3, 3.4; Q1, Q6, Q7, Q14). The
-- job is apps/memoryskills/legacy_verdict.py; one row per (ledger row, classifier version).
--
-- NO FOREIGN KEY, and so no cascade. An ephemeral namespace's write DELETEs the key's chain
-- and memory.prune DELETEs non-head versions in a high_churn namespace; a cascade would
-- erase a verdict, a contradiction included, together with its evidence. The row's
-- identity is COPIED instead, so a count taken later still sees it.
--
-- WHAT THE TRIGGER BELOW PREVENTS, AND WHAT IT DOES NOT. It refuses a verdict whose copied
-- identity is not a live row's, and `verified_bytes` that do not hash to that row's
-- payload_jcs_sha256 or do not parse to its provenance payload: fabricated BYTES would
-- need a second preimage. It does NOT prevent a fabricated VERDICT: the bytes that hash to
-- the stored digest can be computed by anyone from the stored payload, so a writer of this
-- table could mark a failing row `verified`. A verdict is what the job concluded, never
-- proof; the proof is a reader verifying the bytes itself (design section 8, finding 1).
--
-- APPEND-ONLY FOR meshy_app. FORCE RLS with 008's split: SELECT under app.server_internal,
-- INSERT only with app.projection_writer as well (the job's context, divergence 022), no
-- UPDATE or DELETE policy at all, and UPDATE, DELETE and TRUNCATE revoked from meshy_app,
-- the mechanism sql/062 and sql/111 record for invocation_frames.
CREATE TABLE IF NOT EXISTS shared_memory_legacy_verdict (
    row_id                   UUID        NOT NULL,
    row_namespace            TEXT        NOT NULL,
    row_key                  TEXT        NOT NULL,
    row_version              INTEGER     NOT NULL,
    row_created_at           TIMESTAMPTZ NOT NULL,
    row_payload_jcs_sha256   TEXT        NOT NULL CHECK (row_payload_jcs_sha256 ~ '^[0-9a-f]{64}$'),
    classifier_version       TEXT        NOT NULL CHECK (classifier_version <> ''),
    verdict                  TEXT        NOT NULL
                             CHECK (verdict IN ('verified', 'hash_bound_signature_fails',
                                                'no_hash_match', 'no_envelope')),
    -- The loader and canonicaliser whose bytes matched payload_jcs_sha256 (design 3.2:
    -- upstream hashed json.dumps(sort_keys=True), not RFC 8785, and JSONB keeps a number's
    -- value but not its type, so both loaders are tried).
    loader                   TEXT        CHECK (loader IN ('json_default', 'ijson_float')),
    canonicalizer            TEXT        CHECK (canonicalizer IN ('rfc8785', 'sort_keys')),
    verified_bytes           BYTEA,
    verified_kid             TEXT,
    -- peer_keys.revoked_at earlier than the row's created_at: a signature no verifying
    -- handler would have accepted, counted apart even when it holds (design 3.3 step 7).
    key_revoked_before_write BOOLEAN,
    -- For hash_bound_signature_fails under the writer's key: whether libsodium's lenient
    -- verify accepts what verify_strict refused (a small-order key or a malleable scalar).
    lenient_accepts          BOOLEAN,
    -- kind, skill_name, caller_peer_id, namespace, key, value: the envelope verifies but
    -- describes some other row, so it is not evidence for this one.
    binding_failures         TEXT[]      NOT NULL DEFAULT '{}',
    -- A field whose arg the writer SIGNED differs from its column (the analogue of the 197).
    disagreeing_signed       TEXT[]      NOT NULL DEFAULT '{}',
    -- A field the server DEFAULTED or normalised differs (the wiki OPEN, comma tags, or an
    -- arg type design 2.3 refuses and an older daemon rule read): rule drift, not a lie.
    disagreeing_defaulted    TEXT[]      NOT NULL DEFAULT '{}',
    checked_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (row_id, classifier_version),
    CONSTRAINT shared_memory_legacy_verdict_bytes_only_when_verified
        CHECK ((verdict = 'verified') = (verified_bytes IS NOT NULL)),
    CONSTRAINT shared_memory_legacy_verdict_match_recorded
        CHECK ((verdict IN ('verified', 'hash_bound_signature_fails'))
               = (loader IS NOT NULL AND canonicalizer IS NOT NULL)),
    CONSTRAINT shared_memory_legacy_verdict_bytes_hash_to_the_row
        CHECK (verified_bytes IS NULL
               OR encode(sha256(verified_bytes), 'hex') = row_payload_jcs_sha256)
);

CREATE INDEX IF NOT EXISTS shared_memory_legacy_verdict_version_idx
    ON shared_memory_legacy_verdict (classifier_version, verdict);

CREATE OR REPLACE FUNCTION shared_memory_legacy_verdict_bind() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    r record;
    p jsonb;
BEGIN
    SELECT namespace, key, version, created_at, payload_jcs_sha256, provenance_jsonb
      INTO r FROM shared_memory WHERE id = NEW.row_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'E_VERDICT_UNBOUND: no shared_memory row % is visible to this insert',
            NEW.row_id USING ERRCODE = 'check_violation';
    END IF;
    IF (r.namespace, r.key, r.version, r.created_at, r.payload_jcs_sha256)
            IS DISTINCT FROM (NEW.row_namespace, NEW.row_key, NEW.row_version,
                              NEW.row_created_at, NEW.row_payload_jcs_sha256) THEN
        RAISE EXCEPTION 'E_VERDICT_UNBOUND: the copied identity is not that of row %',
            NEW.row_id USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.verified_bytes IS NOT NULL THEN
        IF encode(sha256(NEW.verified_bytes), 'hex') IS DISTINCT FROM r.payload_jcs_sha256 THEN
            RAISE EXCEPTION 'E_VERDICT_BYTES: verified_bytes do not hash to the payload_jcs_sha256 of row %',
                NEW.row_id USING ERRCODE = 'check_violation';
        END IF;
        BEGIN
            p := convert_from(NEW.verified_bytes, 'UTF8')::jsonb;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'E_VERDICT_BYTES: verified_bytes are not UTF-8 JSON (%)', SQLERRM
                USING ERRCODE = 'check_violation';
        END;
        IF p IS DISTINCT FROM r.provenance_jsonb -> 'payload' THEN
            RAISE EXCEPTION 'E_VERDICT_BYTES: verified_bytes are not the provenance payload of row %',
                NEW.row_id USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shared_memory_legacy_verdict_bind_trg ON shared_memory_legacy_verdict;
CREATE TRIGGER shared_memory_legacy_verdict_bind_trg
    BEFORE INSERT ON shared_memory_legacy_verdict
    FOR EACH ROW
    EXECUTE FUNCTION shared_memory_legacy_verdict_bind();

ALTER TABLE shared_memory_legacy_verdict ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_memory_legacy_verdict FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS shared_memory_legacy_verdict_read ON shared_memory_legacy_verdict;
CREATE POLICY shared_memory_legacy_verdict_read ON shared_memory_legacy_verdict
    FOR SELECT
    USING (current_setting('app.server_internal', true) = '1');
DROP POLICY IF EXISTS shared_memory_legacy_verdict_insert ON shared_memory_legacy_verdict;
CREATE POLICY shared_memory_legacy_verdict_insert ON shared_memory_legacy_verdict
    FOR INSERT
    WITH CHECK (current_setting('app.server_internal', true) = '1'
                AND current_setting('app.projection_writer', true) = '1');
GRANT SELECT, INSERT ON shared_memory_legacy_verdict TO meshy_app;
REVOKE UPDATE, DELETE, TRUNCATE ON shared_memory_legacy_verdict FROM meshy_app;

-- ------------------------------------------------------------------ 6. skill_calls keeps the pair
-- Design 2.6 and Q8. skill_calls keeps `status` and `result_payload` as two columns (the
-- brief for B02 derives shared_memory's status only), so the #922 class (a row whose status
-- column says completed while the result the server signed says failed; 197 such rows in
-- production, docs/V1-DESIGN-RULES.md E2) is held here by a CHECK instead.
--
-- NOT VALID: enforced for every new and every UPDATEd row, and not checked against the rows
-- already there, which stay readable. An UPDATE of a violating historical row is refused;
-- this build updates no terminal row (the writers: apps/calls/route.py's one terminal
-- UPDATE writes status and result_payload from one variable, and janitor.py, reads.py and
-- route.py's other UPDATEs touch in-flight rows, whose result_payload is NULL). Upstream's
-- writers at the production sha satisfy it too (design 2.6: retention nulls result_payload
-- in the same UPDATE; finish_call_atomic writes the pair since #916).
--
-- WHAT IT CANNOT SEE (design section 8, finding 7): a row finished before upstream's
-- migration 107, or whose result_payload retention has nulled, passes whatever its status.
-- tests/test_signed_bytes_schema.py re-runs the statement below over a seeded violator.
ALTER TABLE skill_calls DROP CONSTRAINT IF EXISTS skill_calls_status_matches_signed_result;
ALTER TABLE skill_calls ADD CONSTRAINT skill_calls_status_matches_signed_result
    CHECK (result_payload IS NULL OR status = result_payload ->> 'status') NOT VALID;
