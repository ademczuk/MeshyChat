"""Tests for `meshy_node card context` fleet status board and overlap detection (#297 PR 3)."""

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from tools.meshy_node import cards, cli, node


def _ctx_card(seat: str, cid: str, time_str: str, *, branch=None, prs=None, threads=None, files=None, task="Working"):
    return {
        "key": f"card/{seat}/{cid}",
        "schema": "card.v1",
        "id": cid,
        "type": "Context",
        "owner": seat,
        "seat": seat,
        "date": time_str[:10],
        "time": time_str,
        "status": "active",
        "text": (
            f"Context | owner: {seat} | seat: {seat} | {time_str[:16]}\n"
            f"Branch: {branch or 'none'} | PRs: {', '.join(str(x) for x in (prs or ())) or 'none'} | Threads: none\n"
            f"Files: {', '.join(files or ()) or 'none'}\n"
            f"Task: {task}"
        ),
        "refs": {
            "branch": branch,
            "prs": list(prs or ()),
            "threads": list(threads or ()),
            "files": list(files or ()),
        },
    }


def _com_card(seat: str, cid: str, time_str: str, thread_id: str, text: str, done_when: str):
    return {
        "key": f"card/{seat}/{cid}",
        "schema": "card.v1",
        "id": cid,
        "type": "Commitment",
        "owner": seat,
        "seat": seat,
        "date": time_str[:10],
        "time": time_str,
        "status": "active",
        "thread_id": thread_id,
        "done_when": done_when,
        "text": f"Commitment | owner: {seat} | seat: {seat} | {time_str[:16]}\n{text}",
    }


def test_fleet_context_deduplicates_seat_heads_and_detects_overlaps(monkeypatch, tmp_path):
    now = datetime(2026, 10, 14, 13, 10, tzinfo=timezone.utc)
    ctx_rows = [
        # Older head for antigravity (should be superseded/ignored in favor of newer timestamp)
        _ctx_card(
            "antigravity", "a" * 32, "2026-10-14T12:00:00.000Z",
            branch="antigravity/old-branch", prs=["#295"], files=["apps/old.py"], task="Old task",
        ),
        _ctx_card(
            "antigravity", "b" * 32, "2026-10-14T13:05:00.000Z",
            branch="antigravity/context-card-cli-297",
            prs=["#302", "#304"],
            threads=["meshychat-transcript-context-20261010"],
            files=["tools/meshy_node/cards.py", "apps/cards/validation.py"],
            task="Build card context CLI",
        ),
        _ctx_card(
            "pxx", "c" * 32, "2026-10-14T13:06:00.000Z",
            branch="pxx/review-302",
            prs=[302],
            threads=["meshychat-transcript-context-20261010"],
            files=["apps/cards/validation.py"],
            task="Review PR #302",
        ),
    ]
    com_rows = [
        _com_card(
            "nxxxxx", "d" * 32, "2026-10-14T13:00:00.000Z",
            "meshychat-transcript-context-20261010",
            "Review #299 RSS probe",
            "Verdict posted",
        )
    ]

    calls = []

    def fake_cards(state_file, state, query, **kwargs):
        calls.append(dict(query))
        qmap = dict(query)
        if qmap.get("type") == "Context":
            return {"cards": ctx_rows}
        if qmap.get("type") == "Commitment":
            return {"cards": com_rows}
        return {"cards": []}

    monkeypatch.setattr(node, "cards", fake_cards)

    data = cards.fleet_context(tmp_path / "state.json", {"base_url": "http://127.0.0.1:6310"}, now=now)
    seats = {e["seat"]: e for e in data["seats"]}
    assert sorted(seats) == ["antigravity", "nxxxxx", "pxx"]
    # Newest head selected for antigravity
    assert seats["antigravity"]["context_key"] == "card/antigravity/" + "b" * 32
    assert seats["antigravity"]["refs"]["branch"] == "antigravity/context-card-cli-297"

    # Overlaps across seats: PR #302 (a reviewer, pxx), file apps/cards/validation.py (a reviewer, pxx),
    # thread <thread> (a reviewer, nxxxxx, pxx)
    overlaps = {(o["kind"], o["value"]): o["seats"] for o in data["overlaps"]}
    assert overlaps[("pr", "#302")] == ["antigravity", "pxx"]
    assert overlaps[("file", "apps/cards/validation.py")] == ["antigravity", "pxx"]
    assert overlaps[("thread", "meshychat-transcript-context-20261010")] == ["antigravity", "nxxxxx", "pxx"]

    lines = cards.format_context_board(data)
    board_text = "\n".join(lines)
    assert cards.CONTEXT_FIRST_LINE in board_text
    assert "antigravity | 2026-10-14 13:05 (5 min ago)" in board_text
    assert "pr #302: antigravity, pxx" in board_text
    assert "file apps/cards/validation.py: antigravity, pxx" in board_text


def test_fleet_context_overlap_filter_and_seat_validation(monkeypatch, tmp_path):
    ctx_rows = [
        _ctx_card("antigravity", "b" * 32, "2026-10-14T13:05:00.000Z", prs=["#302"], files=["apps/cards/validation.py"]),
        _ctx_card("uxxx", "e" * 32, "2026-10-14T13:05:00.000Z", prs=["#296"], files=["apps/meshsem/views.py"]),
    ]
    monkeypatch.setattr(
        node, "cards",
        lambda sf, st, q, **kw: {"cards": ctx_rows if dict(q).get("type") == "Context" else []},
    )

    filtered = cards.fleet_context(tmp_path / "state.json", {}, overlap="302")
    assert [e["seat"] for e in filtered["seats"]] == ["antigravity"]
    assert filtered["overlap_filter"] == "302"

    # Invalid seat label is refused before any network call
    with pytest.raises(node.Refused, match="not a seat"):
        cards.fleet_context(tmp_path / "state.json", {}, seat="NOT A SEAT!")

    # Combining --seat with --overlap is refused before any network call
    with pytest.raises(node.Refused, match="do not combine"):
        cards.fleet_context(tmp_path / "state.json", {}, seat="antigravity", overlap="302")

    # Non-list refs fields (e.g. "prs": "123") are ignored rather than iterating characters
    bad_refs_card = _ctx_card("antigravity", "f" * 32, "2026-10-14T13:05:00.000Z")
    bad_refs_card["refs"] = {"branch": "main", "prs": "123", "files": "apps/x.py", "threads": "t1"}
    monkeypatch.setattr(
        node, "cards",
        lambda sf, st, q, **kw: {"cards": [bad_refs_card] if dict(q).get("type") == "Context" else []},
    )
    res = cards.fleet_context(tmp_path / "state.json", {})
    assert res["seats"][0]["refs"] == {"branch": "main", "prs": [], "threads": [], "files": []}

    # Malformed /v1/cards response without a list of cards raises node.Refused
    monkeypatch.setattr(node, "cards", lambda sf, st, q, **kw: {"cards": "not-a-list"})
    with pytest.raises(node.Refused, match="without a list"):
        cards.fleet_context(tmp_path / "state.json", {})


def test_cli_card_context_text_and_json_modes(monkeypatch, tmp_path, capsys):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"schema": "meshy-node-state/1", "base_url": "http://127.0.0.1:6310", "peer_id": "p1", "label": "antigravity"}),
        encoding="utf-8",
    )
    ctx_rows = [
        _ctx_card(
            "antigravity", "b" * 32, "2026-10-14T13:05:00.000Z",
            branch="antigravity/context-card-cli-297",
            prs=["#302"],
            files=["tools/meshy_node/cards.py"],
            task="Task with <script> & \u202e bidi",
        )
    ]
    monkeypatch.setattr(
        node, "cards",
        lambda sf, st, q, **kw: {"cards": ctx_rows if dict(q).get("type") == "Context" else []},
    )

    # 1. Plain-text board escapes markup and bidi controls via cards.short
    rc = cli.main(["card", "context", "--base-url", "http://127.0.0.1:6310", "--state-file", str(state_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "<script>" not in out
    assert "[U+202E]" in out
    assert "antigravity/context-card-cli-297" in out

    # 2. --json mode outputs valid JSON object
    rc_json = cli.main([
        "card", "context", "--base-url", "http://127.0.0.1:6310", "--state-file", str(state_file), "--json"
    ])
    assert rc_json == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["seats"][0]["seat"] == "antigravity"
    assert parsed["seats"][0]["refs"]["prs"] == ["#302"]

    # 3. --seat combined with --overlap exits 2
    rc_combo = cli.main([
        "card", "context", "--base-url", "http://127.0.0.1:6310", "--state-file", str(state_file),
        "--seat", "antigravity", "--overlap", "302",
    ])
    assert rc_combo == 2


def test_fleet_context_excludes_completed_commitments_and_closing_decisions(monkeypatch, tmp_path):
    """Issue #327 P2-1: completing a Commitment writes a Decision that supersedes it, and GET /v1/cards?type=Commitment
    returns that Decision with replaces_requested=True. Neither a closing Decision nor a superseded Commitment may
    appear as an open commitment or contribute a false overlap."""
    open_com = _com_card(
        "nxxxxx", "1" * 32, "2026-10-15T10:00:00.000Z",
        "open-thread-20261015", "Audit active item", "Report posted",
    )
    cross_seat_ruling = {
        "schema": "card.v1",
        "id": "9" * 32,
        "key": "card/dxxxxx/" + "9" * 32,
        "type": "Ruling",
        "owner": "operator",
        "seat": "dxxxxx",
        "date": "2026-10-15",
        "time": "2026-10-15T10:02:00.000Z",
        "status": "active",
        "text": "Ruling | owner: operator | seat: dxxxxx | 2026-10-15 10:02",
        "supersedes": [open_com["key"]],
        "replaces_requested": True,
    }
    closed_com = _com_card(
        "nxxxxx", "2" * 32, "2026-10-15T09:00:00.000Z",
        "closed-thread-20261015", "Finished work", "PR merged",
    )
    closing_dec = {
        **cards.decision("nxxxxx", closed_com["key"], closed_com, "Merged in #325", at="2026-10-15T09:30:00.000Z"),
        "key": "card/nxxxxx/" + cards.done_id(closed_com["key"]),
        "replaces_requested": True,
    }
    superseded_by_field_com = {
        **_com_card(
            "pxx", "3" * 32, "2026-10-15T08:00:00.000Z",
            "closed-thread-20261015", "Superseded commitment", "Done",
        ),
        "superseded_by": ["card/pxx/" + "4" * 32],
        "superseded_by_count": 1,
    }
    ctx_antigravity = _ctx_card(
        "antigravity", "a" * 32, "2026-10-15T10:05:00.000Z",
        threads=["closed-thread-20261015"], task="Check closed thread",
    )

    monkeypatch.setattr(
        node, "cards",
        lambda sf, st, q, **kw: {
            "cards": [ctx_antigravity] if dict(q).get("type") == "Context"
            else [closing_dec, cross_seat_ruling, closed_com, superseded_by_field_com, open_com]
        },
    )

    data = cards.fleet_context(tmp_path / "state.json", {})
    seats = {e["seat"]: e for e in data["seats"]}
    # pxx had only a superseded commitment and no Context card -> omitted
    assert sorted(seats) == ["antigravity", "nxxxxx"]
    assert [c["key"] for c in seats["nxxxxx"]["commitments"]] == [open_com["key"]]
    # <thread> is not an active commitment for nxxxxx or pxx, so there is no overlap with antigravity
    assert data["overlaps"] == []
    board = "\n".join(cards.format_context_board(data))
    assert "Audit active item" in board
    assert "Finished work" not in board
    assert "Done: Finished work" not in board
    assert "Superseded commitment" not in board
    assert "Overlaps across seats: none" in board


def test_fleet_context_follows_next_across_pages_and_reports_incomplete_when_capped(monkeypatch, tmp_path):
    """Issue #327 P2-2: with 401 cards across 3 pages (200 + 200 + 1) and an overlap only in the third page,
    fleet_context follows `next` up to CONTEXT_MAX_PAGES and names the overlap; when the page cap is hit, it
    prints 'overlaps: not checked past N cards' rather than 'none'."""
    page1 = [
        _ctx_card("nxxxxx", f"{i:032x}", f"2026-10-15T12:00:{i % 60:02d}.000Z", prs=[f"#{1000 + i}"])
        for i in range(1, 201)
    ]
    page2 = [
        _ctx_card("nxxxxx", f"{i:032x}", f"2026-10-15T11:00:{i % 60:02d}.000Z", prs=[f"#{1000 + i}"])
        for i in range(201, 401)
    ]
    # Card #401 on page 3 is pxx's active Context head referencing PR #327, overlapping antigravity
    page3 = [
        _ctx_card("pxx", "f" * 32, "2026-10-15T10:00:00.000Z", prs=["#327"], task="Review #327"),
    ]
    ag_head = _ctx_card("antigravity", "e" * 32, "2026-10-15T12:30:00.000Z", prs=["#327"], task="Fix #327")
    page1[0] = ag_head

    def paged_cards(sf, st, query, **kw):
        q = dict(query)
        if q.get("type") != "Context":
            return {"cards": [], "next": None}
        after = q.get("after")
        if after is None:
            return {"cards": page1, "next": "cursor-page-2"}
        if after == "cursor-page-2":
            return {"cards": page2, "next": "cursor-page-3"}
        if after == "cursor-page-3":
            return {"cards": page3, "next": None}
        raise AssertionError(f"unexpected cursor {after!r}")

    monkeypatch.setattr(node, "cards", paged_cards)

    # 1. Default CONTEXT_MAX_PAGES (5) follows `next` to page 3 (401 cards) and names the overlap on #327
    data = cards.fleet_context(tmp_path / "state.json", {})
    assert data["truncated"] is False
    assert data["cards_checked"] == 401
    assert data["overlaps"] == [{"kind": "pr", "value": "#327", "seats": ["antigravity", "pxx"]}]
    board = "\n".join(cards.format_context_board(data))
    assert "pr #327: antigravity, pxx" in board
    assert "Overlaps across seats: none" not in board

    # 2. When CONTEXT_MAX_PAGES cap (2 pages = 400 cards) is hit before page 3, output says not checked past 400 cards
    monkeypatch.setattr(cards, "CONTEXT_MAX_PAGES", 2)
    capped = cards.fleet_context(tmp_path / "state.json", {})
    assert capped["truncated"] is True
    assert capped["cards_checked"] == 400
    assert capped["overlaps"] == []
    capped_board = "\n".join(cards.format_context_board(capped))
    assert "overlaps: not checked past 400 cards" in capped_board
    assert "Overlaps across seats: none" not in capped_board

    # 3. Malformed empty-string cursor is refused rather than silently treated as end-of-pagination
    monkeypatch.setattr(node, "cards", lambda sf, st, q, **kw: {"cards": page1, "next": ""})
    with pytest.raises(node.Refused, match="invalid next cursor"):
        cards.fleet_context(tmp_path / "state.json", {})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
