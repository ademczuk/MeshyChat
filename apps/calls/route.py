"""Step 9 of the call pipeline: ROUTE. docs/DIVERGENCES.md 017.

The server has no Redis and no webhook that carries a call (its doorbell, 041 S9, carries
a pointer). Step 9 has two destinations:

  * TARGET IS THE MEMORY AUTHORITY (`CallsContext.memory_peer_id`, from settings
    MESHY_MEMORY_PEER_ID). The skill runs IN-PROCESS through
    `apps.memoryskills.handlers.dispatch` with the VERIFIED payload and the AUTHENTICATED
    caller, on the request's own connection. That connection is the one
    `dbconn.serving_connection` has already proven runs as the NOBYPASSRLS role in
    autocommit, and every handler binds its own `rls.server_internal` transaction, which is
    the only context the three memory tables admit. The result is signed with the server
    key (apps/calls/result_signing.py) and written back in one guarded statement.
  * ANY OTHER TARGET (divergence 041 S5; design 4.4, Q1). `routed` when the target holds a
    socket in ANY worker (`apps/wsgw/hub_presence.is_online`, the cross-worker presence
    table), `queued` otherwise, in ONE guarded statement with its event, committed before
    this returns. The commit fires the status trigger (sql_meshychat/018), whose pointer
    reaches every worker's CallBridge (apps/wsgw/call_notify.py): the worker holding the
    target's socket re-reads the ROW and sends `skill_invoke`, so the target never sees an
    invoke for a row that is not yet `routed`. A `queued` call waits for its target until
    its deadline: reconnect redelivery routes and delivers it, or the deadline sweep times it out
    (`run_call_worker`, apps/calls/worker.py). A target with a verified doorbell URL is
    told by a pointer-only POST from that worker (041 S9, apps/calls/webhooks.py); the
    call stays `queued` until the target connects. The call is no longer terminal when step 9
    returns; the HTTP views answer 202 with Location and the client polls (Q1). A presence
    read that said "offline" for a target that IS connected in some worker is repaired by
    that worker's CallBridge, which routes a `queued` call for a target it holds (4.3, the
    safety net). `E_TARGET_UNREACHABLE` is no longer written by this server.
    A `dashboard_test` call (the one unsigned transport) to a remote target fails here with
    `E_UNVERIFIED_CALL` (critique F16): its row holds no signature, so its skill_invoke
    would carry none, and a verifying target would refuse it only after the delivery; failing
    here refuses it before delivery.

THE STATE MACHINE IS GUARDED:

    pending --route---------------------------> failed     (not executed)
    pending --route--> running --dispatch-----> completed | failed   (signed)
    pending --route--> routed | queued                     (a remote target; 041 S5)

Each arrow is one `UPDATE ... WHERE status = <expected>` in the same transaction as its
audit event. A lost race (a cancel, or the deadline janitor) therefore never produces a
second terminal write; the loser re-reads.

WHAT IS SIGNED is the `skill.result` payload: `{v, kind, call_id,
target_peer_id, status, result, issued_at, nonce}` plus `error_code` / `error_message` on
a failure. `result_payload` stores those exact bytes' source object beside `result`, so the
signature stays attached to what it signed (rules A3 and E2).
A domain failure is `status: failed` with `result = {ok: false, error_code,
error_message}`, which is both what the server signs and what the node client's `_normalize`
promotes.

WHAT IS NOT SIGNED: the two failures above that ran nothing. Signing a result for a skill
this server did not execute would attest to work that never happened.

ONE LIFECYCLE GAP, stated rather than hidden. The handler commits its own transaction and
the terminal UPDATE is a second one. A process that dies between them leaves a memory
write committed and the call `running`; the deadline janitor (apps/calls/janitor.py) then
times it out, so the caller sees `timeout` for a write that landed. The handler cannot
share the transaction: `rls.server_internal` refuses to open inside one.
"""
from __future__ import annotations

import datetime as _dt
import logging
import uuid

from psycopg.types.json import Jsonb

from apps.calls import events, reads, webhooks
from apps.calls.envelope import KIND_SKILL_RESULT, SUPPORTED_VERSION
from apps.core import rls
from apps.wsgw.hub_presence import is_online

log = logging.getLogger("meshychat.calls.route")

#: The id existing node clients address. meshychat/settings.py carries the same default;
#: this constant is what the settings module and tests agree on, not a second source.
DEFAULT_MEMORY_PEER_ID = uuid.UUID("29d6a9f7-ed27-5153-a9f2-f6feb3175e77")

#: Typed failure codes this module writes into `skill_calls.error_code`.
#: E_NO_SIGNING_KEY: the server key could not sign the result. E_UNVERIFIED_CALL: an unsigned
#: (dashboard_test) call to a remote target, refused before delivery.
#: E_TARGET_UNREACHABLE is retained for rows that carry it; divergence 041 S5 no longer
#: writes it for remote targets.
E_TARGET_UNREACHABLE = "E_TARGET_UNREACHABLE"
E_NO_SIGNING_KEY = "E_NO_SIGNING_KEY"
E_UNVERIFIED_CALL = "E_UNVERIFIED_CALL"
E_INTERNAL = "E_INTERNAL"
#: A call carrying an object capability to a target that is not the memory authority
#: (divergence 040, slice 6). The view refuses it before the call starts; this is step 9's
#: own refusal, so no later delivery path can take the call with its token or without it.
E_CAPABILITY_NOT_ACCEPTED = "E_CAPABILITY_NOT_ACCEPTED"

#: The two terminals a signed skill.result can express.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


def memory_peer_id_from_settings() -> uuid.UUID:
    """The ONE reader of settings.MESHY_MEMORY_PEER_ID, for every transport that builds a
    `CallsContext` (apps/calls/views.py, apps/a2a/views.py). No getattr default: the
    setting is always defined by meshychat/settings.py, and a missing one must raise
    rather than fall back to a value an operator meant to replace."""
    from django.conf import settings
    return uuid.UUID(str(settings.MESHY_MEMORY_PEER_ID))


def is_memory_authority(target_peer_id, memory_peer_id) -> bool:
    """The routing decision, and the check most worth mutating: compared as UUIDs, never as
    strings, so a differently-cased path segment cannot route around it."""
    return uuid.UUID(str(target_peer_id)) == uuid.UUID(str(memory_peer_id))


def _iso_z(value: _dt.datetime) -> str:
    """Format a datetime with a trailing UTC `Z`."""
    return value.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def route(conn, *, memory_peer_id, call_id: str, caller_peer_id, target_peer_id,
          skill_name: str, envelope, verified, verified_at: _dt.datetime,
          principal=None) -> str:
    """Run step 9 for a call that step 8 just inserted as `pending`. Returns its status.

    `verified` is the pipeline's `VerifiedPayload`, or None on the one enumerated
    unsigned transport. `caller_peer_id` is the AUTHENTICATED identity, and it is what the
    handler is given; nothing here reads a peer id out of the payload.

    `principal` is the call's object capability (`StartCallInput.object_capability`,
    divergence 040 slice 6), or None: handed to the memory handler, and to nothing else. A
    call to any other target never carries one (the view refuses it first,
    apps/calls/object_capability.py); if one ever did, it fails here unexecuted rather than
    be delivered with or without its token.
    """
    if principal is not None and not is_memory_authority(target_peer_id, memory_peer_id):
        return _fail_unexecuted(
            conn, call_id, E_CAPABILITY_NOT_ACCEPTED,
            "a capability is taken only on a call to the memory authority; this call was not "
            "delivered")

    if not is_memory_authority(target_peer_id, memory_peer_id):
        if verified is None:
            # dashboard_test (critique F16): the row stores no signature, so a skill_invoke
            # built from it would carry none. Refused here, typed, before any delivery.
            return _fail_unexecuted(
                conn, call_id, E_UNVERIFIED_CALL,
                "a remote target is offered only a verified signed envelope")
        return _route_remote(conn, call_id, target_peer_id)

    if verified is None:
        # Only the dashboard_test transport skips verification (pipeline.py step 4). A
        # memory handler writes provenance claiming a verified signature; it must never
        # be handed a payload nobody verified.
        return _fail_unexecuted(
            conn, call_id, E_UNVERIFIED_CALL,
            "memory skills execute only on a verified signed envelope")

    from apps.calls.result_signing import register_under_memory_peer, server_result_signer
    try:
        signer = server_result_signer()
    except Exception as exc:  # noqa: BLE001 - recorded as a typed failure, never swallowed
        # FAIL CLOSED, BEFORE EXECUTING. An executed write whose result cannot be signed
        # would leave a completed call carrying no signature, which is the unsigned
        # success tools/memory/curation_signed_store.py:68-69 refuses to consume.
        log.error("call %s: no result signer (%s: %s); failing without executing",
                  call_id, type(exc).__name__, exc)
        return _fail_unexecuted(conn, call_id, E_NO_SIGNING_KEY,
                                "server result signing key unavailable")

    # Operator decision 38 (the project's design log (not part of this release) 017, F07): the receipt names the memory peer
    # as issuer and carries the signer's kid, so the signer's key is registered under that
    # peer before it signs. A failure here is logged and the call proceeds: the result is
    # still signed and verifiable in /.well-known/jwks.json, only its receipt would not
    # resolve under the issuer, and the next call tries again.
    try:
        register_under_memory_peer(conn, memory_peer_id, signer)
    except Exception:  # noqa: BLE001 - logged with its traceback, never silent
        log.exception("call %s: result key %s not registered under memory peer %s",
                      call_id, signer.kid, memory_peer_id)

    if not _start_running(conn, call_id):
        # Lost the pending -> running race (a cancel, or the janitor). Do NOT execute.
        return _current_status(conn, call_id)

    status, result, code, message = _execute(conn, skill_name, {
        "payload": verified.payload,
        "signature": envelope.signature,
        "signing_kid": envelope.signing_kid,
        "verified_at": _iso_z(verified_at),
    }, caller_peer_id, principal=principal)

    payload = _result_payload(call_id, target_peer_id, status, result, code, message)
    try:
        signature = signer.sign(payload)
    except Exception:  # noqa: BLE001 - replaced by a signed failure, and logged
        # A result RFC 8785 cannot canonicalize (an integer beyond 2**53 - 1 in a stored
        # value, for one) cannot be signed. Record a SIGNED failure rather than an
        # unsigned success; a write that already landed stays landed and is reported.
        log.exception("call %s: result not canonicalizable; recording E_INTERNAL", call_id)
        status, code, message = STATUS_FAILED, E_INTERNAL, "result is not canonicalizable"
        result = {"ok": False, "error_code": code, "error_message": message}
        payload = _result_payload(call_id, target_peer_id, status, result, code, message)
        signature = signer.sign(payload)

    finished = _finish(conn, call_id, status=status, result=result, payload=payload,
                       signature=signature, signing_kid=signer.kid, error_code=code,
                       error_message=message)
    if finished is None:
        return _current_status(conn, call_id)
    return finished


def _execute(conn, skill_name: str, verified_payload: dict, caller_peer_id, *,
             principal=None):
    """Dispatch and turn every outcome into (status, result, error_code, error_message).

    A `HandlerError` keeps its registered code; anything else is `E_INTERNAL` with the
    constant message and its traceback in the log, never in the result.

    `principal` (divergence 040, slice 6) goes to `handlers.dispatch`, which hands it to the
    handler; None is today's call, unchanged.
    """
    outcome = _dispatch(conn, skill_name, verified_payload, caller_peer_id,
                        principal=principal)
    # STATUS F10: memory.query runs HERE, so it is counted here, after the handler's own
    # transaction has ended, in one of the counter's own (apps/mesh/usage.py). A count that
    # cannot land is logged and counted there; it never changes this call's outcome.
    from apps.mesh import usage
    usage.record_call(conn, skill_name, caller_peer_id, outcome[2])
    return outcome


def _dispatch(conn, skill_name: str, verified_payload: dict, caller_peer_id, *,
              principal=None):
    """`_execute`'s dispatch and its mapping, unchanged; `_execute` counts what it returns."""
    from apps.memoryskills import handlers
    from apps.memoryskills.errors import HandlerError

    try:
        result = handlers.dispatch(skill_name, verified_payload, caller_peer_id, conn,
                                   principal=principal)
    except HandlerError as exc:
        return (STATUS_FAILED,
                {"ok": False, "error_code": exc.code, "error_message": exc.message},
                exc.code, exc.message)
    except Exception:  # noqa: BLE001 - becomes a typed, signed failure; logged in full
        log.exception("memory skill %s raised", skill_name)
        return (STATUS_FAILED,
                {"ok": False, "error_code": E_INTERNAL, "error_message": "internal error"},
                E_INTERNAL, "internal error")
    if not isinstance(result, dict):
        log.error("memory skill %s returned %s, not an object", skill_name,
                  type(result).__name__)
        return (STATUS_FAILED,
                {"ok": False, "error_code": E_INTERNAL, "error_message": "internal error"},
                E_INTERNAL, "internal error")
    return STATUS_COMPLETED, result, None, None


def _result_payload(call_id, target_peer_id, status, result, error_code, error_message):
    """Build the signed result payload.

    `target_peer_id` is the peer the result is FROM: the memory authority. The nonce is
    signed and inert.
    """
    payload = {
        "v": SUPPORTED_VERSION,
        "kind": KIND_SKILL_RESULT,
        "call_id": call_id,
        "target_peer_id": str(target_peer_id),
        "status": status,
        "result": result,
        "issued_at": _iso_z(_dt.datetime.now(_dt.timezone.utc)),
        "nonce": str(uuid.uuid4()),
    }
    if error_code is not None:
        payload["error_code"] = error_code
    if error_message is not None:
        payload["error_message"] = error_message
    return payload


# ------------------------------------------------------------ guarded transitions

def _route_remote(conn, call_id: str, target_peer_id) -> str:
    """pending -> routed | queued, and its event, in ONE transaction (design 4.4; 041 S5).

    `online` is S2's presence read (`hub_presence.is_online`), which runs in its own
    server_internal transaction just before: `rls.server_internal` refuses to nest, and the
    store is the only module that names its table. A socket that connects between the two
    is found by its own connect-time redelivery, which routes a `pending` or `queued` row
    (apps/wsgw/call_notify.py); one that drops between them leaves a `routed` row that its
    reconnect, re-offer sweep, or deadline ends.

    The UPDATE is guarded on `pending`. A reconnecting target's redelivery can route the row
    first (step 8 committed it before this ran); then nothing matches, no event is written,
    and step 9 reports the status it re-reads. The COMMIT fires the status trigger's pointer
    (sql_meshychat/018), so no worker can offer a row before it is `routed`.

    THE DOORBELL (041 S9, design 4.4 and 4.11). A `queued` edge also inserts, in this same
    transaction, a `due` doorbell attempt when the target has a verified registration and was
    not rung in the last 60 s (`webhooks.insert_due`, ON CONFLICT DO NOTHING on the one-
    outstanding-per-peer index). Its insert trigger wakes `run_call_worker` at the commit.
    A `routed` edge inserts none: a socket already has the call."""
    online = is_online(conn, target_peer_id)
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skill_calls SET status = CASE WHEN %s THEN 'routed' ELSE 'queued' END "
                " WHERE call_id = %s AND status = 'pending' RETURNING status",
                (online, call_id))
            row = cur.fetchone()
            if row is not None:
                events.insert_event(cur, call_id, row[0])
                if row[0] == "queued":
                    webhooks.insert_due(cur, call_id=call_id, peer_id=target_peer_id)
    if row is None:
        return _current_status(conn, call_id)
    log.info("call %s to peer %s: %s", call_id, target_peer_id, row[0])
    return row[0]


def _start_running(conn, call_id: str) -> bool:
    """It skips the routed step: there is no delivery to acknowledge, so a memory call goes
    straight from pending to running. Returns False when the call was no longer pending."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skill_calls SET status = 'running', started_at = NOW() "
                " WHERE call_id = %s AND status = 'pending' RETURNING call_id",
                (call_id,))
            if cur.fetchone() is None:
                return False
            events.insert_event(cur, call_id, "running")
    return True


def _fail_unexecuted(conn, call_id: str, error_code: str, error_message: str) -> str:
    """Move pending -> failed with no result or signature and add its event."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skill_calls SET status = 'failed', error_code = %s, "
                "       error_message = %s, completed_at = NOW() "
                " WHERE call_id = %s AND status = 'pending' RETURNING status",
                (error_code, error_message, call_id))
            row = cur.fetchone()
            if row is not None:
                events.insert_event(cur, call_id, "failed",
                                    details={"code": error_code, "message": error_message})
    if row is None:
        return _current_status(conn, call_id)
    log.info("call %s failed without executing: %s", call_id, error_code)
    return row[0]


def _finish(conn, call_id: str, *, status, result, payload, signature, signing_kid,
            error_code, error_message) -> str | None:
    """running -> completed | failed, result + canonical payload + signature + error
    fields in ONE statement, and its event in the same transaction. `result_payload` is
    written as a PAIR with `result`, never coalesced. Returns the new status, or None when the call was no longer
    running."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skill_calls SET status = %s, result = %s, result_payload = %s, "
                "       result_signature = %s, result_signing_kid = %s, "
                "       error_code = %s, error_message = %s, completed_at = NOW() "
                " WHERE call_id = %s AND status = 'running' RETURNING status",
                (status, Jsonb(result), Jsonb(payload), signature, signing_kid,
                 error_code, error_message, call_id))
            row = cur.fetchone()
            if row is not None:
                events.insert_event(cur, call_id, status)
    return None if row is None else row[0]


def _current_status(conn, call_id: str) -> str:
    found = reads.load_call(conn, call_id)
    if isinstance(found, reads.NotFound):
        # The row was inserted by this request inside the call_id lock; a vanished row is
        # a fault, not a status to report.
        raise RuntimeError(f"call {call_id} vanished during routing")
    return found.row["status"]


__all__ = ["DEFAULT_MEMORY_PEER_ID", "E_NO_SIGNING_KEY", "E_TARGET_UNREACHABLE",
           "E_UNVERIFIED_CALL", "is_memory_authority", "memory_peer_id_from_settings",
           "route"]
