"""tools/postbox/kimi_arm_hook.py: a loop-wrapped waiter blocks the stop (ASDLC OI-75, 2026-10-03).

A waiter started as `while true; do python wait_next.py <identity>; sleep 1; done` holds the
identity's wait lock for ever (each exit respawns at once), so the hook's lock check alone reads
"armed" — but wait_next.py wakes the kimi session by EXITING, and the shell loop swallows every
exit. uxxx measured ~23 hours of mail printed into the loop's output with no turn run on the
nxxxxx seat. The hook now looks for a shell process whose command line has a loop shape AND
wait_next.py with the seat's identity, matched by process NAME and loop SHAPE, and blocks the
stop with an instruction to end that task and re-arm BARE. A process that merely QUOTES the loop
text (a prompt in some non-shell process's command line) must not block.

The held locks in these tests are real: a SEPARATE PROCESS takes them the way wait_next.py and the
postbox do (postbox_os.try_lock), same pattern as test_postbox_lock.py.

Run:  .venv/Scripts/python.exe -m pytest tests/test_postbox_kimi_arm_hook.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

msvcrt = pytest.importorskip("msvcrt", reason="kimi_arm_hook is Windows-only (msvcrt)")

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"


@pytest.fixture
def hook():
    spec = importlib.util.spec_from_file_location("kimi_arm_hook_under_test", PB / "kimi_arm_hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def make_seat(tmp_path, hook, monkeypatch):
    """Write a one-identity seat into a tmp postbox dir; returns the identity's log path stem."""
    def _make(identity):
        (tmp_path / "kimi-seats.json").write_text(json.dumps({"s1": identity}), encoding="utf-8")
        (tmp_path / "config.json").write_text(
            json.dumps({"identities": [{"name": identity, "log": str(tmp_path / (identity + ".log"))}]}),
            encoding="utf-8")
        monkeypatch.setattr(hook, "HERE", str(tmp_path))
        monkeypatch.setattr(hook, "PY", sys.executable)
        return tmp_path
    return _make


@pytest.fixture
def seat(make_seat):
    return make_seat("nxxxxx-prod")


def holder(lock_path, hold_s=30):
    """A second process that takes the lock, says so on stdout, and holds it until killed."""
    code = textwrap.dedent("""
        import os, sys, time
        sys.path.insert(0, sys.argv[1])
        import postbox_os
        fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT)
        assert postbox_os.try_lock(fd)
        print("HELD", flush=True)
        time.sleep(float(sys.argv[3]))
    """)
    p = subprocess.Popen([sys.executable, "-c", code, str(PB), str(lock_path), str(hold_s)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "HELD"
    return p


@pytest.fixture
def armed_locks(seat):
    """Hold postbox.lock and the identity's wait-session lock the way a real waiter would."""
    procs = [holder(seat / "postbox.lock"),
             holder(seat / "nxxxxx-prod.log.wait-session.lock")]
    yield
    for p in procs:
        p.kill()
        p.wait(timeout=10)


def fake_psutil(monkeypatch, processes):
    """Install a fake psutil whose process_iter yields (name,) rows whose .cmdline() answers cmd.

    The hook iterates ["pid", "name"] and calls cmdline() only for shell-named processes, the same
    shape real psutil has; the fake also carries an Error class for the hook's except clause."""
    class _Error(Exception):
        pass

    def _proc(i, name, cmd):
        return SimpleNamespace(pid=9000 + i, info={"pid": 9000 + i, "name": name},
                               cmdline=lambda: cmd)
    fake = SimpleNamespace(Error=_Error,
                           process_iter=lambda attrs: iter(
                               _proc(i, name, cmd) for i, (name, cmd) in enumerate(processes)))
    monkeypatch.setitem(sys.modules, "psutil", fake)


def run_main(hook, monkeypatch, capsys, cwd):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": str(cwd)})))
    rc = hook.main()
    return rc, capsys.readouterr().err


def test_loop_wrapped_waiter_blocks(hook, seat, armed_locks, monkeypatch, capsys):
    fake_psutil(monkeypatch, [
        ("bash.exe", ["bash", "-c", "while true; do python wait_next.py nxxxxx-prod; sleep 1; done"]),
    ])
    rc, err = run_main(hook, monkeypatch, capsys, seat)
    assert rc == 2
    assert "shell loop" in err
    assert "BARE" in err
    assert "TaskStop" in err


def test_loop_in_one_bash_c_string_still_blocks(hook, seat, armed_locks, monkeypatch, capsys):
    # Git Bash often passes the whole loop as one -c argument; identity must match inside it.
    fake_psutil(monkeypatch, [
        ("sh.exe", ["sh", "-c", 'while true; do python wait_next.py nxxxxx-prod; sleep 1; done']),
    ])
    rc, err = run_main(hook, monkeypatch, capsys, seat)
    assert rc == 2
    assert "shell loop" in err


def test_bare_waiter_passes(hook, seat, armed_locks, monkeypatch, capsys):
    fake_psutil(monkeypatch, [
        ("python.exe", ["python", "C:/Users/x/.meshychat/postbox/wait_next.py", "nxxxxx-prod"]),
    ])
    rc, err = run_main(hook, monkeypatch, capsys, seat)
    assert rc == 0
    assert err == ""


def test_prompt_quoting_loop_text_does_not_block(hook, seat, armed_locks, monkeypatch, capsys):
    # A prompt that quotes the words lives in a NON-shell process's command line; NAME gate saves it.
    quote = "please never run: while true; do python wait_next.py nxxxxx-prod; sleep 1; done"
    fake_psutil(monkeypatch, [
        ("node.exe", ["node", "kimi", "agent", "--prompt", quote]),
        ("python.exe", ["python", "-c", quote]),
    ])
    rc, err = run_main(hook, monkeypatch, capsys, seat)
    assert rc == 0
    assert err == ""


def test_other_identity_loop_does_not_block_this_seat(hook, seat, armed_locks, monkeypatch, capsys):
    fake_psutil(monkeypatch, [
        ("bash.exe", ["bash", "-c", "while true; do python wait_next.py uxxx-prod; sleep 1; done"]),
    ])
    rc, err = run_main(hook, monkeypatch, capsys, seat)
    assert rc == 0
    assert err == ""


def test_superstring_identity_loop_does_not_block(hook, seat, armed_locks, monkeypatch, capsys):
    # A loop for nxxxxx-prod2 next to a seat named nxxxxx-prod: a plain substring match would
    # convict, the standalone-word boundary must not (memory-bxxxxx's review on PR #67).
    fake_psutil(monkeypatch, [
        ("bash.exe", ["bash", "-c", "while true; do python wait_next.py nxxxxx-prod2; sleep 1; done"]),
    ])
    rc, err = run_main(hook, monkeypatch, capsys, seat)
    assert rc == 0
    assert err == ""


def test_substring_seat_identity_does_not_block(hook, make_seat, tmp_path, monkeypatch, capsys):
    # A seat named nxxxxx next to a loop for nxxxxx-prod: same boundary, from the other side.
    seat = make_seat("nxxxxx")
    procs = [holder(tmp_path / "postbox.lock"),
             holder(tmp_path / "nxxxxx.log.wait-session.lock")]
    try:
        fake_psutil(monkeypatch, [
            ("bash.exe", ["bash", "-c", "while true; do python wait_next.py nxxxxx-prod; sleep 1; done"]),
        ])
        rc, err = run_main(hook, monkeypatch, capsys, seat)
    finally:
        for p in procs:
            p.kill()
            p.wait(timeout=10)
    assert rc == 0
    assert err == ""


def test_bare_waiter_in_a_bash_shell_passes(hook, seat, armed_locks, monkeypatch, capsys):
    # The realistic bare waiter a Shell tool starts: bash -c "python .../wait_next.py nxxxxx-prod",
    # no loop. Shell-name and identity both match, so ONLY the loop-shape check keeps it from
    # conviction; without it the re-arm instruction would be unsatisfiable (uxxx's review, PR #67).
    fake_psutil(monkeypatch, [
        ("bash.exe", ["bash", "-c", "C:/Python312/python.exe C:/postbox/wait_next.py nxxxxx-prod"]),
    ])
    rc, err = run_main(hook, monkeypatch, capsys, seat)
    assert rc == 0
    assert err == ""


def test_shell_loop_without_wait_next_does_not_block(hook, seat, armed_locks, monkeypatch, capsys):
    # A shell loop that carries the identity but never touches wait_next.py is not the waiter.
    fake_psutil(monkeypatch, [
        ("bash.exe", ["bash", "-c", "while true; do echo nxxxxx-prod; sleep 1; done"]),
    ])
    rc, err = run_main(hook, monkeypatch, capsys, seat)
    assert rc == 0
    assert err == ""


def test_unarmed_seat_gets_bare_instruction(hook, seat, monkeypatch, capsys):
    # Postbox running (its lock held) but no waiter holding the identity's wait lock.
    p = holder(seat / "postbox.lock")
    try:
        fake_psutil(monkeypatch, [])
        rc, err = run_main(hook, monkeypatch, capsys, seat)
    finally:
        p.kill()
        p.wait(timeout=10)
    assert rc == 2
    assert "not armed" in err
    assert "BARE" in err                       # the plain-words warning rides the normal arm text
    assert "wait_next.py nxxxxx-prod" in err


# ---- headless delivery turns (postbox.run_kimi_delivery, 24 Oct 2026, thread <thread>) --------
# A headless turn must end without a waiter, or it never exits. The skip is bound to the session id the postbox
# delivers to (pxx 31e3ed): unset arms, another session's id arms, this session's id lets the turn end.

def unarmed_run(hook, seat, monkeypatch, capsys):
    p = holder(seat / "postbox.lock")
    try:
        fake_psutil(monkeypatch, [])
        return run_main(hook, monkeypatch, capsys, seat)
    finally:
        p.kill()
        p.wait(timeout=10)


def test_headless_unset_still_arms(hook, seat, monkeypatch, capsys):
    monkeypatch.delenv("MESHY_KIMI_HEADLESS", raising=False)
    rc, err = unarmed_run(hook, seat, monkeypatch, capsys)
    assert rc == 2 and "not armed" in err            # kxxx's control: an interactive stop is still blocked


def test_headless_for_another_session_still_arms(hook, seat, monkeypatch, capsys):
    monkeypatch.setenv("MESHY_KIMI_HEADLESS", "s2")
    rc, err = unarmed_run(hook, seat, monkeypatch, capsys)
    assert rc == 2 and "not armed" in err


def test_headless_bare_one_does_not_switch_arming_off(hook, seat, monkeypatch, capsys):
    monkeypatch.setenv("MESHY_KIMI_HEADLESS", "1")
    rc, err = unarmed_run(hook, seat, monkeypatch, capsys)
    assert rc == 2 and "not armed" in err


def test_headless_for_this_session_ends_the_turn(hook, seat, monkeypatch, capsys):
    monkeypatch.setenv("MESHY_KIMI_HEADLESS", "s1")
    rc, err = unarmed_run(hook, seat, monkeypatch, capsys)
    assert rc == 0 and err == ""


# ---- --resolve: a probe refuses to start in a folder mapped to a production identity (24 Oct 2026) ------------

def test_resolve_refuses_a_folder_mapped_to_prod(hook, tmp_path):
    probe = tmp_path / "kimitest"
    probe.mkdir()
    (tmp_path / "kimi-seats.json").write_text(json.dumps({"cwd:" + str(probe): "nxxxxx-prod:kimitest"}),
                                              encoding="utf-8")
    out = io.StringIO()
    assert hook.resolve_cli([str(probe)], here=str(tmp_path), out=out) == 3
    assert "nxxxxx-prod:kimitest" in out.getvalue() and "REFUSED" in out.getvalue()


def test_resolve_allows_a_test_identity_and_an_unmapped_folder(hook, tmp_path):
    probe = tmp_path / "kimi-delivery-test"
    probe.mkdir()
    (tmp_path / "kimi-seats.json").write_text(json.dumps({"cwd:" + str(probe): "kimi-delivery-test"}),
                                              encoding="utf-8")
    out = io.StringIO()
    assert hook.resolve_cli([str(probe)], here=str(tmp_path), out=out) == 0
    assert "kimi-delivery-test" in out.getvalue()
    out = io.StringIO()
    assert hook.resolve_cli([str(tmp_path / "elsewhere")], here=str(tmp_path), out=out) == 0
    assert "(none)" in out.getvalue()


def test_resolve_refuses_when_the_seat_map_cannot_be_read(hook, tmp_path):
    out = io.StringIO()
    assert hook.resolve_cli(["--here", str(tmp_path), str(tmp_path)], out=out) == 2
    assert "cannot read kimi-seats.json" in out.getvalue()


def test_resolve_refuses_a_subfolder_and_a_junction_of_a_mapped_folder(hook, tmp_path):
    # pxx eac28e #6: realpath on both sides, and a folder inside a mapped folder counts as that folder's seat
    probe = tmp_path / "kimitest"
    (probe / "deeper").mkdir(parents=True)
    (tmp_path / "kimi-seats.json").write_text(json.dumps({"cwd:" + str(probe): "nxxxxx-prod:kimitest"}),
                                              encoding="utf-8")
    out = io.StringIO()
    assert hook.resolve_cli([str(probe / "deeper")], here=str(tmp_path), out=out) == 3, out.getvalue()
    link = tmp_path / "innocent"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(probe)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("could not make a junction: %s" % r.stdout.strip())
    out = io.StringIO()
    assert hook.resolve_cli([str(link)], here=str(tmp_path), out=out) == 3, out.getvalue()
    assert "nxxxxx-prod" in out.getvalue()
