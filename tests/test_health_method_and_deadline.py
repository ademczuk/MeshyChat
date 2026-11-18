"""Two health-endpoint findings from the adversarial review, now pinned.

1. METHOD. The module must reject non-GET methods with 405. Measured: every method reached
   the handler and returned the health body.

2. DEADLINE. The module claimed "returns within ~2s". Measured: a slow-drip Redis kept the
   probe running 21 s against a 2 s timeout, because `settimeout` is per-recv and the reader
   loops; and `Future.result(timeout=...)` does not CANCEL the worker, so three serial waits
   stack. The bound is now wall-clock for the whole probe set.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_non_get_methods_are_405(method):
    resp = getattr(Client(), method)("/v1/health")
    assert resp.status_code == 405, (
        f"{method.upper()} /v1/health returned {resp.status_code}; non-GET methods "
        "must answer 405 with an Allow header")


def test_get_still_works():
    """POSITIVE CONTROL. Without it, a handler that 405s everything would pass above."""
    resp = Client().get("/v1/health")
    assert resp.status_code in (200, 503), f"GET /v1/health returned {resp.status_code}"
    body = resp.json()
    for field in ("status", "version", "db", "redis", "blob"):
        assert field in body, f"health body is missing {field!r}"


def test_health_returns_within_its_declared_budget(monkeypatch):
    """A probe that hangs must not hold the request open past the budget.

    The control is the timing itself: a stalled probe is injected, and the endpoint must
    still answer. Without the deadline this waits for the stalled worker.
    """
    import meshychat.urls as u

    def stalled(*_a, **_k):
        time.sleep(30)
        return True

    monkeypatch.setattr(u, "probe_redis", stalled)
    started = time.monotonic()
    resp = Client().get("/v1/health")
    elapsed = time.monotonic() - started

    budget = u.PROBE_TIMEOUT * 3 + 2.0        # whole-set budget, plus slack for the runner
    assert elapsed < budget, (
        f"/v1/health took {elapsed:.1f}s with one stalled probe; budget is {budget:.1f}s. "
        "Future.result(timeout=...) does not cancel the worker, so the bound must be "
        "wall-clock for the set")
    assert resp.status_code in (200, 503)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
