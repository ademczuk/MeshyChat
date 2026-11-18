"""The TARGET's frames: skill_ack, skill_running, skill_result and skill_error (divergence 041 S4),
and skill_chunk (S7).

Design `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6): 4.7, 5.1 and 4.4,
the approved Q5, Q6 and Q7, critique F4 and F7, and risk R12. The handlers that call these are
in `apps/wsgw/frame_handlers.py`.

Each handler is ONE guarded statement that checks the call and writes in one step. Three
properties matter:

  1. THE CHECK IS THE STATEMENT. Each frame is ONE guarded statement under
     `rls.server_internal`, and its WHERE holds all of

         c.call_id = <the frame's>
         c.status = <the status the edge starts from>
         c.target_peer_id = <this socket's peer>                    design 5.1
         c.target_peer_id <> <the memory authority>                 design 4.4
         EXISTS (this peer is 'active')                             design 4.7, R12
         EXISTS (a live peer_keys row for the socket's bound key)   design 4.7, critique F4

     (the last only for a socket whose token is bound to a key). So nothing separates "is
     this mine" from "change it"; a suspended peer, or a socket whose key was revoked, is
     refused in the window before divergence 030 closes its socket (a suspension is plain SQL
     and sends no notification, so that window is up to REVOCATION_SWEEP_SECS); and the memory
     authority, whose calls run IN-PROCESS between pending and their terminal, can never race
     that run from a socket. The event is written in the same transaction, in the shape of
     `apps/calls/route.py`.

     The live-key clause is `revocation.key_live_sql`, THE rule `revocation.key_states`
     applies, over the rows naming the bound key. Two differences from `key_states`, both
     toward refusing: a key with no row is NOT live here (the upgrade and the sweep call it
     unknown and leave the socket open; a frame is an act, and needs a key known to be live),
     and the key is matched by its base64url TEXT with padding stripped and the standard
     alphabet mapped, not by decoded bytes, so a stored spelling that only decoding would
     match reads as no row. `apps/peers/keys.py` stores only unpadded base64url.

  2. ONE REFUSAL FOR "NO SUCH CALL" AND "NOT YOURS" (Q7, the WS door only). A statement that
     matched nothing re-reads the call under the SAME ownership clauses. If
     that finds nothing, the answer is the frame's code with `NOT_ROUTED_TO_THIS_PEER`,
     whatever the reason, so a peer cannot probe which call_ids exist. Only the server log says
     which (`_why_refused`). Once
     ownership is proven the answer may say more: an idempotent success when the call already
     has the status the frame asks for, else the state-transition text naming its status.

  3. A RESULT IS BOUND TO ITS FRAME, AND ITS STATUS IS NOT COERCED. The signed call_id must be
     the frame's, so a target cannot file call B's signed result as call A's. The terminal is
     the SIGNED status: absent or `completed` gives completed, `failed` gives failed, and
     anything else is SKILL_RESULT_BAD (Q6), because storing it as completed would leave a row
     that contradicts the signature.
     The row's `result`, `error_code` and `error_message` are the signed payload's own
     fields, NULL when absent, for `completed` too (critique F7). The fleet's verifier binds
     all five of status, call_id, result, error_code and error_message to the row
     (`tools/meshy_node/wire.RESULT_BOUND_FIELDS`).

     A signed `result_attachments` other than an empty list is SKILL_RESULT_BAD too (the S4
     review's finding 5). Attachment validation is not implemented here: accepting one needs
     validation against the skill's `returns_attachments`, the target's owner and the signed
     `_attachments` manifest. The row's list decides who may read a file
     (`apps/files/repository.find_referencing_calls`), so this handler rejects an unvalidated
     list rather than storing or silently dropping a signed one.

NO NONCE LEDGER ON skill.result (Q5). A replayed result is inert: the state guard admits one
terminal, the binding pins it to its call, and verification pins it to its signer.

SKILL_CHUNK (S7; `chunk` below; design 4.7's skill_chunk row, 5.2, Q5, Q11 and F9):

  * `chunk_type` text or json. `file_ref` is SKILL_CHUNK_BAD until its slice (Q11): it needs
    apps/files' `ready` and owner checks. The
    domain (text, json, file_ref) and `0 <= sequence <= 2147483647` are S1's parse.
  * `verify_payload(kind = skill.chunk, peer = the socket's)`, then the BINDING: the signed
    `call_id`, `sequence` and `chunk_type` are each the frame's (the sequence an integer, not a
    float or a boolean that compares equal). This binding prevents a chunk signed for call A at
    sequence N from being filed as call B's at M.
  * The stored `content` is the RFC 8785 bytes of the verified payload (apps/crypto's jcs): the
    exact bytes the signature covers, so `chunk_signature` verifies over `content` with no
    re-canonicalisation (rule A3). At most 512 KiB of those bytes are stored.
  * NO NONCE LEDGER (Q5), as for skill.result: a replayed chunk meets `UNIQUE (call_id,
    sequence)` or a call that is no longer running.
  * THE CHECK IS THE STATEMENT, as for the other four frames, in ONE statement with one
    snapshot: a CTE reads the call under the ownership clauses (`_owned`: target = me, not the
    memory authority, the peer active, the bound key live), inserts the chunk only if that call
    is `running`, and returns the status it saw. Nothing seen: the uniform refusal,
     SKILL_CHUNK_FAILED with NOT_ROUTED_TO_THIS_PEER, whatever the reason, so a peer cannot
     probe which call_ids exist. Seen and not running: SKILL_CHUNK_NOT_RUNNING with the
     state-transition text. A unique violation
    is SKILL_CHUNK_BAD `duplicate sequence`, told only to the call's own running target, since
    no other peer's insert selects a row. The insert's pointer (sql_meshychat/018) brings the
    chunk to the caller's registered sockets (apps/wsgw/call_notify.py).

THE TRIM AT TERMINAL (S7, design 5.2): `result` and `error` keep the call's newest 200 chunks
(`apps/calls/chunks.trim`, sql/008's `trim_chunks_for_call`) inside the transaction of their
terminal edge.

THE SOCKET'S OWN CONNECTION (design 4.7). `SocketConnection` opens one role-bound autocommit
connection (`apps/core/dbconn.connect`) at the socket's first target frame, reuses it for every
later one, and is closed in the consumer's disconnect. Channels dispatches one message at a
time per consumer, so the frames keep their wire order and the connection is never used by two
statements at once; the lock is for a close that meets a statement still running. It is
bounded (STATEMENT_TIMEOUT_MS, CONNECT_TIMEOUT_SECS), as every CallBridge connection is since
the S3 review's F1, and it names its socket in pg_stat_activity. A database error drops it,
and the next frame opens a fresh one.

Routing and invoke delivery are S5's (apps/calls/route.py, apps/wsgw/call_notify.py), the
caller's frames S6's (apps/wsgw/caller_frames.py, whose cancel carries `peer_standing` below).
Nothing tells a target that its call was cancelled or timed out (Q12).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import threading
import uuid

import psycopg
from psycopg.types.json import Jsonb

from apps.calls import chunks as call_chunks
from apps.calls import wire
from apps.calls.envelope import (KIND_SKILL_CHUNK, KIND_SKILL_RESULT, SignedEnvelope,
                                 verify_payload)
from apps.calls.errors import SignatureFailed
from apps.core import dbconn, rls
from apps.crypto import primitives as P

from . import envelope, revocation

log = logging.getLogger(__name__)

#: The uniform refusal's text (Q7): the same for a call that does not exist and a call that is
#: not this peer's, and for a call routed to the memory authority, a suspended peer and a dead
#: bound key.
NOT_ROUTED_TO_THIS_PEER = "call is not routed to this peer"
#: A database failure's text. It names nothing about the call.
INTERNAL_ERROR = "internal error"

#: The socket connection's bounds (S3 review F1): a statement waiting on a row another
#: transaction holds gives up, and the frame is answered, instead of holding the socket.
STATEMENT_TIMEOUT_MS = 5000
CONNECT_TIMEOUT_SECS = 5
#: pg_stat_activity's application_name for a socket's connection: this prefix and the socket's
#: connection_id (the one its `welcome` frame carries). 52 characters, under Postgres's 63.
APPLICATION_NAME_PREFIX = "meshy-ws-frames-"

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

#: The chunk types a target may send now (design Q11: `file_ref` is deferred).
CHUNK_TYPES_STORED = frozenset({"text", "json"})
FILE_REF_DEFERRED = "file_ref chunks are not accepted by this server yet (design Q11)"
DUPLICATE_SEQUENCE = "duplicate sequence"


def chunk_limit(cap: int) -> str:
    """The refusal text for a chunk past `apps/calls/chunks.MAX_CHUNKS_PER_CALL` (S7 review H1)."""
    return f"chunk limit reached: a call holds at most {cap} chunks"

#: The ownership clauses (see the module docstring, 1). The call's alias is `c`.
_TARGET_IS_ME = "c.target_peer_id = %(me)s"
_NOT_THE_AUTHORITY = "c.target_peer_id <> %(authority)s"
_PEER_ACTIVE = "EXISTS (SELECT 1 FROM peers p WHERE p.id = %(me)s AND p.status = 'active')"
_BOUND_KEY_LIVE = (
    "EXISTS (SELECT 1 FROM peer_keys k WHERE k.peer_id = %(me)s"
    " AND rtrim(translate(k.public_key, '+/', '-_'), '=') = %(bound_key)s"
    f" AND ({revocation.key_live_sql('k')}))")


@dataclasses.dataclass(frozen=True)
class Refusal:
    """The error frame a target frame is answered with. None means it landed (no reply)."""

    code: str
    message: str

    def frame(self) -> dict:
        return envelope.error(self.code, self.message)


class SocketClosed(RuntimeError):
    """A target frame after the socket's connection was closed."""


class SocketConnection:
    """One socket's own connection for its target frames. See the module docstring."""

    def __init__(self, connection_id: uuid.UUID, dsn: str | None = None) -> None:
        self.connection_id = connection_id
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None
        self._closed = False
        self._lock = threading.Lock()

    def run(self, statement, /, **kwargs):
        """`statement(conn, **kwargs)` on this socket's connection, opened if it is not.
        Called on a worker thread (the consumer is async and psycopg's connection is not)."""
        with self._lock:
            if self._closed:
                raise SocketClosed("this socket's frame connection is closed")
            if self._conn is None:
                self._conn = dbconn.connect(
                    self._dsn or dbconn.dsn(), connect_timeout=CONNECT_TIMEOUT_SECS,
                    statement_timeout_ms=STATEMENT_TIMEOUT_MS,
                    application_name=APPLICATION_NAME_PREFIX + str(self.connection_id))
            try:
                return statement(self._conn, **kwargs)
            except psycopg.Error:
                # A statement bound, a broken connection, anything the database raised: this
                # connection is discarded. The next frame opens a fresh one.
                self._drop()
                raise

    def close(self) -> None:
        """The consumer's disconnect. Idempotent; a later `run` raises SocketClosed."""
        with self._lock:
            self._closed = True
            self._drop()

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - a connection that will not close is gone anyway
                log.warning("a socket's frame connection did not close cleanly", exc_info=True)


# ------------------------------------------------------------------------------ the clauses

def peer_standing(bound_key: bytes | None) -> str:
    """The socket's peer may still act: it is `active`, and, for a socket whose token is bound
    to a key, that key is live. The caller's cancel carries these two clauses too (divergence
    041 S6, apps/wsgw/caller_frames.cancel_by_caller); `%(me)s` and `%(bound_key)s` as
    `_params` binds them."""
    clauses = [_PEER_ACTIVE]
    if bound_key is not None:
        clauses.append(_BOUND_KEY_LIVE)
    return " AND ".join(clauses)


def _owned(bound_key: bytes | None) -> str:
    """The ownership clauses for this socket, joined for a WHERE (module docstring, 1)."""
    clauses = [_TARGET_IS_ME, _NOT_THE_AUTHORITY, _PEER_ACTIVE]
    if bound_key is not None:
        clauses.append(_BOUND_KEY_LIVE)
    return " AND ".join(clauses)


def _params(*, call_id: str, me: uuid.UUID, bound_key: bytes | None,
            authority: uuid.UUID, **more) -> dict:
    params = {"call_id": call_id, "me": me, "authority": authority,
              "bound_key": None if bound_key is None else P.b64u(bound_key)}
    params.update(more)
    return params


def _event(cur, event_type: str, params: dict, details=None) -> None:
    """The edge's event, `source = 'target'`, on the statement's own transaction."""
    cur.execute(
        "INSERT INTO skill_call_events (call_id, event_type, details, source, source_peer_id) "
        "VALUES (%(call_id)s, %(event_type)s, %(details)s, 'target', %(me)s)",
        dict(params, event_type=event_type,
             details=None if details is None else Jsonb(details)))


def _resolve(conn, frame_type: str, code: str, params: dict, bound_key: bytes | None, *,
             desired: str, edge: str) -> Refusal | None:
    """A guarded statement matched nothing: re-read the call under the SAME ownership clauses
    (module docstring, 2). Not this peer's to hear about: the uniform refusal. Already at
    `desired`: an idempotent success. Anything else: the state-transition text.

    For skill_ack `desired` is `routed`, the status an ack leaves unchanged: the re-read finds
    it only for a call that became routed between the two statements, and that ack is
    answered as a success with no event (the event is the audit trail's; the target's
    skill_running is what moves the call)."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT c.status FROM skill_calls c "
                        "WHERE c.call_id = %(call_id)s AND " + _owned(bound_key), params)
            row = cur.fetchone()
    if row is None:
        log.warning("%s refused for call %r from peer %s: %s", frame_type, params["call_id"],
                    params["me"], _why_refused(conn, params, bound_key))
        return Refusal(code, NOT_ROUTED_TO_THIS_PEER)
    if row[0] == desired:
        return None
    return Refusal(code, f"state transition: cannot move from {row[0]} to {edge}")


def _why_refused(conn, params: dict, bound_key: bytes | None) -> str:
    """For the SERVER LOG ONLY: which clause refused. The wire answer never depends on it."""
    try:
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT (SELECT c.target_peer_id FROM skill_calls c "
                            "         WHERE c.call_id = %(call_id)s), "
                            "       (SELECT p.status FROM peers p WHERE p.id = %(me)s)", params)
                target, peer_status = cur.fetchone()
        me = params["me"]
        if target is None:
            return "no such call"
        if target != me:
            return "the call targets another peer"
        if target == params["authority"]:
            return "the call targets the memory authority, whose calls run in-process"
        if peer_status != "active":
            return f"this peer is not active (status {peer_status})"
        if bound_key is not None:
            state = revocation.key_states(conn, [(me, bound_key)])[(me, bound_key)]
            if state != revocation.KEY_LIVE:
                return f"the key this socket's token is bound to is {state}"
        return ("not explained by the call, the peer or its key: the row moved between the "
                "statement and the re-read, or the key is stored in another spelling")
    except Exception as exc:  # noqa: BLE001 - a diagnosis must never change the answer
        return f"not diagnosed ({type(exc).__name__})"


# ---------------------------------------------------------------------------- the frames

def ack(conn, *, call_id: str, me: uuid.UUID, bound_key: bytes | None,
        authority: uuid.UUID) -> Refusal | None:
    """skill_ack: no status change, a
    `target_acked` event only, written only while the call is `routed` and this peer's."""
    params = _params(call_id=call_id, me=me, bound_key=bound_key, authority=authority)
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skill_call_events (call_id, event_type, details, source, "
                "  source_peer_id) "
                "SELECT c.call_id, 'target_acked', NULL::jsonb, 'target', %(me)s "
                "  FROM skill_calls c "
                " WHERE c.call_id = %(call_id)s AND c.status = 'routed' AND "
                + _owned(bound_key) + " RETURNING id", params)
            if cur.fetchone() is not None:
                return None
    return _resolve(conn, envelope.SKILL_ACK, envelope.CODE_SKILL_ACK_FAILED, params, bound_key,
                    desired="routed", edge="acked")


def running(conn, *, call_id: str, me: uuid.UUID, bound_key: bytes | None,
            authority: uuid.UUID) -> Refusal | None:
    """skill_running: routed to running,
    `started_at`, and a `running` event with `source = 'target'`."""
    params = _params(call_id=call_id, me=me, bound_key=bound_key, authority=authority)
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skill_calls AS c SET status = 'running', started_at = NOW() "
                " WHERE c.call_id = %(call_id)s AND c.status = 'routed' AND "
                + _owned(bound_key) + " RETURNING c.call_id", params)
            if cur.fetchone() is not None:
                _event(cur, "running", params)
                return None
    return _resolve(conn, envelope.SKILL_RUNNING, envelope.CODE_SKILL_RUNNING_FAILED, params,
                    bound_key, desired="running", edge="running")


def result(conn, *, call_id: str, env: SignedEnvelope, me: uuid.UUID,
           bound_key: bytes | None, authority: uuid.UUID) -> Refusal | None:
    """skill_result: verified as `skill.result` signed
    by this peer, bound to the frame's call_id, its terminal the SIGNED status, then running
    to that terminal in ONE guarded statement with the result, the verified payload, its
    signature and kid, and the signed error pair (module docstring, 3)."""
    code = envelope.CODE_SKILL_RESULT_FAILED
    try:
        verified = verify_payload(conn, env, me, KIND_SKILL_RESULT)
    except SignatureFailed as exc:
        return Refusal(exc.code, str(exc))
    payload = verified.payload
    bad = _refuse_payload(payload, call_id)
    if bad is not None:
        return bad
    terminal = STATUS_COMPLETED if payload.get("status", STATUS_COMPLETED) == STATUS_COMPLETED \
        else STATUS_FAILED
    params = _params(
        call_id=call_id, me=me, bound_key=bound_key, authority=authority,
        terminal=terminal,
        result=Jsonb(payload["result"]) if "result" in payload else None,
        payload=Jsonb(payload), signature=env.signature, signing_kid=env.signing_kid,
        error_code=payload.get("error_code"), error_message=payload.get("error_message"))
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skill_calls AS c SET status = %(terminal)s, result = %(result)s, "
                "       result_payload = %(payload)s, result_signature = %(signature)s, "
                "       result_signing_kid = %(signing_kid)s, error_code = %(error_code)s, "
                "       error_message = %(error_message)s, completed_at = NOW() "
                " WHERE c.call_id = %(call_id)s AND c.status = 'running' AND "
                + _owned(bound_key) + " RETURNING c.call_id", params)
            if cur.fetchone() is not None:
                _event(cur, terminal, params)
                call_chunks.trim(cur, call_id)                       # 041 S7, design 5.2
                return None
    return _resolve(conn, envelope.SKILL_RESULT, code, params, bound_key,
                    desired=terminal, edge=terminal)


def error(conn, *, call_id: str, error_code: str, error_message: str, me: uuid.UUID,
          bound_key: bytes | None, authority: uuid.UUID) -> Refusal | None:
    """skill_error: running to failed, unsigned,
    with the error pair (bounded at parse, envelope.MAX_ERROR_*_CHARS) on the row and in the
    `failed` event's details."""
    params = _params(call_id=call_id, me=me, bound_key=bound_key, authority=authority,
                     error_code=error_code, error_message=error_message)
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skill_calls AS c SET status = 'failed', error_code = %(error_code)s, "
                "       error_message = %(error_message)s, completed_at = NOW() "
                " WHERE c.call_id = %(call_id)s AND c.status = 'running' AND "
                + _owned(bound_key) + " RETURNING c.call_id", params)
            if cur.fetchone() is not None:
                _event(cur, STATUS_FAILED, params,
                       details={"code": error_code, "message": error_message})
                call_chunks.trim(cur, call_id)                       # 041 S7, design 5.2
                return None
    return _resolve(conn, envelope.SKILL_ERROR, envelope.CODE_SKILL_ERROR_FAILED, params,
                    bound_key, desired=STATUS_FAILED, edge=STATUS_FAILED)


def chunk(conn, *, call_id: str, sequence: int, chunk_type: str, env: SignedEnvelope,
          me: uuid.UUID, bound_key: bytes | None, authority: uuid.UUID) -> Refusal | None:
    """skill_chunk: text or json, verified as `skill.chunk`
    signed by this peer, bound to the frame's call_id, sequence and chunk_type, at most 512 KiB
    stored, then stored for this peer's RUNNING call in ONE statement (module docstring)."""
    bad = envelope.CODE_SKILL_CHUNK_BAD
    if chunk_type not in CHUNK_TYPES_STORED:
        return Refusal(bad, FILE_REF_DEFERRED)
    try:
        verified = verify_payload(conn, env, me, KIND_SKILL_CHUNK)
    except SignatureFailed as exc:
        return Refusal(exc.code, str(exc))
    refused = _refuse_chunk_payload(verified.payload, call_id, sequence, chunk_type)
    if refused is not None:
        return refused
    content = P.jcs(verified.payload)
    if len(content) > call_chunks.MAX_CHUNK_CONTENT_BYTES:
        return Refusal(bad, f"content {len(content)} bytes exceeds max "
                            f"{call_chunks.MAX_CHUNK_CONTENT_BYTES}")
    cap = call_chunks.MAX_CHUNKS_PER_CALL
    params = _params(call_id=call_id, me=me, bound_key=bound_key, authority=authority,
                     sequence=sequence, chunk_type=chunk_type, content=content,
                     signature=env.signature, signing_kid=env.signing_kid, cap=cap)
    try:
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "WITH owned AS ("
                    " SELECT c.call_id, c.status FROM skill_calls c"
                    "  WHERE c.call_id = %(call_id)s AND " + _owned(bound_key) + "), "
                    # The cap (S7 review H1): the chunks this peer's RUNNING call holds, counted
                    # up to the cap through UNIQUE (call_id, sequence). Nothing is counted for a
                    # call that is not owned, or not running: no other peer learns the count.
                    "held AS ("
                    " SELECT count(*) AS n FROM ("
                    "  SELECT 1 FROM owned o JOIN skill_call_chunks k ON k.call_id = o.call_id"
                    "   WHERE o.status = 'running' LIMIT %(cap)s) AS counted), "
                    "stored AS ("
                    " INSERT INTO skill_call_chunks (call_id, sequence, chunk_type, content,"
                    "   chunk_signature, chunk_signing_kid)"
                    " SELECT o.call_id, %(sequence)s, %(chunk_type)s, %(content)s,"
                    "        %(signature)s, %(signing_kid)s"
                    "   FROM owned o, held h WHERE o.status = 'running' AND h.n < %(cap)s"
                    " RETURNING id) "
                    "SELECT (SELECT o.status FROM owned o), (SELECT count(*) FROM stored)",
                    params)
                status, stored = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return Refusal(bad, DUPLICATE_SEQUENCE)
    if stored:
        return None
    if status is None:
        log.warning("%s refused for call %r from peer %s: %s", envelope.SKILL_CHUNK, call_id,
                    me, _why_refused(conn, params, bound_key))
        return Refusal(envelope.CODE_SKILL_CHUNK_FAILED, NOT_ROUTED_TO_THIS_PEER)
    if status != "running":
        # The row is owned (the CTE selected it); report its current state.
        return Refusal(envelope.CODE_SKILL_CHUNK_NOT_RUNNING,
                       f"state transition: cannot move from {wire.debug_status(status)} "
                       f"to one of [Running]")
    # Owned and running, and nothing stored: the one clause left is the cap.
    log.warning("%s refused for call %r from peer %s: the call holds %d chunks",
                envelope.SKILL_CHUNK, call_id, me, cap)
    return Refusal(bad, chunk_limit(cap))


# ------------------------------------------------------------- what a signed result must be

def _refuse_payload(payload: dict, call_id: str) -> Refusal | None:
    """A VERIFIED skill.result payload that is not stored, or None. Every check reads
    the frame and its payload only, never the call, so none of them says anything about a
    call the peer does not own."""
    bad = envelope.CODE_SKILL_RESULT_BAD
    if payload.get("call_id") != call_id:
        return Refusal(bad, "the signed call_id is not this frame's call_id: a result is "
                            "filed only under the call it was signed for")
    if "status" in payload and payload["status"] not in (STATUS_COMPLETED, STATUS_FAILED):
        return Refusal(bad, f"the signed status must be completed or failed, or absent for "
                            f"completed; got {_show(payload['status'])}")
    for name in ("error_code", "error_message"):
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            return Refusal(bad, f"the signed {name} must be a string when present")
    if "result_attachments" in payload and payload["result_attachments"] != []:
        return Refusal(bad, "the signed result_attachments must be absent or an empty list: "
                            "this server does not accept result attachments yet")
    if _unstorable(payload):
        return Refusal(bad, "the signed payload holds U+0000 or a lone surrogate, which the "
                            "database cannot store")
    return None


def _refuse_chunk_payload(payload: dict, call_id: str, sequence: int,
                          chunk_type: str) -> Refusal | None:
    """A VERIFIED skill.chunk payload that is not bound to its frame, or None. It reads the
    frame and its payload only, never the call."""
    bad = envelope.CODE_SKILL_CHUNK_BAD
    if payload.get("call_id") != call_id:
        return Refusal(bad, "the signed call_id is not this frame's call_id")
    signed = payload.get("sequence")
    if type(signed) is not int or signed != sequence:
        return Refusal(bad, "the signed sequence is not this frame's sequence")
    if payload.get("chunk_type") != chunk_type:
        return Refusal(bad, "the signed chunk_type is not this frame's chunk_type")
    return None


def _show(value) -> str:
    """A signed value, for a message: JSON, at most 64 characters."""
    text = json.dumps(value, ensure_ascii=True)
    return text if len(text) <= 64 else text[:61] + "..."


def _unstorable(value) -> bool:
    """Whether any string in the payload, key or value, holds what `jsonb` and `text` refuse:
    U+0000, or a lone surrogate that is not UTF-8. Iterative: a deep payload is not a
    recursion limit."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False
