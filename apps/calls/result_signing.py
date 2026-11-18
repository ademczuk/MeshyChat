"""Sign a `skill.result` the SERVER produced. docs/DIVERGENCES.md 017.

WHO SIGNS. The server executes `memory.*` in-process (apps/calls/route.py), so the key
present at execution time is the SERVER's. It signs with the key it already uses to sign
JWTs, push JWTs, and continuation tokens.

A VERIFIER RESOLVES THE KID AGAINST `/.well-known/jwks.json`, not against a peer's key
set. The kid is the SERVER rule, 16 lowercase hex characters of sha256(raw pubkey)[..8]
(`apps/crypto/primitives.server_kid`), which is exactly the kid
`apps/authapi/views.server_jwks` publishes for the same key. A peer kid
(`apps/crypto/primitives.kid_for_peer_key`) is 16 base64url characters, and shape does NOT
tell the two apart: lowercase hex is a subset of base64url, so every server kid is also a
well-formed peer kid. What separates them is that they are different functions of different
digest bytes: the server rule hex-encodes digest[0:8], the peer rule base64url-encodes the
digest and keeps 16 characters, which are digest[0:12]. A peer kid equal to a given server
kid therefore needs a key whose digest's first 12 bytes encode to that exact string: a
preimage question on SHA-256, not a matter of shape.

THE KID IS DERIVED FROM THE KEY THAT SIGNED, not read from configuration. A kid copied
from the configured public key would name a key other than the signer whenever the two
disagree, and a signature whose kid names the wrong key verifies against nothing while
looking attributable. `jwt_material_from_env` already refuses a mismatched pair at load;
deriving it here means even an in-process override cannot produce a false attribution.

ONE KEY, TWO MESSAGE FORMATS, NO CONFUSION BETWEEN THEM. A JWS signing input is
`base64url(header) "." base64url(claims)`: only base64url characters and one dot. A result
signature covers the RFC 8785 bytes of a JSON object, which always begin with `{`. No byte
string is both, so a result signature can never be replayed as a token or the reverse.

WHAT A CALLER CHECKS TODAY: nothing. The node client copies `result_signature` and
`result_signing_kid` into its result and never verifies them. The signer choice therefore
affects only a future verifier.

THE KEY IS ALSO REGISTERED UNDER THE MEMORY PEER (operator decision 38, 2026-09-26, option
C; docs/DIVERGENCES.md 017, STATUS F07). A receipt names the memory peer as `issuer` and
carries this kid, and the receipt verifier enforces issuer == targetPeerId. Decision 38
deliberately leaves that rule unchanged; `register_under_memory_peer` makes the pair resolve by
writing this key into the memory peer's `peer_keys`. The consequence, stated rather than hidden:
ONE KEY NOW SPEAKS FOR TWO IDENTITIES. Whatever a live `peer_keys` row admits for the
memory peer (its sign-in challenge, a signed envelope naming it as caller, a key rotation's
co-signature, its `/agents/{id}/jwks.json` when this is its newest live key) the server key
now admits too. Nothing new is trusted by that: the same key already mints every peer's JWT.

WHY AT SIGNING TIME, NOT A MIGRATION. The key is configuration (MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH),
not schema: a migration cannot know it, and would register a kid that a key rotation then
orphans. Step 9 calls the registration before it signs, every memory call: one indexed read
of (peer_id, kid) when the row is already live, a locked insert only the first time a kid is
seen. So it survives a restart (the row is durable), a restore of a database taken before
the key existed (the next call writes it), and a server key rotation (the next call
registers the new kid and moves superseded SERVER registrations into the 24 h grace window,
`apps.peers.keys.GRACE_WINDOW_HOURS`). A key the
memory peer holds in its own right is never touched: a server registration is recognised by
its kid being the SERVER rule's kid of its own stored key, which a peer-rule kid is not
unless the key's digest makes the two encodings agree (a preimage question, above).
A row for this kid that is not live (revoked by the key route or by hand) is LEFT revoked:
revocation is the operator's, and a signer that re-activated it would overrule them. That
call still completes and is still signed; only its receipt stops resolving.
"""
from __future__ import annotations

import dataclasses
import logging
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.crypto import primitives as P

log = logging.getLogger("meshychat.calls.result_signing")


class NoResultSigningKey(RuntimeError):
    """The server has no key to sign a result with. The call must not execute."""


@dataclasses.dataclass(frozen=True)
class ResultSigner:
    private_key: Ed25519PrivateKey
    #: The server kid rule, derived from `private_key` itself (see module docstring).
    kid: str

    def sign(self, payload: dict) -> str:
        """base64url (no padding) of Ed25519 over the RFC 8785 bytes of `payload`.

        `P.jcs` raises on a value RFC 8785 cannot represent (an integer beyond 2**53 - 1,
        for one). That propagates: a result that cannot be canonicalized cannot be signed,
        and the router records a signed FAILURE instead of an unsigned success.
        """
        return P.b64u(self.private_key.sign(P.jcs(payload)))

    def public_key_raw(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def server_result_signer() -> ResultSigner:
    """The signer, from the SAME configuration the JWT minter reads.

    `apps.authapi.config.get_config()` is the minter's source, and the JWKS route publishes
    that configuration's key; reading anything else would let the published key and the
    signing key drift. Raises `NoResultSigningKey` when no private key is configured, which
    the router turns into a failed call BEFORE the skill runs (fail closed: never an
    executed write with an unsigned result).
    """
    from apps.authapi import tokens
    from apps.authapi.config import get_config

    pem = get_config().jwt_signing_key_pem
    if not pem:
        raise NoResultSigningKey(
            "no server signing key is configured (MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH); a "
            "memory result cannot be signed, so the skill is not executed")
    key = tokens.load_signing_key(pem)
    raw = key.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                        format=serialization.PublicFormat.Raw)
    return ResultSigner(private_key=key, kid=P.server_kid(raw))


#: What `register_under_memory_peer` found. Returned (and logged when it is not PRESENT or
#: REGISTERED) so a caller and a test can tell the four apart without reading the table.
PRESENT = "present"          # the kid is already a live key of the memory peer
REGISTERED = "registered"    # inserted now; superseded server registrations put into grace
NOT_LIVE = "not_live"        # a row for the kid exists and is revoked or inactive: left so
NO_PEER = "no_peer"          # the memory peer does not exist or is not active: nothing done


def is_server_registration(kid: str, public_key_b64u: str) -> bool:
    """True when a `peer_keys` row was written by `register_under_memory_peer`: its kid is
    the SERVER rule's kid of its own stored key (16 lowercase hex characters). A key a peer
    publishes gets the PEER rule's kid (16 base64url characters of the same digest), which
    equals the server rule's for the same key only if base64url(digest[0:12]) happens to be
    hex(digest[0:8]): a condition on 96 bits of that one SHA-256 digest (the module
    docstring's preimage question is the cross-key case, matching ANOTHER key's server kid),
    not a shape test, so a peer's own key is not mistaken for a server registration."""
    try:
        raw = P.b64u_decode(public_key_b64u)
    except Exception:  # noqa: BLE001 - an undecodable row is nobody's registration
        return False
    return len(raw) == 32 and P.server_kid(raw) == kid


def register_under_memory_peer(conn, memory_peer_id, signer: ResultSigner) -> str:
    """Make (memory peer, `signer.kid`) resolve to the signer's public key. Idempotent.

    `conn` must be AUTOCOMMIT (the request's `dbconn.serving_connection`, or the worker's),
    so the insert and the grace update commit together in `conn.transaction()`. `peer_keys`
    has no row-level security, and `meshychat_app` holds INSERT/UPDATE on it (apps/peers/db.py).
    The peer row is locked FOR UPDATE first, the lock a key publish takes
    (`apps.peers.keys.lock_peer_for_publish`), so a registration and a rotation of the same
    peer serialize. See the module docstring for why and for the consequence.
    """
    peer = str(uuid.UUID(str(memory_peer_id)))
    public = P.b64u(signer.public_key_raw())
    found = _classify(conn, peer, signer.kid, public)
    if found is not None:
        return found
    from apps.peers.keys import GRACE_WINDOW_HOURS
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT id FROM peers WHERE id = %s AND status = 'active' FOR UPDATE",
                    (peer,))
        if cur.fetchone() is None:
            log.warning("result key %s: memory peer %s is absent or not active; not "
                        "registered, so its receipts will not resolve", signer.kid, peer)
            return NO_PEER
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active, created_at) "
                    "VALUES (%s, %s, %s, TRUE, NOW()) "
                    "ON CONFLICT (peer_id, kid) DO NOTHING RETURNING id",
                    (peer, signer.kid, public))
        if cur.fetchone() is None:
            # Written between the read above and the lock: classify what is there now.
            return _classify(conn, peer, signer.kid, public) or PRESENT
        cur.execute("SELECT kid, public_key FROM peer_keys WHERE peer_id = %s AND kid <> %s "
                    "AND active = TRUE AND revoked_at IS NULL FOR UPDATE",
                    (peer, signer.kid))
        superseded = [k for k, pk in cur.fetchall() if is_server_registration(k, pk)]
        if superseded:
            cur.execute("UPDATE peer_keys SET active = FALSE, "
                        "    revoked_at = NOW() + make_interval(hours => %s) "
                        "WHERE peer_id = %s AND kid = ANY(%s) "
                        "  AND active = TRUE AND revoked_at IS NULL",
                        (GRACE_WINDOW_HOURS, peer, superseded))
    log.info("result key %s registered under memory peer %s%s", signer.kid, peer,
             f"; superseded server kids in grace: {superseded}" if superseded else "")
    return REGISTERED


def _classify(conn, peer: str, kid: str, public: str) -> str | None:
    """PRESENT or NOT_LIVE for an existing (peer, kid) row, None when there is none."""
    with conn.cursor() as cur:
        cur.execute("SELECT active, revoked_at, public_key FROM peer_keys "
                    "WHERE peer_id = %s AND kid = %s", (peer, kid))
        row = cur.fetchone()
    if row is None:
        return None
    active, revoked_at, stored = row
    if stored != public:
        # A kid is a digest of its key, so this is a hand-written row, never a collision.
        log.error("result key %s: memory peer %s holds a DIFFERENT key under this kid; "
                  "left alone, and its receipts will not verify", kid, peer)
        return NOT_LIVE
    if active and revoked_at is None:
        return PRESENT
    log.warning("result key %s under memory peer %s is revoked or inactive; left so "
                "(revocation is the operator's), and its receipts will not resolve",
                kid, peer)
    return NOT_LIVE


__all__ = ["NOT_LIVE", "NO_PEER", "NoResultSigningKey", "PRESENT", "REGISTERED",
           "ResultSigner", "is_server_registration", "register_under_memory_peer",
           "server_result_signer"]
