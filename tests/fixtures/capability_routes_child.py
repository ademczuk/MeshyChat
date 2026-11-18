"""The served app answering the mint and revoke routes, for tests/test_capability_mint_route.py
(divergence 040 slice 5). Run ONLY as a subprocess, with whatever MESHY_CAPABILIT* variables the
parent sets.

It boots the way daphne does (`import meshychat.asgi`, whose get_asgi_application() runs
django.setup()), then POSTs `{}` to each route with no credentials through the project's own
middleware and urlconf. One line out, `ROUTES_RESULT <json>`:

  before   the watched modules in sys.modules after startup, before any request
  after    the same, after both requests
  answers  {path: [status, error code]}

The watched modules are what "tokens off imports nothing" is about: the verifier, the mint and
revoke module, and biscuit_auth.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

WATCHED = ("apps.memoryskills.capability", "apps.memoryskills.capability_issue", "biscuit_auth")


def loaded() -> list[str]:
    return sorted(m for m in WATCHED if sys.modules.get(m) is not None)


import meshychat.asgi  # noqa: E402,F401  - the served entrypoint; django.setup() runs here

before = loaded()

from django.test import Client  # noqa: E402

client = Client()
answers = {}
for path in ("/v1/memory/capabilities", "/v1/memory/capabilities/revoke"):
    response = client.post(path, data="{}", content_type="application/json")
    try:
        code = json.loads(response.content).get("error")
    except (ValueError, AttributeError):
        code = None
    answers[path] = [response.status_code, code]

print("ROUTES_RESULT " + json.dumps({"before": before, "after": loaded(), "answers": answers}),
      flush=True)
