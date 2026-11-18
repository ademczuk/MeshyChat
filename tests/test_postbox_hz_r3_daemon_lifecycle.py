"""HZ Round 3 owner tests for postbox.py fetch_gap, durable uncommitted journal, cursor recovery, and child/helper HOLD."""
from __future__ import annotations

import datetime as dt, importlib.util, io, json, os, subprocess, sys, time, tracemalloc, types
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
if str(PB) not in sys.path:
    sys.path.insert(0, str(PB))
import postbox_os  # noqa: E402


def _load_pb(mod_name: str, tmp_path: Path, monkeypatch):
    spec = importlib.util.spec_from_file_location(mod_name, PB / "postbox.py")
    pb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pb)
    monkeypatch.setattr(pb, "STATUS", str(tmp_path / "status.json"))
    monkeypatch.setattr(pb, "ERR", str(tmp_path / "postbox.err.log"))
    monkeypatch.setattr(pb, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(pb, "graphline", None)
    monkeypatch.setattr(pb, "owedline", None)
    return pb


def _ident(tmp_path: Path, name: str = "seat-r3lc") -> dict:
    client = tmp_path / "client"
    (client / "tools" / "meshy_node").mkdir(parents=True, exist_ok=True)
    state, log = tmp_path / f"{name}.state.json", tmp_path / f"{name}.log"
    state.write_text(json.dumps({"peer_id": "peer-r3lc", "label": name}), encoding="utf-8")
    return {"name": name, "address": name, "python": sys.executable, "client": str(client),
            "state": str(state), "log": str(log), "base_url": "http://127.0.0.1:9"}


def _traced(fn):
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        res = fn()
        return res, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_fetch_gap_bounded_multipass_and_streaming_sidecar_quarantine(tmp_path, monkeypatch):
    pb = _load_pb("pb_r3lc_fg", tmp_path, monkeypatch)
    T0 = dt.datetime(2026, 11, 1, 8, 0, 0, tzinfo=dt.timezone.utc)
    ident_fg = _ident(tmp_path, "seat-fg")
    assert pb.fetch_gap(ident_fg, clock=lambda: T0) == (0, None)
    with open(ident_fg["log"], "wb") as lf:
        for idx in range(12):
            ts = (T0 + dt.timedelta(seconds=idx + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            lf.write((json.dumps({"event": "message", "key": f"msg/fg-{idx:02d}", "version": 1,
                                  "role": "to", "created_at": ts}) + "\n").encode("utf-8"))
    monkeypatch.setattr(pb, "FETCH_MAX_WAITING", 5)
    assert pb.fetch_gap(ident_fg, clock=lambda: T0 + dt.timedelta(minutes=5)) == (5, T0 + dt.timedelta(seconds=1))
    st_fg1 = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident_fg["name"]]
    assert st_fg1["fetch_gap_incomplete"] is True and st_fg1["fetch_gap"] == "ok"
    sidecar = Path(ident_fg["state"] + pb.FETCH_SIDECAR_SUFFIX)
    with open(sidecar, "ab") as sf:
        for idx in range(7):
            sf.write((json.dumps({"key": f"msg/fg-{idx:02d}", "version": 1,
                                  "at": (T0 + dt.timedelta(minutes=2)).isoformat()}) + "\n").encode("utf-8"))
    assert pb.fetch_gap(ident_fg, clock=lambda: T0 + dt.timedelta(minutes=6)) == (5, T0 + dt.timedelta(seconds=8))
    assert json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident_fg["name"]]["fetch_gap_incomplete"] is False

    ident_c = _ident(tmp_path, "seat-fgc")
    assert pb.fetch_gap(ident_c, clock=lambda: T0) == (0, None)
    with open(ident_c["log"], "wb") as lf:
        for k, s in (("msg/fg-cleared", 1), ("msg/fg-still-unread", 2)):
            lf.write((json.dumps({"event": "message", "key": k, "version": 1, "role": "to",
                                  "created_at": (T0 + dt.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n").encode("utf-8"))
    sidecar_corrupt = Path(ident_c["state"] + pb.FETCH_SIDECAR_SUFFIX)
    with open(sidecar_corrupt, "ab") as cf:
        for idx in range(20):
            cf.write(f"{{bad-json-{idx}\n".encode("utf-8"))
        cf.write((json.dumps({"key": "msg/fg-cleared", "version": 1,
                              "at": (T0 + dt.timedelta(days=9)).isoformat()}) + "\n").encode("utf-8"))
    monkeypatch.setattr(pb, "FETCH_SIDECAR_MAX", 1)
    assert pb.fetch_gap(ident_c, clock=lambda: T0 + dt.timedelta(days=10)) == (1, T0 + dt.timedelta(seconds=2))
    assert len(Path(str(sidecar_corrupt) + ".quarantine.jsonl").read_text(encoding="utf-8").splitlines()) == 20

    ident_sc = _ident(tmp_path, "seat-sc-hold")
    assert pb.fetch_gap(ident_sc, clock=lambda: T0) == (0, None)
    sidecar_hold = Path(ident_sc["state"] + pb.FETCH_SIDECAR_SUFFIX)
    big_bad = b"{corrupt-fetched-line-" + (b"Y" * 300000) + b"\n"
    with open(sidecar_hold, "ab") as sf:
        sf.write(big_bad)
    sc_bytes = sidecar_hold.read_bytes()
    monkeypatch.setattr(pb, "QUARANTINE_TOTAL_MAX_BYTES", 10)
    assert pb.fetch_gap(ident_sc, clock=lambda: T0 + dt.timedelta(days=10)) == (0, None)
    assert sidecar_hold.read_bytes() == sc_bytes
    st_hold = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident_sc["name"]]
    assert st_hold["fetch_gap"] == "hold" and st_hold["fetch_gap_hold"]
    sc_hold_file = Path(str(sidecar_hold) + ".hold.json")
    assert sc_hold_file.exists()
    monkeypatch.setattr(pb, "QUARANTINE_TOTAL_MAX_BYTES", 64 * 1048576)
    assert pb.fetch_gap(ident_sc, clock=lambda: T0 + dt.timedelta(days=10)) == (0, None)
    assert sidecar_hold.read_bytes() == sc_bytes
    sc_hold_file.unlink()
    assert pb.fetch_gap(ident_sc, clock=lambda: T0 + dt.timedelta(days=10)) == (0, None)
    sc_qrec = json.loads(Path(str(sidecar_hold) + ".quarantine.jsonl").read_text(encoding="utf-8").strip())
    assert (Path(str(sidecar_hold) + ".quarantine.jsonl.raw") / sc_qrec["raw_blob"]).read_bytes() == big_bad

    monkeypatch.setattr(pb, "FETCH_MAX_WAITING", 16384)
    ident_big = _ident(tmp_path, "seat-fg-heap")
    assert pb.fetch_gap(ident_big, clock=lambda: T0) == (0, None)
    with open(ident_big["log"], "wb") as lf:
        lf.writelines(
            f'{{"event":"message","key":"msg/h-{i:05d}","version":1,"role":"to","created_at":"2026-11-01T{9 + i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z"}}\n'.encode("utf-8")
            for i in range(32768, 0, -1)
        )
    t_heap0 = time.monotonic()
    cnt_big, oldest_big = pb.fetch_gap(ident_big, clock=lambda: T0 + dt.timedelta(days=2))
    assert cnt_big == 16384 and oldest_big == dt.datetime(2026, 11, 1, 9, 0, 1, tzinfo=dt.timezone.utc)
    assert time.monotonic() - t_heap0 < 5.0


def test_p1_b3_uncommitted_journal_and_monitor_cursor_recovery_when_inbox_empty(tmp_path, monkeypatch):
    pb = _load_pb("pb_r3lc_p1b3", tmp_path, monkeypatch)
    ident = _ident(tmp_path)
    monkeypatch.setattr(pb, "_run_bounded_cmd", lambda argv, **_kw: subprocess.CompletedProcess(
        argv, 0, json.dumps({"label": ident["name"], "hits": []}), ""))

    ident_wal, real_rec = _ident(tmp_path, "seat-wal"), pb.record_listener_line
    wal_frame = (json.dumps({"event": "message", "type": "mesh_message", "key": "msg/wal-1",
                             "version": 1, "role": "to", "from": "bob", "created_at": "2026-11-01T08:05:00Z"}) + "\n").encode("utf-8")
    wal_fail = {"once": True}
    monkeypatch.setattr(pb, "listeners_on", lambda _st: [])
    monkeypatch.setattr(pb, "tie_to_daemon", lambda _p: True)
    monkeypatch.setattr(postbox_os, "process_create_time", lambda _pid: None)
    monkeypatch.setattr(pb.subprocess, "Popen", lambda *_a, **_k: types.SimpleNamespace(
        pid=555001, stdout=io.BytesIO(wal_frame), poll=lambda: 0, wait=lambda **_kw: 0, terminate=lambda: None, kill=lambda: None))
    monkeypatch.setattr(pb, "record_listener_line",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ENOSPC")) if wal_fail.pop("once", False) else real_rec(*a, **k))
    with pytest.raises(OSError, match="ENOSPC"):
        pb.run_identity(ident_wal, ident_wal["base_url"])
    wal_files, _ = pb._uncommitted_entries(ident_wal["log"])
    assert len(wal_files) == 1
    assert [r.get("key") for r in map(json.loads, Path(ident_wal["log"]).read_text(encoding="utf-8").splitlines()) if r.get("event") == "message"] == []
    assert pb._flush_uncommitted(ident_wal) == 1 and pb._uncommitted_entries(ident_wal["log"])[0] == []
    assert [r.get("key") for r in map(json.loads, Path(ident_wal["log"]).read_text(encoding="utf-8").splitlines()) if r.get("event") == "message"] == ["msg/wal-1"]
    monkeypatch.setattr(pb, "record_listener_line", real_rec)

    frame1 = (json.dumps({"event": "message", "type": "mesh_message", "key": "msg/crash-1",
                          "version": 1, "role": "to", "from": "bob", "created_at": "2026-11-01T08:10:00Z"}) + "\n").encode("utf-8")
    jpath = pb._queue_uncommitted(ident, frame1, None)
    assert jpath and os.path.isfile(jpath)
    assert pb.reconcile(ident, ident["base_url"]) == 0
    assert pb._flush_uncommitted(ident) == 1 and not os.path.exists(jpath)
    spool_rows = [json.loads(x) for x in Path(ident["log"]).read_text(encoding="utf-8").splitlines()]
    assert [r.get("key") for r in spool_rows] == ["msg/crash-1"]

    monkeypatch.setattr(pb, "UNCOMMITTED_MAX", 2)
    j1, j2 = pb._queue_uncommitted(ident, frame1, None), pb._queue_uncommitted(ident, frame1, None)
    with pytest.raises(pb._UncommittedHoldError):
        pb._queue_uncommitted(ident, frame1, None)
    assert os.path.isfile(j1) and os.path.isfile(j2)
    hfile = pb._hold_file_path(ident["log"])
    pb._latch_durable_hold(ident, "uncommitted journal full")
    assert pb._check_listener_hold(ident) is True and os.path.isfile(hfile)
    os.unlink(hfile); assert pb._check_listener_hold(ident) is False
    real_write, fail_once = postbox_os.write_private_json_atomic, {"n": 1}
    monkeypatch.setattr(postbox_os, "write_private_json_atomic",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ENOSPC")) if fail_once.pop("n", 0) else real_write(*a, **k))
    pb._latch_durable_hold(ident, "enospc latch")
    assert not os.path.exists(hfile) and pb._check_listener_hold(ident) is True and os.path.isfile(hfile)
    os.unlink(hfile); assert pb._check_listener_hold(ident) is False

    ident_cur = _ident(tmp_path, "seat-moncur")
    mon_path = Path(ident_cur["state"] + ".monitor")
    committed_doc = json.dumps({"cursor": 100, "recent_keys": ["msg/old"]}).encode("utf-8")
    mon_path.write_bytes(committed_doc)
    pb._anchor_monitor_cursor_before_spawn(ident_cur)
    mon_path.write_bytes(json.dumps({"cursor": 105, "recent_keys": ["msg/old", "msg/uncommitted-ws"]}).encode("utf-8"))
    pb._anchor_monitor_cursor_before_spawn(ident_cur)
    assert mon_path.read_bytes() == committed_doc
    os.unlink(ident_cur["log"] + ".monitor.committed")
    with pytest.raises(ValueError, match="committed monitor cursor missing"):
        pb._anchor_monitor_cursor_before_spawn(ident_cur)
    Path(ident_cur["log"] + ".monitor.committed").write_bytes(committed_doc)
    mon_path.write_bytes(json.dumps({"cursor": 105, "recent_keys": ["msg/old", "msg/committed-ws"]}).encode("utf-8"))
    pb._commit_monitor_cursor_after_exit(ident_cur)
    assert not os.path.exists(ident_cur["log"] + ".listener.active")
    pb._anchor_monitor_cursor_before_spawn(ident_cur)
    assert json.loads(mon_path.read_text(encoding="utf-8"))["cursor"] == 105

    ident_init = _ident(tmp_path, "seat-initarm")
    init_mon = Path(ident_init["state"] + ".monitor")
    pb._anchor_monitor_cursor_before_spawn(ident_init)
    init_doc = json.dumps({"schema": "meshy-monitor-cursor/1", "cursor": "seq:10:1:u", "recent_keys": []}).encode("utf-8")
    init_mon.write_bytes(init_doc)
    pb._anchor_monitor_cursor_before_spawn(ident_init)
    assert init_mon.read_bytes() == init_doc and Path(ident_init["log"] + ".monitor.committed").read_bytes() == init_doc
    os.unlink(ident_init["log"] + ".monitor.committed")
    postbox_os.write_private_json_atomic(ident_init["log"] + ".listener.active", {"pid": 1, "at": "t", "had_monitor": False})
    init_mon.write_bytes(json.dumps({"schema": "meshy-monitor-cursor/1", "cursor": "seq:12:1:u", "recent_keys": ["msg/x"]}).encode("utf-8"))
    with pytest.raises(ValueError, match="initial monitor cursor advanced without committed backup"):
        pb._anchor_monitor_cursor_before_spawn(ident_init)
    assert init_mon.exists()

    ident_seed = _ident(tmp_path, "seat-seed")
    (Path(ident_seed["client"]) / "tools" / "meshy_node" / "monitor.py").write_text("# stub\n", encoding="utf-8")
    assert pb._anchor_monitor_cursor_before_spawn(ident_seed, ident_seed["base_url"]) is False
    seed_doc = json.dumps({"schema": "meshy-monitor-cursor/1", "cursor": "seq:1:1:u", "recent_keys": []}).encode("utf-8")
    monkeypatch.setattr(pb, "_run_bounded_cmd", lambda argv, **_kw: (
        Path(ident_seed["state"] + ".monitor").write_bytes(seed_doc),
        subprocess.CompletedProcess(argv, 0, "", ""))[1])
    assert pb._anchor_monitor_cursor_before_spawn(ident_seed, ident_seed["base_url"]) is True
    assert Path(ident_seed["log"] + ".monitor.committed").read_bytes() == seed_doc


def test_p2_a1_a2_dispose_listener_and_send_drain_helper_settlement(tmp_path, monkeypatch):
    pb = _load_pb("pb_r3lc_p2a", tmp_path, monkeypatch)
    ident = _ident(tmp_path)
    ops_dir = Path(ident["state"] + ".ops")
    ops_dir.mkdir()
    with open(ops_dir / "huge.json", "wb") as f:
        f.seek(2 * 1048576 - 1); f.write(b"\n")
    pending, peak = _traced(lambda: pb._pending_send_ops(ident))
    assert pending is True and peak < 512 * 1024

    exited_proc = types.SimpleNamespace(pid=444001, stdout=None, poll=lambda: 0, wait=lambda **_k: 0,
                                        terminate=lambda: None, kill=lambda: None)
    monkeypatch.setattr(pb, "listeners_on", lambda _st: [444002])
    monkeypatch.setattr(postbox_os, "process_create_time", lambda _pid: 100.0)
    monkeypatch.setattr(postbox_os, "terminate_owned_tree", lambda *_a, **_k: False)
    assert pb._dispose_listener_child(ident, exited_proc, 100.0, job_tied=False) is False
    st = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident["name"]]
    assert st["listener"] == "hold" and "listeners still present: [444002]" in st["listener_hold"]

    ident2 = _ident(tmp_path, "seat-noctime")
    monkeypatch.setattr(pb, "listeners_on", lambda _st: [])
    assert pb._dispose_listener_child(ident2, exited_proc, None, job_tied=False) is False
    st2 = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident2["name"]]
    assert st2["listener"] == "hold" and "descendant cleanup uncertain" in st2["listener_hold"]

    ident_dr = _ident(tmp_path, "seat-drainhold")
    hdir = Path(postbox_os.helper_dir_for_log(ident_dr["log"]))
    sleeper = subprocess.Popen([sys.executable, "-S", "-c", "import time; time.sleep(60)"])
    try:
        ct = postbox_os.process_create_time(sleeper.pid)
        postbox_os.write_private_json_atomic(str(hdir / "h-mut-drain.json"), {
            "schema": 1, "kind": "send_drain", "mutating": True, "pid": sleeper.pid, "pgid": None,
            "create_time": ct, "create_time_known": True, "daemon_pid": 99999991,
            "daemon_create_time": 1.0, "started_at": time.time() - 5, "timeout_s": 90.0})
        drain_spawns = []
        monkeypatch.setattr(pb, "_run_bounded_cmd", lambda *a, **k: drain_spawns.append((a, k)))
        pb.drain_send_operations(ident_dr)
        assert sleeper.poll() is None and drain_spawns == []
        st_dr = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident_dr["name"]]
        assert st_dr["send_drain"] == "hold" and "mutating_helper_unsettled" in st_dr["send_drain_hold"]
    finally:
        if sleeper.poll() is None:
            sleeper.kill(); sleeper.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
