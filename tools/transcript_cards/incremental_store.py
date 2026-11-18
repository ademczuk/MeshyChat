"""Private crash-atomic output/checkpoint storage, not a transcript parser.

Synthetic integration component only. No native enrollment, hook, scheduler,
network, graph receipt or compatibility with the existing full-rescan queue.
Caller supplies owner-approved bounded output and a safe original source offset.
SQLite commits output BLOBs and semantic state together; uncommitted output is
never discoverable through this API. Native-generation trust remains the caller's.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat

from capture_queue import private_scratch


SCHEMA = "transcript-incremental-store.v1"


class StoreError(ValueError):
    pass


class StoreConflict(StoreError):
    pass


class StoreIOError(StoreError):
    pass


class StoreBusy(StoreIOError):
    """Retryable SQLite contention; callers must bound retries."""


class StoreFull(StoreIOError):
    """Non-retryable SQLite page-cap or filesystem quota exhaustion."""


def _private_errors(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, sqlite3.Error) as error:
            # Extended SQLite codes retain the primary code in the low byte.
            # Do not classify by error text: it can contain private paths.
            code = getattr(error, "sqlite_errorcode", None)
            busy = isinstance(error, sqlite3.Error) and isinstance(code, int) and (
                code & 0xff) in (getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6))
            full = (isinstance(error, sqlite3.Error) and isinstance(code, int)
                    and (code & 0xff) == getattr(sqlite3, "SQLITE_FULL", 13)) or (
                    isinstance(error, OSError) and error.errno == errno.ENOSPC)
        # Neither rendered diagnostics nor exception chaining retain OS paths.
        if busy:
            raise StoreBusy("private capture store is busy; retry within a bounded policy") from None
        if full:
            raise StoreFull("private capture store reached configured byte limit") from None
        raise StoreIOError("private capture store could not complete the operation") from None
    return call


@dataclass(frozen=True)
class Limits:
    checkpoint_bytes: int = 256 * 1024
    metadata_bytes: int = 64 * 1024
    output_bytes: int = 8 * 1024 * 1024
    database_bytes: int = 64 * 1024 * 1024
    sources: int = 4096
    batches: int = 4096

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in vars(self).values()):
            raise StoreError("positive integer store limits required")


@dataclass(frozen=True)
class Snapshot:
    source: str
    generation: str
    parser: str
    offset: int
    revision: int
    batch: str | None
    metadata: bytes = field(repr=False)
    checkpoint: bytes = field(repr=False)

    def parser_state(self):
        """PRIVATE state for the owner parser; never include in shared status."""
        return json.loads(self.checkpoint)


@dataclass(frozen=True)
class Batch:
    key: str
    source: str
    generation: str
    revision: int
    start: int
    end: int
    output_sha256: str
    drops_sha256: str


@dataclass(frozen=True)
class BatchPage:
    snapshot: Snapshot
    items: tuple = field(repr=False)
    next_revision: int
    has_more: bool


def _opaque(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise StoreError("opaque source and generation identities required")


def _parser(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise StoreError("bounded parser contract version required")


def _encode(value, limit):
    try:
        if not isinstance(value, dict):
            raise TypeError
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        pass
    else:
        if len(encoded) <= limit:
            return encoded
        raise StoreError("private state byte limit exceeded")
    raise StoreError("private state requires a finite JSON object") from None


def _batch_key(source, generation, parser, revision, start, end, output_hash, drops_hash, state):
    identity = [SCHEMA, source, generation, parser, revision, start, end,
                output_hash, drops_hash, hashlib.sha256(state).hexdigest()]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def _direct(info, directory=False):
    regular = stat.S_ISDIR if directory else stat.S_ISREG
    if not regular(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise StoreError("private store requires direct files and directories")


class Store:
    @_private_errors
    def __init__(self, directory, limits=None):
        self.directory = Path(directory).absolute()
        self.limits = limits or Limits()
        if not isinstance(self.limits, Limits):
            raise StoreError("validated store limits required")
        # Check existing ancestors before creation; the designated directory is
        # owned by this store. Same-user replacement races are not prevented.
        for path in reversed((self.directory, *self.directory.parents)):
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            _direct(info, directory=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        _direct(self.directory.stat(follow_symlinks=False), directory=True)
        private_scratch(self.directory)  # BEFORE creating SQLite or journal files.
        self.path = self.directory / "incremental.sqlite"
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS format(schema TEXT NOT NULL)")
            schema = db.execute("SELECT schema FROM format").fetchall()
            if schema and schema != [(SCHEMA,)]:
                raise StoreError("private store format requires explicit rebuild review")
            if not schema:
                db.execute("INSERT INTO format VALUES(?)", (SCHEMA,))
            db.execute("""CREATE TABLE IF NOT EXISTS sources (
                source TEXT PRIMARY KEY, generation TEXT NOT NULL, parser TEXT NOT NULL,
                offset INTEGER NOT NULL, revision INTEGER NOT NULL, batch TEXT,
                metadata BLOB NOT NULL, checkpoint BLOB NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS batches (
                key TEXT PRIMARY KEY, source TEXT NOT NULL, generation TEXT NOT NULL,
                revision INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
                output_sha256 TEXT NOT NULL, drops_sha256 TEXT NOT NULL,
                output BLOB NOT NULL, drops BLOB NOT NULL,
                UNIQUE(source, generation, revision))""")
            db.commit()

    @contextmanager
    def _connection(self):
        _direct(self.directory.stat(follow_symlinks=False), directory=True)
        try:
            info = self.path.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _direct(info)
            if info.st_size > self.limits.database_bytes:
                raise StoreError("private database byte limit exceeded")
        # SQLite can open these during recovery. Reject indirect sidecars before
        # connecting; hostile same-user replacement after this check is outside
        # the private-directory trust boundary.
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                _direct(Path(str(self.path) + suffix).stat(follow_symlinks=False))
            except FileNotFoundError:
                continue
            if suffix != "-journal":
                raise StoreError("unexpected WAL state requires explicit review")
        db = sqlite3.connect(self.path, timeout=2)
        try:
            # DELETE avoids a persistent WAL; rollback journal also inherits the
            # private directory ACL. FULL is SQLite durability, not a measured
            # host power-loss or hardware quota guarantee.
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            pages = self.limits.database_bytes // db.execute("PRAGMA page_size").fetchone()[0]
            if pages <= 0:
                raise StoreError("database budget cannot hold a SQLite page")
            actual = db.execute(f"PRAGMA max_page_count={pages}").fetchone()[0]
            if actual > pages:
                raise StoreError("existing database exceeds configured page limit")
            yield db
        finally:
            db.close()

    def _snapshot(self, db, source):
        sizes = db.execute("SELECT length(metadata),length(checkpoint) FROM sources WHERE source=?",
                           (source,)).fetchone()
        if sizes and (sizes[0] > self.limits.metadata_bytes
                      or sizes[1] > self.limits.checkpoint_bytes):
            raise StoreError("retained private state exceeds configured limits")
        row = db.execute("SELECT * FROM sources WHERE source=?", (source,)).fetchone()
        if row is None:
            return None
        snapshot = Snapshot(*row)
        if snapshot.revision == 0:
            if snapshot.offset != 0 or snapshot.batch is not None:
                raise StoreConflict("initial checkpoint disagrees with durable output")
        else:
            if snapshot.revision < 0 or snapshot.batch is None:
                raise StoreConflict("progressed checkpoint requires durable output")
            batch, _, _ = self._committed(db, snapshot.batch)
            key = _batch_key(snapshot.source, snapshot.generation, snapshot.parser,
                             snapshot.revision, batch.start, snapshot.offset,
                             batch.output_sha256, batch.drops_sha256, snapshot.checkpoint)
            if ((batch.source, batch.generation, batch.revision, batch.end, batch.key) !=
                    (snapshot.source, snapshot.generation, snapshot.revision, snapshot.offset, key)):
                raise StoreConflict("checkpoint disagrees with durable output")
        return snapshot

    def _committed(self, db, key):
        sizes = db.execute("SELECT length(output)+length(drops) FROM batches WHERE key=?",
                           (key,)).fetchone()
        if sizes is None:
            raise StoreConflict("committed batch not found")
        if sizes[0] > self.limits.output_bytes:
            raise StoreError("retained output exceeds configured batch limit")
        row = db.execute("SELECT * FROM batches WHERE key=?", (key,)).fetchone()
        batch = Batch(*row[:8])
        if (hashlib.sha256(row[8]).hexdigest(), hashlib.sha256(row[9]).hexdigest()) != (
                batch.output_sha256, batch.drops_sha256):
            raise StoreConflict("committed output digest disagreement")
        return batch, row[8], row[9]

    @_private_errors
    def enroll(self, source, generation, parser, metadata, checkpoint):
        """Explicit NEW local generation only; never reset incompatible state."""
        _opaque(source)
        _opaque(generation)
        _parser(parser)
        meta = _encode(metadata, self.limits.metadata_bytes)
        state = _encode(checkpoint, self.limits.checkpoint_bytes)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._snapshot(db, source)
            if current:
                if (current.generation, current.parser, current.metadata) != (generation, parser, meta):
                    raise StoreConflict("enrollment mismatch requires an explicit reviewed generation")
                return current  # Never overwrite progressed semantic state.
            if db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] >= self.limits.sources:
                raise StoreError("enrolled source limit exceeded")
            db.execute("INSERT INTO sources VALUES(?,?,?,0,0,NULL,?,?)",
                       (source, generation, parser, meta, state))
            db.commit()
            return Snapshot(source, generation, parser, 0, 0, None, meta, state)

    @_private_errors
    def resume(self, source, generation, parser):
        _opaque(source)
        _opaque(generation)
        _parser(parser)
        with self._connection() as db:
            db.execute("BEGIN")  # State and referenced output share one read snapshot.
            current = self._snapshot(db, source)
            if current is None or (current.generation, current.parser) != (generation, parser):
                raise StoreConflict("compatible enrolled checkpoint required")
            return current

    @_private_errors
    def commit(self, expected, safe_offset, output, drops, checkpoint, *, boundary=None,
               projection=None):
        """One bounded LOCAL commit. boundary is a synthetic process-exit seam.

        Caller must prove safe_offset is an original complete-record boundary,
        check source identity/window and validate parser output/redaction first.
        This store enforces monotonicity, limits and stale-write rejection only.
        An unchanged replay returns the already committed batch without writing.
        No local batch means server persistence or graph indexing.
        An optional trusted local projection runs in the SAME transaction as
        output and checkpoint. It must bound and validate its own state; failure
        rolls back all three. It receives approved bytes, never native records.
        """
        if not isinstance(expected, Snapshot):
            raise StoreError("expected committed snapshot required")
        if type(safe_offset) is not int or not expected.offset <= safe_offset <= 2**63 - 1:
            raise StoreError("monotonic original safe offset required")
        if type(output) is not bytes or type(drops) is not bytes:
            raise StoreError("bounded owner-approved byte output required")
        if len(output) + len(drops) > self.limits.output_bytes:
            raise StoreError("batch output byte limit exceeded")
        state = _encode(checkpoint, self.limits.checkpoint_bytes)
        if safe_offset == expected.offset and not output and not drops and state == expected.checkpoint:
            raise StoreError("empty capture cannot advance a revision")
        output_hash, drops_hash = [hashlib.sha256(v).hexdigest() for v in (output, drops)]
        key = _batch_key(expected.source, expected.generation, expected.parser,
                         expected.revision + 1, expected.offset, safe_offset,
                         output_hash, drops_hash, state)
        batch = Batch(key, expected.source, expected.generation, expected.revision + 1,
                      expected.offset, safe_offset, output_hash, drops_hash)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._snapshot(db, expected.source)
            if (current is not None and current.generation == expected.generation
                    and current.parser == expected.parser and current.metadata == expected.metadata
                    and current.revision == batch.revision and current.batch == key
                    and current.offset == safe_offset and current.checkpoint == state):
                if self._committed(db, key) != (batch, output, drops):
                    raise StoreConflict("replayed batch disagrees with durable output")
                if projection:
                    projection(db, batch, output, drops, replay=True)
                return batch
            if current != expected:
                raise StoreConflict("capture snapshot is stale; resume before retry")
            if db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] >= self.limits.batches:
                raise StoreError("retained batch limit exceeded; review required")
            if boundary:
                boundary("before_output")
            db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (*vars(batch).values(), output, drops))
            if boundary:
                boundary("after_output")
            if projection:
                projection(db, batch, output, drops)
            if boundary:
                boundary("after_projection")
            db.execute("UPDATE sources SET offset=?,revision=?,batch=?,checkpoint=? WHERE source=?",
                       (safe_offset, batch.revision, key, state, expected.source))
            if boundary:
                boundary("after_state")
            db.commit()
            if boundary:
                boundary("after_commit")
        return batch

    @_private_errors
    def committed(self, key):
        """Bounded committed batch and private approved bytes; no orphan scan."""
        _opaque(key)
        with self._connection() as db:
            return self._committed(db, key)

    @_private_errors
    def batches(self, source, generation, parser, *, after_revision=0,
                limit=64, byte_limit=None):
        """Read-only bounded replay of retained LOCAL output, oldest first.

        A caller's position selects a page; it is not a delivery/index receipt.
        Private approved bytes and a coherent checkpoint share one transaction.
        Missing/corrupt batches refuse the page rather than report it drained.
        No pruning, cursor update or upload occurs. Integrity is checked for the
        visited page and latest checkpoint, not every historical batch.
        """
        _opaque(source)
        _opaque(generation)
        _parser(parser)
        if type(after_revision) is not int or not 0 <= after_revision <= 2**63 - 1:
            raise StoreError("nonnegative replay position required")
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise StoreError("bounded replay count required")
        byte_limit = self.limits.output_bytes if byte_limit is None else byte_limit
        if type(byte_limit) is not int or not 0 < byte_limit <= self.limits.output_bytes:
            raise StoreError("bounded replay byte budget required")
        with self._connection() as db:
            db.execute("BEGIN")
            current = self._snapshot(db, source)
            if current is None or (current.generation, current.parser) != (generation, parser):
                raise StoreConflict("compatible enrolled checkpoint required")
            if after_revision > current.revision:
                raise StoreConflict("replay position exceeds committed checkpoint")
            rows = db.execute("""SELECT key,revision,length(output)+length(drops)
                FROM batches WHERE source=? AND generation=? AND revision>?
                ORDER BY revision LIMIT ?""",
                (source, generation, after_revision, limit)).fetchall()
            items, used, next_revision = [], 0, after_revision
            for key, revision, size in rows:
                if revision != next_revision + 1 or revision > current.revision:
                    raise StoreConflict("retained replay has a revision gap")
                if used + size > byte_limit:
                    if not items:
                        raise StoreError("next retained batch exceeds replay byte budget")
                    break
                item = self._committed(db, key)
                batch = item[0]
                if (batch.source, batch.generation, batch.revision) != (source, generation, revision):
                    raise StoreConflict("retained replay batch identity disagrees")
                items.append(item)
                used += size
                next_revision = revision
            if not items and after_revision < current.revision:
                raise StoreConflict("retained replay is missing committed output")
            # An absent tail must not masquerade as another pending page.
            if len(items) == len(rows) and len(rows) < limit and next_revision < current.revision:
                raise StoreConflict("retained replay is missing committed output")
            return BatchPage(current, tuple(items), next_revision, next_revision < current.revision)
