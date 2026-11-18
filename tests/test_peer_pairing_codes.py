"""Any peer can mint a pairing code (divergence 062), with no cap and no rate limit.

WHY. The operator's instruction of 2026-09-30: anyone on the network should be able to mint a
node, and there should be no cap or limit on it. Before this only an OWNER could mint a code
(`POST /v1/auth/onetime`, tests/test_onboard.py), and the redeem took B16's per-IP budget.

WHAT IS PINNED, each beside the control that differs from it in one variable:

  * an active peer mints for ITS OWN owner; the code records the issuer, the new node lands
    under that owner and carries `metadata.onboarded_by_peer_id` (control: an owner-minted
    code records neither);
  * an owner named in the body is ignored (control: the other owner's own path lands there);
  * no token is 401; a suspended peer cannot mint; under DPoP enforcement a proof by another
    key is 403 and nothing is stored (control: the peer's own proof mints);
  * a code whose issuer was suspended is refused 410 and left UNUSED, and redeems once the
    issuer is active again; a used code still answers "already used" first;
  * no cap: one peer mints many codes and one address redeems them all, past B16's default
    burst, and none is refused (the per-owner peer quota is the owner's tier setting, lifted
    here as `set_owner_tier` lifts it, and pinned separately in tests/test_onboard.py).

Run:  .venv313/Scripts/python.exe -m pytest tests/test_peer_pairing_codes.py -q -p no:cacheprovider
"""
from __future__ import annotations

import uuid

from tests.test_onboard import (  # noqa: F401 - fixtures are used by name; sets up Django
    _fresh_limiters,
    claims_of,
    mint_code,
    outcome,
    owner_conn,
    peer_count,
    post,
    redeem,
    server,
    signed_in_owner,
)

from django.test import Client, override_settings  # noqa: E402

from apps.core import redeem_limit  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402

MINT = "/v1/peers/me/pairing-codes"


def onboarded_peer(owner_conn, tag: str) -> dict:
    """An owner, and a node it onboarded the ordinary way: the node's JWT and key seed."""
    signer, owner = signed_in_owner(owner_conn, tag)
    r, seed = redeem(mint_code(owner))
    assert r.status_code == 201, r.content
    return {"owner_id": signer["owner_id"], "owner": owner, "peer_id": uuid.UUID(r.json()["peer_id"]),
            "jwt": r.json()["jwt"], "seed": seed}


def peer_mint(peer: dict, *, seed: bytes | None = None, body=None, client: Client | None = None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {peer['jwt']}",
               "HTTP_DPOP": P.dpop_wire(peer["seed"] if seed is None else seed, "POST", MINT,
                                        peer["jwt"])}
    return post(client or Client(), MINT, body, **headers)


def code_row(conn, code: str):
    return conn.execute("SELECT owner_id, issued_by_peer_id, used_at FROM pairing_codes "
                        "WHERE code = %s", (code,)).fetchone()


def unlimited_peers(conn, owner_id) -> None:
    """The owner's tier setting, as `manage.py set_owner_tier --override peers_per_owner=-1`
    writes it: the per-owner peer count is the owner's plan, not a limit on minting."""
    conn.execute("INSERT INTO owner_tier_overrides (owner_id, capability, value) "
                 "VALUES (%s, 'peers_per_owner', -1) ON CONFLICT (owner_id, capability) "
                 "DO UPDATE SET value = EXCLUDED.value", (owner_id,))


def test_a_peer_mints_a_code_and_the_new_node_lands_under_its_owner(server, owner_conn):
    peer = onboarded_peer(owner_conn, "pm-journey")
    with override_settings(MESHY_DPOP_ENFORCE=True):
        r = peer_mint(peer)
    assert r.status_code == 201, r.content
    body = r.json()
    assert list(body) == ["code", "expires_at", "deep_link"], "the owner route's shape"
    assert len(body["code"]) == 32 and body["deep_link"].endswith(body["code"])
    assert code_row(owner_conn, body["code"]) == (peer["owner_id"], peer["peer_id"], None)

    node, _ = redeem(body["code"])
    assert node.status_code == 201, node.content
    new_id = node.json()["peer_id"]
    assert node.json()["owner_id"] == str(peer["owner_id"])
    assert claims_of(node.json()["jwt"])["scopes"] == []
    assert owner_conn.execute("SELECT owner_id, metadata FROM peers WHERE id = %s",
                              (new_id,)).fetchone() \
        == (peer["owner_id"], {"onboarded_by_peer_id": str(peer["peer_id"])})
    assert owner_conn.execute("SELECT count(*) FROM shared_memory_grant WHERE grantee_peer_id = %s",
                              (new_id,)).fetchone()[0] == 0, "a peer-minted node gets no grants"
    assert code_row(owner_conn, body["code"])[2] is not None, "the code is used"

    # Control: an OWNER-minted code records no issuer, and its node no issuer either.
    owner_code = mint_code(peer["owner"])
    assert code_row(owner_conn, owner_code)[1] is None
    other, _ = redeem(owner_code)
    assert other.status_code == 201, other.content
    assert owner_conn.execute("SELECT metadata FROM peers WHERE id = %s",
                              (other.json()["peer_id"],)).fetchone()[0] is None


def test_the_owner_comes_from_the_peer_never_the_body(server, owner_conn):
    a = onboarded_peer(owner_conn, "pm-own-a")
    b = onboarded_peer(owner_conn, "pm-own-b")
    r = peer_mint(a, body={"owner_id": str(b["owner_id"])})
    assert r.status_code == 201, r.content
    assert code_row(owner_conn, r.json()["code"])[0] == a["owner_id"]
    node, _ = redeem(r.json()["code"])
    assert node.json()["owner_id"] == str(a["owner_id"])
    # Control: B's own peer lands its code in B.
    rb = peer_mint(b)
    assert code_row(owner_conn, rb.json()["code"])[0] == b["owner_id"]


def test_minting_needs_an_active_peer_and_its_own_proof(server, owner_conn):
    peer = onboarded_peer(owner_conn, "pm-auth")
    before = owner_conn.execute("SELECT count(*) FROM pairing_codes").fetchone()[0]

    assert outcome(post(Client(), MINT)) == (401, "E_AUTH_MISSING")
    # An owner's browser session is not a peer credential: the owner has its own route.
    assert outcome(post(peer["owner"], MINT)) == (401, "E_AUTH_MISSING")
    with override_settings(MESHY_DPOP_ENFORCE=True):
        wrong = peer_mint(peer, seed=b"\x07" * 32)
        assert outcome(wrong) == (403, "E_FORBIDDEN"), wrong.content
    assert owner_conn.execute("SELECT count(*) FROM pairing_codes").fetchone()[0] == before

    owner_conn.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (peer["peer_id"],))
    assert outcome(peer_mint(peer)) == (403, "E_FORBIDDEN")
    assert owner_conn.execute("SELECT count(*) FROM pairing_codes").fetchone()[0] == before

    # Control: the same peer, active again, with its own proof, mints.
    owner_conn.execute("UPDATE peers SET status = 'active' WHERE id = %s", (peer["peer_id"],))
    with override_settings(MESHY_DPOP_ENFORCE=True):
        assert peer_mint(peer).status_code == 201


def test_suspending_the_issuer_withdraws_its_unused_codes(server, owner_conn):
    peer = onboarded_peer(owner_conn, "pm-issuer")
    held, used = peer_mint(peer).json()["code"], peer_mint(peer).json()["code"]
    assert redeem(used)[0].status_code == 201
    owner_count = peer_count(owner_conn, peer["owner_id"])

    owner_conn.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (peer["peer_id"],))
    r, _ = redeem(held)
    assert outcome(r) == (410, "E_PAIRING_ISSUER_INACTIVE"), r.content
    assert code_row(owner_conn, held)[2] is None, "a refused redeem used the code"
    assert peer_count(owner_conn, peer["owner_id"]) == owner_count
    # Order: a USED code answers as it always has, whatever its issuer's state.
    assert outcome(redeem(used)[0]) == (410, "E_PAIRING_CONSUMED")
    # An owner-minted code of the same owner is unaffected by a peer's suspension.
    assert redeem(mint_code(peer["owner"]))[0].status_code == 201

    # Control: the issuer active again, the SAME code redeems.
    owner_conn.execute("UPDATE peers SET status = 'active' WHERE id = %s", (peer["peer_id"],))
    again, _ = redeem(held)
    assert again.status_code == 201, again.content


def test_no_cap_on_minting_or_on_redeeming_from_one_address(server, owner_conn):
    peer = onboarded_peer(owner_conn, "pm-nocap")
    unlimited_peers(owner_conn, peer["owner_id"])
    burst, _rate = redeem_limit.config()
    n = burst * 2 + 5                       # well past B16's default burst on one address
    codes = [peer_mint(peer).json()["code"] for _ in range(n)]
    assert len(set(codes)) == n
    before = peer_count(owner_conn, peer["owner_id"])
    for code in codes:
        r, _ = redeem(code, client=Client(REMOTE_ADDR="203.0.113.9"))
        assert r.status_code == 201, (codes.index(code), r.content)
    assert peer_count(owner_conn, peer["owner_id"]) == before + n
    # Control: the invitation route on the same address still has B16's budget.
    for _ in range(burst):
        post(Client(), "/v1/auth/redeem-invitation", {"code": f"g-{uuid.uuid4().hex}"},
             REMOTE_ADDR="203.0.113.9")
    assert outcome(post(Client(), "/v1/auth/redeem-invitation", {"code": "g-last"},
                        REMOTE_ADDR="203.0.113.9"))[0] == 429


def test_the_node_clients_invite_mints_through_this_route(server, owner_conn, tmp_path,
                                                          monkeypatch):
    """`python -m tools.meshy_node invite`, its real headers and DPoP proof, served by this
    route under DPoP enforcement. Only the transport is swapped: the client's `http_json` is
    routed into the Django test client instead of a socket."""
    import json

    from tools.meshy_node import node, wire

    peer = onboarded_peer(owner_conn, "pm-client")
    state_path = tmp_path / "node.json"
    state = {"schema": node.STATE_SCHEMA, "base_url": "http://testserver", "status": "minted",
             "signing_seed_b64url": wire.b64u(peer["seed"]),
             "public_key_b64url": wire.b64u(wire.public_key_of(peer["seed"])),
             "peer_id": str(peer["peer_id"]), "owner_id": str(peer["owner_id"]), "kid": "k",
             "jwt": peer["jwt"], "jwt_expires_at": "2999-01-01T00:00:00Z",
             "memory_peer_id": node.DEFAULT_MEMORY_PEER_ID}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    seen = []

    def through_django(method, base_url, path, *, body=None, headers=None, query=None, **_):
        seen.append((method, path, sorted(headers or {})))
        extra = {"HTTP_" + k.upper().replace("-", "_"): v for k, v in (headers or {}).items()}
        r = Client().generic(method, path, data="" if body is None else json.dumps(body),
                             content_type="application/json", **extra)
        return r.status_code, r.json()

    monkeypatch.setattr(node, "http_json", through_django)
    with override_settings(MESHY_DPOP_ENFORCE=True):
        out = node.invite(state_path, node.load_state(state_path, "http://testserver"), count=3)
    assert seen == [("POST", MINT, ["Authorization", "DPoP"])] * 3
    assert out["issued_by_peer_id"] == str(peer["peer_id"]) and len(out["codes"]) == 3
    for row in out["codes"]:
        assert code_row(owner_conn, row["code"])[:2] == (peer["owner_id"], peer["peer_id"])
    r, _ = redeem(out["codes"][0]["code"])
    assert r.status_code == 201 and r.json()["owner_id"] == str(peer["owner_id"]), r.content


def test_a_redeem_racing_the_issuers_suspension_waits_for_it_and_is_refused(server, owner_conn):
    """The issuer's row is read FOR SHARE, so a redeem that meets a suspension in flight WAITS
    for it and then refuses. Made deterministic as tests/test_onboard.py's code race is: the
    test holds the suspension open while the redeem runs in a thread. Without the lock the
    redeem reads the issuer as still active and onboards a node after the suspension."""
    import threading

    import psycopg

    from tests.test_onboard import DSN

    peer = onboarded_peer(owner_conn, "pm-race")
    code, other = peer_mint(peer).json()["code"], peer_mint(peer).json()["code"]
    result = {}
    worker = threading.Thread(target=lambda: result.update(r=redeem(code)[0]))
    with psycopg.connect(DSN) as suspender:          # NOT autocommit: one open transaction
        suspender.execute("UPDATE peers SET status = 'suspended' WHERE id = %s",
                          (peer["peer_id"],))
        worker.start()
        worker.join(timeout=1.5)
        assert worker.is_alive(), "the redeem must wait on the issuer's row lock"
        suspender.commit()
    worker.join(timeout=60)
    assert not worker.is_alive()
    assert outcome(result["r"]) == (410, "E_PAIRING_ISSUER_INACTIVE"), result["r"].content
    assert code_row(owner_conn, code)[2] is None
    # Control: the issuer active again, an unlocked code of the same peer redeems at once.
    owner_conn.execute("UPDATE peers SET status = 'active' WHERE id = %s", (peer["peer_id"],))
    assert redeem(other)[0].status_code == 201


def test_a_quota_refusal_of_a_peer_minted_code_rolls_everything_back(server, owner_conn):
    """The owner's peers_per_owner tier still applies to a peer-minted code, and its refusal
    leaves the code unused and writes no peer, exactly as for an owner-minted one."""
    import datetime as dt

    peer = onboarded_peer(owner_conn, "pm-quota")
    code = peer_mint(peer).json()["code"]
    first_of_month = dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    owner_conn.execute(
        "INSERT INTO owner_usage (owner_id, capability, cycle_start, counter) "
        "VALUES (%s, 'peers_per_owner', %s, 5) ON CONFLICT (owner_id, capability, "
        "cycle_start) DO UPDATE SET counter = EXCLUDED.counter",
        (peer["owner_id"], first_of_month))
    before = peer_count(owner_conn, peer["owner_id"])
    r, _ = redeem(code)
    assert outcome(r) == (402, "E_QUOTA_EXCEEDED"), r.content
    assert code_row(owner_conn, code)[2] is None
    assert peer_count(owner_conn, peer["owner_id"]) == before
    # Control: the owner's tier lifted, the SAME code redeems.
    unlimited_peers(owner_conn, peer["owner_id"])
    assert redeem(code)[0].status_code == 201


def test_an_unthrottled_refusal_writes_nothing(server, owner_conn):
    """What the removed throttle leaves: unknown, expired and used codes, many times over from
    one address, each refused with its own code and NOTHING written (no peer, key, refresh
    token, or change to any code row). Control: one valid code in the same run writes a peer."""
    peer = onboarded_peer(owner_conn, "pm-bound")
    unlimited_peers(owner_conn, peer["owner_id"])
    used, expired = peer_mint(peer).json()["code"], peer_mint(peer).json()["code"]
    assert redeem(used)[0].status_code == 201
    owner_conn.execute("UPDATE pairing_codes SET expires_at = NOW() - interval '1 second' "
                       "WHERE code = %s", (expired,))

    def snapshot():
        return (owner_conn.execute("SELECT count(*) FROM peers").fetchone()[0],
                owner_conn.execute("SELECT count(*) FROM peer_keys").fetchone()[0],
                owner_conn.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0],
                owner_conn.execute("SELECT md5(string_agg(code || coalesce(used_at::text, '-'), "
                                   "',' ORDER BY code)) FROM pairing_codes").fetchone()[0])

    before = snapshot()
    ip = Client(REMOTE_ADDR="203.0.113.77")
    for i in range(30):
        assert outcome(redeem(f"nope-{i}-{uuid.uuid4().hex}", client=ip)[0]) \
            == (404, "E_PAIRING_INVALID")
        assert outcome(redeem(expired, client=ip)[0]) == (410, "E_PAIRING_EXPIRED")
        assert outcome(redeem(used, client=ip)[0]) == (410, "E_PAIRING_CONSUMED")
    assert snapshot() == before, "a refused redeem wrote something"
    ok, _ = redeem(peer_mint(peer).json()["code"], client=ip)
    assert ok.status_code == 201
    assert snapshot()[0] == before[0] + 1, "control: a valid code on the same address writes"
