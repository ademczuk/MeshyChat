"""`ApiError` is ONE class, shared by every surface - the grants and dashboard ones included.

WHAT THIS PINS AND WHY. Until 2026-10-02 `apps/grants/errors.py` and
`apps/dashboard/errors.py` each declared their own `ApiError`, byte-identical in
constructor, `str()` and `body()` to `apps/peers/errors.py`'s. Three classes of one shape
cost code everywhere the surfaces meet: `apps/peers/views.py::_owner_route` had to write
`except (errors.ApiError, owner_errors.ApiError)`, `apps/authapi/onboard.py` caught the two
in two otherwise-identical blocks, and `apps/core/jsonerrors.py` can only recognise the type
by its NAME. `apps/files`, `apps/skillsreg`, `apps/meshguard`, `apps/calls` and `apps/a2a`
had already imported peers' class for exactly that reason (`apps/skillsreg/errors.py` tells
the story of a 403 that would have become a 500).

The fold keeps every observable: the (status, code, message) each constructor answers, the
two-key body, `str(exc)`, the `detail` that `internal()` carries to the log and never to the
wire, and the class NAME the middleware compares. The identity test is the one that fails on
three classes; the others hold the contract each copy had, so a later "simplification" of
the shared class cannot quietly change one surface's wire.

Run:  pytest tests/test_apierror_one_class.py -q      (no database needed)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.a2a import views as a2a_views  # noqa: E402
from apps.calls import wire as calls_wire  # noqa: E402
from apps.dashboard import errors as dashboard_errors  # noqa: E402
from apps.files import errors as files_errors  # noqa: E402
from apps.grants import errors as grants_errors  # noqa: E402
from apps.meshguard import errors as meshguard_errors  # noqa: E402
from apps.peers import errors as peers_errors  # noqa: E402
from apps.skillsreg import errors as skillsreg_errors  # noqa: E402

#: Every module that spells `ApiError`, keyed by the surface it serves.
SURFACES = {
    "peers": peers_errors.ApiError,
    "grants": grants_errors.ApiError,
    "dashboard": dashboard_errors.ApiError,
    "files": files_errors.ApiError,
    "skillsreg": skillsreg_errors.ApiError,
    "meshguard": meshguard_errors.ApiError,
    "calls": calls_wire.ApiError,
    "a2a": a2a_views.ApiError,
}

#: The three modules whose constructors this file compares, by surface.
MODULES = {"peers": peers_errors, "grants": grants_errors, "dashboard": dashboard_errors}

#: The plain variants are defined once; every surface answers the same triple.
PLAIN = [
    ("not_found", (), (404, "E_NOT_FOUND", "not found")),
    ("forbidden", (), (403, "E_FORBIDDEN", "forbidden")),
    ("validation", ("v",), (400, "E_VALIDATION", "v")),
    ("conflict", ("c",), (409, "E_CONFLICT", "c")),
    ("auth_missing", (), (401, "E_AUTH_MISSING", "missing auth")),
    ("auth_invalid", ("a",), (401, "E_AUTH_INVALID", "a")),
]

#: The owner-credential variants grants and the dashboard both carry (not peers).
OWNER_CREDENTIAL = [
    ("session_expired", (), (401, "E_SESSION_EXPIRED", "session not active")),
    ("api_key_invalid", (),
     (401, "E_API_KEY_INVALID", "invalid, expired, or revoked API key")),
    ("scope_required", ("dashboard:read",),
     (403, "E_SCOPE_REQUIRED", "API key is missing the required scope: dashboard:read")),
    ("csrf_required", (), (403, "E_CSRF_REQUIRED", "missing or invalid CSRF token")),
]


def _triple(exc) -> tuple:
    return (exc.status, exc.code, exc.message)


def test_grants_and_dashboard_raise_the_class_every_other_surface_shares():
    """THE IDENTITY. One `except apps.peers.errors.ApiError` catches what any surface raises,
    which is what lets `_owner_route` and the onboarding routes keep one `except` for the
    dashboard extractor's refusals beside their own. Fails with three classes."""
    for name, cls in SURFACES.items():
        assert cls is peers_errors.ApiError, f"{name} declares its own ApiError"
    # The dashboard's one SUBCLASS stays a
    # subclass of the shared class, so the same `except` still catches it.
    assert issubclass(dashboard_errors.UnknownCapability, peers_errors.ApiError)

    for raised in (dashboard_errors.auth_missing(), grants_errors.csrf_required(),
                   dashboard_errors.unknown_capability("x")):
        with pytest.raises(peers_errors.ApiError) as caught:
            raise raised
        assert caught.value is raised


def test_the_shared_class_keeps_the_contract_each_copy_had():
    """grants' copy took `detail` as a keyword (its `_domain_status` passes it), the
    dashboard's copy always had a `.detail` attribute (its `_error_response` reads it for
    any 5xx), peers' copy was the `str()`/`body()` the wire is checked against, and
    `apps/core/jsonerrors.py` lets the type through by its NAME. All four, on one class."""
    plain = peers_errors.ApiError(404, "E_NOT_FOUND", "not found")
    assert plain.detail is None, "the attribute exists without a cause (dashboard contract)"
    assert str(plain) == "E_NOT_FOUND: not found" and plain.args == ("E_NOT_FOUND: not found",)
    assert plain.body() == {"error": "E_NOT_FOUND", "message": "not found"}

    carried = peers_errors.ApiError(500, "E_INTERNAL", "internal error", detail="the cause")
    assert carried.detail == "the cause"
    assert carried.body() == {"error": "E_INTERNAL", "message": "internal error"}, (
        "the detail is for the log; the body has exactly two keys")
    assert type(carried).__name__ == "ApiError", "the middleware's string compare"


@pytest.mark.parametrize("surface", ["peers", "grants", "dashboard"])
@pytest.mark.parametrize("name,args,expected", PLAIN)
def test_every_plain_constructor_answers_the_same_triple_on_every_surface(
        surface, name, args, expected):
    """The (status, code) pairing is what a client branches on. The grants and dashboard
    constructors were byte-identical to peers' and are now the same function; this holds
    the triple so the re-export cannot have moved a byte."""
    module = MODULES[surface]
    exc = getattr(module, name)(*args)
    assert isinstance(exc, peers_errors.ApiError)
    assert _triple(exc) == expected


@pytest.mark.parametrize("surface", ["grants", "dashboard"])
@pytest.mark.parametrize("name,args,expected", OWNER_CREDENTIAL)
def test_the_owner_credential_constructors_still_agree(surface, name, args, expected):
    """Both owner surfaces answer the same owner-credential refusals; these stayed in their
    own modules and must keep agreeing."""
    module = MODULES[surface]
    exc = getattr(module, name)(*args)
    assert isinstance(exc, peers_errors.ApiError)
    assert _triple(exc) == expected


@pytest.mark.parametrize("surface", ["peers", "grants", "dashboard"])
def test_internal_carries_the_cause_to_the_log_and_the_constant_to_the_wire(surface):
    """The detail is logged and the client gets the CONSTANT. Each surface's
    `internal()` kept its own body (two set the attribute, one passes the keyword); all
    three must land on the same exception."""
    module = MODULES[surface]
    exc = module.internal("SELECT secret FROM table")
    assert isinstance(exc, peers_errors.ApiError)
    assert _triple(exc) == (500, "E_INTERNAL", "internal error")
    assert exc.detail == "SELECT secret FROM table"
    assert "SELECT" not in str(exc.body()) and "detail" not in exc.body()
