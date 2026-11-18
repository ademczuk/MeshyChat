"""HZ Round 4 PR B tests: byte budgets, Bloom/saturation status, 32-slot quarantine + fsync_dir, Gap 5, and P1-B.3."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import pytest

from tools.postbox import postbox_os

ROOT = Path(__file__).resolve().parents[1]
PB_PATH = ROOT / "tools" / "postbox" / "postbox.py"
T0 = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)


def _load_pb(mod_name: str, tmp_path: Path, monkeypatch):
    shutil.copy2(ROOT / "tools" / "postbox" / "postbox_child.py", tmp_path / "postbox_child.py")
    spec = importlib.util.spec_from_file_location(mod_name, PB_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    for attr, val in (("HERE", tmp_path), ("ERR", tmp_path / "postbox.err.log"),
                      ("STATUS", tmp_path / "status.json"), ("CONFIG", tmp_path / "config.json")):
        monkeypatch.setattr(mod, attr, str(val))
    return mod


def _ident(tmp_path: Path, name: str = "seat") -> dict:
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    state = tmp_path / f"{name}-state.json"
    state.write_text(json.dumps({"peer_id": f"p-{name}", "address": name}), encoding="utf-8")
    return {"name": name, "cli": "claude", "python": sys.executable, "client": str(ROOT), "base_url": "http://127.0.0.1:9",
            "state": str(state), "log": str(tmp_path / "spool" / f"{name}.log"), "track_fetches": True}


def test_p1_a1_byte_budgets_bloom_saturation_and_incomplete_sidecar_trim(tmp_path, monkeypatch):
    pb = _load_pb("pb_r4b_p1a1", tmp_path, monkeypatch)
    ident = _ident(tmp_path, "seat-b")
    monkeypatch.setattr(pb, "SEEN_KEYS_MAX", 1000)
    monkeypatch.setattr(pb, "SEEN_KEYS_MAX_BYTES", 900)
    with open(ident["log"], "wb") as lf:
        for i in range(12):
            lf.write((json.dumps({"event": "message", "key": f"msg/{'k' * 60}-{i}", "version": 1}) + "\n").encode())
    ks = pb.seen_keys(ident)
    assert ks.saturated and ks.evictions > 0 and ks.used_bytes <= 900
    st = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident["name"]]
    assert st["seen_keys_saturated"] is True and st["seen_keys_evictions"] == ks.evictions
    scans = []
    orig_scan = pb._log_has_key_streaming
    monkeypatch.setattr(pb, "_log_has_key_streaming", lambda lp, pair: (scans.append(pair), orig_scan(lp, pair))[1])
    assert pb.key_seen(ident, "msg/never-seen-anywhere", 1) is False
    assert scans == []
    evicted_key = f"msg/{'k' * 60}-0"
    assert pb.key_seen(ident, evicted_key, 1) is True and (evicted_key, 1) in scans

    ident_fg = _ident(tmp_path, "seat-fg")
    assert pb.fetch_gap(ident_fg, clock=lambda: T0) == (0, None)
    sidecar = Path(ident_fg["state"] + pb.FETCH_SIDECAR_SUFFIX)
    old_ts = (T0 - dt.timedelta(days=20)).isoformat()
    with open(sidecar, "ab") as sf:
        for i in range(25):
            sf.write((json.dumps({"key": f"msg/old-{i}", "version": 1, "at": old_ts}) + "\n").encode())
    with open(ident_fg["log"], "wb") as lf:
        for i in range(10):
            lf.write((json.dumps({"event": "message", "key": f"msg/u-{i}", "version": 1, "role": "to",
                                  "created_at": (T0 + dt.timedelta(seconds=i + 1)).isoformat()}) + "\n").encode())
    monkeypatch.setattr(pb, "FETCH_MAX_WAITING_BYTES", 320)
    monkeypatch.setattr(pb, "FETCH_SIDECAR_MAX", 200)
    cnt, _ = pb.fetch_gap(ident_fg, clock=lambda: T0 + dt.timedelta(days=1))
    fg_st = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident_fg["name"]]
    assert 1 <= cnt < 10 and fg_st["fetch_gap_incomplete"] is True
    assert len(sidecar.read_text(encoding="utf-8").splitlines()) == 1


def test_p1_b1_quarantine_32_slots_no_overwrite_dir_fsync_and_concurrent_lock(tmp_path, monkeypatch):
    pb = _load_pb("pb_r4b_p1b1", tmp_path, monkeypatch)
    fsynced = []
    orig_fsync = pb.postbox_os.fsync_dir
    monkeypatch.setattr(pb.postbox_os, "fsync_dir", lambda d: (fsynced.append(os.path.abspath(d)), orig_fsync(d)))
    qpath = tmp_path / "spool" / "seat.log.quarantine.jsonl"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    for i in range(33):
        pb._append_quarantine_record(str(qpath), {"seq": i, "pad": "x" * 80}, max_bytes=10)
    for idx in range(1, 33):
        slot = Path(f"{qpath}.{idx}")
        assert slot.exists() and json.loads(slot.read_text(encoding="utf-8").strip())["seq"] == idx - 1
    with pytest.raises(pb._QuarantineHoldError, match="slots exhausted"):
        pb._append_quarantine_record(str(qpath), {"seq": 33, "pad": "x" * 80}, max_bytes=10)
    assert os.path.abspath(str(qpath.parent)) in fsynced

    q2 = tmp_path / "spool" / "conc.quarantine.jsonl"
    threads = [threading.Thread(target=lambda idx=i: pb._quarantine_stream_chunks(str(q2), [b"Z" * 128], {"t": idx}))
               for i in range(16)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(q2.read_text(encoding="utf-8").splitlines()) == 16


def test_p2_a1_durable_desc_hold_drain_lock_codex_clear_and_kimi_wire(tmp_path, monkeypatch):
    pb = _load_pb("pb_r4b_p2", tmp_path, monkeypatch)
    ident = _ident(tmp_path, "seat-h")
    monkeypatch.setattr(pb, "listeners_on", lambda _st: [])

    class _ExitedProc:
        pid, stdout = 999991, None
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass

    assert pb._dispose_listener_child(ident, _ExitedProc(), None, job_tied=False) is False
    hfile = Path(ident["log"] + ".hold.json")
    assert hfile.exists()
    pb._listener_holds.clear(); pb._durable_holds.clear()
    assert pb._check_listener_hold(ident) is True
    hfile.unlink()
    assert pb._check_listener_hold(ident) is False
    # Direct _drain_lock / drain_send_operations serialization check:
    monkeypatch.setattr(pb, "_pending_send_ops", lambda _id: True)
    in_drain, max_in_drain, entered = 0, 0, threading.Event()
    d_mu = threading.Lock()

    def _fake_bounded(*_a, **_kw):
        nonlocal in_drain, max_in_drain
        with d_mu:
            in_drain += 1
            max_in_drain = max(max_in_drain, in_drain)
            entered.set()
        time.sleep(0.08)
        with d_mu:
            in_drain -= 1
        return subprocess.CompletedProcess(["python"], 0, b'{"refused":0,"uncertain":0}\n', b"")

    monkeypatch.setattr(pb, "_run_bounded_cmd", _fake_bounded)
    dt1 = threading.Thread(target=pb.drain_send_operations, args=(ident,))
    dt2 = threading.Thread(target=pb.drain_send_operations, args=(ident,))
    dt1.start(); assert entered.wait(2.0); dt2.start()
    dt1.join(5.0); dt2.join(5.0)
    assert max_in_drain == 1

    # Direct REBLANK_MAX_BYTES batching check:
    ident_rb = _ident(tmp_path, "seat-rb")
    with open(ident_rb["log"], "wb") as rbf:
        for i in range(6):
            k = f"msg/{'r' * 60}-{i}"
            rbf.write((json.dumps({"event": "message", "key": k, "version": 1, "preview_unverified": "secret"}) + "\n").encode())
            rbf.write((json.dumps({"event": "redaction", "key": k, "version": 1}) + "\n").encode())
    monkeypatch.setattr(pb, "REBLANK_MAX_BYTES", 240)
    monkeypatch.setattr(pb, "REBLANK_MAX_PASSES", 1)
    assert pb.reblank_log(ident_rb, incremental=True) >= 1
    rb_st = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident_rb["name"]]
    assert rb_st["reblank_incomplete"] is True
    monkeypatch.setattr(pb, "REBLANK_MAX_PASSES", 16)
    pb.reblank_log(ident_rb, incremental=True)
    rb_st2 = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident_rb["name"]]
    assert rb_st2["reblank_incomplete"] is False and b"secret" not in Path(ident_rb["log"]).read_bytes()

    # Direct codex_helper_hold set-then-clear check via run_codex_delivery:
    ident_cx = _ident(tmp_path, "seat-cx")
    ident_cx["cli"], ident_cx["target"], ident_cx["codex_settle_s"], ident_cx["codex_gap_s"] = "codex", "thr-1", 0, 0
    Path(pb.CONFIG).write_text(json.dumps({"identities": [ident_cx]}), encoding="utf-8")
    Path(ident_cx["log"] + ".codex.offset").write_text("0\n", encoding="utf-8")
    Path(ident_cx["log"]).write_bytes(b'{"event":"message","key":"msg/cx-1","version":1,"role":"to"}\n')
    calls_cx = {"n": 0}

    def _fake_settle_cx(_hdir, only_kind=None):
        calls_cx["n"] += 1
        if calls_cx["n"] == 1:
            return {"settled": False, "issues": ["codex_queue pid 42 still running"]}
        pb._request_stop()
        return {"settled": True, "issues": []}

    sleeps_cx = {"n": 0}

    def _fake_sleep_cx(_sec):
        sleeps_cx["n"] += 1
        return sleeps_cx["n"] > 3

    monkeypatch.setattr(pb.postbox_os, "settle_helper_dir", _fake_settle_cx)
    monkeypatch.setattr(pb, "_sleep_stop", _fake_sleep_cx)
    monkeypatch.setattr(pb, "_run_bounded_cmd", lambda *_a, **_kw: subprocess.CompletedProcess(["codex"], 0, "ok\n", ""))
    pb._reset_stop()
    pb.run_codex_delivery(ident_cx)
    cx_st = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident_cx["name"]]
    assert calls_cx["n"] >= 2 and cx_st["codex"] == "ok" and cx_st["codex_helper_hold"] is None
    pb._reset_stop()

    wdir = tmp_path / "kimi" / "sessions" / "w" / "session_00000000-0000-0000-0000-000000000001" / "agents" / "main"
    wdir.mkdir(parents=True)
    wfile = wdir / "wire.jsonl"
    wfile.write_bytes(b'{"type":"init"}\n')
    kw = pb.KimiWire({"kimi_home": str(tmp_path / "kimi"), "kimi_session": "session_00000000-0000-0000-0000-000000000001"})
    mark = kw.mark()
    monkeypatch.setattr(pb, "CODEX_ROLLOUT_MAX_LINE", 64)
    with open(wfile, "ab") as wf: wf.write(b'{"type":"turn.prompt","pad":"' + (b"P" * 120) + b'"}\n')
    ok, why = kw.completed(mark)
    assert ok is False and "oversize line" in why


def test_gap5_and_p1_b3_subprocess_crash_replay_steady_state_cursor_and_stop(tmp_path, monkeypatch):
    pb = _load_pb("pb_r4b_e2e", tmp_path, monkeypatch)
    ident = _ident(tmp_path, "seat-crash")
    crash_code = "\n".join([
        "import importlib.util, json, os, sys",
        f"spec = importlib.util.spec_from_file_location('pb_c', {str(PB_PATH)!r})",
        "pb = importlib.util.module_from_spec(spec); sys.modules['pb_c'] = pb; spec.loader.exec_module(pb)",
        f"pb.HERE, pb.ERR, pb.STATUS = {str(tmp_path)!r}, {str(tmp_path / 'postbox.err.log')!r}, {str(tmp_path / 'status.json')!r}",
        f"ident = json.loads({json.dumps(ident)!r})",
        "frames = [b'{\"event\":\"message\",\"key\":\"msg/c-1\",\"version\":1,\"role\":\"to\",\"preview\":\"secret\"}\\n',",
        "          b'{\"event\":\"edit\",\"key\":\"msg/c-1\",\"version\":1}\\n',",
        "          b'{\"event\":\"redaction\",\"key\":\"msg/c-1\",\"redacted_at\":\"2026-11-02T12:00:00Z\"}\\n',",
        "          b'not-json-frame\\n', b'{\"event\":\"status\",\"state\":\"error\",\"rc\":1}\\n']",
        "with pb._open_regular_append(ident['log']) as out:",
        "  for raw in frames: q = pb.now(); pb._queue_uncommitted(ident, raw, None, queued_at=q); pb.record_listener_line(ident, out, raw, queued_at=q)",
        "os._exit(99)",
    ])
    assert subprocess.run([sys.executable, "-c", crash_code], timeout=20).returncode == 99
    before_lines = Path(ident["log"]).read_bytes().splitlines()
    assert len(before_lines) == 5 and pb._has_uncommitted(ident) and pb._flush_uncommitted(ident) == 5
    assert Path(ident["log"]).read_bytes().splitlines() == before_lines and not pb._has_uncommitted(ident)
    ident_f = _ident(tmp_path, "seat-frames")
    with pb._open_regular_append(ident_f["log"]) as fout:
        for fr in (
            b'{"event":"frame","type":"mesh_reaction","key":"msg/f-1","unhandled":true}\n',
            b'{"event":"frame","type":"mesh_edit","key":"msg/f-1","unhandled":true}\n',
            b'{"event":"frame","type":"skill_invoke","key":"msg/f-1","call_id_unverified":"c-1","unhandled":true}\n',
            b'{"event":"frame","type":"skill_invoke","key":"msg/f-1","call_id_unverified":"c-2","unhandled":true}\n',
            b'{"event":"frame","type":"skill_invoke","key":"msg/f-1","call_id_unverified":"c-1","unhandled":true}\n',
        ):
            pb.record_listener_line(ident_f, fout, fr)
    assert len(Path(ident_f["log"]).read_bytes().splitlines()) == 4

    with pb._active_listeners_lock:
        pb._signal_stop(signal.SIGTERM, None)
        assert pb._stop_requested() is True
    pb._reset_stop()

    fake_client = tmp_path / "fake_client"
    (fake_client / "tools" / "meshy_node").mkdir(parents=True)
    for init_p in (fake_client / "tools" / "__init__.py", fake_client / "tools" / "meshy_node" / "__init__.py"):
        init_p.write_text("", encoding="utf-8")
    (fake_client / "tools" / "meshy_node" / "__main__.py").write_text("\n".join([
        "import json, pathlib, sys, time",
        "args = sys.argv[1:]; st = pathlib.Path(args[args.index('--state-file') + 1])",
        "if args[0] == 'inbox': print(json.dumps({'label': 'seat-live', 'hits': []})); sys.exit(0)",
        "sys.stdout.write(json.dumps({'event': 'message', 'key': 'msg/live-1', 'version': 1, 'role': 'to'}) + '\\n'); sys.stdout.flush()",
        "pathlib.Path(str(st) + '.monitor').write_text(json.dumps({'cursor': 'c-steady-1', 'recent_keys': ['msg/live-1']}), encoding='utf-8')",
        "while True: time.sleep(0.2)",
    ]), encoding="utf-8")
    ident_live = _ident(tmp_path, "seat-live")
    ident_live["client"], ident_live["track_fetches"] = str(fake_client), False
    Path(ident_live["state"] + ".monitor").write_text(json.dumps({"cursor": "c-init", "recent_keys": []}), encoding="utf-8")
    monkeypatch.setattr(pb, "MONITOR_COMMIT_LAG_S", 0.2)
    pb._reset_stop()
    t = threading.Thread(target=pb.guarded, args=(pb.run_identity, ident_live, ident_live["base_url"]), daemon=True)
    t.start()
    comm = Path(ident_live["log"] + ".monitor.committed")
    act = Path(ident_live["log"] + ".listener.active")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        pb._maybe_checkpoint_monitor_cursor()
        if comm.exists() and json.loads(comm.read_text(encoding="utf-8")).get("cursor") == "c-steady-1":
            break
        time.sleep(0.1)
    assert json.loads(comm.read_text(encoding="utf-8"))["cursor"] == "c-steady-1" and act.exists()
    Path(ident_live["state"] + ".monitor").write_text(
        json.dumps({"cursor": "c-unspooled", "recent_keys": ["msg/live-1", "msg/unspooled-2"]}), encoding="utf-8")
    pb._maybe_checkpoint_monitor_cursor()
    time.sleep(0.3)
    pb._maybe_checkpoint_monitor_cursor()
    assert json.loads(comm.read_text(encoding="utf-8"))["cursor"] == "c-steady-1"
    Path(ident_live["state"] + ".monitor").write_text(json.dumps({"cursor": "c-stop-2", "recent_keys": ["msg/live-1"]}), encoding="utf-8")
    pb._request_stop()
    t.join(timeout=10.0)
    assert not t.is_alive() and not act.exists()
    assert json.loads(comm.read_text(encoding="utf-8"))["cursor"] == "c-stop-2"


def test_f1_fetch_gap_transient_oserror_retries_while_quarantine_budget_latches_hold(tmp_path, monkeypatch):
    pb = _load_pb("pb_r4b_f1", tmp_path, monkeypatch)
    ident = _ident(tmp_path, "seat-f1")
    assert pb.fetch_gap(ident, clock=lambda: T0) == (0, None)
    sidecar = Path(ident["state"] + pb.FETCH_SIDECAR_SUFFIX)
    hold_file = Path(pb._fetch_hold_path(str(sidecar)))
    old_ts = (T0 - dt.timedelta(days=20)).isoformat()
    with open(sidecar, "ab") as sf:
        for i in range(10):
            sf.write((json.dumps({"key": f"msg/old-{i}", "version": 1, "at": old_ts}) + "\n").encode())
    monkeypatch.setattr(pb, "FETCH_SIDECAR_MAX", 100)
    orig_replace = pb.os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        if os.path.abspath(str(dst)) == os.path.abspath(str(sidecar)) and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("WinError 32: file held by indexer")
        return orig_replace(src, dst)

    monkeypatch.setattr(pb.os, "replace", _flaky_replace)
    pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(days=1))
    st1 = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident["name"]]
    assert calls["n"] == 1 and st1["fetch_gap"] == "hold" and not hold_file.exists()
    assert len(sidecar.read_text(encoding="utf-8").splitlines()) == 11
    # Next pass retries automatically and trims cleanly:
    pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(days=1))
    st2 = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident["name"]]
    assert st2["fetch_gap"] == "ok" and st2["fetch_gap_hold"] is None and not hold_file.exists()
    assert len(sidecar.read_text(encoding="utf-8").splitlines()) == 1

    # Control: _QuarantineHoldError DOES latch durable .hold.json across passes until removed:
    with open(sidecar, "ab") as sf:
        sf.write(b"not-valid-json-1\nnot-valid-json-2\n")
    monkeypatch.setattr(pb, "FETCH_MAX_DAMAGED_SPANS", 1)
    pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(days=2))
    assert hold_file.exists()
    monkeypatch.setattr(pb, "FETCH_MAX_DAMAGED_SPANS", 256)
    pb.fetch_gap(ident, clock=lambda: T0 + dt.timedelta(days=2))
    st3 = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident["name"]]
    assert st3["fetch_gap"] == "hold" and "damaged sidecar spans exceeded" in st3["fetch_gap_hold"]

    # Q1 check: _msg_key_spooled matches message at version > 1, but rejects edit-only without message:
    ident_q1 = _ident(tmp_path, "seat-q1")
    Path(ident_q1["log"]).write_bytes(
        b'{"event":"message","key":"msg/edited-catchup","version":2,"role":"to"}\n'
        b'{"event":"edit","key":"msg/edit-without-msg","version":2}\n'
    )
    assert pb._msg_key_spooled(ident_q1, "msg/edited-catchup") is True
    assert pb._msg_key_spooled(ident_q1, "msg/edit-without-msg") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
