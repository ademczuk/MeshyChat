"""HTTP surface for peers, presence and agent cards.

The routes use these status codes:
  POST /v1/peers/me/keys        -> 201 Created
  DELETE /v1/peers/me/keys/{kid}-> 204 No Content
  POST /v1/peers/me/agent-card  -> 204 No Content
  POST /v1/peers/me/presence    -> 200 {"ok": true}

AUTHENTICATION IS PER-ROUTE, NOT GLOBAL. The two `/agents/{peer_id}/...` routes are
mounted publicly because public keys are public and
an external A2A verifier holds no authenticated JWT. Everything else is on the authenticated
peer router. Getting this backwards in either direction is a real defect: an
authenticated JWKS route would make published cards unverifiable from outside.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from psycopg.types.json import Jsonb

# Module-level only for what importing this module already loaded (`import psycopg` loads
# its json types; settings imports apps.authapi.config; .auth imports tokens and didkey;
# .keys imports .timefmt), so hoisting them runs no new module. apps.dashboard.auth,
# apps.authapi.onboard, apps.files.quota and .names stay function-local below: importing
# them here would run those modules during import.
from apps.authapi.config import get_config
from apps.authapi.didkey import to_did_key
from apps.authapi.tokens import build_claims, sign
from apps.calls import webhooks
from apps.wsgw.revocation import notify as notify_revocation

from . import cards, db, errors, keys, presence
from .auth import authenticate_peer
from .timefmt import format_rfc3339


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.validation(f"body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise errors.validation("body must be a JSON object")
    return body


def _route(*allowed):
    """Open a `meshychat_app` autocommit connection, answer a method outside `allowed` 405,
    run the handler, map ApiError to a response. Every route runs on its own connection,
    so no request can inherit another's transaction-local context.

    The 405 is answered BEFORE the connection opens (#129, STATUS C73): a wrong method
    costs no connect, and answers 405 even when the
    database is unreachable."""
    def decorate(handler):
        def wrapper(request, *args, **kwargs):
            if request.method not in allowed:
                return HttpResponse(status=405, headers={"Allow": ", ".join(allowed)})
            try:
                with db.connection() as conn:
                    return handler(request, conn, *args, **kwargs)
            except errors.ApiError as exc:
                return JsonResponse(exc.body(), status=exc.status)
        wrapper.__name__ = getattr(handler, "__name__", "route")
        return csrf_exempt(wrapper)
    return decorate


def _cosig_header(request):
    """Return the `X-Key-CoSig` value, None when absent; a non-ASCII
    one is 401, not a 400. Each caller reads it at its own point in its check order."""
    cosig = request.headers.get("X-Key-CoSig")
    if cosig is not None:
        try:
            cosig.encode("ascii")
        except UnicodeEncodeError:
            raise errors.auth_invalid("X-Key-CoSig must be ASCII")
    return cosig


def _published_key(key) -> JsonResponse:
    """Return 201 with {kid, active, created_at}, not the full view."""
    return JsonResponse({"kid": key.kid, "active": key.active,
                         "created_at": key.view()["created_at"]}, status=201)


def _peer_uuid(raw):
    """Parse a `{peer_id}` path segment.

    A malformed id is a 400 (checked after authentication), not a 404: 404 would tell a
    client the peer does not exist when the id was never well-formed. Every error from
    this app uses the JSON `{error, message}` shape.
    """
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise errors.validation(f"invalid peer id: {raw}")


# ----------------------------------------------------------------- presence

@_route("POST")
def presence_beat(request, conn):
    """POST /v1/peers/me/presence."""
    auth = authenticate_peer(conn, request)
    body = _json_body(request)
    return JsonResponse(presence.beat(conn, auth.peer_id, body), status=200)


@_route("POST")
def turn_report(request, conn):
    """POST /v1/peers/me/turn: token-bound advisory turn, never a presence beat."""
    from . import turn
    auth = authenticate_peer(conn, request)
    return JsonResponse(turn.report(conn, auth.peer_id, _json_body(request)), status=200)


@_route("GET")
def presence_list(request, conn):
    """GET /v1/peers/presence. Authenticated, but every peer sees every row."""
    authenticate_peer(conn, request)
    payload = presence.list_presence(
        conn,
        stale_after_secs=request.GET.get("stale_after_secs"),
        active_after_secs=request.GET.get("active_after_secs"),
    )
    return JsonResponse(payload, status=200)


# --------------------------------------------------------------------- keys

@_route("POST", "GET")
def my_keys(request, conn):
    """POST (publish/rotate) + GET (list) /v1/peers/me/keys."""
    auth = authenticate_peer(conn, request)

    if request.method == "GET":
        # Return ALL keys, active and revoked.
        return JsonResponse([k.view() for k in keys.list_for_peer(conn, auth.peer_id)],
                            status=200, safe=False)

    # Read the co-signature after the token and before the body.
    cosig = _cosig_header(request)

    body = _json_body(request)
    # owner_scoped=False on the PEER route, so a first (bootstrap)
    # publish is refused here and must use the owner-scoped route.
    key = keys.publish(conn, auth.peer_id, body, cosig, owner_scoped=False,
                       caller_lineage=auth.lin)
    return _published_key(key)


@_route("DELETE")
def revoke_my_key(request, conn, kid):
    """DELETE /v1/peers/me/keys/{kid}.

    `force_immediate=True`: the self-service revoke is immediate, with no grace window.
    That is the opposite of what a rotation does to the superseded
    key, and the difference is deliberate - a peer revoking a key it believes is
    compromised must not leave it usable for another 24 hours.
    """
    auth = authenticate_peer(conn, request)
    # One transaction: the revoke and the notification that closes the sockets bound to
    # the key commit together (STATUS B15; divergence 030). A lost
    # notification is recovered by the bridge's sweep, which re-reads every bound key.
    with conn.transaction():
        keys.revoke(conn, auth.peer_id, kid, force_immediate=True)
        notify_revocation(conn, auth.peer_id, kid=kid)
    return HttpResponse(status=204)


@_route("GET")
def peer_active_key(request, conn, peer_id):
    """GET /v1/peers/{peer_id}/keys/active. 404 when none."""
    authenticate_peer(conn, request)
    key = keys.find_active(conn, _peer_uuid(peer_id))
    if key is None:
        raise errors.not_found()
    return JsonResponse(key.view(), status=200)


# --------------------------------------------------------------- agent cards

@_route("GET")
def jwks(request, conn, peer_id):
    """GET /agents/{peer_id}/jwks.json. PUBLIC.

    THE ACTIVE KEY ONLY. A key inside its 24h rotation grace window is NOT served
    here even though it still verifies. That asymmetry is intentional: discovery should
    point at the current key, while verification tolerates the recent one.

    An undecodable stored key yields an EMPTY key set rather than an error
    and a peer with no key yields `{"keys": []}` - 200 either way.
    """
    key = keys.find_active(conn, _peer_uuid(peer_id))
    entries = []
    if key is not None:
        try:
            entries = [(keys.decode_public_key(key.public_key), key.kid)]
        except errors.ApiError:
            entries = []
    return JsonResponse(cards.build_jwks(entries), status=200)


@_route("GET")
def serve_card(request, conn, peer_id):
    """GET /agents/{peer_id}/agent-card.json. PUBLIC, 404 when
    the peer has published nothing."""
    with conn.cursor() as cur:
        cur.execute("SELECT card FROM agent_cards WHERE peer_id = %s", (str(_peer_uuid(peer_id)),))
        row = cur.fetchone()
    if row is None:
        raise errors.not_found()
    return JsonResponse(row[0], status=200)


@_route("POST")
def publish_card(request, conn):
    """POST /v1/peers/me/agent-card.

    The check order matters for what a client learns: the
    signature is verified BEFORE the jku/kid pin, so an unsigned or wrongly-signed card
    never reaches the pin check and cannot probe the server's configured base URL.
    """
    auth = authenticate_peer(conn, request)
    card = _json_body(request)

    key = keys.find_active(conn, auth.peer_id)
    if key is None:
        raise errors.validation("peer has no active signing key")
    try:
        raw = keys.decode_public_key(key.public_key)
    except errors.ApiError:
        raise errors.internal("stored public key is not base64url")

    try:
        cards.verify_card(card, raw)
    except cards.CardVerifyError:
        raise errors.validation("card signature does not verify under your active key")

    base = getattr(settings, "MESHY_PUBLIC_BASE_URL", None)
    if not base:
        # Return 500, not 400. The client did nothing wrong; the
        # server cannot form a jku an external verifier could resolve, so it refuses to
        # store a card rather than storing one that is unpinned.
        raise errors.internal(
            "MESHYCHAT_PUBLIC_BASE_URL is not configured; cannot publish an "
            "externally-verifiable agent card")
    expected = cards.expected_jku(base, auth.peer_id)

    header = cards.protected_header(card)
    if header["kid"] != key.kid:
        raise errors.validation("card protected `kid` does not match your active signing key")
    if header["jku"] != expected:
        raise errors.validation(f"card protected `jku` must be {expected}")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_cards (peer_id, card, updated_at) VALUES (%s,%s,NOW()) "
            "ON CONFLICT (peer_id) DO UPDATE SET card = EXCLUDED.card, updated_at = NOW()",
            (str(auth.peer_id), json.dumps(card)))
    return HttpResponse(status=204)


# ------------------------------------------------------- invocation webhook (041 S9)

@_route("PUT", "DELETE")
def invocation_webhook(request, conn):
    """PUT and DELETE /v1/peers/me/invocation-webhook (divergence 041 S9; design 4.11, Q9).
    PUT `{"url": ...}` stores the peer's doorbell URL UNVERIFIED with a fresh challenge and
    answers 202 `{url, verified: false}`; `run_call_worker` proves control of it before it is
    ever rung (apps/calls/webhooks.py). The body is that one key and nothing else. The URL
    must pass S8's rules (https, 443, no userinfo, no fragment, at most 2048 characters, no
    blocked literal), else 400. DELETE removes the registration: 204, or 404 when there is
    none. Both are peer WRITES through `authenticate_peer`, so DPoP binds them; there is no
    cookie path. This request never POSTs to the URL (rule E7). The challenge is never
    returned: whoever controls the URL echoes it, and the peer need not know it.
    """
    auth = authenticate_peer(conn, request)
    if request.method == "DELETE":
        if not webhooks.deregister(conn, auth.peer_id):
            raise errors.not_found()
        return HttpResponse(status=204)
    body = _json_body(request)
    if set(body) != {"url"}:
        raise errors.validation('the body is {"url": "<https URL>"} and nothing else')
    try:
        url = webhooks.check_registration_url(body["url"])
    except webhooks.BadWebhookUrl as exc:
        raise errors.validation(f"invalid webhook url: {exc}")
    return JsonResponse(webhooks.register(conn, auth.peer_id, url), status=202)


# ---------------------------------------------------------------- owner routes
#
# F05, divergence 016. These are OWNER-authenticated: the credential is the
# owner's (session cookie with CSRF, a `meshypat_` PAT, or the dev JWT), resolved by the one
# owner extractor this project has (`apps/dashboard/auth.py`). The owner id is never read
# from the request.
#
# Check the credential first, then reject a malformed `{peer_id}` with 400, then reject a
# body that does not fit with 400/422, and only then run `require_scope`. A PAT without the
# scope and a malformed body therefore receives 422, not 403.

#: Scope required to mint a peer token.
TOKENS_MINT = "tokens:mint"
DEFAULT_TOKEN_TTL_SECS = 3600
#: Ceiling on a minted peer token's lifetime, 86400 seconds. There is no setting or
#: environment variable for it on this server, so this default is the contract.
PEER_TOKEN_MAX_TTL_SECS = 86_400
#: Error codes returned by peer-token minting.
E_SIGNING_DISABLED = "E_SIGNING_DISABLED"
E_SCOPE_ESCALATION = "E_SCOPE_ESCALATION"
_I64 = (-(2 ** 63), 2 ** 63 - 1)


#: `_route` itself, named for the owner surface: the extractor (`apps/dashboard/auth.py`)
#: and this app's helpers raise the one `ApiError` every surface shares, so `_route`'s
#: single `except` renders both with the same `{error, message}` body.
_owner_route = _route


def _owner(conn, request):
    from apps.dashboard import auth as owner_auth
    return owner_auth.authenticate_owner(conn, request)


def _json_object_body(request) -> dict:
    """Reject malformed JSON with 400 and a non-object JSON value with 422."""
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.ApiError(400, errors.E_VALIDATION, f"malformed JSON body: {exc}")
    if not isinstance(payload, dict):
        raise errors.ApiError(422, errors.E_VALIDATION, "request body must be a JSON object")
    return payload


def _shape_error(name: str, what: str) -> errors.ApiError:
    return errors.ApiError(422, errors.E_VALIDATION, f"invalid type for field `{name}`: "
                                                     f"expected {what}")


def _opt_string_list(payload: dict, name: str):
    """An optional list of strings: absent or null is None."""
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _shape_error(name, "a list of strings")
    return value


def _opt_i64(payload: dict, name: str):
    """An optional signed 64-bit integer; bool, float, and string do not fit."""
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not _I64[0] <= value <= _I64[1]:
        raise _shape_error(name, "i64")
    return value


def _owns_active_peer(conn, peer_id, owner_id) -> bool:
    """Use one predicate: exists, is the caller's,
    and is active. Missing, another owner's and inactive are one answer (404), so the
    route is not a peer-id enumeration oracle."""
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM peers WHERE id = %s AND owner_id = %s "
                    "AND status = 'active')", (str(peer_id), str(owner_id)))
        return bool(cur.fetchone()[0])


def _active_key_did(conn, peer_id):
    """Return the peer's active key as a did:key, or None when it has no
    active key or the stored value is not a 32-byte curve point (a keyless peer still gets
    a bearer token)."""
    from apps.authapi.onboard import is_edwards_point
    key = keys.find_active(conn, peer_id)
    if key is None:
        return None
    try:
        raw = keys.decode_public_key(key.public_key)
    except errors.ApiError:
        return None
    return to_did_key(raw) if is_edwards_point(raw) else None


@_owner_route("POST")
def mint_peer_token(request, conn, peer_id):
    """POST /v1/peers/{peer_id}/tokens. 201, returned once and
    never persisted."""
    owner = _owner(conn, request)
    pid = _peer_uuid(peer_id)
    body = _json_object_body(request)
    requested = _opt_string_list(body, "scopes")
    ttl_secs = _opt_i64(body, "ttl_secs")

    owner.require_scope(TOKENS_MINT)
    if not _owns_active_peer(conn, pid, owner.owner_id):
        raise errors.not_found()
    cfg = get_config()
    # No issuer is the same condition as sign-in: a token with no `iss` would verify
    # nowhere.
    if not cfg.jwt_signing_key_pem or not cfg.jwt_issuer:
        raise errors.ApiError(503, E_SIGNING_DISABLED,
                              "peer-token minting is not configured in this environment")

    ttl = min(max(DEFAULT_TOKEN_TTL_SECS if ttl_secs is None else ttl_secs, 1),
              PEER_TOKEN_MAX_TTL_SECS)
    scopes = list(requested or [])
    # A PAT may grant only a subset of what it holds; the human may grant any.
    if owner.method == "pat":
        extra = next((s for s in scopes if s not in owner.scopes), None)
        if extra is not None:
            raise errors.ApiError(
                403, E_SCOPE_ESCALATION,
                f"peer token cannot carry a scope the minting credential does not hold: "
                f"{extra}")

    issued_at = dt.datetime.now(dt.timezone.utc)
    expires_at = issued_at + dt.timedelta(seconds=ttl)
    claims = build_claims(
        sub=owner.owner_id,                        # TASK-25: the authorizing owner
        iss=cfg.jwt_issuer,
        iat=int(issued_at.timestamp()),
        exp=int(expires_at.timestamp()),
        scopes=scopes,
        aud="peer",
        lin=None,
        cnf=_active_key_did(conn, pid),            # TASK-26
        act_sub=pid,                               # the acting peer
    )
    token = sign(claims, cfg.jwt_signing_key_pem)
    # Key order is part of the response contract: token, token_type, peer_id, scopes,
    # expires_at, issued_at.
    return JsonResponse({
        "token": token,
        "token_type": "Bearer",
        "peer_id": str(pid),
        "scopes": scopes,
        "expires_at": format_rfc3339(expires_at),
        "issued_at": format_rfc3339(issued_at),
    }, status=201)


#: Peer quota capability and owner scopes.
PEERS_PER_OWNER = "peers_per_owner"
PEERS_WRITE = "peers:write"
PEERS_READ = "peers:read"
_PEER_VIEW_COLS = "id, owner_id, name, display_name, tier, status, created_at"


def _peer_view(row) -> dict:
    """Return the peer fields in the response contract order."""
    pid, owner_id, name, display_name, tier, status, created_at = row
    return {"id": str(pid), "owner_id": str(owner_id), "name": name,
            "display_name": display_name, "tier": tier, "status": status,
            "created_at": format_rfc3339(created_at)}


def _opt_string(payload: dict, name: str):
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _shape_error(name, "a string")
    return value


@_owner_route("POST", "GET")
def peers_collection(request, conn):
    """POST (create, 201) and GET (list, 200) /v1/peers.

    The peers-per-owner reservation and the INSERT are ONE transaction here, where
    a refused or failed create leaves the counter where it was: the choice divergence 016
    (2) made for onboarding.
    """
    owner = _owner(conn, request)
    if request.method == "GET":
        owner.require_scope(PEERS_READ)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_PEER_VIEW_COLS} FROM peers WHERE owner_id = %s "
                        "AND status <> 'deleted' ORDER BY created_at", (str(owner.owner_id),))
            return JsonResponse([_peer_view(r) for r in cur.fetchall()], safe=False)

    from apps.files import quota

    body = _json_object_body(request)
    raw_name = body.get("name")
    if not isinstance(raw_name, str):
        raise errors.ApiError(422, errors.E_VALIDATION, "missing or invalid field `name`")
    display_name = _opt_string(body, "display_name")
    metadata = body.get("metadata")                  # Any JSON; null is None.
    owner.require_scope(PEERS_WRITE)
    name = raw_name.strip()
    if not name:
        raise errors.validation("name must not be empty")

    from . import names

    with conn.transaction():
        # Divergence 065: a name is a mail address, so a taken or reserved one is refused
        # (apps/peers/names.py), under a lock that orders a concurrent create of the same name.
        try:
            with conn.cursor() as cur:
                names.claim(cur, name)
        except names.NameUnavailable as exc:
            raise errors.name_unavailable(str(exc))
        try:
            quota.get_quota_service().try_reserve(conn, owner.owner_id, PEERS_PER_OWNER, 1)
        except quota.UnknownCapability as exc:
            raise errors.ApiError(400, errors.E_VALIDATION, f"unknown capability: {exc}")
        except (quota.QuotaExceeded, quota.ProductAccessDenied) as exc:
            raise errors.ApiError(402, "E_QUOTA_EXCEEDED", str(exc))
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO peers (owner_id, name, display_name, metadata) "
                f"VALUES (%s, %s, %s, %s) RETURNING {_PEER_VIEW_COLS}",
                (str(owner.owner_id), name, display_name,
                 None if metadata is None else Jsonb(metadata)))
            row = cur.fetchone()
    return JsonResponse(_peer_view(row), status=201)


@_owner_route("DELETE")
def delete_peer(request, conn, peer_id):
    """DELETE /v1/peers/{peer_id} (STATUS F35, ASDLC OI-67, the operator's
    ruling D-2026-10-03-1). 204, or 404 when no row matched.

    A SOFT delete: `status = 'deleted'`, and the row stays because every signed row the peer
    wrote still names it and its keys stay readable for re-verification. The predicate is
    `id AND owner_id` with NO status term, so deleting an already-deleted peer of
    one's own is 204 again; another owner's peer and a missing one are both 404.

    Deleting a peer also revokes its live sessions: the same transaction deletes the
    `mesh_presence` row and sends the revocation NOTIFY (apps/wsgw/revocation.py), so on commit every serving process
    re-reads the peer's standing and closes its sockets with 4410; a lost notification is
    caught by the standing sweep. Every authenticated route already refuses a peer that is not
    `active` (apps/peers/auth.py), so its JWTs stop working at the commit, not
    at their expiry. Its REFRESH TOKENS are not revoked: `POST /v1/auth/refresh` reads the token
    and the peer's key and never `peers.status`, so a deleted peer's refresh token keeps rotating
    (measured: 200, a fresh 30-day successor, and again), and every JWT that yields is refused by
    the peer routes with 403 E_FORBIDDEN, so nothing can be done with it on this server
    (tests/test_owner_routes.py, `test_a_deleted_peers_refresh_token_still_rotates_...`). This
    behavior is kept as is; anything that checks a JWT's signature without the peer's status would accept one of those
    JWTs. A cleanup failure rolls the delete back and is a 500, so a peer is never
    reported deleted while its presence row survives.
    """
    owner = _owner(conn, request)
    pid = _peer_uuid(peer_id)
    owner.require_scope(PEERS_WRITE)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("UPDATE peers SET status = 'deleted', updated_at = NOW() "
                        "WHERE id = %s AND owner_id = %s", (str(pid), str(owner.owner_id)))
            if cur.rowcount == 0:
                raise errors.not_found()
            cur.execute("DELETE FROM mesh_presence WHERE peer_id = %s", (str(pid),))
        notify_revocation(conn, pid)
    return HttpResponse(status=204)


#: Invitation defaults and bounds.
INVITE_DEFAULT_MAX_USES = 1
INVITE_MAX_MAX_USES = 50
INVITE_DEFAULT_EXPIRES_IN_HOURS = 168
INVITE_MAX_EXPIRES_IN_HOURS = 720
INVITE_CODE_INSERT_RETRIES = 5
_U32_MAX = 2 ** 32 - 1


def _opt_u32(payload: dict, name: str):
    """An optional unsigned 32-bit integer: absent or null is None; a bool, a float, a
    string, a negative number or one past 2^32-1 does not fit (422)."""
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _U32_MAX:
        raise _shape_error(name, "u32")
    return value


@_route("POST")
def mint_invite(request, conn):
    """POST /v1/invites (STATUS F35, ASDLC OI-67, the operator's ruling
    D-2026-10-03-1). PEER-authenticated: a mentor peer mints a
    code its mentees redeem at `POST /v1/auth/redeem-invitation`, which until now could only
    refuse because nothing on this server wrote an `invitations` row.

    `max_uses` defaults to 1 and is clamped to 1..50; `expires_in_hours` defaults to 168 and is
    clamped to 1..720 (clamped, not refused). `owner_handle` is stored as given.
    201 `{code, expires_at, max_uses}`. The code is six words (apps/peers/invite_code.py); a
    UNIQUE(code) collision draws a fresh one, at most five times, and the sixth failure is a 500
    because no unique code was inserted. The redeem side is already bounded: a per-client-IP throttle (B16) and the
    invitation row locked `FOR UPDATE`.
    """
    import psycopg
    from datetime import datetime, timedelta, timezone

    from . import invite_code
    from .timefmt import format_rfc3339

    auth = authenticate_peer(conn, request)
    body = _json_object_body(request)
    max_uses = _opt_u32(body, "max_uses")
    hours = _opt_u32(body, "expires_in_hours")
    handle = body.get("owner_handle")
    if handle is not None and not isinstance(handle, str):
        raise _shape_error("owner_handle", "a string")
    max_uses = min(max(INVITE_DEFAULT_MAX_USES if max_uses is None else max_uses, 1),
                   INVITE_MAX_MAX_USES)
    hours = min(max(INVITE_DEFAULT_EXPIRES_IN_HOURS if hours is None else hours, 1),
                INVITE_MAX_EXPIRES_IN_HOURS)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    for _ in range(INVITE_CODE_INSERT_RETRIES):
        code = invite_code.generate_code()
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("INSERT INTO invitations (code, issuer_peer_id, owner_handle, "
                            "expires_at, max_uses) VALUES (%s, %s, %s, %s, %s) "
                            "RETURNING code, expires_at, max_uses",
                            (code, str(auth.peer_id), handle, expires_at, max_uses))
                row = cur.fetchone()
        except psycopg.errors.UniqueViolation:
            continue
        return JsonResponse({"code": row[0], "expires_at": format_rfc3339(row[1]),
                             "max_uses": row[2]}, status=201)
    raise errors.internal(f"invitation insert: exhausted {INVITE_CODE_INSERT_RETRIES} "
                          "retries on unique code")


@_owner_route("POST")
def publish_peer_key(request, conn, peer_id):
    """POST /v1/peers/{peer_id}/keys, the owner's break-glass publish.

    The same key service as the peer route (`keys.publish`), with `owner_scoped=True` and
    no lineage: a keyless peer gets its FIRST key here, which the peer
    route refuses, and nothing is re-anchored, so the migration-031 lineage guard does not
    apply. A peer that already has an active key still rotates only with `X-Key-CoSig` by
    that key: owner_scoped relaxes the bootstrap and the lineage guard, not the co-signature.
    """
    owner = _owner(conn, request)
    pid = _peer_uuid(peer_id)
    body = _json_object_body(request)
    owner.require_scope(PEERS_WRITE)
    if not _owns_active_peer(conn, pid, owner.owner_id):
        raise errors.not_found()
    # Evaluated as the publish call's argument: a
    # non-ASCII X-Key-CoSig is 401, after the ownership check.
    cosig = _cosig_header(request)
    key = keys.publish(conn, pid, body, cosig, owner_scoped=True, caller_lineage=None)
    return _published_key(key)


__all__ = [
    "presence_beat", "presence_list", "my_keys", "revoke_my_key", "peer_active_key",
    "jwks", "serve_card", "publish_card", "invocation_webhook", "mint_peer_token",
    "peers_collection", "publish_peer_key",
]
