"""Every unhandled exception leaves as the JSON error envelope, never as HTML.

THE DEFECT THIS CLOSES, found independently by four reviewers on four surfaces (grants,
billing, skillsreg, meshguard): each app's `_route` wrapper catches `ApiError` and nothing
else. Anything else - a `psycopg.Error`, a `CheckViolation`, a `UniqueViolation`, a
`KeyError`, a `TypeError` - walks past it into Django's default handler and the client
gets an HTML page where the contract promises `{"error": ..., "message": ...}`.

IT IS NOT HYPOTHETICAL. Reachable on ordinary inputs that were measured, not imagined:

  * `POST /v1/grants` with `max_executions=0` -> the DB CHECK raises `CheckViolation`
  * re-releasing a skill version that exists only on soft-deleted rows -> `UniqueViolation`
  * a duplicate policy-attachment POST -> `UniqueViolation` (`rgpa_unique_binding`)
  * an unknown resolve verdict -> `KeyError` on the status lookup
  * a non-ASCII `X-CSRF-Token` -> `TypeError` from `hmac.compare_digest` on two `str`

and **with `MESHY_DEBUG=1` the rendered page carries the DSN**, plus - in the grants case -
the `CheckViolation` DETAIL line listing every column value of the attempted row.

## Two decisions worth stating, because both could be argued the other way

**It returns the envelope even when DEBUG is on.** A debug page is more useful to a
developer than a log line, but DEBUG is exactly the configuration where the page leaks the
DSN, and "we would never run that in production" is the same sentence behind every
configuration incident. The traceback is not lost: it is logged, in full, server-side. The
wire is the envelope always, so what the client sees does not depend on a setting.

**It does not catch Django's own control-flow exceptions.** `Http404`,
`PermissionDenied` and `SuspiciousOperation` mean something specific to Django and already
have handlers that produce the right status. Swallowing them here would turn a 404 into a
500. `ApiError` is likewise passed through untouched - the per-app wrappers convert it, and
if one ever misses, the app's own handler should be fixed rather than masked here.

A middleware is the right layer for this and a decorator is not: a decorator only covers
the handlers someone remembered to decorate, and `apps/skillsreg/views.py:436` is a route
that was forgotten (it bypasses `@_route` entirely and never authenticates).
"""
from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404, JsonResponse

log = logging.getLogger("meshychat.core.errors")

#: The constant body. It names no table, column, path or setting: an error envelope is a
#: reply to whoever sent the request, including whoever sent it on purpose.
INTERNAL_BODY = {"error": "E_INTERNAL", "message": "internal error"}

#: Exceptions that mean something to Django's own handlers. Passing these through is the
#: difference between a 404 and a 500.
PASS_THROUGH = (Http404, PermissionDenied, SuspiciousOperation)


def envelope(code: str, message: str, *, status: int, headers=None) -> JsonResponse:
    """The error envelope, `{"error": code, "message": message}`, with COMPACT separators.

    `JsonResponse` defaults to `", "` / `": "`. Compact separators keep this error envelope
    byte-for-byte consistent with the project's compact success bodies; adding spaces would
    give errors a different wire form.

    The one constructor behind the core refusals that each hand-built this body: `json_500`
    below, `middleware.DatabaseConnectionMiddleware._unauthenticated` (401),
    `capability_header.refusal` (403) and `metrics._refusal` (503 and 401). Each caller's
    bytes, status and `Content-Type` (`application/json`, JsonResponse's default) are what
    they were. `peer_limit.http_response` and `redeem_limit.admit` are NOT behind it: their
    429 bodies go out with Django's default separators, and routing them here would change
    wire bytes.
    """
    return JsonResponse({"error": code, "message": message}, status=status, headers=headers,
                        json_dumps_params={"separators": (",", ":")})


def json_500() -> JsonResponse:
    """`INTERNAL_BODY` as the envelope: the reply to an unhandled exception."""
    return envelope(INTERNAL_BODY["error"], INTERNAL_BODY["message"], status=500)


class JsonErrorEnvelopeMiddleware:
    """Convert any exception escaping a view into the JSON envelope."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, PASS_THROUGH):
            return None
        if type(exception).__name__ == "ApiError":
            # An app's own error type, which its wrapper is supposed to convert. Let it
            # through rather than flattening a 401 or a 409 into a 500 - a missing
            # conversion is a bug in that app's wrapper and should stay visible there.
            return None
        # The DETAIL is logged in full, with the traceback, and never put on the wire.
        #
        # `exc_info=exception` EXPLICITLY, not `log.exception(...)`. The latter reads the
        # ambient `sys.exc_info()`, which is set only while an `except` block is on the
        # stack; called any other way it logs the literal text "NoneType: None" and the
        # cause is gone. Django does call this from inside an except block today - but a
        # log line that is correct only because of where it happens to be called from is
        # one refactor away from being the thing that hid an incident. The message also
        # carries `repr(exception)` so the cause is in the LINE, not only the traceback.
        log.error("unhandled %s on %s %s: %r", type(exception).__name__,
                  request.method, request.path, exception, exc_info=exception)
        return json_500()


__all__ = ["INTERNAL_BODY", "PASS_THROUGH", "JsonErrorEnvelopeMiddleware", "envelope",
           "json_500"]
