"""SQL for the call pipeline, using columns READ off the live database.

RLS NOTE, and it is not optional. `skill_calls` is the one table here with
`ENABLE`+`FORCE ROW LEVEL SECURITY` (sql/006, confirmed against pg_class). Its policies
are asymmetric:

    SELECT : caller_peer_id or target_peer_id = app.current_peer_id   (sql/006)
             OR app.server_internal = '1'                             (sql/016)
    INSERT : caller_peer_id = app.current_peer_id
             OR app.server_internal = '1'                  (sql_meshychat/001)
    UPDATE, DELETE : app.server_internal = '1' ONLY       (sql_meshychat/012, divergence 029)

(This note said `INSERT : WITH CHECK (true)`, which was sql/006 before 001 changed it; the
UPDATE and DELETE policies were `USING (true)` until 012.) Every `skill_calls` write in
apps/calls runs under `rls.server_internal()`, so none of them depends on a peer context.

The pipeline is server-internal code: it must read a call row it is not a party to (the
idempotency check) and it must read back the row it just wrote. Under `meshychat_app` with
no context BOTH read as zero rows and NO ERROR IS RAISED - the idempotency check would
report "new call" for a call that exists, and the insert would then die on the
`call_id` UNIQUE constraint. Worse, `INSERT ... RETURNING` is itself subject to the
SELECT policy, so the insert would appear to return nothing.

So both reads run inside `rls.server_internal()`. That is a privileged context; it is
entered per operation and released with its transaction, never held for a request
(apps/core/rls.py).
"""
from __future__ import annotations

import datetime as _dt
import unicodedata
import uuid

from psycopg.types.json import Jsonb

from apps.calls import events, nonce_ledger
from apps.calls.errors import IdempotencyConflict, SkillNotFound
from apps.core import rls
from apps.crypto import primitives as P

# skills.max_runtime_ms is nullable; the pipeline defaults it to 30_000 ms.
DEFAULT_MAX_RUNTIME_MS = 30_000

# Call IDs are at most 200 bytes.
CALL_ID_MAX_LEN = 200

_SKILL_COLUMNS = ("id", "peer_id", "name", "version", "mode", "share_mode",
                  "attachments_allowed", "returns_attachments", "max_runtime_ms",
                  "is_current", "deleted_at")


def validate_call_id(raw: str) -> str:
    """Validate a call ID before it is logged, used as a pubsub channel name, or persisted.
    A control character in a
    caller-supplied idempotency key is a log-injection vector, which is why an empty /
    overlong / control-bearing key is a 400 rather than something the DB shrugs at."""
    if raw == "":
        raise ValueError("call_id must not be empty")
    if len(raw.encode("utf-8")) > CALL_ID_MAX_LEN:
        raise ValueError("call_id exceeds maximum length")
    # Unicode general category Cc identifies control characters. It is NOT the same as
    # the inverse of `str.isprintable` (which also rejects ordinary spaces
    # and every separator). Checking only category Cc preserves spaces and separators while
    # rejecting control characters.
    if any(unicodedata.category(ch) == "Cc" for ch in raw):
        raise ValueError("call_id must not contain control characters")
    return raw


def resolve_for_call(conn, target_peer_id, name: str, version_pin: str | None = None):
    """Resolve an invocable current skill or an exact version pin.

    `version_pin=None` resolves the CURRENT version, and the predicate carries
    `deleted_at IS NULL` as well as `is_current = TRUE` - a soft-deleted current row
    must not be invocable. A range pin (^ ~ > <) is unsupported and is raised
    rather than approximated: silently widening a pin is an authorization change.
    """
    cols = ", ".join(_SKILL_COLUMNS)
    with conn.cursor() as cur:
        if version_pin is None:
            cur.execute(
                f"SELECT {cols} FROM skills "
                "WHERE peer_id = %s AND name = %s AND is_current = TRUE "
                "  AND deleted_at IS NULL",
                (str(target_peer_id), name))
        else:
            if version_pin[:1] in {"^", "~", ">", "<"}:
                raise SkillNotFound(
                    f"semver range matching ({version_pin}) is not supported")
            cur.execute(
                f"SELECT {cols} FROM skills "
                "WHERE peer_id = %s AND name = %s AND version = %s "
                "  AND deleted_at IS NULL",
                (str(target_peer_id), name, version_pin.lstrip("=")))
        row = cur.fetchone()
    if row is None:
        raise SkillNotFound(f"skill not found: peer={target_peer_id} name={name}")
    return dict(zip(_SKILL_COLUMNS, row))


def get_call_by_call_id(conn, call_id: str) -> dict | None:
    """Server-internal read - see the module docstring on why the context is required."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT call_id, caller_peer_id, args, status, mode, skill_version, "
                "       deadline_at FROM skill_calls WHERE call_id = %s", (call_id,))
            row = cur.fetchone()
    if row is None:
        return None
    return {"call_id": row[0], "caller_peer_id": row[1], "args": row[2],
            "status": row[3], "mode": row[4], "skill_version": row[5],
            "deadline_at": row[6]}


def idempotency_check(conn, call_id: str, payload_canonical: bytes, caller_peer_id):
    """Return the existing row for a replay, or None for a new call.

    Both sides are JCS-canonicalized BEFORE hashing, so a
    serializer difference - key order, number formatting - cannot manufacture a false
    409. Note the stored side is `skill_calls.args`, which holds the WHOLE signed
    payload, not just the user args; see `insert_call`.
    """
    existing = get_call_by_call_id(conn, call_id)
    if existing is None:
        return None
    if existing["caller_peer_id"] is None or \
            uuid.UUID(str(existing["caller_peer_id"])) != uuid.UUID(str(caller_peer_id)):
        raise IdempotencyConflict(
            f"call_id conflict: caller differs from existing call {call_id}")
    if P.sha256(payload_canonical) != P.sha256(P.jcs(existing["args"])):
        raise IdempotencyConflict(
            f"call_id conflict: payload differs from existing call {call_id}")
    return existing


def deadline_for(skill: dict, now: _dt.datetime) -> _dt.datetime:
    """Return the deadline using the skill runtime or the default."""
    ms = skill.get("max_runtime_ms") or DEFAULT_MAX_RUNTIME_MS
    return now + _dt.timedelta(milliseconds=ms)


def insert_call(conn, *, call_id, caller_peer_id, caller_owner_id, target_peer_id,
                target_owner_id, skill_id, skill_name, skill_version, mode, transport,
                payload, args_signature, args_signing_kid, args_attachments, grant_id,
                deadline_at, webhook_url=None, webhook_secret_hash=None,
                nonce_claim=None) -> dict:
    """Insert a call and its 'created' event.

    `args` IS THE WHOLE SIGNED PAYLOAD, NOT `payload["args"]`. The idempotency check
    canonicalizes `row.args` to compare against the canonical ENVELOPE bytes. Storing
    only the user args would break idempotency and destroy the record of what was actually
    signed. The column name args is the trap here: it holds the whole signed payload, not just
    the call's arguments.

    Row and event are written in ONE transaction, so a call can never exist without
    its 'created' event. `status` is left to the column default 'pending'
    and moved on by the routing step, which is out of this module.

    `nonce_claim` (gate 8, step 4c's `nonce_ledger.NonceClaim`) is written FIRST in that
    same transaction: it commits with the call or not at all, and a claim that loses to
    a concurrent presentation raises `nonce_ledger.NonceReplay` and writes no call. None
    when the gate is off (and from the fixtures that insert a call directly).
    """
    with rls.server_internal(conn):
        if nonce_claim is not None:
            nonce_ledger.claim_nonce(conn, nonce_claim)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skill_calls "
                "  (call_id, caller_peer_id, caller_owner_id, target_peer_id, "
                "   target_owner_id, skill_id, skill_name, skill_version, mode, "
                "   transport, status, args, args_signature, args_signing_kid, "
                "   args_attachments, grant_id, webhook_url, webhook_secret_hash, "
                "   deadline_at) "
                # args_attachments is uuid[]; the explicit cast is required because an
                # EMPTY Python list carries no element type for psycopg to infer.
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,"
                "        %s::uuid[],%s,%s,%s,%s) "
                "RETURNING id, call_id, status, mode, skill_version, deadline_at, "
                "          grant_id, args_signature, args_signing_kid",
                (call_id, str(caller_peer_id), str(caller_owner_id), str(target_peer_id),
                 str(target_owner_id), str(skill_id), skill_name, skill_version, mode,
                 transport, Jsonb(payload), args_signature, args_signing_kid,
                 [str(a) for a in args_attachments], str(grant_id) if grant_id else None,
                 webhook_url, webhook_secret_hash, deadline_at))
            row = cur.fetchone()
            # source='meshy' is a compatibility data value accepted by the live CHECK
            # constraint alongside ('caller','target').
            events.insert_event(cur, call_id, "created")
    return {"id": row[0], "call_id": row[1], "status": row[2], "mode": row[3],
            "skill_version": row[4], "deadline_at": row[5], "grant_id": row[6],
            "args_signature": row[7], "args_signing_kid": row[8]}
