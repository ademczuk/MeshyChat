"""The board over HTTP, so a seat can ASK from anywhere it can reach the server.

Every route is peer-authenticated and READ ONLY. The board is written by declaring acts
through the ordinary mesh write path, which already validates `subject` and `ref` at the
seam; nothing here writes.

WIRE SHAPE, and the one decision in it. A subject with no acts is a distinct case, not an
empty list (`docs/V1-DESIGN-RULES.md` B3, and the whole reason the board exists). On the
wire that is `"state": "no_declared_acts"` versus `"state": "acts"`, both HTTP 200: the
question was answered, and "nothing has been declared" IS the answer. It is not a 404,
because a 404 is what an unmounted route returns too, and a caller could not tell the two
apart. Every response also carries `adoption`, the declared/total pair, so no reader can
take "no acts" as "no work happened".

CONNECTION. Unlike the four sibling apps that open their own `psycopg.connect`, this one
goes through `apps.core.dbconn`, so it is the first app-serving route whose connection
actually crosses `assert_serving_connection` rather than only asserting the role. See B1.

THE ONE WRITE. `POST /v1/board/acts` (2026-09-21) is the door that makes declaring cheap:
a signed `memory.write` envelope carrying a declared act, dispatched to the SAME handler
every other write crosses, with a gate in front that refuses a bare message and names the
missing field. It is the only non-GET here, and the read routes still answer POST with 405.
"""
from __future__ import annotations

import json

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.calls.errors import SignatureFailed
from apps.calls.wire import api_error_from_rpc
from apps.core import dbconn, peer_limit
from apps.core.jsonable import jsonable
from apps.memoryskills.errors import HandlerError
from apps.peers import errors
from apps.peers.auth import authenticate_peer

from . import declare, graph, projection

#: HandlerError code -> HTTP status for the write door. The memory handlers speak the
#: daemon's vocabulary, not HTTP; this is the one place the two meet on the board. An
#: unlisted code is a 400: the request was understood and refused for a reason the body
#: names, which is neither our fault (500) nor "not this shape" (422).
_STATUS_FOR = {
    "E_PERMISSION_DENIED": 403,
    "E_NAMESPACE_UNCLAIMED": 403,
    "E_PRODUCT_NO_SHARED_WRITE": 403,
    "E_BAD_ENVELOPE": 400,
    "E_PROVENANCE_KEY": 400,
    "E_BAD_REQUEST": 422,
    "E_MESH_SCHEMA": 422,
    "E_BAD_KEY_TS": 422,
    "E_KEY_LABEL_MISMATCH": 422,
    # divergence 028: the act names someone other than the bearer, or rewrites their row.
    "E_AUTHOR_MISMATCH": 403,
    "E_FROM_MISMATCH": 403,
    "E_NOT_MESSAGE_AUTHOR": 403,
    "E_USE_MEMORY_EDIT": 403,      # divergence 059: a later version of a mail key is memory.edit's
    "E_WRITE_CONFLICT": 409,
    "E_INTERNAL": 500,
}


#: The one read-model-to-JSON conversion, shared with apps/mesh/views.py.
_jsonable = jsonable


def _route(handler):
    """Guarded connection, peer auth, server-internal context, ApiError mapping.

    Order matters and is the same for every route: authenticate on the base connection
    (peer status is a real query), THEN enter `server_internal`, which is what reading
    `shared_memory` requires and which rls.py says to enter for a specific operation
    rather than for the length of a request.
    """
    def wrapper(request, *args, **kwargs):
        if request.method != "GET":
            return HttpResponse(status=405, headers={"Allow": "GET"})
        try:
            with dbconn.serving_connection() as conn:
                auth = authenticate_peer(conn, request)
                with dbconn.request_server_internal(conn) as c:
                    return handler(request, c, auth, *args, **kwargs)
        except errors.ApiError as exc:
            return JsonResponse(exc.body(), status=exc.status)
    wrapper.__name__ = getattr(handler, "__name__", "board_route")
    return csrf_exempt(wrapper)


def _adoption(conn) -> dict:
    declared, total = projection.adoption(conn)
    return {"declared": declared, "total": total}


@_route
def subjects(request, conn, auth):
    """GET /v1/board/subjects: every subject with at least one act, plus adoption."""
    return JsonResponse({"subjects": projection.subjects_seen(conn),
                         "adoption": _adoption(conn)}, status=200)


@_route
def subject_state(request, conn, auth, subject):
    """GET /v1/board/subjects/<subject>: the state of one unit of work."""
    state = projection.state_for(conn, subject)
    body = {"subject": subject, "adoption": _adoption(conn)}
    if isinstance(state, projection.NoDeclaredActs):
        body["state"] = "no_declared_acts"
    else:
        body["state"] = "acts"
        body["acts"] = _jsonable(state.acts)
        body["latest_by_verb"] = {v: _jsonable(a) for v, a in state.latest_by_verb.items()}
        body["clock_suspect"] = any(a.clock_is_suspect for a in state.acts)
    # Acts on the same NUMBER under a different spelling (`ref/936` when you asked about
    # `pr/936`). A SIBLING FIELD, never merged into `acts`: not showing them is a silent
    # filter and merging them asserts an identity nobody established.
    same = projection.same_number_acts(conn, subject)
    if same:
        body["same_number"] = {k: _jsonable(v) for k, v in same.items()}
    body["prose"] = list(projection.render(state, adoption_pair=(
        body["adoption"]["declared"], body["adoption"]["total"])))
    body["prose"].extend(projection.render_same_number(subject, same))
    return JsonResponse(body, status=200)


@_route
def board_graph(request, conn, auth):
    """GET /v1/board/graph[?actor=<peer_id>][&ref=<ref>][&limit=N]: acts as edges."""
    limit = request.GET.get("limit")
    result = graph.edges(conn, actor_peer_id=request.GET.get("actor") or None,
                         ref=request.GET.get("ref") or None,
                         limit=int(limit) if limit else None)
    return JsonResponse({"edges": _jsonable(result.edges), "truncated": result.truncated,
                         "adjacency": graph.adjacency(result.edges),
                         "adoption": _adoption(conn)}, status=200)


@_route
def my_acts(request, conn, auth):
    """GET /v1/board/me: what the calling peer has itself declared."""
    result = graph.acts_by_actor(conn, auth.peer_id)
    return JsonResponse({"actor_peer_id": str(auth.peer_id), "edges": _jsonable(result.edges),
                         "truncated": result.truncated}, status=200)


# --------------------------------------------------------------------------- #
# The write door
# --------------------------------------------------------------------------- #

def _write_route(handler):
    """Like `_route` for a POST, with one deliberate difference: the handler gets the
    BASE connection, not a server-internal one. `handle_write` enters
    `rls.server_internal` itself and refuses an open transaction, so the read-back after
    the write is the handler's own, separate, block."""
    def wrapper(request, *args, **kwargs):
        if request.method != "POST":
            return HttpResponse(status=405, headers={"Allow": "POST"})
        try:
            with dbconn.serving_connection() as conn:
                auth = authenticate_peer(conn, request)
                # B12 (the project's design log (not part of this release) 003): a declare writes a memory row without the
                # pipeline, so it takes the peer's CALLS bucket here, as a memory.write would.
                peer_limit.admit(auth.peer_id, transport="http")
                return handler(request, conn, auth, *args, **kwargs)
        except peer_limit.PeerRateLimited as exc:
            return peer_limit.http_response(exc)
        except errors.ApiError as exc:
            return JsonResponse(exc.body(), status=exc.status)
        except HandlerError as exc:
            return JsonResponse({"error": exc.code, "message": str(exc)},
                                status=_STATUS_FOR.get(exc.code, 400))
        except SignatureFailed as exc:
            # Divergence 038: the envelope checks `declare` runs are invoke's, so the answer
            # is invoke's too, through the one mapping invoke uses.
            err = api_error_from_rpc(exc)
            return JsonResponse(err.body(), status=err.status)
    wrapper.__name__ = getattr(handler, "__name__", "board_write_route")
    return csrf_exempt(wrapper)


def _json_body(request) -> dict:
    """Wrong content type is 415, not JSON is 400, and JSON but not an object is 422."""
    ctype = (request.content_type or "").split(";")[0].strip().lower()
    if ctype != "application/json" and not ctype.endswith("+json"):
        raise errors.ApiError(415, "E_VALIDATION",
                              f"expected `application/json` content-type, got {ctype!r}")
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.ApiError(400, "E_VALIDATION", f"body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise errors.ApiError(422, "E_VALIDATION", "body must be a JSON object")
    return body


@_write_route
def declare_act(request, conn, auth):
    """POST /v1/board/acts: declare one act. Body `{"envelope": {payload, signature,
    signing_kid}}` where `payload.args` are `memory.write` args for a mesh message v1
    row carrying subject, type and ref (build them with `declare.act_value` and
    `declare.write_args`; sign with `declare.signed_envelope`).

    201 with the act AS THE DATABASE RECORDS IT, read back through the projection, so
    the response proves the row landed and that the actor on it is the bearer. A replay
    of an already-landed envelope is a 200 with `replay: true` (the handler's dedup), not
    a second row.
    """
    body = _json_body(request)
    envelope = body.get("envelope")
    if not isinstance(envelope, dict):
        raise errors.ApiError(422, "E_VALIDATION",
                              "body must carry `envelope` {payload, signature, signing_kid}")
    result = declare.declare(conn, envelope, auth.peer_id)

    with dbconn.request_server_internal(conn) as c:
        state = projection.state_for(c, result["subject"])
        adoption = _adoption(c)
    landed = None
    if isinstance(state, projection.SubjectState):
        landed = next((a for a in state.acts if a.key == result["key"]), None)
    if landed is None:  # pragma: no cover - the handler returned a key the board cannot see
        raise errors.ApiError(500, "E_INTERNAL",
                              f"write reported {result['key']} but the board does not show it")
    out = {
        "key": result["key"],
        "version": result["version"],
        "replay": bool(result.get("replay", False)),
        "act": _jsonable(landed),
        "adoption": adoption,
        "prose": list(projection.render(state, adoption_pair=(adoption["declared"],
                                                              adoption["total"]))),
    }
    return JsonResponse(out, status=200 if out["replay"] else 201)
