"""Call delivery without Redis: the CallBridge (divergence 041 S3, S5, S6 and S7; design 4.2 to 4.9).

Design `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6). Two triggers
(sql_meshychat/018) NOTIFY a pointer at commit:

    meshy_call        {call_id, status, target}   on every status edge of skill_calls
    meshy_call_chunk  {call_id, sequence}         on every committed skill_call_chunks row

and every serving worker runs one CallBridge that LISTENs on both.

S3 BUILT THE SKELETON, S5 FILLS THE TARGET'S HALF, S6 THE CALLER'S TERMINALS AND S7 ITS
CHUNKS. It LISTENs, parses each payload for its pointer, decides what the notification asks of
THIS worker, and counts that decision by reason (design section 7):

  meshy_call, routed             the `target` hint holds a socket here    S5: re-read the ROW;
                                 (`call:routed:target_here`), or not       send skill_invoke to
                                 (`call:routed:target_not_here`)           the ROW's target
  meshy_call, queued             the hint holds a socket here             S5: the safety net,
                                 (`call:queued:target_here`), or not       queued -> routed for a
                                                                           row target held here
  meshy_call, a terminal status  the call is registered here             S6: re-read the ROW;
                                 (`call:terminal:registered`), or not      the one terminal
                                 (`call:terminal:no_registration`)         mapping's frame to
                                                                           its proven sockets
  meshy_call, any other status   nothing to do (`call:status_ignored`)
  meshy_call_chunk               the call is registered here             S7: read the ROW by
                                 (`chunk:registered`), or not              (call_id, sequence);
                                 (`chunk:no_registration`)                 skill_chunk to its
                                                                           proven sockets
  malformed                      `call:undecodable` / `chunk:undecodable`  never anything

THE TARGET'S HALF (041 S5; design 4.4, 4.6, 4.8, 4.9, Q14, Q17, critique F8, 2.12.3):
  * THE ROUTED ARM. The row is read by its UNIQUE call_id under `server_internal` and must be
    `routed` and OFFERABLE (below); on the loop, in one turn, its target must hold a socket
    here, and `skill_invoke` is built from the ROW (`envelope = {payload: args, signature:
    args_signature, signing_kid: args_signing_kid}`, no continuation fields) and sent to
    EVERY socket of the row's target with `publish_to_peer`, no `by_key` (4.6: a call's args
    depend on nothing but the target). A forged pointer can make a real row reach its real
    target again, and nothing else.
  * THE QUEUED SAFETY NET (4.3). One guarded statement routes the row, with its event, only
    if it is `queued`, OFFERABLE and its target is one of the peers with a socket HERE (read
    on the loop): the ROW's target decides, never the hint. Its own `routed` pointer then
    delivers. A presence miss delays a call by one notification instead of stranding it.
  * REDELIVERY (4.8). On a socket's connect (`redeliver`, spawned by the consumer after its
    welcome and its presence row) and in the recovery sweep of every (re)LISTEN (for every
    peer with a socket here): the peer's `pending`, `routed`, `queued` and `running` calls
    that are OFFERABLE, most urgent first, at most OFFER_LIMIT. In one transaction the
    `pending` and `queued` ones are routed, guarded, with their events, and COMMITTED; their
    invokes are sent by those edges' own pointers, so no invoke is ever queued for a row that
    is not yet `routed`. The `routed` and `running` ones are sent again unchanged, to the
    connecting socket, or to every socket of the peer on a
    recovery; the target dedupes by call_id.
  * THE RE-OFFER SWEEP (Q17, F8), every `reoffer_secs`: the `routed`, OFFERABLE calls of
    peers with a socket here that no target event has answered, routed longer than
    `reoffer_age_secs` ago, at most OFFER_LIMIT, sent again. A LOSS on a local socket's send
    queue (`request_reoffer`) runs a pass at once over THAT peer's unanswered calls only, with
    no age floor, and at most once per `loss_reoffer_interval_secs` per peer (S5 review F1: a
    loss used to run the whole sweep, re-sending every local peer's aged calls, and the
    pass's own pushes into a full queue reported new losses, so a slow reader kept it running
    every poll). A loss pass is not the periodic sweep and does not postpone it.
  * LOST TERMINALS (F04, operator decision 60, 2026-09-27; ASDLC OI-30; docs/DIVERGENCES.md
    048). A caller's registration ends when its terminal frame is QUEUED, so a terminal the
    drop-oldest send queue then evicts was sent by no path. The writer's loss record names the
    call of every such frame (send_queue.FrameLoss.terminal_call_ids: only those no copy of
    which is still queued or was taken by the writer), and `request_reoffer` HOLDS those names
    for the socket that lost them, whatever the throttle says, at most REPUSH_PENDING_MAX per
    socket. The pass that runs for the peer (the loss pass, or the periodic sweep when the
    loss pass is throttled) takes at most `offer_limit` per socket, drops the ones whose socket
    is gone, re-reads the ROWS by call_id (terminal only), keeps those whose caller is the
    socket's peer (5.1), and on the loop, in one turn, pushes each one's frame through the one
    terminal mapping to THAT socket, unless the socket has a copy queued or taken
    (`SendQueue.has_terminal`): a caller is not told twice. A read that fails holds the names
    again for the next pass. A non-terminal frame a loss took stays dropped (4.9).
  * OFFERABLE (`_OFFERABLE`, one definition for every path): the deadline is ahead, the
    target is NOT the memory authority (whose calls run in-process, 4.4), the row carries the
    caller's signature and kid (a `dashboard_test` row carries none, F16) and a caller, the
    target peer is `active`, and so is the CALLER (operator decision 2026-09-25, divergence
    041's dated paragraph on F23 F8: a caller suspended after step 5 has its in-flight calls
    offered by no path, the doorbell's included, and the deadline sweep ends them).

POINTERS ONLY. NOTIFY is unauthenticated: any session on the database can send one on any
channel. So a payload is parsed for the pointer and nothing else (`parse_call_payload`,
`parse_chunk_payload`): an extra key is never read, and a call_id must be a call id by the
rule the HTTP path and the frames apply (`apps/calls/repository.validate_call_id`). `status`
and `target` are HINTS: from S5 on, every frame is built from the committed ROW, re-read by
its unique call_id, and a forged notification can at most make a real row reach its real
party again (the guarantee divergence 020 proves for mesh nudges). In S3 a forged
notification is counted and does nothing (T14's no-op half, tests/test_call_bridge.py).

THE SHAPE, a second instance of LiveBridge's pattern (mesh_notify.py:591-909), NOT added to it
(design 4.2): its own daemon thread (`meshy-call-bridge`), its own LISTEN connection and its
own query connection, both role-bound through apps/core/dbconn, handing work to the event loop
that owns the sockets with `run_coroutine_threadsafe`. A chunk stream can notify far faster
than the mesh does, and one thread would delay mesh nudges behind chunk reads. It starts with
the first socket in the process (`attach`, called from the consumer's connect after the live
bridge, as consumer.py:345-349 starts that one), because only a worker holding sockets has
anything to deliver. On every (re)LISTEN it runs the RECOVERY SWEEP FIRST and reports
listening after (mesh_notify.py:703-707), so a presence row, written only after LISTENING, is
only ever written by a worker that has swept.

COUNTED ON THE LOOP. Every per-notification and per-sweep REASON is counted inside the
coroutine that decided it, on the event loop, in the same turn as the registry read the
decision rests on; from S5 on, a frame is queued in that turn too, so a count never lags the
wire (the race divergence 020 hit). The listener's own lifecycle (`listens`, `listen_errors`,
`<channel>:errors`) and the beat's database results are counted on its thread.

THE SWEEPS: `_recovery_sweep` on every (re)LISTEN, and `_reoffer_sweep` every `reoffer_secs`
(10 s, Q17). Each reads the peers with a socket here through the loop and counts itself; S5
fills them with redelivery and the re-offer (above). S6 adds `_terminal_recheck` after each,
before listening is reported on a (re)LISTEN and on the periodic sweep's cadence: every call
registered here is re-read, and a terminal one's frame is delivered (a pointer lost to a LISTEN
connection that was down, or to a loop hop that timed out). S7 adds `_chunk_recovery` to the
(re)LISTEN sweep, BEFORE its terminal re-check (a terminal ends the registrations the chunks
go to): for every call with a proven registration here, the chunks past that registration's
high-water mark, at most the newest 200. Not on the periodic cadence (design 4.6): a chunk
pointer lost to a loop hop is recovered by the client (a sequence gap, then a subscribe or
GET .../chunks), as 4.9 states for a chunk the send queue drops.

THE CALLER'S CHUNKS (041 S7; design 4.6's chunk arm, 4.7, 5.1). A chunk pointer for a call
registered here makes the bridge read the chunk ROW by its UNIQUE (call_id, sequence)
(`apps/calls/chunks.read_one`) and build the frame through the one chunk frame
(apps/wsgw/caller_frames.chunk_frame); on the loop, in one turn, it goes to every PROVEN
registration of the call, each one's high-water mark advanced (a provisional one, a subscribe
still reading, gets nothing: its replay reads the row after it is proven). No row
(`chunk:row_missing`: a forged pointer, or a chunk the trim at terminal removed first) sends
nothing: a forged pointer can make a real chunk reach its real caller again, and nothing
else.

THE CALLER'S TERMINALS (041 S6; design 4.6's terminal arm, 4.7, 5.1). A terminal pointer for a
call registered in this worker's CallRegistry (apps/wsgw/call_registry.py) makes the bridge
re-read the ROW by its unique call_id and build the frame through the ONE terminal mapping
(apps/wsgw/caller_frames.terminal_frame); on the loop, in one turn, it goes to every PROVEN
    registration of the call, each then removed, and a provisional one (a subscribe still reading)
    is marked instead. A pointer whose row is not terminal sends nothing
(`terminal:not_terminal`): a forged pointer can make a real terminal reach its real caller
again, and nothing else.

PRESENCE (design 4.3; the store is apps/wsgw/hub_presence.py, sql_meshychat/016):
  * `attach`: the socket's row is written only once this bridge reports LISTENING; a worker
    that cannot deliver must not look online, or calls to its sockets sit `routed` in a black
    hole until their deadline. If the bridge is not listening yet, the first beat after it
    is writes the row.
  * `detach`: the consumer's disconnect deletes the row, after the socket is deregistered and
    after any beat that already snapshotted it: the beat holds `_presence_lock` from its
    registry snapshot to its UPSERT, and the delete takes the same lock (S2 review F3).
    BOUNDED (S3 review F1): the socket is first recorded as DEPARTED, and the delete waits for
    the lock at most `detach_wait_secs` (1 s), polling it on the event loop, so a waiting
    disconnect holds no thread of the default executor. A beat still holding the lock after
    that is stuck on the database, and its open write may hold this very row's lock, so the
    detach does not try the delete: it returns, and that beat deletes every departed socket's
    row AFTER its write, whether the write committed or not. The row then reads online until
    the stuck beat ends, as every row of a worker whose beat is stuck does.
  * THE BEAT, every `beat_secs` (10 s) and at once after each (re)LISTEN, runs on this
    bridge's thread only while its LISTEN connection is up, and reads the registry THROUGH
    THE LOOP. A worker whose LISTEN connection is down, or whose loop is wedged, stops
    beating, and its rows read offline after `hub_presence.ONLINE_WINDOW_SECS`. Each beat also
    prunes every worker's rows unseen for `hub_presence.PRUNE_AFTER_SECS` (S2 review F6).
  * `worker_token` is drawn when the bridge's thread starts, not at import (S2 review I1).

BOUNDED CONNECTIONS (S3 review F1). The insert's, the delete's and the query connection (the
beat, the prune, the departed drain and, from S5, the row reads) are opened with a statement
bound (`STATEMENT_TIMEOUT_MS`) and a connect timeout (`CONNECT_TIMEOUT_SECS`). A beat's UPSERT
that names a peer for the first time checks the foreign key, which waits on any transaction
holding that peer's row FOR UPDATE (a key publish, apps/peers/keys.py:186); unbounded, the beat
held the presence lock and this thread for as long as that transaction lived. Now it gives up,
is counted as a beat error, and the listener goes on with the notifications. The LISTEN
connection runs no statement that can wait, and keeps LiveBridge's shape.

NOT YET (design section 7): the webhook doorbell for a `queued` call (S9).
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import functools
import json
import logging
import threading
import time
import uuid

from psycopg.types.json import Jsonb

from apps.calls import chunks as call_chunks
from apps.calls import reads, terminal
from apps.calls.envelope import SignedEnvelope
from apps.calls.repository import validate_call_id
from apps.calls.route import memory_peer_id_from_settings
from apps.core import dbconn, rls

from . import caller_frames, envelope, hub_presence
from .call_registry import CALL_REGISTRY, CallRegistry
from .connection_manager import MANAGER, ConnectionManager
from .constants import (
    CALL_CHUNK_NOTIFY_CHANNEL,
    CALL_NOTIFY_CHANNEL,
    CALL_NOTIFY_POLL_SECS,
    CALL_REOFFER_SWEEP_SECS,
    LISTEN_BACKOFF_INITIAL_SECS,
    LISTEN_BACKOFF_MAX_SECS,
    LISTEN_CLEAN_RESTART_FLOOR_SECS,
)
from .mesh_notify import LOOP_CALL_TIMEOUT_SECS, READY_TIMEOUT_SECS, _QueryConn
from .send_queue import TERMINAL_MEMORY

log = logging.getLogger(__name__)

#: Every status skill_calls' CHECK admits (sql/006, widened by sql/080). A pointer naming
#: anything else did not come from the trigger. tests/test_call_bridge.py reads the live
#: constraint and compares. The same ten as `apps/calls/reads.CALL_STATUSES` (the wire
#: strings, pinned to the same CHECK by tests/test_a2a.py), so it is built from them.
CALL_STATUSES = frozenset(reads.CALL_STATUSES)

#: The statuses that ask a TARGET's worker for work (design 4.6): deliver, or route.
OFFERED_STATUSES = frozenset({"routed", "queued"})

#: The statuses that end a call and ask a CALLER's worker for its terminal frame: the one
#: terminal mapping's (`apps/calls/terminal`), which `_terminal_rows` reads too.
TERMINAL_STATUSES = terminal.TERMINAL_STATUSES

#: skill_call_chunks.sequence is INTEGER: a pointer outside it cannot name a row. The upper
#: bound is the chunk frame's own (`envelope.SEQUENCE_MAX`, the same int4 column).
SEQUENCE_MIN, SEQUENCE_MAX = -(2 ** 31), envelope.SEQUENCE_MAX

#: The statement bound on every connection the bridge writes presence or queries through
#: (S3 review F1). Half a beat interval: a beat stuck on a locked peer row gives up before the
#: next one is due.
STATEMENT_TIMEOUT_MS = 5000

#: libpq's connect_timeout, integer seconds, on the same connections.
CONNECT_TIMEOUT_SECS = 5

#: How long a disconnect's `detach` waits for a beat that holds the presence lock (S3 review
#: F1). A beat holds it for one loop hop and one UPSERT, milliseconds; one still holding it
#: after this is stuck, and the delete is handed to it.
DETACH_LOCK_WAIT_SECS = 1.0

#: How often a waiting `detach` retries the lock, on the event loop.
DETACH_LOCK_POLL_SECS = 0.02

#: The most calls one redelivery or one sweep offers (rule B5), most urgent deadline first.
#: Under the send queue's SEND_QUEUE_CAPACITY (256), so one pass cannot evict its own frames.
OFFER_LIMIT = 64

#: The statuses redelivery offers.
REDELIVERED_STATUSES = ("pending", "routed", "queued", "running")

#: The most registered calls one terminal re-check reads in one statement (rule B5); a worker
#: with more reads them in batches of this many. The lost-terminal re-push reads in the same
#: batches.
RECHECK_BATCH = 500

#: The most lost terminal call ids the bridge holds for one socket (F04, rule B8): as many as
#: that socket's queue can name between two of its writer's takes. One past it is counted
#: `terminal_repush:overflow`, and its caller recovers by GET /v1/calls/{id} or a subscribe.
REPUSH_PENDING_MAX = TERMINAL_MEMORY

#: OFFERABLE: the one definition of a call that may be offered to its target (module
#: docstring). The call's alias is `c`; `%(authority)s` is the memory authority's peer id.
_OFFERABLE = (
    "c.deadline_at > now()"
    " AND c.target_peer_id <> %(authority)s"
    " AND c.args_signature IS NOT NULL AND c.args_signing_kid IS NOT NULL"
    " AND c.caller_peer_id IS NOT NULL"
    " AND EXISTS (SELECT 1 FROM peers p WHERE p.id = c.target_peer_id AND p.status = 'active')"
    # The caller's standing too (operator decision 2026-09-25, divergence 041 F23 F8).
    " AND EXISTS (SELECT 1 FROM peers q WHERE q.id = c.caller_peer_id AND q.status = 'active')")

#: What a skill_invoke is built from, in `_Offer`'s field order.
_OFFER_COLUMNS = ("c.call_id, c.caller_peer_id, c.target_peer_id, c.skill_name, "
                  "c.skill_version, c.mode, c.deadline_at, c.args, c.args_signature, "
                  "c.args_signing_kid, c.status")

#: queued -> routed for a row target held here, with its event (the safety net, 4.3).
_SAFETY_NET = (
    "WITH routed AS ("
    " UPDATE skill_calls c SET status = 'routed'"
    "  WHERE c.call_id = %(call_id)s AND c.status = 'queued'"
    "    AND c.target_peer_id = ANY(%(local)s::uuid[]) AND " + _OFFERABLE +
    " RETURNING c.call_id) "
    "INSERT INTO skill_call_events (call_id, event_type, details, source, source_peer_id) "
    "SELECT call_id, 'routed', %(details)s, 'meshy', NULL FROM routed RETURNING call_id")

#: pending | queued -> routed, with events, for the rows a redelivery selected (4.8).
_REDELIVERY_ROUTE = (
    "WITH routed AS ("
    " UPDATE skill_calls c SET status = 'routed'"
    "  WHERE c.call_id = ANY(%(ids)s::text[]) AND c.status IN ('pending', 'queued')"
    "    AND c.target_peer_id = ANY(%(peers)s::uuid[]) AND " + _OFFERABLE +
    " RETURNING c.call_id) "
    "INSERT INTO skill_call_events (call_id, event_type, details, source, source_peer_id) "
    "SELECT call_id, 'routed', %(details)s, 'meshy', NULL FROM routed RETURNING call_id")

#: The re-offer sweep's selection (Q17): routed, offerable, no target event, and routed longer
#: ago than the age floor, or targeted at a peer whose send queue lost frames.
_REOFFER = (
    f"SELECT {_OFFER_COLUMNS} FROM skill_calls c"
    " WHERE c.target_peer_id = ANY(%(local)s::uuid[]) AND c.status = 'routed'"
    " AND " + _OFFERABLE +
    " AND NOT EXISTS (SELECT 1 FROM skill_call_events e"
    "                  WHERE e.call_id = c.call_id AND e.source = 'target')"
    " AND (c.target_peer_id = ANY(%(urgent)s::uuid[])"
    "      OR COALESCE((SELECT max(e.occurred_at) FROM skill_call_events e"
    "                    WHERE e.call_id = c.call_id AND e.event_type = 'routed'),"
    "                  c.created_at) < now() - make_interval(secs => %(age)s))"
    " ORDER BY c.deadline_at LIMIT %(limit)s")


# --------------------------------------------------------------------------- #
# The pointers: pure, no I/O
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class CallPointer:
    """A `meshy_call` payload, as far as it is ever read: which row, and two hints."""

    call_id: str
    status: str
    target: uuid.UUID


@dataclasses.dataclass(frozen=True)
class ChunkPointer:
    """A `meshy_call_chunk` payload: which chunk row."""

    call_id: str
    sequence: int


def _object(raw) -> dict | None:
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _call_id(value) -> str | None:
    """A call id by the HTTP path's rule, or None. A lone surrogate fails its UTF-8 length
    check with UnicodeEncodeError, which is a ValueError."""
    if not isinstance(value, str):
        return None
    try:
        return validate_call_id(value)
    except ValueError:
        return None


def _canonical_uuid(value) -> uuid.UUID | None:
    """The trigger renders a uuid in its canonical form; any other spelling is not its."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def parse_call_payload(raw) -> CallPointer | None:
    """The pointer in a `meshy_call` payload, or None when it cannot be one (skipped and
    counted, never fatal, as mesh_notify.py:753-758 treats a mesh payload)."""
    obj = _object(raw)
    if obj is None:
        return None
    call_id = _call_id(obj.get("call_id"))
    status = obj.get("status")
    target = _canonical_uuid(obj.get("target"))
    if call_id is None or target is None:
        return None
    if not isinstance(status, str) or status not in CALL_STATUSES:
        return None
    return CallPointer(call_id=call_id, status=status, target=target)


def parse_chunk_payload(raw) -> ChunkPointer | None:
    """The pointer in a `meshy_call_chunk` payload, or None."""
    obj = _object(raw)
    if obj is None:
        return None
    call_id = _call_id(obj.get("call_id"))
    sequence = obj.get("sequence")
    if call_id is None or type(sequence) is not int:       # bool is an int subclass
        return None
    if not SEQUENCE_MIN <= sequence <= SEQUENCE_MAX:
        return None
    return ChunkPointer(call_id=call_id, sequence=sequence)


@dataclasses.dataclass(frozen=True)
class _Offer:
    """A call as a skill_invoke is built from it: its row, never a pointer."""

    call_id: str
    caller_peer_id: uuid.UUID
    target_peer_id: uuid.UUID
    skill_name: str
    skill_version: str
    mode: str
    deadline_at: object
    args: object
    args_signature: str
    args_signing_kid: str
    status: str


def _invoke_frame(offer: _Offer) -> dict:
    """skill_invoke from the ROW (design 4.6). ValueError when the row cannot make one."""
    return envelope.skill_invoke(
        call_id=offer.call_id, caller_peer_id=offer.caller_peer_id,
        target_peer_id=offer.target_peer_id, skill_name=offer.skill_name,
        skill_version=offer.skill_version, mode=offer.mode,
        envelope=SignedEnvelope(payload=offer.args, signature=offer.args_signature,
                                signing_kid=offer.args_signing_kid),
        deadline_at=offer.deadline_at)


def _authority() -> uuid.UUID:
    """The memory authority, read per use from the one settings reader (4.4)."""
    return memory_peer_id_from_settings()


# --------------------------------------------------------------------------- #
# The bridge
# --------------------------------------------------------------------------- #

class _BoundedQueryConn(_QueryConn):
    """LiveBridge's lazily opened query connection (mesh_notify._QueryConn), opened through
    the bridge's bounded `_connect` (S3 review F1). The same guard on the new connection."""

    def __init__(self, connect) -> None:
        super().__init__(None)
        self._connect = connect

    def get(self):
        if self._conn is None or self._conn.closed:
            conn = self._connect()
            try:
                dbconn.assert_serving_connection(conn)
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self._conn


class CallBridge:
    """One LISTEN loop on the call channels feeding one ConnectionManager.

    Started by the first socket that connects (`attach`), bound to the event loop that
    socket runs on, because that loop owns every SendQueue and the registry. `beat_secs`,
    `reoffer_secs`, `reoffer_age_secs`, `ready_timeout`, `prune_after_secs`,
    `detach_wait_secs` and `offer_limit` are read on every pass, so a test can change them on a
    running bridge (a new `beat_secs` takes effect after the next beat);
    `statement_timeout_ms` is read when a connection is opened.
    """

    def __init__(self, manager: ConnectionManager, *, registry: CallRegistry | None = None,
                 dsn=None,
                 beat_secs: float = hub_presence.BEAT_INTERVAL_SECS,
                 reoffer_secs: float = CALL_REOFFER_SWEEP_SECS,
                 reoffer_age_secs: float = CALL_REOFFER_SWEEP_SECS,
                 loss_reoffer_interval_secs: float = CALL_REOFFER_SWEEP_SECS,
                 ready_timeout: float = READY_TIMEOUT_SECS,
                 prune_after_secs: float = hub_presence.PRUNE_AFTER_SECS,
                 statement_timeout_ms: int = STATEMENT_TIMEOUT_MS,
                 detach_wait_secs: float = DETACH_LOCK_WAIT_SECS) -> None:
        self.manager = manager
        #: The calls whose caller-side frames this worker delivers (041 S6): the registry its
        #: sockets register into. A bridge built without one gets its own.
        self.registry = registry if registry is not None else CallRegistry()
        self._dsn = dsn
        self.beat_secs = beat_secs
        self.reoffer_secs = reoffer_secs
        #: A `routed` call younger than this has its invoke in flight, and is not offered
        #: again by the periodic sweep (Q17). The sweep's own cadence by default.
        self.reoffer_age_secs = reoffer_age_secs
        #: The least time between two loss-triggered passes for one peer (S5 review F1). A
        #: pass pushes up to `offer_limit` invokes into the socket that lost frames, and into a
        #: full queue each push evicts a frame the writer then reports as a new loss.
        self.loss_reoffer_interval_secs = loss_reoffer_interval_secs
        self.offer_limit = OFFER_LIMIT
        self.ready_timeout = ready_timeout
        self.prune_after_secs = prune_after_secs
        self.statement_timeout_ms = statement_timeout_ms
        self.detach_wait_secs = detach_wait_secs
        #: Drawn when the thread starts (S2 review I1); the presence rows carry it.
        self.worker_token: uuid.UUID | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._listening = threading.Event()
        #: Held from a beat's registry snapshot to its UPSERT, and by a socket's delete: a
        #: beat that snapshotted a socket never writes after that socket's row is deleted.
        #: Never waited on for longer than `detach_wait_secs` by a delete (S3 review F1).
        self._presence_lock = threading.Lock()
        #: Sockets whose disconnect has begun: each one's row must not outlive a beat that
        #: snapshotted it. A detach adds its socket before it waits, and removes it once it
        #: deleted the row under the lock; a beat deletes and removes every one left, after
        #: its write. `_departed_lock` guards the set and is never held across I/O.
        self._departed: set[uuid.UUID] = set()
        self._departed_lock = threading.Lock()
        self._stats: collections.Counter = collections.Counter()
        self._stats_lock = threading.Lock()
        #: Peers whose socket's send queue recorded a loss that was not throttled: the
        #: listener's next poll runs a loss pass over their unanswered calls, without the age
        #: floor. Non-empty is the trigger.
        self._urgent: set[uuid.UUID] = set()
        #: When each peer's last loss pass was accepted (monotonic). Only entries younger than
        #: `loss_reoffer_interval_secs` are kept (rule B8), pruned on every accepted loss.
        self._urgent_accepted: dict[uuid.UUID, float] = {}
        self._urgent_lock = threading.Lock()
        #: F04: per socket (connection_id), its peer and the calls whose terminal frame its
        #: send queue lost, oldest first, at most REPUSH_PENDING_MAX. Held by
        #: `request_reoffer`, taken by `_terminal_repush`; a socket's entry goes when its last
        #: name is taken. Guarded by `_urgent_lock`.
        self._lost_terminals: dict[uuid.UUID, tuple[uuid.UUID, dict[str, None]]] = {}

    # --- lifecycle ------------------------------------------------------------

    def _target(self) -> str:
        return self._dsn or dbconn.dsn()

    def _connect(self):
        """A connection for a presence write or a query: role-bound as every dbconn
        connection is, and bounded (S3 review F1)."""
        return dbconn.connect(self._target(), connect_timeout=CONNECT_TIMEOUT_SECS,
                              statement_timeout_ms=self.statement_timeout_ms)

    async def ensure_started(self, *, ready_timeout: float | None = None) -> bool:
        """Start the listener for the running loop if it is not already. True once LISTEN is
        live. Never raises for a database problem: the socket is still useful without call
        delivery, and waits at most `ready_timeout` for it."""
        loop = asyncio.get_running_loop()
        stale = None
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            if not (alive and self._loop is loop):
                if alive:
                    stale = self._stop
                self._loop = loop
                self._stop = threading.Event()
                self._listening = threading.Event()
                self.worker_token = uuid.uuid4()
                self._thread = threading.Thread(
                    target=self._run,
                    args=(loop, self._stop, self._listening, self.worker_token),
                    name="meshy-call-bridge", daemon=True)
                self._thread.start()
            listening = self._listening
        if stale is not None:
            # A bridge from a loop that is gone exits at its next poll; joining it here
            # would block the loop this socket is served on.
            stale.set()
        if listening.is_set():
            return True
        timeout = self.ready_timeout if ready_timeout is None else ready_timeout
        ready = await asyncio.to_thread(listening.wait, timeout)
        if not ready:
            log.warning("call bridge: LISTEN not established within %.1fs; this socket gets "
                        "no presence row until it is", timeout)
        return ready

    async def wait_listening(self, timeout: float = READY_TIMEOUT_SECS) -> bool:
        return await asyncio.to_thread(self._listening.wait, timeout)

    def is_listening(self) -> bool:
        return self._listening.is_set()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread, stop = self._thread, self._stop
            self._thread = None
            self._loop = None
        stop.set()
        if thread is not None:
            thread.join(timeout)

    def stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)

    def _count(self, name: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[name] += n

    # --- presence (design 4.3), called by the consumer ------------------------------

    async def attach(self, connection_id: uuid.UUID, peer_id: uuid.UUID) -> bool:
        """The consumer's connect, after the live bridge: start this bridge or join it, and
        write the socket's presence row ONLY once the bridge reports LISTENING. True when a
        row was written. When it was not, the first beat after LISTENING writes it."""
        if not await self.ensure_started():
            self._count("presence:insert_skipped_not_listening")
            return False
        return await asyncio.to_thread(self._insert_row, connection_id, peer_id,
                                       self.worker_token)

    async def detach(self, connection_id: uuid.UUID) -> bool:
        """The consumer's disconnect, AFTER the socket is deregistered: delete its row, once
        any beat that snapshotted it has written. True when a row was deleted here.

        BOUNDED (S3 review F1). The socket is recorded as departed, then the presence lock is
        polled on the loop for at most `detach_wait_secs`, so the wait holds no executor
        thread. Not taken by then, a beat holding it is stuck on the database, and its open
        write may hold this row's lock too; the delete is not tried, and that beat, or the
        next, deletes the row after its write (`_drain_departed`). False then."""
        with self._departed_lock:
            self._departed.add(connection_id)
        if not await self._take_presence_lock(self.detach_wait_secs):
            self._count("presence:delete_handed_to_beat")
            return False
        try:
            return await asyncio.to_thread(self._delete_row, connection_id)
        finally:
            self._presence_lock.release()

    # --- redelivery and the loss trigger (041 S5), called by the consumer ----------------

    async def redeliver(self, handle) -> int:
        """A socket's connect-time redelivery (design 4.8),
        spawned by the consumer AFTER its welcome is queued and its presence row written.
        Its peer's in-flight calls are selected and the `pending` and `queued` ones routed and
        committed (their own pointers send them); the `routed` and `running` ones are sent
        again to THIS socket. Returns the frames queued. Counted as `redelivery:runs` once
        the frames are queued; a failure is counted and logged, never raised."""
        try:
            offers = await asyncio.to_thread(self._redeliver_for, handle.peer_id)
        except Exception as exc:
            self._count("redelivery:errors")
            log.warning("call bridge: redelivery to peer %s failed: %s: %s",
                        handle.peer_id, type(exc).__name__, exc)
            return 0
        sent = await self._offer(offers, "redelivery", only=handle)
        self._count("redelivery:runs")
        return sent

    def request_reoffer(self, peer_id: uuid.UUID, *, connection_id: uuid.UUID | None = None,
                        terminal_call_ids=()) -> None:
        """A local socket's send queue dropped frames (the consumer's writer found the loss
        record): within one poll, offer THAT peer's unanswered calls again without waiting
        for the age floor (design 4.6, critique F8), unless a loss pass for it was accepted
        less than `loss_reoffer_interval_secs` ago; then it is counted as throttled, and the
        periodic sweep still covers the peer (S5 review F1). Thread-safe.

        F04: `terminal_call_ids` are the calls whose terminal frame the socket `connection_id`
        lost (send_queue.FrameLoss.terminal_call_ids). They are held for that socket whatever
        the throttle decides, and the next pass for the peer pushes each one to it again
        (`_terminal_repush`), counted `terminal_repush:requested`."""
        if connection_id is not None and terminal_call_ids:
            added = self._hold_lost_terminals(peer_id, connection_id, terminal_call_ids)
            if added:
                self._count("terminal_repush:requested", added)
        now = time.monotonic()
        interval = self.loss_reoffer_interval_secs
        with self._urgent_lock:
            last = self._urgent_accepted.get(peer_id)
            throttled = last is not None and now - last < interval
            if not throttled:
                self._urgent_accepted = {p: t for p, t in self._urgent_accepted.items()
                                         if now - t < interval}
                self._urgent_accepted[peer_id] = now
                self._urgent.add(peer_id)
        self._count("reoffer:loss_requests")
        if throttled:
            self._count("reoffer:loss_throttled")

    def _loss_pending(self) -> bool:
        with self._urgent_lock:
            return bool(self._urgent)

    def _hold_lost_terminals(self, peer_id: uuid.UUID, connection_id: uuid.UUID,
                             call_ids) -> int:
        """Hold lost terminal names for one socket, each once, at most REPUSH_PENDING_MAX
        (rule B8); one past the bound is counted `terminal_repush:overflow` and logged, and its
        caller recovers by GET /v1/calls/{id} or a subscribe. Returns how many were added.
        Thread-safe."""
        added = overflow = 0
        with self._urgent_lock:
            entry = self._lost_terminals.get(connection_id)
            if entry is None:
                entry = (peer_id, {})
                self._lost_terminals[connection_id] = entry
            held = entry[1]
            for call_id in call_ids:
                if call_id in held:
                    continue
                if len(held) >= REPUSH_PENDING_MAX:
                    overflow += 1
                    continue
                held[call_id] = None
                added += 1
            if not held:
                del self._lost_terminals[connection_id]
        if overflow:
            self._count("terminal_repush:overflow", overflow)
            log.warning("call bridge: %d lost terminal frame(s) of peer %s not held for a "
                        "re-push (at most %d per socket); those callers recover by GET "
                        "/v1/calls/{id} or a subscribe", overflow, peer_id, REPUSH_PENDING_MAX)
        return added

    def _take_lost_terminals(self, periodic: bool, urgent) -> list:
        """The held names a pass re-pushes: every socket's on the periodic sweep, the urgent
        peers' sockets' on a loss pass; at most `offer_limit` per socket (fewer than a queue
        holds, so one pass cannot evict its own frames), the rest left for the next pass."""
        taken = []
        with self._urgent_lock:
            for connection_id in list(self._lost_terminals):
                peer_id, held = self._lost_terminals[connection_id]
                if not periodic and peer_id not in urgent:
                    continue
                call_ids = list(held)[:self.offer_limit]
                for call_id in call_ids:
                    del held[call_id]
                if not held:
                    del self._lost_terminals[connection_id]
                taken.append((peer_id, connection_id, call_ids))
        return taken

    def _redeliver_for(self, peer_id: uuid.UUID) -> list[_Offer]:
        conn = self._connect()
        try:
            return self._redeliver_rows(conn, [peer_id], "redelivery")
        finally:
            conn.close()

    def _redeliver_rows(self, conn, peers: list[uuid.UUID], via: str) -> list[_Offer]:
        """Select the peers' in-flight OFFERABLE calls and route the `pending` and `queued`
        ones, guarded, with their events, in ONE transaction, committed before this returns.
        Returns the ones to send directly: those `routed` or `running` when selected."""
        params = {"peers": list(peers), "authority": _authority(),
                  "limit": self.offer_limit, "statuses": list(REDELIVERED_STATUSES),
                  "details": Jsonb({"via": via})}
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_OFFER_COLUMNS} FROM skill_calls c"
                    " WHERE c.target_peer_id = ANY(%(peers)s::uuid[])"
                    " AND c.status = ANY(%(statuses)s::text[]) AND " + _OFFERABLE +
                    " ORDER BY c.deadline_at LIMIT %(limit)s", params)
                offers = [_Offer(*row) for row in cur.fetchall()]
                waiting = [o.call_id for o in offers if o.status in ("pending", "queued")]
                routed = []
                if waiting:
                    cur.execute(_REDELIVERY_ROUTE, dict(params, ids=waiting))
                    routed = [row[0] for row in cur.fetchall()]
        if routed:
            self._count(f"{via}:routed", len(routed))
        return [o for o in offers if o.status in ("routed", "running")]

    async def _offer(self, offers: list[_Offer], kind: str, *, only=None,
                     none: str | None = None) -> int:
        """Send each call's skill_invoke, on the loop, in the turn that checks the registry,
        and count it there. `only` is one socket's handle (a connect's redelivery); without
        it, every socket of the ROW's target that is registered here. `none` is counted when
        there is nothing to offer."""
        if not offers and none:
            self._count(none)
        sent = 0
        for offer in offers:
            try:
                frame = _invoke_frame(offer)
            except ValueError as exc:
                self._count(f"{kind}:unbuildable")
                log.warning("call bridge: call %s cannot be offered: %s", offer.call_id, exc)
                continue
            if only is not None:
                if only.closed or only.peer_id != offer.target_peer_id:
                    self._count(f"{kind}:target_gone")
                    continue
                await only.queue.push(frame)
                sent += 1
            elif await self.manager.is_online(offer.target_peer_id):
                sent += await self.manager.publish_to_peer(offer.target_peer_id, frame)
            else:
                self._count(f"{kind}:target_gone")
        if sent:
            self._count(f"{kind}:sent", sent)
        return sent

    async def _take_presence_lock(self, timeout: float) -> bool:
        """The presence lock, polled on the loop without blocking it; False after `timeout`."""
        deadline = time.monotonic() + timeout
        while not self._presence_lock.acquire(blocking=False):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(DETACH_LOCK_POLL_SECS)
        return True

    def _insert_row(self, connection_id, peer_id, worker_token) -> bool:
        conn = self._connect()
        try:
            written = hub_presence.insert(conn, connection_id, peer_id, worker_token)
        finally:
            conn.close()
        self._count("presence:inserts" if written else "presence:insert_skipped_no_peer")
        return written

    def _delete_row(self, connection_id) -> bool:
        """Run by `detach` while it holds the presence lock: no beat is between its snapshot
        and its write, so once the row is deleted the socket is no longer departed."""
        conn = self._connect()
        try:
            deleted = hub_presence.delete(conn, connection_id)
        finally:
            conn.close()
        with self._departed_lock:
            self._departed.discard(connection_id)
        self._count("presence:deletes")
        return deleted

    # --- the listener thread --------------------------------------------------

    def _run(self, loop, stop: threading.Event, listening: threading.Event,
             worker_token: uuid.UUID) -> None:
        """Reconnect forever with capped exponential backoff."""
        delay = LISTEN_BACKOFF_INITIAL_SECS
        while not stop.is_set() and not loop.is_closed():
            try:
                self._run_once(loop, stop, listening, worker_token)
                delay = LISTEN_BACKOFF_INITIAL_SECS
                stop.wait(LISTEN_CLEAN_RESTART_FLOOR_SECS)
            except Exception as exc:
                listening.clear()
                self._count("listen_errors")
                log.warning("call bridge: %s: %s; retrying in %.1fs",
                            type(exc).__name__, exc, delay)
                stop.wait(delay)
                delay = min(delay * 2, LISTEN_BACKOFF_MAX_SECS)
        listening.clear()

    def _run_once(self, loop, stop: threading.Event, listening: threading.Event,
                  worker_token: uuid.UUID) -> None:
        listen = dbconn.connect(self._target(), pool=False)     # a LISTEN connection is this thread's own, never pooled
        query = _BoundedQueryConn(self._connect)
        try:
            with listen.cursor() as cur:
                cur.execute(f"LISTEN {CALL_NOTIFY_CHANNEL}")
                cur.execute(f"LISTEN {CALL_CHUNK_NOTIFY_CHANNEL}")
            # Whatever was committed while this connection was not listening was never
            # heard, so recover it BEFORE reporting ready (mesh_notify.py:703-707): the
            # target's half (redelivery) and the caller's (its terminals, 041 S6).
            self._sweep_safely("recovery", self._recovery_sweep, loop, query)
            self._sweep_safely("recovery_chunks", self._chunk_recovery, loop, query)
            self._sweep_safely("recovery_terminal",
                               functools.partial(self._terminal_recheck, kind="recovery"),
                               loop, query)
            listening.set()
            self._count("listens")
            self._beat_safely(loop, query, worker_token)
            next_beat = time.monotonic() + self.beat_secs
            last_reoffer = time.monotonic()
            while not stop.is_set():
                if loop.is_closed():
                    return
                # Read each pass, so a changed cadence takes effect at once.
                due = min(next_beat, last_reoffer + self.reoffer_secs)
                wait = min(CALL_NOTIFY_POLL_SECS, max(0.0, due - time.monotonic()))
                # A LISTEN connection that died raises here, before any beat below, so a
                # worker that cannot hear a notification stops beating.
                for note in listen.notifies(timeout=wait):
                    if stop.is_set() or loop.is_closed():
                        return
                    self._handle(note.channel, note.payload, loop, query)
                if stop.is_set() or loop.is_closed():
                    return
                now = time.monotonic()
                if now >= next_beat:
                    self._beat_safely(loop, query, worker_token)
                    next_beat = time.monotonic() + self.beat_secs
                periodic = now >= last_reoffer + self.reoffer_secs
                if periodic or self._loss_pending():
                    # A loss (`request_reoffer`) runs a pass within one poll over the lossy
                    # peers only; it is not the periodic sweep, and does not postpone it.
                    with self._urgent_lock:
                        urgent, self._urgent = self._urgent, set()
                    self._sweep_safely(
                        "reoffer", functools.partial(self._reoffer_sweep, periodic=periodic,
                                                     urgent=urgent),
                        loop, query)
                    # F04: the terminal frames the loss took, on the same pass and for the same
                    # peers (every held one on the periodic sweep).
                    self._sweep_safely(
                        "reoffer_repush", functools.partial(self._terminal_repush,
                                                            periodic=periodic, urgent=urgent),
                        loop, query)
                    if periodic:
                        # The registered calls' terminal re-check rides the periodic cadence
                        # (041 S6), not a loss pass: a loss says nothing about a REGISTERED
                        # call's terminal. The terminals a loss took are named in its record and
                        # re-pushed just above (F04).
                        self._sweep_safely(
                            "reoffer_terminal",
                            functools.partial(self._terminal_recheck, kind="reoffer"),
                            loop, query)
                        last_reoffer = time.monotonic()
        finally:
            listening.clear()
            query.drop()
            listen.close()

    def _on_loop(self, loop, coro):
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            raise
        return future.result(LOOP_CALL_TIMEOUT_SECS)

    # --- notifications --------------------------------------------------------

    def _handle(self, channel: str, payload: str, loop, query: _QueryConn) -> None:
        """One notification. Nothing a single notification does can end the listener."""
        try:
            if channel == CALL_NOTIFY_CHANNEL:
                self._on_call(payload, loop, query)
            elif channel == CALL_CHUNK_NOTIFY_CHANNEL:
                self._on_chunk(payload, loop, query)
            else:
                self._count("unknown_channel")
        except Exception as exc:
            query.drop()
            self._count(f"{channel}:errors")
            log.warning("call bridge: %s notification failed: %s: %s",
                        channel, type(exc).__name__, exc)

    def _on_call(self, payload: str, loop, query: _QueryConn) -> None:
        pointer = parse_call_payload(payload)
        reason = self._on_loop(loop, self._decide_call(pointer))
        if reason == "call:routed:target_here":
            offers = self._read_routed(query, pointer.call_id)
            self._on_loop(loop, self._offer(offers, "invoke", none="invoke:not_offered"))
        elif reason == "call:queued:target_here":
            local = self._on_loop(loop, self.manager.connected_peers())
            self._safety_net(query, pointer.call_id, local)
        elif reason == "call:terminal:registered":
            found = reads.load_call(query.get(), pointer.call_id)
            frame = (caller_frames.terminal_frame(found.row)
                     if isinstance(found, reads.Found) else None)
            self._on_loop(loop, self._deliver_terminals([(pointer.call_id, frame)], "terminal"))

    def _read_routed(self, query: _QueryConn, call_id: str) -> list[_Offer]:
        """The routed arm's read: the row by its UNIQUE call_id, `routed` and OFFERABLE."""
        conn = query.get()
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_OFFER_COLUMNS} FROM skill_calls c"
                    " WHERE c.call_id = %(call_id)s AND c.status = 'routed' AND " + _OFFERABLE,
                    {"call_id": call_id, "authority": _authority()})
                return [_Offer(*row) for row in cur.fetchall()]

    def _safety_net(self, query: _QueryConn, call_id: str, local: list[uuid.UUID]) -> None:
        """The queued safety net (4.3): route the row only if its target is one of `local`,
        the peers with a socket in THIS worker. Its `routed` pointer then delivers."""
        conn = query.get()
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute(_SAFETY_NET, {"call_id": call_id, "local": list(local),
                                          "authority": _authority(),
                                          "details": Jsonb({"via": "safety_net"})})
                routed = cur.fetchall()
        self._count("safety_net:routed" if routed else "safety_net:no_row")

    async def _decide_call(self, pointer: CallPointer | None) -> str:
        """What a `meshy_call` notification asks of THIS worker, decided and counted here,
        on the loop, in the turn that reads the registry. The payload's hints only choose
        whether the row is worth reading; they never decide what is sent."""
        self._count("call:notifications")
        if pointer is None:
            reason = "call:undecodable"
        elif pointer.status in OFFERED_STATUSES:
            here = await self.manager.is_online(pointer.target)
            reason = f"call:{pointer.status}:{'target_here' if here else 'target_not_here'}"
        elif pointer.status in TERMINAL_STATUSES:
            registered = await self.registry.is_registered(pointer.call_id)
            reason = f"call:terminal:{'registered' if registered else 'no_registration'}"
        else:
            reason = "call:status_ignored"
        self._count(reason)
        return reason

    def _on_chunk(self, payload: str, loop, query: _QueryConn) -> None:
        """The chunk arm (041 S7, design 4.6): for a call registered here, the chunk ROW by
        (call_id, sequence), its frame built from the row, delivered on the loop."""
        pointer = parse_chunk_payload(payload)
        reason = self._on_loop(loop, self._decide_chunk(pointer))
        if reason != "chunk:registered":
            return
        row = call_chunks.read_one(query.get(), pointer.call_id, pointer.sequence)
        frame = None if row is None else caller_frames.chunk_frame(pointer.call_id, row)
        if row is not None and frame is None:
            self._count("chunk:unbuildable")
            return
        self._on_loop(loop, self._deliver_chunk(pointer.call_id, pointer.sequence, frame))

    async def _decide_chunk(self, pointer: ChunkPointer | None) -> str:
        """What a `meshy_call_chunk` notification asks of THIS worker, decided and counted on the
        loop, in the turn that reads the registry. The pointer only chooses whether the row is
        worth reading."""
        self._count("chunk:notifications")
        if pointer is None:
            reason = "chunk:undecodable"
        elif await self.registry.is_registered(pointer.call_id):
            reason = "chunk:registered"
        else:
            reason = "chunk:no_registration"
        self._count(reason)
        return reason

    async def _deliver_chunk(self, call_id: str, sequence: int, frame: dict | None) -> int:
        """On the loop, in one turn: the chunk frame to the call's PROVEN registrations
        (CallRegistry.deliver_chunk). None (no such row) sends nothing, counted
        `chunk:row_missing`."""
        if frame is None:
            self._count("chunk:row_missing")
            return 0
        sent = await self.registry.deliver_chunk(call_id, sequence, frame)
        if sent:
            self._count("chunk:sent", sent)
        return sent

    # --- the sweeps: hooks in S3 --------------------------------------------------

    def _sweep_safely(self, kind: str, sweep, loop, query: _QueryConn) -> None:
        """One sweep; a failure is counted and logged, never fatal to the listener."""
        try:
            sweep(loop, query)
        except Exception as exc:
            query.drop()
            self._count(f"sweep:{kind}_errors")
            log.warning("call bridge: %s sweep failed: %s: %s", kind, type(exc).__name__, exc)

    def _recovery_sweep(self, loop, query: _QueryConn) -> None:
        """On every (re)LISTEN, BEFORE listening is reported: redelivery (4.8) for every peer
        with a socket here, whose frames go to all of that peer's sockets. A pointer
        committed while the LISTEN connection was down was never heard; this recovers the
        target's half. The caller's half is `_chunk_recovery` (S7) and `_terminal_recheck`
        (S6), run after it."""
        local = self._on_loop(loop, self._sweep_scope("recovery"))
        if not local:
            return
        offers = self._redeliver_rows(query.get(), local, "recovery")
        self._on_loop(loop, self._offer(offers, "recovery"))

    def _reoffer_sweep(self, loop, query: _QueryConn, *, periodic: bool = True,
                       urgent=None) -> None:
        """Q17, critique F8. PERIODIC, every `reoffer_secs`: `routed` calls of peers with a
        socket here, OFFERABLE, that no target event has answered, routed longer than
        `reoffer_age_secs` ago, at most `offer_limit`, offered again. A LOSS PASS
        (`periodic=False`, after `request_reoffer`): the same, over the peers whose queue lost
        frames ONLY, with no age floor (S5 review F1). A periodic sweep also covers those
        peers without the floor. `urgent` is the lossy peers the listener took for this pass,
        shared with `_terminal_repush` (F04); None takes them here."""
        if urgent is None:
            with self._urgent_lock:
                urgent, self._urgent = self._urgent, set()
        if not periodic and not urgent:
            return
        local = self._on_loop(loop, self._sweep_scope("reoffer" if periodic else "reoffer_loss"))
        urgent = [p for p in urgent if p in local]
        peers = local if periodic else urgent
        if not peers:
            return
        conn = query.get()
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute(_REOFFER, {"local": list(peers),
                                       "urgent": urgent,
                                       "authority": _authority(),
                                       "age": float(self.reoffer_age_secs),
                                       "limit": self.offer_limit})
                offers = [_Offer(*row) for row in cur.fetchall()]
        self._on_loop(loop, self._offer(offers, "reoffer"))

    def _terminal_repush(self, loop, query: _QueryConn, *, periodic: bool, urgent) -> None:
        """F04 (module docstring, LOST TERMINALS): the terminal frames local sockets' queues
        lost, pushed to those sockets again. On a loss pass, the urgent peers' sockets only; on
        the periodic sweep, every socket's (a throttled loss's names wait for it). Counted as
        `terminal_repush:sent`, and `:socket_gone`, `:not_terminal`, `:not_caller` and
        `:already_delivered` for what is not sent."""
        taken = self._take_lost_terminals(periodic, urgent)
        if not taken:
            return
        live = self._on_loop(loop, self._live_sockets(taken))
        if not live:
            return
        try:
            rows = self._terminal_rows(query.get(), sorted(
                {call_id for _peer, _conn, call_ids in live for call_id in call_ids}))
        except Exception:
            for peer_id, connection_id, call_ids in live:
                self._hold_lost_terminals(peer_id, connection_id, call_ids)
            raise
        by_id = {row["call_id"]: row for row in rows}
        items = []
        for peer_id, connection_id, call_ids in live:
            for call_id in call_ids:
                row = by_id.get(call_id)
                frame = caller_frames.terminal_frame(row) if row is not None else None
                if frame is None:
                    self._count("terminal_repush:not_terminal")
                elif row["caller_peer_id"] != peer_id:
                    # 5.1: a terminal goes to the caller's sockets only. A queue holds only
                    # frames the hub built for its own socket, so this is a guard, not a path.
                    self._count("terminal_repush:not_caller")
                else:
                    items.append((peer_id, connection_id, call_id, frame))
        if items:
            self._on_loop(loop, self._repush_terminals(items))

    async def _live_sockets(self, taken: list) -> list:
        """On the loop: the taken entries whose socket is still registered here and not
        closed; the names of the others are counted `terminal_repush:socket_gone` and let go
        (their callers recover by GET /v1/calls/{id} or, reconnected, a subscribe)."""
        live = []
        for peer_id, connection_id, call_ids in taken:
            handle = await self.manager.connection(peer_id, connection_id)
            if handle is None or handle.closed:
                self._count("terminal_repush:socket_gone", len(call_ids))
            else:
                live.append((peer_id, connection_id, call_ids))
        return live

    async def _repush_terminals(self, items: list) -> int:
        """On the loop, in one turn: each (peer, socket, call, frame) pushed to that socket,
        still registered and open, unless its queue has a copy of the call's terminal queued or
        taken by the writer (`SendQueue.has_terminal`), which is counted
        `terminal_repush:already_delivered`: a caller is not told twice."""
        sent = 0
        for peer_id, connection_id, call_id, frame in items:
            handle = await self.manager.connection(peer_id, connection_id)
            if handle is None or handle.closed:
                self._count("terminal_repush:socket_gone")
                continue
            if handle.queue.has_terminal(call_id):
                self._count("terminal_repush:already_delivered")
                continue
            await handle.queue.push(frame)
            sent += 1
        if sent:
            self._count("terminal_repush:sent", sent)
            log.info("call bridge: re-pushed %d lost terminal frame(s)", sent)
        return sent

    def _chunk_recovery(self, loop, query: _QueryConn) -> None:
        """041 S7, design 4.6 part b, on every (re)LISTEN before the terminal re-check: for each
        call with a proven registration here (read on the loop, with the lowest high-water mark
        among its registrations), the chunks past that mark, at most the newest 200
        (`apps/calls/chunks.replay`), each registration then sent the ones past ITS mark.
        Counted as `recovery_chunk:sent`. One keyed range read per such call."""
        floors = self._on_loop(loop, self.registry.chunk_floors())
        if not floors:
            return
        conn = query.get()
        for call_id, floor in floors.items():
            items = []
            for row in call_chunks.replay(conn, call_id, after=floor):
                frame = caller_frames.chunk_frame(call_id, row)
                if frame is None:
                    self._count("recovery_chunk:unbuildable")
                    continue
                items.append((row["sequence"], frame))
            if items:
                self._on_loop(loop, self._deliver_chunk_replay(call_id, items))

    async def _deliver_chunk_replay(self, call_id: str, items: list) -> int:
        """On the loop, in one turn: CallRegistry.deliver_chunks_after, counted there."""
        sent = await self.registry.deliver_chunks_after(call_id, items)
        if sent:
            self._count("recovery_chunk:sent", sent)
        return sent

    def _terminal_recheck(self, loop, query: _QueryConn, *, kind: str) -> None:
        """041 S6, design 4.6: every call registered here (read on the loop) re-read by its
        call_id, in batches of RECHECK_BATCH, and each terminal one's frame delivered through
        the one mapping, as the terminal arm delivers it. Counted as `<kind>_terminal:sent`."""
        ids = self._on_loop(loop, self.registry.call_ids())
        if not ids:
            return
        rows = self._terminal_rows(query.get(), ids)
        if rows:
            frames = [(r["call_id"], caller_frames.terminal_frame(r)) for r in rows]
            self._on_loop(loop, self._deliver_terminals(frames, f"{kind}_terminal"))

    @staticmethod
    def _terminal_rows(conn, ids: list[str]) -> list[dict]:
        """The TERMINAL rows among `ids`, each by its unique call_id, in batches of
        RECHECK_BATCH (rule B5): the terminal re-check's read, and the lost-terminal re-push's."""
        cols = ", ".join(reads.CALL_COLUMNS)
        rows = []
        for start in range(0, len(ids), RECHECK_BATCH):
            with rls.server_internal(conn):
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {cols} FROM skill_calls"
                                " WHERE call_id = ANY(%s::text[]) AND status = ANY(%s::text[])",
                                (ids[start:start + RECHECK_BATCH],
                                 sorted(terminal.TERMINAL_STATUSES)))
                    rows.extend(dict(zip(reads.CALL_COLUMNS, r)) for r in cur.fetchall())
        return rows

    async def _deliver_terminals(self, frames: list, kind: str) -> int:
        """On the loop, in one turn: each (call_id, frame) to the call's PROVEN registrations,
        each then removed (CallRegistry.deliver_terminal). A None frame (the row is not
        terminal) sends nothing and is counted `<kind>:not_terminal`."""
        sent = 0
        for call_id, frame in frames:
            if frame is None:
                self._count(f"{kind}:not_terminal")
                continue
            sent += await self.registry.deliver_terminal(call_id, frame)
        if sent:
            self._count(f"{kind}:sent", sent)
        return sent

    async def _sweep_scope(self, kind: str) -> list[uuid.UUID]:
        """The peers a sweep would select for, read on the loop; the sweep counted there."""
        self._count(f"sweep:{kind}")
        return await self.manager.connected_peers()

    # --- the presence beat ------------------------------------------------------------

    def _beat_safely(self, loop, query: _QueryConn, worker_token: uuid.UUID) -> None:
        try:
            self._beat(loop, query, worker_token)
        except Exception as exc:
            query.drop()
            self._count("presence:beat_errors")
            log.warning("call bridge: presence beat failed: %s: %s", type(exc).__name__, exc)

    def _beat(self, loop, query: _QueryConn, worker_token: uuid.UUID) -> None:
        """UPSERT a row for every socket this worker holds, read THROUGH THE LOOP, delete the
        row of every departed socket, then prune every worker's rows nobody has seen for
        `prune_after_secs`."""
        with self._presence_lock:
            rows = self._on_loop(loop, self.manager.sockets())
            conn = query.get()
            try:
                written = hub_presence.upsert_beat(conn, rows, worker_token)
            finally:
                # AFTER the write, committed or not: a socket whose detach could not wait for
                # this beat may be in `rows`, and the write may have brought its row back.
                self._drain_departed(conn)
        pruned = hub_presence.prune(query.get(), self.prune_after_secs)
        self._count("presence:beats")
        self._count("presence:beat_rows", written)
        self._count("presence:pruned", pruned)

    def _drain_departed(self, conn) -> None:
        """Delete the row of every departed socket on the beat's connection, and forget each
        one deleted. Under the presence lock, after the beat's write (S3 review F1). A departed
        socket is deregistered, so no later snapshot holds it: once deleted here it is done.
        Never raises; one not deleted stays for the next beat."""
        with self._departed_lock:
            departed = list(self._departed)
        if not departed:
            return
        done = []
        try:
            for connection_id in departed:
                hub_presence.delete(conn, connection_id)
                done.append(connection_id)
        except Exception as exc:
            self._count("presence:departed_errors")
            log.warning("call bridge: departed rows not deleted: %s: %s",
                        type(exc).__name__, exc)
        finally:
            with self._departed_lock:
                self._departed.difference_update(done)
            self._count("presence:departed_deleted", len(done))


#: The production bridge, bound to the registries the default consumer registers into.
CALL_BRIDGE = CallBridge(MANAGER, registry=CALL_REGISTRY)
