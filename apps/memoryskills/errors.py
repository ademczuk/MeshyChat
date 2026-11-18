"""Canonical memory-skill error codes and the typed handler exception.

The registry is CLOSED on purpose. `HandlerError.__init__` maps an unregistered
code to E_INTERNAL, which stops a typo'd code from escaping to a caller as though
it were a documented contract. Coercing instead of raising keeps every mistyped
code a generic internal fault rather than a 500.
"""
from __future__ import annotations

E_PERMISSION_DENIED = "E_PERMISSION_DENIED"
E_NAMESPACE_UNCLAIMED = "E_NAMESPACE_UNCLAIMED"
E_DISCIPLINE_VIOLATION = "E_DISCIPLINE_VIOLATION"
E_REPLAY = "E_REPLAY"
E_BAD_ENVELOPE = "E_BAD_ENVELOPE"
E_UNKNOWN_SKILL = "E_UNKNOWN_SKILL"
E_INVALID_ARGS = "E_INVALID_ARGS"
E_INTERNAL = "E_INTERNAL"

E_UNKNOWN_PEER = "E_UNKNOWN_PEER"
E_PROVENANCE_KEY = "E_PROVENANCE_KEY"
E_BAD_REQUEST = "E_BAD_REQUEST"
E_NAMESPACE_ALREADY_CLAIMED = "E_NAMESPACE_ALREADY_CLAIMED"
E_NOT_BOOTSTRAP_AUTHORITY = "E_NOT_BOOTSTRAP_AUTHORITY"
E_INVALID_STATUS = "E_INVALID_STATUS"
E_NEW_KEY_BAD_STATUS = "E_NEW_KEY_BAD_STATUS"
E_PRE_DISCIPLINE_TRANSITION = "E_PRE_DISCIPLINE_TRANSITION"
E_NO_PROPOSED_FIX = "E_NO_PROPOSED_FIX"
E_WRITE_CONFLICT = "E_WRITE_CONFLICT"
E_UNKNOWN_KEY = "E_UNKNOWN_KEY"
E_INVALID_SINCE = "E_INVALID_SINCE"
E_INVALID_FIELDS = "E_INVALID_FIELDS"
E_WIKI_NO_TAGS = "E_WIKI_NO_TAGS"
E_WIKI_NO_CONFIRMER = "E_WIKI_NO_CONFIRMER"
E_WIKI_SELF_CONFIRM = "E_WIKI_SELF_CONFIRM"
E_PRODUCT_NO_SHARED_WRITE = "E_PRODUCT_NO_SHARED_WRITE"
E_PRODUCT_NO_SHARED_GRANT = "E_PRODUCT_NO_SHARED_GRANT"
E_UNBOUNDED_CROSS_GRANT = "E_UNBOUNDED_CROSS_GRANT"
E_BAD_KEY_TS = "E_BAD_KEY_TS"
E_MESH_SCHEMA = "E_MESH_SCHEMA"

# Divergence 028: without author checks, any writer could name any author or append a version
# of any key. A shared/mesh-chat msg/ row is attributed to the peer that signed it. Registered
# here because the registry is closed, and an unregistered code reaches the caller as E_INTERNAL.
E_AUTHOR_MISMATCH = "E_AUTHOR_MISMATCH"
E_FROM_MISMATCH = "E_FROM_MISMATCH"
E_KEY_LABEL_MISMATCH = "E_KEY_LABEL_MISMATCH"
E_NOT_MESSAGE_AUTHOR = "E_NOT_MESSAGE_AUTHOR"

# Divergence 039: a number beyond 2^53-1 anywhere in a memory.write payload is refused
# because a read of the row could not be signed (design G1). The strict types reuse the
# already-registered E_INVALID_ARGS and E_INVALID_STATUS.
E_NUMBER_OUT_OF_RANGE = "E_NUMBER_OUT_OF_RANGE"

# Divergence 040, slice 4: object-capability tokens (design docs/design/biscuit-capabilities.md
# 3.2, 4.3). Registered here because the registry is closed and an unregistered code reaches
# the caller as E_INTERNAL. apps/memoryskills/capability.py raises them;
# E_CAPABILITY_UNAVAILABLE is also the refusal of apps/core/capability_header.py.
E_CAPABILITY_INVALID = "E_CAPABILITY_INVALID"
E_CAPABILITY_HOLDER = "E_CAPABILITY_HOLDER"
E_CAPABILITY_REVOKED = "E_CAPABILITY_REVOKED"
E_CAPABILITY_DENIED = "E_CAPABILITY_DENIED"
E_CAPABILITY_NOT_ACCEPTED = "E_CAPABILITY_NOT_ACCEPTED"
E_CAPABILITY_UNAVAILABLE = "E_CAPABILITY_UNAVAILABLE"

# Divergence 059: edit of a mail row (docs/design/edit.md). Registered here because the
# registry is closed and an unregistered code reaches the caller as E_INTERNAL.
# E_USE_MEMORY_EDIT is memory.write's refusal of a later version of a mail key (the author's
# included); E_MESSAGE_REDACTED is memory.edit's refusal of a redacted head.
E_USE_MEMORY_EDIT = "E_USE_MEMORY_EDIT"
E_MESSAGE_REDACTED = "E_MESSAGE_REDACTED"

# Divergence 091: decision cards (docs/design/decision-cards.md 4). Registered here because
# the registry is closed and an unregistered code reaches the caller as E_INTERNAL.
# apps/cards/validation.py raises them from memory.write.
E_CARD_KEY = "E_CARD_KEY"
E_CARD_SCHEMA = "E_CARD_SCHEMA"
E_CARD_TIME = "E_CARD_TIME"
E_CARD_DIGEST = "E_CARD_DIGEST"
E_CARD_SEAT_MISMATCH = "E_CARD_SEAT_MISMATCH"
E_CARD_NOT_AUTHOR = "E_CARD_NOT_AUTHOR"
E_CARD_SUPERSEDES = "E_CARD_SUPERSEDES"
E_CARD_REDACTED = "E_CARD_REDACTED"
E_CARD_FROZEN = "E_CARD_FROZEN"
E_CARD_LOADER = "E_CARD_LOADER"
E_CARD_UNCHANGED = "E_CARD_UNCHANGED"

# context.v1 (docs/design/context-v1.md 5-8). Registered for the same reason as the card codes. apps/context raises
# them, from memory.write; E_CONTEXT_REDACT_UNAVAILABLE from memory.redact. The writer turns ONLY E_CONTEXT_SECRET and
# E_CONTEXT_CLIENT_NAME into an omitted item. Retried unchanged: E_CONTEXT_SCREEN_UNAVAILABLE, and a manifest refused
# with E_CONTEXT_MANIFEST_PREDECESSOR or E_CONTEXT_MANIFEST_ITEM_MISSING (a write it follows has not committed yet).
# E_CONTEXT_MANIFEST_ORDER and E_CONTEXT_MANIFEST are final: the manifest itself is wrong, and a resend fails the same.
E_CONTEXT_KEY = "E_CONTEXT_KEY"
E_CONTEXT_SCHEMA = "E_CONTEXT_SCHEMA"
E_CONTEXT_TIME = "E_CONTEXT_TIME"
E_CONTEXT_DIGEST = "E_CONTEXT_DIGEST"
E_CONTEXT_TOO_LARGE = "E_CONTEXT_TOO_LARGE"
E_CONTEXT_TEXT_ENCODING = "E_CONTEXT_TEXT_ENCODING"
E_CONTEXT_SEAT_MISMATCH = "E_CONTEXT_SEAT_MISMATCH"
E_CONTEXT_WRITE_ONCE = "E_CONTEXT_WRITE_ONCE"
E_CONTEXT_MANIFEST = "E_CONTEXT_MANIFEST"
E_CONTEXT_MANIFEST_ORDER = "E_CONTEXT_MANIFEST_ORDER"
E_CONTEXT_MANIFEST_PREDECESSOR = "E_CONTEXT_MANIFEST_PREDECESSOR"
E_CONTEXT_MANIFEST_ITEM_MISSING = "E_CONTEXT_MANIFEST_ITEM_MISSING"
E_CONTEXT_SECRET = "E_CONTEXT_SECRET"
E_CONTEXT_CLIENT_NAME = "E_CONTEXT_CLIENT_NAME"
E_CONTEXT_SCREEN_UNAVAILABLE = "E_CONTEXT_SCREEN_UNAVAILABLE"
E_CONTEXT_REDACT_UNAVAILABLE = "E_CONTEXT_REDACT_UNAVAILABLE"
# the directive rule (seat directives; threat_model.md, amendment of 2 Nov 2026): the kill switch is off, a seat sent more than
# its hourly allowance, or a redaction asked for a message whose directive is the audit record.
E_FEATURE_OFF = "E_FEATURE_OFF"
E_RATE_LIMITED = "E_RATE_LIMITED"
E_DIRECTIVE_IMMUTABLE = "E_DIRECTIVE_IMMUTABLE"

VALID_ERROR_CODES = {
    value
    for name, value in tuple(globals().items())
    if name.startswith("E_") and isinstance(value, str)
}


class HandlerError(Exception):
    """Caller-safe domain error shared by the dispatcher and its handlers."""

    def __init__(self, code: str, message: str):
        self.code = code if code in VALID_ERROR_CODES else E_INTERNAL
        self.message = message
        super().__init__(f"{self.code}: {message}")
