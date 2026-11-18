"""#278: a JSON-typed request body nested deeper than 256 levels is 400 E_VALIDATION, not a 500.

Measured on main fea48820 before this change: a validly signed invoke whose value nested 940
levels answered 500 E_INTERNAL (a recursive walker ran out of stack; memory-bxxxxx's #275
review measured about 940). 300, 600 and 900 levels answered 202 and were stored. The cap is a middleware, so it holds on
every JSON route; the tests pin the measuring function, the two routes the issue names, the
boundary on both sides, and the middleware's place in the stack.
"""
from __future__ import annotations

import json
import uuid

import pytest

from apps.core import jsondepth
from apps.core.jsondepth import MAX_DEPTH, depth, too_deep
from tests.test_call_routing import client, owner_conn, server_config, world  # noqa: F401  (fixtures; server_config is autouse)
from tests.test_call_routing import bearer, signed

MESSAGE = f"JSON nested deeper than {MAX_DEPTH} levels is refused"


def _nested(n: int) -> str:
    return "[" * n + "]" * n


def test_depth_counts_structure_and_ignores_brackets_inside_strings():
    assert depth(b"1") == 0 and depth(b'"x"') == 0 and depth(b"") == 0
    assert depth(b"[]") == 1 and depth(b'{"a":[{"b":[]}]}') == 4
    assert depth(b'{"a":"[[[[[[{{{{"}') == 1, "brackets in a string are text"
    assert depth(b'{"a":"x\\"[[[[","b":[]}') == 2, "an escaped quote does not end the string"
    assert depth(b'{"a":"x\\\\","b":[[]]}') == 3, "an escaped backslash does end it"
    assert depth(_nested(MAX_DEPTH).encode()) == MAX_DEPTH
    assert not too_deep(_nested(MAX_DEPTH).encode())
    assert too_deep(_nested(MAX_DEPTH + 1).encode())
    # many brackets side by side are wide, not deep
    assert not too_deep(("[" + "[]," * 5000 + "[]]").encode())
    # a string full of brackets is not deep however long
    assert not too_deep(json.dumps({"t": "[" * 5000}).encode())


def test_an_unterminated_string_is_measured_in_linear_time():
    """memory-bxxxxx's #280 review: the first regex retried an unclosed string from every
    later quote, each try scanning to the end, so 257 brackets (past the count shortcut), one
    quote and escaped quotes took 13 s at 60 KB before any authentication. Now it is linear."""
    import time
    body = b"[" * 257 + b'"' + (chr(92).encode() + b'"') * 30_000
    started = time.perf_counter()
    assert too_deep(body)
    assert time.perf_counter() - started < 2.0
    # an unclosed string ending on a lone backslash, and one ending on a quote run
    assert depth(b'[["abc' + chr(92).encode()) == 2
    assert depth(b'[' + b'"' * 10_001) == 1


@pytest.mark.parametrize("path",["/v1/agents/{peer}/skills/memory.write/invoke", "/v1/board/acts"])
def test_one_level_past_the_cap_is_refused_and_the_cap_itself_passes(client, world, path):
    """The middleware answers before identity: past the cap is its 400, AT the cap the
    request goes on and the next layer (bearer-less here, so 401) answers instead."""
    url = path.format(peer=world["memory_peer"])
    over = client.post(url, data=json.dumps({"x": json.loads(_nested(MAX_DEPTH))}),
                       content_type="application/json")
    assert over.status_code == 400, over.content[:200]
    assert json.loads(over.content) == {"error": "E_VALIDATION", "message": MESSAGE}
    at = client.post(url, data=json.dumps({"x": json.loads(_nested(MAX_DEPTH - 1))}),
                     content_type="application/json")
    assert at.status_code != 400 and MESSAGE.encode() not in at.content, at.content[:200]


def test_a_validly_signed_invoke_nested_940_deep_is_400_not_500(client, world):
    """The issue's own case, signed honestly (no wire edit): measured on main fea48820, a value
    nested 940 levels answered 500 E_INTERNAL. 300, 600 and 900 answered 202, so the cap also
    refuses depths main accepted; the PR says so."""
    caller, memory_peer, ns = world["caller"], world["memory_peer"], world["namespace"]
    value = "x"
    for _ in range(940):
        value = [value]
    envelope = signed(caller, memory_peer, "memory.write",
                      {"namespace": ns, "key": f"k-{uuid.uuid4().hex[:8]}", "value": {"t": value}})
    body = json.dumps({"envelope": envelope, "mode_override": "async",
                       "call_id": envelope["payload"]["call_id"]})
    resp = client.post(f"/v1/agents/{memory_peer}/skills/memory.write/invoke",
                       data=body, content_type="application/json", headers=bearer(caller))
    assert resp.status_code == 400, resp.content[:200]
    assert json.loads(resp.content)["message"] == MESSAGE


def test_a_shallow_body_that_is_not_json_by_content_type_still_gets_the_views_answer(client, world):
    """The cap measures nesting, not the type: a shallow text/plain body still reaches the
    view, whose 415 still says so."""
    url = f"/v1/agents/{world['memory_peer']}/skills/memory.write/invoke"
    resp = client.post(url, data=_nested(3), content_type="text/plain",
                       headers=bearer(world["caller"]))
    assert resp.status_code == 415, resp.content[:200]


# ------------------------------------------------- #281: deep bodies without a JSON content type
DEEP = _nested(4000)        # past json.loads' own recursion limit (about 3000)


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded",
                                   "application/octet-stream", None])
def test_a_deep_body_without_a_json_type_is_400_on_a_pre_auth_route(client, ctype):
    """Measured on main 5522d813: /v1/auth/challenge json.loads the body whatever its type, so
    4000 levels as text/plain answered 500 RecursionError (#281)."""
    kw = {"content_type": ctype} if ctype else {"content_type": ""}
    resp = client.post("/v1/auth/challenge", data=DEEP, **kw)
    assert resp.status_code == 400, resp.content[:200]
    assert json.loads(resp.content) == {"error": "E_VALIDATION", "message": MESSAGE}


def test_leading_whitespace_does_not_hide_a_deep_body(client):
    resp = client.post("/v1/auth/challenge", data=" \r\n\t" + DEEP, content_type="text/plain")
    assert resp.status_code == 400 and MESSAGE.encode() in resp.content, resp.content[:200]


def test_a_deep_body_cannot_hide_behind_a_multipart_type(client):
    """memory-bxxxxx's #286 review: a multipart exemption let the caller's own Content-Type skip
    the cap, and /v1/auth/challenge (which json.loads the body whatever the type) answered 500."""
    for ctype in ("multipart/form-data; boundary=x", "Multipart/Form-Data; boundary=x"):
        resp = client.post("/v1/auth/challenge", data=DEEP + "\r\n--x--\r\n", content_type=ctype)
        assert resp.status_code == 400 and MESSAGE.encode() in resp.content, (ctype, resp.status_code)


def test_a_real_multipart_upload_is_still_streamed_to_request_files():
    """A real upload still reaches request.FILES and request.POST, a small one read through
    request.body and one over DATA_UPLOAD_MAX_MEMORY_SIZE left unread for Django to stream."""
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.http import JsonResponse
    from django.test import RequestFactory
    seen = {}

    def view(request):
        seen["files"] = {k: len(v.read()) for k, v in request.FILES.items()}
        seen["post"] = dict(request.POST.items())
        return JsonResponse({})
    mw = jsondepth.JsonDepthMiddleware(view)
    rf = RequestFactory()
    assert mw(rf.post("/x", data={"a": "1", "f": SimpleUploadedFile("f.bin", b"x" * 1000)})).status_code == 200
    assert seen == {"files": {"f": 1000}, "post": {"a": "1"}}
    seen.clear()
    assert mw(rf.post("/x", data={"f": SimpleUploadedFile("b.bin", b"x" * 3_000_000)})).status_code == 200
    assert seen["files"] == {"f": 3_000_000}


def test_a_body_over_the_memory_limit_is_left_to_the_view(client):
    """Over DATA_UPLOAD_MAX_MEMORY_SIZE the cap cannot read the body; the view's own
    request.body refuses it exactly as before the cap existed, and the cap does not turn
    that into a 500 of its own."""
    big = "[" + ("1," * 1_400_000) + "1]"
    assert len(big) > 2_621_440
    resp = client.post("/v1/auth/challenge", data=big, content_type="text/plain")
    assert resp.status_code != 500 and MESSAGE.encode() not in resp.content, resp.status_code


def _sweep_paths():
    """Every route in the real URLconf, path parameters filled with a uuid."""
    from django.urls import get_resolver
    from django.urls.resolvers import URLPattern, URLResolver
    import re as _re
    out = []

    def walk(patterns, prefix):
        for p in patterns:
            route = prefix + str(p.pattern)
            if isinstance(p, URLResolver):
                walk(p.url_patterns, route)
            elif isinstance(p, URLPattern):
                out.append("/" + _re.sub(r"<[^>]+>", str(uuid.UUID(int=1)), route).lstrip("^").rstrip("$"))
    walk(get_resolver().url_patterns, "")
    return sorted(set(p for p in out if p.startswith("/v1/") and "/files/raw/" not in p))


def test_every_route_refuses_a_deep_untyped_body_before_its_own_code(client, world):
    """memory-bxxxxx's #281 probe as a test: a 4000-deep text/plain or octet-stream body to
    every route answers the cap's 400, so no route's own parsing, authentication or method
    check runs first. On main 5522d813 this fails on every route (none of them was capped for
    these types), and /v1/auth/challenge, /v1/auth/owner/challenge and /v1/auth/owner/sign-in
    answered 500 RecursionError. memory-bxxxxx measured 20 such routes with a bearer each
    route accepts; this test's bearer is the call-routing one, so the other route families
    refuse it before parsing, which is why the cap's own 400 is asserted, not just 'no 500'."""
    paths = _sweep_paths()
    assert len(paths) > 100, len(paths)          # the sweep reaches the real routing table
    wrong = []
    for path in paths:
        for method in ("post", "put"):
            for ctype in ("text/plain", "application/octet-stream", "multipart/form-data; boundary=x"):
                resp = getattr(client, method)(path, data=DEEP, content_type=ctype,
                                               headers=bearer(world["caller"]))
                if resp.status_code != 400 or MESSAGE.encode() not in resp.content:
                    wrong.append((method, path, ctype, resp.status_code))
    assert wrong == [], wrong[:20]


def test_the_middleware_sits_inside_the_envelope_and_before_the_database():
    from django.conf import settings
    stack = list(settings.MIDDLEWARE)
    ours = "apps.core.jsondepth.JsonDepthMiddleware"
    assert ours in stack, stack
    assert stack.index("apps.core.jsonerrors.JsonErrorEnvelopeMiddleware") < stack.index(ours)
    assert stack.index(ours) < stack.index("apps.core.middleware.DatabaseConnectionMiddleware")
    assert jsondepth.MAX_DEPTH == 256
