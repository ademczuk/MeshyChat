"""The Channels consumer for WebSocket connections.

`connect` runs the pre-upgrade gate chain, while `receive` and the writer
task handle the live socket. The gate ORDER is load-bearing: Origin is checked
before the JWT and peer lookup because it is the cheapest refusal and leaks
nothing about peer state to a request that will be refused anyway.

    1. Origin allow-list                         -> 403
    2. JWT in Sec-WebSocket-Protocol             -> 401
    3. peers.status = 'active'                   -> 403 (500 on DB error)
    4. DPoP proof, when enforced                 -> 401
   4b. the bound key not revoked  (divergence 030)     -> 4412
   4c. mesh-chat read, RECORDED   (divergence 030)     -> never refuses
    5. JWT exp captured
    6. 0RTT agent-card hash                       -> 426
    -- upgrade --
    7. register, per-peer cap                     -> Error frame + close
    8. revoked-during-setup guard
    9. presence online
   10. welcome frame
   11. live bridge started                        (divergence 020)
   12. call bridge started, and   (divergence 041 S3; design 4.3 and 4.6)
       the presence row written
       once it is LISTENING
   13. in-flight calls redelivered (divergence 041 S5, design 4.8),
       spawned, so the welcome stays the first frame

LIVE DELIVERY (divergence 020). Mesh nudges and beat presence reach this socket
through `apps/wsgw/mesh_notify.LiveBridge`, which LISTENs on Postgres instead of
subscribing to Redis. The first socket in a process starts it; step 11 waits,
bounded, for its LISTEN so a message committed right after the welcome is not
missed. Online/offline frames come from `presence.LocalPresenceBackend`.

REVOCATION (STATUS B15, divergence 030). The live bridge's revocation half
(apps/wsgw/revocation.py) calls `ConnectionManager.drop_peer`/`drop_connections`
when a connected peer is suspended, loses read on shared/mesh-chat, or has the key
its token is bound to revoked. The handle carries the typed close code; this
consumer stops dispatching and writing the moment the handle is marked closed, then
closes with that code.

CALL DELIVERY AND PRESENCE (divergence 041 S3). The CallBridge
(apps/wsgw/call_notify.py) LISTENs on the call triggers' pointers; the first socket in a
process starts it (step 12), after the live bridge. This socket's `hub_connections` row is
written only once that bridge reports LISTENING (`attach`), never at `register` (step 7): a
row from a worker that cannot deliver would route calls into a black hole. `disconnect`
deletes it after deregistering (`detach`). Since 041 S5 the bridge delivers `skill_invoke`
to this socket; step 13 spawns this peer's redelivery (`CallBridge.redeliver`), and a loss
the writer finds in the send queue asks the bridge to offer the unanswered calls again at
once (`CallBridge.request_reoffer`). Since F04 (docs/DIVERGENCES.md 048) the same request
names the calls whose TERMINAL frame the loss took, and the bridge's loss pass pushes each one
to this socket again.

THE TARGET'S FRAMES (divergence 041 S4). skill_ack, skill_running, skill_result and
skill_error run on this socket's OWN database connection (`frames_db`,
apps/wsgw/target_frames.SocketConnection): opened at the first such frame, used by every
later one in wire order, and closed in `disconnect`. Each is one guarded statement carrying
the socket's bound key (`ConnectionHandle.bound_key`) into its live-key clause.

THE CALLER'S FRAMES (divergence 041 S6). skill_call is SPAWNED (`_spawn`, tracked, cancelled
at disconnect) under this socket's 16 slots (`call_slots`) and a process-wide cap
(apps/wsgw/caller_frames.py), so the reader never waits on a pipeline. A call's caller-side
frames reach this socket through the CallRegistry (`call_registry`), the one this socket's
CallBridge delivers from; `disconnect` removes every registration of the socket.
skill_cancel runs on the socket's own connection like the target frames.

WHAT IS NOT HERE, named rather than faked: chunk frames (041 S7).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from channels.generic.websocket import AsyncWebsocketConsumer

from apps.core import metrics, peer_limit
from apps.core.env import lookup

from . import auth, db, envelope, revocation, target_frames
from .connection_manager import MANAGER, ConnectionHandle, ConnectionManager, TooManyConnections
from .constants import (
    CLOSE_CREDENTIAL_EXPIRED,
    CLOSE_FORBIDDEN,
    CLOSE_INTERNAL_ERROR,
    CLOSE_REVOKED_KEY,
    CLOSE_TOO_MANY_CONNECTIONS,
    CLOSE_UNAUTHORIZED,
    CLOSE_UNIMPLEMENTED,
    CLOSE_UPGRADE_REQUIRED,
    MAX_MESSAGE_SIZE,
    NEGOTIATED_PROTOCOL,
)
from .didkey import DidKeyError, from_did_key
from .frame_handlers import FrameContext, SkillFrameNotImplemented, dispatch
from .call_notify import CALL_BRIDGE as DEFAULT_CALL_BRIDGE
from .call_notify import CallBridge
from .call_registry import CallRegistry
from .caller_frames import CallSlots
from .mesh_notify import BRIDGE as DEFAULT_BRIDGE
from .mesh_notify import LiveBridge
from .presence import BACKEND as DEFAULT_PRESENCE
from .presence import PresenceBackend
from .send_queue import SendQueue

log = logging.getLogger(__name__)

#: The frames `receive` does not charge to the peer's FRAMES bucket (B12, apps/core/peer_limit).
#: A skill_call is charged to the CALLS bucket by `start_call`, where every transport's call is,
#: so charging it here as well would count it twice (caller_frames checks that bucket before a
#: pipeline thread is used). A target frame that lands answers a call that was charged to its
#: caller, and a limit on it would cut a stream's chunks mid-call; one that is REFUSED is charged,
#: and one that meets an empty FRAMES bucket is refused first, in frame_handlers._target_frame.
_UNCHARGED_FRAMES = frozenset({
    envelope.SKILL_CALL,
    envelope.SKILL_ACK, envelope.SKILL_RUNNING, envelope.SKILL_CHUNK,
    envelope.SKILL_RESULT, envelope.SKILL_ERROR,
})


@dataclass
class HubConfig:
    """The configuration this hub reads."""

    #: None = not configured; `connect` then refuses every upgrade (divergence 015).
    jwt_public_key_pem: bytes | None
    #: The exact value used by the minter and HTTP verifier. This used to default
    #: to `meshy` while the minter defaulted to `meshy-server`; no default: mismatched
    #: issuer settings must fail closed rather than silently accept incompatible tokens.
    jwt_issuer: str | None
    #: DPoP enforcement, default ON. The parsed default is true (divergence 015).
    dpop_enforce: bool = True
    #: The allowed dashboard origins. Empty is the documented production posture; a same-origin request still
    #: passes on the Host match.
    dashboard_cors_origins: list[str] = field(default_factory=list)
    #: The peers lookup. Switchable so a unit test can exercise
    #: the frame layer without a database; it is ON by default and the
    #: integration tests leave it on.
    check_peer_active: bool = True
    #: The 0RTT negotiation, opt-in and fail-open.
    zero_rtt: bool = True


def config_from_env() -> HubConfig:
    """Production wiring. The JWT key, issuer and DPoP switch come from the SAME settings
    the HTTP verifier reads, which meshychat/settings.py builds with the SAME parser the
    minter uses (apps/authapi/config.py:jwt_material_from_env, divergence 015). Reading
    separate `MESHY_JWT_*` variables with separate defaults could leave the gateway on a
    different issuer with DPoP off."""
    from django.conf import settings

    origins = lookup("MESHYCHAT_DASHBOARD_CORS_ORIGINS", "")
    return HubConfig(
        jwt_public_key_pem=getattr(settings, "MESHY_JWT_PUBLIC_KEY_PEM", None),
        jwt_issuer=getattr(settings, "MESHY_JWT_ISSUER", None),
        # Only a literal False turns it off, exactly as apps/peers/auth.py:dpop_enforced.
        dpop_enforce=getattr(settings, "MESHY_DPOP_ENFORCE", None) is not False,
        dashboard_cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )


def _bound_key(cnf) -> bytes | None:
    """The raw public key a token's `cnf` names, or None (a bearer token, or a `cnf` that
    is not a did:key - which DPoP enforcement has already refused when it is on)."""
    if not isinstance(cnf, str) or not cnf:
        return None
    try:
        return from_did_key(cnf)
    except (DidKeyError, ValueError):
        return None


class MeshyWebSocketConsumer(AsyncWebsocketConsumer):
    """`GET /v1/ws`."""

    def __init__(
        self,
        config: HubConfig | None = None,
        manager: ConnectionManager | None = None,
        presence: PresenceBackend | None = None,
        bridge: LiveBridge | None = None,
        call_bridge: CallBridge | None = None,
    ) -> None:
        super().__init__()
        self.config = config or config_from_env()
        self.manager = manager or MANAGER
        self.presence = presence or DEFAULT_PRESENCE
        # The default bridge feeds the DEFAULT registry. A consumer given its own manager
        # and no bridge gets none, rather than a bridge writing into a registry this
        # socket is not in (which would look like a working push and deliver nothing).
        if bridge is not None:
            self.bridge: LiveBridge | None = bridge
        else:
            self.bridge = DEFAULT_BRIDGE if manager is None else None
        # The same rule for the call bridge (041 S3): its beat reads the registry it was
        # built with, so a consumer with its own registry and no call bridge gets none.
        if call_bridge is not None:
            self.call_bridge: CallBridge | None = call_bridge
        else:
            self.call_bridge = DEFAULT_CALL_BRIDGE if manager is None else None
        # The registry this socket's call registrations go in (041 S6): its bridge's, which
        # delivers the terminal frames. With no bridge, a private one: nothing delivers from it,
        # and a caller then hears only what its own frames' reads push.
        self.call_registry: CallRegistry = (self.call_bridge.registry
                                            if self.call_bridge is not None else CallRegistry())
        # MAX_INFLIGHT_SKILL_CALLS for this socket, and the dispatches it spawned (041 S6).
        self.call_slots = CallSlots()
        self._skill_calls: set[asyncio.Task] = set()
        self.connection_id = uuid.uuid4()
        self.peer_id: uuid.UUID | None = None
        self.queue = SendQueue()
        self.handle: ConnectionHandle | None = None
        # The socket's own connection for its target frames (041 S4), opened lazily: a socket
        # that never answers a call never holds one.
        self.frames_db = target_frames.SocketConnection(self.connection_id)
        self._writer: asyncio.Task | None = None
        self._expiry: asyncio.Task | None = None
        self._revocation: asyncio.Task | None = None
        self._redelivery: asyncio.Task | None = None
        self._registered = False
        # Set by every close the SERVER starts (`close` below): the reader stops at once.
        self._closing = False
        # Set when the socket's teardown has run (`_teardown`): once, from `disconnect` or from
        # a close the server refused (`close`).
        self._torn_down = False

    async def __call__(self, scope, receive, send):
        """The consumer's whole life, with the teardown in a `finally`.

        `disconnect` (and the refused-close path in `close`) used to be the only callers of
        `_teardown`. When daphne gives up on an application (its application_close_timeout,
        or a stalled upgrade) it CANCELS the task running this method, Channels' dispatch loop
        unwinds with CancelledError, and neither ran: the socket stayed registered in the
        ConnectionManager with no socket behind it. MAX_CONNS_PER_PEER such cancellations
        locked the peer out of every later upgrade with TOO_MANY_CONNECTIONS (HQD production,
        27 Oct 2026, 12:50Z: all ten HQB identities refused for 7.5 h until a restart).
        `_teardown` runs at most once, so after a normal `disconnect` this does nothing. It
        runs shielded for as long as it takes, whatever number of further cancels arrive (daphne's
        application checker cancels again on every tick while the application is not done, and a
        stall is exactly when the teardown is slow: pxx, #441 review). `_teardown` marks itself done
        on its first line, so a teardown cut off partway would never be retried. Once it has
        finished, its own exception (if any) surfaces, and a cancellation that arrived is re-raised
        once."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self._torn_down:
                teardown = asyncio.ensure_future(self._teardown())
                cancelled = False
                while not teardown.done():
                    try:
                        await asyncio.shield(teardown)
                    except asyncio.CancelledError:
                        cancelled = True
                teardown.result()
                if cancelled:
                    raise asyncio.CancelledError

    # --- the gate chain ------------------------------------------------------

    async def connect(self) -> None:
        headers = {name.lower(): value for name, value in self.scope.get("headers", ())}
        entries = auth.entries_from_scope(self.scope)
        path = self.scope.get("path", "")

        # 1. Origin. First, and before any crypto or DB.
        if not auth.origin_allowed(headers, self.config.dashboard_cors_origins):
            log.warning("WS upgrade rejected: disallowed Origin")
            await self.close(code=CLOSE_FORBIDDEN)
            return

        # Nothing to verify against is a server fault, never a pass (divergence 015).
        # Checked before verification because PyJWT given issuer=None SKIPS the issuer check.
        if not self.config.jwt_public_key_pem or not self.config.jwt_issuer:
            log.error("WS upgrade refused: JWT verification key or issuer is not configured")
            await self.close(code=CLOSE_INTERNAL_ERROR)
            return

        # 2. JWT-in-subprotocol.
        try:
            verified = auth.verify_upgrade(
                entries, self.config.jwt_public_key_pem, self.config.jwt_issuer
            )
        except auth.AuthError as exc:
            log.warning("WS upgrade rejected: %s", exc)
            await self.close(code=CLOSE_UNAUTHORIZED)
            return
        self.peer_id = verified.peer_id

        # 3. peers.status = 'active'.
        if self.config.check_peer_active:
            try:
                active = await db.peer_is_active(verified.peer_id)
            except Exception:
                log.exception("WS peer status lookup failed")
                await self.close(code=CLOSE_INTERNAL_ERROR)
                return
            if not active:
                log.warning("WS upgrade rejected: inactive peer %s", verified.peer_id)
                await self.close(code=CLOSE_FORBIDDEN)
                return

        # 4. DPoP.
        if self.config.dpop_enforce:
            # Under enforce (TASK-46), a BEARER token with cnf
            # absent - is DENIED outright, not allowed to fall through. The WS
            # upgrade is the write-capable channel boundary and there is no
            # per-frame DPoP, so a bearer upgrade would be the WS analogue of
            # the HTTP bearer-write hole.
            if not verified.cnf:
                log.warning("WS upgrade rejected: DPoP required (bearer cnf:None under enforce)")
                await self.close(code=CLOSE_UNAUTHORIZED)
                return
            token = next(
                (e[len("jwt."):] for e in entries if e.startswith("jwt.")), ""
            )
            ok, reason, jti = auth.verify_ws_dpop_proof(entries, verified.cnf, token, path)
            if not ok:
                log.warning("WS upgrade rejected: DPoP %s", reason)
                await self.close(code=CLOSE_UNAUTHORIZED)
                return
            wire = auth.extract_dpop_wire(entries) or ""
            try:
                fresh = await db.claim_dpop_jti(jti, verified.peer_id, auth.dpop_expiry(wire))
            except Exception:
                log.exception("WS upgrade rejected: DPoP jti ledger error")
                await self.close(code=CLOSE_UNAUTHORIZED)
                return
            if not fresh:
                log.warning("WS upgrade rejected: DPoP replay")
                await self.close(code=CLOSE_UNAUTHORIZED)
                return

        # 4b. The key the token is bound to is not revoked (divergence 030). A socket
        # closed by a key revocation must not come straight back on the same unexpired
        # token. A key with no row at all is UNKNOWN, not revoked (apps/wsgw/revocation.py).
        bound_key = _bound_key(verified.cnf)
        if bound_key is not None and self.config.check_peer_active:
            try:
                state = await db.key_state(verified.peer_id, bound_key)
            except Exception:
                log.exception("WS key standing lookup failed")
                await self.close(code=CLOSE_INTERNAL_ERROR)
                return
            if state == revocation.KEY_DEAD:
                log.warning("WS upgrade rejected: the bound key is revoked (peer %s)",
                            verified.peer_id)
                await self.close(code=CLOSE_REVOKED_KEY)
                return

        # 4c. Whether the peer holds read on shared/mesh-chat NOW (divergence 030). Not a
        # gate: a peer with no read may hold a socket (divergence 020). It is the evidence
        # the bridge judges a later grant revoke on: only a socket that HAD read can lose
        # it, and a notification's payload is no evidence at all. A failed lookup records
        # True: the socket is then re-judged from the tables, never closed on the guess.
        mesh_read = False
        if self.config.check_peer_active:
            try:
                mesh_read = await db.mesh_read(verified.peer_id)
            except Exception:
                log.exception("WS mesh-chat read lookup failed; the socket is re-judged")
                mesh_read = True

        # 6. 0RTT agent-card hash. Fail-open by design.
        if self.config.zero_rtt and verified.used_protocol_hash:
            try:
                card = await db.agent_card(verified.peer_id)
                if card is not None and auth.protocol_hash(card) != verified.used_protocol_hash:
                    log.warning("WS 0RTT miss: usedProtocolHash stale")
                    await self.close(code=CLOSE_UPGRADE_REQUIRED)
                    return
            except Exception:
                log.exception("WS 0RTT hash compute failed (fail-open to upgrade)")

        # -- upgrade. The handshake offers only the negotiated protocol; jwt. and dpop.
        # entries are never echoed.
        await self.accept(subprotocol=NEGOTIATED_PROTOCOL)

        # 7. Register, enforcing the per-peer cap.
        handle = ConnectionHandle(
            connection_id=self.connection_id,
            peer_id=verified.peer_id,
            queue=self.queue,
            bound_key=bound_key,
            mesh_read=mesh_read,
        )
        try:
            await self.manager.register(handle)
        except TooManyConnections:
            log.warning("WS upgrade rejected: per-peer connection cap reached")
            await self.send(text_data=json.dumps(envelope.error(
                envelope.CODE_TOO_MANY_CONNECTIONS, "per-peer connection limit reached"
            )))
            await self.close(code=CLOSE_TOO_MANY_CONNECTIONS)
            return
        self.handle = handle
        self._registered = True

        # 8. Revoked during the setup window.
        if handle.closed:
            log.info("WS closing immediately: revoked during setup")
            await self.manager.deregister(verified.peer_id, self.connection_id)
            self._registered = False
            await self.close(code=handle.close_code, reason=handle.close_reason or None)
            return

        # 9. Presence: non-fatal.
        try:
            await self.presence.mark_online(verified.peer_id, self.connection_id)
        except Exception:
            log.exception("presence.mark_online failed")

        # 10. Welcome, queued immediately.
        await self.queue.push(
            envelope.welcome(_dt.datetime.now(_dt.timezone.utc), self.connection_id)
        )

        self._writer = asyncio.create_task(self._writer_loop())
        self._revocation = asyncio.create_task(self._await_revocation(handle))

        # The JWT-expiry deadline: a session may not outlive
        # its token, whether or not a revocation event ever arrives. An expiry we
        # could not determine disables the deadline rather than closing a
        # freshly-authenticated socket.
        if verified.expires_at is not None:
            remaining = max(0, verified.expires_at - int(time.time()))
            self._expiry = asyncio.create_task(self._force_close_at_expiry(remaining))

        # 11. Live delivery (divergence 020). AFTER the welcome is queued, so the first
        # frame a client reads is still `welcome`. Non-fatal, like presence: a socket
        # with no push still answers pings and subscriptions, and the client's poll
        # remains the authoritative path to the message.
        if self.bridge is not None:
            try:
                await self.bridge.ensure_started()
            except Exception:
                log.exception("live bridge failed to start")

        # 12. Call delivery and cross-worker presence (divergence 041 S3). The call bridge
        # starts with the first socket, like the live bridge, and this socket's presence row
        # is written only once it reports LISTENING: `attach` waits for that, bounded, and
        # writes nothing if it does not come. A socket whose row is missing is written by the
        # bridge's first beat after it listens. Non-fatal, like step 11.
        if self.call_bridge is not None:
            try:
                await self.call_bridge.attach(self.connection_id, verified.peer_id)
            except Exception:
                log.exception("call bridge failed to start or to record this socket")

        # 13. Redelivery (divergence 041 S5, design 4.8) runs after the welcome and presence
        # row because frames are queued directly. It is SPAWNED: the socket answers its frames
        # while redelivery reads. The bridge
        # routes the peer's `pending` and `queued` calls (committed before any invoke) and
        # sends the `routed` and `running` ones to this socket again.
        if self.call_bridge is not None:
            self._redelivery = asyncio.create_task(self._redeliver(handle))

    async def _redeliver(self, handle: ConnectionHandle) -> None:
        try:
            await self.call_bridge.redeliver(handle)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("call redelivery to this socket failed; the re-offer sweep and the "
                          "next connect offer its calls again")

    # --- the live socket -----------------------------------------------------

    async def _writer_loop(self) -> None:
        """Drain the send queue into the socket.

        The loss check sits HERE because this is the one seam every outbound frame
        crosses. `SendQueue` counted its drops from the start, but nothing read the
        counter outside the tests, so a slow socket lost frames and neither end ever
        found out. That is the fail-open shape docs/V1-DESIGN-RULES.md E1 names as
        the single largest defect class: "a failure and a pass looked identical."
        """
        try:
            while True:
                frame = await self.queue.pop()
                if self._revoked():
                    # A frame queued before the revocation landed is not written after it.
                    return
                loss = self.queue.take_loss()
                if loss is not None:
                    # Reported, and silent on the wire. Closing the socket to force a resync
                    # is herdr's policy and is the better one, but it changes the wire
                    # contract, so it is written up as a proposal in the project's design log (not part of this release)
                    # (P-001) rather than applied without approval. What IS repaired is
                    # a lost terminal frame (F04, below).
                    log.warning(
                        "WS send queue dropped %d frame(s) for peer=%s seq=%d..%d "
                        "(capacity=%d): the client cannot detect this loss",
                        loss.count, self.peer_id,
                        loss.first_seq, loss.last_seq, self.queue.capacity,
                    )
                    # F04, operator decision 60 (2026-09-27): the drop stays silent on the
                    # wire and is COUNTED, here, by the frames this warning names
                    # (meshychat_ws_send_queue_dropped_total, apps/core/metrics.py).
                    metrics.WS_SEND_QUEUE_DROPPED.inc(loss.count)
                    if loss.terminal_call_ids or loss.terminals_unnamed:
                        self._log_lost_terminals(loss)
                    # A dropped skill_invoke is offered again at once (divergence 041 S5,
                    # design 4.6, critique F8): the bridge runs a loss pass over THIS peer's
                    # calls that no target frame has answered, at most once per interval
                    # (S5 review F1). A dropped TERMINAL frame is pushed to THIS socket again
                    # by the same pass, or by the periodic sweep when the pass is throttled
                    # (F04, operator decision 60, the project's design log (not part of this release) 048): its registration
                    # ended when it was queued, so nothing else would send it.
                    if self.call_bridge is not None:
                        self.call_bridge.request_reoffer(
                            self.peer_id, connection_id=self.connection_id,
                            terminal_call_ids=loss.terminal_call_ids)
                await self.send(text_data=json.dumps(frame))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("WS writer ending", exc_info=True)

    def _log_lost_terminals(self, loss) -> None:
        """F04: which calls' terminal frames the loss took, and what happens to them. At most
        eight call ids in the line; the count is the whole. Worded apart from the counted
        "WS send queue dropped" warning above, so a log count of that phrase stays one per
        burst."""
        ids = loss.terminal_call_ids
        shown = ", ".join(ids[:8]) + (", ..." if len(ids) > 8 else "")
        log.warning(
            "WS send queue loss took the terminal frame of %d call(s) for peer=%s (%s)%s; %s",
            len(ids), self.peer_id, shown,
            (f", and {loss.terminals_unnamed} more not named (past the queue's memory)"
             if loss.terminals_unnamed else ""),
            ("each named one is pushed again by the call bridge's loss pass"
             if self.call_bridge is not None else
             "no call bridge here: the caller recovers by GET /v1/calls/{id} or a subscribe"))

    async def close(self, code=None, reason=None) -> None:
        """Every close the server starts: a gate's refusal, the per-peer cap, the token's
        expiry, a revocation, the 1011 of a frame not built yet. The reader stops FIRST.
        Channels keeps dispatching what the client sent until the client's own close comes
        back, and a frame read in that window is not acted on (041 S4 review, finding 1: a
        skill_running sent after the 4401 expiry close, or after the 1011 close, moved the
        call).

        A close the SERVER refuses raises here (041 S6 review F1). daphne hands the code to
        autobahn's `sendClose`, which refuses anything but 1000 and 3000-4999, so
        CLOSE_UNIMPLEMENTED (1011) and CLOSE_TOO_MANY_CONNECTIONS (1013) raise through this
        await. The application then ends, daphne closes the socket with 1011 itself
        (`handle_exception`), and Channels never dispatches `disconnect`: the socket stayed in
        the ConnectionManager, its presence row and its frame connection stayed, and
        MAX_CONNS_PER_PEER such closes locked the peer out of the worker. So the teardown runs
        HERE, and the exception goes on: under daphne the raise is what closes the socket at
        all. The code the client reads is unchanged by this (1011 under daphne, for both);
        which codes the wire should carry is the operator's call (docs/DIVERGENCES.md, 041 S6
        review)."""
        self._closing = True
        try:
            await super().close(code=code, reason=reason)
        except Exception as exc:
            log.warning("WS close with code %s refused by the server (%s); the socket is torn "
                        "down here, since its disconnect will not run", code, exc)
            await self._teardown()
            raise

    def _revoked(self) -> bool:
        """The revocation flag (connection_manager.py), set BEFORE the close event fires.
        Read by the reader and the writer so neither acts between the revocation and the
        close."""
        return self.handle is not None and self.handle.closed

    async def _await_revocation(self, handle: ConnectionHandle) -> None:
        """ConnectionManager.drop_peer tears the socket down, closed
        with the typed code the drop recorded on the handle (STATUS B15)."""
        try:
            await handle.close.wait()
        except asyncio.CancelledError:
            raise
        log.info("WS reader: close signal received (revocation, code %s)", handle.close_code)
        if self._writer is not None:
            self._writer.cancel()
        await self.close(code=handle.close_code, reason=handle.close_reason or None)

    async def _force_close_at_expiry(self, seconds: float) -> None:
        """The credential lapses, the socket goes."""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise
        log.info("WS closing: JWT expired (peer=%s)", self.peer_id)
        await self.close(code=CLOSE_CREDENTIAL_EXPIRED)

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        if self._revoked() or self._closing:
            # A revoked socket dispatches nothing more, not even an
            # error frame; the close is already on its way. The same for any close the
            # server started (`close` above).
            return
        if self.handle is None:
            # Never registered: the per-peer cap refused it after the upgrade
            # and its close is on its way. It is not a socket, and it
            # dispatches nothing. It used to: with no handle its bound key read as None, so
            # a target frame ran without the live-key clause (041 S4 review, finding 1).
            return
        if bytes_data is not None:
            # Binary frames are unsupported.
            await self.queue.push(
                envelope.error(envelope.CODE_UNSUPPORTED, "binary frames not supported")
            )
            return
        if text_data is None:
            return
        if len(text_data) > MAX_MESSAGE_SIZE:
            # Channels has no transport-level size cap, so the 16 MiB limit is
            # enforced here rather than left unbounded.
            await self.queue.push(
                envelope.error(envelope.CODE_UNSUPPORTED, "message exceeds the 16 MiB cap")
            )
            return
        try:
            frame = envelope.parse_client_frame(json.loads(text_data))
        except (json.JSONDecodeError, ValueError) as exc:
            # Invalid JSON is answered with a parse error.
            await self.queue.push(envelope.error(envelope.CODE_PARSE, f"bad JSON: {exc}"))
            return
        if frame.type not in _UNCHARGED_FRAMES:
            # B12 (the project's design log (not part of this release) 003): the socket's own frames, in the peer's FRAMES
            # bucket, so a peer whose calls are spent still gets its pong. Refused with an
            # error frame; the socket stays open.
            try:
                peer_limit.admit(self.peer_id, transport="ws", bucket=peer_limit.FRAMES)
            except peer_limit.PeerRateLimited as exc:
                await self.queue.push(envelope.error(peer_limit.WS_RATE_LIMITED, exc.message))
                return

        ctx = FrameContext(
            manager=self.manager,
            presence=self.presence,
            peer_id=self.peer_id,
            connection_id=self.connection_id,
            queue=self.queue,
            bound_key=self.handle.bound_key,
            db=self.frames_db,
            registry=self.call_registry,
            call_slots=self.call_slots,
            spawn=self._spawn,
        )
        try:
            await dispatch(frame, ctx)
        except SkillFrameNotImplemented as exc:
            # NOT an ack. The frame is refused in the reply AND the socket is
            # closed, so a client cannot read this as a successful handoff.
            # Sent DIRECTLY rather than queued: the writer task is about to be
            # cancelled by the close, and a refusal that races its own teardown
            # would reach the client only sometimes - which is worse than never.
            await self.send(text_data=json.dumps(
                envelope.error(envelope.CODE_UNIMPLEMENTED, str(exc))
            ))
            await self.close(code=CLOSE_UNIMPLEMENTED)
        except ValueError as exc:
            await self.queue.push(envelope.error(envelope.CODE_PARSE, f"bad frame: {exc}"))

    def _spawn(self, coro) -> asyncio.Task:
        """A skill_call's dispatch (041 S6), tracked so `disconnect` cancels it: a dispatch
        cancelled there registers nothing for a socket that is gone (its pipeline, once begun,
        still finishes; apps/wsgw/caller_frames.py)."""
        task = asyncio.ensure_future(coro)
        self._skill_calls.add(task)
        task.add_done_callback(self._skill_calls.discard)
        return task

    async def disconnect(self, code: int) -> None:
        """The socket's teardown."""
        await self._teardown()

    async def _teardown(self) -> None:
        """Deregister BEFORE mark_offline, and flip presence
        offline only when this was the peer's LAST connection. Runs once: from `disconnect`,
        or from a close the server refused (`close`), whose `disconnect` never comes. The task
        running it is never cancelled by it (a close refused inside one of this socket's own
        tasks would otherwise end its own teardown at the next await)."""
        if self._torn_down:
            return
        self._torn_down = True
        current = asyncio.current_task()
        for task in (self._writer, self._expiry, self._revocation, self._redelivery,
                     *self._skill_calls):
            if task is not None and task is not current:
                task.cancel()
        if self._registered and self.peer_id is not None:
            last = await self.manager.deregister(self.peer_id, self.connection_id)
            self._registered = False
            if last:
                try:
                    await self.presence.mark_offline(self.peer_id, self.connection_id)
                except Exception:
                    log.exception("presence.mark_offline failed")
        # Every call registration of this socket (041 S6). After the
        # cancels above, so no dispatch of this socket registers again.
        await self.call_registry.unregister_connection(self.connection_id)
        # AFTER the deregister, so no later beat snapshots this socket; the bridge makes the
        # delete wait for a beat that already did, for at most 1 s, and past that hands the
        # delete to that beat (041 S3, review F1). Any socket that was ever registered, since
        # a beat can write a row between register and an early close.
        if self.call_bridge is not None and self.handle is not None:
            try:
                await self.call_bridge.detach(self.connection_id)
            except Exception:
                log.exception("call bridge: this socket's presence row was not deleted; "
                              "it reads offline within the online window")
        # The socket's own frame connection (041 S4). Channels dispatches `disconnect` only
        # after the last `receive` returned, so no target frame is running on it; the close
        # takes its lock anyway, off the loop.
        try:
            await asyncio.to_thread(self.frames_db.close)
        except Exception:
            log.exception("this socket's frame connection did not close cleanly")
