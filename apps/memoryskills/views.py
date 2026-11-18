"""The capability routes: mint and revoke (B07 slice 5, docs/DIVERGENCES.md 040).

  POST /v1/memory/capabilities          mint a token (design 2.2, 2.3, 3.1)
  POST /v1/memory/capabilities/revoke   revoke a block of one (design 2.5 (a), 3.1)

A mint is an HTTP route, not a memory skill, so the token is returned once and never persisted:
a skill result would be stored in skill_calls.result (design 2.2). Both are POSTs, so
authenticate_peer enforces DPoP whenever it is on (apps/peers/auth.py), and the caller is the
authenticated peer (act.sub, else sub).

TOKENS OFF: while MESHY_CAPABILITIES is off, or no root key store is loaded, both routes answer
403 E_CAPABILITY_UNAVAILABLE before anything else, as the header middleware answers a request
carrying `Meshychat-Capability` (apps/core/capability_header.py), and they IMPORT NOTHING: the mint
and revoke module, the verifier and biscuit_auth are imported only past that check.

The revoke route takes the token it revokes in the `Meshychat-Capability` header, the one channel a
token is presented in (design 3.1), so it is on the header middleware's accept list (with the
envelope path's invoke and A2A routes since slice 6); the mint takes none, and a header on it is
E_CAPABILITY_NOT_ACCEPTED there.
"""
from __future__ import annotations

import json

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.core import capability_header, dbconn, peer_limit
from apps.peers import errors
from apps.peers.auth import authenticate_peer

from . import capability_keys

UNAVAILABLE = "E_CAPABILITY_UNAVAILABLE"
UNAVAILABLE_MESSAGE = ("capability tokens are not available on this server "
                       "(MESHY_CAPABILITIES is off, or no root key is loaded)")


def _tokens_available() -> bool:
    """The flag, read per request as the middleware reads it, AND a loaded key store."""
    return capability_header.tokens_on() and capability_keys.current() is not None


def _json_body(request) -> dict:
    """The three statuses the other routes answer: wrong content type 415, not JSON 400, JSON
    but not an object 422 (apps/board/views.py). JSON the parser will not take is 400 too:
    json.loads raises ValueError (not its JSONDecodeError subclass) for an integer over 4300
    digits and RecursionError for nesting past the recursion limit, and either was a 500."""
    ctype = (request.content_type or "").split(";")[0].strip().lower()
    if ctype != "application/json" and not ctype.endswith("+json"):
        raise errors.ApiError(415, "E_VALIDATION",
                              f"expected `application/json` content-type, got {ctype[:80]!r}")
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (ValueError, RecursionError):     # ValueError: JSONDecodeError, UnicodeDecodeError
        raise errors.ApiError(400, "E_VALIDATION", "body is not valid JSON, or is JSON this "
                              "server does not parse (an integer over 4300 digits, or nesting "
                              "too deep)") from None
    if not isinstance(body, dict):
        raise errors.ApiError(422, "E_VALIDATION", "body must be a JSON object")
    return body


def _route(handler):
    """POST only; tokens off answers UNAVAILABLE and imports nothing; then a guarded
    connection, the peer's authentication (DPoP on a POST), the per-peer limit (B12), the JSON
    body, the handler. Every refusal is the server's error envelope."""
    def wrapper(request):
        if request.method != "POST":
            return HttpResponse(status=405, headers={"Allow": "POST"})
        if not _tokens_available():
            return capability_header.refusal(UNAVAILABLE, UNAVAILABLE_MESSAGE)
        from . import capability_issue      # imported only with tokens on
        store = capability_keys.current()
        try:
            with dbconn.serving_connection() as conn:
                auth = authenticate_peer(conn, request)
                # B12 (the project's design log (not part of this release) 003): the authenticated peer's CALLS bucket, the one
                # its invokes take. Reached only with tokens on, which is when a mint can loop.
                peer_limit.admit(auth.peer_id, transport="http")
                body = _json_body(request)
                return handler(request, conn, str(auth.peer_id), body, store,
                               capability_issue)
        except peer_limit.PeerRateLimited as exc:
            return peer_limit.http_response(exc)
        except errors.ApiError as exc:
            return JsonResponse(exc.body(), status=exc.status)
    wrapper.__name__ = getattr(handler, "__name__", "capability_route")
    return csrf_exempt(wrapper)


@_route
def mint_capability(request, conn, caller, body, store, issue):
    """POST /v1/memory/capabilities: 201 with the token, ONCE; not to be cached."""
    minted = issue.mint(conn, issuer=caller, body=body, store=store)
    response = JsonResponse(minted, status=201)
    response["Cache-Control"] = "no-store"
    return response


@_route
def revoke_capability(request, conn, caller, body, store, issue):
    """POST /v1/memory/capabilities/revoke: 200 with what was revoked and the caller's
    standing. The token, when presented, is read from `Meshychat-Capability` and never echoed."""
    presented = capability_header.presented(request.META)
    return JsonResponse(issue.revoke(conn, caller=caller, body=body, presented=presented,
                                     store=store), status=200)
