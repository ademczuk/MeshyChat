"""tools/meshy_node: what the node client refuses, and the bytes it signs.

The client's real proof is a journey against a live server (two nodes minted from pairing
codes, a socket, the embed job); that run is recorded outside the test suite because it
needs HQB's GPU. These tests pin what a journey CANNOT tell apart:

  * a client that would have talked to the wrong server (no `--base-url`, a state file
    minted elsewhere, an environment proxy) behaves exactly like a correct one on the day
    the right server is the only one listening;
  * a client that sends a non-v1 field is caught by the server's validator only after the
    field was signed and sent;
  * a signature scheme that is wrong in the same way on both sides round-trips.

So each check is measured against a DIFFERENT instrument from the one that produced it
(docs/V1-DESIGN-RULES.md C5): the server's own `apps.crypto.primitives` and
`apps.memoryskills.validation`, and a DPoP known answer made by a separate implementation.
Every refusal sits beside
the control that differs from it in the one variable under test (C1).

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node.py -q -p no:cacheprovider
"""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.authapi.config import SIGNIN_PURPOSE  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import validation  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from tools.meshy_node import cli, node, wire  # noqa: E402

#: A DPoP known answer computed by the server's primitive, not by this client: the same
#: constant `tests/test_crypto_parity.py` pins for the server. seed `bytes([7] * 32)`, POST
#: /v1/x, jwt "h.p.s", ts 1_000_000_000, jti `bytes([0x11] * 16)`.
KAT_DPOP = ("AARQT1NUAAUvdjEveAAAAAA7msoAMAomZLy6qQuerO_7t-QPAZosybBQt7yNFI1M18KN9dkRERERERERERERE"
            "RERERERL6PfumYqeRVOONEmgaoGEgLBgXLlh1NgJFOhITRGJzTbrCFeNuM4kXysoVuLbZORGx_BKRqz-hmmXZ_oDdtrAA")

COMMANDS = [
    ("mint", ["--code", "c"]),
    ("keygen", ["--public-key-out", "k.pub"]),
    ("bind", ["--peer-id", "00000000-0000-0000-0000-000000000001"]),
    ("adopt", ["--from-state", "old-state.json"]),
    ("pair", []),
    ("invite", []),
    ("claim", ["--namespace", "shared/mesh-chat"]),
    ("grant", ["--namespace", "shared/mesh-chat", "--grantee",
               "00000000-0000-0000-0000-000000000001", "--permission", "write"]),
    ("signin", []),
    ("send", ["--to", "b", "--thread", "t", "--body", "x"]),
    ("inbox", []),
    ("search", ["--q", "x"]),
    ("context", ["--key", "msg/1-x"]),
    ("neighbours", ["--key", "msg/1-x"]),
    ("summarize", ["--thread", "t"]),
    ("listen", []),
    ("read", ["--key", "msg/1-x"]),
    ("fetch", ["--key", "msg/1-x"]),
    ("close-thread", ["--thread", "t"]),
]


# --------------------------------------------------------------------------- a recording server

class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers the inbox route like the server does and records every request it sees."""
    seen: list = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.command, self.path, self.headers.get("Authorization")))
        body = json.dumps({"label": "desktop-abcdef12", "label_source": "peers.name",
                           "labels": [], "hits": [], "truncated": False,
                           "coverage": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        type(self).seen.append((self.command, self.path, json.loads(self.rfile.read(length))))
        body = json.dumps({"error": "E_PAIRING_INVALID",
                           "message": "pairing code not found"}).encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _start_recorder(stack):
    seen: list = []
    handler = type("Recorder", (_Recorder,), {"seen": seen})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.append(server)
    return f"http://127.0.0.1:{server.server_address[1]}", seen


@pytest.fixture()
def recorders():
    """A factory of live recording servers, each with its own request log."""
    stack: list = []
    try:
        yield lambda: _start_recorder(stack)
    finally:
        for server in stack:
            server.shutdown()
            server.server_close()


@pytest.fixture()
def recorder(recorders):
    return recorders()


def _dead_port() -> int:
    """A port nothing listens on: bound, read, released."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _state_file(tmp_path: Path, base_url: str) -> Path:
    """A minted-looking state file whose JWT is not near expiry, so no sign-in is attempted."""
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


# =============================================================== 1. it cannot reach a server by accident

def test_every_subcommand_requires_an_explicit_base_url(monkeypatch):
    """No default and no environment fallback. The environment carries every spelling a
    fleet tool reads a server URL from, so a fallback would have something to find."""
    for name in ("MESHY_BASE_URL", "BASE_URL", "AGENTMESHY_BASE_URL"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    parser = cli.build_parser()
    for cmd, extra in COMMANDS:
        with pytest.raises(SystemExit) as refused:
            parser.parse_args([cmd, "--state-file", "s.json", *extra])
        assert refused.value.code == 2, f"{cmd} ran without --base-url"
        # Control: the same command with the flag parses, so the refusal was the flag.
        args = parser.parse_args([cmd, "--base-url", "http://127.0.0.1:1",
                                  "--state-file", "s.json", *extra])
        assert args.base_url == "http://127.0.0.1:1"
    for bad in ("", "   ", "ftp://127.0.0.1", "127.0.0.1:8000", "http://u:p@127.0.0.1",
                "http://127.0.0.1/?x=1"):
        with pytest.raises(node.Refused):
            node.normalize_base_url(bad)
    assert node.normalize_base_url(" http://127.0.0.1:8000/ ") == "http://127.0.0.1:8000"


def test_a_key_minted_against_one_server_is_never_used_against_another(tmp_path, recorders):
    """The state file names the server that minted its key. A command pointed at another,
    LIVE, server is refused before a socket opens: that server sees nothing. The control is
    the same command and file pointed at the minting server (a trailing slash is the same
    server), which does see the request."""
    minted_on, seen = recorders()
    elsewhere, seen_elsewhere = recorders()
    path = _state_file(tmp_path, minted_on)

    assert cli.main(["inbox", "--base-url", elsewhere, "--state-file", str(path)]) == 2
    assert seen_elsewhere == [], "the refused command reached the other server"
    assert seen == []

    assert cli.main(["inbox", "--base-url", minted_on + "/", "--state-file", str(path)]) == 0
    assert [(m, p.split("?")[0]) for m, p, _ in seen] == [("GET", "/v1/mesh/inbox")]
    assert seen[0][2] == "Bearer h.p.s"


def test_an_environment_proxy_is_never_used(tmp_path, recorder, monkeypatch):
    """urllib's default opener sends a request to whatever HTTP_PROXY names. With the proxy
    pointed at a dead port, only a client that ignores it reaches the server."""
    base_url, seen = recorder
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    dead = f"http://127.0.0.1:{_dead_port()}"
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, dead)
    status, body = node.http_json("GET", base_url, "/v1/mesh/inbox")
    assert status == 200 and body["label"] == "desktop-abcdef12"
    assert len(seen) == 1, "the request did not arrive directly"


# =============================================================== 2. the message is v1 and only v1

def test_the_field_sets_are_the_servers():
    assert wire.V1_REQUIRED == validation.MESH_MESSAGE_V1_REQUIRED_FIELDS
    assert wire.V1_OPTIONAL == validation.MESH_MESSAGE_V1_OPTIONAL_FIELDS
    assert wire.MESH_NAMESPACE == validation.MESH_CHAT_NAMESPACE


def test_a_built_message_passes_the_servers_validator_and_an_extra_field_is_refused_first():
    full = wire.mesh_message(to=["desktop-b"], sender="desktop-a", msg_type="request",
                             thread_id="journey/one", body="hello", author_family="claude",
                             cc=["desktop-c"], in_reply_to="msg/1700000000000-x",
                             requires_ack=True, subject="pr/1", ref="abc123",
                             session_label="desktop-a#2", done_when="post the receipt",
                             closes=["msg/1700000000001-y"],
                             declines=["msg/1700000000002-z"],
                             attachments=[{"file_id": "00000000-0000-4000-8000-000000000001",
                                           "name": "serial.log", "size_bytes": 3, "sha256": "0" * 64}],
                             operator_words=["go"],
                             directive={"v": 1, "words": ["go"], "target": "desktop-b",
                                        "target_peer": "6b22cef7-ca52-5546-91ed-b4b3f3fcf516",
                                        "expires_at": "2026-10-26T17:50:00Z"},
                             window_end="2026-10-26T17:20:00Z")
    # Every optional field but the urgent lane's: urgency never rides a directive and goes to ONE seat, so this message
    # cannot carry it (tests/test_urgent_lane.py builds urgent messages and checks the shared field list).
    assert set(full) == wire.V1_REQUIRED | (wire.V1_OPTIONAL - {"urgent", "urgent_declined"})
    key = wire.mesh_key("desktop-a")
    validation.validate_mesh_key_ts(key)
    validation.validate_mesh_message_v1(validation.MESH_CHAT_NAMESPACE, key, full)

    minimal = wire.mesh_message(to=["desktop-b"], sender="desktop-a", msg_type="note",
                                thread_id="t", body="x", author_family="claude")
    assert set(minimal) == wire.V1_REQUIRED, "an unset optional must be OMITTED, not null"
    validation.validate_mesh_message_v1(validation.MESH_CHAT_NAMESPACE, key, minimal)

    # The refusal: one field outside v1. The client refuses it before signing; the server's
    # validator refuses the same value, so the client is not stricter than the contract.
    extra = {**minimal, "tags": ["x"]}
    with pytest.raises(wire.NotV1, match="tags"):
        wire.assert_v1(extra)
    with pytest.raises(HandlerError) as server_said:
        validation.validate_mesh_message_v1(validation.MESH_CHAT_NAMESPACE, key, extra)
    assert server_said.value.code == "E_MESH_SCHEMA"
    with pytest.raises(wire.NotV1, match="schema_version"):
        wire.assert_v1({**minimal, "schema_version": True})


# =============================================================== 3. the signatures, by another instrument

def test_dpop_matches_the_known_answer_and_the_servers_verifier():
    assert wire.dpop_proof(bytes([7] * 32), "POST", "/v1/x", "h.p.s", timestamp=1_000_000_000,
                           jti=bytes([0x11] * 16)) == KAT_DPOP
    seed = wire.new_seed()
    proof = wire.dpop_proof(seed, "WS_UPGRADE", "/v1/ws", "a.b.c")
    public = P.public_key_of(seed)
    ok, why = P.verify_dpop(proof, public, method="WS_UPGRADE", target="/v1/ws", jwt="a.b.c")
    assert ok, why
    # Control: the server's verifier refuses the same proof for another path.
    assert not P.verify_dpop(proof, public, method="WS_UPGRADE", target="/v1/x",
                             jwt="a.b.c")[0]


def test_the_envelope_and_the_sign_in_verify_under_the_servers_primitives():
    seed = wire.new_seed()
    public = P.public_key_of(seed)
    env = wire.call_envelope(seed=seed, kid="desktop-k", caller_peer_id=str(uuid.uuid4()),
                             target_peer_id=node.DEFAULT_MEMORY_PEER_ID, skill="memory.write",
                             args={"namespace": "shared/mesh-chat", "key": "msg/1-x",
                                   "value": {"a": 1}})
    assert set(env) == {"payload", "signature", "signing_kid"}
    # mem_client.build_envelope's field set (mem_client.py:562-577 at b578b5c3a).
    assert set(env["payload"]) == {"v", "kind", "call_id", "caller_peer_id", "target_peer_id",
                                   "skill_name", "skill_version", "args", "args_attachments",
                                   "issued_at", "nonce"}
    assert P.verify_strict(public, P.jcs(env["payload"]), P.b64u_decode(env["signature"]))
    tampered = {**env["payload"], "args": {**env["payload"]["args"], "key": "msg/2-x"}}
    assert not P.verify_strict(public, P.jcs(tampered), P.b64u_decode(env["signature"]))

    peer_id, nonce = str(uuid.uuid4()), "n0nce"
    # The canonical bytes apps/authapi/views.sign_in verifies.
    server_bytes = P.jcs({"aud": "peer", "nonce": nonce, "peer_id": peer_id,
                          "purpose": SIGNIN_PURPOSE})
    assert wire.signin_message(peer_id, nonce) == server_bytes
    assert P.verify_strict(public, server_bytes,
                           wire.sign(seed, wire.signin_message(peer_id, nonce)))


def test_a_result_is_accepted_only_when_the_jwks_key_signed_exactly_that_status():
    server_seed = wire.new_seed()
    x = wire.b64u(wire.public_key_of(server_seed))
    jwks = {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": x, "kid": "k1", "alg": "EdDSA"}]}
    payload = {"v": 1, "kind": "skill.result", "call_id": "c1", "target_peer_id": "t",
               "status": "completed", "result": {"version": 1}, "issued_at": "now",
               "nonce": "n"}
    call = {"call_id": "c1", "status": "completed", "result": {"version": 1},
            "result_payload": payload, "result_signing_kid": "k1",
            "result_signature": wire.b64u(wire.sign(server_seed, wire.jcs(payload)))}
    assert wire.result_signature_ok(call, jwks) == (True, "ok")
    # A valid signature beside a row claiming another status (#922, rule E2).
    ok, why = wire.result_signature_ok({**call, "status": "failed"}, jwks)
    assert not ok and "status" in why
    # A kid the JWKS does not publish, and a signature by another key.
    assert not wire.result_signature_ok({**call, "result_signing_kid": "k2"}, jwks)[0]
    other = wire.b64u(wire.sign(wire.new_seed(), wire.jcs(payload)))
    assert not wire.result_signature_ok({**call, "result_signature": other}, jwks)[0]
    assert not wire.result_signature_ok({**call, "result_signature": None}, jwks)[0]


def test_a_mint_refuses_to_overwrite_a_key(tmp_path, recorder):
    """An existing state file holds a node's private key: `mint` refuses it and sends
    nothing. Control: a fresh path does reach the server, with ONLY the public key in the
    body, and a refused redeem (404 here) leaves no half-written key behind."""
    base_url, seen = recorder
    path = _state_file(tmp_path, base_url)
    before = path.read_bytes()
    assert cli.main(["mint", "--base-url", base_url, "--state-file", str(path),
                     "--code", "abc"]) == 2
    assert path.read_bytes() == before and seen == []

    fresh = tmp_path / "fresh.json"
    assert cli.main(["mint", "--base-url", base_url, "--state-file", str(fresh),
                     "--code", "abc"]) == 1
    assert [(m, p) for m, p, _ in seen] == [("POST", "/v1/onboard/peers")]
    sent = seen[0][2]
    assert set(sent) == {"onetime_code", "public_key", "device_label"}
    assert len(wire.b64u_decode(sent["public_key"])) == 32
    assert not fresh.exists(), "a refused redeem must not leave a pending key file"


# =============================================================== 4. the operator's key (divergence 025)

def _keygen(tmp_path: Path, base_url: str, name: str = "operator") -> tuple[Path, Path]:
    state, pub = tmp_path / f"{name}.json", tmp_path / f"{name}.pub"
    assert cli.main(["keygen", "--base-url", base_url, "--state-file", str(state),
                     "--public-key-out", str(pub)]) == 0
    return state, pub


def test_keygen_writes_the_key_bootstrap_owner_reads_and_never_overwrites_one(tmp_path):
    """keygen sends nothing and writes two files: the private state and the public half, in
    the one-line base64url form `manage.py bootstrap_owner --public-key-file` parses. Checked
    with the SERVER's parser and kid rule (a different instrument, rule C5): the key the
    server will register is this state's key, and the kid bind derives is the kid
    bootstrap_owner stores. An existing state file, or an existing public-key file, is
    refused and nothing is written; the control is the same command with fresh paths."""
    from apps.authapi import bootstrap
    base_url = f"http://127.0.0.1:{_dead_port()}"          # nothing listens: nothing is sent
    state_path, pub = _keygen(tmp_path, base_url)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == node.STATUS_KEY_ONLY and "peer_id" not in state
    raw = bootstrap.read_public_key(pub.read_bytes())
    assert raw == wire.b64u_decode(state["public_key_b64url"])
    assert raw == wire.public_key_of(wire.b64u_decode(state["signing_seed_b64url"]))
    assert wire.peer_kid(raw) == P.kid_for_peer_key(raw)
    public_text = pub.read_text(encoding="ascii")
    assert "PRIVATE" not in public_text and state["signing_seed_b64url"] not in public_text

    before = (state_path.read_bytes(), pub.read_bytes())
    assert cli.main(["keygen", "--base-url", base_url, "--state-file", str(state_path),
                     "--public-key-out", str(tmp_path / "other.pub")]) == 2
    other_state = tmp_path / "other.json"
    assert cli.main(["keygen", "--base-url", base_url, "--state-file", str(other_state),
                     "--public-key-out", str(pub)]) == 2
    assert (state_path.read_bytes(), pub.read_bytes()) == before
    assert not other_state.exists() and not (tmp_path / "other.pub").exists(), (
        "a refused keygen wrote a file")


def test_a_key_file_that_appears_after_the_check_is_never_replaced(tmp_path, recorder,
                                                                  monkeypatch):
    """keygen and mint check that their state file is absent, generate a key, then write
    it. A file that appears in between (a second keygen or mint racing for the same path)
    holds that other command's private key: replacing it loses the key while the other
    command reports success. The racing file is planted inside the window, at the seed
    call. Each command is refused, the planted file is unchanged, no public half is
    written and nothing is sent. Control, one variable apart: the same window with nothing
    planted writes the key (keygen) and reaches the server (mint)."""
    base_url, seen = recorder
    racer = b'{"racer": "the other command holds this key"}\n'
    real_seed, plant = wire.new_seed, {}

    def seed_inside_the_window():
        if plant.get("path") is not None:
            plant["path"].write_bytes(racer)
        return real_seed()

    monkeypatch.setattr(node.wire, "new_seed", seed_inside_the_window)

    state, pub = tmp_path / "raced.json", tmp_path / "raced.pub"
    plant["path"] = state
    assert cli.main(["keygen", "--base-url", base_url, "--state-file", str(state),
                     "--public-key-out", str(pub)]) == 2, "keygen raced and reported success"
    assert state.read_bytes() == racer, "keygen replaced a key file that appeared after its check"
    assert not pub.exists(), "a refused keygen wrote a public half"

    minted = tmp_path / "node-raced.json"
    plant["path"] = minted
    assert cli.main(["mint", "--base-url", base_url, "--state-file", str(minted),
                     "--code", "abc"]) == 2, "mint raced and went on"
    assert minted.read_bytes() == racer, "mint replaced a key file that appeared after its check"
    assert seen == [], "a refused mint sent its key"

    plant.clear()                                   # control: nothing appears in the window
    ok_state, ok_pub = tmp_path / "ok.json", tmp_path / "ok.pub"
    assert cli.main(["keygen", "--base-url", base_url, "--state-file", str(ok_state),
                     "--public-key-out", str(ok_pub)]) == 0
    assert json.loads(ok_state.read_text(encoding="utf-8"))["status"] == node.STATUS_KEY_ONLY
    assert ok_pub.exists()
    assert cli.main(["mint", "--base-url", base_url, "--state-file",
                     str(tmp_path / "node-ok.json"), "--code", "abc"]) == 1
    assert [(m, p) for m, p, _ in seen] == [("POST", "/v1/onboard/peers")], "control: sent"


def test_a_key_that_names_no_peer_signs_nothing_and_a_refused_bind_writes_nothing(
        tmp_path, recorder):
    """A `key-only` state holds a private key and no peer. Every command that signs or
    authenticates refuses it BEFORE a socket opens (the live recorder sees nothing). The
    control is `bind` against the same server, which does reach it (the challenge), is
    refused there (404), and leaves the state file byte for byte as it was: a bind is
    written only after the server has accepted the key as that peer's."""
    base_url, seen = recorder
    state_path, _ = _keygen(tmp_path, base_url)
    before = state_path.read_bytes()
    common = ["--base-url", base_url, "--state-file", str(state_path)]
    for cmd, extra in [("signin", []), ("pair", []), ("invite", []), ("inbox", []),
                       ("search", ["--q", "x"]),
                       ("context", ["--key", "msg/1-x"]), ("listen", ["--count", "1"]),
                       ("send", ["--to", "b", "--thread", "t", "--body", "x"]),
                       ("claim", ["--namespace", "shared/mesh-chat"]),
                       ("grant", ["--namespace", "shared/mesh-chat", "--grantee",
                                  str(uuid.uuid4()), "--permission", "write"])]:
        assert cli.main([cmd, *common, *extra]) == 2, cmd
    assert seen == [], f"a key-only state reached the server: {seen}"

    assert cli.main(["bind", *common, "--peer-id", str(uuid.uuid4())]) == 1
    assert [(m, p) for m, p, _ in seen] == [("POST", "/v1/auth/challenge")]
    assert state_path.read_bytes() == before, "a refused bind wrote the state file"


def test_bind_refuses_a_state_that_already_names_a_peer(tmp_path, recorder):
    """A state file names one peer. Re-binding a minted node's key to another peer id is
    refused before anything is sent; the file is unchanged."""
    base_url, seen = recorder
    path = _state_file(tmp_path, base_url)
    before = path.read_bytes()
    assert cli.main(["bind", "--base-url", base_url, "--state-file", str(path),
                     "--peer-id", str(uuid.uuid4())]) == 2
    assert seen == [] and path.read_bytes() == before


def test_the_owner_sign_in_bytes_are_the_servers_and_not_a_node_sign_in():
    """`pair` signs the OWNER sign-in payload. Pinned against the server's canonical bytes
    (apps/authapi/views.owner_sign_in: aud "dashboard", its own purpose), and shown distinct
    from a node sign-in over the same nonce, so neither signature replays as the other."""
    from apps.authapi.config import OWNER_SIGNIN_PURPOSE
    peer_id, nonce = str(uuid.uuid4()), "n0nce-owner"
    server_bytes = P.jcs({"aud": "dashboard", "nonce": nonce, "peer_id": peer_id,
                          "purpose": OWNER_SIGNIN_PURPOSE})
    assert wire.owner_signin_message(peer_id, nonce) == server_bytes
    assert wire.owner_signin_message(peer_id, nonce) != wire.signin_message(peer_id, nonce)
    seed = wire.new_seed()
    assert P.verify_strict(P.public_key_of(seed), server_bytes,
                           wire.sign(seed, wire.owner_signin_message(peer_id, nonce)))


def test_a_signed_call_counts_as_done_only_when_its_signature_verified():
    """`claim`, `grant` and `send` exit 0 on ONE rule. A completed result whose signature did
    not verify is not success; neither is an ok:false result or a failed call."""
    done = {"status": "completed", "result": {"op": "namespace.claim"},
            "result_signature_verified": True}
    assert node.succeeded(done)
    assert not node.succeeded({**done, "result_signature_verified": False})
    assert not node.succeeded({**done, "result": {"ok": False, "error_code": "E_X"}})
    assert not node.succeeded({**done, "status": "failed"})
    assert not node.succeeded({**done, "result": None})


def test_grant_takes_exactly_one_of_permission_and_revoke_and_a_peer_uuid(tmp_path, recorder):
    base_url, seen = recorder
    path = _state_file(tmp_path, base_url)
    common = ["grant", "--base-url", base_url, "--state-file", str(path),
              "--namespace", "shared/mesh-chat"]
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(common + ["--grantee", str(uuid.uuid4())])
    with pytest.raises(SystemExit):
        parser.parse_args(common + ["--grantee", str(uuid.uuid4()), "--permission", "write",
                                    "--revoke"])
    assert cli.main(common + ["--grantee", "desktop-b", "--permission", "write"]) == 2
    assert seen == []
    # Control: a well-formed grant reaches the server (which refuses it here: 404).
    assert cli.main(common + ["--grantee", str(uuid.uuid4()), "--permission", "write"]) == 1
    assert [p.split("/")[-1] for _, p, _ in seen] == ["invoke"]


def test_a_revoke_that_revoked_nothing_is_not_success(tmp_path, recorder, monkeypatch,
                                                      capsys):
    """memory.grant with revoke=true COMPLETES, signed, with `revoked: 0` when no active
    grant matched (a mistyped grantee, the wrong namespace). For the command an operator
    runs to cut a node off, that is not success: it exits 1. Control, one variable apart:
    `revoked: 1` exits 0, and so does a permission grant (it has no `revoked` field)."""
    base_url, _seen = recorder
    path = _state_file(tmp_path, base_url)
    answer = {}

    def signed(_path, _state, *, skill, args, poll_timeout=40.0):
        return {"call_id": "c", "invoke": {"http": 202}, "status": "completed",
                "result": {"op": skill, **answer}, "error_code": None,
                "error_message": None, "result_signing_kid": "k",
                "result_signature_verified": True, "result_signature_detail": "ok"}

    monkeypatch.setattr(node, "invoke", signed)
    common = ["grant", "--base-url", base_url, "--state-file", str(path),
              "--namespace", "shared/mesh-chat", "--grantee", str(uuid.uuid4())]
    answer.update(revoked=0)
    assert cli.main(common + ["--revoke"]) == 1, "a revoke that revoked nothing exited 0"
    answer.update(revoked=True)
    assert cli.main(common + ["--revoke"]) == 1, "a boolean is not a count of revoked grants"
    answer.update(revoked=1)
    assert cli.main(common + ["--revoke"]) == 0, "control: a revoke that took effect"
    answer.clear()
    assert cli.main(common + ["--revoke"]) == 1, "a revoke result with no count"
    assert cli.main(common + ["--permission", "write"]) == 0, "control: a grant"
    capsys.readouterr()


# =============================================================== 5. reranking is on unless the caller turns it off (divergence 026)

def test_search_and_context_rerank_unless_told_not_to(tmp_path, recorder, capsys):
    """The server reranks by default (divergence 026, as amended: on a live paraphrase test
    the reranker put the target first in 10/12 queries against 5/12). The client follows,
    sending rerank=1 unless `--no-rerank`, and `--rerank`, the old spelling, still means
    the default. Control: `--no-rerank` sends 0."""
    from urllib.parse import parse_qs, urlsplit
    base_url, seen = recorder
    path = _state_file(tmp_path, base_url)
    for cmd, extra in (("search", ["--q", "x"]), ("context", ["--key", "msg/1-x"])):
        sent = {}
        for flags in ((), ("--no-rerank",), ("--rerank",)):
            del seen[:]
            assert cli.main([cmd, "--base-url", base_url, "--state-file", str(path),
                             *extra, *flags]) == 0
            (_, url, _), = seen
            sent[flags] = parse_qs(urlsplit(url).query)["rerank"]
        assert sent == {(): ["1"], ("--no-rerank",): ["0"], ("--rerank",): ["1"]}, (cmd, sent)
    capsys.readouterr()


def test_neighbours_asks_one_hop_and_the_inferred_edges_only_when_told(tmp_path, recorder,
                                                                       capsys):
    """`neighbours` is GET /v1/mesh/neighbours (divergence 032): one hop from a message, its
    declared neighbours, plus the persisted related_to edges the 5090's reranker wrote when
    `--include-inferred` is given. It sends include_inferred=0 by default and `limit` only
    when asked, so the server's own default and bounds (1..100) apply. It carries no body,
    so no DPoP proof is needed, as for search."""
    from urllib.parse import parse_qs, urlsplit
    base_url, seen = recorder
    path = _state_file(tmp_path, base_url)
    sent = {}
    for flags in ((), ("--include-inferred",), ("--include-inferred", "--limit", "7")):
        del seen[:]
        assert cli.main(["neighbours", "--base-url", base_url, "--state-file", str(path),
                         "--key", "msg/1-x", *flags]) == 0
        (method, url, _), = seen
        parts = urlsplit(url)
        assert (method, parts.path) == ("GET", "/v1/mesh/neighbours"), (method, url)
        sent[flags] = parse_qs(parts.query)
    assert sent[()] == {"key": ["msg/1-x"], "include_inferred": ["0"]}, sent
    assert sent[("--include-inferred",)] == {"key": ["msg/1-x"], "include_inferred": ["1"]}
    assert sent[("--include-inferred", "--limit", "7")] == {
        "key": ["msg/1-x"], "include_inferred": ["1"], "limit": ["7"]}
    capsys.readouterr()


@pytest.mark.parametrize("value, via", [(None, None), ("notice", ["notice"]), ("seat", None), ("NOTICE", None)])
def test_neighbours_sends_via_notice_only_when_the_caller_set_the_variable_to_notice(
        tmp_path, recorder, capsys, monkeypatch, value, via):
    """graphline's lookup is the notice's, not the seat's: it sets MESHYCHAT_VIA=notice and the client sends
    via=notice, which the server counts as neighbours_notice (divergence 072). Unset, or any other value,
    sends nothing, so a seat's own call is counted as its own. CONTROL: the unset case is the plain request.
    MUTANT: drop `params.update(_via())` from node.neighbours; the notice case sends no via."""
    from urllib.parse import parse_qs, urlsplit
    if value is None:
        monkeypatch.delenv(node.VIA_ENV, raising=False)
    else:
        monkeypatch.setenv(node.VIA_ENV, value)
    base_url, seen = recorder
    path = _state_file(tmp_path, base_url)
    assert cli.main(["neighbours", "--base-url", base_url, "--state-file", str(path), "--key", "msg/1-x"]) == 0
    (_, url, _), = seen
    assert parse_qs(urlsplit(url).query).get("via") == via
    capsys.readouterr()


# =============================================================== 6. reading a message: read, listen --read, inbox --bodies
#
# THE FINDING THIS SECTION ANSWERS (2026-09-16, live, two nodes): a node could not read a
# message it was sent. The frame is a pointer (key, from_peer_id, to, created_at), an inbox
# hit is an edge with no body, and the client had no way to call the memory.read skill the
# server implements. These tests pin the CLIENT half against fake servers, one variable at
# a time; tests/test_meshy_node_read.py runs the same commands against the real server.

READ_KEY = "msg/1789394600000-desktop-a-0a0b0c"
#: A mesh message v1 as memory.write stores it. Synthetic text only.
STORED = {"schema_version": 1, "to": ["desktop-b"], "from": "desktop-a", "type": "request",
          "thread_id": "ops/lease", "ts": "2026-09-16T11:50:00Z",
          "body": "synthetic: renew the lease inside the job loop",
          "author_family": "claude", "in_reply_to": "msg/1789394599999-desktop-b-010203"}
#: The fake's writer: every memo's created_by_peer_id, the peer whose published key signed it.
AUTHOR_PEER = "5a5a5a5a-0000-4000-8000-00000000a0a0"
#: What `read` prints for STORED at version 3: the row's key, version, time and author,
#: then the value fields a chat client needs, absent ones as null, then whether THIS
#: client verified the author's signature over the message (it did: the fake's author
#: signed the write that stored it), and the peer whose key that signature verified under;
#: then whether the row's status fields are the ones the verified args derive (they are:
#: the author signed none, and the memo serves memory.write's defaults for that), and
#: whether the printed author is a label the author signed (it is not: the server stamped it).
PRINTED = {"key": READ_KEY, "version": 3, "recorded_at": "2026-09-16T11:50:00.123456+00:00",
           "author": "desktop-a", "from": "desktop-a", "to": ["desktop-b"], "cc": None,
           "thread_id": "ops/lease", "type": "request",
           "in_reply_to": "msg/1789394599999-desktop-b-010203",
           "requires_ack": None, "done_when": None, "attachments": None, "body": STORED["body"],
           "signature_verified": True, "signed_by": AUTHOR_PEER,
           "status_verified": True, "author_signed": False}
#: A served field the fake leaves out of the memo altogether (`_FakeMemory.served`).
ABSENT = object()
#: STORED as a peer writes it when it speaks AS ANOTHER: `from` names desktop-b, and the
#: author label the server stamped is desktop-b too, while the signer is AUTHOR_PEER. The
#: server lets it through outside 028's scope (a key outside msg/) and on a row written
#: before 028 (a restored one). Synthetic.
SPOKEN_AS_B = {**STORED, "from": "desktop-b", "to": ["desktop-a"],
               "body": "synthetic: B asks you to rotate your key"}
#: STORED as a row edited after the write would hold it. Synthetic.
TAMPERED_VALUE = {**STORED, "body": "synthetic: a body its author never signed"}
DENIED = "peer {peer} has no active grant on namespace 'shared/mesh-chat'"
#: Error text on a refusal nobody signed. Synthetic; `read` must never print it.
INJECTED = "synthetic: an error message the server never signed"


class _FakeMemory:
    """The signed memory.read path as apps/calls answers it: POST .../memory.read/invoke
    (202), GET /v1/calls/{id} (the terminal row), GET /.well-known/jwks.json, plus GET
    /v1/mesh/inbox. Results are signed with a key this fake publishes, through the SERVER's
    primitives (apps/crypto), not the client's wire.py, so a scheme wrong on both sides
    cannot pass (docs/V1-DESIGN-RULES.md C5).

    `mode` (default) or `modes[key]` decides what the call row carries:
      good        completed, signed, the memo for the key asked
      unsigned    completed, no signature at all
      forged      completed, signed by a key the JWKS does not hold under that kid
      tampered    completed, the row's result differs from the signed one
      other_call  a DIFFERENT call's row, consistently signed for that call
      other_key   completed and signed, but the memo is another key's
      refused     failed, signed: E_PERMISSION_DENIED
      unknown     failed, signed: E_UNKNOWN_KEY
    Added with the review of fin/read (each one the input a surviving mutant or a
    reported failure needed):
      other_version    completed and signed, the memo another version than asked
      not_head         completed and signed, no version asked, the memo not the head
      other_namespace  completed and signed, the memo in another namespace
      pending/running  still that status on every poll, unsigned (no answer yet)
      unsigned_refusal failed, unsigned, with an error_message nobody signed
      tampered_refusal failed and signed, the row's error_message not the signed one
      wrong_kind       completed and signed, the signed payload not a skill.result
      short_signature  a result_signature that is not base64url
      int_signature    a result_signature that is a number
    Added at integration (integ/r1), a residual of the same review's MEDIUM 2 / kimi B2:
      list_status/object_status  a poll answer whose `status` is a JSON array / object
    `jwks_mode` "good" (default), "no_x" (the key has no public key) or "down" (500).
    GET /v1/mesh/context answers `context_answer` ({} unless a test sets it) and records
    its headers in `context_headers`; GET /v1/mesh/search answers `search_answer`.

    THE AUTHOR'S SIDE, for the client's `signature_verified`. Every memo's
    `created_by_peer_id` is `author_peer`, whose key (`author_seed`, kid `author_kid`)
    GET /agents/{peer}/jwks.json publishes. A read that asks `include_provenance` gets
    the write's envelope in `memo.provenance`, signed by that key through the SERVER's
    primitives. The server signs its answer in every mode below (the result signature
    verifies), so each one is a message the SERVER vouches for and the author may not.
    `author_mode` (default) or `author_modes[key]`:
      good              the stored value is what the author signed
      no_provenance     the memo's provenance is null
      value_tampered    the stored value differs from the signed one (the row was edited
                        after the write)
      payload_tampered  the signed payload was edited too, to match: the author's
                        signature no longer verifies
      forged_author     signed under the author's kid by another key
      other_signer      the envelope's caller is not the row's writer
      not_a_write       the envelope is a memory.read call
      other_message     the envelope wrote another key
      retyped           the signed `requires_ack` is true, the stored one 1: equal to
                        Python, a different JSON value
      from_tampered     the stored `from` differs from the signed one (review F4)
      to_tampered       the stored `to` differs from the signed one (review F4)
      not_a_call        the envelope names memory.write but its kind is not skill.call
                        (review F5: the skill-name check alone let it through)
      speaks_as_another the author signed SPOKEN_AS_B (`from` desktop-b) and the row's
                        author_label is desktop-b: every signature check passes, and the
                        printed author and `from` are not the signer
    `author_jwks_mode`: "good", "rotated" (the JWKS no longer holds the kid), "down" (500)
    or "malformed" (the key's x is a number). Each JWKS request is in `author_jwks_gets`.

    THE ROW'S STATUS FIELDS, for the client's `status_verified` (B02 phase 0). `signed_args`
    is merged into the args the author signs (a signed status, tags, author, ...), for
    every key; `served` (every key) and `served_for[key]` overwrite the memo's served
    fields after it is built, as a row edited in the store would serve them, and a value of
    ABSENT drops the field from the memo. Both empty: the author signs namespace, key and
    value only, and the memo serves the defaults memory.write stores for that.
    """

    def __init__(self):
        self.seed = os.urandom(32)
        self.kid = "srv-test"
        self.mode = "good"
        self.modes: dict[str, str] = {}
        self.jwks_mode = "good"
        self.author_seed = os.urandom(32)
        self.author_kid = "desktop-author"
        self.author_peer = AUTHOR_PEER
        self.author_mode = "good"
        self.author_modes: dict[str, str] = {}
        self.author_jwks_mode = "good"
        self.author_jwks_gets: list[str] = []
        self.signed_args: dict = {}
        self.served: dict = {}
        self.served_for: dict[str, dict] = {}
        self.context_answer: dict = {}
        self.search_answer: dict = {}
        self.context_headers: list[dict] = []
        self.inbox_hits: list[dict] = []
        self.invokes: list[dict] = []
        self.accept_writes = False
        self.gets: list[str] = []
        self._payloads: dict[str, dict] = {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, obj):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                fake.invokes.append({"path": self.path, "body": body,
                                     "dpop": self.headers.get("DPoP"),
                                     "authorization": self.headers.get("Authorization")})
                write = fake.accept_writes and self.path.endswith("/skills/memory.write/invoke")
                if not self.path.endswith("/skills/memory.read/invoke") and not write:
                    return self._send(404, {"error": "E_NOT_FOUND", "message": "not found"})
                fake._payloads[body["call_id"]] = body["envelope"]["payload"]
                return self._send(202, {"call_id": body["call_id"], "status": "pending"})

            def do_GET(self):  # noqa: N802
                route = self.path.split("?", 1)[0]
                fake.gets.append(route)
                if route == "/.well-known/jwks.json":
                    if fake.jwks_mode == "down":
                        return self._send(500, {"error": "E_INTERNAL",
                                                "message": "internal error"})
                    jwk = {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "kid": fake.kid,
                           "x": P.b64u(P.public_key_of(fake.seed))}
                    if fake.jwks_mode == "no_x":
                        del jwk["x"]
                    return self._send(200, {"keys": [jwk]})
                if route.startswith("/agents/") and route.endswith("/jwks.json"):
                    fake.author_jwks_gets.append(route)
                    return self._send(*fake.author_jwks(route.split("/")[2]))
                if route == "/v1/mesh/context":
                    fake.context_headers.append({k.lower(): v for k, v in self.headers.items()})
                    return self._send(200, fake.context_answer)
                if route == "/v1/mesh/search":
                    return self._send(200, fake.search_answer)
                if route == "/v1/mesh/inbox":
                    return self._send(200, fake.inbox_body())
                if route.startswith("/v1/calls/"):
                    return self._send(200, fake.call_row(route.rsplit("/", 1)[1]))
                return self._send(404, {"error": "E_NOT_FOUND", "message": "not found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def inbox_body(self) -> dict:
        return {"label": "desktop-b", "label_source": "peers.name",
                "labels": [{"label": "desktop-b", "source": "peers.name"}],
                "hits": self.inbox_hits, "truncated": False,
                "coverage": {"projected": len(self.inbox_hits), "total": len(self.inbox_hits)},
                "inferred": []}

    def read_args(self) -> list[dict]:
        return [i["body"]["envelope"]["payload"]["args"] for i in self.invokes]

    def author_jwks(self, peer: str) -> tuple[int, dict]:
        """GET /agents/{peer}/jwks.json as apps/peers/views.jwks answers it: the peer's
        active key only, `{"keys": []}` for a peer it does not know."""
        if self.author_jwks_mode == "down":
            return 500, {"error": "E_INTERNAL", "message": "internal error"}
        if peer != self.author_peer or self.author_jwks_mode == "rotated":
            keys = [] if peer != self.author_peer else [
                {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig",
                 "kid": "desktop-rotated", "x": P.b64u(P.public_key_of(os.urandom(32)))}]
            return 200, {"keys": keys}
        x = P.b64u(P.public_key_of(self.author_seed))
        return 200, {"keys": [{"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig",
                               "kid": self.author_kid,
                               "x": 7 if self.author_jwks_mode == "malformed" else x}]}

    def author_jwks_pair(self, peer: str) -> tuple[dict | None, str | None]:
        """author_jwks as node.author_keys answers it, for a direct wire call."""
        status, body = self.author_jwks(peer)
        return (body, None) if status == 200 else (None, f"answered {status}")

    def provenance(self, memo: dict, amode: str) -> dict | None:
        """memo.provenance as handle_write stores it (apps/memoryskills/handlers.py step 7):
        the write's verbatim payload, its signature and kid, and the key the server found."""
        if amode == "no_provenance":
            return None
        payload = {"v": 1, "kind": "skill.result" if amode == "not_a_call" else "skill.call",
                   "call_id": str(uuid.uuid4()),
                   "caller_peer_id": (str(uuid.uuid4()) if amode == "other_signer"
                                      else self.author_peer),
                   "target_peer_id": node.DEFAULT_MEMORY_PEER_ID,
                   "skill_name": "memory.read" if amode == "not_a_write" else "memory.write",
                   "skill_version": "0.2.0",
                   "args": {"namespace": memo["namespace"],
                            "key": memo["key"] + ("-x" if amode == "other_message" else ""),
                            "value": ({**STORED, "requires_ack": True}
                                      if amode == "retyped" else SPOKEN_AS_B
                                      if amode == "speaks_as_another" else STORED),
                            **self.signed_args},
                   "args_attachments": [], "issued_at": "2026-09-16T11:50:00Z",
                   "nonce": str(uuid.uuid4())}
        signer = os.urandom(32) if amode == "forged_author" else self.author_seed
        signature = P.b64u(P.sign(signer, P.jcs(payload)))
        if amode == "payload_tampered":
            payload = {**payload, "args": {**payload["args"], "value": TAMPERED_VALUE}}
        return {"payload": payload, "signature": signature, "signing_kid": self.author_kid,
                "caller_owner_id": str(uuid.uuid4()),
                "payload_jcs_sha256": P.sha256(P.jcs(payload)).hex(),
                "public_key_b64url": P.b64u(P.public_key_of(self.author_seed)),
                "public_key_sha256": "", "verified_at": "2026-09-16T11:50:00Z",
                "pre_discipline": False}

    def call_row(self, call_id: str) -> dict:
        payload = self._payloads[call_id]
        args = payload["args"]
        key = args["key"]
        mode = self.modes.get(key, self.mode)
        if mode in ("list_status", "object_status"):
            return {"call_id": call_id,
                    "status": ["completed"] if mode == "list_status" else {"s": "completed"},
                    "result": None, "result_payload": None, "result_signature": None,
                    "result_signing_kid": None, "error_code": None, "error_message": None}
        if mode in ("pending", "running"):
            return {"call_id": call_id, "status": mode, "result": None,
                    "result_payload": None, "result_signature": None,
                    "result_signing_kid": None, "error_code": None, "error_message": None}
        memo = {"id": str(uuid.uuid4()),
                "namespace": "shared/other" if mode == "other_namespace" else args["namespace"],
                "key": key + "-other" if mode == "other_key" else key,
                "version": args.get("version", 3) + (4 if mode == "other_version" else 0),
                "is_head": "version" not in args and mode != "not_head",
                "parent_id": None, "value": STORED, "status": None, "tags": [],
                "author_label": "desktop-a", "pre_discipline": False, "claimed_status": None,
                "created_by_peer_id": self.author_peer, "created_by_owner_id": str(uuid.uuid4()),
                "created_at": "2026-09-16 11:50:00.123456+00:00"}
        amode = self.author_modes.get(key, self.author_mode)
        if args.get("include_provenance"):                   # handlers.handle_read
            memo["provenance"] = self.provenance(memo, amode)
        if amode in ("value_tampered", "payload_tampered"):
            memo["value"] = TAMPERED_VALUE
        if amode == "retyped":
            memo["value"] = {**STORED, "requires_ack": 1}
        if amode == "speaks_as_another":
            memo["value"], memo["author_label"] = SPOKEN_AS_B, "desktop-b"
        if amode == "from_tampered":
            memo["value"] = {**STORED, "from": "desktop-c"}
        if amode == "to_tampered":
            memo["value"] = {**STORED, "to": ["desktop-c"]}
        for field, served in {**self.served, **self.served_for.get(key, {})}.items():
            if served is ABSENT:
                memo.pop(field, None)
            else:
                memo[field] = served
        code = message = None
        if mode in ("refused", "unsigned_refusal", "tampered_refusal"):
            code, message = "E_PERMISSION_DENIED", DENIED.format(peer=payload["caller_peer_id"])
        elif mode == "unknown":
            code, message = "E_UNKNOWN_KEY", f"no memo at shared/mesh-chat/{key}"
        if code:
            status, result = "failed", {"ok": False, "error_code": code, "error_message": message}
        else:
            op = payload["skill_name"]
            status, result = "completed", {"op": op, "memo": memo}
        answered = str(uuid.uuid4()) if mode == "other_call" else call_id
        signed = {"v": 1, "kind": "skill.call" if mode == "wrong_kind" else "skill.result",
                  "call_id": answered,
                  "target_peer_id": node.DEFAULT_MEMORY_PEER_ID, "status": status,
                  "result": result, "issued_at": "2026-09-16T11:50:01Z", "nonce": "n"}
        if code:
            signed.update(error_code=code, error_message=message)
        signer = os.urandom(32) if mode == "forged" else self.seed
        row_result = result
        if mode == "tampered":
            row_result = {"op": "memory.read", "memo": {
                **memo, "value": {**STORED, "body": "synthetic: a tampered body"}}}
        row = {"call_id": answered, "status": status, "result": row_result,
               "result_payload": signed, "result_signing_kid": self.kid,
               "result_signature": P.b64u(P.sign(signer, P.jcs(signed))),
               "error_code": code, "error_message": message}
        if mode in ("unsigned", "unsigned_refusal"):
            row.update(result_payload=None, result_signature=None, result_signing_kid=None)
        if mode in ("unsigned_refusal", "tampered_refusal"):
            # Only the row's top-level text: the signed `result` still matches, so at
            # c5457ac..09b08e0 the row verified and this text was printed.
            row["error_message"] = INJECTED
        if mode == "short_signature":
            row["result_signature"] = "A"
        if mode == "int_signature":
            row["result_signature"] = 123
        return row


@pytest.fixture()
def fake_memory():
    fake = _FakeMemory()
    try:
        yield fake
    finally:
        fake.close()


def _read(base_url: str, path: Path, *extra: str) -> int:
    return cli.main(["read", "--base-url", base_url, "--state-file", str(path),
                     "--key", READ_KEY, *extra])


def test_read_prints_the_message_from_a_signed_verified_memory_read(tmp_path, fake_memory,
                                                                     capsys):
    """`read` is memory.read through the SAME signed path `send` uses: a skill.call envelope
    signed by the node's key, a DPoP proof for exactly that POST, the call polled, and the
    result signature checked against the server's JWKS. It prints the message a chat client
    needs. With --version the version is asked for as an integer and printed back."""
    path = _state_file(tmp_path, fake_memory.base_url)
    assert _read(fake_memory.base_url, path) == 0
    assert json.loads(capsys.readouterr().out) == PRINTED

    (inv,) = fake_memory.invokes
    assert inv["path"] == f"/v1/agents/{node.DEFAULT_MEMORY_PEER_ID}/skills/memory.read/invoke"
    env = inv["body"]["envelope"]
    assert env["payload"]["skill_name"] == "memory.read"
    assert env["payload"]["args"] == {"namespace": "shared/mesh-chat", "key": READ_KEY,
                                      "include_provenance": True}
    assert inv["body"]["call_id"] == env["payload"]["call_id"], "the signed call_id is echoed"
    state = json.loads(path.read_text(encoding="utf-8"))
    public = P.b64u_decode(state["public_key_b64url"])
    assert P.verify_strict(public, P.jcs(env["payload"]), P.b64u_decode(env["signature"]))
    assert inv["authorization"] == "Bearer h.p.s"
    ok, why = P.verify_dpop(inv["dpop"], public, method="POST", target=inv["path"],
                            jwt="h.p.s")
    assert ok, why
    assert "/.well-known/jwks.json" in fake_memory.gets, "the signature was not checked"

    assert _read(fake_memory.base_url, path, "--version", "2") == 0
    assert json.loads(capsys.readouterr().out) == {**PRINTED, "version": 2}
    assert fake_memory.read_args()[-1] == {"namespace": "shared/mesh-chat", "key": READ_KEY,
                                           "version": 2, "include_provenance": True}


def test_read_verifies_the_authors_signature_against_the_authors_published_key(
        tmp_path, fake_memory, capsys):
    """`signature_verified: true` means THIS client, in THIS call, checked the author's
    Ed25519 signature over the envelope that wrote the message (memory.read's provenance,
    asked for with include_provenance), with the key the AUTHOR publishes at GET
    /agents/{created_by_peer_id}/jwks.json under the envelope's kid, and found that the
    envelope wrote exactly this namespace, key and value as that peer. It is not the
    server's result signature, which `read` already required before printing anything.
    Pinned against a different instrument: the fake's author signs with the server's
    primitives, and the check below re-verifies with them."""
    path = _state_file(tmp_path, fake_memory.base_url)
    assert _read(fake_memory.base_url, path) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["signature_verified"] is True and "signature_error" not in got, got
    assert got["signed_by"] == fake_memory.author_peer, got
    assert fake_memory.author_jwks_gets == [f"/agents/{fake_memory.author_peer}/jwks.json"]
    assert list(got)[-4:] == ["signature_verified", "signed_by", "status_verified",
                              "author_signed"], "printed after the message's fields"


def test_signature_verified_names_the_signer_and_says_nothing_about_author_or_from(
        tmp_path, fake_memory, capsys):
    """REVIEW F1 (fin/r1-small, HIGH, reproduced on the real server). `signature_verified:
    true` proves the value is what the ROW'S WRITER signed (created_by_peer_id, whose
    published key verified it). It does not prove that `author` or `from` is that peer:
    those are labels, the client cannot map a label to a peer, and the server binds them
    to the signer only for a msg/ key written since 028. Here the signer wrote a value
    whose `from` is desktop-b under the author label desktop-b, and every check passes.
    So the output must NAME the signer (`signed_by`), which is not the peer the labels
    name; the printed author and `from` stay as stored. Control: the same read of a
    message whose labels are the signer's own prints the same `signed_by`."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.author_mode = "speaks_as_another"
    assert _read(fake_memory.base_url, path) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["signature_verified"] is True and "signature_error" not in got, got
    assert (got["author"], got["from"], got["body"]) == (
        "desktop-b", "desktop-b", SPOKEN_AS_B["body"]), got
    assert got["signed_by"] == AUTHOR_PEER, got

    fake_memory.author_mode = "good"
    assert _read(fake_memory.base_url, path) == 0
    assert json.loads(capsys.readouterr().out) == PRINTED, "control: its own labels"


#: author_mode, author_jwks_mode, the signature_error code a read must report.
AUTHOR_FAILURES = [
    ("no_provenance", "good", "E_SIGNATURE_NO_ENVELOPE"),
    ("value_tampered", "good", "E_SIGNATURE_NOT_THE_MESSAGE"),
    ("payload_tampered", "good", "E_SIGNATURE_INVALID"),
    ("forged_author", "good", "E_SIGNATURE_INVALID"),
    ("other_signer", "good", "E_SIGNATURE_NOT_THE_MESSAGE"),
    ("not_a_write", "good", "E_SIGNATURE_NOT_THE_MESSAGE"),
    ("other_message", "good", "E_SIGNATURE_NOT_THE_MESSAGE"),
    ("retyped", "good", "E_SIGNATURE_NOT_THE_MESSAGE"),
    ("from_tampered", "good", "E_SIGNATURE_NOT_THE_MESSAGE"),
    ("to_tampered", "good", "E_SIGNATURE_NOT_THE_MESSAGE"),
    ("not_a_call", "good", "E_SIGNATURE_NOT_THE_MESSAGE"),
    ("good", "rotated", "E_SIGNATURE_KEY_UNAVAILABLE"),
    ("good", "down", "E_SIGNATURE_KEY_UNAVAILABLE"),
    ("good", "malformed", "E_SIGNATURE_KEY_UNAVAILABLE"),
]


@pytest.mark.parametrize("author_mode,jwks_mode,code", AUTHOR_FAILURES,
                         ids=[f"{a}-{j}" for a, j, _ in AUTHOR_FAILURES])
def test_a_message_the_client_could_not_verify_reads_false_with_the_reason(
        tmp_path, fake_memory, capsys, author_mode, jwks_mode, code):
    """Every answer here is SIGNED BY THE SERVER, so `read` prints it and exits 0: the
    server did say this. What the client could not establish is that the AUTHOR said it,
    and the message says so: `signature_verified: false` and a `signature_error` whose
    code names why. A tampered value is printed as served and flagged, never passed off
    as the author's. Control, one variable apart: the same read with the author's side
    good is `true` and prints PRINTED exactly."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.author_mode, fake_memory.author_jwks_mode = author_mode, jwks_mode
    assert _read(fake_memory.base_url, path) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["signature_verified"] is False, got
    assert got["signed_by"] is None, "nothing verified, so no signer is named"
    assert got["signature_error"].startswith(code + ": "), got["signature_error"]
    # B02 phase 0: nothing verified, so no status was compared, and the reason is the
    # signature's own code, never a status contradiction (design test 18).
    assert got["status_verified"] is False and got["author_signed"] is False, got
    assert got["status_error"].startswith(code + ": "), got["status_error"]
    expected_body = (TAMPERED_VALUE if author_mode in ("value_tampered", "payload_tampered")
                     else STORED)["body"]
    assert got["body"] == expected_body, got
    assert list(got)[-6:] == ["signature_verified", "signed_by", "signature_error",
                              "status_verified", "status_error", "author_signed"], list(got)

    fake_memory.author_mode, fake_memory.author_jwks_mode = "good", "good"
    assert _read(fake_memory.base_url, path) == 0
    assert json.loads(capsys.readouterr().out) == PRINTED, "control: the author's side good"


@pytest.mark.parametrize("fault", ["raises", "not_a_pair"])
def test_keys_that_could_not_be_fetched_are_unavailable_not_an_invalid_signature(
        fake_memory, fault):
    """Review F5 of fin/r1-small. When fetching the writer's published keys itself fails
    (the fetch raises, or answers something that is not a (jwks, why) pair), nothing was
    verified because no key was found: E_SIGNATURE_KEY_UNAVAILABLE, naming the fault. It
    was E_SIGNATURE_INVALID, which says the signature was checked and failed. Control: the
    same memo with a fetch that answers the writer's keys verifies."""
    memo = {"namespace": "shared/mesh-chat", "key": READ_KEY, "value": STORED,
            "created_by_peer_id": AUTHOR_PEER}
    memo["provenance"] = fake_memory.provenance(memo, "good")

    def broken(peer):
        if fault == "raises":
            raise RuntimeError("synthetic: the key fetch blew up")
        return "not a pair"

    ok, why = wire.author_signature_ok(memo, broken)
    assert ok is False and why.startswith("E_SIGNATURE_KEY_UNAVAILABLE: "), why
    assert AUTHOR_PEER in why, why
    assert wire.author_signature_ok(memo, fake_memory.author_jwks_pair) == (True, None),         "control: the writer's keys"


def test_a_row_an_edit_wrote_verifies_as_a_memory_edit_call_and_the_pairing_is_strict(fake_memory):
    """Divergence 059. The editor signs the WHOLE new value as a `memory.edit` request and the server stores that envelope as the new version's,
    so a read of an edited row would say `signature_verified: false` ("not a memory.write call") if the client insisted on memory.write. The row
    says it is an edit (`edited_at`, from the server's edit record), and then its envelope must be memory.edit; a row that says nothing of an
    edit must carry memory.write. The two are PAIRED: a memory.edit envelope on a row that claims no edit, and a memory.write envelope on a row
    that claims one, are not the message. CONTROL: the ordinary row verifies."""
    memo = {"namespace": "shared/mesh-chat", "key": READ_KEY, "value": STORED, "created_by_peer_id": AUTHOR_PEER}
    memo["provenance"] = fake_memory.provenance(memo, "good")
    assert wire.author_signature_ok(memo, fake_memory.author_jwks_pair) == (True, None), "control: an ordinary row"

    prov = fake_memory.provenance(memo, "good")
    payload = {**prov["payload"], "skill_name": "memory.edit"}
    edit_prov = {**prov, "payload": payload, "signature": P.b64u(P.sign(fake_memory.author_seed, P.jcs(payload)))}
    edited = {**memo, "edited_at": "2026-09-23T07:50:00Z", "edited_by": "peer-1a2b3c4d", "provenance": edit_prov}
    assert wire.author_signature_ok(edited, fake_memory.author_jwks_pair) == (True, None), "an edited row's own envelope"

    ok, why = wire.author_signature_ok({**memo, "provenance": edit_prov}, fake_memory.author_jwks_pair)
    assert ok is False and why.startswith(wire.SIG_NOT_THE_MESSAGE) and "memory.write" in why, why
    ok, why = wire.author_signature_ok({**memo, "edited_at": "2026-09-23T07:50:00Z"}, fake_memory.author_jwks_pair)
    assert ok is False and why.startswith(wire.SIG_NOT_THE_MESSAGE) and "memory.edit" in why, why


def test_inbox_bodies_says_per_hit_whether_the_authors_signature_verified(
        tmp_path, fake_memory, capsys):
    """inbox --bodies reads each hit through `read`, so each attached message carries its
    own verdict: a hit whose stored value was edited after the write reads false beside
    hits that read true."""
    fake_memory.inbox_hits = [dict(h) for h in HITS]
    fake_memory.author_modes[HITS[1]["key"]] = "value_tampered"
    path = _state_file(tmp_path, fake_memory.base_url)
    assert cli.main(["inbox", "--base-url", fake_memory.base_url, "--state-file", str(path),
                     "--bodies"]) == 0
    hits = json.loads(capsys.readouterr().out)["hits"]
    assert [h["read"]["signature_verified"] for h in hits] == [True, False, True], hits
    assert [h["read"]["signed_by"] for h in hits] == [AUTHOR_PEER, None, AUTHOR_PEER], hits
    assert hits[1]["read"]["signature_error"].startswith("E_SIGNATURE_NOT_THE_MESSAGE: ")
    assert "signature_error" not in hits[0]["read"] and "signature_error" not in hits[2]["read"]


def test_context_and_search_bodies_say_the_client_verified_no_signature(
        tmp_path, fake_memory, capsys):
    """`context` and `search` show bodies from routes that carry no signed envelope, so
    the client cannot check anything: every object that shows a body says
    `signature_verified: false` and why, pointing at `read`. An object with no body (a
    withheld anchor, a thread hit) is left as the server sent it."""
    anchor_key, related_key = "msg/1789394600001-desktop-a-000001", READ_KEY
    fake_memory.context_answer = {
        "anchor": {"key": anchor_key, "version": 1, "body": "synthetic: the anchor",
                   "body_truncated": False, "body_withheld": None},
        "thread": {"hits": [{"key": "msg/1789394600002-desktop-b-000002", "version": 1}]},
        "related": {"hits": [{"key": related_key, "version": 3, "body": "synthetic: related",
                              "body_truncated": False}], "absent": None}}
    fake_memory.search_answer = {"hits": [{"key": related_key, "version": 3,
                                           "body": "synthetic: hit", "body_truncated": False}]}
    path = _state_file(tmp_path, fake_memory.base_url)
    common = ["--base-url", fake_memory.base_url, "--state-file", str(path)]
    assert cli.main(["context", *common, "--key", anchor_key]) == 0
    ctx = json.loads(capsys.readouterr().out)
    assert cli.main(["search", *common, "--q", "lease"]) == 0
    found = json.loads(capsys.readouterr().out)
    for shown in (ctx["anchor"], ctx["related"]["hits"][0], found["hits"][0]):
        assert shown["signature_verified"] is False, shown
        assert shown["signature_error"].startswith("E_SIGNATURE_NOT_CARRIED: "), shown
        assert f"read --key {shown['key']}" in shown["signature_error"], shown
    assert "signature_verified" not in ctx["thread"]["hits"][0], "no body, nothing claimed"
    assert fake_memory.author_jwks_gets == [], "nothing was verified, so nothing fetched"

    fake_memory.context_answer["anchor"].update(body=None, body_truncated=None,
                                                body_withheld="not_authorized")
    assert cli.main(["context", *common, "--key", anchor_key]) == 0
    withheld = json.loads(capsys.readouterr().out)["anchor"]
    assert "signature_verified" not in withheld, withheld

    # An EMPTY body is still a body shown (review F5: a truthiness test skipped it).
    fake_memory.search_answer = {"hits": [{"key": related_key, "version": 3, "body": "",
                                           "body_truncated": False}]}
    assert cli.main(["search", *common, "--q", "lease"]) == 0
    (empty,) = json.loads(capsys.readouterr().out)["hits"]
    assert empty["signature_verified"] is False, empty
    assert empty["signature_error"].startswith("E_SIGNATURE_NOT_CARRIED: "), empty


@pytest.mark.parametrize("mode", ["unsigned", "forged", "tampered", "other_call"])
def test_an_unverified_result_is_a_failure_and_prints_no_message(tmp_path, fake_memory,
                                                                 capsys, mode):
    """A result the server's key did not sign, or signed for another call, is a FAILURE
    (exit 1) and none of its content is printed: a caller that ignores the exit code must
    not be handed unverified text shaped like a message. Control: the same read, signed."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.mode = mode
    assert _read(fake_memory.base_url, path) == 1, f"{mode} was accepted"
    out = capsys.readouterr().out
    got = json.loads(out)
    assert got["error"] == "E_RESULT_UNVERIFIED" and got["key"] == READ_KEY, got
    assert got["result_signature_verified"] is False
    assert STORED["body"] not in out and "tampered body" not in out, out

    fake_memory.mode = "good"
    assert _read(fake_memory.base_url, path) == 0, "control: the signed read"
    assert json.loads(capsys.readouterr().out) == PRINTED


def test_a_signed_answer_about_another_key_is_not_the_message(tmp_path, fake_memory, capsys):
    """A verified result whose memo is not the key (or version) asked for is refused: the
    signature proves the server said it, not that it answers THIS read."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.mode = "other_key"
    assert _read(fake_memory.base_url, path) == 1
    out = capsys.readouterr().out
    assert json.loads(out)["error"] == "E_RESULT_MISMATCH" and STORED["body"] not in out


@pytest.mark.parametrize("mode,code,message", [
    ("refused", "E_PERMISSION_DENIED", "peer {peer} has no active grant on namespace "
                                       "'shared/mesh-chat'"),
    ("unknown", "E_UNKNOWN_KEY", "no memo at shared/mesh-chat/" + READ_KEY)])
def test_a_refused_read_exits_1_with_the_servers_error_verbatim(tmp_path, fake_memory, capsys,
                                                                mode, code, message):
    path = _state_file(tmp_path, fake_memory.base_url)
    peer = json.loads(path.read_text(encoding="utf-8"))["peer_id"]
    fake_memory.mode = mode
    assert _read(fake_memory.base_url, path) == 1
    got = json.loads(capsys.readouterr().out)
    assert got["error"] == "E_READ_REFUSED" and got["status"] == "failed", got
    assert got["error_code"] == code
    assert got["error_message"] == message.format(peer=peer)
    assert got["key"] == READ_KEY and "body" not in got


def test_an_http_refusal_prints_the_servers_body_and_a_bad_version_sends_nothing(
        tmp_path, recorder, capsys):
    """The recorder answers every POST 404 with its own error body: `read` exits 1 and
    prints that body as the server sent it. A non-positive --version is refused (exit 2)
    before anything is signed or sent."""
    base_url, seen = recorder
    path = _state_file(tmp_path, base_url)
    for bad in ("0", "-1"):
        assert _read(base_url, path, "--version", bad) == 2
    assert seen == [], "a refused version reached the server"
    capsys.readouterr()
    assert _read(base_url, path) == 1
    assert json.loads(capsys.readouterr().out) == {
        "http_status": 404, "code": "E_HTTP", "retryable": False,
        "body": {"error": "E_PAIRING_INVALID",
                                     "message": "pairing code not found"}}


# ---------------------------------------------------------------- listen --read

class _Frozen(datetime):
    """`datetime` with a fixed `now`, so listen's `received_at` can be pinned to the byte."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 16, 11, 50, 0, 123000, tzinfo=timezone.utc)


@contextlib.contextmanager
def _ws_server(frames: list[dict], *, close_code: int | None = None,
               accepted: list | None = None, refuse_status: int | None = None):
    """A WebSocket server on /v1/ws that accepts `meshy.v1`, sends `frames` in order and
    then waits for the client to close. Its base URL is the node's --base-url.

    `close_code`: after the frames, the SERVER closes the socket with that code (a real
    close frame on the wire), as apps/wsgw/consumer.py does when a token expires.
    `accepted`: a list each accepted connection is appended to, so a test can prove a
    command opened no socket.
    `refuse_status`: the upgrade is answered with this HTTP status and no socket opens,
    as the ASGI server answers a consumer that closes before accepting."""
    from websockets.sync.server import serve

    def refuse(connection, request):
        if refuse_status is None:
            return None
        return connection.respond(refuse_status, "refused by the test server")

    def handler(ws):
        if accepted is not None:
            accepted.append(ws.request.path)
        for frame in frames:
            ws.send(json.dumps(frame))
        if close_code is not None:
            ws.close(close_code, "closed by the test server")
            return
        try:
            for _ in ws:
                pass
        except Exception:  # noqa: BLE001 - the client closing is the end of the test
            pass

    server = serve(handler, "127.0.0.1", 0, subprotocols=["meshy.v1"], compression=None,
                   process_request=refuse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.socket.getsockname()[1]}"
    finally:
        server.shutdown()


def _mesh(key: str) -> dict:
    return {"type": "mesh_message", "key": key, "from_peer_id": "11111111-2222-3333-4444-"
            "555555555555", "to": "desktop-b", "created_at": "2026-09-16T11:50:00Z"}


LISTEN_FRAMES = [{"type": "welcome"}, _mesh("msg/1-a-000001"), {"type": "presence"},
                 _mesh("msg/2-a-000002")]


def test_listen_without_read_is_byte_for_byte_what_it_was(tmp_path, monkeypatch, capsys):
    """Without --read, listen is unchanged TO THE BYTE: the same stdout lines, the same
    stderr lines, and no read attempted. Expected bytes are written out by hand from the
    pre-change client (c5457ac), not computed by the code under test."""
    monkeypatch.setattr(node, "datetime", _Frozen)
    reads: list = []
    monkeypatch.setattr(node, "read", lambda *a, **k: reads.append(k), raising=False)
    with _ws_server(LISTEN_FRAMES) as base_url:
        path = _state_file(tmp_path, base_url)
        assert cli.main(["listen", "--base-url", base_url, "--state-file", str(path),
                         "--count", "2", "--duration", "30"]) == 0
    cap = capsys.readouterr()
    frame = ('{"type": "mesh_message", "key": "msg/%s-a-00000%s", "from_peer_id": '
             '"11111111-2222-3333-4444-555555555555", "to": "desktop-b", '
             '"created_at": "2026-09-16T11:50:00Z"}')
    stamp = '"received_at": "2026-09-16T11:50:00.123+00:00"'
    assert cap.out == (f'{{{stamp}, "frame": {frame % (1, 1)}}}\n'
                       f'{{{stamp}, "frame": {frame % (2, 2)}}}\n')
    assert cap.err == ('{"listen": "connected", "subprotocol": "meshy.v1"}\n'
                       f'{{{stamp}, "frame": {{"type": "welcome"}}}}\n'
                       f'{{{stamp}, "frame": {{"type": "presence"}}}}\n'
                       '{"listen": "done", "mesh_message_frames": 2}\n')
    assert reads == [], "listen read a message it was not asked to"


@pytest.mark.parametrize("value", ["0", "-1"])
@pytest.mark.parametrize("mode", [[], ["--monitor"]], ids=["plain", "monitor"])
def test_listen_count_below_one_is_refused_at_parse_time(tmp_path, capsys, value, mode):
    """`--count 0` had no defined meaning: plain listen opened the socket, waited for
    nothing and exited 0 having printed nothing a caller could tell from a quiet mesh. It
    is refused when the arguments are parsed, as argparse refuses any bad value (exit 2,
    the option named), and no socket is opened. Control, one variable apart: `--count 1`
    against the same server connects and prints its one frame."""
    accepted: list = []
    with _ws_server(LISTEN_FRAMES, accepted=accepted) as base_url:
        path = _state_file(tmp_path, base_url)
        common = ["listen", *mode, "--base-url", base_url, "--state-file", str(path)]
        with pytest.raises(SystemExit) as refused:
            cli.main([*common, "--count", value, "--duration", "10"])
        assert refused.value.code == 2, f"--count {value} was accepted"
        err = capsys.readouterr().err
        assert "--count" in err and "at least 1" in err, err
        assert accepted == [], f"--count {value} opened a socket"

        if not mode:
            assert cli.main([*common, "--count", "1", "--duration", "10"]) == 0
            out = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
            assert [o["frame"]["key"] for o in out] == ["msg/1-a-000001"], out
            assert accepted == ["/v1/ws"], "control: --count 1 connects"
    assert cli.build_parser().parse_args(
        [*common, "--count", "1"]).count == 1, "control: 1 parses as the integer 1"


@pytest.mark.parametrize("bounded", [False, True], ids=["unbounded", "duration"])
def test_plain_listen_closed_with_4401_exits_nonzero_naming_the_code(tmp_path, capsys,
                                                                    bounded):
    """The server's gateway closes an open socket with 4401 when the token expires
    (apps/wsgw/consumer.py, CLOSE_CREDENTIAL_EXPIRED) and refuses a token with the same
    code. Plain listen then printed `{"listen": "closed", "code": 4401}`, then `done`, and
    exited 0: a caller checking the exit code saw a listen that had finished its job. It
    exits 1, and its last stderr line names the code and what to do. The frame that
    arrived before the close is still printed. A real close frame, from a real socket.

    Controls, one variable apart (the close code): 1000, a normal close, and 4403, which
    tests/test_meshy_node_monitor.py pins as exit 0 for plain listen against the real
    server, both still end `done` with exit 0. Plain listen does not reconnect; the
    monitor is the reconnecting mode."""
    from apps.wsgw.constants import CLOSE_CREDENTIAL_EXPIRED, CLOSE_UNAUTHORIZED
    assert CLOSE_CREDENTIAL_EXPIRED == CLOSE_UNAUTHORIZED == 4401, "the server's code moved"

    bound = ["--duration", "30"] if bounded else []
    frames = [{"type": "welcome"}, _mesh("msg/1-a-000001")]
    with _ws_server(frames, close_code=4401) as base_url:
        path = _state_file(tmp_path, base_url)
        code = cli.main(["listen", "--base-url", base_url, "--state-file", str(path), *bound])
    cap = capsys.readouterr()
    assert code == 1, (code, cap.err)
    assert [json.loads(ln)["frame"]["key"] for ln in cap.out.splitlines()] == [
        "msg/1-a-000001"]
    err = [json.loads(ln) for ln in cap.err.splitlines()]
    assert {"listen": "closed", "code": 4401} in err, err
    last = err[-1]
    assert last["listen"] == "refused" and last["code"] == 4401, last
    assert last["mesh_message_frames"] == 1, last
    assert "4401" in last["detail"] and "signin" in last["detail"], last
    assert "--monitor" in last["detail"], last
    assert not any(e.get("listen") == "done" for e in err), err

    for control in (1000, 4403):
        with _ws_server(frames, close_code=control) as base_url:
            path = _state_file(tmp_path, base_url)     # rewritten for this server
            code = cli.main(["listen", "--base-url", base_url, "--state-file", str(path),
                             *bound])
        cap = capsys.readouterr()
        err = [json.loads(ln) for ln in cap.err.splitlines()]
        assert code == 0, (control, cap.err)
        assert {"listen": "closed", "code": control} in err, err
        assert err[-1] == {"listen": "done", "mesh_message_frames": 1}, err


@pytest.mark.parametrize("status", [403, 401])
def test_plain_listen_refused_at_the_upgrade_exits_1_with_a_line_naming_the_status(
        tmp_path, capsys, status):
    """Review F6 of fin/r1-small. A token already expired or refused when the socket is
    OPENED never reaches 4401 on an open socket: the gateway closes before accepting
    (apps/wsgw/consumer.py), and the ASGI server answers the upgrade with HTTP 403. A gateway
    that refuses it differently answers 401, so the test covers both. Plain listen then died on
    websockets' InvalidStatus, a
    traceback and no line naming it. It exits 1 with its last stderr line naming the HTTP
    status and what to do, nothing on stdout, and every stderr line is JSON. A real HTTP
    refusal of a real upgrade. Control, one variable apart: the same server accepting
    prints its frame and exits 0."""
    frames = [{"type": "welcome"}, _mesh("msg/1-a-000001")]
    accepted: list = []
    with _ws_server(frames, refuse_status=status, accepted=accepted) as base_url:
        path = _state_file(tmp_path, base_url)
        code = cli.main(["listen", "--base-url", base_url, "--state-file", str(path),
                         "--duration", "10"])
    cap = capsys.readouterr()
    assert code == 1, (code, cap.err)
    assert cap.out == "" and accepted == [], (cap.out, accepted)
    err = [json.loads(ln) for ln in cap.err.splitlines()]
    last = err[-1]
    assert last["listen"] == "refused" and last["http_status"] == status, last
    assert last["code"] is None and last["mesh_message_frames"] == 0, last
    assert str(status) in last["detail"] and "signin" in last["detail"], last
    assert not any(e.get("listen") in ("done", "connected") for e in err), err

    with _ws_server(frames, close_code=1000) as base_url:
        path = _state_file(tmp_path, base_url)
        code = cli.main(["listen", "--base-url", base_url, "--state-file", str(path),
                         "--duration", "10"])
    cap = capsys.readouterr()
    assert code == 0 and json.loads(cap.out)["frame"]["key"] == "msg/1-a-000001", cap


def test_listen_read_pairs_each_frame_with_its_message_and_survives_a_failed_read(
        tmp_path, monkeypatch, capsys):
    """With --read, each mesh_message frame is followed by that message fetched through
    `read`, on ONE line holding both. A read that fails for any reason (refused by the
    server, an HTTP error, the server unreachable, a frame with no key) is a typed
    `read_error` on that frame's line, and listening CONTINUES to the next frame."""
    got_message = {**PRINTED, "key": "msg/1-a-000001"}
    refused = {"error": "E_READ_REFUSED", "key": "msg/2-a-000002", "version": None,
               "status": "failed", "error_code": "E_UNKNOWN_KEY",
               "error_message": "no memo at shared/mesh-chat/msg/2-a-000002"}
    asked: list = []

    def fake_read(_path, _state, *, key, version=None, **_kw):
        asked.append((key, version))
        if key.startswith("msg/1-"):
            return got_message
        if key.startswith("msg/2-"):
            raise node.ReadFailed(refused)
        if key.startswith("msg/3-"):
            raise node.ServerSaid(503, {"error": "E_UNAVAILABLE", "message": "busy"})
        raise ConnectionRefusedError("refused by the test")

    monkeypatch.setattr(node, "read", fake_read)
    frames = [{"type": "welcome"}, _mesh("msg/1-a-000001"), _mesh("msg/2-a-000002"),
              {"type": "mesh_message", "from_peer_id": "x"}, _mesh("msg/3-a-000003"),
              _mesh("msg/4-a-000004")]
    with _ws_server(frames) as base_url:
        path = _state_file(tmp_path, base_url)
        assert cli.main(["listen", "--base-url", base_url, "--state-file", str(path),
                         "--count", "5", "--duration", "30", "--read"]) == 0
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [ln["frame"].get("key") for ln in lines] == [
        "msg/1-a-000001", "msg/2-a-000002", None, "msg/3-a-000003", "msg/4-a-000004"]
    assert asked == [("msg/1-a-000001", None), ("msg/2-a-000002", None),
                     ("msg/3-a-000003", None), ("msg/4-a-000004", None)]
    assert lines[0]["read"] == got_message and "read_error" not in lines[0]
    assert lines[1]["read_error"] == {**refused, "code": "E_READ_REFUSED",
                                       "retryable": False} and "read" not in lines[1]
    assert lines[2]["read_error"]["error"] == "E_FRAME_HAS_NO_KEY"
    assert lines[3]["read_error"] == {"error": "E_READ_HTTP", "key": "msg/3-a-000003",
                                      "version": None, "http_status": 503,
                                      "body": {"error": "E_UNAVAILABLE", "message": "busy"},
                                      "code": "E_READ_HTTP", "retryable": True}
    assert lines[4]["read_error"]["error"] == "E_UNREACHABLE"
    assert all(set(ln) == {"received_at", "frame", "read" if "read" in ln else "read_error"}
               for ln in lines), lines


# ---------------------------------------------------------------- inbox --bodies

HITS = [{"key": f"msg/{n}-desktop-a-00000{n}", "version": v, "edge_type": "addressed_to",
         "dst": "desktop-b", "src_field": "to", "recorded_at": "2026-09-16T11:50:00+00:00",
         "author": "desktop-a"} for n, v in ((1, 1), (2, 1), (3, 2))]


def test_inbox_without_bodies_prints_the_servers_answer_unchanged(tmp_path, fake_memory,
                                                                  capsys):
    """Without --bodies, `inbox` prints exactly what the server answered and reads nothing,
    even when there are hits it could have read."""
    fake_memory.inbox_hits = [dict(h) for h in HITS]
    path = _state_file(tmp_path, fake_memory.base_url)
    assert cli.main(["inbox", "--base-url", fake_memory.base_url,
                     "--state-file", str(path)]) == 0
    assert capsys.readouterr().out == json.dumps(fake_memory.inbox_body(), indent=2) + "\n"
    assert fake_memory.invokes == [], "inbox read a message it was not asked to"


def test_inbox_bodies_attaches_each_hits_message_or_a_typed_error(tmp_path, fake_memory,
                                                                  capsys):
    """--bodies reads each hit at the version the hit names and attaches the message, or a
    typed error: a refused read and an unverified one each fail only their own hit. Every
    other field of the answer is the server's, unchanged."""
    fake_memory.inbox_hits = [dict(h) for h in HITS]
    fake_memory.modes = {HITS[1]["key"]: "unknown", HITS[2]["key"]: "unsigned"}
    path = _state_file(tmp_path, fake_memory.base_url)
    assert cli.main(["inbox", "--base-url", fake_memory.base_url, "--state-file", str(path),
                     "--bodies"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {k: v for k, v in out.items() if k != "hits"} == {
        k: v for k, v in fake_memory.inbox_body().items() if k != "hits"}
    assert [{k: v for k, v in h.items() if k not in ("read", "read_error")}
            for h in out["hits"]] == HITS
    first, second, third = out["hits"]
    assert first["read"] == {**PRINTED, "key": HITS[0]["key"], "version": 1}
    assert second["read_error"]["error"] == "E_READ_REFUSED"
    assert second["read_error"]["error_code"] == "E_UNKNOWN_KEY" and "read" not in second
    assert third["read_error"]["error"] == "E_RESULT_UNVERIFIED" and "read" not in third
    assert [a.get("version") for a in fake_memory.read_args()] == [1, 1, 2]
    assert [a["key"] for a in fake_memory.read_args()] == [h["key"] for h in HITS]


# ---------------------------------------------------------------- the review of fin/read
#
# Each test below answers one finding of the two reviews of c5457ac..09b08e0, and names the
# mutant it kills. Every input is synthetic.

@pytest.mark.parametrize("mode", ["unsigned_refusal", "tampered_refusal"])
def test_a_refusal_that_did_not_verify_prints_none_of_its_text(tmp_path, fake_memory, capsys,
                                                               mode):
    """README "8. Chat": a result whose signature does not verify prints none of its
    content, and a refusal is a result. An UNSIGNED failed row, and a signed one whose
    row carries an error_message the server did not sign, are E_RESULT_UNVERIFIED (exit 1)
    and the unsigned text appears nowhere. Control, one variable apart: the same refusal,
    signed as sent, prints the server's code and message verbatim.
    Kills: the refusal branch run before the signature check (unsigned_refusal), and
    error_message dropped from the signed fields the row must match (tampered_refusal)."""
    path = _state_file(tmp_path, fake_memory.base_url)
    peer = json.loads(path.read_text(encoding="utf-8"))["peer_id"]
    fake_memory.mode = mode
    assert _read(fake_memory.base_url, path) == 1
    out = capsys.readouterr().out
    got = json.loads(out)
    assert got["error"] == "E_RESULT_UNVERIFIED", got
    assert got["result_signature_verified"] is False and got["status"] == "failed"
    assert INJECTED not in out and "error_message" not in got, out

    fake_memory.mode = "refused"
    assert _read(fake_memory.base_url, path) == 1
    signed = json.loads(capsys.readouterr().out)
    assert signed["error"] == "E_READ_REFUSED" and signed["result_signature_verified"] is True
    assert signed["error_message"] == DENIED.format(peer=peer), "control: the signed refusal"


def test_a_signed_payload_that_is_not_a_skill_result_is_not_an_answer(tmp_path, fake_memory,
                                                                      capsys):
    """The server's key signing a payload whose `kind` is not skill.result is not a
    result, however well the rest matches (the kind is the domain separator,
    apps/calls/envelope.py). Control: the same row with kind skill.result reads.
    Kills: the kind check removed from wire.result_signature_ok."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.mode = "wrong_kind"
    assert _read(fake_memory.base_url, path) == 1
    out = capsys.readouterr().out
    assert json.loads(out)["error"] == "E_RESULT_UNVERIFIED" and STORED["body"] not in out
    fake_memory.mode = "good"
    assert _read(fake_memory.base_url, path) == 0, "control"
    capsys.readouterr()


@pytest.mark.parametrize("mode", ["pending", "running"])
def test_a_read_still_running_when_polling_stops_is_not_a_refusal(tmp_path, fake_memory,
                                                                  mode):
    """A call the server has not finished when the client stops polling has no answer
    yet. It is E_READ_POLL_TIMEOUT with the status it was left in, never E_READ_REFUSED
    ("the server ran memory.read and it failed") with a null code. Control: the same read
    with the server answering is a message.
    Kills: a non-terminal status reported as the server's refusal."""
    path = _state_file(tmp_path, fake_memory.base_url)
    state = node.load_state(path, fake_memory.base_url)
    fake_memory.mode = mode
    with pytest.raises(node.ReadFailed) as failed:
        node.read(path, state, key=READ_KEY, poll_timeout=0.3)
    assert failed.value.error["error"] == "E_READ_POLL_TIMEOUT", failed.value.error
    assert failed.value.error["status"] == mode and "error_code" not in failed.value.error
    fake_memory.mode = "good"
    assert node.read(path, state, key=READ_KEY, poll_timeout=0.3) == PRINTED, "control"


@pytest.mark.parametrize("mode,jwks_mode", [("short_signature", "good"),
                                            ("int_signature", "good"), ("good", "no_x")])
def test_a_malformed_signature_or_key_is_unverified_never_an_exception(
        tmp_path, fake_memory, capsys, mode, jwks_mode):
    """What the server sends is checked, never trusted to be well formed: a signature that
    is not base64url, a signature that is a number, a JWKS key with no public key. Each is
    E_RESULT_UNVERIFIED (exit 1) from `read`, and a typed `read_error` from read_or_error
    (what listen --read and inbox --bodies use), never an exception that ends the command.
    Control: the well-formed row reads.
    Kills: wire.result_signature_ok's catch-all removed (binascii.Error, TypeError and
    KeyError then escape, as they did at 09b08e0)."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.mode, fake_memory.jwks_mode = mode, jwks_mode
    assert _read(fake_memory.base_url, path) == 1
    out = capsys.readouterr().out
    assert json.loads(out)["error"] == "E_RESULT_UNVERIFIED" and STORED["body"] not in out
    state = node.load_state(path, fake_memory.base_url)
    name, got = node.read_or_error(path, state, READ_KEY)
    assert name == "read_error" and got["error"] == "E_RESULT_UNVERIFIED", got
    fake_memory.mode, fake_memory.jwks_mode = "good", "good"
    assert _read(fake_memory.base_url, path) == 0, "control"
    capsys.readouterr()


@contextlib.contextmanager
def _not_http():
    """A server that answers every request with a line that is not an HTTP status line."""
    import socket as _socket
    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def run():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                # Read the WHOLE request first: closing with bytes unread makes Windows
                # reset the connection, which the client would see as a reset (an
                # OSError, typed all along) instead of the bad status line.
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                head, _, rest = raw.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                while len(rest) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                conn.sendall(b"GARBAGE-NOT-HTTP\r\n\r\n")
                conn.shutdown(_socket.SHUT_WR)
                while conn.recv(65536):
                    pass
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=run, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


def test_an_answer_that_is_not_http_fails_one_read_and_never_the_listen(tmp_path,
                                                                        monkeypatch, capsys):
    """http.client.HTTPException (a bad status line, a truncated body) is not an OSError,
    and at 09b08e0 it escaped read_or_error and ended `listen --read` and `inbox --bodies`,
    and `read` with a traceback. Now it is BrokenResponse, an OSError: `read` exits 2 with
    `unreachable`, read_or_error types it E_UNREACHABLE, and listen --read carries on to
    the next frame (the REAL read path, against a server that answers garbage).
    Kills: the HTTPException wrap removed from node.http_request."""
    with _not_http() as garbage:
        path = _state_file(tmp_path, garbage)
        assert _read(garbage, path) == 2
        cap = capsys.readouterr()
        assert json.loads(cap.err)["unreachable"].startswith("BrokenResponse: BadStatusLine")
        state = node.load_state(path, garbage)
        name, got = node.read_or_error(path, state, READ_KEY)
        assert name == "read_error" and got["error"] == "E_UNREACHABLE", got

        real_read = node.read

        def read_via_garbage(p, st, *, key, **kw):
            if key.startswith("msg/1-"):
                return real_read(p, {**st, "base_url": garbage}, key=key, **kw)
            return {**PRINTED, "key": key}

        monkeypatch.setattr(node, "read", read_via_garbage)
        with _ws_server([_mesh("msg/1-a-000001"), _mesh("msg/2-a-000002")]) as base_url:
            listen_path = tmp_path / "listen"
            listen_path.mkdir()
            lpath = _state_file(listen_path, base_url)
            assert cli.main(["listen", "--base-url", base_url, "--state-file", str(lpath),
                             "--count", "2", "--duration", "30", "--read"]) == 0
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [ln["frame"]["key"] for ln in lines] == ["msg/1-a-000001", "msg/2-a-000002"]
    assert lines[0]["read_error"]["error"] == "E_UNREACHABLE", lines[0]
    assert lines[1]["read"]["key"] == "msg/2-a-000002", "the listen went on"



@pytest.mark.parametrize("mode", ["list_status", "object_status"])
def test_a_poll_answer_whose_status_is_not_a_string_fails_one_read_never_the_command(
        tmp_path, fake_memory, monkeypatch, capsys, mode):
    """A poll answer is the server's (or whoever answered in its place). One whose `status`
    is a JSON array or object is not a call row, and at 0636d25 it escaped read_or_error
    as a TypeError (`in TERMINAL` hashes it), ending `listen --read` and `inbox --bodies`
    and `read` with a traceback: the residual of the fin/read review's MEDIUM 2 / kimi B2
    that the HTTPException and signature fixes did not reach. Now it is ServerSaid, as a
    poll answer that is not an object already was: `read` exits 1 with the HTTP status and
    body, read_or_error types it E_READ_HTTP, and listen --read carries on to the next
    frame (the REAL read path, against the fake memory authority).
    Control: the same key read with the server answering is the message.
    Kills: the string check on the polled status removed from node.invoke."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.mode = mode
    assert _read(fake_memory.base_url, path) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["http_status"] == 200 and not isinstance(out["body"]["status"], str), out
    state = node.load_state(path, fake_memory.base_url)
    name, got = node.read_or_error(path, state, READ_KEY)
    assert name == "read_error" and got["error"] == "E_READ_HTTP", got
    assert got["http_status"] == 200 and STORED["body"] not in json.dumps(got)
    fake_memory.mode = "good"
    assert node.read(path, state, key=READ_KEY) == PRINTED, "control"

    fake_memory.modes = {"msg/1-a-000001": mode}
    real_read = node.read

    def read_via_fake(p, st, *, key, **kw):
        return real_read(p, {**st, "base_url": fake_memory.base_url}, key=key, **kw)

    monkeypatch.setattr(node, "read", read_via_fake)
    with _ws_server([_mesh("msg/1-a-000001"), _mesh("msg/2-a-000002")]) as base_url:
        listen_path = tmp_path / "listen"
        listen_path.mkdir()
        lpath = _state_file(listen_path, base_url)
        assert cli.main(["listen", "--base-url", base_url, "--state-file", str(lpath),
                         "--count", "2", "--duration", "30", "--read"]) == 0
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [ln["frame"]["key"] for ln in lines] == ["msg/1-a-000001", "msg/2-a-000002"]
    assert lines[0]["read_error"]["error"] == "E_READ_HTTP", lines[0]
    assert lines[1]["read"]["key"] == "msg/2-a-000002", "the listen went on"

def test_listen_bounds_each_read_by_what_is_left_of_duration(tmp_path, monkeypatch, capsys):
    """With --duration, each read's polling is bounded by what is left of it, so a slow
    call cannot hold the listen for the full 40 s poll past the deadline. Without
    --duration the read keeps its own default (the control: no bound is passed).
    Kills: listen calling read_or_error without the remaining time."""
    asked: list = []

    def fake_read(_path, _state, *, key, version=None, **kw):
        asked.append(kw.get("poll_timeout"))
        return {**PRINTED, "key": key}

    monkeypatch.setattr(node, "read", fake_read)
    frames = [_mesh("msg/1-a-000001"), _mesh("msg/2-a-000002")]
    with _ws_server(frames) as base_url:
        path = _state_file(tmp_path, base_url)
        assert cli.main(["listen", "--base-url", base_url, "--state-file", str(path),
                         "--count", "2", "--duration", "5", "--read"]) == 0
    assert len(asked) == 2 and all(isinstance(t, float) and 0 < t <= 5 for t in asked), asked
    asked.clear()
    with _ws_server(frames) as base_url:
        path = _state_file(tmp_path, base_url)
        assert cli.main(["listen", "--base-url", base_url, "--state-file", str(path),
                         "--count", "2", "--read"]) == 0
    assert asked == [None, None], "control: no --duration, no bound"
    capsys.readouterr()


def test_inbox_bodies_reads_nothing_for_a_hit_without_an_integer_version(tmp_path,
                                                                         fake_memory,
                                                                         capsys):
    """--bodies reads each hit at the version the hit names. A hit that names none (null,
    a string, a bool) gets E_HIT_HAS_NO_VERSION and NO read: at 09b08e0 it was read at the
    head, which can be a different version from the one the hit points at. Control: the
    integer hit beside them is read at its version.
    Kills: the head fallback restored."""
    hits = [dict(HITS[0])] + [{**HITS[1], "key": f"msg/9{n}-desktop-a-00009{n}",
                               "version": v} for n, v in ((1, None), (2, "2"), (3, True))]
    fake_memory.inbox_hits = hits
    path = _state_file(tmp_path, fake_memory.base_url)
    assert cli.main(["inbox", "--base-url", fake_memory.base_url, "--state-file", str(path),
                     "--bodies"]) == 0
    out = json.loads(capsys.readouterr().out)["hits"]
    assert out[0]["read"]["version"] == 1, "control: the integer version is read"
    for hit in out[1:]:
        assert hit["read_error"] == {"error": "E_HIT_HAS_NO_VERSION", "key": hit["key"],
                                     "version": hit["version"],
                                     "code": "E_HIT_HAS_NO_VERSION", "retryable": False}, hit
        assert "read" not in hit
    assert [a["key"] for a in fake_memory.read_args()] == [HITS[0]["key"]], \
        "a hit with no integer version was read"


@pytest.mark.parametrize("mode,extra,why", [
    ("other_version", ("--version", "2"), "version 6"),
    ("not_head", (), "not the head"),
    ("other_namespace", (), "shared/other/")])
def test_a_signed_answer_about_another_version_namespace_or_row_is_not_the_message(
        tmp_path, fake_memory, capsys, mode, extra, why):
    """README "8. Chat": a verified answer about a different key OR VERSION exits 1 and
    prints none of its content. At 09b08e0 only the key case was tested, and the three
    checks below survived their mutants (the fake always echoed what was asked). Control:
    the same read, answered about what was asked.
    Kills: message_of's version check, its is_head check and its namespace check, each
    removed."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.mode = mode
    assert _read(fake_memory.base_url, path, *extra) == 1
    out = capsys.readouterr().out
    got = json.loads(out)
    assert got["error"] == "E_RESULT_MISMATCH" and why in got["detail"], got
    assert STORED["body"] not in out
    fake_memory.mode = "good"
    assert _read(fake_memory.base_url, path, *extra) == 0, "control"
    capsys.readouterr()


def test_a_jwks_that_cannot_be_fetched_verifies_nothing(tmp_path, fake_memory, capsys):
    """With the server's JWKS answering 500 nothing can be verified: the read is
    E_RESULT_UNVERIFIED and says why. Control: the same read with the JWKS up.
    Kills: a failed JWKS fetch counted as a verified signature (node.invoke)."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.jwks_mode = "down"
    assert _read(fake_memory.base_url, path) == 1
    out = capsys.readouterr().out
    got = json.loads(out)
    assert got["error"] == "E_RESULT_UNVERIFIED" and got["detail"] == "JWKS answered 500"
    assert STORED["body"] not in out
    fake_memory.jwks_mode = "good"
    assert _read(fake_memory.base_url, path) == 0, "control"
    capsys.readouterr()


def test_context_proves_possession_of_the_nodes_key(tmp_path, fake_memory, capsys):
    """The context anchor's body is memory.read's answer, which the server gives only to a
    request that proves it holds its key (divergence 027). So `context` sends a DPoP proof
    bound to exactly this GET, this path and this token, signed by the node's key.
    Kills: context sending its GET without the proof."""
    path = _state_file(tmp_path, fake_memory.base_url)
    assert cli.main(["context", "--base-url", fake_memory.base_url, "--state-file",
                     str(path), "--key", READ_KEY]) == 0
    capsys.readouterr()
    (headers,) = fake_memory.context_headers
    public = P.b64u_decode(json.loads(path.read_text(encoding="utf-8"))["public_key_b64url"])
    assert headers.get("authorization") == "Bearer h.p.s"
    ok, why = P.verify_dpop(headers.get("dpop") or "", public, method="GET",
                            target="/v1/mesh/context", jwt="h.p.s")
    assert ok, why


def test_the_exit_codes_say_that_listen_read_and_inbox_bodies_exit_0_on_failed_reads(
        tmp_path, fake_memory, capsys):
    """`listen --read` and `inbox --bodies` exit 0 even when every read failed (each failure
    is a typed read_error), and the EXIT CODES the CLI prints now say so. Behaviour
    (the pin): an inbox whose every hit is refused exits 0. The documentation is the
    finding: at 09b08e0 it did not say.
    Kills: the sentence removed from cli's EXIT CODES."""
    fake_memory.inbox_hits = [dict(h) for h in HITS]
    fake_memory.mode = "refused"
    path = _state_file(tmp_path, fake_memory.base_url)
    assert cli.main(["inbox", "--base-url", fake_memory.base_url, "--state-file", str(path),
                     "--bodies"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert all(h["read_error"]["error"] == "E_READ_REFUSED" for h in out["hits"])
    doc = " ".join(cli.__doc__.split())
    assert ("`listen --read` and `inbox --bodies` exit 0 even when some or every read "
            "failed") in doc, doc


def test_a_slow_read_with_frames_queued_behind_it_does_not_close_the_listen(tmp_path,
                                                                           monkeypatch,
                                                                           capsys):
    """The review's inferred risk, run: listen --read reads in its receive loop, and the
    websockets client stops reading the socket once 16 frames are queued, so it cannot
    read the pong to its own keepalive ping either; a read slower than the keepalive
    window then closed the listen. The keepalive is shortened to 0.5 s (the default is
    20 s ping plus 20 s wait) so one 2 s read shows it; 24 frames are sent at once. At
    09b08e0 the listen closed after 18. Control, one variable apart: the same run with a
    read that does not wait prints all 24.
    Kills: the unbounded receive queue for --read removed."""
    import websockets.sync.client as ws_client

    real_connect = ws_client.connect

    def fast_keepalive(*args, **kwargs):
        kwargs.setdefault("ping_interval", 0.5)
        kwargs.setdefault("ping_timeout", 0.5)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(ws_client, "connect", fast_keepalive)
    frames = [_mesh(f"msg/{n}-a-{n:06d}") for n in range(24)]
    for delay in (0.0, 2.0):
        waited: list = []

        def slow_read(_path, _state, *, key, **_kw):
            if not waited:
                waited.append(key)
                time.sleep(delay)
            return {**PRINTED, "key": key}

        monkeypatch.setattr(node, "read", slow_read)
        with _ws_server(frames) as base_url:
            sub = tmp_path / f"delay-{delay}"
            sub.mkdir()
            path = _state_file(sub, base_url)
            assert cli.main(["listen", "--base-url", base_url, "--state-file", str(path),
                             "--count", "24", "--duration", "20", "--read"]) == 0
        cap = capsys.readouterr()
        keys = [json.loads(ln)["frame"]["key"] for ln in cap.out.splitlines()]
        assert keys == [f["key"] for f in frames], (delay, len(keys), cap.err[-300:])
        assert '"closed"' not in cap.err, (delay, cap.err[-300:])


# --------------------------------------------------------------------------------------------------
# A VALUE THAT STARTS WITH '-' (found on a real full-suite run of integ/r1, 2026-09-16). A pairing code is
# base64url, so 1 in 64 begins with '-'; argparse then reads it as an option and reports `--code: expected
# one argument`. That broke tests/test_meshy_node_read.py's module fixture (9 errors) on a run whose code
# began with one, like "-rOgmD0WHMb4u8JvgMBAvd0o6XRHdlvH", and it would break a real operator relaying such a code. A body or
# a query that starts with '-' fails the same way when it has no space in it (argparse treats a token with a
# space as a value). Every single-value option binds the next token.
@pytest.mark.parametrize("argv, field, value", [
    (["mint", "--code", "-rOgmD0WHMb4u8JvgMBAvd0o6XRHdlvH"], "code", "-rOgmD0WHMb4u8JvgMBAvd0o6XRHdlvH"),
    (["send", "--to", "desktop-a", "--thread", "t/x", "--body", "-draft"], "body", "-draft"),
    (["search", "--q", "-urgent"], "q", "-urgent"),
])
def test_a_value_that_starts_with_a_hyphen_is_still_the_options_value(monkeypatch, tmp_path, argv, field, value):
    seen = {}
    monkeypatch.setattr(cli, "run", lambda args: seen.update(vars(args)) or 0)
    full = [argv[0], "--base-url", "http://127.0.0.1:9", "--state-file", str(tmp_path / "n.json")] + argv[1:]
    assert cli.main(full) == 0
    assert seen[field] == value


def test_an_option_given_as_the_last_token_is_still_a_parse_error(monkeypatch, tmp_path):
    """Control: binding the next token must not invent a value when there is none."""
    monkeypatch.setattr(cli, "run", lambda args: 0)
    with pytest.raises(SystemExit) as exc:
        cli.main(["mint", "--base-url", "http://127.0.0.1:9", "--state-file", str(tmp_path / "n.json"), "--code"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------------------------------
# THE CLIENT UNDER THE SERVER'S REPLAY LEDGER (divergence 036). The server now refuses, by default, a
# signed envelope whose nonce it has already recorded (403 E_FORBIDDEN, the same answer as a missing
# grant). A client that retried a lost answer by sending the SAME envelope again would be refused
# whenever the first presentation reached the ledger and created no call. Measured on this client:
# `invoke` (node.py) is the only path that sends a signed envelope, it builds a new one (fresh call_id,
# fresh nonce) every time it is called, and it sends each one once: there is no retry loop around the
# POST, and `listen --monitor` (monitor.py) sends no signed envelope at all (its catch-up and
# reconnects are GETs and a socket upgrade, each with a fresh DPoP proof). These two tests pin that.

def test_every_signed_call_the_client_makes_carries_its_own_nonce_and_call_id(tmp_path, fake_memory,
                                                                             capsys):
    """Five signed calls from one state file: two `read`s and an `inbox --bodies` over three hits.
    Every envelope's nonce and call_id is new, and the echoed call_id is the signed one."""
    fake_memory.inbox_hits = [dict(h) for h in HITS]
    path = _state_file(tmp_path, fake_memory.base_url)
    assert _read(fake_memory.base_url, path) == 0
    assert _read(fake_memory.base_url, path) == 0
    assert cli.main(["inbox", "--base-url", fake_memory.base_url, "--state-file", str(path),
                     "--bodies"]) == 0
    capsys.readouterr()
    payloads = [i["body"]["envelope"]["payload"] for i in fake_memory.invokes]
    assert len(payloads) == 5, "positive control: every call reached the server"
    assert all(isinstance(p.get("nonce"), str) and p["nonce"] for p in payloads), payloads
    assert len({p["nonce"] for p in payloads}) == 5, [p["nonce"] for p in payloads]
    assert len({p["call_id"] for p in payloads}) == 5
    assert [i["body"]["call_id"] for i in fake_memory.invokes] == [p["call_id"] for p in payloads]


@pytest.mark.parametrize("command", ["read", "send"])
@pytest.mark.parametrize("failure", ["invoke_timeout", "poll_refused"])
def test_an_invoke_whose_answer_never_came_is_not_sent_again(tmp_path, fake_memory, monkeypatch,
                                                            capsys, command, failure):
    """The server receives the invoke before its answer is lost or a later poll is refused.
    The client reports unreachable (exit 2) without repeating the POST. No poll reaches the server.
    A retry of those bytes would be the byte-identical resend the ledger refuses.
    Control: the same command with the answer delivered completes, through the same wrapper."""
    fake_memory.accept_writes = command == "send"
    real = node.http_request
    lose = {"on": True}

    def answer_lost(method, base_url, path_, **kw):
        if lose["on"] and failure == "poll_refused" and path_.startswith("/v1/calls/"):
            raise ConnectionRefusedError("poll refused")
        got = real(method, base_url, path_, **kw)          # the server really receives it
        if lose["on"] and failure == "invoke_timeout" and method == "POST" and path_.endswith("/invoke"):
            raise TimeoutError("timed out")
        return got

    monkeypatch.setattr(node, "http_request", answer_lost)
    path = _state_file(tmp_path, fake_memory.base_url)
    def run_command():
        if command == "read":
            return _read(fake_memory.base_url, path)
        return cli.main(["send", "--base-url", fake_memory.base_url, "--state-file", str(path),
                         "--to", "b", "--thread", "t", "--body", "hello",
                         "--no-recipient-check", "--no-card-check"])

    assert run_command() == 2
    cap = capsys.readouterr()
    err = json.loads(cap.err)
    if command == "send":
        # #629: an unknown-outcome send names the key it tried, which is the key the server received.
        sent_key = fake_memory.invokes[0]["body"]["envelope"]["payload"]["args"]["key"]
        assert err.pop("attempted_key") == sent_key, cap.err
        assert err.pop("settle") == "read --key %s before any resend: present = it landed, do not resend" % sent_key
    assert err == {
        "unreachable": ("TimeoutError: timed out" if failure == "invoke_timeout"
                        else "ConnectionRefusedError: poll refused"),
        "code": "E_UNREACHABLE",
        "retryable": command == "read",
        **({"outcome": "unknown"} if command == "send" else {}),
    }, cap.err
    assert len(fake_memory.invokes) == 1, "the client sent the envelope again"
    assert not any(g.startswith("/v1/calls/") for g in fake_memory.gets), fake_memory.gets

    lose["on"] = False
    assert run_command() == 0
    completed = json.loads(capsys.readouterr().out)
    if command == "read":
        assert completed == PRINTED
    else:
        assert completed["status"] == "completed"
    assert len(fake_memory.invokes) == 2
    first, second = (i["body"]["envelope"]["payload"] for i in fake_memory.invokes)
    assert first["nonce"] != second["nonce"] and first["call_id"] != second["call_id"]


def test_an_invoke_the_server_failed_is_not_sent_again(tmp_path, fake_memory, monkeypatch,
                                                      capsys):
    """A 5xx is the answer the fleet's other client (mem_client) retries with the SAME body
    (review of divergence 036, finding 1; mutant M4 put that retry here and passed every
    test). This client sends each envelope once: the server receives the POST, answers 500,
    and the command reports it (exit 1, the status and body) with no second POST and no
    poll. Control: the same read answered normally completes, with a new nonce."""
    real = node.http_request
    fail = {"left": 1}

    def server_failed(method, base_url, path_, **kw):
        got = real(method, base_url, path_, **kw)          # the server really receives it
        if fail["left"] and method == "POST" and path_.endswith("/invoke"):
            fail["left"] -= 1
            return 500, {"error": "E_INTERNAL", "message": "internal error"}, {}
        return got

    monkeypatch.setattr(node, "http_request", server_failed)
    path = _state_file(tmp_path, fake_memory.base_url)
    assert _read(fake_memory.base_url, path) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["http_status"] == 500, printed
    assert fail["left"] == 0, "positive control: the 500 was delivered"
    assert len(fake_memory.invokes) == 1, "the client sent the envelope again"
    assert not any(g.startswith("/v1/calls/") for g in fake_memory.gets), fake_memory.gets

    assert _read(fake_memory.base_url, path) == 0
    assert json.loads(capsys.readouterr().out) == PRINTED
    assert len(fake_memory.invokes) == 2
    first, second = (i["body"]["envelope"]["payload"] for i in fake_memory.invokes)
    assert first["nonce"] != second["nonce"] and first["call_id"] != second["call_id"]


# =============================================================== 7. the shared helpers, pinned
# One writer, one proved header pair and one socket endpoint stand behind the state file,
# the proved calls and both listens. Each test here pins the contract the callers relied on
# before the helper existed, so the helper cannot drift from what they wrote or sent.

def test_the_state_file_write_is_not_fsynced_and_the_sidecar_write_is(tmp_path, monkeypatch):
    """`node._write_private` is the one atomic owner-only JSON writer. The state file's
    writers call it as they always did, with no fsync (no new I/O on the state file); the
    monitor's `_write_sidecar` calls it with `fsync=True`, the temp file fsynced BEFORE
    the replace (review A2), and on POSIX its directory fsynced AFTER it (`_fsync_dir`, so the
    new name survives a power loss; Windows has no directory fsync). Both leave the same bytes,
    an owner-only file where the platform honours it, and no temp file behind."""
    from tools.meshy_node import monitor
    dir_sync = ["fsync"] if node._dir_fsync_supported() else []
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    data = {"z": 1, "a": [1, 2], "schema": "x"}
    plain, synced, side = tmp_path / "n.json", tmp_path / "s.json", tmp_path / "n.json.monitor"
    node._write_private(plain, data)
    assert events == ["replace"], events
    node._write_private(synced, data, fsync=True)
    assert events == ["replace", "fsync", "replace", *dir_sync], events
    monitor._write_sidecar(side, data)
    assert events == ["replace", "fsync", "replace", *dir_sync, "fsync", "replace", *dir_sync], events
    want = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert plain.read_bytes() == synced.read_bytes() == side.read_bytes() == want
    if os.name != "nt":
        assert [p.stat().st_mode & 0o777 for p in (plain, synced, side)] == [0o600] * 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["n.json", "n.json.monitor", "s.json"]


def test_the_proved_header_pair_is_the_bearer_and_a_fresh_proof_for_that_method_and_path(
        tmp_path):
    """`node._proved_headers` builds what `invite`, `invoke`, `_get` with `prove` and the
    monitor's catch-up each built by hand: `Authorization` first, then `DPoP`, nothing
    else; the proof signed by the node's key for exactly the method and the base URL's
    prefix plus the route (the path the server sees); and a new proof per call, since a
    proof is spent once. Control: the same proof is refused for another method."""
    base = "http://127.0.0.1:9/prefix"
    state = json.loads(_state_file(tmp_path, base).read_text(encoding="utf-8"))
    public = P.b64u_decode(state["public_key_b64url"])
    headers = node._proved_headers(state, "h.p.s", "POST", node.PEER_PAIRING_CODES)
    assert list(headers) == ["Authorization", "DPoP"]
    assert headers["Authorization"] == "Bearer h.p.s"
    target = "/prefix" + node.PEER_PAIRING_CODES
    ok, why = P.verify_dpop(headers["DPoP"], public, method="POST", target=target, jwt="h.p.s")
    assert ok, why
    ok, _why = P.verify_dpop(headers["DPoP"], public, method="GET", target=target, jwt="h.p.s")
    assert not ok, "control: the proof is bound to its method"
    again = node._proved_headers(state, "h.p.s", "POST", node.PEER_PAIRING_CODES)
    assert again["DPoP"] != headers["DPoP"], "a proof is spent once: each call makes its own"


@pytest.mark.parametrize("tampered", [False, True])
def test_read_attachment_metadata_is_bound_to_signed_value(tmp_path, fake_memory, capsys, tampered):
    attachments = [{"file_id": "00000000-0000-4000-8000-000000000001",
                    "name": "serial.log", "size_bytes": 3, "sha256": "0" * 64}]
    value = {**STORED, "attachments": attachments}
    fake_memory.signed_args = {"value": value}
    served = ({**value, "attachments": [{**attachments[0], "name": "altered.log"}]}
              if tampered else value)
    fake_memory.served = {"value": served}
    path = _state_file(tmp_path, fake_memory.base_url)
    result = _read(fake_memory.base_url, path)
    got = json.loads(capsys.readouterr().out)
    if tampered:
        assert result == 0
        assert got["attachments"] == served["attachments"]
        assert got["signature_verified"] is False
        assert got["status_verified"] is False
        assert got["signature_error"].startswith("E_SIGNATURE_NOT_THE_MESSAGE: ")
        assert got["status_error"].startswith("E_SIGNATURE_NOT_THE_MESSAGE: ")
    else:
        assert result == 0
        assert got["attachments"] == attachments
        assert got["signature_verified"] is True
        assert got["status_verified"] is True


def test_the_socket_endpoint_is_the_one_listen_and_the_monitor_both_connect_to(tmp_path):
    """`node._ws_endpoint`: the scheme follows the base URL's (ws for http, wss for https),
    its path prefix is kept ahead of /v1/ws, and the subprotocol list is exactly `meshy.v1`,
    `jwt.<token>`, `dpop.<proof>`, the proof bound to WS_UPGRADE and the socket's path.
    `listen` and `listen --monitor` hand exactly this pair to `connect`."""
    for base, want_uri, want_target in (
            ("http://127.0.0.1:9", "ws://127.0.0.1:9/v1/ws", "/v1/ws"),
            ("https://mesh.example:8443/meshy", "wss://mesh.example:8443/meshy/v1/ws",
             "/meshy/v1/ws")):
        state = json.loads(_state_file(tmp_path, base).read_text(encoding="utf-8"))
        public = P.b64u_decode(state["public_key_b64url"])
        uri, subprotocols = node._ws_endpoint(state, "h.p.s")
        assert uri == want_uri
        assert subprotocols[:2] == ["meshy.v1", "jwt.h.p.s"] and len(subprotocols) == 3
        assert subprotocols[2].startswith("dpop.")
        ok, why = P.verify_dpop(subprotocols[2][len("dpop."):], public,
                                method=wire.WS_UPGRADE, target=want_target, jwt="h.p.s")
        assert ok, why
