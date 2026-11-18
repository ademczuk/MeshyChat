"""Rule 3 of the file access check, and the pickup receipt (issue #118; operator ruling 2026-10-12).

RULE 3. A peer named in `to` or `cc` of a mesh message (shared/mesh-chat) whose `attachments` lists a file may
download that file. Rules 1 (same owner) and 2 (call participant) are unchanged, and so is the 404 for everyone else.

The grant is RECORDED when the message is written, never looked up from the message at download time: the mail
namespace is tens of thousands of JSON rows with no index on `attachments` or the recipients, and a per-download scan
of it under FORCE RLS is the 041 failure again. `record_message_grants` runs inside memory.write's own transaction
(already bound to app.server_internal), after the row is inserted, so the message and its grants commit or roll back
together.

NO MINTING. A grant is written for a file only when the SENDER could already read it by rule 1 or 2 at that moment.
Without that, attaching a guessed file id would hand its bytes to whoever the message addresses (the hole #122 closed
for call attachments). Rule 3 itself does not count for the sender, so a recipient cannot pass a file further on by
writing a message of its own. A file the sender may not read is skipped silently: the message is stored as today and
its recipients get the same 404 a guessed id gets.

RECIPIENTS are resolved with `apps.mesh.views.peers_answering`, the inverse of `labels_for`, so a grant reaches exactly
the peers that inbox and unread deliver the message to. "all" and "both" grant nothing: a broadcast is not an address
for private bytes. The grant belongs to the peers addressed when the message was written; a later roster change
neither adds nor removes readers. A redaction of the message deletes its grants (`delete_message_grants`).

THE RECEIPT (`record_download`) is one row per presigned download URL handed out, with the rule that admitted the
reader. It records that a URL was ISSUED, not that the bytes arrived: the server never sees the GET.

Every function here takes a connection already bound to app.server_internal by its caller; none opens a transaction.
"""
from __future__ import annotations

import re

from apps.memoryskills.validation import (MESH_MAX_ATTACHMENTS, MESH_MAX_RECIPIENTS, is_mesh_message)

#: Labels that address everyone and therefore grant nothing (apps/mesh BROADCAST_LABELS' spelling).
BROADCAST_LABELS = frozenset({"all", "both"})
#: The v1 rules allow MESH_MAX_RECIPIENTS in `to` AND as many again in `cc`, and MESH_MAX_ATTACHMENTS files. These
#: bounds are applied HERE, before any label is resolved or any file looked up, whatever reached this function.
MAX_LABELS = 2 * MESH_MAX_RECIPIENTS
MAX_FILES = MESH_MAX_ATTACHMENTS
#: A label resolves to at most a handful of peers; this is a backstop against a pathological roster, not a quota that
#: decides which file is granted (rows are never cut file by file).
MAX_GRANTS_PER_MESSAGE = MAX_LABELS * MAX_FILES * 4
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FALLBACK_RE = re.compile(r"^peer-[0-9a-f]{8}$")

RULE_OWNER, RULE_CALL, RULE_MESSAGE = 1, 2, 3

_REFERENCING_CALLS_SQL = "SELECT caller_peer_id, target_peer_id FROM public.file_referencing_calls(%s)"


def _labels(value, field) -> list[str]:
    raw = value.get(field)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [x.strip() for x in raw[:MAX_LABELS] if isinstance(x, str) and 0 < len(x.strip()) <= 200]


def sender_may_read(cur, file_id, peer_id, owner_id) -> bool:
    """Rules 1 and 2 only, on an open cursor (no transaction of its own). False for a missing file."""
    cur.execute("SELECT owner_id FROM file_artifacts WHERE id = %s", (str(file_id),))
    row = cur.fetchone()
    if row is None:
        return False
    if owner_id is not None and str(row[0]) == str(owner_id):
        return True
    cur.execute(_REFERENCING_CALLS_SQL, (str(file_id),))
    peer = str(peer_id)
    return any((caller is not None and str(caller) == peer) or str(target) == peer
               for caller, target in cur.fetchall())


def record_message_grants(conn, *, namespace, key, value, sender_peer_id, sender_owner_id,
                          resolve=None) -> int:
    """Write rule-3 grants for one stored mail message. Returns the number of rows inserted.

    Only a version 1 ever reaches here with grants to write: a later version of a mail key is refused by memory.write
    (divergence 059, E_USE_MEMORY_EDIT), and memory.edit freezes everything but `body`, so the recipients and the
    attachments a grant was computed from cannot change afterwards. ON CONFLICT DO NOTHING keeps a replay a replay.

    A no-op outside shared/mesh-chat, for a value without `attachments`, and for a message that addresses only
    broadcast labels. `resolve(conn, label) -> [peer id]` defaults to `peers_answering`, which answers ACTIVE peers
    only; tests pass their own."""
    # Only a v1 mail MESSAGE: a msg/ key of shared/mesh-chat (divergence 028's scope, the one 059 freezes) whose value
    # passed the v1 rules. Any other key there keeps plain key-value versioning, so a later version could drop a
    # recipient and leave its grant behind (pxx's review, P1); an unversioned value admitted by the transitional flag
    # skipped the attachment rules. Neither grants anything.
    if not is_mesh_message(namespace, key) or not isinstance(value, dict) or value.get("schema_version") != 1:
        return 0
    attachments = value.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return 0
    file_ids = list(dict.fromkeys(
        a["file_id"] for a in attachments[:MAX_FILES]
        if isinstance(a, dict) and isinstance(a.get("file_id"), str) and _UUID_RE.match(a["file_id"])))
    if not file_ids:
        return 0
    if resolve is None:
        from apps.mesh.views import peers_answering as resolve
    labels = [x for x in dict.fromkeys(_labels(value, "to") + _labels(value, "cc"))
              if x.lower() not in BROADCAST_LABELS][:MAX_LABELS]
    recipients: list[str] = []
    for label in labels:
        found = list(resolve(conn, label))
        # A fallback-shaped label (peer-<8 hex>) that answers to more than one peer is ambiguous: a legacy peer can
        # hold such a NAME while another peer's id starts with those 8 hex. Mail may go to both; file bytes go to
        # neither.
        if _FALLBACK_RE.match(label) and len(found) > 1:
            continue
        for pid in found:
            if pid not in recipients and pid != str(sender_peer_id):
                recipients.append(pid)
    if not recipients:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        readable = [f for f in file_ids if sender_may_read(cur, f, sender_peer_id, sender_owner_id)]
        rows = [(f, p, key, str(sender_peer_id)) for f in readable for p in recipients][:MAX_GRANTS_PER_MESSAGE]
        for row in rows:
            cur.execute(
                "INSERT INTO public.file_message_grants (file_id, peer_id, message_key, sender_peer_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", row)
            inserted += cur.rowcount
    return inserted


#: The key a direct grant (POST /v1/files/{id}/grants) is stored under in file_message_grants. Not a mail key, so a
#: redaction never withdraws it, and one per granting peer, so a repeated grant is a replay (ON CONFLICT DO NOTHING).
DIRECT_GRANT_PREFIX = "grant/"


def direct_grant_key(granter_peer_id) -> str:
    return DIRECT_GRANT_PREFIX + str(granter_peer_id)


def record_direct_grant(conn, *, file_id, label, granter_peer_id, resolve=None) -> list[str]:
    """Grant one file to the peers `label` reaches (audit finding 5, oxxxxxx: six reviewers got 404s on files
    sent to someone else). The CALLER has already checked the granter OWNS the file (rule 1); nothing here mints.
    Rule 3 then admits each peer on download, exactly as a message grant does. Returns the peer ids granted (the
    granter itself is never one). Broadcast labels and an ambiguous fallback label resolve to nobody, as for a
    message. Runs on a connection bound to app.server_internal, like every function here."""
    if not isinstance(label, str) or not 0 < len(label.strip()) <= 200:
        return []
    label = label.strip()
    if label.lower() in BROADCAST_LABELS:
        return []
    if resolve is None:
        from apps.mesh.views import peers_answering as resolve
    found = list(resolve(conn, label))
    if _FALLBACK_RE.match(label) and len(found) > 1:
        return []
    peers = [p for p in dict.fromkeys(found) if p != str(granter_peer_id)][:MAX_LABELS]
    key = direct_grant_key(granter_peer_id)
    with conn.cursor() as cur:
        for pid in peers:
            cur.execute(
                "INSERT INTO public.file_message_grants (file_id, peer_id, message_key, sender_peer_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (str(file_id), pid, key, str(granter_peer_id)))
    return peers


def delete_message_grants(conn, key) -> int:
    """A redaction of the message withdraws grants not still attached to any unredacted version (#320).
    Returns the number of rows deleted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM shared_memory "
            " WHERE namespace = 'shared/mesh-chat' AND key = %s "
            " ORDER BY version FOR UPDATE",
            (key,))
        cur.execute(
            "DELETE FROM public.file_message_grants "
            " WHERE message_key = %s "
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM shared_memory sm, "
            "            jsonb_array_elements("
            "              CASE WHEN jsonb_typeof(sm.value -> 'attachments') = 'array' "
            "                   THEN sm.value -> 'attachments' ELSE '[]'::jsonb END"
            "            ) AS att "
            "      WHERE sm.namespace = 'shared/mesh-chat' AND sm.key = %s "
            "        AND COALESCE((sm.value ->> 'redacted')::boolean, false) = false "
            "        AND att ->> 'file_id' = file_message_grants.file_id::text"
            "   )",
            (key, key))
        return cur.rowcount


def message_grant_key(conn, file_id, peer_id) -> str | None:
    """Rule 3: the key of the oldest message that grants `peer_id` the file, or None. A primary-key range read."""
    with conn.cursor() as cur:
        cur.execute("SELECT message_key FROM public.file_message_grants WHERE file_id = %s AND peer_id = %s "
                    "ORDER BY created_at, message_key LIMIT 1", (str(file_id), str(peer_id)))
        row = cur.fetchone()
        return row[0] if row else None


#: A client that retries a presign in a loop must not write a receipt each time: one per (file, peer, rule) per window.
RECEIPT_DEDUP_SECS = 60


def record_download(conn, file_id, peer_id, rule, sha256, message_key=None) -> bool:
    """One receipt, unless the same reader got a URL for the same file by the same rule within RECEIPT_DEDUP_SECS.
    Returns True when a row was written. Raises on a database failure, so the caller hands out no URL it did not
    record."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.file_downloads (file_id, peer_id, rule, sha256, message_key) "
            "SELECT %s, %s, %s, %s, %s WHERE NOT EXISTS ("
            "  SELECT 1 FROM public.file_downloads WHERE file_id = %s AND peer_id = %s AND rule = %s "
            "  AND presigned_at > now() - make_interval(secs => %s))",
            (str(file_id), str(peer_id), int(rule), sha256, message_key,
             str(file_id), str(peer_id), int(rule), RECEIPT_DEDUP_SECS))
        return cur.rowcount == 1


def list_downloads(conn, file_id, limit: int = 100) -> list[dict]:
    """The newest `limit` receipts of one file, newest first."""
    with conn.cursor() as cur:
        cur.execute("SELECT peer_id, rule, sha256, message_key, presigned_at FROM public.file_downloads "
                    "WHERE file_id = %s ORDER BY presigned_at DESC, id DESC LIMIT %s",
                    (str(file_id), int(limit)))
        return [{"peer_id": str(p), "rule": int(r), "sha256": s, "message_key": k, "presigned_at": t.isoformat()}
                for p, r, s, k, t in cur.fetchall()]
