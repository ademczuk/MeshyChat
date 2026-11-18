"""Daphne's `WebRequest` must not run Twisted's legacy `multipart/form-data` / form parser (#290).

THE FAILURE, 2026-10-14 (#290):
`daphne.http_protocol.WebRequest` subclasses `twisted.web.http.Request` and calls
`http.Request.__init__` with its default `parsePOSTFormSubmission=True`. For any `POST` with
`Content-Type: multipart/form-data` or `application/x-www-form-urlencoded`,
`http.Request.requestReceived` reads the entire spooled body (`self.content.read()`) into memory
and runs `twisted.web.http._getMultiPartArgs` or `parse_qs` into `self.args` before
`WebRequest.process()` builds the ASGI scope. `WebRequest` never reads `self.args`: it streams
`self.content` into `self.application_queue` for Django's ASGI handler.

Measured on `daphne 4.2.3` / `twisted 26.4.0`:
- Before `disable_daphne_post_form_parsing()`: an unauthenticated 100 MB `multipart/form-data`
  POST drove the worker's peak working set from 85.34 MB to 1,558.51 MB (+1,473.17 MB, ~14.7x
  amplification) and retained +105.77 MB private memory after returning 405.
- After `disable_daphne_post_form_parsing()`: the same 100 MB `multipart/form-data` POST peaks
  at +116.23 MB (matching `application/json` at +115.50 MB) and retains +5.34 MB.

Run:  .venv/Scripts/python.exe -m pytest tests/test_daphne_post_form_parsing.py --noconftest -q -p no:cacheprovider
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from daphne.http_protocol import WebRequest
from twisted.web import http
from twisted.web.http_headers import Headers

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _DummyServer:
    root_path = ""
    proxy_forwarded_address_header = None
    proxy_forwarded_port_header = None
    proxy_forwarded_proto_header = None
    request_buffer_size = 8192

    def protocol_connected(self, _request) -> None:
        pass

    def protocol_disconnected(self, _request) -> None:
        pass


class _DummyFactory:
    def __init__(self) -> None:
        self.server = _DummyServer()


class _DummyTransport:
    def getPeer(self):
        return None

    def getHost(self):
        return None


class _DummyChannel:
    def __init__(self) -> None:
        self.factory = _DummyFactory()
        self.transport = _DummyTransport()
        self.bad_request_called = False

    def getPeer(self):
        return None

    def getHost(self):
        return None

    def _respondToBadRequestAndDisconnect(self) -> None:
        self.bad_request_called = True


def _import_asgi_without_db(monkeypatch):
    """Import `meshychat.asgi` with `serving_connection` stubbed so no database is touched."""
    from apps.core import dbconn, schema_sets

    class _NoopConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(dbconn, "serving_connection", lambda **kw: _NoopConn())
    monkeypatch.setattr(schema_sets, "assert_schema_complete", lambda conn: None)
    if "meshychat.asgi" in sys.modules:
        del sys.modules["meshychat.asgi"]
    import meshychat.asgi as asgi_mod

    return asgi_mod


def _make_multipart_request(payload: bytes) -> tuple[WebRequest, bytes]:
    boundary = b"----MeshyTestBoundary7MA4YWxkTrZu0gW"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="field1"\r\n\r\n'
        + payload
        + b"\r\n--" + boundary + b"--\r\n"
    )
    channel = _DummyChannel()
    req = WebRequest(channel)
    req.content = io.BytesIO(body)
    req.content.seek(0, io.SEEK_END)
    req.requestHeaders = Headers({b"content-type": [b"multipart/form-data; boundary=" + boundary]})
    req.process = lambda: None
    return req, body


def test_asgi_disables_twisted_post_form_parsing_and_preserves_raw_body_for_asgi(monkeypatch):
    """With `meshychat.asgi` loaded, `WebRequest._parsePOSTFormSubmission` is pinned to `False`
    even after `http.Request.__init__` assigns `self._parsePOSTFormSubmission = True`. Calling
    `requestReceived` on a `multipart/form-data` POST never invokes `_getMultiPartArgs`, leaves
    `req.args` empty, and preserves `req.content` at offset 0 for Daphne's ASGI body stream.
    The positive control restores `_parsePOSTFormSubmission = True` and proves Twisted would
    otherwise invoke `_getMultiPartArgs` and copy the part into `req.args`."""
    asgi_mod = _import_asgi_without_db(monkeypatch)
    assert hasattr(asgi_mod, "disable_daphne_post_form_parsing")

    multipart_calls: list[int] = []
    real_get_multipart = http._getMultiPartArgs

    def spy_multipart(content: bytes, ctype: bytes):
        multipart_calls.append(len(content))
        return real_get_multipart(content, ctype)

    monkeypatch.setattr(http, "_getMultiPartArgs", spy_multipart)

    payload = b"x" * 65536
    req, expected_body = _make_multipart_request(payload)
    assert req._parsePOSTFormSubmission is False

    req.requestReceived(b"POST", b"/v1/calls/invoke?foo=bar", b"HTTP/1.1")
    assert multipart_calls == [], "Twisted's _getMultiPartArgs must not be called when disabled"
    assert req.args == {b"foo": [b"bar"]}, "URL query args are still parsed; POST body is not"
    assert req.content.tell() == 0 and req.content.read() == expected_body

    # Positive control: without the property override on WebRequest, http.Request.__init__ sets
    # self._parsePOSTFormSubmission = True and requestReceived runs _getMultiPartArgs into req.args.
    monkeypatch.delattr(WebRequest, "_parsePOSTFormSubmission", raising=False)
    try:
        control_req, _ = _make_multipart_request(payload)
        assert control_req._parsePOSTFormSubmission is True
        control_req.requestReceived(b"POST", b"/v1/calls/invoke", b"HTTP/1.1")
        assert multipart_calls == [len(expected_body)]
        assert control_req.args == {b"field1": [payload]}
    finally:
        asgi_mod.disable_daphne_post_form_parsing()


def test_requirements_declares_twisted_25_5_0_and_boot_refuses_older_twisted(monkeypatch):
    """`requirements.txt` declares `Twisted>=25.5.0`, and `disable_daphne_post_form_parsing()`
    refuses at boot if `twisted.web.http.Request.__init__` lacks `parsePOSTFormSubmission`."""
    from packaging.requirements import Requirement

    req_lines = [
        ln.split("#", 1)[0].strip()
        for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    ]
    twisted_reqs = [Requirement(ln) for ln in req_lines if ln and Requirement(ln).name.lower() == "twisted"]
    assert len(twisted_reqs) == 1
    spec = twisted_reqs[0].specifier
    assert spec.contains("25.5.0")
    assert spec.contains("26.4.0")
    assert not spec.contains("24.11.0")

    asgi_mod = _import_asgi_without_db(monkeypatch)
    asgi_mod.disable_daphne_post_form_parsing()

    def legacy_init(self, channel, queued=0):
        pass

    monkeypatch.setattr(http.Request, "__init__", legacy_init)
    with pytest.raises(RuntimeError, match="Twisted>=25.5.0"):
        asgi_mod.disable_daphne_post_form_parsing()
