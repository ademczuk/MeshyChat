"""Bounded raw-record windows for a future owner-reviewed incremental loader.

No JSON/dialect parsing, discovery, persistent cursor, upload or installation.
The caller supplies a selected file, its enrolled identity, a committed complete
offset and a fixed high-water target. Raw bytes stay local and are never logged.
This verifies the current window; it cannot detect arbitrary older rewrites.
"""
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
import time


class WindowError(ValueError):
    """Diagnostic classes deliberately contain no native paths or record text."""


class SourceChanged(WindowError):
    pass


class WindowDeadline(WindowError):
    pass


class SourceIOError(WindowError):
    pass


@dataclass(frozen=True)
class Identity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, info):
        return cls(info.st_dev, info.st_ino)


@dataclass(frozen=True)
class Budget:
    read_bytes: int = 1024 * 1024
    record_bytes: int = 256 * 1024
    records: int = 1024
    seconds: float = 5.0

    def __post_init__(self):
        for value in (self.read_bytes, self.record_bytes, self.records):
            if type(value) is not int or value <= 0:
                raise WindowError("positive integer window budget required")
        if self.record_bytes >= self.read_bytes:
            raise WindowError("read budget must leave one oversized-record probe byte")
        if type(self.seconds) not in (float, int) or not 0 < self.seconds < float("inf"):
            raise WindowError("finite positive window deadline required")


@dataclass(frozen=True)
class Record:
    offset: int
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class Window:
    records: tuple
    next_offset: int
    target: int
    reason: str
    bytes_read: int
    observed_bytes: int
    blocked_record: dict | None = None

    @property
    def reached_target(self):
        return self.next_offset == self.target


def _regular(info):
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SourceChanged("selected source is no longer a direct regular file")


def _deadline(until):
    if time.monotonic() > until:
        raise WindowDeadline("source window deadline exceeded")


def read_window(path, identity, offset, target, budget=None):
    """Read and recheck one bounded window; never acknowledge a partial record.

    At most 2*read_bytes+2 bytes are read: one bounded window, the same window
    again, and (when offset>0) the boundary byte before and after. The raw window
    and bounded complete records are held only in memory. The caller must atomically commit
    parser state/output before using next_offset; returned bytes are not parsed,
    approved context or a persistence receipt. An oversized or incomplete record
    stays at its original offset with an observable reason. A captured target
    ending mid-record cannot be marked completed just because read() reached it.

    Regular-file reads are not interruptible OS deadlines. Ancestor containment,
    enrollment permissions, source-generation trust and hard resource quotas
    remain caller/deployment responsibilities. Identity and window checks do not
    prove immutability before this window or after the final verification.
    """
    try:
        return _read_window(path, identity, offset, target, budget)
    except OSError:
        # Leave the handler before raising: suppression alone still retains the
        # native exception (and its filename) in __context__.
        pass
    raise SourceIOError("selected source could not be accessed") from None


def _read_window(path, identity, offset, target, budget):
    budget = Budget() if budget is None else budget
    if not isinstance(budget, Budget):
        raise WindowError("validated window budget required")
    if not isinstance(identity, Identity):
        raise WindowError("enrolled identity required")
    if type(offset) is not int or type(target) is not int or not 0 <= offset <= target:
        raise WindowError("invalid committed offset or high-water target")
    path = Path(path)
    until = time.monotonic() + budget.seconds
    before = path.stat(follow_symlinks=False)
    _regular(before)
    if Identity.from_stat(before) != identity or before.st_size < target:
        raise SourceChanged("source identity or captured target changed")
    _deadline(until)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        stream = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    with stream:
        opened = os.fstat(stream.fileno())
        _regular(opened)
        if Identity.from_stat(opened) != identity or opened.st_size < target:
            raise SourceChanged("opened source differs from enrollment")
        reads = 0
        if offset:
            stream.seek(offset - 1)
            previous = stream.read(1)
            reads += len(previous)
            if previous != b"\n":
                raise SourceChanged("committed offset is not a complete-record boundary")
        stream.seek(offset)
        size = min(budget.read_bytes, target - offset)
        data = stream.read(size)
        reads += len(data)
        if len(data) != size:
            raise SourceChanged("source shortened while reading window")
        _deadline(until)
        rows, end, reason = [], 0, "target"
        while end < len(data):
            _deadline(until)
            if len(rows) == budget.records:
                reason = "record_budget"
                break
            newline = data.find(b"\n", end)
            if newline < 0:
                remainder = len(data) - end
                if remainder > budget.record_bytes:
                    reason = "oversized_record"
                elif offset + len(data) == target:
                    reason = "partial_tail"
                else:
                    reason = "byte_budget"
                break
            length = newline + 1 - end
            if length > budget.record_bytes:
                reason = "oversized_record"
                break
            rows.append(Record(offset + end, data[end:newline + 1]))
            end = newline + 1
        if end == len(data) and offset + end < target:
            reason = "byte_budget"
        # Validate the exact bytes observed, including a partial/oversized tail.
        # A changed window never returns records or an advanced offset.
        stream.seek(offset)
        verified = stream.read(size)
        reads += len(verified)
        if verified != data:
            raise SourceChanged("source window changed before verification")
        if offset:
            stream.seek(offset - 1)
            boundary = stream.read(1)
            reads += len(boundary)
            if boundary != b"\n":
                raise SourceChanged("committed boundary changed before verification")
        after = os.fstat(stream.fileno())
        named = path.stat(follow_symlinks=False)
        _regular(after)
        _regular(named)
        if (Identity.from_stat(after) != identity or Identity.from_stat(named) != identity
                or after.st_size < target or named.st_size < target):
            raise SourceChanged("source identity or target changed before verification")
        _deadline(until)
    blocked = None
    if reason == "oversized_record":
        newline = data.find(b"\n", end)
        complete = newline >= 0
        observed = data[end:newline + 1] if complete else data[end:]
        blocked = {"offset": offset + end, "length": len(observed),
                   "sha256": hashlib.sha256(observed).hexdigest(),
                   "scope": "complete_record" if complete else "observed_prefix",
                   "record_complete": complete}
    return Window(tuple(rows), offset + end, target, reason, reads, len(data), blocked)
