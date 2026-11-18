"""The client's owner sign-in takes the session cookie under either name (divergence 078).

The server sets `meshy_session` today and moves to `meshychat_session` later; this client reads whichever the
sign-in set and sends the session back under that name, so it works with a server on either side of the
rename. tests/test_fresh_install_runbook.py drives `pair` end to end against a real server under the current
name; this file covers both names without one, by answering the client's two HTTP calls itself.

Run:  pytest tests/test_meshy_node_session_cookie.py -q
"""
from __future__ import annotations

import os
import sys
from email.message import Message
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import node, wire  # noqa: E402


def test_the_cookie_is_found_under_either_name_and_the_newer_name_wins():
    find = node.session_cookie
    assert find(["meshy_session=abc; Path=/; HttpOnly"]) == ("meshy_session", "abc")
    assert find(["meshychat_session=xyz; Path=/; HttpOnly"]) == ("meshychat_session", "xyz")
    assert find(["meshy_session=old; Path=/", "meshychat_session=new; Path=/"]) == ("meshychat_session", "new")
    assert find(["meshychat_session=new", "meshy_session=old"]) == ("meshychat_session", "new")
    # Not a session cookie: another name, a longer name, an empty value, nothing at all.
    for headers in (["csrftoken=1"], ["meshy_session_x=1"], ["xmeshychat_session=1"], ["meshy_session=; Path=/"],
                    [], None):
        assert find(headers) is None, headers


def _signer_state():
    seed = os.urandom(32)
    return {"base_url": "http://server.invalid", "peer_id": "11111111-1111-1111-1111-111111111111",
            "status": "bound", "signing_seed_b64url": wire.b64u(seed)}


def _fake_server(monkeypatch, set_cookie: str | None):
    """Answers challenge, sign-in (with `set_cookie`) and /v1/auth/me; records each call's headers."""
    calls = []

    def http_json(method, base_url, path, *, body=None, headers=None, **_kw):
        calls.append((method, path, dict(headers or {})))
        if path == "/v1/auth/owner/challenge":
            return 200, {"nonce": "n-1"}
        if path == "/v1/auth/me":
            return 200, {"csrf_token": "csrf-1", "owner_id": "o-1"}
        if path == "/v1/auth/onetime":
            return 201, {"code": "c-1", "expires_at": "later"}
        raise AssertionError(path)

    def http_request(method, base_url, path, *, body=None, headers=None, **_kw):
        calls.append((method, path, dict(headers or {})))
        assert path == "/v1/auth/owner/sign-in"
        reply = Message()
        if set_cookie:
            reply["Set-Cookie"] = set_cookie
        return 200, {"owner_id": "o-1"}, reply

    monkeypatch.setattr(node, "http_json", http_json)
    monkeypatch.setattr(node, "http_request", http_request)
    return calls


@pytest.mark.parametrize("name", ["meshychat_session", "meshy_session"])
def test_the_session_goes_back_under_the_name_the_server_set(monkeypatch, tmp_path, name):
    """`pair` signs in once and mints a code: both later calls carry the cookie under the name the sign-in
    set. MUTANT: send it as `meshy_session` always; the meshychat_session case fails."""
    calls = _fake_server(monkeypatch, f"{name}=sess-1; Path=/; HttpOnly; SameSite=Lax")
    out = node.pair(tmp_path / "state.json", _signer_state(), count=1)
    assert out["codes"] == [{"code": "c-1", "expires_at": "later"}]
    sent = [(path, h.get("Cookie")) for _m, path, h in calls if path in ("/v1/auth/me", "/v1/auth/onetime")]
    assert sent == [("/v1/auth/me", f"{name}=sess-1"), ("/v1/auth/onetime", f"{name}=sess-1")], sent


def test_a_sign_in_with_no_session_cookie_is_refused_naming_both_names(monkeypatch, tmp_path):
    _fake_server(monkeypatch, "csrftoken=1; Path=/")
    with pytest.raises(node.ServerSaid) as exc:
        node.owner_session(_signer_state())
    assert "meshychat_session or meshy_session" in str(exc.value.body), exc.value.body
