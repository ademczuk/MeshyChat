"""test_context_upload.py - one seat's transcripts into shared/transcript-context (tools/transcript_cards/context_upload.py).

No server and no database: `Server` keeps write-once rows as memory.write does and answers each read-back through
the real FrozenOutbox.observe, as authenticated_readback.reconcile_present does after its signature checks (those
have their own tests in tools/transcript_cards/test_authenticated_readback.py). Everything else is the real code:
the context parser, the screened outbox with the application's own validator, and the private SQLite store.

Each test is one rule of the ingest plan's step 3, and each "nothing happened" has its control beside it.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "transcript_cards"))
sys.path.insert(0, str(ROOT))

import context_parser as cp  # noqa: E402
import context_upload as cu  # noqa: E402

SEAT = "fixture-seat"
STAMP = "2026-10-10T11:00:00Z"
LATER = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _django():
    cu._configure_django()


# ---- the fixture transcript (Codex rollout shape, as tests/test_transcript_cards_context_parser.py builds it) -----

def _rec(kind, payload, stamp=STAMP):
    return {"type": kind, "timestamp": stamp, "payload": payload}


def meta(session):
    return _rec("session_meta", {"id": session, "originator": "codex-tui", "source": "cli"})


def user(n, text, stamp=STAMP):
    return _rec("response_item", {"type": "message", "id": "u%d" % n, "role": "user",
                                  "content": [{"type": "input_text", "text": text}]}, stamp)


def said(n, text, stamp=STAMP):
    return _rec("response_item", {"type": "message", "id": "a%d" % n, "role": "assistant", "phase": "final_answer",
                                  "content": [{"type": "output_text", "text": text}]}, stamp)


def plan(call, steps):
    return _rec("response_item", {"type": "function_call", "call_id": call, "name": "update_plan",
                                  "arguments": json.dumps({"plan": [{"step": s, "status": "pending"} for s in steps]})})


def write_lines(path, records, mode="w"):
    with open(path, mode, encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def idle(path):
    old = (LATER - timedelta(hours=1)).timestamp()
    os.utime(path, (old, old))


# ---- the server: write-once rows, and the read-back the outbox needs --------------------------------------------

class Server:
    def __init__(self):
        self.rows, self.writes = {}, []
        self.refuse = {}             # key -> typed refusal code
        self.lose_reply = set()      # the write lands, then the transport fails before the answer
        self.lose_write = set()      # the transport fails and the write never lands
        self.down = False

    def write(self, key, value):
        if self.down:
            raise cu.Unreachable("down")
        self.writes.append(key)
        if key in self.lose_write:
            self.lose_write.discard(key)
            raise cu.Unreachable("lost")
        if key in self.refuse:
            return {"status": "failed", "result_signature_verified": True, "error_code": self.refuse[key]}
        if key in self.rows:
            return {"status": "failed", "result_signature_verified": True, "error_code": "E_USE_MEMORY_EDIT"}
        self.rows[key] = value
        if key in self.lose_reply:
            self.lose_reply.discard(key)
            raise cu.Unreachable("reply lost")
        return {"status": "completed", "result_signature_verified": True, "result": {"key": key, "version": 1}}

    def reconcile(self, outbox, batch_key):
        if self.down:
            raise cu.Unreachable("down")
        action = outbox.next_action(batch_key)
        assert action.kind == "read"
        value = self.rows.get(action.record.key)
        artifact = json.dumps({"key": action.record.key, "token": action.attempt.token}).encode()
        return outbox.observe(action.attempt, value=None if value is None else cp.serialize(value),
                              evidence_sha256=hashlib.sha256(artifact).hexdigest(), evidence_artifact=artifact)


def cfg(tmp_path, source, seat=SEAT, **kw):
    return cu.Config(seat=seat, dialect="codex", store_dir=tmp_path / "store", sources=[str(source)], **kw)


def expected_items(path, batch_items=256, gen=1):
    parser, _ = cp.parse_file(str(path), SEAT, "codex", batch_items=batch_items, namespace=cu.NAMESPACE)
    return parser.items


def item_rows(server):
    return {k: v for k, v in server.rows.items() if k.startswith("ctx/")}


def three_turns(path, session="s-1"):
    write_lines(path, [meta(session), user(1, "first question"), said(1, "first answer"), user(2, "second")])
    idle(path)


# ---- end to end ---------------------------------------------------------------------------------------------------

def test_an_idle_source_is_uploaded_whole_and_a_second_pass_sends_nothing(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    items = expected_items(src)
    assert len(items) == 3
    assert item_rows(server) == {cp.item_key(i): i for i in items}, "the uploaded values are not the parser's"
    assert [k for k in server.rows if k.startswith("ctxm/")] == [cp.manifest_key(cp.manifests(items)[0])]
    assert (report.status(), report.exit_code(), report.batches_committed, report.items_committed,
            report.emitted, report.pending) == ("progressed", 0, 1, 3, 3, 0)
    sent = list(server.writes)
    again = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert server.writes == sent, "a second pass sent again"
    assert (again.status(), again.exit_code()) == ("drained", 0)


def test_items_go_before_their_manifest_and_batch_n_before_batch_n_plus_1(tmp_path):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-2")] + [user(n, "q%d" % n) for n in range(7)])
    idle(src)
    server = Server()
    cu.run_pass(cfg(tmp_path, src, batch_items=3), server, now=LATER)
    kinds = ["m" if k.startswith("ctxm/") else "i" for k in server.writes]
    assert kinds == ["i", "i", "i", "m", "i", "i", "i", "m", "i", "m"]
    batches = [int(k.split("/")[4]) for k in server.writes]
    assert batches == sorted(batches)


def test_a_live_partial_batch_waits_for_its_age_and_then_goes(tmp_path):
    src = tmp_path / "rollout.jsonl"
    now = datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_lines(src, [meta("s-3"), user(1, "still typing", fresh)])
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src), server, now=now)
    assert server.writes == [] and report.pending == 1, "a live batch was cut on the first pass"
    later = cu.run_pass(cfg(tmp_path, src), server, now=now + timedelta(minutes=6))
    assert len(item_rows(server)) == 1 and later.items_committed == 1


def test_a_partial_line_at_the_end_of_the_file_is_not_read(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    with open(src, "a", encoding="utf-8") as f:
        f.write(json.dumps(said(9, "half written"))[:20])
    idle(src)
    server = Server()
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert len(item_rows(server)) == 3


# ---- a line is never split across batches ---------------------------------------------------------------------------

def test_a_line_that_would_spill_is_cut_before_and_nothing_is_lost_or_doubled(tmp_path):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-4"), user(1, "one"), user(2, "two"), plan("c1", ["alpha", "beta", "gamma"]),
                      user(3, "three")])
    idle(src)
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, batch_items=4), server, now=LATER)
    items = item_rows(server)
    assert report.conflicts == []
    assert len(items) == 6 and sorted(v["ordinal"] for v in items.values()) == list(range(6))
    by_batch = {}
    for v in items.values():
        by_batch.setdefault(v["batch"], []).append(v)
    # batch 1 is the two user turns; the plan's three items start batch 2 together, never split from their line
    assert [v["item"] for v in sorted(by_batch[1], key=lambda v: v["ordinal"])] == ["user_text", "user_text"]
    assert [v["item"] for v in sorted(by_batch[2], key=lambda v: v["ordinal"])][:3] == ["task", "task", "task"]
    for manifest in (v for k, v in server.rows.items() if k.startswith("ctxm/")):
        assert manifest["items"] == [[v["ordinal"], v.get("text_sha256")] for v in
                                     sorted(by_batch[manifest["batch"]], key=lambda v: v["ordinal"])]


def test_a_record_with_more_items_than_a_batch_is_a_conflict_not_a_loss(tmp_path):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-5"), plan("c1", ["a1", "b2", "c3", "d4", "e5"])])
    idle(src)
    report = cu.run_pass(cfg(tmp_path, src, batch_items=4), Server(), now=LATER)
    assert (report.status(), report.exit_code()) == ("conflict", 4)


# ---- uncertainty: the read decides, and frozen bytes are sent again unchanged -----------------------------------

def test_a_reply_lost_after_the_write_landed_is_settled_by_the_read_without_a_second_write(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    first = cp.item_key(expected_items(src)[0])
    server = Server()
    server.lose_reply.add(first)
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (report.status(), report.exit_code()) == ("unreachable", 5)
    resumed = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert server.writes.count(first) == 1, "a landed write was sent twice"
    assert resumed.exit_code() == 0 and len(item_rows(server)) == 3


def test_a_write_that_never_landed_is_sent_again_with_the_same_bytes(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    first = expected_items(src)[0]
    server = Server()
    server.lose_write.add(cp.item_key(first))
    assert cu.run_pass(cfg(tmp_path, src), server, now=LATER).exit_code() == 5
    assert cu.run_pass(cfg(tmp_path, src), server, now=LATER).exit_code() == 0
    assert server.writes.count(cp.item_key(first)) == 2
    assert server.rows[cp.item_key(first)] == first


def test_an_unreachable_server_loses_nothing(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    server.down = True
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (report.exit_code(), report.pending) == (5, 3)
    server.down = False
    assert cu.run_pass(cfg(tmp_path, src), server, now=LATER).exit_code() == 0
    assert len(item_rows(server)) == 3


# ---- refusals hold the batch, never skip it ---------------------------------------------------------------------

def test_a_refused_item_holds_its_batch_and_nothing_after_it_is_sent(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    second = cp.item_key(expected_items(src)[1])
    server = Server()
    server.refuse[second] = "E_CONTEXT_SOMETHING_NEW"
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (report.status(), report.exit_code()) == ("held", 3)
    [held] = report.context_held
    assert (held["code"], held["batch"], held["first_ord"], held["last_ord"]) == ("E_CONTEXT_SOMETHING_NEW", 1, 1, 1)
    assert held["age_s"] == 0 and report.failed == {"E_CONTEXT_SOMETHING_NEW": 1}
    assert not any(k.startswith("ctxm/") for k in server.writes), "a manifest went out past a held item"
    assert len(item_rows(server)) == 1
    hour = cu.run_pass(cfg(tmp_path, src), server, now=LATER + timedelta(hours=1))
    assert hour.context_held[0]["age_s"] == 3600, "the held age restarted"
    # control: once the server takes it, the batch completes and the hold clears
    del server.refuse[second]
    done = cu.run_pass(cfg(tmp_path, src), server, now=LATER + timedelta(hours=2))
    assert done.context_held == [] and done.batches_committed == 1 and len(item_rows(server)) == 3
    [held_file] = list((tmp_path / "store").rglob("held.json"))
    assert json.loads(held_file.read_text()) == {}, "a cleared hold stayed on disk and would age a later one"


@pytest.mark.parametrize("result, code", [
    ({"status": "failed", "result_signature_verified": True, "error_code": "E_CONTEXT_X"}, "E_CONTEXT_X"),
    # the key already holds version 1 (divergence 059): the read decides, it is not a refusal
    ({"status": "failed", "result_signature_verified": True, "error_code": "E_USE_MEMORY_EDIT"}, None),
    # an unsigned failure, a timeout and a success may all have landed: the read decides
    ({"status": "failed", "result_signature_verified": False, "error_code": "E_CONTEXT_X"}, None),
    ({"status": "timeout", "result_signature_verified": True, "error_code": "E_DEADLINE_EXCEEDED"}, None),
    ({"status": "failed", "result_signature_verified": True, "error_code": ""}, None),
    ({"status": "completed", "result_signature_verified": True}, None),
    (None, None),
])
def test_only_a_signed_typed_failure_is_a_refusal(result, code):
    assert cu.refusal_code(result) == code


def test_a_readback_that_differs_is_a_conflict_for_review(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    first = cp.item_key(expected_items(src)[0])
    server = Server()
    server.rows[first] = {"schema": "context.v1", "kind": "item", "planted": True}
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (report.status(), report.exit_code()) == ("conflict", 4)
    assert report.conflicts[0]["code"] == "readback_differs"


# ---- source identity and generations ---------------------------------------------------------------------------

def test_a_rewritten_source_opens_the_next_generation(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    with open(src, "r+", encoding="utf-8", newline="\n") as f:      # rewritten in place: same file identity
        f.seek(0)
        f.truncate()
        for r in [meta("s-1"), user(1, "FIRST QUESTION, edited")]:
            f.write(json.dumps(r) + "\n")
    idle(src)
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    gens = sorted({k.split("/")[3] for k in item_rows(server)})
    assert gens == ["0001", "0002"] and report.exit_code() == 0
    assert any(v["text"] == "FIRST QUESTION, edited" for k, v in item_rows(server).items() if "/0002/" in k)


def test_a_file_replaced_under_its_path_is_refused(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    other = tmp_path / "other.jsonl"
    three_turns(other, session="s-other")
    os.replace(other, src)
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (report.status(), report.exit_code()) == ("conflict", 4)


def test_a_second_seat_cannot_enroll_a_file_another_seat_holds(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    cu.run_pass(cfg(tmp_path, src), Server(), now=LATER)
    report = cu.run_pass(cfg(tmp_path, src, seat="other-seat"), Server(), now=LATER)
    assert report.conflicts and report.exit_code() == 4


# ---- budget, accounting and the status line ------------------------------------------------------------------------

def test_max_batches_bounds_a_pass_and_the_next_pass_goes_on(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, max_batches=2), server, now=LATER)
    assert len(server.writes) == 1, "the budget counts the freeze and each send"
    assert report.pending == 3 and report.exit_code() == 0
    for _ in range(5):
        cu.run_pass(cfg(tmp_path, src, max_batches=2), server, now=LATER)
    assert len(item_rows(server)) == 3 and len(server.rows) == 4


def test_unreadable_lines_and_skipped_records_are_counted(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    with open(src, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    idle(src)
    report = cu.run_pass(cfg(tmp_path, src), Server(), now=LATER)
    assert report.excluded == {"no_item": 1, "unreadable_line": 1}      # session_meta makes no item
    assert (report.records_emitted, report.records_pending) == (3, 0)


def test_every_record_is_counted_once_across_passes_and_the_classes_sum_to_the_file(tmp_path):
    src = tmp_path / "rollout.jsonl"
    now = datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_lines(src, [meta("s-10"), user(1, "q", fresh), said(1, "a", fresh), plan("c1", ["one step"])])
    with open(src, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    server = Server()
    first = cu.run_pass(cfg(tmp_path, src), server, now=now)                  # live: nothing committed yet
    assert (first.records_emitted, sum(first.excluded.values()), first.records_pending) == (0, 0, 5)
    totals = {"emitted": 0, "excluded": 0}
    for later in (now + timedelta(minutes=6), now + timedelta(minutes=7)):
        r = cu.run_pass(cfg(tmp_path, src), server, now=later)
        totals["emitted"] += r.records_emitted
        totals["excluded"] += sum(r.excluded.values())
    assert totals == {"emitted": 3, "excluded": 2}, "a record was counted twice or never"
    assert totals["emitted"] + totals["excluded"] == 5 == len(src.read_text(encoding="utf-8").splitlines())


def test_the_status_carries_no_transcript_text_and_names_sources_by_src_only(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    server.refuse[cp.item_key(expected_items(src)[0])] = "E_CONTEXT_X"
    out = json.dumps(cu.run_pass(cfg(tmp_path, src), server, now=LATER).as_json())
    for text in ("first question", "first answer", "rollout.jsonl", str(tmp_path)):
        assert text not in out
    assert set(json.loads(out)) == {"status", "seat", "passes", "batches_committed", "items_committed",
                                    "last_manifest_key", "context_held", "accounting", "conflicts", "reason",
                                    "f3_deferred", "visited"}
    # `visited` names sources by their place in --source, never by path (seat-handover item 1)
    assert json.loads(out)["visited"] == [{"source_index": 0, "batches": json.loads(out)["visited"][0]["batches"]}]


def test_exit_codes_are_the_published_seam():
    assert (cu.EXIT_OK, cu.EXIT_USAGE, cu.EXIT_HELD, cu.EXIT_CONFLICT, cu.EXIT_UNREACHABLE) == (0, 2, 3, 4, 5)
    assert cu.main(["--seat", "x"]) == cu.EXIT_USAGE


# ---- a reviewer's review of 4bc4b4d9 (f39579), one regression each ---------------------------------------------

def test_f1_a_pass_that_commits_items_before_its_manifest_is_progress_not_drained(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    cu.run_pass(cfg(tmp_path, src, max_batches=2), server, now=LATER)      # freeze + first item
    second = cu.run_pass(cfg(tmp_path, src, max_batches=2), server, now=LATER)
    assert (second.items_committed, second.batches_committed, second.emitted) == (2, 0, 0)
    assert second.status() == "progressed", "half a batch sent, and the caller was told it was drained"


def test_f2_a_record_the_parser_refuses_is_a_conflict_with_a_fixed_code_and_the_next_source_still_runs(tmp_path):
    bad, good = tmp_path / "bad.jsonl", tmp_path / "good.jsonl"
    write_lines(bad, [user(1, "PRIVATE TEXT before any session")])     # no session_meta first
    idle(bad)
    three_turns(good)
    server = Server()
    c = cu.Config(seat=SEAT, dialect="codex", store_dir=tmp_path / "store", sources=[str(bad), str(good)])
    report = cu.run_pass(c, server, now=LATER)
    assert report.exit_code() == 4 and {"src": None, "code": "parser_refused"} in report.conflicts
    assert "PRIVATE TEXT" not in json.dumps(report.as_json())
    assert len(item_rows(server)) == 3, "one refused source stopped the others"


def test_f3a_a_rewrite_before_the_first_freeze_does_not_open_a_second_generation_later(tmp_path):
    src = tmp_path / "rollout.jsonl"
    now = datetime.now(timezone.utc)
    write_lines(src, [meta("s-6"), user(1, "draft one", now.strftime("%Y-%m-%dT%H:%M:%SZ"))])
    server = Server()
    cu.run_pass(cfg(tmp_path, src), server, now=now)                          # live: nothing frozen
    assert server.writes == []
    with open(src, "r+", encoding="utf-8", newline="\n") as f:                 # rewritten before any freeze
        f.seek(0)
        f.truncate()
        for r in [meta("s-6"), user(1, "draft TWO, rewritten")]:
            f.write(json.dumps(r) + "\n")
    idle(src)
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)                        # untouched since
    assert sorted({k.split("/")[3] for k in item_rows(server)}) == ["0001"], "an untouched file was uploaded twice"


def test_f3b_a_file_enrolled_empty_is_still_checked_for_a_rewrite_once_it_has_bytes(tmp_path):
    src = tmp_path / "rollout.jsonl"
    src.write_bytes(b"")
    server = Server()
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)                        # enrolled at 0 bytes
    three_turns(src)
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    with open(src, "r+", encoding="utf-8", newline="\n") as f:                 # overwritten in place, longer
        f.seek(0)
        f.truncate()
        for r in [meta("s-other"), user(1, "a different session"), said(1, "longer"), user(2, "than before"),
                  said(2, "by a few lines")]:
            f.write(json.dumps(r) + "\n")
    idle(src)
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert sorted({k.split("/")[3] for k in item_rows(server)}) == ["0001", "0002"]


def test_f4_a_record_landing_exactly_two_batches_on_is_refused_not_half_frozen(tmp_path):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-7"), user(1, "one"), user(2, "two"),
                      plan("c1", ["p1", "p2", "p3", "p4", "p5", "p6"])])       # 2 + 6 = 8 = two full batches of 4
    idle(src)
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, batch_items=4), server, now=LATER)
    assert report.exit_code() == 4
    assert not any(v["item"] == "task" for v in item_rows(server).values()), "part of the record was uploaded"


def test_f5_a_rewrite_waits_for_the_old_generation_to_drain_first(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    server.down = True
    cu.run_pass(cfg(tmp_path, src), server, now=LATER)                        # gen 1 frozen, nothing sent
    with open(src, "r+", encoding="utf-8", newline="\n") as f:
        f.seek(0)
        f.truncate()
        for r in [meta("s-1"), user(1, "rewritten while the server was down")]:
            f.write(json.dumps(r) + "\n")
    idle(src)
    server.down = False
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    gen1 = {k for k in server.rows if "/0001/" in k or k.endswith("/0001/000001")}
    assert len([k for k in item_rows(server) if "/0001/" in k]) == 3, "gen 1's frozen records were stranded"
    assert any(k.startswith("ctxm/") and "/0001/" in k for k in server.rows)
    assert any("/0002/" in k for k in item_rows(server)) and report.exit_code() == 0


def test_f6_a_spill_does_not_freeze_past_the_budget(tmp_path):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-8"), user(1, "a"), user(2, "b"), user(3, "c"), plan("c1", ["x1", "x2"])])
    idle(src)
    report = cu.run_pass(cfg(tmp_path, src, batch_items=4, max_batches=1), Server(), now=LATER)
    assert report.emitted == 3, "a second batch was frozen past --max-batches 1"
    assert report.pending == 3 + 2


def test_f7_an_unreadable_source_is_a_conflict_that_names_no_path(tmp_path, monkeypatch):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)

    def refuse(*_a, **_k):
        raise PermissionError(13, "denied", str(src))
    monkeypatch.setattr(cu, "Enrollment", refuse)
    report = cu.run_pass(cfg(tmp_path, src), Server(), now=LATER)
    assert {"src": None, "code": "source unreadable"} in report.conflicts
    assert str(tmp_path) not in json.dumps(report.as_json())


# ---- GitHub Codex on 36b9ebf4 ------------------------------------------------------------------------------------

def test_codex_r4375282525_a_crash_right_after_the_freeze_keeps_the_prefix_with_the_offset(tmp_path, monkeypatch):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    real = cu.Store.resume
    calls = {"n": 0}

    def crash_once(self, *a, **k):                       # the process dies right after the freeze committed
        calls["n"] += 1
        if calls["n"] == 1:
            raise cu.StoreIOError("crash")
        return real(self, *a, **k)
    monkeypatch.setattr(cu.Store, "resume", crash_once)
    server = Server()
    assert cu.run_pass(cfg(tmp_path, src), server, now=LATER).exit_code() == 5
    monkeypatch.setattr(cu.Store, "resume", real)
    with open(src, "r+", encoding="utf-8", newline="\n") as f:                 # then rewritten in place, longer
        f.seek(0)
        f.truncate()
        for r in [meta("s-9"), user(1, "another session"), said(1, "written over"), user(2, "the old one"),
                  said(2, "and longer")]:
            f.write(json.dumps(r) + "\n")
    idle(src)
    for _ in range(3):
        cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert sorted({k.split("/")[3] for k in item_rows(server)}) == ["0001", "0002"], "the rewrite went unseen"


def test_codex_r4375282532_a_timed_out_write_that_did_not_land_ends_the_pass_instead_of_looping(tmp_path):
    src = tmp_path / "rollout.jsonl"
    three_turns(src)

    class TimesOut(Server):
        def __init__(self):
            super().__init__()
            self.timeouts = True

        def write(self, key, value):
            assert len(self.writes) < 50, "the same record was sent in a loop"
            if self.timeouts:
                self.writes.append(key)
                return {"status": "timeout", "result_signature_verified": True, "error_code": "E_DEADLINE_EXCEEDED"}
            return super().write(key, value)
    server = TimesOut()
    report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (report.exit_code(), len(server.writes)) == (5, 1)
    server.timeouts = False
    assert cu.run_pass(cfg(tmp_path, src), server, now=LATER).exit_code() == 0
    assert len(item_rows(server)) == 3


def test_codex_r4375729665_an_itemless_tail_settles_once_the_file_is_idle_and_is_never_counted_twice(tmp_path):
    src = tmp_path / "rollout.jsonl"
    event = _rec("event_msg", {"type": "token_count"})
    write_lines(src, [meta("s-11"), event])                                   # nothing here makes an item
    idle(src)
    server = Server()
    first = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (sum(first.excluded.values()), first.records_pending, first.status()) == (2, 0, "drained")
    again = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (sum(again.excluded.values()), again.records_pending) == (0, 0), "the tail was counted twice"
    write_lines(src, [user(1, "now a question"), event], mode="a")           # later, a dialogue line and a tail
    idle(src)
    third = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (third.records_emitted, sum(third.excluded.values()), third.records_pending) == (1, 1, 0)
    assert len(item_rows(server)) == 1
    total = 2 + third.records_emitted + sum(third.excluded.values())
    assert total == len(src.read_text(encoding="utf-8").splitlines())


def test_a_live_itemless_tail_stays_pending_until_idle(tmp_path):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-12"), _rec("event_msg", {"type": "token_count"})])  # mtime now: live
    report = cu.run_pass(cfg(tmp_path, src), Server(), now=datetime.now(timezone.utc))
    assert (sum(report.excluded.values()), report.records_pending) == (0, 2)


# ---- a reviewer 0a5c6b on 2837be65 --------------------------------------------------------------------------------

def _rewrite(path, records):
    with open(path, "r+", encoding="utf-8", newline="\n") as f:
        f.seek(0)
        f.truncate()
        for r in records:
            f.write(json.dumps(r) + "\n")
    idle(path)


EVENT = _rec("event_msg", {"type": "token_count"})


def test_0a5c6b_case_a_a_settled_itemless_file_rewritten_before_any_batch_is_counted_again(tmp_path):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-13"), EVENT, EVENT, EVENT])                    # four item-less records, settled
    idle(src)
    server = Server()
    assert sum(cu.run_pass(cfg(tmp_path, src), server, now=LATER).excluded.values()) == 4
    _rewrite(src, [meta("s-13"), user(1, "short")])                          # shorter, in place, revision 0
    r = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert (r.records_emitted, sum(r.excluded.values())) == (1, 1), "the replacement records were never counted"


def test_0a5c6b_case_b_a_rewritten_tail_after_a_committed_batch_is_counted_again(tmp_path):
    src = tmp_path / "rollout.jsonl"
    head = [meta("s-14"), user(1, "q"), said(1, "a")]
    write_lines(src, head + [EVENT, EVENT, EVENT])
    idle(src)
    server = Server()
    # batch_items=2: the two dialogue items fill batch 1 exactly at the end of said(1), so the events after it are an
    # item-less tail that no batch commits, and it settles because the file is idle
    first = cu.run_pass(cfg(tmp_path, src, batch_items=2), server, now=LATER)
    assert (first.records_emitted, sum(first.excluded.values())) == (2, 4)
    _rewrite(src, head + [user(2, "a new tail")])                            # committed prefix intact, tail replaced
    r = cu.run_pass(cfg(tmp_path, src, batch_items=2), server, now=LATER)
    assert (r.records_emitted, sum(r.excluded.values())) == (1, 0), "the new tail record was hidden by the old cursor"


def test_0a5c6b_a_pass_that_fails_mid_file_settles_nothing(tmp_path):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [EVENT, EVENT, user(1, "before any session id")])         # the user record is refused
    idle(src)
    r = cu.run_pass(cfg(tmp_path, src), Server(), now=LATER)
    assert r.exit_code() == 4
    assert sum(r.excluded.values()) == 0, "a failed pass settled the lines before its failure"
    [enrollment] = list((tmp_path / "store").rglob("enrollment.json"))
    assert json.loads(enrollment.read_text()).get("accounted_through", 0) == 0


def test_a_parser_state_version_bump_is_a_named_conflict_not_a_store_mismatch(tmp_path, monkeypatch):
    """#421 bumps context_parser.STATE_VERSION. The store's parser identity is the adapter version only, so an enrolled
    source still resumes; a checkpoint the new parser refuses is the named conflict parser_state_version, after the
    frozen batches have drained."""
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    server = Server()
    assert cu.run_pass(cfg(tmp_path, src), server, now=LATER).exit_code() == 0
    write_lines(src, [said(2, "more after the bump")], mode="a")
    idle(src)
    monkeypatch.setattr(cp, "STATE_VERSION", cp.STATE_VERSION + 1)
    try:
        report = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    except ValueError as exc:                    # a reviewer 027ebe: fail on an assertion, not on the escape
        raise AssertionError("a refused parser checkpoint escaped run_pass: %r" % (exc,)) from None
    assert {"src": None, "code": "parser_state_version"} in report.conflicts
    assert not any(c["code"].startswith("store:") for c in report.conflicts), "the bump read as an enrollment mismatch"


# ---- GitHub Codex on 113b387e ------------------------------------------------------------------------------------

def _counting_open(monkeypatch, path):
    """Count the bytes the uploader reads from `path`."""
    read = {"bytes": 0}

    class Counted:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def read(self, *a):
            b = self.f.read(*a)
            read["bytes"] += len(b)
            return b

        def readline(self, *a):
            b = self.f.readline(*a)
            read["bytes"] += len(b)
            return b

        def seek(self, *a):
            return self.f.seek(*a)

    def opener(p, mode="r", *a, **k):
        f = open(p, mode, *a, **k)
        return Counted(f) if os.path.abspath(str(p)) == os.path.abspath(str(path)) else f
    monkeypatch.setattr(cu, "open", opener, raising=False)
    return read


def test_codex_r4377304927_a_budgeted_pass_reads_only_as_far_as_its_budget(tmp_path, monkeypatch):
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-big")] + [user(n, "question %d " % n + "x" * 200) for n in range(60)])
    idle(src)
    read = _counting_open(monkeypatch, src)
    report = cu.run_pass(cfg(tmp_path, src, batch_items=2, max_batches=1), Server(), now=LATER)
    assert report.emitted == 2
    assert read["bytes"] < src.stat().st_size // 4, "one batch's pass read %d of %d bytes" % (
        read["bytes"], src.stat().st_size)


def test_codex_r4377304927_budgeted_passes_still_upload_everything_and_count_every_record_once(tmp_path):
    src = tmp_path / "rollout.jsonl"
    lines = [meta("s-big2")]
    for n in range(12):
        lines += [user(n, "q%d" % n), said(n, "a%d" % n)]
    write_lines(src, lines)
    idle(src)
    server, emitted, excluded = Server(), 0, {}
    for _ in range(60):
        report = cu.run_pass(cfg(tmp_path, src, batch_items=3, max_batches=1), server, now=LATER)
        emitted += report.records_emitted
        for cls, n in report.excluded.items():
            excluded[cls] = excluded.get(cls, 0) + n
        if report.status() == "drained":
            break
    assert report.status() == "drained" and report.records_pending == 0
    assert item_rows(server) == {cp.item_key(i): i for i in expected_items(src, batch_items=3)}
    assert emitted + sum(excluded.values()) == len(lines), (emitted, excluded, len(lines))


def _tool(call):
    return _rec("response_item", {"type": "function_call", "call_id": call, "name": "shell", "arguments": "{}"})


def test_codex_r4377304927_a_tail_settled_after_a_freeze_is_not_counted_again_when_the_file_grows(tmp_path):
    """The streamed read hashes the settled tail from the last freeze, not from the start of the read: a pass that
    freezes a batch and then settles an item-less tail must leave a fingerprint the next pass matches."""
    src = tmp_path / "rollout.jsonl"
    write_lines(src, [meta("s-tail"), user(1, "q1"), user(2, "q2"), _tool("t1"), _tool("t2"), _tool("t3")])
    idle(src)
    server, totals = Server(), {"emitted": 0, "excluded": 0, "pending": 0}

    def run():
        r = cu.run_pass(cfg(tmp_path, src, batch_items=2), server, now=LATER)
        assert not r.conflicts, r.conflicts
        totals["emitted"] += r.records_emitted
        totals["excluded"] += sum(r.excluded.values())
        totals["pending"] = r.records_pending
        return r
    first = run()
    assert (first.records_emitted, sum(first.excluded.values()), first.records_pending) == (2, 4, 0)
    write_lines(src, [user(3, "q3"), user(4, "q4")], mode="a")
    idle(src)
    run()
    assert totals == {"emitted": 4, "excluded": 4, "pending": 0}, "the settled tail was counted twice or never"
    assert item_rows(server) == {cp.item_key(i): i for i in expected_items(src, batch_items=2)}


# ---- GitHub Codex on 8783ebd3 ------------------------------------------------------------------------------------

def _wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _widest_batch():
    """256 items at the 65,536-byte wire cap and their manifest: the largest batch the outbox admits."""
    from frozen_outbox import Record
    seat, src = "synthetic-seat", "3" * 32
    coordinates = dict(seat=seat, src=src, gen=1, batch=1)
    prefix = "%s/%s/0001/000001" % (seat, src)
    items = []
    for ordinal in range(256):
        base = dict(schema="context.v1", kind="item", **coordinates, ordinal=ordinal, item="user_text", role="user",
                    dialect="codex", adapter_version="1.0", time="2026-10-19T00:00:00.000Z", text="",
                    text_sha256="0" * 64)
        text = "w" * (65536 - len(_wire(base)))
        value = dict(base, text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest())
        assert len(_wire(value)) == 65536
        items.append(Record("ctx/%s/%010d" % (prefix, ordinal), _wire(value)))
    manifest = dict(schema="context.v1", kind="manifest", **coordinates, first=0, last=255,
                    items=[[json.loads(r.value)["ordinal"], json.loads(r.value)["text_sha256"]] for r in items],
                    counts=dict(written=256, omitted_secret=0, omitted_client_name=0, omitted_too_large=0))
    return tuple(items), Record("ctxm/" + prefix, _wire(manifest))


def test_codex_r4381639373_the_largest_valid_batch_freezes_under_the_uploaders_limits(tmp_path):
    """256 items at the wire cap base64 to about 22.5 MiB in the frozen batch: past the store's 8 MiB output default,
    which refused a valid full batch on every pass (a 256-item batch of 26,000-character messages already does)."""
    from frozen_outbox import FrozenOutbox
    store = cu.Store(tmp_path / "store", cu.LIMITS)
    snapshot = store.enroll("1" * 64, "2" * 64, "synthetic-parser", {}, {"next": 0})
    items, manifest = _widest_batch()
    FrozenOutbox(store).freeze(snapshot, 100, items, manifest, {"next": 256})
    assert store.resume("1" * 64, "2" * 64, "synthetic-parser").offset == 100


def test_codex_r4381639373_the_output_limit_holds_the_largest_valid_batch():
    """The bound by arithmetic: 256 items and a manifest, each at the 65,536-byte wire cap, base64 plus a key and
    JSON quoting per record, plus the envelope."""
    key_and_quoting = 256
    per_record = 4 * ((65536 + 2) // 3) + key_and_quoting
    assert (256 + 1) * per_record + 4096 <= cu.LIMITS.output_bytes


# ---- GitHub Codex on 8f623d5c, a reviewer 5063f5 -----------------------------------------------------------------

def test_codex_r4382734763_a_full_plan_of_long_non_ascii_steps_fits_the_checkpoint_limit(tmp_path):
    """256 distinct steps of 1,000 three-byte characters: every item fits the wire, but the checkpoint carrying the
    plan table is about 770 KB. Past the old 512 KiB limit the store refused every freeze, so no pass could advance.
    The parser's real state goes through the store's own check (enroll encodes the checkpoint as a commit does)."""
    parser = cp.ContextParser(SEAT, "codex", namespace=cu.NAMESPACE)
    parser.feed(meta("s-plan"))
    parser.feed(plan("c-big", [("%03d" % n) + "\u20ac" * 997 for n in range(cp.MAX_PLAN_STEPS)]))
    checkpoint = cu._checkpoint(parser.state(), cu.PREFIX_BYTES, "0" * 64)
    store = cu.Store(tmp_path / "store", cu.LIMITS)
    store.enroll("1" * 64, "2" * 64, cu.PARSER_ID, {}, checkpoint)
    assert len(store.resume("1" * 64, "2" * 64, cu.PARSER_ID).checkpoint) > 512 * 1024


def test_codex_r4382734763_the_checkpoint_limit_holds_the_largest_parser_state(tmp_path):
    """The bound by construction: a full plan table of 1,000-character steps that JSON escapes to 6 bytes a
    character, a final report at the wire cap, encoded the way the store encodes it, fits the uploader's limit."""
    parser = cp.ContextParser(SEAT, "codex", namespace=cu.NAMESPACE)
    state = parser.state()
    state["plan"] = [["t:" + ("%03d" % n) + "\x01" * 997, [n, n, n]] for n in range(cp.MAX_PLAN_STEPS)]
    state["final"] = {"time": "2026-10-26T00:00:00Z", "ref": "r" * 64, "bytes": 65536, "reason": None,
                      "text": "\x01" * (65536 // 6)}
    checkpoint = cu._checkpoint(state, cu.PREFIX_BYTES, "0" * 64)
    encoded = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(encoded) > 512 * 1024, "the construction no longer exceeds the old limit"
    assert len(encoded) <= cu.LIMITS.checkpoint_bytes, len(encoded)


def test_antigravity_5063f5_the_store_keeps_as_many_batches_and_sources_as_the_wire_can_number():
    """No batch is pruned yet, so a long-lived source keeps every batch it froze. The wire numbers batches with six
    digits and generations with four (ctx/<seat>/<src>/<gen4>/<batch6>/<ord10>)."""
    key = cp.item_key({"seat": SEAT, "src": "0" * 32, "gen": 1, "batch": 1, "ordinal": 0})
    gen_digits, batch_digits = len(key.split("/")[3]), len(key.split("/")[4])
    assert cu.LIMITS.batches >= 10 ** batch_digits - 1
    assert cu.LIMITS.sources >= 10 ** gen_digits - 1


def test_a_parser_that_takes_source_path_gets_the_enrolled_file(tmp_path, monkeypatch):
    """Kimi (#425) and antigravity (#421) parsers take a source's identity from its path. Without it every record is
    skipped as no_session_id (shown on HQD with #425's parser: 0 items without the path, 2 with it)."""
    src = tmp_path / "rollout.jsonl"
    three_turns(src)
    seen = []

    class PathAware(cp.ContextParser):
        def __init__(self, *args, source_path=None, **kwargs):
            seen.append(source_path)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cu, "ContextParser", PathAware)
    assert cu.run_pass(cfg(tmp_path, src), Server(), now=LATER).exit_code() == 0
    assert seen and seen[0] == str(src), seen


def test_an_antigravity_source_without_a_session_id_in_its_records_uploads_by_its_path(tmp_path):
    """txxxx 712a61 INFO 2, end to end through run_pass: Antigravity records carry no conversation_id, so the source's
    identity comes only from its brain/<uuid>/ path. Without the path the uploader committed nothing and reported every
    record as no_session_id."""
    logs = tmp_path / "brain" / "conv-uuid-42" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    src = logs / "transcript_full.jsonl"
    stamp = "2026-10-10T11:00:00Z"
    write_lines(src, [{"step_index": n, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                       "created_at": stamp, "content": "synthetic question %d" % n} for n in range(3)])
    idle(src)
    server = Server()
    report = cu.run_pass(cu.Config(seat=SEAT, dialect="antigravity", store_dir=tmp_path / "store",
                                   sources=[str(src)]), server, now=LATER)
    assert not report.conflicts, report.conflicts
    assert "no_session_id" not in report.excluded, report.excluded
    assert (report.exit_code(), report.items_committed, report.records_pending) == (0, 3, 0)
    parser, _ = cp.parse_file(str(src), SEAT, "antigravity", namespace=cu.NAMESPACE)
    assert item_rows(server) == {cp.item_key(i): i for i in parser.items}


def test_antigravity_dbdb58_a_relative_source_path_still_gives_the_source_its_identity(tmp_path, monkeypatch):
    """A relative --source (run from inside the logs directory) dropped the brain/<uuid>/ directories the identity
    is read from, so gen 1 enrolled with no src and every record was no_session_id, for good."""
    logs = tmp_path / "brain" / "conv-uuid-43" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    src = logs / "transcript_full.jsonl"
    write_lines(src, [{"step_index": n, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                       "created_at": "2026-10-10T11:00:00Z", "content": "synthetic %d" % n} for n in range(2)])
    idle(src)
    monkeypatch.chdir(logs)
    server = Server()
    report = cu.run_pass(cu.Config(seat=SEAT, dialect="antigravity", store_dir=tmp_path / "store",
                                   sources=["transcript_full.jsonl"]), server, now=LATER)
    assert "no_session_id" not in report.excluded, report.excluded
    assert (report.exit_code(), report.items_committed) == (0, 2)
    parser, _ = cp.parse_file(str(src), SEAT, "antigravity", namespace=cu.NAMESPACE)
    assert item_rows(server) == {cp.item_key(i): i for i in parser.items}


@pytest.mark.parametrize("relative", [False, True])
def test_kimi_source_without_a_record_session_id_uploads_by_path(tmp_path, monkeypatch, relative):
    """#425: SourcePass must give the enrolled path to the Kimi parser, whose records carry no session id.
    Cover the real session_<id>/agents/main layout and a relative --source. Red control: removing source_path
    from SourcePass._enroll quarantines every content record and this test commits no items."""
    session = "01234567-89ab-cdef-0123-456789abcdef"
    wire_dir = tmp_path / "sessions" / "wd_test" / ("session_" + session) / "agents" / "main"
    wire_dir.mkdir(parents=True)
    src = wire_dir / "wire.jsonl"
    stamp = 1760680800123
    write_lines(src, [
        {"type": "metadata", "protocol_version": "1.5", "time": stamp - 1},
        {"type": "context.append_message", "time": stamp,
         "message": {"id": "u1", "role": "user", "origin": {"kind": "user"},
                     "content": [{"type": "text", "text": "synthetic kimi question"}]}},
        {"type": "context.append_loop_event", "time": stamp + 1,
         "event": {"type": "content.part", "uuid": "a1",
                   "part": {"type": "text", "text": "synthetic kimi answer"}}},
    ])
    idle(src)
    if relative:
        monkeypatch.chdir(wire_dir)
    server = Server()
    report = cu.run_pass(cu.Config(seat=SEAT, dialect="kimi", store_dir=tmp_path / "store",
                                   sources=["wire.jsonl" if relative else str(src)]), server, now=LATER)
    assert not report.conflicts, report.conflicts
    assert "no_session_id" not in report.excluded, report.excluded
    assert (report.exit_code(), report.items_committed, report.records_pending) == (0, 2, 0)
    parser, _ = cp.parse_file(str(src), SEAT, "kimi", namespace=cu.NAMESPACE)
    assert len(parser.items) == 2 and item_rows(server) == {cp.item_key(i): i for i in parser.items}


# ---- F3 (ingest plan D1): off by default, part of the enrolled identity, flushed before an idle cut ----------------

def _open_call(path, session="s-f3"):
    day_ago = "2026-10-09T11:00:00Z"           # a day before LATER: the open call is due by the idle clock
    write_lines(path, [meta(session), user(1, "q", day_ago),
                       _rec("response_item", {"type": "function_call", "call_id": "c1", "name": "shell",
                                              "arguments": "{}"}, day_ago),
                       said(1, "a", day_ago)])
    idle(path)


def test_switching_f3_on_defers_an_enrolled_source_and_new_sources_get_f3(tmp_path):
    """--f3 ON for a seat whose source was enrolled without it: that source resumes text-only and is listed in
    f3_deferred (it does not stop the seat with a conflict: HQD 1 Nov, every pass exited 4), and a source enrolled
    from then on is F3. One source never mixes F1 and F3 items (txxxx 6b8c71)."""
    old, new = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    _open_call(old, session="s-old")
    server = Server()
    assert cu.run_pass(cfg(tmp_path, old), server, now=LATER).exit_code() == 0
    text_only = dict(item_rows(server))
    assert text_only and all(v["item"] != "tool_exchange" for v in text_only.values())
    report = cu.run_pass(cfg(tmp_path, old, f3=True), server, now=LATER)
    assert report.conflicts == [] and report.exit_code() == 0, report.conflicts
    [wire_src] = {k.split("/")[2] for k in text_only}
    assert report.f3_deferred == [{"src": wire_src, "gen": 1}], "named by its wire src, joinable to its keys"
    assert report.as_json()["f3_deferred"] == report.f3_deferred
    assert item_rows(server) == text_only, "the deferred source sent nothing new and no tool_exchange"
    _open_call(new, session="s-new")
    fresh = cu.run_pass(cfg(tmp_path, new, f3=True), server, now=LATER)
    assert fresh.conflicts == [] and fresh.f3_deferred == []
    assert "tool_exchange" in [v["item"] for k, v in item_rows(server).items() if k not in text_only]
    again = cu.run_pass(cfg(tmp_path, old), server, now=LATER)
    assert again.conflicts == [], "F3 off still resumes the source enrolled without it"


def test_a_deferred_source_that_keeps_growing_uploads_its_new_lines_text_only(tmp_path):
    """pxx on #471: a session enrolled before F3 that keeps running is the common case. Lines appended after the
    deferral go up text-only through _capture, with no conflict (a capture that read cfg.f3 instead of the source's
    mode would refuse the checkpoint as parser_state_version)."""
    src = tmp_path / "rollout.jsonl"
    _open_call(src, session="s-grow")
    server = Server()
    assert cu.run_pass(cfg(tmp_path, src), server, now=LATER).exit_code() == 0
    before = dict(item_rows(server))
    write_lines(src, [user(2, "a later question", DAY_AGO), _call("c2", DAY_AGO), said(2, "a later answer", DAY_AGO)],
                mode="a")
    idle(src)
    grown = cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER)
    assert grown.conflicts == [] and grown.exit_code() == 0, grown.conflicts
    assert len(grown.f3_deferred) == 1
    new = {k: v for k, v in item_rows(server).items() if k not in before}
    assert new, "the appended lines were uploaded"
    assert all(v["item"] != "tool_exchange" for v in new.values()), "a deferred source stays text-only"


def test_switching_f3_off_for_an_f3_source_is_still_a_conflict(tmp_path):
    """The kill switch: a source enrolled WITH F3 never silently downgrades; --f3 off stops it with a conflict."""
    src = tmp_path / "rollout.jsonl"
    _open_call(src)
    server = Server()
    assert cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER).exit_code() == 0
    sent = list(server.writes)
    off = cu.run_pass(cfg(tmp_path, src), server, now=LATER)
    assert any(c["code"].startswith("store:") and "enrollment mismatch" in c["code"] for c in off.conflicts), \
        off.conflicts
    assert server.writes == sent
    on = cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER)
    assert on.conflicts == [] and on.f3_deferred == [], "an F3 source resumes as F3 (its mode is read from the store)"


def test_kimi_mixed_f3_mode_never_mixes_per_enrolled_source(tmp_path):
    """A Kimi source keeps its enrolled F3 mode: switching ON defers it (text-only, no conflict), switching OFF for
    an F3 source is a conflict. Neither direction duplicates delivery."""
    for initial_f3 in (False, True):
        case = tmp_path / str(initial_f3)
        session = "01234567-89ab-cdef-0123-456789abcdef"
        wire_dir = case / "sessions" / "wd_test" / ("session_" + session) / "agents" / "main"
        wire_dir.mkdir(parents=True)
        src = wire_dir / "wire.jsonl"
        stamp = 1760680800123
        write_lines(src, [
            {"type": "metadata", "protocol_version": "1.5", "time": stamp - 1},
            {"type": "context.append_message", "time": stamp,
             "message": {"id": "u1", "role": "user", "origin": {"kind": "user"},
                         "content": [{"type": "text", "text": "synthetic kimi question"}]}},
            {"type": "context.append_loop_event", "time": stamp + 1,
             "event": {"type": "content.part", "uuid": "a1",
                       "part": {"type": "text", "text": "synthetic kimi answer"}}},
        ])
        idle(src)

        def config(f3):
            return cu.Config(seat=SEAT, dialect="kimi", store_dir=case / "store", sources=[str(src)], f3=f3)

        server = Server()
        assert cu.run_pass(config(initial_f3), server, now=LATER).exit_code() == 0
        sent = list(server.writes)
        switched = cu.run_pass(config(not initial_f3), server, now=LATER)
        if initial_f3:
            assert any(c["code"].startswith("store:") and "enrollment mismatch" in c["code"]
                       for c in switched.conflicts), (initial_f3, switched.conflicts)
        else:
            assert switched.conflicts == [] and len(switched.f3_deferred) == 1, (initial_f3, switched.conflicts)
        restored = cu.run_pass(config(initial_f3), server, now=LATER)
        assert restored.conflicts == [], (initial_f3, restored.conflicts)
        assert server.writes == sent, (initial_f3, "a rejected or restored pass duplicated delivery")


def test_an_idle_f3_source_flushes_its_open_call_and_the_schema_admits_its_tool_exchange(tmp_path):
    """With #433 the validator accepts the F3 kinds, so the open call that an idle cut flushes result-missing is
    admitted and written as a tool_exchange (before #433 this same batch was refused locally, E_CONTEXT_SCHEMA)."""
    src = tmp_path / "rollout.jsonl"
    _open_call(src)
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER)
    assert report.conflicts == [], report.conflicts
    assert report.exit_code() == 0
    items = sorted(v["item"] for k, v in server.rows.items() if k.startswith("ctx/"))
    assert "tool_exchange" in items, items
    off = Server()
    assert cu.run_pass(cfg(tmp_path / "off", src), off, now=LATER).exit_code() == 0
    assert sorted(v["item"] for k, v in off.rows.items() if k.startswith("ctx/")) == ["assistant_text", "user_text"]


#: The item kinds a validator knew before F3 (#433). Pinned so the refusal path below is tested against a validator
#: that does not accept tool_exchange: since #433 main's own validator accepts it, and nothing else tests that a kind
#: the validator does not know is refused LOCALLY, before any write (rescued from kxxx/<thread>).
PRE_F3_ITEM_KINDS = frozenset({"user_text", "assistant_text", "task", "artifact", "omitted"})


def test_a_batch_kind_the_validator_does_not_know_is_refused_locally_before_any_write(tmp_path, monkeypatch):
    from apps.context import validation
    monkeypatch.setattr(validation, "ITEM_KINDS", PRE_F3_ITEM_KINDS)
    src = tmp_path / "rollout.jsonl"
    _open_call(src)
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER)
    # The idle cut flushes the open call result-missing into the batch as a tool_exchange, which this validator
    # does not accept: refused locally, nothing sent.
    assert [c["code"].split(":")[0] for c in report.conflicts] == ["E_CONTEXT_SCHEMA"], report.conflicts
    assert "tool_exchange" not in validation.ITEM_KINDS, "control: the refusal came from the pinned pre-F3 kinds"
    assert server.writes == []


def _admit_f3(monkeypatch):
    """The validator refuses the F3 kinds until its own reviewed PR; these tests look at what the uploader FREEZES,
    so they freeze without that admission step (everything else in the path is the real one)."""
    monkeypatch.setattr(cu, "freeze_screened",
                        lambda outbox, expected, off, items, manifest, checkpoint, **kw:
                        outbox.freeze(expected, off, items, manifest, checkpoint))


def _day_old(path, records):
    write_lines(path, records)
    idle(path)


DAY_AGO = "2026-10-09T11:00:00Z"            # LATER is 2026-10-10 12:00Z: an open call this old is due


def _call(call_id, stamp, args="{}"):
    return _rec("response_item", {"type": "function_call", "call_id": call_id, "name": "shell", "arguments": args},
                stamp)


def _batches(server):
    out = {}
    for k, v in server.rows.items():
        if k.startswith("ctx/"):
            out.setdefault(v["batch"], []).append(v["item"])
    return out


def test_an_idle_flush_that_fills_the_open_batch_freezes_it(tmp_path, monkeypatch):
    _admit_f3(monkeypatch)
    src = tmp_path / "rollout.jsonl"
    _day_old(src, [meta("s-fill"), user(1, "q", DAY_AGO), _call("c1", DAY_AGO)])
    server = Server()
    first = cu.run_pass(cfg(tmp_path, src, f3=True, batch_items=2), server, now=LATER)
    assert first.conflicts == [] and _batches(server) == {1: ["user_text", "tool_exchange"]}
    again = cu.run_pass(cfg(tmp_path, src, f3=True, batch_items=2), server, now=LATER)
    assert again.records_pending == 0 and len(item_rows(server)) == 2


def test_an_idle_flush_that_would_spill_cuts_before_it_and_freezes_both_batches(tmp_path, monkeypatch):
    _admit_f3(monkeypatch)
    src = tmp_path / "rollout.jsonl"
    big = json.dumps({"blob": "x" * 100_000})                 # two parts: more than the one slot left open
    _day_old(src, [meta("s-spill"), user(1, "q", DAY_AGO), _call("c1", DAY_AGO, big)])
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, f3=True, batch_items=2), server, now=LATER)
    assert report.conflicts == []
    assert _batches(server) == {1: ["user_text"], 2: ["tool_exchange", "tool_exchange"]}
    parts = sorted((v for v in server.rows.values() if v.get("item") == "tool_exchange"), key=lambda v: v["part"])
    assert [(p["part"], p["parts"], p["result_status"]) for p in parts] == [(1, 2, "missing"), (2, 2, "missing")]


def test_a_long_tool_call_on_an_idle_file_stays_one_exchange_under_the_uploader(tmp_path, monkeypatch):
    _admit_f3(monkeypatch)
    src = tmp_path / "rollout.jsonl"
    _day_old(src, [meta("s-long"), user(1, "q", "2026-10-10T10:50:00Z"), _call("c1", "2026-10-10T10:50:00Z")])
    server = Server()
    cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER)   # 70 minutes idle: the call keeps waiting
    assert all(v["item"] != "tool_exchange" for v in item_rows(server).values())
    write_lines(src, [_rec("response_item", {"type": "function_call_output", "call_id": "c1", "output": "done"},
                           "2026-10-10T12:00:00Z")], mode="a")
    idle(src)
    cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER + timedelta(minutes=10))
    [x] = [v for v in item_rows(server).values() if v["item"] == "tool_exchange"]
    assert (x["call_status"], x["result_status"]) == ("ok", "ok")



def test_a_call_waiting_in_the_checkpoint_is_flushed_on_a_later_pass_with_no_new_lines(tmp_path, monkeypatch):
    # txxxx 68839a, GitHub Codex on a42dd4f2: the last records are a user item and a call younger than a day. Pass 1
    # freezes the user batch and keeps the call in the checkpoint; the file never grows again, and every later pass
    # returned before the flush, so the call was never stored, even a day later.
    _admit_f3(monkeypatch)
    src = tmp_path / "rollout.jsonl"
    stamp = "2026-10-10T10:50:00Z"
    _day_old(src, [meta("s-wait"), user(1, "q", stamp), _call("c1", stamp)])
    server = Server()
    cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER)
    assert sorted(v["item"] for v in item_rows(server).values()) == ["user_text"]
    cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER + timedelta(days=2))
    [x] = [v for v in item_rows(server).values() if v["item"] == "tool_exchange"]
    assert (x["call_status"], x["result_status"]) == ("ok", "missing")
    again = cu.run_pass(cfg(tmp_path, src, f3=True), server, now=LATER + timedelta(days=3))
    assert len(item_rows(server)) == 2 and again.conflicts == [], "nothing more to flush, nothing sent twice"


def test_an_exchange_wider_than_a_small_batch_does_not_stall_the_source(tmp_path, monkeypatch):
    _admit_f3(monkeypatch)
    src = tmp_path / "rollout.jsonl"
    stamp = "2026-10-10T10:00:00Z"
    _day_old(src, [meta("s-wide"), _call("c1", stamp),
                   _rec("response_item", {"type": "function_call_output", "call_id": "c1", "output": "w" * 150_000},
                        stamp),
                   said(1, "after it", stamp)])
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, f3=True, batch_items=2), server, now=LATER)
    assert report.conflicts == []
    assert sorted(v["item"] for v in item_rows(server).values()) == ["assistant_text", "omitted"]


def test_parallel_claude_results_wider_than_a_small_batch_do_not_stall_the_source(tmp_path, monkeypatch):
    # GitHub Codex on c2263808: one Claude record with two 100,000-character results at batch_items=2 emitted four
    # items, and every pass raised "one record produced more items than a batch holds" at that record.
    _admit_f3(monkeypatch)
    src = tmp_path / "session.jsonl"
    stamp = "2026-10-10T10:00:00.000Z"

    def rec(kind, uuid, content):
        return {"type": kind, "sessionId": "s-par", "uuid": uuid, "timestamp": stamp,
                "message": {"role": kind, "content": content}}
    _day_old(src, [rec("assistant", "a1", [{"type": "tool_use", "id": i, "name": "Bash", "input": {}}
                                           for i in ("W1", "W2")]),
                   rec("user", "u1", [{"type": "tool_result", "tool_use_id": i, "content": c * 100_000}
                                      for i, c in (("W1", "w"), ("W2", "v"))]),
                   rec("user", "u2", "after it")])
    server = Server()
    config = cu.Config(seat=SEAT, dialect="claude", store_dir=tmp_path / "store", sources=[str(src)], f3=True,
                       batch_items=2)
    report = cu.run_pass(config, server, now=LATER)
    assert report.conflicts == []
    assert sorted(v["item"] for v in item_rows(server).values()) == ["tool_exchange", "tool_exchange", "user_text"]


def test_more_parallel_results_than_the_default_batch_do_not_stall_the_source(tmp_path, monkeypatch):
    # nxxxxx 0bf133: 257 small results in one record at the default 256. The record keeps 256 exchanges, reports the
    # 257th as record_full, and the line after it still uploads.
    _admit_f3(monkeypatch)
    src = tmp_path / "session.jsonl"
    stamp = "2026-10-10T10:00:00.000Z"
    ids = ["P%d" % n for n in range(257)]

    def rec(kind, uuid, content):
        return {"type": kind, "sessionId": "s-257", "uuid": uuid, "timestamp": stamp,
                "message": {"role": kind, "content": content}}
    _day_old(src, [rec("assistant", "a1", [{"type": "tool_use", "id": i, "name": "Bash", "input": {}} for i in ids]),
                   rec("user", "u1", [{"type": "tool_result", "tool_use_id": i, "content": "ok"} for i in ids]),
                   rec("user", "u2", "after it")])
    server = Server()
    config = cu.Config(seat=SEAT, dialect="claude", store_dir=tmp_path / "store", sources=[str(src)], f3=True)
    report = cu.run_pass(config, server, now=LATER)
    assert report.conflicts == [] and report.exit_code() == 0
    kinds = [v["item"] for v in item_rows(server).values()]
    assert kinds.count("tool_exchange") == 256 and kinds.count("user_text") == 1


def test_a_record_with_thinking_and_plan_items_wider_than_the_batch_does_not_stall(tmp_path, monkeypatch):
    # GitHub Codex on 56d830a8 (txxxx f15fc8): thinking plus a TodoWrite adding two tasks at batch_items=2.
    _admit_f3(monkeypatch)
    src = tmp_path / "session.jsonl"
    stamp = "2026-10-10T10:00:00.000Z"
    todos = [{"content": s, "status": "pending"} for s in ("one", "two")]
    write = {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": todos}}

    def rec(kind, uuid, content):
        return {"type": kind, "sessionId": "s-plan", "uuid": uuid, "timestamp": stamp,
                "message": {"role": kind, "content": content}}
    _day_old(src, [rec("assistant", "a1", [{"type": "thinking", "thinking": "weigh"}, write]),
                   rec("user", "u2", "after it")])
    server = Server()
    config = cu.Config(seat=SEAT, dialect="claude", store_dir=tmp_path / "store", sources=[str(src)], f3=True,
                       batch_items=2)
    report = cu.run_pass(config, server, now=LATER)
    assert report.conflicts == []
    assert sorted(v["item"] for v in item_rows(server).values()) == ["task", "task", "user_text"]


def test_a_codex_plan_that_triggers_a_pressure_flush_does_not_stall_the_source(tmp_path, monkeypatch):
    # GitHub Codex on 7d96b08f (txxxx 1d1746): the flushed call and the plan's two tasks were three items at
    # batch_items=2, and every pass stopped at the update_plan line.
    _admit_f3(monkeypatch)
    src = tmp_path / "rollout.jsonl"
    stamp = "2026-10-10T10:00:00Z"
    plan = json.dumps({"explanation": "e" * 400, "plan": [{"step": "one", "status": "pending"},
                                                          {"step": "two", "status": "pending"}]})
    _day_old(src, [meta("s-press"), _call("big", stamp, "x" * (cp.F3_PENDING_BYTES - 200)),
                   _rec("response_item", {"type": "function_call", "call_id": "plan", "name": "update_plan",
                                          "arguments": plan}, stamp),
                   said(1, "after it", stamp)])
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, f3=True, batch_items=2), server, now=LATER)
    assert report.conflicts == []
    kinds = sorted(v["item"] for v in item_rows(server).values())
    assert kinds.count("task") == 2 and "assistant_text" in kinds, kinds


def test_the_real_preflight_freezes_an_f3_batch_and_checks_it_as_the_server_does(tmp_path, monkeypatch):
    # nxxxxx 9075a0, txxxx e7cea2: freeze_screened tallied only the four F1 count classes, so every manifest that
    # carried the F3 counts failed E_CONTEXT_MANIFEST before freezing. No _admit_f3 here: the REAL preflight runs,
    # against a validator that admits the F3 kinds as #433's does.
    import types
    calls = {"screened": [], "split": []}
    stub = types.SimpleNamespace(
        SCREENED_FIELDS=("text", "step"),
        validate_context=lambda ns, key, value, now=None: value["kind"],
        screens_apply=lambda ns: True,
        screens=types.SimpleNamespace(screen=lambda *texts: calls["screened"].append(texts)),
        _exchange_screen_text=lambda value: ["EXCHANGE:" + value["text"][:8]],
        _check_split_exchanges=lambda ns, stored: calls["split"].append([o for o, _v in stored]))
    monkeypatch.setitem(sys.modules, "apps.context.validation", stub)
    src = tmp_path / "session.jsonl"
    stamp = "2026-10-10T10:00:00.000Z"

    def rec(kind, uuid, content):
        return {"type": kind, "sessionId": "s-pre", "uuid": uuid, "timestamp": stamp,
                "message": {"role": kind, "content": content}}
    later = "2026-10-10T10:10:00.000Z"                    # ten minutes on: T2's call is due by the data clock

    def late(kind, uuid, content):
        return dict(rec(kind, uuid, content), timestamp=later)
    _day_old(src, [rec("assistant", "a1", [{"type": "tool_use", "id": "T1", "name": "Bash", "input": {}}]),
                   rec("user", "u1", [{"type": "tool_result", "tool_use_id": "T1", "content": "w" * 100_000}]),
                   rec("user", "u9", [{"type": "tool_result", "tool_use_id": "T9", "content": "orphan"}]),
                   rec("assistant", "a2", [{"type": "tool_use", "id": "T2", "name": "Bash", "input": {}}]),
                   late("user", "u2", "after it")])
    server = Server()
    config = cu.Config(seat=SEAT, dialect="claude", store_dir=tmp_path / "store", sources=[str(src)], f3=True)
    report = cu.run_pass(config, server, now=LATER)
    assert report.conflicts == [], report.conflicts
    kinds = sorted(v["item"] for v in item_rows(server).values())
    assert kinds == ["tool_exchange"] * 4 + ["user_text"], kinds
    counts = {k: 0 for k in ("tool_exchange", "call_missing", "result_missing", "parts_extra")}
    for v in (v for k, v in server.rows.items() if k.startswith("ctxm/")):
        for k in counts:
            counts[k] += v["counts"].get(k, 0)
    assert counts == {"tool_exchange": 4, "call_missing": 1, "result_missing": 1, "parts_extra": 1}, counts
    assert any(t and t[0].startswith("EXCHANGE:") for t in calls["screened"]), "exchanges screened as the server does"
    assert calls["split"] and all(calls["split"]), calls["split"]


def test_antigravity_f3_uploads_split_and_pressure_batches_through_real_validator(tmp_path):
    logs = tmp_path / "brain" / "ag-up-sess" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    src = logs / "transcript_full.jsonl"
    t0, t1, t2 = "2026-10-10T10:00:00Z", "2026-10-10T10:01:00Z", "2026-10-10T10:02:00Z"
    _day_old(src, [
        {"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": t0, "content": "<USER_REQUEST>\nbegin\n</USER_REQUEST>"},
        {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t0,
         "thinking": "weigh step", "tool_calls": [{"name": "big_call", "args": {"blob": "x" * (cp.F3_PENDING_BYTES - 200)}}]},
        {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t1,
         "content": "reply with reserve", "thinking": "second thought", "tool_calls": [{"name": "read_file", "args": {"Path": "/f"}}]},
        {"step_index": 4, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": t1, "content": "w" * 100_000},
        {"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t2,
         "tool_calls": [{"name": "c_a", "args": {}}, {"name": "c_b", "args": {}}]},
        {"step_index": 6, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": t2, "content": "<USER_REQUEST>\ninterrupt\n</USER_REQUEST>"},
        {"step_index": 7, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": t2, "content": "<USER_REQUEST>\nhold checkpoint at eof\n</USER_REQUEST>"},
        {"step_index": 9, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": t2, "content": "unmatched-early-tail"},
    ])
    on_srv, off_srv = Server(), Server()
    on_cfg = cu.Config(seat=SEAT, dialect="antigravity", store_dir=tmp_path / "on", sources=[str(src)], f3=True, batch_items=2)
    on_rep = cu.run_pass(on_cfg, on_srv, now=LATER)
    assert on_rep.conflicts == [] and on_rep.exit_code() == 0, on_rep.conflicts
    split_parts = [v for v in item_rows(on_srv).values() if v["item"] == "tool_exchange" and v["parts"] > 1]
    assert len(split_parts) == 2 and len({v["batch"] for v in split_parts}) == 1
    assert not any(v.get("call_status") == "missing" for v in item_rows(on_srv).values())
    on_rep2 = cu.run_pass(on_cfg, on_srv, now=LATER + timedelta(days=2))
    assert on_rep2.conflicts == [] and any(v.get("call_status") == "missing" for v in item_rows(on_srv).values())
    off_cfg = cu.Config(seat=SEAT, dialect="antigravity", store_dir=tmp_path / "off", sources=[str(src)], f3=False, batch_items=2)
    off_rep = cu.run_pass(off_cfg, off_srv, now=LATER)
    assert off_rep.conflicts == [] and off_rep.exit_code() == 0
    assert sorted(v["item"] for v in item_rows(off_srv).values()) == ["assistant_text", "user_text", "user_text", "user_text"]


# ---- visit order: newest last write first, inside one budget (seat-handover item 1, 2 Nov) -------------------------

def _aged(path, records, hours):
    write_lines(path, records)
    t = (LATER - timedelta(hours=hours)).timestamp()
    os.utime(path, (t, t))


def _backlogs(tmp_path):
    """An old session with a big backlog, and the seat's current session written since: the 2 Nov case, where the
    old file's backfill spent every pass's budget and the new session was never sent."""
    old, new = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    turns = []
    for n in range(1, 21):
        turns += [user(n, "old question %d" % n), said(n, "old answer %d" % n)]
    _aged(old, [meta("s-old")] + turns, hours=30)
    _aged(new, [meta("s-new"), user(1, "new question"), said(1, "new answer")], hours=1)
    return old, new


@pytest.mark.parametrize("given", [("old", "new"), ("new", "old")])
def test_the_newest_source_spends_the_budget_first_whatever_the_given_order(tmp_path, given):
    old, new = _backlogs(tmp_path)
    paths = {"old": str(old), "new": str(new)}
    sources = [paths[g] for g in given]
    c = cu.Config(seat=SEAT, dialect="codex", store_dir=tmp_path / "store", sources=sources, max_batches=1,
                  batch_items=2)
    report = cu.run_pass(c, Server(), now=LATER)
    assert report.conflicts == []
    # the budget of one is spent on the new session, and the pass stops before the old backlog
    assert report.visited == [{"source_index": sources.index(paths["new"]), "batches": 1}]


def test_every_source_is_visited_newest_first_when_the_budget_allows(tmp_path):
    old, new = _backlogs(tmp_path)
    c = cu.Config(seat=SEAT, dialect="codex", store_dir=tmp_path / "store", sources=[str(old), str(new)],
                  batch_items=2)
    report = cu.run_pass(c, Server(), now=LATER)
    assert [v["source_index"] for v in report.visited] == [1, 0]
    assert all(v["batches"] > 0 for v in report.visited), report.visited


def test_a_source_that_cannot_be_stated_is_visited_last_and_reported(tmp_path):
    old, new = _backlogs(tmp_path)
    gone = tmp_path / "gone.jsonl"
    c = cu.Config(seat=SEAT, dialect="codex", store_dir=tmp_path / "store", sources=[str(gone), str(old), str(new)],
                  batch_items=2)
    report = cu.run_pass(c, Server(), now=LATER)
    assert [v["source_index"] for v in report.visited] == [2, 1, 0]
    assert report.visited[-1]["batches"] == 0
    assert {"src": None, "code": "source missing"} in report.conflicts


# ---- completed-batch evidence pruning & StoreFull handling (3 Nov, antigravity/kxxx) --------------------------------

def _store_db_for(store_dir: Path, source: Path) -> Path:
    info = os.stat(source, follow_symlinks=False)
    file_id = hashlib.sha256(f"file|{info.st_dev}|{info.st_ino}".encode("utf-8")).hexdigest()[:32]
    return store_dir / file_id / "store" / "incremental.sqlite"


def test_completed_batches_prune_readback_evidence_during_drain(tmp_path):
    src = tmp_path / "rollout.jsonl"
    turns = [meta("s-prune")]
    for n in range(1, 4):
        turns += [user(n, "q %d" % n), said(n, "a %d" % n)]
    write_lines(src, turns)
    idle(src)
    server = Server()
    report = cu.run_pass(cfg(tmp_path, src, batch_items=2), server, now=LATER)
    assert (report.exit_code(), report.batches_committed, report.items_committed) == (0, 3, 6)
    db_path = _store_db_for(tmp_path / "store", src)
    import sqlite3
    with sqlite3.connect(db_path) as db:
        ev_count = db.execute("SELECT COUNT(*) FROM outbox_evidence_v1").fetchone()[0]
        matched_count = db.execute("SELECT COUNT(*) FROM outbox_receipts_v1 WHERE state='matched'").fetchone()[0]
    assert ev_count == 0, "completed batches left readback artifacts in outbox_evidence_v1"
    assert matched_count == 9


def test_wedged_full_store_exits_4_when_unprunable_and_recovers_in_place_via_evidence_pruning(tmp_path, monkeypatch):
    """antigravity-prod 3 Nov incident: 21,125 historical outbox_evidence_v1 blobs filled the 1 GiB SQLite page cap
    mid-batch. Without pruning, SQLITE_FULL must exit 4 (StoreFull conflict) rather than looping silently as exit 5
    (unreachable) and must not block other healthy sources; with pruning enabled, SourcePass.__init__ frees completed
    batches' evidence onto SQLite's freelist before the pending readback runs, recovering the wedged store in place."""
    import sqlite3
    from dataclasses import replace

    wedged = tmp_path / "wedged.jsonl"
    healthy = tmp_path / "healthy.jsonl"
    _aged(wedged, [meta("s-wedged"), user(1, "q1"), said(1, "a1"), user(2, "q2"), said(2, "a2")], hours=2)
    _aged(healthy, [meta("s-healthy"), user(1, "hq1"), said(1, "ha1")], hours=3)

    class FatProofServer(Server):
        def __init__(self):
            super().__init__()
            self.fail_on_write_n = None

        def write(self, key, value):
            if self.fail_on_write_n is not None and len(self.writes) >= self.fail_on_write_n:
                raise cu.Unreachable("mid-batch stop")
            return super().write(key, value)

        def reconcile(self, outbox, batch_key):
            action = outbox.next_action(batch_key)
            assert action.kind == "read"
            value = self.rows.get(action.record.key)
            blob = json.dumps({"key": action.record.key, "token": action.attempt.token, "pad": "P" * 32768}).encode()
            return outbox.observe(action.attempt, value=None if value is None else cp.serialize(value),
                                  evidence_sha256=hashlib.sha256(blob).hexdigest(), evidence_artifact=blob)

    real_prune = cu.FrozenOutbox.prune_completed_evidence
    monkeypatch.setattr(cu.FrozenOutbox, "prune_completed_evidence", lambda self, *a, **k: 0)

    srv = FatProofServer()
    # Batch 1 (2 items + manifest = 3 writes), then Batch 2 writes item 0 + starts item 1 before transport stops
    srv.fail_on_write_n = 5
    r1 = cu.run_pass(cfg(tmp_path, wedged, batch_items=2), srv, now=LATER)
    assert r1.exit_code() == 5 and r1.batches_committed == 1
    srv.fail_on_write_n = None

    db_path = _store_db_for(tmp_path / "store", wedged)
    with sqlite3.connect(db_path) as db:
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM outbox_evidence_v1").fetchone()[0] >= 4
    monkeypatch.setattr(cu, "LIMITS", replace(cu.LIMITS, database_bytes=pages * page_size))

    # 1. With pruning disabled, hitting SQLITE_FULL reports StoreFull as exit 4 (conflict), NOT exit 5 (unreachable),
    #    and still visits and drains the second healthy session file in the same pass!
    multi_cfg = cu.Config(seat=SEAT, dialect="codex", store_dir=tmp_path / "store",
                          sources=[str(wedged), str(healthy)], batch_items=2)
    r_full = cu.run_pass(multi_cfg, srv, now=LATER)
    assert r_full.exit_code() == 4
    assert r_full.conflicts == [{"src": None, "code": "store: private capture store reached configured byte limit"}]
    assert len([k for k in item_rows(srv) if "/s-healthy/" in k or v_src(srv, k) == "s-healthy"]) == 2 or r_full.batches_committed == 1

    # 2. Restore real pruning under the EXACT SAME 0-free-page database_bytes cap: wedged store frees Batch 1's pages
    #    onto SQLite's freelist and drains Batch 2 cleanly with exit 0!
    monkeypatch.setattr(cu.FrozenOutbox, "prune_completed_evidence", real_prune)
    r_recovered = cu.run_pass(cfg(tmp_path, wedged, batch_items=2), srv, now=LATER)
    assert r_recovered.exit_code() == 0
    assert r_recovered.batches_committed == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox_evidence_v1").fetchone()[0] == 0
        assert db.execute("PRAGMA freelist_count").fetchone()[0] > 0


def v_src(server, key):
    return server.rows[key].get("src")


def test_persisted_holds_on_unvisited_sources_stay_visible_without_starving_active_source(tmp_path):
    old = tmp_path / "old.jsonl"
    active = tmp_path / "active.jsonl"
    write_lines(old, [meta("s-old"), user(1, "old q"), said(1, "old a")])
    idle(old)
    os.utime(old, (LATER.timestamp() - 7200, LATER.timestamp() - 7200))

    server = Server()
    old_item = expected_items(old)[1]
    old_src = old_item["src"]
    second_old = cp.item_key(old_item)
    server.refuse[second_old] = "E_CONTEXT_HELD_OLD"
    r_hold = cu.run_pass(cfg(tmp_path, old, batch_items=2), server, now=LATER)
    assert (r_hold.status(), r_hold.exit_code()) == ("held", 3)
    assert len(r_hold.context_held) == 1 and r_hold.context_held[0]["src"] == old_src

    # Now active.jsonl has newer mtime and spends the entire max_batches=2 budget before old.jsonl is visited.
    write_lines(active, [meta("s-active"), user(1, "act q1"), said(1, "act a1"), user(2, "act q2"), said(2, "act a2")])
    os.utime(active, (LATER.timestamp() - 600, LATER.timestamp() - 600))
    multi = cu.Config(seat=SEAT, dialect="codex", store_dir=tmp_path / "store",
                      sources=[str(old), str(active)], batch_items=2, max_batches=2)

    r_prog = cu.run_pass(multi, server, now=LATER + timedelta(hours=2))
    assert r_prog.visited == [{"source_index": 1, "batches": 2}], "old.jsonl should not have been visited"
    assert (r_prog.status(), r_prog.exit_code()) == ("progressed", 0), "active source must keep progressing"
    assert r_prog.failed == {}, "unvisited persisted hold must not double-count per-pass accounting.failed"
    assert r_prog.context_held == [{
        "src": old_src, "gen": 1, "batch": 1, "first_ord": 1, "last_ord": 1,
        "code": "E_CONTEXT_HELD_OLD", "age_s": 7200,
    }]

    # Verify legacy 2-field held.json (pre-metadata format) also resolves cleanly while unvisited,
    # without instantiating Store() or modifying incremental.sqlite, and corrupt/unreadable unvisited
    # sidecars (enrollment.json, paths.json, missing sqlite) never abort run_pass().
    [held_file] = [p for p in (tmp_path / "store").rglob("held.json") if json.loads(p.read_text())]
    raw_held = json.loads(held_file.read_text())
    legacy_held = {k: {"since": v["since"], "code": v["code"]} for k, v in raw_held.items()}
    held_file.write_text(json.dumps(legacy_held), encoding="utf-8")

    corrupt_src = tmp_path / "corrupt-unvisited.jsonl"
    write_lines(corrupt_src, [meta("s-corrupt"), user(1, "cq"), said(1, "ca")])
    os.utime(corrupt_src, (LATER.timestamp() - 9000, LATER.timestamp() - 9000))
    c_info = os.stat(corrupt_src)
    c_dir = (tmp_path / "store") / cu._sha("file", c_info.st_dev, c_info.st_ino)[:32]
    c_dir.mkdir(parents=True, exist_ok=True)
    (c_dir / "enrollment.json").write_bytes(b"{corrupt-enrollment-json")
    (c_dir / "held.json").write_text(json.dumps({"deadbeef": {"since": 1.0, "code": "E_FAKE"}}), encoding="utf-8")

    old_db = _store_db_for(tmp_path / "store", old)
    db_bytes_before = old_db.read_bytes()
    multi_with_corrupt = cu.Config(seat=SEAT, dialect="codex", store_dir=tmp_path / "store",
                                   sources=[str(corrupt_src), str(old), str(active)], batch_items=2, max_batches=2)

    r_leg = cu.run_pass(multi_with_corrupt, server, now=LATER + timedelta(hours=3))
    assert r_leg.visited == [{"source_index": 2, "batches": 2}]
    assert (r_leg.status(), r_leg.exit_code()) == ("progressed", 0)
    assert old_db.read_bytes() == db_bytes_before, "legacy hold lookup must be strictly read-only"
    assert not (c_dir / "store").exists(), "unvisited corrupt source must never create a Store directory"
    assert r_leg.context_held == [{
        "src": old_src, "gen": 1, "batch": 1, "first_ord": 1, "last_ord": 1,
        "code": "E_CONTEXT_HELD_OLD", "age_s": 10800,
    }]

    # Drain remaining active batches; once active is drained, old's hold gives status='held' (rc=3), and clearing
    # server.refuse drains old and empties held.json.
    for _ in range(4):
        r_step = cu.run_pass(multi, server, now=LATER + timedelta(hours=3))
        if r_step.status() == "held":
            assert r_step.exit_code() == 3
            break
    else:
        raise AssertionError("expected status='held' once active source drained")

    del server.refuse[second_old]
    r_clear = cu.run_pass(multi, server, now=LATER + timedelta(hours=4))
    assert r_clear.context_held == [] and json.loads(held_file.read_text()) == {}

