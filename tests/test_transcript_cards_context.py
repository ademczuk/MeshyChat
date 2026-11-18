"""Tests for per-seat Context card extraction in tools/transcript_cards (#297 PR 2)."""

import json
from pathlib import Path

import pytest

from tools.transcript_cards import context_snapshot, transcript_cards


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_normalize_repo_path_strips_workspace_and_rejects_outside():
    assert context_snapshot.normalize_repo_path(
        r"C:\Projects\MeshyChat\apps\cards\validation.py"
    ) == "apps/cards/validation.py"
    assert context_snapshot.normalize_repo_path(
        "/home/meshy/repos/MeshyChat/tools/transcript_cards/transcript_cards.py"
    ) == "tools/transcript_cards/transcript_cards.py"
    assert context_snapshot.normalize_repo_path("tests/test_cards_context.py") == "tests/test_cards_context.py"
    assert context_snapshot.normalize_repo_path("AGENTS.md") == "AGENTS.md"

    # Reject workstation paths outside known repo layout, dot-home state dirs, globs, or traversal attempts
    assert context_snapshot.normalize_repo_path(r"C:\Users\user\.ssh\id_ed25519") is None
    assert context_snapshot.normalize_repo_path(r"C:\Users\user\.meshychat\prod\antigravity-prod-state.json") is None
    assert context_snapshot.normalize_repo_path("/Users/user/.meshychat/meshychat/settings.py") is None
    assert context_snapshot.normalize_repo_path("tests/test_*.py") is None
    assert context_snapshot.normalize_repo_path("apps/cards/val?.py") is None
    assert context_snapshot.normalize_repo_path("/etc/passwd") is None
    assert context_snapshot.normalize_repo_path("apps/../../etc/passwd") is None
    assert context_snapshot.normalize_repo_path("../apps/cards/validation.py") is None
    assert context_snapshot.normalize_repo_path("apps/" + "a" * 200 + ".py") is None


def test_extract_seat_context_across_all_four_dialects(tmp_path):
    # 1. Antigravity dialect
    ag_dir = tmp_path / "brain" / "conv-ag-42" / ".system_generated" / "logs"
    ag_dir.mkdir(parents=True)
    ag_file = ag_dir / "transcript.jsonl"
    _write_jsonl(ag_file, [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-10-14T12:00:00.000Z",
            "content": "<USER_REQUEST>Implement PR 2 of #297</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-14T12:01:00.000Z",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {
                        "CommandLine": (
                            "git checkout -b antigravity/context-card-loader-297; "
                            "gh pr view 302; "
                            "python -m tools.meshy_node send "
                            "--state-file C:/Users/user/.meshychat/prod/antigravity-prod-state.json "
                            "--thread transcripts-to-mesh-graph-20261008 --body ok"
                        )
                    },
                },
                {
                    "name": "replace_file_content",
                    "args": {
                        "TargetFile": r"C:\Projects\MeshyChat\tools\transcript_cards\transcript_cards.py"
                    },
                },
            ],
        },
    ])
    ag_snap = context_snapshot.extract_seat_context(
        [str(ag_file)], "antigravity", "antigravity", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    assert ag_snap["refs"]["branch"] == "antigravity/context-card-loader-297"
    assert "#302" in ag_snap["refs"]["prs"]
    assert ag_snap["refs"]["threads"] == ["transcripts-to-mesh-graph-20261008"]
    # .meshychat/prod/antigravity-prod-state.json must NOT leak mid-segment as meshychat/prod/...
    assert ag_snap["refs"]["files"] == ["tools/transcript_cards/transcript_cards.py"]
    assert ag_snap["sessions"] == ["conv-ag-42"]
    assert "Implement PR 2 of #297" in ag_snap["task"]

    # 2. Claude dialect
    claude_file = tmp_path / "claude.jsonl"
    _write_jsonl(claude_file, [
        {
            "type": "assistant",
            "timestamp": "2026-10-14T12:05:00.000Z",
            "sessionId": "sess-claude-1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git switch -c dxxxxx/context-test && gh pr diff 298"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/work/projects/repo/apps/cards/store.py"},
                    },
                ]
            },
        }
    ])
    cl_snap = context_snapshot.extract_seat_context(
        [str(claude_file)], "claude", "dxxxxx", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    assert cl_snap["refs"]["branch"] == "dxxxxx/context-test"
    assert cl_snap["refs"]["prs"] == ["#298"]
    assert cl_snap["refs"]["files"] == ["apps/cards/store.py"]

    # 3. Codex dialect
    codex_file = tmp_path / "rollout-codex.jsonl"
    _write_jsonl(codex_file, [
        {
            "type": "response_item",
            "timestamp": "2026-10-14T12:10:00.000Z",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps({
                    "command": ["bash", "-lc", "git push -u origin kxxx/card-usage-238 && gh pr checks 301"]
                }),
            },
        }
    ])
    cx_snap = context_snapshot.extract_seat_context(
        [str(codex_file)], "codex", "codex-kxxx-cli", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    assert cx_snap["refs"]["branch"] == "kxxx/card-usage-238"
    assert cx_snap["refs"]["prs"] == ["#301"]

    # 4. Kimi dialect: real redacted wire.jsonl lines from nxxxxx (msg/<key>)
    kimi_main = tmp_path / "kimi" / "sessions" / "ws" / "session_kimi-real-1" / "agents" / "main"
    kimi_main.mkdir(parents=True)
    kimi_file = kimi_main / "wire.jsonl"
    _write_jsonl(kimi_file, [
        {"type": "metadata", "protocol_version": "1"},
        {
            "type": "[redacted]",
            "event": {
                "type": "content.part",
                "uuid": "[redacted]",
                "turnId": "0",
                "step": 1,
                "stepUuid": "[redacted]",
                "part": {"type": "text", "text": "[redacted]"},
            },
            "time": 1787064216231,
        },
        {
            "type": "[redacted]",
            "event": {
                "type": "tool.call",
                "uuid": "[redacted]",
                "turnId": "0",
                "step": 1,
                "stepUuid": "[redacted]",
                "toolCallId": "[redacted]",
                "name": "Read",
                "args": {"path": "tools/meshy_node/cards.py"},
            },
            "time": 1787064216270,
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "tool.call",
                "uuid": "u-shell-1",
                "turnId": "1",
                "step": 2,
                "stepUuid": "s-2",
                "toolCallId": "tc-2",
                "name": "Shell",
                "args": {"command": "git checkout nxxxxx/card-search-date-normalize && gh pr view 299"},
            },
            "time": 1787064217500,
        },
    ])
    km_snap = context_snapshot.extract_seat_context(
        [str(kimi_file)], "kimi", "nxxxxx", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    assert km_snap["refs"]["branch"] == "nxxxxx/card-search-date-normalize"
    assert km_snap["refs"]["prs"] == ["#299"]
    assert km_snap["refs"]["files"] == ["tools/meshy_node/cards.py"]
    assert km_snap["latest_time"] == "2026-08-18T14:43:37.500Z"
    assert km_snap["sessions"] == ["kimi-real-1"]


def test_context_card_clamps_refs_to_server_bounds_dropping_oldest_first(tmp_path):
    from apps.cards import validation

    ag_file = tmp_path / "ag_bounds.jsonl"
    # 12 PRs (#1..#12), 10 threads (th-1..th-10), 20 files (apps/f1.py..apps/f20.py) in chronological order
    pr_cmds = " && ".join(f"gh pr view {i}" for i in range(1, 13))
    th_cmds = " && ".join(f"python -m tools.meshy_node thread --id th-{i}" for i in range(1, 11))
    tool_calls = [{"name": "run_command", "args": {"CommandLine": f"git checkout -b ag/bounds && {pr_cmds} && {th_cmds}"}}]
    for i in range(1, 21):
        tool_calls.append({"name": "view_file", "args": {"AbsolutePath": f"C:/Projects/repo/apps/f{i}.py"}})

    _write_jsonl(ag_file, [
        {
            "step_index": 0,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-14T12:00:00.000Z",
            "tool_calls": tool_calls,
        }
    ])
    snap = context_snapshot.extract_seat_context(
        [str(ag_file)], "antigravity", "antigravity", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    # Clamped to server bounds (8 PRs, 8 threads, 16 files), keeping newest (highest index) and dropping oldest (#1..#4)
    assert len(snap["refs"]["prs"]) == 8
    assert snap["refs"]["prs"] == [f"#{i}" for i in range(12, 4, -1)]
    assert len(snap["refs"]["threads"]) == 8
    assert snap["refs"]["threads"] == [f"th-{i}" for i in range(10, 2, -1)]
    assert len(snap["refs"]["files"]) == 16
    assert snap["refs"]["files"] == [f"apps/f{i}.py" for i in range(20, 4, -1)]

    card, _, err = context_snapshot.build_context_card(
        "antigravity", "antigravity", snap,
        loader_version=transcript_cards.LOADER_VERSION,
        detect_fn=transcript_cards.detect,
        utc_instant_fn=transcript_cards.utc_instant,
    )
    assert err is None
    validation.validate_card("shared/decisions", f"card/antigravity/{card['id']}", card)


def test_run_emit_context_flood_guard_and_supersession(tmp_path):
    brain = tmp_path / "brain" / "conv-1" / ".system_generated" / "logs"
    brain.mkdir(parents=True)
    tpath = brain / "transcript.jsonl"
    out_dir = tmp_path / "out"

    records = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-10-14T12:00:00.000Z",
            "content": "<USER_REQUEST>Build context snapshot loader for #297</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-14T12:01:00.000Z",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {"CommandLine": "git checkout -b antigravity/context-card-loader-297 && gh pr view 302"},
                },
                {
                    "name": "write_to_file",
                    "args": {
                        "TargetFile": r"C:\Projects\MeshyChat\tools\transcript_cards\context_snapshot.py"
                    },
                },
            ],
        },
    ]
    _write_jsonl(tpath, records)

    # Default run (emit_context=False) emits no Context card
    rep0 = transcript_cards.run(None, "antigravity", str(out_dir), antigravity_dir=str(tmp_path / "brain"))
    assert "Context" not in rep0["cards_by_type"]

    # First run with emit_context=True emits 1 Context card
    rep1 = transcript_cards.run(
        None, "antigravity", str(out_dir), antigravity_dir=str(tmp_path / "brain"), emit_context=True
    )
    assert rep1["context_card"]["status"] == "emitted"
    first_ctx_id = rep1["context_card"]["id"]
    assert rep1["context_card"]["supersedes"] == []
    assert rep1["cards_by_type"].get("Context") == 1

    stored1 = [json.loads(line) for line in (out_dir / "cards.jsonl").read_text(encoding="utf-8").splitlines() if line]
    ctx1 = [c for c in stored1 if c["type"] == "Context"]
    assert len(ctx1) == 1
    assert ctx1[0]["id"] == first_ctx_id
    assert ctx1[0]["owner"] == "antigravity"
    assert ctx1[0]["seat"] == "antigravity"
    assert ctx1[0]["source"]["first_hand"] is False
    assert ctx1[0]["attribution"] == {
        "speaker": "agent",
        "quoted_author": None,
        "extraction": "antigravity/context_digest",
        "confirmation": "typed",
    }
    assert ctx1[0]["refs"] == {
        "branch": "antigravity/context-card-loader-297",
        "prs": ["#302"],
        "threads": [],
        "files": ["tools/transcript_cards/context_snapshot.py"],
    }

    # Second run with no changes hits flood guard (unchanged)
    rep2 = transcript_cards.run(
        None, "antigravity", str(out_dir), antigravity_dir=str(tmp_path / "brain"), emit_context=True
    )
    assert rep2["context_card"] == {"status": "unchanged", "id": first_ctx_id}
    assert rep2["new_cards"] == 0

    # Rebuild with unchanged content preserves the existing Context card id and time (never E_CARD_FROZEN)
    rep_rebuild = transcript_cards.run(
        None, "antigravity", str(out_dir), antigravity_dir=str(tmp_path / "brain"), rebuild=True, emit_context=True
    )
    assert rep_rebuild["context_card"] == {"status": "unchanged", "id": first_ctx_id}
    stored_reb = [json.loads(line) for line in (out_dir / "cards.jsonl").read_text(encoding="utf-8").splitlines() if line]
    ctx_reb = [c for c in stored_reb if c["type"] == "Context"]
    assert len(ctx_reb) == 1 and ctx_reb[0]["id"] == first_ctx_id and ctx_reb[0]["time"] == ctx1[0]["time"]

    # Append new activity with same or earlier timestamp to verify supersession + strictly increasing time bump
    records.append({
        "step_index": 2,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "created_at": "2026-10-14T12:01:00.000Z",
        "tool_calls": [
            {
                "name": "run_command",
                "args": {"CommandLine": "gh pr view 303"},
            }
        ],
    })
    _write_jsonl(tpath, records)

    # MEDIUM 3 (kxxx msg/<key>): a refused emit (E_CARD_SCHEMA) leaves the old head
    # and cursor in place, and the next accepted emit supersedes the right card (first_ctx_id).
    rep_refused = transcript_cards.run(
        None, "antigravity", str(out_dir), antigravity_dir=str(tmp_path / "brain"),
        emit_context=True,
        write_context_fn=lambda c: {"outcome": "refused", "written": False, "error_code": "E_CARD_SCHEMA"},
    )
    assert rep_refused["context_card"]["status"] == "refused"
    assert rep_refused["context_card"]["error_code"] == "E_CARD_SCHEMA"
    cursor_after_refusal = json.loads((out_dir / "cursor.json").read_text(encoding="utf-8"))
    assert cursor_after_refusal["context_head_id"] == first_ctx_id
    stored_after_refusal = [
        json.loads(line) for line in (out_dir / "cards.jsonl").read_text(encoding="utf-8").splitlines() if line
    ]
    ctx_after_refusal = [c for c in stored_after_refusal if c["type"] == "Context"]
    assert len(ctx_after_refusal) == 1 and ctx_after_refusal[0]["id"] == first_ctx_id

    # Next emit succeeds and supersedes first_ctx_id
    rep3 = transcript_cards.run(
        None, "antigravity", str(out_dir), antigravity_dir=str(tmp_path / "brain"),
        emit_context=True,
        write_context_fn=lambda c: {"outcome": "written", "written": True, "version": 1},
    )
    assert rep3["context_card"]["status"] == "emitted"
    assert rep3["context_card"]["version"] == 1
    second_ctx_id = rep3["context_card"]["id"]
    assert second_ctx_id != first_ctx_id
    assert rep3["context_card"]["supersedes"] == [f"card/antigravity/{first_ctx_id}"]

    stored3 = [json.loads(line) for line in (out_dir / "cards.jsonl").read_text(encoding="utf-8").splitlines() if line]
    ctx3 = [c for c in stored3 if c["type"] == "Context"]
    assert len(ctx3) == 1
    assert ctx3[0]["id"] == second_ctx_id
    assert ctx3[0]["supersedes"] == [f"card/antigravity/{first_ctx_id}"]
    assert ctx3[0]["time"] > ctx1[0]["time"]
    assert "#303" in ctx3[0]["refs"]["prs"]

    # Also verify two-step CLI rollback via record_context_write_result on refusal
    records.append({
        "step_index": 3,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "created_at": "2026-10-14T12:05:00.000Z",
        "tool_calls": [{"name": "run_command", "args": {"CommandLine": "gh pr view 304"}}],
    })
    _write_jsonl(tpath, records)
    rep4 = transcript_cards.run(
        None, "antigravity", str(out_dir), antigravity_dir=str(tmp_path / "brain"), emit_context=True
    )
    assert rep4["context_card"]["status"] == "emitted"
    rb = transcript_cards.record_context_write_result(
        str(out_dir), {"outcome": "refused", "written": False, "error_code": "E_CARD_SCHEMA"}
    )
    assert rb == {"status": "refused", "error_code": "E_CARD_SCHEMA", "restored_head_id": second_ctx_id}
    rep5 = transcript_cards.run(
        None, "antigravity", str(out_dir), antigravity_dir=str(tmp_path / "brain"), emit_context=True
    )
    assert rep5["context_card"]["status"] == "emitted"
    assert rep5["context_card"]["supersedes"] == [f"card/antigravity/{second_ctx_id}"]


def test_context_card_redaction_gate_and_rescreen_drop(tmp_path):
    # 1. Secret in a git branch command is stripped during extraction
    ag_file = tmp_path / "ag_secret.jsonl"
    secret_tok = "ghp_" + "A" * 30
    _write_jsonl(ag_file, [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-10-14T12:00:00.000Z",
            "content": "<USER_REQUEST>Check redaction</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-14T12:01:00.000Z",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {"CommandLine": f"git checkout -b leak/{secret_tok} && gh pr view 297"},
                }
            ],
        },
    ])
    snap = context_snapshot.extract_seat_context(
        [str(ag_file)], "antigravity", "antigravity", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    assert snap["refs"]["branch"] is None
    assert snap["refs"]["prs"] == ["#297"]

    # 2. Defense-in-depth: if an unscreened snapshot contains a secret in task/refs, build_context_card refuses it
    tainted_snapshot = {
        "refs": {"branch": f"leak/{secret_tok}", "prs": ["#297"], "threads": [], "files": []},
        "task": "Check redaction",
        "latest_time": "2026-10-14T12:01:00.000Z",
        "sessions": ["conv-1"],
        "source_files": ["transcript.jsonl"],
    }
    card, sha, err = context_snapshot.build_context_card(
        "antigravity", "antigravity", tainted_snapshot,
        loader_version=transcript_cards.LOADER_VERSION,
        detect_fn=transcript_cards.detect,
        utc_instant_fn=transcript_cards.utc_instant,
    )
    assert card is None
    assert err == "redaction:vendor_key"

    # 3. Stored Context card whose refs trip a detector is dropped at rescreen, and malformed cursor time never crashes
    brain = tmp_path / "brain2" / "conv-2" / ".system_generated" / "logs"
    brain.mkdir(parents=True)
    _write_jsonl(brain / "transcript.jsonl", [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-10-14T12:00:00.000Z",
            "content": "<USER_REQUEST>Clean prompt</USER_REQUEST>",
        }
    ])
    out2 = tmp_path / "out2"
    rep_init = transcript_cards.run(
        None, "antigravity", str(out2), antigravity_dir=str(tmp_path / "brain2"), emit_context=True
    )
    bad_ctx_id = rep_init["context_card"]["id"]
    stored_cards = [json.loads(line) for line in (out2 / "cards.jsonl").read_text(encoding="utf-8").splitlines() if line]
    for c in stored_cards:
        if c["type"] == "Context":
            c["refs"]["branch"] = f"leak/{secret_tok}"
    (out2 / "cards.jsonl").write_text("".join(json.dumps(c) + "\n" for c in stored_cards), encoding="utf-8")
    cur_data = json.loads((out2 / "cursor.json").read_text(encoding="utf-8"))
    cur_data["context_head_time"] = "not-a-timestamp"
    (out2 / "cursor.json").write_text(json.dumps(cur_data), encoding="utf-8")

    rep_rescreen = transcript_cards.run(
        None, "antigravity", str(out2), antigravity_dir=str(tmp_path / "brain2"), emit_context=False
    )
    assert rep_rescreen["rescreen_drops"] == 1
    drops_rows = [json.loads(line) for line in (out2 / "drops.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert any(d["id"] == bad_ctx_id and d["type"] == "Context" for d in drops_rows)


def test_antigravity_json_encoded_args_worktree_branches_heredoc_and_ruling_summary(tmp_path):
    ag_dir = tmp_path / "brain" / "conv-real-ag" / ".system_generated" / "logs"
    ag_dir.mkdir(parents=True)
    real_file = tmp_path / "projects" / "repo" / "tools" / "meshy_node" / "cards.py"

    full_cmd = (
        "@'\n"
        "import subprocess\n"
        'doc = "inline (`@\' ... \'@`) example before apps/calls/views.py and docs/STATUS.md"\n'
        'subprocess.run(["python", "-m", "tools.meshy_node", "send", "--thread", "pr-reviews-20261014"])\n'
        "'@ | python - ; "
        "git -C C:/Projects/mc-wt-319 push -u origin antigravity/load-hook-emit-context-297 ; "
        "gh api repos/sampleco/MeshyChat/pulls/319/comments"
    )
    encoded_full_cmd = json.dumps(full_cmd)
    truncated_cmd = encoded_full_cmd[:120] + "...[truncated]"

    # Compact transcript.jsonl has truncated tool_calls; transcript_full.jsonl has the full record
    _write_jsonl(ag_dir / "transcript.jsonl", [
        {
            "step_index": 10,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-15T08:00:00.000Z",
            "truncated_fields": ["tool_calls"],
            "tool_calls": [
                {"name": "run_command", "args": {"CommandLine": truncated_cmd}},
                {"name": "replace_file_content", "args": {"TargetFile": json.dumps(str(real_file))}},
            ],
        }
    ])
    _write_jsonl(ag_dir / "transcript_full.jsonl", [
        {
            "step_index": 10,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-15T08:00:00.000Z",
            "tool_calls": [
                {"name": "run_command", "args": {"CommandLine": encoded_full_cmd}},
                {"name": "replace_file_content", "args": {"TargetFile": json.dumps(str(real_file))}},
            ],
        }
    ])

    long_q = (
        "Overnight status: PR #312 (fixing #308) merged into main (c9f35524); HQB MeshyChat-Postbox "
        "restarted on c9f35524 and verified healthy; main + mc-cards-job fast-forwarded. What should "
        "Antigravity execute next?"
    )
    long_a = "(Recommended) Review the 3 new open PRs (#315, #316, #317) and implement Issue #199"
    ruling_card = {
        "type": "Ruling",
        "time": "2026-10-15T08:01:00.000Z",
        "question": long_q,
        "answer": long_a,
        "text": (
            f"Ruling | owner: operator | seat: antigravity | 2026-10-15 08:01\n"
            f"Q: {long_q}\n"
            f"Offered: {long_a}; Option 2; Option 3\n"
            f"A: {long_a}"
        ),
    }

    snap = context_snapshot.extract_seat_context(
        [str(ag_dir / "transcript.jsonl")], "antigravity", "antigravity", [ruling_card],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    assert snap["refs"]["branch"] == "antigravity/load-hook-emit-context-297"
    assert "#319" in snap["refs"]["prs"]
    assert snap["refs"]["threads"] == ["pr-reviews-20261014"]
    # Heredoc prose strings (apps/calls/views.py, an internal project document (not part of this release)) are excluded; real_file is normalized even if not on disk
    assert snap["refs"]["files"] == ["tools/meshy_node/cards.py"]
    # Both Q: and A: are preserved in task summary
    assert snap["task"].startswith("Q: Overnight status: PR #312")
    assert "| A: (Recommended) Review the 3 new open PRs" in snap["task"]


def test_sh_heredoc_keeps_redirect_and_truncated_windows_paths_and_worktree_flags(tmp_path):
    # P2-1: POSIX heredoc keeps redirect target on opening line, requires exact closing delimiter line
    # (ignoring EOF.txt prefix and space-indented EOF on plain <<), and supports tab-indented <<-EOF
    cmd_heredoc = (
        "cat <<'EOF' > tools/generated.py\n"
        "EOF.txt apps/fake/nope.py\n"
        "  EOF\n"
        "print('docs/fake.md')\n"
        "EOF\n"
    )
    stripped = context_snapshot._strip_heredocs(cmd_heredoc)
    assert "tools/generated.py" in stripped
    assert "apps/fake/nope.py" not in stripped
    assert "docs/fake.md" not in stripped

    cmd_heredoc_dash = (
        "cat <<-EOF > tools/indented.py\n"
        "\tEOF.txt apps/fake/nope.py\n"
        "\tEOF\n"
    )
    stripped_dash = context_snapshot._strip_heredocs(cmd_heredoc_dash)
    assert "tools/indented.py" in stripped_dash
    assert "apps/fake/nope.py" not in stripped_dash

    cmd_bitshift = "python3 -c 'print(1 << 2)'\n2\npytest tools/a.py\n"
    stripped_shift = context_snapshot._strip_heredocs(cmd_bitshift)
    assert "tools/a.py" in stripped_shift

    # P2-2: truncated Windows paths in _unwrap_ag_arg do not turn \r, \t, \n into control characters
    raw_trunc = r'"pytest C:\\Users\\user\\repo\\tests\\test_new.py C:\\Users\\user\\repo\\tools\\run.py...[truncated]'
    unwrapped = context_snapshot._unwrap_ag_arg(raw_trunc)
    assert unwrapped == r"pytest C:\Users\user\repo\tests\test_new.py C:\Users\user\repo\tools\run.py"

    # Also test fallback when json.loads fails due to a partial \u escape at the cut point
    raw_trunc_u = r'"pytest C:\\Users\\user\\repo\\tests\\test_new.py \u00...[truncated]'
    unwrapped_u = context_snapshot._unwrap_ag_arg(raw_trunc_u)
    assert r"C:\Users\user\repo\tests\test_new.py" in unwrapped_u

    # Raw/plain quoted Windows paths (both invalid JSON like \P and JSON-resembling like \t, \r) preserve backslashes when is_path=True
    assert context_snapshot.normalize_repo_path(r'"C:\Projects\repo\tools\run.py"') == "tools/run.py"
    assert context_snapshot._unwrap_ag_arg(r'"C:\temp\repo\tools\run.py"', is_path=True) == r"C:\temp\repo\tools\run.py"
    assert context_snapshot.normalize_repo_path(r'"C:\temp\repo\tools\run.py"') == "tools/run.py"
    assert context_snapshot.normalize_repo_path(r'"tools\run.py"') == "tools/run.py"
    # JSON escape sequences that decode to control characters (such as \u0009) are rejected, never turned into tools/a/u0009b.py
    assert context_snapshot._unwrap_ag_arg(r'"tools/a\u0009b.py"', is_path=True) == ""
    assert context_snapshot.normalize_repo_path(r'"tools/a\u0009b.py"') is None
    assert context_snapshot.normalize_repo_path(r'"tools\\a\u0009b.py"') is None

    # Multi-line JSON-dumped CommandLine values containing tests\n, \nrun, tools\n decode cleanly as JSON
    cmd1 = "python -m pytest tests\ngit switch -c feature/x"
    assert context_snapshot._unwrap_ag_arg(json.dumps(cmd1)) == cmd1
    cmd2 = "cat <<'EOF' > tools/gen.py\nrun tests\nEOF"
    assert context_snapshot._unwrap_ag_arg(json.dumps(cmd2)) == cmd2
    assert context_snapshot._strip_heredocs(context_snapshot._unwrap_ag_arg(json.dumps(cmd2))).strip() == "cat   > tools/gen.py"
    cmd3 = 'cd tools\npython3 -m tools.meshy_node send --thread "pr-reviews-1"'
    assert context_snapshot._unwrap_ag_arg(json.dumps(cmd3)) == cmd3

    # Lone UTF-16 surrogates in JSON-encoded args are rejected and never crash build_context_card
    assert context_snapshot._unwrap_ag_arg(r'"tools/bad\ud800.py"') == ""
    assert context_snapshot.normalize_repo_path(r'"tools/bad\ud800.py"') is None
    assert context_snapshot.normalize_repo_path("tools/bad\ud800.py") is None

    # P3-1: git worktree add -f / --force skips options and never captures ../wt-x as a branch
    ag_dir = tmp_path / "brain" / "conv-wt-flags" / ".system_generated" / "logs"
    ag_dir.mkdir(parents=True)
    _write_jsonl(ag_dir / "transcript.jsonl", [
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-15T09:00:00.000Z",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {
                        "CommandLine": json.dumps(
                            "git worktree add -f ../wt-x feature/worktree-flag && git switch feature/newer-switch\n"
                            "cat <<'EOF' > tools/generated.py\nprint('apps/fake/nope.py')\nEOF"
                        )
                    },
                },
                {
                    "name": "run_command",
                    "args": {"CommandLine": raw_trunc},
                },
                {
                    "name": "view_file",
                    "args": {"AbsolutePath": r'"tools/bad\ud800.py"'},
                },
            ],
        }
    ])
    snap = context_snapshot.extract_seat_context(
        [str(ag_dir / "transcript.jsonl")], "antigravity", "antigravity", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    assert snap["refs"]["branch"] == "feature/newer-switch"
    assert "tools/generated.py" in snap["refs"]["files"]
    assert "apps/fake/nope.py" not in snap["refs"]["files"]

    # Also verify standalone worktree add with -f extracts feature/worktree-flag
    _write_jsonl(ag_dir / "transcript.jsonl", [
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-15T09:01:00.000Z",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {"CommandLine": json.dumps("git worktree add -f ../wt-x feature/worktree-flag")},
                },
            ],
        }
    ])
    snap2 = context_snapshot.extract_seat_context(
        [str(ag_dir / "transcript.jsonl")], "antigravity", "antigravity", [],
        detect_fn=transcript_cards.detect, utc_instant_fn=transcript_cards.utc_instant,
    )
    assert snap2["refs"]["branch"] == "feature/worktree-flag"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
