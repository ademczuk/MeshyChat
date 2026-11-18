"""memory.edit: a seat of the same owner appends a new signed version of a mail row (divergence 059).

Design: docs/design/edit.md, reviewed by memory-bxxxxx as non-author (design records #29). The operator's
ruling is ASDLC D-2026-09-29-U14, "New version, history kept": the edit is a new signed version of the key, the
old version stays in the history and can itself be redacted (memory.redact, divergence 053).

WHY A SEPARATE SKILL. memory.write refuses a later version of a mail key on four counts (divergence 028: R1 the
stamped author, R2 `from`, R3 the key's embedded label, R4 one writer), and relaxing it would open all four to every
writer to serve one path. `from` is what the wait serves as the sender, so an edit keeps `from` as the ORIGINAL
sender; the editor is `author_label`, the signature and `created_by_peer_id`.

THE REQUEST. args are exactly `namespace` (shared/mesh-chat), `key` (a msg/ key), `expected_head_version` and `value`,
the WHOLE new value as the editor signs it. Nothing else is accepted (not tags, not status, not author): the database's
agree-trigger derives those columns from the signed args and the row must agree, and a mail row has none.

WHAT IT CHECKS, in this order (the database holds the owner, redaction and frozen-field rules again,
sql_meshychat/029, for a row that reaches it by another path):

  1. not made with a capability token; write permission on the key; the I2 plane rule as memory.write applies it;
  2. the head exists, and `expected_head_version` is it (`E_WRITE_CONFLICT` otherwise);
  3. fewer than MAX_VERSIONS versions exist, and the head is not redacted (a tombstone is final);
  4. the caller's OWNER wrote version 1 (`created_by_owner_id` of the chain's lowest version);
  5. the head is a schema-version-1 message, the new value passes the v1 rules, and EVERYTHING BUT `body` is the
     head's: to, cc, thread_id, in_reply_to, type, ts, from, author_family, requires_ack, subject, ref,
     session_label. An edit that changed to or cc would be a send to new recipients that skipped every send check.

WHAT IT DOES, in one transaction (`rls.projection_writer`: server_internal plus the flag the derived-row delete needs): reads the authors of the direct replies (told, as redaction
tells them), marks the transaction as the edit path, retires the head, inserts version + 1 exactly as memory.write does
(the editor's signed bytes and envelope), inserts the record (shared_memory_edit: no text) and deletes what was derived
from the OLD version's text, by the same list memory.redact uses.
"""
from __future__ import annotations

import json
import re

import psycopg
import psycopg.errors
from psycopg.rows import dict_row

from apps.core import rls

from . import redact
from .errors import VALID_ERROR_CODES, HandlerError
from .permissions import Key, check_permission, derive_owner_id, fetch_caller_pubkey, is_dev_plane
from .signed_bytes import refuse_unsafe_numbers
from .validation import (
    MESH_CHAT_NAMESPACE,
    args_of,
    payload_of,
    require,
    validate_mesh_key_sender,
    validate_mesh_message_v1,
)

OP = "memory.edit"

#: The most versions a key may hold, so a seat editing a thousand times cannot send every recipient a thousand events
#: (design/edit.md section 9: 20). The edit that would make version MAX_VERSIONS + 1 is refused.
MAX_VERSIONS = 20

#: The only args an edit request carries. A `tags`, `status` or `author` would have to agree with the row's columns
#: (sql_meshychat/014), and a mail row has none.
ARGS = frozenset({"namespace", "key", "expected_head_version", "value"})

#: The transaction-local setting that marks the edit path (sql_meshychat/029). It authorizes nothing by itself: any role
#: can SET a custom setting. It says which path is inserting; the trigger's own checks are the teeth.
EDIT_MARK = "app.mesh_edit_owner"

_MISSING = object()
_DB_CODE = re.compile(r"^(E_[A-Z_]+):")


def _typed(exc: psycopg.errors.CheckViolation) -> HandlerError | None:
    """The typed refusal a trigger of 029 raised, or None for any other violation. The handler checks first, so this is
    the backstop for a race the handler could not see; its message is the trigger's own."""
    message = str(exc).splitlines()[0] if str(exc) else ""
    m = _DB_CODE.match(message)
    if m and m.group(1) in VALID_ERROR_CODES:
        return HandlerError(m.group(1), message[len(m.group(0)):].strip())
    return None


def _result(row, *, replay: bool) -> dict:
    return {
        "op": OP, "namespace": row["namespace"], "key": row["key"], "version": int(row["version"]),
        "prior_version": int(row["version"]) - 1, "is_head": bool(row.get("is_head", True)),
        "id": str(row["id"]), "author_label": row["author_label"],
        "edited_at": str(row["created_at"]), "replay": replay,
    }


def handle_edit(verified_payload, caller_peer_id, conn, *, principal=None):
    from . import handlers as _h

    _h._refuse_capability(principal, OP, "an edit changes what other seats read")
    payload = payload_of(verified_payload)
    refuse_unsafe_numbers(payload)
    args = args_of(payload)
    unknown = sorted(set(args) - ARGS)
    if unknown:
        raise HandlerError("E_INVALID_ARGS", f"memory.edit takes namespace, key, expected_head_version and value; "
                                             f"not {', '.join(unknown)}")
    namespace = require(args, "namespace")
    key = require(args, "key")
    if namespace != MESH_CHAT_NAMESPACE or not str(key).startswith("msg/"):
        raise HandlerError("E_INVALID_ARGS", f"memory.edit edits a msg/ key of {MESH_CHAT_NAMESPACE} only")
    value = args.get("value")
    if not isinstance(value, dict):
        raise HandlerError("E_INVALID_ARGS", "value must be the whole new message, as an object")
    expected = args.get("expected_head_version")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise HandlerError("E_INVALID_ARGS", "expected_head_version must be the head version you edited from (an integer)")

    # The bytes verify_strict accepted ARE the row's signed_bytes: memory.write's envelope, checked by memory.write's own body.
    call_id, signature, signing_kid, verified_at, signed_bytes, jcs_sha = _h._signed_envelope(
        verified_payload, payload, conn, caller_peer_id)

    try:
        # rls.projection_writer is server_internal PLUS the right to delete the derived rows (sql_meshychat/008): the edit
        # deletes those of the version it replaces in this transaction, as memory.redact does, and a DELETE without the
        # flag matches nothing and says nothing (redact.delete_derived asks after it). The second request path allowed in.
        with rls.projection_writer(conn):
            check_permission(namespace, caller_peer_id, "write", conn, resource=Key(key))

            existing = _h._existing_by_dedup(conn, jcs_sha, call_id)
            if existing is not None:
                _h._replay_of_own_write(existing, caller_peer_id)
                if (existing["namespace"], existing["key"]) != (namespace, key):
                    raise HandlerError("E_REPLAY", "this call_id was already used by a write to another key")
                # Only what an EDIT wrote is an edit's replay. The dedup also finds the row of a memory.write that used this call_id (the
                # message's own first version, say), which is no replay of anything this call means to do (hosted review of #35).
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM shared_memory_edit WHERE memory_id = %s", (existing["id"],))
                    if cur.fetchone() is None:
                        raise HandlerError("E_REPLAY", "this call_id was already used by a call that is not an edit")
                return _result(existing, replay=True)

            caller_owner_id = str(derive_owner_id(conn, caller_peer_id))
            if namespace.startswith("shared/") and not is_dev_plane(conn, caller_owner_id):
                raise HandlerError("E_PRODUCT_NO_SHARED_WRITE",
                                   f"only dev-plane owners may write a shared/* namespace ({namespace!r})")

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT id, version, value FROM shared_memory "
                            "WHERE namespace = %s AND key = %s AND is_head FOR UPDATE", (namespace, key))
                head = cur.fetchone()
                if head is None:
                    # An edit that WAITED for the head row's lock reads nothing once the holder commits: READ COMMITTED re-checks `is_head`
                    # against the updated row, which the holder has retired, and this statement began before the commit, so it never sees the
                    # new head. A new statement does. A key that exists at some version has moved under this call: a conflict, which tells
                    # the client to read again, not an unknown key (memory-bxxxxx's review of #35; test 19 reproduces it with two connections).
                    cur.execute("SELECT version AS latest FROM shared_memory WHERE namespace = %s AND key = %s "
                                "ORDER BY version DESC LIMIT 1", (namespace, key))
                    moved = cur.fetchone()
                    if moved is not None:
                        raise HandlerError("E_WRITE_CONFLICT", f"{key} changed while this edit waited: it is now at version "
                                                               f"{moved['latest']} and this edit was made against {expected}; read it again")
                    raise HandlerError("E_UNKNOWN_KEY", f"no such message: {namespace}/{key}")
                head_version = int(head["version"])
                if expected != head_version:
                    raise HandlerError("E_WRITE_CONFLICT",
                                       f"expected head version {expected}, actual {head_version}")
                if head_version >= MAX_VERSIONS:
                    raise HandlerError("E_INVALID_ARGS", f"{key} already has {head_version} versions, the most a message "
                                                         f"may hold; send a new message")
                cur.execute("SELECT 1 FROM shared_memory_redaction WHERE memory_id = %s", (head["id"],))
                if cur.fetchone() is not None:
                    raise HandlerError("E_MESSAGE_REDACTED", f"version {head_version} of {key} is redacted; a "
                                                             f"tombstone cannot be replaced")
                cur.execute("SELECT created_by_owner_id FROM shared_memory WHERE namespace = %s AND key = %s "
                            "ORDER BY version ASC LIMIT 1", (namespace, key))
                first_owner = cur.fetchone()
                if first_owner is None or str(first_owner["created_by_owner_id"]) != caller_owner_id:
                    raise HandlerError("E_PERMISSION_DENIED", f"{key} was written by another owner; a seat edits only "
                                                              f"what its own owner wrote")

                old = head["value"]
                if not isinstance(old, dict) or old.get("schema_version") != 1:
                    raise HandlerError("E_INVALID_ARGS", f"{key} is not a schema-version-1 message; only those are edited")
                # An operator relay is never edited (GitHub Codex on #447): an edit keeps operator_words but writes a
                # new version with a fresh created_at, so a days-old instruction would read as just written. A voice
                # that needs to correct one sends a new message.
                if "operator_words" in old:
                    raise HandlerError("E_INVALID_ARGS", f"{key} carries operator_words; an operator relay is not "
                                                         f"edited, send a new message instead")
                # the directive rule: a directive is never edited, for the same reason, and an edit never adds one (the new
                # value is checked against the old below, but this refusal does not depend on that).
                if "directive" in old or (isinstance(value, dict) and "directive" in value):
                    raise HandlerError("E_INVALID_ARGS", f"{key}: a seat directive is not edited or added by an edit; "
                                                         f"send a new message instead")
                # The urgent lane: an edit re-announces the message, so an edited urgent message would jump the queue
                # again past the hourly allowance; neither field is edited, added or removed (oxxxxxx point 4).
                if any(f in old or (isinstance(value, dict) and f in value) for f in ("urgent", "urgent_declined")):
                    raise HandlerError("E_INVALID_ARGS", f"{key}: an urgent message is not edited, and an edit never "
                                                         f"makes one urgent; send a new message instead")
                validate_mesh_message_v1(namespace, key, value)
                validate_mesh_key_sender(namespace, key, value)
                changed = sorted(k for k in (set(value) | set(old))
                                 if k != "body" and value.get(k, _MISSING) != old.get(k, _MISSING))
                if changed:
                    raise HandlerError("E_INVALID_ARGS", f"an edit changes the body only; {', '.join(changed)} differ from "
                                                         f"version {head_version}")
                if value.get("body") == old.get("body"):
                    raise HandlerError("E_INVALID_ARGS", "the body is unchanged: nothing to edit")

                author_label = _h.stamped_label(caller_peer_id)
                pubkey_b64url, pubkey_fp = fetch_caller_pubkey(conn, caller_peer_id, signing_kid)
                provenance = _h._provenance(payload, signature, signing_kid, caller_owner_id, jcs_sha,
                                            pubkey_b64url, pubkey_fp, verified_at, False)

                # The authors of the direct replies, read before the record is written because the record holds them
                # (the event goes to them as well as to the row's own recipients, as a redaction's does).
                reply_authors = redact.read_reply_authors(cur, namespace, key, caller_peer_id)

                # THE EDIT PATH: marked for this transaction only, and unmarked as soon as the row is in. The mark
                # authorizes nothing; sql_meshychat/029's checks (the peer's owner from peers, version 1's owner, the
                # previous version not redacted, only the body changed) are what admit the insert.
                cur.execute("SELECT set_config(%s, %s, true)", (EDIT_MARK, caller_owner_id))
                cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (head["id"],))
                cur.execute(
                    "INSERT INTO shared_memory "
                    "(namespace, key, version, is_head, parent_id, value, status, tags, author_label, pre_discipline, "
                    " claimed_status, embed_policy, provenance_jsonb, payload_jcs_sha256, signed_bytes, "
                    " created_by_peer_id, created_by_owner_id) "
                    "VALUES (%s, %s, %s, TRUE, %s, %s::jsonb, NULL, ARRAY[]::text[], %s, FALSE, NULL, 'forbidden', "
                    " %s::jsonb, %s, %s, %s, %s) "
                    "RETURNING id, namespace, key, version, is_head, author_label, created_at",
                    (namespace, key, head_version + 1, head["id"], json.dumps(value), author_label,
                     json.dumps(provenance), jcs_sha, signed_bytes, str(caller_peer_id), caller_owner_id))
                new = cur.fetchone()
                cur.execute("SELECT set_config(%s, '', true)", (EDIT_MARK,))
                cur.execute(
                    "INSERT INTO shared_memory_edit (memory_id, namespace, key, version, prior_version, "
                    " edited_by_peer_id, edited_by_owner_id, request_call_id, notify_peer_ids) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::uuid[])",
                    (new["id"], namespace, key, new["version"], head_version, str(caller_peer_id), caller_owner_id,
                     call_id, sorted({a["peer_id"] for a in reply_authors})))
                # What was derived from the OLD version's text goes with it, by the list redaction uses.
                redact.delete_derived(cur, head["id"])
    except psycopg.errors.UniqueViolation:
        with rls.server_internal(conn):
            existing = _h._existing_by_dedup(conn, jcs_sha, call_id)
        if existing is not None:
            _h._replay_of_own_write(existing, caller_peer_id)
            return _result(existing, replay=True)
        raise HandlerError("E_WRITE_CONFLICT", f"concurrent edit of {namespace}/{key}; retry")
    except psycopg.errors.CheckViolation as exc:
        typed = _typed(exc)
        if typed is None:
            raise
        raise typed

    out = _result(new, replay=False)
    out["reply_authors"] = reply_authors
    return out
