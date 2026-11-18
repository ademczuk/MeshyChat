"""Revocation reaches an open socket.

STATUS B15, divergence 030. `ConnectionManager.drop_peer` was the whole mechanism and
nothing called it, so a peer that lost its standing kept its socket until its JWT expired.

Three causes are DB-VERIFIED before a socket is closed:

  cause                      how it is signalled                        close code
  -------------------------  -----------------------------------------  ----------
  peers.status not 'active'  the standing sweep; or a NOTIFY an         4410
                             operator sends in the suspending
                             transaction (nothing in this project writes peers.status)
  read on shared/mesh-chat   memory.grant {revoke} NOTIFYs in its own   4411
  revoked                    transaction; the sweep finds one revoked
                             behind the route's back (or expired)
  the key a token is bound   DELETE /v1/peers/me/keys/{kid} NOTIFYs;    4412
  to (`cnf`) revoked         the sweep finds a grace window that ended

THE PAYLOAD IS A POINTER, as for mesh_msg (divergence 020): NOTIFY is unauthenticated, any
session on the database can send one. So a notification only says WHICH peer to look at;
every decision is read back from the tables below, and the `namespace` it may carry decides
nothing. A forged notification about a peer in good standing closes nothing.

WHO COUNTS AS HAVING LOST READ. A peer may hold a socket with no read on shared/mesh-chat
at all: it gets no mesh push, and still pings and watches presence (divergence 020,
tests/test_mesh_live.py's erin). Closing every such socket would be a new ban, not a
revocation. So the grant cause fires only on a TRANSITION, and the evidence of one is this
process's own, never a payload: the peer's socket HELD read when it connected (the consumer
records it, `ConnectionHandle.mesh_read`), or a sweep SAW the peer holding read. Either way
the peer is judged again from the tables, and closed only if it holds no read now.

Until the review of c766b76 a notification that merely NAMED shared/mesh-chat was taken as
the evidence, so a forged one, or an admin revoking a grant that had already expired,
closed a socket that never had read (tests/test_ws_revocation.py, section 5).

A KEY is judged by the rows that name it: live if any row for this peer and this key is
live (`active` and never revoked, or inside its grace window), dead if rows exist and none
is, UNKNOWN if none exists - and an unknown key is not a revoked one. The server mints `cnf`
only from `peer_keys` (apps/authapi), so unknown means a row deleted by hand; that is left
open and said so in divergence 030, rather than guessed at.
"""
from __future__ import annotations

import dataclasses
import json
import uuid

from apps.core import rls
from apps.crypto import primitives as P

from .constants import (
    CLOSE_REVOKED_GRANT,
    CLOSE_REVOKED_KEY,
    CLOSE_REVOKED_PEER_INACTIVE,
    REVOCATION_NOTIFY_CHANNEL,
)

PEER_INACTIVE = "peer_inactive"
GRANT_REVOKED = "grant_revoked"
KEY_REVOKED = "key_revoked"

#: cause -> (close code, close reason). The reason is for a human reading a client log.
CLOSES = {
    PEER_INACTIVE: (CLOSE_REVOKED_PEER_INACTIVE, "revoked: peer is no longer active"),
    GRANT_REVOKED: (CLOSE_REVOKED_GRANT, "revoked: read on shared/mesh-chat was revoked"),
    KEY_REVOKED: (CLOSE_REVOKED_KEY, "revoked: the key this token is bound to was revoked"),
}

KEY_LIVE = "live"
KEY_DEAD = "dead"
KEY_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- signalling

def notify(conn, peer_id, *, namespace: str | None = None, kid: str | None = None) -> None:
    """Ask every serving process to re-read `peer_id`'s standing now.

    Run it on the connection, and inside the transaction, that made the revoking write:
    pg_notify is delivered on COMMIT and dropped on rollback, so a revoke that did not
    happen never closes a socket. A lost notification is not a lost revocation for status
    and keys - the sweep re-reads both - and is for a grant only when the LISTEN connection
    is down at that moment (divergence 030).
    """
    payload: dict = {"peer_id": str(peer_id)}
    if namespace is not None:
        payload["namespace"] = namespace
    if kid is not None:
        payload["kid"] = kid
    with conn.cursor() as cur:
        cur.execute("SELECT pg_notify(%s, %s)", (REVOCATION_NOTIFY_CHANNEL, json.dumps(payload)))


@dataclasses.dataclass(frozen=True)
class Notice:
    peer_id: uuid.UUID
    #: The namespace a revoked grant was on, when the sender named one.
    namespace: str | None = None


def parse_notice(raw: str) -> Notice | None:
    """A notification, or None when it is not one (skipped, never fatal)."""
    try:
        obj = json.loads(raw)
        peer_id = uuid.UUID(str(obj["peer_id"]))
    except (ValueError, TypeError, KeyError):
        return None
    namespace = obj.get("namespace") if isinstance(obj, dict) else None
    return Notice(peer_id=peer_id, namespace=namespace if isinstance(namespace, str) else None)


# ----------------------------------------------------------------------------- standing

def inactive_peers(conn, peer_ids) -> set[uuid.UUID]:
    """The peers among `peer_ids` that are not `status = 'active'`, including a missing row:
    the upgrade admits only `EXISTS(... status = 'active')`, so a socket whose row is gone has
    no standing either. `peers` has no RLS."""
    ids = list(peer_ids)
    if not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM peers WHERE id = ANY(%s) AND status = 'active'", (ids,))
        active = {row[0] for row in cur.fetchall()}
    return set(ids) - active


def _raw_key(text) -> bytes | None:
    try:
        return P.b64u_decode(str(text))
    except (ValueError, TypeError):
        return None


def key_live_sql(alias: str = "") -> str:
    """THE rule a `peer_keys` row is live by, as SQL: `active` and never revoked, or inside
    its grace window. One definition, read by `key_states` below and by the live-key clause
    every target frame's statement carries (apps/wsgw/target_frames.py, divergence 041 S4).
    `alias` qualifies the columns (`k` gives `k.revoked_at`). ONE parenthesised expression,
    so a caller may write `... AND <it>` (bare, that read `(... AND a) OR b`; S4 review,
    finding 8)."""
    p = f"{alias}." if alias else ""
    return f"(({p}revoked_at IS NULL AND {p}active = TRUE) OR {p}revoked_at > NOW())"


def key_states(conn, bound) -> dict[tuple[uuid.UUID, bytes], str]:
    """{(peer, key): live|dead|unknown} for each `(peer, key)` in `bound`.

    Live is `active AND revoked_at IS NULL` or `revoked_at > NOW()` - the grace-aware rule
    a signature verifier resolves keys by (apps/calls/envelope.py, apps/peers/keys.py
    `find_by_kid` + `is_revoked_now`). Compared by DECODED key bytes, so the stored
    spelling of the base64 cannot make a revoked key look unknown. `peer_keys` has no RLS.
    """
    pairs = {(peer, key) for peer, key in bound}
    if not pairs:
        return {}
    peers = list({peer for peer, _ in pairs})
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT peer_id, public_key, {key_live_sql()} "
            "FROM peer_keys WHERE peer_id = ANY(%s)", (peers,))
        rows = cur.fetchall()
    seen: dict[tuple[uuid.UUID, bytes], bool] = {}
    for peer_id, public_key, live in rows:
        raw = _raw_key(public_key)
        if raw is None:
            continue
        key = (peer_id, raw)
        seen[key] = seen.get(key, False) or bool(live)
    out = {}
    for pair in pairs:
        if pair not in seen:
            out[pair] = KEY_UNKNOWN
        else:
            out[pair] = KEY_LIVE if seen[pair] else KEY_DEAD
    return out


def has_mesh_read(conn, peer_id) -> bool:
    """`read` on shared/mesh-chat by THE predicate the mesh push and the reads use
    (`memoryskills.permissions.check_permission`), never a copy of it.

    Imported here, not at the top: `apps.memoryskills` imports its handlers on package
    import, and `memory.grant` imports `notify` from this module, so a top-level import
    would be a cycle whichever side is imported first."""
    from apps.memoryskills.errors import HandlerError
    from apps.memoryskills.permissions import NAMESPACE_WIDE, check_permission
    from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

    with rls.server_internal(conn):
        try:
            check_permission(MESH_CHAT_NAMESPACE, peer_id, "read", conn,
                             resource=NAMESPACE_WIDE)
        except HandlerError:
            return False
    return True


@dataclasses.dataclass(frozen=True)
class Drop:
    """One decision: close these sockets of this peer (all of them when `connections` is
    None) for this cause."""

    peer_id: uuid.UUID
    cause: str
    connections: tuple[uuid.UUID, ...] | None = None


def judge(conn, peers, bound, grant_watch) -> tuple[list[Drop], set[uuid.UUID]]:
    """Decide, for the connected `peers`, which sockets have lost their standing.

    `bound` is `ConnectionManager.bound_keys()`; `grant_watch` the peers whose read on
    shared/mesh-chat is to be checked: those that had it, at connect or at a sweep (see the
    module docstring: only on a transition).
    Returns the drops, in cause order (inactive, then key, then grant), and the peers of
    `grant_watch` found STILL holding read. A peer dropped for one cause is not judged for
    the next: its sockets are already gone.
    """
    peers = set(peers)
    drops: list[Drop] = []
    inactive = inactive_peers(conn, peers)
    for peer in sorted(inactive, key=str):
        drops.append(Drop(peer, PEER_INACTIVE))
    live_bound = [(p, c, k) for p, c, k in bound if p in peers and p not in inactive]
    states = key_states(conn, [(p, k) for p, _, k in live_bound])
    by_peer: dict[uuid.UUID, list[uuid.UUID]] = {}
    for peer, connection, key in live_bound:
        if states.get((peer, key)) == KEY_DEAD:
            by_peer.setdefault(peer, []).append(connection)
    for peer in sorted(by_peer, key=str):
        drops.append(Drop(peer, KEY_REVOKED, tuple(by_peer[peer])))
    readers: set[uuid.UUID] = set()
    for peer in sorted(set(grant_watch) & peers - inactive, key=str):
        if has_mesh_read(conn, peer):
            readers.add(peer)
        else:
            drops.append(Drop(peer, GRANT_REVOKED))
    return drops, readers


def readers_among(conn, peers) -> set[uuid.UUID]:
    """The peers among `peers` that hold read on shared/mesh-chat now."""
    return {peer for peer in peers if has_mesh_read(conn, peer)}
