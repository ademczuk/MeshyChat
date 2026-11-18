"""tools/postbox/meshy_channel.py: the delivery loop survives a failed pass (issue #255, item 1).

The channel's tail() thread is the session's only mail delivery. A failure in one pass (an OSError on a full disk, a
damaged checkpoint, an over-size routing notice) used to end the thread silently: ping still answered and every health
view stayed green while no mail arrived. Now each distinct failure is reported once as a [PROBLEM] event, the pass is
retried every RETRY_S, and the mail is delivered once a pass succeeds.

Mutants (each applied and seen to fail):
  C1 the try/except removed (the old code: the error ends tail)       test_a_failing_pass_is_reported_once_and_mail_flows_after
  C2 a report on every failed pass                                    test_a_failing_pass_is_reported_once_and_mail_flows_after
  C3 no wait after a failure (a busy loop)                            test_a_failing_pass_is_reported_once_and_mail_flows_after
  C4 `failed` not reset after a good pass                             test_the_same_failure_after_a_recovery_is_reported_again
  C5 the [PROBLEM] report not guarded (a failing write ends tail)     test_a_write_that_fails_does_not_end_the_thread
  C6 #199: the malformed-setting report not guarded                   test_a_failed_setting_report_does_not_end_the_thread
  C7 #199: the setting read every pass (a report per pass)            test_a_malformed_hold_setting_is_one_problem_and_mail_flows
"""
from __future__ import annotations

import pytest

from tests.test_postbox_channel_lock_retry import Stop, channel, kinds, rig  # noqa: F401  (the fixtures)
from tests.test_postbox_delivery_bank import append, message


def _flaky(channel, monkeypatch, failures):
    """Make Bank.collect raise the given exceptions, in order, before it starts answering for real."""
    real = channel.delivery_bank.Bank.collect
    left = list(failures)

    def collect(self, *a, **kw):
        if left:
            raise left.pop(0)
        return real(self, *a, **kw)
    monkeypatch.setattr(channel.delivery_bank.Bank, "collect", collect)


def test_a_failing_pass_is_reported_once_and_mail_flows_after(channel, rig, monkeypatch):
    """Three failing passes with the same error: ONE channel_error event, a RETRY_S wait after each, and the mail that
    was waiting is delivered by the first pass that works. Before, the first error ended tail for the session."""
    r = rig
    r.release()
    append(r.i["log"], message("msg/waiting"))
    _flaky(channel, monkeypatch, [OSError(28, "No space left on device")] * 3)

    def on_sleep(s):
        if s == 1:                                          # the delivery loop's sleep after a good pass
            raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(r.i)
    assert kinds(r) == ["channel_open", "channel_error", "message"], kinds(r)
    problem = r.pushed[1][0]
    assert problem.startswith("[PROBLEM] MeshyChat channel for seat-prod: delivering failed (OSError:")
    assert "retries every %d s" % channel.RETRY_S in problem
    assert r.sleeps == [channel.RETRY_S] * 3 + [1], r.sleeps
    assert r.pushed[2][1]["key"] == "msg/waiting"


def test_the_same_failure_after_a_recovery_is_reported_again(channel, rig, monkeypatch):
    """A failure, a good pass, then the SAME failure again: two reports, because the recovery ended the first outage
    and this is a new one. The same failure repeated with no good pass between is reported once (the test above)."""
    r = rig
    r.release()
    _flaky(channel, monkeypatch, [ValueError("spool truncated; reconcile the checkpoint before resuming")])
    state = {"n": 0}

    def on_sleep(s):
        state["n"] += 1
        if state["n"] == 2:                                 # after the first good pass, fail once more, the same way
            _flaky(channel, monkeypatch, [ValueError("spool truncated; reconcile the checkpoint before resuming")])
        if state["n"] >= 4:
            raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(r.i)
    errors = [c for c, m in r.pushed if m.get("kind") == "channel_error"]
    assert len(errors) == 2, errors
    assert errors[0] == errors[1] and "ValueError: spool truncated" in errors[0]


def test_a_write_that_fails_does_not_end_the_thread(channel, rig, monkeypatch):
    """MUTANT C5 (the report not guarded). dxxxxx's review: when the failing call in a pass IS the write (a closed
    stdout pipe), the [PROBLEM] report raises the same error. It is caught, noted on stderr, and the loop keeps
    retrying: tail is still running after several retry periods."""
    r = rig
    r.release()
    append(r.i["log"], message("msg/never-written"))
    sent = []

    def broken_send(obj):                                  # the open event goes out; every write after it fails
        sent.append(obj)
        if len(sent) > 1:
            raise OSError(32, "Broken pipe")
    monkeypatch.setattr(channel, "send", broken_send)

    def on_sleep(s):
        if len(r.sleeps) >= 3:                              # three retry periods survived
            raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):                               # not OSError: the thread did not die
        channel.tail(r.i)
    assert r.sleeps == [channel.RETRY_S] * 3, r.sleeps


def test_a_malformed_hold_setting_is_one_problem_and_mail_flows(channel, rig, monkeypatch):
    """#199 follow-up: "hold_cc_without_ack": "true" (a string) holds nothing. It is ONE [PROBLEM] event at start,
    however many passes run, and the mail is delivered."""
    r = rig
    r.release()
    r.i["hold_cc_without_ack"] = "true"
    append(r.i["log"], message("msg/after"))
    good = []

    def on_sleep(s):
        if s == 1:
            good.append(s)
            if len(good) >= 3:                              # three good passes
                raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(r.i)
    assert kinds(r) == ["channel_open", "channel_error", "message"], kinds(r)
    assert r.pushed[1][0] == ("[PROBLEM] MeshyChat channel for seat-prod: hold_cc_without_ack='true' is "
                              "not true or false; nothing is held.")
    assert r.pushed[2][1]["key"] == "msg/after"


def test_a_failed_setting_report_does_not_end_the_thread(channel, rig, monkeypatch, capsys):
    """#199 follow-up (Codex r1 on 4393f2c): the setting's report runs before the guarded loop. When that write fails,
    the thread goes on: the mail after it is still delivered."""
    r = rig
    r.release()
    r.i["hold_cc_without_ack"] = 1
    append(r.i["log"], message("msg/after"))
    real_send, calls = channel.send, []

    def send(obj):                                          # the open event goes out; the setting's report fails once
        calls.append(obj)
        if len(calls) == 2:
            raise OSError(32, "Broken pipe")
        real_send(obj)
    monkeypatch.setattr(channel, "send", send)

    def on_sleep(s):
        if s == 1:
            raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):                               # not OSError: the thread did not die
        channel.tail(r.i)
    assert kinds(r) == ["channel_open", "message"], kinds(r)
    assert r.pushed[1][1]["key"] == "msg/after"
    assert capsys.readouterr().err == ("meshy channel: seat-prod: hold_cc_without_ack=1 is not true or false; "
                                      "nothing is held\n")


def test_a_failed_setting_report_and_stderr_do_not_end_the_thread(channel, rig, monkeypatch):
    """P3 follow-up (Codex r1 on a05d949): the channel's report falls back to stderr; when stderr is gone too,
    hold_options' guard takes it, and the mail after it is still delivered."""
    r = rig
    r.release()
    r.i["hold_cc_without_ack"] = 1
    append(r.i["log"], message("msg/after"))
    real_send, calls = channel.send, []

    def send(obj):                                          # the open event goes out; the setting's report fails once
        calls.append(obj)
        if len(calls) == 2:
            raise OSError(32, "Broken pipe")
        real_send(obj)
    monkeypatch.setattr(channel, "send", send)

    class Gone:
        def write(self, text):
            raise OSError(9, "Bad file descriptor")
    monkeypatch.setattr(channel.sys, "stderr", Gone())

    def on_sleep(s):
        if s == 1:
            raise Stop()
    r.on_sleep = on_sleep
    with pytest.raises(Stop):                               # not OSError: the thread did not die
        channel.tail(r.i)
    assert kinds(r) == ["channel_open", "message"], kinds(r)
    assert r.pushed[1][1]["key"] == "msg/after"


def test_channel_reloads_hold_cc_without_ack_when_config_changes(channel, rig, tmp_path, monkeypatch):
    """#199 HQB operational fix: a running `meshy_channel.py` session started before `hold_cc_without_ack` was edited
    in `config.json` reloads `hold_options` when `CONFIG` changes on disk (without re-reading on unchanged passes),
    holds `cc` messages with `requires_ack=False` once enabled, and releases them if `hold_cc_without_ack` is turned
    back off."""
    import json
    from tests.test_postbox_delivery_bank import copy_message

    r = rig
    r.release()
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(channel, "HERE", str(tmp_path))

    def write_cfg(hold_val, pad=""):
        entry = {"name": r.i["name"], "log": r.i["log"]}
        if hold_val is not None:
            entry["hold_cc_without_ack"] = hold_val
        cfg_file.write_text(json.dumps({"base_url": "http://x", "identities": [entry]}) + pad, encoding="utf-8")

    # Pass 1: started with hold_cc_without_ack absent -> copy_message("msg/cc-1") is delivered immediately.
    write_cfg(None)
    append(r.i["log"], copy_message("msg/cc-1"))
    passes = {"n": 0, "after_pass_2": None}

    def on_sleep(s):
        if s != 1:
            return
        passes["n"] += 1
        if passes["n"] == 1:
            # Turn hold_cc_without_ack=True on in config.json before Pass 2, and append msg/cc-2.
            write_cfg(True, pad="\n")
            append(r.i["log"], copy_message("msg/cc-2"))
        elif passes["n"] == 2:
            # Snapshot pushed keys after Pass 2 (proving msg/cc-2 was held, not delivered on Pass 2),
            # then turn hold_cc_without_ack=False in config.json before Pass 3 -> msg/cc-2 is released!
            passes["after_pass_2"] = [m.get("key") for _c, m in r.pushed if m.get("kind") == "message"]
            write_cfg(False, pad="  \n")
        elif passes["n"] >= 3:
            raise Stop()

    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(r.i)

    # Pass 1 delivered msg/cc-1; Pass 2 held msg/cc-2 (still only msg/cc-1 pushed); Pass 3 released msg/cc-2.
    assert passes["after_pass_2"] == ["msg/cc-1"]
    msg_keys = [m.get("key") for _c, m in r.pushed if m.get("kind") == "message"]
    assert msg_keys == ["msg/cc-1", "msg/cc-2"]


def test_codex_delivery_reloads_hold_cc_without_ack_when_config_changes(tmp_path, monkeypatch):
    """#199 HQB operational fix: `postbox.run_codex_delivery` also reloads `hold_options` when `CONFIG` changes on disk,
    holding `msg/cc-2` on pass 2 when `hold_cc_without_ack=True` is written and releasing it on pass 3 when turned off."""
    import importlib.util
    import json
    import subprocess
    from tests.test_postbox_delivery_bank import PB, copy_message
    from tests.test_postbox_reconcile import ident

    spec = importlib.util.spec_from_file_location("postbox_codex_reload_under_test", PB / "postbox.py")
    pb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pb)
    monkeypatch.setattr(pb, "STATUS", str(tmp_path / "status.json"))
    monkeypatch.setattr(pb, "ERR", str(tmp_path / "postbox.err.log"))
    monkeypatch.setattr(pb, "graphline", None)
    monkeypatch.setattr(pb, "owedline", None)

    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(pb, "CONFIG", str(cfg_file))

    i = ident(tmp_path, target="fixture-session", codex_settle_s=0, codex_min_gap_s=60)
    offset = i["log"] + ".codex.offset"
    (tmp_path / "seat.log.codex.offset").write_text("0", encoding="utf-8")

    def write_cfg(hold_val, pad=""):
        entry = {"name": i["name"], "log": i["log"]}
        if hold_val is not None:
            entry["hold_cc_without_ack"] = hold_val
        cfg_file.write_text(json.dumps({"base_url": "http://x", "identities": [entry]}) + pad, encoding="utf-8")

    write_cfg(None)
    append(i["log"], copy_message("msg/cc-1"))
    queued = []
    state = {"polls": 0, "after_pass_2": None}

    def fake_queue(argv, **_kw):
        queued.append(argv[argv.index("--message") + 1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def fake_sleep(seconds):
        if seconds != 2:
            return
        state["polls"] += 1
        if state["polls"] == 2:
            # Pass 1 just finished; turn hold_cc_without_ack=True on in config.json before Pass 2 and append msg/cc-2.
            write_cfg(True, pad="\n")
            append(i["log"], copy_message("msg/cc-2"))
        elif state["polls"] == 3:
            # Pass 2 just finished; snapshot queued count (must still be 1 because msg/cc-2 was held), then turn off.
            state["after_pass_2"] = list(queued)
            write_cfg(False, pad="  \n")
        elif state["polls"] >= 4:
            raise Stop()

    monkeypatch.setattr(pb.subprocess, "run", fake_queue)
    monkeypatch.setattr(pb.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)

    assert len(state["after_pass_2"]) == 1 and "msg/cc-1" in state["after_pass_2"][0]
    assert len(queued) == 2 and "msg/cc-2" in queued[1]


def test_channel_reloads_when_config_changes_between_load_identities_and_tail(channel, rig, tmp_path, monkeypatch):
    """Codex inline P2 on #345: if `config.json` changes after `load_identities()` reads it (e.g. while waiting for the
    MCP handshake or channel lock) and before `tail(ident)` enters its loop, `ident['_config_stat']` preserves the
    signature from `load_identities()` so Pass 1 immediately reloads the updated `hold_cc_without_ack`."""
    import json
    from tests.test_postbox_delivery_bank import copy_message

    r = rig
    r.release()
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(channel, "HERE", str(tmp_path))
    monkeypatch.setattr(channel, "IDENTITY", r.i["name"])

    def write_cfg(hold_val, pad=""):
        entry = {
            "name": r.i["name"],
            "log": r.i["log"],
            "client": r.i["client"],
            "python": r.i["python"],
            "state": r.i["state"],
        }
        if hold_val is not None:
            entry["hold_cc_without_ack"] = hold_val
        cfg_file.write_text(json.dumps({"base_url": "http://x", "identities": [entry]}) + pad, encoding="utf-8")

    # 1. load_identities() reads hold_cc_without_ack=False
    write_cfg(False)
    found, missing, skipped = channel.load_identities()
    assert len(found) == 1 and not missing and not skipped
    loaded_ident = found[0]
    assert loaded_ident.get("hold_cc_without_ack") is False

    # 2. Before tail(loaded_ident) starts, config.json changes on disk to hold_cc_without_ack=True
    write_cfg(True, pad="\n")
    append(loaded_ident["log"], copy_message("msg/cc-handshake-race"))

    def on_sleep(s):
        if s == 1:
            raise Stop()

    r.on_sleep = on_sleep
    with pytest.raises(Stop):
        channel.tail(loaded_ident)

    # Pass 1 reloaded config.json and held msg/cc-handshake-race (0 messages pushed).
    msg_keys = [m.get("key") for _c, m in r.pushed if m.get("kind") == "message"]
    assert msg_keys == []
    assert loaded_ident.get("hold_cc_without_ack") is True
