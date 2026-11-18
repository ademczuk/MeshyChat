"""`AuthenticatedOwner` - the owner identity every dashboard route is scoped to.

The dashboard is OWNER-authenticated, not
peer-authenticated: `apps/peers/auth.py` resolves `aud="peer"` tokens to a peer id and
is the wrong extractor here. Binding the wrong one is not a 401 - it is a working
request scoped to the wrong subject.

THE RESOLUTION ORDER IS THE SECURITY PROPERTY
--------------------------------------------------------------------
1. `meshy_session` cookie. Present-and-invalid surfaces E_SESSION_EXPIRED and does NOT
   fall through: a stale cookie must not be able to escalate to another scheme. No
   cookie at all continues.
2. `Authorization: Bearer meshypat_...` - a PAT. Resolved by SHA-256 hash lookup
   against `owner_api_keys`, never by JWT verification, so it does not touch the
   fail-closed JWT gate. A `meshypat_`-prefixed token is resolved HERE or rejected
   HERE; it never falls through.
3. Bearer JWT fallback, GATED. When SSO is configured and `MESHY_ALLOW_DEV_JWT` is off,
   a plain Bearer JWT is rejected. This is the load-bearing gate; it is two settings here,
   and while SSO is not configured bearer JWTs are permitted by default.
4. No Authorization header -> E_AUTH_MISSING.

Steps 1 and 2 are terminal by construction rather than by comment: each returns or
raises, and neither has a code path that continues to the next scheme.

CSRF: the cookie is the only AMBIENT credential, so
it is the only path that must carry `x-csrf-token` on a state-changing method. PAT and
dev-JWT callers are exempt because a client that must attach a header is not subject to
cross-site forgery in the first place. Every route in this app is GET, so `require_csrf`
is a no-op on today's wire; it is implemented rather than omitted because the write side
lands on these same paths and an unimplemented CSRF check is indistinguishable from a
passing one.

That was the argument for having it at all. It was ALSO the reason the version that
shipped was a presence check - nothing on the surface could exercise it, so nothing
noticed that `if not request.headers.get("x-csrf-token")` accepts any value a
cross-site page can make the browser send. `require_csrf` now defers to
`apps.core.websec.csrf_ok`, the HMAC-bound check, and `tests/test_dashboard.py`
exercises it directly rather than waiting for a write route to exist.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Optional

from apps.authapi import tokens
from apps.authapi.tokens import JwtError
from apps.core import env, websec
from django.conf import settings
from psycopg.rows import dict_row

from . import errors

#: Prefix for personal access tokens.
PAT_PREFIX = "meshypat_"

#: Default owner-session cookie name.
DEFAULT_SESSION_COOKIE = "meshy_session"

#: EdDSA only. Accepting HS256 would let anyone holding
#: the PUBLIC key mint tokens.
JWT_ALGORITHM = tokens.ALGORITHM

#: The dev-JWT audience must be exactly this.
DASHBOARD_AUDIENCE = "dashboard"

#: Methods that need no CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


# ----------------------------------------------------------------- the identity

@dataclass(frozen=True)
class AuthenticatedOwner:
    """`method` is the discriminator, not decoration.

    Cookie and DevJwt are the human acting directly (full authority). `pat` is a
    programmatic credential constrained to its granted scopes, and `require_scope`
    is how a route says so.
    """

    owner_id: uuid.UUID
    method: str                      # "cookie" | "pat" | "dev_jwt"
    key_id: Optional[uuid.UUID] = None
    scopes: tuple = ()
    claims: dict = field(default_factory=dict)

    def require_scope(self, scope: str) -> None:
        """Cookie/DevJwt always pass; a PAT must carry it."""
        if self.method in ("cookie", "dev_jwt"):
            return
        if scope in self.scopes:
            return
        raise errors.scope_required(scope)

    def require_human(self) -> None:
        """Reserved for inherently interactive actions."""
        if self.method in ("cookie", "dev_jwt"):
            return
        raise errors.ApiError(
            403, errors.E_SCOPE_REQUIRED,
            "this action requires an interactive owner session, not an API key")

    def require_csrf(self, request) -> None:
        """Cookie callers only, unsafe
        methods only, and the token COMPARED rather than counted.

        This used to be `if not request.headers.get("x-csrf-token")`. Django's
        `CsrfViewMiddleware` is deliberately absent from `meshychat/settings.py`
        (settings.py says so where it lists the middleware it leaves out), so that
        presence check was the ONLY CSRF control on this surface - and any cross-site
        page that can make the browser send `X-CSRF-Token: x` defeats a presence check.
        A mutation replacing the whole method with `return` left the suite green.

        The comparison lives in `apps.core.websec.csrf_ok`, which is shared rather than
        re-written here: the token is an HMAC over a domain string and the SESSION ID, so
        it cannot be replayed into another session, it fails CLOSED when no state secret
        is configured outside DEBUG, and it compares BYTES - `hmac.compare_digest` on two
        `str` raises `TypeError` for any codepoint above 0x7F, and Django decodes headers
        as latin-1, so an attacker-chosen header byte would otherwise decide whether this
        server answers JSON or an HTML 500.
        """
        if self.method != "cookie":
            return
        if request.method.upper() in SAFE_METHODS:
            return
        session_id = self.claims.get("session_id")
        if session_id is None:
            # FAIL CLOSED, and before websec: the expected token is derived FROM the
            # session id, so there is nothing to compare against. `csrf_ok` promises not
            # to raise on a hostile header, not on a missing session, and a cookie
            # credential without one is a bug here, not a caller's input.
            raise errors.csrf_required()
        if not websec.csrf_ok(request.method, request.headers.get("x-csrf-token"),
                              session_id, state_secret(),
                              debug=bool(getattr(settings, "DEBUG", False))):
            raise errors.csrf_required()


# ------------------------------------------------------------------- settings

def _setting(name: str, default=None):
    value = getattr(settings, name, None)
    return default if value is None else value


def session_cookie_name() -> str:
    """Return the configured session-cookie name, defaulting to `meshy_session`.

    THE ONE READER of the cookie name, for every owner surface: this extractor, owner
    sign-in and `/v1/auth/me` (apps/authapi/views.py), and the grants extractor
    (apps/grants/owner_auth.py). It used to read the Django setting only while grants read
    `MESHYCHAT_SESSION_COOKIE_NAME` from the environment only, so configuring either spelling
    signed a session in to half the product (STATUS B10). Both spellings now reach this
    one function through `apps.core.env.setting`, the reader `state_secret` below already
    uses: the Django setting `MESHY_SESSION_COOKIE_NAME`, else the environment variable
    `MESHYCHAT_SESSION_COOKIE_NAME`. A blank value is not a name.
    """
    raw = env.setting("MESHY_SESSION_COOKIE_NAME", "MESHYCHAT_SESSION_COOKIE_NAME")
    name = str(raw).strip() if raw is not None else ""
    return name or DEFAULT_SESSION_COOKIE


def state_secret() -> bytes | None:
    """Return the HMAC key used to mint and check CSRF tokens.

    Read through `apps.core.env.setting`, so it is reachable from a Django setting OR
    from the environment. A secret that only one of
    the two sources can supply is the same defect `bearer_jwt_allowed` had: a control
    nobody can switch on.

    `None` means "not configured", and `csrf_ok` turns that into a refusal outside
    DEBUG. An empty or blank value is NOT a secret and is reported as unconfigured, so
    `MESHYCHAT_STATE_SECRET=""` fails closed in production rather than keying an HMAC on the
    empty string.
    """
    raw = env.setting("MESHY_STATE_SECRET", "MESHYCHAT_STATE_SECRET")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.strip() or None
    raw = str(raw).strip()
    return raw.encode("utf-8") if raw else None


def bearer_jwt_allowed() -> bool:
    """Return whether Bearer JWT authentication is allowed for owner routes.

    `MESHY_SSO_CONFIGURED` defaults to False because this deployment has no SSO service
    wired, so refusing the Bearer path would leave the
    dashboard with no reachable credential at all.

    THE DEFAULT WAS FINE; REACHING IT WAS NOT. Both flags used to be read through
    `_setting`, which consults Django settings ONLY - and `meshychat/settings.py` defines
    neither. So the documented production posture could not be switched on by any means:
    exporting `MESHY_SSO_CONFIGURED=1` did nothing, `bearer_jwt_allowed()` stayed True, and
    any EdDSA `aud=dashboard` token from the configured issuer was full owner authority.
    The sibling extractor in this same project got it right, which is how the divergence
    survived unnoticed - two owner extractors, one flag, different reachability.

    They now go through `apps.core.env.env_flag`, which reads the setting and falls back to
    the environment. Defaults are unchanged, so nothing moves for a deployment that sets
    neither; what changes is that a deployment CAN now set them.

    `env_flag` also means "0" is False, which `bool("0")` is not - a deployment that
    exports `MESHY_ALLOW_DEV_JWT=0` to turn the dev path OFF would otherwise have turned it
    on.

    ONE SWITCH FOR EVERY OWNER SURFACE. `bearer_allowed` governs the dashboard and
    /v1/grants alike. The grants extractor (apps/grants/owner_auth.py) read its own
    flag, `MESHYCHAT_ALLOW_DEV_JWT`, while this one read only the two flags above. Each
    surface honoured only its own off switch: `MESHYCHAT_ALLOW_DEV_JWT=0` closed grants and
    left the dashboard open, and the production posture above closed the dashboard and
    left grant issue and revoke open. The grants extractor now asks THIS function, and
    this function honours both switches: either one turned off closes the dev-JWT path
    everywhere. It is the conjunction of the two old answers, so nothing that was closed
    opens. `MESHYCHAT_ALLOW_DEV_JWT` keeps its old default (unset = allowed) because it is an
    ALLOW flag: unset must mean "not turned off", exactly as the grants copy read it.
    """
    if not env.env_flag("MESHYCHAT_ALLOW_DEV_JWT", "MESHYCHAT_ALLOW_DEV_JWT", default=True):
        return False
    if not env.env_flag("MESHY_SSO_CONFIGURED", "MESHY_SSO_CONFIGURED"):
        return True
    return env.env_flag("MESHY_ALLOW_DEV_JWT", "MESHY_ALLOW_DEV_JWT")


def _sha256_hex(raw: str) -> str:
    """Return `hex(sha256(utf8 bytes))` for session tokens and PATs."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bearer(request) -> Optional[str]:
    """LENIENT. A missing / non-ASCII / non-Bearer header
    returns None rather than raising, so the JWT path downstream can produce its own
    precise error for each case."""
    header = request.headers.get("Authorization")
    if header is None:
        return None
    try:
        header.encode("ascii")
    except UnicodeEncodeError:
        return None
    if not header.startswith("Bearer "):
        return None
    return header[len("Bearer "):]


# --------------------------------------------------------------- the three paths

def _from_cookie(conn, request) -> Optional[AuthenticatedOwner]:
    """Returns None for "no cookie", raises for a bad one."""
    raw = request.COOKIES.get(session_cookie_name())
    if not raw:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        # Matched on token_hash, and expiry after the database's current time is part
        # of the lookup rather than a check afterwards, so an expired row is simply not
        # found. `owner_sessions` has no RLS (measured), so no privileged context.
        cur.execute(
            "SELECT id, owner_id, expires_at FROM owner_sessions "
            "WHERE token_hash = %(hash)s AND expires_at > NOW()",
            {"hash": _sha256_hex(raw)},
        )
        row = cur.fetchone()
    if row is None:
        # TERMINAL. Falling through to the Bearer paths here is the escalation
        # that a stale cookie could otherwise create.
        raise errors.session_expired()
    return AuthenticatedOwner(owner_id=row["owner_id"], method="cookie",
                              claims={"session_id": row["id"]})


def _from_pat(conn, token: str) -> AuthenticatedOwner:
    """Resolve a PAT; an invalid or expired PAT is terminal."""
    with conn.cursor(row_factory=dict_row) as cur:
        # resolve_and_touch: the UPDATE ... RETURNING is one statement, so the liveness
        # predicate and the read cannot disagree. The three-part predicate is the whole
        # authorisation - a `token_hash` match alone would accept a revoked key.
        cur.execute(
            "UPDATE owner_api_keys SET last_used_at = NOW() "
            "WHERE token_hash = %(hash)s "
            "  AND revoked_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > NOW()) "
            "RETURNING id, owner_id, scopes, expires_at",
            {"hash": _sha256_hex(token)},
        )
        row = cur.fetchone()
    if row is None:
        raise errors.api_key_invalid()
    return AuthenticatedOwner(
        owner_id=row["owner_id"],
        method="pat",
        key_id=row["id"],
        scopes=tuple(row["scopes"] or ()),
    )


def verify_dashboard_jwt(token: str) -> dict:
    """Verify a dashboard JWT through `apps.authapi.tokens.verify_token`.

    This used to decode the token itself with no leeway (clock skew rejected valid tokens;
    docs/STATUS.md B13). The policy - EdDSA only, issuer pinned, `exp` and `iss` required
    and correctly typed (CVE-2026-25537), 60 s leeway - is now one function, pinned on
    this entry point by tests/test_jwt_one_verifier.py.

    `verify_aud` is OFF: the audience is an EXTRACTOR
    decision, applied by the caller below, because the same verify serves the peer and
    dashboard extractors.
    """
    public_key_pem = _setting("MESHY_JWT_PUBLIC_KEY_PEM")
    issuer = _setting("MESHY_JWT_ISSUER")
    if not public_key_pem or not issuer:
        raise errors.internal("JWT verification key or issuer is not configured")
    try:
        return tokens.verify_token(token, public_key_pem, issuer)
    except JwtError as exc:
        raise errors.auth_invalid(f"token invalid: {exc}")


def _from_dev_jwt(conn, request, token: Optional[str]) -> AuthenticatedOwner:
    """Resolve a Bearer JWT as an authenticated owner."""
    if not bearer_jwt_allowed():
        # AuthMissing, not Forbidden. The gate's answer is
        # "this server has no such credential", not "your credential was rejected".
        raise errors.auth_missing()
    if token is None:
        if request.headers.get("Authorization") is None:
            raise errors.auth_missing()
        raise errors.auth_invalid("not a Bearer token")

    claims = verify_dashboard_jwt(token)

    # 403, not 401: the token verified, it is for another
    # audience. A peer token reaching a dashboard route is exactly this case.
    if claims.get("aud") != DASHBOARD_AUDIENCE:
        raise errors.forbidden()

    sub = claims.get("sub")
    try:
        owner_id = uuid.UUID(str(sub))
    except (ValueError, TypeError, AttributeError):
        raise errors.auth_invalid("token subject is not a uuid")

    # The Bearer fallback must not mint an OwnerId for a sub
    # with no dashboard_users row. Without this a forged-sub dev JWT resolves to a
    # non-existent owner and surfaces as a 500 downstream instead of a clean 401 here.
    # It is also what keeps `vis` from being computed for an owner that cannot exist.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM dashboard_users WHERE id = %(id)s) AS found",
                    {"id": owner_id})
        found = cur.fetchone()
    if found is None or "found" not in found:
        raise errors.internal("owner status lookup returned no row")
    if not found["found"]:
        raise errors.auth_invalid("unknown owner")

    return AuthenticatedOwner(owner_id=owner_id, method="dev_jwt", claims=claims)


def authenticate_owner(conn, request) -> AuthenticatedOwner:
    """The extractor. `conn` is used for real lookups - the credential is never the
    last word on whether the session, key or owner still exists."""
    cookie_owner = _from_cookie(conn, request)
    if cookie_owner is not None:
        cookie_owner.require_csrf(request)
        return cookie_owner

    token = _bearer(request)
    if token is not None and token.startswith(PAT_PREFIX):
        return _from_pat(conn, token)

    return _from_dev_jwt(conn, request, token)
