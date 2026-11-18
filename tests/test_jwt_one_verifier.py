"""One JWT verifier, one leeway, one required-claims set, on every entry point. STATUS B13.

THE DEFECT, measured on c5457ac. Five functions under apps/ verified the SAME token format
and each called PyJWT itself:

    apps/authapi/tokens.py     verify_token          60 s leeway (DEFAULT_LEEWAY_SECS)
    apps/peers/auth.py         verify_token          NO leeway   (every peer HTTP route)
    apps/wsgw/auth.py          verify_peer_jwt       NO leeway   (the WebSocket upgrade)
    apps/dashboard/auth.py     verify_dashboard_jwt  NO leeway   (the dashboard owner)
    apps/grants/owner_auth.py  _verify_dashboard_jwt 60 s, its own copy of the type rule

A token 30 s past `exp` should be accepted at every entry point; three of the five refused it.
A skew that small is ordinary between a node and this server; the effect of inconsistent
verification was a peer that could still refresh and read `/v1/grants` while its peer routes,
its socket, and its dashboard said 401.

WHAT IS PINNED, on every entry point, each negative beside its control in the same test
(docs/V1-DESIGN-RULES.md C1):
  * leeway: `exp` 30 s ago is ACCEPTED, `exp` 90 s ago is REFUSED. The margins sit either
    side of 60 s by 30 s, so a slow box cannot flip either;
  * issuer: the configured issuer is accepted, another issuer is refused;
  * required claims: a token without `exp`, and one without `iss`, are refused.
And structurally: no module under apps/ except apps/authapi/tokens.py decodes a JWT itself,
with a positive control that the detector does find the one decode that is allowed.

Run:  .venv/Scripts/python.exe -m pytest tests/test_jwt_one_verifier.py -q  (no database)
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from channels.testing import WebsocketCommunicator  # noqa: E402
from django.test import override_settings  # noqa: E402

from apps.authapi import tokens  # noqa: E402
from apps.dashboard import auth as dash_auth  # noqa: E402
from apps.grants import owner_auth  # noqa: E402
from apps.peers import auth as peer_auth  # noqa: E402
from apps.wsgw import auth as ws_auth  # noqa: E402
from apps.wsgw.connection_manager import ConnectionManager  # noqa: E402
from apps.wsgw.constants import NEGOTIATED_PROTOCOL, WS_PATH  # noqa: E402
from apps.wsgw.consumer import HubConfig, MeshyWebSocketConsumer  # noqa: E402
from apps.wsgw.presence import RecordingPresenceBackend  # noqa: E402

ISSUER = "one-verifier-test"

_SK = Ed25519PrivateKey.generate()
SK_PEM = _SK.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
PK_PEM = _SK.public_key().public_bytes(serialization.Encoding.PEM,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)

#: Inside and outside the 60 s leeway by 30 s each way.
WITHIN_LEEWAY = -30
BEYOND_LEEWAY = -90


def _token(aud: str, *, exp_offset: int | None = 600, iss: str | None = ISSUER) -> str:
    now = int(time.time())
    claims: dict = {"sub": str(uuid.uuid4()), "iat": now - 120, "scopes": [], "aud": aud}
    if exp_offset is not None:
        claims["exp"] = now + exp_offset
    if iss is not None:
        claims["iss"] = iss
    return pyjwt.encode(claims, SK_PEM, algorithm="EdDSA")


def _settings():
    # The settings value is a str in tests/test_grants.py and bytes in test_ws.py; both
    # are exercised across the suite, so both must verify here too.
    return override_settings(MESHY_JWT_PUBLIC_KEY_PEM=PK_PEM.decode("ascii"),
                             MESHY_JWT_ISSUER=ISSUER)


def _peer(token):
    with _settings():
        return peer_auth.verify_token(token)


def _dashboard(token):
    with _settings():
        return dash_auth.verify_dashboard_jwt(token)


def _grants(token):
    with _settings():
        return owner_auth._verify_dashboard_jwt(token)


#: Every function under apps/ that verifies a JWT, with the audience its callers expect.
ENTRY_POINTS = {
    "authapi.tokens.verify_token": (lambda t: tokens.verify_token(t, PK_PEM, ISSUER), "peer"),
    "peers.auth.verify_token": (_peer, "peer"),
    "wsgw.auth.verify_peer_jwt": (lambda t: ws_auth.verify_peer_jwt(t, PK_PEM, ISSUER), "peer"),
    "dashboard.auth.verify_dashboard_jwt": (_dashboard, "dashboard"),
    "grants.owner_auth._verify_dashboard_jwt": (_grants, "dashboard"),
}


def _refused(verify, token) -> Exception:
    with pytest.raises(Exception) as exc:
        verify(token)
    status = getattr(exc.value, "status", None)
    assert status in (None, 401), f"a bad token was mapped to {status}, not 401"
    return exc.value


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_the_leeway_is_sixty_seconds_on_every_entry_point(entry):
    verify, aud = ENTRY_POINTS[entry]
    # CONTROL: a live token verifies, so the refusal below is the expiry deciding.
    assert verify(_token(aud))["iss"] == ISSUER
    within = verify(_token(aud, exp_offset=WITHIN_LEEWAY))
    assert within["exp"] < time.time(), "the fixture did not mint an already-expired token"
    err = _refused(verify, _token(aud, exp_offset=BEYOND_LEEWAY))
    assert "expired" in str(err).lower(), f"{entry} refused for another reason: {err}"


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_the_issuer_is_pinned_on_every_entry_point(entry):
    verify, aud = ENTRY_POINTS[entry]
    assert verify(_token(aud))["iss"] == ISSUER                     # control
    err = _refused(verify, _token(aud, iss="someone-else"))
    assert "issuer" in str(err).lower(), f"{entry} refused for another reason: {err}"


@pytest.mark.parametrize("claim", ["exp", "iss"])
@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_exp_and_iss_are_required_on_every_entry_point(entry, claim):
    verify, aud = ENTRY_POINTS[entry]
    assert verify(_token(aud))["iss"] == ISSUER                     # control
    missing = {"exp": _token(aud, exp_offset=None), "iss": _token(aud, iss=None)}[claim]
    err = _refused(verify, missing)
    assert claim in str(err), f"{entry} refused a token without {claim} for another reason: {err}"


# ------------------------------------------------------------- the WebSocket upgrade

def _hub(manager: ConnectionManager):
    config = HubConfig(jwt_public_key_pem=PK_PEM, jwt_issuer=ISSUER, dpop_enforce=False,
                       check_peer_active=False, zero_rtt=False)
    return MeshyWebSocketConsumer.as_asgi(config=config, manager=manager,
                                         presence=RecordingPresenceBackend())


async def _upgrade(app, token):
    comm = WebsocketCommunicator(app, WS_PATH,
                                 subprotocols=[NEGOTIATED_PROTOCOL, f"jwt.{token}"])
    return comm, await comm.connect(timeout=5)


@pytest.mark.asyncio
async def test_the_upgrade_itself_applies_the_same_leeway_and_issuer():
    """The entry point as a client reaches it, not only the function under it.

    A token inside the leeway is ADMITTED and then closed by the expiry deadline at once:
    verification accepts it and the consumer computes the deadline as
    `max(0, expires_at - now)` with no leeway (apps/wsgw/consumer.py:401-407)."""
    manager = ConnectionManager()
    app = _hub(manager)

    live, (accepted, _) = await _upgrade(app, _token("peer"))
    assert accepted is True, "control: a live token was refused"
    assert json.loads(await live.receive_from(timeout=5))["type"] == "welcome"
    await live.disconnect()

    inside, (accepted, code) = await _upgrade(app, _token("peer", exp_offset=WITHIN_LEEWAY))
    assert accepted is True, f"a token 30 s past exp was refused at the upgrade ({code})"
    assert json.loads(await inside.receive_from(timeout=5))["type"] == "welcome"
    closed = await inside.receive_output(timeout=5)
    assert closed == {"type": "websocket.close", "code": 4401}, closed
    await inside.disconnect()

    for label, token in [("beyond the leeway", _token("peer", exp_offset=BEYOND_LEEWAY)),
                         ("another issuer", _token("peer", iss="someone-else")),
                         ("no exp", _token("peer", exp_offset=None))]:
        comm, (accepted, code) = await _upgrade(app, token)
        await comm.disconnect()
        assert (accepted, code) == (False, 4401), f"{label}: ({accepted}, {code})"


# ------------------------------------------------------------------ the structure

def _pyjwt_uses(source: str) -> list[str]:
    """Every use of PyJWT in `source` other than naming one of its EXCEPTION classes.

    It was a regex for `pyjwt.decode(` / `jwt.decode(`, which the review of c766b76 showed
    blind to `pyjwt.decode_complete(`, `jwt.PyJWT().decode(`, `from jwt import decode` and
    `jwt.api_jwt.decode(` (finding F8). So this reads the SYNTAX TREE: a module that imports
    the `jwt` package (or a submodule) may only name its `...Error` classes, which is all
    apps/wsgw/auth.py needs to re-raise a refusal as it always did. Anything else - a call,
    a class, the module passed around - is a use. Not seen: an import by string
    (`importlib.import_module("jwt")`); nothing under apps/ does that."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    aliases: set[str] = set()
    uses: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "jwt" or a.name.startswith("jwt."):
                    aliases.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and (
                node.module == "jwt" or node.module.startswith("jwt.")):
            uses += [f"from {node.module} import {a.name}" for a in node.names
                     if not a.name.endswith("Error")]
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in aliases:
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node:
                if not parent.attr.endswith("Error"):
                    uses.append(f"{node.id}.{parent.attr}")
            else:
                uses.append(node.id)
    return uses


def _pyjwt_sites() -> dict[str, list[str]]:
    out = {}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        uses = _pyjwt_uses(path.read_text(encoding="utf-8"))
        if uses:
            out[path.relative_to(ROOT).as_posix()] = uses
    return out


#: Each way to decode a JWT that the regex this replaced did not see, and the one it did.
PLANTED_DECODES = [
    "import jwt as pyjwt\nclaims = pyjwt.decode(token, key)",
    "import jwt as pyjwt\nclaims = pyjwt.decode_complete(token, key)",
    "import jwt\nclaims = jwt.PyJWT().decode(token, key)",
    "from jwt import decode\nclaims = decode(token, key)",
    "import jwt\nclaims = jwt.api_jwt.decode(token, key)",
    "import jwt.api_jwt\nclaims = jwt.api_jwt.decode(token, key)",
    "from jwt.api_jwt import decode_complete",
    "import jwt as j\nverify = j.decode",
    "import jwt\nlibrary = jwt",
]


def test_only_the_one_verifier_decodes_a_jwt():
    sites = _pyjwt_sites()
    # POSITIVE CONTROL: the detector sees the decode that IS allowed. A detector that
    # matched nothing would otherwise pass this test on every tree.
    assert "pyjwt.decode" in sites.get("apps/authapi/tokens.py", []), sites
    others = {p: u for p, u in sites.items() if p != "apps/authapi/tokens.py"}
    assert others == {}, f"PyJWT used outside apps/authapi/tokens.py: {others}"
    # A planted second decode IS seen, in every spelling (the detector can fail) ...
    missed = [p for p in PLANTED_DECODES if not _pyjwt_uses(p)]
    assert missed == [], f"the detector does not see: {missed}"
    # ... and naming an exception class is not a decode (apps/wsgw/auth.py re-raises them).
    assert _pyjwt_uses("import jwt as pyjwt\nraise pyjwt.InvalidTokenError('x')") == []


# ------------------------------------------------------------------ the issuer, unset

def _foreign(aud: str) -> str:
    """A live token the server key signed, under ANOTHER issuer."""
    return _token(aud, iss="someone-else")


@pytest.mark.parametrize("issuer", [None, ""])
def test_the_one_verifier_refuses_to_verify_without_an_issuer(issuer):
    """PyJWT given `issuer=None` SKIPS the issuer check, so a token from any issuer the key
    signed would pass. Every caller guarded against that itself; the review of c766b76
    (finding F4) showed the one verifier did not, so a caller that forgot the guard - or
    a new one - would have been silent. `MESHY_JWT_ISSUER` is None whenever
    `MESHYCHAT_JWT_ISSUER` is unset (meshychat/settings.py). CONTROL: the pinned issuer accepts
    its own token and refuses the foreign one.

    RED on c766b76: `verify_token(foreign, pem, None)` returned the foreign claims.
    Mutant killed: the issuer refusal removed from `tokens.verify`."""
    assert tokens.verify_token(_token("peer"), PK_PEM, ISSUER)["iss"] == ISSUER
    with pytest.raises(tokens.JwtError, match="issuer"):
        tokens.verify_token(_foreign("peer"), PK_PEM, ISSUER)
    for token in (_foreign("peer"), _token("peer")):
        with pytest.raises(tokens.JwtError, match="issuer"):
            tokens.verify_token(token, PK_PEM, issuer)
        with pytest.raises(tokens.JwtError, match="issuer"):
            tokens.verify(token, [PK_PEM], issuer)


HTTP_VERIFIERS = {
    "peers.auth.verify_token": (peer_auth.verify_token, "peer"),
    "dashboard.auth.verify_dashboard_jwt": (dash_auth.verify_dashboard_jwt, "dashboard"),
    "grants.owner_auth._verify_dashboard_jwt": (owner_auth._verify_dashboard_jwt, "dashboard"),
}


@pytest.mark.parametrize("issuer", [None, ""])
@pytest.mark.parametrize("entry", sorted(HTTP_VERIFIERS))
def test_an_unset_issuer_is_a_server_fault_on_every_http_verifier(entry, issuer):
    """Nothing to verify against is a SERVER fault (500), never a pass and never the
    caller's 401 (divergence 015). The WebSocket
    upgrade's 1011 is pinned in tests/test_jwt_seam.py. CONTROL: the same entry point with
    the issuer set verifies the same key's token.

    Mutants killed (review of c766b76, finding F4; each survived its suites): A6, A7 and
    A8, the `or not issuer` guard removed from the peer, dashboard and grants extractor."""
    fn, aud = HTTP_VERIFIERS[entry]
    with _settings():
        assert fn(_token(aud))["iss"] == ISSUER
    for token in (_foreign(aud), _token(aud)):
        with override_settings(MESHY_JWT_PUBLIC_KEY_PEM=PK_PEM.decode("ascii"),
                               MESHY_JWT_ISSUER=issuer):
            with pytest.raises(Exception) as exc:
                fn(token)
        assert getattr(exc.value, "status", None) == 500, f"{entry}: {exc.value!r}"


def test_there_is_one_leeway_and_one_required_claims_set():
    assert tokens.DEFAULT_LEEWAY_SECS == 60
    assert tuple(tokens.REQUIRED_CLAIMS) == ("exp", "iss")
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if path.name == "tokens.py" and path.parent.name == "authapi":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bleeway\s*=", text), f"{path} sets its own leeway"
