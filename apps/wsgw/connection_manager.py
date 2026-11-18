"""Shared per-peer WS connection registry.

Three jobs, all of them security-relevant:
  * `register` enforces MAX_CONNS_PER_PEER, bounding the blast radius of ONE
    credential;
  * `subscribe_presence` enforces MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN, because
    Subscribe channels are caller-supplied and unvalidated;
  * `drop_peer` / `drop_connections` are how revocation actually reaches a live
    socket. Their caller is the revocation half of the live bridge
    (apps/wsgw/mesh_notify.py, apps/wsgw/revocation.py; STATUS B15, divergence 030).

ON THE `closed` FLAG. `asyncio.Event` is level-triggered and latches a wakeup,
but the flag is still set BEFORE the event because the consumer's post-register
guard reads it as a plain boolean.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from .constants import (
    CLOSE_FORBIDDEN,
    MAX_CONNS_PER_PEER,
    MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN,
    PRESENCE_PREFIX,
)
from .send_queue import SendQueue


class TooManyConnections(Exception):
    """The peer is already at MAX_CONNS_PER_PEER."""

    def __init__(self) -> None:
        super().__init__(
            f"peer already has the maximum {MAX_CONNS_PER_PEER} concurrent connections"
        )


@dataclass
class ConnectionHandle:
    """One registered socket."""

    connection_id: uuid.UUID
    peer_id: uuid.UUID
    queue: SendQueue
    close: asyncio.Event = field(default_factory=asyncio.Event)
    presence_subscriptions: set[uuid.UUID] = field(default_factory=set)
    #: See the module docstring. Set by drop_peer BEFORE `close` is fired.
    closed: bool = False
    #: The raw Ed25519 public key the socket's token is bound to (its `cnf` did:key), or
    #: None for a bearer token. A key revocation closes exactly the sockets bound to it.
    bound_key: bytes | None = None
    #: What the socket is closed WITH once `closed` is set: written by the drop, BEFORE the
    #: flag, and read by the consumer when it tears the socket down.
    close_code: int = CLOSE_FORBIDDEN
    close_reason: str = ""
    #: Whether the peer held read on shared/mesh-chat WHEN THIS SOCKET CONNECTED. A revoke
    #: can only be a transition for a socket that had it, so this is the evidence the
    #: revocation half of the bridge judges a grant on; a notification's payload is not
    #: (apps/wsgw/revocation.py). Fixed at connect, like `bound_key`.
    mesh_read: bool = False


def parse_presence_channel(channel: str) -> uuid.UUID | None:
    """`presence:<uuid>` or nothing.

    Non-presence channels (e.g. `call:<id>`) return None and are IGNORED by the
    presence path - they are not an error.
    """
    if not channel.startswith(PRESENCE_PREFIX):
        return None
    try:
        return uuid.UUID(channel[len(PRESENCE_PREFIX):])
    except ValueError:
        return None


class ConnectionManager:
    """peer_id -> list of handles."""

    def __init__(self) -> None:
        self._by_peer: dict[uuid.UUID, list[ConnectionHandle]] = {}

    async def register(self, handle: ConnectionHandle) -> None:
        """Raises TooManyConnections at the cap.

        There is no await between the length check and push, so the count cannot
        be raced on a single event loop.
        """
        conns = self._by_peer.setdefault(handle.peer_id, [])
        if len(conns) >= MAX_CONNS_PER_PEER:
            # Do not leave an empty list behind for a peer that never connected.
            if not conns:
                self._by_peer.pop(handle.peer_id, None)
            raise TooManyConnections()
        conns.append(handle)

    async def deregister(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
        """Remove one connection. Returns True when the peer now has NONE left.

        The return value is what gates
        `presence::mark_offline`: a peer with several live sockets must not be
        marked offline when one of them closes. An unknown peer reports True
        ("nothing remains for it").
        """
        conns = self._by_peer.get(peer_id)
        if conns is None:
            return True
        conns[:] = [h for h in conns if h.connection_id != connection_id]
        if not conns:
            del self._by_peer[peer_id]
            return True
        return False

    async def publish_to_peer(self, peer_id: uuid.UUID, frame: dict, *,
                              by_key: dict | None = None) -> int:
        """Fan out to every socket this peer holds.

        `by_key` (divergence 033): a frame for the sockets whose token is
        bound to that key, and `frame` for every other socket, including one that connected
        after the caller looked. The push uses it for the body preview, which is decided per
        key (apps/wsgw/mesh_notify.py)."""
        count = 0
        for handle in self._by_peer.get(peer_id, ()):
            out = by_key.get(handle.bound_key, frame) if by_key else frame
            await handle.queue.push(out)
            count += 1
        return count

    def _handle_by_connection(self, connection_id: uuid.UUID) -> ConnectionHandle | None:
        for handles in self._by_peer.values():
            for handle in handles:
                if handle.connection_id == connection_id:
                    return handle
        return None

    async def subscribe_presence(self, connection_id: uuid.UUID, channels: list[str]) -> int:
        """Returns the number NEWLY inserted.

        At the cap, the remaining channels are dropped and the caller is still
        acked with the full list it sent. A client cannot tell from the ack which
        channels took effect, and a narrower ack would invent a contract.
        """
        handle = self._handle_by_connection(connection_id)
        if handle is None:
            return 0
        inserted = 0
        for channel in channels:
            if len(handle.presence_subscriptions) >= MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN:
                break
            peer_id = parse_presence_channel(channel)
            if peer_id is not None and peer_id not in handle.presence_subscriptions:
                handle.presence_subscriptions.add(peer_id)
                inserted += 1
        return inserted

    async def unsubscribe_presence(self, connection_id: uuid.UUID, channels: list[str]) -> int:
        """Returns the number actually removed."""
        handle = self._handle_by_connection(connection_id)
        if handle is None:
            return 0
        removed = 0
        for channel in channels:
            peer_id = parse_presence_channel(channel)
            if peer_id is not None and peer_id in handle.presence_subscriptions:
                handle.presence_subscriptions.discard(peer_id)
                removed += 1
        return removed

    async def presence_subscribed(self, connection_id: uuid.UUID,
                                  channels: list[str]) -> list[uuid.UUID]:
        """The peers named by `channels` that this socket is subscribed to NOW, in the order
        first named, each once. A subscribe's snapshot answers for these only: a channel the
        cap dropped, or a malformed one, is not here (divergence 106)."""
        handle = self._handle_by_connection(connection_id)
        if handle is None:
            return []
        out: list[uuid.UUID] = []
        for channel in channels:
            peer_id = parse_presence_channel(channel)
            if peer_id is not None and peer_id in handle.presence_subscriptions and peer_id not in out:
                out.append(peer_id)
        return out

    async def publish_presence(self, peer_id: uuid.UUID, frame: dict) -> int:
        """Only to sockets watching `peer_id`."""
        count = 0
        for handles in self._by_peer.values():
            for handle in handles:
                if peer_id in handle.presence_subscriptions:
                    await handle.queue.push(frame)
                    count += 1
        return count

    @staticmethod
    def _fire(handle: ConnectionHandle, code: int, reason: str) -> None:
        # Order: the close code, then the flag, then the wake. A consumer that sees the
        # flag must see the code that goes with it.
        handle.close_code = code
        handle.close_reason = reason
        handle.closed = True
        handle.close.set()

    async def drop_peer(self, peer_id: uuid.UUID, *, code: int = CLOSE_FORBIDDEN,
                        reason: str = "") -> int:
        """Revocation: remove ALL of a peer's connections and wake each socket.

        The flag is stored BEFORE the wake for
        the reason in the module docstring. `code`/`reason` are what each socket is
        closed with (constants.CLOSE_REVOKED_*).
        """
        handles = self._by_peer.pop(peer_id, [])
        for handle in handles:
            self._fire(handle, code, reason)
        return len(handles)

    async def drop_connections(self, peer_id: uuid.UUID, connection_ids, *, code: int,
                               reason: str = "") -> int:
        """Revocation of SOME of a peer's sockets: the ones bound to a revoked key.
        Per-key revocation (divergence 030). The peer's other
        sockets stay registered and keep receiving."""
        wanted = set(connection_ids)
        conns = self._by_peer.get(peer_id)
        if not conns or not wanted:
            return 0
        dropped = [h for h in conns if h.connection_id in wanted]
        conns[:] = [h for h in conns if h.connection_id not in wanted]
        if not conns:
            del self._by_peer[peer_id]
        for handle in dropped:
            self._fire(handle, code, reason)
        return len(dropped)

    async def bound_keys(self) -> list[tuple[uuid.UUID, uuid.UUID, bytes]]:
        """(peer, connection, key) for every registered socket whose token names a key."""
        return [(h.peer_id, h.connection_id, h.bound_key)
                for handles in self._by_peer.values() for h in handles
                if h.bound_key is not None]

    async def mesh_readers(self) -> set[uuid.UUID]:
        """The peers with at least one registered socket that held read on
        shared/mesh-chat when it connected (`ConnectionHandle.mesh_read`)."""
        return {h.peer_id for handles in self._by_peer.values() for h in handles
                if h.mesh_read}

    async def is_online(self, peer_id: uuid.UUID) -> bool:
        """Whether the peer has a registered socket."""
        return peer_id in self._by_peer

    async def peer_count(self) -> int:
        """The number of peers with registered sockets."""
        return len(self._by_peer)

    async def connection_count(self, peer_id: uuid.UUID) -> int:
        """Needed to assert the cap without reaching into `_by_peer` from a test."""
        return len(self._by_peer.get(peer_id, ()))

    async def connected_peers(self) -> list[uuid.UUID]:
        """All peers with registered sockets."""
        return list(self._by_peer.keys())

    async def sockets(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(connection_id, peer_id) for every registered socket. The CallBridge's presence
        beat writes one `hub_connections` row per socket (041 S3)."""
        return [(h.connection_id, h.peer_id)
                for handles in self._by_peer.values() for h in handles]

    async def connection(self, peer_id: uuid.UUID,
                         connection_id: uuid.UUID) -> ConnectionHandle | None:
        """The peer's registered socket `connection_id`, or None once it is gone. The
        CallBridge re-pushes a lost terminal frame (F04, docs/DIVERGENCES.md 048) to
        the one socket whose queue lost it, and only while that socket is registered. Reads the
        peer's own list, at most MAX_CONNS_PER_PEER handles."""
        for handle in self._by_peer.get(peer_id, ()):
            if handle.connection_id == connection_id:
                return handle
        return None

    async def socket_keys(self) -> dict[uuid.UUID, set[bytes | None]]:
        """Every connected peer, with the keys its sockets' tokens are bound to (None for a
        bearer token). The push decides the body preview per key
        (divergence 033)."""
        return {peer: {h.bound_key for h in handles}
                for peer, handles in self._by_peer.items()}


#: The hub's process-wide registry. A Channels consumer has no injected shared
#: state, so the module-level instance serves that role. Tests build their own
#: to stay isolated.
MANAGER = ConnectionManager()
