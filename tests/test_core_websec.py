"""The salvaged CSRF check and the config-flag reader.

WHY THIS FILE EXISTS. Two implementations were rescued out of `apps/billing` before that
package was deleted, because they were the only correct ones in the repo and their twins on
the dashboard surface were broken. But "correct" was itself only half true: the billing
CSRF check had never been tested against a WRONG token (a mutation making
`hmac.compare_digest` return True unconditionally left all 51 billing tests green), and it
raised `TypeError` on a non-ASCII header.

So each test below states the mutation or input it exists to catch. Every negative carries
a positive control in the same test - a refusal on its own is also what a broken secret, a
wrong session id or a typo produces.

Run: .venv/Scripts/python.exe -m pytest tests/test_core_websec.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.core import env as envmod  # noqa: E402
from apps.core import websec  # noqa: E402

SECRET = b"a-state-secret-of-no-particular-length"
SESSION = uuid.UUID("11111111-2222-3333-4444-555555555555")


# ------------------------------------------------------------------------- CSRF

def test_a_wrong_token_is_refused_and_a_right_one_passes():
    """KILLS the `return True` mutation that survived 51/51 billing tests.

    That suite only ever tested MISSING vs CORRECT, which proves the header is READ - not
    that it is COMPARED. A gate that accepts any non-empty string is not a CSRF gate; it
    is a requirement to send a header, and an attacker can send a header.
    """
    good = websec.csrf_token_for(SESSION, SECRET)

    # POSITIVE CONTROL: the correct token passes, so a refusal below is about the token.
    assert websec.csrf_ok("POST", good, SESSION, SECRET) is True

    assert websec.csrf_ok("POST", "wrong-but-non-empty", SESSION, SECRET) is False
    assert websec.csrf_ok("POST", good[:-1], SESSION, SECRET) is False, "truncated"
    assert websec.csrf_ok("POST", good + "x", SESSION, SECRET) is False, "extended"
    assert websec.csrf_ok("POST", None, SESSION, SECRET) is False
    assert websec.csrf_ok("POST", "", SESSION, SECRET) is False

    # BOUND TO THE SESSION, not a bearer value: the same secret, another session, no.
    other = websec.csrf_token_for(uuid.uuid4(), SECRET)
    assert websec.csrf_ok("POST", other, SESSION, SECRET) is False
    # ...and bound to the SECRET.
    assert websec.csrf_ok("POST", websec.csrf_token_for(SESSION, b"other"), SESSION,
                          SECRET) is False


def test_a_non_ascii_header_is_a_refusal_and_never_an_exception():
    """THE LIVE DEFECT IN THE SALVAGED CODE. `hmac.compare_digest` on two `str` raises
    `TypeError: comparing strings with non-ASCII characters is not supported`. Django
    decodes request headers as latin-1, so ANY byte above 0x7F in `X-CSRF-Token` produced
    that TypeError - and the route wrapper catches `ApiError` only, so it escaped as an
    unhandled HTML 500.

    An attacker-chosen header byte must not decide whether the server answers JSON.
    """
    for hostile in ("é", "tokén", "ÿ" * 43, "\U0001f600"):
        assert websec.csrf_ok("POST", hostile, SESSION, SECRET) is False

    # POSITIVE CONTROL: the function is still capable of returning True, so the four
    # Falses above are refusals rather than a function that refuses everything.
    assert websec.csrf_ok("POST", websec.csrf_token_for(SESSION, SECRET), SESSION,
                          SECRET) is True


def test_safe_methods_pass_and_unsafe_ones_do_not():
    """A read never carries a token; every state-changing verb does."""
    for method in ("GET", "HEAD", "OPTIONS", "TRACE", "get", "head"):
        assert websec.csrf_ok(method, None, SESSION, SECRET) is True, method
    for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
        assert websec.csrf_ok(method, None, SESSION, SECRET) is False, method


def test_no_configured_secret_fails_closed_in_release_and_passes_in_debug():
    """THE DIRECTION IS THE WHOLE POINT: an unconfigured deployment must
    refuse, because the alternative is a control that ships silently disabled."""
    assert websec.csrf_ok("POST", "anything", SESSION, None, debug=False) is False
    assert websec.csrf_ok("POST", "anything", SESSION, None, debug=True) is True
    # A read is still a read even with no secret.
    assert websec.csrf_ok("GET", None, SESSION, None, debug=False) is True


def test_the_token_matches_the_pinned_derivation():
    """The domain separator is `b"meshy-csrf:v1:"` VERBATIM, trailing colon
    included. A token minted from a different spelling verifies against nothing, and the
    failure would look like "the user's browser sent a stale token"."""
    assert websec.CSRF_DOMAIN == b"meshy-csrf:v1:"
    # Accepts the session id as a string too - that is how it arrives from a row.
    assert websec.csrf_token_for(str(SESSION), SECRET) == \
        websec.csrf_token_for(SESSION, SECRET)
    token = websec.csrf_token_for(SESSION, SECRET)
    assert "=" not in token and "+" not in token and "/" not in token, "base64url, unpadded"
    assert len(token) == 43, "a 32-byte digest is 43 unpadded base64url characters"


# ------------------------------------------------------------------- env_flag

def test_env_flag_reads_the_environment_when_the_setting_is_absent(monkeypatch):
    """THE DEFECT THIS FIXES. `apps/dashboard/auth.py:114-116` reads the Django setting
    ONLY, and `meshychat/settings.py` never defines MESHY_SSO_CONFIGURED - so that gate
    was permanently off and no environment variable could turn it on.
    """
    monkeypatch.delenv("MESHY_SSO_CONFIGURED", raising=False)
    # POSITIVE CONTROL: absent everywhere, the flag is OFF - so a True below comes from
    # the environment and not from a default.
    assert envmod.env_flag("MESHY_SSO_CONFIGURED", "MESHY_SSO_CONFIGURED") is False

    monkeypatch.setenv("MESHY_SSO_CONFIGURED", "1")
    assert envmod.env_flag("MESHY_SSO_CONFIGURED", "MESHY_SSO_CONFIGURED") is True


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    (" on ", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
    ("perhaps", False),
])
def test_only_the_named_values_are_true(monkeypatch, raw, expected):
    """"0" IS FALSE, though `bool("0")` is True. These flags are written by a human into a
    shell file, and every such file in the world means off by "0". A flag nobody can spell
    correctly must fail CLOSED, so anything unrecognised is False too."""
    monkeypatch.setenv("MESHY_TEST_FLAG", raw)
    assert envmod.env_flag("MESHY_TEST_FLAG", "MESHY_TEST_FLAG") is expected


def test_an_explicit_setting_beats_the_environment(monkeypatch):
    """"Explicitly off" must beat "the environment says on" - otherwise a deployment
    cannot turn anything off without finding every shell that exports it."""
    monkeypatch.setenv("MESHY_TEST_FLAG", "1")
    with override_settings(MESHY_TEST_FLAG=False):
        assert envmod.env_flag("MESHY_TEST_FLAG", "MESHY_TEST_FLAG") is False
    # CONTROL: with the setting gone the same environment value is honoured, so the False
    # above came from the setting rather than from the flag being broken.
    assert envmod.env_flag("MESHY_TEST_FLAG", "MESHY_TEST_FLAG") is True


# --------------------------------------------------------------- response headers

def test_the_middleware_sets_the_headers_on_errors_too_and_never_overwrites():
    """A HEADER SET ONLY ON THE HAPPY PATH IS NOT A CONTROL: an error body is still a body
    a browser will sniff."""
    from django.http import HttpResponse

    for status in (200, 401, 500):
        mw = websec.SecurityHeadersMiddleware(lambda r, s=status: HttpResponse("x", status=s))
        response = mw(None)
        assert response["X-Content-Type-Options"] == "nosniff", status
        assert response["Referrer-Policy"] == "no-referrer", status

    # A route that deliberately set something different KEEPS it, so the difference stays
    # visible in that route instead of being silently normalised away here.
    def deliberate(_request):
        response = HttpResponse("x")
        response["Referrer-Policy"] = "same-origin"
        return response

    out = websec.SecurityHeadersMiddleware(deliberate)(None)
    assert out["Referrer-Policy"] == "same-origin"
    assert out["X-Content-Type-Options"] == "nosniff"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
