"""T16, the tokens-off half: `Meshychat-Capability` is REFUSED on every route and every upgrade.

B07 slice 1 (docs/design/biscuit-capabilities.md 3.1, "Today's silent window", and critique
C16; docs/DIVERGENCES.md 040). This server has no capability-token verifier, and before this
slice it ignored the header like any other it does not read: a client that sent
`Meshychat-Capability` was authorised by its database grants alone and had no way to learn that
the token it attached was never looked at. A capability NARROWS what its holder may do; a
server that ignores it silently widens the request back to everything the grants allow.

WHAT IS WALKED, AND WHY NOTHING IS LISTED HERE. The walk starts from what the server MOUNTS,
`meshychat.asgi.application`, not from a module list beside it: its protocols must be exactly
`http` and `websocket`; `http` must be Django's own handler, whose routes are
`django.urls.get_resolver()`'s, followed through every include; `websocket` must be the refusal
wrapper, and the routes are those of the layers under it, channels routers nested or not. A
layer the walk does not know FAILS it, so a route mounted beside the refusal, in asgi.py or in
a wrapper, cannot go unwalked (review F3). No route is written in this file, so a route mounted
later is walked the day it lands: it is refused, or this file fails until someone decides for
it on the ONE accept list (`apps.core.capability_header.ACCEPTED`). A route whose request path
the walk cannot build FAILS the walk rather than dropping out of it (`UnbuildableRoute`).

HOW "THE VIEW NEVER RAN" IS SHOWN. For its own test, each walked pattern's view (or consumer)
is replaced by a spy that counts its calls. A refusal applied AFTER the view ran would pass
every status check and fails the count; the same request without the header must then reach
the spy, once per method, or the refusal proves nothing.

THE REQUESTS GO THROUGH THE PROJECT'S OWN ASGI APPLICATION (`meshychat.asgi.application`) as
raw scopes, the shape daphne hands over: the settings' middleware stack, and header names as
bytes, in whatever case the scope carries them. An upgrade carries a real browser's upgrade
headers, in the order they arrive, with the capability header in their MIDDLE unless a test
places it first, last or twice (review F4).

THE VALUE IS HIGH-ENTROPY, like a real token, and a leak is ANY six consecutive characters of
it, found in a body, a response header, a close message, a log record, the formatted log
output, stdout or stderr (review F2): a leak of a token's first few characters is a leak.

Every negative has its positive control in the same test (docs/V1-DESIGN-RULES.md C1): the
header-free request reaches the spy or the real view with that view's own answer (/v1/health
unchanged, an authenticated route's own 401, an unmounted path's 404); an unrelated header and
four that nearly spell this one are still ignored; the walk finds at least as many HTTP routes
as the url modules on disk declare, so a walker that finds nothing fails.

Run:  pytest tests/test_capability_header_refused.py -q
The real-view controls need the dev database (conftest.py builds and drops its own).
"""
from __future__ import annotations

import asyncio
import importlib
import io
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from asgiref.testing import ApplicationCommunicator  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.testing import HttpCommunicator  # noqa: E402
from django.core.handlers.asgi import ASGIHandler  # noqa: E402
from django.http import HttpResponse  # noqa: E402
from django.urls import URLPattern, URLResolver, get_resolver, resolve  # noqa: E402
from django.urls import path as dj_path  # noqa: E402
from django.urls import converters as dj_converters  # noqa: E402
from django.urls.exceptions import Resolver404  # noqa: E402
from django.urls.resolvers import RegexPattern, RoutePattern  # noqa: E402

from apps.wsgw import routing as ws_routing  # noqa: E402
from meshychat.asgi import application  # noqa: E402

# Literals, not imports from the module under test, so this file runs on a tree that does not
# have it: RED there is "the header was ignored", not an ImportError.
CODE = "E_CAPABILITY_UNAVAILABLE"
STATUS = 403
CLOSE_CODE = 4414
#: The wrapper that must be the websocket protocol's OUTERMOST layer, named, not imported (above).
REFUSAL_LAYER = "apps.core.capability_header.RefuseCapabilityUpgrade"

#: The token text the tests send: high-entropy, like a real token's base64url, and every run of
#: LEAK_WINDOW characters in it holds a character that is not a hex digit, so no request id or
#: hash can hold one by chance. A distinctive WORD would not do: "capability" is in the refusal's
#: own message, and a check for the whole value misses a leak of its first ten characters.
VALUE = b"t16Qz7XkP2vW9mRj4LcY8nBd3HsFwT"
LEAK_WINDOW = 6


def leaks(text) -> list[str]:
    """Every LEAK_WINDOW-character run of VALUE that occurs in `text` (str or bytes)."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("latin-1")
    value = VALUE.decode()
    runs = {value[i:i + LEAK_WINDOW] for i in range(len(value) - LEAK_WINDOW + 1)}
    return sorted(run for run in runs if run in text)

#: Every spelling a scope can carry. daphne and h11 lowercase header names; the ASGI spec does
#: not promise it, so the capitalised and mixed forms are sent as raw bytes too.
SPELLINGS = (b"meshychat-capability", b"Meshychat-Capability", b"MESHYCHAT-CAPABILITY",
             b"mEsHyChAt-cApAbIlItY")
#: The empty value is a header that is present: it is refused like any other.
VALUES = (VALUE, b"")
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")

#: Headers that must NOT be refused. Four nearly spell it; the underscore form
#: are dropped before any view by daphne (CVE-2015-0219) and by Django's ASGI request, so they
#: are not this header on HTTP, and the upgrade applies the same rule.
NEAR_MISSES = (
    (b"meshychat-capabilities", VALUE),
    (b"x-meshychat-capability", VALUE),
    (b"meshychat-capability-v2", VALUE),
    (b"meshychat_capability", VALUE),
    (b"x-t16-unrelated", VALUE),
)

HOST = (b"host", b"localhost")
TIMEOUT = 10

#: A browser's upgrade request, in the order daphne and uvicorn pass headers on (the order they
#: arrived; neither sorts them). No Origin and no Sec-WebSocket-Protocol: the hub's own answer
#: to a credential-free upgrade is the control below, and those would change it.
UPGRADE_HEADERS = (
    HOST,
    (b"connection", b"Upgrade"),
    (b"upgrade", b"websocket"),
    (b"sec-websocket-version", b"13"),
    (b"sec-websocket-key", b"dGhlIHNhbXBsZSBub25jZQ=="),
    (b"sec-websocket-extensions", b"permessage-deflate; client_max_window_bits"),
    (b"user-agent", b"t16/1.0"),
)
#: Where an upgrade's extra headers go unless a test says otherwise: neither first nor last.
MIDDLE = 3


def upgrade_headers(extra=(), at: int = MIDDLE) -> list[tuple[bytes, bytes]]:
    return [*UPGRADE_HEADERS[:at], *extra, *UPGRADE_HEADERS[at:]]


def header_plan() -> list[tuple[str, bytes, bytes]]:
    """(method, spelling, value) for one route: every method, and across them every spelling
    with the token's value AND with the empty value. The methods are walked as many times as
    that takes (twice, for eight spellings and two values over eight methods)."""
    rounds = -(-len(SPELLINGS) * len(VALUES) // len(METHODS))
    plan = [(m, SPELLINGS[i % len(SPELLINGS)], VALUES[(i // len(SPELLINGS)) % len(VALUES)])
            for i, m in enumerate(METHODS * rounds)]
    assert {(s, v) for _m, s, v in plan} == {(s, v) for s in SPELLINGS for v in VALUES}
    return plan


# --------------------------------------------------------------------------- the walk
class UnbuildableRoute(AssertionError):
    """The walk met a route it cannot build a request path for. It FAILS, never skips."""


@dataclass(frozen=True)
class Route:
    protocol: str         # "http" or "websocket"
    route: str            # as ResolverMatch.route spells it, e.g. "v1/calls/<str:call_id>"
    path: str             # a concrete request path that resolves to it
    pattern: URLPattern   # the object whose callback serves it

    def __str__(self) -> str:
        return f"{self.protocol}:{self.route}"


#: django/urls/resolvers.py `_PATH_PARAMETER_COMPONENT_RE`.
_PARAM = re.compile(r"<(?:(?P<converter>[^>:]+):)?(?P<parameter>[^>]+)>")

#: One sample value per converter TYPE. A converter not named here fails the walk.
SAMPLES = {
    dj_converters.StringConverter: "t16-sample",
    dj_converters.PathConverter: "t16/sample",
    dj_converters.SlugConverter: "t16-sample",
    dj_converters.IntConverter: "16",
    dj_converters.UUIDConverter: "16161616-1616-4616-8616-161616161616",
}


def _concrete(patterns) -> tuple[str, str]:
    """(route, request path) for a chain of patterns, outermost first."""
    route, path = "", ""
    for pat in patterns:
        if not isinstance(pat, RoutePattern):
            raise UnbuildableRoute(
                f"{pat!r} is a {type(pat).__name__}: T16 builds request paths for path() "
                "routes only. Teach _concrete() this pattern type; do not let the route go "
                "unwalked.")
        text = str(pat)

        def sample(m, pat=pat):
            conv = pat.converters[m.group("parameter")]
            value = SAMPLES.get(type(conv))
            if value is None:
                raise UnbuildableRoute(
                    f"route {text!r}: no T16 sample for converter {type(conv).__name__}. "
                    "Add one to SAMPLES; do not let the route go unwalked.")
            return conv.to_url(conv.to_python(value))

        route += text
        path += _PARAM.sub(sample, text)
    return route, "/" + path


def walk_http() -> list[Route]:
    out: list[Route] = []

    def walk(resolver, prefix):
        for p in resolver.url_patterns:
            if isinstance(p, URLResolver):
                walk(p, prefix + [p.pattern])
            elif isinstance(p, URLPattern):
                route, path = _concrete(prefix + [p.pattern])
                out.append(Route("http", route, path, p))
            else:
                raise UnbuildableRoute(f"{p!r}: neither a URLPattern nor a URLResolver")

    walk(get_resolver(), [])
    return out


def _qualname(obj) -> str:
    return f"{type(obj).__module__}.{type(obj).__qualname__}"


def mounted_websocket_app():
    """What the server hands a WebSocket scope to."""
    if not isinstance(application, ProtocolTypeRouter):
        raise UnbuildableRoute(
            f"meshychat.asgi.application is a {_qualname(application)}, not channels' "
            "ProtocolTypeRouter: T16 cannot see what it mounts")
    return application.application_mapping["websocket"]


def walk_websocket(app=None) -> list[Route]:
    """Every leaf route the websocket application serves: by default the one
    `meshychat.asgi.application` MOUNTS, so a route mounted beside `websocket_urlpatterns`
    is walked too. Layers it knows: the refusal wrapper (through `.inner`) and channels'
    `URLRouter` (through `.routes`; a route whose callback carries the `_path_routing` flag
    channels prefix-matches on is a nested router). Any other layer FAILS the walk: T16
    cannot see which paths it routes, so it cannot send them an upgrade."""
    out: list[Route] = []

    def walk_app(layer, prefix):
        if _qualname(layer) == REFUSAL_LAYER:
            walk_app(layer.inner, prefix)
        elif isinstance(layer, URLRouter):
            walk_patterns(layer.routes, prefix)
        else:
            raise UnbuildableRoute(
                f"websocket layer {_qualname(layer)} ({layer!r}): T16 cannot see which routes "
                "it serves. Teach walk_websocket() this layer; do not let its routes go "
                "unwalked.")

    def walk_patterns(patterns, prefix):
        for p in patterns:
            if not isinstance(p, URLPattern):
                raise UnbuildableRoute(f"websocket route {p!r} is not a URLPattern")
            if getattr(p.callback, "_path_routing", False):
                walk_app(p.callback, prefix + [p.pattern])      # a nested router
                continue
            route, request_path = _concrete(prefix + [p.pattern])
            out.append(Route("websocket", route, request_path, p))

    walk_app(mounted_websocket_app() if app is None else app, [])
    return out


def declared_http_route_count() -> int:
    """The HTTP routes the url modules ON DISK declare, counted without the resolver.

    A second instrument for the walk's population: `meshychat/urls.py` plus every
    `apps/<app>/urls.py` (the glob tests/test_every_url_module_is_mounted.py derives), each
    module's own `urlpatterns`, an include of an inline list followed, an include of a module
    not (that module is counted by the glob, or, if it is outside the glob, only the walk
    finds it, which the >= below allows).
    """
    modules = ["meshychat.urls"] + sorted(
        f"apps.{p.parent.name}.urls" for p in (ROOT / "apps").glob("*/urls.py"))

    def count(patterns) -> int:
        n = 0
        for p in patterns:
            if isinstance(p, URLPattern):
                n += 1
            elif isinstance(p, URLResolver) and isinstance(p.urlconf_name, (list, tuple)):
                n += count(p.urlconf_name)
        return n

    return sum(count(importlib.import_module(m).urlpatterns) for m in modules)


def _walk_or_error(walker) -> tuple[list[Route], Exception | None]:
    """Each walk on its own: a websocket layer the walker cannot see into must not empty the
    HTTP population too. The error is reported by the walk test, not as a collection error."""
    try:
        return walker(), None
    except UnbuildableRoute as exc:
        return [], exc


HTTP_ROUTES, HTTP_WALK_ERROR = _walk_or_error(walk_http)
WS_ROUTES, WS_WALK_ERROR = _walk_or_error(walk_websocket)


# --------------------------------------------------------------------------- transport
async def _http(method: str, path: str, headers=()) -> dict:
    comm = HttpCommunicator(application, method, path, headers=[HOST, *headers])
    response = await comm.get_response(timeout=TIMEOUT)
    await comm.wait(timeout=TIMEOUT)
    response["header_map"] = {bytes(k).lower(): bytes(v) for k, v in response["headers"]}
    return response


def http(method: str, path: str, headers=()) -> dict:
    return asyncio.run(_http(method, path, headers))


async def _upgrade(path: str, headers=(), at: int = MIDDLE) -> dict:
    """The application's first answer to a WebSocket upgrade, as the raw ASGI message.
    `headers` go into a browser's upgrade headers at index `at`: the middle by default."""
    scope = {"type": "websocket", "path": path, "query_string": b"",
             "headers": upgrade_headers(headers, at), "subprotocols": []}
    comm = ApplicationCommunicator(application, scope)
    await comm.send_input({"type": "websocket.connect"})
    first = await comm.receive_output(timeout=TIMEOUT)
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
    await comm.wait(timeout=TIMEOUT)
    return first


def upgrade(path: str, headers=(), at: int = MIDDLE) -> dict:
    return asyncio.run(_upgrade(path, headers, at))


def refusal_problems(response: dict) -> list[str]:
    """Everything wrong with `response` as the capability refusal; empty when it is one."""
    problems = []
    if response["status"] != STATUS:
        problems.append(f"status {response['status']}, not {STATUS}")
    body = response["body"]
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if not (isinstance(parsed, dict) and list(parsed) == ["error", "message"]
            and parsed["error"] == CODE and isinstance(parsed["message"], str)):
        problems.append(f"body is not {{'error': {CODE!r}, 'message': ...}}: {body[:160]!r}")
    echoed = leaks(body) + [run for v in response["header_map"].values() for run in leaks(v)]
    if echoed:
        problems.append(f"the header's value came back in the response: {echoed}")
    return problems


def make_view_spy():
    calls: list[str] = []

    def t16_view_spy(request, *args, **kwargs):
        calls.append(request.method)
        return HttpResponse(b"t16: the view ran", status=200, headers={"X-T16-View": "ran"})

    t16_view_spy.calls = calls
    return t16_view_spy


def make_consumer_spy():
    calls: list[str] = []

    async def t16_consumer_spy(scope, receive, send):
        calls.append(scope.get("path", ""))
        message = await receive()
        if message["type"] != "websocket.connect":
            return
        await send({"type": "websocket.accept"})
        while (await receive())["type"] != "websocket.disconnect":
            pass

    t16_consumer_spy.calls = calls
    return t16_consumer_spy


class captured_logs:
    """Every record from the root logger AND the two that do not propagate to it
    (`meshychat`, `django.request`: settings.LOGGING), at DEBUG."""

    NAMES = ("", "meshychat", "django.request")

    def __init__(self, caplog):
        self.caplog = caplog
        self.saved = []

    def __enter__(self):
        for name in self.NAMES:
            logger = logging.getLogger(name)
            self.saved.append((logger, logger.level))
            logger.setLevel(logging.DEBUG)
            if self.caplog.handler not in logger.handlers:
                logger.addHandler(self.caplog.handler)
        self.caplog.handler.setLevel(logging.DEBUG)
        return self.caplog

    def __exit__(self, *exc):
        for logger, level in self.saved:
            logger.setLevel(level)
            if logger.name != "root" and self.caplog.handler in logger.handlers:
                logger.removeHandler(self.caplog.handler)
        return False


class captured_streams:
    """The FORMATTED log output, what an operator reads: every plain `StreamHandler` in the
    logging tree (settings.LOGGING's `stdout` handler) writes into one buffer for the block.
    caplog sees records; this sees the lines they become."""

    def __enter__(self) -> io.StringIO:
        self.buffer = io.StringIO()
        self.swapped = []
        loggers = [logging.getLogger()] + [
            lg for lg in logging.Logger.manager.loggerDict.values()
            if isinstance(lg, logging.Logger)]
        seen = set()
        for logger in loggers:
            for handler in logger.handlers:
                if type(handler) is logging.StreamHandler and id(handler) not in seen:
                    seen.add(id(handler))
                    self.swapped.append((handler, handler.setStream(self.buffer)))
        assert self.swapped, "control: no StreamHandler in the logging tree to read"
        return self.buffer

    def __exit__(self, *exc):
        for handler, stream in self.swapped:
            handler.setStream(stream)
        return False


# --------------------------------------------------------------------------- the walk itself
def test_the_walk_reaches_every_declared_route_and_each_path_resolves_to_its_own():
    """The population the refusal tests below iterate. A blind walker fails here."""
    assert HTTP_WALK_ERROR is None, f"the HTTP walk could not build a route: {HTTP_WALK_ERROR}"
    assert WS_WALK_ERROR is None, f"the WebSocket walk could not build a route: {WS_WALK_ERROR}"

    declared = declared_http_route_count()
    assert declared > 0, "control: the url modules on disk declare no route at all"
    assert len(HTTP_ROUTES) >= declared, (
        f"the walk found {len(HTTP_ROUTES)} HTTP routes, the url modules declare {declared}")
    assert len(WS_ROUTES) >= len(ws_routing.websocket_urlpatterns) >= 1, (
        f"the walk found {len(WS_ROUTES)} WebSocket routes for "
        f"{len(ws_routing.websocket_urlpatterns)} patterns")
    walked_patterns = {id(r.pattern) for r in WS_ROUTES}
    assert all(id(p) in walked_patterns for p in ws_routing.websocket_urlpatterns), (
        "apps.wsgw.routing's patterns are not the ones the mounted application serves")

    keys = [str(r) for r in HTTP_ROUTES + WS_ROUTES]
    assert len(keys) == len(set(keys)), "a route was walked twice"

    # Each built path is a real request for ITS route, not one an earlier pattern shadows.
    for r in HTTP_ROUTES:
        match = resolve(r.path)
        assert match.route == r.route and match.func is r.pattern.callback, (
            f"{r.path} resolves to {match.route!r}, not to the walked {r.route!r}")
    for r in WS_ROUTES:
        assert r.pattern.pattern.match(r.path.lstrip("/")), f"{r.path} does not match {r.route}"

    # Controls on the instruments: resolve() can fail, and the builder refuses what it cannot
    # build instead of skipping it.
    with pytest.raises(Resolver404):
        resolve("/v1/t16-not-mounted")
    with pytest.raises(UnbuildableRoute):
        _concrete([RegexPattern(r"^t16/(?P<n>[0-9]+)$")])

    class T16Converter:
        regex = "[0-9]+"

        def to_python(self, value):
            return value

        def to_url(self, value):
            return value

    odd = RoutePattern("t16/<n>")
    odd.converters = {"n": T16Converter()}
    with pytest.raises(UnbuildableRoute):
        _concrete([odd])

    # The websocket walker on hand-built stacks: nested routers are followed, and a layer it
    # does not know fails it rather than hiding the routes under it.
    async def t16_leaf(scope, receive, send):
        pass

    nested = URLRouter([dj_path("v1/t16-a", t16_leaf),
                        dj_path("v1/t16-n/", URLRouter([dj_path("b/<int:n>", t16_leaf)]))])
    assert [(r.route, r.path) for r in walk_websocket(nested)] == [
        ("v1/t16-a", "/v1/t16-a"), ("v1/t16-n/b/<int:n>", "/v1/t16-n/b/16")]

    class T16Wrapper:
        def __init__(self, inner):
            self.inner = inner

    with pytest.raises(UnbuildableRoute):
        walk_websocket(T16Wrapper(nested))


def test_the_walk_starts_from_what_the_server_mounts():
    """Review F3. The walk must start from the application the server runs: a WebSocket
    route mounted in asgi.py BESIDE the refusal wrapper, not in apps.wsgw.routing, reached
    the consumer with the header while a walk of the module list passed."""
    assert isinstance(application, ProtocolTypeRouter), _qualname(application)
    mapping = application.application_mapping
    assert sorted(mapping) == ["http", "websocket"], (
        f"asgi.py mounts {sorted(mapping)}: a protocol T16 does not walk is one no refusal "
        "was proved on")
    assert isinstance(mapping["http"], ASGIHandler), (
        f"http is served by {_qualname(mapping['http'])}, not Django's handler: the "
        "middleware stack, and so the refusal, may not run for it")
    assert _qualname(mapping["websocket"]) == REFUSAL_LAYER, (
        f"the websocket protocol's outermost layer is {_qualname(mapping['websocket'])}, not "
        "the refusal: a route or wrapper outside it sees the header first")
    # CONTROL: the check can fail; a bare router is not the refusal.
    assert _qualname(mapping["websocket"].inner) != REFUSAL_LAYER


# --------------------------------------------------------------------------- HTTP, every route
@pytest.mark.parametrize("route", HTTP_ROUTES, ids=str)
def test_the_header_is_refused_on_every_http_route_before_its_view_runs(route, monkeypatch):
    """Every method, every spelling, the empty value: refused, and the view never ran.
    Control in the same test: without the header, and with near misses, the view runs."""
    spy = make_view_spy()
    monkeypatch.setattr(route.pattern, "callback", spy)

    async def exercise():
        with_header = [(m, s, v, await _http(m, route.path, [(s, v)]))
                       for m, s, v in header_plan()]
        ran_with_header = list(spy.calls)
        without = [(m, await _http(m, route.path)) for m in METHODS]
        near = await _http("GET", route.path, NEAR_MISSES)
        return with_header, ran_with_header, without, near

    with_header, ran_with_header, without, near = asyncio.run(exercise())

    failures = []
    for method, spelling, value, response in with_header:
        for problem in refusal_problems(response):
            failures.append(f"{method} {route.path} {spelling.decode()}={value.decode()!r}: "
                            f"{problem}")
    assert not failures and ran_with_header == [], (
        f"{route}: the view ran {len(ran_with_header)} times on requests carrying the "
        f"header ({ran_with_header}); " + "; ".join(failures))

    # POSITIVE CONTROL: the same requests without the header reach the view.
    for method, response in without:
        assert response["status"] == 200 and response["header_map"].get(b"x-t16-view") == b"ran", (
            f"control: {method} {route.path} without the header did not reach its view: "
            f"{response['status']} {response['body'][:120]!r}")
    assert near["status"] == 200 and near["header_map"].get(b"x-t16-view") == b"ran", (
        f"control: {route.path} with only near-miss headers was refused: {near['body'][:160]!r}")
    assert spy.calls == [*METHODS, "GET"], spy.calls


# --------------------------------------------------------------------------- HTTP, real views
def test_the_refusal_is_the_servers_json_error_shape():
    """`{"error","message"}`, compact like every body here, JSON, and still inside the
    security-header middleware (an error body is still a body a browser will sniff)."""
    response = http("GET", "/v1/health", [(b"meshychat-capability", VALUE)])
    assert refusal_problems(response) == [], refusal_problems(response)
    parsed = json.loads(response["body"])
    assert response["body"] == json.dumps(parsed, separators=(",", ":")).encode(), (
        f"not compact: {response['body']!r}")
    assert response["header_map"].get(b"content-type") == b"application/json"
    assert response["header_map"].get(b"x-content-type-options") == b"nosniff"


def test_health_is_unchanged_without_the_header_and_refused_with_it():
    # The seven baseline fields and rule B9's three (tests/test_health.py's pins; the three joined
    # when fin/post-r10 met integ/r11); tokens are off here, so no `capabilities`.
    from tests.test_health import B9_FIELDS, HEALTH_FIELDS
    fields = HEALTH_FIELDS + B9_FIELDS
    plain = http("GET", "/v1/health")
    assert plain["status"] in (200, 503) and list(json.loads(plain["body"])) == fields, (
        f"control: /v1/health without the header is not the health answer: {plain['body']!r}")
    unrelated = http("GET", "/v1/health", [(b"x-t16-unrelated", VALUE)])
    assert list(json.loads(unrelated["body"])) == fields, (
        f"control: an unrelated header changed /v1/health: {unrelated['body']!r}")

    refused = http("GET", "/v1/health", [(b"meshychat-capability", VALUE)])
    assert refusal_problems(refused) == [], (
        f"/v1/health with the header: {refused['status']} {refused['body'][:160]!r}")


#: Authenticated routes on the surfaces the design names (board, mesh GETs, calls invoke, A2A,
#: dashboard). Path segments are placeholders: authentication fails before they are read.
AUTHENTICATED = [
    ("GET", "/v1/calls"),
    ("POST", "/v1/board/acts"),
    ("GET", "/v1/mesh/inbox"),
    ("GET", "/v1/mesh/wait"),
    ("POST", "/v1/agents/t16-peer/skills/memory.read/invoke"),
    ("POST", "/a2a/agents/t16-peer/skills/memory.read/message:send"),
    ("GET", "/v1/dashboard/api/peers"),
]


@pytest.mark.parametrize("method,path", AUTHENTICATED)
def test_an_authenticated_route_answers_its_own_401_only_without_the_header(method, path):
    plain = http(method, path)
    assert plain["status"] == 401, (
        f"control: {method} {path} without credentials is not a 401: "
        f"{plain['status']} {plain['body'][:160]!r}")
    for headers in ([(b"meshychat-capability", VALUE)],
                    [(b"authorization", b"Bearer t16-not-a-jwt"), (b"Meshychat-Capability", b"")]):
        refused = http(method, path, headers)
        assert refusal_problems(refused) == [], (
            f"{method} {path} with {headers}: {refused['status']} {refused['body'][:160]!r}")


def test_an_unmounted_path_is_refused_too():
    """ANY route: the refusal does not depend on the path resolving."""
    plain = http("GET", "/v1/t16-not-mounted")
    assert plain["status"] == 404, f"control: {plain['status']}"
    refused = http("GET", "/v1/t16-not-mounted", [(b"meshychat-capability", VALUE)])
    assert refusal_problems(refused) == [], refused["body"][:160]


def test_the_value_is_never_repeated_in_a_body_a_header_a_close_a_log_or_stdout(caplog, capsys):
    """Review F2: both transports, any run of the value, every place output goes. The
    settings route every log line to stdout, so stdout and the formatted lines are read too,
    not only the log records."""
    with captured_logs(caplog), captured_streams() as formatted:
        print("t16-stdout-control")
        responses = [http("GET", "/v1/health", [(b"meshychat-capability", VALUE)]),
                     http("POST", "/v1/board/acts", [(b"Meshychat-Capability", VALUE)])]
        closes = [upgrade("/v1/ws", [(b"meshychat-capability", VALUE)]),
                  upgrade("/t16-no-socket", [(b"MESHYCHAT-CAPABILITY", VALUE)])]
    out = capsys.readouterr()
    for response in responses:
        assert refusal_problems(response) == [], refusal_problems(response)
    assert [c.get("code") for c in closes] == [CLOSE_CODE, CLOSE_CODE], closes
    records = "\n".join(r.getMessage() for r in caplog.records)
    places = {"close messages": repr(closes), "log records": records,
              "formatted log lines": formatted.getvalue(), "stdout": out.out,
              "stderr": out.err}
    found = {where: leaks(text) for where, text in places.items() if leaks(text)}
    assert not found, f"the value leaked: {found}"

    # CONTROLS: each capture saw this test's own output, so an absence above is not a deaf
    # instrument; and the detector finds a run of LEAK_WINDOW characters, not fewer.
    assert "t16-stdout-control" in out.out, out.out
    for line in ("refused POST /v1/board/acts", "refused upgrade /v1/ws",
                 "refused upgrade /t16-no-socket"):
        assert line in records, f"no log record holds {line!r}: {records}"
        assert line in formatted.getvalue(), f"no formatted line holds {line!r}"
    assert leaks("x" + VALUE.decode()[3:3 + LEAK_WINDOW] + "x") != []
    assert leaks(VALUE.decode()[:LEAK_WINDOW - 1]) == []


#: What ends or rewrites a line in a log file or on a terminal: the C0 controls, DEL, NEL and
#: the Unicode line and paragraph separators (str.splitlines() breaks on several of them).
LINE_BREAKERS = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")
FORGED = "FORGED INFO meshychat.capability all-clear"


def test_a_refused_path_cannot_forge_a_log_line(caplog):
    """Review F1. The refusal's log line carries the request path, which any client chooses,
    unauthenticated, on any path. It is escaped as Django's own request log escapes it
    (django/utils/log.py `log_message`, the CVE-2025-48432 fix): a newline arrives as the two
    characters backslash and n, inside the one line the refusal writes."""
    tail = "%0D%1B[2J%E2%80%A8%C2%85end"          # CR, ESC, U+2028, NEL: after a newline
    http_path = "/v1/t16%0A" + FORGED.replace(" ", "%20") + tail
    ws_path = "/v1/ws\n" + FORGED + "\r\x1b[2J\u2028\x85end"
    with captured_logs(caplog), captured_streams() as formatted:
        response = http("GET", http_path, [(b"meshychat-capability", VALUE)])
        closed = upgrade(ws_path, [(b"meshychat-capability", VALUE)])
    assert refusal_problems(response) == [], response["body"][:160]
    assert closed.get("code") == CLOSE_CODE, closed

    lines = [r.getMessage() for r in caplog.records if r.name == "meshychat.capability"]
    assert len(lines) == 2, lines
    for line in lines:
        assert not LINE_BREAKERS.search(line), f"a line breaker reached the log: {line!r}"
        # CONTROL: the path WAS logged, escaped, not dropped.
        assert FORGED in line and "\\n" in line and "\\u2028" in line, line
    forged = [ln for ln in formatted.getvalue().splitlines() if ln.startswith("FORGED")]
    assert forged == [], f"a forged line was written: {forged}"
    # CONTROL: the formatted output holds both refusals, so the absence is not a deaf stream.
    assert formatted.getvalue().count(CODE) == 2, formatted.getvalue()


# --------------------------------------------------------------------------- WebSocket
@pytest.mark.parametrize("route", WS_ROUTES, ids=str)
def test_an_upgrade_carrying_the_header_is_refused_before_the_consumer_runs(route, monkeypatch):
    spy = make_consumer_spy()
    monkeypatch.setattr(route.pattern, "callback", spy)

    # Every spelling, a value and empty, in the middle of a browser's upgrade headers; then
    # the header first, last, and twice (a value, then empty) (review F4).
    plan = [(f"{s.decode()}={v.decode()!r}", [(s, v)], MIDDLE)
            for s in SPELLINGS for v in VALUES]
    plan += [(f"at {at}", [(b"meshychat-capability", VALUE)], at)
             for at in (0, len(UPGRADE_HEADERS))]
    plan.append(("twice", [(b"meshychat-capability", VALUE), (b"x-t16-between", b"1"),
                           (b"Meshychat-Capability", b"")], MIDDLE))
    failures = []
    for label, headers, at in plan:
        first = upgrade(route.path, headers, at)
        if first.get("type") != "websocket.close" or first.get("code") != CLOSE_CODE:
            failures.append(f"{label}: {first}")
        elif leaks(repr(first)):
            failures.append(f"{label}: the value came back in {first}")
    assert not failures and spy.calls == [], (
        f"{route}: the consumer ran {len(spy.calls)} times; " + "; ".join(failures))

    # POSITIVE CONTROL: without the header, and with near misses, the upgrade reaches it.
    for headers in ((), NEAR_MISSES):
        first = upgrade(route.path, headers)
        assert first.get("type") == "websocket.accept", (
            f"control: {route.path} with {headers or 'no extra headers'} was not accepted: "
            f"{first}")
    assert spy.calls == [route.path, route.path], spy.calls


def test_the_real_hub_answers_for_itself_only_without_the_header():
    """No mocks: the mounted consumer's own refusal of a credential-free upgrade is the
    control, and the header replaces it with this one."""
    plain = upgrade("/v1/ws")
    assert plain.get("type") == "websocket.close" and plain.get("code") in (4401, 4500), (
        f"control: the hub's own answer to an upgrade with no credentials: {plain}")
    refused = upgrade("/v1/ws", [(b"meshychat-capability", VALUE)])
    assert refused.get("type") == "websocket.close" and refused.get("code") == CLOSE_CODE, (
        f"the hub was reached with the header: {refused}")


@pytest.mark.parametrize("unrouted", ["/v1/t16-no-socket", "/t16-no-socket",
                                      "/.well-known/t16-no-socket"])
def test_an_upgrade_to_an_unrouted_path_is_refused_too(unrouted):
    """Inside /v1/ and outside it (review F5): the refusal does not depend on the prefix."""
    refused = upgrade(unrouted, [(b"meshychat-capability", VALUE)])
    assert refused.get("code") == CLOSE_CODE, refused
    with pytest.raises(ValueError, match="No route found"):     # control: the router's own
        upgrade(unrouted)


def test_the_close_code_is_registered_with_the_hubs_close_codes():
    """Review F8. Every other hub close code is in apps/wsgw/constants.py, where a later
    branch picks a free one; 4414 is registered there, once, and the wrapper sends that one."""
    from apps.core import capability_header
    from apps.wsgw import constants

    codes = {n: v for n, v in vars(constants).items()
             if n.startswith("CLOSE_") and isinstance(v, int)}
    # CONTROL: the scan reads the block (4403 is the hub's refused Origin).
    assert codes.get("CLOSE_FORBIDDEN") == 4403, codes
    assert codes.get("CLOSE_CAPABILITY_UNAVAILABLE") == CLOSE_CODE, (
        f"{CLOSE_CODE} is not registered in apps/wsgw/constants.py: {codes}")
    assert [n for n, v in codes.items() if v == CLOSE_CODE] == ["CLOSE_CAPABILITY_UNAVAILABLE"]
    assert capability_header.CLOSE_CAPABILITY_UNAVAILABLE == CLOSE_CODE


# --------------------------------------------------------------------------- the accept list
def unknown_accept_entries(entries, walked) -> set:
    """Entries that name no walked (protocol, route)."""
    return {e for e in entries if e not in walked}


#: The accept list as it must be: slice 5's revoke route, which takes the token it revokes in
#: `Meshychat-Capability`, and slice 6's two envelope-path routes, native invoke and A2A
#: `message:send`, whose views pre-check the token and hand it to the memory handlers
#: (divergence 040, slices 5 and 6). Slice 1 to 4 held it empty.
ACCEPTED_NOW = frozenset({("http", "v1/memory/capabilities/revoke"),
                          ("http", "v1/agents/<str:peer_id>/skills/<str:name>/invoke"),
                          ("http", "a2a/agents/<str:peer_id>/skills/<str:skill_name>/message:send")})


def test_the_accept_list_is_one_frozenset_holding_the_revoke_route_and_the_envelope_path():
    """The routes that may be named as honouring the header: the revoke route (slice 5), which
    reads the token to revoke it, and the envelope path's two transports (slice 6, design 3.1),
    invoke and A2A message:send. No other: the mint takes no token, and the mesh GETs and the
    socket are later slices. Every entry must name a walked route; the checker's controls are
    below."""
    from apps.core import capability_header

    assert capability_header.ACCEPTED == ACCEPTED_NOW and isinstance(
        capability_header.ACCEPTED, frozenset), (
        f"the accept list is the revoke route and the envelope path, found "
        f"{capability_header.ACCEPTED!r}")

    walked = {(r.protocol, r.route) for r in HTTP_ROUTES + WS_ROUTES}
    assert unknown_accept_entries(capability_header.ACCEPTED, walked) == set()
    assert unknown_accept_entries({("http", "v1/health")}, walked) == set()
    assert unknown_accept_entries({("http", "v1/t16-typo"), ("websocket", "v1/health")},
                                  walked) == {("http", "v1/t16-typo"), ("websocket", "v1/health")}


def test_an_accepted_route_is_still_refused_while_tokens_are_off(monkeypatch):
    """Design 5.2: tokens disabled, header present, on ANY route. The list decides nothing
    until a verifier exists."""
    from apps.core import capability_header

    monkeypatch.setattr(capability_header, "ACCEPTED",
                        frozenset({("http", "v1/health"), ("websocket", "v1/ws")}))
    response = http("GET", "/v1/health", [(b"meshychat-capability", VALUE)])
    assert refusal_problems(response) == [], response["body"][:160]
    assert upgrade("/v1/ws", [(b"meshychat-capability", VALUE)]).get("code") == CLOSE_CODE


def test_the_real_accepted_route_is_refused_while_tokens_are_off():
    """The revoke route, on the REAL list (slice 5), is refused like any other while tokens are
    off: the middleware answers before its view."""
    response = http("POST", "/v1/memory/capabilities/revoke", [(b"meshychat-capability", VALUE)])
    assert refusal_problems(response) == [], response["body"][:160]


def test_the_middleware_runs_before_the_identity_and_database_middleware():
    from django.conf import settings

    ours = "apps.core.capability_header.CapabilityHeaderMiddleware"
    stack = list(settings.MIDDLEWARE)
    assert ours in stack, f"not registered: {stack}"
    assert stack.index(ours) < stack.index("apps.core.middleware.DatabaseConnectionMiddleware")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
