"""The auth endpoints.

    POST /v1/auth/challenge
    POST /v1/auth/sign-in
    POST /v1/auth/owner/challenge
    POST /v1/auth/owner/sign-in
    GET  /v1/auth/me
    POST /v1/auth/refresh
    GET  /.well-known/jwks.json
    GET  /.well-known/meshy-invoke-jwks.json   doorbell key (041 S9)

The pairing-code pair (`/v1/auth/onetime`, `/v1/onboard/peers`) lives in `onboard.py`.

The JWKS route is mounted at the root, so the well-known path has no /v1 prefix.

TWO PROPERTIES THAT LOOK LIKE DETAILS AND ARE NOT:

1. UNIFORM RESPONSES. `/v1/auth/challenge` answers identically for a peer that exists
   and one that does not — it mints a nonce either way and only STORES it when the peer
   exists. `/v1/auth/sign-in` returns one 401 for all six of its failure modes. Both
   behaviors ensure neither route is a
   peer-existence oracle. A "helpful" error message here is a security regression.

2. SINGLE-USE NONCES ARE ENFORCED BY THE DATABASE, NOT BY A READ-THEN-WRITE. The
   consume is one `UPDATE ... WHERE consumed_at IS NULL AND expires_at > NOW()
   RETURNING peer_id`. A row comes back only if the challenge
   existed, was unconsumed and was unexpired, all decided atomically. A SELECT followed
   by an UPDATE would let two concurrent sign-ins both pass the SELECT, which is exactly
   the replay this is defending against.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import uuid

import psycopg
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.authapi import errors
from apps.authapi.config import (OWNER_SESSION_TTL_SECS, OWNER_SIGNIN_PURPOSE,
                                 SIGNIN_PURPOSE, get_config)
from apps.authapi.db import connection
from apps.authapi.didkey import to_did_key
from apps.authapi.onboard import _URL_SAFE, _strict_b64, is_edwards_point
from apps.authapi.ratelimit import redeem_throttle, resolve_client_ip, sig_auth_rate_check
from apps.authapi.request import _Refusal, _body, _field, _rfc3339, _str_field, _uuid_field
from apps.authapi.tokens import build_claims, jwks_document, sign
from apps.core import dbconn, env, websec
from apps.crypto.primitives import b64u, b64u_decode, jcs, verify_strict
from apps.dashboard import auth as owner_auth
from apps.dashboard import errors as owner_errors
from apps.peers import keys as peer_keys
from apps.peers import names as peer_names
from apps.peers.timefmt import format_rfc3339

log = logging.getLogger("meshychat.authapi")


def _peer_exists_active(conn, peer_id) -> bool:
    """`status = 'active'` is part of the predicate."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM peers WHERE id = %s AND status = 'active')",
            (peer_id,),
        )
        return cur.fetchone()[0]


def _find_active_key(conn, peer_id):
    """Find the active key for a peer.

    The ORDER BY is load-bearing, not tidiness: migration 030 dropped the one-active-
    key-per-peer unique index so a rotation can hold two active rows transiently, and
    without a deterministic order this security-relevant lookup would return whichever
    row Postgres streamed first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kid, public_key FROM peer_keys "
            "WHERE peer_id = %s AND active = TRUE AND revoked_at IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (peer_id,),
        )
        return cur.fetchone()


def _raw_public_key(public_key_b64u: str) -> bytes | None:
    """peer_keys.public_key is unpadded base64url of the raw 32 bytes.
    Anything else is a rejection, not an exception."""
    try:
        raw = b64u_decode(public_key_b64u)
    except Exception:  # noqa: BLE001
        return None
    return raw if len(raw) == 32 else None


def _consume_challenge_and_verify(conn, *, peer_id, nonce: str, signature_b64: str,
                                  aud: str, purpose: str) -> tuple[bytes, str] | None:
    """Steps (a)-(c) of both sign-ins, which differ
    only in `aud` and `purpose`. Returns (raw public key, its stored base64url string) or
    None. None is deliberately ONE answer for every failure - consumed, expired or unknown
    nonce, no active key, a bad stored key, a bad signature - so each route maps it to its
    single 401 and neither becomes a peer-existence oracle (HARDENING-4)."""
    # (a) Atomic single-use consume, FIRST: a request that then fails - wrong key, bad
    # signature - has still burned its nonce, so a captured request never succeeds twice.
    # The WHERE clause is the whole replay defence.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_challenges SET consumed_at = NOW() "
            "WHERE peer_id = %s AND nonce = %s "
            "  AND consumed_at IS NULL AND expires_at > NOW() "
            "RETURNING peer_id",
            (peer_id, nonce),
        )
        if cur.fetchone() is None:
            return None

    # (b) The peer's ACTIVE key — the same lookup key-publish and the receiver use.
    active = _find_active_key(conn, peer_id)
    if active is None:
        return None
    pubkey = _raw_public_key(active[1])
    if pubkey is None:
        return None

    # (c) The purpose-bound canonical payload. JCS (RFC 8785) sorts the keys, so
    # the client must canonicalize the SAME object: {aud, nonce, peer_id, purpose}
    # `purpose` is what stops a sign-in signature being
    # replayed as a key-publish PoP or an owner-session mint.
    canonical = jcs({
        "aud": aud,
        "nonce": nonce,
        "peer_id": str(peer_id),
        "purpose": purpose,
    })
    try:
        signature = b64u_decode(signature_b64)
    except Exception:  # noqa: BLE001
        return None
    if len(signature) != 64 or not verify_strict(pubkey, canonical, signature):
        return None
    return pubkey, active[1]


@csrf_exempt
@require_http_methods(["POST"])
def challenge(request):
    """POST /v1/auth/challenge — a single-use nonce for a peer to sign."""
    return _issue_challenge(request, "challenge")


def _issue_challenge(request, limiter_route: str):
    """The body of both challenge routes. `owner_challenge` uses the same table, TTL, and
    uniform answer - except for the limiter bucket name. The two share `auth_challenges`,
    so a nonce from either route can be consumed by either sign-in; what stops a peer
    sign-in signature being replayed as an owner session is the purpose-bound payload,
    not the route that issued the nonce."""
    cfg = get_config()
    payload, err = _body(request)
    if err:
        return err
    peer_id, err = _uuid_field(payload, "peer_id")
    if err:
        return err

    ip = resolve_client_ip(request, behind_trusted_proxy=cfg.behind_trusted_proxy)
    if not sig_auth_rate_check(limiter_route, ip, str(peer_id)):
        return errors.rate_limited()

    nonce = b64u(secrets.token_bytes(32))
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=cfg.challenge_ttl_secs)

    with connection() as conn:
        # Mint-and-store for a peer that exists, mint-and-discard otherwise. The
        # response content is identical either way; the residual timing difference is
        # bounded by the rate
        # limiter above, not papered over here.
        if _peer_exists_active(conn, peer_id):
            with conn.cursor() as cur:
                # Opportunistic prune: without it an INSERT-per-call grows the table
                # forever — a buggy client reconnect loop does it by accident. The prune
                # runs before the insert.
                cur.execute("DELETE FROM auth_challenges WHERE expires_at < NOW()")
                cur.execute(
                    "INSERT INTO auth_challenges (peer_id, nonce, expires_at) "
                    "VALUES (%s, %s, %s) ON CONFLICT (peer_id, nonce) DO NOTHING",
                    (peer_id, nonce, expires_at),
                )

    return JsonResponse({"nonce": nonce, "expires_at": _rfc3339(expires_at)})


@csrf_exempt
@require_http_methods(["POST"])
def sign_in(request):
    """POST /v1/auth/sign-in — verify the peer's signature over the challenge with its
    ACTIVE key, then mint a short aud=peer JWT. No refresh token is issued: the node
    re-mints by re-signing a fresh challenge, so the signing key is the sole recovery
    root and there is no long-lived bearer credential to revoke or race."""
    cfg = get_config()
    payload, err = _body(request)
    if err:
        return err
    peer_id, err = _uuid_field(payload, "peer_id")
    if err:
        return err
    nonce, err = _str_field(payload, "nonce")
    if err:
        return err
    signature_b64, err = _str_field(payload, "signature_b64")
    if err:
        return err

    ip = resolve_client_ip(request, behind_trusted_proxy=cfg.behind_trusted_proxy)
    if not sig_auth_rate_check("signin", ip, str(peer_id)):
        return errors.rate_limited()

    # Checked before the challenge is consumed: a server that
    # cannot mint must not burn the caller's single-use nonce on the way to a 503.
    # No issuer is the same condition (the project's design log (not part of this release) 015): a token stamped
    # `"iss": null` would be verified by any PyJWT caller
    # that passes issuer=None, because PyJWT skips the issuer check on None.
    if not cfg.jwt_signing_key_pem or not cfg.jwt_issuer:
        return errors.signing_key_unavailable()

    with connection() as conn:
        # (a)-(c): consume, active key, purpose-bound signature.
        verified = _consume_challenge_and_verify(
            conn, peer_id=peer_id, nonce=nonce, signature_b64=signature_b64,
            aud="peer", purpose=SIGNIN_PURPOSE)
        if verified is None:
            return errors.signin_invalid()
        pubkey, _ = verified

        # (d) sub = the authorizing OWNER, act.sub = the acting PEER (TASK-25). The peer
        # just proved control of its key and a peer
        # always belongs to an owner, so this lookup IS that binding. The same row
        # carries the provisioned scopes: minting an empty vector here would strip
        # every scope on the peer's first re-mint (tests/test_auth_flows.py:422 pins this).
        with conn.cursor() as cur:
            cur.execute("SELECT owner_id, scopes FROM peers WHERE id = %s", (peer_id,))
            row = cur.fetchone()
        if row is None:
            return errors.signin_invalid()
        owner_id, scopes = row

    now = dt.datetime.now(dt.timezone.utc)
    exp = now + dt.timedelta(seconds=cfg.signin_jwt_ttl_secs)
    claims = build_claims(
        sub=owner_id,
        iss=cfg.jwt_issuer,
        iat=int(now.timestamp()),
        exp=int(exp.timestamp()),
        scopes=scopes or [],          # NULL scopes => the empty vector, not an error
        aud="peer",
        lin=None,                     # no refresh token, so no lineage
        cnf=to_did_key(pubkey),       # TASK-26: always bound, this mint holds the key
        act_sub=peer_id,
    )
    token = sign(claims, cfg.jwt_signing_key_pem)
    return JsonResponse({"jwt": token, "jwt_expires_at": _rfc3339(exp)})


@csrf_exempt
@require_http_methods(["POST"])
def owner_challenge(request):
    """POST /v1/auth/owner/challenge - a single-use nonce for a peer key to sign when
    minting its owner's dashboard session. Uniform for any
    well-formed peer_id, exactly as the peer challenge; its own limiter bucket
    ("owner-challenge")."""
    return _issue_challenge(request, "owner-challenge")


def _owner_signin_invalid():
    """Use one 401 for every challenge/key/signature failure, with
    this route's own message. Same anti-enumeration reason as `errors.signin_invalid`."""
    return errors.coded(errors.E_AUTH_INVALID,
                        "owner sign-in challenge or signature invalid", 401)


@csrf_exempt
@require_http_methods(["POST"])
def owner_sign_in(request):
    """POST /v1/auth/owner/sign-in: headless owner session.

    A peer signs {aud:"dashboard", nonce, peer_id, purpose:"meshy-owner-signin-v1"} with
    its ACTIVE key. The server binds the peer to its owner and issues an ordinary
    `meshy_session` cookie, the same credential a browser gets, so `/v1/auth/onetime`
    and every other owner route accept it through the one owner extractor. An aud=peer
    JWT is never accepted as owner authority; this is the only bridge, and it needs a
    fresh signature.

    The order is deliberate for two reasons:
      * the nonce is consumed first, so a request that then fails - wrong
        key, bad signature, unregistered key - has still burned it. A replay of a
        captured request can never succeed a second time, whatever else is wrong;
      * the allowlist check comes last: a valid signature is necessary and
        NOT sufficient. The key must also be registered for this owner in
        `owner_human_keys` (TASK-352 Option B), else 403 E_OWNER_KEY_UNREGISTERED.
        Without it one leaked peer key would be full account takeover.

    Unlike the peer sign-in there is NO signing-key check: nothing here mints a JWT.
    """
    cfg = get_config()
    payload, err = _body(request)
    if err:
        return err
    peer_id, err = _uuid_field(payload, "peer_id")
    if err:
        return err
    nonce, err = _str_field(payload, "nonce")
    if err:
        return err
    signature_b64, err = _str_field(payload, "signature_b64")
    if err:
        return err

    ip = resolve_client_ip(request, behind_trusted_proxy=cfg.behind_trusted_proxy)
    if not sig_auth_rate_check("owner-signin", ip, str(peer_id)):
        return errors.rate_limited()

    with dbconn.serving_connection(cfg.dsn) as conn:
        # (a)-(c) as the peer sign-in: the same consume statement and key
        # lookup, the dashboard aud AND purpose, so neither signature verifies as the
        # other. The stored key string is kept for the allowlist in (e).
        verified = _consume_challenge_and_verify(
            conn, peer_id=peer_id, nonce=nonce, signature_b64=signature_b64,
            aud="dashboard", purpose=OWNER_SIGNIN_PURPOSE)
        if verified is None:
            return _owner_signin_invalid()
        _, active_public_key = verified

        with conn.cursor() as cur:
            # (d) `status = 'active'` is in the predicate here as well.
            cur.execute("SELECT owner_id FROM peers WHERE id = %s AND status = 'active'",
                        (peer_id,))
            row = cur.fetchone()
            if row is None:
                return _owner_signin_invalid()
            owner_id = row[0]

            # (e) The allowlist uses the exact stored base64url string and excludes
            # revoked rows in the WHERE clause.
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM owner_human_keys "
                "WHERE owner_id = %s AND public_key = %s AND revoked_at IS NULL)",
                (owner_id, active_public_key),
            )
            if not cur.fetchone()[0]:
                return errors.coded(
                    errors.E_OWNER_KEY_UNREGISTERED,
                    "this peer key is not registered for owner sign-in; an owner must "
                    "register it via POST /v1/owner/human-keys before it can mint an "
                    "owner session",
                    403,
                )

            # (f) Generate a 64-hex token and store only its hex SHA-256; the raw value
            # exists only in the cookie. The TTL is 86400 when no SSO service is
            # configured, which is always the case here.
            raw_token = uuid.uuid4().hex + uuid.uuid4().hex
            expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=OWNER_SESSION_TTL_SECS)
            cur.execute(
                "INSERT INTO owner_sessions (owner_id, expires_at, user_agent, ip_inet, "
                "token_hash) VALUES (%s, %s, %s, NULL, %s) RETURNING expires_at",
                (owner_id, expires, request.headers.get("User-Agent"),
                 hashlib.sha256(raw_token.encode("utf-8")).hexdigest()),
            )
            session_expires_at = cur.fetchone()[0]

    response = JsonResponse({"owner_id": str(owner_id),
                             "session_expires_at": _rfc3339(session_expires_at)})
    # The cookie is Path=/, HttpOnly, Secure unless a debug build, and SameSite=Lax; a
    # cross-site dashboard uses None plus Secure (MESHY_DASHBOARD_CROSS_SITE).
    # The NAME comes from the function the owner extractor reads it with, so issue and
    # lookup cannot disagree.
    cross_site = env.env_flag("MESHY_DASHBOARD_CROSS_SITE", "MESHYCHAT_DASHBOARD_CROSS_SITE")
    max_age = max(0, int((session_expires_at - dt.datetime.now(dt.timezone.utc))
                         .total_seconds()))
    response.set_cookie(
        owner_auth.session_cookie_name(), raw_token, max_age=max_age, path="/",
        httponly=True,
        secure=(not getattr(settings, "DEBUG", False)) or cross_site,
        samesite="None" if cross_site else "Lax",
    )
    return response


def _me_entitlements(conn, owner_id):
    """Best effort: a failed entitlement read degrades to null rather than breaking /me, which
    the dashboard needs to render anything. Stale is reported as INACTIVE, never omitted:
    omitting reads to a client as "no Accounts record, use tier", the opposite of what a
    passed deadline means. The authoritative gate is server-side (apps/core/entitlements)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT active, capabilities, principal_capabilities, recheck_at "
                "FROM accounts_entitlements WHERE owner_id = %s", (owner_id,))
            row = cur.fetchone()
    except psycopg.Error as exc:
        log.warning("/v1/auth/me: could not read cached entitlements: %s", exc)
        return None
    if row is None:
        return None                                                  # Absent
    active, capabilities, principal_capabilities, recheck_at = row
    now = dt.datetime.now(dt.timezone.utc)
    if recheck_at <= now:                                            # Stale
        return {"active": False, "capabilities": [], "principal_capabilities": [],
                "recheck_at": _rfc3339(now)}
    return {"active": active, "capabilities": list(capabilities),
            "principal_capabilities": list(principal_capabilities),
            "recheck_at": _rfc3339(recheck_at)}


@require_http_methods(["GET", "HEAD"])
def me(request):
    """GET /v1/auth/me. Cookie only: no PAT or Bearer fallback, so no cookie is
    401 E_AUTH_MISSING and a cookie naming no live session is 401 E_SESSION_EXPIRED.

    This route lets a cookie session learn its CSRF token, and `POST /v1/auth/onetime`
    refuses a cookie write without one. A
    headless owner that signed in at `/v1/auth/owner/sign-in` had no way to mint a code
    without this route. The token is computed by the same function, from the same
    secret, that the owner extractor checks it with (apps/core/websec.py,
    apps/dashboard/auth.py:state_secret), so issue and check cannot disagree.
    """
    cfg = get_config()
    raw = request.COOKIES.get(owner_auth.session_cookie_name())
    if not raw:
        return errors.coded(errors.E_AUTH_MISSING, "missing auth", 401)
    with dbconn.serving_connection(cfg.dsn) as conn:
        with conn.cursor() as cur:
            # Expiry is part of the lookup, so an expired row is simply not found.
            cur.execute(
                "SELECT id, owner_id, expires_at FROM owner_sessions "
                "WHERE token_hash = %s AND expires_at > NOW()",
                (hashlib.sha256(raw.encode("utf-8")).hexdigest(),))
            session = cur.fetchone()
            if session is None:
                return errors.coded(errors.E_SESSION_EXPIRED, "session not active", 401)
            session_id, owner_id, session_expires_at = session
            cur.execute(
                "SELECT email, tier, is_admin, onboarding_dismissed_at "
                "FROM dashboard_users WHERE id = %s", (owner_id,))
            row = cur.fetchone()
        if row is None:
            # The session lookup requires an owner row. The FK from owner_sessions makes it
            # unreachable short of a concurrent delete; if it happens, a 500 is the intended
            # answer.
            raise RuntimeError("auth/me: session names an owner with no dashboard_users row")
        email, tier, is_admin, onboarding_dismissed_at = row
        entitlements = _me_entitlements(conn, owner_id)
    secret = owner_auth.state_secret()
    # Key order is part of the response contract.
    return JsonResponse({
        "owner_id": str(owner_id),
        "email": email,
        "tier": tier,
        "is_admin": is_admin,
        "session_expires_at": _rfc3339(session_expires_at),
        "onboarding_dismissed_at": (None if onboarding_dismissed_at is None
                                    else _rfc3339(onboarding_dismissed_at)),
        # None only when no state secret is configured; the CSRF check then refuses
        # cookie writes outside DEBUG, so a null here is not permission.
        "csrf_token": websec.csrf_token_for(session_id, secret) if secret else None,
        "entitlements": entitlements,
    })


def _sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


@csrf_exempt
@require_http_methods(["POST"])
def refresh(request):
    """POST /v1/auth/refresh — rotate a refresh token into a fresh JWT + new refresh
    token, revoking the consumed one in the same transaction.
    If anything fails the rotation rolls back as a unit and the client can retry with
    the original token."""
    cfg = get_config()
    payload, err = _body(request)
    if err:
        return err
    inbound, err = _str_field(payload, "refresh_token")
    if err:
        return err

    if not cfg.jwt_signing_key_pem or not cfg.jwt_issuer:   # divergence 015, as in sign_in
        return errors.signing_key_unavailable()

    inbound = inbound.strip()
    if not inbound:
        return errors.coded(errors.E_REFRESH_TOKEN_INVALID, "missing refresh token", 401)
    inbound_hash = _sha256(inbound)

    with connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            # The JOIN is the peer-key revocation check: a refresh token whose peer key
            # was revoked produces NO row, so a revoked key kills the rotation chain.
            # `issued_under_kid IS NULL` keeps pre-migration tokens working.
            cur.execute(
                "SELECT rt.peer_id, rt.owner_id, rt.expires_at, rt.revoked_at, "
                "       rt.issued_under_kid, rt.lineage_id "
                "FROM refresh_tokens rt "
                "JOIN peer_keys pk ON pk.peer_id = rt.peer_id "
                "  AND pk.active = TRUE AND pk.revoked_at IS NULL "
                "  AND (pk.kid = rt.issued_under_kid OR rt.issued_under_kid IS NULL) "
                "WHERE rt.token_hash = %s "
                "FOR UPDATE OF rt",
                (inbound_hash,),
            )
            row = cur.fetchone()
        if row is None:
            return errors.coded(
                errors.E_REFRESH_TOKEN_INVALID,
                "refresh token not found or peer key revoked", 401)
        peer_id, owner_id, expires_at, revoked_at, issued_under_kid, lineage_id = row
        now = dt.datetime.now(dt.timezone.utc)
        if revoked_at is not None:
            return errors.coded(errors.E_REFRESH_TOKEN_INVALID, "refresh token revoked", 401)
        if expires_at <= now:
            return errors.coded(errors.E_REFRESH_TOKEN_INVALID, "refresh token expired", 401)

        new_refresh = b64u(secrets.token_bytes(32))
        new_expires_at = now + dt.timedelta(days=cfg.refresh_token_ttl_days)
        with conn.cursor() as cur:
            # INHERIT the consumed token's lineage_id (migration 031): the rotation
            # chain is ONE session. Letting the column DEFAULT start a fresh lineage
            # here would strand the peer's key-publish re-anchor.
            cur.execute(
                "INSERT INTO refresh_tokens "
                "  (token_hash, peer_id, owner_id, expires_at, issued_under_kid, lineage_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (_sha256(new_refresh), peer_id, owner_id, new_expires_at,
                 issued_under_kid, lineage_id),
            )
            cur.execute(
                "UPDATE refresh_tokens SET revoked_at = NOW() WHERE token_hash = %s",
                (inbound_hash,),
            )
            cur.execute("SELECT scopes FROM peers WHERE id = %s", (peer_id,))
            scopes = cur.fetchone()[0]

        # TASK-26: re-derive `cnf` on every rotation. WITHOUT this, a peer holding a
        # cnf-bound token could rotate into a cnf-less one and SHED its DPoP binding —
        # a downgrade bypass the moment enforcement is switched on.
        active = _find_active_key(conn, peer_id)
        raw = _raw_public_key(active[1]) if active else None
        cnf = to_did_key(raw) if raw else None

        exp = now + dt.timedelta(seconds=cfg.peer_jwt_ttl_secs)
        claims = build_claims(
            sub=owner_id,
            iss=cfg.jwt_issuer,
            iat=int(now.timestamp()),
            exp=int(exp.timestamp()),
            scopes=scopes or [],
            aud="peer",
            lin=lineage_id,      # stamped so a later key-publish re-anchors THIS session
            cnf=cnf,
            act_sub=peer_id,
        )
        # Minted inside the transaction so a signing failure rolls back the new row.
        token = sign(claims, cfg.jwt_signing_key_pem)

    return JsonResponse({
        "jwt": token,
        "jwt_expires_at": _rfc3339(exp),
        "refresh_token": new_refresh,
    })


@require_http_methods(["GET"])
def server_jwks(request):
    """GET /.well-known/jwks.json — the SERVER key set, public and unauthenticated
    because a verify anchor has to be fetchable without credentials.

    Distinct from the per-peer JWKS at /agents/{peer_id}/jwks.json: push notifications
    are signed by this server, so a caller verifies them against this set, never a
    per-peer one.

    THE KID RULE IS THE SERVER ONE: 16 lowercase HEX chars of the first 8 bytes of
    sha256(raw pubkey).
    A peer key's kid is 16 chars of BASE64URL of the same digest.
    Using the peer rule here yields a kid no verifier can resolve.

    Scope: the current token key only. Rotated-out keys are accepted for invocation-frame
    verification but are not advertised, because adding them would widen the set verifiers
    trust this endpoint for.

    NOT THE DOORBELL KEY (operator decision 2026-09-25, divergence 041's dated paragraph; the
    S9 review's F7). S9 published the invocation doorbell's key here as a second entry; it is
    now published ALONE by `invoke_webhook_jwks` at its own path, so a verifier that trusts
    this set for tokens cannot resolve a doorbell's kid, whatever it checks.
    """
    cfg = get_config()
    keys = [cfg.jwt_public_key_pem] if cfg.jwt_public_key_pem else []
    return JsonResponse(jwks_document(keys))


@require_http_methods(["GET"])
def invoke_webhook_jwks(request):
    """GET /.well-known/meshy-invoke-jwks.json - the invocation doorbell's key set, public and
    unauthenticated (divergence 041 S9; its own path by operator decision 2026-09-25).

    The one key `apps.calls.webhook_key.published_public_pem` gives, under its server-rule kid
    (16 lowercase hex of the first 8 bytes of sha256(raw)), read from its file on each request
    so a rotation is published at once; `{"keys": []}` when this host has no doorbell key (doorbells off, no
    file yet, a file that is not a key, or the token key, which that function refuses). A
    receiver resolves a doorbell's kid here, and ONLY here: /.well-known/jwks.json carries the
    token key alone, and this set never carries it.
    """
    from apps.calls.webhook_key import published_public_pem

    doorbell = published_public_pem()
    return JsonResponse(jwks_document([doorbell] if doorbell is not None else []))


# ------------------------------------------------------ /v1/owner/human-keys (F05)
#
# The owner-sign-in allowlist's management surface: a key
# registered here lets a peer holding it sign in as the owner (`owner_sign_in` above reads
# the same table), so EVERY handler is `require_human` - a PAT, whatever its scopes, is
# refused, because registering a key is itself a path to full owner authority.
#
# Order: the credential, then the body and path parameters, then `require_human`. Status and
# message of every refusal are fixed; clients match on them, so do not reword.

#: Measured in UTF-8 bytes (`len(x.encode("utf-8"))`), not characters, so a short non-ASCII
#: value can still exceed the limit.
MAX_HUMAN_KID_LEN = 128
MAX_HUMAN_LABEL_LEN = 256


def _human_key_error(message: str) -> JsonResponse:
    return errors.coded(errors.E_VALIDATION, message, 400)


def _validate_human_public_key(text: str) -> JsonResponse | None:
    """Require strict URL_SAFE_NO_PAD, 32 bytes, and a curve point, each with its own message."""
    raw = _strict_b64(text, _URL_SAFE, b"-_", False)
    if raw is None:
        return _human_key_error("public_key must be base64url-no-pad")
    if len(raw) != 32:
        return _human_key_error("public_key must decode to 32 bytes")
    if not is_edwards_point(raw):
        return _human_key_error("public_key is not a valid Ed25519 point")
    return None


def _human_key_view(row) -> dict:
    """Return human-key metadata only, never key bytes."""
    kid, label, created_at, revoked_at = row
    return {"kid": kid, "label": label, "created_at": format_rfc3339(created_at),
            "revoked_at": None if revoked_at is None else format_rfc3339(revoked_at)}


@csrf_exempt
@require_http_methods(["POST", "GET"])
def human_keys(request):
    """POST (register, 201) and GET (list, 200) /v1/owner/human-keys."""
    cfg = get_config()
    try:
        with dbconn.serving_connection(cfg.dsn) as conn:
            owner = owner_auth.authenticate_owner(conn, request)
            if request.method == "GET":
                owner.require_human()
                with conn.cursor() as cur:
                    cur.execute("SELECT kid, label, created_at, revoked_at "
                                "FROM owner_human_keys WHERE owner_id = %s "
                                "ORDER BY created_at, kid", (owner.owner_id,))
                    rows = cur.fetchall()
                return JsonResponse([_human_key_view(r) for r in rows], safe=False)

            payload, err = _body(request)                                 # JSON request body
            if err:
                return err
            kid_raw, err = _str_field(payload, "kid")
            if err:
                return err
            public_key, err = _str_field(payload, "public_key")
            if err:
                return err
            label_raw, err = _field(payload, "label", required=False)
            if err:
                return err
            owner.require_human()

            kid = kid_raw.strip()
            if not kid:
                return _human_key_error("kid must not be empty")
            if len(kid.encode("utf-8")) > MAX_HUMAN_KID_LEN:
                return _human_key_error(
                    f"kid must be at most {MAX_HUMAN_KID_LEN} characters")
            label = label_raw.strip() if label_raw is not None else None
            label = label or None
            if label is not None and len(label.encode("utf-8")) > MAX_HUMAN_LABEL_LEN:
                return _human_key_error(
                    f"label must be at most {MAX_HUMAN_LABEL_LEN} characters")
            bad = _validate_human_public_key(public_key)
            if bad:
                return bad
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO owner_human_keys (owner_id, kid, public_key, "
                                "label) VALUES (%s, %s, %s, %s)",
                                (owner.owner_id, kid, public_key, label))
            except psycopg.errors.UniqueViolation:
                return errors.coded(owner_errors.E_CONFLICT,
                                    "a human key with this kid is already registered for "
                                    "this owner", 409)
    except owner_errors.ApiError as exc:
        return JsonResponse(exc.body(), status=exc.status)
    return JsonResponse({"kid": kid, "label": label}, status=201)


@csrf_exempt
@require_http_methods(["DELETE"])
def revoke_human_key(request, kid):
    """DELETE /v1/owner/human-keys/{kid}: 204 when an active key of this owner was revoked, else 404
    (unknown, another owner's or already revoked are one answer, no existence oracle)."""
    cfg = get_config()
    try:
        with dbconn.serving_connection(cfg.dsn) as conn:
            owner = owner_auth.authenticate_owner(conn, request)
            owner.require_human()
            with conn.cursor() as cur:
                cur.execute("UPDATE owner_human_keys SET revoked_at = NOW() "
                            "WHERE owner_id = %s AND kid = %s AND revoked_at IS NULL "
                            "RETURNING kid", (owner.owner_id, kid))
                revoked = cur.fetchone() is not None
    except owner_errors.ApiError as exc:
        return JsonResponse(exc.body(), status=exc.status)
    if not revoked:
        exc = owner_errors.not_found()
        return JsonResponse(exc.body(), status=exc.status)
    return HttpResponse(status=204)


# ------------------------------------------------ POST /v1/auth/redeem-invitation (F05)
#
# No credential: the invitation code is the authorisation, and a
# redeem CREATES AN OWNER (a synthetic mentee owner, its peer, the lesson subscription to
# the mentor). It is bounded by the invitation: a peer mints one at `POST /v1/invites`
# (apps/peers/views.py mint_invite, 2026-10-03, F35), and until one exists this
# route can only refuse. One transaction with the invitation row `FOR UPDATE`, so two redeems
# cannot both take the last seat, and every refusal after the lock rolls everything back.

E_INVITATION_INVALID = "E_INVITATION_INVALID"
E_INVITATION_EXPIRED = "E_INVITATION_EXPIRED"
E_INVITATION_CONSUMED = "E_INVITATION_CONSUMED"
E_MENTOR_KEY_MISSING = "E_MENTOR_KEY_MISSING"
#: Lifetimes of the credentials a redeemed invitation issues.
INVITE_PEER_JWT_TTL_SECS = 3600
INVITE_REFRESH_TOKEN_TTL_DAYS = 30


@csrf_exempt
@require_http_methods(["POST"])
def redeem_invitation(request):
    # B16: the per-client-IP budget, before the body is read and before the code is looked
    # up, so a guess refused here never reaches the database (apps/core/redeem_limit).
    throttled = redeem_throttle(request)
    if throttled is not None:
        return throttled
    payload, err = _body(request)                              # JSON request body
    if err:
        return err
    raw_code, err = _str_field(payload, "code")
    if err:
        return err
    asked_name, err = _field(payload, "display_name", required=False)
    if err:
        return err

    cfg = get_config()
    # Before the code is read, so an unconfigured server never spends a seat. No
    # issuer is the same condition (divergence 015).
    if not cfg.jwt_signing_key_pem or not cfg.jwt_issuer:
        return errors.signing_key_unavailable()
    code = raw_code.strip().lower()
    if not code:
        return errors.coded(E_INVITATION_INVALID, "missing invitation code", 404)

    try:
        with dbconn.serving_connection(cfg.dsn) as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT id, issuer_peer_id, owner_handle, expires_at, max_uses, "
                            "uses FROM invitations WHERE LOWER(code) = %s FOR UPDATE",
                            (code,))
                row = cur.fetchone()
            if row is None:
                raise _Refusal(errors.coded(E_INVITATION_INVALID, "invitation not found",
                                            404))
            inv_id, mentor_peer_id, owner_handle, expires_at, max_uses, uses = row
            now = dt.datetime.now(dt.timezone.utc)
            if expires_at is not None and expires_at <= now:
                raise _Refusal(errors.coded(E_INVITATION_EXPIRED, "invitation expired", 410))
            if uses >= max_uses:
                raise _Refusal(errors.coded(E_INVITATION_CONSUMED,
                                            "invitation already consumed", 410))

            with conn.cursor() as cur:
                # The peer's name derives from the owner id, so divergence 065's door draws the
                # id afresh if that name is already some peer's (or the roster's).
                drawn: list = []

                def _mentee():
                    drawn.append(uuid.uuid4())
                    return f"mentee-{drawn[-1].hex[:12]}"

                peer_names.draw(cur, _mentee)
                owner_id = drawn[-1]
                short = owner_id.hex[:12]
                display_name = asked_name or owner_handle or f"mentee-{short}"
                cur.execute("INSERT INTO dashboard_users (id, email, display_name, role) "
                            "VALUES (%s, %s, %s, 'owner')",
                            (owner_id, f"mentee-{short}@invite.meshy", display_name))
                cur.execute("INSERT INTO peers (owner_id, name, display_name) "
                            "VALUES (%s, %s, %s) RETURNING id",
                            (owner_id, f"mentee-{short}", display_name))
                mentee_peer_id = cur.fetchone()[0]
                cur.execute("INSERT INTO lesson_subscriptions (mentor_peer_id, "
                            "mentee_peer_id, mentee_owner_id) VALUES (%s, %s, %s)",
                            (mentor_peer_id, mentee_peer_id, owner_id))
                cur.execute("UPDATE invitations SET uses = uses + 1, "
                            "redeemed_at = COALESCE(redeemed_at, NOW()), "
                            "redeemed_by_owner = COALESCE(redeemed_by_owner, %s) "
                            "WHERE id = %s", (owner_id, inv_id))

            mentor_key = peer_keys.find_active(conn, mentor_peer_id)
            if mentor_key is None:
                raise _Refusal(errors.coded(E_MENTOR_KEY_MISSING,
                                            "mentor has no active signing key", 422))

            issued_at = dt.datetime.now(dt.timezone.utc)
            exp = issued_at + dt.timedelta(seconds=INVITE_PEER_JWT_TTL_SECS)
            claims = build_claims(
                sub=owner_id,             # TASK-25: the synthesized mentee owner
                iss=cfg.jwt_issuer,
                iat=int(issued_at.timestamp()),
                exp=int(exp.timestamp()),
                scopes=[],                # deliberately empty
                aud="peer",
                lin=None,
                cnf=None,                 # the mentee is keyless at redeem
                act_sub=mentee_peer_id,
            )
            token = sign(claims, cfg.jwt_signing_key_pem)

            refresh_token = b64u(secrets.token_bytes(32))
            with conn.cursor() as cur:
                # issued_under_kid NULL: the mentee has no key yet.
                cur.execute("INSERT INTO refresh_tokens (token_hash, peer_id, owner_id, "
                            "expires_at, issued_under_kid) VALUES (%s, %s, %s, %s, NULL)",
                            (hashlib.sha256(refresh_token.encode("utf-8")).digest(),
                             mentee_peer_id, owner_id,
                             issued_at + dt.timedelta(days=INVITE_REFRESH_TOKEN_TTL_DAYS)))
    except _Refusal as refusal:
        return refusal.response

    return JsonResponse({
        "peer_id": str(mentee_peer_id),
        "owner_id": str(owner_id),
        "jwt": token,
        "refresh_token": refresh_token,
        "mentor_peer_id": str(mentor_peer_id),
        "mentor_public_key": mentor_key.public_key,
        "mentor_kid": mentor_key.kid,
        "jwt_expires_at": format_rfc3339(exp),
    }, status=201)
