"""`did:key` for Ed25519 - the `cnf` claim's value, and the key a DPoP proof verifies under.

The encoding uses a multicodec 0xed01 prefix, then base58btc, then a multibase `z`.
base58 is implemented here (20 lines) rather than adding a dependency, and is pinned by a
round-trip test against the Bitcoin alphabet.

This is the ONE implementation; apps/wsgw/didkey.py re-exports it. The WS gateway needs it
because the JWT's `cnf` claim IS the peer's did:key - there is no registry lookup at verify
time, so the DPoP proof's verifying key comes out of `from_did_key`.
The error class and message text are the gateway's, because the gateway is the one caller
whose refusal text reaches anything (its upgrade-rejected log line); every other caller
catches `ValueError`, which `DidKeyError` still is.

NOTE (why this is not an RFC 9449 jkt): RFC 9449 puts a JWK SHA-256 THUMBPRINT in
`cnf.jkt`. This server does not: `cnf` is a bare string holding the peer's did:key. A
verifier that assumes a jkt here will never match.
"""
from __future__ import annotations

# Bitcoin/base58btc alphabet.
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}

#: Unsigned varint of multicodec 0xED (Ed25519 public key).
ED25519_MULTICODEC = bytes([0xED, 0x01])


class DidKeyError(ValueError):
    """Raised for a bad did:key prefix, bad base58, a non-Ed25519 multicodec, or a wrong length."""


def b58_encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = []
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_ALPHABET[rem])
    # Leading zero bytes are not representable in the integer, so they are re-added
    # as leading '1's. Dropping this makes the encoding non-injective.
    for byte in raw:
        if byte != 0:
            break
        out.append(_ALPHABET[0])
    return "".join(reversed(out))


def b58_decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _INDEX:
            raise DidKeyError("invalid base58btc encoding")
        n = n * 58 + _INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    leading = 0
    for ch in text:
        if ch != _ALPHABET[0]:
            break
        leading += 1
    return b"\x00" * leading + body


def to_did_key(public_key32: bytes) -> str:
    """Encode a raw 32-byte Ed25519 public key as did:key."""
    if len(public_key32) != 32:
        raise DidKeyError("Ed25519 public key must be 32 bytes")
    return "did:key:z" + b58_encode(ED25519_MULTICODEC + public_key32)


def from_did_key(did: str) -> bytes:
    """Return the raw 32-byte Ed25519 public key.

    Reject a non-`did:key:z` prefix, bad base58, a
    non-Ed25519 multicodec or wrong length.
    """
    if not did.startswith("did:key:z"):
        raise DidKeyError("not a did:key (must start with 'did:key:z')")
    raw = b58_decode(did[len("did:key:z"):])
    if len(raw) != len(ED25519_MULTICODEC) + 32 or raw[:2] != ED25519_MULTICODEC:
        raise DidKeyError("not an Ed25519 did:key (wrong multicodec prefix or length)")
    return raw[2:]
