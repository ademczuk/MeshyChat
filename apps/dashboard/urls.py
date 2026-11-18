"""Routes for the dashboard surface.

Paths use Django's `<str:...>` form for path segments. There is no prefix, so every path
below is absolute exactly as written. This module is included from the project URLconf with
`path("", include("apps.dashboard.urls"))`, and adding a prefix there would move all
thirty of them.

ALREADY MOUNTED. `meshychat/urls.py` includes this module, so a test that resolves one
of these paths is testing the REAL routing table. No test in `tests/test_dashboard.py`
overrides ROOT_URLCONF - the previous round pinned its own urlconf and every route test
passed while the application served 404.

`<str:invoice_id>` rather than `<uuid:invoice_id>`: Django's `uuid` converter would not
MATCH a malformed id, so the request would fall through to a resolver 404 with an HTML
body. A malformed id is rejected with 400 inside the view, where the error shape is this
app's.

ORDERING. `notifications/count` and `memories/{peers,search,export,duplicates}` are
listed before the bare `notifications` / `memories` paths. Django matches in order and
these are all exact literals so the order is not load-bearing today, but it becomes
load-bearing the moment a `<str:id>` sibling is added for the write side - which is the
next slice.

SEAMS LEFT NAMED RATHER THAN SILENTLY ABSENT
--------------------------------------------
passkeys      `/v1/dashboard/auth/passkeys/*`. Skipped by instruction.
              They are NOT routed here, so they 404 rather than answering a
              fabricated challenge.
writes        every POST/PATCH/DELETE on the paths below. They resolve to the GET view,
              which answers 405 + `Allow: GET`.
vram          `/v1/dashboard/api/vram/*` - a reverse proxy to the VRAM manager under a
              DIFFERENT extractor (`AuthenticatedAdmin`).
jobcontrol    `/v1/dashboard/api/jobcontrol/*` - a reverse proxy to an external URL,
              including a `{*path}` catch-all.
onboarding    `POST /v1/dashboard/onboarding/dismiss` and `DELETE
              /v1/dashboard/agents/{peer_id}` - writes.
statement     `GET /v1/dashboard/api/payouts/{id}/statement` - the omitted READ route.
              It 404s here.
              docs/DIVERGENCES.md 057.

NOT A SEAM ANY MORE: `POST /v1/dashboard/pairing/generate` is routed (F05, 2026-09-26),
from `apps/authapi/urls.py` rather than this module: it is `onboard.mint_onetime` mounted
on the dashboard path. It is not listed below, and it answers.
"""
from django.urls import path

from . import views

urlpatterns = [
    # The core data layer.
    path("v1/dashboard/api/peers", views.peers, name="dashboard-peers"),
    path("v1/dashboard/api/dispatch/nodes", views.dispatch_nodes, name="dashboard-dispatch-nodes"),
    path("v1/dashboard/api/namespaces", views.namespaces, name="dashboard-namespaces"),

    # The sub-paths come first (see ORDERING above).
    path("v1/dashboard/api/memories/peers", views.memory_peers, name="dashboard-memory-peers"),
    path("v1/dashboard/api/memories/search", views.memory_search, name="dashboard-memory-search"),
    path("v1/dashboard/api/memories/export", views.memory_export, name="dashboard-memory-export"),
    path("v1/dashboard/api/memories/duplicates", views.memory_duplicates,
         name="dashboard-memory-duplicates"),
    path("v1/dashboard/api/memories", views.memories, name="dashboard-memories"),

    path("v1/dashboard/api/messages", views.messages, name="dashboard-messages"),
    path("v1/dashboard/api/stats", views.stats, name="dashboard-stats"),
    path("v1/dashboard/api/tier-info", views.tier_info, name="dashboard-tier-info"),
    path("v1/dashboard/api/models", views.models, name="dashboard-models"),
    path("v1/dashboard/api/usage", views.usage, name="dashboard-usage"),
    path("v1/dashboard/api/tools", views.tools, name="dashboard-tools"),
    path("v1/dashboard/api/bridges", views.bridges, name="dashboard-bridges"),
    path("v1/dashboard/api/marketplace", views.marketplace, name="dashboard-marketplace"),
    path("v1/dashboard/api/notifications/count", views.notifications_count,
         name="dashboard-notifications-count"),
    path("v1/dashboard/api/notifications", views.notifications, name="dashboard-notifications"),

    # Log routes.
    path("v1/dashboard/api/logs/settings", views.log_settings, name="dashboard-log-settings"),
    path("v1/dashboard/api/logs", views.logs, name="dashboard-logs"),

    # Agent routes.
    path("v1/dashboard/agents", views.agents, name="dashboard-agents"),

    # Document routes.
    path("v1/dashboard/api/clients", views.clients, name="dashboard-clients"),
    path("v1/dashboard/api/invoices/<str:invoice_id>/items", views.invoice_items,
         name="dashboard-invoice-items"),
    path("v1/dashboard/api/invoices", views.invoices, name="dashboard-invoices"),
    path("v1/dashboard/api/documents", views.documents, name="dashboard-documents"),
    path("v1/dashboard/api/expenses", views.expenses, name="dashboard-expenses"),
    path("v1/dashboard/api/payouts", views.payouts, name="dashboard-payouts"),
    path("v1/dashboard/api/tax-report", views.tax_report, name="dashboard-tax-report"),
]
