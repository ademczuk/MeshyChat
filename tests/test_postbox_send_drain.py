"""tools/postbox send-operation drain (PR 2, `send --op-id`).

The postbox asks each identity's own client to reconcile pending send operations. These tests pin the cost and
compatibility rules: no client command when nothing is pending, a backoff between attempts, and a client older than
`send drain` noted once instead of on every poll.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POSTBOX = ROOT / "tools" / "postbox" / "postbox.py"


@pytest.fixture
def pb(tmp_path, monkeypatch):
    if "msvcrt" not in sys.modules:
        try:
            import msvcrt  # noqa: F401
        except ImportError:
            monkeypatch.setitem(sys.modules, "msvcrt", types.ModuleType("msvcrt"))
    spec = importlib.util.spec_from_file_location("postbox_drain_under_test", POSTBOX)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATUS", str(tmp_path / "status.json"))
    monkeypatch.setattr(mod, "ERR", str(tmp_path / "postbox.err.log"))
    mod._DRAIN_NEXT.clear()
    mod._DRAIN_NOTICE_NEXT.clear()
    return mod


def _ident(tmp_path):
    return {"name": "seat-prod", "python": "python", "state": str(tmp_path / "state.json"),
            "client": str(tmp_path), "base_url": "http://x"}


def _op(tmp_path, op_id, state):
    folder = tmp_path / "state.json.ops"
    folder.mkdir(exist_ok=True)
    entry = {"schema": 1, "op_id": op_id, "state": state}
    if state == "succeeded":
        entry["guard_counted"] = True                      # a settled success: its send-guard accounting is done
    (folder / f"{op_id}.json").write_text(json.dumps(entry), encoding="utf-8")


def _runs(monkeypatch, pb, rc=0, stdout=b"", stderr=b"", settle=None):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if settle:
            settle()
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    monkeypatch.setattr(pb.subprocess, "run", fake_run)
    return calls


def test_no_journal_or_only_terminal_ops_runs_no_client_command(pb, tmp_path, monkeypatch):
    calls = _runs(monkeypatch, pb)
    pb.drain_send_operations(_ident(tmp_path))
    _op(tmp_path, "done", "succeeded")
    _op(tmp_path, "no", "refused")
    pb.drain_send_operations(_ident(tmp_path))
    assert calls == []


def test_a_drain_cursor_is_not_a_pending_operation(pb, tmp_path, monkeypatch):
    folder = tmp_path / "state.json.ops"
    folder.mkdir()
    (folder / ".drain-cursor.json").write_text(json.dumps({"op_id": "settled"}), encoding="utf-8")
    _op(tmp_path, "settled", "succeeded")
    calls = _runs(monkeypatch, pb)

    pb.drain_send_operations(_ident(tmp_path))

    assert calls == []


def test_a_pending_op_runs_the_drain_and_backs_off_while_it_stays_pending(pb, tmp_path, monkeypatch):
    _op(tmp_path, "open", "uncertain")
    calls = _runs(monkeypatch, pb)
    clock = [1000.0]
    monkeypatch.setattr(pb.time, "time", lambda: clock[0])
    pb.drain_send_operations(_ident(tmp_path))
    assert len(calls) == 1 and calls[0][3:5] == ["send", "drain"]
    pb.drain_send_operations(_ident(tmp_path))
    assert len(calls) == 1, "a second poll inside the backoff must not run the client again"
    clock[0] += 2.1
    pb.drain_send_operations(_ident(tmp_path))
    assert len(calls) == 2
    clock[0] += 2.1                       # attempt 2 waits 4 s, not 2
    pb.drain_send_operations(_ident(tmp_path))
    assert len(calls) == 2
    clock[0] += 60
    for _ in range(6):
        pb.drain_send_operations(_ident(tmp_path))
        clock[0] += 31                    # the backoff is capped at 30 s
    assert len(calls) == 8                # two before the loop, then every 31 s step runs: the cap is 30 s


def test_a_settled_op_clears_the_backoff(pb, tmp_path, monkeypatch):
    _op(tmp_path, "open", "uncertain")
    calls = _runs(monkeypatch, pb, settle=lambda: _op(tmp_path, "open", "succeeded"))
    pb.drain_send_operations(_ident(tmp_path))
    assert len(calls) == 1 and "seat-prod" not in pb._DRAIN_NEXT


def test_drain_summary_with_unsettled_work_is_noted_at_most_hourly(pb, tmp_path, monkeypatch):
    _op(tmp_path, "open", "uncertain")
    calls = _runs(monkeypatch, pb, stdout=b'{"event":"send_drain","succeeded":0,"refused":1,"uncertain":2,"refused_ids":["no"],"uncertain_ids":["open","slow"]}\n')
    clock = [1000.0]
    monkeypatch.setattr(pb.time, "time", lambda: clock[0])
    pb.drain_send_operations(_ident(tmp_path))
    clock[0] += 3
    pb.drain_send_operations(_ident(tmp_path))
    log = Path(pb.ERR).read_text(encoding="utf-8")
    assert len(calls) == 2 and log.count("refused=1 uncertain=2") == 1


# What a client older than `send drain` really prints for `send drain` (main 11a86f54, measured on HQB 23 Oct 2026):
# argparse reads `drain` as the send path and stops at the missing required options.
REAL_OLD_CLIENT = (b"usage: python -m tools.meshy_node send [-h] --base-url BASE_URL\n"
                   b"python -m tools.meshy_node send: error: the following arguments are required: --to, --thread\n")


@pytest.mark.parametrize("err", [REAL_OLD_CLIENT, b"usage: ...\nmeshy_node: error: unrecognized arguments: drain\n"],
                         ids=["real-old-client", "unrecognized-arguments"])
def test_a_client_without_send_drain_is_noted_once(pb, tmp_path, monkeypatch, err):
    _op(tmp_path, "open", "uncertain")
    calls = _runs(monkeypatch, pb, rc=2, stderr=err)
    clock = [1000.0]
    monkeypatch.setattr(pb.time, "time", lambda: clock[0])
    for _ in range(5):
        pb.drain_send_operations(_ident(tmp_path))
        clock[0] += 10
    assert len(calls) == 1, "an old client is asked once, then left for an hour"
    log = Path(pb.ERR).read_text(encoding="utf-8") if Path(pb.ERR).exists() else ""
    assert log.count("has no `send drain`") == 1
    clock[0] += pb.DRAIN_OLD_CLIENT_S
    pb.drain_send_operations(_ident(tmp_path))
    assert len(calls) == 2
    log = Path(pb.ERR).read_text(encoding="utf-8")
    assert log.count("has no `send drain`") == 1, "a second refusal an hour later is not noted again"
