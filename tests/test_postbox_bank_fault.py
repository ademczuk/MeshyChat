"""Audit finding 3 (a reviewer 55836e; oxxxxxx 10b8bd): a retarget left a Codex delivery bank at mode 0644.

On 7-8 Nov 2026 the HZ admin's Codex target changed, and its bank checkpoint `<log>.codex.offset.bank.json` was edited
by hand to name the new consumer. The edit left the file 0644, which the bank refuses, so run_codex_delivery crashed
every 60 s for 13 hours (1,610 times), recorded only as a status field and in an error log nobody read.

Two fixes, tested here, database-free:
  1. tools/postbox/bank_retarget.py does the retarget through delivery_bank.atomic_json: the consumer is rewritten
     and the file is owner-only (0600 on POSIX), and the bank then loads it.
  2. postbox.guarded() writes `<log>.<loop>.fault.json` after FAULT_AFTER crashes in a row, and says so once in the
     error log.
Run:  python -m pytest tests/test_postbox_bank_fault.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
if str(PB) not in sys.path:
    sys.path.insert(0, str(PB))

import delivery_bank  # noqa: E402

POSIX = os.name == "posix"


def _load(name):
    spec = importlib.util.spec_from_file_location("pbtest_bankfault_" + name, PB / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pb(tmp_path):
    mod = _load("postbox")
    mod.STATUS = str(tmp_path / "status.json")
    mod.ERR = str(tmp_path / "postbox.err.log")
    mod._status.clear()
    return mod


def _ident(tmp_path, target="019a0000-0000-7000-8000-00000000000b"):
    log = tmp_path / "spool" / "codex-seat.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    return {"name": "codex-seat", "log": str(log), "target": target}


def _checkpoint(ident, consumer, mode=0o644):
    path = ident["log"] + ".codex.offset.bank.json"
    Path(path).write_text(json.dumps({"schema_version": 2, "consumer": consumer, "identity": ident["name"],
                                      "offset": 7, "delivered": ["t1"], "held": []}), encoding="utf-8")
    if POSIX:
        os.chmod(path, mode)
    return path


# ------------------------------------------------------------------------------------------ 1. bank_retarget

def test_a_retarget_rewrites_the_consumer_and_the_bank_loads_it(tmp_path):
    """The hand edit's job, done safely: the consumer names the new target, the offset and delivered tokens stay, the
    file is 0600 on POSIX, and Bank() loads it (the 8 Nov file made Bank() raise on every pass)."""
    br = _load("bank_retarget")
    ident = _ident(tmp_path)
    path = _checkpoint(ident, "codex:019a0000-0000-7000-8000-00000000000a")
    code, line = br.retarget(ident)
    assert code == 0 and line.startswith("rewrote"), line
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["consumer"] == "codex:" + ident["target"] and data["offset"] == 7 and data["delivered"] == ["t1"]
    if POSIX:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    bank = delivery_bank.Bank(ident, ident["log"] + ".codex.offset", 0, "codex:" + ident["target"])
    assert bank.offset == 7 and bank.delivered == {"t1"}


@pytest.mark.skipif(not POSIX, reason="file modes are POSIX")
def test_a_0644_checkpoint_with_the_right_consumer_is_rewritten_0600(tmp_path):
    """The exact 8 Nov state: consumer already right, mode 0644. The bank refuses it; the command repairs the mode."""
    br = _load("bank_retarget")
    ident = _ident(tmp_path)
    path = _checkpoint(ident, "codex:" + ident["target"], mode=0o644)
    with pytest.raises(ValueError):
        delivery_bank.Bank(ident, ident["log"] + ".codex.offset", 0, "codex:" + ident["target"])
    code, line = br.retarget(ident)
    assert code == 0 and "mode 0o644 -> 0o600" in line, line
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    delivery_bank.Bank(ident, ident["log"] + ".codex.offset", 0, "codex:" + ident["target"])


def test_nothing_to_do_and_dry_run_write_nothing(tmp_path):
    br = _load("bank_retarget")
    ident = _ident(tmp_path)
    assert br.retarget(ident)[1].startswith("nothing to do: no checkpoint")
    path = _checkpoint(ident, "codex:old-thread")
    before = Path(path).read_bytes()
    code, line = br.retarget(ident, dry_run=True)
    assert code == 0 and line.startswith("dry run") and Path(path).read_bytes() == before
    br.retarget(ident)
    code, line = br.retarget(ident)
    assert code == 0 and line.startswith("nothing to do"), line


def test_a_retarget_refuses_another_identitys_checkpoint_a_link_and_a_non_checkpoint(tmp_path):
    br = _load("bank_retarget")
    ident = _ident(tmp_path)
    path = Path(ident["log"] + ".codex.offset.bank.json")
    path.write_text(json.dumps({"schema_version": 2, "consumer": "codex:x", "identity": "someone-else",
                                "offset": 0, "delivered": []}), encoding="utf-8")
    assert br.retarget(ident)[0] == 1
    path.write_text(json.dumps({"consumer": "codex:x", "identity": ident["name"]}), encoding="utf-8")
    assert br.retarget(ident)[0] == 1
    path.write_text("not json", encoding="utf-8")
    assert br.retarget(ident)[0] == 1
    if POSIX:
        real = tmp_path / "elsewhere.json"
        real.write_text("{}", encoding="utf-8")
        path.unlink()
        path.symlink_to(real)
        code, line = br.retarget(ident)
        assert code == 1 and "regular file" in line, line
        assert real.read_text(encoding="utf-8") == "{}"
    assert br.retarget({"name": "n", "log": ident["log"]})[0] == 1


def test_the_cli_reads_the_identity_from_the_config(tmp_path, capsys):
    br = _load("bank_retarget")
    ident = _ident(tmp_path)
    _checkpoint(ident, "codex:old-thread")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"identities": [ident, {"name": "other", "log": "x", "target": "y"}]}), encoding="utf-8")
    assert br.main(["--config", str(cfg), "--identity", "codex-seat"]) == 0
    assert "rewrote" in capsys.readouterr().out
    assert br.main(["--config", str(cfg), "--identity", "absent"]) == 2
    assert br.main(["--config", str(tmp_path / "missing.json"), "--identity", "codex-seat"]) == 2


def test_the_cli_refuses_while_a_postbox_holds_its_lock_and_writes_nothing(tmp_path, capsys):
    """pxx deddc2 P2-1: a running postbox's Bank writes its own (old) consumer back on its next commit, so a retarget
    under it is undone. The command takes postbox.lock and refuses while a daemon holds it; the checkpoint is
    untouched. CONTROL: the same command, lock free, rewrites it."""
    br = _load("bank_retarget")
    import postbox_os
    ident = _ident(tmp_path)
    path = _checkpoint(ident, "codex:old-thread")
    before = Path(path).read_bytes()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"identities": [ident]}), encoding="utf-8")
    daemon_fd = os.open(str(tmp_path / "postbox.lock"), os.O_RDWR | os.O_CREAT)
    assert postbox_os.try_lock(daemon_fd)
    try:
        br.LOCK_TRIES, br.LOCK_GAP_S = 2, 0.0
        assert br.main(["--config", str(cfg), "--identity", "codex-seat"]) == 1
        assert "a postbox is running" in capsys.readouterr().out
        assert Path(path).read_bytes() == before
    finally:
        postbox_os.unlock(daemon_fd)
        os.close(daemon_fd)
    assert br.main(["--config", str(cfg), "--identity", "codex-seat"]) == 0
    assert json.loads(Path(path).read_text(encoding="utf-8"))["consumer"] == "codex:" + ident["target"]


# ------------------------------------------------------------------------------------------ 2. the fault file

def _crashing(n_crashes):
    calls = []

    def run_codex_delivery(_ident):
        calls.append(1)
        if len(calls) <= n_crashes:
            raise ValueError("receipt must be owned by this account with mode 600")
    return run_codex_delivery, calls


def test_three_crashes_in_a_row_write_a_fault_file_and_one_log_line(pb, tmp_path):
    """RED before the fix: 1,610 crashes left no file at all."""
    ident = _ident(tmp_path)
    fn, calls = _crashing(4)
    pb.guarded(fn, ident, pause_s=0, sleep=lambda _s: None)
    assert len(calls) == 5
    path = Path(ident["log"] + ".run_codex_delivery.fault.json")
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["schema"] == "postbox.fault.v1" and rec["identity"] == "codex-seat"
    assert rec["loop"] == "run_codex_delivery" and rec["crashes_in_a_row"] == 4
    assert rec["error"] == "ValueError: receipt must be owned by this account with mode 600"
    if POSIX:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    log = Path(pb.ERR).read_text(encoding="utf-8")
    assert log.count("FAULT: run_codex_delivery crashed 3 times in a row") == 1


def test_two_crashes_write_no_fault_file(pb, tmp_path):
    """CONTROL: below the threshold there is no file, so the file above comes from the count."""
    ident = _ident(tmp_path)
    fn, _ = _crashing(2)
    pb.guarded(fn, ident, pause_s=0, sleep=lambda _s: None)
    assert not Path(ident["log"] + ".run_codex_delivery.fault.json").exists()
    assert "FAULT" not in Path(pb.ERR).read_text(encoding="utf-8")


def test_a_recovery_in_between_restarts_the_count_and_removes_the_old_file(pb, tmp_path):
    ident = _ident(tmp_path)
    t = [1000.0]
    tracker = pb._FaultTracker(ident, "run_codex_delivery", clock=lambda: t[0])
    path = Path(ident["log"] + ".run_codex_delivery.fault.json")
    for _ in range(3):
        tracker.crashed("Traceback\nRuntimeError: boom")
        t[0] += 61
    assert path.exists()
    t[0] += pb.FAULT_WINDOW_S + 1                       # it ran for over ten minutes, then crashed once
    tracker.crashed("Traceback\nRuntimeError: boom")
    assert tracker.count == 1 and not path.exists()


def test_a_run_that_outlives_the_window_removes_the_fault_file_without_another_crash(pb, tmp_path):
    """pxx deddc2 P2-2 (and GitHub Codex's thread): three crashes, then a healthy run longer than the window. The file
    goes while the loop is still running, with no further crash. RED at 356b0ff7: the file stayed until a crash."""
    import threading
    import time as _time
    pb.FAULT_WINDOW_S = 0.3
    ident = _ident(tmp_path)
    path = Path(ident["log"] + ".run_codex_delivery.fault.json")
    seen = {}
    calls = []

    def run_codex_delivery(_ident):
        calls.append(1)
        if len(calls) <= 3:
            raise RuntimeError("boom")
        seen["during"] = path.exists()            # the 4th run starts healthy: the file is still there
        deadline = _time.time() + 3.0
        while path.exists() and _time.time() < deadline:
            _time.sleep(0.05)
        seen["after"] = path.exists()

    t = threading.Thread(target=pb.guarded, args=(run_codex_delivery, ident), kwargs={"pause_s": 0,
                                                                                    "sleep": lambda _s: None})
    t.start()
    t.join(10)
    assert not t.is_alive()
    assert seen == {"during": True, "after": False}, seen


def test_the_recovery_timer_is_cancelled_by_a_crash(pb, tmp_path):
    """A run that crashes before the window ends cancels its timer: the count goes on and the file stays."""
    ident = _ident(tmp_path)
    armed = []

    class _T:
        def __init__(self, delay, fn):
            self.fn, self.cancelled = fn, False
            armed.append(self)

        def cancel(self):
            self.cancelled = True

    tracker = pb._FaultTracker(ident, "run_codex_delivery", timer=_T)
    for _ in range(3):
        tracker.running()
        tracker.crashed("Traceback\nRuntimeError: boom")
    tracker.running()
    assert len(armed) == 3 and all(a.cancelled for a in armed[:2]) and not armed[2].cancelled
    tracker.crashed("Traceback\nRuntimeError: boom")
    assert armed[2].cancelled and tracker.count == 4
    assert Path(ident["log"] + ".run_codex_delivery.fault.json").exists()


def test_a_fault_file_from_an_earlier_run_is_removed_at_start(pb, tmp_path):
    ident = _ident(tmp_path)
    path = Path(ident["log"] + ".run_codex_delivery.fault.json")
    path.write_text("{}", encoding="utf-8")
    pb._FaultTracker(ident, "run_codex_delivery")
    assert not path.exists()


def test_an_identity_without_a_log_gets_no_file_and_no_crash(pb):
    calls = []

    def fn(_ident):
        calls.append(1)
        if len(calls) <= 3:
            raise RuntimeError("boom")
    pb.guarded(fn, {"name": "t"}, pause_s=0, sleep=lambda _s: None)
    assert len(calls) == 4
