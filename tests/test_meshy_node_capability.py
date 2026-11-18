"""The node client's first token feature (B07 slice 6, Q11; docs/DIVERGENCES.md 040; design
docs/design/biscuit-capabilities.md 3.1, critique C8).

Through the cutover the fleet's server is the one that ignores an unknown header, so a token sent
there is silently replaced by the sender's DB grants. `tools/meshy_node` sends
`Meshychat-Capability` only to a server whose `/v1/health` advertises `object-capability-v1`, and
refuses before it sends the call otherwise. It refuses a header over 8 KiB (the server's own cap,
3.1; a larger one would not survive Twisted's 16 KiB header budget beside the JWT and the proof)
before it opens any socket. And it signs `capability_sha256`, the hex sha256 of the exact header
string, into the call's payload; the token itself is never in the payload.

The server here is a STUB on a real socket (http.server, ephemeral port), because what is under
test is what the client sends and refuses to send: every request it receives is recorded. The
real server's half is tests/test_capability_health.py and tests/test_capability_envelope_path.py;
T20 (tests/test_capability_header_budget.py) runs this client's rules against daphne.

Every refusal sits beside its control: a non-advertising server receives no call, the
advertising one receives the token; an 8193-byte token is refused, an 8192-byte one is sent.

Run:  pytest tests/test_meshy_node_capability.py -q     (no database)
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import node, wire  # noqa: E402

HEALTH_BODY = {"status": "ok", "version": "0.1.0", "git_sha": "unknown", "git_dirty": False,
               "db": True, "redis": True, "blob": True}
TOKEN = "CAcS6QIK_gEK" + "x" * 300          # shape only: the stub decides nothing


class Stub:
    """A server that answers /v1/health with `health` and records every request. With
    `redirect` (a status and a Location), it answers the invoke POST with that redirect."""

    def __init__(self, health, health_status: int = 200, redirect: tuple | None = None):
        self.health, self.health_status, self.redirect = health, health_status, redirect
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status: int, body) -> None:
                raw = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _record(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                seen = {"method": self.command, "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw) if raw else None}
                stub.requests.append(seen)
                return seen

            def do_GET(self):
                seen = self._record()
                if seen["path"] == "/v1/health":
                    return self._answer(stub.health_status, stub.health)
                if seen["path"].startswith("/v1/calls/"):
                    call_id = seen["path"].rsplit("/", 1)[1]
                    return self._answer(200, {"call_id": call_id, "status": "completed",
                                              "result": {"op": "memory.read"}})
                if seen["path"] == "/.well-known/jwks.json":
                    return self._answer(200, {"keys": []})
                return self._answer(404, {"error": "E_NOT_FOUND", "message": "not found"})

            def do_POST(self):
                seen = self._record()
                if stub.redirect is not None:
                    status, location = stub.redirect
                    self.send_response(status)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                call_id = seen["body"]["call_id"]
                return self._answer(202, {"call_id": call_id, "status": "completed"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def invokes(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]


def _state(base_url: str) -> dict:
    seed = wire.new_seed()
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return {"schema": node.STATE_SCHEMA, "base_url": base_url, "status": node.STATUS_MINTED,
            "peer_id": str(uuid.uuid4()), "kid": "k-test", "memory_peer_id": str(uuid.uuid4()),
            "signing_seed_b64url": wire.b64u(seed), "jwt": "header.payload.signature",
            "jwt_expires_at": later}


def _read(stub: Stub, tmp_path, token, **kw):
    state = _state(stub.base_url)
    return node.invoke(tmp_path / "state.json", state, skill="memory.read",
                       args={"namespace": "ns/a", "key": "k"}, poll_timeout=1.0,
                       capability=token, **kw)


def test_the_client_sends_the_token_to_a_server_that_advertises_object_capability_v1(tmp_path):
    """The control: the server advertises the field, so the invoke POST carries the header,
    exactly the token, and the SIGNED payload carries its sha256, never the token."""
    with Stub({**HEALTH_BODY, "capabilities": ["object-capability-v1"]}) as stub:
        _read(stub, tmp_path, TOKEN)
    (call,) = stub.invokes()
    assert call["headers"].get("meshychat-capability") == TOKEN
    payload = call["body"]["envelope"]["payload"]
    assert payload["capability_sha256"] == hashlib.sha256(TOKEN.encode("ascii")).hexdigest()
    assert TOKEN not in json.dumps(call["body"]) and TOKEN[-24:] not in json.dumps(call["body"])
    assert [r["path"] for r in stub.requests][0] == "/v1/health", "health is read first"


@pytest.mark.parametrize("health", [
    HEALTH_BODY,                                                   # health response without capabilities
    {**HEALTH_BODY, "capabilities": []},
    {**HEALTH_BODY, "capabilities": ["object-capability-v2"]},
    {**HEALTH_BODY, "capabilities": "object-capability-v1"},       # not a list
    "not json at all",
], ids=["v2", "empty", "another", "a string", "not json"])
@pytest.mark.parametrize("health_status", [200, 503], ids=["healthy", "degraded"])
def test_the_client_refuses_to_send_the_token_to_a_server_that_does_not_advertise_it(
        tmp_path, health, health_status):
    """Critique C8: a server that does not list object-capability-v1 would ignore the header
    and decide by the sender's DB grants. The client refuses, and sends no call at all: the
    only request the server saw is the health read. A degraded server (503, Redis or blob
    storage down) that does not list it is refused the same: the status is not read, the
    body decides (the slice 6 review, finding 4: a client trusting any 5xx survived, because
    the only 503 here was the one that advertises)."""
    raw = health.encode("utf-8") if isinstance(health, str) else health
    with Stub(raw, health_status) as stub:
        with pytest.raises(node.Refused) as exc:
            _read(stub, tmp_path, TOKEN)
    assert "object-capability-v1" in str(exc.value) and TOKEN[:16] not in str(exc.value)
    assert [(r["method"], r["path"]) for r in stub.requests] == [("GET", "/v1/health")]


def test_a_degraded_server_that_advertises_the_field_still_receives_the_token(tmp_path):
    """/v1/health answers 503 when Redis or blob storage is down, with the same body. The
    advertisement is a property of the server, not of its dependencies' health."""
    with Stub({**HEALTH_BODY, "status": "degraded", "redis": False,
               "capabilities": ["object-capability-v1"]}, 503) as stub:
        _read(stub, tmp_path, TOKEN)
    assert len(stub.invokes()) == 1


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_the_client_never_follows_a_redirect_with_the_token(tmp_path, code):
    """Q11: the token goes only to a server whose /v1/health advertised object-capability-v1.
    urllib's default opener answers a 301, 302 or 303 to the invoke POST by sending a GET to
    the Location with every header of the POST: Meshychat-Capability, the bearer and the DPoP
    proof, to a server never asked for its advertisement (the slice 6 review, finding 3,
    measured with two stubs). So a request that carries the token follows no redirect: the
    3xx is the server's answer (ServerSaid), and the other server receives nothing. A 307 or
    308 was never followed for a POST; they are here so the rule covers every redirect.
    Positive control: the other server records a request sent to it directly."""
    advertised = {**HEALTH_BODY, "capabilities": ["object-capability-v1"]}
    with Stub(HEALTH_BODY) as elsewhere:
        with Stub(advertised, redirect=(code, elsewhere.base_url + "/v1/elsewhere")) as stub:
            with pytest.raises(node.ServerSaid) as exc:
                _read(stub, tmp_path, TOKEN)
        assert exc.value.status == code, exc.value
        assert elsewhere.requests == [], [(r["method"], r["path"]) for r in elsewhere.requests]
        assert node.http_json("GET", elsewhere.base_url, "/v1/health")[0] == 200
        assert [r["path"] for r in elsewhere.requests] == ["/v1/health"]
    (call,) = stub.invokes()
    assert call["headers"].get("meshychat-capability") == TOKEN, "the advertising server got it"


def test_the_client_refuses_a_header_over_8_kib_before_it_opens_a_socket(tmp_path):
    """Design 3.1 and T20: 8 KiB is the server's cap, and a bigger header does not fit beside
    the JWT and the DPoP proof in Twisted's 16 KiB. 8193 bytes: refused, and the server saw
    nothing, not even the health read. Control: exactly 8192 bytes is sent."""
    advertised = {**HEALTH_BODY, "capabilities": ["object-capability-v1"]}
    with Stub(advertised) as stub:
        with pytest.raises(node.Refused) as exc:
            _read(stub, tmp_path, "A" * 8193)
    assert stub.requests == [] and "8192" in str(exc.value)
    with Stub(advertised) as stub:
        _read(stub, tmp_path, "A" * 8192)
    (call,) = stub.invokes()
    assert len(call["headers"]["meshychat-capability"]) == 8192


def test_a_token_that_is_not_ascii_text_is_refused_before_a_socket(tmp_path):
    """The header is ASCII (a biscuit is url-safe base64), and the signed hash is over the
    exact header string: a token the client could not send byte for byte is refused."""
    with Stub({**HEALTH_BODY, "capabilities": ["object-capability-v1"]}) as stub:
        for bad in ("café", "a\nb", "", 12345):
            with pytest.raises(node.Refused):
                _read(stub, tmp_path, bad)
    assert stub.requests == []


def test_without_a_token_nothing_changes(tmp_path):
    """No token: no health read, no header, no capability_sha256 in the payload."""
    with Stub(HEALTH_BODY) as stub:
        _read(stub, tmp_path, None)
    (call,) = stub.invokes()
    assert [r["path"] for r in stub.requests][0].startswith("/v1/agents/")
    assert "meshychat-capability" not in call["headers"]
    assert "capability_sha256" not in call["body"]["envelope"]["payload"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
