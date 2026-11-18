"""#374, GitHub Codex P2 on e3fafb50 (transcript_cards.py:1905, and the same shape in load_hook): a compaction summary
that merely mentions this seat's marker set `has_own`, so a transcript whose real prompt is ANOTHER seat's marker was
not foreign, and --single-seat-dir admitted it. The rule now: real prompts decide when any of them carries a marker;
a summary or meta record counts only when none does. The second half keeps what #354 relies on: a continuation
transcript whose only markers sit in its summary is still judged by them (the exxxx-session and 3e33a0aa cases in
test_transcript_cards_c5_and_board_worker.py)."""
import json
import os
import sys

import pytest

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import load_hook as lh  # noqa: E402
import transcript_cards as tc  # noqa: E402

CONT = "This session is being continued from a previous conversation that ran out of context.\n"


def _summary(text, flag="isCompactSummary"):
    rec = {"type": "user", "uuid": "u-sum", "sessionId": "s1", "timestamp": "2026-10-23T08:00:00.000Z",
           "message": {"role": "user", "content": text}}
    if flag:
        rec[flag] = True
    return rec


def _prompt(text, i=1):
    return {"type": "user", "uuid": "u-%d" % i, "sessionId": "s1", "timestamp": "2026-10-23T08:0%d:00.000Z" % i,
            "message": {"role": "user", "content": text}}


def _write(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"type": "ai-title", "sessionId": "s1"}) + "\n")
        for r in records:
            f.write(json.dumps(r) + "\n")


def _foreign_both(proj, name, seat):
    """Both scans, so neither can drift from the other: the loader's and the worker's."""
    own = {seat}
    loader = tc._has_foreign_seat_marker(str(proj / name), own, [("echo %s-shell-ok" % seat).encode()])
    worker = lh._transcript_has_foreign_marker(str(proj), name, seat)
    return loader, worker


# The summary forms the loader recognizes: the flag, the meta flag, and the continuation text with no flag at all.
SUMMARY_FORMS = [("isCompactSummary", CONT), ("isMeta", ""), (None, CONT)]


@pytest.mark.parametrize("flag,lead", SUMMARY_FORMS)
def test_a_summary_naming_this_seat_does_not_outvote_another_seats_real_prompt(tmp_path, flag, lead):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(str(proj / "foreign.jsonl"), [
        _summary(lead + "Earlier marker: echo txxxx-shell-ok", flag),
        _prompt("echo uxxx-shell-ok\nuxxx's work"),
    ])
    assert _foreign_both(proj, "foreign.jsonl", "txxxx") == (True, True)
    picked = [os.path.basename(p) for p in tc.select_files(str(proj), "txxxx", cache={}, single_seat=True)]
    assert "foreign.jsonl" not in picked


def test_a_meta_record_naming_this_seat_does_not_outvote_another_seats_real_prompt(tmp_path):
    """Precedence demotes isMeta like a summary. The meta record names the seat by its launcher alias (bxxxxx for
    memory-bxxxxx), so the loader's early return on its configured marker is not what decides it."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(str(proj / "foreign.jsonl"), [
        _summary("Earlier marker: echo bxxxxx-shell-ok", "isMeta"),
        _prompt("echo uxxx-shell-ok\nuxxx's work"),
    ])
    loader = tc._has_foreign_seat_marker(str(proj / "foreign.jsonl"), {"memory-bxxxxx", "bxxxxx"},
                                         [b"echo memory-bxxxxx-shell-ok"])
    worker = lh._transcript_has_foreign_marker(str(proj), "foreign.jsonl", "memory-bxxxxx")
    assert (loader, worker) == (True, True)


def test_the_negative_control_is_admitted_without_the_foreign_prompt(tmp_path):
    """The same file minus the other seat's real prompt is this seat's continuation, so the test above fails for the
    foreign prompt and not for the summary record alone."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(str(proj / "own-cont.jsonl"), [
        _summary(CONT + "Earlier marker: echo txxxx-shell-ok"),
        _prompt("carry on"),
    ])
    assert _foreign_both(proj, "own-cont.jsonl", "txxxx") == (False, False)
    picked = [os.path.basename(p) for p in tc.select_files(str(proj), "txxxx", cache={}, single_seat=True)]
    assert picked == ["own-cont.jsonl"]


def test_summary_only_markers_still_decide_a_continuation(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(str(proj / "uxxx-cont.jsonl"), [_summary(CONT + "Earlier marker: echo uxxx-shell-ok"), _prompt("go on")])
    _write(str(proj / "both-cont.jsonl"), [
        _summary(CONT + "echo txxxx-shell-ok, and it quoted echo uxxx-shell-ok"), _prompt("go on", 2)])
    assert _foreign_both(proj, "uxxx-cont.jsonl", "txxxx") == (True, True)       # another seat's continuation
    assert _foreign_both(proj, "both-cont.jsonl", "txxxx") == (False, False)     # own continuation quoting another


def test_a_real_own_prompt_beats_a_summary_naming_another_seat(tmp_path):
    """The prompt names the seat by its launcher alias (bxxxxx for memory-bxxxxx), which is in `own` but is not the
    configured marker, so the loader's early return on its own marker does not decide it: the precedence rule does."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(str(proj / "own.jsonl"), [
        _summary(CONT + "Earlier marker: echo uxxx-shell-ok"),
        _prompt("echo bxxxxx-shell-ok\nmy work"),
    ])
    loader = tc._has_foreign_seat_marker(str(proj / "own.jsonl"), {"memory-bxxxxx", "bxxxxx"},
                                         [b"echo memory-bxxxxx-shell-ok"])
    worker = lh._transcript_has_foreign_marker(str(proj), "own.jsonl", "memory-bxxxxx")
    assert (loader, worker) == (False, False)


def test_codex_p2_control_a_real_own_prompt_still_counts_as_own(tmp_path):
    """kxxx's control (8f6446): a real own prompt next to another seat's real prompt is still own."""
    project = tmp_path / "project"
    project.mkdir()
    both = project / "both.jsonl"
    both.write_text(json.dumps({"type": "user", "uuid": "b-0", "sessionId": "s", "timestamp": "2026-10-23T08:00:00.000Z",
                                "message": {"role": "user", "content": "echo uxxx-shell-ok"}}) + "\n"
                    + json.dumps({"type": "user", "uuid": "b-1", "sessionId": "s", "timestamp": "2026-10-23T08:00:01.000Z",
                                  "message": {"role": "user", "content": "echo txxxx-shell-ok"}}) + "\n", encoding="utf-8")
    assert not tc._has_foreign_seat_marker(str(both), {"txxxx"}, [b"echo txxxx-shell-ok"])
    assert not lh._transcript_has_foreign_marker(str(project), "both.jsonl", "txxxx")


def _peer_message(text):
    """The shape of the record HQB found (23 Oct 2026): a cross-session peer message, written by the harness."""
    return {"type": "user", "uuid": "u-peer", "sessionId": "s1", "timestamp": "2026-10-23T08:05:00.000Z",
            "isMeta": True, "promptSource": "system",
            "origin": {"kind": "peer", "from": "uds:pipe-synthetic", "name": "another-session"},
            "message": {"role": "user", "content": text}}


def test_a_peer_message_quoting_a_seats_marker_does_not_give_that_seat_another_seats_transcript(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(str(proj / "uxxx-session.jsonl"), [
        _prompt("echo uxxx-shell-ok\nuxxx's work"),
        _peer_message("bxxxxx here: my only Bash call today was the plain echo bxxxxx-shell-ok at start."),
    ])
    marker = "echo bxxxxx-shell-ok"
    assert not tc._marker_in_user_prompt(
        json.dumps(_peer_message("quoting echo bxxxxx-shell-ok")).encode(), marker.encode())
    assert tc._has_foreign_seat_marker(str(proj / "uxxx-session.jsonl"), {"memory-bxxxxx", "bxxxxx"},
                                       [marker.encode()]) is True
    assert lh._transcript_has_foreign_marker(str(proj), "uxxx-session.jsonl", "memory-bxxxxx") is True
    picked = [os.path.basename(p) for p in
              tc.select_files(str(proj), "memory-bxxxxx", cache={}, extra_markers=[marker])]
    assert "uxxx-session.jsonl" not in picked


def test_the_control_a_typed_marker_prompt_is_still_picked(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(str(proj / "bxxxxx-session.jsonl"), [_prompt("echo bxxxxx-shell-ok\nmy work")])
    picked = [os.path.basename(p) for p in
              tc.select_files(str(proj), "memory-bxxxxx", cache={}, extra_markers=["echo bxxxxx-shell-ok"])]
    assert picked == ["bxxxxx-session.jsonl"]
