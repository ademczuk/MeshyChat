"""A minimal node client for this server: `python -m tools.meshy_node`.

WHAT IT IS FOR. Every route test in this repository drives the server through Django's
test client, in-process, with keys the test itself made. None of them is a NODE: a separate
process that holds its own private key, redeems a pairing code over HTTP, signs its own
envelopes and DPoP proofs, and parks on a real WebSocket. This package is that node, kept
small enough to read in one sitting, so an end-to-end journey can be run against a live
server and every byte on the wire comes from code that does not import the server's.

WHAT IT DELIBERATELY DOES NOT IMPORT. Nothing under `apps/`. The Ed25519, RFC 8785 and DPoP
code in `wire.py` is written again from the wire contract rather than borrowed from
`apps/crypto/primitives.py`, so a journey that passes is two implementations agreeing
(docs/V1-DESIGN-RULES.md C5) rather than one implementation agreeing with itself. The DPoP
wire is pinned to the 178-character `KAT_DPOP` answer in `tests/test_crypto_parity.py` by
`tests/test_meshy_node.py`.

IT CANNOT REACH A SERVER BY ACCIDENT. `--base-url` is required on every subcommand, has no
default, and is never read from the environment. A state file records the server that
minted its key, and every later command refuses a `--base-url` that names a different one,
before any socket is opened: a key minted against a throwaway local server is never used to
sign for another. HTTP and WebSocket connections ignore `HTTP_PROXY` and friends, so the
URL given is the URL reached.

Subcommands (each prints one JSON object, except `listen`, which prints one JSON line per
`mesh_message` frame, and `offer`, which prints one JSON line per event):

    keygen   OPERATOR: a key generated HERE, its public half for manage.py bootstrap_owner
    bind     OPERATOR: that key bound to the signer peer bootstrap_owner created, proven by
             a sign-in before anything is written
    pair     OPERATOR: owner sign-in with that key, then POST /v1/auth/onetime: pairing codes
    claim    OPERATOR: namespace.claim through the signed invoke path (the bootstrap
             authority only, MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID)
    grant    OPERATOR: memory.grant through the signed invoke path (a namespace admin only)
    mint     redeem a pairing code: key generated HERE, POST /v1/onboard/peers, state file
    adopt    a node that ALREADY has an identity: this client's
             state file written from its existing key, only after a sign-in and the
             server's own key list prove it; the old file is read, never changed
    signin   POST /v1/auth/challenge + /v1/auth/sign-in with the node's key: a fresh JWT
    send     a mesh message v1 (apps/memoryskills/validation.py) through the signed invoke
             path to memory.write, DPoP-bound, then the call polled and its result
             signature checked against the server's JWKS; with --attach, each file first
             uploaded to /v1/files and verified, and cited in `attachments` (files.py,
             divergence 079)
    read     one mesh message through memory.read on the signed invoke path, the result
             signature checked against the server's JWKS: key, version, recorded_at,
             author, and the value's from, to, cc, thread_id, type, in_reply_to, requires_ack,
             done_when, attachments and body (requires_ack and done_when are the ask the
             sender signed)
    fetch    `read`, then every attachment downloaded and checked against the size and
             sha256 the message names, never replacing a file
    inbox    GET /v1/mesh/inbox; with --bodies, each hit read through `read`
    search   GET /v1/mesh/search
    context  GET /v1/mesh/context, with a DPoP proof: the anchor's body is memory.read's
             answer, given only to a request that proves it holds its key
    listen   GET /v1/ws, printing mesh_message frames as they arrive; with --read, each
             frame with the message it points at, read through `read`
    offer    PRIVATE bytes to one seat over the LAN (offer.py): one signed one-shot download,
             announced in one message; the bytes never reach the server
    pickup   fetch what an offer message offers, check its size and sha256, and save it under
             its name, never over an existing file

A FRAME AND AN INBOX HIT ARE POINTERS. The server's mesh_message frame carries key,
from_peer_id, `to`, created_at and this node's `role` (to, cc or broadcast); to a node
memory.read would show the message to, it also carries `cc` and `preview_unverified`, the
body's first 160 characters, NOT signature-checked (divergence 033). An inbox hit is an
addressed_to edge, with no body. Neither is the message (apps/wsgw/envelope.py
mesh_message: the client reads the row over its signed path). `read` is that path. `listen --read` and `inbox --bodies` use it per frame
and per hit, and a read that fails there is a typed `read_error`, never the end of the
command.

AN OBJECT-CAPABILITY TOKEN (docs/DIVERGENCES.md 040, slice 6) is presented through the library
only, `node.invoke`, `node.send` and `node.read` with `capability=`: no subcommand takes one,
because the design names no command line for it. The client sends `Meshychat-Capability` only to a
server whose /v1/health lists `object-capability-v1`, refuses a token over 8 KiB or not printable
ASCII before any socket, and signs its sha256 (`capability_sha256`) into the payload, never the
token.

The OPERATOR subcommands exist because a fresh install could not be brought to a chatting
mesh without them (docs/DIVERGENCES.md 025): bootstrap_owner's own next steps said "signed
by this key, call namespace.claim ... and memory.grant", and the only way to do that was to
hand-build a signed envelope and a DPoP proof. README.md "Fresh install" is the runbook that
uses them; tests/test_fresh_install_runbook.py runs it.
"""
