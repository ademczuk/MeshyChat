"""The dashboard error surface.

Body shape is `{"error": <CODE>, "message": <text>}`. An ordinary dashboard error carries
exactly those two keys.

The (status, code) PAIRING is what a client branches on. Three pairs in this file are easy
to get subtly wrong:

  E_SESSION_EXPIRED  401  a PRESENT-but-invalid `meshy_session` cookie. It does NOT fall
                          through to the Bearer schemes: a
                          stale cookie must not be able to escalate to another
                          credential class.
  E_API_KEY_INVALID  401  a `meshypat_`-prefixed Bearer token that resolves to no live
                          key. Also terminal - it never
                          falls through to the JWT path.
  E_FORBIDDEN        403  a dev-JWT whose `aud` is not exactly "dashboard".
                          NOT 401: the token verified, it
                          is simply for another audience.

`ApiError` is IMPORTED from `apps.peers.errors`, not redefined, as `apps/files`,
`apps/skillsreg` and `apps/meshguard` already do: `apps/peers/views.py`'s owner routes and
`apps/authapi` catch this surface's extractor errors beside their own, and one class means
one `except` there. The shared plain variants come with it; everything this surface adds
stays below.
"""
from __future__ import annotations

# One class, imported rather than re-declared - see the module docstring. The plain
# variants come with it.
from apps.peers.errors import (  # noqa: F401
    ApiError,
    auth_invalid,
    auth_missing,
    conflict,
    forbidden,
    not_found,
    validation,
)

# Error-code strings.
E_NOT_FOUND = "E_NOT_FOUND"
E_FORBIDDEN = "E_FORBIDDEN"
E_ADMIN_ONLY = "E_ADMIN_ONLY"
E_INTERNAL = "E_INTERNAL"
E_VALIDATION = "E_VALIDATION"
E_CONFLICT = "E_CONFLICT"
E_AUTH_MISSING = "E_AUTH_MISSING"
E_AUTH_INVALID = "E_AUTH_INVALID"
E_SESSION_EXPIRED = "E_SESSION_EXPIRED"
E_API_KEY_INVALID = "E_API_KEY_INVALID"
E_SCOPE_REQUIRED = "E_SCOPE_REQUIRED"
E_CSRF_REQUIRED = "E_CSRF_REQUIRED"
E_QUOTA_EXCEEDED = "E_QUOTA_EXCEEDED"


def admin_only() -> ApiError:
    return ApiError(403, E_ADMIN_ONLY, "admin only")


def session_expired() -> ApiError:
    """Cookie present, no active row."""
    return ApiError(401, E_SESSION_EXPIRED, "session not active")


def api_key_invalid() -> ApiError:
    """API key resolves to no live key."""
    return ApiError(401, E_API_KEY_INVALID, "invalid, expired, or revoked API key")


def scope_required(scope: str) -> ApiError:
    """API key is missing a required scope."""
    return ApiError(403, E_SCOPE_REQUIRED,
                    f"API key is missing the required scope: {scope}")


def csrf_required() -> ApiError:
    """CSRF validation failed."""
    return ApiError(403, E_CSRF_REQUIRED, "missing or invalid CSRF token")


# --- Tier errors mapped to API errors ------------------------------------------
# Two mappings are easy to get wrong:
#   * ProductAccessDenied and EntitlementsStale are 402 with the code E_QUOTA_EXCEEDED,
#     NOT a bespoke code and NOT 401. A 401 would be wrong because it would log the user
#     out of a session that is perfectly valid.
#   * UnknownCapability and InvalidTier are 400 E_VALIDATION, not 500. A capability the
#     seeded set does not recognise is a bad request, not a server fault.
# `message` is the tier error text.

def product_access_denied() -> ApiError:
    return ApiError(402, E_QUOTA_EXCEEDED, "product access denied by Platform Accounts")


def entitlements_stale() -> ApiError:
    return ApiError(402, E_QUOTA_EXCEEDED,
                    "accounts entitlements are stale; re-authenticate to refresh "
                    "product access")


class UnknownCapability(ApiError):
    """Unknown capability as a distinct CLASS rather than only a code.

    The log settings path matches on that ONE variant and degrades to the default
    retention, propagating every other tier error. A caller that instead matched on
    (400, E_VALIDATION) would also swallow `InvalidTier` - and, once the entitlements
    gate moved into `resolve_cap`, any other error that shares
    a status. The degradation must name the variant it is degrading.
    """


def unknown_capability(capability: str) -> ApiError:
    return UnknownCapability(400, E_VALIDATION, f"unknown capability: {capability}")


def invalid_tier(tier: str) -> ApiError:
    return ApiError(400, E_VALIDATION, f"invalid tier in db: {tier}")


def internal(message: str) -> ApiError:
    """Logs the detail and returns the CONSTANT "internal error".

    Carrying `message` onto the wire would disclose internal detail: the dashboard's
    internal errors are SQL strings that name tables and columns.
    """
    err = ApiError(500, E_INTERNAL, "internal error")
    err.detail = message
    return err
