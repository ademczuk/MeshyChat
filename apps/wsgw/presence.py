"""Presence backend seam for connect, refresh and final socket close.

`LocalPresenceBackend` is the production default for one process (divergence 020). It sends
`peer_online` and `peer_offline` frames directly from the in-process registry to every socket
subscribed to `presence:<peer>`. `RecordingPresenceBackend` remains for tests that inspect the
call log rather than the frames. All three presence lifecycle calls are non-fatal: the local
registry is the authoritative source within this process.

WHY NO COMPARE-AND-DELETE. The consumer calls `mark_offline` only when
`ConnectionManager.deregister` has just reported the peer's last socket gone. Neither action
suspends, so nothing can interleave between them. Shared presence would need a conditional
delete because network writes can arrive out of order; divergence 020 keeps this backend
single-process.

DEBOUNCE (divergence 105). A socket that drops and comes back inside a few seconds is one
peer that never left: a node's reconnect after a network blip, a client that opens a second
socket before closing the first. Sent as they happen, those are an offline and an online that
a watcher has to read as a departure and a return. So the backend holds each change before it
sends it:
  * ARRIVAL_SECS (3 s): `peer_online` goes out once the peer has held a socket that long. A
    peer whose last socket closes inside the window was never shown, so nothing is sent;
  * DEPARTURE_GRACE_SECS (5 s): `peer_offline` goes out once the peer has held no socket that
    long. A socket that comes back inside the grace cancels it, and nothing is sent: watchers
    still believe the peer online, which it is.
A peer already shown online keeps the old rule that every further accepted socket sends
`peer_online` at once. 0 for either window sends that change immediately, as before. The
state is per peer and in this process, like the registry it follows, and each timer
re-checks the registry when it fires, so a frame never contradicts the sockets held then.
This is the socket presence only: the beat route's `peer_presence` frames are not held.
A server restart is NOT smoothed: it is longer than the grace (13 to 20 s), and the new process
starts with no state, so every watcher's own socket reconnects too and each peer arrives anew.

SNAPSHOT ON SUBSCRIBE (divergence 106). Frames are changes, so a watcher that subscribes after a
peer was shown would learn nothing until the next change. `is_shown` answers whether this
backend's last frame for a peer was `peer_online`, and the subscribe handler sends that frame
once to the subscribing socket alone (apps/wsgw/frame_handlers.py). Only `peer_online`: a peer
not shown sends nothing, as before, because "no socket here" is not the same as "gone" (a peer
inside its arrival window, or one on another process, is not shown).

NOT IMPLEMENTED HERE:
  * cross-instance presence through Redis keys `meshy:presence:<peer_id>` (120 s TTL), which the
    dashboard reads as `ws_online`;
  * online/offline events crossing processes. The beat route's row presence does cross
    processes over Postgres NOTIFY (apps/wsgw/mesh_notify.py).
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from . import envelope
from .connection_manager import MANAGER, ConnectionManager
from .constants import PRESENCE_TTL_SECS  # noqa: F401  (part of the interface contract)

log = logging.getLogger("meshychat.wsgw.presence")

#: How long a peer must hold a socket before `peer_online` is sent (divergence 105).
ARRIVAL_SECS = 3.0
#: How long a peer must hold no socket before `peer_offline` is sent (divergence 105).
DEPARTURE_GRACE_SECS = 5.0


class PresenceBackend:
    """The three presence lifecycle calls."""

    async def mark_online(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        raise NotImplementedError

    async def refresh(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        raise NotImplementedError

    async def mark_offline(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        raise NotImplementedError

    def is_shown(self, peer_id: uuid.UUID) -> bool:
        """Whether a subscribe's snapshot may say `peer_online` for this peer (divergence 106).
        A backend that sends no frames shows no one."""
        return False


class LocalPresenceBackend(PresenceBackend):
    """Online/offline frames to this process's `presence:<peer>` subscribers.

    Bound to ONE ConnectionManager, the registry whose sockets it publishes to. A
    consumer built with its own manager must be given a backend bound to that same
    manager, or its presence events go to a registry nobody on it is watching.
    """

    def __init__(self, manager: ConnectionManager, *, arrival_secs: float = ARRIVAL_SECS,
                 departure_grace_secs: float = DEPARTURE_GRACE_SECS, sleep=asyncio.sleep) -> None:
        self.manager = manager
        self.arrival_secs = arrival_secs
        self.departure_grace_secs = departure_grace_secs
        self._sleep = sleep                       # a seam for tests, which release it by hand
        self._shown: set[uuid.UUID] = set()       # peers whose last frame sent was peer_online
        self._held: dict[uuid.UUID, tuple[str, asyncio.Task]] = {}   # "arrival" | "departure"

    def is_shown(self, peer_id: uuid.UUID) -> bool:
        # A peer inside its departure grace is still shown: its watchers were told online and
        # have not been told otherwise.
        return peer_id in self._shown

    def _pending(self, peer_id: uuid.UUID) -> str | None:
        held = self._held.get(peer_id)
        if held is None:
            return None
        if held[1].done():                        # a timer that ended without firing (its loop
            del self._held[peer_id]               # closed under it) holds nothing back
            return None
        return held[0]

    def _cancel(self, peer_id: uuid.UUID) -> None:
        _, task = self._held.pop(peer_id)
        task.cancel()

    def _hold(self, peer_id: uuid.UUID, kind: str, delay: float) -> None:
        task = asyncio.get_running_loop().create_task(self._release(peer_id, kind, delay))
        self._held[peer_id] = (kind, task)

    async def _release(self, peer_id: uuid.UUID, kind: str, delay: float) -> None:
        await self._sleep(delay)
        held = self._held.get(peer_id)
        if held is None or held[1] is not asyncio.current_task():
            return
        del self._held[peer_id]
        # The registry decides: an arrival whose sockets are gone, or a departure whose peer
        # is back, is not sent. Either way a later call starts from what is shown.
        online = await self.manager.is_online(peer_id)
        if (kind == "arrival") != online:
            return
        if online:
            self._shown.add(peer_id)
            frame = envelope.peer_online(peer_id)
        else:
            self._shown.discard(peer_id)
            frame = envelope.peer_offline(peer_id)
        try:
            await self.manager.publish_presence(peer_id, frame)
        except Exception:
            log.exception("presence: publishing the held %s for %s failed", kind, peer_id)

    async def mark_online(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        pending = self._pending(peer_id)
        if pending == "departure":                # back inside the grace: it never left
            self._cancel(peer_id)
            return
        if pending == "arrival":                  # a second socket inside the window: one frame
            return
        if peer_id in self._shown or self.arrival_secs <= 0:
            # Every accepted socket of a peer already shown publishes, not only the first.
            self._shown.add(peer_id)
            await self.manager.publish_presence(peer_id, envelope.peer_online(peer_id))
            return
        self._hold(peer_id, "arrival", self.arrival_secs)

    async def refresh(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        # The in-process registry has no TTL to re-arm: a socket is present until it is
        # deregistered, so there is nothing to do and nothing is sent.
        return None

    async def mark_offline(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        pending = self._pending(peer_id)
        if pending == "arrival":                  # gone before it was shown: nothing to undo
            self._cancel(peer_id)
            return
        if pending == "departure" or peer_id not in self._shown:
            return
        if self.departure_grace_secs <= 0:
            self._shown.discard(peer_id)
            await self.manager.publish_presence(peer_id, envelope.peer_offline(peer_id))
            return
        self._hold(peer_id, "departure", self.departure_grace_secs)


class RecordingPresenceBackend(PresenceBackend):
    """Keeps an ordered log of (event, peer_id, connection_id). Test instrument; it was
    the production default until divergence 020, when it meant presence went nowhere."""

    def __init__(self) -> None:
        self.events: list[tuple[str, uuid.UUID, uuid.UUID]] = []

    async def mark_online(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        self.events.append(("online", peer_id, connection_id))

    async def refresh(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        self.events.append(("refresh", peer_id, connection_id))

    async def mark_offline(self, peer_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        self.events.append(("offline", peer_id, connection_id))


#: The production default: bound to the process-wide registry the consumer uses by
#: default (connection_manager.MANAGER), so the two cannot disagree about which sockets
#: exist.
BACKEND: PresenceBackend = LocalPresenceBackend(MANAGER)
