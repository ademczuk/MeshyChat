"""The auth surface, proved end to end against the real database and real Ed25519 keys.

WHAT THESE ARE FOR: every interesting assertion on this surface is a NEGATIVE — a
replayed nonce must not work, an expired token must not verify, a wrong-purpose
signature must not authenticate. A negative passes vacuously the moment the setup is
broken: a route that 500s, a peer that was never inserted, a key the server never had.
So each rejection below is paired with a POSITIVE CONTROL that differs from it in
exactly the one variable under test, in the same test function. Where the control is
subtle the comment says what would still be green if the control were missing.

Run:  .venv/Scripts/python.exe -m pytest tests/test_auth_flows.py -q
Needs MESHY_TEST_DSN (defaults to the dev stack database built by sqlmigrate.py).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN_DEFAULT = "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django"

# Django settings are PROCESS-GLOBAL, and other test modules in this suite boot the
# same project settings. Every variable below is set with `setdefault` so that whichever
# module imports first wins and the others inherit it, instead of the run depending on
# pytest's collection order. `apps.authapi.testsettings` is the fallback for running
# this file alone; the routes under test are then pinned per-test with
# `override_settings(ROOT_URLCONF=...)`, so these tests hold whatever the project
# URLconf ends up including.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
os.environ.setdefault("MESHY_DSN", os.environ.get("MESHY_TEST_DSN", DSN_DEFAULT))

import django  # noqa: E402
from django.conf import settings as django_settings  # noqa: E402

try:
    django.setup()
except Exception:  # noqa: BLE001 - the project settings module may not be importable yet
    os.environ["DJANGO_SETTINGS_MODULE"] = "apps.authapi.testsettings"
    django_settings._wrapped = django.conf.empty
    django.setup()

from django.test import Client, override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.authapi import ratelimit, tokens  # noqa: E402
from apps.authapi.didkey import from_did_key, to_did_key  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN", DSN_DEFAULT)
ISSUER = "meshy-test"


# --------------------------------------------------------------------------- helpers

def keypair_pem() -> tuple[bytes, bytes, bytes]:
    """(pkcs8 private PEM, SubjectPublicKeyInfo PEM, raw 32-byte public key).

    PKCS#8 is not a preference: the signing-key loader accepts nothing else and raises
    `JwtError` on SEC1/OpenSSH input."""
    sk = Ed25519PrivateKey.generate()
    sk_pem = sk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk = sk.public_key()
    pk_pem = pk.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    raw = pk.public_bytes(encoding=serialization.Encoding.Raw,
                          format=serialization.PublicFormat.Raw)
    return sk_pem, pk_pem, raw


@pytest.fixture(autouse=True)
def _auth_urlconf():
    """Pin the routes under test regardless of which settings module booted the
    process. Without this these tests would silently 404 the moment another module
    won the `DJANGO_SETTINGS_MODULE` race, and a 404 is not distinguishable from a
    route that was never wired."""
    with override_settings(ROOT_URLCONF="meshychat.urls"):
        yield


@pytest.fixture(autouse=True)
def _fresh_limiters():
    """The limiter is process-global and keyed partly on the client IP, which the test
    client holds constant at 127.0.0.1. Without this reset the 31st request of the whole
    FILE would 429 and unrelated tests would fail for a reason that has nothing to do
    with what they assert."""
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


@pytest.fixture()
def server_keys():
    """Install a freshly generated server key pair for the duration of one test."""
    sk_pem, pk_pem, raw = keypair_pem()
    with authconfig.override_config(
        jwt_issuer=ISSUER,
        jwt_signing_key_pem=sk_pem,
        jwt_public_key_pem=pk_pem,
        jwt_public_key_history_pem=(),
        dsn=DSN,
    ):
        yield {"private_pem": sk_pem, "public_pem": pk_pem, "raw": raw}


@pytest.fixture()
def owner_conn():
    """Owner role: bypasses RLS. Used only to seed fixtures and to inspect state."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def client():
    return Client()


def make_peer(conn, tag: str, *, scopes=None, with_key: bool = True, status: str = "active"):
    """An owner + peer (+ active peer key). Column names read off the live schema:
    the owner FK targets `dashboard_users`, not `owners`."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    seed = os.urandom(32)
    pubkey = P.public_key_of(seed)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email) VALUES (%s, %s)",
                    (owner_id, f"auth-{tag}-{owner_id.hex[:8]}@example.test"))
        cur.execute(
            "INSERT INTO peers (id, owner_id, name, display_name, status, scopes) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (peer_id, owner_id, f"auth-{tag}-{peer_id.hex[:8]}", f"auth {tag}", status, scopes))
        if with_key:
            cur.execute(
                "INSERT INTO peer_keys (peer_id, kid, public_key) VALUES (%s,%s,%s)",
                (peer_id, P.kid_for_peer_key(pubkey), P.b64u(pubkey)))
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id, "seed": seed, "pubkey": pubkey,
            "kid": P.kid_for_peer_key(pubkey)}


def post(client, path: str, payload: dict):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def signin_signature(seed: bytes, peer_id, nonce: str,
                     purpose: str = authconfig.SIGNIN_PURPOSE, aud: str = "peer") -> str:
    """The exact bytes a peer signs: JCS over
    {aud, nonce, peer_id, purpose}. `purpose` is the domain separator — it is what
    stops this signature being replayed as an owner-session mint."""
    canonical = P.jcs({"aud": aud, "nonce": nonce,
                       "peer_id": str(peer_id), "purpose": purpose})
    return P.b64u(P.sign(seed, canonical))


def get_challenge(client, peer_id) -> str:
    response = post(client, "/v1/auth/challenge", {"peer_id": str(peer_id)})
    assert response.status_code == 200, response.content
    return response.json()["nonce"]


def segment(token: str, index: int) -> bytes:
    part = token.split(".")[index]
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# ------------------------------------------------------------------- tokens.py policy

def test_claim_set_and_serialisation_match_the_token_shape(server_keys):
    """The claim names, omitted optional fields, and field order are pinned."""
    peer, owner, lineage = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    claims = tokens.build_claims(
        sub=owner, iss=ISSUER, iat=tokens.now_unix(), exp=tokens.expires_in_secs(60),
        scopes=["skills:declare"], aud="peer", lin=lineage,
        cnf="did:key:zExample", act_sub=peer)
    token = tokens.sign(claims, server_keys["private_pem"])

    header = json.loads(segment(token, 0))
    assert header == {"typ": "JWT", "alg": "EdDSA"}, "Header::new(Algorithm::EdDSA)"
    assert segment(token, 0) == b'{"typ":"JWT","alg":"EdDSA"}', (
        "jsonwebtoken emits declaration order; a sorted header is a different token")

    body = json.loads(segment(token, 1))
    assert list(body) == ["sub", "iss", "iat", "exp", "scopes", "aud", "lin", "cnf", "act"], (
        "the JWT payload fields must use their declared order")
    assert body["sub"] == str(owner) and body["act"] == {"sub": str(peer)}
    assert body["cnf"] == "did:key:zExample"

    # `aud` is the one optional field that stays on
    # the wire as null. lin/cnf/act are omitted when absent, so they vanish.
    bare = tokens.build_claims(sub=owner, iss=ISSUER, iat=tokens.now_unix(),
                               exp=tokens.expires_in_secs(60))
    assert json.loads(segment(tokens.sign(bare, server_keys["private_pem"]), 1)) == {
        "sub": str(owner), "iss": ISSUER, "iat": bare["iat"], "exp": bare["exp"],
        "scopes": [], "aud": None,
    }, "lin/cnf/act must be ABSENT and aud must be present-and-null"

    # Positive control for every rejection below: this token verifies.
    decoded = tokens.verify(token, [server_keys["public_pem"]], ISSUER)
    assert decoded["sub"] == str(owner) and decoded["lin"] == str(lineage)


def test_expired_token_is_rejected(server_keys):
    """Expiration validation is the only bound on a token's lifetime. A
    regression that disabled it would let a captured JWT verify forever."""
    common = dict(sub=uuid.uuid4(), iss=ISSUER, scopes=[], aud="peer")
    # 600s in the past — well past the 60s leeway jsonwebtoken defaults to, so the
    # only thing that can reject this is the exp check.
    stale = tokens.sign(tokens.build_claims(
        iat=tokens.now_unix() - 1200, exp=tokens.expires_in_secs(-600), **common),
        server_keys["private_pem"])
    fresh = tokens.sign(tokens.build_claims(
        iat=tokens.now_unix(), exp=tokens.expires_in_secs(600), **common),
        server_keys["private_pem"])

    with pytest.raises(tokens.JwtError):
        tokens.verify(stale, [server_keys["public_pem"]], ISSUER)
    # CONTROL: identical key, issuer and signature path; only the expiry differs. If
    # this failed too, the rejection above would prove nothing about exp.
    assert tokens.verify(fresh, [server_keys["public_pem"]], ISSUER)["aud"] == "peer"


def test_token_signed_under_a_rotated_out_key_verifies_via_history(server_keys):
    """The key-history path. A server with no history configured boots
    perfectly clean and fails only when a frame signed under the previous key arrives,
    which is why this needs its own test rather than
    being assumed from a clean start-up."""
    old_sk, old_pk, _ = keypair_pem()
    current_pk = server_keys["public_pem"]
    _, stranger_pk, _ = keypair_pem()

    token = tokens.sign(tokens.build_claims(
        sub=uuid.uuid4(), iss=ISSUER, iat=tokens.now_unix(),
        exp=tokens.expires_in_secs(600), aud="peer"), old_sk)

    # NEGATIVE: current key alone cannot verify it. Without this the test below would
    # pass even if `verify` ignored the key argument entirely.
    with pytest.raises(tokens.JwtError):
        tokens.verify(token, [current_pk], ISSUER)
    # NEGATIVE: the history is not a bypass — an unrelated key does not rescue it.
    with pytest.raises(tokens.JwtError):
        tokens.verify(token, [current_pk, stranger_pk], ISSUER)
    # POSITIVE: with the rotated-out key in the history, it verifies.
    assert tokens.verify(token, [current_pk, old_pk], ISSUER)["aud"] == "peer"

    # And the history does not weaken the OTHER checks: an expired token signed under
    # the historical key is still refused, rather than being retried until some key
    # "accepts" it.
    expired_old = tokens.sign(tokens.build_claims(
        sub=uuid.uuid4(), iss=ISSUER, iat=tokens.now_unix() - 1200,
        exp=tokens.expires_in_secs(-600), aud="peer"), old_sk)
    with pytest.raises(tokens.JwtError):
        tokens.verify(expired_old, [current_pk, old_pk], ISSUER)

    # The config assembles the same list in the same order (current first).
    with authconfig.override_config(jwt_public_key_history_pem=(old_pk,)):
        keys = authconfig.get_config().verify_keys()
    assert keys == (current_pk, old_pk)
    assert tokens.verify(token, keys, ISSUER)["aud"] == "peer"


def test_wrong_typed_exp_is_rejected(server_keys):
    """CVE-2026-25537 class: a spec claim with the wrong JSON type must
    not be treated as absent. PyJWT's own exp check is `int(payload["exp"])`, which
    ACCEPTS the string below — so this is testing our added type check, not the
    library's."""
    base = {"sub": str(uuid.uuid4()), "iss": ISSUER, "iat": tokens.now_unix(), "scopes": []}
    string_exp = tokens.sign({**base, "exp": str(tokens.expires_in_secs(600))},
                             server_keys["private_pem"])
    int_exp = tokens.sign({**base, "exp": tokens.expires_in_secs(600)},
                          server_keys["private_pem"])

    with pytest.raises(tokens.JwtError, match="exp"):
        tokens.verify(string_exp, [server_keys["public_pem"]], ISSUER)
    # CONTROL: same key, same claims, exp as a NUMBER. Proves the rejection is about
    # the type and not about some unrelated breakage in this hand-built payload.
    assert tokens.verify(int_exp, [server_keys["public_pem"]], ISSUER)["iss"] == ISSUER

    # `iss` is required for the same reason: pinning the issuer alone does not
    # make it required, so a missing one would skip the issuer pin.
    no_iss = tokens.sign({"sub": base["sub"], "iat": base["iat"],
                          "exp": tokens.expires_in_secs(600)}, server_keys["private_pem"])
    with pytest.raises(tokens.JwtError):
        tokens.verify(no_iss, [server_keys["public_pem"]], ISSUER)


def test_issuer_is_pinned_but_audience_is_not(server_keys):
    """Audience validation is disabled: audience is the caller's job, so a
    dashboard-audience token must still DECODE here. The issuer pin is what must hold."""
    dashboard = tokens.sign(tokens.build_claims(
        sub=uuid.uuid4(), iss=ISSUER, iat=tokens.now_unix(),
        exp=tokens.expires_in_secs(600), aud="dashboard"), server_keys["private_pem"])
    # POSITIVE: aud is not enforced here.
    assert tokens.verify(dashboard, [server_keys["public_pem"]], ISSUER)["aud"] == "dashboard"
    # NEGATIVE: the issuer IS. Same token, verified against a different issuer.
    with pytest.raises(tokens.JwtError):
        tokens.verify(dashboard, [server_keys["public_pem"]], "someone-else")


def test_only_eddsa_is_accepted(server_keys):
    """Only EdDSA is accepted. An HS256 token whose 'key' is the public key is the
    classic algorithm-confusion forgery; it must not decode."""
    import hmac
    # Built by hand: PyJWT refuses to ENCODE an HMAC token under a PEM key, which is
    # exactly the mistake being simulated, so the attacker's side is assembled here.
    header = b'{"alg":"HS256","typ":"JWT"}'
    body = json.dumps({"sub": str(uuid.uuid4()), "iss": ISSUER,
                       "exp": tokens.expires_in_secs(600)},
                      separators=(",", ":")).encode()
    signing_input = f"{P.b64u(header)}.{P.b64u(body)}".encode("ascii")
    forged = (signing_input.decode("ascii") + "." +
              P.b64u(hmac.new(server_keys["public_pem"], signing_input,
                              hashlib.sha256).digest()))
    with pytest.raises(tokens.JwtError):
        tokens.verify(forged, [server_keys["public_pem"]], ISSUER)
    # CONTROL: the same claims signed with EdDSA do decode, so the rejection is about
    # the algorithm rather than about the claim set.
    genuine = tokens.sign(tokens.build_claims(sub=uuid.uuid4(), iss=ISSUER,
                                              iat=tokens.now_unix(),
                                              exp=tokens.expires_in_secs(600)),
                          server_keys["private_pem"])
    assert tokens.verify(genuine, [server_keys["public_pem"]], ISSUER)["iss"] == ISSUER


def test_did_key_round_trips_and_is_not_a_jkt():
    """`cnf` carries the peer's did:key, not an RFC 9449 jkt."""
    raw = P.public_key_of(bytes([3] * 32))
    did = to_did_key(raw)
    assert did.startswith("did:key:z")
    assert from_did_key(did) == raw
    # NEGATIVE: the thumbprint an RFC 9449 implementation would have put here is a
    # different string entirely, so a verifier expecting one never matches the other.
    assert did != P.b64u(P.sha256(raw))


# ------------------------------------------------------------------------ JWKS route

def test_jwks_uses_the_server_kid_rule(client, server_keys):
    response = client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    keys = response.json()["keys"]
    assert len(keys) == 1
    key = keys[0]
    assert key["kty"] == "OKP" and key["crv"] == "Ed25519" and key["alg"] == "EdDSA"
    assert key["x"] == P.b64u(server_keys["raw"]), "x is the RAW 32-byte key, base64url"

    expected = P.server_kid(server_keys["raw"])
    assert key["kid"] == expected
    assert len(expected) == 16 and all(c in "0123456789abcdef" for c in expected), (
        "server kid must be 16 lowercase hexadecimal characters from sha256(raw)[..8]")
    # NEGATIVE: the peer-key rule produces a different string from the same digest.
    # Two derivations, deliberately not merged; publishing the peer one here would
    # give a kid no verifier can resolve.
    assert key["kid"] != P.kid_for_peer_key(server_keys["raw"])


# ------------------------------------------------------------------- challenge route

def test_challenge_is_not_a_peer_existence_oracle(client, owner_conn, server_keys):
    """Uniform response for any well-formed peer_id: mint and
    store for a peer that exists, mint and DISCARD for one that does not."""
    peer = make_peer(owner_conn, "oracle")
    ghost = uuid.uuid4()

    known = post(client, "/v1/auth/challenge", {"peer_id": str(peer["peer_id"])})
    unknown = post(client, "/v1/auth/challenge", {"peer_id": str(ghost)})

    assert known.status_code == unknown.status_code == 200
    assert sorted(known.json()) == sorted(unknown.json()) == ["expires_at", "nonce"]
    assert len(known.json()["nonce"]) == len(unknown.json()["nonce"]) == 43, "32 bytes b64url"

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM auth_challenges WHERE peer_id = %s", (ghost,))
        stored_for_ghost = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM auth_challenges WHERE peer_id = %s",
                    (peer["peer_id"],))
        stored_for_known = cur.fetchone()[0]
    assert stored_for_ghost == 0, "a nonce for a non-existent peer must not be stored"
    # CONTROL: the write path works at all. Without it, "0 rows for the ghost" would
    # also be green if the INSERT were broken for everyone.
    assert stored_for_known == 1


# -------------------------------------------------------------------- sign-in route

def test_sign_in_round_trip_with_a_real_key(client, owner_conn, server_keys):
    peer = make_peer(owner_conn, "roundtrip", scopes=["skills:declare"])
    nonce = get_challenge(client, peer["peer_id"])

    response = post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]),
        "nonce": nonce,
        "signature_b64": signin_signature(peer["seed"], peer["peer_id"], nonce),
    })
    assert response.status_code == 200, response.content
    body = response.json()
    assert sorted(body) == ["jwt", "jwt_expires_at"], "no refresh token is issued"

    claims = tokens.verify(body["jwt"], [server_keys["public_pem"]], ISSUER)
    # TASK-25 on-behalf-of: sub is the authorizing OWNER, act.sub the acting PEER.
    assert claims["sub"] == str(peer["owner_id"])
    assert claims["act"] == {"sub": str(peer["peer_id"])}
    assert claims["aud"] == "peer"
    assert claims["scopes"] == ["skills:declare"], (
        "migration 104: a sign-in must mint the peer's PROVISIONED scopes, or the peer "
        "loses every scope on its first re-mint")
    assert "lin" not in claims, "a sign-in JWT has no lin claim"
    assert from_did_key(claims["cnf"]) == peer["pubkey"], "cnf binds the peer's own key"

    # The challenge row is now consumed — the state the replay test depends on.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT consumed_at FROM auth_challenges WHERE peer_id=%s AND nonce=%s",
                    (peer["peer_id"], nonce))
        assert cur.fetchone()[0] is not None


def test_replayed_nonce_is_rejected(client, owner_conn, server_keys):
    """Single-use, enforced by the atomic consume."""
    peer = make_peer(owner_conn, "replay")
    nonce = get_challenge(client, peer["peer_id"])
    signature = signin_signature(peer["seed"], peer["peer_id"], nonce)
    payload = {"peer_id": str(peer["peer_id"]), "nonce": nonce, "signature_b64": signature}

    first = post(client, "/v1/auth/sign-in", payload)
    replay = post(client, "/v1/auth/sign-in", payload)

    # CONTROL: the very same bytes worked once. Without this, a 401 on the replay would
    # also be what a broken signature check, a missing peer or a bad nonce looks like.
    assert first.status_code == 200, first.content
    assert replay.status_code == 401
    assert replay.json() == {"error": "E_AUTH_INVALID",
                             "message": "sign-in challenge or signature invalid"}


def test_wrong_purpose_payload_is_rejected(client, owner_conn, server_keys):
    """Domain separation. A signature made for the owner
    sign-in purpose must not authenticate a peer sign-in, or one leaked owner-session
    signature would mint peer tokens and vice versa."""
    peer = make_peer(owner_conn, "purpose")

    wrong_nonce = get_challenge(client, peer["peer_id"])
    wrong = post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]),
        "nonce": wrong_nonce,
        "signature_b64": signin_signature(peer["seed"], peer["peer_id"], wrong_nonce,
                                          purpose=authconfig.OWNER_SIGNIN_PURPOSE,
                                          aud="dashboard"),
    })
    assert wrong.status_code == 401, wrong.content

    # CONTROL: same peer, same key, same route, fresh nonce — only `purpose` (and the
    # `aud` that travels with it) differs. This is what makes the 401 above evidence
    # about domain separation rather than about the peer or the key.
    right_nonce = get_challenge(client, peer["peer_id"])
    right = post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]),
        "nonce": right_nonce,
        "signature_b64": signin_signature(peer["seed"], peer["peer_id"], right_nonce),
    })
    assert right.status_code == 200, right.content


def test_sign_in_failures_are_indistinguishable(client, owner_conn, server_keys):
    """Every failure path returns the same 401 body. A
    different message per cause turns this route into an oracle for peer existence and
    key state."""
    peer = make_peer(owner_conn, "uniform")
    keyless = make_peer(owner_conn, "keyless", with_key=False)
    other = make_peer(owner_conn, "other")

    nonce = get_challenge(client, peer["peer_id"])
    cases = {
        # wrong signer: a valid signature over the right payload, wrong key
        "wrong_key": {"peer_id": str(peer["peer_id"]), "nonce": nonce,
                      "signature_b64": signin_signature(other["seed"], peer["peer_id"], nonce)},
        # nonce that was never issued
        "unknown_nonce": {"peer_id": str(peer["peer_id"]), "nonce": P.b64u(os.urandom(32)),
                          "signature_b64": signin_signature(peer["seed"], peer["peer_id"],
                                                            P.b64u(os.urandom(32)))},
        # peer with no active key at all
        "no_active_key": {"peer_id": str(keyless["peer_id"]),
                          "nonce": get_challenge(client, keyless["peer_id"]),
                          "signature_b64": P.b64u(bytes(64))},
        # unknown peer entirely
        "unknown_peer": {"peer_id": str(uuid.uuid4()), "nonce": P.b64u(os.urandom(32)),
                         "signature_b64": P.b64u(bytes(64))},
    }
    seen = {}
    for label, payload in cases.items():
        response = post(client, "/v1/auth/sign-in", payload)
        seen[label] = (response.status_code, json.dumps(response.json(), sort_keys=True))
    assert len(set(seen.values())) == 1, f"responses differ by cause: {seen}"
    assert all(v[0] == 401 for v in seen.values()), seen

    # CONTROL: the route can succeed. Four identical 401s would also be what a route
    # that rejects EVERYTHING looks like.
    good_nonce = get_challenge(client, peer["peer_id"])
    assert post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]), "nonce": good_nonce,
        "signature_b64": signin_signature(peer["seed"], peer["peer_id"], good_nonce),
    }).status_code == 200


def test_a_failed_sign_in_still_burns_its_nonce(client, owner_conn, server_keys):
    """The consume runs first, before the key and the signature are
    looked at, so a request that then fails has used its nonce up: the same nonce with a
    correct signature is refused. A server that checked the signature before consuming would
    let an attacker probe signatures against one live challenge."""
    peer = make_peer(owner_conn, "burn")
    nonce = get_challenge(client, peer["peer_id"])
    bad = post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]), "nonce": nonce,
        "signature_b64": P.b64u(bytes(64))})
    assert bad.status_code == 401, bad.content
    with owner_conn.cursor() as cur:
        cur.execute("SELECT consumed_at FROM auth_challenges WHERE peer_id=%s AND nonce=%s",
                    (peer["peer_id"], nonce))
        assert cur.fetchone()[0] is not None, "the bad signature did not consume the nonce"

    good = post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]), "nonce": nonce,
        "signature_b64": signin_signature(peer["seed"], peer["peer_id"], nonce)})
    assert good.status_code == 401
    assert good.json() == {"error": "E_AUTH_INVALID",
                           "message": "sign-in challenge or signature invalid"}

    # CONTROL: the same signature over a FRESH nonce signs in, so the 401 above is the burn.
    fresh = get_challenge(client, peer["peer_id"])
    assert post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]), "nonce": fresh,
        "signature_b64": signin_signature(peer["seed"], peer["peer_id"], fresh),
    }).status_code == 200


def test_expired_challenge_is_rejected(client, owner_conn, server_keys):
    """The consume predicate includes `expires_at > NOW()`, so TTL is enforced in the
    same atomic UPDATE as single-use."""
    peer = make_peer(owner_conn, "ttl")
    stale_nonce = get_challenge(client, peer["peer_id"])
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE auth_challenges SET expires_at = NOW() - interval '1 second' "
                    "WHERE peer_id=%s AND nonce=%s", (peer["peer_id"], stale_nonce))
    owner_conn.commit()

    stale = post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]), "nonce": stale_nonce,
        "signature_b64": signin_signature(peer["seed"], peer["peer_id"], stale_nonce)})
    assert stale.status_code == 401, stale.content

    # CONTROL: an unexpired challenge for the same peer and key succeeds, so the 401
    # is about expiry rather than about this peer's signature ever working.
    fresh_nonce = get_challenge(client, peer["peer_id"])
    assert post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]), "nonce": fresh_nonce,
        "signature_b64": signin_signature(peer["seed"], peer["peer_id"], fresh_nonce),
    }).status_code == 200


def test_sign_in_without_a_signing_key_is_503_and_does_not_burn_the_nonce(
        client, owner_conn, server_keys):
    """E_REDEEM_FAILED / 503 is checked before the consume."""
    peer = make_peer(owner_conn, "nokey")
    nonce = get_challenge(client, peer["peer_id"])
    signature = signin_signature(peer["seed"], peer["peer_id"], nonce)

    with authconfig.override_config(jwt_signing_key_pem=None):
        response = post(client, "/v1/auth/sign-in", {
            "peer_id": str(peer["peer_id"]), "nonce": nonce, "signature_b64": signature})
    assert response.status_code == 503
    assert response.json()["error"] == "E_REDEEM_FAILED"

    # CONTROL: the nonce survived the 503, so the same request succeeds once the key
    # is back. A server that consumed first would have locked this peer out for the
    # whole outage.
    assert post(client, "/v1/auth/sign-in", {
        "peer_id": str(peer["peer_id"]), "nonce": nonce, "signature_b64": signature,
    }).status_code == 200


# -------------------------------------------------------------------- refresh route

def seed_refresh_token(conn, peer: dict, *, kid: str | None = None,
                       expires_in_days: int = 30, revoked: bool = False) -> tuple[str, uuid.UUID]:
    raw = P.b64u(os.urandom(32))
    lineage = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refresh_tokens (token_hash, peer_id, owner_id, expires_at, "
            "issued_under_kid, lineage_id, revoked_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (hashlib.sha256(raw.encode()).digest(), peer["peer_id"], peer["owner_id"],
             dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_in_days),
             kid, lineage,
             dt.datetime.now(dt.timezone.utc) if revoked else None))
    conn.commit()
    return raw, lineage


def test_refresh_rotates_and_the_consumed_token_dies(client, owner_conn, server_keys):
    peer = make_peer(owner_conn, "refresh", scopes=["skills:declare"])
    token, lineage = seed_refresh_token(owner_conn, peer, kid=peer["kid"])

    first = post(client, "/v1/auth/refresh", {"refresh_token": token})
    assert first.status_code == 200, first.content
    body = first.json()
    assert sorted(body) == ["jwt", "jwt_expires_at", "refresh_token"]
    assert body["refresh_token"] != token, "the token must actually rotate"

    claims = tokens.verify(body["jwt"], [server_keys["public_pem"]], ISSUER)
    assert claims["sub"] == str(peer["owner_id"]) and claims["act"] == {"sub": str(peer["peer_id"])}
    assert claims["lin"] == str(lineage), (
        "migration 031: the successor INHERITS the lineage — a fresh one would strand "
        "this session's key-publish re-anchor")
    assert claims["scopes"] == ["skills:declare"], (
        "a rotation that minted an empty vector would decay every token to scope-free")
    assert from_did_key(claims["cnf"]) == peer["pubkey"], (
        "TASK-26: cnf is re-derived on rotation, or a peer could shed its DPoP binding")

    # NEGATIVE: the consumed token is revoked in the same transaction.
    replay = post(client, "/v1/auth/refresh", {"refresh_token": token})
    assert replay.status_code == 401
    assert replay.json()["error"] == "E_REFRESH_TOKEN_INVALID"
    # CONTROL: the SUCCESSOR still works, so the 401 is about the consumed token and
    # not about the route or this peer being broken.
    assert post(client, "/v1/auth/refresh",
                {"refresh_token": body["refresh_token"]}).status_code == 200


def test_refresh_dies_with_the_peer_key(client, owner_conn, server_keys):
    """The JOIN on peer_keys is the revocation check: revoke
    the key and the whole rotation chain stops."""
    peer = make_peer(owner_conn, "revoke")
    token, _ = seed_refresh_token(owner_conn, peer, kid=peer["kid"])

    # CONTROL first, while the key is live: this exact token rotates fine.
    live = post(client, "/v1/auth/refresh", {"refresh_token": token})
    assert live.status_code == 200, live.content
    successor = live.json()["refresh_token"]

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE peer_keys SET active = FALSE, revoked_at = NOW() "
                    "WHERE peer_id = %s", (peer["peer_id"],))
    owner_conn.commit()

    dead = post(client, "/v1/auth/refresh", {"refresh_token": successor})
    assert dead.status_code == 401, dead.content
    assert dead.json() == {"error": "E_REFRESH_TOKEN_INVALID",
                           "message": "refresh token not found or peer key revoked"}


def test_refresh_rejects_expired_and_revoked_tokens(client, owner_conn, server_keys):
    peer = make_peer(owner_conn, "expiry")
    expired, _ = seed_refresh_token(owner_conn, peer, kid=peer["kid"], expires_in_days=-1)
    revoked, _ = seed_refresh_token(owner_conn, peer, kid=peer["kid"], revoked=True)
    good, _ = seed_refresh_token(owner_conn, peer, kid=peer["kid"])

    assert post(client, "/v1/auth/refresh", {"refresh_token": expired}).json()["message"] \
        == "refresh token expired"
    assert post(client, "/v1/auth/refresh", {"refresh_token": revoked}).json()["message"] \
        == "refresh token revoked"
    assert post(client, "/v1/auth/refresh", {"refresh_token": "not-a-token"}).status_code == 401
    # CONTROL: a live token on the same peer rotates, so the three rejections above are
    # about the token state rather than about this fixture never having worked.
    assert post(client, "/v1/auth/refresh", {"refresh_token": good}).status_code == 200


# ---------------------------------------------------------------------- rate limiting

def test_rate_limit_is_per_peer_and_uniform(client, owner_conn, monkeypatch, server_keys):
    """The per-peer bucket is taken before any DB work and
    uniformly for peers that exist and peers that do not, so a 429 leaks nothing.

    The peer bucket is the production size with NO refill (issue #451): it refills at
    0.2/sec, and on a loaded gate box the 10-call burst took 7.6 s, long enough to earn the
    11th call back, so the 11th got 200 (mg446c)."""
    monkeypatch.setattr(ratelimit, "sig_auth_peer_limiter",
                        ratelimit.TokenBucketLimiter(ratelimit.SIG_AUTH_BURST, 0.0))
    peer = make_peer(owner_conn, "ratelimit")
    burst = ratelimit.SIG_AUTH_BURST

    # CONTROL: the whole burst is allowed. Without this a "429 on request N+1" would
    # also be green if the route 429'd from the first call.
    for i in range(burst):
        assert post(client, "/v1/auth/challenge",
                    {"peer_id": str(peer["peer_id"])}).status_code == 200, f"call {i}"

    limited = post(client, "/v1/auth/challenge", {"peer_id": str(peer["peer_id"])})
    assert limited.status_code == 429
    assert limited.json() == {"error": "E_RATE_LIMITED",
                              "message": "rate limit exceeded; retry later"}

    # The bucket is keyed on the peer, not global: a DIFFERENT peer is unaffected.
    other = make_peer(owner_conn, "ratelimit-other")
    assert post(client, "/v1/auth/challenge",
                {"peer_id": str(other["peer_id"])}).status_code == 200

    # ...and an UNKNOWN peer is limited on the same schedule, so the limiter adds no
    # existence oracle of its own.
    ghost = uuid.uuid4()
    for _ in range(burst):
        assert post(client, "/v1/auth/challenge", {"peer_id": str(ghost)}).status_code == 200
    assert post(client, "/v1/auth/challenge", {"peer_id": str(ghost)}).status_code == 429


def test_forged_forwarding_header_cannot_pick_a_fresh_limiter_key(client, monkeypatch,
                                                                  server_keys):
    """Proxy headers count only when the operator asserts a
    trusted proxy. Otherwise a caller rotates X-Forwarded-For and the IP bucket stops
    existing.

    The IP bucket is swapped for a tiny one with NO refill: the production bucket is 30
    deep and refills at 1/sec, so draining it over HTTP is a race against its own
    refill, and a test that loses that race reports "not rate limited" when the limiter
    is fine. Capacity is the only variable this test cares about."""
    tiny = ratelimit.TokenBucketLimiter(2, 0.0)
    monkeypatch.setattr(ratelimit, "sig_auth_ip_limiter", tiny)
    monkeypatch.setattr(ratelimit, "sig_auth_peer_limiter",
                        ratelimit.TokenBucketLimiter(10_000, 0.0))

    def challenge(peer_id, **extra):
        return client.post("/v1/auth/challenge", data=json.dumps({"peer_id": str(peer_id)}),
                           content_type="application/json", **extra)

    # CONTROL: the bucket's whole capacity is allowed, each under a DIFFERENT peer_id,
    # so only the IP bucket can be what stops the next one.
    for _ in range(2):
        assert challenge(uuid.uuid4()).status_code == 200
    assert challenge(uuid.uuid4()).status_code == 429

    forged = challenge(uuid.uuid4(), HTTP_X_FORWARDED_FOR="203.0.113.7")
    assert forged.status_code == 429, "a forged XFF must not mint a new bucket"

    # CONTROL: the very same header IS honoured once the operator opts in — which is
    # what proves the 429 above came from the trust gate rather than from this server
    # ignoring the header everywhere (a bug that would look identical here).
    with authconfig.override_config(behind_trusted_proxy=True):
        trusted = challenge(uuid.uuid4(), HTTP_X_FORWARDED_FOR="203.0.113.7")
    assert trusted.status_code == 200, trusted.content


# ------------------------------------------------------------------- request parsing

def test_malformed_bodies_are_refused_before_any_database_work(client, server_keys):
    syntax = client.post("/v1/auth/challenge", data=b"{not json",
                         content_type="application/json")
    assert syntax.status_code == 400, "a JSON syntax error is 400"
    shape = post(client, "/v1/auth/challenge", {"peer_id": 12345})
    assert shape.status_code == 422, "well-formed JSON that does not fit the struct is 422"
    not_a_uuid = post(client, "/v1/auth/challenge", {"peer_id": "nope"})
    assert not_a_uuid.status_code == 422
    # CONTROL: a well-formed body on the same route succeeds.
    assert post(client, "/v1/auth/challenge", {"peer_id": str(uuid.uuid4())}).status_code == 200


def test_routes_reject_the_wrong_method(client, server_keys):
    assert client.get("/v1/auth/sign-in").status_code == 405
    assert client.post("/.well-known/jwks.json").status_code == 405
    # CONTROL: the right method on each.
    assert client.get("/.well-known/jwks.json").status_code == 200
    assert post(client, "/v1/auth/sign-in", {}).status_code == 422


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
