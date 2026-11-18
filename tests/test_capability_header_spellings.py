"""The capability header has one spelling, `Meshychat-Capability`, and one reader.

tests/test_capability_header_refused.py walks every route and upgrade with the header (tokens off),
and tests/test_capability_header_not_accepted.py the routes that take none (tokens on). This file covers
the reader: a route that takes a token reads it through `capability_header.presented`, never from the
META key itself, and a header that only resembles the name is not the header.

Run:  pytest tests/test_capability_header_spellings.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_capability_header_refused import (  # noqa: E402
    HTTP_ROUTES,
    VALUE,
    http,
    upgrade,
)

from django.test import override_settings  # noqa: E402

from apps.core import capability_header as ch  # noqa: E402

ON = override_settings(MESHY_CAPABILITIES=True)
REVOKE = "/v1/memory/capabilities/revoke"
NAME = b"meshychat-capability"


# ------------------------------------------------------------------------------- the readers

def test_presented_reads_the_header_and_only_the_header():
    assert ch.presented({}) is None and not ch.carries_capability({})
    assert ch.presented({ch.META_KEY: "a"}) == "a"
    assert ch.presented({ch.META_KEY: ""}) == "" and ch.carries_capability({ch.META_KEY: ""})
    near = {"HTTP_MESHYCHAT_CAPABILITIES": "b", "HTTP_X_MESHYCHAT_CAPABILITY": "c"}
    assert ch.presented(near) is None and not ch.carries_capability(near)


def test_an_accepted_route_reads_the_token_through_presented(monkeypatch):
    """The revoke route's pattern, served by a spy that reads the token the way the real views do."""
    from django.http import HttpResponse

    route = next(r for r in HTTP_ROUTES if r.route == "v1/memory/capabilities/revoke")
    seen = []

    def spy(request, *args, **kwargs):
        seen.append(ch.presented(request.META))
        return HttpResponse(b"read", status=200)

    monkeypatch.setattr(route.pattern, "callback", spy)
    with ON:
        for headers in ([(NAME, b"tok-1")], [(b"Meshychat-Capability", b"tok-2")]):
            assert http("POST", REVOKE, headers)["status"] == 200
    assert seen == ["tok-1", "tok-2"], seen


def test_the_real_views_read_the_token_through_presented_only():
    """Every module that reads the token from a request leaves the META key to the middleware module,
    which defines it: one reader, so a later change to how the token is read has one place to make.
    MUTANT: read `request.META[capability_header.META_KEY]` in apps/calls/object_capability.py; this fails."""
    import re

    reads = re.compile(
        r"META(?:\.get\(|\[)\s*(?:capability_header\.META_KEY|['\"]HTTP_MESHYCHAT_CAPABILITY)"
        r"|headers(?:\.get\(|\[)\s*(?:capability_header\.HEADER|['\"]Meshychat-Capability)",
        re.I)
    offenders = []
    checked = 0
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if path.name == "capability_header.py":
            continue
        checked += 1
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if reads.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert checked > 100, "control: the scan reached the apps tree"
    assert offenders == [], offenders
    # Sensitivity control: the scan sees the line this rule replaced.
    assert reads.search("    token = request.META[capability_header.META_KEY]")
    assert reads.search('presented = request.headers.get(capability_header.HEADER)')


# --------------------------------------------------------------------------------- the upgrade

def test_an_upgrade_with_the_header_is_closed_before_accept():
    headers = [(NAME, VALUE)]
    assert upgrade("/v1/ws", headers).get("code") == 4414, headers
    with ON:
        assert upgrade("/v1/ws", headers).get("code") == 4415, headers


# --------------------------------------------------------------------------- the error report

def test_the_error_report_hides_the_header():
    from django.test import RequestFactory

    from apps.core.error_reports import CapabilityReporterFilter
    token = "t" * 40
    request = RequestFactory().get("/v1/health", HTTP_MESHYCHAT_CAPABILITY=token)
    meta = CapabilityReporterFilter().get_safe_request_meta(request)
    assert meta["HTTP_MESHYCHAT_CAPABILITY"] != token, meta
