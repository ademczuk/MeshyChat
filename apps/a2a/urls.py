"""URL map for the A2A shim.

ALREADY MOUNTED in `meshychat/urls.py` (`path("", include("apps.a2a.urls"))`), and this
module was mounted while it was still EMPTY, deliberately: the previous round of route
modules were written, tested and never mounted, and every one of those route tests
passed while the application served 404 because each test pinned its own ROOT_URLCONF.
`tests/test_a2a.py` therefore overrides no URLconf at all and resolves against the
project's real routing table.

These routers are merged into the project's URLconf without a prefix, so every path here
is absolute exactly as written. Paths use Django's `<str:param>` converter.

`<str:call_id>`: a call_id is a caller-supplied idempotency key (TEXT, up to 200 bytes),
not a uuid, so a `<uuid:>` converter would 404 every legitimate non-uuid key. A key
containing `/` is unroutable because Django's `str` converter does not match a slash.

`<str:peer_id>` rather than `<uuid:peer_id>`: Django's `uuid` converter would not MATCH
a malformed id, so the request would fall through to a RESOLVER 404 with an HTML body,
while parsing inside the view returns a 400. The id is parsed inside the view where the
status and error envelope are under this app's control.

`message:send` is a LITERAL path segment - the colon is part of the A2A method name, not
a parameter marker. Django's path converters use `<...>`, so a bare colon is literal
text and matches exactly.

ORDER: `/a2a/tasks/<str:call_id>/stream` precedes `/a2a/tasks/<str:call_id>`. Django
resolves top-down and `str` excludes `/`, so the bare pattern could not swallow the
stream path anyway - the order is written to be correct rather than correct by accident.

WHAT IS NOT HERE, named rather than silently dropped. The three AGENT-CARD DISCOVERY
routes are already served by `apps/peers` (`apps/peers/urls.py`):

    GET  /agents/{peer_id}/jwks.json        -> apps.peers.views.jwks
    GET  /agents/{peer_id}/agent-card.json  -> apps.peers.views.serve_card
    POST /v1/peers/me/agent-card            -> apps.peers.views.publish_card

and the SERVER key set `/.well-known/jwks.json` is served by `apps/authapi`. Re-declaring
any of them here would give the project two handlers for one path, with Django silently
preferring whichever include declaration comes first in `meshychat/urls.py` - a shadowing bug that
no route test would catch. This server serves no `/.well-known/agent-card.json` and one
is deliberately not added here.
"""
from django.urls import path

from . import views

urlpatterns = [
    # Task routes.
    path("a2a/tasks/<str:call_id>/stream", views.stream_task, name="a2a-task-stream"),
    path("a2a/tasks/<str:call_id>", views.get_task, name="a2a-task-get"),

    # A2A message route.
    path("a2a/agents/<str:peer_id>/skills/<str:skill_name>/message:send",
         views.message_send, name="a2a-message-send"),
]
