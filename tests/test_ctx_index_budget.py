"""The transcript index's resident budget comes from MESHY_CTX_INDEX_BUDGET_BYTES (divergence 108).

HQD serves production in about 10 GB beside other work. On 3 Nov 2026 its production server held 1,823 MB private and
182 MB resident, and a full 50,000-row shard is about 196 MiB, so 2 GiB of resident shards is not a budget that box
can keep. The setting lets one box run smaller while every other box keeps the 2 GiB default. No database here.
"""
from __future__ import annotations

import logging

import pytest

from apps.meshsem import ctxindex

ENV = "MESHY_CTX_INDEX_BUDGET_BYTES"


@pytest.fixture(autouse=True)
def _unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def test_unset_keeps_the_two_gib_default():
    assert ctxindex.configured_budget() == ctxindex.BUDGET_BYTES == 2 * 1024 ** 3
    assert ctxindex.ShardCache().budget == ctxindex.BUDGET_BYTES


def test_a_whole_number_of_bytes_at_or_above_the_floor_is_the_budget(monkeypatch):
    monkeypatch.setenv(ENV, "1073741824")
    assert ctxindex.configured_budget() == 1024 ** 3
    assert ctxindex.ShardCache().budget == 1024 ** 3
    monkeypatch.setenv(ENV, str(ctxindex.BUDGET_BYTES_MIN))
    assert ctxindex.configured_budget() == ctxindex.BUDGET_BYTES_MIN


@pytest.mark.parametrize("raw", ["1GB", "1.5e9", "lots", "-1073741824", "0",
                                 str(ctxindex.BUDGET_BYTES_MIN - 1)])
def test_a_value_that_is_not_whole_bytes_or_is_under_the_floor_keeps_the_default_and_says_so(monkeypatch, caplog, raw):
    monkeypatch.setenv(ENV, raw)
    with caplog.at_level(logging.WARNING, logger="meshychat.meshsem.ctxindex"):
        assert ctxindex.configured_budget() == ctxindex.BUDGET_BYTES
    assert any(ENV in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_an_explicit_budget_still_wins_over_the_setting(monkeypatch):
    monkeypatch.setenv(ENV, "1073741824")
    assert ctxindex.ShardCache(budget_bytes=12345).budget == 12345


def test_the_configured_budget_is_what_the_cache_evicts_to(monkeypatch):
    """The setting is the cap put() holds, not only a reported number: with a 300 MiB budget, a second 200 MiB shard
    evicts the first."""
    monkeypatch.setenv(ENV, str(300 * 1024 ** 2))
    cache = ctxindex.ShardCache()

    class _Shard:
        def __init__(self, key, nbytes):
            self.key, self.nbytes = key, nbytes

    assert cache.put(_Shard("a", 200 * 1024 ** 2)) and cache.keys() == ["a"]
    assert cache.put(_Shard("b", 200 * 1024 ** 2)) and cache.keys() == ["b"]
    assert cache.bytes == 200 * 1024 ** 2
