"""A brand-new node can be minted: owner sign-in, a pairing code, redemption (divergence 016).

THE JOURNEY IS THE POINT. Before this change no application path created an owner, a peer
or a first key: `pairing_codes` existed in the schema and nothing wrote it. So the first
test here starts from a database with NO owner in it and walks the whole path an operator
and a node walk, through the project URLconf, with a token the server minted:

    manage.py bootstrap_owner  ->  POST /v1/auth/owner/challenge + /v1/auth/owner/sign-in
    ->  POST /v1/auth/onetime  ->  POST /v1/onboard/peers  ->  that JWT on a peer route

The rest pin each refusal on the way, each beside the positive control that differs from
it in the one variable under test (docs/V1-DESIGN-RULES.md C1). Mutation evidence for the
single-use and expiry checks is recorded in docs/DIVERGENCES.md 016.

THE CSRF TOKEN COMES FROM THE SERVER. A cookie write needs `X-CSRF-Token`, and
`GET /v1/auth/me` hands a session its token, so `csrf_for` asks that route as a headless
client must. `test_me_hands_a_cookie_session_its_csrf_token` checks the value with a
different instrument: it is recomputed from the session row and the secret.

Run:  .venv/Scripts/python.exe -m pytest tests/test_onboard.py -q -p no:cacheprovider
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import os
import secrets
import sys
import threading
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN_DEFAULT = "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.authapi import bootstrap, onboard, ratelimit  # noqa: E402
from apps.authapi import config as authconfig  # noqa: E402
from apps.authapi.didkey import to_did_key  # noqa: E402
from apps.core import schema_sets, websec  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN", DSN_DEFAULT)
ISSUER = "meshy-onboard-test"
STATE_SECRET = "onboard-test-state-secret-0123456789"
PRESENCE = "/v1/peers/me/presence"
INBOX = "/v1/mesh/inbox"


# --------------------------------------------------------------------------- fixtures

def _keypair_pem() -> tuple[bytes, bytes]:
    sk = Ed25519PrivateKey.generate()
    return (
        sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                         serialization.NoEncryption()),
        sk.public_key().public_bytes(serialization.Encoding.PEM,
                                     serialization.PublicFormat.SubjectPublicKeyInfo),
    )


def _serve(dsn: str):
    """The server as deployed, pointed at `dsn`: the project URLconf, one JWT key pair
    for mint (authapi config) and verify (settings), a CSRF state secret, DEBUG off."""
    sk_pem, pk_pem = _keypair_pem()
    settings_cm = override_settings(
        ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"], DEBUG=False,
        MESHY_JWT_PUBLIC_KEY_PEM=pk_pem, MESHY_JWT_ISSUER=ISSUER, MESHY_DSN=dsn,
        MESHY_STATE_SECRET=STATE_SECRET)
    config_cm = authconfig.override_config(
        jwt_issuer=ISSUER, jwt_signing_key_pem=sk_pem, jwt_public_key_pem=pk_pem,
        jwt_public_key_history_pem=(), dsn=dsn)
    return settings_cm, config_cm, pk_pem


@pytest.fixture()
def server():
    settings_cm, config_cm, pk_pem = _serve(DSN)
    with settings_cm, config_cm:
        yield {"dsn": DSN, "pk": pk_pem}


@pytest.fixture(autouse=True)
def _fresh_limiters():
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


@pytest.fixture()
def owner_conn():
    """Owner (superuser) role: seeds and inspects. Never a connection a request uses."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="module")
def fresh_install():
    """A database with the full schema and NO owner in it: a brand-new install.

    Cloned from the migrated template when that is current (as tests/conftest.py does),
    migrated from scratch otherwise. Its own database, not the run's shared one, because
    "the FIRST owner" is only a meaningful claim where no owner exists yet."""
    base = DSN.rsplit("/", 1)[0]
    admin = base + "/postgres"
    name = f"meshy_onboard_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    dsn = f"{base}/{name}"
    template = os.environ.get("MESHY_CLONE_TEMPLATE", "meshy_template")
    cloned = False
    with psycopg.connect(admin, autocommit=True) as conn:
        have = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                            (template,)).fetchone() is not None
    if have:
        try:
            with psycopg.connect(f"{base}/{template}") as tconn:
                stale = any(schema_sets.pending(tconn).values())
        except (psycopg.Error, schema_sets.SchemaIncomplete):
            stale = True
        if not stale:
            try:
                with psycopg.connect(admin, autocommit=True) as conn:
                    conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
                cloned = True
            except psycopg.Error:
                cloned = False
    if not cloned:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        with psycopg.connect(dsn) as conn:
            schema_sets.apply_all(conn, verbose=False)
    with psycopg.connect(dsn) as conn:
        schema_sets.assert_schema_complete(conn)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# --------------------------------------------------------------------------- helpers

def post(client, path: str, payload=None, **extra):
    body = "" if payload is None else json.dumps(payload)
    return client.post(path, data=body, content_type="application/json", **extra)


def outcome(response) -> tuple[int, str | None]:
    try:
        return response.status_code, response.json().get("error")
    except ValueError:
        return response.status_code, None


def operator_grants_mesh_read(dsn: str, peer_id, owner_id) -> None:
    """The operator step onboarding leaves out (docs/ONBOARD.md:55-56): read on
    shared/mesh-chat, which `GET /v1/mesh/inbox` requires (divergence 018 (a)).

    A namespace owner must be a dev-plane owner (sql/042 enforce_ns_dev_only) and the
    bootstrapped owner is product-plane, so a fresh install's unclaimed namespace is
    claimed by a dev-plane peer of its own and the node is GRANTED read, as a fleet node
    is. Written as the database owner: this is a fixture, not a request path."""
    ns = "shared/mesh-chat"
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace"
                    " WHERE namespace = %s", (ns,))
        row = cur.fetchone()
        if row is None:
            ns_owner, ns_peer = uuid.uuid4(), uuid.uuid4()
            cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                        " VALUES (%s,%s,'dev',%s)",
                        (ns_owner, f"onb-ns-{ns_owner.hex[:8]}@example.test", uuid.uuid4()))
            cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                        (ns_peer, ns_owner, f"onb-ns-{ns_peer.hex[:8]}"))
            cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                        " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                        " VALUES (%s,%s,%s,'dev',FALSE,'keep_all')", (ns, ns_peer, ns_owner))
            row = (ns_peer, ns_owner)
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id)"
                    " VALUES (%s,%s,%s,'read',%s,%s)", (ns, peer_id, owner_id, row[0], row[1]))


def seed_signer(conn, tag: str, *, register: bool = True, plane: str | None = None) -> dict:
    """An owner, its signer peer with an active key, and (optionally) that key on the
    owner-sign-in allowlist."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    if plane is None:
        conn.execute("INSERT INTO dashboard_users (id, email) VALUES (%s, %s)",
                     (owner_id, f"onb-{tag}-{owner_id.hex[:8]}@example.test"))
    else:
        # sql/040: (plane = 'dev') = (trust_group_id IS NOT NULL).
        conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                     "VALUES (%s, %s, %s, %s)",
                     (owner_id, f"onb-{tag}-{owner_id.hex[:8]}@example.test", plane,
                      uuid.uuid4() if plane == "dev" else None))
    conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s, %s, %s)",
                 (peer_id, owner_id, f"onb-{tag}-{peer_id.hex[:8]}"))
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s, %s, %s, TRUE)", (peer_id, P.kid_for_peer_key(pub), P.b64u(pub)))
    if register:
        conn.execute("INSERT INTO owner_human_keys (owner_id, kid, public_key) "
                     "VALUES (%s, %s, %s)", (owner_id, to_did_key(pub), P.b64u(pub)))
    return {"owner_id": owner_id, "peer_id": peer_id, "seed": seed, "pub": pub}


def owner_signature(seed: bytes, peer_id, nonce: str, *, aud: str = "dashboard",
                    purpose: str = "meshy-owner-signin-v1") -> str:
    """Signs the JCS encoding of {aud, nonce, peer_id, purpose} and returns the signature
    base64url-encoded."""
    return P.b64u(P.sign(seed, P.jcs({"aud": aud, "nonce": nonce,
                                      "peer_id": str(peer_id), "purpose": purpose})))


def owner_challenge(client, peer_id) -> str:
    r = post(client, "/v1/auth/owner/challenge", {"peer_id": str(peer_id)})
    assert r.status_code == 200, r.content
    return r.json()["nonce"]


def owner_sign_in(client, signer: dict, **sig_kw):
    nonce = owner_challenge(client, signer["peer_id"])
    body = {"peer_id": str(signer["peer_id"]), "nonce": nonce,
            "signature_b64": owner_signature(signer["seed"], signer["peer_id"], nonce,
                                             **sig_kw)}
    return post(client, "/v1/auth/owner/sign-in", body), body


def csrf_for(client) -> str:
    """The session's CSRF token, from `GET /v1/auth/me` - the way a client learns it."""
    r = client.get("/v1/auth/me")
    assert r.status_code == 200, r.content
    token = r.json()["csrf_token"]
    assert token, "a server with a state secret must hand the session a token"
    return token


def signed_in_owner(owner_conn, tag: str) -> tuple[dict, Client]:
    signer = seed_signer(owner_conn, tag)
    client = Client()
    r, _ = owner_sign_in(client, signer)
    assert r.status_code == 200, r.content
    return signer, client


def mint_code(client) -> str:
    r = post(client, "/v1/auth/onetime", HTTP_X_CSRF_TOKEN=csrf_for(client))
    assert r.status_code == 201, r.content
    return r.json()["code"]


def redeem(code: str, seed: bytes | None = None, client: Client | None = None, **extra_body):
    seed = os.urandom(32) if seed is None else seed
    body = {"onetime_code": code, "public_key": P.b64u(P.public_key_of(seed)),
            "device_label": "test-node", **extra_body}
    return post(client or Client(), "/v1/onboard/peers", body), seed


def claims_of(token: str) -> dict:
    return pyjwt.decode(token, options={"verify_signature": False})


def peer_count(conn, owner_id) -> int:
    return conn.execute("SELECT count(*) FROM peers WHERE owner_id = %s",
                        (owner_id,)).fetchone()[0]


# =========================================================================== 1. JOURNEY

def test_a_brand_new_install_mints_its_first_owner_and_then_a_working_node(
        fresh_install, tmp_path):
    dsn = fresh_install
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM dashboard_users").fetchone()[0] == 0, \
            "the install must start with no owner, or 'first owner' proves nothing"

    # --- the operator: a key pair; only the PUBLIC half is handed to the command.
    operator_seed = os.urandom(32)
    operator_sk = Ed25519PrivateKey.from_private_bytes(operator_seed)
    pub_file = tmp_path / "owner.pub"
    pub_file.write_bytes(operator_sk.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    out = io.StringIO()
    call_command("bootstrap_owner", email="operator@example.test",
                 public_key_file=str(pub_file), dsn=dsn, stdout=out)
    created = json.loads(out.getvalue())
    signer = {"owner_id": uuid.UUID(created["owner_id"]),
              "peer_id": uuid.UUID(created["peer_id"]), "seed": operator_seed}

    settings_cm, config_cm, _ = _serve(dsn)
    with settings_cm, config_cm:
        # --- owner sign-in -> a meshy_session cookie.
        owner = Client()
        r, _ = owner_sign_in(owner, signer)
        assert r.status_code == 200, r.content
        assert r.json()["owner_id"] == created["owner_id"]
        cookie = r.cookies["meshy_session"]
        assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Lax"
        assert int(cookie["max-age"]) > 86_000

        # --- a pairing code, minted with that cookie.
        r = post(owner, "/v1/auth/onetime", HTTP_X_CSRF_TOKEN=csrf_for(owner))
        assert r.status_code == 201, r.content
        minted = r.json()
        assert list(minted) == ["code", "expires_at", "deep_link"]
        assert minted["deep_link"] == "agentmeshy-mentee://onboard?code=" + minted["code"]

        # --- the node: its own key, sent as the node client sends it.
        node_seed = os.urandom(32)
        node_pub = P.public_key_of(node_seed)
        r, _ = redeem(minted["code"], node_seed)
        assert r.status_code == 201, r.content
        body = r.json()
        # The node client (`tools/meshy_node/node.py`) reads peer_id, owner_id, kid and jwt from
        # this body, and refresh_token and jwt_expires_at optionally; the response carries exactly
        # these six.
        assert list(body) == ["peer_id", "owner_id", "kid", "jwt", "refresh_token",
                              "jwt_expires_at"]
        assert body["owner_id"] == created["owner_id"]
        claims = claims_of(body["jwt"])
        assert claims["sub"] == created["owner_id"]
        assert claims["act"] == {"sub": body["peer_id"]}
        assert claims["aud"] == "peer" and claims["scopes"] == [] and "lin" not in claims
        assert claims["cnf"] == to_did_key(node_pub), "cnf must be the NODE's key"
        assert claims["exp"] - claims["iat"] == 3600

        # --- the JWT works on a peer route; a forged one does not (control).
        # The inbox needs read on shared/mesh-chat (divergence 018 (a)) and onboarding
        # grants nothing (divergence 016), so the node's genuine token is AUTHENTICATED
        # and then refused by the namespace gate: a typed 403, where the forged token is
        # a 401 before any gate runs. The operator's separate grant step
        # (docs/ONBOARD.md:55-56) is what opens the inbox, and it is the positive control.
        node = Client()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {body['jwt']}"}
        assert outcome(node.get(INBOX, **auth)) == (403, "E_NAMESPACE_UNCLAIMED"), (
            "a fresh install has no mesh-chat claim, and onboarding must not create one")
        forged = body["jwt"][:-2] + ("AA" if body["jwt"][-2:] != "AA" else "BB")
        assert node.get(INBOX, HTTP_AUTHORIZATION=f"Bearer {forged}").status_code == 401
        operator_grants_mesh_read(dsn, body["peer_id"], created["owner_id"])
        assert node.get(INBOX, **auth).status_code == 200
        assert node.get(INBOX, HTTP_AUTHORIZATION=f"Bearer {forged}").status_code == 401, (
            "the grant opened the inbox for the node's token, not for a forged one")

        # --- and it WRITES under DPoP enforcement, with a proof by the node's key; the
        # operator's key, on the same token and route, is refused (the control).
        with override_settings(MESHY_DPOP_ENFORCE=True):
            wrong = P.dpop_wire(operator_seed, "POST", PRESENCE, body["jwt"])
            r = node.post(PRESENCE, data="{}", content_type="application/json",
                          HTTP_DPOP=wrong, **auth)
            assert outcome(r) == (403, "E_FORBIDDEN")
            right = P.dpop_wire(node_seed, "POST", PRESENCE, body["jwt"])
            r = node.post(PRESENCE, data="{}", content_type="application/json",
                          HTTP_DPOP=right, **auth)
            assert r.status_code == 200, r.content

        # --- the refresh token rotates (the stored hash and issued_under_kid are right).
        r = post(node, "/v1/auth/refresh", {"refresh_token": body["refresh_token"]})
        assert r.status_code == 200, r.content
        assert claims_of(r.json()["jwt"])["act"] == {"sub": body["peer_id"]}

    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT p.owner_id, p.display_name, k.public_key, k.kid "
                           "FROM peers p JOIN peer_keys k ON k.peer_id = p.id "
                           "WHERE p.id = %s", (body["peer_id"],)).fetchone()
    assert row == (signer["owner_id"], "test-node", P.b64u(node_pub), body["kid"])


# ======================================================================= 2. SINGLE USE

def test_a_pairing_code_redeems_once(server, owner_conn):
    signer, owner = signed_in_owner(owner_conn, "once")
    code = mint_code(owner)
    first, _ = redeem(code)
    assert first.status_code == 201, first.content
    second, _ = redeem(code)                      # a different node, the same code
    assert outcome(second) == (410, "E_PAIRING_CONSUMED")
    assert second.json()["message"] == "pairing code already used"
    assert peer_count(owner_conn, signer["owner_id"]) == 2, "signer + ONE onboarded node"
    # Control: the same owner's NEXT code still redeems, so the refusal was the code and
    # not the owner, the quota or the route.
    again, _ = redeem(mint_code(owner))
    assert again.status_code == 201, again.content


def test_an_expired_code_is_refused_and_left_unconsumed(server, owner_conn):
    signer, owner = signed_in_owner(owner_conn, "expiry")
    stale, fresh = mint_code(owner), mint_code(owner)
    owner_conn.execute("UPDATE pairing_codes SET expires_at = NOW() - interval '1 second' "
                       "WHERE code = %s", (stale,))
    r, seed = redeem(stale)
    assert outcome(r) == (410, "E_PAIRING_EXPIRED")
    assert r.json()["message"] == "pairing code expired"
    assert owner_conn.execute("SELECT used_at FROM pairing_codes WHERE code = %s",
                              (stale,)).fetchone()[0] is None
    assert owner_conn.execute("SELECT count(*) FROM peer_keys WHERE public_key = %s",
                              (P.b64u(P.public_key_of(seed)),)).fetchone()[0] == 0
    # Control: an unexpired code from the same owner, minted in the same second.
    ok, _ = redeem(fresh)
    assert ok.status_code == 201, ok.content


def test_a_redeem_that_races_a_winner_waits_for_it_and_loses(server, owner_conn):
    """The code row is read FOR UPDATE, so a redeem racing one
    that is about to commit WAITS and then sees the code used. Made deterministic by
    playing the winner from the test: a transaction that locks the row, marks it used and
    holds, while the route runs in a thread. Without the row lock the route reads the old
    row version, sails past the single-use check, and only blocks at its own UPDATE -
    after creating a peer - then commits a second redemption of one code."""
    _, owner = signed_in_owner(owner_conn, "race")
    code, other = mint_code(owner), mint_code(owner)
    result = {}
    worker = threading.Thread(target=lambda: result.update(r=redeem(code)[0]))
    with psycopg.connect(DSN) as winner:            # NOT autocommit: one open transaction
        winner.execute("SELECT 1 FROM pairing_codes WHERE code = %s FOR UPDATE", (code,))
        winner.execute("UPDATE pairing_codes SET used_at = NOW() WHERE code = %s", (code,))
        worker.start()
        worker.join(timeout=1.5)
        assert worker.is_alive(), "the redeem must be waiting on the winner's row lock"
        winner.commit()
    worker.join(timeout=60)
    assert not worker.is_alive()
    assert outcome(result["r"]) == (410, "E_PAIRING_CONSUMED")
    # Control: an unlocked code from the same owner redeems at once.
    ok, _ = redeem(other)
    assert ok.status_code == 201, ok.content


def test_the_code_lives_five_minutes(server, owner_conn):
    """The code lives for 300 s. The deep link and stored row agree with the response."""
    _, owner = signed_in_owner(owner_conn, "ttl")
    before = dt.datetime.now(dt.timezone.utc)
    r = post(owner, "/v1/auth/onetime", HTTP_X_CSRF_TOKEN=csrf_for(owner))
    assert r.status_code == 201, r.content
    expires = dt.datetime.fromisoformat(r.json()["expires_at"].replace("Z", "+00:00"))
    assert 299 <= (expires - before).total_seconds() <= 301
    stored = owner_conn.execute("SELECT expires_at FROM pairing_codes WHERE code = %s",
                                (r.json()["code"],)).fetchone()[0]
    assert abs((stored - expires).total_seconds()) < 0.001
    assert len(r.json()["code"]) == 32, "pairing codes are 24 unpadded base64url bytes"


def test_unknown_and_empty_codes(server):
    assert outcome(redeem("no-such-code-" + uuid.uuid4().hex)[0]) == (404, "E_PAIRING_INVALID")
    assert outcome(redeem("   ")[0]) == (400, "E_PAIRING_INVALID")
    r = post(Client(), "/v1/onboard/peers", {"public_key": "x"})
    assert outcome(r) == (422, "E_VALIDATION"), "missing onetime_code is a body error"


# =============================================================== 3. OWNER A IS NOT OWNER B

def test_a_code_minted_by_owner_a_onboards_only_into_owner_a(server, owner_conn):
    """The redeem route takes NO owner from its caller: the code names the owner. An
    `owner_id` in the body is ignored like any unknown field,
    and a session cookie for B on the redeeming request changes nothing."""
    a, a_client = signed_in_owner(owner_conn, "own-a")
    b, b_client = signed_in_owner(owner_conn, "own-b")
    b_before = peer_count(owner_conn, b["owner_id"])
    code_a = mint_code(a_client)

    r, _ = redeem(code_a, client=b_client, owner_id=str(b["owner_id"]))
    assert r.status_code == 201, r.content
    assert r.json()["owner_id"] == str(a["owner_id"])
    assert claims_of(r.json()["jwt"])["sub"] == str(a["owner_id"])
    assert owner_conn.execute("SELECT owner_id FROM peers WHERE id = %s",
                              (r.json()["peer_id"],)).fetchone()[0] == a["owner_id"]
    assert peer_count(owner_conn, b["owner_id"]) == b_before
    # Control: B's own code does land in B, so the check above could have failed.
    rb, _ = redeem(mint_code(b_client))
    assert rb.json()["owner_id"] == str(b["owner_id"])
    assert peer_count(owner_conn, b["owner_id"]) == b_before + 1


# ======================================================================= 4. OWNER SIGN-IN

def test_a_replayed_owner_challenge_is_refused(server, owner_conn):
    signer = seed_signer(owner_conn, "replay")
    client = Client()
    first, body = owner_sign_in(client, signer)
    assert first.status_code == 200, first.content
    replay = post(Client(), "/v1/auth/owner/sign-in", body)
    assert outcome(replay) == (401, "E_AUTH_INVALID")
    assert replay.json()["message"] == "owner sign-in challenge or signature invalid"
    assert "meshy_session" not in replay.cookies
    # Control: a FRESH challenge for the same peer and key still signs in.
    again, _ = owner_sign_in(Client(), signer)
    assert again.status_code == 200, again.content


def test_owner_sign_in_needs_the_key_on_the_owner_allowlist(server, owner_conn):
    """TASK-352 Option B: a valid signature is not sufficient."""
    signer = seed_signer(owner_conn, "allow", register=False)
    r, _ = owner_sign_in(Client(), signer)
    assert outcome(r) == (403, "E_OWNER_KEY_UNREGISTERED")
    assert "meshy_session" not in r.cookies
    owner_conn.execute("INSERT INTO owner_human_keys (owner_id, kid, public_key) "
                       "VALUES (%s, %s, %s)",
                       (signer["owner_id"], "hk-" + uuid.uuid4().hex, P.b64u(signer["pub"])))
    ok, _ = owner_sign_in(Client(), signer)
    assert ok.status_code == 200, ok.content
    # A REVOKED registration authorises nothing.
    owner_conn.execute("UPDATE owner_human_keys SET revoked_at = NOW() WHERE owner_id = %s",
                       (signer["owner_id"],))
    r, _ = owner_sign_in(Client(), signer)
    assert outcome(r) == (403, "E_OWNER_KEY_UNREGISTERED")


def test_owner_sign_in_failures_are_one_answer_and_each_burns_its_nonce(server, owner_conn):
    """The nonce is consumed FIRST, and every challenge/key/signature
    failure is the same 401 with this route's own message, so the route is no oracle for which
    part was wrong - and a failed request has still used its nonce up."""
    signer = seed_signer(owner_conn, "uniform")
    other = seed_signer(owner_conn, "uniform-other")
    keyless = seed_signer(owner_conn, "uniform-keyless")
    owner_conn.execute("UPDATE peer_keys SET active = FALSE WHERE peer_id = %s",
                       (keyless["peer_id"],))

    def attempt(peer_id, nonce, seed):
        return post(Client(), "/v1/auth/owner/sign-in", {
            "peer_id": str(peer_id), "nonce": nonce,
            "signature_b64": owner_signature(seed, peer_id, nonce)})

    wrong_key_nonce = owner_challenge(Client(), signer["peer_id"])
    cases = {
        "wrong_key": attempt(signer["peer_id"], wrong_key_nonce, other["seed"]),
        "unknown_nonce": attempt(signer["peer_id"], P.b64u(os.urandom(32)), signer["seed"]),
        "no_active_key": attempt(keyless["peer_id"], owner_challenge(Client(), keyless["peer_id"]),
                                 keyless["seed"]),
        "unknown_peer": attempt(uuid.uuid4(), P.b64u(os.urandom(32)), signer["seed"]),
    }
    seen = {label: (r.status_code, r.content) for label, r in cases.items()}
    assert set(seen.values()) == {(401, b'{"error": "E_AUTH_INVALID", "message": '
                                        b'"owner sign-in challenge or signature invalid"}')}, seen
    assert not any("meshy_session" in r.cookies for r in cases.values())

    # The wrong-key attempt burned its nonce: the right key over it is refused too.
    assert attempt(signer["peer_id"], wrong_key_nonce, signer["seed"]).status_code == 401
    # CONTROL: the right key over a fresh nonce signs in.
    ok, _ = owner_sign_in(Client(), signer)
    assert ok.status_code == 200, ok.content


def test_views_and_onboard_share_one_request_scaffolding():
    """The body parser, field readers, RFC 3339 form and rollback carrier live once, in
    apps/authapi/request.py, which imports neither route module - so neither has to import
    the other inside a function body to dodge a cycle."""
    from apps.authapi import request as shared
    from apps.authapi import views
    for name in ("_body", "_rfc3339", "_field", "_Refusal"):
        assert getattr(onboard, name) is getattr(shared, name), name
    for name in ("_body", "_rfc3339", "_field", "_Refusal", "_uuid_field", "_str_field"):
        assert getattr(views, name) is getattr(shared, name), name
    # The validators the human-key route borrows are onboard's own, bound at module scope.
    assert views._strict_b64 is onboard._strict_b64
    assert views.is_edwards_point is onboard.is_edwards_point


def test_a_peer_sign_in_signature_cannot_mint_an_owner_session(server, owner_conn):
    signer = seed_signer(owner_conn, "purpose")
    r, _ = owner_sign_in(Client(), signer, aud="peer", purpose="meshy-signin-v1")
    assert outcome(r) == (401, "E_AUTH_INVALID")
    ok, _ = owner_sign_in(Client(), signer)
    assert ok.status_code == 200, ok.content


def test_a_peer_suspended_after_its_challenge_cannot_sign_in_as_owner(server, owner_conn):
    """The route re-checks `status = 'active'` AFTER the signature: the
    challenge was stored while the peer was active, so nothing earlier can refuse it."""
    signer = seed_signer(owner_conn, "suspended")
    nonce = owner_challenge(Client(), signer["peer_id"])
    body = {"peer_id": str(signer["peer_id"]), "nonce": nonce,
            "signature_b64": owner_signature(signer["seed"], signer["peer_id"], nonce)}
    owner_conn.execute("UPDATE peers SET status = 'suspended' WHERE id = %s",
                       (signer["peer_id"],))
    assert outcome(post(Client(), "/v1/auth/owner/sign-in", body)) == (401, "E_AUTH_INVALID")
    # Control: the same peer, active again, with a fresh challenge.
    owner_conn.execute("UPDATE peers SET status = 'active' WHERE id = %s", (signer["peer_id"],))
    ok, _ = owner_sign_in(Client(), signer)
    assert ok.status_code == 200, ok.content


def test_owner_challenge_answers_alike_for_a_peer_that_does_not_exist(server):
    """No existence oracle: same shape, nothing stored."""
    r = post(Client(), "/v1/auth/owner/challenge", {"peer_id": str(uuid.uuid4())})
    assert r.status_code == 200 and set(r.json()) == {"nonce", "expires_at"}


# ============================================================ 5. WHO MAY MINT A CODE

def _pat(conn, owner_id, scopes) -> str:
    raw = "meshypat_" + secrets.token_urlsafe(24)
    conn.execute("INSERT INTO owner_api_keys (owner_id, name, token_hash, token_prefix, "
                 "scopes) VALUES (%s, %s, %s, %s, %s)",
                 (owner_id, "onboard-test", hashlib.sha256(raw.encode()).hexdigest(),
                  raw[:12], scopes))
    return raw


def test_onetime_needs_an_owner_and_both_scopes(server, owner_conn):
    signer, owner = signed_in_owner(owner_conn, "mint-auth")
    assert outcome(post(Client(), "/v1/auth/onetime")) == (401, "E_AUTH_MISSING")
    # Cookie without its CSRF token: refused. With it: see the control at the end.
    assert outcome(post(owner, "/v1/auth/onetime")) == (403, "E_CSRF_REQUIRED")
    for scopes, want in (([], (403, "E_SCOPE_REQUIRED")),
                         (["peers:write"], (403, "E_SCOPE_REQUIRED")),
                         (["tokens:mint"], (403, "E_SCOPE_REQUIRED")),
                         (["peers:write", "tokens:mint"], (201, None))):
        pat = _pat(owner_conn, signer["owner_id"], scopes)
        got = outcome(post(Client(), "/v1/auth/onetime", HTTP_AUTHORIZATION=f"Bearer {pat}"))
        assert got == want, (scopes, got)
    assert outcome(post(owner, "/v1/auth/onetime",
                        HTTP_X_CSRF_TOKEN=csrf_for(owner)))[0] == 201


def test_me_hands_a_cookie_session_its_csrf_token(server, owner_conn):
    """Cookie only; the token is computed over THIS session."""
    signer, owner = signed_in_owner(owner_conn, "me")
    assert outcome(Client().get("/v1/auth/me")) == (401, "E_AUTH_MISSING")
    stranger = Client()
    stranger.cookies["meshy_session"] = "no-such-session"
    assert outcome(stranger.get("/v1/auth/me")) == (401, "E_SESSION_EXPIRED")
    pat = _pat(owner_conn, signer["owner_id"], ["peers:write", "tokens:mint"])
    assert outcome(Client().get("/v1/auth/me", HTTP_AUTHORIZATION=f"Bearer {pat}")) \
        == (401, "E_AUTH_MISSING"), "a PAT is not accepted as a /me credential"

    r = owner.get("/v1/auth/me")
    assert r.status_code == 200, r.content
    body = r.json()
    assert list(body) == ["owner_id", "email", "tier", "is_admin", "session_expires_at",
                          "onboarding_dismissed_at", "csrf_token", "entitlements"]
    assert body["owner_id"] == str(signer["owner_id"])
    assert (body["tier"], body["is_admin"], body["entitlements"]) == ("free", False, None)
    # A different instrument: the HMAC recomputed from the session row and the secret.
    raw = owner.cookies["meshy_session"].value
    sid = owner_conn.execute("SELECT id FROM owner_sessions WHERE token_hash = %s",
                             (hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()[0]
    assert body["csrf_token"] == websec.csrf_token_for(sid, STATE_SECRET.encode())
    # Bound to the session: another owner's token is refused on this cookie's write, and
    # this session's own token is the control.
    _, other = signed_in_owner(owner_conn, "me-other")
    assert outcome(post(owner, "/v1/auth/onetime", HTTP_X_CSRF_TOKEN=csrf_for(other))) \
        == (403, "E_CSRF_REQUIRED")
    assert post(owner, "/v1/auth/onetime",
                HTTP_X_CSRF_TOKEN=body["csrf_token"]).status_code == 201
    # An EXPIRED session is not found, however valid its cookie.
    owner_conn.execute("UPDATE owner_sessions SET expires_at = NOW() - interval '1 second' "
                       "WHERE id = %s", (sid,))
    assert outcome(owner.get("/v1/auth/me")) == (401, "E_SESSION_EXPIRED")


def test_me_reports_the_entitlements_verdict_and_stale_as_inactive(server, owner_conn):
    """Absent -> null, Fresh -> the row, Stale -> inactive, not omitted."""
    signer, owner = signed_in_owner(owner_conn, "me-ent")
    assert owner.get("/v1/auth/me").json()["entitlements"] is None
    owner_conn.execute(
        "INSERT INTO accounts_entitlements (owner_id, issuer, subject, active, capabilities, "
        "principal_capabilities, recheck_at, fetched_at) VALUES (%s, %s, %s, TRUE, %s, %s, "
        "NOW() + interval '1 hour', NOW())",
        (signer["owner_id"], "https://accounts.example.test", uuid.uuid4().hex,
         ["meshy.calls.invoke"], ["meshy.calls.invoke"]))
    fresh = owner.get("/v1/auth/me").json()["entitlements"]
    assert (fresh["active"], fresh["capabilities"], fresh["principal_capabilities"]) == \
        (True, ["meshy.calls.invoke"], ["meshy.calls.invoke"])
    owner_conn.execute("UPDATE accounts_entitlements SET recheck_at = NOW() - interval "
                       "'1 second' WHERE owner_id = %s", (signer["owner_id"],))
    stale = owner.get("/v1/auth/me").json()["entitlements"]
    assert (stale["active"], stale["capabilities"], stale["principal_capabilities"]) == \
        (False, [], [])


def test_without_a_state_secret_there_is_no_token_and_cookie_writes_fail_closed(
        server, owner_conn, monkeypatch):
    """With DEBUG off and no state secret, cookie writes are refused. /me then offers null, and
    null is not permission."""
    _, owner = signed_in_owner(owner_conn, "nosecret")
    monkeypatch.delenv("MESHYCHAT_STATE_SECRET", raising=False)
    with override_settings(MESHY_STATE_SECRET=None):
        assert owner.get("/v1/auth/me").json()["csrf_token"] is None
        assert outcome(post(owner, "/v1/auth/onetime", HTTP_X_CSRF_TOKEN="anything")) \
            == (403, "E_CSRF_REQUIRED")
    # Control: the same session, the secret back, its token accepted.
    assert post(owner, "/v1/auth/onetime", HTTP_X_CSRF_TOKEN=csrf_for(owner)).status_code \
        == 201


# ================================================================= 6. WHAT A PEER GETS

def test_a_new_peer_receives_no_grants_and_no_scopes(server, owner_conn):
    """Onboarding grants nothing (apps/authapi/onboard.py). Absence needs a control: the same
    query must be able to SEE a grant, so one is added afterwards and counted."""
    _, owner = signed_in_owner(owner_conn, "grants")
    skill_grants_before = owner_conn.execute("SELECT count(*) FROM skill_grants").fetchone()[0]
    r, _ = redeem(mint_code(owner))
    assert r.status_code == 201, r.content
    peer_id = uuid.UUID(r.json()["peer_id"])

    def shared_grants() -> int:
        return owner_conn.execute("SELECT count(*) FROM shared_memory_grant "
                                  "WHERE grantee_peer_id = %s", (peer_id,)).fetchone()[0]

    assert shared_grants() == 0
    assert owner_conn.execute("SELECT count(*) FROM skill_grants").fetchone()[0] \
        == skill_grants_before
    assert owner_conn.execute("SELECT scopes FROM peers WHERE id = %s",
                              (peer_id,)).fetchone()[0] is None
    assert claims_of(r.json()["jwt"])["scopes"] == []

    # Control: a namespace (claimable only by a dev-plane owner, migration 038) and a
    # grant to the new peer - the query now counts it.
    ns_owner = seed_signer(owner_conn, "ns-owner", register=False, plane="dev")
    ns = f"shared/onboard-control-{uuid.uuid4().hex[:8]}"
    owner_conn.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                       "owner_owner_id) VALUES (%s, %s, %s)",
                       (ns, ns_owner["peer_id"], ns_owner["owner_id"]))
    new_owner = owner_conn.execute("SELECT owner_id FROM peers WHERE id = %s",
                                   (peer_id,)).fetchone()[0]
    owner_conn.execute(
        "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
        "permission, granted_by_peer_id, granted_by_owner_id) VALUES (%s,%s,%s,%s,%s,%s)",
        (ns, peer_id, new_owner, "write", ns_owner["peer_id"], ns_owner["owner_id"]))
    assert shared_grants() == 1


def test_the_peers_per_owner_cap_refuses_and_rolls_everything_back(server, owner_conn):
    """The cap refusal keeps its message and leaves the code UNCONSUMED by transactional rollback,
    so the same code can redeem after the cap is lifted."""
    signer, owner = signed_in_owner(owner_conn, "quota")
    code = mint_code(owner)
    first_of_month = dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    owner_conn.execute(
        "INSERT INTO owner_usage (owner_id, capability, cycle_start, counter) "
        "VALUES (%s, 'peers_per_owner', %s, 5) ON CONFLICT (owner_id, capability, "
        "cycle_start) DO UPDATE SET counter = EXCLUDED.counter",
        (signer["owner_id"], first_of_month))
    r, seed = redeem(code)
    assert outcome(r) == (402, "E_QUOTA_EXCEEDED")
    assert r.json()["message"] == "quota exceeded for peers_per_owner: 6 would exceed cap 5"
    assert peer_count(owner_conn, signer["owner_id"]) == 1, "the signer only"
    assert owner_conn.execute("SELECT used_at FROM pairing_codes WHERE code = %s",
                              (code,)).fetchone()[0] is None
    # Control: lift the cap and the SAME code now redeems.
    owner_conn.execute("INSERT INTO owner_tier_overrides (owner_id, capability, value) "
                       "VALUES (%s, 'peers_per_owner', 10)", (signer["owner_id"],))
    ok, _ = redeem(code, seed)
    assert ok.status_code == 201, ok.content


def test_public_key_forms(server, owner_conn):
    """Pins accepted and rejected public-key forms."""
    _, owner = signed_in_owner(owner_conn, "pubkey")
    code = mint_code(owner)
    bad = {"not base64 at all!": "public_key is not a valid 32-byte Ed25519 key",
           P.b64u(os.urandom(31)): "public_key must be 32 bytes",
           # URL-safe alphabet WITH padding: no engine accepts it (a '-' is outside the
           # STANDARD alphabet, and the two NO_PAD engines refuse any '=').
           "-" + "A" * 42 + "=": "public_key is not a valid 32-byte Ed25519 key"}
    for text, message in bad.items():
        r = post(Client(), "/v1/onboard/peers", {"onetime_code": code, "public_key": text})
        assert outcome(r) == (400, "E_PUBKEY_INVALID") and r.json()["message"] == message
    # Control, and the fallback engine: STANDARD base64 with padding is accepted and
    # stored in the one canonical form.
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    r = post(Client(), "/v1/onboard/peers",
             {"onetime_code": code, "public_key": base64.b64encode(pub).decode()})
    assert r.status_code == 201, r.content
    assert owner_conn.execute("SELECT public_key FROM peer_keys WHERE kid = %s",
                              (r.json()["kid"],)).fetchone()[0] == P.b64u(pub)


def test_a_key_that_is_not_a_curve_point_onboards_with_a_bearer_token(server, owner_conn):
    """A key that is not a valid curve point is onboarded without `cnf`, as a plain bearer token;
    a valid key gets `cnf`."""
    _, owner = signed_in_owner(owner_conn, "nonpoint")
    not_a_point = (2).to_bytes(32, "little")
    assert not onboard.is_edwards_point(not_a_point)
    r = post(Client(), "/v1/onboard/peers",
             {"onetime_code": mint_code(owner), "public_key": P.b64u(not_a_point)})
    assert r.status_code == 201, r.content
    assert "cnf" not in claims_of(r.json()["jwt"])
    ok, seed = redeem(mint_code(owner))
    assert claims_of(ok.json()["jwt"])["cnf"] == to_did_key(P.public_key_of(seed))


# ================================================================ 7. PURE PIECES

def _rfc8032_decodes(raw: bytes) -> bool:
    """RFC 8032 s5.1.3 point decoding, a DIFFERENT computation from `is_edwards_point`
    (square root by exponentiation and a check, not Euler's criterion). Only canonical
    y (< p) is compared, where the two definitions coincide."""
    p = 2 ** 255 - 19
    d = (-121665 * pow(121666, p - 2, p)) % p
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    u, v = (y * y - 1) % p, (d * y * y + 1) % p
    x = (u * pow(v, 3, p) * pow(u * pow(v, 7, p), (p - 5) // 8, p)) % p
    if (v * x * x - u) % p == 0:
        return True
    return (v * x * x + u) % p == 0


def test_the_point_check_agrees_with_rfc8032_decoding():
    for y in range(0, 3000):
        raw = y.to_bytes(32, "little")
        assert onboard.is_edwards_point(raw) is _rfc8032_decodes(raw), y
    for _ in range(200):
        assert onboard.is_edwards_point(P.public_key_of(os.urandom(32)))


def test_the_base64_engines_are_strict():
    raw = os.urandom(32)
    canonical = P.b64u(raw)
    assert onboard.decode_pubkey(canonical) == raw
    assert onboard.decode_pubkey(base64.b64encode(raw).decode()) == raw
    assert onboard.decode_pubkey(base64.b64encode(raw).decode().rstrip("=")) == raw
    # Non-canonical trailing bits: the last char of 43 carries 2 unused bits.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(canonical[-1])
    assert last & 0b11 == 0
    assert onboard.decode_pubkey(canonical[:-1] + alphabet[last | 1]) is None
    assert onboard.decode_pubkey(canonical[:20] + "!" + canonical[21:]) is None
    # Padding with a URL-safe-only character: no engine accepts it. Without the '-' the
    # same shape IS valid padded STANDARD base64, so the second line is
    # the control, not a contradiction. (The first draft padded a random key and was
    # flaky for exactly that reason: about a quarter of keys hold no '-' or '_'.)
    assert onboard.decode_pubkey("-" + "A" * 42 + "=") is None
    assert onboard.decode_pubkey("A" * 43 + "=") == bytes(32)


# ================================================================ 8. bootstrap_owner

def _pub_pem(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def test_bootstrap_refuses_when_an_owner_exists_unless_told(tmp_path, owner_conn):
    owner_conn.execute("INSERT INTO dashboard_users (email) VALUES (%s)",
                       (f"exists-{uuid.uuid4().hex[:8]}@example.test",))
    pub = tmp_path / "k.pub"
    pub.write_bytes(_pub_pem(os.urandom(32)))
    email = f"second-{uuid.uuid4().hex[:8]}@example.test"
    with pytest.raises(CommandError, match="not the first owner"):
        call_command("bootstrap_owner", email=email, public_key_file=str(pub), dsn=DSN,
                     stdout=io.StringIO())
    assert owner_conn.execute("SELECT count(*) FROM dashboard_users WHERE email = %s",
                              (email,)).fetchone()[0] == 0
    out = io.StringIO()
    call_command("bootstrap_owner", email=email, public_key_file=str(pub), dsn=DSN,
                 additional_owner=True, stdout=out)
    made = json.loads(out.getvalue())
    rows = owner_conn.execute(
        "SELECT (SELECT count(*) FROM peers WHERE owner_id = u.id), "
        "       (SELECT count(*) FROM owner_human_keys WHERE owner_id = u.id "
        "          AND public_key = %s AND revoked_at IS NULL), "
        "       (SELECT count(*) FROM peer_keys WHERE peer_id = %s AND public_key = %s "
        "          AND active) "
        "FROM dashboard_users u WHERE u.email = %s",
        (made["public_key"], made["peer_id"], made["public_key"], email)).fetchone()
    assert rows == (1, 1, 1)
    # A re-run with the same email is refused even with the flag: never a re-key.
    with pytest.raises(CommandError, match="already exists"):
        call_command("bootstrap_owner", email=email, public_key_file=str(pub), dsn=DSN,
                     additional_owner=True, stdout=io.StringIO())


def test_bootstrap_never_reads_a_private_key(tmp_path):
    sk_pem, _ = _keypair_pem()
    path = tmp_path / "private.pem"
    path.write_bytes(sk_pem)
    with pytest.raises(CommandError, match="PRIVATE key"):
        call_command("bootstrap_owner", email="x@example.test", public_key_file=str(path),
                     dsn=DSN, additional_owner=True, stdout=io.StringIO())
    # Control: the public half of the same key parses.
    pub = serialization.load_pem_private_key(sk_pem, None).public_key()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert bootstrap.read_public_key(pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)) == raw
    assert bootstrap.read_public_key((P.b64u(raw) + "\n").encode()) == raw


def test_bootstrap_refuses_keys_that_could_never_sign_in():
    with pytest.raises(bootstrap.BootstrapRefused, match="not a valid Ed25519 point"):
        bootstrap.read_public_key(P.b64u((2).to_bytes(32, "little")).encode())
    small = bytes.fromhex("0100000000000000000000000000000000000000000000000000000000000000")
    with pytest.raises(bootstrap.BootstrapRefused, match="small-order"):
        bootstrap.read_public_key(P.b64u(small).encode())


def test_bootstrap_has_no_default_database(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHY_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    pub = tmp_path / "k.pub"
    pub.write_bytes(_pub_pem(os.urandom(32)))
    with pytest.raises(CommandError, match="no database named"):
        call_command("bootstrap_owner", email="x@example.test", public_key_file=str(pub),
                     stdout=io.StringIO())
