"""test_context_upload_tail.py - tail-first catch-up in tools/transcript_cards/context_upload.py (TailEnrollment).

A file with a backlog is cut at a line start near its end. The bytes from the cut go first, as a tail lane with its
own wire src, and the file's own lane fills in up to the cut and stops. The property every test leans on: the head
lane's items, then the tail lane's, are the items one lane would make from the same file (the same records, once
each, in order). The same fakes as tests/test_context_upload.py: no server, no database, the real parser, outbox,
validator and store.
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_context_upload import (LATER, SEAT, Server, _rec, cfg, expected_items, idle, meta, said,  # noqa: E402
                                 user, write_lines)
import context_parser as cp  # noqa: E402
import context_upload as cu  # noqa: E402

WINDOW, THRESHOLD = 2_000, 1_000          # bytes: small, so a fixture file of a few dozen lines has a backlog


def tcfg(tmp_path, src, **kw):
    kw.setdefault("tail_window_bytes", WINDOW)
    kw.setdefault("tail_threshold_bytes", THRESHOLD)
    return cfg(tmp_path, src, **kw)


def backlog(path, n=60, session="s-tail"):
    write_lines(path, [meta(session)] + [user(i, "question %03d" % i) if i % 2 else said(i, "answer %03d" % i)
                                         for i in range(n)])
    idle(path)


def drain(tmp_path, src, server, passes=200, **kw):
    for _ in range(passes):
        report = cu.run_pass(tcfg(tmp_path, src, **kw), server, now=LATER)
        assert not report.conflicts, report.conflicts
        if report.status() == "drained":
            return report
    raise AssertionError("never drained")


def by_src(server):
    lanes = {}
    for key, value in server.rows.items():
        if key.startswith("ctx/"):
            lanes.setdefault(value["src"], []).append(value)
    for rows in lanes.values():
        rows.sort(key=lambda v: (v["gen"], v["batch"], v["ordinal"]))
    return lanes


def shape(items):
    """Everything an item says, but where it sits: its src (the lane), batch and ordinal."""
    return [{k: v for k, v in i.items() if k not in ("src", "batch", "ordinal")} for i in items]


def enrollment(tmp_path, src):
    e = cu.Enrollment(tmp_path / "store", SEAT, "codex", src)
    return e


def lanes_of(tmp_path, src, server):
    """(head rows, tail rows), split by the tail's recorded wire src."""
    tail_src = enrollment(tmp_path, src).record.get("cut_src")
    lanes = by_src(server)
    tail = lanes.pop(tail_src, []) if tail_src else []
    assert len(lanes) <= 1, "more than one non-tail src"
    head = next(iter(lanes.values()), [])
    return head, tail


# ---- controls -----------------------------------------------------------------------------------------------------

def test_a_file_under_the_threshold_has_one_lane_as_before(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src, n=4)
    assert src.stat().st_size <= THRESHOLD, "control: the fixture really is under the threshold"
    server = Server()
    drain(tmp_path, src, server)
    assert enrollment(tmp_path, src).cut is None
    assert len(by_src(server)) == 1
    assert shape(next(iter(by_src(server).values()))) == shape(expected_items(src))


def test_the_window_off_means_one_lane_however_large_the_backlog(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server, tail_window_bytes=0)
    assert enrollment(tmp_path, src).cut is None and len(by_src(server)) == 1


# ---- the cut ------------------------------------------------------------------------------------------------------

def test_a_backlog_is_cut_at_a_line_start_inside_the_window_and_the_tail_goes_first(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    size = src.stat().st_size
    server = Server()
    first = cu.run_pass(tcfg(tmp_path, src, max_batches=1), server, now=LATER)
    e = enrollment(tmp_path, src)
    data = src.read_bytes()
    assert e.cut is not None and size - WINDOW <= e.cut < size and data[e.cut - 1:e.cut] == b"\n"
    assert e.record["cut_sha256"] == cu._cut_sha(src, e.cut)
    # one unit a pass (a freeze, then each send): every unit goes to the tail lane until it has nothing left, so the
    # first several writes are all the tail's, never the backlog from offset 0
    assert first.emitted and not server.writes          # pass 1's one unit froze the tail's first batch
    for _ in range(6):
        cu.run_pass(tcfg(tmp_path, src, max_batches=1), server, now=LATER)
    assert len(server.writes) == 6
    assert {k.split("/")[2] for k in server.writes} == {e.record["cut_src"]}, server.writes


def test_head_then_tail_is_exactly_what_one_lane_makes_nothing_lost_nothing_doubled(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server, max_batches=3)
    head, tail = lanes_of(tmp_path, src, server)
    assert head and tail, "both lanes made items"
    assert shape(head) + shape(tail) == shape(expected_items(src))
    # each lane is its own src: batch 1 starts at ordinal 0, ordinals contiguous (the server's manifest order rule)
    for rows in (head, tail):
        assert [r["ordinal"] for r in rows] == list(range(len(rows)))
        assert rows[0]["batch"] == 1
    manifests = [k for k in server.rows if k.startswith("ctxm/")]
    tail_src = enrollment(tmp_path, src).record["cut_src"]
    assert any(k.split("/")[2] == tail_src for k in manifests), "the tail lane committed a manifest"


def test_a_codex_tail_that_starts_past_session_meta_still_has_its_identity(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    report = drain(tmp_path, src, server)
    _head, tail = lanes_of(tmp_path, src, server)
    assert tail and all(r["src"] == enrollment(tmp_path, src).record["cut_src"] for r in tail)
    assert "no_session_id" not in report.excluded


def test_the_own_lane_stops_at_the_cut_and_new_lines_go_to_the_tail(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server)
    head_before, tail_before = lanes_of(tmp_path, src, server)
    write_lines(src, [user(900, "after the cut"), said(901, "still the tail")], mode="a")
    idle(src)
    drain(tmp_path, src, server)
    head, tail = lanes_of(tmp_path, src, server)
    assert head == head_before, "the own lane read past its cut"
    assert [r.get("text") for r in tail[len(tail_before):]] == ["after the cut", "still the tail"]
    assert shape(head) + shape(tail) == shape(expected_items(src))


@pytest.mark.parametrize("seed", range(8))
def test_random_files_and_random_windows_union_to_one_lane(tmp_path, seed):
    rng = random.Random(seed)
    src = tmp_path / "rollout.jsonl"
    n = rng.randrange(20, 90)
    write_lines(src, [meta("s-%d" % seed)] + [
        (user if rng.random() < 0.5 else said)(i, "x" * rng.randrange(1, 300)) for i in range(n)])
    idle(src)
    window = rng.randrange(300, 6_000)
    server = Server()
    drain(tmp_path, src, server, tail_window_bytes=window, tail_threshold_bytes=rng.randrange(100, 3_000),
          max_batches=rng.choice([1, 2, 5, None]), batch_items=rng.choice([3, 7, 256]))
    head, tail = lanes_of(tmp_path, src, server)
    assert shape(head) + shape(tail) == shape(expected_items(src))


# ---- an enrollment from before this version ----------------------------------------------------------------------

def test_an_enrollment_already_part_way_in_gets_its_cut_past_what_it_committed(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src, n=80)
    server = Server()
    for _ in range(4):                                     # the old behaviour: one lane, part way in
        cu.run_pass(tcfg(tmp_path, src, tail_window_bytes=0, max_batches=2, batch_items=4), server, now=LATER)
    assert enrollment(tmp_path, src).cut is None
    committed = len(server.rows)
    assert committed
    drain(tmp_path, src, server, batch_items=4)
    e = enrollment(tmp_path, src)
    assert e.cut is not None
    head, tail = lanes_of(tmp_path, src, server)
    assert shape(head) + shape(tail) == shape(expected_items(src))


# ---- no cut where a tail cannot stand alone ------------------------------------------------------------------------

def test_a_subagent_source_is_never_cut(tmp_path):
    src = tmp_path / "rollout.jsonl"
    sub = _rec("session_meta", {"id": "s-sub", "originator": "codex-tui", "source": {"subagent": "review"}})
    write_lines(src, [sub] + [user(i, "sub %03d" % i) for i in range(60)])
    idle(src)
    server = Server()
    drain(tmp_path, src, server)
    assert enrollment(tmp_path, src).cut is None


# ---- a rewrite --------------------------------------------------------------------------------------------------

def test_bytes_changed_just_before_the_cut_open_the_next_generation_for_both_lanes(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server)
    e = enrollment(tmp_path, src)
    cut = e.cut
    data = bytearray(src.read_bytes())
    i = data.rindex(b"answer", 0, cut)                    # same length, different bytes, inside the fingerprint
    data[i:i + 6] = b"ANSWER"
    src.write_bytes(bytes(data))
    idle(src)
    drain(tmp_path, src, server)
    e = enrollment(tmp_path, src)
    assert e.gen == 2
    gens = {v["gen"] for v in server.rows.values() if isinstance(v, dict) and "gen" in v}
    assert gens == {1, 2}


def test_a_truncation_below_the_cut_opens_the_next_generation(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server)
    cut = enrollment(tmp_path, src).cut
    data = src.read_bytes()
    src.write_bytes(data[:data.rindex(b"\n", 0, cut - 1) + 1])
    idle(src)
    report = drain(tmp_path, src, server)
    assert enrollment(tmp_path, src).gen == 2 and not report.conflicts


def test_bytes_changed_after_the_cut_do_not_touch_the_own_lane(tmp_path):
    # control for the fingerprint: only the bytes just BEFORE the cut decide, a change in the tail is the tail's own
    # rewrite check's business (the tail lane appends; an in-place edit past every committed offset is just unread)
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    cu.run_pass(tcfg(tmp_path, src, max_batches=1), server, now=LATER)
    e = enrollment(tmp_path, src)
    data = bytearray(src.read_bytes())
    i = data.index(b"question", e.cut)
    data[i:i + 8] = b"QUESTION"
    src.write_bytes(bytes(data))
    idle(src)
    own = cu.SourcePass(tcfg(tmp_path, src), src, Server(), cu.Report(SEAT), LATER, cu.Budget(0))
    own.plan_tail()
    assert own.rewritten is False


# ---- F3 calls open across the cut -------------------------------------------------------------------------------

def _call(call_id, stamp="2026-10-10T11:00:00Z"):
    return _rec("response_item", {"type": "function_call", "call_id": call_id, "name": "shell",
                                  "arguments": json.dumps({"command": ["echo", call_id]})}, stamp)


def _output(call_id, stamp="2026-10-10T11:00:01Z"):
    return _rec("response_item", {"type": "function_call_output", "call_id": call_id, "output": "ok " + call_id},
                stamp)


def test_f3_a_call_whose_result_lies_past_the_cut_drains_without_a_conflict(tmp_path):
    src = tmp_path / "rollout.jsonl"
    records = [meta("s-f3")]
    for i in range(40):
        records += [user(i, "q %03d" % i), _call("c%03d" % i), _output("c%03d" % i)]
    write_lines(src, records)
    idle(src)
    server = Server()
    report = drain(tmp_path, src, server, f3=True)
    e = enrollment(tmp_path, src)
    assert e.cut is not None and not report.conflicts
    head, tail = lanes_of(tmp_path, src, server)
    assert head and tail
    assert any(r.get("item") == "tool_exchange" for r in tail), "the tail lane carries tool output under F3"


def test_f3_the_own_lane_flushes_a_call_open_at_the_cut_even_while_the_file_is_live(tmp_path):
    # The cut lands exactly between a call and its output. The file is LIVE (written just now), so only the own
    # lane's "what lies before the cut is final" rule lets it flush that call result-missing and finish.
    src = tmp_path / "rollout.jsonl"
    records = [meta("s-live")] + [user(i, "q %03d" % i) for i in range(30)] + [_call("cX")]
    write_lines(src, records)
    at_output = src.stat().st_size
    write_lines(src, [_output("cX")] + [user(100 + i, "later %03d" % i) for i in range(10)], mode="a")
    live = LATER.timestamp()
    os.utime(src, (live, live))
    window = src.stat().st_size - at_output
    server = Server()
    for _ in range(60):
        cu.run_pass(tcfg(tmp_path, src, f3=True, tail_window_bytes=window, tail_threshold_bytes=100), server,
                    now=LATER)
    assert enrollment(tmp_path, src).cut == at_output
    head, _tail = lanes_of(tmp_path, src, server)
    calls = [r for r in head if r.get("item") == "tool_exchange"]
    assert len(calls) == 1 and calls[0].get("result_status") == "missing", calls


def test_every_record_is_counted_once_across_both_lanes(tmp_path):
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    lines = sum(1 for line in src.read_bytes().splitlines() if line.strip())
    server = Server()
    emitted = excluded = 0
    for _ in range(200):
        report = cu.run_pass(tcfg(tmp_path, src, max_batches=2), server, now=LATER)
        emitted += report.records_emitted
        excluded += sum(report.excluded.values())
        if report.status() == "drained":
            break
    assert emitted + excluded == lines


def test_a_truncation_below_the_cut_before_either_lane_froze_anything_is_still_a_rewrite(tmp_path):
    # Only the cut's own check can see this: the tail lane has no committed offset to be truncated below, and the
    # file's own lane has not read as far as the truncation.
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    own = cu.SourcePass(tcfg(tmp_path, src), src, Server(), cu.Report(SEAT), LATER, cu.Budget(0))
    own.plan_tail()
    cut = enrollment(tmp_path, src).cut
    assert cut is not None
    data = src.read_bytes()
    src.write_bytes(data[:data.rindex(b"\n", 0, cut - 1) + 1])
    idle(src)
    drain(tmp_path, src, Server())
    assert enrollment(tmp_path, src).gen == 2


# ---- round 2: a reviewer 246a0d and GitHub Codex r4435554091 / r4435554097 --------------------------------------

def plan_at(call, steps):
    return _rec("response_item", {"type": "function_call", "call_id": call, "name": "update_plan",
                                  "arguments": json.dumps({"plan": [{"step": s, "status": st} for s, st in steps]})})


def test_a_plan_that_spans_the_cut_reads_as_one_lane_would(tmp_path):
    # Codex r4435554097: a step added before the cut and completed after it is a completion, not a second add; a step
    # dropped in the first plan after the cut is a removal.
    src = tmp_path / "rollout.jsonl"
    before = [meta("s-plan"), plan_at("p1", [("write the test", "pending"), ("fix it", "pending"),
                                             ("drop me", "pending")])]
    before += [user(i, "work %03d" % i) for i in range(40)]
    write_lines(src, before)
    at = src.stat().st_size
    write_lines(src, [plan_at("p2", [("write the test", "completed"), ("fix it", "in_progress")])]
                + [said(100 + i, "later %03d" % i) for i in range(5)], mode="a")
    idle(src)
    server = Server()
    drain(tmp_path, src, server, tail_window_bytes=src.stat().st_size - at, tail_threshold_bytes=100)
    assert enrollment(tmp_path, src).cut == at
    head, tail = lanes_of(tmp_path, src, server)
    assert tail and shape(head) + shape(tail) == shape(expected_items(src))


def test_outside_the_tail_dialects_a_file_keeps_one_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "TAIL_DIALECTS", ("claude",))
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    drain(tmp_path, src, Server())
    assert enrollment(tmp_path, src).cut is None


def test_no_cut_when_the_first_item_lies_past_the_identity_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "CUT_IDENTITY_BYTES", 64)    # shorter than the first line: identity never settles
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    drain(tmp_path, src, Server())
    assert enrollment(tmp_path, src).cut is None


def test_a_src_named_by_the_path_is_not_enough_the_subagent_flag_must_settle_first(tmp_path, monkeypatch):
    # a reviewer 246a0d P1: an Antigravity log names its session in its PATH, so the parser has a src before it has
    # read step 0, the record that makes it a subagent. The cut decision must read on until the first item.
    monkeypatch.setattr(cu, "TAIL_DIALECTS", ("antigravity",))
    d = tmp_path / "brain" / "4f2a1c3e-9b8d-4e7f-a6c5-1d2e3f4a5b6c" / ".system_generated" / "logs"
    d.mkdir(parents=True)
    src = d / "overview.txt.jsonl"
    rows = [{"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "[Message] sender=task-7 go"}]
    rows += [{"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "thinking %03d" % i,
              "created_at": "2026-10-10T11:00:00Z"} for i in range(1, 80)]
    write_lines(src, rows)
    idle(src)
    own = cu.SourcePass(cu.Config(seat=SEAT, dialect="antigravity", store_dir=tmp_path / "store", sources=[str(src)],
                                  tail_window_bytes=WINDOW, tail_threshold_bytes=THRESHOLD),
                        src, Server(), cu.Report(SEAT), LATER, cu.Budget(0))
    assert own.snapshot.parser_state()["parser"]["src"] is not None, "control: the path named the src up front"
    own.plan_tail()
    assert own.enrollment.cut is None


def test_after_a_rewrite_the_tail_keeps_its_src_and_its_new_generation_supersedes_the_old(tmp_path):
    # Codex r4435554091: the file is truncated to a short transcript after a split. The old tail's gen-1 rows must
    # not stay the newest gen of their src: the next generation writes the SAME tail src at gen 2.
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server)
    tail_src = enrollment(tmp_path, src).record["cut_src"]
    write_lines(src, [meta("s-tail"), user(1, "short again"), said(2, "and small"), user(3, "the end")])
    idle(src)
    assert src.stat().st_size < THRESHOLD                 # no backlog: only "it had a tail" can cut it
    drain(tmp_path, src, server)
    e = enrollment(tmp_path, src)
    assert e.gen == 2 and e.cut is not None and e.record["cut_src"] == tail_src
    lanes = by_src(server)
    assert len(lanes) == 2 and tail_src in lanes, "a third src appeared"
    assert max(r["gen"] for r in lanes[tail_src]) == 2
    # and the own lane wrote gen 2 too: a cut after a lone session_meta would have left it nothing (c18b54 P1-1)
    head_src = next(k for k in lanes if k != tail_src)
    assert max(r["gen"] for r in lanes[head_src]) == 2


def test_the_next_generation_is_cut_before_its_own_lane_reads_from_zero(tmp_path):
    # a reviewer 246a0d P2 #2: opening gen 2 in run() must not send gen 2's backlog from offset 0 ahead of its tail.
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server)
    data = bytearray(src.read_bytes())
    i = data.index(b"answer")
    data[i:i + 6] = b"ANSWER"
    src.write_bytes(bytes(data))
    idle(src)
    before = len(server.writes)
    for _ in range(4):
        cu.run_pass(tcfg(tmp_path, src, max_batches=1), server, now=LATER)
    gen2 = [k for k in server.writes[before:] if k.split("/")[3] == "0002"]
    tail_src = enrollment(tmp_path, src).record["cut_src"]
    assert gen2 and all(k.split("/")[2] == tail_src for k in gen2), gen2


def test_a_settled_itemless_tail_is_never_counted_again_by_the_tail_lane(tmp_path):
    # a reviewer 246a0d P2 #3: with batch_items=3 the third question fills a batch and freezes at its line end, and
    # the item-less lines after it settle (counted) on an idle pass past that committed offset. When the file then
    # crosses the threshold with a window reaching back INTO those lines, the cut must land after them, not inside.
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-acc")] + [user(i, "q %02d" % i) for i in range(3)])
    committed_at = src.stat().st_size
    write_lines(src, [_rec("turn_context", {"cwd": "c:/x", "n": i}) for i in range(12)], mode="a")
    settled_at = src.stat().st_size
    idle(src)
    server = Server()
    counted = 0

    def passes(**kw):
        nonlocal counted
        for _ in range(100):
            r = cu.run_pass(tcfg(tmp_path, src, batch_items=3, **kw), server, now=LATER)
            counted += r.records_emitted + sum(r.excluded.values())
            if r.status() == "drained":
                return
        raise AssertionError("never drained")

    passes(tail_window_bytes=WINDOW, tail_threshold_bytes=10 ** 9)
    e = enrollment(tmp_path, src)
    assert e.cut is None and e.accounted_through() == settled_at > committed_at, "control: a settled item-less tail"
    write_lines(src, [said(100 + i, "more %03d" % i) for i in range(12)], mode="a")
    idle(src)
    size = src.stat().st_size
    passes(tail_window_bytes=size - (committed_at + 100), tail_threshold_bytes=100)
    e = enrollment(tmp_path, src)
    assert e.cut is not None and e.cut >= settled_at, (e.cut, committed_at, settled_at)
    lines = sum(1 for line in src.read_bytes().splitlines() if line.strip())
    assert counted == lines


def test_a_named_session_that_makes_no_item_inside_the_bound_is_not_cut(tmp_path, monkeypatch):
    # The bound ends just past session_meta: the src is named, but no record has made an item, so whether the source
    # is a subagent is not settled yet. Trusting the src alone is what a reviewer's 246a0d P1 was.
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    first_line = len(src.read_bytes().split(b"\n", 1)[0]) + 1
    monkeypatch.setattr(cu, "CUT_IDENTITY_BYTES", first_line + 10)
    own = cu.SourcePass(tcfg(tmp_path, src), src, Server(), cu.Report(SEAT), LATER, cu.Budget(0))
    assert own._identity_for_cut() == (None, False, False, 0)
    own.plan_tail()
    assert own.enrollment.cut is None


# ---- round 3: a reviewer c18b54 -----------------------------------------------------------------------------------

def test_a_rewrite_to_a_file_with_one_item_is_not_cut_so_no_lane_is_left_without_its_generation(tmp_path):
    # P1-1: nothing after any cut the file allows would make an item, so a cut would leave the tail lane writing no
    # gen 2 (its gen 1 staying current). The own lane takes the whole file instead.
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server)
    write_lines(src, [meta("s-tail"), user(1, "the only item")])
    idle(src)
    drain(tmp_path, src, server)
    e = enrollment(tmp_path, src)
    assert e.gen == 2 and e.cut is None
    gen2 = [v for k, v in server.rows.items() if k.startswith("ctx/") and v["gen"] == 2]
    assert [v.get("text") for v in gen2] == ["the only item"]


def test_an_edit_inside_what_the_tail_committed_is_a_rewrite(tmp_path):
    # P2-1: the tail lane's prefix is its own first bytes (from the cut), not the file's [0, 4096), which an edit past
    # the cut never touches. Same length, so only the prefix can see it.
    src = tmp_path / "rollout.jsonl"
    backlog(src, n=80)
    server = Server()
    drain(tmp_path, src, server)
    cut = enrollment(tmp_path, src).cut
    assert cut > cu.PREFIX_BYTES, "control: the file's own prefix lies wholly before the cut"
    data = bytearray(src.read_bytes())
    i = data.index(b"question", cut)
    data[i:i + 8] = b"QUESTION"
    src.write_bytes(bytes(data))
    idle(src)
    drain(tmp_path, src, server)
    assert enrollment(tmp_path, src).gen == 2


@pytest.mark.parametrize("chunk", [1, 7, 64, 1 << 20])
def test_lines_backwards_gives_every_line_newest_first_at_any_chunk_size(tmp_path, monkeypatch, chunk):
    monkeypatch.setattr(cu._Stream, "CHUNK", chunk)
    src = tmp_path / "lines.jsonl"
    rows = [b"a" * n for n in (3, 0, 17, 1, 40, 0, 5)]
    src.write_bytes(b"\n".join(rows) + b"\n")
    want = [r for r in reversed(rows) if r.strip()]
    assert list(cu._lines_backwards(src, src.stat().st_size)) == want


def test_lines_backwards_skips_a_line_past_its_cap_and_keeps_the_ones_around_it(tmp_path, monkeypatch):
    monkeypatch.setattr(cu._Stream, "CHUNK", 16)
    src = tmp_path / "lines.jsonl"
    src.write_bytes(b"before\n" + b"x" * 500 + b"\nafter\n")
    assert list(cu._lines_backwards(src, src.stat().st_size, max_line=100)) == [b"after", b"before"]


def test_a_plan_the_parser_refuses_before_the_cut_fails_closed_not_an_older_plan(tmp_path):
    # P2-2: the newest plan record before the cut holds more steps than the parser allows. Its own lane would stop
    # there; the tail must not seed itself from an older plan instead.
    src = tmp_path / "rollout.jsonl"
    rows = [meta("s-full"), plan_at("p0", [("old step", "pending")])]
    rows += [user(i, "work %03d" % i) for i in range(30)]
    rows += [plan_at("p1", [("step %03d" % i, "pending") for i in range(300)])]
    write_lines(src, rows)
    at = src.stat().st_size
    write_lines(src, [said(100 + i, "later %03d" % i) for i in range(5)], mode="a")
    idle(src)
    server = Server()
    report = cu.run_pass(tcfg(tmp_path, src, tail_window_bytes=src.stat().st_size - at, tail_threshold_bytes=100),
                         server, now=LATER)
    e = enrollment(tmp_path, src)
    assert e.cut == at
    assert {"src": None, "code": "plan_state_full"} in report.conflicts, report.conflicts
    # the tail lane refused at its enrollment: it sent nothing, rather than its items seeded from the older plan
    assert not [k for k in server.rows if k.split("/")[2] == e.record["cut_src"]], sorted(server.rows)


def test_after_a_rewrite_no_cut_when_only_itemless_lines_follow_the_first_item(tmp_path):
    # P1-1, its second half: a cut is possible (lines follow the first item) but nothing after it makes an item, so
    # the tail lane would write no gen 2. Not cut until something after the cut would.
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server)
    write_lines(src, [meta("s-tail"), user(1, "the only item")]
                + [_rec("turn_context", {"cwd": "c:/x", "n": i}) for i in range(3)])
    idle(src)
    drain(tmp_path, src, server)
    e = enrollment(tmp_path, src)
    assert e.gen == 2 and e.cut is None


def test_a_drained_short_rewrite_that_grows_a_little_is_cut_at_its_committed_offset(tmp_path):
    # a reviewer df63b1 P3-1: the one-item rewrite drains its own lane to the end of the file (no cut: nothing
    # follows the first item). New items then arrive, fewer than the window, so the floor is the committed offset
    # itself. The cut lands exactly there and the tail writes its next generation, rather than waiting for the window.
    src = tmp_path / "rollout.jsonl"
    backlog(src)
    server = Server()
    drain(tmp_path, src, server)
    tail_src = enrollment(tmp_path, src).record["cut_src"]
    write_lines(src, [meta("s-tail"), user(1, "the only item")])
    idle(src)
    drain(tmp_path, src, server)
    e = enrollment(tmp_path, src)
    assert e.gen == 2 and e.cut is None
    committed = src.stat().st_size
    write_lines(src, [said(2, "it grew"), user(3, "a little")], mode="a")
    idle(src)
    drain(tmp_path, src, server)
    e = enrollment(tmp_path, src)
    assert e.cut == committed and e.record["cut_src"] == tail_src
    tail_gen2 = [v.get("text") for k, v in sorted(server.rows.items())
                 if k.startswith("ctx/") and v["src"] == tail_src and v["gen"] == 2]
    assert tail_gen2 == ["it grew", "a little"]
