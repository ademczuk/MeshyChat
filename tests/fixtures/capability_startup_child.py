"""The served app's startup with object-capability tokens configured, for
tests/test_capability_keys.py (T11, divergence 040 slice 4). Run ONLY as a subprocess.

It boots the way daphne and uvicorn do: `import meshychat.asgi`, whose
get_asgi_application() runs django.setup(), with whatever MESHY_CAPABILIT* variables the
parent sets. A refused startup is this process exiting non-zero with the error on stderr, as a
server's would. Otherwise it prints one line, `CAPABILITY_RESULT <json>`:

  biscuit   whether `biscuit_auth` is in sys.modules after startup
  kid       the loaded key store's active kid, or null when no store was loaded
  kids      the verify set's kids
  reads     only when CAPABILITY_CHILD_RECORD=1: every environment variable NAME read through
            os.environ (or os.getenv) during startup, in order. The recorder replaces
            os.environ before anything else is imported, so settings.py and every ready()
            read through it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

READS: list[str] = []

if os.environ.get("CAPABILITY_CHILD_RECORD") == "1":
    class _Recording(dict):
        """os.environ as a dict that writes down every name read from it."""

        def __getitem__(self, key):
            READS.append(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            READS.append(key)
            return super().get(key, default)

        def __contains__(self, key):
            READS.append(key)
            return super().__contains__(key)

    os.environ = _Recording(os.environ)          # os.getenv reads this same global

if os.environ.get("CAPABILITY_CHILD_BREAK_BISCUIT") == "1":
    sys.modules["biscuit_auth"] = None           # `import biscuit_auth` now raises ImportError

import meshychat.asgi  # noqa: E402,F401  - the served entrypoint; django.setup() runs here

from apps.memoryskills import capability_keys  # noqa: E402

store = capability_keys.current()
print("CAPABILITY_RESULT " + json.dumps({
    "biscuit": sys.modules.get("biscuit_auth") is not None,
    "kid": store.active_kid if store else None,
    "kids": sorted(store.kids) if store else [],
    "reads": READS,
}), flush=True)
