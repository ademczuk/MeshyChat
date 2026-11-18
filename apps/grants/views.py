"""HTTP surface for /v1/grants/* and /v1/access-requests/*.

Every path is absolute exactly as written in urls.py.

STATUS CODES, which are the part most easily wrong by one:

    POST   /v1/grants                          201 Created
    GET    /v1/grants/issued|held              200
    PATCH  /v1/grants/{id}                     200
    DELETE /v1/grants/{id}                     200  <- the REVOKED GRANT, not 204
    POST   /v1/access-requests                 201 Created
    GET    /v1/access-requests/sent|received   200
    POST   /v1/access-requests/{id}/approve    200  {"request":…,"grant":…}
    POST   /v1/access-requests/{id}/deny       200  the request
    POST   /v1/access-requests/{id}/withdraw   200  the request

SCOPE GATING IS PER-ROUTE AND IS NOT UNIFORM. Every /v1/grants/* handler calls
`require_scope` with `grants:read` on the two lists and
`grants:write` on issue/update/revoke. NOT ONE of the six access-request handlers calls
it. Adding a scope gate to the access-request routes would reject PAT callers, and
dropping one from the grants routes would let a read-scoped key write.

THE ERROR MAPPING TABLE is `_domain_status` below. It lives in one place instead of
being spread over nine handlers.

FOUR DELIBERATE, BOUNDED DECISIONS ON THE REQUEST SIDE - stated because a silent one
is the worst outcome. They are numbered for THIS docstring only; docs/DIVERGENCES.md's
register numbers (001, 002, ...) are a different sequence, so "request-side divergence
3" is not register entry 003. Number 3 is register entry 042 (numbered at integration,
integ/r9), number 4 is register entry 045; 1 and 2 have no register entry:

  1. A malformed request BODY gets a status of 400 for JSON syntax, 422 for valid JSON
     with the wrong shape, or 415 for a missing content type. The body is the
     `{error,message}` envelope, not a framework plain-text message.
  2. A malformed `{id}` PATH SEGMENT is 400 E_VALIDATION before the handler. Django's
     `<uuid:…>` converter
     would instead NOT MATCH and produce an HTML 404 from the resolver, which is why
     the routes are typed `<str:…>` and parsed here.
  3. (2026-09-25, STATUS C36) `max_executions` of 0 or less on `POST /v1/grants` is a
     400 E_VALIDATION, refused before any database work, because the value would
     otherwise reach the CHECK `skill_grants_max_executions_positive` and surface as a
     500. The message names the field. 2**31 fails with a 422. The check also runs
     before the mandate block, so a non-positive cap beside an unverifiable mandate gets
     this 400 (2026-09-25, review of 4cbbc9e5;
     tests/test_grants.py::test_a_cap_below_one_is_refused_before_the_mandate_is_verified).
  4. (2026-09-25, STATUS C47, operator decision; register entry 045) the four list reads
     take an OPT-IN keyset page, `?limit=N&before=ID`: at most N rows (1 to 500), newest
     first by (timestamp, id), older than the row ID names, which must be a row of the
     same list (else 400 E_VALIDATION; a malformed value is a 400 too, before the scope
     gate as every query field is). The next page is `before=<the last row's id>`.
     Without either parameter the answer is the whole list, byte for byte as before
     paging existed. A malformed `?limit=abc` is a 400.

REQUEST-PARSING ORDER (STATUS C37). The handlers parse the path, query and body before
`require_scope`. So a malformed path, query or body gets its 400/415/422 even from a
key that lacks the scope. Only the owner credential itself is checked earlier. A mandate
is decoded in the handler body after the scope gate, so its shape errors stay behind
`require_scope`.
"""
from __future__ import annotations

import json
import logging

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.crypto.ijson import echo_text

from . import config, db, errors, service, wire
from .owner_auth import GRANTS_READ, GRANTS_WRITE, authenticate_owner
from .repository import Empty, NotFound
from .wire import grant_to_json, request_to_json

log = logging.getLogger("meshychat.grants")


# --------------------------------------------------------------------- plumbing

def _json_response(payload, status: int = 200) -> HttpResponse:
    # Compact JSON omits spaces after separators; the default json.dumps separators would change
    # every byte after the first key.
    return HttpResponse(json.dumps(payload, separators=(",", ":")),
                        content_type="application/json", status=status)


def _error_response(exc: errors.ApiError) -> HttpResponse:
    if exc.status >= 500 and exc.detail:
        # Log the detail and return the constant. The detail must not
        # reach the client.
        log.error("internal error on the grants surface: %s", exc.detail)
    return _json_response(exc.body(), status=exc.status)


#: Domain error -> (status, code, body message).
#: `USE_DISPLAY` means the domain error's own text goes on the wire; the others carry
#: their constant mapped message, so the detail stays internal.
USE_DISPLAY = object()

_DOMAIN_MAP = {
    # --- Grant service errors -------------------------------------------------
    service.GrantNotFound: (404, errors.E_NOT_FOUND, "not found"),
    service.NotGrantor: (403, errors.E_FORBIDDEN, "forbidden"),
    service.AlreadyRevoked: (409, errors.E_CONFLICT, USE_DISPLAY),
    service.DuplicateActive: (409, errors.E_CONFLICT, USE_DISPLAY),
    service.WideningDelegation: (400, errors.E_VALIDATION, USE_DISPLAY),
    service.MandateViolation: (403, errors.E_FORBIDDEN, "forbidden"),
    service.FlowScopeNotAuthorized: (403, errors.E_FORBIDDEN, "forbidden"),
    service.ManifestPinFailed: (500, errors.E_INTERNAL, "internal error"),
    service.ManifestAttestationFailed: (500, errors.E_INTERNAL, "internal error"),
    service.CapabilityMintingUnavailable: (500, errors.E_INTERNAL, "internal error"),
    # --- Access request errors ------------------------------------------------
    service.RequestNotFound: (404, errors.E_NOT_FOUND, "not found"),
    service.NotRequester: (403, errors.E_FORBIDDEN, "forbidden"),
    service.NotGranter: (403, errors.E_FORBIDDEN, "forbidden"),
    service.NotPending: (409, errors.E_CONFLICT, USE_DISPLAY),
    service.DuplicatePending: (409, errors.E_CONFLICT, USE_DISPLAY),
}


def _domain_status(exc: service.DomainError) -> errors.ApiError:
    entry = _DOMAIN_MAP.get(type(exc))
    if entry is None:
        # An unmapped domain error must not become a 200 or a stack trace. This default
        # fails closed and carries the cause to the log.
        return errors.internal(f"unmapped domain error: {type(exc).__name__}: {exc}")
    status, code, message = entry
    text = str(exc) if message is USE_DISPLAY else message
    return errors.ApiError(status, code, text,
                           detail=str(exc) if status >= 500 else None)


def _route(*allowed):
    """One connection per request, the route's methods answered first, and one place where
    every error becomes a response. Used as `@_route("GET")`.

    The method is answered BEFORE the connection opens (#129), as `apps/a2a/views.py` does
    (STATUS C73): the 405 still precedes the credential (401) and every parse, a wrong
    method costs no connect, and it answers 405 even when the database is unreachable."""
    def decorate(handler):
        def wrapper(request, *args, **kwargs):
            if request.method not in allowed:
                # A path that exists under another method answers 405 with Allow;
                # Django would otherwise fall through to a 404.
                return HttpResponse(status=405, headers={"Allow": ", ".join(allowed)})
            try:
                with db.connection() as conn:
                    return handler(request, conn, *args, **kwargs)
            except errors.ApiError as exc:
                return _error_response(exc)
            except service.DomainError as exc:
                return _error_response(_domain_status(exc))
        wrapper.__name__ = getattr(handler, "__name__", "route")
        # Django's CSRF middleware is not in this project's MIDDLEWARE, but the decorator
        # is kept so mounting it later cannot start rejecting these calls: the CSRF control
        # on this surface is the cookie-path check in owner_auth.
        return csrf_exempt(wrapper)
    return decorate


def _body(request) -> dict:
    """Return 415 without the content type, 400 for a syntax error, or 422 for a type
    error. A decision note requires a body; send `{}` for an empty case."""
    ctype = (request.META.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise errors.ApiError(415, errors.E_VALIDATION,
                              "expected request with `Content-Type: application/json`")
    if not request.body:
        raise errors.json_syntax_error("EOF while parsing a value")
    try:
        parsed = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.json_syntax_error(str(exc))
    if not isinstance(parsed, dict):
        raise errors.json_data_error("invalid type: expected a JSON object")
    return parsed


def _uuid_path(raw, what: str):
    """Parse one of the four accepted UUID forms (wire.parse_uuid_str), else 400."""
    try:
        return wire.parse_uuid_str(raw)
    except ValueError:
        raise errors.validation(f"invalid {what}: {raw}")


def _uuid_field(body, name: str):
    """A UUID body field is a JSON string in one of four accepted forms
    (wire.parse_uuid_str), else 422."""
    value = body.get(name)
    if value is None:
        raise errors.json_data_error(f"missing field `{name}`")
    try:
        return wire.parse_uuid_str(value)
    except ValueError:
        raise errors.json_data_error(f"invalid value: `{name}` is not a UUID")


#: The accepted signed 32-bit range for `max_executions`.
_I32_MIN, _I32_MAX = -(2 ** 31), 2 ** 31 - 1


def _i32_field(body, name: str):
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise errors.json_data_error(f"invalid type: `{name}` must be an integer")
    if not _I32_MIN <= value <= _I32_MAX:
        raise errors.json_data_error(
            f"invalid value: `{name}` = {value} does not fit in a 32-bit integer")
    return value


def _str_field(body, name: str, *, required: bool):
    value = body.get(name)
    if value is None:
        if required:
            raise errors.json_data_error(f"missing field `{name}`")
        return None
    if not isinstance(value, str):
        raise errors.json_data_error(f"invalid type: `{name}` must be a string")
    return value


def _ts_field(body, name: str):
    value = body.get(name)
    if value is None:
        return None
    try:
        return wire.parse_rfc3339(value)
    except ValueError as exc:
        raise errors.json_data_error(f"invalid value for `{name}`: {exc}")


def _bool_query(request, name: str) -> bool:
    """Parse `?include_revoked=true|false`; anything else is a validation failure, and
    an absent parameter is false. Treating `?include_revoked` or `=1` as true would show
    revoked grants to a client that should receive a 400.
    """
    raw = request.GET.get(name)
    if raw is None:
        return False
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise errors.validation(
        f"Failed to deserialize query string: {name}: provided string was not "
        "`true` or `false`")


def _status_query(request):
    """Parse a lowercase status; an unknown value is a 400, never a silently-ignored
    filter that would return every status."""
    raw = request.GET.get("status")
    if raw is None:
        return None
    if raw not in service.VALID_STATUSES:
        raise errors.validation(
            f"Failed to deserialize query string: status: unknown variant `{echo_text(raw)}`, "
            f"expected one of {', '.join('`%s`' % s for s in service.VALID_STATUSES)}")
    return raw


def _page_query(request):
    """(limit, before) of the opt-in keyset page (request-side divergence 4), each None
    when absent. `limit` is a decimal integer from 1 to MAX_PAGE_LIMIT, `before` a UUID in
    one of four accepted UUID forms; anything else is a 400, like the other query
    fields, and parsed before the scope gate (C37). Leading zeros are dropped and the rest
    is length-checked BEFORE `int()`: CPython refuses a decimal string over 4300 digits
    with a ValueError, which was a 500 here, and no in-range value is longer than the cap."""
    limit = request.GET.get("limit")
    if limit is not None:
        cap = service.repository.MAX_PAGE_LIMIT
        value = limit.lstrip("0")
        if (not (limit.isascii() and limit.isdigit()) or len(value) > len(str(cap))
                or not 1 <= int(value or "0") <= cap):
            raise errors.validation(
                f"Failed to deserialize query string: limit: expected an integer from 1 "
                f"to {cap}")
        limit = int(value)
    before = request.GET.get("before")
    if before is not None:
        try:
            before = wire.parse_uuid_str(before)
        except ValueError:
            raise errors.validation(
                "Failed to deserialize query string: before: UUID parsing failed")
    return limit, before


def _rows(outcome) -> list:
    """Found(list) | Empty(scope) -> the JSON array. The Empty case is logged with the
    scope it queried, so an empty list in a response can be traced to a real read. A
    NotFound is a page's `before` that names no row of the list: a 400."""
    if isinstance(outcome, NotFound):
        raise errors.validation("before: no such row in this list")
    if isinstance(outcome, Empty):
        log.debug("no rows for %s", outcome.scope)
        return []
    return outcome.value


# ----------------------------------------------------------------------- grants

@_route("POST")
def grants_collection(request, conn):
    """POST /v1/grants -> 201."""
    auth = authenticate_owner(conn, request)
    # Every typed request field is parsed here,
    # BEFORE the scope gate and before the mandate touches the database (C37).
    body = _body(request)
    grantee_owner_id = _uuid_field(body, "grantee_owner_id")
    granter_peer_id = _uuid_field(body, "granter_peer_id")
    skill_name = _str_field(body, "skill_name", required=True)
    version_pin = _str_field(body, "version_pin", required=False)
    source = body.get("source")
    if source is None:
        raise errors.json_data_error("missing field `source`")
    if source not in service.VALID_SOURCES:
        raise errors.json_data_error(
            f"unknown variant `{echo_text(source)}`, expected one of "
            f"{', '.join('`%s`' % s for s in service.VALID_SOURCES)}")
    source_ref = _str_field(body, "source_ref", required=False)
    expires_at = _ts_field(body, "expires_at")
    max_executions = _i32_field(body, "max_executions")

    auth.require_scope(GRANTS_WRITE)

    # Request-side divergence 3 in the module docstring (C36): refused here, not by
    # the DB CHECK, and before the mandate is verified.
    if max_executions is not None and max_executions <= 0:
        raise errors.validation(
            f"max_executions must be a positive integer (got {max_executions})")

    mandate_obj = None
    mandate_sig = None
    raw_mandate = body.get("mandate")
    if raw_mandate is not None:
        # Every malformed shape here is a 400, before any DB work.
        try:
            mandate_obj, mandate_sig = service.decode_mandate(raw_mandate)
        except ValueError as exc:
            raise errors.validation(str(exc))
        try:
            service.verify_mandate_against_owner(conn, mandate_obj, mandate_sig,
                                                 auth.owner_id)
        except PermissionError as exc:
            # An unverifiable mandate is 403 and fails closed.
            log.info("mandate rejected for owner %s: %s", auth.owner_id, exc)
            raise errors.forbidden()
    elif config.mandate_require_at_issuance():
        # The deploy-dark gate is default OFF.
        raise errors.validation(
            "grant issuance requires an owner-signed IntentMandate "
            "(MESHYCHAT_MANDATE_REQUIRE_AT_ISSUANCE)")

    grant = service.issue_grant(
        conn,
        grantor_owner_id=auth.owner_id,          # the grantor is the AUTHENTICATED owner
        grantee_owner_id=grantee_owner_id,
        granter_peer_id=granter_peer_id,
        skill_name=skill_name,
        version_pin=version_pin,
        source=source,
        source_ref=source_ref,
        expires_at=expires_at,
        scopes=body.get("scopes"),
        max_executions=max_executions,
        mandate=mandate_obj, mandate_sig=mandate_sig)
    # The response flattens the grant and omits biscuit_token when it is None. It is
    # always None here, so the body is the bare grant.
    return _json_response(grant_to_json(grant), status=201)


@_route("GET")
def list_issued(request, conn):
    """GET /v1/grants/issued."""
    auth = authenticate_owner(conn, request)
    include_revoked = _bool_query(request, "include_revoked")   # Parse query first.
    limit, before = _page_query(request)
    auth.require_scope(GRANTS_READ)
    rows = _rows(service.list_issued(conn, auth.owner_id, include_revoked,
                                     limit=limit, before=before))
    return _json_response([grant_to_json(g) for g in rows])


@_route("GET")
def list_held(request, conn):
    """GET /v1/grants/held."""
    auth = authenticate_owner(conn, request)
    include_revoked = _bool_query(request, "include_revoked")   # Parse query first.
    limit, before = _page_query(request)
    auth.require_scope(GRANTS_READ)
    rows = _rows(service.list_held(conn, auth.owner_id, include_revoked,
                                   limit=limit, before=before))
    return _json_response([grant_to_json(g) for g in rows])


@_route("PATCH", "DELETE")
def grant_item(request, conn, grant_id):
    """PATCH + DELETE /v1/grants/{id}."""
    auth = authenticate_owner(conn, request)
    # The path and then the request body are parsed before the handler body, so both are
    # parsed ahead of `require_scope` (C37).
    gid = _uuid_path(grant_id, "grant id")

    if request.method == "DELETE":
        auth.require_scope(GRANTS_WRITE)
        # `reason` is a query parameter, not a body field.
        grant = service.revoke_grant(conn, gid, auth.owner_id, request.GET.get("reason"))
        return _json_response(grant_to_json(grant))

    body = _body(request)
    # Absent means leave unchanged; `null` means clear. The two are different, so
    # presence is tested with `in`, never with a None check.
    expires_present = "expires_at" in body
    pin_present = "version_pin" in body
    new_expires = _ts_field(body, "expires_at") if expires_present else None
    new_pin = _str_field(body, "version_pin", required=False) if pin_present else None
    auth.require_scope(GRANTS_WRITE)
    if not expires_present and not pin_present:
        raise errors.validation("provide at least one of expires_at or version_pin")
    grant = service.update_grant(conn, gid, auth.owner_id,
                                 expires_at_present=expires_present, new_expires_at=new_expires,
                                 version_pin_present=pin_present, new_version_pin=new_pin)
    return _json_response(grant_to_json(grant))


# -------------------------------------------------------------- access requests

@_route("POST")
def access_requests_collection(request, conn):
    """POST /v1/access-requests -> 201. No scope gate."""
    auth = authenticate_owner(conn, request)
    body = _body(request)
    req = service.submit_request(
        conn,
        requester_owner_id=auth.owner_id,        # the requester is the AUTHENTICATED owner
        granter_owner_id=_uuid_field(body, "granter_owner_id"),
        granter_peer_id=_uuid_field(body, "granter_peer_id"),
        skill_name=_str_field(body, "skill_name", required=True),
        message=_str_field(body, "message", required=False),
        expires_at=_ts_field(body, "expires_at"))
    return _json_response(request_to_json(req), status=201)


@_route("GET")
def list_sent(request, conn):
    """GET /v1/access-requests/sent."""
    auth = authenticate_owner(conn, request)
    status = _status_query(request)
    limit, before = _page_query(request)
    rows = _rows(service.repository.list_requests_sent(conn, auth.owner_id, status,
                                                       limit=limit, before=before))
    return _json_response([request_to_json(r) for r in rows])


@_route("GET")
def list_received(request, conn):
    """GET /v1/access-requests/received."""
    auth = authenticate_owner(conn, request)
    status = _status_query(request)
    limit, before = _page_query(request)
    rows = _rows(service.repository.list_requests_received(conn, auth.owner_id, status,
                                                           limit=limit, before=before))
    return _json_response([request_to_json(r) for r in rows])


@_route("POST")
def approve(request, conn, request_id):
    """POST /v1/access-requests/{id}/approve.

    Body is REQUIRED: send `{}` for an empty note. A handler that tolerated an absent
    body would accept calls that are rejected today.
    """
    auth = authenticate_owner(conn, request)
    rid = _uuid_path(request_id, "access request id")
    note = _str_field(_body(request), "note", required=False)
    req, grant = service.approve_request(conn, rid, auth.owner_id, note)
    # Return both values in this order.
    return _json_response({"request": request_to_json(req), "grant": grant_to_json(grant)})


@_route("POST")
def deny(request, conn, request_id):
    """POST /v1/access-requests/{id}/deny."""
    auth = authenticate_owner(conn, request)
    rid = _uuid_path(request_id, "access request id")
    note = _str_field(_body(request), "note", required=False)
    return _json_response(request_to_json(service.deny_request(conn, rid, auth.owner_id, note)))


@_route("POST")
def withdraw(request, conn, request_id):
    """POST /v1/access-requests/{id}/withdraw.

    Takes no body and never parses one, so a request with no content type is accepted
    here where approve/deny answer 415.
    """
    auth = authenticate_owner(conn, request)
    rid = _uuid_path(request_id, "access request id")
    return _json_response(request_to_json(service.withdraw_request(conn, rid, auth.owner_id)))


__all__ = ["grants_collection", "list_issued", "list_held", "grant_item",
           "access_requests_collection", "list_sent", "list_received",
           "approve", "deny", "withdraw"]
