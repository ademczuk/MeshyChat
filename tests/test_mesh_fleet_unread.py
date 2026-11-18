"""The operator's fleet backlog (divergence 081): GET /v1/mesh/fleet/unread (counts) and
GET /v1/mesh/fleet/unread/<peer_id> (one peer's waiting mail).

WHY. Divergence 074 gave each seat its own fetch backlog. The operator asked to see every seat's
at once (5 Oct, by AskUserQuestion: count, oldest age and trend, plus an on-demand drilldown,
labelled "fetch backlog", no paging), exported as HQB gauges. kxxx's conditions
(msg/<key>, msg/<key>):
  - the exporter is a dedicated peer with ONE narrow grant, the peer-JWT scope mesh:backlog:read,
    and no namespace grant; the counts route takes the bootstrap authority or that scope, and the
    scope must be both in the token and on the peer's current row;
  - the drilldown is the bootstrap authority only;
  - the counts carry no keys, senders or text;
  - the fleet is bounded by what happened in the window, never by peers.status;
  - a test shows the exporter refused on the drilldown and on every other mesh route.

The harness is tests/test_mesh_routes.py's, and the reads are planted with
tests/test_mesh_unread.py's `_read`, the real client's envelope.

Run:  bash ../runt-wt.sh <worktree> tests/test_mesh_fleet_unread.py -q
Mutants (each a one-line break, each seen to fail a test here; apps/mesh/views.py unless named):
  F1  the scope is taken from the token alone         test_a_revoked_column_refuses_a_live_claim
  F2  the scope is taken from the column alone        test_a_column_grant_without_the_claim_is_refused
  F3  the drilldown also accepts the scope            test_the_exporter_is_refused_on_the_drilldown_and_every_other_mesh_route
  F4  an unset authority equals an empty peer id      test_an_unset_authority_fails_closed
  F5  a fleet count is not the peer's own unread total  test_each_count_is_the_number_the_peer_sees_itself
  F6  the counts carry each message key               test_the_counts_carry_no_keys_senders_or_text
  F7  an addressed peer that never wrote is left out (graph.py)  test_a_peer_only_ever_addressed_is_counted
  F8  the cap is not applied                          test_the_cap_truncates_and_says_so
  F9  the down script keeps the fleet rows (down/037) test_037_rollback_removes_fleet_usage_rows_and_restores_036_check
  F10 a refusal is not counted as denied              test_refusals_and_answers_are_counted_in_route_usage
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid

import jwt as pyjwt
import pytest
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

from tests.test_mesh_routes import (DSN, ISSUER, JWT_PRIVATE_PEM, NS, P, _auth, _peer,  # noqa: F401
                                    _sqlmigrate, client, mesh_settings, owner)
from tests.test_mesh_unread import _mail, _read, _target, _unread
from apps.memoryskills import handlers
from apps.mesh import views
from apps.mesh import wait as mesh_wait

SCOPE = views.BACKLOG_SCOPE
NOW = lambda: _dt.datetime.now(_dt.timezone.utc)  # noqa: E731


@pytest.fixture(autouse=True)
def _clean_globals(monkeypatch):
    before = handlers.BOOTSTRAP_AUTHORITY_PEER_ID
    handlers.prime_roster_labels(b"{}")
    monkeypatch.setattr(views, "FLEET_PEER_CAP", 100_000)
    yield
    handlers.set_bootstrap_authority(before)
    handlers.prime_roster_labels(b"{}")


def _bearer(peer_id, scopes=()) -> dict:
    now = int(NOW().timestamp())
    token = pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + 300,
                          "aud": "peer", "scopes": list(scopes)}, JWT_PRIVATE_PEM, algorithm="EdDSA")
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _scope_column(owner, peer, scopes) -> None:
    owner.execute("UPDATE peers SET scopes = %s WHERE id = %s", (list(scopes), peer["peer_id"]))


def _exporter(owner) -> dict:
    """The exporter as kxxx ruled it: the scope on its row and in its token, NO namespace grant.
    A seed peer first, so the exporter can never be the namespace's claimant."""
    _peer(owner, "seed")
    ex = _peer(owner, "exporter", grant=False)
    _scope_column(owner, ex, [SCOPE])
    return ex


def _authority(owner) -> dict:
    op = _peer(owner, "operator", grant=False)
    handlers.set_bootstrap_authority(op["peer_id"])
    return op


def _window(minutes: int = 30) -> str:
    since = (NOW() - _dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return f"?since={since}"


def _counts(client, who, query: str = "", scopes=()) -> dict:
    r = client.get(f"/v1/mesh/fleet/unread{query or _window()}", **_bearer(who["peer_id"], scopes))
    assert r.status_code == 200, r.content[:400]
    return r.json()


def _row_for(body, peer) -> dict | None:
    return next((r for r in body["fleet"]["peers"] if r["peer_id"] == str(peer["peer_id"])), None)


# --------------------------------------------------------------------------- the gate

def test_an_ordinary_peer_is_refused_on_both_routes(client, owner):
    p = _peer(owner, "plain")
    r = client.get("/v1/mesh/fleet/unread", **_auth(p["peer_id"]))
    assert r.status_code == 403 and r.json()["error"] == "E_SCOPE_REQUIRED", r.content
    r = client.get(f"/v1/mesh/fleet/unread/{p['peer_id']}", **_auth(p["peer_id"]))
    assert r.status_code == 403 and r.json()["error"] == "E_NOT_BOOTSTRAP_AUTHORITY", r.content
    assert client.get("/v1/mesh/fleet/unread").status_code == 401


def test_the_authority_reads_both_routes(client, owner):
    op = _authority(owner)
    assert client.get(f"/v1/mesh/fleet/unread{_window()}", **_auth(op["peer_id"])).status_code == 200
    assert client.get(f"/v1/mesh/fleet/unread/{op['peer_id']}", **_auth(op["peer_id"])).status_code == 200


def test_an_unset_authority_fails_closed(client, owner):
    """MUTANT F4: with no authority configured, no peer is it, the configured one included."""
    op = _authority(owner)
    handlers.set_bootstrap_authority("")
    assert client.get("/v1/mesh/fleet/unread", **_auth(op["peer_id"])).status_code == 403
    assert client.get(f"/v1/mesh/fleet/unread/{op['peer_id']}", **_auth(op["peer_id"])).status_code == 403


def test_the_exporter_reads_the_counts(client, owner):
    ex = _exporter(owner)
    _counts(client, ex, scopes=[SCOPE])


def test_a_revoked_column_refuses_a_live_claim(client, owner):
    """MUTANT F1: clearing peers.scopes revokes at once, though the token still claims it."""
    ex = _exporter(owner)
    _counts(client, ex, scopes=[SCOPE])
    _scope_column(owner, ex, [])
    r = client.get(f"/v1/mesh/fleet/unread{_window()}", **_bearer(ex["peer_id"], [SCOPE]))
    assert r.status_code == 403 and r.json()["error"] == "E_SCOPE_REQUIRED", r.content


def test_a_column_grant_without_the_claim_is_refused(client, owner):
    """MUTANT F2: a token minted before the grant cannot use it until it is refreshed."""
    ex = _exporter(owner)
    r = client.get(f"/v1/mesh/fleet/unread{_window()}", **_bearer(ex["peer_id"], []))
    assert r.status_code == 403, r.content


def _mesh_urls() -> list[str]:
    """Every URL apps.mesh mounts, with sample values for its parameters: a route added later is
    walked without anyone listing it here."""
    out = []
    for p in get_resolver("meshychat.urls").url_patterns:
        if isinstance(p, URLResolver) and getattr(p.urlconf_module, "__name__", "") == "apps.mesh.urls":
            for q in p.url_patterns:
                assert isinstance(q, URLPattern)
                route = str(q.pattern)
                route = route.replace("<uuid:peer_id>", str(uuid.uuid4()))
                route = route.replace("<path:thread_id>", "t-walk")
                assert "<" not in route, route
                out.append("/" + route)
    return out


def test_the_exporter_is_refused_on_the_drilldown_and_every_other_mesh_route(client, owner):
    """kxxx's condition: the exporter's credential answers ONE route. MUTANT F3."""
    ex = _exporter(owner)
    urls = _mesh_urls()
    assert "/v1/mesh/fleet/unread" in urls and len(urls) >= 10, urls
    bearer = _bearer(ex["peer_id"], [SCOPE])
    for url in urls:
        if url.endswith("/wait"):
            # The plain wait has no read gate by design and answers from row visibility: the
            # exporter may call it and sees no namespace at all. The
            # addressed wait, which answers shared/mesh-chat's set, is gated and refuses it.
            plain = client.get(url + "?timeout=0", **bearer)
            assert plain.status_code == 204, (url, plain.status_code)
            assert plain[mesh_wait.VISIBLE_NAMESPACES_HEADER] == "0", dict(plain.headers)
            r = client.get(url + "?addressed=1&timeout=1", **bearer)
            assert r.status_code == 403, (url, r.status_code, r.content[:300])
            continue
        r = client.get(url, **bearer)
        if url == "/v1/mesh/fleet/unread":
            assert r.status_code == 200, (url, r.content[:300])
        else:
            assert r.status_code == 403, (url, r.status_code, r.content[:300])


# --------------------------------------------------------------------------- the counts

def test_each_count_is_the_number_the_peer_sees_itself(client, owner):
    """MUTANT F5: a fleet count is that peer's own GET /v1/mesh/unread total, read for read."""
    op = _authority(owner)
    a, b, sender = _peer(owner, "fa"), _peer(owner, "fb"), _peer(owner, "fs")
    target = _target(owner)
    k1 = _mail(owner, sender, to=a["name"])
    _mail(owner, sender, to=a["name"])
    _mail(owner, sender, to=b["name"])
    _read(owner, a, target, k1)
    P.rebuild(owner)
    body = _counts(client, op)
    for p in (a, b):
        own = _unread(client, p, _window())["unread"]
        row = _row_for(body, p)
        assert row is not None and row["count"] == own["total"], (row, own)
        assert row["oldest_at"] == own["oldest_at"], (row, own)
    assert _row_for(body, a)["count"] == 1 and _row_for(body, b)["count"] == 1


def test_the_counts_carry_no_keys_senders_or_text(client, owner):
    """MUTANT F6: per peer only a label, an id, a count and an age."""
    op = _authority(owner)
    a, sender = _peer(owner, "na"), _peer(owner, "ns")
    key = _mail(owner, sender, to=a["name"])
    P.rebuild(owner)
    body = _counts(client, op)
    raw = json.dumps(body)
    assert key not in raw and "synthetic" not in raw
    # The sender is a row of its own (it wrote in the window), never a field of `a`'s.
    assert sender["name"] not in json.dumps(_row_for(body, a))
    assert set(_row_for(body, a)) == {"peer_id", "label", "label_source", "count", "oldest_at",
                                      "oldest_age_secs", "addressed"}


def test_a_peer_only_ever_addressed_is_counted(client, owner):
    """A seat that receives mail and never writes or reads is the most backlogged one: it must be
    in the fleet. MUTANT F7 (graph.py: drop the addressed-label candidates)."""
    op = _authority(owner)
    silent, sender = _peer(owner, "silent"), _peer(owner, "ss")
    _mail(owner, sender, to=silent["name"])
    P.rebuild(owner)
    row = _row_for(_counts(client, op), silent)
    assert row is not None and row["count"] == 1 and row["addressed"] is True, row


def test_a_peer_with_nothing_in_the_window_is_left_out_and_a_roster_peer_is_kept(client, owner):
    op = _authority(owner)
    idle, listed = _peer(owner, "idle"), _peer(owner, "listed")
    handlers.prime_roster_labels(json.dumps(
        {"peers": {str(listed["peer_id"]): f"listed-{uuid.uuid4().hex[:6]}"}}).encode())
    body = _counts(client, op)
    assert _row_for(body, idle) is None
    row = _row_for(body, listed)
    assert row is not None and row["count"] == 0 and row["label_source"] == "roster", row


def test_the_cap_truncates_and_says_so(client, owner, monkeypatch):
    """MUTANT F8."""
    op = _authority(owner)
    sender = _peer(owner, "cs")
    for tag in ("c1", "c2"):
        _mail(owner, sender, to=_peer(owner, tag)["name"])
    P.rebuild(owner)
    monkeypatch.setattr(views, "FLEET_PEER_CAP", 1)
    fleet = _counts(client, op)["fleet"]
    assert len(fleet["peers"]) == 1 and fleet["truncated"] is True, fleet
    assert fleet["candidates"] >= 3 and fleet["unanswered_labels"] is None, fleet


def test_limit_is_refused_on_the_counts(client, owner):
    op = _authority(owner)
    r = client.get("/v1/mesh/fleet/unread?limit=5", **_auth(op["peer_id"]))
    assert r.status_code == 422, r.content


def test_fleet_unread_batches_peer_labels_and_counts_without_n_plus_one(client, owner):
    """Batch fleet_unread agrees peer-for-peer with individual /v1/mesh/unread across roster overrides,
    contested duplicate names, multi-label mail, edited v2 heads, keyless reads, and pruned reads, while
    running a constant number of SQL statements as the covered peer count grows from 3 to 10."""
    from apps.core import dbconn
    from tests.test_mesh_routes import _edit_row_at, _row_at

    op = _authority(owner)
    sender = _peer(owner, "bsend")
    target = _target(owner)
    peers = [_peer(owner, f"bp{i}") for i in range(9)]
    # Contested duplicate name on peers[1] and peers[2] (divergence 066: neither answers on 'dup-seat').
    owner.execute("UPDATE peers SET name = 'dup-seat' WHERE id = ANY(%s::uuid[])",
                  ([peers[1]["peer_id"], peers[2]["peer_id"]],))
    roster_label = f"roster-{uuid.uuid4().hex[:6]}"
    handlers.prime_roster_labels(json.dumps({"peers": {str(peers[0]["peer_id"]): roster_label}}).encode())

    # peers[0]: addressed on both roster label and peers.name in one message -> counted once.
    _mail(owner, sender, to=[roster_label, peers[0]["name"]], minutes_ago=25)
    # peers[1]: contested name ignored; fallback label still counts.
    _mail(owner, sender, to=["dup-seat"], minutes_ago=24)
    _mail(owner, sender, to=[f"peer-{str(peers[1]['peer_id'])[:8]}"], minutes_ago=23)
    # peers[3]: edited v2 head after v1 was read -> pending at edited_at; plus a pruned v2 read -> cleared.
    t0 = NOW() - _dt.timedelta(minutes=22)
    k_edit, rid_edit = _row_at(owner, sender, {"to": [peers[3]["name"]], "body": "v1"}, t0)
    _read(owner, peers[3], target, k_edit, version=1, returned=1, at=t0 + _dt.timedelta(minutes=1))
    _edit_row_at(owner, sender, k_edit, {"to": [peers[3]["name"]], "body": "v2"}, rid_edit,
                 t0 + _dt.timedelta(minutes=5))
    k_pruned, rid_pruned = _row_at(owner, sender, {"to": [peers[3]["name"]], "body": "p1"}, t0)
    _edit_row_at(owner, sender, k_pruned, {"to": [peers[3]["name"]], "body": "p2"}, rid_pruned,
                 t0 + _dt.timedelta(minutes=6))
    _read(owner, peers[3], target, k_pruned, returned=2, pruned=True, at=t0 + _dt.timedelta(minutes=7))
    # peers[4]: keyless read neither fails nor clears (U23).
    _mail(owner, sender, to=[peers[4]["name"]], minutes_ago=15)
    _read(owner, peers[4], target, None)
    # peers[5..7]: addressed mail; peers[8]: wrote mail only (0 unread).
    for i in (5, 6, 7):
        _mail(owner, sender, to=[peers[i]["name"]], minutes_ago=10 + i)
    _mail(owner, peers[8], to=[sender["name"]], minutes_ago=9)
    P.rebuild(owner)

    def count_statements(cap: int) -> tuple[dict, int]:
        n_sql = 0
        orig_serving = dbconn.serving_connection

        class _CountingCursor:
            def __init__(self, cur):
                self._cur = cur

            def __enter__(self):
                self._cur.__enter__()
                return self

            def __exit__(self, *exc):
                return self._cur.__exit__(*exc)

            def execute(self, sql, params=None, **kw):
                nonlocal n_sql
                n_sql += 1
                return self._cur.execute(sql, params, **kw)

            def __getattr__(self, name):
                return getattr(self._cur, name)

        class _CountingConn:
            def __init__(self, c):
                self._c = c

            def cursor(self, *a, **kw):
                return _CountingCursor(self._c.cursor(*a, **kw))

            def __getattr__(self, name):
                return getattr(self._c, name)

        import contextlib

        @contextlib.contextmanager
        def wrapped_serving():
            with orig_serving() as c:
                yield _CountingConn(c)

        old_cap = views.FLEET_PEER_CAP
        views.FLEET_PEER_CAP = cap
        dbconn.serving_connection = wrapped_serving
        try:
            return _counts(client, op), n_sql
        finally:
            dbconn.serving_connection = orig_serving
            views.FLEET_PEER_CAP = old_cap

    _counts(client, op)  # warm one-time auth/pool caches before comparing per-request SQL counts
    _, sql_at_3 = count_statements(3)
    body_all, sql_at_10 = count_statements(100_000)
    assert sql_at_10 == sql_at_3 and sql_at_10 <= 16, (sql_at_3, sql_at_10)
    assert body_all["fleet"]["timed_out"] is False
    for p in peers:
        own_full = _unread(client, p, _window())
        own = own_full["unread"]
        row = _row_for(body_all, p)
        assert row is not None, p
        assert row["label"] == own_full["label"] and row["label_source"] == own_full["label_source"]
        assert row["count"] == own["total"] and row["oldest_at"] == own["oldest_at"], (p, row, own)


def test_a_timed_out_fleet_unread_answers_200_with_no_false_zeros(client, owner, monkeypatch):
    """When graph.fleet_unread_counts passes UNREAD_TIMEOUT_MS, /v1/mesh/fleet/unread answers 200 with
    timed_out true, truncated true, empty peers (no fabricated zero counts), and live coverage."""
    from apps.mesh import graph

    op = _authority(owner)
    a, sender = _peer(owner, "ta"), _peer(owner, "ts")
    _mail(owner, sender, to=[a["name"]])
    P.rebuild(owner)
    real = graph.fleet_unread_counts

    def busy(conn, *args, **kwargs):
        with conn.cursor() as cur:
            cur.execute("SELECT pg_sleep(1)")
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(views, "UNREAD_TIMEOUT_MS", 200)
    monkeypatch.setattr(graph, "fleet_unread_counts", busy)
    body = _counts(client, op)
    fleet = body["fleet"]
    assert fleet["timed_out"] is True and fleet["truncated"] is True and fleet["peers"] == []
    assert fleet["unanswered_labels"] is None and "shorter" in fleet["reason"]
    assert body["coverage"]["total"] is not None
    monkeypatch.setattr(graph, "fleet_unread_counts", real)
    ok = _counts(client, op)
    assert ok["fleet"]["timed_out"] is False and _row_for(ok, a)["count"] == 1


# --------------------------------------------------------------------------- the drilldown

def test_the_drilldown_is_the_peers_own_answer_without_text(client, owner):
    op = _authority(owner)
    a, sender = _peer(owner, "da"), _peer(owner, "ds")
    _mail(owner, sender, to=a["name"])
    _mail(owner, sender, to=a["name"])
    P.rebuild(owner)
    r = client.get(f"/v1/mesh/fleet/unread/{a['peer_id']}{_window()}", **_auth(op["peer_id"]))
    assert r.status_code == 200, r.content[:300]
    mine = _unread(client, a, _window())["unread"]
    assert r.json()["unread"] == mine
    assert "synthetic" not in r.content.decode()
    assert all(set(i) == {"key", "version", "author", "dst", "version_at"} for i in mine["items"])


def test_the_drilldown_of_an_unknown_peer_is_404(client, owner):
    op = _authority(owner)
    r = client.get(f"/v1/mesh/fleet/unread/{uuid.uuid4()}", **_auth(op["peer_id"]))
    assert r.status_code == 404, r.content


# --------------------------------------------------------------------------- usage and schema

def _usage(owner, route, peer, outcome) -> int:
    row = owner.execute("SELECT calls FROM mesh_route_usage WHERE route = %s AND peer_id = %s "
                        "AND outcome = %s AND day = (now() AT TIME ZONE 'UTC')::date",
                        (route, peer["peer_id"], outcome)).fetchone()
    return row[0] if row else 0


def test_refusals_and_answers_are_counted_in_route_usage(client, owner):
    """MUTANT F10."""
    op, plain = _authority(owner), _peer(owner, "u-plain")
    client.get(f"/v1/mesh/fleet/unread{_window()}", **_auth(op["peer_id"]))
    client.get("/v1/mesh/fleet/unread", **_auth(plain["peer_id"]))
    client.get(f"/v1/mesh/fleet/unread/{op['peer_id']}", **_auth(plain["peer_id"]))
    assert _usage(owner, "fleet_unread", op, "ok") == 1
    assert _usage(owner, "fleet_unread", plain, "denied") == 1
    assert _usage(owner, "fleet_unread_peer", plain, "denied") == 1


def test_037_rollback_removes_fleet_usage_rows_and_restores_036_check(owner):
    """The down script deletes the counts of the routes it added before restoring 036's CHECK.
    MUTANT F9: leave the fleet rows behind; the restored CHECK refuses the rollback."""
    peer = _peer(owner, "fleet-rollback")
    owner.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) "
                  "VALUES ('fleet_unread', current_date, %s, 'ok', 1)", (peer["peer_id"],))
    try:
        rolled = _sqlmigrate("--rollback-to", "meshychat:36")
        assert rolled.returncode == 0, rolled.stderr
        left = owner.execute("SELECT count(*) FROM mesh_route_usage "
                             "WHERE route IN ('fleet_unread', 'fleet_unread_peer')").fetchone()[0]
        check = owner.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                              "conname = 'mesh_route_usage_route_check'").fetchone()[0]
        assert left == 0 and "'fleet_unread'" not in check and "'unread'" in check, check
    finally:
        restored = _sqlmigrate()
        assert restored.returncode == 0, restored.stderr
