"""The MeshyChat WebSocket hub.

    constants.py           caps and windows
    send_queue.py          drop-oldest backpressure
    connection_manager.py  socket registry
    envelope.py            `type`-tagged wire frames
    didkey.py              did:key and Ed25519 handling
    auth.py                DPoP, 0RTT and Origin gates
    db.py                  upgrade database reads and writes
    frame_handlers.py      inbound frame handling
    consumer.py            socket lifecycle
    presence.py            in-process online/offline frames
    mesh_notify.py         Postgres LISTEN/NOTIFY delivery (divergence 020)
    revocation.py          standing changes close live sockets (divergence 030)
    hub_presence.py        cross-worker presence in `hub_connections` (divergence 041 S2)
    call_notify.py         CallBridge delivery, presence, redelivery and recovery (041 S3, S5, S6)
    target_frames.py       target call edges on the socket's own connection (divergence 041 S4)
    call_registry.py       caller-side call frame registrations (divergence 041 S6)
    caller_frames.py       caller frames and terminal mapping (divergence 041 S6)

Each module documents its own contract, including any explicitly marked guess.
"""
