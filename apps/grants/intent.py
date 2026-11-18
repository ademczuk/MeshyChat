"""Signed human-intent mandates (TASK-29).

Three aspects of the mandate format are contractual:

  * the signing input is the mandate domain separator plus `JCS(mandate)`. The domain
    separator stops a mandate signature being replayed as
    any other signature the owner key makes, and JCS (RFC 8785) is what lets the
    verifier produce identical bytes. `apps/crypto/primitives.jcs` is the same
    canonicaliser the rest of this project signs with.
  * the mandate's own field set and their ORDER do not matter for the hash (JCS sorts
    keys), but the field NAMES and types do: `owner, grantee, target_peer, skill,
    scopes, expires_at, budget`. `budget` is present as `null` when absent; dropping it would change
    the canonical bytes and every signature.
  * `mandate_authorizes` is REJECT-BROADER, and its empty-scope rule inverts the naive
    reading: an EMPTY grant scope set means FULL-skill access at runtime, so a mandate
    with restrictive scopes must not authorize an unscoped grant (TASK-87/HIGH-4). Treating
    "no scopes requested" as "nothing to check"
    would silently widen every mandate-bound issuance.

`verify_mandate` uses `verify_strict`, not the permissive `verify`.
`apps/crypto/primitives.verify_strict` rejects small-order and malleable signatures.
"""
from __future__ import annotations

import hashlib

from apps.crypto.primitives import jcs, verify_strict
from apps.grants.wire import parse_uuid_str

#: Domain separator for signed mandate payloads.
MANDATE_DOMAIN = b"meshy/intent-mandate/v1"

#: MandateError texts are logged or wrapped; a MandateViolation is answered as a
#: constant 403 `forbidden`, so these strings do not reach the client.
BAD_SIGNATURE = "mandate signature is invalid under the owner key"
WRONG_GRANTEE = "grantee does not match the mandate"
WRONG_PEER = "target peer does not match the mandate"
WRONG_SKILL = "skill does not match the mandate"
EXPIRY_EXCEEDS_MANDATE = "requested expiry exceeds the mandate expiry"
MANDATE_EXPIRED = "mandate has expired"


class MandateError(ValueError):
    """A mandate validation failure."""


def scope_not_authorized(scope: str) -> MandateError:
    return MandateError(f"scope '{scope}' is not authorized by the mandate")


class IntentMandate:
    """Construct from a decoded JSON object.

    Extra fields are ignored, and canonical bytes are rebuilt from known fields only.
    """

    __slots__ = ("owner", "grantee", "target_peer", "skill", "scopes", "expires_at", "budget")

    def __init__(self, owner, grantee, target_peer, skill, scopes, expires_at, budget=None):
        self.owner = str(owner)
        self.grantee = str(grantee)
        self.target_peer = str(target_peer)
        self.skill = skill
        self.scopes = list(scopes)
        self.expires_at = int(expires_at)
        self.budget = None if budget is None else int(budget)

    @classmethod
    def from_json(cls, value) -> "IntentMandate":
        """Raise ValueError for an invalid mandate JSON value."""
        if not isinstance(value, dict):
            raise ValueError("invalid type: expected a JSON object for the mandate")
        try:
            owner = value["owner"]
            grantee = value["grantee"]
            target_peer = value["target_peer"]
            skill = value["skill"]
            scopes = value["scopes"]
            expires_at = value["expires_at"]
        except KeyError as exc:
            raise ValueError(f"missing field `{exc.args[0]}`") from None
        if not isinstance(skill, str):
            raise ValueError("invalid type: `skill` must be a string")
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise ValueError("invalid type: `scopes` must be an array of strings")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise ValueError("invalid type: `expires_at` must be an integer")
        budget = value.get("budget")
        if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool)):
            raise ValueError("invalid type: `budget` must be an integer or null")
        # These three identifiers must parse before authorization. STATUS C37 fixed an
        # invalid owner value that used to reach owner binding as a 403. `parse_uuid_str`
        # accepts four UUID forms rather than Python's wider `uuid.UUID`. A parsed value
        # is stored in canonical lowercase, hyphenated form in the signed bytes, whatever form the client
        # sent. tests/test_grants.py::
        # test_a_mandate_is_verified_over_its_canonical_ids_whatever_form_was_sent
        # signs both byte strings and holds which one verifies.
        ids = {}
        for name, raw in (("owner", owner), ("grantee", grantee), ("target_peer", target_peer)):
            if not isinstance(raw, str):
                raise ValueError(f"invalid type: `{name}` must be a UUID string")
            try:
                ids[name] = parse_uuid_str(raw)
            except ValueError:
                raise ValueError(f"invalid value: `{name}` is not a UUID") from None
        return cls(ids["owner"], ids["grantee"], ids["target_peer"], skill, scopes,
                   expires_at, budget)

    def _canonical(self) -> dict:
        # JCS sorts keys, so this dict's order is irrelevant, but its contents are signed.
        return {
            "owner": self.owner,
            "grantee": self.grantee,
            "target_peer": self.target_peer,
            "skill": self.skill,
            "scopes": self.scopes,
            "expires_at": self.expires_at,
            "budget": self.budget,
        }

    def signing_input(self) -> bytes:
        """Return domain || JCS(self)."""
        return MANDATE_DOMAIN + jcs(self._canonical())


def verify_mandate(mandate: IntentMandate, signature64: bytes, owner_public32: bytes) -> None:
    """Raise MandateError(BAD_SIGNATURE) for an invalid signature."""
    if not verify_strict(owner_public32, mandate.signing_input(), signature64):
        raise MandateError(BAD_SIGNATURE)


def mandate_hash(mandate: IntentMandate) -> bytes:
    """Return the 32-byte SHA-256 hash of the canonical signed bytes."""
    return hashlib.sha256(mandate.signing_input()).digest()


def mandate_authorizes(mandate: IntentMandate, now: int, grantee, target_peer,
                       skill: str, scopes, expires_at: int) -> None:
    """Raise MandateError unless the grant fits the mandate constraints."""
    if now > mandate.expires_at:
        raise MandateError(MANDATE_EXPIRED)
    if str(grantee) != mandate.grantee:
        raise MandateError(WRONG_GRANTEE)
    if str(target_peer) != mandate.target_peer:
        raise MandateError(WRONG_PEER)
    if skill != mandate.skill:
        raise MandateError(WRONG_SKILL)
    scopes = list(scopes)
    # TASK-87: empty requested scopes == full-skill access at runtime, so a mandate
    # with restrictive scopes must refuse it. NOT a vacuous "nothing to check".
    if not scopes and mandate.scopes:
        raise scope_not_authorized(
            "unscoped grant (full-skill) exceeds the mandate's restrictive scope")
    for s in scopes:
        if s not in mandate.scopes:
            raise scope_not_authorized(s)
    if expires_at > mandate.expires_at:
        raise MandateError(EXPIRY_EXCEEDS_MANDATE)
