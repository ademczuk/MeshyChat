"""Reachability through the PROJECT's own URLconf and ASGI router.

WHY THIS FILE EXISTS: the adversarial review found that `apps/authapi` and `apps/wsgw` were
never mounted. Every route test in those modules passed anyway, because each pinned
`ROOT_URLCONF` to its own urls module (or drove the consumer directly). **A route test that
supplies its own URLconf proves the view and says nothing about the wiring**, and the
application served 404 for all of them.

So these tests deliberately use NO fixture that overrides routing. They resolve against
`meshychat.urls` and `meshychat.asgi` exactly as a request would.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.urls import NoReverseMatch, resolve, reverse  # noqa: E402

# Routes the SERVER must answer. Each is (path, the view name it must resolve to).
REQUIRED = [
    ("/v1/health", "health"),
    ("/v1/auth/challenge", "auth-challenge"),
    ("/v1/auth/sign-in", "auth-sign-in"),
    ("/v1/auth/refresh", "auth-refresh"),
    # The mint path (the project's design log (not part of this release) 016): without these five no node can be created.
    ("/v1/auth/owner/challenge", "auth-owner-challenge"),
    ("/v1/auth/owner/sign-in", "auth-owner-sign-in"),
    ("/v1/auth/me", "auth-me"),
    ("/v1/auth/onetime", "auth-onetime"),
    ("/v1/onboard/peers", "onboard-peers"),
    ("/.well-known/jwks.json", "server-jwks"),
    # The doorbell key's own key set (divergence 041, operator decision 2026-09-25).
    ("/.well-known/meshy-invoke-jwks.json", "invoke-webhook-jwks"),
    ("/v1/peers/me/presence", "presence-beat"),
    ("/v1/peers/presence", "presence-list"),
    ("/v1/peers/me/keys", "my-keys"),
    ("/agents/11111111-1111-1111-1111-111111111111/jwks.json", "peer-jwks"),
    ("/agents/11111111-1111-1111-1111-111111111111/agent-card.json", "peer-agent-card"),
    ("/v1/peers/me/agent-card", "publish-agent-card"),
]


@pytest.mark.parametrize("path,name", REQUIRED)
def test_route_is_reachable_through_the_project_urlconf(path, name):
    match = resolve(path)          # raises Resolver404 if the module is not mounted
    assert match.url_name == name, f"{path} resolved to {match.url_name!r}, expected {name!r}"


def test_a_route_that_should_not_exist_is_a_404():
    """Positive control for the parametrised tests above: resolve() must be capable of
    FAILING here, otherwise 'it resolved' proves nothing."""
    from django.urls.exceptions import Resolver404
    with pytest.raises(Resolver404):
        resolve("/v1/definitely-not-mounted")


def test_reverse_agrees_with_resolve():
    """A name that resolves but cannot be reversed means two different route tables."""
    assert reverse("health") == "/v1/health"
    with pytest.raises(NoReverseMatch):
        reverse("definitely-not-a-route-name")


def test_the_websocket_protocol_is_actually_mounted():
    """The hub was defined, tested directly, and reachable by nobody."""
    from meshychat.asgi import application
    mapping = getattr(application, "application_mapping", {})
    assert "websocket" in mapping, (
        "ASGI router has no 'websocket' key: the server would answer no WebSocket upgrade "
        "at all, while apps/wsgw's own tests keep passing against the consumer directly")
    assert "http" in mapping, "http protocol missing - positive control for the check above"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
