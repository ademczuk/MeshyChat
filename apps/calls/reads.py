"""Reads for the `/v1/calls/*` surface, with TYPED absence.

THE RULE THIS MODULE EXISTS TO ENFORCE. `skill_calls` has FORCE row-level security
(measured: `pg_class.relrowsecurity` and `relforcerowsecurity` are both true). Under
role `meshychat_app` with NO context bound, a SELECT returns ZERO ROWS AND RAISES NOTHING.
So a bare `[]` or a bare `None` out of a reader here has at least four possible causes:

    the row does not exist          -> 404
    the row exists, viewer is not a party -> 403
    the row exists and matched nothing under the filter -> 200 []
    THE CONNECTION HAD NO PEER CONTEXT and the policy hid everything -> a BUG

The last one is the dangerous one, because it is the one that looks exactly like "no
data" to every caller. None of these functions returns a bare empty collection: they
return `Found` / `NotFound` / `Denied` / `Rows` / `Empty`, and `Empty` is only ever
produced by a query that PROVED its own context was bound first (see `_assert_bound`).

WHICH CONTEXT EACH READ USES, and why they differ:
  * by call_id           -> server_internal. The route must be able to see a call in
    order to decide whether the viewer may. Authorization is then done in application
    code by `for_viewer`.
  * list for a peer      -> peer_context. The SQL `WHERE` and the RLS policy say the
    same thing, on purpose: the policy stays load-bearing, so a future edit to the
    WHERE clause cannot widen the result set past the viewer.
  * events / chunks      -> neither. MEASURED: `skill_call_events` and
    `skill_call_chunks` have relrowsecurity = FALSE, so these reads use the bare pool
    with no GUC. Their authorization is
    the `assert_peer_can_see` on the PARENT CALL, which the view does first.

NO PER-REQUEST AGGREGATE. There is no COUNT(*) in this file: a chain-wide count on the
authorize path would make its work grow with the chain. Every statement below is a keyed
lookup or a LIMITed range scan.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass

from apps.calls import chunks as call_chunks
from apps.calls import events
from apps.core import rls

#: Call columns selected by readers. `human_auth_kid` and
#: `human_consent_hash` exist on the TABLE but not in the call JSON shape, so they are not
#: selected - a column the wire never carries should not be read into the process.
CALL_COLUMNS = (
    "id", "call_id", "caller_peer_id", "caller_owner_id", "target_peer_id",
    "target_owner_id", "skill_id", "skill_name", "skill_version", "mode",
    "transport", "status", "args", "args_signature", "args_signing_kid",
    "args_attachments", "result", "result_payload", "result_attachments",
    "result_signature", "result_signing_kid", "error_code", "error_message",
    "grant_id", "continuation_nonce", "webhook_url", "webhook_secret_hash",
    "deadline_at", "started_at", "completed_at", "created_at", "pause_context_id",
    "pause_requested_schema", "resume_nonce", "resume_spent_at",
)

EVENT_COLUMNS = ("id", "call_id", "event_type", "details", "source",
                 "source_peer_id", "occurred_at")

CHUNK_COLUMNS = ("id", "call_id", "sequence", "chunk_type", "content",
                 "file_artifact_id", "chunk_signature", "chunk_signing_kid",
                 "emitted_at")

#: List queries default to 100 rows and clamp the limit to 1 through 500.
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500

#: The two roles that may see a call.
ROLE_CALLER = "caller"
ROLE_TARGET = "target"

#: Wire strings used to validate `?status=`.
CALL_STATUSES = ("pending", "routed", "queued", "running", "input_required",
                 "auth_required", "completed", "failed", "timeout", "abandoned")

#: States from which a call may be cancelled.
IN_FLIGHT_STATUSES = ("pending", "routed", "queued", "running")


# --------------------------------------------------------------- typed outcomes

@dataclass(frozen=True)
class Found:
    """The row exists and the caller is entitled to it."""
    row: dict


@dataclass(frozen=True)
class NotFound:
    """No row with this key. Distinct from Denied: 404 vs 403."""
    call_id: str


@dataclass(frozen=True)
class Denied:
    """The row exists; this viewer is neither its caller nor its target."""
    call_id: str
    viewer: uuid.UUID


@dataclass(frozen=True)
class Rows:
    """A list query that ran under a PROVEN context and matched at least one row."""
    rows: list


@dataclass(frozen=True)
class Empty:
    """A list query that ran under a PROVEN context and matched nothing.

    `scope` records what was actually queried, so a caller reading this value knows the
    search space rather than inferring it. An unproven context never produces this -
    `_assert_bound` raises instead.
    """
    scope: str


class ContextNotBound(RuntimeError):
    """The connection was not bound to the identity the query claims to be scoped to.

    Raised rather than returning `Empty`, because these two are indistinguishable at
    the call site and only one of them is a bug.
    """


def _rows(cur, columns) -> list[dict]:
    return [dict(zip(columns, r)) for r in cur.fetchall()]


def _assert_bound(conn, peer_id) -> None:
    """Positive control for every RLS-scoped read in this file.

    Reads back `app.current_peer_id` INSIDE the transaction the query will run in and
    compares it to the identity the caller says they are scoping to. Without this, a
    context that silently failed to bind returns zero rows and the route answers 200 []
    - the exact failure the RLS module warns is silent.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.current_peer_id', true)")
        bound = cur.fetchone()[0]
    if bound is None or bound == "" or uuid.UUID(bound) != uuid.UUID(str(peer_id)):
        raise ContextNotBound(
            f"peer context is {bound!r}, expected {peer_id!r}: an RLS-scoped read on "
            "this connection would return zero rows and look like 'no data'")


# ------------------------------------------------------------------------ calls

def load_call(conn, call_id: str) -> Found | NotFound:
    """Keyed on the UNIQUE
    `skill_calls.call_id`; server-internal, because the route has to see the row before
    it can decide whether the viewer may."""
    cols = ", ".join(CALL_COLUMNS)
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM skill_calls WHERE call_id = %s", (call_id,))
            rows = _rows(cur, CALL_COLUMNS)
    if not rows:
        return NotFound(call_id=call_id)
    return Found(row=rows[0])


def load_call_status(conn, call_id: str) -> Found | NotFound:
    """The STATUS of one call and nothing else: `Found.row` is `{call_id, status}`.

    For a POLL that only needs to know whether a call has ended (the A2A SSE stream, five
    times a second per open stream). `load_call` selects all 34 columns, `args` and
    `result` jsonb among them, to answer that; this is the same keyed lookup on the UNIQUE
    `call_id`, under the same server-internal context, reading two. STATUS C73.
    It authorizes nothing: callers must already have run `for_viewer` on a `load_call`.
    """
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT call_id, status FROM skill_calls WHERE call_id = %s",
                        (call_id,))
            rows = _rows(cur, ("call_id", "status"))
    if not rows:
        return NotFound(call_id=call_id)
    return Found(row=rows[0])


def for_viewer(found: Found, viewer: uuid.UUID) -> Found | Denied:
    """Caller OR target, nobody else.

    `caller_peer_id` is NULLABLE, and `call.caller_peer_id == Some(viewer)` is FALSE for
    a NULL caller - so a caller-less call is visible to its target only. Comparing a
    None against a viewer with `==` in Python gives the same answer; it is spelled out
    because `None == viewer` quietly being False is the kind of thing that is right by
    accident until someone "tidies" it.
    """
    row = found.row
    caller = row["caller_peer_id"]
    target = row["target_peer_id"]
    v = uuid.UUID(str(viewer))
    if caller is not None and uuid.UUID(str(caller)) == v:
        return found
    if uuid.UUID(str(target)) == v:
        return found
    return Denied(call_id=row["call_id"], viewer=v)


def list_for_peer(conn, viewer: uuid.UUID, role: str, status: str | None,
                  since: _dt.datetime | None, limit: int) -> Rows | Empty:
    """List calls visible to one peer.

    `since` filters `created_at > since` - STRICTLY greater. The SQL predicate is `>`.

    Ordered `created_at DESC` and LIMITed. No count, no offset: this is the route that
    a busy peer hits most, and an unbounded read here is the shape house rule 4 names.
    """
    if role not in (ROLE_CALLER, ROLE_TARGET):
        raise ValueError(f"role must be {ROLE_CALLER!r} or {ROLE_TARGET!r}")
    cols = ", ".join(CALL_COLUMNS)
    with rls.peer_context(conn, peer_id=str(viewer)):
        _assert_bound(conn, viewer)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM skill_calls "
                " WHERE ((%s::bool AND caller_peer_id = %s) "
                "     OR (%s::bool AND target_peer_id = %s)) "
                "   AND (%s::text IS NULL OR status = %s) "
                "   AND (%s::timestamptz IS NULL OR created_at > %s) "
                " ORDER BY created_at DESC LIMIT %s",
                (role == ROLE_CALLER, str(viewer), role == ROLE_TARGET, str(viewer),
                 status, status, since, since, limit))
            rows = _rows(cur, CALL_COLUMNS)
    if not rows:
        return Empty(scope=f"skill_calls role={role} viewer={viewer} status={status} "
                           f"since={since} limit={limit}")
    return Rows(rows=rows)


# --------------------------------------------------------------- events + chunks

def list_events(conn, call_id: str, since: _dt.datetime | None) -> Rows | Empty:
    """`occurred_at > since`, strictly.

    No RLS context: `skill_call_events` has row-level security DISABLED (measured), and
    reads use the bare pool. Authorization is the caller-or-target check the
    view has already run on the PARENT call, which is why `call_id` must be one the
    view resolved rather than one it took from the path unchecked.
    """
    cols = ", ".join(EVENT_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols} FROM skill_call_events "
            " WHERE call_id = %s AND (%s::timestamptz IS NULL OR occurred_at > %s) "
            " ORDER BY occurred_at ASC, id ASC", (call_id, since, since))
        rows = _rows(cur, EVENT_COLUMNS)
    if not rows:
        return Empty(scope=f"skill_call_events call_id={call_id} since={since}")
    return Rows(rows=rows)


def list_chunks(conn, call_id: str, since_seq: int, *,
                limit: int | None = None) -> Rows | Empty:
    """`sequence > since_seq`, default 0.

    EXCLUSIVE: called with since=0 it skips a chunk at sequence 0 (design 2.12.11);
    GET /v1/calls/{id}/chunks keeps that default (Q15) and `since=-1` includes 0. The WS replay
    reads through `apps/calls/chunks.replay` instead (divergence 041 S7).

    `limit` pages the range scan (STATUS C73): the A2A stream reads a page at a time and
    re-reads from the last sequence it emitted. `None` leaves the GET
    /v1/calls/{id}/chunks read unbounded."""
    cols = ", ".join(CHUNK_COLUMNS)
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be a positive page size, got {limit!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols} FROM skill_call_chunks "
            " WHERE call_id = %s AND sequence > %s ORDER BY sequence ASC"
            + (" LIMIT %s" if limit is not None else ""),
            (call_id, since_seq) + ((limit,) if limit is not None else ()))
        rows = _rows(cur, CHUNK_COLUMNS)
    if not rows:
        return Empty(scope=f"skill_call_chunks call_id={call_id} since={since_seq}"
                           + (f" limit={limit}" if limit is not None else ""))
    return Rows(rows=rows)


# ------------------------------------------------------------------- provenance

def receipt_provenance(conn, call_id: str, grant_id) -> dict:
    """Return receipt provenance pointers from the LIVE columns.

    Two column facts read off the database, not guessed:
      * `invocation_frames.mandate_hash` is TEXT      -> used as-is
      * `skill_grants.mandate_hash`      is BYTEA     -> `encode(..., 'hex')`
    This asymmetric representation is correct because of those two types.

    Each branch is a keyed lookup with `LIMIT 1`, not an aggregate.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "  COALESCE((SELECT n.parent_call_id FROM flow_continuation_nonces n "
            "             WHERE n.consumed_by_call_id = %s "
            "             ORDER BY n.consumed_at NULLS LAST, n.issued_at LIMIT 1), '') "
            "    AS parent_call_id, "
            "  COALESCE((SELECT f.mandate_hash FROM invocation_frames f "
            "             WHERE f.call_id = %s AND f.decision = 'allow' "
            "             ORDER BY f.id DESC LIMIT 1), "
            "           (SELECT encode(g.mandate_hash, 'hex') FROM skill_grants g "
            "             WHERE g.id = %s), '') AS root_mandate_hash, "
            "  COALESCE((SELECT f.chain_id::text FROM invocation_frames f "
            "             WHERE f.call_id = %s AND f.decision = 'allow' "
            "             ORDER BY f.id DESC LIMIT 1), '') AS chain_id",
            (call_id, call_id, str(grant_id) if grant_id else None, call_id))
        row = cur.fetchone()
    # The row is produced by the scalar subqueries and always
    # exists. Read by POSITION into named fields rather than defaulting to None, so a
    # shape change here fails loudly instead of rendering as three empty strings.
    return {"parent_call_id": row[0], "root_mandate_hash": row[1], "chain_id": row[2]}


# ------------------------------------------------------------------------ writes

def cancel_to_abandoned(conn, call_id: str, source_peer_id: uuid.UUID,
                        reason: str | None) -> dict | None:
    """Guarded transition to abandoned, AND its audit row, in ONE transaction.

    `WHERE status = ANY(in_flight)` is the whole point: the DATABASE arbitrates, so a
    cancel racing a target's completion cannot produce a double-terminal write. `None`
    means the race was lost, and the caller re-reads to find out to what.

    server_internal is required because the route has
    already authorized the actor as the call's caller; the escape is what lets the
    server write a row it is not the peer-context party to.

    WHY THE EVENT IS WRITTEN HERE AND NOT BY THE CALLER. These used to be two statements
    on an AUTOCOMMIT connection: the UPDATE committed on its own, and the
    `caller_disconnected` INSERT was a second, independent commit the view issued
    afterwards. Anything between them - a dropped connection, a statement timeout, a
    raise - left the call terminal with NO record of who abandoned it or why, which is
    the one direction an audit row must not fail in. `rls.server_internal` already opens
    a real transaction (it has to, for the LOCAL GUC), so putting the INSERT inside it
    makes the state change and its audit atomic at no extra cost. The event is written
    only when the UPDATE actually won the race.
    """
    cols = ", ".join(CALL_COLUMNS)
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skill_calls SET status = 'abandoned', completed_at = NOW() "
                " WHERE call_id = %s AND status = ANY(%s::text[]) "
                f"RETURNING {cols}", (call_id, list(IN_FLIGHT_STATUSES)))
            rows = _rows(cur, CALL_COLUMNS)
        if not rows:
            return None
        # Module-global lookup on purpose: the audit write stays one named, testable
        # step rather than being inlined into the transition it must accompany.
        insert_caller_disconnected(conn, call_id, source_peer_id, reason)
        # The trim at terminal (divergence 041 S7, design 5.2), in the same transaction.
        # The WS cancel (apps/wsgw/caller_frames.cancel_by_caller) trims, so the HTTP
        # cancellation trims too.
        with conn.cursor() as cur:
            call_chunks.trim(cur, call_id)
    return rows[0]


def insert_caller_disconnected(conn, call_id: str, source_peer_id: uuid.UUID,
                               reason: str | None) -> None:
    """Write the caller-disconnected audit event. `details` is `{"reason": r}` when a reason was given
    and NULL otherwise - not `{}`, which would be a different stored value.

    The live CHECK constraint permits exactly `source='caller'`, `source='target'`, and
    `source='meshy'`; `source='meshy'` is the compatibility value. `source_peer_id` is the
    AUTHENTICATED canceller, so the audit row names who did it rather than trusting the body.

    Called from INSIDE `cancel_to_abandoned`'s transaction, on the same connection, so
    it shares that transaction rather than committing on its own - see the note there.
    """
    details = {"reason": reason} if reason is not None else None
    with conn.cursor() as cur:
        events.insert_event(cur, call_id, "caller_disconnected", details=details,
                            source="caller", source_peer_id=str(source_peer_id))


def touch_last_activity(conn, peer_id: uuid.UUID) -> None:
    """Record recent peer activity.

    The `WHERE` on the DO UPDATE is a write-rate damper, not an optimisation: without
    it every invoke rewrites the row and a busy peer turns presence into a hot-row
    contention point. Ten seconds is the update interval.

    `mesh_presence` has row-level security DISABLED (measured), so this needs no
    context. The caller treats a failure here as non-fatal.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mesh_presence (peer_id, state, last_beat, last_activity, "
            "  updated_at) VALUES (%s, 'active', NULL, NOW(), NOW()) "
            "ON CONFLICT (peer_id) DO UPDATE SET last_activity = NOW() "
            " WHERE mesh_presence.last_activity IS NULL "
            "    OR mesh_presence.last_activity < NOW() - INTERVAL '10 seconds'",
            (str(peer_id),))


__all__ = [
    "CALL_COLUMNS", "CALL_STATUSES", "CHUNK_COLUMNS", "ContextNotBound",
    "DEFAULT_LIST_LIMIT", "Denied", "Empty", "EVENT_COLUMNS", "Found",
    "IN_FLIGHT_STATUSES", "MAX_LIST_LIMIT", "NotFound", "ROLE_CALLER", "ROLE_TARGET",
    "Rows", "cancel_to_abandoned", "for_viewer", "insert_caller_disconnected",
    "list_chunks", "list_events", "list_for_peer", "load_call", "load_call_status",
    "receipt_provenance",
    "touch_last_activity",
]
