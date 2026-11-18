"""#306: a request body over its route's cap is refused with 413 before Twisted spools it (the residual of #290).

`meshychat.asgi.cap_request_bodies()` patches daphne's `WebRequest`:
- `gotLength` refuses a DECLARED Content-Length over the cap: 413, connection closed, nothing spooled;
- `handleContentChunk` counts a body with no declared length (chunked) and cuts it off at the same cap;
- `requestReceived` never runs the app for a refused request, so buffered bytes cannot complete it into an
  empty-body call.
The cap is the files app's startup max_file_bytes (MESHYCHAT_MAX_FILE_BYTES) for `/v1/files/raw/<token>` and PREAUTH_BODY_MAX_BYTES (4 MB) for every other
route. The tests set both small through their environment overrides, so nothing allocates megabytes.

Run:  .venv/Scripts/python.exe -m pytest tests/test_preauth_body_cap.py --noconftest -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from daphne.http_protocol import WebRequest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_daphne_post_form_parsing import _import_asgi_without_db  # noqa: E402


class _Transport:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    def getPeer(self):
        return None

    def getHost(self):
        return None


class _Server:
    root_path = ""
    proxy_forwarded_address_header = None
    proxy_forwarded_port_header = None
    proxy_forwarded_proto_header = None
    request_buffer_size = 8192

    def protocol_connected(self, _request) -> None:
        pass

    def protocol_disconnected(self, _request) -> None:
        pass


class _Factory:
    def __init__(self) -> None:
        self.server = _Server()


class _Channel:
    """What the patched methods touch on twisted's HTTPChannel: the request line's `_path`, the transport, and
    loseConnection."""

    def __init__(self, path: bytes) -> None:
        self._path = path
        self.transport = _Transport()
        self.factory = _Factory()
        self.lost = False

    def loseConnection(self) -> None:
        self.lost = True

    def getPeer(self):
        return None

    def getHost(self):
        return None


def _file_limit(monkeypatch, max_file_bytes):
    """Stand in for the files config the app read at startup (`blob.active()`); None: no blob backend."""
    from apps.files import blob
    if max_file_bytes is None:
        def _none():
            raise blob.BlobError("no blob backend")
        monkeypatch.setattr(blob, "active", _none)
    else:
        monkeypatch.setattr(blob, "active", lambda: (SimpleNamespace(max_file_bytes=max_file_bytes), None))


@pytest.fixture
def asgi(monkeypatch):
    monkeypatch.setenv("MESHY_PREAUTH_BODY_MAX_BYTES", "100")
    _file_limit(monkeypatch, 1000)
    return _import_asgi_without_db(monkeypatch)


def _request(path: bytes) -> tuple[WebRequest, _Channel, list]:
    channel = _Channel(path)
    req = WebRequest(channel)
    ran: list = []
    req.process = lambda: ran.append("process")
    return req, channel, ran


def test_the_cap_per_route_and_its_overrides(asgi, monkeypatch):
    assert asgi.preauth_body_cap(b"/v1/mesh/inbox") == 100
    assert asgi.preauth_body_cap(b"/v1/files/raw/tok123") == 1000
    assert asgi.preauth_body_cap(b"/v1/files/raw/tok123?x=1") == 1000, "the query string does not hide the route"
    assert asgi.preauth_body_cap(b"/v1/files/rawx") == 100, "only the raw upload prefix gets the file limit"
    monkeypatch.delenv("MESHY_PREAUTH_BODY_MAX_BYTES")
    assert asgi.preauth_body_cap(b"/v1/mesh/inbox") == asgi.PREAUTH_BODY_MAX_BYTES == 4 * 1024 * 1024
    monkeypatch.setenv("MESHYCHAT_MAX_FILE_BYTES", "5")
    assert asgi.preauth_body_cap(b"/v1/files/raw/t") == 1000, "the file limit is the one read at startup, not the env now"
    _file_limit(monkeypatch, None)
    assert asgi.preauth_body_cap(b"/v1/files/raw/t") == asgi.PREAUTH_BODY_MAX_BYTES, "no blob backend: the route answers 503, ordinary cap"
    monkeypatch.setenv("MESHY_PREAUTH_BODY_MAX_BYTES", "not-a-number")
    assert asgi.preauth_body_cap(b"/x") == asgi.PREAUTH_BODY_MAX_BYTES, "a bad override falls back, never to 0"
    monkeypatch.setenv("MESHY_PREAUTH_BODY_MAX_BYTES", "0")
    assert asgi.preauth_body_cap(b"/x") == asgi.PREAUTH_BODY_MAX_BYTES


def test_a_declared_length_over_the_cap_is_refused_before_anything_is_spooled(asgi):
    req, channel, ran = _request(b"/v1/mesh/inbox")
    req.gotLength(101)
    assert channel.transport.written.startswith(b"HTTP/1.1 413 "), channel.transport.written
    assert channel.lost is True
    assert req.content.getvalue() == b"", "nothing is spooled for a refused body"
    req.handleContentChunk(b"x" * 10)   # small enough to pass the cap on its own: only the refusal drops it
    assert req.content.getvalue() == b"", "late bytes of a refused body are dropped"
    assert channel.transport.written.count(b"HTTP/1.1 413 ") == 1, "the 413 is written once"
    req.requestReceived(b"POST", b"/v1/mesh/inbox", b"HTTP/1.1")
    assert ran == [], "a refused request never reaches the app"


def test_a_declared_length_at_the_cap_is_served_as_before(asgi):
    req, channel, ran = _request(b"/v1/mesh/inbox")
    req.gotLength(100)
    req.handleContentChunk(b"x" * 100)
    assert channel.transport.written == b"" and channel.lost is False
    req.content.seek(0)
    assert req.content.read() == b"x" * 100
    req.requestReceived(b"POST", b"/v1/mesh/inbox", b"HTTP/1.1")
    assert ran == ["process"]


def test_a_body_with_no_declared_length_is_cut_off_at_the_cap(asgi):
    req, channel, ran = _request(b"/v1/mesh/inbox")
    req.gotLength(None)
    req.handleContentChunk(b"x" * 60)
    assert channel.transport.written == b"", "under the cap so far"
    req.handleContentChunk(b"x" * 41)
    assert channel.transport.written.startswith(b"HTTP/1.1 413 ") and channel.lost is True
    req.handleContentChunk(b"x" * 10)
    req.requestReceived(b"POST", b"/v1/mesh/inbox", b"HTTP/1.1")
    assert ran == []


def test_a_raw_file_upload_gets_the_file_limit_and_no_more(asgi):
    ok, ok_channel, ok_ran = _request(b"/v1/files/raw/tok123")
    ok.gotLength(500)
    assert ok_channel.transport.written == b"", "500 bytes is over the general cap but under the file limit"
    ok.handleContentChunk(b"x" * 500)
    ok.requestReceived(b"PUT", b"/v1/files/raw/tok123", b"HTTP/1.1")
    assert ok_ran == ["process"]

    big, big_channel, big_ran = _request(b"/v1/files/raw/tok123")
    big.gotLength(1001)
    assert big_channel.transport.written.startswith(b"HTTP/1.1 413 ") and big_channel.lost is True
    big.requestReceived(b"PUT", b"/v1/files/raw/tok123", b"HTTP/1.1")
    assert big_ran == []


def test_the_patch_is_installed_on_import(asgi):
    from twisted.web import http
    assert WebRequest.gotLength is not http.Request.gotLength
    assert WebRequest.handleContentChunk is not http.Request.handleContentChunk


def test_a_file_limit_of_zero_is_the_same_limit_the_app_applies(asgi, monkeypatch):
    """MESHYCHAT_MAX_FILE_BYTES=0 makes the app refuse every non-empty upload; the pre-auth cap used to read 0 as
    unset and admit 100 MB before the app refused it (#313 review)."""
    _file_limit(monkeypatch, 0)
    empty, empty_channel, empty_ran = _request(b"/v1/files/raw/tok")
    empty.gotLength(0)
    empty.requestReceived(b"PUT", b"/v1/files/raw/tok", b"HTTP/1.1")
    assert empty_channel.transport.written == b"" and empty_ran == ["process"]
    one, one_channel, one_ran = _request(b"/v1/files/raw/tok")
    one.gotLength(1)
    assert one_channel.transport.written.startswith(b"HTTP/1.1 413 ") and one_channel.lost is True


def test_a_refused_expect_continue_request_gets_the_413_and_no_100(asgi):
    """Through twisted's own HTTPChannel: it sends "100 Continue" right after gotLength for `Expect: 100-continue`,
    which put a 100 on the wire after the 413 (#313 review)."""
    from twisted.internet.testing import StringTransport
    from twisted.web import http

    def _over(path, expect):
        channel = http.HTTPChannel()
        channel.requestFactory = WebRequest
        channel.factory = _Factory()
        transport = StringTransport()
        channel.makeConnection(transport)
        channel.dataReceived(b"PUT " + path + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 101\r\n"
                             + (b"Expect: 100-continue\r\n" if expect else b"") + b"\r\n")
        return transport.value()

    refused = _over(b"/v1/mesh/inbox", expect=True)
    assert refused.startswith(b"HTTP/1.1 413 "), refused
    assert b"100 Continue" not in refused, refused
    served = _over(b"/v1/files/raw/tok", expect=True)      # under the file limit: the 100 still goes out
    assert served.startswith(b"HTTP/1.1 100 Continue"), served
