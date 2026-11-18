"""`file_artifacts` persistence + the `skill_calls` reference reads.

COLUMNS READ OFF THE LIVE SCHEMA, NOT GUESSED (information_schema.columns +
pg_constraint on `meshy_django`):

    file_artifacts(id uuid pk default gen_random_uuid(), owner_id uuid NOT NULL,
        uploaded_by_peer_id uuid NULL, r2_key text NOT NULL, sha256 text NULL,
        size_bytes bigint NULL, content_type text NOT NULL, filename text NULL,
        status text NOT NULL default 'pending', grace_until timestamptz NULL,
        created_at timestamptz NOT NULL default now(), finalized_at timestamptz NULL)

    file_artifacts_owner_id_fkey          -> dashboard_users(id) ON DELETE CASCADE
                                             *** NOT an `owners` table ***
    file_artifacts_uploaded_by_peer_id_fkey -> peers(id) ON DELETE SET NULL
    file_artifacts_r2_key_key             -> UNIQUE (r2_key)   *** a second create for the
                                             same (owner, id) would be a 23505, not a
                                             silent second row ***
    file_artifacts_status_check           -> pending|ready|expired|failed
    no generated columns.

ROW-LEVEL SECURITY, MEASURED: `file_artifacts` has relrowsecurity = FALSE,
relforcerowsecurity = FALSE and ZERO rows in pg_policy. The database does NOT scope this
table. The owner check in `service.py` is the ONLY thing standing between two
tenants. It is therefore written as an explicit comparison on a value the caller never
supplied - the owner resolved from the token's peer row - and it is covered by a test
with a positive control in both directions.

`skill_calls` is the opposite: relrowsecurity AND relforcerowsecurity are both TRUE.
Under `meshychat_app` with no GUC bound, every SELECT against it returns ZERO ROWS and raises
nothing - so an in-use guard written naively FAILS OPEN and a legitimate call
participant's download is silently denied. Both reads below therefore run inside
`dbconn.request_server_internal`, and
`assert_server_internal_is_bound` is the positive control that the binding took.

TYPED ABSENCE. Nothing here returns a bare `[]`/`None`/`{}` to mean "no". A missing row
is `NotFound`, a row the caller may not see is `Denied`, an empty-but-authorised result
is `Empty`. An RLS-scoped read with no context also returns zero rows, and that is
indistinguishable from "no data" unless the two are different objects.
"""
from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass

from apps.core import dbconn

#: The terminal set, spelled once. set_grace_until, has_non_terminal_reference and the
#: grace sweep all share it so the guards never drift.
TERMINAL_CALL_STATUSES = ("completed", "failed", "timeout", "abandoned")

STATUS_PENDING = "pending"
STATUS_READY = "ready"

_COLUMNS = ("id, owner_id, uploaded_by_peer_id, r2_key, sha256, size_bytes, "
            "content_type, filename, status, grace_until, created_at, finalized_at")


# --------------------------------------------------------------- typed absence

class Absent:
    """Base of the three absence answers. Falsy so `if not result:` reads naturally,
    but never equal to `[]` or `None`, so a caller cannot accidentally treat one kind
    of absence as another."""
    __slots__ = ("reason",)

    def __init__(self, reason: str = ""):
        self.reason = reason

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.reason!r})"


class NotFound(Absent):
    """No such row. The id does not exist (or no longer does)."""


class Denied(Absent):
    """The row exists and the caller has no access path to it. Kept DISTINCT from
    NotFound inside the repository even though the HTTP layer collapses both to 404 -
    the collapse is a deliberate existence-leak defence and
    it belongs at the boundary, where it can be read, not in the data layer, where it
    would be indistinguishable from a bug."""


class Empty(Absent):
    """Authorised, queried, and genuinely no rows. NOT an error."""


@dataclass(frozen=True)
class FileRow:
    """Persisted file-artifact row."""
    id: uuid.UUID
    owner_id: uuid.UUID
    uploaded_by_peer_id: uuid.UUID | None
    r2_key: str
    sha256: str | None
    size_bytes: int | None
    content_type: str
    filename: str | None
    status: str
    grace_until: object | None
    created_at: object
    finalized_at: object | None


@dataclass(frozen=True)
class CallRef:
    """A `skill_calls` row referencing an artifact."""
    call_id: uuid.UUID
    status: str
    caller_peer_id: uuid.UUID | None
    target_peer_id: uuid.UUID


def _row(record) -> FileRow:
    return FileRow(*record)


def _uuid(value) -> uuid.UUID:
    """A `uuid.UUID`, always.

    `%s = ANY(<uuid[]>)` is type-strict in a way `id = %s` is not: psycopg sends a
    Python `str` as text, and `text = ANY(uuid[])` has no operator, so the statement
    fails with a type error rather than quietly matching nothing. Passing the UUID
    object lets psycopg's built-in uuid dumper send the right oid. Measured: the ANY
    form raises `operator does not exist: text = uuid` when handed a str."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# ---------------------------------------------------------------- file_artifacts

def get(conn, file_id) -> FileRow | NotFound:
    """Single indexed lookup on the primary key."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM file_artifacts WHERE id = %s", (str(file_id),))
        rec = cur.fetchone()
    if rec is None:
        return NotFound(f"no file_artifacts row with id={file_id}")
    return _row(rec)


def insert_with_id(conn, file_id, owner_id, uploaded_by_peer_id, r2_key,
                   content_type, filename) -> None:
    """The id is caller-supplied because `r2_key = {owner_id}/{id}` needs
    it before the INSERT.

    WRITE-SIDE OWNERSHIP (house rule 5 / sql_meshychat/001): `file_artifacts` carries no
    RLS policy at all, so the database enforces nothing here - measured, not assumed, and
    stated rather than left to be discovered. `owner_id` is the value `service.create`
    resolved from `peers WHERE id = <token's peer> AND status='active'`; it is never read
    from the request body, so a caller cannot attribute an artifact to another tenant.
    The `uploaded_by_peer_id` FK (-> peers ON DELETE SET NULL) and the owner FK
    (-> dashboard_users ON DELETE CASCADE) are the only database-side constraints.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO file_artifacts "
            "(id, owner_id, uploaded_by_peer_id, r2_key, content_type, filename, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'pending')",
            (str(file_id), str(owner_id),
             str(uploaded_by_peer_id) if uploaded_by_peer_id else None,
             r2_key, content_type, filename))


def mark_ready(conn, file_id, sha256: str, size_bytes: int) -> bool:
    """`pending` -> `ready`. Returns False when 0 rows matched, which is
    how the caller detects a lost finalize race (the row left `pending` under it)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE file_artifacts SET status='ready', sha256=%s, size_bytes=%s, "
            "finalized_at=NOW() WHERE id=%s AND status='pending'",
            (sha256, int(size_bytes), str(file_id)))
        return cur.rowcount > 0


def mark_failed(conn, file_id) -> bool:
    """`pending` -> `failed`."""
    with conn.cursor() as cur:
        cur.execute("UPDATE file_artifacts SET status='failed' "
                    "WHERE id=%s AND status='pending'", (str(file_id),))
        return cur.rowcount > 0


def delete(conn, file_id) -> bool:
    """HARD delete of the row. `FileService.soft_delete` deletes the backend object after
    this succeeds, despite its name."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM file_artifacts WHERE id=%s", (str(file_id),))
        return cur.rowcount > 0


# ------------------------------------------------------- skill_calls references

def assert_server_internal_is_bound(conn) -> None:
    """POSITIVE CONTROL for the two reads below.

    Both of them are only correct because `app.server_internal` is set: without it the
    FORCE-RLS `skill_calls` returns zero rows and raises nothing, so "no referencing
    call" and "the GUC was never bound" produce byte-identical answers. This asserts the
    setting is actually in force on THIS transaction, so the failure is named."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.server_internal', true)")
        value = cur.fetchone()[0]
    if value != "1":
        raise RuntimeError(
            "skill_calls read attempted without app.server_internal bound: the table "
            "FORCEs row-level security, so this read would have returned zero rows and "
            f"the in-use guard would have failed OPEN. current_setting = {value!r}")


@contextlib.contextmanager
def _server_internal(conn):
    """`dbconn.request_server_internal` AND its positive control, as one step. Every
    `skill_calls` read or write here needs both: the binding, because FORCE RLS returns
    zero rows without it and raises nothing, and the assertion, because only that turns
    "the binding never took" into a named failure instead of a guard that failed open.
    One context manager is how the next function added here gets both or neither."""
    with dbconn.request_server_internal(conn) as c:
        assert_server_internal_is_bound(c)
        yield c


def has_non_terminal_reference(conn, file_id) -> bool:
    """The in-use guard for DELETE.

    `EXISTS`, not a count. This is the shape house rule 4 is about: a `COUNT(*)` over
    every call that ever referenced a file, evaluated on each request, would be a
    per-request aggregate over a table that only grows. The rule's own cost figure,
    ~490 GB/day of production disk reads, is this project's measurement of a DIFFERENT
    query: `reconcile_chain_head` counts a whole grant chain on every authorization
    (docs/V1-DESIGN-RULES.md B5). `EXISTS` stops at the first
    match, the predicate `status NOT IN (terminal)` is exactly the predicate of the
    partial index `skill_calls_owner_inflight_idx` (measured 2026-09-25 with seq scans
    off: a bitmap scan of that index; tests/test_files.py fails the route on any
    unbounded scan), and NOTHING here materialises a row count.

    Evaluated entirely in SQL rather than by scanning rows in Python: `find_referencing_calls`
    caps at 100 rows, and a non-terminal reference beyond that cap would slip past a
    client-side scan and let the guard fail open."""
    with _server_internal(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM skill_calls "
                " WHERE status <> ALL(%s) "
                "   AND (%s = ANY(args_attachments) OR %s = ANY(result_attachments)))",
                (list(TERMINAL_CALL_STATUSES), _uuid(file_id), _uuid(file_id)))
            return bool(cur.fetchone()[0])


#: The referencing-calls lookup, a module constant so tests/test_files.py can EXPLAIN exactly what runs,
#: as meshychat_app under FORCE RLS (divergence 095).
REFERENCING_CALLS_SQL = "SELECT id, status, caller_peer_id, target_peer_id FROM public.file_referencing_calls(%s)"


def find_referencing_calls(conn, file_id) -> list[CallRef] | Empty:
    """Capped at 100 rows (pagination deferred to SP3+).

    Returns `Empty()` - not `[]` - when there are no referencing calls, because the
    caller's next step is an authorisation decision and "nobody referenced it" must not
    be reachable by the same code path as "the read was scoped away"."""
    # Through public.file_referencing_calls (sql_meshychat/041, divergence 095), not a SELECT on skill_calls: under FORCE
    # RLS a non-LEAKPROOF `@>` cannot use the GIN indexes ahead of the policy, so the plain query scanned the whole table
    # and every non-owner presign timed out (measured on a production copy, 2026-10-10). The function reads past RLS for
    # ONE file id and returns these four columns only. The server_internal binding stays: it is the positive control.
    with _server_internal(conn) as c:
        with c.cursor() as cur:
            cur.execute(REFERENCING_CALLS_SQL, (_uuid(file_id),))
            rows = cur.fetchall()
    if not rows:
        return Empty(f"no skill_calls row references file {file_id}")
    return [CallRef(call_id=r[0], status=r[1], caller_peer_id=r[2], target_peer_id=r[3])
            for r in rows]


# ------------------------------------------------------------ lifecycle (spec 11)

def set_grace_until(conn, file_id, retention_secs: int) -> bool:
    """Starts the post-terminal grace window (spec 11.3).

    One-shot: the `grace_until IS NULL` guard means a fresh reference can neither reset
    nor extend a window already running. Returns False when the row was already in grace
    OR a non-terminal call still references the file - the caller must not read False as
    "no such row"."""
    with _server_internal(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE file_artifacts "
                "   SET grace_until = NOW() + make_interval(secs => %s::float8) "
                " WHERE id = %s AND grace_until IS NULL "
                "   AND NOT EXISTS (SELECT 1 FROM skill_calls "
                "                    WHERE status <> ALL(%s) "
                "                      AND (%s = ANY(args_attachments) "
                "                           OR %s = ANY(result_attachments)))",
                (float(retention_secs), _uuid(file_id), list(TERMINAL_CALL_STATUSES),
                 _uuid(file_id), _uuid(file_id)))
            return cur.rowcount > 0


def sweep_pending_past_window(conn, window_secs: int) -> list[tuple] | Empty:
    """`pending` rows older than the upload window become
    `failed`. CAPPED AT 100 per call - the cap is the reason this is a janitor operation
    and not a per-request one."""
    with conn.cursor() as cur:
        cur.execute(
            "WITH expired AS ("
            "  SELECT id FROM file_artifacts "
            "   WHERE status='pending' "
            "     AND created_at < NOW() - make_interval(secs => %s::float8) LIMIT 100) "
            "UPDATE file_artifacts SET status='failed' "
            " WHERE id IN (SELECT id FROM expired) RETURNING id, r2_key",
            (float(window_secs),))
        rows = cur.fetchall()
    return rows or Empty("no pending artifact is past its upload window")


def sweep_ready_past_grace(conn) -> list[tuple] | Empty:
    """`ready` rows whose grace window elapsed are hard-
    deleted. The `NOT EXISTS` re-check closes the race where a NEW non-terminal call
    re-referenced the file after grace started - `set_grace_until` is one-shot, so
    without this the sweep would delete a blob backing an in-flight call."""
    with _server_internal(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "WITH expired AS ("
                "  SELECT f.id FROM file_artifacts f "
                "   WHERE f.status='ready' AND f.grace_until IS NOT NULL "
                "     AND f.grace_until < NOW() "
                "     AND NOT EXISTS (SELECT 1 FROM skill_calls "
                "                      WHERE status <> ALL(%s) "
                "                        AND (f.id = ANY(args_attachments) "
                "                             OR f.id = ANY(result_attachments))) "
                "   LIMIT 100) "
                "DELETE FROM file_artifacts WHERE id IN (SELECT id FROM expired) "
                "RETURNING id, r2_key",
                (list(TERMINAL_CALL_STATUSES),))
            rows = cur.fetchall()
    return rows or Empty("no ready artifact is past its grace window")


# ------------------------------------------- /v1/health's blob probe (OI-48, divergence 047)

def newest_ready_keys(conn, limit: int) -> list[str] | Empty:
    """The `r2_key` of the newest `limit` ready rows, newest first: what `/v1/health`'s blob
    probe looks for under blob_dir (`meshychat.urls.ready_files_missing`; divergence 047,
    ASDLC OI-48).

    BOUNDED BY `limit` in what it returns, NOT in what it reads: no index serves
    `WHERE status = 'ready' ORDER BY created_at DESC`, so this is a sequential scan of
    `file_artifacts`. That is deliberate for now (divergence 047): production's table held 3
    rows when this was written (2026-09-27), and the partial index that bounds the read
    (drafted as sql_meshychat/025, `(created_at DESC) WHERE status = 'ready'`) was left out
    because any migration's roll-forward gate needs a full production backup first. Add it
    when the table reaches a few thousand rows.

    No RLS context is bound: `file_artifacts` has no row-level security (module docstring,
    measured), so the application role reads every tenant's rows, which is what a check of
    the whole store needs. Only keys leave this function, and the caller logs counts, never
    a key (a key names its owner)."""
    with conn.cursor() as cur:
        cur.execute("SELECT r2_key FROM file_artifacts WHERE status = 'ready' "
                    "ORDER BY created_at DESC LIMIT %s", (int(limit),))
        rows = cur.fetchall()
    if not rows:
        return Empty("no ready file_artifacts row")
    return [r[0] for r in rows]


def still_ready(conn, keys: list[str]) -> list[str] | Empty:
    """Which of `keys` still name a ready row. The blob probe asks it of the keys whose file
    it did not find, because every delete removes the ROW before the file
    (`FileService.soft_delete`, `sweep_ready_past_grace` then `_sweep_blobs`): a key read as
    ready, then deleted, then looked for is a delete in flight, not a lost file. One probe
    of the UNIQUE (r2_key) index per key; the caller passes at most its cap of keys."""
    with conn.cursor() as cur:
        cur.execute("SELECT r2_key FROM file_artifacts "
                    " WHERE r2_key = ANY(%s) AND status = 'ready'", (list(keys),))
        rows = cur.fetchall()
    if not rows:
        return Empty("none of the keys names a ready row any longer")
    return [r[0] for r in rows]


def attachments_of_call(conn, call_id: str) -> tuple | NotFound:
    """Both attachment arrays of one `skill_calls` row, keyed on the TEXT `call_id` column
    (not the uuid `id`)."""
    with _server_internal(conn) as c:
        with c.cursor() as cur:
            cur.execute("SELECT args_attachments, result_attachments FROM skill_calls "
                        " WHERE call_id = %s", (call_id,))
            rec = cur.fetchone()
    if rec is None:
        return NotFound(f"no skill_calls row with call_id={call_id!r}")
    return rec


# ------------------------------------------------------------ rule 3 and receipts (issue #118)

def message_grant_key(conn, file_id, peer_id) -> str | None:
    """Rule 3 (apps/files/grants.py): the key of a mail message that attached the file and addressed this peer, or
    None. A primary-key range read on file_message_grants, under the same binding and positive control as every
    skill_calls read here."""
    from . import grants
    with _server_internal(conn) as c:
        return grants.message_grant_key(c, file_id, peer_id)


def record_direct_grant(conn, file_id, label, granter_peer_id) -> list[str]:
    """A direct grant (apps/files/grants.py record_direct_grant) under the server_internal binding and its control."""
    from . import grants
    with _server_internal(conn) as c:
        return grants.record_direct_grant(c, file_id=file_id, label=label, granter_peer_id=granter_peer_id)


def record_download(conn, file_id, peer_id, rule: int, sha256, message_key=None) -> bool:
    from . import grants
    with _server_internal(conn) as c:
        return grants.record_download(c, file_id, peer_id, rule, sha256, message_key)


def list_downloads(conn, file_id, limit: int = 100) -> list[dict]:
    from . import grants
    with _server_internal(conn) as c:
        return grants.list_downloads(c, file_id, limit)
