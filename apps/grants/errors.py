"""The error surface for /v1/grants/* and /v1/access-requests/*.

Body shape: `{"error": <code>, "message": <text>}`. `approval_id` and `expires_at`
are omitted when absent, and nothing on this surface sets them.

THE PAIRING IS THE CONTRACT. A client branches on (status, code), so each constructor
below uses its assigned pairing rather than a plausible-looking code.
The three that are easy to get wrong on this surface:

  * `NotGrantor` / `NotRequester` / `NotGranter` are **403 E_FORBIDDEN**, not 404.
    They deliberately DISCLOSE that the row exists.
  * `update_grant` finding nothing is **404** whether the grant is missing, revoked,
    or owned by someone else (no cross-owner leakage) - the opposite decision from the
    one above.
  * `NotPending` and `DuplicateActive`/`DuplicatePending` are **409 E_CONFLICT** with
    the domain error's own text on the wire.
    The message is reproduced verbatim in `service.py`, because a client that branches
    on it is branching on those bytes.

The internal helper logs the detail server-side, keeps it OFF the wire, and returns the
constant "internal error". Leaking it would disclose internal detail.

`ApiError` is IMPORTED from `apps.peers.errors`, not redefined, as `apps/files`,
`apps/skillsreg` and `apps/meshguard` already do: one class means one `except` wherever
this surface meets another (`apps/authapi/onboard.py`, `apps/peers/views.py`). The plain
constructors whose (status, code, message) apply on every surface come with it;
the owner-credential and request-parsing constructors below are this surface's own.
"""
from __future__ import annotations

# One class, imported rather than re-declared - see the module docstring.
from apps.peers.errors import (  # noqa: F401
    ApiError,
    auth_invalid,
    auth_missing,
    conflict,
    forbidden,
    not_found,
    validation,
)

# Exact strings, not paraphrases.
E_NOT_FOUND = "E_NOT_FOUND"
E_FORBIDDEN = "E_FORBIDDEN"
E_INTERNAL = "E_INTERNAL"
E_VALIDATION = "E_VALIDATION"
E_CONFLICT = "E_CONFLICT"
E_AUTH_MISSING = "E_AUTH_MISSING"
E_AUTH_INVALID = "E_AUTH_INVALID"
#: The three owner-credential codes.
E_SESSION_EXPIRED = "E_SESSION_EXPIRED"
E_API_KEY_INVALID = "E_API_KEY_INVALID"
E_SCOPE_REQUIRED = "E_SCOPE_REQUIRED"
E_CSRF_REQUIRED = "E_CSRF_REQUIRED"


def session_expired() -> ApiError:
    """A cookie that resolves to no active row."""
    return ApiError(401, E_SESSION_EXPIRED, "session not active")


def api_key_invalid() -> ApiError:
    """A meshypat_ token that resolves to nothing. 401."""
    return ApiError(401, E_API_KEY_INVALID, "invalid, expired, or revoked API key")


def scope_required(scope: str) -> ApiError:
    """A PAT missing a route's scope. 403, not 401."""
    return ApiError(403, E_SCOPE_REQUIRED,
                    f"API key is missing the required scope: {scope}")


def csrf_required() -> ApiError:
    """Cookie auth on a state-changing method."""
    return ApiError(403, E_CSRF_REQUIRED, "missing or invalid CSRF token")


def internal(detail: str) -> ApiError:
    return ApiError(500, E_INTERNAL, "internal error", detail=detail)


# --- request parsing rejections -------------------------------------------------
# These are JSON and UUID parsing failures before a handler runs. Only the STATUS is
# fixed by contract here; the body is this app's JSON envelope rather than a framework
# plain-text message. views.py's module docstring states this as a bounded decision.

def json_syntax_error(message: str) -> ApiError:
    """Malformed JSON -> 400."""
    return ApiError(400, E_VALIDATION, message)


def json_data_error(message: str) -> ApiError:
    """Well-formed JSON with invalid data -> 422 Unprocessable Entity.

    A body that IS valid JSON but does not deserialize into the target type (missing
    field, wrong type) is 422, not 400. Mapping it to 400 would change the status every
    client sees for a malformed body.
    """
    return ApiError(422, E_VALIDATION, message)
