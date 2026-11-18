"""`python -m tools.meshy_node thread`: a thread read through GET /v1/mesh/threads/<id>.

Run: pytest tests/test_meshy_node_thread.py -q (no database needed).
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import urllib.parse
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node, wire  # noqa: E402

ANSWER = {"thread_id": "ops/lease:current", "hits": [], "truncated": False,
          "coverage": {"projected": 1, "total": 1}, "inferred": {"included": False}}


class _Server(http.server.BaseHTTPRequestHandler):
    seen: list = []
    status = 200
    body: dict = ANSWER

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        type(self).seen.append({"path": parsed.path,
                                "query": dict(urllib.parse.parse_qsl(parsed.query)),
                                "headers": {key.lower(): value for key, value in self.headers.items()}})
        raw = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    handler = type("S", (_Server,), {"seen": [], "status": 200, "body": ANSWER})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    handler.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield handler
    srv.shutdown()
    srv.server_close()


def _state(tmp_path: Path, base_url: str) -> Path:
    seed = wire.new_seed()
    path = tmp_path / "node.json"
    path.write_text(json.dumps({
        "schema": node.STATE_SCHEMA, "base_url": base_url, "status": "minted",
        "signing_seed_b64url": wire.b64u(seed),
        "public_key_b64url": wire.b64u(wire.public_key_of(seed)),
        "peer_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4()), "kid": "desktop-x",
        "jwt": "h.p.s", "jwt_expires_at": "2999-01-01T00:00:00Z",
        "memory_peer_id": node.DEFAULT_MEMORY_PEER_ID}), encoding="utf-8")
    return path


def _run(server, path, *args) -> int:
    return cli.main(["thread", "--base-url", server.base_url, "--state-file", str(path), *args])


def test_thread_encodes_a_slash_and_colon_id_and_prints_the_answer(tmp_path, server, capsys):
    """MUTANT: pass the raw id in node.thread; the recording server sees a second path segment."""
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--id", "ops/lease:current") == 0
    assert json.loads(capsys.readouterr().out) == ANSWER
    (request,) = server.seen
    assert request["path"] == "/v1/mesh/threads/ops%2Flease%3Acurrent"
    assert request["query"] == {}
    assert request["headers"]["authorization"] == "Bearer h.p.s"


def test_thread_sends_a_given_limit(tmp_path, server, capsys):
    """MUTANT: omit limit from node.thread's parameters; the server sees no requested bound."""
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--id", "ops/lease", "--limit", "5") == 0
    capsys.readouterr()
    (request,) = server.seen
    assert request["query"] == {"limit": "5"}


def test_thread_non_200_has_the_documented_exit_one(tmp_path, server, capsys):
    """MUTANT: make cli.run return zero after node.ServerSaid; this refusal becomes success."""
    server.status = 503
    server.body = {"error": "E_MESH_DISABLED"}
    assert _run(server, _state(tmp_path, server.base_url), "--id", "ops/lease") == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "http_status": 503, "body": server.body,
        "code": "E_HTTP", "retryable": True,
    }
