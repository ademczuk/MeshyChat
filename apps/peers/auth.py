"""`AuthenticatedPeer` is the peer identity a route is allowed to act on.

SCOPE BOUNDARY: token MINTING, refresh, sign-in and the owner/dashboard session live in
`apps/authapi`, which another agent owns. This module implements only the VERIFY side
the peer routes need, and it implements it rather than stubbing it, because three of the
rules below are the authorisation:

  - `aud` must be exactly "peer". A token with no audience claim is
    never legitimately minted, so absent is rejected by the same path as wrong - 403.
  - the acting peer is `act.sub` when the on-behalf-of actor claim is present, else
    `sub`. Keying on `sub` alone would attribute an owner-authorized
    call to the OWNER's id.
  - the peer row must still be `status = 'active'` - a deleted peer
    keeps a valid unexpired JWT, so the token alone is not sufficient.

`lin` (the refresh lineage) is carried through because key rotation keys
on it: absent means re-anchor nothing, and that is a deliberate migration-031 decision,
not a missing value to paper over.

DPoP on writes (docs/DIVERGENCES.md 015). With `settings.MESHY_DPOP_ENFORCE` on - the
default - a POST/PUT/PATCH/DELETE must:

  - carry a token WITH a `cnf` claim. A bearer token (no cnf) is refused outright on a
    write (TASK-46): otherwise, a stolen bearer token could write with no proof of
    possession;
  - carry a `DPoP` header whose proof is bound to this method, this path and this exact
    token, signed by the key `cnf` names, inside the 120 s window;
  - spend that proof's jti in `dpop_jti_seen` (migration 046). A second use is a replay.

Every one of those failures is 403 E_FORBIDDEN. Reads are deliberately not gated: the
node client sends no proof on its polling GETs, so gating reads would lock out every node.
"""
from __future__ import annotations

import uuid

import psycopg
from django.conf import settings

from apps.authapi import tokens
from apps.authapi.didkey import from_did_key
from apps.authapi.tokens import JwtError
from apps.crypto import primitives as P
# One definition shared with the WebSocket gate: the proof window and the jti row's
# lifetime past the proof timestamp.
from apps.wsgw.constants import DPOP_WINDOW_SECS

from . import errors

#: Reads stay bearer-only; these methods require DPoP when enforcement is enabled.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuthenticatedPeer:
    __slots__ = ("peer_id", "scopes", "lin", "authorized_by_owner_id", "token")

    def __init__(self, peer_id, scopes, lin, authorized_by_owner_id, token):
        self.peer_id = peer_id
        self.scopes = scopes
        self.lin = lin
        self.authorized_by_owner_id = authorized_by_owner_id
        self.token = token

    def require_scope(self, scope: str) -> None:
        """Raise 403 E_SCOPE_REQUIRED when the scope is absent."""
        if scope not in self.scopes:
            raise errors.ApiError(
                403, "E_SCOPE_REQUIRED",
                f"peer token is missing the required scope: {scope}")


def _config(name: str, default=None):
    value = getattr(settings, name, None)
    return default if value is None else value


def verify_token(token: str) -> dict:
    """Verify through the one verifier, `apps.authapi.tokens.verify_token`.

    This used to call PyJWT itself with no leeway, so a token 30 s past `exp` was refused on
    every peer route although the shared verifier's 60 s leeway accepts it (STATUS B13). The policy - EdDSA only, issuer pinned, `exp`
    and `iss` required and correctly typed, 60 s leeway - now lives in one place and is
    pinned on this entry point by tests/test_jwt_one_verifier.py.

    `validate_aud` is off there because the audience is an extractor
    decision (peer vs dashboard), applied in `authenticate_peer` below.
    """
    public_key_pem = _config("MESHY_JWT_PUBLIC_KEY_PEM")
    issuer = _config("MESHY_JWT_ISSUER")
    if not public_key_pem or not issuer:
        raise errors.internal("JWT verification key or issuer is not configured")
    try:
        return tokens.verify_token(token, public_key_pem, issuer)
    except JwtError as exc:
        raise errors.auth_invalid(f"token invalid: {exc}")


def _uuid_or_none(value):
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def authenticate_peer(conn, request) -> AuthenticatedPeer:
    """Authenticate a peer. `conn` is used for the peer-status lookup, which is a real
    query against the real table - the token is not the last word on whether the peer
    still exists."""
    header = request.headers.get("Authorization")
    if header is None:
        raise errors.auth_missing()
    try:
        header.encode("ascii")
    except UnicodeEncodeError:
        raise errors.auth_invalid("non-ascii Authorization header")
    if not header.startswith("Bearer "):
        raise errors.auth_invalid("not a Bearer token")
    token = header[len("Bearer "):]

    claims = verify_token(token)

    if claims.get("aud") != "peer":
        raise errors.forbidden()

    act = claims.get("act") or None
    act_sub = _uuid_or_none(act.get("sub")) if isinstance(act, dict) else None
    sub = _uuid_or_none(claims.get("sub"))
    if sub is None:
        raise errors.auth_invalid("token invalid: sub is not a uuid")
    peer_sub = act_sub if act_sub is not None else sub
    authorized_by_owner_id = sub if act_sub is not None else None

    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM peers WHERE id = %s AND status = 'active')",
                    (str(peer_sub),))
        peer_active = cur.fetchone()[0]
    if not peer_active:
        raise errors.forbidden()

    if dpop_enforced() and request.method in WRITE_METHODS:
        require_bound_proof(conn, request, claims, token, peer_sub)

    scopes = claims.get("scopes") or []
    if not isinstance(scopes, list):
        scopes = []
    return AuthenticatedPeer(
        peer_id=peer_sub,
        scopes=[s for s in scopes if isinstance(s, str)],
        lin=_uuid_or_none(claims.get("lin")),
        authorized_by_owner_id=authorized_by_owner_id,
        token=token,
    )


def dpop_enforced() -> bool:
    """Only a literal False turns DPoP enforcement off; an absent setting is on.

    The default is on, and an absent setting counts as on, so a settings module that never
    defined it cannot silently disable DPoP (docs/DIVERGENCES.md 015).
    """
    return getattr(settings, "MESHY_DPOP_ENFORCE", None) is not False


def request_target(request) -> str:
    """Return the path as the client sent it, without a query.

    Under ASGI that is `scope["raw_path"]`, still percent-encoded, which is what a client
    signs. `request.path` is the DECODED path, so for any path carrying a %-escape it
    differs from the signed target and a correct proof would be refused. It is the
    fallback only where no raw path exists (the WSGI test client)."""
    scope = getattr(request, "scope", None)
    raw = scope.get("raw_path") if isinstance(scope, dict) else None
    if raw:
        return bytes(raw).split(b"?", 1)[0].decode("latin-1")
    return request.path


def require_bound_proof(conn, request, claims: dict, token: str, peer_id) -> None:
    """Require a valid, bound, fresh, and newly spent proof.

    Raises 403 on refusal and 500 on a ledger fault.
    """
    cnf = claims.get("cnf")
    if not isinstance(cnf, str) or not cnf:
        raise errors.forbidden()                       # TASK-46
    wire = request.headers.get("DPoP")
    if wire is None:
        raise errors.forbidden()                       # no DPoP header on a write
    try:
        wire.encode("ascii")
    except UnicodeEncodeError:
        raise errors.forbidden()                       # non-ASCII proof header
    try:
        key = from_did_key(cnf)                        # Decode the proof key.
    except ValueError:
        raise errors.forbidden()
    ok, _why = P.verify_dpop(wire, key, method=request.method,
                             target=request_target(request), jwt=token,
                             max_skew_secs=DPOP_WINDOW_SECS)
    if not ok:
        raise errors.forbidden()                       # Invalid or stale proof.
    proof = P.parse_dpop_wire(wire)
    try:
        # One atomic insert with zero rows returned means the jti was already spent.
        # `dpop_jti_seen` carries no RLS (measured in
        # apps/wsgw/db.py), so no context is bound for it.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dpop_jti_seen (jti, peer_id, expires_at) "
                "VALUES (%s, %s, to_timestamp(%s)) "
                "ON CONFLICT (jti) DO NOTHING RETURNING jti",
                (proof["jti"], str(peer_id), proof["timestamp"] + DPOP_WINDOW_SECS))
            claimed = cur.fetchone()
    except psycopg.Error as exc:
        raise errors.internal(f"dpop jti ledger: {exc}")
    if claimed is None:
        raise errors.forbidden()                       # replay
