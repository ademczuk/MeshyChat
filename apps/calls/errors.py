"""Call-pipeline errors, carrying stable wire codes and HTTP statuses.

These are contracts, not implementation detail: a client branches on
`error.code`, so a rename here is a breaking wire change.
"""
from __future__ import annotations

# --- wire error codes -------------------------------------------------------
E_SIG_BAD = "E_SIG_BAD"
E_SIG_REPLAY = "E_SIG_REPLAY"
E_SIG_KEY_REVOKED = "E_SIG_KEY_REVOKED"
E_SIG_IDENTITY_MISMATCH = "E_SIG_IDENTITY_MISMATCH"
E_SIG_KIND_MISMATCH = "E_SIG_KIND_MISMATCH"
E_SIG_VERSION_UNSUPPORTED = "E_SIG_VERSION_UNSUPPORTED"
E_NOT_FOUND = "E_NOT_FOUND"
E_FORBIDDEN = "E_FORBIDDEN"
E_VALIDATION = "E_VALIDATION"
E_CONFLICT = "E_CONFLICT"
E_INTERNAL = "E_INTERNAL"
E_FILE_ATTACHMENT_INVALID = "E_FILE_ATTACHMENT_INVALID"


class RpcError(Exception):
    """Engine-level error. `code` and `http_status` are the wire contract."""

    code = E_INTERNAL
    http_status = 500

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class ValidationFailed(RpcError):
    code = E_VALIDATION
    http_status = 400


class NumberOutOfRange(ValidationFailed):
    """A memory.write payload holding a number beyond 2^53-1 (divergence 039, design Q4).

    Refused at intake, before the call row, because `skill_calls.args` stores the payload
    as JSONB, which returns 1e16 as the integer 10**16, and the idempotency check then
    cannot re-canonicalise it: the retry of such a call answered HTTP 500. The code is the
    one the memory handler uses for the same refusal on the board and in-process paths."""
    code = "E_NUMBER_OUT_OF_RANGE"
    http_status = 400


class SkillNotFound(RpcError):
    """A missing skill maps to a not-found response."""
    code = E_NOT_FOUND
    http_status = 404


class NotAuthorized(RpcError):
    """An authorization failure renders as 403 E_FORBIDDEN.

    The `reason` string is NOT returned to the client; the response has the fixed message
    "forbidden".
    `self.message` is therefore a SERVER-SIDE reason only; do not surface it.
    """
    code = E_FORBIDDEN
    http_status = 403


class RateLimited(RpcError):
    """The caller's per-peer bucket is empty (B12, docs/DIVERGENCES.md 003; apps/core/peer_limit).

    429 uses the code the sign-in limiter already answers, and
    `retry_after` (whole seconds), which the HTTP rendering carries as `Retry-After`."""
    code = "E_RATE_LIMITED"
    http_status = 429

    def __init__(self, message: str = "", *, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class IdempotencyConflict(RpcError):
    code = E_CONFLICT
    http_status = 409


class AttachmentsNotPermitted(RpcError):
    code = E_FILE_ATTACHMENT_INVALID
    http_status = 400


class SignatureFailed(RpcError):
    """Envelope verification failure. The code/status pair varies per reason, so
    unlike every other error here it is set per-instance.

    A signature, replay, revoked-key or identity failure is 401 (the CALLER is not who they
    claim), while a kind/version/malformed failure is 400 (the REQUEST is broken).
    """

    def __init__(self, reason: str, code: str = E_SIG_BAD, http_status: int = 401):
        super().__init__(reason)
        self.code = code
        self.http_status = http_status

    @classmethod
    def sig_bad(cls, detail: str = "signature bad"):
        return cls(detail, E_SIG_BAD, 401)

    @classmethod
    def replay(cls, window: int):
        return cls(f"envelope replay window exceeded (issued_at outside +/- {window} seconds)",
                   E_SIG_REPLAY, 401)

    @classmethod
    def key_revoked(cls):
        return cls("signing key revoked", E_SIG_KEY_REVOKED, 401)

    @classmethod
    def identity_mismatch(cls):
        return cls("identity mismatch: payload peer_id != JWT subject",
                   E_SIG_IDENTITY_MISMATCH, 401)

    @classmethod
    def kind_mismatch(cls, expected: str, actual: str):
        return cls(f"kind mismatch: expected {expected}, got {actual}",
                   E_SIG_KIND_MISMATCH, 400)

    @classmethod
    def version_unsupported(cls, v):
        return cls(f"envelope version {v} not supported (supported: 1)",
                   E_SIG_VERSION_UNSUPPORTED, 400)

    @classmethod
    def malformed(cls, detail: str):
        # Malformed envelopes carry E_SIG_BAD with status 400.
        return cls(f"malformed envelope: {detail}", E_SIG_BAD, 400)
