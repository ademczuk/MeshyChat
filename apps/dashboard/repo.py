"""Reads, with absence TYPED.

THE DEFECT THIS FILE EXISTS TO PREVENT
--------------------------------------
Every `shared_memory*` table in this schema is FORCE-RLS with a single policy:

    USING (current_setting('app.server_internal', true) = '1')

Measured on the live database (`pg_policies`): `shared_memory`,
`shared_memory_grant`, `shared_memory_namespace` and the five `dashboard_*` state
tables all carry exactly that predicate and nothing else. So a read issued on a
`meshychat_app` connection WITHOUT the privileged context returns **zero rows and raises
nothing**. On the wire that is `{"peers": []}` - indistinguishable from "this owner
genuinely has no peers". The dashboard is the one surface where that mistake is
invisible: an empty panel is a plausible empty panel.

The production pool runs as the `meshychat_app` NOBYPASSRLS role, so a plain pool read sees
ZERO `shared_memory` rows, `vis`/`vis_peers` come back empty, and every
co-author-broadened panel renders zero peers/namespaces/memories.

So no function here returns a bare `[]`, `{}` or `None`, and there is no bare `except`.
A read returns one of two values and the caller must name which one it is handling:

    Rows(rows)        the query ran under a PROVEN context and matched rows
    Empty(scope)      the query ran under a PROVEN context and matched nothing

`NotFound` (404) and `Denied` (403) outcomes were declared here too and deleted on
2026-09-29: no production path constructed them, so the only test of them was a test
that built them itself (review queue). A by-id route that needs one brings it back with
the test that reaches it.

`Rows` and `Empty` are only ever constructed AFTER `SELECT current_setting(...)` has
confirmed inside the same transaction that the context is bound. That check is a
POSITIVE CONTROL for every negative result this module can produce: without it,
"you may see nothing" and "the connection was never privileged" are the same bytes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from apps.core import dbconn

from . import errors

log = logging.getLogger("meshychat.dashboard")


# --------------------------------------------------------------------- outcomes

@dataclass(frozen=True)
class Rows:
    """One or more rows, read under a proven privileged context."""

    rows: list
    #: Which read produced them. Carried so a log line names the query, not "a read".
    scope: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return True


@dataclass(frozen=True)
class Empty:
    """The read ran, the context was proven, and nothing matched.

    Distinct from `Rows([])`, which this module never constructs, and from a bare
    `[]`, which it never returns. A caller that renders an empty list for this is
    making a decision; a caller that renders an empty list for a silent failure is
    not.
    """

    scope: str

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return False


Outcome = Rows | Empty


def rows_or_raise(outcome: Outcome) -> list:
    """Unwrap for the list-shaped routes, which render `Empty` as `[]` on the wire.

    This is the ONE place `Empty -> []` happens, and it happens after the outcome has
    been named. Anything else is an internal error, never an empty collection: a 200
    with `[]` for a read that did not run is the shape that hides a bug behind a
    plausible-looking panel.
    """
    if isinstance(outcome, Rows):
        return outcome.rows
    if isinstance(outcome, Empty):
        log.debug("dashboard %s: read ran under a proven context and matched nothing",
                  outcome.scope)
        return []
    raise errors.internal(f"unhandled read outcome: {type(outcome).__name__}")


# ------------------------------------------------------------------ connections

def connection():
    """An autocommit `meshychat_app` connection that has PASSED the RLS guard.

    Delegates to `apps.core.dbconn`, which refuses (a) a role with `rolbypassrls` and
    (b) a non-autocommit connection whose `SET LOCAL` would bind to a savepoint. Both
    refusals are exceptions rather than log lines, and both are the difference between
    "scoped" and "reads every tenant while looking healthy".
    """
    return dbconn.serving_connection()


CONTEXT_PROOF = (
    "SELECT current_setting('app.server_internal', true) = '1' AS internal, "
    "       current_user AS role"
)


def prove_context(cur) -> None:
    """Positive control: refuse to report absence from an unprivileged read.

    Runs INSIDE the server-internal transaction, so it proves the thing the rows
    depend on rather than something adjacent. Without it every negative result in
    this module is unfalsifiable.
    """
    cur.execute(CONTEXT_PROOF)
    row = cur.fetchone()
    if row is None:
        raise errors.internal("server-internal context probe returned no row")
    # Resolve the column by NAME off cursor.description rather than trusting the
    # cursor's row_factory. A tuple cursor makes `"internal" in row` a SUBSTRING test
    # against the first column, which is False for every value this query can return -
    # so the probe would raise "no row" for a perfectly good context and, worse, the
    # same expression on a differently-shaped row could be True by accident. Caught by
    # test_the_rls_context_is_what_makes_the_read_return_rows, which calls this on a
    # default (tuple) cursor.
    names = [d.name for d in (cur.description or ())]
    if "internal" not in names:
        raise errors.internal(
            f"server-internal context probe returned unexpected columns: {names}")
    proof = row if isinstance(row, dict) else dict(zip(names, row))
    if not proof["internal"]:
        raise errors.internal(
            "refusing to read an RLS-gated dashboard table without "
            "app.server_internal='1': every row would be filtered out and the "
            f"response would be an empty list (role={proof.get('role')!r})"
        )


def run_internal(conn, scope: str, fn):
    """Run `fn(cur)` inside ONE server-internal transaction, after `prove_context`, on a
    dict_row cursor, and return what it returns.

    The scaffold `fetch_all_internal` and the logs route share: logs runs three
    statements (retention read, lazy expiry delete, list) in one transaction, so it cannot
    be a single `fetch_all_internal`, and before this
    it carried its own copy of the transaction, the proof and the two except arms.
    """
    try:
        with dbconn.request_server_internal(conn) as c:
            with c.cursor(row_factory=dict_row) as cur:
                prove_context(cur)
                return fn(cur)
    except errors.ApiError:
        raise
    except psycopg.Error as exc:
        # Named exception type, not a bare except, and re-raised as a typed error
        # rather than swallowed into an empty list.
        raise errors.internal(f"dashboard {scope}: {exc}") from exc


def fetch_all_internal(conn, sql: str, params: dict, scope: str) -> Rows | Empty:
    """A read inside a server-internal transaction.

    The dashboard's authorisation is the `vis` app-WHERE, NOT row security: this
    context deliberately opens the RLS gate so the CTE can be the wall. That makes the
    CTE the only wall, which is why `tenant_scope.py` is a seam and not seven copies.

    `is_local = true` (what `rls.server_internal` sets) scopes the GUC to THIS
    transaction, so it cannot leak onto the connection's next use.
    """
    def read(cur):
        cur.execute(sql, params)
        return cur.fetchall()
    rows = run_internal(conn, scope, read)
    if rows:
        return Rows(rows, scope)
    return Empty(scope)


def fetch_one_internal(conn, sql: str, params: dict, scope: str) -> Rows | Empty:
    """Single-row variant for the aggregate handlers (stats, usage).

    Still returns `Rows`/`Empty` rather than a row-or-None: a `None` here would reach
    a view as a missing aggregate and be rendered as zeros, which reads as "no
    activity" rather than "the aggregate did not run".
    """
    outcome = fetch_all_internal(conn, sql, params, scope)
    if isinstance(outcome, Rows) and len(outcome.rows) > 1:
        raise errors.internal(f"dashboard {scope}: expected one row, got {len(outcome.rows)}")
    return outcome


def fetch_all_plain(conn, sql: str, params: dict, scope: str) -> Rows | Empty:
    """A read of a table with NO row-level security, so no privileged context.

    MEASURED, not assumed: `pg_class.relrowsecurity` is FALSE for `peers`, `skills`,
    `models`, `mesh_presence`, `dashboard_users`, `owner_sessions`, `owner_api_keys`
    and the six `dashboard_*` docs tables (clients, invoices, invoice_items,
    documents, expenses, payouts), and, for the tier and entitlement routes,
    `owner_subscriptions`, `tier_capabilities`, `owner_tier_overrides`, `owner_usage` and
    `accounts_entitlements`. tests/test_dashboard.py DERIVES the tables from this
    function's call sites and fails on one missing here. Their authorization is the
    route's `owner_id` predicate,
    which is why every caller of this function binds the caller's own id and why
    every query filters by owner_id in code.

    Using `fetch_all_internal` here instead would work and would be WRONG: it would
    enter a privileged context for a read that does not need one, and `rls.py` is
    explicit that server-internal is "entered for a specific server-side operation,
    never for the length of a request".
    """
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise errors.internal(f"dashboard {scope}: {exc}") from exc
    if rows:
        return Rows(rows, scope)
    return Empty(scope)


def objs(outcome: Outcome) -> list:
    """Project the single `obj` JSONB column.

    Reads the column by EXPLICIT key and raises when it is absent. A `.get("obj")`
    that defaults to None would turn a mis-aliased projection into a list of nulls,
    which renders as an empty dashboard panel rather than an error.
    """
    out = []
    for row in rows_or_raise(outcome):
        if "obj" not in row:
            raise errors.internal(
                f"dashboard projection is missing the 'obj' column (got {sorted(row)})")
        out.append(row["obj"])
    return out


def one_obj(outcome: Outcome, what: str) -> Any:
    """Single-row `obj`, with absence typed rather than returned as `None`.

    `what` names the thing for the 404 message and is required, so a caller cannot
    reach this function without having decided what a missing row MEANS on its route.
    """
    values = objs(outcome)
    if not values:
        raise errors.not_found()
    return values[0]


__all__ = [
    "Rows", "Empty", "Outcome",
    "connection", "run_internal", "fetch_all_internal", "fetch_one_internal",
    "fetch_all_plain", "objs", "one_obj", "rows_or_raise", "prove_context",
]
