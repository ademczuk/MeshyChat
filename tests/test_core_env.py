"""apps/core/env.py's numeric readers, and the two pieces the rate limiters now share.

WHAT THIS PINS. `env.number` was apps/core/peer_limit.py's private `_parse`, which
apps/core/redeem_limit.py imported across modules; `env.rate_config` is the (burst, refill)
shape both limiters' `config()` spelled out; `peer_limit.stale` and
`peer_limit.retry_after_seconds` were a predicate and a rounding each module carried. The
limiters' own tests (tests/test_peer_rate_limit.py, tests/test_redeem_rate_limit.py) prove a
bad value refuses and a wait rounds up on the wire; this file pins what they do not: the
refusal MESSAGES, which name the setting and the defect, the setting-then-environment-then-
default precedence, the helpers' edges, and that each limiter's wire answer IS the helper's.

Run: .venv/Scripts/python.exe -m pytest tests/test_core_env.py -q
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.test import override_settings  # noqa: E402

from apps.core import env, peer_limit, redeem_limit  # noqa: E402

NAME = "MESHY_TEST_RATE"


@pytest.mark.parametrize("raw,kind,message", [
    # A fractional burst is refused, never truncated to int(1.5) == 1 (review INFO 7).
    (1.5, int, f"{NAME} must be a whole number, got 1.5"),
    ("many", int, f"{NAME} must be a int, got 'many'"),
    ("1.5", int, f"{NAME} must be a int, got '1.5'"),
    (None, float, f"{NAME} must be a float, got None"),
    ("nan", float, f"{NAME} must be a finite number above zero, got 'nan'"),
    ("inf", float, f"{NAME} must be a finite number above zero, got 'inf'"),
    (0, int, f"{NAME} must be a finite number above zero, got 0"),
    (-1, float, f"{NAME} must be a finite number above zero, got -1"),
    (0.5, int, f"{NAME} must be a whole number, got 0.5"),
    # A bool is an int in Python; as a setting it is a mistake, not a burst of 1.
    (True, int, f"{NAME} must be a finite number above zero, got True"),
])
def test_number_refuses_with_the_message_that_names_the_setting_and_the_defect(raw, kind,
                                                                              message):
    with pytest.raises(ImproperlyConfigured, match=re.escape(message)) as refused:
        env.number(NAME, raw, kind)
    assert str(refused.value) == message


@pytest.mark.parametrize("raw,kind,value", [
    ("7", int, 7), (7, int, 7), (3.0, int, 3), ("2.5", float, 2.5), (1, float, 1.0),
    ("1e-3", float, 0.001),
])
def test_number_accepts_a_finite_value_above_zero_as_its_kind(raw, kind, value):
    got = env.number(NAME, raw, kind)
    assert got == value and type(got) is kind, (got, type(got))


def test_rate_config_reads_the_setting_then_the_environment_then_the_default(monkeypatch):
    """`env.setting`'s precedence, applied to both names, each validated: a setting wins over
    the environment, the environment over the default, and a setting explicitly set to an
    invalid value refuses rather than falling through to a valid environment."""
    burst_name, rate_name = f"{NAME}_BURST", f"{NAME}_PER_SEC"
    read = lambda: env.rate_config(burst_name, rate_name, 12, 0.25)  # noqa: E731
    monkeypatch.delenv(burst_name, raising=False)
    monkeypatch.delenv(rate_name, raising=False)
    assert read() == (12, 0.25)
    monkeypatch.setenv(burst_name, "4")
    monkeypatch.setenv(rate_name, "0.5")
    assert read() == (4, 0.5)
    with override_settings(**{burst_name: 9, rate_name: "2"}):
        assert read() == (9, 2.0)
    with override_settings(**{burst_name: 0}), pytest.raises(ImproperlyConfigured,
                                                              match=burst_name):
        read()
    monkeypatch.setenv(rate_name, "fast")
    with pytest.raises(ImproperlyConfigured, match=f"{rate_name} must be a float"):
        read()


def test_each_limiter_reads_its_own_names_through_rate_config(monkeypatch):
    """The environment half of the precedence, on the real names: a value in the environment
    reaches each `config()` (settings.py defines neither name, so the environment is read)."""
    monkeypatch.setenv("MESHY_PEER_RATE_BURST", "33")
    monkeypatch.setenv("MESHY_REDEEM_RATE_PER_SEC", "0.125")
    assert peer_limit.config() == (33, peer_limit.DEFAULT_REFILL_PER_SEC)
    assert redeem_limit.config() == (redeem_limit.DEFAULT_BURST, 0.125)


@pytest.mark.parametrize("wait,seconds", [
    (1.5, 2), (2.0, 2), (0.5, 1), (0.0, 1), (1e-12, 1), (2.1, 3),
    (2.0 + 1e-10, 2),            # floating-point noise above a whole second is that second
    (2.0 + 1e-8, 3),             # a real excess, however small, is the next second
    (30.0, 30),
])
def test_retry_after_seconds_rounds_up_to_whole_seconds_and_never_below_one(wait, seconds):
    got = peer_limit.retry_after_seconds(wait)
    assert got == seconds and type(got) is int, (wait, got)


def test_both_limiters_answer_retry_after_through_the_shared_rounding():
    """Burst 1, refill 0.4/s: the second request waits 2.5 s, and both the peer refusal's
    `retry_after` and the redeem 429's header say what `retry_after_seconds(2.5)` says."""
    assert peer_limit.retry_after_seconds(2.5) == 3
    with override_settings(MESHY_PEER_RATE_BURST=1, MESHY_PEER_RATE_PER_SEC=0.4):
        peer_limit.admit("peer-a", transport="http")
        with pytest.raises(peer_limit.PeerRateLimited) as refused:
            peer_limit.admit("peer-a", transport="http")
        assert refused.value.retry_after == 3
        assert peer_limit.http_response(refused.value)["Retry-After"] == "3"
    with override_settings(MESHY_REDEEM_RATE_BURST=1, MESHY_REDEEM_RATE_PER_SEC=0.4):
        assert redeem_limit.admit("203.0.113.9") is None
        response = redeem_limit.admit("203.0.113.9")
        assert response is not None and response.status_code == 429
        assert response["Retry-After"] == "3"


def test_stale_is_true_for_no_table_and_for_a_table_built_on_another_burst_or_rate():
    """The predicate both `_current`s use to rebuild a table when the configured value
    changed, on either kind of table: PeerBuckets and IpBuckets both carry `burst` and
    `refill_per_sec`."""
    for build in (peer_limit.PeerBuckets, redeem_limit.IpBuckets):
        table = build(5, 2.5)
        assert peer_limit.stale(None, 5, 2.5) is True
        assert peer_limit.stale(table, 5, 2.5) is False
        assert peer_limit.stale(table, 6, 2.5) is True, build
        assert peer_limit.stale(table, 5, 2.0) is True, build


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
