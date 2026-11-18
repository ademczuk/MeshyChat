"""Tests for opt-in `--with-context` enrichment on `meshy_node read` and `meshy_node context`
(tools/meshy_node/converse.py and tools/meshy_node/cli.py).

Run:  pytest tests/test_meshy_node_converse_context.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.meshy_node import cards, cli, converse, node, sendguard, transcript_graph


def _fake_state(tmp_path: Path) -> tuple[Path, dict]:
    sf = tmp_path / "state.json"
    st = {"base_url": "http://127.0.0.1:6310", "peer_id": "11111111-1111-1111-1111-111111111111", "label": "antigravity"}
    sf.write_text(json.dumps(st), encoding="utf-8")
    return sf, st


def _run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    parser = cli.build_parser()
    bound = cli._bind_values(parser, argv)
    args = parser.parse_args(bound)
    rc = cli.run(args)
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_plain_read_and_context_stay_unchanged_without_flag(tmp_path, monkeypatch, capsys):
    sf, st = _fake_state(tmp_path)
    monkeypatch.setattr(node, "load_state", lambda path, url: st)
    monkeypatch.setattr(sendguard, "record_inbound", lambda *a, **kw: None)

    enrichment_called = []
    monkeypatch.setattr(
        converse,
        "build_conversation_context",
        lambda *a, **kw: enrichment_called.append(True) or {},
    )
    monkeypatch.setattr(
        node,
        "read",
        lambda *a, **kw: {
            "key": "msg/1791640000000-kxxx-aaaaaa",
            "author": "kxxx",
            "signature_verified": True,
            "value": {"thread_id": "pillar4/main", "body": "hello world"},
        },
    )
    monkeypatch.setattr(
        node,
        "context",
        lambda *a, **kw: {
            "anchor": {"key": "msg/1791640000000-kxxx-aaaaaa", "thread_id": "pillar4/main", "body": "hello world"},
            "related": [],
        },
    )

    rc_r, doc_r = _run_cli(
        ["read", "--base-url", "http://127.0.0.1:6310", "--state-file", str(sf), "--key", "msg/1791640000000-kxxx-aaaaaa"],
        capsys,
    )
    assert rc_r == 0 and "conversation_context" not in doc_r

    rc_c, doc_c = _run_cli(
        ["context", "--base-url", "http://127.0.0.1:6310", "--state-file", str(sf), "--key", "msg/1791640000000-kxxx-aaaaaa"],
        capsys,
    )
    assert rc_c == 0 and "conversation_context" not in doc_c
    assert enrichment_called == []


def test_read_with_context_attaches_edges_cards_and_safe_transcript_pointers(tmp_path, monkeypatch, capsys):
    sf, st = _fake_state(tmp_path)
    monkeypatch.setattr(node, "load_state", lambda path, url: st)
    monkeypatch.setattr(sendguard, "record_inbound", lambda *a, **kw: None)
    monkeypatch.setattr(
        node,
        "read",
        lambda *a, **kw: {
            "key": "msg/1791640000000-kxxx-aaaaaa",
            "author": "kxxx",
            "signature_verified": True,
            "value": {"thread_id": "pillar4/main", "subject": "pr/500", "body": "check shard budget calibration"},
        },
    )
    monkeypatch.setattr(
        node,
        "neighbours",
        lambda *a, **kw: {
            "inferred": {
                "included": True,
                "related_to": [
                    {"key": "msg/same", "author": "kxxx", "thread_id": "pillar4/main", "scores": {"pair": 6.0}},
                    {
                        "key": "msg/1791630000000-uxxx-bbbbbb",
                        "author": "uxxx",
                        "thread_id": "ctx/budget",
                        "direction": "out",
                        "recorded_at": "2026-11-03T12:00:00+00:00",
                        "scores": {"pair": 4.81234},
                        "body": "SECRET-PEER-BODY-MUST-NOT-APPEAR",
                    },
                ],
            }
        },
    )
    monkeypatch.setattr(
        node,
        "cards",
        lambda *a, **kw: {
            "cards": [
                {
                    "key": "card/memory-bxxxxx/0123456789abcdef0123456789abcdef",
                    "type": "Ruling",
                    "seat": "memory-bxxxxx",
                    "owner": "operator",
                    "thread_id": "pillar4/main",
                    "text": "Ruling | owner: operator | seat: memory-bxxxxx | 2026-11-03T10:00\nCap resident ctxindex budget at 1 GiB on HQD",
                }
            ]
        },
    )
    monkeypatch.setattr(
        cards,
        "search",
        lambda *a, **kw: {
            "hits": [
                {
                    "key": "card/kxxx/fedcba9876543210fedcba9876543210",
                    "author": "kxxx",
                    "thread_id": "ctx/budget",
                    "rerank_score": 3.75,
                    "body": "Decision | owner: kxxx | seat: kxxx | 2026-11-03T11:00\nDrop same-thread and self-author pairs before pre-send reranking",
                }
            ]
        },
    )
    monkeypatch.setattr(
        transcript_graph,
        "search_transcripts",
        lambda *a, **kw: {
            "_untrusted_transcript_evidence": True,
            "banner": transcript_graph.UNTRUSTED_BANNER,
            "hits": [
                {
                    "seat": "uxxx",
                    "src": "0123456789abcdef0123456789abcdef",
                    "gen": 2,
                    "batch": 4,
                    "first_ord": 10,
                    "last_ord": 12,
                    "time_last": "2026-11-03T15:00:00+00:00",
                    "score": 0.0321,
                    "rerank_score": 5.125,
                    "text": "RAW-TRANSCRIPT-SECRET-TOKEN-DO-NOT-EMIT",
                }
            ],
        },
    )

    rc, doc = _run_cli(
        [
            "read",
            "--base-url",
            "http://127.0.0.1:6310",
            "--state-file",
            str(sf),
            "--key",
            "msg/1791640000000-kxxx-aaaaaa",
            "--with-context",
        ],
        capsys,
    )
    assert rc == 0
    cc = doc["conversation_context"]
    assert cc["untrusted"] is True and cc["trust_note"] == converse.TRUST_NOTE
    assert len(cc["related_edges"]) == 1
    assert cc["related_edges"][0]["key"] == "msg/1791630000000-uxxx-bbbbbb"
    assert cc["related_edges"][0]["pair_score"] == 4.8123
    assert "body" not in cc["related_edges"][0]

    assert set(converse.ENRICH_CARD_TYPES).issubset(set(cards.TYPES))
    assert [c["type"] for c in cc["active_cards"]] == ["Ruling", "Decision"]
    assert cc["active_cards"][0]["owner"] == "operator"
    assert cc["active_cards"][0]["summary"] == "Cap resident ctxindex budget at 1 GiB on HQD"
    assert cc["active_cards"][1]["owner"] == "kxxx"
    assert cc["active_cards"][1]["summary"] == "Drop same-thread and self-author pairs before pre-send reranking"
    assert len(cc["transcript_pointers"]) == 1
    ptr = cc["transcript_pointers"][0]
    assert ptr["seat"] == "uxxx" and ptr["first_ord"] == 10 and "transcript-read" in ptr["read_cmd"]
    assert "text" not in ptr
    assert cc["transcript_read_hint"] == transcript_graph.READ_HINT

    serialized = json.dumps(doc)
    assert "SECRET-PEER-BODY-MUST-NOT-APPEAR" not in serialized
    assert "RAW-TRANSCRIPT-SECRET-TOKEN-DO-NOT-EMIT" not in serialized
    assert "_untrusted_transcript_evidence" not in serialized
    # Sendguard backstop must accept the enriched JSON without tripping untrusted transcript markers.
    sendguard.refuse_untrusted_transcript_paste(serialized)


def test_context_with_context_reuses_related_edges_and_fails_open_on_subsource_error(tmp_path, monkeypatch, capsys):
    sf, st = _fake_state(tmp_path)
    monkeypatch.setattr(node, "load_state", lambda path, url: st)

    neighbours_calls = []
    monkeypatch.setattr(node, "neighbours", lambda *a, **kw: neighbours_calls.append(1))
    monkeypatch.setattr(
        node,
        "context",
        lambda *a, **kw: {
            "anchor": {"key": "msg/1791640000000-kxxx-aaaaaa", "thread_id": "pillar4/main", "body": "topic query"},
            "related_edges": {
                "hits": [
                    {
                        "key": "msg/1791630000000-uxxx-bbbbbb",
                        "author": "uxxx",
                        "thread_id": "other/thread",
                        "direction": "in",
                        "scores": {"pair": 3.5},
                        "body": "STRIPPED-CONTEXT-BODY",
                    }
                ]
            },
        },
    )

    def _boom(*a, **kw):
        raise RuntimeError("index offline")

    monkeypatch.setattr(node, "cards", _boom)
    monkeypatch.setattr(cards, "search", _boom)
    monkeypatch.setattr(transcript_graph, "search_transcripts", _boom)

    rc, doc = _run_cli(
        [
            "context",
            "--base-url",
            "http://127.0.0.1:6310",
            "--state-file",
            str(sf),
            "--key",
            "msg/1791640000000-kxxx-aaaaaa",
            "--with-context",
        ],
        capsys,
    )
    assert rc == 0
    assert neighbours_calls == [], "context --with-context should reuse related_edges from context response"
    cc = doc["conversation_context"]
    assert len(cc["related_edges"]) == 1
    assert "STRIPPED-CONTEXT-BODY" not in json.dumps(cc)
    assert "active_cards" in cc["errors"] and "transcript_pointers" in cc["errors"]


def test_read_with_context_skips_non_mesh_namespace(tmp_path, monkeypatch, capsys):
    sf, st = _fake_state(tmp_path)
    monkeypatch.setattr(node, "load_state", lambda path, url: st)
    monkeypatch.setattr(
        node,
        "read",
        lambda *a, **kw: {"key": "wiki/topic", "namespace": "shared/wiki", "value": {"title": "t"}},
    )
    rc, doc = _run_cli(
        [
            "read",
            "--base-url",
            "http://127.0.0.1:6310",
            "--state-file",
            str(sf),
            "--namespace",
            "shared/wiki",
            "--key",
            "wiki/topic",
            "--with-context",
        ],
        capsys,
    )
    assert rc == 0
    assert "skipped" in doc["conversation_context"]


def test_partial_card_errors_and_overall_wall_clock_deadline_surface_cleanly(tmp_path, monkeypatch):
    sf, st = _fake_state(tmp_path)
    primary = {"value": {"thread_id": "pillar4/main", "body": "shard prefilter"}}

    # 1. Partial error: node.cards raises while cards.search succeeds -> active_cards has hit AND errors['active_cards'] is partial.
    monkeypatch.setattr(node, "neighbours", lambda *a, **kw: {"inferred": {"included": True, "related_to": []}})
    monkeypatch.setattr(node, "cards", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("list failed")))
    monkeypatch.setattr(
        cards,
        "search",
        lambda *a, **kw: {
            "hits": [
                {
                    "key": "card/kxxx/0123456789abcdef0123456789abcdef",
                    "author": "kxxx",
                    "thread_id": "pillar4/main",
                    "body": "Decision | owner: kxxx | seat: kxxx | 2026-11-04T09:00\nKeep wake neighbours fast",
                }
            ]
        },
    )
    monkeypatch.setattr(transcript_graph, "search_transcripts", lambda *a, **kw: {"hits": []})

    cc_part = converse.build_conversation_context(sf, st, key="msg/1791640000000-kxxx-aaaaaa", primary=primary)
    assert len(cc_part["active_cards"]) == 1
    assert cc_part["errors"]["active_cards"] == "partial (RuntimeError)"

    # 2. Overall wall-clock deadline + in-call node.finishing_by propagation (GitHub Codex P2 4466910137):
    #    every source call runs inside node.finishing_by(deadline), mid-call DeadlinePassed fails open, and
    #    subsequent collectors skip once deadline is reached.
    seen_finish_by = []

    def _nb_check_finish(*_a, **_kw):
        seen_finish_by.append(("nb", node._FINISH_BY.get()))
        return {"inferred": {"included": True, "related_to": []}}

    def _cards_check_finish(*_a, **_kw):
        seen_finish_by.append(("cards", node._FINISH_BY.get()))
        return {
            "cards": [
                {
                    "key": "card/kxxx/0123456789abcdef0123456789abcdef",
                    "author": "kxxx",
                    "thread_id": "pillar4/main",
                    "body": "Decision | owner: kxxx | seat: kxxx | 2026-11-04T09:00\nBound HTTP timeout",
                }
            ]
        }

    def _search_timeout(*_a, **_kw):
        seen_finish_by.append(("search", node._FINISH_BY.get()))
        raise node.DeadlinePassed("cards.search exceeded deadline")

    monkeypatch.setattr(node, "neighbours", _nb_check_finish)
    monkeypatch.setattr(node, "cards", _cards_check_finish)
    monkeypatch.setattr(cards, "search", _search_timeout)
    monkeypatch.setattr(
        transcript_graph,
        "search_transcripts",
        lambda *_a, **_kw: (_ for _ in ()).throw(node.DeadlinePassed("tx exceeded deadline")),
    )

    cc_in_call = converse.build_conversation_context(
        sf, st, key="msg/1791640000000-kxxx-aaaaaa", primary=primary, deadline_s=10.0
    )
    assert len(seen_finish_by) == 3 and all(at is not None for _lbl, at in seen_finish_by)
    assert seen_finish_by[0][1] == seen_finish_by[1][1] == seen_finish_by[2][1]
    assert len(cc_in_call["active_cards"]) == 1
    assert cc_in_call["errors"]["active_cards"] == "partial (deadline_exceeded)"
    assert cc_in_call["errors"]["transcript_pointers"] == "unavailable (deadline_exceeded)"

    # 3. Pre-collector skip once clock advances past deadline:
    ticks = iter([1000.0, 1000.1, 1000.2, 1011.0, 1011.0, 1011.0])
    fake_clock = lambda: next(ticks, 1011.0)  # noqa: E731
    monkeypatch.setattr(converse, "_monotonic", fake_clock)
    monkeypatch.setattr(node.time, "monotonic", fake_clock)
    skipped_calls = []
    monkeypatch.setattr(node, "cards", lambda *a, **kw: skipped_calls.append("cards"))
    monkeypatch.setattr(cards, "search", lambda *a, **kw: skipped_calls.append("search"))
    monkeypatch.setattr(transcript_graph, "search_transcripts", lambda *a, **kw: skipped_calls.append("tx"))

    cc_dl = converse.build_conversation_context(
        sf, st, key="msg/1791640000000-kxxx-aaaaaa", primary=primary, deadline_s=10.0
    )
    assert skipped_calls == []
    assert cc_dl["errors"]["active_cards"] == "skipped (deadline_exceeded)"
    assert cc_dl["errors"]["transcript_pointers"] == "skipped (deadline_exceeded)"


def test_long_256_char_thread_ids_and_keys_preserved_for_lookups_and_cross_thread_edges(tmp_path, monkeypatch):
    """GitHub Codex P2 4467094981: preserve full 256-char thread_ids and <=512-char msg/ keys across read & context."""
    sf, st = _fake_state(tmp_path)
    prefix_250 = "t" + ("-segment01" * 24) + "-abcde123"
    tid_anchor, tid_sibling = prefix_250 + "-alpha", prefix_250 + "-bravo"
    assert len(tid_anchor) == 256 and len(tid_sibling) == 256
    long_key = "msg/1791630000000-antigravity-" + ("k" * 180)
    edge_hits = [
        {"key": "msg/1791630000001-kxxx-same", "author": "kxxx", "thread_id": tid_anchor, "scores": {"pair": 5.0}},
        {"key": long_key, "author": "uxxx", "thread_id": tid_sibling, "scores": {"pair": 4.2}},
    ]
    seen_threads = []
    monkeypatch.setattr(node, "neighbours", lambda *_a, **_kw: {"inferred": {"included": True, "related_to": edge_hits}})
    monkeypatch.setattr(
        node, "cards",
        lambda _sf, _st, q: seen_threads.append(dict(q).get("thread")) or {
            "cards": [{"key": "card/kxxx/0123456789abcdef0123456789abcdef", "type": "Ruling", "seat": "kxxx",
                       "owner": "operator", "thread_id": tid_anchor, "text": "Preserve 256-char thread IDs"}]
        },
    )
    monkeypatch.setattr(cards, "search", lambda *_a, **_kw: {"hits": []})
    monkeypatch.setattr(transcript_graph, "search_transcripts", lambda *_a, **_kw: {"hits": []})

    for mode, prim in [
        ("read", {"value": {"thread_id": tid_anchor, "body": "long thread check"}}),
        ("context", {"anchor": {"key": "msg/1791640000000-kxxx-aaaaaa", "thread_id": tid_anchor, "body": "long thread check"},
                     "related_edges": {"hits": edge_hits}}),
    ]:
        cc = converse.build_conversation_context(sf, st, key="msg/1791640000000-kxxx-aaaaaa", primary=prim, mode=mode)
        assert seen_threads[-1] == tid_anchor
        assert [(e["key"], e["thread_id"]) for e in cc["related_edges"]] == [(long_key, tid_sibling)]
        assert cc["active_cards"][0]["thread"] == tid_anchor
