"""Peer signing keys: publish, rotate, revoke, resolve.

Peer-key publication, rotation, and lookup.
The statement order, the `FOR UPDATE` locks, and the grace-window arithmetic are the
contract, not an implementation detail.

THE THREE THINGS THIS FILE EXISTS TO GET RIGHT

1. PROOF OF POSSESSION. `pop_signature` is an Ed25519 signature by the NEW private key
   over `JCS({peer_id, new_pubkey, nonce, issued_at})`, with snake_case field names on
   the wire. Without it a peer could publish someone else's public key.

2. CO-SIGNATURE. A ROTATION (the peer already has an active key) additionally requires
   `X-Key-CoSig`: the SAME canonical bytes signed by the CURRENT active key. A first
   publish has no key to co-sign with, so it is refused on
   the peer route entirely and must come through the owner-scoped route (the peer route
   answers 403 E_FORBIDDEN). A missing or whitespace-only co-signature is a 401 E_SIG_BAD,
   not a silent skip.

3. THE GRACE WINDOW. Rotation does not delete the old key: it sets `active = FALSE` and
   `revoked_at` is set 24 hours ahead (`GRACE_WINDOW_HOURS = 24`). It is therefore the
   grace-window END, not the moment of
   revocation - `PeerKey.is_revoked_now` is `revoked_at <= now`. So
   `find_by_kid` deliberately resolves a
   key that is `active = TRUE` *OR* whose `revoked_at` is still in the future, and
   in-flight signatures keep verifying for 24 hours.
   Reading `revoked_at IS NOT NULL` as "dead" would break every call mid-rotation.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid

from apps.crypto import primitives as P

from . import errors
from .timefmt import format_rfc3339, now_utc, parse_rfc3339

# Grace window for a superseded key.
GRACE_WINDOW_HOURS = 24
# Maximum accepted issued_at skew.
REPLAY_WINDOW_SECS = 120

_B64URL_NOPAD = re.compile(r"^[A-Za-z0-9_-]+$")


class PeerKey:
    """One `peer_keys` row."""

    __slots__ = ("id", "peer_id", "kid", "public_key", "active", "created_at", "revoked_at")

    def __init__(self, id, peer_id, kid, public_key, active, created_at, revoked_at):
        self.id = id
        self.peer_id = peer_id
        self.kid = kid
        self.public_key = public_key
        self.active = active
        self.created_at = created_at
        self.revoked_at = revoked_at

    def is_revoked_now(self, now: _dt.datetime | None = None) -> bool:
        """`revoked_at` is the grace-window END timestamp."""
        if self.revoked_at is None:
            return False
        return self.revoked_at <= (now or now_utc())

    def view(self) -> dict:
        """Return the peer-key wire view."""
        return {
            "kid": self.kid,
            "public_key": self.public_key,
            "active": self.active,
            "created_at": format_rfc3339(self.created_at),
            "revoked_at": None if self.revoked_at is None else format_rfc3339(self.revoked_at),
        }


_COLS = "id, peer_id, kid, public_key, active, created_at, revoked_at"


def _row(r) -> PeerKey:
    return PeerKey(*r)


# --------------------------------------------------------------------------- kid

def decode_public_key(public_key_b64u: str) -> bytes:
    """Require base64url without padding and exactly 32 bytes, or reject the public key.

    Python's `urlsafe_b64decode` also accepts `+/` and padding. The charset is checked
    explicitly so only one spelling of a key is accepted and stored; the string as sent is
    what is signed.
    """
    if not isinstance(public_key_b64u, str) or not _B64URL_NOPAD.match(public_key_b64u):
        raise errors.invalid_public_key()
    try:
        raw = P.b64u_decode(public_key_b64u)
    except Exception:
        raise errors.invalid_public_key()
    if len(raw) != 32:
        raise errors.invalid_public_key()
    return raw


def derive_kid(public_key_b64u: str) -> str:
    """Return the first 16 base64url-without-padding chars of SHA-256(raw)."""
    return P.kid_for_peer_key(decode_public_key(public_key_b64u))


def _decode_signing_public_key(public_key_b64u: str) -> bytes:
    """Use the same decode, but a stored key that will not decode is a
    SIGNATURE failure (401), not an invalid-input failure (400) - the bad value came
    from the database, not from this caller."""
    try:
        return decode_public_key(public_key_b64u)
    except errors.ApiError:
        raise errors.sig_bad_401("key publish signature verification failed")


def _verify_signature(public_key: bytes, signature_b64u: str, canonical: bytes) -> None:
    """`verify_strict`, which is why `apps.crypto.primitives`
    re-adds the small-order and non-canonical-scalar checks PyNaCl omits."""
    if not isinstance(signature_b64u, str) or not _B64URL_NOPAD.match(signature_b64u):
        raise errors.sig_bad_401("key publish signature verification failed")
    try:
        sig = P.b64u_decode(signature_b64u)
    except Exception:
        raise errors.sig_bad_401("key publish signature verification failed")
    if len(sig) != 64 or not P.verify_strict(public_key, canonical, sig):
        raise errors.sig_bad_401("key publish signature verification failed")


# ----------------------------------------------------------------- proof payload

def validate_proof_fields(req: dict) -> _dt.datetime:
    """Return the parsed issued_at.

    NOTE the two different failure kinds: missing or empty fields are E_SIG_BAD at 400,
    while an out-of-window issued_at is E_SIG_REPLAY at 401. Keeping them distinct tells a
    client what it may retry.
    """
    nonce = req.get("nonce") or ""
    pop = req.get("pop_signature") or ""
    if not str(nonce).strip() or not str(pop).strip():
        raise errors.sig_bad_400("key publish proof is missing or malformed")
    raw_issued = req.get("issued_at")
    if raw_issued is None:
        raise errors.sig_bad_400("key publish proof is missing or malformed")
    try:
        issued_at = parse_rfc3339(raw_issued)
    except ValueError:
        raise errors.sig_bad_400("key publish proof is missing or malformed")
    skew = int(abs((now_utc() - issued_at).total_seconds()))
    if skew > REPLAY_WINDOW_SECS:
        raise errors.sig_replay(
            "key publish nonce replayed or issued_at outside replay window")
    return issued_at


def key_publish_bytes(peer_id, req: dict, issued_at: _dt.datetime) -> bytes:
    """Build the canonical key-publication proof bytes.

    The canonical object uses snake_case: peer_id, new_pubkey, nonce, issued_at. The
    peer ID is the hyphenated lowercase UUID string.
    """
    payload = {
        "peer_id": str(peer_id),
        "new_pubkey": req["public_key"],
        "nonce": req["nonce"],
        "issued_at": format_rfc3339(issued_at),
    }
    return P.jcs(payload)


# -------------------------------------------------------------------- repository

def lock_peer_for_publish(conn, peer_id) -> None:
    """Lock an active peer before publishing its key."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM peers WHERE id = %s AND status = 'active' FOR UPDATE",
                    (str(peer_id),))
        if cur.fetchone() is None:
            raise errors.not_found()


def active_for_update(conn, peer_id) -> list[PeerKey]:
    """The ORDER BY is load-bearing: migration 030 dropped the
    one-active-key unique index, so two active rows can exist transiently."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM peer_keys "
            "WHERE peer_id = %s AND active = TRUE AND revoked_at IS NULL "
            "ORDER BY created_at DESC, id DESC FOR UPDATE", (str(peer_id),))
        return [_row(r) for r in cur.fetchall()]


def remember_nonce(conn, peer_id, nonce: str, issued_at: _dt.datetime) -> bool:
    """Return False when the (peer_id, nonce) pair was already spent."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO key_publish_nonces (peer_id, nonce, issued_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (peer_id, nonce) DO NOTHING RETURNING nonce",
            (str(peer_id), nonce, issued_at))
        return cur.fetchone() is not None


def insert_active(conn, peer_id, kid: str, public_key: str) -> PeerKey:
    """Insert and return an active key."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO peer_keys (peer_id, kid, public_key, active, created_at) "
            f"VALUES (%s,%s,%s,TRUE,NOW()) RETURNING {_COLS}",
            (str(peer_id), kid, public_key))
        return _row(cur.fetchone())


def revoke_old_active(conn, peer_id, new_kid: str) -> None:
    """THE GRACE WINDOW: revoked_at is set to a FUTURE
    timestamp, so the superseded key keeps verifying for `GRACE_WINDOW_HOURS`."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE peer_keys SET active = FALSE, "
            "    revoked_at = NOW() + make_interval(hours => %s) "
            "WHERE peer_id = %s AND kid <> %s AND active = TRUE AND revoked_at IS NULL",
            (GRACE_WINDOW_HOURS, str(peer_id), new_kid))


def count_live_refresh_tokens(conn, peer_id) -> int:
    """Count tokens used by the migration-031 lineage guard."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM refresh_tokens WHERE peer_id = %s "
                    "AND revoked_at IS NULL AND expires_at > NOW()", (str(peer_id),))
        return cur.fetchone()[0] or 0


def reanchor_refresh_tokens(conn, peer_id, new_kid: str, caller_lineage) -> int:
    """LINEAGE-SCOPED on purpose: moving every token would
    re-open the stolen-sibling hole that migration 031 closed. `caller_lineage is
    None` re-anchors NOTHING - there is deliberately no peer-wide fallback."""
    if caller_lineage is None:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE refresh_tokens SET issued_under_kid = %s "
            "WHERE peer_id = %s AND lineage_id = %s AND issued_under_kid IS NOT NULL "
            "  AND revoked_at IS NULL AND expires_at > NOW()",
            (new_kid, str(peer_id), str(caller_lineage)))
        return cur.rowcount


def list_for_peer(conn, peer_id) -> list[PeerKey]:
    """Return every key, active and revoked."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM peer_keys WHERE peer_id = %s "
                    "ORDER BY created_at DESC", (str(peer_id),))
        return [_row(r) for r in cur.fetchall()]


def find_active(conn, peer_id) -> PeerKey | None:
    """Return the newest active key, if any."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM peer_keys "
                    "WHERE peer_id = %s AND active = TRUE AND revoked_at IS NULL "
                    "ORDER BY created_at DESC, id DESC LIMIT 1", (str(peer_id),))
        r = cur.fetchone()
    return None if r is None else _row(r)


def find_by_kid(conn, peer_id, kid: str) -> PeerKey | None:
    """The GRACE-AWARE resolver.

    `active = TRUE OR (revoked_at IS NOT NULL AND revoked_at > NOW())`. This is the
    lookup a signature verifier uses, and it is the only reason a
    call signed just before a rotation still verifies just after it.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM peer_keys WHERE peer_id = %s AND kid = %s "
            "  AND (active = TRUE OR (revoked_at IS NOT NULL AND revoked_at > NOW()))",
            (str(peer_id), kid))
        r = cur.fetchone()
    return None if r is None else _row(r)


def revoke(conn, peer_id, kid: str, force_immediate: bool) -> None:
    """The LEAST(...) clamp is the whole point: grace mode must never EXTEND an existing
    revocation, or re-revoking a dead key would push its cutoff into the future and
    resurrect it for another 24 hours. `force_immediate` still wins outright.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE peer_keys SET active = FALSE, "
            "  revoked_at = CASE WHEN %s::bool THEN NOW() "
            "    ELSE LEAST(COALESCE(revoked_at, 'infinity'::timestamptz), "
            "               NOW() + make_interval(hours => %s)) END "
            "WHERE peer_id = %s AND kid = %s",
            (force_immediate, GRACE_WINDOW_HOURS, str(peer_id), kid))
        if cur.rowcount == 0:
            raise errors.not_found()


# ----------------------------------------------------------------------- service

def publish(conn, peer_id, req: dict, co_signature_b64u: str | None,
            owner_scoped: bool, caller_lineage=None) -> PeerKey:
    """The order of checks is the contract.

    Everything from the peer lock onward runs in ONE transaction, so a rotation cannot
    be observed with two active keys committed or with the nonce spent but no key
    inserted. `conn` must be autocommit (see `db.connection`) or `conn.transaction()`
    degrades to a savepoint inside psycopg's implicit transaction.
    """
    public_key = decode_public_key(req.get("public_key"))
    kid = P.kid_for_peer_key(public_key)
    issued_at = validate_proof_fields(req)
    canonical = key_publish_bytes(peer_id, req, issued_at)
    _verify_signature(public_key, req["pop_signature"], canonical)

    with conn.transaction():
        lock_peer_for_publish(conn, peer_id)

        active = active_for_update(conn, peer_id)
        if not active:
            if not owner_scoped:
                # A bootstrap without owner scope is 403 E_FORBIDDEN.
                raise errors.forbidden()
        else:
            sig = co_signature_b64u.strip() if isinstance(co_signature_b64u, str) else None
            if not sig:
                raise errors.sig_bad_401("key publish co-signature is required")
            _verify_signature(_decode_signing_public_key(active[0].public_key), sig, canonical)

        if not remember_nonce(conn, peer_id, req["nonce"], issued_at):
            raise errors.sig_replay(
                "key publish nonce replayed or issued_at outside replay window")

        # The migration-031 binding guard: a peer self-rotating on a
        # lin-null JWT that still holds live refresh tokens would silently strand them,
        # so it is refused LOUDLY with a remedy instead of becoming a delayed lockout.
        # Owner break-glass is exempt: the peer is expected to re-auth.
        if not owner_scoped and caller_lineage is None:
            if count_live_refresh_tokens(conn, peer_id) > 0:
                raise errors.conflict(
                    "refresh once to bind your lineage before rotating keys: your JWT "
                    "carries no lineage (lin) claim but you hold live refresh tokens, so "
                    "this rotation would strand them; call /v1/auth/refresh to mint a "
                    "lineage-stamped JWT, then retry")

        key = insert_active(conn, peer_id, kid, req["public_key"])
        revoke_old_active(conn, peer_id, kid)
        reanchor_refresh_tokens(conn, peer_id, kid, caller_lineage)
    return key


def build_publish_request(seed32: bytes, peer_id, *, nonce: str | None = None,
                          issued_at: _dt.datetime | None = None) -> dict:
    """Produce a well-formed key-publication request signed by the new key.

    This is the client half of the proof. It lives beside the server half deliberately:
    a test that built the request with `key_publish_bytes` would be agreeing with itself,
    so the canonical bytes are built here from the field list rather than shared.
    """
    public_key = P.b64u(P.public_key_of(seed32))
    req = {
        "public_key": public_key,
        "nonce": nonce or uuid.uuid4().hex,
        "issued_at": format_rfc3339(issued_at or now_utc()),
    }
    canonical = P.jcs({
        "peer_id": str(peer_id),
        "new_pubkey": req["public_key"],
        "nonce": req["nonce"],
        "issued_at": req["issued_at"],
    })
    req["pop_signature"] = P.b64u(P.sign(seed32, canonical))
    req["_canonical"] = canonical      # the bytes a co-signer must sign, too
    return req


def co_sign(seed32: bytes, req: dict) -> str:
    """The `X-Key-CoSig` header value: the CURRENT active key over the SAME canonical
    bytes the new key signed."""
    return P.b64u(P.sign(seed32, req["_canonical"]))
