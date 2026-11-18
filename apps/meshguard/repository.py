"""All SQL for the meshguard surface, with TYPED absence.

HOUSE RULE 3 IS THE ORGANISING PRINCIPLE HERE. Nothing below returns a bare `[]`, `{}` or
`None` to mean "nothing". Every lookup returns either a value or one of the two absence
types in this module:

    NotFound(what, key)  the row does not exist, or is not the caller's to see
    Empty(scope)         the query RAN and matched zero rows

and a refused write returns `Duplicate(what, constraint)` rather than leaking the
`UniqueViolation`. They are distinguishable at the call site because they mean different
things on the wire (404 vs `{"items": []}` vs 409), and because a bare `[]` is also what
an errored query, an unbound RLS context and a typo'd table name produce. `Empty` carries
the scope string it queried so a log line says WHICH search space was empty - a negative
result with no statement of the search space is not a result.

This surface's two 403s - resolving your own approval and attaching at `server_default`
scope - are decided and raised by the ROUTE, not returned from here: no function in this module ever produced
the `Denied` type that once stood for them, so it was deleted for the reason
docs/REVIEW-FIX-QUEUE.md items 261 and 498 give for the dashboard and skillsreg copies
("produced and consumed nowhere").

The same rule governs the columns: every read below names its columns explicitly and
reads them by NAME from a dict row, never by position, so adding a column cannot silently
shift a value into the wrong field.

SCHEMA MEASURED, NOT GUESSED (information_schema.columns / pg_constraint / pg_indexes on
meshy_django, 2026-09-21). The facts that shaped this file:

  * `meshy_guard_approvals.approval_id` has CHECK `^appr_[0-9a-f]{32,}$`
    (`meshy_guard_approvals_id_format`); policies `^rgp_[0-9a-f]{32}$`; attachments
    `^rga_[0-9a-f]{32}$` (`rgpa_id_shape`). The generators below produce exactly 32 hex
    chars, so a 16-byte id is the only shape that satisfies all three.
  * `meshy_guard_approvals_status_verdict` CHECK ties status to verdict pairwise
    (approved/accept, denied/decline, cancelled/cancel), and
    `meshy_guard_approvals_resolution_shape` requires resolved_at + resolver_peer_id +
    verdict together. A resolve that sets only `status` violates the second.
  * `rga_pending_elicitation_idx` is UNIQUE on `(owner_id, elicitation_key) WHERE
    status='pending' AND elicitation_key IS NOT NULL` - that partial unique index is what
    makes the ON CONFLICT re-attach in `insert_ask_approval` legal.
  * `rgpa_scope_owner_shape` CHECK: server_default => owner_id NULL AND session_key NULL;
    owner => owner_id NOT NULL AND session_key NULL; session => both NOT NULL. The route
    validation mirrors it so the client gets a 400 with a message instead of a 500 from a
    constraint violation.
  * `rgpa_unique_binding` is UNIQUE on `policy_id`, `scope`, owner_id coalesced with the
    all-zero UUID, and session_key coalesced with the empty string. THIS ONE
    WAS MISSING FROM THIS CENSUS and `attach_policy` was written as if no uniqueness
    existed, so a repeated POST of the same binding was an unhandled `UniqueViolation`
    and a 500. The two COALESCEs are what make it bite for `owner` scope, where
    `session_key` is NULL: a plain multi-column UNIQUE would not, because NULLs do not
    compare equal.
  * `meshy_guard_policy_attachments.phases` is `text[]` with CHECK
    `phases <@ ARRAY['tool_call','flow','request'] AND cardinality(phases) > 0`.
  * `meshy_guard_policies.name` is UNIQUE (`meshy_guard_policies_name_key`), which is the
    conflict target the seed upsert needs.
  * NO table here has RLS (see `db.py`). Owner scoping is in the WHERE clause, always
    from the token-derived owner.

HOUSE RULE 4 (no per-request full-table/full-chain aggregate). Two places could have
become one and did not:

  * the approval queue's `pending_count` is `COUNT(*) ... WHERE owner_id = %s AND
    status = 'pending' AND expires_at > NOW()`. It is not a full-table count: it is
    served by the partial index `meshy_guard_approvals_owner_pending_idx` on
    `(owner_id, created_at) WHERE status = 'pending'`, so it scans ONE owner's pending
    cards - a set the expiry sweep keeps bounded. MEASURED (not asserted in prose) by
    `test_pending_count_is_bounded_by_the_owners_queue_not_the_table`, which
    EXPLAIN-ANALYZEs `PENDING_COUNT_SQL` itself and compares the rows actually read against
    an unscoped count over the same table.
  * the pending-scan queue uses `EXISTS (SELECT 1 FROM invocation_frames ...)`, not a
    count over the chain. `EXISTS` stops at the first matching frame; `COUNT(*)` over a
    chain is precisely the ~490 GB/day defect named in the house rules.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from apps.crypto import primitives as P

# ------------------------------------------------------------------ typed absence


@dataclass(frozen=True)
class NotFound:
    """The row does not exist, or the caller is not entitled to learn that it does.

    "Absent" and "outside your tenant" produce the same 404 so the response does not
    disclose existence. The two are still distinguished HERE, in `what`, because a log
    line needs the difference.
    """

    what: str
    key: str | None = None


@dataclass(frozen=True)
class Duplicate:
    """A UNIQUE index refused the write: the row the caller asked for already exists.

    A typed outcome rather than a leaked `psycopg.errors.UniqueViolation`, for the same
    reason `NotFound` exists: the view must decide the status code, and an exception that
    escapes `_route` is Django's HTML 500 - which, with MESHY_DEBUG on, renders the DSN
    and every column of the attempted row into the response body.

    `constraint` is the index name Postgres reported, so a log line says WHICH uniqueness
    was violated. The branch is on the EXCEPTION TYPE and the `sqlstate`, never on the
    message text: message wording is a Postgres version detail, the 23505 class is not.
    """

    what: str
    constraint: str | None = None


@dataclass(frozen=True)
class Empty:
    """The query ran and matched zero rows. NOT an error, NOT a bare [].

    `scope` states the search space that was empty, so "no rows" is never reported
    without saying where it looked.
    """

    scope: str

    def rows(self) -> list:
        """The wire value. Explicit, so a view converting Empty to [] is a visible step."""
        return []


@dataclass(frozen=True)
class Rows:
    """A non-empty result set. Paired with `Empty` so a caller must handle both."""

    items: list

    def rows(self) -> list:
        return self.items


def rows_or_empty(items: Sequence, scope: str) -> Rows | Empty:
    return Rows(list(items)) if items else Empty(scope)


# ----------------------------------------------------------------------- id/hash

APPROVAL_PREVIEW_MAX_CHARS = 1024      # Schema preview limit.
DEFAULT_ASK_TIMEOUT_SECS = 86_400      # Default approval timeout.


def _prefixed_id(prefix: str) -> str:
    """Return prefix plus 16 random bytes in hex.

    `secrets.token_hex(16)` draws from the OS CSPRNG and yields the exactly-32 lowercase
    hex chars the three CHECK constraints require.
    """
    return prefix + secrets.token_hex(16)


def generate_approval_id() -> str:
    return _prefixed_id("appr_")


def generate_policy_id() -> str:
    return _prefixed_id("rgp_")


def generate_attachment_id() -> str:
    return _prefixed_id("rga_")


def truncate_content_preview(raw: str) -> str:
    """Truncate by CHARACTERS, not bytes. The CHECK is
    `char_length(content_preview) <= 1024`, so slicing bytes would over-truncate a
    multibyte preview and under-satisfy nothing."""
    return raw[:APPROVAL_PREVIEW_MAX_CHARS]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def approval_args_hash(input_args: Any) -> str:
    """Return sha256 over the JCS canonicalisation of the args.

    `apps.crypto.primitives.jcs` is the house canonicaliser; the signature paths hash
    the same bytes.
    """
    return _sha256_hex(P.jcs(input_args))


def derive_hook_elicitation_key(owner_id, session_key: str, skill_name: str,
                                input_digest: str, attachment_id: str) -> dict:
    """Derive the hook elicitation key.

    The tool-use id is deliberately NOT in the material: harnesses mint a fresh one per
    retry, so including it would open a new approval card for every retry of the same
    action. `args_hash` carries the client digest verbatim, which is what binds the
    release to the exact action a human approved.
    """
    material = f"hook:{owner_id}:{session_key}:{skill_name}:{input_digest}:{attachment_id}"
    return {
        "args_hash": input_digest,
        "elicitation_key": _sha256_hex(material.encode("utf-8")),
    }


# ------------------------------------------------------------------------ columns

#: Approval columns, named explicitly so a schema addition cannot silently change this read.
_APPROVAL_COLUMNS = (
    "approval_id, status, owner_id, caller_peer_id, skill_id, call_id, phase, policy, "
    "reason_code, message, content_preview, manifest_hash, sandbox_status, created_at, "
    "expires_at, resolved_at, resolver_peer_id, verdict, audit_event_id, policy_id, "
    "attachment_id, elicitation_key, approved_args_hash, consumed_at"
)

_POLICY_COLUMNS = (
    "policy_id, name, kind, handler_ref, version, description, internal_only, enabled, "
    "params_schema, created_at, updated_at"
)

_ATTACHMENT_COLUMNS = (
    "attachment_id, policy_id, scope, owner_id, session_key, params, phases, enabled, "
    "ordinal, ask_timeout_secs, created_by_peer_id, created_at, updated_at"
)

_DECISION_COLUMNS = (
    "decision_id, policy_id, attachment_id, decision, reason, phase, mode, call_id, "
    "owner_id, caller_peer_id, target_peer_id, skill_name, session_key, approval_id, "
    "created_at"
)


def _dict_cursor(conn):
    return conn.cursor(row_factory=dict_row)


# -------------------------------------------------------------------- identities


def owner_for_peer(conn, peer_id) -> str | NotFound:
    """Resolve an active peer's owner. RG4: `status = 'active'` is part of the predicate.

    A suspended or deleted peer keeps a valid unexpired JWT, so the token alone must not
    reach this surface. Returns NotFound (-> 404) rather than raising, so the caller
    decides the status code.
    """
    with _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT owner_id FROM peers WHERE id = %s AND status = 'active'",
            (str(peer_id),),
        )
        row = cur.fetchone()
    if row is None:
        return NotFound("active peer", str(peer_id))
    return str(row["owner_id"])


def owner_for_skill(conn, skill_id) -> str | NotFound:
    """Resolve a skill's owner, requiring the owning peer to be active."""
    with _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT p.owner_id FROM skills s JOIN peers p ON p.id = s.peer_id "
            "WHERE s.id = %s AND p.status = 'active'",
            (str(skill_id),),
        )
        row = cur.fetchone()
    if row is None:
        return NotFound("skill owner", str(skill_id))
    return str(row["owner_id"])


def same_tenant_or_devgroup(conn, caller_owner: str, target_owner: str) -> bool:
    """Apply the migration-039 SQL tenancy predicate.

    MEASURED definition (pg_get_functiondef):

        SELECT caller = target
            OR EXISTS (SELECT 1 FROM dashboard_users c, dashboard_users t
                       WHERE c.id = caller AND t.id = target
                         AND c.plane = 'dev' AND t.plane = 'dev'
                         AND c.trust_group_id IS NOT NULL
                         AND c.trust_group_id = t.trust_group_id);

    Note the table is `dashboard_users`, not `owners` - that mistake is on the house
    rules' list of guesses that cost this project a failed run. Verified callable as
    `meshy_app` (now `meshychat_app`; it is SECURITY DEFINER).

    FAIL CLOSED: a NULL result denies. A database error is NOT caught here - it
    propagates because "the tenancy check could not run" must never read as "the tenancy
    check passed".
    """
    with _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT meshychat_same_tenant_or_devgroup(%s, %s) AS allowed",
            (str(caller_owner), str(target_owner)),
        )
        row = cur.fetchone()
    return bool(row and row["allowed"])


# ------------------------------------------------------------------- RG reviews


def skill_review_json(conn, skill_id) -> Any | NotFound:
    """Return the skill review JSON, or NotFound."""
    with _dict_cursor(conn) as cur:
        cur.execute("SELECT meshychat_guard_review FROM skills WHERE id = %s", (str(skill_id),))
        row = cur.fetchone()
    if row is None:
        return NotFound("skill review", str(skill_id))
    return row["meshychat_guard_review"]


def meshy_memory_review_json(conn, owner_id, key: str) -> Any | NotFound:
    """Return the memory review JSON, keyed by `(created_by_owner_id, key)`:
    `meshy_memory` names its owner column `created_by_owner_id` (measured), and that is
    also the column `sql_meshychat/001` binds its insert policy to."""
    with _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT meshychat_guard_review FROM meshychat_memory "
            "WHERE created_by_owner_id = %s AND key = %s",
            (str(owner_id), key),
        )
        row = cur.fetchone()
    if row is None:
        return NotFound("meshychat_memory review", key)
    return row["meshychat_guard_review"]


# ------------------------------------------------------------------ pending scans


def pending_scan_results_for_owner(conn, owner_id, limit: int) -> Rows | Empty:
    """Return an owner's pending scan results.

    EXISTS, not COUNT: the chain-membership test stops at the first matching frame. A
    `COUNT(*)` over `invocation_frames` per request would scan the whole chain.
    """
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT s.id, s.chain_id, s.conclusion, s.score, s.review_status,
                   s.findings, s.scanner_version, s.signing_kid
            FROM meshychat_guard_scan_results s
            WHERE s.review_status = 'requires_review'
              AND EXISTS (
                  SELECT 1 FROM invocation_frames f
                  WHERE f.chain_id = s.chain_id AND f.caller_owner_id = %s
              )
            ORDER BY s.occurred_at DESC
            LIMIT %s
            """,
            (str(owner_id), int(limit)),
        )
        rows = cur.fetchall()
    out = [
        {
            "id": r["id"],
            "chain_id": str(r["chain_id"]),
            "conclusion": r["conclusion"],
            "score": r["score"],
            "review_status": r["review_status"],
            "findings": r["findings"],
            "scanner_version": r["scanner_version"],
            "signing_kid": r["signing_kid"],
        }
        for r in rows
    ]
    return rows_or_empty(out, f"meshychat_guard_scan_results requires_review for owner {owner_id}")


# --------------------------------------------------------------------- approvals


def _expire_pending_for_owner(conn, owner_id) -> None:
    """Expire this owner's pending approvals. Owner-scoped and index-backed
    (`meshy_guard_approvals_owner_pending_idx`), run before any read of the queue so an
    expired card is never shown as pending."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE meshychat_guard_approvals "
            "SET status = 'expired', resolved_at = COALESCE(resolved_at, NOW()) "
            "WHERE owner_id = %s AND status = 'pending' AND expires_at <= NOW()",
            (str(owner_id),),
        )


def _expire_pending_by_id(conn, approval_id: str) -> None:
    """Expire one pending approval by id.

    A WRITE keyed on nothing but the id, so it carries no tenancy of its own. It is
    private, and the only caller is `expire_and_reload_approval` below, which the routes
    reach only AFTER `_require_same_tenant` has passed. Calling it from a lookup that
    runs before that check - which is what `approval_by_id` used to be - lets any
    authenticated peer in any tenant drive `pending -> expired` on a row it may not even
    see, by naming its id.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE meshychat_guard_approvals "
            "SET status = 'expired', resolved_at = COALESCE(resolved_at, NOW()) "
            "WHERE approval_id = %s AND status = 'pending' AND expires_at <= NOW()",
            (approval_id,),
        )


#: The queue-depth count, as a module constant so `tests/test_meshguard.py` can MEASURE
#: the statement this function actually issues rather than a copy of it. A test that
#: EXPLAINs its own transcription of a query proves the transcription, not the code.
PENDING_COUNT_SQL = (
    "SELECT COUNT(*) AS n FROM meshychat_guard_approvals "
    "WHERE owner_id = %s AND status = 'pending' AND expires_at > NOW()"
)


def pending_approvals_for_owner(conn, owner_id, limit: int) -> dict:
    """Return `ApprovalQueueResponse {items, pending_count}`.

    `pending_count` is the count of the owner's LIVE pending cards, not of `items`: the
    two differ when the queue is longer than `limit`, and a client paging the queue needs
    the real depth. See the module docstring for why this COUNT is not house-rule-4
    shaped.
    """
    _expire_pending_for_owner(conn, owner_id)
    with _dict_cursor(conn) as cur:
        cur.execute(PENDING_COUNT_SQL, (str(owner_id),))
        pending_count = int(cur.fetchone()["n"])
        cur.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM meshychat_guard_approvals "
            "WHERE owner_id = %s AND status = 'pending' AND expires_at > NOW() "
            "ORDER BY created_at ASC LIMIT %s",
            (str(owner_id), int(limit)),
        )
        rows = cur.fetchall()
    return {"items": [dict(r) for r in rows], "pending_count": pending_count}


def approval_row(conn, approval_id: str) -> dict | NotFound:
    """A PURE READ of one card: no sweep, no write, nothing a caller could drive.

    The route may run this BEFORE it knows who is asking - it is what the tenancy check
    needs `owner_id` from. This is the read-only half of expiring and reloading a card;
    the sweep half is `expire_and_reload_approval`. Splitting them is the whole point: one function
    that read and wrote could only ever be called on one side of the check, and it was
    being called on the wrong one.
    """
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM meshychat_guard_approvals WHERE approval_id = %s",
            (approval_id,),
        )
        row = cur.fetchone()
    if row is None:
        return NotFound("approval", approval_id)
    return dict(row)


def expire_and_reload_approval(conn, approval_id: str) -> dict | NotFound:
    """Sweep this card, then read it back.

    CALL ONLY AFTER THE CALLER IS AUTHORISED FOR THIS ROW'S TENANT. The sweep is a state
    transition; it is bounded to rows already past `expires_at`, but a bounded write is
    still a write, and a 404 that has already moved a row is not a refusal.
    """
    _expire_pending_by_id(conn, approval_id)
    return approval_row(conn, approval_id)


#: `resolved_status`. The pairing is also a DATABASE constraint
#: (`meshy_guard_approvals_status_verdict`), so a wrong pairing is a 500, not a bad row.
_VERDICT_STATUS = {"accept": "approved", "decline": "denied", "cancel": "cancelled"}


def resolve_approval(conn, approval_id: str, owner_id, resolver_peer_id,
                     verdict: str) -> dict | NotFound:
    """Resolve an approval in ONE transaction, in this order:

      1. resolve it if it is still pending AND unexpired;
      2. otherwise, if it is pending but expired, mark it expired and return that;
      3. otherwise return whatever it currently is.

    Step 2 is not bookkeeping: without it, a request that arrives one second after expiry
    would return the row still labelled `pending`, and a client would retry forever. The
    UPDATE sets resolved_at + resolver_peer_id + verdict TOGETHER because
    `meshy_guard_approvals_resolution_shape` requires all three whenever status leaves the
    pending/expired pair.

    `conn` is autocommit, so the transaction is explicit here (`conn.transaction()`), not
    implicit. The read in step 3 is inside it so a concurrent resolve cannot be observed
    half-applied.

    ALL THREE STATEMENTS CARRY `owner_id = %s`, the way `set_attachment_enabled` and
    `remove_attachment` do. `owner_id` is the owner the ROUTE authorised - the approval
    row's own owner, re-asserted at the write - and NOT the caller's, because
    `same_tenant_or_devgroup` deliberately admits a dev-group sibling and binding the
    caller's owner would break that case. It is defence in depth and it is load-bearing
    here: no `meshy_guard_*` table has row-level security (measured, see `db.py`), so
    without this predicate the route is the only thing between an approval id and a
    resolution, and one regression in the view is total.
    """
    status = _VERDICT_STATUS[verdict]
    owner = str(owner_id)
    with conn.transaction():
        with _dict_cursor(conn) as cur:
            cur.execute(
                f"UPDATE meshychat_guard_approvals "
                "SET status = %s, resolved_at = NOW(), resolver_peer_id = %s, verdict = %s "
                "WHERE approval_id = %s AND owner_id = %s "
                "  AND status = 'pending' AND expires_at > NOW() "
                f"RETURNING {_APPROVAL_COLUMNS}",
                (status, str(resolver_peer_id), verdict, approval_id, owner),
            )
            row = cur.fetchone()
            if row is not None:
                return dict(row)

            cur.execute(
                "UPDATE meshychat_guard_approvals "
                "SET status = 'expired', resolved_at = COALESCE(resolved_at, NOW()) "
                "WHERE approval_id = %s AND owner_id = %s "
                "  AND status = 'pending' AND expires_at <= NOW() "
                f"RETURNING {_APPROVAL_COLUMNS}",
                (approval_id, owner),
            )
            row = cur.fetchone()
            if row is not None:
                return dict(row)

            cur.execute(
                f"SELECT {_APPROVAL_COLUMNS} FROM meshychat_guard_approvals "
                "WHERE approval_id = %s AND owner_id = %s",
                (approval_id, owner),
            )
            row = cur.fetchone()
    if row is None:
        return NotFound("approval", approval_id)
    return dict(row)


#: The lookup outcome of `find_release_or_pending` - three named cases, not
#: `Optional[row]`, because "an approved card is waiting to be consumed" and "a pending
#: card already exists" lead to opposite branches.
@dataclass(frozen=True)
class ApprovedLookup:
    item: dict


@dataclass(frozen=True)
class PendingLookup:
    item: dict


@dataclass(frozen=True)
class NoLookup:
    scope: str


def find_release_or_pending(conn, owner_id, elicitation_key: str, args_hash: str):
    """Find a release or pending approval.

    The approved branch additionally requires `consumed_at IS NULL` and
    `approved_args_hash = %s`: one approved card releases exactly ONE execution, and only
    of the exact action that was approved.
    """
    _expire_pending_for_owner(conn, owner_id)
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"""
            SELECT {_APPROVAL_COLUMNS}
            FROM meshychat_guard_approvals
            WHERE owner_id = %s
              AND elicitation_key = %s
              AND (
                  (status = 'approved' AND expires_at > NOW() AND consumed_at IS NULL
                   AND approved_args_hash = %s)
                  OR (status = 'pending' AND expires_at > NOW())
              )
            ORDER BY CASE status WHEN 'approved' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                     created_at ASC
            LIMIT 1
            """,
            (str(owner_id), elicitation_key, args_hash),
        )
        row = cur.fetchone()
    if row is None:
        return NoLookup(f"approvals for owner {owner_id} key {elicitation_key[:12]}...")
    item = dict(row)
    if item["status"] == "approved":
        return ApprovedLookup(item)
    if item["status"] == "pending":
        return PendingLookup(item)
    return NoLookup(f"approval {item['approval_id']} is {item['status']}")


def consume_approval(conn, owner_id, approval_id: str, args_hash: str) -> bool:
    """Consume an approval. Return True only if exactly one row moved.

    The predicate repeats every release condition rather than trusting the SELECT that
    found the row: between that read and this write another request may have consumed it.
    `rowcount == 1` is the TOCTOU guard, and the caller fails CLOSED on False.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE meshychat_guard_approvals SET consumed_at = NOW() "
            "WHERE owner_id = %s AND approval_id = %s AND status = 'approved' "
            "  AND expires_at > NOW() AND consumed_at IS NULL AND approved_args_hash = %s",
            (str(owner_id), approval_id, args_hash),
        )
        return cur.rowcount == 1


def insert_ask_approval(conn, approval: dict, link: dict) -> dict | NotFound:
    """Insert or re-attach an ask approval.

    `ON CONFLICT (owner_id, elicitation_key) WHERE status='pending' AND elicitation_key
    IS NOT NULL DO NOTHING` re-attaches instead of opening a duplicate card. That clause
    names the partial UNIQUE index `rga_pending_elicitation_idx` (measured); psycopg
    passes it through verbatim, and Postgres matches it to the index by predicate.
    """
    _expire_pending_for_owner(conn, approval["owner_id"])
    timeout_secs = max(1, int(approval.get("ask_timeout_secs") or DEFAULT_ASK_TIMEOUT_SECS))
    preview = truncate_content_preview(approval.get("content_preview") or "")
    deferred = approval.get("deferred_effects")
    if deferred is None:
        deferred = []
    params = (
        generate_approval_id(),
        str(approval["owner_id"]),
        str(approval["caller_peer_id"]),
        approval.get("skill_id"),
        approval.get("call_id"),
        approval["phase"],
        approval.get("policy"),
        approval.get("reason_code"),
        approval["message"],
        preview,
        approval.get("manifest_hash"),
        approval.get("sandbox_status"),
        timeout_secs,
        approval.get("audit_event_id"),
        Jsonb(deferred),
        link["policy_id"],
        link["attachment_id"],
        link["elicitation_key"],
        link["approved_args_hash"],
    )
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"""
            INSERT INTO meshychat_guard_approvals
              (approval_id, owner_id, caller_peer_id, skill_id, call_id, phase, policy,
               reason_code, message, content_preview, manifest_hash, sandbox_status,
               expires_at, audit_event_id, deferred_effects, policy_id, attachment_id,
               elicitation_key, approved_args_hash, consumed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    NOW() + (%s::bigint * INTERVAL '1 second'), %s, %s,
                    %s,%s,%s,%s,NULL)
            ON CONFLICT (owner_id, elicitation_key)
              WHERE status = 'pending' AND elicitation_key IS NOT NULL
            DO NOTHING
            RETURNING {_APPROVAL_COLUMNS}
            """,
            params,
        )
        row = cur.fetchone()
        if row is not None:
            return dict(row)
        cur.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM meshychat_guard_approvals "
            "WHERE owner_id = %s AND elicitation_key = %s AND status = 'pending' "
            "  AND expires_at > NOW() ORDER BY created_at ASC LIMIT 1",
            (str(approval["owner_id"]), link["elicitation_key"]),
        )
        row = cur.fetchone()
    if row is None:
        # The conflict fired and then the conflicting row vanished: a real race, not an
        # empty result, so it is typed as an absence and becomes a 404, never a silent
        # "no approval needed".
        return NotFound("re-attached approval", link["elicitation_key"])
    return dict(row)


# ---------------------------------------------------------------------- policies


def upsert_policy_seed(conn, seed: dict) -> dict:
    """Upsert a policy seed. The conflict target is `name`
    (`meshy_guard_policies_name_key`), so re-seeding updates in place and never
    duplicates. `enabled` is intentionally NOT in the DO UPDATE list: an operator who
    disabled a builtin must not have it re-enabled by the next request that lists
    policies."""
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"""
            INSERT INTO meshychat_guard_policies
              (policy_id, name, kind, handler_ref, version, description,
               internal_only, enabled, params_schema)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
            ON CONFLICT (name) DO UPDATE
            SET kind = EXCLUDED.kind,
                handler_ref = EXCLUDED.handler_ref,
                version = EXCLUDED.version,
                description = EXCLUDED.description,
                internal_only = EXCLUDED.internal_only,
                params_schema = EXCLUDED.params_schema,
                updated_at = NOW()
            RETURNING {_POLICY_COLUMNS}
            """,
            (
                generate_policy_id(),
                seed["name"],
                seed["kind"],
                seed["handler_ref"],
                int(seed["version"]),
                seed["description"],
                bool(seed["internal_only"]),
                Jsonb(seed["params_schema"]),
            ),
        )
        return dict(cur.fetchone())


def seed_builtin_policies(conn, seeds) -> list:
    """Seed built-in policies, idempotently by the UNIQUE(name) conflict target."""
    return [upsert_policy_seed(conn, s) for s in seeds]


def policy_by_id(conn, policy_id: str) -> dict | NotFound:
    """Return one policy, or NotFound."""
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"SELECT {_POLICY_COLUMNS} FROM meshychat_guard_policies WHERE policy_id = %s",
            (policy_id,),
        )
        row = cur.fetchone()
    if row is None:
        return NotFound("policy", policy_id)
    return dict(row)


def list_discoverable_policies(conn) -> Rows | Empty:
    """List enabled non-internal policies. `internal_only = FALSE AND enabled = TRUE` - the
    emergency-brake policy is internal_only and therefore never discoverable."""
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"SELECT {_POLICY_COLUMNS} FROM meshychat_guard_policies "
            "WHERE internal_only = FALSE AND enabled = TRUE ORDER BY name ASC"
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows_or_empty(rows, "meshychat_guard_policies where not internal_only and enabled")


# ------------------------------------------------------------------- attachments


def attach_policy(conn, new: dict) -> dict | Duplicate:
    """Attach a policy.

    `phases` binds a Python list of str to `text[]`; psycopg adapts it natively, which is
    what the `rgpa_phases_check` containment test needs (a JSON string would fail the
    `<@ ARRAY[...]` test with a type error).

    `rgpa_unique_binding` is UNIQUE on `(policy_id, scope, COALESCE(owner_id, all-zero
    uuid), COALESCE(session_key, ''))`, so re-POSTing a binding that already exists is a
    `UniqueViolation` and not a second row. It is returned as a typed `Duplicate` for the
    view to turn into a 409. A duplicate binding is a well-formed request, not a 500.

    `except psycopg.errors.UniqueViolation` - the EXCEPTION TYPE, which psycopg derives
    from sqlstate 23505. Matching on the message would break on a Postgres that words it
    differently, and would catch an unrelated error that happened to mention "unique".
    Every other database error still propagates.
    """
    try:
        return _insert_attachment(conn, new)
    except psycopg.errors.UniqueViolation as exc:
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
        return Duplicate("policy attachment", constraint)


def _insert_attachment(conn, new: dict) -> dict:
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"""
            INSERT INTO meshychat_guard_policy_attachments
              (attachment_id, policy_id, scope, owner_id, session_key, params,
               phases, ordinal, ask_timeout_secs, created_by_peer_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_ATTACHMENT_COLUMNS}
            """,
            (
                generate_attachment_id(),
                new["policy_id"],
                new["scope"],
                None if new.get("owner_id") is None else str(new["owner_id"]),
                new.get("session_key"),
                Jsonb(new["params"]),
                list(new["phases"]),
                int(new.get("ordinal") or 0),
                new.get("ask_timeout_secs"),
                str(new["created_by_peer_id"]),
            ),
        )
        return dict(cur.fetchone())


def set_attachment_enabled(conn, attachment_id: str, owner_id, enabled: bool) -> dict | NotFound:
    """Set an attachment's enabled state.

    The predicate is `owner_id = %s`, so a `server_default` row (owner_id NULL) and
    another tenant's row both fail to match and surface as 404 - no existence leak.
    """
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"UPDATE meshychat_guard_policy_attachments SET enabled = %s, updated_at = NOW() "
            "WHERE attachment_id = %s AND owner_id = %s "
            f"RETURNING {_ATTACHMENT_COLUMNS}",
            (bool(enabled), attachment_id, str(owner_id)),
        )
        row = cur.fetchone()
    if row is None:
        return NotFound("attachment", attachment_id)
    return dict(row)


def remove_attachment(conn, attachment_id: str, owner_id) -> None | NotFound:
    """Remove an attachment. Zero rows affected is NotFound, not success."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM meshychat_guard_policy_attachments "
            "WHERE attachment_id = %s AND owner_id = %s",
            (attachment_id, str(owner_id)),
        )
        if cur.rowcount == 0:
            return NotFound("attachment", attachment_id)
    return None


def attachments_for_owner(conn, owner_id) -> Rows | Empty:
    """List attachments visible to an owner. server_default rows are included read-only, and the
    ordering is scope precedence (session < owner < server_default) then ordinal."""
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"""
            SELECT {_ATTACHMENT_COLUMNS}
            FROM meshychat_guard_policy_attachments
            WHERE scope = 'server_default' OR owner_id = %s
            ORDER BY CASE scope WHEN 'session' THEN 0 WHEN 'owner' THEN 1 ELSE 2 END,
                     ordinal ASC, created_at ASC
            """,
            (str(owner_id),),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows_or_empty(rows, f"attachments visible to owner {owner_id}")


def attachments_for_evaluation(conn, owner_id, session_key, phase: str) -> Rows | Empty:
    """Return the policy gate's input set.

    `%s = ANY(a.phases)` is the phase filter, and the scope disjunction is what makes a
    session attachment apply only to ITS session. Index `rgpa_eval_idx` on
    `(owner_id, enabled, scope, ordinal) WHERE enabled` serves it.
    """
    with _dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT a.attachment_id, a.policy_id, p.name AS policy_name, p.kind,
                   p.handler_ref, a.scope, a.owner_id, a.session_key, a.params,
                   a.phases, a.ordinal, a.ask_timeout_secs, a.created_at
            FROM meshychat_guard_policy_attachments a
            JOIN meshychat_guard_policies p ON p.policy_id = a.policy_id
            WHERE a.enabled = TRUE
              AND p.enabled = TRUE
              AND %s = ANY(a.phases)
              AND (
                  a.scope = 'server_default'
                  OR (a.scope = 'owner' AND a.owner_id = %s)
                  OR (a.scope = 'session' AND a.owner_id = %s AND a.session_key = %s)
              )
            ORDER BY CASE a.scope WHEN 'session' THEN 0 WHEN 'owner' THEN 1 ELSE 2 END,
                     a.ordinal ASC, a.created_at ASC
            """,
            (phase, str(owner_id), str(owner_id), session_key),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows_or_empty(
        rows, f"enabled attachments for owner {owner_id} phase {phase} session {session_key!r}"
    )


# --------------------------------------------------------------------- decisions


def record_policy_decision(conn, rec: dict) -> None:
    """Record the APPEND-ONLY audit row.

    INSERT only. There is no UPDATE and no DELETE anywhere in this module for
    `meshy_guard_policy_decisions`: the table is the audit trail of what the gate decided,
    and a decision that can be edited afterwards is not one. (The grant `meshychat_app` holds
    does include UPDATE/DELETE - the append-only property is this application's, and
    The `test_decisions_are_append_only` test proves the application keeps it by re-reading
    an earlier row byte for byte after later writes.)

    `owner_id` and `caller_peer_id` are NOT NULL and are always the token-derived
    identity - this is the house-rule-5 attribution for a table the database does not
    constrain (see `db.py`).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meshychat_guard_policy_decisions
              (policy_id, attachment_id, decision, reason, phase, mode, call_id,
               owner_id, caller_peer_id, target_peer_id, skill_name, session_key,
               approval_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                rec.get("policy_id"),
                rec.get("attachment_id"),
                rec["decision"],
                rec.get("reason"),
                rec["phase"],
                rec["mode"],
                rec.get("call_id"),
                str(rec["owner_id"]),
                str(rec["caller_peer_id"]),
                None if rec.get("target_peer_id") is None else str(rec["target_peer_id"]),
                rec.get("skill_name"),
                rec.get("session_key"),
                rec.get("approval_id"),
            ),
        )


def recent_decisions_for_owner(conn, owner_id, limit: int) -> Rows | Empty:
    """Return recent decisions for an owner. Served by `rgpd_owner_created_idx` on
    `(owner_id, created_at DESC)`, so the ORDER BY + LIMIT is an index walk, not a sort of
    the owner's whole history."""
    with _dict_cursor(conn) as cur:
        cur.execute(
            f"SELECT {_DECISION_COLUMNS} FROM meshychat_guard_policy_decisions "
            "WHERE owner_id = %s ORDER BY created_at DESC LIMIT %s",
            (str(owner_id), int(limit)),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows_or_empty(rows, f"policy decisions for owner {owner_id}")


# ------------------------------------------------------------------------- misc


def json_size(value: Any) -> int:
    """Return serialized byte length for the preview and backlog size caps.

    `separators` produces the compact JSON representation used for the caps, so the cap
    measures the exact bytes `_preview_text` stores.
    """
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
