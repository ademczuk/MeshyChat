"""Every app that ships a `urls.py` must be reachable from the ROOT urlconf.

WHY THIS EXISTS ALONGSIDE tests/test_project_wiring.py, which already tests wiring.

That file pins a LIST of routes and asserts each one resolves. It is the right test and
it caught the right bug. But its population is enumerated by hand, so it can only fail
for a route somebody remembered to add to the list. The defect it was written for is an
OMISSION, and an omission cannot be caught by a list that the same omission would have
kept you from updating.

The incident is recorded in `meshychat/urls.py` in the project's own words:

    "MOUNTED 2026-09-21. These modules existed and were NOT included here, so every one
     of their route tests passed while the application served 404 - the tests pinned
     ROOT_URLCONF to each app's own urls module. A route test that supplies its own
     URLconf proves the view, never the wiring."

Two apps were mounted that day. The class was not closed, because nothing checks the
NEXT one. This test derives its population from the filesystem instead: whatever apps
ship a `urls.py` today are the apps that must be mounted today, with no list to update.

Measured when written, 2026-09-21: `apps/authapi/urls.py` and `apps/peers/urls.py` exist
and both are mounted, so this passes. `apps/calls/` and `apps/memoryskills/` ship no
`urls.py` at all, so they are correctly out of scope here. That is a real and separate
gap (2,615 lines with no HTTP surface and no importer outside their own tests) but it is
NOT what this test is for, and conflating the two would make this test fail for a reason
it cannot fix.

Run:  pytest tests/test_every_url_module_is_mounted.py -v   (no database needed)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import django  # noqa: E402
from django.conf import settings  # noqa: E402

if not settings.configured:  # pragma: no cover - mirrors the sibling wiring test
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
    django.setup()

from django.urls import URLResolver, get_resolver  # noqa: E402

APPS_DIR = ROOT / "apps"


def url_modules_on_disk() -> set[str]:
    """Dotted names of every `apps/<app>/urls.py` present in the tree.

    Derived, never enumerated. A hand-written list here would reintroduce exactly the
    omission this file exists to catch.
    """
    return {
        f"apps.{p.parent.name}.urls"
        for p in APPS_DIR.glob("*/urls.py")
    }


def _walk(resolver, seen: set[str]) -> None:
    for pattern in resolver.url_patterns:
        if not isinstance(pattern, URLResolver):
            continue
        target = pattern.urlconf_name
        # include("apps.x.urls") resolves to the MODULE; include([...]) gives a list,
        # which has no name and is not an app urls module, so it is skipped.
        name = getattr(target, "__name__", target if isinstance(target, str) else None)
        if name:
            seen.add(name)
        _walk(pattern, seen)


def mounted_url_modules() -> set[str]:
    """Dotted names reachable from the ROOT urlconf, following nested includes."""
    seen: set[str] = set()
    _walk(get_resolver(), seen)
    return seen


def test_discovery_finds_url_modules_at_all():
    """Positive control. Without it, a broken glob would make every assertion below
    pass over an empty set, which is the vacuous-scope failure this suite must not have.
    """
    found = url_modules_on_disk()
    assert len(found) >= 2, (
        f"expected to discover at least the two known url modules, found {found}. "
        "The glob is broken, so the mounting assertion below proves nothing."
    )
    assert "apps.peers.urls" in found, "sanity: apps/peers/urls.py is known to exist"


def test_the_root_urlconf_mounts_something():
    """Second positive control, on the other instrument.

    `mounted_url_modules()` walking to an empty set would also make the real assertion
    pass vacuously, and it fails differently from a broken glob, so it needs its own check.
    """
    assert mounted_url_modules(), "the root urlconf includes no url modules at all"


@pytest.mark.parametrize("module", sorted(url_modules_on_disk()))
def test_every_url_module_on_disk_is_mounted(module):
    """THE ASSERTION. One case per app, so a failure names the app rather than a set."""
    mounted = mounted_url_modules()
    assert module in mounted, (
        f"{module} exists on disk but is not reachable from meshychat/urls.py. "
        f"Its own route tests can still pass by pinning ROOT_URLCONF to it, while the "
        f"application serves 404. Mounted: {sorted(mounted)}"
    )


def test_an_unmounted_module_would_actually_be_detected():
    """Proves the comparison can distinguish mounted from unmounted.

    Without this, a bug that made `mounted_url_modules()` return everything would keep
    the parametrised test green forever. Uses a name that cannot exist rather than
    touching the tree.
    """
    assert "apps.definitely_not_an_app.urls" not in mounted_url_modules()
