"""Queued notices already handled while the native client held them (#58).

Pure temporary receipts and generated routing text. No live queue, database,
server or signing state. The current renderer and a frozen legacy batch shape
are both exercised; handling notifications never removes an unresolved task.
"""
import copy
import re
import sys
from pathlib import Path

import pytest
from tests.test_postbox_reconcile import pb, ident  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools/postbox"))
from queued_notices import handled_keys


@pytest.fixture
def world(pb, tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "graphline", None)
    monkeypatch.setattr(pb, "owedline", None)
    i = ident(tmp_path)
    events = [dict(event="message", role=role, key="msg/" + name, version=1,
                   from_peer_id="fixture-peer", created_at="2026-10-08T13:26:53Z")
              for name, role in [("receipt", "to"), ("cc", "cc")]]
    rows = {e["key"]: e for e in events}
    ledger = {e["key"]: {"version": 1, "unresolved": "guest approval still pending"} for e in events}
    def handled(i, e):
        return ledger.get(e["key"], {}).get("version") == e.get("version", 1)
    def check(item, renderer=pb.codex_batch_notice):
        return handled_keys(item, i, rows, renderer, handled)
    def item(es=events, renderer=pb.codex_batch_notice):
        return {"input": [{"type": "text", "text": renderer(i, es)}]}
    return i, events, rows, ledger, check, item


def test_fully_handled_batch_not_limited_to_single_header(world):
    i, events, rows, ledger, check, item = world
    before = copy.deepcopy((rows, ledger))
    queued = item()
    # The old single-only matcher misses a batch, even though each key is handled.
    old = re.match(r"MeshyChat message for you .* key=(msg/\S+) ", queued["input"][0]["text"])
    assert old is None
    assert check(queued) == ("msg/receipt", "msg/cc")
    assert (rows, ledger) == before


def test_single_notice_still_matches(world):
    i, events, rows, ledger, check, item = world
    assert check(item(events[:1])) == ("msg/receipt",)


def test_original_versionless_spool_row_is_compatible(world):
    i, events, rows, ledger, check, item = world
    queued = item()
    for event in events:
        event.pop("version")
    assert check(queued) == ("msg/receipt", "msg/cc")


def test_legacy_batch_header_reconciles_as_one_notice(world):
    i, events, rows, ledger, check, item = world
    def legacy(i, es):
        lines = ["MeshyChat message for you (%s). role=%s key=%s from_peer=%s at=%s"
                 % (i["name"], e["role"], e["key"], e["from_peer_id"], e["created_at"]) for e in es]
        return "%d MeshyChat deliveries for %s, handled together in this one turn.\n\n" % (len(es), i["name"]) + "\n\n".join(lines)
    assert check(item(renderer=legacy), legacy) == ("msg/receipt", "msg/cc")


def test_mixed_batch_keeps_unhandled_work_and_whole_input(world):
    i, events, rows, ledger, check, item = world
    queued = item()
    ledger.pop("msg/cc")
    before = copy.deepcopy(queued)
    assert check(queued) is None
    assert queued == before


def test_predicate_reads_handled_state_afresh_on_every_call(world):
    """MUTANT Q8: memoize handled_keys on the item's text; the second call then returns the stale tuple.

    This is the predicate half of "re-run immediately before a deletion": a later call sees handling
    withdrawn after an earlier positive answer. The deleting caller is not in this repository, so the
    test that its delete re-checks (handled state flipped between its first check and the delete)
    lands with that caller (README, "Reconcile already queued single and batched notices")."""
    i, events, rows, ledger, check, item = world
    queued = item()
    assert check(queued)
    ledger.pop("msg/receipt")
    assert check(queued) is None


@pytest.mark.parametrize("version", [2, True, 0, None, "1"])
def test_new_or_unknown_version_never_cancelled_by_old_notice(world, version):
    i, events, rows, ledger, check, item = world
    queued = item()
    rows["msg/receipt"]["version"] = version
    assert check(queued) is None


def test_edit_row_and_listener_warning_are_preserved(world):
    i, events, rows, ledger, check, item = world
    queued = item()
    rows["msg/receipt"]["event"] = "edit"
    assert check(queued) is None
    warning = dict(event="status", state="error", error="retention_gap")
    assert check(item([warning])) is None


@pytest.mark.parametrize("kind", ["quote", "append", "truncate", "attachment", "another-seat", "another-peer", "role"])
def test_nonexact_operator_edited_or_foreign_input_is_preserved(world, kind):
    i, events, rows, ledger, check, item = world
    queued = item()
    value = queued["input"][0]
    if kind == "quote": value["text"] = "Please investigate: " + value["text"]
    if kind == "append": value["text"] += "\nNew operator request"
    if kind == "truncate": value["text"] = value["text"][:-1]
    if kind == "attachment": queued["input"].append({"type": "image", "image_url": "fixture"})
    if kind == "another-seat": value["text"] = value["text"].replace(i["name"], "other-seat")
    if kind == "another-peer": value["text"] = value["text"].replace("fixture-peer", "another-peer")
    if kind == "role": rows["msg/receipt"]["role"] = "cc"
    assert check(queued) is None


@pytest.mark.parametrize("item", [None, {}, {"input": []}, {"input": ["bad"]}, {"input": [{"type": "text", "text": "x" * 24001}]}])
def test_malformed_or_oversized_item_is_preserved(world, item):
    assert world[4](item) is None


def test_missing_spool_row_and_duplicate_key_are_preserved(world):
    i, events, rows, ledger, check, item = world
    assert check(item([events[0], events[0]])) is None
    queued = item()
    rows.pop("msg/receipt")
    assert check(queued) is None


def test_bad_protected_handling_reader_cannot_authorize_cancellation(world, pb):
    i, events, rows, ledger, check, item = world
    def unreadable(i, e): raise OSError("fixture protected ledger refused")
    assert handled_keys(item(), i, rows, pb.codex_batch_notice, unreadable) is None
    assert handled_keys(item(), i, rows, pb.codex_batch_notice, lambda i, e: "true") is None


SECRET = "fixture peer text that must never reach a log line"


@pytest.mark.parametrize("stage", ["render_batch", "is_handled"])
@pytest.mark.parametrize("error", [AttributeError, RuntimeError, IndexError, LookupError, ZeroDivisionError])
def test_any_callback_exception_keeps_item_and_is_noted(world, pb, stage, error):
    """Review on #176: a callback raising outside the four named types (an AttributeError on a row of an
    unexpected shape) must not escape a predicate whose contract is a tuple or None.

    MUTANT Q1: catch only (OSError, ValueError, TypeError, KeyError) again; the error then propagates.
    MUTANT Q2: drop the note call; nothing is logged.
    MUTANT Q3: log str(e) instead of the class name; the fixture's message text reaches the log line.
    MUTANT Q4: never advance the stage past render_batch; an is_handled failure is then misnamed."""
    i, events, rows, ledger, check, item = world
    def boom(*_):
        raise error(SECRET)
    render = boom if stage == "render_batch" else pb.codex_batch_notice
    handled = boom if stage == "is_handled" else (lambda i, e: True)
    lines = []
    assert handled_keys(item(), i, rows, render, handled, note=lines.append) is None
    assert len(lines) == 1
    assert stage in lines[0] and error.__name__ in lines[0] and i["name"] in lines[0]
    assert SECRET not in lines[0] and "msg/" not in lines[0]


def test_failing_note_still_answers_none(world):
    """MUTANT Q5: let _report call note without its own try; a full disk in the log then raises out."""
    i, events, rows, ledger, check, item = world
    def full_disk(_):
        raise OSError("fixture: no space left on device")
    def broken(i, e):
        raise AttributeError("fixture row shape")
    assert handled_keys(item(), i, rows, lambda i, es: item()["input"][0]["text"], broken, note=full_disk) is None


def test_default_note_writes_one_line_to_stderr(world, capsys):
    """MUTANT Q6: make the default note a no-op; a cleaner that passes no note then logs nothing."""
    i, events, rows, ledger, check, item = world
    def broken(i, e):
        raise AttributeError(SECRET)
    assert handled_keys(item(), i, rows, lambda i, es: item()["input"][0]["text"], broken) is None
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "is_handled raised AttributeError" in err and SECRET not in err


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupt_is_not_swallowed(world, interrupt):
    """MUTANT Q7: catch BaseException; an operator's Ctrl-C or a shutdown is then eaten as 'keep the item'."""
    i, events, rows, ledger, check, item = world
    def stop(i, e):
        raise interrupt()
    with pytest.raises(interrupt):
        handled_keys(item(), i, rows, lambda i, es: item()["input"][0]["text"], stop, note=lambda m: None)
