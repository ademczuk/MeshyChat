"""CSRF for cookie-authenticated writes, and the security response headers.

## CSRF

The token is an HMAC over a fixed domain string and the SESSION ID, so it is bound to the
session rather than being a bearer value any page can mint. A write with a cookie
credential must carry it; a read never needs it.

TWO DEFECTS THIS MODULE EXISTS TO FIX, both measured in this repo on 2026-09-21:

1. `apps/dashboard/auth.py:102-109` checked only that the header was PRESENT. Django's
   `CsrfViewMiddleware` is deliberately absent from `meshychat/settings.py`, so that
   presence check was the ONLY CSRF control on the dashboard surface. A mutation replacing
   it with `return` left all 76 tests green.

2. `apps/grants/owner_auth.py:105-121` compared two `str`
   with `hmac.compare_digest`, which **raises `TypeError` for any codepoint above 0x7F**.
   Django decodes request headers as latin-1, so any byte > 0x7F in `X-CSRF-Token`
   produced that TypeError, and the route wrapper caught only `ApiError` - so a
   non-ASCII header became an unhandled HTML 500. An attacker-chosen header byte must
   never decide whether the server answers JSON.

The fix for (2) is to compare BYTES. `compare_digest` on bytes has no ASCII restriction,
and encoding a token that is by construction base64url is lossless. A malformed header
then takes the same path as a wrong one: a refusal.

FAIL-CLOSED RULE: with no state secret configured there is no token to compare against.
A debug build passes (so a developer is not locked out of their own dashboard); a release
build REFUSES. The unconfigured case must never be the permissive one in
production - that is how a control gets deployed off.

## Response headers

This project had **no security headers on any route**, while `apps/files/views.py` justified
serving `application/octet-stream` with an argument that depends on `nosniff` being set. A
comment is not a header.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

#: Exact byte domain, trailing colon included. The separator exists so
#: this MAC cannot be confused with the OIDC-state MAC that shares the same state_secret,
#: and a token minted from a different spelling would verify against nothing.
CSRF_DOMAIN = b"meshy-csrf:v1:"

#: A read never carries a token.
SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")


def csrf_token_for(session_id, secret: bytes) -> str:
    """base64url, unpadded, over (domain || session_id bytes)."""
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    mac = hmac.new(secret, digestmod=hashlib.sha256)
    mac.update(CSRF_DOMAIN)
    mac.update(session_id.bytes)
    return base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode("ascii")


def csrf_ok(method: str, provided: str | None, session_id, secret: bytes | None, *,
            debug: bool = False) -> bool:
    """True when this write may proceed.

    Returns a bool for EVERY input, including a header carrying bytes that are not valid
    UTF-8 or not ASCII. It must not be possible to make this function raise: the caller
    catches API errors, not TypeErrors, so anything else escapes as an HTML 500.
    """
    if method.upper() in SAFE_METHODS:
        return True
    if secret is None:
        return bool(debug)          # Release FAILS CLOSED.
    if not provided:
        return False
    expected = csrf_token_for(session_id, secret)
    # BYTES, NOT STR. See defect (2) in the module docstring. latin-1 round-trips every
    # byte Django can hand us, so a non-ASCII header compares unequal instead of raising.
    try:
        given_bytes = provided.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(given_bytes, expected.encode("ascii"))


#: The headers every response carries. Deliberately short: each one is here because
#: something in this codebase depends on it, not because a scanner asks for it.
SECURITY_HEADERS = {
    # apps/files serves opaque bytes as application/octet-stream and argues that a browser
    # must not sniff them as text/html. That argument is only true with this header.
    "X-Content-Type-Options": "nosniff",
    # Presigned download URLs carry an HMAC token in the PATH. Without this, following a
    # link off such a page puts the token in the Referer header of a third-party request.
    "Referrer-Policy": "no-referrer",
    # This is a JSON API; nothing here is meant to be framed.
    "X-Frame-Options": "DENY",
}


class SecurityHeadersMiddleware:
    """Apply `SECURITY_HEADERS` to every response, including error responses.

    A HEADER SET ONLY ON THE HAPPY PATH IS NOT A CONTROL - an error body is still a body a
    browser will sniff. Existing values are never overwritten, so a route that deliberately
    sets something different keeps it and the difference stays visible in that route.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        for name, value in SECURITY_HEADERS.items():
            if name not in response:
                response[name] = value
        return response


__all__ = ["CSRF_DOMAIN", "SAFE_METHODS", "SECURITY_HEADERS", "SecurityHeadersMiddleware",
           "csrf_ok", "csrf_token_for"]
