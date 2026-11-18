"""Issue #58 HZ Postbox Intake Round 2 (PR A): S4 channel resilience/1ae316d2 compat, S6 latency compat/streaming, P1-2 bounded readers/pipes."""
from __future__ import annotations

import contextlib, hashlib, importlib.util, json, os, subprocess, sys, time, tracemalloc, types
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
if str(PB) not in sys.path:
    sys.path.insert(0, str(PB))
import delivery_bank, graphline, latency, owedline, postbox_os  # noqa: E402

ROUTING_1AE316D2 = ("event", "state", "type", "key", "version", "role", "from", "from_peer_id",
                    "created_at", "edited_by", "edited_at", "recovered", "source", "rc", "terminal")
BLOBS_1AE316D2 = {
    "postbox_os.py": "ccbe6f459acfea2d2690bd533c561177609634b41d6835fa9fa80a7718223bce",
    "delivery_bank.py": "f4a87484931725e367ecdda1d047ad73fd0d877ad5a65034cbb68ac35772f045",
    "meshy_channel.py": "0d5f84451213405442069e2278d1a975e49f1200bb3d1a1f3e09a3590681dab1",
}


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, PB / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ident(tmp_path: Path, name: str = "seat-prod") -> dict:
    client = tmp_path / "client"
    (client / "tools" / "meshy_node").mkdir(parents=True, exist_ok=True)
    state = tmp_path / (name + "-state.json")
    state.write_text(json.dumps({"peer_id": "peer-hz", "label": name}), encoding="utf-8")
    return {"name": name, "address": "seat-addr", "log": str(tmp_path / (name + ".log")),
            "state": str(state), "client": str(client), "python": sys.executable, "base_url": "http://127.0.0.1:6310"}


def _traced_call(fn):
    tracemalloc.start()
    cur0, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        return fn(), tracemalloc.get_traced_memory()[1] - cur0
    finally:
        tracemalloc.stop()


class _StopTail(Exception):
    pass


def test_s4_channel_startup_retries_corrupt_checkpoint_once_and_recovers(tmp_path, monkeypatch):
    channel = _load("meshy_channel_r2_s4", "meshy_channel.py")
    for attr, val in (("HERE", str(tmp_path)), ("graphline", None), ("owedline", None)):
        monkeypatch.setattr(channel, attr, val)
    ident = _ident(tmp_path)
    Path(ident["log"]).write_text(json.dumps({"event": "message", "role": "to", "key": "msg/after-repair", "version": 1,
                                              "from": "peer", "created_at": "2026-10-31T09:00:00Z", "rx_at": "2026-10-31T09:00:01.000Z"}) + "\n", encoding="utf-8")
    off_file, bank_file = ident["log"] + ".channel.offset", ident["log"] + ".channel.offset.bank.json"
    Path(off_file).write_text("0", encoding="utf-8")
    Path(bank_file).write_text("{corrupt-json", encoding="utf-8")
    if os.name == "posix":
        os.chmod(bank_file, 0o600)
    pushed, sleeps, lock_fds = [], [], []
    real_try_lock, real_deliver, armed = channel.postbox_os.try_lock, channel._deliver_once, {"fail": True}

    def on_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 1:
            assert channel.LAST_ERRORS[ident["name"]]["phase"] == "startup"
        elif len(sleeps) == 2:
            os.remove(bank_file)
        elif sec == 1:
            raise _StopTail()

    monkeypatch.setattr(channel, "send", lambda obj: pushed.append((obj["params"]["content"], obj["params"]["meta"])))
    monkeypatch.setattr(channel.postbox_os, "try_lock", lambda fd: (lock_fds.append(fd), real_try_lock(fd))[1])
    monkeypatch.setattr(channel, "_deliver_once",
                        lambda i, b, opts=None: (_ for _ in ()).throw(OSError(28, "disk full")) if armed.pop("fail", False) else real_deliver(i, b, opts))
    monkeypatch.setattr(channel.time, "sleep", on_sleep)
    try:
        with pytest.raises(_StopTail):
            channel.tail(ident)
    finally:
        for fd in set(lock_fds):
            with contextlib.suppress(OSError):
                os.close(fd)
    assert len(lock_fds) == 1 and sleeps == [channel.RETRY_S, channel.RETRY_S, channel.RETRY_S, 1]
    assert [m.get("kind") for _, m in pushed] == ["channel_error", "channel_open", "channel_error", "message"]
    assert "startup failed" in pushed[0][0] and "delivering failed" in pushed[2][0] and pushed[3][1]["key"] == "msg/after-repair"
    err_rows = [json.loads(x) for x in Path(ident["log"] + ".channel.err").read_text(encoding="utf-8").splitlines()]
    assert [r["phase"] for r in err_rows] == ["startup", "delivering"] and channel.LAST_ERRORS[ident["name"]]["phase"] == "delivering"

    fresh_id = _ident(tmp_path, name="fresh-seat")
    Path(fresh_id["log"]).write_text(json.dumps({"event": "message", "role": "to", "key": "msg/old-before-open", "version": 1,
                                                 "from": "peer", "created_at": "2026-10-31T08:59:00Z"}) + "\n", encoding="utf-8")
    f_pushed, f_fds, orig_commit, f_state = [], [], channel.delivery_bank.Bank.commit, {"fail_commit": True}

    def flaky_commit(self, end, events):
        if f_state.pop("fail_commit", False):
            raise OSError(28, "disk full on initial commit")
        return orig_commit(self, end, events)

    def f_sleep(sec):
        if sec == channel.RETRY_S:
            with open(fresh_id["log"], "a", encoding="utf-8") as lf:
                lf.write(json.dumps({"event": "message", "role": "to", "key": "msg/arrived-during-retry", "version": 1,
                                     "from": "peer", "created_at": "2026-10-31T09:00:05Z"}) + "\n")
        elif sec == 1:
            raise _StopTail()

    monkeypatch.setattr(channel.delivery_bank.Bank, "commit", flaky_commit)
    monkeypatch.setattr(channel, "send", lambda obj: f_pushed.append(obj["params"]["meta"]))
    monkeypatch.setattr(channel.postbox_os, "try_lock", lambda fd: (f_fds.append(fd), real_try_lock(fd))[1])
    monkeypatch.setattr(channel, "_deliver_once", real_deliver)
    monkeypatch.setattr(channel.time, "sleep", f_sleep)
    try:
        with pytest.raises(_StopTail):
            channel.tail(fresh_id)
    finally:
        for fd in set(f_fds):
            with contextlib.suppress(OSError):
                os.close(fd)
    assert [m.get("kind") for m in f_pushed] == ["channel_error", "channel_open", "message"]
    assert f_pushed[2]["key"] == "msg/arrived-during-retry"


def _write_s4_mixed_spool(tmp_path: Path, monkeypatch):
    pb, channel = _load("postbox_r2_s4_writer", "postbox.py"), _load("meshy_channel_r2_1ae316d2", "meshy_channel.py")
    monkeypatch.setattr(pb, "STATUS", str(tmp_path / "status.json"))
    monkeypatch.setattr(pb, "ERR", str(tmp_path / "postbox.err.log"))
    for mod in (pb, channel):
        monkeypatch.setattr(mod, "graphline", None)
    monkeypatch.setattr(channel, "owedline", None)
    ident = dict(_ident(tmp_path), hold_cc_without_ack=True)
    off_file = ident["log"] + ".channel.offset"
    with open(ident["log"], "ab", buffering=0) as out:
        pb.record_listener_line(ident, out, (json.dumps({
            "event": "message", "type": "mesh_message", "key": "msg/cc-held", "version": 1, "role": "cc",
            "requires_ack": False, "from": "peer-cc", "created_at": "2026-10-31T08:00:00Z", "preview_unverified": "never leak cc"}) + "\n").encode("utf-8"))
    b1 = delivery_bank.Bank(ident, off_file, 0, "claude-channel")
    end1, ev1 = b1.collect(postbox_os.wanted, hold=postbox_os.held_by_default)
    b1.commit(end1, ev1)
    assert ev1 == [] and delivery_bank.private_json(off_file + ".bank.json")["schema_version"] == 2

    big = "X" * (delivery_bank.MAX_LINE + 4096)
    lines = [
        {"event": "message", "type": "mesh_message", "key": "msg/to-1", "version": 1, "role": "to", "from": "alice", "from_peer_id": "p11111111", "created_at": "2026-10-31T08:01:00Z", "preview_unverified": "secret body 1"},
        {"event": "message", "type": "mesh_message", "key": "msg/redacted", "version": 1, "role": "to", "from": "bob", "created_at": "2026-10-31T08:02:00Z", "preview_unverified": "REDACTED_SECRET"},
        {"event": "redaction", "key": "msg/redacted", "at": "2026-10-31T08:02:05Z"},
        {"event": "edit", "key": "msg/to-1", "version": 2, "role": "to", "edited_by": "alice", "edited_at": "2026-10-31T08:03:00Z"},
        {"event": "message", "type": "mesh_message", "key": "msg/over-msg", "version": 1, "role": "to", "from": "carol", "created_at": "2026-10-31T08:04:00Z", "preview_unverified": big},
        {"event": "edit", "key": "msg/over-edit", "version": 2, "role": "to", "edited_by": "carol", "edited_at": "2026-10-31T08:05:00Z", "preview_unverified": big},
        {"event": "status", "state": "listening", "detail": big},
        {"event": "status", "state": "error", "source": "ws"},
        {"event": "supervisor", "state": "listener_exited", "rc": 1},
    ]
    with open(ident["log"], "ab", buffering=0) as out:
        for item in lines:
            pb.record_listener_line(ident, out, (json.dumps(item) + "\n").encode("utf-8"))
    inbox = json.dumps({"label": "seat-addr", "labels": [{"label": "seat-addr"}], "hits": [
        {"key": "msg/reconciled", "version": 1, "author": "dave", "recorded_at": "2026-10-31T08:06:00Z", "src_field": "to"}]})
    monkeypatch.setattr(pb.subprocess, "run", lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, stdout=inbox, stderr=""))
    assert pb.reconcile(ident, ident["base_url"]) == 1
    return ident, channel, off_file


def _stage_1ae316d2_reader(dst_dir: Path, root_dir: Path = ROOT, env_dir: str | None = None) -> Path:
    src_override = os.environ.get("MESHY_1AE316D2_POSTBOX_DIR") if env_dir is None else env_dir
    dst_dir.mkdir(parents=True, exist_ok=True)
    if src_override:
        src_path = Path(src_override)
        for fname, sha in BLOBS_1AE316D2.items():
            fp = src_path / fname
            assert fp.is_file(), f"missing 1ae316d2 file {fp}"
            raw = fp.read_bytes()
            assert hashlib.sha256(raw).hexdigest() == sha, f"sha256 mismatch for {fp}"
            (dst_dir / fname).write_bytes(raw)
        return dst_dir
    if subprocess.run(["git", "-C", str(root_dir), "cat-file", "-e", "1ae316d2:tools/postbox/meshy_channel.py"],
                      capture_output=True).returncode != 0:
        pytest.skip("commit 1ae316d2 not in git object store and MESHY_1AE316D2_POSTBOX_DIR unset")
    for fname, sha in BLOBS_1AE316D2.items():
        raw = subprocess.check_output(["git", "-C", str(root_dir), "show", f"1ae316d2:tools/postbox/{fname}"])
        assert hashlib.sha256(raw).hexdigest() == sha
        (dst_dir / fname).write_bytes(raw)
    return dst_dir


def test_s4_1ae316d2_live_channel_compatibility_across_all_spool_shapes(tmp_path, monkeypatch):
    ident, channel, off_file = _write_s4_mixed_spool(tmp_path, monkeypatch)
    raw_spool = Path(ident["log"]).read_text(encoding="utf-8")
    assert "REDACTED_SECRET" not in raw_spool and '"oversize_line"' in raw_spool and '"oversize_bytes"' in raw_spool

    b2 = delivery_bank.Bank(ident, off_file, 0, "claude-channel")
    end2, ev2 = b2.collect(postbox_os.wanted, hold=postbox_os.held_by_default)
    assert all(set(e).issubset(ROUTING_1AE316D2) for e in ev2)
    assert [(e.get("event"), e.get("key") or e.get("state"), e.get("version")) for e in ev2] == [
        ("message", "msg/cc-held", 1), ("message", "msg/to-1", 1), ("message", "msg/redacted", 1),
        ("edit", "msg/to-1", 2), ("message", "msg/over-msg", 1), ("edit", "msg/over-edit", 2),
        ("status", "error", None), ("supervisor", "listener_exited", None), ("message", "msg/reconciled", 1)]
    for e in ev2:
        if e["event"] == "message":
            txt, meta = channel.message_event(ident, e)
            assert meta["kind"] == "message" and "secret body" not in txt and "never leak" not in txt
        elif e["event"] == "edit":
            txt, meta = channel.edit_event(ident, e)
            assert meta["kind"] == "edit" and "[RECALL]" in txt
    b2.commit(end2, ev2)
    assert delivery_bank.private_json(off_file + ".bank.json")["schema_version"] == 1
    monkeypatch.undo()
    with pytest.raises(pytest.skip.Exception, match="1ae316d2 not in git object store"):
        _stage_1ae316d2_reader(tmp_path / "shallow_skip", root_dir=tmp_path, env_dir="")
    bad_src = tmp_path / "bad_1ae316d2"
    bad_src.mkdir()
    for fname in BLOBS_1AE316D2:
        (bad_src / fname).write_bytes(b"# wrong bytes\n")
    with pytest.raises(AssertionError, match="sha256 mismatch"):
        _stage_1ae316d2_reader(tmp_path / "bad_dst", root_dir=tmp_path, env_dir=str(bad_src))


def test_s4_1ae316d2_old_reader_accepts_new_writer_spool_lines(tmp_path, monkeypatch):
    ident, _channel, _off_file = _write_s4_mixed_spool(tmp_path, monkeypatch)
    monkeypatch.undo()
    old_dir = _stage_1ae316d2_reader(tmp_path / "hz_1ae316d2")
    script = tmp_path / "run_1ae316d2.py"
    script.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(old_dir)!r})\n"
        "import delivery_bank as ob, meshy_channel as oc, postbox_os as oo\n"
        "oc.graphline = oc.owedline = None\n"
        "pushed = []\n"
        "oc.send = lambda obj: pushed.append(obj['params'])\n"
        f"ident = json.loads({json.dumps(ident)!r})\n"
        "off = ident['log'] + '.old1ae.offset'\n"
        "bank = ob.Bank(ident, off, 0, 'claude-channel')\n"
        "oc._deliver_once(ident, bank, oo.hold_options(ident))\n"
        "print(json.dumps({'count': len(pushed), 'meta': pushed[0]['meta'], 'offset': bank.offset}))\n",
        encoding="utf-8",
    )
    res = json.loads(subprocess.check_output([sys.executable, str(script)], text=True))
    assert res["count"] == 1 and res["meta"]["kind"] == "batch" and int(res["meta"]["count"]) == 9
    assert res["offset"] == os.path.getsize(ident["log"])


def test_s6_latency_86794969_compat_missing_reader_and_bounded_streaming(tmp_path, monkeypatch):
    ident = _ident(tmp_path)
    with open(ident["log"], "wb") as f:
        for k, s, rx in (("msg/m1", "00:00", "00:02"), ("msg/m2", "01:00", "01:03"), ("msg/m3", "02:00", "02:04")):
            if k == "msg/m2":
                f.write(b'{"event":"status","pad":"' + (b"Z" * 65536) * 128 + b'"}\n')
            f.write((json.dumps({"event": "message", "key": k, "version": 1, "role": "to",
                                 "created_at": f"2026-10-31T08:{s}Z", "rx_at": f"2026-10-31T08:{rx}.000Z"}) + "\n").encode("utf-8"))
    Path(ident["log"] + ".codex.offset.dispatch.jsonl").write_text(json.dumps({"at": "2026-10-31T08:00:05.000Z", "events": [
        {"event": "message", "key": "msg/m1", "version": 1, "role": "to"},
        {"event": "message", "key": "msg/m2", "version": 1, "role": "to"},
        {"event": "message", "key": ["unhashable"], "version": 1, "role": "to"}]}) + "\n", encoding="utf-8")
    Path(ident["state"] + ".read.json").write_text(json.dumps({
        "schema_version": 1, "peer_id": "peer-hz", "reads": [{"key": "msg/m1", "version": 1, "at": "2026-10-31T08:00:15.000Z"}]}), encoding="utf-8")
    compat_node = types.SimpleNamespace(READ_LEDGER_SUFFIX=".read.json", READ_LEDGER_MAX=2000, _read_ledger_records=lambda p, pid: [
        r for d in [json.loads(Path(p).read_text(encoding="utf-8"))] if d["peer_id"] == pid for r in d["reads"]])
    monkeypatch.setattr(latency, "import_node", lambda _i: compat_node)
    rep, delta = _traced_call(lambda: latency.report(ident))
    assert delta < 2 * 1048576 and rep["sources"]["reads"] == "ok (compat _read_ledger_records)" and rep["sources"]["spool"] == "ok"
    assert rep["stages"]["handed_to_read"] == {"count": 1, "p50_s": 10.0, "p95_s": 10.0} and [x["key"] for x in rep["delivered_never_read"]] == ["msg/m2"]
    assert latency.report(ident, max_messages=2)["sources"]["spool"] == "ok (capped at 2 messages)"

    monkeypatch.setattr(latency, "import_node", lambda _i: types.SimpleNamespace(READ_LEDGER_SUFFIX=".read.json"))
    rep_none = latency.report(ident)
    assert rep_none["sources"]["reads"] == "unsupported: client has no ledger_records" and rep_none["delivered_never_read"] is None
    monkeypatch.setattr(latency, "import_node", lambda _i: types.SimpleNamespace())
    assert latency.report(ident)["sources"]["reads"] == "unsupported: client has no READ_LEDGER_SUFFIX"
    monkeypatch.setattr(latency, "import_node", lambda _i: compat_node)
    with open(ident["state"] + ".read.json", "wb") as f:
        f.seek(20 * 1048576)
        f.write(b"x")
    rep_big = latency.report(ident)
    assert rep_big["sources"]["reads"] == "unavailable: ledger exceeds %d-byte limit" % delivery_bank.MAX_LEDGER and rep_big["delivered_never_read"] is None


def test_p1_2_bounded_read_ledger_tracemalloc_and_report_once(tmp_path):
    ident = _ident(tmp_path, name="seat-p12")
    assert delivery_bank.read_ledger(ident) == {}
    ledger_path, reports = ident["state"] + ".read.json", []
    with open(ledger_path, "wb") as f:
        f.seek(20 * 1048576 - 1)
        f.write(b"\n")
    (got1, got2), delta = _traced_call(lambda: (delivery_bank.read_ledger(ident, report=reports.append), delivery_bank.read_ledger(ident, report=reports.append)))
    assert got1 == {} and got2 == {} and delta < 512 * 1024
    assert len(reports) == 1 and ledger_path in reports[0] and "20971520" in reports[0] and "inactive" in reports[0]
    Path(ident["log"]).write_text(json.dumps({"event": "message", "role": "to", "key": "msg/wake-anyway", "version": 1, "rx_at": "2026-10-31T09:10:00.000Z"}) + "\n", encoding="utf-8")
    b = delivery_bank.Bank(ident, ident["log"] + ".codex.offset", 0, "codex:t")
    _, events = b.collect(postbox_os.wanted, fetched=lambda: delivery_bank.read_ledger(ident, report=reports.append))
    assert [e["key"] for e in events] == ["msg/wake-anyway"] and len(reports) == 1
    for bad_ver in ([], {}, True):
        Path(ledger_path).write_text(json.dumps({"schema_version": bad_ver, "peer_id": "peer-hz"}), encoding="utf-8")
        assert delivery_bank.read_ledger(ident, report=reports.append) == {}
        assert delivery_bank.read_ledger(ident, report=reports.append) == {}
        _, ev_bad = delivery_bank.Bank(ident, ident["log"] + ".codex2.offset", 0, "codex:t2").collect(
            postbox_os.wanted, fetched=lambda: delivery_bank.read_ledger(ident, report=reports.append))
        assert [e["key"] for e in ev_bad] == ["msg/wake-anyway"]
    assert len(reports) == 4


def _read_pid(path: Path) -> int | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    return int(raw) if raw.isdigit() else None


def _pid_stopped(pid: int, *, proc_root: Path = Path("/proc")) -> bool:
    if postbox_os.process_create_time(pid) is None:
        return True
    if postbox_os.IS_WINDOWS:
        return False
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        return raw.rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        if (proc_root / "self" / "stat").is_file():
            return True
    except (OSError, IndexError):
        pass
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    st = r.stdout.strip()
    return r.returncode != 0 or not st or st[:1] == "Z"


def _wait_gone(pid: int, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not _pid_stopped(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return _pid_stopped(pid)


def test_p1_2_run_bounded_flood_timeout_and_line_backoff(tmp_path, monkeypatch):
    script, hang, hang_ready = tmp_path / "flood.py", tmp_path / "hang.py", tmp_path / "hang.ready"
    script.write_text("import sys\nc, e = b'A'*65536, b'E'*65536\nfor _ in range(128): sys.stdout.buffer.write(c)\n"
                      "for _ in range(32): sys.stderr.buffer.write(e)\nsys.stdout.buffer.flush(); sys.stderr.buffer.flush()\n", encoding="utf-8")
    r, delta = _traced_call(lambda: postbox_os.run_bounded([sys.executable, "-S", str(script)], max_stdout=65536, max_stderr=16384, text=True, timeout=30))
    assert r.returncode == 0 and len(r.stdout) == 65537 and len(r.stderr) == 16385 and delta < 2 * 1048576
    hang.write_text(
        f"import os, sys\nsys.stdout.buffer.write(b'hi'); sys.stdout.buffer.flush()\n"
        f"t = {str(hang_ready)!r} + '.tmp'\n"
        f"with open(t, 'wb') as f: f.write(b'1')\n"
        f"os.replace(t, {str(hang_ready)!r})\nsys.stdin.buffer.read(1)\n",
        encoding="utf-8",
    )
    r_fd, w_fd = os.pipe()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            postbox_os.run_bounded([sys.executable, "-S", str(hang)], stdin=r_fd, max_stdout=4096, max_stderr=4096, text=True, timeout=5.0)
    finally:
        for fd in (r_fd, w_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
    assert hang_ready.is_file() and hang_ready.read_bytes() == b"1" and exc_info.value.stdout == "hi"
    inherit_hang, inh_gpid = tmp_path / "inherit_hang.py", tmp_path / "inherit.gpid"
    inherit_hang.write_text(
        f"import os, subprocess, sys\n"
        f"g = subprocess.Popen([sys.executable, '-S', '-c', 'import sys; sys.stdin.buffer.read(1)'])\n"
        f"t = {str(inh_gpid)!r} + '.tmp'\n"
        f"with open(t, 'w', encoding='utf-8') as f: f.write(str(g.pid))\n"
        f"os.replace(t, {str(inh_gpid)!r})\n"
        f"sys.stdout.buffer.write(b'ok'); sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    r_fd2, w_fd2 = os.pipe()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as inh_exc:
            postbox_os.run_bounded([sys.executable, "-S", str(inherit_hang)], stdin=r_fd2, max_stdout=4096, max_stderr=4096, text=True, timeout=5.0)
        gpid_inh = _read_pid(inh_gpid)
        assert gpid_inh is not None and inh_exc.value.stdout == "ok"
        # POSIX run_bounded starts a new session and kills the whole process group before returning;
        # Windows has no per-call job object once the intermediate parent exits, so finally reaps it.
        if not postbox_os.IS_WINDOWS:
            assert _wait_gone(gpid_inh)
    finally:
        for fd in (r_fd2, w_fd2):
            with contextlib.suppress(OSError):
                os.close(fd)
        gpid_cleanup = _read_pid(inh_gpid)
        if gpid_cleanup is not None and not _pid_stopped(gpid_cleanup):
            postbox_os.kill_tree(gpid_cleanup)
    fake_proc = tmp_path / "fake_proc"
    (fake_proc / "self").mkdir(parents=True)
    (fake_proc / "self" / "stat").write_text("1 (init) S 0\n", encoding="utf-8")
    (fake_proc / "42").mkdir()
    with monkeypatch.context() as mp:
        mp.setattr(postbox_os, "IS_WINDOWS", False)
        mp.setattr(postbox_os, "process_create_time", lambda _pid: 100.0)
        (fake_proc / "42" / "stat").write_text("42 (py ) worker) Z 1 42 42\n", encoding="utf-8")
        assert _pid_stopped(42, proc_root=fake_proc) is True
        (fake_proc / "42" / "stat").write_text("42 (py ) worker) S 1 42 42\n", encoding="utf-8")
        assert _pid_stopped(42, proc_root=fake_proc) is False
        assert _pid_stopped(99, proc_root=fake_proc) is True
        no_proc = tmp_path / "no_proc"
        mp.setattr(subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess(["ps"], 0, "Z+\n", ""))
        assert _pid_stopped(42, proc_root=no_proc) is True
        mp.setattr(subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess(["ps"], 0, "Ss\n", ""))
        assert _pid_stopped(42, proc_root=no_proc) is False

    ident = _ident(tmp_path, name="flood-seat")
    pkg = Path(ident["client"]) / "tools" / "meshy_node"
    (Path(ident["client"]) / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text("import sys\nsys.stdout.buffer.write(b'{' + b' '*1200000 + b'}')\nsys.stdout.buffer.flush()\n", encoding="utf-8")
    graphline._backoff_until.clear()
    owedline._backoff_until.clear()
    assert graphline.for_event(ident, {"event": "message", "key": "msg/flood"}) is None and graphline._cache_path(ident) in graphline._backoff_until
    assert owedline.wake_line(ident, "claude-channel", every=True) == (None, None) and owedline._cache_path(ident) in owedline._backoff_until


def test_s7_context_worker_disabled_inert_and_loader_bounded(tmp_path):
    pb, cw = _load("postbox_r2_s7", "postbox.py"), _load("context_worker_r2_s7", "context_worker.py")
    store_root = tmp_path / "hz_unused_context_store"
    hz_idents = [
        {"name": "oxxxxxx-prod", "enabled": True, "cli": "claude"},
        {"name": "codex-oxxxxxx-admin-cli", "enabled": True, "cli": "codex", "context": {"enabled": False}},
        {"name": "str-seat", "enabled": True, "cli": "claude", "context": {"enabled": "true"}},
        {"name": "off-seat", "enabled": False, "cli": "claude", "context": {"enabled": True}},
    ]
    assert cw.context_enabled_identities(hz_idents) == []
    assert pb.build_context_worker_thread({"identities": hz_idents, "context_store_root": str(store_root)}, hz_idents) is None
    statuses, notes = [], []
    cw.guarded_context_worker(hz_idents, store_root=str(store_root), set_status=lambda *a, **k: statuses.append((a, k)), note=notes.append)
    assert not store_root.exists() and statuses == [] and notes == []

    flood = tmp_path / "cw_flood.py"
    flood.write_text(
        "import sys\n"
        "for _ in range(512): sys.stdout.buffer.write(b'O' * 65536)\n"
        "for _ in range(16): sys.stderr.buffer.write(b'E' * 65536)\n"
        "sys.stdout.buffer.flush(); sys.stderr.buffer.flush()\n",
        encoding="utf-8",
    )
    (rc, out_s, err_s), delta = _traced_call(lambda: cw._run_subprocess([sys.executable, "-S", str(flood)], str(tmp_path), 20.0))
    assert rc == 0 and len(out_s) == 4 * 1048576 + 1 and len(err_s) == 262144 + 1 and delta < 16 * 1048576
    hang, cw_gpid = tmp_path / "cw_hang.py", tmp_path / "cw.gpid"
    hang.write_text(
        f"import os, subprocess, sys, time\n"
        f"g = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(60.0)'])\n"
        f"t = {str(cw_gpid)!r} + '.tmp'\n"
        f"with open(t, 'w', encoding='utf-8') as f: f.write(str(g.pid))\n"
        f"os.replace(t, {str(cw_gpid)!r})\n"
        f"sys.stdout.buffer.write(b'ok'); sys.stdout.buffer.flush()\ntime.sleep(60.0)\n",
        encoding="utf-8",
    )
    try:
        rc_t, out_t, _err_t = cw._run_subprocess([sys.executable, "-S", str(hang)], str(tmp_path), 5.0)
        gpid_cw = _read_pid(cw_gpid)
        assert gpid_cw is not None and rc_t is None and out_t == "ok"
        assert _wait_gone(gpid_cw)
    finally:
        gpid_cw_cleanup = _read_pid(cw_gpid)
        if gpid_cw_cleanup is not None and not _pid_stopped(gpid_cw_cleanup):
            postbox_os.kill_tree(gpid_cw_cleanup)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
