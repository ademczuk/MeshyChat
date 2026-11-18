"""The error surface for peer routes.

The body shape is `{"error": <code>, "message": <text>}`. Clients branch on (status, code), so
each constructor uses its assigned pairing.
"""
from __future__ import annotations

# Exact strings, not paraphrases.
E_NOT_FOUND = "E_NOT_FOUND"
E_FORBIDDEN = "E_FORBIDDEN"
E_INTERNAL = "E_INTERNAL"
E_VALIDATION = "E_VALIDATION"
E_CONFLICT = "E_CONFLICT"
E_PEER_NAME_UNAVAILABLE = "E_PEER_NAME_UNAVAILABLE"  # Divergence 065
E_AUTH_MISSING = "E_AUTH_MISSING"
E_AUTH_INVALID = "E_AUTH_INVALID"
E_PUBLIC_KEY_INVALID = "E_PUBLIC_KEY_INVALID"
E_SIG_BAD = "E_SIG_BAD"
E_SIG_REPLAY = "E_SIG_REPLAY"


class ApiError(Exception):
    """A response-shaped error. `status` and `code` are the wire contract.

    THE ONE CLASS FOR EVERY SURFACE. `apps/files`, `apps/skillsreg`, `apps/meshguard`,
    `apps/calls`, `apps/a2a`, `apps/grants` and `apps/dashboard` import it rather than
    declaring their own. Two classes of one shape is how a wrapper ends up catching one and
    500-ing on the other (`apps/skillsreg/errors.py` tells that story), and it is why the
    owner routes in `apps/peers/views.py` once needed `except (a.ApiError, b.ApiError)`
    and why `apps/core/jsonerrors.py` recognises the type by NAME: the grants and dashboard
    copies, byte-identical to this one, were folded in here on 2026-10-02. Their
    surface-specific constructors stayed in their own modules.
    """

    def __init__(self, status: int, code: str, message: str, detail: str | None = None):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        #: Server-side only, never serialized: `internal()` puts the real cause here so it
        #: reaches the log without reaching the client. `body()` does not read it.
        self.detail = detail

    def body(self) -> dict:
        return {"error": self.code, "message": self.message}


# --- plain variants -------------------------------------------------------------

def not_found() -> ApiError:
    return ApiError(404, E_NOT_FOUND, "not found")


def forbidden() -> ApiError:
    return ApiError(403, E_FORBIDDEN, "forbidden")


def validation(message: str) -> ApiError:
    return ApiError(400, E_VALIDATION, message)


def conflict(message: str) -> ApiError:
    return ApiError(409, E_CONFLICT, message)


def name_unavailable(message: str) -> ApiError:
    """Divergence 065: the peer name is taken or reserved (apps/peers/names.py). The message never
    says by whom."""
    return ApiError(409, E_PEER_NAME_UNAVAILABLE, message)


def auth_missing() -> ApiError:
    return ApiError(401, E_AUTH_MISSING, "missing auth")


def auth_invalid(message: str) -> ApiError:
    return ApiError(401, E_AUTH_INVALID, message)


def internal(message: str) -> ApiError:
    # Log the detail and return the CONSTANT "internal error" to the
    # client. Leaking `message` here would disclose server detail, so the
    # detail stays on the exception for logs and never reaches `body()`.
    err = ApiError(500, E_INTERNAL, "internal error")
    err.detail = message
    return err


# --- key service errors ---------------------------------------------------------
# Key proof failures use E_SIG_BAD at either 400 for malformed input or 401 for a failed
# signature or required co-signature. The status distinguishes those cases.

def invalid_public_key() -> ApiError:
    return ApiError(400, E_PUBLIC_KEY_INVALID, "invalid public key")


def sig_bad_400(message: str) -> ApiError:
    return ApiError(400, E_SIG_BAD, message)


def sig_bad_401(message: str) -> ApiError:
    return ApiError(401, E_SIG_BAD, message)


def sig_replay(message: str) -> ApiError:
    return ApiError(401, E_SIG_REPLAY, message)
