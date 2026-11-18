"""`Meshychat-Capability` is refused while this server has no token verifier (B07 slice 1).

THE SILENT WINDOW THIS CLOSES (docs/design/biscuit-capabilities.md 3.1 and critique C16;
docs/DIVERGENCES.md 040). Django ignores a header nothing reads. Before this module, a
client that sent the header was authorised by its database grants alone and could not
learn that the token it attached was never looked at. Measured on the unchanged tree: a GET
carrying the header got the same status as one without it on all 105 mounted routes, and a
WebSocket upgrade carrying it reached the hub. A capability NARROWS what its holder may do; a
server that ignores it widens the request back to everything the grants allow, silently.

WHERE THE REFUSAL LIVES. A middleware, not a per-view check: a per-view check misses the
routes nobody remembers, and the design names three (the board's declare route, the mesh
GETs, the upgrade). It sits before `DatabaseConnectionMiddleware`, so a refused request
reaches no identity resolver, no URL resolution, no view and no database. Django middleware
never sees a WebSocket scope, so `RefuseCapabilityUpgrade` wraps the websocket router in
`meshychat/asgi.py` and closes the upgrade before accept.

ONE RULE ON BOTH TRANSPORTS: a header named `meshychat-capability`, in any case, with any
value, the empty value included. HTTP reads `request.META`, which is exactly what a later
`request.headers["Meshychat-Capability"]` reads. A name with an underscore
(`Meshychat_Capability`) is not this header: daphne drops such names before the application
(CVE-2015-0219) and Django's ASGI request drops them again, so the upgrade applies the same rule.

THE ACCEPT LIST, `ACCEPTED`, is the ONE place a route may be named as honouring a token:
`(protocol, route)` pairs, `route` spelled as `ResolverMatch.route` spells it. It holds three
routes: since slice 5 the revoke route (`v1/memory/capabilities/revoke`), which takes the token
it revokes in this header (apps/memoryskills/views.py), and since slice 6 the envelope path's two
transports, native invoke and A2A `message:send` (design 3.1), whose views pre-check the token
before the call starts and hand it to the memory handlers (apps/calls/object_capability.py).
While tokens are off even an accepted route refuses (design 5.2, "tokens disabled, header
present, on ANY route"). tests/test_capability_header_refused.py (T16) walks every mounted
route against it.

TOKENS ON (MESHY_CAPABILITIES, slice 4). A header on an HTTP route that is not on the list is
`E_CAPABILITY_NOT_ACCEPTED` (design 4.3 and 5.2), never ignored: a token presented where the
design says no is refused. An HTTP route on the list passes the middleware, and its view
decides. The route is resolved here, before any view; an unmounted path is not on the list.
A WebSocket upgrade is refused whatever the list says, with `CLOSE_CAPABILITY_NOT_ACCEPTED`
(4415): v1 takes no token on the socket (design 2.6, step 1). The flag is read per request
from settings.MESHY_CAPABILITIES, and only a real bool is read; anything else fails the
request closed. tests/test_capability_header_not_accepted.py is the tokens-on half of T16.

THE REFUSAL NEVER REPEATS THE HEADER'S VALUE, in the body, a header, a close message or a log
line: the value is a bearer credential's signed text, and the answer goes to whoever sent it.

THE LOG LINE ESCAPES WHAT THE CLIENT CHOSE. It records the method and the path, and any client
reaches it unauthenticated, on any path: a path holding a newline (`%0A` on the wire) would
otherwise write a line of the client's own into the log. Both are escaped as Django's request
log escapes its arguments (django/utils/log.py `log_message`, the CVE-2025-48432 fix), so a
newline arrives as the two characters backslash and n. Review F1, docs/DIVERGENCES.md 040.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.urls import Resolver404, resolve

from apps.core.jsonerrors import envelope
from apps.wsgw.constants import CLOSE_CAPABILITY_NOT_ACCEPTED, CLOSE_CAPABILITY_UNAVAILABLE

log = logging.getLogger("meshychat.capability")

#: The header, as the design names it (3.1), and the key Django files it under.
HEADER = "Meshychat-Capability"
META_KEY = "HTTP_MESHYCHAT_CAPABILITY"
_WIRE_NAME = b"meshychat-capability"

CODE = "E_CAPABILITY_UNAVAILABLE"
MESSAGE = ("capability tokens are not available on this server; "
           "a request carrying Meshychat-Capability is refused, not served "
           "without it")
#: Tokens on, and the route (or the socket) takes none (slice 4).
NOT_ACCEPTED_CODE = "E_CAPABILITY_NOT_ACCEPTED"
NOT_ACCEPTED_MESSAGE = ("this route does not take a capability token; a request carrying "
                        "Meshychat-Capability is refused, not served without it")
#: 403, chosen here (the design names only the code): RFC 9110 15.5.4, "if authentication
#: credentials were provided in the request, the server considers them insufficient to grant
#: access". The design's other capability refusals are 403 too (5.2, `E_CAPABILITY_INVALID`).
#: The reasoning against 400, 401, 421 and 501 is in the project's design log (not part of this release) 040.
STATUS = 403

#: The upgrade's refusal, `CLOSE_CAPABILITY_UNAVAILABLE` (4414), is registered with the hub's
#: other close codes in apps/wsgw/constants.py, where a later branch picks a free one. A close
#: before accept reaches the client as an HTTP 403 whatever its code (daphne 4.2.3
#: `serverReject`; uvicorn likewise), which is the HTTP answer above. The code is visible
#: in-process, and distinct from the hub's own 4403 (a refused Origin) so a test can tell who
#: refused. 4413 is the design's, for a revoked capability (2.6).

#: THE accept list. Empty in slices 1 to 4; since slice 5 the revoke route, which reads the
#: token it revokes from this header; since slice 6 native invoke and A2A message:send, the
#: envelope path (divergence 040, slices 5 and 6). The mesh GETs (slice 7) and the socket
#: (slice 8) are not on it.
ACCEPTED: frozenset[tuple[str, str]] = frozenset({
    ("http", "v1/memory/capabilities/revoke"),
    ("http", "v1/agents/<str:peer_id>/skills/<str:name>/invoke"),
    ("http", "a2a/agents/<str:peer_id>/skills/<str:skill_name>/message:send"),
})


def carries_capability(meta) -> bool:
    """True when an HTTP request carries the header, whatever its value."""
    return META_KEY in meta


def presented(meta) -> str | None:
    """The token an HTTP request presents; None when it presents none. Every route that takes a
    token reads it here."""
    return meta.get(META_KEY)


def upgrade_carries_capability(headers) -> bool:
    """True when a WebSocket scope's raw headers carry it, in any case, whatever its value."""
    for name, _value in headers:
        if isinstance(name, str):
            name = name.encode("latin-1")
        if bytes(name).lower() == _WIRE_NAME:
            return True
    return False


def _loggable(value) -> str:
    """A client-chosen string as one log line can hold it: `unicode_escape`d, as Django's
    `log_message` escapes its arguments. A newline, a carriage return, an escape sequence and
    every non-ASCII character (U+2028 and NEL included) arrive as escapes, and a backslash
    doubled, so an escape the client typed cannot pass for one this function wrote."""
    if not isinstance(value, str):
        return repr(value)
    return value.encode("unicode_escape").decode("ascii")


def tokens_on() -> bool:
    """settings.MESHY_CAPABILITIES, read per request (so override_settings works without a
    restart), and only as a real bool: the string "false" is truthy and would read as ON
    while looking like OFF. settings.py parses the environment into a bool once."""
    value = settings.MESHY_CAPABILITIES
    if not isinstance(value, bool):
        raise ImproperlyConfigured(
            f"settings.MESHY_CAPABILITIES must be a bool, got {type(value).__name__}; set "
            "MESHY_CAPABILITIES in the environment instead")
    return value


def refusal(code: str = CODE, message: str = MESSAGE) -> JsonResponse:
    """The server's error envelope, compact like every other body here."""
    return envelope(code, message, status=STATUS)


def http_route_accepted(path: str) -> bool:
    """Whether the path resolves to a route on the accept list. An unmounted path is not."""
    try:
        match = resolve(path)
    except Resolver404:
        return False
    return ("http", match.route) in ACCEPTED


class CapabilityHeaderMiddleware:
    """Refuse an HTTP request that carries the header before anything else runs: always while
    tokens are off, and on every route not on the accept list while they are on."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if carries_capability(request.META):
            # Method and path only: never the header's value.
            if not tokens_on():
                log.info("refused %s %s: %s (a capability header present, tokens are off)",
                         _loggable(request.method), _loggable(request.path), CODE)
                return refusal()
            if not http_route_accepted(request.path_info):
                log.info("refused %s %s: %s (a capability header present, the route takes none)",
                         _loggable(request.method), _loggable(request.path),
                         NOT_ACCEPTED_CODE)
                return refusal(NOT_ACCEPTED_CODE, NOT_ACCEPTED_MESSAGE)
        return self.get_response(request)


class RefuseCapabilityUpgrade:
    """ASGI wrapper for the websocket router: an upgrade carrying the header is closed
    before accept, and the router and its consumers never see it."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "websocket" and upgrade_carries_capability(
                scope.get("headers") or ()):
            if tokens_on():
                code, close, why = (NOT_ACCEPTED_CODE, CLOSE_CAPABILITY_NOT_ACCEPTED,
                                    "the socket takes none in v1")
            else:
                code, close, why = CODE, CLOSE_CAPABILITY_UNAVAILABLE, "tokens are off"
            log.info("refused upgrade %s: %s (a capability header present, %s)",
                     _loggable(scope.get("path", "")), code, why)
            message = await receive()
            if message.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": close, "reason": code})
            return
        await self.inner(scope, receive, send)


__all__ = ["ACCEPTED", "CLOSE_CAPABILITY_NOT_ACCEPTED", "CLOSE_CAPABILITY_UNAVAILABLE", "CODE",
           "HEADER", "META_KEY", "NOT_ACCEPTED_CODE", "CapabilityHeaderMiddleware",
           "RefuseCapabilityUpgrade", "carries_capability", "http_route_accepted",
           "presented", "refusal", "tokens_on", "upgrade_carries_capability"]
