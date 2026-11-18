"""HZ Round 3 owner tests for delivery_bank, latency, and postbox_child bounded/private guarantees."""
from __future__ import annotations

import builtins
import json
import os
import stat
import subprocess
import sys
import tracemalloc
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
if str(PB) not in sys.path:
    sys.path.insert(0, str(PB))

import delivery_bank  # noqa: E402
import latency  # noqa: E402
import postbox_child  # noqa: E402


def _traced(fn):
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        out = fn()
        _, peak = tracemalloc.get_traced_memory()
        return out, peak
    finally:
        tracemalloc.stop()


def _write_sparse(path: Path, size: int) -> None:
    with open(path, "wb") as f:
        f.seek(size - 1)
        f.write(b"\n")


def _ident(tmp_path: Path, name: str = "seat-r3") -> dict:
    client = tmp_path / "client"
    (client / "tools" / "meshy_node").mkdir(parents=True, exist_ok=True)
    state = tmp_path / f"{name}.state.json"
    state.write_text(json.dumps({"peer_id": "peer-r3", "label": name}), encoding="utf-8")
    handled = tmp_path / f"{name}.handled.json"
    delivery_bank.atomic_json(str(handled), {"schema_version": 1, "identity": name, "handled": []})
    log = tmp_path / f"{name}.log"
    log.write_text("", encoding="utf-8")
    return {
        "name": name,
        "address": name,
        "python": sys.executable,
        "client": str(client),
        "state": str(state),
        "log": str(log),
        "base_url": "http://127.0.0.1:9",
        "handled_file": str(handled),
        "handled_private_acl_verified": True,
    }


def test_p1_a2_delivery_bank_reported_bounded_and_state_keyed(tmp_path):
    ident = _ident(tmp_path, name="seat-rep")
    ledger = Path(ident["state"] + ".read.json")
    reports = []
    delivery_bank._reported.clear()

    for mb in (18, 19, 20):
        _write_sparse(ledger, mb * 1048576)
        assert delivery_bank.read_ledger(ident, report=reports.append) == {}
    assert len(reports) == 1
    rl_keys = [k for k in delivery_bank._reported if isinstance(k, tuple) and k[:1] == ("read_ledger",)]
    assert len(rl_keys) == 1

    ledger.write_text(json.dumps({"schema_version": 1, "peer_id": "peer-r3", "reads": []}), encoding="utf-8")
    assert delivery_bank.read_ledger(ident, report=reports.append) == {}
    assert [k for k in delivery_bank._reported if isinstance(k, tuple) and k[:1] == ("read_ledger",)] == []

    _write_sparse(ledger, 21 * 1048576)
    assert delivery_bank.read_ledger(ident, report=reports.append) == {}
    assert len(reports) == 2

    for idx in range(delivery_bank.REPORTED_MAX + 500):
        delivery_bank._remember_reported(("synthetic", idx))
    assert len(delivery_bank._reported) == delivery_bank.REPORTED_MAX
    assert ("synthetic", 0) not in delivery_bank._reported
    assert ("synthetic", delivery_bank.REPORTED_MAX + 499) in delivery_bank._reported


def test_p1_a3_delivery_bank_and_latency_regular_nonblocking_private_io(tmp_path, monkeypatch):
    not_reg = tmp_path / "dir_target"
    not_reg.mkdir()
    with pytest.raises((OSError, ValueError)):
        delivery_bank._bounded_json_file(str(not_reg), 1024)
    with pytest.raises((OSError, ValueError)):
        delivery_bank.private_json(str(not_reg))
    assert delivery_bank.dispatch_line(str(not_reg), {"at": "now"}) is False

    real_file = tmp_path / "real.json"
    real_file.write_text('{"ok": 1}', encoding="utf-8")
    link_file = tmp_path / "link.json"
    try:
        os.symlink(real_file, link_file)
        can_symlink = True
    except (AttributeError, NotImplementedError, OSError):
        can_symlink = False
    if can_symlink:
        with pytest.raises(ValueError):
            delivery_bank._bounded_json_file(str(link_file), 1024)
        with pytest.raises(ValueError):
            delivery_bank.private_json(str(link_file))
        assert delivery_bank.dispatch_line(str(link_file), {"at": "now"}) is False
        with pytest.raises(ValueError):
            list(latency.iter_jsonl(str(link_file)))

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "sidecar.fifo"
        os.mkfifo(fifo)
        with pytest.raises((OSError, ValueError)):
            delivery_bank._bounded_json_file(str(fifo), 1024)
        assert delivery_bank.dispatch_line(str(fifo), {"at": "now"}) is False

    disp = tmp_path / "seat.log.dispatched.jsonl"
    disp.write_bytes(b'{"at":"seed"}\n')
    if os.name == "posix":
        os.chmod(disp, 0o644)
        assert delivery_bank.dispatch_line(str(disp), {"at": "now"}) is True
        assert stat.S_IMODE(os.lstat(disp).st_mode) == 0o600
    else:
        chmod_calls = []
        orig_fstat = os.fstat
        file_uid = os.lstat(disp).st_uid
        monkeypatch.setattr(os, "name", "posix", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: file_uid, raising=False)
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: chmod_calls.append((fd, mode)), raising=False)
        monkeypatch.setattr(
            os,
            "fstat",
            lambda fd: types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=file_uid, st_size=14, st_file_attributes=0)
            if isinstance(fd, int) else orig_fstat(fd),
        )
        assert delivery_bank.dispatch_line(str(disp), {"at": "now"}) is True
        assert chmod_calls and chmod_calls[-1][1] == 0o600


def test_gap_postbox_child_bounded_ps_and_unkillable_wait_timeout(tmp_path, monkeypatch):
    flood = tmp_path / "ps_flood.py"
    flood.write_text("import sys\nsys.stdout.buffer.write(b'1 0\\n' * 900000)\nsys.stdout.buffer.flush()\n", encoding="utf-8")
    out, peak = _traced(lambda: postbox_child._run_ps_bounded([sys.executable, "-S", str(flood)], max_bytes=65536))
    assert out is None and peak < 1048576

    ok_script = tmp_path / "ps_ok.py"
    ok_script.write_text("print('  10   1\\n  20  10\\n')", encoding="utf-8")
    table = postbox_child._run_ps_bounded([sys.executable, "-S", str(ok_script)], max_bytes=65536)
    assert table is not None and postbox_child.tree_below(10, table) == [20]

    class UnkillableChild:
        pid = 888881

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            assert timeout is not None, "postbox_child.terminate called unbounded wait()"
            raise subprocess.TimeoutExpired(["child"], timeout)

    monkeypatch.setattr(postbox_child, "GRACE_S", 0.02)
    monkeypatch.setattr(postbox_child, "POLL_S", 0.01)
    monkeypatch.setattr(postbox_child, "descendants", lambda _pid: [])
    assert postbox_child.terminate(UnkillableChild()) is False

    monkeypatch.setattr(postbox_child, "daemon_watch", lambda _pid: (lambda: True))
    monkeypatch.setattr(postbox_child.subprocess, "Popen", lambda _argv: UnkillableChild())
    monkeypatch.setattr(postbox_child.signal, "signal", lambda *_a: None)
    assert postbox_child.main(["postbox_child.py", "123", "--", "dummy"]) == 5


def test_gap_latency_bounded_jsonl_and_single_fd_atomic_read_ledger(tmp_path, monkeypatch):
    lines_file = tmp_path / "many.jsonl"
    lines_file.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(25)), encoding="utf-8")
    assert len(latency.jsonl(str(lines_file), max_records=7)) == 7

    big_cfg = tmp_path / "big_lat_cfg.json"
    _write_sparse(big_cfg, 2 * 1048576)
    with pytest.raises(delivery_bank._OversizeJsonFile):
        latency.load_identity(str(big_cfg), "seat-r3")

    ident = _ident(tmp_path, name="seat-lat")
    ledger_path = Path(ident["state"] + ".read.json")
    ledger_path.write_text(json.dumps({
        "schema_version": 1,
        "peer_id": "peer-r3",
        "reads": [{"key": "msg/k1", "version": 1, "at": "2026-11-01T07:00:00Z"}],
    }), encoding="utf-8")

    fake_node = types.SimpleNamespace(
        READ_LEDGER_SUFFIX=".read.json",
        READ_LEDGER_MAX=2000,
        ledger_records=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("re-opened via external client!")),
    )
    monkeypatch.setattr(latency, "import_node", lambda _i: fake_node)
    real_open_reg = delivery_bank.open_regular_read
    real_builtin_open = builtins.open
    reg_opens, builtin_opens = [], []

    def spy_open_reg(path, **kw):
        if str(path) == str(ledger_path):
            reg_opens.append(str(path))
        return real_open_reg(path, **kw)

    def spy_builtin_open(file, *a, **kw):
        if isinstance(file, (str, os.PathLike)) and str(file) == str(ledger_path):
            builtin_opens.append(str(file))
        return real_builtin_open(file, *a, **kw)

    monkeypatch.setattr(delivery_bank, "open_regular_read", spy_open_reg)
    monkeypatch.setattr(builtins, "open", spy_builtin_open)
    sources = {}
    reads, full = latency.read_ledger(ident, sources)
    assert sources["reads"] == "ok" and full is False and "msg/k1" in reads
    assert len(reg_opens) == 1 and builtin_opens == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
