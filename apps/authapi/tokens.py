"""EdDSA JWT issuance and verification.

The claim set is constructed in a fixed order so tokens minted from the same claims have
identical signed bytes:

    sub     uuid    the peer_id OR the owner_id, depending on aud
    iss     str     issuer, pinned at verification
    iat     int     seconds since epoch
    exp     int     seconds since epoch
    scopes  [str]   always serialized
    aud     str?    always serialized as `null` when absent
    lin     uuid?   omitted when absent; refresh-token lineage
    cnf     str?    omitted when absent; the peer's did:key, not a jkt
    act     {sub}?  omitted when absent; the acting peer

The validation policy deliberately differs from the library default:

    algorithm     EdDSA only
    required      exp AND iss. `iss` is required so a token without one cannot skip issuer
                   matching (PyJWT skips the check when the claim is absent); its type is
                   enforced by `require_claim_types`.
    validate_exp  true, with 60s leeway
    validate_aud  FALSE — audience is enforced by the CALLER (the extractors), not
                   here. A peer/dashboard split enforced in this
                  function would break every caller that decodes both.

TWO PLACES THIS DOES MORE WORK THAN PyJWT WOULD:

1. WRONG-TYPED CLAIMS. PyJWT's exp check is `int(payload["exp"])`, which happily
    accepts the STRING "99999999999". `require_claim_types` re-adds the type check so
    this policy rejects wrong types. Pinned by `test_wrong_typed_exp_is_rejected`.

2. THE PUBLIC KEY HISTORY. `verify` takes a LIST of public keys. A frame signed under a
   rotated-out key still verifies (invocation-frame audit only; token authentication passes
   exactly one key via `verify_token`). Only a SIGNATURE failure falls through to the next
   key: an expired or wrong-issuer token is
   raised immediately rather than being re-tried under every key and reported as
   "unknown key", which would hide why it really failed.
"""
from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping, Sequence

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from apps.crypto.primitives import b64u, server_kid

ALGORITHM = "EdDSA"
#: 60 s because PyJWT's own default is 0, which would reject tokens a few seconds past `exp`;
#: one value for every verifier (see `verify`).
DEFAULT_LEEWAY_SECS = 60
#: `exp` and `iss` must be present and correctly typed.
REQUIRED_CLAIMS = ("exp", "iss")


class JwtError(Exception):
    """Every failure of this module; callers inspect `__cause__` for the specific cause."""


def now_unix() -> int:
    return int(time.time())


def expires_in_secs(secs: int) -> int:
    return now_unix() + secs


def build_claims(
    *,
    sub,
    iss: str,
    iat: int,
    exp: int,
    scopes: Sequence[str] = (),
    aud: str | None = None,
    lin=None,
    cnf: str | None = None,
    act_sub=None,
) -> dict:
    """Build the claim dict in its signed field order.

    `aud` is emitted even when None; `lin`, `cnf`, and `act` are omitted when None.
    Getting this backwards changes the signed bytes, so it is not cosmetic.
    """
    claims: dict = {
        "sub": str(sub),
        "iss": iss,
        "iat": int(iat),
        "exp": int(exp),
        "scopes": list(scopes),
        "aud": aud,
    }
    if lin is not None:
        claims["lin"] = str(lin)
    if cnf is not None:
        claims["cnf"] = cnf
    if act_sub is not None:
        claims["act"] = {"sub": str(act_sub)}
    return claims


def load_signing_key(pem: bytes) -> Ed25519PrivateKey:
    """Load a PKCS#8 PEM Ed25519 private key."""
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:  # noqa: BLE001 - any parse failure is one error to callers
        raise JwtError(f"not a PKCS#8 PEM private key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise JwtError("alg EdDSA requires an Ed25519 private key")
    return key


def load_public_key(pem: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(pem)
    except Exception as exc:  # noqa: BLE001
        raise JwtError(f"not a PEM public key: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise JwtError("alg EdDSA requires an Ed25519 public key")
    return key


def public_key_raw(pem: bytes) -> bytes:
    """The 32 raw bytes, which is what both the kid and the JWKS `x` are built from."""
    return load_public_key(pem).public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def sign(claims: Mapping, signing_key_pem: bytes) -> str:
    """Sign claims as a compact JWS.

    The JWS is assembled here because PyJWT sorts header keys; both orders verify, but a fixed
    `{"typ":"JWT","alg":"EdDSA"}` keeps minted tokens byte-stable for the same claims.
    """
    key = load_signing_key(signing_key_pem)
    header = b'{"typ":"JWT","alg":"EdDSA"}'
    # Compact separators and raw UTF-8 keep the payload bytes deterministic.
    payload = json.dumps(dict(claims), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signing_input = f"{b64u(header)}.{b64u(payload)}".encode("ascii")
    return f"{signing_input.decode('ascii')}.{b64u(key.sign(signing_input))}"


def require_claim_types(payload: Mapping) -> None:
    """Enforce required-claim types in addition to the JWT library checks.

    ONE function for every verifier, and now reached through ONE verifier: `verify` calls
    it, and every entry point calls `verify_token`. Until 2026-09-23 the peer routes, the
    WebSocket upgrade and the dashboard each decoded for themselves and accepted a string
    `exp` this module refuses (tests/test_jwt_claim_types.py); the decode itself was
    consolidated here afterwards (STATUS B13, tests/test_jwt_one_verifier.py).

    PyJWT does not type-check required claims: `int(payload["exp"])` accepts the string
    "99999999999", so an attacker-supplied string exp would skip the lifetime bound. Reject
    both wrong types explicitly so this policy enforces the lifetime bound.
    """
    exp = payload.get("exp")
    # bool is an int subclass in Python; `exp: true` is not a timestamp.
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise JwtError("missing required claim: exp")
    if not isinstance(payload.get("iss"), str):
        raise JwtError("missing required claim: iss")


#: The old private name, kept so nothing that imported it breaks.
_require_claim_types = require_claim_types


def verify(
    token: str,
    public_key_pems: Sequence[bytes] | bytes,
    issuer: str,
) -> dict:
    """Verify a signed JWT.

    The leeway is not a parameter. Five verifiers under apps/ once made five
    choices (60, 60, 0, 0, 0 seconds), so a token 30 s past `exp` was good on /v1/grants and
    dead on every peer route, the socket, and the dashboard (STATUS B13).
    `DEFAULT_LEEWAY_SECS` fixes it for every caller.

    ACCEPTS A LIST, BUT CALLERS FOR *TOKENS* MUST PASS EXACTLY ONE KEY.

    The key history (`MESHYCHAT_JWT_PUBLIC_KEY_HISTORY_PEM_PATHS`) is consumed only for
    invocation-frame verification under a rotated-out key. It is not part of JWT
    verification.

    JWT authentication uses only the current key. Accepting a retired key would widen the
    trust set. The list parameter remains for frame verification; `verify_token` below is
    the single-key authentication entry point.

    A non-signature failure (expired, wrong issuer, malformed, wrong alg) raises at once —
    it is not key-dependent, so retrying under another key would only lose the real reason.

    NO ISSUER, NO VERIFY. PyJWT given `issuer=None` SKIPS the issuer check, so a token any
    issuer minted with this key would pass. Every caller refused an unset issuer itself, and
    this function did not, so a caller that forgot would have accepted it silently (review
    of c766b76, finding F4; `MESHY_JWT_ISSUER` is None whenever `MESHYCHAT_JWT_ISSUER` is unset).
    This server can start with no issuer (divergence 015), so `verify` refuses it itself.
    """
    if isinstance(public_key_pems, (bytes, bytearray)):
        public_key_pems = [bytes(public_key_pems)]
    pems = list(public_key_pems)
    if not pems:
        raise JwtError("no public key configured to verify against")
    if not isinstance(issuer, str) or not issuer:
        raise JwtError("no issuer configured to verify against")

    last_signature_error: Exception | None = None
    for pem in pems:
        key = load_public_key(pem)
        try:
            payload = pyjwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=issuer,
                leeway=DEFAULT_LEEWAY_SECS,
                options={
                    "require": list(REQUIRED_CLAIMS),
                    "verify_signature": True,
                    "verify_exp": True,
                    # The caller enforces the audience for its own surface.
                    "verify_aud": False,
                },
            )
        except pyjwt.InvalidSignatureError as exc:
            last_signature_error = exc
            continue
        except pyjwt.PyJWTError as exc:
            raise JwtError(str(exc)) from exc
        require_claim_types(payload)
        return payload

    raise JwtError(
        "token invalid: no configured public key (current or history) verifies this signature"
    ) from last_signature_error


def jwk_for(public_key_pem: bytes) -> dict:
    """Build one OKP/Ed25519 JWK with its fixed field set and order.

    The kid is the SERVER rule: 16 LOWERCASE HEX chars = sha256(raw pubkey)[:8]
    A peer key uses a different base64url-derived kid. The two are deliberately not
    merged; using the peer rule here produces a kid no verifier resolves."""
    raw = public_key_raw(public_key_pem)
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"),
        "kid": server_kid(raw),
        "alg": "EdDSA",
        "use": "sig",
    }


def jwks_document(public_key_pems: Sequence[bytes]) -> dict:
    """Build a JWKS document from the given keys and their server-rule kids.

    An unparseable key yields `{"keys": []}` rather than an error because the boot path
    already validates configured keys; a throw here would turn a verification anchor into
    an outage."""
    keys = []
    for pem in public_key_pems:
        try:
            keys.append(jwk_for(pem))
        except JwtError:
            continue
    return {"keys": keys}


def verify_token(token: str, current_public_key_pem: bytes | str, issuer: str) -> dict:
    """Verify a JWT against the current key only.

    THE ONE VERIFIER (STATUS B13). Every entry point that authenticates with a JWT calls
    this and nothing else: the peer HTTP extractor (apps/peers/auth.py, and through it
    `/v1/mesh/wait`), the WebSocket upgrade (apps/wsgw/auth.py), and both owner extractors
    (apps/dashboard/auth.py, apps/grants/owner_auth.py). One leeway, one required-claims set,
    one claim-type rule; `tests/test_jwt_one_verifier.py` pins all three on every one of
    them, and fails if a module under apps/ decodes a JWT itself.

    `verify()` takes a list for frame verification, where a rotated-out key is legitimate;
    a token signed by a retired key is not. A `str` PEM is accepted because that is how a
    Django setting usually holds one; a list is refused, loudly, for the reason above.
    """
    if isinstance(current_public_key_pem, str):
        current_public_key_pem = current_public_key_pem.encode("ascii")
    if not isinstance(current_public_key_pem, (bytes, bytearray)):
        raise JwtError("verify_token takes exactly one PEM; the history is for FRAMES")
    return verify(token, [bytes(current_public_key_pem)], issuer)
