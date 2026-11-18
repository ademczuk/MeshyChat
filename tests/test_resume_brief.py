"""resume-brief (tools/meshy_node/resume_brief.py): a fresh session's brief, from fixtures only (no server, no gh)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools.meshy_node import resume_brief as rb

NOW = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)


def _row(key, author, hours, state="open", thread="t-1", msg_type="request", done_when=None):
    return {"hit": {"key": key, "author": author, "dst": "kxxx",
                    "recorded_at": (NOW - timedelta(hours=hours)).isoformat()},
            "thread_id": thread, "state": state, "msg_type": msg_type, "done_when": done_when}


OWED = {"owed": [
    _row("msg/3-uxxx-c", "uxxx", 2, thread="mg479"),
    _row("msg/1-txxxx-a", "txxxx", 30, state="active", done_when="merged"),
    _row("msg/2-pxx-b", "pxx", 5, state="closed"),  # closed: never shown
    _row("msg/4-dxxxxx-d", "dxxxxx", 1, state="answered"),  # answered: not owed
], "truncated": False}
AWAITING = {"awaiting": [{**_row("msg/5-kxxx-e", "kxxx", 3, thread="pr-480"), "owing_label": "memory-bxxxxx"}]}
HITS = {"hits": [
    {"item_keys": ["ctx/kxxx/s/0001/000001/0000000001"], "time_last": (NOW - timedelta(hours=2)).isoformat(),
     "text": "[assistant_text] wrote the size note"},
    {"item_keys": ["ctx/kxxx/s/0001/000001/0000000002"], "time_last": (NOW - timedelta(minutes=10)).isoformat(),
     "text": "[assistant_text] running the mod tests\nat low priority"},
    {"item_keys": ["ctx/kxxx/s/0001/000001/0000000003"], "time_last": (NOW - timedelta(minutes=5)).isoformat(),
     "text": "[user_text] <task-notification> <task-id>b1</task-id>"},
], "coverage": {"path": "exact"}}


def _gh(prs=None, states=None, fail=False):
    calls = []

    def gh(argv):
        calls.append(argv)
        if fail:
            raise RuntimeError("gh: not logged in")
        if argv[:2] == ["pr", "list"]:
            return prs if prs is not None else [{"number": 481, "headRefName": "kxxx/resume-brief", "headRefOid": "abcdef0123"},
                                                {"number": 470, "headRefName": "txxxx/x", "headRefOid": "1111111111"}]
        n = int(argv[2])
        return {"number": n, "state": (states or {}).get(n, "OPEN"), "headRefOid": "9" * 40}
    gh.calls = calls
    return gh


def _build(budget=6000, owed=OWED, awaiting=AWAITING, hits=HITS, gh=None, searches=None, **kw):
    def search(**kwargs):
        if searches is not None:
            searches.append(kwargs)
        if isinstance(hits, Exception):
            raise hits
        return hits

    def fn(v):
        def f():
            if isinstance(v, Exception):
                raise v
            return v
        return f
    return rb.build("kxxx", budget=budget, now=NOW, owed_fn=fn(owed), awaiting_fn=fn(awaiting), search_fn=search,
                    gh=gh or _gh(), **kw)


def test_sections_in_order_with_pointers_and_no_closed_or_answered_ask():
    out = _build()
    lines = out.splitlines()
    assert lines[0].startswith("resume brief for kxxx, 08:00 Vienna (2026-11-02T07:00:00Z)")
    # pxx 93204b: said at every session start and compaction, before any section, and never cut for the budget
    assert lines[1] == rb.NO_TOOLS_LINE and "never call mcp__plugin_meshy_meshy__channel" in lines[1]
    assert "every read, fetch and send is tools.meshy_node" in lines[1] and lines[1].startswith("  ")
    assert _build(budget=rb.MIN_BUDGET).splitlines()[1] == rb.NO_TOOLS_LINE        # also at the smallest budget
    # the header, the line and the footer's room fit the smallest budget for a seat name of forty characters
    assert len(lines[0]) + 40 + 1 + len(rb.NO_TOOLS_LINE) + 1 + rb.FOOTER_ROOM <= rb.MIN_BUDGET
    # one sentence in two places: the channel's standing instructions say the same
    import importlib.util
    import pathlib
    import sys as _sys
    pb = pathlib.Path(rb.__file__).resolve().parents[1] / "postbox"
    _sys.path.insert(0, str(pb))
    try:
        spec = importlib.util.spec_from_file_location("meshy_channel_for_brief", pb / "meshy_channel.py")
        ch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ch)
    finally:
        _sys.path.remove(str(pb))
    assert ch.NO_TOOLS in ch.MAIL_INSTRUCTIONS and ch.MAIL_INSTRUCTIONS.count("mcp__plugin_meshy_meshy__channel") == 1
    for part in ("exposes no tools", "never call mcp__plugin_meshy_meshy__channel", "every read, fetch and send is tools.meshy_node"):
        assert part in ch.NO_TOOLS and part in rb.NO_TOOLS_LINE, part
    titles = [ln for ln in lines if not ln.startswith("  ")][1:]
    assert [t.split(" ")[0] for t in titles] == ["handover", "owed", "replied", "waiting", "pull", "your"]
    # SD 255: the open ask is owed; the active one (posted in its thread, not as a reply) is listed on its own
    assert "owed (reply expected from you, oldest first) 1:" in lines
    assert "replied in thread, not linked (read each; reply or --closes it, oldest first) 1:" in lines
    owed = [ln for ln in lines if ln.startswith("  msg/") and " from " in ln]
    assert [ln.split()[0] for ln in owed] == ["msg/3-uxxx-c", "msg/1-txxxx-a"]  # the owed section, then the replied one
    assert ", open, " in owed[0] and ", active, " in owed[1] and "has a done-when" in owed[1]
    assert lines.index(owed[1]) > lines.index("replied in thread, not linked (read each; reply or --closes it, "
                                              "oldest first) 1:")
    assert "msg/2-pxx-b" not in out and "msg/4-dxxxxx-d" not in out  # a close removes an ask; answered is not owed
    assert "  msg/5-kxxx-e on memory-bxxxxx, 3h, open" in lines


def test_a_merged_pr_ask_reads_likely_done_and_stays_counted():
    out = _build(gh=_gh(states={479: "MERGED", 480: "OPEN"}))
    line = next(ln for ln in out.splitlines() if ln.startswith("  msg/3-uxxx-c"))
    assert line.endswith("likely done (MeshyChat PR #479 MERGED), close with --closes msg/3-uxxx-c?")
    assert "owed (reply expected from you, oldest first) 1:" in out  # still counted (bf2506 amendment 2)
    # CONTROL: the same ask with its PR open is not called done
    out = _build(gh=_gh(states={479: "OPEN"}))
    assert "likely done" not in out


def test_prs_by_lane_prefix_and_by_thread_id_with_their_role():
    gh = _gh(states={479: "MERGED", 480: "OPEN"})
    out = _build(gh=gh)
    assert "  PR #481 OPEN head abcdef01 (yours, branch kxxx/resume-brief)" in out
    assert "  PR #479 MERGED head 99999999 (owed in thread mg479, msg/3-uxxx-c)" in out
    assert "  PR #480 OPEN head 99999999 (waiting in thread pr-480, msg/5-kxxx-e)" in out
    assert "#470" not in out  # another seat's branch
    assert ["pr", "view", "481"] not in [c[:3] for c in gh.calls]  # a listed PR is not asked again


def test_waiting_rows_in_the_servers_real_shape_print_no_thread():
    """The server's awaiting rows carry no thread_id (apps/mesh/graph.AwaitingAck): the brief must not print
    "thread -" on each of them, nor look up a PR from a thread it was never given (Codex on #480)."""
    real = {"awaiting": [{"hit": {"key": "msg/9-kxxx-z", "author": "kxxx", "dst": "pxx",
                                  "recorded_at": (NOW - timedelta(hours=4)).isoformat()},
                          "state": "open", "owing_label": "pxx"}]}  # no thread_id, as AwaitingAck serializes
    gh = _gh()
    out = _build(awaiting=real, gh=gh)
    assert "  msg/9-kxxx-z on pxx, 4h, open" in out.splitlines()
    assert "thread -" not in out
    views = [c[2] for c in gh.calls if c[:2] == ["pr", "view"]]
    assert views == ["479"]  # only the PR in the owed ask's thread (mg479); the waiting ask gave no thread


def test_gh_failure_is_said_not_silent():
    out = _build(gh=_gh(fail=True))
    assert "gh pr list failed (RuntimeError: gh: not logged in)" in out
    assert "PR #479 ? (gh failed: RuntimeError)" in out


def test_recent_work_is_newest_first_one_line_and_skips_harness_events():
    out = _build()
    rows = [ln for ln in out.splitlines() if " ago ctx/" in ln]
    assert rows == ["  10m ago ctx/kxxx/s/0001/000001/0000000002: [assistant_text] running the mod tests at low priority",
                    "  2h ago ctx/kxxx/s/0001/000001/0000000001: [assistant_text] wrote the size note"]
    assert "(1 harness event(s) skipped)" in out


def _kind_hit(n, minutes, kinds, text):
    return {"item_keys": [f"ctx/kxxx/s/0001/000001/{n:010d}"], "time_last": (NOW - timedelta(minutes=minutes)).isoformat(),
            "item_kinds": kinds, "text": text}


def test_recent_work_keeps_only_the_seats_own_words_when_the_server_names_item_kinds():
    """#480 P3: a live brief's 8 rows were all slices of tool calls and their output. With the server's item_kinds the
    brief keeps the seat's own assistant text: a hit cut from inside a tool exchange is skipped and counted, and a hit
    that crosses from tool output into the seat's answer shows only the answer."""
    hits = {"hits": [
        _kind_hit(1, 30, ["tool_exchange"], '","in_reply_to": null, "requires_ack": true'),
        _kind_hit(2, 20, ["tool_exchange", "assistant_text"],
                  "exit 0, 10271 passed\n\n[assistant_text]\nGate passed; merging #500 now."),
        _kind_hit(3, 10, ["assistant_text"], "the F4 red control is written and r4 accepts it"),
        _kind_hit(4, 5, ["user_text"], "a peer's message quoted into the turn"),
    ], "coverage": {"path": "exact"}}
    out = _build(hits=hits)
    rows = [ln for ln in out.splitlines() if " ago ctx/" in ln]
    assert rows == ["  10m ago ctx/kxxx/s/0001/000001/0000000003: the F4 red control is written and r4 accepts it",
                    "  20m ago ctx/kxxx/s/0001/000001/0000000002: Gate passed; merging #500 now."]
    assert "(2 hit(s) without your own words skipped)" in out
    # CONTROL: a server without item_kinds (before this change) still shows every hit's text, as before
    old = {"hits": [{k: v for k, v in h.items() if k != "item_kinds"} for h in hits["hits"]]}
    assert sum(" ago ctx/" in ln for ln in _build(hits=old).splitlines()) == 4


def test_own_words_stop_at_every_item_kind_the_server_stores():
    """kxxx on #502, P2-502-1: a task item (a TodoWrite or update_plan step) follows the seat's assistant_text, and a
    label list that missed `task` quoted the plan step as the seat's words. The probe is kxxx's, verbatim."""
    hit = _kind_hit(1, 5, ["user_text", "assistant_text", "task", "tool_exchange"],
                    "go\n\n[assistant_text]\nPlan updated; starting on the gate fix.\n\n[task]\n"
                    "plan step completed: run the gate\n\n[tool_exchange]\nls -la")
    assert rb.own_words(hit) == "Plan updated; starting on the gate fix."
    hit = _kind_hit(2, 5, ["assistant_text", "artifact"], "done\n\n[artifact]\nnot mine")
    assert rb.own_words(hit) == "done"


def test_a_label_inside_an_item_is_not_an_item():
    """Codex on #502 (and kxxx's P3-502-3): tool output or a user message that quotes a transcript holds a blank line
    and "[assistant_text]". item_kinds decides what the items are, so the quote is never the seat's words."""
    quoted = "here is the excerpt\n\n[assistant_text]\nnot my words"
    assert rb.own_words(_kind_hit(1, 5, ["tool_exchange"], quoted)) is None
    assert rb.own_words(_kind_hit(2, 5, ["user_text"], quoted)) is None
    # a quote inside the tool output before the seat's real item stays with the tool output
    hit = _kind_hit(3, 5, ["tool_exchange", "tool_exchange", "assistant_text"],
                    quoted + "\n\n[tool_exchange]\nls\n\n[assistant_text]\nMy own words.")
    assert rb.own_words(hit) == "My own words."
    # the seat quoting a label of a kind that is not the next item keeps its words whole
    hit = _kind_hit(4, 5, ["assistant_text"], "The label reads\n\n[tool_exchange]\nin the chunk text.")
    assert rb.own_words(hit) == "The label reads\n\n[tool_exchange]\nin the chunk text."
    assert rb.own_words(_kind_hit(5, 5, [], "nothing to go on")) is None


@pytest.mark.parametrize("skipped", [None, "omitted"])
def test_an_item_the_text_never_holds_does_not_put_the_walk_out_of_step(skipped):
    """kxxx on #502, P2-502-4: item_kinds lists every ordinal, and turn_chunks leaves omitted and blank items out of the
    text. Control: the same text with no such entry gives the same words."""
    text = 'go\n\n[assistant_text]\nChecking the log now.\n\n[tool_exchange]\nTOOL OUTPUT\n\n[assistant_text]\nThe log is clean.'
    kinds = ["user_text", "assistant_text", "tool_exchange", "assistant_text"]
    assert rb.own_words(_kind_hit(1, 5, kinds, text)) == "Checking the log now. The log is clean."
    assert rb.own_words(_kind_hit(2, 5, kinds[:2] + [skipped] + kinds[2:], text)) == "Checking the log now. The log is clean."


def test_the_brief_knows_the_same_item_kinds_as_the_server():
    from apps.context import validation
    assert set(rb.ITEM_KINDS) == set(validation.ITEM_KINDS)


def test_a_chunk_that_starts_inside_a_label_drops_the_fragment():
    """#502 P3: chunk starts fall at any offset, so a chunk can begin on the join's second newline or inside its first
    item's label; the fragment is not the seat's words."""
    for lead in ("\n[assistant_text]\n", "[assistant_text]\n", "sistant_text]\n", "]\n"):
        assert rb.own_words(_kind_hit(1, 5, ["assistant_text"], lead + "Merged #500.")) == "Merged #500.", repr(lead)
    # CONTROL: text that merely starts with a bracket is kept whole
    assert rb.own_words(_kind_hit(1, 5, ["assistant_text"], "[draft] Merged #500.")) == "[draft] Merged #500."


def test_recent_work_says_so_when_no_hit_holds_the_seats_own_words():
    hits = {"hits": [_kind_hit(1, 30, ["tool_exchange"], "stdout tail"), _kind_hit(2, 20, ["user_text"], "a question")]}
    out = _build(hits=hits)
    assert "holds your own words; only tool output and other turns matched" in out
    assert " ago ctx/" not in out


def test_the_search_always_names_the_seat_and_a_since_and_is_refused_without_a_seat():
    searches = []
    _build(searches=searches)
    assert searches == [{"q": rb.RECENT_QUERY, "seat": "kxxx", "since": "2026-11-02T01:00:00Z", "k": rb.RECENT_K}]
    called = []
    sec = rb.recent_section("", lambda **kw: called.append(kw) or HITS, NOW)
    assert sec.status.startswith("refused: a transcript search needs this seat's name")
    assert called == []  # never run broad
    sec = rb.recent_section("kxxx/../x", lambda **kw: called.append(kw) or HITS, NOW)
    assert sec.status.startswith("refused") and called == []


@pytest.mark.parametrize("hits, says", [
    ({"hits": [], "coverage": {"path": "exact", "scored": 0}}, "search answered no hits for this seat since 2026-11-02T01:00:00Z"),
    (RuntimeError("HTTP 503"), "search failed (RuntimeError: HTTP 503); nothing is known about this window"),
    ({"hits": [HITS["hits"][2]]}, "only harness events since 2026-11-02T01:00:00Z"),
])
def test_an_empty_or_failed_search_is_reported_never_silence(hits, says):
    out = _build(hits=hits)
    assert says in out


def test_a_failed_owed_or_awaiting_source_is_said():
    out = _build(owed=RuntimeError("timed out"), awaiting=OSError("refused"))
    assert "owed (reply expected from you, oldest first): unavailable (RuntimeError: timed out)" in out
    assert ("replied in thread, not linked (read each; reply or --closes it, oldest first): unavailable "
            "(RuntimeError: timed out)") in out, "the second section of the same source says so too, never 'none'"
    assert "waiting on (your asks others owe): unavailable (OSError: refused)" in out
    out = _build(owed={"owed": [], "truncated": True}, awaiting={"awaiting": []})
    assert "owed (reply expected from you, oldest first): none open in the last 14 days (the server's list was cut)" in out
    assert ("replied in thread, not linked (read each; reply or --closes it, oldest first): none in the last 14 days "
            "(the server's list was cut)") in out


def test_a_cut_awaiting_list_is_said_never_shown_as_the_full_count():
    """Codex on #480 (resume_brief.py:181): `awaiting` returns `truncated: true` past its page; the count gets a `+`
    as owed's does, and an empty cut page says it was cut. CONTROL: an uncut page carries neither."""
    cut = {**AWAITING, "truncated": True}
    assert "waiting on (your asks others owe) 1+" in _build(awaiting=cut)
    assert "waiting on (your asks others owe) 1+" not in _build(awaiting=AWAITING)
    assert "waiting on (your asks others owe) 1" in _build(awaiting=AWAITING)
    out = _build(awaiting={"awaiting": [], "truncated": True})
    assert "waiting on (your asks others owe): none open or active in the last 14 days (the server's list was cut)" in out


@pytest.mark.parametrize("budget", list(range(rb.MIN_BUDGET, 2400, 37)) + [800, 6000])
def test_the_budget_is_honored_and_no_line_is_cut(budget):
    full = _build(budget=100_000).splitlines()
    out = _build(budget=budget)
    assert len(out) <= budget
    for ln in out.splitlines():
        assert ln in full or ln.startswith("  (+") or ln.startswith("cut for budget: ")
    if out != "\n".join(full):
        assert any(ln.startswith(("  (+", "cut for budget: ")) for ln in out.splitlines())  # a cut is always said


@pytest.mark.parametrize("budget", range(rb.MIN_BUDGET, 1600, 7))
def test_the_budget_holds_when_rows_are_counted_and_sections_cut_together(budget):
    many = {"owed": [_row(f"msg/{i}-uxxx-x", "uxxx", 50 - i, thread=f"long-thread-name-{i:03d}") for i in range(40)]}
    out = _build(budget=budget, owed=many)
    assert len(out) <= budget


def test_rows_that_do_not_fit_are_counted_with_their_command():
    many = {"owed": [_row(f"msg/{i}-uxxx-x", "uxxx", 50 - i) for i in range(40)], "truncated": False}
    out = _build(budget=900, owed=many)
    more = [ln for ln in out.splitlines() if ln.startswith("  (+")]
    assert more and more[0].endswith(" more: run meshy_node owed)")
    shown = sum(1 for ln in out.splitlines() if ln.startswith("  msg/") and "-uxxx-x" in ln)
    assert more[0].startswith(f"  (+{40 - shown} more")
    assert "cut for budget: " in out  # the later sections did not fit and are named


def test_a_budget_below_the_floor_is_refused():
    with pytest.raises(ValueError):
        _build(budget=rb.MIN_BUDGET - 1)


def test_handover_file_next_action_and_gate_file_freshness(tmp_path):
    h = tmp_path / "HANDOVER-kxxx.md"
    h.write_text("# handover\nNEXT ACTION: finish the resume brief PR\nRESTORE\n", encoding="utf-8")
    g = tmp_path / "hqb-gate.json"
    g.write_text(json.dumps({"updated_at": (NOW - timedelta(minutes=2)).isoformat(), "verdict": "PASS", "head": "abc", "noise": 1}), encoding="utf-8")
    out = _build(handover_file=h, gate_file=g)
    assert "  NEXT ACTION: finish the resume brief PR" in out
    assert 'gate: hqb-gate.json updated 2m ago: {"head": "abc", "verdict": "PASS"}' in out
    g.write_text(json.dumps({"updated_at": (NOW - timedelta(minutes=6)).isoformat(), "verdict": "PASS"}), encoding="utf-8")
    assert "gate: unavailable (hqb-gate.json stale, updated_at" in _build(gate_file=g)
    assert "gate: unavailable (missing.json missing)" in _build(gate_file=tmp_path / "missing.json")
    h.write_text("# handover\n" + "x\n" * 20 + "NEXT ACTION: too late\n", encoding="utf-8")
    assert "no NEXT ACTION line in its first 15 lines" in _build(handover_file=h)


def test_the_gate_line_shows_hqbs_queue_and_last_result(tmp_path):
    """#480 P3: HQB's hqb-gate.json carries running, queue and recent, and the line showed only {"running": null}."""
    g = tmp_path / "hqb-gate.json"
    doc = {"updated_at": (NOW - timedelta(minutes=2)).isoformat(), "host": "HQB", "running": None,
           "queue": [{"tag": "mg501", "pr": 501}, {"tag": "mg502", "pr": 502}],
           "recent": [{"tag": "mgbatch10", "pr": 490, "verdict": "PASS", "finished_at": (NOW - timedelta(hours=3)).isoformat()},
                      {"tag": "mg500", "pr": 500, "verdict": "REFUSED", "finished_at": (NOW - timedelta(minutes=40)).isoformat()}]}
    g.write_text(json.dumps(doc), encoding="utf-8")
    out = _build(gate_file=g)
    assert ("gate: hqb-gate.json updated 2m ago: nothing running; queue 2: mg501 PR #501, mg502 PR #502; "
            "last mg500 PR #500 REFUSED, finished 40m ago") in out
    doc["running"], doc["queue"] = {"tag": "mg503", "pr": 503}, []
    g.write_text(json.dumps(doc), encoding="utf-8")
    assert "hqb-gate.json updated 2m ago: running mg503 PR #503; queue 0; last mg500 PR #500 REFUSED" in _build(gate_file=g)


def test_the_handover_next_action_survives_the_startup_budget(tmp_path):
    """memory-bxxxxx's P2-1 on #480: at --budget 800 nine 12-day-old waiting rows filled the brief and the handover's
    NEXT ACTION, then the last section, was the first one cut."""
    h = tmp_path / "HANDOVER-memory-bxxxxx.md"
    h.write_text("# handover\nNEXT ACTION: re-check #480 on the new head after the full gate, then merge\n", encoding="utf-8")
    waiting = {"awaiting": [{**_row(f"msg/179250{i}000000-memory-bxxxxx-{i:06x}", "memory-bxxxxx", 288 + i,
                                    thread=f"seat-handover-long-thread-name-{i}"), "owing_label": "antigravity"}
                            for i in range(9)]}
    out = _build(budget=800, awaiting=waiting, handover_file=h)
    assert len(out) <= 800
    assert "  NEXT ACTION: re-check #480 on the new head after the full gate, then merge" in out
    assert "handover file" not in "".join(ln for ln in out.splitlines() if ln.startswith("cut for budget: "))
    # CONTROL: the waiting rows really overflow at this budget, so the test is about the cut and not a roomy budget
    assert any(ln.startswith(("  (+", "cut for budget: ")) for ln in out.splitlines())


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_the_hook_deadline_starts_no_source_after_its_soft_end():
    """P2-2: one deadline for the run. A source that uses up the time leaves the later ones unstarted, each said."""
    clock = _Clock()
    dl = rb.Deadline(10, 15, clock=clock)
    called = []

    def slow_owed():
        called.append("owed")
        clock.t += 11  # past the soft end
        return OWED

    gh = _gh()
    out = rb.build("kxxx", budget=6000, now=NOW, deadline=dl, owed_fn=slow_owed,
                   awaiting_fn=lambda: called.append("awaiting") or AWAITING,
                   search_fn=lambda **kw: called.append("search") or HITS, gh=gh)
    assert called == ["owed"]
    assert gh.calls == []  # the PR list and views were not started either
    assert "waiting on (your asks others owe): unavailable (TimeoutError: not started: the 10 s hook deadline passed before awaiting)" in out
    assert "search failed (TimeoutError: not started: the 10 s hook deadline passed before the transcript search)" in out
    assert "gh pr list failed (TimeoutError: not started" in out
    assert "owed (reply expected from you, oldest first) 1:" in out  # what finished in time is still shown
    assert "replied in thread, not linked (read each; reply or --closes it, oldest first) 1:" in out
    # CONTROL: with time left every source runs
    called.clear()
    rb.build("kxxx", budget=6000, now=NOW, deadline=rb.Deadline(10, 15, clock=_Clock()), owed_fn=lambda: called.append("owed") or OWED,
             awaiting_fn=lambda: called.append("awaiting") or AWAITING, search_fn=lambda **kw: called.append("search") or HITS, gh=_gh())
    assert called == ["owed", "awaiting", "search"]


def test_a_call_that_starts_gets_only_the_time_left_before_the_hard_end():
    clock = _Clock()
    dl = rb.Deadline(18, 25, clock=clock)
    assert dl.cap(rb.GH_TIMEOUT_S) == rb.GH_TIMEOUT_S
    clock.t += 17
    assert not dl.expired() and dl.cap(rb.GH_TIMEOUT_S) == pytest.approx(8)
    clock.t += 1
    assert dl.expired()
    clock.t += 10
    assert dl.cap(rb.GH_TIMEOUT_S) == 0.5
    with pytest.raises(ValueError):
        rb.Deadline(5, 4)


def test_the_hook_gives_up_at_the_hard_end_with_one_line(monkeypatch, capsys):
    """A source that hangs past its own timeout cannot hold the session's start past HOOK_HARD_S."""
    import threading
    import time
    from types import SimpleNamespace
    release = threading.Event()
    monkeypatch.setattr(rb, "HOOK_SOFT_S", 0.2)
    monkeypatch.setattr(rb, "HOOK_HARD_S", 0.4)
    monkeypatch.setattr(rb, "build", lambda *a, **kw: release.wait(30) and "never")
    args = SimpleNamespace(seat="kxxx", budget=800, repo=rb.DEFAULT_REPO, gate_file=None, handover_file=None,
                           state_file="s.json", hook=True)
    t0 = time.monotonic()
    try:
        assert rb.run(args, {}, None, None) == 0
    finally:
        release.set()
    assert time.monotonic() - t0 < 5
    out = capsys.readouterr().out.splitlines()
    assert out == ["resume-brief: unavailable (over the 0.4 s hook deadline; run it without --hook to wait for it)"]
    # CONTROL: a build that finishes in time prints the brief, not the line
    monkeypatch.setattr(rb, "build", lambda *a, **kw: "resume brief for kxxx")
    assert rb.run(args, {}, None, None) == 0
    assert capsys.readouterr().out.splitlines() == ["resume brief for kxxx"]


def test_the_hook_form_never_fails_the_session(tmp_path, capsys):
    from tools.meshy_node import cli
    argv = ["resume-brief", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "missing-state.json"),
            "--seat", "kxxx", "--budget", "800"]
    assert cli.main(argv + ["--hook"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 and out[0].startswith("resume-brief: unavailable (")
    # CONTROL: without --hook the same failure is an error, not a brief
    try:
        rc = cli.main(argv)
    except (Exception, SystemExit):  # noqa: BLE001 - raising is also "not a brief"
        rc = "raised"
    assert rc != 0
    assert not capsys.readouterr().out.startswith("resume brief for")


@pytest.mark.parametrize("when, clock", [
    (datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc), "08:00"),  # winter, CET
    (datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc), "09:00"),  # summer, CEST
    (datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc), "01:59"),  # last Sunday of March, before 01:00Z
    (datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), "03:00"),  # and after
    (datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc), "02:59"),  # last Sunday of October, before 01:00Z
    (datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc), "02:00"),  # and after
])
def test_vienna_time_follows_the_eu_rule(when, clock):
    assert rb.vienna(when) == clock


def test_prs_in_thread_ids():
    assert rb.prs_in_thread("mg479") == [479]
    assert rb.prs_in_thread("pr-475-review") == [475]
    assert rb.prs_in_thread("review #296") == [296]
    assert rb.prs_in_thread("PluginMarketplace#296") == []  # another repository's PR is not looked up in this one
    assert rb.prs_in_thread("transcripts-all-nodes-20261101") == []  # a date is not a PR
    assert rb.prs_in_thread(None) == []


def test_an_active_ask_is_listed_apart_and_a_row_without_a_state_stays_owed():
    """SD 255 (round 1 item 15): only a row the server calls active leaves the owed section, so one with no state is
    still owed, never hidden; the active one gets the same line, "likely done" included when its PR has merged.
    MUTANT: drop None from the owed section's states; the stateless row vanishes. MUTANT: keep active in the owed
    section; it is counted there as well. MUTANT: leave pr_states out of the replied section; no "likely done"."""
    owed = {"owed": [_row("msg/7-pxx-g", "pxx", 4, state=None),
                     _row("msg/8-uxxx-h", "uxxx", 6, state="active", thread="mg479")]}
    out = _build(owed=owed, gh=_gh(states={479: "MERGED"}))
    assert "owed (reply expected from you, oldest first) 1:" in out and "  msg/7-pxx-g from pxx, 4h, ?, " in out
    assert "replied in thread, not linked (read each; reply or --closes it, oldest first) 1:" in out
    line = next(ln for ln in out.splitlines() if ln.startswith("  msg/8-uxxx-h"))
    assert ", active, " in line and line.endswith("MERGED), close with --closes msg/8-uxxx-h?"), line
