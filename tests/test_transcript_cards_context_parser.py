"""test_transcript_cards_context_parser.py - the context.v1 parser (txxxx, parser owner).

The EXPECTED outcomes below are written by hand from the rules in docs/design/context-v1.md (r4 + #346) and txxxx's
b2d1a0 plan rule, BEFORE the parser existed (this file was committed red). They are not produced by the parser and
no helper of the parser builds them. Only the fixture records, the key rule inputs (sha256 prefixes) and the text
digests are computed here, with hashlib.

Run:
  python tests/test_transcript_cards_context_parser.py
  pytest tests/test_transcript_cards_context_parser.py -q
"""

import hashlib
import json
import os
import sys
import time

LOADER_DIR = os.environ.get("TRANSCRIPT_CARDS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
if LOADER_DIR not in sys.path:
    sys.path.insert(0, LOADER_DIR)

import context_parser as cp  # noqa: E402

SEAT = "fixture-seat"
STAMP = "2026-10-10T11:00:00Z"
WIRE_TIME = "2026-10-10T11:00:00.000Z"
PRIVATE = "SYNTHETIC-PRIVATE-ANALYSIS-MUST-NOT-BE-GRAPHED"
CLIENT = "SYNTHETIC-OPERATOR-CLIENT-ALPHA"
TAIL = "\nConstraint: keep the cache local. Artifact: amber-report.json. OPEN: verify AMBER-TAIL."
VISIBLE = "Visible progress: " + "Vienna \U0001f9ed caf\u00e9. " * 900 + TAIL
COMMENTARY = "Visible commentary: retain the deployment decision."
KEY_MARKER = "-----BEGIN PRIVATE KEY-----"


# The fields v8 added (F3, 27 Oct 2026): a real v6 or v7 checkpoint has none of them.
V8_KEYS = ("f3", "tool_pending", "tool_seen", "newest_time", "f3_dialect")


def h(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def h32(text):
    return h(text)[:32]


# ---- the fixture records (Codex rollout shape, as codex-kxxx-cli's six cases build them) --------------------------

def record(kind, payload):
    return {"type": kind, "timestamp": STAMP, "payload": payload}


def assistant(item, text, phase="final_answer"):
    return record("response_item", {"type": "message", "id": item, "role": "assistant", "phase": phase,
                                    "content": [{"type": "output_text", "text": text}]})


def user(identity, text):
    return record("response_item", {"type": "message", "id": identity, "role": "user",
                                    "content": [{"type": "input_text", "text": text}]})


def plan(call, steps):
    return record("response_item", {"type": "function_call", "call_id": call, "name": "update_plan",
                                    "arguments": json.dumps({"plan": [{"step": s, "status": st} for s, st in steps]})})


def meta(session, subagent=False):
    return record("session_meta", {"id": session, "originator": "codex-tui",
                                   "source": {"subagent": {"thread_spawn": {"parent_thread_id": "synthetic-parent"}}}
                                   if subagent else "cli"})


def parse(records, session, denylist=(CLIENT,), batch_items=256, dialect="codex"):
    parser = cp.ContextParser(seat=SEAT, dialect=dialect, denylist=list(denylist), batch_items=batch_items)
    for r in records:
        parser.feed(r)
    return parser


def view(parser):
    keep = ("ordinal", "item", "role", "text", "step", "event", "counts", "omitted_reason", "omitted_bytes")
    return [{k: v for k, v in item.items() if k in keep and not (k == "text" and item["item"] == "task")}
            for item in parser.items]


def text_item(ordinal, kind, role, text):
    row = {"ordinal": ordinal, "item": kind, "role": role, "text": text}
    if role is None:
        del row["role"]
    return row


def task(ordinal, event, step, counts=None):
    row = {"ordinal": ordinal, "item": "task", "event": event, "step": step}
    if counts is not None:
        row["counts"] = counts
    return row


def omitted(ordinal, reason, nbytes=None):
    row = {"ordinal": ordinal, "item": "omitted", "omitted_reason": reason}
    if nbytes is not None:
        row["omitted_bytes"] = nbytes
    return row


# ---- the nine acceptance cases ----------------------------------------------------------------------------------

def case_visible():
    recs = [meta("synthetic-visible"), assistant("private-analysis", PRIVATE, "analysis"),
            assistant("visible-commentary", COMMENTARY, "commentary"), assistant("visible-long", VISIBLE),
            record("event_msg", {"type": "agent_message", "message": VISIBLE})]
    return recs, "synthetic-visible", [text_item(0, "assistant_text", "assistant", COMMENTARY),
                                       text_item(1, "assistant_text", "assistant", VISIBLE)]


def case_plan():
    recs = [meta("synthetic-plan"),
            plan("plan-initial", [("A", "pending"), ("A", "pending")]),
            plan("plan-complete-one", [("A", "completed"), ("A", "pending")]),
            plan("plan-reorder", [("A", "pending"), ("A", "completed")]),
            plan("plan-remove", [("A", "completed")]),
            plan("plan-reword", [("B", "pending")])]
    return recs, "synthetic-plan", [task(0, "added", "A"), task(1, "added", "A"), task(2, "completed", "A"),
                                    task(3, "removed", "A"), task(4, "removed", "A"), task(5, "added", "B")]


def case_ambiguous():
    recs = [meta("synthetic-ambiguous"), plan("ambiguous-initial", [("A", "pending"), ("A", "pending")]),
            plan("ambiguous-change", [("A", "completed")])]
    return recs, "synthetic-ambiguous", [task(0, "added", "A"), task(1, "added", "A"),
                                         task(2, "ambiguous", "A", {"pending": 0, "in_progress": 0, "completed": 1})]


def case_client_tail():
    return ([meta("synthetic-name"), assistant("blocked-name", VISIBLE + "\n" + CLIENT)], "synthetic-name",
            [omitted(0, "client_name")])


def case_secret_tail():
    return ([meta("synthetic-secret"), assistant("blocked-secret", VISIBLE + "\n" + KEY_MARKER)],
            "synthetic-secret", [omitted(0, "secret")])


def case_subagent():
    return ([meta("synthetic-child", subagent=True), assistant("child-prose", VISIBLE),
             plan("child-plan", [("child work", "pending")])], "synthetic-child",
            [text_item(0, "artifact", None, VISIBLE)])


def case_authority():
    return ([meta("synthetic-dialogue"), user("operator-long", VISIBLE), assistant("assistant-repeats-operator", VISIBLE)],
            "synthetic-dialogue", [text_item(0, "user_text", "user", VISIBLE),
                                   text_item(1, "assistant_text", "assistant", VISIBLE)])


def case_user_client():
    return ([meta("synthetic-user-name"), user("blocked-user-name", VISIBLE + "\n" + CLIENT)],
            "synthetic-user-name", [omitted(0, "client_name")])


def case_user_secret():
    return ([meta("synthetic-user-secret"), user("blocked-user-secret", VISIBLE + "\n" + KEY_MARKER)],
            "synthetic-user-secret", [omitted(0, "secret")])


CASES = [case_visible, case_plan, case_ambiguous, case_client_tail, case_secret_tail, case_subagent, case_authority,
         case_user_client, case_user_secret]


def test_acceptance_cases_match_hand_derived_outcomes():
    for build in CASES:
        recs, session, expected = build()
        parser = parse(recs, session)
        parser.finish()                       # a subagent source gives its artifact here; others give nothing
        got = view(parser)
        assert got == expected, (build.__name__, got, expected)


def test_item_envelope_is_exactly_section_5():
    recs, session, _ = case_visible()
    parser = parse(recs, session)
    first = parser.items[0]
    assert list(first) == ["schema", "kind", "seat", "src", "gen", "batch", "ordinal", "item", "role", "dialect",
                           "adapter_version", "time", "native_ref", "text", "text_sha256"], list(first)
    assert first["schema"] == "context.v1" and first["kind"] == "item" and first["seat"] == SEAT
    assert first["src"] == h32(session) and first["gen"] == 1 and first["batch"] == 1
    assert first["dialect"] == "codex" and first["time"] == WIRE_TIME
    assert first["native_ref"] == h32("visible-commentary")
    assert first["text_sha256"] == h(COMMENTARY)
    import re
    assert re.fullmatch(r"\d{1,4}(\.\d{1,4}){0,3}", first["adapter_version"])


def test_key_rule():
    recs, session, _ = case_visible()
    parser = parse(recs, session)
    keys = [cp.item_key(i) for i in parser.items]
    assert keys == ["ctx/%s/%s/0001/000001/%010d" % (SEAT, h32(session), n) for n in (0, 1)], keys
    assert cp.manifest_key(parser.items[0]) == "ctxm/%s/%s/0001/000001" % (SEAT, h32(session))


def test_omitted_item_keeps_nothing_derived_from_the_text():
    recs, session, _ = case_secret_tail()
    item = parse(recs, session).items[0]
    assert list(item) == ["schema", "kind", "seat", "src", "gen", "batch", "ordinal", "item", "dialect",
                          "adapter_version", "time", "native_ref", "omitted_reason"], list(item)
    blob = json.dumps(item)
    assert h(VISIBLE + "\n" + KEY_MARKER) not in blob and "text" not in item and "text_sha256" not in item


def test_task_items_carry_no_role_and_are_screened_on_step_and_text():
    recs, session, _ = case_plan()
    t = parse(recs, session).items[0]
    assert "role" not in t and t["step"] == "A" and t["event"] == "added" and "counts" not in t
    assert t["text"] == "plan step added: A", t["text"]
    marked = parse([meta("s"), plan("p", [(CLIENT, "pending")])], "s")
    assert view(marked) == [omitted(0, "client_name")]
    secret_step = parse([meta("s"), plan("p", [(KEY_MARKER, "pending")])], "s")
    assert view(secret_step) == [omitted(0, "secret")]


def test_markers_at_start_middle_and_end_of_a_60000_byte_item_are_all_dropped_and_fast():
    filler = "ordinary prose about caching. " * 2000
    filler = filler[:60000]
    for marker, reason in ((KEY_MARKER, "secret"), (CLIENT, "client_name")):
        for position in (0, len(filler) // 2, len(filler)):
            text = filler[:position] + marker + filler[position:]
            began = time.time()
            got = view(parse([meta("s"), assistant("a", text)], "s"))
            assert got == [omitted(0, reason)], (reason, position, got)
            assert time.time() - began < 20, "screening a 60 KB item must be linear"
    clean = view(parse([meta("s"), assistant("a", filler)], "s"))
    assert clean == [text_item(0, "assistant_text", "assistant", filler)], "positive control: clean text is kept"


def test_client_name_screen_uses_nfkc_casefold_letters_and_digits_only():
    spaced = "Zor blax-Corp"
    got = view(parse([meta("s"), assistant("a", "see " + spaced + " notes")], "s", denylist=["zorblaxcorp"]))
    assert got == [omitted(0, "client_name")], got
    try:
        parse([meta("s")], "s", denylist=["abc"])
    except ValueError:
        pass
    else:
        raise AssertionError("a marker under 4 letters or digits must refuse the whole list")


def test_budget_exact_fit_is_stored_and_one_byte_over_is_omitted_too_large():
    probe = parse([meta("s"), assistant("a", "x")], "s").items[0]
    overhead = len(cp.serialize(probe)) - 1
    fit = "x" * (65536 - overhead)
    over = fit + "x"
    stored = parse([meta("s"), assistant("a", fit)], "s").items[0]
    assert stored["item"] == "assistant_text" and len(cp.serialize(stored)) == 65536
    refused = parse([meta("s"), assistant("a", over)], "s").items[0]
    assert refused["item"] == "omitted" and refused["omitted_reason"] == "too_large"
    assert refused["omitted_bytes"] == len(over.encode("utf-8")) and "text" not in refused


def test_budget_counts_escaping_not_raw_bytes():
    quotes = '"' * 40000          # 40,000 raw bytes, 80,000 serialized
    got = parse([meta("s"), assistant("a", quotes)], "s").items[0]
    assert got["item"] == "omitted" and got["omitted_reason"] == "too_large" and got["omitted_bytes"] == 40000


def test_omitted_bytes_is_the_utf8_size_not_the_character_count():
    wide = chr(0xe9) * 40000                  # two bytes each in UTF-8: 80,000 bytes, over the budget
    got = parse([meta("s"), assistant("a", wide)], "s").items[0]
    assert got["item"] == "omitted" and got["omitted_reason"] == "too_large" and got["omitted_bytes"] == 80000, got


def test_machine_typed_user_prompts_are_not_dialogue():
    got = parse([meta("s"), user("m", "MANDATORY session-start action: do the thing"),
                 user("n", "Process this banked delivery in one turn."), user("o", "a real operator line")], "s")
    assert view(got) == [text_item(0, "user_text", "user", "a real operator line")]


def test_lone_surrogate_is_replaced_before_hashing():
    got = parse([meta("s"), assistant("a", "a\ud800b")], "s").items[0]
    assert got["text"] == "a\ufffdb" and got["text_sha256"] == h("a\ufffdb")
    cp.serialize(got)             # must encode as UTF-8


def test_plan_rules_each_directly():
    only = lambda recs: [(r["item"], r["event"], r["step"]) for r in view(parse([meta("s")] + recs, "s"))]
    # a reorder yields nothing
    assert only([plan("1", [("A", "pending"), ("B", "pending")]), plan("2", [("B", "pending"), ("A", "pending")])]) == \
        [("task", "added", "A"), ("task", "added", "B")]
    # a removal is never a completion
    assert only([plan("1", [("A", "pending"), ("A", "pending")]), plan("2", [("A", "pending")])]) == \
        [("task", "added", "A"), ("task", "added", "A"), ("task", "removed", "A")]
    # a status change on one step is a completion; going back is neither a completion nor an addition
    assert only([plan("1", [("A", "pending")]), plan("2", [("A", "in_progress")]), plan("3", [("A", "completed")]),
                 plan("4", [("A", "pending")])]) == [("task", "added", "A"), ("task", "completed", "A")]
    # an unchanged plan repeated yields nothing
    assert only([plan("1", [("A", "pending")]), plan("2", [("A", "pending")])]) == [("task", "added", "A")]


def test_duplicate_event_msg_and_analysis_never_become_items():
    recs, session, _ = case_visible()
    assert all(PRIVATE not in json.dumps(i) for i in parse(recs, session).items)
    assert len(parse(recs, session).items) == 2


def test_determinism_and_window_cut_with_a_fresh_process():
    recs, session, _ = case_plan()
    whole = parse(recs, session)
    expected_bytes = [cp.serialize(i) for i in whole.items]
    for cut in range(1, len(recs)):
        first = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT], batch_items=256)
        for r in recs[:cut]:
            first.feed(r)
        revived = cp.ContextParser.from_state(json.loads(json.dumps(first.state())), denylist=[CLIENT])      # a fresh process
        for r in recs[cut:]:
            revived.feed(r)
        got = [cp.serialize(i) for i in first.items] + [cp.serialize(i) for i in revived.items]
        assert got == expected_bytes, cut


def test_batching_and_manifests():
    recs = [meta("big")] + [assistant("a%d" % n, "turn %d" % n) for n in range(600)]
    parser = parse(recs, "big", batch_items=256)
    assert [(i["batch"], i["ordinal"]) for i in parser.items[:1] + parser.items[255:258] + parser.items[-1:]] == \
        [(1, 0), (1, 255), (2, 256), (2, 257), (3, 599)]
    manifests = cp.manifests(parser.items, 256)
    assert [(m["batch"], m["first"], m["last"]) for m in manifests] == [(1, 0, 255), (2, 256, 511), (3, 512, 599)]
    m1 = manifests[0]
    assert list(m1) == ["schema", "kind", "seat", "src", "gen", "batch", "first", "last", "items", "counts"], list(m1)
    assert m1["items"][0] == [0, h("turn 0")] and len(m1["items"]) == 256
    assert m1["counts"] == {"written": 256, "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 0}


def test_manifest_of_a_drop_only_batch_has_all_null_entries():
    recs, session, _ = case_secret_tail()
    parser = parse(recs, session)
    (m,) = cp.manifests(parser.items, 256)
    assert m["items"] == [[0, None]] and m["counts"] == {"written": 0, "omitted_secret": 1,
                                                          "omitted_client_name": 0, "omitted_too_large": 0}


def test_claude_dialect_one_item_per_text_block_sharing_record_time_and_ref():
    sid = "claude-session-1"
    u = {"type": "user", "sessionId": sid, "uuid": "u-1", "timestamp": "2026-10-10T09:00:00.123Z",
         "message": {"role": "user", "content": "what is the plan?"}}
    a = {"type": "assistant", "sessionId": sid, "uuid": "a-1", "timestamp": "2026-10-10T09:00:05.500Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "First block."},
             {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {"file_path": "x"}},
             {"type": "text", "text": "Second block."}]}}
    tool_result = {"type": "user", "sessionId": sid, "uuid": "u-2", "timestamp": "2026-10-10T09:00:06.000Z",
                   "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01",
                                                            "content": "file contents"}]}}
    parser = cp.ContextParser(seat=SEAT, dialect="claude", denylist=[], batch_items=256)
    for r in (u, a, tool_result):
        parser.feed(r)
    got = view(parser)
    assert got == [text_item(0, "user_text", "user", "what is the plan?"),
                   text_item(1, "assistant_text", "assistant", "First block."),
                   text_item(2, "assistant_text", "assistant", "Second block.")], got
    assert parser.items[1]["time"] == parser.items[2]["time"] == "2026-10-10T09:00:05.500Z"
    assert parser.items[1]["native_ref"] == parser.items[2]["native_ref"] == h32("a-1")
    assert parser.items[0]["src"] == h32(sid)


def test_claude_sidechain_and_meta_records_are_not_dialogue():
    sid = "claude-session-2"
    side = {"type": "assistant", "sessionId": sid, "uuid": "s-1", "isSidechain": True,
            "timestamp": "2026-10-10T09:00:05.500Z", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "subagent prose"}]}}
    meta_rec = {"type": "user", "sessionId": sid, "uuid": "m-1", "isMeta": True,
                "timestamp": "2026-10-10T09:00:06.000Z", "message": {"role": "user", "content": "caveat text"}}
    parser = cp.ContextParser(seat=SEAT, dialect="claude", denylist=[], batch_items=256)
    parser.feed(side)
    parser.feed(meta_rec)
    assert parser.items == []


def test_oversize_step_is_screened_whole_before_it_is_called_too_large():
    long_step = "x" * 1001
    plain = view(parse([meta("s"), plan("p", [(long_step, "pending")])], "s"))
    assert plain == [omitted(0, "too_large", 1001)], plain
    named = view(parse([meta("s"), plan("p", [("ACME-CORP-LTD" + "x" * 1000, "pending")])], "s",
                       denylist=["acmecorpltd"]))
    assert named == [omitted(0, "client_name")], named
    keyed = view(parse([meta("s"), plan("p", [(KEY_MARKER + "x" * 1000, "pending")])], "s"))
    assert keyed == [omitted(0, "secret")], keyed


def test_a_short_step_never_collides_with_the_key_of_a_long_one():
    import hashlib as hl
    long_step = "x" * 1001
    lookalike = "h:" + hl.sha256(long_step.encode("utf-8")).hexdigest()
    got = view(parse([meta("s"), plan("1", [(long_step, "pending")]), plan("2", [(lookalike, "pending")])], "s"))
    assert got == [omitted(0, "too_large", 1001), omitted(1, "too_large", 1001), task(2, "added", lookalike)], got
    literal = view(parse([meta("s"), plan("1", [("sha256:abc", "pending")])], "s"))
    assert literal == [task(0, "added", "sha256:abc")], literal


def test_surrogates_in_session_and_record_ids_are_cleaned_before_hashing():
    recs = [meta("sess" + chr(0xd800)), assistant("rec" + chr(0xd801), "hello")]
    item = parse(recs, "x").items[0]
    assert item["src"] == h32("sess" + chr(0xfffd)) and item["native_ref"] == h32("rec" + chr(0xfffd))


def test_each_text_block_is_its_own_item_and_tool_results_do_not_hide_text():
    multi = record("response_item", {"type": "message", "id": "m", "role": "assistant", "phase": "final_answer",
                                     "content": [{"type": "output_text", "text": "A"}, {"type": "output_text", "text": "B"}]})
    got = parse([meta("s"), multi], "s")
    assert [i["text"] for i in got.items] == ["A", "B"] and got.items[0]["native_ref"] == got.items[1]["native_ref"]
    mixed = {"type": "user", "sessionId": "c", "uuid": "u", "timestamp": "2026-10-10T09:00:00.000Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t", "content": "output"},
                 {"type": "text", "text": "hello"}, {"type": "text", "text": "again"}]}}
    parser = cp.ContextParser(seat=SEAT, dialect="claude", denylist=[], batch_items=256)
    parser.feed(mixed)
    assert [i["text"] for i in parser.items] == ["hello", "again"]


def test_state_carries_the_machine_filter_and_refuses_a_different_denylist():
    first = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT], drop_machine_typed=False)
    first.feed(meta("s"))
    revived = cp.ContextParser.from_state(json.loads(json.dumps(first.state())), denylist=[CLIENT])
    revived.feed(user("m", "MANDATORY session-start action: do the thing"))
    assert [i["text"] for i in revived.items] == ["MANDATORY session-start action: do the thing"]
    try:
        cp.ContextParser.from_state(first.state(), denylist=["another-client-name"])
    except ValueError:
        pass
    else:
        raise AssertionError("a state written under one client-name list must not resume under another")


def test_oversize_table_is_pruned_when_the_step_leaves_the_plan():
    parser = parse([meta("s"), plan("1", [("y" * 1001, "pending")]), plan("2", [("B", "pending")])], "s")
    assert parser.state()["oversize"] == {}


def test_cli_writes_identical_bytes_twice_and_reports_the_input_digest():
    import tempfile
    work = tempfile.mkdtemp()
    recs, session, _ = case_authority()
    source = os.path.join(work, "in.jsonl")
    raw = "".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in recs).encode("utf-8")
    open(source, "wb").write(raw)
    deny = os.path.join(work, "deny.json")
    open(deny, "w").write(json.dumps([CLIENT]))
    outs = []
    for n in (1, 2):
        out = os.path.join(work, "out%d" % n)
        assert cp.main([source, "--seat", SEAT, "--dialect", "codex", "--denylist", deny, "--out", out]) == 0
        outs.append({f: open(os.path.join(out, f), "rb").read() for f in ("items.jsonl", "manifests.jsonl")})
    assert outs[0] == outs[1]
    report = json.load(open(os.path.join(work, "out1", "report.json"), encoding="utf-8"))
    assert report["input_sha256"] == hashlib.sha256(raw).hexdigest() and report["items"] == 2
    assert report["by_kind"] == {"assistant_text": 1, "user_text": 1} and report["adapter_version"] == "1.0"
    first = json.loads(outs[0]["items.jsonl"].splitlines()[0])
    assert first["key"] == "ctx/%s/%s/0001/000001/0000000000" % (SEAT, h32(session)) and first["value"]["ordinal"] == 0


def test_a_skipped_record_consumes_no_ordinal():
    undated = dict(assistant("no-time", "dropped for want of a time"), timestamp="")
    undated_plan = dict(plan("p0", [("Z", "pending")]), timestamp="")
    parser = parse([meta("s"), assistant("one", "first"), undated, undated_plan, assistant("two", "second")], "s")
    assert [(i["ordinal"], i["text"]) for i in parser.items] == [(0, "first"), (1, "second")]
    assert sorted(r for r, _ in parser.skipped) == ["no_time", "no_time"]
    after = parse([meta("s"), undated_plan, plan("p1", [("Z", "completed")])], "s")
    assert [(i["ordinal"], i["event"]) for i in after.items] == [(0, "completed")], "state still advanced on the undated plan"


def test_subagent_gives_one_artifact_the_last_non_empty_assistant_block_and_nothing_else():
    recs = [meta("agent-1", subagent=True), assistant("c1", "first thought", "commentary"),
            plan("cp", [("work", "pending")]), user("cu", "an instruction"), assistant("c2", "the final report"),
            assistant("c3", "   "), assistant("c4", "hidden reasoning", "analysis")]
    parser = parse(recs, "agent-1")
    assert parser.items == [], "nothing is emitted until the source is finished"
    (item,) = parser.finish()
    assert list(item) == ["schema", "kind", "seat", "src", "gen", "batch", "ordinal", "item", "dialect",
                          "adapter_version", "time", "native_ref", "text", "text_sha256", "agent_id"], list(item)
    assert item["item"] == "artifact" and "role" not in item and item["text"] == "the final report"
    assert item["agent_id"] == h32("agent-1") == item["src"] and item["native_ref"] == h32("c2")
    assert item["ordinal"] == 0 and item["text_sha256"] == h("the final report")
    assert parser.finish() == [], "finish is idempotent"
    try:
        parser.feed(assistant("late", "after the end"))
    except ValueError:
        pass
    else:
        raise AssertionError("a finished source takes no more records")


def test_subagent_with_no_final_text_gives_no_artifact_and_says_why():
    parser = parse([meta("agent-2", subagent=True), plan("p", [("w", "pending")]),
                    assistant("a", "private", "analysis")], "agent-2")
    assert parser.finish() == [] and parser.skipped == [("no_final_text", None)]


def test_subagent_final_report_is_screened_whole_and_budgeted_like_any_text():
    for text, expected in ((VISIBLE + chr(10) + KEY_MARKER, omitted(0, "secret")),
                           (VISIBLE + chr(10) + CLIENT, omitted(0, "client_name")),
                           ("z" * 70000, omitted(0, "too_large", 70000))):
        parser = parse([meta("agent-3", subagent=True), assistant("f", text)], "agent-3")
        parser.finish()
        assert view(parser) == [expected], (text[:20], view(parser))
        assert "agent_id" not in parser.items[0] and "text" not in parser.items[0]


def test_a_cut_before_the_end_keeps_the_pending_final_report_in_state():
    recs = [meta("agent-4", subagent=True), assistant("a", "draft one"), assistant("b", "the real ending")]
    whole = parse(recs, "agent-4")
    whole.finish()
    first = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
    for r in recs[:2]:
        first.feed(r)
    revived = cp.ContextParser.from_state(json.loads(json.dumps(first.state())), denylist=[CLIENT])
    revived.feed(recs[2])
    revived.finish()
    assert [cp.serialize(i) for i in revived.items] == [cp.serialize(i) for i in whole.items]


def test_a_cut_after_the_final_block_still_finishes_with_it():
    recs = [meta("agent-5", subagent=True), assistant("a", "the only report")]
    first = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
    for r in recs:
        first.feed(r)
    revived = cp.ContextParser.from_state(json.loads(json.dumps(first.state())), denylist=[CLIENT])
    (item,) = revived.finish()
    assert item["text"] == "the only report" and item["native_ref"] == h32("a") and item["agent_id"] == h32("agent-5")


def test_a_state_from_an_older_parser_version_is_refused_with_a_clear_error():
    old = {"v": 1, "seat": SEAT, "dialect": "codex", "gen": 1, "batch_items": 256, "adapter_version": "1.0",
           "denylist_sha256": "", "src": h32("s"), "subagent": False, "ordinal": 3, "plan": [], "oversize": {}}
    try:
        cp.ContextParser.from_state(old)
    except ValueError as e:
        assert "rebuild" in str(e), str(e)
    else:
        raise AssertionError("a version 1 state must be refused, not resumed")
    v2 = dict(old, v=2)                      # the state shape before batches were the uploader's to cut
    try:
        cp.ContextParser.from_state(v2)
    except ValueError as e:
        assert "rebuild" in str(e), str(e)
    else:
        raise AssertionError("a version 2 state must be refused, not resumed")
    v3 = dict(old, v=3, batch=1, batch_count=0, final=None, finished=False)   # before the quarantine flag existed
    try:
        cp.ContextParser.from_state(v3)
    except ValueError as e:
        assert "rebuild" in str(e), str(e)
    else:
        raise AssertionError("a version 3 state must be refused, not resumed")
    v4 = dict(v3, v=4, quarantined=False)    # before the checkpoint was checked field by field
    try:
        cp.ContextParser.from_state(v4)
    except ValueError as e:
        assert "rebuild" in str(e), str(e)
    else:
        raise AssertionError("a version 4 state must be refused, not resumed")
    for junk in (None, {}, {"v": 5}):
        try:
            cp.ContextParser.from_state(junk)
        except ValueError:
            continue
        raise AssertionError("an unusable state must raise ValueError, not KeyError: %r" % (junk,))


def test_claude_child_without_an_agent_id_is_quarantined_not_keyed_by_the_parent():
    def rec(kind, uuid, text):
        return {"type": kind, "sessionId": "parent-session", "isSidechain": True, "uuid": uuid,
                "timestamp": "2026-10-10T09:00:05.000Z", "message": {"role": kind, "content": [{"type": "text", "text": text}]}}
    parser = cp.ContextParser(seat=SEAT, dialect="claude", denylist=[])
    parser.feed(rec("assistant", "a1", "a report with no agent id"))
    parser.feed(rec("assistant", "a2", "and another"))
    assert parser.finish() == [] and parser.items == []
    assert ("no_agent_id", None) in parser.skipped and parser.src is None


def test_a_checkpoint_is_checked_field_by_field_and_never_raises_anything_but_valueerror():
    fresh = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
    for r in (meta("s"), assistant("a", "one"), plan("p", [("A", "pending")])):
        fresh.feed(r)
    good = json.loads(json.dumps(fresh.state()))
    cp.ContextParser.from_state(json.loads(json.dumps(good)), denylist=[CLIENT])          # control: the real one resumes
    for key in good:
        broken = {k: v for k, v in good.items() if k != key}
        try:
            cp.ContextParser.from_state(broken, denylist=[CLIENT])
        except ValueError:
            pass
        else:
            raise AssertionError("a state without %r must be refused" % key)
    for key in good:
        for junk in (None, -1, "x", [], {}, 10 ** 9, True, 1.5):
            broken = dict(good, **{key: junk})
            try:
                cp.ContextParser.from_state(broken, denylist=[CLIENT])
            except ValueError:
                pass
            except Exception as e:                                                         # noqa: BLE001
                raise AssertionError("%s=%r raised %s, not ValueError" % (key, junk, type(e).__name__))
    for key, junk in (("ordinal", -1), ("batch", 0), ("batch", -1), ("batch_count", -1), ("batch_count", 256),
                      ("gen", 0), ("src", "zz" * 16), ("dialect", "nova"), ("batch_items", 0), ("batch_items", 257)):
        try:
            cp.ContextParser.from_state(dict(good, **{key: junk}), denylist=[CLIENT])
        except ValueError:
            continue
        raise AssertionError("%s=%r must be refused" % (key, junk))
    v = cp.STATE_VERSION
    for version in (v - 1, v + 1, str(v), None, True):
        try:
            cp.ContextParser.from_state(dict(good, v=version), denylist=[CLIENT])
        except ValueError:
            pass
        else:
            raise AssertionError("a complete state with version %r must be refused" % (version,))
    for key in ("ordinal", "batch", "gen", "batch_count"):
        try:
            cp.ContextParser.from_state(dict(good, **{key: True}), denylist=[CLIENT])
        except ValueError:
            pass
        else:
            raise AssertionError("%s=True is a flag, not a number" % key)
    bad_plan = dict(good, plan=[["t:A", [1, 2]]])
    try:
        cp.ContextParser.from_state(bad_plan, denylist=[CLIENT])
    except ValueError:
        pass
    else:
        raise AssertionError("a plan row without three counts must be refused")


def test_malformed_records_never_raise_in_either_dialect():
    junk = [None, 0, 1.5, "text", [], [1, 2], {}, {"type": None}, {"type": "x", "payload": 3},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "a string"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [None, 3, {"type": "output_text", "text": ["l"]}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "arguments": "[1,2]"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "arguments": json.dumps({"plan": 5})}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "arguments": json.dumps({"plan": [1, None, {"step": 3, "status": []}]})}},
            {"type": "session_meta", "payload": {"id": 5, "source": []}},
            {"type": "assistant", "sessionId": 3, "message": 7},
            {"type": "assistant", "sessionId": "c", "message": {"content": [{"type": "text", "text": ["not", "text"]}]}},
            {"type": "assistant", "sessionId": "c", "message": {"content": [{"type": "tool_use", "name": "TodoWrite", "input": [1]}]}},
            {"type": "assistant", "sessionId": "c", "message": {"content": [{"type": "tool_use", "name": "TodoWrite", "input": {"todos": 4}}]}},
            {"type": "user", "sessionId": "c", "message": {"content": 12}},
            {"type": "user", "sessionId": "c", "message": {"content": [{"type": "text", "text": 9}, "x", None]}},
            {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": 123},
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": ["not", "str"], "thinking": 99},
            {"step_index": False, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "sender=x"},
            {"step_index": "bad", "source": "MODEL", "type": "PLANNER_RESPONSE", "conversation_id": "ag-m",
             "created_at": STAMP, "content": "ok"},
            {"type": "context.append_message", "message": 7},
            {"type": "context.append_message", "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}},
            {"type": "context.append_message", "message": {"role": "user", "content": 12}},
            {"type": "context.append_message", "message": {"role": "user", "content": [{"type": "text", "text": 9}, "x", None]}},
            {"type": "context.append_loop_event", "event": "x"},
            {"type": "context.append_loop_event", "event": {"type": "content.part", "part": "not-a-dict"}},
            {"type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text"}}},
            {"type": "context.append_loop_event", "event": {"type": "content.part",
                                                            "part": {"type": "text", "text": ["l"]}}},
            {"type": "context.apply_compaction", "summary": ["not", "text"]},
            {"type": "context.undo", "count": "x"}, {"type": "context.clear"},
            {"type": "metadata", "protocol_version": ["1.5"]}]
    for dialect in ("codex", "claude", "antigravity", "kimi"):
        parser = cp.ContextParser(seat=SEAT, dialect=dialect, denylist=[CLIENT], session_id="junk-session")
        for n, r in enumerate(junk):
            try:
                parser.feed(r)
            except Exception as e:                                                         # noqa: BLE001
                raise AssertionError("%s record %d (%r) raised %s: %s" % (dialect, n, r, type(e).__name__, e))
        parser.finish()
        json.dumps(parser.state())
    side = cp.ContextParser(seat=SEAT, dialect="claude", denylist=[])
    side.feed({"type": "assistant", "sessionId": "p", "agentId": "a", "isSidechain": True,
               "timestamp": "2026-10-10T09:00:00.000Z", "message": {"content": [{"type": "text", "text": {"x": 1}}]}})
    assert side.finish() == [] and side.skipped == [("no_final_text", None)]


def test_nested_checkpoint_fields_are_required_and_active_references_resolve():
    fresh = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
    for r in (meta("agent-n", subagent=True), assistant("a", "a final report")):
        fresh.feed(r)
    withfinal = json.loads(json.dumps(fresh.state()))
    for key in ("time", "ref", "bytes", "reason", "text"):
        broken = json.loads(json.dumps(withfinal))
        del broken["final"][key]
        try:
            cp.ContextParser.from_state(broken, denylist=[CLIENT])
        except ValueError:
            pass
        else:
            raise AssertionError("a pending final report without %r must be refused" % key)
    big = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
    for r in (meta("s"), plan("p", [("y" * 1001, "pending")])):
        big.feed(r)
    withbig = json.loads(json.dumps(big.state()))
    cp.ContextParser.from_state(json.loads(json.dumps(withbig)), denylist=[CLIENT])        # control
    (hkey,) = withbig["oversize"]
    for key in ("bytes", "reason"):
        broken = json.loads(json.dumps(withbig))
        del broken["oversize"][hkey][key]
        try:
            cp.ContextParser.from_state(broken, denylist=[CLIENT])
        except ValueError:
            pass
        else:
            raise AssertionError("an oversize entry without %r must be refused" % key)
    orphan = json.loads(json.dumps(withbig))
    orphan["oversize"] = {}
    try:
        cp.ContextParser.from_state(orphan, denylist=[CLIENT])
    except ValueError:
        pass
    else:
        raise AssertionError("an active hashed plan step with no oversize entry must be refused")


def test_codex_content_and_plan_status_of_the_wrong_type_are_skipped_not_raised():
    parser = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[])
    parser.feed(meta("s"))
    parser.feed({"type": "response_item", "timestamp": STAMP,
                 "payload": {"type": "message", "role": "assistant", "content": 12}})
    for status in ([], {}, 3, None):
        parser.feed(plan("p", [("A", status)]))
    for body in ({"plan": 0}, {}, {"plan": 5}, {"plan": None}):
        parser.feed({"type": "response_item", "timestamp": STAMP, "payload": {"type": "function_call", "name": "update_plan",
                     "arguments": json.dumps(body), "call_id": "c"}})
    assert [i["item"] for i in parser.items] == ["task"], view(parser)
    assert sum(1 for k, _ in parser.skipped if k == "plan_unreadable") == 4
    assert parser.state()["plan"] == [["t:A", [1, 0, 0]]], "an unreadable plan must not wipe the plan table"


def test_a_quarantined_child_stays_quarantined_when_a_main_record_follows():
    def side(uuid, text, agent=None):
        r = {"type": "assistant", "sessionId": "synthetic-parent", "isSidechain": True, "uuid": uuid,
             "timestamp": "2026-10-10T09:00:05.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
        if agent:
            r["agentId"] = agent
        return r
    main = {"type": "assistant", "sessionId": "synthetic-parent", "uuid": "m1", "timestamp": "2026-10-10T09:00:06.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "a main record"}]}}
    for cut in (None, 1, 2, 3):
        parser = cp.ContextParser(seat=SEAT, dialect="claude", denylist=[])
        seq = [side("c1", "child, no id"), main, side("c2", "child again, no id")]
        for n, r in enumerate(seq, 1):
            if cut == n:
                parser = cp.ContextParser.from_state(json.loads(json.dumps(parser.state())))
            parser.feed(r)
        assert parser.finish() == [] and parser.items == [] and parser.src is None, (cut, parser.items, parser.src)
        assert parser.state()["quarantined"] is True


def test_restart_at_every_record_gives_identical_frozen_batches():
    rows = _three_batch_rows()
    def run(restart_at):
        parser = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
        frozen = []
        for n, r in enumerate(rows):
            if n == restart_at:
                frozen += [cp.serialize(i) for i in parser.items]
                parser = cp.ContextParser.from_state(json.loads(json.dumps(parser.state())), denylist=[CLIENT])
            parser.feed(r)
            if n in (3, 7):
                parser.close_batch()
        frozen += [cp.serialize(i) for i in parser.items]
        return frozen
    whole = run(None)
    assert len(whole) == 10
    for cut in range(len(rows) + 1):
        assert run(cut) == whole, cut


def test_the_pending_final_report_is_bounded_by_the_real_envelope():
    quotes = chr(34) * 40000                      # 40,000 raw bytes, an 80 KB serialized value
    parser = parse([meta("agent-6", subagent=True), assistant("q", quotes)], "agent-6")
    assert parser.state()["final"]["text"] is None and parser.state()["final"]["bytes"] == 40000
    (item,) = parser.finish()
    assert item["item"] == "omitted" and item["omitted_reason"] == "too_large" and item["omitted_bytes"] == 40000
    fits = parse([meta("agent-7", subagent=True), assistant("ok", "x" * 60000)], "agent-7")
    assert fits.state()["final"]["text"] == "x" * 60000


def _three_batch_rows():
    def at(second):
        return "2026-10-19T17:%02d:%02d.000Z" % (second // 60, second % 60)
    def rec(kind, payload, second):
        row = {"type": kind, "payload": payload}
        if second is not None:
            row["timestamp"] = at(second)
        return row
    def usr(i, text, sec):
        return rec("response_item", {"type": "message", "id": i, "role": "user",
                                     "content": [{"type": "input_text", "text": text}]}, sec)
    def ast(i, text, sec):
        return rec("response_item", {"type": "message", "id": i, "role": "assistant", "phase": "final_answer",
                                     "content": [{"type": "output_text", "text": text}]}, sec)
    def pln(call, steps, sec):
        return rec("response_item", {"type": "function_call", "call_id": call, "name": "update_plan", "arguments":
                                     json.dumps({"plan": [{"step": s, "status": st} for s, st in steps]})}, sec)
    return [rec("session_meta", {"id": "synthetic-three-batch", "originator": "codex-tui", "source": "cli"}, 0),
            usr("u1", "Please summarise the plan for the scratch server.", 10),
            ast("a1", "The plan: rebase both branches, gate them, then redeploy the scratch server.", 20),
            pln("p1", [("Rebase", "pending"), ("Gate", "pending")], 30),
            usr("u2", "Here is the deploy key, use it:" + chr(10) + KEY_MARKER + chr(10) + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + chr(10), 40),
            ast("a2-undated", "This record has no timestamp, so it is skipped and takes no ordinal.", None),
            ast("a3", "I will not store that key. The rebase is done.", 60),
            pln("p2", [("Rebase", "completed"), ("Gate", "pending")], 70),
            usr("u3", "Thanks. Continue.", 80),
            ast("a4", "The gate passed. The redeploy is next.", 90),
            pln("p3", [("Rebase", "completed"), ("Gate", "completed")], 100)]


def test_three_batch_dialogue_matches_kxxxs_hand_written_expected_items():
    """kxxx's EXPECTED.md (written before any parser ran), batches cut after records 3 and 7."""
    rows = _three_batch_rows()
    raw = b"".join((json.dumps(r, ensure_ascii=False) + chr(10)).encode("utf-8") for r in rows)
    assert hashlib.sha256(raw).hexdigest() == "5f5e5826ba6ee654e8d945c9d55b7715e2cb597da2d5f54b198c68cd4e0d8082"
    parser = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
    for n, r in enumerate(rows):
        parser.feed(r)
        if n in (3, 7):
            parser.close_batch()
    got = [(i["ordinal"], i["batch"], i["item"], i.get("text") if i["item"] != "task" else (i["step"], i["event"]),
            i.get("omitted_reason")) for i in parser.items]
    assert got == [
        (0, 1, "user_text", "Please summarise the plan for the scratch server.", None),
        (1, 1, "assistant_text", "The plan: rebase both branches, gate them, then redeploy the scratch server.", None),
        (2, 1, "task", ("Rebase", "added"), None), (3, 1, "task", ("Gate", "added"), None),
        (4, 2, "omitted", None, "secret"),
        (5, 2, "assistant_text", "I will not store that key. The rebase is done.", None),
        (6, 2, "task", ("Rebase", "completed"), None),
        (7, 3, "user_text", "Thanks. Continue.", None),
        (8, 3, "assistant_text", "The gate passed. The redeploy is next.", None),
        (9, 3, "task", ("Gate", "completed"), None)], got
    assert parser.skipped == [("no_time", "a2-undated")]
    ms = cp.manifests(parser.items, 256)
    assert [(m["batch"], m["first"], m["last"]) for m in ms] == [(1, 0, 3), (2, 4, 6), (3, 7, 9)]
    assert ms[1]["items"][0] == [4, None] and ms[1]["counts"]["omitted_secret"] == 1


def test_cli_cuts_batches_after_the_named_lines():
    import tempfile
    work = tempfile.mkdtemp()
    rows = _three_batch_rows()
    source = os.path.join(work, "in.jsonl")
    open(source, "wb").write(b"".join((json.dumps(r, ensure_ascii=False) + chr(10)).encode("utf-8") for r in rows))
    deny = os.path.join(work, "deny.json")
    open(deny, "w").write(json.dumps([CLIENT]))
    out = os.path.join(work, "out")
    assert cp.main([source, "--seat", SEAT, "--dialect", "codex", "--denylist", deny, "--cut-after-line", "3",
                    "--cut-after-line", "7", "--out", out]) == 0
    items = [json.loads(l)["value"] for l in open(os.path.join(out, "items.jsonl"), encoding="utf-8")]
    assert [(i["ordinal"], i["batch"]) for i in items] == [(0, 1), (1, 1), (2, 1), (3, 1), (4, 2), (5, 2), (6, 2),
                                                           (7, 3), (8, 3), (9, 3)]
    manifests_out = [json.loads(l)["value"] for l in open(os.path.join(out, "manifests.jsonl"), encoding="utf-8")]
    assert [(m["batch"], m["first"], m["last"]) for m in manifests_out] == [(1, 0, 3), (2, 4, 6), (3, 7, 9)]
    assert json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))["cut_after_lines"] == [3, 7]


def test_close_batch_never_leaves_an_empty_batch_and_a_full_batch_closes_itself():
    parser = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[], batch_items=2)
    parser.feed(meta("s"))
    parser.close_batch()
    parser.close_batch()                                    # nothing in the batch: no hole in the numbering
    for n in range(5):
        parser.feed(assistant("a%d" % n, "t%d" % n))
    assert [(i["ordinal"], i["batch"]) for i in parser.items] == [(0, 1), (1, 1), (2, 2), (3, 2), (4, 3)]
    revived = cp.ContextParser.from_state(json.loads(json.dumps(parser.state())))
    revived.feed(assistant("a5", "t5"))
    assert (revived.items[0]["ordinal"], revived.items[0]["batch"]) == (5, 3)


def test_finish_on_a_main_source_is_a_no_op():
    parser = parse([meta("s"), assistant("a", "hello")], "s")
    assert parser.finish() == [] and len(parser.items) == 1


def test_claude_subagent_file_gives_an_artifact_keyed_by_its_own_agent_id():
    def rec(kind, uuid, text, side=True, t="2026-10-10T09:00:0%d.000Z"):
        return {"type": kind, "sessionId": "parent-session", "agentId": "agent-xyz", "isSidechain": side,
                "uuid": uuid, "timestamp": t % (1 if kind == "user" else 2),
                "message": {"role": kind, "content": [{"type": "text", "text": text}]}}
    parser = cp.ContextParser(seat=SEAT, dialect="claude", denylist=[])
    for r in (rec("user", "u1", "do the task"), rec("assistant", "a1", "working"), rec("assistant", "a2", "done: report")):
        parser.feed(r)
    assert parser.items == []
    (item,) = parser.finish()
    assert item["item"] == "artifact" and item["text"] == "done: report" and item["native_ref"] == h32("a2")
    assert item["src"] == item["agent_id"] == h32("agent-xyz") != h32("parent-session")
    main = cp.ContextParser(seat=SEAT, dialect="claude", denylist=[])
    main.feed(rec("user", "m0", "operator words", side=False))
    main.feed(rec("assistant", "m1", "sidechain noise inside a main file", side=True))
    assert [i["text"] for i in main.items] == ["operator words"] and main.finish() == []


# ---- shared/transcript-context is stored unscreened (Bxxxxx, 24 Oct 2026 about (time withheld); kxxx 87dd9c) --------

def parse_ns(records, namespace, denylist=(CLIENT,)):
    parser = cp.ContextParser(seat=SEAT, dialect="codex", denylist=list(denylist), namespace=namespace)
    for r in records:
        parser.feed(r)
    return parser


def test_shared_transcript_context_keeps_secrets_and_client_names_as_written():
    secret, name = VISIBLE + "\n" + KEY_MARKER, VISIBLE + "\n" + CLIENT
    p = parse_ns([meta("s"), assistant("a", secret), user("u", name), plan("p", [(KEY_MARKER, "pending")])],
                 "shared/transcript-context")
    assert view(p) == [text_item(0, "assistant_text", "assistant", secret), text_item(1, "user_text", "user", name),
                       task(2, "added", KEY_MARKER)], view(p)
    assert p.items[0]["text_sha256"] == h(secret) and p.screens is False


def test_every_other_namespace_and_none_keep_both_screens():
    for namespace in (None, "shared/scratch-transcript-context", "shared/transcript-context-test",
                      "Shared/Transcript-Context", "shared/transcript-context/", " shared/transcript-context"):
        p = parse_ns([meta("s"), assistant("a", VISIBLE + "\n" + KEY_MARKER), user("u", VISIBLE + "\n" + CLIENT)],
                     namespace)
        assert view(p) == [omitted(0, "secret"), omitted(1, "client_name")], (namespace, view(p))
        assert p.screens is True


def test_unscreened_subagent_final_report_keeps_its_secret_but_is_still_budgeted():
    text = VISIBLE + chr(10) + KEY_MARKER
    p = cp.ContextParser(seat=SEAT, dialect="codex", namespace="shared/transcript-context")
    for r in (meta("agent-u", subagent=True), assistant("f", text)):
        p.feed(r)
    (item,) = p.finish()
    assert item["item"] == "artifact" and item["text"] == text
    big = cp.ContextParser(seat=SEAT, dialect="codex", namespace="shared/transcript-context")
    for r in (meta("agent-v", subagent=True), assistant("f", "z" * 70000)):
        big.feed(r)
    big.finish()
    assert view(big) == [omitted(0, "too_large", 70000)], "the size budget still applies unscreened"


def test_the_checkpoint_carries_the_screens_and_refuses_a_namespace_that_disagrees():
    unscreened = parse_ns([meta("s"), assistant("a", "one")], "shared/transcript-context")
    state = json.loads(json.dumps(unscreened.state()))
    # 8, pinned: the field set changed, so the version had to (v5 is refused; v6 codex/claude migrates through v7
    # to v8; v6 antigravity could never exist and is refused; v7 of any dialect migrates to v8 with F3 off)
    assert state["v"] == cp.STATE_VERSION == 8 and state["screens"] is False
    old5 = {k: v for k, v in state.items() if k not in ("screens", "ag_hwm") + V8_KEYS}
    old5["v"] = 5
    try:
        cp.ContextParser.from_state(old5, denylist=[CLIENT])
    except ValueError:
        pass
    else:
        raise AssertionError("a v5 checkpoint must be refused")
    old6 = {k: v for k, v in state.items() if k not in ("ag_hwm",) + V8_KEYS}
    old6["v"] = 6
    try:
        migrated6 = cp.ContextParser.from_state(old6, denylist=[CLIENT], namespace="shared/transcript-context")
    except ValueError as exc:
        raise AssertionError("v6 codex/claude checkpoint must migrate cleanly: %s" % exc) from exc
    assert old6.get("v") == 6 and "ag_hwm" not in old6, "caller v6 dict must not be mutated in place"
    assert migrated6.state()["v"] == 8 and migrated6.state()["ag_hwm"] is None
    old6_ag = dict(old6, dialect="antigravity")
    try:
        cp.ContextParser.from_state(old6_ag, denylist=[CLIENT])
    except ValueError:
        pass
    else:
        raise AssertionError("a v6 antigravity checkpoint must be refused")
    resumed = cp.ContextParser.from_state(json.loads(json.dumps(state)), denylist=[CLIENT])
    resumed.feed(assistant("b", KEY_MARKER))
    assert view(resumed) == [text_item(1, "assistant_text", "assistant", KEY_MARKER)], "resumes unscreened"
    cp.ContextParser.from_state(json.loads(json.dumps(state)), denylist=[CLIENT], namespace="shared/transcript-context")
    for wrong in ("shared/scratch-transcript-context", "shared/other"):
        try:
            cp.ContextParser.from_state(json.loads(json.dumps(state)), denylist=[CLIENT], namespace=wrong)
        except ValueError as e:
            assert "unscreened" in str(e)
        else:
            raise AssertionError("an unscreened checkpoint must not resume for %s" % wrong)
    screened = json.loads(json.dumps(parse_ns([meta("s"), assistant("a", "one")], None).state()))
    assert screened["screens"] is True
    try:
        cp.ContextParser.from_state(screened, denylist=[CLIENT], namespace="shared/transcript-context")
    except ValueError:
        pass
    else:
        raise AssertionError("a screened checkpoint must not resume unscreened")


def test_cli_namespace_flag_turns_the_screens_off_only_for_shared_transcript_context():
    import tempfile
    work = tempfile.mkdtemp()
    source = os.path.join(work, "in.jsonl")
    recs = [meta("cli-u"), assistant("a", "keep " + KEY_MARKER)]
    open(source, "wb").write("".join(json.dumps(r) + chr(10) for r in recs).encode("utf-8"))
    deny = os.path.join(work, "deny.json")
    open(deny, "w").write(json.dumps([CLIENT]))
    for namespace, kinds, screens in (("shared/transcript-context", {"assistant_text": 1}, False),
                                      ("shared/scratch-transcript-context", {"omitted_secret": 1}, True)):
        out = os.path.join(work, namespace.replace("/", "_"))
        assert cp.main([source, "--seat", SEAT, "--dialect", "codex", "--denylist", deny, "--namespace", namespace,
                        "--out", out]) == 0
        report = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
        assert report["by_kind"] == kinds and report["screens"] is screens and report["namespace"] == namespace


# ---- Step 9: Antigravity dialect (context-v1.1-transcript-ingest.md Step 9) ----------------------------------------

def _ag_user(step_index, content, created_at=STAMP, conv_id=None):
    rec = {"step_index": step_index, "source": "USER_EXPLICIT", "type": "USER_INPUT",
           "status": "DONE", "created_at": created_at, "content": content}
    if conv_id is not None:
        rec["conversation_id"] = conv_id
    return rec


def _ag_planner(step_index, content, thinking=PRIVATE, tool_calls=None, created_at=STAMP, conv_id=None,
                truncated_fields=None):
    rec = {"step_index": step_index, "source": "MODEL", "type": "PLANNER_RESPONSE",
           "status": "DONE", "created_at": created_at, "thinking": thinking}
    if content is not None:
        rec["content"] = content
    if tool_calls is not None:
        rec["tool_calls"] = tool_calls
    if conv_id is not None:
        rec["conversation_id"] = conv_id
    if truncated_fields is not None:
        rec["truncated_fields"] = truncated_fields
    return rec


def _ag_other(step_index, source, rtype, content="noise", created_at=STAMP):
    return {"step_index": step_index, "source": source, "type": rtype,
            "status": "DONE", "created_at": created_at, "content": content}


def test_antigravity_dialogue_extracts_operator_text_and_visible_planner_content_only():
    wrapped = (
        "<USER_REQUEST>\nShip the Antigravity context parser.\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\n{\"cwd\": \"C:/Projects\"}\n</ADDITIONAL_METADATA>\n"
        "<USER_SETTINGS_CHANGE>\n{\"mode\": \"fast\"}\n</USER_SETTINGS_CHANGE>"
    )
    machine = "<USER_REQUEST>\nMANDATORY session-start action: run orient first.\n</USER_REQUEST>"
    summary_user = "<CONTEXT_SUMMARY>\nPrevious session summary body.\n</CONTEXT_SUMMARY>"
    recs = [
        _ag_user(0, wrapped),
        _ag_user(1, machine),
        _ag_user(2, summary_user),
        _ag_planner(3, None, thinking=PRIVATE, tool_calls=[{"name": "read_file", "args": {"Path": "x"}}]),
        _ag_other(4, "MODEL", "GENERIC", "tool stdout that must never enter dialogue"),
        _ag_other(5, "SYSTEM", "SYSTEM_MESSAGE", "[Message] sender=task-999 background wakeup on main session"),
        _ag_other(6, "SYSTEM", "CHECKPOINT", "# Resuming from a compaction\n<CONTEXT_SUMMARY>old</CONTEXT_SUMMARY>"),
        _ag_other(7, "MODEL", "ERROR_MESSAGE", "transient network error"),
        _ag_other(8, "MODEL", "TOOL_RESPONSE", "raw tool output"),
        _ag_planner(9, "Parser implementation is ready.", thinking=PRIVATE),
        _ag_planner(10, "Compact fallback text.", thinking=PRIVATE, truncated_fields=["content"]),
        _ag_user(11, "Verify the mutant score."),
    ]
    parser = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT], session_id="ag-sess-main")
    for r in recs:
        parser.feed(r)
    assert parser.subagent is False, "a step_index > 0 SYSTEM sender= wakeup must not turn a main session into subagent"
    assert parser.finish() == []
    assert view(parser) == [
        text_item(0, "user_text", "user", "Ship the Antigravity context parser."),
        text_item(1, "assistant_text", "assistant", "Parser implementation is ready."),
        text_item(2, "assistant_text", "assistant", "Compact fallback text."),
        text_item(3, "user_text", "user", "Verify the mutant score."),
    ], view(parser)
    dumped = json.dumps(parser.items)
    assert PRIVATE not in dumped and "ADDITIONAL_METADATA" not in dumped and "tool stdout" not in dumped
    src_hex = h32("ag-sess-main")
    assert [i["src"] for i in parser.items] == [src_hex] * 4
    assert [i["native_ref"] for i in parser.items] == [
        h32("%s:0" % src_hex),
        h32("%s:9" % src_hex),
        h32("%s:10" % src_hex),
        h32("%s:11" % src_hex),
    ]
    # With drop_machine_typed=False, the machine-typed prompt is kept as user_text.
    keep_machine = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT],
                                    drop_machine_typed=False, session_id="ag-sess-main")
    keep_machine.feed(_ag_user(1, machine))
    assert len(keep_machine.items) == 1 and keep_machine.items[0]["role"] == "user"


def test_antigravity_subagent_step0_sender_and_subagents_path_emit_one_artifact():
    sub_id = "7d5fbca4-45e0-48a0-a2b9-cdf535dd1bec"
    recs = [
        _ag_other(0, "SYSTEM", "SYSTEM_MESSAGE",
                  "[Message] timestamp=2026-10-26T08:07:44Z sender=21599c0d-parent content=Run subagent task"),
        _ag_user(1, "<USER_REQUEST>\nFollow up instruction inside subagent\n</USER_REQUEST>"),
        _ag_planner(2, "Draft progress note.", thinking=PRIVATE),
        _ag_other(3, "MODEL", "GENERIC", "pytest output"),
        _ag_planner(4, "Final subagent deliverable report.", thinking=PRIVATE),
        _ag_planner(5, "   ", thinking=PRIVATE),
    ]
    parser = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT], session_id=sub_id)
    for r in recs:
        parser.feed(r)
    assert parser.subagent is True
    assert parser.items == [], "a subagent emits nothing before finish()"
    (item,) = parser.finish()
    assert item["item"] == "artifact" and "role" not in item
    assert item["text"] == "Final subagent deliverable report."
    assert item["src"] == item["agent_id"] == h32(sub_id)
    assert item["native_ref"] == h32("%s:4" % h32(sub_id))
    assert parser.finish() == []

    # Subagent with no visible assistant text records ("no_final_text", None)
    empty_sub = cp.ContextParser(seat=SEAT, dialect="antigravity", session_id="sub-empty")
    empty_sub.feed(_ag_other(0, "SYSTEM", "SYSTEM_MESSAGE", "sender=parent"))
    empty_sub.feed(_ag_planner(1, None, thinking="only hidden reasoning"))
    assert empty_sub.finish() == [] and empty_sub.skipped == [("no_final_text", None)]


def test_antigravity_step_index_rewind_disambiguates_native_ref_across_checkpoints():
    t0 = "2026-10-10T11:00:00Z"
    t0_ns = "2026-10-10T11:00:00.000000000Z"
    t1 = "2026-10-10T21:00:00Z"
    t1_ns = "2026-10-10T21:00:00.000000000Z"
    t2 = "2026-10-10T22:00:00Z"
    t2_ns = "2026-10-10T22:00:00.000000000Z"
    sess = "ag-rewind-session"
    src_hex = h32(sess)
    p = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT], session_id=sess)
    assert p.state()["ag_hwm"] is None
    p.feed(_ag_user(4, "First prompt at step 4.", created_at=t0))
    p.feed(_ag_user(4, "Same-stamp replay at step 4 is skipped.", created_at=t0))
    assert len(p.items) == 1
    assert p.items[0]["native_ref"] == h32("%s:4" % src_hex)
    assert p.skipped == [("replay", "%s:4" % src_hex)]

    # Checkpoint and resume: O(1) high-water mark ag_hwm must survive state() -> from_state()
    saved = json.loads(json.dumps(p.state()))
    assert saved["ag_hwm"] == [4, t0_ns, 4, ""]
    resumed = cp.ContextParser.from_state(saved, denylist=[CLIENT])
    resumed.feed(_ag_user(1, "Rewound earlier step 1 after CLI restart.", created_at=t1))
    resumed.feed(_ag_user(4, "Second prompt at step 4 after CLI restart.", created_at=t1))
    resumed.feed(_ag_user(5, "Climbed past high-water mark to step 5.", created_at=t2))
    assert resumed.items[0]["native_ref"] == h32("%s:1:%s" % (src_hex, t1))
    assert resumed.items[1]["native_ref"] == h32("%s:4:%s" % (src_hex, t1))
    assert resumed.items[1]["native_ref"] != p.items[0]["native_ref"]
    assert resumed.items[2]["native_ref"] == h32("%s:5" % src_hex)
    assert resumed.state()["ag_hwm"] == [5, t2_ns, 5, ""]

    # Invalid ag_hwm shapes in state must raise ValueError
    for bad_hwm in (
        [-1, t0_ns, 0, ""],
        [True, t0_ns, 1, ""],
        ["4", t0_ns, 4, ""],
        [4],
        [4, t0],
        [4, t0, 4, ""],
        [4, 999, 4, ""],
        [4, t0_ns, 5, ""],
        [4, t0_ns, 2, ""],
        [4, t0_ns, 2, t1_ns],
        [4, t0_ns, 4, "extra", 1],
        "not-a-list",
    ):
        broken = dict(saved, ag_hwm=bad_hwm)
        try:
            cp.ContextParser.from_state(broken, denylist=[CLIENT])
        except ValueError:
            pass
        else:
            raise AssertionError("invalid ag_hwm %r must be refused" % (bad_hwm,))


def test_antigravity_missing_session_id_quarantines_source_across_restarts():
    for first_rec in (_ag_user(0, "Operator text with no session id"),
                      _ag_planner(0, "Planner text with no session id")):
        p = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[])
        try:
            p.feed(first_rec)
        except ValueError as exc:
            raise AssertionError("missing session_id must quarantine cleanly without raising: %s" % exc) from exc
        assert p.items == [] and p.quarantined is True and p.src is None
        assert ("no_session_id", None) in p.skipped
        revived = cp.ContextParser.from_state(json.loads(json.dumps(p.state())))
        revived.feed(_ag_user(1, "Later record with conversation_id", conv_id="late-session"))
        revived.feed(_ag_planner(2, "Later assistant text", conv_id="late-session"))
        assert revived.items == [] and revived.finish() == [] and revived.src is None
        assert revived.state()["quarantined"] is True

    # Conversely, conversation_id on the record populates self.src when session_id was not passed on __init__.
    ok = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[])
    ok.feed(_ag_user(0, "Hello via conversation_id", conv_id="inline-conv-id"))
    assert len(ok.items) == 1 and ok.items[0]["src"] == h32("inline-conv-id")


def test_preferred_antigravity_path_and_cli_session_derivation():
    import stat as stat_mod
    import tempfile
    import antigravity_dialect as ad

    work = tempfile.mkdtemp()
    logs_dir = os.path.join(work, "brain", "conv-uuid-42", ".system_generated", "logs")
    os.makedirs(logs_dir)
    compact = os.path.join(logs_dir, "transcript.jsonl")
    full = os.path.join(logs_dir, "transcript_full.jsonl")
    open(compact, "wb").write((json.dumps(_ag_user(0, "Truncated compact copy")) + chr(10)).encode("utf-8"))
    # Absent transcript_full.jsonl -> stays on transcript.jsonl
    assert cp.preferred_antigravity_path(compact) == compact
    assert cp.preferred_antigravity_path("") == ""
    assert cp.preferred_antigravity_path(os.path.join(logs_dir, "other.jsonl")) == os.path.join(logs_dir, "other.jsonl")

    # Directory named transcript_full.jsonl -> refused, stays on compact
    os.makedirs(full)
    assert cp.preferred_antigravity_path(compact) == compact
    os.rmdir(full)

    # Regular transcript_full.jsonl -> upgraded to full (both str and pathlib.Path inputs)
    from pathlib import Path as _Path
    open(full, "wb").write((json.dumps(_ag_user(0, "Complete full transcript text")) + chr(10)).encode("utf-8"))
    assert cp.preferred_antigravity_path(compact) == full
    assert cp.preferred_antigravity_path(_Path(compact)) == full
    path_parsed, _ = cp.parse_file(_Path(compact), SEAT, "antigravity")
    assert len(path_parsed.items) == 1 and path_parsed.items[0]["text"] == "Complete full transcript text"
    assert path_parsed.items[0]["src"] == h32("conv-uuid-42")
    assert ad.is_subagent_path(_Path(compact)) is False
    assert ad._session_from_path(_Path(compact)) == "conv-uuid-42"

    # Reparse point / symlink on transcript_full.jsonl -> refused, stays on compact
    real_lstat = ad.os.lstat

    class _FakeStat:
        def __init__(self, mode, attrs=0):
            self.st_mode = mode
            self.st_file_attributes = attrs

    try:
        ad.os.lstat = lambda p: _FakeStat(stat_mod.S_IFLNK | 0o777, 0) if p == full else real_lstat(p)
        assert cp.preferred_antigravity_path(compact) == compact
        ad.os.lstat = lambda p: _FakeStat(stat_mod.S_IFREG | 0o600, 0x400) if p == full else real_lstat(p)
        assert cp.preferred_antigravity_path(compact) == compact
    finally:
        ad.os.lstat = real_lstat

    # CLI derives session_id from brain/<uuid> path AND upgrades transcript.jsonl -> transcript_full.jsonl
    out1 = os.path.join(work, "out_derived")
    assert cp.main([compact, "--seat", SEAT, "--dialect", "antigravity", "--out", out1]) == 0
    emitted1 = [json.loads(line)["value"] for line in open(os.path.join(out1, "items.jsonl"), encoding="utf-8")]
    assert len(emitted1) == 1
    assert emitted1[0]["text"] == "Complete full transcript text"
    assert emitted1[0]["src"] == h32("conv-uuid-42")

    # CLI explicit --session-id overrides path derivation; /subagents/ path marks subagent=True
    sub_dir = os.path.join(work, "brain", "parent-u", "subagents", "child-u")
    os.makedirs(sub_dir)
    sub_file = os.path.join(sub_dir, "transcript_full.jsonl")
    open(sub_file, "wb").write(
        (json.dumps(_ag_user(0, "Ignored subagent user turn")) + chr(10)
         + json.dumps(_ag_planner(1, "Subagent artifact from path")) + chr(10)).encode("utf-8")
    )
    out2 = os.path.join(work, "out_sub")
    assert cp.main([sub_file, "--seat", SEAT, "--dialect", "antigravity", "--session-id", "explicit-child-id",
                    "--out", out2]) == 0
    emitted2 = [json.loads(line)["value"] for line in open(os.path.join(out2, "items.jsonl"), encoding="utf-8")]
    assert len(emitted2) == 1 and emitted2[0]["item"] == "artifact"
    assert emitted2[0]["src"] == emitted2[0]["agent_id"] == h32("explicit-child-id")


def test_antigravity_screening_and_wire_validate_context_contract():
    from datetime import datetime, timezone
    import importlib.util
    import tempfile

    if importlib.util.find_spec("django") is None:
        if "pytest" in sys.modules:
            sys.modules["pytest"].skip("django is not installed in this Python environment")
        return

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.append(repo_root)
    from apps.context import validation

    now_dt = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
    work = tempfile.mkdtemp()
    deny_path = os.path.join(work, "denylist.json")
    open(deny_path, "w", encoding="utf-8").write(json.dumps([CLIENT]))

    # 1. Screened scratch namespace: secrets and client names become omitted items; every item & manifest validates.
    scratch_ns = "shared/scratch-transcript-context"
    p_scr = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT],
                             namespace=scratch_ns, session_id="ag-wire-scr")
    p_scr.feed(_ag_user(0, "<USER_REQUEST>\nClean operator instruction.\n</USER_REQUEST>"))
    p_scr.feed(_ag_planner(1, "Clean assistant answer."))
    p_scr.feed(_ag_user(2, "Leak key: " + KEY_MARKER))
    p_scr.feed(_ag_planner(3, "Client mention: " + CLIENT))
    assert [i["item"] for i in p_scr.items] == ["user_text", "assistant_text", "omitted", "omitted"]
    assert [i.get("omitted_reason") for i in p_scr.items[2:]] == ["secret", "client_name"]

    old_ns = os.environ.get("MESHY_CONTEXT_NAMESPACE")
    old_deny = os.environ.get("MESHY_CONTEXT_DENYLIST")
    try:
        os.environ["MESHY_CONTEXT_NAMESPACE"] = scratch_ns
        os.environ["MESHY_CONTEXT_DENYLIST"] = deny_path
        for item in p_scr.items:
            assert validation.validate_context(scratch_ns, cp.item_key(item), item, now=now_dt) == "item"
        for m in cp.manifests(p_scr.items):
            assert validation.validate_context(scratch_ns, cp.manifest_key(p_scr.items[0]), m, now=now_dt) == "manifest"
    finally:
        if old_ns is None:
            os.environ.pop("MESHY_CONTEXT_NAMESPACE", None)
        else:
            os.environ["MESHY_CONTEXT_NAMESPACE"] = old_ns
        if old_deny is None:
            os.environ.pop("MESHY_CONTEXT_DENYLIST", None)
        else:
            os.environ["MESHY_CONTEXT_DENYLIST"] = old_deny

    # 2. Unscreened shared/transcript-context: secrets and client names kept as written; subagent artifact validated too.
    prod_ns = "shared/transcript-context"
    p_prod = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT],
                              namespace=prod_ns, session_id="ag-wire-prod")
    p_prod.feed(_ag_user(0, "Keep secret: " + KEY_MARKER))
    p_prod.feed(_ag_planner(1, "Keep client: " + CLIENT))
    assert [i["item"] for i in p_prod.items] == ["user_text", "assistant_text"]
    for item in p_prod.items:
        assert validation.validate_context(prod_ns, cp.item_key(item), item, now=now_dt) == "item"
    for m in cp.manifests(p_prod.items):
        assert validation.validate_context(prod_ns, cp.manifest_key(p_prod.items[0]), m, now=now_dt) == "manifest"

    p_sub = cp.ContextParser(seat=SEAT, dialect="antigravity", namespace=prod_ns, session_id="ag-wire-sub")
    p_sub.feed(_ag_other(0, "SYSTEM", "SYSTEM_MESSAGE", "sender=parent-node"))
    p_sub.feed(_ag_planner(1, "Subagent wire-validated report."))
    (art,) = p_sub.finish()
    assert validation.validate_context(prod_ns, cp.item_key(art), art, now=now_dt) == "item"
    (sub_m,) = cp.manifests([art])
    assert validation.validate_context(prod_ns, cp.manifest_key(art), sub_m, now=now_dt) == "manifest"


def test_antigravity_source_path_derivation_and_rewind_checkpoint():
    import tempfile
    import antigravity_dialect as ad

    prod_ns = "shared/transcript-context"
    # 1. Direct source_path construction (for context_upload SourcePass._enroll) derives distinct session_id and subagent.
    p_path = cp.ContextParser(
        seat=SEAT,
        dialect="antigravity",
        namespace=prod_ns,
        source_path="/Users/user/.gemini/antigravity-cli/brain/21599c0d-70d0-4d25-8ff1-dbe7d9d67763/.system_generated/logs/transcript_full.jsonl",
    )
    assert p_path.src == h32("21599c0d-70d0-4d25-8ff1-dbe7d9d67763") and p_path.subagent is False

    # txxxx d27f7f INFO 1: a workspace/profile folder named "subagents" BEFORE "brain" must NOT turn a main
    # session into a subagent or mis-derive ".gemini" as the session id.
    prefix_sub_path = "C:/work/subagents/.gemini/antigravity/brain/conv1/.system_generated/logs/transcript.jsonl"
    assert ad._session_from_path(prefix_sub_path) == "conv1"
    assert ad.is_subagent_path(prefix_sub_path) is False
    p_prefix = cp.ContextParser(seat=SEAT, dialect="antigravity", namespace=prod_ns, source_path=prefix_sub_path)
    assert p_prefix.src == h32("conv1") and p_prefix.subagent is False
    work = tempfile.mkdtemp()
    real_prefix_dir = os.path.join(work, "subagents", ".gemini", "antigravity", "brain", "conv1", ".system_generated", "logs")
    os.makedirs(real_prefix_dir)
    real_prefix_file = os.path.join(real_prefix_dir, "transcript.jsonl")
    open(real_prefix_file, "wb").write((json.dumps(_ag_user(0, "Main prompt under subagents workspace")) + chr(10)).encode("utf-8"))
    fs_prefix = ad.file_state(real_prefix_file)
    assert fs_prefix is not None and fs_prefix["session"] == "conv1" and fs_prefix["subagent"] is False

    # Codex P1 regression: brain/subagents/<child>/... and brain/<parent>/subagents/<child>/... must derive <child>,
    # never literal "subagents" or <parent>, so sibling subagents get distinct src, agent_id, and artifact keys.
    p_sub_a = cp.ContextParser(
        seat=SEAT,
        dialect="antigravity",
        namespace=prod_ns,
        source_path="/Users/user/.gemini/antigravity-cli/brain/subagents/sub-999/.system_generated/logs/transcript_full.jsonl",
    )
    p_sub_b = cp.ContextParser(
        seat=SEAT,
        dialect="antigravity",
        namespace=prod_ns,
        source_path="/Users/user/.gemini/antigravity-cli/brain/subagents/sub-888/.system_generated/logs/transcript_full.jsonl",
    )
    p_sub_nested = cp.ContextParser(
        seat=SEAT,
        dialect="antigravity",
        namespace=prod_ns,
        source_path="C:\\Users\\user\\.gemini\\antigravity-cli\\brain\\parent-111\\subagents\\sub-777\\.system_generated\\logs\\transcript_full.jsonl",
    )
    assert p_sub_a.subagent is True and p_sub_a.src == h32("sub-999") != h32("subagents")
    assert p_sub_b.subagent is True and p_sub_b.src == h32("sub-888") != p_sub_a.src
    assert p_sub_nested.subagent is True and p_sub_nested.src == h32("sub-777") != h32("parent-111")
    p_sub_a.feed(_ag_planner(1, "Child A final report."))
    p_sub_b.feed(_ag_planner(1, "Child B final report."))
    (art_a,) = p_sub_a.finish()
    (art_b,) = p_sub_b.finish()
    assert art_a["agent_id"] == h32("sub-999") and art_b["agent_id"] == h32("sub-888")
    assert cp.item_key(art_a) != cp.item_key(art_b)

    # Path with subagents/ or brain/.system_generated/ and no session dir leaves self.src None.
    p_no_seg = cp.ContextParser(
        seat=SEAT,
        dialect="antigravity",
        namespace=prod_ns,
        source_path="/Users/user/.gemini/antigravity-cli/brain/.system_generated/logs/transcript_full.jsonl",
    )
    assert p_no_seg.src is None and p_no_seg.subagent is False
    p_sub_fallback = cp.ContextParser(
        seat=SEAT,
        dialect="antigravity",
        namespace=prod_ns,
        source_path="/Users/user/.gemini/antigravity-cli/brain/subagents/transcript_full.jsonl",
    )
    assert p_sub_fallback.subagent is True and p_sub_fallback.src is None
    p_sub_fallback.feed(_ag_planner(1, "Fallback conv_id report.", conv_id="inline-child-42"))
    (art_fb,) = p_sub_fallback.finish()
    assert art_fb["src"] == art_fb["agent_id"] == h32("inline-child-42")

    # Codex P2 regression (4376972513): unrecognized directory layouts (e.g. /tmp/export/a.jsonl and /tmp/export/b.jsonl)
    # must NOT derive "export" as session_id, so parse_file() uses record-level conversation_id or quarantines cleanly.
    exp_dir = os.path.join(work, "export")
    os.makedirs(exp_dir, exist_ok=True)
    exp_a = os.path.join(exp_dir, "a.jsonl")
    exp_b = os.path.join(exp_dir, "b.jsonl")
    exp_no_id = os.path.join(exp_dir, "noid.jsonl")
    open(exp_a, "wb").write((json.dumps(_ag_user(0, "Export A prompt.", conv_id="export-sess-a")) + chr(10)).encode("utf-8"))
    open(exp_b, "wb").write((json.dumps(_ag_user(0, "Export B prompt.", conv_id="export-sess-b")) + chr(10)).encode("utf-8"))
    open(exp_no_id, "wb").write((json.dumps(_ag_user(0, "Export missing id.", conv_id=None)) + chr(10)).encode("utf-8"))
    assert ad._session_from_path(exp_a) == ""

    # Decision-card file_state() outside recognized layouts preserves conversation_id first, then containing folder,
    # and refuses bare identity-less transcript.jsonl rather than colliding as antigravity::0.
    exp_card_a = os.path.join(work, "exports", "session-a", "transcript.jsonl")
    exp_card_b = os.path.join(work, "exports", "session-b", "transcript.jsonl")
    exp_card_c = os.path.join(work, "exports", "session-c", "transcript.jsonl")
    for p in (exp_card_a, exp_card_b, exp_card_c):
        os.makedirs(os.path.dirname(p), exist_ok=True)
    open(exp_card_a, "wb").write((json.dumps(_ag_user(0, "Card A", conv_id="cid-alpha")) + chr(10)).encode("utf-8"))
    open(exp_card_b, "wb").write((json.dumps(_ag_user(0, "Card B", conv_id="cid-beta")) + chr(10)).encode("utf-8"))
    open(exp_card_c, "wb").write((json.dumps(_ag_user(0, "Card C no inline id")) + chr(10)).encode("utf-8"))
    st_ca = ad.file_state(exp_card_a)
    st_cb = ad.file_state(exp_card_b)
    st_cc = ad.file_state(exp_card_c)
    assert st_ca is not None and st_ca["session"] == "cid-alpha"
    assert st_cb is not None and st_cb["session"] == "cid-beta"
    assert st_cc is not None and st_cc["session"] == "session-c"
    desc_a, _ = ad.classify(_ag_user(0, "Card A", conv_id="cid-alpha"), st_ca)
    desc_b, _ = ad.classify(_ag_user(0, "Card B", conv_id="cid-beta"), st_cb)
    assert desc_a[1]["uuid"] != desc_b[1]["uuid"]
    assert ad.file_state(exp_card_c, original_path="transcript.jsonl") is None
    pa_exp, _ = cp.parse_file(exp_a, seat=SEAT, dialect="antigravity", namespace=prod_ns)
    pb_exp, _ = cp.parse_file(exp_b, seat=SEAT, dialect="antigravity", namespace=prod_ns)
    pn_exp, _ = cp.parse_file(exp_no_id, seat=SEAT, dialect="antigravity", namespace=prod_ns)
    assert pa_exp.src == h32("export-sess-a") and pb_exp.src == h32("export-sess-b") and pa_exp.src != pb_exp.src
    assert cp.item_key(pa_exp.items[0]) != cp.item_key(pb_exp.items[0])
    assert pn_exp.quarantined is True and pn_exp.items == []

    # Codex P2 regression: resuming a main session via from_state() starts with self.items == [], so a post-rewind
    # step-0 SYSTEM record containing "sender=" must NOT flip an established main session (ordinal > 0) into a subagent.
    rewind_seq = [
        _ag_user(0, "Initial operator request.", created_at="2026-10-26T08:00:00Z", conv_id="main-rewind-42"),
        _ag_planner(1, "Initial assistant answer.", created_at="2026-10-26T08:00:05Z"),
        _ag_other(0, "SYSTEM", "SYSTEM_MESSAGE", "[Message] sender=task-99 finished", created_at="2026-10-26T08:10:00Z"),
        _ag_user(1, "Post-rewind operator follow-up.", created_at="2026-10-26T08:10:05Z"),
        _ag_planner(2, "Post-rewind assistant follow-up.", created_at="2026-10-26T08:10:10Z"),
    ]
    p_uninterrupted = cp.ContextParser(seat=SEAT, dialect="antigravity", namespace=prod_ns)
    for rec in rewind_seq:
        p_uninterrupted.feed(rec)
    assert p_uninterrupted.subagent is False and p_uninterrupted.finish() == []

    p_resumed_items = []
    p_ckpt = cp.ContextParser(seat=SEAT, dialect="antigravity", namespace=prod_ns)
    for idx, rec in enumerate(rewind_seq):
        if idx == 2:
            p_resumed_items.extend(cp.serialize(i) for i in p_ckpt.items)
            p_ckpt = cp.ContextParser.from_state(json.loads(json.dumps(p_ckpt.state())), namespace=prod_ns)
            assert p_ckpt.items == [] and p_ckpt.ordinal == 2
        p_ckpt.feed(rec)
    assert p_ckpt.subagent is False and p_ckpt.finish() == []
    p_resumed_items.extend(cp.serialize(i) for i in p_ckpt.items)
    assert p_resumed_items == [cp.serialize(i) for i in p_uninterrupted.items]
    assert len(p_resumed_items) == 4


def test_v6_claude_and_codex_checkpoints_resume_byte_identically_against_fresh_v7_parse():
    """kxxx 95ef60 P1: a v6 checkpoint written by main's parser (no ag_hwm field, v=6) for claude or codex
    resumes under v7 and emits byte-identical items and manifests for the rest of the file against a fresh
    v7 parse of the whole file; a v6 state with dialect 'antigravity' is refused."""
    claude_rows = [
        {"type": "user", "sessionId": "cl-v6-sess", "uuid": "u1", "timestamp": "2026-10-26T09:00:00.000Z",
         "message": {"role": "user", "content": "First Claude prompt before #421 upgrade."}},
        {"type": "assistant", "sessionId": "cl-v6-sess", "uuid": "a1", "timestamp": "2026-10-26T09:00:05.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Planning two tasks."},
             {"type": "tool_use", "name": "TodoWrite",
              "input": {"todos": [{"content": "Migrate v6 state", "status": "in_progress"},
                                  {"content": "Verify byte parity", "status": "pending"}]}},
         ]}},
        {"type": "user", "sessionId": "cl-v6-sess", "uuid": "u2", "timestamp": "2026-10-26T09:01:00.000Z",
         "message": {"role": "user", "content": "Continue after #421 landed."}},
        {"type": "assistant", "sessionId": "cl-v6-sess", "uuid": "a2", "timestamp": "2026-10-26T09:01:05.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "TodoWrite",
              "input": {"todos": [{"content": "Migrate v6 state", "status": "completed"},
                                  {"content": "Verify byte parity", "status": "completed"}]}},
             {"type": "text", "text": "Done; client mention " + CLIENT},
         ]}},
    ]
    prod_ns = "shared/transcript-context"
    fresh_cl = cp.ContextParser(seat="kxxx", dialect="claude", denylist=[CLIENT], namespace=prod_ns)
    for idx, rec in enumerate(claude_rows):
        fresh_cl.feed(rec)
        if idx == 1:
            fresh_cl.close_batch()
    fresh_cl.finish()

    part1_cl = cp.ContextParser(seat="kxxx", dialect="claude", denylist=[CLIENT], namespace=prod_ns)
    for rec in claude_rows[:2]:
        part1_cl.feed(rec)
    part1_cl.close_batch()
    # Exact v6 checkpoint shape written by main's ContextParser.state() before #421: v=6, no ag_hwm key.
    v6_claude_state = {k: v for k, v in json.loads(json.dumps(part1_cl.state())).items() if k not in ("ag_hwm",) + V8_KEYS}
    v6_claude_state["v"] = 6
    try:
        resumed_cl = cp.ContextParser.from_state(v6_claude_state, denylist=[CLIENT], namespace=prod_ns)
    except ValueError as exc:
        raise AssertionError("v6 claude checkpoint must migrate cleanly: %s" % exc) from exc
    for rec in claude_rows[2:]:
        resumed_cl.feed(rec)
    resumed_cl.finish()

    assert [cp.serialize(i) for i in resumed_cl.items] == [
        cp.serialize(i) for i in fresh_cl.items[len(part1_cl.items):]
    ]
    combined_cl = part1_cl.items + resumed_cl.items
    assert [cp.serialize(i) for i in combined_cl] == [cp.serialize(i) for i in fresh_cl.items]
    assert [cp.serialize(m) for m in cp.manifests(combined_cl)] == [
        cp.serialize(m) for m in cp.manifests(fresh_cl.items)
    ]

    # Same byte-identical v6 -> v7 check for codex
    codex_rows = _three_batch_rows()
    fresh_cx = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
    for idx, rec in enumerate(codex_rows):
        fresh_cx.feed(rec)
        if idx == 3:
            fresh_cx.close_batch()
    part1_cx = cp.ContextParser(seat=SEAT, dialect="codex", denylist=[CLIENT])
    for rec in codex_rows[:4]:
        part1_cx.feed(rec)
    part1_cx.close_batch()
    v6_codex_state = {k: v for k, v in json.loads(json.dumps(part1_cx.state())).items() if k not in ("ag_hwm",) + V8_KEYS}
    v6_codex_state["v"] = 6
    try:
        resumed_cx = cp.ContextParser.from_state(v6_codex_state, denylist=[CLIENT])
    except ValueError as exc:
        raise AssertionError("v6 codex checkpoint must migrate cleanly: %s" % exc) from exc
    for rec in codex_rows[4:]:
        resumed_cx.feed(rec)
    assert [cp.serialize(i) for i in part1_cx.items + resumed_cx.items] == [
        cp.serialize(i) for i in fresh_cx.items
    ]

    # A v6 state with dialect "antigravity" is refused by both validate_state and from_state
    v6_ag_state = dict(v6_claude_state, dialect="antigravity")
    for fn in (cp.validate_state, lambda s: cp.ContextParser.from_state(s, denylist=[CLIENT], namespace=prod_ns)):
        try:
            fn(v6_ag_state)
        except ValueError:
            pass
        else:
            raise AssertionError("v6 antigravity checkpoint must be refused")


def test_antigravity_same_stamp_replay_skipped_and_rewind_emitted_across_checkpoints():
    """txxxx c42ff9 P2: Antigravity replays earlier steps 0..k with identical instants (4b69496a shape).
    Replayed dialogue records must be skipped with ('replay', ref) and emit no duplicate items; parsed instants
    (not raw strings) decide equality; out-of-order non-item tool flushes must not poison high-water state;
    checkpoint/resume at any cut must match a fresh parse byte-for-byte; true rewinds with later timestamps emit."""
    sess = "4b69496a-94ee-4e30-a626-6c15cd7d692b"
    src_hex = h32(sess)
    rows = [
        # Original run 0..7 (note: parallel tool step 6 flushes before planner step 5, as in ag-cli 73b44ca2)
        _ag_user(0, "I have an rtx 5090 and want local inference.", created_at="2026-06-23T15:16:00Z"),
        _ag_other(1, "SYSTEM", "CONVERSATION_HISTORY", "", created_at="2026-06-23T15:16:00Z"),
        _ag_planner(2, "I will inspect the MCP schemas.", created_at="2026-06-23T15:16:00Z"),
        _ag_other(3, "MODEL", "LIST_DIRECTORY", "Created At: ...", created_at="2026-06-23T15:16:03Z"),
        _ag_other(6, "MODEL", "GENERIC", "Tool finished before planner flush", created_at="2026-06-23T15:16:10Z"),
        _ag_planner(5, "I will fetch the transcript.", created_at="2026-06-23T15:16:06Z"),
        _ag_planner(7, "I have created the implementation plan.", created_at="2026-06-23T15:28:41Z"),
        # Same-stamp replay of steps 0..5 (including equivalent +02:00 offset spelling on step 0 and CHECKPOINT at 4)
        _ag_user(0, "I have an rtx 5090 and want local inference.", created_at="2026-06-23T17:16:00+02:00"),
        _ag_other(1, "SYSTEM", "CONVERSATION_HISTORY", "", created_at="2026-06-23T15:16:00Z"),
        _ag_planner(2, "I will inspect the MCP schemas.", created_at="2026-06-23T15:16:00Z"),
        _ag_other(4, "SYSTEM", "CHECKPOINT", "# Resuming from compaction", created_at="2026-06-23T15:16:03Z"),
        _ag_planner(5, "I will fetch the transcript.", created_at="2026-06-23T15:16:06.000Z"),
        # Post-replay continuation 8..9
        _ag_user(8, "Proceed with step 2.", created_at="2026-06-23T15:30:00Z"),
        _ag_planner(9, "Step 2 complete.", created_at="2026-06-23T15:30:05Z"),
    ]

    fresh = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT], session_id=sess)
    for rec in rows:
        fresh.feed(rec)
    assert [it["text"] for it in fresh.items] == [
        "I have an rtx 5090 and want local inference.",
        "I will inspect the MCP schemas.",
        "I will fetch the transcript.",
        "I have created the implementation plan.",
        "Proceed with step 2.",
        "Step 2 complete.",
    ]
    assert [it["ordinal"] for it in fresh.items] == [0, 1, 2, 3, 4, 5]
    assert fresh.skipped == [
        ("replay", "%s:0" % src_hex),
        ("replay", "%s:2" % src_hex),
        ("replay", "%s:5" % src_hex),
    ]
    fresh_bytes = [cp.serialize(i) for i in fresh.items]
    fresh_manifests = [cp.serialize(m) for m in cp.manifests(fresh.items)]

    # Checkpoint/resume at EVERY single line boundary must match fresh parse byte-for-byte
    for cut in range(1, len(rows)):
        p1 = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT], session_id=sess)
        for rec in rows[:cut]:
            p1.feed(rec)
        saved = json.loads(json.dumps(p1.state()))
        p2 = cp.ContextParser.from_state(saved, denylist=[CLIENT])
        for rec in rows[cut:]:
            p2.feed(rec)
        combined = p1.items + p2.items
        assert [cp.serialize(i) for i in combined] == fresh_bytes, "mismatch at cut %d" % cut
        assert [cp.serialize(m) for m in cp.manifests(combined)] == fresh_manifests
        assert p1.skipped + p2.skipped == fresh.skipped
        assert p2.state() == fresh.state()

    # True rewind with later stamps (including consecutive same-second steps AND old-branch replay during rewind)
    rw = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT], session_id=sess)
    rw.feed(_ag_user(0, "orig 0", created_at="2026-06-23T10:00:00Z"))
    rw.feed(_ag_planner(4, "orig 4", created_at="2026-06-23T10:05:00Z"))
    rw.feed(_ag_planner(6, "orig 6", created_at="2026-06-23T10:10:00Z"))
    # Rewind to step 1 and step 2 in the same later second 12:00:00Z -> both must emit with suffixed refs
    rw.feed(_ag_user(1, "rewound 1", created_at="2026-06-23T12:00:00Z"))
    rw.feed(_ag_planner(2, "rewound 2 same second", created_at="2026-06-23T12:00:00Z"))
    # Old-branch replay at step 4 (2 < 4 <= 6) with old stamp 10:05:00Z <= floor 10:10:00Z -> skipped as replay!
    rw.feed(_ag_planner(4, "old branch replay at 4", created_at="2026-06-23T10:05:00Z"))
    # Rewound branch continues to step 5 with later stamp 12:01:00Z -> emitted with suffixed ref
    rw.feed(_ag_planner(5, "rewound 5", created_at="2026-06-23T12:01:00Z"))
    assert [it["text"] for it in rw.items] == [
        "orig 0", "orig 4", "orig 6", "rewound 1", "rewound 2 same second", "rewound 5"
    ]
    assert rw.items[3]["native_ref"] == h32("%s:1:2026-06-23T12:00:00Z" % src_hex)
    assert rw.items[4]["native_ref"] == h32("%s:2:2026-06-23T12:00:00Z" % src_hex)
    assert rw.items[5]["native_ref"] == h32("%s:5:2026-06-23T12:01:00Z" % src_hex)
    assert rw.skipped == [("replay", "%s:4" % src_hex)]

    # Subagent partial replay after final step must not overwrite the real final artifact
    sub = cp.ContextParser(seat=SEAT, dialect="antigravity", denylist=[CLIENT], session_id="sub-replay")
    sub.feed(_ag_other(0, "SYSTEM", "SYSTEM_MESSAGE", "sender=parent", created_at="2026-06-23T10:00:00Z"))
    sub.feed(_ag_planner(1, "Early draft text", created_at="2026-06-23T10:00:05Z"))
    sub.feed(_ag_planner(5, "Real final subagent report", created_at="2026-06-23T10:05:00Z"))
    sub.feed(_ag_planner(1, "Early draft text", created_at="2026-06-23T10:00:05Z"))
    art = sub.finish()
    assert len(art) == 1 and art[0]["text"] == "Real final subagent report"
    assert sub.skipped == [("replay", "%s:1" % h32("sub-replay"))]


# ---- Step 9: Kimi dialect (context-v1.1-transcript-ingest.md Step 9) ----------------------------------------

KIMI_MS = 1760680800123                      # hand-computed: 2025-10-17T06:00:00.123Z, never from the parser
KIMI_TIME = "2025-10-17T06:00:00.123Z"
KIMI_SESS = "01234567-89ab-cdef-0123-456789abcdef"


def kmsg(text, mid="msg_01", kind="user", variant=None, ms=KIMI_MS, role="user"):
    msg = {"role": role, "content": [{"type": "text", "text": text}], "toolCalls": []}
    if mid is not None:
        msg["id"] = mid
    if kind is not None:
        origin = {"kind": kind}
        if variant is not None:
            origin["variant"] = variant
        msg["origin"] = origin
    return {"type": "context.append_message", "message": msg, "time": ms}


def kloop(etype, ms=KIMI_MS, **kw):
    ev = {"type": etype}
    ev.update(kw)
    return {"type": "context.append_loop_event", "event": ev, "time": ms}


def ktext(text, uuid="ev-01", ms=KIMI_MS):
    return kloop("content.part", ms, uuid=uuid, turnId="0", step=1, part={"type": "text", "text": text})


def kthink(text, uuid="ev-th", ms=KIMI_MS):
    return kloop("content.part", ms, uuid=uuid, turnId="0", step=1, part={"type": "think", "think": text})


def kparse(records, session=KIMI_SESS, denylist=(CLIENT,), subagent=False, batch_items=256, **kw):
    parser = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=list(denylist), batch_items=batch_items,
                              session_id=session, subagent=subagent, **kw)
    for r in records:
        parser.feed(r)
    return parser


def _kimi_wire_fixture():
    """One main wire holding every record family, in real order: bookkeeping, a typed turn, the loop's events,
    machine-origin messages, compaction, undo/clear, and an unknown type."""
    return [
        {"type": "metadata", "protocol_version": "1.5", "created_at": KIMI_MS - 1000},
        {"type": "profile.bind", "modelAlias": "kimi-code/k3", "systemPrompt": "synthetic", "time": KIMI_MS - 999},
        {"type": "llm.request", "kind": "loop", "model": "k3", "time": KIMI_MS - 998},
        {"type": "usage.record", "model": "k3", "usage": {"output": 1}, "time": KIMI_MS - 997},
        {"type": "token_counting.measured", "length": 2, "tokens": 5, "time": KIMI_MS - 996},
        {"type": "turn.prompt", "input": [{"type": "text", "text": "the operator's first question"}],
         "origin": {"kind": "user"}, "time": KIMI_MS - 995},
        {"type": "agent.message.appended",
         "message": {"message": {"role": "user", "content": [{"type": "text", "text": "the operator's first question"}]},
                     "meta": {"source": "input", "origin": {"kind": "user"}, "promptId": "msg_a"}}, "time": KIMI_MS - 994},
        kmsg("the operator's first question", mid="msg_a"),
        kloop("step.begin", uuid="ev-sb", turnId="0", step=1),
        kthink(PRIVATE, uuid="ev-th1"),
        ktext("First answer block.", uuid="ev-a"),
        kloop("tool.call", uuid="ev-tc", toolCallId="tool_1", name="Read", args={"path": "x"}),
        kloop("tool.result", uuid="ev-tr", toolCallId="tool_1", result={"output": "synthetic tool output"}),
        kloop("step.end", uuid="ev-se", turnId="0", step=1),
        ktext("Second answer block.", uuid="ev-b"),
        kmsg("AGENTS.md text the harness re-injected", mid="msg_inj", kind="injection", variant="agents_md"),
        kmsg("<cron-fire jobId=\"01M0SYNTHETIC\" cron=\"7 * * * *\">synthetic</cron-fire>", mid="msg_cron",
             kind="cron_job"),
        kmsg("a system trigger rendering", mid="msg_sys", kind="system_trigger"),
        kmsg("a background task rendering", mid="msg_task", kind="task"),
        kmsg("a skill activation rendering", mid="msg_skill", kind="skill_activation"),
        {"type": "context.apply_compaction", "summary": "a model-written summary of earlier dialogue",
         "compactedCount": 9, "time": KIMI_MS},
        {"type": "context.undo", "count": 1, "time": KIMI_MS},
        {"type": "context.clear", "time": KIMI_MS},
        {"type": "some.future.type", "time": KIMI_MS},
        kmsg("the operator's second question", mid="msg_b"),
        ktext("Final answer.", uuid="ev-c"),
    ]


def test_kimi_dialogue_items_and_every_drop_class():
    parser = kparse(_kimi_wire_fixture())
    assert view(parser) == [text_item(0, "user_text", "user", "the operator's first question"),
                            text_item(1, "assistant_text", "assistant", "First answer block."),
                            text_item(2, "assistant_text", "assistant", "Second answer block."),
                            text_item(3, "user_text", "user", "the operator's second question"),
                            text_item(4, "assistant_text", "assistant", "Final answer.")], view(parser)
    assert [s for s, _ in parser.skipped] == (
        ["kimi_bookkeeping"] * 7                # metadata, profile.bind, llm.request, usage.record,
                                                # token_counting.measured, turn.prompt, agent.message.appended
        + ["kimi_loop_event", "kimi_thinking"]  # step.begin, then the think part (F1: thinking is dropped, 9b)
        + ["kimi_loop_event"] * 3               # tool.call, tool.result, step.end
        + ["kimi_injection_agents_md", "kimi_cron_job", "kimi_system_trigger", "kimi_origin_task",
           "kimi_origin_skill_activation"]
        + ["kimi_compaction", "kimi_undo", "kimi_clear", "kimi_bookkeeping"]), parser.skipped
    src = h32(KIMI_SESS)
    first = parser.items[0]
    assert list(first) == ["schema", "kind", "seat", "src", "gen", "batch", "ordinal", "item", "role", "dialect",
                           "adapter_version", "time", "native_ref", "text", "text_sha256"], list(first)
    assert first["dialect"] == "kimi" and first["src"] == src and first["time"] == KIMI_TIME
    assert [i["native_ref"] for i in parser.items] == [h32("msg_a"), h32("ev-a"), h32("ev-b"), h32("msg_b"),
                                                       h32("ev-c")]
    assert all(i["src"] == src for i in parser.items)
    dumped = json.dumps(parser.items)
    assert PRIVATE not in dumped and "synthetic tool output" not in dumped and "summary of earlier" not in dumped


def test_kimi_machine_origins_and_machine_typed_text_are_dropped_reported_and_flag_controlled():
    delivery = 'MeshyChat message for you (nxxxxx-prod): read msg/1 with the signed command'
    recs = [kmsg("a typed line", mid="m1"), kmsg(delivery, mid="m2"), kmsg("harness words", mid="m3", kind="cron_job")]
    parser = kparse(recs)
    assert view(parser) == [text_item(0, "user_text", "user", "a typed line")]
    assert parser.skipped == [("kimi_machine_typed", "m2"), ("kimi_cron_job", "m3")]
    keep = kparse(recs, drop_machine_typed=False)                 # the audit mode, as for codex
    assert [i["text"] for i in keep.items] == ["a typed line", delivery, "harness words"]
    assert keep.skipped == []


def test_kimi_legacy_message_without_origin_is_typed_and_reminder_parts_are_cut():
    legacy = {"type": "context.append_message",
              "message": {"role": "user", "content": [{"type": "text", "text": "an old-format typed line"}],
                          "id": "msg_old"}, "time": KIMI_MS}
    mixed = kmsg("kept words", mid="msg_mix")
    mixed["message"]["content"] = [{"type": "text", "text": "<system-reminder>\nhousekeeping\n</system-reminder>"},
                                   {"type": "text", "text": "kept words"},
                                   {"type": "image_url", "image_url": {"url": "data:synthetic"}}]
    bare = {"type": "context.append_message",
            "message": {"role": "user", "content": "a bare string body", "id": "msg_bare"}, "time": KIMI_MS}
    in_list = kmsg("unused", mid="msg_lst")
    in_list["message"]["content"] = ["a bare string part", {"type": "text", "text": "and a typed part"}]
    parser = kparse([legacy, mixed, bare, in_list])
    assert view(parser) == [text_item(0, "user_text", "user", "an old-format typed line"),
                            text_item(1, "user_text", "user", "kept words"),
                            text_item(2, "user_text", "user", "a bare string body"),
                            text_item(3, "user_text", "user", "a bare string part"),
                            text_item(4, "user_text", "user", "and a typed part")], view(parser)
    assert parser.skipped == []


def test_kimi_restart_at_every_record_gives_identical_items_and_drop_classes():
    recs = _kimi_wire_fixture()
    whole = kparse(recs)
    whole.finish()
    expected_items = [cp.serialize(i) for i in whole.items]
    for cut in range(len(recs) + 1):
        first = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], session_id=KIMI_SESS)
        for r in recs[:cut]:
            first.feed(r)
        revived = cp.ContextParser.from_state(json.loads(json.dumps(first.state())), denylist=[CLIENT])
        for r in recs[cut:]:
            revived.feed(r)
        revived.finish()
        got = [cp.serialize(i) for i in first.items] + [cp.serialize(i) for i in revived.items]
        assert got == expected_items, cut
        assert first.skipped + revived.skipped == whole.skipped, cut


def test_v6_kimi_checkpoint_resumes_byte_identically_against_fresh_v7_parse():
    """kxxx's #421/#425 reconciliation: a v6 checkpoint for dialect "kimi" (v=6, no ag_hwm key, as the step-9
    parser wrote it while STATE_VERSION was still 6) migrates under the v7 parser with ag_hwm=None and emits
    byte-identical items and manifests for the rest of the wire against a fresh v7 parse of the whole wire;
    a v6 state with dialect "antigravity" is still refused."""
    recs = _kimi_wire_fixture()
    cut = 20                                    # mid-wire: after the first dialogue items, before the rest
    fresh = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], session_id=KIMI_SESS)
    for idx, r in enumerate(recs):
        if idx == cut:
            fresh.close_batch()
        fresh.feed(r)
    fresh.finish()

    part = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], session_id=KIMI_SESS)
    for r in recs[:cut]:
        part.feed(r)
    part.close_batch()
    # Exact v6 checkpoint shape written while the step-9 branch pinned STATE_VERSION = 6: v=6, no ag_hwm key and
    # none of v8's F3 keys (it migrates v6 -> v7 -> v8).
    v6_kimi_state = {k: v for k, v in json.loads(json.dumps(part.state())).items()
                     if k != "ag_hwm" and k not in V8_KEYS}
    v6_kimi_state["v"] = 6
    try:
        resumed = cp.ContextParser.from_state(v6_kimi_state, denylist=[CLIENT])
    except ValueError as exc:
        raise AssertionError("v6 kimi checkpoint must migrate cleanly: %s" % exc) from exc
    for r in recs[cut:]:
        resumed.feed(r)
    resumed.finish()

    combined = part.items + resumed.items
    assert [cp.serialize(i) for i in combined] == [cp.serialize(i) for i in fresh.items]
    assert [cp.serialize(m) for m in cp.manifests(combined)] == [
        cp.serialize(m) for m in cp.manifests(fresh.items)
    ]
    assert part.skipped + resumed.skipped == fresh.skipped
    assert resumed.state() == fresh.state()     # converges to the fresh v7 state, ag_hwm None included
    cp.validate_state(resumed.state())

    # Negative: a v6 state with dialect "antigravity" is still refused by both validate_state and from_state
    v6_ag_state = dict(v6_kimi_state, dialect="antigravity")
    for fn in (cp.validate_state, cp.ContextParser.from_state):
        try:
            fn(v6_ag_state)
        except ValueError:
            pass
        else:
            raise AssertionError("v6 antigravity checkpoint must be refused")


def test_kimi_subagent_wire_gives_one_artifact_and_drops_the_dispatch_prompt():
    sub_src = KIMI_SESS + "/agents/agent-3"
    recs = [{"type": "metadata", "protocol_version": "1.5", "created_at": KIMI_MS - 1000},
            kmsg("You are an evidence-checker. Do the task.", mid="m0", kind="system_trigger"),
            kthink(PRIVATE, uuid="ev-th"), ktext("a draft progress note", uuid="ev-1"),
            kloop("tool.call", uuid="ev-tc", toolCallId="tool_9", name="Grep", args={}),
            kloop("tool.result", uuid="ev-tr", toolCallId="tool_9", result={"output": "synthetic"}),
            ktext("the subagent's final report", uuid="ev-2"), ktext("   ", uuid="ev-3")]
    parser = kparse(recs, session=sub_src, subagent=True)
    assert parser.items == [], "a subagent source emits nothing before finish()"
    assert parser.skipped == [("kimi_bookkeeping", None), ("kimi_subagent_prompt", "m0"), ("kimi_thinking", "ev-th"),
                              ("kimi_loop_event", "ev-tc"), ("kimi_loop_event", "ev-tr")], parser.skipped
    (item,) = parser.finish()
    assert item["item"] == "artifact" and "role" not in item and item["text"] == "the subagent's final report"
    assert item["src"] == item["agent_id"] == h32(sub_src) and item["native_ref"] == h32("ev-2")
    assert item["time"] == KIMI_TIME
    assert parser.finish() == [], "finish is idempotent"
    empty = kparse([kmsg("dispatch", mid="m9"), kthink("only reasoning", uuid="ev-t")], session=sub_src,
                   subagent=True)
    assert empty.finish() == [] and ("no_final_text", None) in empty.skipped


def test_kimi_subagent_cut_keeps_the_pending_report_in_state():
    sub_src = KIMI_SESS + "/agents/agent-4"
    recs = [kmsg("dispatch", mid="m0"), ktext("draft one", uuid="ev-1"), ktext("the real ending", uuid="ev-2")]
    whole = kparse(recs, session=sub_src, subagent=True)
    whole.finish()
    first = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], session_id=sub_src, subagent=True)
    for r in recs[:2]:
        first.feed(r)
    revived = cp.ContextParser.from_state(json.loads(json.dumps(first.state())), denylist=[CLIENT])
    revived.feed(recs[2])
    revived.finish()
    assert [cp.serialize(i) for i in revived.items] == [cp.serialize(i) for i in whole.items]


def test_kimi_items_are_screened_and_budgeted_like_every_dialect():
    parser = kparse([kmsg("here is the key " + KEY_MARKER, mid="m1"), ktext("mentioning " + CLIENT, uuid="e1")])
    assert view(parser) == [omitted(0, "secret"), omitted(1, "client_name")]
    big = kparse([ktext("z" * 70000, uuid="e2")])
    assert view(big) == [omitted(0, "too_large", 70000)]
    unscreened = kparse([kmsg("keep " + KEY_MARKER, mid="m3"), ktext("keep " + CLIENT, uuid="e3")],
                        namespace="shared/transcript-context")
    assert [i["item"] for i in unscreened.items] == ["user_text", "assistant_text"] and unscreened.screens is False
    resumed = cp.ContextParser.from_state(json.loads(json.dumps(unscreened.state())), denylist=[CLIENT],
                                          namespace="shared/transcript-context")
    assert resumed.screens is False


def test_kimi_undo_and_clear_are_reported_and_retract_nothing():
    recs = [kmsg("first typed question", mid="m1"), ktext("answer one", uuid="e1"),
            {"type": "context.undo", "count": 2, "time": KIMI_MS},
            kmsg("second typed question", mid="m2"), {"type": "context.clear", "time": KIMI_MS},
            ktext("answer two", uuid="e2")]
    parser = kparse(recs)
    assert [i.get("text") for i in parser.items] == ["first typed question", "answer one",
                                                     "second typed question", "answer two"]
    assert parser.skipped == [("kimi_undo", None), ("kimi_clear", None)]


def test_kimi_records_without_a_usable_time_take_no_ordinal():
    undated_msg = kmsg("a typed line with no time", mid="m-undated", ms=None)
    del undated_msg["time"]
    undated_part = ktext("an undated answer", uuid="e-undated", ms=None)
    del undated_part["time"]
    parser = kparse([kmsg("before", mid="m1"), undated_msg, undated_part, ktext("after", uuid="e2")])
    assert [(i["ordinal"], i["text"]) for i in parser.items] == [(0, "before"), (1, "after")]
    assert parser.skipped == [("no_time", "m-undated"), ("no_time", "e-undated")]


def test_kimi_content_without_a_session_identity_quarantines_the_source():
    recs = [ktext("answer text", uuid="e1"), kmsg("typed question", mid="m1"), ktext("more answer", uuid="e2")]
    parser = kparse(recs, session=None)                       # no --session-id and no path: no identity
    assert parser.items == [] and parser.quarantined is True and parser.src is None
    assert parser.skipped == [("no_session_id", None)], parser.skipped
    revived = cp.ContextParser.from_state(json.loads(json.dumps(parser.state())), denylist=[CLIENT])
    revived.feed(kmsg("a later typed question", mid="m2"))
    assert revived.items == [] and revived.state()["quarantined"] is True
    assert revived.skipped == []
    # a typed MESSAGE first quarantines just the same, and exactly once
    first_msg = kparse([kmsg("typed with no identity", mid="mx"), ktext("answer", uuid="ex")], session=None)
    assert first_msg.items == [] and first_msg.quarantined is True
    assert first_msg.skipped == [("no_session_id", None)], first_msg.skipped
    # a machine-origin message is not content: it never quarantines
    quiet = kparse([kmsg("harness words", mid="m9", kind="cron_job")], session=None)
    assert quiet.quarantined is False and quiet.items == [] and quiet.skipped == [("kimi_cron_job", "m9")]


def test_kimi_non_user_message_roles_are_reported_never_emitted():
    parser = kparse([kmsg("assistant-role wire noise", mid="m1", role="assistant"),
                     {"type": "context.append_message", "message": {"content": []}, "time": KIMI_MS},
                     kmsg("typed", mid="m2")])
    assert view(parser) == [text_item(0, "user_text", "user", "typed")]
    assert parser.skipped == [("kimi_message_assistant", "m1"), ("kimi_message_None", None)], parser.skipped


def test_kimi_path_derivation_subagent_mark_and_cli():
    import tempfile
    work = tempfile.mkdtemp()
    root = os.path.join(work, "sessions", "wd_proj_deadbeef", "session_" + KIMI_SESS, "agents")
    main_wire = os.path.join(root, "main", "wire.jsonl")
    sub_wire = os.path.join(root, "agent-3", "wire.jsonl")
    os.makedirs(os.path.dirname(main_wire))
    os.makedirs(os.path.dirname(sub_wire))
    recs = [{"type": "metadata", "protocol_version": "1.5", "created_at": KIMI_MS}, kmsg("path-derived identity", mid="m1"),
            ktext("path-derived answer", uuid="e1")]
    raw = b"".join((json.dumps(r, ensure_ascii=False) + chr(10)).encode("utf-8") for r in recs)
    open(main_wire, "wb").write(raw)
    parser, report = cp.parse_file(main_wire, SEAT, "kimi")
    assert [i["item"] for i in parser.items] == ["user_text", "assistant_text"]
    assert parser.items[0]["src"] == h32(KIMI_SESS) and report["dialect"] == "kimi"
    assert report["by_kind"] == {"assistant_text": 1, "user_text": 1}
    open(sub_wire, "wb").write(raw)
    sub_parser, _ = cp.parse_file(sub_wire, SEAT, "kimi")
    (artifact,) = sub_parser.items                      # parse_file calls finish(): the artifact is emitted there
    assert artifact["item"] == "artifact" and artifact["src"] == artifact["agent_id"] == h32(KIMI_SESS + "/agents/agent-3")
    out = os.path.join(work, "out")
    assert cp.main([main_wire, "--seat", SEAT, "--dialect", "kimi", "--out", out]) == 0
    items = [json.loads(l)["value"] for l in open(os.path.join(out, "items.jsonl"), encoding="utf-8")]
    assert [i["item"] for i in items] == ["user_text", "assistant_text"] and items[0]["src"] == h32(KIMI_SESS)
    rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
    assert rep["dialect"] == "kimi" and rep["records"] == 3
    out2 = os.path.join(work, "out2")
    assert cp.main([main_wire, "--seat", SEAT, "--dialect", "kimi", "--session-id", "explicit-kimi-session",
                    "--out", out2]) == 0
    items2 = [json.loads(l)["value"] for l in open(os.path.join(out2, "items.jsonl"), encoding="utf-8")]
    assert items2[0]["src"] == h32("explicit-kimi-session")
    # a wire-shaped file at a path with no session_ component has no identity: quarantine, nothing emitted
    bare = os.path.join(work, "bare.jsonl")
    open(bare, "wb").write(raw)
    bare_parser, bare_report = cp.parse_file(bare, SEAT, "kimi")
    assert bare_parser.items == [] and bare_parser.quarantined is True
    assert ["no_session_id"] == [r for r, _ in bare_report["skipped"] if r == "no_session_id"]


def test_kimi_dialect_rejects_a_non_bool_subagent_flag():
    try:
        cp.ContextParser(seat=SEAT, dialect="kimi", session_id=KIMI_SESS, subagent="yes")
    except ValueError:
        pass
    else:
        raise AssertionError("a non-bool subagent flag must be refused")


def test_kimi_explicit_session_id_on_a_subagent_path_never_collides_with_the_parent():
    # txxxx's step-9 P2 #1: --session-id on a subagent wire used to put it on the parent's src
    base = os.path.join("C:", "home", "wd_x", "session_abc-def", "agents")
    parent = cp.ContextParser(seat=SEAT, dialect="kimi", session_id="abc-def",
                              source_path=os.path.join(base, "main", "wire.jsonl"))
    child = cp.ContextParser(seat=SEAT, dialect="kimi", session_id="abc-def",
                             source_path=os.path.join(base, "sub-1", "wire.jsonl"))
    assert parent.subagent is False and parent.src == h32("abc-def")
    assert child.subagent is True and child.src == h32("abc-def/agents/sub-1") != parent.src
    derived = cp.ContextParser(seat=SEAT, dialect="kimi", source_path=os.path.join(base, "sub-1", "wire.jsonl"))
    assert derived.subagent is True and derived.src == child.src, "given and derived ids key a subagent the same"
    child.feed(kmsg("the parent's dispatch prompt", mid="m0", kind="system_trigger"))
    child.feed(ktext("the child report", uuid="e0"))
    assert child.items == [] and child.skipped == [("kimi_subagent_prompt", "m0")]
    (art,) = child.finish()
    assert art["src"] == art["agent_id"] == h32("abc-def/agents/sub-1")


def test_kimi_off_layout_wire_with_a_session_id_is_a_main_wire():
    # txxxx's step-9 P2 #2: a frozen/copy path outside agents/ used to run in subagent mode and lose the dialogue
    import tempfile
    work = tempfile.mkdtemp()
    frozen = os.path.join(work, "spool", "frozen-0001.jsonl")
    os.makedirs(os.path.dirname(frozen))
    recs = [{"type": "metadata", "protocol_version": "1.5", "created_at": KIMI_MS},
            kmsg("a typed prompt on a copied wire", mid="m1"), ktext("its answer", uuid="e1")]
    open(frozen, "wb").write(b"".join((json.dumps(r, ensure_ascii=False) + chr(10)).encode("utf-8") for r in recs))
    parser, report = cp.parse_file(frozen, SEAT, "kimi", session_id="frozen-session-1")
    assert parser.subagent is False
    assert view(parser) == [text_item(0, "user_text", "user", "a typed prompt on a copied wire"),
                            text_item(1, "assistant_text", "assistant", "its answer")], view(parser)
    assert parser.items[0]["src"] == h32("frozen-session-1") and parser.skipped == [("kimi_bookkeeping", None)]
    assert report["by_kind"] == {"assistant_text": 1, "user_text": 1}
    # a main wire under an agents/ dir is unchanged, and only a non-main dir under agents/ is a subagent
    import kimi_dialect as kd
    assert kd.wire_identity(frozen)[1] is False
    assert kd.wire_identity(os.path.join(work, "agents", "main", "wire.jsonl"))[1] is False
    assert kd.wire_identity(os.path.join(work, "agents", "agent-0", "wire.jsonl"))[1] is True


def test_kimi_screening_and_wire_validate_context_contract():
    from datetime import datetime, timezone
    import importlib.util
    import tempfile

    if importlib.util.find_spec("django") is None:
        if "pytest" in sys.modules:
            sys.modules["pytest"].skip("django is not installed in this Python environment")
        return

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.append(repo_root)
    from apps.context import validation

    now_dt = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
    work = tempfile.mkdtemp()
    deny_path = os.path.join(work, "denylist.json")
    open(deny_path, "w", encoding="utf-8").write(json.dumps([CLIENT]))

    # 1. Screened scratch namespace: secrets and client names become omitted items; every item & manifest validates.
    scratch_ns = "shared/scratch-transcript-context"
    p_scr = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], namespace=scratch_ns,
                             session_id="kimi-wire-scr")
    p_scr.feed(kmsg("Clean operator instruction.", mid="m1"))
    p_scr.feed(ktext("Clean assistant answer.", uuid="e1"))
    p_scr.feed(kmsg("Leak key: " + KEY_MARKER, mid="m2"))
    p_scr.feed(ktext("Client mention: " + CLIENT, uuid="e2"))
    assert [i["item"] for i in p_scr.items] == ["user_text", "assistant_text", "omitted", "omitted"]
    assert [i.get("omitted_reason") for i in p_scr.items[2:]] == ["secret", "client_name"]

    old_ns = os.environ.get("MESHY_CONTEXT_NAMESPACE")
    old_deny = os.environ.get("MESHY_CONTEXT_DENYLIST")
    try:
        os.environ["MESHY_CONTEXT_NAMESPACE"] = scratch_ns
        os.environ["MESHY_CONTEXT_DENYLIST"] = deny_path
        for item in p_scr.items:
            assert validation.validate_context(scratch_ns, cp.item_key(item), item, now=now_dt) == "item"
        for m in cp.manifests(p_scr.items):
            assert validation.validate_context(scratch_ns, cp.manifest_key(p_scr.items[0]), m, now=now_dt) == "manifest"
    finally:
        if old_ns is None:
            os.environ.pop("MESHY_CONTEXT_NAMESPACE", None)
        else:
            os.environ["MESHY_CONTEXT_NAMESPACE"] = old_ns
        if old_deny is None:
            os.environ.pop("MESHY_CONTEXT_DENYLIST", None)
        else:
            os.environ["MESHY_CONTEXT_DENYLIST"] = old_deny

    # 2. Unscreened shared/transcript-context: secrets and client names kept as written; subagent artifact validates.
    prod_ns = "shared/transcript-context"
    p_prod = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], namespace=prod_ns,
                              session_id="kimi-wire-prod")
    p_prod.feed(kmsg("Keep secret: " + KEY_MARKER, mid="m3"))
    p_prod.feed(ktext("Keep client: " + CLIENT, uuid="e3"))
    assert [i["item"] for i in p_prod.items] == ["user_text", "assistant_text"]
    for item in p_prod.items:
        assert validation.validate_context(prod_ns, cp.item_key(item), item, now=now_dt) == "item"
    for m in cp.manifests(p_prod.items):
        assert validation.validate_context(prod_ns, cp.manifest_key(p_prod.items[0]), m, now=now_dt) == "manifest"

    p_sub = cp.ContextParser(seat=SEAT, dialect="kimi", namespace=prod_ns,
                             session_id=KIMI_SESS + "/agents/agent-9", subagent=True)
    p_sub.feed(kmsg("the parent's dispatch prompt", mid="m9", kind="system_trigger"))
    p_sub.feed(ktext("Subagent wire-validated report.", uuid="e9"))
    (art,) = p_sub.finish()
    assert validation.validate_context(prod_ns, cp.item_key(art), art, now=now_dt) == "item"
    (sub_m,) = cp.manifests([art])
    assert validation.validate_context(prod_ns, cp.manifest_key(art), sub_m, now=now_dt) == "manifest"


def _run_all():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:           # noqa: BLE001
                failures += 1
                print("FAIL", name, type(e).__name__, str(e)[:300])
    print("%d failed" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())


# ---- #429 on #425: the kimi dialect under the v8 checkpoint and the F3 switch (txxxx 11546c) -----------------------

def test_a_v7_kimi_checkpoint_from_main_resumes_as_v8_with_identical_items():
    """A v7 state as main's kimi dialect wrote it (no F3 keys) migrates to v8 with F3 off and finishes the wire
    byte-identically to a fresh parse; nothing is refused or stranded."""
    recs = _kimi_wire_fixture()
    for cut in (1, 20, len(recs) - 1):
        fresh = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], session_id=KIMI_SESS)
        part = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], session_id=KIMI_SESS)
        for idx, r in enumerate(recs):
            if idx == cut:
                fresh.close_batch()
            fresh.feed(r)
            if idx < cut:
                part.feed(r)
        fresh.finish()
        part.close_batch()
        v7 = {k: v for k, v in json.loads(json.dumps(part.state())).items() if k not in V8_KEYS}
        v7["v"] = 7
        resumed = cp.ContextParser.from_state(v7, denylist=[CLIENT])
        assert v7["v"] == 7 and "f3" not in v7, "the caller's v7 dict is not mutated"
        assert resumed.f3 is False and resumed.state()["v"] == 8
        for r in recs[cut:]:
            resumed.feed(r)
        resumed.finish()
        combined = part.items + resumed.items
        assert [cp.serialize(i) for i in combined] == [cp.serialize(i) for i in fresh.items], cut
        assert part.skipped + resumed.skipped == fresh.skipped, cut
        assert resumed.state() == fresh.state(), cut


def test_kimi_f3_off_is_unchanged_and_on_emits_tool_exchange_and_thinking():
    """F3-off behavior is byte-identical to the pre-adapter parser. With F3 on, the fixture's tool pair and
    think part add one tool_exchange and one thinking item, while dialogue stays in the same order."""
    recs = _kimi_wire_fixture()
    assert any(r.get("event", {}).get("type") in ("tool.call", "tool.result") for r in recs
               if isinstance(r.get("event"), dict)), "control: the fixture has tool records F3 pairs"
    off, on = kparse(recs), kparse(recs, f3=True)
    off.finish()
    on.finish()
    assert not any(i["item"] in ("tool_exchange", "thinking") for i in off.items)
    f3_items = [i for i in on.items if i["item"] in ("tool_exchange", "thinking")]
    assert [i["item"] for i in f3_items] == ["thinking", "tool_exchange"]
    assert [(i["item"], i["text"]) for i in on.items if i["item"] not in ("tool_exchange", "thinking")] == [
        (i["item"], i["text"]) for i in off.items]
    assert on._unit is None and on.tool_pending == [] and on.f3_dialect == {}


def test_a_v7_kimi_checkpoint_written_by_mains_parser_resumes_as_v8():
    """txxxx 9596cc: a v7 state built by THIS parser would miss an adapter or field bump, so the states here were
    written by main's own parser at cd5a68b9 (STATE_VERSION 7), with the same wire fixture, and checked in. Each
    resumes under the v8 parser and finishes byte-identically to a fresh parse cut at the same record."""
    import pathlib
    golden = json.loads((pathlib.Path(__file__).parent / "fixtures" / "context_v1"
                         / "kimi_v7_states_main_cd5a68b9.json").read_text(encoding="utf-8"))
    recs = _kimi_wire_fixture()
    assert sorted(int(c) for c in golden["cuts"]) == [1, 13, 20, len(recs) - 1]
    for cut_s, g in golden["cuts"].items():
        cut = int(cut_s)
        assert g["state"]["v"] == 7 and not any(k in g["state"] for k in V8_KEYS), cut
        fresh = cp.ContextParser(seat=SEAT, dialect="kimi", denylist=[CLIENT], session_id=KIMI_SESS)
        for idx, r in enumerate(recs):
            if idx == cut:
                fresh.close_batch()
            fresh.feed(r)
        fresh.finish()
        resumed = cp.ContextParser.from_state(json.loads(json.dumps(g["state"])), denylist=[CLIENT])
        for r in recs[cut:]:
            resumed.feed(r)
        resumed.finish()
        got = g["items"] + [cp.serialize(i).decode("utf-8") for i in resumed.items]
        assert got == [cp.serialize(i).decode("utf-8") for i in fresh.items], cut
        assert [tuple(s) for s in g["skipped"]] + resumed.skipped == fresh.skipped, cut
        assert resumed.state() == fresh.state(), cut


def _non_kimi_v7_resume_fixtures():
    """Synthetic wires used both by the v7 golden generator and the v8 resume test."""
    claude_rows = [
        {"type": "user", "sessionId": "cl-v7-sess", "uuid": "u1", "timestamp": "2026-10-26T09:00:00.000Z",
         "message": {"role": "user", "content": "First synthetic Claude prompt."}},
        {"type": "assistant", "sessionId": "cl-v7-sess", "uuid": "a1", "timestamp": "2026-10-26T09:00:05.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Planning two synthetic tasks."},
             {"type": "tool_use", "name": "TodoWrite",
              "input": {"todos": [{"content": "Migrate v7 state", "status": "in_progress"},
                                  {"content": "Verify byte parity", "status": "pending"}]}},
         ]}},
        {"type": "user", "sessionId": "cl-v7-sess", "uuid": "u2", "timestamp": "2026-10-26T09:01:00.000Z",
         "message": {"role": "user", "content": "Continue after the synthetic checkpoint."}},
        {"type": "assistant", "sessionId": "cl-v7-sess", "uuid": "a2", "timestamp": "2026-10-26T09:01:05.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "TodoWrite",
              "input": {"todos": [{"content": "Migrate v7 state", "status": "completed"},
                                  {"content": "Verify byte parity", "status": "completed"}]}},
             {"type": "text", "text": "Synthetic migration complete."},
         ]}},
    ]
    antigravity_rows = [
        _ag_user(0, "Initial synthetic operator request.", created_at="2026-10-26T08:00:00Z", conv_id="main-v7-42"),
        _ag_planner(1, "Initial synthetic assistant answer.", created_at="2026-10-26T08:00:05Z"),
        _ag_other(0, "SYSTEM", "SYSTEM_MESSAGE", "[Message] sender=task-99 finished", created_at="2026-10-26T08:10:00Z"),
        _ag_user(1, "Post-rewind synthetic operator follow-up.", created_at="2026-10-26T08:10:05Z"),
        _ag_planner(2, "Post-rewind synthetic assistant follow-up.", created_at="2026-10-26T08:10:10Z"),
    ]
    return {
        "claude": (claude_rows, {"namespace": "shared/transcript-context"}),
        "codex": (_three_batch_rows(), {}),
        "antigravity": (antigravity_rows, {"namespace": "shared/transcript-context"}),
    }


def _v7_golden_cuts(records):
    return sorted({1, len(records) // 2, len(records) - 1})


def test_v7_non_kimi_checkpoints_from_main_resume_as_v8_with_identical_items():
    """Golden v7 states written by main at cd5a68b9 resume under v8 for every non-Kimi dialect.

    Each cut finishes byte-identically to a fresh v8 parse, including items, drop classes, and final state.
    """
    import pathlib
    fixture_dir = pathlib.Path(__file__).parent / "fixtures" / "context_v1"
    for dialect, (recs, kwargs) in _non_kimi_v7_resume_fixtures().items():
        golden = json.loads((fixture_dir / ("%s_v7_states_main_cd5a68b9.json" % dialect)).read_text(encoding="utf-8"))
        assert golden["source_commit"] == "cd5a68b9bc90ac9128df0fee138b56b8991f58fc"
        assert sorted(int(c) for c in golden["cuts"]) == _v7_golden_cuts(recs), dialect
        for cut_s, g in golden["cuts"].items():
            cut = int(cut_s)
            assert g["state"]["v"] == 7 and g["state"]["dialect"] == dialect, (dialect, cut)
            assert not any(k in g["state"] for k in V8_KEYS), (dialect, cut)
            fresh = cp.ContextParser(seat=SEAT, dialect=dialect, denylist=[CLIENT], **kwargs)
            for idx, r in enumerate(recs):
                if idx == cut:
                    fresh.close_batch()
                fresh.feed(r)
            fresh.finish()
            resumed = cp.ContextParser.from_state(json.loads(json.dumps(g["state"])), denylist=[CLIENT])
            for r in recs[cut:]:
                resumed.feed(r)
            resumed.finish()
            got = g["items"] + [cp.serialize(i).decode("utf-8") for i in resumed.items]
            assert got == [cp.serialize(i).decode("utf-8") for i in fresh.items], (dialect, cut)
            assert [tuple(s) for s in g["skipped"]] + resumed.skipped == fresh.skipped, (dialect, cut)
            assert resumed.state() == fresh.state(), (dialect, cut)
