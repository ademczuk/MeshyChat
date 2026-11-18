"""An unavailable source and an outstanding dependency must not look idle."""
from __future__ import annotations

import pytest

from tools.meshy_node import node, orient


def test_graph_outage_is_a_candidate_and_never_an_empty_queue():
    report = {"mesh_graph": {"available": False, "mode": "error", "error": "brief: timed out"}}
    report["candidate_options"] = orient.synthesize_candidates(report)
    assert any(c["source"] == "mesh_graph.incomplete" for c in report["candidate_options"])
    assert "No pending items detected" not in orient.format_orient_text(report)


def test_partial_graph_keeps_successful_asks_and_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(node, "load_state", lambda *a: {"label": "test-seat"})
    monkeypatch.setattr(node, "brief", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("brief unavailable")))
    monkeypatch.setattr(node, "owed", lambda *a, **k: {"items": []})
    monkeypatch.setattr(node, "awaiting", lambda *a, **k: {"awaiting": []})
    graph = orient.inspect_mesh_graph(tmp_path / "state.json", "http://localhost:6310")
    assert graph["available"] is True
    assert graph["mode"] == "partial"
    assert "brief unavailable" in graph["error"]
    report = {"mesh_graph": graph}
    report["candidate_options"] = orient.synthesize_candidates(report)
    assert any(c["source"] == "mesh_graph.incomplete" for c in report["candidate_options"])
    assert "brief unavailable" in orient.format_orient_text(report)


def test_awaiting_is_preserved_without_instructing_duplicate_messages():
    report = {
        "mesh_graph": {
            "available": True,
            "mode": "live",
            "error": None,
            "awaiting": [{"key": "msg/123-review", "owing_label": "reviewer", "thread_id": "pr-500", "state": "open"}],
        }
    }
    report["candidate_options"] = orient.synthesize_candidates(report)
    dependency = next(c for c in report["candidate_options"] if c["source"] == "mesh_graph.awaiting")
    assert dependency["keys"] == ["msg/123-review"]
    assert "Do not resend" in dependency["description"]
    rendered = orient.format_orient_text(report)
    assert "awaiting=1" in rendered
    assert "AWAITING: msg/123-review awaiting reviewer" in rendered
    assert "msg/123-review" in rendered
    assert "reviewer" in rendered


def test_clean_empty_report_does_not_claim_five_sources_were_checked():
    report = {"mesh_graph": {"available": True, "mode": "live", "error": None}, "candidate_options": []}
    assert "across the 5 sources" not in orient.format_orient_text(report)
    assert "does not establish" in orient.format_orient_text(report)


def test_fallback_error_preserves_known_ask(monkeypatch, tmp_path):
    monkeypatch.setattr(node, "load_state", lambda *a: {"label": "test-seat"})
    monkeypatch.setattr(
        node,
        "brief",
        lambda *a, **k: {
            "owed": {"items": [{"state": "open", "hit": {"key": "msg/123-known"}}]},
            "awaiting": {"items": []},
        },
    )
    monkeypatch.setattr(node, "owed", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("owed timeout")))
    monkeypatch.setattr(
        orient.cli,
        "_answered_still_open",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("fallback timeout")),
    )
    graph = orient.inspect_mesh_graph(tmp_path / "state.json", "http://localhost:6310")
    assert graph["reply_owed"][0]["key"] == "msg/123-known"
    assert graph["mode"] == "partial"
    assert "fallback timeout" in graph["error"]
    sources = {c["source"] for c in orient.synthesize_candidates({"mesh_graph": graph})}
    assert {"mesh_graph.reply_owed", "mesh_graph.incomplete"} <= sources


def test_swallowed_answered_still_open_unavailability_marks_partial(monkeypatch, tmp_path):
    """When `cli._answered_still_open` swallows a 5xx/timeout and returns `count=None`,
    `inspect_mesh_graph` still records `answered_still_open: unavailable` rather than silently
    reporting a complete live graph."""
    monkeypatch.setattr(node, "load_state", lambda *a: {"label": "test-seat"})
    monkeypatch.setattr(
        node,
        "brief",
        lambda *a, **k: {
            "owed": {"items": []},
            "awaiting": {"items": []},
        },
    )
    monkeypatch.setattr(node, "owed", lambda *a, **k: (_ for _ in ()).throw(OSError("conn reset")))
    graph = orient.inspect_mesh_graph(tmp_path / "state.json", "http://localhost:6310")
    assert graph["available"] is True
    assert graph["mode"] == "partial"
    assert "answered_still_open: unavailable" in graph["error"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
