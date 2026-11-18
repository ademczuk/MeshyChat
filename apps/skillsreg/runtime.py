"""The declared runtime vocabulary (TASK-556.3).

This module defines two wire structures, their canonicalization, stamping, and
write-side validation.

ONE RULE SPLITS THEM. `RuntimeProfile` is behaviour/security-DEFINING and is covered by
the manifest hash when present, so changing it trips a grant's version pin. `RuntimeStatus`
is operational state and is NEVER hash-covered; the SERVER stamps `lease_declared_at` and
a client-supplied timestamp never survives.

BOTH STRUCTS REJECT UNKNOWN KEYS. An unknown key is a shape failure that receives 422
before any service-layer code runs - it is not a validation error and not a silently-dropped
field. `parse_runtime_profile` therefore raises `RuntimeShapeError`, which `views.py`
maps to 422, while the validator below raises `RuntimeValidateError`, which maps to 400.
Collapsing the two would move a whole class of request from 422 to 400.

Errors name the offending FIELD PATH, never the offending VALUE: the redaction scan
exists to catch credentials, and echoing the match back into a log or an HTTP body would
defeat the control it implements.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re

from apps.peers.timefmt import format_rfc3339
from apps.core.env import lookup

log = logging.getLogger("meshychat.skillsreg")

#: Serialized cap per object.
MAX_RUNTIME_OBJ_BYTES = 8 * 1024

#: Recognized harness names.
WELL_KNOWN_HARNESS = ("claude_code", "codex", "cursor", "gemini", "grok", "kimi", "pi",
                      "qwen", "copilot", "hermes", "openclaw", "openai_agents",
                      "native_terminal", "service")
#: Recognized provider classes.
WELL_KNOWN_PROVIDER_CLASS = ("anthropic", "openai", "google", "xai", "moonshot",
                             "local", "mixed")

_AUTH_CLASS = ("none", "peer_signed", "gateway_profile", "direct_api_key",
               "named_provider", "human_delegated")
_SANDBOX_BACKEND = ("none", "linux_bwrap", "darwin_seatbelt", "windows_job_object",
                    "container", "vm")
_FILESYSTEM_SCOPE = ("none", "workdir_readonly", "workdir_write", "declared_paths",
                     "unrestricted")
_NETWORK_MODE = ("none", "proxy_allowlist", "unrestricted")
_TERMINAL_ACCESS = ("none", "readonly", "interactive")
_TOOL_INTERFACE = ("mcp", "function", "agent", "terminal")

# --- regexes ---------------------------------------------------------------
# Python's `$` also matches just before a final "\n". The three anchored grammars
# (_POLICY_TAG, _SLUG, _CUSTOM_SLUG) therefore use `fullmatch`, never `match`:
# with `match`, `custom:x\n` was a custom slug and `policy:x\n` a tag in either arm,
# while `codex\n` was a well-formed unknown slug stored with a warning. All three
# must be malformed (B09 flip prep, 2026-09-27). _JWT_SHAPE keeps `search` with its
# `$`, so a JWT followed by one newline is still refused, because strictness is the safe
# direction for a credential scan (DIVERGENCES 049).
_KEY_DENYLIST = re.compile(
    r"(secret|token|password|api[_-]?key|credential|authorization|bearer"
    r"|private[_-]?key|cookie|session[_-]?id)", re.IGNORECASE)
_DENYLIST_BENIGN_KEYS = ("credential_proxy",)
_ABS_PATH = re.compile(r"(^([A-Za-z]:[\\/]|/))|([\\/]Users[\\/])|(/home/)")
_URL_USERINFO = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s]+@")
_JWT_SHAPE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_CRED_PREFIX = re.compile(r"^(sk-|ghp_|github_pat_|xox[baprs]-|AKIA[0-9A-Z]{16}|-----BEGIN)")
_POLICY_TAG = re.compile(r"^[a-z0-9_]+:[a-z0-9_-]+$")
_SLUG = re.compile(r"^[a-z0-9_]+$")
_CUSTOM_SLUG = re.compile(r"^custom:[a-z0-9_]+$")


class RuntimeShapeError(ValueError):
    """Deserialization failure: unknown key, wrong JSON type, unknown enum variant.

    It never reaches the service layer and receives 422.
    """


class RuntimeValidateError(ValueError):
    """Runtime-vocabulary validation failure."""


def _invalid(reason: str) -> RuntimeValidateError:
    return RuntimeValidateError(f"runtime vocabulary invalid: {reason}")


def _redacted(reason: str) -> RuntimeValidateError:
    return RuntimeValidateError(
        f"runtime vocabulary rejected (secret-like or node-local content): {reason}")


def _enforce_vocab() -> bool:
    """True only when `MESHYCHAT_RUNTIME_VOCAB_ENFORCE` is exactly `1`, `true` or `TRUE`.

    Read at call time."""
    return lookup("MESHYCHAT_RUNTIME_VOCAB_ENFORCE") in ("1", "true", "TRUE")


# --------------------------------------------------------------- deserialization

def _opt_str(obj, key, path):
    v = obj.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise RuntimeShapeError(f"{path}.{key}: expected a string")
    return v


def _opt_bool(obj, key, path):
    v = obj.get(key)
    if v is None:
        return None
    if not isinstance(v, bool):
        raise RuntimeShapeError(f"{path}.{key}: expected a boolean")
    return v


def _opt_u32(obj, key, path):
    v = obj.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 0xFFFF_FFFF):
        raise RuntimeShapeError(f"{path}.{key}: expected a u32")
    return v


def _opt_enum(obj, key, path, allowed):
    v = obj.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or v not in allowed:
        raise RuntimeShapeError(
            f"{path}.{key}: unknown variant, expected one of {', '.join(allowed)}")
    return v


def _str_list(obj, key, path):
    v = obj.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or any(not isinstance(i, str) for i in v):
        raise RuntimeShapeError(f"{path}.{key}: expected an array of strings")
    return list(v)


def _enum_list(obj, key, path, allowed):
    out = _str_list(obj, key, path)
    for item in out:
        if item not in allowed:
            raise RuntimeShapeError(
                f"{path}.{key}: unknown variant {item!r}, expected one of {', '.join(allowed)}")
    return out


def _deny_unknown(obj, known, path):
    extra = [k for k in obj if k not in known]
    if extra:
        raise RuntimeShapeError(f"{path}: unknown field `{extra[0]}`")


_SANDBOX_KEYS = ("backend", "filesystem_scope", "network_mode", "credential_proxy",
                 "home_masked")
_SESSION_KEYS = ("supports_async", "cancellable", "supports_timers", "terminal_access",
                 "max_concurrent_sessions", "pass_history")
_PROFILE_KEYS = ("harness", "auth_class", "provider_class", "policy_tags",
                 "sandbox_constraints", "session_affordances", "tool_interfaces")
_STATUS_KEYS = ("harness_version", "model_refs", "lease_ttl_secs", "lease_declared_at",
                "host_label")


def _parse_sandbox(raw, path):
    if not isinstance(raw, dict):
        raise RuntimeShapeError(f"{path}: expected an object")
    _deny_unknown(raw, _SANDBOX_KEYS, path)
    return {
        "backend": _opt_enum(raw, "backend", path, _SANDBOX_BACKEND),
        "filesystem_scope": _opt_enum(raw, "filesystem_scope", path, _FILESYSTEM_SCOPE),
        "network_mode": _opt_enum(raw, "network_mode", path, _NETWORK_MODE),
        "credential_proxy": _opt_bool(raw, "credential_proxy", path),
        "home_masked": _opt_bool(raw, "home_masked", path),
    }


def _parse_session(raw, path):
    if not isinstance(raw, dict):
        raise RuntimeShapeError(f"{path}: expected an object")
    _deny_unknown(raw, _SESSION_KEYS, path)
    return {
        "supports_async": _opt_bool(raw, "supports_async", path),
        "cancellable": _opt_bool(raw, "cancellable", path),
        "supports_timers": _opt_bool(raw, "supports_timers", path),
        "terminal_access": _opt_enum(raw, "terminal_access", path, _TERMINAL_ACCESS),
        "max_concurrent_sessions": _opt_u32(raw, "max_concurrent_sessions", path),
        "pass_history": _opt_bool(raw, "pass_history", path),
    }


def parse_runtime_profile(raw):
    """Parse an optional runtime profile. `None` in returns `None`."""
    if raw is None:
        return None
    path = "runtime_profile"
    if not isinstance(raw, dict):
        raise RuntimeShapeError(f"{path}: expected an object")
    _deny_unknown(raw, _PROFILE_KEYS, path)
    sandbox = raw.get("sandbox_constraints")
    session = raw.get("session_affordances")
    return {
        "harness": _opt_str(raw, "harness", path),
        "auth_class": _opt_enum(raw, "auth_class", path, _AUTH_CLASS),
        "provider_class": _opt_str(raw, "provider_class", path),
        "policy_tags": _str_list(raw, "policy_tags", path),
        "sandbox_constraints": (None if sandbox is None
                                else _parse_sandbox(sandbox, f"{path}.sandbox_constraints")),
        "session_affordances": (None if session is None
                                else _parse_session(session, f"{path}.session_affordances")),
        "tool_interfaces": _enum_list(raw, "tool_interfaces", path, _TOOL_INTERFACE),
    }


def parse_runtime_status(raw):
    """Parse an optional runtime status.

    `lease_declared_at` is accepted on input and then DISCARDED by
    `stamp_status`; a client cannot backdate its own freshness lease.
    """
    if raw is None:
        return None
    path = "runtime_status"
    if not isinstance(raw, dict):
        raise RuntimeShapeError(f"{path}: expected an object")
    _deny_unknown(raw, _STATUS_KEYS, path)
    declared = raw.get("lease_declared_at")
    if declared is not None and not isinstance(declared, str):
        raise RuntimeShapeError(f"{path}.lease_declared_at: expected an RFC 3339 string")
    return {
        "harness_version": _opt_str(raw, "harness_version", path),
        "model_refs": _str_list(raw, "model_refs", path),
        "lease_ttl_secs": _opt_u32(raw, "lease_ttl_secs", path),
        "lease_declared_at": declared,
        "host_label": _opt_str(raw, "host_label", path),
    }


# ----------------------------------------------------------------- serialization

def _skip_absent(pairs):
    """Omit None values and empty lists in declaration order.

    The order is part of the wire rather than a detail."""
    return {k: v for k, v in pairs if not (v is None or (isinstance(v, list) and not v))}


def profile_to_wire(profile):
    if profile is None:
        return None
    sandbox = profile.get("sandbox_constraints")
    session = profile.get("session_affordances")
    return _skip_absent([
        ("harness", profile.get("harness")),
        ("auth_class", profile.get("auth_class")),
        ("provider_class", profile.get("provider_class")),
        ("policy_tags", profile.get("policy_tags") or []),
        ("sandbox_constraints",
         None if sandbox is None else _skip_absent([(k, sandbox.get(k)) for k in _SANDBOX_KEYS])),
        ("session_affordances",
         None if session is None else _skip_absent([(k, session.get(k)) for k in _SESSION_KEYS])),
        ("tool_interfaces", profile.get("tool_interfaces") or []),
    ])


def status_to_wire(status):
    if status is None:
        return None
    return _skip_absent([(k, status.get(k)) for k in _STATUS_KEYS])


# -------------------------------------------------------------------- normalize

def _sandbox_is_empty(s) -> bool:
    return s is None or all(s.get(k) is None for k in _SANDBOX_KEYS)


def _session_is_empty(s) -> bool:
    return s is None or all(s.get(k) is None for k in _SESSION_KEYS)


def profile_is_empty(profile) -> bool:
    """An all-absent profile is no declaration."""
    if profile is None:
        return True
    return (profile.get("harness") is None
            and profile.get("auth_class") is None
            and profile.get("provider_class") is None
            and not profile.get("policy_tags")
            and _sandbox_is_empty(profile.get("sandbox_constraints"))
            and _session_is_empty(profile.get("session_affordances"))
            and not profile.get("tool_interfaces"))


def normalized_profile(profile):
    """Return the canonical form, or None.

    Sorted+deduped `policy_tags`/`tool_interfaces`, all-absent sub-objects dropped, and
    an all-absent profile collapsed to None. The last part is the ABSENCE LAW that keeps
    a legacy skill's manifest hash byte-identical: if an
    "empty but declared" profile survived into the hash view, every pinned grant and
    every published attestation on a legacy skill would break at once.
    """
    if profile_is_empty(profile):
        return None
    sandbox = profile.get("sandbox_constraints")
    session = profile.get("session_affordances")
    return {
        "harness": profile.get("harness"),
        "auth_class": profile.get("auth_class"),
        "provider_class": profile.get("provider_class"),
        "policy_tags": sorted(set(profile.get("policy_tags") or [])),
        "sandbox_constraints": None if _sandbox_is_empty(sandbox) else sandbox,
        "session_affordances": None if _session_is_empty(session) else session,
        "tool_interfaces": sorted(set(profile.get("tool_interfaces") or [])),
    }


def stamp_status(status, now: _dt.datetime):
    """Set `lease_declared_at = now` iff a TTL is declared.

    A declared_at without a TTL is meaningless, so it is CLEARED rather than carried;
    and a client-supplied value never survives, which is the whole point.
    """
    if status is None:
        return None
    out = dict(status)
    out["lease_declared_at"] = (format_rfc3339(now)
                                if status.get("lease_ttl_secs") is not None else None)
    return out


# --------------------------------------------------------------------- validate

def _redaction_scan(value, path) -> None:
    """Walk keys and string values."""
    if isinstance(value, dict):
        for k, v in value.items():
            if _KEY_DENYLIST.search(k) and k not in _DENYLIST_BENIGN_KEYS:
                raise _redacted(f"{path}.{k}: key matches the secret-name denylist")
            _redaction_scan(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _redaction_scan(v, f"{path}[{i}]")
    elif isinstance(value, str):
        if _ABS_PATH.search(value):
            raise _redacted(
                f"{path}: absolute paths are node-local material, declare a class instead")
        if _URL_USERINFO.search(value):
            raise _redacted(f"{path}: URLs with userinfo can carry credentials")
        if _JWT_SHAPE.search(value):
            raise _redacted(f"{path}: JWT-shaped value")
        if _CRED_PREFIX.search(value):
            raise _redacted(f"{path}: credential-prefix-shaped value")


def _check_object(what, wire) -> None:
    raw = json.dumps(wire, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RUNTIME_OBJ_BYTES:
        raise _invalid(f"{what}: {len(raw)} bytes exceeds the {MAX_RUNTIME_OBJ_BYTES}-byte cap")
    _redaction_scan(wire, what)


def _check_vocab_slug(what, slug, well_known) -> None:
    """Validate a known or custom vocabulary slug.

    MALFORMED slugs hard-reject; well-formed but UNKNOWN slugs soft-warn unless
    MESHYCHAT_RUNTIME_VOCAB_ENFORCE is set. Getting that split backwards would either
    reject every peer running an unregistered harness, or accept `Kimi Code`.
    """
    if slug in well_known or _CUSTOM_SLUG.fullmatch(slug):
        return
    if not _SLUG.fullmatch(slug):
        raise _invalid(f"{what}: malformed slug (want lowercase [a-z0-9_]+ or custom:<slug>)")
    if _enforce_vocab():
        raise RuntimeValidateError(
            f"unknown {what} slug (set MESHYCHAT_RUNTIME_VOCAB_ENFORCE=0 to soft-allow): {slug}")
    log.warning(
        "runtime_vocab_unknown_slug (soft-warn; set MESHYCHAT_RUNTIME_VOCAB_ENFORCE=1 to reject): "
        "what=%s slug=%s", what, slug)


def validate_runtime_profile(profile) -> None:
    """Validate a runtime profile."""
    _check_object("runtime_profile", profile_to_wire(profile))
    harness = profile.get("harness")
    if harness is not None:
        _check_vocab_slug("harness", harness, WELL_KNOWN_HARNESS)
    provider = profile.get("provider_class")
    if provider is not None:
        _check_vocab_slug("provider_class", provider, WELL_KNOWN_PROVIDER_CLASS)
    for tag in profile.get("policy_tags") or []:
        if not _POLICY_TAG.fullmatch(tag):
            raise _invalid(
                "policy_tags: malformed tag (want namespace:slug, e.g. policy:cost_budget)")


def validate_runtime_status(status) -> None:
    """Validate a runtime status. `host_label` is a label, not an address."""
    _check_object("runtime_status", status_to_wire(status))
    label = status.get("host_label")
    if label is not None:
        # `all(c.is_ascii_digit())`, spelled against the ASCII set: `str.isdigit()`
        # is true for U+0660 and for superscripts, so it would let a non-ASCII
        # digits-only label through a check whose whole job is to reject one.
        if (not label or len(label) > 64 or "." in label or ":" in label
                or all("0" <= c <= "9" for c in label)):
            raise _invalid("runtime_status.host_label: a short label only "
                           "(no dots, colons, or digits-only)")
    for ref in status.get("model_refs") or []:
        if not ref.strip() or len(ref) > 128:
            raise _invalid("runtime_status.model_refs: entries must be non-empty model "
                           "names (<=128 chars)")


def validate_runtime_fields(profile, status) -> None:
    """Validate present runtime fields."""
    if profile is not None:
        validate_runtime_profile(profile)
    if status is not None:
        validate_runtime_status(status)
