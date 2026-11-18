"""Queries against `skill_grants` and `skill_access_requests`.

Column lists were
read from `information_schema.columns` on the live database before a line was written
(skill_grants has 23 columns, skill_access_requests 13; `owners` does not exist - the
owner table is `dashboard_users`, which is what every FK on both tables references).

ABSENCE IS TYPED. Every reader below returns one of

    Found(value) | NotFound(what, key) | Empty(scope)

and NEVER a bare `[]`, `{}` or `None`. There is no `except:` that swallows a failure
into an empty collection: a psycopg error propagates, and the two constraint violations
that are DOMAIN outcomes (`skill_grants_active_unique`,
`skill_access_requests_pending_unique`) are caught BY NAME and re-raised as typed
errors, so an unrelated violation can never be mistaken for a duplicate.

`Empty` carries the scope that was queried rather than collapsing to a falsy value,
because "this owner has issued no grants" and "nothing was visible to this connection"
produce the identical row count. On this surface those cases are distinguishable -
neither table has RLS, measured in db.py - and `db.assert_route_scoped_tables_are_not_
rls_bound` is the control that keeps that true.

NO PER-REQUEST AGGREGATE. Nothing here counts. The lookup a call authorises against
(`find_active_grant`) is the exact four-column key of the partial unique index
`skill_grants_active_unique` and returns at most one row; the list reads are keyed on
`grantor_owner_id`/`grantee_owner_id` (and `requester_owner_id`/`granter_owner_id` for
access requests), each an index range since sql_meshychat/020 (STATUS C47) - before it,
`/v1/access-requests/received` could use no index at all. The lists are NOT bounded BY
DEFAULT: they return an owner's whole history. A caller that passes
`limit` and/or `before` (operator decision 2026-09-25, divergence 045) gets a KEYSET page
instead: newest first by (timestamp DESC, id DESC), at most `limit` rows, older than the row
`before` names, which must be a row of the same owner's list (else `NotFound`). The page
runs two statements when `before` is given: the cursor row's lookup by its primary key and
owner, then the page. A `COUNT(*)` over a chain on every authorize is
an expensive defect measured in a comparable deployment at ~490 GB/day of disk reads,
and the shape
that produces it - `.count()` on a related manager - is exactly what "no ORM here"
prevents. `tests/test_grants.py` reads the QUERY PLAN of the statement each function really
executes (not a retyped copy) and asserts no Aggregate node and an index condition, with
deliberately-bad queries as the positive controls that both are detectable.

THE LIVENESS PREDICATE requires `revoked_at IS NULL` and an expiry that is either NULL or
later than the database's current time. It is the difference between counting
CAPABILITY and counting HISTORY. Filtering on only one of the two arms is the recorded
defect: a revoked grant keeps its row forever, and an expired one is indistinguishable
from a live one on `revoked_at` alone.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

import psycopg

# ---------------------------------------------------------------- typed outcomes


@dataclass(frozen=True)
class Found:
    value: Any


@dataclass(frozen=True)
class NotFound:
    what: str
    key: str


@dataclass(frozen=True)
class Empty:
    """A read that matched no rows. `scope` names what was queried, so a caller (or a
    log line) can tell an empty result from an unbound one."""
    scope: str


class DuplicateActiveGrant(Exception):
    """`skill_grants_active_unique` tripped."""


class DuplicatePendingRequest(Exception):
    """`skill_access_requests_pending_unique` tripped."""


class OwnershipGuardViolation(Exception):
    """The DB trigger `skill_grants_granter_ownership_guard_trg` refused the row.

    The route already checks the same IG4 coupling before inserting, so reaching this
    means the two disagree (a race, or a route bug). It is surfaced rather than mapped
    to the route's own 403, so the disagreement is visible instead of silently papered
    over as if the route had caught it.
    """


# -------------------------------------------------------------------- row types


@dataclass(frozen=True)
class GrantRow:
    id: Any
    grantor_owner_id: Any
    grantee_owner_id: Any
    granter_peer_id: Any
    skill_name: str
    version_pin: str | None
    source: str
    source_ref: str | None
    scopes: Any
    granted_at: _dt.datetime
    expires_at: _dt.datetime | None
    revoked_at: _dt.datetime | None
    revoked_by: Any
    revoke_reason: str | None
    max_executions: int | None
    used_executions: int
    max_spend_cents: int | None
    used_spend_cents: int
    freshness_ceiling_secs: int | None
    approved_manifest_hash: Any
    approved_requires_human_auth: bool


@dataclass(frozen=True)
class RequestRow:
    id: Any
    requester_owner_id: Any
    granter_owner_id: Any
    granter_peer_id: Any
    skill_name: str
    message: str | None
    status: str
    decided_at: _dt.datetime | None
    decided_by: Any
    decision_note: str | None
    resulting_grant_id: Any
    created_at: _dt.datetime
    expires_at: _dt.datetime


#: The RETURNING list, in the stable SkillGrant field order.
GRANT_COLUMNS = """id, grantor_owner_id, grantee_owner_id, granter_peer_id,
                   skill_name, version_pin, source, source_ref, scopes,
                   granted_at, expires_at, revoked_at, revoked_by, revoke_reason,
                   max_executions, used_executions,
                   max_spend_cents, used_spend_cents, freshness_ceiling_secs,
                   approved_manifest_hash, approved_requires_human_auth"""

REQUEST_COLUMNS = """id, requester_owner_id, granter_owner_id, granter_peer_id,
                     skill_name, message, status, decided_at, decided_by,
                     decision_note, resulting_grant_id, created_at, expires_at"""


def _grant(row) -> GrantRow:
    return GrantRow(*row)


def _request(row) -> RequestRow:
    return RequestRow(*row)


def _constraint_name(exc: psycopg.errors.IntegrityError) -> str | None:
    diag = getattr(exc, "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None


# --------------------------------------------------------------------- grants


def find_grant_by_id(conn, grant_id):
    """Find a grant by id."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {GRANT_COLUMNS} FROM skill_grants WHERE id = %s", (str(grant_id),))
        row = cur.fetchone()
    return Found(_grant(row)) if row else NotFound("grant", str(grant_id))


def find_active_grant(conn, grantor_owner_id, grantee_owner_id, granter_peer_id, skill_name):
    """Find the live grant used for authorization.

    The four columns ARE `skill_grants_active_unique`'s key, so this is an index probe
    returning at most one row, and the liveness predicate is both arms.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {GRANT_COLUMNS} FROM skill_grants
                 WHERE grantor_owner_id = %s
                   AND grantee_owner_id = %s
                   AND granter_peer_id = %s
                   AND skill_name = %s
                   AND revoked_at IS NULL
                   AND (expires_at IS NULL OR expires_at > NOW())""",
            (str(grantor_owner_id), str(grantee_owner_id), str(granter_peer_id), skill_name))
        row = cur.fetchone()
    if row:
        return Found(_grant(row))
    return Empty(f"live grant ({grantor_owner_id} -> {grantee_owner_id}) on "
                 f"{granter_peer_id}/{skill_name}")


def insert_grant(conn, *, grantor_owner_id, grantee_owner_id, granter_peer_id, skill_name,
                 version_pin, approved_manifest_hash, source, source_ref, expires_at,
                 scopes, max_executions, approved_requires_human_auth):
    """Insert a grant.

    `version_pin` and `approved_manifest_hash` travel together: pinned means BOTH are
    set, unpinned means both are NULL. They
    are written in ONE statement so there is no window where a version_pin exists with
    a NULL hash - the invoke gate SKIPS a NULL hash, so that window is a rug-pull hole.
    """
    import json

    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""INSERT INTO skill_grants
                    (grantor_owner_id, grantee_owner_id, granter_peer_id, skill_name,
                     version_pin, source, source_ref, scopes, expires_at, max_executions,
                     approved_manifest_hash, approved_requires_human_auth)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING {GRANT_COLUMNS}""",
                (str(grantor_owner_id), str(grantee_owner_id), str(granter_peer_id), skill_name,
                 version_pin, source, source_ref,
                 None if scopes is None else json.dumps(scopes),
                 expires_at, max_executions,
                 None if approved_manifest_hash is None else bytes(approved_manifest_hash),
                 approved_requires_human_auth))
            row = cur.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            if _constraint_name(exc) == "skill_grants_active_unique":
                raise DuplicateActiveGrant() from exc
            raise
        except psycopg.errors.CheckViolation as exc:
            # The IG4 trigger raises with ERRCODE check_violation and no constraint
            # name, so it is identified by its message rather than by _constraint_name.
            if "skill_grants IG4" in str(exc) or "unknown grant source" in str(exc):
                raise OwnershipGuardViolation(str(exc)) from exc
            raise
    return Found(_grant(row))


def revoke_grant(conn, grant_id, revoked_by, reason):
    """`AND revoked_at IS NULL` makes this compare-and-set:
    no row means someone else revoked it first, never 'clobber the earlier reason'."""
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE skill_grants
                   SET revoked_at = NOW(), revoked_by = %s, revoke_reason = %s
                 WHERE id = %s AND revoked_at IS NULL
             RETURNING {GRANT_COLUMNS}""",
            (str(revoked_by), reason, str(grant_id)))
        row = cur.fetchone()
    return Found(_grant(row)) if row else NotFound("unrevoked grant", str(grant_id))


def update_grant(conn, grant_id, grantor_owner_id, *, update_expires_at, new_expires_at,
                 update_version_pin, new_version_pin, update_manifest_hash,
                 new_manifest_hash, update_approved_human_auth, new_approved_human_auth):
    """Update a live grant owned by the grantor.

    `WHERE … AND grantor_owner_id = %s AND revoked_at IS NULL` is why a missing row,
    a revoked row and ANOTHER OWNER's row are one outcome: 404. That is deliberate: no
    cross-owner leakage. It is the opposite of the revoke path, which answers 403 for
    the wrong owner.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE skill_grants
                   SET expires_at                   = CASE WHEN %s THEN %s ELSE expires_at END,
                       version_pin                  = CASE WHEN %s THEN %s ELSE version_pin END,
                       approved_manifest_hash       = CASE WHEN %s THEN %s
                                                           ELSE approved_manifest_hash END,
                       approved_requires_human_auth = CASE WHEN %s THEN %s
                                                           ELSE approved_requires_human_auth END
                 WHERE id = %s AND grantor_owner_id = %s AND revoked_at IS NULL
             RETURNING {GRANT_COLUMNS}""",
            (update_expires_at, new_expires_at,
             update_version_pin, new_version_pin,
             update_manifest_hash,
             None if new_manifest_hash is None else bytes(new_manifest_hash),
             update_approved_human_auth, new_approved_human_auth,
             str(grant_id), str(grantor_owner_id)))
        row = cur.fetchone()
    return Found(_grant(row)) if row else NotFound(
        "live grant owned by this grantor", f"{grant_id}/{grantor_owner_id}")


def grant_is_mandate_bound(conn, grant_id) -> bool:
    """Return False when the grant is missing or has no mandate hash."""
    with conn.cursor() as cur:
        cur.execute("SELECT mandate_hash IS NOT NULL FROM skill_grants WHERE id = %s",
                    (str(grant_id),))
        row = cur.fetchone()
    return bool(row[0]) if row else False


def set_mandate_provenance(conn, grant_id, mandate_hash32: bytes, signature64: bytes) -> None:
    """Store the hash and signature as provenance after issuance."""
    with conn.cursor() as cur:
        cur.execute("UPDATE skill_grants SET mandate_hash = %s, mandate_sig = %s WHERE id = %s",
                    (bytes(mandate_hash32), bytes(signature64), str(grant_id)))


#: The most rows one keyset page returns (divergence 045); views.py refuses a larger `limit`.
MAX_PAGE_LIMIT = 500


def _page(cur, *, table: str, columns: str, owner_column: str, stamp: str, owner,
          where: str, params: tuple, limit, before):
    """One keyset page (divergence 045) of `table` for `owner`: `where` (with `params`) as the
    unpaged read has it, newest first by (`stamp` DESC, id DESC), at most `limit` rows (None:
    all), older than the row `before` names (None: from the newest). Returns the rows, or
    NotFound when `before` names no row of this owner's list. Identifiers are this module's
    constants, never input."""
    after = ""
    after_params: tuple = ()
    if before is not None:
        cur.execute(f"SELECT {stamp}, id FROM {table} WHERE id = %s AND {owner_column} = %s",
                    (str(before), str(owner)))
        found = cur.fetchone()
        if found is None:
            return NotFound(f"{table} row of {owner_column} {owner}", str(before))
        after = f" AND {stamp} <= %s AND ({stamp} < %s OR id < %s)"
        after_params = (found[0], found[0], found[1])
    cur.execute(
        f"SELECT {columns} FROM {table} WHERE {owner_column} = %s AND {where}{after} "
        f"ORDER BY {stamp} DESC, id DESC LIMIT %s",
        (str(owner), *params, *after_params, limit))
    return cur.fetchall()


def _owner_list(conn, unpaged: str, row, scope: str, *, table: str, columns: str,
                owner_column: str, stamp: str, owner, where: str, params: tuple, limit, before):
    """The four owner-keyed list reads: `unpaged` (the whole history, one statement, which
    each caller spells out so its SQL text stays its own) when neither `limit` nor `before`
    is given, else `_page`. Rows map through `row`; no rows is `Empty(scope)`, and a
    `before` naming no row of this owner's list is `_page`'s NotFound, passed through."""
    with conn.cursor() as cur:
        if limit is None and before is None:
            cur.execute(unpaged, (str(owner), *params))
            rows = cur.fetchall()
        else:
            rows = _page(cur, table=table, columns=columns, owner_column=owner_column,
                         stamp=stamp, owner=owner, where=where, params=params,
                         limit=limit, before=before)
            if isinstance(rows, NotFound):
                return rows
    if not rows:
        return Empty(scope)
    return Found([row(r) for r in rows])


def list_issued(conn, grantor_owner_id, include_revoked: bool, *, limit=None, before=None):
    """List issued grants, with a keyset page when `limit` or `before` is given."""
    return _owner_list(
        conn,
        f"""SELECT {GRANT_COLUMNS} FROM skill_grants
                     WHERE grantor_owner_id = %s AND (%s OR revoked_at IS NULL)
                     ORDER BY granted_at DESC""",
        _grant, f"grants issued by {grantor_owner_id} (include_revoked={include_revoked})",
        table="skill_grants", columns=GRANT_COLUMNS, owner_column="grantor_owner_id",
        stamp="granted_at", owner=grantor_owner_id, where="(%s OR revoked_at IS NULL)",
        params=(include_revoked,), limit=limit, before=before)


def list_held(conn, grantee_owner_id, include_revoked: bool, *, limit=None, before=None):
    """List held grants, with a keyset page when `limit` or `before` is given."""
    return _owner_list(
        conn,
        f"""SELECT {GRANT_COLUMNS} FROM skill_grants
                     WHERE grantee_owner_id = %s AND (%s OR revoked_at IS NULL)
                     ORDER BY granted_at DESC""",
        _grant, f"grants held by {grantee_owner_id} (include_revoked={include_revoked})",
        table="skill_grants", columns=GRANT_COLUMNS, owner_column="grantee_owner_id",
        stamp="granted_at", owner=grantee_owner_id, where="(%s OR revoked_at IS NULL)",
        params=(include_revoked,), limit=limit, before=before)


# ------------------------------------------------------------- access requests


def peer_belongs_to_owner(conn, peer_id, owner_id) -> bool:
    """Check the IG4 coupling with an index
    probe on the primary key. Not a count."""
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS(SELECT 1 FROM peers WHERE id = %s AND owner_id = %s)",
                    (str(peer_id), str(owner_id)))
        return bool(cur.fetchone()[0])


def insert_request(conn, *, requester_owner_id, granter_owner_id, granter_peer_id,
                   skill_name, message, expires_at):
    """Insert an access request."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""INSERT INTO skill_access_requests
                    (requester_owner_id, granter_owner_id, granter_peer_id, skill_name,
                     message, expires_at)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    RETURNING {REQUEST_COLUMNS}""",
                (str(requester_owner_id), str(granter_owner_id), str(granter_peer_id),
                 skill_name, message, expires_at))
            row = cur.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            if _constraint_name(exc) == "skill_access_requests_pending_unique":
                raise DuplicatePendingRequest() from exc
            raise
    return Found(_request(row))


def find_request_by_id(conn, request_id):
    """Find an access request by id."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {REQUEST_COLUMNS} FROM skill_access_requests WHERE id = %s",
                    (str(request_id),))
        row = cur.fetchone()
    return Found(_request(row)) if row else NotFound("access request", str(request_id))


def update_request_decision(conn, request_id, status, decided_by, note, resulting_grant_id):
    """Set a pending access request's decision.

    `AND status = 'pending'` is a COMPARE-AND-SET, not a convenience filter. No row
    means a concurrent approve/deny/withdraw/expire won the race; the caller must roll
    its transaction back rather than clobber a terminal decision - which, on the
    approve path, is what stops a DENIED request ending up with a live grant.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE skill_access_requests
                   SET status = %s, decided_at = NOW(), decided_by = %s,
                       decision_note = %s, resulting_grant_id = %s
                 WHERE id = %s AND status = 'pending'
             RETURNING {REQUEST_COLUMNS}""",
            (status, str(decided_by), note,
             None if resulting_grant_id is None else str(resulting_grant_id),
             str(request_id)))
        row = cur.fetchone()
    return Found(_request(row)) if row else NotFound("pending access request", str(request_id))


def expire_request(conn, request_id):
    """Expire a pending request. System-driven, so `decided_by` stays NULL."""
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE skill_access_requests
                   SET status = 'expired', decided_at = NOW()
                 WHERE id = %s AND status = 'pending'
             RETURNING {REQUEST_COLUMNS}""",
            (str(request_id),))
        row = cur.fetchone()
    return Found(_request(row)) if row else NotFound("pending access request", str(request_id))


def list_requests_sent(conn, requester_owner_id, status_filter, *, limit=None, before=None):
    """List sent requests, with a keyset page when `limit` or `before` is given."""
    return _owner_list(
        conn,
        f"""SELECT {REQUEST_COLUMNS} FROM skill_access_requests
                     WHERE requester_owner_id = %s
                       AND (%s::text IS NULL OR status = %s)
                     ORDER BY created_at DESC""",
        _request, f"requests sent by {requester_owner_id} (status={status_filter})",
        table="skill_access_requests", columns=REQUEST_COLUMNS,
        owner_column="requester_owner_id", stamp="created_at", owner=requester_owner_id,
        where="(%s::text IS NULL OR status = %s)", params=(status_filter, status_filter),
        limit=limit, before=before)


def list_requests_received(conn, granter_owner_id, status_filter, *, limit=None, before=None):
    """List received requests, with a keyset page when `limit` or `before` is given."""
    return _owner_list(
        conn,
        f"""SELECT {REQUEST_COLUMNS} FROM skill_access_requests
                     WHERE granter_owner_id = %s
                       AND (%s::text IS NULL OR status = %s)
                     ORDER BY created_at DESC""",
        _request, f"requests received by {granter_owner_id} (status={status_filter})",
        table="skill_access_requests", columns=REQUEST_COLUMNS,
        owner_column="granter_owner_id", stamp="created_at", owner=granter_owner_id,
        where="(%s::text IS NULL OR status = %s)", params=(status_filter, status_filter),
        limit=limit, before=before)


# ------------------------------------------------------------------- skills


def find_skill_for_call(conn, peer_id, name, version_pin):
    """Find the requested skill version, restricted to the columns the manifest hash, the
    human-auth capture, and the publisher attestation (the
    signature pair `verify_manifest_attestation` checks, B09) need.

    version_pin None  -> the CURRENT, non-deleted row.
    version_pin '=x'  -> '=' is a redundant exact-match marker; stripped.
    version_pin '^~><'-> ValueError: ranges defer to SP3 and are an ERROR,
                         never a silent fall-through to the current version.
    """
    columns = ("peer_id, name, version, mode, description, input_schema, output_schema, "
               "attachments_allowed, returns_attachments, requires_human_auth, tags, "
               "runtime_profile, manifest_sig, signing_kid")
    if version_pin is None:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM skills "
                " WHERE peer_id = %s AND name = %s AND is_current = TRUE AND deleted_at IS NULL",
                (str(peer_id), name))
            row = cur.fetchone()
        if row is None:
            return NotFound("current skill", f"{peer_id}/{name}")
    else:
        if version_pin[:1] in ("^", "~", ">", "<"):
            raise ValueError(f"semver range pins are not supported: {version_pin}")
        trimmed = version_pin.lstrip("=")
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM skills "
                " WHERE peer_id = %s AND name = %s AND version = %s AND deleted_at IS NULL",
                (str(peer_id), name, trimmed))
            row = cur.fetchone()
        if row is None:
            return NotFound("skill version", f"{peer_id}/{name}@{version_pin}")
    keys = [c.strip() for c in columns.split(",")]
    return Found(dict(zip(keys, row)))


def active_human_keys(conn, owner_id):
    """Return kid and public_key for every UNREVOKED key.

    The revoked filter is in the WHERE clause, security load-bearing: a revoked human
    key must never re-authorize a mandate.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kid, public_key FROM owner_human_keys "
            " WHERE owner_id = %s AND revoked_at IS NULL ORDER BY created_at, kid",
            (str(owner_id),))
        rows = cur.fetchall()
    if not rows:
        return Empty(f"active human keys for {owner_id}")
    return Found(rows)
