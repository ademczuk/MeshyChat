"""A 5xx ApiError built without `detail` logs its message, on every surface that logs one.

Before the error classes were folded into `apps.peers.errors.ApiError`, the peers class had
no `detail` attribute, so `getattr(exc, "detail", exc.message)` in the calls/a2a scaffold and
in meshguard's route wrapper fell back to the message. The shared class always sets `detail`
(None unless `internal()` filled it), which turned that fallback off and logged "None".
These tests pin the message in the log line and keep `internal()`'s detail winning.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import RequestFactory  # noqa: E402

from apps.peers import errors  # noqa: E402


def _raising(exc):
    def handler(request, *args, **kwargs):
        raise exc
    return handler


def _calls_and_a2a_routes(monkeypatch, handler):
    from apps.a2a import views as a2a_views
    from apps.calls import views as calls_views
    from apps.core import dbconn

    monkeypatch.setattr(dbconn, "serving_connection",
                        lambda *a, **k: contextlib.nullcontext(object()))
    return (("meshychat.calls", calls_views._route("GET")(handler)),
            ("meshychat.a2a", a2a_views._route("GET")(handler)))


def test_a_5xx_without_detail_logs_its_message_on_both_transports(monkeypatch, caplog):
    handler = _raising(errors.ApiError(503, "E_SPACE_UNKNOWN", "space foo is not configured"))
    request = RequestFactory().get("/probe")
    for name, route in _calls_and_a2a_routes(monkeypatch, handler):
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            response = route(request, call_id="c-1")
        assert response.status_code == 503
        messages = [r.getMessage() for r in caplog.records if r.name == name]
        assert messages == ["GET /probe -> E_SPACE_UNKNOWN: space foo is not configured"], messages


def test_internal_detail_still_wins_and_stays_off_the_wire(monkeypatch, caplog):
    handler = _raising(errors.internal("db said no: relation is missing"))
    request = RequestFactory().get("/probe")
    for name, route in _calls_and_a2a_routes(monkeypatch, handler):
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            response = route(request, call_id="c-1")
        assert response.status_code == 500
        assert "relation is missing" not in response.content.decode()
        messages = [r.getMessage() for r in caplog.records if r.name == name]
        assert messages == ["GET /probe -> E_INTERNAL: db said no: relation is missing"], messages


def test_meshguard_logs_the_message_of_a_5xx_without_detail(monkeypatch, caplog):
    from apps.meshguard import views

    monkeypatch.setattr(views.db, "connection", lambda *a, **k: contextlib.nullcontext(object()))
    monkeypatch.setattr(views, "authenticate_peer", lambda conn, request: object())
    monkeypatch.setattr(views, "_owner", lambda conn, auth: "owner")
    route = views._route("GET")(_raising(errors.ApiError(503, "E_X", "gate store unreachable")))
    with caplog.at_level(logging.ERROR):
        response = route(RequestFactory().get("/probe"))
    assert response.status_code == 503
    messages = [r.getMessage() for r in caplog.records if r.name == "meshychat.meshguard"]
    assert messages == ["meshguard: gate store unreachable"], messages

    # CONTROL: internal()'s detail is what meshguard logs when it is set.
    caplog.clear()
    route = views._route("GET")(_raising(errors.internal("lookup failed: timeout")))
    with caplog.at_level(logging.ERROR):
        response = route(RequestFactory().get("/probe"))
    assert response.status_code == 500
    messages = [r.getMessage() for r in caplog.records if r.name == "meshychat.meshguard"]
    assert messages == ["meshguard: lookup failed: timeout"], messages
