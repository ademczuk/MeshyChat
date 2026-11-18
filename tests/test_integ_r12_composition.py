"""integ/r12: what fin/post-r10 (F23 F8, the doorbell JWKS split) and integ/r11 (B07's object
capabilities, skill frames' remote routing) must still mean together. Every refusal sits beside
its positive control, one field apart, in the same test.

  1. B07's guard runs AHEAD of skill frames' remote routing. A token-bearing call to any peer
     but the memory authority is refused E_CAPABILITY_NOT_ACCEPTED, unexecuted: by the view on
     invoke and on A2A message:send (403, no skill_calls row, no nonce), and by step 9 when a
     transport hands start_call a principal (failed, no `routed` or `queued` status or event,
     no doorbell attempt), with the target offline (a verified doorbell) and online (a socket).
     CONTROL: the same call without the token is `queued` with its doorbell attempt, or
     `routed`.
  2. F8's caller-active clause and Biscuit's capability checks compose. A suspended caller is
     refused on every path that starts a call, with and without a token: invoke and A2A to the
     memory authority and to another peer (403 E_FORBIDDEN at authentication, whose peer row
     must be `active`, ahead of the capability pre-check); suspended AFTER authentication, the
     guard refuses a token to another peer (E_CAPABILITY_NOT_ACCEPTED) and step 5 every other
     shape (E_FORBIDDEN, its nonce burned); and start_call with a principal (NotAuthorized at
     step 5, before step 9 can see the principal). No row, no write, on any of them. A call already in flight
     can only be tokenless (1), and once its caller is suspended it is offered by no path:
     the routed arm's read, the queued safety net, a connecting target's redelivery, the
     re-offer sweep, and the doorbell (skipped `nothing_queued`, nothing POSTed). CONTROL:
     every refused call lands while the caller is active, and each offer path takes the same
     rows again once the caller is reactivated.
  3. The doorbell JWKS split leaves Biscuit's key handling untouched. With tokens on and a
     doorbell key in place, /.well-known/jwks.json carries the token key ONLY and
     /.well-known/meshy-invoke-jwks.json the doorbell key ONLY; neither carries the biscuit
     root key; the installed RootKeyStore, its kids and root public key, and /v1/health's
     `capabilities` are what they were before the doorbell key existed, and a token-bearing
     write still completes. CONTROLS: the doorbell set is empty before the key file exists
     (so its later entry is the key's doing); the doorbell kid resolves in the doorbell set
     and the token kid in the token set; a token under a foreign root is refused.

The WebSocket upgrade is not a fourth path: a token on it is refused whatever the accept list
says (apps/core/capability_header.py, CLOSE_CAPABILITY_NOT_ACCEPTED), and a caller frame starts
its call through the same start_call, whose step 5 is 2b's.

Run:  pytest tests/test_integ_r12_composition.py -q
"""
from __future__ import annotations

import json
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

from tests.capability_invoke import invoke_context, outcome  # noqa: E402

TOKEN_JWKS = "/.well-known/jwks.json"
DOORBELL_JWKS = "/.well-known/meshy-invoke-jwks.json"


@pytest.fixture()
def inv():
    with invoke_context() as i:
        yield i


def _write(ns: str, key: str) -> dict:
    return {"namespace": ns, "key": key, "value": {"n": 1}}


def _keys(inv, ns: str, key: str) -> list[int]:
    return [r["version"] for r in inv.memory_rows(ns, key)]


def _send(inv, transport, peer, ns, key, token, target=None):
    if transport == "invoke":
        return inv.call(peer, "memory.write", _write(ns, key), token=token, target=target)
    return inv.a2a(peer, "memory.write", _write(ns, key), token=token, target=target)


def _remote_target(inv, tag: str):
    """A peer other than the memory authority that offers memory.write, with a verified,
    enabled doorbell registration, so a `queued` edge to it rings (041 S9)."""
    other = inv.peer(tag)
    inv.w.sql("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
              "is_current) VALUES (%s, 'memory.write', '0.2.0', 'sync', 'grants_only', '{}', "
              "TRUE)", (other.id,))
    inv.w.sql("INSERT INTO peer_invocation_webhooks (peer_id, url, challenge, verified_at) "
              "VALUES (%s, %s, %s, now())",
              (other.id, f"https://hook.example.test/{tag}", "R" * 43))
    return other


def _events(inv, call_id: str) -> list[str]:
    return [r["event_type"] for r in inv._rows(
        "SELECT event_type FROM skill_call_events WHERE call_id = %s ORDER BY id", (call_id,))]


def _attempts(inv, call_id: str) -> int:
    return len(inv._rows("SELECT id FROM peer_webhook_attempts WHERE call_id = %s", (call_id,)))


def _calls_of(inv, peer) -> int:
    return len(inv._rows("SELECT call_id FROM skill_calls WHERE caller_peer_id = %s",
                         (peer.id,)))


def _start(inv, caller, target, key, ns, *, principal):
    """start_call as a transport that builds a StartCallInput itself (no view in front)."""
    from apps.calls import pipeline
    from apps.calls import route as routing
    from apps.calls.envelope import SignedEnvelope

    env = inv.envelope(caller, "memory.write", _write(ns, key),
                       token=principal.token if principal else None, target=target.id)
    inp = pipeline.StartCallInput(
        caller_peer_id=uuid.UUID(caller.id), target_peer_id=uuid.UUID(target.id),
        skill_name="memory.write", envelope=SignedEnvelope(**env),
        call_id=env["payload"]["call_id"], object_capability=principal)
    ctx = pipeline.CallsContext(conn=inv.w.app, envelope_nonce_enforce=True,
                                memory_peer_id=routing.memory_peer_id_from_settings())
    return env, pipeline.start_call(ctx, inp)


class _Online:
    """The target online: a hub_presence row of its own that beat just now."""

    def __init__(self, inv, peer):
        self.inv, self.peer, self.conn_id = inv, peer, uuid.uuid4()

    def __enter__(self):
        from apps.wsgw import hub_presence
        assert hub_presence.insert(self.inv.w.app, self.conn_id, self.peer.id, uuid.uuid4())
        self.inv.w.app.commit()
        assert hub_presence.is_online(self.inv.w.app, self.peer.id), "control: online"
        return self

    def __exit__(self, *exc):
        from apps.wsgw import hub_presence
        hub_presence.delete(self.inv.w.app, self.conn_id)
        self.inv.w.app.commit()


# ============================================================ 1. the guard ahead of routing

@pytest.mark.parametrize("online", [False, True], ids=["target-offline", "target-online"])
def test_1_a_token_bearing_call_to_a_remote_target_is_refused_ahead_of_routing(inv, online):
    from apps.memoryskills.capability import PresentedCapability

    a, e = inv.peer("A"), inv.peer("E")
    other = _remote_target(inv, "r12-g1")
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "write")
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0)

    def check():
        # The views: 403 before start_call, no row, no nonce.
        for transport in ("invoke", "a2a"):
            seen, env = _send(inv, transport, e, ns, f"g1/{transport}", tok, target=other.id)
            assert outcome(seen) == (403, "E_CAPABILITY_NOT_ACCEPTED", None), (
                transport, seen.status, seen.body)
            assert inv.call_row(env["payload"]["call_id"]) is None, transport
            assert inv.nonce_rows(env) == [], transport
        # Step 9, behind the views: failed unexecuted, never routed or queued, never rung.
        _env, res = _start(inv, e, other, "g1/start", ns,
                           principal=PresentedCapability(tok.to_base64(), "memory.write"))
        row = inv.call_row(res.call_id)
        assert (row["status"], row["error_code"], row["result"]) == (
            "failed", "E_CAPABILITY_NOT_ACCEPTED", None), row
        assert not {"routed", "queued"} & set(_events(inv, res.call_id)), _events(
            inv, res.call_id)
        assert _attempts(inv, res.call_id) == 0, "a token-bearing call rang the doorbell"

        # CONTROL, one field apart (no token), in the same target state.
        expect = "routed" if online else "queued"
        for transport in ("invoke", "a2a"):
            seen, env = _send(inv, transport, e, ns, f"g1c/{transport}", None, target=other.id)
            assert seen.status in (200, 202), (transport, seen.status, seen.body)
            row = inv.call_row(env["payload"]["call_id"])
            assert (row["status"], row["error_code"]) == (expect, None), (transport, row)
            assert expect in _events(inv, row["call_id"]), transport
        _env, res = _start(inv, e, other, "g1c/start", ns, principal=None)
        assert inv.call_row(res.call_id)["status"] == expect
        if not online:
            # The first queued control inserted the one due attempt the target may hold.
            rung = inv._rows("SELECT call_id FROM peer_webhook_attempts WHERE peer_id = %s",
                             (other.id,))
            assert len(rung) == 1, "control: the queued edge rings the doorbell"

    if online:
        with _Online(inv, other):
            check()
    else:
        check()
    for key in ("g1/invoke", "g1/a2a", "g1/start", "g1c/invoke", "g1c/a2a", "g1c/start"):
        assert _keys(inv, ns, key) == [], key


# =============================================== 2. F8's caller clause and the capabilities

@pytest.mark.parametrize("raced", [False, True], ids=["suspended-before", "suspended-after-auth"])
@pytest.mark.parametrize("transport", ["invoke", "a2a"])
def test_2a_a_suspended_caller_is_refused_with_or_without_a_token(inv, monkeypatch, transport,
                                                                  raced):
    """Suspended before the request, authentication refuses first (apps/peers/auth.py, the
    peer row must be `active`): 403 E_FORBIDDEN on all four shapes, ahead of the capability
    pre-check. Suspended AFTER authentication (the race, made by wrapping the extractor), the
    capability guard and step 5 decide: a token to another peer is E_CAPABILITY_NOT_ACCEPTED,
    every other shape step 5's E_FORBIDDEN, the token having passed the view's
    pre-check. The view renders step 5's NotAuthorized as its generic "forbidden", so where
    each refusal happened is read off the nonce ledger: a refusal after step 4c BURNS the
    envelope's nonce (pipeline.py, so the bytes are a replay from then on), and one in
    authentication or the view's guard checks none. Either way: no skill_calls row and no
    write."""
    from apps.a2a import views as a2a_views
    from apps.calls import views as call_views

    a, e = inv.peer("A"), inv.peer("E")
    other = _remote_target(inv, f"r12-s-{transport}-{raced}")
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "write")
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0)

    suspend = {"on": False, "passed": 0}
    for module in (call_views, a2a_views):
        real = module.authenticate_peer

        def authenticate_then_suspend(conn, request, _real=real):
            auth = _real(conn, request)
            if suspend["on"]:
                suspend["passed"] += 1
                inv.w.sql("UPDATE peers SET status = 'suspended' WHERE id = %s",
                          (str(auth.peer_id),))
            return auth

        monkeypatch.setattr(module, "authenticate_peer", authenticate_then_suspend)

    # CONTROLS: while E is active, both forms land at the memory authority, and a tokenless
    # call to another peer starts (queued), so each refusal below is the suspension's.
    for key, token in (("ctl/tok", tok), ("ctl/plain", None)):
        seen, _env = _send(inv, transport, e, ns, key, token)
        assert seen.status == 200, (transport, key, seen.status, seen.body)
        if transport == "invoke":
            assert outcome(seen) == (200, "completed", None), (key, seen.body)
        else:
            assert seen.body["status"]["state"] == "TASK_STATE_COMPLETED", (key, seen.body)
        assert _keys(inv, ns, key) == [1], (transport, key, seen.body)
    seen, env = _send(inv, transport, e, ns, "ctl/remote", None, target=other.id)
    assert inv.call_row(env["payload"]["call_id"])["status"] == "queued", seen.body

    if raced:
        suspend["on"] = True
    else:
        inv.w.sql("UPDATE peers SET status = 'suspended' WHERE id = %s", (e.id,))
    cases = (("sus/tok", tok, None, "E_FORBIDDEN"),
             ("sus/plain", None, None, "E_FORBIDDEN"),
             ("sus/remote-tok", tok, other.id,
              "E_CAPABILITY_NOT_ACCEPTED" if raced else "E_FORBIDDEN"),
             ("sus/remote-plain", None, other.id, "E_FORBIDDEN"))
    for n, (key, token, target, code) in enumerate(cases, start=1):
        if raced:
            inv.w.sql("UPDATE peers SET status = 'active' WHERE id = %s", (e.id,))
        seen, env = _send(inv, transport, e, ns, key, token, target=target)
        assert outcome(seen) == (403, code, None), (transport, key, seen.body)
        assert suspend["passed"] == (n if raced else 0), (transport, key, suspend)
        assert inv.call_row(env["payload"]["call_id"]) is None, (transport, key)
        burned = raced and code == "E_FORBIDDEN"          # step 5, after step 4c
        assert len(inv.nonce_rows(env)) == (1 if burned else 0), (transport, key, burned)
        assert _keys(inv, ns, key) == [], (transport, key, seen.body)


def test_2b_start_call_with_a_principal_refuses_a_suspended_caller_at_step_5(inv):
    """No transport can hand step 9 a suspended caller's principal: step 5 refuses first, and
    writes no row, to the memory authority and to another peer alike."""
    from apps.calls.errors import NotAuthorized
    from apps.memoryskills.capability import PresentedCapability

    a, e = inv.peer("A"), inv.peer("E")
    other = _remote_target(inv, "r12-s2b")
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "write")
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0)
    memory = types.SimpleNamespace(id=inv.memory_peer)

    def principal():
        return PresentedCapability(tok.to_base64(), "memory.write")

    # CONTROL: active, the token writes in-process; to another peer, step 9 fails it.
    _env, res = _start(inv, e, memory, "s2b/ctl", ns, principal=principal())
    assert inv.call_row(res.call_id)["status"] == "completed"
    assert _keys(inv, ns, "s2b/ctl") == [1]
    _env, res = _start(inv, e, other, "s2b/ctl-remote", ns, principal=principal())
    assert inv.call_row(res.call_id)["error_code"] == "E_CAPABILITY_NOT_ACCEPTED"
    assert _calls_of(inv, e) == 2

    inv.w.sql("UPDATE peers SET status = 'suspended' WHERE id = %s", (e.id,))
    for target, key in ((memory, "s2b/sus"), (other, "s2b/sus-remote")):
        with pytest.raises(NotAuthorized, match="not active"):
            _start(inv, e, target, key, ns, principal=principal())
        inv.w.app.rollback()
        assert _calls_of(inv, e) == 2, key
        assert _keys(inv, ns, key) == [], key


def test_2c_a_suspended_callers_in_flight_calls_are_offered_by_no_path(inv, monkeypatch):
    """A call in flight is tokenless (check 1). Its caller suspended, no offer path selects
    it and the doorbell names nothing; reactivated, every path takes it again."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from psycopg.types.json import Jsonb

    from apps.calls import webhooks
    from apps.calls.webhook_key import _of
    from apps.core import rls
    from apps.wsgw import call_notify as cn

    a, e = inv.peer("A"), inv.peer("E")
    other = _remote_target(inv, "r12-s2c")
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "write")
    authority = uuid.UUID(inv.memory_peer)
    target = uuid.UUID(other.id)
    conn = inv.w.app

    def http(key):
        seen, env = inv.call(e, "memory.write", _write(ns, key), target=other.id)
        assert seen.status == 202, seen.body
        return env["payload"]["call_id"]

    queued = http("s2c/queued")
    with _Online(inv, other):
        routed = http("s2c/routed")
    assert (inv.call_row(queued)["status"], inv.call_row(routed)["status"]) == (
        "queued", "routed")
    assert _attempts(inv, queued) == 1

    def select(sql, params):
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute(sql, params)
                out = sorted(r[0] for r in cur.fetchall())
        conn.commit()
        return out

    def read_routed():
        """The routed arm's read, the bridge's own method on this connection."""
        query = types.SimpleNamespace(get=lambda: conn)
        out = [o.call_id for o in cn.CallBridge._read_routed(None, query, routed)]
        conn.commit()
        return out

    def reoffer():
        return select(cn._REOFFER, {"local": [target], "urgent": [target],
                                    "authority": authority, "age": 0.0, "limit": 100})

    def redelivery():
        """The redelivery's selection (CallBridge._redeliver_rows), read-only: its route
        UPDATE carries the same OFFERABLE, exercised by the safety net's below."""
        return select(f"SELECT c.call_id FROM skill_calls c"
                      " WHERE c.target_peer_id = ANY(%(peers)s::uuid[])"
                      " AND c.status = ANY(%(statuses)s::text[]) AND " + cn._OFFERABLE,
                      {"peers": [target], "authority": authority,
                       "statuses": list(cn.REDELIVERED_STATUSES)})

    def safety_net():
        return select(cn._SAFETY_NET, {"call_id": queued, "local": [target],
                                       "authority": authority,
                                       "details": Jsonb({"via": "integ-r12"})})

    posts = []

    def fake_post(url, body, headers=None):
        posts.append(json.loads(body))
        return types.SimpleNamespace(status=200)

    monkeypatch.setattr(webhooks.egress, "post", fake_post)
    key = _of(Ed25519PrivateKey.generate())

    def ring_once():
        """claim_attempt's UPDATE, scoped to this target (earlier tests leave attempts of
        their own due), then the real ring."""
        token = f"integ-r12 {uuid.uuid4().hex[:12]}"
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute("UPDATE peer_webhook_attempts SET state = 'claimed', "
                            "       claimed_by = %s, claimed_at = clock_timestamp() "
                            " WHERE peer_id = %s AND state = 'due' "
                            "RETURNING id, peer_id, call_id, claimed_by", (token, other.id))
                [row] = cur.fetchall()
        conn.commit()
        return webhooks.ring(conn, webhooks.Claim(*row), key, "https://issuer.example.test")

    inv.w.sql("UPDATE peers SET status = 'suspended' WHERE id = %s", (e.id,))
    try:
        assert read_routed() == [], "routed arm"
        assert reoffer() == [], "re-offer sweep"
        assert redelivery() == [], "redelivery"
        assert safety_net() == [], "safety net"
        ring = ring_once()
        assert (ring.state, ring.error_class, ring.rung_for) == (
            "skipped", "nothing_queued", None), ring
        assert posts == [], "the doorbell POSTed for a suspended caller"
        assert (inv.call_row(queued)["status"], inv.call_row(routed)["status"]) == (
            "queued", "routed"), "nothing moved"
    finally:
        inv.w.sql("UPDATE peers SET status = 'active' WHERE id = %s", (e.id,))

    # CONTROL: the caller reactivated, the same rows are offerable on every path.
    assert read_routed() == [routed]
    assert reoffer() == [routed]
    assert redelivery() == sorted([queued, routed])
    again = http("s2c/again")                       # a new due attempt (the last was skipped)
    assert _attempts(inv, again) == 1
    ring = ring_once()
    assert (ring.state, ring.rung_for) == ("sent", again), ring
    assert [p["call_id"] for p in posts] == [again]
    assert safety_net() == [queued]
    assert inv.call_row(queued)["status"] == "routed"


# ======================================================= 3. the JWKS split and Biscuit keys

def test_3_the_doorbell_jwks_split_leaves_biscuit_key_handling_untouched(inv, tmp_path):
    import biscuit_auth

    from apps.authapi import config as authconfig
    from apps.authapi import tokens
    from apps.calls import webhook_key
    from apps.crypto import primitives as P
    from apps.memoryskills import capability_keys
    from meshychat.urls import advertised_capabilities
    from tests.capability_world import ROOT_KID

    client = Client()

    def jwks(path):
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        return json.loads(response.content)["keys"]

    token_kid = P.server_kid(tokens.public_key_raw(authconfig.get_config().jwt_public_key_pem))
    store = capability_keys.current()
    assert store is inv.w.store
    root_x = P.b64u(store.public_key_bytes(ROOT_KID))

    def biscuit_state():
        return {"store": capability_keys.current(), "kids": store.kids,
                "root": store.public_key_bytes(ROOT_KID), "caps": advertised_capabilities()}

    before = biscuit_state()
    assert before["caps"] == ["object-capability-v1"], "control: tokens are on"

    key_path = tmp_path / "doorbell.pem"
    with override_settings(MESHY_INVOKE_WEBHOOK_KEY_PATH=str(key_path)):
        # CONTROL: no key file yet, the doorbell set is empty and the token set is the token
        # key alone, so what appears below is the key's doing.
        assert jwks(DOORBELL_JWKS) == []
        assert [k["kid"] for k in jwks(TOKEN_JWKS)] == [token_kid]

        key = webhook_key.ensure(key_path)
        token_set, doorbell_set = jwks(TOKEN_JWKS), jwks(DOORBELL_JWKS)
        assert [k["kid"] for k in token_set] == [token_kid], token_set
        assert doorbell_set == [{"kty": "OKP", "crv": "Ed25519",
                                 "x": P.b64u(key.public_raw()), "kid": key.kid,
                                 "alg": "EdDSA", "use": "sig"}], doorbell_set
        assert key.kid != token_kid
        # A doorbell resolves only in its own set; a token only in the token set.
        header = json.loads(P.b64u_decode(key.sign_jwt({"x": 1}).split(".")[0]))
        assert header["kid"] in {k["kid"] for k in doorbell_set}
        assert header["kid"] not in {k["kid"] for k in token_set}
        # Neither set publishes the biscuit root key.
        assert root_x not in {k["x"] for k in token_set + doorbell_set}

        # Biscuit's key handling: the same store, kids, root key and advertisement.
        assert biscuit_state() == before

        # And tokens still decide calls. CONTROL: a token under this root completes; the
        # same shape under a foreign root is refused at the view.
        a, e = inv.peer("A"), inv.peer("E")
        ns = inv.w.claim(a)
        tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0)
        seen, _env = inv.call(e, "memory.write", _write(ns, "j/ok"), token=tok)
        assert outcome(seen) == (200, "completed", None), seen.body
        assert _keys(inv, ns, "j/ok") == [1]
        foreign = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),),
                             depth_cap=0, register=False, root=biscuit_auth.KeyPair())
        seen, env = inv.call(e, "memory.write", _write(ns, "j/foreign"), token=foreign)
        assert outcome(seen) == (403, "E_CAPABILITY_INVALID", None), seen.body
        assert inv.call_row(env["payload"]["call_id"]) is None
        assert _keys(inv, ns, "j/foreign") == []
    assert biscuit_state() == before


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
