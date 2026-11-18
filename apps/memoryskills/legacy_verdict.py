"""Classify shared_memory rows that carry no `signed_bytes`. Divergence 039; design
docs/design/signed-bytes-status.md (fin/design-signed-bytes) sections 3.2 and 3.3, Q6, Q7,
Q14, and critique findings 2 and 9.

WHICH ROWS. Every row with NULL `signed_bytes`: written before sql_meshychat/014, or since
by a session without this build's session marker (A1 admitted both `meshy` and `meshy_app`, now `meshychat_owner` and `meshychat_app`).
Nothing here rewrites, re-signs or deletes a ledger row; it appends one verdict per (row,
CLASSIFIER_VERSION) to shared_memory_legacy_verdict, whose identity columns are COPIED from
the row, so a verdict outlives an ephemeral rewrite or a prune of its row.

THE STEPS, design 3.3:
  1. parse `provenance_jsonb::text` twice, with json.loads and with the I-JSON loader
     (apps/crypto/ijson.loads_ijson: an integer beyond 2**53 - 1 becomes a float). BOTH,
     because JSONB keeps a number's value and not its type: 1e16 can become the int 10**16
     before sort_keys hashing or a float before RFC 8785 hashing. One parse files one of
     the two as no_hash_match.
  2. no payload, signature or kid: `no_envelope`.
  3. the four candidates {json_default, ijson_float} x {rfc8785 (skipped if it raises),
     sort_keys}; none hashing to payload_jcs_sha256: `no_hash_match`.
  4. verify the matching bytes with verify_strict under peer_keys (created_by_peer_id,
     signing_kid) in ANY state, revoked included, and require that key to be the one the
     row's provenance names: the key is only as good as the table, not the row. Fails:
     `hash_bound_signature_fails` (with `lenient_accepts` when libsodium's plain verify
     would have taken it). Passes: `verified`, with the bytes.
  5. for a verified row, the binding the handler checks: kind skill.call, skill_name
     memory.write, the signer is created_by_peer_id, and the signed namespace, key and
     value are the row's. A failure is `binding_failures`: that signature is evidence for
     some other envelope.
  6. design 2.3's derivations (apps/memoryskills/signed_bytes.derive_fields), split: a
     field whose arg the writer SIGNED and whose column differs is `disagreeing_signed`;
     one the server DEFAULTED or normalised (the wiki OPEN, comma tags), or whose signed
     arg is a JSON type 2.3 refuses and a retired rule read, is
     `disagreeing_defaulted`: rule drift, not a contradiction.
  7. `key_revoked_before_write`: the key's revoked_at is earlier than the row's created_at.

A verdict is ADVISORY. The bytes it records are bound to the row by the table's trigger,
but anyone can compute them from the stored payload, so a writer of the table could
fabricate a `verified`. A reader that wants proof verifies the bytes itself.

THE JOB. `catch_up` is the registered job body (apps/jobs/registry.py): one transaction
under `rls.projection_writer`, the oldest unclassified rows first, at most `max_batch` per
tick, and the runner's statement_timeout (rule E7). `census` is the counts-only dry run of
design section 3.5 measurement 2: it writes nothing, runs in a read-only session, works on a
database that has not applied sql_meshychat/014, and refuses a standby.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from collections import Counter

from psycopg.rows import dict_row

from apps.core import rls
from apps.crypto import primitives as P
from apps.crypto.ijson import loads_ijson

from .signed_bytes import derive_fields
from .validation import norm_tags

#: Bumped whenever a rule below changes; a verdict is read only for the current version.
CLASSIFIER_VERSION = "b02-a1.1"

JOB_NAME = "shared_memory_legacy_verdict"
DEFAULT_BATCH = 500

VERDICTS = ("verified", "hash_bound_signature_fails", "no_hash_match", "no_envelope")


def sort_keys_bytes(obj) -> bytes:
    """The legacy writer's hash input: `json.dumps` with `sort_keys`, which it called RFC
    8785 and is not."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False).encode("utf-8")


LOADERS = (("json_default", json.loads), ("ijson_float", loads_ijson))
CANONICALIZERS = (("rfc8785", P.jcs), ("sort_keys", sort_keys_bytes))


@dataclasses.dataclass
class Verdict:
    verdict: str
    loader: str | None = None
    canonicalizer: str | None = None
    verified_bytes: bytes | None = None
    verified_kid: str | None = None
    key_revoked_before_write: bool | None = None
    lenient_accepts: bool | None = None
    binding_failures: list = dataclasses.field(default_factory=list)
    disagreeing_signed: list = dataclasses.field(default_factory=list)
    disagreeing_defaulted: list = dataclasses.field(default_factory=list)


def json_equal(a, b) -> bool:
    """JSON equality: numbers by value (1 == 1.0, as JSONB and RFC 8785 identify them), a
    boolean never equal to a number (Python's True == 1 is not JSON's)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _parse(loader, text):
    try:
        return loader(text)
    except (ValueError, TypeError):
        return None


def _lenient(pub: bytes, message: bytes, signature: bytes) -> bool:
    """libsodium's own verify, WITHOUT verify_strict's small-order and scalar checks."""
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    try:
        VerifyKey(pub).verify(message, signature)
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def _same_peer(a, b) -> bool:
    try:
        return uuid.UUID(str(a)) == uuid.UUID(str(b))
    except (ValueError, TypeError, AttributeError):
        return False


def _binding(p: dict, row: dict, loader) -> list[str]:
    failures = []
    if p.get("kind") != "skill.call":
        failures.append("kind")
    if p.get("skill_name") != "memory.write":
        failures.append("skill_name")
    if not _same_peer(p.get("caller_peer_id"), row["created_by_peer_id"]):
        failures.append("caller_peer_id")
    args = p.get("args") if isinstance(p.get("args"), dict) else {}
    if args.get("namespace") != row["namespace"]:
        failures.append("namespace")
    if args.get("key") != row["key"]:
        failures.append("key")
    signed_value = args["value"] if "value" in args else args.get("body")
    column_value = _parse(loader, row["value_text"])
    if not json_equal(signed_value, column_value):
        failures.append("value")
    return failures


def _disagreements(p: dict, row: dict) -> tuple[list[str], list[str]]:
    derived = derive_fields(p)
    args = p.get("args") if isinstance(p.get("args"), dict) else {}
    signed, defaulted = [], []
    columns = {"pre_discipline": row["pre_discipline"], "status": row["status"],
               "claimed_status": row["claimed_status"]}
    for field in ("pre_discipline", "status", "claimed_status"):
        d = derived[field]
        if d.refused:
            defaulted.append(field)
        elif d.value != columns[field]:
            (signed if d.signed else defaulted).append(field)
    d = derived["author_label"]
    if d.refused:
        defaulted.append("author_label")
    elif d.value is not None and d.value != row["author_label"]:
        signed.append("author_label")
    d = derived["tags"]
    column_tags = list(row["tags"] or [])
    if isinstance(args.get("tags"), str):
        if norm_tags(args["tags"]) != column_tags:
            defaulted.append("tags")
    elif d.refused:
        defaulted.append("tags")
    elif d.value != column_tags:
        (signed if d.signed else defaulted).append("tags")
    return signed, defaulted


def classify(row: dict, key_of) -> Verdict:
    """One row's verdict. `row` carries the columns `_SELECT` reads; `key_of(peer_id, kid)`
    returns (public_key_b64url, revoked_at) from peer_keys, in any state, or None."""
    parsed = {name: _parse(load, row["provenance_text"]) for name, load in LOADERS}
    base = parsed["json_default"] if isinstance(parsed["json_default"], dict) else None
    base = base or (parsed["ijson_float"] if isinstance(parsed["ijson_float"], dict) else None)
    if base is None:
        return Verdict("no_envelope")
    signature_b64, kid = base.get("signature"), base.get("signing_kid")
    if (not isinstance(base.get("payload"), dict) or not isinstance(signature_b64, str)
            or not signature_b64 or not isinstance(kid, str) or not kid):
        return Verdict("no_envelope")

    match = None
    for lname, load in LOADERS:
        prov = parsed[lname]
        if not isinstance(prov, dict) or not isinstance(prov.get("payload"), dict):
            continue
        for cname, canonicalize in CANONICALIZERS:
            try:
                raw = canonicalize(prov["payload"])
            except (ValueError, TypeError):
                continue
            if hashlib.sha256(raw).hexdigest() == row["payload_jcs_sha256"]:
                match = (lname, load, cname, raw, prov["payload"])
                break
        if match:
            break
    if match is None:
        return Verdict("no_hash_match")
    lname, load, cname, raw, payload = match

    out = Verdict("hash_bound_signature_fails", loader=lname, canonicalizer=cname)
    key = key_of(row["created_by_peer_id"], kid)
    if key is None:
        return out
    pub_b64, revoked_at = key
    out.key_revoked_before_write = bool(revoked_at is not None
                                        and revoked_at < row["created_at"])
    if base.get("public_key_b64url") != pub_b64:
        return out
    try:
        pub, signature = P.b64u_decode(pub_b64), P.b64u_decode(signature_b64)
    except (ValueError, TypeError):
        return out
    if not P.verify_strict(pub, raw, signature):
        out.lenient_accepts = _lenient(pub, raw, signature)
        return out
    out.verdict, out.verified_bytes, out.verified_kid = "verified", raw, kid
    out.binding_failures = _binding(payload, row, load)
    out.disagreeing_signed, out.disagreeing_defaulted = _disagreements(payload, row)
    return out


# --------------------------------------------------------------------------- the reader
# Design 2.5, Q6, Q7 and Q11: what memory.read and memory.query add under include_provenance.

BYTES_BOUND = "bytes_bound"
LEGACY_VERIFIED = "legacy_verified"
LEGACY_CONTRADICTS = "legacy_contradicts"
LEGACY_UNVERIFIED = "legacy_unverified"

#: The current verdict of the row being read, as one JSON column. CLASSIFIER_VERSION is a
#: constant of this module, never input, and is checked to be a plain token before it is
#: written into SQL, so the read queries' parameter lists stay as they were.
assert all(c.isalnum() or c in ".-_" for c in CLASSIFIER_VERSION), CLASSIFIER_VERSION
VERDICT_COLUMN_SQL = (
    "(SELECT json_build_object('verdict', v.verdict, "
    "        'binding_failures', v.binding_failures, "
    "        'disagreeing_signed', v.disagreeing_signed, "
    "        'disagreeing_defaulted', v.disagreeing_defaulted, "
    "        'key_revoked_before_write', v.key_revoked_before_write) "
    " FROM shared_memory_legacy_verdict v "
    " WHERE v.row_id = shared_memory.id "
    f"  AND v.classifier_version = '{CLASSIFIER_VERSION}') AS legacy_verdict")


def status_basis(signed_bytes, verdict: dict | None, provenance) -> dict:
    """The fields a reader gets beside the memo's columns, which are served unchanged.

    `bytes_bound`: the row has signed_bytes, and the database's agree-trigger held every
    derived column to them but one: tags signed as a comma-separated string, which the
    trigger does not check and nothing re-checks on a row with bytes yet. It does NOT say
    the signature verified: the database cannot check one, and the handler's check at
    insert leaves no record the database holds. The reader verifies `signed_bytes` itself
    and derives the fields from them (design 2.5, critique finding 1).

    A row without bytes is served by its verdict (Q6 as amended): `legacy_verified` when the
    signature verified under a key not revoked before the write, the envelope is bound to
    this row, and no SIGNED field differs (a defaulted one is noted beside it);
    `legacy_contradicts` when a signed field differs, with the signed value beside the
    column's; `legacy_unverified` otherwise, naming why. A verdict is advisory; the proof
    is the reader's own check of the payload in `provenance`."""
    if signed_bytes is not None:
        return {"signed_bytes": P.b64u(bytes(signed_bytes)), "status_basis": BYTES_BOUND}
    detail = {"classifier_version": CLASSIFIER_VERSION}
    out = {"signed_bytes": None, "status_basis": LEGACY_UNVERIFIED,
           "status_basis_detail": detail}
    if verdict is None:
        detail["reason"] = ["not_classified"]
        return out
    detail["verdict"] = verdict["verdict"]
    if verdict["verdict"] != "verified":
        detail["reason"] = [verdict["verdict"]]
        return out
    if verdict["binding_failures"]:
        detail["reason"] = ["binding_failures"]
        detail["binding_failures"] = list(verdict["binding_failures"])
        return out
    if verdict["key_revoked_before_write"]:
        detail["reason"] = ["key_revoked_before_write"]
        return out
    if verdict["disagreeing_defaulted"]:
        detail["defaulted_differs"] = list(verdict["disagreeing_defaulted"])
    if verdict["disagreeing_signed"]:
        payload = provenance.get("payload") if isinstance(provenance, dict) else None
        derived = derive_fields(payload if isinstance(payload, dict) else {})
        detail["signed"] = {f: derived[f].value for f in verdict["disagreeing_signed"]}
        out["status_basis"] = LEGACY_CONTRADICTS
        return out
    out["status_basis"] = LEGACY_VERIFIED
    return out


# --------------------------------------------------------------------------- the database

_COLUMNS = ("sm.id, sm.namespace, sm.key, sm.version, sm.created_at, sm.payload_jcs_sha256, "
            "sm.provenance_jsonb::text AS provenance_text, sm.value::text AS value_text, "
            "sm.status, sm.pre_discipline, sm.claimed_status, sm.author_label, sm.tags, "
            "sm.created_by_peer_id")

#: A redacted row (sql_meshychat/027, divergence 053) has NULL `signed_bytes` because its signed bytes ARE
#: the text that was withdrawn, not because it never had a signature. Left to the job it would be
#: reported `no_envelope`, and a reader of the verdict would take a deliberate redaction for a row that
#: was never signed (design section 5, test 14). A redaction record for the row means it is not
#: historical and gets no verdict.
_SKIP_REDACTED = ("  AND NOT EXISTS (SELECT 1 FROM shared_memory_redaction r "
                  "                  WHERE r.memory_id = sm.id) ")


def _select_unclassified(skip_redacted: bool) -> str:
    return (f"SELECT {_COLUMNS} FROM shared_memory sm "
            "WHERE sm.signed_bytes IS NULL "
            + (_SKIP_REDACTED if skip_redacted else "") +
            "  AND NOT EXISTS (SELECT 1 FROM shared_memory_legacy_verdict v "
            "                  WHERE v.row_id = sm.id AND v.classifier_version = %(version)s) "
            "ORDER BY sm.created_at, sm.id LIMIT %(limit)s")


def _has_redaction_records(cur) -> bool:
    """Whether sql_meshychat/027 is applied. The job also runs on a schema older than it, where
    there is no record table to skip by and nothing was ever redacted."""
    cur.execute("SELECT to_regclass('public.shared_memory_redaction') IS NOT NULL AS present")
    return bool(cur.fetchone()["present"])

_INSERT = (
    "INSERT INTO shared_memory_legacy_verdict (row_id, row_namespace, row_key, row_version, "
    " row_created_at, row_payload_jcs_sha256, classifier_version, verdict, loader, "
    " canonicalizer, verified_bytes, verified_kid, key_revoked_before_write, lenient_accepts, "
    " binding_failures, disagreeing_signed, disagreeing_defaulted) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")


def _key_lookup(cur, rows: list[dict]):
    peers = sorted({str(r["created_by_peer_id"]) for r in rows})
    keys: dict[tuple[str, str], tuple] = {}
    if peers:
        cur.execute("SELECT peer_id, kid, public_key, revoked_at FROM peer_keys "
                    "WHERE peer_id = ANY(%s::uuid[])", (peers,))
        for r in cur.fetchall():
            keys[(str(r["peer_id"]), r["kid"])] = (r["public_key"], r["revoked_at"])

    def key_of(peer_id, kid):
        return keys.get((str(peer_id), kid))
    return key_of


@dataclasses.dataclass(frozen=True)
class Report:
    rows_read: int
    by_verdict: dict
    more: bool


def catch_up(conn, *, max_batch: int = DEFAULT_BATCH) -> Report:
    """Classify the oldest `max_batch` unclassified rows and append their verdicts, in one
    transaction under the job-only writer context (sql_meshychat/008's split, divergence
    022). `conn` is an autocommit meshychat_app connection with no transaction open."""
    by_verdict: Counter = Counter()
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_select_unclassified(_has_redaction_records(cur)),
                        {"version": CLASSIFIER_VERSION, "limit": int(max_batch)})
            rows = cur.fetchall()
            key_of = _key_lookup(cur, rows)
            params = []
            for row in rows:
                v = classify(row, key_of)
                by_verdict[v.verdict] += 1
                params.append((row["id"], row["namespace"], row["key"], row["version"],
                               row["created_at"], row["payload_jcs_sha256"],
                               CLASSIFIER_VERSION, v.verdict, v.loader, v.canonicalizer,
                               v.verified_bytes, v.verified_kid, v.key_revoked_before_write,
                               v.lenient_accepts, v.binding_failures, v.disagreeing_signed,
                               v.disagreeing_defaulted))
            if params:
                cur.executemany(_INSERT, params)
    return Report(rows_read=len(rows), by_verdict=dict(by_verdict),
                  more=len(rows) >= max_batch)


def classify_ids(conn, ids) -> dict:
    """{row id: Verdict} for the given shared_memory rows, whatever their signed_bytes, and
    writing nothing. Needs neither sql_meshychat/014's column nor its table, so it answers on
    a database A1's down script has been applied to (design 4.3: C recovers A1's bytes)."""
    with rls.server_internal(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM shared_memory sm WHERE sm.id = ANY(%s::uuid[])",
                        ([str(i) for i in ids],))
            rows = cur.fetchall()
            key_of = _key_lookup(cur, rows)
            return {row["id"]: classify(row, key_of) for row in rows}


def run_job(conn, job) -> int:
    """The registered job body; returns rows read, which the runner compares to max_batch."""
    return catch_up(conn, max_batch=job.max_batch).rows_read


def _tally(tally: Counter, v: Verdict) -> None:
    tally[v.verdict] += 1
    if v.verdict == "verified":
        if "status" in v.disagreeing_signed:
            tally["verified_status_signed_disagreement"] += 1
        if v.disagreeing_signed:
            tally["verified_any_signed_disagreement"] += 1
        if v.disagreeing_defaulted:
            tally["verified_any_defaulted_disagreement"] += 1
        if v.binding_failures:
            tally["verified_binding_failures"] += 1
        if v.key_revoked_before_write:
            tally["verified_key_revoked_before_write"] += 1
    if v.lenient_accepts:
        tally["signature_fails_but_lenient_accepts"] += 1
    if v.loader and v.canonicalizer:
        tally[f"matched_{v.loader}_{v.canonicalizer}"] += 1


def counts(conn, classifier_version: str = CLASSIFIER_VERSION) -> dict:
    """The stored verdicts, counted, deleted ledger rows included (design Q14). Measurement
    3.5.2's numbers once the job has run; never any row content."""
    tally: Counter = Counter({v: 0 for v in VERDICTS})
    with rls.server_internal(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT verdict, loader, canonicalizer, key_revoked_before_write, "
                        "       lenient_accepts, binding_failures, disagreeing_signed, "
                        "       disagreeing_defaulted "
                        "FROM shared_memory_legacy_verdict WHERE classifier_version = %s",
                        (classifier_version,))
            for r in cur.fetchall():
                _tally(tally, Verdict(**r))
    return dict(tally)


def census(conn, *, batch: int = 1000) -> dict:
    """Measurement 3.5.2 as a DRY RUN: classify every row without bytes and count, writing
    nothing. For a database that may not have applied sql_meshychat/014 (then every row is
    historical). The caller supplies a read-only session; this refuses a standby."""
    tally: Counter = Counter({v: 0 for v in VERDICTS})
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT pg_is_in_recovery() AS standby")
        if cur.fetchone()["standby"]:
            raise RuntimeError("refusing to run on a standby (design 3.5: never on the standby)")
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'shared_memory' AND column_name = 'signed_bytes'")
        has_bytes = cur.fetchone() is not None
        where = "sm.signed_bytes IS NULL" if has_bytes else "TRUE"
        if has_bytes and _has_redaction_records(cur):     # a redacted row is not historical (see _SKIP_REDACTED)
            where += " AND NOT EXISTS (SELECT 1 FROM shared_memory_redaction r WHERE r.memory_id = sm.id)"
        after = None
        while True:
            cur.execute(
                f"SELECT {_COLUMNS} FROM shared_memory sm WHERE {where} "
                "AND (%(after_at)s::timestamptz IS NULL "
                "     OR (sm.created_at, sm.id) > (%(after_at)s::timestamptz, %(after_id)s::uuid)) "
                "ORDER BY sm.created_at, sm.id LIMIT %(limit)s",
                {"after_at": after[0] if after else None,
                 "after_id": after[1] if after else None, "limit": batch})
            rows = cur.fetchall()
            if not rows:
                break
            key_of = _key_lookup(cur, rows)
            for row in rows:
                _tally(tally, classify(row, key_of))
            after = (rows[-1]["created_at"], rows[-1]["id"])
    tally["rows_classified"] = sum(tally[v] for v in VERDICTS)
    tally["schema_has_signed_bytes"] = int(has_bytes)
    return dict(tally)


def main(argv=None) -> int:
    """`python -m apps.memoryskills census --dsn DSN` (apps/memoryskills/__main__.py):
    counts only, in a read-only session as meshychat_app under server_internal. `counts` reads
    the stored verdicts; `catch-up` runs one job tick."""
    import argparse
    import sys

    from apps.core import dbconn

    ap = argparse.ArgumentParser(prog="python -m apps.memoryskills")
    ap.add_argument("command", choices=("census", "catch-up", "counts"))
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--max-batch", type=int, default=DEFAULT_BATCH)
    a = ap.parse_args(argv)
    conn = dbconn.connect(a.dsn or dbconn.dsn())
    try:
        if a.command == "census":
            conn.execute("SET default_transaction_read_only = on")
            with rls.server_internal(conn):
                out = census(conn)
        elif a.command == "counts":
            out = counts(conn)
        else:
            out = dataclasses.asdict(catch_up(conn, max_batch=a.max_batch))
    finally:
        conn.close()
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0
