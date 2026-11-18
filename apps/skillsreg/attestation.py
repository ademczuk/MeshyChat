"""Publisher manifest attestations (TASK-49).

A publisher signs the 32-byte manifest hash with its per-peer Ed25519 key; the row
stores the raw 64-byte signature and the `kid` that selects the exact `peer_keys` row a
later verifier must use.

WHAT THE WRITE PATH DOES **NOT** DO, and this is worth stating because it looks like an
omission: it does not VERIFY the signature. `declare`/`release`/`update` decode the pair and
store it; verification happens at grant-issue time in `verify_manifest_attestation` below,
called by apps/grants/service.py at every pin, which fails closed. Verifying at publish too
would refuse requests that are accepted today.

The DB agrees with the decoder and will not be talked out of it - two CHECK constraints:
    skills_manifest_attestation_pair  both NULL, or both present with a non-blank kid
    skills_manifest_sig_len           octet_length(manifest_sig) = 64
so a decoder bug surfaces as a constraint violation rather than as a stored half-pair.
"""
from __future__ import annotations

import base64


class AttestationRefused(ValueError):
    """Base class for manifest-attestation refusals."""


class ManifestAttestationError(AttestationRefused):
    """Malformed attestation input."""

    def __init__(self, detail: str):
        super().__init__(f"manifest attestation malformed: {detail}")


class AttestationKeyMissing(AttestationRefused):
    """No key row found for the kid (an unknown kid, or a key whose revocation window
    has closed)."""

    def __init__(self, peer_id, kid: str):
        super().__init__(f"manifest attestation key missing for peer {peer_id} kid {kid}")


class AttestationKeyInactiveOrRevoked(AttestationRefused):
    """The key row is inactive, or its `revoked_at` has passed."""

    def __init__(self, peer_id, kid: str):
        super().__init__(
            f"manifest attestation key inactive or revoked for peer {peer_id} kid {kid}")


class AttestationSignatureInvalid(AttestationRefused):
    """The signature does not verify."""

    def __init__(self, detail: str):
        super().__init__(f"manifest attestation signature invalid: {detail}")


def _strict_b64u_no_pad(text) -> bytes:
    """base64url, NO padding, and STRICT - the one decoder the two callers below share.

    `base64.urlsafe_b64decode` with re-added padding accepts trailing garbage, so
    `validate=True` is passed and the padded input is built explicitly. A laxer decoder
    would accept non-canonical encodings of a signature or key that a strict decoder
    refuses, so one stored value could verify under one rule and not another.

    Raises `ValueError` for a non-string, for any `=` in the input (the no-pad half) and
    for anything `b64decode` refuses. Replaces two copies that differed only in the
    exception they raised: each caller still raises its own class and message, and the
    64-/32-byte length checks stay with the callers.
    """
    if not isinstance(text, str) or "=" in text:
        raise ValueError("not base64url-no-pad")
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise ValueError("not base64url-no-pad") from None


def _b64url_no_pad_decode(text: str) -> bytes:
    """Decode `manifest_sig` for the write path."""
    try:
        return _strict_b64u_no_pad(text)
    except ValueError:
        raise ManifestAttestationError("manifest_sig is not valid base64url-no-pad")


def from_base64url(manifest_sig_b64u: str, signing_kid: str) -> tuple[bytes, str]:
    """Return `(raw_sig_64_bytes, signing_kid)`."""
    sig = _b64url_no_pad_decode(manifest_sig_b64u)
    if len(sig) != 64:
        raise ManifestAttestationError("manifest_sig must be 64 raw Ed25519 bytes")
    if not isinstance(signing_kid, str) or not signing_kid.strip():
        raise ManifestAttestationError(
            "signing_kid must be present when manifest_sig is present")
    return sig, signing_kid


def decode_pair(manifest_sig, signing_kid):
    """Decode a manifest-signature pair.

    Return None when BOTH are absent (the legacy sentinel), the decoded pair when both
    are present, and raises for a half-pair. The half-pair is its own error rather than
    "treat the missing one as absent": silently dropping a supplied signature would
    store an unattested skill while the publisher believed it had attested one.
    """
    if manifest_sig is None and signing_kid is None:
        return None
    if manifest_sig is not None and signing_kid is not None:
        return from_base64url(manifest_sig, signing_kid)
    raise ValueError("manifest_sig and signing_kid must be supplied together")


def _publisher_public_key(text) -> bytes:
    """Decode the stored base64url public key, then require 32 bytes.
    Stricter than `apps.crypto.primitives.b64u_decode`, which ignores bytes outside the
    alphabet; malformed keys must not verify here."""
    try:
        raw = _strict_b64u_no_pad(text)
    except ValueError:
        raise AttestationSignatureInvalid(
            "publisher public key is not valid base64url-no-pad") from None
    if len(raw) != 32:
        raise AttestationSignatureInvalid("publisher public key must decode to 32 bytes")
    return raw


def _database_now(conn):
    """The instant a key's `revoked_at` is compared against.

    Uses the database clock, not the application server's, because the database stamps
    `revoked_at` and the `find_by_kid` predicate uses the same clock.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


def verify_manifest_attestation(conn, skill, require_attestation: bool, *, manifest_hash):
    """Verify an attestation when present. Return None or raise `AttestationRefused`.

      * (NULL, NULL) is the legacy sentinel: accepted unless `require_attestation`.
      * A PRESENT pair is ALWAYS verified, whatever the flag: a half pair, a signature
        that is not 64 bytes and a blank kid are Malformed; the publisher's key is the
        `peer_keys` row named by (skill.peer_id, signing_kid), and an unknown kid is
        KeyMissing, an inactive or revoked key KeyInactiveOrRevoked; then Ed25519
        `verify_strict` over the 32-byte manifest hash, which is the byte string a publisher
        signs with no prefix.

    `manifest_hash` is the caller's hash function, so the bytes verified here are the bytes
    the caller pins. A failure inside it is raised through as the caller's own
    `ManifestError`.

    The key lookup uses apps/calls/envelope.py's `fetch_peer_key`, which the envelope
    verifier also uses.
    """
    sig, kid = skill.get("manifest_sig"), skill.get("signing_kid")
    if sig is None and kid is None:
        if require_attestation:
            raise ManifestAttestationError("manifest attestation required")
        return None
    if sig is None or kid is None:
        raise ManifestAttestationError(
            "manifest_sig and signing_kid must be both present or both NULL")
    sig = bytes(sig)
    if len(sig) != 64:
        raise ManifestAttestationError("manifest_sig must be 64 raw Ed25519 bytes")
    if not kid.strip():
        raise ManifestAttestationError("signing_kid must not be empty")

    from apps.calls.envelope import fetch_peer_key
    from apps.crypto import primitives as P

    peer_id = skill["peer_id"]
    key = fetch_peer_key(conn, peer_id, kid)
    if key is None:
        raise AttestationKeyMissing(peer_id, kid)
    if not key.active or key.is_revoked_now(_database_now(conn)):
        raise AttestationKeyInactiveOrRevoked(peer_id, kid)
    public = _publisher_public_key(key.public_key_b64u)
    # A 32-byte string that is not a curve point fails inside `verify_strict` as
    # "manifest signature verification failed". The grants surface answers that refusal as
    # a 500 "internal error".
    digest = manifest_hash(skill)
    if not P.verify_strict(public, digest, sig):
        raise AttestationSignatureInvalid("manifest signature verification failed")
    return None
