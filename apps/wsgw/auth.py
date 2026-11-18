"""Upgrade-time authentication for JWT, DPoP, Origin, and 0RTT gates.

WHERE THE CREDENTIAL IS. Not a header, not the query string: spec 6.2 puts the
JWT in `Sec-WebSocket-Protocol` as a `jwt.<token>` entry alongside the literal
`meshy.v1`. The browser WebSocket API cannot set headers, which is also why the
DPoP proof rides there as `dpop.<b64url>`.
Channels hands that header to us pre-split as `scope["subprotocols"]`.

THE JWT POLICY IS NOT HERE. It was: this module decoded the token itself, with
no leeway, and a hook that would have handed verification to an `apps.auth.jwt`
that never existed. The auth module that did land is `apps/authapi`, and
`verify_peer_jwt` now calls its ONE verifier, `apps.authapi.tokens.verify_token`
(STATUS B13, tests/test_jwt_one_verifier.py), so a token the HTTP routes accept
the socket accepts, and vice versa.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass

import jwt as pyjwt

from apps.authapi import tokens
from apps.crypto import primitives as P

from .constants import (
    DPOP_PREFIX,
    DPOP_WINDOW_SECS,
    DPOP_WS_METHOD,
    JWT_PREFIX,
    NEGOTIATED_PROTOCOL,
    USED_PROTOCOL_HASH_PREFIX,
)
from .didkey import DidKeyError, from_did_key


class AuthError(Exception):
    """`.kind` lets a test assert WHICH check refused rather than on prose."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


MISSING_PROTOCOL = "MissingProtocol"
NO_JWT_ENTRY = "NoJwtEntry"
DUPLICATE_JWT = "DuplicateJwt"
TOKEN_INVALID = "TokenInvalid"
WRONG_AUDIENCE = "WrongAudience"
MISSING_AUDIENCE = "MissingAudience"


@dataclass
class VerifiedConn:
    """The verified fields used to establish a WebSocket connection."""

    peer_id: uuid.UUID
    negotiated_protocol: str
    #: TASK-26: the token's DPoP confirmation claim (a did:key).
    cnf: str | None
    #: TASK-25: the authorizing OWNER on an on-behalf-of token.
    authorized_by_owner_id: uuid.UUID | None
    #: TASK-28 AC#3: the client's last-seen agent-card hash.
    used_protocol_hash: str | None
    #: Stored here so the token is verified ONCE and the expiry deadline tracks
    #: the same token that authenticated; rejecting duplicate jwt. entries
    #: avoids ambiguity about which token sets the deadline.
    expires_at: int | None = None


def split_protocol_entries(protocol_header_value: str) -> list[str]:
    """`Sec-WebSocket-Protocol` is comma-separated."""
    return [raw.strip() for raw in protocol_header_value.split(",")]


def entries_from_scope(scope: dict) -> list[str]:
    """Channels pre-splits the header into `scope["subprotocols"]`. Fall back to
    the raw header when it is absent, so this works under any ASGI server."""
    subprotocols = scope.get("subprotocols")
    if subprotocols:
        return [str(s).strip() for s in subprotocols]
    for name, value in scope.get("headers", ()):
        if name.lower() == b"sec-websocket-protocol":
            return split_protocol_entries(value.decode("latin-1"))
    return []


def verify_peer_jwt(token: str, jwt_public_key_pem: bytes, expected_issuer: str) -> dict:
    """Verify one peer JWT, return claims. The policy is `apps.authapi.tokens.verify_token`'s,
    with these rules:

      * algorithm EdDSA ONLY - never the token's own `alg`;
      * issuer pinned;
      * `exp` AND `iss` both REQUIRED and correctly typed (CVE-2026-25537);
      * 60 s leeway on `exp`, the same on every entry point (STATUS B13);
      * `aud` NOT validated here - the audience rule is the
        extractor's, and for the WS upgrade it is `aud == "peer"` exactly
        enforced in `verify_upgrade` below.

    A refusal is raised as the PyJWT error underneath it (or `InvalidTokenError` for the
    claim-type rule, which has none), so every caller's handling of a bad token is the one
    it had before this delegated.
    """
    # FAIL CLOSED on a missing key or issuer (divergence 015). This is not redundant with
    # the consumer's own check: PyJWT treats issuer=None as "do not check the issuer",
    # so without this line an unconfigured issuer accepts a token from ANY issuer.
    if not jwt_public_key_pem or not expected_issuer:
        raise ValueError("JWT verification key or issuer is not configured")
    try:
        return tokens.verify_token(token, jwt_public_key_pem, expected_issuer)
    except tokens.JwtError as exc:
        cause = exc.__cause__
        if isinstance(cause, pyjwt.PyJWTError):
            raise cause from None
        raise pyjwt.InvalidTokenError(str(exc)) from exc


def verify_upgrade(
    protocol_header_value: str | list[str],
    jwt_public_key_pem: bytes,
    expected_issuer: str,
) -> VerifiedConn:
    """Verify an upgrade. The order of checks is load-bearing."""
    entries = (
        split_protocol_entries(protocol_header_value)
        if isinstance(protocol_header_value, str)
        else [str(e).strip() for e in protocol_header_value]
    )

    found_protocol = False
    jwt_token: str | None = None
    jwt_count = 0
    used_protocol_hash: str | None = None
    for entry in entries:
        if entry == NEGOTIATED_PROTOCOL:
            found_protocol = True
        elif entry.startswith(JWT_PREFIX):
            jwt_token = entry[len(JWT_PREFIX):]
            jwt_count += 1
        elif entry.startswith(USED_PROTOCOL_HASH_PREFIX):
            # The last entry wins if repeated.
            used_protocol_hash = entry[len(USED_PROTOCOL_HASH_PREFIX):]

    if not found_protocol:
        raise AuthError(MISSING_PROTOCOL, "missing meshy.v1 in Sec-WebSocket-Protocol")
    if jwt_count > 1:
        # verify_upgrade reads the LAST jwt. entry and
        # jwt_expiry_from_proto reads the FIRST, so two DIFFERING entries would
        # authenticate on one token while the teardown deadline tracked another.
        raise AuthError(DUPLICATE_JWT, "duplicate jwt.<token> entries in Sec-WebSocket-Protocol")
    if jwt_token is None:
        raise AuthError(NO_JWT_ENTRY, "no jwt.<token> entry in Sec-WebSocket-Protocol")

    try:
        claims = verify_peer_jwt(jwt_token, jwt_public_key_pem, expected_issuer)
    except Exception as exc:  # pyjwt raises a family; flatten it to one refusal
        raise AuthError(TOKEN_INVALID, f"token invalid: {exc}") from exc

    # The None branch is removed on purpose: smoke-test tokens carry
    # aud="smoke-test" and dashboard tokens aud="dashboard", and
    # both must be refused on this socket.
    aud = claims.get("aud")
    if aud is None:
        raise AuthError(MISSING_AUDIENCE,
                        "audience claim missing; production tokens must carry aud=peer")
    if aud != "peer":
        raise AuthError(WRONG_AUDIENCE, f"audience must be peer, got: {aud}")

    # On-behalf-of tokens carry sub=owner + act.sub=peer.
    act = claims.get("act")
    if isinstance(act, dict) and "sub" in act:
        peer_sub = uuid.UUID(str(act["sub"]))
        authorized_by_owner_id = uuid.UUID(str(claims["sub"]))
    else:
        peer_sub = uuid.UUID(str(claims["sub"]))
        authorized_by_owner_id = None

    return VerifiedConn(
        peer_id=peer_sub,
        negotiated_protocol=NEGOTIATED_PROTOCOL,
        cnf=claims.get("cnf"),
        authorized_by_owner_id=authorized_by_owner_id,
        used_protocol_hash=used_protocol_hash,
        expires_at=int(claims["exp"]),
    )


# --- DPoP -------------------------------------------------------------------


def extract_dpop_wire(entries: list[str]) -> str | None:
    """Return the `dpop.<b64url>` subprotocol entry."""
    for entry in entries:
        if entry.startswith(DPOP_PREFIX):
            return entry[len(DPOP_PREFIX):]
    return None


def verify_ws_dpop_proof(
    entries: list[str],
    cnf: str,
    token: str,
    target: str,
    *,
    now: int | None = None,
) -> tuple[bool, str, bytes | None]:
    """Verify DPoP except for the jti
    ledger write, which needs the database and lives in db.claim_dpop_jti.

    Returns `(ok, reason, jti)`. `jti` comes back on success so the caller can
    spend it; on failure it is None and the caller must fail the upgrade CLOSED.

    Binding: method=WS_UPGRADE, target=the request path, and the presented JWT.
    Verification itself is `apps.crypto.primitives`, whose vectors
    `test_crypto_parity` pins.
    """
    wire = extract_dpop_wire(entries)
    if wire is None:
        return False, "proof entry missing", None
    try:
        key = from_did_key(cnf)
    except DidKeyError as exc:
        return False, f"cnf claim is not a resolvable did:key: {exc}", None
    ok, why = P.verify_dpop(
        wire, key,
        method=DPOP_WS_METHOD,
        target=target,
        jwt=token,
        now=int(time.time()) if now is None else now,
        max_skew_secs=DPOP_WINDOW_SECS,
    )
    if not ok:
        return False, f"proof invalid: {why}", None
    return True, "ok", P.parse_dpop_wire(wire)["jti"]


def dpop_expiry(wire: str) -> int:
    """`proof.timestamp + DPOP_WINDOW_SECS` is the jti row's TTL."""
    return P.parse_dpop_wire(wire)["timestamp"] + DPOP_WINDOW_SECS


# --- Origin gate -------------------------------------------------------------


def origin_host_eq(origin: str, host: str) -> bool:
    """Exact host+port; scheme is NOT compared because the
    Host header carries none. A malformed Origin never matches."""
    if "://" not in origin:
        return False
    _, _, rest = origin.partition("://")
    return bool(rest) and rest == host


def origin_allowed(headers: dict[bytes, bytes], allow_list: list[str]) -> bool:
    """ABSENT Origin PASSES, and that is deliberate, not an oversight:
    mesh nodes, curl and the CLI daemons never send one.
    Only a PRESENT-but-not-allow-listed Origin is refused. A same-origin request
    passes on the Host match because the production
    dashboard is served same-origin with an intentionally EMPTY allow-list.
    """
    origin_raw = headers.get(b"origin")
    if origin_raw is None:
        return True
    try:
        origin = origin_raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    host_raw = headers.get(b"host")
    if host_raw is not None:
        try:
            if origin_host_eq(origin, host_raw.decode("utf-8")):
                return True
        except UnicodeDecodeError:
            pass
    return origin in allow_list


# --- 0RTT agent-card hash ---------------------------------------------------


def protocol_hash(card: object) -> str:
    """Return lowercase hex sha256 over the JCS-canonicalized card."""
    return hashlib.sha256(P.jcs(card)).hexdigest()
