"""wait_next.py --once: a consumer with no offset file yet must still see mail that arrives between runs.

Measured on HQB, 2026-10-01. A Kimi seat polled with `wait_next.py nxxxxx-prod --consumer cron --once`
from a 15 minute cron. A consumer with no offset file starts at the END of the log ("from now"), and
the offset was saved only when it moved. A poll that found nothing new therefore saved nothing, the
next poll started at the new end again, and mail that arrived between two polls was never delivered.
The cron fired on time at 10:37:32Z and 10:52:31Z and reported `empty` both times, with two messages
(10:36:22Z, 10:36:50Z) sitting in the log from before the first fire of that consumer.

The fix: the first pass of a consumer that has no offset file writes one, even when nothing is new.

Each poll runs in its own process against a log and a config in tmp_path. Nothing here touches a
server, a database or the real postbox directory.

Run:  .venv/Scripts/python.exe -m pytest tests/test_postbox_wait_next_once.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POSTBOX_DIR = ROOT / "tools" / "postbox"


def _msg(key):
    return {"event": "message", "type": "mesh_message", "key": key, "role": "to",
            "from_peer_id": "11111111-1111-1111-1111-111111111111", "to": ["seat"],
            "created_at": "2026-10-01T10:36:22Z", "preview_unverified": "x", "preview_truncated": False,
            "via": "live", "rx_at": "2026-10-01T10:36:22.579Z"}


def _append(log, obj):
    with open(log, "ab") as f:
        f.write((json.dumps(obj) + "\n").encode("utf-8"))


@pytest.fixture
def wn(tmp_path):
    """A copy of wait_next.py (and the postbox_os it imports) in tmp_path beside a one-identity
    config.json, because the script finds its config and its postbox lock in its own directory.
    Returns (run, log, offset_file): run(*argv) is one poll in its OWN process, as a cron runs it, so
    the consumer lock is released between polls exactly as it is in production."""
    for name in ("wait_next.py", "postbox_os.py", "delivery_bank.py"):
        shutil.copy(POSTBOX_DIR / name, tmp_path / name)
    log = tmp_path / "seat.log"
    (tmp_path / "config.json").write_text(json.dumps({
        "base_url": "http://127.0.0.1:1",
        "identities": [{"name": "seat", "log": log.as_posix(), "client": tmp_path.as_posix(),
                        "python": sys.executable, "state": (tmp_path / "state.json").as_posix()}],
    }), encoding="utf-8")

    def run(*argv):
        p = subprocess.run([sys.executable, str(tmp_path / "wait_next.py"), "seat", *argv],
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stdout + p.stderr
        return [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]

    return run, log, Path(str(log) + ".wait-cron.offset")


#: What a poll with no mail reports. No postbox daemon runs in tmp_path, so the probe says
#: postbox_not_running; with one it says empty. Neither is a delivery, which is what these tests ask.
QUIET = ("empty", "postbox_not_running")


def _keys(events):
    return [e["key"] for e in events if e.get("event") == "message"]


def _state(events):
    return [e["state"] for e in events if e.get("event") == "waiter"]


def _read_with(events):
    """The key of each read_with line, in order. A seat reads a message with the command on this
    line, so a delivery without it is a notice the seat cannot follow."""
    return [e["key"] for e in events if e.get("event") == "read_with"]


def test_once_delivers_mail_that_arrives_between_two_polls(wn):
    """The incident. Poll one finds nothing new; mail arrives; poll two must deliver it."""
    run, log, off_file = wn
    _append(log, _msg("msg/1-old"))                       # history from before this consumer existed

    first = run("--consumer", "cron", "--once")
    assert _keys(first) == [], "history is not replayed to a new consumer"
    assert _state(first)[0] in QUIET
    assert off_file.exists(), "the first pass must save the offset even when nothing is new"
    assert int(off_file.read_text()) == log.stat().st_size

    _append(log, _msg("msg/2-between-polls"))

    second = run("--consumer", "cron", "--once")
    assert _keys(second) == ["msg/2-between-polls"]
    assert _read_with(second) == ["msg/2-between-polls"], "each delivered message carries its read command"
    assert _state(second) == ["woke"]
    assert int(off_file.read_text()) == log.stat().st_size


def test_once_does_not_deliver_twice(wn):
    """Control for the test above: the saved offset is honoured, so a third poll delivers nothing."""
    run, log, off_file = wn
    _append(log, _msg("msg/1-old"))
    run("--consumer", "cron", "--once")
    _append(log, _msg("msg/2-between-polls"))
    assert _keys(run("--consumer", "cron", "--once")) == ["msg/2-between-polls"]

    third = run("--consumer", "cron", "--once")
    assert _keys(third) == []
    assert _state(third)[0] in QUIET


def test_an_existing_offset_is_not_rewritten_when_nothing_is_new(wn):
    """Unchanged behavior: a consumer that already has an offset file does not write on an empty poll,
    so the fix adds one write in a consumer's lifetime and none after it."""
    run, log, off_file = wn
    _append(log, _msg("msg/1-old"))
    off_file.write_text(str(log.stat().st_size))
    before = off_file.stat().st_mtime_ns

    assert _keys(run("--consumer", "cron", "--once")) == []
    assert off_file.stat().st_mtime_ns == before


def test_a_backlog_behind_a_saved_offset_is_delivered(wn):
    """Unchanged behavior: a late poll with a saved offset delivers everything after it."""
    run, log, off_file = wn
    off_file.write_text("0")
    _append(log, _msg("msg/1"))
    _append(log, _msg("msg/2"))

    got = run("--consumer", "cron", "--once")
    assert _keys(got) == ["msg/1", "msg/2"]
    assert _read_with(got) == ["msg/1", "msg/2"]


@pytest.mark.parametrize("damaged", ["", "not a number"])
def test_an_unreadable_offset_file_is_replaced_on_the_first_pass(wn, damaged):
    """The same blindness by another road (nxxxxx's review of this file): an offset file that exists
    but holds no number must be rewritten on the first pass, or mail between two polls is lost. And
    since kxxx's ruling on #78 it replays the log from the start, so the mail queued behind the lost
    position is delivered, not skipped."""
    run, log, off_file = wn
    queued = _msg("msg/1-old")
    queued["created_at"] = queued["rx_at"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # received inside the replayed day
    _append(log, queued)
    off_file.write_text(damaged)

    assert _keys(run("--consumer", "cron", "--once")) == ["msg/1-old"]
    assert int(off_file.read_text()) == log.stat().st_size

    _append(log, _msg("msg/2-between-polls"))
    assert _keys(run("--consumer", "cron", "--once")) == ["msg/2-between-polls"]


def test_a_blocking_waiter_saves_a_quiet_offset_once_not_every_pass(tmp_path, monkeypatch, capsys):
    """memory-bxxxxx's review of #51: with the first-pass save, a blocking waiter that never marks the
    offset as saved rewrites the file on every 2 second pass (measured: 5 writes in 9 s against 1).
    --once makes one pass and cannot see that, so this drives the blocking loop in-process: two quiet
    passes, then mail. The offset file is written twice, once for the first pass and once for the
    delivery, and not on the quiet pass between them."""
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location("wait_next_blocking_under_test", POSTBOX_DIR / "wait_next.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log = tmp_path / "seat.log"
    (tmp_path / "config.json").write_text(json.dumps({
        "base_url": "http://127.0.0.1:1",
        "identities": [{"name": "seat", "log": log.as_posix(), "client": tmp_path.as_posix(),
                        "python": sys.executable, "state": (tmp_path / "state.json").as_posix()}],
    }), encoding="utf-8")
    _append(log, _msg("msg/1-old"))
    monkeypatch.setattr(mod, "HERE", str(tmp_path))
    monkeypatch.setattr(mod, "stdout_discarded", lambda: False)
    monkeypatch.setattr(mod, "postbox_running", lambda: True)
    monkeypatch.setattr(sys, "argv", ["wait_next.py", "seat"])

    writes, sleeps = [], []
    real_replace = mod.os.replace

    def counting_replace(src, dst):
        writes.append(dst)
        return real_replace(src, dst)

    def sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) == 2:                               # after the second quiet pass, mail arrives
            _append(log, _msg("msg/2-while-blocking"))
        assert len(sleeps) < 10, "the waiter never woke"

    monkeypatch.setattr(mod.os, "replace", counting_replace)
    monkeypatch.setattr(mod.time, "sleep", sleep)

    assert mod.main() == 0
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert _keys(events) == ["msg/2-while-blocking"]
    assert len(sleeps) == 2, "two quiet passes ran before the mail"
    assert len(writes) == 4, "one bank and one compatibility offset per commit, no quiet-pass writes: %r" % writes


def test_wait_next_holds_cc_without_ack_and_piggybacks_on_next_wake(wn, tmp_path):
    """Issue #199: when hold_cc_without_ack is true on the identity, wait_next.py holds a cc-only
    message with requires_ack=False instead of waking immediately, persists it in the bank checkpoint,
    and delivers it alongside the next waking message."""
    run, log, off_file = wn
    cfg_path = tmp_path / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["identities"][0]["hold_cc_without_ack"] = True
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    # Initial pass establishes offset at 0
    assert _state(run("--consumer", "cron", "--once"))[0] in QUIET

    cc_msg = dict(_msg("msg/1-cc-no-ack"), role="cc", to=["other"], cc=["seat"], requires_ack=False)
    _append(log, cc_msg)

    # Poll 1: cc without ack is held, waiter stays quiet, checkpoint records held line
    first = run("--consumer", "cron", "--once")
    assert _keys(first) == []
    assert _state(first)[0] in QUIET
    bank_file = Path(str(off_file) + ".bank.json")
    bank_data = json.loads(bank_file.read_text(encoding="utf-8"))
    assert [h["routing"]["key"] for h in bank_data.get("held", [])] == ["msg/1-cc-no-ack"]

    # Poll 2: a direct "to" message arrives; both the held cc and the new "to" message deliver together
    _append(log, _msg("msg/2-direct-to"))
    second = run("--consumer", "cron", "--once")
    assert _keys(second) == ["msg/1-cc-no-ack", "msg/2-direct-to"]
    assert _read_with(second) == ["msg/1-cc-no-ack", "msg/2-direct-to"]
    assert _state(second) == ["woke"]
    bank_data_after = json.loads(bank_file.read_text(encoding="utf-8"))
    assert bank_data_after.get("held", []) == []


def test_wait_next_commits_held_changed_on_quiet_pass_and_warns_on_bad_setting(tmp_path, monkeypatch, capsys):
    """Issue #199: (1) if bank.held_changed is True on a pass where new_off == bank.offset and hits == []
    (e.g. a held entry whose token is already in delivered is released when hold is disabled, with no
    new log bytes), wait_next still commits the cleaned checkpoint; (2) a non-boolean hold_cc_without_ack
    reports to stderr via wait_next."""
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location("wait_next_hold_under_test", POSTBOX_DIR / "wait_next.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log = tmp_path / "seat.log"
    log.write_bytes(b"")
    off_file = Path(str(log) + ".wait-cron.offset")
    off_file.write_text("0", encoding="utf-8")
    bank_file = Path(str(off_file) + ".bank.json")
    tok = '["message","msg/already-done",1]'
    bank_file.write_text(json.dumps({
        "schema_version": 2, "consumer": "wait:cron", "identity": "seat",
        "offset": 0, "delivered": [tok], "held_releasing": False,
        "held": [{"token": tok, "routing": _msg("msg/already-done"), "held_at": "2026-10-15T00:00:00Z"}],
    }), encoding="utf-8")
    bank_file.chmod(0o600)

    (tmp_path / "config.json").write_text(json.dumps({
        "base_url": "http://127.0.0.1:1",
        "identities": [{"name": "seat", "log": log.as_posix(), "client": tmp_path.as_posix(),
                        "python": sys.executable, "state": (tmp_path / "state.json").as_posix(),
                        "hold_cc_without_ack": "invalid-string"}],
    }), encoding="utf-8")
    monkeypatch.setattr(mod, "HERE", str(tmp_path))
    monkeypatch.setattr(mod, "stdout_discarded", lambda: False)
    monkeypatch.setattr(mod, "postbox_running", lambda: True)
    monkeypatch.setattr(sys, "argv", ["wait_next.py", "seat", "--consumer", "cron", "--once"])

    assert mod.main() == 0
    captured = capsys.readouterr()
    assert "wait_next: seat: hold_cc_without_ack='invalid-string' is not true or false" in captured.err
    saved_bank = json.loads(bank_file.read_text(encoding="utf-8"))
    assert saved_bank.get("held", []) == [], "bank.held_changed must trigger bank.commit even when offset did not move"


def test_wait_next_hot_reloads_hold_cc_without_ack_across_blocking_passes(tmp_path, monkeypatch, capsys):
    """#199 / #345 gap fix: a long-lived blocking wait_next.py refreshes hold_cc_without_ack via
    postbox_os.refresh_hold_options() on each poll pass when config.json changes."""
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location("wait_next_hot_reload_under_test", POSTBOX_DIR / "wait_next.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log = tmp_path / "seat.log"
    log.write_bytes(b"")
    cfg_path = tmp_path / "config.json"

    def write_cfg(hold_val):
        cfg_path.write_text(json.dumps({
            "base_url": "http://127.0.0.1:1",
            "identities": [{"name": "seat", "log": log.as_posix(), "client": tmp_path.as_posix(),
                            "python": sys.executable, "state": (tmp_path / "state.json").as_posix(),
                            "hold_cc_without_ack": hold_val}],
        }), encoding="utf-8")

    # Start with hold_cc_without_ack=False
    write_cfg(False)
    monkeypatch.setattr(mod, "HERE", str(tmp_path))
    monkeypatch.setattr(mod, "stdout_discarded", lambda: False)
    monkeypatch.setattr(mod, "postbox_running", lambda: True)
    monkeypatch.setattr(sys, "argv", ["wait_next.py", "seat", "--consumer", "session"])

    sleeps = []

    def sleep(_seconds):
        sleeps.append(len(sleeps) + 1)
        if len(sleeps) == 1:
            # Hot-enable hold_cc_without_ack=True and append a cc(requires_ack=False) message
            write_cfg(True)
            _append(log, dict(_msg("msg/cc-held-on-reload"), role="cc", to=["other"], cc=["seat"], requires_ack=False))
        elif len(sleeps) == 2:
            # Pass 2 held the cc message without waking! Now hot-disable hold_cc_without_ack=False so pass 3 releases it
            bank_path = Path(str(log) + ".wait-session.offset.bank.json")
            bank_data = json.loads(bank_path.read_text(encoding="utf-8"))
            assert [h["routing"]["key"] for h in bank_data.get("held", [])] == ["msg/cc-held-on-reload"]
            write_cfg(False)
        assert len(sleeps) < 6, "waiter did not wake after hot-disabling hold_cc_without_ack"

    monkeypatch.setattr(mod.time, "sleep", sleep)
    assert mod.main() == 0
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert _keys(events) == ["msg/cc-held-on-reload"]
    assert _state(events) == ["woke"]
    assert len(sleeps) == 2


def test_wait_next_peek_bank_succeeds_while_waiter_lock_is_held_and_flush_held_drains(wn, tmp_path):
    """--peek-bank is lock-free and works while another waiter holds .wait-<consumer>.lock;
    --flush-held drains banked cc items at a turn boundary."""
    import os as _os
    sys.path.insert(0, str(POSTBOX_DIR))
    import postbox_os as _postbox_os

    run, log, off_file = wn
    cfg_path = tmp_path / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["identities"][0]["hold_cc_without_ack"] = True
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    # Initial pass sets offset at 0
    assert _state(run("--consumer", "cron", "--once"))[0] in QUIET

    # Append a cc message and bank it on a quiet pass
    _append(log, dict(_msg("msg/cc-banked-1"), role="cc", to=["other"], cc=["seat"], requires_ack=False))
    assert _state(run("--consumer", "cron", "--once"))[0] in QUIET

    # Append an uncollected message after the banked offset
    _append(log, dict(_msg("msg/cc-uncollected-2"), role="cc", to=["other"], cc=["seat"], requires_ack=False))

    # Hold the mandatory waiter lock in this process to simulate a background waiter running
    lock_path = str(log) + ".wait-cron.lock"
    lock_fd = _os.open(lock_path, _os.O_RDWR | _os.O_CREAT)
    try:
        assert _postbox_os.try_lock(lock_fd) is True
        # Normal --once is refused with exit code 2 while lock is held
        p_locked = subprocess.run(
            [sys.executable, str(tmp_path / "wait_next.py"), "seat", "--consumer", "cron", "--once"],
            capture_output=True, text=True, timeout=30,
        )
        assert p_locked.returncode == 2
        assert "another waiter for seat/cron is armed" in p_locked.stdout

        # --peek-bank succeeds with exit code 0 while lock is held and does not advance offset
        peek_rows = run("--consumer", "cron", "--peek-bank")
        assert len(peek_rows) == 1
        peek = peek_rows[0]
        assert peek["ident"] == "seat"
        assert peek["consumer"] == "cron"
        assert peek["held_count"] == 1
        assert [h["key"] for h in peek["held"]] == ["msg/cc-banked-1"]
        assert peek["uncollected_bytes"] > 0
    finally:
        _postbox_os.unlock(lock_fd)
        _os.close(lock_fd)

    # Now run --once --flush-held to drain both the banked cc and the uncollected cc at a turn boundary
    flushed = run("--consumer", "cron", "--once", "--flush-held")
    assert _keys(flushed) == ["msg/cc-banked-1", "msg/cc-uncollected-2"]
    assert _state(flushed) == ["woke"]

    peek_after = run("--consumer", "cron", "--peek-bank")[0]
    assert peek_after["held_count"] == 0
    assert peek_after["uncollected_bytes"] == 0


def test_wait_next_peek_bank_reports_unreadable_bank_and_confines_consumer_name(wn, tmp_path):
    """pxx P2-3 and INFO on #350:
    1. When the .bank.json file is corrupt/half-written, --peek-bank must not silently report held_count=0
       and uncollected_bytes=0; it must return an explicit error state (held_count=None, offset=None,
       uncollected_bytes=None, error=...) and exit nonzero (rc=1).
    2. peek_bank refuses path-traversal consumer names ('x/../../outside').
    """
    import sys as _sys
    _sys.path.insert(0, str(POSTBOX_DIR))
    import delivery_bank as _delivery_bank

    run, log, off_file = wn
    cfg_path = tmp_path / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["identities"][0]["hold_cc_without_ack"] = True
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    assert _state(run("--consumer", "cron", "--once"))[0] in QUIET
    _append(log, dict(_msg("msg/cc-held-before-corrupt"), role="cc", to=["other"], cc=["seat"], requires_ack=False))
    assert _state(run("--consumer", "cron", "--once"))[0] in QUIET

    bank_path = Path(str(off_file) + ".bank.json")
    assert bank_path.exists()
    # Corrupt the bank checkpoint with a half-written JSON payload
    bank_path.write_text('{"schema_version":2, ', encoding="utf-8")

    p_corrupt = subprocess.run(
        [sys.executable, str(tmp_path / "wait_next.py"), "seat", "--consumer", "cron", "--peek-bank"],
        capture_output=True, text=True, timeout=30,
    )
    assert p_corrupt.returncode == 1
    info = json.loads(p_corrupt.stdout.strip().splitlines()[0])
    assert info["ident"] == "seat"
    assert info["consumer"] == "cron"
    assert info["held_count"] is None
    assert info["offset"] is None
    assert info["uncollected_bytes"] is None
    assert isinstance(info.get("error"), str) and info["error"]

    # Consumer path traversal confinement
    bad_info = _delivery_bank.peek_bank(str(log), consumer="x/../../outside", ident="seat")
    assert bad_info["error"] == "invalid consumer name"
    assert bad_info["held_count"] is None
    assert bad_info["offset"] is None


def test_wait_next_peek_bank_reads_real_codex_delivery_bank_and_refuses_target_mismatch(tmp_path, monkeypatch):
    """pxx P2-b (52a05e) and kxxx fe4276 on #350:
    1. A direct Codex bank written by `postbox.run_codex_delivery` (consumer `'codex:' + ident['target']`)
       is read cleanly by `wait_next.py <ident> --consumer codex --peek-bank` (exit 0, no error).
    2. If the identity's `target` in `config.json` changes so the bank's `'codex:<old_target>'` no longer
       matches `'codex:' + ident['target']`, `--peek-bank` reports `delivery checkpoint identity/consumer mismatch`
       and exits 1.
    """
    import importlib.util
    import os

    pb_spec = importlib.util.spec_from_file_location("postbox_codex_under_test", POSTBOX_DIR / "postbox.py")
    pb_mod = importlib.util.module_from_spec(pb_spec)
    pb_spec.loader.exec_module(pb_mod)

    log = tmp_path / "codex-seat.log"
    _append(log, dict(_msg("msg/cc-codex-1"), role="cc", to=["other"], cc=["codex-seat"], requires_ack=False))

    ident = {
        "name": "codex-seat",
        "cli": "codex",
        "target": "thread-live-123",
        "log": log.as_posix(),
        "client": tmp_path.as_posix(),
        "python": sys.executable,
        "state": (tmp_path / "state.json").as_posix(),
        "hold_cc_without_ack": True,
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"base_url": "http://127.0.0.1:1", "identities": [ident]}), encoding="utf-8")

    monkeypatch.setattr(pb_mod, "CONFIG", str(cfg_path))
    monkeypatch.setattr(pb_mod, "STATUS", str(tmp_path / "status.json"))
    monkeypatch.setattr(pb_mod, "ERR", str(tmp_path / "postbox.err.log"))

    # Drive one pass of the real postbox.run_codex_delivery loop so it writes <log>.codex.offset.bank.json
    # with consumer "codex:thread-live-123" and banks the held cc message
    sleeps = []

    class _StopLoop(Exception):
        pass

    def fake_sleep(_s):
        sleeps.append(_s)
        if len(sleeps) == 1:
            # Append a held cc message after the initial fresh commit at EOF
            _append(log, dict(_msg("msg/cc-codex-held"), role="cc", to=["other"], cc=["codex-seat"], requires_ack=False))
        elif len(sleeps) >= 2:
            raise _StopLoop()

    monkeypatch.setattr(pb_mod.time, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        pb_mod.run_codex_delivery(ident)

    codex_bank_path = Path(str(log) + ".codex.offset.bank.json")
    assert codex_bank_path.exists()
    raw_saved = json.loads(codex_bank_path.read_text(encoding="utf-8"))
    assert raw_saved["consumer"] == "codex:thread-live-123"

    env = dict(os.environ, MESHY_POSTBOX_CONFIG=str(cfg_path))
    p_ok = subprocess.run(
        [sys.executable, str(POSTBOX_DIR / "wait_next.py"), "codex-seat", "--consumer", "codex", "--peek-bank"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert p_ok.returncode == 0, p_ok.stdout + p_ok.stderr
    info_ok = json.loads(p_ok.stdout.strip().splitlines()[0])
    assert info_ok.get("error") is None
    assert info_ok["ident"] == "codex-seat"
    assert info_ok["consumer"] == "codex"
    assert info_ok["held_count"] == 1
    assert [h["key"] for h in info_ok["held"]] == ["msg/cc-codex-held"]

    # Now change target in config.json -> --peek-bank must refuse the mismatched codex target and exit 1
    ident_changed = dict(ident, target="thread-other-999")
    cfg_path.write_text(json.dumps({"base_url": "http://127.0.0.1:1", "identities": [ident_changed]}), encoding="utf-8")
    p_mismatch = subprocess.run(
        [sys.executable, str(POSTBOX_DIR / "wait_next.py"), "codex-seat", "--consumer", "codex", "--peek-bank"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert p_mismatch.returncode == 1
    info_mismatch = json.loads(p_mismatch.stdout.strip().splitlines()[0])
    assert info_mismatch["error"] == "delivery checkpoint identity/consumer mismatch"
    assert info_mismatch["held_count"] is None


def test_wait_next_startup_config_stat_before_load_detects_concurrent_edit(tmp_path, monkeypatch, capsys):
    """GitHub Codex r4366519998 + pxx on #350:
    Taking `init_stat_sig = postbox_os.config_stat(cfg_path)` BEFORE `json.load(open(cfg_path))` ensures that
    if `config.json` is edited right after `json.load` (before `watched.append`), `refresh_hold_options`
    immediately detects the signature change and reloads the updated `hold_cc_without_ack` policy.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("wait_next_stat_order_under_test", POSTBOX_DIR / "wait_next.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    log = tmp_path / "seat.log"
    _append(log, dict(_msg("msg/cc-startup-race"), role="cc", to=["other"], cc=["seat"], requires_ack=False))
    cfg_path = tmp_path / "config.json"

    def make_cfg(hold_val):
        return json.dumps({
            "base_url": "http://127.0.0.1:1",
            "identities": [{
                "name": "seat",
                "log": log.as_posix(),
                "client": tmp_path.as_posix(),
                "python": sys.executable,
                "state": (tmp_path / "state.json").as_posix(),
                "hold_cc_without_ack": hold_val,
            }],
        })

    cfg_path.write_text(make_cfg(False), encoding="utf-8")
    (tmp_path / "seat.log.wait-cron.offset").write_text("0", encoding="utf-8")

    real_json_load = mod.json.load
    first_load = True

    def racing_json_load(fp, *a, **kw):
        nonlocal first_load
        res = real_json_load(fp, *a, **kw)
        if first_load:
            first_load = False
            # Simulate config.json changing on disk immediately after wait_next's initial json.load()
            cfg_path.write_text(make_cfg(True), encoding="utf-8")
        return res

    monkeypatch.setattr(mod.json, "load", racing_json_load)
    monkeypatch.setattr(mod, "HERE", str(tmp_path))
    monkeypatch.setattr(mod, "stdout_discarded", lambda: False)
    monkeypatch.setattr(mod, "postbox_running", lambda: True)
    monkeypatch.setattr(sys, "argv", ["wait_next.py", "seat", "--consumer", "cron", "--once"])

    assert mod.main() == 0
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    # Because the edit to hold_cc_without_ack=True was caught, the cc message is held rather than waking!
    assert _keys(events) == []
    bank_data = json.loads((tmp_path / "seat.log.wait-cron.offset.bank.json").read_text(encoding="utf-8"))
    assert [h["routing"]["key"] for h in bank_data.get("held", [])] == ["msg/cc-startup-race"]

