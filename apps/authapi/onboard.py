"""Pairing-code bootstrap: an owner mints a single-use code, a fresh node trades it for a
peer, its first key and its first credentials.

    POST /v1/auth/onetime     owner-authenticated, 201
    POST /v1/onboard/peers    authenticated BY THE CODE, 201
    POST /v1/peers/me/pairing-codes   divergence 062, peer-authenticated, 201

ANY PEER CAN MINT (divergence 062, the operator's instruction of 2026-09-30: anyone on the
network should be able to mint, with no cap or limit). An active peer mints a code for ITS OWN
owner, never for one named in the body. The code records the issuing peer; the redeem refuses
it if that peer is no longer active under the same owner, so suspending a peer withdraws the
codes it handed out. The new peer carries `metadata.onboarded_by_peer_id`. Nothing counts how
many codes a peer mints, and the redeem is not rate limited (B16's budget stays on invitation
redemption only): a code is 24 random bytes, so guessing one is not a budget question.

The redeem route is `/v1/onboard/peers`, not `/v1/peers`, because the owner-session
create route already owns `POST /v1/peers`.

THE NODE CLIENT IS THE OTHER HALF OF THIS CONTRACT. `tools/memory/onboard_local.py` in the
node repository posts `{onetime_code, public_key, device_label?}` (:193-197) to
`/v1/onboard/peers` (:246), accepts any 2xx (:253), and stores `peer_id`, `owner_id`,
`kid`, `jwt` as required fields and `refresh_token`, `jwt_expires_at` as optional ones
(:262-273). The response below carries exactly those six names.

WHAT A NEWLY ONBOARDED PEER RECEIVES, AND WHAT IT DOES NOT. It receives a peer row, one
active key, a peer JWT whose `cnf` is that key, and a refresh token. It receives NO
grants: `create_peer` writes no `shared_memory_grant` or `skill_grants` row, and no
trigger on `peers` writes one. The node repository says the same from its side:
"After local-keygen onboarding, grant the new peer the required namespaces"
(docs/ONBOARD.md:55-56), and `onboarding_grant_proof.py` exists to refuse a launch until
an operator has. `peers.scopes` is NULL and the JWT's `scopes` is empty, on purpose.
Granting more here would bypass the separate, owner-driven authorization step.

SINGLE USE IS A ROW LOCK, NOT A READ-THEN-WRITE. The code row is read `FOR UPDATE`
inside the transaction that creates the peer and marks the code used, so two nodes
racing on one code serialise on the
lock and the second sees `used_at` set. Every refusal raises out of the transaction, so
nothing it did is committed, including the peers-per-owner reservation.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import re
import secrets
import uuid
from urllib.parse import quote

from django.http import JsonResponse
from psycopg.types.json import Jsonb
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.authapi import errors
from apps.authapi.config import get_config
from apps.authapi.didkey import to_did_key
from apps.authapi.tokens import build_claims, sign
from apps.authapi.request import _Refusal, _body, _field, _rfc3339
from apps.core import dbconn
from apps.crypto.primitives import b64u
from apps.dashboard import auth as owner_auth
from apps.dashboard import errors as owner_errors
from apps.files import quota
from apps.peers import errors as peer_errors
from apps.peers import names as peer_names
from apps.peers.auth import authenticate_peer

#: Five minutes: "a node pongs through in ~70s" and the prior 60 s
#: expired codes mid-onboarding. (sql/025's header still says 60; the code wins.)
ONETIME_TTL_SECS = 300
#: A PAT must hold BOTH to mint, because redeeming the code creates a
#: peer (peers:write) and returns credentials (tokens:mint). Cookie and dev-JWT owners
#: are full authority and pass.
PEERS_WRITE = "peers:write"
TOKENS_MINT = "tokens:mint"
#: The desktop deep-link scheme.
DEEP_LINK_PREFIX = "agentmeshy-mentee://onboard?code="
#: Lifetimes for issued peer credentials.
PEER_JWT_TTL_SECS = 3600
REFRESH_TOKEN_TTL_DAYS = 30
PEERS_PER_OWNER = "peers_per_owner"


# ------------------------------------------------------------------ primitives

def generate_pairing_code() -> str:
    """Return 24 CSPRNG bytes as 32 unpadded base64url characters."""
    return b64u(secrets.token_bytes(24))


_URL_SAFE = re.compile(r"[A-Za-z0-9_-]*")
_STANDARD = re.compile(r"[A-Za-z0-9+/]*")


def _strict_b64(text: str, alphabet: re.Pattern, altchars: bytes | None,
                padded: bool) -> bytes | None:
    """Decode a base64 form strictly.

    A strict decoder refuses characters outside its alphabet, refuses padding the form
    does not allow (and requires it where it does), and refuses non-canonical trailing
    bits. Python's `b64decode` silently DISCARDS non-alphabet characters unless told
    otherwise and accepts non-canonical trailing bits. The canonical check is a round
    trip: re-encode and compare. A lax decode would accept non-canonical spellings of a
    key that the node client never produces.
    """
    body = text
    if padded:
        if len(text) % 4:
            return None
        body = text.rstrip("=")
        if len(text) - len(body) > 2:
            return None
    elif "=" in text:
        return None
    if not alphabet.fullmatch(body) or len(body) % 4 == 1:
        return None
    try:
        raw = base64.b64decode(body + "=" * (-len(body) % 4), altchars=altchars,
                               validate=True)
    except (binascii.Error, ValueError):
        return None
    again = base64.b64encode(raw, altchars=altchars).decode("ascii")
    if (again if padded else again.rstrip("=")) != text:
        return None
    return raw


def decode_pubkey(text: str) -> bytes | None:
    """Decode URL-safe unpadded, standard unpadded, then padded base64.

    The caller checks length so its distinct validation messages stay reachable.
    """
    for alphabet, altchars, padded in ((_URL_SAFE, b"-_", False),
                                       (_STANDARD, None, False),
                                       (_STANDARD, None, True)):
        raw = _strict_b64(text, alphabet, altchars, padded)
        if raw is not None:
            return raw
    return None


_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def is_edwards_point(raw: bytes) -> bool:
    """Return whether the value's y-coordinate has an Edwards curve encoding.

    The top (sign) bit is ignored and a non-canonical y (>= p) is reduced mod p rather
    than rejected, matching standard Ed25519 decompression. The low 255 bits are a y for which
    (y^2 - 1) / (d*y^2 + 1) is a square in the field, zero included. Small-order and
    torsioned points PASS; `verify_strict` is where they are refused. The denominator
    is never zero:
    that would need y^2 = -1/d, and -1/d is not a square because d is not.

    Used for ONE decision: whether the onboarding JWT gets a `cnf`. A 32-byte value that is not a point still
    onboards, with a bearer token.
    """
    if len(raw) != 32:
        return False
    y = (int.from_bytes(raw, "little") & ((1 << 255) - 1)) % _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    w = (u * pow(v, _P - 2, _P)) % _P
    return w == 0 or pow(w, (_P - 1) // 2, _P) == 1


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------------ POST /v1/auth/onetime

@csrf_exempt
@require_http_methods(["POST"])
def mint_onetime(request):
    """Takes no body: the owner IS the request's credential.

    The owner is resolved by the one owner extractor this project has for the dashboard
    surface (`apps/dashboard/auth.py`), in this order:
    session cookie (with CSRF on this POST), then a `meshypat_` PAT, then the dev JWT.
    The cookie is what `POST /v1/auth/owner/sign-in` issues, so a headless owner and a
    browser reach this route the same way.
    """
    cfg = get_config()
    code = generate_pairing_code()
    expires_at = _now() + dt.timedelta(seconds=ONETIME_TTL_SECS)
    try:
        with dbconn.serving_connection(cfg.dsn) as conn:
            owner = owner_auth.authenticate_owner(conn, request)
            owner.require_scope(PEERS_WRITE)
            owner.require_scope(TOKENS_MINT)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pairing_codes (code, owner_id, expires_at) "
                    "VALUES (%s, %s, %s)",
                    (code, owner.owner_id, expires_at),
                )
    except owner_errors.ApiError as exc:
        return JsonResponse(exc.body(), status=exc.status)
    return _minted(code, expires_at)


def _minted(code: str, expires_at: dt.datetime) -> JsonResponse:
    # Key order is part of the response contract: code, expires_at, deep_link.
    # URL encoding leaves A-Z a-z 0-9 - _ . ~ alone, which is every character a
    # base64url code can hold, so the link carries the code verbatim.
    return JsonResponse({
        "code": code,
        "expires_at": _rfc3339(expires_at),
        "deep_link": DEEP_LINK_PREFIX + quote(code, safe="-_.~"),
    }, status=201)


# ------------------------------------------------ POST /v1/peers/me/pairing-codes

@csrf_exempt
@require_http_methods(["POST"])
def mint_peer_code(request):
    """Divergence 062. Takes no body: the peer IS the request's credential,
    and the owner is the one `peers` gives that peer, never a value the caller sends.

    `authenticate_peer` refuses a token that is not a peer token, a peer that is not
    active, and (DPoP enforced, this being a POST) a request without a proof by the
    token's key. The answer has the owner route's shape, so a node client redeems a
    peer-minted code exactly as an owner-minted one."""
    cfg = get_config()
    code = generate_pairing_code()
    expires_at = _now() + dt.timedelta(seconds=ONETIME_TTL_SECS)
    try:
        with dbconn.serving_connection(cfg.dsn) as conn:
            auth = authenticate_peer(conn, request)
            with conn.cursor() as cur:
                cur.execute("SELECT owner_id FROM peers WHERE id = %s AND status = 'active'",
                            (str(auth.peer_id),))
                row = cur.fetchone()
                if row is None:          # suspended between the check above and this read
                    raise peer_errors.forbidden()
                cur.execute(
                    "INSERT INTO pairing_codes (code, owner_id, expires_at, issued_by_peer_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (code, row[0], expires_at, auth.peer_id),
                )
    except peer_errors.ApiError as exc:
        return JsonResponse(exc.body(), status=exc.status)
    return _minted(code, expires_at)


# ---------------------------------------------------------- POST /v1/onboard/peers

def _quota_refusal(exc: Exception) -> JsonResponse:
    """Map a tier refusal to its API response."""
    if isinstance(exc, quota.UnknownCapability):
        return errors.coded(errors.E_VALIDATION, f"unknown capability: {exc}", 400)
    return errors.coded(errors.E_QUOTA_EXCEEDED, str(exc), 402)


@csrf_exempt
@require_http_methods(["POST"])
def create_peer(request):
    """Redeem a pairing code in the required validation order.

    Status, code and message of every refusal are fixed: the node client and tests match on
    them. Divergence 062: a code minted by a peer is refused while that peer is not
    active under the code's owner, and the new peer records who issued it. NOT rate limited:
    B16's per-IP budget was taken here until 062 and stays on invitation redemption."""
    payload, err = _body(request)
    if err:
        return err
    onetime_code, err = _field(payload, "onetime_code", required=True)
    if err:
        return err
    public_key, err = _field(payload, "public_key", required=True)
    if err:
        return err
    device_label, err = _field(payload, "device_label", required=False)
    if err:
        return err

    cfg = get_config()
    # Checked before the code is touched, so an unconfigured server never burns
    # a code on its way to a 503. No issuer is the same condition (divergence 015).
    if not cfg.jwt_signing_key_pem or not cfg.jwt_issuer:
        return errors.signing_key_unavailable()

    code = onetime_code.strip()
    if not code:
        return errors.coded(errors.E_PAIRING_INVALID, "missing pairing code", 400)

    pubkey_raw = decode_pubkey(public_key)
    if pubkey_raw is None:
        return errors.coded(errors.E_PUBKEY_INVALID,
                            "public_key is not a valid 32-byte Ed25519 key", 400)
    if len(pubkey_raw) != 32:
        return errors.coded(errors.E_PUBKEY_INVALID, "public_key must be 32 bytes", 400)
    pubkey_b64 = b64u(pubkey_raw)       # stored in ONE canonical form

    try:
        with dbconn.serving_connection(cfg.dsn) as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT owner_id, expires_at, used_at, issued_by_peer_id "
                    "FROM pairing_codes "
                    "WHERE code = %s FOR UPDATE",
                    (code,),
                )
                row = cur.fetchone()
            if row is None:
                raise _Refusal(errors.coded(errors.E_PAIRING_INVALID,
                                            "unknown pairing code", 404))
            owner_id, code_expires_at, used_at, issued_by = row
            # SINGLE USE. Checked before expiry: a used code
            # answers "already used" even after it has also expired.
            if used_at is not None:
                raise _Refusal(errors.coded(errors.E_PAIRING_CONSUMED,
                                            "pairing code already used", 410))
            # EXPIRY, on the application clock.
            if code_expires_at <= _now():
                raise _Refusal(errors.coded(errors.E_PAIRING_EXPIRED,
                                            "pairing code expired", 410))
            # ISSUER (divergence 062). Checked after single use and expiry, so a used or
            # expired code answers as it always has. Nothing is written on this refusal:
            # the code stays unused, and redeems again if its issuer is reactivated.
            # FOR SHARE: a suspension of the issuer racing this redeem waits for it, or this
            # redeem waits for the suspension and then refuses. Without the lock the redeem
            # reads the issuer as it was before a suspension that commits first.
            if issued_by is not None:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM peers WHERE id = %s AND owner_id = %s "
                                "AND status = 'active' FOR SHARE", (issued_by, owner_id))
                    issuer_active = cur.fetchone() is not None
                if not issuer_active:
                    raise _Refusal(errors.coded(errors.E_PAIRING_ISSUER_INACTIVE,
                                                "the peer that issued this pairing code "
                                                "is no longer active", 410))

            # Peers-per-owner goes through the one tier chokepoint this project has
            # (apps/files/quota.py, which runs the entitlements gate first).
            try:
                quota.get_quota_service().try_reserve(conn, owner_id, PEERS_PER_OWNER, 1)
            except (quota.QuotaExceeded, quota.ProductAccessDenied,
                    quota.UnknownCapability) as exc:
                raise _Refusal(_quota_refusal(exc)) from exc

            kid = f"desktop-{uuid.uuid4().hex}"
            with conn.cursor() as cur:
                # Through divergence 065's door: a name that is already some peer's (or the
                # roster's) is drawn again rather than shared.
                peer_name = peer_names.draw(cur, lambda: f"desktop-{uuid.uuid4().hex[:8]}")
                cur.execute(
                    "INSERT INTO peers (owner_id, name, display_name, metadata) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (owner_id, peer_name,
                     "Desktop" if device_label is None else device_label,
                     None if issued_by is None
                     else Jsonb({"onboarded_by_peer_id": str(issued_by)})),
                )
                peer_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s, %s, %s, TRUE)",
                    (peer_id, kid, pubkey_b64),
                )
                cur.execute("UPDATE pairing_codes SET used_at = NOW() WHERE code = %s",
                            (code,))
                # Attach web-first pending lesson subscriptions to the new peer. With no
                # pending rows it touches nothing.
                cur.execute(
                    "UPDATE lesson_subscriptions SET mentee_peer_id = %s "
                    "WHERE mentee_owner_id = %s AND mentee_peer_id IS NULL "
                    "AND revoked_at IS NULL",
                    (peer_id, owner_id),
                )

            issued_at = _now()
            exp = issued_at + dt.timedelta(seconds=PEER_JWT_TTL_SECS)
            claims = build_claims(
                sub=owner_id,                 # TASK-25: the authorizing owner
                iss=cfg.jwt_issuer,
                iat=int(issued_at.timestamp()),
                exp=int(exp.timestamp()),
                # EMPTY: the peer did not exist a moment ago, so no provisioning decision
                # exists.
                scopes=[],
                aud="peer",
                lin=None,
                # TASK-26: the node's own key, whenever it is a curve point.
                cnf=to_did_key(pubkey_raw) if is_edwards_point(pubkey_raw) else None,
                act_sub=peer_id,
            )
            token = sign(claims, cfg.jwt_signing_key_pem)

            refresh_token = b64u(secrets.token_bytes(32))
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO refresh_tokens "
                    "(token_hash, peer_id, owner_id, expires_at, issued_under_kid) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (hashlib.sha256(refresh_token.encode("utf-8")).digest(), peer_id,
                     owner_id, issued_at + dt.timedelta(days=REFRESH_TOKEN_TTL_DAYS), kid),
                )
    except _Refusal as refusal:
        return refusal.response

    # Key order is part of the response contract.
    return JsonResponse({
        "peer_id": str(peer_id),
        "owner_id": str(owner_id),
        "kid": kid,
        "jwt": token,
        "refresh_token": refresh_token,
        "jwt_expires_at": _rfc3339(exp),
    }, status=201)
