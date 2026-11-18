"""Routes for the meshguard surface.

MOUNTED IN THE PROJECT URLCONF ALREADY (`meshychat/urls.py`: `path("",
include("apps.meshguard.urls"))`). The previous round of modules were written, tested and
never mounted: every route test passed while the application served 404, because each
test pinned its own ROOT_URLCONF. `tests/test_meshguard.py` therefore overrides NOTHING
about routing and resolves against the real table.

The routes have no prefix, so every path below is absolute. The old prefix `/v1/meshyguard/`
is not served (operator decision 54, 2026-09-27, docs/DIVERGENCES.md 004).

`<str:...>` rather than `<uuid:...>` for `skill_id` and `owner_id`, for the reason
`apps/peers/urls.py` gives: Django's `uuid` converter would not MATCH a malformed id, so
the request would fall through to the resolver's own HTML 404. Keeping the segment as
`str` puts the parse inside the view, where the error body is this app's JSON envelope. `key` is `<path:key>` because a
key can contain slashes (`wiki/...`), and `<str:>` stops at the first one.

ORDER MATTERS in two places, and both are the reason Django's first-match resolution is
not an implementation detail here:

  * `/v1/meshguard/approvals/pending` is declared BEFORE
    `/v1/meshguard/approvals/<str:approval_id>`, or the literal `pending` would be
    captured as an approval id and every queue read would 404.
  * `/v1/meshguard/policies/attachments` and `/v1/meshguard/policies/decisions` are
    declared BEFORE nothing that would shadow them - `policies` itself is an exact match
    and has no capture - but `attachments/<id>/toggle` precedes `attachments/<id>` so
    the DELETE route cannot swallow the toggle path as an id ending in "/toggle"
    (`<str:>` excludes `/`, so it could not; the order is kept so a later `<path:>` change
    cannot break it quietly).

The `test_every_route_resolves_through_the_project_urlconf` test asserts each name against
`django.urls.resolve` on the REAL routing table, with a negative control proving `resolve`
is capable of failing.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/meshguard/skills/<str:skill_id>",
         views.skill_review, name="meshguard-skill-review"),
    path("v1/meshguard/memory/meshy/<str:owner_id>/<path:key>",
         views.meshy_memory_review, name="meshguard-meshy-memory-review"),
    path("v1/meshguard/scans/pending",
         views.pending_scans, name="meshguard-pending-scans"),
    path("v1/meshguard/approvals/pending",
         views.pending_approvals, name="meshguard-pending-approvals"),
    path("v1/meshguard/approvals/<str:approval_id>/resolve",
         views.resolve_approval, name="meshguard-resolve-approval"),
    path("v1/meshguard/approvals/<str:approval_id>",
         views.approval_item, name="meshguard-approval-item"),

    path("v1/meshguard/policies",
         views.list_policies, name="meshguard-policies"),
    path("v1/meshguard/policies/attachments",
         views.attachments, name="meshguard-attachments"),
    path("v1/meshguard/policies/attachments/<str:attachment_id>/toggle",
         views.toggle_attachment, name="meshguard-toggle-attachment"),
    path("v1/meshguard/policies/attachments/<str:attachment_id>",
         views.remove_attachment, name="meshguard-remove-attachment"),
    path("v1/meshguard/policies/decisions",
         views.list_decisions, name="meshguard-decisions"),

    path("v1/meshguard/hooks/evaluate",
         views.hooks_evaluate, name="meshguard-hooks-evaluate"),
    path("v1/meshguard/hooks/backlog",
         views.hooks_backlog, name="meshguard-hooks-backlog"),
    path("v1/meshguard/hooks/bypass-audit",
         views.hooks_bypass_audit, name="meshguard-hooks-bypass-audit"),
]
