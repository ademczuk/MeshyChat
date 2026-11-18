"""Harness hook events: validation and the evaluate / backlog / bypass-audit flows.

The shape that matters most here is the BLOCKING/OBSERVATIONAL split. `post_tool` and
`model_response` describe something that has
ALREADY happened, so a deny or an ask from them can block nothing and must never open an
approval card - the outcome degrades to allow-with-warning and a decision row records
what enforcement would have done. Treating them as blocking would put a human approval
card in front of an action that already ran.

The ASK sequence is content-addressed on `(owner, session, skill_name, input_digest, attachment)`
and NOT on any per-retry tool-use
id, so a model retrying the same action re-attaches to the same card instead of opening a
new one. `input_digest` is REQUIRED as soon as a policy asks: without it there is nothing
to bind the release to, so the request is refused with a 400 rather than issuing a card
that any later action could redeem.
"""
from __future__ import annotations

import json
import logging

from . import enforcement, errors, gate, policies, repository

log = logging.getLogger("meshychat.meshguard.hooks")

MAX_HARNESS_CHARS = 32
MAX_SESSION_KEY_CHARS = 256
MAX_PREVIEW_BYTES = 16 * 1024
MAX_BACKLOG_ENTRIES = 100
MAX_BACKLOG_BODY_BYTES = 256 * 1024

#: kind -> (is_blocking, approval phase).
#: The phase values are the three `meshy_guard_approvals_phase_check` admits.
HOOK_KINDS = {
    "pre_tool": (True, "tool_call"),
    "post_tool": (False, "tool_call"),
    "prompt_submit": (True, "request"),
    "model_request": (True, "request"),
    "model_response": (False, "request"),
}


class ValidatedEvent:
    """The wire event projected onto the gate's inputs."""

    __slots__ = ("kind", "harness", "session_key", "skill_name", "tool_or_kind",
                 "input_digest", "input_args")

    def __init__(self, kind, harness, session_key, skill_name, tool_or_kind,
                 input_digest, input_args):
        self.kind = kind
        self.harness = harness
        self.session_key = session_key
        self.skill_name = skill_name
        self.tool_or_kind = tool_or_kind
        self.input_digest = input_digest
        self.input_args = input_args

    @property
    def phase(self) -> str:
        return HOOK_KINDS[self.kind][1]

    @property
    def is_blocking(self) -> bool:
        return HOOK_KINDS[self.kind][0]


def valid_harness(raw) -> bool:
    """Check `^[a-z0-9_]{1,32}$` without compiling a regular expression."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_HARNESS_CHARS:
        return False
    return all(("a" <= c <= "z") or ("0" <= c <= "9") or c == "_" for c in raw)


def require_kind(raw) -> str:
    """The `kind` check of `validate_event`, shared with the bypass-audit route.

    One copy of each of the three opening checks and of their message strings: those
    strings are pinned by tests as the wire contract, and the bypass-audit route used to
    carry a verbatim second copy that could drift from this one without any test
    noticing. Each caller's check order, messages and 400 status are unchanged.
    """
    if raw not in HOOK_KINDS:
        raise errors.validation(f"unknown hook event kind '{raw}'")
    return raw


def require_harness(raw) -> str:
    """The `harness` check of `validate_event`, shared with the bypass-audit route.

    See `require_kind`.
    """
    if not valid_harness(raw):
        raise errors.validation("harness must match ^[a-z0-9_]{1,32}$")
    return raw


def require_session_key(session) -> str:
    """The `session.session_key` check of `validate_event`, shared with the bypass-audit
    route; see `require_kind`.

    Takes the `session` object rather than the key, because the two callers answer a
    MISSING object differently and both answers are the wire: `validate_event` has
    already refused it as "session is required" before calling this, while the
    bypass-audit route reports the session_key message for it, which is what the
    `isinstance` below produces. For a present object both get the same answer.
    """
    session_key = session.get("session_key") if isinstance(session, dict) else None
    if not isinstance(session_key, str):
        raise errors.validation("session.session_key must be 1..=256 chars")
    session_key = session_key.strip()
    if not session_key or len(session_key) > MAX_SESSION_KEY_CHARS:
        raise errors.validation("session.session_key must be 1..=256 chars")
    return session_key


def _preview_fields(section) -> tuple:
    if not isinstance(section, dict):
        return (None, None)
    return (section.get("input_digest"), section.get("input_preview"))


def validate_event(raw) -> ValidatedEvent:
    """Raise `errors.ApiError` (400) for every invalid event."""
    if not isinstance(raw, dict):
        raise errors.validation("event must be a JSON object")
    if raw.get("v") != 1:
        raise errors.validation(f"v must be 1, got {raw.get('v')}")
    kind = require_kind(raw.get("kind"))
    harness = require_harness(raw.get("harness"))

    session = raw.get("session")
    if not isinstance(session, dict):
        raise errors.validation("session is required")
    session_key = require_session_key(session)

    tool = raw.get("tool")
    prompt = raw.get("prompt")
    result = raw.get("result")

    if kind in ("pre_tool", "post_tool") and not isinstance(tool, dict):
        raise errors.validation(f"tool is required for {kind} events")
    if kind == "prompt_submit" and not isinstance(prompt, dict):
        raise errors.validation("prompt is required for prompt_submit events")
    if isinstance(tool, dict):
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise errors.validation("tool.name must be non-empty")

    for section in (tool, prompt, result):
        _digest, preview = _preview_fields(section)
        if preview is None:
            continue
        try:
            size = repository.json_size(preview)
        except (TypeError, ValueError) as exc:
            raise errors.validation(f"input_preview not serializable: {exc}")
        if size > MAX_PREVIEW_BYTES:
            raise errors.validation(
                f"input_preview exceeds {MAX_PREVIEW_BYTES} serialized bytes"
            )

    if kind in ("pre_tool", "post_tool"):
        tool_or_kind = tool["name"].strip()
        source = tool
    elif kind in ("prompt_submit", "model_request"):
        tool_or_kind = kind
        source = prompt
    else:  # model_response
        tool_or_kind = kind
        source = result

    input_digest, input_preview = _preview_fields(source)
    return ValidatedEvent(
        kind=kind,
        harness=harness,
        session_key=session_key,
        # The `harness/<harness>/<tool-or-kind>` namespace is what a policy's
        # `skill == '...'` expression matches against.
        skill_name=f"harness/{harness}/{tool_or_kind}",
        tool_or_kind=tool_or_kind,
        input_digest=input_digest if isinstance(input_digest, str) else None,
        input_args={} if input_preview is None else input_preview,
    )


def policy_event(event: ValidatedEvent, owner_id, peer_id) -> policies.PolicyEvent:
    """Project an event into the policy gate's inputs."""
    return policies.PolicyEvent(
        phase=event.phase,
        skill_name=event.skill_name,
        input_args=event.input_args,
        caller_owner_id=owner_id,
        caller_peer_id=peer_id,
        target_owner_id=None,
        target_peer_id=None,
        call_id=None,
        manifest_hash=None,
        session_key=event.session_key,
    )


def record_hook_decision(conn, owner_id, peer_id, event: ValidatedEvent,
                         mode: enforcement.GateMode, decision: str, reason=None,
                         policy_id=None, attachment_id=None, approval_id=None) -> None:
    """Record an append-only decision with owner and peer from the token."""
    repository.record_policy_decision(
        conn,
        {
            "policy_id": policy_id,
            "attachment_id": attachment_id,
            "decision": decision,
            "reason": reason,
            "phase": event.phase,
            "mode": mode.mode,
            "call_id": None,
            "owner_id": owner_id,
            "caller_peer_id": peer_id,
            "target_peer_id": None,
            "skill_name": event.skill_name,
            "session_key": event.session_key,
            "approval_id": approval_id,
        },
    )


def _allow_response(mode: enforcement.GateMode, policy_names=None) -> dict:
    """Build an allow response, omitting absent optional fields rather than sending null."""
    body = {"decision": "allow", "mode": mode.mode}
    if policy_names is not None:
        body["policy_names"] = policy_names
    return body


def _ask_view(mode: enforcement.GateMode, ask: gate.PolicyAsk, item: dict,
              rfc3339) -> dict:
    """Build an ask response with its pending approval."""
    return {
        "decision": "ask",
        "reason": ask.reason,
        "approval": {
            "approval_id": item["approval_id"],
            "status": "pending",
            "expires_at": rfc3339(item["expires_at"]),
        },
        "policy_names": [ask.policy_name],
        "mode": mode.mode,
    }


def observational_response(conn, owner_id, peer_id, event: ValidatedEvent,
                           mode: enforcement.GateMode, decision) -> dict:
    """Build the response for an event whose side effect already happened.

    Deny and ask both degrade to allow-with-warning
    and a decision row preserves what enforcement would have done. `policy_names` is
    omitted on the degraded path, which is how a client tells a degraded allow from a clean
    one even before reading `warning`.
    """
    if isinstance(decision, gate.GateAllow):
        return _allow_response(mode, policy_names=[])

    if isinstance(decision, gate.GateDeny):
        original, reason, policy_id, attachment_id = "deny", decision.reason, None, None
    elif isinstance(decision, gate.GateRequiresApproval):
        if not decision.asks:
            raise errors.internal("policy gate returned RequiresApproval with no asks")
        first = decision.asks[0]
        original, reason = "ask", first.reason
        policy_id, attachment_id = first.policy_id, first.attachment_id
    else:  # pragma: no cover - GateDecision is a closed set
        raise errors.internal(f"unhandled gate decision {type(decision).__name__}")

    record_hook_decision(conn, owner_id, peer_id, event, mode, original,
                         reason=f"observational_degraded: {reason}",
                         policy_id=policy_id, attachment_id=attachment_id)
    return {"decision": "allow", "warning": reason, "mode": mode.mode}


def ask_response(conn, owner_id, peer_id, event: ValidatedEvent,
                 mode: enforcement.GateMode, asks: list, rfc3339) -> dict:
    """Produce, re-attach, or release an approval.

    ONE card per event: only the first ask opens or attaches an approval; the rest are
    recorded as Ask decision rows so the audit surface shows every policy that asked.
    """
    if not asks:
        raise errors.internal("policy gate returned RequiresApproval with no asks")
    first, rest = asks[0], asks[1:]
    for ask in rest:
        record_hook_decision(conn, owner_id, peer_id, event, mode, "ask",
                             reason=ask.reason, policy_id=ask.policy_id,
                             attachment_id=ask.attachment_id)

    if not event.input_digest:
        # Without the client's digest there is nothing to
        # bind a release to, so no honest approval can be issued. 400, not a card.
        raise errors.validation(
            f"input_digest is required when a policy asks for approval (kind {event.kind})"
        )

    key = repository.derive_hook_elicitation_key(
        owner_id, event.session_key, event.skill_name, event.input_digest,
        first.attachment_id,
    )
    lookup = repository.find_release_or_pending(
        conn, owner_id, key["elicitation_key"], key["args_hash"]
    )

    if isinstance(lookup, repository.ApprovedLookup):
        item = lookup.item
        consumed = repository.consume_approval(
            conn, owner_id, item["approval_id"], key["args_hash"]
        )
        if not consumed:
            # A raced or expired consume fails closed: a
            # stale release must not admit the action.
            return {
                "decision": "deny",
                "reason": (f"policy approval no longer consumable [{item['approval_id']}]; "
                           "re-request approval"),
                "policy_names": [first.policy_name],
                "mode": mode.mode,
            }
        record_hook_decision(conn, owner_id, peer_id, event, mode, "allow",
                             reason=f"released by approval {item['approval_id']}",
                             policy_id=first.policy_id, attachment_id=first.attachment_id,
                             approval_id=item["approval_id"])
        return _allow_response(mode, policy_names=[first.policy_name])

    if isinstance(lookup, repository.PendingLookup):
        # Re-attach: the card already exists, no duplicate is opened.
        return _ask_view(mode, first, lookup.item, rfc3339)

    approval = {
        "owner_id": owner_id,
        "caller_peer_id": peer_id,
        "skill_id": None,
        "call_id": None,
        "phase": event.phase,
        "policy": first.policy_name,
        "reason_code": "requires_human_approval",
        "message": (
            f"harness {event.harness} {event.tool_or_kind} in session "
            f"{event.session_key} requires approval by policy {first.policy_name}: "
            f"{first.reason}"
        ),
        "content_preview": repository.truncate_content_preview(
            _preview_text(event.input_args)
        ),
        "manifest_hash": None,
        "sandbox_status": None,
        "ask_timeout_secs": None,
        "audit_event_id": None,
        "deferred_effects": [],
    }
    link = {
        "policy_id": first.policy_id,
        "attachment_id": first.attachment_id,
        "elicitation_key": key["elicitation_key"],
        "approved_args_hash": key["args_hash"],
    }
    item = repository.insert_ask_approval(conn, approval, link)
    if isinstance(item, repository.NotFound):
        raise errors.not_found()
    record_hook_decision(conn, owner_id, peer_id, event, mode, "ask",
                         reason=first.reason, policy_id=first.policy_id,
                         attachment_id=first.attachment_id,
                         approval_id=item["approval_id"])
    return _ask_view(mode, first, item, rfc3339)


def _preview_text(input_args) -> str:
    """Use compact JSON separators so preview text matches the bytes measured by `json_size`."""
    return json.dumps(input_args, separators=(",", ":"), ensure_ascii=False)
