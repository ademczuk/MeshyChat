"""Issue #238 C1, C2 and C4, divergence 101: the card list and the card search are counted in
mesh_route_usage under `cards` and `card_search`, and GET /v1/cards/usage reports them per seat, per UTC
day, with a `counting_since` of their own. Through the REAL urlconf, with tests/test_cards_routes.py's
fixtures (a real peer and bearer, the deterministic fake model hosts, a graph namespace named as the
card namespace).

Run:  .venv/Scripts/python.exe -m pytest tests/test_cards_usage.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_cards_routes import _card, _list, _search, deck  # noqa: E402,F401  (fixtures)
from tests.test_meshsem_graphsearch import (  # noqa: E402,F401  (fixtures)
    _bearer, _member, _walk, client, graph, owner, world)

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE  # noqa: E402
from apps.mesh import usage  # noqa: E402


def _counted(owner, route, peer_id, outcome) -> int:
    row = owner.execute("SELECT COALESCE(sum(calls), 0) FROM mesh_route_usage "
                        "WHERE route = %s AND peer_id = %s AND outcome = %s "
                        "AND day = (now() AT TIME ZONE 'UTC')::date",
                        (route, str(peer_id), outcome)).fetchone()
    return int(row[0])


def _usage(client, reader, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/v1/cards/usage?{query}", **_bearer(reader["peer_id"]))


# --------------------------------------------------------------------------- what is counted

def test_a_card_list_is_counted_under_cards_ok(client, owner, deck):
    ns, holder, reader = deck
    _card(owner, holder, ns, "Ruling | counted", time="2026-10-08T07:32:13.000Z")
    before = _counted(owner, "cards", reader["peer_id"], "ok")
    assert _list(client, reader).status_code == 200
    assert _list(client, reader).status_code == 200
    assert _counted(owner, "cards", reader["peer_id"], "ok") == before + 2


def test_a_card_search_is_counted_under_card_search_and_a_mail_search_is_not(client, owner, deck):
    ns, holder, reader = deck
    _card(owner, holder, ns, "Ruling | searchable words", time="2026-10-08T07:32:13.000Z")
    assert _walk(owner, ns).pending == 0
    before = _counted(owner, "card_search", reader["peer_id"], "ok")
    assert _search(client, reader, "searchable", ns).status_code == 200
    assert _counted(owner, "card_search", reader["peer_id"], "ok") == before + 1
    # the mail's own search, named or by default: not a card search, and counted under no card route
    for url in ("/v1/mesh/search?q=anything&rerank=0", f"/v1/mesh/search?q=anything&rerank=0&ns={MESH_CHAT_NAMESPACE}"):
        client.get(url, **_bearer(reader["peer_id"]))
    assert _counted(owner, "card_search", reader["peer_id"], "ok") == before + 1
    assert _counted(owner, "cards", reader["peer_id"], "ok") == 0
    # the card namespace beside another one is not a card search, whatever the server answers it
    any_outcome = {o: _counted(owner, "card_search", reader["peer_id"], o) for o in ("ok", "denied", "error")}
    client.get(f"/v1/mesh/search?q=anything&rerank=0&ns={ns}&ns={MESH_CHAT_NAMESPACE}", **_bearer(reader["peer_id"]))
    assert {o: _counted(owner, "card_search", reader["peer_id"], o) for o in ("ok", "denied", "error")} == any_outcome


def test_a_peer_without_the_read_grant_is_counted_denied(client, owner, deck):
    ns, holder, reader = deck
    stranger = _member(owner, "nogrant")
    r = _list(client, stranger)
    assert r.status_code == 403, r.content
    assert _counted(owner, "cards", stranger["peer_id"], "denied") == 1
    assert _counted(owner, "cards", stranger["peer_id"], "ok") == 0
    assert _search(client, stranger, "anything", ns).status_code == 403
    assert _counted(owner, "card_search", stranger["peer_id"], "denied") == 1


def test_a_refused_parameter_is_counted_error(client, owner, deck):
    ns, holder, reader = deck
    r = _list(client, reader, status="nonsense")
    assert r.status_code == 422, r.content
    assert _counted(owner, "cards", reader["peer_id"], "error") == 1
    assert _counted(owner, "cards", reader["peer_id"], "ok") == 0


def test_a_failed_count_never_fails_the_read(client, owner, deck, monkeypatch):
    """The counter's own contract (apps/mesh/usage.record): a name the CHECK refuses is a lost count,
    logged and added to WRITE_FAILURES, and the read is still served."""
    ns, holder, reader = deck
    from apps.cards import views as card_views
    before = usage.WRITE_FAILURES
    monkeypatch.setattr(usage, "_UPSERT", usage._UPSERT.replace("mesh_route_usage (", "mesh_route_usage_nope ("))
    assert _list(client, reader).status_code == 200
    assert usage.WRITE_FAILURES == before + 1
    assert card_views  # the route module stays importable with the counter broken


# --------------------------------------------------------------------------- the per-seat view

def test_the_usage_view_names_each_seat_per_day_and_route(client, owner, deck):
    ns, holder, reader = deck
    _card(owner, holder, ns, "Ruling | for the view", time="2026-10-08T07:32:13.000Z")
    assert _walk(owner, ns).pending == 0
    _list(client, reader)
    _list(client, reader)
    _search(client, reader, "view", ns)
    r = _usage(client, reader, days=1)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["namespace"] == ns and body["window_days"] == 1 and body["routes"] == list(usage.CARD_ROUTES)
    assert body["interactive_routes"] == ["cards", "card_search"]
    assert body["automatic_routes"] == list(usage.CARD_AUTOMATIC_ROUTES)
    seat = owner.execute("SELECT name FROM peers WHERE id = %s", (reader["peer_id"],)).fetchone()[0]
    mine = {(row["route"]): row for row in body["rows"] if row["seat"] == seat}
    assert mine["cards"]["ok"] >= 2 and mine["card_search"]["ok"] >= 1, body["rows"]
    assert set(mine["cards"]) == {"day", "seat", "peer_id", "route", "ok", "denied", "error"}
    assert mine["cards"]["peer_id"] == str(reader["peer_id"])
    assert body["counting_since"] is not None
    assert set(body["totals"]) == set(usage.CARD_ROUTES)
    assert body["totals"]["cards"]["counting_since"] is not None


def test_reading_the_view_is_not_itself_counted(client, owner, deck):
    ns, holder, reader = deck
    before = {route: _counted(owner, route, reader["peer_id"], "ok") for route in usage.CARD_ROUTES}
    assert _usage(client, reader).status_code == 200
    assert {route: _counted(owner, route, reader["peer_id"], "ok") for route in usage.CARD_ROUTES} == before


@pytest.mark.parametrize("days", ["0", "91", "x"])
def test_the_view_refuses_a_window_it_cannot_answer(client, owner, deck, days):
    ns, holder, reader = deck
    r = _usage(client, reader, days=days)
    assert r.status_code == 422, r.content


def test_the_view_needs_the_card_namespaces_read_grant(client, owner, deck):
    ns, holder, reader = deck
    stranger = _member(owner, "viewnogrant")
    assert _usage(client, stranger).status_code == 403


def test_each_card_route_has_its_own_counting_since(owner):
    """card_summary dates each card route from its own first row, not the table's first day, which is
    weeks earlier (issue #238 C1). Rolled back, so the run's database keeps its rows."""
    import psycopg
    from tests.test_mesh_usage_memory_query import DSN
    from psycopg.rows import dict_row
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            peer = cur.execute("SELECT id FROM peers LIMIT 1").fetchone()[0]
            cur.execute("DELETE FROM mesh_route_usage WHERE route = ANY(%s)", (list(usage.CARD_ROUTES),))
            cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) VALUES "
                        "('inbox', (now() AT TIME ZONE 'UTC')::date - 30, %s, 'ok', 1), "
                        "('cards', (now() AT TIME ZONE 'UTC')::date - 3, %s, 'ok', 2)", (peer, peer))
        conn.row_factory = dict_row
        got = usage.card_summary(conn, days=7)
        conn.rollback()
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).date()
    assert got["routes"]["cards"]["counting_since"] == (today - dt.timedelta(days=3)).isoformat()
    assert got["routes"]["cards"]["calls"] == 2
    assert got["routes"]["card_search"]["counting_since"] is None
    assert got["routes"]["card_search"]["calls"] == 0


def test_card_daily_window_cap_and_counting_since(owner):
    """card_daily's window is `day > today - days` (today and the days-1 before it), it never answers for
    more than CARD_DAILY_MAX_DAYS whatever it is asked, and its counting_since is the EARLIEST card
    route's first day. Rolled back, so the run's database keeps its rows."""
    import psycopg
    from tests.test_mesh_usage_memory_query import DSN
    from psycopg.rows import dict_row
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            peer = cur.execute("SELECT id FROM peers LIMIT 1").fetchone()[0]
            cur.execute("DELETE FROM mesh_route_usage WHERE route = ANY(%s)", (list(usage.CARD_ROUTES),))
            cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) VALUES "
                        "('cards', (now() AT TIME ZONE 'UTC')::date - 10, %s, 'ok', 1), "
                        "('card_search', (now() AT TIME ZONE 'UTC')::date - 2, %s, 'ok', 5), "
                        "('cards', (now() AT TIME ZONE 'UTC')::date - 1, %s, 'denied', 3)", (peer, peer, peer))
        conn.row_factory = dict_row
        two = usage.card_daily(conn, days=2)
        wide = usage.card_daily(conn, days=10_000)
        conn.rollback()
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).date()
    # the day-2 row is outside a two-day window
    assert [(r["day"], r["route"], r["denied"]) for r in two["rows"]] == [
        ((today - dt.timedelta(days=1)).isoformat(), "cards", 3)], two["rows"]
    assert two["counting_since"] == (today - dt.timedelta(days=10)).isoformat()
    assert wide["window_days"] == usage.CARD_DAILY_MAX_DAYS
    assert len(wide["rows"]) == 3


def test_two_peers_sharing_a_name_stay_two_rows(owner):
    """peers.name is not unique, so card_daily groups by peer_id and returns it: two peers given one label
    in a rolled-back transaction are two rows with their own counts, never one sum (GitHub Codex on #301)."""
    import psycopg
    from tests.test_mesh_usage_memory_query import DSN
    from psycopg.rows import dict_row
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            a, b = [r[0] for r in cur.execute("SELECT id FROM peers ORDER BY id LIMIT 2").fetchall()]
            cur.execute("UPDATE peers SET name = 'same-label' WHERE id IN (%s, %s)", (a, b))
            cur.execute("DELETE FROM mesh_route_usage WHERE route = ANY(%s)", (list(usage.CARD_ROUTES),))
            cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) VALUES "
                        "('cards', (now() AT TIME ZONE 'UTC')::date, %s, 'ok', 2), "
                        "('cards', (now() AT TIME ZONE 'UTC')::date, %s, 'denied', 5)", (a, b))
        conn.row_factory = dict_row
        got = usage.card_daily(conn, days=1)
        conn.rollback()
    rows = {r["peer_id"]: r for r in got["rows"]}
    assert set(rows) == {str(a), str(b)}, got["rows"]
    assert (rows[str(a)]["ok"], rows[str(a)]["denied"]) == (2, 0)
    assert (rows[str(b)]["ok"], rows[str(b)]["denied"]) == (0, 5)
    assert {r["seat"] for r in got["rows"]} == {"same-label"}


# --------------------------------------------------------------------------- the client

def test_the_client_card_usage_command_asks_the_view_with_its_days(monkeypatch, tmp_path):
    """`card usage --days 3` calls node.card_usage with 3 and prints its answer; without --days it asks for the
    server's default (no `days` parameter at all)."""
    from tools.meshy_node import cli
    from tools.meshy_node import node as client_node
    seen = []
    monkeypatch.setattr(client_node, "card_usage", lambda path, state, days=None: seen.append(days) or {"rows": []})
    parser = cli.build_parser()
    args = parser.parse_args(["card", "usage", "--base-url", "http://127.0.0.1:9", "--state-file",
                              str(tmp_path / "s.json"), "--days", "3"])
    assert cli._card(args, {}) == 0
    args = parser.parse_args(["card", "usage", "--base-url", "http://127.0.0.1:9", "--state-file",
                              str(tmp_path / "s.json")])
    assert cli._card(args, {}) == 0
    assert seen == [3, None]


# --------------------------------------------------------------------------- C3: reads by purpose

@pytest.mark.parametrize("purpose, route", [
    (None, "cards"), ("", "cards"), ("interactive", "cards"), (" briefing ", "cards_briefing"),
    ("send", "cards_send"), ("write", "cards_write"), ("status", None),
    ("dialog", "cards_unknown"), ("Briefing", "cards_unknown"), ("x" * 40, "cards_unknown")])
def test_card_route_maps_each_list_purpose(purpose, route):
    """Issue #238 C3, divergence 103: no purpose or `interactive` keeps `cards`, a purpose the list knows gets
    its own route, `status` is not counted, and anything else (a search-only purpose, another case, too long)
    is `cards_unknown`: counted, never refused, never folded into the interactive count."""
    assert usage.card_route("cards", purpose) == route


@pytest.mark.parametrize("purpose, route", [
    (None, "card_search"), ("dialog", "card_search_dialog"), ("status", None), ("briefing", "card_search_unknown")])
def test_card_route_maps_each_search_purpose(purpose, route):
    assert usage.card_route("card_search", purpose) == route


def test_every_purpose_route_is_a_counted_route():
    named = {r for routes in usage.CARD_PURPOSE_ROUTES.values() for r in routes.values()}
    named |= {base + "_unknown" for base in usage.CARD_PURPOSE_ROUTES}
    assert named == set(usage.CARD_AUTOMATIC_ROUTES)
    assert set(usage.CARD_ROUTES) <= set(usage.COUNTED)


def test_a_list_with_a_purpose_is_counted_under_that_purpose_and_not_as_interactive(client, owner, deck):
    ns, holder, reader = deck
    _card(owner, holder, ns, "Ruling | by purpose", time="2026-10-08T07:32:13.000Z")
    routes = ("cards", "cards_briefing", "cards_unknown")
    before = {r: _counted(owner, r, reader["peer_id"], "ok") for r in routes}
    assert _list(client, reader, purpose="briefing").status_code == 200
    assert _list(client, reader, purpose="nonsense").status_code == 200    # an unknown purpose is never refused
    assert _list(client, reader, purpose="status").status_code == 200      # and a status poll is not counted
    after = {r: _counted(owner, r, reader["peer_id"], "ok") for r in routes}
    assert after == {"cards": before["cards"], "cards_briefing": before["cards_briefing"] + 1,
                     "cards_unknown": before["cards_unknown"] + 1}


def test_a_card_search_with_the_dialog_purpose_is_counted_apart(client, owner, deck):
    ns, holder, reader = deck
    _card(owner, holder, ns, "Ruling | dialog words", time="2026-10-08T07:32:13.000Z")
    assert _walk(owner, ns).pending == 0
    before = {r: _counted(owner, r, reader["peer_id"], "ok") for r in ("card_search", "card_search_dialog")}
    assert _search(client, reader, "dialog", ns, purpose="dialog").status_code == 200
    assert {r: _counted(owner, r, reader["peer_id"], "ok") for r in ("card_search", "card_search_dialog")} == {
        "card_search": before["card_search"], "card_search_dialog": before["card_search_dialog"] + 1}


def test_a_denied_read_with_a_purpose_is_counted_denied_under_that_purpose(client, owner, deck):
    ns, holder, reader = deck
    stranger = _member(owner, "nogrant-purpose")
    assert _list(client, stranger, purpose="send").status_code == 403
    assert _counted(owner, "cards_send", stranger["peer_id"], "denied") == 1
    assert _counted(owner, "cards", stranger["peer_id"], "denied") == 0


def test_the_client_sends_each_automatic_purpose(monkeypatch, tmp_path):
    """The client's own reads name their purpose and a read somebody asked for names none: node.cards and
    node.search put `purpose` on the query only when given."""
    from tools.meshy_node import node as client_node
    sent = []

    def fake_http_json(method, base, path, headers=None, query=None, **kw):
        sent.append((path, list(query) if not isinstance(query, dict) else sorted(query.items())))
        return 200, {"cards": [], "hits": [], "namespace": "ns"}

    monkeypatch.setattr(client_node, "http_json", fake_http_json)
    monkeypatch.setattr(client_node, "require_peer", lambda path, state: None)
    monkeypatch.setattr(client_node, "fresh_jwt", lambda path, state: "jwt")
    monkeypatch.setattr(client_node, "_get", lambda path, state, route, query: fake_http_json("GET", "", route,
                                                                                          query=query)[1])
    state = {"base_url": "http://127.0.0.1:9", "jwt": "jwt"}
    client_node.cards(tmp_path / "s.json", state, [("seat", "me")])
    client_node.cards(tmp_path / "s.json", state, [("seat", "me")], purpose="briefing")
    client_node.search(tmp_path / "s.json", state, q="q", k=5, rerank=True, ns="ns")
    client_node.search(tmp_path / "s.json", state, q="q", k=5, rerank=True, ns="ns", purpose="dialog")
    purposes = [[v for k, v in query if k == "purpose"] for _, query in sent]
    assert purposes == [[], ["briefing"], [], ["dialog"]]
