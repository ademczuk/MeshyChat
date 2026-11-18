"""read_file, the loader's one parsing entry point (tools/transcript_cards/transcript_cards.py), and approved_cards.

A caller with its own bounded reader and its own transaction (the capture queue of #194) reads a transcript in
windows and commits the checkpoint read_file returns together with the cards. What is held here:

  * windows change nothing: one window, or one line per window with the checkpoint carried between them, give the
    same cards, and both give the cards run() gives for the same file;
  * every piece of state that crosses a window is in the checkpoint: a Claude Code prefilled ask, a Codex user item
    whose event is not read yet (the offset stays at the item), a Kimi open ask;
  * a new generation (new_checkpoint) carries no pending ask of the old one;
  * refusals change nothing: another schema, loader version, id rule or dialect is CheckpointIncompatible; more
    pending prefilled asks than allowed is PendingStateFull; a record out of order or without its newline is a
    ValueError; the checkpoint passed in is never changed;
  * approved_cards screens with the loader's detectors: a raw that trips one is a drop row and never a card.

Every record is synthetic. Run:  python -m pytest tests/test_transcript_cards_read_file.py -q -p no:cacheprovider
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "transcript_cards"))

import transcript_cards as tc  # noqa: E402

SEAT = "fixture"
QS = [{"question": "Which store do we use for the cache?", "header": "Store", "multiSelect": False,
       "options": [{"label": "SQLite", "description": "one file"}, {"label": "Postgres", "description": "a server"}]}]


def _rec(uuid, parent, rtype, content, t, **more):
    return {"type": rtype, "uuid": uuid, "parentUuid": parent, "sessionId": "11111111-2222-3333-4444-555555555555",
            "timestamp": t, "version": "2.1.294", "message": {"role": rtype, "content": content}, **more}


def claude_records(prefilled=False):
    ask_input = {"questions": QS, **({"answers": {QS[0]["question"]: "SQLite"}} if prefilled else {})}
    return [
        _rec("u-1", None, "user", "keep the cache local and do not add a server", "2026-10-10T07:00:00.000Z"),
        _rec("a-1", "u-1", "assistant", [{"type": "tool_use", "id": "toolu_q1", "name": "AskUserQuestion",
                                          "input": ask_input}], "2026-10-10T07:00:05.000Z"),
        _rec("u-2", "a-1", "user", [{"type": "tool_result", "tool_use_id": "toolu_q1", "content": "answered"}],
             "2026-10-10T07:09:00.000Z", toolUseResult={"questions": QS, "answers": {QS[0]["question"]: "SQLite"}}),
        _rec("a-2", "u-2", "assistant", [{"type": "text", "text": "Using SQLite for the cache."}],
             "2026-10-10T07:09:10.000Z"),
    ]


def lines_of(records):
    """(offset, bytes) for each record as one JSON line, offsets from 0."""
    out, offset = [], 0
    for r in records:
        b = (json.dumps(r) + "\n").encode("utf-8")
        out.append((offset, b))
        offset += len(b)
    return out


def read_windows(lines, checkpoint, sizes, **kw):
    """Read `lines` through read_file in windows of `sizes` lines, each starting at the checkpoint's offset, as a
    queue does. Returns (all raws, the last checkpoint)."""
    raws, cp = [], checkpoint
    by_offset = {off: i for i, (off, _) in enumerate(lines)}
    for size in sizes:
        start = by_offset.get(cp["offset"], len(lines))
        window = lines[start:start + size]
        got, _, cp = tc.read_file(window, cp, seat=SEAT, **kw)
        raws.extend(got)
    return raws, cp


def card_set(raws):
    cards, drops = tc.approved_cards(raws, SEAT, "f.jsonl")
    return {(c["type"], cid, c["source"]["source_sha256"]) for cid, c in cards.items()}, drops


def test_windows_change_nothing_and_match_run(tmp_path):
    lines = lines_of(claude_records())
    one, cp1 = read_windows(lines, tc.new_checkpoint("claude", {}), [len(lines)])
    many, cpn = read_windows(lines, tc.new_checkpoint("claude", {}), [1] * len(lines))
    assert card_set(one) == card_set(many)
    assert cp1["offset"] == cpn["offset"] == lines[-1][0] + len(lines[-1][1])
    assert sorted(t for t, _, _ in card_set(one)[0]) == ["Candidate", "Ruling"]
    # the same file through run(): the same ids and the same content hashes
    project, store = tmp_path / "p", tmp_path / "s"
    project.mkdir()
    (project / "11111111-2222-3333-4444-555555555555.jsonl").write_bytes(b"".join(b for _, b in lines))
    tc.run(str(project), SEAT, str(store), all_files=True)
    ran = [json.loads(x) for x in (store / "cards.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert {(c["type"], c["id"], c["source"]["source_sha256"]) for c in ran} == card_set(one)[0]


def test_a_prefilled_ask_crosses_a_window_and_makes_no_ruling():
    lines = lines_of(claude_records(prefilled=True))
    raws, cp = read_windows(lines, tc.new_checkpoint("claude", {}), [2, 2], prune_answered=False)
    assert [r["type"] for r in raws] == ["Candidate"]              # the call is in window 1, its result in window 2
    assert cp["prefilled_asks"] == ["toolu_q1"]
    raws, cp = read_windows(lines, tc.new_checkpoint("claude", {}), [2, 2])
    assert [r["type"] for r in raws] == ["Candidate"] and cp["prefilled_asks"] == []   # pruned once its result is read


def test_more_pending_prefilled_asks_than_allowed_is_refused_and_changes_nothing():
    lines = lines_of(claude_records(prefilled=True))
    cp = tc.new_checkpoint("claude", {})
    before = copy.deepcopy(cp)
    with pytest.raises(tc.PendingStateFull):
        tc.read_file(lines[:2], cp, seat=SEAT, max_pending=0)
    assert cp == before
    _, _, ok = tc.read_file(lines[:2], cp, seat=SEAT, max_pending=1)
    assert ok["prefilled_asks"] == ["toolu_q1"]


@pytest.mark.parametrize("field,value", [("schema", 99), ("loader_version", "0.0"), ("id_rule", -1),
                                         ("dialect", "gemini"), ("offset", -1), ("meta", None)])
def test_a_checkpoint_that_is_not_this_loaders_is_refused(field, value):
    cp = dict(tc.new_checkpoint("claude", {}), **{field: value})
    with pytest.raises(tc.CheckpointIncompatible):
        tc.read_file([], cp, seat=SEAT)


def test_a_record_out_of_order_or_without_its_newline_is_refused():
    lines = lines_of(claude_records())
    with pytest.raises(ValueError):
        tc.read_file(lines[1:], tc.new_checkpoint("claude", {}), seat=SEAT)       # starts past offset 0
    off, b = lines[0]
    with pytest.raises(ValueError):
        tc.read_file([(off, b[:-1])], tc.new_checkpoint("claude", {}), seat=SEAT)


def _cx_meta():
    return {"timestamp": "2026-10-08T05:00:00.000Z", "type": "session_meta",
            "payload": {"id": "S-cx", "source": "cli", "originator": "codex-tui", "cwd": "C:/x", "cli_version": "0.161.0"}}


def _cx_item(text, ts="2026-10-08T05:21:00.000Z"):
    return {"timestamp": ts, "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


def _cx_event(text, ts="2026-10-08T05:21:00.000Z"):
    return {"timestamp": ts, "type": "event_msg",
            "payload": {"type": "user_message", "message": text, "images": [], "local_images": [], "text_elements": []}}


def test_a_codex_item_whose_event_is_not_read_holds_the_offset_and_makes_one_card(tmp_path):
    text = "hold the deploy until the gate is green"
    path = tmp_path / "rollout-2026-10-08T05-00-00-aaaa.jsonl"
    records = [_cx_meta(), _cx_item(text), _cx_event(text)]
    path.write_bytes(b"".join(b for _, b in lines_of(records)))
    dialect, meta = tc.enroll(str(path), kimi=False)
    assert (dialect, meta) == ("codex", {"session": "S-cx"})
    lines = lines_of(records)
    # window 1 ends on the item: the offset stays AT the item, so the next window reads it again with its event
    raws1, _, cp = tc.read_file(lines[:2], tc.new_checkpoint(dialect, meta), seat=SEAT)
    assert cp["offset"] == lines[1][0]
    raws2, _, cp = tc.read_file(lines[1:], cp, seat=SEAT)
    assert cp["offset"] == lines[-1][0] + len(lines[-1][1])
    whole, _, _ = tc.read_file(lines, tc.new_checkpoint(dialect, meta), seat=SEAT)
    assert card_set(raws1 + raws2)[0] == card_set(whole)[0] and len(card_set(whole)[0]) == 1


def _k_msg(text, ts):
    return {"type": "context.append_message", "time": ts,
            "message": {"role": "user", "id": "m-" + str(ts), "content": [{"type": "text", "text": text}],
                        "origin": {"kind": "user"}}}


def _k_call(ts):
    return {"type": "context.append_loop_event", "time": ts,
            "event": {"type": "tool.call", "uuid": "ev-k1", "toolCallId": "k1", "name": "AskUserQuestion",
                      "args": {"questions": [{"question": "Deploy on Friday?", "header": "h",
                                              "options": [{"label": "Deploy", "description": "ship it"},
                                                          {"label": "Hold", "description": "wait"}]}]}}}


def _k_result(ts):
    return {"type": "context.append_loop_event", "time": ts,
            "event": {"type": "tool.result", "toolCallId": "k1",
                      "result": {"output": json.dumps({"answers": {"Deploy on Friday?": "Hold"}}), "isError": False}}}


def test_a_kimi_open_ask_crosses_a_window_but_never_a_new_generation():
    t0 = 1787064216000
    records = [{"type": "metadata", "protocol_version": "1.5", "created_at": t0 - 1000},
               _k_msg("plan the release", t0), _k_call(t0 + 1000), _k_result(t0 + 2000)]
    lines = lines_of(records)
    meta = {"session": "00000000-1111-2222-3333-444444444444", "subagent": False}
    raws1, _, cp = tc.read_file(lines[:3], tc.new_checkpoint("kimi", meta), seat=SEAT)
    assert list(cp["open_asks"]) == ["k1"]
    raws2, _, cp = tc.read_file(lines[3:], cp, seat=SEAT)
    assert [r["type"] for r in raws2] == ["Ruling"] and cp["open_asks"] == {}
    # a replaced file is a new generation: its answer alone, read against a fresh checkpoint, is no Ruling
    lone = lines_of([records[0], _k_result(t0 + 2000)])
    raws3, _, _ = tc.read_file(lone, tc.new_checkpoint("kimi", meta), seat=SEAT)
    assert [r for r in raws3 if r["type"] == "Ruling"] == []


def test_the_checkpoint_passed_in_is_never_changed():
    lines = lines_of(claude_records(prefilled=True))
    cp = tc.new_checkpoint("claude", {})
    before = copy.deepcopy(cp)
    tc.read_file(lines, cp, seat=SEAT)
    assert cp == before


def test_approved_cards_drops_a_raw_that_trips_a_detector():
    secret = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA" + "A" * 60 + "\n-----END RSA PRIVATE KEY-----"
    records = [_rec("u-9", None, "user", "store this key: " + secret, "2026-10-10T07:00:00.000Z")]
    raws, _, _ = tc.read_file(lines_of(records), tc.new_checkpoint("claude", {}), seat=SEAT)
    assert len(raws) == 1                                        # read_file itself does not screen
    cards, drops = tc.approved_cards(raws, SEAT, "f.jsonl")
    assert cards == {} and len(drops) == 1
    row = next(iter(drops.values()))
    assert row["detectors"] and "BEGIN" not in json.dumps(row)    # named by detector, never by text
