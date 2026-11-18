"""Client half of audit finding 9a: `meshy_node thread --after K --envelope` asks the server for the rows after K and
their content, with a DPoP proof only when envelopes are asked for. Database-free: node._get is replaced.

Run:  python -m pytest tests/test_meshy_node_thread_paging.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node  # noqa: E402


def test_after_and_envelope_reach_the_query_and_only_envelope_proves(monkeypatch, tmp_path):
    """RED before the fix: the client had no --after or --envelope."""
    seen = []
    monkeypatch.setattr(node, "_get", lambda path, state, route, query, prove=False:
                        seen.append((route, dict(query), prove)) or {"hits": []})
    node.thread(tmp_path / "s.json", {}, thread_id="a/b:c", after="msg/1-x", envelope=True, limit=50)
    node.thread(tmp_path / "s.json", {}, thread_id="a/b:c")
    assert seen[0] == ("/v1/mesh/threads/a%2Fb%3Ac", {"limit": 50, "after": "msg/1-x", "envelope": 1}, True)
    assert seen[1] == ("/v1/mesh/threads/a%2Fb%3Ac", {}, False)


def test_the_cli_passes_the_flags(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(node, "thread", lambda path, state, **kw: calls.append(kw) or {"hits": []})
    monkeypatch.setattr(node, "load_state", lambda *a, **k: {}, raising=False)
    (tmp_path / "s.json").write_text("{}", encoding="utf-8")
    rc = cli.main(["thread", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "s.json"),
                   "--id", "t", "--after", "msg/9-y", "--envelope"])
    assert rc == 0 and calls == [{"thread_id": "t", "limit": None, "after": "msg/9-y", "envelope": True}]
