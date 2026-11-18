"""Policy handlers, the builtin seeds, and attachment-param validation.

The three handlers and their `params_schema` values are defined together because the
schema is published on `GET /v1/meshguard/policies` and a client generates its attachment
body from it.

The verdict vocabulary is:
Abstain | Allow{reason?} | Deny{reason} | Ask{reason}. It is a CLOSED set - a handler
that cannot decide returns Abstain, never None, and never "allow because nothing
matched". Absence is typed here for the same reason it is typed in `repository.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import cel

# --------------------------------------------------------------------- verdicts


@dataclass(frozen=True)
class Abstain:
    """This policy has no opinion about this event."""


@dataclass(frozen=True)
class Allow:
    reason: str | None = None


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class Ask:
    reason: str


Verdict = Abstain | Allow | Deny | Ask


class PolicyParamsError(ValueError):
    """Params do not satisfy the handler's schema. 400 E_VALIDATION at the route."""


class PolicyEvaluationError(RuntimeError):
    """The handler could not reach a verdict for this event.

    The gate turns it into a Deny under enforcement and records an `error` decision row
    under observe. It is NEVER swallowed into an allow - that is the whole point of the
    type.
    """


# ------------------------------------------------------------------------ event


@dataclass(frozen=True)
class PolicyEvent:
    """The policy event, including the five values exposed to CEL."""

    phase: str
    skill_name: str
    input_args: Any
    caller_owner_id: str
    caller_peer_id: str
    target_owner_id: str | None = None
    target_peer_id: str | None = None
    call_id: str | None = None
    manifest_hash: str | None = None
    session_key: str | None = None

    def cel_variables(self) -> dict:
        """Return exactly these five CEL bindings, with these names."""
        return {
            "skill": self.skill_name,
            "phase": self.phase,
            "caller_peer": str(self.caller_peer_id),
            "target_peer": None if self.target_peer_id is None else str(self.target_peer_id),
            "args": self.input_args,
        }


# --------------------------------------------------------------------- handlers


def _match_verdict(on_match: str, reason: str) -> Verdict:
    """Turn a matching action into its verdict."""
    if on_match == "allow":
        return Allow(reason=reason)
    if on_match == "deny":
        return Deny(reason=reason)
    if on_match == "ask":
        return Ask(reason=reason)
    raise PolicyParamsError(
        f"on_match must be one of allow, deny, ask; got {on_match!r}"
    )


def _expression_params(params: Any) -> tuple[str, str, str]:
    """Validate the required expression parameters.

    The published schema says `additionalProperties: false`, so an extra key is refused
    here. A params body that carries a key the handler ignores is a policy whose author
    believes something is being enforced that is not.
    """
    if not isinstance(params, Mapping):
        raise PolicyParamsError("invalid expression params: not an object")
    missing = [k for k in ("expression", "on_match", "reason") if k not in params]
    if missing:
        raise PolicyParamsError(f"invalid expression params: missing {', '.join(missing)}")
    extra = sorted(set(params) - {"expression", "on_match", "reason"})
    if extra:
        raise PolicyParamsError(
            f"invalid expression params: unknown key(s) {', '.join(extra)} "
            "(params_schema declares additionalProperties: false)"
        )
    expression, on_match, reason = params["expression"], params["on_match"], params["reason"]
    if not isinstance(expression, str):
        raise PolicyParamsError("invalid expression params: expression must be a string")
    if not isinstance(on_match, str):
        raise PolicyParamsError("invalid expression params: on_match must be a string")
    if not isinstance(reason, str):
        raise PolicyParamsError("invalid expression params: reason must be a string")
    if on_match not in ("allow", "deny", "ask"):
        raise PolicyParamsError(
            f"invalid expression params: on_match must be allow|deny|ask, got {on_match!r}"
        )
    return expression, on_match, reason


def expression_handler(params: Any, event: PolicyEvent) -> Verdict:
    """Evaluate an expression policy.

    Bool TRUE  -> the configured on_match verdict
    Bool FALSE -> Abstain
    anything else, or any compile/eval error -> PolicyEvaluationError

    The non-bool message is deliberately value-free: formatting the result can leak arg
    values into the persisted deny reason and the invocation frame, and the deny reason IS persisted to
    `meshy_guard_policy_decisions.reason`. The same restraint is applied to the
    compile/eval error text, which names the construct but never the data.
    """
    expression, on_match, reason = _expression_params(params)
    try:
        program = cel.compile_expression(expression)
    except cel.CelError as exc:
        raise PolicyEvaluationError(str(exc)) from exc
    try:
        result = program.execute(event.cel_variables())
    except cel.CelError as exc:
        raise PolicyEvaluationError(str(exc)) from exc
    if result is True:
        return _match_verdict(on_match, reason)
    if result is False:
        return Abstain()
    raise PolicyEvaluationError("expression policy returned a non-bool result")


def wildcard_match(pattern: str, value: str) -> bool:
    """Match simple globs: `*` only, no character classes.

    `fnmatch` also honors `?`, `[seq]` and `[!seq]`, so a pattern containing a literal
    `?` or `[` needs this narrower matcher.
    """
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == value
    parts = pattern.split("*")
    cursor = 0
    index = 0
    if not pattern.startswith("*"):
        first = parts[0]
        if not value.startswith(first):
            return False
        cursor = len(first)
        index = 1
    last_index = len(parts) - 1
    while index < len(parts):
        part = parts[index]
        is_last = index == last_index
        if part == "":
            index += 1
            continue
        if is_last and not pattern.endswith("*"):
            pos = value.rfind(part, cursor)
            if pos < 0:
                return False
            return value[pos:].endswith(part)
        pos = value.find(part, cursor)
        if pos < 0:
            return False
        cursor = pos + len(part)
        index += 1
    return pattern.endswith("*") or cursor == len(value)


def _skill_name_ask_params(params: Any) -> tuple[list[str], str]:
    if not isinstance(params, Mapping):
        raise PolicyParamsError("invalid skill_name_ask params: not an object")
    patterns = params.get("patterns")
    reason = params.get("reason")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise PolicyParamsError(
            "invalid skill_name_ask params: patterns must be an array of strings"
        )
    if not patterns:
        raise PolicyParamsError("skill_name_ask params.patterns must be non-empty")
    if not isinstance(reason, str) or not reason.strip():
        raise PolicyParamsError("skill_name_ask params.reason must be non-empty")
    return patterns, reason


def skill_name_ask_handler(params: Any, event: PolicyEvent) -> Verdict:
    """Request approval when a configured pattern matches the skill name."""
    patterns, reason = _skill_name_ask_params(params)
    if any(wildcard_match(p, event.skill_name) for p in patterns):
        return Ask(reason=reason)
    return Abstain()


def deny_all_sessions_handler(params: Any, event: PolicyEvent) -> Verdict:
    """Deny every session. internal_only, so it is not
    discoverable and cannot be attached through the public route; it exists for tests and
    operator-scoped blocks."""
    if not isinstance(params, Mapping):
        raise PolicyParamsError("deny_all_sessions params must be an object")
    return Deny(reason="deny_all_sessions")


#: Policy handler registry, keyed by `meshy_guard_policies.handler_ref`.
HANDLERS = {
    "expression": expression_handler,
    "skill_name_ask": skill_name_ask_handler,
    "deny_all_sessions": deny_all_sessions_handler,
}


def validate_attachment_params(handler_ref: str, params: Any) -> None:
    """Validate attachment parameters. An UNKNOWN handler fails closed.

    For `expression` this also COMPILES the expression, so an out-of-subset construct is
    refused at attach time with a 400 rather than surfacing as a runtime error on the first
    gated call.
    """
    if handler_ref == "expression":
        expression, _on_match, _reason = _expression_params(params)
        try:
            cel.compile_expression(expression)
        except cel.CelError as exc:
            raise PolicyParamsError(str(exc)) from exc
        return
    if handler_ref == "skill_name_ask":
        _skill_name_ask_params(params)
        return
    if handler_ref == "deny_all_sessions":
        if not isinstance(params, Mapping):
            raise PolicyParamsError("deny_all_sessions params must be an object")
        return
    raise PolicyParamsError(f"unknown policy handler: {handler_ref}")


def evaluate(handler_ref: str, params: Any, event: PolicyEvent) -> Verdict:
    """Dispatch. An unknown handler_ref RAISES rather than abstaining.

    A missing registry entry is an `error` decision and, under enforcement, a deny.
    Abstaining would mean a policy row pointing at a handler this build does not have
    would be silently skipped.
    """
    handler = HANDLERS.get(handler_ref)
    if handler is None:
        raise PolicyEvaluationError(f"unknown policy handler: {handler_ref}")
    return handler(params, event)


# --------------------------------------------------------------------- the seeds
# `params_schema` is published verbatim on GET /v1/meshguard/policies, so it is data, not
# documentation.

BUILTIN_POLICY_SEEDS = [
    {
        "name": "expression",
        "kind": "expression",
        "handler_ref": "expression",
        "version": 1,
        "description": "Evaluate a side-effect-free CEL expression against the call event.",
        "internal_only": False,
        "params_schema": {
            "type": "object",
            "required": ["expression", "on_match", "reason"],
            "properties": {
                "expression": {"type": "string"},
                "on_match": {"type": "string", "enum": ["allow", "deny", "ask"]},
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "skill_name_ask",
        "kind": "builtin",
        "handler_ref": "skill_name_ask",
        "version": 1,
        "description": (
            "Require approval when the skill name matches one of the configured patterns."
        ),
        "internal_only": False,
        "params_schema": {
            "type": "object",
            "required": ["patterns", "reason"],
            "properties": {
                "patterns": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "deny_all_sessions",
        "kind": "builtin",
        "handler_ref": "deny_all_sessions",
        "version": 1,
        "description": "Internal emergency brake used by tests and operator-scoped blocks.",
        "internal_only": True,
        "params_schema": {"type": "object", "additionalProperties": False},
    },
]
