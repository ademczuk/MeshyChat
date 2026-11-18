"""#129: a wrong method is answered 405 + Allow BEFORE any database connection opens.

apps/a2a/views.py has done this since STATUS C73 (tests/test_a2a.py::
test_a_wrong_method_is_answered_before_any_connection_is_opened). meshguard, grants, peers,
dashboard and calls answered it inside the connection block, so every 405 paid a full connect
and, with the database down, came back as a 500. The route method check runs before any
extractor.

Each app below gets, on one of its routes, with `apps.core.dbconn.connect` replaced by one that
records the attempt and raises as an unreachable server would:

  FINDING   a wrong method answers 405 with the route's `Allow`, and no connect is attempted;
  CONTROL   the right method still attempts the connect and fails as it did before this change
            (500), which also proves that the replaced `connect` sits on the request's path.

No database is needed: every connect is refused by the fixture.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The PROJECT settings, as test_dashboard.py and test_grants.py do, so the paths below resolve
# through meshychat.urls as mounted. A sibling module may have configured Django first; the
# fixture below layers what these tests depend on over whichever configuration won.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

CALL_ID = str(uuid.UUID(int=129))

#: (app, path, allowed, a method outside it). One route per app, each served by that app's
#: `_route`; the calls entries cover both of its decorations, GET and POST.
ROUTES = [
    ("meshguard", "/v1/meshguard/scans/pending", "GET", "post"),
    ("grants", "/v1/grants/issued", "GET", "post"),
    ("peers", "/v1/peers/presence", "GET", "post"),
    ("dashboard", "/v1/dashboard/api/peers", "GET", "post"),
    ("calls-get", "/v1/calls/" + CALL_ID, "GET", "delete"),
    ("calls-post", "/v1/calls/" + CALL_ID + "/cancel", "POST", "get"),
]


@pytest.fixture(autouse=True)
def _project_urls():
    with override_settings(ALLOWED_HOSTS=["*"], ROOT_URLCONF="meshychat.urls"):
        yield


@pytest.fixture
def unreachable(monkeypatch):
    """Every connect attempt is recorded, then refused the way a down server refuses it."""
    attempts: list = []

    def refuse(*args, **kwargs):
        attempts.append((args, kwargs))
        raise psycopg.OperationalError("fixture: the database is unreachable")

    monkeypatch.setattr("apps.core.dbconn.connect", refuse)
    return attempts


@pytest.mark.parametrize("app, path, allowed, wrong", ROUTES, ids=[r[0] for r in ROUTES])
def test_a_wrong_method_is_answered_without_a_connection(unreachable, app, path, allowed, wrong):
    response = getattr(Client(), wrong)(path)
    assert response.status_code == 405, (app, response.status_code)
    assert response.headers["Allow"] == allowed
    assert unreachable == [], f"{app}: a 405 attempted {len(unreachable)} connection(s)"


@pytest.mark.parametrize("app, path, allowed, wrong", ROUTES, ids=[r[0] for r in ROUTES])
def test_the_right_method_still_needs_the_database(unreachable, app, path, allowed, wrong):
    response = getattr(Client(raise_request_exception=False), allowed.lower())(path)
    assert response.status_code == 500, (app, response.status_code)
    assert unreachable, f"{app}: the allowed method never reached the connect this test refuses"
