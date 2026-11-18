"""Private bytes over the LAN: `offer` serves a file once to one signed pickup, `pickup` fetches and verifies it, and the
bytes never pass through the mesh server (tools/meshy_node/offer.py).

THE FAILURE, 2026-10: seats moved bytes that must never reach the server or a cloud store (firmware, per-unit calibration,
MAC-derived payloads) with ad-hoc one-shot HTTP servers on the LAN and links pasted into messages by hand. Links expired or
were spent four times and had to be served again, and the sha256 was copied by hand (one hand-typed pin was wrong).

EVERY REFUSAL SITS BESIDE ITS CONTROL (docs/V1-DESIGN-RULES.md C1). Each 404 is followed by the one GET that IS served, so
a refusal that spent the offer would fail its own control; each pickup refusal that must happen before the offer is
contacted asserts the offering server saw no request at all.

No network and no database: every server listens on 127.0.0.1, and node.send, node.read, node.author_keys and
node.load_state are replaced, as tests/test_meshy_node_docshare.py replaces node.send (the send-deadline tests run the
real node.send and replace node._opener, under a clock that moves only by their requests).

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_offer.py -q -p no:cacheprovider
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import io
import json
import os
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, docshare, node, offer, wire  # noqa: E402

_REAL_SEND = node.send               # the `world` fixture replaces it; the send-deadline tests put it back
_REAL_SUCCEEDED = node.succeeded     # as node.send

RECIPIENT = "7f0c2a52-1b3d-4e5f-8a9b-000000000b0b"
OFFERER = "7f0c2a52-1b3d-4e5f-8a9b-000000000a1c"
MALLORY = "7f0c2a52-1b3d-4e5f-8a9b-000000000e0e"    # a third seat that can read alice's offers: the relay's author
BASE = "http://127.0.0.1:1"          # never contacted: every call that would reach it is replaced
DATA = b"synthetic payload \x00\x01\x02 not a real firmware image\n" * 50
FAR = "2099-01-01T00:00:00Z"
URL = "http://192.168.50.10:40123/" + "t" * 43   # an offer line's url, for the signing tests that run no server


def _identity(peer: str, label: str) -> tuple[dict, dict]:
    """(state, the JWKS that peer publishes): a node key made here, as `mint` makes one."""
    seed = wire.new_seed()
    public = wire.public_key_of(seed)
    kid = wire.peer_kid(public)
    state = {"schema": node.STATE_SCHEMA, "base_url": BASE, "status": node.STATUS_MINTED, "peer_id": peer,
             "kid": kid, "signing_seed_b64url": wire.b64u(seed), "label": label}
    return state, {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": kid, "x": wire.b64u(public)}]}


@pytest.fixture()
def bob():
    return _identity(RECIPIENT, "bob")


@pytest.fixture()
def alice():
    return _identity(OFFERER, "alice")


@pytest.fixture()
def serve(tmp_path, bob):
    """Start an OfferServer for a file's snapshot on 127.0.0.1, already armed (as after an accepted send). Returns a
    factory of (server, events, meta); every server is stopped and every snapshot removed at teardown."""
    made, snapshots = [], []

    def start(data: bytes = DATA, *, name: str = "fw.bin", ttl: float = 30.0, expires: str = FAR):
        folder = tmp_path / f"src{len(made)}"
        folder.mkdir()
        f = folder / name
        f.write_bytes(data)
        snapshot = offer.Snapshot(f)
        snapshots.append(snapshot)
        events: list = []
        token = secrets.token_urlsafe(offer.TOKEN_BYTES)
        server = offer.OfferServer(("127.0.0.1", 0), url_host="127.0.0.1", offered_by=OFFERER, snapshot=snapshot,
                                   token=token, expires=expires, ttl=ttl, not_after=float("inf"), for_peer=RECIPIENT,
                                   keys=offer.published_keys(bob[1]), emit=events.append)
        made.append(server)
        server.start()
        assert server.url == f"http://127.0.0.1:{server.server_address[1]}/{token}"
        meta = {"name": name, "size_bytes": snapshot.size, "sha256": snapshot.sha256, "url": server.url,
                "expires": expires, "for_peer": RECIPIENT}
        return server, events, {"meta": meta, "token": token, "file": f}

    yield start
    for server in made:
        server.stop()
    for snapshot in snapshots:
        snapshot.close()


@pytest.fixture()
def snapshot_dirs(monkeypatch):
    """Every directory a Snapshot makes (tempfile.mkdtemp), so a test can check none is left."""
    made, real = [], tempfile.mkdtemp

    def mkdtemp(*a, **kw):
        made.append(real(*a, **kw))
        return made[-1]
    monkeypatch.setattr(offer.tempfile, "mkdtemp", mkdtemp)
    return made


def _symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:          # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")


def _request(server, path: str, headers: dict | None = None, method: str = "GET") -> tuple[int, bytes]:
    """One request to `server` (an OfferServer, or the port of one on 127.0.0.1)."""
    port = server if isinstance(server, int) else server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _signed(state: dict, info: dict) -> dict:
    """A pickup of the `serve` offer: alice's, at the url its line carries."""
    return offer.pickup_headers(state, url=info["meta"]["url"], offered_by=OFFERER, token=info["token"],
                                expires=info["meta"]["expires"])


def _refusals(events) -> list[str]:
    return [e["why"] for e in events if e["event"] == "refused"]


# ------------------------------------------------------------------------------------------------ the server: one GET

def test_one_signed_get_is_served_and_the_offer_names_the_address_that_took_it(serve, bob):
    server, events, info = serve()
    status, body = _request(server, "/" + info["token"], _signed(bob[0], info))
    assert status == 200 and body == DATA
    # The whole body read is not the handler done: it sets `outcome` and `done` after its last write, on its own thread.
    assert server.done.wait(timeout=5) and server.outcome == "served" and server.wait() == "served"
    served = [e for e in events if e["event"] == "served"]
    assert served == [{"event": "served", "to_address": "127.0.0.1", "peer": RECIPIENT, "bytes": len(DATA),
                       "sha256": hashlib.sha256(DATA).hexdigest()}]


def test_a_second_get_is_404_even_signed_with_a_fresh_nonce(serve, bob):
    server, events, info = serve()
    assert _request(server, "/" + info["token"], _signed(bob[0], info))[0] == 200          # control
    assert server.done.wait(timeout=5)              # the first handler finished: until then it may hold the lock
    status, body = _request(server, "/" + info["token"], _signed(bob[0], info))
    assert (status, body) == (404, b"404 Not Found\n")
    assert _refusals(events) == ["already served"]


def test_a_wrong_token_is_404_and_does_not_spend_the_offer(serve, bob):
    server, events, info = serve()
    other = secrets.token_urlsafe(offer.TOKEN_BYTES)
    for path in ("/" + other, "/", "/" + info["token"] + "/", "//" + info["token"], "/" + info["token"] + "?x=1"):
        assert _request(server, path, _signed(bob[0], info))[0] == 404, path
    assert server.outcome is None and not server.done.is_set()
    assert _request(server, "/" + info["token"], _signed(bob[0], info))[0] == 200           # still there
    assert _refusals(events) == ["not the offer's path"] * 5


def test_head_and_every_other_method_is_404_and_does_not_spend_the_offer(serve, bob):
    server, events, info = serve()
    for method in ("HEAD", "POST", "PUT", "DELETE", "OPTIONS"):
        status, body = _request(server, "/" + info["token"], _signed(bob[0], info), method=method)
        assert status == 404 and body == (b"" if method == "HEAD" else b"404 Not Found\n"), method
    assert server.outcome is None
    assert _request(server, "/" + info["token"], _signed(bob[0], info))[0] == 200
    assert all(why.startswith("method ") for why in _refusals(events))


def test_a_request_not_signed_by_the_recipients_key_is_404_and_does_not_spend_the_offer(serve, bob, alice):
    """The token is stored on the server inside the message, so holding it must not be enough: only the key the
    recipient's peer publishes opens the file."""
    server, events, info = serve()
    path = "/" + info["token"]
    unsigned = _request(server, path)
    forged = dict(_signed(alice[0], info), **{offer.PEER_HEADER: RECIPIENT})                # alice's key, bob's peer id
    as_alice = _signed(alice[0], info)                                                      # alice signing as herself
    tampered = dict(_signed(bob[0], info), **{offer.NONCE_HEADER: secrets.token_urlsafe(18)})
    for headers in (forged, as_alice, tampered):
        assert _request(server, path, headers)[0] == 404
    assert unsigned[0] == 404 and server.outcome is None
    assert _request(server, path, _signed(bob[0], info))[0] == 200                          # control: bob's own
    why = _refusals(events)
    assert why[0].startswith("the request is not signed")
    assert any("signed as peer " + OFFERER in w for w in why)
    assert sum("does not verify" in w or "is not one peer" in w for w in why) == 2


def test_only_a_signed_request_can_meet_a_transfer_in_progress(serve, bob):
    """A reader of the message holds the token but not the key. Its requests are refused on the signature, before the
    transfer lock is tried, so they cannot make the real pickup meet "a transfer is in progress"."""
    server, events, info = serve()
    path = "/" + info["token"]
    with server.transfer:                                   # as while the real pickup's transfer runs
        assert _request(server, path)[0] == 404
        assert _request(server, path, _signed(bob[0], info))[0] == 404
    assert _refusals(events)[0].startswith("the request is not signed")
    assert _refusals(events)[1] == "a transfer is in progress"
    assert _request(server, path, _signed(bob[0], info))[0] == 200                          # not counted


def test_a_nonce_is_accepted_once(bob):
    keys, nonces = offer.published_keys(bob[1]), set()
    headers = offer.pickup_headers(bob[0], url=URL, offered_by=OFFERER, token="t" * 43, expires=FAR)
    check = dict(url=URL, offered_by=OFFERER, token="t" * 43, expires=FAR, for_peer=RECIPIENT, keys=keys, nonces=nonces)
    assert offer.pickup_problem(headers, **check) is None                                   # control
    assert offer.pickup_problem(headers, **check) == "the nonce was used before: a replayed request"
    assert offer.pickup_problem(headers, **dict(check, expires="2099-01-01T00:00:01Z")) is not None, \
        "a signature over one offer's window does not open another's"


def test_the_signed_bytes_are_their_own_domain(bob):
    signed = offer.pickup_message(url=URL, offered_by=OFFERER, token="t" * 43, nonce="n" * 24, expires=FAR,
                                  peer_id=RECIPIENT)
    assert b'"purpose":"meshy-offer-pickup-v1"' in signed
    assert signed != wire.signin_message(RECIPIENT, "n" * 24)
    assert json.loads(signed) == {"expires": FAR, "nonce": "n" * 24, "offered_by": OFFERER, "peer_id": RECIPIENT,
                                  "purpose": offer.PICKUP_PURPOSE, "token": "t" * 43, "url": URL}


@pytest.mark.parametrize("other", [
    {"url": "http://192.168.7.66:40123/" + "t" * 43},                   # a decoy's server, the same token
    {"url": "http://192.168.50.10:40124/" + "t" * 43},                   # another port of the same host
    {"offered_by": MALLORY},                                             # the same url, another seat's message
], ids=["another-address", "another-port", "another-offering-seat"])
def test_a_signature_made_for_another_offers_url_or_author_does_not_verify(bob, other):
    """A pickup signature names the url it was sent to and the seat whose message offered it, and the offer checks both
    against its own: one a decoy collected (the same token, expires and for_peer, at its own url, in its own message)
    verifies nowhere else. Each is refused on its own, so the url and the offering peer are each bound."""
    keys, nonces = offer.published_keys(bob[1]), set()
    mine = dict(url=URL, offered_by=OFFERER, token="t" * 43, expires=FAR)
    elsewhere = offer.pickup_headers(bob[0], **dict(mine, **other))
    assert offer.pickup_problem(elsewhere, **mine, for_peer=RECIPIENT, keys=keys, nonces=nonces).startswith(
        "the signature does not verify")
    assert nonces == set(), "a refused signature spent its nonce"
    assert offer.pickup_problem(offer.pickup_headers(bob[0], **mine), **mine, for_peer=RECIPIENT, keys=keys,
                                nonces=nonces) is None                                      # control: this offer's own


def test_after_the_window_everything_is_404(serve, bob):
    server, events, info = serve(ttl=0.3)
    time.sleep(0.4)
    assert _request(server, "/" + info["token"], _signed(bob[0], info))[0] == 404
    assert _refusals(events) == ["the window has closed"] and server.wait() == "expired"


@pytest.fixture()
def timed_transfer(tmp_path, bob, monkeypatch):
    """The real server and signed HTTP handler, with an in-memory connection and a clock advanced by socket writes.
    No listening socket is needed: finish_request and shutdown_request run the same handler as a loopback pickup."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(offer, "CHUNK", 16)
    source = tmp_path / "fw.bin"
    source.write_bytes(b"x" * 64)
    snapshot = offer.Snapshot(source)
    events = []
    with monkeypatch.context() as patch:
        patch.setattr(offer.OfferServer, "server_bind", lambda self: None)
        patch.setattr(offer.OfferServer, "server_activate", lambda self: None)
        server = offer.OfferServer(("127.0.0.1", 40123), url_host="127.0.0.1", offered_by=OFFERER, snapshot=snapshot,
                                   token="t" * 43, expires=FAR, ttl=5, not_after=float("inf"), for_peer=RECIPIENT,
                                   keys=offer.published_keys(bob[1]), emit=events.append)
    server.deadline = clock["now"] + server.ttl

    class Connection:
        def __init__(self, seconds):
            self.seconds, self.timeout = seconds, None
            self.parts, self.writes = [], []
            self.closed_at = None

        def makefile(self, *args):
            headers = offer.pickup_headers(bob[0], url=server.url, offered_by=OFFERER, token=server.token, expires=FAR)
            request = (f"GET /{server.token} HTTP/1.0\r\n" +
                       "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n")
            return io.BytesIO(request.encode("ascii"))

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, chunk):
            if self.closed_at is not None:
                raise OSError("the connection is closed")
            remaining = server.deadline - clock["now"]
            self.writes.append((clock["now"], self.timeout))
            assert remaining > 0 and self.timeout == pytest.approx(min(60, remaining))
            if chunk.startswith(b"HTTP/"):
                self.parts.append(chunk)
            elif self.seconds > self.timeout:
                self.parts.append(chunk[:1])           # progress does not renew sendall's total timeout
                clock["now"] += self.timeout
                raise TimeoutError("the slow write used the remaining window")
            else:
                self.parts.append(chunk)
                clock["now"] += self.seconds

        def shutdown(self, how):
            self.close()

        def close(self):
            self.closed_at = clock["now"]

    yield server, events, clock, Connection
    server.stop()
    snapshot.close()


@pytest.mark.parametrize("seconds", [2.0, 2.5, 1.25, 0.25],
                         ids=["write-times-out", "between-writes", "last-write-at-deadline", "fast-control"])
def test_a_signed_transfer_must_finish_inside_the_window(timed_transfer, seconds):
    """A valid pickup keeps making progress. Expiry during a write, between writes and at completion must all stop it;
    a fast transfer spends the offer. The clock moves during writes, independent of scheduler timing."""
    server, events, clock, connection = timed_transfer
    conn = connection(seconds)
    try:
        server.finish_request(conn, ("127.0.0.1", 12345))
    finally:
        server.shutdown_request(conn)
    assert server.done.is_set() and conn.closed_at == clock["now"]
    assert conn.parts[0].startswith(b"HTTP/1.0 200 ")
    body = b"".join(conn.parts[1:])
    assert len(conn.writes) >= 3, "the pickup must have kept making progress"
    if seconds == 0.25:
        assert body == b"x" * 64 and clock["now"] < server.deadline
        assert server.wait() == server.outcome == "served"
        assert [e["event"] for e in events] == ["served"]
    else:
        assert body and clock["now"] == server.deadline
        assert server.wait() == server.outcome == "expired"
        assert [e["event"] for e in events] == ["interrupted"]
        assert "expired mid-transfer; not counted as served" in events[0]["detail"]
        if seconds != 1.25:
            assert len(body) < server.size


def test_wait_closes_an_active_connection_at_the_deadline_without_waiting_for_the_transfer(timed_transfer, monkeypatch):
    server, events, clock, connection = timed_transfer
    entered, release = threading.Event(), threading.Event()
    real_send = server._send

    def paused_send(handler):
        entered.set()
        assert release.wait(5)
        real_send(handler)

    monkeypatch.setattr(server, "_send", paused_send)
    result = []
    waiter = threading.Thread(target=lambda: result.append(server.wait()), daemon=True)
    conn = connection(0.25)
    pickup = threading.Thread(target=server.process_request_thread, args=(conn, ("127.0.0.1", 12345)), daemon=True)
    try:
        pickup.start()
        assert entered.wait(5) and server.transfer.locked()
        clock["now"] = server.deadline
        waiter.start()
        waiter.join(timeout=1)
        assert result == ["expired"], "wait waited for the active transfer after the deadline"
        assert server.transfer.locked() and server.outcome == "expired"
        assert conn.closed_at == server.deadline, "wait left the active connection open"
    finally:
        release.set()
        pickup.join(timeout=5)
        waiter.join(timeout=5)
    assert server.done.wait(5)
    assert server.outcome == "expired" and [e["event"] for e in events] == ["interrupted"]


def test_stopping_the_offer_cuts_off_an_active_transfer_and_never_counts_it_served(timed_transfer, monkeypatch):
    """Ctrl-C while a pickup is inside `_send` (GitHub Codex review of #124, P2): `stop()` used to close only the
    listening socket, and the daemon worker went on sending private bytes and could still report "served" after the
    CLI said the offer was stopped. Now `stop()` marks the offer closed and "stopped", shuts the active connection
    down, and lets the transfer report itself first. CONTROL: the fast transfer in the test above is "served"."""
    server, events, clock, connection = timed_transfer
    entered, release = threading.Event(), threading.Event()
    real_send = server._send

    def paused_send(handler):
        entered.set()
        assert release.wait(5)
        real_send(handler)

    monkeypatch.setattr(server, "_send", paused_send)
    conn = connection(0.25)
    pickup = threading.Thread(target=server.process_request_thread, args=(conn, ("127.0.0.1", 12345)), daemon=True)
    try:
        pickup.start()
        assert entered.wait(5) and server.transfer.locked()
        threading.Timer(0.2, release.set).start()       # the transfer resumes while stop() waits for it to unwind
        server.stop()
        assert server.outcome == "stopped" and server.closed
        assert conn.closed_at is not None, "stop left the active connection open"
    finally:
        release.set()
        pickup.join(timeout=5)
    assert server.outcome == "stopped"
    assert [e["event"] for e in events] == ["interrupted"]
    assert "stopped mid-transfer; not counted as served" in events[0]["detail"]
    assert b"".join(conn.parts[1:]) != b"x" * 64, "the whole file went out after the stop"


def test_an_interrupted_transfer_inside_the_window_does_not_spend_the_offer(timed_transfer, monkeypatch):
    server, events, clock, connection = timed_transfer
    conn = connection(0.25)
    real_sendall = conn.sendall

    def disconnect(chunk):
        if len(conn.parts) == 2:                       # headers and one payload chunk got through
            raise OSError("the recipient disconnected")
        real_sendall(chunk)

    monkeypatch.setattr(conn, "sendall", disconnect)
    server.process_request_thread(conn, ("127.0.0.1", 12345))
    assert server.outcome is None and not server.done.is_set() and clock["now"] < server.deadline
    assert [e["event"] for e in events] == ["interrupted"]
    retry = connection(0.25)                           # another valid pickup, with a fresh nonce
    server.process_request_thread(retry, ("127.0.0.1", 12345))
    assert b"".join(retry.parts[1:]) == b"x" * 64
    assert server.wait() == server.outcome == "served"
    assert [e["event"] for e in events] == ["interrupted", "served"]


def _rewrite(f: Path) -> None:
    f.write_bytes(DATA[:-1] + b"X")                                                         # same size, other bytes


def _replace(f: Path) -> None:
    f.unlink()
    f.write_bytes(b"another file under the same name")


@pytest.mark.parametrize("change", [_rewrite, _replace, Path.unlink], ids=["rewritten", "replaced", "removed"])
def test_the_snapshot_is_served_whatever_happens_to_the_file_after_the_offer(serve, bob, monkeypatch, change):
    """The bytes served are the private snapshot copied before the offer, never FILE read again. Rehashing FILE before
    and during the GET would notice a change only after the changed bytes had crossed the LAN; here FILE is changed
    after the offer and can no longer even be opened, and what is served is still exactly what the line's sha256
    names."""
    server, events, info = serve()
    change(info["file"])

    def never(*a, **kw):
        raise AssertionError("the offered file was opened again after its snapshot was taken")
    monkeypatch.setattr(offer, "open_regular", never)
    status, body = _request(server, "/" + info["token"], _signed(bob[0], info))
    assert status == 200 and body == DATA and hashlib.sha256(body).hexdigest() == info["meta"]["sha256"]
    assert server.done.wait(timeout=5)
    assert server.outcome == "served" and [e["event"] for e in events] == ["served"]


def test_the_snapshot_is_private_and_removed_when_it_is_closed(tmp_path):
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    snapshot = offer.Snapshot(src)
    try:
        assert (snapshot.size, snapshot.sha256) == (len(DATA), hashlib.sha256(DATA).hexdigest())
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(snapshot.dir).st_mode) == 0o700
            assert os.listdir(snapshot.dir) == [], "on POSIX the copy has no name another process could open"
        snapshot.file.seek(0)
        assert snapshot.file.read() == DATA
    finally:
        snapshot.close()
    assert not os.path.exists(snapshot.dir)
    snapshot.close()                                                                        # twice is safe


def test_a_file_that_changes_while_it_is_copied_is_refused(tmp_path, monkeypatch, snapshot_dirs):
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    real_open = offer.open_regular

    def open_then_append(path):
        opened = real_open(path)
        with open(path, "ab") as writer:                    # another process writes while the copy runs
            writer.write(b"more")
        return opened
    monkeypatch.setattr(offer, "open_regular", open_then_append)
    with pytest.raises(node.Refused, match="changed while it was being copied"):
        offer.Snapshot(src)
    assert len(snapshot_dirs) == 1 and not os.path.exists(snapshot_dirs[0]), "the half copy is left behind"
    monkeypatch.setattr(offer, "open_regular", real_open)
    offer.Snapshot(src).close()                                                             # control: a still file


def test_a_link_is_never_offered(tmp_path):
    real = tmp_path / "real.bin"
    real.write_bytes(DATA)
    link = tmp_path / "link.bin"
    _symlink_or_skip(real, link)
    with pytest.raises(node.Refused, match="not a regular file"):
        offer.open_regular(link)
    with pytest.raises(node.Refused):
        offer.open_regular(tmp_path)
    fh, _st = offer.open_regular(real)                                                      # control
    fh.close()


# ------------------------------------------------------------------------------------------------ the bind address

@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "::ffff:0.0.0.0"])
def test_a_wildcard_bind_is_refused_without_the_flag(bind):
    with pytest.raises(node.Refused, match="every interface"):
        offer.check_bind(bind)
    with pytest.raises(node.Refused, match="--advertise"):
        offer.check_bind(bind, any_interface=True)
    same_family = "192.168.50.10" if ":" not in bind else "fd00::36"      # the listener's family follows --bind
    assert offer.check_bind(bind, any_interface=True, advertise=same_family)[1] == same_family


def test_a_lan_address_is_accepted_and_a_public_one_or_a_name_is_not():
    assert offer.check_bind("192.168.50.10") == ("192.168.50.10", "192.168.50.10")
    assert offer.check_bind("127.0.0.1") == ("127.0.0.1", "127.0.0.1")
    assert offer.check_bind("fd00::5") == ("fd00::5", "fd00::5")
    with pytest.raises(node.Refused, match="public address"):
        offer.check_bind("8.8.8.8")
    assert offer.check_bind("8.8.8.8", any_interface=True)[0] == "8.8.8.8"
    for name in ("localhost", "0", "", "192.168.50.10 "):
        with pytest.raises(node.Refused, match="not an IP address"):
            offer.check_bind(name)
    with pytest.raises(node.Refused, match="--advertise is for a wildcard"):
        offer.check_bind("192.168.50.10", advertise="192.168.7.37")


# ------------------------------------------------------------------------------------------------ the body and the line

def _meta(**over) -> dict:
    meta = {"name": "fw.bin", "size_bytes": 12, "sha256": "a" * 64, "url": "http://192.168.50.10:40123/" + "T" * 43,
            "expires": FAR, "for_peer": RECIPIENT}
    meta.update(over)
    return meta


def test_the_offer_body_is_not_refused_by_the_local_path_guard():
    """It carries an http URL, which a reader can open from anywhere, not a path on the sender's disk."""
    body = offer.offer_body(_meta())
    assert docshare.local_paths(body) == []
    docshare.refuse_local_paths(body, node.Refused)
    docshare.refuse_local_paths(offer.receipt_body("fw.bin", 12, "a" * 64), node.Refused)
    with pytest.raises(node.Refused):                                                       # control: the guard is live
        docshare.refuse_local_paths(body + "\nsaved at /Users/someone/fw.bin", node.Refused)


def test_the_body_carries_exactly_one_compact_line_that_parses_back():
    body = offer.offer_body(_meta(name="f\u00efrmware v2.bin"))
    lines = [ln for ln in body.split("\n") if ln.startswith(offer.OFFER_PREFIX)]
    assert len(lines) == 1 and lines[0].isascii() and ", " not in lines[0]
    meta, url, token = offer.parse_offer(body)
    assert meta == _meta(name="f\u00efrmware v2.bin") and url == _meta()["url"] and token == "T" * 43


def _code(body) -> str:
    with pytest.raises(offer.PickupFailed) as err:
        offer.parse_offer(body)
    return err.value.error["error"]


def test_pickup_parses_exactly_one_offer_line():
    line = offer.offer_line(_meta())
    assert _code("no offer here") == offer.E_OFFER_NOT_FOUND
    assert _code(None) == offer.E_OFFER_NOT_FOUND
    assert _code("> " + line) == offer.E_OFFER_NOT_FOUND, "a quoted line is not an offer"
    assert _code(line + "\n" + offer.offer_line(_meta(name="other.bin"))) == offer.E_OFFER_AMBIGUOUS
    assert _code(line + "\n" + line) == offer.E_OFFER_AMBIGUOUS
    assert offer.parse_offer("intro\r\n" + line + "\r\nafter")[0]["name"] == "fw.bin"       # control


@pytest.mark.parametrize("raw", [
    "{not json", "[]", '{"name":"fw.bin"}',
    json.dumps(dict(_meta(), extra=1)),
    '{"name":"a","name":"fw.bin","size_bytes":12,"sha256":"' + "a" * 64 + '","url":"http://1.2.3.4:5/' + "T" * 43
    + '","expires":"' + FAR + '","for_peer":"' + RECIPIENT + '"}',
    json.dumps(_meta(size_bytes=True)), json.dumps(_meta(size_bytes=-1)), json.dumps(_meta(size_bytes="12")),
    json.dumps(_meta(sha256="A" * 64)), json.dumps(_meta(expires="tomorrow")), json.dumps(_meta(for_peer="bob")),
    json.dumps(_meta(url="file:///etc/hosts")), json.dumps(_meta(url="https://192.168.50.10:1/" + "T" * 43)),
    json.dumps(_meta(url="http://fileserver.lan:80/" + "T" * 43)), json.dumps(_meta(url="http://192.168.50.10/" + "T" * 43)),
    json.dumps(_meta(url="http://u:p@192.168.50.10:1/" + "T" * 43)), json.dumps(_meta(url="http://192.168.50.10:1/short")),
    json.dumps(_meta(url="http://192.168.50.10:1/" + "T" * 43 + "?q=1")),
], ids=lambda r: r[:60])
def test_a_malformed_line_is_refused(raw):
    assert _code("x\n" + offer.OFFER_PREFIX + raw) == offer.E_OFFER_MALFORMED


BAD_NAMES = ["", ".", "..", ".envrc", ".git", "a/b", "../fw.bin", "a\\b", "a:b", "a\x00b", "fw\n.bin", "a\x7fb",
             "\u202enib.exe", "x" * 256]


@pytest.mark.parametrize("name", BAD_NAMES, ids=repr)
def test_a_name_that_could_leave_the_folder_or_mislead_is_refused(name):
    assert offer.name_problem(name) is not None
    assert _code(offer.offer_line(_meta(name=name))) == offer.E_OFFER_NAME_REFUSED
    assert offer.name_problem("fw-v2.bin") is None and offer.name_problem("calibration.2026.json") is None  # control


# ------------------------------------------------------------------------------------------------ pickup against a server

def _message(meta: dict, *, verified: bool = True, body: str | None = None, signed_by: str = OFFERER) -> dict:
    return {"key": "msg/1-alice-abc123", "from": "alice", "thread_id": "offer/x", "type": "offer",
            "body": offer.offer_body(meta) if body is None else body, "signature_verified": verified,
            "signed_by": signed_by if verified else None}


def _pickup_code(message, state, out_dir) -> str:
    with pytest.raises(offer.PickupFailed) as err:
        offer.pickup(message, state, out_dir=out_dir, timeout=5)
    return err.value.error["error"]


def test_pickup_verifies_the_sha256_and_removes_what_it_fetched(serve, bob, tmp_path):
    server, _events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    wrong = dict(info["meta"], sha256=hashlib.sha256(b"something else").hexdigest())
    assert _pickup_code(_message(wrong), bob[0], out) == offer.E_OFFER_HASH_MISMATCH
    assert list(out.iterdir()) == [], "neither the temporary file nor the name may be left"
    assert server.done.wait(timeout=5)
    assert server.outcome == "served", "the bytes did cross: the refusal is the pickup's, after the fetch"


def test_pickup_verifies_the_size(serve, bob, tmp_path):
    for claimed in (len(DATA) + 5, len(DATA) - 5):
        server, _events, info = serve()
        out = tmp_path / f"out{claimed}"
        out.mkdir()
        assert _pickup_code(_message(dict(info["meta"], size_bytes=claimed)), bob[0], out) == offer.E_OFFER_SIZE_MISMATCH
        assert list(out.iterdir()) == []


def test_pickup_saves_the_verified_file_under_its_name(serve, bob, tmp_path):
    server, events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    got = offer.pickup(_message(info["meta"]), bob[0], out_dir=out, timeout=5)
    assert (out / "fw.bin").read_bytes() == DATA and [p.name for p in out.iterdir()] == ["fw.bin"]
    assert got["sha256_verified"] is True and got["sha256"] == info["meta"]["sha256"] and got["size_bytes"] == len(DATA)
    assert got["signed_by"] == OFFERER and got["offered_from"] == "127.0.0.1"
    assert server.done.wait(timeout=5)
    assert server.outcome == "served" and [e["event"] for e in events] == ["served"]


def test_pickup_never_overwrites_and_does_not_contact_the_offer(serve, bob, tmp_path):
    server, events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    (out / "fw.bin").write_bytes(b"mine")
    assert _pickup_code(_message(info["meta"]), bob[0], out) == offer.E_OFFER_EXISTS
    assert (out / "fw.bin").read_bytes() == b"mine" and [p.name for p in out.iterdir()] == ["fw.bin"]
    assert events == [] and server.outcome is None, "the offer was contacted, and could have been spent"


def test_a_name_that_appears_during_the_fetch_is_not_overwritten(serve, bob, tmp_path, monkeypatch):
    server, _events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    real_fetch = offer._fetch

    def fetch_then_race(*a, **kw):
        result = real_fetch(*a, **kw)
        (out / "fw.bin").write_bytes(b"arrived meanwhile")
        return result
    monkeypatch.setattr(offer, "_fetch", fetch_then_race)
    assert _pickup_code(_message(info["meta"]), bob[0], out) == offer.E_OFFER_EXISTS
    assert (out / "fw.bin").read_bytes() == b"arrived meanwhile" and [p.name for p in out.iterdir()] == ["fw.bin"]


def _refuse_links(monkeypatch, code: int, *, then=None) -> None:
    """os.link refused with errno `code`, as on a filesystem without hard links (EPERM, ENOTSUP) or a disk that says no;
    `then(dst)` runs first."""
    def link(src, dst, **kw):
        if then is not None:
            then(dst)
        raise OSError(code, os.strerror(code), str(dst))
    monkeypatch.setattr(offer.os, "link", link)


def test_without_hard_links_a_name_that_appears_meanwhile_is_still_not_overwritten(serve, bob, tmp_path, monkeypatch):
    """--out without hard links (FAT, exFAT): the verified file is renamed into place by a rename that refuses an
    existing name in the same step. A check of the name and then a plain rename replaces a file that appears between the
    two; here the name appears while the link is refused, and every check made after the fetch answers as it would have
    just before."""
    server, _events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    target = out / "fw.bin"
    fetched, real_fetch, real_lexists = [], offer._fetch, os.path.lexists

    def fetch(*a, **kw):
        result = real_fetch(*a, **kw)
        fetched.append(True)
        return result

    def lexists(path):
        return False if fetched and Path(path) == target else real_lexists(path)
    monkeypatch.setattr(offer, "_fetch", fetch)
    monkeypatch.setattr(offer.os.path, "lexists", lexists)
    _refuse_links(monkeypatch, errno.EPERM, then=lambda dst: Path(dst).write_bytes(b"arrived meanwhile"))
    assert _pickup_code(_message(info["meta"]), bob[0], out) == offer.E_OFFER_EXISTS
    assert target.read_bytes() == b"arrived meanwhile" and [p.name for p in out.iterdir()] == ["fw.bin"]


def test_the_no_replace_rename_of_this_platform_refuses_an_existing_name(tmp_path):
    """The real call (renamex_np RENAME_EXCL, renameat2 RENAME_NOREPLACE, or Windows' os.rename) on this disk."""
    rename = offer._noreplace_rename()
    if rename is None:
        pytest.skip(f"{sys.platform} has no rename that refuses an existing name; pickup fails typed there")
    src, dst = tmp_path / "new.part", tmp_path / "fw.bin"
    src.write_bytes(b"fetched")
    dst.write_bytes(b"mine")
    with pytest.raises(FileExistsError):
        rename(str(src), dst)
    assert dst.read_bytes() == b"mine" and src.read_bytes() == b"fetched"
    dst.unlink()
    rename(str(src), dst)                                                                   # control
    assert dst.read_bytes() == b"fetched" and not src.exists()


def _flag_refused(src, dst):
    raise OSError(errno.EINVAL, "Invalid argument", str(dst))       # renameat2 on a filesystem without RENAME_NOREPLACE


@pytest.mark.parametrize("rename", [None, _flag_refused], ids=["no-such-call", "flag-refused"])
def test_with_neither_hard_links_nor_a_no_replace_rename_the_file_gets_no_name(serve, bob, tmp_path, monkeypatch,
                                                                               rename):
    server, _events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    _refuse_links(monkeypatch, errno.EPERM)
    monkeypatch.setattr(offer, "_noreplace_rename", lambda: rename)
    assert _pickup_code(_message(info["meta"]), bob[0], out) == offer.E_OFFER_PLACE_UNSUPPORTED
    assert list(out.iterdir()) == [], "neither the name nor the temporary file may be left"


def test_a_link_refused_for_another_reason_is_a_write_error_and_nothing_is_renamed(serve, bob, tmp_path, monkeypatch):
    """Only a filesystem without hard links falls back to the rename; a disk that refuses the link (EACCES here) has
    refused the write."""
    server, _events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    _refuse_links(monkeypatch, errno.EACCES)
    assert _pickup_code(_message(info["meta"]), bob[0], out) == offer.E_OFFER_WRITE
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("name", ["..", "a/b", "a\\b", "a:b", "fw\n.bin"], ids=repr)
def test_pickup_refuses_a_bad_name_before_contacting_the_offer(serve, bob, tmp_path, name):
    server, events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    assert _pickup_code(_message(dict(info["meta"], name=name)), bob[0], out) == offer.E_OFFER_NAME_REFUSED
    assert list(out.iterdir()) == [] and events == []


WINDOWS_REFUSED = [
    *(f"fw{ch}.bin" for ch in '<>"|?*'), "fw\x1f.bin",                         # Win32 error 123; a control character
    "CON", "con.txt", "Prn.tar.gz", "aux", "NUL.bin", "CON .txt", "COM0", "COM1", "com9.bin", "LPT0.bin", "LPT1",
    "CONIN$", "conin$.txt", "CONOUT$", "conout$.txt",
    "lpt9.txt", "COM\u00b9", "lpt\u00b2.bin", "COM\u00b3.txt",                   # a device name, any case, any extension
    "fw.", "fw.bin.", "fw ", "fw.bin ",                                            # Windows drops a trailing dot or space
]


@pytest.mark.parametrize("name", WINDOWS_REFUSED, ids=repr)
def test_a_name_windows_cannot_save_is_refused_on_every_platform_before_the_offer_is_spent(serve, bob, tmp_path,
                                                                                           monkeypatch, name):
    """THE FAILURE (review, 2026-10-05): `fw?.bin` passed name_problem, which is fine on Linux and macOS, and a Windows
    pickup fetched it (spending the single-use offer), then failed to give it its name (Win32 error 123) and removed the
    copy: the file was lost and the offer gone. Now a name Windows cannot save is refused everywhere, before the fetch:
    nothing is fetched, the offer sees no request, and the CONTROL that follows, the same offer under its own name, is
    still served."""
    server, events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    fetched, real_fetch = [], offer._fetch

    def fetch(*a, **kw):
        fetched.append(a[0])
        return real_fetch(*a, **kw)
    monkeypatch.setattr(offer, "_fetch", fetch)
    assert offer.name_problem(name) is not None
    assert _pickup_code(_message(dict(info["meta"], name=name)), bob[0], out) == offer.E_OFFER_NAME_REFUSED
    assert fetched == [] and events == [] and server.outcome is None, "the offer was contacted, and could have been spent"
    assert list(out.iterdir()) == []
    got = offer.pickup(_message(info["meta"]), bob[0], out_dir=out, timeout=5)               # control: still open
    assert got["sha256_verified"] is True and (out / "fw.bin").read_bytes() == DATA and len(fetched) == 1
    assert server.done.wait(timeout=5) and server.outcome == "served"


@pytest.mark.parametrize("name", ["fw.bin", "CONSOLE.txt", "CONINPUT.txt", "CONOUT-report.txt", "com10.bin", "lpt.txt",
                                  "auxiliary", "nul-report.json", "COM1-notes", "fw .bin", "fw v2.bin"], ids=repr)
def test_a_name_that_only_resembles_a_windows_device_or_holds_an_inner_space_is_accepted(name):
    """The control of the test above: the Windows rules refuse a device name and a trailing dot or space, not every name
    that starts like one or has a space inside it."""
    assert offer.name_problem(name) is None


def test_pickup_refuses_an_unverified_message_or_another_peers_offer_before_contacting_it(serve, bob, alice, tmp_path):
    server, events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    assert _pickup_code(_message(info["meta"], verified=False), bob[0], out) == offer.E_OFFER_UNVERIFIED
    assert _pickup_code(_message(info["meta"]), alice[0], out) == offer.E_OFFER_NOT_FOR_THIS_PEER
    assert _pickup_code(_message(info["meta"], body="hello"), bob[0], out) == offer.E_OFFER_NOT_FOUND
    assert _pickup_code(dict(_message(info["meta"]), signed_by=None), bob[0], out) == offer.E_OFFER_UNVERIFIED, \
        "a pickup is signed for the seat that offered: with no signer to name it is not made"
    assert events == [] and list(out.iterdir()) == []


def test_a_folder_that_cannot_be_written_is_a_typed_error_before_contacting_the_offer(serve, bob, tmp_path, monkeypatch):
    server, events, info = serve()
    out = tmp_path / "out"
    out.mkdir()

    def refused(*a, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(offer.tempfile, "mkstemp", refused)
    assert _pickup_code(_message(info["meta"]), bob[0], out) == offer.E_OFFER_WRITE
    assert events == [] and server.outcome is None


class _Decoy(ThreadingHTTPServer):
    """A malicious seat's own offer server on another loopback port: it keeps the pickup headers of every request it is
    sent and answers 404, so its owner can replay them wherever it likes."""
    daemon_threads = True

    def __init__(self):
        self.captured: list[dict] = []
        decoy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                decoy.captured.append({h: self.headers[h] for h in (offer.PEER_HEADER, offer.KID_HEADER,
                                                                    offer.NONCE_HEADER, offer.SIGNATURE_HEADER)})
                self.send_error(404)

            def log_message(self, *a):
                pass
        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def test_a_pickup_signature_a_decoy_offer_captured_does_not_open_the_original(serve, bob, tmp_path):
    """THE FAILURE (Codex, round 5): a pickup signed only {expires, nonce, peer_id, purpose, token}. Mallory, a seat that
    can read alice's offer to bob, published an offer of her own, validly signed by her key, reusing alice's token,
    expires and for_peer with a url at her own server. Bob ran pickup on it while alice's offer was still open, his node
    signed the request and sent it to mallory, and mallory replayed those headers unchanged to alice's server, which
    verified them and served bob's private file to mallory. Now the signature also binds the offer line's url and the
    peer that signed the offer message, and alice's server verifies both against its own: the replay is a 404, it does
    not spend the offer, and bob's own pickup of the real offer is still served."""
    server, events, info = serve()                                                          # alice's offer to bob
    decoy = _Decoy()
    try:
        decoy_meta = dict(info["meta"], url=f"http://127.0.0.1:{decoy.server_address[1]}/{info['token']}")
        assert (decoy_meta["url"] != info["meta"]["url"] and decoy_meta["for_peer"] == RECIPIENT
                and decoy_meta["expires"] == info["meta"]["expires"])
        out = tmp_path / "out"
        out.mkdir()
        assert _pickup_code(_message(decoy_meta, signed_by=MALLORY), bob[0], out) == offer.E_OFFER_FETCH
        assert len(decoy.captured) == 1 and all(decoy.captured[0].values()), "bob's pickup never reached the decoy"
    finally:
        decoy.stop()
    status, body = _request(server, "/" + info["token"], decoy.captured[0])                 # the relay
    assert (status, body) == (404, b"404 Not Found\n"), "alice's server served bob's file to a relayed signature"
    assert server.outcome is None and not server.done.is_set()
    assert len(_refusals(events)) == 1 and _refusals(events)[0].startswith("the signature does not verify")
    assert _request(server, "/" + info["token"], _signed(bob[0], info))[0] == 200           # control: the real pickup
    assert server.done.wait(timeout=5) and server.outcome == "served"


def test_a_spent_offer_is_a_typed_fetch_error(serve, bob, tmp_path):
    server, _events, info = serve()
    for n in (1, 2):
        (tmp_path / f"o{n}").mkdir()
    offer.pickup(_message(info["meta"]), bob[0], out_dir=tmp_path / "o1", timeout=5)
    assert server.done.wait(timeout=5)
    with pytest.raises(offer.PickupFailed) as err:
        offer.pickup(_message(info["meta"]), bob[0], out_dir=tmp_path / "o2", timeout=5)
    assert err.value.error["error"] == offer.E_OFFER_FETCH and err.value.error["http_status"] == 404
    assert list((tmp_path / "o2").iterdir()) == []


# ------------------------------------------------------------------------------------------------ through the CLI

@pytest.fixture()
def world(monkeypatch, tmp_path, alice, bob):
    """Two seats, alice (offers) and bob (picks up), with every call to the mesh server replaced: load_state by state file
    name, author_keys answers bob's published key, send records and answers `w["send"]`, read answers `w["messages"]`."""
    states = {"alice.json": alice[0], "bob.json": bob[0]}
    for name in states:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    w = {"sent": [], "messages": {}, "send": None, "offered": threading.Event(), "dir": tmp_path}

    def send(path, state, **kw):
        w["sent"].append(kw)
        if kw.get("msg_type") == offer.OFFER_TYPE:
            w["offered"].set()
        if w["send"] is not None:
            return w["send"](kw)
        return {"status": "completed", "key": f"msg/{len(w['sent'])}-{state['label']}-abc123"}

    def read(path, state, *, key, **kw):
        return w["messages"][key]

    monkeypatch.setattr(node, "load_state", lambda path, base_url: states[Path(path).name])
    monkeypatch.setattr(node, "author_keys", lambda base, peer: (bob[1], None) if peer == RECIPIENT
                        else (None, f"GET /agents/{peer}/jwks.json answered 404"))
    monkeypatch.setattr(node, "send", send)
    monkeypatch.setattr(node, "read", read)
    monkeypatch.setattr(node, "succeeded", lambda r: r.get("status") == "completed")
    return w


def _offer_argv(w, file: Path, *extra) -> list[str]:
    return ["offer", "--base-url", BASE, "--state-file", str(w["dir"] / "alice.json"), str(file), "--to", "bob",
            "--to-peer", RECIPIENT, "--bind", "127.0.0.1", *extra]


def _port(body: str) -> int:
    """The port of the URL an offer body carries."""
    return int(offer.parse_offer(body)[1].rsplit(":", 1)[1].split("/", 1)[0])


def test_offer_then_pickup_with_a_receipt_end_to_end(world, tmp_path, capsys, alice):
    src = tmp_path / "calibration.bin"
    src.write_bytes(DATA)
    out = tmp_path / "out"
    out.mkdir()
    events, result = io.StringIO(), {}
    args = cli.build_parser().parse_args(_offer_argv(world, src, "--ttl", "30"))
    t = threading.Thread(target=lambda: result.setdefault("rc", offer.run_offer(args, alice[0], out=events)),
                         daemon=True)
    t.start()
    assert world["offered"].wait(10), "the offer message was never sent"
    sent = world["sent"][0]
    assert sent["to"] == ["bob"] and sent["thread_id"] == "offer/" + hashlib.sha256(DATA).hexdigest()[:12]
    assert "requires_ack" not in sent, "offers are deliveries, not tracked asks (#198 item 2)"
    key = "msg/1-alice-abc123"
    world["messages"][key] = {"key": key, "from": "alice", "thread_id": sent["thread_id"], "body": sent["body"],
                              "signature_verified": True, "signed_by": OFFERER}
    rc = cli.main(["pickup", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", key,
                   "--out", str(out), "--receipt"])
    summary = json.loads(capsys.readouterr().out)
    t.join(10)
    assert rc == 0 and result.get("rc") == 0, (rc, result, summary, events.getvalue())
    assert (out / "calibration.bin").read_bytes() == DATA
    assert summary["picked_up"] is True and summary["sha256_verified"] is True and summary["receipt"]["sent"] is True
    assert "warning" not in summary["receipt"], "the ledger was writable: nothing to warn about"
    receipt = world["sent"][1]
    assert receipt["in_reply_to"] == key and receipt["thread_id"] == sent["thread_id"] and receipt["to"] == ["alice"]
    assert receipt["msg_type"] == "ack" and "\n" not in receipt["body"]
    assert receipt["body"] == f"picked up calibration.bin: {len(DATA)} bytes, sha256 {hashlib.sha256(DATA).hexdigest()} verified"
    lines = [json.loads(ln) for ln in events.getvalue().splitlines()]
    assert [e["event"] for e in lines] == ["offered", "served"]
    assert lines[1]["to_address"] == "127.0.0.1"
    token = offer.parse_offer(sent["body"])[2]
    assert token not in events.getvalue(), "the offer's own output never prints the token"


def test_ttl_expiry_exits_non_zero(world, tmp_path, capsys):
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    started = time.monotonic()
    assert cli.main(_offer_argv(world, src, "--ttl", "1")) == 1
    assert time.monotonic() - started < 10
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [e["event"] for e in lines] == ["offered", "expired"]
    with pytest.raises(OSError):                                                             # the socket is closed
        socket.create_connection(("127.0.0.1", _port(world["sent"][0]["body"])), 1)


def _offer_in_background(world, alice, src: Path, *extra) -> tuple[threading.Thread, dict, io.StringIO, dict]:
    """(the thread running `offer`, {"rc": its exit code once done}, its event lines, the offer message as bob's `read`
    returns it). Returns as soon as the offer message is handed to the send, before the send answers."""
    events, result = io.StringIO(), {}
    args = cli.build_parser().parse_args(_offer_argv(world, src, *extra))
    t = threading.Thread(target=lambda: result.setdefault("rc", offer.run_offer(args, alice[0], out=events)),
                         daemon=True)
    t.start()
    assert world["offered"].wait(10), "the offer message was never sent"
    sent = world["sent"][0]
    message = {"key": "msg/1-alice-abc123", "from": "alice", "thread_id": sent["thread_id"], "body": sent["body"],
               "signature_verified": True, "signed_by": OFFERER}
    return t, result, events, message


def test_a_ledger_that_cannot_be_written_after_the_send_is_a_warning_and_the_offer_is_served(
        world, tmp_path, alice, bob, monkeypatch, snapshot_dirs):
    """Once the server accepted the offer message the recipient holds it, so a send ledger that cannot be written (a
    full disk) must not close the socket under it: a `warning` line, after the `offered` line that names the key."""
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    out = tmp_path / "out"
    out.mkdir()

    def full_disk(*a, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(offer.sendguard, "record", full_disk)
    t, result, events, message = _offer_in_background(world, alice, src)
    got = offer.pickup(message, bob[0], out_dir=out, timeout=5)
    t.join(10)
    assert result.get("rc") == 0 and got["sha256_verified"] is True, (result, events.getvalue())
    lines = [json.loads(ln) for ln in events.getvalue().splitlines()]
    assert [e["event"] for e in lines] == ["offered", "warning", "served"]
    assert lines[0]["key"] == message["key"] and lines[1]["key"] == message["key"]
    assert "ledger" in lines[1]["detail"] and "No space left" in lines[1]["detail"]
    assert snapshot_dirs and not any(os.path.exists(d) for d in snapshot_dirs), "the snapshot outlived the offer"


def test_through_the_commands_a_relayed_decoy_signature_is_refused_and_the_real_pickup_served(world, tmp_path, alice,
                                                                                                bob, capsys):
    """The same relay end to end: `offer` puts the url its server checks in its line and binds its own peer id, and
    `pickup` signs the url and the `signed_by` of the message it read. Bob picks up mallory's decoy through the CLI, the
    decoy replays what it captured to alice's offer, and alice's offer refuses it, prints the refusal and still serves
    bob's own pickup of the real message once."""
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    out = tmp_path / "out"
    out.mkdir()
    t, result, events, message = _offer_in_background(world, alice, src, "--ttl", "30")
    meta, _url, token = offer.parse_offer(message["body"])
    decoy = _Decoy()
    try:
        decoy_key = "msg/2-mallory-abc123"
        world["messages"][decoy_key] = {
            "key": decoy_key, "from": "mallory", "thread_id": message["thread_id"], "signature_verified": True,
            "signed_by": MALLORY,
            "body": offer.offer_body(dict(meta, url=f"http://127.0.0.1:{decoy.server_address[1]}/{token}"))}
        rc = cli.main(["pickup", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", decoy_key,
                       "--out", str(out)])
        assert rc == 1 and json.loads(capsys.readouterr().out)["error"] == offer.E_OFFER_FETCH
        assert len(decoy.captured) == 1, "bob's pickup never reached the decoy"
    finally:
        decoy.stop()
    assert _request(_port(message["body"]), "/" + token, decoy.captured[0]) == (404, b"404 Not Found\n"), \
        "alice's offer served bob's file to a signature relayed from a decoy"
    got = offer.pickup(message, bob[0], out_dir=out, timeout=10)                             # control: the real one
    t.join(10)
    assert result.get("rc") == 0 and got["sha256_verified"] is True, (result, events.getvalue())
    assert (out / "fw.bin").read_bytes() == DATA
    lines = [json.loads(ln) for ln in events.getvalue().splitlines()]
    assert [e["event"] for e in lines] == ["offered", "refused", "served"], lines
    assert lines[1]["why"].startswith("the signature does not verify") and lines[1]["from"] == "127.0.0.1"


def _slow_send(seconds: float):
    def send(kw):
        time.sleep(seconds)
        return {"status": "completed", "key": "msg/1-alice-abc123"}
    return send


def test_a_send_slower_than_the_ttl_still_leaves_the_whole_window(world, tmp_path, alice, bob):
    """The window is --ttl seconds from the moment pickups are answered, after the send was accepted. A deadline fixed
    before the send (rounded up, at most 2 s for --ttl 1) would have closed during this 2.2 s send, and every pickup
    would have met a 404 from the first moment."""
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    out = tmp_path / "out"
    out.mkdir()
    world["send"] = _slow_send(2.2)
    t, result, events, message = _offer_in_background(world, alice, src, "--ttl", "1")
    got = offer.pickup(message, bob[0], out_dir=out, timeout=10)      # waits on the socket until the offer answers
    t.join(10)
    assert result.get("rc") == 0 and got["size_bytes"] == len(DATA), (result, events.getvalue())
    assert [json.loads(ln)["event"] for ln in events.getvalue().splitlines()] == ["offered", "served"]


@pytest.mark.parametrize("allowance", [None, 0], ids=["default-allowance", "send-slower-than-the-allowance"])
def test_the_expires_the_line_names_is_never_earlier_than_the_window_the_server_keeps(world, tmp_path, monkeypatch, capsys,
                                                                                      allowance):
    """`expires` is an upper bound: with the default allowance the server keeps the whole TTL from the moment it starts
    answering. A send that took longer than the allowance anyway is never served, even one that made no request at all
    (this fake send makes none, so no request of it could be cut): node.finishing_by refuses a block that RETURNS after
    it (Codex, round 4). Before, this send was accepted and the window it opened was cut short at `expires`."""
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    if allowance is not None:
        monkeypatch.setattr(offer, "SEND_ALLOWANCE_S", allowance)
    seen = {}

    class Watched(offer.OfferServer):
        def start(self, announce=None):
            seen["wall"], seen["clock"] = time.time(), time.monotonic()
            started = super().start(announce)
            seen["deadline"] = self.deadline
            return started
    monkeypatch.setattr(offer, "OfferServer", Watched)
    world["send"] = _slow_send(1.2)
    assert cli.main(_offer_argv(world, src, "--ttl", "1")) == 1                             # nobody picked it up
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    if allowance is not None:
        assert [e["event"] for e in lines] == ["not_sent"] and lines[0]["status"] == "unconfirmed", lines
        assert "after the deadline" in lines[0]["error_message"]
        assert seen == {}, "the offer started serving after a send that returned past its allowance"
        return
    assert [e["event"] for e in lines] == ["offered", "expired"], lines
    expires = offer.parse_offer(world["sent"][0]["body"])[0]["expires"]
    named = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    closes = seen["wall"] + (seen["deadline"] - seen["clock"])
    assert closes <= named + 0.01, f"the server keeps the offer {closes - named:.3f} s past the expires it names"
    if allowance is None:
        assert seen["deadline"] - seen["clock"] >= 0.99, "the TTL did not start when pickups became possible"


class _Answer:
    def __init__(self, status: int, body):
        self.status, self.headers, self._raw = status, {}, json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._raw


class _SlowMesh:
    """The mesh server behind a real node.send, on a clock (time.monotonic) that moves only while a request is out. Each
    request takes `step` seconds; one given a shorter socket timeout times out at it, as a socket does, unless `trickle`
    (its answer came a little at a time, each read inside the timeout, the whole after it)."""

    def __init__(self, step: float, *, trickle: bool):
        self.now, self.step, self.trickle, self.requests = 1000.0, step, trickle, []

    def monotonic(self) -> float:
        return self.now

    def open(self, request, timeout):
        path = urllib.parse.urlsplit(request.full_url).path
        self.requests.append({"path": path, "at": self.now, "timeout": timeout})
        if self.step > timeout and not self.trickle:
            self.now += timeout
            raise TimeoutError("timed out")
        self.now += self.step
        if path == "/v1/auth/challenge":
            return _Answer(200, {"nonce": "n" * 24})
        if path == "/v1/auth/sign-in":
            return _Answer(200, {"jwt": "h.p.s", "jwt_expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/invoke"):
            return _Answer(202, {"status": "queued"})
        if path.startswith("/v1/calls/"):
            return _Answer(200, {"status": "completed", "call_id": path.rsplit("/", 1)[1], "result": {"ok": True}})
        return _Answer(200, {"keys": []})                                                   # /.well-known/jwks.json


@pytest.mark.parametrize("step,trickle,accepted", [(50.0, False, False), (50.0, True, False), (15.0, False, True)],
                         ids=["each-answered-inside-its-socket-timeout", "answer-trickles-past-it", "control-in-time"])
def test_a_send_whose_requests_together_outlast_the_allowance_is_never_served(world, tmp_path, alice, monkeypatch, capsys,
                                                                              step, trickle, accepted):
    """THE FAILURE (Codex, round 3): the line's `expires` is fixed before the send, at the TTL plus SEND_ALLOWANCE_S, and
    the send was bounded only per socket operation. Five requests (a sign-in's two, the invoke, a poll, the result's
    JWKS), each answered in 50 s, inside its 60 s socket timeout, took 250 s: the send was accepted, `offered` was
    printed, and the window had closed 150 s before it opened, so --ttl 1 was `expired` at once. Now the whole send is
    cut at the allowance less START_MARGIN_S (node.finishing_by): a send not answered by then is `not_sent` and nothing
    is served, and one answered by then leaves the whole TTL. Deterministic: the clock moves only by the requests."""
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    mesh = _SlowMesh(step, trickle=trickle)
    monkeypatch.setattr(time, "monotonic", mesh.monotonic)
    monkeypatch.setattr(node, "_opener", lambda **kw: mesh)
    monkeypatch.setattr(node, "send", _REAL_SEND)
    alice[0]["memory_peer_id"] = node.DEFAULT_MEMORY_PEER_ID                                # no JWT yet: a sign-in first
    seen = {}

    class Watched(offer.OfferServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["port"] = self.server_address[1]

        def start(self, *a, **kw):
            seen["started"] = time.monotonic()
            started = super().start(*a, **kw)
            seen["window"] = self.deadline - seen["started"]
            return started
    monkeypatch.setattr(offer, "OfferServer", Watched)
    assert cli.main(_offer_argv(world, src, "--ttl", "1")) == 1                             # nobody picks it up
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [e["event"] for e in lines] == (["offered", "expired"] if accepted else ["not_sent"]), lines
    finish_by = 1000.0 + offer.SEND_ALLOWANCE_S - offer.START_MARGIN_S
    if accepted:                                                                            # control: 75 s in all
        assert len(mesh.requests) == 5 and seen["started"] <= finish_by
        assert seen["window"] >= 1, "an accepted send left less than the TTL before the expires its line names"
    else:
        assert lines[0]["status"] == "unconfirmed" and "nothing was served" in lines[0]["detail"]
        assert "started" not in seen, "the offer answered pickups after a send that outlasted its allowance"
        assert not (tmp_path / "alice.json.sendledger").exists(), "a send not seen accepted is not counted"
        with pytest.raises(OSError):                                                         # the socket is closed
            socket.create_connection(("127.0.0.1", seen["port"]), 1)
    assert all(r["at"] < finish_by and r["at"] + r["timeout"] <= finish_by for r in mesh.requests), \
        f"a request was sent, or allowed to run, past the send's deadline: {mesh.requests}"


def test_finishing_by_bounds_each_request_inside_it_and_none_outside(monkeypatch):
    """node.finishing_by, the bound the offer's send runs under: inside it a request's socket timeout is cut to the time
    left and no request is sent once the instant has passed; after the block every request is as before."""
    mesh = _SlowMesh(1.0, trickle=False)
    monkeypatch.setattr(time, "monotonic", mesh.monotonic)
    monkeypatch.setattr(node, "_opener", lambda **kw: mesh)
    with node.finishing_by(1005.0):
        assert node.http_json("GET", BASE, "/.well-known/jwks.json")[0] == 200
        mesh.now = 1005.0
        with pytest.raises(node.DeadlinePassed, match="was not sent"):
            node.http_json("GET", BASE, "/.well-known/jwks.json")
    assert node.http_json("GET", BASE, "/.well-known/jwks.json")[0] == 200                  # control: the block ended
    assert [r["timeout"] for r in mesh.requests] == [5.0, node.HTTP_TIMEOUT_S]


def test_finishing_by_refuses_a_block_that_returns_after_it_and_keeps_its_own_exception(monkeypatch):
    """Every request ending in time is not the call ending in time (Codex, round 4): node.finishing_by also refuses a
    block that RETURNS after its instant, whatever its requests did; a block that ends by it returns as before, and an
    exception the block raises itself is raised as it is, never replaced by DeadlinePassed."""
    mesh = _SlowMesh(1.0, trickle=False)
    monkeypatch.setattr(time, "monotonic", mesh.monotonic)
    monkeypatch.setattr(node, "_opener", lambda **kw: mesh)
    with node.finishing_by(1005.0):                                                          # control: ends AT it
        assert node.http_json("GET", BASE, "/.well-known/jwks.json")[0] == 200
        mesh.now = 1005.0
    with pytest.raises(node.DeadlinePassed, match="returned 0.500 s after the deadline"):
        with node.finishing_by(1010.0):
            assert node.http_json("GET", BASE, "/.well-known/jwks.json")[0] == 200          # answered at 1006: in time
            mesh.now = 1010.5                                                                # the work after it is not
    with pytest.raises(KeyError, match="its own"):
        with node.finishing_by(1011.0):
            mesh.now = 1020.0
            raise KeyError("its own")
    assert node.http_json("GET", BASE, "/.well-known/jwks.json")[0] == 200                  # no bound is left behind


class _SignedMesh(_SlowMesh):
    """_SlowMesh whose call row the server key really signs, beside a JWKS that publishes that key, so node.send's own
    success rule (node.succeeded, the result's signature included) accepts what it answers."""

    def __init__(self, step: float):
        super().__init__(step, trickle=False)
        self.seed = wire.new_seed()

    def open(self, request, timeout):
        answer = super().open(request, timeout)
        path = urllib.parse.urlsplit(request.full_url).path
        if path.startswith("/v1/calls/"):
            row = json.loads(answer.read())
            payload = {"v": 1, "kind": wire.KIND_SKILL_RESULT, "issued_at": "now", "nonce": "n",
                       **{field: row.get(field) for field in wire.RESULT_BOUND_FIELDS}}
            return _Answer(200, {**row, "result_payload": payload, "result_signing_kid": "server-1",
                                 "result_signature": wire.b64u(wire.sign(self.seed, wire.jcs(payload)))})
        if path == "/.well-known/jwks.json":
            return _Answer(200, {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "server-1",
                                           "x": wire.b64u(wire.public_key_of(self.seed))}]})
        return answer


@pytest.mark.parametrize("checked", [3.0, -3.0], ids=["result-check-ends-past-the-allowance", "control-in-time"])
def test_a_send_whose_result_check_outlasts_the_allowance_is_never_served(world, tmp_path, alice, monkeypatch, capsys,
                                                                          checked):
    """THE FAILURE (Codex, round 4): node.finishing_by cut each REQUEST of the send at the allowance, never the send.
    node.invoke checks the result's signature after its last request, so with every request answered in time (the last
    by 1075 s here; the allowance less START_MARGIN_S ends at 1099 s) a check that took the clock 3 s past it returned
    an accepted send late: `offered` was printed after the window had closed, then `expired`, and the expires the
    message names never held a serving window. Now a block that returns late is DeadlinePassed: `not_sent`, and the
    server never starts. The result is genuinely signed and verifies, so nothing but the deadline refuses it; the
    control's check ends 3 s inside the allowance and keeps the whole TTL. Deterministic: the clock moves only by the
    requests and the check."""
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    mesh = _SignedMesh(15.0)
    monkeypatch.setattr(time, "monotonic", mesh.monotonic)
    monkeypatch.setattr(node, "_opener", lambda **kw: mesh)
    monkeypatch.setattr(node, "send", _REAL_SEND)
    monkeypatch.setattr(node, "succeeded", _REAL_SUCCEEDED)
    alice[0]["memory_peer_id"] = node.DEFAULT_MEMORY_PEER_ID                                # no JWT yet: a sign-in first
    finish_by = 1000.0 + offer.SEND_ALLOWANCE_S - offer.START_MARGIN_S
    real_check, checks = wire.result_signature_ok, []

    def slow_check(call, jwks):
        checks.append(real_check(call, jwks))
        mesh.now = finish_by + checked                                                       # the check took this long
        return checks[-1]
    monkeypatch.setattr(wire, "result_signature_ok", slow_check)
    seen = {}

    class Watched(offer.OfferServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["port"] = self.server_address[1]

        def start(self, *a, **kw):
            seen["started"] = time.monotonic()
            started = super().start(*a, **kw)
            seen["window"] = self.deadline - seen["started"]
            return started
    monkeypatch.setattr(offer, "OfferServer", Watched)
    assert cli.main(_offer_argv(world, src, "--ttl", "1")) == 1                             # nobody picks it up
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert checks == [(True, "ok")], "the result must verify, so that only the deadline can refuse the send"
    assert len(mesh.requests) == 5 and mesh.requests[-1]["at"] + mesh.step <= finish_by - 20, mesh.requests
    if checked < 0:                                                                          # control
        assert [e["event"] for e in lines] == ["offered", "expired"], lines
        assert seen["window"] >= 1, "an accepted send left less than the TTL before the expires its line names"
        return
    assert [e["event"] for e in lines] == ["not_sent"], lines
    assert lines[0]["status"] == "unconfirmed" and "after the deadline" in lines[0]["error_message"]
    assert "started" not in seen, "the offer started serving after a send that returned past its allowance"
    assert not (tmp_path / "alice.json.sendledger").exists(), "a send not seen accepted is not counted"
    with pytest.raises(OSError):                                                             # the socket is closed
        socket.create_connection(("127.0.0.1", seen["port"]), 1)


@pytest.mark.parametrize("pause,served", [(2.5, False), (offer.START_MARGIN_S, True)],
                         ids=["paused-past-the-margin", "control-paused-within-it"])
def test_serving_starts_only_while_the_whole_ttl_still_fits_before_expires(world, tmp_path, alice, bob, monkeypatch,
                                                                           pause, served):
    """The other half of the same failure (Codex, round 4): after the send answered, the offer runs local steps before
    serving, and START_MARGIN_S is all the allowance leaves them. A process paused for longer (stopped, swapping) used to
    print `offered` and open a window cut at `expires`, shorter than the TTL or already closed. Now OfferServer.start
    checks, as it starts, that the whole TTL still fits before `expires`: when it does not, nothing is ever answered,
    and the accepted offer is `expired` with no `offered` line before it (its send counted: the server accepted it). The
    send ends exactly at its deadline, and the clock (time.monotonic) moves only by the send and the pause."""
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    out = tmp_path / "out"
    out.mkdir()
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    finish_by = 1000.0 + offer.SEND_ALLOWANCE_S - offer.START_MARGIN_S

    def send(kw):
        clock["now"] = finish_by                                                             # in time, just
        return {"status": "completed", "key": "msg/1-alice-abc123"}

    def succeeded(result):
        clock["now"] += pause                                                                # the process pauses here
        return result.get("status") == "completed"
    world["send"] = send
    monkeypatch.setattr(node, "succeeded", succeeded)
    t, result, events, message = _offer_in_background(world, alice, src, "--ttl", "5")
    if served:                                                                               # control
        got = offer.pickup(message, bob[0], out_dir=out, timeout=10)
        t.join(10)
        assert result.get("rc") == 0 and got["sha256_verified"] is True, (result, events.getvalue())
        assert [json.loads(ln)["event"] for ln in events.getvalue().splitlines()] == ["offered", "served"]
        return
    t.join(10)
    lines = [json.loads(ln) for ln in events.getvalue().splitlines()]
    assert result.get("rc") == 1 and [e["event"] for e in lines] == ["expired"], (result, lines)
    assert lines[0]["key"] == message["key"] and "nothing was served" in lines[0]["detail"]
    with pytest.raises(OSError):                                                             # never answered, now closed
        socket.create_connection(("127.0.0.1", _port(message["body"])), 1)
    assert (tmp_path / "alice.json.sendledger").exists(), "a send the server accepted is counted by the volume guard"


def test_a_send_that_fails_stops_the_server_and_nothing_is_served(world, tmp_path, bob, capsys):
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    seen = {}

    def failing(kw):
        # While the send is in flight the socket exists but nothing on it is answered, signed or not.
        meta, _url, token = offer.parse_offer(kw["body"])
        conn = http.client.HTTPConnection("127.0.0.1", _port(kw["body"]), timeout=0.5)
        try:
            conn.request("GET", "/" + token, headers=offer.pickup_headers(bob[0], url=meta["url"], offered_by=OFFERER,
                                                                          token=token, expires=meta["expires"]))
            seen["answer"] = conn.getresponse().status
        except OSError as exc:
            seen["answer"] = type(exc).__name__
        finally:
            conn.close()
        return {"status": "failed", "error_code": "E_TEST", "error_message": "refused", "key": "msg/x"}
    world["send"] = failing
    assert cli.main(_offer_argv(world, src)) == 1
    assert seen["answer"] in ("TimeoutError", "timeout"), seen
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [e["event"] for e in lines] == ["not_sent"] and lines[0]["error_code"] == "E_TEST"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", _port(world["sent"][0]["body"])), 1)
    assert not (tmp_path / "alice.json.sendledger").exists(), "a send the server did not accept is not counted"


def test_a_send_that_raises_stops_the_server_too(world, tmp_path, capsys):
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)

    def unreachable(kw):
        raise ConnectionRefusedError("the mesh server is down")
    world["send"] = unreachable
    assert cli.main(_offer_argv(world, src)) == 2
    assert capsys.readouterr().out == ""
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", _port(world["sent"][0]["body"])), 1)


@pytest.mark.parametrize("bind", ["0.0.0.0", "::"])
def test_offer_refuses_a_wildcard_bind_without_the_flag_and_sends_nothing(world, tmp_path, capsys, bind):
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    argv = _offer_argv(world, src)
    argv[argv.index("--bind") + 1] = bind
    assert cli.main(argv) == 2 and world["sent"] == []
    assert "every interface" in json.loads(capsys.readouterr().err)["refused"]


@pytest.mark.parametrize("change,why", [
    (lambda argv, f: argv.__setitem__(argv.index("--to-peer") + 1, "bob"), "not a peer id"),
    (lambda argv, f: argv.__setitem__(argv.index("--to-peer") + 1, OFFERER), "publishes no Ed25519 key"),
    (lambda argv, f: argv.__setitem__(argv.index("--to") + 1, "bob,carol"), "exactly one recipient"),
    (lambda argv, f: argv.extend(["--ttl", "0"]), "--ttl"),
    (lambda argv, f: argv.extend(["--ttl", str(offer.MAX_TTL_S + 1)]), "--ttl"),
    (lambda argv, f: argv.__setitem__(argv.index(str(f)), str(f.parent / "missing.bin")), "cannot read"),
    # --ttl 1: were the check gone, the offer would be served, and for 1 s rather than the default hour.
    (lambda argv, f: argv.extend(["--subject", "the firmware", "--ttl", "1"]),
     "--subject 'the firmware' refused: subject must be"),
], ids=["peer-not-a-uuid", "peer-without-a-key", "two-recipients", "ttl-zero", "ttl-too-long", "no-file",
        "free-text-subject"])
def test_offer_refuses_before_anything_is_sent(world, tmp_path, capsys, snapshot_dirs, change, why):
    """MUTANT (backlog item 15): drop run_offer's --subject check; the free-text-subject case reaches send (the
    recorder here stands in for node.send, so wire.mesh_message's own guard is not what refuses it)."""
    src = tmp_path / "fw.bin"
    src.write_bytes(DATA)
    argv = _offer_argv(world, src)
    change(argv, src)
    assert cli.main(argv) == 2 and world["sent"] == []
    assert why in json.loads(capsys.readouterr().err)["refused"]
    assert not any(os.path.exists(d) for d in snapshot_dirs), "a snapshot outlived the refusal"


def test_offer_refuses_a_link_and_a_name_pickup_would_refuse(world, tmp_path, capsys):
    real = tmp_path / "real.bin"
    real.write_bytes(DATA)
    odd = tmp_path / "\u202enib.exe"                        # shows as "exe.bin": a name that misleads
    odd.write_bytes(DATA)
    assert cli.main(_offer_argv(world, odd)) == 2
    link = tmp_path / "link.bin"
    _symlink_or_skip(real, link)
    assert cli.main(_offer_argv(world, link)) == 2
    assert world["sent"] == []
    errs = [json.loads(ln)["refused"] for ln in capsys.readouterr().err.splitlines()]
    assert "control or format character" in errs[0] and "not a regular file" in errs[1]


def test_a_receipt_ledger_that_cannot_be_written_still_reports_the_completed_pickup(world, serve, bob, tmp_path, capsys,
                                                                                    monkeypatch):
    """THE FAILURE (Codex, round 3): the file was verified and in place and the receipt accepted, and then the send ledger
    could not be written (a full disk). sendguard.record raised through run_pickup before its one JSON object was
    printed, cli.main reported `unreachable` and exit 2 with nothing on stdout, and automation could only retry, into an
    existing file and a spent offer. Now the receipt carries a `warning` and the object is the completed pickup's."""
    server, _events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    key = "msg/1-alice-abc123"
    world["messages"][key] = _message(info["meta"])

    def full_disk(*a, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(offer.sendguard, "record", full_disk)
    rc = cli.main(["pickup", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", key,
                   "--out", str(out), "--receipt"])
    printed = capsys.readouterr()
    assert rc == 0 and printed.err == "", printed
    summary = json.loads(printed.out)
    assert summary["picked_up"] is True and summary["sha256_verified"] is True and summary["key"] == key
    assert summary["path"] == os.path.abspath(out / "fw.bin") and summary["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert summary["receipt"]["sent"] is True and summary["receipt"]["in_reply_to"] == key
    assert "ledger" in summary["receipt"]["warning"] and "No space left" in summary["receipt"]["warning"]
    assert (out / "fw.bin").read_bytes() == DATA and server.done.wait(timeout=5) and server.outcome == "served"
    assert [s["msg_type"] for s in world["sent"]] == [offer.RECEIPT_TYPE], "the receipt went once"


def test_pickup_cli_prints_a_typed_error_and_exits_1(world, tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    world["messages"]["msg/9"] = {"key": "msg/9", "from": "alice", "thread_id": "t", "signature_verified": True,
                                  "signed_by": OFFERER,
                                  "body": offer.offer_body(_meta()) + "\n" + offer.offer_line(_meta(name="b.bin"))}
    assert cli.main(["pickup", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", "msg/9",
                     "--out", str(out), "--receipt"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["picked_up"] is False and printed["error"] == offer.E_OFFER_AMBIGUOUS
    assert world["sent"] == [], "no receipt for a pickup that failed"


@pytest.mark.parametrize("bind,advertise,ok", [("0.0.0.0", "fd00::5", False), ("::", "192.168.7.5", False),
                                                ("0.0.0.0", "192.168.7.5", True), ("::", "fd00::5", True)],
                         ids=["v4-wildcard-v6-url", "v6-wildcard-v4-url", "v4-control", "v6-control"])
def test_a_wildcard_bind_advertises_only_an_address_of_its_own_family(bind, advertise, ok):
    """GitHub Codex review of #124 (P2): the listener's family comes from --bind, so a wildcard paired with an address of
    the other family published an accepted offer whose link no connection could ever reach. Now that pair is refused
    before anything is opened or sent; the same-family controls still pass."""
    if ok:
        assert offer.check_bind(bind, any_interface=True, advertise=advertise) == (bind, advertise)
    else:
        with pytest.raises(node.Refused, match="same family"):
            offer.check_bind(bind, any_interface=True, advertise=advertise)


def test_pickup_from_peer_refuses_another_signer_before_contacting_the_offer(serve, bob, tmp_path):
    """--from-peer pins the expected offering peer (`signed_by`) before the offer line is parsed or fetched: a wrong
    signer raises E_OFFER_WRONG_SENDER without contacting the offer, and the positive control (`from_peer=OFFERER`,
    including uppercase UUID normalization) still fetches and verifies the file."""
    server, events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(node.Refused, match="not a peer id"):
        offer.pickup(_message(info["meta"]), bob[0], out_dir=out, timeout=5, from_peer="alice")
    with pytest.raises(offer.PickupFailed) as err:
        offer.pickup(_message(info["meta"], signed_by=MALLORY), bob[0], out_dir=out, timeout=5, from_peer=OFFERER)
    assert err.value.error["error"] == offer.E_OFFER_WRONG_SENDER
    assert err.value.error["signed_by"] == MALLORY and err.value.error["expected_peer"] == OFFERER
    assert events == [] and server.outcome is None and list(out.iterdir()) == [], "the offer was contacted"
    got = offer.pickup(_message(info["meta"]), bob[0], out_dir=out, timeout=5, from_peer=OFFERER.upper())  # control
    assert got["sha256_verified"] is True and (out / "fw.bin").read_bytes() == DATA
    assert server.done.wait(timeout=5) and server.outcome == "served"


def test_pickup_cli_from_peer_validates_uuid_before_read_and_pins_the_offering_peer(world, serve, bob, tmp_path,
                                                                                    capsys, monkeypatch):
    """Through the CLI: a malformed `--from-peer` exits 2 before `node.read` is called; a valid `--from-peer` that does
    not match `signed_by` exits 1 with E_OFFER_WRONG_SENDER without contacting the offer or sending a receipt; and the
    positive control (`--from-peer OFFERER`) exits 0 and serves the file."""
    server, events, info = serve()
    out = tmp_path / "out"
    out.mkdir()
    key = "msg/1-alice-abc123"
    world["messages"][key] = _message(info["meta"])
    reads = []
    real_read = node.read
    monkeypatch.setattr(node, "read", lambda *a, **kw: (reads.append(kw.get("key")), real_read(*a, **kw))[1])

    assert cli.main(["pickup", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", key,
                     "--from-peer", "not-a-uuid", "--out", str(out)]) == 2
    assert reads == [], "a malformed --from-peer must be refused before reading the message"
    assert "not a peer id" in json.loads(capsys.readouterr().err)["refused"]

    assert cli.main(["pickup", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", key,
                     "--from-peer", MALLORY, "--out", str(out), "--receipt"]) == 1
    wrong = json.loads(capsys.readouterr().out)
    assert wrong["picked_up"] is False and wrong["error"] == offer.E_OFFER_WRONG_SENDER
    assert wrong["signed_by"] == OFFERER and wrong["expected_peer"] == MALLORY
    assert events == [] and server.outcome is None and world["sent"] == [] and list(out.iterdir()) == []

    assert cli.main(["pickup", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", key,
                     "--from-peer", OFFERER.upper(), "--out", str(out), "--receipt"]) == 0
    ok = json.loads(capsys.readouterr().out)
    assert ok["picked_up"] is True and ok["sha256_verified"] is True and ok["receipt"]["sent"] is True
    assert (out / "fw.bin").read_bytes() == DATA and server.done.wait(timeout=5) and server.outcome == "served"


def test_seven_offer_pickup_round_trips_in_ten_minutes_pass_while_ordinary_send_pair_trips(
    world, tmp_path, capsys, alice
):
    """7 end-to-end offer + pickup --receipt round trips (including CLI read of each offer and receipt)
    in 10 minutes pass without check_pair_loop refusal, while a control of 6 ordinary sends + 6 replies
    trips at the 7th send."""
    out = tmp_path / "out"
    out.mkdir()
    for i in range(7):
        src = tmp_path / f"fw-{i}.bin"
        src.write_bytes(DATA)
        world["offered"].clear()
        before_len = len(world["sent"])
        events, result = io.StringIO(), {}
        args = cli.build_parser().parse_args(_offer_argv(world, src, "--ttl", "30"))
        t = threading.Thread(
            target=lambda a=args, ev=events, r=result: r.setdefault("rc", offer.run_offer(a, alice[0], out=ev)),
            daemon=True,
        )
        t.start()
        assert world["offered"].wait(10), f"offer {i} was never sent"
        sent = world["sent"][before_len]
        offer_key = f"msg/offer-{i}"
        world["messages"][offer_key] = {
            "key": offer_key, "from": "alice", "thread_id": sent["thread_id"], "body": sent["body"],
            "signature_verified": True, "signed_by": OFFERER,
        }
        assert cli.main(["read", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", offer_key]) == 0
        capsys.readouterr()
        rc = cli.main(["pickup", "--base-url", BASE, "--state-file", str(tmp_path / "bob.json"), "--key", offer_key,
                       "--out", str(out), "--receipt"])
        summary = json.loads(capsys.readouterr().out)
        t.join(10)
        assert rc == 0 and result.get("rc") == 0 and summary["picked_up"] is True and summary["receipt"]["sent"] is True
        receipt = world["sent"][before_len + 1]
        receipt_key = f"msg/receipt-{i}"
        world["messages"][receipt_key] = {
            "key": receipt_key, "from": "bob", "thread_id": receipt["thread_id"], "body": receipt["body"],
            "signature_verified": True, "signed_by": RECIPIENT,
        }
        assert cli.main(["read", "--base-url", BASE, "--state-file", str(tmp_path / "alice.json"), "--key", receipt_key]) == 0
        capsys.readouterr()

    # Control on the same alice.json: 6 ordinary sends to bob pass, and the 7th ordinary send trips pair-loop
    alice_path = tmp_path / "alice.json"
    for i in range(6):
        assert cli.main(["send", "--base-url", BASE, "--state-file", str(alice_path),
                         "--thread", "t", "--to", "bob", "--body", f"ordinary ping {i}"]) == 0
        capsys.readouterr()
        reply_key = f"msg/ctrl-reply-{i}"
        world["messages"][reply_key] = {
            "key": reply_key, "from": "bob", "thread_id": "t", "body": f"ordinary pong {i}",
            "signature_verified": True, "signed_by": RECIPIENT,
        }
        assert cli.main(["read", "--base-url", BASE, "--state-file", str(alice_path), "--key", reply_key]) == 0
        capsys.readouterr()
    assert cli.main(["send", "--base-url", BASE, "--state-file", str(alice_path),
                     "--thread", "t", "--to", "bob", "--body", "seventh ordinary ping"]) == 2
    assert "pair-loop guard: 6 sends to bob and " in capsys.readouterr().err
