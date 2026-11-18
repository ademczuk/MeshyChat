"""The mesh graph over HTTP. Peer-authenticated, READ ONLY on the log and the projection.

Nothing here writes the projection. It is written by its background job and by
`python -m apps.mesh rebuild`, never by a request, which is discipline 2 in
apps/mesh/projection.py made structural: there is no view through which a request could
reach mesh_edges or mesh_edges_projection with a write. The one table a request writes is
`mesh_route_usage` (divergence 018), a counter of requests that is not derived from the
log and has nothing to be rebuilt from; apps/mesh/usage.py says why it is a table.

`_route` is apps/board/views.py's, copied rather than imported so a change to the board's
auth shape cannot silently change this surface. Same order: authenticate on the base
connection, THEN enter `server_internal`, which reading the projection requires.

READ PERMISSION ON shared/mesh-chat IS REQUIRED, divergence 018 (a). Before it, any
authenticated peer could read any inbox, owed list, thread or the drift numbers: the
routes checked the bearer and nothing else, while `memory.read` and `memory.query` on the
same namespace are default-deny. Every graph route now calls the SAME function those
handlers call, `apps.memoryskills.permissions.check_permission(namespace, peer, "read")`,
inside the same `server_internal` context, and a refusal is a 403 carrying that
function's own typed code (E_PERMISSION_DENIED, or E_NAMESPACE_UNCLAIMED when nobody has
claimed the namespace). One predicate, called here, re-expressed nowhere
(V1-DESIGN-RULES A2). `/v1/mesh/wait` is the exception and says why in apps/mesh/wait.py:
its authorisation is the row filter, and a peer that can read nothing gets 204.

THE CALLER'S LABELS, divergence 018 (b). The fleet roster (`handlers.roster_label_of`) was
never loaded in production here (since divergence 034 it is, at startup, when
MESHY_FLEET_ROSTER_PATH is set), so every caller used to be answered as `peer-<id8>`,
a label no sender addresses. The peer's own `peers.name` is now used, and the roster,
when one is loaded, is an override for the PRIMARY label. Both are answered for when
they differ, because on the production standby (2026-09-23, last 7 days) they disagree
for real seats in BOTH directions: `nxxxxx-cc` is the roster label and 100 messages were
addressed to `nxxxxx` (peers.name) and none to `nxxxxx-cc`, while `memory-bxxxxx` is the
roster label with 205 messages and `bxxxxx` (peers.name) had 9. A single label, whichever
source won, would have emptied someone's inbox. `label`/`label_source` name the primary,
`labels` lists every label answered for with its source, and each hit carries the `dst`
it was reached through. `peer-<id8>`, the daemon's own fallback, is always in `labels`,
last, because it is what this server stamps as the author of the caller's own writes when
no roster is loaded; with neither other source usable it is also the primary and
`label_source` says "fallback".

The label is a FILTER, not an authorisation. A peer that names itself after another seat
sees mail addressed to that seat; it could already read that mail, because reading the
inbox at all needs read on the whole namespace (the gate above).

COVERAGE ON EVERY RESPONSE, for the reason apps/board attaches adoption everywhere.
USAGE ON THE DRIFT RESPONSE, divergence 018 (d): per-route calls and distinct peers over
seven days, from `mesh_route_usage`.

INFERRED EDGES ONLY ON REQUEST, divergence 021. `inbox` and `thread` merge in what
apps/meshaudit inferred for legacy rows only under `?include_inferred=1`, and every hit
carries `tier` ("declared" or "inferred") either way; apps/meshaudit/reads.py owns that
merge so these views stay a thin read of mesh_edges. The inferred tier sits behind the
same read gate as the declared one (018 (a)): `_route` refuses before either is read, and
the inbox's inferred hits are matched against every label in `labels`, not only the
primary, so the two tiers answer for the same caller.

NEIGHBOURS, divergence 032. `GET /v1/mesh/neighbours?key=msg/...` is one message's graph
neighbourhood, ONE hop: declared (its replies_to parent, what replies to it, its thread and
the thread's size) by default, and with `include_inferred=1` the persisted related_to edges
in both directions (apps/meshsem/related_reads.py; each neighbour carries its thread, so a
client walks across threads one call per hop), the auditor's inferred replies_to, and the
related coverage. No message bodies, as on every graph route. `limit` defaults to 20 and
above 100 is a 422, refused rather than clamped; a bad flag is a 422.
"""
from __future__ import annotations

import logging
import re
import datetime as _dt
import threading
import time
import unicodedata
from datetime import datetime

import psycopg
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.calls.retention import call_retention_secs
from apps.core import dbconn, peer_limit
from apps.core.jsonable import jsonable
from apps.memoryskills import handlers
from apps.memoryskills.errors import HandlerError
from apps.memoryskills.permissions import NAMESPACE_WIDE, check_permission
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE, validate_mesh_key_ts
from apps.meshaudit import reads as inferred_reads
from apps.peers import errors
from apps.peers import names as peer_names
from apps.peers.auth import authenticate_peer, verify_token

from . import graph, projection, usage
from . import wait as mesh_wait

log = logging.getLogger(__name__)

#: The HandlerError codes `check_permission` raises for a caller it refuses. Both are a
#: 403, as on the board's write door (apps/board/views.py _STATUS_FOR). The set the usage
#: counter reads a `denied` outcome by, so a 403 here and a `denied` row there are one rule.
_DENIED = usage.DENIED_CODES


#: The one read-model-to-JSON conversion, shared with apps/board/views.py.
_jsonable = jsonable


def require_mesh_read(conn, peer_id) -> None:
    """Divergence 018 (a): `memory.read`'s own check, on the namespace this surface reads.
    Raises HandlerError on refusal. `conn` must already be inside `server_internal`:
    shared_memory_namespace and shared_memory_grant are readable only there (sql/028).
    NAMESPACE_WIDE: the question is whether the caller may read the namespace these routes
    answer from, not any one key in it (divergence 040, slice 2)."""
    check_permission(MESH_CHAT_NAMESPACE, peer_id, "read", conn, resource=NAMESPACE_WIDE)


def _route(route_name: str, *, spends_calls: bool = True):
    """`spends_calls=False` (divergence 083: the sender's advisory recipient check) refuses when the
    peer's CALLS bucket is empty but takes NO token, so an advisory read can never spend the token the
    send it precedes needs (Codex on #145). Such a route bounds itself (_recipients_admit)."""
    def decorate(handler):
        def wrapper(request, *args, **kwargs):
            if request.method != "GET":
                return HttpResponse(status=405, headers={"Allow": "GET"})
            try:
                with dbconn.serving_connection() as conn:
                    auth = authenticate_peer(conn, request)
                    # B12 (the project's design log (not part of this release) 003): the peer's CALLS bucket, before the read
                    # gate, the read and its usage row.
                    if spends_calls:
                        peer_limit.admit(auth.peer_id, transport="mesh")
                    else:
                        peer_limit.check(auth.peer_id, transport="mesh")
                    outcome = "error"
                    recorded = route_name   # a handler may name its own usage dimension (_record_as)
                    try:
                        with dbconn.request_server_internal(conn) as c:
                            refused = _mesh_read_refusal(c, auth.peer_id)
                            if refused is not None:
                                outcome = "denied"
                                return refused
                            response = handler(request, c, auth, *args, **kwargs)
                            recorded = getattr(response, "_mesh_usage_route", route_name)
                            outcome = "ok"
                            return response
                    finally:
                        # After the read's transaction has closed, in one of its own
                        # (apps/mesh/usage.py). Never raises.
                        usage.record(conn, recorded, auth.peer_id, outcome)
            except peer_limit.PeerRateLimited as exc:
                return peer_limit.http_response(exc)
            except errors.ApiError as exc:
                return JsonResponse(exc.body(), status=exc.status)
        wrapper.__name__ = getattr(handler, "__name__", "mesh_route")
        return csrf_exempt(wrapper)
    return decorate


def _coverage(conn) -> dict:
    projected, total = graph.coverage(conn)
    return {"projected": projected, "total": total}


def _peer_name(conn, peer_id) -> str | None:
    """`peers.name`, used verbatim when present, or None when blank or contested (divergence
    066). `apps.peers.names.name_of`, the read the addressed wait's `caller_labels` makes too."""
    return peer_names.name_of(conn, peer_id)


def _labels_from_name(peer_id, name: str | None) -> list[dict]:
    out: list[dict] = []
    roster = handlers.roster_label_of(peer_id)
    if roster:
        out.append({"label": roster, "source": "roster"})
    if name and name != roster:
        out.append({"label": name, "source": "peers.name"})
    fallback = f"peer-{str(peer_id)[:8]}"
    if all(x["label"] != fallback for x in out):
        out.append({"label": fallback, "source": "fallback"})
    return out


def labels_for(conn, peer_id) -> list[dict]:
    """Every label the caller is answered for, primary first, each with its source.

    Order: the roster label when one is loaded, then `peers.name` (unless another peer also
    holds that name or the roster gives it to another peer: divergence 066, `_peer_name`),
    then `peer-<id8>`.
    The fallback is ALWAYS answered for, last, and is the primary only when neither
    source is usable. It stays in the set because it is the `author_label` this server
    stamps on the peer's own writes when no roster is loaded
    (apps/memoryskills/handlers.py:614): without it `owed` could not recognise a
    message the caller wrote itself, or that it has spoken in a thread since."""
    return _labels_from_name(peer_id, _peer_name(conn, peer_id))


def labels_for_peers(conn, peer_ids) -> dict[str, list[dict]]:
    """Batch `labels_for` over `peer_ids` using one `peer_names.names_of` lookup rather than
    scanning `peers` per candidate peer on `/v1/mesh/fleet/unread`."""
    ids = list(dict.fromkeys(str(p) for p in peer_ids))
    names = peer_names.names_of(conn, ids)
    return {pid: _labels_from_name(pid, names.get(pid)) for pid in ids}


def _limit(request, default: int | None) -> int | None:
    raw = request.GET.get("limit")
    if raw is None:
        return default
    try:
        n = int(raw)
    except ValueError:
        raise errors.ApiError(422, "E_VALIDATION", "limit must be an integer")
    return n if n > 0 else default


#: The longest `thread` the owed filter takes: the write path's own bound on a thread_id, 1 to 256 characters
#: (MESH_THREAD_RE, apps/memoryskills/validation.py). No message written under that rule has a longer one, and
#: the cap keeps a caller from sending a megabyte to the planner (pxx's review of #165). It bounds the value as queried,
#: stripped, so padding round a thread a message can carry is not refused (memory-bxxxxx's review of #212).
OWED_THREAD_MAX = 256


def _owed_filters(request, *, with_thread: bool) -> tuple[list[str] | None, datetime | None, str | None, dict]:
    """Parse `/owed`'s optional bounds without turning a malformed bound into all mail.

    The route is used twice weekly and measured 428 stale unanswered rows on one seat;
    a naive client timestamp would make the server timezone decide its window, so it is
    refused rather than guessed. The returned echo preserves the caller's spelling so a
    saved command can be compared to the response that applied it.

    `thread` (#155, carried forward from #165 by a reviewer) is parsed only `with_thread`,
    which owed passes and awaiting does not: awaiting neither applies nor echoes it, so a
    client that checks the echo refuses that answer instead of taking it as filtered. It is
    refused when blank, when it holds a control character (a NUL reached Postgres as an
    uncaught 500, Codex on #165), or when longer than OWED_THREAD_MAX. It is queried stripped,
    as the projection stores a thread (normalize.thread_of), and echoed as sent, like
    `since`. `thread_id` is not an alias: one name, so one echo key (pxx's review of #165).
    """
    applied: dict = {}
    states = None
    raw_states = request.GET.get("state")
    if raw_states is not None:
        states = raw_states.split(",")
        if not states or any(state not in {"open", "active", "answered", "declined", "closed"} for state in states):
            raise errors.ApiError(422, "E_VALIDATION",
                                  "state must be comma-separated open, active, answered, declined, or closed")
        applied["state"] = states
    since = None
    raw_since = request.GET.get("since")
    if raw_since is not None:
        try:
            since = datetime.fromisoformat(raw_since[:-1] + "+00:00"
                                            if raw_since.endswith("Z") else raw_since)
        except ValueError:
            since = None
        if since is None or since.tzinfo is None or since.utcoffset() is None:
            raise errors.ApiError(422, "E_VALIDATION",
                                  "since must be an ISO-8601 timestamp with timezone offset or Z")
        applied["since"] = raw_since
    thread = None
    raw_thread = request.GET.get("thread") if with_thread else None
    if raw_thread is not None:
        thread = raw_thread.strip()
        if not thread:
            raise errors.ApiError(422, "E_VALIDATION", "thread must not be empty")
        if any(unicodedata.category(ch) == "Cc" for ch in raw_thread):
            raise errors.ApiError(422, "E_VALIDATION", "thread must not contain control characters")
        if len(thread) > OWED_THREAD_MAX:
            raise errors.ApiError(422, "E_VALIDATION",
                                  f"thread must be at most {OWED_THREAD_MAX} characters, the longest "
                                  "thread_id a message can be written with")
        applied["thread"] = raw_thread
    return states, since, thread, applied


def _record_as(response: JsonResponse, route_name: str) -> JsonResponse:
    """Let a read handler select its already-migrated usage dimension."""
    response._mesh_usage_route = route_name
    return response


def _brief_filters(request, as_of: datetime) -> tuple[datetime, int | None, int, int, str | None]:
    """Strictly parse brief's bounded window and presentation controls."""
    raw_since = request.GET.get("since")
    raw_days = request.GET.get("days")
    if raw_since is not None and raw_days is not None:
        raise errors.ApiError(422, "E_VALIDATION", "since and days cannot be used together")
    if raw_since is not None:
        try:
            since = datetime.fromisoformat(raw_since[:-1] + "+00:00"
                                            if raw_since.endswith("Z") else raw_since)
        except ValueError:
            since = None
        if since is None or since.tzinfo is None or since.utcoffset() is None:
            raise errors.ApiError(422, "E_VALIDATION",
                                  "since must be an ISO-8601 timestamp with timezone offset or Z")
        days = None
    else:
        try:
            days = 14 if raw_days is None else int(raw_days)
        except ValueError:
            raise errors.ApiError(422, "E_VALIDATION", "days must be an integer within 1..90")
        if not 1 <= days <= 90:
            raise errors.ApiError(422, "E_VALIDATION", "days must be within 1..90")
        since = as_of - _dt.timedelta(days=days)
    def bounded(name: str, default: int, maximum: int) -> int:
        raw = request.GET.get(name)
        try:
            value = default if raw is None else int(raw)
        except ValueError:
            raise errors.ApiError(422, "E_VALIDATION", f"{name} must be an integer within 1..{maximum}")
        if not 1 <= value <= maximum:
            raise errors.ApiError(422, "E_VALIDATION", f"{name} must be within 1..{maximum}")
        return value
    limit = bounded("limit", 20, 100)
    help_after_h = bounded("help_after_h", 4, 168)
    via = request.GET.get("via")
    if via is not None and via != "notice":
        raise errors.ApiError(422, "E_VALIDATION", "via must be notice")
    return since, days, limit, help_after_h, via


@_route("inbox")
def inbox(request, conn, auth):
    """GET /v1/mesh/inbox[?limit=N][&cc=0]: messages addressed to the calling peer."""
    labels = labels_for(conn, auth.peer_id)
    include_cc = request.GET.get("cc", "1") not in ("0", "false")
    limit = _limit(request, 50)
    answered = [x["label"] for x in labels]
    result = graph.inbox(conn, answered, include_cc=include_cc, limit=limit)
    tiered = inferred_reads.for_inbox(request, conn, result, labels=answered, limit=limit)
    return JsonResponse({
        "label": labels[0]["label"], "label_source": labels[0]["source"], "labels": labels,
        "hits": _jsonable(tiered.hits), "truncated": tiered.truncated,
        "coverage": _coverage(conn), "inferred": _jsonable(tiered.inferred),
    }, status=200)


@_route("owed")
def owed(request, conn, auth):
    """GET /v1/mesh/owed: unresolved acks the calling peer owes.

    The default includes open, active, and directly answered asks: a reply is activity,
    not proof of completion. An explicit close settles the debt; state=closed is only an
    historical inspection request. That default is for a request without `state`: the
    client's bare `owed` sends state=open,active since #195, so it lists open and active
    asks only, and `owed --state all` gets this default. `thread=` narrows the list to one
    thread in SQL before LIMIT (#155); `_owed_filters` says how each bound is checked.
    """
    labels = labels_for(conn, auth.peer_id)
    states, since, thread, applied = _owed_filters(request, with_thread=True)
    limit = _limit(request, 100)
    items = graph.owed_acks(conn, [x["label"] for x in labels], limit=limit + 1,
                            states=states, since=since, thread_id=thread)
    truncated = len(items) > limit
    body = {
        "label": labels[0]["label"], "label_source": labels[0]["source"], "labels": labels,
        "owed": _jsonable(items[:limit]), "truncated": truncated, "coverage": _coverage(conn),
    }
    if applied:
        body["filters"] = applied
    return JsonResponse(body, status=200)


def _closable_mesh_key(key: str) -> bool:
    """The key a message may name in `closes` (validate_mesh_message_v1): msg/, at most 512 characters, with an
    in-band epoch-ms ts segment. The same rule, not a narrower one, so every question an answer can close can also
    be looked up here (Codex on #407: a stricter allowlist silently disabled auto-close for valid keys)."""
    if not key.startswith("msg/") or len(key) > 512 or "\x00" in key:
        return False   # NUL: no stored key has one (jsonb refuses U+0000), and psycopg refuses it in a parameter
    try:
        validate_mesh_key_ts(key)
    except HandlerError:
        return False
    return True


@_route("owed", spends_calls=False)
def owed_key(request, conn, auth):
    """One-key, routing-only pre-send check. Shares owed usage, not its CALLS charge.

    No list, body, field selection or caller-label override is accepted. The independent
    advisory bound prevents this tokenless route from becoming an unmetered scan.
    """
    _owed_key_admit(auth.peer_id)
    keys = request.GET.getlist("key")
    if (set(request.GET) != {"key"} or len(keys) != 1 or request.body
            or not _closable_mesh_key(keys[0])):
        raise errors.ApiError(422, "E_VALIDATION", "give exactly one msg/ key and no other fields or body")
    key = keys[0]
    labels = labels_for(conn, auth.peer_id)
    items = graph.owed_acks(conn, [x["label"] for x in labels], keys=[key], limit=1,
                            states=["open", "active", "answered", "closed"])
    # Explicit allowlist: never serialize done_when, author, message content or a full Hit.
    rows = [{"hit": {"key": item.hit.key, "version": item.hit.version},
             "state": item.state, "msg_type": item.msg_type, "thread_id": item.thread_id}
            for item in items]
    return JsonResponse({"key": key, "owed": rows}, status=200)


@_route("awaiting")
def awaiting(request, conn, auth):
    """GET /v1/mesh/awaiting: unresolved asks this caller authored that another label owes."""
    labels = labels_for(conn, auth.peer_id)
    states, since, _thread, applied = _owed_filters(request, with_thread=False)
    limit = _limit(request, 100)
    items = graph.awaiting(conn, [x["label"] for x in labels], limit=limit + 1,
                           states=states, since=since)
    truncated = len(items) > limit
    body = {"label": labels[0]["label"], "label_source": labels[0]["source"], "labels": labels,
            "awaiting": _jsonable(items[:limit]), "truncated": truncated, "coverage": _coverage(conn)}
    if applied:
        body["filters"] = applied
    return JsonResponse(body, status=200)


TO_CLOSE_SETTLE_S = 300
TO_CLOSE_LIMIT = 12


@_route("brief")
def brief(request, conn, auth):
    """GET /v1/mesh/brief: a bounded routing-only work summary for the caller."""
    as_of = _dt.datetime.now(_dt.timezone.utc)
    since, days, limit, help_after_h, via = _brief_filters(request, as_of)
    labels = labels_for(conn, auth.peer_id)
    answered = [x["label"] for x in labels]
    owed = graph.owed_acks(conn, answered, states=("open", "active"), since=since,
                           limit=limit + 1)
    awaited = _brief_awaiting(conn, answered, peer_id=auth.peer_id, since=since, limit=limit)
    threads = graph.thread_brief(conn, answered, since=since, limit=limit + 1)
    wanted = graph.help_wanted(conn, answered, since=since,
                                help_after=_dt.timedelta(hours=help_after_h), limit=limit + 1)
    to_close_limit = limit if limit > 20 else min(limit, TO_CLOSE_LIMIT)
    to_close_items, to_close_truncated = graph.to_close(
        conn, answered, since=since,
        settled_before=as_of - _dt.timedelta(seconds=TO_CLOSE_SETTLE_S),
        limit=to_close_limit,
    )
    owed_counts = graph.owed_counts(conn, answered, since=since)
    body = {
        "label": labels[0]["label"], "label_source": labels[0]["source"], "labels": labels,
        "as_of": as_of.isoformat(),
        "filters": {"since": since.isoformat(), "days": days, "limit": limit,
                    "help_after_h": help_after_h},
        "owed": {"items": _jsonable(owed[:limit]),
                 "counts": {state: owed_counts[state] for state in ("open", "active")},
                 "truncated": len(owed) > limit},
        "awaiting": None if awaited is None else {
            "items": _jsonable(awaited[0][:limit]),
            "counts": {state: awaited[1][state] for state in ("open", "active")},
            "truncated": len(awaited[0]) > limit},
        "awaiting_timed_out": awaited is None,
        "to_close": {"items": _jsonable(to_close_items),
                     "truncated": to_close_truncated,
                     "due": False,
                     "settle_seconds": TO_CLOSE_SETTLE_S},
        "threads": {"items": _jsonable(threads[:limit]), "truncated": len(threads) > limit},
        "help_wanted": {"items": _jsonable(wanted[:limit]), "truncated": len(wanted) > limit},
        "coverage": _coverage(conn),
    }
    return _record_as(JsonResponse(body, status=200), "brief_notice" if via else "brief")


#: `days` when neither it nor `since` is given. A seat's band looks back 12 hours; a week leaves
#: room for a seat that was away.
UNREAD_DEFAULT_DAYS = 7


def _unread_window(request, as_of: datetime) -> tuple[datetime, dict]:
    """The window `unread` answers for, and the `filters` it echoes.

    `since` (ISO-8601 with an offset or Z) or `days` (an integer), never both; `limit` 1..500,
    default 100. Malformed or out-of-range input is a 422, as on brief. One bound is a CAP rather
    than a refusal, on kxxx's condition: a window may not open before the read-call retention
    (MESHYCHAT_CALL_RETENTION_DAYS, apps/calls/retention.py) less one day, because a read the
    retention has already deleted would turn mail that was fetched back into backlog. `since`
    earlier than that floor is moved up to it, and the answer says so (`capped`,
    `requested_since`); `days` is refused past it, since its whole range is known up front."""
    retention_days = max(1, call_retention_secs() // 86400)
    max_days = max(1, retention_days - 1)
    floor = as_of - _dt.timedelta(days=max_days)
    raw_since, raw_days = request.GET.get("since"), request.GET.get("days")
    if raw_since is not None and raw_days is not None:
        raise errors.ApiError(422, "E_VALIDATION", "since and days cannot be used together")
    applied: dict = {"retention_days": retention_days, "capped": False}
    if raw_since is not None:
        try:
            since = datetime.fromisoformat(raw_since[:-1] + "+00:00"
                                           if raw_since.endswith("Z") else raw_since)
        except ValueError:
            since = None
        if since is None or since.tzinfo is None or since.utcoffset() is None:
            raise errors.ApiError(422, "E_VALIDATION",
                                  "since must be an ISO-8601 timestamp with timezone offset or Z")
        applied["days"] = None
        if since < floor:
            applied["capped"], applied["requested_since"] = True, raw_since
            since = floor
    else:
        try:
            # The DEFAULT is clamped to the maximum, so a plain call never fails on a short
            # retention (Codex on #111: at MESHYCHAT_CALL_RETENTION_DAYS=7 it always did). An
            # explicit `days` past the maximum is still refused below.
            days = min(UNREAD_DEFAULT_DAYS, max_days) if raw_days is None else int(raw_days)
        except ValueError:
            raise errors.ApiError(422, "E_VALIDATION", f"days must be an integer within 1..{max_days}")
        if not 1 <= days <= max_days:
            raise errors.ApiError(422, "E_VALIDATION", f"days must be within 1..{max_days}")
        since = as_of - _dt.timedelta(days=days)
        applied["days"] = days
    raw_limit = request.GET.get("limit")
    try:
        limit = 100 if raw_limit is None else int(raw_limit)
    except ValueError:
        raise errors.ApiError(422, "E_VALIDATION", "limit must be an integer within 1..500")
    if not 1 <= limit <= 500:
        raise errors.ApiError(422, "E_VALIDATION", "limit must be within 1..500")
    applied["since"], applied["limit"] = since.isoformat(), limit
    return since, applied


#: The unread query's own statement bound, in ms (R6, oxxxxxx). On 8 Nov 2026 an 89-day window for a busy seat
#: (kxxx) ran into the server's 30 s statement timeout twice, and the route answered 500. The query now runs in a
#: savepoint under this bound, and past it the route answers 200 with `timed_out` true, `truncated` true, no items and
#: a null `total`, as the context search's lexical arm degrades (apps/meshsem/ctxsearch.py). A shorter window is the
#: remedy. A client that trusts the list only when it is complete (reconcile's unread gate, meshy-mod's digest, which
#: needs an integer total) reads nothing from it.
UNREAD_TIMEOUT_MS = 10_000


def _with_unread_timeout(conn, run, *, what: str, since: _dt.datetime):
    """Run `run(conn)` in a savepoint under `UNREAD_TIMEOUT_MS`, or return None on QueryCanceled."""
    try:
        with conn.transaction():
            prev = None
            with conn.cursor() as cur:
                cur.execute("SELECT setting::int FROM pg_settings WHERE name = 'statement_timeout'")
                row = cur.fetchone()
                current = int(row[0]) if row is not None else 0
                if current <= 0 or current > UNREAD_TIMEOUT_MS:
                    prev = max(0, current)
                    cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{int(UNREAD_TIMEOUT_MS)}ms",))
            result = run(conn)
            if prev is not None:
                with conn.cursor() as cur:
                    cur.execute("SELECT set_config('statement_timeout', %s, true)",
                                ("0" if prev == 0 else f"{prev}ms",))
            return result
    except psycopg.errors.QueryCanceled:
        logging.getLogger("meshychat.mesh").warning(
            "%s timed out after %d ms (window since %s)", what, UNREAD_TIMEOUT_MS, since.isoformat())
        return None


def _unread_bounded(conn, labels, *, peer_id, since, limit):
    """graph.unread under UNREAD_TIMEOUT_MS, or None when the query passed it. In a savepoint: a QueryCanceled rolls it
    back, and the SET LOCAL with it, so the request goes on. On success the request's own statement_timeout is put
    back, since a released savepoint would keep the lower bound for the rest of the request (as ctxsearch does). A
    bound already lower than UNREAD_TIMEOUT_MS is left as it is."""
    return _with_unread_timeout(
        conn,
        lambda c: graph.unread(c, labels, peer_id=peer_id, since=since, limit=limit),
        what=f"unread for peer {str(peer_id)[:8]}",
        since=since,
    )


def _fleet_unread_bounded(conn, peer_labels, *, since):
    """graph.fleet_unread_counts under UNREAD_TIMEOUT_MS, or None when the query passed it."""
    return _with_unread_timeout(
        conn,
        lambda c: graph.fleet_unread_counts(c, peer_labels, since=since),
        what=f"fleet_unread for {len(peer_labels)} peer(s)",
        since=since,
    )


def _peer_counts(conn, rlabels, pid, caller_labels, *, since, count_until):
    """One recipient peer's two counts for the recipients route, each under the unread bound in its own savepoint:
    (graph.unread result, unanswered_from or None), or None when the unread count is unknown. On 16 Nov 2026 one
    peer's count passing the server's statement timeout answered 500 for the whole call, 16 times in half an hour.
    Unknown is: the unread count passed its bound, or the call's budget for starting counts (`count_until`, a
    monotonic time) was already spent. The row then carries null, never 0."""
    if time.monotonic() >= count_until:
        return None
    result = _unread_bounded(conn, rlabels, peer_id=pid, since=since, limit=1)
    if result is None:
        return None
    # Divergence 084 (#141): the CALLER's sends to this peer it has not answered. The caller's own numbers, about
    # its own mail, so this widens no exposure. Null when it passed the bound, and null when the unread count just
    # made used up the call's budget: a second count started then could run its full bound past the route's time.
    if time.monotonic() >= count_until:
        return result, None
    unanswered = _with_unread_timeout(
        conn, lambda c: graph.unanswered_from(c, caller_labels, rlabels, since=since),
        what=f"unanswered_from for peer {str(pid)[:8]}", since=since)
    return result, unanswered


def _brief_awaiting(conn, answered, *, peer_id, since, limit):
    """The brief's awaiting rows (limit + 1 of them) and per-state counts under the unread bound, in one savepoint:
    (rows, counts), or None past the bound. On 16 Nov 2026 this read passed the server's statement timeout five
    times in half an hour and the whole brief answered 500. None makes the section null with `awaiting_timed_out`
    true: unknown, never an empty list."""
    return _with_unread_timeout(
        conn, lambda c: (graph.awaiting(c, answered, states=("open", "active"), since=since, limit=limit + 1),
                         graph.awaiting_counts(c, answered, since=since)),
        what=f"brief awaiting for peer {str(peer_id)[:8]}", since=since)


def _unread_box(result) -> dict:
    """The `unread` member of an answer: the list, or, when the query passed its bound, a cut that says so."""
    if result is None:
        return {"items": [], "total": None, "oldest_at": None, "truncated": True, "timed_out": True,
                "reason": f"the unread query passed its {UNREAD_TIMEOUT_MS // 1000} s bound for this window: "
                          "ask for a shorter one (days or since)"}
    return {"items": _jsonable(result.items), "total": result.total, "oldest_at": _jsonable(result.oldest_at),
            "truncated": result.truncated, "timed_out": False}


@_route("unread")
def unread(request, conn, auth):
    """GET /v1/mesh/unread: the caller's fetch backlog, mail addressed to it (to, not cc) whose
    head version it has not fetched with a completed memory.read (graph.unread says exactly
    what counts). Newest first, with the window's `total` and `oldest_at`. A fetch is not an
    action: a read here means the client fetched the message, not that anyone looked at it.
    The query has its own bound (UNREAD_TIMEOUT_MS): past it the answer says `timed_out`."""
    as_of = _dt.datetime.now(_dt.timezone.utc)
    since, applied = _unread_window(request, as_of)
    labels = labels_for(conn, auth.peer_id)
    result = _unread_bounded(conn, [x["label"] for x in labels], peer_id=auth.peer_id,
                             since=since, limit=applied["limit"])
    return JsonResponse({
        "label": labels[0]["label"], "label_source": labels[0]["source"], "labels": labels,
        "as_of": as_of.isoformat(), "filters": applied,
        "unread": _unread_box(result),
        "coverage": _coverage(conn),
    }, status=200)


#: The most labels one recipient-status call may ask about (divergence 083). A message's `to`
#: is short; a long list is a scan, not a send.
RECIPIENTS_MAX = 10
#: The route's own bound, since it takes no CALLS token: calls per peer per minute, in this process.
#: An honest client caches answers for a minute, so it makes about one call per recipient set.
RECIPIENTS_PER_MINUTE = 30
#: At most this many peers are tracked; when full, peers idle for a minute are dropped, and if it is still
#: full a NEW peer is refused for a minute (the active peers' histories are never discarded).
RECIPIENTS_TRACKED_MAX = 4096
#: Seconds one recipients call may spend starting per-peer counts. Each count has its own bound (UNREAD_TIMEOUT_MS),
#: so the counting ends at most that bound after this budget; the peers not reached read as unknown.
RECIPIENTS_COUNT_BUDGET_S = 15.0
_recipients_seen: dict[str, list[float]] = {}
_recipients_lock = threading.Lock()
_owed_key_seen: dict[str, list[float]] = {}
_owed_key_lock = threading.Lock()


def _recipients_admit(peer_id) -> None:
    """Refuse (429) a peer's RECIPIENTS_PER_MINUTE+1th call within 60 s. A fixed bound per process,
    so a peer cannot use the tokenless route as an unmetered scan. The read, the check and the
    write are ONE critical section: unlocked, concurrent requests each saw room and all got in
    (Codex on #145 measured 8 extra admissions from 29)."""
    now = time.monotonic()
    key = str(peer_id)
    with _recipients_lock:
        if key not in _recipients_seen and len(_recipients_seen) >= RECIPIENTS_TRACKED_MAX:
            # Full: drop only peers idle for a minute (their histories hold nothing that still counts). Never
            # clear the table: that would forget the peers being refused right now (Codex on #145).
            for k in [k for k, ts in _recipients_seen.items() if not ts or now - ts[-1] >= 60.0]:
                del _recipients_seen[k]
            if len(_recipients_seen) >= RECIPIENTS_TRACKED_MAX:
                raise peer_limit.PeerRateLimited(key, "mesh", "recipients", 60)   # a NEW peer waits; no one is forgotten
        seen = [t for t in _recipients_seen.get(key, ()) if now - t < 60.0]
        if len(seen) >= RECIPIENTS_PER_MINUTE:
            _recipients_seen[key] = seen
            retry = max(1, int(60.0 - (now - seen[0])) + 1)
        else:
            seen.append(now)
            _recipients_seen[key] = seen
            return
    raise peer_limit.PeerRateLimited(key, "mesh", "recipients", retry)


def _owed_key_admit(peer_id) -> None:
    """The same per-process bound as _recipients_admit, on its own table, for the non-spending owed/key route.
    A sibling copy rather than a shared helper taking the table as a parameter: tests/test_bounded_caches.py
    sees a cache only where it is grown and capped BY NAME, and a parameter hides it (its LIMITS)."""
    now = time.monotonic()
    key = str(peer_id)
    with _owed_key_lock:
        if key not in _owed_key_seen and len(_owed_key_seen) >= RECIPIENTS_TRACKED_MAX:
            for k in [k for k, ts in _owed_key_seen.items() if not ts or now - ts[-1] >= 60.0]:
                del _owed_key_seen[k]
            if len(_owed_key_seen) >= RECIPIENTS_TRACKED_MAX:
                raise peer_limit.PeerRateLimited(key, "mesh", "owed_key", 60)   # a NEW peer waits; no one is forgotten
        seen = [t for t in _owed_key_seen.get(key, ()) if now - t < 60.0]
        if len(seen) >= RECIPIENTS_PER_MINUTE:
            _owed_key_seen[key] = seen
            retry = max(1, int(60.0 - (now - seen[0])) + 1)
        else:
            seen.append(now)
            _owed_key_seen[key] = seen
            return
    raise peer_limit.PeerRateLimited(key, "mesh", "owed_key", retry)


def peers_answering(conn, label: str) -> list[str]:
    """The active peers `label` reaches: exactly those whose labels_for lists it (roster label,
    uncontested peers.name, or the peer-<id8> fallback), so this is the inverse of labels_for and
    cannot disagree with what inbox and unread answer each peer for."""
    candidates: set[str] = {pid for pid, lab in handlers.roster_labels().items() if lab == label}
    with conn.cursor() as cur:
        cur.execute("SELECT id::text FROM peers WHERE status = 'active' AND name = %s", (label,))
        candidates.update(r[0] for r in cur.fetchall())
        if label.startswith("peer-") and len(label) == 13:
            cur.execute("SELECT id::text FROM peers WHERE status = 'active' AND left(id::text, 8) = %s",
                        (label[5:],))
            candidates.update(r[0] for r in cur.fetchall())
        if candidates:
            cur.execute("SELECT id::text FROM peers WHERE status = 'active' AND id = ANY(%s::uuid[])",
                        (sorted(candidates),))
            active = {r[0] for r in cur.fetchall()}
        else:
            active = set()
    return sorted(p for p in active if any(x["label"] == label for x in labels_for(conn, p)))


def _online_of(conn, peer_ids) -> dict[str, bool]:
    """{peer_id: holds a live socket now}, in one statement, by hub_presence's own rule (`online_condition`).
    `conn` is inside server_internal already (the request's), which the store's table requires."""
    # The read by name, as apps/calls/heartbeat.py takes it: importing the store module itself would reach its
    # writes, which tests/test_hub_presence.py refuses outside the CallBridge.
    from apps.wsgw.hub_presence import ONLINE_WINDOW_SECS, online_condition

    ids = [str(p) for p in peer_ids]
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT p::text, " + online_condition("p") + " FROM unnest(%s::uuid[]) AS p",
                    (ONLINE_WINDOW_SECS, ids))
        return {pid: bool(on) for pid, on in cur.fetchall()}


@_route("recipients", spends_calls=False)
def recipients(request, conn, auth):
    """GET /v1/mesh/recipients?to=a,b: for each label a sender is about to write to, the peers it
    reaches, each with its presence (effective_state, note, seconds since its last beat) and its
    unread count and oldest age over the default unread window (divergence 083, issue #140).
    COUNTS ONLY: no key, sender or text. Optional detail=work adds windowed last-send/indexed timestamps,
    fourteen-day owed/awaiting counts and up to two recipient labels per peer, held for 60 s. With detail absent,
    the one extra read is `active` / `active_sent_at` (10 Nov 2026, f38d28 item 5): ONE grouped statement for every
    peer of the call (work.active_sent), over the last 30 minutes of msg/ keys, in its own savepoint so a failure
    reads null. Open to any peer that may read shared/mesh-chat, as the
    presence list is open to every peer; the send path calls it to WARN, never to refuse.

    `online` (2 Nov 2026) is MEASURED, not published: whether any worker holds a websocket for that peer that it beat
    for within hub_presence.ONLINE_WINDOW_SECS (30 s), the rule call routing uses (`hub_presence.is_online`). The
    published `effective_state` is what a seat SAID and can stay `active` for months when it was beaten without a
    ttl (production, 2 Nov: 75 and 87 days); `online` is the listener's own socket. It is read through
    `online_condition`, the store's sanctioned read for a statement inside server_internal (is_online opens its own
    server_internal, which refuses to nest inside this request's). One bit per peer, the same audience as presence."""
    from apps.peers import presence as peer_presence

    _recipients_admit(auth.peer_id)
    detail = request.GET.get("detail")
    if detail not in (None, "work"):
        raise errors.ApiError(422, "E_VALIDATION", "detail must be work")
    raw = request.GET.get("to") or ""
    labels = [x.strip() for x in raw.split(",") if x.strip()]
    if not labels or len(labels) > RECIPIENTS_MAX or any(len(x) > 200 for x in labels):
        raise errors.ApiError(422, "E_VALIDATION",
                              f"to must name 1..{RECIPIENTS_MAX} labels, each at most 200 characters")
    as_of = _dt.datetime.now(_dt.timezone.utc)
    max_days = max(1, max(1, call_retention_secs() // 86400) - 1)
    since = as_of - _dt.timedelta(days=min(UNREAD_DEFAULT_DAYS, max_days))
    out = []
    caller_labels = [x["label"] for x in labels_for(conn, auth.peer_id)]
    from . import work as _work
    answering = {label: peers_answering(conn, label) for label in dict.fromkeys(labels)}
    # MEASURED activity (f38d28 item 5), ONE statement for every peer of this call (pxx P2-1)
    sent = _work.active_sent(conn, [pid for ids in answering.values() for pid in ids])
    count_until, unknown_peers, budget_spent = time.monotonic() + RECIPIENTS_COUNT_BUDGET_S, 0, False
    for label, peer_ids in answering.items():
        states = peer_presence.states_of(conn, peer_ids)
        online = _online_of(conn, peer_ids)
        peers = []
        for pid in peer_ids:
            rlabels = [x["label"] for x in labels_for(conn, pid)]
            counted = _peer_counts(conn, rlabels, pid, caller_labels, since=since, count_until=count_until)
            if counted is None:
                unknown_peers, budget_spent = unknown_peers + 1, budget_spent or time.monotonic() >= count_until
            result, unanswered = counted if counted is not None else (None, None)
            st = states.get(pid, {"effective_state": "unknown", "note": None, "beat_age_secs": None,
                                  "state_expires_at": None})
            # MEASURED activity (f38d28 item 5): what a seat did, beside what it said. The dashboard's #575 rule.
            is_active, sent_at = _work.measured_active(sent, pid, st)
            extra = {}
            if detail == "work":
                from . import work
                # Oxxx confirmed counts/labels to this audience in signed f0bdf1 (Kxxx 67804f).
                # A failed optional read must not discard the ordinary recipient row or poison its transaction.
                try:
                    with conn.transaction():
                        extra = work.snapshot(conn, pid, rlabels)
                except Exception as exc:  # noqa: BLE001 - optional facts unavailable, never a fabricated zero
                    log.warning("fleet work unavailable peer=%s error=%s", pid, type(exc).__name__)
                    extra = {"last_sent_at": None, "indexed_turn_at": None, "work_as_of": None,
                             "owed": None, "awaiting": None}
            peers.append({"peer_id": pid, **st, **extra, "online": online.get(str(pid), False),
                          "active": is_active,
                          "active_sent_at": None if sent_at is None else sent_at.isoformat(),
                          "unread_count": None if result is None else result.total,
                          "unread_timed_out": result is None,
                          "unanswered_from_you": unanswered,
                          "unread_oldest_age_secs": (None if result is None or result.oldest_at is None else
                                                     max(0, int((as_of - result.oldest_at).total_seconds())))})
        out.append({"label": label, "peers": peers})
    body = {"as_of": as_of.isoformat(), "unread_since": since.isoformat(), "recipients": out,
            "unread_unknown_peers": unknown_peers, "unread_budget_spent": budget_spent}
    if detail == "work":
        from . import work
        body["work"] = {"counts_scope": "recipients", "held_seconds": 60,
                        "ask_days": work.ASK_DAYS, "sent_days": work.SENT_DAYS}
    return JsonResponse(body, status=200)


#: The peer-JWT scope that opens the fleet counts route and nothing else (divergence 081). The
#: exporter peer holds it and no grant on shared/mesh-chat, so every other mesh route refuses it
#: at the read gate. Set only by the operator, in peers.scopes (sql/104: no API write path).
BACKLOG_SCOPE = "mesh:backlog:read"

#: The most peers one fleet counts answer covers; past it the answer says `truncated`.
#: Divergence 081 records the latency this was measured at.
FLEET_PEER_CAP = 200


def _is_bootstrap_authority(peer_id) -> bool:
    """MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID, read at call time as apps/meshsum/admission.py
    reads it. Unset fails closed: no peer is the authority."""
    authority = (handlers.BOOTSTRAP_AUTHORITY_PEER_ID or "").strip()
    return bool(authority) and str(peer_id) == authority


def _holds_scope_now(conn, auth, scope: str) -> bool:
    """BOTH the token's claim AND the peer's current peers.scopes row (kxxx,
    msg/<key>). The column alone would let a token minted before a grant use
    it; the claim alone would outlive a revocation by up to the token's hour. `conn` is inside
    server_internal."""
    if scope not in (auth.scopes or ()):
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT scopes FROM peers WHERE id = %s AND status = 'active'",
                    (str(auth.peer_id),))
        row = cur.fetchone()
    return row is not None and scope in (row[0] or ())


def _operator_route(route_name: str, *, scope: str | None):
    """The fleet routes' wrapper (divergence 081). It is _route's sequence, bearer auth, the
    peer's calls bucket, one server_internal transaction and the usage row, with the read gate
    replaced: the caller must be the bootstrap authority, or, where `scope` is given, hold that
    scope in its token and on its peer row now. No shared/mesh-chat grant is asked for or
    implied, so the exporter that holds the scope reads nothing else."""
    def decorate(handler):
        def wrapper(request, *args, **kwargs):
            if request.method != "GET":
                return HttpResponse(status=405, headers={"Allow": "GET"})
            try:
                with dbconn.serving_connection() as conn:
                    auth = authenticate_peer(conn, request)
                    peer_limit.admit(auth.peer_id, transport="mesh")
                    outcome = "error"
                    try:
                        with dbconn.request_server_internal(conn) as c:
                            allowed = _is_bootstrap_authority(auth.peer_id) or (
                                scope is not None and _holds_scope_now(c, auth, scope))
                            if not allowed:
                                outcome = "denied"
                                if scope is None:
                                    refusal = errors.ApiError(
                                        403, "E_NOT_BOOTSTRAP_AUTHORITY",
                                        "only the bootstrap authority may read this route")
                                else:
                                    refusal = errors.ApiError(
                                        403, "E_SCOPE_REQUIRED",
                                        f"this route needs the bootstrap authority or the {scope} "
                                        f"scope, in the token and on the peer")
                                return JsonResponse(refusal.body(), status=refusal.status)
                            response = handler(request, c, auth, *args, **kwargs)
                            outcome = "ok"
                            return response
                    finally:
                        usage.record(conn, route_name, auth.peer_id, outcome)
            except peer_limit.PeerRateLimited as exc:
                return peer_limit.http_response(exc)
            except errors.ApiError as exc:
                return JsonResponse(exc.body(), status=exc.status)
        wrapper.__name__ = getattr(handler, "__name__", "mesh_operator_route")
        return csrf_exempt(wrapper)
    return decorate


@_operator_route("fleet_unread", scope=BACKLOG_SCOPE)
def fleet_unread(request, conn, auth):
    """GET /v1/mesh/fleet/unread: every candidate peer's fetch backlog as counts (divergence 081).

    Per peer: its primary label, peer id, the count of addressed mail it has not fetched, and the
    oldest such message's time and age. No keys, senders or text. Each count is `unread()` with
    that peer's own labels and the same window rules, so it is the number that peer's own
    GET /v1/mesh/unread reports. The peers covered are graph.fleet_candidates': who wrote mesh
    mail in the window, who is on the roster, and who an addressed label can name. A fetch is not
    an action: a read here means the peer's client fetched the message."""
    if "limit" in request.GET:
        raise errors.ApiError(422, "E_VALIDATION", "limit does not apply to the fleet counts")
    as_of = _dt.datetime.now(_dt.timezone.utc)
    since, applied = _unread_window(request, as_of)
    applied.pop("limit", None)
    cand = graph.fleet_candidates(conn, since=since, roster=handlers.roster_labels())
    covered = cand.peers[:FLEET_PEER_CAP]
    labels_by_peer = labels_for_peers(conn, covered)
    answered = {x["label"] for labels in labels_by_peer.values() for x in labels}
    peer_labels = [(pid, [x["label"] for x in labels_by_peer[pid]]) for pid in covered]
    counts = _fleet_unread_bounded(conn, peer_labels, since=since)
    sources = {"addressed": len(cand.addressed), "senders": len(cand.senders),
               "roster": len(cand.roster)}
    if counts is None:
        return JsonResponse({
            "as_of": as_of.isoformat(), "filters": applied,
            "fleet": {
                "peers": [], "candidates": len(cand.peers), "cap": FLEET_PEER_CAP,
                "truncated": True, "timed_out": True,
                "reason": f"the fleet unread query passed its {UNREAD_TIMEOUT_MS // 1000} s bound for this window: "
                          "ask for a shorter one (days or since)",
                "sources": sources, "unanswered_labels": None,
            },
            "coverage": _coverage(conn),
        }, status=200)
    rows = []
    for peer_id in covered:
        labels = labels_by_peer[peer_id]
        result = counts[peer_id]
        rows.append({
            "peer_id": peer_id, "label": labels[0]["label"], "label_source": labels[0]["source"],
            "count": result.total, "oldest_at": _jsonable(result.oldest_at),
            "oldest_age_secs": (None if result.oldest_at is None
                                else max(0, int((as_of - result.oldest_at).total_seconds()))),
            "addressed": peer_id in cand.addressed,
        })
    truncated = len(cand.peers) > len(covered)
    return JsonResponse({
        "as_of": as_of.isoformat(), "filters": applied,
        "fleet": {
            "peers": rows, "candidates": len(cand.peers), "cap": FLEET_PEER_CAP,
            "truncated": truncated, "timed_out": False,
            "sources": sources,
            # Addressed labels no covered peer answers for: broadcasts, retired names. Only
            # meaningful when every candidate was covered.
            "unanswered_labels": (None if truncated
                                  else len(cand.addressed_labels - answered)),
        },
        "coverage": _coverage(conn),
    }, status=200)


@_operator_route("fleet_unread_peer", scope=None)
def fleet_unread_peer(request, conn, auth, peer_id):
    """GET /v1/mesh/fleet/unread/<peer_id>: one peer's fetch backlog, as that peer's own
    GET /v1/mesh/unread answers it: key, version, sender (author label), the label it was
    addressed to, and the version's time. Never the text. The bootstrap authority only, on
    demand (divergence 081): an unattended job never holds this."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM peers WHERE id = %s AND status = 'active'", (str(peer_id),))
        if cur.fetchone() is None:
            raise errors.ApiError(404, "E_NOT_FOUND", "no active peer has that id")
    as_of = _dt.datetime.now(_dt.timezone.utc)
    since, applied = _unread_window(request, as_of)
    labels = labels_for(conn, peer_id)
    result = _unread_bounded(conn, [x["label"] for x in labels], peer_id=peer_id,
                             since=since, limit=applied["limit"])
    return JsonResponse({
        "peer_id": str(peer_id), "label": labels[0]["label"],
        "label_source": labels[0]["source"], "labels": labels,
        "as_of": as_of.isoformat(), "filters": applied,
        "unread": _unread_box(result),
        "coverage": _coverage(conn),
    }, status=200)


@_route("mentions")
def mentions(request, conn, auth):
    """GET /v1/mesh/mentions?ref=...&limit=N: messages naming one artifact reference.

    `&by=peer` (divergence 090, issue #159 phase 2) answers who cites it instead: the same hits grouped by the peer
    that signed each row (graph.custody), each holder with its labels, counts and newest keys. Same route, same
    gate and bound, so it is counted as a `mentions` read and needs no new route in the usage CHECK."""
    ref = request.GET.get("ref")
    raw_limit = request.GET.get("limit")
    by = request.GET.get("by")
    if by not in (None, "peer"):
        raise errors.ApiError(422, "E_VALIDATION", "by must be `peer` when given")
    try:
        limit = 50 if raw_limit is None else int(raw_limit)
        if not 1 <= limit <= 100:
            raise ValueError
        from apps.meshsem import store as sem_store
        if by == "peer":
            held = graph.custody(conn, ref or "", limit=limit, gate=sem_store.forbidden_rows_open())
        else:
            result = graph.mentions(conn, ref or "", limit=limit, gate=sem_store.forbidden_rows_open())
    except ValueError:
        raise errors.ApiError(422, "E_VALIDATION", "ref must be a canonicalizable artifact reference and limit must be within 1..100")
    if by == "peer":
        return JsonResponse({"ref": held.ref, "by": "peer", "holders": _jsonable(held.holders),
                             "hits_read": held.hits_read, "truncated": held.truncated,
                             "also_matched": held.also_matched, "coverage": _coverage(conn)}, status=200)
    return JsonResponse({"ref": result.ref, "hits": _jsonable(result.hits),
                         "truncated": result.truncated, "also_matched": result.also_matched,
                         "coverage": _coverage(conn)}, status=200)


#: The message fields an envelope carries: what a reader needs to follow a thread without one read per key.
ENVELOPE_FIELDS = ("from", "to", "cc", "type", "thread_id", "in_reply_to", "closes", "declines", "refines",
                   "body", "ts", "subject", "ref", "requires_ack", "done_when", "attachments", "redacted")
PROVENANCE_LIMIT_MAX = 100


def _envelopes(conn, request, auth, keys) -> tuple[dict, str | None]:
    """({key: envelope} for the keys this reader may read, why any were withheld). The gate is memory.read's own
    (`body_withheld_many`, the addressed wait's preview gate), so an envelope shows exactly what a read would, and a
    reader with no DPoP proof (when DPoP is enforced) gets none. An envelope is NOT a read: nothing is marked read
    for unread or owed (audit finding 9a; a reviewer 0ae861)."""
    from apps.wsgw import mesh_notify

    reader = _preview_reader(conn, request, auth)
    if not getattr(reader, "possession", False):
        return {}, "no_proof"
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}, None
    gate = mesh_notify.preview_gates(conn, reader, keys)
    readable = [k for k in keys if gate.get(k) is None]
    out: dict = {}
    if readable:
        with conn.cursor() as cur:
            cur.execute("SELECT key, version, value FROM shared_memory "
                        "WHERE namespace = 'shared/mesh-chat' AND is_head AND key = ANY(%s)", (readable,))
            for key, version, value in cur.fetchall():
                if isinstance(value, dict):
                    env = {f: value[f] for f in ENVELOPE_FIELDS if f in value}
                    env["version"] = version
                    # memory.edit keeps its record outside the value, and a later version of a mesh message is
                    # written only by memory.edit (divergence 059), so the head version says whether it was edited
                    # (GitHub Codex at views.py:942, pxx P2-2).
                    env["edited"] = isinstance(version, int) and version > 1
                    out[key] = env
    withheld = sorted({gate[k] for k in keys if gate.get(k) is not None})
    return out, (",".join(str(w) for w in withheld) if withheld else None)


def _author_jwks(conn, peer_ids: list[str]) -> dict[str, dict]:
    """Active Ed25519 JWKS per author peer id (matching GET /agents/{peer}/jwks.json: active key only,
    empty `keys` list when none or undecodable), fetched in a single indexed query."""
    from apps.peers import cards as peer_cards
    from apps.peers import errors as peer_errors
    from apps.peers import keys as peer_keys

    unique_ids = list(dict.fromkeys(pid for pid in peer_ids if isinstance(pid, str) and pid))
    if not unique_ids:
        return {}
    out: dict[str, dict] = {pid: {"keys": []} for pid in unique_ids}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (peer_id) peer_id::text, kid, public_key FROM peer_keys "
            "WHERE peer_id = ANY(%s::uuid[]) AND active = TRUE AND revoked_at IS NULL "
            "ORDER BY peer_id, created_at DESC, id DESC",
            (unique_ids,),
        )
        for pid, kid, pub_b64u in cur.fetchall():
            try:
                entries = [(peer_keys.decode_public_key(pub_b64u), kid)]
            except peer_errors.ApiError:
                entries = []
            out[str(pid)] = peer_cards.build_jwks(entries)
    return out


def _attach_provenance(conn, envs: dict) -> dict[str, dict]:
    """Attach `created_by_peer_id`, `provenance`, and `provenance_memo` (carrying exact stored `value` and
    any `edited_at` from `shared_memory_edit` for offline `wire.author_signature_ok`) onto readable envelopes,
    and return `{peer_id: jwks}` for every writer on the page."""
    if not envs:
        return {}
    readable = list(envs)
    rows_by_key: dict[str, tuple] = {}
    edited_ids: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, key, version, value, created_by_peer_id::text, provenance_jsonb "
            "FROM shared_memory WHERE namespace = 'shared/mesh-chat' AND is_head AND key = ANY(%s)",
            (readable,),
        )
        ver_by_id: dict[str, int] = {}
        for mid, key, version, value, peer_id, prov in cur.fetchall():
            rows_by_key[key] = (mid, int(version), value, peer_id, prov)
            if int(version) > 1:
                edited_ids.append(mid)
                ver_by_id[str(mid)] = int(version)
        edited_at_by_id: dict[str, str] = {}
        if edited_ids:
            # Match apps/memoryskills/handlers.py:1372 (handle_read on head row id) and sql_meshychat/029's
            # (memory_id, namespace, key, version) binding with deterministic ORDER BY.
            cur.execute(
                "SELECT DISTINCT ON (e.memory_id) e.memory_id::text, e.version, "
                "to_char(e.edited_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') "
                "FROM shared_memory_edit e "
                "WHERE e.namespace = 'shared/mesh-chat' AND e.memory_id = ANY(%s::uuid[]) "
                "ORDER BY e.memory_id, e.version DESC, e.id DESC",
                (edited_ids,),
            )
            for mid, edit_ver, edited_at in cur.fetchall():
                if edited_at and ver_by_id.get(str(mid)) == int(edit_ver):
                    edited_at_by_id[str(mid)] = edited_at

    peer_ids: list[str] = []
    for key, env in envs.items():
        got = rows_by_key.get(key)
        if got is None:
            continue
        mid, ver, value, peer_id, prov = got
        edited_at = edited_at_by_id.get(mid)
        memo = {
            "namespace": "shared/mesh-chat",
            "key": key,
            "version": ver,
            "value": value,
            "created_by_peer_id": peer_id,
            "provenance": prov,
        }
        if edited_at is not None:
            memo["edited_at"] = edited_at
        env["created_by_peer_id"] = peer_id
        if edited_at is not None:
            env["edited_at"] = edited_at
        env["provenance_memo"] = memo
        if peer_id:
            peer_ids.append(peer_id)
    return _author_jwks(conn, peer_ids)


@_route("thread")
def thread(request, conn, auth, thread_id):
    """GET /v1/mesh/threads/<thread_id>[?after=<msg key>][&envelope=1][&provenance=1][&order=stream|causal][&limit=N]:
    the thread, whichever spelling filed each row, oldest first (or causal reply-tree order when `order=causal`).
    Page cap (`limit`): unbounded by default (`next_after` pages when `?limit=N` cuts a page); when `provenance=1`,
    `limit` defaults to and is capped at `PROVENANCE_LIMIT_MAX` (100) because `provenance=1` roughly doubles
    per-message wire bytes (carrying raw `provenance_memo.value` alongside the projected `envelope`)."""
    limit = _limit(request, None)
    after = request.GET.get("after")
    if after is not None:
        after = after.strip()
        if not (after.startswith("msg/") or after.startswith("pos:")) or len(after) > 512:
            raise errors.ApiError(422, "E_VALIDATION", "after must be a next_after position or a msg/ key")
        # Inferred rows are not in the declared order, so a page of the merged list has no position to resume
        # from (pxx P2-3): paging and the inferred tier are not combined.
        if inferred_reads.include_inferred(request):
            raise errors.ApiError(422, "E_VALIDATION", "after cannot be combined with include_inferred=1")
    raw_order = request.GET.get("order")
    order = "stream" if raw_order is None else raw_order.strip()
    if order not in ("stream", "causal"):
        raise errors.ApiError(422, "E_VALIDATION", "order must be stream or causal")
    if order == "causal" and inferred_reads.include_inferred(request):
        raise errors.ApiError(422, "E_VALIDATION", "order=causal cannot be combined with include_inferred=1")
    want_env = request.GET.get("envelope") in ("1", "true")
    raw_prov = request.GET.get("provenance")
    if raw_prov not in (None, "0", "false", "1", "true"):
        raise errors.ApiError(422, "E_VALIDATION", "provenance must be 0, 1, false, or true")
    want_prov = raw_prov in ("1", "true")
    if want_prov and not want_env:
        raise errors.ApiError(422, "E_VALIDATION", "provenance=1 requires envelope=1")
    if want_prov:
        if request.GET.get("limit") is not None and (limit is None or limit > PROVENANCE_LIMIT_MAX):
            raise errors.ApiError(422, "E_VALIDATION", f"provenance=1 limit must be within 1..{PROVENANCE_LIMIT_MAX}")
        if limit is None:
            limit = PROVENANCE_LIMIT_MAX
    try:
        result, next_after = graph.thread_page(conn, thread_id, limit=limit, after=after)
    except graph.UnknownAfter:
        raise errors.ApiError(422, "E_VALIDATION", "after names no message or position of this thread")
    tiered = inferred_reads.for_thread(request, conn, result, thread_id=thread_id,
                                       limit=limit)
    hits = _jsonable(tiered.hits)
    unresolved_refs: list[dict] | None = None
    if order == "causal":
        hits, unresolved_refs = graph.causal_order(conn, hits)
    body = {
        "thread_id": thread_id, "hits": hits,
        "truncated": tiered.truncated, "coverage": _coverage(conn),
        "inferred": _jsonable(tiered.inferred),
    }
    if raw_order is not None:
        body["order"] = order
    if unresolved_refs is not None:
        body["unresolved_refs"] = unresolved_refs
    # Only for the declared order alone: with inferred rows mixed in, the page is not a prefix of that order.
    if next_after is not None and not inferred_reads.include_inferred(request):
        body["next_after"] = next_after
    if want_env:
        envs, why = _envelopes(conn, request, auth, [h["key"] for h in hits if isinstance(h, dict) and "key" in h])
        if want_prov:
            body["author_jwks"] = _attach_provenance(conn, envs)
        for h in hits:
            if isinstance(h, dict) and h.get("key") in envs:
                h["envelope"] = envs[h["key"]]
        body["envelope_withheld"] = why
    return JsonResponse(body, status=200)


@_route("drift")
def drift(request, conn, auth):
    """GET /v1/mesh/drift: edge_type -> src_field -> count, and per-route usage."""
    return JsonResponse({"drift": graph.drift(conn), "coverage": _coverage(conn),
                         "projection": _jsonable(projection.state(conn)),
                         "usage": usage.summary(conn)}, status=200)


@_route("neighbours")
def neighbours(request, conn, auth):
    """GET /v1/mesh/neighbours?key=msg/...[&include_inferred=0|1][&limit=N]."""
    from apps.meshsem import related_reads, store as sem_store

    key = (request.GET.get("key") or "").strip()
    if not key.startswith("msg/"):
        raise errors.ApiError(422, "E_VALIDATION", "key must be a msg/ key")
    include = inferred_reads.include_inferred(request)
    raw = request.GET.get("limit")
    limit = related_reads.LIMIT_DEFAULT
    if raw is not None:
        try:
            limit = int(raw)
        except ValueError:
            raise errors.ApiError(422, "E_VALIDATION", "limit must be an integer")
        if not 1 <= limit <= related_reads.LIMIT_MAX:
            # Refused, not clamped: a clamp returns fewer than asked with nothing saying so.
            raise errors.ApiError(422, "E_VALIDATION",
                                  f"limit must be within 1..{related_reads.LIMIT_MAX}")
    # The space matters only to the inferred tier. A declared answer reads no vector and no
    # calibration, so an unknown configured space must not take it down with the rest.
    space = None
    if include:
        try:
            space = sem_store.configured_space()
        except KeyError as exc:
            raise errors.ApiError(503, "E_SPACE_UNKNOWN", str(exc))
    try:
        body = related_reads.neighbours(conn, space, key, include_inferred=include,
                                        limit=limit,
                                        gate=sem_store.forbidden_rows_open())
    except related_reads.AnchorNotFound:
        raise errors.ApiError(404, "E_NOT_FOUND",
                              f"no head message {key!r} in {MESH_CHAT_NAMESPACE}")
    body["coverage"] = _coverage(conn)
    return _record_as(JsonResponse(body, status=200),
                      "neighbours_notice" if request.GET.get("via") == "notice" else "neighbours")


# --------------------------------------------------------------------------- #
# The long-poll, divergence 018 (c). The contract is in apps/mesh/wait.py.
# --------------------------------------------------------------------------- #

def _no_content(visible: int) -> HttpResponse:
    response = HttpResponse(status=204)
    # A bare 204 carries no content type; Django adds one.
    del response["Content-Type"]
    response[mesh_wait.VISIBLE_NAMESPACES_HEADER] = str(visible)
    return response


def _mesh_read_refusal(c, peer_id) -> JsonResponse | None:
    """The read gate's typed 403, or None when the caller may read; `c` is already inside
    `server_internal`. Divergence 031: the addressed wait answers shared/mesh-chat's set, so
    it is gated like the graph routes, and both call THIS (`_route` and `wait`): one check,
    one typed 403. A HandlerError that is not a denial propagates."""
    try:
        require_mesh_read(c, peer_id)
    except HandlerError as exc:
        if exc.code not in _DENIED:
            raise
        return JsonResponse({"error": exc.code, "message": exc.message}, status=403)
    return None


def _preview_reader(conn, request, auth):
    """Who asks, for the addressed wait's preview (divergence 033): the context route's
    reader (`meshsem.views.reader_for_request`: a DPoP proof for THIS GET, checked and
    spent, when DPoP is enforced), except that a jti-ledger FAULT (its 5xx) withholds the
    preview instead of failing the answer (review F2 of 44640f2). The context route raises
    that fault because there the body is the answer; here the answer is the frames, which the
    wait gives without any proof, and the preview is the only thing the proof is for. A 4xx never
    reaches here: `_reader` reads every refusal as "no proof"."""
    from apps.meshsem.search import Reader
    from apps.meshsem.views import reader_for_request

    try:
        return reader_for_request(conn, request, auth)
    except errors.ApiError as exc:
        if exc.status < 500:
            raise
        log.warning("addressed wait: DPoP ledger fault for %s, preview withheld: %s",
                    auth.peer_id, exc)
        return Reader(peer_id=auth.peer_id, possession=False)


def _addressed_answer(peer_id, read: mesh_wait.AddressedRead, eff: int) -> HttpResponse:
    """Divergence 031. The first read's frames, or a park for some, then 200 with them or
    204 naming the position the wait read up to. `eff` 0 never parks. A read that spent
    its scan budget on rows for others answers 200 with no messages, at once: "go on from
    `cursor`", never a 204 that would mean "nothing left". Both name, in RESUME_HEADER,
    where a caller that stops here resumes: since STATUS E13, for a caller on the commit
    order, the position itself (apps/mesh/wait.py), which no later commit can land behind;
    for a caller that did not opt in to it, the position held back to the settle horizon, as
    before E13 (`AddressedRead.resume`)."""
    frames, position, more = read.frames, read.position, read.more
    if not frames and not more and eff > 0:
        frames, position, more = mesh_wait.park_addressed(peer_id, position, read.labels,
                                                          eff, reader=read.reader)
    if frames or more:
        response = HttpResponse(mesh_wait.addressed_body(frames, position), status=200,
                                content_type="application/json")
        response[mesh_wait.VISIBLE_NAMESPACES_HEADER] = str(read.visible)
    else:
        response = _no_content(read.visible)
        response[mesh_wait.CURSOR_HEADER] = mesh_wait.cursor_wire(position)
    response[mesh_wait.RESUME_HEADER] = mesh_wait.cursor_wire(read.resume(position))
    response[mesh_wait.ADDRESSED_HEADER] = "1"
    return response


@csrf_exempt
def wait(request):
    """GET /v1/mesh/wait?since=<cursor>&timeout=<secs>.

    The query string is parsed before the handler runs (400), then the waiter permit (503),
    authentication (401/403), and the effective timeout.
    `addressed=1` (divergence 031, apps/mesh/wait.py) adds the mesh read gate after
    authentication and reads even at an effective timeout of 0.
    """
    if request.method != "GET":
        return HttpResponse(status=405, headers={"Allow": "GET"})
    try:
        params = mesh_wait.parse_params(request.GET)
    except errors.ApiError as exc:
        return JsonResponse(exc.body(), status=exc.status)
    if not mesh_wait.try_acquire_waiter():
        return HttpResponse("too many waiters", status=503,
                            content_type="text/plain; charset=utf-8")
    peer_id = None
    outcome = "error"
    db_unreachable = False
    try:
        with dbconn.serving_connection() as conn:
            auth = authenticate_peer(conn, request)
            # B12 (the project's design log (not part of this release) 003): the peer's CALLS bucket, before the token's expiry
            # is read, the gate, the read and the park. Before `peer_id` is set, so a refusal
            # writes no usage row.
            peer_limit.admit(auth.peer_id, transport="mesh")
            peer_id = auth.peer_id
            # `claims.exp`. AuthenticatedPeer does not carry it and
            # apps/peers/auth.py is the shared verifier another slice owns, so the token
            # the verifier just accepted is decoded once more by the same function.
            exp = verify_token(auth.token)["exp"]
            eff = mesh_wait.effective_timeout(params.timeout, exp, int(time.time()))
            if params.addressed:
                with dbconn.request_server_internal(conn) as c:
                    refused = _mesh_read_refusal(c, peer_id)
                if refused is not None:
                    outcome = "denied"
                    return refused
                # Divergence 033: who asks, as the context route reads it (a DPoP proof
                # for THIS GET, checked and spent, when DPoP is enforced), for the preview.
                # Its absence, or a ledger fault, withholds the preview, never the answer.
                reader = _preview_reader(conn, request, auth)
                addressed = mesh_wait.first_read_addressed(conn, peer_id, params.since,
                                                           reader, seq=params.seq)
            elif eff == 0:
                visible = mesh_wait.count_visible(conn, peer_id)
                outcome = "ok"
                return _no_content(visible)
            else:
                first = mesh_wait.first_read(conn, peer_id, params.since)
        # The request's connection is closed here, before the wait parks.
        if params.addressed:
            response = _addressed_answer(peer_id, addressed, eff)
            outcome = "ok"
            return response
        rows = first.rows or mesh_wait.park(peer_id, first.since, eff, nudged=first.nudged)
        outcome = "ok"
        if not rows:
            return _no_content(first.visible)
        response = HttpResponse(mesh_wait.ok_body(rows), status=200,
                                content_type="application/json")
        response[mesh_wait.VISIBLE_NAMESPACES_HEADER] = str(first.visible)
        return response
    except peer_limit.PeerRateLimited as exc:
        return peer_limit.http_response(exc)
    except errors.ApiError as exc:
        return JsonResponse(exc.body(), status=exc.status)
    except psycopg.OperationalError:
        # The database did not answer or could not finish: a connect past its bound, a refused or lost connection, and
        # (psycopg's same class) a statement cancelled past its 30 s bound or a serialization/deadlock rollback. All
        # answer 503 with Retry-After, which the node backs off on, never a 500 a client would retry at once
        # (memory-bxxxxx; pxx P3). Nothing here has moved delivery state (reads only; the client keeps
        # its cursor), so no message is lost. The usage row is skipped: its own connect would cost a second bound.
        db_unreachable = True
        log.warning("mesh wait: the database did not answer; 503", exc_info=True)
        return HttpResponse("database unavailable", status=503, headers={"Retry-After": "5"},
                            content_type="text/plain; charset=utf-8")
    finally:
        mesh_wait.release_waiter()
        if peer_id is not None and not db_unreachable:
            usage.record_on_own_connection("wait", peer_id, outcome)
