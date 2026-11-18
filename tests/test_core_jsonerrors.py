"""The middleware that keeps an unhandled exception from leaving as HTML.

WHAT THIS FILE PROVES AND WHAT IT DOES NOT. These are unit tests of the conversion: given
an exception, what comes back. They do NOT prove the middleware is installed - that is a
wiring claim, and a test that supplies its own middleware list proves the class and says
nothing about `meshychat/settings.py`, exactly as a test supplying its own `ROOT_URLCONF`
proves a view and says nothing about the routing table.

The wiring claim is carried by `test_the_middleware_is_actually_installed` below plus the
end-to-end case in `tests/test_grants.py` (`max_executions=0`, which reaches a real
`CheckViolation` on a real database). Both must exist; neither substitutes for the other.

Run: .venv/Scripts/python.exe -m pytest tests/test_core_jsonerrors.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.exceptions import PermissionDenied, SuspiciousOperation  # noqa: E402
from django.http import Http404  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from apps.core import jsonerrors  # noqa: E402


@pytest.fixture()
def middleware():
    return jsonerrors.JsonErrorEnvelopeMiddleware(lambda request: None)


@pytest.fixture()
def request_():
    return RequestFactory().post("/v1/grants")


@pytest.mark.parametrize("exc", [
    KeyError("completed"),                 # meshguard: unknown resolve verdict
    TypeError("comparing strings with non-ASCII characters is not supported"),
    ValueError("nope"),
    RuntimeError("nope"),
])
def test_an_unhandled_exception_becomes_the_json_envelope(middleware, request_, exc):
    """The four measured shapes, plus the generic case. Each of these reached Django's
    HTML handler before this middleware existed."""
    response = middleware.process_exception(request_, exc)
    assert response is not None, f"{type(exc).__name__} was not converted"
    assert response.status_code == 500
    assert response["Content-Type"] == "application/json"
    assert json.loads(response.content) == {"error": "E_INTERNAL",
                                            "message": "internal error"}


def test_a_database_error_is_converted_and_its_detail_never_reaches_the_wire():
    """The grants case: `POST /v1/grants` with `max_executions=0` raises a psycopg
    `CheckViolation` whose DETAIL line lists **every column value of the attempted row**.
    The envelope must carry none of it."""
    import psycopg

    mw = jsonerrors.JsonErrorEnvelopeMiddleware(lambda r: None)
    exc = psycopg.errors.CheckViolation(
        'new row for relation "skill_grants" violates check constraint '
        '"skill_grants_max_executions_positive"\n'
        "DETAIL:  Failing row contains (dead-beef, secret-owner-uuid, 0, ...).")
    response = mw.process_exception(RequestFactory().post("/v1/grants"), exc)

    body = response.content.decode()
    assert json.loads(body) == {"error": "E_INTERNAL", "message": "internal error"}
    for leaked in ("skill_grants", "DETAIL", "secret-owner-uuid", "check constraint"):
        assert leaked not in body, f"{leaked!r} reached the wire"


def test_the_body_is_compact_like_every_success_body(middleware, request_):
    """`JsonResponse` defaults to `", "` / `": "`. The
    success bodies in this project are already compact, so a spaced error body would make
    the two paths disagree byte for byte."""
    response = middleware.process_exception(request_, RuntimeError("x"))
    assert response.content == b'{"error":"E_INTERNAL","message":"internal error"}'
    assert b", " not in response.content and b'": ' not in response.content


@pytest.mark.parametrize("exc", [Http404(), PermissionDenied(), SuspiciousOperation()])
def test_djangos_own_control_flow_exceptions_pass_through(middleware, request_, exc):
    """These already have handlers that produce the RIGHT status. Converting them would
    turn a 404 into a 500 - a fix that breaks four routes to fix one."""
    assert middleware.process_exception(request_, exc) is None


def test_an_apierror_passes_through_to_the_apps_own_handler(middleware, request_):
    """A missing conversion is a bug in that app's `_route`, and masking it here would
    flatten a 401 or a 409 into a 500 and hide the bug at the same time."""
    from apps.peers.errors import ApiError

    assert middleware.process_exception(request_, ApiError(409, "E_CONFLICT", "x")) is None


def test_the_exception_is_logged_in_full(middleware, request_, caplog):
    """The detail is not discarded, it is MOVED: server-side log, never the wire. A
    middleware that silently swallowed the cause would be worse than the HTML page."""
    with caplog.at_level("ERROR", logger="meshychat.core.errors"):
        middleware.process_exception(request_, RuntimeError("the specific cause"))
    assert "the specific cause" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "POST" in caplog.text and "/v1/grants" in caplog.text


def test_the_middleware_is_actually_installed():
    """THE WIRING CLAIM, made against the real settings module rather than an override.

    Without this, every test above passes on a class nobody has installed - which is the
    same failure as a route test that pins its own URLconf. It also pins the ORDER
    relative to the security-headers middleware: headers must wrap the error response, so
    `SecurityHeadersMiddleware` has to sit OUTSIDE (earlier in the list).
    """
    installed = list(settings.MIDDLEWARE)
    envelope = "apps.core.jsonerrors.JsonErrorEnvelopeMiddleware"
    headers = "apps.core.websec.SecurityHeadersMiddleware"
    assert envelope in installed, f"not installed: {installed}"
    assert headers in installed, f"not installed: {installed}"
    assert installed.index(headers) < installed.index(envelope), \
        "security headers must wrap the error response, so it goes first"


def test_envelope_is_the_one_body_behind_every_core_refusal():
    """`jsonerrors.envelope` replaced four hand-built copies of the same wire body: `json_500`,
    the 401 of `DatabaseConnectionMiddleware`, `capability_header.refusal` and
    `metrics._refusal`. This pins the helper's contract (status, the two keys, compact
    separators, `application/json`, headers passed through) and, for each site, the exact
    bytes, status and Content-Type it produced before: `json.dumps` with compact separators,
    which is what every site spelled out by hand, so a reader can check each line against
    the body it replaced."""
    from apps.core import capability_header, metrics
    from apps.core.middleware import DatabaseConnectionMiddleware, Unauthenticated

    def hand_built(code: str, message: str) -> bytes:
        return json.dumps({"error": code, "message": message}, separators=(",", ":")).encode()

    teapot = jsonerrors.envelope("E_TEAPOT", "short and stout", status=418,
                                 headers={"Retry-After": "7"})
    assert (teapot.status_code, teapot["Content-Type"], teapot["Retry-After"]) == (
        418, "application/json", "7")
    assert teapot.content == hand_built("E_TEAPOT", "short and stout") \
        == b'{"error":"E_TEAPOT","message":"short and stout"}'
    assert not jsonerrors.envelope("E_X", "x", status=400).has_header("Retry-After")

    sites = [
        (jsonerrors.json_500(), 500, "E_INTERNAL", "internal error"),
        (DatabaseConnectionMiddleware._unauthenticated(Unauthenticated()),
         401, "E_AUTH_MISSING", "missing auth"),
        (DatabaseConnectionMiddleware._unauthenticated(Unauthenticated("E_AUTH_INVALID", "bad")),
         401, "E_AUTH_INVALID", "bad"),
        (capability_header.refusal(), 403, capability_header.CODE, capability_header.MESSAGE),
        (capability_header.refusal(capability_header.NOT_ACCEPTED_CODE,
                                   capability_header.NOT_ACCEPTED_MESSAGE),
         403, capability_header.NOT_ACCEPTED_CODE, capability_header.NOT_ACCEPTED_MESSAGE),
        (metrics._refusal(503, "E_METRICS_DISABLED",
                          "metrics are off: set MESHY_METRICS_TOKEN to serve them"),
         503, "E_METRICS_DISABLED", "metrics are off: set MESHY_METRICS_TOKEN to serve them"),
        (metrics._refusal(401, "E_AUTH_INVALID", "invalid metrics token"),
         401, "E_AUTH_INVALID", "invalid metrics token"),
    ]
    for response, status, code, message in sites:
        assert response.status_code == status, (code, response.status_code)
        assert response["Content-Type"] == "application/json", code
        assert response.content == hand_built(code, message), (code, response.content)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
