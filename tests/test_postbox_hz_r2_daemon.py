"""HZ postbox daemon round 2 (#58): P1-1 child cleanup/HOLD, S3 quarantine, P1-2 streaming bounds."""
from __future__ import annotations

import base64, contextlib, datetime as dt, errno, hashlib, io, json, os, subprocess, sys, time, tracemalloc
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools" / "postbox")]
import delivery_bank, postbox_os  # noqa: E402
from tests.test_postbox_reconcile import pb  # noqa: E402,F401

BASE_PY = getattr(sys, "_base_executable", sys.executable)
T0 = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)
_alive = lambda pid: postbox_os.process_create_time(pid) is not None  # noqa: E731


def _peak_bytes(fn) -> int:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak(); fn(); return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class _GenRawIO(io.RawIOBase):
    def __init__(self, chunks): self._it, self._rem = iter(chunks), b""
    def readable(self): return True
    def readinto(self, b):
        while not self._rem:
            self._rem = next(self._it, b"")
            if not self._rem: return 0
        n = min(len(b), len(self._rem)); b[:n], self._rem = self._rem[:n], self._rem[n:]; return n


def _ident(tmp_path, **kw):
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    return {"name": "seat-prod", "log": str(tmp_path / "seat.log"), "python": BASE_PY,
            "state": str(tmp_path / "state.json"), "client": str(tmp_path), "base_url": "http://x", "track_fetches": True, **kw}


def test_p1_1_storage_failure_reaps_listener_and_recovers_uncommitted_mail(pb, tmp_path, monkeypatch):
    client, spawns_file = tmp_path / "client", tmp_path / "spawns.json"
    (client / "tools").mkdir(parents=True); spawns_file.write_text("[]", encoding="utf-8")
    (client / "tools" / "meshy_node.py").write_text(
        f"import json, os, sys, time\nsf = {str(spawns_file)!r}\n"
        "if 'listen' in sys.argv:\n"
        "    arr = json.loads(open(sf, encoding='utf-8').read()) + [os.getpid()]\n"
        "    open(sf, 'w', encoding='utf-8').write(json.dumps(arr)); n = len(arr)\n"
        "    sys.stdout.write(json.dumps({'event':'message','type':'mesh_message','key':f'msg/{n}-r2b',"
        "'version':1,'role':'to','from':'alice','created_at':f'2026-10-31T00:00:0{n}Z'}) + '\\n')\n"
        "    sys.stdout.flush(); time.sleep(30)\n"
        "else:\n    print(json.dumps({'label':'seat','labels':[{'label':'seat'}],'hits':[],'truncated':False,'drained':0,'refused':0,'uncertain':0}))\n",
        encoding="utf-8")
    idt, real_write, real_status, state = _ident(tmp_path, client=str(client)), pb._write_line, pb.set_status, {"fail": True, "reaped": False}
    def flaky_write(out, raw, log):
        if state["fail"] and b"msg/1-r2b" in raw:
            assert _alive(json.loads(spawns_file.read_text(encoding="utf-8"))[0])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(out, raw, log)
    def watch_status(name, **kw):
        res = real_status(name, **kw)
        if kw.get("last_message") == "msg/2-r2b": raise KeyboardInterrupt("done")
        return res
    def on_pause(_s):
        spawns = json.loads(spawns_file.read_text(encoding="utf-8"))
        assert len(spawns) == 1 and not _alive(spawns[0])
        state["reaped"], state["fail"] = True, False
    monkeypatch.setattr(pb, "_write_line", flaky_write); monkeypatch.setattr(pb, "set_status", watch_status)
    with pytest.raises(KeyboardInterrupt, match="done"):
        pb.guarded(pb.run_identity, idt, "http://x", pause_s=0, sleep=on_pause)
    spawns = json.loads(spawns_file.read_text(encoding="utf-8"))
    assert state["reaped"] and len(spawns) == 2 and spawns[0] != spawns[1] and not _alive(spawns[0]) and not _alive(spawns[1])
    rows = [json.loads(ln) for ln in Path(idt["log"]).read_text(encoding="utf-8").splitlines()]
    assert [d["key"] for d in rows if d.get("event") == "message"] == ["msg/1-r2b", "msg/2-r2b"]


def test_p1_1_uncertain_child_cleanup_latches_hold_and_liveness_fails(pb, tmp_path, monkeypatch):
    idt, popens, real_write = _ident(tmp_path), [], pb._write_line
    class _StuckProc:
        pid, _handle = 999991, 0
        stdout = io.BytesIO(b'{"event":"message","key":"msg/h1","version":1,"role":"to"}\n')
        poll = terminate = kill = staticmethod(lambda: None)
        wait = staticmethod(lambda timeout=None: (_ for _ in ()).throw(subprocess.TimeoutExpired("listen", timeout or 0)))
    for attr, val in (("LISTENER_STOP_GRACE_S", 0.01), ("LISTENER_KILL_GRACE_S", 0.01), ("tie_to_daemon", lambda _p: None),
                      ("reconcile", lambda *_a: 0), ("drain_send_operations", lambda *_a: None), ("listeners_on", lambda _s: [])):
        monkeypatch.setattr(pb, attr, val)
    monkeypatch.setattr(pb.postbox_os, "process_create_time", lambda _pid: 1000.0)
    monkeypatch.setattr(pb.subprocess, "Popen", lambda *_a, **_k: (popens.append(_StuckProc()) or popens[-1]))
    monkeypatch.setattr(pb, "_write_line", lambda out, raw, log: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full"))
                        if b"msg/h1" in raw else real_write(out, raw, log))
    monkeypatch.setattr(pb.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt("held"))
                        if s == 60 and pb._status.get("seat-prod", {}).get("listener") == "hold" else None)
    with pytest.raises(KeyboardInterrupt, match="held"):
        pb.guarded(pb.run_identity, idt, "http://x", pause_s=0, sleep=lambda _s: None)
    st = json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))
    assert len(popens) == 1 and st["seat-prod"]["listener"] == "hold" and "999991" in st["seat-prod"]["listener_hold"]
    rows = pb.liveness(st)
    assert rows == [("seat-prod", False, rows[0][2])] and "HOLD" in rows[0][2]
    assert pb._proc_match(999991, 1000.5) == "dead"  # F3: exact ctime when psutil or Windows CreationDate is used
    idt2, alive2 = _ident(tmp_path, name="ps-fail-seat", log=str(tmp_path / "psfail.log")), {"on": True}
    class _ScanFailProc:
        pid, _handle, stdout = 999992, 0, io.BytesIO(b"")
        poll = staticmethod(lambda: None if alive2["on"] else 0)
        terminate = kill = staticmethod(lambda: alive2.__setitem__("on", False))
        wait = staticmethod(lambda timeout=None: 0)
    monkeypatch.setattr(pb.postbox_os, "_psutil", lambda: None)
    monkeypatch.setattr(pb.postbox_os, "IS_WINDOWS", False)
    monkeypatch.setattr(pb.postbox_os, "process_create_time", lambda _pid: 2000.0 if alive2["on"] else None)
    monkeypatch.setattr(pb, "_popen_bounded", lambda *_a, **_k: type("R", (), {"returncode": 1, "stdout": "", "stdout_truncated": False})())
    assert pb._dispose_listener_child(idt2, _ScanFailProc(), 2001.0) is False and not alive2["on"]
    assert pb._check_listener_hold(idt2) is True  # F2: failed ps scan keeps HOLD latched across recheck after child exits
    st2 = {n: (ok, msg) for n, ok, msg in pb.liveness(json.loads(Path(pb.STATUS).read_text(encoding="utf-8")))}
    assert st2["ps-fail-seat"][0] is False and "HOLD" in st2["ps-fail-seat"][1]
    monkeypatch.undo()
    monkeypatch.setattr(pb.postbox_os, "_psutil", lambda: None)
    monkeypatch.setattr(pb, "_popen_bounded", lambda *_a, **_k: type("R", (), {"returncode": 0, "stdout": "", "stdout_truncated": True})())
    with pytest.raises(RuntimeError, match="truncated=True"):
        pb.listeners_on(idt2["state"])


def test_s3_partial_tail_and_damaged_fetch_sidecar_quarantined_before_mutation(pb, tmp_path, monkeypatch):
    idt = _ident(tmp_path)
    log_p, corrupt = Path(idt["log"]), b'{"event":"message","partial_tail":true'
    valid = (b'{"event":"message","key":"msg/a","version":1,"role":"to","created_at":"2026-10-31T12:01:00Z"}\n'
             b'{"event":"message","key":"msg/b","version":1,"role":"cc","created_at":"2026-10-31T12:02:00Z"}\n')
    log_p.write_bytes(valid + corrupt)
    order, real_q = [], pb._append_quarantine_record
    monkeypatch.setattr(pb, "_append_quarantine_record", lambda qp, rec, **k: (order.append((qp, log_p.stat().st_size)), real_q(qp, rec, **k))[1])
    with pb.log_lock(str(log_p)):
        assert pb._quarantine_partial_tail(str(log_p)) == len(valid)
    assert order == [(str(log_p) + ".quarantine.jsonl", len(valid) + len(corrupt))] and log_p.read_bytes() == valid
    qrec = json.loads(Path(str(log_p) + ".quarantine.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert (qrec["offset"], qrec["bytes_len"], qrec["sha256"], base64.b64decode(qrec["raw_b64"])) == (
        len(valid), len(corrupt), hashlib.sha256(corrupt).hexdigest(), corrupt)
    pb.fetch_gap(idt, clock=lambda: T0)
    sidecar, bad_line = Path(idt["state"] + ".fetched.jsonl"), b"{corrupt-fetched-sidecar-line\n"
    with open(sidecar, "ab") as sf:
        sf.write((json.dumps({"key": "msg/read-ok", "version": 1, "at": (T0 + dt.timedelta(days=9)).isoformat()}) + "\n").encode("utf-8") + bad_line)
    monkeypatch.setattr(pb, "FETCH_SIDECAR_MAX", 1)
    real_replace, seen_q = pb.os.replace, []
    monkeypatch.setattr(pb.os, "replace", lambda src, dst: (
        seen_q.append(Path(str(sidecar) + ".quarantine.jsonl").exists()) if dst == str(sidecar) else None, real_replace(src, dst))[1])
    assert pb.fetch_gap(idt, clock=lambda: T0 + dt.timedelta(days=10)) == (1, T0 + dt.timedelta(minutes=1)) and seen_q == [True]
    sq = json.loads(Path(str(sidecar) + ".quarantine.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert sq["reason"] == "malformed_json" and sq["sha256"] == hashlib.sha256(bad_line).hexdigest()
    assert json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))["seat-prod"]["fetch_damaged_lines"] == 1
    qp_mid = tmp_path / "midwrite.quarantine.jsonl"
    qp_mid.write_bytes(b'{"partial_enospc":1')
    pb._append_quarantine_record(str(qp_mid), {"reason": "after_enospc", "seq": 2})
    qlines = qp_mid.read_bytes().splitlines(keepends=True)
    assert qlines[0] == b'{"partial_enospc":1\n' and qlines[1].endswith(b"\n") and json.loads(qlines[1])["seq"] == 2


def _stream_oversize(prefix: bytes, pad_byte: bytes, pad_len: int, suffix: bytes):
    chunks = [prefix] + [pad_byte * 65536] * (pad_len // 65536) + ([pad_byte * (pad_len % 65536)] if pad_len % 65536 else []) + [suffix]
    return chunks, sum(len(c) for c in chunks), hashlib.sha256(b"".join(chunks)).hexdigest()


def test_p1_2_streaming_oversize_blanking_and_rollout_memory_bounds(pb, tmp_path, monkeypatch):
    idt, ten_mb = _ident(tmp_path), 10 * 1048576
    c1, n1, s1 = _stream_oversize(b'{"event":"message","type":"mesh_message","key":"msg/big-1","version":1,"role":"to","from":"alice","created_at":"2026-10-31T12:05:00Z","preview_unverified":"SECRET_OVERSIZE_PREVIEW_', b"A", ten_mb, b'"}\n')
    c2, n2, s2 = _stream_oversize(b'{"event":"edit","key":"msg/big-1","version":2,"role":"to","edited_by":"alice","edited_at":"2026-10-31T12:06:00Z","pad":"', b"B", ten_mb, b'"}\n')
    c3, n3, s3 = _stream_oversize(b'{"broken":', b"X", ten_mb, b"\n")
    many_to = (json.dumps({"event": "message", "type": "mesh_message", "key": "msg/65k-to", "version": 1, "role": "to",
                           "from": "alice", "created_at": "2026-10-31T12:07:00Z", "to": ["recipient-seat-%05d" % i for i in range(65000)]}) + "\n").encode("utf-8")
    c4, n4, s4 = [many_to[i:i + 65536] for i in range(0, len(many_to), 65536)], len(many_to), hashlib.sha256(many_to).hexdigest()
    reader = io.BufferedReader(_GenRawIO(c1 + c2 + c3 + c4), buffer_size=65536)
    def _run_ingest():
        with open(idt["log"], "ab", buffering=0) as out:
            for raw, ov in pb._iter_bounded_lines(reader, delivery_bank.MAX_LINE, stage_dir=idt["log"] + ".oversize.jsonl.raw"):
                pb.record_listener_line(idt, out, raw, oversize_info=ov)
    assert _peak_bytes(_run_ingest) < 3 * 1048576
    ov_txt = Path(idt["log"] + ".oversize.jsonl").read_text(encoding="utf-8")
    ov_rows = [json.loads(x) for x in ov_txt.splitlines()]
    assert "SECRET_OVERSIZE_PREVIEW" not in ov_txt and "head_b64" not in ov_rows[0]
    assert [(r["bytes"], r["sha256"], r["valid_json"]) for r in ov_rows] == [(n1, s1, True), (n2, s2, True), (n3, s3, False), (n4, s4, True)]
    _, events = delivery_bank.Bank(idt, idt["log"] + ".codex.offset", 0, "codex:t").collect(
        pb.wanted, lambda d: "%s@%s" % (d["key"], d.get("version")))
    assert [(e["key"], e.get("version")) for e in events] == [("msg/big-1", 1), ("msg/big-1", 2), ("msg/65k-to", 1)]
    b_idt = _ident(tmp_path, name="blank-seat", log=str(tmp_path / "blank.log"))
    with open(b_idt["log"], "wb") as bf:
        for idx in range(2000):
            key = "msg/redact-target" if idx in (0, 1998) else ("msg/other-%d" % idx)
            bf.write((json.dumps({"event": "message", "key": key, "role": "to", "preview_unverified": "secret-" + ("z" * 3000)}) + "\n").encode("utf-8"))
        bf.write(b'{"event":"redaction","key":"msg/redact-target","version":1}\n')
    orig_size = os.path.getsize(b_idt["log"])
    assert _peak_bytes(lambda: (_ for _ in ()).throw(AssertionError()) if pb.reblank_log(b_idt, incremental=True) != 2 else None) < 2 * 1048576
    assert os.path.getsize(b_idt["log"]) == orig_size and b"secret-" not in open(b_idt["log"], "rb").readline()
    opens, real_open = [], open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: (opens.append(str(f)), real_open(f, *a, **k))[1])
    assert pb.reblank_log(b_idt, incremental=True) == 0 and str(b_idt["log"]) not in opens; monkeypatch.undo()
    rollout, now_ep = tmp_path / "rollout.jsonl", time.time()
    ts_iso = dt.datetime.fromtimestamp(now_ep, tz=dt.timezone.utc).isoformat()
    with open(rollout, "wb") as rf:
        rf.write((json.dumps({"timestamp": ts_iso, "type": "event_msg", "payload": {"type": "task_started"}}) + "\n").encode("utf-8"))
        rf.writelines(b"R" * 65536 for _ in range(160))
    ct, st_holder = pb.CodexTurn(str(rollout)), []
    assert _peak_bytes(lambda: st_holder.append(ct.update(clock=lambda: now_ep + 1))) < 2.5 * 1048576
    read_after_first = ct.bytes_read
    assert st_holder == [(None, None)] and ct.oversize_skips == 1 and ct.update(clock=lambda: now_ep + 2) == (None, None) and ct.bytes_read == read_after_first
    with open(rollout, "ab") as rf:
        rf.write(b"\n" + (json.dumps({"timestamp": ts_iso, "type": "event_msg", "payload": {"type": "task_complete"}}) + "\n").encode("utf-8"))
    assert ct.update(clock=lambda: now_ep + 3)[0] is False
    bad_idt = _ident(tmp_path, name="unhash-seat", log=str(tmp_path / "unhash.log"))
    with open(bad_idt["log"], "ab", buffering=0) as uf:
        assert pb.record_listener_line(bad_idt, uf, b'{"event":"message","key":["bad"],"version":1,"preview_unverified":"p"}\n') is not None
        assert pb.record_listener_line(bad_idt, uf, b'{"event":["bad"],"key":"msg/x"}\n') is not None
    assert pb.seen_keys(bad_idt) == set() and pb.blank_preview(bad_idt["log"], "msg/x") == 0
    monkeypatch.setattr(pb.postbox_os, "kill_tree", lambda _pid: False)
    seen_procs, real_popen = [], pb.subprocess.Popen
    monkeypatch.setattr(pb.subprocess, "Popen", lambda *a, **k: (seen_procs.append(real_popen(*a, **k)) or seen_procs[-1]))
    ready = tmp_path / "popen_ready"
    r_fd, w_fd = os.pipe()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as tex:
            pb._popen_bounded(
                [BASE_PY, "-S", "-c",
                 f"import os, sys; sys.stdout.buffer.write(b'pre'); sys.stdout.buffer.flush(); "
                 f"t = {str(ready)!r} + '.tmp'; "
                 f"f = open(t, 'wb'); f.write(b'1'); f.close(); os.replace(t, {str(ready)!r}); "
                 f"sys.stdin.buffer.read(1)"],
                stdin=r_fd, max_stdout=1024, timeout=5.0,
            )
    finally:
        for fd in (r_fd, w_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
    assert ready.is_file() and ready.read_bytes() == b"1"
    assert tex.value.stdout == "pre" and len(seen_procs) == 1 and seen_procs[0].poll() is not None
