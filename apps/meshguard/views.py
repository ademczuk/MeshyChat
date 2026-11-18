"""HTTP surface for MeshGuard: reviews, the approval queue, the policy registry, hooks.

Routes, methods and status codes are defined here.

=============================================================================
THE GATE DEFAULTS CLOSED. See `enforcement.py` for the full statement and the
reasoning: an unset, empty or unrecognised `MESHYCHAT_POLICY_REGISTRY_ENFORCE`
value ENFORCES. Only an
explicit `observe` / `0` / `false` / `no` / `off` relaxes it. The wire
vocabulary is unchanged - `mode` is still "observe" or "enforce".
=============================================================================

THE STATUS CODES that are easy to get wrong by one:

  GET  /v1/meshguard/skills/{id}                   404 when outside the tenant, NOT 403;
                                                    a 403 would disclose that the skill exists.
  POST /v1/meshguard/approvals/{id}/resolve        403 when the caller opened the card,
                                                    to prevent self-approval.
  POST /v1/meshguard/policies/attachments          403 for scope=server_default and 400
                                                    for every other scope problem.
  POST /v1/meshguard/policies/attachments/{id}/toggle
                                                   404 for a server_default or
                                                   other-tenant row - the repository
                                                   predicate is `owner_id = $2`, so
                                                   neither leaks its existence.
  POST /v1/meshguard/hooks/backlog                 413 E_BACKLOG_TOO_LARGE for too many
                                                    entries; 400 for too many BYTES. Two
                                                    different limits, two different codes.

AUTHENTICATION is peer-JWT on every route here; there is no public route on this surface.
The owner is derived from the authenticated peer on every one of them
(`repository.owner_for_peer`) and is never read from a body or a query string.

Timestamps go out in RFC 3339 form.
"""
from __future__ import annotations

import json
import logging
import uuid

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.core import rls
from apps.peers.auth import authenticate_peer
from apps.peers.timefmt import format_rfc3339

from . import db, enforcement, errors, gate, hooks, policies, repository

log = logging.getLogger("meshychat.meshguard")

#: Both owner-scoped list routes are capped at 100.
QUEUE_LIMIT = 100
#: Decisions default to 50; a given limit is clamped to 1 through 200.
DECISIONS_DEFAULT_LIMIT = 50
DECISIONS_MIN_LIMIT = 1
DECISIONS_MAX_LIMIT = 200


# ------------------------------------------------------------------- plumbing


def _json_body(request) -> dict:
    if not request.body:
        raise errors.validation("body must be a JSON object")
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.validation(f"body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise errors.validation("body must be a JSON object")
    return body


def _route(*allowed):
    """Answer the METHOD, authenticate the peer, derive the owner, then run the handler -
    on one connection per request, with ApiError mapped to the JSON envelope. Used as
    `@_route("GET")`; the handler runs as `handler(request, conn, auth, owner,
    **path_kwargs)`.

    THE PROLOGUE IS STRUCTURAL, NOT COPIED. Every one of the fourteen routes used to open
    with the same five lines (method check, `authenticate_peer`, `_owner`), and a route
    that dropped them read exactly like one that had them - the omission
    docs/REVIEW-FIX-QUEUE.md item 492 records for skillsreg's `sync_manifest`, which
    "bypasses @_route and never authenticates". Here a route cannot be reached without
    them. The order is unchanged: 405 + `Allow`, then 401 from the credential, then 404
    for a peer that is no longer active (`_owner`). The method is answered BEFORE the
    connection opens (#129): a wrong method costs no connect, and answers 405 even when the database
    is unreachable.

    An unexpected exception is NOT caught into a response here: Django turns it into a
    500 with no body of ours, and the traceback reaches the log. Catching it and
    returning an empty result is the shape house rule 3 forbids - and on THIS surface it
    would mean a failed gate reading as "nothing to enforce".
    """
    def decorate(handler):
        def wrapper(request, *args, **kwargs):
            if request.method not in allowed:
                return HttpResponse(status=405, headers={"Allow": ", ".join(allowed)})
            try:
                with db.connection() as conn:
                    auth = authenticate_peer(conn, request)
                    owner = _owner(conn, auth)
                    return handler(request, conn, auth, owner, *args, **kwargs)
            except errors.ApiError as exc:
                if exc.status >= 500:
                    # `detail` defaults to None on the shared class: log the message then.
                    log.error("meshguard: %s",
                              exc.detail if exc.detail is not None else exc.message)
                return JsonResponse(exc.body(), status=exc.status)
        wrapper.__name__ = getattr(handler, "__name__", "route")
        return csrf_exempt(wrapper)
    return decorate


def _owner(conn, auth) -> str:
    """A NotFound absence becomes 404 here and nowhere else."""
    owner = repository.owner_for_peer(conn, auth.peer_id)
    if isinstance(owner, repository.NotFound):
        log.info("meshguard: %s %s is not an active peer", owner.what, owner.key)
        raise errors.not_found()
    return owner


def _require_same_tenant(conn, caller_owner: str, target_owner: str) -> None:
    """Outside the tenant/dev-group is a 404, never a 403."""
    if not repository.same_tenant_or_devgroup(conn, caller_owner, target_owner):
        raise errors.not_found()


def _ts(value):
    return None if value is None else format_rfc3339(value)


def _uuid(raw, what: str) -> str:
    """Parse and validate a UUID path segment.

    A malformed id is a 400, not a 404, because 404 would claim the thing does not exist;
    it is rejected at the top of the view. The body is this app's JSON envelope.

    It also keeps a malformed id out of the SQL, where psycopg would raise
    `InvalidTextRepresentation` and the route would answer 500.
    """
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        raise errors.validation(f"invalid {what}: {raw}")


# ------------------------------------------------------------------ wire shapes


def _approval_view(row: dict) -> dict:
    """Build an approval response in its declared field order.

    The optional fields (`skill_id`, `call_id`, `policy`, and others) are OMITTED when null
    rather than sent as null; `resolved_at`, `resolver_peer_id` and `verdict` are always
    present, null included. That asymmetry is the
    wire contract, not an oversight - a client distinguishes "unresolved" (null verdict)
    from "no policy attached" (absent policy_id).
    """
    out = {
        "approval_id": row["approval_id"],
        "status": row["status"],
        "owner_id": str(row["owner_id"]),
        "caller_peer_id": str(row["caller_peer_id"]),
    }
    if row.get("skill_id") is not None:
        out["skill_id"] = str(row["skill_id"])
    if row.get("call_id") is not None:
        out["call_id"] = row["call_id"]
    out["phase"] = row["phase"]
    if row.get("policy") is not None:
        out["policy"] = row["policy"]
    if row.get("reason_code") is not None:
        out["reason_code"] = row["reason_code"]
    out["message"] = row["message"]
    out["content_preview"] = row["content_preview"]
    if row.get("manifest_hash") is not None:
        out["manifest_hash"] = row["manifest_hash"]
    if row.get("sandbox_status") is not None:
        out["sandbox_status"] = row["sandbox_status"]
    out["created_at"] = _ts(row["created_at"])
    out["expires_at"] = _ts(row["expires_at"])
    out["resolved_at"] = _ts(row.get("resolved_at"))
    resolver = row.get("resolver_peer_id")
    out["resolver_peer_id"] = None if resolver is None else str(resolver)
    out["verdict"] = row.get("verdict")
    for key in ("audit_event_id", "policy_id", "attachment_id", "elicitation_key",
                "approved_args_hash"):
        if row.get(key) is not None:
            out[key] = row[key]
    if row.get("consumed_at") is not None:
        out["consumed_at"] = _ts(row["consumed_at"])
    return out


def _discoverable_policy(row: dict) -> dict:
    """Build a discovery response without `handler_ref`.

    Adding it would publish the internal dispatch name to every authenticated peer.
    """
    return {
        "policy_id": row["policy_id"],
        "name": row["name"],
        "kind": row["kind"],
        "version": row["version"],
        "description": row["description"],
        "params_schema": row["params_schema"],
    }


def _attachment_view(conn, row: dict) -> dict:
    """Build an attachment response with its policy name joined per row."""
    policy = repository.policy_by_id(conn, row["policy_id"])
    if isinstance(policy, repository.NotFound):
        # An attachment whose policy vanished. The FK
        # `meshy_guard_policy_attachments_policy_id_fkey` makes this unreachable while the
        # FK holds; if it ever is reached, it is a 500 and not a row with a blank name.
        raise errors.internal(
            f"attachment {row['attachment_id']} references missing policy {row['policy_id']}"
        )
    return {
        "attachment_id": row["attachment_id"],
        "policy_id": row["policy_id"],
        "policy_name": policy["name"],
        "scope": row["scope"],
        "session_key": row["session_key"],
        "params": row["params"],
        "phases": list(row["phases"]),
        "enabled": row["enabled"],
        "ordinal": row["ordinal"],
        "ask_timeout_secs": row["ask_timeout_secs"],
        "created_at": _ts(row["created_at"]),
    }


def _decision_view(row: dict) -> dict:
    """Build a policy-decision response."""
    return {
        "decision_id": row["decision_id"],
        "policy_id": row["policy_id"],
        "attachment_id": row["attachment_id"],
        "decision": row["decision"],
        "reason": row["reason"],
        "phase": row["phase"],
        "mode": row["mode"],
        "call_id": row["call_id"],
        "skill_name": row["skill_name"],
        "session_key": row["session_key"],
        "approval_id": row["approval_id"],
        "created_at": _ts(row["created_at"]),
    }


# ------------------------------------------------------------------ RG reviews


@_route("GET")
def skill_review(request, conn, auth, caller_owner, skill_id):
    """GET /v1/meshguard/skills/{skill_id}."""
    skill_id = _uuid(skill_id, "skill id")
    skill_owner = repository.owner_for_skill(conn, skill_id)
    if isinstance(skill_owner, repository.NotFound):
        raise errors.not_found()
    _require_same_tenant(conn, caller_owner, skill_owner)
    review = repository.skill_review_json(conn, skill_id)
    if isinstance(review, repository.NotFound):
        raise errors.not_found()
    return JsonResponse({"review": review}, status=200)


@_route("GET")
def meshy_memory_review(request, conn, auth, caller_owner, owner_id, key):
    """GET /v1/meshguard/memory/meshy/{owner_id}/{key}.

    THE ROUTE MOVED, THE TABLE DID NOT, and that difference is not cosmetic. The wire path
    is ours to name (divergence 013). The table is `meshy_memory`, created by
    `sql/012_file_and_memory.sql`, a checksummed migration and one of 104 applied
    verbatim. Editing it crash-loops startup, which `.gitattributes` prevents; renaming it
    would fork the schema from the shared database. So `meshy_memory` and
    `meshy_guard_review` keep their compatibility names, and the SQL below still says so.

    THIS ROUTE WAS DEAD UNTIL NOW (divergence 012): `meshy_memory` is RLS-enabled AND FORCED,
    and `_route` hands over a connection with no context bound, so the policy matched
    nothing and the NotFound branch always won. Measured on one seeded row, same query:
    no context -> invisible; `peer_context(owner_id)` -> the row; `server_internal` ->
    invisible, because the policy is tenant-scoped rather than privileged.

    The context is bound to the NAMED owner, and only AFTER `_require_same_tenant` has
    authorised the caller to read that owner. Binding
    the CALLER's owner instead would silently break the dev-group case the tenancy check
    exists to allow.
    """
    owner_id = _uuid(owner_id, "owner id")
    _require_same_tenant(conn, caller_owner, owner_id)
    with rls.peer_context(conn, owner_id=str(owner_id)) as bound:
        review = repository.meshy_memory_review_json(bound, owner_id, key)
    if isinstance(review, repository.NotFound):
        raise errors.not_found()
    return JsonResponse({"review": review}, status=200)


# ------------------------------------------------------------------ scan queue


@_route("GET")
def pending_scans(request, conn, auth, owner):
    """GET /v1/meshguard/scans/pending.

    Owner-scoped: a peer sees only its own agents' pending misalignment reviews.
    """
    result = repository.pending_scan_results_for_owner(conn, owner, QUEUE_LIMIT)
    if isinstance(result, repository.Empty):
        log.debug("meshguard: pending scans empty (%s)", result.scope)
    return JsonResponse({"scans": result.rows()}, status=200)


# -------------------------------------------------------------------- approvals


@_route("GET")
def pending_approvals(request, conn, auth, owner):
    """GET /v1/meshguard/approvals/pending."""
    queue = repository.pending_approvals_for_owner(conn, owner, QUEUE_LIMIT)
    return JsonResponse(
        {
            "items": [_approval_view(r) for r in queue["items"]],
            "pending_count": queue["pending_count"],
        },
        status=200,
    )


@_route("GET")
def approval_item(request, conn, auth, caller_owner, approval_id):
    """GET /v1/meshguard/approvals/{approval_id}.

    READ, AUTHORISE, THEN SWEEP - in that order, and the order is the point. The lookup
    that answers "whose card is this?" must not itself move the card: `approval_row` is a
    pure read, and `expire_and_reload_approval` (which performs the `pending -> expired`
    UPDATE) runs only once `_require_same_tenant` has admitted this caller. Before the
    split, naming any approval id from any tenant was enough to drive that transition on
    a row the caller then got a 404 for.
    """
    approval = repository.approval_row(conn, approval_id)
    if isinstance(approval, repository.NotFound):
        raise errors.not_found()
    _require_same_tenant(conn, caller_owner, str(approval["owner_id"]))
    approval = repository.expire_and_reload_approval(conn, approval_id)
    if isinstance(approval, repository.NotFound):
        raise errors.not_found()
    return JsonResponse(_approval_view(approval), status=200)


@_route("POST")
def resolve_approval(request, conn, auth, caller_owner, approval_id):
    """POST /v1/meshguard/approvals/{approval_id}/resolve.

    The self-resolve refusal is the load-bearing line: the peer whose gated action opened
    this card may not resolve it, or a compromised agent rubber-stamps itself past the
    human-in-the-loop gate the queue exists to be. It is checked AFTER the tenancy check,
    so an out-of-tenant caller still gets 404 rather than learning the caller_peer_id.

    NOTHING WRITES BEFORE THOSE TWO CHECKS. The lookup is `approval_row`, a pure read;
    the expiry sweep that used to ride along with it now happens inside
    `repository.resolve_approval` (its step 2), on the far side of both checks. And the
    owner that lookup returned is handed to `resolve_approval` so the UPDATE itself is
    owner-scoped - the route's authorisation re-asserted in the WHERE clause, because no
    `meshy_guard_*` table has RLS to fall back on.
    """
    body = _json_body(request)
    verdict = body.get("verdict")
    if verdict not in ("accept", "decline", "cancel"):
        # An unknown verdict is rejected before any resolution work, with 400.
        raise errors.validation(
            f"verdict must be one of accept, decline, cancel; got {verdict!r}"
        )

    approval = repository.approval_row(conn, approval_id)
    if isinstance(approval, repository.NotFound):
        raise errors.not_found()
    approval_owner = str(approval["owner_id"])
    _require_same_tenant(conn, caller_owner, approval_owner)
    if str(auth.peer_id) == str(approval["caller_peer_id"]):
        log.info("meshguard: self-resolve refused for %s: %s", approval_id,
                 "the peer whose gated action created this approval may not resolve it")
        raise errors.forbidden()

    # TODO(TASK-536): on Accept the
    # deferred_effects are meant to be applied in the same transaction once the producer
    # schema for grant and tag writes is finalized. The
    # column is written with an empty JSON array and never replayed.
    resolved = repository.resolve_approval(conn, approval_id, approval_owner,
                                           auth.peer_id, verdict)
    if isinstance(resolved, repository.NotFound):
        raise errors.not_found()
    return JsonResponse(_approval_view(resolved), status=200)


# --------------------------------------------------------------- policy registry


@_route("GET")
def list_policies(request, conn, auth, owner):
    """GET /v1/meshguard/policies.

    Auth proves an active peer; discovery itself is not owner-scoped, so the `owner`
    the prologue derived is deliberately unread here. The builtin seeds are upserted
    first so a fresh deploy has something discoverable.
    """
    repository.seed_builtin_policies(conn, policies.BUILTIN_POLICY_SEEDS)
    result = repository.list_discoverable_policies(conn)
    rows = result.rows()
    return JsonResponse({"policies": [_discoverable_policy(r) for r in rows]}, status=200)


@_route("GET", "POST")
def attachments(request, conn, auth, owner):
    """GET + POST /v1/meshguard/policies/attachments."""
    if request.method == "GET":
        result = repository.attachments_for_owner(conn, owner)
        return JsonResponse(
            {"attachments": [_attachment_view(conn, r) for r in result.rows()]},
            status=200,
        )

    body = _json_body(request)
    scope = body.get("scope")
    if scope not in ("server_default", "owner", "session"):
        raise errors.validation(f"scope must be owner or session, got '{scope}'")
    session_key = body.get("session_key")
    if scope == "server_default":
        # Admin-only. 403, not 400: the request is well-formed and refused.
        raise errors.forbidden()
    if scope == "owner" and session_key is not None:
        raise errors.validation("session_key must be null for owner scope")
    if scope == "session" and (not isinstance(session_key, str) or not session_key.strip()):
        raise errors.validation("session_key is required for session scope")

    policy_id = body.get("policy_id")
    if not isinstance(policy_id, str):
        raise errors.validation("policy_id is required")
    policy = repository.policy_by_id(conn, policy_id)
    if isinstance(policy, repository.NotFound):
        raise errors.not_found()
    if not policy["enabled"]:
        raise errors.validation("policy is disabled")

    params = body.get("params")
    try:
        policies.validate_attachment_params(policy["handler_ref"], params)
    except policies.PolicyParamsError as exc:
        # This is where an out-of-subset CEL expression is refused: at ATTACH time, with
        # a 400 naming the construct, rather than at evaluation time as a silent allow.
        raise errors.validation(str(exc))

    raw_phases = body.get("phases")
    if raw_phases is None:
        phases = ["tool_call"]           # Default phase.
    else:
        if not isinstance(raw_phases, list):
            raise errors.validation("phases must be an array when provided")
        phases = []
        for p in raw_phases:
            if p not in ("tool_call", "flow", "request"):
                raise errors.validation(f"unknown phase '{p}'")
            phases.append(p)
        if not phases:
            raise errors.validation("phases must be non-empty when provided")

    ordinal = body.get("ordinal")
    if ordinal is not None and not isinstance(ordinal, int):
        raise errors.validation("ordinal must be an integer")
    ask_timeout = body.get("ask_timeout_secs")
    if ask_timeout is not None and not isinstance(ask_timeout, int):
        raise errors.validation("ask_timeout_secs must be an integer")

    row = repository.attach_policy(conn, {
        "policy_id": policy_id,
        "scope": scope,
        # HOUSE RULE 5: the owner is the token-derived one, never body["owner_id"].
        "owner_id": owner,
        "session_key": session_key,
        "params": params,
        "phases": phases,
        "ordinal": ordinal or 0,
        "ask_timeout_secs": ask_timeout,
        "created_by_peer_id": auth.peer_id,
    })
    if isinstance(row, repository.Duplicate):
        # `rgpa_unique_binding` refused it: this exact (policy, scope, owner, session)
        # binding already exists. 409 and not 400 - the body is well-formed and the
        # refusal is about existing state - and not 500, which is what the unhandled
        # UniqueViolation produced (and, under MESHY_DEBUG, with a traceback carrying the
        # DSN and the attempted row). The client receives a conflict rather than an
        # internal error because the duplicate is a valid request against existing state.
        log.info("meshguard: duplicate attachment refused (%s) for owner %s",
                 row.constraint, owner)
        raise errors.conflict(
            "an attachment for this policy already exists at this scope"
        )
    return JsonResponse(_attachment_view(conn, row), status=200)


@_route("POST")
def toggle_attachment(request, conn, auth, owner, attachment_id):
    """POST /v1/meshguard/policies/attachments/{id}/toggle."""
    body = _json_body(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise errors.validation("enabled must be a boolean")
    row = repository.set_attachment_enabled(conn, attachment_id, owner, enabled)
    if isinstance(row, repository.NotFound):
        raise errors.not_found()
    return JsonResponse(_attachment_view(conn, row), status=200)


@_route("DELETE")
def remove_attachment(request, conn, auth, owner, attachment_id):
    """DELETE /v1/meshguard/policies/attachments/{id}."""
    outcome = repository.remove_attachment(conn, attachment_id, owner)
    if isinstance(outcome, repository.NotFound):
        raise errors.not_found()
    return JsonResponse({"removed": True}, status=200)


@_route("GET")
def list_decisions(request, conn, auth, owner):
    """GET /v1/meshguard/policies/decisions?limit=N."""
    raw = request.GET.get("limit")
    if raw is None:
        limit = DECISIONS_DEFAULT_LIMIT
    else:
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            # Reject a non-integer with 400 before querying; the body is this app's JSON
            # envelope.
            raise errors.validation(f"limit must be an integer, got {raw!r}")
        limit = max(DECISIONS_MIN_LIMIT, min(DECISIONS_MAX_LIMIT, limit))
    result = repository.recent_decisions_for_owner(conn, owner, limit)
    return JsonResponse(
        {"decisions": [_decision_view(r) for r in result.rows()]}, status=200
    )


# ------------------------------------------------------------------------ hooks


@_route("POST")
def hooks_evaluate(request, conn, auth, owner):
    """POST /v1/meshguard/hooks/evaluate.

    Enforcement is ON unless explicitly relaxed (see the module header). The gate's own
    per-attachment decision rows are written by `gate.policy_registry_gate`; this route
    adds only the rows the gate cannot know about - degradation, release and approval
    linkage.
    """
    event = hooks.validate_event(_json_body(request))
    mode = enforcement.policy_registry_mode()
    log.debug("meshguard: hook %s in mode %s (%s)", event.kind, mode.mode, mode.reason)

    decision = gate.policy_registry_gate(
        conn, mode, hooks.policy_event(event, owner, auth.peer_id)
    )

    if not event.is_blocking:
        return JsonResponse(
            hooks.observational_response(conn, owner, auth.peer_id, event, mode, decision),
            status=200,
        )

    if isinstance(decision, gate.GateAllow):
        return JsonResponse({"decision": "allow", "policy_names": [], "mode": mode.mode},
                            status=200)
    if isinstance(decision, gate.GateDeny):
        # The gate already recorded the deny row.
        return JsonResponse(
            {
                "decision": "deny",
                "reason": decision.reason,
                "policy_names": [decision.policy_name],
                "mode": mode.mode,
            },
            status=200,
        )
    return JsonResponse(
        hooks.ask_response(conn, owner, auth.peer_id, event, mode, decision.asks,
                           format_rfc3339),
        status=200,
    )


@_route("POST")
def hooks_backlog(request, conn, auth, owner):
    """POST /v1/meshguard/hooks/backlog.

    An audit lane, never an enforcement mechanism: every accepted entry lands as an
    OBSERVE-mode decision row flagged `deferred`, whatever the live gate mode is. A
    malformed entry is counted in `rejected` and skipped, not fatal to the batch.

    THE BATCH IS ONE TRANSACTION. The contract above only holds for a MALFORMED entry,
    which is decided in Python and never reaches the database. A
    DATABASE fault on entry k is a different thing: with per-entry commits it left rows
    0..k-1 durable and still answered 500, and the honest client response - resend the
    spool - then duplicated every one of them in an append-only audit lane that has no
    idempotency key to dedupe on. All-or-nothing makes that retry safe.

    `conn` is autocommit, so this transaction is explicit. The ApiError from
    `validate_event` is raised and caught INSIDE it, which is safe because it never
    touched the database: an aborted-transaction state would need a failed statement.
    """
    body = _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise errors.validation("entries must be an array")
    if len(entries) > hooks.MAX_BACKLOG_ENTRIES:
        raise errors.backlog_too_large(
            f"backlog accepts at most {hooks.MAX_BACKLOG_ENTRIES} entries per batch"
        )
    if repository.json_size(body) > hooks.MAX_BACKLOG_BODY_BYTES:
        raise errors.validation(
            f"backlog body exceeds {hooks.MAX_BACKLOG_BODY_BYTES} serialized bytes"
        )

    observe = enforcement.GateMode(enforcement.OBSERVE, "backlog is always observe-mode")
    recorded = 0
    rejected = 0
    with conn.transaction():
        for entry in entries:
            if not isinstance(entry, dict):
                rejected += 1
                continue
            outcome = entry.get("outcome")
            decision = outcome.get("decision") if isinstance(outcome, dict) else None
            # Only allow/deny/error are accepted here. `ask` and `abstain` are refused
            # because a spooled ask has no card to attach to.
            if decision not in ("allow", "deny", "error"):
                rejected += 1
                continue
            try:
                event = hooks.validate_event(entry.get("event"))
            except errors.ApiError:
                rejected += 1
                continue
            reason = f"deferred: {outcome.get('reason') or ''}".strip()
            hooks.record_hook_decision(conn, owner, auth.peer_id, event, observe,
                                       decision, reason=reason)
            recorded += 1

    out = {"recorded": recorded}
    if rejected:
        # The field is absent, not 0, when nothing was rejected.
        out["rejected"] = rejected
    return JsonResponse(out, status=200)


@_route("POST")
def hooks_bypass_audit(request, conn, auth, owner):
    """POST /v1/meshguard/hooks/bypass-audit.

    A coverage signal from a shim whose user set MESHY_HOOK_BYPASS: one decision row with
    decision `allow` and reason `client_bypass`, so a fleet view can show which nodes are
    running uncovered. It asserts nothing about what that node then did.
    """
    body = _json_body(request)
    # The same three checks `hooks.validate_event` opens with, in the same order and
    # with the same messages - shared rather than copied, because those message strings
    # are the wire and a second copy here could drift from the first with no test
    # noticing. A `session` that is not an object is the session_key message on this
    # route, not validate_event's "session is required";
    # `require_session_key` takes the object so each route keeps its own answer.
    kind = hooks.require_kind(body.get("kind"))
    harness = hooks.require_harness(body.get("harness"))
    session_key = hooks.require_session_key(body.get("session"))

    event = hooks.ValidatedEvent(
        kind=kind,
        harness=harness,
        session_key=session_key,
        skill_name=f"harness/{harness}/{kind}",
        tool_or_kind=kind,
        input_digest=None,
        input_args={},
    )
    hooks.record_hook_decision(conn, owner, auth.peer_id, event,
                               enforcement.policy_registry_mode(), "allow",
                               reason="client_bypass")
    return JsonResponse({"recorded": True}, status=200)
