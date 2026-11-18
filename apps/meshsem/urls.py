"""URL map for semantic mesh reads. Two GETs, peer-authenticated, no writes.

Mounted from `meshychat/urls.py`; tests/test_every_url_module_is_mounted.py fails if it
is not. Paths sit under /v1/mesh/ beside apps/mesh's routes and collide with none of
them (inbox, owed, threads/<path>, drift).
"""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/mesh/search", views.search, name="mesh-search"),
    path("v1/mesh/transcript-usage", views.transcript_usage, name="mesh-transcript-usage"),
    path("v1/mesh/context", views.context, name="mesh-context"),
    path("v1/mesh/hints", views.hints, name="mesh-hints"),
]
