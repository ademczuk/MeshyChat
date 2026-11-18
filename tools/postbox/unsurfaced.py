"""Read-only, bounded evidence of notices delayed between dispatch and a session.

The checkpoint belongs to this observer, never to delivery. A first observation starts
at each transcript's EOF; it cannot prove anything about historical transcript bytes.
Existing logs also start at EOF. Counts cover only the displayed since baseline.
No transcript, message key or local path is put on the wire.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

try:
    from . import delivery_bank
except ImportError:
    import delivery_bank

GRACE_SECONDS = 300
REBASE_QUIET_SECONDS = 1800
MAX_PENDING = 2048
MAX_STREAMS = 384
MAX_LOGS = 64
MAX_DIRECTORY_ENTRIES = 4096
LOG_BYTES_PER_PASS = 256 * 1024
TRANSCRIPT_BYTES_PER_PASS = 256 * 1024
MAX_LINE = 256 * 1024
MAX_CHECKPOINT = 2 * 1024 * 1024
MAX_READ_LEDGER = 1024 * 1024
INCOMPLETE = -1  # Explicit partial/overflow sentinel, never a clean zero.
KEY = re.compile(rb"msg/[A-Za-z0-9][A-Za-z0-9._:/-]{0,251}")


def instant(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(dt.timezone.utc) if stamp.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def key_ok(key):
    return isinstance(key, str) and key.isascii() and KEY.fullmatch(key.encode()) is not None


class Tracker:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.streams, self.pending = {}, {}
        self.since = None
        self.identity = None
        self.gap = self.overflow = False
        self.last_issue = None
        self.last_fault_at = None
        self._now = None
        self._partial_paths = set()
        self._rotation_cursors = []
        self._fault = False
        self._initializing = False
        self._saved = None
        self._read_signature, self._read_cache = None, {}
        try:
            body = delivery_bank._bounded_json_file(checkpoint, MAX_CHECKPOINT)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.gap = True
            return
        try:
            assert body["schema"] == 1 and instant(body["since"]) is not None
            assert isinstance(body["streams"], dict) and len(body["streams"]) <= MAX_STREAMS
            assert isinstance(body["pending"], dict) and len(body["pending"]) <= MAX_PENDING
            for path, cursor in body["streams"].items():
                assert isinstance(path, str) and len(path) <= 4096
                assert set(cursor) == {"dev", "ino", "offset", "size", "discard"}
                assert all(type(cursor[k]) is int and cursor[k] >= 0 for k in ("dev", "ino", "offset", "size"))
                assert type(cursor["discard"]) is bool
            for key, item in body["pending"].items():
                assert key_ok(key) and instant(item["at"]) and instant(item["after"])
                assert type(item["version"]) is int and item["version"] >= 1
                assert type(item["uncertain"]) is bool
            self.streams, self.pending, self.since = body["streams"], body["pending"], body["since"]
            assert isinstance(body["identity"], str) and body["identity"]
            self.identity = body["identity"]
            self.gap, self.overflow = bool(body["gap"]), bool(body["overflow"])
            issue = body.get("last_issue")
            if issue is not None:
                assert set(issue) == {"kind", "since", "rebased_at"}
                assert issue["kind"] in ("gap", "overflow", "gap+overflow")
                assert instant(issue["since"]) and instant(issue["rebased_at"])
            self.last_issue = issue
            fault = body.get("last_fault_at")
            assert fault is None or instant(fault) is not None
            self.last_fault_at = fault
        except (AssertionError, KeyError, TypeError, ValueError):
            self.gap = True

    def _mark_gap(self):
        self.gap = self._fault = True
        self.last_fault_at = self._now

    def _stream(self, path, budget, *, transcript=False, limit=None, expected=None, probe_budget=0):
        """Complete JSONL records only; aggregate I/O is charged even for a partial row.

        Initial EOF is a baseline. A genuinely new session starts at zero; an old
        file enrolled later starts at EOF. Header probes share the transcript budget.
        Oversized records are drained with an explicit coverage gap, never unbounded RAM.
        """
        fd, info = delivery_bank.open_regular_read(path)
        with os.fdopen(fd, "rb") as handle:
            if expected is not None and (info.st_dev, info.st_ino) != expected:
                raise ValueError("unsurfaced_source_identity")
            cursor = self.streams.get(path)
            known_path = cursor is not None
            identity = (info.st_dev, info.st_ino)
            if cursor is None or (cursor["dev"], cursor["ino"]) != identity:
                # Rotation may leave the old inode in an archive. It gets its own
                # path but must reuse the inode cursor even when that path is new;
                # otherwise settled dispatches are replayed from byte zero.
                matches = [c for c in (*self.streams.values(), *self._rotation_cursors)
                           if (c["dev"], c["ino"]) == identity]
                cursor = dict(max(matches, key=lambda c: c["offset"])) if matches else None
            # A bounded pass may leave unread bytes beyond the consumed offset.
            # Detect their removal before replacing the previously observed size,
            # even when this inode still extends to or beyond the read position.
            if cursor is not None and info.st_size < cursor["size"]:
                self._mark_gap()
            reset = cursor is None or info.st_size < cursor["offset"]
            probe_used = 0
            if reset:
                growth = not self._initializing
                if transcript and not known_path and growth:
                    born = getattr(info, "st_birthtime", None)
                    if born is not None:
                        growth = born >= instant(self.since).timestamp()
                    else:
                        if probe_budget <= 0:
                            raise ValueError("unsurfaced_probe_budget")
                        header = handle.readline(min(4096, probe_budget))
                        probe_used = len(header)
                        try:
                            first = json.loads(header) if header.endswith(b"\n") else {}
                            stamp = instant(first.get("timestamp")) if isinstance(first, dict) else None
                        except (ValueError, UnicodeDecodeError):
                            stamp = None
                        growth = stamp is not None and stamp >= instant(self.since)
                if transcript and not self._initializing and (known_path or not growth):
                    self._mark_gap()
                    for item in self.pending.values():
                        item["uncertain"] = True
                    growth = False
                cursor = {"dev": info.st_dev, "ino": info.st_ino,
                          "offset": 0 if growth else info.st_size,
                          "size": info.st_size, "discard": False}
            self.streams[path] = cursor
            cursor["size"] = info.st_size
            handle.seek(cursor["offset"])
            end_size = min(info.st_size, limit) if limit is not None else info.st_size
            data = handle.read(max(0, min(budget, MAX_LINE, end_size - cursor["offset"])))
            used, rows = len(data) + probe_used, []
            if not data:
                return rows, used, cursor["offset"] < end_size
            end = data.rfind(b"\n")
            if end < 0:
                self._partial_paths.add(path)
                if len(data) == MAX_LINE or cursor["discard"]:
                    self._mark_gap()
                    cursor["discard"] = True
                    cursor["offset"] += len(data)
                return rows, used, True
            complete = data[:end + 1]
            cursor["offset"] += len(complete)
            parts = complete.splitlines()
            if cursor["discard"]:
                parts = parts[1:]
                cursor["discard"] = False
            for line in parts:
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    self._mark_gap()
                    continue
                if isinstance(obj, dict):
                    rows.append((obj, line))
                else:
                    self._mark_gap()
            return rows, used, cursor["offset"] < end_size

    def _logs(self, ident):
        log = os.path.abspath(ident["log"])
        parent, prefix = os.path.dirname(log), os.path.basename(log) + "."
        paths = []
        with os.scandir(parent) as entries:
            for number, entry in enumerate(entries):
                if number >= MAX_DIRECTORY_ENTRIES:
                    raise ValueError("unsurfaced_directory_limit")
                name = entry.name
                if not name.startswith(prefix):
                    continue
                for suffix in (".dispatch.jsonl", ".skips.jsonl"):
                    stem, marker, archive = name.partition(suffix)
                    if marker and stem.endswith(".offset") and (not archive or
                            archive.startswith(".") and archive[1:].isdigit() and 1 <= int(archive[1:]) <= 16):
                        paths.append((entry.path, suffix == ".skips.jsonl"))
                if len(paths) > MAX_LOGS:
                    raise ValueError("unsurfaced_log_limit")
        # Dispatch first so a same-pass skip can settle it. Archives are found by
        # inode, not guessed order: duplicate dispatches retain their first age.
        return sorted(paths, key=lambda pair: (pair[1], not pair[0].rsplit(".", 1)[-1].isdigit(), pair[0]))

    def _add(self, event, at):
        key, version = event.get("key"), event.get("version", 1)
        if event.get("event") not in ("message", "edit"):
            return
        if not key_ok(key) or type(version) is not int or version < 1:
            self._mark_gap()
            return
        old = self.pending.get(key)
        if old:
            old["at"] = min(old["at"], at.isoformat())
            old["after"] = max(old["after"], at.isoformat())
            old["version"] = max(old["version"], version)
        else:
            self.pending[key] = {"at": at.isoformat(), "after": at.isoformat(), "version": version,
                                 "uncertain": at < instant(self.since)}
        if len(self.pending) > MAX_PENDING:
            oldest = min(self.pending, key=lambda k: self.pending[k]["at"])
            del self.pending[oldest]  # Report lost coverage before any later baseline recovery.
            self.overflow = True
            self._fault = True  # Eviction is a fresh fault even if retained keys settle this pass.
            self.last_fault_at = self._now

    def _reads(self, ident):
        path = ident.get("state")
        if not isinstance(path, str):
            return {}
        try:
            signature = tuple((s.st_dev, s.st_ino, s.st_size) for s in
                              (os.lstat(path), os.lstat(path + ".read.json")))
            if signature == self._read_signature:
                return self._read_cache
            state = delivery_bank._bounded_json_file(path, MAX_READ_LEDGER)
            ledger = delivery_bank._bounded_json_file(path + ".read.json", MAX_READ_LEDGER)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            self._mark_gap()
            return {}
        reads = {}
        if (not isinstance(state, dict) or not isinstance(state.get("peer_id"), str)
                or not isinstance(ledger, dict) or type(ledger.get("schema_version")) is not int
                or ledger.get("schema_version") != 1 or not isinstance(ledger.get("reads"), list)):
            self._mark_gap()
            return {}
        if ledger.get("peer_id") == state["peer_id"]:
            if len(ledger["reads"]) > 2000:
                self._mark_gap()
                return {}
            for row in ledger["reads"]:
                if (isinstance(row, dict) and key_ok(row.get("key")) and type(row.get("version")) is int
                        and row["version"] >= 1 and isinstance(row.get("at"), str)):
                    reads[row["key"]] = max(reads.get(row["key"], 0), row["version"])
        self._read_signature, self._read_cache = signature, reads
        return reads

    def update(self, ident, sources, *, state, now, status=lambda **kw: None, expected_inodes=None):
        if self.identity is not None and self.identity != ident["name"]:
            raise ValueError("unsurfaced_identity_changed")
        self.identity = ident["name"]
        self._now = now.isoformat()
        self._partial_paths = set()
        # Preserve inode positions before any archive pathname is overwritten:
        # processing the new .1 must not erase the cursor for the old .1 -> .2.
        self._rotation_cursors = [dict(c) for c in self.streams.values()]
        self._initializing = self.since is None
        self._fault = self.gap if self._initializing else False
        if self.since is None:
            self.since = now.isoformat()
        if not ident.get("log"):
            return {"unsurfaced": None, "unsurfaced_oldest_at": None, "unsurfaced_since": None}
        # Establish transcript baselines BEFORE ingesting any dispatch records.
        backlog = False
        budget = TRANSCRIPT_BYTES_PER_PASS
        limits = {}
        for path in sources:
            _, used, _ = self._stream(path, 0, transcript=True, probe_budget=budget,
                                      expected=(expected_inodes or {}).get(path))
            budget -= used
            limits[path] = self.streams[path]["size"]
        log_budget = LOG_BYTES_PER_PASS
        skips = []
        previous_unread = {(c["dev"], c["ino"]) for path, c in self.streams.items()
                           if path not in sources and c["offset"] < c["size"]}
        logs = self._logs(ident)
        for path, is_skip in logs:
            if log_budget <= 0:
                backlog = True
                break
            rows, used, behind = self._stream(path, log_budget)
            log_budget -= used
            backlog |= behind
            for record, _ in rows:
                if is_skip:
                    skips.append(record)
                    continue
                if record.get("identity") != ident["name"]:
                    self._mark_gap()
                    continue
                at = instant(record.get("at"))
                events = record.get("events")
                if at is None or at > now + dt.timedelta(seconds=60) or not isinstance(events, list):
                    self._mark_gap()
                    continue
                for event in events:
                    if isinstance(event, dict):
                        self._add(event, at)
        for record in skips:
            key = record.get("key")
            item = self.pending.get(key) if key_ok(key) else None
            version = record.get("version")
            if (item and record.get("reason") == "already_read" and type(version) is int
                    and version >= item["version"]):
                del self.pending[key]
        # This bounded ledger is peer-bound by delivery_bank; another identity's
        # read or an older version never settles this identity's pending notice.
        reads = self._reads(ident)
        for key, item in list(self.pending.items()):
            if reads.get(key, 0) >= item["version"]:
                del self.pending[key]
        if not self.pending and not backlog:
            # No candidate keys: adopt the captured EOF without reading transcript
            # contents. Bytes arriving after that snapshot belong to the next pass.
            for path in sources:
                self.streams[path]["offset"] = limits[path]
        # Don't consume transcript growth ahead of dispatch ingestion. Otherwise a
        # bounded log backlog could make a later-discovered key miss its evidence.
        for path in (() if backlog else sources):
            if budget <= 0:
                backlog = True
                break
            rows, used, behind = self._stream(path, budget, transcript=True, limit=limits[path],
                                             expected=(expected_inodes or {}).get(path))
            budget -= used
            backlog |= behind
            for record, line in rows:
                stamp = instant(record.get("timestamp"))
                if stamp is None or stamp > now + dt.timedelta(seconds=60):
                    continue
                for raw_key in set(KEY.findall(line)):
                    key = raw_key.decode("ascii")
                    item = self.pending.get(key)
                    if item and stamp >= instant(item["after"]):
                        del self.pending[key]
        if len(self.streams) > MAX_STREAMS:
            raise ValueError("unsurfaced_stream_limit")
        active = set(sources) | {path for path, _ in logs}
        active_inodes = {(c["dev"], c["ino"]) for path, c in self.streams.items() if path in active}
        if previous_unread - active_inodes:
            self._mark_gap()
        for path, cursor in self.streams.items():
            if (path not in active and cursor["offset"] < cursor["size"]
                    and (cursor["dev"], cursor["ino"]) not in active_inodes):
                self._mark_gap()  # An unread stream disappeared without a retained archive.
        # Retain a gap verdict, not an endlessly replayed missing cursor.
        self.streams = {path: c for path, c in self.streams.items() if path in active}
        partial = self.gap or self.overflow or backlog or any(i["uncertain"] for i in self.pending.values())
        if partial and self.last_fault_at is None:
            self.last_fault_at = self._now
        quiet = (now - instant(self.last_fault_at)).total_seconds() if self.last_fault_at else 0
        if partial and not self._fault and (not self.pending and not backlog or quiet >= REBASE_QUIET_SECONDS):
            self.last_issue = {"kind": "gap+overflow" if self.gap and self.overflow else
                               "overflow" if self.overflow else "gap",
                               "since": self.since, "rebased_at": now.isoformat()}
            # Quiet recovery preserves suspected missing keys, including original
            # dispatch age. An arrival inside the gap may remain until an own read
            # or new session appearance settles it; visibility wins over hiding it.
            for item in self.pending.values():
                item["uncertain"] = False
            for path, cursor in self.streams.items():
                end = limits[path] if path in limits else cursor["size"]
                if cursor["offset"] < end:
                    cursor["offset"] = end
                    cursor["discard"] = path in self._partial_paths
            self.since = now.isoformat()
            self.gap = self.overflow = False
            self.last_fault_at = None
            backlog = False
        elif not partial:
            self.last_fault_at = None
        body = {"schema": 1, "identity": self.identity, "since": self.since, "streams": self.streams, "pending": self.pending,
                "gap": self.gap, "overflow": self.overflow, "last_issue": self.last_issue,
                "last_fault_at": self.last_fault_at}
        encoded = json.dumps(body, sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_CHECKPOINT:
            raise ValueError("unsurfaced_checkpoint_limit")
        if encoded != self._saved:
            delivery_bank.atomic_json(self.checkpoint, body)
            self._saved = encoded
        uncertain = self.gap or self.overflow or backlog or any(i["uncertain"] for i in self.pending.values())
        aged = [instant(i["at"]) for i in self.pending.values()
                if (now - instant(i["at"])).total_seconds() >= GRACE_SECONDS]
        oldest = min(aged).isoformat() if aged else None
        # Token-bound turn state distinguishes held/open from lost/closed at display.
        # Never hide an aged pending notice merely because a turn remains open.
        count = INCOMPLETE if uncertain else len(aged)
        status(unsurfaced_overflow=self.overflow, unsurfaced_incomplete=bool(uncertain),
               unsurfaced_backlog=backlog, unsurfaced_since=self.since, unsurfaced_last_issue=self.last_issue)
        return {"unsurfaced": count, "unsurfaced_oldest_at": oldest if count not in (None, 0) else None,
                "unsurfaced_since": self.since}
