"""`/v1/health` advertises `object-capability-v1` only while tokens are on (B07 slice 6, Q11;
docs/DIVERGENCES.md 040; design docs/design/biscuit-capabilities.md 3.1 and 7, Q11).

Critique C8: a server that does not support tokens ignores an unknown header, so a client that
sent `Meshychat-Capability` there would be authorised by its DB grants and believe the token was
in force. A client therefore refuses to send the header to a server that does not advertise the
field (tools/meshy_node, tests/test_meshy_node_capability.py). This health response is the
server's half.

  * Tokens OFF (MESHY_CAPABILITIES unset, the default): the body has seven baseline fields in
    fixed order and then rule B9's three (fin/post-r10, operator decision
    2026-09-25), exactly tests/test_health.py's pin. The field is ABSENT, not an empty list
    (divergence 040 records the choice; the design names none).
  * Tokens ON, a root key store loaded: those ten, then `"capabilities":
    ["object-capability-v1"]` LAST, compact like the rest of the body. The conditional
    field is last, so the always-present ten are one fixed prefix either way (the order
    chosen when fin/post-r10 met integ/r11 in meshychat/urls.py).
  * The flag on with NO key store (a state ready() refuses to start in, reachable in a test):
    nothing is advertised, since every token route answers E_CAPABILITY_UNAVAILABLE then. The
    advertisement is the same test the routes make, so it never promises what they refuse.

The status and the probes are unchanged either way (the advertisement is not a dependency).

Run:  pytest tests/test_capability_health.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

from tests.capability_world import world_context  # noqa: E402

#: The fixed health fields, as tests/test_health.py pins them.
HEALTH_FIELDS = ["status", "version", "git_sha", "git_dirty", "db", "redis", "blob"]
#: Rule B9's three, after `blob` and before `capabilities`, as tests/test_health.py pins them.
B9_FIELDS = ["code_sha256", "worker_code_sha256", "worker_code_stale"]
ALWAYS = HEALTH_FIELDS + B9_FIELDS
ADVERTISED = ["object-capability-v1"]


def _health():
    response = Client(HTTP_HOST="testserver").get("/v1/health")
    raw = response.content.decode("utf-8")
    return response.status_code, raw, json.loads(raw)


def test_health_advertises_nothing_while_tokens_are_off():
    from tests.test_health import B9_FIELDS as PINNED_B9
    from tests.test_health import HEALTH_FIELDS as PINNED

    assert HEALTH_FIELDS == PINNED, "this file's copy of the pin drifted from test_health's"
    assert B9_FIELDS == PINNED_B9, "this file's copy of the B9 pin drifted from test_health's"
    with override_settings(MESHY_CAPABILITIES=False):
        status, raw, body = _health()
    assert list(body) == ALWAYS, list(body)
    assert "capabilities" not in raw, raw
    assert status in (200, 503)


def test_health_advertises_object_capability_v1_while_tokens_are_on():
    with world_context():
        with override_settings(MESHY_CAPABILITIES=True):
            status, raw, body = _health()
        # The flag decides, not only the store: the SAME loaded store with the flag off
        # advertises nothing (the routes answer UNAVAILABLE then too).
        with override_settings(MESHY_CAPABILITIES=False):
            off = _health()[2]
    assert list(body) == ALWAYS + ["capabilities"], list(body)
    assert body["capabilities"] == ADVERTISED
    assert raw.endswith(',"capabilities":["object-capability-v1"]}'), raw[-80:]
    assert status in (200, 503)
    assert list(off) == ALWAYS, list(off)


def test_the_flag_without_a_key_store_advertises_nothing():
    from apps.memoryskills import capability_keys

    previous = capability_keys.install(None)
    try:
        with override_settings(MESHY_CAPABILITIES=True):
            _status, raw, body = _health()
    finally:
        capability_keys.install(previous)
    assert list(body) == ALWAYS and "capabilities" not in raw, raw


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
