"""Domain rules for grants and access requests.

The order of checks is part of the API: several conditions differ only in which error
the caller sees first.

WHAT THIS MODULE REFUSES TO DO, AND WHY IT SAYS SO OUT LOUD

  * BISCUIT CAPABILITY TOKENS ARE NOT MINTED. The capability-minting path is deploy-dark
    (`MESHYCHAT_BISCUIT_ROOT_SK` unset means `biscuit_token` is absent, so the response
    body is byte-identical to a bare grant). If a root key
    IS configured, issuance is refused rather than returning a response
    that silently lacks the token the caller was promised. The seam is named here and
    nowhere else.

THE THREE TRANSACTIONS. `approve` must insert the grant and flip the request in ONE
transaction: the flip is guarded by `AND status = 'pending'`, so when a concurrent
decision wins the race the transaction is ABANDONED and the grant it just inserted
rolls back with it. Without that, a denied or withdrawn request can end up with a live
grant attached - which is a live authorisation nobody approved. Writes run inside
`rls.peer_context(owner_id=…)`: on this surface no policy requires it (neither table
has RLS - measured, see db.py), but it costs one statement, it makes the acting owner
the bound identity for the write, and it is the transaction the approve path needs
anyway. If `sql_meshychat/001`'s ownership-bound INSERT rule is ever extended to these
tables, these writes already satisfy it instead of starting to fail.
"""
from __future__ import annotations

import datetime as _dt
import logging
import uuid

from apps.core import rls
from apps.crypto.primitives import b64u_decode
from apps.skillsreg import attestation as skill_attestation

from . import config, delegation, intent, manifest, repository
from .repository import Empty, Found, NotFound
from .scopes import ScopeError, has_flow_accept_origin_scope, scope_set_from_json

log = logging.getLogger("meshychat.grants")

#: Access requests expire after 14 days by default.
DEFAULT_REQUEST_TTL = _dt.timedelta(days=14)

VALID_SOURCES = ("marketplace_subscription", "direct", "approved_request",
                 "public_auto", "delegated")
VALID_STATUSES = ("pending", "approved", "denied", "expired", "withdrawn")


# ------------------------------------------------------------------ domain errors
# One class per grant or access-request failure. views.py holds the mapping table to
# (status, code), keeping the API mapping auditable instead of scattering it through
# handlers.

class DomainError(Exception):
    """Base. `str(self)` is put in the body verbatim for conflict and validation
    responses and never for forbidden or internal ones."""


class GrantNotFound(DomainError):
    def __init__(self):
        super().__init__("grant not found")


class NotGrantor(DomainError):
    def __init__(self):
        super().__init__("only the grantor can revoke this grant")


class AlreadyRevoked(DomainError):
    def __init__(self):
        super().__init__("grant already revoked")


class DuplicateActive(DomainError):
    def __init__(self):
        super().__init__("active grant already exists for (grantor, grantee, peer, skill_name)")


class WideningDelegation(DomainError):
    def __init__(self, cause: str):
        super().__init__(f"delegated grant does not attenuate its source_ref parent: {cause}")


class MandateViolation(DomainError):
    def __init__(self, cause: str):
        super().__init__(f"grant exceeds the signed mandate: {cause}")


class ManifestPinFailed(DomainError):
    def __init__(self, cause: str):
        super().__init__(f"manifest pin failed: {cause}")


class ManifestAttestationFailed(DomainError):
    def __init__(self, cause: str):
        super().__init__(f"manifest attestation verification failed: {cause}")


class FlowScopeNotAuthorized(DomainError):
    def __init__(self, cause: str):
        super().__init__(
            f"flow accept-origin scope is not authorized for this grant issuance: {cause}")


class RequestNotFound(DomainError):
    def __init__(self):
        super().__init__("request not found")


class NotRequester(DomainError):
    def __init__(self):
        super().__init__("only the requester can withdraw")


class NotGranter(DomainError):
    def __init__(self):
        super().__init__("only the granter can approve or deny")


class NotPending(DomainError):
    def __init__(self, status: str):
        # Capitalized in the message (`Expired`, not `expired`); clients match this text,
        # so keep it.
        super().__init__(f"request status is {status.capitalize()}; expected pending")


class DuplicatePending(DomainError):
    def __init__(self):
        super().__init__("pending request already exists for this (requester, peer, skill)")


class CapabilityMintingUnavailable(DomainError):
    """MESHYCHAT_BISCUIT_ROOT_SK is set. See the module docstring: refuse, never omit."""
    def __init__(self):
        super().__init__(
            "biscuit mint failed: capability minting is configured "
            "(MESHYCHAT_BISCUIT_ROOT_SK is set) but this server cannot mint")


# ------------------------------------------------------------------- helpers

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _write_context(conn, owner_id):
    """A real transaction with `app.current_owner_id` bound. See module docstring."""
    return rls.peer_context(conn, owner_id=str(owner_id))


def _capture_requires_human_auth(conn, peer_id, skill_name, version_pin) -> bool:
    """Capture whether the resolved skill requires human authorization.

    Legacy and direct issuance may encounter an unresolved skill; in that narrow case,
    migration 074's safe default is false. The fallback is logged and never hides a write
    failure.
    """
    try:
        outcome = repository.find_skill_for_call(conn, peer_id, skill_name, version_pin)
    except ValueError as exc:  # semver-range pin
        log.debug("could not resolve skill for approved_requires_human_auth capture; "
                  "defaulting false: %s", exc)
        return False
    if isinstance(outcome, NotFound):
        log.debug("could not resolve skill %s/%s for approved_requires_human_auth capture; "
                  "defaulting false (%s)", peer_id, skill_name, outcome.key)
        return False
    return bool(outcome.value["requires_human_auth"])


def _resolve_manifest_pin(conn, granter_peer_id, skill_name, version_pin):
    """Resolve, verify, and hash before insertion so failure prevents the grant.

    Returns (version_pin, hash32) or (None, None). Issuance and re-pinning share this
    path. Verification runs whatever the attestation flag says (B09): a present
    signature pair is always verified, while the flag controls only whether an absent
    pair is refused. Skipping verification with the flag off would pin a grant over a
    forged or revoked-key signature. An unpinned grant, and a PATCH that clears a pin,
    are not verified (TASK-56; docs/DIVERGENCES.md 010).
    """
    if version_pin is None:
        return None, None
    try:
        outcome = repository.find_skill_for_call(conn, granter_peer_id, skill_name, version_pin)
    except ValueError as exc:
        raise ManifestPinFailed(f"resolve approved skill: {exc}") from exc
    if isinstance(outcome, NotFound):
        raise ManifestPinFailed(
            f"resolve approved skill: skill not found ({outcome.what}: {outcome.key})")
    skill = outcome.value
    try:
        skill_attestation.verify_manifest_attestation(
            conn, skill, config.manifest_attestation_enforce(),
            manifest_hash=manifest.manifest_hash)
    except (skill_attestation.AttestationRefused, manifest.ManifestError) as exc:
        # A hash failure inside the verifier is surfaced as an attestation failure too.
        raise ManifestAttestationFailed(str(exc)) from exc
    try:
        return version_pin, manifest.manifest_hash(skill)
    except manifest.ManifestError as exc:
        raise ManifestPinFailed(f"hash approved manifest: {exc}") from exc


def decode_mandate(raw):
    """Return (IntentMandate, 64-byte sig) or raise ValueError.

    Every failure is a 400 validation response, including malformed base64 and a
    signature whose length is not 64 bytes.
    """
    if not isinstance(raw, dict) or "mandate" not in raw or "sig" not in raw:
        raise ValueError("invalid mandate: missing field `mandate` or `sig`")
    try:
        mandate = intent.IntentMandate.from_json(raw["mandate"])
    except ValueError as exc:
        raise ValueError(f"invalid mandate: {exc}") from None
    sig_text = raw["sig"]
    if not isinstance(sig_text, str):
        raise ValueError("invalid mandate: `sig` must be a string")
    import base64
    try:
        # Mandate signatures use standard padded base64, not URL-safe base64.
        sig = base64.b64decode(sig_text.encode("ascii"), validate=True)
    except Exception:
        raise ValueError("mandate sig is not valid base64") from None
    if len(sig) != 64:
        raise ValueError("mandate sig must be 64 bytes")
    return mandate, sig


def verify_mandate_against_owner(conn, mandate, sig, owner_id) -> None:
    """Verify a mandate against its authenticated owner's active keys (TASK-84 / CRITICAL-1).

    Two independent reasons to deny, and BOTH are fail-closed:
      * the mandate's claimed human signer is not the authenticated grantor;
      * no ACTIVE registered human key of that owner verifies the signature. An EMPTY
        key set yields no match and therefore denies - "a mandate-present grant with no
        registered human key cannot be trusted".
    """
    if str(mandate.owner) != str(owner_id):
        raise PermissionError("mandate owner is not the authenticated grantor")
    keys = repository.active_human_keys(conn, owner_id)
    if isinstance(keys, Empty):
        raise PermissionError("no active registered human key for this owner")
    for _kid, public_key in keys.value:
        try:
            raw = b64u_decode(public_key)
        except Exception:
            # Skip an unusable key rather than failing the whole resolution. One bad row
            # must not deny a mandate a sibling key would verify.
            continue
        if len(raw) != 32:
            continue
        try:
            intent.verify_mandate(mandate, sig, raw)
            return
        except intent.MandateError:
            continue
    raise PermissionError("no registered human key verifies this mandate")


# --------------------------------------------------------------------- grants

def _require_attenuation(conn, source_ref, grantor_owner_id, granter_peer_id, skill_name,
                         expires_at, scopes, max_executions) -> None:
    """A delegated grant's two checks, shared by issuance and by an expiry PATCH: its
    `source_ref` names a LIVE parent, and the row (with the expiry it will have) is an
    attenuation of that parent. Each refusal is `WideningDelegation`, whose text is the 400
    body's message.

    A delegated row whose source_ref does not resolve to a live parent is incoherent:
    reject it rather than demoting it to a root issuance.
    """
    parent = None
    if source_ref:
        try:
            parent_id = uuid.UUID(str(source_ref))
        except (ValueError, TypeError, AttributeError):
            parent_id = None
        if parent_id is not None:
            found = repository.find_grant_by_id(conn, parent_id)
            parent = found.value if isinstance(found, Found) else None
    if parent is None:
        raise WideningDelegation(str(delegation.parent_not_live()))
    try:
        delegation.validate_delegation_provenance(
            parent, grantor_owner_id, granter_peer_id, skill_name, expires_at,
            scopes, max_executions, _now())
    except delegation.ProvenanceError as exc:
        raise WideningDelegation(str(exc)) from None


def issue_grant(conn, *, grantor_owner_id, grantee_owner_id, granter_peer_id, skill_name,
                version_pin, source, source_ref, expires_at, scopes, max_executions,
                mandate=None, mandate_sig=None):
    """Issue a grant after mandate, pinning, authorization, and uniqueness checks."""
    if config.biscuit_minting_enabled():
        raise CapabilityMintingUnavailable()

    # Step 1: reject a grant broader than the signed mandate before any write.
    if mandate is not None:
        if expires_at is None:
            raise MandateViolation(intent.EXPIRY_EXCEEDS_MANDATE)
        try:
            intent.mandate_authorizes(
                mandate, int(_now().timestamp()), grantee_owner_id, granter_peer_id,
                skill_name, sorted(scope_set_from_json(scopes)), int(expires_at.timestamp()))
        except intent.MandateError as exc:
            raise MandateViolation(str(exc)) from None

    # Step 2: pinned issuance resolves and hashes first, preventing unenforceable rows.
    pin, manifest_hash32 = _resolve_manifest_pin(conn, granter_peer_id, skill_name, version_pin)

    # --- base issuance ----------------------------------------------------------
    try:
        carries_flow = has_flow_accept_origin_scope(scopes)
    except ScopeError as exc:
        raise FlowScopeNotAuthorized(f"invalid flow scope: {exc}") from None
    if carries_flow and str(grantor_owner_id) == str(grantee_owner_id):
        raise FlowScopeNotAuthorized("self-grants cannot stamp meshy:flow:accept-origin scopes")

    if source == "delegated":
        _require_attenuation(conn, source_ref, grantor_owner_id, granter_peer_id, skill_name,
                             expires_at, scopes, max_executions)
    else:
        # ROOT issuance: the grantor must OWN the granter peer (spec 6.5). The DB
        # trigger `skill_grants_granter_ownership_guard_trg` enforces the same coupling
        # - this check exists so the answer is 403 rather than a 500 from a trigger.
        if not repository.peer_belongs_to_owner(conn, granter_peer_id, grantor_owner_id):
            raise NotGrantor()

    existing = repository.find_active_grant(
        conn, grantor_owner_id, grantee_owner_id, granter_peer_id, skill_name)
    if isinstance(existing, Found):
        raise DuplicateActive()

    requires_human_auth = _capture_requires_human_auth(conn, granter_peer_id, skill_name, pin)

    with _write_context(conn, grantor_owner_id):
        try:
            inserted = repository.insert_grant(
                conn,
                grantor_owner_id=grantor_owner_id, grantee_owner_id=grantee_owner_id,
                granter_peer_id=granter_peer_id, skill_name=skill_name,
                version_pin=pin, approved_manifest_hash=manifest_hash32,
                source=source, source_ref=source_ref, expires_at=expires_at,
                scopes=scopes, max_executions=max_executions,
                approved_requires_human_auth=requires_human_auth)
        except repository.DuplicateActiveGrant:
            # The pre-check above is a fast path and is NOT atomic with the insert;
            # the partial unique index is the authoritative guard.
            raise DuplicateActive() from None
        grant = inserted.value
        if mandate is not None:
            # Store provenance in the grant transaction so a crash cannot lose its link.
            repository.set_mandate_provenance(conn, grant.id, intent.mandate_hash(mandate),
                                              mandate_sig)
    if mandate is not None:
        refreshed = repository.find_grant_by_id(conn, grant.id)
        if isinstance(refreshed, Found):
            grant = refreshed.value
    return grant


def revoke_grant(conn, grant_id, revoker_owner_id, reason):
    """Revoke a live grant owned by the requesting owner."""
    found = repository.find_grant_by_id(conn, grant_id)
    if isinstance(found, NotFound):
        raise GrantNotFound()
    existing = found.value
    if str(existing.grantor_owner_id) != str(revoker_owner_id):
        raise NotGrantor()
    if existing.revoked_at is not None:
        raise AlreadyRevoked()
    with _write_context(conn, revoker_owner_id):
        revoked = repository.revoke_grant(conn, grant_id, revoker_owner_id, reason)
    if isinstance(revoked, NotFound):
        # Lost the race to a concurrent revoke; the row is revoked either way, and the
        # earlier reason is the one that stands.
        raise AlreadyRevoked()
    return revoked.value


def update_grant(conn, grant_id, owner_id, *, expires_at_present, new_expires_at,
                 version_pin_present, new_version_pin):
    """Update a grant. `*_present` means absent leaves the field unchanged.

    TWO TENANT LAYERS, EACH ENOUGH ON ITS OWN (STATUS C41). The owner check below runs
    for every PATCH shape, and `repository.update_grant` scopes its UPDATE by
    `grantor_owner_id` as well. The pre-read is needed so a PATCH that only clears the
    pin is not left with the WHERE clause as its only scoping. It costs one extra
    primary-key read and the response is 404 either way.
    """
    update_manifest_hash = False
    new_manifest_hash = None
    update_human_auth = False
    new_human_auth = False

    found = repository.find_grant_by_id(conn, grant_id)
    if isinstance(found, NotFound):
        raise GrantNotFound()
    existing = found.value
    if str(existing.grantor_owner_id) != str(owner_id):
        # NOT 403: NotFound is deliberate, so a PATCH cannot be used to probe for
        # another owner's grant ids.
        raise GrantNotFound()

    if expires_at_present:
        if repository.grant_is_mandate_bound(conn, grant_id):
            # TASK-29 AC#3: a mandate-bound grant's expiry is monotonically
            # non-increasing. The mandate is not stored to re-check, so widening -
            # including CLEARING to no-expiry - must be a re-issuance.
            if new_expires_at is None:
                widens = True
            elif existing.expires_at is None:
                widens = False
            else:
                widens = new_expires_at > existing.expires_at
            if widens:
                raise MandateViolation(intent.EXPIRY_EXCEEDS_MANDATE)
        if existing.source == "delegated":
            _require_attenuation(conn, existing.source_ref, existing.grantor_owner_id,
                                 existing.granter_peer_id, existing.skill_name,
                                 new_expires_at, existing.scopes, existing.max_executions)

    if version_pin_present:
        if new_version_pin is None:
            # Clearing the pin CLEARS the hash. Leaving a stale hash on an unpinned
            # grant bricks it: the invoke gate would hash the CURRENT version and
            # mismatch forever.
            update_manifest_hash, new_manifest_hash = True, None
        else:
            pin, h = _resolve_manifest_pin(conn, existing.granter_peer_id,
                                           existing.skill_name, new_version_pin)
            update_manifest_hash, new_manifest_hash = True, h
            # TASK-30 round-4: the hash and the human-auth flag are ONE approved
            # snapshot. Re-pinning without re-capturing the flag leaves a stale
            # `false` that skips the human-auth gate after a publisher drops it.
            update_human_auth = True
            new_human_auth = _capture_requires_human_auth(
                conn, existing.granter_peer_id, existing.skill_name, pin)

    with _write_context(conn, owner_id):
        updated = repository.update_grant(
            conn, grant_id, owner_id,
            update_expires_at=expires_at_present, new_expires_at=new_expires_at,
            update_version_pin=version_pin_present, new_version_pin=new_version_pin,
            update_manifest_hash=update_manifest_hash, new_manifest_hash=new_manifest_hash,
            update_approved_human_auth=update_human_auth,
            new_approved_human_auth=new_human_auth)
    if isinstance(updated, NotFound):
        raise GrantNotFound()
    return updated.value


def lookup_active_grant(conn, grantor_owner_id, grantee_owner_id, granter_peer_id, skill_name):
    """Return the active grant an authorization check consults.

    Returns Found(grant) or Empty(scope) - never a bare None, so a caller cannot mistake
    "this grant is revoked/expired" for "the query did not run".
    """
    return repository.find_active_grant(
        conn, grantor_owner_id, grantee_owner_id, granter_peer_id, skill_name)


def list_issued(conn, grantor_owner_id, include_revoked: bool, *, limit=None, before=None):
    return repository.list_issued(conn, grantor_owner_id, include_revoked,
                                  limit=limit, before=before)


def list_held(conn, grantee_owner_id, include_revoked: bool, *, limit=None, before=None):
    return repository.list_held(conn, grantee_owner_id, include_revoked,
                                limit=limit, before=before)


# ------------------------------------------------------------- access requests

def submit_request(conn, *, requester_owner_id, granter_owner_id, granter_peer_id,
                   skill_name, message, expires_at):
    """Submit an access request after validating the granter peer."""
    # Without this check a requester could pair granter_owner_id=B with a peer owned by
    # a third owner C, and B's approval would persist an inconsistent grant. A mismatch
    # is reported as 404, not 403.
    if not repository.peer_belongs_to_owner(conn, granter_peer_id, granter_owner_id):
        raise RequestNotFound()
    expires = expires_at if expires_at is not None else _now() + DEFAULT_REQUEST_TTL
    with _write_context(conn, requester_owner_id):
        try:
            inserted = repository.insert_request(
                conn, requester_owner_id=requester_owner_id,
                granter_owner_id=granter_owner_id, granter_peer_id=granter_peer_id,
                skill_name=skill_name, message=message, expires_at=expires)
        except repository.DuplicatePendingRequest:
            raise DuplicatePending() from None
    return inserted.value


def _load_pending(conn, request_id, *, party_owner_id, party_field, wrong_party):
    """Shared preamble of withdraw, approve, and deny.

    Order matters: not-found, then wrong party, then
    not-pending, then expiry. Checking expiry before the party check would tell a
    stranger that the request exists.
    """
    found = repository.find_request_by_id(conn, request_id)
    if isinstance(found, NotFound):
        raise RequestNotFound()
    existing = found.value
    if str(getattr(existing, party_field)) != str(party_owner_id):
        raise wrong_party()
    if existing.status != "pending":
        raise NotPending(existing.status)
    _reject_if_expired(conn, existing)
    return existing


def _reject_if_expired(conn, existing) -> None:
    """Flip a passed deadline to `expired`
    DURABLY, in its own transaction, and then reported - so the stored state reflects
    reality and a pending request can never be approved into a grant after its expiry."""
    if existing.expires_at <= _now():
        with _write_context(conn, existing.granter_owner_id):
            repository.expire_request(conn, existing.id)
        raise NotPending("expired")


def _lost_decision_race(conn, request_id) -> DomainError:
    """Report the current state instead of clobbering a terminal decision."""
    found = repository.find_request_by_id(conn, request_id)
    if isinstance(found, NotFound):
        return RequestNotFound()
    return NotPending(found.value.status)


def withdraw_request(conn, request_id, requester_owner_id):
    """Withdraw a pending request owned by the requester."""
    _load_pending(conn, request_id, party_owner_id=requester_owner_id,
                  party_field="requester_owner_id", wrong_party=NotRequester)
    with _write_context(conn, requester_owner_id):
        updated = repository.update_request_decision(
            conn, request_id, "withdrawn", requester_owner_id, None, None)
        if isinstance(updated, NotFound):
            raise _lost_decision_race(conn, request_id)
    return updated.value


def deny_request(conn, request_id, granter_owner_id, note):
    """Deny a pending request owned by the granter."""
    _load_pending(conn, request_id, party_owner_id=granter_owner_id,
                  party_field="granter_owner_id", wrong_party=NotGranter)
    with _write_context(conn, granter_owner_id):
        updated = repository.update_request_decision(
            conn, request_id, "denied", granter_owner_id, note, None)
        if isinstance(updated, NotFound):
            raise _lost_decision_race(conn, request_id)
    return updated.value


def approve_request(conn, request_id, granter_owner_id, note):
    """Approve a pending request and return (request, grant)."""
    existing = _load_pending(conn, request_id, party_owner_id=granter_owner_id,
                             party_field="granter_owner_id", wrong_party=NotGranter)
    requires_human_auth = _capture_requires_human_auth(
        conn, existing.granter_peer_id, existing.skill_name, None)

    with _write_context(conn, granter_owner_id):
        try:
            inserted = repository.insert_grant(
                conn,
                grantor_owner_id=existing.granter_owner_id,
                grantee_owner_id=existing.requester_owner_id,
                granter_peer_id=existing.granter_peer_id,
                skill_name=existing.skill_name,
                version_pin=None, approved_manifest_hash=None,
                source="approved_request",
                source_ref=str(existing.id),
                # TASK-4: an access request NEVER carries requester-supplied scopes
                # into approval, and approval-derived grants are uncapped and unexpiring.
                expires_at=None, scopes=None, max_executions=None,
                approved_requires_human_auth=requires_human_auth)
        except repository.DuplicateActiveGrant:
            raise DuplicateActive() from None
        grant = inserted.value
        updated = repository.update_request_decision(
            conn, request_id, "approved", granter_owner_id, note, grant.id)
        if isinstance(updated, NotFound):
            # Raising here abandons the transaction, so the grant inserted two
            # statements ago rolls back with it. That is the whole point: a
            # denied/withdrawn request must never end up with a live grant.
            raise _lost_decision_race(conn, request_id)
    return updated.value, grant
