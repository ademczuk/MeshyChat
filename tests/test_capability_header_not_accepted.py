"""T16, the tokens-ON half: with MESHY_CAPABILITIES on, `Meshychat-Capability` on a route that is not
on the accept list is E_CAPABILITY_NOT_ACCEPTED (B07 slice 4; design
docs/design/biscuit-capabilities.md 4.3 and 5.2, "tokens enabled, header on a route not on the
accept list"; divergence 040). Off, it stays E_CAPABILITY_UNAVAILABLE
(tests/test_capability_header_refused.py, slice 1, whose walk this file reuses).

A token presented where the design says no is refused, never silently ignored. The accept list
(`apps.core.capability_header.ACCEPTED`) holds the revoke route since slice 5, which takes the
token it revokes in the header, and since slice 6 the envelope path's two routes, invoke and A2A
message:send, whose views pre-check the token and hand it to the memory handlers; with tokens on
every other route and every upgrade refuses the header. A WebSocket
upgrade is refused whatever the list says: v1 accepts no token on the socket (design 2.6, step
1), and its close code is 4415, beside the tokens-off 4414.

Every refusal is beside its control: the same request without the header reaches the view; a
route put on the accept list for the test, and the revoke route on the real one, let the header
through to their views.

Run:  pytest tests/test_capability_header_not_accepted.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_capability_header_refused import (  # noqa: E402
    HTTP_ROUTES,
    LEAK_WINDOW,
    VALUE,
    WS_ROUTES,
    _http,
    captured_logs,
    captured_streams,
    http,
    leaks,
    make_consumer_spy,
    make_view_spy,
    upgrade,
)

from django.test import override_settings  # noqa: E402

CODE = "E_CAPABILITY_NOT_ACCEPTED"
OFF_CODE = "E_CAPABILITY_UNAVAILABLE"
STATUS = 403
CLOSE_CODE = 4415
OFF_CLOSE_CODE = 4414

ON = override_settings(MESHY_CAPABILITIES=True)


def not_accepted_problems(response: dict) -> list[str]:
    problems = []
    if response["status"] != STATUS:
        problems.append(f"status {response['status']}")
    try:
        body = json.loads(response["body"])
    except ValueError:
        body = None
    if not (isinstance(body, dict) and list(body) == ["error", "message"]
            and body["error"] == CODE):
        problems.append(f"body {response['body'][:160]!r}")
    echoed = leaks(response["body"]) + [r for v in response["header_map"].values()
                                        for r in leaks(v)]
    if echoed:
        problems.append(f"the value came back: {echoed}")
    return problems


@pytest.mark.parametrize("route", HTTP_ROUTES, ids=str)
def test_with_tokens_on_the_header_is_not_accepted_on_every_http_route(route, monkeypatch):
    """Every walked route not on the accept list refuses the header before its view; a route
    ON the list (slice 5: the revoke route) lets it through to its view, the list's positive
    control on the real list."""
    from apps.core import capability_header

    accepted = ("http", route.route) in capability_header.ACCEPTED
    spy = make_view_spy()
    monkeypatch.setattr(route.pattern, "callback", spy)

    async def exercise():
        with_header = [await _http(m, route.path, [(b"meshychat-capability", v)])
                       for m in ("GET", "POST") for v in (VALUE, b"")]
        ran = list(spy.calls)
        without = await _http("GET", route.path)
        return with_header, ran, without

    with ON:
        with_header, ran, without = asyncio.run(exercise())
    if accepted:
        assert ran == ["GET", "GET", "POST", "POST"] and all(
            r["status"] == 200 and r["header_map"].get(b"x-t16-view") == b"ran"
            for r in with_header), (str(route), ran, [r["status"] for r in with_header])
    else:
        failures = [p for r in with_header for p in not_accepted_problems(r)]
        assert not failures and ran == [], (str(route), ran, failures)
    assert without["status"] == 200 and without["header_map"].get(b"x-t16-view") == b"ran", (
        f"control: {route.path} without the header did not reach its view")


def test_the_revoke_route_is_on_the_accept_list_and_the_header_reaches_its_view():
    """T16's positive control for the revoke route (slice 5), through its REAL view: with
    tokens on and a key store loaded, a request carrying the header is answered by the route
    itself (here its own 401, no credentials), not by the middleware. CONTROLS: the mint route,
    not on the list, is refused NOT_ACCEPTED on the same request; with tokens off the revoke
    route is refused UNAVAILABLE."""
    import os as _os

    from apps.memoryskills import capability_keys

    private = _os.urandom(32)
    store = capability_keys.RootKeyStore(private, 3, {3: capability_keys.public_of(private)})
    previous = capability_keys.install(store)
    try:
        with ON:
            revoke = http("POST", "/v1/memory/capabilities/revoke",
                          [(b"meshychat-capability", VALUE)])
            mint = http("POST", "/v1/memory/capabilities", [(b"meshychat-capability", VALUE)])
    finally:
        capability_keys.install(previous)
    assert revoke["status"] == 401 and json.loads(revoke["body"])["error"] == "E_AUTH_MISSING", (
        revoke["status"], revoke["body"][:200])
    assert not leaks(revoke["body"])
    assert not_accepted_problems(mint) == [], mint["body"][:200]
    off = http("POST", "/v1/memory/capabilities/revoke", [(b"meshychat-capability", VALUE)])
    assert json.loads(off["body"])["error"] == OFF_CODE


@pytest.mark.parametrize("route", WS_ROUTES, ids=str)
def test_with_tokens_on_an_upgrade_carrying_the_header_is_closed_4415(route, monkeypatch):
    spy = make_consumer_spy()
    monkeypatch.setattr(route.pattern, "callback", spy)
    with ON:
        refused = [upgrade(route.path, [(b"meshychat-capability", v)]) for v in (VALUE, b"")]
        plain = upgrade(route.path)
    assert [(r.get("type"), r.get("code")) for r in refused] == [
        ("websocket.close", CLOSE_CODE)] * 2, refused
    assert spy.calls == [route.path], "the consumer ran with the header, or not without it"
    assert plain.get("type") == "websocket.accept"


def test_off_is_still_unavailable_and_on_is_not_accepted_on_the_same_request():
    """The one variable is the flag."""
    off = http("GET", "/v1/health", [(b"meshychat-capability", VALUE)])
    assert json.loads(off["body"])["error"] == OFF_CODE
    assert upgrade("/v1/ws", [(b"meshychat-capability", VALUE)]).get("code") == OFF_CLOSE_CODE
    with ON:
        on = http("GET", "/v1/health", [(b"meshychat-capability", VALUE)])
        closed = upgrade("/v1/ws", [(b"meshychat-capability", VALUE)])
    assert not_accepted_problems(on) == [], on["body"]
    assert closed.get("code") == CLOSE_CODE, closed


def test_an_unmounted_path_is_not_accepted_either():
    with ON:
        refused = http("GET", "/v1/t16-not-mounted", [(b"meshychat-capability", VALUE)])
        plain = http("GET", "/v1/t16-not-mounted")
    assert not_accepted_problems(refused) == [], refused["body"]
    assert plain["status"] == 404, "control: the path is not mounted"


def test_a_route_on_the_accept_list_lets_the_header_through_to_its_view(monkeypatch):
    """The positive control T16 names: a route on the list is not refused by the middleware
    with tokens on (its view decides). Off, even it refuses."""
    from apps.core import capability_header

    monkeypatch.setattr(capability_header, "ACCEPTED", frozenset({("http", "v1/health")}))
    with ON:
        through = http("GET", "/v1/health", [(b"meshychat-capability", VALUE)])
        other = http("GET", "/v1/livez" if any(r.route == "v1/livez" for r in HTTP_ROUTES)
                     else "/v1/t16-not-mounted", [(b"meshychat-capability", VALUE)])
    assert through["status"] in (200, 503) and "status" in json.loads(through["body"]), (
        through["body"][:160])
    assert not_accepted_problems(other) == [], other["body"]
    off = http("GET", "/v1/health", [(b"meshychat-capability", VALUE)])
    assert json.loads(off["body"])["error"] == OFF_CODE


def test_a_websocket_accept_entry_does_not_open_the_socket(monkeypatch):
    """v1 takes no token on the socket (design 2.6): an upgrade is refused whatever the list
    says."""
    from apps.core import capability_header

    monkeypatch.setattr(capability_header, "ACCEPTED", frozenset({("websocket", "v1/ws")}))
    with ON:
        assert upgrade("/v1/ws", [(b"meshychat-capability", VALUE)]).get("code") == CLOSE_CODE


def test_the_value_is_not_repeated_and_the_log_line_names_the_refusal(caplog):
    with captured_logs(caplog), captured_streams() as formatted:
        with ON:
            response = http("POST", "/v1/board/acts", [(b"Meshychat-Capability", VALUE)])
            closed = upgrade("/v1/ws", [(b"meshychat-capability", VALUE)])
    assert not_accepted_problems(response) == []
    records = "\n".join(r.getMessage() for r in caplog.records)
    for text in (repr(closed), records, formatted.getvalue()):
        assert not leaks(text), leaks(text)
    assert "refused POST /v1/board/acts" in records and CODE in records, records
    assert "refused upgrade /v1/ws" in records, records
    assert leaks("x" + VALUE.decode()[2:2 + LEAK_WINDOW]), "control: the detector works"


def test_the_not_accepted_close_code_is_registered_with_the_hubs():
    from apps.core import capability_header
    from apps.wsgw import constants

    codes = {n: v for n, v in vars(constants).items()
             if n.startswith("CLOSE_") and isinstance(v, int)}
    assert codes.get("CLOSE_CAPABILITY_NOT_ACCEPTED") == CLOSE_CODE, codes
    assert [n for n, v in codes.items() if v == CLOSE_CODE] == ["CLOSE_CAPABILITY_NOT_ACCEPTED"]
    assert capability_header.CLOSE_CAPABILITY_NOT_ACCEPTED == CLOSE_CODE
    assert codes.get("CLOSE_CAPABILITY_UNAVAILABLE") == OFF_CLOSE_CODE      # control


def test_the_middleware_reads_the_flag_per_request_and_only_as_a_bool():
    """override_settings takes effect without a restart (the setting is read per request, the
    tests above), and a setting that is not a real bool is not read as truthy ("false" is a
    non-empty string): the request fails closed, a 500 that serves nothing and repeats
    nothing. settings.py only ever holds a bool (apps/memoryskills/capability_keys.py)."""
    with override_settings(MESHY_CAPABILITIES="false"):
        broken = http("GET", "/v1/health", [(b"meshychat-capability", VALUE)])
    assert broken["status"] == 500 and not leaks(broken["body"]), broken
