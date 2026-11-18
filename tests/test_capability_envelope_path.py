"""Tokens on the envelope path (B07 slice 6, docs/DIVERGENCES.md 040; design
docs/design/biscuit-capabilities.md 3.1, 3.2 "Where each refusal surfaces", 3.3, 4.2, 4.3, 4.4).

`Meshychat-Capability` on `POST /v1/agents/{memory}/skills/{name}/invoke` and on A2A `message:send`
reaches the memory handlers (sites 2 to 5). Every test drives the real route through the
project's urlconf and middleware, with the replay ledger at its shipped default (ON).

  * T1, the #913 shape: a writer with namespace write, narrowed by a token to one range.
  * T6 over HTTP: a refused token never falls through to the caller's own grant; the same
    write without the header lands.
  * The view's pre-check (3.2 steps 1 and 3): a token that does not parse, or a caller that
    is not its holder, is HTTP 403 with no skill_calls row and no nonce checked or burned: the
    same envelope bytes, sent again once the refusal's cause is gone, complete.
  * capability_sha256, the signed binding of the header: a mismatch in either direction is
    refused (header without the field, the field without the header, a wrong hash).
  * The skills and shapes 4.3 says never take a token refuse one (E_CAPABILITY_NOT_ACCEPTED):
    memory.grant, memory.prune, namespace.claim, a pre_discipline write, memory.list with no
    namespace. So does a call to any peer but the memory authority, before the call starts.
  * T4: 4.4's delegation chain allows, and its four variants are refused.
  * T9: range semantics against the real LIKE.
  * T17: a token does not delegate authorship (divergence 028).
  * Q6: `authz` in the result and `capability` in write provenance only when a token was used;
    a call without one answers in exactly today's shape.
  * The board's in-process path passes no principal, so it refuses a payload that names one.

Every refusal sits beside its control in the same test.

Run:  pytest tests/test_capability_envelope_path.py -q
"""
from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

from tests.capability_invoke import (  # noqa: E402
    OMIT,
    invoke_context,
    outcome,
    seed_of,
    sha256_hex,
)
from tests.capability_routes import admin_on  # noqa: E402

COMPLETED = (200, "completed", None)
MESH = "shared/mesh-chat"


def failed(code: str) -> tuple:
    return 200, "failed", code


def refused(code: str) -> tuple:
    return 403, code, None


@pytest.fixture()
def inv(monkeypatch):
    # These tests decide paths, not run limits. The verifier's 100 ms wall-clock budget
    # (capability.LIMIT_TIME) was overrun on a loaded gate box, so a valid T1 token came back
    # 403 E_CAPABILITY_DENIED "exceeded the verifier's limits" (mg446c, issue #451). The facts
    # and iterations limits stay; test_capability_verifier pins all three production values.
    from apps.memoryskills import capability
    monkeypatch.setattr(capability, "LIMIT_TIME", capability.LIMIT_TIME * 50)
    with invoke_context() as i:
        yield i


def write(ns: str, key: str, **value) -> dict:
    return {"namespace": ns, "key": key, "value": value or {"n": 1}}


def keys_of(inv, ns: str, key: str) -> list[int]:
    return [r["version"] for r in inv.memory_rows(ns, key)]


# ============================================================================ T1 and T6

def test_t1_a_token_narrows_a_writer_with_namespace_write_to_its_key_range(inv):
    """The #913 shape (design 6.1 T1). E holds write on the whole namespace by grant, and
    presents a token its issuer scoped to `task/42/`: the write inside the range lands, the one
    outside it is refused with a signed failure and does not land. Red before slice 6: the
    header was refused on invoke (E_CAPABILITY_NOT_ACCEPTED), so neither write ran."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "write")
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("task/42/", "write"),), depth_cap=0)

    inside, _ = inv.call(e, "memory.write", write(ns, "task/42/a"), token=tok)
    assert outcome(inside) == COMPLETED, inside
    assert keys_of(inv, ns, "task/42/a") == [1]

    outside, _ = inv.call(e, "memory.write", write(ns, "task/7/a"), token=tok)
    assert outcome(outside) == failed("E_CAPABILITY_DENIED"), outside
    assert keys_of(inv, ns, "task/7/a") == []


def test_t6_a_refused_token_never_falls_through_to_the_callers_own_grant(inv):
    """Design 4.2: the token is the whole authority for the request. E holds namespace-wide
    write by grant and presents a READ-only token: the write is refused, not served by the
    grant. Control: the same write without the header lands, by the grant."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "write")
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "read"),), depth_cap=0)

    with_token, _ = inv.call(e, "memory.write", write(ns, "t6"), token=tok)
    assert outcome(with_token) == failed("E_CAPABILITY_DENIED"), with_token
    assert keys_of(inv, ns, "t6") == []

    without, _ = inv.call(e, "memory.write", write(ns, "t6"))
    assert outcome(without) == COMPLETED, without
    assert keys_of(inv, ns, "t6") == [1]


# ============================================================================ the view's pre-check

def test_a_token_that_does_not_parse_is_403_before_the_call_starts(inv):
    """3.2 steps 1 (size and parse): refused by the view, HTTP 403 E_CAPABILITY_INVALID, and
    the call never starts: no skill_calls row, no nonce recorded. The three: a valid token with
    one character changed, text that is no token, and 9000 bytes (over the 8 KiB cap). Control:
    the caller's valid token on the same write completes."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0).to_base64()
    middle = len(tok) // 2
    tampered = tok[:middle] + ("A" if tok[middle] != "A" else "B") + tok[middle + 1:]

    for bad in (tampered, "not-a-biscuit", "x" * 9000):
        seen, env = inv.call(e, "memory.write", write(ns, "pre"), token=bad)
        assert outcome(seen) == refused("E_CAPABILITY_INVALID"), (bad[:12], seen)
        assert inv.call_row(env["payload"]["call_id"]) is None, "the call must never start"
        assert inv.nonce_rows(env) == [], "no nonce may be checked or burned"
    assert keys_of(inv, ns, "pre") == []

    ok, _ = inv.call(e, "memory.write", write(ns, "pre"), token=tok)
    assert outcome(ok) == COMPLETED, ok


def test_a_holder_refusal_at_the_view_writes_no_row_and_burns_no_nonce(inv):
    """3.2 step 3 (the holder walk) in the view: A delegated its token to E with a key that is
    not live now, so the walk refuses (E_CAPABILITY_HOLDER, HTTP 403) before start_call. The
    pin that no nonce was burned and no row written: once A's key is live again, the SAME
    envelope bytes with the SAME token complete. A burned nonce would answer the replay's 403;
    a written row would answer the first call's result."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    tok = inv.w.delegate(inv.w.mint(issuer=a, subject=a, ns=ns, rights=(("", "write"),)), a, e)
    inv.w.sql("UPDATE peer_keys SET active = FALSE, revoked_at = now() - interval '1 hour' "
              "WHERE peer_id = %s", (a.id,))
    env = inv.envelope(e, "memory.write", write(ns, "holder"), token=tok)

    first = inv.invoke(e, "memory.write", env, token=tok)
    assert outcome(first) == refused("E_CAPABILITY_HOLDER"), first
    assert inv.call_row(env["payload"]["call_id"]) is None
    assert inv.nonce_rows(env) == []

    inv.w.sql("UPDATE peer_keys SET active = TRUE, revoked_at = NULL WHERE peer_id = %s",
              (a.id,))
    again = inv.invoke(e, "memory.write", env, token=tok)
    assert outcome(again) == COMPLETED, again
    assert keys_of(inv, ns, "holder") == [1]
    assert len(inv.nonce_rows(env)) == 1, "the call that ran recorded its nonce"


def test_the_caller_must_be_the_holder_at_the_view(inv):
    """A's token, delegated to E, presented by A (its subject, no longer the holder): 403
    E_CAPABILITY_HOLDER from the view. Control: E presents it and the write lands."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    tok = inv.w.delegate(inv.w.mint(issuer=a, subject=a, ns=ns, rights=(("", "write"),)), a, e)

    by_a, env = inv.call(a, "memory.write", write(ns, "h2"), token=tok)
    assert outcome(by_a) == refused("E_CAPABILITY_HOLDER"), by_a
    assert inv.call_row(env["payload"]["call_id"]) is None
    by_e, _ = inv.call(e, "memory.write", write(ns, "h2"), token=tok)
    assert outcome(by_e) == COMPLETED, by_e


def test_a_token_on_a_call_to_any_other_peer_is_refused_before_the_call(inv):
    """A token is decided only by the memory authority's in-process handlers (3.3). A call to
    any other target carrying one is refused by the view, 403 E_CAPABILITY_NOT_ACCEPTED, with
    no row: the token is never kept for a delivery. Control: the same call without the header
    starts and is queued for its target at step 9, as every call to an offline peer is since
    divergence 041 S5 (integ/r11: it failed E_TARGET_UNREACHABLE on the branch's base)."""
    a, e, other = inv.peer("A"), inv.peer("E"), inv.peer("other")
    ns = inv.w.claim(a)
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0)
    inv.w.sql("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
              "is_current) VALUES (%s, 'memory.write', '0.2.0', 'sync', 'grants_only', '{}', "
              "TRUE)", (other.id,))

    seen, env = inv.call(e, "memory.write", write(ns, "remote"), token=tok, target=other.id)
    assert outcome(seen) == refused("E_CAPABILITY_NOT_ACCEPTED"), seen
    assert inv.call_row(env["payload"]["call_id"]) is None

    control, env = inv.call(e, "memory.write", write(ns, "remote"), target=other.id)
    assert control.status == 202, control
    assert inv.call_row(env["payload"]["call_id"])["status"] == "queued"


def test_step_9_fails_a_token_bearing_call_to_another_peer_unexecuted(inv):
    """Defence behind the view (apps/calls/route.py): a StartCallInput carrying an object
    capability to any target but the memory authority, as a transport that built one without
    the view's check would, fails at step 9, E_CAPABILITY_NOT_ACCEPTED, unexecuted: no path
    delivers a call with its token or with the token dropped.

    THE GUARD RUNS AHEAD OF 041's REMOTE ROUTING (operator decision at integ/r11). Since 041 S5
    step 9 routes a call to another peer `routed` (its socket is online) or `queued` (it is
    not), and a queued call inserts a doorbell attempt when the target has a verified webhook
    (041 S9). The token-bearing call is failed before `_route_remote`: no `routed` or `queued`
    status or event, and no doorbell attempt, whichever state the target is in. Control, one
    field apart (no principal) and in the same target state: the call is queued with its
    doorbell attempt, then, with the target online, routed, so each absence above is one the
    routing would otherwise have produced."""
    import uuid

    from apps.calls import pipeline
    from apps.calls import route as routing
    from apps.calls.envelope import SignedEnvelope
    from apps.memoryskills.capability import PresentedCapability
    from apps.wsgw import hub_presence

    a, e, other = inv.peer("A"), inv.peer("E"), inv.peer("other")
    ns = inv.w.claim(a)
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0)
    inv.w.sql("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
              "is_current) VALUES (%s, 'memory.write', '0.2.0', 'sync', 'grants_only', '{}', "
              "TRUE)", (other.id,))
    # A verified, enabled doorbell registration: a queued edge to `other` rings it.
    inv.w.sql("INSERT INTO peer_invocation_webhooks (peer_id, url, challenge, verified_at) "
              "VALUES (%s, 'https://hook.example.test/r11', %s, now())", (other.id, "R" * 43))

    def start(principal, key: str) -> dict:
        env = inv.envelope(e, "memory.write", write(ns, key),
                           token=tok if principal else None, target=other.id)
        inp = pipeline.StartCallInput(
            caller_peer_id=uuid.UUID(e.id), target_peer_id=uuid.UUID(other.id),
            skill_name="memory.write", envelope=SignedEnvelope(**env),
            call_id=env["payload"]["call_id"], object_capability=principal)
        ctx = pipeline.CallsContext(conn=inv.w.app, envelope_nonce_enforce=True,
                                    memory_peer_id=routing.memory_peer_id_from_settings())
        return inv.call_row(pipeline.start_call(ctx, inp).call_id)

    def events(row) -> list[str]:
        return [r["event_type"] for r in inv._rows(
            "SELECT event_type FROM skill_call_events WHERE call_id = %s ORDER BY id",
            (row["call_id"],))]

    def attempts(row) -> int:
        return len(inv._rows("SELECT id FROM peer_webhook_attempts WHERE call_id = %s",
                             (row["call_id"],)))

    def principal():
        return PresentedCapability(tok.to_base64(), "memory.write")

    # The target offline, its doorbell verified.
    row = start(principal(), "step9/offline")
    assert (row["status"], row["error_code"], row["result"]) == (
        "failed", "E_CAPABILITY_NOT_ACCEPTED", None), row
    assert not {"routed", "queued"} & set(events(row)), events(row)
    assert attempts(row) == 0, "a token-bearing call rang the target's doorbell"
    control = start(None, "step9/offline")
    assert control["status"] == "queued", control
    assert "queued" in events(control) and attempts(control) == 1, "control: it rings"

    # The target online: a socket of its own that beat just now.
    conn_id, worker = uuid.uuid4(), uuid.uuid4()
    assert hub_presence.insert(inv.w.app, conn_id, other.id, worker)
    inv.w.app.commit()
    try:
        assert hub_presence.is_online(inv.w.app, other.id), "control: the target is online"
        row = start(principal(), "step9/online")
        assert (row["status"], row["error_code"], row["result"]) == (
            "failed", "E_CAPABILITY_NOT_ACCEPTED", None), row
        assert not {"routed", "queued"} & set(events(row)), events(row)
        control = start(None, "step9/online")
        assert control["status"] == "routed", control
        assert "routed" in events(control)
    finally:
        hub_presence.delete(inv.w.app, conn_id)
        inv.w.app.commit()
    assert keys_of(inv, ns, "step9/offline") == [] and keys_of(inv, ns, "step9/online") == []


def test_a_cross_tenant_caller_with_a_skill_grant_is_decided_by_its_object_token(inv):
    """Step 5 admits a caller outside the memory authority's trust group only by a memory.read
    skill grant, and gate 1 (`capability_token`) is consulted only then (gate 0
    returns first for everyone in the group, as every other test's callers are). The object
    token rides its own field, so gate 1 is never handed it: the read completes by the token,
    and the same caller without the header is refused by the namespace (it holds no grant)."""
    a, x = inv.peer("A"), inv.w.peer("X")                  # X: a trust group of its own
    ns = inv.w.claim(a)
    inv.w.sql("INSERT INTO skill_grants (grantor_owner_id, grantee_owner_id, granter_peer_id, "
              "skill_name, source) SELECT owner_id, %s, id, 'memory.read', 'direct' "
              "FROM peers WHERE id = %s", (x.owner_id, inv.memory_peer))
    seeded, _ = inv.call(a, "memory.write", write(ns, "x/1", text="cross"))
    assert outcome(seeded) == COMPLETED, seeded
    tok = inv.w.mint(issuer=a, subject=x, ns=ns, rights=(("x/", "read"),), depth_cap=0)

    read, _ = inv.call(x, "memory.read", {"namespace": ns, "key": "x/1"}, token=tok)
    assert outcome(read) == COMPLETED, read
    assert read.body["result"]["memo"]["value"] == {"text": "cross"}
    without, _ = inv.call(x, "memory.read", {"namespace": ns, "key": "x/1"})
    assert outcome(without) == failed("E_PERMISSION_DENIED"), without


# ============================================================================ capability_sha256

@pytest.mark.parametrize("case", ["the header without the field", "the field without the header",
                                  "the hash of another token", "the hash in upper case",
                                  "a null field with the header"])
def test_a_capability_sha256_mismatch_in_either_direction_is_refused(inv, case):
    """Design 3.1: the SIGNED payload carries `capability_sha256`, the sha256 of the exact header
    string, and a mismatch in either direction is refused, E_BAD_ENVELOPE, a signed failure that
    writes nothing. E also holds write by grant, so each case would land if the binding were
    not compared. Control: the matching hash with the header lands."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "write")
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0).to_base64()
    other = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),),
                       depth_cap=1).to_base64()
    assert other != tok

    control, _ = inv.call(e, "memory.write", write(ns, "bound"), token=tok)
    assert outcome(control) == COMPLETED, control

    header, field = {
        "the header without the field": (tok, OMIT),
        "the field without the header": (None, sha256_hex(tok)),
        "the hash of another token": (tok, sha256_hex(other)),
        "the hash in upper case": (tok, sha256_hex(tok).upper()),
        "a null field with the header": (tok, None),
    }[case]
    env = inv.envelope(e, "memory.write", write(ns, "unbound"), token=tok,
                       capability_sha256=field)
    seen = inv.invoke(e, "memory.write", env, token=header)
    assert outcome(seen) == failed("E_BAD_ENVELOPE"), seen
    assert keys_of(inv, ns, "unbound") == []
    message = seen.body["error_message"]
    assert "capability" in message and tok[:16] not in message, message


@pytest.mark.parametrize("skill", ["memory.read", "memory.query", "memory.list"])
def test_the_capability_sha256_binding_holds_on_the_read_skills_too(inv, skill):
    """The binding is the dispatcher's, for every skill that takes a token, and above it was
    exercised on memory.write alone: a binding compared only for writes, or one that let a
    null field pass beside a header, survived every test (the slice 6 review, finding 2). On
    sites 3, 4 and 5 either regression lets a stripped header's field, or a header whose field
    was dropped, be decided by the caller's own grants: the silent fall-through that design
    3.1 and critique C8 rule out. E holds read by grant, so each case would complete if the
    binding were skipped; each is E_BAD_ENVELOPE, a signed failure. Control: the bound call
    completes, decided by the token (its result says so)."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "read")
    seeded, _ = inv.call(a, "memory.write", write(ns, "b/1"))
    assert outcome(seeded) == COMPLETED, seeded
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("b/", "read"),), depth_cap=0).to_base64()
    other = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("b/", "read"),),
                       depth_cap=1).to_base64()
    assert other != tok
    args = {"memory.read": {"namespace": ns, "key": "b/1"},
            "memory.query": {"namespace": ns, "key_prefix": "b/"},
            "memory.list": {"namespace": ns, "prefix": "b/"}}[skill]

    control, _ = inv.call(e, skill, args, token=tok)
    assert outcome(control) == COMPLETED, control
    assert control.body["result"]["authz"]["via"] == "capability", control.body["result"]

    for case, header, field in (("the header without the field", tok, OMIT),
                                ("the field without the header", None, sha256_hex(tok)),
                                ("a null field with the header", tok, None),
                                ("the hash of another token", tok, sha256_hex(other))):
        env = inv.envelope(e, skill, args, token=tok, capability_sha256=field)
        seen = inv.invoke(e, skill, env, token=header)
        assert outcome(seen) == failed("E_BAD_ENVELOPE"), (skill, case, seen)
        message = seen.body["error_message"]
        assert "capability" in message and tok[:16] not in message, (skill, case, message)


def test_the_boards_in_process_path_refuses_a_payload_that_names_a_capability(inv):
    """The board door (apps/board/declare.py) dispatches memory.write with no principal, so a
    payload naming a capability is refused (E_BAD_ENVELOPE) and nothing lands (design 3.1).
    Control: the same act, its payload without the field, lands."""
    from apps.board import declare as D
    from apps.crypto import primitives as P
    from apps.memoryskills.errors import HandlerError

    x = inv.peer("X")
    admin_on(inv.w, x, MESH)
    label = f"peer-{x.id[:8]}"
    ident = D.Identity(peer_id=x.id, kid=x.kid, seed=seed_of(x), label=label)

    def act(named: bool) -> tuple[dict, str]:
        key = D.mint_key(label)
        args = D.write_args(key=key, value=D.act_value(subject="pr/1", verb="reviewed",
                                                       ref="0" * 40, actor_label=label),
                            actor_label=label)
        env = D.signed_envelope(ident, args, target_peer_id=inv.memory_peer)
        if named:
            env["payload"]["capability_sha256"] = sha256_hex("any token at all")
            env["signature"] = P.b64u(P.sign(seed_of(x), P.jcs(env["payload"])))
        return env, key

    env, key = act(True)
    with pytest.raises(HandlerError) as exc:
        D.declare(inv.w.app, env, x.id)
    assert exc.value.code == "E_BAD_ENVELOPE", exc.value
    assert keys_of(inv, MESH, key) == []

    env, key = act(False)
    assert D.declare(inv.w.app, env, x.id)["replay"] is False
    assert keys_of(inv, MESH, key) == [1]


# ============================================================================ where 4.3 says no

def test_skills_that_never_take_a_token_refuse_one(inv):
    """Design 4.3 and 2.1: memory.grant (a token must never mint durable authority),
    namespace.claim (bootstrap authority), a pre_discipline write (a custody write is
    administrative) and memory.list with no namespace (it enumerates relationships, not
    objects) each refuse a token, E_CAPABILITY_NOT_ACCEPTED, a signed failure. A is the
    namespace's owner and its own token's holder, so the pre-check passes and the refusal is
    the handler's. Controls: each call without the header gets the answer it gets today, which
    is not that code. memory.prune is not a skill the memory authority publishes (its manifest,
    apps/calls/bootstrap.py), so invoke answers 404 before any handler;
    its refusal is pinned at dispatch, below."""
    a, g = inv.peer("A"), inv.peer("G")
    ns = inv.w.claim(a)
    tok = inv.w.mint(issuer=a, subject=a, ns=ns, rights=(("", "write"), ("", "read")))
    calls = {
        "memory.grant": ({"namespace": ns, "grantee_peer_id": g.id, "permission": "read"},
                         "completed", None),
        "namespace.claim": ({"namespace": f"{ns}-claim"}, "failed",
                            "E_NOT_BOOTSTRAP_AUTHORITY"),
        "memory.write": ({"namespace": ns, "key": "custody", "value": {"n": 1},
                          "pre_discipline": True}, "completed", None),
        "memory.list": ({}, "completed", None),
    }
    for skill, (args, status, code) in calls.items():
        with_token, _ = inv.call(a, skill, args, token=tok)
        assert outcome(with_token) == failed("E_CAPABILITY_NOT_ACCEPTED"), (skill, with_token)
        without, _ = inv.call(a, skill, args)
        assert outcome(without) == (200, status, code), (skill, without)
    assert keys_of(inv, ns, "custody") == [1], "only the call without the header wrote"
    assert inv.w.sql("SELECT 1 FROM shared_memory_grant WHERE namespace = %s AND "
                     "grantee_peer_id = %s", (ns, g.id)) == 1, "only one grant was written"


def test_memory_prune_refuses_a_principal_at_dispatch(inv):
    """memory.prune is destructive and bulk (design 4.3): with a principal it is
    E_CAPABILITY_NOT_ACCEPTED before it reads anything. Control: the same call without one gets
    today's answer for a keep_all namespace, E_BAD_REQUEST."""
    from apps.memoryskills import capability, handlers
    from apps.memoryskills.errors import HandlerError

    a = inv.peer("A")
    ns = inv.w.claim(a)
    token = inv.w.mint(issuer=a, subject=a, ns=ns, rights=(("", "write"),)).to_base64()
    principal = capability.PresentedCapability(token, "memory.prune")

    def prune(principal, **extra):
        env = {"payload": {"kind": "skill.call", "caller_peer_id": a.id,
                           "skill_name": "memory.prune", "args": {"namespace": ns}, **extra}}
        with pytest.raises(HandlerError) as exc:
            handlers.dispatch("memory.prune", env, a.id, inv.w.app, principal=principal)
        return exc.value

    refused_ = prune(principal, capability_sha256=principal.sha256)
    assert refused_.code == "E_CAPABILITY_NOT_ACCEPTED", refused_
    assert token[:16] not in refused_.message
    assert prune(None).code == "E_BAD_REQUEST"


# ============================================================================ T4: 4.4's chain

def test_t4_the_worked_delegation_chain_allows_and_its_four_variants_are_refused(inv):
    """Design 4.4. A mints read and write on `task/42/` (depth 2) and delegates to E; E gives F
    a read-only slice under `task/42/review/`. F reads `task/42/review/notes` over the envelope
    path: ALLOW, and the result says how (Q6). The four variants: F writes (the block's
    `check if action("read")`), F reads `task/42/other` (the prefix check), E presents F's
    token (the walk: the holder is F), and F after its hop expired."""
    a, e, f = inv.peer("A"), inv.peer("E"), inv.peer("F")
    ns = inv.w.claim(a)
    for key in ("task/42/review/notes", "task/42/other"):
        seeded, _ = inv.call(a, "memory.write", write(ns, key, text=key))
        assert outcome(seeded) == COMPLETED, seeded
    root = inv.w.mint(issuer=a, subject=a, ns=ns,
                      rights=(("task/42/", "read"), ("task/42/", "write")), depth_cap=2)
    narrow = ('check if action("read");\n'
              'check if resource($ns, $r), $r.starts_with("task/42/review/");\n')
    to_e = inv.w.delegate(root, a, e, ttl=240)
    tok = inv.w.delegate(to_e, e, f, ttl=180, checks=narrow)

    read, _ = inv.call(f, "memory.read", {"namespace": ns, "key": "task/42/review/notes"},
                       token=tok)
    assert outcome(read) == COMPLETED, read
    result = read.body["result"]
    assert result["memo"]["value"] == {"text": "task/42/review/notes"}
    assert result["authz"] == {"via": "capability", "cap_id": tok.revocation_ids[0],
                               "issuer": a.id, "holder_chain": [a.id, e.id, f.id], "hops": 2}

    as_write, _ = inv.call(f, "memory.write", write(ns, "task/42/review/x"), token=tok)
    assert outcome(as_write) == failed("E_CAPABILITY_DENIED"), as_write
    assert keys_of(inv, ns, "task/42/review/x") == []
    other_key, _ = inv.call(f, "memory.read", {"namespace": ns, "key": "task/42/other"},
                            token=tok)
    assert outcome(other_key) == failed("E_CAPABILITY_DENIED"), other_key
    by_e, _ = inv.call(e, "memory.read", {"namespace": ns, "key": "task/42/review/notes"},
                       token=tok)
    assert outcome(by_e) == refused("E_CAPABILITY_HOLDER"), by_e
    expired = inv.w.delegate(to_e, e, f, ttl=-5, checks=narrow)
    late, _ = inv.call(f, "memory.read", {"namespace": ns, "key": "task/42/review/notes"},
                       token=expired)
    assert outcome(late) == refused("E_CAPABILITY_DENIED"), late


# ============================================================================ T9: range semantics

def test_t9_a_range_is_allowed_only_inside_the_granted_prefix_against_the_real_like(inv):
    """Design 4.1 and inference 5: a range request is allowed iff its prefix lies inside a
    granted prefix, and the handler's LIKE (with % and _ escaped) returns exactly the keys that
    start with it. The granted prefix holds both metacharacters, and a sibling key would match
    an UNESCAPED `t9/a_b%c/%`. Equal and inside are allowed, with exactly the keys that start
    with the prefix; wider, sibling and the whole namespace are refused. memory.list the same.
    Control: a reader by grant, without a token, gets the same rows for the same prefix."""
    a, r, g = inv.peer("A"), inv.peer("R"), inv.peer("G")
    ns = inv.w.claim(a)
    inv.w.grant(ns, g, a, "read")
    for key in ("t9/a_b%c/1", "t9/a_b%c/2", "t9/aXbYc/1", "t9/other"):
        seeded, _ = inv.call(a, "memory.write", write(ns, key))
        assert outcome(seeded) == COMPLETED, seeded
    tok = inv.w.mint(issuer=a, subject=r, ns=ns, rights=(("t9/a_b%c/", "read"),), depth_cap=0)

    def query(caller, prefix, *, token=tok, skill="memory.query"):
        args = {"namespace": ns}
        if prefix is not None:
            args["key_prefix" if skill == "memory.query" else "prefix"] = prefix
        seen, _ = inv.call(caller, skill, args, token=token)
        rows = (seen.body.get("result") or {}).get(
            "results" if skill == "memory.query" else "items") if seen.status == 200 else None
        return outcome(seen), sorted(x["key"] for x in rows or [])

    for skill in ("memory.query", "memory.list"):
        assert query(r, "t9/a_b%c/", skill=skill) == (COMPLETED, ["t9/a_b%c/1", "t9/a_b%c/2"])
        assert query(r, "t9/a_b%c/1", skill=skill) == (COMPLETED, ["t9/a_b%c/1"])
        for outside in ("t9/a", "t9/aXbYc/", None):
            assert query(r, outside, skill=skill) == (failed("E_CAPABILITY_DENIED"), []), (
                skill, outside)
        assert query(g, "t9/a_b%c/", token=None, skill=skill) == (
            COMPLETED, ["t9/a_b%c/1", "t9/a_b%c/2"])


@pytest.mark.parametrize("skill", ["memory.query", "memory.list"])
def test_t9_each_like_escape_alone_keeps_a_token_inside_its_prefix(inv, skill):
    """T9 above puts both metacharacters in ONE granted prefix, and its sibling key matches
    only when BOTH escapes are gone, so a handler that stopped escaping just one of them
    passed it (the slice 6 review, finding 1: with memory.query no longer escaping `_`, a
    token for `u/a_b/` returned `u/aXb/secret`). Here each granted prefix holds ONE
    metacharacter, beside a sibling key that the pattern with that one unescaped would match:
    `u/a_b/` beside `u/aXb/`, `p/a%b/` beside `p/aZZb/`. The token's holder gets exactly the
    key inside its prefix, as does a reader by grant asking for the same prefix. Positive
    control: that reader sees both siblings under the common prefix, so each absence below is
    the escape's doing, not a missing row."""
    a, r, g = inv.peer("A"), inv.peer("R"), inv.peer("G")
    ns = inv.w.claim(a)
    inv.w.grant(ns, g, a, "read")
    for key in ("u/a_b/1", "u/aXb/secret", "p/a%b/1", "p/aZZb/secret"):
        seeded, _ = inv.call(a, "memory.write", write(ns, key))
        assert outcome(seeded) == COMPLETED, seeded
    field, rows = ("key_prefix", "results") if skill == "memory.query" else ("prefix", "items")

    def keys(caller, prefix, token=None) -> list[str]:
        seen, _ = inv.call(caller, skill, {"namespace": ns, field: prefix}, token=token)
        assert outcome(seen) == COMPLETED, (skill, prefix, seen)
        return sorted(x["key"] for x in seen.body["result"][rows])

    assert keys(g, "u/a") == sorted(["u/a_b/1", "u/aXb/secret"])
    assert keys(g, "p/a") == sorted(["p/a%b/1", "p/aZZb/secret"])
    for granted, inside in (("u/a_b/", "u/a_b/1"), ("p/a%b/", "p/a%b/1")):
        tok = inv.w.mint(issuer=a, subject=r, ns=ns, rights=((granted, "read"),), depth_cap=0)
        assert keys(r, granted, tok) == [inside], (skill, granted)
        assert keys(g, granted) == [inside], (skill, granted)


# ============================================================================ T17: authorship

def _mesh_key(label: str) -> str:
    return f"msg/{int(time.time() * 1000) + secrets.randbelow(1000)}-{label}-{secrets.token_hex(3)}"


def _mesh_value(sender: str, body: str) -> dict:
    return {"schema_version": 1, "to": ["all"], "from": sender, "type": "status",
            "thread_id": "cap6/t17", "ts": "2026-09-25T00:00:00Z", "body": body,
            "author_family": "claude"}


def test_t17_a_token_does_not_delegate_authorship(inv, monkeypatch):
    """Design 4.2 (critique C4): a token lets E write AS ITSELF. E holds A's write token on
    shared/mesh-chat (delegated by A). A and E share a LABEL (the roster cache), as test_mesh_authorship
    isolates R4, so only authorship separates them. E writing version 2 of A's message:
    E_NOT_MESSAGE_AUTHOR; E sending as A (value.from naming A's own label): E_FROM_MISMATCH;
    E's own new message under the same token lands (the control). A's message stays A's."""
    issuer, a, e = inv.peer("I"), inv.peer("A"), inv.peer("E")
    admin_on(inv.w, issuer, MESH)
    shared = f"t17-{secrets.token_hex(4)}"
    # Since divergence 066 a `peers.name` two peers hold is nobody's label, and the roster file refuses one label for two peers, so the label cache is the only place left to make the state: what a roster reload that moves a label to another peer leaves behind.
    from apps.memoryskills import handlers
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(a.id): shared, str(e.id): shared})
    root = inv.w.mint(issuer=issuer, subject=a, ns=MESH, rights=(("msg/", "write"),),
                      depth_cap=1)
    key = _mesh_key(shared)
    first, _ = inv.call(a, "memory.write",
                        {"namespace": MESH, "key": key,
                         "value": _mesh_value(shared, "A's synthetic message")}, token=root)
    assert outcome(first) == COMPLETED, first
    tok = inv.w.delegate(root, a, e)

    tamper, _ = inv.call(e, "memory.write",
                         {"namespace": MESH, "key": key,
                          "value": _mesh_value(shared, "E's TAMPERED synthetic text")},
                         token=tok)
    assert outcome(tamper) == failed("E_NOT_MESSAGE_AUTHOR"), tamper
    a_label = f"peer-{a.id[:8]}"
    as_a, _ = inv.call(e, "memory.write",
                       {"namespace": MESH, "key": _mesh_key(a_label),
                        "value": _mesh_value(a_label, "E sending as A")}, token=tok)
    assert outcome(as_a) == failed("E_FROM_MISMATCH"), as_a
    own = _mesh_key(f"peer-{e.id[:8]}")
    mine, _ = inv.call(e, "memory.write",
                       {"namespace": MESH, "key": own,
                        "value": _mesh_value(f"peer-{e.id[:8]}", "E's own synthetic message")},
                       token=tok)
    assert outcome(mine) == COMPLETED, mine
    assert [(r["version"], r["writer"]) for r in inv.memory_rows(MESH, own)] == [(1, e.id)]
    assert [(r["version"], r["writer"]) for r in inv.memory_rows(MESH, key)] == [(1, a.id)]


# ============================================================================ Q6: how it was authorised

#: What each result and each row carries today, pinned so a call WITHOUT a token keeps
#: answering in exactly today's shape (Q6: byte-identical). The terminal body is built in order
#: by the view; a result and a provenance are stored as jsonb, which keeps its keys in its own
#: order, so those two are compared as sets.
TERMINAL_KEYS = ["call_id", "skill_version", "mode", "status", "result", "signature",
                 "signing_kid", "completed_at"]
RESULT_KEYS = {
    # author_label and tags: divergence 039's (G4, Q5), main's, not on this branch's base
    # (integ/r11).
    "memory.write": ["op", "namespace", "key", "version", "is_head", "id", "status",
                     "pre_discipline", "claimed_status", "author_label", "tags", "created_at",
                     "replay"],
    "memory.read": ["op", "memo"],
    "memory.query": ["op", "namespace", "count", "results", "next_cursor"],
    "memory.list": ["op", "namespace", "count", "items", "next_cursor"],
}
PROVENANCE_KEYS = ["payload", "signature", "signing_kid", "caller_owner_id", "payload_jcs_sha256",
                   "public_key_b64url", "public_key_sha256", "verified_at", "pre_discipline"]


def _four(inv, caller, ns: str, key: str, token=None) -> dict:
    out = {}
    for skill, args in (("memory.write", write(ns, key)),
                        ("memory.read", {"namespace": ns, "key": key}),
                        ("memory.query", {"namespace": ns, "key_prefix": key}),
                        ("memory.list", {"namespace": ns, "prefix": key})):
        seen, _ = inv.call(caller, skill, args, token=token)
        assert outcome(seen) == COMPLETED, (skill, seen)
        out[skill] = seen.body
    return out


def test_q6_a_call_without_a_token_answers_in_exactly_todays_shape(inv):
    """No `authz`, no provenance `capability`, and the same keys in the same order as before
    slice 6, on each of the four skills that take a token and on the row a write stores."""
    a, g = inv.peer("A"), inv.peer("G")
    ns = inv.w.claim(a)
    inv.w.grant(ns, g, a, "write")
    for skill, body in _four(inv, g, ns, "q6/plain").items():
        assert list(body) == TERMINAL_KEYS, (skill, list(body))
        assert set(body["result"]) == set(RESULT_KEYS[skill]), (skill, list(body["result"]))
    (row,) = inv.memory_rows(ns, "q6/plain")
    assert set(row["provenance_jsonb"]) == set(PROVENANCE_KEYS), list(row["provenance_jsonb"])


def test_q6_authz_and_the_provenance_capability_are_present_when_a_token_was_used(inv):
    """The same four calls with a token: each result carries `authz`, the decision record
    (design 3.2 step 6), beside today's keys, and the row a write stores carries `capability`,
    the same record, in its provenance. Neither holds the token."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("q6/", "read"), ("q6/", "write")),
                     depth_cap=0)
    record = {"via": "capability", "cap_id": tok.revocation_ids[0], "issuer": a.id,
              "holder_chain": [e.id], "hops": 0}
    for skill, body in _four(inv, e, ns, "q6/tok", token=tok).items():
        assert list(body) == TERMINAL_KEYS, (skill, list(body))
        assert set(body["result"]) == {*RESULT_KEYS[skill], "authz"}, (skill, body["result"])
        assert body["result"]["authz"] == record, (skill, body["result"]["authz"])
    (row,) = inv.memory_rows(ns, "q6/tok")
    assert set(row["provenance_jsonb"]) == {*PROVENANCE_KEYS, "capability"}
    assert row["provenance_jsonb"]["capability"] == record
    assert row["provenance_jsonb"]["payload"]["capability_sha256"] == sha256_hex(
        tok.to_base64())
    assert tok.to_base64()[:16] not in str(row["provenance_jsonb"])


# ============================================================================ A2A

def test_the_a2a_route_takes_the_token_as_invoke_does(inv):
    """`message:send` is the envelope path's other transport (3.1): it is on the accept list,
    runs the same pre-check and passes the token to the same handler. Control: a write inside
    the token's range completes; outside it fails; a token that does not parse is 403 before
    the call starts."""
    a, e = inv.peer("A"), inv.peer("E")
    ns = inv.w.claim(a)
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("a2a/", "write"),), depth_cap=0)

    def state(seen):
        return seen.status, ((seen.body or {}).get("status") or {}).get("state"), seen.code

    ok, _ = inv.a2a(e, "memory.write", write(ns, "a2a/in"), token=tok)
    assert state(ok) == (200, "TASK_STATE_COMPLETED", None), ok
    assert keys_of(inv, ns, "a2a/in") == [1]
    no, _ = inv.a2a(e, "memory.write", write(ns, "out"), token=tok)
    assert state(no) == (200, "TASK_STATE_FAILED", None), no
    assert keys_of(inv, ns, "out") == []
    bad, env = inv.a2a(e, "memory.write", write(ns, "a2a/bad"), token="not-a-biscuit")
    assert (bad.status, bad.code) == (403, "E_CAPABILITY_INVALID"), bad
    assert inv.call_row(env["payload"]["call_id"]) is None


def test_the_a2a_route_refuses_a_token_on_a_call_to_another_peer_before_the_call(inv):
    """The A2A view's own copy of the target check (3.3; the slice 6 review, finding 5: a view
    handing the pre-check the path's target as the memory authority survived every test).
    Without it, step 9's guard still fails the call unexecuted, but only after its skill_calls
    row is written and its nonce burned, and 3.2 says a refusal in the view starts no call. So:
    a token on message:send to any other peer is 403 E_CAPABILITY_NOT_ACCEPTED with no row and
    no nonce. Control: the same call without the header starts, and is queued for its target
    at step 9, as every call to an offline peer is since divergence 041 S5 (integ/r11: it
    failed E_TARGET_UNREACHABLE on the branch's base)."""
    a, e, other = inv.peer("A"), inv.peer("E"), inv.peer("other")
    ns = inv.w.claim(a)
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0)
    inv.w.sql("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
              "is_current) VALUES (%s, 'memory.write', '0.2.0', 'sync', 'grants_only', '{}', "
              "TRUE)", (other.id,))

    seen, env = inv.a2a(e, "memory.write", write(ns, "a2a/remote"), token=tok, target=other.id)
    assert (seen.status, seen.code) == (403, "E_CAPABILITY_NOT_ACCEPTED"), seen
    assert inv.call_row(env["payload"]["call_id"]) is None, "the call must never start"
    assert inv.nonce_rows(env) == [], "no nonce may be checked or burned"

    control, env = inv.a2a(e, "memory.write", write(ns, "a2a/remote"), target=other.id)
    assert control.status == 200, control
    row = inv.call_row(env["payload"]["call_id"])
    assert (row["status"], row["error_code"]) == ("queued", None), row
    assert keys_of(inv, ns, "a2a/remote") == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
