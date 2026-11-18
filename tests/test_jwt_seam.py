"""The JWT/DPoP configuration seam: ONE configuration drives mint and verify.

THE DEFECT THIS FILE IS PROVED AGAINST (divergence 015). The minter and verifiers read
different key and issuer settings, including values settings.py never defined. In a real
deploy /v1/auth/sign-in minted tokens that every peer route answered with 500 E_INTERNAL.
Every route test was green, because every route test injected its own verify key with
override_settings: none of them ever took a token the server minted and presented it to a
route the server serves.

The subprocess tests below do exactly that, from the environment alone. The in-process
tests pin each DPoP rule the HTTP verifier now enforces, and the parser rules underneath.

House rules (docs/V1-DESIGN-RULES.md): every refusal has its positive control in the SAME
test, differing in the one variable under test (C1); nothing here trusts an empty
response or a missing failure to mean a check ran (C6).

Run:  .venv/Scripts/python.exe -m pytest tests/test_jwt_seam.py -q
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
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
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

import jwt as pyjwt  # noqa: E402
from channels.testing import WebsocketCommunicator  # noqa: E402
from django.core.handlers.asgi import ASGIRequest  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.authapi import ratelimit, tokens  # noqa: E402
from apps.authapi.didkey import to_did_key  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.peers import auth as peer_auth  # noqa: E402
from apps.wsgw import auth as ws_auth  # noqa: E402
from apps.wsgw.constants import CLOSE_INTERNAL_ERROR, NEGOTIATED_PROTOCOL, WS_PATH  # noqa: E402
from apps.wsgw.consumer import HubConfig, MeshyWebSocketConsumer, config_from_env  # noqa: E402
from apps.wsgw.presence import RecordingPresenceBackend  # noqa: E402
from apps.wsgw.connection_manager import ConnectionManager  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN", DSN_DEFAULT)
ISSUER = "meshy-seam-test"
CHILD = ROOT / "tests" / "fixtures" / "jwt_seam_child.py"
PRESENCE = "/v1/peers/me/presence"
INBOX = "/v1/mesh/inbox"


# --------------------------------------------------------------------------- helpers

def keypair() -> tuple[bytes, bytes]:
    """Return a PKCS#8 private PEM and its SPKI public PEM."""
    sk = Ed25519PrivateKey.generate()
    return (
        sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                         serialization.NoEncryption()),
        sk.public_key().public_bytes(serialization.Encoding.PEM,
                                     serialization.PublicFormat.SubjectPublicKeyInfo),
    )


def make_peer(conn, tag: str) -> dict:
    """An owner, an active peer and its active Ed25519 key, the rows sign-in reads."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    seed = os.urandom(32)
    pubkey = P.public_key_of(seed)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email) VALUES (%s, %s)",
                    (owner_id, f"seam-{tag}-{owner_id.hex[:8]}@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"seam-{tag}-{peer_id.hex[:8]}", f"seam {tag}"))
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key) VALUES (%s,%s,%s)",
                    (peer_id, P.kid_for_peer_key(pubkey), P.b64u(pubkey)))
    grant_mesh_read(conn, peer_id, owner_id)
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id, "seed": seed,
            "did_key": to_did_key(pubkey)}


MESH_NS = "shared/mesh-chat"


def grant_mesh_read(conn, peer_id, owner_id) -> None:
    """Read on shared/mesh-chat, which `GET /v1/mesh/inbox` requires since divergence 018.

    The inbox is this file's example of a peer READ route; the seam under test is the
    token, not the namespace gate, so every peer here holds the grant a real node holds.
    The namespace owner must be a dev-plane owner (sql/042 enforce_ns_dev_only) and this
    file's owners are product-plane, so an unclaimed namespace is claimed by a dev-plane
    peer of its own and the seam peer is GRANTED read, as a fleet node is."""
    with conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace"
                    " WHERE namespace = %s", (MESH_NS,))
        row = cur.fetchone()
        if row is None:
            ns_owner, ns_peer = uuid.uuid4(), uuid.uuid4()
            cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                        " VALUES (%s,%s,'dev',%s)",
                        (ns_owner, f"seam-ns-{ns_owner.hex[:8]}@example.test", uuid.uuid4()))
            cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                        (ns_peer, ns_owner, f"seam-ns-{ns_peer.hex[:8]}"))
            cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                        " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                        " VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                        (MESH_NS, ns_peer, ns_owner))
            row = (ns_peer, ns_owner)
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id)"
                    " VALUES (%s,%s,%s,'read',%s,%s)",
                    (MESH_NS, peer_id, owner_id, row[0], row[1]))


@pytest.fixture()
def owner_conn():
    """Owner role: seeds fixtures only. Never the connection a request is served on."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture(autouse=True)
def _fresh_limiters():
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


# ============================================================================
# 1. THE SEAM, deploy-shaped: a subprocess configured by environment ONLY
# ============================================================================

def _deploy_env(tmp: Path, peer: dict, **extra: str) -> dict:
    """The parent's environment minus every JWT/DPoP/test-harness variable, plus exactly
    what a deploy sets. MESHYCHAT_DPOP_ENFORCE is REMOVED, so the child sees the production
    default rather than the harness's (tests/conftest.py)."""
    sk_pem, pk_pem = keypair()
    (tmp / "jwt-private.pem").write_bytes(sk_pem)
    (tmp / "jwt-public.pem").write_bytes(pk_pem)
    dropped = ("MESHY_JWT_", "MESHYCHAT_JWT_", "MESHYCHAT_DPOP_", "PYTEST_",
               "MESHY_TEST_DSN", "MESHY_ALLOW_TEST_DSN")
    env = {k: v for k, v in os.environ.items() if not k.startswith(dropped)}
    env.update({
        "DJANGO_SETTINGS_MODULE": "meshychat.settings",
        "MESHY_ALLOWED_HOSTS": "testserver",
        "MESHY_DSN": os.environ.get("MESHY_DSN") or DSN,
        "MESHYCHAT_JWT_ISSUER": ISSUER,
        "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": str(tmp / "jwt-public.pem"),
        # MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH alone; MESHYCHAT_JWT_SIGNING_KEY_PEM_PATH is
        # deliberately not set.
        "MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": str(tmp / "jwt-private.pem"),
        "SEAM_PEER_ID": str(peer["peer_id"]),
        "SEAM_PEER_SEED_HEX": peer["seed"].hex(),
        "SEAM_PEER_DID_KEY": peer["did_key"],
    })
    env.update(extra)
    return env


def _run_child(env: dict) -> dict:
    proc = subprocess.run([sys.executable, str(CHILD)], env=env, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("SEAM_RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (
        f"child exit {proc.returncode}; stdout tail:\n{proc.stdout[-1500:]}\n"
        f"stderr tail:\n{proc.stderr[-3000:]}")
    return json.loads(lines[0][len("SEAM_RESULT "):])


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    """One child run shared by the assertions below: each is a separate test so a failure
    names the property that broke, not just "the seam"."""
    with psycopg.connect(DSN) as conn:
        peer = make_peer(conn, "deploy")
    return peer, _run_child(_deploy_env(tmp_path_factory.mktemp("deploy"), peer))


def test_a_token_minted_by_sign_in_opens_a_peer_route_with_no_injected_keys(deploy):
    """THE seam. POST /v1/auth/sign-in through the real urlconf, and THAT token, unmodified,
    on GET /v1/mesh/inbox. Before divergence 015, sign-in returned 503 because it did not read
    MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH; with the older signing-key spelling it minted a token,
    and every peer route answered 500 E_INTERNAL because verifiers read undefined settings.

    The flipped-byte request is the negative control, and it differs from the accepted one
    in ONE byte: a verifier that accepted anything would pass the first half alone, and
    one that refused everything would pass the second half alone."""
    peer, out = deploy
    assert out["challenge"]["status"] == 200, out
    assert out["sign_in"]["status"] == 200, out
    assert out["claims"]["iss"] == ISSUER and out["claims"]["aud"] == "peer", out
    assert out["claims"]["act_sub"] == str(peer["peer_id"]), out
    assert out["inbox"] == {"status": 200, "error": None}, out
    assert out["inbox_flipped_byte"] == {"status": 401, "error": "E_AUTH_INVALID"}, out


def test_mint_http_verify_and_ws_verify_read_one_configuration(deploy):
    """One issuer and one key, whoever asks. Distinct minter and WS issuer defaults rejected
    minted tokens."""
    _peer, out = deploy
    config = out["config"]
    assert config["settings_issuer"] == config["minter_issuer"] == config["ws_issuer"] == ISSUER
    assert config["settings_has_public_key"] and config["minter_can_sign"], config
    assert config["settings_key_is_minters_key"] and config["ws_key_is_settings_key"], config


def test_dpop_is_enforced_on_writes_by_default_in_a_deploy(deploy):
    """MESHYCHAT_DPOP_ENFORCE absent from the child's environment means ON.

    Sign-in binds `cnf` to the peer's own key, so the SAME token
    writes only with a proof that key signed. The three requests differ only in the DPoP
    header: none -> 403, fresh -> 200, the same proof again -> 403 (the jti is spent)."""
    peer, out = deploy
    assert out["config"]["settings_dpop_enforce"] is True, out["config"]
    assert out["config"]["ws_dpop_enforce"] is True, out["config"]
    assert out["claims"]["cnf"] == out["peer_did_key"] == peer["did_key"], out
    assert out["write_without_proof"] == {"status": 403, "error": "E_FORBIDDEN"}, out
    assert out["write_with_proof"] == {"status": 200, "error": None}, out
    assert out["write_replayed_proof"] == {"status": 403, "error": "E_FORBIDDEN"}, out


def test_the_kill_switch_turns_dpop_off_in_a_deploy(tmp_path, owner_conn):
    """The control for the default above: the same deploy with MESHYCHAT_DPOP_ENFORCE=false
    accepts the proof-less write. Without it, "403 by default" could be a route that
    refuses every write."""
    peer = make_peer(owner_conn, "killswitch")
    out = _run_child(_deploy_env(tmp_path, peer, MESHYCHAT_DPOP_ENFORCE="false"))
    assert out["config"]["settings_dpop_enforce"] is False, out["config"]
    assert out["config"]["ws_dpop_enforce"] is False, out["config"]
    assert out["write_without_proof"] == {"status": 200, "error": None}, out


# ============================================================================
# 2. DPoP on HTTP peer writes, in-process
# ============================================================================

@pytest.fixture()
def server():
    sk_pem, pk_pem = keypair()
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=pk_pem, MESHY_JWT_ISSUER=ISSUER,
                           MESHY_DPOP_ENFORCE=True, MESHY_DSN=DSN):
        yield {"sk": sk_pem, "pk": pk_pem}


def mint(server, peer, *, cnf: str | None, ttl: int = 300) -> str:
    """Build a peer token with the minter's own code (apps/authapi/tokens.py), sign-in shaped:
    sub = owner and act.sub = peer."""
    now = tokens.now_unix()
    return tokens.sign(tokens.build_claims(
        sub=peer["owner_id"], iss=ISSUER, iat=now, exp=now + ttl, scopes=[], aud="peer",
        cnf=cnf, act_sub=peer["peer_id"]), server["sk"])


def write(client, token: str, wire: str | None = None, path: str = PRESENCE):
    extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    if wire is not None:
        extra["HTTP_DPOP"] = wire
    return client.post(path, data="{}", content_type="application/json", **extra)


def outcome(response) -> tuple[int, str | None]:
    return response.status_code, response.json().get("error")


def test_a_bearer_write_is_refused_under_enforcement_and_allowed_by_the_kill_switch(
        server, owner_conn):
    """TASK-46: a token with NO cnf cannot write under enforcement,
    even carrying a proof its peer really signed. Same token, same request, same proof
    shape, enforcement off -> 200: the refusal is the flag, not the route."""
    peer = make_peer(owner_conn, "bearer")
    token = mint(server, peer, cnf=None)
    client = Client()
    wire = P.dpop_wire(peer["seed"], "POST", PRESENCE, token)
    assert outcome(write(client, token, wire)) == (403, "E_FORBIDDEN")
    with override_settings(MESHY_DPOP_ENFORCE=False):
        fresh = P.dpop_wire(peer["seed"], "POST", PRESENCE, token)
        assert outcome(write(client, token, fresh)) == (200, None)


def test_a_cnf_bound_write_needs_a_proof_and_each_proof_is_spent_once(server, owner_conn):
    peer = make_peer(owner_conn, "spend")
    token = mint(server, peer, cnf=peer["did_key"])
    client = Client()
    assert outcome(write(client, token)) == (403, "E_FORBIDDEN"), "no DPoP header"
    wire = P.dpop_wire(peer["seed"], "POST", PRESENCE, token)
    assert outcome(write(client, token, wire)) == (200, None), "a valid proof"
    assert outcome(write(client, token, wire)) == (403, "E_FORBIDDEN"), "the same proof again"
    # Control for the replay refusal: a FRESH proof still works, so the third request was
    # refused for its jti and not because the peer is now locked out.
    fresh = P.dpop_wire(peer["seed"], "POST", PRESENCE, token)
    assert outcome(write(client, token, fresh)) == (200, None)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dpop_jti_seen WHERE peer_id = %s", (peer["peer_id"],))
        assert cur.fetchone()[0] == 2, "one ledger row per ACCEPTED proof"


def test_a_proof_bound_to_anything_else_is_refused(server, owner_conn):
    """Method, target, token, key and window are each part of the proof.
    The last request is the control - a correct proof over the same token and route."""
    peer = make_peer(owner_conn, "binding")
    token = mint(server, peer, cnf=peer["did_key"])
    # A different exp, because Ed25519 is deterministic: the same claims in the same
    # second sign to the SAME token, and the "wrong token" case would test nothing.
    other_token = mint(server, peer, cnf=peer["did_key"], ttl=301)
    assert other_token != token
    stranger_seed = os.urandom(32)
    client = Client()
    # Each proof is built just before its request, so the clock distance to the server's
    # check is the offset below plus milliseconds: +/-125 s against a 120 s window stays
    # outside it however slowly the requests before it ran.
    wrong = {
        "method": lambda: P.dpop_wire(peer["seed"], "PUT", PRESENCE, token),
        "target": lambda: P.dpop_wire(peer["seed"], "POST", "/v1/peers/me/keys", token),
        "token": lambda: P.dpop_wire(peer["seed"], "POST", PRESENCE, other_token),
        "key": lambda: P.dpop_wire(stranger_seed, "POST", PRESENCE, token),
        "stale": lambda: P.dpop_wire(peer["seed"], "POST", PRESENCE, token,
                                     timestamp=int(time.time()) - 125),
        "future": lambda: P.dpop_wire(peer["seed"], "POST", PRESENCE, token,
                                      timestamp=int(time.time()) + 125),
        "malformed": lambda: "not-a-dpop-proof",
        "non-ascii": lambda: "\u00e9",
    }
    got = {name: outcome(write(client, token, build())) for name, build in wrong.items()}
    assert got == {name: (403, "E_FORBIDDEN") for name in wrong}, got
    assert outcome(write(client, token, P.dpop_wire(peer["seed"], "POST", PRESENCE, token))) \
        == (200, None)


def test_reads_are_never_gated_by_dpop(server, owner_conn):
    """The proof binds WRITES. The node client's wait loop sends none on its polling GETs, so
    gating a read would lock every node out.
    Paired with the same token refused on a write, so the 200 is not a verifier that
    ignores enforcement altogether."""
    peer = make_peer(owner_conn, "read")
    token = mint(server, peer, cnf=peer["did_key"])
    client = Client()
    assert client.get(INBOX, HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 200
    assert outcome(write(client, token)) == (403, "E_FORBIDDEN")


def test_an_absent_enforcement_setting_means_enforced(server, owner_conn):
    """Only a literal False turns enforcement off. A settings module that never defined
    MESHY_DPOP_ENFORCE (apps/authapi/testsettings.py is one) gets the default, ON."""
    peer = make_peer(owner_conn, "absent")
    token = mint(server, peer, cnf=None)
    client = Client()
    with override_settings():
        del dj_settings.MESHY_DPOP_ENFORCE
        assert not hasattr(dj_settings, "MESHY_DPOP_ENFORCE")
        assert outcome(write(client, token)) == (403, "E_FORBIDDEN")
    with override_settings(MESHY_DPOP_ENFORCE=False):
        assert outcome(write(client, token)) == (200, None)


def test_the_proof_target_is_the_raw_path_the_client_sent():
    """The proof target is the percent-encoded path, with no query. Django's
    `request.path` is DECODED, so for any %-escape it is not the string the client signed.
    Built as a real ASGIRequest (the deploy path); the WSGI fallback is the control."""
    scope = {"type": "http", "method": "POST", "path": "/v1/skills/a b",
             "raw_path": b"/v1/skills/a%20b", "query_string": b"x=1", "headers": [],
             "root_path": ""}
    request = ASGIRequest(scope, io.BytesIO(b""))
    assert request.path == "/v1/skills/a b"
    assert peer_auth.request_target(request) == "/v1/skills/a%20b"
    no_raw = ASGIRequest({**scope, "raw_path": None}, io.BytesIO(b""))
    assert peer_auth.request_target(no_raw) == "/v1/skills/a b"


# ============================================================================
# 3. The parser both sides share (apps/authapi/config.py:jwt_material_from_env)
# ============================================================================

def _pems(tmp: Path, name: str) -> tuple[str, str]:
    sk_pem, pk_pem = keypair()
    (tmp / f"{name}-private.pem").write_bytes(sk_pem)
    (tmp / f"{name}-public.pem").write_bytes(pk_pem)
    return str(tmp / f"{name}-private.pem"), str(tmp / f"{name}-public.pem")


def test_dpop_enforcement_defaults_on_and_only_an_explicit_false_turns_it_off():
    """The default is true; only recognized false values turn enforcement off."""
    read = authconfig.jwt_material_from_env
    assert read({}).dpop_enforce is True
    assert read({"MESHYCHAT_DPOP_ENFORCE": "  "}).dpop_enforce is True
    for off in ("false", "FALSE", "0", "no", "off"):
        assert read({"MESHYCHAT_DPOP_ENFORCE": off}).dpop_enforce is False, off
    for on in ("true", "1", "yes", "on"):
        assert read({"MESHYCHAT_DPOP_ENFORCE": on}).dpop_enforce is True, on
    with pytest.raises(authconfig.JwtConfigError):
        read({"MESHYCHAT_DPOP_ENFORCE": "flase"})


def test_the_issuer_has_no_default_anywhere():
    """The issuer is required and has no default: distinct minter and WS defaults disagreed."""
    assert authconfig.jwt_material_from_env({}).issuer is None
    assert authconfig.AuthConfig().jwt_issuer is None
    assert authconfig.AuthConfig.from_env({"MESHY_DSN": DSN}).jwt_issuer is None
    with override_settings(MESHY_JWT_ISSUER=None, MESHY_JWT_PUBLIC_KEY_PEM=None):
        assert config_from_env().jwt_issuer is None
    # Control: a configured issuer arrives, unchanged, at every reader.
    assert authconfig.AuthConfig.from_env(
        {"MESHY_DSN": DSN, "MESHYCHAT_JWT_ISSUER": "iss-x"}).jwt_issuer == "iss-x"
    with override_settings(MESHY_JWT_ISSUER="iss-x"):
        assert config_from_env().jwt_issuer == "iss-x"


def test_the_primary_private_key_path_setting_is_read(tmp_path):
    """Accept both private-key path setting names, and reject conflicting values, so one .env
    can drive both minting and verification."""
    sk, pk = _pems(tmp_path, "a")
    other_sk, _ = _pems(tmp_path, "b")
    read = authconfig.jwt_material_from_env
    primary_path = read({"MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": sk, "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": pk})
    legacy = read({"MESHYCHAT_JWT_SIGNING_KEY_PEM_PATH": sk, "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": pk})
    assert primary_path.signing_key_pem == legacy.signing_key_pem == Path(sk).read_bytes()
    assert read({"MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": sk, "MESHYCHAT_JWT_SIGNING_KEY_PEM_PATH": sk,
                 "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": pk}).signing_key_pem is not None
    with pytest.raises(authconfig.JwtConfigError):
        read({"MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": sk, "MESHYCHAT_JWT_SIGNING_KEY_PEM_PATH": other_sk,
              "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": pk})


def test_a_key_pair_that_cannot_verify_its_own_tokens_refuses_to_load(tmp_path):
    """Refuse a non-Ed25519 key or a private key that does not pair with the public key. A
    valid pair is the control."""
    sk_a, pk_a = _pems(tmp_path, "a")
    _sk_b, pk_b = _pems(tmp_path, "b")
    garbage = tmp_path / "garbage.pem"
    garbage.write_bytes(b"-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----\n")
    read = authconfig.jwt_material_from_env
    good = read({"MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": sk_a, "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": pk_a})
    assert good.signing_key_pem and good.public_key_pem
    refused = {
        "partner": {"MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": sk_a, "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": pk_b},
        "no verify key": {"MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": sk_a},
        "public garbage": {"MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": str(garbage)},
        "private garbage": {"MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": str(garbage),
                            "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": pk_a},
        "history garbage": {"MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": pk_a,
                            "MESHYCHAT_JWT_PUBLIC_KEY_HISTORY_PEM_PATHS": str(garbage)},
    }
    for name, env in refused.items():
        with pytest.raises(authconfig.JwtConfigError):
            read(env)
            pytest.fail(f"{name} loaded")


def test_the_minter_refuses_without_an_issuer_and_keeps_the_nonce(owner_conn):
    """No issuer is "cannot mint" (503), checked before the single-use challenge is
    consumed. The control signs in with the SAME nonce once an issuer is configured, which
    proves the 503 neither burned it nor came from a broken fixture."""
    peer = make_peer(owner_conn, "noiss")
    sk_pem, pk_pem = keypair()
    client = Client()
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"]), \
            authconfig.override_config(jwt_signing_key_pem=sk_pem, jwt_public_key_pem=pk_pem,
                                       dsn=DSN, jwt_issuer=None):
        r = client.post("/v1/auth/challenge", data=json.dumps({"peer_id": str(peer["peer_id"])}),
                        content_type="application/json")
        nonce = r.json()["nonce"]
        body = json.dumps({"peer_id": str(peer["peer_id"]), "nonce": nonce,
                           "signature_b64": P.b64u(P.sign(peer["seed"], P.jcs({
                               "aud": "peer", "nonce": nonce, "peer_id": str(peer["peer_id"]),
                               "purpose": authconfig.SIGNIN_PURPOSE})))})
        r = client.post("/v1/auth/sign-in", data=body, content_type="application/json")
        assert r.status_code == 503 and r.json()["error"] == "E_REDEEM_FAILED", r.content
        with authconfig.override_config(jwt_issuer=ISSUER):
            r = client.post("/v1/auth/sign-in", data=body, content_type="application/json")
            assert r.status_code == 200, r.content
            assert pyjwt.decode(r.json()["jwt"], pk_pem, algorithms=["EdDSA"],
                                options={"verify_aud": False})["iss"] == ISSUER


def test_refresh_refuses_without_an_issuer_before_touching_the_token():
    """The refresh route's copy of the same guard. The control is the SAME request with an
    issuer configured: it gets past the guard and is refused for the token (401), so the
    503 is the issuer guard and not a routing or key failure."""
    sk_pem, pk_pem = keypair()
    client = Client()
    body = json.dumps({"refresh_token": "not-a-real-refresh-token"})
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"]),             authconfig.override_config(jwt_signing_key_pem=sk_pem, jwt_public_key_pem=pk_pem,
                                       dsn=DSN, jwt_issuer=None):
        r = client.post("/v1/auth/refresh", data=body, content_type="application/json")
        assert (r.status_code, r.json()["error"]) == (503, "E_REDEEM_FAILED"), r.content
        with authconfig.override_config(jwt_issuer=ISSUER):
            r = client.post("/v1/auth/refresh", data=body, content_type="application/json")
            assert (r.status_code, r.json()["error"]) == (401, "E_REFRESH_TOKEN_INVALID")


# ============================================================================
# 4. The WS gateway reads the same configuration and fails closed without it
# ============================================================================

def test_the_ws_gateway_reads_the_verifier_settings():
    """The WS state receives the HTTP configuration's issuer, key and DPoP flag."""
    _sk, pk = keypair()
    with override_settings(MESHY_JWT_PUBLIC_KEY_PEM=pk, MESHY_JWT_ISSUER="iss-ws",
                           MESHY_DPOP_ENFORCE=True):
        hub = config_from_env()
        assert (hub.jwt_public_key_pem, hub.jwt_issuer, hub.dpop_enforce) == (pk, "iss-ws", True)
    with override_settings(MESHY_JWT_PUBLIC_KEY_PEM=pk, MESHY_JWT_ISSUER="iss-ws",
                           MESHY_DPOP_ENFORCE=False):
        assert config_from_env().dpop_enforce is False
    assert HubConfig(jwt_public_key_pem=pk, jwt_issuer="iss-ws").dpop_enforce is True


def test_an_unset_issuer_is_refused_not_skipped():
    """PyJWT treats issuer=None as "do not check the issuer". The first assertion is the
    instrument control: it shows that library behaviour is real, which is what makes the
    backstop in apps/wsgw/auth.py load-bearing rather than decorative."""
    sk, pk = keypair()
    now = int(time.time())
    foreign = pyjwt.encode({"sub": str(uuid.uuid4()), "iss": "someone-else", "iat": now,
                            "exp": now + 60, "aud": "peer"}, sk, algorithm="EdDSA")
    assert pyjwt.decode(foreign, pk, algorithms=["EdDSA"], issuer=None,
                        options={"verify_aud": False})["iss"] == "someone-else"
    with pytest.raises(ValueError):
        ws_auth.verify_peer_jwt(foreign, pk, None)
    with pytest.raises(pyjwt.InvalidIssuerError):
        ws_auth.verify_peer_jwt(foreign, pk, ISSUER)
    assert ws_auth.verify_peer_jwt(foreign, pk, "someone-else")["iss"] == "someone-else"


@pytest.mark.asyncio
async def test_an_unconfigured_ws_gateway_refuses_every_upgrade():
    """Same token, same socket, one variable: the configured issuer. Unset -> the upgrade
    is refused with 1011 (a server fault, like HTTP's 500) BEFORE verification runs.
    The database-touching gates are off so the control's accept depends only on the JWT."""
    sk, pk = keypair()
    now = int(time.time())
    token = pyjwt.encode({"sub": str(uuid.uuid4()), "iss": ISSUER, "iat": now,
                          "exp": now + 60, "aud": "peer", "scopes": []}, sk, algorithm="EdDSA")

    async def attempt(issuer):
        app = MeshyWebSocketConsumer.as_asgi(
            config=HubConfig(jwt_public_key_pem=pk, jwt_issuer=issuer, dpop_enforce=False,
                             check_peer_active=False, zero_rtt=False),
            manager=ConnectionManager(), presence=RecordingPresenceBackend())
        comm = WebsocketCommunicator(app, WS_PATH,
                                     subprotocols=[NEGOTIATED_PROTOCOL, f"jwt.{token}"])
        result = await comm.connect(timeout=5)
        await comm.disconnect()
        return result

    assert await attempt(None) == (False, CLOSE_INTERNAL_ERROR)
    connected, _ = await attempt(ISSUER)
    assert connected is True
