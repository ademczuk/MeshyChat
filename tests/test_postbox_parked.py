"""A Codex turn that ends with a question for Bxxxxx is forwarded to MeshyChat once (tools/postbox/parked.py).

a recorded decision on 7 Nov 2026: "we should have meshychat fixed so that any node can be steered without having me to
write my authorisation". On 6 Nov a Codex seat asked him in its own window and nobody saw it for 8.5 hours.

Hardened on 8 Nov 2026 after codex-oxxxxxx-cli's source read (a20d84, F1 to F7, with sixteen expected-red witnesses in
its packet's check_parked.py) and a reviewer's intake (25d3d5). The tests named for a finding are the witnesses'
cases, turned around: each asserts the safe behaviour the witness found missing."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "postbox"))
import parked  # noqa: E402

QUESTION = ("Step 1 is done.\n\nDo you authorize the scoped recovery review: restoration verification, read-only "
            "diagnostics and closure with the collector stopped?")
#: The clock the checks run at: just after the fixture turns (21:41Z), so none of them is MAX_AGE_S old.
NOW = dt.datetime(2026, 11, 6, 21, 45, tzinfo=dt.timezone.utc)
SENT_KEY = "msg/1790000000000-fake-ok"


def _check(*args, **kw):
    kw.setdefault("now", NOW)
    return parked.check(*args, **kw)


def ev(kind, turn, ts="2026-11-06T21:41:00Z", **payload):
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": kind, "turn_id": turn, **payload}})


#: A sender's receipt, as kxxx_send and `meshy_node send` print it.
#: The signed value of an ask to the coordinator, as node.send and node._op_sent print it beside the result.
ASK_VALUE = {"to": ["oxxxxxx"], "type": "question", "requires_ack": True}
RECEIPT = json.dumps({"key": SENT_KEY, "status": "completed", "result_signature_verified": True, "value": ASK_VALUE})


def call(cmd, call_id=None):
    payload = {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": cmd})}
    if call_id:
        payload["call_id"] = call_id
    return json.dumps({"timestamp": "2026-11-06T21:40:30Z", "type": "response_item", "payload": payload})


def call_output(call_id, stdout, exit_code=0):
    """A shell tool call's output: {"output": <stdout>, "metadata": {"exit_code": n}}, as a string."""
    return json.dumps({"timestamp": "2026-11-06T21:40:31Z", "type": "response_item",
                       "payload": {"type": "function_call_output", "call_id": call_id,
                                   "output": json.dumps({"output": stdout, "metadata": {"exit_code": exit_code}})}})


def exec_call(code, call_id=None):
    """A Codex `exec` tool call, the shape this fleet's rollouts hold (custom_tool_call, code as its input)."""
    payload = {"type": "custom_tool_call", "name": "exec", "input": code}
    if call_id:
        payload["call_id"] = call_id
    return json.dumps({"timestamp": "2026-11-06T21:40:30Z", "type": "response_item", "payload": payload})


def exec_output(call_id, stdout, exit_code=0):
    """A Codex exec output as HQD's rollouts hold it (measured 8 Nov): input_text items, the last one the
    exec_command result {chunk_id, wall_time_seconds, exit_code, original_token_count, output}."""
    result = json.dumps({"chunk_id": "c1", "wall_time_seconds": 0.1, "exit_code": exit_code,
                         "original_token_count": 10, "output": stdout})
    return json.dumps({"timestamp": "2026-11-06T21:40:31Z", "type": "response_item",
                       "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                                   "output": [{"type": "input_text", "text": "Script completed\nOutput:\n"},
                                              {"type": "input_text", "text": "{}"},
                                              {"type": "input_text", "text": result}]}})


def write(path, *lines, mode="a"):
    with open(path, mode, encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


def ident(tmp_path):
    return {"name": "codex-oxxxxxx-admin-prod", "log": str(tmp_path / "admin.log"),
            "parked_question": {"to": "oxxxxxx", "sender": ["python", "send.py"], "seat": "codex-oxxxxxx-admin-cli"}}


class Sender:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, argv, to, thread, body, op_id):
        self.calls.append({"argv": argv, "to": to, "thread": thread, "body": body, "op_id": op_id})
        return self.results.pop(0) if self.results else (True, False)


def text_output(call_id, stdout, code=0):
    """A Codex CLI exec_command output as plain text, HQB's shape (a reviewer d29a54)."""
    text = ("Chunk ID: ab12\nWall time: 0.1 seconds\nProcess exited with code %d\nOriginal token count: 9\nOutput:\n%s"
            % (code, stdout))
    return json.dumps({"timestamp": "2026-11-06T21:40:31Z", "type": "response_item",
                       "payload": {"type": "function_call_output", "call_id": call_id, "output": text}})


def multi_exec_output(call_id, *stdouts):
    """A Codex exec output holding one exec_command result per stdout (exec code that ran several)."""
    items = [{"type": "input_text", "text": json.dumps({"chunk_id": "c%d" % n, "exit_code": 0, "output": s})}
             for n, s in enumerate(stdouts)]
    return json.dumps({"timestamp": "2026-11-06T21:40:31Z", "type": "response_item",
                       "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": items}})


def _setup(tmp_path, *lines, coordinator="oxxxxxx"):
    rollout = tmp_path / "rollout.jsonl"
    write(rollout, "", mode="w")
    watch = parked.TurnWatch(str(rollout), from_end=True, coordinator=coordinator)
    ledger = parked.Ledger(str(tmp_path / "l.json"))
    write(rollout, *lines)
    watch.update()
    return watch, ledger


def _asking(turn="t1", ts="2026-11-06T21:41:00Z"):
    return [ev("task_started", turn, ts=ts), ev("task_complete", turn, ts=ts, last_agent_message=QUESTION)]


def test_asks_operator_needs_a_question_and_an_ask_for_a_decision():
    assert parked.asks_operator(QUESTION)
    assert parked.asks_operator("Merged #529. May I deploy HQD now?")
    assert parked.asks_operator("Two options below.\nWhich option do you want?")
    assert not parked.asks_operator("done: merged #529 at 3684e98d.")                # no question
    assert not parked.asks_operator("Why did the gate take 3 hours? Looking into it.")  # a question, but no ask
    assert not parked.asks_operator(None) and not parked.asks_operator("")


def test_a_finished_turn_that_asks_is_forwarded_once_and_remembered(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    write(rollout, ev("task_started", "t0"), ev("task_complete", "t0", last_agent_message="old"), mode="w")
    watch, ledger, send = parked.TurnWatch(str(rollout), from_end=True), parked.Ledger(str(tmp_path / "l.json")), Sender()
    write(rollout, ev("task_started", "t1"), call("git status"), ev("task_complete", "t1", last_agent_message=QUESTION))
    watch.update()
    assert _check(ident(tmp_path), watch, ledger, send=send) == ["t1"]
    c = send.calls[0]
    assert c["to"] == "oxxxxxx" and c["thread"].startswith("parked-codex-oxxxxxx-admin-cli-")
    assert "> Do you authorize the scoped recovery review" in c["body"] and "seat_directive" in c["body"]
    assert c["op_id"].startswith("parked.") and len(c["op_id"]) == len("parked.") + 32
    # once: the same turn seen again (a restart re-reading the file) is not sent again, from the saved ledger
    watch2 = parked.TurnWatch(str(rollout))
    watch2.update()
    assert _check(ident(tmp_path), watch2, parked.Ledger(str(tmp_path / "l.json")), send=send) == []
    assert len(send.calls) == 1


def test_history_already_in_the_rollout_is_never_forwarded(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    write(rollout, ev("task_started", "t1"), ev("task_complete", "t1", last_agent_message=QUESTION), mode="w")
    watch, send = parked.TurnWatch(str(rollout), from_end=True), Sender()
    watch.update()
    assert _check(ident(tmp_path), watch, parked.Ledger(str(tmp_path / "l.json")), send=send) == []
    assert send.calls == []


def test_a_turn_that_asked_on_meshychat_is_not_forwarded_but_a_plain_reply_does_not_count(tmp_path):
    watch, ledger = _setup(tmp_path, ev("task_started", "t1"),
                           call("python codex_send.py --to oxxxxxx --type question --requires-ack --body-file q.txt",
                                "c1"), call_output("c1", RECEIPT),
                           ev("task_complete", "t1", last_agent_message=QUESTION),
                           ev("task_started", "t2"),
                           call("python -m tools.meshy_node send --to pxx --type answer --body-file r.txt", "c2"),
                           call_output("c2", RECEIPT),
                           ev("task_complete", "t2", last_agent_message=QUESTION))
    assert _check(ident(tmp_path), watch, ledger, send=Sender()) == ["t2"]   # t1 asked on the mesh; t2 only replied


def test_f6_an_echoed_example_does_not_hide_a_question_and_a_wrapper_send_is_seen(tmp_path):
    """F6 (witnesses byte-cue-echo-suppresses-real-question, wrapper-question-not-recognized): the byte cues counted an
    echoed `meshy_node --requires-ack` example as a send, and missed a wrapper's real one. A call is parsed and judged
    by one simple command's own argv. MUTANTS: printers counted; flags matched inside a quoted argument."""
    watch, ledger = _setup(tmp_path, ev("task_started", "t1"),
                           call('echo "meshy_node --requires-ack example only"', "c1"), call_output("c1", RECEIPT),
                           ev("task_complete", "t1", last_agent_message=QUESTION),
                           ev("task_started", "t2"),
                           call("python /SYNTHETIC/send_exact.py --to oxxxxxx --type question --requires-ack "
                                "--body-file q", "c2"),
                           call_output("c2", RECEIPT),
                           ev("task_complete", "t2", last_agent_message=QUESTION))
    assert _check(ident(tmp_path), watch, ledger, send=Sender()) == ["t1"], "the echo hid nothing; t2 had asked"


SEND = "python /SYNTHETIC/send.py --to oxxxxxx --type question --requires-ack --body-file q"
EXEC_SEND = 'text(await tools.exec_command({cmd: "python C:/w/send.py --to oxxxxxx --type question --requires-ack"}))'
OP_RECEIPT = json.dumps({"key": SENT_KEY, "status": "completed", "op_id": "op-1", "value": ASK_VALUE})
NOTE = "python -m tools.meshy_node send --to oxxxxxx --thread t --body '--requires-ack'"


def _receipt(**value):
    return json.dumps({"key": SENT_KEY, "status": "completed", "value": value})


@pytest.mark.parametrize("lines, asked", [
    ([call(SEND, "c1")], False),
    ([call(SEND, "c1"), call_output("c1", '{"refused": "outbound_guard", "sent": false}', 2)], False),
    ([call(SEND, "c1"), call_output("c1", RECEIPT)], True),
    ([call(SEND, "c1"), call_output("c9", RECEIPT)], False),
    ([call(SEND, "c1"), call_output("c1", RECEIPT.replace('"completed"', '"completed", "ok": false'))], False),
    ([call(SEND, "c1"), call_output("c1", RECEIPT, exit_code=1)], False),
    ([call(SEND, "c1"), call_output("c1", RECEIPT, exit_code=False)], False),
    ([call(SEND), call_output("c1", RECEIPT)], False),
    ([call("python /SYNTHETIC/send.py --to oxxxxxx --requires-ack --dry-run", "c1"), call_output("c1", RECEIPT)], False),
    ([exec_call(EXEC_SEND, "x1"), exec_output("x1", RECEIPT)], True),
    ([exec_call(EXEC_SEND, "x1"), exec_output("x1", RECEIPT, exit_code=1)], False),
    ([exec_call("// " + EXEC_SEND, "x1"), exec_output("x1", RECEIPT)], False),
    ([exec_call("/* " + EXEC_SEND + " */", "x1"), exec_output("x1", RECEIPT)], False),
    # pxx 6ca3d4 R2-1: only the sender's ONE answer, parsed strictly, from a known wrapper with exit code 0
    ([call(SEND, "c1"), call_output("c1", "Example only: " + RECEIPT)], False),
    ([call(SEND, "c1"), call_output("c1", RECEIPT + "\n" + RECEIPT)], False),
    ([call(SEND + "; cat receipt.json", "c1"), call_output("c1", RECEIPT)], False),
    ([call(SEND, "c1"), call("cat receipt.json", "c1"), call_output("c1", RECEIPT)], False),
    ([call(SEND, "c1"), call_output("c1", RECEIPT.replace('"completed"', '"completed", "exit_code": 1'))], False),
    ([call(SEND, "c1"), text_output("c1", RECEIPT)], True),
    ([call(SEND, "c1"), text_output("c1", RECEIPT, code=2)], False),
    ([call(SEND, "c1"), text_output("c1", "Example only: " + RECEIPT)], False),
    ([exec_call(EXEC_SEND, "x1"), multi_exec_output("x1", RECEIPT, RECEIPT)], False),
    # pxx 6ca3d4 R2-2: bound to the coordinator and to the op
    ([call("python /SYNTHETIC/send.py --to elsewhere --requires-ack --body-file q", "c1"),
      call_output("c1", RECEIPT)], False),
    ([call("python /SYNTHETIC/send.py --to pxx --cc oxxxxxx --requires-ack --body-file q", "c1"),
      call_output("c1", RECEIPT)], True),
    ([call("python /SYNTHETIC/send.py --to=pxx,oxxxxxx --requires-ack --body-file q", "c1"),
      call_output("c1", RECEIPT)], True),
    ([call(SEND + " --op-id op-2", "c1"), call_output("c1", OP_RECEIPT)], False),
    ([call(SEND + " --op-id op-1", "c1"), call_output("c1", OP_RECEIPT)], True),
    ([call(SEND, "c1"), call_output("c1", json.dumps({"key": SENT_KEY, "status": "completed",
                                                        "value": {"to": ["elsewhere"]}}))], False),
    ([call(SEND, "c1"), call_output("c1", json.dumps({"key": SENT_KEY, "status": "completed",
                                                        "value": {"to": ["pxx"], "cc": ["oxxxxxx"],
                                                                  "requires_ack": True}}))], True),
    # pxx 216b4e R3-2: argv only picks the candidate; the signed value decides
    ([call(NOTE, "c1"), call_output("c1", _receipt(to=["oxxxxxx"], type="note"))], False),
    ([call("python /SYNTHETIC/send.py --to elsewhere --body --to=oxxxxxx --requires-ack", "c1"),
      call_output("c1", json.dumps({"key": SENT_KEY, "status": "completed"}))], False),
    ([call(SEND, "c1"), call_output("c1", json.dumps({"key": SENT_KEY, "status": "completed"}))], False),
    ([call(SEND, "c1"), call_output("c1", _receipt(to=["oxxxxxx"], type="note"))], False),
    ([call(SEND, "c1"), call_output("c1", _receipt(to=["oxxxxxx"], type="question"))], True),
    ([call(SEND, "c1"), call_output("c1", _receipt(to=["oxxxxxx"], type="request", requires_ack=True))], True),
    ([call(SEND, "c1"), call_output("c1", _receipt(to="oxxxxxx", type="question"))], False),
    ([call(SEND.replace("oxxxxxx", "OxxxXxx"), "c1"), call_output("c1", _receipt(to=["OxxxXxx"], type="question"))],
     False),
])
def test_f6_a_turn_asked_only_when_its_sends_own_output_holds_a_receipt(tmp_path, lines, asked):
    """pxx 2b3dc4 B (P1): a send that was refused, failed, never answered, commented out or a dry run counted as
    asked, so the turn's question ended silently. pxx 6ca3d4 R2-1 and R2-2 (P1): a receipt quoted in other text, one
    of two, one from a compound command or a reused call id, and one for a send to someone else or for another op all
    counted too. The turn counts as asked only when bound_receipt holds. MUTANTS: any send-like call counts; the output
    scanned rather than parsed; the coordinator, the op or the exit code not checked; a compound command bound."""
    watch, ledger = _setup(tmp_path, ev("task_started", "t1"), *lines,
                           ev("task_complete", "t1", last_agent_message=QUESTION))
    assert _check(ident(tmp_path), watch, ledger, send=Sender()) == ([] if asked else ["t1"])


def test_f6_a_watch_without_a_coordinator_binds_no_receipt(tmp_path):
    """No configured coordinator, nothing a receipt could be for: every asking turn is forwarded."""
    watch, ledger = _setup(tmp_path, ev("task_started", "t1"), call(SEND, "c1"), call_output("c1", RECEIPT),
                           ev("task_complete", "t1", last_agent_message=QUESTION), coordinator=None)
    assert _check(ident(tmp_path), watch, ledger, send=Sender()) == ["t1"]


def test_f6_exec_code_in_a_comment_or_a_string_is_no_call():
    """pxx 2b3dc4 B: `// tools.exec_command(...)` counted as a call. Comments and string contents are left out."""
    assert parked.tool_commands({"type": "custom_tool_call", "input": "// " + EXEC_SEND}) == []
    named = 'const s = `exec_command({cmd: "python send.py --requires-ack"})`; text(await tools.exec_command({cmd: "ls"}))'
    assert parked.tool_commands({"type": "custom_tool_call", "input": named}) == ["ls"]


@pytest.mark.parametrize("cmd, asks", [
    ('echo "meshy_node --requires-ack example only"', False),
    ("echo --requires-ack", False),
    ("printf '%s' --type question", False),
    ('Write-Host "--requires-ack"', False),
    ("grep -- --requires-ack notes.txt", False),
    ("python send.py --to oxxxxxx --type question --body-file q", True),
    ("python send.py --to oxxxxxx --type=question --body-file q", True),
    ("cd C:/x && python send.py --requires-ack --body-file q", True),
    ('& "C:/py/python.exe" "C:/w/codex_kxxx_cli_send.py" --to kxxx --type request --requires-ack', True),
    ("FOO=1 python send.py --requires-ack", True),
    ("python send.py --type answer --body-file r", False),
    ('python -c "print(\'--requires-ack\')"', False),
    ('python send.py --requires-ack "unclosed', False),
    (["bash", "-lc", "python send.py --requires-ack"], True),
    (["bash", "-lc", "echo --requires-ack"], False),
    (["python", "send.py", "--type", "question"], True),
    # pxx c4c93c: a heredoc's body and a comment are text, never commands
    ("cat > b.txt <<'EOF'\nPlease reply; I sent this with --requires-ack so it is owed.\nEOF\n"
     "python send.py --to oxxxxxx --body-file b.txt", False),
    ("# later: python -m tools.meshy_node send --requires-ack --to oxxxxxx\nls -la", False),
    ("cat <<'EOF' > s.sh\npython send.py --requires-ack\nEOF", False),
    ("cat <<-EOF\n\tpython send.py --requires-ack\n\tEOF\nls", False),
    ("cat <<-EOF\n\tbody\n\tEOF\npython send.py --requires-ack", True),
    ("# done; python send.py --requires-ack", False),
    ("cat <<EOF | python send.py --requires-ack --body-file -\nthe body\nEOF", True),
    ("cat > b.txt <<'EOF'\nbody\nEOF\npython send.py --requires-ack --body-file b.txt", True),
    ("$b = @'\nI used --requires-ack here\n'@\nls", False),
    ("$b = @'\nbody\n'@\n& python send.py --requires-ack --body $b", True),
    ("python send.py --requires-ack --body-file q  # owed", True),
    ('python send.py --to "a#b" --requires-ack', True),
    ("echo x#y && python send.py --requires-ack", True),
    # pxx c4c93c INFO: a wrapper around a printer is no sender
    ("env printf --requires-ack", False),
    ("xargs echo --requires-ack", False),
    ("nice -n 5 echo --requires-ack", False),
    # only a Python interpreter running a script or a module sends; its own options are not the script's
    ("node send.js --requires-ack", False),
    ("cat send.py --requires-ack", False),
    ("py -3 send.py --requires-ack", True),
    ("python -X utf8 -m tools.meshy_node send --to oxxxxxx --requires-ack", True),
    ("python --requires-ack send.py --to oxxxxxx", False),
    ('bash -lc "python send.py --requires-ack"', True),
    ('pwsh -NoProfile -Command "python send.py --type question"', True),
    ("python send.py --to oxxxxxx --expects-reply --body-file q", True),
    ("python send.py --requires-ack --help", False),
    ("python send.py --requires-ack --dry-run", False),
    ('python -c "pass" --requires-ack', False),
    ("true <<< x --requires-ack", False),
    ("C:\\py\\python.exe C:\\w\\send.py --requires-ack", True),
    # a reviewer d29a54 P2: an escaped quote inside a shell's script stays an escape
    ('bash -lc "python send.py --body \\"a b\\" --requires-ack"', True),
])
def test_f6_a_send_is_one_simple_commands_own_flags(cmd, asks):
    """F6, and pxx c4c93c (P2): heredoc bodies and comments were parsed as commands, so a line of text naming the flag
    suppressed a real question. Only a Python sender's own arguments count now. MUTANTS: any program counts; heredoc,
    here-string or comment text read as commands; the interpreter's options read as the script's."""
    assert parked.asks_on_mesh(cmd) is asks, cmd


def test_f6_codex_exec_code_is_read_for_its_commands(tmp_path):
    """This fleet's real rollouts hold `custom_tool_call` exec code: `text(await tools.exec_command({cmd: "..."}))`
    (measured on HQD 8 Nov: 441 such calls in one rollout, none a function_call shell). MUTANT: exec code not read."""
    asked = 'text(await tools.exec_command({cmd: "python C:/w/send.py --to oxxxxxx --type question --requires-ack", ' \
            'workdir: "C:/x"}))'
    echoed = "text(await tools.exec_command({cmd: 'echo \"--requires-ack\"'}))"
    assert parked.tool_commands({"type": "custom_tool_call", "input": asked}) == [
        "python C:/w/send.py --to oxxxxxx --type question --requires-ack"]
    assert parked.tool_commands({"type": "custom_tool_call", "input": "const r = await tools.exec_command({cmd: `ls`})"}) \
        == ["ls"]
    watch, ledger = _setup(tmp_path, ev("task_started", "t1"), exec_call(asked, "x1"), exec_output("x1", RECEIPT),
                           ev("task_complete", "t1", last_agent_message=QUESTION),
                           ev("task_started", "t2"), exec_call(echoed, "x2"), exec_output("x2", RECEIPT),
                           ev("task_complete", "t2", last_agent_message=QUESTION))
    assert _check(ident(tmp_path), watch, ledger, send=Sender()) == ["t2"]


def test_a_refused_text_sends_a_pointer_without_the_quote(tmp_path):
    watch, ledger = _setup(tmp_path, *_asking())
    send = Sender((False, True), (True, False))
    assert _check(ident(tmp_path), watch, ledger, send=send) == ["t1"]
    assert len(send.calls) == 2 and "Do you authorize" not in send.calls[1]["body"]
    assert "refused by the outbound gate" in send.calls[1]["body"] and send.calls[1]["op_id"].endswith(".pointer")
    assert ledger.rows["t1"]["state"] == "sent-pointer"


def test_f2_an_unknown_outcome_is_held_never_resent_and_only_its_status_settles_it(tmp_path):
    """F2 (witness unknown-outcome-automatic-retry): a failed send was re-queued and sent again, with nothing proving
    the first was not accepted. The intent is now in the ledger before the send; an UNKNOWN outcome is held there and
    never sent again here; the identity's op status settles it. MUTANTS: the held turn re-queued; no intent written
    before the send; a status asked more often than RECONCILE_EVERY_S."""
    watch, ledger = _setup(tmp_path, *_asking())
    seen_before = []
    send = Sender((False, False, "TimeoutExpired"))
    real = send.__call__

    def spy(argv, to, thread, body, op_id):
        seen_before.append(dict(parked.Ledger(str(tmp_path / "l.json")).rows))   # what the disk held at send time
        return real(argv, to, thread, body, op_id)
    reports = []
    assert _check(ident(tmp_path), watch, ledger, send=spy, report=lambda *r: reports.append(r)) == []
    op_id = send.calls[0]["op_id"]
    assert seen_before[0]["t1"]["state"] == "sending" and seen_before[0]["t1"]["op_id"] == op_id
    row = ledger.rows["t1"]
    assert row["state"] == "unknown" and row["body_sha256"] == hashlib.sha256(send.calls[0]["body"].encode()).hexdigest()
    assert reports == [("t1", "held: TimeoutExpired; not sent again until its status says so (%s)" % op_id, 1)]
    assert _check(ident(tmp_path), watch, ledger, send=spy) == [] and len(send.calls) == 1, "no status: held"
    asked = []
    status = lambda op: asked.append(op) or (parked.SENT, SENT_KEY, SENT_KEY)         # noqa: E731
    soon = NOW + dt.timedelta(seconds=parked.RECONCILE_EVERY_S - 1)
    assert _check(ident(tmp_path), watch, ledger, send=spy, status=status, now=soon) == [] and asked == []
    later = NOW + dt.timedelta(seconds=parked.RECONCILE_EVERY_S + 1)
    assert _check(ident(tmp_path), watch, ledger, send=spy, status=status, now=later) == ["t1"]
    assert asked == [op_id] and len(send.calls) == 1 and ledger.rows["t1"]["state"] == "sent"
    assert ledger.rows["t1"]["key"] == SENT_KEY


def test_f2_a_status_that_says_the_guard_held_it_sends_the_pointer_and_others_keep_it_held(tmp_path):
    """The client's op status `held-by-guard` (refused by its send guard, never sent) sends the pointer under its own
    op id and the turn's thread; `refused-by-server` is final and reported; anything else stays held. MUTANTS: the
    pointer sent for an unknown status; the held row's thread not kept."""
    watch, ledger = _setup(tmp_path, *_asking("t1"), *_asking("t2", ts="2026-11-06T21:42:00Z"),
                           *_asking("t3", ts="2026-11-06T21:43:00Z"))
    send = Sender((False, False, "x"), (False, False, "y"), (False, False, "z"))
    for _ in range(3):
        _check(ident(tmp_path), watch, ledger, send=send)
    assert {t: ledger.rows[t]["state"] for t in ("t1", "t2", "t3")} == {"t1": "unknown", "t2": "unknown", "t3": "unknown"}
    ops = {t: ledger.rows[t]["op_id"] for t in ("t1", "t2", "t3")}
    answers = {ops["t1"]: (parked.OP_HELD_BY_GUARD, None, "held"), ops["t2"]: ("refused-by-server", None, "refused"),
               ops["t3"]: (parked.UNKNOWN, None, "status 'uncertain'")}
    reports, later = [], NOW + dt.timedelta(hours=1)
    got = _check(ident(tmp_path), watch, ledger, send=send, status=lambda op: answers[op], now=later,
                 report=lambda *r: reports.append(r))
    assert got == ["t1"] and send.calls[-1]["op_id"] == ops["t1"] + ".pointer"
    assert send.calls[-1]["thread"] == ledger.rows["t1"]["thread"] and "isn't quoted" in send.calls[-1]["body"]
    assert ledger.rows["t1"]["state"] == "sent-pointer" and ledger.rows["t2"]["state"] == "refused-by-server"
    assert ledger.rows["t3"]["state"] == "unknown" and len(send.calls) == 4
    assert [r[0] for r in reports] == ["t2", "t3"]


def test_f2_a_held_turn_does_not_hold_the_turns_after_it(tmp_path):
    """pxx 8fc6a2's case under F2: two asking turns and a sender whose outcome is unknown. The first is held in the
    ledger; the second waits for the next check and is sent then. MUTANT: the held turn re-queued; it is sent twice."""
    watch, ledger = _setup(tmp_path, *_asking("t1"), *_asking("t2"))
    send = Sender((False, False, "HTTP 503"))
    assert _check(ident(tmp_path), watch, ledger, send=send) == []
    assert [t["turn_id"] for t in watch._done] == ["t2"]
    assert _check(ident(tmp_path), watch, ledger, send=send) == ["t2"]
    assert [c["op_id"] for c in send.calls].count(send.calls[0]["op_id"]) == 1 and ledger.rows["t1"]["state"] == "unknown"


def test_a_turn_sent_after_midnight_keeps_the_thread_of_its_day(tmp_path):
    """codex-kxxx-cli e42fc2: the thread carries the turn's own UTC day, never the check's."""
    watch, ledger = _setup(tmp_path, *_asking(ts="2026-11-06T23:59:00Z"))
    send = Sender()
    assert _check(ident(tmp_path), watch, ledger, send=send,
                  now=dt.datetime(2026, 11, 7, 0, 0, 30, tzinfo=dt.timezone.utc)) == ["t1"]
    assert send.calls[0]["thread"] == "parked-codex-oxxxxxx-admin-cli-20261106"


def test_it_runs_only_when_configured_with_a_coordinator_and_a_sender_and_not_disabled():
    """`"enabled": false` disables it (a reviewer 25d3d5: enabled() ignored it). MUTANT: the flag ignored."""
    assert parked.enabled({"parked_question": {"to": "kxxx", "sender": ["python", "s.py"]}})
    assert parked.enabled({"parked_question": {"to": "kxxx", "sender": ["s"], "enabled": True}})
    for cfg in (None, {}, {"to": "kxxx"}, {"sender": ["s"]}, {"to": "", "sender": ["s"]}, {"to": "kxxx", "sender": []},
                {"to": "kxxx", "sender": ["s"], "enabled": False}, {"to": "kxxx", "sender": ["s"], "enabled": "false"},
                {"to": "kxxx", "sender": ["s"], "enabled": 0}):
        assert not parked.enabled({} if cfg is None else {"parked_question": cfg}), cfg


def test_only_the_last_message_is_quoted_and_capped():
    long = "x" * 5000 + "\nMay I deploy now?"
    body = parked.body_for("seat", {"turn_id": "t1", "at": "2026-11-06T21:41:00Z", "message": long})
    assert body.count("x") <= parked.QUOTE_CHARS + 10 and "May I deploy now?" in body and "21:41Z" in body


def test_a_question_that_ended_while_nothing_watched_is_still_forwarded_once(tmp_path):
    """pxx 43d250 P3 1: a start reads back BACKLOG_BYTES, from the next line start; the ledger skips what went
    before. MUTANT: start at the end (backlog 0); nothing is sent."""
    rollout, ledger = tmp_path / "rollout.jsonl", parked.Ledger(str(tmp_path / "l.json"))
    write(rollout, "x" * 300, ev("task_started", "t1"), ev("task_complete", "t1", last_agent_message=QUESTION),
          mode="w")                                          # the first line is cut by the backlog's start
    watch, send = parked.TurnWatch(str(rollout), from_end=True, backlog=200 + len(QUESTION) * 2), Sender()
    watch.update()
    assert _check(ident(tmp_path), watch, ledger, send=send) == ["t1"] and len(send.calls) == 1
    again = parked.TurnWatch(str(rollout), from_end=True, backlog=parked.BACKLOG_BYTES)   # the next restart
    again.update()
    assert _check(ident(tmp_path), again, ledger, send=send) == [] and len(send.calls) == 1, "the ledger has it"


def test_a_read_back_question_older_than_max_age_is_history(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    write(rollout, ev("task_started", "t1"), ev("task_complete", "t1", last_agent_message=QUESTION), mode="w")
    watch, send = parked.TurnWatch(str(rollout), from_end=True, backlog=parked.BACKLOG_BYTES), Sender()
    watch.update()
    late = NOW + dt.timedelta(seconds=parked.MAX_AGE_S + 3600)
    assert _check(ident(tmp_path), watch, parked.Ledger(str(tmp_path / "l.json")), send=send, now=late) == []
    assert send.calls == []


@pytest.mark.parametrize("stamp, why", [("not-a-timestamp", "no valid time"), (None, "no valid time"),
                                        ("2026-11-06T21:41:00", "no valid time"),
                                        ("2099-01-01T00:00:00Z", "ahead of this clock")])
def test_f7_a_turn_without_a_valid_time_or_from_the_future_is_never_forwarded(tmp_path, stamp, why):
    """F7 (witnesses timestamp-malformed/missing/future-forwarded): the age test passed a turn whose time it could
    not read. MUTANTS: a missing time forwarded; a future time forwarded."""
    watch = parked.TurnWatch(str(tmp_path / "none"))
    watch._done = [{"turn_id": "t1", "at": stamp, "message": QUESTION, "mesh_sent": False}]
    send, reports = Sender(), []
    assert _check(ident(tmp_path), watch, parked.Ledger(str(tmp_path / "l.json")), send=send,
                  report=lambda *r: reports.append(r)) == []
    assert send.calls == [] and why in reports[0][1]


def test_f7_the_future_skew_boundary(tmp_path):
    """A turn up to FUTURE_SKEW_S ahead of the clock is forwarded; one past it is not. MUTANT: the skew test off by
    the sign or the bound."""
    for ahead, sent in ((parked.FUTURE_SKEW_S - 1, True), (parked.FUTURE_SKEW_S + 1, False)):
        watch = parked.TurnWatch("unused")
        stamp = (NOW + dt.timedelta(seconds=ahead)).isoformat()
        watch._done = [{"turn_id": "t1", "at": stamp, "message": QUESTION, "mesh_sent": False}]
        led = parked.Ledger(str(tmp_path / ("l%d.json" % ahead)))
        assert (_check({"name": "x", "parked_question": {"to": "m", "sender": ["s"], "seat": "x"}}, watch, led,
                       send=Sender()) == ["t1"]) is sent, ahead


def test_a_replaced_rollout_is_read_back_like_a_start_never_from_zero(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    write(rollout, ev("task_started", "t9"), ev("task_complete", "t9", last_agent_message="fine"), mode="w")
    watch, send = parked.TurnWatch(str(rollout), from_end=True), Sender()
    rollout.unlink()
    write(rollout, ev("task_started", "t0"), ev("task_complete", "t0", last_agent_message=QUESTION), mode="w")
    watch.update()
    assert _check(ident(tmp_path), watch, parked.Ledger(str(tmp_path / "l.json")), send=send) == []
    assert send.calls == []


def test_f3_a_corrupt_ledger_stops_every_send_and_is_never_written_over(tmp_path):
    """F3 (witness corrupt-ledger-resets-and-sends): an unreadable ledger was taken as empty, so every eligible turn
    went out again. pxx 2b3dc4 C (P1): {"t1": {}} loaded clean, so t1 read as sent and was skipped silently; a row
    needs a known state and its op id. MUTANTS: a corrupt ledger read as empty; rows not checked."""
    for n, content in enumerate((b"{broken", b"[1, 2]", b'{"t0": "not a row"}', b'{"t1": {}}',
                                 b'{"t1": {"state": "weird", "op_id": "x"}}', b'{"t1": {"state": "unknown"}}',
                                 b'{"t1": {"state": "unknown-pointer", "op_id": "x"}}')):
        path = tmp_path / "l.json"
        path.write_bytes(content)
        sub = tmp_path / ("w%d" % n)
        sub.mkdir()
        watch, _ = _setup(sub, *_asking())
        send, reports = Sender(), []
        ledger = parked.Ledger(str(path))
        assert ledger.corrupt
        assert _check(ident(tmp_path), watch, ledger, send=send, report=lambda *r: reports.append(r)) == []
        assert send.calls == [] and path.read_bytes() == content and "nothing is sent" in reports[0][1]
        with pytest.raises(parked.LedgerCorrupt):
            ledger.put("t1", {"state": "sent"})


def test_f3_rows_are_pruned_by_age_only_never_a_held_or_young_one(tmp_path):
    """F3 (witness ledger-cap-forgets-forwarded-turn): the 200-row cap dropped a turn still inside the read-back
    window, which was then forwarded again. MUTANTS: a count cap; a held row pruned."""
    ledger = parked.Ledger(str(tmp_path / "l.json"))
    ledger.now = NOW
    old = (NOW - dt.timedelta(seconds=parked.LEDGER_RETAIN_S + 60)).isoformat()
    young = (NOW - dt.timedelta(hours=1)).isoformat()
    ledger.rows = {"T%03d" % i: {"at": "%04d" % i, "state": "sent"} for i in range(300)}   # times it cannot read
    ledger.rows.update({"old-sent": {"at": old, "state": "sent"}, "old-held": {"at": old, "state": "unknown"},
                        "young": {"at": young, "state": "sent"}})
    ledger.put("new", {"at": young, "state": "sent"})
    assert "T000" in ledger.rows and "young" in ledger.rows and "old-held" in ledger.rows
    assert "old-sent" not in ledger.rows, "a final row past LEDGER_RETAIN_S goes"
    watch = parked.TurnWatch("unused")
    watch._done = [{"turn_id": "T000", "at": "2026-11-06T21:41:00Z", "message": QUESTION, "mesh_sent": False}]
    send = Sender()
    assert _check(ident(tmp_path), watch, ledger, send=send) == [] and send.calls == []


def test_f3_past_its_row_limit_the_ledger_takes_nothing_and_nothing_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(parked, "LEDGER_MAX_ROWS", 3)
    ledger = parked.Ledger(str(tmp_path / "l.json"))
    ledger.rows = {"a": {"state": "unknown"}, "b": {"state": "unknown"}, "c": {"state": "unknown"}}
    watch, _ = _setup(tmp_path, *_asking())
    send, reports = Sender(), []
    assert _check(ident(tmp_path), watch, ledger, send=send, report=lambda *r: reports.append(r)) == []
    assert send.calls == [] and "could not be recorded" in reports[-1][1]


def test_f3_every_write_is_fsynced_and_owner_only(tmp_path, monkeypatch):
    """MUTANTS: no fsync; the file created readable by others (the mode is read on every platform, since Windows
    ignores it on disk); a fixed-name .tmp reused (codex-pxx-cli F: an old 0644 one kept its mode and was promoted)."""
    synced, modes = [], []
    real, real_open = os.fsync, os.open
    stale = tmp_path / "l.json.tmp"
    stale.write_text("old")
    monkeypatch.setattr(parked.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    monkeypatch.setattr(parked.os, "open", lambda path, flags, mode=0o777: modes.append(mode) or real_open(path, flags, mode))
    ledger = parked.Ledger(str(tmp_path / "l.json"))
    ledger.now = NOW                                                     # the row is not past LEDGER_RETAIN_S
    ledger.put("t1", {"at": "2026-11-06T21:41:00Z", "state": "sent", "op_id": "parked.x"})
    assert synced and json.loads((tmp_path / "l.json").read_text())["t1"]["state"] == "sent"
    assert modes == [0o600] and stale.read_text() == "old", "a fresh temporary file, never the stale one"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["l.json", "l.json.tmp"]
    if os.name != "nt":                                         # the file on disk too, where the mode means something
        assert (os.stat(tmp_path / "l.json").st_mode & 0o777) == 0o600


def test_f3_a_failed_write_leaves_no_row_and_the_turn_is_tried_again(tmp_path, monkeypatch):
    """GitHub Codex on #549: a failed intent write left the `sending` row in memory, so the turn was skipped and later
    held as a phantom. The rows change only once the file has, and the temporary file is removed. MUTANT: the row
    set before the write."""
    watch, ledger = _setup(tmp_path, *_asking())
    real = parked.os.replace

    def broken(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(parked.os, "replace", broken)
    send, reports = Sender(), []
    assert _check(ident(tmp_path), watch, ledger, send=send, report=lambda *r: reports.append(r)) == []
    assert send.calls == [] and "t1" not in ledger.rows and "could not be recorded" in reports[-1][1]
    assert [t["turn_id"] for t in watch._done] == ["t1"]
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == [], "the temporary file is removed"
    monkeypatch.setattr(parked.os, "replace", real)
    assert _check(ident(tmp_path), watch, ledger, send=send) == ["t1"] and len(send.calls) == 1


def test_f2_a_write_that_fails_after_the_send_keeps_the_later_turns(tmp_path):
    """a reviewer 7cb004 P2-3: an OSError writing a send's outcome raised out of check() with the rest of the batch
    already taken from the watch. The intent stays held for its status and the later turns go back. MUTANT: the
    post-send write unguarded."""
    watch, ledger = _setup(tmp_path, *_asking("t1"), *_asking("t2"))
    real = ledger.put

    def put(tid, row):
        if tid == "t1" and row["state"] == "sent":
            raise OSError("disk full")
        return real(tid, row)
    ledger.put = put
    reports = []
    assert _check(ident(tmp_path), watch, ledger, send=Sender(), report=lambda *r: reports.append(r)) == []
    assert ledger.rows["t1"]["state"] == "sending" and [t["turn_id"] for t in watch._done] == ["t2"]
    assert "could not record the outcome" in reports[-1][1]
    assert _check(ident(tmp_path), watch, ledger, send=Sender()) == ["t2"]


def test_f2_a_pointer_refused_too_is_final_and_reported(tmp_path):
    """a reviewer 7cb004 P2-3: a refused pointer stayed held and was asked about every 5 minutes for good. Nothing
    shorter is left to send, so it is final, from a send and from a status. MUTANT: the refused pointer held."""
    watch, ledger = _setup(tmp_path, *_asking("t1"))
    send, reports, asked = Sender((False, True), (False, True)), [], []
    assert _check(ident(tmp_path), watch, ledger, send=send, report=lambda *r: reports.append(r)) == []
    assert ledger.rows["t1"]["state"] == "pointer-refused" and "refused too" in reports[-1][1]
    status = lambda op: asked.append(op) or (parked.UNKNOWN, None, "x")      # noqa: E731
    _check(ident(tmp_path), watch, ledger, send=send, status=status, now=NOW + dt.timedelta(hours=1))
    assert asked == [] and len(send.calls) == 2, "final: never asked about again"
    held = parked.Ledger(str(tmp_path / "l2.json"))
    held.put("t2", {"at": "2026-11-06T21:41:00Z", "state": "unknown-pointer", "op_id": "parked.y",
                    "pointer_op_id": "parked.y.pointer", "thread": "parked-x"})
    _check(ident(tmp_path), parked.TurnWatch("unused"), held, send=send,
           status=lambda op: (parked.OP_HELD_BY_GUARD, None, "held"), now=NOW + dt.timedelta(hours=1))
    assert held.rows["t2"]["state"] == "pointer-refused" and len(send.calls) == 2


def test_a_forwarded_turn_does_not_clear_the_failing_status_while_a_row_is_held(tmp_path, monkeypatch):
    """a reviewer 7cb004 P2-1: run() cleared parked_question_failing whenever a turn went out, though a held row
    stayed held. pxx 6ca3d4 R2-5: and a final row never sent (a pointer refused too, a server refusal). It is cleared
    only when no question is open. MUTANTS: cleared on every forward; an unsent final row not counted."""
    who = ident(tmp_path)
    led = Path(who["log"] + ".parked.json")
    monkeypatch.setattr(parked, "check", lambda *a, **k: ["t2"])
    rollout = tmp_path / "rollout.jsonl"
    write(rollout, "", mode="w")
    for state, at, cleared in (("unknown", _ago(hours=1), False), ("pointer-refused", _ago(hours=1), False),
                               ("refused-by-server", _ago(hours=1), False), ("sent", _ago(hours=1), True),
                               ("pointer-refused", _ago(hours=25), True)):
        led.write_text(json.dumps({"t1": {"at": at, "op_id": "parked.x", "state": state}}))
        flags, statuses = iter([False, True]), []
        parked.run(who, rollout_for=lambda _i: str(rollout), stopped=lambda: next(flags, True), sleep=lambda s: True,
                   set_status=lambda name, **kw: statuses.append(kw))
        assert statuses and ("parked_question_failing" in statuses[-1]) is cleared, (state, at, statuses)


def _ago(**delta):
    """A time `delta` before the real clock: run() judges ages by it."""
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).isoformat()


def test_a_question_never_sent_is_published_again_after_a_restart(tmp_path):
    """GitHub Codex on #549 at c019e07c: after a restart the status starts empty and only held rows are reported
    again, so a pointer refused too, or a server refusal, vanished from it. run() publishes the oldest such row when
    it starts, unless it is past LEDGER_RETAIN_S by the clock (pxx 216b4e INFO). MUTANTS: nothing published at start;
    an aged row published."""
    who = ident(tmp_path)
    Path(who["log"] + ".parked.json").write_text(json.dumps({
        "t3": {"at": _ago(hours=30), "state": "pointer-refused", "op_id": "parked.z"},
        "t2": {"at": _ago(hours=1), "state": "refused-by-server", "op_id": "parked.y"},
        "t1": {"at": _ago(hours=2), "state": "pointer-refused", "op_id": "parked.x", "detail": "guard"},
        "t0": {"at": _ago(hours=3), "state": "sent", "op_id": "parked.w"}}))
    rollout = tmp_path / "rollout.jsonl"
    write(rollout, "", mode="w")
    flags, statuses = iter([False, True]), []
    parked.run(who, rollout_for=lambda _i: str(rollout), stopped=lambda: next(flags, True), sleep=lambda s: True,
               set_status=lambda name, **kw: statuses.append(kw))
    assert statuses and statuses[0]["parked_question_failing"] == "t1", statuses
    assert "pointer-refused" in statuses[0]["parked_question_error"] and "guard" in statuses[0]["parked_question_error"]


@pytest.mark.parametrize("stdout, rc, outcome", [
    ('diagnostic mentions "key" but is not JSON', 0, parked.UNKNOWN),
    ('{"status":"refused","key":null}', 0, parked.UNKNOWN),
    ('{"status":"completed","signature_verified":false,"key":"msg/synthetic"}', 0, parked.UNKNOWN),
    ('{"status":"unknown","nested":{"key":"msg/synthetic"}}', 0, parked.UNKNOWN),
    ("no valid receipt", 0, parked.UNKNOWN),
    ("transport is NOT refused; receipt unknown", 1, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed"}', 1, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","op_id":"another"}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","state":"uncertain"}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","value_mismatch":["to"]}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","result_signature_verified":false}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"pending"}', 0, parked.UNKNOWN),
    ('{"key":"note/synthetic","status":"completed"}', 0, parked.UNKNOWN),
    ('{"key":"msg/","status":"completed"}', 0, parked.UNKNOWN),
    ('{"key":"msg/a b","status":"completed"}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","ok":false}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","sent":false}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","status_verified":false}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","error_code":"E_X"}', 0, parked.UNKNOWN),
    ('{"key":"msg/synthetic","status":"completed","result":{"ok":false}}', 0, parked.UNKNOWN),
    ('{"refused":"outbound_guard","sent":true}', 2, parked.UNKNOWN),
    ('{"refused":"outbound_guard","sent":false,"key":"msg/synthetic"}', 2, parked.UNKNOWN),
    ('{"refused":"outbound_guard","sent":false,"op_id":"another"}', 2, parked.UNKNOWN),
    ('{"refused":"outbound_guard","sent":false,"status":"completed"}', 2, parked.UNKNOWN),
    ('{"refused":"outbound_guard","sent":false,"op_id":"op-1"}', 2, parked.REFUSED),
    ('{"refused":"outbound_guard","sent":false}', 2, parked.REFUSED),
    ('{"key":"msg/synthetic","status":"completed"}', 0, parked.SENT),
    ('{"key":"msg/synthetic","status":"completed","op_id":"op-1","state":"succeeded","result_signature_verified":true}',
     0, parked.SENT),
])
def test_f1_only_a_complete_structured_answer_is_a_send_or_a_refusal(stdout, rc, outcome):
    """F1 (witnesses sender-accepts-*, stderr-key-can-create-success, nonrefusal-substring-dispatches-pointer):
    exit 0 and "key" anywhere was a send, and "refused" anywhere sent the pointer. MUTANTS: a substring test; the
    op id not compared; a false signature flag passed; the exit code, the status, the key's shape, the state or a
    value mismatch not checked; a refusal that names a key, or says it was sent, taken as one."""
    assert parked.classify(rc, stdout, "op-1")[0] == outcome


def test_f1_f5_the_real_sender_path_reads_stdout_bounded_and_says_unknown_past_the_limit(tmp_path, monkeypatch):
    """F5 (witness unbounded-output-and-body-unlink-path): the outputs were read whole. A sender that writes more
    than OUTPUT_LIMIT is UNKNOWN, and only stdout is the answer. MUTANTS: the outputs read unbounded; stderr read as the
    answer when stdout is empty."""
    monkeypatch.setattr(parked, "OUTPUT_LIMIT", 64)
    got = parked.send_via(_fake_sender(tmp_path, "big"), "oxxxxxx", "parked-x", "the body", "op-1")
    assert got == (False, False, "the sender wrote more than 64 bytes", parked.UNKNOWN)
    got = parked.send_via(_fake_sender(tmp_path, "stderr-key"), "oxxxxxx", "parked-x", "the body", "op-1")
    assert got[0] is False and got[3] == parked.UNKNOWN, "a key on stderr is no receipt"
    got = parked.send_via(_fake_sender(tmp_path, "stderr-only-key"), "oxxxxxx", "parked-x", "the body", "op-1")
    assert got[0] is False and got[3] == parked.UNKNOWN, "a receipt on stderr alone is no receipt"


@pytest.mark.parametrize("answer, rc, state", [
    ({"op_id": "op-1", "state": "succeeded", "already_sent": True, "key": SENT_KEY}, 0, parked.SENT),
    ({"op_id": "op-1", "state": "held-by-guard", "already_sent": False}, 1, parked.OP_HELD_BY_GUARD),
    ({"op_id": "op-1", "state": "refused", "already_sent": False}, 1, "refused-by-server"),
    ({"op_id": "op-1", "state": "uncertain", "already_sent": False}, 1, parked.UNKNOWN),
    ({"op_id": "another", "state": "succeeded", "already_sent": True, "key": SENT_KEY}, 0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": True}, 0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": False, "key": SENT_KEY}, 0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": True, "key": SENT_KEY}, 1, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "held-by-guard", "already_sent": True}, 1, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "refused"}, 1, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": True, "key": SENT_KEY, "result_signature_verified": False},
     0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": True, "key": SENT_KEY, "status_verified": False}, 0,
     parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": True, "key": SENT_KEY, "ok": False}, 0, parked.UNKNOWN),
    ({"state": "succeeded", "already_sent": True, "key": SENT_KEY}, 0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": True, "key": "msg/a b"}, 0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": 0, "key": SENT_KEY}, 0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": None, "key": SENT_KEY}, 0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "already_sent": "false", "key": SENT_KEY}, 0, parked.UNKNOWN),
    ({"op_id": "op-1", "state": "succeeded", "key": SENT_KEY}, 0, parked.UNKNOWN),
])
def test_f2_the_status_answer_is_read_by_its_fields(tmp_path, monkeypatch, answer, rc, state):
    """Each state needs the fields that make it so: a send needs exit 0, this op id, a msg/ key, already_sent exactly
    true (pxx 6ca3d4 R2-3: 0, None and "false" passed) and nothing that says no (pxx 2b3dc4 A); a guard hold or a
    refusal needs already_sent false. MUTANTS: the exit code, already_sent, the op id or a no-saying field not
    checked."""
    monkeypatch.setattr(parked, "_run_bounded", lambda argv, **kw: (rc, json.dumps(answer), False, "x"))
    assert parked.status_via(PEEK, "op-1")[0] == state
    assert parked.status_via({}, "op-1")[0] == parked.UNKNOWN, "no status command: unknown"


#: The identity fields peek_spec builds from, and the spec it builds.
WHO = {"python": "python", "client": "C:/client", "state": "s.json", "base_url": "http://x"}
PEEK = parked.peek_spec(WHO)


@pytest.mark.parametrize("argv", [
    ["python", "-m", "tools.meshy_node", "send-status", "--state-file", "--peek"],
    ["anything", "--peek"],
    ["python", "-m", "tools.meshy_node", "send-status"],
    ["python", "-m", "tools.meshy_node", "send-status", "--peek", "--base-url", "http://x", "--state-file", "--peek"],
    ["python", "-m", "tools.meshy_node", "send-status", "--peek", "--base-url", "--state-file", "s.json", "x"],
    ["python", "-m", "tools.meshy_node", "send-status", "--peek", "--base-url", "http://x", "--state-file", "s.json",
     "--extra"],
])
def test_f2_only_the_built_peek_command_is_ever_run(monkeypatch, argv):
    """pxx 6ca3d4 R2-4: `"--peek" in argv` let --peek stand as another option's value, which runs the mutating
    send-status. Only the exact shape peek_spec builds is run. MUTANT: token membership."""
    seen = []
    monkeypatch.setattr(parked, "_run_bounded", lambda a, **kw: seen.append(a) or (0, "{}", False, "x"))
    got = parked.status_via({"argv": argv, "cwd": "."}, "parked.0123")
    assert got[0] == parked.UNKNOWN and seen == [], "never run"


def test_f2_the_status_command_is_send_status_peek_with_the_op_id_last(monkeypatch):
    """a reviewer 7cb004 P1: send-status takes the op id as a positional, so `--op-id` exited 2 every time. And
    codex-oxxxxxx-cli 29cd67: plain send-status may present an unsettled op again, so only `--peek` is run, built from
    the identity (pxx 6ca3d4 R2-4). Checked against the client's real parser. MUTANTS: the op id as --op-id; the
    spec not built from the identity."""
    seen = []
    monkeypatch.setattr(parked, "_run_bounded", lambda argv, **kw: seen.append((list(argv), kw.get("cwd"))) or
                        (1, "{}", False, "x"))
    assert PEEK == {"argv": ["python", "-m", "tools.meshy_node", "send-status", "--peek", "--base-url", "http://x",
                             "--state-file", "s.json"], "cwd": "C:/client"}
    parked.status_via(PEEK, "parked.0123")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.meshy_node import cli
    try:
        args = cli.build_parser().parse_args(seen[0][0][3:])
    except SystemExit:                                          # argparse's refusal is an exit, not an exception
        raise AssertionError("the client's parser refused %r" % (seen[0][0][3:],)) from None
    assert args.cmd == "send-status" and args.op_id == "parked.0123" and args.peek is True
    assert str(args.state_file) == "s.json" and seen[0][1] == "C:/client"
    for missing in ("python", "client", "state", "base_url"):
        assert parked.peek_spec(dict(WHO, **{missing: ""})) is None, missing
    assert parked.peek_spec(dict(WHO, state="--peek")) is None, "a field that reads as an option is refused"


def test_the_postbox_runs_it_as_its_own_guarded_loop_only_when_enabled():
    src = (Path(__file__).resolve().parents[1] / "tools" / "postbox" / "postbox.py").read_text(encoding="utf-8")
    assert "if parked is not None and parked.enabled(ident):" in src
    assert "args=(run_parked_question, ident)" in src and "target=guarded" in src


FAKE_SENDER = r'''
import json, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
(here / "argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
mode = (here / "mode.txt").read_text(encoding="utf-8").strip()
if mode == "ok":
    print(json.dumps({"key": "msg/1790000000000-fake-ok", "status": "completed"}))
    sys.exit(0)
if mode == "big":
    print("x" * 200)
    sys.exit(0)
if mode == "stderr-key":
    print("no receipt")
    sys.stderr.write('{"key": "msg/1790000000000-fake-ok", "status": "completed"}\n')
    sys.exit(0)
if mode == "stderr-only-key":
    sys.stderr.write('{"key": "msg/1790000000000-fake-ok", "status": "completed"}\n')
    sys.exit(0)
subprocess.Popen([sys.executable, "-c", "import time, pathlib; time.sleep(__MARK_DELAY__); "
                  "pathlib.Path(%r).write_text('x')" % str(here / "grandchild.mark")])
time.sleep(__MARK_DELAY__)
(here / "child.mark").write_text("x")
time.sleep(30)
'''
#: The longest a stopped send may take to end, by the code's own bounds: the stop seen at 0.6 s, one poll, the tree
#: kill and the reap, plus a second of slack. Still far below the fake sender's own 30 s, so a pass still proves the
#: send ended at the stop. memory-bxxxxx f4bc80: a fixed 2.5 s failed at 3.0 and 3.6 s under gate load on HQB.
STOP_BOUND_S = 0.6 + parked.STOP_POLL_S + parked.KILL_TIMEOUT_S + parked.REAP_TIMEOUT_S + 1.0
#: When the slow fake sender's child and grandchild write their marks: past STOP_BOUND_S, so a mark proves the tree
#: was NOT ended, never only that a loaded box ended it slowly.
MARK_DELAY_S = 8
FAKE_SENDER = FAKE_SENDER.replace("__MARK_DELAY__", str(MARK_DELAY_S))


def _fake_sender(tmp_path, mode):
    (tmp_path / "sender.py").write_text(FAKE_SENDER, encoding="utf-8")
    (tmp_path / "mode.txt").write_text(mode, encoding="utf-8")
    return [sys.executable, str(tmp_path / "sender.py")]


def _body_file(tmp_path):
    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    return Path(argv[argv.index("--body-file") + 1])


def test_a_stop_during_a_send_ends_the_sender_and_its_child_and_removes_the_body_file(tmp_path):
    """codex-kxxx-cli 28100b, pxx e9acdb P2: a stop ends the whole process tree within the code's own bound
    (STOP_BOUND_S), long before the sender's 30 s; the body file is removed, and the outcome is UNKNOWN (held, never
    resent). The marks come at MARK_DELAY_S, past that bound, so a slow kill on a loaded box passes and a missed one
    still fails. MUTANT: ignore the stop. MUTANT: kill only the sender."""
    import time
    assert MARK_DELAY_S > STOP_BOUND_S + 0.5, "the marks must come after the slowest allowed end"
    argv = _fake_sender(tmp_path, "slow")
    t0 = time.monotonic()
    got = parked.send_via(argv, "oxxxxxx", "parked-x", "the body", "op-1", stopped=lambda: time.monotonic() - t0 > 0.6)
    took = time.monotonic() - t0
    assert got == (False, False, "stopped: the postbox is stopping, the send was ended", parked.UNKNOWN), got
    assert took < STOP_BOUND_S, took
    assert not _body_file(tmp_path).exists(), "the body file is removed"
    time.sleep(max(0.0, MARK_DELAY_S + 1.0 - (time.monotonic() - t0)))
    assert not (tmp_path / "child.mark").exists() and not (tmp_path / "grandchild.mark").exists(), "the tree is ended"


def test_a_kill_that_does_not_take_is_bounded_and_reported_never_as_ended(tmp_path, monkeypatch):
    """codex-kxxx-cli 8390ed, pxx 3f07dc: the tree killer does nothing (on Windows it hangs until its timeout, as a
    stuck taskkill would) and the sender keeps running: send_via returns within the bound and says the exit was not
    confirmed. MUTANTS: "ended" without a confirmed exit; no bound on the wait; a 10 s taskkill timeout."""
    import signal
    import subprocess
    import tempfile
    import time
    real_run, real_killpg = subprocess.run, getattr(parked.os, "killpg", None)

    def stuck_taskkill(cmd, *a, timeout=None, **kw):
        time.sleep(timeout or 0)
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))          # the body and output files land here
    monkeypatch.setattr(parked.subprocess, "run", stuck_taskkill)
    monkeypatch.setattr(parked.os, "killpg", lambda *a: None, raising=False)
    monkeypatch.setattr(parked.subprocess.Popen, "kill", lambda self: None)
    monkeypatch.setattr(parked._Job, "terminate", lambda self: False)    # pxx daa1bd: on Windows the job must not take
    argv = _fake_sender(tmp_path, "slow")
    t0, seen = time.monotonic(), []

    def stopped():
        if time.monotonic() - t0 > 0.6:
            seen[:] = seen or [time.monotonic()]                    # when the send first saw the stop
            return True
        return False
    got = parked.send_via(argv, "oxxxxxx", "parked-x", "the body", "op-1", stopped=stopped)
    after_stop = time.monotonic() - seen[0] if seen else None
    monkeypatch.undo()
    pid = int(got[2].rsplit("pid ", 1)[1].rstrip(")")) if "pid " in got[2] else None
    try:
        assert got[:2] == (False, False) and got[2].startswith("stopped: the sender did not confirm its exit (pid ")
        assert got[3] == parked.UNKNOWN
        assert after_stop is not None and after_stop < 6.5, after_stop
        assert "ended" not in got[2] and not _body_file(tmp_path).exists(), "the body file is removed anyway"
    finally:
        if pid is not None:                                          # end the sender for real
            if parked.os.name == "nt":
                real_run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
            else:
                real_killpg(pid, signal.SIGKILL)
    time.sleep(1.0)
    left = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(("parked-", "tmp")))
    assert left == [], "the output files go with the sender's last handle: %s" % left


class _Proc:
    def __init__(self, exited=True):
        self.pid, self.killed, self.exited = 4242, False, exited

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0 if self.exited else None


@pytest.mark.parametrize("platform, tree, exited, ended", [
    ("posix", PermissionError(1, "denied"), True, False),
    ("posix", ProcessLookupError(3, "gone"), True, True),
    ("posix", None, True, True),
    ("posix", None, False, False),
    ("nt", 128, True, False),
    ("nt", 0, True, True),
])
def test_f4_the_tree_counts_as_ended_only_when_the_tree_kill_took(monkeypatch, platform, tree, exited, ended):
    """F4 (witness group-kill-failure-leader-exit-reported-ended): a failed killpg with an exited leader was reported
    as ended. MUTANTS: the tree kill's failure ignored; taskkill's exit code ignored."""
    import types

    def killpg(pid, sig):
        if isinstance(tree, BaseException):
            raise tree
    fake_os = types.SimpleNamespace(name=platform, killpg=killpg)
    monkeypatch.setattr(parked, "os", fake_os)
    if platform == "nt":
        monkeypatch.setattr(parked.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=tree))
    assert parked._kill_tree(_Proc(exited)) is ended


def test_a_send_past_its_timeout_is_ended_the_same_way(tmp_path, monkeypatch):
    """The body file is found in a temp folder of this test's own, never through the fake's argv.json: with a 1 s
    timeout a loaded box can end the fake before it writes that file (mg567s, 9 Nov: a flake on main 47808df8)."""
    import tempfile
    import time
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(bodies))
    monkeypatch.setattr(parked, "SEND_TIMEOUT_S", 1)
    argv = _fake_sender(tmp_path, "slow")
    t0 = time.monotonic()
    got = parked.send_via(argv, "oxxxxxx", "parked-x", "the body", "op-1")
    assert got == (False, False, "TimeoutExpired", parked.UNKNOWN)
    # The BODY file (prefix "parked-") is unlinked by send_via before it returns: checked at once. The two OUTPUT
    # files (TemporaryFile, prefix "tmp") are delete-on-close, and on Windows the ended sender's inherited handles
    # can outlive taskkill's return by a moment on a loaded box, so they get a bounded wait. mg597 (11 Nov) failed
    # here with exactly two files left, tmp542arv3e and tmpqmyom_15: the outputs, not the body.
    assert [p.name for p in bodies.iterdir() if p.name.startswith("parked-")] == [], "the body file outlived the send"
    gone_by = time.monotonic() + 10
    while list(bodies.iterdir()) and time.monotonic() < gone_by:
        time.sleep(0.1)
    assert list(bodies.iterdir()) == [], "an output file outlived the ended sender by 10 s"
    time.sleep(max(0.0, MARK_DELAY_S + 1.0 - (time.monotonic() - t0)))
    assert not (tmp_path / "child.mark").exists() and not (tmp_path / "grandchild.mark").exists()


def test_a_send_that_works_still_reports_its_key(tmp_path):
    got = parked.send_via(_fake_sender(tmp_path, "ok"), "oxxxxxx", "parked-x", "the body", "op-1")
    assert got[:2] == (True, False) and got[3] == parked.SENT and not _body_file(tmp_path).exists()


def test_a_send_ended_by_a_stop_is_held_under_its_op_id_and_the_next_start_does_not_resend_it(tmp_path):
    """pxx e9acdb's case, end to end with the real send_via, under F2: a stop mid-send leaves the turn HELD in the
    ledger with its op_id; the next start does not send it again, and its status settles it. MUTANT: a stopped send
    left out of the ledger; the next start sends it again."""
    import functools
    import time
    watch, ledger = _setup(tmp_path, *_asking())
    base = ident(tmp_path)
    who = dict(base, parked_question=dict(base["parked_question"], sender=_fake_sender(tmp_path, "slow")))
    t0 = time.monotonic()
    send = functools.partial(parked.send_via, stopped=lambda: time.monotonic() - t0 > 0.6)
    assert _check(who, watch, ledger, send=send) == []
    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    op_id = argv[argv.index("--op-id") + 1]
    assert ledger.rows["t1"]["state"] == "unknown" and ledger.rows["t1"]["op_id"] == op_id
    rollout = tmp_path / "rollout.jsonl"
    again = parked.TurnWatch(str(rollout), from_end=True, backlog=parked.BACKLOG_BYTES)   # the next start
    again.update()
    retry = Sender()
    fresh = parked.Ledger(str(tmp_path / "l.json"))
    assert _check(who, again, fresh, send=retry) == [] and retry.calls == [], "held, never resent"


def test_the_loop_hands_its_stop_flag_to_every_send_and_builds_the_status_from_the_config(tmp_path, monkeypatch):
    """run() binds the postbox's stop flag into the send; `"status": true` binds the identity's own peek (peek_spec)
    into the reconcile, and anything else (an argv of round 1) binds none; the watch carries the coordinator. MUTANTS:
    send_via without `stopped`; a configured argv run; the watch without its coordinator."""
    seen, statuses, coordinators = [], [], []

    def fake_send(argv, to, thread, body, op_id, *, stopped=None):
        seen.append(stopped)
        return True, False, "", parked.SENT

    def fake_check(ident_, watch, ledger, *, send, status, report):
        statuses.append(status)
        coordinators.append(watch.coordinator)
        send([], "", "", "", "op")
        return []
    monkeypatch.setattr(parked, "send_via", fake_send)
    monkeypatch.setattr(parked, "check", fake_check)
    rollout = tmp_path / "rollout.jsonl"
    write(rollout, "", mode="w")
    for status_cfg, built in ((True, True), ({"argv": ["python", "-m", "tools.meshy_node", "send-status"]}, False)):
        flags = iter([False, True])
        stop = lambda: next(flags, True)                       # noqa: E731 - one pass of the loop, then stop
        who = dict(ident(tmp_path), **WHO)
        who["parked_question"] = dict(who["parked_question"], status=status_cfg)
        seen.clear(), statuses.clear()
        parked.run(who, rollout_for=lambda _i: str(rollout), stopped=stop, sleep=lambda s: True)
        assert seen == [stop], seen
        if built:
            assert statuses[0].func is parked.status_via and statuses[0].args[0] == parked.peek_spec(who)
        else:
            assert statuses == [None], "a configured command is never the status"
    assert coordinators == ["oxxxxxx", "oxxxxxx"]


#: The child of the senders below: it writes the mark named by its first argument after 4 s.
CHILD_CODE = "import sys, time, pathlib; time.sleep(4); pathlib.Path(sys.argv[1]).write_text('x')"


def _sender_with_a_child(sender, mark):
    """A fake sender that starts CHILD_CODE at once and then sleeps 30 s. The mark's path goes in as json (a valid
    Python string literal on every platform) and reaches the child as an argument, never pasted into code: pxx
    daa1bd / GitHub Codex on 75884e0e, %r of a Windows path inside a single-quoted -c string did not compile, so the
    sender died before it started a child. Both texts are compiled here, so a quoting slip fails on every platform."""
    src = ("import subprocess, sys, time\n"
           "MARK = %s\n"
           "subprocess.Popen([sys.executable, '-c', %s, MARK])\n"
           "time.sleep(30)\n") % (json.dumps(str(mark)), json.dumps(CHILD_CODE))
    compile(CHILD_CODE, "<child>", "exec")
    compile(src, str(sender), "exec")
    sender.write_text(src, encoding="utf-8")
    return sender


def test_the_child_sender_compiles_with_a_windows_path(tmp_path):
    """The quoting check, on every platform: a path with backslashes and a quote still makes a sender that compiles
    and names that path exactly. MUTANT: paste the path with %r into a single-quoted string."""
    mark = "C:\\Users\\builder\\AppData\\Local\\Temp\\pytest-1\\it's.mark"
    sender = _sender_with_a_child(tmp_path / "s.py", mark)
    ns = {}
    exec(compile(sender.read_text(encoding="utf-8").splitlines()[1], "<mark>", "exec"), ns)
    assert ns["MARK"] == mark


def test_on_windows_a_child_started_while_the_end_runs_is_ended_too(tmp_path, monkeypatch):
    """mg602 (11 Nov 2026): after a 1 s timeout the fake sender's grandchild still wrote its mark. taskkill /T walks
    the tree once; the sender is now in a job object and the end terminates the job. Here the sender starts its
    child at once, and taskkill is made to miss it (run without /T), which is what a walk that began before the
    child existed does. Ending the job ends the child anyway. Windows only: elsewhere the process group covers it.
    MUTANT: _kill_tree ignores the job (the child writes its mark)."""
    import time
    if parked.os.name != "nt":
        pytest.skip("job objects are Windows only")
    started = tmp_path / "late.mark"
    sender = _sender_with_a_child(tmp_path / "late_sender.py", started)
    monkeypatch.setattr(parked, "SEND_TIMEOUT_S", 0.2)
    real_run = parked.subprocess.run

    def taskkill_that_misses_children(cmd, *a, **kw):    # reached only when the job is not used
        return real_run([c for c in cmd if c != "/T"], *a, **kw)
    monkeypatch.setattr(parked.subprocess, "run", taskkill_that_misses_children)
    t0 = time.monotonic()
    got = parked.send_via([sys.executable, str(sender)], "oxxxxxx", "parked-x", "the body", "op-1")
    assert got[3] == parked.UNKNOWN and got[2] == "TimeoutExpired", got
    time.sleep(max(0.0, 6.0 - (time.monotonic() - t0)))
    assert not started.exists(), "a child the sender started during the end outlived it"


def test_with_a_job_the_tree_counts_as_ended_only_when_the_walk_and_the_job_both_took(monkeypatch):
    """pxx 3cfe08: taskkill /T runs first even with a job (it reaches a child started before the assign), and the
    tree counts as ended only when both took. No job, or one never assigned: taskkill alone decides, as before."""
    import types
    if parked.os.name != "nt":
        pytest.skip("job objects are Windows only")
    seen = []
    monkeypatch.setattr(parked.subprocess, "run",
                        lambda cmd, *a, **k: seen.append(cmd) or types.SimpleNamespace(returncode=0))
    job = types.SimpleNamespace(assigned=True, terminate=lambda: True)
    assert parked._kill_tree(_Proc(True), job=job) is True and seen[0][:2] == ["taskkill", "/T"]
    job.terminate = lambda: False
    assert parked._kill_tree(_Proc(True), job=job) is False                  # the job did not take
    monkeypatch.setattr(parked.subprocess, "run", lambda cmd, *a, **k: types.SimpleNamespace(returncode=1))
    job.terminate = lambda: True
    assert parked._kill_tree(_Proc(True), job=job) is False                  # the walk did not take
    monkeypatch.setattr(parked.subprocess, "run", lambda cmd, *a, **k: types.SimpleNamespace(returncode=0))
    assert parked._kill_tree(_Proc(True), job=types.SimpleNamespace(assigned=False)) is True    # never assigned
    assert parked._kill_tree(_Proc(True), job=None) is True


def test_on_windows_a_child_started_before_the_assign_is_ended_by_the_walk(tmp_path, monkeypatch):
    """GitHub Codex on 9a7d4d7b, pxx 3cfe08: a child the sender starts before it is in the job is not in the job, so
    ending the job alone leaves it. The assign is delayed here until the child exists; the walk that runs first ends
    it. MUTANT: _kill_tree skips taskkill when a job exists (the child writes its mark)."""
    import time
    if parked.os.name != "nt":
        pytest.skip("job objects are Windows only")
    early = tmp_path / "early.mark"
    sender = _sender_with_a_child(tmp_path / "early_sender.py", early)
    real_assign = parked._Job.assign

    def late_assign(self, proc):
        time.sleep(1.0)                                    # the sender has started its child by now
        return real_assign(self, proc)
    monkeypatch.setattr(parked._Job, "assign", late_assign)
    monkeypatch.setattr(parked, "SEND_TIMEOUT_S", 0.2)
    t0 = time.monotonic()
    got = parked.send_via([sys.executable, str(sender)], "oxxxxxx", "parked-x", "the body", "op-1")
    assert got[3] == parked.UNKNOWN and got[2] == "TimeoutExpired", got
    time.sleep(max(0.0, 6.0 - (time.monotonic() - t0)))
    assert not early.exists(), "a child the sender started before the job assign outlived the end"
