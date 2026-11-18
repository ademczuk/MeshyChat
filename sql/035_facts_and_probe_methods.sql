-- 035: candidate E - facts/<domain> registry + probe-method blocklist gate.
--
-- Every cross-peer reconcile failure this session was a peer asserting EXTERNAL
-- state from a stale or BUGGED local read. E makes stale-vs-fresh-vs-bugged
-- structurally visible instead of laundering a bad probe as truth.
--
-- THREE axes (mesh protocol v2, rounds 4-6):
--   verified_at  - catches STALE (a sound method over old data; reader re-probes
--                  past a TTL).
--   verified_via - records WHICH method, as a METHOD-ID (not free text, so the
--                  gate is exact - the enforceability cut).
--   probe_methods blocklist - catches the BUGGED PREDICATE: a method that is
--                  false-positive every time, independent of state. A fresh
--                  timestamp cannot catch this; only knowing the predicate is
--                  broken can. The fact-write gate is FAIL-CLOSED: allow-class
--                  method -> accept; block-class -> REFUSE; unknown id ->
--                  accept-but-flag 'unverifiable'.

-- The probe-method registry (the mesh-probe-hygiene-blocklist v2, lifted into
-- the substrate as the artifact the gate consumes).
CREATE TABLE probe_methods (
    id              TEXT PRIMARY KEY,
    class           TEXT NOT NULL CHECK (class IN ('allow','block')),
    -- block rows: `match` is the broken pattern + `reason` why; `replacement_id`
    -- points at the allow-class ground-truth probe. allow rows: `probe` is the
    -- sound command + `note`.
    match_pattern   TEXT,
    probe           TEXT,
    reason          TEXT,
    note            TEXT,
    replacement_id  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- facts/<domain> - a claim about external state, gated on verified_via.
CREATE TABLE facts (
    domain          TEXT NOT NULL,
    fact_key        TEXT NOT NULL,
    value           JSONB NOT NULL,
    -- method-id of the probe that established this fact; validated against
    -- probe_methods at write time. 'unverifiable' trust when the id is unknown.
    verified_via    TEXT NOT NULL,
    trust           TEXT NOT NULL CHECK (trust IN ('verified','unverifiable')),
    verified_at     TIMESTAMPTZ NOT NULL,
    probed_by       UUID NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    -- how long the fact is considered fresh; readers past this re-probe.
    ttl_secs        INTEGER NOT NULL DEFAULT 300,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (domain, fact_key)
);

CREATE INDEX facts_domain_idx ON facts (domain);

CREATE TRIGGER facts_updated_at
    BEFORE UPDATE ON facts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Seed the v2 blocklist registry (3 bugged predicates + 3 ground-truth allows).
INSERT INTO probe_methods (id, class, match_pattern, probe, reason, note, replacement_id) VALUES
  ('git.ref.fetch-branch-then-read-tracking','block',
   'git fetch origin <branch> then read origin/<branch> / merge-base / cat-file', NULL,
   'fetch with a branch refspec updates FETCH_HEAD only, NOT refs/remotes/origin/<branch>; subsequent tracking-ref reads are stale',
   NULL,'git.ref.ls-remote-branch'),
  ('git.ref.ls-remote-branch','allow', NULL,
   'git ls-remote origin refs/heads/<branch>', NULL,
   'queries the remote directly, ignores local refs', NULL),
  ('git.exists.ls-remote-ref-and-echo','block',
   'git ls-remote origin <ref> && echo PRESENT', NULL,
   'ls-remote exits 0 with zero matches (errors only on transport failure); the && fires regardless = false-positive independent of state',
   NULL,'git.exists.ls-remote-heads-grep'),
  ('git.exists.ls-remote-heads-grep','allow', NULL,
   'git ls-remote --heads origin | grep -q .', NULL,
   'enumerates refs that EXIST; non-empty test; fails loud', NULL),
  ('api.count.len-without-status-check','block',
   'len(resp.get(items,[])) without first checking HTTP status / error field', NULL,
   'an error envelope is read as none/zero (empty-vs-error conflation)',
   NULL,'api.count.status-checked-len'),
  ('api.count.status-checked-len','allow', NULL,
   'assert response ok / no error field BEFORE treating [] as none', NULL,
   'an error is unverifiable, not empty', NULL);
