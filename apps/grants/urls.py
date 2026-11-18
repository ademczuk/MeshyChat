"""Routes for the grants surface.

The routes are included at the root, not nested, so every path below is absolute. Adding
a prefix would move all ten routes.

Order is load-bearing. Django takes the first matching pattern, so `issued` and `held`
must precede `<str:grant_id>` or a GET of /v1/grants/issued would be handled by the
PATCH/DELETE view and answer 405. `tests/test_grants.py` resolves both through the
project URLconf, so this ordering is measured rather than trusted.

`<str:…>` rather than `<uuid:…>`: Django's uuid converter would NOT MATCH a malformed
id, so the request would fall through to an HTML 404 from the resolver. The ID is parsed
inside the view where the status and error envelope are under this app's control
(see views.py).

This module was mounted in meshychat/urls.py BEFORE these views existed. The previous
round wrote route modules that were never mounted: every route test passed while the
application served 404, because each test pinned its own ROOT_URLCONF. Nothing in
tests/test_grants.py overrides ROOT_URLCONF, so a path that resolves there resolves in
the real routing table.
"""
from django.urls import path

from . import views

urlpatterns = [
    # Grant routes.
    path("v1/grants", views.grants_collection, name="grants-issue"),
    path("v1/grants/issued", views.list_issued, name="grants-issued"),
    path("v1/grants/held", views.list_held, name="grants-held"),
    path("v1/grants/<str:grant_id>", views.grant_item, name="grants-item"),

    # Access-request routes.
    path("v1/access-requests", views.access_requests_collection, name="access-requests-submit"),
    path("v1/access-requests/sent", views.list_sent, name="access-requests-sent"),
    path("v1/access-requests/received", views.list_received, name="access-requests-received"),
    path("v1/access-requests/<str:request_id>/approve", views.approve,
         name="access-requests-approve"),
    path("v1/access-requests/<str:request_id>/deny", views.deny, name="access-requests-deny"),
    path("v1/access-requests/<str:request_id>/withdraw", views.withdraw,
         name="access-requests-withdraw"),
]
