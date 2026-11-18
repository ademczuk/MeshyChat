"""`python -m apps.board derive --model-url` sends mesh text only where MUST-8 allows.

THE DEFECT (Kimi review, angle C finding 1). The board deriver's model client
(`apps/board/__main__.py:_http_generate`) POSTed sentences of mesh message bodies
(`apps/board/derive.py`, the prompt carries the bearing sentence) to whatever
`--model-url` named, through a bare `urllib.request.urlopen`: no locality check, the
environment's HTTP_PROXY honoured, redirects followed. Divergence 019 made
`apps/meshsem/providers.post_content` THE chokepoint for mesh content (IP-literal
loopback or RFC1918 only, an empty ProxyHandler, redirects refused), and
`apps/modelhost/locality.py` says so in its first paragraph; this second egress predated
it and never went through it.

Now the client goes through `post_content`, and the CLI refuses a non-LAN `--model-url`
before it reads a row. Every refusal below sits beside the control that the same request,
aimed at a permitted host, is delivered (C1).

Run:  pytest tests/test_board_model_url_must8.py -q        (no database needed)
"""
from __future__ import annotations

import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.board import __main__ as board_cli  # noqa: E402
from apps.meshsem import providers  # noqa: E402
from tests.fixtures.meshsem_fake import FakeModelHost, Recorder  # noqa: E402

PROMPT = "pr/933 was reviewed at 51fbdcb, lgtm"      # stands in for a mesh sentence


@pytest.fixture()
def host():
    h = FakeModelHost(chat="reasoning")
    yield h
    h.close()


@pytest.fixture()
def recorder():
    r = Recorder()
    yield r
    r.close()


def test_a_loopback_model_url_is_asked(host):
    """The positive control every refusal below is measured against."""
    generate = board_cli._http_generate(host.base_url, "m", 2000)
    assert generate(PROMPT) == "ready"
    assert host.calls("/v1/chat/completions") == 1
    sent = host.requests[-1][1]["messages"][0]["content"]
    assert sent == PROMPT


@pytest.mark.parametrize("url_of", [
    lambda h: f"http://localhost:{h.port}",            # a NAME: resolved after any check
    lambda h: f"http://user:pw@127.0.0.1:{h.port}",    # credentials in the URL
])
def test_a_model_url_must8_refuses_never_receives_mesh_text(host, url_of):
    generate = board_cli._http_generate(url_of(host), "m", 2000)
    with pytest.raises(providers.ContentRefused):
        generate(PROMPT)
    assert host.requests == [], "mesh text reached a host MUST-8 refuses"


def test_the_environment_proxy_is_not_used(host, recorder, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", recorder.base_url)
    monkeypatch.setenv("http_proxy", recorder.base_url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    # Control: a default opener built now DOES send this request to the proxy.
    urllib.request.build_opener().open(urllib.request.Request(
        f"{host.base_url}/v1/chat/completions", data=b"{}", method="POST"), timeout=5)
    assert len(recorder.hits) == 1, "control: the default opener uses the env proxy"
    # Every opener rebuilt with the proxy in the environment, as a process started with
    # HTTP_PROXY set would build it: the content opener (tests/test_meshsem_providers.py
    # explains why it must be rebuilt) AND urllib's process-global one, which a bare
    # urlopen() builds on first use. Without the second, an earlier test's urlopen would
    # have cached a proxy-free global opener and a bare-urlopen client would pass here.
    monkeypatch.setattr(providers, "_OPENER", providers._content_opener())
    monkeypatch.setattr(urllib.request, "_opener", None)
    before = host.calls("/v1/chat/completions")
    assert board_cli._http_generate(host.base_url, "m", 2000)(PROMPT) == "ready"
    assert len(recorder.hits) == 1, "mesh text was sent through the environment proxy"
    assert host.calls("/v1/chat/completions") == before + 1


def test_a_redirect_is_refused_not_followed(recorder):
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):                           # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"{recorder.base_url}/v1/chat/completions")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        generate = board_cli._http_generate(f"http://127.0.0.1:{srv.server_address[1]}",
                                            "m", 2000)
        with pytest.raises(providers.ProviderError):
            generate(PROMPT)
        assert recorder.hits == [], "the redirect carried mesh text to its Location"
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_cli_refuses_a_non_lan_model_url_before_reading_a_row(host, capsys, monkeypatch):
    """Refused up front with exit 2, so the operator is told, rather than every row
    turning into a per-row 'model raised' refusal while the sweep reports success."""
    def no_db(*_a, **_k):
        raise AssertionError("the CLI opened the database before refusing --model-url")

    monkeypatch.setattr(board_cli.dbconn, "serving_connection", no_db)
    rc = board_cli.main(["derive", "--model-url", f"http://localhost:{host.port}"])
    assert rc == 2
    assert "MUST-8" in capsys.readouterr().err
    assert host.requests == []


def test_the_client_lives_beside_the_cli_and_the_cli_re_exports_it():
    """`_http_generate` and `answer_text` moved to `apps/board/modelclient.py` so the entry
    point only parses and dispatches. The CLI still binds the SAME objects, so the tests that
    import them from `apps.board.__main__` exercise the client `_derive` actually calls, not
    a copy that could drift from it."""
    from apps.board import modelclient

    assert board_cli._http_generate is modelclient._http_generate
    assert board_cli.answer_text is modelclient.answer_text
