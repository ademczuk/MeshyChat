"""`listen --monitor`: mesh messages as stdout lines for a Claude Code Monitor.

WHY. A Claude Code seat receives mesh messages through the Monitor tool, which runs a
command and turns EACH STDOUT LINE into a notification in the seat's session. A Monitor
expires after 30 minutes and is re-armed, so the gap between two runs is routine. Plain
`listen` prints frames but sends its status to stderr (a Monitor never sees it, so a
dropped socket is silent), exits on the first close, and cannot resume. This mode keeps
everything plain `listen` does out of its way: `cli.run` hands `--monitor` here and nothing
else in the package changes.

THE LINES. Every line is ONE JSON object on stdout, flushed. Nothing is written to stderr.

  {"event":"message","type":"mesh_message","key":..,"from_peer_id":..,"from":..,"to":[..],
   "cc":[..],"created_at":..,"role":"to"|"cc"|"broadcast","preview_unverified":..,
   "preview_truncated":..,"via":"live"|"catch_up"}
      one per message, exactly the push's frame (divergence 033; `from` is the sender's alias, present only for a peer the fleet roster names, divergence 063), and each message key at
      most once across restarts (below). `role` says why THIS node got it. `to` and `cc`
      are lists of labels, each absent when the row has none (a server before 033 sends
      `to` as JSON text; `_labels` reads it into the same list). `preview_unverified` is
      the body's first 160 characters, NOT signature-checked, with `preview_truncated`;
      both, and `cc`, are present only when the server found memory.read would show this
      node the message. `to_truncated`/`cc_truncated` appear when a legacy row named more than a
      frame carries. A server before 033 sends no `role` and no preview. When the message is a
      version an EDIT wrote (divergence 059) the frame says so with `version`, `edited_by` and
      `edited_at`, all three or none, and the line carries them: `from` stays the original
      sender, so without them a reader would credit that sender with words the editor wrote.
  {"event":"redaction","key":..,"version":V,"redacted_at":T,"reason_code":R,"via":"live"|"catch_up"}
      a message this node was sent has been REDACTED (divergence 053; docs/design/redaction.md):
      the server blanked its copies, and the copies outside the server, the postbox's spool
      line among them, are the seat's to blank. It names the row and says why, never what it
      said: the frame's `key` (the message line's), its integer `version`, `redacted_at` (the
      record's time, RFC 3339 with a Z) and `reason_code` (one of the server's closed list),
      the strings cut to 200 characters and scrubbed as a `frame` line's fields are, and
      NOTHING else of the frame. The server sends it to the message's recipients and to the
      authors of its replies, never to the seat that redacted. It travels as a message does:
      live, and from the commit order after the cursor (a monitor that was down gets it when
      it is armed again), once per (key, version) across restarts, remembered with the
      message keys after the line is out. Not a message: it counts toward neither `--count`
      nor `caught_up`'s count, and carries no `at` (`redacted_at` is its time). A catch-up
      prints it after the messages before it in the commit order, and one that arrives live
      DURING a catch-up is held until the catch-up is out, so it never prints before the
      message it withdraws. A frame that names no key or no integer version is one `frame`
      line, not dropped: a redaction this monitor cannot read is one the seat must hear of.
  {"event":"edit","key":..,"version":V,"edited_at":T,"edited_by":L,"role":R,"via":"live"|"catch_up"}
      a message this node was sent has been EDITED (divergence 059; docs/design/edit.md): a seat
      of the author's owner appended a new signed version, and whatever this node kept of the
      old text (the postbox's spool preview, a note) is out of date, and a request whose text
      changed after the seat acted on it is a silent divergence. It names the row and the
      version, never the text: the frame's `key` (the message line's), its integer `version`
      (the new one), `edited_at` (the record's time, RFC 3339 with a Z), `edited_by` (the
      editor's label; the message's `from` stays the original sender) and `role` (why THIS node
      is told: "to", "cc", "broadcast", or "replied" for a seat that wrote a direct reply and
      was not among the recipients), the strings cut to 200 characters and scrubbed as a
      `frame` line's fields are, and NOTHING else of the frame. It travels exactly as a
      redaction's line does: to the message's recipients and the authors of its replies, never
      to the seat that edited; live, and from the commit order after the cursor; once per
      (key, version) across restarts; not a message (it counts toward neither `--count` nor
      `caught_up`'s count, and has no `at`); held during a catch-up until the messages before it
      are out; and a frame that names no key or no integer version is one `frame` line. UNLIKE
      a redaction's line, a seat that receives it is to be WOKEN (the postbox does that).
  {"event":"frame","type":T,"key":..,"call_id_unverified":..,"from_peer_id":..,"unhandled":true,
   "at":AT}
      one per socket frame the monitor does not handle: a skill frame (divergence 041:
      skill_invoke, skill_chunk, a call's result, error or timeout) or a type no server
      sends today. It carries the frame's `type` (null when the frame is not JSON or has
      none) and, each only when the frame has it as a string, `key`, the frame's `call_id`
      as `call_id_unverified` (text the caller chose, not the server) and `from_peer_id`,
      cut to 200 characters and scrubbed like a defect's detail; never a payload, an
      envelope or arguments. A frame whose type, key and call id match one already printed
      is not printed again in this process, across reconnects too: the server re-offers an
      unanswered call every 10 s and on every connect until its deadline. --monitor watches and never executes a skill: it
      answers no frame, so a call to a node that runs only a monitor still runs to its
      deadline, and serving skills needs a separate handler (this client has none). Not a
      message: no `via`, and it counts toward neither `--count` nor the cursor. Its "at"
      is when the monitor printed it (client R6: a call's arrival had no time before). A
      frame is printed as it is read off the socket; one that arrives during a catch-up is
      read off before the catch-up's next page, so its "at" trails its arrival by at most
      one page's HTTP round trip (HTTP_TIMEOUT_S at worst), not by the whole catch-up
  {"event":"status","state":"connected","at":AT}      the socket is open and subscribed;
      printed with the session's first line (a message, a redaction, a frame, or `caught_up`),
      so a session whose catch-up fails before any of them costs only its `reconnecting` line
  {"event":"status","state":"caught_up","count":N,"at":AT}   the backlog since the last run
      is out
  {"event":"status","state":"closed","reason":..,"at":AT}    the open stream ended:
      "socket_closed" (with the close `code`), "error" (with the `error`), or on the way
      out "signal", "duration", "count"
  {"event":"status","state":"reconnecting","attempt":N,"delay_s":S,"after":KIND,"at":AT}
      one per attempt, KIND naming what failed ("socket_closed", "credential_expired",
      "http_503", "ConnectionRefusedError", ...); printed before the delay is waited
  {"event":"status","state":"auth_refreshed","why":..,"jwt_expires_at":..,"at":AT}
      the JWT was re-minted with the node's key: "expiring", "refused" (by the server, or
      a stored token the handshake cannot carry), "credential_expired" or "rotation"
  {"event":"status","state":"cursor_unsaved","error":E,"at":AT}  /  {..."cursor_saved",..}
      the sidecar could not be written (another process holds it, a full disk): the
      monitor carries on from memory and retries each second; a re-arm meanwhile may
      repeat what went out since the last save. Then: it was written again
  {"event":"status","state":"error","type":T,"terminal":true,"detail":..,"at":AT}
      the last line; the process exits non-zero (TERMINAL below). The detail never
      carries the node's JWT or seed, nor anything shaped like a JWT

"at" (AT above, on status and frame lines): the UTC time the line was EMITTED, RFC 3339 with
milliseconds and a Z, "2026-09-24T11:22:17.327Z". `Lines.put`, the one writer, reads the
wall clock as it builds the line and before it writes it; there is no queue between the
two, so a reader slow to drain stdout does not move it. It is the time of the LINE, not of
the event behind it: `connected` goes out with the session's first line, after at least
the first catch-up page, so the span from a `closed` line's "at" to the next `connected`
line's "at" is the whole reconnect (backoff, sign-in, upgrade, catch-up), and a
`reconnecting` line's "at" is when its delay began. It is the wall clock (`time.time`),
which NTP may step: a later line can carry an earlier "at", and stdout order is the order
of events. Message lines carry no "at": they are the push's frame, and their time is the
row's own `created_at` (divergence 031, amended 2026-09-24).

Status lines are for state CHANGES only. There is no heartbeat and no repeated identical
state, because every line costs the seat a turn. Idle, the monitor still works (a periodic
catch-up and the socket's own pings), silently.

RECONNECT. Any close or error is followed by a `reconnecting` line (a `closed` line first
when the stream had been announced `connected`), a jittered exponential backoff
(`backoff_delay`: attempt n waits between half and all of min(cap, initial * 2^(n-1));
defaults 1 s and 60 s) and a new session. A session that reached `caught_up` resets the
attempt count. The first connect waits up to OPEN_TIMEOUT_S (15 s) for the socket to open,
even when a session failed before it tried one (an anchor catch-up or sign-in at start);
reconnect attempt n waits RECONNECT_OPEN_TIMEOUT_S (4 s) * 2^(n-1), capped at 15 s, since a
firewall that drops packets to a closed port makes every attempt wait its whole timeout
while the server is down (divergence 031, amended 2026-09-25). The JWT is re-minted through
the client's own sign-in (`node.signin`) when the state says it expires within
`node.JWT_REFRESH_MARGIN_S`, when the server refuses it, when it holds a character the
handshake cannot carry, and when the server closes the socket because the token it was
opened with lapsed (4401) and that token is still the one held. A JWT about to lapse on an
open stream is ROTATED instead: a second socket is opened with a fresh token before the old
one closes, so a routine expiry costs one line (`auth_refreshed`), not five. Rotation is
keyed on the token the OPEN socket was opened with; a second socket the server refuses (the
peer at its socket cap) is retried every ROTATE_RETRY_S with the same fresh token, never a
new sign-in.

TERMINAL: the server's answer that retrying cannot change. Exit 2 when the client refused
before sending (as the rest of the CLI), 1 when the server answered. "The server answered"
means with its own TYPED error, {"error": CODE, ...}: every refusal this server sends has
that shape (apps/authapi/errors.py, apps/mesh/views.py), and a 4xx without it (a proxy's
page during a deploy) is transient.
  state_file          the state file is missing, unreadable, not a node's, for another
                      server, or names no peer (`node.load_state`, `node.require_peer`)
                                                                                    exit 2
  cursor_file         the sidecar is unreadable, holds a cursor that is not one, or
                      belongs to another peer/server                              exit 2
  key_revoked         sign-in answered a typed 4xx (not 429): unknown or suspended peer,
                      or a key the server no longer holds for it                    exit 1
  grant_gone          the catch-up answered 403 E_PERMISSION_DENIED /
                      E_NAMESPACE_UNCLAIMED: the node can no longer read the mesh    exit 1
  peer_inactive       the catch-up answered 403 E_FORBIDDEN: the peer itself          exit 1
  (any of the three)  the server closed the socket with a typed revocation (divergence
                      030): 4410 peer_inactive, 4411 grant_gone, 4412 key_revoked;
                      no `closed` and no reconnect first                           exit 1
  credential_refused  3 consecutive refusals (upgrade 401/403, catch-up 401) of a JWT
                      minted moments before: the server accepts the key but not its
                      tokens. A refused upgrade is a 403 whatever the cause (a close
                      before accept), so one refusal is not enough to call it   exit 1
  server_unsupported  the catch-up answered without `X-Meshy-Addressed` or a usable
                      `X-Meshy-Resume-Cursor`: the server lacks divergence 031 and would
                      have answered the whole mesh                                exit 1
  wait_refused        any other typed 4xx from the catch-up                       exit 1
Everything else (network errors, 5xx, 429, an untyped 4xx, a close, a missing welcome, a
sidecar that cannot be written) is transient.

RESUME. The cursor lives in a SIDECAR next to the state file (`<state file>.monitor`),
written as the state file is (`node._write_private`: atomic, owner-only) and fsynced before
it replaces the last one (`_write_sidecar`): the /v1/mesh/wait cursor, plus the last
RECENT_KEYS message keys emitted. The FIRST arm of a state file (no sidecar yet) takes its
resume point from the server before anything else: the newest mesh row the node can see, or
the beginning when it sees none. On EVERY connect, the socket is opened and subscribed
FIRST (its frames buffer), then the backlog is read from
GET /v1/mesh/wait?since=<cursor>&addressed=1&timeout=0, page by page from each answer's
scan position, until it answers 204, then the buffered frames are emitted, deduped against
the keys already out. The same catch-up runs every `--sync-every` seconds on an open
stream, and after every RECENT_KEYS // 4 lines, which advances the cursor and recovers any
push the server lost (divergence 020: the push is at most once).

The cursor SAVED is the server's `X-Meshy-Resume-Cursor`. The monitor stores it and sends
it back; it reads it only to tell the two kinds apart and to keep an older-kind cursor from
moving back. Since STATUS E13 the server answers a SEQ cursor, a place in its commit order
and the row at it (`seq:<n>:<micros>:<uuid>`), to a client that asks for one (`seq=1`,
which every catch-up page sends; a client that does not ask keeps the older cursor, review
finding 1 of E13), and its resume point is its scan position: a row's seq is drawn when its
writing transaction COMMITS, so no row can land behind a position already read, and a
catch-up re-reads nothing. A seq cursor is taken as the server answers it, a lower one
included: within one numbering the position never goes back, so a lower one means the
server renumbered (its 023 rolled back and applied again) and placed this node again from
the row the old cursor named (review finding 6). The first catch-up against such a server sends the old kind
once, which the server translates without skipping a row, and the store takes the seq
cursor it answers (a server rolled back past E13 answers the old kind again, and the store
takes that too). Against a server before E13 the resume point is the scan position held
ADDRESSED_SETTLE_SECS (10 s) behind the database clock: a row's `created_at` is its writing
transaction's START, so a write still open when a catch-up runs commits BEHIND rows that
catch-up passed; resuming from the scan position lost it (review F1). The next catch-up
re-reads those seconds and the recorded keys dedupe them; a write open longer than that
is caught up only if the push delivers it. A key is recorded AFTER its line is flushed,
and the cursor advances only past lines already out: a crash between the two re-emits at
most that line (at least once), and the recorded keys keep a restart from repeating
anything else (exactly once, as the reader sees it), as long as fewer than
RECENT_KEYS * 3 // 4 messages reach this node between two catch-ups (plus, before E13,
within ADDRESSED_SETTLE_SECS).

THE SET (divergence 031). The live frame goes to a peer for rows in shared/mesh-chat that
address it by `to` or `cc` or are broadcasts; the plain /v1/mesh/wait answers EVERY row the
peer can read in any namespace, and carries no `cc`. `addressed=1` makes the long-poll
answer the push's own set with the push's own frame, so live and caught-up lines are one
set, deduped by key. Each catch-up GET carries a DPoP proof for itself (divergence 033):
the server previews a body only to a reader memory.read would show it to, and with DPoP
enforced that includes proving the key, as the socket's upgrade does. Without the proof
the caught-up lines would carry no preview while the live ones did.

WHAT IT DOES NOT DO. It never executes a skill and never answers a frame: the `frame`
line above is all a skill_invoke gets. Two monitors on one state file each keep their own
cursor in the same sidecar (last writer wins) and each prints every message: run one per
state file. The body is not fetched; a line carries at most the server's 160-character
preview, and the key to read the message by.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import collections
import http.client
import json
import math
import os
import random
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import node

SIDECAR_SUFFIX = ".monitor"
SIDECAR_SCHEMA = "meshy-monitor-cursor/1"
#: How many emitted message keys the sidecar remembers. They must cover every row after the
#: saved cursor, since the next catch-up re-reads all of those: what went out since the last
#: catch-up (bounded by a catch-up after every RECENT_KEYS // 4 lines, `_sync_after`) plus
#: the settle window the resume point is held back by. Past it, the catch-up re-prints what
#: the window dropped, and each re-printed key evicts the next (review A1).
RECENT_KEYS = 1024
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 60.0
SYNC_EVERY_S = 30.0
WAIT_PATH = "/v1/mesh/wait"
ADDRESSED_HEADER = "X-Meshy-Addressed"
CURSOR_HEADER = "X-Meshy-Cursor"
RESUME_HEADER = "X-Meshy-Resume-Cursor"
#: The socket's open timeout (TCP connect, upgrade) for the process's FIRST connect, and for
#: a rotation (the old socket is still open, so nothing waits on it).
OPEN_TIMEOUT_S = 15.0
#: The open timeout of reconnect attempt 1, doubling per attempt up to OPEN_TIMEOUT_S. A
#: firewall that DROPS packets to a closed port (HQD's, while the server redeploys) makes a
#: connect wait out its whole timeout instead of being refused: at 15 s that was most of an
#: 18-21 s closed->connected gap (txxxx, vxxxxxxxxxx, 2026-09-25). The backoff already
#: spaces the attempts; the doubling gives a LAN that needs longer the full 15 s by
#: attempt 3.
RECONNECT_OPEN_TIMEOUT_S = 4.0
WELCOME_TIMEOUT_S = 10.0
HTTP_TIMEOUT_S = 20.0
#: The longest any blocking call waits before the loop looks at the stop flag again.
SLICE_S = 0.5
#: A JWT minted by this process less than this long ago counts as fresh.
FRESH_JWT_S = 60.0
CREDENTIAL_REFUSALS_TERMINAL = 3
#: The fields a `frame` line copies from a socket frame the monitor does not handle, each
#: only when it is a string, and each cut to FRAME_FIELD_MAX characters, as (the frame's
#: name, the line's name). Never anything else: a skill frame's envelope carries a caller's
#: arguments (divergence 041). A call_id is text the CALLER chose (apps/calls/repository.py
#: `validate_call_id` takes any string without a control character, spaces included), so
#: the line labels it `call_id_unverified`, as a message line's `preview_unverified`.
FRAME_FIELDS = (("key", "key"), ("call_id", "call_id_unverified"),
                ("from_peer_id", "from_peer_id"))
FRAME_FIELD_MAX = 200
#: The frame types printed in the order the server sent them: a message, the redaction that
#: withdraws it and the edit that replaces its text. A frame of any of these types that reaches the
#: socket during a catch-up is kept for the drain after it (`_frame_now`), so no redaction or edit
#: line goes out before the message it concerns.
ORDERED_FRAMES = ("mesh_message", "mesh_redaction", "mesh_edit")
#: How many (type, key, call_id) identities of printed `frame` lines the process remembers.
#: The server offers a routed call again every CALL_REOFFER_SWEEP_SECS (10 s) and on every
#: connect until its deadline, and a monitor never answers it: without this memory one call
#: cost the seat a line per offer (review 1 of client r5).
FRAME_SEEN_MAX = 1024

#: Divergence 030's typed revocation closes (apps/wsgw/revocation.py, constants.py): the
#: server has re-read the peer's standing from its tables and closed the socket for it.
#: Retrying cannot change that, so each ends the monitor with the type the HTTP path gives
#: for the same cause. Keyed on the server's numbers (tests pin the two together).
REVOKED_CLOSES = {4410: "peer_inactive", 4411: "grant_gone", 4412: "key_revoked"}
#: How long before the recorded expiry an open stream rotates its JWT.
ROTATE_MARGIN_S = float(node.JWT_REFRESH_MARGIN_S)
ROTATE_RETRY_S = 10.0
#: 403 codes that mean "cannot read the mesh" (apps/mesh/views.py _DENIED).
GRANT_CODES = frozenset({"E_PERMISSION_DENIED", "E_NAMESPACE_UNCLAIMED"})
#: The 403 code for the peer itself (suspended, or not active).
FORBIDDEN_CODE = "E_FORBIDDEN"
#: Pauses between the attempts of one sidecar save before it is reported unsaved, and how
#: often an unsaved sidecar is tried again while the stream is open.
SAVE_PAUSES_S = (0.05, 0.1, 0.2)
SAVE_RETRY_EVERY_S = 1.0
#: RFC 7230 `tchar`: what a WebSocket subprotocol token may hold (websockets' own check)
#: and, a fortiori, a bearer header. A stored JWT with anything else cannot be sent.
_SENDABLE = re.compile(r"[-!#$%&'*+.^_`|~0-9A-Za-z]+")
#: Anything shaped like a JWT (a base64url JSON header), for scrubbing a defect's text.
_JWT_LIKE = re.compile(r"eyJ[A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]*)*")
#: A /v1/mesh/wait cursor, decoded: `<created_at_micros>:<row uuid>` exactly as the server
#: encodes it (apps/mesh/wait.py `encode_cursor`), within the years 1 to 9999; or, since
#: STATUS E13, `seq:<n>:<micros>:<uuid>` (`seq:0` before any row), a place in the server's
#: commit order and the row at it (`encode_seq_cursor`), in the server decoder's grammar.
_SEQ_TEXT = re.compile(r"seq:(?:(0)|([1-9][0-9]{0,18}):(0|-?[1-9][0-9]{0,18}):"
                       r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_SEQ_MAX = 2 ** 63 - 1
_CURSOR_TEXT = re.compile(r"(-?[0-9]{1,18}):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                          r"[0-9a-f]{4}-[0-9a-f]{12})")
_CURSOR_B64 = re.compile(r"[A-Za-z0-9_-]+")
_MICROS_MIN, _MICROS_MAX = -62135596800000000, 253402300799999999


def _seconds(text: str) -> float:
    """A duration flag: positive and finite. 0 made every reconnect delay 0, a hot loop
    printing a line per attempt (review B4); nan and inf are no duration at all."""
    value = float(text)
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {text!r}")
    return value


class _MonitorOnly(argparse.Action):
    """Stores the value and records the flag, so plain `listen` refuses it instead of
    silently ignoring it (review I1; `cli.run` hands such a namespace to `run`, which
    refuses it without `--monitor`)."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.monitor_only = sorted({*(getattr(namespace, "monitor_only", None) or ()),
                                         option_string})


def add_arguments(listen) -> None:
    """The flags `cli.build_parser` adds to `listen`: one call, so the hook there is one
    line."""
    listen.add_argument(
        "--monitor", action="store_true",
        help="for a Claude Code Monitor: one JSON line per message, unhandled frame or "
             "state change on stdout only, reconnect with backoff, resume from a cursor "
             "kept next to the state file (<state-file>.monitor); never executes a skill; "
             "see tools/meshy_node/monitor.py")
    listen.add_argument(
        "--backoff-initial", type=_seconds, default=BACKOFF_INITIAL_S, metavar="S",
        action=_MonitorOnly,
        help=f"--monitor: first reconnect delay (default {BACKOFF_INITIAL_S:g} s); "
             "attempt n waits a random time between half and all of "
             "min(cap, initial * 2^(n-1))")
    listen.add_argument(
        "--backoff-cap", type=_seconds, default=BACKOFF_CAP_S, metavar="S",
        action=_MonitorOnly,
        help=f"--monitor: the reconnect delay never exceeds this (default {BACKOFF_CAP_S:g} s)")
    listen.add_argument(
        "--sync-every", type=_seconds, default=SYNC_EVERY_S, metavar="S",
        action=_MonitorOnly,
        help=f"--monitor: re-read the backlog this often on an open stream, silently "
             f"(default {SYNC_EVERY_S:g} s)")


def backoff_delay(attempt: int, initial: float, cap: float, rand=random.random) -> float:
    """Attempt n (1-based): a random point in [d/2, d], d = min(cap, initial * 2^(n-1))."""
    nominal = min(cap, initial * (2.0 ** min(max(attempt, 1) - 1, 60)))
    return nominal / 2 + (nominal / 2) * rand()


def sidecar_path(state_file) -> Path:
    return Path(str(state_file) + SIDECAR_SUFFIX)


def cursor_order(cursor) -> tuple[int, int, int] | None:
    """A /v1/mesh/wait cursor as (kind, order...), for `CursorStore.advance`. Kind 0: base64url
    without padding of `<created_at_micros>:<uuid>` (divergence 018 (c)), ordered as Postgres
    compares `(created_at, id)`: (0, micros, the uuid's 16 bytes big-endian as an integer).
    Kind 1, since STATUS E13: `seq:<n>:<micros>:<uuid>` (`seq:0` before any row), a place in
    the server's commit order and the row at it, (1, n, 0).
    None when it is not a cursor the server could have written, which the server itself
    would treat as NO cursor (review A3)."""
    if not isinstance(cursor, str) or not _CURSOR_B64.fullmatch(cursor):
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    except (binascii.Error, ValueError):
        return None
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != cursor:
        return None
    text = raw.decode("ascii", "replace")
    seq = _SEQ_TEXT.fullmatch(text)
    if seq is not None:
        if seq.group(1) is not None:
            return 1, 0, 0
        n, micros = int(seq.group(2)), int(seq.group(3))
        return None if n > _SEQ_MAX or not -_SEQ_MAX - 1 <= micros <= _SEQ_MAX else (1, n, 0)
    match = _CURSOR_TEXT.fullmatch(text)
    if match is None:
        return None
    micros = int(match.group(1))
    if not _MICROS_MIN <= micros <= _MICROS_MAX:
        return None
    return 0, micros, int(match.group(2).replace("-", ""), 16)


def _scrub(text: str, *states) -> str:
    """`text` without the node's secrets (JWT, seed, refresh token, of every state given)
    or anything shaped like a JWT: a defect's message is printed into the seat's session
    (review C2)."""
    for state in states:
        for field in ("jwt", "signing_seed_b64url", "refresh_token"):
            secret = state.get(field) if isinstance(state, dict) else None
            if isinstance(secret, str) and len(secret) >= 8:
                text = text.replace(secret, "<redacted>")
    return _JWT_LIKE.sub("<redacted>", text)


# --------------------------------------------------------------------------- stdout

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: The lines `Lines.put` stamps with their emit time. `frame` since client R6 (nodes uxxx and
#: bxxxxx, 2026-09-26): a frame line with no time could not say when a call reached the node.
#: Never `message`: a message line is the push's frame, timed by its own `created_at`.
STAMPED_EVENTS = frozenset({"status", "frame"})


def utc_stamp(seconds: float) -> str:
    """Epoch seconds as RFC 3339 UTC with milliseconds and a Z: "2026-09-24T11:22:17.327Z".
    By arithmetic from the epoch, not `datetime.fromtimestamp`, which raises OSError on
    Windows for a time before 1970: a wall clock stepped that far back still stamps."""
    at = _EPOCH + timedelta(seconds=seconds)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Lines:
    """The only writer. False when stdout is gone (the Monitor stopped reading).

    Every status and frame line gets "at", the wall clock (`clock`, `time.time` unless a
    test gives another) read as the line is built, before it is written: the EMIT time, not
    the time the reader drained it. Message lines are written as given: they are the push's
    frame, and their time is the row's `created_at`."""

    def __init__(self, stream, clock=None):
        self.stream = stream
        self.clock = time.time if clock is None else clock
        self.broken = False

    def put(self, obj: dict) -> bool:
        if self.broken:
            return False
        if obj.get("event") in STAMPED_EVENTS:
            obj = {**obj, "at": utc_stamp(self.clock())}
        try:
            self.stream.write(json.dumps(obj) + "\n")
            self.stream.flush()
            return True
        except (OSError, ValueError):
            self.broken = True
            self._detach()
            return False

    def _detach(self) -> None:
        """The reader is gone. Point the stream's descriptor at the null device, so the
        interpreter's own flush at exit (of the line this write left buffered) succeeds,
        rather than printing "Exception ignored" on stderr and exiting 120 (review F4)."""
        try:
            fd = self.stream.fileno()
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, fd)
            finally:
                os.close(null)
        except (OSError, ValueError, AttributeError):
            pass


# --------------------------------------------------------------------------- the sidecar

class CursorFileError(Exception):
    pass


def _write_sidecar(path: Path, data: dict) -> None:
    """`node._write_private`'s steps (a temp file in the same directory, owner-only, an
    atomic replace) with the temp file fsynced BEFORE the replace. The sidecar is rewritten
    on every line; without the fsync a power loss after the rename can leave it empty (the
    rename is journaled, the data need not be), a `cursor_file` refusal and a lost gap
    (review A2)."""
    node._write_private(path, data, fsync=True)


class CursorStore:
    """The resume point and the recent keys, persisted on every change. A save that fails
    (the file held by another process, a full disk) is not an error: the state stays in
    memory, `unsaved` names what went wrong, and the next save tries again (review F2)."""

    def __init__(self, path: Path, base_url: str, peer_id: str):
        self.path, self.base_url, self.peer_id = Path(path), base_url, peer_id
        self.cursor: str | None = None
        self._keys: collections.deque = collections.deque()
        self._set: set = set()
        self.unsaved: str | None = None

    @classmethod
    def load(cls, path: Path, base_url: str, peer_id: str) -> "CursorStore":
        store = cls(path, base_url, peer_id)
        if not store.path.exists():
            return store
        try:
            data = json.loads(store.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CursorFileError(f"{store.path} is unreadable: {exc}")
        if not isinstance(data, dict) or data.get("schema") != SIDECAR_SCHEMA:
            raise CursorFileError(f"{store.path} is not a {SIDECAR_SCHEMA} file")
        if data.get("base_url") != base_url or data.get("peer_id") != peer_id:
            raise CursorFileError(
                f"{store.path} is the cursor of peer {data.get('peer_id')!r} on "
                f"{data.get('base_url')!r}, not of this state file's peer {peer_id!r} on "
                f"{base_url!r}; remove it to start from now")
        cursor = data.get("cursor")
        if cursor is not None and cursor_order(cursor) is None:
            raise CursorFileError(
                f"{store.path} holds {str(cursor)[:80]!r}, which is not a /v1/mesh/wait "
                "cursor (the server would start from its newest row and skip the backlog); "
                "remove it to start from now")
        store.cursor = cursor
        for key in data.get("recent_keys") or []:
            if isinstance(key, str) and key not in store._set:
                store._keys.append(key)
                store._set.add(key)
        store._trim()
        return store

    def _trim(self) -> None:
        while len(self._keys) > RECENT_KEYS:
            self._set.discard(self._keys.popleft())

    def seen(self, key: str) -> bool:
        return key in self._set

    def remember(self, key: str) -> None:
        if key in self._set:
            return
        self._keys.append(key)
        self._set.add(key)
        self._trim()
        self.save()

    def advance(self, cursor) -> None:
        """Move the resume point to `cursor`, never to a non-cursor. A legacy cursor only
        FORWARD over a legacy one: that kind's resume point is held behind the server's
        clock and can fall behind one already saved. A seq cursor as the server answers it:
        its scan position never goes back within one numbering, so a lower one means the
        numbering changed (023 rolled back and applied again) and the server placed this
        node again from the row its old cursor named (review finding 6 of E13). A cursor of
        the OTHER kind is taken as it comes: the server changed what it answers (to the
        commit order at STATUS E13, or back from it after a rollback), and it can read only
        the kind it now answers."""
        new = cursor_order(cursor)
        if new is None or cursor == self.cursor:
            return
        old = cursor_order(self.cursor)
        if old is not None and old[0] == new[0] == 0 and new <= old:
            return
        self.cursor = cursor
        self.save()

    def save(self, pauses=SAVE_PAUSES_S) -> bool:
        data = {"schema": SIDECAR_SCHEMA, "base_url": self.base_url,
                "peer_id": self.peer_id, "cursor": self.cursor,
                "recent_keys": list(self._keys)}
        for pause in (*pauses, None):
            try:
                _write_sidecar(self.path, data)
            except OSError as exc:
                self.unsaved = type(exc).__name__
                if pause is None:
                    return False
                time.sleep(pause)
            else:
                self.unsaved = None
                return True
        return False


# --------------------------------------------------------------------------- control flow

class _Transient(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class _Terminal(Exception):
    def __init__(self, type_: str, detail: str, code: int):
        super().__init__(detail)
        self.type, self.detail, self.code = type_, detail, code


def _revoked(exc) -> None:
    """Raise the terminal a typed revocation close means (REVOKED_CLOSES); any other close
    returns, and the caller treats it as before."""
    code = exc.rcvd.code if exc.rcvd else None
    if code in REVOKED_CLOSES:
        reason = (exc.rcvd.reason or "").strip()
        raise _Terminal(REVOKED_CLOSES[code],
                        f"the server closed the socket with {code}"
                        + (f": {reason}" if reason else "") + " (divergence 030)", 1)


class _Refused(Exception):
    """The upgrade was refused (HTTP 401/403): an expired token and a suspended peer look
    the same here, so the caller decides by whether the token was fresh. The argument is
    the `reconnecting` kind."""


def _split_text(text: str) -> list[str]:
    """One recipient field as TEXT, parsed as the node clients parse it: the string branch
    of reactor.py `_to_labels` (a leading `[` is a JSON array held in a string, falling
    back to a quote-stripping comma split when it does not parse, as when sql/036 cut it at
    256 characters; anything else is a comma list), with its case kept."""
    s = text.strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [x.strip() for x in arr if isinstance(x, str) and x.strip()]
        except (ValueError, TypeError):
            inner = s.strip("[]")
            return [t for t in (t.strip().strip("\"'") for t in inner.split(",")) if t]
    return [p for p in (p.strip() for p in s.split(",")) if p]


def _labels(value) -> list[str]:
    """A frame's `to` or `cc` as a list of labels (divergence 033). A server with 033 sends
    a list, kept as it is. One before it sends `to` as sql/036's `value ->> 'to'` text,
    read here into the same list, for one release (`_split_text`); anything else is []."""
    if isinstance(value, list):
        return [x for x in value if isinstance(x, str)]
    if isinstance(value, str):
        return _split_text(value)
    return []


def _server_code(body) -> str | None:
    """The server's own error code, when `body` is its typed error {"error": CODE, ...}.
    None for anything else: a 4xx without it is not this server refusing (review B2)."""
    if isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"]:
        return body["error"]
    return None


def _sync_after() -> int:
    """How many lines may go out before a catch-up runs to move the cursor past them.
    A function, so a test that lowers RECENT_KEYS in the child lowers this with it."""
    return max(1, RECENT_KEYS // 4)


class _Stop(Exception):
    pass


def _parse_frame(raw):
    """`raw` as the JSON it carries, or None when the monitor cannot read it: not text, not
    JSON, or nested deeper than the interpreter parses (RecursionError, neither a TypeError
    nor a ValueError, which ended the monitor with a terminal error before client r5:
    review 5). `_frame`, `_frame_now` and `_open` each carried this guard of their own."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None


# --------------------------------------------------------------------------- the monitor

class Monitor:
    def __init__(self, path: Path, state: dict, store: CursorStore, lines: Lines, *,
                 backoff_initial: float = BACKOFF_INITIAL_S,
                 backoff_cap: float = BACKOFF_CAP_S, sync_every: float = SYNC_EVERY_S,
                 count: int | None = None, duration: float | None = None):
        self.path, self.state, self.store, self.lines = Path(path), state, store, lines
        self.base_url = state["base_url"]
        self.backoff_initial = max(0.0, backoff_initial)
        self.backoff_cap = max(self.backoff_initial, backoff_cap)
        self.sync_every = max(0.5, sync_every)
        self.count = count
        self.deadline = None if duration is None else time.monotonic() + duration
        self.stop_event = threading.Event()
        self.stop_reason: str | None = None
        self.stop_signal: str | None = None
        self.ws = None
        self.open = False
        self.healthy = False
        #: The reconnect attempt the current session is (0: the process's first connect).
        self.attempt = 0
        #: Whether this process has tried to open a socket yet. The first try keeps
        #: OPEN_TIMEOUT_S even when a session failed before it (review 6 of client r5).
        self.socket_tried = False
        self.emitted = 0
        self.minted_at: float | None = None
        self.force_refresh: str | None = None
        self.refusals = 0
        self.rotate_after = 0.0
        self._early: list[dict] = []
        #: The identities of the frame lines already printed (FRAME_SEEN_MAX, oldest first).
        self._frames_seen: collections.deque = collections.deque()
        self._frames_seen_set: set = set()
        #: The token the OPEN socket was opened with, and its recorded expiry: rotation and
        #: a 4401 are about THAT token, not whatever the state holds by then (review F3).
        self.ws_jwt: str | None = None
        self.ws_expires: str | None = None
        #: Lines out since the last completed catch-up (`_sync_after`, review A1).
        self.unsynced = 0
        #: Whether the reader has been told the sidecar is unsaved, and when to try again.
        self.unsaved_said = False
        self.save_retry_at = 0.0

    # ---- stopping
    def request_stop(self, reason: str, signame: str | None = None) -> None:
        if self.stop_reason is None:
            self.stop_reason, self.stop_signal = reason, signame
        self.stop_event.set()

    def _check_stop(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.request_stop("duration")
        if self.stop_event.is_set():
            raise _Stop()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self._check_stop()
            left = end - time.monotonic()
            if left <= 0:
                return
            self.stop_event.wait(min(SLICE_S, left))

    # ---- lines
    def _put(self, line: dict) -> None:
        """`line` out, or the end of the monitor: `Lines.put` answers False once the reader
        has gone (stdout closed), and nothing more is worth a line. `_status`, `_message`,
        `_event` and `_unhandled` each stopped this way of their own before."""
        if not self.lines.put(line):
            self.request_stop("stdout_closed")
            raise _Stop()

    def _remember(self, ident: str) -> None:
        """Keep `ident` (a message's key, an event's `<event>:<key>:<version>`) in the cursor
        file AFTER its line is out: at least once, so one the reader never got is printed by
        the next run. The line counts toward the next catch-up (`_sync_after`, review A1)."""
        self.store.remember(ident)          # AFTER the line is out: at least once
        self.unsynced += 1
        self._note_saving()

    def _status(self, state: str, **fields) -> None:
        self._put({"event": "status", "state": state, **fields})

    def _message(self, frame, via: str) -> int:
        if not isinstance(frame, dict) or frame.get("type") != "mesh_message":
            return 0
        key = frame.get("key")
        if not isinstance(key, str) or not key or self.store.seen(key):
            return 0
        body = {k: v for k, v in frame.items() if k not in ("event", "via")}
        for field in ("to", "cc"):
            if field in body:
                body[field] = _labels(body[field])
        self._announce()
        self._put({"event": "message", **body, "via": via})
        self._remember(key)
        self.emitted += 1
        if self.count is not None and self.emitted >= self.count:
            self.request_stop("count")
        return 1

    def _redaction(self, frame, via: str) -> int:
        """`{"event":"redaction","key":K,"version":V,"redacted_at":T,"reason_code":R,"via":..}`
        for a `mesh_redaction` frame, once per (key, version): the id is kept in the cursor
        file's recent keys AFTER the line is out (at least once, as a message's key is), so a
        re-arm and a live frame that the catch-up already returned print it once. Only the four
        fields, each a string cut and scrubbed as a `frame` line's are (the version an int):
        the line is never the frame. A frame that names no key, or no integer version, is one
        `frame` line (`_unhandled`): a redaction this monitor cannot read is one the seat has to
        hear about. Not a message: it does not count toward `--count` or `caught_up`, and moves
        no cursor (the wait's own position does). Returns 0, the messages it emitted."""
        return self._event(frame, via, "redaction", ("redacted_at", "reason_code"))

    def _edit(self, frame, via: str) -> int:
        """`{"event":"edit","key":K,"version":V,"edited_at":T,"edited_by":L,"role":R,"via":..}` for a
        `mesh_edit` frame (divergence 059), once per (key, version), by the rules of `_redaction`: the
        id `edit:<key>:<version>` is kept with the message keys AFTER the line is out; only the five
        fields, each a string cut and scrubbed as a `frame` line's are (the version an int); a frame
        that names no key or no integer version is one `frame` line; not a message, so it does not count
        toward `--count` or `caught_up`. Returns 0, the messages it emitted."""
        return self._event(frame, via, "edit", ("edited_at", "edited_by", "role"))

    def _event(self, frame, via: str, event: str, names: tuple) -> int:
        """The line of an event that names a row and a version (a redaction, an edit): once per
        (event, key, version). `names` are the frame's other string fields, in the order they are printed.
        Returns 0: an event is never a message."""
        key, version = frame.get("key"), frame.get("version")
        if (not isinstance(key, str) or not key
                or isinstance(version, bool) or not isinstance(version, int)):
            self._unhandled(frame)
            return 0
        seen = f"{event}:{key}:{version}"
        if self.store.seen(seen):
            return 0
        line: dict = {"event": event, "key": self._frame_field(key), "version": version}
        for name in names:
            value = self._frame_field(frame.get(name))
            if value is not None:
                line[name] = value
        line["via"] = via
        self._announce()
        self._put(line)
        self._remember(seen)
        return 0

    def _frame(self, raw) -> int:
        """One socket frame: a mesh_message is a message line (`_message`), a mesh_redaction a
        redaction line (`_redaction`), a mesh_edit an edit line (`_edit`); ANY other frame,
        a type no server sends today, one that is not JSON, and one nested too deep to
        parse included, is one `frame` line (`_unhandled`), except a repeat of a line
        already printed (`_first_frame_line`). Before
        client r5 every other frame vanished, so a skill call pushed at a monitor's socket
        (divergence 041) ran to its deadline unseen (txxxx, vxxxxxxxxxx, 2026-09-25)."""
        frame = _parse_frame(raw)
        if isinstance(frame, dict) and frame.get("type") == "mesh_message":
            return self._message(frame, "live")
        if isinstance(frame, dict) and frame.get("type") == "mesh_redaction":
            return self._redaction(frame, "live")
        if isinstance(frame, dict) and frame.get("type") == "mesh_edit":
            return self._edit(frame, "live")
        self._unhandled(frame)
        return 0

    def _unhandled(self, frame) -> None:
        """`{"event":"frame","type":T,..,"unhandled":true,"at":AT}`: the frame's type (null when it
        has none, or is not JSON) and its FRAME_FIELDS, never its payload, envelope or
        arguments, scrubbed of the node's secrets and anything shaped like a JWT. The
        monitor answers nothing and executes nothing: it watches. Not a message: it does
        not count toward `--count` and moves no cursor."""
        fields = frame if isinstance(frame, dict) else {}
        line: dict = {"event": "frame", "type": self._frame_field(fields.get("type"))}
        for name, label in FRAME_FIELDS:
            value = self._frame_field(fields.get(name))
            if value is not None:
                line[label] = value
        line["unhandled"] = True
        if not self._first_frame_line(line):
            return
        self._announce()
        self._put(line)

    def _first_frame_line(self, line: dict) -> bool:
        """Whether `line` is the first with its identity (type, key, call id) in this
        process; a line with neither a key nor a call id has none, and always prints. The
        server re-offers a routed call every 10 s and on every connect (call_notify.py
        `_REOFFER`), and each repeat would cost the seat a turn."""
        if line.get("key") is None and line.get("call_id_unverified") is None:
            return True
        ident = (line["type"], line.get("key"), line.get("call_id_unverified"))
        if ident in self._frames_seen_set:
            return False
        self._frames_seen.append(ident)
        self._frames_seen_set.add(ident)
        while len(self._frames_seen) > FRAME_SEEN_MAX:
            self._frames_seen_set.discard(self._frames_seen.popleft())
        return True

    def _frame_field(self, value) -> str | None:
        if not isinstance(value, str):
            return None
        return _scrub(value, self.state)[:FRAME_FIELD_MAX]

    def _announce(self) -> None:
        """`connected`, once per session, with the session's first line (a message, a
        `frame` line, or `caught_up`). A session whose catch-up fails before any of them
        (the waiter permits all taken, a 429, a bad body) would otherwise cost
        `connected`, `closed` and `reconnecting` per attempt, where a full outage costs
        one line (review B3)."""
        if not self.open:
            self.open = True
            self._status("connected")

    def _advance(self, cursor) -> None:
        self.store.advance(cursor)
        self._note_saving()

    def _note_saving(self) -> None:
        """One line when the sidecar stops being saved, one when it is saved again: state
        changes, never a line per failed attempt (review F2)."""
        if self.store.unsaved and not self.unsaved_said:
            self.unsaved_said = True
            self.save_retry_at = time.monotonic() + SAVE_RETRY_EVERY_S
            self._status("cursor_unsaved", error=self.store.unsaved)
        elif not self.store.unsaved and self.unsaved_said:
            self.unsaved_said = False
            self._status("cursor_saved")

    # ---- the run
    def run(self) -> int:
        self.attempt = 0
        while True:
            try:
                self._session()
            except _Stop:
                return self._finish()
            except _Terminal as t:
                self._close_ws()
                self.lines.put({"event": "status", "state": "error", "type": t.type,
                                "terminal": True, "detail": t.detail})
                return t.code
            except _Transient as t:
                self._close_ws()
                if self.healthy:
                    self.attempt, self.healthy = 0, False
                self.attempt += 1
                delay = backoff_delay(self.attempt, self.backoff_initial, self.backoff_cap)
                try:
                    self._check_stop()
                    self._status("reconnecting", attempt=self.attempt, delay_s=round(delay, 3),
                                 after=t.kind)
                    self._sleep(delay)
                except _Stop:
                    return self._finish()

    def _finish(self) -> int:
        if self.open:
            fields = {"reason": self.stop_reason or "signal"}
            if self.stop_signal:
                fields["signal"] = self.stop_signal
            self.lines.put({"event": "status", "state": "closed", **fields})
        self.open = False
        self._close_ws()
        self.store.save()
        return 0

    def _close_ws(self, ws=None) -> None:
        if ws is None:
            ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if ws is self.ws:
            self.ws = None

    def _dropped(self, kind: str, **fields):
        """The open stream ended: one `closed` line, then a transient failure."""
        if self.open:
            self.open = False
            self._status("closed", **fields)
        raise _Transient(kind)

    def _session(self) -> None:
        self.healthy = False
        if self.store.cursor is None:
            # The FIRST arm of this state file: take "now" from the server BEFORE the
            # socket is set up, so a message sent while it is being set up is after it.
            why = self._mint_reason()
            if why:
                self._refresh(why)
            self._catch_up(anchor=True)
        self.ws = self._connect()
        try:
            caught = self._catch_up()
            caught += self._drain(self.ws)
        except _Transient as t:
            self._dropped(t.kind, reason="error", error=t.kind)
        self._announce()
        self._status("caught_up", count=caught)
        self.healthy = True
        self.refusals = 0
        self._live()

    # ---- auth
    def _refresh(self, why: str) -> None:
        try:
            state = node.load_state(self.path, self.base_url)
            node.require_peer(self.path, state)
        except node.Refused as exc:
            raise _Terminal("state_file", str(exc), 2)
        try:
            node.signin(self.path, state)
        except node.ServerSaid as exc:
            if 400 <= exc.status < 500 and exc.status != 429 and _server_code(exc.body):
                raise _Terminal("key_revoked", f"sign-in answered {exc.status}: "
                                f"{json.dumps(exc.body)[:300]}", 1)
            raise _Transient(f"signin_http_{exc.status}")
        except (OSError, http.client.HTTPException) as exc:
            raise _Transient(type(exc).__name__)
        self.state = state
        self.minted_at = time.monotonic()
        self.force_refresh = None
        self._status("auth_refreshed", why=why, jwt_expires_at=state.get("jwt_expires_at"))

    def _fresh(self) -> bool:
        return self.minted_at is not None and time.monotonic() - self.minted_at < FRESH_JWT_S

    def _mint_reason(self) -> str | None:
        """Why the state's JWT must be minted again before it is used, or None. A stored
        token with a character the handshake cannot carry is as good as refused: sent, it
        made websockets raise with the token in the message (review C2)."""
        if self.force_refresh:
            return self.force_refresh
        if node._expires_soon(self.state):
            return "expiring"
        if not _SENDABLE.fullmatch(str(self.state.get("jwt") or "")):
            return "refused"
        return None

    def _refused(self, kind: str) -> None:
        self.refusals += 1
        if self.refusals >= CREDENTIAL_REFUSALS_TERMINAL:
            raise _Terminal("credential_refused",
                            f"{self.refusals} consecutive refusals of a freshly minted JWT "
                            f"(last: {kind})", 1)
        raise _Transient(kind)

    def _connect(self):
        refreshed = False
        while True:
            self._check_stop()
            why = self._mint_reason()
            if why:
                self._refresh(why)
                refreshed = True
            jwt, expires = self.state["jwt"], self.state.get("jwt_expires_at")
            timeout = self._open_timeout()
            self.socket_tried = True
            try:
                ws = self._open(jwt, timeout)
            except _Refused as exc:
                if not refreshed and not self._fresh():
                    self.force_refresh = "refused"
                    continue
                self._refused(exc.args[0])
            else:
                self.ws_jwt, self.ws_expires = jwt, expires
                return ws

    def _open_timeout(self) -> float:
        """OPEN_TIMEOUT_S for the process's first connect; for reconnect attempt n,
        RECONNECT_OPEN_TIMEOUT_S * 2^(n-1), never more than OPEN_TIMEOUT_S. The process's
        first socket open is keyed on "no socket tried yet", not on attempt 0: a first arm
        whose anchor catch-up or sign-in failed (a 503 at start) has counted attempt 1
        before any socket was tried, and got 4 s (review 6 of client r5)."""
        if self.attempt <= 0 or not self.socket_tried:
            return OPEN_TIMEOUT_S
        return min(OPEN_TIMEOUT_S,
                   RECONNECT_OPEN_TIMEOUT_S * (2.0 ** min(self.attempt - 1, 16)))

    def _open(self, jwt: str, open_timeout: float | None = None):
        """GET /v1/ws, the credentials in Sec-WebSocket-Protocol as plain `listen` sends
        them, then the welcome. Frames after the welcome buffer in the connection (no
        queue limit) until they are read. No frame size limit either: websockets' own
        default (1 MiB) closed the socket (1009) on a skill_invoke carrying a caller's
        args, which the server accepts up to 16 MiB (apps/wsgw/constants.py
        MAX_MESSAGE_SIZE) and redelivers on every connect, so the monitor looped
        closed/reconnecting until the call's deadline and never printed its frame line
        (review 3 of client r5). The monitor keeps four short fields of such a frame. `open_timeout` bounds the TCP connect and the
        upgrade (OPEN_TIMEOUT_S when not given, as for a rotation)."""
        from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
        from websockets.sync.client import connect

        uri, subprotocols = node._ws_endpoint(self.state, jwt)
        try:
            ws = connect(uri, subprotocols=subprotocols,
                         proxy=None, compression=None,
                         open_timeout=OPEN_TIMEOUT_S if open_timeout is None else open_timeout,
                         close_timeout=2, max_queue=None, max_size=None)
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise _Refused(f"upgrade_http_{status}")
            raise _Transient(f"upgrade_http_{status}")
        except (InvalidHandshake, OSError, TimeoutError) as exc:
            raise _Transient(type(exc).__name__)
        deadline = time.monotonic() + WELCOME_TIMEOUT_S
        early: list = []
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _Transient("no_welcome")
                try:
                    raw = ws.recv(timeout=min(SLICE_S, left))
                except TimeoutError:
                    self._check_stop()
                    continue
                except ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd else None
                    _revoked(exc)
                    raise _Transient(f"closed_before_welcome_{code}")
                # Not JSON (`_parse_frame` answers None, so no `kind`): not the welcome, and
                # `_drain` prints it as a frame line in its turn, as it would after the
                # welcome (review 4 of client r5).
                frame = _parse_frame(raw)
                kind = frame.get("type") if isinstance(frame, dict) else None
                if kind == "welcome":
                    self._early = early
                    return ws
                if kind == "error":
                    raise _Transient(f"ws_error_{frame.get('code')}")
                early.append(raw)
        except BaseException:
            self._close_ws(ws)
            raise

    # ---- catching up
    def _catch_up(self, anchor: bool = False) -> int:
        """GET /v1/mesh/wait?addressed=1&timeout=0 from the cursor until 204, each page
        from the last one's scan position. Each page's lines go out before the cursor
        moves, and the cursor SAVED is the answer's resume point: since STATUS E13 the scan
        position itself (a place in the commit order); before it, a point the server held
        back so a write still open then was read next time (review F1).

        `anchor`: there is no cursor yet. One call without `since`, which the server
        resolves to the newest mesh row the node can see (or the beginning, when it sees
        none), and that POSITION becomes the cursor; nothing is emitted. A row that lands
        in that same instant counts as before the arm."""
        emitted = 0
        retried = False
        since = self.store.cursor
        while True:
            self._check_stop()
            if not anchor:
                self._pull()
            # `seq=1`: the commit order's cursor (STATUS E13), which the server answers only
            # to a client that asks for it, so a monitor from before E13 keeps the cursor it
            # reads (review finding 1). A server before E13 ignores the parameter.
            query = {"timeout": "0", "addressed": "1", "seq": "1"}
            if since:
                query["since"] = since
            jwt = self.state["jwt"]
            # Divergence 033: a proof for THIS GET, so the addressed wait may preview a body
            # as memory.read would show it (027's gate asks for one when DPoP is enforced).
            # A server before 033 ignores it; a proof is spent, so each page makes its own.
            # (`self.state["base_url"]` is `self.base_url`: `load_state` refuses any other.)
            request_headers = node._proved_headers(self.state, jwt, "GET", WAIT_PATH)
            try:
                status, body, headers = node.http_request(
                    "GET", self.base_url, WAIT_PATH, headers=request_headers,
                    query=query, timeout=HTTP_TIMEOUT_S)
            except (OSError, http.client.HTTPException) as exc:
                raise _Transient(type(exc).__name__)
            if status == 401:
                if not retried and not self._fresh():
                    retried = True
                    self._refresh("refused")
                    continue
                self._refused("http_401")
            if status == 429 or status >= 500:
                raise _Transient(f"http_{status}")
            if 400 <= status < 500:
                raise self._refusal(status, body)
            resume = headers.get(RESUME_HEADER)
            if status in (200, 204) and (headers.get(ADDRESSED_HEADER) != "1"
                                         or cursor_order(resume) is None):
                raise _Terminal("server_unsupported",
                                f"{WAIT_PATH} ignored addressed=1 (no {ADDRESSED_HEADER}, or "
                                f"no usable {RESUME_HEADER}): the server predates "
                                "divergence 031", 1)
            if status == 204:
                position = headers.get(CURSOR_HEADER)
                if cursor_order(position) is None:
                    raise _Transient("wait_bad_body")
                self._advance(position if anchor else resume)
                if not anchor:
                    self.unsynced = 0
                return emitted
            if status == 200:
                messages = body.get("messages") if isinstance(body, dict) else None
                cursor = body.get("cursor") if isinstance(body, dict) else None
                if not isinstance(messages, list) or cursor_order(cursor) is None:
                    raise _Transient("wait_bad_body")
                if anchor:
                    self._advance(cursor)
                    return emitted
                if not messages and cursor == since:
                    self.unsynced = 0
                    return emitted                   # no progress: nothing more to read
                for frame in messages:
                    # A stop (signal, --count) between two lines leaves the cursor where
                    # it was: the next run re-reads this page and the keys dedupe it.
                    self._check_stop()
                    if isinstance(frame, dict) and frame.get("type") == "mesh_redaction":
                        self._redaction(frame, "catch_up")
                    elif isinstance(frame, dict) and frame.get("type") == "mesh_edit":
                        self._edit(frame, "catch_up")
                    else:
                        emitted += self._message(frame, "catch_up")
                self._advance(resume)
                since = cursor
                continue
            raise _Terminal("wait_refused",
                            f"{WAIT_PATH} answered {status}: {json.dumps(body)[:300]}", 1)

    def _refusal(self, status: int, body) -> Exception:
        """A catch-up 4xx other than 401 and 429. Terminal only when the SERVER refused,
        with its typed error; `peer_inactive` only for its own code (review B2)."""
        code = _server_code(body)
        if code is None:
            return _Transient(f"http_{status}")
        detail = f"{WAIT_PATH} answered {status}: {json.dumps(body)[:300]}"
        if status == 403 and code in GRANT_CODES:
            return _Terminal("grant_gone", detail, 1)
        if status == 403 and code == FORBIDDEN_CODE:
            return _Terminal("peer_inactive", detail, 1)
        return _Terminal("wait_refused", detail, 1)

    def _pull(self) -> None:
        """Before each catch-up page: take what the open socket buffered off it, without
        waiting, print a frame line for each frame that is not a message NOW, and keep the
        message frames, in order, for `_drain` after the catch-up (so no live message line
        goes out between two catch-up pages; a mesh_redaction or mesh_edit frame is kept with
        them, in order). A frame line's `at` is its emit time; printed
        here it is within one page of the frame's arrival, where printed by `_drain` it
        trailed it by the whole catch-up (review finding 9 of client R6). A closed socket
        is left for `_drain` to meet."""
        from websockets.exceptions import ConnectionClosed

        ws = self.ws
        if ws is None:
            return
        kept: list = []
        early, self._early = self._early, []
        try:
            for raw in early:
                if not self._frame_now(raw):
                    kept.append(raw)
            while True:
                try:
                    raw = ws.recv(timeout=0)
                except (TimeoutError, ConnectionClosed):
                    return
                if not self._frame_now(raw):
                    kept.append(raw)
        finally:
            self._early = kept + self._early

    def _frame_now(self, raw) -> bool:
        """`_frame` for a frame that is not a mesh_message, a mesh_redaction or a mesh_edit (True:
        its line is out, or it was a repeat); False, and nothing printed, for one of those three,
        which keep the order they came in (ORDERED_FRAMES)."""
        frame = _parse_frame(raw)
        if isinstance(frame, dict) and frame.get("type") in ORDERED_FRAMES:
            return False
        self._unhandled(frame)
        return True

    def _drain(self, ws) -> int:
        """Emit what the socket buffered, without waiting."""
        from websockets.exceptions import ConnectionClosed

        n = 0
        early, self._early = self._early, []
        for raw in early:
            n += self._frame(raw)
        while True:
            try:
                raw = ws.recv(timeout=0)
            except TimeoutError:
                return n
            except ConnectionClosed as exc:
                if ws is not self.ws:
                    return n
                self._socket_closed(exc)
            n += self._frame(raw)

    def _socket_closed(self, exc):
        code = exc.rcvd.code if exc.rcvd else None
        _revoked(exc)
        if code == 4401:          # The server's "credential expired" close.
            # The token that lapsed is the socket's. Sign in again only if the state still
            # holds it: a rotation the server refused has left a fresh one (review F3).
            if self.state.get("jwt") == self.ws_jwt:
                self.force_refresh = "credential_expired"
            self._dropped("credential_expired", reason="socket_closed", code=code)
        self._dropped("socket_closed", reason="socket_closed", code=code)

    # ---- live
    def _live(self) -> None:
        from websockets.exceptions import ConnectionClosed

        next_sync = time.monotonic() + self.sync_every
        while True:
            self._check_stop()
            now = time.monotonic()
            if now >= next_sync or self.unsynced >= _sync_after():
                self._sync()
                next_sync = time.monotonic() + self.sync_every
                continue
            if self.store.unsaved and now >= self.save_retry_at:
                self.store.save(pauses=())
                self.save_retry_at = time.monotonic() + SAVE_RETRY_EVERY_S
                self._note_saving()
                continue
            if self._rotation_due(now):
                self._rotate()
                continue
            try:
                raw = self.ws.recv(timeout=max(0.0, min(SLICE_S, next_sync - now)))
            except TimeoutError:
                continue
            except ConnectionClosed as exc:
                self._socket_closed(exc)
            self._frame(raw)

    def _sync(self) -> None:
        """The catch-up on an open stream: silent unless it finds a message. Then what the
        catch-up took off the socket (`_pull`) goes out, deduped against its own lines, as
        after the session's catch-up: `_live` reads only the socket, so a message frame left
        in `_early` waited for the next `_drain` (a rotation's) and, its key aged out of
        RECENT_KEYS by then, was printed a second time as `live` (review MEDIUM-2 of
        integ/r14)."""
        try:
            self._catch_up()
            self._drain(self.ws)
        except _Transient as t:
            self._dropped(t.kind, reason="error", error=t.kind)

    def _expires_at(self) -> float | None:
        """The recorded expiry of the token the open socket was opened with."""
        raw = self.ws_expires
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _rotation_due(self, now: float) -> bool:
        if now < self.rotate_after:
            return False
        expires = self._expires_at()
        return expires is not None and expires - time.time() < ROTATE_MARGIN_S

    def _rotate(self) -> None:
        """Make before break: a fresh JWT, a second socket subscribed with it, the old
        socket's buffered frames emitted and the old socket closed, then a catch-up for
        anything between. The stream never closes, so the only line is `auth_refreshed`.
        A failure keeps the old socket and tries again later with the SAME fresh token
        (a peer at its socket cap refuses the second socket until one closes); the
        server's own 4401 close at the lapse is the fallback."""
        try:
            if self.state.get("jwt") == self.ws_jwt or self._mint_reason():
                self._refresh("rotation")
            jwt, expires = self.state["jwt"], self.state.get("jwt_expires_at")
            new = self._open(jwt)
        except (_Transient, _Refused):
            self.rotate_after = time.monotonic() + ROTATE_RETRY_S
            return
        old, self.ws = self.ws, new
        self.ws_jwt, self.ws_expires = jwt, expires
        try:
            self._drain(old)
        finally:
            self._close_ws(old)
        self._drain(new)
        self._sync()


# --------------------------------------------------------------------------- entry

_SIGNALS = ("SIGINT", "SIGTERM", "SIGBREAK")


def run(args, stdout=None) -> int:
    """`listen --monitor`. Returns the exit code; writes nothing but stdout lines.

    Also reached by plain `listen` given a flag only the monitor honours (`cli.run`): that
    is refused the plain CLI's way, before anything is sent (review I1)."""
    if not getattr(args, "monitor", False):
        raise node.Refused(f"{', '.join(getattr(args, 'monitor_only', None) or ())} only "
                           "apply to `listen --monitor`")
    lines = Lines(stdout if stdout is not None else sys.stdout)

    def terminal(type_: str, detail: str, code: int) -> int:
        lines.put({"event": "status", "state": "error", "type": type_, "terminal": True,
                   "detail": detail})
        return code

    try:
        base_url = node.normalize_base_url(args.base_url)
        state = node.load_state(args.state_file, base_url)
        node.require_peer(args.state_file, state)
    except node.Refused as exc:
        return terminal("state_file", str(exc), 2)
    except OSError as exc:            # it exists and cannot be read (review B5)
        return terminal("state_file", f"state file {args.state_file} cannot be read: "
                        f"{type(exc).__name__}: {exc.strerror or exc}", 2)
    try:
        store = CursorStore.load(sidecar_path(args.state_file), base_url, state["peer_id"])
    except CursorFileError as exc:
        return terminal("cursor_file", str(exc), 2)

    monitor = Monitor(args.state_file, state, store, lines,
                      backoff_initial=args.backoff_initial, backoff_cap=args.backoff_cap,
                      sync_every=args.sync_every, count=args.count, duration=args.duration)
    previous = {}
    for name in _SIGNALS:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(
                signum, lambda num, _frame, n=name: monitor.request_stop("signal", n))
        except (ValueError, OSError):       # not the main thread, or not settable here
            pass
    try:
        return monitor.run()
    except Exception as exc:                # a defect must still reach the reader
        return terminal(type(exc).__name__, _scrub(str(exc), state, monitor.state)[:300], 1)
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
