"""MESHY_SURFACES (divergence 054): the optional route surfaces are OFF unless named.

The harness mounts every surface (tests/conftest.py), so this file is where the production
default is held: the parser, the patterns for each state, and a subprocess that loads the real
settings with the variable removed and resolves real paths against the real urlconf.

Run:  .venv/Scripts/python.exe -m pytest tests/test_surfaces.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import surfaces  # noqa: E402

#: One real path of each optional surface, and paths of the core that must never go.
OPTIONAL_PATHS = {
    "grants": "/v1/grants",
    "skillsreg": "/v1/peers/me/skills",
    "files": "/v1/files",
    "dashboard": "/v1/dashboard/api/peers",
    "meshguard": "/v1/meshguard/scans/pending",
    "board": "/v1/board/subjects",
    "meshsum": "/v1/mesh/summaries/thread",
}
#: The routes production served between the cutover and 2026-09-29 (its access log, full
#: paths), so the switch can never take one of them.
CORE_PATHS = ["/v1/health", "/v1/livez", "/v1/metrics", "/v1/peers/me/presence",
              "/v1/peers/me/keys", "/v1/mesh/wait",
              "/v1/calls/00000000-0000-0000-0000-000000000000",
              "/v1/agents/00000000-0000-0000-0000-000000000000/skills/memory.query/invoke",
              "/agents/00000000-0000-0000-0000-000000000000/jwks.json",
              "/v1/auth/owner/challenge", "/v1/auth/onetime", "/v1/onboard/peers", "/v1/auth/me",
              "/v1/mesh/context",
              "/v1/mesh/inbox", "/v1/mesh/search", "/v1/auth/challenge", "/v1/auth/sign-in",
              "/v1/peers/presence", "/.well-known/jwks.json"]


# ------------------------------------------------------------------ the parser

@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_mounts_none(raw):
    env = {} if raw is None else {surfaces.ENV: raw}
    assert surfaces.from_env(env) == frozenset()


def test_all_and_a_list():
    assert surfaces.from_env({surfaces.ENV: "all"}) == frozenset(surfaces.OPTIONAL)
    assert surfaces.from_env({surfaces.ENV: " Board , files,"}) == {"board", "files"}


@pytest.mark.parametrize("raw", ["bord", "all,board", "off", "true", "files;board"])
def test_a_name_it_does_not_know_refuses_to_load(raw):
    """A typo must not read as off, and 'all' is a value, not a member of a list."""
    with pytest.raises(surfaces.SurfacesConfigError) as err:
        surfaces.from_env({surfaces.ENV: raw})
    assert surfaces.ENV in str(err.value)


def test_every_optional_surface_has_a_path_here():
    """A surface added to OPTIONAL without a path in this file would go untested."""
    assert set(OPTIONAL_PATHS) == set(surfaces.OPTIONAL)


# ------------------------------------------------------------------ the real urlconf, per state

PROBE = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django
django.setup()
from django.conf import settings
from django.urls import Resolver404, resolve
out = {"surfaces": sorted(settings.MESHY_SURFACES), "paths": {}}
for p in json.loads(sys.argv[1]):
    try:
        resolve(p)
        out["paths"][p] = True
    except Resolver404:
        out["paths"][p] = False
print(json.dumps(out))
"""


def _probe(value: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != surfaces.ENV}
    if value is not None:
        env[surfaces.ENV] = value
    paths = list(OPTIONAL_PATHS.values()) + CORE_PATHS
    done = subprocess.run([sys.executable, "-c", PROBE, json.dumps(paths)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_production_default_mounts_the_core_and_no_optional_surface():
    got = _probe(None)
    assert got["surfaces"] == []
    for path in CORE_PATHS:
        assert got["paths"][path] is True, f"the core lost {path}"
    for name, path in OPTIONAL_PATHS.items():
        assert got["paths"][path] is False, f"{name} is mounted with {surfaces.ENV} unset"


def test_all_mounts_every_one():
    """The positive control of the test above: the same paths resolve when the surfaces are
    on, so an unresolved path there is the switch and not a path this file got wrong."""
    got = _probe("all")
    assert got["surfaces"] == sorted(surfaces.OPTIONAL)
    for name, path in OPTIONAL_PATHS.items():
        assert got["paths"][path] is True, f"{name}: {path} does not resolve even when on"
    for path in CORE_PATHS:
        assert got["paths"][path] is True


def test_one_surface_alone_mounts_only_itself():
    got = _probe("board")
    assert got["surfaces"] == ["board"]
    assert got["paths"][OPTIONAL_PATHS["board"]] is True
    for name, path in OPTIONAL_PATHS.items():
        if name != "board":
            assert got["paths"][path] is False, name


def test_a_bad_value_stops_the_server_from_loading():
    env = {k: v for k, v in os.environ.items() if k != surfaces.ENV}
    env[surfaces.ENV] = "dashbord"
    done = subprocess.run([sys.executable, "-c", PROBE, "[]"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode != 0 and "SurfacesConfigError" in done.stderr
