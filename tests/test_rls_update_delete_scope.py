"""Row-level security on UPDATE and DELETE, and the one INSERT 001 missed (divergence 029).

`sql_meshychat/001` closed the INSERT side of row-level security on `local_memory`,
`skill_calls` and `meshy_memory`, and `local_memory`'s UPDATE. It left seven UPDATE/DELETE
policies at `USING (true)` and one INSERT at `WITH CHECK (true)`, measured on a database
freshly migrated from c5457ac (`pg_policies`, 55 policies):

    meshy_memory_internal_update / _delete      sql/039:44-45          STATUS B05
    local_memory_internal_delete               sql/096:19             STATUS B06
    skill_calls_internal_update / _delete      sql/006:70, :74        STATUS B06
    work_pool_internal_update / _delete        sql/103:85, :87        STATUS B06
    work_pool_internal_insert                  sql/103:83-84          STATUS B14

WHY THE PROBES BELOW HAVE NO `WHERE`. Postgres adds a table's SELECT policy to an UPDATE or
DELETE whenever the statement READS a column (a WHERE, a RETURNING, a SET that references a
column). The SELECT policies here are scoped, so a qualified statement was already confined
by them, incidentally. The leak is the statement that reads nothing: `DELETE FROM t`. So each
probe is exactly that, and it runs inside a transaction that is always rolled back; the
measurement is taken INSIDE that transaction after `RESET ROLE` (back to the bootstrap role,
which sees every tenant), so a probe never commits and never needs a WHERE to be measured.

FOUR CONTEXTS, because "no context" is two different states. A connection nothing was ever
bound on reads the three settings as NULL. A connection whose server_internal and peer-bound
transactions have ENDED reads them as '' (a placeholder setting set LOCAL reverts to the
empty string, not to "undefined"), and that is the state of every pooled connection between
two requests. A predicate written `IS NOT NULL`, or one that compares with '', passes the
second state and not the first, so each probe runs contextless-fresh, contextless-released,
bound to A, and under server_internal, and asserts which state it is in before it writes.

WHO LEGITIMATELY USES EACH POLICY, traced to call sites rather than read off policy names.
This is what the new predicates were written to keep working:

  meshy_memory UPDATE/DELETE  the owner context (`app.current_owner_id`). The positive
      control below binds it around INSERT ... ON CONFLICT DO UPDATE. Nothing under `apps/`
      writes meshy_memory. Referential actions (ON DELETE
      CASCADE from dashboard_users, SET NULL from peers) are not subject to RLS.
  local_memory DELETE        the peer context (`app.current_peer_id`). Nothing under
      `apps/` writes local_memory. The CASCADE from peers is a referential action.
  skill_calls UPDATE/DELETE  `app.server_internal = '1'` only. Every traced writer
      runs there: apps/calls/janitor.py `sweep_expired_deadlines`, reads.py
      `cancel_to_abandoned`, route.py `_start_running` / `_fail_unexecuted` / `_finish`,
      apps/wsgw/target_frames.py `running` / `result` / `error` (divergence 041 S4),
      each inside `rls.server_internal`. No peer-bound writer exists, and a call row is
      written by two parties (the caller's signed args, the target's result), so neither
      peer alone owns it.
  work_pool                  no writer is traced; apps only name it.
      `apps/core/rls.py` files it under server_internal, and the design specifies the
      server post and claim (r5-competing-consumers-work-queue.md, "Tenancy keystone"):
      `tenant_key` is "computed at both post and claim from the authenticated peer's live
      owner, never from the request body", POST "sets caller_peer_id/caller_owner_id
      server-side", and the claim and requeue run in the server-internal context. So every
      command, the INSERT included, is server_internal only.
      A peer-bound INSERT that bound only `caller_peer_id` still chose the owner, the
      tenant_key and every claim column (review of 8c3150f, F1).

Every negative is paired with a positive control in the same test, because "zero rows" is
also what a policy that refuses everything looks like.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.calls import janitor, reads  # noqa: E402
from apps.calls import route as routing  # noqa: E402
from apps.core import dbconn, rls  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

PROBE_VALUE = '{"rls_probe": 1}'


# ------------------------------------------------------------------------- fixtures

@pytest.fixture()
def owner_conn():
    """The bootstrap role: seeds rows and is the measuring instrument. Bypasses RLS."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """The serving path's own constructor: SET ROLE meshychat_app, then VERIFIED not bypassing."""
    conn = dbconn.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


def _tenant(conn, tag: str, *, trust_group=None) -> dict:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    plane = "dev" if trust_group else "product"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,%s,%s)",
                    (owner_id, f"rls31-{tag}-{owner_id.hex[:8]}@example.test", plane,
                     trust_group))
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (peer_id, owner_id, f"rls31-{tag}-{peer_id.hex[:8]}"))
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id, "tag": tag}


def _peer_of(conn, owner_id, tag: str) -> uuid.UUID:
    """A second peer of an EXISTING owner: another party inside the same tenant."""
    peer_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (peer_id, owner_id, f"rls31-{tag}-{peer_id.hex[:8]}"))
    conn.commit()
    return peer_id


def _memory_rows(conn, t: dict) -> dict:
    """One meshy_memory and one local_memory row belonging to tenant `t`."""
    meshy_key, local_key = f"rls31/meshy/{uuid.uuid4()}", f"rls31/local/{uuid.uuid4()}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO meshychat_memory (created_by_peer_id, created_by_owner_id, key, "
                    "value) VALUES (%s,%s,%s,'{\"v\":1}')",
                    (t["peer_id"], t["owner_id"], meshy_key))
        cur.execute("INSERT INTO local_memory (peer_id, key, value) VALUES (%s,%s,'{\"v\":1}')",
                    (t["peer_id"], local_key))
    conn.commit()
    return {"meshy_key": meshy_key, "local_key": local_key}


def _skill(conn, t: dict) -> uuid.UUID:
    skill_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
                    "input_schema) VALUES (%s,%s,%s,'1','sync','private','{}')",
                    (skill_id, t["peer_id"], f"rls31.s{skill_id.hex[:6]}"))
    conn.commit()
    return skill_id


def _call(conn, t: dict, skill_id, *, status="pending", deadline=None) -> str:
    """A call from `t` to itself: the tenant is the only party, so any reach is cross-tenant."""
    call_id = f"rls31-{uuid.uuid4()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_calls (call_id, caller_peer_id, caller_owner_id, "
            "target_peer_id, target_owner_id, skill_id, skill_name, skill_version, mode, "
            "transport, status, args, deadline_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'rls31.s','1','sync','ws',%s,'{}', %s)",
            (call_id, t["peer_id"], t["owner_id"], t["peer_id"], t["owner_id"], skill_id,
             status, deadline or _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)))
    conn.commit()
    return call_id


def _pool_insert(cur, caller: dict, **cols) -> uuid.UUID:
    """One work_pool row posted for `caller`: by default what the design's POST writes (the
    caller's peer and owner, and the product-plane tenant_key, which is the owner id).
    `cols` overrides or adds columns, which is how a probe forges one."""
    row = {"id": uuid.uuid4(), "queue": "rls31", "tenant_key": str(caller["owner_id"]),
           "caller_peer_id": caller["peer_id"], "caller_owner_id": caller["owner_id"],
           "required_skill": "rls31.s", "envelope": "{}", "args": "{}", **cols}
    cur.execute(f"INSERT INTO work_pool ({', '.join(row)}, deadline_at) "
                f"VALUES ({', '.join(['%s'] * len(row))}, now() + interval '1 hour')",
                list(row.values()))
    return row["id"]


def _pool_row(conn, t: dict) -> uuid.UUID:
    with conn.cursor() as cur:
        pool_id = _pool_insert(cur, t)
    conn.commit()
    return pool_id


@pytest.fixture()
def two(owner_conn):
    """Tenants A and B, each with one row in every table under test."""
    out = {}
    for tag in ("A", "B"):
        t = _tenant(owner_conn, tag)
        t.update(_memory_rows(owner_conn, t))
        t["call_id"] = _call(owner_conn, t, _skill(owner_conn, t))
        t["pool_id"] = _pool_row(owner_conn, t)
        out[tag] = t
    return out


# ------------------------------------------------------------------------ the probe

SETTINGS_SQL = ("SELECT current_setting('app.server_internal', true), "
                "current_setting('app.current_peer_id', true), "
                "current_setting('app.current_owner_id', true)")


def _release(app_conn, t: dict) -> None:
    """Run and END one server_internal and one peer-bound transaction on this connection:
    afterwards it is contextless the way a pooled connection is between two requests."""
    with rls.server_internal(app_conn):
        pass
    with rls.peer_context(app_conn, peer_id=t["peer_id"], owner_id=t["owner_id"]):
        pass
    with app_conn.cursor() as cur:
        cur.execute(SETTINGS_SQL)
        state = cur.fetchone()
    assert state == ("", "", ""), (
        f"a released connection must read '' for every setting, read {state}: otherwise "
        "this probe is the fresh-contextless one again")


def _bind(cur, *, peer=None, owner=None, internal=False) -> None:
    """The same three settings apps/core/rls.py binds, LOCAL to the probe's transaction."""
    if internal:
        cur.execute("SELECT set_config('app.server_internal', '1', true)")
    if peer is not None:
        cur.execute("SELECT set_config('app.current_peer_id', %s, true)", (str(peer),))
    if owner is not None:
        cur.execute("SELECT set_config('app.current_owner_id', %s, true)", (str(owner),))


def _reached(app_conn, statement: str, *, table: str, key_col: str, keys: dict,
             changed: str | None, fresh: bool = False, released: dict | None = None,
             **ctx) -> set[str]:
    """Run ONE unqualified UPDATE/DELETE as meshychat_app under `ctx`; return the labels it reached.

    `keys` maps a label ('A', 'B') to the seeded row's key. For an UPDATE, `changed` is the
    predicate that is true once the statement has touched a row; for a DELETE it is None and
    a row is reached when it is gone. `fresh` asserts nothing was ever bound on the
    connection (settings NULL); `released` first binds that tenant and ends the transactions
    (settings ''). Always rolled back.
    """
    by_key = {str(v): label for label, v in keys.items()}
    if released is not None:
        _release(app_conn, released)
    with app_conn.transaction(force_rollback=True):
        with app_conn.cursor() as cur:
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == rls.APP_ROLE, "the probe must run AS the app role"
            if fresh:
                cur.execute(SETTINGS_SQL)
                assert cur.fetchone() == (None, None, None), (
                    "the fresh-contextless probe must run before anything was bound here")
            _bind(cur, **ctx)
            cur.execute(statement)
            # Measure inside the same transaction, as the bootstrap role, which sees every
            # tenant. RESET ROLE is undone by the rollback, like everything else here.
            cur.execute("RESET ROLE")
            cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
            assert cur.fetchone()[0], "the instrument must see every tenant's rows"
            where = f"{key_col}::text = ANY(%s)" + (f" AND {changed}" if changed else "")
            cur.execute(f"SELECT {key_col}::text FROM {table} WHERE {where}", (list(by_key),))
            present = {by_key[r[0]] for r in cur.fetchall()}
    assert rls.current_role(app_conn) == rls.APP_ROLE, "the rollback must restore the role"
    return present if changed else set(keys) - present


def _contexts(t: dict):
    """(name, ctx) for the four contexts every probe is run under, IN THIS ORDER: the fresh
    probe has to run before anything is bound on the connection, and the released one
    binds `t` and ends it before it writes."""
    return (("contextless", {"fresh": True}),
            ("released", {"released": t}),
            ("bound to A", {"peer": t["peer_id"], "owner": t["owner_id"]}),
            ("server_internal", {"internal": True}))


# ----------------------------------------------------------------- B05: meshy_memory

@pytest.mark.parametrize("statement,changed", [
    (f"UPDATE meshychat_memory SET value = '{PROBE_VALUE}'::jsonb",
     f"value = '{PROBE_VALUE}'::jsonb"),
    ("DELETE FROM meshychat_memory", None),
], ids=["update", "delete"])
def test_meshy_memory_unqualified_write_reaches_only_the_bound_owner(app_conn, two,
                                                                     statement, changed):
    """B05. Before: contextless and bound-to-A each reached BOTH tenants' rows."""
    keys = {"A": two["A"]["meshy_key"], "B": two["B"]["meshy_key"]}
    got = {name: _reached(app_conn, statement, table="meshychat_memory", key_col="key",
                          keys=keys, changed=changed, **ctx)
           for name, ctx in _contexts(two["A"])}
    assert got["contextless"] == set(), f"a connection bound to no owner reached {got}"
    assert got["released"] == set(), f"a released connection reached {got['released']}"
    assert got["bound to A"] == {"A"}, (
        f"bound to owner A the statement reached {got['bound to A']}: B's row is another "
        "tenant's, and A's own row must still be writable (the positive control)")
    assert got["server_internal"] == {"A", "B"}, (
        "positive control: the server-internal escape 001 gave this table's INSERT policy "
        f"must survive on UPDATE/DELETE too; it reached {got['server_internal']}")


@pytest.fixture()
def dev_group(owner_conn, app_conn):
    """Dev-plane owners A and C in ONE trust group, and one meshy_memory row owned by A.

    Group members READ each other's rows (`meshy_same_tenant_or_devgroup`, sql/039), so the
    SELECT policy Postgres adds to a QUALIFIED UPDATE/DELETE does not confine it here: this
    is the one place the qualified form leaked too. Writes are the OWNER's, as 001's insert
    policy already says. Every statement runs in a rolled-back transaction.
    """
    group = uuid.uuid4()
    a = _tenant(owner_conn, "devA", trust_group=group)
    c = _tenant(owner_conn, "devC", trust_group=group)
    key = _memory_rows(owner_conn, a)["meshy_key"]

    def as_owner(owner, sql, args):
        with app_conn.transaction(force_rollback=True), app_conn.cursor() as cur:
            _bind(cur, peer=owner["peer_id"], owner=owner["owner_id"])
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else cur.rowcount

    # Control, asserted by every test that uses this: the dev-group READ still works.
    assert as_owner(c, "SELECT key FROM meshychat_memory WHERE key = %s", (key,)) == [(key,)]
    return {"a": a, "c": c, "key": key, "as_owner": as_owner}


def test_a_dev_group_peer_cannot_rewrite_another_owners_meshy_memory(dev_group):
    """B05, qualified. Before: C's UPDATE of A's row touched 1 row."""
    g = dev_group
    assert g["as_owner"](g["c"], "UPDATE meshychat_memory SET value = %s::jsonb WHERE key = %s",
                         (PROBE_VALUE, g["key"])) == 0, "a group peer rewrote another owner's row"
    assert g["as_owner"](g["a"], "UPDATE meshychat_memory SET value = %s::jsonb WHERE key = %s",
                         (PROBE_VALUE, g["key"])) == 1, "positive control: the OWNER could not"


def test_a_dev_group_peer_cannot_delete_another_owners_meshy_memory(dev_group):
    """B05, qualified. Before: C's DELETE of A's row touched 1 row."""
    g = dev_group
    assert g["as_owner"](g["c"], "DELETE FROM meshychat_memory WHERE key = %s",
                         (g["key"],)) == 0, "a group peer deleted another owner's row"
    assert g["as_owner"](g["a"], "DELETE FROM meshychat_memory WHERE key = %s",
                         (g["key"],)) == 1, "positive control: the OWNER could not"


def test_an_owner_cannot_reattribute_its_meshy_memory_to_another_tenant(dev_group):
    """B05, the UPDATE's WITH CHECK. Before: A moved its row to C and nothing raised, because
    the new row passed the dev-group SELECT policy and the UPDATE check was `true`."""
    g = dev_group
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        g["as_owner"](g["a"], "UPDATE meshychat_memory SET created_by_owner_id = %s WHERE key = %s",
                      (g["c"]["owner_id"], g["key"]))
    assert g["as_owner"](g["a"], "UPDATE meshychat_memory SET created_by_owner_id = %s "
                         "WHERE key = %s", (g["a"]["owner_id"], g["key"])) == 1, (
        "positive control: an update that keeps the owner must pass the same check")


def test_the_owner_bound_meshy_memory_paths_still_write(owner_conn, app_conn):
    """Positive control for B05: an upsert with owner context, INSERT ... ON CONFLICT DO
    UPDATE ... RETURNING, a
    plain owner UPDATE, and the owner's DELETE."""
    a = _tenant(owner_conn, "own")
    key = _memory_rows(owner_conn, a)["meshy_key"]
    with rls.peer_context(app_conn, owner_id=a["owner_id"]), app_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meshychat_memory (created_by_peer_id, created_by_owner_id, key, value, "
            "source) VALUES (%s,%s,%s,'{\"v\":2}','peer') "
            "ON CONFLICT (created_by_owner_id, key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = NOW() RETURNING value",
            (a["peer_id"], a["owner_id"], key))
        assert cur.fetchone()[0] == {"v": 2}, "the owner's upsert did not update its row"
    with rls.peer_context(app_conn, owner_id=a["owner_id"]), app_conn.cursor() as cur:
        cur.execute("UPDATE meshychat_memory SET value = '{\"v\":3}' WHERE key = %s", (key,))
        assert cur.rowcount == 1, "the owner could not update its own row"
    with rls.peer_context(app_conn, owner_id=a["owner_id"]), app_conn.cursor() as cur:
        cur.execute("DELETE FROM meshychat_memory WHERE key = %s", (key,))
        assert cur.rowcount == 1, "the owner could not delete its own row"


# ---------------------------------------------------------- B06: local_memory

@pytest.mark.parametrize("statement,changed,scoped_before", [
    ("DELETE FROM local_memory", None, False),
    # The CONTROL row: local_memory's UPDATE was scoped by 001, so it passes on either tree.
    (f"UPDATE local_memory SET value = '{PROBE_VALUE}'::jsonb",
     f"value = '{PROBE_VALUE}'::jsonb", True),
], ids=["delete", "update-control"])
def test_local_memory_unqualified_write_reaches_only_the_bound_peer(app_conn, two, statement,
                                                                    changed, scoped_before):
    """B06. Before: `DELETE FROM local_memory` bound to A removed both tenants' rows."""
    keys = {"A": two["A"]["local_key"], "B": two["B"]["local_key"]}
    got = {name: _reached(app_conn, statement, table="local_memory", key_col="key",
                          keys=keys, changed=changed, **ctx)
           for name, ctx in _contexts(two["A"])}
    assert got["contextless"] == set(), f"a connection bound to no peer reached {got}"
    assert got["released"] == set(), f"a released connection reached {got['released']}"
    assert got["bound to A"] == {"A"}, (
        f"bound to peer A the statement reached {got['bound to A']}; A's own row must "
        "still be reachable (the positive control) and B's must not")
    assert got["server_internal"] == {"A", "B"}, (
        "positive control: 001's server-internal escape on this table must survive")


def test_the_promote_path_still_moves_a_row_from_local_to_meshy(owner_conn, app_conn):
    """POSITIVE CONTROL for B05+B06, the one traced writer that uses BOTH new predicates:
    The promote path binds peer and owner, locks the local row, upserts into meshy_memory, and
    deletes the local row, in one transaction."""
    a = _tenant(owner_conn, "promote")
    local_key = _memory_rows(owner_conn, a)["local_key"]
    with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]), \
            app_conn.cursor() as cur:
        cur.execute("SELECT value FROM local_memory WHERE peer_id = %s AND key = %s "
                    "FOR UPDATE", (a["peer_id"], local_key))
        value = cur.fetchone()[0]
        cur.execute("INSERT INTO meshychat_memory (created_by_peer_id, created_by_owner_id, key, "
                    "value, source) VALUES (%s,%s,%s,%s,'peer') "
                    "ON CONFLICT (created_by_owner_id, key) DO UPDATE "
                    "SET value = EXCLUDED.value",
                    (a["peer_id"], a["owner_id"], local_key, Jsonb(value)))
        cur.execute("DELETE FROM local_memory WHERE peer_id = %s AND key = %s",
                    (a["peer_id"], local_key))
        assert cur.rowcount == 1, "promote could not delete the peer's own local row"
    with owner_conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM local_memory WHERE key = %s), "
                    "(SELECT count(*) FROM meshychat_memory WHERE key = %s)", (local_key, local_key))
        assert cur.fetchone() == (0, 1)


@pytest.mark.parametrize("statement,changed", [
    ("DELETE FROM local_memory", None),
    # 001's UPDATE, the same rule, measured the same way.
    (f"UPDATE local_memory SET value = '{PROBE_VALUE}'::jsonb",
     f"value = '{PROBE_VALUE}'::jsonb"),
], ids=["delete", "update"])
def test_local_memory_write_is_the_peers_not_its_owners(owner_conn, app_conn, statement,
                                                        changed):
    """local_memory is private to ONE peer: its only SELECT policy is peer_id = current
    peer, so a second peer of the same owner is another party. Review of 8c3150f, F4: the
    probes above bind one peer per owner, so they could not tell peer scoping from owner
    scoping, and a DELETE widened to every peer of the bound owner (mutant ME) passed."""
    a1 = _tenant(owner_conn, "sib1")
    a2 = {**a1, "peer_id": _peer_of(owner_conn, a1["owner_id"], "sib2")}
    keys = {"A1": _memory_rows(owner_conn, a1)["local_key"],
            "A2": _memory_rows(owner_conn, a2)["local_key"]}
    got = _reached(app_conn, statement, table="local_memory", key_col="key", keys=keys,
                   changed=changed, peer=a1["peer_id"], owner=a1["owner_id"])
    assert got == {"A1"}, (
        f"bound to peer A1 of owner A the statement reached {got}: A2's row is another "
        "peer's, and A1's own row must still be reachable (the positive control)")


def test_referential_actions_still_reach_rows_the_policies_hide(owner_conn, app_conn):
    """POSITIVE CONTROL: the CASCADE / SET NULL paths are not RLS-scoped. Deleting a peer and
    then its owner from a CONTEXTLESS app-role connection still removes the peer's
    local_memory, nulls meshy_memory's created_by_peer_id, then removes the owner's rows."""
    c = _tenant(owner_conn, "cascade")
    rows = _memory_rows(owner_conn, c)
    with app_conn.cursor() as cur:
        cur.execute("DELETE FROM peers WHERE id = %s", (c["peer_id"],))
        assert cur.rowcount == 1
    with owner_conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM local_memory WHERE key = %s), "
                    "(SELECT created_by_peer_id FROM meshychat_memory WHERE key = %s)",
                    (rows["local_key"], rows["meshy_key"]))
        assert cur.fetchone() == (0, None), "the peer's cascade/SET NULL did not reach"
    owner_conn.commit()
    with app_conn.cursor() as cur:
        cur.execute("DELETE FROM dashboard_users WHERE id = %s", (c["owner_id"],))
        assert cur.rowcount == 1
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM meshychat_memory WHERE key = %s", (rows["meshy_key"],))
        assert cur.fetchone()[0] == 0, "the owner's cascade did not reach meshychat_memory"


# ----------------------------------------------------------- B06: skill_calls

@pytest.mark.parametrize("statement,changed", [
    ("UPDATE skill_calls SET error_message = 'rls-probe'", "error_message = 'rls-probe'"),
    ("DELETE FROM skill_calls", None),
], ids=["update", "delete"])
def test_skill_calls_unqualified_write_reaches_nothing_outside_server_internal(
        app_conn, two, statement, changed):
    """B06. Before: contextless and bound-to-A each reached both tenants' calls. After, the
    only writer context is the one every traced writer uses: server_internal."""
    keys = {"A": two["A"]["call_id"], "B": two["B"]["call_id"]}
    got = {name: _reached(app_conn, statement, table="skill_calls", key_col="call_id",
                          keys=keys, changed=changed, **ctx)
           for name, ctx in _contexts(two["A"])}
    assert "B" not in got["bound to A"], f"a peer-bound connection reached B's call: {got}"
    assert got["contextless"] == set(), f"a connection bound to no peer reached {got}"
    assert got["released"] == set(), f"a released connection reached {got['released']}"
    assert got["bound to A"] == set(), (
        "no traced writer mutates a call from a PEER context, and a call row is two "
        f"parties' record; bound to A the statement reached {got['bound to A']}")
    assert got["server_internal"] == {"A", "B"}, (
        f"positive control: the server's own context must reach calls: {got}")


def test_the_servers_own_call_writes_still_land(owner_conn, app_conn):
    """POSITIVE CONTROL for B06 on skill_calls, through the real functions, as meshychat_app:
    route's pending->running->completed, the cancel transition, the deadline janitor, and
    retention via a server-internal DELETE."""
    a = _tenant(owner_conn, "server")
    skill_id = _skill(owner_conn, a)
    run_id, cancel_id = _call(owner_conn, a, skill_id), _call(owner_conn, a, skill_id)
    # Oldest deadline in the database, so the janitor's LIMIT 1 takes this row.
    expired_id = _call(owner_conn, a, skill_id,
                       deadline=_dt.datetime(1971, 1, 1, tzinfo=_dt.timezone.utc))

    assert routing._start_running(app_conn, run_id) is True, "pending -> running refused"
    assert routing._finish(app_conn, run_id, status="completed", result={"ok": True},
                           payload={"call_id": run_id}, signature="sig", signing_kid="kid",
                           error_code=None, error_message=None) == "completed"
    cancelled = reads.cancel_to_abandoned(app_conn, cancel_id, a["peer_id"], "rls31 probe")
    assert cancelled is not None and cancelled["status"] == "abandoned"
    assert janitor.sweep_expired_deadlines(app_conn, limit=1) == [expired_id]
    with rls.server_internal(app_conn), app_conn.cursor() as cur:
        cur.execute("DELETE FROM skill_calls WHERE call_id = %s AND status = 'completed'",
                    (run_id,))
        assert cur.rowcount == 1, "the server-internal retention delete was refused"
    with owner_conn.cursor() as cur:
        cur.execute("SELECT call_id, status FROM skill_calls WHERE call_id = ANY(%s)",
                    ([run_id, cancel_id, expired_id],))
        assert dict(cur.fetchall()) == {cancel_id: "abandoned", expired_id: "timeout"}


# ------------------------------------------------------ B06 + B14: work_pool

@pytest.mark.parametrize("statement,changed", [
    ("UPDATE work_pool SET priority = 7", "priority = 7"),
    ("DELETE FROM work_pool", None),
], ids=["update", "delete"])
def test_work_pool_unqualified_write_reaches_nothing_outside_server_internal(
        app_conn, two, statement, changed):
    """B06. Before: contextless and bound-to-A each reached both tenants' pool rows."""
    keys = {"A": two["A"]["pool_id"], "B": two["B"]["pool_id"]}
    got = {name: _reached(app_conn, statement, table="work_pool", key_col="id",
                          keys=keys, changed=changed, **ctx)
           for name, ctx in _contexts(two["A"])}
    assert "B" not in got["bound to A"], f"a peer-bound connection reached B's job: {got}"
    assert got["contextless"] == set(), f"a connection bound to no peer reached {got}"
    assert got["released"] == set(), f"a released connection reached {got['released']}"
    assert got["bound to A"] == set(), f"a peer-bound connection mutated the pool: {got}"
    assert got["server_internal"] == {"A", "B"}, (
        f"positive control: the server's claim/requeue context must reach the pool: {got}")


def test_a_claimer_cannot_rewrite_or_remove_the_job_it_holds(owner_conn, app_conn):
    """A claimed job names two peers, its caller and its claimer. The caller-bound probe was
    already there; the claimer's was not, so a USING that also admitted
    claimed_by_peer_id = current peer (review of 8c3150f, F4, mutant MH) passed. Claim,
    requeue and finish are the server's: the design runs them in `with_server_internal`."""
    a = _tenant(owner_conn, "job")
    claimer = _peer_of(owner_conn, a["owner_id"], "claimer")
    with owner_conn.cursor() as cur:
        job = _pool_insert(cur, a, status="claimed", claimed_by_peer_id=claimer, attempts=1,
                           materialized_call_id="rls31:1")
    owner_conn.commit()
    for statement, changed in (("UPDATE work_pool SET priority = 7", "priority = 7"),
                               ("DELETE FROM work_pool", None)):
        probe = dict(table="work_pool", key_col="id", keys={"job": job}, changed=changed)
        as_claimer = _reached(app_conn, statement, peer=claimer, owner=a["owner_id"], **probe)
        as_server = _reached(app_conn, statement, internal=True, **probe)
        assert as_claimer == set(), f"the claimer's own connection ran {statement!r} on its job"
        assert as_server == {"job"}, f"positive control: the server could not run {statement!r}"


def test_work_pool_insert_is_the_servers_alone(owner_conn, app_conn):
    """B14, and the review of 8c3150f, F1. Before 012, bound to peer A, a row claiming caller
    B was accepted. 8c3150f bound `caller_peer_id` only, so a peer-bound post still chose
    every other column: A's own peer with B's owner and tenant_key took B's idempotency key
    (B's own post then raised UniqueViolation on work_pool_idem_uidx), and a row could be
    born claimed by B with attempts and a materialized call id. The design sets those
    server-side, never from the request, and no writer exists in either server, so the
    INSERT is server_internal only."""
    a, b = _tenant(owner_conn, "poolA"), _tenant(owner_conn, "poolB")
    queue, idem = f"rls31-{uuid.uuid4().hex[:8]}", "k1"

    # Control on either tree: the same forgery on the table 001 did cover.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]), \
                app_conn.cursor() as cur:
            cur.execute("INSERT INTO local_memory (peer_id, key, value) VALUES (%s,%s,'{}')",
                        (b["peer_id"], f"rls31/{uuid.uuid4()}"))

    posts = {
        "A as itself": {},
        "A with B's owner and tenant_key, on B's idempotency key": {
            "caller_owner_id": b["owner_id"], "tenant_key": str(b["owner_id"]),
            "idempotency_key": idem},
        "A as B": {"caller_peer_id": b["peer_id"], "caller_owner_id": b["owner_id"]},
        "A, born claimed by B": {"status": "claimed", "claimed_by_peer_id": b["peer_id"],
                                 "attempts": 3, "materialized_call_id": "x:3"},
    }
    accepted = []
    for name, cols in posts.items():
        try:
            with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]), \
                    app_conn.cursor() as cur:
                _pool_insert(cur, a, queue=queue, **cols)
            accepted.append(name)
        except psycopg.errors.InsufficientPrivilege:
            pass
    assert accepted == [], f"a peer-bound connection posted to the pool: {accepted}"

    # Positive control: the server posts for both tenants, B on the key A tried to take.
    with rls.server_internal(app_conn), app_conn.cursor() as cur:
        _pool_insert(cur, b, queue=queue, idempotency_key=idem)
        _pool_insert(cur, a, queue=queue)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT caller_peer_id, caller_owner_id, tenant_key, idempotency_key "
                    "FROM work_pool WHERE queue = %s", (queue,))
        rows = set(cur.fetchall())
    assert rows == {(b["peer_id"], b["owner_id"], str(b["owner_id"]), idem),
                    (a["peer_id"], a["owner_id"], str(a["owner_id"]), None)}, rows


def test_work_pool_insert_without_context_is_refused(owner_conn, app_conn):
    """B14, the #149 shape itself: a write path that forgot its context. Before: accepted.
    Measured twice, fresh (settings NULL) and released (settings '', a pooled connection
    between requests), because a predicate testing IS NOT NULL, or admitting '', refuses
    the first and accepts the second (review of 8c3150f, F4, mutant MG)."""
    a = _tenant(owner_conn, "poolnoctx")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app_conn.transaction(), app_conn.cursor() as cur:
            cur.execute(SETTINGS_SQL)
            assert cur.fetchone() == (None, None, None), "not a fresh connection"
            _pool_insert(cur, a)                          # fresh
    _release(app_conn, a)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app_conn.transaction(), app_conn.cursor() as cur:
            _pool_insert(cur, a)                          # released
    with rls.server_internal(app_conn), app_conn.cursor() as cur:
        _pool_insert(cur, a)                              # control: the server's context


def test_the_server_internal_pool_path_still_writes(owner_conn, app_conn):
    """POSITIVE CONTROL for B06/B14 on work_pool: the server posts for anyone, claims (the
    UPDATE the design's claim SQL runs) and removes, all under server_internal."""
    a, b = _tenant(owner_conn, "srvA"), _tenant(owner_conn, "srvB")
    with rls.server_internal(app_conn), app_conn.cursor() as cur:
        posted_for_b = _pool_insert(cur, b)
        doomed = _pool_insert(cur, a)
        cur.execute("UPDATE work_pool SET status = 'claimed', claimed_by_peer_id = %s, "
                    "attempts = attempts + 1 WHERE id = %s", (a["peer_id"], posted_for_b))
        assert cur.rowcount == 1, "the server-internal claim was refused"
        cur.execute("DELETE FROM work_pool WHERE id = %s", (doomed,))
        assert cur.rowcount == 1, "the server-internal delete was refused"
    with owner_conn.cursor() as cur:
        cur.execute("SELECT id, status FROM work_pool WHERE id = ANY(%s)",
                    ([posted_for_b, doomed],))
        assert dict(cur.fetchall()) == {posted_for_b: "claimed"}


# ------------------------------------------------------------ the class, by census

# ------------------------------------------ divergence 040 slice 4: the capability tables

#: sql_meshychat/019. Append-only by the database: a SELECT and an INSERT policy bound to
#: server_internal, NO UPDATE or DELETE policy (029's shape), and no UPDATE, DELETE or
#: TRUNCATE privilege for the app role, so a write that is not an INSERT is an ERROR in every
#: context, server_internal included, never a silent nothing.
CAPABILITY_TABLES = {
    "capability_revocation": ("revocation_id",
                              "UPDATE capability_revocation SET reason = 'rls-probe'"),
    "capability_event": ("authority_revocation_id",
                         "UPDATE capability_event SET reason = 'rls-probe'"),
}


def _capability_rows(owner_conn, t: dict) -> dict:
    """One revocation row and one register row, written by the bootstrap role for `t`."""
    rid, aid = os.urandom(64), os.urandom(64)
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO capability_revocation (revocation_id, revoked_by_peer, reason, "
                    "not_after) VALUES (%s,%s,'rls probe', now() + interval '1 hour')",
                    (rid, t["peer_id"]))
        cur.execute("INSERT INTO capability_event (event, actor_peer_id, "
                    "authority_revocation_id, issuer_peer_id, subject_peer_id, namespace, "
                    "rights, depth_cap, expires_at, kid) VALUES ('issue',%s,%s,%s,%s,'rls/ns',"
                    "'[{\"prefix\": \"\", \"action\": \"read\"}]',0,"
                    "now() + interval '1 hour',1)",
                    (t["peer_id"], aid, t["peer_id"], t["peer_id"]))
    owner_conn.commit()
    return {"capability_revocation": rid, "capability_event": aid}


@pytest.mark.parametrize("table", sorted(CAPABILITY_TABLES))
@pytest.mark.parametrize("verb", ["update", "delete"])
def test_a_capability_table_refuses_every_update_and_delete_in_every_context(
        owner_conn, app_conn, table, verb):
    """No context may rewrite or remove a revocation or a register row: not a peer, not a
    released connection, not the server itself. Each probe is unqualified, in a transaction
    that is rolled back, and must raise; the row is then still there, as written."""
    t = _tenant(owner_conn, "cap")
    key = _capability_rows(owner_conn, t)[table]
    key_col, update = CAPABILITY_TABLES[table]
    statement = update if verb == "update" else f"DELETE FROM {table}"
    reached = {}
    for name, ctx in _contexts(t):
        if ctx.get("released"):
            _release(app_conn, ctx["released"])
        try:
            with app_conn.transaction(force_rollback=True), app_conn.cursor() as cur:
                if ctx.get("fresh"):
                    cur.execute(SETTINGS_SQL)
                    assert cur.fetchone() == (None, None, None)
                _bind(cur, **{k: v for k, v in ctx.items() if k in ("peer", "owner",
                                                                    "internal")})
                cur.execute(statement)
                reached[name] = cur.rowcount
        except psycopg.errors.InsufficientPrivilege:
            reached[name] = "refused"
    assert reached == {name: "refused" for name, _ in _contexts(t)}, reached
    with owner_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {key_col} = %s "
                    "AND reason IS DISTINCT FROM 'rls-probe'", (key,))
        assert cur.fetchone()[0] == 1, "the row was changed or removed"
    # POSITIVE CONTROL: the same connection, under server_internal, may still INSERT and read.
    with rls.server_internal(app_conn), app_conn.cursor() as cur:
        cur.execute("INSERT INTO capability_revocation (revocation_id, revoked_by_peer, reason, "
                    "not_after) VALUES (%s,%s,'control', now()) RETURNING revocation_id",
                    (os.urandom(64), t["peer_id"]))
        assert cur.fetchone() is not None
        cur.execute(f"SELECT count(*) FROM {table} WHERE {key_col} = %s", (key,))
        assert cur.fetchone()[0] == 1


def write_policies_that_admit_everything(rows) -> list[tuple]:
    """Every policy that governs a write and whose USING or WITH CHECK is literally `true`.

    SELECT policies are out of scope on purpose: a readable-by-all table is a decision, and
    read scoping is 039/096's subject. A write policy of `true` is the #149 class.
    """
    out = []
    for table, name, cmd, qual, check in rows:
        if cmd not in ("INSERT", "UPDATE", "DELETE", "ALL"):
            continue
        if (qual or "").strip() == "true" or (check or "").strip() == "true":
            out.append((table, name, cmd))
    return out


def test_no_write_policy_admits_every_row(owner_conn):
    """B05+B06+B14 as one class. Before: exactly the eight named in the module docstring."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'public'")
        rows = cur.fetchall()
    assert len(rows) >= 50, f"positive control: expected the ~55 live policies, read {len(rows)}"
    assert any(n == "local_memory_internal_update" for _, n, *_ in rows), (
        "positive control: the census must see a known write policy")
    # C2: the checker must REFUSE a real row mutated into the failure.
    planted = ("work_pool", "planted", "DELETE", "true", None)
    assert write_policies_that_admit_everything([planted]) == [("work_pool", "planted",
                                                                "DELETE")]
    offenders = write_policies_that_admit_everything(rows)
    assert not offenders, f"write policies that admit every row: {sorted(offenders)}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
