"""`meshy_node adopt`: a node that already has an identity moves to this client without a new mint.

At the 2026-09-27 cutover every AgentMeshy node kept its peer id and Ed25519 key
(D-2026-09-27-28). Minting it again would make a second peer with a new address and split the
node's mail and authorship in two. `adopt` writes this client's state file from the old
client's, and writes it only when the server has proven the key: the peer signs in with the
seed, and the server's own key list holds that public key, active, under the kid written.

The fake server below checks each sign-in with the SERVER's primitives over the server's
canonical bytes (apps.crypto.primitives, apps.authapi.config), a different instrument from
the client's wire.py (docs/V1-DESIGN-RULES.md C5). Every refusal sits beside the control that
differs from it in the one variable under test (C1), and every refusal is checked to have
written nothing and to have left the old file byte for byte as it was.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_adopt.py -q -p no:cacheprovider
"""
from __future__ import annotations

import http.server
import json
import secrets
import sys
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.authapi.config import SIGNIN_PURPOSE  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from tools.meshy_node import cli, node, wire  # noqa: E402

SERVER_KID = "desktop-" + "ab12" * 8


class _Server(http.server.BaseHTTPRequestHandler):
    """Answers the three routes adopt uses, like the server does, and records every request.
    `signin` maps peer_id -> the public keys a sign-in is accepted with (its active keys);
    `listed` maps peer_id -> what GET /v1/peers/me/keys answers ({kid, public_key, active})."""
    signin: dict = {}
    listed: dict = {}
    nonces: dict = {}
    seen: list = []

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append(("POST", self.path))
        peer_id = body.get("peer_id")
        if self.path == "/v1/auth/challenge":
            if peer_id not in self.signin:
                return self._send(404, {"error": "E_NOT_FOUND"})
            nonce = secrets.token_urlsafe(16)
            self.nonces[peer_id] = nonce
            return self._send(200, {"nonce": nonce})
        if self.path == "/v1/auth/sign-in":
            nonce = self.nonces.pop(peer_id, None)
            message = P.jcs({"aud": "peer", "nonce": nonce, "peer_id": peer_id,
                             "purpose": SIGNIN_PURPOSE})
            try:
                signature = P.b64u_decode(body.get("signature_b64", ""))
            except Exception:
                signature = b""
            for public in self.signin.get(peer_id, []):
                if nonce and P.verify_strict(P.b64u_decode(public), message, signature):
                    return self._send(200, {"jwt": f"jwt.{peer_id}",
                                            "jwt_expires_at": "2999-01-01T00:00:00Z"})
            return self._send(401, {"error": "E_AUTH_INVALID"})
        return self._send(404, {"error": "E_NOT_FOUND"})

    def do_GET(self):  # noqa: N802
        type(self).seen.append(("GET", self.path))
        auth = self.headers.get("Authorization") or ""
        if self.path == "/v1/peers/me/keys" and auth.startswith("Bearer jwt."):
            return self._send(200, self.listed.get(auth[len("Bearer jwt."):], []))
        return self._send(401, {"error": "E_AUTH_INVALID"})

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    handler = type("S", (_Server,), {"signin": {}, "listed": {}, "nonces": {}, "seen": []})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def _old_state(tmp_path: Path, *, seed: bytes, peer_id: str, **extra) -> Path:
    """An AgentMeshy client's state file, in its own shape (signing_key_b64url, a refresh
    token and a JWT of its own, fields this client has never heard of)."""
    fields = {"peer_id": peer_id, "owner_id": str(uuid.uuid4()),
              "signing_key_b64url": wire.b64u(seed),
              "public_key_b64url": wire.b64u(wire.public_key_of(seed)),
              "kid": SERVER_KID, "jwt": "old.jwt.x", "refresh_token": "old-refresh",
              "base_url": "http://old.example", "turn_count": 3}
    fields.update(extra)
    fields = {k: v for k, v in fields.items() if v is not None}
    path = tmp_path / "old-state.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _register(handler, peer_id: str, seed: bytes, *, kid: str = SERVER_KID):
    """The server holds `seed`'s public key as an active key of `peer_id`, under `kid`."""
    public = P.b64u(P.public_key_of(seed))
    handler.signin.setdefault(peer_id, []).append(public)
    handler.listed.setdefault(peer_id, []).append(
        {"kid": kid, "public_key": public, "active": True,
         "created_at": "2026-01-01T00:00:00Z", "revoked_at": None})


def _adopt(base_url: str, new: Path, old: Path) -> int:
    return cli.main(["adopt", "--base-url", base_url, "--state-file", str(new),
                     "--from-state", str(old)])


def test_adopt_carries_the_key_and_takes_the_kid_from_the_server(tmp_path, server, capsys):
    """The new file holds the old seed, the server's kid, and a JWT from a sign-in THIS run
    made; the old client's JWT and refresh token are not carried, and the old file is byte for
    byte as it was. The adopted state then signs in through the ordinary `signin` path, so
    `adopted` is a status that names a peer."""
    base_url, handler = server
    seed, peer_id = wire.new_seed(), str(uuid.uuid4())
    _register(handler, peer_id, seed)
    old = _old_state(tmp_path, seed=seed, peer_id=peer_id)
    before = old.read_bytes()
    new = tmp_path / "new-state.json"

    assert _adopt(base_url, new, old) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == node.STATUS_ADOPTED and out["kid"] == SERVER_KID
    state = json.loads(new.read_text(encoding="utf-8"))
    assert state["schema"] == node.STATE_SCHEMA and state["base_url"] == base_url
    assert state["status"] == node.STATUS_ADOPTED
    assert state["peer_id"] == peer_id and state["kid"] == SERVER_KID
    assert P.b64u_decode(state["signing_seed_b64url"]) == seed
    assert state["jwt"] == f"jwt.{peer_id}", "the JWT is from this run's sign-in"
    assert "refresh_token" not in state and state["jwt"] != "old.jwt.x"
    assert state["memory_peer_id"] == node.DEFAULT_MEMORY_PEER_ID
    assert old.read_bytes() == before, "adopt changed the old client's state file"
    assert handler.seen == [("POST", "/v1/auth/challenge"), ("POST", "/v1/auth/sign-in"),
                            ("GET", "/v1/peers/me/keys")]

    handler.seen.clear()                             # the adopted state is a working state
    assert cli.main(["signin", "--base-url", base_url, "--state-file", str(new)]) == 0
    assert handler.seen == [("POST", "/v1/auth/challenge"), ("POST", "/v1/auth/sign-in")]


def test_a_key_the_server_does_not_hold_for_that_peer_writes_nothing(tmp_path, server):
    """Refused at sign-in: the peer exists, but with a DIFFERENT key. Control: the same run
    with the key registered (the test above)."""
    base_url, handler = server
    seed, peer_id = wire.new_seed(), str(uuid.uuid4())
    _register(handler, peer_id, wire.new_seed())
    old = _old_state(tmp_path, seed=seed, peer_id=peer_id)
    before = old.read_bytes()
    new = tmp_path / "new-state.json"
    assert _adopt(base_url, new, old) == 1
    assert not new.exists() and old.read_bytes() == before
    assert ("POST", "/v1/auth/sign-in") in handler.seen
    assert ("GET", "/v1/peers/me/keys") not in handler.seen


@pytest.mark.parametrize("old_kid, expect", [("stale-kid", 2), (None, 0), (SERVER_KID, 0)])
def test_the_kid_is_the_servers_and_a_disagreeing_old_kid_writes_nothing(
        tmp_path, server, old_kid, expect):
    """The kid signs every call envelope, so it must be the one the server holds. An old
    file that names another kid is refused after the proof, before any write; a file with no
    kid, or the same one, is the control."""
    base_url, handler = server
    seed, peer_id = wire.new_seed(), str(uuid.uuid4())
    _register(handler, peer_id, seed)
    old = _old_state(tmp_path, seed=seed, peer_id=peer_id, kid=old_kid)
    new = tmp_path / "new-state.json"
    assert _adopt(base_url, new, old) == expect
    assert new.exists() == (expect == 0)
    if expect == 0:
        assert json.loads(new.read_text(encoding="utf-8"))["kid"] == SERVER_KID


@pytest.mark.parametrize("keys, expect", [
    ([("revoked", False)], 2),                        # the key is listed, but revoked
    ([(SERVER_KID, True), ("twin", True)], 2),        # listed twice as active: ambiguous
    ([("other-old", False), (SERVER_KID, True)], 0),  # control: one active, one revoked
])
def test_exactly_one_active_server_key_equal_to_the_seed(tmp_path, server, keys, expect):
    """The sign-in proves the key is active; the key list must agree, with exactly one
    active entry for this public key and a kid. Anything else is for a human to read."""
    base_url, handler = server
    seed, peer_id = wire.new_seed(), str(uuid.uuid4())
    _register(handler, peer_id, seed)                # sign-in succeeds in every case
    public = P.b64u(P.public_key_of(seed))
    handler.listed[peer_id] = [{"kid": kid, "public_key": public, "active": active}
                               for kid, active in keys]
    old = _old_state(tmp_path, seed=seed, peer_id=peer_id, kid=None)
    new = tmp_path / "new-state.json"
    assert _adopt(base_url, new, old) == expect
    assert new.exists() == (expect == 0)
    assert ("GET", "/v1/peers/me/keys") in handler.seen


def test_adopt_never_overwrites_and_never_writes_the_old_file(tmp_path, server):
    """An existing new state file, or the old file named as the new one, is refused before
    any request. Control: a fresh path adopts (the first test)."""
    base_url, handler = server
    seed, peer_id = wire.new_seed(), str(uuid.uuid4())
    _register(handler, peer_id, seed)
    old = _old_state(tmp_path, seed=seed, peer_id=peer_id)
    before = old.read_bytes()
    existing = tmp_path / "existing.json"
    existing.write_text("{}", encoding="utf-8")
    assert _adopt(base_url, existing, old) == 2
    assert existing.read_text(encoding="utf-8") == "{}"
    assert _adopt(base_url, old, old) == 2
    assert old.read_bytes() == before
    assert handler.seen == [], "a refused adopt reached the server"


@pytest.mark.parametrize("change", [
    {"public_key_b64url": "WRONG"},                   # names a key that is not the seed's
    {"signing_key_b64url": wire.b64u(b"\x01" * 64)},  # 64 bytes: not a 32-byte seed
    {"signing_key_b64url": None},
    {"peer_id": "not-a-uuid"},
    {"schema": node.STATE_SCHEMA},                    # already this client's file
])
def test_an_old_file_that_is_wrong_about_itself_is_refused_before_any_request(
        tmp_path, server, change):
    base_url, handler = server
    seed, peer_id = wire.new_seed(), str(uuid.uuid4())
    _register(handler, peer_id, seed)
    if change.get("public_key_b64url") == "WRONG":
        change = {"public_key_b64url": wire.b64u(wire.public_key_of(wire.new_seed()))}
    old = _old_state(tmp_path, **{"seed": seed, "peer_id": peer_id, **change})
    before = old.read_bytes()
    new = tmp_path / "new-state.json"
    assert _adopt(base_url, new, old) == 2
    assert not new.exists() and old.read_bytes() == before
    assert handler.seen == []


def test_a_byte_order_mark_is_read_and_the_file_left_alone(tmp_path, server):
    """Windows editors write a BOM; the old client's files on HQB and HQD were written by
    several tools. Control for the BOM: the same content without one (the first test)."""
    base_url, handler = server
    seed, peer_id = wire.new_seed(), str(uuid.uuid4())
    _register(handler, peer_id, seed)
    old = _old_state(tmp_path, seed=seed, peer_id=peer_id)
    old.write_bytes(b"\xef\xbb\xbf" + old.read_bytes())
    before = old.read_bytes()
    new = tmp_path / "new-state.json"
    assert _adopt(base_url, new, old) == 0
    assert old.read_bytes() == before
