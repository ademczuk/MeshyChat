"""Private, transport-free frozen batches and durable reconciliation state.

Admission, complete context.v1 validation/screens, source identity and signed
read verification belong to the caller. This component does not authenticate an
observation. Call observe ONLY after the existing reader has verified namespace,
seat/author, exact key/head/version and (for redaction) authorized provenance.
Evidence digests refer to that caller's durable verification artifact, not proof
by themselves. No network client or native source reader here; completed-batch
evidence artifacts may be pruned explicitly via prune_completed_evidence.
"""
import base64
from dataclasses import dataclass, field
import hashlib
import json
import re
import uuid

from incremental_store import (
    Store, Snapshot, StoreError, StoreConflict, StoreIOError, StoreBusy, StoreFull, _private_errors, _opaque,
)


FORMAT = "transcript-frozen-outbox.v1"
ITEM = re.compile(r"ctx/([a-z0-9][a-z0-9_.-]{0,63})/([0-9a-f]{32})/([0-9]{4})/([0-9]{6})/([0-9]{10})")
REFUSALS = {"E_CONTEXT_SECRET": "secret", "E_CONTEXT_CLIENT_NAME": "client_name"}
MAX_EVIDENCE_BYTES = 512 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _same_value(expected, actual):
    # Python equality alone equates True, 1 and 1.0. Context coordinates require
    # integers; arrays retain their order and strings retain their code points.
    if type(expected) is not type(actual):
        return False
    if type(expected) is dict:
        return expected.keys() == actual.keys() and all(
            _same_value(value, actual[key]) for key, value in expected.items())
    if type(expected) is list:
        return len(expected) == len(actual) and all(
            _same_value(left, right) for left, right in zip(expected, actual))
    return expected == actual


def _matches_readback(frozen, observed):
    """Compare strict JSON values, independently of wire/member ordering.

    This is not signature canonicalization. The caller authenticates the read;
    the original frozen bytes remain the only bytes eligible for retry.
    """
    if not 0 < len(observed) <= 65536:
        return False
    try:
        actual = json.loads(observed.decode("utf-8"), object_pairs_hook=_unique_object)
        # Reject non-finite numbers and lone surrogates accepted by json.loads.
        _json(actual)
        return _same_value(json.loads(frozen), actual)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False


def _replacement(records, position, item, manifest, code):
    """Guard a caller-screened, minimal omission; no screen bypass here."""
    reason = REFUSALS.get(code) if type(code) is str else None
    if reason is None or not 0 <= position < len(records) - 1:
        raise StoreConflict("only an item screening refusal permits replacement")
    old = json.loads(records[position].value)
    if old.get("item") not in ("user_text", "assistant_text", "task", "artifact"):
        raise StoreConflict("an omission cannot be replaced again")
    required = ("schema", "kind", "seat", "src", "gen", "batch", "ordinal",
                "dialect", "adapter_version", "time")
    if any(key not in old for key in required):
        raise StoreConflict("required omission metadata missing")
    minimal = {key: old[key] for key in required}
    minimal.update(item="omitted", omitted_reason=reason)
    prepared = list(records)
    prepared[position], prepared[-1] = item, manifest
    _records(tuple(prepared[:-1]), prepared[-1])
    if item.key != records[position].key or json.loads(item.value) != minimal:
        raise StoreConflict("replacement must preserve identity and strip derived metadata")
    expected = json.loads(records[-1].value)
    counts = expected.get("counts")
    names = {"written", "omitted_secret", "omitted_client_name", "omitted_too_large"}
    if (type(counts) is not dict or set(counts) != names or
            any(type(n) is not int or n < 0 for n in counts.values()) or counts["written"] < 1):
        raise StoreConflict("valid original manifest counts required")
    actual = dict.fromkeys(names, 0)
    for record in records[:-1]:
        value = json.loads(record.value)
        name = "omitted_" + str(value.get("omitted_reason")) if value.get("item") == "omitted" else "written"
        if name not in actual:
            raise StoreConflict("original omission reason invalid")
        actual[name] += 1
    if counts != actual:
        raise StoreConflict("original manifest counts disagree with items")
    expected["items"][position][1] = None
    counts["written"] -= 1
    counts["omitted_" + reason] += 1
    if manifest.key != records[-1].key or json.loads(manifest.value) != expected:
        raise StoreConflict("replacement manifest must change only this digest and counts")
    return tuple(prepared)


@dataclass(frozen=True)
class Record:
    key: str
    value: bytes = field(repr=False)


@dataclass(frozen=True)
class Attempt:
    batch: str
    position: int
    token: str


@dataclass(frozen=True)
class Action:
    kind: str
    batch: str
    record: Record | None = field(default=None, repr=False)
    attempt: Attempt | None = None


def _records(items, manifest):
    """Structural guard only; never substitutes for the admission validator."""
    if type(items) is not tuple or not 1 <= len(items) <= 256:
        raise StoreError("one to 256 prepared items required")
    records = items + (manifest,)
    values, coordinates, ordinals = [], None, []
    for position, record in enumerate(records):
        if not isinstance(record, Record) or type(record.value) is not bytes or not 0 < len(record.value) <= 65536:
            raise StoreError("bounded prepared wire record required")
        try:
            value = json.loads(record.value)
            if type(value) is not dict or _json(value) != record.value:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise StoreError("compact UTF-8 JSON object required") from None
        if position < len(items):
            match = ITEM.fullmatch(record.key) if type(record.key) is str else None
            if not match:
                raise StoreError("prepared item key invalid")
            seat, src, gen, batch, ordinal = match.groups()
            current = seat, src, gen, batch
            if int(gen) == 0 or int(batch) == 0 or (coordinates is not None and coordinates != current):
                raise StoreError("prepared batch coordinates disagree")
            coordinates = current
            ordinals.append(int(ordinal))
            expected = dict(schema="context.v1", kind="item", seat=seat,
                            src=src, gen=int(gen), batch=int(batch), ordinal=int(ordinal))
        else:
            seat, src, gen, batch = coordinates
            if record.key != f"ctxm/{seat}/{src}/{gen}/{batch}":
                raise StoreError("prepared manifest key disagrees")
            expected = dict(schema="context.v1", kind="manifest", seat=seat,
                            src=src, gen=int(gen), batch=int(batch))
        if any(type(value.get(key)) is not type(expected_value) or value[key] != expected_value
               for key, expected_value in expected.items()):
            raise StoreError("prepared key and value disagree")
        values.append(value)
    if ordinals != list(range(ordinals[0], ordinals[0] + len(items))):
        raise StoreError("prepared ordinals must be contiguous")
    pairs = [[ordinal, value.get("text_sha256")] for ordinal, value in zip(ordinals, values)]
    summary = values[-1]
    links = summary.get("items")
    if (type(summary.get("first")) is not int or type(summary.get("last")) is not int or
            type(links) is not list or any(type(pair) is not list or len(pair) != 2 or
                                         type(pair[0]) is not int for pair in links) or
            (summary["first"], summary["last"], links) != (ordinals[0], ordinals[-1], pairs)):
        raise StoreError("prepared manifest linkage disagrees")
    return records, coordinates, ordinals


class FrozenOutbox:
    def __init__(self, store):
        if not isinstance(store, Store):
            raise StoreError("private incremental store required")
        self.store = store
        # Decoded, validated frozen records per (batch key, output sha256), before omission overlays (#426).
        # _committed re-verifies the output bytes against that digest on every load, so a hit is the same bytes.
        # Values are (records, committed output + drops bytes); the whole cache shares the store's output budget.
        self._decoded = {}
        self._decoded_bytes = 0
        self._initialize()

    @_private_errors
    def _initialize(self):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS outbox_receipts_v1(
                batch TEXT NOT NULL, position INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','ready','matched','redacted','conflict')),
                token TEXT NOT NULL, evidence TEXT,
                PRIMARY KEY(batch,position))""")
            db.execute("""CREATE TABLE IF NOT EXISTS outbox_attempt_guards_v1(
                batch TEXT NOT NULL, position INTEGER NOT NULL, token TEXT NOT NULL,
                first_attempt INTEGER NOT NULL CHECK(first_attempt IN (0,1)),
                PRIMARY KEY(batch,position))""")
            db.execute("""CREATE TABLE IF NOT EXISTS outbox_omissions_v1(
                batch TEXT NOT NULL, position INTEGER NOT NULL, code TEXT NOT NULL,
                token TEXT NOT NULL, evidence TEXT NOT NULL,
                old_item_sha TEXT NOT NULL, old_manifest_sha TEXT NOT NULL,
                item BLOB NOT NULL, manifest BLOB NOT NULL,
                item_sha TEXT NOT NULL, manifest_sha TEXT NOT NULL,
                PRIMARY KEY(batch,position))""")
            db.execute("""CREATE TABLE IF NOT EXISTS outbox_evidence_v1(
                digest TEXT PRIMARY KEY, artifact BLOB NOT NULL)""")
            db.commit()

    @_private_errors
    def freeze(self, expected, safe_offset, items, manifest, checkpoint, *, boundary=None):
        """Freeze exact caller-approved bytes with the source checkpoint atomically.

        One local revision is one wire batch; this prototype starts a fresh wire
        generation at batch one. Existing non-outbox revisions are incompatible.
        """
        if not isinstance(expected, Snapshot):
            raise StoreError("incremental source snapshot required")
        records, coordinates, ordinals = _records(items, manifest)
        if int(coordinates[-1]) != expected.revision + 1:
            raise StoreConflict("wire batch and local revision disagree")
        if expected.revision:
            with self.store._connection() as db:
                previous = self._load(db, expected.batch)[1]
            _, prior_coords, prior_ordinals = _records(previous[:-1], previous[-1])
            if coordinates[:3] != prior_coords[:3] or int(ordinals[0]) != int(prior_ordinals[-1]) + 1:
                raise StoreConflict("wire generation or source ordinal continuity disagrees")
        elif int(ordinals[0]) != 0:
            raise StoreConflict("first wire batch must start at ordinal zero")
        output = _json(dict(schema=FORMAT, records=[
            [record.key, base64.b64encode(record.value).decode("ascii")] for record in records]))
        return self.store.commit(expected, safe_offset, output, b"[]", checkpoint, boundary=boundary)

    def _load(self, db, key):
        _opaque(key)
        batch, output, drops = self.store._committed(db, key)
        try:
            if drops != b"[]":
                raise ValueError()
            cache_key = (key, batch.output_sha256)
            cached = self._decoded.get(cache_key)
            records = cached[0] if cached is not None else None
            if records is None:
                value = json.loads(output)
                if value["schema"] != FORMAT or set(value) != {"schema", "records"}:
                    raise ValueError()
                records = tuple(Record(k, base64.b64decode(v, validate=True)) for k, v in value["records"])
                _records(records[:-1], records[-1])
                charge = len(output) + len(drops)       # upper bound on the decoded records' retained bytes
                while self._decoded and self._decoded_bytes + charge > self.store.limits.output_bytes:
                    _, old_charge = self._decoded.pop(next(iter(self._decoded)))
                    self._decoded_bytes -= old_charge
                self._decoded[cache_key] = (records, charge)
                self._decoded_bytes += charge
            count, size = db.execute("""SELECT COUNT(*),COALESCE(SUM(length(item)+length(manifest)),0)
                FROM outbox_omissions_v1 WHERE batch=?""", (key,)).fetchone()
            if count >= len(records) or size > self.store.limits.output_bytes:
                raise StoreConflict("retained omission overlay exceeds bounds")
            rows = db.execute("""SELECT position,code,old_item_sha,old_manifest_sha,
                item,manifest,item_sha,manifest_sha FROM outbox_omissions_v1
                WHERE batch=? ORDER BY position""", (key,)).fetchall()
            for position, code, old_item_sha, old_manifest_sha, item, manifest, item_sha, manifest_sha in rows:
                if (type(position) is not int or not 0 <= position < len(records) - 1 or
                        hashlib.sha256(records[position].value).hexdigest() != old_item_sha or
                        hashlib.sha256(records[-1].value).hexdigest() != old_manifest_sha or
                        hashlib.sha256(item).hexdigest() != item_sha or
                        hashlib.sha256(manifest).hexdigest() != manifest_sha):
                    raise StoreConflict("omission overlay digest disagreement")
                records = _replacement(records, position, Record(records[position].key, item),
                                       Record(records[-1].key, manifest), code)
        except (KeyError, IndexError, ValueError, TypeError, UnicodeError, RecursionError):
            raise StoreConflict("committed output is not a valid frozen bundle") from None
        return batch, records

    def _next(self, db, key):
        batch, records = self._load(db, key)
        # Never let a later manifest (or item) race an unconfirmed predecessor.
        if batch.revision > 1:
            prior = db.execute("SELECT key FROM batches WHERE source=? AND generation=? AND revision=?",
                               (batch.source, batch.generation, batch.revision - 1)).fetchone()
            if prior is None:
                raise StoreConflict("frozen predecessor missing")
            _, prior_records = self._load(db, prior[0])
            state = db.execute("SELECT state FROM outbox_receipts_v1 WHERE batch=? AND position=?",
                               (prior[0], len(prior_records) - 1)).fetchone()
            if state != ("matched",):
                return Action("wait_predecessor", prior[0])
        rows = dict((position, (state, token)) for position, state, token in db.execute(
            "SELECT position,state,token FROM outbox_receipts_v1 WHERE batch=?", (key,)))
        if any(type(p) is not int or not 0 <= p < len(records) for p in rows):
            raise StoreConflict("receipt position outside frozen bundle")
        for position, record in enumerate(records):
            state, token = rows.get(position, ("ready", None))
            if state == "matched":
                continue
            if state in ("redacted", "conflict"):
                return Action("review_" + state, key)
            if state == "pending":
                return Action("read", key, record, Attempt(key, position, token))
            if state != "ready":
                raise StoreConflict("invalid durable receipt state")
            return Action("send", key, record)
        return Action("complete", key)

    @_private_errors
    def next_action(self, key):
        with self.store._connection() as db:
            db.execute("BEGIN")
            return self._next(db, key)

    @_private_errors
    def begin_send(self, key, *, boundary=None):
        """Commit uncertainty BEFORE handing exact bytes to a transport.

        A send receipt alone cannot complete this attempt. Even a crash before
        the transport runs requires read reconciliation on the next invocation.
        """
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            action = self._next(db, key)
            if action.kind != "send":
                raise StoreConflict("batch is not ready for a send")
            _, records = self._load(db, key)
            position = next(i for i, record in enumerate(records) if record.key == action.record.key)
            attempt = Attempt(key, position, uuid.uuid4().hex)
            prior = db.execute("SELECT evidence FROM outbox_receipts_v1 WHERE batch=? AND position=?",
                               (key, position)).fetchone()
            db.execute("INSERT OR REPLACE INTO outbox_attempt_guards_v1 VALUES(?,?,?,?)",
                       (key, position, attempt.token, int(prior is None)))
            db.execute("INSERT OR REPLACE INTO outbox_receipts_v1 VALUES(?,?,'pending',?,NULL)",
                       (key, position, attempt.token))
            if prior is not None and prior[0] is not None:
                superseded = prior[0]
                still_used = (
                    db.execute("SELECT 1 FROM outbox_receipts_v1 WHERE evidence=? LIMIT 1",
                               (superseded,)).fetchone() is not None
                    or db.execute("SELECT 1 FROM outbox_omissions_v1 WHERE evidence=? LIMIT 1",
                                  (superseded,)).fetchone() is not None
                )
                if not still_used:
                    db.execute("DELETE FROM outbox_evidence_v1 WHERE digest=?", (superseded,))
            if boundary:
                boundary("before_attempt_commit")
            db.commit()
            if boundary:
                boundary("after_attempt_commit")
            return Action("send", key, action.record, attempt)

    @_private_errors
    def replace_refused(self, attempt, *, code, item, manifest, evidence_sha256, boundary=None):
        """Atomically replace a definitely uncommitted FIRST send refusal only.

        Caller must authenticate the exact attempt's precommit screening refusal,
        then fully validate/screen BOTH replacement envelopes, including retained
        identifiers. A digest is only an evidence reference. A timeout, absence,
        unknown error or retry cannot authorize this operation: earlier uncertain
        attempts may still commit. Historical attempts lacking the guard refuse.
        Original capture/checkpoint stay immutable; a private audited overlay
        supplies new wire bytes. This is not redaction or server persistence.
        """
        if not isinstance(attempt, Attempt) or type(attempt.position) is not int:
            raise StoreError("durable attempt required")
        _opaque(evidence_sha256)
        if type(code) is not str or code not in REFUSALS:
            raise StoreError("definite screening refusal required")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _, records = self._load(db, attempt.batch)
            action = self._next(db, attempt.batch)
            guard = db.execute("SELECT token,first_attempt FROM outbox_attempt_guards_v1 WHERE batch=? AND position=?",
                               (attempt.batch, attempt.position)).fetchone()
            if action.kind != "read" or action.attempt != attempt or guard != (attempt.token, 1):
                raise StoreConflict("current first attempt required; reconcile uncertainty")
            if db.execute("SELECT 1 FROM outbox_receipts_v1 WHERE batch=? AND position>?",
                          (attempt.batch, attempt.position)).fetchone():
                raise StoreConflict("later item or manifest has already been attempted")
            prepared = _replacement(records, attempt.position, item, manifest, code)
            used = db.execute("SELECT COALESCE(SUM(length(item)+length(manifest)),0) FROM outbox_omissions_v1 WHERE batch=?",
                              (attempt.batch,)).fetchone()[0]
            if used + len(item.value) + len(manifest.value) > self.store.limits.output_bytes:
                raise StoreError("retained omission overlay byte limit exceeded")
            hashes = [hashlib.sha256(v).hexdigest() for v in (
                records[attempt.position].value, records[-1].value, item.value, manifest.value)]
            db.execute("INSERT INTO outbox_omissions_v1 VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (attempt.batch, attempt.position, code, attempt.token, evidence_sha256,
                        hashes[0], hashes[1], item.value, manifest.value, hashes[2], hashes[3]))
            if boundary:
                boundary("after_replacement_output")
            db.execute("UPDATE outbox_receipts_v1 SET state='ready',evidence=? WHERE batch=? AND position=?",
                       (evidence_sha256, attempt.batch, attempt.position))
            if boundary:
                boundary("before_replacement_commit")
            db.commit()
            if boundary:
                boundary("after_replacement_commit")
            return Action("send", attempt.batch, prepared[attempt.position])

    @_private_errors
    def observe(self, attempt, *, value, evidence_sha256, redacted=False,
                evidence_artifact=None, boundary=None):
        """Apply an already authenticated, exact-key read, never a send receipt.

        Missing readback authorizes replay of the SAME frozen envelope, not a
        receipt. This requires the server's write-once contract: a late original
        write and the retry cannot overwrite one another. Unverifiable reads
        must not call this method. Redaction blocks the original manifest;
        provenance and manifest redaction integration remain caller/server work.
        Equal strict JSON values reconcile despite member ordering, whitespace
        or escaping differences. This never changes the frozen retry bytes.
        """
        if not isinstance(attempt, Attempt) or type(attempt.position) is not int:
            raise StoreError("durable attempt required")
        _opaque(evidence_sha256)
        if evidence_artifact is not None and (
                type(evidence_artifact) is not bytes
                or not 0 < len(evidence_artifact) <= MAX_EVIDENCE_BYTES
                or hashlib.sha256(evidence_artifact).hexdigest() != evidence_sha256):
            raise StoreError("invalid private verification artifact")
        if type(redacted) is not bool or (value is not None and type(value) is not bytes) or (redacted and value is not None):
            raise StoreError("invalid verified observation")
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _, records = self._load(db, attempt.batch)
            row = db.execute("SELECT state,token FROM outbox_receipts_v1 WHERE batch=? AND position=?",
                             (attempt.batch, attempt.position)).fetchone()
            if row != ("pending", attempt.token) or not 0 <= attempt.position < len(records):
                raise StoreConflict("stale or already handled observation")
            if evidence_artifact is not None:
                old = db.execute("SELECT artifact FROM outbox_evidence_v1 WHERE digest=?",
                                 (evidence_sha256,)).fetchone()
                if old is not None and old[0] != evidence_artifact:
                    raise StoreConflict("private verification artifact digest collision")
                db.execute("INSERT OR IGNORE INTO outbox_evidence_v1 VALUES(?,?)",
                           (evidence_sha256, evidence_artifact))
                if boundary:
                    boundary("after_evidence_output")
            state = ("redacted" if redacted else "ready" if value is None else
                     "matched" if _matches_readback(records[attempt.position].value, value) else "conflict")
            db.execute("UPDATE outbox_receipts_v1 SET state=?,evidence=? WHERE batch=? AND position=?",
                       (state, evidence_sha256, attempt.batch, attempt.position))
            if boundary:
                boundary("before_observation_commit")
            db.commit()
            if boundary:
                boundary("after_observation_commit")
            return state

    @_private_errors
    def evidence(self, digest):
        """Read a PRIVATE artifact with integrity checks; a digest alone is no proof."""
        _opaque(digest)
        with self.store._connection() as db:
            row = db.execute("SELECT artifact FROM outbox_evidence_v1 WHERE digest=?",
                             (digest,)).fetchone()
        if row is None:
            raise StoreError("private verification artifact unavailable")
        artifact = row[0]
        if (type(artifact) is not bytes or not 0 < len(artifact) <= MAX_EVIDENCE_BYTES
                or hashlib.sha256(artifact).hexdigest() != digest):
            raise StoreError("private verification artifact integrity failure")
        return artifact

    @_private_errors
    def prune_completed_evidence(self, batch_key=None, *, boundary=None):
        """Delete private readback artifacts no longer needed for unfinished batches.

        Frozen batch bytes, parser checkpoints, receipt states/digests and any
        omission-overlay artifacts remain intact. An artifact in outbox_evidence_v1
        is removed only when no outbox_omissions_v1 row references its digest and
        every outbox_receipts_v1 row referencing its digest belongs to a batch
        whose durable next_action is 'complete'. Freed pages stay on SQLite's
        freelist for subsequent writes.
        """
        if batch_key is not None:
            _opaque(batch_key)
        with self.store._connection() as db:
            if db.execute("SELECT 1 FROM outbox_evidence_v1 LIMIT 1").fetchone() is None:
                return 0
            if batch_key is not None:
                candidates = [batch_key]
            else:
                candidates = [r[0] for r in db.execute("""
                    SELECT DISTINCT r.batch
                    FROM outbox_evidence_v1 e
                    JOIN outbox_receipts_v1 r ON r.evidence = e.digest
                    LEFT JOIN batches b ON b.key = r.batch
                    ORDER BY b.source, b.generation, b.revision, r.batch
                """)]
            pruned = 0
            chunk_size = 8
            for start in range(0, len(candidates), chunk_size):
                chunk = candidates[start:start + chunk_size]
                db.execute("BEGIN IMMEDIATE")
                complete = {}

                def is_complete(key):
                    if key not in complete:
                        complete[key] = self._next(db, key).kind == "complete"
                    return complete[key]

                target_batches = {key for key in chunk if is_complete(key)}
                if not target_batches:
                    db.rollback()
                    continue
                target_digests = {
                    r[0] for key in target_batches
                    for r in db.execute(
                        "SELECT DISTINCT evidence FROM outbox_receipts_v1 WHERE batch=? AND evidence IS NOT NULL",
                        (key,))
                }
                if target_digests:
                    protected = {r[0] for r in db.execute("SELECT evidence FROM outbox_omissions_v1")}
                    for b_key, digest in db.execute(
                            "SELECT batch, evidence FROM outbox_receipts_v1 WHERE evidence IS NOT NULL"):
                        if digest in target_digests and b_key not in target_batches and not is_complete(b_key):
                            protected.add(digest)
                    for digest in sorted(target_digests - protected):
                        pruned += db.execute(
                            "DELETE FROM outbox_evidence_v1 WHERE digest=?", (digest,)).rowcount
                if boundary:
                    boundary("before_prune_commit")
                db.commit()

            if batch_key is None and db.execute("SELECT 1 FROM outbox_evidence_v1 LIMIT 1").fetchone() is not None:
                db.execute("BEGIN IMMEDIATE")
                referenced = {
                    r[0] for r in db.execute(
                        "SELECT evidence FROM outbox_receipts_v1 WHERE evidence IS NOT NULL")
                } | {
                    r[0] for r in db.execute("SELECT evidence FROM outbox_omissions_v1")
                }
                orphans = [
                    r[0] for r in db.execute("SELECT digest FROM outbox_evidence_v1")
                    if r[0] not in referenced
                ]
                for digest in orphans:
                    pruned += db.execute(
                        "DELETE FROM outbox_evidence_v1 WHERE digest=?", (digest,)).rowcount
                if boundary:
                    boundary("before_prune_commit")
                db.commit()
            return pruned
