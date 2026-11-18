"""`python -m tools.meshy_node presence`: a seat publishes its own state (issue #139, divergence 082),
for its hooks: `asleep` with note `compacting` before a compaction, `active` at session start.
Against a recording server (tests/test_meshy_node_owed.py's, given a POST handler), no database:

  * a bearer POST to /v1/peers/me/presence with exactly the state, note and ttl given;
  * no `note` or `ttl_secs` key when they are not given (the server's defaults stand);
  * the client refuses (exit 2, nothing sent) a ttl outside 1..86400, as the server would;
  * a refusal by the server exits 1 with its body.

Run:  pytest tests/test_meshy_node_presence.py -q     (needs no database)
Mutants (each seen to fail a test here, tools/meshy_node/node.py):
  P1  ttl_secs is never sent                 test_hook_beat_sends_state_note_and_ttl
  P2  the ttl bound is not checked           test_a_ttl_out_of_bounds_is_refused_before_sending
  P3  note is sent as null when absent       test_without_note_or_ttl_only_the_state_is_sent
  P4  a bare bearer, no DPoP proof           test_the_beat_carries_a_dpop_proof_for_exactly_this_post
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import struct
import threading
import urllib.parse

import pytest

from tests.test_meshy_node_owed import _Server, _state
from tools.meshy_node import cli


class _PostServer(_Server):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw_in = self.rfile.read(length) if length else b""
        type(self).seen.append({"method": "POST", "path": urllib.parse.urlsplit(self.path).path,
                                "body": json.loads(raw_in) if raw_in else None,
                                "headers": {k.lower(): v for k, v in self.headers.items()}})
        raw = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def server():
    handler = type("P", (_PostServer,), {"seen": [], "status": 200, "body": {"ok": True}, "routes": {}})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    handler.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield handler
    srv.shutdown()
    srv.server_close()


def _run(server, path, *args) -> int:
    return cli.main(["presence", "--base-url", server.base_url, "--state-file", str(path), *args])


def test_hook_beat_sends_state_note_and_ttl(tmp_path, server, capsys):
    """MUTANT P1."""
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--state", "asleep", "--note", "compacting", "--ttl", "900") == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    (request,) = server.seen
    assert request["method"] == "POST" and request["path"] == "/v1/peers/me/presence"
    assert request["body"] == {"state": "asleep", "note": "compacting", "ttl_secs": 900}
    assert request["headers"]["authorization"].startswith("Bearer ")


def test_the_beat_carries_a_dpop_proof_for_exactly_this_post(tmp_path, server, capsys):
    """MUTANT P4 (Codex on #144). The route is a peer write: where DPoP is enforced, the server's
    default, a bearer alone is refused 403, so every hook would fail. The proof (tools/meshy_node/
    wire.dpop_proof: u16 method, u16 target, i64 time, sha256 of the token, 16-byte jti, signature)
    must name this method and this path and be bound to the bearer sent beside it."""
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--state", "active", "--ttl", "60") == 0
    capsys.readouterr()
    (request,) = server.seen
    proof = request["headers"].get("dpop")
    assert proof, "no DPoP header: the server refuses this write where DPoP is enforced"
    raw = base64.urlsafe_b64decode(proof + "=" * (-len(proof) % 4))
    (mlen,) = struct.unpack(">H", raw[:2])
    method = raw[2:2 + mlen].decode()
    off = 2 + mlen
    (tlen,) = struct.unpack(">H", raw[off:off + 2])
    target = raw[off + 2:off + 2 + tlen].decode()
    off += 2 + tlen + 8
    token_hash = raw[off:off + 32]
    jwt = request["headers"]["authorization"].split(" ", 1)[1]
    assert method == "POST"
    assert target == "/v1/peers/me/presence"
    assert token_hash == hashlib.sha256(jwt.encode()).digest(), "the proof is not bound to the bearer sent"


def test_without_note_or_ttl_only_the_state_is_sent(tmp_path, server, capsys):
    """MUTANT P3. An absent key keeps the server's default; a null would be a different request."""
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--state", "active") == 0
    capsys.readouterr()
    (request,) = server.seen
    assert request["body"] == {"state": "active"}


@pytest.mark.parametrize("ttl", ["0", "86401", "-5"])
def test_a_ttl_out_of_bounds_is_refused_before_sending(tmp_path, server, capsys, ttl):
    """MUTANT P2."""
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--state", "active", "--ttl", ttl) == 2
    assert server.seen == [], "an out-of-bounds ttl reached the server"
    assert "--ttl" in capsys.readouterr().err


def test_an_unknown_state_is_refused_by_the_parser(tmp_path, server):
    path = _state(tmp_path, server.base_url)
    with pytest.raises(SystemExit) as e:
        _run(server, path, "--state", "compacting")
    assert e.value.code == 2 and server.seen == []


def test_a_server_refusal_exits_1_with_its_body(tmp_path, server, capsys):
    server.status, server.body = 400, {"error": "E_VALIDATION", "message": "invalid ttl_secs"}
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--state", "active", "--ttl", "60") == 1
    out = capsys.readouterr()
    assert "invalid ttl_secs" in out.out + out.err
