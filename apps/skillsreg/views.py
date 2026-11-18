"""HTTP surface for the skills registry.

STATUS CODES, which are the part most easily wrong by one:
    GET    /v1/peers/me/skills                   -> 200 [Skill]
    POST   /v1/peers/me/skills                   -> 201 Skill
    POST   /v1/peers/me/skills/{name}/versions   -> 201 Skill
    PATCH  /v1/peers/me/skills/{name}            -> 200 Skill
    DELETE /v1/peers/me/skills/{name}            -> 204 (no body)
    GET    /v1/skills/{peer_id}/{name}           -> 200 Skill / 404

TWO AUTHORITIES ON EVERY WRITE. `skills:declare` is checked first
and unconditionally; `skills:publish` is an ADDITIONAL authority required only when the
share_mode PUBLISHES (public, marketplace_free, marketplace_paid). The declare check
stays first so a peer that may not declare at all is never told its problem is the
publish scope - that would leak the shape of what it is missing. Publish alone is not
enough. DELETE needs only `skills:declare`: removing a skill from discovery is not
publishing, and requiring publish authority would strand a peer whose publish grant
lapsed with a public skill it could not withdraw.

DESIGN NOTES, stated plainly because a silent behavior difference is worse than a loud one:

  1. BODY SHAPE OF A MALFORMED REQUEST. A syntactically bad body receives 400 and a
     well-formed body of the wrong SHAPE receives 422. The bodies are this app's JSON
     `{error, message}` envelope. `apps/authapi/views.py:55-59` already made exactly this
     choice, so the two apps agree with each other.
  2. `POST /v1/peers/me/skills/sync` IS NOT IMPLEMENTED. The PATH is still registered,
     before the `{name}` capture: `PATCH /v1/peers/me/skills/sync` is a 405, NOT an
     update of a skill named "sync". Dropping the route would make that path reach
     `update`, allowing a peer to mutate a skill through the sync URL. Non-POST methods
     get 405; an AUTHENTICATED POST holding `skills:declare` gets 501 and says so. Being
     unimplemented is not a reason to skip authentication: the route
     runs the same `_route` wrapper and the same scope gate as every other write here,
     so an anonymous caller is refused 401 rather than told the path exists.
  3. LESSON FAN-OUT after a successful lesson-mode write is not performed. The Redis
     publish belongs to the wsgw surface,
     which another agent owns; the WRITE and its validation are complete, only the
     broadcast is missing. A subscriber therefore learns about a new lesson on its next
     poll rather than immediately.
  4. `GET /v1/skills`, `/v1/peers/{peer_id}/skills`, and
     `/v1/skills/{peer_id}/{name}/versions[/{version}]` are not implemented here. Only
     the single "fetch one" route is.
  5. HMAC request signing on writes is not enforced: no handler or middleware in this
     app checks it.
"""
from __future__ import annotations

import json
import uuid

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.peers.auth import authenticate_peer
from apps.peers.timefmt import format_rfc3339
from apps.skillsreg import attestation, db, errors, repository, service
from apps.skillsreg import runtime as rt
from apps.skillsreg.errors import ApiError

SKILLS_DECLARE_SCOPE = "skills:declare"
SKILLS_PUBLISH_SCOPE = "skills:publish"

#: Canonical TEXT representations, which are also the
#: values the `skills_mode_check` / `skills_share_mode_check` CHECK constraints allow.
SKILL_MODES = ("sync", "async", "stream", "lesson")
SHARE_MODES = ("private", "grants_only", "marketplace_free", "marketplace_paid", "public")
#: Modes that publish a skill beyond its owner. The two sets are asserted disjoint and
#: exhaustive in the test suite, so a new share mode cannot silently inherit an answer.
PUBLICATION_SHARE_MODES = ("public", "marketplace_free", "marketplace_paid")
SELF_DESCRIPTION_SHARE_MODES = ("private", "grants_only")


# ----------------------------------------------------------------- plumbing

def _error_response(exc: ApiError) -> HttpResponse:
    """Through `_json` below, so an error body is COMPACT exactly as every success body is.

    `JsonResponse` defaults to `", "` / `": "`, so an independently encoded error body would
    disagree byte for byte with the success body. `apps/core/jsonerrors.json_500` does this for the
    unhandled case, and `apps/dashboard/views.py` writes its `_error_response` this way.
    """
    return _json(exc.body(), status=exc.status)


def _route(handler):
    """One `meshychat_app` autocommit connection per request, ApiError -> response.

    Every route gets its OWN connection, so no request can inherit another's
    transaction-local state. `apps/core/dbconn.py` explains why autocommit is not a
    tuning choice; `db.py` explains why `release_version` additionally depends on it.
    """
    def wrapper(request, *args, **kwargs):
        try:
            with db.connection() as conn:
                return handler(request, conn, *args, **kwargs)
        except ApiError as exc:
            return _error_response(exc)
    wrapper.__name__ = getattr(handler, "__name__", "route")
    return csrf_exempt(wrapper)


def _method(request, *allowed):
    if request.method not in allowed:
        return HttpResponse(status=405, headers={"Allow": ", ".join(allowed)})
    return None


def _json_object(request) -> dict:
    """400 for "not JSON", 422 for "JSON but not an object"."""
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.bad_json(f"malformed JSON body: {exc}")
    if not isinstance(body, dict):
        raise errors.unprocessable("invalid type: expected a JSON object")
    return body


def require_share_authority(auth, share_mode: str) -> None:
    """Declare is checked first and unconditionally; publish is an ADDITIONAL authority,
    never a substitute, so a token holding only `skills:publish` is still refused.
    """
    if SKILLS_DECLARE_SCOPE not in auth.scopes:
        raise errors.scope_required(SKILLS_DECLARE_SCOPE)
    if share_mode in PUBLICATION_SHARE_MODES and SKILLS_PUBLISH_SCOPE not in auth.scopes:
        raise errors.scope_required(SKILLS_PUBLISH_SCOPE)


# --------------------------------------------------- request-body deserialization
# Request body rules:
#   * a missing required field is a 422, not a defaulted value;
#   * an optional field treats explicit JSON `null` as None, so `{"examples": null}` and
#     an absent `examples` are the same;
#   * any JSON value, including null, is accepted, so `input_schema: null` is a legal body
#     that the schema gate then soft-warns about - it is not a 422.

_MISSING = object()


def _field(body, name, *, required=False):
    if name not in body:
        if required:
            raise errors.unprocessable(f"missing field `{name}`")
        return _MISSING
    return body[name]


def _string(body, name, *, required=False):
    """A required string field, or an optional string field when `required` is false.

    Absent and explicit null are both None for the optional form, while null for a
    required field is a type error.
    """
    raw = _field(body, name, required=required)
    if not required and (raw is _MISSING or raw is None):
        return None
    if not isinstance(raw, str):
        raise errors.unprocessable(f"invalid type for `{name}`: expected a string")
    return raw


def _enum(body, name, allowed, *, required=True):
    raw = _field(body, name, required=required)
    if raw is _MISSING or raw is None:
        return None
    if not isinstance(raw, str) or raw not in allowed:
        raise errors.unprocessable(
            f"unknown variant for `{name}`, expected one of {', '.join(allowed)}")
    return raw


def _bool(body, name, default=False):
    """A boolean with default False; `default=None` preserves an omitted update field."""
    raw = _field(body, name)
    if raw is _MISSING or raw is None:
        return default
    if not isinstance(raw, bool):
        raise errors.unprocessable(f"invalid type for `{name}`: expected a boolean")
    return raw


def _opt_i32(body, name):
    raw = _field(body, name)
    if raw is _MISSING or raw is None:
        return None
    # `isinstance(True, int)` is True in Python; the integer field rejects a
    # JSON boolean, so the bool case is excluded explicitly rather than by accident.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise errors.unprocessable(f"invalid type for `{name}`: expected an i32")
    if not (-2_147_483_648 <= raw <= 2_147_483_647):
        raise errors.unprocessable(f"`{name}` is out of range for i32")
    return raw


def _tags(body):
    raw = _field(body, "tags")
    if raw is _MISSING or raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(i, str) for i in raw):
        raise errors.unprocessable("invalid type for `tags`: expected an array of strings")
    return list(raw)


def _value(body, name, *, required=False):
    """A field that accepts any JSON value, including null. When not required, absent and
    explicit null are both None."""
    raw = _field(body, name, required=required)
    return None if raw is _MISSING else raw


def _runtime_fields(body):
    try:
        profile = rt.parse_runtime_profile(_value(body, "runtime_profile"))
        status = rt.parse_runtime_status(_value(body, "runtime_status"))
    except rt.RuntimeShapeError as exc:
        # An unknown key, wrong type, or unknown enum value is a SHAPE failure and
        # receives 422, not the 400 the runtime VALIDATOR produces.
        raise errors.unprocessable(str(exc))
    return profile, status


def _attestation(body):
    try:
        return attestation.decode_pair(_string(body, "manifest_sig"),
                                       _string(body, "signing_kid"))
    except ValueError as exc:
        # Decode failures receive 400 E_VALIDATION.
        raise errors.validation(str(exc))


def _declare_request(body) -> dict:
    """Parse a skill declaration request."""
    profile, status = _runtime_fields(body)
    return {
        "name": _string(body, "name", required=True),
        "version": _string(body, "version", required=True),
        "display_name": _string(body, "display_name"),
        "description": _string(body, "description"),
        "mode": _enum(body, "mode", SKILL_MODES),
        "share_mode": _enum(body, "share_mode", SHARE_MODES),
        "input_schema": _value(body, "input_schema", required=True),
        "output_schema": _value(body, "output_schema"),
        "attachments_allowed": _bool(body, "attachments_allowed"),
        "returns_attachments": _bool(body, "returns_attachments"),
        "requires_human_auth": _bool(body, "requires_human_auth"),
        "max_runtime_ms": _opt_i32(body, "max_runtime_ms"),
        "price_cents": _opt_i32(body, "price_cents"),
        "currency": _string(body, "currency"),
        "tags": _tags(body),
        "examples": _value(body, "examples"),
        "runtime_profile": profile,
        "runtime_status": status,
        "attestation": _attestation(body),
    }


def _release_request(body) -> dict:
    """Parse a version release request, identical minus `name`, which is
    the path segment."""
    req = dict(_declare_request({**body, "name": body.get("name", "")}))
    req.pop("name")
    return req


def _update_request(body) -> dict:
    """Parse a metadata update request.

    Full-replace semantics on every field make `requires_human_auth` different from the
    other two booleans: absent means LEAVE IT (`COALESCE(%s, requires_human_auth)`), while
    an absent `attachments_allowed` genuinely sets it false.
    """
    profile, status = _runtime_fields(body)
    return {
        "share_mode": _enum(body, "share_mode", SHARE_MODES),
        "display_name": _string(body, "display_name"),
        "description": _string(body, "description"),
        "tags": _tags(body),
        "max_runtime_ms": _opt_i32(body, "max_runtime_ms"),
        "price_cents": _opt_i32(body, "price_cents"),
        "currency": _string(body, "currency"),
        "attachments_allowed": _bool(body, "attachments_allowed"),
        "returns_attachments": _bool(body, "returns_attachments"),
        "requires_human_auth": _bool(body, "requires_human_auth", default=None),
        "examples": _value(body, "examples"),
        "runtime_profile": profile,
        "runtime_status": status,
        "attestation": _attestation(body),
    }


# ------------------------------------------------------------------ serialization

def to_wire(skill: dict) -> dict:
    """Serialize a Skill for the wire.

    Field ORDER is the order of the dict built below, and it is part of the wire.
    `runtime_profile` and `runtime_status` are ABSENT rather than null when unset; every
    other field is always present.

    `id` is a bare UUID string, not an object.

    `manifest_sig` is an ARRAY OF INTEGERS, not base64.
    """
    out = {
        "id": str(skill["id"]),
        "peer_id": str(skill["peer_id"]),
        "name": skill["name"],
        "version": skill["version"],
        "display_name": skill["display_name"],
        "description": skill["description"],
        "mode": skill["mode"],
        "share_mode": skill["share_mode"],
        "input_schema": skill["input_schema"],
        "output_schema": skill["output_schema"],
        "attachments_allowed": skill["attachments_allowed"],
        "returns_attachments": skill["returns_attachments"],
        "requires_human_auth": skill["requires_human_auth"],
        "max_runtime_ms": skill["max_runtime_ms"],
        "price_cents": skill["price_cents"],
        "currency": skill["currency"],
        "tags": list(skill["tags"] or []),
        "examples": skill["examples"],
        "manifest_sig": (None if skill["manifest_sig"] is None
                         else list(skill["manifest_sig"])),
        "signing_kid": skill["signing_kid"],
    }
    if skill.get("runtime_profile") is not None:
        out["runtime_profile"] = skill["runtime_profile"]
    if skill.get("runtime_status") is not None:
        out["runtime_status"] = skill["runtime_status"]
    out["is_current"] = skill["is_current"]
    out["deleted_at"] = (None if skill["deleted_at"] is None
                         else format_rfc3339(skill["deleted_at"]))
    out["created_at"] = format_rfc3339(skill["created_at"])
    out["updated_at"] = format_rfc3339(skill["updated_at"])
    return out


def _json(payload, status: int = 200) -> HttpResponse:
    """Use compact JSON; Django's JsonResponse defaults to `", "`/`": "`
    separators, which would change every byte after the first key."""
    return HttpResponse(json.dumps(payload, separators=(",", ":")),
                        content_type="application/json", status=status)


# ------------------------------------------------------------------------ routes

@_route
def my_skills(request, conn):
    """GET (list) + POST (declare) /v1/peers/me/skills."""
    bad = _method(request, "GET", "POST")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)

    if request.method == "GET":
        # List non-deleted rows, then keep only the
        # CURRENT versions. No scope gate on the read.
        rows = repository.list_for_peer(conn, auth.peer_id, include_deleted=False)
        if rows is repository.EMPTY:
            # The repository said EMPTY - an explicit "the query ran and matched
            # nothing", not a swallowed error. The route's answer to that is `[]`,
            # which is the response for a peer with no skills.
            return _json([])
        return _json([to_wire(s) for s in rows if s["is_current"]])

    body = _json_object(request)
    req = _declare_request(body)
    require_share_authority(auth, req["share_mode"])
    skill = service.create_skill(conn, auth.peer_id, req)
    return _json(to_wire(skill), status=201)


@_route
def release_version(request, conn, name):
    """POST /v1/peers/me/skills/{name}/versions."""
    bad = _method(request, "POST")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    req = _release_request(_json_object(request))
    require_share_authority(auth, req["share_mode"])
    skill = service.release_version(conn, auth.peer_id, name, req)
    return _json(to_wire(skill), status=201)


@_route
def skill_detail(request, conn, name):
    """PATCH (update) + DELETE (soft-delete) /v1/peers/me/skills/{name}."""
    bad = _method(request, "PATCH", "DELETE")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)

    if request.method == "DELETE":
        # Declare authority only, and 0 rows affected is a 404.
        if SKILLS_DECLARE_SCOPE not in auth.scopes:
            raise errors.scope_required(SKILLS_DECLARE_SCOPE)
        if service.soft_delete(conn, auth.peer_id, name) == 0:
            raise errors.not_found()
        return HttpResponse(status=204)

    req = _update_request(_json_object(request))
    require_share_authority(auth, req["share_mode"])
    skill = service.update_metadata(conn, auth.peer_id, name, req)
    return _json(to_wire(skill))


@_route
def sync_manifest(request, conn):
    """POST /v1/peers/me/skills/sync. NOT IMPLEMENTED; see design note 2.

    This exists so the PATH keeps its precedence over the `{name}` capture (`urls.py`
    declares it first): `PATCH /v1/peers/me/skills/sync` is a 405 and NOT an update of a
    skill named "sync". Without this route registered ahead of `{name}`, that request
    would reach `skill_detail` and mutate a row.

    NOT IMPLEMENTED IS NOT A REASON TO BE UNAUTHENTICATED, and this route used to be
    exactly that: it was declared with a bare `@csrf_exempt` rather than `@_route`, so
    it opened no connection, never called `authenticate_peer`, and told an anonymous
    caller 501. Authentication and the `skills:declare` check run before the handler body,
    so the anonymous POST is a 401 and a token without `skills:declare` is a 403.

    ORDER: the method gate stays ahead of authentication, so a wrong method is rejected
    first. The 501 comes LAST, so what a
    caller learns about this path depends on what it was entitled to learn.
    """
    bad = _method(request, "POST")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    if SKILLS_DECLARE_SCOPE not in auth.scopes:
        raise errors.scope_required(SKILLS_DECLARE_SCOPE)
    # Raised rather than returned so it leaves through `_error_response` and is compact
    # like every other body this app emits.
    raise ApiError(501, errors.E_INTERNAL,
                   "manifest sync is not implemented by this server")


@_route
def get_current(request, conn, peer_id, name):
    """GET /v1/skills/{peer_id}/{name}.

    The hydrate endpoint: the full current Skill, both schema blobs included.

    VISIBILITY: every arm answers 404 rather than 403:
      * the owner always sees its own skill, whatever the share_mode;
      * publicly-browseable modes (public, marketplace_*) are visible to any peer;
      * `private` is never visible to anyone else;
      * `grants_only` is visible only while the viewer's OWNER holds a live grant.
    404 and not 403 is the point - a non-grantee must not learn that a `grants_only` skill
    EXISTS, and a 403 would tell them.
    """
    bad = _method(request, "GET")
    if bad:
        return bad
    auth = authenticate_peer(conn, request)
    try:
        owner_peer = uuid.UUID(str(peer_id))
    except (ValueError, AttributeError, TypeError):
        # A malformed id is a 400, not a 404, to match `apps/peers/views.py:_peer_uuid`;
        # the body is this app's JSON envelope. It is rejected after authentication at
        # the top of this view.
        raise errors.validation(f"invalid peer id: {peer_id}")

    skill = repository.find_current_by_name(conn, owner_peer, name)
    if skill is repository.NOT_FOUND:
        raise errors.not_found()
    _visibility_check(conn, skill, auth.peer_id)
    return _json(to_wire(skill))


def _visibility_check(conn, skill, viewer_peer_id) -> None:
    """Raise 404 when the viewer may not see `skill`."""
    if str(skill["peer_id"]) == str(viewer_peer_id):
        return
    if skill["share_mode"] in PUBLICATION_SHARE_MODES:
        return
    if skill["share_mode"] != "grants_only":
        raise errors.not_found()
    owner_id = repository.peer_owner_id(conn, viewer_peer_id)
    if owner_id is repository.NOT_FOUND:
        # `peer_owner_id` returns NOT_FOUND when the peer row is gone.
        raise errors.not_found()
    if not repository.has_active_grant(conn, owner_id, skill["peer_id"], skill["name"]):
        raise errors.not_found()
