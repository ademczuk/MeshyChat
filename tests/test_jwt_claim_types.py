"""Every JWT verifier refuses a wrong-TYPED `exp`, not just the two that remembered to.

THE DEFECT (Kimi review, angle A finding 1; confirmed by reading every `pyjwt.decode` call
site under apps/). PyJWT's expiry check is `int(payload["exp"])`, which accepts the STRING
"99999999999" and truncates a FLOAT. The intended rule is that a wrong-typed spec claim counts
as absent, and because `exp` is required (see the CVE-2026-25537 note in
apps/authapi/tokens.py), absent means REFUSED. apps/authapi/tokens.py
re-adds that type check (`_require_claim_types`) and apps/grants/owner_auth.py copies it,
but three verifiers decoded with PyJWT and stopped there:

    apps/peers/auth.py      verify_token          every peer-authenticated HTTP route
    apps/wsgw/auth.py       verify_peer_jwt       the WebSocket upgrade
    apps/dashboard/auth.py  verify_dashboard_jwt  the dashboard owner extractor

Divergence 015 says one configuration drives mint, HTTP verify and WS verify; it did not
say one POLICY, and the policies differed. Reaching this needs a token signed by the
server's key, so it is a consistency defect, not a forgery: but a verifier weaker than the
one it replaces is exactly what tokens.py's docstring says it exists to prevent.

Each case below differs from its control in ONE variable, the type of `exp` (C1).

Run:  pytest tests/test_jwt_claim_types.py -q      (no database needed)
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import override_settings  # noqa: E402

from apps.authapi import tokens  # noqa: E402
from apps.dashboard import auth as dash_auth  # noqa: E402
from apps.grants import owner_auth  # noqa: E402
from apps.peers import auth as peer_auth  # noqa: E402
from apps.wsgw import auth as ws_auth  # noqa: E402

ISSUER = "claim-types-test"

_SK = Ed25519PrivateKey.generate()
SK_PEM = _SK.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
PK_PEM = _SK.public_key().public_bytes(serialization.Encoding.PEM,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)


def _token(exp, aud: str) -> str:
    now = int(time.time())
    return pyjwt.encode({"sub": str(uuid.uuid4()), "iss": ISSUER, "iat": now, "exp": exp,
                         "aud": aud, "scopes": []}, SK_PEM, algorithm="EdDSA")


def _peer(token):
    with override_settings(MESHY_JWT_PUBLIC_KEY_PEM=PK_PEM, MESHY_JWT_ISSUER=ISSUER):
        return peer_auth.verify_token(token)


def _dashboard(token):
    with override_settings(MESHY_JWT_PUBLIC_KEY_PEM=PK_PEM, MESHY_JWT_ISSUER=ISSUER):
        return dash_auth.verify_dashboard_jwt(token)


def _grants(token):
    with override_settings(MESHY_JWT_PUBLIC_KEY_PEM=PK_PEM, MESHY_JWT_ISSUER=ISSUER):
        return owner_auth._verify_dashboard_jwt(token)


VERIFIERS = {
    "authapi.tokens.verify": (lambda t: tokens.verify(t, PK_PEM, ISSUER), "peer"),
    "peers.auth.verify_token": (_peer, "peer"),
    "wsgw.auth.verify_peer_jwt": (lambda t: ws_auth.verify_peer_jwt(t, PK_PEM, ISSUER), "peer"),
    "dashboard.auth.verify_dashboard_jwt": (_dashboard, "dashboard"),
    "grants.owner_auth._verify_dashboard_jwt": (_grants, "dashboard"),
}


def _later() -> int:
    return int(time.time()) + 600


WRONG_TYPES = {
    # PyJWT converts a numeric string to an integer.
    "exp as a numeric string": lambda: str(_later()),
    # PyJWT truncates it; the claim type check refuses it.
    "exp as a float": lambda: float(_later()) + 0.5,
}


@pytest.mark.parametrize("wrong", sorted(WRONG_TYPES))
@pytest.mark.parametrize("verifier", sorted(VERIFIERS))
def test_a_wrong_typed_exp_is_refused_where_an_integer_one_is_accepted(verifier, wrong):
    verify, aud = VERIFIERS[verifier]

    # CONTROL FIRST, same key, same issuer, same audience: an integer exp verifies. Without
    # it a verifier that refused everything would pass the assertion below.
    claims = verify(_token(_later(), aud))
    assert isinstance(claims["exp"], int) and claims["iss"] == ISSUER

    # The library really does accept the wrong type on its own; this is what makes the
    # re-added check load-bearing rather than decorative (C3: the fixture can fail).
    bad = _token(WRONG_TYPES[wrong](), aud)
    assert pyjwt.decode(bad, PK_PEM, algorithms=["EdDSA"], issuer=ISSUER,
                        options={"require": ["exp", "iss"], "verify_aud": False})["iss"] == ISSUER

    with pytest.raises(Exception) as exc:
        verify(bad)
    assert "exp" in str(exc.value), f"{verifier} refused for another reason: {exc.value}"
    status = getattr(exc.value, "status", None)
    assert status in (None, 401), f"{verifier} mapped a bad token to {status}, not 401"


def test_there_is_one_claim_type_rule():
    """The HTTP, WS and owner verifiers reach the SAME rule, so a verifier copying it by
    hand (as grants/owner_auth.py did) is the exception, not the pattern.

    Since STATUS B13 the rule is reached through the one verifier: `tokens.verify` applies
    it, and every entry point calls `tokens.verify_token` instead of decoding for itself
    (the decode half is pinned by tests/test_jwt_one_verifier.py). So the check is that
    the verifier applies the rule and each module calls the verifier."""
    assert tokens.require_claim_types is tokens._require_claim_types
    import inspect
    assert "require_claim_types(" in inspect.getsource(tokens.verify)
    for mod in (peer_auth, ws_auth, dash_auth):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "tokens.verify_token(" in src, f"{mod.__name__} does not call the one verifier"
    # Since REFACTOR-QUEUE-2026-11 B1 the grants surface has no verifier of its own: it IS the dashboard's.
    assert owner_auth._verify_dashboard_jwt is dash_auth.verify_dashboard_jwt


def _refusal_reason(verify, token) -> str:
    with pytest.raises(Exception) as exc:
        verify(token)
    # ApiError carries `.message`; JwtError and PyJWT's own errors carry it in str().
    return str(getattr(exc.value, "message", exc.value))


def _hs256_keyed_by(public_pem: bytes, claims: dict) -> str:
    """The key-confusion forgery, assembled by hand: PyJWT refuses to ENCODE an HMAC
    token under a PEM key, which is the very mistake being simulated."""
    import base64
    import hashlib
    import hmac
    import json

    def b64u(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    signing_input = (b64u(b'{"alg":"HS256","typ":"JWT"}') + "."
                     + b64u(json.dumps(claims, separators=(",", ":")).encode()))
    mac = hmac.new(public_pem, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{b64u(mac)}"


@pytest.mark.parametrize("verifier", sorted(VERIFIERS))
def test_an_hs256_token_keyed_by_the_public_key_is_refused_by_the_algorithm_pin(verifier):
    """STATUS C27 (RFQ M12), at every verifier rather than only the dashboard's.

    Adding "HS256" to a verifier's `algorithms` list survives any test that only asks
    whether the forgery is REFUSED, because PyJWT 2.14 also refuses a PEM-shaped HMAC key
    (InvalidKeyError) - `test_auth_flows.py::test_only_eddsa_is_accepted` included. That
    heuristic is the library's, not this project's control, and it has been bypassed
    before (PyJWT < 2.4.0, CVE-2022-29217). So this pins the REASON: the algorithm
    allowlist ("The specified alg value is not allowed"), which is what each verifier's
    `algorithms=[EdDSA]` is for.

    CONTROLS: the same claims signed EdDSA verify at the same verifier; the forgery is a
    genuine HMAC-SHA256 under the public key (PyJWT's own primitive, which applies no key
    heuristic), so a refusal is not a malformed token."""
    from jwt.algorithms import HMACAlgorithm

    verify, aud = VERIFIERS[verifier]
    now = int(time.time())
    claims = {"sub": str(uuid.uuid4()), "iss": ISSUER, "iat": now, "exp": now + 600,
              "aud": aud, "scopes": []}
    assert verify(pyjwt.encode(claims, SK_PEM, algorithm="EdDSA"))["iss"] == ISSUER

    forged = _hs256_keyed_by(PK_PEM, claims)
    import base64

    signing_input, _, sig_b64 = forged.rpartition(".")
    signature = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    assert HMACAlgorithm(HMACAlgorithm.SHA256).verify(
        signing_input.encode("ascii"), PK_PEM, signature)

    reason = _refusal_reason(verify, forged)
    assert "alg value is not allowed" in reason, f"{verifier} refused for another reason: {reason}"


@pytest.mark.parametrize("verifier", sorted(VERIFIERS))
def test_an_expired_token_is_refused_where_a_live_one_is_accepted(verifier):
    """STATUS C27 (RFQ M13), the lifetime half, at every verifier. ONE VARIABLE: `exp`
    90 s in the past versus ten minutes ahead.

    90 s, not an hour: the shared policy allows a 60 s leeway, so a token 90 s past `exp` is
    refused by every correct verifier with 30 s to spare. An
    hour in the past let `leeway=3500` pass at the grants and dashboard verifiers. The
    other side of the leeway (30 s past is still accepted) is not pinned here: today only
    two of the five verifiers have any leeway (STATUS B13)."""
    verify, aud = VERIFIERS[verifier]
    now = int(time.time())
    assert isinstance(verify(_token(now + 600, aud))["exp"], int)
    reason = _refusal_reason(verify, _token(now - 90, aud))
    assert "expired" in reason, f"{verifier} refused for another reason: {reason}"
