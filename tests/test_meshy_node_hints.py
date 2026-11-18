"""Tests for `tools/meshy_node/hints.py` and CLI integration (`meshy_node hints` / `send --hints-check`)."""
from __future__ import annotations

import datetime as _dt
import http.server
import json
import os
import socket
import socketserver
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DSN = os.environ.get("MESHY_TEST_DSN", "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

ISSUER = "meshy-nodehints-test-issuer"
_ISSUER_KEY = ed25519.Ed25519PrivateKey.generate()
PRIV = _ISSUER_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
PUB = _ISSUER_KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:
    django.setup()  # pragma: no cover

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.authapi.didkey import to_did_key  # noqa: E402
from apps.mesh import projection as mesh_projection  # noqa: E402
from apps.meshsem import embedjob, graphwalk, search, send_hints, spaces, views  # noqa: E402
from tests.fixtures import history_rows, meshsem_db as db, signed_rows as sr  # noqa: E402
from tests.fixtures.meshsem_fake import FakeModelHost  # noqa: E402
from tools.meshy_node import cli, hints, node, wire  # noqa: E402

_MEMORY: dict[str, Any] = {}


def _memory_authority() -> dict[str, Any]:
    if not _MEMORY:
        with psycopg.connect(DSN, autocommit=True) as conn:
            _MEMORY.update(db.memory_authority(conn))
    return _MEMORY


def _make_node_state(
    base_url: str, *, peer_id: str | None = None, label: str = "antigravity", with_cnf: bool = True, tmp_dir: Path | None = None
) -> tuple[Path | None, dict[str, Any]]:
    seed = wire.new_seed()
    pub_raw = wire.public_key_of(seed)
    pid = str(peer_id or uuid.uuid4())
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims: dict[str, Any] = {"sub": pid, "iss": ISSUER, "iat": now, "exp": now + 600, "aud": "peer", "scopes": []}
    if with_cnf:
        claims["cnf"] = to_did_key(pub_raw)
    tok = pyjwt.encode(claims, PRIV, algorithm="EdDSA")
    exp_iso = _dt.datetime.fromtimestamp(now + 600, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state: dict[str, Any] = {
        "schema": node.STATE_SCHEMA,
        "status": node.STATUS_MINTED,
        "base_url": node.normalize_base_url(base_url),
        "peer_id": pid,
        "signing_seed_b64url": wire.b64u(seed),
        "public_key_b64url": wire.b64u(pub_raw),
        "jwt": tok,
        "jwt_expires_at": exp_iso,
        "label": label,
    }
    if tmp_dir is None:
        return None, state
    state_path = tmp_dir / f"state-{uuid.uuid4().hex[:8]}.json"
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state_path, state


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _StubHintsServer:
    def __init__(self, handler_fn):
        self.calls: list[dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    parsed = raw.decode("utf-8", "replace")
                rec = {"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": parsed}
                outer.calls.append(rec)
                handler_fn(self, rec)

            def log_message(self, fmt, *args):  # noqa: A002
                return

        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


@pytest.fixture()
def owner_conn():
    with psycopg.connect(DSN, autocommit=True) as conn:
        db.ensure_namespace(conn)
        yield conn


@pytest.fixture()
def dpop_live_world(monkeypatch):
    space, card_ns = db.make_space("clienthints"), f"shared/decisions-{uuid.uuid4().hex[:8]}"
    monkeypatch.setitem(spaces.SPACES, space.name, space)
    with psycopg.connect(DSN, autocommit=True) as conn:
        floor, ns_owner = db.commit_floor(conn), db.peer(conn, "clienthintsnsowner")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id, "
                "claimant_plane, ephemeral, retention_policy) VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                (card_ns, ns_owner["peer_id"], ns_owner["owner_id"]),
            )
    eh, rh = FakeModelHost(dims=db.DIMS), FakeModelHost(dims=db.DIMS)
    rh.rerank_default = 4.5
    send_hints.forget()
    views._HINTS_BOUND.forget()
    providers = [
        {"id": "embed-hqb", "kind": "embed", "base_url": eh.base_url, "space": space.name},
        {"id": "rerank-hqb", "kind": "rerank", "base_url": rh.base_url},
    ]
    ctx = override_settings(
        ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"], MESHY_DSN=DSN, MESHY_DPOP_ENFORCE=True,
        MESHY_JWT_PUBLIC_KEY_PEM=PUB, MESHY_JWT_ISSUER=ISSUER, MESHY_SEMANTIC_SPACE=space.name,
        MESHY_MODEL_PROVIDERS=providers, MESHY_EMBED_FORBIDDEN_ROWS=True,
        MESHY_MEMORY_PEER_ID=str(_memory_authority()["peer_id"]), MESHY_SEND_HINTS_ENABLED=True,
        MESHY_SEND_HINTS_CARD_MIN_SCORE=2.0, MESHY_SEND_HINTS_MESH_MIN_SCORE=1.3,
        MESHY_CARDS_NAMESPACE=card_ns, MESHY_GRAPH_NAMESPACES=f"shared/mesh-chat,{card_ns}",
    )
    ctx.enable()
    search.forget()

    def _bridge(req_handler: http.server.BaseHTTPRequestHandler, rec: dict[str, Any]) -> None:
        extra: dict[str, str] = {}
        if rec["headers"].get("authorization") is not None:
            extra["HTTP_AUTHORIZATION"] = rec["headers"]["authorization"]
        if rec["headers"].get("dpop") is not None:
            extra["HTTP_DPOP"] = rec["headers"]["dpop"]
        resp = Client().post(rec["path"], data=json.dumps(rec["body"]).encode("utf-8"), content_type="application/json", **extra)
        req_handler.send_response(resp.status_code)
        req_handler.send_header("Content-Type", resp.get("Content-Type", "application/json"))
        req_handler.send_header("Content-Length", str(len(resp.content)))
        req_handler.end_headers()
        req_handler.wfile.write(resp.content)

    srv = _StubHintsServer(_bridge)
    yield {"server": srv, "space": space, "card_ns": card_ns, "floor": floor}
    srv.close()
    ctx.disable()
    eh.close()
    rh.close()
    for fn in (search.forget, send_hints.forget, views._HINTS_BOUND.forget):
        fn()


def _drain_live(conn, world_dict: dict[str, Any]) -> None:
    for _ in range(200):
        res = embedjob.catch_up(conn)
        if not res.more or res.rows_processed == 0:
            break
    db.keep_only_rows_after(conn, world_dict["space"], world_dict["floor"])
    while mesh_projection.catch_up(conn).more:
        pass
    graphwalk.catch_up(conn)


def _grant_read(conn, namespace: str, grantee: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace WHERE namespace = %s", (namespace,))
        owner_peer, owner_owner = cur.fetchone()
        cur.execute(
            "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, permission, "
            "granted_by_peer_id, granted_by_owner_id) VALUES (%s,%s,%s,'read',%s,%s) ON CONFLICT DO NOTHING",
            (namespace, grantee["peer_id"], grantee["owner_id"], owner_peer, owner_owner),
        )


def test_live_dpop_enforced_query_and_notice_end_to_end(owner_conn, dpop_live_world, tmp_path):
    srv, card_ns, grp = dpop_live_world["server"], dpop_live_world["card_ns"], _memory_authority()["group"]
    alice = db.peer(owner_conn, "alicehints", group=grp, key=True)
    db.peer(owner_conn, "bobhints", group=grp, key=True)
    carol = db.peer(owner_conn, "carolhints", group=grp, key=True)
    db.grant(owner_conn, alice, "read")
    _grant_read(owner_conn, card_ns, alice)

    shared_words = "dpop nonce binding collision hints prefilter rollout calibration verified"
    planted_mesh = db.row(
        owner_conn, carol,
        {"schema_version": 1, "from": "carolhints", "to": ["deltahints"], "thread_id": "pillar4-dpop-rollout",
         "subject": "dpop pre-send hints question", "type": "question",
         "body": f"Should we enable {shared_words} across seats? SECRET_MESH_BODY_DO_NOT_LEAK"},
        embed_policy="allowed",
    )
    msg_key = planted_mesh["key"]
    cid, rid = uuid.uuid4().hex, uuid.uuid4()
    card_key = f"card/carolhints/{cid}"
    history_rows.insert_seed(
        owner_conn,
        sr.row(
            carol["peer_id"], carol["owner_id"], card_ns, card_key,
            {"schema": "card.v1", "id": cid, "type": "Ruling", "owner": "operator", "seat": "carolhints",
             "time": "2026-11-05T05:27:00.000Z",
             "text": f"Ruling | owner: operator\nAll POST meshsem routes enforce {shared_words}. SECRET_CARD_DO_NOT_LEAK",
             "text_sha256": "0" * 64,
             "source": {"record_uuid": "r1", "block_index": 0, "kind": "transcript", "first_hand": True}},
            rid=rid, call_id=f"call-{rid}", version=1, is_head=True, author_label="carolhints", embed_policy="allowed",
        ),
    )
    _drain_live(owner_conn, dpop_live_world)

    state_path, state = _make_node_state(srv.base_url, peer_id=str(alice["peer_id"]), label="alicehints", with_cnf=True, tmp_dir=tmp_path)
    draft = f"Proposal to launch {shared_words} on POST /v1/mesh/hints before merging."
    ans = hints.query_send_hints((state_path, state), to=["bobhints"], thread_id="new-pillar4-proposal", body=draft, k=3, timeout_s=2.0)
    assert ans["ran"] is True and ans["enabled"] is True and ans["reranked"] is True
    assert {h["key"] for h in ans["hints"]} == {card_key, msg_key}
    raw_dump = json.dumps(ans)
    assert "SECRET_MESH_BODY_DO_NOT_LEAK" not in raw_dump and "SECRET_CARD_DO_NOT_LEAK" not in raw_dump
    assert all("body" not in item and "text" not in item for item in ans["hints"])

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dpop_jti_seen WHERE peer_id = %s", (str(alice["peer_id"]),))
        assert cur.fetchone()[0] == 1

    notice = hints.format_send_hints_notice(ans)
    lines = notice.splitlines()
    assert 1 <= len(lines) <= 3
    assert any("[hint: open ask]" in ln for ln in lines) and any("[hint: recent decision]" in ln for ln in lines)
    assert hints.format_send_hints_notice(ans, in_reply_to=msg_key) == ""

    send_hints.forget()
    ans_from_path = hints.query_send_hints(state_path, to="bobhints", body=draft, k=2, timeout_s=2.0)
    assert ans_from_path["ran"] is True and {h["key"] for h in ans_from_path["hints"]} == {card_key, msg_key}
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dpop_jti_seen WHERE peer_id = %s", (str(alice["peer_id"]),))
        assert cur.fetchone()[0] == 2

    _, bare_state = _make_node_state(srv.base_url, peer_id=str(alice["peer_id"]), label="alicehints", with_cnf=False)
    bare_ans = hints.query_send_hints(bare_state, to=["bobhints"], body=draft, timeout_s=2.0)
    assert bare_ans == {"ran": False, "reason": "auth_403", "http_status": 403, "hints": []}


@pytest.mark.parametrize(
    "status_code,expected_reason",
    [(404, "unsupported_route"), (405, "unsupported_route"), (501, "unsupported_route"),
     (429, "rate_limited"), (401, "auth_401"), (403, "auth_403"),
     (500, "server_500"), (503, "server_503"), (422, "http_422")],
)
def test_fail_open_on_every_non_200_http_status(status_code, expected_reason):
    def _reply_err(req_handler: http.server.BaseHTTPRequestHandler, _rec: dict[str, Any]) -> None:
        payload = json.dumps({"error": {"code": "E_TEST", "status": status_code}}).encode("utf-8")
        req_handler.send_response(status_code)
        req_handler.send_header("Content-Type", "application/json")
        req_handler.send_header("Content-Length", str(len(payload)))
        req_handler.end_headers()
        req_handler.wfile.write(payload)

    srv = _StubHintsServer(_reply_err)
    try:
        _, state = _make_node_state(srv.base_url)
        ans = hints.query_send_hints(state, to=["kxxx"], body="Checking pre-send collision hints fail-open behavior across HTTP errors.")
        assert ans == {"ran": False, "reason": expected_reason, "http_status": status_code, "hints": []}
        assert hints.format_send_hints_notice(ans) == ""
    finally:
        srv.close()


def test_fail_open_on_timeout_oversize_truncated_and_connection_errors():
    gate = threading.Event()

    def _stall(req_handler: http.server.BaseHTTPRequestHandler, _rec: dict[str, Any]) -> None:
        gate.wait(timeout=1.5)
        try:
            req_handler.send_response(200)
            req_handler.end_headers()
        except OSError:
            pass

    srv = _StubHintsServer(_stall)
    try:
        _, state = _make_node_state(srv.base_url)
        t0 = time.monotonic()
        ans = hints.query_send_hints(state, to=["kxxx"], body="Draft body long enough to trigger a stalled server timeout check cleanly.", timeout_s=0.15)
        assert ans == {"ran": False, "reason": "timeout", "hints": []} and (time.monotonic() - t0) < 0.75
    finally:
        gate.set()
        srv.close()

    def _huge(req_handler: http.server.BaseHTTPRequestHandler, _rec: dict[str, Any]) -> None:
        big = b"x" * (hints.MAX_RESPONSE_BYTES + 2048)
        req_handler.send_response(200)
        req_handler.send_header("Content-Type", "application/json")
        req_handler.send_header("Content-Length", str(len(big)))
        req_handler.end_headers()
        req_handler.wfile.write(big)

    srv_big = _StubHintsServer(_huge)
    try:
        _, state_big = _make_node_state(srv_big.base_url)
        ans_big = hints.query_send_hints(state_big, to=["kxxx"], body="Draft body checking oversized HTTP response protection in hints client.")
        assert ans_big == {"ran": False, "reason": "response_too_large", "hints": []}
    finally:
        srv_big.close()

    def _truncated(req_handler: http.server.BaseHTTPRequestHandler, _rec: dict[str, Any]) -> None:
        req_handler.send_response(200)
        req_handler.send_header("Content-Type", "application/json")
        req_handler.send_header("Content-Length", "200")
        req_handler.end_headers()
        req_handler.wfile.write(b"{\"en")
        req_handler.close_connection = True

    srv_trunc = _StubHintsServer(_truncated)
    try:
        _, state_trunc = _make_node_state(srv_trunc.base_url)
        ans_trunc = hints.query_send_hints(state_trunc, to=["kxxx"], body="Draft body checking truncated HTTP JSON fail-open behavior.")
        assert ans_trunc == {"ran": False, "reason": "malformed_response", "hints": []}
    finally:
        srv_trunc.close()

    def _drop_socket(req_handler: http.server.BaseHTTPRequestHandler, _rec: dict[str, Any]) -> None:
        req_handler.close_connection = True
        req_handler.connection.close()

    srv_drop = _StubHintsServer(_drop_socket)
    try:
        _, state_drop = _make_node_state(srv_drop.base_url)
        ans_drop = hints.query_send_hints(state_drop, to=["kxxx"], body="Draft body checking dropped TCP socket before status line fail-open.")
        assert ans_drop == {"ran": False, "reason": "connection_error", "hints": []}
    finally:
        srv_drop.close()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    _, dead_state = _make_node_state(f"http://127.0.0.1:{closed_port}")
    ans_dead = hints.query_send_hints(dead_state, to=["kxxx"], body="Draft body checking closed TCP socket fail-open behavior.", timeout_s=2.5)
    assert ans_dead["ran"] is False and ans_dead["reason"] in {"connection_error", "timeout"} and ans_dead["hints"] == []


def test_expired_jwt_signin_bounded_by_worker_deadline_and_max_bytes(tmp_path):
    trickle_stop = threading.Event()

    def _signin_handler(req_handler: http.server.BaseHTTPRequestHandler, rec: dict[str, Any]) -> None:
        if rec["path"] == "/v1/auth/challenge" and rec["body"].get("peer_id") == "stall":
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", "application/json")
            req_handler.send_header("Content-Length", "200")
            req_handler.end_headers()
            for _ in range(15):
                if trickle_stop.wait(timeout=0.05):
                    break
                try:
                    req_handler.wfile.write(b" ")
                    req_handler.wfile.flush()
                except OSError:
                    break
            return
        if rec["path"] == "/v1/auth/challenge" and rec["body"].get("peer_id") == "huge":
            big = b"x" * (hints.MAX_RESPONSE_BYTES + 1024)
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", "application/json")
            req_handler.send_header("Content-Length", str(len(big)))
            req_handler.end_headers()
            req_handler.wfile.write(big)
            return
        if rec["path"] == "/v1/auth/challenge":
            out = json.dumps({"nonce": "n-123"}).encode("utf-8")
        elif rec["path"] == "/v1/auth/sign-in":
            _, fresh_st = _make_node_state("http://127.0.0.1:9", peer_id=rec["body"]["peer_id"])
            out = json.dumps({"jwt": fresh_st["jwt"], "jwt_expires_at": fresh_st["jwt_expires_at"]}).encode("utf-8")
        else:
            out = json.dumps({"enabled": True, "reranked": True, "hints": [], "thresholds": {}}).encode("utf-8")
        req_handler.send_response(200)
        req_handler.send_header("Content-Type", "application/json")
        req_handler.send_header("Content-Length", str(len(out)))
        req_handler.end_headers()
        req_handler.wfile.write(out)

    srv = _StubHintsServer(_signin_handler)
    try:
        sp_stall, st_stall = _make_node_state(srv.base_url, peer_id="stall", tmp_dir=tmp_path)
        st_stall["jwt_expires_at"] = "2020-01-01T00:00:00Z"
        t0 = time.monotonic()
        ans_stall = hints.query_send_hints((sp_stall, st_stall), to=["kxxx"], body="Draft body checking trickling sign-in response stays bounded by timeout.", timeout_s=0.15)
        assert ans_stall == {"ran": False, "reason": "timeout", "hints": []} and (time.monotonic() - t0) < 0.75

        _, st_huge = _make_node_state(srv.base_url, peer_id="huge")
        st_huge["jwt_expires_at"] = "2020-01-01T00:00:00Z"
        assert hints.query_send_hints(st_huge, to=["kxxx"], body="Draft body checking oversized sign-in response fails open.") == {"ran": False, "reason": "response_too_large", "hints": []}

        sp_ok, st_ok = _make_node_state(srv.base_url, tmp_dir=tmp_path)
        st_ok["jwt_expires_at"] = "2020-01-01T00:00:00Z"
        sp_ok.write_text(json.dumps(st_ok), encoding="utf-8")
        before_bytes = sp_ok.read_bytes()
        assert hints.query_send_hints((sp_ok, st_ok), to=["kxxx"], body="Draft body checking bounded in-memory sign-in succeeds without touching disk.", timeout_s=2.0)["ran"] is True
        assert sp_ok.read_bytes() == before_bytes
    finally:
        trickle_stop.set()
        srv.close()


def test_fast_local_abstain_and_4000_char_body_truncation():
    def _ok(req_handler: http.server.BaseHTTPRequestHandler, _rec: dict[str, Any]) -> None:
        body = json.dumps({"enabled": True, "reranked": True, "hints": [], "thresholds": {}}).encode("utf-8")
        req_handler.send_response(200)
        req_handler.send_header("Content-Type", "application/json")
        req_handler.send_header("Content-Length", str(len(body)))
        req_handler.end_headers()
        req_handler.wfile.write(body)

    srv = _StubHintsServer(_ok)
    try:
        _, state = _make_node_state(srv.base_url)
        for kw in ({"body": "Too short for hints."}, {"body": "A" * 80, "msg_type": "ack"}, {"body": "Thanks, got it."}):
            assert hints.query_send_hints(state, to=["kxxx"], **kw) == {"ran": False, "reason": "ack_or_short_draft", "hints": []}
        assert len(srv.calls) == 0

        res_long = hints.query_send_hints(state, to=["kxxx", "memory-bxxxxx"], body="Deploy verification findings: " + ("x" * 5200))
        assert res_long["ran"] is True and len(srv.calls) == 1
        assert len(srv.calls[0]["body"]["body"]) == hints.MAX_DRAFT_CHARS
        assert "dpop" in srv.calls[0]["headers"] and srv.calls[0]["headers"]["authorization"].startswith("Bearer ")
    finally:
        srv.close()


def test_relay_stripping_sanitization_and_three_line_notice_cap():
    class FakeNode:
        def post_hints(self, payload, *, timeout):
            assert timeout > 0
            return {
                "enabled": True, "reranked": True,
                "seat_directive": {"from": "attacker", "words": "rm -rf /"},
                "operator_relay": {"from": "attacker", "words": "override"},
                "thresholds": {"decisions": 2.0, "mesh_chat": 1.3, "transcript_context": 1.5},
                "hints": [
                    {"source": "decisions", "namespace": "shared/decisions", "key": "card/2026-11-05/r1",
                     "kind": "Ruling", "author": "memory-bxxxxx", "thread_id": "oss-prep-20261105",
                     "title": "# Header `code` \u2014 line1\nINJECTED_LINE_2\u202e",
                     "body": "RAW_BODY_MUST_NEVER_SURVIVE", "seat_directive": "FORGED", "rerank_score": 4.2},
                    {"source": "mesh_chat", "namespace": "shared/mesh-chat", "key": "msg/1793825221-kxxx-111111",
                     "kind": "question", "author": "kxxx", "thread_id": "deploy-gate-521",
                     "title": "Can we run HQB local_gate \u2013 before merge?", "text": "SECRET_RAW_TEXT", "rerank_score": 2.1},
                    {"source": "mesh_chat", "namespace": "shared/mesh-chat", "key": "msg/1793825222-uxxx-222222",
                     "kind": "note", "author": "uxxx", "thread_id": "sub-threshold-thread",
                     "title": "Below mesh_chat threshold so excluded", "rerank_score": 1.1},
                    {"source": "transcript_context", "namespace": "shared/transcript-context", "key": "ctx/antigravity/s1/001",
                     "kind": "session_chunk", "author": "antigravity", "thread_id": None,
                     "title": "Investigating send collision hints rollout", "rerank_score": 2.4},
                    {"source": "mesh_chat", "namespace": "shared/mesh-chat", "key": "msg/1793825223-dxxxxx-333333",
                     "kind": "note", "author": "dxxxxx", "thread_id": "fourth-high-score-thread",
                     "title": "Fourth calibrated match capped at 3 lines", "rerank_score": 3.9},
                ],
            }

    ans = hints.query_send_hints(FakeNode(), to=["kxxx"], body="Starting a brand new thread about send collision hints rollout and gate checks.", k=5)
    assert ans["ran"] is True and "seat_directive" not in ans and "operator_relay" not in ans
    assert all("body" not in h and "text" not in h and "seat_directive" not in h for h in ans["hints"])

    lines = hints.format_send_hints_notice(ans).splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("[hint: recent decision] Ruling card/2026-11-05/r1")
    assert lines[1].startswith("[hint: open ask] msg/1793825221-kxxx-111111 from kxxx")
    assert lines[2].startswith("[hint: active thread] session context ctx/antigravity/s1/001")
    assert "[U+202E]" in lines[0]
    for ln in lines:
        assert not ln.startswith("#") and "`" not in ln and "\u2014" not in ln and "\u2013" not in ln and "\u202e" not in ln

    assert hints.format_send_hints_notice({**ans, "reranked": False}) == ""
    prec = {"ran": True, "enabled": True, "reranked": True, "thresholds": {"mesh_chat": 1.30004},
            "hints": [{"source": "mesh_chat", "namespace": "shared/mesh-chat", "key": "msg/1793825229-kxxx-999999",
                       "kind": "question", "author": "kxxx", "thread_id": "t-prec", "title": "6-dp boundary", "rerank_score": 1.30004}]}
    assert "msg/1793825229-kxxx-999999" in hints.format_send_hints_notice(prec)
    assert hints.query_send_hints(FakeNode(), to=123, closes=456, body="Answering the open deploy gate question directly on the existing thread.")["ran"] is True
    ans_reply = hints.query_send_hints(FakeNode(), to=["kxxx"], in_reply_to="msg/1793825221-kxxx-111111", body="Answering the open deploy gate question directly on the existing thread.")
    assert hints.format_send_hints_notice(ans_reply) == ""


def test_cli_hints_subcommand_and_send_hints_check_never_blocks_send(monkeypatch, capsys, tmp_path):
    state_path, state = _make_node_state("http://fixture", tmp_dir=tmp_path)
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: dict(state))
    parsed = cli.build_parser().parse_args(["hints", "--base-url", "http://fixture", "--state-file", str(state_path), "--to", "kxxx", "--body", "Checking standalone meshy_node hints CLI subcommand output."])
    assert cli._mutating_command(parsed) is False
    assert cli.main(["hints", "--base-url", "http://fixture", "--state-file", str(state_path), "--body-file", str(tmp_path / "missing.txt")]) != 0
    capsys.readouterr()

    canned = {
        "ran": True, "enabled": True, "reranked": True, "space": "qwen3-emb-4b-v1", "truncated": False,
        "thresholds": dict(hints.DEFAULT_THRESHOLDS), "in_reply_to": None, "thread_id": "my-new-thread",
        "hints": [{"source": "mesh_chat", "namespace": "shared/mesh-chat", "key": "msg/1793825319-kxxx-abcdef",
                   "kind": "question", "author": "kxxx", "thread_id": "active-deploy-thread",
                   "title": "Status of Pillar 4 send hints deploy?", "rerank_score": 3.1}],
    }
    hints_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(hints, "query_send_hints", lambda nd, **kw: hints_calls.append(kw) or dict(canned, in_reply_to=kw.get("in_reply_to")))

    rc = cli.main(["hints", "--base-url", "http://fixture", "--state-file", str(state_path), "--to", "kxxx", "--thread", "my-new-thread", "--body", "Checking standalone meshy_node hints CLI subcommand text mode.", "--text"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "[hint: open ask] msg/1793825319-kxxx-abcdef from kxxx (thread active-deploy-thread): Status of Pillar 4 send hints deploy?"

    hints_calls.clear()
    sent_calls: list[dict[str, Any]] = []
    ok_send = {"status": "completed", "result": {"ok": True, "key": "msg/1793825420-ag-111111", "version": 1}, "result_signature_verified": True, "key": "msg/1793825420-ag-111111"}
    monkeypatch.setattr(node, "send", lambda sf, st, **kw: sent_calls.append(kw) or dict(ok_send))
    base_send = ["send", "--base-url", "http://fixture", "--state-file", str(state_path), "--no-recipient-check", "--no-card-check", "--to", "kxxx"]

    assert cli.main([*base_send, "--thread", "new-thread-1", "--body", "Default send makes zero extra HTTP requests unless --hints-check is enabled."]) == 0
    assert len(sent_calls) == 1 and len(hints_calls) == 0
    capsys.readouterr()

    assert cli.main([*base_send, "--hints-check", "--thread", "new-thread-2", "--body", "New thread with --hints-check prints calibrated collision hint on stderr."]) == 0
    assert len(sent_calls) == 2 and len(hints_calls) == 1
    err_lines = [json.loads(ln) for ln in capsys.readouterr().err.strip().splitlines() if ln.strip()]
    assert any(item.get("hint") == "send_collision_hint" and "msg/1793825319-kxxx-abcdef" in item.get("note", "") for item in err_lines)

    hints_calls.clear()
    assert cli.main([*base_send, "--hints-check", "--thread", "active-deploy-thread", "--in-reply-to", "msg/1793825319-kxxx-abcdef", "--body", "Replying on an existing thread skips new-thread collision hints."]) == 0
    assert len(sent_calls) == 3 and len(hints_calls) == 0
    capsys.readouterr()

    monkeypatch.setattr(hints, "query_send_hints", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cli.main([*base_send, "--hints-check", "--thread", "new-thread-3", "--body", "Send must never be blocked even if hints helper raises an unexpected exception."]) == 0
    assert len(sent_calls) == 4
