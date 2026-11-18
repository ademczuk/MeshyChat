"""HZ Round 3 owner tests for postbox.py I/O, bounded spool/reblank, streaming quarantine, and kimi tail caps."""
from __future__ import annotations

import hashlib, importlib.util, io, json, os, subprocess, sys, tracemalloc
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
if str(PB) not in sys.path:
    sys.path.insert(0, str(PB))
import delivery_bank, postbox_os  # noqa: E402


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


def _ident(tmp_path: Path, name: str = "seat-r3io") -> dict:
    client = tmp_path / "client"
    (client / "tools" / "meshy_node").mkdir(parents=True, exist_ok=True)
    state, log = tmp_path / f"{name}.state.json", tmp_path / f"{name}.log"
    state.write_text(json.dumps({"peer_id": "peer-r3io", "label": name}), encoding="utf-8")
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


def test_p1_a1_seen_keys_and_reblank_log_bounded_passes(tmp_path, monkeypatch):
    pb = _load_pb("pb_r3io_p1a1", tmp_path, monkeypatch)
    ident = _ident(tmp_path)
    monkeypatch.setattr(pb, "SEEN_KEYS_MAX", 8)
    with open(ident["log"], "wb") as f:
        for idx in range(25):
            f.write((json.dumps({"event": "message", "key": f"msg/k-{idx:03d}", "version": 1,
                                 "role": "to", "preview_unverified": f"secret-{idx:03d}"}) + "\n").encode("utf-8"))
        for idx in range(10):
            f.write((json.dumps({"event": "redaction", "key": f"msg/k-{idx:03d}"}) + "\n").encode("utf-8"))
    s = pb.seen_keys(ident)
    assert isinstance(s, set) and len(s) == 8
    assert ("msg/k-000", 1) not in s and pb.key_seen(ident, "msg/k-000", 1) is True
    assert pb.claim_key(ident, "msg/k-000", 1) is False and pb.claim_key(ident, "msg/k-999", 1) is True

    monkeypatch.setattr(pb, "REBLANK_BATCH_KEYS", 3)
    monkeypatch.setattr(pb, "REBLANK_MAX_PASSES", 2)
    pb.reblank_log(ident)
    assert json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident["name"]]["reblank_incomplete"] is True
    saved_off = pb._reblank_state[os.path.normcase(os.path.abspath(ident["log"]))]["offset"]
    assert saved_off > 0
    monkeypatch.setattr(pb, "REBLANK_MAX_PASSES", 10)
    pb.reblank_log(ident)
    raw_after_rb = Path(ident["log"]).read_text(encoding="utf-8")
    assert all(f"secret-{i:03d}" not in raw_after_rb for i in range(10)) and "secret-020" in raw_after_rb
    assert json.loads(Path(pb.STATUS).read_text(encoding="utf-8"))[ident["name"]]["reblank_incomplete"] is False


def test_p1_a2_a3_codexturn_note_and_status_sidecar_qualification(tmp_path, monkeypatch):
    pb = _load_pb("pb_r3io_p1a23", tmp_path, monkeypatch)
    rollout = tmp_path / "rollout-r3.jsonl"
    rollout.write_bytes((json.dumps({"timestamp": "2026-11-01T08:00:00Z", "type": "event_msg",
                                     "payload": {"type": "task_started", "turn_id": "t1"}}) + "\n"
                         + json.dumps({"timestamp": "2026-11-01T08:00:05Z", "type": "event_msg",
                                       "payload": {"type": "task_complete", "turn_id": "t1"}}) + "\n").encode("utf-8"))
    assert pb.CodexTurn(str(rollout)).update(clock=lambda: 2000000000.0)[0] is False
    dir_rollout = tmp_path / "dir_rollout"
    dir_rollout.mkdir()
    assert pb.CodexTurn(str(dir_rollout)).update(clock=lambda: 100.0) == (None, None)

    dir_err = tmp_path / "dir_err"
    dir_err.mkdir()
    monkeypatch.setattr(pb, "ERR", str(dir_err))
    pb.note("must not crash on non-regular ERR")

    big_status = tmp_path / "big_status.json"
    with open(big_status, "wb") as f:
        f.seek(postbox_os.MAX_CACHE_BYTES + 4096); f.write(b"\n")
    monkeypatch.setattr(pb, "STATUS", str(big_status))
    monkeypatch.setattr(sys, "argv", ["postbox.py", "--status"])
    with pytest.raises(postbox_os.OversizeFileError):
        pb.main()


def test_p1_b1_b2_streaming_quarantine_and_oversize_routable_vs_unroutable(tmp_path, monkeypatch):
    pb = _load_pb("pb_r3io_p1b12", tmp_path, monkeypatch)
    ident = _ident(tmp_path)
    valid_prefix = (json.dumps({"event": "message", "key": "msg/ok", "version": 1}) + "\n").encode("utf-8")
    damaged_tail = b'{"event":"message","corrupt":"' + (b"X" * 300000)
    Path(ident["log"]).write_bytes(valid_prefix + damaged_tail)
    with pb.log_lock(ident["log"]):
        assert pb._trim_partial_tail(ident["log"]) == len(valid_prefix)
    assert Path(ident["log"]).read_bytes() == valid_prefix
    q_rec = json.loads(Path(ident["log"] + ".quarantine.jsonl").read_text(encoding="utf-8").strip())
    assert q_rec["sha256"] == hashlib.sha256(damaged_tail).hexdigest() and q_rec["bytes_len"] == len(damaged_tail)
    assert (Path(ident["log"] + ".quarantine.jsonl.raw") / q_rec["raw_blob"]).read_bytes() == damaged_tail

    ident_hold = _ident(tmp_path, "seat-qhold")
    raw_before = valid_prefix + b'{"partial_unterminated'
    Path(ident_hold["log"]).write_bytes(raw_before)
    monkeypatch.setattr(pb, "QUARANTINE_TOTAL_MAX_BYTES", 10)
    with pb.log_lock(ident_hold["log"]):
        with pytest.raises(pb._QuarantineHoldError):
            pb._trim_partial_tail(ident_hold["log"])
    assert Path(ident_hold["log"]).read_bytes() == raw_before
    monkeypatch.setattr(pb, "QUARANTINE_TOTAL_MAX_BYTES", 64 * 1048576)

    ident_ov = _ident(tmp_path, "seat-ov")
    stage_dir = Path(ident_ov["log"] + ".oversize.jsonl.raw")
    routable_frame = (json.dumps({
        "event": "message", "type": "mesh_message", "key": "msg/ov-routable", "version": 1,
        "role": "to", "from": "alice", "created_at": "2026-11-01T08:00:00Z",
        "preview_unverified": "SECRET_ROUTABLE_BODY_" + ("S" * (delivery_bank.MAX_LINE + 4096))}) + "\n").encode("utf-8")
    unroutable_frames = [
        b'{"event":"message","broken":' + (b"A" * (delivery_bank.MAX_LINE + 2048)) + b"\n",
        (b"[" * 520) + (b"0," * (delivery_bank.MAX_LINE // 2 + 1024)) + b"0" + (b"]" * 520) + b"\n",
        (json.dumps({"event": "message", "key": "msg/core-over", "version": 1,
                     "role": "R" * 70000, "pad": "P" * (delivery_bank.MAX_LINE + 1024)}) + "\n").encode("utf-8"),
        (json.dumps(["array_frame", "Z" * (delivery_bank.MAX_LINE + 2048)]) + "\n").encode("utf-8"),
        (json.dumps({"event": "message", "version": 1, "pad": "M" * (delivery_bank.MAX_LINE + 2048)}) + "\n").encode("utf-8"),
        (json.dumps({"event": "future_unknown_event", "pad": "U" * (delivery_bank.MAX_LINE + 2048)}) + "\n").encode("utf-8"),
    ]
    stream = io.BytesIO(routable_frame + b"".join(unroutable_frames))
    with open(ident_ov["log"], "ab", buffering=0) as out:
        for raw, ov in pb._iter_bounded_lines(stream, delivery_bank.MAX_LINE, stage_dir=str(stage_dir)):
            pb.record_listener_line(ident_ov, out, raw, oversize_info=ov)
    assert all(b"SECRET_ROUTABLE_BODY_" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    ov_rows = [json.loads(ln) for ln in Path(ident_ov["log"] + ".oversize.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(ov_rows) == 1 + len(unroutable_frames) and ov_rows[0]["raw_preserved"] is False
    for idx, expected_raw in enumerate(unroutable_frames, start=1):
        assert ov_rows[idx]["raw_preserved"] is True and (stage_dir / ov_rows[idx]["raw_blob"]).read_bytes() == expected_raw

    real_replace, exdev_frame = pb.os.replace, b'{"exdev_broken":' + (b"E" * (delivery_bank.MAX_LINE + 1024)) + b"\n"
    monkeypatch.setattr(pb.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError(pb.errno.EXDEV, "xdev")) if ".ovstage-" in str(s) else real_replace(s, d))
    with open(ident_ov["log"], "ab", buffering=0) as out:
        for raw, ov in pb._iter_bounded_lines(io.BytesIO(exdev_frame), delivery_bank.MAX_LINE, stage_dir=str(tmp_path / "alt_stage")):
            pb.record_listener_line(ident_ov, out, raw, oversize_info=ov)
    monkeypatch.setattr(pb.os, "replace", real_replace)
    (_, no_stage_ov), = list(pb._iter_bounded_lines(io.BytesIO(exdev_frame), delivery_bank.MAX_LINE, stage_dir=None))
    assert "_stage_path" not in no_stage_ov and "stage_dir required" in no_stage_ov["_stage_error"]
    orphan_raw = b'{"orphan_blob":' + (b"O" * (delivery_bank.MAX_LINE + 512)) + b"\n"
    (stage_dir / "ov-orphan.bin").write_bytes(orphan_raw)
    ov_info = dict(pb._OversizeJsonScanner().scan(orphan_raw), _target_blob="ov-orphan.bin")
    with open(ident_ov["log"], "ab", buffering=0) as out:
        pb.record_listener_line(ident_ov, out, b"", oversize_info=ov_info)
    ov_rows2 = [json.loads(ln) for ln in Path(ident_ov["log"] + ".oversize.jsonl").read_text(encoding="utf-8").splitlines()]
    assert (stage_dir / ov_rows2[-2]["raw_blob"]).read_bytes() == exdev_frame and ov_rows2[-1]["raw_blob"] == "ov-orphan.bin"

    ident_half, budget = _ident(tmp_path, "seat-half"), 2 * delivery_bank.MAX_LINE + 16384
    monkeypatch.setattr(pb, "QUARANTINE_TOTAL_MAX_BYTES", budget)
    over_half = b'{"over_half":' + (b"H" * (budget * 2 // 3)) + b"\n"
    with open(ident_half["log"], "ab", buffering=0) as out:
        for raw, ov in pb._iter_bounded_lines(io.BytesIO(over_half), delivery_bank.MAX_LINE, stage_dir=ident_half["log"] + ".oversize.jsonl.raw"):
            pb.record_listener_line(ident_half, out, raw, oversize_info=ov)
        for raw, ov in pb._iter_bounded_lines(io.BytesIO(over_half), delivery_bank.MAX_LINE, stage_dir=ident_half["log"] + ".oversize.jsonl.raw"):
            with pytest.raises(pb._QuarantineHoldError):
                pb.record_listener_line(ident_half, out, raw, oversize_info=ov)
            pb._queue_uncommitted(ident_half, raw, ov)
    class _HoldLatched(Exception):
        pass

    monkeypatch.setattr(pb, "listeners_on", lambda _st: [])
    monkeypatch.setattr(pb.time, "sleep", lambda _s: (_ for _ in ()).throw(_HoldLatched("hold_latched")))
    with pytest.raises((pb._QuarantineHoldError, _HoldLatched)):
        pb.run_identity(ident_half, "http://127.0.0.1:9")
    assert pb._status[ident_half["name"]]["listener"] == "hold" and "quarantine_budget" in pb._status[ident_half["name"]]["listener_hold"]
    assert Path(ident_half["log"] + ".hold.json").exists()
    monkeypatch.setattr(pb, "QUARANTINE_TOTAL_MAX_BYTES", 64 * 1048576)


def test_run_bounded_cmd_injected_caps_and_kimi_turn_tail_bounds(tmp_path, monkeypatch):
    pb = _load_pb("pb_r3io_kimi", tmp_path, monkeypatch)
    monkeypatch.setattr(pb.subprocess, "run", lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "A" * 5000, "E" * 5000))
    capped = pb._run_bounded_cmd(["x"], max_stdout=256, max_stderr=128, text=True)
    assert capped.stdout_truncated is True and capped.stderr_truncated is True and len(capped.stdout) == 257
    monkeypatch.undo()
    pb2 = _load_pb("pb_r3io_kimi2", tmp_path, monkeypatch)
    flood_kimi = tmp_path / "kimi_flood.py"
    flood_kimi.write_text(
        "import sys\nchunk = b'K' * 65536\n"
        "for _ in range(160): sys.stdout.buffer.write(chunk)\n"
        "sys.stdout.buffer.write(b'TAIL_MARKER_END\\n'); sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    (rc, tail_txt), peak_k = _traced(lambda: pb2.kimi_turn([sys.executable, "-S", str(flood_kimi)], str(tmp_path), dict(os.environ), 30))
    assert rc == 0 and tail_txt.endswith("TAIL_MARKER_END\n") and len(tail_txt) <= 2000 and peak_k < 2 * 1048576
    chunks_it = iter([b"s" * 1024] * 6000 + [b"SMALL_TAIL_END\n", b""])
    fake_out = type("_Out", (), {"read1": staticmethod(lambda _n: next(chunks_it)), "close": staticmethod(lambda: None)})()
    fake_proc = type("_P", (), {"pid": 999999, "stdout": fake_out, "wait": staticmethod(lambda timeout=None: 0), "poll": staticmethod(lambda: 0)})()
    monkeypatch.setattr(pb2.subprocess, "Popen", lambda *_a, **_k: fake_proc)
    monkeypatch.setattr(pb2, "tie_to_daemon", lambda _p: None)
    (rc2, tail2), peak_s = _traced(lambda: pb2.kimi_turn(["kimi"], str(tmp_path), {}, 30))
    assert rc2 == 0 and tail2.endswith("SMALL_TAIL_END\n") and peak_s < 512 * 1024


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
