import io
import json
import os
import subprocess
import sys
import time
import types

import pytest

from tools.postbox import delivery_bank, graphline, latency, meshy_channel, owedline, postbox_os


def test_append_regular_private_sixteen_slots_never_overwrite_and_latency_reads_all(tmp_path, monkeypatch):
    fsynced_dirs_os, fsynced_dirs_bank = [], []
    monkeypatch.setattr(postbox_os, "fsync_dir", lambda d: fsynced_dirs_os.append(d))
    monkeypatch.setattr(delivery_bank, "_fsync_dir", lambda d: fsynced_dirs_bank.append(d))

    # 1. postbox_os.append_regular_private: 16 archive slots + active file, broken-link skip, never overwrite.
    err_log = tmp_path / "postbox.err.log"
    if hasattr(os, "symlink"):
        try:
            os.symlink(str(tmp_path / "dangling-target"), str(tmp_path / "postbox.err.log.3"))
            has_broken_link = True
        except OSError:
            has_broken_link = False
    else:
        has_broken_link = False

    free_slots = [i for i in range(1, 17) if not (has_broken_link and i == 3)]
    assert postbox_os.append_regular_private(str(err_log), b"gen-0-" + (b"x" * 120) + b"\n", max_bytes=50) is True
    assert fsynced_dirs_os
    for step, slot in enumerate(free_slots, start=1):
        payload = ("gen-%d-" % step).encode("ascii") + (b"y" * 120) + b"\n"
        assert postbox_os.append_regular_private(str(err_log), payload, max_bytes=50) is True
        assert (tmp_path / ("postbox.err.log.%d" % slot)).read_bytes().startswith(("gen-%d-" % (step - 1)).encode("ascii"))

    active_before = err_log.read_bytes()
    slots_before = {s: (tmp_path / ("postbox.err.log.%d" % s)).read_bytes() for s in free_slots}
    # All 16 slots are now occupied (15 regular + 1 broken symlink, or 16 regular): rotation MUST fail closed.
    assert postbox_os.append_regular_private(str(err_log), b"must-not-clobber\n", max_bytes=50) is False
    assert err_log.read_bytes() == active_before
    for s in free_slots:
        assert (tmp_path / ("postbox.err.log.%d" % s)).read_bytes() == slots_before[s]
    if has_broken_link:
        assert os.path.islink(tmp_path / "postbox.err.log.3")

    # 2. delivery_bank.append_regular_private + latency.py across .1..16 + active log in chronological order.
    monkeypatch.setattr(delivery_bank, "DISPATCH_LOG_MAX", 10)
    spool, state = tmp_path / "seat.log", tmp_path / "seat-state.json"
    state.write_text(json.dumps({"peer_id": "peer-r4"}), encoding="utf-8")
    (tmp_path / "seat-state.json.read.json").write_text(json.dumps({
        "schema_version": 1, "peer_id": "peer-r4",
        "reads": [{"key": "msg/already", "version": 1, "at": "2026-11-02T09:59:00.000Z"}],
    }), encoding="utf-8")
    ident = {"name": "seat", "log": str(spool), "state": str(state)}
    b = delivery_bank.Bank(ident, str(spool) + ".codex.offset", 0, "codex")
    with open(spool, "ab") as f:
        f.write((json.dumps({"event": "message", "key": "msg/already", "version": 1, "role": "to",
                             "created_at": "2026-11-02T09:58:00.000Z", "rx_at": "2026-11-02T09:58:01.000Z"}) + "\n").encode("utf-8"))
    end, ev = b.collect(lambda d: d.get("event") == "message", fetched=lambda: delivery_bank.read_ledger(ident))
    assert ev == []
    b.commit(end, ev)
    assert fsynced_dirs_bank
    for idx in range(18):
        with open(spool, "ab") as f:
            f.write((json.dumps({"event": "message", "key": "msg/m%d" % idx, "version": 1, "role": "to",
                                 "created_at": "2026-11-02T10:%02d:00.000Z" % idx,
                                 "rx_at": "2026-11-02T10:%02d:01.000Z" % idx}) + "\n").encode("utf-8"))
        end, ev = b.collect(lambda d: d.get("event") == "message", fetched=lambda: delivery_bank.read_ledger(ident))
        b.commit(end, ev)
    for slot in range(1, 17):
        arch = json.loads((tmp_path / ("seat.log.codex.offset.dispatch.jsonl.%d" % slot)).read_text(encoding="utf-8").strip())
        assert [e["key"] for e in arch["events"]] == ["msg/m%d" % (slot - 1)]
    active_disp = json.loads((tmp_path / "seat.log.codex.offset.dispatch.jsonl").read_text(encoding="utf-8").strip())
    assert [e["key"] for e in active_disp["events"]] == ["msg/m16"]
    rep = latency.report(ident)
    assert rep["consumers"]["codex"] == 17 and rep["skips"].get("already_read") == 1
    (tmp_path / "seat.log.wait-ghost.offset.dispatch.jsonl.14").write_text("{}\n", encoding="utf-8")
    (tmp_path / "seat.log.wait-skiponly.offset.skips.jsonl.16").write_text("{}\n", encoding="utf-8")
    discovered = [latency.consumer_name(str(spool), p) for p in latency.offset_files(str(spool))]
    assert "wait-ghost" in discovered and "wait-skiponly" in discovered


def test_pre_spawn_helper_intent_record_before_popen_and_orphan_settlement(tmp_path, monkeypatch):
    hdir = tmp_path / "seat.log.helpers"
    observed_on_popen = []
    real_popen = subprocess.Popen

    def spy_popen(argv, *args, **kw):
        files = sorted(p for p in hdir.iterdir() if not p.name.startswith(".")) if hdir.exists() else []
        if files:
            observed_on_popen.append((files[0].name, json.loads(files[0].read_text(encoding="utf-8"))))
        return real_popen(argv, *args, **kw)

    monkeypatch.setattr(postbox_os.subprocess, "Popen", spy_popen)
    res = postbox_os.run_bounded(
        [sys.executable, "-S", "-c", "print('ok')"],
        text=True, helper_dir=str(hdir), helper_kind="send_drain", helper_mutating=True, timeout=10,
    )
    assert res.returncode == 0 and res.stdout.strip() == "ok"
    assert len(observed_on_popen) == 1
    rec_name, pre_rec = observed_on_popen[0]
    assert rec_name.startswith("h-send_drain-")
    assert pre_rec["state"] == "spawning" and pre_rec["pid"] is None and pre_rec["mutating"] is True
    assert list(hdir.iterdir()) == []

    # When Popen itself raises, the pre-spawn record is unlinked immediately.
    def boom_popen(*_a, **_kw):
        files = [p for p in hdir.iterdir() if not p.name.startswith(".")]
        assert len(files) == 1
        raise OSError("exec failed before fork completed")

    monkeypatch.setattr(postbox_os.subprocess, "Popen", boom_popen)
    with pytest.raises(OSError, match="exec failed"):
        postbox_os.run_bounded(["nope"], helper_dir=str(hdir), helper_kind="send_drain", helper_mutating=True)
    assert [p for p in hdir.iterdir() if not p.name.startswith(".")] == []
    monkeypatch.undo()

    # Orphaned pre-spawn records from a dead daemon: mutating fails closed; read-only is reaped only when its gate proves
    # the helper never ran; an ungated read-only record proves nothing, so it is kept and reported.
    mut_spawn = hdir / "h-send_drain-99999991-100.json"
    ro_spawn = hdir / "h-owed-99999991-101.json"
    gated_spawn = hdir / "h-owed-99999991-102.json"
    postbox_os.write_private_json_atomic(str(mut_spawn), {
        "schema": 1, "state": "spawning", "kind": "send_drain", "mutating": True, "pid": None,
        "pgid": None, "create_time": None, "create_time_known": False,
        "daemon_pid": 99999991, "daemon_create_time": 1.0, "started_at": time.time() - 20, "timeout_s": 10.0,
    })
    postbox_os.write_private_json_atomic(str(ro_spawn), {
        "schema": 1, "state": "spawning", "kind": "owed", "mutating": False, "pid": None,
        "pgid": None, "create_time": None, "create_time_known": False,
        "daemon_pid": 99999991, "daemon_create_time": 1.0, "started_at": time.time() - 20, "timeout_s": 10.0,
    })

    postbox_os.write_private_json_atomic(str(gated_spawn), {
        "schema": 1, "state": "spawning", "kind": "owed", "mutating": False, "gated": True, "pid": None,
        "pgid": None, "create_time": None, "create_time_known": False,
        "daemon_pid": 99999991, "daemon_create_time": 1.0, "started_at": time.time() - 20, "timeout_s": 10.0,
    })

    s_ro = postbox_os.settle_helper_dir(str(hdir), only_kind="owed")
    assert s_ro["settled"] is False and ro_spawn.exists() and not gated_spawn.exists()
    assert s_ro["issues"] == ["helper_spawning_unsettled:owed:%s" % ro_spawn.name]
    assert s_ro["reaped"] == [{"kind": "owed", "pid": None, "state": "spawning_gated_never_ran"}]

    s_mut = postbox_os.settle_helper_dir(str(hdir), only_kind="send_drain")
    assert s_mut["settled"] is False and mut_spawn.exists()
    assert len(s_mut["active_mutating"]) == 1 and s_mut["active_mutating"][0]["pid"] is None
    assert any("mutating_helper_spawning_unsettled:send_drain:" in x for x in s_mut["issues"])


def test_owed_and_neighbours_isolation_and_auto_settlement(tmp_path):
    hdir = tmp_path / "seat.log.helpers"
    hdir.mkdir(parents=True)

    # 300 leaked `owed` records + corrupt `owed` and `neighbours` records MUST NOT block mutating kinds.
    for idx in range(300):
        (hdir / ("h-owed-99999992-%d.json" % (1000 + idx))).write_text(json.dumps({
            "schema": 1, "state": "running", "kind": "owed", "mutating": False, "pid": 99999970,
            "pgid": None, "create_time": 1.0, "create_time_known": True,
            "daemon_pid": 99999992, "daemon_create_time": 1.0, "started_at": 1.0, "timeout_s": 8.0,
        }), encoding="utf-8")
    (hdir / "h-owed-99999992-999991.json").write_bytes(b"{corrupt-owed")
    (hdir / "h-neighbours-99999992-999992.json").write_bytes(b"{corrupt-neighbours")

    for mut_kind in ("send_drain", "codex_queue", "monitor_anchor"):
        res = postbox_os.settle_helper_dir(str(hdir), only_kind=mut_kind)
        assert res["settled"] is True and res["issues"] == [] and res["active_mutating"] == []

    # A corrupt record of `send_drain` itself (or an untyped legacy file) STILL fails closed for `send_drain`.
    bad_drain = hdir / "h-send_drain-99999992-999993.json"
    bad_drain.write_bytes(b"{corrupt-drain")
    res_bad = postbox_os.settle_helper_dir(str(hdir), only_kind="send_drain")
    assert res_bad["settled"] is False and any("corrupt_helper_record:" in x for x in res_bad["issues"])
    bad_drain.unlink()

    bad_legacy = hdir / "h-99999992-777-999994.json"
    bad_legacy.write_bytes(b"{corrupt-legacy")
    res_leg = postbox_os.settle_helper_dir(str(hdir), only_kind="send_drain")
    assert res_leg["settled"] is False and any("corrupt_helper_record:" in x for x in res_leg["issues"])

    # Clean directory and verify owedline._call + graphline._lookup reap orphaned sleepers before spawning.
    for p in hdir.iterdir():
        p.unlink()
    ident = {
        "name": "seat",
        "python": sys.executable,
        "client": str(tmp_path),
        "state": str(tmp_path / "state.json"),
        "base_url": "http://127.0.0.1:9",
        "log": str(tmp_path / "seat.log"),
    }
    for kind, trigger in (
        ("owed", lambda: owedline._call(ident, "2026-10-01T00:00:00Z", lambda a, **_k: subprocess.CompletedProcess(a, 0, '{"owed":[]}', ""))),
        ("neighbours", lambda: graphline._lookup(ident, "msg/1791000000001-peer-a", lambda a, **_k: subprocess.CompletedProcess(a, 0, "{}", ""))),
    ):
        sleeper = subprocess.Popen([sys.executable, "-S", "-c", "import time; time.sleep(60)"])
        try:
            ct = postbox_os.process_create_time(sleeper.pid)
            assert ct is not None
            rec_file = hdir / ("h-%s-99999993-12345.json" % kind)
            postbox_os.write_private_json_atomic(str(rec_file), {
                "schema": 1, "state": "running", "kind": kind, "mutating": False, "pid": sleeper.pid,
                "pgid": None, "create_time": ct, "create_time_known": True,
                "daemon_pid": 99999993, "daemon_create_time": 1.0, "started_at": time.time() - 30, "timeout_s": 8.0,
            })
            trigger()
            sleeper.wait(timeout=10)
            assert not rec_file.exists()
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=5)


def test_pgid_members_caps_no_psutil_ps_stream_and_end_to_end_link_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(postbox_os, "IS_WINDOWS", False)
    monkeypatch.setattr(postbox_os, "_psutil", lambda: None)
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(os, "getpgrp", lambda: 1, raising=False)
    monkeypatch.setattr(os, "getpgid", lambda p: p, raising=False)
    monkeypatch.setattr(os, "killpg", lambda *_a: None, raising=False)

    oversize_ps = (b" 100 200 501\n" * ((postbox_os.MAX_PGID_PS_BYTES // 13) + 200))

    class FakePsPopen:
        pid = 888001
        returncode = 0

        def __init__(self, argv, **_kw):
            self.argv = argv
            self.stdout = io.BytesIO(oversize_ps if argv[:2] == ["ps", "-axo"] else b"00:05\n")
            self.stderr = io.BytesIO(b"")

        def poll(self):
            return self.returncode

        def wait(self, **_kw):
            return self.returncode

    monkeypatch.setattr(postbox_os.subprocess, "Popen", FakePsPopen)
    assert postbox_os._pgid_members(200) == (False, [])

    # Under the 2 MiB cap with matching uid and pgid, _pgid_members succeeds cleanly.
    oversize_ps = b" 444 200 501\n 445 999 501\n"
    ok, members = postbox_os._pgid_members(200)
    assert ok is True and len(members) == 1 and members[0][0] == 444
    monkeypatch.undo()

    # End-to-end symlink/non-regular refusal on delivery_bank.read_ledger and meshy_channel._append_channel_err.
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"peer_id": "p1"}), encoding="utf-8")
    real_ledger = tmp_path / "real.read.json"
    real_ledger.write_text(json.dumps({
        "schema_version": 1, "peer_id": "p1",
        "reads": [{"key": "msg/k1", "version": 1, "at": "2026-11-02T10:00:00Z"}],
    }), encoding="utf-8")
    ledger_target = tmp_path / "s.json.read.json"
    err_target = tmp_path / "seat.log.channel.err"
    made_link = False
    if hasattr(os, "symlink"):
        try:
            os.symlink(str(real_ledger), str(ledger_target))
            os.symlink(str(tmp_path / "sink.txt"), str(err_target))
            made_link = True
        except OSError:
            made_link = False
    if not made_link:
        ledger_target.mkdir()
        err_target.mkdir()
    notes = []
    assert delivery_bank.read_ledger({"name": "seat", "state": str(state)}, report=notes.append) == {}
    assert notes
    meshy_channel._append_channel_err({"log": str(tmp_path / "seat.log")}, {"phase": "probe"})
    assert not (tmp_path / "sink.txt").exists()


def test_saturated_archives_surface_refused_counter_sidecar_and_recover_after_prune(tmp_path, monkeypatch):
    from tools.postbox import postbox as pb

    postbox_os.reset_refused_append_counts()
    err_log = tmp_path / "postbox.err.log"
    status_file = tmp_path / "status.json"
    disp_log = tmp_path / "seat.log.codex.offset.dispatch.jsonl"
    monkeypatch.setattr(pb, "ERR", str(err_log))
    monkeypatch.setattr(pb, "STATUS", str(status_file))
    monkeypatch.setattr(pb, "_status", {})
    monkeypatch.setattr(pb, "_ident_logs", {})
    monkeypatch.setattr(pb, "_status_write_error", None)
    monkeypatch.setattr(delivery_bank, "DISPATCH_LOG_MAX", 20)

    # Fill active + .1..16 on both postbox.err.log and .dispatch.jsonl.
    err_log.write_bytes(b"active-err-" + (b"e" * 5 * 1048576) + b"\n")
    disp_log.write_bytes(b'{"active":true,"pad":"' + (b"d" * 80) + b'"}\n')
    for idx in range(1, 17):
        (tmp_path / ("postbox.err.log.%d" % idx)).write_bytes(("err-slot-%d\n" % idx).encode("ascii"))
        (tmp_path / ("seat.log.codex.offset.dispatch.jsonl.%d" % idx)).write_bytes(('{"slot":%d}\n' % idx).encode("ascii"))

    assert pb.note("overflow-1") is False and pb.note("overflow-2") is False
    assert delivery_bank.dispatch_line(str(disp_log), {"at": "2026-11-02T12:00:00Z", "events": []}) is False
    assert postbox_os.refused_append_count(str(err_log)) == 2 and postbox_os.refused_append_count(str(disp_log)) == 1
    err_sat = json.loads((tmp_path / "postbox.err.log.saturated.json").read_text(encoding="utf-8"))
    disp_sat = json.loads((tmp_path / "seat.log.codex.offset.dispatch.jsonl.saturated.json").read_text(encoding="utf-8"))
    assert err_sat["reason"] == "archive_slots_full" and err_sat["refused_count"] == 2
    assert disp_sat["reason"] == "archive_slots_full" and disp_sat["refused_count"] == 1
    assert pb.set_status("seat", listener="stopped") is True
    st = json.loads(status_file.read_text(encoding="utf-8"))
    assert st["seat"]["refused_log_appends"] == {"postbox.err.log": 2, "seat.log.codex.offset.dispatch.jsonl": 1}
    assert pb.liveness(st) == [("seat", True, "seat: listener stopped (not claimed running)")]

    # Cross-process consumers (.channel.err, .dispatch.jsonl, .skips.jsonl) surface in status.json when daemon counts are empty.
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    pb._ident_logs.update({"claude-a": str(spool_dir / "claude-a.log"), "wait-b": str(spool_dir / "wait-b.log")})
    ch_err = spool_dir / "claude-a.log.channel.err"
    wt_disp = spool_dir / "wait-b.log.wait-session.offset.dispatch.jsonl"
    wt_skip = spool_dir / "wait-b.log.wait-session.offset.skips.jsonl"
    for target in (ch_err, wt_disp, wt_skip):
        target.write_bytes(b"active-overflow-" + (b"x" * 60) + b"\n")
        for idx in range(1, 17):
            (spool_dir / ("%s.%d" % (target.name, idx))).write_bytes(b"slot\n")
    child_code = (
        "import sys; from tools.postbox import delivery_bank, meshy_channel\n"
        "delivery_bank.DISPATCH_LOG_MAX = meshy_channel.CHANNEL_ERR_MAX = 20\n"
        "meshy_channel._append_channel_err({'log': sys.argv[1]}, {'phase': 'p1'})\n"
        "meshy_channel._append_channel_err({'log': sys.argv[1]}, {'phase': 'p2'})\n"
        "assert delivery_bank.dispatch_line(sys.argv[2], {'events': []}) is False\n"
        "assert delivery_bank.dispatch_line(sys.argv[3], {'key': 'msg/s'}) is False\n"
    )
    subprocess.run([sys.executable, "-c", child_code, str(spool_dir / "claude-a.log"), str(wt_disp), str(wt_skip)],
                   check=True, cwd=os.path.dirname(os.path.dirname(__file__)))
    postbox_os.reset_refused_append_counts()
    (tmp_path / "postbox.err.log.16").unlink()
    (tmp_path / "seat.log.codex.offset.dispatch.jsonl.16").unlink()
    assert pb.set_status("claude-a", listener="running") is True and pb.set_status("wait-b", listener="running") is True
    st_xp = json.loads(status_file.read_text(encoding="utf-8"))
    assert st_xp["claude-a"]["refused_log_appends"] == {"claude-a.log.channel.err": 2}
    assert st_xp["wait-b"]["refused_log_appends"] == {
        "wait-b.log.wait-session.offset.dispatch.jsonl": 1, "wait-b.log.wait-session.offset.skips.jsonl": 1,
    }
    for target in (ch_err, wt_disp, wt_skip):
        (spool_dir / ("%s.16" % target.name)).unlink()
    assert pb.set_status("claude-a", listener="running") is True and pb.set_status("wait-b", listener="running") is True
    st_after = json.loads(status_file.read_text(encoding="utf-8"))
    assert "refused_log_appends" not in st_after["claude-a"] and "refused_log_appends" not in st_after["wait-b"]
    assert not (tmp_path / "postbox.err.log.saturated.json").exists()
    assert not (spool_dir / "claude-a.log.channel.err.saturated.json").exists()

    # POSIX os.link: (a) EEXIST race skips slot; (b) interrupted link+crash completes unlink without double-archiving.
    posix_dir = tmp_path / "posix_rot"
    posix_dir.mkdir()
    p_log = posix_dir / "race.log"
    p_log.write_bytes(b"first-archive-" + (b"z" * 100) + b"\n")

    def fake_link(src, dst):
        if dst.endswith(".1"):
            (posix_dir / "race.log.1").write_bytes(b"concurrent-writer-won\n")
            raise FileExistsError("slot 1 raced")
        with open(dst, "wb") as f_out, open(src, "rb") as f_in:
            f_out.write(f_in.read())

    monkeypatch.setattr(postbox_os, "IS_WINDOWS", False)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(p_log).st_uid, raising=False)
    monkeypatch.setattr(postbox_os, "fsync_dir", lambda _d: None)
    alias_skip = posix_dir / "alias.log.codex.offset.skips.jsonl"
    alias_skip.write_bytes(b'{"key":"msg/a0","reason":"already_read","at":"2026-11-02T10:01:00.000Z"}\n')
    os.link(str(alias_skip), str(alias_skip) + ".1")
    assert postbox_os.append_regular_private(
        str(alias_skip), b'{"key":"msg/a1","reason":"already_read","at":"2026-11-02T10:02:00.000Z"}\n', max_bytes=20,
    ) is True
    assert not (posix_dir / "alias.log.codex.offset.skips.jsonl.2").exists()
    assert latency.report({"name": "alias", "log": str(posix_dir / "alias.log")})["skips"]["already_read"] == 2
    monkeypatch.setattr(os, "link", fake_link, raising=False)
    assert postbox_os.append_regular_private(str(p_log), b"fresh\n", max_bytes=20) is True
    assert (posix_dir / "race.log.1").read_bytes() == b"concurrent-writer-won\n"
    assert (posix_dir / "race.log.2").read_bytes().startswith(b"first-archive-")
    monkeypatch.undo()

    scan_dir = tmp_path / "cap_helpers"
    scan_dir.mkdir()
    (scan_dir / "h-owed-1-1.json").write_text("{}", encoding="utf-8")
    (scan_dir / "h-owed-1-2.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(postbox_os, "MAX_HELPER_DIR_SCAN", 1)
    capped = postbox_os.settle_helper_dir(str(scan_dir), only_kind="owed")
    assert capped["settled"] is False
    assert any("helper_dir_scan_cap_exceeded" in x and "cap_helpers" in x for x in capped["issues"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
