"""`hub_connections`: the cross-worker presence table, and who may touch it (041 S2).

Design `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6), section 4.3,
Q8 and Q14 (keep the table), critique F13. A presence row is a ROUTING INPUT: step 9 (S5)
will read it to decide whether a call to a target is `routed` to a socket or `queued` for a
reconnect or a doorbell. A row that says "online" for a peer nobody holds a socket for
strands its calls in `routed` with no socket and no doorbell until their deadline. So every
command on the table is bound to `app.server_internal`. A connection bound to a peer, or to an
owner alone, or to nothing, may not read it, write it, refresh it or delete it.

WHAT THE POLICY DOES NOT STOP. `server_internal` is not "the server's own threads only":
request handlers enter it too (`apps/core/rls.py`, `projection_writer`'s docstring: "A
request holds `server_internal` for its reads"; every handler in `apps/calls/route.py` binds
it on the request's own connection). So server code on a request path that entered it could
write a row, and the database would agree. That half is closed by the caller guard in
tests/test_hub_presence.py: only the store names the table, and only the modules its
allowlist names (none until S3) may reach the store's writes. Divergence 022 closed the same
gap for projections with a second flag; this table's writer is not a job, so it has none.

THE PROBES. Each runs ONE statement as `meshychat_app` in a transaction that is always rolled
back, and measures inside that transaction after `RESET ROLE`, as the bootstrap role that
sees every row (the method of tests/test_rls_update_delete_scope.py). UPDATE and DELETE are
UNQUALIFIED: Postgres adds the SELECT policy to an UPDATE or DELETE whose statement reads a
column, so a `WHERE` would be confined by the SELECT policy alone and a loosened UPDATE or
DELETE policy would go unseen. FIVE CONTEXTS. "No context" is two states: a fresh connection
reads the settings as NULL, a connection whose transactions have ended reads ''. A request
binds its peer and owner (`request`), and some request paths bind the OWNER ALONE
(`apps/grants/service.py`, `apps/meshguard/views.py`), a state a policy keyed on "no peer
bound" would wrongly admit (`owner`).
Every refusal is paired, in the same test, with the same statement succeeding under
`server_internal` (rule C1): zero rows is also what a policy that refuses everything shows.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.core import dbconn, rls  # noqa: E402
from tests.fixtures.hub_presence_db import hub_connections_clean  # noqa: E402,F401  (autouse)

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

SETTINGS_SQL = ("SELECT current_setting('app.server_internal', true), "
                "current_setting('app.current_peer_id', true), "
                "current_setting('app.current_owner_id', true)")


# ------------------------------------------------------------------------- fixtures

@pytest.fixture()
def owner_conn():
    """The bootstrap role: seeds rows and is the measuring instrument. Bypasses RLS."""
    with psycopg.connect(DSN) as conn:
        yield conn


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane) VALUES (%s,%s,'product')",
                    (owner_id, f"hub-{tag}-{owner_id.hex[:8]}@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (peer_id, owner_id, f"hub-{tag}-{peer_id.hex[:8]}"))
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id}


def _row(conn, peer: dict) -> uuid.UUID:
    """One presence row for `peer`, written by the bootstrap role (RLS does not bind it)."""
    connection_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hub_connections (connection_id, peer_id, worker_token) "
                    "VALUES (%s, %s, %s)", (connection_id, peer["peer_id"], uuid.uuid4()))
    conn.commit()
    return connection_id


@pytest.fixture()
def two(owner_conn):
    """Peers A and B, each holding one presence row seeded by the bootstrap role."""
    out = {}
    for tag in ("A", "B"):
        p = _peer(owner_conn, tag)
        p["connection_id"] = _row(owner_conn, p)
        out[tag] = p
    return out


# ------------------------------------------------------------------------ the probe

def _bind(cur, *, peer=None, owner=None, internal=False) -> None:
    """The settings apps/core/rls.py binds, LOCAL to the probe's transaction."""
    if internal:
        cur.execute("SELECT set_config('app.server_internal', '1', true)")
    if peer is not None:
        cur.execute("SELECT set_config('app.current_peer_id', %s, true)", (str(peer),))
    if owner is not None:
        cur.execute("SELECT set_config('app.current_owner_id', %s, true)", (str(owner),))


def _release(app, p: dict) -> None:
    """End one server_internal and one peer-bound transaction on `app`: it is then
    contextless the way a pooled connection is between two requests."""
    with rls.server_internal(app):
        pass
    with rls.peer_context(app, peer_id=p["peer_id"], owner_id=p["owner_id"]):
        pass
    with app.cursor() as cur:
        cur.execute(SETTINGS_SQL)
        assert cur.fetchone() == ("", "", ""), "the released probe is the fresh one again"


def _contexts(p: dict):
    """(name, ctx), IN THIS ORDER, each run on a connection of its own. `request` is a
    request's context bound to the row's OWN peer: the strongest claim a request has.
    `owner` binds the row's owner and no peer, as an owner-scoped request path does."""
    return (("contextless", {"fresh": True}),
            ("released", {"released": p}),
            ("request", {"peer": p["peer_id"], "owner": p["owner_id"]}),
            ("owner", {"owner": p["owner_id"]}),
            ("server_internal", {"internal": True}))


#: Every context that must be refused, in the order `_contexts` yields them.
REFUSED = ("contextless", "released", "request", "owner")


def _probe(statement: str, args: tuple, *, measure: str, fresh=False, released=None, **ctx):
    """Run `statement` as meshychat_app under `ctx`, rolled back. Returns (error, measured):
    `error` is the SQLSTATE class name raised by the statement (None when it ran),
    `measured` the rows `measure` returns as the bootstrap role inside the same
    transaction, or the rows the statement itself returned when `measure` is 'returned'."""
    app = dbconn.connect(DSN)             # the serving path's constructor, verified
    try:
        if released is not None:
            _release(app, released)
        error, measured = None, None
        try:
            with app.transaction(force_rollback=True), app.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == rls.APP_ROLE, "the probe must run AS the app role"
                if fresh:
                    cur.execute(SETTINGS_SQL)
                    assert cur.fetchone() == (None, None, None), "fresh means nothing bound"
                _bind(cur, **ctx)
                cur.execute(statement, args)
                returned = cur.fetchall() if cur.description else None
                if measure == "returned":
                    measured = returned
                else:
                    cur.execute("RESET ROLE")
                    cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
                    assert cur.fetchone()[0], "the instrument must see every row"
                    cur.execute(measure[0], measure[1])
                    measured = cur.fetchall()
        except psycopg.errors.InsufficientPrivilege as exc:
            error = type(exc).__name__
        assert rls.current_role(app) == rls.APP_ROLE, "the rollback must restore the role"
        return error, measured
    finally:
        app.close()


def _labels(rows, two) -> set[str]:
    by_id = {str(two[k]["connection_id"]): k for k in two}
    return {by_id[str(r[0])] for r in rows if str(r[0]) in by_id}


# ------------------------------------------------------------------- T20, RLS half

def test_a_request_cannot_read_presence_and_the_server_can(two):
    """SELECT. A request (or nothing) sees no row; server_internal sees both (control)."""
    got = {name: _probe("SELECT connection_id FROM hub_connections", (),
                        measure="returned", **ctx)
           for name, ctx in _contexts(two["A"])}
    for name in REFUSED:
        assert got[name][0] is None and _labels(got[name][1], two) == set(), (
            f"{name}: a presence read outside server_internal saw {got[name]}")
    assert got["server_internal"][0] is None
    assert _labels(got["server_internal"][1], two) == {"A", "B"}, (
        f"positive control: server_internal must read every row, read {got['server_internal']}")


def test_a_request_cannot_write_a_presence_row_and_the_server_can(two):
    """INSERT. A request bound to peer A inserting A's OWN row is refused (the fake-online
    forgery F13 names); so is a contextless one; server_internal writes it (control)."""
    got = {}
    for name, ctx in _contexts(two["A"]):
        new_id = uuid.uuid4()
        got[name] = (new_id, _probe(
            "INSERT INTO hub_connections (connection_id, peer_id, worker_token) "
            "VALUES (%s, %s, %s)", (new_id, two["A"]["peer_id"], uuid.uuid4()),
            measure=("SELECT connection_id FROM hub_connections WHERE connection_id = %s",
                     (new_id,)), **ctx))
    for name in REFUSED:
        _, (error, _rows) = got[name]
        assert error == "InsufficientPrivilege", (
            f"{name}: a presence INSERT outside server_internal was not refused: {got[name]}")
    new_id, (error, rows) = got["server_internal"]
    assert error is None and rows == [(new_id,)], (
        f"positive control: server_internal must insert, got {got['server_internal']}")


def test_a_request_cannot_refresh_presence_and_the_server_can(two, owner_conn):
    """UPDATE, unqualified. Refreshing `seen_at` is how a row stays "online", so a request
    that could run it could keep an offline peer looking online. Both seeded rows are aged
    first, so a reached row is unambiguous: its `seen_at` moved forward."""
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE hub_connections SET seen_at = now() - interval '1 hour' "
                    "WHERE connection_id = ANY(%s)",
                    ([two["A"]["connection_id"], two["B"]["connection_id"]],))
    owner_conn.commit()
    measure = ("SELECT connection_id FROM hub_connections WHERE connection_id = ANY(%s) "
               "AND seen_at > now() - interval '1 minute'",
               ([two["A"]["connection_id"], two["B"]["connection_id"]],))
    got = {name: _probe("UPDATE hub_connections SET seen_at = now()", (), measure=measure,
                        **ctx)
           for name, ctx in _contexts(two["A"])}
    for name in REFUSED:
        error, rows = got[name]
        assert error is None and _labels(rows, two) == set(), (
            f"{name}: an unqualified presence UPDATE reached {got[name]}")
    error, rows = got["server_internal"]
    assert error is None and _labels(rows, two) == {"A", "B"}, (
        f"positive control: server_internal must refresh every row, got {got['server_internal']}")


def test_a_request_cannot_delete_presence_and_the_server_can(two):
    """DELETE, unqualified. Deleting a row makes an online peer look offline."""
    ids = [two["A"]["connection_id"], two["B"]["connection_id"]]
    measure = ("SELECT connection_id FROM hub_connections WHERE connection_id = ANY(%s)", (ids,))
    got = {name: _probe("DELETE FROM hub_connections", (), measure=measure, **ctx)
           for name, ctx in _contexts(two["A"])}
    for name in REFUSED:
        error, rows = got[name]
        assert error is None and _labels(rows, two) == {"A", "B"}, (
            f"{name}: an unqualified presence DELETE removed rows: {got[name]}")
    error, rows = got["server_internal"]
    assert error is None and _labels(rows, two) == set(), (
        f"positive control: server_internal must delete, rows left {got['server_internal']}")


def test_row_level_security_is_enabled_and_forced():
    """ENABLE and FORCE, read by the production instrument (`rls.assert_rls_active`).

    FORCE cannot be observed by a probe here: `meshychat_app` does not own the table, and the
    owner that FORCE binds is the bootstrap role, which has BYPASSRLS. It is still the
    posture every table in sql_meshychat/ carries, and it is what keeps the table closed if
    the owner ever becomes a non-bypassing role, so it is read from the catalogue.
    """
    app = dbconn.connect(DSN)
    try:
        rls.assert_rls_active(app, "hub_connections")
        rls.assert_rls_active(app, "mesh_route_usage")          # control: a known-good table
        with pytest.raises(RuntimeError, match="not enabled"):  # control: the check can fail
            rls.assert_rls_active(app, "peers")
    finally:
        app.close()


def test_the_table_has_the_design_shape(owner_conn):
    """Section 4.3's columns, key, foreign key and index; a peer's deletion takes its rows
    (a referential action, which RLS does not bind), another peer's row stays (control)."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'hub_connections' ORDER BY ordinal_position")
        cols = cur.fetchall()
        cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                    "AND tablename = 'hub_connections' ORDER BY indexname")
        indexes = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT confdeltype, confrelid::regclass::text, "
                    "pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'hub_connections'::regclass AND contype = 'f'")
        fks = cur.fetchall()
    assert [(c[0], c[1], c[2]) for c in cols] == [
        ("connection_id", "uuid", "NO"), ("peer_id", "uuid", "NO"),
        ("worker_token", "uuid", "NO"),
        ("connected_at", "timestamp with time zone", "NO"),
        ("seen_at", "timestamp with time zone", "NO")], cols
    assert [c[3] for c in cols] == [None, None, None, "now()", "now()"], cols
    assert any("UNIQUE" in d and "(connection_id)" in d for d in indexes), indexes
    assert any("(peer_id, seen_at)" in d for d in indexes), indexes
    assert fks == [("c", "peers", "FOREIGN KEY (peer_id) REFERENCES peers(id) ON DELETE CASCADE")], fks

    a, b = _peer(owner_conn, "casA"), _peer(owner_conn, "casB")
    ra, rb = _row(owner_conn, a), _row(owner_conn, b)
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM peers WHERE id = %s", (a["peer_id"],))
        owner_conn.commit()
        cur.execute("SELECT connection_id FROM hub_connections WHERE connection_id = ANY(%s)",
                    ([ra, rb],))
        assert cur.fetchall() == [(rb,)], "a deleted peer's presence must go with it"
