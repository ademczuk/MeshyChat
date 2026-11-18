"""Unit tests for Pullboard-inspired approval gates, verdict freshness projection,
merge-tree conflict checks, lane ownership, and `gate-check` CLI wiring (`tools/meshy_node/verdicts.py`).

Run:
  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_verdicts.py --noconftest -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, verdicts  # noqa: E402


def test_parse_and_validate_verdict_code_go():
    header = (
        "VERDICT: CODE GO; ITEM: MeshyChat#349; HEAD: 7ea91905a1b2c3d4; "
        "FAMILY: gemini; P1: 0; P2: 0; NEXT: ready to merge"
    )
    parsed = verdicts.parse_verdict_header(header)
    assert parsed is not None
    assert parsed["decision"] == "CODE GO"
    assert parsed["structured"] is True
    assert parsed["item"] == "MeshyChat#349"
    assert parsed["head"] == "7ea91905a1b2c3d4"
    assert parsed["family"] == "gemini"
    assert parsed["p1"] == 0
    assert parsed["p2"] == 0
    assert verdicts.validate_verdict(parsed) == []


def test_validate_verdict_code_go_refuses_missing_head_or_nonzero_p1():
    # Missing concrete hex HEAD / ARTIFACT_SHA256
    p_none = verdicts.parse_verdict_header("VERDICT: CODE GO; ITEM: #1; HEAD: none; P1: 0; P2: 0")
    errs_none = verdicts.validate_verdict(p_none)
    assert any("E_VERDICT_HEAD_REQUIRED" in e for e in errs_none)

    p_latest = verdicts.parse_verdict_header("VERDICT: GO; ITEM: #1; HEAD: latest; P1: 0")
    errs_latest = verdicts.validate_verdict(p_latest)
    assert any("E_VERDICT_HEAD_REQUIRED" in e for e in errs_latest)

    # P1 > 0 on CODE GO
    p_p1 = verdicts.parse_verdict_header("VERDICT: CODE GO; ITEM: #1; HEAD: abcdef123456; P1: 1; P2: 0")
    errs_p1 = verdicts.validate_verdict(p_p1)
    assert any("E_VERDICT_P1_NONZERO" in e for e in errs_p1)

    # 64-hex TAR_SHA256 satisfies positive decision even without HEAD
    sha64 = "a" * 64
    p_tar = verdicts.parse_verdict_header(f"VERDICT: CODE GO; ITEM: R334; TAR_SHA256: {sha64}; P1: 0; P2: 0")
    assert verdicts.validate_verdict(p_tar) == []


def test_validate_verdict_fix_first_guards_and_typed_causes():
    good_ff = (
        "VERDICT: FIX FIRST; ITEM: #348; HEAD: 1f5a2579a1b2; P1: 1; P2: 0; "
        "REASON: TEST_FAILURE; CAUSE: test:tests/test_ctx.py::test_kimi, file:tools/ctx.py:42"
    )
    parsed = verdicts.parse_verdict_header(good_ff)
    assert parsed is not None
    assert parsed["decision"] == "FIX FIRST"
    assert parsed["causes"] == ["test:tests/test_ctx.py::test_kimi", "file:tools/ctx.py:42"]
    assert verdicts.validate_verdict(parsed) == []

    # Zero defects (P1=0, P2=0) on FIX FIRST is refused
    zero_defects = "VERDICT: FIX FIRST; ITEM: #348; HEAD: 1f5a2579a1b2; P1: 0; P2: 0; REASON: OTHER"
    errs_zero = verdicts.validate_verdict(zero_defects)
    assert any("E_VERDICT_ZERO_DEFECTS" in e for e in errs_zero)

    # Invalid REASON enum
    bad_reason = "VERDICT: NO-GO; ITEM: #348; HEAD: 1f5a2579a1b2; P1: 1; P2: 0; REASON: NOT_A_REASON"
    errs_reason = verdicts.validate_verdict(bad_reason)
    assert any("E_VERDICT_BAD_REASON" in e for e in errs_reason)

    # Invalid CAUSE syntax
    bad_cause = "VERDICT: REVISE; ITEM: #348; HEAD: 1f5a2579a1b2; P1: 0; P2: 2; REASON: BEHAVIOR_MISMATCH; CAUSE: untyped_cause"
    errs_cause = verdicts.validate_verdict(bad_cause)
    assert any("E_VERDICT_BAD_CAUSE" in e for e in errs_cause)


def test_verifier_standing_self_verify_family_and_spawn_lineage():
    # 1. Self-verification is refused
    self_res = verdicts.check_verifier_standing("txxxx", "txxxx")
    assert not self_res["ok"] and self_res["code"] == "SELF_VERIFY"

    # 2. Builder-spawned subagent is refused via creator_map lineage walk
    creator_map = {
        "subagent-alpha": "worker-1",
        "worker-1": "txxxx",
        "txxxx": "coordinator",
        "antigravity": "coordinator",
    }
    sub_res = verdicts.check_verifier_standing("subagent-alpha", "txxxx", creator_map=creator_map)
    assert not sub_res["ok"] and sub_res["code"] == "VERIFIER_STANDING"

    # Independent seat under coordinator passes
    ind_res = verdicts.check_verifier_standing("antigravity", "txxxx", creator_map=creator_map)
    assert ind_res["ok"] and ind_res["verifier_family"] == "gemini" and ind_res["builder_family"] == "claude"

    # Cycle in creator_map is refused cleanly without infinite loop
    cycle_map = {"seat-a": "seat-b", "seat-b": "seat-a"}
    cyc_res = verdicts.check_verifier_standing("seat-a", "txxxx", creator_map=cycle_map)
    assert not cyc_res["ok"] and cyc_res["code"] == "VERIFIER_STANDING"

    # 3. Cross-family requirement (O2_FAMILY_MATCH)
    same_fam = verdicts.check_verifier_standing(
        "pxx", "txxxx", require_cross_family=True
    )
    assert not same_fam["ok"] and same_fam["code"] == "O2_FAMILY_MATCH"

    cross_fam = verdicts.check_verifier_standing(
        "codex-bxxxxx-cli", "txxxx", require_cross_family=True
    )
    assert cross_fam["ok"]


def test_check_head_is_new_blocks_resubmit_of_rejected_head():
    prior = [
        {"item": "PR-348", "decision": "FIX FIRST", "head": "111122223333", "criterion_digest": "dig-1"},
    ]
    # Exact same head and digest -> refused with HEAD_NOT_NEW
    same = verdicts.check_head_is_new("PR-348", "111122223333", "dig-1", prior)
    assert not same["ok"] and same["code"] == "HEAD_NOT_NEW"

    # New commit SHA -> allowed
    new_sha = verdicts.check_head_is_new("PR-348", "444455556666", "dig-1", prior)
    assert new_sha["ok"]

    # Refrozen criterion digest -> allowed
    new_dig = verdicts.check_head_is_new("PR-348", "111122223333", "dig-2", prior)
    assert new_dig["ok"]


def test_evaluate_quorum_multi_family_and_stale_head_classification():
    target_sha = "7ea91905a1b2c3d4e5f600001111222233334444"
    old_sha = "57de925b00001111222233334444555566667777"

    messages = [
        # Builder attempting to self-approve -> ignored and recorded in standing_refusals
        {
            "author": "oxxxxxx",
            "signature_verified": True,
            "body": f"VERDICT: CODE GO; ITEM: R334; HEAD: {target_sha}; P1: 0; P2: 0",
        },
        # Pxx approved the old SHA, not yet the new target_sha -> STALE_GO
        {
            "author": "pxx",
            "signature_verified": True,
            "body": f"VERDICT: CODE GO; ITEM: R334; HEAD: {old_sha}; P1: 0; P2: 0",
        },
        # Antigravity (gemini) approved the current target_sha -> VERIFIED_GO
        {
            "author": "antigravity",
            "signature_verified": True,
            "body": f"VERDICT: CODE GO; ITEM: R334; HEAD: 7ea91905a1b2; P1: 0; P2: 0",
        },
        # Codex approved the current target_sha -> VERIFIED_GO
        {
            "author": "codex-oxxxxxx-cli",
            "signature_verified": True,
            "body": f"VERDICT: CODE GO; ITEM: R334; HEAD: {target_sha}; P1: 0; P2: 0",
        },
    ]

    # When pxx is required, quorum is False because pxx is STALE_GO
    res_stale = verdicts.evaluate_quorum(
        messages,
        target_sha=target_sha,
        builder="oxxxxxx",
        required_seats=["pxx", "antigravity", "codex-oxxxxxx-cli"],
        required_families=["claude", "codex", "gemini"],
    )
    assert res_stale["quorum_met"] is False
    assert res_stale["stale_go_seats"] == ["pxx"]
    assert res_stale["verified_go_seats"] == ["antigravity", "codex-oxxxxxx-cli"]
    assert len(res_stale["standing_refusals"]) == 1
    assert res_stale["standing_refusals"][0]["code"] == "SELF_VERIFY"

    # Once pxx posts CODE GO on target_sha, all 3 families (claude, codex, gemini) are VERIFIED_GO
    messages.append({
        "author": "pxx",
        "signature_verified": True,
        "body": f"VERDICT: CODE GO; ITEM: R334; HEAD: {target_sha}; P1: 0; P2: 0",
    })
    res_ok = verdicts.evaluate_quorum(
        messages,
        target_sha=target_sha,
        builder="oxxxxxx",
        required_seats=["pxx", "antigravity", "codex-oxxxxxx-cli"],
        required_families=["claude", "codex", "gemini"],
    )
    assert res_ok["quorum_met"] is True
    assert res_ok["verified_families"] == ["claude", "codex", "gemini"]
    assert res_ok["stale_go_seats"] == []


def test_project_verdict_freshness_disjoint_vs_overlapping_paths():
    review = verdicts.capture_verdict_review(
        ROOT,
        item_id="PR-349",
        head_sha="aaaa1111",
        trunk_sha="0000base",
        changed_paths=["tools/postbox/delivery_bank.py", "tests/test_postbox_delivery_bank.py"],
    )
    assert review["paths"] == ["tests/test_postbox_delivery_bank.py", "tools/postbox/delivery_bank.py"]

    # 1. Trunk unchanged -> fresh
    unchanged = verdicts.project_verdict_freshness(
        None, review, tip_sha="0000base", current_head_sha="aaaa1111"
    )
    assert unchanged == {"stale": False, "reason": "TRUNK_UNCHANGED", "files": []}

    # 2. Main advanced, but only touched unrelated files -> fresh (DISJOINT_PATHS)
    disjoint = verdicts.project_verdict_freshness(
        None,
        review,
        tip_sha="1111newmain",
        current_head_sha="aaaa1111",
        main_touched_paths=["apps/context/validation.py", "README.md"],
    )
    assert disjoint == {"stale": False, "reason": "DISJOINT_PATHS", "files": []}

    # 3. Main advanced and touched one of the reviewed paths -> stale (STALE_VERDICT_PATH_OVERLAP)
    overlap = verdicts.project_verdict_freshness(
        None,
        review,
        tip_sha="2222newmain",
        current_head_sha="aaaa1111",
        main_touched_paths=["tools/postbox/delivery_bank.py", "README.md"],
    )
    assert overlap == {
        "stale": True,
        "reason": "STALE_VERDICT_PATH_OVERLAP",
        "files": ["tools/postbox/delivery_bank.py"],
    }

    # 4. Candidate head moved -> stale (HEAD_MOVED)
    moved = verdicts.project_verdict_freshness(
        None, review, tip_sha="0000base", current_head_sha="bbbb2222"
    )
    assert moved == {"stale": True, "reason": "HEAD_MOVED", "files": []}


def test_git_merge_tree_and_freshness_on_real_temp_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        res = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")

    (repo / "a.txt").write_text("line 1\n", encoding="utf-8")
    (repo / "b.txt").write_text("base b\n", encoding="utf-8")
    git("add", "a.txt", "b.txt")
    git("commit", "-m", "base")
    base_sha = git("rev-parse", "HEAD")

    # Feature branch modifies a.txt
    git("checkout", "-b", "feat")
    (repo / "a.txt").write_text("feature change\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-m", "feat a")
    feat_sha = git("rev-parse", "HEAD")

    # Capture verdict review at base_sha
    review = verdicts.capture_verdict_review(repo, item_id="1", head_sha=feat_sha, trunk_sha=base_sha)
    assert review["paths"] == ["a.txt"]

    # Advance main on disjoint file b.txt
    git("checkout", "main")
    (repo / "b.txt").write_text("main change to b\n", encoding="utf-8")
    git("add", "b.txt")
    git("commit", "-m", "main b")
    main_disjoint_sha = git("rev-parse", "HEAD")

    # Merge-tree check is clean and freshness projection is DISJOINT_PATHS
    mc_clean = verdicts.check_merge_conflicts(repo, main_disjoint_sha, feat_sha)
    assert mc_clean["clean"] is True and mc_clean["conflicting_files"] == []
    fresh = verdicts.project_verdict_freshness(repo, review, tip_sha=main_disjoint_sha)
    assert fresh == {"stale": False, "reason": "DISJOINT_PATHS", "files": []}

    # Now advance main with a conflicting edit to a.txt
    (repo / "a.txt").write_text("conflicting main change\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-m", "main conflict a")
    main_conflict_sha = git("rev-parse", "HEAD")

    mc_conflict = verdicts.check_merge_conflicts(repo, main_conflict_sha, feat_sha)
    assert mc_conflict["clean"] is False
    assert mc_conflict["conflicting_files"] == ["a.txt"]

    stale = verdicts.project_verdict_freshness(repo, review, tip_sha=main_conflict_sha)
    assert stale == {"stale": True, "reason": "STALE_VERDICT_PATH_OVERLAP", "files": ["a.txt"]}


def test_longest_prefix_lane_ownership_and_overlaps():
    lanes = {
        "postbox": ["tools/postbox/"],
        "postbox-plugin": ["tools/postbox/plugin/"],
        "meshy-node": {"owns": ["tools/meshy_node/"]},
    }
    # Longest prefix wins for nested folder
    assert verdicts.file_lane("tools/postbox/wait_next.py", lanes) == "postbox"
    assert verdicts.file_lane("tools/postbox/plugin/hook.py", lanes) == "postbox-plugin"
    assert verdicts.file_lane("tools/meshy_node/verdicts.py", lanes) == "meshy-node"
    assert verdicts.file_lane("README.md", lanes) == "coordinator"

    violations = verdicts.out_of_lane(
        ["tools/postbox/wait_next.py", "tools/postbox/plugin/hook.py", "CHANGELOG.md"],
        "postbox",
        lanes,
        shared=["CHANGELOG.md"],
    )
    assert violations == ["tools/postbox/plugin/hook.py (postbox-plugin's)"]

    overlaps = verdicts.check_lane_overlaps(
        {
            "pr-1": ["tools/postbox/wait_next.py"],
            "pr-2": ["tools/postbox/delivery_bank.py"],
            "pr-3": ["tools/meshy_node/cli.py"],
        },
        lanes,
    )
    assert overlaps["clean"] is False
    assert overlaps["lane_overlaps"] == [{"branches": ["pr-1", "pr-2"], "lanes": ["postbox"]}]


def test_cli_gate_check_subcommand(tmp_path, capsys):
    msgs_file = tmp_path / "messages.json"
    sha = "7ea91905a1b2c3d4"
    msgs_file.write_text(
        json.dumps([
            {"author": "antigravity", "signature_verified": True, "body": f"VERDICT: CODE GO; ITEM: #1; HEAD: {sha}; P1: 0; P2: 0"},
            {"author": "txxxx", "signature_verified": True, "body": f"VERDICT: CODE GO; ITEM: #1; HEAD: {sha}; P1: 0; P2: 0"},
        ]),
        encoding="utf-8",
    )
    rc = cli.main([
        "gate-check",
        "--messages-file", str(msgs_file),
        "--target-sha", sha,
        "--required-seats", "antigravity,txxxx",
        "--required-families", "gemini,claude",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["quorum_met"] is True
    assert out["verified_go_seats"] == ["antigravity", "txxxx"]


def test_evaluate_quorum_matches_only_explicit_header_target_sha_for_structured_verdicts():
    old_sha = "1111111111111111"
    cur_sha = "7ea91905a1b2c3d4"

    # 1. Structured CODE GO for old_sha mentioning cur_sha in body prose must NOT satisfy cur_sha
    stale_go_mentioning_cur = {
        "author": "antigravity",
        "signature_verified": True,
        "body": (
            f"VERDICT: CODE GO; ITEM: #1; HEAD: {old_sha}; P1: 0; P2: 0\n"
            f"Compared against baseline {cur_sha} during diff check."
        ),
    }
    res1 = verdicts.evaluate_quorum(
        [stale_go_mentioning_cur],
        target_sha=cur_sha,
        required_seats=["antigravity"],
        required_families=["gemini"],
    )
    assert res1["quorum_met"] is False
    assert res1["verified_go_seats"] == []

    # 2. Structured FIX FIRST for old_sha mentioning cur_sha in body prose must NOT block cur_sha
    stale_fix_mentioning_cur = {
        "author": "txxxx",
        "signature_verified": True,
        "body": (
            f"VERDICT: FIX FIRST; ITEM: #1; HEAD: {old_sha}; P1: 1; P2: 0\n"
            f"Please rebase onto {cur_sha} after fixing."
        ),
    }
    current_go = {
        "author": "antigravity",
        "signature_verified": True,
        "body": f"VERDICT: CODE GO; ITEM: #1; HEAD: {cur_sha}; P1: 0; P2: 0",
    }
    res2 = verdicts.evaluate_quorum(
        [stale_fix_mentioning_cur, current_go],
        target_sha=cur_sha,
        required_seats=["antigravity"],
        required_families=["gemini"],
    )
    assert res2["quorum_met"] is True
    assert res2["blocked_seats"] == []


def test_cli_gate_check_live_thread_flattened_read_and_signature_verification(monkeypatch, tmp_path, capsys):
    from tools.meshy_node import node

    sha = "7ea91905a1b2c3d4"
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(node, "load_state", lambda *_a, **_kw: {"address": "antigravity"})
    monkeypatch.setattr(
        node,
        "thread",
        lambda *_a, **_kw: {"messages": [{"key": "msg/1"}, {"key": "msg/2"}]},
    )

    # First run: flattened top-level dicts from node.read() with real read shape (signed_by UUID, author_signed False)
    read_rows = {
        "msg/1": {
            "key": "msg/1",
            "status": "OK",
            "status_verified": True,
            "signature_verified": True,
            "signed_by": "6b1f749c-1111-4222-8333-444455556666",
            "author_signed": False,
            "recorded_at": "2026-10-24T09:00:00Z",
            "author": "antigravity",
            "body": f"VERDICT: CODE GO; ITEM: #1; HEAD: {sha}; P1: 0; P2: 0",
        },
        "msg/2": {
            "key": "msg/2",
            "status": "OK",
            "status_verified": True,
            "signature_verified": False,
            "signed_by": "2d6d1628-bdc0-5034-a975-e0ffe6e2e9c4",
            "author_signed": False,
            "recorded_at": "2026-10-24T09:05:00Z",
            "author": "txxxx",
            "body": f"VERDICT: CODE GO; ITEM: #1; HEAD: {sha}; P1: 0; P2: 0",
        },
    }
    monkeypatch.setattr(node, "read", lambda _url, _st, key: read_rows[key])

    rc = cli.main([
        "gate-check",
        "--base-url", "http://127.0.0.1:1",
        "--state-file", str(state_file),
        "--thread", "hz-build",
        "--target-sha", sha,
        "--required-seats", "antigravity,txxxx",
        "--required-families", "gemini,claude",
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["quorum_met"] is False
    assert out["verified_go_seats"] == ["antigravity"]
    assert any(r["code"] == "UNVERIFIED_SIGNATURE" and r["seat"] == "txxxx" for r in out["standing_refusals"])

    # Second run: both signatures verified -> quorum_met is True
    read_rows["msg/2"]["signature_verified"] = True
    rc2 = cli.main([
        "gate-check",
        "--base-url", "http://127.0.0.1:1",
        "--state-file", str(state_file),
        "--thread", "hz-build",
        "--target-sha", sha,
        "--required-seats", "antigravity,txxxx",
        "--required-families", "gemini,claude",
    ])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["quorum_met"] is True
    assert out2["verified_go_seats"] == ["antigravity", "txxxx"]


    # Third run: if any message in the thread fails to read (e.g. a timed-out FIX FIRST read),
    # gate-check must fail closed with INCOMPLETE_THREAD_READ rather than evaluating partial messages.
    def _read_with_timeout(_url, _st, key):
        if key == "msg/2":
            raise node.ReadFailed({"error": node.E_READ_POLL_TIMEOUT, "message": "timeout"})
        return read_rows[key]

    monkeypatch.setattr(node, "read", _read_with_timeout)
    rc3 = cli.main([
        "gate-check",
        "--base-url", "http://127.0.0.1:1",
        "--state-file", str(state_file),
        "--thread", "hz-build",
        "--target-sha", sha,
        "--required-seats", "antigravity",
        "--required-families", "gemini",
    ])
    assert rc3 == 1
    out3 = json.loads(capsys.readouterr().out)
    assert out3["quorum_met"] is False
    assert out3["read_failures"] == [{"key": "msg/2", "code": "E_READ_POLL_TIMEOUT", "message": "timeout"}]
    assert {"reason": "INCOMPLETE_THREAD_READ", "failed_keys": ["msg/2"]} in out3["blockers"]

    # Fourth run: if thread projection coverage is incomplete (projected < total), gate-check
    # must fail closed with INCOMPLETE_THREAD_PROJECTION even when all projected messages are CODE GO.
    monkeypatch.setattr(node, "read", lambda _url, _st, key: read_rows[key])
    monkeypatch.setattr(
        node,
        "thread",
        lambda *_a, **_kw: {
            "messages": [{"key": "msg/1"}, {"key": "msg/2"}],
            "coverage": {"projected": 1, "total": 2},
        },
    )
    rc4 = cli.main([
        "gate-check",
        "--base-url", "http://127.0.0.1:1",
        "--state-file", str(state_file),
        "--thread", "hz-build",
        "--target-sha", sha,
        "--required-seats", "antigravity,txxxx",
        "--required-families", "gemini,claude",
    ])
    assert rc4 == 1
    out4 = json.loads(capsys.readouterr().out)
    assert out4["quorum_met"] is False
    assert out4["projection_coverage"] == {"projected": 1, "total": 2}
    assert {"reason": "INCOMPLETE_THREAD_PROJECTION", "projected": 1, "total": 2} in out4["blockers"]

    # Fifth run: --messages-file with real node.read rows (signed_by UUID, author_signed True/False)
    # and an earlier pre-028 row under another peer UUID must still reach VERIFIED_GO (N6, N7, N9).
    msg_file = tmp_path / "real_reads.json"
    msg_file.write_text(
        json.dumps([
            {
                "key": "msg/0",
                "signature_verified": True,
                "status_verified": True,
                "signed_by": "11111111-1111-4111-8111-111111111111",
                "author_signed": True,
                "recorded_at": "2026-10-24T08:00:00Z",
                "author": "txxxx",
                "body": "initial note",
            },
            read_rows["msg/1"],
            {**read_rows["msg/2"], "author_signed": True},
        ]),
        encoding="utf-8",
    )
    rc5 = cli.main([
        "gate-check",
        "--messages-file", str(msg_file),
        "--target-sha", sha,
        "--required-seats", "antigravity,txxxx",
        "--required-families", "gemini,claude",
    ])
    assert rc5 == 0
    out5 = json.loads(capsys.readouterr().out)
    assert out5["quorum_met"] is True
    assert out5["verified_go_seats"] == ["antigravity", "txxxx"]


def test_archive_sha256_target_requires_exact_digest_match_not_short_head_prefix():
    archive_sha256 = "abcdef123456" + ("0" * 52)

    # 1. A 12-char commit abbreviation in HEAD ('abcdef123456') must NEVER satisfy a 64-char archive SHA-256
    short_head_msg = {
        "author": "antigravity",
        "signature_verified": True,
        "body": "VERDICT: CODE GO; ITEM: R334; HEAD: abcdef123456; P1: 0; P2: 0",
    }
    res_short = verdicts.evaluate_quorum(
        [short_head_msg],
        target_sha=archive_sha256,
        required_seats=["antigravity"],
        required_families=["gemini"],
    )
    assert res_short["quorum_met"] is False
    assert res_short["verified_go_seats"] == []
    assert res_short["stale_go_seats"] == ["antigravity"]

    # 2. A 64-char TAR_SHA256 matches only on exact 64-character equality
    exact_tar_msg = {
        "author": "antigravity",
        "signature_verified": True,
        "body": f"VERDICT: CODE GO; ITEM: R334; TAR_SHA256: {archive_sha256}; P1: 0; P2: 0",
    }
    res_exact = verdicts.evaluate_quorum(
        [exact_tar_msg],
        target_sha=archive_sha256,
        required_seats=["antigravity"],
        required_families=["gemini"],
    )
    assert res_exact["quorum_met"] is True
    assert res_exact["verified_go_seats"] == ["antigravity"]

    # 3. Conversely, a 64-char TAR_SHA256 must not match a 12-char commit target sharing its prefix
    res_commit_target = verdicts.evaluate_quorum(
        [exact_tar_msg],
        target_sha="abcdef123456",
        required_seats=["antigravity"],
        required_families=["gemini"],
    )
    assert res_commit_target["quorum_met"] is False
    assert res_commit_target["stale_go_seats"] == ["antigravity"]


def test_path_normalization_preserves_leading_dots_on_dotfiles_and_dotdirs():
    review = verdicts.capture_verdict_review(
        ROOT,
        item_id="PR-351",
        head_sha="aaaa1111",
        trunk_sha="0000base",
        changed_paths=["./.env", ".github/workflows/ci.yml", "././.gitignore"],
    )
    assert review["paths"] == [".env", ".github/workflows/ci.yml", ".gitignore"]

    overlap_env = verdicts.project_verdict_freshness(
        None,
        review,
        tip_sha="1111newmain",
        current_head_sha="aaaa1111",
        main_touched_paths=[".env"],
    )
    assert overlap_env == {
        "stale": True,
        "reason": "STALE_VERDICT_PATH_OVERLAP",
        "files": [".env"],
    }

    overlap_ci = verdicts.project_verdict_freshness(
        None,
        review,
        tip_sha="2222newmain",
        current_head_sha="aaaa1111",
        main_touched_paths=["./.github/workflows/ci.yml"],
    )
    assert overlap_ci == {
        "stale": True,
        "reason": "STALE_VERDICT_PATH_OVERLAP",
        "files": [".github/workflows/ci.yml"],
    }


def test_parse_verdict_excludes_blockquotes_and_fenced_code_blocks():
    # 1. Quoted approval followed by actual FIX FIRST must parse as FIX FIRST
    body_quoted = (
        "> VERDICT: CODE GO; HEAD: abcdef123456; P1: 0\n\n"
        "VERDICT: FIX FIRST; HEAD: abcdef123456; P1: 1; P2: 0; REASON: TEST_FAILURE"
    )
    parsed_q = verdicts.parse_verdict_header(body_quoted)
    assert parsed_q is not None
    assert parsed_q["decision"] == "FIX FIRST"
    assert parsed_q["p1"] == 1

    # 2. Fenced code block containing an example CODE GO followed by actual FIX FIRST
    body_fenced = (
        "```text\n"
        "VERDICT: CODE GO; HEAD: abcdef123456; P1: 0\n"
        "```\n"
        "VERDICT: FIX FIRST; HEAD: abcdef123456; P1: 1"
    )
    parsed_f = verdicts.parse_verdict_header(body_fenced)
    assert parsed_f is not None
    assert parsed_f["decision"] == "FIX FIRST"

    # 3. Message containing ONLY quoted or fenced or inline-code verdicts returns None
    assert verdicts.parse_verdict_header("> VERDICT: CODE GO; HEAD: abcdef123456; P1: 0") is None
    assert verdicts.parse_verdict_header("```\nVERDICT: CODE GO; HEAD: abcdef123456; P1: 0\n```") is None
    assert verdicts.parse_verdict_header("Use `VERDICT: CODE GO` when ready.") is None


def test_round3_decision_normalization_and_fail_closed_quorum():
    sha = "f6db5330aa11223344556677889900aabbccddeeff0011223344556677889900"
    go_msg = {
        "author": "dxxxxx",
        "signature_verified": True,
        "body": f"VERDICT: CODE GO; HEAD: {sha}; P1: 0; P2: 0",
    }

    # 1. Punctuation, hyphens, spaces, and synonyms on VERDICT header lines must all block quorum
    for neg_body in (
        f"VERDICT: NO-GO. HEAD: {sha}; P1: 1",
        f"VERDICT: NO-GO, tests fail; HEAD: {sha}; P1: 1",
        f"VERDICT: NO-GO - tests fail; HEAD: {sha}; P1: 1",
        f"VERDICT: NO-GO: tests fail; HEAD: {sha}; P1: 1",
        f"VERDICT: NO-GO!; HEAD: {sha}; P1: 1",
        f"VERDICT: FIX-FIRST; HEAD: {sha}; P1: 1",
        f"VERDICT: FIX FIRST.; HEAD: {sha}; P1: 1",
        f"VERDICT: NO GO; HEAD: {sha}; P1: 1",
        f"VERDICT: NOGO; HEAD: {sha}; P1: 1",
        f"VERDICT: NOT GO; HEAD: {sha}; P1: 1",
        f"VERDICT: REJECT; HEAD: {sha}; P1: 1",
        f"VERDICT: BLOCK; HEAD: {sha}; P1: 1",
        f"NO-GO; HEAD: {sha}; P1: 1",
        "NO-GO",
        "```\nexample\nVERDICT: NO-GO; P1: 1",  # unclosed code fence
        "VERDICT - NO-GO; P1: 1",
        "VERDICT \u2014 NO-GO; P1: 1",
        "VERDICT \u2013 NO-GO; P1: 1",
        "VERDICT NO-GO; P1: 1",
        "Verdict = NO-GO; P1: 1",
        "**Verdict:** NO-GO; P1: 1",
        "[VERDICT] NO-GO; P1: 1",
        "VERDICT (round 3): NO-GO",
        "Verdict -- NO-GO",
        "VERDICT => NO-GO",
        f"my verdict on 350 was GO; VERDICT: NO-GO; HEAD: {sha}; P1: 1",
    ):
        res = verdicts.evaluate_quorum(
            [go_msg, {"author": "uxxx", "signature_verified": True, "body": neg_body}],
            target_sha=sha,
        )
        assert res["quorum_met"] is False, f"expected quorum_met=False for {neg_body!r}"
        assert res["seats"]["uxxx"]["status"] == "BLOCKED_FIX_FIRST", neg_body

    # 2. HOLD, unknown decisions, and parenthesized GO qualifiers on VERDICT header lines block quorum
    for non_go_body in (
        f"VERDICT: HOLD; HEAD: {sha}",
        f"VERDICT: DO NOT MERGE; HEAD: {sha}; P1: 1",
        f"VERDICT: GO ONLY AFTER P1 FIXED; HEAD: {sha}; P1: 2",
        f"VERDICT: GO (pending P1 fix); HEAD: {sha}; P1: 0",
    ):
        res = verdicts.evaluate_quorum(
            [go_msg, {"author": "uxxx", "signature_verified": True, "body": non_go_body}],
            target_sha=sha,
        )
        assert res["quorum_met"] is False, f"expected quorum_met=False for {non_go_body!r}"
        assert "uxxx" in res["blocked_seats"], non_go_body

    # 2b. Unstructured prose containing any negative token from a seat that currently stands at GO
    # withdraws its GO (dropping the seat to HOLD until it posts a new structured header), while
    # never blocking a third seat (0ecc4a).
    uxxx_go_msg = {
        "author": "uxxx",
        "signature_verified": True,
        "body": f"VERDICT: CODE GO; HEAD: {sha}; P1: 0; P2: 0",
    }
    for prose_neg in (
        f"My verdict is NO-GO at {sha}",
        "Our verdict was NO-GO",
        "Final verdict, NO-GO",
        "The verdict for 351 is NO-GO",
        "Verdict changed to NO-GO",
        "my verdict: not a go",
        "my verdict is a veto",
        "the verdict, sadly, is no-go",
        "verdict for this head: blocking",
        "I'm changing my verdict to NO-GO",
        "my verdict: 'NO-GO'",
        "+1 to kxxx: verdict NO-GO",
        "Verdict for 351 was NO-GO after the rerun",
        "Kxxx's verdict on 350 was GO. My verdict here: NO-GO.",
        'my verdict is "NO-GO"',
        "verdict stays NO-GO until the latch is fixed",
        "My verdict has changed: NO-GO",
        "In short: NO-GO from me, verdict stands",
        "verdict for this was NO-GO",
        "verdict on 5b24b0ef was NO-GO",
        "VERDICT:NO-GO",
        "verdict is NO-GO for 5b24b0ef",
        "verdict - do not merge",
        "Revised verdict -> NO GO",
        "Verdict (after rerun) - NO-GO",
        "verdict = reject",
        "tests fail on this head",
        "-1 from me",
        "not ready to merge",
        "changes requested",
        "I retract my approval",
        "withdrawing my GO",
        "please don't land this yet",
        "nope, broken on Windows",
        "Actually found a P1 in custody, please wait",
        "> VERDICT: NO-GO\n+1 to the above",
        "Hold for a moment while I re-check",
    ):
        withdrawn_res = verdicts.evaluate_quorum(
            [go_msg, uxxx_go_msg, {"author": "uxxx", "signature_verified": True, "body": prose_neg}],
            target_sha=sha,
            required_seats=["dxxxxx", "uxxx"],
        )
        assert withdrawn_res["quorum_met"] is False, f"expected GO withdrawal for {prose_neg!r}"
        assert "uxxx" in withdrawn_res["blocked_seats"], prose_neg
        assert "uxxx" not in withdrawn_res["verified_go_seats"], prose_neg
        # Posting a new structured CODE GO restores uxxx's VERIFIED_GO standing
        if verdicts.parse_verdict_header(prose_neg) is None:
            assert withdrawn_res["seats"]["uxxx"]["status"] == "HOLD", prose_neg
            restored_res = verdicts.evaluate_quorum(
                [go_msg, uxxx_go_msg, {"author": "uxxx", "signature_verified": True, "body": prose_neg}, uxxx_go_msg],
                target_sha=sha,
                required_seats=["dxxxxx", "uxxx"],
            )
            assert restored_res["quorum_met"] is True, prose_neg
            # A later structured header for a DIFFERENT sha must NOT launder the HOLD withdrawal (N25)
            other_sha = "1111222233334444555566667777888899990000"
            for other_body in (
                f"VERDICT: NO-GO; HEAD: {other_sha}; P1: 1",
                f"VERDICT: CODE GO; HEAD: {other_sha}; P1: 0; P2: 0",
            ):
                launder_res = verdicts.evaluate_quorum(
                    [
                        uxxx_go_msg,
                        {"author": "uxxx", "signature_verified": True, "body": prose_neg},
                        {"author": "uxxx", "signature_verified": True, "body": other_body},
                    ],
                    target_sha=sha,
                )
                assert launder_res["quorum_met"] is False, (prose_neg, other_body)
                assert launder_res["seats"]["uxxx"]["status"] == "HOLD", (prose_neg, other_body)

    # 2d. N21 + M2 pin: when required_seats is set:
    # - A structured negative from ANY authenticated seat (even outside required_seats) blocks quorum.
    # - Only required_seats count toward verified_go_seats / required_families, and prose withdrawal is
    #   limited to seats that count.
    n21_block = verdicts.evaluate_quorum(
        [
            go_msg,
            {"author": "codex-peer", "signature_verified": True, "body": f"VERDICT: NO-GO; HEAD: {sha}; P1: 1"},
        ],
        target_sha=sha,
        required_seats=["dxxxxx"],
    )
    assert n21_block["quorum_met"] is False
    assert n21_block["blocked_seats"] == ["codex-peer"]

    m2_nonreq_go = verdicts.evaluate_quorum(
        [
            go_msg,
            {"author": "codex-peer", "signature_verified": True, "body": f"VERDICT: CODE GO; HEAD: {sha}; P1: 0; P2: 0"},
            {"author": "uxxx", "signature_verified": True, "body": f"VERDICT: CODE GO; HEAD: {sha}; P1: 0; P2: 0"},
            {"author": "uxxx", "signature_verified": True, "body": "tests fail on this head"},
        ],
        target_sha=sha,
        required_seats=["dxxxxx"],
    )
    assert m2_nonreq_go["quorum_met"] is True
    assert m2_nonreq_go["verified_go_seats"] == ["dxxxxx"]
    assert m2_nonreq_go["blocked_seats"] == []

    m2_nonreq_fam = verdicts.evaluate_quorum(
        [
            go_msg,
            {"author": "codex-peer", "signature_verified": True, "body": f"VERDICT: CODE GO; HEAD: {sha}; P1: 0; P2: 0"},
        ],
        target_sha=sha,
        required_seats=["dxxxxx"],
        required_families=["claude", "codex"],
    )
    assert m2_nonreq_fam["quorum_met"] is False
    assert m2_nonreq_fam["missing_families"] == ["codex"]

    # 2c. Chatter from a third seat (or non-negative prose from a GO seat) never changes quorum (x7 + x8 corpora)
    for third_seat_chatter in (
        "waiting for kxxx's verdict on 351",
        "no verdict yet",
        "VERDICT pending",
        "see the verdict-check output",
        "```\nverdict notes",
        "my verdict stands",
        "kxxx's verdict on 350 was NO-GO, this is 351",
        "no verdict yet; the NO-GO was about CI",
        "verdict on 1111222233334444555566667777888899990000 was NO-GO",
        "I will not block on this verdict",
        "Revise the verdict template, unrelated",
        "the REJECT path in verdict tooling",
        'uxxx said "verdict: NO-GO"',
        "CI failed on main, waiting for the verdict",
        "thanks; uxxx's verdict on 350 was NO-GO but mine on 351 stands",
        "No verdict from me, gotta go",
        "the verdict on #350 is NO-GO",
        "verdict-bot: 3 GO, 1 NO-GO across the fleet today",
        "a verdict is GO only when P1 is zero",
        "re: verdict on 351 being NO-GO earlier, now fixed",
        "what's your verdict? NO-GO or GO?",
        "If anyone's verdict is NO-GO, ping me",
        "my verdict on the docs PR: NO-GO, but this one is fine",
    ):
        chat_res = verdicts.evaluate_quorum(
            [
                {**go_msg, "recorded_at": "2026-10-24T09:00:00Z"},
                {**uxxx_go_msg, "recorded_at": "2026-10-24T09:01:00Z"},
                {"author": "coordinator", "signature_verified": True, "recorded_at": "2026-10-24T09:05:00Z", "body": third_seat_chatter},
            ],
            target_sha=sha,
            required_seats=["dxxxxx", "uxxx"],
        )
        assert chat_res["quorum_met"] is True, f"third-seat chatter false-blocked: {third_seat_chatter!r}"
        # Also without required_seats specified, prose chatter from a third seat does not create a block
        chat_res_unscoped = verdicts.evaluate_quorum(
            [
                {**go_msg, "recorded_at": "2026-10-24T09:00:00Z"},
                {**uxxx_go_msg, "recorded_at": "2026-10-24T09:01:00Z"},
                {"author": "coordinator", "signature_verified": True, "recorded_at": "2026-10-24T09:05:00Z", "body": third_seat_chatter},
            ],
            target_sha=sha,
        )
        assert chat_res_unscoped["quorum_met"] is True, f"unscoped third-seat chatter false-blocked: {third_seat_chatter!r}"

    # 3. Unstructured GO prose and <12-hex HEAD tokens (e.g. 'defaced' or 7-hex prefix, F5/F8) are refused
    errs_mid_prose = verdicts.validate_verdict(f"All checks green on {sha} - VERDICT: GO")
    assert any("E_VERDICT_MISSING" in e for e in errs_mid_prose)
    errs_unstruct = verdicts.validate_verdict(f"VERDICT: GO\nAll checks green on {sha}")
    assert any("E_VERDICT_UNSTRUCTURED" in e for e in errs_unstruct)
    errs_go_for = verdicts.validate_verdict(f"VERDICT: GO for {sha}, all tests pass")
    assert len(errs_go_for) >= 1
    defaced_target = "defaced11112222333344445555666677778888"
    for bad_head_body in (
        "VERDICT: GO; HEAD: defaced; P1: 0",
        f"VERDICT: GO; HEAD: {defaced_target[:7]}; P1: 0",
    ):
        errs_short = verdicts.validate_verdict(bad_head_body)
        assert any("E_VERDICT_HEAD_REQUIRED" in e for e in errs_short)
        res_short_q = verdicts.evaluate_quorum(
            [{"author": "dxxxxx", "signature_verified": True, "body": bad_head_body}],
            target_sha=defaced_target,
        )
        assert res_short_q["quorum_met"] is False
        assert res_short_q["seats"]["dxxxxx"]["status"] == "INVALID_VERDICT"


def test_round3_family_spoof_signature_required_and_chronological_ordering():
    sha = "f6db5330aa11223344556677889900aabbccddeeff0011223344556677889900"
    good = f"VERDICT: CODE GO; HEAD: {sha}; P1: 0; P2: 0"

    # 1. Self-asserted FAMILY: codex from a claude seat (dxxxxx) or unknown seat (zed) is refused with FAMILY_SPOOF,
    # and an unknown seat without family fails require_cross_family with O2_FAMILY_MATCH (F2)
    for spoof_msg in (
        {"author": "dxxxxx", "signature_verified": True, "body": good + "; FAMILY: codex"},
        {"author": "zed", "author_family": "codex", "signature_verified": True, "body": good},
        {"author": "zed", "signature_verified": True, "body": good + "; FAMILY: codex"},
    ):
        spoof_res = verdicts.evaluate_quorum(
            [spoof_msg],
            target_sha=sha,
            builder="txxxx",
            require_cross_family=True,
        )
        assert spoof_res["quorum_met"] is False
        assert any(r["code"] == "FAMILY_SPOOF" for r in spoof_res["standing_refusals"])

    unknown_cross = verdicts.evaluate_quorum(
        [{"author": "zed", "signature_verified": True, "body": good}],
        target_sha=sha,
        builder="txxxx",
        require_cross_family=True,
    )
    assert unknown_cross["quorum_met"] is False
    assert any(r["code"] == "O2_FAMILY_MATCH" for r in unknown_cross["standing_refusals"])

    # 2. Missing, None, 0, 'false', or False signature_verified or status_verified=False is refused (M9)
    for extra in (
        {},
        {"signature_verified": None},
        {"signature_verified": 0},
        {"signature_verified": "false"},
        {"signature_verified": False},
        {"signature_verified": True, "status_verified": False},
        {"signature_verified": True, "status_verified": None},
    ):
        unv_res = verdicts.evaluate_quorum(
            [{"author": "dxxxxx", "body": good, **extra}],
            target_sha=sha,
        )
        assert unv_res["quorum_met"] is False
        assert any(r["code"] == "UNVERIFIED_SIGNATURE" for r in unv_res["standing_refusals"])

    # 3. signed_by mismatch is refused with SIGNER_MISMATCH even if author_signed is True (N1)
    for bad_sig_msg in (
        {"author": "codex-peer", "signed_by": "other-peer", "signature_verified": True, "body": good},
        {"author": "codex-peer", "signed_by": "attacker", "author_signed": True, "signature_verified": True, "body": good},
    ):
        mismatch_res = verdicts.evaluate_quorum(
            [bad_sig_msg],
            target_sha=sha,
            required_seats=["codex-peer"],
        )
        assert mismatch_res["quorum_met"] is False
        assert any(r["code"] == "SIGNER_MISMATCH" for r in mismatch_res["standing_refusals"])

    # 4. Duplicate header fields and missing P1 on GO are refused
    assert any("E_VERDICT_DUPLICATE_FIELD" in e for e in verdicts.validate_verdict(f"VERDICT: GO; HEAD: {sha}; P1: 2; P1: 0"))
    assert any("E_VERDICT_DUPLICATE_FIELD" in e for e in verdicts.validate_verdict(f"VERDICT: GO; HEAD: 111122223333; COMMIT: {sha}; P1: 0"))
    assert any("E_VERDICT_P1_REQUIRED" in e for e in verdicts.validate_verdict(f"VERDICT: GO; HEAD: {sha}"))

    # 5. Newest-first and mixed-timestamp message lists evaluate chronologically / fail closed so NO-GO wins (F7/N5)
    old_go = {"author": "dxxxxx", "signature_verified": True, "recorded_at": "2026-10-20T10:00:00Z", "body": good}
    new_nogo = {"author": "dxxxxx", "signature_verified": True, "recorded_at": "2026-10-20T11:00:00Z", "body": f"VERDICT: NO-GO; HEAD: {sha}; P1: 1"}
    untimed_nogo = {"author": "dxxxxx", "signature_verified": True, "body": f"VERDICT: NO-GO; HEAD: {sha}; P1: 1"}
    mid_hold = {"author": "dxxxxx", "signature_verified": True, "recorded_at": "2026-10-20T09:00:00Z", "body": f"VERDICT: HOLD; HEAD: {sha}"}
    mid_other_nogo = {"author": "dxxxxx", "signature_verified": True, "recorded_at": "2026-10-20T09:00:00Z", "body": "VERDICT: NO-GO; HEAD: 1111222233334444; P1: 1"}
    untimed_go = {"author": "dxxxxx", "signature_verified": True, "body": good}
    bad_ts_go = {"author": "dxxxxx", "signature_verified": True, "recorded_at": "not-a-time", "body": good}
    for msg_list in (
        [new_nogo, old_go],
        [new_nogo, untimed_go],
        [new_nogo, bad_ts_go],
        [untimed_nogo, untimed_go],
        [untimed_go, untimed_nogo],
        [untimed_nogo, mid_hold, old_go],
        [untimed_nogo, mid_other_nogo, old_go],
    ):
        chrono_res = verdicts.evaluate_quorum(msg_list, target_sha=sha)
        assert chrono_res["quorum_met"] is False
        assert chrono_res["seats"]["dxxxxx"]["status"] == "BLOCKED_FIX_FIRST"

    # Untimestamped block bound to an OLD sha or unbound does NOT permanently latch when a timestamped GO on target_sha exists (N13)
    for prior_untimed_block in (
        {"author": "dxxxxx", "signature_verified": True, "body": "VERDICT: NO-GO; HEAD: 1111222233334444; P1: 1"},
        {"author": "dxxxxx", "signature_verified": True, "body": "verdict: NO-GO"},
        {"author": "dxxxxx", "signature_verified": True, "body": "VERDICT: NO-GO; P1: 1"},
        {"author": "dxxxxx", "signature_verified": True, "body": "VERDICT: HOLD"},
        {"author": "dxxxxx", "signature_verified": True, "body": "VERDICT: GO; HEAD: 1234567; P1: 0"},
    ):
        unlatched_res = verdicts.evaluate_quorum([prior_untimed_block, old_go], target_sha=sha)
        assert unlatched_res["quorum_met"] is True
        assert unlatched_res["seats"]["dxxxxx"]["status"] == "VERIFIED_GO"
        # Whereas an untimestamped GO still cannot clear an untimestamped unbound block
        still_blocked_res = verdicts.evaluate_quorum([untimed_go, prior_untimed_block], target_sha=sha)
        assert still_blocked_res["quorum_met"] is False


def test_round6_expected_peer_and_live_server_author_binding(monkeypatch, tmp_path, capsys):
    from tools.meshy_node import node

    sha = "7ea91905a1b2c3d4"
    kxxx_uuid = "99999999-1111-4222-8333-444455556666"
    wrong_uuid = "00000000-1111-4222-8333-444455556666"

    # 1. --expected-peer pinning (N14): matching UUID passes (rc 0), wrong UUID or missing signed_by fails with SIGNER_MISMATCH (rc 1),
    # and malformed --expected-peer fails with rc 2.
    msg_file = tmp_path / "expected_peer.json"
    msg_file.write_text(
        json.dumps([
            {
                "key": "msg/1",
                "author": "kxxx",
                "signature_verified": True,
                "status_verified": True,
                "signed_by": kxxx_uuid,
                "body": f"VERDICT: CODE GO; ITEM: #351; HEAD: {sha}; P1: 0; P2: 0",
            }
        ]),
        encoding="utf-8",
    )
    rc_ok = cli.main([
        "gate-check",
        "--messages-file", str(msg_file),
        "--target-sha", sha,
        "--required-seats", "kxxx",
        "--expected-peer", f"KXXX={kxxx_uuid.upper()}",
    ])
    assert rc_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["quorum_met"] is True
    assert out_ok["verified_go_seats"] == ["kxxx"]

    rc_wrong = cli.main([
        "gate-check",
        "--messages-file", str(msg_file),
        "--target-sha", sha,
        "--required-seats", "kxxx",
        "--expected-peer", f"kxxx={wrong_uuid}",
    ])
    assert rc_wrong == 1
    out_wrong = json.loads(capsys.readouterr().out)
    assert out_wrong["quorum_met"] is False
    assert any(r["code"] == "SIGNER_MISMATCH" and r["seat"] == "kxxx" for r in out_wrong["standing_refusals"])

    msg_file_no_sig = tmp_path / "expected_peer_nosig.json"
    msg_file_no_sig.write_text(
        json.dumps([
            {
                "key": "msg/1",
                "author": "kxxx",
                "signature_verified": True,
                "status_verified": True,
                "body": f"VERDICT: CODE GO; ITEM: #351; HEAD: {sha}; P1: 0; P2: 0",
            }
        ]),
        encoding="utf-8",
    )
    rc_nosig = cli.main([
        "gate-check",
        "--messages-file", str(msg_file_no_sig),
        "--target-sha", sha,
        "--required-seats", "kxxx",
        "--expected-peer", f"kxxx={kxxx_uuid}",
    ])
    assert rc_nosig == 1
    out_nosig = json.loads(capsys.readouterr().out)
    assert out_nosig["quorum_met"] is False
    assert any(r["code"] == "SIGNER_MISMATCH" and r["seat"] == "kxxx" for r in out_nosig["standing_refusals"])

    rc_bad_arg = cli.main([
        "gate-check",
        "--messages-file", str(msg_file),
        "--target-sha", sha,
        "--expected-peer", "kxxx=",
    ])
    assert rc_bad_arg == 2
    capsys.readouterr()

    # 2. Live gate-check takes author ONLY from server-stamped msg_doc["author"] and refuses null author (N11)
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(node, "load_state", lambda *_a, **_kw: {"address": "antigravity"})
    monkeypatch.setattr(
        node,
        "thread",
        lambda *_a, **_kw: {"messages": [{"key": "msg/spoof"}]}
    )
    # Case 2a: server-stamped author is 'uxxx', but nested value claims 'kxxx'
    monkeypatch.setattr(
        node,
        "read",
        lambda _u, _s, key: {
            "key": key,
            "author": "uxxx",
            "signature_verified": True,
            "status_verified": True,
            "signed_by": kxxx_uuid,
            "value": {
                "author": "kxxx",
                "from": "kxxx",
                "body": f"VERDICT: CODE GO; ITEM: #351; HEAD: {sha}; P1: 0; P2: 0",
            },
        },
    )
    rc_spoof = cli.main([
        "gate-check",
        "--base-url", "http://127.0.0.1:1",
        "--state-file", str(state_file),
        "--thread", "pr-351",
        "--target-sha", sha,
        "--required-seats", "kxxx",
    ])
    assert rc_spoof == 1
    out_spoof = json.loads(capsys.readouterr().out)
    assert out_spoof["quorum_met"] is False
    assert out_spoof["seats"]["kxxx"]["status"] == "MISSING"
    assert out_spoof["seats"]["uxxx"]["status"] == "VERIFIED_GO"

    # Case 2b: server-stamped author is None, writer-controlled 'from' is 'kxxx' -> refused as E_MISSING_SERVER_AUTHOR
    monkeypatch.setattr(
        node,
        "read",
        lambda _u, _s, key: {
            "key": key,
            "author": None,
            "from": "kxxx",
            "signature_verified": True,
            "status_verified": True,
            "signed_by": kxxx_uuid,
            "body": f"VERDICT: CODE GO; ITEM: #351; HEAD: {sha}; P1: 0; P2: 0",
        },
    )
    rc_null_author = cli.main([
        "gate-check",
        "--base-url", "http://127.0.0.1:1",
        "--state-file", str(state_file),
        "--thread", "pr-351",
        "--target-sha", sha,
        "--required-seats", "kxxx",
    ])
    assert rc_null_author == 1
    out_null = json.loads(capsys.readouterr().out)
    assert out_null["quorum_met"] is False
    assert out_null["read_failures"] == [
        {"key": "msg/spoof", "code": "E_MISSING_SERVER_AUTHOR", "message": "server-stamped author is missing or null"}
    ]

    # Case 2c: live row with a non-string body is recorded in read_failures as E_INVALID_MESSAGE_BODY (N17)
    monkeypatch.setattr(
        node,
        "read",
        lambda _u, _s, key: {
            "key": key,
            "author": "kxxx",
            "signature_verified": True,
            "status_verified": True,
            "signed_by": kxxx_uuid,
            "body": ["not", "a", "string"],
        },
    )
    rc_bad_body = cli.main([
        "gate-check",
        "--base-url", "http://127.0.0.1:1",
        "--state-file", str(state_file),
        "--thread", "pr-351",
        "--target-sha", sha,
        "--required-seats", "kxxx",
    ])
    assert rc_bad_body == 1
    out_bad_body = json.loads(capsys.readouterr().out)
    assert out_bad_body["quorum_met"] is False
    assert out_bad_body["read_failures"] == [
        {"key": "msg/spoof", "code": "E_INVALID_MESSAGE_BODY", "message": "message body is missing or not a string"}
    ]


def test_unauthenticated_stale_rejection_never_replaces_blocking_or_verified_verdict():
    """GitHub Codex r4366663830: an unverified or signer-mismatched rejection naming a different SHA
    (STALE_FIX_FIRST) must never overwrite an existing BLOCKED_FIX_FIRST or authenticated VERIFIED_GO."""
    target = "7ea91905a1b2c3d4"
    other = "1111222233334444"
    kxxx_uuid = "99999999-1111-4222-8333-444455556666"
    dxxxxx_uuid = "88888888-1111-4222-8333-444455556666"
    attacker_uuid = "00000000-1111-4222-8333-444455556666"

    kxxx_go = {
        "key": "msg/1",
        "author": "kxxx",
        "signature_verified": True,
        "signed_by": kxxx_uuid,
        "recorded_at": "2026-10-25T05:00:00Z",
        "body": f"VERDICT: CODE GO; ITEM: #351; HEAD: {target}; P1: 0; P2: 0",
    }
    dxxxxx_block = {
        "key": "msg/2",
        "author": "dxxxxx",
        "signature_verified": True,
        "signed_by": dxxxxx_uuid,
        "recorded_at": "2026-10-25T05:01:00Z",
        "body": f"VERDICT: FIX FIRST; ITEM: #351; HEAD: {target}; P1: 1; P2: 0",
    }
    for spoof_msg in (
        {
            "key": "msg/3",
            "author": "dxxxxx",
            "signature_verified": False,
            "recorded_at": "2026-10-25T05:02:00Z",
            "body": f"VERDICT: FIX FIRST; ITEM: #351; HEAD: {other}; P1: 1; P2: 0",
        },
        {
            "key": "msg/3",
            "author": "dxxxxx",
            "signature_verified": True,
            "signed_by": attacker_uuid,
            "recorded_at": "2026-10-25T05:02:00Z",
            "body": f"VERDICT: FIX FIRST; ITEM: #351; HEAD: {other}; P1: 1; P2: 0",
        },
    ):
        res = verdicts.evaluate_quorum(
            [kxxx_go, dxxxxx_block, spoof_msg],
            target_sha=target,
            expected_peers={"kxxx": kxxx_uuid, "dxxxxx": dxxxxx_uuid},
        )
        assert res["quorum_met"] is False
        assert res["seats"]["dxxxxx"]["status"] == "BLOCKED_FIX_FIRST"

    # Unauthenticated STALE_FIX_FIRST targeting kxxx must not downgrade kxxx's authenticated VERIFIED_GO
    spoof_kxxx_stale = {
        "key": "msg/4",
        "author": "kxxx",
        "signature_verified": True,
        "signed_by": attacker_uuid,
        "recorded_at": "2026-10-25T05:03:00Z",
        "body": f"VERDICT: FIX FIRST; ITEM: #351; HEAD: {other}; P1: 1; P2: 0",
    }
    res2 = verdicts.evaluate_quorum(
        [kxxx_go, spoof_kxxx_stale],
        target_sha=target,
        expected_peers={"kxxx": kxxx_uuid},
    )
    assert res2["quorum_met"] is True
    assert res2["seats"]["kxxx"]["status"] == "VERIFIED_GO"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
