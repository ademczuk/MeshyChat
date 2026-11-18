"""#374, GitHub Codex's two P2s on bbf7dfe7 (pxx b981c4, kxxx 3064af). Both code paths came in with b62f5c78.

1. The store reset: when a previously ingested Codex rollout was missing from today's selection, the run wiped the
   whole store, even when the file had only been deleted. Its cards could not be rebuilt, so they were lost (Codex's
   repro: 2 cards fall to 1). Now the store resets only when a missing file is still on disk (the selection
   changed); a file gone from disk keeps its cards and is named in files_vanished. The Kimi and Antigravity branches
   had the same shape when an exclusion was configured.
2. The Context card: every mapped Codex session and child was added to its sessions with no bound, so with many
   sessions the card went over the server's 8,192-byte limit and was refused (Codex: 221 sessions gave 9,894 bytes).
   Now the list is newest first, cut to the five-session bound, and a card is never emitted over the limit.
Needs no database. Every fixture is synthetic."""
import json
import os
import sys

import pytest

LOADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards")
sys.path.insert(0, LOADER_DIR)
import context_snapshot  # noqa: E402
import transcript_cards as tc  # noqa: E402

CWD = "C:/Projects/Seat-A"


def _rollout(home, day, sid, text, mtime=None, source="cli"):
    folder = os.path.join(home, "2026", "10", day)
    os.makedirs(folder, exist_ok=True)
    recs = [
        {"timestamp": "2026-10-%sT05:00:00.000Z" % day, "type": "session_meta",
         "payload": {"id": sid, "source": source, "originator": "codex-tui", "cli_version": "0.161.0", "cwd": CWD}},
        {"timestamp": "2026-10-%sT05:01:00.000Z" % day, "type": "response_item",
         "payload": {"type": "message", "role": "user", "id": "msg_" + sid,
                     "content": [{"type": "input_text", "text": text}]}},
        {"timestamp": "2026-10-%sT05:01:00.000Z" % day, "type": "event_msg",
         "payload": {"type": "user_message", "message": text}},
    ]
    path = os.path.join(folder, "rollout-2026-10-%sT05-00-00-%s.jsonl" % (day, sid))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(json.dumps(r) + "\n" for r in recs))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _cards(out):
    with open(os.path.join(out, "cards.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _run(out, home, **kw):
    return tc.run(None, "codex-seat-a", out, codex_dir=home, codex_cwds=[CWD], **kw)


def test_a_deleted_codex_rollout_keeps_its_cards(tmp_path):
    """Codex's repro: two rollouts give 2 cards; delete one and run again. It must stay at 2."""
    home, out = str(tmp_path / "home"), str(tmp_path / "out")
    _rollout(home, "10", "aaa1", "please keep the nightly report")
    gone = _rollout(home, "11", "aaa2", "use the staging bucket for this")
    _run(out, home)
    before = len([c for c in _cards(out) if c["type"] == "Candidate"])
    assert before == 2
    os.remove(gone)
    rep = _run(out, home)
    assert len([c for c in _cards(out) if c["type"] == "Candidate"]) == before
    assert os.path.basename(gone) in rep.get("files_vanished", [])


def test_a_deselected_codex_rollout_still_resets_the_store(tmp_path):
    """The control: a file still on disk but excluded is a selection change, and its cards go."""
    home, out = str(tmp_path / "home"), str(tmp_path / "out")
    _rollout(home, "10", "aaa1", "please keep the nightly report")
    kept_out = _rollout(home, "11", "aaa2", "use the staging bucket for this")
    _run(out, home)
    assert len([c for c in _cards(out) if c["type"] == "Candidate"]) == 2
    rep = _run(out, home, exclude_files=[os.path.basename(kept_out)])
    texts = [c["text"] for c in _cards(out) if c["type"] == "Candidate"]
    assert len(texts) == 1 and not any("staging bucket" in t for t in texts)
    assert os.path.basename(kept_out) not in rep.get("files_vanished", [])


def _missing(tmp_path, delete):
    base = tmp_path / "home"
    a = base / "s-a" / "wire.jsonl"
    b = base / "s-b" / "wire.jsonl"
    for p in (a, b):
        p.parent.mkdir(parents=True)
        p.write_text("{}\n", encoding="utf-8")
    if delete:
        b.unlink()
    vanished = []
    changed = tc._missing_still_on_disk({str(a), str(b)}, [str(a)], str(base), vanished)
    return changed, vanished


def test_kimi_and_antigravity_keep_cards_for_a_file_gone_from_disk(tmp_path):
    """The Kimi and Antigravity branches share this rule: gone from disk is vanished, still on disk is a change."""
    changed, vanished = _missing(tmp_path, delete=True)
    assert changed is False and vanished == [os.path.join("s-b", "wire.jsonl")]


def test_kimi_and_antigravity_still_reset_for_a_deselected_file(tmp_path):
    changed, vanished = _missing(tmp_path, delete=False)
    assert changed is True and vanished == []


def test_the_context_card_names_at_most_five_sessions_newest_first_and_fits_the_server_limit(tmp_path):
    home, out = str(tmp_path / "home"), str(tmp_path / "out")
    n = 221
    for i in range(n):
        _rollout(home, "%02d" % (10 + i % 15), "s%04d-%s" % (i, "x" * 24),
                 "keep decision %d for the nightly report" % i, mtime=1_790_000_000 + i)
    rep = _run(out, home, emit_context=True)
    assert rep["files"] == n
    ctx = [c for c in _cards(out) if c["type"] == "Context"]
    assert len(ctx) == 1
    sessions = ctx[0]["source"]["sessions"]
    assert 1 <= len(sessions) <= context_snapshot.MAX_CONTEXT_SESSIONS
    newest = ["s%04d-%s" % (i, "x" * 24) for i in range(n - 1, n - 1 - len(sessions), -1)]
    assert sessions == newest
    size = len(json.dumps(ctx[0], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size <= context_snapshot.MAX_CARD_BYTES == 8_192


def test_the_server_accepts_that_context_card(tmp_path):
    """The server's own validation, where Django is importable (the HQB gate); skipped without it."""
    validation = pytest.importorskip("apps.cards.validation")
    assert validation.MAX_VALUE_BYTES == context_snapshot.MAX_CARD_BYTES
    home, out = str(tmp_path / "home"), str(tmp_path / "out")
    for i in range(221):
        _rollout(home, "%02d" % (10 + i % 15), "s%04d-%s" % (i, "x" * 24), "keep decision %d" % i,
                 mtime=1_790_000_000 + i)
    _run(out, home, emit_context=True)
    ctx = [c for c in _cards(out) if c["type"] == "Context"][0]
    size = len(json.dumps(ctx, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size <= validation.MAX_VALUE_BYTES


def test_build_context_card_never_returns_a_card_over_the_limit():
    """The size guard itself: sessions are cut first, and a card that still cannot fit is not emitted."""
    snapshot = {"refs": {"branch": "main", "prs": [], "threads": [], "files": []}, "task": "ship the fix",
                "latest_time": "2026-10-23T10:00:00Z", "sessions": ["s-%03d-%s" % (i, "y" * 1500) for i in range(9)],
                "source_files": []}
    card, _, err = context_snapshot.build_context_card(
        "codex-seat-a", "codex", snapshot, loader_version="t", detect_fn=lambda *a: set())
    assert err is None and card is not None
    assert len(card["source"]["sessions"]) <= context_snapshot.MAX_CONTEXT_SESSIONS
    assert context_snapshot._card_bytes(card) <= context_snapshot.MAX_CARD_BYTES
    snapshot["source_files"] = ["f" * 9000]
    card, _, err = context_snapshot.build_context_card(
        "codex-seat-a", "codex", snapshot, loader_version="t", detect_fn=lambda *a: set())
    assert card is None and err.startswith("oversize:")


def _kimi_session(home, sid, text):
    d = os.path.join(home, "wd_x", "session_" + sid, "agents", "main")
    os.makedirs(d, exist_ok=True)
    recs = [{"type": "metadata", "protocol_version": "1.5", "created_at": 1787064215000},
            {"type": "context.append_message", "time": 1787064216000,
             "message": {"role": "user", "id": "m-" + sid, "origin": {"kind": "user"},
                         "content": [{"type": "text", "text": text}]}}]
    path = os.path.join(d, "wire.jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(json.dumps(r) + "\n" for r in recs))
    return path


def test_a_deleted_kimi_wire_keeps_its_cards_when_an_exclusion_is_set(tmp_path):
    """End to end through the Kimi branch, which only reset when an exclusion was configured."""
    home, out = str(tmp_path / "khome"), str(tmp_path / "out")
    _kimi_session(home, "11111111-0000-0000-0000-000000000001", "Use the bridge for the reranker from now on.")
    gone = _kimi_session(home, "22222222-0000-0000-0000-000000000002", "Keep the nightly chain on main.")
    excl = [_kimi_session(home, "33333333-0000-0000-0000-000000000003", "an excluded session, both runs")]
    tc.run(None, "nxxxxx-prod", out, kimi_dir=home, exclude_files=excl)
    before = len([c for c in _cards(out) if c["type"] == "Candidate"])
    assert before == 2
    os.remove(gone)
    rep = tc.run(None, "nxxxxx-prod", out, kimi_dir=home, exclude_files=excl)
    assert len([c for c in _cards(out) if c["type"] == "Candidate"]) == before
    assert os.path.relpath(gone, home) in rep.get("files_vanished", [])


def test_a_parent_with_221_children_gets_a_card_the_server_accepts_and_the_spool_matches(tmp_path):
    """Codex's third P2 (pxx 20caa8): every child id went onto the parent's latest card with no size check, so 221
    children made a 9,608-byte Candidate card, spooled and then refused. Now at most MAX_CONTEXT_SESSIONS children are
    named, the parent's own session is never removed, and every written card fits MAX_CARD_BYTES."""
    home, out = str(tmp_path / "home"), str(tmp_path / "out")
    parent = "parent-" + "p" * 30
    _rollout(home, "10", parent, "please keep the nightly report")
    for i in range(221):
        _rollout(home, "11", "child-%04d-%s" % (i, "c" * 30), "subagent work %d" % i,
                 source={"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}})
    rep = _run(out, home, spool_pending=True)
    assert len(rep["codex_mapped_children"]) == 221
    cands = [c for c in _cards(out) if c["type"] == "Candidate"]
    assert len(cands) == 1
    card = cands[0]
    sessions = card["source"]["sessions"]
    assert parent in sessions
    assert len(sessions) <= 1 + context_snapshot.MAX_CONTEXT_SESSIONS
    assert context_snapshot._card_bytes(card) <= context_snapshot.MAX_CARD_BYTES
    assert not rep.get("oversize_cards")
    with open(os.path.join(out, "pending.jsonl"), encoding="utf-8") as f:
        spooled = [json.loads(line) for line in f if line.strip()]
    match = [s for s in spooled if (s.get("value") or s).get("id") == card["id"]]
    assert match, "the parent's card is in the spool"
    spooled_card = match[0].get("value") or match[0]
    assert spooled_card["source"]["sessions"] == sessions
    try:
        validation = __import__("apps.cards.validation", fromlist=["validate_card"])
    except ImportError:
        return
    assert context_snapshot._card_bytes(card) <= validation.MAX_VALUE_BYTES


def test_the_size_guard_never_removes_a_protected_session():
    card = {"source": {"sessions": ["a-own"] + ["z-%03d-%s" % (i, "q" * 200) for i in range(60)]}, "text": "x"}
    assert context_snapshot.fit_card_sessions(card, protect={"a-own"})
    assert "a-own" in card["source"]["sessions"]
    assert context_snapshot._card_bytes(card) <= context_snapshot.MAX_CARD_BYTES
    big = {"source": {"sessions": ["keep"]}, "text": "y" * 9000}
    assert context_snapshot.fit_card_sessions(big, protect={"keep"}) is False
    assert big["source"]["sessions"] == ["keep"]
