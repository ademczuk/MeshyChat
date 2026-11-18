"""The authentication error wire format.

    {"error": "<CODE>", "message": "<text>"}

The codes below are the ones the authentication routes return.
"""
from __future__ import annotations

from django.http import JsonResponse

E_AUTH_INVALID = "E_AUTH_INVALID"
E_RATE_LIMITED = "E_RATE_LIMITED"
E_REDEEM_FAILED = "E_REDEEM_FAILED"
E_REFRESH_TOKEN_INVALID = "E_REFRESH_TOKEN_INVALID"
E_VALIDATION = "E_VALIDATION"
# Owner sign-in and pairing-code codes.
E_OWNER_KEY_UNREGISTERED = "E_OWNER_KEY_UNREGISTERED"
E_AUTH_MISSING = "E_AUTH_MISSING"
E_SESSION_EXPIRED = "E_SESSION_EXPIRED"
E_PAIRING_INVALID = "E_PAIRING_INVALID"
E_PAIRING_EXPIRED = "E_PAIRING_EXPIRED"
E_PAIRING_CONSUMED = "E_PAIRING_CONSUMED"
E_PAIRING_ISSUER_INACTIVE = "E_PAIRING_ISSUER_INACTIVE"  # divergence 062
E_PUBKEY_INVALID = "E_PUBKEY_INVALID"
E_QUOTA_EXCEEDED = "E_QUOTA_EXCEEDED"


def coded(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": code, "message": message}, status=status)


def rate_limited() -> JsonResponse:
    """Return the fixed 429 response for a rate-limited request."""
    return coded(E_RATE_LIMITED, "rate limit exceeded; retry later", 429)


def signing_key_unavailable() -> JsonResponse:
    """Return 503, not 500, when signing configuration is unavailable."""
    return coded(E_REDEEM_FAILED, "jwt signing key not configured on this server", 503)


def signin_invalid() -> JsonResponse:
    """Use one response for every sign-in failure: unknown peer, no active key, unknown
    nonce, consumed nonce, expired nonce, bad signature. Splitting these into
    distinct messages would turn this route into a peer-existence oracle, which is
    the property tagged HARDENING-4 in `views.py`.
    """
    return coded(E_AUTH_INVALID, "sign-in challenge or signature invalid", 401)
