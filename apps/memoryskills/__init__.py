"""memory.* skills: write, read, query, list, grant, prune (+ namespace.claim).

This package provides the skill handlers behind a clean interface.

Usage:

    from apps.memoryskills import db, handlers
    conn = db.connect()                      # autocommit, SET ROLE meshychat_app
    handlers.set_bootstrap_authority(peer)   # MUST-1; unset fails CLOSED
    handlers.dispatch("memory.write", verified_payload, caller_peer_id, conn)
"""
from importlib import import_module

from .errors import VALID_ERROR_CODES, HandlerError
from .permissions import NAMESPACE_WIDE, Key, Prefix, check_permission

_HANDLER_EXPORTS = frozenset({
    "DISPATCH",
    "dispatch",
    "handle_grant",
    "handle_list",
    "handle_namespace_claim",
    "handle_prune",
    "handle_query",
    "handle_read",
    "handle_write",
    "prime_roster_labels",
    "set_bootstrap_authority",
})

__all__ = [
    "HandlerError",
    "VALID_ERROR_CODES",
    "DISPATCH",
    "dispatch",
    "check_permission",
    "Key",
    "Prefix",
    "NAMESPACE_WIDE",
    "handle_write",
    "handle_read",
    "handle_query",
    "handle_list",
    "handle_grant",
    "handle_prune",
    "handle_namespace_claim",
    "prime_roster_labels",
    "set_bootstrap_authority",
]


def __getattr__(name: str):
    if name in _HANDLER_EXPORTS:
        handlers = import_module(".handlers", __name__)
        return getattr(handlers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
