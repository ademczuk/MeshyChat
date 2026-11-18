"""URL map for the mesh graph. GET reads, all peer-authenticated.

Five read the projection and require read on shared/mesh-chat (`neighbours`, divergence
032, adds the persisted related_to edges on request); `wait` is the long-poll
(divergence 018, apps/mesh/wait.py). The two `fleet` routes (divergence
081) do not take the read gate: they answer the bootstrap authority, and the counts route also the
exporter peer holding the mesh:backlog:read scope.

`<path:thread_id>` because MESH_THREAD_RE admits `/` and `str` stops at the first one.

Mounted from `meshychat/urls.py`. `tests/test_every_url_module_is_mounted.py` fails if it
is not, which is what makes this module reachable rather than merely present.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/mesh/inbox", views.inbox, name="mesh-inbox"),
    path("v1/mesh/owed/key", views.owed_key, name="mesh-owed-key"),
    path("v1/mesh/owed", views.owed, name="mesh-owed"),
    path("v1/mesh/awaiting", views.awaiting, name="mesh-awaiting"),
    path("v1/mesh/brief", views.brief, name="mesh-brief"),
    path("v1/mesh/unread", views.unread, name="mesh-unread"),
    path("v1/mesh/recipients", views.recipients, name="mesh-recipients"),
    path("v1/mesh/fleet/unread", views.fleet_unread, name="mesh-fleet-unread"),
    path("v1/mesh/fleet/unread/<uuid:peer_id>", views.fleet_unread_peer,
         name="mesh-fleet-unread-peer"),
    path("v1/mesh/mentions", views.mentions, name="mesh-mentions"),
    path("v1/mesh/threads/<path:thread_id>", views.thread, name="mesh-thread"),
    path("v1/mesh/drift", views.drift, name="mesh-drift"),
    path("v1/mesh/neighbours", views.neighbours, name="mesh-neighbours"),
    path("v1/mesh/wait", views.wait, name="mesh-wait"),
]
