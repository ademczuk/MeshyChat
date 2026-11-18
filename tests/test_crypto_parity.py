"""Known answers: the server and its client must emit the SAME bytes.

KAT_DPOP is what this tree computes for the fixed vector below under the signing domain
`meshy/dpop/v1` (apps/crypto/primitives.py DPOP_DOMAIN). tests/test_meshy_node.py pins the
same constant for the client (tools/meshy_node/wire.py), so a change to the signed bytes on
either side fails one of the two files.

The value is specific to this signing domain: a deployment that signs under another domain
string produces other bytes, and is not wire-compatible with this one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.crypto import primitives as P  # noqa: E402

# Fixed vector: seed=[7;32], POST /v1/x, jwt="h.p.s", ts=1_000_000_000, jti=[0x11;16]
KAT_DPOP = ("AARQT1NUAAUvdjEveAAAAAA7msoAMAomZLy6qQuerO_7t-QPAZosybBQt7yNFI1M18KN9dkRERERERERERERE"
            "RERERERL6PfumYqeRVOONEmgaoGEgLBgXLlh1NgJFOhITRGJzTbrCFeNuM4kXysoVuLbZORGx_BKRqz-hmmXZ_oDdtrAA")

SEED7 = bytes([7] * 32)
JTI11 = bytes([0x11] * 16)


def test_dpop_wire_matches_the_known_answer():
    wire = P.dpop_wire(SEED7, "POST", "/v1/x", "h.p.s", timestamp=1_000_000_000, jti=JTI11)
    assert wire == KAT_DPOP, f"DPoP wire drifted:\n  this = {wire}\n  pinned = {KAT_DPOP}"


def test_dpop_roundtrip_and_binding():
    pub = P.public_key_of(SEED7)
    wire = P.dpop_wire(SEED7, "POST", "/v1/x", "h.p.s", timestamp=1_000_000_000, jti=JTI11)
    ok, why = P.verify_dpop(wire, pub, method="POST", target="/v1/x", jwt="h.p.s", now=1_000_000_000)
    assert ok, why
    # Bound to the request: each of these MUST fail, or the proof is replayable.
    for kwargs, label in [
        (dict(method="GET", target="/v1/x", jwt="h.p.s"), "different method"),
        (dict(method="POST", target="/v1/y", jwt="h.p.s"), "different target"),
        (dict(method="POST", target="/v1/x", jwt="other"), "different token"),
    ]:
        ok, _ = P.verify_dpop(wire, pub, now=1_000_000_000, **kwargs)
        assert not ok, f"proof accepted for a {label} - it is replayable"
    ok, _ = P.verify_dpop(wire, pub, method="POST", target="/v1/x", jwt="h.p.s",
                          now=1_000_000_000 + 3600)
    assert not ok, "proof accepted an hour out of window"


def test_jcs_is_rfc8785_not_sort_keys():
    # sort_keys ordering matches for ASCII keys...
    assert P.jcs({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    # ...but RFC 8785 sorts by UTF-16 code unit, and formats numbers per ECMAScript.
    assert P.jcs({"a": 1.0}) == b'{"a":1}', "integral float must serialise as an integer"
    assert P.jcs([1e21]) == b"[1e+21]", "exponent form must be produced, not refused"


def test_verify_strict_rejects_what_libsodium_accepts():
    """The security difference the production daemon documents but does not implement."""
    msg = b"payload"
    sig = P.sign(SEED7, msg)
    pub = P.public_key_of(SEED7)
    assert P.verify_strict(pub, msg, sig)

    # 1. small-order public key must be refused outright
    small = bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000000")
    assert not P.verify_strict(small, msg, sig), "small-order public key accepted"

    # 2. malleable signature: s + L is a second valid encoding of the same scalar
    L = 2 ** 252 + 27742317777372353535851937790883648493
    s = int.from_bytes(sig[32:], "little")
    malleable = sig[:32] + (s + L).to_bytes(32, "little")
    assert not P.verify_strict(pub, msg, malleable), "non-canonical scalar accepted"

    assert not P.verify_strict(pub, b"different", sig)


def test_key_id_derivations_are_two_different_rules():
    pub = P.public_key_of(SEED7)
    peer, server = P.kid_for_peer_key(pub), P.server_kid(pub)
    assert len(peer) == 16 and len(server) == 16
    assert all(c in "0123456789abcdef" for c in server), "server kid must be lowercase hex"
    assert peer != server, "the two kid rules must not be collapsed into one"


def test_the_two_kid_rules_are_told_apart_by_their_digest_bytes_not_by_shape():
    """E23. apps/calls/result_signing.py said a server kid (16 lowercase hex) and a peer kid
    (16 base64url) "cannot be confused by shape". They can: lowercase hex is a subset of the
    base64url alphabet, so EVERY server kid is also a well-formed peer kid. What separates
    them is that they are different functions of different digest bytes: the server rule
    hex-encodes digest[0:8], the peer rule base64url-encodes the digest and keeps 16
    characters, which are digest[0:12]. A peer kid equal to a given server kid is a
    preimage question on SHA-256, not something a shape check could ever settle."""
    import hashlib
    import string

    from apps.calls import result_signing

    pub = P.public_key_of(SEED7)
    digest = hashlib.sha256(pub).digest()
    server, peer = P.server_kid(pub), P.kid_for_peer_key(pub)
    b64u_alphabet = set(string.ascii_letters + string.digits + "-_")
    assert len(server) == 16 and set(server) <= b64u_alphabet, "has a peer kid's shape"
    assert server == digest[:8].hex()
    assert peer == P.b64u(digest[:12]) and len(peer) == 16, "the peer kid is digest[0:12]"
    # Positive control for the alphabet check: a character base64url lacks is caught.
    assert not set(server + "=") <= b64u_alphabet

    doc = result_signing.__doc__
    assert "cannot be confused by shape" not in doc, "the false shape claim is back"
    for needle in ("subset of base64url", "digest[0:8]", "digest[0:12]", "preimage"):
        assert needle in doc, needle


def test_signing_input_endianness_differs_from_wire():
    """The signed form is little-endian; the wire is big-endian. Guard the asymmetry."""
    signed = P.dpop_signing_input("POST", "/v1/x", 1_000_000_000, "h.p.s", JTI11)
    assert signed.startswith(b"meshy/dpop/v1\x04\x00\x00\x00POST")   # u32 LE length
    wire = P.b64u_decode(P.dpop_wire(SEED7, "POST", "/v1/x", "h.p.s",
                                     timestamp=1_000_000_000, jti=JTI11))
    assert wire.startswith(b"\x00\x04POST")                          # u16 BE length


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
