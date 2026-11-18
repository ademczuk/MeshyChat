"""A request body nested deeper than MAX_DEPTH is refused 400 before any view (#278, #281).

THE 500 THIS CLOSES. json.loads builds a nested body up to about 3000 levels, but every
recursive walker after it (`ijson.untextable_string_path`, `ijson.unsafe_number_path`,
rfc8785's canonicaliser) runs out of interpreter stack near 995 levels measured alone, and
lower inside a request's call stack: memory-bxxxxx measured about 940 levels answering 500
RecursionError on the invoke route (#275's review). Past about 3000, json.loads itself raises
RecursionError, which every parse site except apps/memoryskills/views._json_body let out as
a 500.

WHERE THE CAP LIVES. A middleware, not a per-view check, for the reason
apps/core/capability_header.py gives: a per-view check misses the routes nobody remembers,
and this repo has more than ten `json.loads(request.body)` sites. It sits inside the error
envelope middleware and before `DatabaseConnectionMiddleware`, so a refused body reaches no
identity, no database and no walker.

HOW IT MEASURES, WITHOUT PARSING. Strings are removed first (a bracket inside a string is
text, not structure), then only the four bracket bytes are kept and the running depth is
summed. Both passes run in C (re, bytes.translate, itertools.accumulate). A body with no more
opening brackets than MAX_DEPTH cannot be too deep and skips both passes. Malformed JSON is
measured as it stands; the view's own parser still refuses it.

THE CAP, 256, is far above any body this server's clients send and far below the walkers'
limit, so it holds however deep a route's own call stack is.
"""
from __future__ import annotations

import itertools
import re

from apps.core.jsonerrors import envelope

MAX_DEPTH = 256

# Linear, not quadratic (memory-bxxxxx's #280 review): the possessive `*+` never gives back
# what it matched, and a string that never closes matches to the end of the body instead of
# failing and being retried from every later quote. The plain `*"` form took 21 s on an 80 KB
# pre-auth body. Possessive quantifiers need Python 3.11 or newer.
_STRING = re.compile(rb'"(?:[^"\\]|\\.)*+(?:"|\\?\Z)', re.S)
_NOT_BRACKETS = bytes(b for b in range(256) if b not in b"[]{}")
_STEP = [0] * 256
_STEP[ord("[")] = _STEP[ord("{")] = 1
_STEP[ord("]")] = _STEP[ord("}")] = -1


def depth(body: bytes) -> int:
    """The deepest nesting of `[` and `{` outside strings, 0 for a scalar."""
    structure = _STRING.sub(b"", body).translate(None, _NOT_BRACKETS)
    return max(itertools.accumulate(map(_STEP.__getitem__, structure)), default=0)


def too_deep(body: bytes, limit: int = MAX_DEPTH) -> bool:
    if body.count(b"[") + body.count(b"{") <= limit:
        return False
    return depth(body) > limit


def _is_json(request) -> bool:
    ctype = (request.content_type or "").split(";")[0].strip().lower()
    return ctype == "application/json" or ctype.endswith("+json")


# The raw blob route stores the bytes of an uploaded FILE; it never parses them, and it
# streams them with request.read() because request.body refuses anything over
# DATA_UPLOAD_MAX_MEMORY_SIZE (apps/files/views._read_capped_body). The uploader sends the
# file's own Content-Type there, so a .json attachment
# arrives as application/json. Measured on main 2a337bfa: reading request.body here turned
# every JSON attachment over 2.5 MB into RequestDataTooBig, and would refuse a deeply nested
# JSON file whose contents are nobody's request.
RAW_UPLOAD_PREFIX = "/v1/files/raw/"


# #281: twenty routes json.loads(request.body) whatever the Content-Type, so a deep body sent as
# text/plain, octet-stream, multipart or with no type at all still reached json.loads and
# answered 500 RecursionError. Any body that opens like JSON is measured, whatever its type. The
# Content-Type is the caller's header, so it cannot buy an exemption (memory-bxxxxx's #286
# review: a deep body labelled multipart answered 500 on 7 pre-auth routes). The raw blob route
# (RAW_UPLOAD_PREFIX) is exempt by PATH. A multipart body too big for request.body is not read
# here, so Django can still stream a real upload into request.FILES; a real multipart body opens
# with "--boundary", never a bracket, so reading a small one costs nothing.
_OPENS_LIKE_JSON = re.compile(rb"\A\s*[\[{]")


def _readable(request) -> bool:
    """A body request.body may hold in memory (Django refuses more with RequestDataTooBig)."""
    from django.conf import settings
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0) <= settings.DATA_UPLOAD_MAX_MEMORY_SIZE
    except ValueError:
        return False


def _maybe_json(request) -> bool:
    ctype = (request.content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("multipart/") and not _readable(request):
        return False
    return bool(_OPENS_LIKE_JSON.match(request.body))


class JsonDepthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # A body over DATA_UPLOAD_MAX_MEMORY_SIZE raises RequestDataTooBig on request.body here,
        # and Django answers it 400, the same answer the view's own request.body gave before this
        # middleware existed (measured; test_a_body_over_the_memory_limit_is_left_to_the_view).
        if (not request.path_info.startswith(RAW_UPLOAD_PREFIX)
                and (_is_json(request) or _maybe_json(request)) and too_deep(request.body)):
            return envelope("E_VALIDATION",
                            f"JSON nested deeper than {MAX_DEPTH} levels is refused",
                            status=400)
        return self.get_response(request)
