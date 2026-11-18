"""Every limit the hub enforces.

A cap must remain tied to its documented reason and contract.
"""
from __future__ import annotations

# --- connection limits -------------------------------------------------------

#: Each connection allocates a 256-slot SendQueue plus reader/writer tasks, so
#: the cap bounds the blast radius of one credential.
MAX_CONNS_PER_PEER = 8

#: The insert loop stops at the cap rather than erroring,
#: so a Subscribe frame carrying more is PARTIALLY applied and still acked.
MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN = 1000

#: Design 4.7 and 5.4: the most `call:` registrations one socket holds, the presence cap
#: reused (divergence 041 S6). Applied the way that cap is: the channels past it are dropped
#: and the ack still echoes them all.
MAX_CALL_SUBSCRIPTIONS_PER_CONN = MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN

#: Prefix for presence subscriptions.
PRESENCE_PREFIX = "presence:"

# --- send queue --------------------------------------------------------------

#: Every socket's queue is built with this capacity, so 256 is the
#: per-connection capacity in production.
SEND_QUEUE_CAPACITY = 256

# --- frame handling ----------------------------------------------------------

#: The per-socket cap on spawned skill_call dispatches (divergence 041 S6,
#: apps/wsgw/caller_frames.CallSlots); over it the frame is SKILL_CALL_BUSY without
#: blocking the reader. The number is part of the contract a client is written against
#: (it appears in the SKILL_CALL_BUSY message text).
MAX_INFLIGHT_SKILL_CALLS = 16

#: The process-wide cap on concurrent WS skill_call pipeline threads (design
#: 8.1 R1, proposal 32): each one holds a database connection of its own for the whole
#: pipeline, and there is no pool, so 16 per socket times 8 sockets per peer is 128 per peer
#: with nothing bounding the sum. Over it the frame is SKILL_CALL_BUSY, at once, as over the
#: per-socket cap (divergence 041 S6). The live max_connections read 500 (design 3.2).
MAX_CONCURRENT_WS_PIPELINE_CALLS = 32

# --- authentication ----------------------------------------------------------

#: Compatibility protocol name.
NEGOTIATED_PROTOCOL = "meshy.v1"

#: The JWT rides in Sec-WebSocket-Protocol.
JWT_PREFIX = "jwt."
#: The DPoP proof rides in the same header.
DPOP_PREFIX = "dpop."
#: TASK-28 AC#3 0RTT agent-card hash.
USED_PROTOCOL_HASH_PREFIX = "usedProtocolHash."

#: The WS upgrade's DPoP proof is bound to this pseudo-method.
DPOP_WS_METHOD = "WS_UPGRADE"

#: DPoP proof window in seconds.
DPOP_WINDOW_SECS = 120

#: 16 MiB inbound cap for a message and a frame.
MAX_MESSAGE_SIZE = 16 * 1024 * 1024

#: WebSocket endpoint path.
WS_PATH = "/v1/ws"

# --- presence ---------------------------------------------------------------

#: Presence time to live in seconds.
PRESENCE_TTL_SECS = 120

# --- live delivery: Postgres LISTEN/NOTIFY in place of Redis (divergence 020) ----

#: The channel sql/036_mesh_notify_trigger.sql's `mesh_chat_notify` publishes on.
#: The trigger and listeners use this one name; a second trigger would
#: double-notify every message.
MESH_NOTIFY_CHANNEL = "mesh_msg"

#: A mail row was REDACTED (divergence 053, sql_meshychat/027): the
#: trigger on `shared_memory_redaction` NOTIFYs here with the record's id, a POINTER as
#: `mesh_msg`'s payload is. The live bridge (apps/wsgw/mesh_notify.py) reads the record back
#: and pushes a `mesh_redaction` frame to the row's recipients and to the authors of its
#: replies; the process's wake subscriber (apps/mesh/wake.py) wakes every parked addressed
#: wait, which finds the event in the commit order. Redactions use their own
#: channel because a redaction is not a message.
REDACTION_NOTIFY_CHANNEL = "meshy_mesh_redaction"

#: A mail row was EDITED (divergence 059, sql_meshychat/029): the trigger on
#: `shared_memory_edit` NOTIFYs here with the record's id, a POINTER as the two channels above carry. The
#: live bridge (apps/wsgw/mesh_notify.py) reads the record and its row back and pushes a `mesh_edit` frame to
#: the row's recipients (each with the role that admitted it) and to the authors of its replies; the wake
#: subscriber (apps/mesh/wake.py) wakes every parked addressed wait, which finds the event in the commit order.
EDIT_NOTIFY_CHANNEL = "meshy_mesh_edit"

#: The beat route NOTIFYs here with the peer id alone and the bridge reads the
#: row back (divergence 020). A Postgres channel name is an identifier, so no colon.
PRESENCE_ROW_NOTIFY_CHANNEL = "meshy_presence_row"

#: Reconnect backoff for a dropped LISTEN connection: 500 ms, doubling, capped
#: at 30 s, with a 100 ms floor after a clean end.
LISTEN_BACKOFF_INITIAL_SECS = 0.5
LISTEN_BACKOFF_MAX_SECS = 30.0
LISTEN_CLEAN_RESTART_FLOOR_SECS = 0.1

# --- skill calls: the CallBridge (divergence 041 S3) --------------------------------------

#: Two triggers (sql_meshychat/018) NOTIFY a POINTER on a
#: committed status edge and on a committed chunk, and every serving worker's CallBridge
#: (apps/wsgw/call_notify.py) LISTENs. Postgres channel names are identifiers: no colon.
CALL_NOTIFY_CHANNEL = "meshy_call"
CALL_CHUNK_NOTIFY_CHANNEL = "meshy_call_chunk"

#: Design 4.6 and Q17 (critique F8): how often the CallBridge offers again a `routed` call
#: its target has not acknowledged. The revocation sweep's cadence.
CALL_REOFFER_SWEEP_SECS = 10.0

#: How long the CallBridge's listener blocks in one `notifies()` call before it re-checks its
#: stop flag and its timers. LiveBridge uses 1 s (`mesh_notify.NOTIFY_POLL_SECS`); this one is
#: shorter because its beat and re-offer timers are checked between calls, and because the
#: test suite stops it after every test that opened a socket and pays this latency each time.
CALL_NOTIFY_POLL_SECS = 0.25

# --- revocation: an open socket loses a standing it had (STATUS B15, divergence 030) ---

#: The routes that revoke NOTIFY here in the revoking transaction, and the live
#: bridge LISTENs. The payload
#: is a POINTER ({"peer_id", optional "namespace"/"kid"}): the bridge re-reads the peer's
#: standing before it drops anything, because NOTIFY is unauthenticated.
REVOCATION_NOTIFY_CHANNEL = "meshy_peer_revoked"

#: How often the bridge re-reads the standing of every locally connected peer.
#: It sweeps on a timer and again when a LISTEN reconnects, because nothing
#: writes `peers.status` - an operator suspends with SQL, which sends no
#: notification - and a disconnected LISTEN missed notifications. `settings.MESHY_WS_REVOCATION_SWEEP_SECS`
#: overrides it.
REVOCATION_SWEEP_SECS = 10.0
#: The floor on that interval, so a misconfigured 0 cannot spin the listener thread.
REVOCATION_SWEEP_MIN_SECS = 0.1

# --- close codes ------------------------------------------------------------
# GUESS, and flagged as one. An ASGI websocket consumer cannot return an HTTP
# status through Channels' test communicator. These private-use (4000-4999)
# close codes are this service's own choice. The NUMERIC VALUES ARE GUESSES:
# no client contract fixes them. The accept/refuse decision at each gate is a contract, not a guess.
CLOSE_UNAUTHORIZED = 4401          # HTTP 401
CLOSE_FORBIDDEN = 4403             # HTTP 403
CLOSE_UPGRADE_REQUIRED = 4426      # HTTP 426
CLOSE_INTERNAL_ERROR = 4500        # HTTP 500
#: A JWT expiry needs no particular close code; 4401 is chosen so a test can
#: observe it, and is also a GUESS.
CLOSE_CREDENTIAL_EXPIRED = 4401
#: An accepted socket receives TOO_MANY_CONNECTIONS and then closes. The frame is
#: the contract; the close code is unspecified, so 1013 ("try again later") is the choice.
#: UNDER DAPHNE this code and CLOSE_UNIMPLEMENTED below are never sent: autobahn's `sendClose`
#: accepts only 1000 and 3000-4999, so the consumer's close raises, the consumer tears the
#: socket down and lets it raise, and daphne closes with 1011 itself (041 S6 review F1). The
#: tests' communicator does not check codes. Whether the wire should carry a 4xxx code instead
#: is left to the operator: the values are contract values the tests pin.
CLOSE_TOO_MANY_CONNECTIONS = 1013
#: This service does not implement skill_resume (for good, design 2.10; skill_chunk
#: had no handler until divergence 041 S7),
#: and refuses it rather than acking. See frame_handlers.SkillFrameNotImplemented.
CLOSE_UNIMPLEMENTED = 1011
#: A LIVE socket closed because its peer lost a standing it held (STATUS B15). These
#: codes are one per cause so a client can tell "re-authenticate" from
#: "you were suspended", and they are GUESSES in the sense this block uses the word. 4403
#: is not reused: it already means "refused at the upgrade".
CLOSE_REVOKED_PEER_INACTIVE = 4410   # peers.status is no longer 'active'
CLOSE_REVOKED_GRANT = 4411           # the peer's read on shared/mesh-chat was revoked
CLOSE_REVOKED_KEY = 4412             # the key the token is bound to (`cnf`) was revoked
#: An upgrade carrying `Meshychat-Capability` while this server has no token verifier, closed
#: before accept by apps/core/capability_header.py `RefuseCapabilityUpgrade`, which reads it
#: from here (B07 slice 1, the project's design log (not part of this release) 040). The wrapper sits outside the
#: router, so no consumer runs. 4413 is RESERVED by the design
#: (docs/design/biscuit-capabilities.md 2.6) for a revoked or expired capability on an open
#: socket, and nothing sends it yet.
CLOSE_CAPABILITY_UNAVAILABLE = 4414
#: An upgrade carrying `Meshychat-Capability` while tokens are ON: v1 takes no token on the socket
#: (design 2.6, step 1), so it is E_CAPABILITY_NOT_ACCEPTED, closed before accept by the same
#: wrapper (B07 slice 4, the project's design log (not part of this release) 040). Distinct from 4414 so a client can tell
#: "this server has no tokens" from "this route takes none".
CLOSE_CAPABILITY_NOT_ACCEPTED = 4415
