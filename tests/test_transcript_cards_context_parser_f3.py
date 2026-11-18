"""F3 item kinds in context_parser (ingest plan D1, step 3 item 8, step 9b): tool_exchange and thinking, off by default.

Spec agreed with txxxx (parser owner) on <thread>: kxxx 668fd6, txxxx 6b8c71, kxxx 100a81.
Every rule has a test here that fails when the rule is taken out (tools/mutants/lists/context_parser_f3.py).
Synthetic text only.
"""
import json
import os
import sys

LOADER_DIR = os.environ.get("TRANSCRIPT_CARDS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
if LOADER_DIR not in sys.path:
    sys.path.insert(0, LOADER_DIR)

import context_parser as cp  # noqa: E402

SEAT = "fixture-seat"
NS = "shared/transcript-context"          # unscreened, as production
SECRET = "-----BEGIN PRIVATE KEY-----"


def t(minute, second=0):
    return "2026-10-27T08:%02d:%02d.000Z" % (minute, second)


# ---- Claude records ------------------------------------------------------------------------------------------------

def cl(kind, uuid, minute, content, **extra):
    rec = {"type": kind, "sessionId": "f3-sess", "uuid": uuid, "timestamp": t(minute),
           "message": {"role": kind, "content": content}}
    rec.update(extra)
    return rec


def call(uuid, minute, tool_id, name="Bash", inp=None):
    return cl("assistant", uuid, minute, [{"type": "tool_use", "id": tool_id, "name": name,
                                           "input": inp if inp is not None else {"command": "ls"}}])


def result(uuid, minute, tool_id, body="ok", is_error=False):
    return cl("user", uuid, minute, [{"type": "tool_result", "tool_use_id": tool_id, "content": body,
                                      "is_error": is_error}])


def said(uuid, minute, text):
    return cl("user", uuid, minute, text)


def parser(f3=True, namespace=NS, dialect="claude", **kw):
    return cp.ContextParser(SEAT, dialect, namespace=namespace, f3=f3, **kw)


def feed(p, records):
    for r in records:
        p.feed(r)
    return p


def exchanges(p):
    return [i for i in p.items if i["item"] == "tool_exchange"]


def whole(item):
    return json.loads(item["text"])


# ---- off by default -------------------------------------------------------------------------------------------------

def test_f3_off_by_default_emits_no_new_kinds_and_keeps_no_pairing_state():
    p = cp.ContextParser(SEAT, "claude", namespace=NS)
    assert p.f3 is False
    feed(p, [said("u0", 0, "hi"), call("a1", 1, "toolu_1"), result("u1", 1, "toolu_1"),
             cl("assistant", "a2", 2, [{"type": "thinking", "thinking": "plain thought", "signature": "s"}])])
    assert [i["item"] for i in p.items] == ["user_text"]
    s = p.state()
    assert (s["f3"], s["tool_pending"], s["tool_seen"], s["newest_time"], s["f3_dialect"]) == (False, [], [], "", {})


# ---- pairing ----------------------------------------------------------------------------------------------------

def test_a_call_and_its_result_are_one_item_call_then_result():
    p = feed(parser(), [call("a1", 1, "toolu_1", inp={"command": "ls -la"}), result("u1", 2, "toolu_1", "file.txt")])
    [x] = exchanges(p)
    assert whole(x) == {"tool": "Bash", "call": '{"command":"ls -la"}', "result": "file.txt"}
    assert (x["call_status"], x["result_status"], x["part"], x["parts"]) == ("ok", "ok", 1, 1)
    assert x["tool_ref"] == cp._hex32("toolu_1") and x["time"] == t(1)
    assert x["text_sha256"] == x["exchange_sha256"] == cp._sha(x["text"])
    assert p.tool_pending == []


def test_an_error_result_is_marked_error():
    p = feed(parser(), [call("a1", 1, "toolu_1"), result("u1", 1, "toolu_1", "boom", is_error=True)])
    assert exchanges(p)[0]["result_status"] == "error"


def test_results_out_of_order_pair_by_id_never_by_position():
    p = feed(parser(), [call("a1", 1, "A"), call("a2", 1, "B"), result("u2", 2, "B", "rb"), result("u1", 2, "A", "ra")])
    got = [(x["tool_ref"], whole(x)["result"]) for x in exchanges(p)]
    assert got == [(cp._hex32("B"), "rb"), (cp._hex32("A"), "ra")]


def test_a_duplicate_result_is_reported_not_emitted_twice():
    p = feed(parser(), [call("a1", 1, "A"), result("u1", 1, "A", "first"), result("u2", 1, "A", "second")])
    assert [whole(x)["result"] for x in exchanges(p)] == ["first"]
    assert ("tool_duplicate_result", "u2") in p.skipped


def test_a_duplicate_call_while_waiting_is_reported():
    p = feed(parser(), [call("a1", 1, "A"), call("a2", 1, "A")])
    assert ("tool_duplicate_call", "a2") in p.skipped and len(p.tool_pending) == 1


# ---- flush by data time --------------------------------------------------------------------------------------------

def test_flush_sends_only_calls_five_minutes_older_than_the_newest_record():
    p = feed(parser(), [call("a1", 0, "OLD"), call("a2", 4, "NEW"), said("u9", 5, "later")])
    out = p.flush_pending()
    assert [(x["tool_ref"], x["result_status"]) for x in out] == [(cp._hex32("OLD"), "missing")]
    assert whole(out[0])["result"] is None
    assert [e[0] for e in p.tool_pending] == [cp._hex32("NEW")]


def test_flush_at_exactly_five_minutes_and_not_one_second_before():
    early = feed(parser(), [call("a1", 0, "X"), cl("user", "u", 4, "later")])
    early.newest_time = "2026-10-27T08:04:59.000Z"
    assert early.flush_pending() == []
    edge = feed(parser(), [call("a1", 0, "X"), said("u", 5, "later")])
    assert len(edge.flush_pending()) == 1


def test_flush_sends_a_call_with_no_usable_time():
    p = parser()
    p.feed(dict(call("a1", 1, "X"), timestamp="not a time"))
    assert len(p.tool_pending) == 1, "a call with no time still waits for its result"
    assert p.flush_pending() == []                # flushed, but an item needs a time: reported, never invented
    assert p.tool_pending == [] and ("no_time", "a1") in p.skipped


def test_the_data_clock_survives_a_checkpoint_round_trip():
    p = feed(parser(), [call("a1", 0, "OLD"), call("a2", 4, "NEW"), said("u", 5, "later")])
    resumed = cp.ContextParser.from_state(json.loads(json.dumps(p.state())), namespace=NS)
    assert resumed.newest_time == t(5)
    assert [x["tool_ref"] for x in resumed.flush_pending()] == [cp._hex32("OLD")], "only the 5-minute-old call"


def test_an_idle_file_flushes_by_the_callers_clock_only_after_a_day():
    p = feed(parser(), [call("a1", 0, "LAST")])          # the session ended with this call open
    assert p.flush_pending() == [], "by data time alone it is 0 minutes old"
    assert p.flush_pending(now="2026-10-28T07:59:59.000Z") == [], "one second short of a day it still waits"
    assert [x["result_status"] for x in p.flush_pending(now="2026-10-28T08:00:00.000Z")] == ["missing"]


def test_a_long_tool_call_stays_one_exchange_while_the_file_is_idle():
    # a 70-minute test gate, and an operator question answered after 6 hours (txxxx ccb845: 29 such calls measured)
    for name, later in (("Bash", "2026-10-27T09:10:00.000Z"), ("AskUserQuestion", "2026-10-27T14:00:00.000Z")):
        p = feed(parser(), [call("a1", 0, "LONG", name=name)])
        assert p.due_pending(now=later) == [] and p.flush_pending(now=later) == []
        p.feed(dict(result("u1", 0, "LONG", "done"), timestamp=later))
        [x] = exchanges(p)
        assert (x["call_status"], x["result_status"], whole(x)["tool"]) == ("ok", "ok", name)


def test_due_pending_names_the_calls_flush_would_send_in_call_order():
    p = feed(parser(), [call("a1", 0, "A"), call("a2", 1, "B"), call("a3", 4, "C"), said("u", 6, "later")])
    assert p.due_pending() == [cp._hex32("A"), cp._hex32("B")]
    assert [x["tool_ref"] for x in p.flush_pending(only=cp._hex32("B"))] == [cp._hex32("B")]
    assert [e[0] for e in p.tool_pending] == [cp._hex32("A"), cp._hex32("C")]


def test_a_late_result_after_a_flush_is_its_own_call_missing_item():
    p = feed(parser(), [call("a1", 0, "X"), said("u", 6, "later")])
    p.flush_pending()
    p.feed(result("u2", 7, "X", "finally"))
    got = [(x["call_status"], x["result_status"], whole(x)["result"]) for x in exchanges(p)]
    assert got == [("ok", "missing", None), ("missing", "ok", "finally")]


def test_a_result_whose_call_was_never_seen_is_call_missing():
    p = feed(parser(), [result("u1", 1, "GHOST", "orphan")])
    [x] = exchanges(p)
    assert (x["call_status"], whole(x)) == ("missing", {"tool": None, "call": None, "result": "orphan"})


# ---- bounded checkpoint ---------------------------------------------------------------------------------------------

def test_pending_calls_are_capped_and_the_oldest_goes_out_result_missing(monkeypatch):
    monkeypatch.setattr(cp, "F3_PENDING_BYTES", 4000)
    big = {"blob": "x" * 1500}
    p = feed(parser(), [call("a%d" % n, 1, "C%d" % n, inp=big) for n in range(4)])
    assert len(cp.serialize(p.tool_pending)) <= 4000
    assert [x["result_status"] for x in exchanges(p)] and all(x["result_status"] == "missing" for x in exchanges(p))
    assert exchanges(p)[0]["tool_ref"] == cp._hex32("C0")


def test_seen_ids_are_bounded_and_an_old_duplicate_is_call_missing_not_new(monkeypatch):
    monkeypatch.setattr(cp, "F3_SEEN_IDS", 3)
    recs = []
    for n in range(5):
        recs += [call("a%d" % n, 1, "S%d" % n), result("u%d" % n, 1, "S%d" % n, "r%d" % n)]
    p = feed(parser(), recs)
    assert len(p.tool_seen) == 3
    p.feed(result("u-old", 2, "S0", "again"))           # S0 fell out of the window
    last = exchanges(p)[-1]
    assert (last["call_status"], whole(last)["result"]) == ("missing", "again")
    p.feed(result("u-new", 2, "S4", "again"))           # S4 is still in the window
    assert ("tool_duplicate_result", "u-new") in p.skipped


# ---- size: screen whole, then split losslessly ----------------------------------------------------------------------

def test_a_large_exchange_is_split_into_parts_that_rebuild_it_byte_for_byte():
    body = "line é\U0001f9ed \"quoted\"\n" * 9000            # ~200 KB, with escapes and astral characters
    p = feed(parser(), [call("a1", 1, "BIG"), result("u1", 1, "BIG", body)])
    parts = exchanges(p)
    assert len(parts) > 1 and [x["part"] for x in parts] == list(range(1, len(parts) + 1))
    assert {x["parts"] for x in parts} == {len(parts)}
    rebuilt = "".join(x["text"] for x in parts)
    # digests, not the strings: a failure must not make pytest diff 200 KB of text
    assert cp._sha(rebuilt) == cp._sha(json.dumps({"tool": "Bash", "call": '{"command":"ls"}', "result": body},
                                                  ensure_ascii=False, separators=(",", ":")))
    assert all(x["exchange_sha256"] == cp._sha(rebuilt) for x in parts)
    assert all(len(cp.serialize(x)) <= cp.MAX_VALUE_BYTES for x in parts)
    assert [x["ordinal"] for x in parts] == list(range(parts[0]["ordinal"], parts[0]["ordinal"] + len(parts)))


def test_a_secret_across_a_part_boundary_is_caught_by_screening_the_whole_exchange():
    probe = feed(parser(namespace=None), [call("a1", 1, "P"), result("u1", 1, "P", "y" * 150_000)])
    first = exchanges(probe)[0]["text"]
    cut = len(first)                                      # where part 1 ends in the whole exchange text
    prefix = json.dumps({"tool": "Bash", "call": '{"command":"ls"}', "result": ""}, separators=(",", ":"))[:-2]
    offset = cut - len(prefix) - 10                       # SECRET straddles the boundary by 10 characters
    body = "y" * offset + SECRET + "y" * (150_000 - offset - len(SECRET))
    whole_text = json.dumps({"tool": "Bash", "call": '{"command":"ls"}', "result": body},
                            ensure_ascii=False, separators=(",", ":"))
    assert SECRET not in whole_text[:cut] and SECRET not in whole_text[cut:], "the fixture must straddle the cut"
    assert SECRET in whole_text
    p = feed(parser(namespace=None), [call("a1", 1, "P"), result("u1", 1, "P", body)])
    assert exchanges(p) == []
    assert [(i["item"], i["omitted_reason"]) for i in p.items] == [("omitted", "secret")]


def test_unscreened_namespace_keeps_the_secret_whole_across_parts():
    body = "z" * 100_000 + SECRET + "z" * 100_000
    p = feed(parser(), [call("a1", 1, "K"), result("u1", 1, "K", body)])
    assert SECRET in "".join(x["text"] for x in exchanges(p)), "kept whole, unscreened"


def test_an_exchange_past_the_part_cap_is_omitted_too_large(monkeypatch):
    monkeypatch.setattr(cp, "F3_MAX_PARTS", 3)
    body = "w" * 300_000
    p = feed(parser(), [call("a1", 1, "H"), result("u1", 1, "H", body)])
    assert [(i["item"], i.get("omitted_reason")) for i in p.items] == [("omitted", "too_large")]


def test_split_text_concatenates_back_and_respects_the_budget():
    text = "a\"\\\n\u0001é\U0001f9ed" * 5000
    pieces = cp._split_text(text, 1000)
    assert cp._sha("".join(pieces)) == cp._sha(text) and all(cp._json_text_bytes(p) <= 1000 for p in pieces)
    assert cp._split_text("abc", 5) is None


# ---- thinking and exclusions ---------------------------------------------------------------------------------------

def test_claude_plain_thinking_is_an_item_and_signature_only_thinking_is_excluded():
    p = feed(parser(), [cl("assistant", "a1", 1, [{"type": "thinking", "thinking": "weigh options", "signature": "s"},
                                                  {"type": "thinking", "thinking": "", "signature": "s"},
                                                  {"type": "redacted_thinking", "data": "opaque"}])])
    assert [(i["item"], i.get("text")) for i in p.items] == [("thinking", "weigh options")]
    assert [s for s in p.skipped if s[0] == "encrypted_reasoning"] == [("encrypted_reasoning", "a1")] * 2


def test_claude_meta_and_compaction_records_are_reported_with_f3():
    p = feed(parser(), [said("u0", 0, "hi"), dict(said("m", 0, "meta"), isMeta=True),
                        dict(said("c", 0, "summary"), isCompactSummary=True)])
    assert ("is_meta", "m") in p.skipped and ("compact_summary", "c") in p.skipped
    assert [i["item"] for i in p.items] == ["user_text"]


def cx(ptype, minute, **payload):
    return {"type": "response_item", "timestamp": t(minute), "payload": dict(type=ptype, **payload)}


def cx_parser():
    p = parser(dialect="codex")
    p.feed({"type": "session_meta", "timestamp": t(0), "payload": {"id": "cx-sess", "source": "cli"}})
    return p


def test_codex_custom_and_function_calls_pair_with_their_outputs():
    p = feed(cx_parser(), [cx("custom_tool_call", 1, call_id="c1", name="apply_patch", input="*** patch"),
                           cx("function_call", 1, call_id="c2", name="shell", arguments='{"cmd":"ls"}'),
                           cx("function_call_output", 2, call_id="c2", output="listing"),
                           cx("custom_tool_call_output", 2, call_id="c1", output={"ok": True})])
    assert [whole(x) for x in exchanges(p)] == [
        {"tool": "shell", "call": '{"cmd":"ls"}', "result": "listing"},
        {"tool": "apply_patch", "call": "*** patch", "result": '{"ok":true}'}]


def test_codex_encrypted_reasoning_is_excluded_and_readable_reasoning_and_analysis_are_thinking():
    p = feed(cx_parser(), [cx("reasoning", 1, id="r1", summary=[], encrypted_content="opaque"),
                           cx("reasoning", 1, id="r2", summary=[{"type": "summary_text", "text": "plan it"}]),
                           cx("message", 1, id="m1", role="assistant", phase="analysis",
                              content=[{"type": "output_text", "text": "think aloud"}])])
    assert [(i["item"], i["text"]) for i in p.items] == [("thinking", "plan it"), ("thinking", "think aloud")]
    assert ("encrypted_reasoning", "r1") in p.skipped


def test_codex_compaction_is_reported():
    p = feed(cx_parser(), [{"type": "compacted", "timestamp": t(1), "payload": {"message": "summary"}}])
    assert ("compact_summary", None) in p.skipped


# ---- manifests ---------------------------------------------------------------------------------------------------

def test_manifest_counts_carry_the_f3_classes_only_in_a_batch_with_f3_items():
    p = feed(parser(), [said("u0", 0, "hi")])
    p.close_batch()
    feed(p, [call("a1", 1, "A"), result("u1", 1, "A", "r"), call("a2", 1, "B"), said("u9", 7, "later")])
    p.flush_pending()
    p.feed(result("u3", 8, "B", "late"))
    p.feed(result("u4", 8, "C", "orphan"))
    m1, m2 = cp.manifests(p.items)
    assert set(m1["counts"]) == {"written", "omitted_secret", "omitted_client_name", "omitted_too_large"}
    assert {k: m2["counts"][k] for k in ("tool_exchange", "call_missing", "result_missing", "parts_extra")} == {
        "tool_exchange": 4, "call_missing": 2, "result_missing": 1, "parts_extra": 0}


# ---- checkpoint: v8, resume, upgrade, mode switch -------------------------------------------------------------------

def _resume_everywhere(records, cuts=(), flush_at=()):
    """Feed the records, cutting batches after the given indexes and flushing before them; then the same with a JSON
    round trip of the checkpoint before EVERY record. Returns both item lists."""
    def run(roundtrip):
        p = parser()
        for n, r in enumerate(records):
            if roundtrip:
                items = p.items
                p = cp.ContextParser.from_state(json.loads(json.dumps(p.state())), namespace=NS)
                p.items = items
            p.feed(r)
            if n in flush_at:
                p.flush_pending()
            if n in cuts:
                p.close_batch()
        return p.items
    return run(False), run(True)


def test_resume_at_every_record_with_calls_pending_gives_the_same_items():
    records = [said("u0", 0, "go"), call("a1", 0, "A"), call("a2", 1, "B"), said("u1", 2, "mid"),
               result("u2", 3, "B", "rb"), said("u3", 9, "much later"), result("u4", 10, "A", "late a"),
               result("u5", 10, "B", "dup"), cl("assistant", "a9", 10, [{"type": "thinking", "thinking": "t"}])]
    straight, resumed = _resume_everywhere(records, cuts=(3, 5), flush_at=(5,))
    assert [cp.serialize(i) for i in resumed] == [cp.serialize(i) for i in straight]
    assert any(x["result_status"] == "missing" for x in straight if x["item"] == "tool_exchange")


def test_a_v7_checkpoint_of_every_dialect_resumes_with_f3_off():
    for dialect in ("codex", "claude", "antigravity"):
        p = cp.ContextParser(SEAT, dialect, namespace=NS, session_id="v7-sess" if dialect == "antigravity" else None)
        v7 = {k: v for k, v in json.loads(json.dumps(p.state())).items()
              if k not in ("f3", "tool_pending", "tool_seen", "newest_time", "f3_dialect")}
        v7["v"] = 7
        frozen = json.dumps(v7, sort_keys=True)
        resumed = cp.ContextParser.from_state(v7, namespace=NS)
        assert resumed.f3 is False and resumed.state()["v"] == 8
        assert json.dumps(v7, sort_keys=True) == frozen, "the caller's v7 dict must not be mutated"


def test_a_v7_upgrade_names_kimi_for_when_its_dialect_lands():
    assert "kimi" in cp._upgrade_v7_state.__code__.co_consts or any(
        isinstance(c, tuple) and "kimi" in c for c in cp._upgrade_v7_state.__code__.co_consts)


def test_an_f3_state_refuses_to_resume_with_f3_off_and_the_reverse():
    on = feed(parser(f3=True), [call("a1", 1, "A")]).state()
    off = parser(f3=False).state()
    for state, wrong in ((on, False), (off, True)):
        try:
            cp.ContextParser.from_state(json.loads(json.dumps(state)), namespace=NS, f3=wrong)
        except ValueError:
            continue
        raise AssertionError("a state written with f3=%s resumed with f3=%s" % (state["f3"], wrong))


def test_an_f3_off_state_carrying_pairing_state_is_refused():
    s = parser(f3=False).state()
    s["tool_pending"] = [[cp._hex32("A"), "Bash", "{}", t(1), None]]
    try:
        cp.validate_state(s)
    except ValueError:
        return
    raise AssertionError("pairing state on an F3-off source must be refused")


def test_the_dialect_slot_round_trips_and_is_bounded():
    p = parser()
    p.f3_dialect = {"fifo": ["ag:1:0", "ag:1:1"]}
    resumed = cp.ContextParser.from_state(json.loads(json.dumps(p.state())), namespace=NS)
    assert resumed.f3_dialect == {"fifo": ["ag:1:0", "ag:1:1"]}
    s = p.state()
    s["f3_dialect"] = {"x": "y" * cp.F3_DIALECT_BYTES}
    try:
        cp.validate_state(s)
    except ValueError:
        return
    raise AssertionError("an oversized dialect slot must be refused")


def test_parse_file_flushes_what_still_waits_at_the_end(tmp_path):
    src = tmp_path / "t.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in [call("a1", 1, "A"), said("u", 2, "bye")]) + "\n",
                   encoding="utf-8")
    p, report = cp.parse_file(str(src), SEAT, "claude", namespace=NS, f3=True)
    assert [x["result_status"] for x in exchanges(p)] == ["missing"] and report["f3"] is True


def test_a_quoted_secret_in_a_call_or_a_result_is_screened_raw_not_only_escaped():
    # GitHub Codex on 0a7517dc (context_parser.py:888), txxxx a0a35f: json.dumps escapes the quotes inside the call
    # and the result, so a quoted secret no longer matches a detector in the whole exchange. The raw fields are
    # screened too.
    in_call = feed(parser(namespace=None), [call("a1", 1, "Q1", inp={"password": "hunter2"}), result("u1", 1, "Q1")])
    assert [(i["item"], i.get("omitted_reason")) for i in in_call.items] == [("omitted", "secret")]
    key = 'AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
    in_result = feed(parser(namespace=None), [call("a1", 1, "Q2"), result("u1", 1, "Q2", key)])
    assert [(i["item"], i.get("omitted_reason")) for i in in_result.items] == [("omitted", "secret")]
    kept = feed(parser(), [call("a1", 1, "Q3", inp={"password": "hunter2"}), result("u1", 1, "Q3")])
    assert [i["item"] for i in kept.items] == ["tool_exchange"], "unscreened namespace keeps it, by Bxxxxx's rule"


def test_a_lone_surrogate_in_structured_tool_input_or_output_is_replaced_not_a_refusal():
    # GitHub Codex on 1cf18567 (context_parser.py:572), txxxx dc1af5: serialize() of a structure holding a lone
    # surrogate raised UnicodeEncodeError (a ValueError), the uploader's parser_refused, which stalls the source at that
    # record for good. Both structured fields are replaced the way text is, and the next record still goes in.
    lone = "echo \ud800"
    p = feed(parser(), [call("a1", 1, "S1", inp={"command": lone}),
                        cl("user", "u1", 1, [{"type": "tool_result", "tool_use_id": "S1",
                                              "content": [{"type": "text", "text": lone}]}]),
                        said("u2", 2, "next record")])
    [x] = exchanges(p)
    got = whole(x)
    assert "\ud800" not in x["text"] and "�" in json.loads(got["call"])["command"]
    assert "�" in got["result"]
    assert [i["item"] for i in p.items] == ["tool_exchange", "user_text"]


def test_an_exchange_with_more_parts_than_a_batch_holds_is_omitted_too_large():
    # GitHub Codex on a7a9ed7a (context_parser.py:917), txxxx c01326: with batch_items below F3_MAX_PARTS, a split
    # exchange could need more parts than one batch holds, and the uploader stopped at that record on every pass.
    p = feed(parser(batch_items=2), [call("a1", 1, "W"), result("u1", 1, "W", "w" * 150_000), said("u2", 2, "next")])
    assert [(i["item"], i.get("omitted_reason")) for i in p.items] == [("omitted", "too_large"), ("user_text", None)]


# ---- one record's F3 output fits one batch (GitHub Codex on c2263808) ----------------------------------------------

def two_calls(uuid, minute, *ids):
    return cl("assistant", uuid, minute, [{"type": "tool_use", "id": i, "name": "Bash", "input": {"command": "ls"}}
                                          for i in ids])


def results(uuid, minute, bodies, text=None):
    blocks = [{"type": "tool_result", "tool_use_id": i, "content": b} for i, b in bodies]
    return cl("user", uuid, minute, blocks + ([{"type": "text", "text": text}] if text else []))


def per_record(p, records):
    return [len(p.feed(r)) for r in records]


def test_parallel_results_in_one_record_never_emit_more_items_than_a_batch_holds():
    # Codex's repro: batch_items=2, one user record with two 100,000-character results, 2 parts each: 4 items, and the
    # uploader stopped at that record for good. The first takes the record's two slots; the second is reported.
    # A dialogue line first, so the record's room counts from the record, not from the source.
    recs = [said("u0", 1, "start"), two_calls("a1", 1, "W1", "W2"),
            results("u1", 1, [("W1", "w" * 100_000), ("W2", "v" * 100_000)])]
    p = parser(batch_items=2)
    counts = per_record(p, recs)
    assert counts == [1, 0, 2]
    assert [(i["part"], i["parts"]) for i in exchanges(p)] == [(1, 2), (2, 2)]
    assert ("record_full", "a1") in p.skipped        # an exchange carries its call record's ref


def test_a_record_full_of_exchanges_omits_the_one_that_no_longer_fits_whole():
    # Two slots: a one-part exchange takes one; the next needs two parts, gets the one left, so is omitted; a third
    # gets none.
    recs = [two_calls("a1", 1, "S", "B", "C"), results("u1", 1, [("S", "ok"), ("B", "b" * 100_000), ("C", "ok")])]
    p = parser(batch_items=2)
    assert per_record(p, recs) == [0, 2]
    assert [(i["item"], i.get("omitted_reason")) for i in p.items] == [("tool_exchange", None), ("omitted", "too_large")]
    assert ("record_full", "a1") in p.skipped        # an exchange carries its call record's ref


def test_a_records_text_blocks_keep_their_slots():
    recs = [two_calls("a1", 1, "W1", "W2"), results("u1", 1, [("W1", "ok"), ("W2", "ok")], text="and a note")]
    p = parser(batch_items=2)
    assert per_record(p, recs) == [0, 2]
    assert [i["item"] for i in p.items] == ["tool_exchange", "user_text"]
    thinking = cl("assistant", "a2", 2, [{"type": "thinking", "thinking": "weigh"}, {"type": "text", "text": "go"}])
    q = parser(batch_items=1)
    assert per_record(q, [thinking]) == [1] and q.items[0]["item"] == "assistant_text"
    assert ("record_full", "a2") in q.skipped


def test_at_the_default_batch_every_parallel_result_is_kept():
    ids = ["R%d" % n for n in range(40)]
    p = parser()
    assert per_record(p, [two_calls("a1", 1, *ids), results("u1", 1, [(i, "ok") for i in ids])]) == [0, 40]
    assert not [s for s in p.skipped if s[0] == "record_full"]


def test_a_flushed_exchange_is_bounded_on_its_own_not_by_the_record_before_it():
    p = parser(batch_items=2)
    p.feed(call("a1", 1, "W", inp={"blob": "x" * 100_000}))
    p.feed(said("u1", 7, "later"))                   # six minutes on: the call is due
    assert [i["parts"] for i in p.flush_pending() if i["item"] == "tool_exchange"] == [2, 2]


def todo(*steps):
    return {"type": "tool_use", "id": "todo-%d" % len(steps), "name": "TodoWrite",
            "input": {"todos": [{"content": s, "status": "pending"} for s in steps]}}


def test_a_records_plan_items_keep_their_slots_too():
    # GitHub Codex on 56d830a8 (txxxx f15fc8): a thinking block and a TodoWrite adding two tasks at batch_items=2. F3 off
    # gives the two task items; F3 on gave three, and the uploader stopped at that record on every pass.
    rec = cl("assistant", "a1", 1, [{"type": "thinking", "thinking": "weigh"}, todo("one", "two")])
    off = parser(f3=False, batch_items=2)
    assert per_record(off, [rec]) == [2]
    on = parser(batch_items=2)
    assert per_record(on, [rec]) == [2]
    assert [i["item"] for i in on.items] == ["task", "task"] and ("record_full", "a1") in on.skipped


def test_the_plan_reserve_diffs_each_todowrite_against_the_one_before_it():
    # Two TodoWrites in one record: the second adds one step to the first, so 2 + 1 plan items, and the thinking
    # block gets the one slot left at batch_items=4.
    rec = cl("assistant", "a1", 1, [{"type": "thinking", "thinking": "weigh"}, todo("one", "two"),
                                    todo("one", "two", "three")])
    # Without the chaining the reserve would be 2 + 3 = 5, and the thinking item would be dropped as record_full.
    assert parser(batch_items=4)._f1_reserve("assistant", rec["message"]["content"], t(1)) == 3
    p = parser(batch_items=4)
    assert per_record(p, [rec]) == [4]
    assert [i["item"] for i in p.items] == ["thinking", "task", "task", "task"]


def _codex_plan_pressure(f3):
    # GitHub Codex on 7d96b08f (txxxx 1d1746): a pending call near F3_PENDING_BYTES, then an update_plan whose own call
    # pushes the pending calls over it, so the pressure flush emits the old call before _plan() emits the tasks.
    p = parser(dialect="codex", f3=f3, batch_items=2)
    p.feed({"type": "session_meta", "timestamp": t(0), "payload": {"id": "cx-sess", "source": "cli"}})
    p.feed(cx("function_call", 1, call_id="big", name="shell", arguments="x" * (cp.F3_PENDING_BYTES - 200)))
    plan = json.dumps({"explanation": "e" * 400, "plan": [{"step": "one", "status": "pending"},
                                                          {"step": "two", "status": "pending"}]})
    return p, len(p.feed(cx("function_call", 2, call_id="plan", name="update_plan", arguments=plan)))


def test_a_codex_plan_that_triggers_a_pressure_flush_keeps_its_task_slots():
    off, n_off = _codex_plan_pressure(False)
    assert n_off == 2 and [i["item"] for i in off.items] == ["task", "task"]
    on, n_on = _codex_plan_pressure(True)
    assert n_on == 2, [i["item"] for i in on.items]
    assert [i["item"] for i in on.items] == ["task", "task"] and ("record_full", "big") in on.skipped


# ---- GitHub Codex on 899aca19 and ddc306c1 (txxxx 4096ba) ---------------------------------------------------------

def test_a_secret_in_a_structured_result_is_screened_as_written_not_as_escaped_json():
    secret_text = json.dumps({"password": "hunter2"})
    blocks = [{"type": "text", "text": secret_text}]
    p = feed(parser(namespace=None), [call("a1", 1, "S"), result("u1", 1, "S", blocks)])
    assert [(i["item"], i.get("omitted_reason")) for i in p.items] == [("omitted", "secret")], p.items
    plain = feed(parser(namespace=None), [call("a1", 1, "S"), result("u1", 1, "S", secret_text)])
    assert [i.get("omitted_reason") for i in plain.items] == ["secret"], "control: the same text given directly"
    inp = feed(parser(namespace=None), [call("a1", 1, "I", inp={"env": {"note": secret_text}}), result("u1", 1, "I", "ok")])
    assert [i.get("omitted_reason") for i in inp.items] == ["secret"], "a structured input holding it too"
    clean = feed(parser(namespace=None), [call("a1", 1, "C"), result("u1", 1, "C", [{"type": "text", "text": "fine"}])])
    assert [i["item"] for i in clean.items] == ["tool_exchange"], "control: a clean structured result is kept"


def test_parse_file_keeps_a_split_exchange_in_one_batch(tmp_path):
    src = tmp_path / "t.jsonl"
    recs = [said("u0", 1, "start"), call("a1", 1, "W"), result("u1", 1, "W", "w" * 100_000)]
    src.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    p, _report = cp.parse_file(str(src), SEAT, "claude", batch_items=2, namespace=NS, f3=True)
    parts = [i for i in p.items if i["item"] == "tool_exchange"]
    assert len(parts) == 2 and len({i["batch"] for i in parts}) == 1, [(i["part"], i["batch"]) for i in parts]
    assert [i["item"] for i in p.items] == ["user_text", "tool_exchange", "tool_exchange"]


def test_parse_file_keeps_a_split_exchange_flushed_at_the_end_in_one_batch(tmp_path):
    src = tmp_path / "t.jsonl"
    recs = [said("u0", 1, "start"), call("a1", 1, "W", inp={"blob": "x" * 100_000})]
    src.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    p, _report = cp.parse_file(str(src), SEAT, "claude", batch_items=2, namespace=NS, f3=True)
    parts = [i for i in p.items if i["item"] == "tool_exchange"]
    assert len(parts) == 2 and len({i["batch"] for i in parts}) == 1, [(i["part"], i["batch"]) for i in parts]


# ---- Kimi dialect F3 adapter: contract tests, deliberately red until the adapter lands ------------------------------

KIMI0 = 1760680800123
KIMI_TIME = "2025-10-17T06:00:00.123Z"


def ki_event(kind, ms=KIMI0, **fields):
    event = {"type": kind}
    event.update(fields)
    return {"type": "context.append_loop_event", "event": event, "time": ms}


def ki_call(tool_id="tool_1", args=None, uuid="ki-call", ms=KIMI0, name="Read"):
    return ki_event("tool.call", ms=ms, uuid=uuid, toolCallId=tool_id, name=name,
                    args={"command": "ls"} if args is None else args)


def ki_result(tool_id="tool_1", output="ok", uuid="ki-result", parent="ki-parent", ms=KIMI0 + 1000,
              is_error=False):
    return ki_event("tool.result", ms=ms, uuid=uuid, parentUuid=parent, toolCallId=tool_id,
                    result={"output": output, "isError": is_error})


def ki_parser(f3=True, namespace=NS):
    return cp.ContextParser(SEAT, "kimi", namespace=namespace, f3=f3, session_id="kimi-f3-sess")


def test_tool_call_names_are_bounded_for_every_dialect():
    """The shared _tool_call boundary keeps every dialect's checkpoint and exchange server-valid."""
    for dialect in ("claude", "codex", "antigravity", "kimi"):
        for bad in (123, "", "x" * 300):
            p = cp.ContextParser(SEAT, dialect, namespace=NS, f3=True, session_id="name-sess")
            p.src = cp._hex32("name-sess")  # Claude/Codex normally learn this from session_meta
            p._tool_call("bad-name", bad, "{}", KIMI_TIME, "native")
            cp.validate_state(p.state())
            assert p.tool_pending[0][1] is None, (dialect, bad)
            p.flush_pending(everything=True)
            [x] = exchanges(p)
            assert whole(x)["tool"] is None, (dialect, bad)


def test_kimi_bad_tool_names_resume_mid_call_and_emit_no_tool_name():
    """The actual Kimi wire path, not just the shared helper, guards a pending call before checkpointing."""
    for bad in (123, "", "x" * 300):
        p = feed(ki_parser(), [ki_call(name=bad)])
        cp.validate_state(p.state())
        revived = cp.ContextParser.from_state(json.loads(json.dumps(p.state())), namespace=NS)
        feed(revived, [ki_result()])
        [x] = exchanges(revived)
        assert whole(x)["tool"] is None, bad


def test_kimi_f3_pairs_by_id_with_compact_json_and_error_status():
    p = feed(ki_parser(), [ki_call(args={"z": 1, "a": {"b": True}}),
                           ki_result(output={"ok": False, "items": [1, "x"]}, is_error=True)])
    [x] = exchanges(p)
    assert whole(x) == {"tool": "Read", "call": '{"z":1,"a":{"b":true}}',
                        "result": '{"ok":false,"items":[1,"x"]}'}
    assert (x["call_status"], x["result_status"]) == ("ok", "error")
    assert x["tool_ref"] == cp._hex32("tool_1") and x["time"] == KIMI_TIME
    assert x["native_ref"] == cp._hex32("ki-call") and p.tool_pending == []


def test_kimi_f3_late_result_uses_parent_uuid_or_tool_id():
    for parent, expected in (("ki-parent", "ki-parent"), (None, "tool_1")):
        p = feed(ki_parser(), [ki_result(output="boom", parent=parent, is_error=True)])
        [x] = exchanges(p)
        assert (x["call_status"], x["result_status"]) == ("missing", "error")
        assert x["native_ref"] == cp._hex32(expected) and whole(x)["result"] == "boom"


def test_kimi_f3_thinking_uses_think_not_text_or_reasoning_key():
    thought = ki_event("content.part", uuid="ki-think", reasoningKey="reasoning_content",
                       part={"type": "think", "think": "visible plan", "text": "wrong field"})
    p = feed(ki_parser(), [thought])
    assert [(i["item"], i["text"]) for i in p.items] == [("thinking", "visible plan")]
    assert p.items[0]["native_ref"] == cp._hex32("ki-think")


def test_kimi_f3_compaction_is_excluded_as_compact_summary():
    p = feed(ki_parser(), [{"type": "context.apply_compaction", "summary": "synthetic", "time": KIMI0}])
    assert p.items == [] and p.skipped == [("compact_summary", None)]


def test_kimi_bad_times_skip_with_f3_off_and_on():
    records = [ki_event("step.begin", ms=1e20),
               {"type": "context.apply_compaction", "summary": "synthetic", "time": 1e20},
               {"type": "context.undo", "count": 1, "time": float("nan")}]
    for f3 in (False, True):
        p = feed(ki_parser(f3=f3), records)
        assert p.items == []
        assert p.skipped == [("kimi_loop_event", None),
                             ("compact_summary" if f3 else "kimi_compaction", None),
                             ("kimi_undo", None)]


def test_kimi_f3_large_exchange_splits_losslessly():
    body = "line é\U0001f9ed \"quoted\"\n" * 9000
    p = feed(ki_parser(), [ki_call(args={"command": "ls"}), ki_result(output=body)])
    parts = exchanges(p)
    assert len(parts) > 1 and [x["part"] for x in parts] == list(range(1, len(parts) + 1))
    rebuilt = "".join(x["text"] for x in parts)
    expected = json.dumps({"tool": "Read", "call": '{"command":"ls"}', "result": body},
                          ensure_ascii=False, separators=(",", ":"))
    assert cp._sha(rebuilt) == cp._sha(expected)
    assert all(x["exchange_sha256"] == cp._sha(rebuilt) for x in parts)
    assert all(len(cp.serialize(x)) <= cp.MAX_VALUE_BYTES for x in parts)


def test_kimi_f3_secret_across_a_part_boundary_is_screened_before_splitting():
    probe = feed(ki_parser(namespace=None), [ki_call(), ki_result(output="y" * 150_000)])
    cut = len(exchanges(probe)[0]["text"])
    prefix = json.dumps({"tool": "Read", "call": '{"command":"ls"}', "result": ""}, separators=(",", ":"))[:-2]
    offset = cut - len(prefix) - 10
    body = "y" * offset + SECRET + "y" * (150_000 - offset - len(SECRET))
    whole_text = json.dumps({"tool": "Read", "call": '{"command":"ls"}', "result": body},
                            ensure_ascii=False, separators=(",", ":"))
    assert SECRET not in whole_text[:cut] and SECRET not in whole_text[cut:]
    p = feed(ki_parser(namespace=None), [ki_call(), ki_result(output=body)])
    assert exchanges(p) == []
    assert [(i["item"], i["omitted_reason"]) for i in p.items] == [("omitted", "secret")]


def test_kimi_f3_quoted_secret_in_structured_call_or_result_is_screened():
    in_call = feed(ki_parser(namespace=None), [ki_call(args={"password": "hunter2"}), ki_result()])
    assert [(i["item"], i.get("omitted_reason")) for i in in_call.items] == [("omitted", "secret")]
    key = 'AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
    in_result = feed(ki_parser(namespace=None), [ki_call(), ki_result(output={"key": key})])
    assert [(i["item"], i.get("omitted_reason")) for i in in_result.items] == [("omitted", "secret")]
    kept = feed(ki_parser(), [ki_call(args={"password": "hunter2"}), ki_result()])
    assert [i["item"] for i in kept.items] == ["tool_exchange"]


def test_kimi_f3_resume_at_every_record_with_a_pending_call_is_byte_identical():
    thought = ki_event("content.part", uuid="ki-think", part={"type": "think", "think": "plan"})
    records = [ki_call("A", uuid="ki-a"), ki_call("B", uuid="ki-b"),
               ki_result("B", output="bee", uuid="ki-rb"), thought]
    straight = feed(ki_parser(), records)
    straight.finish()
    expected = [cp.serialize(i) for i in straight.items]
    assert {i["item"] for i in straight.items} >= {"tool_exchange", "thinking"}, "control: adapter output exists"
    for cut in range(len(records) + 1):
        first = feed(ki_parser(), records[:cut])
        revived = cp.ContextParser.from_state(json.loads(json.dumps(first.state())), namespace=NS)
        feed(revived, records[cut:])
        revived.finish()
        got = [cp.serialize(i) for i in first.items] + [cp.serialize(i) for i in revived.items]
        assert got == expected, cut
        assert first.skipped + revived.skipped == straight.skipped, cut


def test_kimi_f3_pending_call_uses_the_data_clock():
    later_text = ki_event("content.part", ms=KIMI0 + 300_000, uuid="ki-later",
                          part={"type": "text", "text": "five minutes later"})
    p = feed(ki_parser(), [ki_call("A", uuid="ki-a"), ki_call("B", uuid="ki-b", ms=KIMI0 + 240_000), later_text])
    [flushed] = p.flush_pending()
    assert flushed["item"] == "tool_exchange" and flushed["result_status"] == "missing"
    assert flushed["tool_ref"] == cp._hex32("A"), "only the five-minute-old call flushes"
    assert [i["item"] for i in p.items] == ["assistant_text", "tool_exchange"]
    assert [e[0] for e in p.tool_pending] == [cp._hex32("B")], "the four-minute call stays pending"


def test_kimi_f3_items_require_a_session_identity():
    thought = ki_event("content.part", uuid="ki-think", part={"type": "think", "think": "plan"})
    for record in (ki_call(), ki_result(), thought):
        p = cp.ContextParser(SEAT, "kimi", namespace=NS, f3=True)
        feed(p, [record])
        assert p.quarantined is True and p.items == []
        assert ("no_session_id", None) in p.skipped
    p = cp.ContextParser(SEAT, "kimi", namespace=NS, f3=True)
    feed(p, [ki_call(), ki_result(), thought])
    assert p.quarantined is True and p.items == []
    assert ("no_session_id", None) in p.skipped


def test_kimi_f3_subagent_keeps_one_artifact_without_f3_items():
    thought = ki_event("content.part", uuid="ki-think", part={"type": "think", "think": "private plan"})
    draft = ki_event("content.part", uuid="ki-draft", part={"type": "text", "text": "draft"})
    final = ki_event("content.part", uuid="ki-final", part={"type": "text", "text": "final report"})
    p = cp.ContextParser(SEAT, "kimi", namespace=NS, f3=True, session_id="kimi-subagent", subagent=True)
    feed(p, [ki_call(), ki_result(), thought, draft, final])
    (artifact,) = p.finish()
    assert artifact["item"] == "artifact" and artifact["text"] == "final report"
    assert not any(i["item"] in ("tool_exchange", "thinking") for i in p.items)
    assert p.tool_pending == [] and p.f3_dialect == {}


# ---- Antigravity F3 adapter + real #433 validator preflight --------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.append(REPO_ROOT)

from datetime import datetime, timezone  # noqa: E402
from apps.context import validation as ctx_val  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from frozen_outbox import FrozenOutbox, Record  # noqa: E402
from incremental_store import Store  # noqa: E402
from screened_outbox import freeze_screened  # noqa: E402

AG_NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def ag_parser(f3=True, namespace=NS, session_id="ag-synth-sess", **kw):
    return cp.ContextParser(SEAT, "antigravity", namespace=namespace, session_id=session_id, f3=f3, **kw)


def ag_user(step, text, minute=1, **extra):
    body = text if text.lstrip().startswith("<") else "<USER_REQUEST>\n%s\n</USER_REQUEST>" % text
    return dict({"step_index": step, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": t(minute), "content": body}, **extra)


def ag_plan(step, minute=1, content=None, thinking=None, calls=None, **extra):
    rec = {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t(minute)}
    rec.update({k: v for k, v in (("content", content), ("thinking", thinking), ("tool_calls", calls)) if v is not None}, **extra)
    return rec


def ag_res(step, content="ok", minute=1, status="DONE", rtype="GENERIC", **extra):
    rec = {"step_index": step, "source": "MODEL", "type": rtype, "status": status, "created_at": t(minute)}
    if content is not None:
        rec["content"] = content
    return dict(rec, **extra)


def ag_sys(step, content="system note", minute=1, source="SYSTEM", rtype="SYSTEM_MESSAGE"):
    return {"step_index": step, "source": source, "type": rtype, "status": "DONE", "created_at": t(minute), "content": content}


def _assert_valid_batches(items, tmp_path, namespace=NS):
    manifests = cp.manifests(items)
    if not manifests:
        return
    store = Store(str(tmp_path / "outbox_store"))
    outbox = FrozenOutbox(store)
    src_id, gen_id = "a" * 64, "b" * 64
    snap = store.enroll(src_id, gen_id, "context-parser-1.0", {"seat": SEAT}, {"v": cp.STATE_VERSION})
    for m in manifests:
        batch_rows = [i for i in items if i["batch"] == m["batch"]]
        for i in batch_rows:
            assert ctx_val.validate_context(cp.UNSCREENED_NAMESPACE, cp.item_key(i), i, now=AG_NOW) == "item"
            if cp.screens_apply(namespace):
                ctx_val.screens.screen(*(ctx_val._exchange_screen_text(i) if i["item"] == "tool_exchange"
                                         else [i[f] for f in ctx_val.SCREENED_FIELDS if isinstance(i.get(f), str)]))
        assert ctx_val.validate_context(cp.UNSCREENED_NAMESPACE, cp.manifest_key(m), m, now=AG_NOW) == "manifest"
        ctx_val._check_split_exchanges(namespace or "shared/scratch-ctx", [(i["ordinal"], i) for i in batch_rows])
        recs = tuple(Record(cp.item_key(i), cp.serialize(i)) for i in batch_rows)
        freeze_screened(outbox, snap, m["batch"] * 10, recs, Record(cp.manifest_key(m), cp.serialize(m)),
                        {"batch": m["batch"]}, seat=SEAT, now=AG_NOW)
        snap = store.resume(src_id, gen_id, "context-parser-1.0")


def test_antigravity_f3_off_is_byte_identical_to_f1():
    recs = [ag_sys(0, "hook ready", source="SYSTEM_SDK"), ag_user(1, "inspect widget"),
            ag_plan(2, 1, content="checking", thinking="weigh paths", calls=[{"name": "read_file", "args": {"Path": "/repo/a.py"}}]),
            ag_res(3, "def a(): pass", 1), ag_sys(4, "checkpoint", 2, rtype="CHECKPOINT"),
            ag_user(5, "<CONTEXT_SUMMARY>\nsummary\n</CONTEXT_SUMMARY>", 2), ag_plan(6, 3, content="all set", thinking="done")]
    p = feed(ag_parser(f3=False), recs)
    assert [(i["item"], i["text"]) for i in p.items] == [("user_text", "inspect widget"), ("assistant_text", "checking"), ("assistant_text", "all set")]
    assert set(cp.manifests(p.items)[0]["counts"]) == {"written", "omitted_secret", "omitted_client_name", "omitted_too_large"}
    s = p.state()
    assert (s["f3"], s["tool_pending"], s["tool_seen"], s["newest_time"], s["f3_dialect"], p.skipped) == (False, [], [], "", {}, [])


def test_antigravity_f3_pairs_multi_calls_statuses_and_passes_real_validator(tmp_path):
    recs = [ag_sys(0, "boot", source="SYSTEM_SDK"), ag_user(1, "run checks", 1),
            ag_plan(2, 1, content="running two tools", thinking="plan two reads",
                    calls=[{"name": "read_file", "args": {"Path": "/x"}}, {"name": "run_command", "args": {"CommandLine": "echo hi"}}]),
            ag_res(3, "contents-x", 1, status="DONE"), ag_res(4, "backgrounded task-1", 1, status="RUNNING"), ag_sys(5, "[Message] task-1 done", 2),
            ag_plan(6, 2, calls=[{"name": "edit_file", "args": {"Path": "/x"}}, {"name": "   "}, {"name": "z" * 300}, {"name": 99}]),
            ag_res(7, "boom", 2, status="ERROR"), ag_res(8, None, 2, status="INVALID", error="bad args"),
            ag_res(9, "legacy view", 2, rtype="VIEW_FILE"), ag_res(10, "nonzero rc", 2, rtype="RUN_COMMAND", exit_code=2),
            ag_plan(11, 3, calls=[{"name": "run_command", "args": {"cmd": "true"}}, {"name": "run_command", "args": {"cmd": "bool-rc"}}]),
            ag_res(12, "zero rc", 3, rtype="RUN_COMMAND", exit_code=0), ag_res(13, "bool rc is not int", 3, rtype="RUN_COMMAND", exit_code=True),
            ag_sys(14, "checkpoint", 3, rtype="CHECKPOINT"), ag_sys(15, "history", 3, rtype="CONVERSATION_HISTORY"),
            ag_sys(16, "transport err", 3, source="MODEL", rtype="ERROR_MESSAGE"), ag_user(17, "<CONTEXT_SUMMARY>\ncompacted\n</CONTEXT_SUMMARY>", 4)]
    p = feed(ag_parser(), recs)
    assert [i["item"] for i in p.items] == ["user_text", "thinking", "assistant_text"] + ["tool_exchange"] * 8
    xs = exchanges(p)
    assert whole(xs[0]) == {"tool": "read_file", "call": '{"Path":"/x"}', "result": "contents-x"}
    assert whole(xs[1]) == {"tool": "run_command", "call": '{"CommandLine":"echo hi"}', "result": "backgrounded task-1"}
    assert [x["result_status"] for x in xs] == ["ok", "ok", "error", "error", "ok", "error", "ok", "ok"]
    assert [whole(x)["tool"] for x in xs[2:6]] == ["edit_file", None, None, None]
    assert whole(xs[3])["result"] == "bad args" and whole(xs[3])["call"] == "{}" and whole(xs[4])["call"] == "{}"
    assert [r for r, _ref in p.skipped] == ["is_meta", "is_meta", "compact_summary", "compact_summary", "is_meta", "compact_summary"]
    _assert_valid_batches(p.items, tmp_path)


def test_antigravity_explicit_tool_ids_pair_out_of_order_and_catch_duplicates(tmp_path):
    recs = [ag_plan(1, 8, calls=[{"id": " explicit-A ", "name": "tA", "args": {"a": 1}},
                                 {"id": "explicit-A", "name": "tA2", "args": {"a": 2}},
                                 {"id": "explicit-B", "name": "tB", "args": {"b": 1}}]),
            ag_res(0, "orphan-at-0", 2), ag_res(2, "anon-at-2", 8), ag_res(5, "res-B", 8, tool_call_id=" explicit-B "),
            ag_res(5, "res-A", 8, tool_call_id="explicit-A"), ag_res(5, "dup-B", 8, tool_call_id="explicit-B"),
            ag_res(8, "dup-B2", 8, tool_call_id="explicit-B")]
    p = ag_parser()
    p.feed(recs[0])
    assert [x[1] for x in p.f3_dialect["steps"]] == ["explicit-A", "explicit-B"]
    feed(p, recs[1:])
    p.flush_pending(everything=True)
    _assert_valid_batches(p.items, tmp_path)
    assert [(whole(x)["tool"], whole(x)["result"]) for x in exchanges(p)] == [
        (None, "orphan-at-0"), ("tB", "res-B"), ("tA", "res-A"), (None, "anon-at-2")]
    assert any(r == "tool_duplicate_call" for r, _ in p.skipped) and any(r == "tool_duplicate_result" for r, _ in p.skipped)
    assert ("replay", p.src + ":5") in p.skipped and p.f3_dialect.get("steps") is None and p.f3_dialect.get("early") is None
    # Codex 4420285110: early result with explicit tool_call_id buffers until its planner arrives.
    eo = feed(ag_parser(), [ag_res(2, "early-exp-A", 2, tool_call_id=" call-A "), ag_res(4, "early-dup-A", 2, tool_call_id="call-A"),
                            ag_res(3, "chopped-T", 2, tool_call_id="call-T", truncated_fields=["content"]),
                            ag_plan(1, 1, calls=[{"id": "call-A", "name": "tEarly"}, {"id": "call-A", "name": "tDup"},
                                                 {"id": "call-T", "name": "tTrunc"}]),
                            ag_res(6, "orphan-exp", 3, tool_call_id="call-orphan")])
    eo.flush_pending(everything=True)
    _assert_valid_batches(eo.items, tmp_path / "eo")
    assert [(whole(x)["tool"], x["call_status"], x["result_status"], whole(x)["result"]) for x in exchanges(eo)] == [
        ("tEarly", "ok", "ok", "early-exp-A"), ("tTrunc", "ok", "missing", None), (None, "missing", "ok", "orphan-exp")]
    assert ("tool_duplicate_call", eo.src + ":1") in eo.skipped and ("tool_duplicate_result", eo.src + ":4") in eo.skipped
    assert exchanges(eo)[2]["tool_ref"] == cp._hex32("call-orphan")


def test_antigravity_turn_boundaries_flush_cancelled_calls_without_shifting_later_calls(tmp_path):
    recs = [ag_res(0, "orphan-0", 1), ag_plan(1, 1, calls=[{"name": "c_user", "args": {"n": 1}}]), ag_user(2, "cancel and switch", 2),
            ag_plan(3, 2, calls=[{"name": "c_sum", "args": {"n": 2}}]), ag_user(4, "<CONTEXT_SUMMARY>\ncut\n</CONTEXT_SUMMARY>", 2),
            ag_plan(5, 3, calls=[{"name": "c_drop", "args": {"n": 3}}]), ag_plan(6, 3, calls=[{"name": "c_good", "args": {"n": 4}}]),
            ag_user(2, "cancel and switch", 2), ag_user(4, "<CONTEXT_SUMMARY>\ncut\n</CONTEXT_SUMMARY>", 2), ag_res(7, "good-result", 3)]
    p = feed(ag_parser(), recs)
    _assert_valid_batches(p.items, tmp_path)
    assert [(whole(x)["tool"], x["call_status"], x["result_status"], whole(x)["result"]) for x in exchanges(p)] == [
        (None, "missing", "ok", "orphan-0"), ("c_user", "ok", "missing", None), ("c_sum", "ok", "missing", None),
        ("c_drop", "ok", "missing", None), ("c_good", "ok", "ok", "good-result")]
    assert ("replay", p.src + ":2") in p.skipped and ("replay", p.src + ":4") in p.skipped


def test_antigravity_out_of_order_generic_and_result_replay_and_rewind(tmp_path, monkeypatch):
    s37 = dict(ag_res(37, "early-logged", 1), created_at="2026-10-27T08:01:41Z")
    s36 = dict(ag_plan(36, 1, content="kept prose", thinking="kept thought", calls=[{"name": "read_file", "args": {"p": 1}}]), created_at="2026-10-27T08:01:39Z")
    replay_res, orphan38 = dict(ag_res(37, "stale-replay", 1), created_at="2026-10-27T08:01:41Z"), dict(ag_res(38, "orphan-before-rewind", 1), created_at="2026-10-27T08:01:42Z")
    rew_p37, rew_r38 = dict(ag_plan(37, 2, calls=[{"name": "live_tool", "args": {"p": 2}}]), created_at="2026-10-27T08:03:00Z"), dict(ag_res(38, "rewound-live-result", 3), created_at="2026-10-27T08:03:02Z")
    p = feed(ag_parser(), [s37, s36, replay_res, orphan38, rew_p37, dict(rew_p37), rew_r38])
    _assert_valid_batches(p.items, tmp_path)
    assert {"assistant_text", "thinking"} <= {i["item"] for i in p.items} and [r for r, _ in p.skipped].count("replay") == 2
    assert [(whole(x)["tool"], x["call_status"], whole(x)["result"]) for x in exchanges(p)] == [
        ("read_file", "ok", "early-logged"), (None, "missing", "orphan-before-rewind"), ("live_tool", "ok", "rewound-live-result")]
    dr = feed(ag_parser(), [ag_plan(1, 1, calls=[{"name": "old1"}]), ag_plan(1, 8, calls=[{"name": "new1"}]), ag_res(2, "old-r", 2), ag_res(2, "live-r", 9),
                            dict(ag_plan(1, 9, calls=[{"name": "subms"}]), created_at="2026-10-27T08:09:30.123900Z"),
                            dict(ag_res(2, "stale-subms", 9), created_at="2026-10-27T08:09:30.123500Z"),
                            dict(ag_res(2, "live-subms", 9), created_at="2026-10-27T08:09:30.124000Z")])
    assert [(whole(x)["tool"], x["result_status"], whole(x)["result"]) for x in exchanges(dr)] == [("old1", "missing", None), ("new1", "ok", "live-r"), ("subms", "ok", "live-subms")]
    assert [r for r, _ in dr.skipped].count("replay") == 2
    par = feed(ag_parser(), [dict(ag_plan(1, 1, calls=[{"name": "c1"}, {"name": "c2"}]), created_at="2026-10-27T08:01:00Z"),
                             dict(ag_res(3, "r2", 1), created_at="2026-10-27T08:01:02Z"), dict(ag_res(2, "r1", 1), created_at="2026-10-27T08:01:01Z"),
                             dict(ag_res(2, "r1-dup", 1), created_at="2026-10-27T08:01:01Z")])
    assert [(whole(x)["tool"], whole(x)["result"]) for x in exchanges(par)] == [("c2", "r2"), ("c1", "r1")]
    assert par.skipped == [("replay", par.src + ":2")] and par.flush_pending(everything=True) == []
    epar = feed(ag_parser(), [dict(ag_res(3, "er2", 1), created_at="2026-10-27T08:01:02Z"), dict(ag_res(2, "er1", 1), created_at="2026-10-27T08:01:01Z"),
                              dict(ag_plan(1, 1, calls=[{"name": "ec1"}, {"name": "ec2"}]), created_at="2026-10-27T08:01:00Z")])
    assert [(whole(x)["tool"], whole(x)["result"]) for x in exchanges(epar)] == [("ec1", "er1"), ("ec2", "er2")]
    monkeypatch.setattr(cp, "_AG_RES_SEEN", 1)
    mev = feed(ag_parser(), [ag_res(6, "hz", 2), ag_res(4, "m4", 2), ag_res(5, "m5", 2)])
    mev.flush_pending(everything=True)
    assert [whole(x)["result"] for x in exchanges(mev)] == ["hz", "m4", "m5"]
    ov = feed(ag_parser(), [ag_plan(1, 1, calls=[{"name": "o0"}, {"name": "o1"}, {"name": "o2"}]), ag_res(4, "v2", 2), ag_res(3, "v1", 2),
                            ag_res(2, "v0", 2), ag_res(2, "v0-dup", 2), ag_plan(1, 8, calls=[{"name": "rew0"}]), ag_res(2, "stale", 2), ag_res(2, "live", 9)])
    assert [(whole(x)["tool"], whole(x)["result"]) for x in exchanges(ov)] == [("o2", "v2"), ("o1", "v1"), ("o0", "v0"), ("rew0", "live")]
    assert [r for r, _ in ov.skipped].count("replay") == 2


def test_antigravity_f3_pairs_by_step_when_results_precede_planner_b3d5de51_shape(tmp_path, monkeypatch):
    p1 = ag_plan(11088, 1, calls=[{"name": "list_dir", "args": {"DirectoryPath": "/a"}}, {"name": "grep_search", "args": {"Query": "q1"}}])
    r1, r2 = dict(ag_res(11089, "dir-out", 2), type="LIST_DIRECTORY"), dict(ag_res(11090, "grep-1", 3), type="GREP_SEARCH")
    p2 = ag_plan(11091, 4, calls=[{"name": "grep_search", "args": {"Query": "q2"}}, {"name": "grep_search", "args": {"Query": "q3"}}])
    r3, r4 = dict(ag_res(11092, "grep-early-2", 5), type="GREP_SEARCH"), dict(ag_res(11093, "grep-err-3", 6, status="ERROR"), type="GREP_SEARCH")
    out_of_order, in_order = feed(ag_parser(), [r1, p1, r2, r3, p2, r4]), feed(ag_parser(), [p1, r1, r2, p2, r3, r4])
    _assert_valid_batches(out_of_order.items, tmp_path / "ooo")
    key_fn = lambda xs: [(whole(x), x["call_status"], x["result_status"], x["time"], x.get("native_ref")) for x in xs]
    assert key_fn(exchanges(out_of_order)) == key_fn(exchanges(in_order))
    assert [(whole(x)["tool"], whole(x)["result"], x["result_status"]) for x in exchanges(out_of_order)] == [
        ("list_dir", "dir-out", "ok"), ("grep_search", "grep-1", "ok"), ("grep_search", "grep-early-2", "ok"), ("grep_search", "grep-err-3", "error")]
    tp = feed(ag_parser(), [ag_res(21, "chopped", 2, truncated_fields=["content"]), ag_plan(20, 1, calls=[{"name": "trunc_early"}, {"name": "ok_sibling"}]), ag_res(22, "sib-ok", 2)])
    assert [(whole(x)["tool"], x["call_status"], x["result_status"]) for x in exchanges(tp)] == [("trunc_early", "ok", "missing"), ("ok_sibling", "ok", "ok")]
    sm = feed(ag_parser(), [dict(ag_plan(1, 1, calls=[{"name": "old_e"}]), created_at="2026-10-27T08:01:00.000100000Z"),
                            dict(ag_res(2, "r_old1", 1), created_at="2026-10-27T08:01:00.000200000Z"),
                            dict(ag_res(2, "r_old2", 1), created_at="2026-10-27T08:01:00.000300000Z"),
                            dict(ag_plan(1, 1, calls=[{"name": "new_e"}]), created_at="2026-10-27T08:01:00.000800000Z"),
                            dict(ag_res(2, "r_live", 1), created_at="2026-10-27T08:01:00.000900000Z")])
    assert [(whole(x)["tool"], x["call_status"], whole(x)["result"]) for x in exchanges(sm)] == [("old_e", "ok", "r_old1"), (None, "missing", "r_old2"), ("new_e", "ok", "r_live")]
    ap = feed(ag_parser(), [ag_plan(1, 0, calls=[{"name": "aged_call", "args": {}}]), ag_sys(2, "tick", 6),
                            ag_res(10, "aged_early_1", 6), ag_res(11, "aged_early_2", 6), ag_res(12, "young_early", 10), ag_sys(13, "tick2", 12)])
    due = ap.due_pending()
    assert len(due) == 3 and all(len(ap.flush_pending(only=ref)) == 1 for ref in due) and ap.f3_dialect.get("steps") is None
    assert ap.due_pending(now="2026-10-28T09:00:00.000Z") == [ap.f3_dialect["early"][0][0]]
    ap.flush_pending(now="2026-10-28T09:00:00.000Z")
    assert ap.f3_dialect.get("early") is None
    assert [(whole(x)["tool"], x["call_status"], x["result_status"], whole(x)["result"]) for x in exchanges(ap)] == [
        ("aged_call", "ok", "missing", None), (None, "missing", "ok", "aged_early_1"), (None, "missing", "ok", "aged_early_2"), (None, "missing", "ok", "young_early")]
    monkeypatch.setattr(cp, "F3_DIALECT_BYTES", 180)
    ec = feed(ag_parser(), [ag_res(n, "e%d" % n, 1) for n in range(2, 7)])
    assert len(cp.serialize(ec.state()["f3_dialect"])) <= 180 and exchanges(ec) and all(x["call_status"] == "missing" for x in exchanges(ec)) and whole(exchanges(ec)[0])["result"] == "e2"


def test_antigravity_subagents_and_missing_session_id_quarantine():
    sub_recs = [ag_sys(0, "[Message] sender=parent-1 task", 0),
                ag_plan(1, 1, thinking="sub thought", calls=[{"name": "read_file", "args": {}}]),
                ag_res(2, "sub tool out", 1), ag_plan(3, 2, content="final subagent report")]
    sub = feed(ag_parser(f3=True), sub_recs)
    sub.finish()
    assert [(i["item"], i["text"]) for i in sub.items] == [("artifact", "final subagent report")]
    assert sub.skipped == [] and sub.f3_dialect == {}
    for first in (ag_plan(1, 1, thinking="orphan thought"), ag_res(1, "orphan res", 1)):
        q = cp.ContextParser(SEAT, "antigravity", namespace=NS, f3=True)
        q.feed(first)
        q.feed(ag_user(2, "ignored after quarantine", 2, conversation_id="late-id"))
        assert q.quarantined is True and q.items == [] and q.skipped == [("no_session_id", None)]


def test_antigravity_missing_step_index_uses_persisted_anon_counter(tmp_path):
    p = ag_parser()
    for rec in (ag_plan(None, 1, calls=[{"name": "t1"}]), ag_res(None, "o1", 1), ag_res(None, "orphan-a", 1),
                ag_res(None, "orphan-b", 1), ag_plan(None, 2, calls=[{"name": "old"}]),
                ag_plan(None, 8, calls=[{"name": "new"}]), ag_res(None, "stale", 3), ag_res(None, "live", 9)):
        items, skipped = p.items, p.skipped
        p = cp.ContextParser.from_state(json.loads(json.dumps(p.state())), namespace=NS)
        p.items, p.skipped = items, skipped
        p.feed(rec)
    _assert_valid_batches(p.items, tmp_path)
    assert [whole(x)["result"] for x in exchanges(p)] == ["o1", "orphan-a", "orphan-b", None, "live"]
    assert ("replay", None) in p.skipped


def test_antigravity_structured_secrets_client_names_and_surrogates(tmp_path, monkeypatch):
    (tmp_path / "deny.json").write_text('["acmecorpsecret"]', encoding="utf-8")
    monkeypatch.setenv("MESHY_CONTEXT_DENYLIST", str(tmp_path / "deny.json"))
    nested = json.dumps({"password": "hunter2-nested"})
    recs = [ag_plan(1, 1, calls=[{"name": "call_sec", "args": {"password": "hunter2"}},
                                 {"name": "nest_call", "args": {"env": {"blob": nested}}},
                                 {"name": "res_sec", "args": {"ok": 1}}, {"name": "nest_res", "args": {"ok": 2}},
                                 {"name": "client_hit", "args": {"org": "AcmeCorpSecret"}}]),
            ag_res(2, "ok", 1), ag_res(3, "ok", 1), ag_res(4, SECRET, 1), ag_res(5, {"outer": [nested]}, 1), ag_res(6, "clean", 1)]
    screened = feed(ag_parser(namespace=None, denylist=("acmecorpsecret",)), recs)
    assert [(i["item"], i.get("omitted_reason")) for i in screened.items] == [("omitted", "secret")] * 4 + [("omitted", "client_name")]
    _assert_valid_batches(screened.items, tmp_path / "sc", namespace="shared/scratch-ctx")
    unscreened = feed(ag_parser(namespace=NS, denylist=("acmecorpsecret",)), recs)
    assert [i["item"] for i in unscreened.items] == ["tool_exchange"] * 5
    _assert_valid_batches(unscreened.items, tmp_path / "unsc", namespace=NS)
    for idx, exp_code in ((1, "E_CONTEXT_SECRET"), (4, "E_CONTEXT_CLIENT_NAME")):
        try:
            ctx_val.screens.screen(*ctx_val._exchange_screen_text(unscreened.items[idx]))
            raise AssertionError("missed %s" % exp_code)
        except HandlerError as exc:
            assert exc.code == exp_code
    lone = "bad-\ud800-tail"
    sp = feed(ag_parser(), [ag_plan(1, 1, thinking=lone, calls=[{"id": lone, "name": "fn-\ud800", "args": {"k": lone}}]),
                            ag_res(2, {"out": lone}, 1, tool_call_id=lone)])
    _assert_valid_batches(sp.items, tmp_path / "surr")
    assert all("\ud800" not in i["text"] and "\ufffd" in i["text"] for i in sp.items)


def test_antigravity_f3_vendor_prefixes_23oct_leak_and_part_boundary_screen(tmp_path, monkeypatch):
    (tmp_path / "deny.json").write_text('["acmecorpsecret"]', encoding="utf-8")
    monkeypatch.setenv("MESHY_CONTEXT_DENYLIST", str(tmp_path / "deny.json"))
    mix = lambda n: ("aB3dE5gH7jK9mN2pQ4rS6tU8vW0xY1zA" * (n // 32 + 2))[:n]
    samples = [p + mix(32) for p in ("sk-", "sk-ant-", "sk-proj-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
                                     "xoxb-", "xoxa-", "xoxp-", "xoxo-", "xoxs-", "xoxr-", "AIza", "xai-", "hf_", "glpat-",
                                     "lin_api_", "npm_", "pypi-", "rk_")]
    samples += [p + "Q3W7E9R2T5Y8U1I4" for p in ("AKIA", "ASIA", "ABIA", "ACCA")]
    samples += ['SERVICE_API_KEY = "' + mix(108) + '"', 'SERVICE_API_KEY = "' + mix(51) + '"']
    recs = [ag_plan(1, 1, calls=[{"name": "read_file", "args": {"n": i}} for i in range(len(samples))])]
    recs += [ag_res(2 + i, "cfg:\n" + val, 1) for i, val in enumerate(samples)]
    p = feed(ag_parser(namespace=None), recs)
    assert [(i["item"], i.get("omitted_reason")) for i in p.items] == [("omitted", "secret")] * len(samples)
    leak51, filler = '\nSERVICE_API_KEY = "' + mix(51) + '"\n', ("y" * 79 + "\n") * 1900
    probe = feed(ag_parser(namespace=None), [ag_plan(1, 1, calls=[{"name": "read_file"}]), ag_res(2, filler, 1)])
    offset = len(json.loads(exchanges(probe)[0]["text"] + '"}')["result"]) - 24
    body = filler[:offset] + leak51 + filler[offset:]
    whole_text = json.dumps({"tool": "read_file", "call": "{}", "result": body}, ensure_ascii=False, separators=(",", ":"))
    p1_text = exchanges(feed(ag_parser(), [ag_plan(1, 1, calls=[{"name": "read_file"}]), ag_res(2, body, 1)]))[0]["text"]
    assert 'SERVICE_API_KEY = \\"' + mix(51) not in p1_text and 'SERVICE_API_KEY = \\"' + mix(51) not in whole_text[len(p1_text):]
    bp = feed(ag_parser(namespace=None), [ag_plan(1, 1, calls=[{"name": "read_file"}]), ag_res(2, body, 1)])
    assert exchanges(bp) == [] and [(i["item"], i["omitted_reason"]) for i in bp.items] == [("omitted", "secret")]
    _assert_valid_batches(bp.items, tmp_path / "b", namespace="shared/scratch-ctx")


def test_antigravity_truncated_fields_fail_closed_and_preserve_step_alignment(tmp_path):
    recs = [ag_plan(1, 1, content="chopped prose", thinking="chopped thought",
                    calls=[{"name": "ok_tool", "args": {"x": 1}}], truncated_fields=["thinking", "content"]),
            ag_res(2, "ok-res", 1),
            ag_plan(3, 1, calls=[{"name": "chop1"}, {"name": "chop2"}], truncated_fields=["tool_calls"]),
            ag_res(4, "res-of-chop1", 1), ag_res(5, "chopped-res-of-chop2", 1, truncated_fields=["content"]),
            ag_plan(6, 2, calls=[{"name": "untrunc_call", "args": {"k": "v"}}, {"name": "after_trunc", "args": {"k": "v2"}}]),
            ag_res(7, "chopped-generic", 2, truncated_fields=["content"]), ag_res(8, "clean-v2", 2),
            dict(ag_user(9, "", 3, truncated_fields=["content"]), content=""),
            ag_plan(10, 3, content="bad1", thinking="bad2", calls=[{"name": "bad3"}], truncated_fields="malformed"),
            ag_res(11, "after-malformed", 3, truncated_fields=[123])]
    p = feed(ag_parser(), recs)
    _assert_valid_batches(p.items, tmp_path)
    assert [(whole(x)["tool"], x["call_status"], x["result_status"], whole(x)["result"]) for x in exchanges(p)] == [
        ("ok_tool", "ok", "ok", "ok-res"), (None, "missing", "ok", "res-of-chop1"),
        ("untrunc_call", "ok", "missing", None), ("after_trunc", "ok", "ok", "clean-v2")]
    assert [r for r, _ in p.skipped].count("truncated_fields") == 10
    logs = tmp_path / "brain" / "conv-123" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text(json.dumps(ag_plan(1, 1, thinking="chop", truncated_fields=["thinking"])) + "\n", encoding="utf-8")
    (logs / "transcript_full.jsonl").write_text(
        json.dumps(ag_plan(1, 1, thinking="full thought", calls=[{"name": "t"}])) + "\n" + json.dumps(ag_res(2, "full result", 1)) + "\n", encoding="utf-8")
    pf, _rep = cp.parse_file(str(logs / "transcript.jsonl"), SEAT, "antigravity", namespace=NS, f3=True)
    assert [(i["item"], i.get("text")) for i in pf.items] == [
        ("thinking", "full thought"), ("tool_exchange", '{"tool":"t","call":"{}","result":"full result"}')]


def test_antigravity_f1_reserve_split_batches_dialect_cap_and_resume(tmp_path, monkeypatch):
    b1 = ag_parser(batch_items=1)
    assert len(b1.feed(ag_plan(1, 1, content="visible", thinking="dropped"))) == 1
    assert [i["item"] for i in b1.items] == ["assistant_text"] and ("record_full", b1.src + ":1") in b1.skipped
    b2 = ag_parser(batch_items=2)
    b2.feed(ag_plan(1, 1, calls=[{"name": "big", "args": {"b": "x" * (cp.F3_PENDING_BYTES - 200)}}]))
    assert len(b2.feed(ag_plan(2, 1, content="kept reply", thinking="thought", calls=[{"name": "push", "args": {"b": "y" * 500}}]))) == 2
    assert b2.items[-1]["item"] == "assistant_text"
    b2u = ag_parser(batch_items=2)
    b2u.feed(ag_plan(1, 1, calls=[{"name": "c1"}, {"name": "c2"}]))
    assert len(b2u.feed(ag_user(2, "interrupt", 2))) == 2
    assert [i["item"] for i in b2u.items] == ["tool_exchange", "user_text"] and any(r == "record_full" for r, _ in b2u.skipped)
    nt = ag_parser(batch_items=1)
    nt.feed(ag_plan(1, 1, calls=[{"name": "c1"}]))
    assert len(nt.feed(dict(ag_user(2, "bad-ts", 2), created_at="bad"))) == 1 and [i["item"] for i in nt.items] == ["tool_exchange"]
    sf = tmp_path / "split.jsonl"
    sf.write_text("".join(json.dumps(r) + "\n" for r in [
        ag_user(1, "start", 1), ag_plan(2, 1, calls=[{"name": "read_file", "args": {"Path": "/big"}}]), ag_res(3, "q" * 100_000, 1)]), encoding="utf-8")
    sp, _ = cp.parse_file(str(sf), SEAT, "antigravity", batch_items=2, namespace=NS, session_id="ag-split", f3=True)
    _assert_valid_batches(sp.items, tmp_path / "split_v")
    assert len(exchanges(sp)) == 2 and len({x["batch"] for x in exchanges(sp)}) == 1
    monkeypatch.setattr(cp, "F3_MAX_PARTS", 2)
    huge = feed(ag_parser(), [ag_plan(1, 1, calls=[{"name": "r"}]), ag_res(2, "z" * 250_000, 1)])
    assert [(i["item"], i.get("omitted_reason")) for i in huge.items] == [("omitted", "too_large")]
    monkeypatch.setattr(cp, "F3_DIALECT_BYTES", 140)
    capped = feed(ag_parser(), [ag_plan(1, 1, calls=[{"name": "c%d" % n} for n in range(6)])])
    assert len(cp.serialize(capped.state()["f3_dialect"])) <= 140
    assert exchanges(capped) and all(x["result_status"] == "missing" for x in exchanges(capped)) and whole(exchanges(capped)[0])["tool"] == "c0"


def test_antigravity_line_by_line_checkpoint_resume_and_state_validation():
    recs = [ag_user(1, "go", 0), ag_plan(2, 1, content="step", thinking="think", calls=[{"name": "t1", "args": {"a": 1}}, {"name": "t2", "args": {"a": 2}}]),
            ag_res(3, "r1", 1), ag_res(4, "r2", 2, status="ERROR"), ag_res(6, "early-r", 3), ag_res(7, "early-exp", 3, tool_call_id="exp-e"),
            ag_plan(5, 3, calls=[{"name": "early_t", "args": {}}, {"id": "exp-e", "name": "exp_t", "args": {}}, {"name": "open_call", "args": {}}]),
            ag_user(8, "interrupt", 4), ag_plan(9, 5, calls=[{"name": "wait_eof", "args": {}}]), ag_sys(10, "later", 11)]

    def run(roundtrip):
        p = ag_parser()
        for idx, r in enumerate(recs):
            if roundtrip:
                items, skipped = p.items, p.skipped
                p = cp.ContextParser.from_state(json.loads(json.dumps(p.state())), namespace=NS)
                p.items, p.skipped = items, skipped
            p.feed(r)
            if idx == len(recs) - 1:
                p.flush_pending()
        return p

    direct, resumed = run(False), run(True)
    assert [cp.serialize(i) for i in resumed.items] == [cp.serialize(i) for i in direct.items]
    assert resumed.skipped == direct.skipped and resumed.state() == direct.state()
    base, ns_ok, h32 = direct.state(), "2026-10-27T08:01:00.000000000Z", "0" * 32
    for bad in ({"extra": 1}, {"steps": "bad"}, {"steps": [[]]}, {"steps": [[-1, "x", ""]]}, {"steps": [[1, "", ""]]}, {"steps": [[1, 123, ""]]},
                {"steps": [[1, "x", "bad"]]}, {"early": "bad"}, {"early": [["short", 1, "b", False, "", None]]}, {"early": [[h32, 0, "b", False, "", None]]},
                {"early": [[h32, "", "b", False, "", None]]}, {"early": [[h32, True, "b", False, "", None]]}, {"early": [[h32, 1, 9, False, "", None]]},
                {"early": [[h32, 1, "b", 1, "", None]]}, {"early": [[h32, 1, "b", False, "bad", None]]}, {"early": [[h32, 1, "b", False, "", 9]]},
                {"early": [[h32, 1, "a", False, "", None], [h32, 2, "b", False, "", None]]}, {"res_seen": "bad"}, {"res_seen": [[1, "bad", ns_ok]]},
                {"res_seen": [[-1, "", ns_ok]]}, {"res_seen": [[1, "", "bad"]]}, {"res_seen": [[1, "", ns_ok], [1, "", ns_ok]]}, {"res_hwm": "bad"},
                {"res_hwm": [1, ns_ok]}, {"res_hwm": [-1, ns_ok, 0, ""]}, {"res_hwm": [1, "bad", 1, ""]}, {"res_hwm": [1, ns_ok, 2, ""]},
                {"res_hwm": [1, ns_ok, 1, "bad"]}, {"anon": 0}, {"anon": True}):
        try:
            cp.validate_state(dict(base, f3_dialect=bad))
            raise AssertionError("tampered f3_dialect was accepted: %r" % (bad,))
        except ValueError:
            pass
