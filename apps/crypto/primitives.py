"""Signing primitives, pinned by known-answer tests in `tests/test_crypto_parity.py`.

The wire format is pinned by the `KAT_DPOP` constant in
`tests/test_crypto_parity.py`. This module is written to that specification and checked
against the constant, so correctness is measured rather than claimed.

STRICT VERIFICATION rejects small-order public keys and non-canonical signature scalars.
The base verification library does not make those checks, so calling it alone would accept
malleable signatures. `verify_strict` below adds both checks.

CANONICAL JSON uses RFC 8785. `json.dumps(sort_keys=True, ...)` differs on number
formatting. A float policy that rejects exponent form would narrow the accepted domain.
"""
from __future__ import annotations

import base64
import hashlib
import struct
import time
import uuid

import rfc8785
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

DPOP_DOMAIN = b"meshy/dpop/v1"

# Ed25519 group order L. A signature whose scalar s >= L is malleable: a second, valid
# encoding of the same signature exists. verify_strict rejects it; libsodium does not.
_L = 2 ** 252 + 27742317777372353535851937790883648493

# The 8 canonical small-order points (identity, order-2, order-4, order-8). A public key
# that is one of these makes a signature verify against ANY message under some keys.
_SMALL_ORDER = {
    bytes.fromhex("0100000000000000000000000000000000000000000000000000000000000000"),
    bytes.fromhex("ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"),
    bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000080"),
    bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"),
    bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"),
    bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85"),
    bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa"),
}


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jcs(obj) -> bytes:
    """RFC 8785 canonical JSON. Uses the real implementation, not sort_keys."""
    return rfc8785.dumps(obj)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sign(seed32: bytes, message: bytes) -> bytes:
    if len(seed32) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    return SigningKey(seed32).sign(message).signature


def verify_strict(public_key32: bytes, message: bytes, signature64: bytes) -> bool:
    """Strict Ed25519 verification.

    Rejects, in addition to a bad signature:
      - a small-order public key
      - a non-canonical scalar (s >= L), i.e. a malleable second encoding
    """
    if len(public_key32) != 32 or len(signature64) != 64:
        return False
    if public_key32 in _SMALL_ORDER:
        return False
    s = int.from_bytes(signature64[32:], "little")
    if s >= _L:
        return False
    try:
        VerifyKey(public_key32).verify(message, signature64)
    except (BadSignatureError, ValueError):
        return False
    return True


def public_key_of(seed32: bytes) -> bytes:
    return bytes(SigningKey(seed32).verify_key)


def kid_for_peer_key(public_key32: bytes) -> str:
    """Peer key id: first 16 chars of unpadded base64url(SHA-256(raw public key)).

    NOTE the server's OWN key uses a different rule (16 lowercase HEX chars of the
    same digest). Two derivations, deliberately not merged - see `server_kid`.
    """
    return b64u(sha256(public_key32))[:16]


def server_kid(public_key32: bytes) -> str:
    """Server key id: 16 lowercase hex characters (first 8 bytes of the digest)."""
    return sha256(public_key32)[:8].hex()


def dpop_signing_input(method: str, target: str, timestamp: int, jwt: str, jti: bytes) -> bytes:
    """Domain-separated, u32 LITTLE-endian lengths, i64 LITTLE-endian timestamp.

    The signed form and the wire form use DIFFERENT endianness. That asymmetry is
    load-bearing and is why this is two functions rather than one.
    """
    m, t = method.encode("utf-8"), target.encode("utf-8")
    return (DPOP_DOMAIN
            + struct.pack("<I", len(m)) + m
            + struct.pack("<I", len(t)) + t
            + struct.pack("<q", timestamp)
            + sha256(jwt.encode("utf-8"))
            + jti)


def dpop_wire(seed32: bytes, method: str, target: str, jwt: str,
              *, timestamp: int | None = None, jti: bytes | None = None) -> str:
    """The DPoP proof as sent: u16 BIG-endian lengths, i64 BIG-endian timestamp."""
    ts = int(time.time()) if timestamp is None else timestamp
    j = uuid.uuid4().bytes if jti is None else jti
    if len(j) != 16:
        raise ValueError("jti must be 16 bytes")
    m, t = method.encode("utf-8"), target.encode("utf-8")
    signature = sign(seed32, dpop_signing_input(method, target, ts, jwt, j))
    wire = (struct.pack(">H", len(m)) + m
            + struct.pack(">H", len(t)) + t
            + struct.pack(">q", ts)
            + sha256(jwt.encode("utf-8"))
            + j
            + signature)
    return b64u(wire)


def parse_dpop_wire(wire_b64: str) -> dict:
    """Inverse of `dpop_wire`, for the server side. Raises ValueError on a bad shape."""
    raw = b64u_decode(wire_b64)
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(raw):
            raise ValueError("DPoP wire truncated")
        chunk = raw[pos:pos + n]
        pos += n
        return chunk

    method = take(struct.unpack(">H", take(2))[0]).decode("utf-8")
    target = take(struct.unpack(">H", take(2))[0]).decode("utf-8")
    timestamp = struct.unpack(">q", take(8))[0]
    token_hash = take(32)
    jti = take(16)
    signature = take(64)
    if pos != len(raw):
        raise ValueError("DPoP wire has trailing bytes")
    return {"method": method, "target": target, "timestamp": timestamp,
            "token_hash": token_hash, "jti": jti, "signature": signature}


def verify_dpop(wire_b64: str, public_key32: bytes, *, method: str, target: str, jwt: str,
                now: int | None = None, max_skew_secs: int = 120) -> tuple[bool, str]:
    """(ok, reason). Binds the proof to THIS request: method, target and token hash."""
    try:
        p = parse_dpop_wire(wire_b64)
    except ValueError as exc:
        return False, f"malformed: {exc}"
    if p["method"] != method or p["target"] != target:
        return False, "proof is bound to a different method/target"
    if p["token_hash"] != sha256(jwt.encode("utf-8")):
        return False, "proof is bound to a different token"
    current = int(time.time()) if now is None else now
    if abs(current - p["timestamp"]) > max_skew_secs:
        return False, f"timestamp skew {current - p['timestamp']}s exceeds {max_skew_secs}s"
    signing_input = dpop_signing_input(method, target, p["timestamp"], jwt, p["jti"])
    if not verify_strict(public_key32, signing_input, p["signature"]):
        return False, "signature does not verify"
    return True, "ok"
