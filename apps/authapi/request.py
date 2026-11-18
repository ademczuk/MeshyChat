"""The request/response scaffolding `views.py` and `onboard.py` share: the body parser, the
field readers, the RFC 3339 form and the rollback carrier.

WHY ITS OWN MODULE: these lived in the two route modules, and each imported the other's -
`onboard` took `_body`/`_rfc3339` from `views` at module scope, so `views` could only take
anything back from `onboard` inside function bodies, or the import would cycle. Here they
import nothing from either, so both import them at module scope.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from django.http import JsonResponse

from apps.authapi import errors


def _rfc3339(value: dt.datetime) -> str:
    """Format RFC 3339 UTC timestamps with `Z` and fractional seconds only when non-zero."""
    value = value.astimezone(dt.timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _body(request):
    """A JSON syntax error is 400; a well-formed non-object body is 422.

    These routes use the coded JSON envelope for both outcomes."""
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, errors.coded(errors.E_VALIDATION, f"malformed JSON body: {exc}", 400)
    if not isinstance(payload, dict):
        # Every request body on this surface is a JSON object; a well-formed array or scalar
        # is a 422. Without this check the field helpers below called `.get` on a list and
        # the request left as a 500.
        return None, errors.coded(errors.E_VALIDATION, "request body must be a JSON object", 422)
    return payload, None


def _uuid_field(payload, name: str):
    raw = payload.get(name)
    if not isinstance(raw, str):
        return None, errors.coded(errors.E_VALIDATION, f"missing field `{name}`", 422)
    try:
        return uuid.UUID(raw), None
    except ValueError:
        return None, errors.coded(errors.E_VALIDATION, f"field `{name}` is not a UUID", 422)


def _str_field(payload, name: str):
    # NOT `_field(..., required=True)`: a wrong type here says "missing field", there
    # "invalid type for field". Merging them would change a message on the wire.
    raw = payload.get(name)
    if not isinstance(raw, str):
        return None, errors.coded(errors.E_VALIDATION, f"missing field `{name}`", 422)
    return raw, None


def _field(payload: dict, name: str, *, required: bool):
    """A missing required field or a wrong type is 422; an absent or null optional one is None.
    Unknown fields are ignored, so an
    `owner_id` in the body is not an error and has no effect."""
    value = payload.get(name)
    if value is None and not required:
        return None, None
    if not isinstance(value, str):
        what = "missing field" if value is None else "invalid type for field"
        return None, errors.coded(errors.E_VALIDATION, f"{what} `{name}`", 422)
    return value, None


class _Refusal(Exception):
    """Carries a finished response out of a transaction block. Raising (rather than
    returning inside the block) is what makes psycopg ROLL BACK: a `return` from inside
    `conn.transaction()` commits."""

    def __init__(self, response: JsonResponse):
        super().__init__(response.status_code)
        self.response = response
