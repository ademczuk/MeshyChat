"""The dashboard, `/v1/auth/me` and the grants surface accept ONE owner session. STATUS B10.

THE DEFECT, measured on c5457ac. `apps/grants/owner_auth.py` is a second owner extractor,
and it disagreed with the first (`apps/dashboard/auth.py`, which `/v1/auth/me` and owner
sign-in also use) in three places:

  1. the cookie NAME: grants read the environment variable `MESHYCHAT_SESSION_COOKIE_NAME`, the
     dashboard read the Django setting `MESHY_SESSION_COOKIE_NAME`. Configure either one and
     the two surfaces look for different cookies, so one session signs you in to half the
     product;
  2. the CSRF SECRET: grants read `MESHYCHAT_STATE_SECRET` from the environment only, the
     dashboard (and `/v1/auth/me`, which HANDS OUT the token) read the setting first. With
     the secret configured as a setting, `/v1/auth/me` issues a token the grants surface
     then refuses, because it sees no secret at all and fails closed;
  3. the CSRF COMPARE: grants called `hmac.compare_digest` on two `str`, which raises
     `TypeError` for any character above 0x7F (Django decodes headers as latin-1), so a
     hostile header byte decided whether the server answered JSON 403 or a 500.
     `apps/core/websec.csrf_ok` compares bytes; the dashboard already used it.

Every refusal below sits beside its positive control in the same test (C1), and the
controls run first: a 401 is also what an unseeded session or a dead route produces.

Run:  .venv/Scripts/python.exe -m pytest tests/test_owner_session_is_one_session.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("MESHY_TEST_DSN", DSN)
os.environ.setdefault("MESHY_DSN", DSN)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

from apps.core import websec  # noqa: E402

GRANTS_OWNER_AUTH = ROOT / "apps" / "grants" / "owner_auth.py"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Every variable these surfaces read, removed, so each case sets exactly one."""
    for name in ("MESHYCHAT_SESSION_COOKIE_NAME", "MESHYCHAT_STATE_SECRET", "MESHYCHAT_ALLOW_DEV_JWT",
                 "MESHY_SSO_CONFIGURED", "MESHY_ALLOW_DEV_JWT", "MESHY_STATE_SECRET",
                 "MESHY_SESSION_COOKIE_NAME"):
        monkeypatch.delenv(name, raising=False)
    with override_settings(ALLOWED_HOSTS=["*"], MESHY_DSN=DSN, DEBUG=False,
                           MESHY_SESSION_COOKIE_NAME=None, MESHY_STATE_SECRET=None):
        yield


@pytest.fixture()
def world():
    """Owner A with a live session and a peer, owner B to grant to."""
    owner_a, owner_b, peer_a = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session_id, raw = uuid.uuid4(), secrets.token_urlsafe(32)
    with psycopg.connect(DSN, autocommit=True) as conn:
        for oid, tag in ((owner_a, "a"), (owner_b, "b")):
            conn.execute("INSERT INTO dashboard_users (id, email) VALUES (%s,%s)",
                         (oid, f"one-session-{tag}-{oid.hex[:8]}@example.test"))
        conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                     (peer_a, owner_a, f"one-session-peer-{peer_a.hex[:8]}"))
        conn.execute("INSERT INTO owner_sessions (id, owner_id, created_at, expires_at, "
                     "  last_seen_at, token_hash) "
                     "VALUES (%s,%s,NOW(),NOW() + INTERVAL '1 hour',NOW(),%s)",
                     (session_id, owner_a, hashlib.sha256(raw.encode()).hexdigest()))
    yield {"owner_a": owner_a, "owner_b": owner_b, "peer_a": peer_a,
           "session_id": session_id, "raw": raw}
    owners = [owner_a, owner_b]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM skill_grants WHERE grantor_owner_id = ANY(%s) "
                     "OR grantee_owner_id = ANY(%s)", (owners, owners))
        conn.execute("DELETE FROM owner_sessions WHERE owner_id = ANY(%s)", (owners,))
        conn.execute("DELETE FROM peers WHERE owner_id = ANY(%s)", (owners,))
        conn.execute("DELETE FROM dashboard_users WHERE id = ANY(%s)", (owners,))


def _client(cookie_name: str, raw: str) -> Client:
    # raise_request_exception=False: an unhandled exception must be SEEN as the 500 a
    # client receives, not re-raised into the test, or "it answered 500" and "the test
    # harness broke" would read the same.
    client = Client(raise_request_exception=False)
    client.cookies[cookie_name] = raw
    return client


def _error(response) -> str | None:
    try:
        return json.loads(response.content).get("error")
    except (ValueError, AttributeError):
        return None


def _grant(client, world, skill: str, **headers):
    body = {"grantee_owner_id": str(world["owner_b"]), "granter_peer_id": str(world["peer_a"]),
            "skill_name": skill, "source": "direct"}
    return client.post("/v1/grants", data=json.dumps(body), content_type="application/json",
                       **headers)


# ------------------------------------------------------------------ the cookie name

COOKIE_SOURCES = {
    "the default": ({}, {}),
    "the Django setting": ({"MESHY_SESSION_COOKIE_NAME": "meshy_custom_session"}, {}),
    "the environment variable": ({}, {"MESHYCHAT_SESSION_COOKIE_NAME": "meshy_custom_session"}),
}


@pytest.mark.parametrize("source", sorted(COOKIE_SOURCES))
def test_one_cookie_name_setting_signs_in_every_owner_surface(world, monkeypatch, source):
    settings_over, env = COOKIE_SOURCES[source]
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    name = (list(settings_over.values()) + list(env.values()) + ["meshy_session"])[0]
    with override_settings(**settings_over):
        client = _client(name, world["raw"])
        seen = {path: client.get(path).status_code
                for path in ("/v1/auth/me", "/v1/dashboard/agents", "/v1/grants/issued")}
        assert seen == {"/v1/auth/me": 200, "/v1/dashboard/agents": 200,
                        "/v1/grants/issued": 200}, f"{source}: {seen}"
        me = json.loads(client.get("/v1/auth/me").content)
        assert me["owner_id"] == str(world["owner_a"])

        if name != "meshy_session":
            # NEGATIVE, after its control: the same session under the DEFAULT name is not
            # a session on any surface, so the 200s above are the configured name deciding.
            stray = _client("meshy_session", world["raw"])
            for path in ("/v1/auth/me", "/v1/dashboard/agents", "/v1/grants/issued"):
                r = stray.get(path)
                assert r.status_code == 401, f"{source}: {path} took the default cookie"


# ----------------------------------------------------------------------- the CSRF

SECRET_SOURCES = {
    "the Django setting": ({"MESHY_STATE_SECRET": "one-session-secret-setting"}, {}),
    "the environment variable": ({}, {"MESHYCHAT_STATE_SECRET": "one-session-secret-env"}),
}


@pytest.mark.parametrize("source", sorted(SECRET_SOURCES))
def test_the_token_auth_me_issues_is_the_token_grants_accepts(world, monkeypatch, source):
    settings_over, env = SECRET_SOURCES[source]
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with override_settings(**settings_over):
        client = _client("meshy_session", world["raw"])
        token = json.loads(client.get("/v1/auth/me").content)["csrf_token"]
        assert isinstance(token, str) and token, f"{source}: /v1/auth/me issued no token"
        secret = (list(settings_over.values()) + list(env.values()))[0].encode()
        assert token == websec.csrf_token_for(world["session_id"], secret)

        # CONTROL: the issued token opens a cookie write on the grants surface.
        ok = _grant(client, world, f"csrf-ok-{uuid.uuid4().hex[:6]}", HTTP_X_CSRF_TOKEN=token)
        assert ok.status_code == 201, f"{source}: {ok.status_code} {ok.content[:200]}"

        # The same write without it, and with another session's token, is refused.
        missing = _grant(client, world, "csrf-missing")
        assert (missing.status_code, _error(missing)) == (403, "E_CSRF_REQUIRED")
        foreign = websec.csrf_token_for(uuid.uuid4(), secret)
        wrong = _grant(client, world, "csrf-wrong", HTTP_X_CSRF_TOKEN=foreign)
        assert (wrong.status_code, _error(wrong)) == (403, "E_CSRF_REQUIRED")


@pytest.mark.parametrize("header", [
    "é" * 43,                          # latin-1 bytes above 0x7F, token-length
    "x" * 42 + "ÿ",                    # one hostile byte at the end
])
def test_a_non_ascii_csrf_header_is_a_json_refusal_and_never_a_500(world, monkeypatch, header):
    monkeypatch.setenv("MESHYCHAT_STATE_SECRET", "one-session-non-ascii")
    client = _client("meshy_session", world["raw"])
    token = json.loads(client.get("/v1/auth/me").content)["csrf_token"]
    ok = _grant(client, world, f"non-ascii-ok-{uuid.uuid4().hex[:6]}", HTTP_X_CSRF_TOKEN=token)
    assert ok.status_code == 201, ok.content[:200]                    # control

    bad = _grant(client, world, "non-ascii", HTTP_X_CSRF_TOKEN=header)
    assert bad.status_code == 403, f"a non-ASCII X-CSRF-Token answered {bad.status_code}"
    assert _error(bad) == "E_CSRF_REQUIRED", bad.content[:200]
    assert bad["Content-Type"].startswith("application/json")


def test_the_grants_extractor_has_no_compare_or_cookie_reader_of_its_own():
    """One implementation of each, shared: the compare in apps/core/websec.py and the cookie
    name and secret in apps/dashboard/auth.py. Positive control: the detector finds the
    compare where it IS."""
    websec_src = (ROOT / "apps" / "core" / "websec.py").read_text(encoding="utf-8")
    assert "compare_digest(" in websec_src
    src = GRANTS_OWNER_AUTH.read_text(encoding="utf-8")
    assert "compare_digest(" not in src, "grants compares CSRF tokens itself"
    assert "MESHYCHAT_SESSION_COOKIE_NAME" not in (ROOT / "apps" / "grants" / "config.py").read_text(
        encoding="utf-8"), "grants still reads its own cookie-name variable"
    from apps.dashboard import auth as dash_auth
    from apps.grants import owner_auth
    assert owner_auth.session_cookie_name is dash_auth.session_cookie_name
    assert owner_auth.state_secret is dash_auth.state_secret


# ------------------------------------------- the review of c766b76 (findings F5 and F8)

def test_a_grants_cookie_write_with_no_state_secret_fails_closed_in_a_release_build(world):
    """With NO secret configured a DEBUG build passes a cookie write and a
    release build FAILS CLOSED, token or no token. Grants now reaches that rule through
    `websec.csrf_ok(..., debug=settings.DEBUG)`, and nothing pinned the release half on this
    surface. The `clean` fixture removes every secret source and sets DEBUG=False.

    CONTROL, same session and route: under DEBUG the same write is created (201), so the
    refusal is the release rule deciding, not a broken session.

    Review finding F5. Mutant killed: A5, `debug=True` passed always."""
    client = _client("meshy_session", world["raw"])
    with override_settings(DEBUG=True):
        ok = _grant(client, world, f"nosecret-debug-{uuid.uuid4().hex[:6]}")
        assert ok.status_code == 201, ok.content[:200]
    for headers in ({}, {"HTTP_X_CSRF_TOKEN": "anything"},
                    {"HTTP_X_CSRF_TOKEN": websec.csrf_token_for(world["session_id"], b"")}):
        r = _grant(client, world, f"nosecret-{uuid.uuid4().hex[:6]}", **headers)
        assert (r.status_code, _error(r)) == (403, "E_CSRF_REQUIRED"), (headers, r.content[:200])


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("where", ["setting", "environment"])
def test_a_blank_cookie_name_is_the_default_on_every_owner_surface(world, monkeypatch,
                                                                   blank, where):
    """`session_cookie_name` says a blank value is not a name: an operator who sets the
    variable to "" (a template left empty) gets the default `meshy_session`, on every owner
    surface alike, not a cookie no browser can send. NEGATIVE, after its control: a session
    under another name is not a session, so the 200s are the default deciding.

    Review finding F8. Mutant killed: A10, the raw value returned without the blank check
    (`return str(raw) if raw is not None else DEFAULT_SESSION_COOKIE`)."""
    from apps.dashboard import auth as dash_auth

    if where == "setting":
        over = {"MESHY_SESSION_COOKIE_NAME": blank}
    else:
        over = {}
        monkeypatch.setenv("MESHYCHAT_SESSION_COOKIE_NAME", blank)
    with override_settings(**over):
        assert dash_auth.session_cookie_name() == "meshy_session"
        client = _client("meshy_session", world["raw"])
        paths = ("/v1/auth/me", "/v1/dashboard/agents", "/v1/grants/issued")
        seen = {path: client.get(path).status_code for path in paths}
        assert seen == dict.fromkeys(paths, 200), f"{where} {blank!r}: {seen}"
        stray = _client("meshy_custom_session", world["raw"])
        for path in paths:
            assert stray.get(path).status_code == 401, f"{path} took a non-default cookie"
