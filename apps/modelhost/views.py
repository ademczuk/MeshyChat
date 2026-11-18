"""GET /v1/modelhost/capabilities - what this node can do, with the evidence.

UNAUTHENTICATED AND READ-ONLY, deliberately, and the reasoning is worth stating because
the opposite default is the usual one. This returns no tenant data, no row, no secret: it
returns whether a GPU on this box currently answers a rerank call. A peer deciding where to
send work needs that BEFORE it holds a grant for the work, so putting it behind
authentication would make discovery depend on the thing discovery is meant to enable.

What it therefore must never include, and the tests hold this: a DSN, a file path, an API
key, or the arbiter's control URL. `base_url` is omitted from the response for that reason
- a consumer routes by provider id through this node, not by connecting to a port on it.
"""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from apps.modelhost import registry


@require_http_methods(["GET", "HEAD"])
def capabilities(request):
    """A bounded sweep behind a short cache, dated so the caller can judge it.

    THE FIRST VERSION PROBED ON EVERY REQUEST and I measured what that cost: one provider
    pointed at a blackholed host held the request 8,064 ms, and three serialise to ~24 s -
    on a route that is unauthenticated by design, so the hold was repeatable by anyone.

    A cached capability is a claim about the PAST, which is the thing this app exists to
    refuse, so the cache is short and every response carries `measured_at` and `age_ms`.
    A dated claim can be judged; an undated one is what lets a consumer route to a model
    that was evicted an hour ago. There is deliberately no cache-bypass parameter, because
    a bypass would hand the hammer straight back.
    """
    body = registry.snapshot()
    return JsonResponse(body, json_dumps_params={"separators": (",", ":")})


__all__ = ["capabilities"]
