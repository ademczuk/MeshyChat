"""URL map for mesh summaries (divergence 037). Two GETs, peer-authenticated, no edge writes.

Mounted from `meshychat/urls.py`; tests/test_every_url_module_is_mounted.py fails if it is
not. Query parameters rather than path segments: `threads/<path:thread_id>` admits `/`
(apps/mesh/urls.py), so `threads/<id>/summary` would be ambiguous (design 1.1).
"""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/mesh/summaries/thread", views.thread, name="mesh-summary-thread"),
    path("v1/mesh/summaries/backlog", views.backlog, name="mesh-summary-backlog"),
]
