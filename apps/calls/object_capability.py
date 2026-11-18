"""`Meshychat-Capability` on the envelope path: the view's half (B07 slice 6, docs/DIVERGENCES.md 040).

Design docs/design/biscuit-capabilities.md 3.1 (the envelope-path bullet), 3.2 ("Where each
refusal surfaces", critique C10) and 3.3 (transport plumbing). The two routes that carry a signed
call, native invoke (`apps/calls/views.py`) and A2A `message:send` (`apps/a2a/views.py`), are on
the header middleware's accept list (`apps/core/capability_header.ACCEPTED`), so a request
carrying the header reaches their views while tokens are on. Each view calls `presented_for_call`
BEFORE `pipeline.start_call`, so whatever it refuses is an HTTP 403 and starts no call: no
skill_calls row is written, and gate 8's step 4c never runs, so no nonce is checked or burned.

What it refuses, in order, each a 403 with the code in the error envelope:

  1. tokens not available (MESHY_CAPABILITIES off, or no root key store): E_CAPABILITY_UNAVAILABLE.
     The middleware answers this first while tokens are off; this is the view's own copy, so no
     path reaches the verifier without a key store.
  2. a call to any peer but the memory authority: E_CAPABILITY_NOT_ACCEPTED. A token is decided
     only by the memory handlers, in-process and terminal inside `start_call` (design 3.3), so a
     call that would be delivered elsewhere must not carry one: it is refused, never forwarded
     and never dropped (the cross-track flag the skill-frames branch raised, `fin/skill-frames`,
     decided here for this server).
  3. the verifier's steps 1 and 3 (`capability.precheck`): the token's size and parse under the
     kid provider, every signature, and the holder walk, whose final holder must be the
     authenticated caller. E_CAPABILITY_INVALID, E_CAPABILITY_HOLDER, or E_CAPABILITY_DENIED for a
     delegation already past its own expiry.

What passes is a `PresentedCapability(token, skill)`, carried by `StartCallInput.object_capability`
(never `capability_token`, the skill-grant gate 1: design 3.3, Q5) to `route._execute`,
`handlers.dispatch` and the handler's `principal`. The skill is the route's, which step 4b binds
to the signed payload, and `handlers._bind_envelope_to_the_call` binds the token to the signed
payload by its hash (`capability_sha256`). Steps 4 and 5 (liveness, the per-object decision) are
the handler's, and surface as a signed failed result.

No refusal holds the token or any part of it: the messages are the verifier's own, which never
quote a token (T14), and this module logs nothing of it.

WITH TOKENS OFF NOTHING HERE IS REACHED: a request without the header returns None before any
capability module is imported, and one with it never gets past the middleware.
"""
from __future__ import annotations

from apps.calls import route as routing
from apps.core import capability_header, rls
from apps.peers.errors import ApiError

STATUS = 403
UNAVAILABLE = "E_CAPABILITY_UNAVAILABLE"
NOT_ACCEPTED = "E_CAPABILITY_NOT_ACCEPTED"


def presented_for_call(request, conn, *, caller_peer_id, target_peer_id, skill_name: str,
                       memory_peer_id):
    """The token this request presents for a call, pre-checked, or None when it presents none.
    Raises ApiError(403, code) for every refusal above. Call it before `start_call`, with the
    AUTHENTICATED caller, the path's target and skill, and the memory authority the call's
    `CallsContext` names."""
    if not capability_header.carries_capability(request.META):
        return None
    from apps.memoryskills import capability, capability_keys
    from apps.memoryskills.errors import HandlerError

    if not (capability_header.tokens_on() and capability_keys.current() is not None):
        raise ApiError(STATUS, UNAVAILABLE, "capability tokens are not available on this server")
    if not routing.is_memory_authority(target_peer_id, memory_peer_id):
        raise ApiError(STATUS, NOT_ACCEPTED,
                       "a capability is taken only on a call to the memory authority, whose "
                       "skills decide it in-process; this call's target is another peer")
    token = capability_header.presented(request.META)
    principal = capability.PresentedCapability(token, str(skill_name))
    try:
        with rls.server_internal(conn):
            capability.precheck(conn, principal, caller_peer_id)
    except HandlerError as exc:
        raise ApiError(STATUS, exc.code, exc.message) from None
    return principal


__all__ = ["presented_for_call"]
