"""memory.redact: blank a stored row's text and keep its record (divergence 053).

Design: docs/design/redaction.md. The operator's rulings are its section 7 (ASDLC
D-2026-09-29-U9, -U13): a seat may redact a row its own OWNER wrote, the author's seat or
another seat of that owner; `to` and `cc` stay; the authors of direct replies are told.

WHAT ONE CALL DOES, in one transaction (`rls.projection_writer`, which is `rls.server_internal` PLUS the right to
write a projection table: sql_meshychat/008 lets only a job DELETE the derived rows, and a request that lacks the
flag matches ZERO of them, silently. Until 2026-09-30 this ran under server_internal alone and step 3 deleted
nothing on a real database; tests/test_redaction_derived.py seeds the rows and holds it. This is the one request
path allowed in, and tests/test_projection_writer.py names it):

  1. checks the caller's write permission on the key, then selects the versions asked for
     (`version`: an integer, or "all") FOR UPDATE;
  2. sorts them: already redacted (answered with the existing record: the call is
     idempotent), written by ANOTHER owner (never touched; listed in the result so the caller
     knows text remains there), or to be redacted;
  3. for each row to be redacted: inserts the record (sql_meshychat/027: the record is the
     permission the immutability trigger asks for), writes the tombstone, and removes what
     was derived from the text;
  4. returns the records, the versions left alone, and the authors of direct replies.

WHAT IS TEXT AND WHAT IS ROUTING. The tombstone keeps only `ROUTING_FIELDS` of the old value,
and only when they are of the shape routing has (a string, or a list of strings). Everything
else goes: `body`, and `subject`, `ref`, `session_label` and any field this module has never
heard of. `thread_id` is routing but is free text a sender chose; `blank_thread_id` replaces it.

STORED CALLS (design section 2, row 9). A call is stored with its signed payload, so the WRITE
that made a row holds the row's text in `skill_calls.args` for the call retention (90 days),
and a read or query that returned the row holds it in `result` until the read-result prune
replaces it, an hour after the call ends (apps/calls/retention.py). Both are blanked here:

  * the write call is found by the row's own `call_id`, which is unique and indexed;
  * the reads are found among the results the prune has NOT replaced yet, which is the set
    sql/092's partial index holds, so the search reads at most an hour of reads, and each one
    that names the key is replaced by the prune's own summary with the reason `redaction`.

`memory.list` answers with a preview of up to 160 characters of each row, so it is one of the
reads: the read-result prune covers it since sql_meshychat/028, and so does the search here.
WHAT STAYS until the prune has drained it: list results stored BEFORE 028 was applied and older
than an hour are replaced by the prune's own ticks, 100 a minute, not by this call.

WHAT THIS DOES NOT REACH is in the design's section 2: spools, transcripts, replies that
restate the content, logs, the write-ahead log and earlier backups.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json

from apps.core import rls

from .errors import HandlerError
from .permissions import Key, check_permission, derive_owner_id
from .validation import MESH_CHAT_NAMESPACE, args_of, payload_of, require

OP = "memory.redact"

#: The closed list of reasons (the record's CHECK names the same four). No free text: a
#: reason field is a place to repeat the content.
REASON_CODES = ("client_name", "credential", "personal_data", "sent_in_error")

#: What a tombstone keeps of the old value. Identity and routing, never content.
ROUTING_FIELDS = ("schema_version", "from", "to", "cc", "type", "thread_id", "in_reply_to", "closes", "declines",
                  "requires_ack", "ts", "author_family")

#: How many times memory.redact reads a key's versions before it gives up on a key that keeps growing.
_STABLE_READS = 5

#: What a tombstone ALSO keeps when the row is a decision card (divergence 092): the card's identity
#: and the keys it superseded. Identifiers and pointers, no prose. Without them, redacting a card that
#: had replaced an older ruling brought that ruling back as the current one (apps/cards/store.py derives
#: "superseded" from the head cards that name a key). Kept for a card key of the card namespace only;
#: sql_meshychat/040 permits them in the trigger's one list.
CARD_FIELDS = ("schema", "id", "seat", "time", "supersedes")

#: Derived from the row's TEXT, so they go with it. Most `mesh_edges` are routing
#: (reply-to, thread, addressed labels) and stay so `in_reply_to` resolves; its
#: `mentions` edges are body-derived and are removed separately below.
_DERIVED_BY_SRC = ("mesh_lexical", "mesh_embeddings", "mesh_inferred_edges",
                   "mesh_inferred_audits", "mesh_related_runs")


def _routing_value(value, *, field: str = "") -> bool:
    if isinstance(value, bool):
        return field == "requires_ack"
    if isinstance(value, (str, int)):
        return True
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def tombstone(old_value, *, reason_code: str, redacted_at: str, thread_id=None, card: bool = False) -> dict:
    """The value a redacted row holds. `thread_id`, when given, REPLACES the old one. `card`: the row
    is a decision card, and keeps CARD_FIELDS as well."""
    out = {"redacted": True, "reason_code": reason_code, "redacted_at": redacted_at}
    if isinstance(old_value, dict):
        for field in ROUTING_FIELDS + (CARD_FIELDS if card else ()):
            if field in old_value and _routing_value(old_value[field], field=field):
                out[field] = old_value[field]
    if thread_id is not None:
        out["thread_id"] = thread_id
    return out


def _version_arg(args):
    raw = args.get("version", "all")
    if raw == "all":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise HandlerError("E_INVALID_ARGS", 'version must be a positive integer or "all"')
    return raw


def _utc_iso(moment) -> str:
    """The instant as UTC text with a Z, as the event frame renders it (`to_char(... AT TIME ZONE 'UTC', ...Z)`). psycopg hands
    a timestamptz back in the SESSION's zone, so `isoformat()` alone gave `03:50:20+02:00` on a database that runs Europe/Berlin and
    the stored tombstone's `redacted_at` differed by the server's setting for one instant (review of #32, point 8)."""
    return moment.astimezone(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _record_of(row) -> dict:
    return {
        "redaction_id": str(row["id"]), "namespace": row["namespace"], "key": row["key"],
        "version": int(row["version"]), "redacted_by_peer_id": str(row["redacted_by_peer_id"]),
        "acted_as": row["acted_as"], "reason_code": row["reason_code"],
        "thread_id_blanked": bool(row["thread_id_blanked"]),
        "redacted_at": _utc_iso(row["redacted_at"]),
        "original_payload_jcs_sha256": row["original_payload_jcs_sha256"],
        "original_signed_bytes_sha256": row["original_signed_bytes_sha256"],
    }


_RECORD_COLS = ("id, namespace, key, version, redacted_by_peer_id, acted_as, reason_code, "
                "thread_id_blanked, redacted_at, original_payload_jcs_sha256, "
                "original_signed_bytes_sha256")


def handle_redact(verified_payload, caller_peer_id, conn, *, principal=None):
    from psycopg.rows import dict_row

    from . import handlers as _h

    _h._refuse_capability(principal, OP, "a redaction destroys text and cannot be undone")
    payload = payload_of(verified_payload)
    args = args_of(payload)
    namespace = require(args, "namespace")
    key = require(args, "key")
    # docs/design/context-v1.md 8: a context item's redaction must also redact its manifest's digest, in one
    # transaction, as a new manifest version. Until that is built, the in-place tombstone below would leave the digest
    # readable, so the namespace is refused rather than half-redacted.
    from apps.context import validation as _context
    if _context.is_context_namespace(namespace):
        raise HandlerError("E_CONTEXT_REDACT_UNAVAILABLE",
                           "redaction of the context namespace is not built yet (design section 8); nothing was changed")
    version = _version_arg(args)
    reason_code = require(args, "reason_code")
    if reason_code not in REASON_CODES:
        raise HandlerError("E_INVALID_ARGS",
                           "reason_code must be one of " + ", ".join(REASON_CODES))
    blank_thread = args.get("blank_thread_id", False)
    if not isinstance(blank_thread, bool):
        raise HandlerError("E_INVALID_ARGS", "blank_thread_id must be true or false")

    signature = verified_payload.get("signature")
    signing_kid = verified_payload.get("signing_kid")
    if not signature or not signing_kid:
        raise HandlerError("E_BAD_REQUEST",
                           "verified_payload missing signature/signing_kid for the record")
    # The record stores the bytes of THIS request as verified here, as handle_write stores
    # a row's: an attestation is never written by code that did not check it.
    request_bytes = _h._verify_provenance_signature(conn, caller_peer_id, signing_kid,
                                                    payload, signature)
    call_id = payload.get("call_id")

    with rls.projection_writer(conn):
        check_permission(namespace, caller_peer_id, "write", conn, resource=Key(key))
        caller_owner_id = str(derive_owner_id(conn, caller_peer_id))

        with conn.cursor(row_factory=dict_row) as cur:
            serialize_with_projection(cur, namespace, blank_thread)   # before the row locks below
            # READ UNTIL STABLE (divergence 091). `FOR UPDATE` waits for a writer that holds one of these
            # rows, and when it is let through it re-checks the rows its OWN snapshot saw: a version that
            # writer inserted is not among them. A redaction of "all" versions that waited for a
            # memory.write therefore left the version that write had just committed, text and all, as the
            # head. So the read is repeated while it holds the locks, until it finds no version it had
            # not seen. A card writer locks the head before it inserts (apps/cards/validation.py), so for a
            # card the second read is final; the bound is for every other namespace.
            rows, seen = [], None
            for _ in range(_STABLE_READS):
                # Lock all versions of (namespace, key) in ascending version order even when a single version is
                # requested (#320): two concurrent single-version redactions then serialize in a fixed order
                # without deadlocking before _redact_one checks whether any unredacted version still grants a file.
                cur.execute(
                    "SELECT id, version, value, signed_bytes, payload_jcs_sha256, "
                    "       created_by_peer_id, created_by_owner_id, author_label, "
                    "       provenance_jsonb -> 'payload' ->> 'call_id' AS write_call_id "
                    "FROM shared_memory WHERE namespace = %s AND key = %s "
                    "ORDER BY version FOR UPDATE",
                    (namespace, key))
                all_rows = cur.fetchall()
                rows = [r for r in all_rows if version is None or int(r["version"]) == version]
                ids = {r["id"] for r in all_rows}
                if ids == seen:
                    break
                seen = ids
            else:
                raise HandlerError("E_WRITE_CONFLICT", f"{namespace}/{key} gained a version on each of "
                                   f"{_STABLE_READS} reads; the redaction is refused, nothing is changed. Retry")
            if not rows:
                raise HandlerError("E_UNKNOWN_KEY", f"no such row: {namespace}/{key}"
                                   + ("" if version is None else f" version {version}"))
            # the directive rule: a directive and the target's reply are the audit pair, so a directive is never redacted, by
            # its writer or anyone (pxx's review of plan v1, P2-3). A secret sent in one is the operator's to remove.
            if any(isinstance(r["value"], dict) and "directive" in r["value"] for r in rows):
                raise HandlerError("E_DIRECTIVE_IMMUTABLE", f"{namespace}/{key} carries a seat directive; a directive "
                                                            "is the audit record and is not redacted")

            cur.execute(f"SELECT {_RECORD_COLS}, memory_id FROM shared_memory_redaction "
                        "WHERE memory_id = ANY(%s::uuid[])", ([r["id"] for r in rows],))
            done = {r["memory_id"]: r for r in cur.fetchall()}

            # The authors of direct replies, read BEFORE the record is written because the record holds
            # them: the event goes to them as well as to the row's own recipients (ruling U13).
            reply_authors = read_reply_authors(cur, namespace, key, caller_peer_id)

            redacted, already, other_owner = [], [], []
            for row in rows:
                if row["id"] in done:
                    already.append(_record_of(done[row["id"]]))
                # created_by_owner_id is NOT NULL (sql/028, the authorship ledger; 0 NULLs of
                # 50,320 rows on production, 2026-09-29), so str() never compares "None".
                elif str(row["created_by_owner_id"]) != caller_owner_id:
                    other_owner.append({"version": int(row["version"]),
                                        "author_label": row["author_label"]})
                else:
                    redacted.append(_redact_one(
                        cur, row, namespace, key, caller_peer_id, caller_owner_id,
                        reason_code, blank_thread, request_bytes, call_id,
                        sorted({a["peer_id"] for a in reply_authors})))

            if not redacted and not already:
                # Nothing of the caller's owner among the versions asked for. Refused, not
                # answered empty: an empty success would read as "the text is gone".
                raise HandlerError(
                    "E_PERMISSION_DENIED",
                    f"{namespace}/{key}: every version asked for was written by another "
                    "owner; a seat redacts only what its own owner wrote")

            stored = {"write_calls": 0, "read_results": 0}
            if redacted:
                stored = _blank_stored_calls(
                    cur, namespace, key,
                    [r["write_call_id"] for r in rows
                     if r["id"] not in done and r["write_call_id"]
                     and str(r["created_by_owner_id"]) == caller_owner_id])

    return {
        "op": OP, "namespace": namespace, "key": key,
        "redacted": redacted, "already_redacted": already,
        "not_redacted_other_owner": other_owner,
        "reply_authors": reply_authors,
        "stored_calls_blanked": stored,
    }


def read_reply_authors(cur, namespace, key, caller_peer_id) -> list[dict]:
    """The authors of the direct replies to `key` other than the caller, by label: who a redaction's event goes to besides
    the row's own recipients, and an edit's (memory.edit reads them here, so the two lists cannot differ). `cur` is a
    dict_row cursor.

    Reads the namespace's heads (23,000 for mail on production): there is no index on in_reply_to. mesh_edges has one
    (replies_to, by dst), but it is a projection a job writes after the fact, and a reply written seconds ago would be
    missed."""
    cur.execute(
        "SELECT DISTINCT author_label, created_by_peer_id FROM shared_memory "
        "WHERE namespace = %s AND is_head AND value ->> 'in_reply_to' = %s "
        "  AND created_by_peer_id <> %s ORDER BY author_label",
        (namespace, key, str(caller_peer_id)))
    return [{"author_label": r["author_label"],
             "peer_id": str(r["created_by_peer_id"])} for r in cur.fetchall()]


def serialize_with_projection(cur, namespace, blank_thread) -> None:
    """A redaction that blanks a mesh thread id takes apps/mesh/projection.BLANKING_LOCK exclusive, BEFORE its row
    locks: it deletes the copies of that thread id replies inherited below the key (divergence 073), and a projection
    pass, which holds the lock shared, must not copy it onto a reply between the pass's read and this commit. Any other
    redaction takes nothing and never waits for a pass."""
    if blank_thread and namespace == MESH_CHAT_NAMESPACE:
        from apps.mesh import projection
        projection.exclude_passes(cur)


def _redact_one(cur, row, namespace, key, caller_peer_id, caller_owner_id, reason_code,
                blank_thread, request_bytes, call_id, notify_peer_ids) -> dict:
    acted_as = "author" if str(row["created_by_peer_id"]) == str(caller_peer_id) else "owner"
    signed = row["signed_bytes"]
    cur.execute(
        "INSERT INTO shared_memory_redaction (memory_id, namespace, key, version, "
        " redacted_by_peer_id, redacted_by_owner_id, acted_as, reason_code, thread_id_blanked, "
        " original_payload_jcs_sha256, original_signed_bytes_sha256, request_signed_bytes, "
        " request_call_id, notify_peer_ids) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid[]) "
        f"RETURNING {_RECORD_COLS}",
        (row["id"], namespace, key, row["version"], str(caller_peer_id), caller_owner_id,
         acted_as, reason_code, blank_thread, row["payload_jcs_sha256"],
         hashlib.sha256(bytes(signed)).hexdigest() if signed is not None else None,
         request_bytes, call_id, list(notify_peer_ids)))
    record = cur.fetchone()

    from apps.cards import validation as card_rules   # at call time: apps.cards imports this package
    value = tombstone(row["value"], reason_code=reason_code,
                      redacted_at=_utc_iso(record["redacted_at"]),
                      thread_id=f"redacted/{record['id']}" if blank_thread else None,
                      card=card_rules.is_card_namespace(namespace) and card_rules.key_parts(key) is not None)
    cur.execute(
        "UPDATE shared_memory SET value = %s::jsonb, signed_bytes = NULL, "
        " tags = ARRAY['redacted']::text[], "
        " provenance_jsonb = (provenance_jsonb - 'payload') || jsonb_build_object("
        "   'payload_redacted', true, 'payload', "
        "   jsonb_build_object('call_id', provenance_jsonb -> 'payload' -> 'call_id')) "
        "WHERE id = %s",
        (json.dumps(value), row["id"]))
    if cur.rowcount != 1:
        # A row-level policy that filters an UPDATE matches zero rows and raises nothing (sql_meshychat/008's reason for
        # delete_derived's postcondition); the record was inserted a statement ago, so refuse the whole call and let it roll back.
        raise HandlerError("E_INTERNAL", f"the tombstone of {namespace}/{key} version {row['version']} changed "
                                         f"{cur.rowcount} rows, not one; the redaction is refused, nothing is changed")

    delete_derived(cur, row["id"], blank_thread=blank_thread, asks=True)
    if namespace == MESH_CHAT_NAMESPACE and key.startswith("msg/"):
        # Issue #118, #320: the tombstone drops `attachments`, so a redacted version stops granting its files.
        # Keep any grant whose file_id is still attached to an unredacted version of this message (e.g. a v2 edit
        # when only v1 was redacted). Lock all versions of the key so concurrent redactions of the final two versions
        # serialize before evaluating NOT EXISTS under READ COMMITTED.
        cur.execute(
            "SELECT id FROM shared_memory "
            " WHERE namespace = %s AND key = %s "
            " ORDER BY version FOR UPDATE",
            (namespace, key))
        cur.execute(
            "DELETE FROM public.file_message_grants "
            " WHERE message_key = %s "
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM shared_memory sm, "
            "            jsonb_array_elements("
            "              CASE WHEN jsonb_typeof(sm.value -> 'attachments') = 'array' "
            "                   THEN sm.value -> 'attachments' ELSE '[]'::jsonb END"
            "            ) AS att "
            "      WHERE sm.namespace = %s AND sm.key = %s "
            "        AND COALESCE((sm.value ->> 'redacted')::boolean, false) = false "
            "        AND att ->> 'file_id' = file_message_grants.file_id::text"
            "   )",
            (key, namespace, key))
    return _record_of(record)


#: The replies whose `in_thread` edge was INHERITED from the row being blanked, and their own inheriting replies below
#: them (apps/mesh/projection.py `_INHERIT_CANDIDATES`, divergence 073). Each is a copy of the thread id being withdrawn. A reply
#: with a thread of its own stops the walk: what is below it inherited that thread, not this one. UNION, so a cycle ends.
#: The ROOT must be a shared/mesh-chat row: keys are unique only within a namespace, mesh_edges holds the mesh's alone,
#: and a blanking redaction elsewhere takes no BLANKING_LOCK, so a same-named key in another namespace must reach no
#: mesh edge (Codex on #106, via kxxx).
_INHERITED_BELOW = (
    "WITH RECURSIVE below(key) AS ("
    "  SELECT key FROM shared_memory WHERE id = %s AND namespace = %s "
    "  UNION "
    "  SELECT r.src_key FROM below b "
    "  JOIN mesh_edges r ON r.edge_type = 'replies_to' AND r.dst = b.key "
    "  JOIN mesh_edges i ON i.src_id = r.src_id AND i.edge_type = 'in_thread' AND i.src_field = 'inherited'"
    ") "
)
_DELETE_INHERITED_BELOW = (
    _INHERITED_BELOW
    + "DELETE FROM mesh_edges e USING mesh_edges r, below b "
      "WHERE e.edge_type = 'in_thread' AND e.src_field = 'inherited' "
      "AND r.src_id = e.src_id AND r.edge_type = 'replies_to' AND r.dst = b.key"
)
#: The postcondition: no direct reply to the row's key still holds an inherited edge.
_INHERITED_DIRECTLY_BELOW = (
    "SELECT 1 FROM shared_memory sm "
    "JOIN mesh_edges r ON r.edge_type = 'replies_to' AND r.dst = sm.key "
    "JOIN mesh_edges e ON e.src_id = r.src_id AND e.edge_type = 'in_thread' AND e.src_field = 'inherited' "
    "WHERE sm.id = %s AND sm.namespace = %s LIMIT 1"
)


def delete_derived(cur, row_id, *, blank_thread: bool = False, asks: bool = False) -> None:
    """What was derived from one version's TEXT, deleted by that version's row id, with a POSTCONDITION.

    A DELETE whose row-level policy is false matches zero rows and raises nothing (sql_meshychat/008: the derived tables'
    DELETE needs the projection-writer flag (`rls.projection_writer`), which `handle_redact` holds and a plain `server_internal` request does not).
    So after the deletes each table is asked whether any row of this version is left, and one that is left ends the
    call: a redaction must never answer success while the text is still findable through a term or a vector. That is
    the first build's bug (deleted nothing, said nothing), and a future policy change would meet the same wall.

    `blank_thread`: the row's thread id was replaced (`blank_thread_id`), so the `in_thread` edge the edge projection wrote for
    THIS row, whose destination is that thread id, goes too. It is the one routing edge that is text: `mesh_edges` stays, on
    purpose, for `authored_by`, `addressed_to`, `copied_to` and `replies_to` (and `owes_ack`, unless `asks`), while body-derived `mentions` edges
    are deleted with the text, and the other rows of the thread keep
    their own `in_thread` edges. When the projection meets the row again at its redaction position it writes the edge to
    `redacted/<id>`, which is consistent (review of #32, blocking 1). The edges replies INHERITED from this row's key
    (divergence 073), and from those replies on down, are copies of the same thread id, so they go too
    (`_DELETE_INHERITED_BELOW`); the pass at the redaction's position gives those replies a thread again from any version
    of the key that still has one, as a full rebuild of the derived tables would.

    `asks`: the version is being REDACTED, so its `owes_ack` edges go too. A redacted ask leaves owed and
    awaiting, since nobody owes an answer to withdrawn text; the tombstone keeps
    `requires_ack` as routing, and normalize writes no owes_ack edge for a tombstone, so a
    rebuild of the derived tables agrees. An
    edit does not pass it: an edit's new version is still the ask.

    Also the list a version's edit will use (docs/design/edit.md): ONE list, so a table added here is added to both."""
    for table in _DERIVED_BY_SRC:
        cur.execute(f"DELETE FROM {table} WHERE src_id = %s", (row_id,))
    cur.execute("DELETE FROM mesh_related_edges WHERE src_id = %s OR dst_id = %s", (row_id, row_id))
    cur.execute("DELETE FROM mesh_edges WHERE src_id = %s AND edge_type = 'mentions'", (row_id,))
    if blank_thread:
        cur.execute("DELETE FROM mesh_edges WHERE src_id = %s AND edge_type = 'in_thread'", (row_id,))
        cur.execute(_DELETE_INHERITED_BELOW, (row_id, MESH_CHAT_NAMESPACE))
    if asks:
        cur.execute("DELETE FROM mesh_edges WHERE src_id = %s AND edge_type = 'owes_ack'", (row_id,))
    left = []
    for table in _DERIVED_BY_SRC:
        cur.execute(f"SELECT 1 FROM {table} WHERE src_id = %s LIMIT 1", (row_id,))
        if cur.fetchone() is not None:
            left.append(table)
    cur.execute("SELECT 1 FROM mesh_related_edges WHERE src_id = %s OR dst_id = %s LIMIT 1", (row_id, row_id))
    if cur.fetchone() is not None:
        left.append("mesh_related_edges")
    cur.execute("SELECT 1 FROM mesh_edges WHERE src_id = %s AND edge_type = 'mentions' LIMIT 1", (row_id,))
    if cur.fetchone() is not None:
        left.append("mesh_edges.mentions")
    if blank_thread:
        cur.execute("SELECT 1 FROM mesh_edges WHERE src_id = %s AND edge_type = 'in_thread' LIMIT 1", (row_id,))
        if cur.fetchone() is not None:
            left.append("mesh_edges (in_thread)")
        cur.execute(_INHERITED_DIRECTLY_BELOW, (row_id, MESH_CHAT_NAMESPACE))
        if cur.fetchone() is not None:
            left.append("mesh_edges (in_thread inherited by a reply)")
    if asks:
        cur.execute("SELECT 1 FROM mesh_edges WHERE src_id = %s AND edge_type = 'owes_ack' LIMIT 1", (row_id,))
        if cur.fetchone() is not None:
            left.append("mesh_edges (owes_ack)")
    if left:
        raise HandlerError("E_INTERNAL", "derived rows of the version are still there after the delete (a row-level policy "
                                         f"filtered it): {', '.join(left)}; the redaction is refused, nothing is changed")


#: How many unpruned reads one statement of a redaction looks at.
READS_PAGE = 500

# The stored read results a redaction has to look at are every read not yet pruned. sql_meshychat/
# 028's partial index (skill_calls_result_prune_v2_idx) carries this predicate literal for literal
# and is ordered (completed_at, id).
#
# WHY A WALK AND NOT ONE STATEMENT. As one UPDATE with the key test beside the predicate the planner
# did not use that index: on a copy of production (3,007,573 calls, 146 unpruned reads, its estimate
# for the index 2,672,530 rows) it read the whole table, 25 s for the scan, and the redaction died on
# the 30 s statement timeout (rehearsal of 2026-10-08). No test saw it: a test database has a few
# hundred calls. The same predicate with ORDER BY the index's own columns and a LIMIT is an index
# walk whatever the estimate says, and the key test then runs by primary key on that page only.
_READS_WHERE = (
    " WHERE skill_name IN ('memory.query', 'memory.read', 'memory.list') "
    "   AND status IN ('completed', 'failed', 'timeout', 'abandoned') "
    "   AND result IS NOT NULL AND completed_at IS NOT NULL "
    "   AND COALESCE(jsonb_extract_path_text(result, 'pruned'), 'false') <> 'true' ")
_READS_FIRST = "SELECT id, completed_at FROM skill_calls" + _READS_WHERE + "ORDER BY completed_at, id LIMIT %s"
_READS_NEXT = ("SELECT id, completed_at FROM skill_calls" + _READS_WHERE +
               "  AND (completed_at, id) > (%s, %s) ORDER BY completed_at, id LIMIT %s")


def _unpruned_reads(cur):
    """Pages of ids of the unpruned stored reads, oldest first. The position is the last row READ, so
    a page whose rows the caller has since pruned (they leave the index) does not move the walk."""
    cur.execute(_READS_FIRST, (READS_PAGE,))
    while True:
        rows = cur.fetchall()
        if not rows:
            return
        last = rows[-1]
        yield [r["id"] if isinstance(r, dict) else r[0] for r in rows]
        if len(rows) < READS_PAGE:
            return
        at, ident = (last["completed_at"], last["id"]) if isinstance(last, dict) else (last[1], last[0])
        cur.execute(_READS_NEXT, (at, ident, READS_PAGE))


def _blank_stored_calls(cur, namespace, key, write_call_ids) -> dict:
    """The copies of the text in `skill_calls` (module docstring). Returns the counts."""
    written = 0
    if write_call_ids:
        cur.execute(
            "UPDATE skill_calls SET "
            " args = jsonb_set(args, '{args}', jsonb_build_object("
            "          'namespace', %s::text, 'key', %s::text, 'redacted', true)), "
            " result = CASE WHEN jsonb_typeof(result) = 'object' "
            "               THEN (result - 'tags') || '{\"tags\": [\"redacted\"]}'::jsonb "
            "               ELSE result END, "
            " result_payload = CASE WHEN jsonb_typeof(result_payload -> 'result') = 'object' "
            "      THEN jsonb_set(result_payload, '{result,tags}', '[\"redacted\"]'::jsonb) "
            "      ELSE result_payload END "
            "WHERE call_id = ANY(%s) AND skill_name IN ('memory.write', 'memory.edit') "
            "  AND jsonb_typeof(args) = 'object'",
            (namespace, key, list(write_call_ids)))
        written = cur.rowcount

    # The stored reads that name the key: the unpruned reads are walked by sql_meshychat/028's index,
    # a page at a time, and the key test runs on each page's rows by primary key (_unpruned_reads).
    needle = json.dumps(key, ensure_ascii=False)
    read_results = 0
    for ids in _unpruned_reads(cur):
        cur.execute(
            "UPDATE skill_calls AS sc "
            "   SET result = jsonb_strip_nulls(jsonb_build_object("
            "         'ok', true, 'pruned', true, 'pruned_reason', 'redaction', "
            "         'pruned_at', to_jsonb(now()), 'skill_name', sc.skill_name, "
            "         'op', jsonb_extract_path_text(sc.result, 'op'), "
            "         'original_result_bytes', pg_column_size(sc.result), "
            "         'original_result_signature', sc.result_signature, "
            "         'original_result_signing_kid', sc.result_signing_kid)), "
            "       result_attachments = ARRAY[]::uuid[], result_signature = NULL, "
            "       result_signing_kid = NULL, result_payload = NULL "
            " WHERE sc.id = ANY(%s) "
            "   AND (position(%s in sc.result::text) > 0 "
            "        OR position(%s in coalesce(sc.result_payload::text, '')) > 0)",
            (ids, needle, needle))
        read_results += cur.rowcount
    # POSTCONDITIONS, for the reason delete_derived has one: an UPDATE a policy filters matches zero rows and says nothing.
    # After the blanking no stored write of these calls still carries the value, and the read-result predicate finds nothing.
    if write_call_ids:
        cur.execute("SELECT 1 FROM skill_calls WHERE call_id = ANY(%s) AND skill_name IN ('memory.write', 'memory.edit') "
                    "  AND jsonb_typeof(args) = 'object' AND (args -> 'args') ? 'value' LIMIT 1", (list(write_call_ids),))
        if cur.fetchone() is not None:
            raise HandlerError("E_INTERNAL", "a stored write call of the row still carries its value after the blanking; "
                                             "the redaction is refused, nothing is changed")
    for ids in _unpruned_reads(cur):
        cur.execute(
            "SELECT 1 FROM skill_calls AS sc WHERE sc.id = ANY(%s) "
            "  AND (position(%s in sc.result::text) > 0 OR position(%s in coalesce(sc.result_payload::text, '')) > 0) LIMIT 1",
            (ids, needle, needle))
        if cur.fetchone() is not None:
            raise HandlerError("E_INTERNAL", "a stored read result naming the row is still there after the blanking; "
                                             "the redaction is refused, nothing is changed")
    return {"write_calls": written, "read_results": read_results}
