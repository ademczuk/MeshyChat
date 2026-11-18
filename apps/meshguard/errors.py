"""Error surface for the meshguard routes.

The plain variants are exposed from `apps/peers/errors.py` rather than re-declared,
because the wire contract `{"error": <code>, "message": <text>}` and the (status, code)
pairing need one definition. The cross-app import is the established house pattern
(`apps/calls/wire.py`, `apps/files/errors.py`, `apps/skillsreg/errors.py` all do it).

Only what this surface adds lives here:

  * `coded`     - the generic "domain code + status" constructor. The meshguard backlog
                  route is the only user: `E_BACKLOG_TOO_LARGE` at 413. `code` is never
                  interpolated from request data; that property is preserved by only
                  ever passing the module constants below.

A repository `NotFound` (every lookup in this app returns a TYPED absence instead of
None) becomes a 404 at the route that read it, through `not_found`.
"""
from __future__ import annotations

from apps.peers.errors import (  # noqa: F401  (part of the public API)
    E_AUTH_INVALID,
    E_AUTH_MISSING,
    E_CONFLICT,
    E_FORBIDDEN,
    E_INTERNAL,
    E_NOT_FOUND,
    E_VALIDATION,
    ApiError,
    auth_invalid,
    auth_missing,
    conflict,
    forbidden,
    internal,
    not_found,
    validation,
)

#: A module constant cannot carry request data into the `error` field.
E_BACKLOG_TOO_LARGE = "E_BACKLOG_TOO_LARGE"


def coded(code: str, status: int, message: str) -> ApiError:
    """Construct an error with a domain code and status."""
    return ApiError(status, code, message)


def backlog_too_large(message: str) -> ApiError:
    """413 PAYLOAD_TOO_LARGE, not 400."""
    return coded(E_BACKLOG_TOO_LARGE, 413, message)
