"""The envelope must prove THIS call, by THIS caller — and actually be verified.

Three defects found by adversarial review of apps/memoryskills, each fixed and pinned here.
Every test states what it would have looked like BEFORE the fix, because a test added
after the fact is worthless unless it fails without it.

1. NO SIGNATURE WAS EVER VERIFIED. The write path checked that `signature` and
   `signing_kid` were non-empty, fetched the caller's public key for provenance, and never
   verified anything against it — then wrote provenance asserting the write was verified.
2. THE PAYLOAD WAS NOT BOUND TO THE CALLER. `payload.caller_peer_id` was never compared to
   the authenticated peer.
3. THE PAYLOAD WAS NOT BOUND TO THE SKILL. `payload.skill_name` was never compared to the
   dispatched skill, so an envelope signed for one operation could be replayed into another.

Each negative below is paired with the positive control that the same call SUCCEEDS when
the binding is correct — otherwise "refused" could just mean the fixture is broken.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402

# Reuse the module's own fixtures rather than re-deriving the schema by hand.
from tests.test_memory_skills import (  # noqa: E402
    _claim, _envelope, _ns, _peer, app_conn, owner_conn,  # noqa: F401
)


def _ready_peer(app_conn, owner_conn):
    peer = _peer(owner_conn, f"eb{uuid.uuid4().hex[:6]}")
    ns = _ns("bind")
    _claim(app_conn, peer, ns)
    return peer, ns


def test_a_correctly_bound_write_succeeds(app_conn, owner_conn):
    """POSITIVE CONTROL for all three negatives below."""
    peer, ns = _ready_peer(app_conn, owner_conn)
    out = handlers.dispatch(
        "memory.write", _envelope(peer, "memory.write", {"namespace": ns, "key": "k1",
                                                         "value": {"a": 1}}),
        peer["peer_id"], app_conn)
    assert out["op"] == "memory.write"


def test_a_forged_signature_is_refused(app_conn, owner_conn):
    """Before the fix this WROTE THE ROW and recorded provenance claiming verification."""
    peer, ns = _ready_peer(app_conn, owner_conn)
    env = _envelope(peer, "memory.write", {"namespace": ns, "key": "k2", "value": {"a": 1}})
    env["signature"] = P.b64u(b"\x01" * 64)          # well-formed, wrong
    with pytest.raises(handlers.HandlerError) as exc:
        handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    assert exc.value.code == "E_BAD_ENVELOPE", exc.value.code


def test_a_signature_over_different_bytes_is_refused(app_conn, owner_conn):
    """Sign one payload, then alter it: the classic tamper."""
    peer, ns = _ready_peer(app_conn, owner_conn)
    env = _envelope(peer, "memory.write", {"namespace": ns, "key": "k3", "value": {"a": 1}})
    env["payload"]["args"]["value"] = {"a": 999}      # signed bytes no longer match
    with pytest.raises(handlers.HandlerError) as exc:
        handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    assert exc.value.code == "E_BAD_ENVELOPE", exc.value.code


def test_a_payload_naming_another_caller_is_refused(app_conn, owner_conn):
    """Confused deputy: the payload must describe the AUTHENTICATED caller."""
    peer, ns = _ready_peer(app_conn, owner_conn)
    other = _peer(owner_conn, f"eb-other-{uuid.uuid4().hex[:6]}")
    env = _envelope(peer, "memory.write", {"namespace": ns, "key": "k4", "value": {"a": 1}})
    env["payload"]["caller_peer_id"] = str(other["peer_id"])
    with pytest.raises(handlers.HandlerError) as exc:
        handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    assert exc.value.code == "E_BAD_ENVELOPE", exc.value.code


def test_an_envelope_signed_for_another_skill_is_refused(app_conn, owner_conn):
    """An envelope signed for memory.read must not be replayable into memory.write."""
    peer, ns = _ready_peer(app_conn, owner_conn)
    env = _envelope(peer, "memory.read", {"namespace": ns, "key": "k5"})
    with pytest.raises(handlers.HandlerError) as exc:
        handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    assert exc.value.code == "E_BAD_ENVELOPE", exc.value.code


def test_namespace_claim_refuses_when_no_authority_is_configured(app_conn, owner_conn,
                                                                 monkeypatch):
    """An unset authority must refuse EXPLICITLY, not compare against an empty string.

    The bare comparison read as fail-closed only because a real peer id is never empty -
    a property of the callers, not of the check.
    """
    peer = _peer(owner_conn, f"eb-auth-{uuid.uuid4().hex[:6]}")
    monkeypatch.setattr(handlers, "BOOTSTRAP_AUTHORITY_PEER_ID", "")
    env = _envelope(peer, "namespace.claim", {"namespace": _ns("noauth")})
    with pytest.raises(handlers.HandlerError) as exc:
        handlers.dispatch("namespace.claim", env, peer["peer_id"], app_conn)
    assert exc.value.code == "E_NOT_BOOTSTRAP_AUTHORITY", exc.value.code

    # POSITIVE CONTROL: with this peer configured as the authority, the same call proceeds
    # past the authority gate (it may fail later for unrelated reasons, which is fine -
    # what matters is that it is no longer refused FOR LACKING AUTHORITY).
    monkeypatch.setattr(handlers, "BOOTSTRAP_AUTHORITY_PEER_ID", str(peer["peer_id"]))
    env2 = _envelope(peer, "namespace.claim", {"namespace": _ns("withauth")})
    try:
        handlers.dispatch("namespace.claim", env2, peer["peer_id"], app_conn)
    except handlers.HandlerError as exc2:
        assert exc2.code != "E_NOT_BOOTSTRAP_AUTHORITY", (
            "the configured authority was still refused - the control proves nothing")


# --------------------------------------------------------------------------- #
# Divergence 038 (design B02, Q9): the binding REQUIRES the caller and COMPARES kind
# --------------------------------------------------------------------------- #
#
# `_bind_envelope_to_the_call` compared `caller_peer_id` only when the payload carried one
# and never looked at `kind` at all (docs/design/signed-bytes-status.md G2, on
# fin/design-signed-bytes 9aab90b). The HTTP invoke path never showed it, because
# `verify_payload` refuses both shapes first; the board door and any in-process caller of
# `dispatch` reached the handler with nothing in front. Each payload below is RE-SIGNED
# after the edit, so the signature verifies and the only thing that can refuse it is the
# binding; each refusal is followed by a read showing nothing landed, and each test ends
# with the same write, complete, landing (the positive control).

_ABSENT = object()


def _resigned(peer, env, **fields):
    """`env` with `fields` set in its payload (or removed, for `_ABSENT`), signed again
    with the peer's own key so the signature is valid over the edited bytes."""
    payload = dict(env["payload"])
    for name, value in fields.items():
        if value is _ABSENT:
            payload.pop(name, None)
        else:
            payload[name] = value
    return {**env, "payload": payload,
            "signature": P.b64u(P.sign(peer["seed"], P.jcs(payload)))}


def _rows(owner_conn, ns, key) -> int:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND key = %s",
                    (ns, key))
        return cur.fetchone()[0]


@pytest.mark.parametrize("caller", [_ABSENT, None], ids=["absent", "json-null"])
def test_a_payload_that_names_no_caller_is_refused(app_conn, owner_conn, caller):
    """Before 038 this LANDED: the row's writer was the bearer and its signed bytes named
    nobody (probe P3 of the design)."""
    peer, ns = _ready_peer(app_conn, owner_conn)
    args = {"namespace": ns, "key": "nocaller", "value": {"a": 1}}
    env = _resigned(peer, _envelope(peer, "memory.write", args), caller_peer_id=caller)
    with pytest.raises(handlers.HandlerError) as exc:
        handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    assert exc.value.code == "E_BAD_ENVELOPE", exc.value.code
    assert "caller_peer_id" in exc.value.message, exc.value.message
    assert _rows(owner_conn, ns, "nocaller") == 0

    # POSITIVE CONTROL: the same args, naming the caller, land.
    out = handlers.dispatch("memory.write", _envelope(peer, "memory.write", args),
                            peer["peer_id"], app_conn)
    assert out["op"] == "memory.write" and _rows(owner_conn, ns, "nocaller") == 1


@pytest.mark.parametrize("kind", [_ABSENT, None, "skill.result", "reasoning.trace", "",
                                  "SKILL.CALL", "skill.call "],
                         ids=["absent", "json-null", "skill.result", "reasoning.trace",
                              "empty", "uppercase", "trailing-space"])
def test_a_payload_whose_kind_is_not_skill_call_is_refused(app_conn, owner_conn, kind):
    """`kind` is the domain separator (apps/calls/envelope.py:17-22): what stops bytes
    signed as a result, or as nothing in particular, being spent as a call. Before 038 the
    handler never read it.

    The comparison is exact: the uppercase and trailing-space cases kill a binding that folds
    case or strips (review of
    038, F5: a `.lower()` comparison survived the whole suite)."""
    peer, ns = _ready_peer(app_conn, owner_conn)
    args = {"namespace": ns, "key": "kind", "value": {"a": 1}}
    env = _resigned(peer, _envelope(peer, "memory.write", args), kind=kind)
    with pytest.raises(handlers.HandlerError) as exc:
        handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    assert exc.value.code == "E_BAD_ENVELOPE", exc.value.code
    assert "kind" in exc.value.message, exc.value.message
    assert _rows(owner_conn, ns, "kind") == 0

    out = handlers.dispatch("memory.write", _envelope(peer, "memory.write", args),
                            peer["peer_id"], app_conn)
    assert out["op"] == "memory.write" and _rows(owner_conn, ns, "kind") == 1


def test_the_binding_applies_to_every_skill_not_only_writes(app_conn, owner_conn):
    """`dispatch` is the one entry for all seven skills; the binding sits in front of each.
    memory.read with no caller and no kind is refused; the complete read answers."""
    peer, ns = _ready_peer(app_conn, owner_conn)
    handlers.dispatch("memory.write", _envelope(peer, "memory.write", {
        "namespace": ns, "key": "r", "value": {"a": 1}}), peer["peer_id"], app_conn)
    read = _envelope(peer, "memory.read", {"namespace": ns, "key": "r"})
    for bad in (_resigned(peer, read, caller_peer_id=_ABSENT), _resigned(peer, read, kind=_ABSENT)):
        with pytest.raises(handlers.HandlerError) as exc:
            handlers.dispatch("memory.read", bad, peer["peer_id"], app_conn)
        assert exc.value.code == "E_BAD_ENVELOPE", exc.value.code
    out = handlers.dispatch("memory.read", read, peer["peer_id"], app_conn)
    assert out["memo"]["value"] == {"a": 1}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
