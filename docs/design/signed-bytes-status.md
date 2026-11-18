# Design: a shared_memory row's status derived from its stored signed bytes (B02)

> RECORD, landed 2026-09-29 by operator ruling. This is the design as of 2026-09-24 (base 0b6767f). It was approved and built as divergences 039 and 051 (the project's design log (not part of this release)); read those for what the code does today. The status line and the decision requested below are as of 2026-09-24.
>
> Role names: where the text below says `meshy_app` and `meshy` for database roles, read the serving role `meshychat_app` and the owner login `meshychat_owner` (`sql_meshychat/046`). The event source value `meshy` is data.

**Status: DRAFT FOR OPERATOR APPROVAL, revised after an adversarial critique (section 8).
No code, no migration, no divergence number.** Base: `origin/main` 0b6767f. Rules A3 and E2
(`docs/V1-DESIGN-RULES.md:32-36`, `lines 204-208`). Roadmap item 2
(an internal project document (not part of this release)). The implementation would take the next free
`sql_meshychat` number and the next free divergence number when it is built (035 and 036 are
reserved for other work); this document names them `NNN`.

**How to read the claims.** Every claim cites `file:line` at 0b6767f. Three labels:

* **M-read**: I read it in the code or document cited.
* **M-run**: I ran it. The probes are listed in section 7; they ran on a throwaway database
  that the test harness created and dropped, and on SELECT-only casts. No production row, no
  standby, no `meshy_nodetest`.
* **I**: inferred. Each one names the rival explanation the evidence does not rule out.

---

## 0. The decision on record, and what this document asks for

The map says: "the envelope is verified on the write path now, but status is still a separate
column. **Decision needed before release:** store the canonical signed bytes and derive status
from them" (an internal project document (not part of this release)). E2 says: "Store the canonical signed payload
verbatim and DERIVE status from it. A status column that can contradict the signature it
accompanies must not exist" (`docs/V1-DESIGN-RULES.md:207-208`). The brief for this document
scopes it: shared_memory derives; `skill_calls` keeps `result_payload` and `status` as a pair.

**A scope fact the operator should hear before approving (M-read).** The 197-row figure behind
E2 is a `skill_calls` figure, not a shared_memory one: "197 production rows carry a valid
signature over `status: failed` while their own status column says `completed`"
(`docs/V1-DESIGN-RULES.md:204-205`). The shared_memory equivalent (rows whose stored status
disagrees with what their envelope signed) has never been counted. Section 3.5 lists the
counts-only measurement that would give it. Section 2.6 proposes a cheap guard for the
`skill_calls` half, because deriving shared_memory's status leaves the incident's own table
untouched.

**What approval covers:** the answers to the open questions in section 5 (Q1 to Q11 from the
first draft, Q12 to Q14 added by the critique). Each has a recommendation. Nothing is built until
they are answered. B02 is the row in `fin/status`'s an internal project document (not part of this release) (an internal project document (not part of this release) at 7e1e2ba);
that branch is not merged, so `origin/main` has no B02 row to cite.

---

## 1. What is signed today

### 1.1 The write paths into shared_memory

Every row reaches `shared_memory` through `apps/memoryskills/handlers.handle_write`, whose one
INSERT is `handlers.py:924-936` (M-read; a grep for `INSERT INTO shared_memory` across
`apps/`, `sql/`, `sql_meshychat/` and `tools/` finds that INSERT plus one tool, below). Four
routes lead to it:

| path | entry | envelope checks before the handler | server-signed result |
|---|---|---|---|
| W1 HTTP invoke | `POST /v1/agents/{peer}/skills/memory.write/invoke`, body parsed with `json.loads` (`apps/calls/views.py:170`) | all seven of `verify_payload` (`apps/calls/envelope.py:157-238`, step 7 at `lines 224-235`), then the rebind of call_id, target and skill (`apps/calls/pipeline.py:211-219`) | yes (`apps/calls/route.py:156-171`) |
| W2 A2A `message:send` | `apps/a2a/views.py:171`, then `pipeline.start_call` (`lines 511-525`) | same as W1 | yes |
| W3 board | `POST /v1/board/acts` (`apps/board/views.py:211`) -> `declare.declare` -> `handlers.dispatch` (`apps/board/declare.py:306`) | none of `verify_payload`'s checks 1-4; only `_bind_envelope_to_the_call` (`handlers.py:1400-1435`) | no `skill_calls` row, no result signature |
| W4 in-process | `python -m apps.board declare` (`declare_in_process`, `apps/board/declare.py:310`) | none of checks 1-4 either, but the payload is built in-process by `signed_envelope` (`apps/board/declare.py:249-277`, called at `lines 327`) with `v`, `kind`, `caller_peer_id` and `issued_at` always set, so nothing outside the process can omit them | none |

Inside the handler, all four paths verify the signature again: `verify_strict` over
`primitives.jcs(payload)` under the caller's key for the kid (`handlers.py:787`,
`lines 1447-1467`). So on every path the stored row's payload is one the signature verified over.

Two writers do not use the handler (M-read): `tools/related_calibration.py:129-135` seeds a
throwaway calibration database with rows whose `provenance_jsonb` is
`{"payload": {"call_id": ...}}` and no signature, and 14 test files insert shared_memory rows
directly (grep count over `tests/*.py`), plus the shared fixture `tests/fixtures/meshsem_db.py:148`
that the meshsem tests seed through. The database accepts both today. Any invariant in
section 2 refuses them, so both need a signed-row helper (section 4.4).

### 1.2 The client envelope (skill.call)

The bytes a node signs are the RFC 8785 serialisation of the payload object: Ed25519 over
`jcs(payload)`, no prefix, domain separation by the `kind` field inside it
(`apps/calls/envelope.py:17-27`). The `tools/meshy_node` client builds the same bytes with the same
library (`tools/meshy_node/wire.py:35`). The payload fields are `v, kind, call_id,
caller_peer_id, target_peer_id, skill_name, skill_version, args, args_attachments,
issued_at[, nonce]` (`tests/test_call_routing.py:196-208`; `apps/board/declare.py:257-268`
builds the same shape). For memory.write,
`args` carries `namespace, key, value` (or `body`), and optionally `status, tags,
author | author_label, pre_discipline, claimed_status, expected_head_version`
(`handlers.py:717-752`).

Only RFC 8785 bytes verify here. Object keys outside the Basic Multilingual Plane, integers
beyond 2^53-1, and non-integral floats in [1e-6, 1e-4) need explicit client coverage because
their serialisations can differ. This design stores only bytes that verified.

The server never sees the bytes the client signed. It sees the JSON body, parses it
(`views.py:170`), and re-canonicalises the parsed object (`envelope.py:231`). Because
`verify_strict` accepted the signature over the re-canonicalised bytes, those bytes ARE the
signed message. Storing them is storing what was signed; no raw request body is needed.

### 1.3 The server result signature

After the handler returns, the server signs the `skill.result` shape
`{v, kind, call_id, target_peer_id, status, result, issued_at, nonce[, error_code,
error_message]}` with its own key over `jcs` (`route.py:207-227`,
`apps/calls/result_signing.py:54-61`) and writes `status`, `result`, `result_payload`,
`result_signature` and `result_signing_kid` in one UPDATE (`route.py:283-288`). For
memory.write the `result` object is the handler's return value (`handlers.py:956-968`):
M-run (probe P2a), its keys are exactly `claimed_status, created_at, id, is_head, key,
namespace, op, pre_discipline, replay, status, version`. **`author_label` and `tags` are not
in it.** That is the FIRST write. A replay answers `_replay_result` (`handlers.py:677-686`,
M-read): `op, namespace, key, version, is_head, id, replay` only, with no `status`,
`pre_discipline` or `claimed_status`, although `_existing_by_dedup` selects `status`
(`lines 647`). On W1 a retry is normally answered by the `skill_calls` idempotency layer before the
handler, so the handler's replay is mostly a W3 answer, which is unsigned anyway.

A result that `jcs` cannot serialise is replaced by a signed `E_INTERNAL` failure
(`route.py:157-167`). Section 1.5 shows that this fires on a READ of a row that a write
accepted.

### 1.4 Column by column: what covers what

The INSERT's columns (`handlers.py:926-929`) and where each value comes from:

| column | written from | in the client-signed bytes | in the server-signed result | derivable from the bytes alone |
|---|---|---|---|---|
| `namespace`, `key` | `args.namespace`, `args.key` (`handlers.py:717-718`) | yes | yes | yes |
| `value` | `args.value`, else `args.body` (`lines 720`) | yes | no | yes |
| `status` | `validate_status(args.status)` (`lines 725`, `validation.py:322-328`); **`'OPEN'` when absent on a non-custody shared/wiki write** (`lines 743-745`); refused when present on a custody write (`lines 753-758`) | only when the writer sent it. M-run (P2a): a wiki write with no status stores `OPEN` and its signed args carry no `status`. M-run (C4): a list or object status raises `TypeError` in the set lookup, which is not a `HandlerError`, so W1 answers `E_INTERNAL` (`route.py:193-197`) and W3 an unhandled 500 | yes (first write only, 1.3) | yes: a pure function of `args.status`, `args.namespace` and `args.pre_discipline` |
| `pre_discipline` | `bool(args.pre_discipline)` (`lines 737`), Python truthiness | the raw JSON value. M-run (P2b): the STRING `"false"` stores `TRUE`, a custody row, for a namespace admin; for anyone else the same write is refused as an admin op (`lines 793-794`, M-read), a confusing refusal rather than a custody row | yes (first write only) | yes, under Python truthiness |
| `claimed_status` | `args.claimed_status` if pre_discipline (`lines 738`), no vocabulary check | yes | yes | yes |
| `tags` | `norm_tags(args.tags)` (`lines 726`, `validation.py:359-375`) | the pre-normalisation value. M-run (P2c): `" a, b ,,c"` stores `{a,b,c}`. M-run (C4): a list is coerced with `str()`, so `[1, true, null, {"a": 1}]` stores `{1,True,None,"{'a': 1}"}`, Python reprs that no SQL rule reproduces | **no** | yes (Python's split, `str.strip` and `str()`) |
| `author_label` | `args.author or args.author_label`, else the roster label, else `peer-<id8>` (`lines 727-735`) | only when the writer sent it. M-run (P2a): absent, the row reads `peer-<id8>` | **no** | only when signed; the fallback depends on the roster loaded at startup (divergence 034) |
| `created_by_peer_id` | the authenticated caller | `payload.caller_peer_id`, required on W1/W2 by check 4 (`envelope.py:196-212`). **Not required on W3**: M-run (P3), a payload with no `caller_peer_id`, no `kind` and no `issued_at` lands. W4 always signs it (1.1) | no | on W1, W2 and W4; not on W3 |
| `created_by_owner_id` | `derive_owner_id` at write time (`lines 802`) | no | no | no: a server fact |
| `version`, `parent_id`, `is_head` | the head lookup (`lines 824-881`) | no (`expected_head_version` is signed but optional) | `version`, `is_head` | no |
| `embed_policy` | the constant `'forbidden'` (`lines 931`) | no | no | constant |
| `provenance_jsonb` | `{payload, signature, signing_kid, caller_owner_id, payload_jcs_sha256, public_key_b64url, public_key_sha256, verified_at, pre_discipline}` (`lines 895-905`) | `payload` is | no | `payload` is; the rest are server scalars |
| `payload_jcs_sha256` | `sha256(jcs(payload))` (`lines 776`, `validation.py:517-521`) | n/a | no | yes |
| `provenance_call_id` | GENERATED from `provenance_jsonb->'payload'->>'call_id'` (`sql/028_shared_memory_v02.sql:122-124`) | yes | as the call's id | yes (already derived) |

The server-signed result exists only on W1 and W2 (1.1); a board or in-process write has none.

What stops a stored value changing after the insert: the immutability trigger refuses any
change to the listed content columns; only head retirement and, in a `high_churn` namespace,
a `parent_id` unlink are allowed (`sql/063_high_churn_parent_null_exemption.sql:116-132`,
`lines 143-161`). **The trigger names its columns one by one** (M-read). A column added later is
mutable until that function is redefined to include it. That is a trap for this design.

### 1.5 Can the canonical bytes be rebuilt from what is stored?

Mostly, and not always. The payload is stored only as JSONB inside `provenance_jsonb`
(`handlers.py:935`); nothing stores the bytes.

* **M-run (M2, SELECT-only casts on the dev server, `pg_is_in_recovery() = false`):** for
  `json.dumps(payload)::jsonb` read back with psycopg's default loader, `jcs` of the result
  equals `jcs` of the original for the integers 1 and 2^53-1, `1.0`, `100.0`, `0.1`, `1e-7`,
  `1e15`, `-0.0`, U+2028, astral characters and astral-versus-BMP key order. It does not for
  integral floats that Python writes in exponent form, from 1e16 up (`1e16`, `1e20`,
  `1.5e300` tried): Python writes `1e+16`,
  JSONB stores the numeric `10000000000000000`, psycopg reads it back as an `int`, and
  `rfc8785` 0.1.4 refuses integers beyond 2^53-1 with `IntegerDomainError` (each of four
  tried: 2^53+1, 10^16, 2^60, -2^53). JSONB refuses
  U+0000 outright and keeps the last of duplicate keys.
* **M-run (P1, end to end through W1):** a signed memory.write of `{"n": 1e16}` completes and
  lands one row. Every memory.read of that row then fails with the signed
  `E_INTERNAL: result is not canonicalizable` (the message `route.py:164` writes when
  `jcs` refuses the result), with or without `include_provenance`. An idempotent retry of
  the same write answers HTTP 500 `E_INTERNAL`. The control `{"n": 1e15}` reads back
  `completed` and retries with 202. **I (the retry's cause):** `idempotency_check`
  re-canonicalises `skill_calls.args` read back from JSONB (`apps/calls/repository.py:130`)
  and `jcs` raises. **Rival:** another re-canonicalisation on the replay path; I did not
  capture the traceback.
* **M-run (P1):** reconstruction of the `1e16` row with the default loader raises; with a
  loader that reads integers beyond 2^53-1 as floats, `sha256(jcs(payload))` equals the
  stored `payload_jcs_sha256` and the stored signature verifies. For the control, the default
  loader already matches and verifies.

So for rows this build wrote, the signed bytes are recoverable from `provenance_jsonb` with one
parsing rule, and the rule is needed only for numbers RFC 8785 cannot hold as integers. What
the probe also found is a defect of its own (G1 below): a row can be written that no one can
read.

### 1.6 What "verified" means today (`signed_by`)

`signed_by` came from review F1 of fin/r1-small (commit cccc77d), the round that also landed
divergence 034's roster loader. `read` prints `signature_verified` and `signed_by` (`README.md:265-276`,
`tools/meshy_node/node.py:619-624`). The client's check (`wire.author_signature_ok`,
`tools/meshy_node/wire.py:298-369`) requires `kind = skill.call` of memory.write (`lines 335`),
`payload.caller_peer_id` equal to the row's writer (`lines 338`), namespace and key equal, the
signed value's `jcs` equal to the stored value's (`lines 348-349`), and the signature over
`jcs(payload)` valid under the key the writer publishes (`lines 369`). **True ties the row to its
signer, not to its printed author** (`README.md:271-276`): `author` and `from` are labels.

M-run (P4): the same check returns `(True, None)` for a memo whose `status` reads `CONFIRMED`
while the signed args say `OPEN`, for changed `tags`, and for a changed `author_label`; the
control (a changed value) returns `E_SIGNATURE_NOT_THE_MESSAGE`. **Today a status that
contradicts its signature still reads `signature_verified: true`.** On this build the
immutability trigger prevents an UPDATE that would make it contradict; a row that arrives with
the contradiction already in it (section 3) is not caught by anyone.

Scope of that exposure (critique, M-read): the `tools/meshy_node` client's `read` asks only for
shared/mesh-chat (`tools/meshy_node/node.py:612`) and prints no `status` or `tags` at all; its
docstring names `author_label` as server-assigned and deliberately unchecked (`wire.py:312-314`).
So in this build's own client the gap is latent: it bites a shared/wiki reader (the
`mem_cli`, the dashboard) that shows a status beside a verified signature. Closing it does not
need the schema (section 4.1, phase 0).

### 1.7 Gaps found while tracing, routed for triage (not part of B02 itself)

* **G1 (M-run, P1).** A value holding an integral float of 1e16 or more is accepted, stored,
  and then unreadable; its retry answers 500 (run end to end for 1e16; larger values by the
  M2 mechanism, I; rival: a larger value whose JSONB text keeps a fraction, which M2 did not
  find for 1e20 or 1.5e300). an internal project document (not part of this release) records exponent floats being
  PRODUCED rather than refused as a deliberate choice (an internal project document (not part of this release), "canonical JSON is
  real RFC 8785"); the JSONB round trip is what that choice did not cover.
* **G2 (M-run, P3; path corrected by the critique).** On W3, the HTTP board route, a signed
  payload with no `caller_peer_id`, no `kind` and no `issued_at` is written:
  `_bind_envelope_to_the_call` compares `caller_peer_id` and `skill_name` only when present
  (`handlers.py:1425`, `lines 1431`), **never compares `kind` at all, even when present** (M-read,
  `lines 1400-1435`), and nothing on that path runs `verify_payload`. So W3 has neither the domain
  separation `envelope.py:17-23` rests on nor a freshness bound. W4 is not exposed: it signs a
  complete payload itself (1.1). The row's bytes then do not name its signer, and nothing
  bounds when the envelope was made. Control: the same payload naming ANOTHER caller is refused
  `E_BAD_ENVELOPE`. What still stands: W3 authenticates the bearer (`apps/board/views.py:167`)
  and the handler verifies under THAT caller's key (`handlers.py:787`), with revocation and its
  grace window applied (`permissions.py:59-63`). So exploiting the missing freshness bound
  needs the signer's own bearer and DPoP key, for example to re-land an old envelope of theirs
  whose row an ephemeral rewrite or a prune deleted, which the dedup (`handlers.py:636-651`)
  then no longer finds (I; the dedup is by row, the deletion paths are 3.4). W3 writes only
  `msg/` keys in shared/mesh-chat (`apps/board/declare.py:187-193`), so that case needs
  shared/mesh-chat to be ephemeral or high_churn, and memory.prune keeps a message's version 1
  (`handlers.py:1341-1344`). A hardening gap, not an open bypass.
* **G3 (M-run, P2b and C4).** `pre_discipline: "false"` (a string) makes a custody row for a
  namespace admin. A list or object `status` answers `E_INTERNAL` rather than
  `E_INVALID_STATUS`; list `tags` store Python reprs. All three are what strict typing (Q3)
  refuses.
* **G4 (M-run, P2a).** The server's signed write result omits `author_label` and `tags`, so
  the server attests neither, including the author label it stamped itself.
* **G5 (I).** This build refuses integers beyond 2^53-1 at write time and cannot read them if
  present in historical payloads, because `rfc8785` refuses them (M-run). Section 3.5 counts
  those rows.

---

## 2. Target schema

### 2.1 Options

| | shape | for | against |
|---|---|---|---|
| **A** | `shared_memory.signed_bytes BYTEA` for new rows; a separate append-only verdict table for historical rows | the ledger row never changes after insert; historical verdicts are data that can be recomputed and versioned; no coupling to another table's retention | two places to look for a row's bytes |
| B | content-addressed `signed_payloads(sha256 PK, bytes)`, referenced by `payload_jcs_sha256` from shared_memory (and possibly `skill_calls.args`) | one store; a CHECK binds hash to bytes | **no dedup gain**: `payload_jcs_sha256` is already UNIQUE per row (`sql/028:118-119`); sharing with `skill_calls` ties the evidence to a table whose retention is still undecided (#941, an internal project document (not part of this release)) |
| C | A's column, backfilled in place for historical rows | one place | needs a NULL-to-value exception in the immutability trigger; 063 already shows how a narrow relaxation carries the whole table's safety on one premise (`sql/063:29-43`) |
| D | `status` as a GENERATED column over the bytes | the stored status cannot be written at all | historical rows have no bytes, so it needs `COALESCE(derived, legacy_status)` and a copy of every row's status into a new column: a rewrite of shared_memory, the status index and CHECK dropped and rebuilt |

**Recommendation: A** (open question Q1). M-run (SQL probe): a generated column over the bytes
is possible only through an IMMUTABLE wrapper, because `convert_from(bytea, name)` is STABLE
(`provolatile = 's'`) and Postgres refuses it with "generation expression is not immutable";
`sha256(bytea)`, `encode` and `jsonb_in` are IMMUTABLE. A generated column refuses UPDATE
("can only be updated to DEFAULT"). So D is buildable; it costs a rewrite and buys less than
the trigger below, which already makes a disagreeing insert impossible.

### 2.2 The schema under A (a sketch for approval, not a migration)

```sql
-- sql_meshychat/NNN (not written)
ALTER TABLE shared_memory ADD COLUMN signed_bytes BYTEA;   -- NULL only on rows inserted before NNN

-- STABLE, which is what it is (convert_from is STABLE, M-run). A BEFORE INSERT trigger does
-- not need IMMUTABLE; only a generated column or an index expression does (Q2 rejects both).
-- Declaring it IMMUTABLE would be true only while the database encoding stays UTF8, and a
-- mislabelled volatility is how an expression index silently goes wrong later.
CREATE FUNCTION sm_signed_payload(b bytea) RETURNS jsonb
  LANGUAGE sql STABLE STRICT AS $$ SELECT convert_from(b, 'UTF8')::jsonb $$;

CREATE FUNCTION sm_derive_pre_discipline(p jsonb) RETURNS boolean ...   -- 2.3
CREATE FUNCTION sm_derive_status(p jsonb) RETURNS text ...               -- 2.3
CREATE FUNCTION sm_derive_claimed_status(p jsonb) RETURNS text ...       -- 2.3
CREATE FUNCTION sm_signed_value(p jsonb) RETURNS jsonb ...               -- args.value, else args.body

CREATE TRIGGER shared_memory_signed_bytes_agree_trg BEFORE INSERT ON shared_memory
  FOR EACH ROW EXECUTE FUNCTION shared_memory_signed_bytes_agree();     -- 2.4

-- and shared_memory_enforce_immutability() redefined with signed_bytes in BOTH column
-- lists (sql/063:116-132 and lines 143-161), or the column stays mutable (1.4).

CREATE TABLE shared_memory_legacy_verdict (           -- section 3.3; append-only
  -- No FK with ON DELETE CASCADE: an ephemeral rewrite or a prune deletes ledger rows (3.4),
  -- and a cascade would erase the verdict, including a contradiction, with the row. The row's
  -- identity is copied instead, so a count taken later still sees it.
  row_id             uuid NOT NULL,
  row_namespace      text NOT NULL,
  row_key            text NOT NULL,
  row_version        integer NOT NULL,
  classifier_version text NOT NULL,
  verdict            text NOT NULL CHECK (verdict IN ('verified','hash_bound_signature_fails',
                                                      'no_hash_match','no_envelope')),
  loader             text CHECK (loader IN ('json_default','ijson_float')),
  canonicalizer      text CHECK (canonicalizer IN ('rfc8785','sort_keys')),
  verified_bytes     bytea,                           -- set only when verdict = 'verified'
  verified_kid       text,
  key_revoked_before_write boolean,                   -- peer_keys.revoked_at < row created_at
  binding_failures   text[] NOT NULL DEFAULT '{}',    -- kind, skill_name, caller, namespace, key, value
  disagreeing_signed    text[] NOT NULL DEFAULT '{}', -- a SIGNED arg differs from its column
  disagreeing_defaulted text[] NOT NULL DEFAULT '{}', -- a server-DEFAULTED field differs (rule drift)
  checked_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (row_id, classifier_version)
);
```

The bytes are `primitives.jcs` output (`apps/crypto/primitives.py:63-65`), RFC 8785, the
library the `tools/meshy_node` client signs with (`tools/meshy_node/wire.py:35`). For a payload from
any client, they are the bytes `verify_strict` accepted, so they are what that client signed
(1.2). The legacy verdict table records which canonicaliser rebuilt a historical row, because
the was not RFC 8785 (3.2).

The verdict table gets FORCE RLS with per-command policies in the shape `sql_meshychat/008`
gives the projection tables (a read policy, and insert only in the background job's writer
context: divergence 022, the project's design log (not part of this release)), no UPDATE or DELETE policy at all, and
`REVOKE UPDATE, DELETE` from `meshy_app`, the mechanism `sql/111:14-17` records for
invocation_frames. Append-only needs that mechanism; the first draft named the property only.
Its own BEFORE INSERT trigger checks that `verified_bytes`, when set, hash to the row's
`payload_jcs_sha256` and parse to a JSONB equal to the row's `provenance_jsonb->'payload'`.

**What that trigger does NOT do (corrected by the critique).** The first draft said it "makes a
fabricated verdict impossible without a SHA-256 preimage". It does not. The bytes that hash to
`payload_jcs_sha256` are derivable by anyone from the stored payload (canonicalise it), so a
writer of this table can mark a row whose signature FAILS as `verified` and the trigger passes.
What the trigger prevents is fabricated BYTES: bytes other than the ones the stored hash commits
to, which would need a second preimage. A verdict is therefore advisory: it is what the job
concluded, never proof. The proof is a reader verifying the bytes itself (2.5, Q11, Q13).

**The pattern is already on main.** `sql_meshychat/011` (divergence 028) is a BEFORE INSERT
trigger that holds, in the database, a rule the handler also holds (one writer per mesh
message), raises `E_NOT_MESSAGE_AUTHOR: ...` with `ERRCODE = 'check_violation'`, and fires on
INSERT only (`sql_meshychat/011:46-80`). The agree-trigger copies that shape and error
convention. It also strengthens 011: once `created_by_peer_id` must equal the signed
`caller_peer_id`, 011's "one writer per message" becomes "one SIGNER per message". BEFORE
triggers fire in name order (`..._mesh_single_writer_trg`, `..._root_parent_trg`, then
`..._signed_bytes_agree_trg`); none of the three assigns to `NEW` (M-read, a grep for `NEW.x :=`
in 011 and 064 finds nothing), so order does not change what the agree-trigger sees.

`status`, `claimed_status`, `pre_discipline`, `author_label`, `tags` stay ordinary columns,
under their existing names. Readers do not change: the dashboard's `sm.status`
(`apps/dashboard/queries.py:208-214`), the handler's status filter (`handlers.py:1081-1083`),
the closure rule's PROPOSED_FIX lookup (`lines 619-634`), list and prune (`lines 1241`, `lines 1339`), and the
head status index (`sql/028:162-164`).

### 2.3 Derivation rules

One rule per derived field, the same in the handler (Python) and in the trigger (SQL). The
trigger REFUSES on disagreement; it never overwrites. Two implementations that must agree is
the point: a drift between them fails the write instead of storing either answer.

| field | rule over the signed payload `p` | notes |
|---|---|---|
| `pre_discipline` | absent or JSON null: false; a JSON boolean: its value; anything else: refuse | today's rule is Python truthiness (G3). Q3 |
| `status` | `args.status` if a JSON string, NULL if absent or null, refuse otherwise; refuse a non-NULL `args.status` when `pre_discipline`; then, if NULL and `args.namespace = 'shared/wiki'` and not `pre_discipline`: `'OPEN'` | reproduces `handlers.py:725`, `lines 743-745` and `lines 753-758` (the first draft left out the custody refusal); the CHECK on the column still holds the vocabulary (`sql/028:88-97`) |
| `claimed_status` | if `pre_discipline`: `args.claimed_status` when a JSON string, NULL when absent, refuse otherwise; else NULL | `handlers.py:738` |
| `value` | `args.value` when the key is present, else `args.body` | JSONB equality, which is numeric equality: M-run, `'1.0'::jsonb = '1'::jsonb` is true, the same identification RFC 8785 makes |
| `namespace`, `key` | `args.namespace`, `args.key` | text equality |
| `created_by_peer_id` | `p.caller_peer_id`, REQUIRED | the trigger also requires `p.kind = 'skill.call'` and `p.skill_name = 'memory.write'`; this closes G2's identity and domain-separation halves at the database whatever the path. It does NOT close G2's freshness half: `issued_at` is checked against a clock, which a row trigger holding a 120-second window would make depend on insert latency, so freshness stays Q9's application check |
| `payload_jcs_sha256` | `encode(sha256(signed_bytes), 'hex')` | |
| `author_label` | if `args.author` (else `args.author_label`) is a non-empty JSON string, the column must equal it; otherwise the server stamped it and the trigger does not check it | the roster is not in the database. A reader derives `author_signed` from the bytes (2.5); nothing stores it, so it cannot disagree |
| `tags` | as a JSON list of strings: equal, element for element; a list with any non-string element: refuse (today the handler stores `str()` reprs, C4, which SQL cannot reproduce); as a comma string: checked by the offline verifier, not the trigger | Python's `str.strip` removes Unicode whitespace that SQL `btrim` does not (I, from the two languages' definitions, not run here; rival: the fleet never sends such a tag, and then a SQL port would agree in practice; measurement 3.5.4 counts comma-string tags). Tags are filters, not gate inputs. Q3 |

**Identity versus labels (divergence 034).** What the bytes prove about authorship is
`p.caller_peer_id`: the signer, `signed_by`. A signed `author` proves only that the signer
CLAIMED that label; the server binds labels to the signer only for `msg/` keys written since
divergence 028 (`README.md:271-276`). So the design derives identity from the bytes and keeps
labels as labels, with a flag that says whether the writer signed one.

### 2.4 The invariants, and what the database cannot hold

`shared_memory_signed_bytes_agree` (BEFORE INSERT), in phase A2 (section 4.1):

1. `signed_bytes` IS NOT NULL. In phase A1 a NULL is admitted ONLY when `current_user` is not
   `meshy_app` (4.1, Q12): this build inserts as `meshy_app` (`apps/core/rls.py:29`), while
   legacy rows may be inserted by the `meshy` superuser (`sql/028:12-16`). So from A1 on, a regression that
   drops the column from this build's INSERT fails the write instead of quietly producing a
   `legacy_unverified` row.
2. `encode(sha256(signed_bytes),'hex') = payload_jcs_sha256`.
3. `sm_signed_payload(signed_bytes) = provenance_jsonb->'payload'`.
4. Every row of the table in 2.3 agrees, or the insert raises with the field named.

Triggers fire for a superuser as well; RLS bypass does not skip them (I, from Postgres
semantics; rival: a session with `session_replication_role = replica` skips them, which no
code here sets, and so does `ALTER TABLE ... DISABLE TRIGGER`, which `pg_restore
--disable-triggers` issues). Superuser writes are bound by this trigger too. That is why phase
A1 exists.

**A logical restore is an inserter too (critique, I from pg_dump's section order).** A full
`pg_dump`/`pg_restore` creates triggers in the post-data section, after the rows are loaded,
so it never fires them. A `--data-only` restore into a schema already migrated to A2 fires the
agree-trigger on every historical row and refuses all of them (NULL bytes); it would also fire
`sql_meshychat/011` on every mesh version above 1, and its header says a chain written before
it can carry a second writer, which it never re-checked. 3.1's rival (a logical dump and restore, which a checksum-enabled cluster needs) is such
a path; whichever route is taken, the runbook loads historical rows by a full restore or before
NNN is applied, never data-only into A2.

**The database cannot verify the signature.** M-run: the dev database has `pg_trgm`,
`pgcrypto` 1.3 and `plpgsql`; no function in `pg_proc` has `ed25519` or `eddsa` in its name,
and pgcrypto's functions are digests, HMAC, crypt and PGP. The signature stays the
application's to verify (`handlers.py:1466`), plus an offline re-verifier that writes
verdicts for new rows too. What the database guarantees is narrower and still the thing E2
asks for: **the stored status is a function of the stored bytes**, whoever inserts.

### 2.5 What a reader sees

`memory.read` with `include_provenance` adds `signed_bytes` (base64url) and `status_basis`:

| `status_basis` | meaning |
|---|---|
| `bytes_bound` | the row has `signed_bytes`; the trigger held every derived field to them. It does NOT say the signature verified: the database cannot check one (2.4), and the handler's check at insert leaves no record the database can hold. The first draft named this value `signed`, which claims more than the server knows |
| `legacy_verified` | a historical row; the classifier rebuilt bytes that hash-match and verify under a key not revoked before the write, the envelope is bound to this row (3.3 step 5), and no SIGNED field differs from its column. A server-defaulted field may differ by rule drift and is noted beside it (Q6, as amended) |
| `legacy_contradicts` | as `legacy_verified`, but a SIGNED field differs from its column; the memo carries the signed value beside the column's (Q6) |
| `legacy_unverified` | a historical row whose bytes could not be rebuilt or did not verify, whose envelope is bound to another row, whose key was revoked before the write, or that is not yet classified; the memo names which |

The `tools/meshy_node` client extends `author_signature_ok` to verify over `signed_bytes` directly,
parse them with the I-JSON number rule (1.5), check the derived fields against the memo, and
print `status_verified` and `author_signed` beside `signature_verified`. G4's fix (the server
signs `author_label` and `tags` in its write result) makes the server's own stamp attested; it
must extend `_replay_result` too (1.3), or a replayed write answers without them.

**`status_basis` is advisory; the client's own check is the anchor (critique).** A server that
omits `signed_bytes` or answers `legacy_unverified` for a new row is a downgrade the client
cannot detect from the field alone. The client therefore derives status from the payload it has
itself verified, with or without `signed_bytes`: phase 0 (4.1) does exactly this over
`provenance.payload`, which works today for every row whose payload re-canonicalises. On a
disagreement between the server's verdict and the client's own (the verdict table accepts keys
in any state, 3.3; the client accepts only published keys and answers
`E_SIGNATURE_KEY_UNAVAILABLE` for a rotated or revoked one, 8bdf831), the client prints its own.

**What D leaves alone (main since the first draft's base).** A push frame and an addressed wait
answer (divergences 031, 033) carry `role`, `to`, `cc` and a `preview_unverified` body to a
reader memory.read would show it to (`README.md:254-258`): pointers, never the message. They get
no `status_basis` and no bytes. `search` and `context` keep answering
`E_SIGNATURE_NOT_CARRIED` (`README.md:281-282`); `read` stays the check.

### 2.6 skill_calls keeps the pair, with one guard

M-read: on this build `status` and `result_payload` are written from one variable in one
statement (`route.py:283-288`), and the other writers (`janitor.py:67-75`,
`reads.py:318-320`, `route.py:239`, `lines 257`) touch only in-flight rows, whose `result_payload`
is NULL. Nothing in the database holds the pair together. Proposed:

```sql
ALTER TABLE skill_calls ADD CONSTRAINT skill_calls_status_matches_signed_result
  CHECK (result_payload IS NULL OR status = result_payload->>'status') NOT VALID;
```

`NOT VALID` enforces it for every new or updated row and leaves the historical rows readable
(Q8). A later UPDATE of a violating historical row would be refused; this build updates no
terminal row (M-read, the writers listed).

**The `skill_calls` writers, checked by the critique:** read-result retention and `set_result`
set `result_payload = NULL` in the same UPDATE, so the new row satisfies the CHECK even on a
violating row. `finish_call_atomic` writes `status` beside `result_payload`. The remaining
`skill_calls` updates write status or error fields only on in-flight rows, whose
`result_payload` is NULL. S therefore preserves these update patterns and refuses a terminal
status that disagrees with its signed result.

**What the CHECK cannot see (corrected; the first draft said the 197 "stay readable and
reportable").** The 197-row count was recorded on 2026-08-29. Migration 107 began
writing `result_payload` at f4512dfa7, committed 2026-09-06. A row finished
before 107 was deployed has `result_payload IS NULL` and passes the CHECK whatever its status;
so does every memory.read and memory.query row retention has pruned. The CHECK therefore
guards new rows and can count only the post-107, unpruned part of the 197. Measurement 3.5.5
is split accordingly.

---

## 3. Historical rows

### 3.1 How they arrive

Production reaches HQD as a PHYSICAL streaming standby. The rehearsal uses a physical copy of
the standby's volume. **I:** historical shared_memory rows arrive with `provenance_jsonb` as JSONB
and `payload_jcs_sha256` but no bytes, because signed bytes were not stored (3.2). **Rival:**
the new environment is built by a logical dump and restore
instead of promotion, which a checksum-enabled cluster would need. JSONB goes through that as
the same values, so the rows still arrive without bytes and this section holds either way.

### 3.2 Historical storage before signed bytes

* `provenance_jsonb` has the same shape as this build, key for key, inserted through
  `json.dumps` into `::jsonb`.
* `payload_jcs_sha256` = SHA-256 of `json.dumps(payload, separators=(",",":"),
  sort_keys=True, ensure_ascii=False)` (`memory_handlers.py:583-590`), which the daemon
  calls RFC 8785 and which is not: M-run (M1), it differs from `rfc8785` on `1.0`, `100.0`,
  `1e-7`, `1e15`, `1e16`, `-0.0`, astral-versus-BMP key order, and integers beyond 2^53.
* Historical verification used `sort_keys` bytes rather than `verify_strict`.

**I:** a row accepted by both serialisation paths has a stored hash that matches the
`sort_keys` bytes and matches the `rfc8785` bytes except where `rfc8785` refuses a number (G5).
**Rival:** a path that skipped one verifier stored only `sort_keys` bytes. The classifier tries
both and records which matched, so the design does not depend on which is true.

**The loader matters as much as the canonicaliser (critique, M-run C2).** JSONB keeps a number's
value, not how it was typed. A historical payload can contain `1e16` as the Python int
`10000000000000000` before hashing `sort_keys` over that int. Read back with the default loader,
the int returns and `sort_keys` reproduces the stored hash; read back
with the I-JSON loader (integers beyond 2^53-1 as floats), `sort_keys` writes `1e+16` and the
hash fails. `rfc8785` over the float writes `10000000000000000`, which matches only while the
payload has no other point where the two canonicalisers differ: with an astral key added, no
I-JSON candidate matches and only default-loader `sort_keys` does. The first draft parsed once,
with the I-JSON loader, so it would have filed exactly the G5 rows it worries about as
`no_hash_match`.

### 3.3 Classification

A background job (own pool, keyset batches, a per-tick timeout: rule E7,
`docs/V1-DESIGN-RULES.md:229`, and
an internal project document (not part of this release)) reads each row with `signed_bytes IS NULL`:

1. Parse `provenance_jsonb::text` twice: with the default loader and with the I-JSON number
   rule (1.5). Both, because of 3.2's loader finding.
2. No payload, signature or kid: `no_envelope`.
3. Candidate bytes: the four of {default, I-JSON} x {`rfc8785` (skipped if it raises),
   `sort_keys`}. No candidate hashes to `payload_jcs_sha256`: `no_hash_match`. Record the
   loader and the canonicaliser that matched.
4. Verify the matching candidate under `peer_keys (created_by_peer_id, signing_kid)` in ANY
   state, revoked included, and require it to equal `provenance.public_key_b64url`. The key
   is only as good as the table, not the row (`wire.py:304-307` makes the same point for
   clients). Keys survive: `peer_keys` cascades on peer deletion (`sql/002_peer_keys.sql:5`)
   but `shared_memory.created_by_peer_id` blocks deleting a peer with rows
   (`sql/028:130`), and nothing here deletes from `peer_keys` (grep). Fails:
   `hash_bound_signature_fails`. Passes: `verified`, with the bytes and the canonicaliser.
5. For `verified` rows, check the binding the handler checks today: `kind = skill.call`,
   `skill_name = memory.write`, `caller_peer_id = created_by_peer_id`, the signed namespace, key
   and value equal to the row's. A failure goes in `binding_failures`: a signature that verifies
   over an envelope for some other row is not evidence for this one, and serving it as a
   status question (`legacy_contradicts`) would mislead.
6. Compute 2.3's derivations and split the fields that differ from their columns in two:
   `disagreeing_signed`, where the writer SIGNED the arg (`args.status` present and differing
   is the shared_memory analogue of the 197), and `disagreeing_defaulted`, where the column
   holds a server default (the wiki `OPEN`, a normalised tag list) that the daemon of the day
   may have computed by an older rule. The first draft kept one list, which would count rule
   drift as contradiction and inflate the number Q6 rules on.
7. Record `key_revoked_before_write`: `peer_keys.revoked_at` earlier than the row's
   `created_at`. A row that verifies under a key revoked before it was written is one no
   verifying handler would have accepted (`permissions.py:59-63` refuses it past the grace
   window), so it is counted apart even when the signature holds.
8. `legacy_verified` is `verified` with `binding_failures` and `disagreeing_signed` empty and
   `key_revoked_before_write` false; a non-empty `disagreeing_defaulted` is served beside it as
   a note (Q6).

The job records whether it used strict verification; a signature that libsodium accepted and
`verify_strict` refuses (a small-order key) is `hash_bound_signature_fails` here, reported
under its own count.

### 3.4 Rows whose bytes cannot be rebuilt

* **Keep them.** Nothing here rewrites a row, and this design deletes none. The first draft
  said "nothing is deleted"; that holds only for keep_all namespaces. An ephemeral namespace's
  write DELETEs the key's whole chain before inserting (`handlers.py:913-917`), and
  memory.prune DELETEs non-head versions in a high_churn namespace (`lines 1282-1366`); the header
  of `sql_meshychat/011` says so and declines a DELETE trigger for that reason. So the counts in
  3.5 are taken on one snapshot, and verdicts keep the row's identity rather than cascading
  away with it (2.2).
* **Never fabricate bytes, and never have the server sign a historical status.** Migration
  107 set the precedent for `skill_calls`: "a fabricated canonical payload is worse than an
  absent one: it would verify against nothing while looking authoritative"
  (`sql/107_skill_calls_result_payload.sql:15-21`). `route.py:46-48` refuses to sign a
  result for work the server did not do; signing a status it did not verify is the same act.
* **Serve them flagged**: `status_basis: legacy_unverified`, and the client prints
  `status_verified: false` with a typed code.
* **Gates keep reading the column** (Q7). Treating an unverified PROPOSED_FIX as absent would
  change what `_proposed_fix_owners` (`handlers.py:619-634`) allows on existing chains the
  moment the server starts.
* **An owner can re-anchor** a row that matters by writing a new signed version. This is an
  operator action per row, never automatic.

### 3.5 Measurements owed before implementation

Counts only, on the rehearsal copy (`cutover-list.md:1175-1182`). **Never on the standby, never
on production, no row content leaves the box:**

1. shared_memory rows in total, and per namespace class (`shared/wiki`, `shared/mesh-chat`,
   other).
2. Each verdict of 3.3, and per verdict the rows with `disagreeing_signed` containing status
   (the shared_memory counterpart of the 197), separately from `disagreeing_defaulted` (rule
   drift), `binding_failures` and `key_revoked_before_write`.
3. Rows whose `provenance_jsonb` or `value` holds a number beyond 2^53-1 (G5), for example
   `jsonb_path_exists(value, 'lax $.** ? (@.type() == "number" && (@ > 9007199254740991 ||
   @ < -9007199254740991))')` (M-run on five synthetic literals: true for 10^16 at the top
   level and for -2^53 nested in an array, false for 2^53-1, 1.5 and a plain array). Any
   nonzero count is a set of rows this build cannot read today.
4. Rows whose signed args carry a non-boolean `pre_discipline`, a non-string `status`,
   `claimed_status` or `author`, or a comma-string `tags`: what strict typing (Q3) would have
   refused.
5. `skill_calls`, in two counts: rows violating 2.6's CHECK (post-107, unpruned); and rows
   with `result_payload IS NULL`, a non-NULL `result_signature` and a terminal status, which the
   CHECK cannot see (2.6). The sum is not the 197: the 197 were found by #916's own method, and
   the second count only bounds how many of them the CHECK is blind to.
6. `pg_total_relation_size('shared_memory')` and the row count, so the cost of carrying the
   payload a third time (value, provenance, bytes) is a number before A1, not after.

---

## 4. Migration, coexistence, rollback, tests

### 4.1 Phases

| phase | what ships | who it binds |
|---|---|---|
| **0** (added by the critique) | the `tools/meshy_node` client only: after `author_signature_ok` holds, derive `status`, `claimed_status`, `pre_discipline` and list `tags` from the verified `provenance.payload.args` by 2.3's rules and compare with the memo; print `status_verified` with a typed code, reporting a field the server DEFAULTED (no signed arg) as defaulted rather than as a contradiction (3.3 step 6). No server or schema change | readers, today, for every row whose payload re-canonicalises, historical ones included (Q13) |
| **A1** | the column, the functions, the agree-trigger requiring bytes from `meshy_app` and admitting NULL from any other role (2.4 invariant 1, Q12), the immutability redefinition, the handler writing `signed_bytes = jcs(payload)` (the bytes `verify_strict` just accepted, `handlers.py:1466`), the seam refusing non-I-JSON numbers (Q4), strict arg types (Q3), G2's checks (Q9), G4's result fields in both the first-write and the replay result | this build from its first A1 write; writes from other roles may still omit bytes |
| **A2** | the trigger's NULL admission removed for every role | every inserter. Only after every write carries signed bytes and the rollback window has closed, because A2 refuses every unsigned shared_memory write (Q10) |
| **C** | the verdict table and the classifier job | historical rows |
| **D** | `memory.read` fields (2.5) and the client's extended check | readers |
| **S** | 2.6's CHECK, `NOT VALID` | `skill_calls` |

Adding a nullable column with no default does not rewrite the table (I, from Postgres 11+
semantics; rival: another statement in the same migration takes a long lock, as a CHECK added
VALID would by scanning the table; measure the lock time on the rehearsal copy). The trigger
costs one SHA-256 and one JSONB parse per insert over the payload. The first draft bounded that
by a mesh body (`validation.py:96`, 65,536 bytes), which holds only for mesh v1 messages
(`validation.py:293`); any other namespace's value is bounded by Django's request-body limit on
HTTP (`DATA_UPLOAD_MAX_MEMORY_SIZE`, 2.5 MB by default, `apps/files/views.py:344` names it), I.
Measure with a bench test in A1 at both sizes.

### 4.2 Coexistence

* **Clients do not change in A1-C.** The envelope format is the same; only storage changes.
  A1 stores every payload this build verifies today.
* **Phase A1 permits historical inserts.** Rows without `signed_bytes` are classified in C;
  A2 refuses them. Hence Q10.
* **Cutover.** A2 has no date until every production write carries signed bytes. Phase 0 is
  the only part that helps historical readers before this build serves.
* **A restore is an inserter** (2.4): load historical rows by a full restore, or before NNN,
  never data-only into A2.
* **The test suite and the calibration tool** insert rows directly (1.1). A1 admits them only
  while they carry no bytes; A2 needs a helper that builds a signed row through the handler
  or signs a synthetic payload.

### 4.3 Rollback

There are no down-migrations (an internal project document (not part of this release)), and the runner records a
checksum per applied file (`apps/core/sqlmigrate.py:15-17`) and has no down step (M-read: no
`down` anywhere in it). So each phase ships with a named down script, outside the migrate
set, and a test that applies it.

* **A1 down:** drop the trigger and functions, restore 063's immutability function, drop the
  column. Old code then runs unchanged. The bytes of rows written under A1 are lost from the
  column and recovered by C, because the seam admitted only I-JSON numbers and P1 shows those
  rebuild from `provenance_jsonb` with the default loader (I; rival: a value shape P1 did
  not cover. The down test checks the recovery on every row it wrote).
* **A2 down:** restore the NULL admission. Code rolled back past A1 while A2 is live fails
  every unsigned write, closed and loudly. The runbook applies A2's down script before any
  code rollback, and the rebuild's own rollback runbook names it as a step.
* **C down:** drop the verdict table; no ledger row changes.
* **S down:** drop the constraint.

### 4.4 Tests (for the implementation; each RED on the unchanged tree first, each with a named mutant it kills, each with a positive control)

1. **The stored bytes are the signed bytes.** `verify_strict(pub, row.signed_bytes,
   signature)` holds, and `sha256` equals `payload_jcs_sha256`. Mutant: store `jcs(args)`.
2. **A disagreeing insert is refused by the database**, as `meshy_app` in server-internal
   context AND as the owner role (a superuser: RLS bypassed, trigger not). One test per
   derived field in 2.3, each beside the agreeing insert. Mutants: the trigger skips the check
   when `args.status` is absent (killed by the wiki-default case); it compares `caller_peer_id`
   only when present (killed by G2's payload).
3. **The wiki default derives.** No signed status, `OPEN` stored, trigger satisfied. Mutant:
   the SQL rule drops the `pre_discipline` term (killed by a custody write to shared/wiki).
4. **Strict types at the seam** (if Q3 is yes): `pre_discipline: "false"` refused with a
   typed code, `false` accepted. Mutant: truthiness restored.
5. **Non-I-JSON numbers** (Q4): `1e16` refused at the seam, `1e15` accepted and readable.
   Mutant: the check compares with `>` 2^53 instead of `>` 2^53-1 (killed by 2^53).
6. **G2 closed**: no `caller_peer_id`, no `kind` or no `issued_at` refused on W3; the same
   payload complete lands. Mutant: presence checked on W1 only.
7. **Immutability**: an UPDATE of `signed_bytes`, including NULL-to-value, is refused;
   head retirement and a high_churn prune's parent unlink still work. Mutant: 063's function
   left unredefined. PLUS a census (critique): every column of `shared_memory` in
   `information_schema.columns`, except `is_head`, `parent_id`, `updated_at` and the generated
   `provenance_call_id`, appears in BOTH lists of the live function's source (`pg_proc.prosrc`).
   A test that names `signed_bytes` alone passes again the day a column is added or the function
   is redefined as 063 did.
8. **Classifier**: one synthetic fixture per verdict (rfc8785 match, sort_keys-only match,
   hash match with a bad signature, no hash match, no envelope, a key revoked after the write,
   a key revoked BEFORE the write, a signed-status disagreement, a defaulted-status
   disagreement, an envelope bound to another row's key). Plus C2's fixture: an int beyond
   2^53-1 beside an astral key, signed over `sort_keys`, which only the default loader matches.
   Mutants: accept a hash match without verifying; accept the row's own `public_key_b64url`
   without `peer_keys`; parse with one loader only (killed by C2's fixture); one disagreement
   list (killed by the defaulted case). No production content in any fixture.
9. **The verdict table's trigger** refuses `verified_bytes` that do not hash to the row.
10. **skill_calls CHECK**: a new row with `status` not equal to `result_payload->>'status'`
    refused; a pre-existing violating row still readable. Mutant: constraint added VALID
    (the setup fails on the seeded historical row).
11. **Client**: `status_verified` false on a memo whose status differs from the derived one;
    true on the agreeing memo; P4's three cases each caught. Mutant: status not compared.
12. **Census**: every INSERT into shared_memory in `apps/` goes through the handler (a
    structural test, backed by test 2's database-level refusal, which catches a path the
    census misses).
13. **Rollback**: each down script applied on a database with rows from its phase; the old
    handler writes; C recovers A1's bytes.
14. **A1 is role-keyed**: an insert as `meshy_app` with NULL `signed_bytes` is refused; the same
    insert as the owner role lands. Mutant: NULL admitted for every role.
15. **Drift surfaces typed**: a handler whose Python derivation disagrees with the trigger
    (patched in the test) gets the trigger's `check_violation`, nothing is written, and W1
    answers a signed `E_INTERNAL` (`route.py:193-197`), never `E_WRITE_CONFLICT` (the handler
    maps only `UniqueViolation`, `handlers.py:946`).
16. **Replay result**: a W3 replay answers the same `status`, `author_label` and `tags` as the
    first write. Mutant: `_replay_result` unchanged.
17. **Deletion keeps verdicts**: an ephemeral rewrite and a prune remove ledger rows; their
    verdict rows remain and are counted.
18. **Phase 0 client**: `status_verified` false with a typed code on a memo whose status
    differs from the one derived from its verified payload; true on the agreeing memo;
    `E_SIGNATURE_KEY_UNAVAILABLE`, not a contradiction, for a rotated key.

---

## 5. Open questions, each with a recommendation

**Q1. Where do the bytes live: an inline column plus a legacy verdict table (A), a
content-addressed store (B), or an in-place backfill (C)?** Recommend A. The ledger row stays
write-once; B gains no dedup (`sql/028:118-119`) and couples the evidence to `skill_calls`'
undecided retention; C needs the kind of trigger relaxation 063 warns about.

**Q2. Derived fields as plain columns held by an agree-trigger, or as GENERATED columns?**
Recommend the trigger. No rewrite, no change for any reader or index, historical status kept
as it is, and two derivations that must agree. Use a GENERATED column only for a NEW pure
column, if one is ever added.

**Q3. Strict JSON types for the derived args at the write seam** (`pre_discipline` a boolean;
`status`, `claimed_status`, `author` strings; `tags` a list of strings or a string)? Recommend
yes: without it the SQL rule has to reproduce Python truthiness and `str()` reprs (G3), and a
list status keeps answering `E_INTERNAL` instead of `E_INVALID_STATUS` (C4). It refuses writes that
land today, so it needs its own divergence entry and measurement 3.5.4 first. Tags in comma
form: keep accepting them, checked by the offline verifier rather than the trigger.

**Q4. Numbers RFC 8785 cannot hold as integers?** Recommend refusing them at the write seam
with a typed code, which closes G1 (unreadable rows, a 500 on retry), and using the I-JSON
loader only in the classifier. Measurement 3.5.3 decides whether historical rows need a
read-path fix as well (G5).

**Q5. A server-stamped `author_label`?** Recommend keeping the stamp because blank authors lost
attribution (`handlers.py:728-734`), adding `author_label` and
`tags` to the server's signed write result (G4), and having readers print `author_signed`
from the bytes. Rival option: require every writer to sign an author. That breaks senders
that omit it.

**Q6. A historical row that verifies but whose status column disagrees with its derived
status: which one is served?** Recommend the column, unchanged, with
`status_basis: legacy_contradicts` and the derived value beside it, until the count from
3.5.2 is known; then the operator rules per class. Never rewrite the row. Amended by the
critique: `legacy_contradicts` only for `disagreeing_signed`; a `disagreeing_defaulted` row is
rule drift and is served `legacy_verified` with the note; a `binding_failures` row is served
`legacy_unverified`, because its signature is evidence for some other envelope.

**Q7. Historical rows that cannot be rebuilt?** Recommend: keep, flag
`legacy_unverified`, never fabricate or server-sign, let gates keep reading the column, and
re-anchor by a new signed version where an owner cares (3.4).

**Q8. The `skill_calls` CHECK in 2.6?** Recommend yes, `NOT VALID`. It closes the status
contradiction class for new rows at almost no cost. Corrected by the critique: the historical violators are only partly
countable through it, since pre-107 and pruned rows carry no `result_payload` (3.5.5).
It does not satisfy E2's "must not exist" for `skill_calls`; the brief keeps the pair, and
this is the most that can be done without removing it.

**Q9. G2, the board path's missing envelope checks?** Recommend fixing it first and
separately, because it is a security gap independent of B02: `_bind_envelope_to_the_call`
requires `caller_peer_id` present and compares `kind` (today it never does), and W3 runs
`verify_payload`'s checks 1-4 (version, kind, the 120-second window, the identity binding)
before dispatch. W4 builds complete payloads itself and needs no change beyond sharing the
check. The agree-trigger (2.4) then backs identity and kind at the database; freshness stays
this application check.

**Q10. One step or two?** Recommend two (A1, A2), with A2 only after every writer carries signed
bytes and after the rebuild's rollback window closes (4.1). A1 alone already guarantees
agreement for every row that carries bytes, and with Q12 it requires bytes from this build.

**Q11. Send `signed_bytes` to readers?** Recommend yes, under `include_provenance`, so a
client verifies the exact bytes instead of re-canonicalising a JSON round trip (1.5), and
derives status itself. Name the basis `bytes_bound`, not `signed` (2.5).

**Q12 (critique). Should A1's NULL admission be keyed to the inserting role?** Recommend yes:
refuse NULL `signed_bytes` from `meshy_app`, admit it from other roles until A2. It costs one
`current_user` comparison, closes the silent-downgrade window for this build's own writes from
the first day, and leaves direct-insert tests (which insert as the owner role, I: they insert into
`peers` and `dashboard_users` without a context; rival: a
fixture under `meshy_app`, which test 14's first run shows) as they are.

**Q13 (critique). Ship phase 0, the client-side status check, now and on its own?** Recommend
yes. It needs no migration, no divergence number and no answer to Q1-Q12; it covers historical
rows as well as new ones wherever the payload re-canonicalises; and it is the only check a
server cannot downgrade. It changes client output only (a new `status_verified` line), so it
needs a README line, not a divergence entry.

**Q14 (critique). What happens to a verdict when its ledger row is deleted by an ephemeral
rewrite or a prune?** Recommend: the verdict stays, keyed by the copied row identity (2.2), and
the 3.5 counts are taken on one snapshot of the rehearsal copy. The alternative, ON DELETE
CASCADE, erases a contradiction together with its evidence.

---

## 6. What this does not do

* It adds no Ed25519 verification to the database (2.4).
* It does not resolve #941, retention against signed counts (roadmap 1); A keeps shared_memory's
  evidence out of that decision's reach.
* It does not make `author` or `from` identities. `signed_by` stays the identity
  (`README.md:271-276`).
* It closes no STATUS row. B02 stays open until the operator answers section 5.

## 7. Probes run for this document (scratch, not committed)

| id | what | result |
|---|---|---|
| M1 | `rfc8785` 0.1.4 against the `sort_keys` on 16 values | differ on `1.0`, `100.0`, `1e-7`, `1e15`, `1e16`, `1e20`, `-0.0`, astral key order, 2^53+1; `rfc8785` raises on each integer beyond 2^53-1 tried (2^53+1, 10^16, 2^60, -2^53) |
| M2 | `json.dumps(p)::jsonb` read back by psycopg, SELECT only, dev server 16.14, not in recovery | `jcs` preserved except integral floats in exponent form (`1e16`, `1e20`, `1.5e300`); U+0000 refused; duplicate keys keep the last |
| P1 | W1 write then read of `{"n": 1e16}` and control `{"n": 1e15}`, through `tests/test_call_routing.py`'s fixtures | write completes; both reads `failed E_INTERNAL "result is not canonicalizable"`; retry HTTP 500; default-loader rebuild raises, I-JSON rebuild hash-matches and verifies; control reads `completed`, retries 202, rebuilds with the default loader |
| P2a | handler write to shared/wiki with no status and no author (`tests/test_memory_skills.py` fixtures) | row `OPEN` and `peer-<id8>`; neither in the signed args; the write result has no `author_label` or `tags` |
| P2b | `pre_discipline: "false"` with `claimed_status: "OPEN"` | row `pre_discipline = true`, `claimed_status = OPEN`, `status` NULL |
| P2c | `tags: " a, b ,,c"` | row `{a,b,c}` |
| P3 | `handlers.dispatch` with a signed payload lacking `caller_peer_id`, `kind`, `issued_at`; control naming another caller | lands, writer = signer, payload keys `args, call_id, skill_name, v`; control refused `E_BAD_ENVELOPE` |
| P4 | `wire.author_signature_ok` on memos differing from the signed args in status, tags, author_label; control differing in value | the three return `(True, None)`; the control `E_SIGNATURE_NOT_THE_MESSAGE` |
| SQL | `pg_proc.provolatile`; generated columns in a rolled-back transaction | `convert_from` STABLE, `sha256`/`encode`/`decode`/`jsonb_in` IMMUTABLE; generated column over `convert_from` refused, over an IMMUTABLE wrapper accepted; UPDATE of a generated column refused; `'1.0'::jsonb = '1'::jsonb` true |
| EXT | `pg_extension` and `pg_proc` on the dev database | `pg_trgm` 1.6, `pgcrypto` 1.3, `plpgsql` 1.0; zero Ed25519 functions |
| C2 | (critique) pure Python, no database: a payload with the int 10^16, hashed as the daemon does (`sort_keys`), then rebuilt from its JSON text with each loader and each canonicaliser; again with an astral key added | without the astral key: default+`sort_keys` matches, I-JSON+`rfc8785` matches, I-JSON+`sort_keys` does not, default+`rfc8785` raises `IntegerDomainError`. With it: only default+`sort_keys` matches |
| C3 | (critique) `json.dumps` against `rfc8785.dumps` on 5e-05, 1e-4, 1e-6, 1e-7, 123.5 | differ on `5e-05` / `0.00005` and `1e-06` / `0.000001`; agree on 1e-4 and 123.5 |
| C4 | (critique) `validation.validate_status` and `norm_tags` called directly at 0b6767f | a list or object status raises `TypeError` (not `HandlerError`); `5` raises `E_INVALID_STATUS`; `norm_tags([1, true, null, {"a": 1}, 1.0])` returns `['1', 'True', 'None', "{'a': 1}", '1.0']` |

---

## 8. Critique log

An adversarial review of this document against `origin/main` 0b6767f, which is also this
branch's base: the branch was already on current main, so no rebase was needed, and `git
status` showed no uncommitted edit, so there was no unfinished designer revision to fold in.
Every citation below was opened at 0b6767f unless it names another tree. The probes C2-C4 are
in section 7; the rest is M-read.

**Citations checked.** Opened and found accurate at 0b6767f: `handlers.py:717-752`, `lines 787`,
`lines 924-936`, `lines 956-968`, `lines 1400-1435`, `lines 1447-1467`, `lines 619-634`, `lines 1081-1083`, `lines 1339`;
`envelope.py:17-27`, `lines 157-238`; `pipeline.py:211-219`; `views.py:170`; `a2a/views.py:171`,
`lines 511-525`; `route.py:46-48`, `lines 157-167`, `lines 207-227`, `lines 283-288`; `result_signing.py:54-61`;
`primitives.py:63-65`; `repository.py:130`; `validation.py:359-375`, `lines 517-521`;
`sql/028:12-16`, `lines 27-31`, `lines 70-73`, `lines 88-97`, `lines 118-124`, `lines 130`, `lines 162-164`, `lines 276-281`;
`sql/063:29-43`, `lines 116-132`, `lines 143-161` (063 is still the live definition; 064 adds a separate
trigger and redefines nothing); `sql/107:15-21`; `sql/002:5`; `sqlmigrate.py:15-17`;
`README.md:265-276`; `node.py:619-624`; `wire.py:35`, `lines 335`, `lines 338`, `lines 348-349`, `lines 369`;
`dashboard/queries.py:208-214`; the project's design log (not part of this release); an internal project document (not part of this release), `lines 44`,
`lines 45`, `lines 57`, `lines 78-79`; `V1-DESIGN-RULES.md:32-36`, `lines 204-208`, `lines 229`; an internal project document (not part of this release);
Corrected: `wire.py:309-369` is `lines 298-369`;
the test census misses `tests/fixtures/meshsem_db.py`; B02 is not on main (0).

**Findings, most severe first. What changed is in the section named.**

1. **HIGH, security claim overstated (2.2, 2.5, Q11).** The verdict table's trigger was said to
   make "a fabricated verdict impossible without a SHA-256 preimage". The bytes that hash to
   `payload_jcs_sha256` can be computed by anyone from the stored payload, so a writer of the
   table can mark a failing row `verified`; the trigger stops fabricated bytes, not fabricated
   verdicts. Likewise `status_basis: signed` claimed a verification the database never made.
   Changed: the claim is corrected, the value renamed `bytes_bound`, verdicts are advisory, and
   the client's own verification is the anchor.
2. **HIGH, classifier defect, M-run (3.2, 3.3, test 8).** One I-JSON parse fed both
   canonicalisers. Historical rows holding an int beyond 2^53-1, the G5 rows, then match no
   candidate once the payload has any other `rfc8785`/`sort_keys` difference (C2). Changed:
   two loaders by two canonicalisers, the matching pair recorded.
3. **HIGH, cutover (4.1, 4.2, 4.3, Q10).** A2 refuses every unsigned write. The first draft
   gated A2 only on "nothing but this build writes". A2 has no scheduled date until every
   write carries signed bytes. And a data-only logical restore
   into an A2 schema refuses every historical row (2.4). Changed: A2 waits for the rebuild's
   rollback window to close; the restore rule is stated; the rollback runbook names A2's down
   script.
4. **MEDIUM-HIGH, downgrade (2.4, 4.1, Q12, test 14).** A1 admitted a NULL `signed_bytes` from
   anyone, so a regression that drops the column from this build's INSERT would quietly produce
   `legacy_unverified` rows and fail no test. Changed: A1 refuses NULL from `meshy_app`.
5. **MEDIUM, missed alternative (1.6, 2.5, 4.1 phase 0, Q13, test 18).** The client can derive
   and check status from the payload it already verifies, today, with no schema, for historical
   rows as well, and no server can downgrade it. It also records that this build's reference
   `read` is mesh-only and prints no status, so P4's exposure here is latent.
6. **MEDIUM, factual (1.1, 1.4, 1.7 G2, 2.3, Q9).** G2 was placed on W3 and W4. W4 signs complete
   payloads itself (`declare.py:249-277`, `lines 327`); only W3 is exposed. `kind` is never compared
   on W3, even when present, which the draft's "only when present" wording hid. The
   agree-trigger closes identity and kind at the database but not freshness. Exploiting the
   missing window still needs the signer's own bearer and DPoP key, so it is hardening, not an
   open bypass; the draft did not say either.
7. **MEDIUM, inferred as fact (2.6, 3.5.5, Q8).** "The historical violators stay visible and
   countable": the 197-row count dates from 2026-08-29; migration 107 began writing `result_payload`
   at f4512dfa7 (2026-09-06), and read-result retention nulls it. Pre-107 and pruned rows pass
   the CHECK blind. Changed: the claim, and the measurement split in two. Also checked the
   retention UPDATEs: they null `result_payload`, and `finish_call_atomic` writes the pair, so S
   preserves valid updates.
8. **MEDIUM, factual (3.4, 2.2, Q14, test 17).** "Nothing is deleted" is false outside keep_all:
   ephemeral writes and memory.prune DELETE ledger rows, and the verdict FK cascaded, which would
   erase a contradiction with its row. Changed: no cascade; row identity copied; counts on one
   snapshot.
9. **MEDIUM, measurement design (3.3, 3.5.2, Q6).** One `disagreeing` list conflated a signed
   status that contradicts its column (the #922 analogue) with a server default computed by an
   older daemon rule. That would inflate the number Q6 rules on. Changed: `disagreeing_signed`,
   `disagreeing_defaulted`, `binding_failures`, `key_revoked_before_write`.
10. **MEDIUM, test gap (test 7).** Checking that `signed_bytes` is in the immutability lists
    passes again the day another column is added or the function is redefined. Changed:
    a census against `information_schema.columns`.
11. **LOW-MEDIUM, understated result gap (1.3, 2.5, test 16).** `_replay_result` omits `status`,
    `pre_discipline` and `claimed_status`, so the server-signed result covers status on a first
    write only. G4's fix must reach it.
12. **LOW-MEDIUM, derivation gaps, M-run C4 (1.4, 1.7 G3, 2.3, Q3).** The status rule missed the
    custody refusal (`handlers.py:753-758`); a list status raises `TypeError` and answers
    `E_INTERNAL`; list tags store Python reprs; `pre_discipline: "false"` becomes a custody row
    only for an admin.
13. **LOW (1.2).** Non-integral floats in [1e-6, 1e-4) can serialise differently (C3).
14. **LOW (2.2).** The IMMUTABLE wrapper was unnecessary for a trigger and true only
    conditionally; now STABLE. The verdict table's append-only property now has a mechanism
    (008-shaped policies, `REVOKE UPDATE, DELETE`).
15. **LOW (4.1).** The cost bound of 65,536 bytes holds for mesh bodies only.
16. **LOW, main moved (2.2, 2.5).** Built on what landed since the first draft's base: the
    `sql_meshychat/011` trigger as the pattern and the thing this strengthens; divergences 031
    and 033 frames and addressed waits stay unverified pointers; 8bdf831's
    `E_SIGNATURE_KEY_UNAVAILABLE` for a rotated key; cccc77d's `signed_by` unchanged as identity.
    Divergence 030's revocation closes change nothing here beyond the key-state check in 3.3.

**Kept unchanged, because they held up.** The four write paths and the re-verification in the
handler (1.1); that the re-canonicalised bytes ARE the signed message (1.2); the column-by-column
table's substance (1.4); the immutability-trigger trap (1.4); option A over B, C and D (2.1);
the plain columns and an agree-trigger instead of generated columns (Q2); no fabricated bytes and
no server-signed historical status (3.4, Q7); the big-number seam refusal (Q4); keeping the
server-stamped author label and signing it (Q5); the NOT VALID CHECK itself (Q8); the
counts-only discipline of 3.5, never on the standby.

**Not re-run by the critique.** P1, P2a-c, P3, P4, M1, M2, SQL and EXT are the designer's; this
review read the code they exercise and found it consistent with their results, but did not
repeat them. G5 is still inferred (the `serde_jcs` source is still unread).
