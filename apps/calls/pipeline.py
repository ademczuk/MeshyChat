"""`start_call` - the security core (spec 7.2).

THE ORDER OF THE GATES IS THE SECURITY PROPERTY. Each placement has a reason:

  0. per-peer limit       B12, apps/core/peer_limit; before any database work
                           (docs/DIVERGENCES.md 003)
  1. validate call_id      before it is logged or persisted
  2. idempotency check     a retry must not re-authorize
  3. resolve skill         the RESOLVED version is what gets pinned
  4. VERIFY the envelope   AUTHENTICATION
     4b. rebind to request confused-deputy guard
     4c. nonce ledger      gate 8, replay: CHECKED here, recorded with the row at 8,
                           or burned on a refusal (docs/DIVERGENCES.md 036)
  5. AUTHORIZE             grant lookup
  6. deadline
  7. effective mode
  8. INSERT skill_calls
  9. route                 apps/calls/route.py: memory authority in-process, any other target
                           routed or queued (docs/DIVERGENCES.md 017)

WHY 4 BEFORE 5 IS NOT NEGOTIABLE, AND THE INVARIANT THIS FILE PRESERVES:

    The grant request is built from the AUTHENTICATED identity and the token is
    CHECKED against it, never used to build it.

    Authentication establishes WHO the caller is. Only then is a grant looked up FOR
    that identity. Reversing them - looking up a grant named by the request and then
    checking a signature against whatever identity that grant mentions - is the
    classic vacuous verification: the credential answers the question it was supposed
    to be tested against. `authorize.authorize()` resolves the caller owner itself
    from the authenticated `caller_peer_id` and exposes no parameter through which a
    caller could nominate a different tenancy (see apps/calls/authorize.py).

WHY 4c AFTER 4: the nonce is read from the SIGNED payload. Recording it before the
signature is proven would let anyone burn a nonce they never signed and DENY the
legitimate request that later carries it.

WHY 4b EXISTS AT ALL: the signed envelope commits to call_id,
target_peer_id and skill_name, but the HTTP route takes those from the path and body.
Without re-binding the VERIFIED payload to the effective request tuple, a captured
valid envelope can be replayed as a DIFFERENT routed invocation - a valid signature
for call X pointed at skill Y. The WS path derives them from the payload, so this is a
no-op there and closes the asymmetric HTTP gap.

STEP 9 (docs/DIVERGENCES.md 017, and 041 S5). A call to the memory authority executes
in-process and completes or fails with a server-signed result, inside the call_id lock, so
a concurrent retry of the same call_id waits and then reads the terminal row as a replay
instead of executing twice. A call to any other peer is `routed` when that peer holds a
socket in some worker and `queued` otherwise, and the target's worker delivers it
(apps/wsgw/call_notify.py); it is in flight when `start_call` returns.

This file's accept/reject path does not perform concurrent-call quota admission,
attachment manifest validation, invocation-frame chain anchoring, human-auth consent,
flow guard, tag policy, or policy-registry evaluation.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import uuid
from dataclasses import dataclass, field

from apps.calls import authorize as authz
from apps.calls import nonce_ledger, repository
from apps.calls import route as routing
from apps.calls.envelope import (KIND_SKILL_CALL, SignedEnvelope, peer_has_usable_key,
                                 verify_payload)
from apps.calls.errors import (AttachmentsNotPermitted, NotAuthorized, NumberOutOfRange,
                               RateLimited, RpcError, SkillNotFound, ValidationFailed)
from apps.core import peer_limit
from apps.files import service as files_service
from apps.crypto import primitives as P
from apps.crypto.ijson import MAX_SAFE_INTEGER, unsafe_number_path, untextable_string_path

log = logging.getLogger("meshychat.calls")

TRANSPORT_HTTP = "http"
TRANSPORT_WS = "ws"
TRANSPORT_DASHBOARD_TEST = "dashboard_test"

#: The one skill whose payload the intake refuses numbers in (step 1b, divergence 039). Its
#: rows are the ones a memory.read must re-canonicalise; the other skills' calls keep the
#: ordinary intake (numbers accepted), and a retry of one carrying such a number still fails at
#: step 2.
MEMORY_WRITE = "memory.write"
#: Divergence 059: memory.edit carries a whole message value, as memory.write does, so the number seam applies to it too.
MEMORY_EDIT = "memory.edit"


@dataclass
class CallsContext:
    """What the pipeline needs. `conn` is an AUTOCOMMIT psycopg connection that has
    already done `SET ROLE meshychat_app` - both are required by apps/core/rls.py, and
    without the role the RLS policies simply do not apply."""
    conn: object
    # Setting: MESHYCHAT_ENVELOPE_NONCE_ENFORCE.
    # Default ON (the project's design log (not part of this release) 036), and "off" means the gate is NOT CALLED (a missing
    # nonce is then accepted for pre-nonce signers) rather than called-and-ignored -
    # REQUIRED and keyword-only, like memory_peer_id below: it had a default of False
    # here and in both views, and the gate was off everywhere. Every
    # transport passes `nonce_ledger.enforce_from_settings()`.
    envelope_nonce_enforce: bool = field(kw_only=True)
    # Step 9's routing decision (the project's design log (not part of this release) 017): calls to this peer execute
    # in-process. REQUIRED and keyword-only on purpose: every constructor states which
    # peer is the memory authority, from settings.MESHY_MEMORY_PEER_ID, so a transport
    # that forgot to pass it fails at construction instead of silently routing against
    # a default that an operator's setting was meant to replace.
    memory_peer_id: uuid.UUID = field(kw_only=True)


@dataclass
class StartCallInput:
    """Input fields needed to start a call."""
    caller_peer_id: uuid.UUID          # THE AUTHENTICATED IDENTITY (JWT subject)
    target_peer_id: uuid.UUID          # from the route path
    skill_name: str                    # from the route path
    envelope: SignedEnvelope
    transport: str = TRANSPORT_HTTP
    call_id: str | None = None
    version_pin: str | None = None
    mode_override: str | None = None
    capability_token: str | None = None   # Native invoke sends None; see capability.py
    args_attachments: list = field(default_factory=list)
    webhook_url: str | None = None
    webhook_secret_hash: str | None = None
    # An OBJECT capability (the project's design log (not part of this release) 040, slice 6; design B07 3.3): the
    # `apps.memoryskills.capability.PresentedCapability` the view read from `Meshychat-Capability`
    # and pre-checked (apps/calls/object_capability.py), or None. NOT `capability_token`
    # above, which is the SKILL-grant gate 1 and still raises (Q5). Step 9 hands it to
    # the memory handler as its `principal`; it is never persisted: every call that carries
    # one is terminal inside `start_call`, and the view refuses one on a call to any other
    # target, so no queued or delivered call ever holds it.
    object_capability: object | None = None


@dataclass
class StartCallResult:
    """The stored and routing result of a started call."""
    call_id: str
    status: str
    skill_version: str
    mode: str
    deadline_at: _dt.datetime
    replayed: bool
    grant_id: uuid.UUID | None = None


def effective_mode(skill_mode: str, override_mode: str | None, max_runtime_ms: int) -> str:
    """A sync call may not be forced onto a skill whose runtime
    exceeds the 30s sync ceiling - the override would create a call that cannot
    possibly complete inline."""
    chosen = override_mode or skill_mode
    if chosen == "sync" and max_runtime_ms > 30_000:
        raise ValidationFailed(
            f"invalid mode override: sync not allowed for skill mode {skill_mode}")
    return chosen


@contextlib.contextmanager
def _call_id_lock(conn, call_id: str):
    """Serialize the whole pipeline on `call_id`, because
    `idempotency_check` is a plain unlocked SELECT: two concurrent retries can both
    see "new call" and each consume a unit of a single-execution grant before either
    commits the row that would make the second resolve as a replay.

    The request connection takes the SESSION-scoped `pg_advisory_lock` and releases it
    in `finally`. Release is explicit rather than tied to a transaction rollback, which
    is why the `finally` is not optional.
    """
    key_conn = conn
    with key_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (call_id,))
    try:
        yield
    finally:
        with key_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (call_id,))


def start_call(ctx: CallsContext, inp: StartCallInput) -> StartCallResult:
    conn = ctx.conn

    # --- Step 0: the per-peer rate limit (B12, the project's design log (not part of this release) 003) ----
    # First, so every transport that starts a call takes the same bucket before any
    # database work, keyed on the AUTHENTICATED caller (the JWT subject), never on a value
    # the request chose. A retry of a finished call_id is charged too: a loop is a loop.
    try:
        peer_limit.admit(inp.caller_peer_id, transport=inp.transport)
    except peer_limit.PeerRateLimited as exc:
        raise RateLimited(exc.message, retry_after=exc.retry_after) from None

    # --- Step 1: call_id ---------------------------------------------------
    # A caller-supplied key is VALIDATED before anything touches it; a
    # server-generated UUID needs no check.
    if inp.call_id is not None:
        try:
            call_id = repository.validate_call_id(inp.call_id)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
    else:
        call_id = str(uuid.uuid4())

    with _call_id_lock(conn, call_id):
        return _start_call_inner(ctx, inp, call_id)


def _start_call_inner(ctx: CallsContext, inp: StartCallInput,
                      call_id: str) -> StartCallResult:
    conn = ctx.conn
    now = _dt.datetime.now(_dt.timezone.utc)

    # --- Step 1a: text no later step can carry (#252) ---------------------
    # Every skill, before anything hashes or stores the payload: step 2's jcs raises on a
    # lone surrogate and the call row cannot hold U+0000, and either answered HTTP 500. The
    # message names the JSONPath, never the string.
    if isinstance(inp.envelope.payload, dict):
        path = untextable_string_path(inp.envelope.payload)
        if path is not None:
            raise ValidationFailed(
                f"{path} holds U+0000 or a lone surrogate, which no stored copy can carry "
                "(I-JSON, RFC 7493 section 2.1)")

    # --- Step 1b: the memory.write number seam (divergence 039, design Q4) ----
    # Before the call row, because the row stores this payload as JSONB: a float at or
    # beyond 2**53 comes back as an integer rfc8785 refuses, so step 2 of a RETRY could
    # not re-hash it and answered 500, and no memory.read of the written row could be
    # signed. Before step 2 also because an INTEGER beyond 2**53 - 1 makes step 2's own
    # jcs raise. The handler refuses the same payloads on the paths that skip this one.
    if inp.skill_name in (MEMORY_WRITE, MEMORY_EDIT) and isinstance(inp.envelope.payload, dict):
        path = unsafe_number_path(inp.envelope.payload)
        if path is not None:
            raise NumberOutOfRange(
                f"{path} is a number beyond +/-{MAX_SAFE_INTEGER} (2^53-1): RFC 8785 cannot "
                "carry it once it is stored (I-JSON, RFC 7493)")

    # --- Step 2: idempotency ----------------------------------------------
    # The hash is over the canonical ENVELOPE PAYLOAD, which is
    # also what lands in skill_calls.args - the two must agree or every retry 409s.
    canonical = P.jcs(inp.envelope.payload)
    existing = repository.idempotency_check(conn, call_id, canonical, inp.caller_peer_id)
    if existing is not None:
        return StartCallResult(call_id=existing["call_id"], status=existing["status"],
                               skill_version=existing["skill_version"],
                               mode=existing["mode"],
                               deadline_at=existing["deadline_at"], replayed=True)

    # --- Step 3: resolve skill --------------------------------------------
    skill = repository.resolve_for_call(conn, inp.target_peer_id, inp.skill_name,
                                        inp.version_pin)

    # --- Step 4: AUTHENTICATION -------------------------------------------
    # Everything after this point may rely on `verified`; nothing before it may.
    # DashboardTest is the one exemption (spec 6.4): an
    # owner-internal call authenticated by the dashboard JWT that crosses no privilege
    # boundary. It is enumerated, not inferred - anything else must be signed.
    verified = None
    nonce_claim = None
    if inp.transport != TRANSPORT_DASHBOARD_TEST:
        verified = verify_payload(conn, inp.envelope, inp.caller_peer_id,
                                  KIND_SKILL_CALL, now=now)

        # --- Step 4b: bind the VERIFIED payload to the effective request ----
        # Compare against `verified.payload`, which is only reachable once the
        # signature checks out.
        if verified.payload.get("call_id") != call_id:
            raise NotAuthorized(
                "signed envelope call_id does not match the request call_id")
        if verified.payload.get("target_peer_id") != str(inp.target_peer_id):
            raise NotAuthorized(
                "signed envelope target_peer_id does not match the request target")
        if verified.payload.get("skill_name") != inp.skill_name:
            raise NotAuthorized(
                "signed envelope skill_name does not match the request skill")

        # --- Step 4c: gate 8, the envelope nonce ledger --------------------
        # Checked here, RECORDED later: with the call row at step 8, or on its own if
        # a later step refuses. A request that fails before its call row commits leaves
        # no row, so the same-bytes retry a client sends after a 5xx is not a replay
        # (apps/calls/nonce_ledger.py "WHEN THE CLAIM COMMITS"; divergence 036).
        if ctx.envelope_nonce_enforce:
            try:
                nonce_claim = nonce_ledger.check_nonce(conn, verified)
            except (nonce_ledger.NonceReplay, nonce_ledger.NonceMissing) as exc:
                raise _nonce_refusal(exc, call_id, verified.peer_id) from exc

    try:
        return _authorize_and_insert(ctx, inp, call_id, now, skill, verified, nonce_claim)
    except nonce_ledger.NonceReplay as exc:
        # The claim lost at step 8 to a presentation of the same (peer, nonce) that got
        # past its own step 4c check first. Its transaction rolled back: no call.
        raise _nonce_refusal(exc, call_id, verified.peer_id) from exc
    except RpcError as exc:
        # REFUSED after step 4c: burn the nonce, so these bytes are a replay from now
        # on (the grant may exist a second later). A 5xx is a failure, not a refusal,
        # and burns nothing.
        if nonce_claim is not None and exc.http_status < 500:
            nonce_ledger.burn_nonce(conn, nonce_claim)
        raise


def _nonce_refusal(exc: Exception, call_id: str, peer_id) -> NotAuthorized:
    """The typed refusal for the wire, and its reason for the operator. The wire renders
    every NotAuthorized as the same fixed 403, so a replay, a
    missing nonce and a missing grant look alike to the client; this line is where the
    reason is kept."""
    err = nonce_ledger.to_rpc_error(exc)
    log.warning("gate 8 refused call %s from peer %s: %s", call_id, peer_id, err.message)
    return err


def _authorize_and_insert(ctx: CallsContext, inp: StartCallInput, call_id: str, now,
                          skill: dict, verified, nonce_claim) -> StartCallResult:
    """Steps 5 to 9, after authentication. `nonce_claim` is step 4c's, None when the gate
    is off or the transport is exempt; step 8 writes it with the call row."""
    conn = ctx.conn

    # --- Step 5: AUTHORIZE -------------------------------------------------
    # INVARIANT: the grant request is
    # built from the AUTHENTICATED identity and the token is CHECKED against it, never
    # used to build it. `inp.caller_peer_id` is the JWT subject, and step 4 has just
    # proved the envelope was signed by that same peer's active key.
    target_owner_id = authz.lookup_owner_for_peer(conn, inp.target_peer_id)
    caller_owner_id = authz.lookup_owner_for_peer(conn, inp.caller_peer_id)
    auth = authz.authorize(conn, caller_peer_id=inp.caller_peer_id,
                           target_owner_id=target_owner_id,
                           target_peer_id=inp.target_peer_id, skill=skill,
                           capability_token=inp.capability_token)

    # Attachment admission (spec 8.2). First the skill-level gate:
    # attachments on a skill that declared none are refused.
    if inp.args_attachments and not skill["attachments_allowed"]:
        raise AttachmentsNotPermitted(
            f"skill {skill['name']} v{skill['version']}: attachments_allowed=false but "
            f"{len(inp.args_attachments)} supplied")
    # Then each file (divergence 087, issue #122): the caller may attach only a file it could
    # already download (files.service.readable_row). A call's reference is itself rule 2 of
    # the download check, so without this, naming another tenant's file id made both the
    # caller and the target its readers. A file the caller holds through an earlier call it
    # took part in may be passed on, since the caller could re-upload those bytes anyway. A
    # missing file and an unreadable one get the same refusal, naming only the position.
    for i, file_id in enumerate(inp.args_attachments):
        if files_service.readable_row(conn, file_id, inp.caller_peer_id, caller_owner_id) is None:
            raise AttachmentsNotPermitted(
                f"args_attachments[{i}] is not a file the caller may read")

    # --- Steps 6 + 7: deadline and effective mode -------------------------
    deadline_at = repository.deadline_for(skill, now)
    max_runtime_ms = skill.get("max_runtime_ms") or repository.DEFAULT_MAX_RUNTIME_MS
    mode = effective_mode(skill["mode"], inp.mode_override, max_runtime_ms)

    # --- Step 8: insert ----------------------------------------------------
    # The signature and kid are recorded ALONGSIDE the payload so the stored row is
    # independently re-verifiable later; `args` holds the whole signed payload
    # (see repository.insert_call). The nonce claim is written in the same
    # transaction, so the two commit together or not at all.
    row = repository.insert_call(
        conn,
        call_id=call_id,
        caller_peer_id=inp.caller_peer_id,
        caller_owner_id=caller_owner_id,
        target_peer_id=inp.target_peer_id,
        target_owner_id=target_owner_id,
        skill_id=skill["id"],
        skill_name=skill["name"],
        skill_version=skill["version"],
        mode=mode,
        transport=inp.transport,
        payload=inp.envelope.payload,
        args_signature=inp.envelope.signature if verified is not None else None,
        args_signing_kid=inp.envelope.signing_kid if verified is not None else None,
        args_attachments=inp.args_attachments,
        grant_id=auth.grant_id,
        deadline_at=deadline_at,
        webhook_url=inp.webhook_url,
        webhook_secret_hash=inp.webhook_secret_hash,
        nonce_claim=nonce_claim,
    )

    # --- Step 9: route -----------------------------------------------------
    # the project's design log (not part of this release) 017 and 041 S5: executed in-process and signed when the target
    # is the memory authority; `routed` or `queued` for any other target (failed typed if
    # nobody verified the envelope). `verified` and `inp.caller_peer_id` are the
    # AUTHENTICATED values; the router is handed nothing a caller chose after step 4.
    status = routing.route(
        conn, memory_peer_id=ctx.memory_peer_id, call_id=row["call_id"],
        caller_peer_id=inp.caller_peer_id, target_peer_id=inp.target_peer_id,
        skill_name=skill["name"], envelope=inp.envelope, verified=verified,
        verified_at=now, principal=inp.object_capability)
    return StartCallResult(call_id=row["call_id"], status=status,
                           skill_version=row["skill_version"], mode=row["mode"],
                           deadline_at=row["deadline_at"], replayed=False,
                           grant_id=row["grant_id"])


#: Why `refusal_without_envelope` refused, one per gate, in `start_call`'s order.
REFUSED_SKILL_NOT_FOUND = "skill_not_found"              # step 3, SkillNotFound, 404
REFUSED_NO_USABLE_KEY = "no_usable_signing_key"          # step 4's key lookup, 401
REFUSED_NOT_AUTHORIZED = "not_authorized"                # step 5, NotAuthorized, 403


def refusal_without_envelope(conn, *, caller_peer_id, target_peer_id, skill_name: str,
                             caller_key: bytes | None = None) -> str | None:
    """The gates of `start_call` that can be decided with no envelope in hand, in its
    order, for a surface that shows what a call would return without making one: the
    context anchor's body, which is shown only when memory.read without a capability would
    be (docs/DIVERGENCES.md 027; 040 slice 6: no surface that calls this takes a token). None
    when every one passes, else which one refused.

      step 3  the skill resolves on the target (current, not soft-deleted)
      step 4  the caller holds a key `verify_payload` would accept a signature under
              (`envelope.peer_has_usable_key`: its checks 5 and 6, no kid named); with
              `caller_key`, the key the request proved it holds must be that key
      step 5  `authorize`: same tenant or dev trust group, or an active skill grant
              whose version pin permits the resolved version

    These call the functions `start_call` calls, so the two cannot drift. What needs the
    envelope itself (the signature, its freshness, the rebind, the nonce) cannot be
    decided here and is not claimed; proof that the caller holds the key is the
    request's to give (a DPoP proof, apps/peers/auth.require_bound_proof).
    """
    try:
        skill = repository.resolve_for_call(conn, target_peer_id, skill_name, None)
    except SkillNotFound:
        return REFUSED_SKILL_NOT_FOUND
    if not peer_has_usable_key(conn, caller_peer_id, public_key=caller_key):
        return REFUSED_NO_USABLE_KEY
    try:
        target_owner_id = authz.lookup_owner_for_peer(conn, target_peer_id)
        authz.authorize(conn, caller_peer_id=caller_peer_id,
                        target_owner_id=target_owner_id, target_peer_id=target_peer_id,
                        skill=skill, capability_token=None)
    except NotAuthorized:
        return REFUSED_NOT_AUTHORIZED
    return None


__all__ = ["CallsContext", "StartCallInput", "StartCallResult", "start_call",
           "effective_mode", "refusal_without_envelope", "RpcError"]
