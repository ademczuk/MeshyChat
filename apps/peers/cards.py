"""A2A Agent Cards: build, sign, verify, and the JWKS an external verifier resolves.

THE INTEROP CRUX. The card is a DETACHED JWS. The signed bytes
are NOT the bare JCS of the card - they are

    b64u(protected) + "." + b64u(JCS(card minus `signatures`))

which is what an off-the-shelf JOSE verifier reconstructs. Signing the bare JCS produces
a signature no external verifier accepts; `tests/test_peers.py:925` pins that rejection.

THE PIN. A validly-signed card is not enough. The protected
header's `jku` must be EXACTLY this server's per-peer JWKS route and its `kid` must be
the peer's active kid. Without that a peer could publish an authentic card that points
external verifiers at a JWKS it controls, and the signature would endorse an unseen key.
With `MESHYCHAT_PUBLIC_BASE_URL` unset the expected `jku` cannot be
formed at all, so publishing is REFUSED (500) rather than stored unpinned.

`verify` additionally asserts alg=EdDSA and typ=JOSE and requires EXACTLY ONE signature:
a republished card with a trailing second signature must not pass on
`signatures[0]`, or consumers would disagree about who signed it.
"""
from __future__ import annotations

import json

from apps.crypto import primitives as P

from . import errors

# Published card and protocol versions.
CARD_VERSION = "1.0.0"
A2A_PROTOCOL_VERSION = "0.3.0"


def build_card(peer_name: str, peer_display_name: str | None, peer_id: str,
               base_url: str, skills: list[dict]) -> dict:
    """Build a card with the canonical field names.

    Card keys use camelCase. Skill fields stay lowercase (id/name/description/tags);
    changing them would alter the canonical bytes and every signature with them.
    """
    name = (peer_display_name or "") or peer_name
    return {
        "name": name,
        "description": name,                 # Derived from the display name; no column.
        "version": CARD_VERSION,
        "url": f"{base_url}/peers/{peer_id}",
        "preferredTransport": "JSONRPC",
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": s["id"], "name": s["name"], "description": s.get("description", ""),
             "tags": list(s.get("tags", []))}
            for s in skills
        ],
    }


def jcs_payload(card: dict) -> bytes:
    """Return RFC 8785 JCS of the card with `signatures` removed."""
    unsigned = {k: v for k, v in card.items() if k != "signatures"}
    return P.jcs(unsigned)


def sign_card(card: dict, private_key32: bytes, kid: str, jku: str) -> dict:
    """Return a new dict carrying `signatures`."""
    payload = jcs_payload(card)
    protected = {"alg": "EdDSA", "typ": "JOSE", "kid": kid, "jku": jku}
    # The header is signed as bytes and
    # fed back verbatim into the signing input, so its ordering never has to be JCS.
    protected_b64 = P.b64u(json.dumps(protected, separators=(",", ":")).encode("utf-8"))
    payload_b64 = P.b64u(payload)
    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    signature = P.sign(private_key32, signing_input)
    signed = dict(card)
    signed["signatures"] = [{"protected": protected_b64,
                             "signature": P.b64u(signature)}]
    return signed


def protected_header(card: dict) -> dict:
    """All four fields are required; a header missing any of them is malformed."""
    sigs = card.get("signatures") or []
    if not sigs:
        raise errors.validation("card protected header: agent-card verify: no signatures")
    try:
        raw = P.b64u_decode(sigs[0]["protected"])
        header = json.loads(raw)
    except Exception as exc:
        raise errors.validation(f"card protected header: bad protected base64: {exc}")
    if not isinstance(header, dict):
        raise errors.validation("card protected header: not an object")
    for field in ("alg", "typ", "kid", "jku"):
        if not isinstance(header.get(field), str):
            raise errors.validation(f"card protected header: missing `{field}`")
    return header


class CardVerifyError(Exception):
    """Card-verification error. The route turns every one into the same 400 message, so
    the detail is for logs only."""


def verify_card(card: dict, raw_pubkey: bytes) -> None:
    """Raise `CardVerifyError` on any failure."""
    sigs = card.get("signatures")
    if sigs is None:
        raise CardVerifyError("no signatures")
    if not isinstance(sigs, list) or len(sigs) != 1:
        n = len(sigs) if isinstance(sigs, list) else "?"
        raise CardVerifyError(f"expected exactly one signature, got {n}")
    sig = sigs[0]

    try:
        header = protected_header(card)
    except errors.ApiError as exc:
        raise CardVerifyError(exc.message)
    # Assert the advertised algorithm. A cryptographically
    # valid signature under an `alg: none` / `alg: HS256` header must still be
    # rejected, or strict external JOSE verification disagrees about the same bytes.
    if header["alg"] != "EdDSA":
        raise CardVerifyError(f"unsupported alg {header['alg']!r}, expected EdDSA")
    if header["typ"] != "JOSE":
        raise CardVerifyError(f"unexpected typ {header['typ']!r}, expected JOSE")

    payload_b64 = P.b64u(jcs_payload(card))
    # The protected header is fed back VERBATIM, not re-encoded from the decoded dict:
    # re-encoding would change the bytes for any header this server did not itself write.
    signing_input = f"{sig['protected']}.{payload_b64}".encode("ascii")

    if len(raw_pubkey) != 32:
        raise CardVerifyError("public key must be 32 bytes")
    try:
        sig_bytes = P.b64u_decode(sig["signature"])
    except Exception as exc:
        raise CardVerifyError(f"bad signature base64: {exc}")
    if len(sig_bytes) != 64:
        raise CardVerifyError("signature must be 64 bytes")
    # verify_strict rejects small-order keys and malleable scalars.
    if not P.verify_strict(raw_pubkey, signing_input, sig_bytes):
        raise CardVerifyError("signature invalid")


def expected_jku(base_url: str, peer_id) -> str:
    """Return `{base}/agents/{peer_id}/jwks.json`, base right-trimmed
    of '/'. The card is pinned to this exact string, so the trim is not cosmetic."""
    return f"{base_url.rstrip('/')}/agents/{peer_id}/jwks.json"


def ed25519_jwk(raw_pubkey: bytes, kid: str) -> dict:
    """Return an Ed25519 JWK."""
    return {"kty": "OKP", "crv": "Ed25519", "x": P.b64u(raw_pubkey),
            "kid": kid, "alg": "EdDSA", "use": "sig"}


def build_jwks(keys: list[tuple[bytes, str]]) -> dict:
    """Return `{"keys": [...]}`, empty when the peer has no keys."""
    return {"keys": [ed25519_jwk(pk, kid) for pk, kid in keys]}


def protocol_hash(card: dict) -> str:
    """Return lowercase hex SHA-256 over the JCS of the whole served
    card, signatures included, because the client hashes exactly what it received."""
    return P.sha256(P.jcs(card)).hex()
