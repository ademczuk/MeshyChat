"""What settlement does with a spawning record that has no gate proof (P2-2), on every platform.

tests/test_postbox_helper_settlement.py is POSIX only because the gate is. These are the record rules alone, which is what
Windows runs: a record with gated None comes from a platform with no gate, and one with no `gated` key comes from code
before it. Neither proves the helper never ran, and neither may stop a kind's lookups for good.
"""
import json
import subprocess
import sys
import time

import pytest

from tools.postbox import owedline, postbox_os

PY = sys.executable
MARGIN = postbox_os.HELPER_SPAWNING_EXPIRY_MARGIN_S


def dead_pid():
    p = subprocess.Popen([PY, "-I", "-c", "pass"])
    p.wait()
    return p.pid


def spawning(hdir, kind="owed", **over):
    """A spawning record left by a dead daemon. `over` replaces fields; a value of ... removes the field."""
    pid = dead_pid()
    doc = {"schema": 1, "state": "spawning", "kind": kind, "mutating": False, "pid": None, "pgid": None, "create_time": None,
           "create_time_known": False, "daemon_pid": pid, "daemon_create_time": 1.0, "started_at": time.time(),
           "timeout_s": 8.0}
    doc.update(over)
    doc = {k: v for k, v in doc.items() if v is not ...}
    path = hdir / ("h-%s-%d-1.json" % (kind, pid))
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def hdir(tmp_path):
    d = tmp_path / "log.jsonl.helpers"
    d.mkdir()
    return d


def settle(hdir, kind="owed"):
    return postbox_os.settle_helper_dir(str(hdir), only_kind=kind)


def test_a_no_gate_record_of_a_dead_daemon_is_abandoned_at_once(hdir):
    rec = spawning(hdir, gated=None)
    s = settle(hdir)
    assert s["settled"] is True and s["issues"] == [] and not rec.exists()
    assert s["reaped"] == [{"kind": "owed", "pid": None, "state": "spawning_abandoned"}]


def test_a_mutating_no_gate_record_stays_held(hdir):
    rec = spawning(hdir, kind="send_drain", gated=None, mutating=True)
    s = settle(hdir, "send_drain")
    assert s["settled"] is False and rec.exists()
    assert s["issues"] == ["mutating_helper_spawning_unsettled:send_drain:%s" % rec.name]


@pytest.mark.parametrize("gated", [..., False])
def test_a_record_with_no_proof_is_kept_inside_its_window(hdir, gated):
    rec = spawning(hdir, gated=gated)
    s = settle(hdir)
    assert s["settled"] is False and rec.exists()
    assert s["issues"] == ["helper_spawning_unsettled:owed:%s" % rec.name]


@pytest.mark.parametrize("gated", [..., False])
def test_a_record_with_no_proof_expires_past_its_timeout_and_the_margin(hdir, gated):
    rec = spawning(hdir, gated=gated, started_at=time.time() - 8.0 - MARGIN - 5.0)
    s = settle(hdir)
    assert s["settled"] is True and s["issues"] == [] and not rec.exists()
    assert s["reaped"] == [{"kind": "owed", "pid": None, "state": "spawning_expired"}]


def test_the_margin_counts_on_top_of_the_records_own_timeout(hdir, monkeypatch):
    rec = spawning(hdir, started_at=1000.0, timeout_s=8.0)
    monkeypatch.setattr(postbox_os.time, "time", lambda: 1000.0 + 8.0 + MARGIN)        # exactly at the limit: still kept
    assert settle(hdir)["settled"] is False and rec.exists()
    monkeypatch.setattr(postbox_os.time, "time", lambda: 1000.0 + 8.0 + MARGIN + 0.5)
    assert settle(hdir)["settled"] is True and not rec.exists()


def test_a_mutating_record_never_expires(hdir):
    rec = spawning(hdir, kind="send_drain", mutating=True, started_at=1.0)
    s = settle(hdir, "send_drain")
    assert s["settled"] is False and rec.exists()


@pytest.mark.parametrize("over", [
    {"timeout_s": ...}, {"timeout_s": None}, {"timeout_s": "8"}, {"timeout_s": True},
    {"started_at": ...}, {"started_at": None}, {"started_at": "1"}, {"started_at": True},
])
def test_a_record_that_gives_no_bound_is_kept(hdir, over):
    rec = spawning(hdir, **{"started_at": 1.0, **over})
    s = settle(hdir)
    assert s["settled"] is False and rec.exists()


@pytest.mark.parametrize("windows,want", [(True, None), (False, True)])
def test_run_bounded_records_gated_none_only_where_there_is_no_gate(hdir, monkeypatch, windows, want):
    class Stop(Exception):
        pass

    seen = {}

    def begin(helper_dir, **kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(postbox_os, "IS_WINDOWS", windows)
    monkeypatch.setattr(postbox_os, "_begin_helper_record", begin)
    with pytest.raises(Stop):
        postbox_os.run_bounded([PY, "-I", "-c", "pass"], helper_dir=str(hdir), helper_kind="owed")
    assert seen["gated"] is want


def test_begin_helper_record_keeps_none_apart_from_false(hdir):
    path, rec = postbox_os._begin_helper_record(str(hdir), kind="owed", mutating=False, timeout=8, spawn_ts=time.time(),
                                                gated=None)
    assert rec["gated"] is None and json.loads(open(path, encoding="utf-8").read())["gated"] is None
    _path, rec = postbox_os._begin_helper_record(str(hdir), kind="owed", mutating=False, timeout=8, spawn_ts=time.time())
    assert rec["gated"] is False


def test_a_lookup_goes_ahead_over_a_dead_no_gate_record_and_stops_over_a_kept_one(tmp_path, hdir):
    """The reason for the rule: one crash on Windows must not end this identity's owed lookups."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"owed": []}', "")

    ident = {"name": "t", "python": PY, "base_url": "http://127.0.0.1:9", "state": str(tmp_path / "s.json"),
             "log": str(tmp_path / "log.jsonl"), "client": str(tmp_path)}
    kept = spawning(hdir)
    assert owedline._call(ident, "2026-11-03T00:00:00Z", fake_run) is None and calls == []
    kept.unlink()
    spawning(hdir, gated=None)
    owedline._call(ident, "2026-11-03T00:00:00Z", fake_run)
    assert len(calls) == 1
