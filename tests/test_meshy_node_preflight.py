"""Tests for sender merge-ask guard, merger preflight, review preflight, and orient hook."""
from __future__ import annotations

from datetime import datetime, timezone
import json, subprocess, sys, uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node, orient, preflight, sendguard, wire  # noqa: E402

BASE_URL = "https://mesh.example.invalid"
SHA_A, SHA_B = "7dd257a4af8433421065ef21a55171d23792cf84", "aaaaaaaa11111111222222223333333344444444"


def _write_state(tmp_path: Path, *, label: str = "pxx") -> tuple[Path, dict]:
    seed = wire.new_seed()
    st = {"schema": node.STATE_SCHEMA, "base_url": BASE_URL, "status": "minted", "label": label, "signing_seed_b64url": wire.b64u(seed), "public_key_b64url": wire.b64u(wire.public_key_of(seed)), "peer_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4()), "kid": f"kid-{label}", "jwt": "h.p.s", "jwt_expires_at": "2999-01-01T00:00:00Z", "memory_peer_id": node.DEFAULT_MEMORY_PEER_ID}
    path = tmp_path / f"{label}.json"
    path.write_text(json.dumps(st), encoding="utf-8")
    return path, st


@pytest.mark.parametrize("to,cc,body,substr", [
    (["txxxx", "pxx"], None, "please squash #427 once mg427 passes", "merger:"), (["pxx"], None, "please merge #407 when green", "merger:"),
    (["pxx", "txxxx"], None, f"someone merge #407 at {SHA_A}", "merger:"), (["pxx", "txxxx"], None, f"either of you can merge #407 at {SHA_A}", "merger:"),
    (["pxx"], None, f"merge #407 when mg407 passes ({SHA_A})", "merger:"), (["pxx"], None, f"gate passed on #407 ({SHA_A}), then a pinned squash", "merger:"),
    (["pxx"], None, f"PR #407 is ready for pinned squash ({SHA_A})", "merger:"), (["pxx"], None, f"please run gh pr merge 407 --squash ({SHA_A})", "merger:"),
    (["pxx", "txxxx"], None, "Please run `gh pr merge 427 --squash` now.", "merger:"), (["pxx", "txxxx"], None, 'Please run "gh pr merge 427 --squash" now.', "merger:"),
    (["pxx", "txxxx"], None, "`gh pr merge 427 --squash`", "merger:"), (["pxx", "txxxx"], None, "Please run:\n```bash\ngh pr merge 427 --squash\n```", "merger:"),
    (["pxx", "txxxx"], None, f"please squash #407\nmerger: pxx\nmerger: txxxx\nhead: {SHA_A}", "2 `merger:`"),
    (["pxx", "txxxx"], None, f"please squash #407 ({SHA_A}); merger: pxx; merger: txxxx", "2 `merger:`"),
    (["pxx", "txxxx"], None, f"please squash #407\nmerger: pxx or txxxx\nhead: {SHA_A}", "ambiguous"), (["pxx", "txxxx"], None, f"please squash #407\nmerger: pxx/txxxx\nhead: {SHA_A}", "ambiguous"),
    (["pxx", "txxxx"], None, f"please squash #407\nmerger: any\nhead: {SHA_A}", "ambiguous"), (["pxx", "txxxx"], None, f"please squash #407\nmerger: either\nhead: {SHA_A}", "ambiguous"),
    (["pxx", "txxxx"], None, f"please squash #407\nmerger: tbd\nhead: {SHA_A}", "ambiguous"), (["pxx", "txxxx"], None, f"please squash #407\nmerger: none\nhead: {SHA_A}", "ambiguous"),
    (["txxxx"], None, f"please squash #407\nmerger: pxx\nhead: {SHA_A}", "not among message recipients"),
    (["pxx"], None, "please squash #407 at 2ae1d5eb\nmerger: pxx", "40-hex"), (["txxxx"], ["Pxx"], "please squash #407 at 2ae1d5eb\nmerger: pxx", "40-hex"),
    (["pxx"], None, f"> merger: pxx\n> {SHA_A}\nplease squash #427 once mg427 passes", "merger:"),
    (["pxx", "txxxx"], ["memory-bxxxxx"], f"#427 is ready for a non-author squash under the recorded rule, pinned to {SHA_A}. Whoever is free first, please.", "merger:"),
    (["pxx", "txxxx"], None, "Do not merge #426 yet. Please merge #427 now.", "merger:"), (["pxx", "txxxx"], None, "Do not merge #428 but please merge #427 now", "merger:"),
    (["pxx", "txxxx"], None, "No blockers remain so please merge #427 now.", "merger:"), (["pxx", "txxxx"], None, "With no conflicts #427 is ready to merge.", "merger:"),
    (["pxx", "txxxx"], None, f"Please merge commit {SHA_A} for #427 now.", "merger:"), (["pxx"], None, "pxx, merge #427 now", "merger:"),
    (["pxx"], None, f"Please merge #427\nmerger: pxx\nhead: {SHA_A}\nhead: {SHA_B}", "multiple distinct 40-hex"), (["pxx"], None, f"Please merge #427\nmerger: pxx\nbase: {SHA_A}\nhead: {SHA_B[:8]}", "40-hex"),
    (["pxx", "txxxx"], None, "Please merge https://github.com/owner/repo/pull/427", "merger:"), (["pxx", "txxxx"], None, f"Merge https://github.com/owner/repo/pull/427 at {SHA_A}", "merger:"),
])
def test_sendguard_refuses_bad_merge_requests(tmp_path, to, cc, body, substr):
    st_file = tmp_path / "state.json"
    assert any(substr in item for item in sendguard.refusals(st_file, to=to, cc=cc, body=body, now=1000.0))
    with pytest.raises(node.Refused, match="refused before sending"):
        sendguard.admit(st_file, to=to, cc=cc, body=body, subject=None, bulk_ack=None, refused=node.Refused, now=1000.0)


@pytest.mark.parametrize("to,cc,body", [
    (["pxx"], ["txxxx"], f"please squash #407 once mg427 passes\nmerger: pxx\nhead: {SHA_A}"), (["pxx"], None, f"Please merge #427\nmerger: pxx.\nhead: {SHA_A}"),
    (["pxx"], ["txxxx"], f"Please run `gh pr merge 427 --squash` now.\nmerger: pxx\nhead: {SHA_A}"),
    (["pxx"], None, f"pxx, as merger: please merge #427\nmerger: pxx\nhead: {SHA_A}"), (["txxxx"], ["Pxx"], f"PR #407 ready to merge\nmerger: pxx\ncommit: {SHA_A.upper()}"),
    (["txxxx", "pxx"], None, f"#427 is merged as {SHA_A}"), (["txxxx"], None, "merged #407 as 2ae1d5eb"),
    (["txxxx"], None, "already merged #407; PR #405 was merged; #404 has been merged"), (["txxxx"], None, "after #405 merges and once #406 lands, rebase before merging #407"),
    (["txxxx"], None, "git merge-tree --write-tree clean on #407; no merge conflict, merge commit, or gate reuse issue"),
    (["txxxx"], None, "please review #407, do not merge yet"), (["txxxx"], None, "hold merge on #407; #407 is not ready to merge"),
    (["txxxx"], None, "please review #407 before we merge"), (["txxxx"], None, "Do not ask anyone to please merge #427."),
    (["txxxx"], None, 'Example: "please merge #427" or `please merge #427`'), (["txxxx"], None, "Example: `gh pr merge 427 --squash`"),
    (["txxxx"], None, "Please do not run `gh pr merge 427 --squash` now."), (["txxxx"], None, "The command `gh pr merge 427 --squash` failed."),
    (["txxxx"], None, "Example:\n~~~\nplease merge #427\n~~~\n    please merge #427"), (["txxxx"], None, "ack, I'll merge #407 now"),
    (["txxxx"], None, f"VERDICT: CODE GO; ITEM: MeshyChat#407; HEAD: {SHA_A}; FAMILY: gemini; P1: 0; P2: 0; NEXT: ready to merge"),
    (["txxxx", "pxx"], None, "Example:\n```\nplease squash #427 once mg427 passes\n```\n> please squash #427"),
    (["txxxx", "pxx"], None, "Can someone review #427? Whoever is free first, please."),
])
def test_sendguard_admits_valid_merge_requests_and_non_merge_prose(tmp_path, to, cc, body):
    assert sendguard.extract_merger_declarations("pxx, as merger: hold\nmerger: pxx.") == ["pxx"]
    cleaned = sendguard.strip_fences_and_quotes(f'Example: "please merge #427" `txxxx will merge #427` `{SHA_A}`')
    assert "please merge" not in cleaned and "txxxx will merge" not in cleaned and SHA_A in cleaned
    st_file = tmp_path / "state.json"
    assert sendguard.check_merge_request(body, to=to, cc=cc) is None and sendguard.refusals(st_file, to=to, cc=cc, body=body, now=1000.0) == []


def test_preflight_merge_checks_and_cli(monkeypatch, tmp_path, capsys):
    sf, st = _write_state(tmp_path, label="pxx")
    monkeypatch.setattr(node, "load_state", lambda path, url: st)
    now_s = 1_760_000_000.0
    iso = lambda age_m: datetime.fromtimestamp(now_s - age_m * 60.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    assert preflight.check_merge_preflight(sf, st, pr=427, head="2ae1d5eb", thread_id="t-427", now=now_s)["code"] == "invalid_head"
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", window_minutes=-1, now=now_s)["code"] == "invalid_window"
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", window_minutes=float("nan"), now=now_s)["code"] == "invalid_window"
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", checked_by_hand=["bad-key"], now=now_s)["code"] == "invalid_checked_by_hand"
    sub = subprocess.run([sys.executable, "-m", "tools.meshy_node.preflight", "merge", "--pr", "427", "--head", "2ae1d5eb", "--thread", "t-427", "--json"], cwd=str(ROOT), capture_output=True, text=True)
    assert sub.returncode == 1 and json.loads(sub.stdout)["code"] == "invalid_head"

    def mock_thread(msgs: list[dict], *, truncated: bool = False, hits: list[dict] | None = None, extra: dict | None = None) -> list[dict]:
        seen_limits: list[dict] = []
        by_key = {m["key"]: m for m in msgs}
        hs = hits if hits is not None else [{"key": m["key"], "recorded_at": m.get("recorded_at")} for m in msgs]
        monkeypatch.setattr(node, "thread", lambda *a, **kw: (seen_limits.append(kw) or {"thread_id": "t-427", "truncated": truncated, "hits": hs, **(extra or {})}))
        monkeypatch.setattr(node, "read", lambda _sf, _st, *, key, **kw: by_key[key])
        return seen_limits

    seen_lim = mock_thread([{"key": "msg/old", "from": "bxxxxx", "recorded_at": iso(60), "signature_verified": True, "body": "old"}], truncated=True)
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "thread_truncated" and seen_lim[0].get("limit") is None
    mock_thread([], extra={"hits": None})
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "invalid_thread"

    for bad_cov in (None, {"projected": "100", "total": 100}, {"projected": 102, "total": 101}, {"projected": -1, "total": 10}):
        mock_thread([], extra={"coverage": bad_cov})
        assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "incomplete_projection"
    monkeypatch.setattr(node, "invoke", lambda *a, **kw: {"status": "failed", "result_signature_verified": False, "result": {}})
    mock_thread([], extra={"coverage": {"projected": 100, "total": 101}})
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "incomplete_projection"

    unproj_claim = {"key": "msg/unproj", "from": "txxxx", "recorded_at": iso(1), "signature_verified": True, "body": "I will merge #427 now"}
    gp_v1 = {"key": "msg/gp", "version": 1, "from": "bxxxxx", "recorded_at": iso(12), "signature_verified": True, "value": {"thread_id": "t-427", "body": f"head: {SHA_A}"}}
    mid_v1 = {"key": "msg/mid", "version": 1, "from": "bxxxxx", "recorded_at": iso(10), "signature_verified": True, "value": {"in_reply_to": "msg/gp", "body": "ack"}}
    mid_v2 = {"key": "msg/mid", "version": 2, "from": "bxxxxx", "recorded_at": iso(5), "signature_verified": True, "value": {"thread_id": "other-thread", "body": "moved"}}
    mock_thread([], hits=[], extra={"coverage": {"projected": 100, "total": 103}})
    monkeypatch.setattr(node, "read", lambda _sf, _st, *, key, **kw: gp_v1 if key == "msg/gp" else ((mid_v1 if kw.get("version") == 1 else mid_v2) if key == "msg/mid" else ((_ for _ in ()).throw(node.ReadFailed({"error": node.E_READ_REFUSED, "error_code": "E_UNKNOWN_KEY"})) if key == "msg/missing" else unproj_claim)))
    monkeypatch.setattr(node, "invoke", lambda *a, **kw: {"status": "completed", "result_signature_verified": True, "result": {"results": [{"key": "msg/unproj", "version": 1, "created_at": iso(1), "value": {"in_reply_to": "msg/mid", "body": "I will merge #427 now"}}, {"key": "msg/mid", "version": 2, "created_at": iso(5), "value": {"thread_id": "other-thread", "body": "moved"}}], "next_cursor": None}})
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "claimed_by_other_seat"
    monkeypatch.setattr(node, "invoke", lambda *a, **kw: {"status": "completed", "result_signature_verified": True, "result": {"results": [{"key": "msg/other", "version": 1, "created_at": iso(1), "value": {"thread_id": "other-thread", "body": "I will merge #427 now"}}, {"key": "msg/dangling", "version": 1, "created_at": iso(1), "value": {"in_reply_to": "msg/missing", "body": "unrelated"}}], "next_cursor": None}})
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["ok"]

    mock_thread([{"key": "msg/1", "from": "bxxxxx", "recorded_at": iso(5), "signature_verified": False, "body": "ok"}])
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "signature_unverified"
    monkeypatch.setattr(node, "read", lambda *a, **kw: (_ for _ in ()).throw(node.ReadFailed({"error": node.E_READ_REFUSED, "error_code": "E_UNKNOWN_KEY"})))
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "signature_unverified"

    for body, exp_ok, exp_code in [
        (f"#427 is merged as {SHA_A}", False, "already_merged"), ("Merged #428 and #427.", False, "already_merged"),
        ("#427 has just been merged.", False, "already_merged"), ("#427 is merged without conflicts.", False, "already_merged"),
        ("Without conflicts #427 is merged.", False, "already_merged"), ("#427 is merged, if you need the result ask me.", False, "already_merged"),
        ("http://192.168.50.20:6310/owner/repo/pull/427 is already merged", False, "already_merged"), ("#426 is not merged, #427 is merged", False, "already_merged"),
        ("Merged #428.", True, "ok"), ('Example: "txxxx will merge #427 now." and `txxxx will merge #427 now`', True, "ok"),
        ("Example:\n~~~\ntxxxx will merge #427 now\n~~~\n    txxxx will merge #427 now", True, "ok"),
        ("PXX will merge #427 now", True, "ok"), ("@pxx will merge #427 now", True, "ok"), ("pxx: merging #427 now", True, "ok"),
        (f"#427 head: {SHA_A}; merger: pxx; #428 head: {SHA_B}; merger: txxxx", True, "ok"), (f"#427 base head: {SHA_B}\nCurrent commit: {SHA_A}", True, "ok"),
        (f"Please re-review #427 at head {SHA_A}. The unit test is red on {SHA_B[:8]}.\nmerger: pxx", True, "ok"),
        (f"Please review and merge #427\nmerger: pxx\nhead: {SHA_A}", True, "ok"),
        ("merging #427 now", False, "claimed_by_other_seat"), ("I will merge #427 now without delay.", False, "claimed_by_other_seat"),
        ("I will merge #427 now with no errors reported.", False, "claimed_by_other_seat"), ("I'm merging #427 now with no conflicts.", False, "claimed_by_other_seat"),
        ("With no conflicts I'm merging #427 now.", False, "claimed_by_other_seat"), ("I will merge #428 and #427 now.", False, "claimed_by_other_seat"),
        ("#427: I am merging it now.", False, "claimed_by_other_seat"), ("I have claimed the merge for #427.", False, "claimed_by_other_seat"),
        ("pxxphyte will merge #427 now", False, "claimed_by_other_seat"), ("codex-pxx will merge #427 now", False, "claimed_by_other_seat"),
        ("pxx is not merging #427, txxxx will merge #427 now", False, "claimed_by_other_seat"), ("pxx is not merging #427 but txxxx will merge #427 now", False, "claimed_by_other_seat"),
        ("I'm merging https://github.com/owner/repo/pull/427 now", False, "claimed_by_other_seat"), (f"I will merge commit {SHA_A} for #427 now.", False, "claimed_by_other_seat"),
        ("#427 merge checks passed.", True, "ok"), (f"Merge commit {SHA_A} into main for #427.", False, "check_by_hand"),
        ("I'm merging #427 now, any objections?", False, "check_by_hand"), ("Has #427 been merged?", False, "check_by_hand"), ("May I merge #427?", False, "check_by_hand"),
        ("After #427 is merged, delete the branch.", False, "check_by_hand"), ("Not yet merged #427; waiting for CI.", False, "check_by_hand"),
        ("Merging #427 is not safe yet.", False, "check_by_hand"), ("If CI passes, txxxx will merge #427.", False, "check_by_hand"),
        ("txxxx will merge #427 if CI passes.", False, "check_by_hand"), ("I will review #427 and report merge readiness.", False, "check_by_hand"),
        ("Please review #427's merge handling.", False, "check_by_hand"), ("#427: handling the squash button shortly.", False, "check_by_hand"),
        (f"pxx, as merger: please do not merge #427 before set C completes.\nmerger: pxx\nhead: {SHA_A}", False, "check_by_hand"),
        (f"#427 head: https://github.com/acme/repo/commit/{SHA_B}", False, "stale_head"), (f"#427 head: `{SHA_B}`", False, "stale_head"),
        (f"#427 head: {SHA_A}\nUpdated commit: {SHA_B}", False, "ambiguous_head"), (f"PR #427\nmerger: txxxx\nmerger: pxx\nhead: {SHA_A}", False, "ambiguous_merger"),
    ]:
        mock_thread([{"key": "msg/c", "from": "txxxx", "recorded_at": iso(2), "signature_verified": True, "body": body}])
        r = preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)
        assert r["ok"] is exp_ok and r["code"] == exp_code, (body, r)
        r_cbh = preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s, checked_by_hand=["msg/c"])
        assert (r_cbh["ok"] and r_cbh["checked_by_hand"] == ["msg/c"]) if exp_code == "check_by_hand" else (r_cbh["ok"] is exp_ok and r_cbh["code"] == exp_code), (body, r_cbh)

    for follow_up, clears_claim in [
        ("I cancelled my merge claim for #427; pxx is the sole merger.", True), ("I won't merge #427; pxx is the sole merger.", True),
        ("I am not cancelling my merge claim for #427.", False), ("I won't retract my merge claim for #427.", False),
        ("I didn't merge #427 yet, waiting on CI.", False), ("I am not yet merging #427, checking SHA.", False),
        ("I dropped the stale worktree before merging #427.", False), ("If CI fails, I will cancel my merge claim for #427.", False),
    ]:
        mock_thread([{"key": "msg/claim", "from": "txxxx", "recorded_at": iso(4), "signature_verified": True, "body": "I will merge #427 now"},
                     {"key": "msg/fup", "from": "txxxx", "recorded_at": iso(2), "signature_verified": True, "body": follow_up}])
        res_raw = preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)
        res_cbh = preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s, checked_by_hand=["msg/fup"])
        assert res_raw["code"] == ("check_by_hand" if clears_claim else "claimed_by_other_seat"), (follow_up, res_raw)
        assert res_cbh["ok"] is clears_claim and (clears_claim or res_cbh["code"] == "claimed_by_other_seat"), (follow_up, res_cbh)

    mock_thread([{"key": "msg/h1", "from": "bxxxxx", "recorded_at": iso(5), "signature_verified": True, "body": f"head: {SHA_A}"},
                 {"key": "msg/h2", "from": "bxxxxx", "recorded_at": iso(1), "signature_verified": True, "body": f"PR #427 new head: {SHA_B[:8]}"}])
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "ambiguous_head"
    for rev in (False, True):
        ties = [{"key": "msg/t1", "from": "bxxxxx", "recorded_at": iso(2), "signature_verified": True, "body": f"head: {SHA_A}"},
                {"key": "msg/t2", "from": "bxxxxx", "recorded_at": iso(2), "signature_verified": True, "body": f"head: {SHA_B}"}]
        mock_thread(ties[::-1] if rev else ties)
        assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "ambiguous_head"

    mock_thread([{"key": "msg/ed", "from": "txxxx", "recorded_at": iso(1), "edited_at": iso(1), "signature_verified": True, "body": "I will merge #427 now"}], hits=[{"key": "msg/ed", "recorded_at": iso(60)}])
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "claimed_by_other_seat"
    mock_thread([{"key": "msg/7b", "from": "bxxxxx", "recorded_at": iso(1), "signature_verified": True, "body": f"PR #427 updated: old head: {SHA_A}; new head: {SHA_B}"}])
    assert preflight.check_merge_preflight(sf, st, pr=427, head=SHA_A, thread_id="t-427", now=now_s)["code"] == "stale_head" and preflight.check_merge_preflight(sf, st, pr=427, head=SHA_B, thread_id="t-427", now=now_s)["ok"]

    r_iso = lambda age_m: datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - age_m * 60.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    mock_thread([{"key": "msg/old", "from": "txxxx", "recorded_at": r_iso(45), "signature_verified": False, "body": "merging #427"},
                 {"key": "msg/rev", "from": "txxxx", "recorded_at": r_iso(6), "signature_verified": True, "body": "Please review #427's merge handling."},
                 {"key": "msg/8", "from": "bxxxxx", "recorded_at": r_iso(5), "signature_verified": True, "body": f"please squash #427\nmerger: pxx\nhead: {SHA_B}"}])
    assert preflight.main(["merge", "--base-url", BASE_URL, "--state-file", str(sf), "--pr", "427", "--head", SHA_B, "--thread", "t-427"]) == 1 and "check_by_hand" in capsys.readouterr().out
    assert preflight.main(["merge", "--base-url", BASE_URL, "--state-file", str(sf), "--pr", "427", "--head", SHA_B, "--thread", "t-427", "--checked-by-hand", "msg/rev"]) == 0 and "checked_by_hand=msg/rev" in capsys.readouterr().out


def test_preflight_review_and_orient_integration(monkeypatch, tmp_path, capsys):
    sf, st = _write_state(tmp_path, label="pxx")
    monkeypatch.setattr(node, "load_state", lambda path, url: st)
    norm = preflight.normalize_review_paths(["tools/meshy_node/sendguard.py", "tools/ci/local_gate.py", "tools/meshy_node/cli.py", "tools/meshy_node/__init__.py"])
    assert {"sendguard.py", "local_gate.py", "meshy_node/cli.py", "meshy_node"} <= set(norm["tokens"]) and not ({"__init__.py", "tools", "cli.py"} & set(norm["tokens"]))
    seen_calls: list[dict] = []

    def fake_search(_sf, _st, *, q, k=10, rerank=False, ns=None, card_filter=(), **kw):
        seen_calls.append({"q": q, "ns": ns, "card_filter": list(card_filter)})
        if ns == "shared/decisions":
            if ("type", "Antibody") in card_filter and len(seen_calls) > 2:
                raise node.ServerSaid(422, {"error": "E_VALIDATION"})
            return {"namespace": "shared/decisions", "filter": {"types": [v for k_n, v in card_filter if k_n == "type"], "status": "active", "thread": None, "seat": None, "owner": None},
                    "hits": [{"key": "card/kxxx/0123456789abcdef0123456789abcdef", "namespace": "shared/decisions", "type": "Constraint", "status": "active", "seat": "kxxx", "text": "Antibody: sendguard.py MUTANTS require exact substring preservation."},
                             {"key": "card/kxxx/0123456789abcdef0123456789abcdef", "namespace": "shared/decisions", "type": "Constraint", "status": "active", "seat": "kxxx", "text": "Duplicate hit for sendguard.py"},
                             {"key": "card/pxx/fedcba9876543210fedcba9876543210", "namespace": "shared/decisions", "type": "Ruling", "status": "active", "seat": "pxx", "text": "Unrelated ruling about billing invoices."}]}
        return {"hits": [{"key": "msg/ff-1", "author": "dxxxxx", "body": "VERDICT: FIX FIRST; local_gate.py dropped exit code 125 on retry."},
                         {"key": "msg/unrel-2", "author": "dxxxxx", "body": "VERDICT: FIX FIRST; billing_sync.py timeout."}]}

    monkeypatch.setattr(node, "search", fake_search)
    assert preflight.main(["review", "--base-url", BASE_URL, "--state-file", str(sf), "--path", "tools/meshy_node/sendguard.py", "--path", "tools/ci/local_gate.py"]) == 0
    assert "REVIEW PREFLIGHT: 1 matching Antibody/Constraint/Ruling/OpenItem card(s), 1 FIX FIRST note(s)" in capsys.readouterr().out and ("type", "Antibody") in seen_calls[0]["card_filter"]

    repo = tmp_path / "wt"; repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    git("init", "-b", "main"); git("config", "user.email", "test@example.invalid"); git("config", "user.name", "Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8"); git("add", "README.md"); git("commit", "-m", "base")
    git("update-ref", "refs/remotes/origin/main", "HEAD"); git("checkout", "-b", "feat/guard")
    (repo / "tools" / "meshy_node").mkdir(parents=True); (repo / "tools" / "ci").mkdir(parents=True)
    (repo / "tools" / "meshy_node" / "sendguard.py").write_text("# edit\n", encoding="utf-8"); (repo / "tools" / "ci" / "local_gate.py").write_text("# edit\n", encoding="utf-8")
    git("add", "tools/meshy_node/sendguard.py", "tools/ci/local_gate.py"); git("commit", "-m", "touch sendguard and local_gate")

    assert preflight.main(["review", "--base-url", BASE_URL, "--state-file", str(sf), "--repo", str(repo), "--json"]) == 0 and json.loads(capsys.readouterr().out)["match_count"] == 2
    assert preflight.main(["review", "--base-url", BASE_URL, "--state-file", str(sf), "--repo", str(repo), "--base", "refs/heads/does-not-exist", "--json"]) == 1
    bad_diff, bad_wt = json.loads(capsys.readouterr().out), orient._inspect_actionable_review_preflight(sf, BASE_URL, [{"path": str(tmp_path / "missing-wt"), "ahead_of_trunk": 1}])
    assert bad_diff["ok"] is False and bad_diff["available"] is False and "OSError" in bad_diff["error"] and bad_wt is not None and bad_wt["available"] is False and "OSError" in bad_wt["error"]

    monkeypatch.setattr(orient, "inspect_mesh_graph", lambda *a, **kw: {"available": True, "mode": "live", "seat": "pxx"})
    assert "review_preflight" not in orient.run_orient(state_file=sf, base_url=BASE_URL, spool_dir=tmp_path / "s", handover_dir=tmp_path / "h", repos=[repo], probe_brain_identity=False)
    rep_with = orient.run_orient(state_file=sf, base_url=BASE_URL, spool_dir=tmp_path / "s", handover_dir=tmp_path / "h", repos=[repo], probe_brain_identity=False, include_review_preflight=True)
    assert rep_with["review_preflight"]["decision_cards"][0]["key"] == "card/kxxx/0123456789abcdef0123456789abcdef" and rep_with["review_preflight"]["fix_first_messages"][0]["key"] == "msg/ff-1"
    assert "REVIEW PREFLIGHT: 1 card(s), 1 FIX FIRST note(s)" in orient.format_orient_text(rep_with)
    assert cli.main(["orient", "--base-url", BASE_URL, "--state-file", str(sf), "--spool-dir", str(tmp_path / "s"), "--handover-dir", str(tmp_path / "h"), "--repo", str(repo), "--no-brain-probe", "--include-review-preflight"]) == 0
    assert "card/kxxx/0123456789abcdef0123456789abcdef" in capsys.readouterr().out

    monkeypatch.setattr(node, "search", lambda *a, **kw: {"hits": [{"key": "msg/leak", "body": "sendguard.py mail hit"}]})
    assert preflight.main(["review", "--base-url", BASE_URL, "--state-file", str(sf), "--path", "tools/meshy_node/sendguard.py", "--json"]) == 1 and json.loads(capsys.readouterr().out)["available"] is False
    monkeypatch.setattr(node, "search", lambda *a, **kw: (_ for _ in ()).throw(node.ServerSaid(503, {"error": "down"})))
    rep_err = orient.run_orient(state_file=sf, base_url=BASE_URL, spool_dir=tmp_path / "s", handover_dir=tmp_path / "h", repos=[repo], probe_brain_identity=False, include_review_preflight=True)
    assert rep_err["review_preflight"]["available"] is False and "ServerSaid" in rep_err["review_preflight"]["error"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
