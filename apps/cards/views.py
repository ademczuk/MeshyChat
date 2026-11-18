"""GET /v1/cards: the decision cards, filtered (divergence 091; docs/design/decision-cards.md 5).

A FILTER, not a search: no model is called and nothing is ranked. The seat-start read is this route
with `type=Ruling&type=Constraint&type=Commitment&type=OpenItem` and the default `status=active`.
Search over the same cards is `GET /v1/mesh/search?ns=<the card namespace>`, which takes the same
`type`, `seat`, `owner` and `status` (apps/meshsem/views.py).

AUTHORIZATION is the namespace's own read grant, checked by the semantic routes' frame
(apps/meshsem/views._route): a peer that could not `memory.read` the namespace gets the same 403.

    GET /v1/cards?[type=..]&[seat=]&[owner=]&[thread=]&[status=active|superseded|any]&[since=]&[until=]
                  &[recorded_since=]&[limit=1..200]&[after=<cursor>]

NO TEXT PARAMETER, ever: this is a GET, and the server's access log records a GET's query string. A
question in words belongs to the search route's own parameter, not here.
"""
from __future__ import annotations

from django.http import JsonResponse

from apps.core import dbconn
from apps.core.minute_bound import MinuteBound
from apps.memoryskills.errors import HandlerError
from apps.memoryskills.validation import MESH_THREAD_RE
from apps.meshsem.views import _route
from apps.peers import errors

from . import store, validation


def card_filter(request) -> dict:
    """The `type`, `seat`, `owner`, `thread`, `status`, `since` and `until` parameters, validated; the search
    route reads the same ones through this function, so the two cannot disagree on what a filter means.
    `since` (inclusive) and `until` (exclusive) are on the card's own `time`; they are keys of the answer only
    when asked, so an answer to a request without them is what it was before they existed."""
    types = [t.strip() for t in request.GET.getlist("type") if t.strip()]
    for t in types:
        if t not in validation.TYPES:
            raise errors.ApiError(422, errors.E_VALIDATION,
                                  f"type must be one of {', '.join(sorted(validation.TYPES))}")
    status = (request.GET.get("status") or "active").strip()
    if status not in store.STATUSES:
        raise errors.ApiError(422, errors.E_VALIDATION, f"status must be one of {', '.join(store.STATUSES)}")
    out = {"types": tuple(dict.fromkeys(types)), "status": status, "seat": None, "owner": None, "thread": None}
    for name in ("seat", "owner"):
        raw = request.GET.get(name)
        if raw is not None and raw.strip():
            if len(raw) > 64:
                raise errors.ApiError(422, errors.E_VALIDATION, f"{name} is at most 64 characters")
            out[name] = raw.strip()
    thread = request.GET.get("thread")
    if thread is not None and thread.strip():
        if not MESH_THREAD_RE.fullmatch(thread.strip()):
            raise errors.ApiError(422, errors.E_VALIDATION, "thread must be a mesh thread id")
        out["thread"] = thread.strip()
    for name in ("since", "until"):
        raw = _instant(request, name)
        if raw is not None:
            out[name] = raw
    return out


def has_card_filter(request) -> bool:
    return any(name in request.GET for name in ("type", "seat", "owner", "thread", "status", "since", "until"))


def _instant(request, name: str) -> str | None:
    raw = request.GET.get(name)
    if raw is None or raw == "":
        return None
    try:
        validation.parse_time(raw)      # the shape AND a real instant: month 13 has the shape
    except HandlerError:
        raise errors.ApiError(422, errors.E_VALIDATION,
                              f"{name} must be a UTC instant written YYYY-MM-DDTHH:MM:SS.mmmZ")
    return raw


def _limit(request) -> int:
    raw = request.GET.get("limit")
    if raw is None or raw == "":
        return store.DEFAULT_LIMIT
    try:
        limit = int(raw)
    except ValueError:
        raise errors.ApiError(422, errors.E_VALIDATION, "limit must be an integer")
    if not 1 <= limit <= store.MAX_LIMIT:
        # Refused, not clamped: a clamp would return fewer than asked with nothing saying so.
        raise errors.ApiError(422, errors.E_VALIDATION, f"limit must be within 1..{store.MAX_LIMIT}")
    return limit


#: The list's own bound, since it takes no CALLS token: calls per peer per minute, in this process. A
#: client reads once per send (the line before a send) and once per session start, and pages a seat-start
#: walk at 200 a page, so an honest seat stays far below it.
CARDS_PER_MINUTE = 120
_BOUND = MinuteBound("cards", CARDS_PER_MINUTE)


def _counted_as(base: str, request) -> str | None:
    """The usage route of this card read (issue #238 C3): `cards`, or the route of the `purpose` the client
    sent for a read it made on its own behalf (apps/mesh/usage.card_route), or None for a status poll."""
    from apps.mesh import usage
    return usage.card_route(base, request.GET.get("purpose"))


@_route(namespace_of=lambda request: validation.namespace(), own_bound=_BOUND,
        count_as=lambda request: _counted_as("cards", request))
def cards(request, conn, auth):
    """GET /v1/cards. Takes no CALLS token (it runs beside a send, and the first build took the token the
    send needed: pxx's review of the client, msg/<key>); the route's frame counts it
    against _BOUND instead, before the read check."""
    flt = card_filter(request)
    after = None
    raw_after = request.GET.get("after")
    if raw_after:
        try:
            after = store.decode_cursor(raw_after)
        except ValueError:
            raise errors.ApiError(422, errors.E_VALIDATION, "after is not a cursor this route returned")
    ns = validation.namespace()
    with dbconn.request_server_internal(conn) as c:
        page = store.list_cards(c, ns, types=flt["types"], seat=flt["seat"], owner=flt["owner"],
                                thread=flt["thread"], status=flt["status"], since=flt.get("since"),
                                until=flt.get("until"),
                                recorded_since=_instant(request, "recorded_since"), limit=_limit(request),
                                after=after)
    return JsonResponse({"namespace": ns, "filter": {**flt, "types": list(flt["types"])}, **page}, status=200)


#: The usage view's default window and the bound on what one request may ask for.
USAGE_DEFAULT_DAYS = 14


def _usage_days(request) -> int:
    from apps.mesh import usage
    raw = request.GET.get("days")
    if raw is None or raw == "":
        return USAGE_DEFAULT_DAYS
    try:
        days = int(raw)
    except ValueError:
        raise errors.ApiError(422, errors.E_VALIDATION, "days must be an integer")
    if not 1 <= days <= usage.CARD_DAILY_MAX_DAYS:
        # Refused, not clamped: a clamp would answer for fewer days than asked with nothing saying so.
        raise errors.ApiError(422, errors.E_VALIDATION, f"days must be within 1..{usage.CARD_DAILY_MAX_DAYS}")
    return days


@_route(namespace_of=lambda request: validation.namespace(), own_bound=_BOUND)
def card_usage(request, conn, auth):
    """GET /v1/cards/usage?[days=1..90] (issue #238 C4, divergence 101): how often each seat read the cards,
    per UTC day: the card list and the card search, by outcome, from apps/mesh/usage. Readable by any peer
    that may read the card namespace, the same audience as the cards themselves; the operator approved
    the per-seat, per-day view on 12 Oct 2026. Not counted itself, so reading the view never inflates
    what it reports. Diagnostics, not recall: a read is a fetch, not a use (issue #238 A)."""
    from apps.mesh import usage
    days = _usage_days(request)
    with dbconn.request_server_internal(conn) as c:
        daily = usage.card_daily(c, days=days)
        totals = usage.card_summary(c, days=days)
    return JsonResponse({"namespace": validation.namespace(), **daily, "totals": totals["routes"]}, status=200)


__all__ = ["card_filter", "card_usage", "cards", "has_card_filter"]
