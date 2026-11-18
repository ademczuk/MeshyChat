"""STATUS F10: `memory.query` is counted where it runs, so the adoption ratio has both halves.

Divergence 018 (d) left the other half of the graph-chat success metric (inbox adoption
against `memory.query` volume) as a typed null: "memory.query is not dispatched by this
server". That stopped being true when step 9 of the call pipeline began executing memory
calls in-process (apps/calls/route.py, divergence 017): `POST /v1/agents/<memory>/skills/
memory.query/invoke` runs `handlers.handle_query` here. So the count is taken at that one
dispatch site, into the same table and with the same never-fail discipline as the mesh
routes' counts (apps/mesh/usage.py), and `usage.summary` reports it beside them.

Fixtures and helpers are tests/test_call_routing.py's, so every call below resolves through
`meshychat.urls` as a node's does.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_mesh_usage_memory_query.py -q
"""
from __future__ import annotations

import datetime as _dt
import sys
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_call_routing import (  # noqa: E402,F401
    DSN, client, invoke, owner_conn, poll, server_config, signed, world,
)
from apps.core import rls  # noqa: E402


def _counted(owner_conn, route: str, peer_id, outcome: str) -> int:
    """The row the counter writes, read as the bootstrap role (RLS bypassed), so absent
    means absent."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT COALESCE(sum(calls), 0) FROM mesh_route_usage WHERE route = %s "
                    "AND peer_id = %s AND outcome = %s", (route, peer_id, outcome))
        n = int(cur.fetchone()[0])
    owner_conn.commit()
    return n


def _query(client, peer, memory_peer, ns, mode="sync"):
    env = signed(peer, memory_peer, "memory.query", {"namespace": ns, "key_prefix": "q-"})
    response = invoke(client, peer, memory_peer, "memory.query", env, mode=mode)
    assert response.status_code in (200, 202), response.content
    return poll(client, peer, env["payload"]["call_id"])


def test_a_memory_query_is_counted_once_per_call_by_peer_and_outcome(client, owner_conn,
                                                                     world):
    """Two completed queries by the granted caller are two `ok` counts on its row; a query by
    a peer the namespace refuses is one `denied` count on ITS row and no `ok`. CONTROLS: a
    memory.write and a memory.read by the same caller, through the same door, add nothing to
    `memory_query` (the count is keyed on the skill, not on the call), and nothing else of
    the caller's is counted as memory_query."""
    caller, outsider = world["caller"], world["outsider"]
    memory_peer, ns = world["memory_peer"], world["namespace"]
    key = f"q-{uuid.uuid4().hex[:8]}"
    write = signed(caller, memory_peer, "memory.write",
                   {"namespace": ns, "key": key, "value": {"text": "count me"}})
    assert invoke(client, caller, memory_peer, "memory.write", write).status_code == 202
    assert poll(client, caller, write["payload"]["call_id"])["status"] == "completed"
    read = signed(caller, memory_peer, "memory.read", {"namespace": ns, "key": key})
    assert invoke(client, caller, memory_peer, "memory.read", read).status_code == 202
    assert poll(client, caller, read["payload"]["call_id"])["status"] == "completed"
    assert _counted(owner_conn, "memory_query", caller["peer_id"], "ok") == 0, \
        "a write or a read was counted as a query"

    for _ in range(2):
        assert _query(client, caller, memory_peer, ns)["status"] == "completed"
    assert _counted(owner_conn, "memory_query", caller["peer_id"], "ok") == 2

    refused = _query(client, outsider, memory_peer, ns)
    assert refused["status"] == "failed", refused
    assert _counted(owner_conn, "memory_query", outsider["peer_id"], "denied") == 1
    assert _counted(owner_conn, "memory_query", outsider["peer_id"], "ok") == 0
    assert _counted(owner_conn, "memory_query", caller["peer_id"], "ok") == 2


def test_the_usage_summary_reports_memory_query_beside_the_routes(client, owner_conn, world):
    """`usage.summary` carries memory_query as counts (calls, peers, denials, errors) with its
    own `counting_since`, not as a typed null, and keeps it OUT of `routes` (it is not a
    /v1/mesh route). Its `counting_since` is its FIRST day (one probe of the primary key's
    (route, day) prefix since review finding 2 of E13, not a second aggregate): a count
    planted 30 days back is at or after it. CONTROL: every mesh route is still in `routes`."""
    from apps.mesh import usage
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    assert _query(client, caller, memory_peer, ns)["status"] == "completed"
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) "
                    "VALUES ('memory_query', (now() AT TIME ZONE 'UTC')::date - 30, %s, "
                    "'error', 1)", (world["outsider"]["peer_id"],))
    owner_conn.commit()
    with psycopg.connect(DSN, autocommit=True) as app:
        rls.set_role(app)
        with rls.server_internal(app):
            got = usage.summary(app)
    mq = got["memory_query"]
    assert isinstance(mq["calls"], int) and mq["calls"] >= 1, mq
    assert isinstance(mq["peers"], int) and mq["peers"] >= 1, mq
    assert {"denied_calls", "denied_peers", "error_calls"} <= set(mq), mq
    today = _dt.datetime.now(_dt.timezone.utc).date()
    assert mq["counting_since"] is not None, mq
    assert mq["counting_since"] <= (today - _dt.timedelta(days=30)).isoformat(), mq
    assert "memory_query" not in got["routes"], got["routes"]
    assert set(got["routes"]) == set(usage.ROUTES), got["routes"]
    lines = usage.render(got)
    assert any(ln.strip().startswith("memory.query") and "call(s)" in ln for ln in lines), lines


def test_a_counter_that_cannot_write_never_fails_the_query(client, owner_conn, world,
                                                           monkeypatch):
    """The count is an instrument: when it cannot land (here, the upsert pointed at a route
    the CHECK refuses) the query still completes and returns its rows, and the failure is
    counted in WRITE_FAILURES. CONTROL: with the upsert intact the same query is counted."""
    from apps.mesh import usage
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    assert _query(client, caller, memory_peer, ns)["status"] == "completed"
    assert _counted(owner_conn, "memory_query", caller["peer_id"], "ok") == 1

    before = usage.WRITE_FAILURES
    monkeypatch.setattr(usage, "MEMORY_QUERY_ROUTE", "not-a-route")
    got = _query(client, caller, memory_peer, ns)
    assert got["status"] == "completed", got
    assert usage.WRITE_FAILURES == before + 1
    assert _counted(owner_conn, "memory_query", caller["peer_id"], "ok") == 1


def test_the_counted_set_in_code_is_the_check_constraint(owner_conn):
    """usage.COUNTED (every mesh route, memory_query, since sql_meshychat/035 the two notice
    names, which are usage dimensions and not routes, and since 037 the two operator routes,
    divergence 081), since 044 the card list and the card search, and since 045 their purpose routes (issue
    #238), against the CHECK 024, 035, 037, 044 and 045 widen: a name in one and not the other fails every count under it."""
    from apps.mesh import usage
    assert usage.COUNTED == (usage.ROUTES + (usage.MEMORY_QUERY_ROUTE,) + usage.NOTICE_ROUTES
                             + usage.OPERATOR_ROUTES + usage.CARD_ROUTES + usage.TRANSCRIPT_ROUTES)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'mesh_route_usage'::regclass "
                    "AND conname = 'mesh_route_usage_route_check'")
        (definition,) = cur.fetchone()
    owner_conn.commit()
    for name in usage.COUNTED:
        assert f"'{name}'" in definition, (name, definition)
    assert definition.count("'::text") == len(usage.COUNTED), definition


def test_the_down_script_drops_the_counts_it_cannot_keep_and_keeps_the_rest(owner_conn,
                                                                            world):
    """sql_meshychat/down/024 on a table that HOLDS a memory_query count (the round trip in
    tests/test_rollback_gate.py runs on an empty one): the narrower CHECK can only come back
    once those rows are gone, so the script deletes them and nothing else, and afterwards the
    CHECK refuses memory_query again. Run inside one transaction that is rolled back, so the
    run's database keeps 024. CONTROL: before the script, the same CHECK accepts it."""
    down = ROOT / "sql_meshychat" / "down" / "024_mesh_route_usage_memory_query_down.sql"
    peer = world["caller"]["peer_id"]
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) "
                        "VALUES ('memory_query', current_date, %s, 'ok', 3), "
                        "       ('inbox', current_date, %s, 'ok', 5)", (peer, peer))
            # A real rollback to 23 runs every later down first (sqlmigrate --rollback-to), and
            # down/045, down/044, down/039, down/037, down/036, down/035 and down/034 delete the counts of the
            # routes they added. Earlier tests in this database may have called those routes
            # (test_cards_* and test_mesh_fleet_unread sort before this file), so do what those downs
            # do before 024's.
            cur.execute("DELETE FROM mesh_route_usage WHERE route IN "
                        "('transcript_search', 'transcript_search_brief', 'transcript_search_consensus', "
                        "'transcript_search_context', 'transcript_search_unknown', "
                        "'cards_briefing', 'cards_send', 'cards_write', 'card_search_dialog', "
                        "'cards_unknown', 'card_search_unknown', "
                        "'cards', 'card_search', 'recipients', 'fleet_unread', 'fleet_unread_peer', "
                        "'unread', 'brief', 'brief_notice', 'neighbours_notice', 'mentions', 'awaiting')")
            cur.execute(down.read_text(encoding="utf-8"))
            cur.execute("SELECT route, calls FROM mesh_route_usage WHERE peer_id = %s "
                        "ORDER BY route", (peer,))
            assert cur.fetchall() == [("inbox", 5)]
            cur.execute("SAVEPOINT refused")
            try:
                cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome) "
                            "VALUES ('memory_query', current_date, %s, 'ok')", (peer,))
            except psycopg.errors.CheckViolation:
                cur.execute("ROLLBACK TO SAVEPOINT refused")
            else:
                raise AssertionError("the down script left memory_query in the CHECK")
        conn.rollback()
    with owner_conn.cursor() as cur:            # the run's database still has 024
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'mesh_route_usage_route_check'")
        assert "'memory_query'" in cur.fetchone()[0]
    owner_conn.commit()


def test_a_query_on_an_unclaimed_namespace_is_counted_denied(client, owner_conn, world):
    """Review finding 7 of F10: `E_NAMESPACE_UNCLAIMED` is the namespace gate's refusal too
    (the graph routes answer 403 for it), so a memory.query on a namespace nobody claimed is
    a `denied` count, not an `error`. CONTROL: the call really failed with that code, and a
    completed query by the same caller is still `ok`."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    nowhere = f"shared/unclaimed-{uuid.uuid4().hex[:8]}"
    got = _query(client, caller, memory_peer, nowhere)
    assert got["status"] == "failed", got
    assert "E_NAMESPACE_UNCLAIMED" in str(got), got
    assert _counted(owner_conn, "memory_query", caller["peer_id"], "denied") == 1
    assert _counted(owner_conn, "memory_query", caller["peer_id"], "error") == 0
    assert _query(client, caller, memory_peer, ns)["status"] == "completed"
    assert _counted(owner_conn, "memory_query", caller["peer_id"], "ok") == 1
