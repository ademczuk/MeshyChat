"""Semantic reads over the mesh. Peer-authenticated, READ permission on shared/mesh-chat,
READ ONLY (docs/DIVERGENCES.md 019).

    GET /v1/mesh/search?q=...&k=10[&rerank=0]
    GET /v1/mesh/context?key=msg/...&k=10[&rerank=0]

RANKING (divergence 026): reciprocal-rank fusion of a lexical arm and the embedding arm,
then the reranker over the fused top 40. Reranking is ON by default (`rerank` defaults to
1; `rerank=0` turns it off): 026's amendment records the live paraphrase measurement
behind that and the thread-membership A/B that disagrees with it. A reranker that is down
never fails the request: the fused order is served with `reranked: false`, a `method`
without "+rerank" and a typed rerank attempt. Every response names its `method` and its
`arms`, and says `degraded` when an arm could not run.

ROUTE DESIGN. This semantic surface provides peer JWT authentication through the shared peer
verifier (apps/peers/auth.py), GET reads not gated by DPoP (divergence 015), and the
`{"error","message"}` envelope extended on the 503s with the coverage envelope.

AUTHORIZATION IS THE NAMESPACE'S, not a new rule. A peer that could not `memory.read`
shared/mesh-chat must not read it through a vector either, so the same predicate runs
(apps/memoryskills/permissions.check_permission, need="read"): unclaimed namespace or no
active grant is 403 with the handler's own code. apps/mesh's routes check only the
bearer; this surface returns message BODIES, so it checks the grant.

THE CONTEXT ANCHOR'S BODY IS memory.read's (divergence 027), which asks more than the
namespace grant: a DPoP-bound POST, a signed envelope under a usable key, the skill
resolved and the tenancy or skill-grant gate. So `context` hands search.context_for a
Reader that says whether THIS request proved possession of its token's key (a DPoP proof
for this GET, checked and spent by the write path's own `require_bound_proof`; a request
with none is not refused, its anchor body is withheld), and the rest is decided beside
the anchor row. Search hits and related hits keep 019's single gate.

TRANSACTIONS ARE SHORT ON PURPOSE. The wrapper binds server_internal only for the
permission check; the handlers open their own short server_internal transactions around
each read and hold NONE across a model call, which can take seconds on a busy GPU.

Every response - 200 or 503 - carries the space, coverage, the provider ids actually
used, the ranking `method` and `arms`, and `reranked`, which is true only when a reranker
actually ran.
"""
from __future__ import annotations

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.core import dbconn, peer_limit
from apps.core.minute_bound import MinuteBound
from apps.memoryskills.errors import HandlerError
from apps.memoryskills.permissions import NAMESPACE_WIDE, check_permission
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.mesh import usage
from apps.authapi.didkey import from_did_key
from apps.peers import errors
from apps.peers.auth import (authenticate_peer, dpop_enforced, require_bound_proof,
                             verify_token)

from . import search as S
from . import spaces, store


def _route(handler=None, *, namespace_of=None, own_bound=None, count_as=None):
    """The route's frame: GET only, the peer, its rate bucket, then READ permission on the
    namespace the request is about: the mail unless `namespace_of(request)` names another
    (divergence 060: `/v1/mesh/search?ns=`). That name is resolved and validated before the
    check, so an unknown namespace is a 422 and an unreadable one a typed 403.

    `own_bound` (an apps.core.minute_bound.MinuteBound; divergence 091: the card list) is for a read
    that runs beside a send and must not take the token that send needs. The route then refuses when
    the peer's CALLS bucket is empty but takes nothing from it, and counts the call against its own
    bound instead. That happens BEFORE the read check, so a peer with no grant on the namespace is
    bounded too: after the check, its refused calls were counted by nothing (pxx, reading #179).

    `count_as(request)` (issue #238 C2, divergence 101) names the apps.mesh.usage route this request is
    counted under, or None for an uncounted one. It is asked right after authentication, so a request
    refused later (the rate bucket, a bad namespace, the read check) is counted too: `denied` when the
    read check refused it, `error` for any other refusal, `ok` when the handler answered below 400. The
    count lands after the handler's own transactions have closed, in one of its own, and never fails
    the request (apps/mesh/usage.record)."""
    if handler is None:
        return lambda h: _route(h, namespace_of=namespace_of, own_bound=own_bound, count_as=count_as)

    def wrapper(request, *args, **kwargs):
        if request.method != "GET":
            return HttpResponse(status=405, headers={"Allow": "GET"})
        try:
            with dbconn.serving_connection() as conn:
                auth = authenticate_peer(conn, request)
                counted = count_as(request) if count_as else None
                outcome = "error"
                try:
                    # B12 (the project's design log (not part of this release) 003): the peer's CALLS bucket, before the read gate;
                    # a search embeds the query and reranks (apps/meshsem/search.py).
                    if own_bound is None:
                        peer_limit.admit(auth.peer_id, transport="mesh")
                    else:
                        peer_limit.check(auth.peer_id, transport="mesh")
                        own_bound.admit(auth.peer_id)
                    ns = namespace_of(request) if namespace_of else MESH_CHAT_NAMESPACE
                    with dbconn.request_server_internal(conn) as c:
                        try:
                            check_permission(ns, auth.peer_id, "read", c,
                                             resource=NAMESPACE_WIDE)
                        except HandlerError as exc:
                            outcome = "denied"
                            raise errors.ApiError(403, exc.code, exc.message)
                    response = handler(request, conn, auth, *args, **kwargs)
                    if getattr(response, "status_code", 500) < 400:
                        outcome = "ok"
                    return response
                finally:
                    if counted is not None:
                        usage.record(conn, counted, auth.peer_id, outcome)
        except peer_limit.PeerRateLimited as exc:
            return peer_limit.http_response(exc)
        except errors.ApiError as exc:
            return JsonResponse(exc.body(), status=exc.status)
        except S.SemanticUnavailable as exc:
            return _unavailable(exc)
    wrapper.__name__ = getattr(handler, "__name__", "meshsem_route")
    return csrf_exempt(wrapper)


def _space() -> spaces.Space:
    try:
        return store.configured_space()
    except KeyError as exc:
        raise errors.ApiError(503, "E_SPACE_UNKNOWN", str(exc))


def _k(request) -> int:
    raw = request.GET.get("k")
    if raw is None or raw == "":
        return S.DEFAULT_K
    try:
        k = int(raw)
    except ValueError:
        raise errors.ApiError(422, errors.E_VALIDATION, "k must be an integer")
    if not 1 <= k <= S.MAX_K:
        # Refused, not clamped: a clamp would return fewer than asked with nothing
        # saying so.
        raise errors.ApiError(422, errors.E_VALIDATION, f"k must be within 1..{S.MAX_K}")
    return k


#: Reranking is ON by default (divergence 026, as amended). A missing `rerank` parameter
#: means this; `rerank=0` turns it off.
RERANK_DEFAULT = "1"


def _rerank_flag(request) -> bool:
    raw = request.GET.get("rerank", RERANK_DEFAULT).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise errors.ApiError(422, errors.E_VALIDATION, "rerank must be 1 or 0")


def _search_namespace(request) -> str:
    """`ns`: absent or the mail's name is the mail search, as before. Anything else must be a namespace
    the graph walker derives (MESHY_GRAPH_NAMESPACES, apps/meshsem/graphwalk.walked_namespaces); one per
    request, since the two arms' rankings are fused within one namespace."""
    from . import graphwalk
    names = [n.strip() for n in request.GET.getlist("ns") if n.strip()]
    if not names:
        return MESH_CHAT_NAMESPACE
    if len(names) > 1:
        raise errors.ApiError(422, errors.E_VALIDATION, "ns names one namespace per request")
    ns = names[0]
    from . import ctxsearch
    if ctxsearch.is_context_namespace(ns):
        # v1.1 step 7. Whether its index is ON is answered only AFTER the read check (in `search`): this function runs
        # before it (`_route`'s namespace_of), and the namespace holds everything, secrets included, so a caller
        # without the grant must learn nothing about it, not even whether it is indexed (pxx).
        return ns
    if ns != MESH_CHAT_NAMESPACE and ns not in graphwalk.walked_namespaces():
        raise errors.ApiError(422, errors.E_VALIDATION,
                              f"ns {ns!r} is not a namespace this server derives (MESHY_GRAPH_NAMESPACES)")
    return ns


def _card_filter(request, conn, ns: str):
    """(keep, filter) for a search (divergence 091).

    THE CARD NAMESPACE IS ALWAYS FILTERED, and `status` defaults to "active" as it does on the list:
    a seat asking what was decided must not be handed a superseded ruling unmarked because it named no
    parameter. `status=any` asks for everything. The parameters and their meaning are the card list's
    own (apps/cards/views.card_filter, apps/cards/store.matches), including a requested type's
    replacement, and the answer echoes the filter.

    On any other namespace, the mail included, the card parameters are refused, not ignored: a caller
    that filtered would otherwise read an unfiltered answer as a filtered one. Without them the search
    answers as before: (None, None)."""
    from apps.cards import store as card_store
    from apps.cards import validation as card_rules
    from apps.cards import views as card_views
    if not card_rules.is_card_namespace(ns):
        if card_views.has_card_filter(request):
            raise errors.ApiError(422, errors.E_VALIDATION,
                                  "type, seat, owner, thread, status, since and until filter the card namespace only")
        return None, None
    flt = card_views.card_filter(request)
    superseded, replacing = {}, set()
    with dbconn.request_server_internal(conn) as c:
        if flt["status"] != "any":
            superseded = card_store.superseders(c, ns)
        if flt["types"]:
            replacing = card_store.replacers(c, ns, flt["types"])

    def keep(key, value) -> bool:
        return card_store.matches(value, key, types=flt["types"], seat=flt["seat"], owner=flt["owner"],
                                  thread=flt["thread"], status=flt["status"], superseded=superseded,
                                  replacing=replacing, since=flt.get("since"), until=flt.get("until"))
    return keep, {**flt, "types": list(flt["types"])}


def _search_counted(request) -> str | None:
    """The usage route a search is counted under (issue #238 C2, TKT-4629): `transcript_search*` when its one
    `ns` names the context namespace, `card_search*` when it names the card namespace, else None, so a search
    of the mail stays uncounted here. Read from the raw parameter and never raising, because the frame asks
    before `ns` is validated: a refused search on a counted namespace is still counted (`denied` or `error`),
    with counts only and never query text."""
    from apps.cards import validation as card_rules
    from apps.mesh import usage
    from . import ctxsearch
    names = [n.strip() for n in request.GET.getlist("ns") if n.strip()]
    try:
        if len(names) == 1 and ctxsearch.is_context_namespace(names[0]):
            return usage.transcript_route(request.GET.get("purpose"))
        if len(names) == 1 and card_rules.is_card_namespace(names[0]):
            return usage.card_route("card_search", request.GET.get("purpose"))
        return None
    except Exception:   # a counter's question must never fail the request it counts
        return None


@_route(namespace_of=_search_namespace, count_as=_search_counted)
def search(request, conn, auth):
    """GET /v1/mesh/search?q=&k=&rerank=0|1[&ns=]."""
    q = (request.GET.get("q") or "").strip()
    if not q:
        raise errors.ApiError(422, errors.E_VALIDATION, "q is required")
    if len(q) > S.MAX_QUERY_CHARS:
        raise errors.ApiError(422, errors.E_VALIDATION,
                              f"q is longer than {S.MAX_QUERY_CHARS} characters")
    ns = _search_namespace(request)
    from . import ctxsearch
    if ctxsearch.is_context_namespace(ns):
        # Here the read check has passed (`_route`). Its own filters (seat, src, project, since, until) and collapse;
        # the card parameters' names overlap, so the card refusal below must not run for it.
        from . import ctxjob
        if not ctxjob.enabled():
            raise errors.ApiError(422, errors.E_VALIDATION,
                                  f"ns {ns!r} is not indexed on this server (MESHY_CONTEXT_INDEX is off)")
        try:
            body = ctxsearch.search(conn, _space(), ns, q, ctxsearch.filters_from(request.GET), k=_k(request),
                                    rerank=_rerank_flag(request), collapse=ctxsearch.collapse_of(request.GET))
        except errors.ApiError as exc:
            if exc.code != ctxsearch.E_BUSY:
                raise
            # Every transcript-search slot is taken (ctxsearch._gate): say when to come back.
            return JsonResponse(exc.body(), status=exc.status, headers={"Retry-After": str(exc.retry_after)})
        return JsonResponse(body, status=200)
    if "collapse" in request.GET or "src" in request.GET or "project" in request.GET:
        raise errors.ApiError(422, errors.E_VALIDATION, "collapse, src and project filter the context namespace only")
    # Before the branch: the card parameters are refused on the mail too (named or by default), not
    # only on another graph namespace. Ignored, they would return unfiltered mail as a filtered answer.
    keep, card_filter = _card_filter(request, conn, ns)
    if ns != MESH_CHAT_NAMESPACE:
        from . import graphsearch
        body = graphsearch.search(conn, _space(), ns, q, k=_k(request), rerank=_rerank_flag(request),
                                  keep=keep)
        if card_filter is not None:
            body["filter"] = card_filter
    else:
        body = S.search(conn, _space(), q, k=_k(request), rerank=_rerank_flag(request))
    return JsonResponse(body, status=200)


def _reader(conn, request, auth) -> S.Reader:
    """Who is asking, and whether this GET proved what memory.read's POST must: with DPoP
    enforced, a proof bound to this method, path and token, signed by the key the
    token's `cnf` names, fresh, and its jti unspent (then spent). The write path's own
    check (apps/peers/auth.require_bound_proof), so the two cannot differ; the key it was
    checked against is handed on, since it must also be one memory.read verifies under.
    A missing or bad proof is not a 403 here: the route stays a bearer read (019), and
    only the anchor's body, which is memory.read's answer, waits for the proof. A ledger
    fault (5xx) is raised, not read as "no proof". With DPoP off, memory.read's POST
    needs no proof either."""
    if not dpop_enforced():
        return S.Reader(peer_id=auth.peer_id, possession=True)
    if request.headers.get("DPoP") is None:
        return S.Reader(peer_id=auth.peer_id, possession=False)
    claims = verify_token(auth.token)
    try:
        require_bound_proof(conn, request, claims, auth.token, auth.peer_id)
    except errors.ApiError as exc:
        if exc.status >= 500:
            raise
        return S.Reader(peer_id=auth.peer_id, possession=False)
    return S.Reader(peer_id=auth.peer_id, possession=True, key=from_did_key(claims["cnf"]))


#: `_reader`'s public name, for the other GET that shows a mesh message's body only as
#: memory.read without a capability would: the addressed /v1/mesh/wait's preview (divergence
#: 033; divergence 040).
reader_for_request = _reader


@_route
def context(request, conn, auth):
    """GET /v1/mesh/context?key=msg/...&k=&rerank=0|1."""
    key = (request.GET.get("key") or "").strip()
    if not key.startswith("msg/"):
        raise errors.ApiError(422, errors.E_VALIDATION, "key must be a msg/ key")
    body = S.context_for(_reader(conn, request, auth), conn, _space(), key, k=_k(request),
                         rerank=_rerank_flag(request))
    return JsonResponse(body, status=200)


#: Advisory pre-send hints run beside a send and must not take the CALLS token the send needs
#: (divergence 091's MinuteBound pattern).
_HINTS_BOUND = MinuteBound("hints", per_minute=30)


@csrf_exempt
def hints(request):
    """POST /v1/mesh/hints: pre-send semantic collision hints (off by default via MESHY_SEND_HINTS_ENABLED)."""
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    from . import send_hints
    try:
        with dbconn.serving_connection() as conn:
            auth = authenticate_peer(conn, request)
            peer_limit.check(auth.peer_id, transport="mesh")
            _HINTS_BOUND.admit(auth.peer_id)
            with dbconn.request_server_internal(conn) as c:
                try:
                    check_permission(MESH_CHAT_NAMESPACE, auth.peer_id, "read", c,
                                     resource=NAMESPACE_WIDE)
                except HandlerError as exc:
                    raise errors.ApiError(403, exc.code, exc.message)
            req = send_hints.parse_request(request.body)
            if not send_hints.enabled():
                return JsonResponse({"enabled": False, "hints": [], "dropped": {},
                                     "reranked": False, "abstained_reason": "disabled"}, status=200)
            body = send_hints.evaluate_hints(conn, _space(), auth.peer_id, req)
            return JsonResponse(body, status=200)
    except peer_limit.PeerRateLimited as exc:
        return peer_limit.http_response(exc)
    except errors.ApiError as exc:
        return JsonResponse(exc.body(), status=exc.status)
    except S.SemanticUnavailable as exc:
        return _unavailable(exc)


def _unavailable(exc: "S.SemanticUnavailable") -> JsonResponse:
    """A typed refusal; a temporary one (E_INDEX_WARMING) also says when to come back."""
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return JsonResponse(exc.body(), status=exc.status, headers=headers)


#: Per-seat transcript-search usage view (TKT-4629): default 7 UTC days (one week), bounded on its own
#: MinuteBound so diagnostics never take a CALLS token, and never counted itself.
TRANSCRIPT_USAGE_DEFAULT_DAYS = 7
_TRANSCRIPT_USAGE_BOUND = MinuteBound("transcript_usage", per_minute=120)


def _transcript_usage_days(request) -> int:
    raw = request.GET.get("days")
    if raw is None or raw == "":
        return TRANSCRIPT_USAGE_DEFAULT_DAYS
    try:
        days = int(raw)
    except ValueError:
        raise errors.ApiError(422, errors.E_VALIDATION, "days must be an integer")
    if not 1 <= days <= usage.TRANSCRIPT_DAILY_MAX_DAYS:
        raise errors.ApiError(422, errors.E_VALIDATION,
                              f"days must be within 1..{usage.TRANSCRIPT_DAILY_MAX_DAYS}")
    return days


def _transcript_namespace(request) -> str:
    from apps.context import validation as ctx_rules
    return ctx_rules.namespace()


@_route(namespace_of=_transcript_namespace, own_bound=_TRANSCRIPT_USAGE_BOUND)
def transcript_usage(request, conn, auth):
    """GET /v1/mesh/transcript-usage?[days=1..90] (TKT-4629): how often each seat searched the transcript-context
    namespace, per UTC day and aggregated per seat over the window, by route and outcome, from apps/mesh/usage.
    Readable by any peer that holds the read grant on the context namespace; not counted itself."""
    days = _transcript_usage_days(request)
    with dbconn.request_server_internal(conn) as c:
        daily = usage.transcript_daily(c, days=days)
        totals = usage.transcript_summary(c, days=days)
    return JsonResponse({"namespace": _transcript_namespace(request), **daily, "totals": totals["routes"]}, status=200)


__all__ = ["context", "hints", "search", "transcript_usage"]
