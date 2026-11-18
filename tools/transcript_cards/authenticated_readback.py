"""Private, read-only bridge for an own-seat frozen outbox.

Present, unedited version-one context heads and authenticated exact-key absence
are supported. Refusals, redactions and uncertain answers remain pending. This is
not namespace admission, screening, a publisher or native-history ingestion.
Keys are fetched afresh from the configured server, using the existing node
client's trust boundary; saved JWKS are evidence, not independent trust roots.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid

from tools.meshy_node import node, wire
from frozen_outbox import MAX_EVIDENCE_BYTES, StoreError, StoreConflict, _json, _same_value


NAMESPACE = "shared/transcript-context"


class ReadbackPending(StoreError):
    """No authenticated observation was committed; retain the pending attempt."""


@dataclass(frozen=True)
class Receipt:
    state: str
    evidence_sha256: str


def _require(condition, reason):
    if not condition:
        # Fixed messages only: never echo server content, native text or keys.
        raise ReadbackPending(reason)


def _envelope_ok(envelope, jwks):
    if type(envelope) is not dict or type(jwks) is not dict:
        return False
    keys = jwks.get("keys")
    if type(keys) is not list:
        return False
    found = [key for key in keys if type(key) is dict
             and key.get("kid") == envelope.get("signing_kid")]
    if len(found) != 1:
        return False
    key = found[0]
    try:
        return (key.get("kty") == "OKP" and key.get("crv") == "Ed25519"
                and wire.verify(wire.b64u_decode(key["x"]), wire.jcs(envelope["payload"]),
                                wire.b64u_decode(envelope["signature"])))
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def reconcile_present(outbox, batch, *, state_path, state, seat, writer_peer_id,
                      poll_timeout=40.0, boundary=None):
    """Read once through signed invoke and atomically retain proof + observation.

    `seat` and `writer_peer_id` are explicit trusted configuration, never taken
    from readback. Only this identity's own frozen records can be reconciled.
    A genuine authenticated different value commits conflict. A printed CLI
    verification flag cannot authorize progress. No writes or consuming reads.
    """
    try:
        writer = str(uuid.UUID(writer_peer_id))
    except (ValueError, TypeError, AttributeError):
        raise ReadbackPending("trusted writer configuration required") from None
    _require(type(seat) is str and bool(seat) and state.get("peer_id") == writer,
             "own-seat identity configuration required")
    action = outbox.next_action(batch)
    if action.kind != "read":
        raise StoreConflict("current pending read required")
    frozen = json.loads(action.record.value)
    _require(frozen.get("seat") == seat, "frozen seat differs from configuration")
    args = {"namespace": NAMESPACE, "key": action.record.key, "include_provenance": True}
    try:
        result = node.invoke(state_path, state, skill="memory.read", args=args,
                             poll_timeout=poll_timeout, include_verification_evidence=True)
        author_jwks, unavailable = node.author_keys(state["base_url"], writer)
    except (OSError, node.Refused, node.ServerSaid):
        raise ReadbackPending("read transport unavailable") from None
    _require(author_jwks is not None and unavailable is None, "writer keys unavailable")
    try:
        _require(type(result) is dict, "raw verification evidence required")
        proof = result.get("verification_evidence")
        _require(type(proof) is dict, "raw verification evidence required")
        call, server_jwks, request = (proof.get("call"), proof.get("server_jwks"),
                                      proof.get("request_envelope"))
        _require(type(call) is dict and type(server_jwks) is dict,
                 "signed server evidence required")
        # Reject duplicate matching server kids rather than wire's last-key wins.
        keys = server_jwks.get("keys")
        _require(type(keys) is list and sum(type(k) is dict and
                 k.get("kid") == call.get("result_signing_kid") for k in keys) == 1,
                 "unique server key required")
        verified, _ = wire.result_signature_ok(call, server_jwks)
        _require(verified, "server signature unverified")
        payload = call["result_payload"]
        _require(all(_same_value(payload.get(k), call.get(k))
                     for k in wire.RESULT_BOUND_FIELDS), "signed result binding differs")
        _require(_envelope_ok(request, author_jwks), "read request signature unverified")
        asked = request["payload"]
        _require(asked.get("kind") == "skill.call" and asked.get("skill_name") == "memory.read"
                 and asked.get("caller_peer_id") == writer
                 and asked.get("target_peer_id") == state.get("memory_peer_id")
                 and _same_value(asked.get("args"), args)
                 and type(result.get("call_id")) is str
                 and result["call_id"] == asked.get("call_id") == call.get("call_id"),
                 "fresh read request binding differs")
        body = call.get("result")
        if _signed_absence(call, body, payload):
            observed = None
        else:
            observed = _present_value(call, body, action.record.key, seat, writer,
                                      state, author_jwks)
        artifact = _json({"schema": "context-readback-evidence.v1",
                          "verified_at": datetime.now(timezone.utc).isoformat(),
                          "base_url": state["base_url"], "seat": seat, "writer_peer_id": writer,
                          "batch": action.attempt.batch, "position": action.attempt.position,
                          "attempt_token": action.attempt.token, "proof": proof,
                          "author_jwks": author_jwks})
        _require(len(artifact) <= MAX_EVIDENCE_BYTES, "verification evidence exceeds bound")
    except ReadbackPending:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        raise ReadbackPending("malformed verification evidence") from None
    digest = hashlib.sha256(artifact).hexdigest()
    verdict = outbox.observe(action.attempt, value=observed, evidence_sha256=digest,
                             evidence_artifact=artifact, boundary=boundary)
    return Receipt(verdict, digest)


def _signed_absence(call, body, payload):
    """The server's own shape for a missing key, and nothing else.

    handle_read raises HandlerError("E_UNKNOWN_KEY") and apps/calls/route._dispatch turns it into a failed call whose
    result is {"ok": False, "error_code": code, "error_message": message}, the same code and message as the call's.
    A null result, an extra key or another code is not an absence: it falls through and stays pending.
    """
    message = call.get("error_message")
    return (call.get("status") == "failed" and call.get("error_code") == "E_UNKNOWN_KEY"
            and type(message) is str and type(body) is dict
            and set(body) == {"ok", "error_code", "error_message"}
            and body["ok"] is False and body["error_code"] == "E_UNKNOWN_KEY"
            and body["error_message"] == message
            and all(k in payload for k in wire.RESULT_BOUND_FIELDS))


def _present_value(call, body, key, seat, writer, state, author_jwks):
    _require(call.get("status") == "completed" and call.get("error_code") is None
             and call.get("error_message") is None and type(body) is dict
             and body.get("ok", True) is True, "no successful present read")
    memo = body.get("memo")
    _require(type(memo) is dict, "present memo required")
    _require(memo.get("namespace") == NAMESPACE and memo.get("key") == key
             and memo.get("created_by_peer_id") == writer,
             "memo namespace key or writer differs")
    _require(type(memo.get("version")) is int and memo["version"] == 1
             and memo.get("is_head") is True and memo.get("parent_id") is None
             and all(memo.get(k) is None for k in
                     ("edited_at", "edited_by", "redacted_at", "redaction")),
             "only unedited version-one heads supported")
    value = memo.get("value")
    _require(type(value) is dict and value.get("schema") == "context.v1"
             and value.get("kind") in ("item", "manifest") and value.get("seat") == seat
             and value.get("item") != "redacted", "unsupported context value")
    verified, _ = wire.author_signature_ok(memo, lambda peer: (author_jwks, None)
                                           if peer == writer else (None, "wrong writer"))
    _require(verified, "writer signature unverified")
    provenance = memo["provenance"]["payload"]
    write_args = provenance["args"]
    _require(provenance.get("target_peer_id") == state.get("memory_peer_id")
             and "value" in write_args and _same_value(write_args["value"], value),
             "writer target or value binding differs")
    observed = _json(value)
    _require(0 < len(observed) <= 65536, "read value exceeds bound")
    return observed
