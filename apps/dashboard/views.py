"""The dashboard READ surface: `/v1/dashboard/*`.

Every handler here is owner-authenticated (`apps/dashboard/auth.py`), scoped through
the single tenancy seam (`apps/dashboard/tenant_scope.py`), and reads through the typed
repository (`apps/dashboard/repo.py`) so no route can answer "empty" for a read that
never actually ran.

RESPONSE ENVELOPES ARE PART OF THE WIRE. Each route's wrapper key is explicit rather
than guessed from the route name, because the SPA
destructures them: `peers`, `nodes`, `namespaces`, `memories`, `messages`, `models`,
`tools`, `notifications`, `unread`, `items`, `agents`, `clients`, `invoices`,
`documents`, `expenses`, `payouts`, `report`, and - inconsistently but really -
`data` for logs, bridges, memory-search and memory-duplicates, and a BARE ARRAY for
memory-export. Normalising those would be a wire change.

STATUS CODES. 200 on success; 400 E_VALIDATION for a bad query parameter; 401/403 from
the extractor; 405 with an `Allow` header for a wrong method on a path that exists
with other verbs.

WHAT IS NOT HERE, DELIBERATELY
------------------------------
* passkey / WebAuthn routes - out of scope by instruction; the seam is
  named in `urls.py` rather than silently absent.
* every WRITE (POST/PATCH/DELETE) on these same paths. Those paths therefore answer 405
  here. `docs/DIVERGENCES.md` 055 to 057 record three further divergences (HEAD, empty
  filter parameters, the payouts statement route).
* `/v1/dashboard/api/vram/*` and `/v1/dashboard/api/jobcontrol/*` - both are reverse
  proxy to external services and vram is `AuthenticatedAdmin`, a
  different extractor. Stubbing them would return a plausible-looking empty payload for
  a service that is simply not wired.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import logging
import time
import uuid
from decimal import Decimal

import psycopg
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.core import entitlements
from apps.files import quota

from . import auth as owner_auth
from . import errors, queries, repo
from .tenant_scope import TenantScope
from apps.core.env import lookup

log = logging.getLogger("meshychat.dashboard")


# ------------------------------------------------------------------ plumbing

def _json(payload, status: int = 200) -> HttpResponse:
    """Emit compact JSON with no spaces after separators.

    Django's JsonResponse default separators are `', '` and `': '`, so they must be
    overridden. A different separator changes every byte after the first key, which
    matters for a response whose bytes may be hashed, cached or diffed. HttpResponse
    rather than JsonResponse for the same reason JsonResponse would need `safe=False`
    anyway: `/v1/dashboard/api/memories/export` returns a BARE ARRAY.
    """
    return HttpResponse(
        json.dumps(payload, separators=(",", ":"), default=_json_default),
        content_type="application/json",
        status=status,
    )


def _json_default(value):
    """`json_build_object` gives psycopg a JSON document, so most values arrive already
    decoded. The stragglers are the columns read as SQL scalars rather than through a
    projection (tier-info's counts, the tax-report year, logs' retention)."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # JSON numbers represent a Postgres numeric; Decimal is not
        # JSON-serialisable and str() would quote it into a string.
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def _error_response(exc: errors.ApiError) -> HttpResponse:
    if exc.status >= 500 and exc.detail:
        # Log the detail, return the constant. The detail names tables
        # and columns and must not reach the client.
        log.error("dashboard internal error: %s", exc.detail)
    return _json(exc.body(), status=exc.status)


def _route(*allowed):
    """One guarded `meshychat_app` connection per request, the route's methods answered
    first, ApiError -> response. Used as `@_route("GET")`.

    Per-request rather than per-process: `rls.peer_context` / `server_internal` are
    TRANSACTION-local, and a connection shared across requests is how a transaction
    setting is inherited by the next, unrelated caller.

    The method is answered BEFORE the connection opens (#129), as `apps/a2a/views.py`
    does (STATUS C73): the 405 still precedes the credential (401/403) and every parse,
    a wrong method costs no connect, and it answers 405 even when the database is
    unreachable.
    """
    def decorate(handler):
        def wrapper(request, *args, **kwargs):
            bad = _method(request, *allowed)
            if bad:
                return bad
            try:
                with repo.connection() as conn:
                    return handler(request, conn, *args, **kwargs)
            except errors.ApiError as exc:
                return _error_response(exc)
        wrapper.__name__ = getattr(handler, "__name__", "route")
        wrapper.__doc__ = getattr(handler, "__doc__", None)
        return csrf_exempt(wrapper)
    return decorate


def _method(request, *allowed):
    """Return 405 + Allow for a known path with another verb."""
    if request.method not in allowed:
        return HttpResponse(status=405, headers={"Allow": ", ".join(allowed)})
    return None


def _authed(request, conn):
    return owner_auth.authenticate_owner(conn, request)


def _scope(request, conn):
    """Authenticate, then resolve the tenancy scope. Both, in this order, always."""
    return TenantScope.resolve(_authed(request, conn))


# ------------------------------------------------------- query-param parsing

def _clamp_limit(raw, default: int = 200, lo: int = 1, hi: int = 1000) -> int:
    """Use a default of 200 and clamp limits to 1 through 1000.

    A NON-NUMERIC `?limit=` is a 400. Silently falling back to the default would be the
    worse choice: the caller would get a different page than it asked for with no signal.
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise errors.validation(f"limit must be an integer: {raw!r}")
    return max(lo, min(hi, value))


def _clamp_offset(raw) -> int:
    """Use a default offset of 0 and clamp it to 0 through 1,000,000."""
    if raw is None or raw == "":
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise errors.validation(f"offset must be an integer: {raw!r}")
    return max(0, min(1_000_000, value))


def _opt(raw):
    """A query parameter that is absent OR empty is `None`, so the `IS NULL` arm of
    each filter matches. `?namespace_id=` must not filter on the empty string."""
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _opt_uuid(raw):
    """Parse an optional UUID, treating an invalid value as absent.

    A NON-UUID `source_peer_id` is IGNORED, not rejected: a parse failure becomes None
    and the filter arm goes away. This is the documented contract.
    """
    value = _opt(raw)
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _path_uuid(raw, what: str) -> uuid.UUID:
    """A malformed `{id}` is a 400 raised from inside the view, not a 404.

    A 404 would say the resource does not exist when the id was never well-formed.
    """
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise errors.validation(f"invalid {what}: {raw}")


# =========================================================================
# core data layer - /v1/dashboard/api/*
# =========================================================================

@_route("GET")
def peers(request, conn):
    """GET /v1/dashboard/api/peers.

    Visibility is CO-AUTHORSHIP, not only the route's `owner_id` predicate:
    `peers.owner_id` is
    per-tenant, so an owner filter alone would show a logged-in owner only their OWN
    node and never the rest of their mesh. `vis_peers` adds exactly the peers that
    co-authored a namespace the caller may read.
    """
    scope = _scope(request, conn)
    import time as _time
    outcome = repo.fetch_all_internal(
        conn, queries.peers_sql(scope.vis_cte()), queries.peers_params(scope.params(), int(_time.time() * 1000)),
        "peers")
    rows = repo.objs(outcome)
    # ws_online / connection_live keep their names for existing readers, now from the MEASURED socket record
    # (hub_connections) instead of Redis, which HQD production does not run (SD 356): one source of truth.
    for row in rows:
        row["ws_online"] = row["connection_live"] = bool(row.get("reachable"))
    return _json({"peers": rows})


@_route("GET")
def dispatch_nodes(request, conn):
    """GET /v1/dashboard/api/dispatch/nodes. Ordered live-first."""
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.dispatch_nodes_sql(scope.vis_cte()), scope.params(), "dispatch nodes")
    return _json({"nodes": repo.objs(outcome)})


@_route("GET")
def namespaces(request, conn):
    """GET /v1/dashboard/api/namespaces."""
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.namespaces_sql(scope.vis_cte()), scope.params(), "namespaces")
    return _json({"namespaces": repo.objs(outcome)})


@_route("GET")
def memories(request, conn):
    """GET /v1/dashboard/api/memories.

    CONTENT route, so `content_vis_cte()`: memory bodies stay inside owned-or-granted
    namespaces even for an admin caller.
    """
    scope = _scope(request, conn)
    limit = _clamp_limit(request.GET.get("limit"))
    params = scope.params(
        namespace_id=_opt(request.GET.get("namespace_id")),
        source_peer_id=_opt_uuid(request.GET.get("source_peer_id")),
        category=_opt(request.GET.get("category")),
    )
    outcome = repo.fetch_all_internal(
        conn, queries.memories_sql(scope.content_vis_cte(), limit), params, "memories")
    return _json({"memories": repo.objs(outcome)})


@_route("GET")
def messages(request, conn):
    """GET /v1/dashboard/api/messages. CONTENT route."""
    scope = _scope(request, conn)
    limit = _clamp_limit(request.GET.get("limit"))
    offset = _clamp_offset(request.GET.get("offset"))
    params = scope.params(thread_id=_opt(request.GET.get("thread_id")))
    outcome = repo.fetch_all_internal(
        conn, queries.messages_sql(scope.content_vis_cte(), limit, offset),
        params, "messages")
    return _json({"messages": repo.objs(outcome)})


@_route("GET")
def stats(request, conn):
    """GET /v1/dashboard/api/stats.

    Five counts, each bounded by `vis`/`vis_peers` rather than by the table. Read by
    explicit key from the single row; a missing key raises rather than rendering 0,
    because a zeroed dashboard tile reads as "no activity".
    """
    scope = _scope(request, conn)
    outcome = repo.fetch_one_internal(
        conn, queries.stats_sql(scope.vis_cte()), scope.params(), "stats")
    rows = repo.rows_or_raise(outcome)
    if not rows:
        raise errors.internal("stats aggregate returned no row")
    row = rows[0]
    out = {}
    for key in ("peers", "memories", "messages", "models", "tools"):
        if key not in row:
            raise errors.internal(f"stats aggregate is missing {key!r}")
        out[key] = row[key]
    return _json(out)


@_route("GET")
def models(request, conn):
    """GET /v1/dashboard/api/models.

    `port` is redacted unless the reader owns the serving peer or holds an admin
    dashboard session (TASK-556.3: a model's port is node-local material).
    """
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.models_sql(scope.vis_cte()), scope.params(), "models")
    return _json({"models": repo.objs(outcome)})


@_route("GET")
def tools(request, conn):
    """GET /v1/dashboard/api/tools. Same scope as peers."""
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.tools_sql(scope.vis_cte()), scope.params(), "tools")
    return _json({"tools": repo.objs(outcome)})


@_route("GET")
def usage(request, conn):
    """GET /v1/dashboard/api/usage.

    THE RECORDED LEAK. This endpoint once defaulted to NETWORK-GLOBAL under an
    RLS-bypassing connection and exposed every owner's spend to every authenticated
    dashboard owner, including product accounts created anonymously. The default here
    is TENANT-SCOPED, the flag is opt-IN
    (`MESHYCHAT_ALLOW_GLOBAL_USAGE`), and `test_usage_is_tenant_scoped_by_default`
    proves it with a second owner and a positive control.

    The response is the aggregate object itself, not wrapped, so the top-level keys are
    `network` and `peers`.
    """
    scope = _scope(request, conn)
    params = scope.params(global_mode=_usage_global_opt_in())
    outcome = repo.fetch_one_internal(conn, queries.USAGE_SQL, params, "usage")
    return _json(repo.one_obj(outcome, "usage"))


def _usage_global_opt_in() -> bool:
    """`MESHYCHAT_ALLOW_GLOBAL_USAGE` trimmed: `"1"` or
    case-insensitive `"true"`. Any other value, and an unset var, is False."""
    raw = lookup("MESHYCHAT_ALLOW_GLOBAL_USAGE")
    if raw is None:
        return False
    raw = raw.strip()
    return raw == "1" or raw.lower() == "true"


@_route("GET")
def marketplace(request, conn):
    """GET /v1/dashboard/api/marketplace.

    A valid-empty stub lets the SPA tile render.
    Still authenticated, because it is on the owner-auth router: an unauthenticated 200
    would make this route public despite its owner-auth router.
    """
    _authed(request, conn)
    return _json({"items": []})


@_route("GET")
def bridges(request, conn):
    """GET /v1/dashboard/api/bridges.

    Both endpoints of a bridge must be in `vis`, so a bridge that reaches out of the
    caller's visibility is not listed at all.
    """
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.bridges_sql(scope.vis_cte()), scope.params(), "bridges")
    return _json({"data": repo.objs(outcome)})


@_route("GET")
def notifications(request, conn):
    """GET /v1/dashboard/api/notifications."""
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.notifications_sql(scope.content_vis_cte(), unread_only=False),
        scope.params(), "notifications")
    return _json({"notifications": repo.objs(outcome)})


@_route("GET")
def notifications_count(request, conn):
    """GET /v1/dashboard/api/notifications/count.

    `{"unread": <n>}` is the number of rows from the same query with `read_at IS NULL`
    added, so the count matches the list's limit of 200. Replacing it with a
    `SELECT COUNT(*)` would return a different number for an owner with more than 200
    unread notifications.
    """
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.notifications_sql(scope.content_vis_cte(), unread_only=True),
        scope.params(), "notifications_count")
    return _json({"unread": len(repo.rows_or_raise(outcome))})


@_route("GET")
def tier_info(request, conn):
    """GET /v1/dashboard/api/tier-info.

    OWNER-scoped, not co-author-broadened: it binds the caller's own id and never
    touches `vis`.
    """
    scope = _scope(request, conn)
    params = scope.params()

    linked = repo.rows_or_raise(
        repo.fetch_all_plain(conn, queries.LINKED_AGENTS_SQL, params, "tier_info.linked"))
    linked_agents = linked[0]["linked"] if linked else 0

    tier = _owner_tier(conn, scope, "tier_info.tier")
    # Reject a tier outside the five known tiers with 400 E_VALIDATION. The DB CHECK
    # constraint currently allows only free/pro/enterprise, and the constraint is not
    # the list of tiers, so the check is made here too.
    if tier not in ("free", "starter", "pro", "enterprise", "partner"):
        raise errors.invalid_tier(tier)

    # Resolve this cap through `resolve_cap` and propagate the error; it does not default
    # and it does not read it out of the snapshot. Going
    # through the chokepoint is also what runs the entitlements gate for this route.
    max_linked_agents = _resolve_cap(conn, scope, PEERS_PER_OWNER, tier=tier)

    cap_rows = repo.rows_or_raise(repo.fetch_all_plain(
        conn, queries.CAPS_SNAPSHOT_SQL,
        scope.params(tier=tier, cycle=quota._cycle_start(conn, scope.owner)),   # the counter's own cycle
        "tier_info.caps"))
    capabilities = {}
    for row in cap_rows:
        name, value, current = row["capability"], row["value"], row["current"]
        if value is None:
            # A name with neither an override nor a tier cap is `UnknownCapability`, which
            # maps to 400 E_VALIDATION (NOT a 500). It can only arise from an override row for a capability the tier
            # table does not define.
            raise errors.unknown_capability(name)
        # Percent is None when the cap is <= 0 (the
        # unlimited/sentinel case), never a division by zero.
        percent = None if value <= 0 else (current / value * 100.0)
        capabilities[name] = {"value": value, "current": current, "percent": percent}

    return _json({
        "effective_tier": tier,
        "linked_agents": linked_agents,
        "max_linked_agents": max_linked_agents,
        "capabilities": capabilities,
    })


#: The cap `tier-info` reports as `max_linked_agents`.
PEERS_PER_OWNER = "peers_per_owner"


def _owner_tier(conn, scope, label: str) -> str:
    """The caller's tier column as stored, for `tier_info` and `_resolve_cap`.

    NOT validated against the Tier enum here: `tier_info` makes that check itself (400
    E_VALIDATION), while `_resolve_cap` today lets an unknown tier fall through to
    `UnknownCapability`, which `_max_retention_days` degrades on - moving the check in
    would turn that degrade into a 400 on `/api/logs/settings`.
    """
    tier_rows = repo.rows_or_raise(
        repo.fetch_all_plain(conn, queries.OWNER_TIER_SQL, scope.params(), label))
    if not tier_rows:
        # Raise rather than default: an owner with no dashboard_users row cannot have a
        # tier, and inventing "free" would report a cap for an account that does not
        # exist.
        raise errors.internal("tier lookup: no dashboard_users row for the authenticated owner")
    return tier_rows[0]["tier"]


def _resolve_cap(conn, scope, capability: str, *, tier: str | None = None) -> int:
    """Resolve one capability through the single entitlement-protected chokepoint.

    The gate belongs here rather than at each call site because every capability decision
    passes through this chokepoint: quota checks, the calls pipeline's concurrency cap,
    log retention, and peers-per-owner. A per-route gate can be forgotten.

    The check was once hoisted into `_check_entitlements`, called from `tier_info` alone,
    while `_max_retention_days` re-expressed cap resolution in raw SQL. On a revoked owner,
    `/api/tier-info` answered 402 and `/api/logs/settings` answered 200. The fix is not
    "call the gate from the second route too": that leaves the third route to be
    forgotten. It is to have ONE place a cap can be resolved, with the gate in it.

    The order is gate first, then override, then the tier default. A cap with
    neither is `UnknownCapability` - the ONE condition `max_retention_days` degrades on,
    which is why it is raised as a distinct class (`errors.UnknownCapability`).
    """
    _check_entitlements(conn, scope)
    if tier is None:
        tier = _owner_tier(conn, scope, "tier.resolve_tier")
    rows = repo.rows_or_raise(repo.fetch_all_plain(
        conn, queries.RESOLVE_CAP_SQL,
        {**scope.params(tier=tier), "cap": capability}, "tier.resolve_cap"))
    value = rows[0]["value"] if rows else None
    if value is None:
        raise errors.unknown_capability(capability)
    return int(value)


def _check_entitlements(conn, scope) -> None:
    """Run the entitlement gate before every capability resolution.

    NOT called directly by any route: `_resolve_cap` is its only caller, on purpose.
    Calling it from a handler is how the per-route version of this gate came to miss a
    handler in the first place.

    The gate itself is `apps.core.entitlements.require`, the one implementation
    `apps/files/quota` calls too (REFACTOR-QUEUE-2026-11 B2: this was a second copy of its
    SQL and its order). Its order is the policy: NO ROW passes (an owner that predates the
    Accounts cutover is not denied); a row whose `recheck_at` has passed DENIES rather than
    falling back to the last verdict; an inactive row denies. This function only translates
    core's refusals into the dashboard's 402s, and a driver error into the dashboard's JSON
    500, as `repo.fetch_all_plain` did for the copy: core does not wrap one.
    """
    try:
        entitlements.require(conn, scope.owner)
    except entitlements.EntitlementsStale:
        raise errors.entitlements_stale() from None
    except entitlements.ProductAccessDenied:
        raise errors.product_access_denied() from None
    except psycopg.Error as exc:
        raise errors.internal(f"dashboard entitlements: {exc}") from exc


# =========================================================================
# memory explorer - /v1/dashboard/api/memories/*
# =========================================================================

@_route("GET")
def memory_peers(request, conn):
    """GET /v1/dashboard/api/memories/peers.

    Returns the three raw columns, NOT the `obj` projection, so it is read by explicit
    key rather than through `repo.objs`.
    """
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.memory_peers_sql(scope.content_vis_cte()),
        scope.params(), "memories.peers")
    out = []
    for row in repo.rows_or_raise(outcome):
        for key in ("id", "name", "display_name"):
            if key not in row:
                raise errors.internal(f"memories.peers row is missing {key!r}")
        out.append({"id": row["id"], "name": row["name"], "display_name": row["display_name"]})
    return _json({"peers": out})


@_route("GET")
def memory_search(request, conn):
    """GET /v1/dashboard/api/memories/search.

    `search_time_ms` is the measured wall time of the query, and the SPA renders it.

    NOTE the different default limit: search and duplicates default to 50 (max 500),
    while `memories` and the other list routes default to 200 (max 1000). Export has no
    limit, and each limited route keeps its own clamp.
    """
    started = time.monotonic()
    scope = _scope(request, conn)
    limit = _clamp_limit(request.GET.get("limit"), default=50, hi=500)
    params = scope.params(
        namespace_id=_opt(request.GET.get("namespace_id")),
        source_peer_id=_opt_uuid(request.GET.get("source_peer_id")),
        category=_opt(request.GET.get("category")),
        q=_opt(request.GET.get("q")),
    )
    outcome = repo.fetch_all_internal(
        conn, queries.memory_search_sql(scope.content_vis_cte(), limit),
        params, "memories.search")
    data = repo.objs(outcome)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return _json({"data": data, "search_time_ms": elapsed_ms})


@_route("GET")
def memory_export(request, conn):
    """GET /v1/dashboard/api/memories/export.

    A BARE JSON ARRAY, not `{"data": [...]}`. The SPA downloads the response as a file,
    so wrapping it would change the file.
    """
    scope = _scope(request, conn)
    outcome = repo.fetch_all_internal(
        conn, queries.memory_export_sql(scope.content_vis_cte()),
        scope.params(), "memories.export")
    return _json(repo.objs(outcome))


@_route("GET")
def memory_duplicates(request, conn):
    """GET /v1/dashboard/api/memories/duplicates.

    Normalized-TITLE collision, not cosine similarity: there is no embedding index on
    `shared_memory` (`embed_policy='forbidden'`). `threshold` is accepted and ignored,
    as documented in the SPA contract.
    """
    scope = _scope(request, conn)
    limit = _clamp_limit(request.GET.get("limit"), default=50, hi=500)
    outcome = repo.fetch_all_internal(
        conn, queries.memory_duplicates_sql(scope.content_vis_cte(), limit),
        scope.params(), "memories.duplicates")
    return _json({"data": repo.objs(outcome)})


# =========================================================================
# node logs - /v1/dashboard/api/logs*
# =========================================================================

LOG_LEVELS = ("trace", "debug", "info", "warn", "error")
DEFAULT_RETENTION_DAYS = 7
RETENTION_HARD_CAP_DAYS = 365
LOG_RETENTION_DAYS_MAX = "log_retention_days_max"


def _normalize_level(raw):
    """An unknown level is a 400, not an ignored filter.

    Only an ABSENT parameter is no filter. `?level=` is rejected rather than treated as
    absent.
    """
    if raw is None:
        return None
    level = raw.strip().lower()
    if level not in LOG_LEVELS:
        raise errors.validation(f"level must be one of {', '.join(LOG_LEVELS)}")
    return level


def _escape_like(value: str) -> str:
    """Escape a user query so it matches literally under ESCAPE '\\'.

    Backslash FIRST: escaping `%` before `\\` would then double the backslash this
    function just introduced and turn `a%b` into a pattern matching `a\\b`.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: RFC3339: full date, `T`, `t` or a space, time
#: WITH seconds, an optional fraction, and a REQUIRED offset (`Z`, `z` or +hh:mm).
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})")


def _parse_rfc3339(raw):
    """A non-RFC3339 `before` is a 400.

    Only an ABSENT parameter is no filter (`?before=` is a 400).
    The shape is checked before parsing because `fromisoformat` alone accepts a date
    alone, a time without seconds and, worst, a time WITHOUT an offset, which returns a
    naive datetime that Postgres reads in the server's TimeZone, so the cutoff moved
    with a server setting.
    """
    if raw is None:
        return None
    if not _RFC3339.fullmatch(raw):
        raise errors.validation("before must be an RFC3339 timestamp")
    try:
        text = raw[:-1] + "+00:00" if raw[-1] in "Zz" else raw
        return _dt.datetime.fromisoformat(text[:10] + "T" + text[11:])
    except ValueError:
        raise errors.validation("before must be an RFC3339 timestamp")


@_route("GET")
def logs(request, conn):
    """GET /v1/dashboard/api/logs.

    Owner-scoped through the route's `owner_id` predicate, never co-author-broadened:
    node log lines are the
    owner's own operational output.

    Retention is LAZY and runs on the read path: the window is enforced by deleting this
    owner's expired rows inside the same transaction, so no background sweeper is
    needed. That makes this GET a route that writes, which is surprising.
    """
    scope = _scope(request, conn)
    limit = _clamp_limit(request.GET.get("limit"), default=100, hi=500)
    level = _normalize_level(request.GET.get("level"))
    before = _parse_rfc3339(request.GET.get("before"))
    q = _opt(request.GET.get("q"))
    pattern = f"%{_escape_like(q)}%" if q else None
    peer_id = _opt(request.GET.get("peer_id"))
    if peer_id is not None:
        # A malformed value is a 400, NOT an ignored filter. Unlike memories'
        # `source_peer_id`, this is parsed strictly; collapsing them would change their
        # distinct behavior.
        peer_id = _path_uuid(peer_id, "peer_id")

    # dashboard_node_logs and dashboard_log_settings are BOTH server-internal RLS
    # (measured), so the retention read, the expiry delete and the list all need the
    # privileged context - and all three run in ONE transaction.
    def read(cur):
        cur.execute(queries.LOGS_RETENTION_READ_SQL, scope.params())
        row = cur.fetchone()
        retention = DEFAULT_RETENTION_DAYS if row is None else row["days"]
        cur.execute(queries.LOGS_EXPIRE_SQL, scope.params(
            days=max(1, min(RETENTION_HARD_CAP_DAYS, int(retention)))))
        cur.execute(queries.LOGS_LIST_SQL, scope.params(
            peer_id=peer_id, level=level, pattern=pattern,
            before=before, limit=limit))
        return retention, cur.fetchall()
    retention, rows = repo.run_internal(conn, "logs.list", read)

    data = []
    for row in rows:
        if "obj" not in row:
            raise errors.internal("logs.list projection is missing the 'obj' column")
        data.append(row["obj"])
    return _json({"data": data, "retention_days": retention})


@_route("GET")
def log_settings(request, conn):
    """GET /v1/dashboard/api/logs/settings.

    `retention_days` is `min(stored, max)`: a window stored above the tier's ceiling is
    REPORTED clamped, so the UI never shows a number the server will not honour.
    """
    scope = _scope(request, conn)
    maximum = _max_retention_days(conn, scope)
    outcome = repo.fetch_all_internal(
        conn, queries.LOGS_RETENTION_READ_SQL, scope.params(), "logs.settings")
    rows = repo.rows_or_raise(outcome)
    retention = rows[0]["days"] if rows else DEFAULT_RETENTION_DAYS
    return _json({"retention_days": min(retention, maximum), "max_retention_days": maximum})


def _max_retention_days(conn, scope) -> int:
    """Resolve `max_retention_days`, including both degradations.

      cap < 0            -> the hard cap (the "-1 = unlimited" convention)
      UnknownCapability  -> the DEFAULT (7). A pre-097 database has no such row, and
                            this resolves to 7 rather than erroring. This is the
                            ONE place a missing capability is not an error.

    EVERYTHING ELSE PROPAGATES. That makes an Accounts-revoked owner a 402 on this
    route too, because the gate runs inside `_resolve_cap`. This function
    used to re-express cap resolution in its own SQL, which skipped the gate entirely;
    the narrow `except` is deliberate, since a broad one would re-open exactly that hole
    by swallowing the gate's refusal into "7".
    """
    try:
        value = _resolve_cap(conn, scope, LOG_RETENTION_DAYS_MAX)
    except errors.UnknownCapability:
        return DEFAULT_RETENTION_DAYS
    if value < 0:
        return RETENTION_HARD_CAP_DAYS
    return max(1, min(RETENTION_HARD_CAP_DAYS, value))


# =========================================================================
# agents - /v1/dashboard/agents
# =========================================================================

@_route("GET")
def agents(request, conn):
    """GET /v1/dashboard/agents.

    Strictly owner-scoped: a caller only sees or mutates peers under its own owner_id.
    `peers` has no RLS, so that WHERE clause is the authorization.
    """
    scope = _scope(request, conn)
    outcome = repo.fetch_all_plain(conn, queries.AGENTS_SQL, scope.params(), "agents.list")
    return _json({"agents": repo.objs(outcome)})


# =========================================================================
# document routes - /v1/dashboard/api/{clients,invoices,documents,expenses,payouts,tax-report}
# =========================================================================

@_route("GET")
def clients(request, conn):
    """GET /v1/dashboard/api/clients."""
    scope = _scope(request, conn)
    outcome = repo.fetch_all_plain(conn, queries.CLIENTS_SQL, scope.params(), "clients.list")
    return _json({"clients": repo.objs(outcome)})


@_route("GET")
def invoices(request, conn):
    """GET /v1/dashboard/api/invoices."""
    scope = _scope(request, conn)
    params = scope.params(status=_opt(request.GET.get("status")))
    outcome = repo.fetch_all_plain(conn, queries.INVOICES_SQL, params, "invoices.list")
    return _json({"invoices": repo.objs(outcome)})


@_route("GET")
def invoice_items(request, conn, invoice_id):
    """GET /v1/dashboard/api/invoices/{invoice_id}/items."""
    scope = _scope(request, conn)
    params = scope.params(invoice_id=_path_uuid(invoice_id, "invoice id"))
    outcome = repo.fetch_all_plain(conn, queries.INVOICE_ITEMS_SQL, params, "items.list")
    return _json({"items": repo.objs(outcome)})


@_route("GET")
def documents(request, conn):
    """GET /v1/dashboard/api/documents."""
    scope = _scope(request, conn)
    params = scope.params(doc_type=_opt(request.GET.get("type")))
    outcome = repo.fetch_all_plain(conn, queries.DOCUMENTS_SQL, params, "documents.list")
    return _json({"documents": repo.objs(outcome)})


@_route("GET")
def expenses(request, conn):
    """GET /v1/dashboard/api/expenses."""
    scope = _scope(request, conn)
    params = scope.params(
        **{"from": _opt(request.GET.get("from")),
           "to": _opt(request.GET.get("to")),
           "category": _opt(request.GET.get("category"))})
    outcome = repo.fetch_all_plain(conn, queries.EXPENSES_SQL, params, "expenses.list")
    return _json({"expenses": repo.objs(outcome)})


@_route("GET")
def payouts(request, conn):
    """GET /v1/dashboard/api/payouts."""
    scope = _scope(request, conn)
    outcome = repo.fetch_all_plain(conn, queries.PAYOUTS_SQL, scope.params(), "payouts.list")
    return _json({"payouts": repo.objs(outcome)})


@_route("GET")
def tax_report(request, conn):
    """GET /v1/dashboard/api/tax-report.

    An absent `year` means the current year. A non-integer `year` is a 400 rather than a
    silent fallback.
    """
    scope = _scope(request, conn)
    raw_year = _opt(request.GET.get("year"))
    year = None
    if raw_year is not None:
        try:
            year = int(raw_year)
        except ValueError:
            raise errors.validation(f"year must be an integer: {raw_year!r}")
    outcome = repo.fetch_all_plain(
        conn, queries.TAX_REPORT_SQL, scope.params(year=year), "tax_report")
    return _json({"report": repo.one_obj(outcome, "tax report")})
