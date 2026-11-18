"""The names the 2026-10-03 dead-import pass (#65, "deadimports") removed stay removed.

Unused imports and unread module constants were taken out of the db modules, peers.auth and
jobs.registry. Nothing read them, so nothing failed when they went, and nothing would fail if a
later edit put one back: an unused `psycopg` or `rls` import in a db module invites a raw connect
that skips `apps.core.dbconn`, and a second DEFAULT_DSN is a second spelling of the dev DSN that
`dbconn` owns. This census fails if one returns. CONTROL: the live names beside them are present,
so a module that failed to import cannot pass as clean.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

GONE = {
    "apps.peers.db": ("os", "psycopg", "rls", "DEFAULT_DSN"),
    "apps.wsgw.db": ("os", "rls", "DEFAULT_DSN"),
    "apps.skillsreg.db": ("psycopg", "rls"),
    "apps.authapi.db": ("psycopg", "rls"),
    "apps.jobs.registry": ("Callable",),
    "apps.peers.auth": ("JWT_ALGORITHM",),
}

KEPT = {
    "apps.peers.db": ("dbconn",),
    "apps.wsgw.db": ("dbconn",),
    # Its docstring keeps DEFAULT_DSN on purpose, as the value dbconn falls back to.
    "apps.skillsreg.db": ("dbconn", "DEFAULT_DSN", "dsn"),
    "apps.authapi.db": ("dbconn",),
    "apps.jobs.registry": ("Protocol",),
    "apps.peers.auth": ("authenticate_peer",),
}


@pytest.mark.parametrize("module_name", sorted(GONE))
def test_a_removed_name_has_not_come_back(module_name):
    module = importlib.import_module(module_name)
    back = [name for name in GONE[module_name] if hasattr(module, name)]
    assert back == [], f"{module_name} carries {back} again"
    # CONTROL: the module imported and its live names are there.
    missing = [name for name in KEPT[module_name] if not hasattr(module, name)]
    assert missing == [], f"{module_name} lost {missing}"
