"""TKT-4629: server-side per-seat counting of transcript searches in `mesh_route_usage`,
and `GET /v1/mesh/transcript-usage` (plus `meshy_node transcript-usage` and
`python -m apps.mesh transcript-usage`).

Acceptance criteria verified here:
  1. Every authenticated single-namespace `GET /v1/mesh/search` targeting the context namespace is
     recorded in `mesh_route_usage` (`route, day, peer_id, outcome, calls`), split between interactive
     `transcript_search` and automatic client probes (`transcript_search_{brief,consensus,context,unknown}`).
     Counts only: query text is never persisted or echoed.
  2. A week's (`days=7`, the route default) transcript searches can be read per seat from
     `GET /v1/mesh/transcript-usage` (`seats` per-seat summary and `rows` per-UTC-day/seat/route).
  3. `window_total` across `TRANSCRIPT_ROUTES` matches the access-log count of authenticated single-`ns`
     context `GET /v1/mesh/search` requests 1-for-1 (and names the two access-log-only pre-counter edges:
     unauthenticated `401` and non-`GET` `405`, plus multi-`ns` `422`).

Run:  .venv/Scripts/python.exe -m pytest tests/test_transcript_search_usage.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.mesh import usage  # noqa: E402


# --------------------------------------------------------------------------- DB-free unit tests

@pytest.mark.parametrize("purpose, expected", [
    (None, "transcript_search"),
    ("", "transcript_search"),
    ("   ", "transcript_search"),
    ("interactive", "transcript_search"),
    (" interactive ", "transcript_search"),
    ("brief", "transcript_search_brief"),
    (" brief ", "transcript_search_brief"),
    ("consensus", "transcript_search_consensus"),
    ("context", "transcript_search_context"),
    ("status", "transcript_search_unknown"),          # never uncounted, unlike card_route("cards", "status")
    ("Brief", "transcript_search_unknown"),
    ("dialog", "transcript_search_unknown"),
    ("x" * 40, "transcript_search_unknown"),
    (123, "transcript_search"),
])
def test_unit_transcript_route_maps_each_purpose_and_never_returns_none(purpose, expected):
    got = usage.transcript_route(purpose)
    assert got == expected
    assert got in usage.TRANSCRIPT_ROUTES


def test_unit_every_transcript_route_is_in_counted_and_migration_058():
    named = set(usage.TRANSCRIPT_PURPOSE_ROUTES.values()) | {"transcript_search_unknown"}
    assert named == set(usage.TRANSCRIPT_AUTOMATIC_ROUTES)
    assert usage.TRANSCRIPT_ROUTES == usage.TRANSCRIPT_INTERACTIVE_ROUTES + usage.TRANSCRIPT_AUTOMATIC_ROUTES
    assert set(usage.TRANSCRIPT_ROUTES) <= set(usage.COUNTED)

    up_sql = (ROOT / "sql_meshychat" / "058_mesh_route_usage_transcript_search.sql").read_text(encoding="utf-8")
    down_sql = (ROOT / "sql_meshychat" / "down" / "058_mesh_route_usage_transcript_search_down.sql").read_text(
        encoding="utf-8"
    )
    for route in usage.COUNTED:
        assert f"'{route}'" in up_sql, f"{route!r} missing from 058 CHECK"
    for route in usage.TRANSCRIPT_ROUTES:
        assert f"'{route}'" in down_sql, f"{route!r} missing from 058 down migration"


def test_unit_search_counted_dispatches_context_before_cards_and_ignores_mail_or_multi_ns(monkeypatch):
    import os
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
    import django
    from django.conf import settings as dj_settings
    if not dj_settings.configured:
        django.setup()
    from django.test import RequestFactory
    from apps.meshsem import views as sem_views

    monkeypatch.setenv("MESHY_CONTEXT_NAMESPACE", "shared/transcript-context")
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", "shared/decisions")
    rf = RequestFactory()

    assert sem_views._search_counted(rf.get("/v1/mesh/search", {"q": "x", "ns": "shared/transcript-context"})) == "transcript_search"
    assert sem_views._search_counted(
        rf.get("/v1/mesh/search", {"q": "x", "ns": "shared/transcript-context", "purpose": "brief"})
    ) == "transcript_search_brief"
    assert sem_views._search_counted(rf.get("/v1/mesh/search", {"q": "x", "ns": "shared/decisions"})) == "card_search"
    assert sem_views._search_counted(rf.get("/v1/mesh/search", {"q": "x"})) is None
    assert sem_views._search_counted(rf.get("/v1/mesh/search", {"q": "x", "ns": "shared/mesh-chat"})) is None
    assert sem_views._search_counted(
        rf.get("/v1/mesh/search", {"q": "x", "ns": ["shared/transcript-context", "shared/mesh-chat"]})
    ) is None

    # Branch-order parity with views.search: if both env vars ever pointed at one namespace, context wins first.
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", "shared/transcript-context")
    assert sem_views._search_counted(rf.get("/v1/mesh/search", {"q": "x", "ns": "shared/transcript-context"})) == "transcript_search"


def test_unit_render_transcript_usage_formats_empty_and_populated_windows():
    empty = {
        "window_days": 7,
        "counting_since": None,
        "seats": [],
        "window_total": {"ok": 0, "denied": 0, "error": 0, "total": 0},
        "write_failures_this_process": 0,
    }
    lines = usage.render_transcript_usage(empty)
    assert "counting since never" in lines[0]
    assert "no transcript searches counted in this window" in "\n".join(lines)

    populated = {
        "window_days": 7,
        "counting_since": "2026-11-03",
        "seats": [
            {"seat": "dxxxxx", "peer_id": "11111111-1111-1111-1111-111111111111",
             "interactive_ok": 4, "automatic_ok": 9, "ok": 13, "denied": 0, "error": 1, "total": 14},
        ],
        "window_total": {"ok": 13, "denied": 0, "error": 1, "total": 14},
        "write_failures_this_process": 0,
    }
    rendered = "\n".join(usage.render_transcript_usage(populated))
    assert "last 7 day(s); counting since 2026-11-03" in rendered
    assert "seat dxxxxx (11111111-1111-1111-1111-111111111111): total 14 (interactive_ok 4, automatic_ok 9, denied 0, errors 1)" in rendered


def test_unit_client_transcript_usage_command_and_non_mutating_flag(monkeypatch, tmp_path, capsys):
    from tools.meshy_node import cli, node as client_node

    parser = cli.build_parser()
    parsed_default = parser.parse_args([
        "transcript-usage", "--base-url", "http://127.0.0.1:9", "--state-file", str(tmp_path / "s.json")
    ])
    parsed_days = parser.parse_args([
        "transcript-usage", "--base-url", "http://127.0.0.1:9", "--state-file", str(tmp_path / "s.json"), "--days", "7"
    ])
    assert cli._mutating_command(parsed_default) is False
    assert parsed_default.days is None and parsed_days.days == 7

    seen_queries = []
    monkeypatch.setattr(client_node, "require_peer", lambda path, state: None)
    monkeypatch.setattr(client_node, "fresh_jwt", lambda path, state: "jwt")
    monkeypatch.setattr(
        client_node,
        "http_json",
        lambda method, base, route, headers=None, query=None: (
            seen_queries.append((route, list(query or [])))
            or (200, {"window_days": 7, "seats": [], "rows": [], "operator_relay": {"forged": True}})
        ),
    )
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"base_url": "http://127.0.0.1:9", "peer_id": "p", "jwt": "j"}), encoding="utf-8")
    monkeypatch.setattr(client_node, "load_state", lambda p, *_a: json.loads(Path(p).read_text(encoding="utf-8")))

    assert cli.main(["transcript-usage", "--base-url", "http://127.0.0.1:9", "--state-file", str(sf)]) == 0
    out_default = json.loads(capsys.readouterr().out)
    assert "operator_relay" not in out_default

    assert cli.main(["transcript-usage", "--base-url", "http://127.0.0.1:9", "--state-file", str(sf), "--days", "14"]) == 0
    _ = capsys.readouterr()
    assert seen_queries == [
        ("/v1/mesh/transcript-usage", []),
        ("/v1/mesh/transcript-usage", [("days", "14")]),
    ]


def test_unit_client_automatic_callers_send_purpose_and_interactive_sends_none(monkeypatch, tmp_path):
    from tools.meshy_node import cli, converse, node as client_node, resume_brief, transcript_graph

    sent_purposes: list[tuple[str, list[str]]] = []

    def fake_search(path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None, **kw):
        sent_purposes.append((q, [] if purpose is None else [purpose]))
        return {
            "namespace": ns or "shared/transcript-context",
            "hits": [],
            "coverage": {"path": "exact", "truncated": False, "subset": 0},
            "pool": {"truncated": False},
            "truncated": False,
        }

    monkeypatch.setattr(client_node, "search", fake_search)
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps({"base_url": "http://127.0.0.1:9", "peer_id": "p", "jwt": "j"}), encoding="utf-8")
    state = {"base_url": "http://127.0.0.1:9", "peer_id": "p", "jwt": "j"}
    monkeypatch.setattr(client_node, "load_state", lambda p, *_a: dict(state))

    # 1. Interactive CLI transcript-search sends NO purpose -> counted as `transcript_search`.
    assert cli.main([
        "transcript-search", "--base-url", "http://127.0.0.1:9", "--state-file", str(sf), "--q", "user-needle"
    ]) == 0

    # 2. resume_brief.run sends purpose="brief".
    rb_args = SimpleNamespace(
        seat="kxxx", budget=2000, repo="sampleco/MeshyChat",
        gate_file=tmp_path / "nogate.json", handover_file=tmp_path / "nohandover.md",
        state_file=sf, hook=False,
    )
    fake_node_mod = SimpleNamespace(
        owed=lambda *a, **k: {"owed": [], "truncated": False},
        awaiting=lambda *a, **k: {"awaiting": []},
    )
    monkeypatch.setattr(resume_brief, "gh_json", lambda argv, timeout=20: [])
    assert resume_brief.run(rb_args, state, fake_node_mod, transcript_graph) == 0

    # 3. converse._collect_transcript_pointers (`read/context --with-context`) sends purpose="context".
    ptrs, err = converse._collect_transcript_pointers(sf, state, query_text="deploy gate")
    assert ptrs == [] and err is None

    # 4. transcript_graph consensus index & seat probes send purpose="consensus" when automatic (query=None),
    #    and None when an operator/seat passed an explicit topic query (`transcript-consensus --query topic`).
    transcript_graph._probe_transcript_index(
        sf, state, transcript_ns="shared/transcript-context", query=None, seat_filter="kxxx", since_iso="2026-11-08T00:00:00Z"
    )
    transcript_graph._probe_transcript_index(
        sf, state, transcript_ns="shared/transcript-context", query="explicit-topic", seat_filter=None, since_iso="2026-11-08T00:00:00Z"
    )
    transcript_graph._probe_seat_latest_transcript(
        sf, state, transcript_ns="shared/transcript-context", seat_name="kxxx", since_iso="2026-11-08T00:00:00Z"
    )

    assert [p for _, p in sent_purposes] == [
        [],
        ["brief"],
        ["context"],
        ["consensus"],
        [],
        ["consensus"],
    ]


# --------------------------------------------------------------------------- PG17 integration fixtures & tests

try:
    from tests.test_ctx_index_job import DSN, _batch, _tick, app, env, owner, world  # noqa: E402,F401
    from tests.fixtures import meshsem_db as db  # noqa: E402
    from tests.test_meshsem_routes import DSN as ROUTE_DSN, ISSUER, PUB, _bearer  # noqa: E402
except Exception:  # pragma: no cover - allows `--noconftest -k unit_` without PG env
    pass


@pytest.fixture()
def tx_http(app, world):
    """Real Django URLconf over a freshly indexed context namespace with two readers and one ungranted peer."""
    from django.test import Client, override_settings

    _batch(world, 1, [
        ("user_text", "how do we check zq9txcanary search counts per seat"),
        ("assistant_text", "query mesh_route_usage via transcript-usage"),
    ])
    assert _tick(app, world).manifests_indexed == 1
    o = world["owner"]
    o.execute("DELETE FROM mesh_route_usage WHERE route = ANY(%s)", (list(usage.TRANSCRIPT_ROUTES),))
    holder = o.execute(
        "SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace WHERE namespace = %s",
        (world["ns"],),
    ).fetchone()
    reader = db.peer(o, "txseat-alpha")
    reader2 = db.peer(o, "txseat-beta")
    stranger = db.peer(o, "txseat-nogrant")
    for peer in (reader, reader2):
        o.execute(
            "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, permission, "
            "granted_by_peer_id, granted_by_owner_id) VALUES (%s, %s, %s, 'read', %s, %s)",
            (world["ns"], peer["peer_id"], peer["owner_id"], holder[0], holder[1]),
        )
    http_client = Client()
    with override_settings(
        ROOT_URLCONF="meshychat.urls",
        ALLOWED_HOSTS=["*"],
        MESHY_DSN=ROUTE_DSN,
        MESHY_JWT_PUBLIC_KEY_PEM=PUB,
        MESHY_JWT_ISSUER=ISSUER,
    ):
        yield {
            "client": http_client,
            "ns": world["ns"],
            "owner": o,
            "reader": reader,
            "reader2": reader2,
            "stranger": stranger,
        }


def _counted(owner_conn, route: str, peer_id, outcome: str) -> int:
    row = owner_conn.execute(
        "SELECT COALESCE(sum(calls), 0) FROM mesh_route_usage "
        "WHERE route = %s AND peer_id = %s AND outcome = %s "
        "AND day = (now() AT TIME ZONE 'UTC')::date",
        (route, str(peer_id), outcome),
    ).fetchone()
    return int(row[0])


def test_interactive_transcript_search_counts_ok_and_mail_or_multi_ns_do_not(tx_http):
    from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

    c, ns, o, reader = tx_http["client"], tx_http["ns"], tx_http["owner"], tx_http["reader"]
    url = f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0"
    assert c.get(url, **_bearer(reader["peer_id"])).status_code == 200
    assert c.get(url, **_bearer(reader["peer_id"])).status_code == 200
    assert _counted(o, "transcript_search", reader["peer_id"], "ok") == 2

    # Mail searches (default or explicit) and multi-ns requests never increment TRANSCRIPT_ROUTES.
    snap = {
        (r, out): _counted(o, r, reader["peer_id"], out)
        for r in usage.TRANSCRIPT_ROUTES
        for out in ("ok", "denied", "error")
    }
    c.get("/v1/mesh/search?q=anything&rerank=0", **_bearer(reader["peer_id"]))
    c.get(f"/v1/mesh/search?q=anything&rerank=0&ns={MESH_CHAT_NAMESPACE}", **_bearer(reader["peer_id"]))
    multi = c.get(f"/v1/mesh/search?q=anything&rerank=0&ns={ns}&ns={MESH_CHAT_NAMESPACE}", **_bearer(reader["peer_id"]))
    assert multi.status_code == 422
    after = {
        (r, out): _counted(o, r, reader["peer_id"], out)
        for r in usage.TRANSCRIPT_ROUTES
        for out in ("ok", "denied", "error")
    }
    assert after == snap


def test_counts_only_never_persist_or_echo_query_text(tx_http):
    c, ns, o, reader = tx_http["client"], tx_http["ns"], tx_http["owner"], tx_http["reader"]
    canary_q = "canary-secret-query-9918273"
    r = c.get(f"/v1/mesh/search?ns={ns}&q={canary_q}&rerank=0", **_bearer(reader["peer_id"]))
    assert r.status_code == 200

    db_rows = o.execute(
        "SELECT route, day::text, peer_id::text, outcome, calls::text FROM mesh_route_usage "
        "WHERE route = ANY(%s)",
        (list(usage.TRANSCRIPT_ROUTES),),
    ).fetchall()
    assert db_rows
    assert canary_q not in repr(db_rows)

    view = c.get("/v1/mesh/transcript-usage", **_bearer(reader["peer_id"]))
    assert view.status_code == 200
    assert canary_q not in view.content.decode("utf-8")
    assert canary_q not in "\n".join(usage.render_transcript_usage(view.json()))


def test_purpose_routes_separate_automatic_probes_from_interactive_and_never_drop_status(tx_http):
    c, ns, o, reader = tx_http["client"], tx_http["ns"], tx_http["owner"], tx_http["reader"]
    for p in ("brief", "consensus", "context", "nonsense", "status"):
        resp = c.get(f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0&purpose={p}", **_bearer(reader["peer_id"]))
        assert resp.status_code == 200, (p, resp.content)

    got = {r: _counted(o, r, reader["peer_id"], "ok") for r in usage.TRANSCRIPT_ROUTES}
    assert got == {
        "transcript_search": 0,
        "transcript_search_brief": 1,
        "transcript_search_consensus": 1,
        "transcript_search_context": 1,
        "transcript_search_unknown": 2,
    }


def test_denied_and_error_outcomes_including_503_busy_are_counted(tx_http, monkeypatch):
    from apps.meshsem import ctxsearch

    c, ns, o = tx_http["client"], tx_http["ns"], tx_http["owner"]
    reader, stranger = tx_http["reader"], tx_http["stranger"]

    # 403 without read grant (interactive and purpose-tagged)
    assert c.get(f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", **_bearer(stranger["peer_id"])).status_code == 403
    assert c.get(
        f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0&purpose=brief", **_bearer(stranger["peer_id"])
    ).status_code == 403
    assert _counted(o, "transcript_search", stranger["peer_id"], "denied") == 1
    assert _counted(o, "transcript_search_brief", stranger["peer_id"], "denied") == 1

    # 422 validation errors (missing q, card-only parameter, index disabled)
    assert c.get(f"/v1/mesh/search?ns={ns}&q=&rerank=0", **_bearer(reader["peer_id"])).status_code == 422
    assert c.get(f"/v1/mesh/search?ns={ns}&q=zq9txcanary&type=Ruling", **_bearer(reader["peer_id"])).status_code == 422
    monkeypatch.setenv("MESHY_CONTEXT_INDEX", "false")
    assert c.get(f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", **_bearer(reader["peer_id"])).status_code == 422
    monkeypatch.setenv("MESHY_CONTEXT_INDEX", "true")
    assert _counted(o, "transcript_search", reader["peer_id"], "error") == 3

    # 503 E_CTX_SEARCH_BUSY when every transcript-search slot is occupied
    full_gate = ctxsearch._Gate(1)
    assert full_gate._sem.acquire(timeout=0.0)
    monkeypatch.setattr(ctxsearch, "_gate", full_gate)
    monkeypatch.setattr(ctxsearch, "STORE_QUEUE_S", 0.0)
    busy_resp = c.get(f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", **_bearer(reader["peer_id"]))
    assert busy_resp.status_code == 503 and busy_resp.json()["error"] == ctxsearch.E_BUSY
    assert busy_resp.headers.get("Retry-After") == str(ctxsearch.BUSY_RETRY_AFTER_S)
    assert _counted(o, "transcript_search", reader["peer_id"], "error") == 4


def test_failed_usage_write_increments_write_failures_without_failing_search(tx_http, monkeypatch):
    c, ns, reader = tx_http["client"], tx_http["ns"], tx_http["reader"]
    before = usage.WRITE_FAILURES
    monkeypatch.setattr(usage, "_UPSERT", usage._UPSERT.replace("mesh_route_usage (", "mesh_route_usage_nope ("))
    resp = c.get(f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", **_bearer(reader["peer_id"]))
    assert resp.status_code == 200
    assert usage.WRITE_FAILURES == before + 1


def test_weekly_per_seat_view_and_access_log_reconciliation(tx_http):
    """Acceptance criteria 2 & 3: default 7-day per-seat read + exact reconciliation with an access-log
    recorder across 200, 403, 422, 401, 405, mail search, multi-ns search, and `/v1/mesh/transcript-usage` itself."""
    from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

    c, ns, o = tx_http["client"], tx_http["ns"], tx_http["owner"]
    reader, reader2, stranger = tx_http["reader"], tx_http["reader2"], tx_http["stranger"]

    access_log: list[dict] = []

    def logged(method: str, url: str, *, peer=None):
        headers = _bearer(peer["peer_id"]) if peer is not None else {}
        fn = c.get if method == "GET" else c.post
        resp = fn(url, **headers)
        parsed = urlsplit(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        ns_list = [x.strip() for x in qs.get("ns", []) if x.strip()]
        access_log.append({
            "method": method,
            "path": parsed.path,
            "ns_list": ns_list,
            "authenticated": peer is not None and resp.status_code != 401,
            "status": resp.status_code,
        })
        return resp

    # Seat 1 (`reader`): 2 interactive ok, 1 brief ok, 1 consensus ok, 1 bad-param 422
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", peer=reader).status_code == 200
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", peer=reader).status_code == 200
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0&purpose=brief", peer=reader).status_code == 200
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0&purpose=consensus", peer=reader).status_code == 200
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=&rerank=0", peer=reader).status_code == 422

    # Seat 2 (`reader2`): 1 interactive ok, 1 context ok
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", peer=reader2).status_code == 200
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0&purpose=context", peer=reader2).status_code == 200

    # Seat 3 (`stranger`): 1 denied 403
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", peer=stranger).status_code == 403

    # Requests that appear in raw HTTP access logs on `/v1/mesh/search` or `/v1/mesh/transcript-usage`
    # and have documented non-counted boundaries:
    assert logged("GET", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", peer=None).status_code == 401
    assert logged("POST", f"/v1/mesh/search?ns={ns}&q=zq9txcanary&rerank=0", peer=reader).status_code == 405
    assert logged("GET", f"/v1/mesh/search?ns={ns}&ns={MESH_CHAT_NAMESPACE}&q=zq9txcanary&rerank=0", peer=reader).status_code == 422
    assert logged("GET", "/v1/mesh/search?q=zq9txcanary&rerank=0", peer=reader).status_code in (200, 403)

    # Read the weekly view (default days=7) — reading it must NOT count itself.
    assert logged("GET", "/v1/mesh/transcript-usage", peer=stranger).status_code == 403
    view = logged("GET", "/v1/mesh/transcript-usage", peer=reader)
    assert view.status_code == 200, view.content
    body = view.json()

    assert body["namespace"] == ns
    assert body["window_days"] == 7
    assert body["routes"] == list(usage.TRANSCRIPT_ROUTES)
    assert body["interactive_routes"] == ["transcript_search"]
    assert body["automatic_routes"] == list(usage.TRANSCRIPT_AUTOMATIC_ROUTES)
    assert set(body["totals"]) == set(usage.TRANSCRIPT_ROUTES)

    seats_by_id = {s["peer_id"]: s for s in body["seats"]}
    assert seats_by_id[str(reader["peer_id"])] == {
        "seat": o.execute("SELECT name FROM peers WHERE id = %s", (reader["peer_id"],)).fetchone()[0],
        "peer_id": str(reader["peer_id"]),
        "interactive_ok": 2,
        "automatic_ok": 2,
        "ok": 4,
        "denied": 0,
        "error": 1,
        "total": 5,
    }
    assert seats_by_id[str(reader2["peer_id"])]["interactive_ok"] == 1
    assert seats_by_id[str(reader2["peer_id"])]["automatic_ok"] == 1
    assert seats_by_id[str(reader2["peer_id"])]["total"] == 2
    assert seats_by_id[str(stranger["peer_id"])]["denied"] == 1
    assert seats_by_id[str(stranger["peer_id"])]["total"] == 1

    # Access-log reconciliation: authenticated single-ns GET /v1/mesh/search on `ns` matches `window_total` 1-to-1.
    expected_access_log_counted = [
        e for e in access_log
        if e["method"] == "GET"
        and e["path"] == "/v1/mesh/search"
        and e["authenticated"]
        and e["ns_list"] == [ns]
    ]
    assert len(expected_access_log_counted) == 8
    assert body["window_total"] == {"ok": 6, "denied": 1, "error": 1, "total": 8}
    assert sum(s["total"] for s in body["seats"]) == len(expected_access_log_counted)

    # Window validation on GET /v1/mesh/transcript-usage
    for bad_days in ("0", "91", "abc"):
        assert c.get(f"/v1/mesh/transcript-usage?days={bad_days}", **_bearer(reader["peer_id"])).status_code == 422


def test_transcript_daily_window_cap_counting_since_and_duplicate_peer_names(owner):
    """Rolled-back SQL check: 7-day vs 2-day window, 90-day cap, per-route counting_since, and duplicate peer labels."""
    import psycopg
    from psycopg.rows import dict_row
    from tests.test_mesh_usage_memory_query import DSN as USAGE_DSN

    with psycopg.connect(USAGE_DSN) as conn:
        with conn.cursor() as cur:
            a, b = [r[0] for r in cur.execute("SELECT id FROM peers ORDER BY id LIMIT 2").fetchall()]
            cur.execute("UPDATE peers SET name = 'shared-seat-label' WHERE id IN (%s, %s)", (a, b))
            cur.execute("DELETE FROM mesh_route_usage WHERE route = ANY(%s)", (list(usage.TRANSCRIPT_ROUTES),))
            cur.execute(
                "INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) VALUES "
                "('transcript_search', (now() AT TIME ZONE 'UTC')::date - 10, %s, 'ok', 4), "
                "('transcript_search_brief', (now() AT TIME ZONE 'UTC')::date - 2, %s, 'ok', 3), "
                "('transcript_search', (now() AT TIME ZONE 'UTC')::date, %s, 'ok', 2), "
                "('transcript_search', (now() AT TIME ZONE 'UTC')::date, %s, 'denied', 5)",
                (a, a, a, b),
            )
        conn.row_factory = dict_row
        week = usage.transcript_daily(conn, days=7)
        wide = usage.transcript_daily(conn, days=10_000)
        summ = usage.transcript_summary(conn, days=7)
        conn.rollback()

    today = _dt.datetime.now(_dt.timezone.utc).date()
    assert week["counting_since"] == (today - _dt.timedelta(days=10)).isoformat()
    assert summ["routes"]["transcript_search"]["counting_since"] == (today - _dt.timedelta(days=10)).isoformat()
    assert summ["routes"]["transcript_search_brief"]["counting_since"] == (today - _dt.timedelta(days=2)).isoformat()
    assert summ["routes"]["transcript_search_consensus"]["counting_since"] is None
    assert week["window_total"] == {"ok": 5, "denied": 5, "error": 0, "total": 10}
    assert summ["window_total"] == week["window_total"]
    assert wide["window_days"] == usage.TRANSCRIPT_DAILY_MAX_DAYS

    by_peer = {s["peer_id"]: s for s in week["seats"]}
    assert set(by_peer) == {str(a), str(b)}
    assert by_peer[str(a)]["interactive_ok"] == 2 and by_peer[str(a)]["automatic_ok"] == 3
    assert by_peer[str(b)]["denied"] == 5 and by_peer[str(b)]["ok"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
