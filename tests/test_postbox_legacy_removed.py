"""An old postbox identity is skipped after the retired source path was removed.

These tests use only temporary config, spool, status and channel streams. They do not start a listener,
contact a server or read an existing postbox configuration.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
POSTBOX = ROOT / "tools" / "postbox"


def _legacy_identity(tmp_path):
    return {"name": "old-seat", "source": "legacy", "enabled": True,
            "log": str(tmp_path / "old-seat.log"), "state": str(tmp_path / "missing-state.json"),
            "client": str(tmp_path), "python": sys.executable, "cli": "claude"}


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, POSTBOX / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StopLoop(Exception):
    pass


def _stop_sleep(_s):
    raise _StopLoop


def test_daemon_skips_an_old_legacy_config_and_records_why(tmp_path, monkeypatch):
    """MUTANT H1: remove the daemon's `source == "legacy"` skip; this test then reaches the missing-state branch."""
    pb = _load("postbox_legacy_removed_daemon", "postbox.py")
    ident = _legacy_identity(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"base_url": "http://fixture", "identities": [ident]}), encoding="utf-8")
    monkeypatch.setattr(pb, "HERE", str(tmp_path))
    monkeypatch.setattr(pb, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(pb, "STATUS", str(tmp_path / "status.json"))
    monkeypatch.setattr(pb, "ERR", str(tmp_path / "postbox.err.log"))
    monkeypatch.setattr(pb, "acquire_daemon_lock", lambda _fd: True)
    monkeypatch.setattr(pb.time, "sleep", _stop_sleep)
    monkeypatch.setattr(sys, "argv", ["postbox.py"])
    with pytest.raises(_StopLoop):
        pb.main()
    reason = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))["old-seat"]
    assert reason["listener"] == "skipped"
    assert "removed from the postbox on 2026-10-03" in reason["skip_reason"] and "tools.meshy_node adopt" in reason["skip_reason"]
    notes = Path(pb.ERR).read_text(encoding="utf-8")
    assert notes.count("old-seat: legacy source was retired on 2026-09-28 and removed from the postbox on 2026-10-03") == 1


def test_channel_skips_an_old_legacy_config_without_a_read_command(tmp_path, monkeypatch, capsys):
    """MUTANT H2: append a legacy identity to `found`; this test then starts a tail instead of one skip event."""
    ident = _legacy_identity(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"base_url": "http://fixture", "identities": [ident]}), encoding="utf-8")
    monkeypatch.setenv("MESHY_IDENTITY", "old-seat")
    channel = _load("postbox_legacy_removed_channel", "meshy_channel.py")
    monkeypatch.setattr(channel, "HERE", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"method":"notifications/initialized"}\n'))
    channel.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    pushed = [event for event in events if event.get("method") == "notifications/claude/channel"]
    assert len(pushed) == 1
    content = pushed[0]["params"]["content"]
    assert "old-seat" in content and "removed from the postbox on 2026-10-03" in content and "tools.meshy_node adopt" in content
    assert "mem_cli" not in content and "tools.meshy_node read" not in content


def test_waiter_skips_an_old_legacy_config_without_a_read_command(tmp_path, monkeypatch, capsys):
    """MUTANT H3: remove the waiter's `source == "legacy"` guard; this test then arms the retired identity."""
    ident = _legacy_identity(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"base_url": "http://fixture", "identities": [ident]}), encoding="utf-8")
    waiter = _load("postbox_legacy_removed_waiter", "wait_next.py")
    monkeypatch.setattr(waiter, "HERE", str(tmp_path))
    monkeypatch.setattr(waiter, "stdout_discarded", lambda: False)
    monkeypatch.setattr(sys, "argv", ["wait_next.py", "old-seat", "--once"])
    assert waiter.main() == 2
    line = capsys.readouterr().out
    assert "old-seat" in line and "removed from the postbox on 2026-10-03" in line and "tools.meshy_node adopt" in line
    assert "mem_cli" not in line and "tools.meshy_node read" not in line
