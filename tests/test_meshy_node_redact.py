"""`redact`, the client command for memory.redact (divergence 053; docs/design/redaction.md; tools/meshy_node/node.py redact).

Until this command a seat could take back its own message or card only with a hand-built invoke: the client had `edit`
and no `redact`. What is held here:

  * the arguments sent are exactly namespace, key, version, reason_code and blank_thread_id, and nothing else;
  * `--version` is REQUIRED and is "all" or a whole number of at least 1. The server's default is every version and a
    redaction cannot be undone, so the client does not let that default be reached by leaving a flag out;
  * `--reason` is the server's closed list, read here from apps/memoryskills/redact.py itself, so the two cannot drift;
  * exit 0 done, 1 refused or unverified, 2 refused before anything was sent, 3 done but text remains in a version
    another owner wrote (the server leaves those alone and lists them; `text_remains_in_versions` names them).

No network and no database: node.invoke and node.load_state are replaced, as tests/test_meshy_node_send_guard.py
replaces node.send.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_redact.py -q -p no:cacheprovider
Mutants (tools/meshy_node/node.py redact and cli.py, each seen to fail an assertion here, bytecode cache off): the
version check dropped, or bool let through; a reason outside the list sent; blank_thread_id left out of the args;
--version given a default; --reason's choices dropped from the CLI; the exit 3 branch removed; text_remains_in_versions
filtered to well-formed entries. Eight in all, every one killed (2026-10-10).
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node  # noqa: E402

BASE = "http://meshy.test:6310"
STATE = {"base_url": BASE, "status": node.STATUS_MINTED, "peer_id": "11111111-1111-1111-1111-111111111111"}


def _server_reason_codes() -> tuple:
    """REASON_CODES as the server module states it, read without importing the server (which needs Django)."""
    tree = ast.parse((ROOT / "apps" / "memoryskills" / "redact.py").read_text(encoding="utf-8"))
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(getattr(t, "id", None) == "REASON_CODES" for t in stmt.targets):
            return ast.literal_eval(stmt.value)
    raise AssertionError("control: apps/memoryskills/redact.py states no REASON_CODES")


def _answer(*, status="completed", verified=True, result=None) -> dict:
    if result is None:
        result = {"op": "memory.redact", "namespace": "shared/mesh-chat", "key": "msg/1-me-aa", "redacted": [{}],
                  "already_redacted": [], "not_redacted_other_owner": [], "reply_authors": [],
                  "stored_calls_blanked": {"write_calls": 1, "read_results": 0}}
    return {"call_id": "c1", "invoke": {"http": 202, "status": "accepted"}, "status": status, "result": result,
            "error_code": None if status == "completed" else "E_PERMISSION_DENIED", "error_message": None,
            "result_signing_kid": "k", "result_signature_verified": verified, "result_signature_detail": "ok"}


@pytest.fixture
def calls(monkeypatch):
    """The invokes made, and the answer the next one gets (set `calls.answer`)."""
    class Calls(list):
        answer = _answer()

    made = Calls()
    monkeypatch.setattr(node, "load_state", lambda path, base_url: dict(STATE))

    def fake_invoke(path, state, *, skill, args, poll_timeout=40.0, capability=None):
        made.append({"skill": skill, "args": args, "capability": capability})
        return made.answer

    monkeypatch.setattr(node, "invoke", fake_invoke)
    return made


def _run(tmp_path, *extra) -> int:
    return cli.main(["redact", "--base-url", BASE, "--state-file", str(tmp_path / "state.json"), *extra])


def test_the_reason_list_is_the_servers():
    assert node.REDACT_REASON_CODES == _server_reason_codes()


def test_a_redaction_sends_exactly_these_args_and_exits_0(calls, tmp_path, capsys):
    code = _run(tmp_path, "--key", "msg/1-me-aa", "--version", "2", "--reason", "sent_in_error")
    assert code == 0
    assert calls == [{"skill": "memory.redact", "capability": None,
                      "args": {"namespace": "shared/mesh-chat", "key": "msg/1-me-aa", "version": 2,
                               "reason_code": "sent_in_error", "blank_thread_id": False}}]
    out = json.loads(capsys.readouterr().out)
    assert out["text_remains_in_versions"] == [] and out["key"] == "msg/1-me-aa"


def test_all_versions_a_card_namespace_and_the_thread_blanked(calls, tmp_path):
    code = _run(tmp_path, "--key", "card/txxxx/x1", "--namespace", "shared/decisions", "--version", "all",
                "--reason", "client_name", "--blank-thread-id")
    assert code == 0
    assert calls[0]["args"] == {"namespace": "shared/decisions", "key": "card/txxxx/x1", "version": "all",
                                "reason_code": "client_name", "blank_thread_id": True}


def test_version_is_required(calls, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, "--key", "msg/1-me-aa", "--reason", "sent_in_error")
    assert exc.value.code == 2 and calls == []


@pytest.mark.parametrize("bad", ["0", "-1", "x", "ALL", "1.5", ""])
def test_a_version_that_is_neither_all_nor_a_positive_number_sends_nothing(calls, tmp_path, bad):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, "--key", "msg/1-me-aa", "--version", bad, "--reason", "sent_in_error")
    assert exc.value.code == 2 and calls == []


def test_a_reason_outside_the_list_sends_nothing(calls, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, "--key", "msg/1-me-aa", "--version", "1", "--reason", "because")
    assert exc.value.code == 2 and calls == []


@pytest.mark.parametrize("kw", [{"version": True}, {"version": 0}, {"version": "2"}, {"reason_code": "because"},
                                {"key": ""}, {"namespace": ""}, {"blank_thread_id": 1}])
def test_the_function_refuses_the_same_before_any_call(calls, tmp_path, kw):
    args = {"key": "msg/1-me-aa", "version": 1, "reason_code": "sent_in_error", **kw}
    with pytest.raises(node.Refused):
        node.redact(tmp_path / "state.json", dict(STATE), **args)
    assert calls == []


def test_text_another_owner_wrote_is_exit_3_and_named(calls, tmp_path, capsys):
    result = _answer()["result"]
    calls.answer = _answer(result={**result, "not_redacted_other_owner": [{"version": 1, "author_label": "kxxx"}]})
    assert _run(tmp_path, "--key", "msg/1-me-aa", "--version", "all", "--reason", "sent_in_error") == 3
    assert json.loads(capsys.readouterr().out)["text_remains_in_versions"] == [1]


def test_an_entry_of_an_unknown_shape_still_says_text_remains(calls, tmp_path, capsys):
    result = _answer()["result"]
    calls.answer = _answer(result={**result, "not_redacted_other_owner": ["v1"]})
    assert _run(tmp_path, "--key", "msg/1-me-aa", "--version", "all", "--reason", "sent_in_error") == 3
    assert json.loads(capsys.readouterr().out)["text_remains_in_versions"] == ["v1"]


def test_a_refusal_is_exit_1(calls, tmp_path):
    calls.answer = {**_answer(status="failed"), "result": None}
    assert _run(tmp_path, "--key", "msg/1-me-aa", "--version", "1", "--reason", "sent_in_error") == 1


def test_an_unverified_answer_is_exit_1_even_when_it_says_done(calls, tmp_path):
    calls.answer = _answer(verified=False)
    assert _run(tmp_path, "--key", "msg/1-me-aa", "--version", "1", "--reason", "sent_in_error") == 1
