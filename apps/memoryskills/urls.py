"""The capability routes (B07 slice 5, docs/DIVERGENCES.md 040): mint and revoke.

Mounted from meshychat/urls.py; tests/test_every_url_module_is_mounted.py fails if it is not.
The revoke route is the one entry on the header middleware's accept list
(apps/core/capability_header.py `ACCEPTED`): it takes the token it revokes in `Meshychat-Capability`.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/memory/capabilities", views.mint_capability, name="memory-capabilities-mint"),
    path("v1/memory/capabilities/revoke", views.revoke_capability,
         name="memory-capabilities-revoke"),
]
