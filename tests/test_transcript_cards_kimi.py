"""Checks for the Kimi dialect of tools/transcript_cards: kimi_dialect.py reads, transcript_cards.py builds.
Needs no database. Every fixture is synthetic.

Run:  python tests/test_transcript_cards_kimi.py     (exit 0 = all pass; prints one line per check)
      pytest tests/test_transcript_cards_kimi.py -q
Mutants: python tools/transcript_cards/mutate_loader.py   (covers this file's rules too)
"""
import json
import os
import shutil
import sys
import tempfile

# The loader is a standalone script, not a package: every seat runs it by path. TRANSCRIPT_CARDS_DIR lets
# tools/transcript_cards/mutate_loader.py point this file at a mutated copy.
LOADER_DIR = os.environ.get("TRANSCRIPT_CARDS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import transcript_cards as tc  # noqa: E402
import kimi_dialect as kd  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)) if detail and not cond else ""))


T0 = 1787064216000  # a fixed epoch-ms; every fixture time derives from it


def msg(mid, text, kind="user", variant=None, name=None, ts=T0):
    """One context.append_message wire record. mid None = an older wire with no message id."""
    origin = {"kind": kind}
    if variant is not None:
        origin["variant"] = variant
    if name is not None:
        origin["name"] = name
    m = {"role": "user", "content": [{"type": "text", "text": text}], "origin": origin}
    if mid is not None:
        m["id"] = mid
    return {"type": "context.append_message", "time": ts, "message": m}


def call(call_id, questions, ts=T0 + 1000):
    return {"type": "context.append_loop_event", "time": ts,
            "event": {"type": "tool.call", "uuid": "ev-" + call_id, "toolCallId": call_id,
                      "name": "AskUserQuestion", "args": {"questions": questions}}}


def result(call_id, answers=None, is_error=False, raw_output=None, ts=T0 + 2000):
    out = raw_output if raw_output is not None else json.dumps({"answers": answers or {}})
    return {"type": "context.append_loop_event", "time": ts,
            "event": {"type": "tool.result", "toolCallId": call_id,
                      "result": {"output": out, "isError": is_error}}}


QS = [{"question": "Deploy on Friday?", "header": "h",
       "options": [{"label": "Deploy", "description": "ship it"}, {"label": "Hold", "description": "wait"}]},
      {"question": "Which namespace?", "header": "h",
       "options": [{"label": "decisions", "description": "the card one"}]},
      {"question": "Never asked?", "header": "h", "options": []}]


def wire(records):
    return [{"type": "metadata", "protocol_version": "1.5", "created_at": T0 - 1000}] + records


def write_jsonl(path, records, mode="w"):
    with open(path, mode, encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def cards_of(out):
    rows = []
    path = os.path.join(out, "cards.jsonl")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    return rows


def home_with(root, records, sub_records=None):
    """A Kimi home with one session's main wire (and a subagent wire when given). Returns the home path."""
    main_dir = os.path.join(root, "khome", "wd_x", "session_00000000-1111-2222-3333-444444444444",
                            "agents", "main")
    os.makedirs(main_dir, exist_ok=True)
    write_jsonl(os.path.join(main_dir, "wire.jsonl"), wire(records))
    if sub_records is not None:
        sub_dir = os.path.join(root, "khome", "wd_x", "session_00000000-1111-2222-3333-444444444444",
                               "agents", "agent-1")
        os.makedirs(sub_dir, exist_ok=True)
        write_jsonl(os.path.join(sub_dir, "wire.jsonl"), wire(sub_records))
    return os.path.join(root, "khome")


def main():
    root = tempfile.mkdtemp(prefix="tcards-kimi-")
    try:
        home = home_with(root, [
            msg("m-typed", "Use the bridge for the reranker from now on."),
            msg("m-goal-inj", "<system-reminder>\nYou are working under an active goal ...\n</system-reminder>",
                kind="injection", variant="goal"),
            msg("m-todo", "<system-reminder>\nThe TodoList tool ...\n</system-reminder>",
                kind="injection", variant="todo_list_reminder"),
            msg("m-cron", "<cron-fire jobId=\"01M\">\n<prompt>check the mesh</prompt>\n</cron-fire>",
                kind="cron_job", ts=T0 + 10),
            msg("m-trig", "continue the goal", kind="system_trigger", name="goal_continuation"),
            msg("m-peer", "From uxxx: the gate is green on the cards tree."),
            msg("m-env", "before <notification id=\"task:x\">noise</notification> after", ts=T0 + 20),
            msg(None, "an id-less prompt of an older wire"),
            {"type": "context.append_message", "time": T0 + 15,
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "an older wire prompt, origin unwritten"}]}},
            {"type": "goal.create", "goalId": "g-1", "objective": "keep the nightly chain on main",
             "time": T0 + 30},
            {"type": "context.apply_compaction", "time": T0 + 40, "summary": "model words"},
            {"type": "llm.request", "time": T0 + 50},
            call("c-1", QS, ts=T0 + 60),
            result("c-1", {"Deploy on Friday?": "Deploy", "Which namespace?": "decisions"}, ts=T0 + 70),
            call("c-disabled", QS[:1], ts=T0 + 80),
            result("c-disabled", is_error=True,
                   raw_output="AskUserQuestion is disabled while auto permission mode is active.", ts=T0 + 90),
            call("c-open", QS[:1], ts=T0 + 100),                    # no result: open at the file's end
        ], sub_records=[msg("m-sub", "dispatch: check the loader, said the parent")])
        rep = tc.run(None, "nxxxxx-prod", os.path.join(root, "o"), kimi_dir=home)
        cards = cards_of(os.path.join(root, "o"))
        by_type = {}
        for c in cards:
            by_type.setdefault(c["type"], []).append(c)
        check("K1 typed, id-less, legacy, goal and envelope-cut prompts plus two answered questions card",
              len(by_type.get("Candidate", [])) == 5 and len(by_type.get("Ruling", [])) == 2,
              [(c["type"], c["text"][:40]) for c in cards])
        texts = "\n".join(c["text"] for c in cards)
        check("K2 the typed prompt, the goal objective and both answers are the cards",
              "Use the bridge for the reranker" in texts and "keep the nightly chain on main" in texts
              and "Deploy on Friday?" in texts and "A: Deploy" in texts and "Which namespace?" in texts)
        check("K3 machine text is gone: goal reminder, cron, trigger, peer relay, compaction, the never-asked",
              not any(w in texts for w in ("active goal", "check the mesh", "continue the goal",
                                           "the gate is green", "model words", "Never asked?")), texts[:300])
        check("K4 drop classes are named per machine kind",
              all(rep["dropped_by_class"].get(k) for k in
                  ("kimi_injection_goal", "kimi_injection_todo_list_reminder", "kimi_cron_job",
                   "kimi_system_trigger", "kimi_compaction", "kimi_bookkeeping", "kimi_question_open")),
              rep["dropped_by_class"])
        check("K5 the disabled ask and the never-asked question leave nothing",
              "c-disabled" not in texts and rep["dropped_by_class"].get("kimi_question_unanswered") == 2,
              rep["dropped_by_class"])
        check("K6 a pasted envelope in typed text is cut from the machine's first word, not carded whole",
              any("before" in c["text"] and "notification" not in c["text"] and "after" not in c["text"]
                  for c in cards), texts[:300])
        check("K7 an id-less prompt still cards, named by session and time",
              any(c["source"]["record_uuid"].startswith("kimi:00000000-1111-2222-3333-444444444444:")
                  for c in by_type.get("Candidate", [])),
              [c["source"]["record_uuid"] for c in by_type.get("Candidate", [])])
        check("K8 a prompt with no origin is kept and flagged legacy",
              any(c.get("legacy_origin") and "origin unwritten" in c["text"] for c in cards),
              [c.get("legacy_origin") for c in cards])
        check("K9 a Ruling names its question call, not the wire record",
              all(c["source"]["record_uuid"] == "c-1" for c in by_type.get("Ruling", [])),
              [c["source"] for c in by_type.get("Ruling", [])])
        check("K10 a subagent wire is not selected, and named by hand its prompts drop",
              rep["files_kimi"] == 1 and rep["files_not_read"] == 1 and "dispatch: check the loader" not in texts,
              (rep["files_kimi"], rep["files_not_read"]))
        rep_sub = tc.run(None, "nxxxxx-prod", os.path.join(root, "o-sub"), kimi_dir=home,
                         files=[os.path.join(home, "wd_x", "session_00000000-1111-2222-3333-444444444444",
                                             "agents", "agent-1", "wire.jsonl")])
        check("K11 a subagent wire named by hand drops every prompt",
              rep_sub["dropped_by_class"].get("kimi_subagent_prompt") == 1 and rep_sub["cards_total"] == 0,
              rep_sub["dropped_by_class"])
        check("K12 the open ask persists in the cursor",
              rep["dropped_by_class"].get("kimi_question_open") == 1, rep["dropped_by_class"])
        wire_path = os.path.join(home, "wd_x", "session_00000000-1111-2222-3333-444444444444",
                                 "agents", "main", "wire.jsonl")
        write_jsonl(wire_path, [result("c-open", {"Deploy on Friday?": "Hold"}, ts=T0 + 3000)], mode="a")
        rep2 = tc.run(None, "nxxxxx-prod", os.path.join(root, "o"), kimi_dir=home)
        late = [c for c in cards_of(os.path.join(root, "o")) if c["type"] == "Ruling" and "A: Hold" in c["text"]]
        check("K13 a result appended after a run ties to its call through the cursor", len(late) == 1,
              rep2["new_cards"])
        rep3 = tc.run(None, "nxxxxx-prod", os.path.join(root, "o"), kimi_dir=home)
        check("K14 a third run adds nothing", rep3["new_cards"] == 0 and rep3["cards_total"] == rep2["cards_total"],
              (rep3["new_cards"], rep2["cards_total"]))
        check("K15 epoch-ms lands as the one server spelling",
              all(c["time"].endswith("Z") and "." in c["time"] for c in cards_of(os.path.join(root, "o"))))
        check("K16 unreadable calls and answers drop named, not crash",
              kd.classify({"type": "context.append_loop_event", "time": T0,
                           "event": {"type": "tool.call", "toolCallId": "c-bad", "name": "AskUserQuestion",
                                     "args": {"questions": "not-a-list"}}},
                          {"session": "s", "asks": {}})[1] == "kimi_question_unreadable"
              and kd.classify({"type": "context.append_loop_event", "time": T0,
                               "event": {"type": "tool.result", "toolCallId": "c-x",
                                         "result": {"output": "not json"}}},
                              {"session": "s", "asks": {"c-x": {"time": T0, "questions": QS[:1],
                                                                "uuid": "u"}}})[1] == "kimi_answer_unreadable")
        check("K17 a wire file is known by its first record, and the dialect file imports nothing exotic",
              kd.file_state(wire_path) is not None
              and kd.file_state(os.path.join(LOADER_DIR, "transcript_cards.py")) is None
              and sorted(set(line.split()[1].split(".")[0] for line in
                             open(os.path.join(LOADER_DIR, "kimi_dialect.py"), encoding="utf-8").read().splitlines()
                             if line.startswith(("import ", "from ")))) == ["datetime", "json", "os"])

        # txxxx's review of 5e8c0e2: the postbox's typed shapes never card, the answer's own time, multi-select
        home2 = home_with(os.path.join(root, "r2"), [
            msg("m-del1", "MeshyChat message for you (1 new): msg/1791-x from kxxx: hold the write"),
            msg("m-del2", "[STEERING] from kxxx (peer review): hold the write", ts=T0 + 10),
            msg("m-del3", "3 MeshyChat deliveries for nxxxxx since the last wake", ts=T0 + 20),
            msg("m-del4", "answering now; MeshyChat message for you (1 new): msg/1791-y", ts=T0 + 30),
            call("c-multi", [{"question": "Which two?", "header": "h", "multi_select": True,
                              "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]}], ts=T0 + 60000),
            result("c-multi", {"Which two?": ["A", "B"]}, ts=T0 + 120000),
        ])
        rep4 = tc.run(None, "nxxxxx-prod", os.path.join(root, "o2"), kimi_dir=home2)
        cards4 = cards_of(os.path.join(root, "o2"))
        check("K18 a delivery typed into a Kimi pane drops whole, even with words in front",
              rep4["cards_total"] == 1
              and rep4["dropped_by_class"].get("postbox_delivery") == 3
              and rep4["dropped_by_class"].get("channel_line_typed") == 1,
              (rep4["cards_total"], rep4["dropped_by_class"]))
        multi = [c for c in cards4 if c["type"] == "Ruling"]
        check("K19 a Ruling takes the answer record's time, not the call's",
              len(multi) == 1 and multi[0]["time"] == kd._time(T0 + 120000)
              and multi[0]["time"] != kd._time(T0 + 60000),
              [c["time"] for c in multi])
        check("K20 a multi-select list answer is the labels joined, not JSON, and not free text",
              len(multi) == 1 and "A: A, B" in multi[0]["text"] and not multi[0].get("free_text"),
              multi[0]["text"][:200] if multi else None)

        # txxxx's merged-tree finding: the card client imports the loader as a package module
        import importlib
        repo_root = os.path.dirname(os.path.dirname(LOADER_DIR))
        sys.path.insert(0, repo_root)
        try:
            pkg_loader = importlib.import_module("tools.transcript_cards.transcript_cards")
            ok = pkg_loader.LOADER_VERSION == tc.LOADER_VERSION and pkg_loader.kimi_dialect is not None
        except Exception as e:
            ok = repr(e)
        finally:
            sys.path.remove(repo_root)
        check("K21 the loader imports as a package module too (the card client's way)",
              ok is True, ok)

        src = open(os.path.join(LOADER_DIR, "transcript_cards.py"), encoding="utf-8").read()
        check("K22 the dialect import's fallback fires only with no parent package (memory-bxxxxx's note)",
              "except ImportError as exc:" in src and "if exc.name is not None:" in src
              and "\n        raise" in src)

        # codex-kxxx-cli's probe (txxxx-confirmed): a replaced wire file must not inherit the old file's
        # open asks. Two lanes: the legitimate append (the answer ties, one Ruling - keeps passing), and
        # the replacement lane (a new generation holding ONLY the answer: no Ruling, the ask cleared).
        home3 = home_with(os.path.join(root, "r3"), [
            call("c-replaced", QS[:1], ts=T0 + 100),
        ])
        wire3 = os.path.join(home3, "wd_x", "session_00000000-1111-2222-3333-444444444444",
                             "agents", "main", "wire.jsonl")
        tc.run(None, "nxxxxx-prod", os.path.join(root, "o3-append"), kimi_dir=home3)
        tc.run(None, "nxxxxx-prod", os.path.join(root, "o3-replace"), kimi_dir=home3)
        write_jsonl(wire3, [result("c-replaced", {"Deploy on Friday?": "Deploy"}, ts=T0 + 200)], mode="a")
        tc.run(None, "nxxxxx-prod", os.path.join(root, "o3-append"), kimi_dir=home3)
        n_append = sum(1 for c in cards_of(os.path.join(root, "o3-append")) if c["type"] == "Ruling")
        # Replacement lane: its pass 1 saw the question; the wire is then REPLACED by a new generation
        # holding only the matching result (its own metadata record, no question in it).
        write_jsonl(wire3, wire([result("c-replaced", {"Deploy on Friday?": "Deploy"}, ts=T0 + 300)]),
                    mode="w")
        r33 = tc.run(None, "nxxxxx-prod", os.path.join(root, "o3-replace"), kimi_dir=home3)
        n_replaced = sum(1 for c in cards_of(os.path.join(root, "o3-replace")) if c["type"] == "Ruling")
        check("K23 a replaced wire file inherits no open asks (codex-kxxx-cli's probe)",
              n_append == 1 and r33["cursor_resets"] == 1 and n_replaced == 0,
              (n_append, r33["cursor_resets"], n_replaced))

        # K24: enrollment split - a bounded frozen copy is read while original-path metadata is given
        # separately (codex-kxxx-cli's ask; txxxx's split). Nothing about the live walk changes.
        scratch = os.path.join(root, "scratch")
        os.makedirs(scratch)
        frozen = os.path.join(scratch, "f.jsonl")
        write_jsonl(frozen, wire([]))
        live_main = os.path.join("C:", "h", "wd_a", "session_aaaaaaaa-0000-0000-0000-000000000001",
                                 "agents", "main", "wire.jsonl")
        live_sub = os.path.join("C:", "h", "wd_a", "session_bbbbbbbb-0000-0000-0000-000000000002",
                                "agents", "agent-9", "wire.jsonl")
        st = kd.file_state(frozen, original_path=live_main)
        check("K24a a frozen copy enrolls with the ORIGINAL path's session and main mark",
              st is not None and st["session"] == "aaaaaaaa-0000-0000-0000-000000000001"
              and st["subagent"] is False, st)
        st2 = kd.file_state(frozen, original_path=live_sub)
        check("K24b an original subagent wire stays excluded", st2 is not None and st2["subagent"] is True, st2)
        check("K24c two sessions do not collide", st["session"] != st2["session"])
        notkimi = os.path.join(scratch, "wire.jsonl")
        write_jsonl(notkimi, [{"type": "user", "uuid": "x", "message": {"role": "user", "content": "hi"}}])
        check("K24d a non-Kimi first record never enrolls, whatever the path",
              kd.file_state(notkimi, original_path=live_main) is None
              and kd.file_state(notkimi) is None)
        check("K24e the live walk is unchanged: no original_path reads the live path, and an off-layout copy "
              "is a main wire (only a literal agents/<dir> marks a subagent, txxxx's step-9 P2)",
              kd.file_state(frozen)["session"] == "" and
              kd.file_state(frozen)["subagent"] is False)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(RESULTS), len(failed)))
    return 1 if failed else 0


def test_kimi_dialect():
    if not RESULTS:
        main()
    assert all(ok for _, ok, _ in RESULTS), [name for name, ok, _ in RESULTS if not ok]


if __name__ == "__main__":
    sys.exit(main())
