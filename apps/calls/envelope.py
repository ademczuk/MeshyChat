"""Signed-envelope verification.

THE ORDER OF THE SEVEN CHECKS IS THE CONTRACT, not an implementation detail. A client
distinguishes "your clock is wrong" (E_SIG_REPLAY) from "you signed the wrong thing"
(E_SIG_BAD) purely by which check fires first:

  1. payload.v == 1                      E_SIG_VERSION_UNSUPPORTED
  2. payload.kind == expected            E_SIG_KIND_MISMATCH
  3. issued_at within +/-120s            E_SIG_REPLAY
  4. payload peer field == authenticated E_SIG_IDENTITY_MISMATCH
  5. (peer_id, kid) -> key, else SigBad  E_SIG_BAD
  6. key not revoked                     E_SIG_KEY_REVOKED
  7. verify_strict over the JCS bytes    E_SIG_BAD

DOMAIN SEPARATION IS THE `kind` FIELD, NOT A PREFIX. The signed input is the bare JCS
bytes of the payload; unlike `primitives.dpop_signing_input`, it has no prefix. What
stops a captured `skill.result` being replayed as a `skill.call` is that `kind` is
INSIDE the signed object and check 2 compares it against what the caller declared it
expects. Adding a prefix here would break every existing signature.

STRICT verification is load-bearing: plain Ed25519 verification accepts small-order public keys
and malleable (non-canonical) scalars. `primitives.verify_strict` rejects both, so a signature
accepted here is one every strict verifier also accepts.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid
from dataclasses import dataclass

from apps.calls.errors import SignatureFailed
from apps.crypto import primitives as P

# Signed envelopes have a 120-second replay window.
REPLAY_WINDOW_SECS = 120
# Version 1 is the supported envelope format.
SUPPORTED_VERSION = 1

# Wire strings, not implementation names.
KIND_SKILL_CALL = "skill.call"
KIND_SKILL_RESULT = "skill.result"
KIND_SKILL_CHUNK = "skill.chunk"
KIND_REASONING_TRACE = "reasoning.trace"

# Payload field carrying the signer's identity for each kind.
_PEER_FIELD_FOR_KIND = {
    KIND_SKILL_CALL: "caller_peer_id",
    KIND_SKILL_RESULT: "target_peer_id",
    KIND_SKILL_CHUNK: "target_peer_id",
}


@dataclass(frozen=True)
class SignedEnvelope:
    """The three wire fields, no more."""
    payload: dict
    signature: str      # base64url, NO padding
    signing_kid: str


@dataclass(frozen=True)
class VerifiedPayload:
    """Only produced once every check above has passed. Carrying it
    into the rest of the pipeline is what makes "this data is authenticated" a type,
    not a comment."""
    kind: str
    peer_id: uuid.UUID
    issued_at: _dt.datetime
    payload: dict


@dataclass(frozen=True)
class EnvelopeClaims:
    """What checks 1-4 establish from the payload alone: no key has been looked up and no
    signature verified. NOT a VerifiedPayload, and deliberately not convertible into one."""
    kind: str
    peer_id: uuid.UUID
    issued_at: _dt.datetime


@dataclass(frozen=True)
class PeerKeyRecord:
    """A peer key row, including `active`. Envelope verification checks `revoked_at`;
    manifest attestation also checks `active`, using this lookup rather than a second query
    of peer_keys."""
    public_key_b64u: str
    revoked_at: _dt.datetime | None
    active: bool = True

    def is_revoked_now(self, now: _dt.datetime) -> bool:
        """`revoked_at` is the END of the grace window, so the key
        stays usable UNTIL that instant and is dead at-or-after it (`r <= now`)."""
        return self.revoked_at is not None and self.revoked_at <= now


def fetch_peer_key(conn, peer_id, kid: str) -> PeerKeyRecord | None:
    """Look up a key, including a key inside its revocation grace window.

    The predicate is NOT simply `active = TRUE`: a key inside its revocation grace
    window (`revoked_at > NOW()`) is still returned, and verify's step 6 is what
    finally rejects it once the window closes. Collapsing the two into one WHERE
    clause would turn a 401 E_SIG_KEY_REVOKED into a 401 E_SIG_BAD - same status,
    different diagnosis.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public_key, revoked_at, active FROM peer_keys "
            "WHERE peer_id = %s AND kid = %s "
            "  AND (active = TRUE OR (revoked_at IS NOT NULL AND revoked_at > NOW()))",
            (str(peer_id), kid),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return PeerKeyRecord(public_key_b64u=row[0], revoked_at=row[1], active=bool(row[2]))


def peer_has_usable_key(conn, peer_id, now: _dt.datetime | None = None, *,
                        public_key: bytes | None = None) -> bool:
    """Whether `peer_id` holds a key that checks 5 and 6 would accept a signature under:
    a row `fetch_peer_key`'s predicate returns (active, or revoked with the grace window
    still open) that `PeerKeyRecord.is_revoked_now` does not reject. With `public_key`
    (the raw 32 bytes a request proved it holds), only THAT key counts: proving an old,
    revoked key must not borrow the standing of the peer's current one.

    For a surface that answers what a signed call would return but carries no envelope,
    so there is no kid to look up: the context anchor's body (docs/DIVERGENCES.md 027).
    The SAME predicate and the same revocation rule as `verify_payload`, not a second
    definition of a usable key. Possession is NOT shown by this; a request proves it
    separately (a DPoP proof, apps/peers/auth.require_bound_proof).
    """
    now = _dt.datetime.now(_dt.timezone.utc) if now is None else now
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public_key, revoked_at FROM peer_keys "
            "WHERE peer_id = %s "
            "  AND (active = TRUE OR (revoked_at IS NOT NULL AND revoked_at > NOW()))",
            (str(peer_id),),
        )
        rows = cur.fetchall()
    for stored, revoked_at in rows:
        if PeerKeyRecord(public_key_b64u=stored, revoked_at=revoked_at).is_revoked_now(now):
            continue
        if public_key is None:
            return True
        try:
            if P.b64u_decode(stored) == public_key:
                return True
        except Exception:  # noqa: BLE001 - an undecodable stored key verifies nothing
            continue
    return False


# Accepted: RFC 3339 date-times plus a space separator and U+2212 as the offset sign, because
# the replay window must classify the same strings as the verifier clients sign against. One or
# more fraction digits after a dot and `Z`, `z`, or a signed `HH:MM` offset are accepted.
# `fullmatch`, never `$`, which admits a
# trailing newline; `[0-9]`, never `\d`, which admits any Unicode digit.
#
# NOT `datetime.fromisoformat` (reviewed in divergence 038): it accepts ISO 8601 forms
# this parser rejects (basic `20260924T055155Z`, week dates, no seconds, any
# separator, a comma fraction, `+0000`), refuses `z` and U+2212, and holds only years 1 to
# 9999, so `astimezone` raised OverflowError on `0001-01-01T00:00:00+01:00`. Nor the
# repository's three other RFC 3339 parsers (apps/peers/timefmt.py, apps/grants/wire.py,
# apps/calls/wire.py): they allow `+0000`, strip whitespace and match `\d`. They serve
# other surfaces and are not changed here.
_RFC3339 = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt ]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(?:[Zz]|([+\-\u2212])([0-9]{2}):([0-9]{2}))")
_UNIX_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
_UNIX_EPOCH_DAY = _UNIX_EPOCH.date().toordinal()
_NS_PER_SEC = 1_000_000_000


def _parse_rfc3339(text: str) -> int:
    """`issued_at` as nanoseconds since the Unix epoch for this RFC 3339 grammar.
    Raises ValueError for other strings, which map to Malformed.

    An integer, not a datetime, because Python's datetime cannot hold year 0, or year 1 or 9999
    carried across the year boundary by an offset. Those valid instants are classified by the
    replay window rather than rejected as Malformed.

    Leap seconds (`:60`) are refused with E_SIG_BAD even though other RFC 3339 parsers accept
    them: no clock formats one, and arithmetic across one is not a plain count of seconds.
    """
    m = _RFC3339.fullmatch(text)
    if m is None:
        raise ValueError("not RFC 3339's date-time")
    year, month, day, hour, minute, second = (int(g) for g in m.group(1, 2, 3, 4, 5, 6))
    try:
        # Year 0 is valid in the proleptic Gregorian calendar but not for Python. The
        # Gregorian calendar repeats every 400 years, 146097 days, so year 400 checks the
        # day and counts it.
        day_number = (_dt.date(year or 400, month, day).toordinal()
                      - (146097 if year == 0 else 0) - _UNIX_EPOCH_DAY)
    except ValueError as exc:
        raise ValueError(f"no such date: {exc}") from exc
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("time of day out of range")
    fraction = m.group(7) or ""
    nanos = int(fraction[:9].ljust(9, "0")) if fraction else 0     # Preserve 9 digits.
    offset = 0
    if m.group(8):
        offset_minutes = int(m.group(10))
        offset = int(m.group(9)) * 3600 + offset_minutes * 60
        if offset_minutes > 59 or offset >= 86400:                  # offset bound
            raise ValueError("offset out of range")
        if m.group(8) != "+":
            offset = -offset
    seconds = day_number * 86400 + hour * 3600 + minute * 60 + second - offset
    return seconds * _NS_PER_SEC + nanos


def _epoch_ns(at: _dt.datetime) -> int:
    since = at - _UNIX_EPOCH
    return ((since.days * 86400 + since.seconds) * 1_000_000 + since.microseconds) * 1000


def verify_payload(conn, envelope: SignedEnvelope, expected_peer_id, expected_kind: str,
                   now: _dt.datetime | None = None) -> VerifiedPayload:
    """Verify an envelope and return its authenticated payload.

    `expected_peer_id` is the AUTHENTICATED identity (the JWT subject / channel
    identity), never a value taken from the body. Check 4 forces the signed payload
    to agree with it, which is what stops peer A submitting an envelope peer B signed.
    """
    now = _dt.datetime.now(_dt.timezone.utc) if now is None else now

    payload = envelope.payload
    claims = check_claims(payload, expected_peer_id, expected_kind, now)

    # 5. Key lookup. An UNKNOWN kid is deliberately E_SIG_BAD, not E_KEY_NOT_FOUND
    #    It does not tell a prober which kids exist.
    record = fetch_peer_key(conn, claims.peer_id, envelope.signing_kid)
    if record is None:
        raise SignatureFailed.sig_bad("unknown signing kid")

    # 6. Revocation.
    if record.is_revoked_now(now):
        raise SignatureFailed.key_revoked()

    # 7. verify_strict over the JCS bytes of the payload.
    try:
        public_key = P.b64u_decode(record.public_key_b64u)
        signature = P.b64u_decode(envelope.signature)
    except Exception as exc:  # noqa: BLE001 - any decode failure is SigBad.
        raise SignatureFailed.sig_bad("undecodable key or signature") from exc
    try:
        canonical = P.jcs(payload)
    except Exception as exc:  # noqa: BLE001 - a canonicalize error is SigBad.
        raise SignatureFailed.sig_bad("payload is not canonicalizable") from exc
    if not P.verify_strict(public_key, canonical, signature):
        raise SignatureFailed.sig_bad()

    return VerifiedPayload(kind=claims.kind, peer_id=claims.peer_id,
                           issued_at=claims.issued_at, payload=payload)


def check_claims(payload, expected_peer_id, expected_kind: str,
                 now: _dt.datetime | None = None) -> EnvelopeClaims:
    """Checks 1-4 of `verify_payload`, in its order, and nothing that needs a key: the
    version, the kind, the 120 s window, the identity binding. `verify_payload` runs these
    first and then 5-7; a surface whose signature is verified elsewhere runs these alone.

    The board's write door is that surface (docs/DIVERGENCES.md 038, design B02 Q9):
    `handle_write` verifies the signature under the caller's key itself, and until 038
    nothing on the door checked any of 1-4, so a payload with no kind, no caller and no
    `issued_at` landed. ONE definition, so the door refuses an envelope with exactly the
    code, status and message invoke gives it.

    Raises `SignatureFailed`; returns the claims, which are NOT a verification.
    """
    now = _dt.datetime.now(_dt.timezone.utc) if now is None else now

    if not isinstance(payload, dict):
        raise SignatureFailed.malformed("payload is not a JSON object")

    # 1. Version. Booleans, floats (including 1.0), negative numbers, and values past u64
    #    are not versions:
    #    each of those is Malformed, never version 1 and never VersionUnsupported.
    v = payload.get("v")
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < 2 ** 64:
        raise SignatureFailed.malformed("payload.v missing or not a number")
    if v != SUPPORTED_VERSION:
        raise SignatureFailed.version_unsupported(v)

    # 2. Kind - the domain separator.
    kind = payload.get("kind")
    if not isinstance(kind, str):
        raise SignatureFailed.malformed("payload.kind missing or not a string")
    if kind != expected_kind:
        raise SignatureFailed.kind_mismatch(expected_kind, kind)

    # 3. issued_at + the 120s replay window.
    issued_at_raw = payload.get("issued_at")
    if not isinstance(issued_at_raw, str):
        raise SignatureFailed.malformed("payload.issued_at missing")
    try:
        issued_ns = _parse_rfc3339(issued_at_raw)
    except ValueError as exc:
        raise SignatureFailed.malformed(f"payload.issued_at not RFC 3339: {exc}") from exc
    # WHOLE seconds, truncated toward zero: 120.9 s either side is inside the window and
    # 121 s is not. Integer arithmetic is needed because `issued_at` is attacker-controlled.
    if abs(_epoch_ns(now) - issued_ns) // _NS_PER_SEC > REPLAY_WINDOW_SECS:
        raise SignatureFailed.replay(REPLAY_WINDOW_SECS)
    # Inside the window, so a datetime can hold it. Preserve microseconds.
    issued_at = _UNIX_EPOCH + _dt.timedelta(microseconds=issued_ns // 1000)

    # 4. Identity binding.
    expected_uuid = uuid.UUID(str(expected_peer_id))
    peer_field = _PEER_FIELD_FOR_KIND.get(kind)
    if peer_field is None:
        # ReasoningTrace carries no submitter id: the authenticated peer IS the
        # identity.
        payload_peer = expected_uuid
    else:
        raw = payload.get(peer_field)
        if not isinstance(raw, str):
            raise SignatureFailed.malformed(f"payload.{peer_field} missing")
        try:
            payload_peer = uuid.UUID(raw)
        except ValueError as exc:
            raise SignatureFailed.malformed(f"payload.{peer_field} not a UUID") from exc
    if payload_peer != expected_uuid:
        raise SignatureFailed.identity_mismatch()

    return EnvelopeClaims(kind=kind, peer_id=payload_peer, issued_at=issued_at)
