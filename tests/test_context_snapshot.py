"""Tests for incremental `doing` transcript scanner and Context snapshot integration."""

import json
import os
from pathlib import Path

import pytest

from apps.cards import validation
from tools.transcript_cards import context_snapshot, transcript_cards


def test_doing_seen_extracts_titles_and_tasks_across_dialects():
    # 1. Claude Code: ai-title entries and TodoWrite tool_use blocks
    s_claude = context_snapshot.DoingSeen()
    for line in (
        {"type": "ai-title", "aiTitle": "Old title"},
        {"type": "ai-title", "aiTitle": "Auth refresh rework"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {
                            "todos": [
                                {"content": "Write tests", "status": "completed", "activeForm": "Writing tests"},
                                {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"},
                            ]
                        },
                    }
                ]
            },
        },
    ):
        s_claude.visit(line)
    assert s_claude.title == "Auth refresh rework"
    assert s_claude.task == "Running tests"
    assert s_claude.has_task_list is True

    # 2. Codex: thread_name and update_plan with JSON-string arguments
    s_codex = context_snapshot.DoingSeen()
    s_codex.visit({"id": "th-1", "thread_name": "Postbox cc bank"})
    s_codex.visit({
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "update_plan",
            "arguments": json.dumps({
                "plan": [
                    {"step": "Map issues", "status": "completed"},
                    {"step": "Spawn subagents", "status": "in_progress"},
                ]
            }),
        },
    })
    assert s_codex.title == "Postbox cc bank"
    assert s_codex.task == "Spawn subagents"
    assert s_codex.has_task_list is True

    # 3. Gemini / Antigravity: write_todos with active/in-progress status and JSON-encoded string args
    s_gemini = context_snapshot.DoingSeen()
    s_gemini.visit({
        "messages": [
            {
                "toolCalls": [
                    {
                        "name": "write_todos",
                        "args": {"todos": [{"description": "Fix login", "status": "in-progress"}]},
                    }
                ]
            }
        ]
    })
    assert s_gemini.task == "Fix login"

    s_ag = context_snapshot.DoingSeen()
    s_ag.visit({
        "type": "PLANNER_RESPONSE",
        "tool_calls": [
            {
                "name": "write_todos",
                "args": {
                    "todos": json.dumps(
                        json.dumps({"todos": [{"title": "Audit bundle custody", "status": "active"}]})
                    )
                },
            }
        ],
    })
    assert s_ag.task == "Audit bundle custody"

    # 4. A later todo list with nothing in progress clears the active task
    s_claude.visit({
        "input": {
            "todos": [
                {"content": "Write tests", "status": "completed"},
                {"content": "Run tests", "status": "completed"},
            ]
        }
    })
    assert s_claude.title == "Auth refresh rework"
    assert s_claude.task is None
    assert s_claude.has_task_list is True


def test_format_doing_combines_deduplicates_and_caps_at_120_chars():
    assert context_snapshot.format_doing(None, None) is None
    assert context_snapshot.format_doing("  ", "") is None
    assert context_snapshot.format_doing("Auth refresh", None) == "Auth refresh"
    assert context_snapshot.format_doing(None, "Running   pytest  suite") == "Running pytest suite"
    assert context_snapshot.format_doing("Auth refresh", "auth REFRESH") == "Auth refresh"
    assert context_snapshot.format_doing("Auth refresh", "Running tests") == "Auth refresh | Running tests"

    long_task = "x" * 150
    clipped = context_snapshot.format_doing("Title", long_task)
    assert len(clipped) == 120
    assert clipped.endswith("...")
    assert clipped.startswith("Title | xxx")


def test_scan_transcript_doing_incremental_cursor_partial_lines_and_whole_json(tmp_path):
    cache = {}
    jsonl_path = tmp_path / "session.jsonl"

    # First write: one complete line + a trailing partial line (no newline yet)
    line1 = '{"type":"ai-title","aiTitle":"First title"}\n'
    partial = '{"type":"ai-title","aiTitle":"Second'
    jsonl_path.write_bytes((line1 + partial).encode("utf-8"))

    seen1 = context_snapshot.scan_transcript_doing(jsonl_path, cursor_cache=cache)
    assert seen1.title == "First title"
    abs_key = os.path.abspath(str(jsonl_path))
    assert cache[abs_key]["offset"] == len(line1.encode("utf-8"))
    assert cache[abs_key]["whole"] is False

    # Complete the second line and append a TodoWrite line, preserving mtime_ns to simulate coarse Windows mtime
    st_before = os.stat(jsonl_path)
    full_content = (
        line1
        + '{"type":"ai-title","aiTitle":"Second title"}\n'
        + '{"input":{"todos":[{"content":"Incremental step","status":"in_progress"}]}}\n'
    )
    jsonl_path.write_bytes(full_content.encode("utf-8"))
    os.utime(jsonl_path, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))

    seen2 = context_snapshot.scan_transcript_doing(jsonl_path, cursor_cache=cache)
    assert seen2.title == "Second title"
    assert seen2.task == "Incremental step"
    assert cache[abs_key]["offset"] == len(full_content.encode("utf-8"))

    # Unchanged mtime_ns and size returns a clone from cache without re-reading
    seen2_cached = context_snapshot.scan_transcript_doing(jsonl_path, cursor_cache=cache)
    assert seen2_cached == seen2 and seen2_cached is not seen2

    # Truncating/rewriting the file to a shorter size resets the cursor and re-scans from 0
    shorter = '{"type":"ai-title","aiTitle":"Reset title"}\n'
    jsonl_path.write_bytes(shorter.encode("utf-8"))
    seen3 = context_snapshot.scan_transcript_doing(jsonl_path, cursor_cache=cache)
    assert seen3.title == "Reset title"
    assert seen3.task is None

    # Whole JSON document mode (Gemini chat.json rewritten in place)
    json_path = tmp_path / "chat.json"
    json_path.write_text(
        json.dumps({
            "messages": [
                {"toolCalls": [{"name": "write_todos", "args": {"todos": [{"description": "Step A", "status": "in_progress"}]}}]}
            ]
        }),
        encoding="utf-8",
    )
    seen_json1 = context_snapshot.scan_transcript_doing(json_path, cursor_cache=cache)
    assert seen_json1.task == "Step A"
    assert cache[os.path.abspath(str(json_path))]["whole"] is True

    json_path.write_text(
        json.dumps({
            "messages": [
                {"toolCalls": [{"name": "write_todos", "args": {"todos": [{"description": "Step B longer text", "status": "active"}]}}]}
            ]
        }),
        encoding="utf-8",
    )
    seen_json2 = context_snapshot.scan_transcript_doing(json_path, cursor_cache=cache)
    assert seen_json2.task == "Step B longer text"


def test_extract_seat_context_and_build_context_card_surface_and_screen_doing(tmp_path):
    tpath = tmp_path / "claude_doing.jsonl"
    records = [
        {"type": "ai-title", "aiTitle": "Postbox bank and doing"},
        {
            "type": "assistant",
            "timestamp": "2026-10-20T07:30:00.000Z",
            "sessionId": "sess-doing-1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git checkout -b antigravity/postbox-bank-and-doing"},
                    },
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {
                            "todos": [
                                {"content": "Implement peek_bank", "status": "completed"},
                                {"content": "Write unit tests", "status": "in_progress", "activeForm": "Writing unit tests"},
                            ]
                        },
                    },
                ]
            },
        },
    ]
    tpath.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    cache = {}
    snap = context_snapshot.extract_seat_context(
        [str(tpath)], "claude", "dxxxxx", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
        doing_cursor_cache=cache,
    )
    assert snap["doing"] == "Postbox bank and doing | Writing unit tests"
    # With no Ruling card or user prompt, doing also populates task
    assert snap["task"] == "Postbox bank and doing | Writing unit tests"

    card, sha1, err = context_snapshot.build_context_card(
        "dxxxxx", "claude", snap,
        loader_version=transcript_cards.LOADER_VERSION,
        detect_fn=transcript_cards.detect,
        utc_instant_fn=transcript_cards.utc_instant,
    )
    assert err is None
    assert "Task: Postbox bank and doing | Writing unit tests" in card["text"]
    validation.validate_card("shared/decisions", f"card/dxxxxx/{card['id']}", card)

    # When a separate Ruling card supplies `task`, `Doing:` is rendered on its own line and hashed into content_sha
    ruling = {
        "type": "Ruling",
        "time": "2026-10-20T07:31:00.000Z",
        "question": "Ship postbox bank?",
        "answer": "Yes",
        "text": "Ruling | owner: operator | seat: dxxxxx | 2026-10-20 07:31\nQ: Ship postbox bank?\nA: Yes",
    }
    snap_with_ruling = context_snapshot.extract_seat_context(
        [str(tpath)], "claude", "dxxxxx", [ruling],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
        doing_cursor_cache=cache,
    )
    assert snap_with_ruling["doing"] == "Postbox bank and doing | Writing unit tests"
    assert snap_with_ruling["task"] != snap_with_ruling["doing"]
    card2, sha2, err2 = context_snapshot.build_context_card(
        "dxxxxx", "claude", snap_with_ruling,
        loader_version=transcript_cards.LOADER_VERSION,
        detect_fn=transcript_cards.detect,
        utc_instant_fn=transcript_cards.utc_instant,
    )
    assert err2 is None and sha2 != sha1
    assert "Doing: Postbox bank and doing | Writing unit tests" in card2["text"]
    validation.validate_card("shared/decisions", f"card/dxxxxx/{card2['id']}", card2)

    # Defense-in-depth: a secret in `doing` is refused by build_context_card
    secret_tok = "ghp_" + "B" * 30
    tainted_snap = dict(snap_with_ruling, doing=f"Leak {secret_tok}")
    bad_card, _, bad_err = context_snapshot.build_context_card(
        "dxxxxx", "claude", tainted_snap,
        loader_version=transcript_cards.LOADER_VERSION,
        detect_fn=transcript_cards.detect,
        utc_instant_fn=transcript_cards.utc_instant,
    )
    assert bad_card is None and bad_err == "redaction:vendor_key"


def test_scan_transcript_doing_resets_cursor_when_file_replaced_with_longer_file(tmp_path):
    tpath = tmp_path / "rotated.jsonl"
    tpath.write_text(
        json.dumps({"type": "ai-title", "aiTitle": "Old Title"}) + "\n",
        encoding="utf-8",
    )
    cache = {}
    seen1 = context_snapshot.scan_transcript_doing(tpath, cursor_cache=cache)
    assert seen1.title == "Old Title"

    # Replace the file with a longer transcript containing a new title and task
    replacement = tmp_path / "rotated.jsonl.tmp"
    replacement.write_text(
        json.dumps({"type": "ai-title", "aiTitle": "New Longer Replacement Title For Session Two"}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {"todos": [{"content": "Replacement Task", "status": "in_progress"}]},
                    }
                ]
            },
        }) + "\n",
        encoding="utf-8",
    )
    os.replace(replacement, tpath)

    seen2 = context_snapshot.scan_transcript_doing(tpath, cursor_cache=cache)
    assert seen2.title == "New Longer Replacement Title For Session Two"
    assert seen2.task == "Replacement Task"


def test_extract_seat_context_antigravity_expanded_records_preserve_chronological_order(tmp_path):
    sess_dir = tmp_path / "conv-123" / ".system_generated" / "logs"
    sess_dir.mkdir(parents=True)
    compact = sess_dir / "transcript.jsonl"
    full = sess_dir / "transcript_full.jsonl"

    step1_compact = {
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "created_at": "2026-10-21T16:00:00Z",
        "truncated_fields": ["tool_calls"],
        "tool_calls": [],
    }
    step1_full = {
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "created_at": "2026-10-21T16:00:00Z",
        "tool_calls": [
            {
                "name": "write_todos",
                "args": {"todos": [{"description": "Older Expanded Task A", "status": "in_progress"}]},
            }
        ],
    }
    step2_compact = {
        "step_index": 2,
        "type": "PLANNER_RESPONSE",
        "created_at": "2026-10-21T16:01:00Z",
        "tool_calls": [
            {
                "name": "write_todos",
                "args": {"todos": [{"description": "Newer Untruncated Task B", "status": "in_progress"}]},
            }
        ],
    }
    compact.write_text(
        json.dumps(step1_compact) + "\n" + json.dumps(step2_compact) + "\n",
        encoding="utf-8",
    )
    full.write_text(
        json.dumps(step1_full) + "\n" + json.dumps(step2_compact) + "\n",
        encoding="utf-8",
    )

    snap = context_snapshot.extract_seat_context(
        [str(compact)], "antigravity", "antigravity", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
        doing_cursor_cache={},
    )
    assert snap["doing"] == "Newer Untruncated Task B"


def test_doing_seen_tolerates_non_string_status_and_clears_on_empty_todo_or_plan(tmp_path):
    tpath = tmp_path / "clear_and_malformed.jsonl"
    records = [
        {"type": "ai-title", "aiTitle": "Session Title"},
        # 1. Malformed non-string status (list/dict) must not raise TypeError
        {"todos": [{"content": "Malformed list status", "status": []}, {"content": "Malformed dict status", "status": {"k": "v"}}]},
        # 2. Active task set
        {"todos": [{"content": "Active Task One", "status": "in_progress"}]},
    ]
    tpath.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    cache = {}
    seen1 = context_snapshot.scan_transcript_doing(tpath, cursor_cache=cache)
    assert seen1.task == "Active Task One"

    # 3. Empty todos list clears active task
    with tpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"todos": []}) + "\n")
    seen2 = context_snapshot.scan_transcript_doing(tpath, cursor_cache=cache)
    assert seen2.has_task_list is True
    assert seen2.task is None

    # 4. Set via Codex JSON-string update_plan, then clear via JSON-string empty plan
    with tpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"name": "update_plan", "arguments": json.dumps({"plan": [{"step": "Plan Step", "status": "in_progress"}]})}) + "\n")
    seen3 = context_snapshot.scan_transcript_doing(tpath, cursor_cache=cache)
    assert seen3.task == "Plan Step"

    with tpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"name": "update_plan", "arguments": json.dumps({"plan": []})}) + "\n")
    seen4 = context_snapshot.scan_transcript_doing(tpath, cursor_cache=cache)
    assert seen4.task is None


def test_doing_seen_ignores_tool_outputs_user_records_and_non_todo_tools(tmp_path):
    tpath = tmp_path / "ignore_fixture_outputs.jsonl"
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "update_plan",
                "arguments": json.dumps({"plan": [{"step": "Real Active Step", "status": "in_progress"}]}),
            },
        },
        # 1. Codex function_call_output returning a fixture with a plan must NOT overwrite the real task
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": json.dumps({"plan": [{"step": "Example task from fixture", "status": "in_progress"}]}),
            },
        },
        # 2. Non-todo tool call (e.g. Write/Edit) writing a fixture with todos must NOT overwrite the real task
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {
                            "file_path": "tests/fixture.json",
                            "content": json.dumps({"todos": [{"content": "Fixture Todo", "status": "in_progress"}]}),
                            "todos": [{"content": "Unexpected Todo Field On Write", "status": "in_progress"}],
                        },
                    }
                ]
            },
        },
        # 3. User/tool_result record containing empty plan must NOT clear the real task
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": json.dumps({"plan": []}),
                    }
                ],
            },
        },
    ]
    tpath.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    seen = context_snapshot.scan_transcript_doing(tpath, cursor_cache={})
    assert seen.task == "Real Active Step"


def test_extract_seat_context_scopes_doing_title_and_task_to_same_session(tmp_path):
    old_sess = tmp_path / "sess_old.jsonl"
    new_sess = tmp_path / "sess_new.jsonl"
    old_sess.write_text(
        json.dumps({"type": "ai-title", "aiTitle": "Old session", "sessionId": "s-old"}) + "\n"
        + json.dumps({
            "type": "assistant",
            "sessionId": "s-old",
            "timestamp": "2026-10-21T15:00:00Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {"todos": [{"content": "Old unfinished task", "status": "in_progress"}]},
                    }
                ]
            },
        }) + "\n",
        encoding="utf-8",
    )
    new_sess.write_text(
        json.dumps({"type": "ai-title", "aiTitle": "New unrelated session", "sessionId": "s-new"}) + "\n"
        + json.dumps({
            "type": "assistant",
            "sessionId": "s-new",
            "timestamp": "2026-10-21T16:00:00Z",
            "message": {"content": [{"type": "text", "text": "Starting fresh session without todos"}]},
        }) + "\n",
        encoding="utf-8",
    )
    os.utime(old_sess, (1000, 1000))
    os.utime(new_sess, (2000, 2000))

    snap = context_snapshot.extract_seat_context(
        [str(old_sess), str(new_sess)], "claude", "dxxxxx", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
        doing_cursor_cache={},
    )
    assert snap["doing"] == "New unrelated session"


def test_whole_json_document_larger_than_first_read_bytes_and_expanded_ag_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(context_snapshot, "DOING_FIRST_READ_BYTES", 256)

    # 1. Whole JSON document (.json) larger than DOING_FIRST_READ_BYTES on initial scan
    big_json = tmp_path / "chat.json"
    big_json.write_text(
        json.dumps({
            "padding": "x" * 1024,
            "messages": [
                {
                    "toolCalls": [
                        {
                            "name": "write_todos",
                            "args": {"todos": [{"description": "Large JSON Todo", "status": "in_progress"}]},
                        }
                    ]
                }
            ],
        }),
        encoding="utf-8",
    )
    seen_big = context_snapshot.scan_transcript_doing(big_json, cursor_cache={})
    assert seen_big.task == "Large JSON Todo"

    # 2. Non-string "type" or "role" (e.g. JSON Schema properties) must not raise TypeError
    seen_schema = context_snapshot.DoingSeen()
    seen_schema.visit({"type": {"nested": "schema"}, "role": ["list_role"]})
    assert seen_schema.task is None

    # 3. Expanded Antigravity task persists in cursor cache even after > TAIL_BYTES of records follow
    monkeypatch.setattr(context_snapshot, "TAIL_BYTES", 512)
    sess_dir = tmp_path / "conv-persist" / ".system_generated" / "logs"
    sess_dir.mkdir(parents=True)
    compact = sess_dir / "transcript.jsonl"
    full = sess_dir / "transcript_full.jsonl"

    step1_compact = {
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "created_at": "2026-10-21T16:00:00Z",
        "truncated_fields": ["tool_calls"],
        "tool_calls": [],
    }
    step1_full = {
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "created_at": "2026-10-21T16:00:00Z",
        "tool_calls": [
            {
                "name": "write_todos",
                "args": {"todos": [{"description": "Persisted Expanded Task", "status": "in_progress"}]},
            }
        ],
    }
    compact.write_text(json.dumps(step1_compact) + "\n", encoding="utf-8")
    full.write_text(json.dumps(step1_full) + "\n", encoding="utf-8")

    cache = {}
    snap1 = context_snapshot.extract_seat_context(
        [str(compact)], "antigravity", "antigravity", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
        doing_cursor_cache=cache,
    )
    assert snap1["doing"] == "Persisted Expanded Task"

    # Append > TAIL_BYTES of unrelated steps so step 1 falls outside _tail_lines()
    with compact.open("a", encoding="utf-8") as fc, full.open("a", encoding="utf-8") as ff:
        for idx in range(2, 20):
            rec = {"step_index": idx, "type": "PLANNER_RESPONSE", "created_at": "2026-10-21T16:05:00Z", "content": "y" * 100}
            fc.write(json.dumps(rec) + "\n")
            ff.write(json.dumps(rec) + "\n")

    snap2 = context_snapshot.extract_seat_context(
        [str(compact)], "antigravity", "antigravity", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
        doing_cursor_cache=cache,
    )
    assert snap2["doing"] == "Persisted Expanded Task"


def test_scan_transcript_doing_resets_on_same_size_in_place_rewrite_and_exact_boundary_seek(tmp_path, monkeypatch):
    """pxx P2-1 and P2-2 on #350:
    1. Same-size in-place rewrite (same inode, same 256-byte head_probe, size == cached offset, newer mtime_ns)
       must reset the cursor and rescan from 0 instead of returning the stale task.
    2. First bounded read (start > 0, prev_offset == 0) landing on an exact record boundary (byte at start-1 is '\\n')
       must not skip the first complete line at `start`.
    """
    # P2-1: same-size in-place rewrite after 256-byte head_probe
    p1 = tmp_path / "inplace_same_size.jsonl"
    pad_line = json.dumps({"type": "padding", "pad": "A" * 300}) + "\n"
    old_line = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "update_plan",
            "arguments": json.dumps({"plan": [{"step": "OLD TASK", "status": "in_progress"}]}),
        },
    }) + "\n"
    new_line = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "update_plan",
            "arguments": json.dumps({"plan": [{"step": "NEW TASK", "status": "in_progress"}]}),
        },
    }) + "\n"
    assert len(old_line) == len(new_line)
    p1.write_text(pad_line + old_line, encoding="utf-8")
    os.utime(p1, ns=(1_000_000_000, 1_000_000_000))

    cache = {}
    seen_old = context_snapshot.scan_transcript_doing(p1, cursor_cache=cache)
    assert seen_old.task == "OLD TASK"

    # Rewrite in place to the exact same byte length and bump mtime_ns
    with p1.open("r+b") as f:
        f.seek(0)
        f.write((pad_line + new_line).encode("utf-8"))
    os.utime(p1, ns=(2_000_000_000, 2_000_000_000))

    seen_new = context_snapshot.scan_transcript_doing(p1, cursor_cache=cache)
    assert seen_new.task == "NEW TASK"

    # P2-a (pxx 52a05e): rewrite that keeps the 256-byte head_probe AND grows past the cached offset
    grown_pad = json.dumps({"type": "padding", "pad": "B" * 40}) + "\n"
    grown_line = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "update_plan",
            "arguments": json.dumps({"plan": [{"step": "GROWN REWRITE TASK", "status": "in_progress"}]}),
        },
    }) + "\n"
    with p1.open("wb") as f:
        # Keeps `pad_line` (300+ bytes > 256-byte head_probe), replaces `new_line` with `grown_line`, and appends `grown_pad`
        f.write((pad_line + grown_line + grown_pad).encode("utf-8"))
    os.utime(p1, ns=(3_000_000_000, 3_000_000_000))

    seen_grown = context_snapshot.scan_transcript_doing(p1, cursor_cache=cache)
    assert seen_grown.task == "GROWN REWRITE TASK"

    # P2-2: first bounded read landing on exact newline boundary (byte at start-1 is '\n')
    p2 = tmp_path / "exact_boundary.jsonl"
    first_30 = b'{"type":"noop","v":"1234567"}\n'
    assert len(first_30) == 30
    inside_line = (json.dumps({
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "update_plan",
            "arguments": json.dumps({"plan": [{"step": "INSIDE WINDOW", "status": "in_progress"}]}),
        },
    }) + "\n").encode("utf-8")
    window_bytes = 512
    monkeypatch.setattr(context_snapshot, "DOING_FIRST_READ_BYTES", window_bytes)
    pad_len = window_bytes - len(inside_line) - len(b'{"type":"pad","v":""}\n')
    assert pad_len > 0
    tail_pad = (b'{"type":"pad","v":"' + (b"P" * pad_len) + b'"}\n')
    assert len(inside_line) + len(tail_pad) == window_bytes
    p2.write_bytes(first_30 + inside_line + tail_pad)

    seen_boundary = context_snapshot.scan_transcript_doing(p2, cursor_cache={})
    assert seen_boundary.task == "INSIDE WINDOW"


def test_sidechain_skipped_short_tail_append_and_persisted_doing_cursor(tmp_path, monkeypatch):
    """GitHub Codex + pxx review on #350 (r4366519995, r4366553654, r4367029158):
    1. Sidechain records (`isSidechain: true`) must never overwrite the parent session's `title` or `task`.
    2. Multiple short appends (< 256 bytes each) must not trigger false `tail_probe_mismatch` resets.
    3. `transcript_cards.run(..., emit_context=True)` persists `doing_cursors` in `cursor.json` so a fresh
       process keeps the earlier title/task even after > DOING_FIRST_READ_BYTES of non-todo lines are appended.
    """
    # 1. Parent TodoWrite followed by a sidechain TodoWrite (`isSidechain: true`)
    s = context_snapshot.DoingSeen()
    s.visit({"type": "ai-title", "aiTitle": "Parent Title"})
    s.visit({
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "TodoWrite",
                    "input": {"todos": [{"content": "Parent Active Task", "status": "in_progress"}]},
                }
            ]
        },
    })
    s.visit({
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "TodoWrite",
                    "input": {"todos": [{"content": "Subagent Sidechain Task", "status": "in_progress"}]},
                }
            ]
        },
    })
    s.visit({"type": "ai-title", "aiTitle": "Sidechain Title", "isSidechain": True})
    assert s.title == "Parent Title"
    assert s.task == "Parent Active Task"

    # 2. Short tail probes (< 256 bytes appended on scan 2, followed by another append on scan 3)
    p_short = tmp_path / "short_appends.jsonl"
    monkeypatch.setattr(context_snapshot, "DOING_FIRST_READ_BYTES", 512)
    initial_lines = (
        json.dumps({"type": "ai-title", "aiTitle": "Persistent Title"}) + "\n"
        + json.dumps({"input": {"todos": [{"content": "Early Task", "status": "in_progress"}]}}) + "\n"
        + json.dumps({"type": "pad", "v": "X" * 600}) + "\n"
    )
    p_short.write_text(initial_lines, encoding="utf-8")
    cache = {}
    # Seed from 0 by setting DOING_FIRST_READ_BYTES large for first read, then 512 for subsequent reads
    monkeypatch.setattr(context_snapshot, "DOING_FIRST_READ_BYTES", 4096)
    seen1 = context_snapshot.scan_transcript_doing(p_short, cursor_cache=cache)
    assert seen1.title == "Persistent Title"
    assert seen1.task == "Early Task"

    monkeypatch.setattr(context_snapshot, "DOING_FIRST_READ_BYTES", 512)
    # Append < 256 bytes (e.g. 40 bytes)
    with p_short.open("a", encoding="utf-8") as f:
        f.write('{"type":"noop","n":1}\n')
    seen2 = context_snapshot.scan_transcript_doing(p_short, cursor_cache=cache)
    assert seen2.title == "Persistent Title"
    assert seen2.task == "Early Task"

    # Append another < 256 bytes -> must NOT reset DoingSeen and lose Early Task (which is > 512 bytes back)
    with p_short.open("a", encoding="utf-8") as f:
        f.write('{"type":"noop","n":2}\n')
    seen3 = context_snapshot.scan_transcript_doing(p_short, cursor_cache=cache)
    assert seen3.title == "Persistent Title"
    assert seen3.task == "Early Task"

    # 3. Cross-process persistence via cursor.json["doing_cursors"] in transcript_cards.run(emit_context=True)
    proj_dir = tmp_path / "proj"
    out_dir = tmp_path / "out"
    proj_dir.mkdir()
    tfile = proj_dir / "sess1.jsonl"
    tfile.write_text(
        json.dumps({
            "type": "user",
            "uuid": "u-1",
            "timestamp": "2026-10-24T10:00:00.000Z",
            "sessionId": "s-1",
            "origin": {"kind": "human"},
            "message": {"content": [{"type": "text", "text": "echo dxxxxx-shell-ok\nBuild feature"}]},
        }) + "\n"
        + json.dumps({"type": "ai-title", "aiTitle": "Persisted Across Processes", "sessionId": "s-1"}) + "\n"
        + json.dumps({
            "type": "assistant",
            "timestamp": "2026-10-24T10:01:00.000Z",
            "sessionId": "s-1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {"todos": [{"content": "Long-running step", "status": "in_progress"}]},
                    }
                ]
            },
        }) + "\n",
        encoding="utf-8",
    )
    rep1 = transcript_cards.run(str(proj_dir), "dxxxxx", str(out_dir), emit_context=True)
    assert rep1["context_card"]["status"] == "emitted"

    # Clear the in-process global cache and append > DOING_FIRST_READ_BYTES of non-todo lines
    context_snapshot._DOING_CURSOR_CACHE.clear()
    with tfile.open("a", encoding="utf-8") as f:
        for i in range(10):
            f.write(json.dumps({
                "type": "assistant",
                "timestamp": f"2026-10-24T10:02:0{i}.000Z",
                "sessionId": "s-1",
                "message": {"content": [{"type": "text", "text": "Z" * 120}]},
            }) + "\n")

    rep2 = transcript_cards.run(str(proj_dir), "dxxxxx", str(out_dir), emit_context=True)
    cards = [json.loads(line) for line in (out_dir / "cards.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    ctx_cards = [c for c in cards if c["type"] == "Context"]
    assert len(ctx_cards) == 1
    assert "Doing: Persisted Across Processes | Long-running step" in ctx_cards[0]["text"]



if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
