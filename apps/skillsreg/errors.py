"""The error surface for the skills registry.

THE CLASS IS SHARED WITH `apps/peers`, DELIBERATELY. This app authenticates through
`apps.peers.auth.authenticate_peer`, which raises `apps.peers.errors.ApiError`. Defining a
second, unrelated exception class here would mean a scope rejection escaped this app's
view wrapper and surfaced as a Django 500. The envelope class is imported rather than
re-declared, and only the skills-specific CONSTRUCTORS live here.

The (status, code) pairing is the part a client branches on.
"""
from __future__ import annotations

from apps.peers.errors import (E_AUTH_INVALID, E_AUTH_MISSING, E_CONFLICT,  # noqa: F401
                               E_FORBIDDEN, E_INTERNAL, E_NOT_FOUND, E_VALIDATION,
                               ApiError, auth_invalid, auth_missing, conflict, forbidden,
                               internal, not_found, validation)

#: The code `require_scope` raises when a token lacks a scope (apps/peers/auth.py).
#: Listed here because this app is the heaviest user of it: every write route on the
#: registry is scope-gated and `skills:publish` is a SECOND authority on top of
#: `skills:declare`.
E_SCOPE_REQUIRED = "E_SCOPE_REQUIRED"


def scope_required(scope: str) -> ApiError:
    """403 with the scope NAME in the message.

    Named here so a caller in this app never writes the literal by hand; the message
    text is part of the wire contract.
    """
    return ApiError(403, E_SCOPE_REQUIRED,
                    f"peer token is missing the required scope: {scope}")


def unprocessable(message: str) -> ApiError:
    """422, for a well-formed JSON body that does not fit the request shape.

    A syntactically valid body whose SHAPE is wrong (missing required field, wrong type,
    unknown enum variant) receives 422 Unprocessable Entity, and a body that is not JSON
    at all receives 400. Every error out of this app is the JSON `{error, message}` envelope.

    `apps/authapi/views.py:55-59` uses the same envelope, so the two apps agree about the
    shape of a malformed request.
    """
    return ApiError(422, E_VALIDATION, message)


def bad_json(message: str) -> ApiError:
    """400 - a body that is not JSON at all."""
    return ApiError(400, E_VALIDATION, message)
