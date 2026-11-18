"""context_upload.py - one seat's transcripts into shared/transcript-context, one pass (ingest plan steps 3 and 5).

The driver the parts below were built for (docs/design/context-v1.1-transcript-ingest.md, step 3; kxxx owns it as N1,
a reviewer's per-box postbox worker wraps it as N3, msg kxxx-37254c):
  context_parser  turns transcript records into context.v1 items and manifests;
  screened_outbox validates them and freezes the exact bytes with the parser checkpoint (frozen_outbox, one local
                  revision = one wire batch);
  memory.write    sends each frozen record as this seat (tools/meshy_node, signed invoke);
  authenticated_readback reads it back through a signed memory.read and only an authenticated equal value advances.

ONE RUN IS ONE PASS. It enrolls only the --source files it is given (it never discovers files), sends what earlier
passes froze, freezes what is new, sends that, and exits. Looping and pacing are the caller's.

WHAT A PASS GUARANTEES
  * Frozen bytes never change. A crash anywhere leaves either nothing committed or a committed batch whose send is
    reconciled by a read on the next pass (frozen_outbox.begin_send marks the attempt BEFORE the transport runs).
  * Batches go in order: batch n+1's records are not sent until batch n's manifest has read back equal. That holds
    per LANE: a file with more than TAIL_THRESHOLD_BYTES uncommitted is cut at a line start in its last
    TAIL_WINDOW_BYTES, its tail lane (its own wire src, from the cut on) is sent first, and the file's own lane fills
    in up to the cut and stops there, so no line is uploaded by both (TailEnrollment).
  * A line is never split across two batches. A line whose items would spill into the next batch is fed again after
    the cut, so the checkpoint frozen with a batch always covers exactly that batch's items.
  * A partial batch is frozen only once its first item is MAX_BATCH_AGE_S old or the file has been idle that long, so a
    live session does not make a batch per pass (the plan's 5-minute maximum batch age).
  * A refusal the outbox cannot turn into an omission (any refusal of this unscreened namespace) leaves its batch
    pending, never skipped, and is reported as `context_held` with its age; nothing after it is sent.
  * A source whose identity changed under its path, or that another seat enrolled, is refused (exit 4). A source
    rewritten or truncated in place opens the next generation (gen+1); the old generation's records stay readable.

NOT HERE: subagent artifacts (a subagent source needs finish(), which waits on the worker's idle rule), tool_exchange
and thinking items (F3, parser work), receipt pruning, the per-box round-robin and the 2/s and 8/s budgets (N3).

The JSON status carries no transcript content and no paths: sources are named by their wire src id only.
The namespace stores secrets as written (operator, 24 Oct). Never paste what this uploads past the outbound guards.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from context_parser import (CONTEXT_ADAPTER_VERSION, DEFAULT_BATCH_ITEMS, DIALECTS,  # noqa: E402
                            ContextParser, PlanStateFull, item_key, manifest_key, manifests, serialize)
from frozen_outbox import FrozenOutbox, Record  # noqa: E402
from incremental_store import Limits, Store, StoreBusy, StoreConflict, StoreError, StoreFull, StoreIOError  # noqa: E402
from screened_outbox import AdmissionError, freeze_screened  # noqa: E402

NAMESPACE = "shared/transcript-context"
#: The store's parser identity is the ADAPTER version only: the adapter defines the bytes an item becomes, and the
#: checkpoint format (context_parser.STATE_VERSION) is the parser's own business. Keyed on the state version too, every
#: STATE_VERSION bump (#421 takes it from 6 to 7) would make every enrolled source an enrollment mismatch, a conflict
#: that quarantines the seat. A checkpoint the parser cannot read is refused below with a named code instead.
PARSER_ID = "context-parser-%s" % CONTEXT_ADAPTER_VERSION
MAX_BATCH_AGE_S = 300
PREFIX_BYTES = 4096
#: Tail first (TailEnrollment): a file with more than TAIL_THRESHOLD_BYTES not yet committed is cut at the first line
#: start in its last TAIL_WINDOW_BYTES, and the bytes from the cut go first. On a 102.6 MiB HQB log the last 8 MiB was
#: the last 23 hours (a reviewer 3c6e04). CUT_FINGERPRINT_BYTES before the cut are checked unchanged on every pass.
TAIL_THRESHOLD_BYTES = 8 * 1024 * 1024
TAIL_WINDOW_BYTES = 8 * 1024 * 1024
CUT_FINGERPRINT_BYTES = 4096
#: How far into a file the cut decision reads to learn its src when its own lane has frozen nothing yet.
CUT_IDENTITY_BYTES = 1024 * 1024
#: The dialects a file may be cut in. Claude and Codex carry nothing across records that the tail cannot rebuild at the
#: cut (the plan is seeded from the last plan record before it, _plan_at_cut). Antigravity's rewind guard (ag_hwm) is
#: the maximum over every record before the cut, and its subagent flag is decided at step 0 with the session named by
#: the path, so a tail would need the whole head parsed first (a reviewer 246a0d P1). Kimi takes its identity from the
#: path. Their seats were not the backlogged ones on 1 Nov; they keep one lane.
TAIL_DIALECTS = ("claude", "codex")
#: Records that can carry a plan (context_parser: Codex update_plan, Claude TodoWrite). Only a candidate: the parser
#: decides whether it was one.
PLAN_MARKERS = (b'"update_plan"', b'"TodoWrite"')
#: A line longer than this is never assembled by the backwards read (_lines_backwards): a plan record is a few KiB, and
#: a multi-MiB tool output joined piece by piece would cost memory for nothing (a reviewer c18b54 P2-2).
PLAN_LINE_MAX = 4 * 1024 * 1024
#: One store per transcript file. Every read-back keeps its signed proof (up to 512 KiB) until receipt pruning
#: exists, so the store's own 64 MiB default would fill after a few thousand items. A frozen batch is up to 256
#: items and a manifest of up to 64 KiB each, base64 inside a JSON envelope: about 22.5 MiB, past the store's 8 MiB
#: output default, which wedged a valid full batch for good (GitHub Codex r4381639373). 32 MiB holds the worst case.
#: The checkpoint carries the parser's plan table: 256 steps of up to 1,000 characters, each up to 6 bytes once JSON
#: escapes them, so about 1.6 MiB with the final report, past 512 KiB (GitHub Codex r4382734763); 4 MiB holds it.
#: Batches are never pruned yet, so the store keeps every batch a file ever froze: the wire's 6-digit batch number,
#: not the store's 4,096 default, bounds them (a reviewer 5063f5), and its 4-digit generation bounds the sources.
LIMITS = Limits(checkpoint_bytes=4 * 1024 * 1024, database_bytes=1024 * 1024 * 1024, output_bytes=32 * 1024 * 1024,
                sources=9_999, batches=999_999)

EXIT_OK, EXIT_USAGE, EXIT_HELD, EXIT_CONFLICT, EXIT_UNREACHABLE = 0, 2, 3, 4, 5


class SourceConflict(Exception):
    """Needs review: the source's identity changed, another seat holds it, or a read-back differed."""


class Unreachable(Exception):
    """The server or the local store could not answer now; nothing was lost, try the next pass."""


@dataclass
class Report:
    seat: str
    batches_committed: int = 0
    items_committed: int = 0
    last_manifest_key: str | None = None
    context_held: list = field(default_factory=list)
    emitted: int = 0
    excluded: dict = field(default_factory=dict)
    records_emitted: int = 0
    records_pending: int = 0
    pending: int = 0
    failed: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    unreachable: bool = False
    unreachable_reason: str | None = None   # a fixed code, never server or transcript text
    f3_deferred: list = field(default_factory=list)   # src of sources resumed text-only under --f3 (enrolled before)
    visited: list = field(default_factory=list)   # [{"source_index": i, "batches": budget spent}], in visit order
    held_this_pass: bool = False

    def status(self):
        if self.conflicts:
            return "conflict"
        if self.unreachable:
            return "unreachable"
        if self.held_this_pass:
            return "held"
        # A pass that committed any item made progress, even with its manifest still to go (antigravity F1),
        # and even while an unvisited source carries a persisted hold on disk (surfaced in context_held).
        if self.batches_committed or self.items_committed or self.emitted:
            return "progressed"
        if self.context_held:
            return "held"
        return "drained"

    def exit_code(self):
        return {"conflict": EXIT_CONFLICT, "unreachable": EXIT_UNREACHABLE,
                "held": EXIT_HELD}.get(self.status(), EXIT_OK)

    def as_json(self):
        return {"status": self.status(), "seat": self.seat, "passes": 1,
                "batches_committed": self.batches_committed, "items_committed": self.items_committed,
                "last_manifest_key": self.last_manifest_key, "context_held": self.context_held,
                "accounting": {"emitted": self.emitted, "excluded": dict(sorted(self.excluded.items())),
                               "pending": self.pending, "failed": dict(sorted(self.failed.items())),
                               "records": {"emitted": self.records_emitted, "excluded": sum(self.excluded.values()),
                                           "pending": self.records_pending}},
                "conflicts": self.conflicts, "reason": self.unreachable_reason, "f3_deferred": self.f3_deferred,
                "visited": self.visited}


# ---- the transport: memory.write and the authenticated read-back, as this seat ----------------------------------

class NodeTransport:
    """The real server, through tools/meshy_node with this seat's own state file."""

    def __init__(self, state_file, base_url, seat):
        from tools.meshy_node import node
        self._node = node
        self.path = Path(state_file)
        self.state = node.load_state(self.path, base_url)
        node.require_peer(self.path, self.state)   # a keygen-only state is a usage fault up front (antigravity F7)
        self.seat = seat

    def write(self, key, value):
        node = self._node
        try:
            return node.invoke(self.path, self.state, skill="memory.write",
                               args={"namespace": NAMESPACE, "key": key, "value": value}, poll_timeout=40.0)
        except node.ServerSaid as exc:
            # 429 is the per-peer call bucket, shared with this seat's mail: the caller paces (see the README note).
            raise Unreachable("rate_limited" if exc.status == 429 else "server_said_%d" % exc.status) from None
        except (OSError, node.Refused) as exc:
            raise Unreachable(type(exc).__name__) from None

    def reconcile(self, outbox, batch_key):
        from authenticated_readback import ReadbackPending, reconcile_present
        try:
            return reconcile_present(outbox, batch_key, state_path=self.path, state=self.state, seat=self.seat,
                                     writer_peer_id=self.state["peer_id"]).state
        except ReadbackPending as exc:
            raise Unreachable(str(exc)) from None


def refusal_code(result):
    """The typed code of a write the server definitely refused, else None. A timeout or an untyped failure is not a
    refusal (the write may have landed), and E_USE_MEMORY_EDIT means the key already holds version 1 (divergence
    059): the read-back decides both."""
    if (isinstance(result, dict) and result.get("status") == "failed"
            and result.get("result_signature_verified") is True
            and isinstance(result.get("error_code"), str) and result["error_code"]
            and result["error_code"] != "E_USE_MEMORY_EDIT"):
        return result["error_code"]
    return None


# ---- enrollment: one directory per file identity --------------------------------------------------------------

def _sha(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(json.dumps(value, sort_keys=True).encode("utf-8"))
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def _identity(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SourceConflict("not a regular file")
    return info


def _prefix_sha(path, length, start=0):
    with open(path, "rb") as f:
        f.seek(start)
        return hashlib.sha256(f.read(length)).hexdigest()


def _cut_sha(path, cut):
    """sha256 of the CUT_FINGERPRINT_BYTES just before `cut`, or None when the file no longer reaches the cut."""
    start = max(0, cut - CUT_FINGERPRINT_BYTES)
    with open(path, "rb") as f:
        f.seek(start)
        b = f.read(cut - start)
    return hashlib.sha256(b).hexdigest() if len(b) == cut - start else None


def _lines_backwards(path, end, max_line=None):
    """The complete lines before `end`, newest first, read in chunks from `end` back to the start of the file. A line
    is assembled from its pieces once (linear in its length, a reviewer c18b54 P2-2), and one longer than `max_line`
    (PLAN_LINE_MAX) is skipped, never assembled."""
    max_line = PLAN_LINE_MAX if max_line is None else max_line
    with open(path, "rb") as f:
        pieces, held, over = [], 0, False           # the line being assembled: its pieces newest first
        while end > 0:
            start = max(0, end - _Stream.CHUNK)
            f.seek(start)
            parts = f.read(end - start).split(b"\n")
            for i in range(len(parts) - 1, -1, -1):
                part = parts[i]
                if not over:
                    held += len(part)
                    if held > max_line:
                        over, pieces = True, []
                    else:
                        pieces.append(part)
                if i == 0 and start > 0:
                    break                                # the line goes on into the chunk before
                if not over:
                    line = b"".join(reversed(pieces))
                    if line.strip():
                        yield line
                pieces, held, over = [], 0, False
            end = start


def _line_start_at_or_after(path, pos, size):
    """The first offset c >= pos that starts a line (c == 0 or the byte before it is a newline) with bytes after it,
    or None. Read in chunks, never the whole file."""
    if pos <= 0:
        return 0 if size > 0 else None
    with open(path, "rb") as f:
        f.seek(pos - 1)
        at = pos - 1
        while at < size:
            b = f.read(min(_Stream.CHUNK, size - at))
            if not b:
                return None
            i = b.find(b"\n")
            if i >= 0:
                cut = at + i + 1
                return cut if cut < size else None
            at += len(b)
    return None


class _LaneState:
    """One lane's local progress: the last batch read back complete, the settled-tail accounting and its holds. The
    file's own lane is Enrollment; a backlogged file's tail lane is TailEnrollment beside it in the same directory."""

    HELD = "held.json"

    @property
    def gen(self):
        return self.record["gen"]

    def accounted_through(self):
        return self.record.get("accounted_through", 0)

    def mark_accounted(self, offset, tail_from, tail_sha256):
        """Records up to `offset` have been counted (GitHub Codex r4375729665: an item-less tail is never committed by
        a batch, so it is settled once the file is idle). The bytes from `tail_from` (the committed offset then) to
        `offset` are fingerprinted, so a rewrite or truncation of that uncommitted tail un-settles it (antigravity
        0a5c6b, Codex r4376237786). Local accounting only; the store is unchanged."""
        if offset > self.accounted_through() or (offset, tail_from) != (
                self.accounted_through(), self.record.get("accounted_from")):
            self.record.update(accounted_through=offset, accounted_from=tail_from, accounted_tail_sha256=tail_sha256)
            _write_json(self.file, self.record)

    def tail_settled(self, committed, size, sha_through):
        """The settled offset to trust this pass: the stored one only while the uncommitted tail it covers is the
        same bytes, else the committed offset. `sha_through(offset)` is the sha256 of the file's bytes from
        `committed` to `offset` as this pass read them, or None if they cannot be had; `sha_through` is None when
        nothing was read."""
        done = self.accounted_through()
        if done <= committed:
            return done
        start = self.record.get("accounted_from")
        if size < done or start != committed or sha_through is None or (
                sha_through(done) != self.record.get("accounted_tail_sha256")):
            self.record.update(accounted_through=committed, accounted_from=committed,
                               accounted_tail_sha256=EMPTY_SHA256)
            _write_json(self.file, self.record)
            return committed
        return done

    def mark_complete(self, revision):
        if revision > self.record["complete_through"]:
            self.record["complete_through"] = revision
            _write_json(self.file, self.record)

    def held(self):
        return _read_json(self.dir / self.HELD, {})

    def set_held(self, held):
        _write_json(self.dir / self.HELD, held)


class Enrollment(_LaneState):
    """The file's directory in the store: who enrolled it, its identity and its current generation. The prefix that
    detects a rewrite lives in the store's checkpoint, committed with the offset it covers (GitHub Codex r4375282525).
    A backlogged file's cut (TailEnrollment) is recorded here too, once per generation: `cut` is the line start the
    file's own lane stops at and its tail lane starts from, `cut_sha256` fingerprints the bytes just before it, and
    `cut_src` is the tail lane's wire src."""

    def __init__(self, store_dir, seat, dialect, path):
        self.root = Path(store_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        info = _identity(path)
        self.path, self.seat, self.dialect, self.info = Path(path), seat, dialect, info
        registry_file = self.root / "paths.json"
        registry = _read_json(registry_file, {})
        here = str(self.path.resolve())
        ident = [info.st_dev, info.st_ino]
        known = registry.get(here)
        if known is not None and known != ident:
            raise SourceConflict("the file under this path changed identity")
        if known is None:
            registry[here] = ident
            _write_json(registry_file, registry)
        self.dir = self.root / _sha("file", info.st_dev, info.st_ino)[:32]
        self.dir.mkdir(exist_ok=True)
        self.file = self.dir / "enrollment.json"
        record = _read_json(self.file, None)
        if record is None:
            record = {"v": 2, "seat": seat, "dialect": dialect, "gen": 1, "complete_through": 0}
            _write_json(self.file, record)
        if record["seat"] != seat:
            raise SourceConflict("another seat enrolled this file")
        if record["dialect"] != dialect:
            raise SourceConflict("this file was enrolled under another dialect")
        self.record = record

    def ids(self, gen=None):
        gen = self.gen if gen is None else gen
        source = _sha("source", self.info.st_dev, self.info.st_ino, gen)
        return source, _sha("generation", source, gen)

    @property
    def cut(self):
        return self.record.get("cut")

    def set_cut(self, cut, cut_sha256, cut_src):
        self.record.update(cut=cut, cut_sha256=cut_sha256, cut_src=cut_src, tail_src=cut_src)
        _write_json(self.file, self.record)

    def next_generation(self):
        # `tail_src` stays: the file's tail lane keeps ONE src for every generation, so the next generation's tail
        # supersedes the last one's in search (ctxindex sql_latest serves the newest gen per (seat, src)), and a file
        # that had a tail always gets one again (SourcePass.plan_tail). GitHub Codex r4435554091, a reviewer 246a0d P2.
        self.record.update(gen=self.gen + 1, complete_through=0, accounted_through=0, accounted_from=0,
                           accounted_tail_sha256=EMPTY_SHA256, cut=None, cut_sha256=None, cut_src=None)
        _write_json(self.file, self.record)


class TailEnrollment(_LaneState):
    """A backlogged file's TAIL lane: the bytes from the cut on, sent before the file's own lane fills in what lies
    before the cut, so the newest work is searchable first (HQB, 1 Nov: committed batches stood at 21-27 Oct while
    the work asked about was today's). It is a source of its own in the same store, with its own wire src
    (Enrollment.cut_src), because the server orders manifests per (seat, src, gen) and search serves only a source's
    newest gen (apps/context/validation.py _enforce_manifest, apps/meshsem/ctxindex.py sql_latest). Its state is
    keyed to the file's generation and cut."""

    HELD = "held-tail.json"

    def __init__(self, head):
        self.head = head
        self.root, self.path, self.seat, self.dialect = head.root, head.path, head.seat, head.dialect
        self.info, self.dir = head.info, head.dir
        self.file = self.dir / "enrollment-tail.json"
        record = _read_json(self.file, None)
        if record is None or record.get("gen") != head.gen or record.get("cut") != head.cut:
            record = {"v": 1, "gen": head.gen, "cut": head.cut, "complete_through": 0}
            _write_json(self.file, record)
            if (self.dir / self.HELD).exists():
                _write_json(self.dir / self.HELD, {})
        self.record = record

    @property
    def gen(self):
        return self.head.gen

    def ids(self, gen=None):
        gen = self.gen if gen is None else gen
        source = _sha("source-tail", self.info.st_dev, self.info.st_ino, gen, self.head.cut)
        return source, _sha("generation", source, gen)


# ---- one source -------------------------------------------------------------------------------------------------

def _age_s(item_time, now):
    try:
        then = datetime.fromisoformat(item_time.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (now - then).total_seconds()
    except (AttributeError, ValueError):
        return float("inf")          # an unreadable time never holds a batch back


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _checkpoint(parser_state, prefix_len, prefix_sha256):
    return {"parser": parser_state, "prefix_len": prefix_len, "prefix_sha256": prefix_sha256}


def _rewritten(path, size, snapshot, start=0):
    """Truncated below what was committed, or its first committed bytes are not the ones this generation froze. The
    prefix covers min(PREFIX_BYTES, committed offset), grown at each freeze in the same commit as the offset, so a file
    enrolled empty or small is still checked once it has bytes (a reviewer f39579 F3b), and no crash can leave the
    offset ahead of its prefix. It is the lane's own prefix: the bytes from `start` (a tail lane's cut, antigravity
    c18b54 P2-1), so an edit inside what the tail committed is seen as the file's own lane sees one in its first bytes."""
    cp = snapshot.parser_state()
    if size < snapshot.offset or size < start + cp["prefix_len"]:
        return True
    return _prefix_sha(path, cp["prefix_len"], start) != cp["prefix_sha256"]


def _records(batch_items):
    items = tuple(Record(item_key(i), serialize(i)) for i in batch_items)
    manifest = manifests(batch_items)[0]
    return items, Record(manifest_key(manifest), serialize(manifest))


@dataclass
class _At:
    """Where a flush-only pass commits: the committed offset, as no new line was read (SourcePass._flush_waiting)."""
    pos: int


class SourcePass:
    """One lane of one file, one pass. `head` is None for the file's own lane (from offset 0); given, this is that
    file's tail lane (from head's cut on), which run_pass runs first."""

    def __init__(self, cfg, path, transport, report, now, budget, head=None):
        self.cfg, self.transport, self.report, self.now, self.budget = cfg, transport, report, now, budget
        self.tail = None                 # the file's own lane: its tail lane's pass, when the file has a cut
        if head is None:
            self.lane, self.start = "head", 0
            self.enrollment = Enrollment(cfg.store_dir, cfg.seat, cfg.dialect, path)
        else:
            self.lane, self.start = "tail", head.enrollment.cut
            self.enrollment = TailEnrollment(head.enrollment)
        self.store = Store(self.enrollment.dir / "store", LIMITS)
        self.outbox = FrozenOutbox(self.store)
        self.outbox.prune_completed_evidence()
        self.snapshot = self._enroll()
        self._stream = None              # the file capture is reading; its head is the prefix read this pass
        self.rewritten = bool(self.snapshot.revision) and _rewritten(self.enrollment.path,
                                                                     self.enrollment.info.st_size, self.snapshot,
                                                                     self.start)

    def _committed(self):
        """Where this lane's next record starts: its committed offset, never before its lane's start."""
        return max(self.snapshot.offset, self.start)

    def _end(self):
        """Where this lane stops reading: the file's size, or for a file's own lane with a cut, the cut."""
        size = self.enrollment.info.st_size
        cut = self.enrollment.cut if self.lane == "head" else None
        return size if cut is None else min(size, cut)

    def plan_tail(self):
        """The file's own lane decides its cut, once per generation; or checks the recorded one still holds. The
        bytes just before the cut changed, or the file no longer reaches it: the file was rewritten, and both lanes
        drain and then the next generation opens (run). No cut for a subagent source (one final report at finish,
        a reviewer 3c6e04), for a source whose identity is not settled yet, outside TAIL_DIALECTS, or with the window
        off. A file whose earlier generation had a tail is cut whatever its backlog, so that tail is superseded."""
        e, size = self.enrollment, self.enrollment.info.st_size
        if e.cut is not None:
            if size < e.cut or _cut_sha(e.path, e.cut) != e.record.get("cut_sha256"):
                self.rewritten = True
            return
        window, threshold = self.cfg.tail_window_bytes, self.cfg.tail_threshold_bytes
        if self.rewritten or not window or self.cfg.dialect not in TAIL_DIALECTS:
            return
        committed = self.snapshot.offset
        again = e.record.get("tail_src") is not None
        if not again and size - committed <= threshold:
            return
        src, subagent, quarantined, first_item_end = self._identity_for_cut()
        if not isinstance(src, str) or subagent or quarantined:
            return
        # Never before what this lane already counted: an item-less tail it settled past its committed offset is
        # counted, and a cut inside it would count it again in the tail (a reviewer 246a0d P2 #3).
        # And never before the end of the file's first item, so this lane makes at least one item in this
        # generation: a lane that makes none never writes the generation, and its last one stays current in search
        # (a reviewer c18b54 P1-1, a cut after a lone session_meta).
        floor = max(committed, e.accounted_through(), size - window, first_item_end)
        cut = _line_start_at_or_after(e.path, floor, size)
        # A cut AT the committed offset is a real split once this lane has committed something: it ends where it
        # is, and the tail takes what follows. After a rewrite to a short file this lane can drain to the end, and a
        # later small growth then lands its cut exactly there (a reviewer df63b1 P3-1). Only a cut at 0 is empty.
        if cut is None or cut == 0 or cut < committed:
            return
        tail_src = e.record.get("tail_src") or _sha("tail", src)[:32]
        if again and not self._tail_makes_an_item(cut, size, tail_src):
            # The tail lane would write nothing in this generation, so its last one would stay current: no cut yet.
            # The file's own lane takes what there is, and the cut is decided again on a later pass, once it grows.
            return
        cut_sha = _cut_sha(e.path, cut)
        if cut_sha is None:
            return
        e.set_cut(cut, cut_sha, tail_src)

    def _identity_for_cut(self):
        """(src, subagent, quarantined, first_item_end) of this file: from the checkpoint once a batch froze (the lane
        has made an item in this generation, so first_item_end is 0), else from a scratch parse of its first
        CUT_IDENTITY_BYTES (a Codex rollout names its session only at line 0, a reviewer 3c6e04). The scratch parse
        runs until the parser has made its first item, since only then are src AND subagent settled (a path can name
        the src before the record that makes it a subagent, a reviewer 246a0d P1), or until it is known to be a
        subagent, which is never cut (a reviewer c18b54 P1-2). A file that makes no item inside the bound, a first
        line past it included, is not cut. first_item_end is the offset just past the line that made the first item.
        Nothing the scratch parse makes is kept."""
        try:
            state = self.snapshot.parser_state()["parser"]
        except (ValueError, KeyError, TypeError):
            return None, False, False, 0
        if self.snapshot.revision and isinstance(state.get("src"), str):
            return state["src"], bool(state.get("subagent")), bool(state.get("quarantined")), 0
        scratch = ContextParser(self.cfg.seat, self.cfg.dialect, batch_items=self.cfg.batch_items,
                                gen=self.enrollment.gen, namespace=NAMESPACE,
                                source_path=os.path.abspath(self.enrollment.path), f3=False)
        with open(self.enrollment.path, "rb") as f:
            read = 0
            while scratch.ordinal == 0 and not scratch.quarantined and not scratch.subagent \
                    and read < CUT_IDENTITY_BYTES:
                line = f.readline(CUT_IDENTITY_BYTES - read)
                if not line.endswith(b"\n"):
                    break
                read += len(line)
                if not line.strip():
                    continue
                try:
                    scratch.feed(json.loads(line.decode("utf-8", "replace")))
                except (ValueError, TypeError, AttributeError, PlanStateFull):
                    return None, False, False, 0
        if scratch.ordinal == 0 and not scratch.quarantined and not scratch.subagent:
            return None, False, False, 0
        return scratch.src, bool(scratch.subagent), bool(scratch.quarantined), read

    def _tail_makes_an_item(self, cut, size, tail_src):
        """Whether the bytes from `cut` make at least one item, read as the tail lane would (its src seeded), within
        the window. Only lines are parsed; nothing is kept."""
        probe = ContextParser(self.cfg.seat, self.cfg.dialect, batch_items=self.cfg.batch_items,
                              gen=self.enrollment.gen, namespace=NAMESPACE,
                              source_path=os.path.abspath(self.enrollment.path), f3=False)
        probe.src, probe.subagent = tail_src, False
        with open(self.enrollment.path, "rb") as f:
            f.seek(cut)
            pos, stop = cut, min(size, cut + max(self.cfg.tail_window_bytes, CUT_IDENTITY_BYTES))
            while probe.ordinal == 0 and pos < stop:
                line = f.readline(stop - pos)
                if not line.endswith(b"\n"):
                    return False
                pos += len(line)
                if not line.strip():
                    continue
                try:
                    probe.feed(json.loads(line.decode("utf-8", "replace")))
                except ValueError:
                    continue                             # an unreadable line makes no item, as in the lane itself
                except (TypeError, AttributeError, PlanStateFull):
                    return False
        return probe.ordinal > 0

    def _plan_at_cut(self, tail_src):
        """(plan, oversize) as the parser holds them just before the cut, or None when no plan record precedes it. A
        plan update carries the whole plan and the parser's plan is a function of the latest one alone (context_parser
        _plan_diff), so the newest record before the cut that the parser takes as a plan gives exactly the state one
        lane would have there (GitHub Codex r4435554097: a step added before the cut and completed after it must stay
        a completion). The file is read backwards from the cut and only lines holding a PLAN_MARKERS name are parsed."""
        sentinel = {"\x00not-a-plan": [1, 0, 0]}
        for line in _lines_backwards(self.enrollment.path, self.start):
            if not any(m in line for m in PLAN_MARKERS):
                continue
            try:
                record = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue                                 # not JSON: the lane counts it unreadable and goes on
            probe = ContextParser(self.cfg.seat, self.cfg.dialect, batch_items=self.cfg.batch_items,
                                  gen=self.enrollment.gen, namespace=NAMESPACE,
                                  source_path=os.path.abspath(self.enrollment.path), f3=False)
            probe.src, probe.subagent, probe.plan = tail_src, False, dict(sentinel)
            plan_before = probe.plan
            # A record the parser refuses stops the file in its own lane too (SourcePass._feed), so it fails closed
            # here: never an older plan in its place (a reviewer c18b54 P2-2).
            try:
                probe.feed(record)
            except PlanStateFull:
                raise SourceConflict("plan_state_full") from None
            except (ValueError, TypeError, AttributeError):
                raise SourceConflict("parser_refused") from None
            if probe.plan is not plan_before:                # the parser took it as a plan update
                return probe.plan, probe.oversize
        return None

    def _is_enrolled(self, source, generation):
        try:
            self.store.resume(source, generation, PARSER_ID)
        except StoreConflict:
            return False
        return True

    def _enrolled_f3(self, source, generation):
        """(mode, wire src) for this source generation: the F3 mode it was enrolled with, read from the store's own
        enrolled identity (the authority, also for sources enrolled before this method existed), and the wire src id
        its checkpoint carries (None until its first record is read). (None, None) when it is not enrolled yet."""
        try:
            snap = self.store.resume(source, generation, PARSER_ID)
        except StoreConflict:
            return None, None
        try:
            mode = json.loads(snap.metadata.decode("utf-8")).get("f3") is True
        except (ValueError, AttributeError, UnicodeDecodeError):
            return None, None
        try:
            src = snap.parser_state()["parser"].get("src")
        except (ValueError, KeyError, TypeError, AttributeError):
            src = None
        return mode, src if isinstance(src, str) else None

    def _enroll(self):
        source, generation = self.enrollment.ids()
        # F3 is per source generation: a source keeps the mode it was enrolled with, so one source never mixes F1 and
        # F3 items (txxxx). Switching --f3 ON applies to sources enrolled from then on (new session files, and
        # a rewritten file's next generation); a source already enrolled without it resumes text-only and is listed
        # in f3_deferred, instead of stopping the whole seat with a conflict (HQD, 1 Nov: every pass exited 4).
        # Switching --f3 OFF for a source enrolled WITH it is still a conflict: that stops the source, which is the
        # kill switch for tool output, and is never a silent downgrade.
        enrolled, wire_src = self._enrolled_f3(source, generation) if self.cfg.f3 else (None, None)  # --f3 peeks
        self.f3 = bool(self.cfg.f3) and enrolled is not False
        if self.cfg.f3 and enrolled is False:
            # by its WIRE src and generation, as every other report field names sources (module docstring), so a
            # deferred source joins to its ctx/ and ctxm/ keys and to context_held (pxx on #471, P2-1)
            self.report.f3_deferred.append({"src": wire_src, "gen": self.enrollment.gen})
        fresh = ContextParser(self.cfg.seat, self.cfg.dialect, batch_items=self.cfg.batch_items,
                              gen=self.enrollment.gen, namespace=NAMESPACE,
                              # Antigravity (#421) and Kimi (#425) take a source's identity from its path, not from a
                              # record: without it every record is skipped as no_session_id. The live enrolled file,
                              # never a copy, and absolute: a relative --source would drop the directories the
                              # identity is read from, and enroll gen 1 with no src for good (a reviewer dbdb58).
                              # Resume needs no path: from_state restores src from the checkpoint.
                              source_path=os.path.abspath(self.enrollment.path), f3=self.f3)
        identity = {"seat": self.cfg.seat, "dialect": self.cfg.dialect, "gen": self.enrollment.gen}
        if self.lane == "tail":
            # Its own wire src, seeded before the first record: every src assignment in the parser is `if self.src is
            # None`, so feed() keeps it, and a tail that starts past a Codex session_meta line still has an identity.
            fresh.src, fresh.subagent = self.enrollment.head.record["cut_src"], False
            identity.update(lane="tail", cut=self.start)
            if not self._is_enrolled(source, generation):    # the backwards read runs once, at enrollment
                at_cut = self._plan_at_cut(fresh.src)
                if at_cut is not None:
                    fresh.plan, fresh.oversize = {k: list(v) for k, v in at_cut[0].items()}, dict(at_cut[1])
        if self.f3:
            # F3 is part of what was enrolled. With F3 off the identity is unchanged, so every source enrolled before
            # F3 resumes as it was, and an F3-enrolled source under --f3 off is an enrollment mismatch (a conflict).
            identity["f3"] = True
        return self.store.enroll(source, generation, PARSER_ID, identity, _checkpoint(fresh.state(), 0, EMPTY_SHA256))

    def run(self):
        try:
            if not self.drain():
                return
            if self.rewritten:
                # The old generation's frozen batches are sent first: they are its bytes, not the file's
                # (a reviewer f39579 F5). Only then does the rewritten file open the next generation, and only by its
                # own lane once its tail lane has sent everything too: the next generation clears the cut, and with it
                # the tail lane's ids.
                if self.lane == "tail" or self._unsent() or (self.tail is not None and self.tail._unsent()):
                    return
                self.enrollment.next_generation()
                self.snapshot = self._enroll()
                self.rewritten = False
                # The new generation's cut first: reading it from offset 0 in this pass would send the backlog before
                # its tail (a reviewer 246a0d P2 #2). With a cut, its tail lane runs now, ahead of this lane, as
                # run_pass orders the two on every later pass.
                self.plan_tail()
                if self.enrollment.cut is not None:
                    self.tail = SourcePass(self.cfg, self.enrollment.path, self.transport, self.report, self.now,
                                           self.budget, head=self)
                    self.tail.run()
            self.capture()
            self.drain()
        finally:
            self.report.pending += self._unsent()

    def _unsent(self):
        """Items frozen but not yet read back equal: the batches after the last complete one."""
        source, generation = self.enrollment.ids()
        after, count = self.enrollment.record["complete_through"], 0
        while True:
            page = self.store.batches(source, generation, PARSER_ID, after_revision=after, limit=16)
            for batch, output, _drops in page.items:
                count += len(json.loads(output)["records"]) - 1
                after = batch.revision
            if not page.has_more:
                return count

    # -- sending ---------------------------------------------------------------------------------------------------

    def drain(self):
        """Send and reconcile every frozen batch in order. False when this source must stop for this pass."""
        source, generation = self.enrollment.ids()
        after = self.enrollment.record["complete_through"]
        while True:
            page = self.store.batches(source, generation, PARSER_ID, after_revision=after, limit=16)
            for batch, _output, _drops in page.items:
                if not self._drain_batch(batch.key):
                    return False
                self.enrollment.mark_complete(batch.revision)
                self.outbox.prune_completed_evidence(batch.key)
                after = batch.revision
            if not page.has_more:
                return True

    def _drain_batch(self, key):
        refused = None
        sent = False                     # this pass sent the current record (GitHub Codex r4375282532)
        while True:
            action = self.outbox.next_action(key)
            if action.kind == "complete":
                return True
            if action.kind in ("review_conflict", "review_redacted"):
                self.report.conflicts.append({"src": self._src(action.record), "code": action.kind})
                return False
            if action.kind == "wait_predecessor":            # drained in order, so this is a store fault
                self.report.conflicts.append({"src": None, "code": "wait_predecessor"})
                return False
            if action.kind == "send":
                if refused is not None:                      # refused and read back absent: hold, never skip
                    self._hold(key, action.record, refused)
                    return False
                if self.budget.spent():
                    return False
                attempt = self.outbox.begin_send(key)
                self.budget.spend()
                result = self.transport.write(attempt.record.key, json.loads(attempt.record.value))
                refused = refusal_code(result)
                sent = True
                continue
            if action.kind == "read":
                verdict = self.transport.reconcile(self.outbox, key)
                if verdict == "matched":
                    value = json.loads(action.record.value)
                    if value["kind"] == "manifest":
                        self.report.batches_committed += 1
                        self.report.last_manifest_key = action.record.key
                        self._unhold(key)
                    else:
                        self.report.items_committed += 1
                    refused = None
                    sent = False
                elif verdict == "ready" and sent and refused is None:
                    # Sent this pass with no typed refusal (a timeout, say) and read back absent: it is sent again on
                    # the NEXT pass, never in a loop within this one. A send from an earlier pass that reads back
                    # absent is still sent again now (the crash case).
                    raise Unreachable("uncertain write read back absent")
                elif verdict == "conflict":
                    self.report.conflicts.append({"src": self._src(action.record), "code": "readback_differs"})
                    return False
                continue
            raise StoreConflict("unknown outbox action")

    @staticmethod
    def _src(record):
        return record.key.split("/")[2] if record is not None else None

    def _hold(self, key, record, code):
        value = json.loads(record.value)
        held = self.enrollment.held()
        prev = held.get(key)
        first = prev.get("since", self.now.timestamp()) if isinstance(prev, dict) else self.now.timestamp()
        first_ord = value.get("first", value.get("ordinal"))
        last_ord = value.get("last", value.get("ordinal"))
        held[key] = {
            "since": first,
            "code": code,
            "src": value["src"],
            "gen": value["gen"],
            "batch": value["batch"],
            "first_ord": first_ord,
            "last_ord": last_ord,
        }
        self.enrollment.set_held(held)
        self.report.held_this_pass = True
        self.report.failed[code] = self.report.failed.get(code, 0) + 1
        self.report.context_held.append({
            "src": value["src"], "gen": value["gen"], "batch": value["batch"],
            "first_ord": first_ord, "last_ord": last_ord,
            "code": code, "age_s": max(0, int(self.now.timestamp() - first))})

    def _unhold(self, key):
        held = self.enrollment.held()
        if held.pop(key, None) is not None:
            self.enrollment.set_held(held)

    # -- capturing -------------------------------------------------------------------------------------------------

    def capture(self):
        """Parse what is new since the committed offset and freeze it in whole batches."""
        snap = self.snapshot
        start, size = self._committed(), self._end()
        if size <= start:
            self.enrollment.tail_settled(start, size, None)
            return self._flush_waiting(snap)
        # The file is read a line at a time and only as far as this pass's budget goes (GitHub Codex r4377304927):
        # a backlog is never held in memory whole, whatever its size. Hashes of what was read stand in for the bytes.
        with open(self.enrollment.path, "rb") as f:
            stream = _Stream(f, start, size, self.enrollment.accounted_through(), keep_head=start == 0)
            if not stream.has_line():
                return self._flush_waiting(snap)
            self._stream = stream
            try:
                self._capture(snap, start, size, stream)
            finally:
                self._stream = None

    def _capture(self, snap, start, size, stream):
        try:
            parser = ContextParser.from_state(snap.parser_state()["parser"], namespace=NAMESPACE, f3=self.f3)
        except ValueError:
            # A checkpoint from another parser STATE_VERSION, not migrated by the parser: refused, never
            # reinterpreted (plan step 3 item 3). Frozen batches were already drained before this; review decides.
            raise SourceConflict("parser_state_version") from None
        items = parser.items                                  # every item this pass emitted, in order
        self._frozen_batch = parser.batch - 1                 # the last batch number frozen
        self._lines = []                                      # (line end offset, class) for each record read
        read_to_end = False
        try:
            read_to_end = self._capture_lines(parser, items, stream)
        finally:
            # Items parsed this pass but not frozen are re-parsed next pass from the committed offset.
            self.report.pending += sum(1 for i in items if i["batch"] > self._frozen_batch)
            # Per-record accounting (plan step 3 item 7): a record is counted once, when it SETTLES, as emitted (it
            # made items) or excluded by class. It settles when a batch commits the offset past it, or, for an
            # item-less tail that no batch will ever commit, when the file is idle and nothing parsed waits unfrozen
            # (GitHub Codex r4375729665). Until then it is pending. accounted_through keeps passes from counting a
            # record twice, so passes sum into a daily report.
            # A tail settles only after a pass that read to the end of the data without stopping (a budget, a
            # refused freeze, a parser refusal), so a failed pass never settles anything (a reviewer 0a5c6b P2 #2).
            done = self.enrollment.tail_settled(start, size, stream.sha_through)
            settled = self._committed()
            unfrozen = any(i["batch"] > self._frozen_batch for i in items)
            if read_to_end and self._lines and not unfrozen and self._idle():
                settled = max(settled, stream.pos)       # every complete line read, blank ones included
            settled = max(settled, done)
            for end, cls in self._lines:
                if end <= done:
                    continue
                if end > settled:
                    self.report.records_pending += 1
                elif cls == "emitted":
                    self.report.records_emitted += 1
                else:
                    self.report.excluded[cls] = self.report.excluded.get(cls, 0) + 1
            base = self._committed()
            self.enrollment.mark_accounted(settled, base,
                                           stream.segment_sha(base, settled) if settled > base else EMPTY_SHA256)

    def _capture_lines(self, parser, items, stream):
        while True:
            if self.budget.spent():
                return False
            got = stream.next_line()
            if got is None:
                break
            line, line_start, line_end = got
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                self._lines.append((line_end, "unreadable_line"))
                continue
            before_state, before_count, before_batch = parser.state(), len(items), parser.batch
            was_empty = parser.batch_count == 0
            reasons = self._feed(parser, record)
            if parser.batch == before_batch:
                self._note(line_end, len(items) - before_count, reasons)
                continue
            if parser.batch == before_batch + 1 and parser.batch_count == 0:   # this line filled the batch exactly
                self._note(line_end, len(items) - before_count, reasons)
                if not self._freeze(parser, items, before_batch, line_end):
                    return False
                continue
            # The line spilled into a later batch (by any number of batches): cut BEFORE it and feed it again in a
            # fresh batch (a reviewer f39579 F4: an exact landing two batches on is a spill, not a fill).
            if was_empty:
                raise SourceConflict("one record produced more items than a batch holds")
            del items[before_count:]
            parser = ContextParser.from_state(before_state, namespace=NAMESPACE)
            parser.items = items
            parser.close_batch()
            if not self._freeze(parser, items, before_batch, line_start):
                return False
            reasons = self._feed(parser, record)
            self._note(line_end, len(items) - before_count, reasons)
            if parser.batch == before_batch + 2 and parser.batch_count == 0:
                if not self._freeze(parser, items, before_batch + 1, line_end):
                    return False
            elif parser.batch != before_batch + 1:
                raise SourceConflict("one record produced more items than a batch holds")
        if parser.f3 and self._idle():
            parser = self._flush(parser, items, stream, everything=self._at_cut(stream.pos))
            if parser is None:
                return False
        open_batch = [i for i in items if i["batch"] == parser.batch]
        if open_batch and self._old_enough(open_batch[0]):
            parser.close_batch()
            return self._freeze(parser, items, open_batch[0]["batch"], stream.pos)
        return True

    def _flush_waiting(self, snap):
        """No new complete line, but the checkpoint may hold F3 calls waiting for a result. An idle file still flushes
        the due ones and cuts them by age at the committed offset, so a session that ended with a call open does not
        keep it forever (GitHub Codex on a42dd4f2, context_upload.py:583; txxxx)."""
        if not (self.f3 and self._idle()):
            return
        try:
            parser = ContextParser.from_state(snap.parser_state()["parser"], namespace=NAMESPACE, f3=self.f3)
        except ValueError:
            raise SourceConflict("parser_state_version") from None
        if not parser.due_pending(everything=True):
            return
        items = parser.items
        self._frozen_batch = parser.batch - 1
        parser = self._flush(parser, items, _At(self._committed()), everything=self._at_cut(self._committed()))
        if parser is None:
            return
        open_batch = [i for i in items if i["batch"] == parser.batch]
        if open_batch:
            parser.close_batch()
            self._freeze(parser, items, open_batch[0]["batch"], self._committed())

    def _flush(self, parser, items, stream, everything=False):
        """F3, before an age cut: calls still waiting for a result go out result-missing when they are due (5 minutes
        older than the newest record, or a day older than now). One call at a time, frozen the way a record is: a
        flush that fills the open batch freezes it, and one that would spill past it is cut BEFORE it and flushed
        again into a fresh batch, so every checkpoint holds exactly the items frozen with it (txxxx P1).
        `everything`: the file's own lane at its cut flushes every waiting call, due or not, as parse_file does at
        the end of a whole file: no result can follow before the cut, and the call must not wait a day for its age.
        Returns the parser to carry on with, or None when a freeze stopped the pass."""
        now = self.now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for ref in parser.due_pending(now=now, everything=everything):
            before_state, before_count, before_batch = parser.state(), len(items), parser.batch
            was_empty = parser.batch_count == 0
            parser.flush_pending(now=now, everything=everything, only=ref)
            if parser.batch == before_batch:
                continue
            if parser.batch == before_batch + 1 and parser.batch_count == 0:      # filled the batch exactly
                if not self._freeze(parser, items, before_batch, stream.pos):
                    return None
                continue
            if was_empty:
                raise SourceConflict("one flushed exchange produced more items than a batch holds")
            del items[before_count:]
            parser = ContextParser.from_state(before_state, namespace=NAMESPACE)
            parser.items = items
            parser.close_batch()
            if not self._freeze(parser, items, before_batch, stream.pos):
                return None
            parser.flush_pending(now=now, everything=everything, only=ref)
            if parser.batch == before_batch + 2 and parser.batch_count == 0:
                if not self._freeze(parser, items, before_batch + 1, stream.pos):
                    return None
            elif parser.batch != before_batch + 1:
                raise SourceConflict("one flushed exchange produced more items than a batch holds")
        return parser

    def _feed(self, parser, record):
        """A record the parser refuses stops this source with a fixed code, never its text (antigravity F2)."""
        skipped_before = len(parser.skipped)
        try:
            parser.feed(record)
        except PlanStateFull:
            raise SourceConflict("plan_state_full") from None
        except ValueError:
            raise SourceConflict("parser_refused") from None
        return [reason for reason, _ref in parser.skipped[skipped_before:]]

    def _note(self, line_end, produced, reasons):
        """A record's class: emitted when it made items, else the parser's skip reason, else no_item (a record that
        is not dialogue: a tool call, a result, metadata)."""
        self._lines.append((line_end, "emitted" if produced else (reasons[0] if reasons else "no_item")))

    def _at_cut(self, pos):
        """The file's own lane has read everything before its cut."""
        return self.lane == "head" and self.enrollment.cut is not None and pos >= self.enrollment.cut

    def _idle(self):
        # A file's own lane with a cut never sees bytes past it: what it reads is final, so it is idle by definition
        # and freezes its last batch and flushes its open F3 calls at the cut like an idle file's.
        if self.lane == "head" and self.enrollment.cut is not None:
            return True
        return self.now.timestamp() - self.enrollment.info.st_mtime >= MAX_BATCH_AGE_S

    def _old_enough(self, first_item):
        return self._idle() or _age_s(first_item.get("time"), self.now) >= MAX_BATCH_AGE_S

    def _freeze(self, parser, items, batch_number, safe_offset):
        batch_items = [i for i in items if i["batch"] == batch_number]
        if not batch_items:
            return True
        if self.budget.spent():                               # every freeze is budgeted (antigravity F6)
            return False
        records, manifest = _records(batch_items)
        try:
            freeze_screened(self.outbox, self.snapshot, safe_offset, records, manifest,
                            _checkpoint(parser.state(), *self._prefix(safe_offset)), seat=self.cfg.seat)
        except AdmissionError as exc:
            self.report.conflicts.append({"src": batch_items[0]["src"], "code": str(exc)})
            return False
        self.snapshot = self.store.resume(*self.enrollment.ids(), PARSER_ID)
        if self._stream is not None:
            self._stream.cut(safe_offset)
        self._frozen_batch = batch_number
        self.report.emitted += len(batch_items)
        self.budget.spend()
        return True

    def _prefix(self, safe_offset):
        """(length, sha256) of the lane's first min(PREFIX_BYTES, safe_offset - start) committed bytes: from the bytes
        this pass read from the lane's start when it did, else from the file, whose committed prefix was checked
        unchanged this pass."""
        cp = self.snapshot.parser_state()
        length = min(PREFIX_BYTES, safe_offset - self.start)
        if length <= cp["prefix_len"]:
            return cp["prefix_len"], cp["prefix_sha256"]
        head = self._stream.head if self._stream is not None else None
        if head is not None and len(head) >= length:
            return length, hashlib.sha256(head[:length]).hexdigest()
        return length, _prefix_sha(self.enrollment.path, length, self.start)


class _Stream:
    """Complete lines of a file from `start` up to `size`, read one at a time. It keeps no line it has passed, only
    hashes: of everything from `start` (for the settled-tail check at `watch`) and of the segment since the last
    freeze (for the tail a pass settles). A line not ended by a newline before `size` is never read."""

    CHUNK = 1 << 20

    def __init__(self, f, start, size, watch, keep_head=False):
        self.f, self.start, self.size, self.pos, self.watch = f, start, size, start, watch
        f.seek(start)
        self.from_start = hashlib.sha256()
        self.seg, self.seg_from = hashlib.sha256(), start
        self.at_watch = None                     # (from_start, seg_from, seg) hashes when pos reached watch
        self.last = None                         # (start, bytes) of the line just read
        self.head = b"" if keep_head else None   # the first PREFIX_BYTES read from offset 0
        self._ahead = None

    def _readline(self):
        if self.pos >= self.size:
            return None
        line = self.f.readline(self.size - self.pos)
        return line if line.endswith(b"\n") else None

    def has_line(self):
        if self._ahead is None:
            self._ahead = self._readline()
        return self._ahead is not None

    def next_line(self):
        line = self._ahead if self._ahead is not None else self._readline()
        self._ahead = None
        if line is None:
            return None
        start = self.pos
        self._take(line)
        self.last = (start, line)
        return line, start, self.pos

    def _take(self, b):
        self.from_start.update(b)
        self.seg.update(b)
        if self.head is not None and len(self.head) < PREFIX_BYTES:
            self.head += b[:PREFIX_BYTES - len(self.head)]
        self.pos += len(b)
        if self.pos == self.watch:
            self.at_watch = (self.from_start.copy(), self.seg_from, self.seg.copy())

    def cut(self, offset):
        """A batch was frozen through `offset`: the segment restarts there (the current position, or the start of
        the line just read when the batch was cut before it)."""
        if offset == self.pos:
            self.seg = hashlib.sha256()
        elif self.last is not None and offset == self.last[0]:
            self.seg = hashlib.sha256(self.last[1])
        else:
            raise SourceConflict("freeze offset off the read position")
        self.seg_from = offset

    def _extend(self, target):
        """Hash on, unparsed, up to `target` (a pass that stopped early still checks the settled tail)."""
        self._ahead = None                       # a line peeked but not taken is re-read from pos
        if self.pos >= target:
            return
        self.f.seek(self.pos)
        while self.pos < target:
            b = self.f.read(min(self.CHUNK, target - self.pos))
            if not b:
                return
            self._take(b)

    def sha_through(self, offset):
        """sha256 of the bytes from `start` to `offset`, or None if the read never stopped there."""
        if offset == self.watch and self.at_watch is not None:
            return self.at_watch[0].hexdigest()
        self._extend(offset)
        return self.from_start.hexdigest() if self.pos == offset else None

    def segment_sha(self, base, offset):
        """sha256 of the bytes from `base` (the last freeze) to `offset` (the current position or the watch)."""
        if offset == self.watch and self.at_watch is not None and self.at_watch[1] == base and offset <= self.pos:
            return self.at_watch[2].hexdigest()
        self._extend(offset)
        if self.seg_from != base or self.pos != offset:
            raise SourceConflict("settled tail off the read position")
        return self.seg.hexdigest()


class Budget:
    """--max-batches: at most this many frozen batches and record sends in one pass, so a backfill cannot starve
    the caller's other work. None is unbounded."""

    def __init__(self, limit):
        self.limit, self.used = limit, 0

    def spent(self):
        return self.limit is not None and self.used >= self.limit

    def spend(self):
        self.used += 1


@dataclass
class Config:
    seat: str
    dialect: str
    store_dir: Path
    sources: list
    max_batches: int | None = None
    batch_items: int = DEFAULT_BATCH_ITEMS
    f3: bool = False          # ingest plan D1: tool_exchange and thinking items; off until the server accepts them
    tail_window_bytes: int = TAIL_WINDOW_BYTES        # 0: no tail lane, every file from offset 0 only
    tail_threshold_bytes: int = TAIL_THRESHOLD_BYTES


def visit_order(sources):
    """Indexes into `sources` by last write, newest first, inside the pass's one budget (seat-handover item 1): an old
    session's backfill no longer spends the budget before the session the seat is writing now. The cost is the other
    way round: an old file's backfill now gets only what the newer sources leave of the budget, so while the current
    session keeps producing batches it moves slowly, and it catches up once the current session is drained. A source
    that cannot be stat'ed goes last, where the pass reports it; ties keep the given order."""
    def key(i):
        try:
            return (0, -os.stat(sources[i]).st_mtime)
        except OSError:
            return (1, 0.0)
    return sorted(range(len(sources)), key=key)


def _legacy_held_coords(db_path, bkey):
    """Read-only lookup of (src, gen, batch, first_ord, last_ord) for a pre-metadata 2-field held.json entry.

    Never instantiates Store() or FrozenOutbox(), so reporting an unvisited source never opens SQLite in write mode,
    creates tables, or contends with another reader. Returns None if the batch is already complete or unreadable.
    """
    try:
        info = os.stat(db_path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return None
        if info.st_size > LIMITS.database_bytes:
            return None
        uri = "file:%s?mode=ro" % Path(db_path).resolve().as_posix()
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as db:
            row = db.execute(
                "SELECT output, output_sha256 FROM batches WHERE key=? AND length(output) <= ?",
                (bkey, LIMITS.output_bytes),
            ).fetchone()
            if row is None or hashlib.sha256(row[0]).hexdigest() != row[1]:
                return None
            bundle = json.loads(row[0])
            records = bundle.get("records") if isinstance(bundle, dict) else None
            if not isinstance(records, list) or len(records) < 2:
                return None
            has_receipts = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox_receipts_v1'"
            ).fetchone()
            receipts = (
                dict(db.execute("SELECT position, state FROM outbox_receipts_v1 WHERE batch=?", (bkey,)).fetchall())
                if has_receipts else {}
            )
        for position, entry in enumerate(records):
            state = receipts.get(position, "ready")
            if state == "matched":
                continue
            if state not in ("pending", "ready"):
                return None
            if not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[1], str):
                return None
            val = json.loads(base64.b64decode(entry[1], validate=True))
            if not isinstance(val, dict):
                return None
            src, gen, batch = val.get("src"), val.get("gen"), val.get("batch")
            if not isinstance(src, str) or type(gen) is not int or type(batch) is not int:
                return None
            return src, gen, batch, val.get("first", val.get("ordinal")), val.get("last", val.get("ordinal"))
        return None
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, binascii.Error):
        return None


def _collect_persisted_holds(cfg, report, now):
    """Surface persisted held.json / held-tail.json entries from enrolled sources that this pass did not drain (e.g.
    when active sessions spent --max-batches first), so context_held never vanishes while a backfill source stays held."""
    root = Path(cfg.store_dir)
    try:
        if not root.is_dir():
            return
    except OSError:
        return
    try:
        registry = _read_json(root / "paths.json", {})
    except (OSError, ValueError):
        registry = {}
    if not isinstance(registry, dict):
        registry = {}
    seen = {(h.get("src"), h.get("gen"), h.get("batch")) for h in report.context_held if isinstance(h, dict)}
    seen_dirs = set()
    for src_path in cfg.sources:
        try:
            ident = None
            try:
                info = _identity(src_path)
                ident = [info.st_dev, info.st_ino]
            except (OSError, SourceConflict):
                try:
                    cand = registry.get(str(Path(src_path).resolve()))
                except OSError:
                    cand = None
                if isinstance(cand, list) and len(cand) == 2:
                    ident = cand
            if ident is None:
                continue
            dir_key = _sha("file", ident[0], ident[1])[:32]
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)
            enroll_dir = root / dir_key
            if not enroll_dir.is_dir():
                continue
            rec = _read_json(enroll_dir / "enrollment.json", None)
            if not isinstance(rec, dict) or rec.get("seat") != cfg.seat or rec.get("dialect") != cfg.dialect:
                continue
            expected_gen = rec.get("gen")
            lanes = [("held.json", expected_gen)]
            if rec.get("cut") is not None:
                try:
                    tail_rec = _read_json(enroll_dir / "enrollment-tail.json", None)
                except (OSError, ValueError):
                    tail_rec = None
                if isinstance(tail_rec, dict) and tail_rec.get("gen") == expected_gen and tail_rec.get("cut") == rec.get("cut"):
                    lanes.append(("held-tail.json", expected_gen))
            db_path = enroll_dir / "store" / "incremental.sqlite"
            for held_name, lane_gen in lanes:
                try:
                    held_map = _read_json(enroll_dir / held_name, {})
                except (OSError, ValueError):
                    continue
                if not isinstance(held_map, dict) or not held_map:
                    continue
                for bkey, meta in sorted(held_map.items()):
                    if not isinstance(meta, dict):
                        continue
                    code, since = meta.get("code"), meta.get("since")
                    if not isinstance(code, str) or not code or not isinstance(since, (int, float)) or isinstance(since, bool):
                        continue
                    if isinstance(meta.get("src"), str) and type(meta.get("gen")) is int and type(meta.get("batch")) is int:
                        src, gen, batch = meta["src"], meta["gen"], meta["batch"]
                        first_ord, last_ord = meta.get("first_ord"), meta.get("last_ord")
                    else:
                        coords = _legacy_held_coords(db_path, bkey)
                        if coords is None:
                            continue
                        src, gen, batch, first_ord, last_ord = coords
                    if lane_gen is not None and gen != lane_gen:
                        continue
                    if (src, gen, batch) in seen:
                        continue
                    seen.add((src, gen, batch))
                    report.context_held.append({
                        "src": src, "gen": gen, "batch": batch,
                        "first_ord": first_ord, "last_ord": last_ord,
                        "code": code, "age_s": max(0, int(now.timestamp() - float(since))),
                    })
        except (OSError, ValueError):
            continue


def run_pass(cfg, transport, now=None):
    now = now or datetime.now(timezone.utc)
    report = Report(cfg.seat)
    budget = Budget(cfg.max_batches)
    for index in visit_order(cfg.sources):
        path = cfg.sources[index]
        before = budget.used
        # by the source's place in --source, never its path: the report names no file (module docstring)
        report.visited.append({"source_index": index, "batches": 0})
        try:
            own = SourcePass(cfg, path, transport, report, now, budget)
            own.plan_tail()
            if own.enrollment.cut is not None:
                # Tail first: the newest bytes are sent before the backlog, out of the same budget (TailEnrollment).
                tail = SourcePass(cfg, path, transport, report, now, budget, head=own)
                tail.rewritten = tail.rewritten or own.rewritten
                own.rewritten = own.rewritten or tail.rewritten
                own.tail = tail
                tail.run()
            own.run()
        except SourceConflict as exc:
            report.conflicts.append({"src": None, "code": str(exc)})
        except StoreFull as exc:
            report.conflicts.append({"src": None, "code": "store: " + str(exc)})
        except (Unreachable, StoreBusy, StoreIOError) as exc:
            report.unreachable = True
            report.unreachable_reason = str(exc) if isinstance(exc, Unreachable) else type(exc).__name__
            break
        except StoreError as exc:                 # StoreConflict included: the local store needs review
            report.conflicts.append({"src": None, "code": "store: " + str(exc)})
        except FileNotFoundError:
            report.conflicts.append({"src": None, "code": "source missing"})
        except OSError:                           # never the path: a sharing violation or a permission refusal
            report.conflicts.append({"src": None, "code": "source unreadable"})
        finally:
            report.visited[-1]["batches"] = budget.used - before
        if budget.spent():
            break
    _collect_persisted_holds(cfg, report, now)
    return report


def _configure_django():
    """No-op kept for caller compatibility (issue #457).

    apps.context reads policy through apps.core.env.setting, which falls back to
    os.environ when Django is unconfigured. Calling settings.configure() here
    froze process-global django.conf.settings to bare global_settings and broke
    later tests that read project settings such as MESHY_MESH_ACCEPT_UNVERSIONED.
    """
    return None


SOURCES_FILE_MAX_BYTES = 16 * 1048576


def _read_sources_file(path):
    """The `sources` list of a `--sources-file`, or None when the file is missing, too large, not JSON, or not a
    list of non-empty strings: a bad list is a usage fault, never a partial pass."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(SOURCES_FILE_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > SOURCES_FILE_MAX_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    listed = doc.get("sources") if isinstance(doc, dict) else None
    if not isinstance(listed, list) or not all(isinstance(s, str) and s for s in listed):
        return None
    return listed


def main(argv=None):
    ap = argparse.ArgumentParser(description="One pass of one seat's transcripts into shared/transcript-context.")
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--store-dir", required=True)
    ap.add_argument("--seat", required=True)
    ap.add_argument("--dialect", required=True, choices=DIALECTS)
    ap.add_argument("--source", action="append", default=[], help="an enrolled transcript file (repeat)")
    ap.add_argument("--sources-file", default=None,
                    help='a JSON file {"sources": [path, ...]} holding the enrolled files; the worker uses it once '
                    "the --source list would pass Windows' command-line limit")
    ap.add_argument("--namespace", default=NAMESPACE, help="only shared/transcript-context is supported")
    ap.add_argument("--max-batches", type=int, default=None)
    ap.add_argument("--f3", action="store_true", help="also tool_exchange and thinking items (ingest plan D1). Off "
                    "until the server accepts them and steps 2b and 6b land; switching it for an enrolled source "
                    "needs a new generation")
    ap.add_argument("--tail-window-bytes", type=int, default=TAIL_WINDOW_BYTES,
                    help="a file with more than this many bytes uncommitted sends its last this-many bytes first, as a "
                    "tail lane of its own; 0 turns tail lanes off")
    ap.add_argument("--json", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    if args.namespace != NAMESPACE or (args.max_batches is not None and args.max_batches < 1) or (
            args.tail_window_bytes < 0):
        print(json.dumps({"status": "usage", "error": "unsupported namespace or --max-batches"}))
        return EXIT_USAGE
    sources = list(args.source)
    if args.sources_file is not None:
        listed = _read_sources_file(args.sources_file)
        if listed is None:
            print(json.dumps({"status": "usage", "error": "unreadable --sources-file"}))
            return EXIT_USAGE
        sources.extend(listed)
    if not sources:
        print(json.dumps({"status": "usage", "error": "no --source or --sources-file"}))
        return EXIT_USAGE
    _configure_django()
    cfg = Config(args.seat, args.dialect, Path(args.store_dir), sources, args.max_batches, f3=args.f3,
                 tail_window_bytes=args.tail_window_bytes,
                 tail_threshold_bytes=args.tail_window_bytes or TAIL_THRESHOLD_BYTES)
    try:
        transport = NodeTransport(args.state_file, args.base_url, args.seat)
    except Exception as exc:                        # noqa: BLE001 - a state that cannot load is a usage fault
        print(json.dumps({"status": "usage", "error": type(exc).__name__}))
        return EXIT_USAGE
    report = run_pass(cfg, transport)
    out = report.as_json()
    print(json.dumps(out, sort_keys=True) if args.json else
          "%s: %d batches, %d items committed, %d emitted, %d held" % (
              out["status"], out["batches_committed"], out["items_committed"],
              out["accounting"]["emitted"], len(out["context_held"])))
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
