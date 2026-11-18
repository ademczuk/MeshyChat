"""Gate 1: the biscuit capability-token seam. DELIBERATELY NOT IMPLEMENTED.

WHY THERE IS NOTHING HERE. Gate 1 is dormant:

  * `authorize` runs it only when a `capability_token` is passed. The HTTP, A2A, and WebSocket
    call paths all pass None, making gate 2, `find_active_grant`, the sole authorization gate.
  * Minting is refused when `MESHYCHAT_BISCUIT_ROOT_SK` is set (apps/grants/service.py).

Writing a stub that returned True would silently authorize any token the day someone starts
sending one, and it would look tested. Raising keeps the seam FAIL-CLOSED and loud.

WHAT A REAL IMPLEMENTATION OWES (do not weaken these when filling it in):

  * The biscuit is verified OFFLINE against the root public key and checked against a
    `GrantRequest` the caller built from AUTHENTICATED identity - it is never used to
    build that request. `verify_grant_biscuit` below takes the request as a parameter for
    exactly that reason.
  * A token that fails verification is a DENY, not a fall-through to gate 2
    (returns NotAuthorized).
  * The token's predicate must match `find_active_grant`'s live predicate - current
    `expires_at` AND `revoked_at IS NULL` - or an offline token outlives the grant it
    represents.
"""
from __future__ import annotations


class BiscuitMintingNotConfigured(NotImplementedError):
    """Raised by the dormant gate-1 seam. Distinct type so a caller can tell
    "this deploy has no capability tokens" from a genuine bug."""


def verify_grant_biscuit(token: str, request) -> bool:  # noqa: ARG001
    """Gate 1. Never reached while callers send `capability_token=None`.

    `request` is the authenticated-identity `GrantRequest` the token is CHECKED
    AGAINST. It is a parameter, not something this function derives from `token`.
    """
    raise BiscuitMintingNotConfigured(
        "biscuit capability tokens are not enabled: MESHYCHAT_BISCUIT_ROOT_SK is unset and "
        "callers send capability_token=None. Gate 2 find_active_grant is "
        "the sole authorization gate. Implement offline verify against the root key "
        "here, checked against the caller-built GrantRequest - never built from the "
        "token."
    )


def mint_grant_biscuit(*args, **kwargs):  # noqa: ARG001
    """The issuance half of the same dormant seam."""
    raise BiscuitMintingNotConfigured(
        "biscuit minting requires MESHYCHAT_BISCUIT_ROOT_SK, which is unset."
    )
