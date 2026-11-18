"""Routes for the skillsreg surface.

ALREADY MOUNTED in `meshychat/urls.py` (`path("", include("apps.skillsreg.urls"))`).
Tests for these routes resolve against the PROJECT urlconf and must not pin their own
ROOT_URLCONF: the previous round of modules were written, tested and never mounted, and
every route test passed while the application served 404, because each test supplied its
own URL map. A route test that brings its own urlconf proves the view and says nothing
about the wiring.

The paths are declared with Django's `<str:name>` converter. There is no prefix, so every
path below is absolute as written.

`<str:name>` (not `<slug:name>`): a skill name is an arbitrary non-empty string; there is no
name grammar in the schema, only `NOT NULL`. A converter that rejected dots or unicode would
404 a valid name. Django's `str` converter excludes `/` and nothing else.

ORDER IS LOAD-BEARING on the `sync` line. `/v1/peers/me/skills/sync` must be the sync route,
not an update of a skill named "sync". Django resolves in declaration order, so `sync` is
declared first; moving it below `<str:name>` would silently re-point that URL at
`skill_detail`.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/peers/me/skills", views.my_skills, name="my-skills"),
    path("v1/peers/me/skills/sync", views.sync_manifest, name="skills-sync"),
    path("v1/peers/me/skills/<str:name>/versions", views.release_version,
         name="skill-release-version"),
    path("v1/peers/me/skills/<str:name>", views.skill_detail, name="skill-detail"),

    # The "fetch one" hydrate route. Search, per-peer catalog, and version listing are not
    # implemented here.
    path("v1/skills/<str:peer_id>/<str:name>", views.get_current, name="skill-get-current"),
]
