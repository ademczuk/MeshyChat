"""HZ Round 3 owner tests for postbox_os, cache/config readers, sidecar qualification, helper settlement, and S7."""
from __future__ import annotations

import importlib.util, io, json, os, stat, subprocess, sys, threading, time, tracemalloc, types
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
if str(PB) not in sys.path:
    sys.path.insert(0, str(PB))
import context_worker, delivery_bank, graphline, mark_handled, meshy_channel, owedline, postbox_os, wait_next  # noqa: E402


def _traced(fn):
    tracemalloc.start()
    try:
        tracemalloc.reset_peak(); out = fn(); return out, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _write_sparse(path: Path, size: int) -> None:
    with open(path, "wb") as f:
        f.seek(size - 1); f.write(b"\n")


def _ident(tmp_path: Path, name: str = "seat-r3") -> dict:
    client = tmp_path / "client"
    (client / "tools" / "meshy_node").mkdir(parents=True, exist_ok=True)
    state, handled, log = tmp_path / f"{name}.state.json", tmp_path / f"{name}.handled.json", tmp_path / f"{name}.log"
    state.write_text(json.dumps({"peer_id": "peer-r3", "label": name}), encoding="utf-8")
    delivery_bank.atomic_json(str(handled), {"schema_version": 1, "identity": name, "handled": []})
    log.write_text("", encoding="utf-8")
    return {"name": name, "address": name, "python": sys.executable, "client": str(client), "state": str(state),
            "log": str(log), "base_url": "http://127.0.0.1:9", "handled_file": str(handled), "handled_private_acl_verified": True}


def test_p1_a2_oversized_caches_and_configs_refused_before_allocation(tmp_path, monkeypatch):
    ident = _ident(tmp_path)
    labels_p, cache_p, stats_p, owed_p = (Path(graphline._labels_path(ident)), Path(graphline._cache_path(ident)),
                                          Path(graphline._stats_path(ident)), Path(owedline._cache_path(ident)))
    for p in (labels_p, cache_p, stats_p, owed_p):
        _write_sparse(p, 8 * 1048576)
    (_, peak_g) = _traced(lambda: (graphline._server_labels(ident), graphline._load(str(cache_p)),
                                   graphline._read_stats(str(stats_p)), graphline.stats(ident), owedline._read(ident)))
    assert peak_g < 1048576 and graphline._server_labels(ident) == set() and graphline._load(str(cache_p)) == {}
    assert graphline._read_stats(str(stats_p)) == {} and graphline.stats(ident) is None and owedline._read(ident) is None
    _write_sparse(Path(ident["client"]) / "tools" / "meshy_node" / "cli.py", 4 * 1048576)
    routes, peak_cli = _traced(lambda: postbox_os.lookup_routes(ident["client"]))
    assert routes == () and peak_cli < 1048576
    big_cfg = tmp_path / "config.json"
    _write_sparse(big_cfg, 4 * 1048576)
    assert postbox_os.config_stat(str(big_cfg)) is not None
    prev_opts = {"hold": None, "hold_max_age": 123}
    (sig, kept), peak_cfg = _traced(lambda: postbox_os.refresh_hold_options(str(big_cfg), ident, (1, 1), prev_opts))
    assert sig == (1, 1) and kept == prev_opts and peak_cfg < 1048576
    monkeypatch.setattr(meshy_channel, "HERE", str(tmp_path))
    with pytest.raises(postbox_os.OversizeFileError): meshy_channel.load_identities()  # noqa: E701
    monkeypatch.setenv("MESHY_POSTBOX_CONFIG", str(big_cfg))
    monkeypatch.setattr(wait_next, "stdout_discarded", lambda: False)
    monkeypatch.setattr(sys, "argv", ["wait_next.py", "seat-r3", "--once"])
    with pytest.raises(postbox_os.OversizeFileError): wait_next.main()  # noqa: E701
    monkeypatch.setattr(sys, "argv", ["mark_handled.py", "seat-r3", "--config", str(big_cfg), "--init"])
    with pytest.raises(postbox_os.OversizeFileError): mark_handled.main()  # noqa: E701
    if hasattr(os, "name") and "msvcrt" not in sys.modules:
        monkeypatch.setitem(sys.modules, "msvcrt", types.ModuleType("msvcrt"))
    spec = importlib.util.spec_from_file_location("kimi_arm_hook_r3", PB / "kimi_arm_hook.py")
    hook = importlib.util.module_from_spec(spec); spec.loader.exec_module(hook)
    _write_sparse(tmp_path / "kimi-seats.json", 4 * 1048576)
    monkeypatch.setattr(hook, "HERE", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id":"s1","cwd":"x"}'))
    assert hook.main() == 0 and hook.resolve_cli([str(tmp_path)], here=str(tmp_path), out=io.StringIO()) == 2


def test_p1_a3_postbox_os_and_channel_err_regular_nonblocking_private_io(tmp_path, monkeypatch):
    not_reg = tmp_path / "dir_target"
    not_reg.mkdir()
    assert postbox_os.append_regular_private(str(not_reg), b"x\n", max_bytes=1024) is False
    for fn in (lambda: postbox_os.write_private_json_atomic(str(not_reg), {"ok": 1}),
               lambda: postbox_os.read_bounded_json(str(not_reg), max_bytes=1024)):
        with pytest.raises(ValueError): fn()  # noqa: E701
    real_file, link_file = tmp_path / "real.json", tmp_path / "link.json"
    real_file.write_text('{"ok": 1}', encoding="utf-8")
    try:
        os.symlink(real_file, link_file); can_symlink = True
    except (AttributeError, NotImplementedError, OSError):
        can_symlink = False
    if can_symlink:
        assert postbox_os.append_regular_private(str(link_file), b"x\n", max_bytes=1024) is False
        for fn in (lambda: postbox_os.write_private_json_atomic(str(link_file), {"ok": 2}),
                   lambda: postbox_os.read_bounded_json(str(link_file), max_bytes=1024)):
            with pytest.raises(ValueError): fn()  # noqa: E701
        assert can_symlink or True
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "sidecar.fifo"; os.mkfifo(fifo)
        assert postbox_os.append_regular_private(str(fifo), b"x\n", max_bytes=1024) is False
        with pytest.raises((OSError, ValueError)): postbox_os.read_bounded_json(str(fifo), max_bytes=1024)  # noqa: E701
    rot_err = tmp_path / "rot.channel.err"
    rot_err.write_bytes(b"a" * 300)
    assert postbox_os.append_regular_private(str(rot_err), b"b\n", max_bytes=100) is True
    rot_err.write_bytes(b"c" * 300)
    assert postbox_os.append_regular_private(str(rot_err), b"d\n", max_bytes=100) is True
    assert (tmp_path / "rot.channel.err.1").read_bytes() == b"a" * 300 and (tmp_path / "rot.channel.err.2").read_bytes() == b"c" * 300
    for idx in range(1, 17):
        slot = tmp_path / f"rot.channel.err.{idx}"
        slot.write_bytes(f"slot-{idx}".encode("ascii"))
        os.utime(slot, ns=(1000000000 + idx * 1000000, 1000000000 + idx * 1000000))
    rot_err.write_bytes(b"overflow" * 30)
    assert postbox_os.append_regular_private(str(rot_err), b"tail\n", max_bytes=100) is False
    assert rot_err.read_bytes() == b"overflow" * 30
    assert [((tmp_path / f"rot.channel.err.{idx}").read_bytes()) for idx in range(1, 17)] == [
        f"slot-{idx}".encode("ascii") for idx in range(1, 17)
    ]
    target_err = tmp_path / "seat.log.channel.err"
    target_err.write_bytes(b"seed\n")
    if os.name == "posix":
        os.chmod(target_err, 0o644)
        meshy_channel._append_channel_err({"log": str(tmp_path / "seat.log")}, {"phase": "test"})
        assert stat.S_IMODE(os.lstat(target_err).st_mode) == 0o600
    else:
        chmod_calls, orig_fstat, file_uid = [], os.fstat, os.lstat(target_err).st_uid
        monkeypatch.setattr(postbox_os, "IS_WINDOWS", False)
        monkeypatch.setattr(os, "getuid", lambda: file_uid, raising=False)
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: chmod_calls.append((fd, mode)), raising=False)
        monkeypatch.setattr(os, "fstat", lambda fd: types.SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644, st_uid=file_uid, st_size=5, st_file_attributes=0)
            if isinstance(fd, int) else orig_fstat(fd))
        assert postbox_os.append_regular_private(str(target_err), b"line\n", max_bytes=4096) is True
        assert chmod_calls and chmod_calls[-1][1] == 0o600


def test_p2_a2_a3_run_bounded_pipe_errors_injected_caps_and_helper_settlement(tmp_path, monkeypatch):
    capped = postbox_os.run_bounded(
        ["x"], max_stdout=1024, max_stderr=512, text=True,
        run_fn=lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, "A" * 20000, "E" * 5000))
    assert capped.stdout_truncated and capped.stderr_truncated and len(capped.stdout) == 1025 and len(capped.stderr) == 513
    ident = _ident(tmp_path, name="seat-trunc")
    graphline._backoff_until.clear(); owedline._backoff_until.clear()
    big_json = "{" + (" " * (owedline.STDOUT_MAX + 50)) + "}"
    assert graphline._lookup(ident, "msg/1791000000001-peer-a", lambda a, **_k: subprocess.CompletedProcess(a, 0, big_json, ""))[0] is None
    assert owedline._call(ident, "2026-10-01T00:00:00Z", lambda a, **_k: subprocess.CompletedProcess(a, 0, big_json, "")) is None

    class FakePopen:
        pid = 999991
        def __init__(self, *_a, **_kw):
            self.stdout = types.SimpleNamespace(read=lambda _n=-1: (_ for _ in ()).throw(OSError("simulated broken pipe read")), close=lambda: None)
            self.stderr = io.BytesIO(b"")
        wait = poll = staticmethod(lambda *_a, **_kw: 0)

    monkeypatch.setattr(postbox_os.subprocess, "Popen", FakePopen)
    with pytest.raises(OSError, match="pipe reader failed on stdout"): postbox_os.run_bounded(["x"], max_stdout=1024, max_stderr=1024, text=True)  # noqa: E701
    killpg_calls, unblock_ev, reaped_terms = [], threading.Event(), []
    class ExitedLeaderInheritedPipePopen:
        pid, returncode = 777001, None
        def __init__(self, *_a, **_kw):
            self.stdout = types.SimpleNamespace(read=lambda _n=-1: (unblock_ev.wait(2.0), b"")[1], close=lambda: None)
            self.stderr = io.BytesIO(b"")
        def poll(self): self.returncode = 0; return 0
        def wait(self, **_kw): self.returncode = 0; return 0
        kill = staticmethod(lambda: None)

    for k, v in (("getpgrp", lambda: 1), ("getpgid", lambda p: p),
                 ("killpg", lambda g, s: (killpg_calls.append((g, s)), unblock_ev.set()))):
        monkeypatch.setattr(os, k, v, raising=False)
    monkeypatch.setattr(postbox_os, "IS_WINDOWS", False)
    monkeypatch.setattr(postbox_os, "process_create_time", lambda _p: None)
    monkeypatch.setattr(postbox_os.subprocess, "Popen", ExitedLeaderInheritedPipePopen)
    with pytest.raises(subprocess.TimeoutExpired): postbox_os.run_bounded(["x"], timeout=0.05, text=True)  # noqa: E701
    assert len(killpg_calls) == 1 and killpg_calls[0][0] == 777001
    monkeypatch.setattr(postbox_os, "_pgid_members", lambda g: (True, [(777002, time.time())]))
    monkeypatch.setattr(postbox_os, "terminate_owned_tree", lambda m, ct, **_k: (reaped_terms.append(m), True)[1])
    postbox_os._cleanup_session_group(types.SimpleNamespace(returncode=0), 777001, time.time(), time.time())
    assert len(killpg_calls) == 1 and reaped_terms == [777002]
    popen_argvs, gate_read_ends = [], []
    class NonRecursivePopen:
        pid, returncode = 777010, None
        def __init__(self, argv, **kw):
            popen_argvs.append(list(argv)); self.stdout, self.stderr = io.BytesIO(b"00:01\n"), io.BytesIO(b"")  # noqa: E702
            if kw.get("pass_fds"): gate_read_ends.append(os.dup(kw["pass_fds"][-2]))  # noqa: E701 - the launcher's end of the go pipe
        def poll(self): self.returncode = 0; return 0
        def wait(self, **_kw): self.returncode = 0; return 0
    monkeypatch.undo()
    for k, v in (("IS_WINDOWS", False), ("_psutil", lambda: None), ("_SELF_CTIME_CACHE", {}),
                 ("write_private_json_atomic", lambda *_a, **_k: None)):
        monkeypatch.setattr(postbox_os, k, v)
    for k, v in (("getpgrp", lambda: 1), ("getpgid", lambda p: p),
                 ("killpg", lambda *_a: (_ for _ in ()).throw(ProcessLookupError()))):
        monkeypatch.setattr(os, k, v, raising=False)
    monkeypatch.setattr(postbox_os.subprocess, "Popen", NonRecursivePopen)
    assert postbox_os.run_bounded(["worker"], text=True).returncode == 0 and popen_argvs == [["worker"]]
    assert postbox_os.run_bounded(["worker"], helper_dir=str(tmp_path / "nr.helpers"), text=True).returncode == 0
    assert len(popen_argvs) == 4 and popen_argvs[1][0] == "ps" and popen_argvs[2][-1] == "worker" and popen_argvs[3][0] == "ps"
    assert popen_argvs[2][:3] == [sys.executable, "-I", "-S"]             # a recorded helper starts behind the gate
    assert postbox_os.run_bounded(["worker"], helper_dir=str(tmp_path / "nr.helpers"), text=True).returncode == 0
    assert len(popen_argvs) == 6 and popen_argvs[4][-1] == "worker" and popen_argvs[5][0] == "ps"
    for fd in gate_read_ends:
        assert os.read(fd, 1) == b"1"                                     # released only once the record was attached
        os.close(fd)
    monkeypatch.undo()

    hdir = tmp_path / "seat.log.helpers"
    sleeper = subprocess.Popen([sys.executable, "-S", "-c", "import time; time.sleep(60)"])
    try:
        ct = postbox_os.process_create_time(sleeper.pid)
        assert ct is not None
        postbox_os.write_private_json_atomic(str(hdir / "h-dead-ro.json"), {
            "schema": 1, "kind": "inbox", "mutating": False, "pid": sleeper.pid, "pgid": None, "create_time": ct,
            "create_time_known": True, "daemon_pid": 99999991, "daemon_create_time": 1.0, "started_at": time.time() - 30, "timeout_s": 10.0})
        res = postbox_os.settle_helper_dir(str(hdir))
        assert res["settled"] and res["issues"] == [] and res["reaped"] == [{"kind": "inbox", "pid": sleeper.pid, "state": "terminated"}]
        sleeper.wait(timeout=10)
    finally:
        if sleeper.poll() is None: sleeper.kill(); sleeper.wait(timeout=5)  # noqa: E701
    mut_proc = subprocess.Popen([sys.executable, "-S", "-c", "import time; time.sleep(60)"])
    try:
        mut_ct = postbox_os.process_create_time(mut_proc.pid)
        rec_mut, rec_reused, rec_unk, rec_unv = hdir / "h-mut.json", hdir / "h-reused.json", hdir / "h-unk.json", hdir / "h-unv.json"
        postbox_os.write_private_json_atomic(str(rec_mut), {
            "schema": 1, "kind": "send_drain", "mutating": True, "pid": mut_proc.pid, "pgid": None, "create_time": mut_ct,
            "create_time_known": True, "daemon_pid": 99999992, "daemon_create_time": 1.0, "started_at": time.time() - 5, "timeout_s": 30.0})
        res_mut = postbox_os.settle_helper_dir(str(hdir))
        assert mut_proc.poll() is None and not res_mut["settled"] and len(res_mut["active_mutating"]) == 1 and rec_mut.exists()
        assert any("mutating_helper_unsettled:send_drain:" in x for x in res_mut["issues"])
        postbox_os.write_private_json_atomic(str(rec_reused), {
            "schema": 1, "kind": "inbox", "mutating": False, "pid": mut_proc.pid, "pgid": mut_proc.pid, "create_time": (mut_ct or 100.0) + 500.0,
            "create_time_known": True, "daemon_pid": 99999993, "daemon_create_time": 1.0, "started_at": time.time() - 5, "timeout_s": 10.0})
        res_reused = postbox_os.settle_helper_dir(str(hdir))
        assert mut_proc.poll() is None and not rec_reused.exists() and {"kind": "inbox", "pid": mut_proc.pid, "state": "already_exited"} in res_reused["reaped"]
        rec_mut.unlink()
        postbox_os.write_private_json_atomic(str(rec_unk), {
            "schema": 1, "kind": "inbox", "mutating": False, "pid": mut_proc.pid, "pgid": mut_proc.pid, "create_time": None,
            "create_time_known": False, "daemon_pid": 99999994, "daemon_create_time": 1.0, "started_at": time.time() - 5, "timeout_s": 10.0})
        res_unk = postbox_os.settle_helper_dir(str(hdir))
        assert mut_proc.poll() is None and not res_unk["settled"] and any("helper_create_time_unknown:inbox:" in x for x in res_unk["issues"])
        rec_unk.unlink()
        postbox_os.write_private_json_atomic(str(rec_unv), {
            "schema": 1, "kind": "inbox", "mutating": False, "pid": 99999988, "pgid": 99999988, "create_time": 100.0,
            "create_time_known": True, "daemon_pid": 99999995, "daemon_create_time": 1.0, "started_at": time.time() - 5, "timeout_s": 10.0})
        monkeypatch.setattr(os, "getpgrp", lambda: 1, raising=False)
        for k, v in (("IS_WINDOWS", False), ("process_create_time", lambda _p: None),
                     ("_group_has_live_member", lambda g: g == 99999988), ("_pgid_members", lambda g: (True, [(mut_proc.pid, mut_ct or 200.0)]))):
            monkeypatch.setattr(postbox_os, k, v)
        res_unv = postbox_os.settle_helper_dir(str(hdir))
        assert mut_proc.poll() is None and not res_unv["settled"] and rec_unv.exists() and "helper_group_unverified:inbox:99999988" in res_unv["issues"]
    finally:
        if mut_proc.poll() is None: mut_proc.kill(); mut_proc.wait(timeout=5)  # noqa: E701


def test_s7_context_worker_bounded_ledger_state_and_pipe_error(tmp_path, monkeypatch):
    big_ledger, big_state, valid_state = tmp_path / "enrollment.json", tmp_path / "big_state.json", tmp_path / "valid_state.json"
    _write_sparse(big_ledger, context_worker.MAX_LEDGER_BYTES + 4096)
    with pytest.raises(context_worker.LedgerError, match="exceeds"): context_worker.read_ledger(str(big_ledger))  # noqa: E701
    _write_sparse(big_state, context_worker.MAX_STATE_BYTES + 4096)
    seat, peak = _traced(lambda: context_worker.resolve_seat({"name": "fallback-seat", "state": str(big_state)}))
    assert seat == "fallback-seat" and peak < 512 * 1024
    valid_state.write_text(json.dumps({"label": "server-label"}), encoding="utf-8")
    assert context_worker.resolve_seat({"name": "fallback-seat", "state": str(valid_state)}) == "server-label"
    monkeypatch.setattr(context_worker.postbox_os, "run_bounded", lambda *_a, **_k: (_ for _ in ()).throw(OSError("pipe reader failed on stdout")))
    rc, out_s, err_s = context_worker._run_subprocess([sys.executable, "-V"], str(tmp_path), 5.0)
    assert rc == -1 and out_s == "" and "pipe reader failed on stdout" in err_s


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
