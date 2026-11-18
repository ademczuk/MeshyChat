"""URL map for the invoke + calls surface.

ALREADY MOUNTED in `meshychat/urls.py` (`path("", include("apps.calls.urls"))`), and
that mounting is the point: the previous round of route modules were written, tested and
never mounted, and every one of those route tests passed while the application served
404, because each test pinned its own ROOT_URLCONF. `tests/test_calls_http.py` therefore
overrides no URLconf at all and resolves against the project's real routing table.

Paths use Django's `<str:param>` converter:

    /v1/agents/{peer_id}/skills/{name}/invoke
    /v1/calls
    /v1/calls/{call_id}
    /v1/calls/{call_id}/receipt
    /v1/calls/{call_id}/events
    /v1/calls/{call_id}/chunks
    /v1/calls/{call_id}/cancel

These routes are merged into the project's URLconf without a prefix, so every path here is
absolute exactly as written.

`<str:peer_id>` not `<uuid:peer_id>`: Django's `uuid` converter would not MATCH a
malformed id, so the request would fall through to a resolver 404 with an HTML body,
while parsing inside the view returns a 400. The id is parsed inside the view, where the
status and the error envelope are under this app's control.

`<str:call_id>` is right for a different reason: a call_id is a caller-supplied
idempotency key (TEXT, up to 200 bytes), NOT a uuid - validation only forbids empty,
overlong and control-bearing keys. A `<uuid:>` converter here would 404 every
legitimate non-uuid key. A key containing `/` is unroutable because Django's `str`
converter does not match a slash.

ORDER: the four sub-resource routes precede `/v1/calls/<str:call_id>`. Django resolves
top-down, and although `str` excludes `/` (so `calls/abc/events` could not match the
bare pattern anyway), the order is written to be correct rather than correct by
accident.

NO RESUME ROUTE, and that is not an omission. `ResumeAction` / `resume_nonce` support
the flow, but it has no HTTP route. Resume is driven over the WebSocket frame protocol,
which `apps/wsgw` owns.
"""
from django.urls import path

from . import views

urlpatterns = [
    # Skill invocation route.
    path("v1/agents/<str:peer_id>/skills/<str:name>/invoke", views.invoke,
         name="agent-skill-invoke"),

    # Call routes.
    path("v1/calls", views.list_mine, name="calls-list"),
    path("v1/calls/<str:call_id>/receipt", views.get_receipt, name="call-receipt"),
    path("v1/calls/<str:call_id>/events", views.get_events, name="call-events"),
    path("v1/calls/<str:call_id>/chunks", views.get_chunks, name="call-chunks"),
    path("v1/calls/<str:call_id>/cancel", views.cancel, name="call-cancel"),
    path("v1/calls/<str:call_id>", views.get_call, name="call-get"),
]
