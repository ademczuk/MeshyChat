"""test_transcript_cards_antigravity.py - Tests for Antigravity dialect in transcript_cards loader.

Run:
  python tests/test_transcript_cards_antigravity.py
  pytest tests/test_transcript_cards_antigravity.py -q
Mutants:
  python tools/transcript_cards/mutate_loader.py
"""

import json
import os
import shutil
import sys
import tempfile

LOADER_DIR = os.environ.get("TRANSCRIPT_CARDS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
if LOADER_DIR not in sys.path:
    sys.path.insert(0, LOADER_DIR)

import transcript_cards as tc  # noqa: E402
import antigravity_dialect as ad  # noqa: E402

T0 = "2026-10-10T08:00:00Z"
T1 = "2026-10-10T08:01:00Z"
T2 = "2026-10-10T08:02:00Z"
T3 = "2026-10-10T08:03:00Z"

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)) if detail and not cond else ""))


def make_user_record(step_index, content, created_at=T0):
    return {
        "step_index": step_index,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": created_at,
        "content": content,
    }


def make_planner_record(step_index, content="Planning next step", thinking="Internal thoughts", tool_calls=None, created_at=T0):
    rec = {
        "step_index": step_index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": created_at,
        "content": content,
        "thinking": thinking,
    }
    if tool_calls is not None:
        rec["tool_calls"] = tool_calls
    return rec


def make_tool_result_record(step_index, content, created_at=T0):
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "GENERIC",
        "status": "DONE",
        "created_at": created_at,
        "content": content,
    }


def make_system_record(step_index, content="System check", created_at=T0):
    return {
        "step_index": step_index,
        "source": "SYSTEM",
        "type": "SYSTEM_MESSAGE",
        "status": "DONE",
        "created_at": created_at,
        "content": content,
    }


def run_checks():
    root = tempfile.mkdtemp(prefix="tcards-ag-")
    try:
        # A01: typed and goal prompts
        brain1 = os.path.join(root, "b1", "sess-1", ".system_generated", "logs")
        os.makedirs(brain1, exist_ok=True)
        t1 = os.path.join(brain1, "transcript.jsonl")
        out1 = os.path.join(root, "out1")
        recs1 = [
            make_user_record(0, "<USER_REQUEST>\nImplement auth.\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nt: 1\n</ADDITIONAL_METADATA>", T0),
            make_planner_record(1, "Working", "Internal reasoning", created_at=T0),
            make_user_record(2, "<USER_REQUEST>\n/goal Verify all seats on MeshyChat.\n</USER_REQUEST>", T1),
            make_user_record(3, "Constraint: Never touch AcmeOne except AgentRuntime.", T2),
        ]
        with open(t1, "w", encoding="utf-8") as f:
            for r in recs1:
                f.write(json.dumps(r) + "\n")
        rep1 = tc.run(None, "antigravity", out1, antigravity_dir=os.path.join(root, "b1"))
        cards1 = [json.loads(line) for line in open(os.path.join(out1, "cards.jsonl"), encoding="utf-8")]
        check("A01 typed and goal prompts extracted",
              rep1["files_antigravity"] == 1 and rep1["new_cards"] == 3 and len(cards1) == 3)

        # A02: Probe 1 - avoid substring option inversion
        brain2 = os.path.join(root, "b2", "sess-p1", ".system_generated", "logs")
        os.makedirs(brain2, exist_ok=True)
        t2 = os.path.join(brain2, "transcript.jsonl")
        out2 = os.path.join(root, "out2")
        ask_call = [{
            "name": "ask_question",
            "args": {
                "questions": [{
                    "question": "Which backend to use?",
                    "options": ["PostgreSQL", "SQLite"],
                    "is_multi_select": False
                }]
            }
        }]
        recs2 = [
            make_planner_record(0, "Asking options", tool_calls=ask_call, created_at=T0),
            make_user_record(1, "Do not choose PostgreSQL; use SQLite.", T1),
        ]
        with open(t2, "w", encoding="utf-8") as f:
            for r in recs2:
                f.write(json.dumps(r) + "\n")
        tc.run(None, "antigravity", out2, antigravity_dir=os.path.join(root, "b2"))
        cards2 = [json.loads(line) for line in open(os.path.join(out2, "cards.jsonl"), encoding="utf-8")]
        check("A02 Probe 1 no substring option inversion",
              len(cards2) == 1 and cards2[0]["type"] == "Candidate" and "Do not choose" in cards2[0]["text"])

        # A03: Probe 2 - reject uncorrelated tool results
        brain3 = os.path.join(root, "b3", "sess-p2", ".system_generated", "logs")
        os.makedirs(brain3, exist_ok=True)
        t3 = os.path.join(brain3, "transcript.jsonl")
        out3 = os.path.join(root, "out3")
        recs3 = [
            make_planner_record(0, "Asking options", tool_calls=ask_call, created_at=T0),
            make_tool_result_record(1, '{"status": "success", "selection": "PostgreSQL"}', T1),
            make_user_record(2, "Let us proceed with the plan.", T2),
        ]
        with open(t3, "w", encoding="utf-8") as f:
            for r in recs3:
                f.write(json.dumps(r) + "\n")
        rep3 = tc.run(None, "antigravity", out3, antigravity_dir=os.path.join(root, "b3"))
        cards3 = [json.loads(line) for line in open(os.path.join(out3, "cards.jsonl"), encoding="utf-8")]
        check("A03 Probe 2 reject uncorrelated tool result",
              rep3["dropped_by_class"].get("antigravity_tool_result", 0) >= 1 and len(cards3) == 1 and cards3[0]["type"] == "Candidate")

        # A04: Probe 3 - extract visible dialogue detail
        ev_u, _ = ad.extract_context_event(make_user_record(0, "Check node status"), {"session": "s3"})
        ev_p, _ = ad.extract_context_event(make_planner_record(1, "All nodes are green.", thinking="hidden"), {"session": "s3"})
        check("A04 Probe 3 extract visible dialogue detail",
              ev_u["type"] == "user_turn" and ev_p["type"] == "assistant_turn" and ev_p["text"] == "All nodes are green.")

        # A05: Probe 4 - cross-wake ask buffering across capture runs
        brain5 = os.path.join(root, "b5", "sess-p4", ".system_generated", "logs")
        os.makedirs(brain5, exist_ok=True)
        t5 = os.path.join(brain5, "transcript.jsonl")
        out5 = os.path.join(root, "out5")
        with open(t5, "w", encoding="utf-8") as f:
            f.write(json.dumps(make_planner_record(0, "Asking", tool_calls=ask_call, created_at=T0)) + "\n")
        tc.run(None, "antigravity", out5, antigravity_dir=os.path.join(root, "b5"))
        with open(t5, "a", encoding="utf-8") as f:
            f.write(json.dumps(make_tool_result_record(1, "A1: SQLite\n", T1)) + "\n")
        tc.run(None, "antigravity", out5, antigravity_dir=os.path.join(root, "b5"))
        cards5 = [json.loads(line) for line in open(os.path.join(out5, "cards.jsonl"), encoding="utf-8")]
        check("A05 Probe 4 cross-wake ask persistence across runs",
              len(cards5) == 1 and cards5[0]["type"] == "Ruling" and cards5[0]["answer"] == "SQLite")

        # A06: benign tool results and thinking blocks dropped
        brain6 = os.path.join(root, "b6", "sess-drops", ".system_generated", "logs")
        os.makedirs(brain6, exist_ok=True)
        t6 = os.path.join(brain6, "transcript.jsonl")
        out6 = os.path.join(root, "out6")
        recs6 = [
            make_system_record(0, "System check"),
            make_planner_record(1, "Ready", thinking="scratchpad"),
            make_tool_result_record(2, "exit 0"),
        ]
        with open(t6, "w", encoding="utf-8") as f:
            for r in recs6:
                f.write(json.dumps(r) + "\n")
        rep6 = tc.run(None, "antigravity", out6, antigravity_dir=os.path.join(root, "b6"))
        check("A06 benign tool results and thinking blocks dropped",
              rep6["new_cards"] == 0 and "antigravity_system_message" in rep6["dropped_by_class"])

        # A07: subagent exclusion
        brain7 = os.path.join(root, "b7", "sess-sub", ".system_generated", "logs")
        os.makedirs(brain7, exist_ok=True)
        t7 = os.path.join(brain7, "transcript.jsonl")
        recs7 = [
            {"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "sender=task-123 prompt=audit"},
            make_user_record(1, "Subagent turn"),
        ]
        with open(t7, "w", encoding="utf-8") as f:
            for r in recs7:
                f.write(json.dumps(r) + "\n")
        picked7, seen7 = ad.select_files(os.path.join(root, "b7"))
        check("A07 subagent transcripts excluded by select_files",
              seen7 == 1 and len(picked7) == 0)

        # A08: enrollment with original_path
        scratch8 = os.path.join(root, "scratch8", "copy.jsonl")
        os.makedirs(os.path.dirname(scratch8), exist_ok=True)
        with open(scratch8, "w", encoding="utf-8") as f:
            f.write(json.dumps(make_user_record(0, "hi")) + "\n")
        orig8 = os.path.join("C:", "Users", "vexor", ".gemini", "antigravity-cli", "brain",
                             "sess-orig8", ".system_generated", "logs", "transcript.jsonl")
        st8 = ad.file_state(scratch8, original_path=orig8)
        check("A08 frozen copy enrolls with original path",
              st8 is not None and st8["session"] == "sess-orig8" and st8["subagent"] is False)

        # A09: enroll() returns 'antigravity'
        d_en, meta_en = tc.enroll(t1)
        check("A09 enroll returns antigravity dialect",
              d_en == "antigravity" and meta_en["session"] == "sess-1" and meta_en["subagent"] is False)

        # A10: read_file routes to antigravity raws and captures open asks
        lines10 = [(0, (json.dumps(make_planner_record(0, "Asking", tool_calls=ask_call, created_at=T0)) + "\n").encode("utf-8"))]
        meta10 = {"session": "sess-rf", "subagent": False}
        raws10, _, cp10 = tc.read_file(lines10, tc.new_checkpoint("antigravity", meta10), seat="antigravity")
        check("A10 read_file routes to antigravity raws and preserves open asks",
              len(raws10) == 0 and len(cp10["open_asks"]) == 1)

        # A11: cursor persists antigravity open asks
        brain11 = os.path.join(root, "b11", "sess-cur", ".system_generated", "logs")
        os.makedirs(brain11, exist_ok=True)
        t11 = os.path.join(brain11, "transcript.jsonl")
        out11 = os.path.join(root, "out11")
        with open(t11, "w", encoding="utf-8") as f:
            f.write(json.dumps(make_planner_record(0, "Asking", tool_calls=ask_call, created_at=T0)) + "\n")
        tc.run(None, "antigravity", out11, antigravity_dir=os.path.join(root, "b11"))
        cur11 = json.load(open(os.path.join(out11, "cursor.json"), "r", encoding="utf-8"))
        check("A11 cursor persists antigravity open asks",
              t11 in cur11.get("antigravity_open_asks", {}) and len(cur11["antigravity_open_asks"][t11]) == 1)

        # A12: Ruling options wrapped in dicts and free_text is False
        brain12 = os.path.join(root, "b12", "sess-opts", ".system_generated", "logs")
        os.makedirs(brain12, exist_ok=True)
        t12 = os.path.join(brain12, "transcript.jsonl")
        out12 = os.path.join(root, "out12")
        q_opts = [{
            "name": "ask_question",
            "args": {
                "questions": [{
                    "question": "Which strategy?",
                    "options": ["Rebase", "Merge"],
                    "is_multi_select": False
                }]
            }
        }]
        recs12 = [
            make_planner_record(0, "Asking options", tool_calls=q_opts, created_at=T0),
            make_user_record(1, "Rebase", T1),
        ]
        with open(t12, "w", encoding="utf-8") as f:
            for r in recs12:
                f.write(json.dumps(r) + "\n")
        tc.run(None, "antigravity", out12, antigravity_dir=os.path.join(root, "b12"))
        cards12 = [json.loads(line) for line in open(os.path.join(out12, "cards.jsonl"), encoding="utf-8")]
        check("A12 ruling options are dicts and free_text is False",
              len(cards12) == 1 and cards12[0]["type"] == "Ruling" and cards12[0]["free_text"] is False
              and len(cards12[0]["options"]) == 2 and cards12[0]["answer"] == "Rebase")
        check("A14 ruling timestamp normalized to utc_instant",
              len(cards12) == 1 and cards12[0]["time"] == "2026-10-10T08:01:00.000Z")

        # A13: Redaction canary drops
        brain13 = os.path.join(root, "b13", "sess-redact", ".system_generated", "logs")
        os.makedirs(brain13, exist_ok=True)
        t13 = os.path.join(brain13, "transcript.jsonl")
        out13 = os.path.join(root, "out13")
        recs13 = [
            make_user_record(0, "Here is the key: sk-ant-api03-abcdef12345678901234567890", T0),
            make_user_record(1, "Clean prompt without secrets.", T1),
        ]
        with open(t13, "w", encoding="utf-8") as f:
            for r in recs13:
                f.write(json.dumps(r) + "\n")
        rep13 = tc.run(None, "antigravity", out13, antigravity_dir=os.path.join(root, "b13"))
        check("A13 redaction drops secret prompt",
              rep13["new_cards"] == 1 and rep13["redaction_drops_new"] == 1)

        # A14: Partial lines and incremental replay
        brain14 = os.path.join(root, "b14", "sess-part", ".system_generated", "logs")
        os.makedirs(brain14, exist_ok=True)
        t14 = os.path.join(brain14, "transcript.jsonl")
        out14 = os.path.join(root, "out14")
        rec14_1 = make_user_record(0, "First prompt.", T0)
        rec14_2_part = json.dumps(make_user_record(1, "Incomplete record", T1))[:30]
        with open(t14, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec14_1) + "\n")
            f.write(rec14_2_part)
        rep14_1 = tc.run(None, "antigravity", out14, antigravity_dir=os.path.join(root, "b14"))
        rec14_2_full = make_user_record(1, "Completed record.", T1)
        with open(t14, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec14_1) + "\n")
            f.write(json.dumps(rec14_2_full) + "\n")
        rep14_2 = tc.run(None, "antigravity", out14, antigravity_dir=os.path.join(root, "b14"))
        check("A14 partial line deferred and completed record mined",
              rep14_1["new_cards"] == 1 and rep14_2["new_cards"] == 1)

        # A15 to A20: the Antigravity CLI reuses a step_index after a restart. Real case (dxxxxx, 13 Oct): one
        # session holds "please reingage with your monitor..." at step 18074, 07:01:29Z, and a different prompt
        # "/goal go through the agentmeshy messages..." at step 18074 again, 17:53:24Z. Under 1.20 the second was
        # folded into the first card as other_content and lost.
        first_rec = make_user_record(18074, "<USER_REQUEST>\nplease reingage with your monitor\n</USER_REQUEST>",
                                     "2026-08-22T07:01:29Z")
        reused_rec = make_user_record(18074, "<USER_REQUEST>\n/goal go through the agentmeshy messages\n</USER_REQUEST>",
                                      "2026-08-22T17:53:24Z")
        between = [make_planner_record(18075, "ok", created_at="2026-08-22T07:01:40Z"),
                   make_user_record(18076, "carry on", "2026-08-22T07:02:00Z")]

        def ag_case(tag, records):
            d = os.path.join(root, tag, "sess-reuse", ".system_generated", "logs")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "transcript.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            return path, os.path.join(root, tag), os.path.join(root, "out-" + tag)

        def cards_of(out):
            return {c["source"]["record_uuid"]: c for c in
                    (json.loads(line) for line in open(os.path.join(out, "cards.jsonl"), encoding="utf-8"))}

        plain = "antigravity:sess-reuse:18074"
        later = plain + ":2026-08-22T17:53:24Z"

        p15, b15, o15 = ag_case("b15", [first_rec] + between + [reused_rec])
        rep15 = tc.run(None, "antigravity", o15, antigravity_dir=b15)
        c15 = cards_of(o15)
        check("A15 a reused step_index with a different prompt is a card of its own",
              set(c15) == {plain, later, "antigravity:sess-reuse:18076"}
              and "reingage" in c15[plain]["text"] and "/goal go through" in c15[later]["text"]
              and rep15["uuid_conflicts"] == 0 and not c15[plain]["source"].get("other_content"),
              (sorted(c15), rep15["uuid_conflicts"]))

        # The first record at the index keeps the id 1.20 gave it: a store written before this change still holds
        # the same id for the same prompt (no card id that already exists may change).
        p15b, b15b, o15b = ag_case("b15b", [first_rec] + between)
        tc.run(None, "antigravity", o15b, antigravity_dir=b15b)
        c15b = cards_of(o15b)
        ids_before = {k: v["id"] for k, v in c15.items()}
        tc.run(None, "antigravity", o15, antigravity_dir=b15, rebuild=True)
        ids_after = {k: v["id"] for k, v in cards_of(o15).items()}
        check("A16 the first record keeps its id; a rebuilt store gives the same ids",
              c15b[plain]["id"] == c15[plain]["id"] and ids_before == ids_after, (ids_before, ids_after))

        # Incremental: the reused record arrives in a later run, after the cursor has moved past the first one.
        p17, b17, o17 = ag_case("b17", [first_rec] + between)
        tc.run(None, "antigravity", o17, antigravity_dir=b17)
        cur17 = json.load(open(os.path.join(o17, "cursor.json"), encoding="utf-8"))
        with open(p17, "a", encoding="utf-8") as f:
            f.write(json.dumps(reused_rec) + "\n")
        rep17 = tc.run(None, "antigravity", o17, antigravity_dir=b17)
        c17 = cards_of(o17)
        check("A17 the cursor carries the refs, so a reuse in a later run is a new card",
              cur17.get("antigravity_refs", {}).get(p17, {}).get(plain) == "2026-08-22T07:01:29Z"
              and rep17["new_cards"] == 1 and later in c17 and c17[plain]["id"] == c15[plain]["id"],
              (rep17["new_cards"], sorted(c17)))

        # A cursor written before 1.22 has no refs: the file is read again from the start rather than trusting
        # an empty map, which would hand the reused record the plain ref.
        p18, b18, o18 = ag_case("b18", [first_rec] + between)
        tc.run(None, "antigravity", o18, antigravity_dir=b18)
        cpath = os.path.join(o18, "cursor.json")
        cur18 = json.load(open(cpath, encoding="utf-8"))
        cur18.pop("antigravity_refs", None)
        json.dump(cur18, open(cpath, "w", encoding="utf-8"))
        with open(p18, "a", encoding="utf-8") as f:
            f.write(json.dumps(reused_rec) + "\n")
        rep18 = tc.run(None, "antigravity", o18, antigravity_dir=b18)
        c18 = cards_of(o18)
        check("A18 a cursor without refs reads the file again and still separates the reuse",
              later in c18 and rep18["uuid_conflicts"] == 0 and c18[plain]["id"] == c15[plain]["id"],
              (sorted(c18), rep18["uuid_conflicts"]))

        # The same record twice (same step_index, same created_at) is one record, not a reuse.
        p19, b19, o19 = ag_case("b19", [first_rec, first_rec])
        rep19 = tc.run(None, "antigravity", o19, antigravity_dir=b19)
        check("A19 the same record read twice stays one card",
              set(cards_of(o19)) == {plain} and rep19["new_cards"] == 1)

        # read_file across windows carries the refs in the checkpoint.
        line_a = (json.dumps(first_rec) + "\n").encode("utf-8")
        line_b = (json.dumps(reused_rec) + "\n").encode("utf-8")
        meta20 = {"session": "sess-reuse", "subagent": False}
        r20a, _, cp20 = tc.read_file([(0, line_a)], tc.new_checkpoint("antigravity", meta20), seat="antigravity")
        r20b, _, _ = tc.read_file([(len(line_a), line_b)], cp20, seat="antigravity")
        check("A20 read_file windows carry the refs in the checkpoint",
              [r["uuid"] for r in r20a] == [plain] and [r["uuid"] for r in r20b] == [later],
              ([r["uuid"] for r in r20a], [r["uuid"] for r in r20b]))

        # A21 to A23: multi-select A1 modal answers (comma-joined two-option, write-in text, and non-option rejection)
        multi_ask_call = [{
            "name": "ask_question",
            "args": {
                "questions": [{
                    "question": "Which tasks should run next?",
                    "options": [
                        "(Recommended) Fix mutant in mutate_loader.py, then run suite",
                        "Review open PRs #288, #292, #294",
                        "Draft fleet context graph issue",
                    ],
                    "is_multi_select": True,
                }]
            }
        }]
        two_opt_ans = (
            "(Recommended) Fix mutant in mutate_loader.py, then run suite, "
            "Review open PRs #288, #292, #294"
        )
        _, b21, o21 = ag_case("b21", [
            make_planner_record(10, "Select tasks", tool_calls=multi_ask_call, created_at=T0),
            make_tool_result_record(11, "Created At: %s\nCompleted At: %s\nA1: %s\n" % (T0, T1, two_opt_ans), T1),
        ])
        rep21 = tc.run(None, "antigravity", o21, antigravity_dir=b21)
        cards21 = [json.loads(line) for line in open(os.path.join(o21, "cards.jsonl"), encoding="utf-8")]
        check("A21 comma-joined two-option modal A1 answer produces Ruling with free_text False",
              rep21["new_cards"] == 1 and len(cards21) == 1
              and cards21[0]["type"] == "Ruling"
              and cards21[0]["answer"] == two_opt_ans
              and cards21[0]["multi_select"] is True
              and cards21[0]["free_text"] is False,
              cards21)

        write_in_ans = two_opt_ans + ", proceed"
        _, b22, o22 = ag_case("b22", [
            make_planner_record(20, "Select tasks", tool_calls=multi_ask_call, created_at=T0),
            make_tool_result_record(21, "Created At: %s\nCompleted At: %s\nA1: %s\n" % (T0, T1, write_in_ans), T1),
        ])
        rep22 = tc.run(None, "antigravity", o22, antigravity_dir=b22)
        cards22 = [json.loads(line) for line in open(os.path.join(o22, "cards.jsonl"), encoding="utf-8")]
        check("A22 comma-joined multi-option modal A1 answer with write-in text produces Ruling with free_text True",
              rep22["new_cards"] == 1 and len(cards22) == 1
              and cards22[0]["type"] == "Ruling"
              and cards22[0]["answer"] == write_in_ans
              and cards22[0]["multi_select"] is True
              and cards22[0]["free_text"] is True,
              cards22)

        _, b23, o23 = ag_case("b23", [
            make_planner_record(30, "Select tasks", tool_calls=multi_ask_call, created_at=T0),
            make_tool_result_record(31, "Created At: %s\nCompleted At: %s\nA1: None of the above, skip for now\n" % (T0, T1), T1),
        ])
        rep23 = tc.run(None, "antigravity", o23, antigravity_dir=b23)
        cards23 = [json.loads(line) for line in open(os.path.join(o23, "cards.jsonl"), encoding="utf-8")]
        check("A23 modal A1 value matching none of the declared options yields no Ruling",
              rep23["new_cards"] == 0 and len(cards23) == 0
              and rep23["dropped_by_class"].get("antigravity_tool_result", 0) == 1,
              (rep23["new_cards"], rep23["dropped_by_class"]))

        _, b24, o24 = ag_case("b24", [
            make_planner_record(40, "Asking single-select options", tool_calls=ask_call, created_at=T0),
            make_tool_result_record(41, "Created At: %s\nCompleted At: %s\nA1: PostgreSQL, not actually a modal selection\n" % (T0, T1), T1),
        ])
        rep24 = tc.run(None, "antigravity", o24, antigravity_dir=b24)
        cards24 = [json.loads(line) for line in open(os.path.join(o24, "cards.jsonl"), encoding="utf-8")]
        check("A24 single-select modal ask rejects compound A1 string starting with an option",
              rep24["new_cards"] == 0 and len(cards24) == 0
              and rep24["dropped_by_class"].get("antigravity_tool_result", 0) == 1,
              (rep24["new_cards"], rep24["dropped_by_class"]))

        # A25 (kxxx P3-1): secret-shaped write-in text in a multi-select modal Ruling is caught and dropped by redaction
        secret_write_in = two_opt_ans + ", use key sk-ant-api03-abcdef12345678901234567890"
        _, b25, o25 = ag_case("b25", [
            make_planner_record(50, "Select tasks", tool_calls=multi_ask_call, created_at=T0),
            make_tool_result_record(51, "Created At: %s\nCompleted At: %s\nA1: %s\n" % (T0, T1, secret_write_in), T1),
        ])
        rep25 = tc.run(None, "antigravity", o25, antigravity_dir=b25)
        cards25 = [json.loads(line) for line in open(os.path.join(o25, "cards.jsonl"), encoding="utf-8")]
        drops25 = [json.loads(line) for line in open(os.path.join(o25, "drops.jsonl"), encoding="utf-8")]
        check("A25 secret-shaped write-in in multi-select modal Ruling is dropped by redaction",
              rep25["new_cards"] == 0 and len(cards25) == 0
              and rep25["redaction_drops_new"] == 1 and len(drops25) == 1
              and drops25[0]["type"] == "Ruling" and "vendor_key" in drops25[0]["detectors"],
              (rep25["new_cards"], rep25["redaction_drops_new"], drops25))

        # A26 (kxxx P3-2): exact ', ' joiner from a real Antigravity transcript (session 9f8ee4ca, step 9019 -> 9020)
        real_opts_9019 = [
            "(Recommended) Report the 11:05 Vienna (09:05Z) hourly cards run to Kxxx on `transcripts-to-mesh-graph-20261008` once it completes so Kxxx can perform read-back 2 of 3.",
            "(Recommended) Send the drafted Fleet Context Graph proposal (`fleet_context_graph_proposal.md`) to Kxxx on `meshychat-transcript-context-20261010` (or open it as a GitHub issue in `MeshyChat`).",
            "Run the offline behavioral & reconciliation metrics for Issue #238 (Part A1 duplicate-ask rate across seats & Part A3 reconciliation of local store vs server cards).",
            "Start the test server (`:17070`) and run the Issue #290 pre-auth multipart vs JSON memory probe (1 MB, 10 MB, 100 MB peak RSS measurement).",
            "Refute `mux_watchdog.ps1`'s stale `wezterm#7527` PDU comment and wire `mux_crash_ledger.py` into the watchdog so any future `0xc00000fd` crash is automatically logged to `mux_crash_ledger.jsonl`.",
            "Implement the optional `--from-peer <peer_id_or_label>` sender-pinning flag on `meshy_node pickup` from Issue #198 item 1 (with tests).",
        ]
        real_content_9020 = (
            "Created At: 2026-10-14T11:03:56+02:00\n"
            "Completed At: 2026-10-14T11:04:46+02:00\n"
            "A1: " + ", ".join(real_opts_9019 + ["proceed"])
        )
        _, b26, o26 = ag_case("b26", [
            make_planner_record(9019, "Asking options", tool_calls=[{
                "name": "ask_question",
                "args": {"questions": [{"question": "Which tasks next?", "options": real_opts_9019, "is_multi_select": True}]},
            }], created_at="2026-10-14T09:03:56Z"),
            make_tool_result_record(9020, real_content_9020, "2026-10-14T09:04:46Z"),
        ])
        rep26 = tc.run(None, "antigravity", o26, antigravity_dir=b26)
        cards26 = [json.loads(line) for line in open(os.path.join(o26, "cards.jsonl"), encoding="utf-8")]
        check("A26 real Antigravity step 9019->9020 ', ' joiner produces Ruling with write-in tail",
              rep26["new_cards"] == 1 and len(cards26) == 1
              and cards26[0]["type"] == "Ruling"
              and cards26[0]["answer"] == ", ".join(real_opts_9019 + ["proceed"])
              and cards26[0]["free_text"] is True,
              cards26)

    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(RESULTS), len(failed)))
    return 1 if failed else 0


def test_antigravity_dialect():
    if not RESULTS:
        run_checks()
    assert all(ok for _, ok, _ in RESULTS), [name for name, ok, _ in RESULTS if not ok]


if __name__ == "__main__":
    sys.exit(run_checks())
