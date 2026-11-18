"""The policy-registry gate: fold every applicable attachment into one decision.

The mode is resolved in `enforcement.py` and passed in as an argument. The fold works as
follows:

  * ENFORCE short-circuits on the first Deny; OBSERVE records it and keeps evaluating, so
    the pre-flip audit trail shows every policy that had an opinion rather than a prefix.
  * EVERY evaluated attachment gets a decision row - including Abstain and Allow. This gives
    an operator an audit surface that records every policy opinion, not only refusals.
  * An error - a handler this build does not have, a handler that raises, an attachment
    set that will not load - is `decision_for_error`: Deny under enforcement, Allow under
    observe. Under the fail-closed default, that means
    an unsupported CEL expression DENIES. It never silently allows.
  * A failure to WRITE the audit row is itself a deny under enforcement: a gate that
    cannot record what it decided has not decided.

`RequiresApproval` carries every ask, in precedence order; the caller opens a card for the
first and records the rest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import enforcement, policies, repository

log = logging.getLogger("meshychat.meshguard.gate")


# ------------------------------------------------------------------- decisions


@dataclass(frozen=True)
class GateAllow:
    pass


@dataclass(frozen=True)
class GateDeny:
    policy_name: str
    reason: str


@dataclass(frozen=True)
class PolicyAsk:
    """A policy attachment that requires approval."""

    policy_id: str
    attachment_id: str
    policy_name: str
    reason: str


@dataclass(frozen=True)
class GateRequiresApproval:
    asks: list = field(default_factory=list)


GateDecision = GateAllow | GateDeny | GateRequiresApproval


def _decision_for_error(mode: enforcement.GateMode, policy_name: str, reason: str) -> GateDecision:
    """Map an evaluation error to the decision required by the current mode."""
    if mode.is_enforce:
        return GateDeny(policy_name=policy_name, reason=reason)
    return GateAllow()


def _record(conn, mode, event, attachment, decision, reason, approval_id=None) -> bool:
    """Write one decision row. Returns False if the write failed.

    The exception is swallowed HERE and nowhere else, and it is swallowed into a False
    that the caller must handle - not into a decision. Under enforcement the caller turns
    False into a deny.
    """
    try:
        repository.record_policy_decision(
            conn,
            {
                "policy_id": None if attachment is None else attachment["policy_id"],
                "attachment_id": None if attachment is None else attachment["attachment_id"],
                "decision": decision,
                "reason": reason,
                "phase": event.phase,
                "mode": mode.mode,
                "call_id": event.call_id,
                "owner_id": event.caller_owner_id,
                "caller_peer_id": event.caller_peer_id,
                "target_peer_id": event.target_peer_id,
                "skill_name": event.skill_name,
                "session_key": event.session_key,
                "approval_id": approval_id,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - re-raised as a deny by the caller
        log.error("meshguard: policy decision audit write failed: %s: %s",
                  type(exc).__name__, exc)
        return False


def _error_arm(conn, mode, event, attachment, name, reason) -> GateDeny | None:
    """Record an `error` decision and settle it: the `_decision_for_error` deny under
    enforcement, nothing under observe (the caller then moves to the next attachment).

    Replaces the four copies of the same block in `policy_registry_gate` (unknown
    handler, policy error, handler raised, unknown verdict type). Whether the audit row
    was WRITTEN does not change this arm's answer, and never did: the copied
    `if not wrote and mode.is_enforce` branch returned the same `_decision_for_error`
    as the `if mode.is_enforce` right after it, so the reason that reaches the caller is
    the error's own and not "audit write failed". Each caller's decision string, reason
    and return-or-continue are unchanged.
    """
    _record(conn, mode, event, attachment, "error", reason)
    if mode.is_enforce:
        return _decision_for_error(mode, name, reason)
    return None


def _audit_or_deny(conn, mode, event, attachment, name, decision,
                   reason) -> GateDeny | None:
    """Write the verdict's decision row; under enforcement a failed write is itself the
    deny because a gate that cannot record what it decided has not decided; under observe
    it is logged by `_record` and the verdict stands.

    Replaces the four identical checks in `policy_registry_gate` (abstain, allow, deny,
    ask). The decision strings written and the reason on the deny are unchanged.
    """
    if not _record(conn, mode, event, attachment, decision, reason) and mode.is_enforce:
        return _decision_for_error(mode, name, "policy decision audit write failed")
    return None


def policy_registry_gate(conn, mode: enforcement.GateMode,
                         event: policies.PolicyEvent) -> GateDecision:
    """Evaluate every applicable attachment and return the resulting gate decision."""
    loaded = repository.attachments_for_evaluation(
        conn, event.caller_owner_id, event.session_key, event.phase
    )
    # `attachments_for_evaluation` returns Rows | Empty - never a bare list - so "no
    # policy applies" is distinguishable from "the query did not run". An exception from
    # the query propagates rather than being caught into an empty set, because an
    # unreadable attachment table under enforcement must deny, and it does so through
    # the view's 500 rather than by silently evaluating nothing.
    if isinstance(loaded, repository.Empty):
        log.debug("meshguard: no attachments in scope (%s)", loaded.scope)
        return GateAllow()
    attachments = loaded.items

    asks: list[PolicyAsk] = []
    for attachment in attachments:
        handler_ref = attachment["handler_ref"]
        name = attachment["policy_name"]

        if handler_ref not in policies.HANDLERS:
            denied = _error_arm(conn, mode, event, attachment, name,
                                f"unknown policy handler: {handler_ref}")
            if denied is not None:
                return denied
            continue

        try:
            verdict = policies.evaluate(handler_ref, attachment["params"], event)
        except (policies.PolicyEvaluationError, policies.PolicyParamsError) as exc:
            denied = _error_arm(conn, mode, event, attachment, name, str(exc))
            if denied is not None:
                return denied
            continue
        except Exception as exc:  # noqa: BLE001
            # An unexpected handler exception is an error verdict, not a crashed request
            # and not an allow.
            log.error("meshguard: handler %r raised %s: %s", handler_ref,
                      type(exc).__name__, exc)
            denied = _error_arm(conn, mode, event, attachment, name,
                                "policy handler raised")
            if denied is not None:
                return denied
            continue

        if isinstance(verdict, policies.Abstain):
            denied = _audit_or_deny(conn, mode, event, attachment, name, "abstain", None)
            if denied is not None:
                return denied
            continue

        if isinstance(verdict, policies.Allow):
            denied = _audit_or_deny(conn, mode, event, attachment, name, "allow",
                                    verdict.reason)
            if denied is not None:
                return denied
            continue

        if isinstance(verdict, policies.Deny):
            denied = _audit_or_deny(conn, mode, event, attachment, name, "deny",
                                    verdict.reason)
            if denied is not None:
                return denied
            if mode.is_enforce:
                return GateDeny(policy_name=name, reason=verdict.reason)
            continue

        if isinstance(verdict, policies.Ask):
            denied = _audit_or_deny(conn, mode, event, attachment, name, "ask",
                                    verdict.reason)
            if denied is not None:
                return denied
            if mode.is_enforce:
                asks.append(PolicyAsk(
                    policy_id=attachment["policy_id"],
                    attachment_id=attachment["attachment_id"],
                    policy_name=name,
                    reason=verdict.reason,
                ))
            continue

        # A verdict outside the closed set. Unreachable through `policies.evaluate`,
        # which only ever returns the four dataclasses; treated as an error rather than
        # falling off the end of the loop into an implicit allow.
        reason = f"policy handler returned an unknown verdict type {type(verdict).__name__}"
        denied = _error_arm(conn, mode, event, attachment, name, reason)
        if denied is not None:
            return denied

    if not mode.is_enforce:
        # Observe mode admits everything, by construction.
        return GateAllow()
    if not asks:
        return GateAllow()
    return GateRequiresApproval(asks=asks)
