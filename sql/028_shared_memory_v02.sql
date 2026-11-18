-- 028: shared_memory v0.2 — signed, append-only, grant-gated network memory.
--
-- Audit row: shared_memory_primitive.
-- HEAD before this migration: 027_skills_mode_lesson.sql. 028 is the next
-- contiguous up-only sqlx migration (sqlx 0.8.6, one implicit txn per file —
-- no '-- no-transaction' needed: plain B-tree / partial-unique indexes only,
-- NO pgvector / NO HNSW / NO CONCURRENTLY).
--
-- EXECUTION LOCUS: meshy-server runs ZERO skills. The HQB memory-service daemon
-- (Python, same trust domain as Postgres) registers the memory.* skills, the
-- server routes skill.call envelopes to it over WS, and the daemon does the DB
-- work below. The daemon connects as the `meshy` SUPERUSER (tools/memory/cli.py:45),
-- which BYPASSES RLS even under FORCE — therefore authorization for MUST-1 /
-- MUST-2 / MUST-4 lives in daemon APPLICATION code. The RLS + FK + trigger
-- guards here are defense-in-depth (and become load-bearing only if the daemon
-- is ever switched to the non-superuser meshy_app role).
--
-- Locked design implemented here:
--   MUST-1 (namespace-claim squat): shared_memory_namespace claim table; every
--          shared_memory / shared_memory_grant row FKs its namespace, so a write
--          to an UNCLAIMED namespace is rejected at the DB layer too (FKs are NOT
--          bypassed by the superuser daemon). Who-may-claim (BOOTSTRAP_AUTHORITY)
--          and who-may-grant (namespace admin holder) are enforced in the daemon.
--   MUST-2 (owner distinctness): created_by_owner_id is NOT NULL and is derived
--          live from peers.owner_id by the daemon. The fix-author != close-author
--          discipline rule compares this owner column across a key's version chain.
--   MUST-3/MUST-7 (M4 backfill): pre_discipline BOOLEAN + claimed_status TEXT
--          carry the unsigned v0.1 matrix rows re-anchored by the migrating peer.
--          Backfill is performed by the daemon (it must produce signed envelopes),
--          NOT by this SQL file. New keys may not start at PROPOSED_FIX/FIXED and
--          no row may transition OFF pre_discipline — those are daemon rules.
--   MUST-4 (re-verify): provenance_jsonb stores the verbatim signed envelope the
--          daemon independently re-verified; payload_jcs_sha256 + provenance_call_id
--          give DB-level replay dedup that mirrors the daemon's own dedup.
--   MUST-8 (no Gemini key / no embedding): v0.2 ships NO embedding (pgvector
--          deferred). embed_policy defaults 'forbidden'; there is NO embedding /
--          vector column and the daemon must NOT read or carry any Gemini key.
--
-- Replay: the live server keys idempotency on the UNSIGNED transport body.call_id
-- (invoke.rs). This table additionally enforces UNIQUE on the SIGNED
-- payload_jcs_sha256 AND on the SIGNED provenance payload.call_id, so a replayed
-- signed envelope cannot create a second row even via the superuser daemon.
--
-- Status vocabulary: the COMPLETE live v0.1 set actually written by the python
-- matrix tools is exactly {OPEN, PARTIAL, CONFIRMED, REFUTED, MITIGATED,
-- DURABLE_PATTERN}. The four discipline states are NET-NEW in v0.2.
-- SPELLING FLAG: the locked note spelled the discipline states hyphenated
-- (PROPOSED-FIX / PRE-DISCIPLINE). The only live multiword value uses an
-- UNDERSCORE (DURABLE_PATTERN), so for consistency the CHECK below uses
-- PROPOSED_FIX / PRE_DISCIPLINE / WONTFIX. The daemon MUST emit the same
-- underscored spellings.

-- ---------------------------------------------------------------------------
-- 1. shared_memory_namespace — the claim ledger (MUST-1).
--    A namespace must be claimed here before anything can be written to it or
--    granted on it. ON DELETE NO ACTION preserves the claim ledger (the
--    skill_calls ledger precedent, 006:7-10), deliberately diverging from the
--    CASCADE/SET NULL used by meshy_memory (012).
-- ---------------------------------------------------------------------------
CREATE TABLE shared_memory_namespace (
    namespace        TEXT PRIMARY KEY,
    owner_peer_id    UUID NOT NULL REFERENCES peers(id) ON DELETE NO ACTION,
    owner_owner_id   UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE NO ACTION,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX shared_memory_namespace_owner_idx
    ON shared_memory_namespace (owner_owner_id);

-- ---------------------------------------------------------------------------
-- 2. shared_memory — append-only signed version chain.
--    Rows are IMMUTABLE; the ONLY legal UPDATE is head retirement
--    (is_head TRUE -> FALSE), enforced by the trigger below.
-- ---------------------------------------------------------------------------
CREATE TABLE shared_memory (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identity within the chain.
    namespace           TEXT NOT NULL
                        REFERENCES shared_memory_namespace(namespace) ON DELETE NO ACTION,
    key                 TEXT NOT NULL,
    version             INTEGER NOT NULL CHECK (version >= 1),
    parent_id           UUID REFERENCES shared_memory(id) ON DELETE NO ACTION,
    is_head             BOOLEAN NOT NULL DEFAULT TRUE,

    -- Payload + discipline state.
    value               JSONB NOT NULL,
    status              TEXT
                        CHECK (
                            status IS NULL OR status IN (
                                -- live v0.1 vocabulary
                                'OPEN','PARTIAL','CONFIRMED','REFUTED',
                                'MITIGATED','DURABLE_PATTERN',
                                -- net-new v0.2 discipline states
                                'PROPOSED_FIX','FIXED','PRE_DISCIPLINE','WONTFIX'
                            )
                        ),
    pre_discipline      BOOLEAN NOT NULL DEFAULT FALSE,   -- MUST-3 backfill marker
    claimed_status      TEXT,                             -- MUST-3 v0.1 status carried forward

    -- Free-form labels consumed by memory.write/query/list in the daemon.
    -- tags is "contains-all" filtered (@>) and author_label is an exact filter.
    tags                TEXT[] NOT NULL DEFAULT '{}',
    author_label        TEXT,

    -- No embedding in v0.2 (MUST-8): pgvector deferred, key must not be carried.
    embed_policy        TEXT NOT NULL DEFAULT 'forbidden'
                        CHECK (embed_policy IN ('forbidden','allowed')),

    -- Provenance: the verbatim re-verified signed envelope
    -- {payload, signature, signing_kid} plus scalars
    -- {caller_owner_id, payload_jcs_sha256, public_key_b64url,
    --  public_key_sha256, verified_at, pre_discipline}. NO 'nonce' key —
    -- skill.call envelopes have none.
    provenance_jsonb    JSONB NOT NULL,

    -- Promoted provenance scalars used for DB-level replay dedup.
    payload_jcs_sha256  TEXT NOT NULL UNIQUE
                        CHECK (payload_jcs_sha256 ~ '^[0-9a-f]{64}$'),
    -- Generated from the signed envelope so it can never drift from provenance;
    -- NOT NULL rejects any envelope missing payload.call_id.
    provenance_call_id  TEXT
                        GENERATED ALWAYS AS (provenance_jsonb -> 'payload' ->> 'call_id') STORED
                        NOT NULL,

    -- Authorship ledger (MUST-2). Both NOT NULL + ON DELETE NO ACTION to
    -- preserve the immutable ledger (skill_calls precedent). owner is derived
    -- live from peers.owner_id by the daemon; the owner FK deliberately skips
    -- the CASCADE convention used elsewhere.
    created_by_peer_id  UUID NOT NULL REFERENCES peers(id) ON DELETE NO ACTION,
    created_by_owner_id UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE NO ACTION,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One row per (namespace, key, version) — the V02_PLAN's broken
    -- UNIQUE(namespace,key) is replaced by this + the partial-unique head index.
    CONSTRAINT shared_memory_nkv_unique UNIQUE (namespace, key, version),

    -- Chain invariant: version 1 is the root (no parent); every later version
    -- supersedes a parent.
    CONSTRAINT shared_memory_root_parent_chk CHECK (
        (version = 1 AND parent_id IS NULL)
        OR (version > 1 AND parent_id IS NOT NULL)
    )
);

-- Exactly one HEAD per (namespace, key).
CREATE UNIQUE INDEX shared_memory_head_unique
    ON shared_memory (namespace, key)
    WHERE is_head;

-- Replay dedup on the SIGNED payload.call_id (mirrors the daemon's own dedup).
CREATE UNIQUE INDEX shared_memory_call_id_unique
    ON shared_memory (provenance_call_id);

-- MUST-2 same-owner discipline lookups + general owner-scoped reads.
CREATE INDEX shared_memory_owner_idx
    ON shared_memory (created_by_owner_id);

-- Status filtering over current heads (e.g. list OPEN findings in a namespace).
CREATE INDEX shared_memory_head_status_idx
    ON shared_memory (namespace, status)
    WHERE is_head;

-- Tag "contains-all" (@>) filtering over current heads (memory.query tags filter).
CREATE INDEX shared_memory_head_tags_gin
    ON shared_memory USING GIN (tags)
    WHERE is_head;

-- ---------------------------------------------------------------------------
-- 3. shared_memory_grant — per-namespace permission grants (MUST-1 layer 2).
--    Ordered permission read < write < admin. Only a namespace's admin-grant
--    holder may grant others (enforced in the daemon). ON DELETE NO ACTION
--    preserves the grant ledger.
-- ---------------------------------------------------------------------------
CREATE TABLE shared_memory_grant (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace           TEXT NOT NULL
                        REFERENCES shared_memory_namespace(namespace) ON DELETE NO ACTION,
    grantee_peer_id     UUID NOT NULL REFERENCES peers(id) ON DELETE NO ACTION,
    -- Owner of the grantee, derived LIVE from peers.owner_id by the daemon (MUST-2
    -- audit trail). NOT NULL: every peer has a non-null owner (001:17).
    grantee_owner_id    UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE NO ACTION,
    permission          TEXT NOT NULL CHECK (permission IN ('read','write','admin')),
    granted_by_peer_id  UUID NOT NULL REFERENCES peers(id) ON DELETE NO ACTION,
    granted_by_owner_id UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE NO ACTION,
    revoked             BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at          TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- At most ONE non-revoked grant per (namespace, grantee).
CREATE UNIQUE INDEX shared_memory_grant_active_unique
    ON shared_memory_grant (namespace, grantee_peer_id)
    WHERE revoked = FALSE;

-- Default-deny grant lookups: "what active grants does this peer hold".
CREATE INDEX shared_memory_grant_grantee_idx
    ON shared_memory_grant (grantee_peer_id)
    WHERE revoked = FALSE;

-- Grants are mutable (revoke flips revoked/revoked_at); keep updated_at fresh by
-- reusing set_updated_at() (001:34). NOTE: no immutability trigger here — unlike
-- shared_memory, grant rows are intentionally updatable.
CREATE TRIGGER shared_memory_grant_set_updated_at
    BEFORE UPDATE ON shared_memory_grant
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 4. Immutability trigger — append-only ledger; only head retirement allowed.
--    set_updated_at() already exists (001:34) and is REUSED for updated_at.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION shared_memory_enforce_immutability() RETURNS TRIGGER AS $$
BEGIN
    -- Every content/identity column is immutable. (provenance_call_id is a
    -- generated column and cannot be assigned directly; it follows
    -- provenance_jsonb, which is checked here.)
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

    -- The only legal mutation is retiring a head. A retired head must not be
    -- resurrected.
    IF OLD.is_head = FALSE AND NEW.is_head = TRUE THEN
        RAISE EXCEPTION 'shared_memory head cannot be resurrected (is_head FALSE->TRUE forbidden)';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER shared_memory_immutability
    BEFORE UPDATE ON shared_memory
    FOR EACH ROW EXECUTE FUNCTION shared_memory_enforce_immutability();

CREATE TRIGGER shared_memory_set_updated_at
    BEFORE UPDATE ON shared_memory
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 5. RLS — default-deny via the server-internal GUC pattern (migration 016).
--    GUC name app.server_internal = '1' is set transaction-locally by
--    meshy_db::with_server_internal (peer_context.rs). With ENABLE + FORCE and
--    NO permissive blanket policy, any role without BYPASSRLS that has not set
--    the GUC is denied on ALL ops (read/query/list/write/grant). External peers
--    have no SQL session and cannot set it. The superuser daemon bypasses RLS
--    entirely, so authorization for it lives in daemon application code.
-- ---------------------------------------------------------------------------
ALTER TABLE shared_memory_namespace ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_memory_namespace FORCE ROW LEVEL SECURITY;
CREATE POLICY shared_memory_namespace_server_internal ON shared_memory_namespace
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER TABLE shared_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_memory FORCE ROW LEVEL SECURITY;
CREATE POLICY shared_memory_server_internal ON shared_memory
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');

ALTER TABLE shared_memory_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_memory_grant FORCE ROW LEVEL SECURITY;
CREATE POLICY shared_memory_grant_server_internal ON shared_memory_grant
    FOR ALL
    USING (current_setting('app.server_internal', true) = '1')
    WITH CHECK (current_setting('app.server_internal', true) = '1');
