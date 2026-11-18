"""Contract tests for the 5-source pre-idle orientation tool (`tools/meshy_node/orient.py`
and `python -m tools.meshy_node orient`).

Covers positive and negative controls for all 5 sources:
1. Postbox spool & banked `cc` inspection (`inspect_spool`), including `.lock` file avoidance,
   held `cc` detection (#199), delivered-token filtering, and uncollected byte detection.
2. MeshyChat message graph synthesis (`inspect_mesh_graph`) across `brief`, `owed`, and `awaiting`,
   plus offline/unreachable handling.
3. Local git worktree & branch inspection (`inspect_worktrees`) for dirty files, commits ahead
   of trunk (`origin/main`), and clean worktree negative controls.
4. Handover staleness and scratchpad gate log scanning (`inspect_handovers_and_gates`) for
   `LOCAL GATE PASS` and `LOCAL GATE REFUSED`.
5. Brain (`staff` & `elevated`) & environment hygiene (`inspect_brain_and_env`, `clean_brain_env`),
   verifying `OPENSSL_CONF` / `NODE_OPTIONS` detection and stripping before invoking
   `cf-access-headers.sh --identity`.
6. Ranked `candidate_options` synthesis (`synthesize_candidates`), CLI `--json` and text modes,
   and `cli._mutating_command` non-mutating classification.

Run:
  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_orient.py -q -p no:cacheprovider --noconftest
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node, orient  # noqa: E402


def test_orient_is_non_mutating_in_cli():
    """`orient` must be classified as a read-only command in `cli._mutating_command` so it never
    trips sendguard or write-retry error metadata."""
    parser = cli.build_parser()
    args_offline = parser.parse_args(["orient", "--offline"])
    assert cli._mutating_command(args_offline) is False

    args_live = parser.parse_args([
        "orient",
        "--base-url", "http://127.0.0.1:6310",
        "--state-file", "state.json",
        "--identity", "antigravity-prod,bxxxxx-prod",
        "--json",
    ])
    assert cli._mutating_command(args_live) is False

    # Control: `send` and `send-status` remain mutating
    args_send = parser.parse_args([
        "send",
        "--base-url", "http://127.0.0.1:6310",
        "--state-file", "state.json",
        "--to", "peer",
        "--thread", "t",
        "--body", "hi",
    ])
    assert cli._mutating_command(args_send) is True


def test_source1_spool_banked_cc_and_lock_avoidance(tmp_path):
    """Positive and negative controls for postbox spool inspection:
    - Detects undelivered `held` cc items in `<ident>.log.wait-session.offset.bank.json` (#199).
    - Skips `held` items whose token is already in `delivered`.
    - Detects uncollected bytes when `<ident>.log` is larger than the bank's `offset`.
    - Never opens or touches `*.lock` files.
    """
    spool = tmp_path / "spool"
    spool.mkdir()

    # Create a poison .lock directory/file that would fail if read as JSON
    lock_file = spool / "antigravity-prod.log.wait-session.lock"
    lock_file.write_bytes(b"\xff\xfe\x00LOCKED")

    log_lines = [
        json.dumps({
            "event": "message",
            "key": "msg/1792950000001-kxxx-aaaa11",
            "from": "kxxx",
            "to": ["pxx"],
            "cc": ["antigravity"],
            "role": "cc",
            "requires_ack": False,
            "created_at": "2026-10-25T18:00:00Z",
            "rx_at": "2026-10-25T18:00:01Z",
        }),
        json.dumps({
            "event": "message",
            "key": "msg/1792950000002-pxx-bbbb22",
            "from": "pxx",
            "to": ["antigravity"],
            "cc": [],
            "role": "to",
            "requires_ack": True,
            "created_at": "2026-10-25T18:05:00Z",
            "rx_at": "2026-10-25T18:05:01Z",
        }),
    ]
    log_path = spool / "antigravity-prod.log"
    log_bytes = ("\n".join(log_lines) + "\n").encode("utf-8")
    log_path.write_bytes(log_bytes)

    bank_path = spool / "antigravity-prod.log.wait-session.offset.bank.json"
    bank_data = {
        "schema_version": 2,
        "identity": "antigravity-prod",
        "consumer": "wait:session",
        "offset": len(log_bytes) - 40,  # 40 bytes uncollected
        "delivered": [
            '["message","msg/1792950000000-txxxx-delivered",1]',
        ],
        "held": [
            {
                "token": '["message","msg/1792950000000-txxxx-delivered",1]',
                "held_at": "2026-10-25T17:50:00Z",
                "routing": {
                    "event": "message",
                    "key": "msg/1792950000000-txxxx-delivered",
                    "from": "txxxx",
                    "role": "cc",
                    "requires_ack": False,
                    "created_at": "2026-10-25T17:50:00Z",
                },
            },
            {
                "token": '["message","msg/1792950000001-kxxx-aaaa11",1]',
                "held_at": "2026-10-25T18:00:01Z",
                "routing": {
                    "event": "message",
                    "key": "msg/1792950000001-kxxx-aaaa11",
                    "from": "kxxx",
                    "role": "cc",
                    "requires_ack": False,
                    "created_at": "2026-10-25T18:00:00Z",
                    "preview_unverified": "kxxx to pxx (cc antigravity): status update",
                },
            },
        ],
    }
    bank_path.write_text(json.dumps(bank_data), encoding="utf-8")

    # Another identity's bank file (should be excluded when filtering by identity)
    other_bank = spool / "dxxxxx-prod.log.channel.offset.bank.json"
    other_bank.write_text(
        json.dumps({
            "schema_version": 2,
            "identity": "dxxxxx-prod",
            "consumer": "claude-channel",
            "offset": 0,
            "delivered": [],
            "held": [{
                "token": '["message","msg/1792950000099-uxxx-cccc33",1]',
                "held_at": "2026-10-25T18:01:00Z",
                "routing": {"key": "msg/1792950000099-uxxx-cccc33", "from": "uxxx", "role": "cc"},
            }],
        }),
        encoding="utf-8",
    )

    now_ts = orient._epoch("2026-10-25T18:10:01Z")
    res = orient.inspect_spool(spool, identities=["antigravity-prod"], now=now_ts)
    assert res["available"] is True
    assert res["bank_files_checked"] == 1
    assert res["held_cc_count"] == 1
    held = res["held_cc"][0]
    assert held["key"] == "msg/1792950000001-kxxx-aaaa11"
    assert held["from"] == "kxxx"
    assert held["age_s"] == 600.0
    assert len(res["uncollected"]) == 1
    assert res["uncollected"][0]["uncollected_bytes"] == 40
    assert len(res["recent_tail_messages"]) == 2

    # Negative control: missing spool dir
    missing = orient.inspect_spool(tmp_path / "does_not_exist")
    assert missing["available"] is False
    assert missing["held_cc_count"] == 0


def test_source2_mesh_graph_live_and_offline(tmp_path, monkeypatch):
    """Positive and negative controls for `inspect_mesh_graph`."""
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")

    # 1. Offline mode makes zero network calls
    offline_res = orient.inspect_mesh_graph(state_file, "http://127.0.0.1:6310", offline=True)
    assert offline_res["available"] is False
    assert offline_res["mode"] == "offline"

    # 2. Live mode synthesizes `brief` + `owed`
    monkeypatch.setattr(node, "load_state", lambda path, url: {"label": "antigravity", "base_url": url})
    monkeypatch.setattr(
        node,
        "brief",
        lambda path, state, since=None: {
            "owed": {
                "counts": {"open": 1},
                "items": [{
                    "state": "open",
                    "msg_type": "question",
                    "thread_id": "hz-build",
                    "done_when": "verdict posted",
                    "hit": {
                        "key": "msg/1792950000111-oxxxxxx-111111",
                        "author": "oxxxxxx",
                        "recorded_at": "2026-10-25T18:00:00Z",
                    },
                }],
            },
            "awaiting": {
                "counts": {"open": 1},
                "items": [{
                    "state": "open",
                    "owing_label": "txxxx",
                    "thread_id": "pr-348",
                    "hit": {
                        "key": "msg/1792950000222-antigravity-222222",
                        "recorded_at": "2026-10-25T17:00:00Z",
                    },
                }],
            },
            "threads": {
                "items": [{
                    "thread_id": "hz-build",
                    "new": 4,
                    "last_author": "pxx",
                    "last_recorded_at": "2026-10-25T18:20:00Z",
                }],
            },
            "help_wanted": {
                "items": [{
                    "key": "msg/1792950000333-kxxx-333333",
                    "author": "kxxx",
                    "thread_id": "ops-help",
                    "recorded_at": "2026-10-25T16:00:00Z",
                }],
            },
        },
    )
    monkeypatch.setattr(
        node,
        "owed",
        lambda path, state, limit=None, states=None, since=None: {
            "owed": [
                {
                    "state": "open",
                    "msg_type": "question",
                    "thread_id": "hz-build",
                    "done_when": "verdict posted",
                    "hit": {
                        "key": "msg/1792950000111-oxxxxxx-111111",
                        "author": "oxxxxxx",
                        "recorded_at": "2026-10-25T18:00:00Z",
                    },
                },
                {
                    "state": "answered",
                    "msg_type": "request",
                    "thread_id": "cards-c5",
                    "done_when": "PR merged",
                    "hit": {
                        "key": "msg/1792950000444-kxxx-444444",
                        "author": "kxxx",
                        "recorded_at": "2026-10-25T15:00:00Z",
                    },
                },
            ],
            "truncated": False,
        },
    )

    live_res = orient.inspect_mesh_graph(state_file, "http://127.0.0.1:6310", offline=False)
    assert live_res["available"] is True
    assert live_res["mode"] == "live"
    assert live_res["seat"] == "antigravity"
    assert live_res["reply_owed_count"] == 1
    assert live_res["reply_owed"][0]["key"] == "msg/1792950000111-oxxxxxx-111111"
    assert live_res["answered_still_open_count"] == 1
    assert live_res["answered_still_open"][0]["key"] == "msg/1792950000444-kxxx-444444"
    assert live_res["awaiting_count"] == 1
    assert live_res["awaiting"][0]["owing_label"] == "txxxx"
    assert live_res["threads_with_news_count"] == 1
    assert live_res["threads_with_news"][0]["thread_id"] == "hz-build"
    assert live_res["help_wanted_count"] == 1


def test_source3_worktrees_dirty_and_ahead(tmp_path):
    """Positive and negative controls for `inspect_worktrees` using real temporary git repos."""
    # Create a bare-like trunk repo and two worktrees/clones: one clean, one dirty + ahead
    origin_repo = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin_repo)], check=True, capture_output=True)

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    clean_wt = wt_root / "wt-clean"
    subprocess.run(["git", "clone", str(origin_repo), str(clean_wt)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clean_wt), "checkout", "-b", "main"], check=True, capture_output=True)
    (clean_wt / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clean_wt), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clean_wt), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(clean_wt), "push", "-u", "origin", "main"], check=True, capture_output=True)

    dirty_wt = wt_root / "wt-dirty-ahead"
    subprocess.run(["git", "clone", str(origin_repo), str(dirty_wt)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(dirty_wt), "checkout", "-b", "feat/orient", "origin/main"], check=True, capture_output=True)
    (dirty_wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(dirty_wt), "add", "feature.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dirty_wt), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "feat"],
        check=True,
        capture_output=True,
    )
    # Add an uncommitted modification
    (dirty_wt / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    # Set an older mtime on .git/index to verify `--no-optional-locks` / `GIT_OPTIONAL_LOCKS=0`
    # prevents git status from rewriting .git/index or creating .git/index.lock
    import os
    index_path = clean_wt / ".git" / "index"
    old_mtime = 1_700_000_000.0
    os.utime(index_path, (old_mtime, old_mtime))

    report = orient.inspect_worktrees(worktree_roots=[wt_root], trunk="origin/main")
    assert report["scanned_count"] == 2
    assert report["actionable_count"] == 1
    assert not (clean_wt / ".git" / "index.lock").exists()
    assert int(index_path.stat().st_mtime) == int(old_mtime)

    # Also verify `_git_run` explicitly passes `--no-optional-locks` and `GIT_OPTIONAL_LOCKS=0`
    captured_git_calls: list[tuple[list[str], dict[str, str]]] = []
    real_run = subprocess.run

    def spy_git_run(argv, **kwargs):
        if isinstance(argv, list) and argv and argv[0] == "git":
            captured_git_calls.append((list(argv), dict(kwargs.get("env") or {})))
        return real_run(argv, **kwargs)

    orient.subprocess.run = spy_git_run
    try:
        orient._git_run(clean_wt, ["status", "--porcelain", "-b"])
    finally:
        orient.subprocess.run = real_run
    assert len(captured_git_calls) == 1
    assert captured_git_calls[0][0][:2] == ["git", "--no-optional-locks"]
    assert captured_git_calls[0][1].get("GIT_OPTIONAL_LOCKS") == "0"

    by_name = {w["name"]: w for w in report["worktrees"]}
    assert by_name["wt-clean"]["actionable"] is False
    assert by_name["wt-clean"]["dirty_count"] == 0
    assert by_name["wt-clean"]["ahead_of_trunk"] == 0

    assert by_name["wt-dirty-ahead"]["actionable"] is True
    assert by_name["wt-dirty-ahead"]["branch"] == "feat/orient"
    assert by_name["wt-dirty-ahead"]["dirty_count"] == 1
    assert by_name["wt-dirty-ahead"]["ahead_of_trunk"] == 1


def test_source4_handovers_and_gate_logs(tmp_path):
    """Positive and negative controls for `inspect_handovers_and_gates`."""
    hdir = tmp_path / "dxxxxx"
    hdir.mkdir()
    now_ts = 1_800_000_000.0

    stale_file = hdir / "HANDOVER-txxxx-session-2026-10-21.md"
    stale_file.write_text("# Txxxx handover\n", encoding="utf-8")
    # Set mtime to 36 hours ago
    import os
    os.utime(stale_file, (now_ts - 36 * 3600, now_ts - 36 * 3600))

    fresh_file = hdir / "HANDOVER-dxxxxx-session-2026-10-25.md"
    fresh_file.write_text("# Dxxxxx handover\n", encoding="utf-8")
    os.utime(fresh_file, (now_ts - 2 * 3600, now_ts - 2 * 3600))

    sp_dir = tmp_path / "scratchpad"
    sp_dir.mkdir()
    (sp_dir / "gate-runner.log").write_text(
        "running checks...\nLOCAL GATE REFUSED: missing mutant kill\n",
        encoding="utf-8",
    )
    sub_sp = sp_dir / "sub"
    sub_sp.mkdir()
    (sub_sp / "verdict.txt").write_text("all green\nLOCAL GATE PASS (commit 0cae2deb)\n", encoding="utf-8")

    res_txxxx = orient.inspect_handovers_and_gates(
        handover_dir=hdir,
        scratchpad_dirs=[sp_dir],
        seat="txxxx",
        stale_hours=24.0,
        now=now_ts,
    )
    assert res_txxxx["handovers_found"] == 2
    assert res_txxxx["own_handover"] is not None
    assert res_txxxx["own_handover"]["file"] == "HANDOVER-txxxx-session-2026-10-21.md"
    assert res_txxxx["own_handover"]["stale"] is True
    assert len(res_txxxx["stale_handovers"]) == 1
    verdicts_by_file = {g["file"]: g["verdict"] for g in res_txxxx["gate_reports"]}
    assert verdicts_by_file["gate-runner.log"] == "LOCAL GATE REFUSED"
    assert verdicts_by_file["verdict.txt"] == "LOCAL GATE PASS"

    # Negative control: fresh seat has 0 stale handovers
    res_dxxxxx = orient.inspect_handovers_and_gates(
        handover_dir=hdir,
        scratchpad_dirs=[],
        seat="dxxxxx",
        stale_hours=24.0,
        now=now_ts,
    )
    assert res_dxxxxx["own_handover"]["stale"] is False
    assert res_dxxxxx["stale_handovers"] == []


def test_source5_brain_env_hygiene_and_clean_env_stripping(tmp_path, monkeypatch):
    """Positive and negative controls for `OPENSSL_CONF` / `REFUSED_ENVIRONMENT` detection
    and stripping before probing `cf-access-headers.sh --identity`."""
    dirty_env = {
        "PATH": "/usr/bin",
        "OPENSSL_CONF": "C:/Program Files/PostgreSQL/17/ssl/openssl.cnf",
        "NODE_OPTIONS": "--inspect",
        "HTTPS_PROXY": "http://127.0.0.1:8888",
        "NORMAL_VAR": "kept",
    }
    cleaned, refused = orient.clean_brain_env(dirty_env)
    assert refused == ["HTTPS_PROXY", "NODE_OPTIONS", "OPENSSL_CONF"]
    assert "OPENSSL_CONF" not in cleaned
    assert "NODE_OPTIONS" not in cleaned
    assert "HTTPS_PROXY" not in cleaned
    assert cleaned["NORMAL_VAR"] == "kept"

    # Verify `inspect_brain_and_env` passes `cleaned_env` (without `OPENSSL_CONF`) to subprocess.run
    # and redacts email addresses from `cf-access-headers.sh --identity` output.
    staff_sh = tmp_path / "staff-cf-access-headers.sh"
    elev_sh = tmp_path / "elev-cf-access-headers.sh"
    staff_sh.write_text("#!/usr/bin/env bash\necho 'cloudflared alice@example.com'\n", encoding="utf-8")
    elev_sh.write_text("#!/usr/bin/env bash\necho 'none'\n", encoding="utf-8")

    captured_envs: list[dict[str, str]] = []

    def fake_run(argv, **kwargs):
        captured_envs.append(dict(kwargs.get("env") or {}))
        script = argv[1]
        out = "cloudflared alice@example.com\n" if "staff" in script else "none\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    brain_res = orient.inspect_brain_and_env(
        env=dirty_env,
        probe_identity=True,
        staff_script=staff_sh,
        elevated_script=elev_sh,
        bash_bin="bash",
    )
    assert brain_res["env_clean"] is False
    assert brain_res["refused_env_present"] == ["HTTPS_PROXY", "NODE_OPTIONS", "OPENSSL_CONF"]
    assert "rc=5" in brain_res["remediation"]
    assert brain_res["staff"]["active"] is True
    assert brain_res["staff"]["identity"] == "cloudflared (signed in)"
    assert "@" not in brain_res["staff"]["identity"]
    assert "alice@example.com" not in json.dumps(brain_res)
    assert orient._redact_brain_identity("token bob@example.org") == "token <redacted-email>"
    assert orient._redact_brain_identity("service-token 9f8a7b") == "service-token 9f8a7b"
    assert brain_res["elevated"]["active"] is False
    assert brain_res["elevated"]["identity"] == "none"
    assert len(captured_envs) == 2
    for c_env in captured_envs:
        assert "OPENSSL_CONF" not in c_env
        assert "NODE_OPTIONS" not in c_env
        assert c_env.get("NORMAL_VAR") == "kept"

    # Negative control: clean environment has env_clean=True and no remediation warning
    clean_res = orient.inspect_brain_and_env(
        env={"PATH": "/usr/bin"},
        probe_identity=False,
        staff_script=staff_sh,
        elevated_script=elev_sh,
    )
    assert clean_res["env_clean"] is True
    assert clean_res["refused_env_present"] == []
    assert clean_res["remediation"] is None


def test_candidate_options_synthesis_and_cli_round_trip(tmp_path, monkeypatch, capsys):
    """End-to-end test of `cli.main(['orient', ...])` in both `--json` and text modes, verifying
    that `candidate_options` ranks owed asks (P1), banked cc (P2), gate verdicts (P3), dirty
    worktrees (P4), thread news (P5), Brain env/sign-in (P6), and stale handovers (P7)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "antigravity-prod.log").write_text("", encoding="utf-8")
    (spool / "antigravity-prod.log.wait-session.offset.bank.json").write_text(
        json.dumps({
            "schema_version": 2,
            "identity": "antigravity-prod",
            "consumer": "wait:session",
            "offset": 0,
            "delivered": [],
            "held": [{
                "token": '["message","msg/1792950000777-pxx-777777",1]',
                "held_at": "2026-10-25T19:00:00Z",
                "routing": {
                    "key": "msg/1792950000777-pxx-777777",
                    "from": "pxx",
                    "role": "cc",
                    "requires_ack": False,
                },
            }],
        }),
        encoding="utf-8",
    )

    hdir = tmp_path / "dxxxxx"
    hdir.mkdir()
    hfile = hdir / "HANDOVER-antigravity-session-2026-10-20.md"
    hfile.write_text("# Old handover\n", encoding="utf-8")
    import os
    os.utime(hfile, (1_700_000_000, 1_700_000_000))

    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "verdict.txt").write_text("LOCAL GATE PASS\n", encoding="utf-8")

    monkeypatch.setenv("OPENSSL_CONF", "C:/bad/openssl.cnf")

    rc_json = cli.main([
        "orient",
        "--offline",
        "--no-brain-probe",
        "--identity", "antigravity-prod",
        "--spool-dir", str(spool),
        "--handover-dir", str(hdir),
        "--scratchpad-dir", str(sp),
        "--json",
    ])
    assert rc_json == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["seat"] == "antigravity"
    sources = [opt["source"] for opt in payload["candidate_options"]]
    assert "spool.held_cc" in sources
    assert "handovers_and_gates.gate_reports" in sources
    assert "brain.refused_env" in sources
    assert "handovers_and_gates.stale_handovers" in sources
    # Verify priority ordering is strictly non-decreasing
    priorities = [opt["priority"] for opt in payload["candidate_options"]]
    assert priorities == sorted(priorities)

    rc_text = cli.main([
        "orient",
        "--offline",
        "--no-brain-probe",
        "--identity", "antigravity-prod",
        "--spool-dir", str(spool),
        "--handover-dir", str(hdir),
        "--scratchpad-dir", str(sp),
    ])
    assert rc_text == 0
    text_out = capsys.readouterr().out
    assert "=== MeshyChat Pre-Idle Orientation" in text_out
    assert "HELD CC: msg/1792950000777-pxx-777777" in text_out
    assert "LOCAL GATE PASS" in text_out
    assert "OPENSSL_CONF" in text_out
    assert "=== Ranked Candidate Options" in text_out


def test_source1_spool_excludes_terminally_handled_messages(tmp_path):
    """Regression test for PR #412 review comment 4372128061:
    - Held messages whose exact `(key, version)` is recorded as terminally handled in the
      identity's handled receipt (`mark_handled.py` / `delivery_bank.py` schema) are excluded
      from `held_cc` even when an inactive consumer's bank has not marked them `delivered`.
    - A newer unhandled version (`version=2` when only `version=1` is handled) remains surfaced.
    - Malformed or non-terminal handled entries (`signature_verified=False` or `disposition='open'`)
      are skipped and do not suppress held messages.
    """
    postbox_dir = tmp_path / "postbox"
    spool = postbox_dir / "spool"
    handled_dir = postbox_dir / "handled"
    spool.mkdir(parents=True)
    handled_dir.mkdir(parents=True)

    handled_file = handled_dir / "nxxxxx-prod.json"
    handled_file.write_text(
        json.dumps({
            "schema_version": 1,
            "identity": "nxxxxx-prod",
            "handled": [
                {
                    "key": "msg/1792950000101-kxxx-handled-v1",
                    "version": 1,
                    "disposition": "done",
                    "signature_verified": True,
                    "status_verified": True,
                    "handled_at": "2026-10-25T18:02:00Z",
                },
                {
                    "key": "msg/1792950000102-pxx-edited",
                    "version": 1,
                    "disposition": "no_action",
                    "signature_verified": True,
                    "status_verified": True,
                },
                {
                    "key": "msg/1792950000103-txxxx-unverified",
                    "version": 1,
                    "disposition": "done",
                    "signature_verified": False,
                    "status_verified": True,
                },
            ],
        }),
        encoding="utf-8",
    )

    (postbox_dir / "config.json").write_text(
        json.dumps({
            "identities": [{
                "name": "nxxxxx-prod",
                "handled_file": str(handled_file),
                "handled_private_acl_verified": True,
            }],
        }),
        encoding="utf-8",
    )

    (spool / "nxxxxx-prod.log.wait-session.offset.bank.json").write_text(
        json.dumps({
            "schema_version": 2,
            "identity": "nxxxxx-prod",
            "consumer": "wait:session",
            "offset": 0,
            "delivered": [],
            "held": [
                {
                    "token": '["message","msg/1792950000101-kxxx-handled-v1",1]',
                    "held_at": "2026-10-25T18:00:00Z",
                    "routing": {
                        "event": "message",
                        "key": "msg/1792950000101-kxxx-handled-v1",
                        "version": 1,
                        "from": "kxxx",
                        "role": "cc",
                    },
                },
                {
                    "token": '["edit","msg/1792950000102-pxx-edited",2]',
                    "held_at": "2026-10-25T18:03:00Z",
                    "routing": {
                        "event": "edit",
                        "key": "msg/1792950000102-pxx-edited",
                        "version": 2,
                        "from": "pxx",
                        "role": "cc",
                    },
                },
                {
                    "token": '["message","msg/1792950000103-txxxx-unverified",1]',
                    "held_at": "2026-10-25T18:04:00Z",
                    "routing": {
                        "event": "message",
                        "key": "msg/1792950000103-txxxx-unverified",
                        "version": 1,
                        "from": "txxxx",
                        "role": "cc",
                    },
                },
            ],
        }),
        encoding="utf-8",
    )

    res = orient.inspect_spool(spool, identities=["nxxxxx-prod"], now=orient._epoch("2026-10-25T18:10:00Z"))
    held_keys = [(item["key"], item["version"]) for item in res["held_cc"]]
    assert ("msg/1792950000101-kxxx-handled-v1", 1) not in held_keys
    assert ("msg/1792950000102-pxx-edited", 2) in held_keys
    assert ("msg/1792950000103-txxxx-unverified", 1) in held_keys


def test_help_wanted_surfaces_in_candidates_and_text_output(tmp_path):
    """Regression test for PR #412 review comment 4372128058:
    When `help_wanted` asks are present on the MeshyChat graph and all other sources are empty,
    `synthesize_candidates` emits a `mesh_graph.help_wanted` option and `format_orient_text`
    renders both the `help_wanted` count/items and the candidate option instead of claiming
    'No pending items detected across the 5 sources'."""
    report = {
        "seat": "antigravity",
        "identities": ["antigravity-prod"],
        "spool": {
            "available": True,
            "held_cc_count": 0,
            "held_cc": [],
            "uncollected": [],
            "recent_tail_messages": [],
        },
        "mesh_graph": {
            "available": True,
            "mode": "live",
            "seat": "antigravity",
            "reply_owed_count": 0,
            "reply_owed": [],
            "answered_still_open_count": 0,
            "answered_still_open": [],
            "awaiting_count": 0,
            "awaiting": [],
            "threads_with_news_count": 0,
            "threads_with_news": [],
            "help_wanted_count": 1,
            "help_wanted": [{
                "key": "msg/1792950000333-kxxx-333333",
                "author": "kxxx",
                "thread_id": "ops-help",
                "recorded_at": "2026-10-25T16:00:00Z",
            }],
        },
        "worktrees": {"scanned_count": 1, "actionable_count": 0, "worktrees": [], "actionable": []},
        "handovers_and_gates": {
            "handovers_found": 0,
            "own_handover": None,
            "stale_handovers": [],
            "gate_reports": [],
        },
        "brain": {
            "refused_env_present": [],
            "env_clean": True,
            "remediation": None,
            "staff": {"installed": False, "active": False, "identity": None},
            "elevated": {"installed": False, "active": False, "identity": None},
        },
    }
    candidates = orient.synthesize_candidates(report)
    report["candidate_options"] = candidates
    assert len(candidates) == 1
    assert candidates[0]["source"] == "mesh_graph.help_wanted"
    assert candidates[0]["keys"] == ["msg/1792950000333-kxxx-333333"]
    assert "broadcast help-wanted ask(s)" in candidates[0]["label"]

    text_out = orient.format_orient_text(report)
    assert "help_wanted=1" in text_out
    assert "HELP WANTED: msg/1792950000333-kxxx-333333 from kxxx on ops-help" in text_out
    assert "mesh_graph.help_wanted" in text_out
    assert "No pending items detected across the 5 sources" not in text_out

    # Also verify `scripts/brain-os.mjs` discovery next to `scripts/cf-access-headers.sh`
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    cf_sh = scripts_dir / "cf-access-headers.sh"
    brain_mjs = scripts_dir / "brain-os.mjs"
    cf_sh.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    brain_mjs.write_text("// brain-os\n", encoding="utf-8")
    brain_info = orient.inspect_brain_and_env(
        env={"PATH": "/usr/bin"},
        probe_identity=False,
        staff_script=cf_sh,
    )
    assert brain_info["staff"]["brain_os_script"] == brain_mjs.as_posix()
    assert "overview" in brain_info["staff"]["subcommands"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
