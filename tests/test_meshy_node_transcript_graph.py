"""Unit and CLI tests for `tools/meshy_node/transcript_graph.py` and outbound paste guards.

Runs cleanly with `--noconftest` (zero database and zero network sockets).

Run:
    .venv/Scripts/python.exe -m pytest --noconftest tests/test_meshy_node_transcript_graph.py -q -p no:cacheprovider
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.meshy_node import cards, cli, node, sendguard, transcript_graph as tg, wire  # noqa: E402

PEER_A = "11111111-1111-1111-1111-111111111111"
SRC_HEX = "a" * 32


def _minted_state(base_url: str = "http://127.0.0.1:6310", label: str = "antigravity") -> dict:
    return {
        "schema": "meshy-node-state/1",
        "base_url": base_url,
        "peer_id": PEER_A,
        "label": label,
        "status": "minted",
        "kid": "kid-1",
        "signing_seed_b64url": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "memory_peer_id": "0447bf92-25fb-5230-b531-5a0d9c00d8c7",
    }


def _write_state_file(tmp_path: Path, base_url: str = "http://127.0.0.1:6310") -> Path:
    st_path = tmp_path / "state.json"
    st_path.write_text(json.dumps(_minted_state(base_url)), encoding="utf-8")
    return st_path


def _sample_hit(
    *,
    seat: str = "kxxx",
    src: str = SRC_HEX,
    gen: int = 2,
    batch: int = 3,
    first_ord: int = 10,
    last_ord: int = 12,
    text: str | None = "Investigated transcript-context rerank latency.",
    score: float = 0.8123,
    rerank_score: float | None = 0.945,
) -> dict:
    return {
        "seat": seat,
        "src": src,
        "gen": gen,
        "batch": batch,
        "first_ord": first_ord,
        "last_ord": last_ord,
        "char_start": 0,
        "char_end": len(text or ""),
        "time_first": "2026-10-26T07:00:00.000Z",
        "time_last": "2026-10-26T07:01:00.000Z",
        "item_keys": [f"ctx/{seat}/{src}/{gen:04d}/{batch:06d}/{o:010d}" for o in range(first_ord, last_ord + 1)],
        "text": text,
        "text_available": text is not None,
        "score": score,
        "rerank_score": rerank_score,
    }


def _memo_invoke_ok(ns: str, key: str, value: dict, *, is_head: bool = True, peer_id: str = PEER_A) -> dict:
    return {
        "call_id": "call-1",
        "status": "completed",
        "result_signature_verified": True,
        "result": {
            "ok": True,
            "op": "memory.read",
            "memo": {
                "namespace": ns,
                "key": key,
                "version": 1,
                "is_head": is_head,
                "created_at": "2026-10-26 07:05:00+00:00",
                "created_by_peer_id": peer_id,
                "author_label": value.get("seat", "kxxx"),
                "provenance": {"payload": {}},
                "value": value,
            },
        },
    }


def test_client_isolation_and_server_constant_parity():
    src_path = ROOT / "tools" / "meshy_node" / "transcript_graph.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                assert not alias.name.startswith("apps"), alias.name
        elif isinstance(stmt, ast.ImportFrom) and stmt.module:
            assert not stmt.module.startswith("apps"), stmt.module

    srv_src = (ROOT / "apps" / "context" / "validation.py").read_text(encoding="utf-8")
    assert f'DEFAULT_NAMESPACE = "{tg.DEFAULT_TRANSCRIPT_NAMESPACE}"' in srv_src
    assert tg.ITEM_KEY_RE.fullmatch(f"ctx/antigravity/{SRC_HEX}/0001/000001/0000000000")
    assert tg.MANIFEST_KEY_RE.fullmatch(f"ctxm/antigravity/{SRC_HEX}/0001/000001")


def test_search_transcripts_forwarding_untrusted_framing_and_read_cmd(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_search(path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None):
        captured.update({"path": path, "q": q, "k": k, "rerank": rerank, "ns": ns, "card_filter": card_filter})
        return {
            "namespace": ns,
            "space": "bge-m3",
            "k": k,
            "collapse": "session",
            "gate_open": True,
            "index_behind": False,
            "hits": [
                _sample_hit(first_ord=5, last_ord=7),
                _sample_hit(seat="pxx", first_ord=100, last_ord=160, text=None, rerank_score=None),
            ],
        }

    monkeypatch.setattr(node, "search", fake_search)
    st = _minted_state()
    res = tg.search_transcripts(
        tmp_path / "state.json",
        st,
        q="  rerank latency  ",
        k=15,
        rerank=False,
        ns="shared/transcript-context",
        seat="kxxx",
        src=SRC_HEX,
        since="2026-10-25T00:00:00Z",
        until="2026-10-26T12:00:00Z",
        collapse="session",
    )

    assert captured["q"] == "rerank latency"
    assert captured["k"] == 15
    assert captured["rerank"] is False
    assert captured["ns"] == "shared/transcript-context"
    assert captured["card_filter"] == [
        ("seat", "kxxx"),
        ("src", SRC_HEX),
        ("since", "2026-10-25T00:00:00Z"),
        ("until", "2026-10-26T12:00:00Z"),
        ("collapse", "session"),
    ]
    assert res["_untrusted_transcript_evidence"] is True
    assert res["evidence_warning"] == tg.UNTRUSTED_BANNER
    assert len(res["hits"]) == 2
    h0, h1 = res["hits"]
    assert h0["untrusted"] is True
    assert h0["signature_verified"] is False
    assert h0["read_cmd"] == (
        f"meshy_node transcript-read --seat kxxx --src {SRC_HEX} --gen 2 --batch 3 --first-ord 5 --last-ord 7 --namespace shared/transcript-context"
    )
    # Span > 32 in a chunk hit caps suggested --last-ord at first_ord + 31 (100..131)
    assert h1["read_cmd"] == (
        f"meshy_node transcript-read --seat pxx --src {SRC_HEX} --gen 2 --batch 3 --first-ord 100 --last-ord 131 --namespace shared/transcript-context"
    )

    rendered = tg.format_transcript_search(res)
    assert rendered.splitlines()[0] == tg.UNTRUSTED_BANNER
    assert "seat=kxxx" in rendered
    assert "<text unavailable>" in rendered
    assert "\u2014" not in rendered and "\u2013" not in rendered


def test_search_transcripts_default_chunk_and_custom_namespace_env(monkeypatch, tmp_path):
    seen_gets: list = []

    def fake_get(path, state, route, query=None):
        seen_gets.append((route, query))
        return {"namespace": "scratch/tx-ns", "collapse": "chunk", "gate_open": False, "reason": "gate closed", "hits": []}

    # Exercise real `node.search` unpacking `*card_filter` when default `collapse="chunk"` produces `params == []`
    orig_search = node.search

    def checked_search(*a, **kw):
        assert isinstance(kw.get("card_filter"), list), f"card_filter must be list, got {kw.get('card_filter')!r}"
        return orig_search(*a, **kw)

    monkeypatch.setattr(node, "search", checked_search)
    monkeypatch.setattr(node, "_get", fake_get)
    monkeypatch.setenv("MESHY_CONTEXT_NAMESPACE", "scratch/tx-ns")

    res = tg.search_transcripts(tmp_path / "s.json", _minted_state(), q="hello", ns=None, collapse="chunk")
    assert seen_gets == [
        ("/v1/mesh/search", [("q", "hello"), ("k", 10), ("rerank", 1), ("ns", "scratch/tx-ns")]),
    ]
    assert res["namespace"] == "scratch/tx-ns"
    txt = tg.format_transcript_search(res)
    assert "gate_reason: gate closed" in txt
    assert "(no matching transcript chunks)" in txt

    # Invalid env var with whitespace falls back to DEFAULT_TRANSCRIPT_NAMESPACE
    monkeypatch.setenv("MESHY_CONTEXT_NAMESPACE", "bad ns")
    assert tg.named_transcript_namespace(None) == tg.DEFAULT_TRANSCRIPT_NAMESPACE


def test_search_transcripts_validation_and_mismatched_namespace_refusal(monkeypatch, tmp_path):
    st = _minted_state()
    sf = tmp_path / "s.json"
    monkeypatch.setattr(
        node, "search", lambda *a, **kw: {"namespace": kw["ns"], "hits": [_sample_hit()]}
    )

    for bad_ns in ("", "   ", "shared/bad space", "shared/bad\nline", "shared/\x7f"):
        with pytest.raises(node.Refused, match="namespace"):
            tg.search_transcripts(sf, st, q="ok", ns=bad_ns)

    for bad_q in ("", "   "):
        with pytest.raises(node.Refused, match="--q"):
            tg.search_transcripts(sf, st, q=bad_q)

    for bad_k in (0, 51, -1, True):
        with pytest.raises(node.Refused, match="--k"):
            tg.search_transcripts(sf, st, q="ok", k=bad_k)  # type: ignore[arg-type]

    assert tg.MAX_SEARCH_K == 50
    ok_min = tg.search_transcripts(sf, st, q="ok", k=1, ns="custom/tx")
    ok_max = tg.search_transcripts(sf, st, q="ok", k=50, ns="custom/tx")
    assert "--namespace custom/tx" in ok_min["hits"][0]["read_cmd"]
    assert ok_max["hits"][0]["untrusted"] is True

    with pytest.raises(node.Refused, match="--collapse"):
        tg.search_transcripts(sf, st, q="ok", collapse="thread")

    with pytest.raises(node.Refused, match="--seat"):
        tg.search_transcripts(sf, st, q="ok", seat="INVALID SEAT!")

    with pytest.raises(node.Refused, match="control"):
        tg.search_transcripts(sf, st, q="ok", src="abc\x00def")

    # Older server ignored `ns` and returned mail search hits without `namespace == ns`
    monkeypatch.setattr(node, "search", lambda *a, **kw: {"hits": [{"key": "msg/1", "body": "mail"}]})
    with pytest.raises(node.Refused, match="did not answer for namespace"):
        tg.search_transcripts(sf, st, q="ok")

    # Non-list hits refused
    monkeypatch.setattr(node, "search", lambda *a, **kw: {"namespace": tg.DEFAULT_TRANSCRIPT_NAMESPACE, "hits": "bad"})
    with pytest.raises(node.Refused, match="did not answer for namespace"):
        tg.search_transcripts(sf, st, q="ok")


def test_read_transcript_items_keys_spans_missing_and_signatures(monkeypatch, tmp_path):
    sf = _write_state_file(tmp_path)
    st = _minted_state()
    ns = tg.DEFAULT_TRANSCRIPT_NAMESPACE

    k0 = f"ctx/kxxx/{SRC_HEX}/0001/000002/0000000005"
    k1 = f"ctx/kxxx/{SRC_HEX}/0001/000002/0000000006"
    k2 = f"ctx/kxxx/{SRC_HEX}/0001/000002/0000000007"
    km = f"ctxm/kxxx/{SRC_HEX}/0001/000002"

    responses = {
        k0: _memo_invoke_ok(
            ns,
            k0,
            {
                "schema": "context.v1",
                "kind": "item",
                "seat": "kxxx",
                "src": SRC_HEX,
                "gen": 1,
                "batch": 2,
                "ordinal": 5,
                "item": "user_text",
                "role": "user",
                "time": "2026-10-26T07:00:00.000Z",
                "text": "Line one\x1b[31m\nLine two",
            },
        ),
        k1: {
            "call_id": "call-missing",
            "status": "failed",
            "result_signature_verified": True,
            "error_code": "E_UNKNOWN_KEY",
            "error_message": "unknown key",
            "result": None,
        },
        k2: _memo_invoke_ok(
            ns,
            k2,
            {
                "schema": "context.v1",
                "kind": "item",
                "seat": "kxxx",
                "src": SRC_HEX,
                "gen": 1,
                "batch": 2,
                "ordinal": 7,
                "item": "omitted",
                "omitted_reason": "too_large",
                "omitted_bytes": 90000,
                "time": "2026-10-26T07:00:05.000Z",
            },
            peer_id="99999999-9999-9999-9999-999999999999",
        ),
        km: _memo_invoke_ok(
            ns,
            km,
            {
                "schema": "context.v1",
                "kind": "manifest",
                "seat": "kxxx",
                "src": SRC_HEX,
                "gen": 1,
                "batch": 2,
                "first": 5,
                "last": 7,
                "counts": {"written": 2, "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 1},
                "time": "2026-10-26T07:00:06.000Z",
            },
        ),
    }

    monkeypatch.setattr(node, "invoke", lambda path, state, *, skill, args: responses[args["key"]])
    monkeypatch.setattr(
        wire,
        "author_signature_ok",
        lambda memo, fetch_jwks: (True, None)
        if memo["created_by_peer_id"] == PEER_A
        else (False, "NO_ACTIVE_KEY: rotated"),
    )

    try:
        span_res = tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=1, batch=2, first_ord=5, last_ord=7)
    except (node.Refused, node.ReadFailed) as exc:
        assert False, f"E_UNKNOWN_KEY must be recorded as missing, not raise {type(exc).__name__}: {exc}"
    assert span_res["_untrusted_transcript_evidence"] is True
    assert span_res["present_count"] == 2
    assert span_res["missing_count"] == 1
    i0, i1, i2 = span_res["items"]
    assert i0["status"] == "present" and i0["author_signature_verified"] is True and i0["signed_by"] == PEER_A
    assert i1["status"] == "missing" and i1["author_signature_verified"] is False and i1["signed_by"] is None
    assert i2["status"] == "present" and i2["author_signature_verified"] is False and i2["signed_by"] is None
    assert i2["author_signature_error"] == "NO_ACTIVE_KEY: rotated"

    fmt = tg.format_transcript_read(span_res)
    assert fmt.splitlines()[0] == tg.UNTRUSTED_BANNER
    assert "author_signature_verified=true" in fmt
    assert "status=missing author_signature_verified=false" in fmt
    assert "author_signature_verified=false (NO_ACTIVE_KEY: rotated)" in fmt
    assert "<omitted: too_large>" in fmt
    assert "\x1b" not in fmt

    # Exact manifest key read
    man_res = tg.read_transcript_items(sf, st, key=km)
    assert man_res["present_count"] == 1
    assert "manifest first=5 last=7" in tg.format_transcript_read(man_res)

    # Task item read (Codex P2 4376123846): renders step, event, and optional counts even when text is absent
    kt = f"ctx/kxxx/{SRC_HEX}/0001/000002/0000000008"
    responses[kt] = _memo_invoke_ok(
        tg.DEFAULT_TRANSCRIPT_NAMESPACE,
        kt,
        {
            "schema": "context.v1",
            "kind": "item",
            "seat": "kxxx",
            "src": SRC_HEX,
            "gen": 1,
            "batch": 2,
            "ordinal": 8,
            "item": "task",
            "step": "Verify canonical JCS readback",
            "event": "ambiguous",
            "counts": {"pending": 1, "in_progress": 0, "completed": 2},
            "time": "2026-10-26T07:00:07.000Z",
        },
    )
    task_res = tg.read_transcript_items(sf, st, key=kt)
    task_fmt = tg.format_transcript_read(task_res)
    assert "[task event=ambiguous counts={'pending': 1, 'in_progress': 0, 'completed': 2}]" in task_fmt
    assert "Verify canonical JCS readback" in task_fmt


def test_read_transcript_items_bounds_and_read_failures(monkeypatch, tmp_path):
    sf = _write_state_file(tmp_path)
    st = _minted_state()
    ns = tg.DEFAULT_TRANSCRIPT_NAMESPACE

    # Exact 32-item span passes; 33-item span is refused before any invoke call
    invoked: list[str] = []
    monkeypatch.setattr(
        node,
        "invoke",
        lambda path, state, *, skill, args: (
            invoked.append(args["key"])
            or {
                "call_id": "c",
                "status": "failed",
                "result_signature_verified": True,
                "error_code": "E_UNKNOWN_KEY",
                "result": None,
            }
        ),
    )
    ok32 = tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=1, batch=1, first_ord=0, last_ord=31)
    assert len(ok32["items"]) == 32
    assert len(invoked) == 32

    invoked.clear()
    with pytest.raises(node.Refused, match="exceeds max 32"):
        tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=1, batch=1, first_ord=0, last_ord=32)
    assert invoked == []

    # Validation refusals
    with pytest.raises(node.Refused, match="not both"):
        tg.read_transcript_items(sf, st, key=f"ctx/kxxx/{SRC_HEX}/0001/000001/0000000000", seat="kxxx")
    with pytest.raises(node.Refused, match="requires either --key"):
        tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=1, batch=1)
    with pytest.raises(node.Refused, match="must match ctx/"):
        tg.read_transcript_items(sf, st, key="msg/1793000000000-kxxx-abcdef")
    with pytest.raises(node.Refused, match="gen and batch segments must be >= 1"):
        tg.read_transcript_items(sf, st, key=f"ctx/kxxx/{SRC_HEX}/0000/000001/0000000000")
    with pytest.raises(node.Refused, match="gen and batch segments must be >= 1"):
        tg.read_transcript_items(sf, st, key=f"ctx/kxxx/{SRC_HEX}/0001/000000/0000000000")
    with pytest.raises(node.Refused, match="--src"):
        tg.read_transcript_items(sf, st, seat="kxxx", src="not-hex", gen=1, batch=1, first_ord=0)
    with pytest.raises(node.Refused, match="--gen"):
        tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=0, batch=1, first_ord=0)
    with pytest.raises(node.Refused, match="--batch"):
        tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=1, batch=0, first_ord=0)
    with pytest.raises(node.Refused, match="--first-ord"):
        tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=1, batch=1, first_ord=-1)
    with pytest.raises(node.Refused, match="--last-ord"):
        tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=1, batch=1, first_ord=5, last_ord=-1)
    with pytest.raises(node.Refused, match=">= --first-ord"):
        tg.read_transcript_items(sf, st, seat="kxxx", src=SRC_HEX, gen=1, batch=1, first_ord=5, last_ord=4)

    # Server ReadFailed conditions: poll timeout, unverified result signature, forbidden refusal, memo mismatch
    k = f"ctx/kxxx/{SRC_HEX}/0001/000001/0000000000"
    monkeypatch.setattr(node, "invoke", lambda *a, **kw: {"call_id": "c1", "status": "running"})
    with pytest.raises(node.ReadFailed) as e_timeout:
        tg.read_transcript_items(sf, st, key=k)
    assert e_timeout.value.error["error"] == node.E_READ_POLL_TIMEOUT

    monkeypatch.setattr(
        node, "invoke", lambda *a, **kw: {"call_id": "c2", "status": "completed", "result_signature_verified": False}
    )
    with pytest.raises(node.ReadFailed) as e_unv:
        tg.read_transcript_items(sf, st, key=k)
    assert e_unv.value.error["error"] == node.E_RESULT_UNVERIFIED

    monkeypatch.setattr(
        node,
        "invoke",
        lambda *a, **kw: {
            "call_id": "c3",
            "status": "failed",
            "result_signature_verified": True,
            "error_code": "E_FORBIDDEN",
            "error_message": "denied",
            "result": None,
        },
    )
    with pytest.raises(node.ReadFailed) as e_ref:
        tg.read_transcript_items(sf, st, key=k)
    assert e_ref.value.error["error"] == node.E_READ_REFUSED

    monkeypatch.setattr(node, "invoke", lambda *a, **kw: _memo_invoke_ok("other/ns", k, {"seat": "kxxx"}))
    with pytest.raises(node.ReadFailed) as e_mis:
        tg.read_transcript_items(sf, st, key=k)
    assert e_mis.value.error["error"] == node.E_RESULT_MISMATCH

    monkeypatch.setattr(node, "invoke", lambda *a, **kw: _memo_invoke_ok(ns, k, {"seat": "kxxx"}, is_head=False))
    with pytest.raises(node.ReadFailed) as e_head:
        tg.read_transcript_items(sf, st, key=k)
    assert e_head.value.error["error"] == node.E_RESULT_MISMATCH


def test_fleet_transcript_consensus_aggregation_boundaries_and_formatting(monkeypatch, tmp_path):
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)

    def ts(delta_s: float) -> str:
        dt = now - timedelta(seconds=delta_s)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    card_rows = [
        # antigravity: older Context superseded by newer Context exactly at 6.0h (not stale -> health ok)
        {
            "key": "card/antigravity/" + "1" * 32,
            "id": "1" * 32,
            "seat": "antigravity",
            "type": "Context",
            "status": "active",
            "time": ts(8 * 3600),
            "text": "Context | owner: antigravity\nOld task",
        },
        {
            "key": "card/antigravity/" + "2" * 32,
            "id": "2" * 32,
            "seat": "antigravity",
            "type": "Context",
            "status": "active",
            "time": ts(6 * 3600),  # exact threshold 6.0h -> stale is False
            "supersedes": ["card/antigravity/" + "1" * 32],
            "text": "Context | owner: antigravity\n" + ("x" * 220),
        },
        {
            "key": "card/antigravity/" + "3" * 32,
            "id": "3" * 32,
            "seat": "antigravity",
            "type": "Commitment",
            "status": "active",
            "thread_id": "t-orient",
            "time": ts(1800),
            "text": "Commitment | owner: antigravity\nShip transcript consensus",
        },
        # pxx: Context 6h + 1s old WITH open Commitment -> stale=True, health="attention"
        {
            "key": "card/pxx/" + "4" * 32,
            "id": "4" * 32,
            "seat": "pxx",
            "type": "Context",
            "status": "active",
            "time": ts(6 * 3600 + 1),
            "text": "Context | owner: pxx\nReviewing PR #415",
        },
        {
            "key": "card/pxx/" + "5" * 32,
            "id": "5" * 32,
            "seat": "pxx",
            "type": "Commitment",
            "status": "active",
            "thread_id": "t-415",
            "time": ts(3600),
            "text": "Commitment | owner: pxx\nAudit ctxsearch",
        },
        # dxxxxx: Question 86399s (< 24h) -> kept (health="attention"); Question 86400s (== 24h) -> dropped
        {
            "key": "card/dxxxxx/" + "6" * 32,
            "id": "6" * 32,
            "seat": "dxxxxx",
            "type": "Question",
            "status": "active",
            "thread_id": "t-q",
            "question": "Approve schema migration?",
            "time": ts(cards.QUESTION_MAX_AGE_S - 1),
            "text": "Question | owner: dxxxxx\nApprove schema migration?",
        },
        {
            "key": "card/dxxxxx/" + "7" * 32,
            "id": "7" * 32,
            "seat": "dxxxxx",
            "type": "Question",
            "status": "active",
            "question": "Expired question",
            "time": ts(cards.QUESTION_MAX_AGE_S),
            "text": "Question | owner: dxxxxx\nExpired question",
        },
        # txxxx: stale Context (10h) WITHOUT open commitments + OpenItem + Constraint -> health="ok"
        {
            "key": "card/txxxx/" + "8" * 32,
            "id": "8" * 32,
            "seat": "txxxx",
            "type": "Context",
            "status": "active",
            "time": ts(10 * 3600),
            "text": "Context | owner: txxxx\nIdle after detector sweep",
        },
        {
            "key": "card/txxxx/" + "9" * 32,
            "id": "9" * 32,
            "seat": "txxxx",
            "type": "OpenItem",
            "status": "active",
            "time": ts(7200),
            "text": "OpenItem | owner: txxxx\nFollow up on loader fixture",
        },
        {
            "key": "card/txxxx/" + "a" * 32,
            "id": "a" * 32,
            "seat": "txxxx",
            "type": "Constraint",
            "status": "active",
            "time": ts(900),
            "text": "Constraint | owner: operator\nNever run DB tests during HQB merge-gate",
        },
        # Decision/Ruling rows returned via replaces_requested=True and superseded Commitment/Question: ignored
        {
            "key": "card/txxxx/" + "b" * 32,
            "id": "b" * 32,
            "seat": "txxxx",
            "type": "Decision",
            "status": "active",
            "replaces_requested": True,
            "time": ts(600),
            "text": "Closed commitment",
        },
        {
            "key": "card/txxxx/" + "c" * 32,
            "id": "c" * 32,
            "seat": "txxxx",
            "type": "Commitment",
            "status": "active",
            "superseded_by_count": 1,
            "time": ts(5000),
            "text": "Already superseded",
        },
        {
            "key": "card/dxxxxx/" + "d" * 32,
            "id": "d" * 32,
            "seat": "dxxxxx",
            "type": "Question",
            "status": "active",
            "question": "Answered question",
            "time": ts(1200),
            "text": "Question | owner: dxxxxx\nAnswered question",
        },
        {
            "key": "card/dxxxxx/" + "e" * 32,
            "id": "e" * 32,
            "seat": "dxxxxx",
            "type": "Ruling",
            "status": "active",
            "replaces_requested": True,
            "supersedes": ["card/dxxxxx/" + "d" * 32],
            "time": ts(600),
            "text": "Ruling | owner: operator\nApproved",
        },
        # codex-bxxxxx-cli: historical unpaired question_codex Question (>= 24h) + historical 72h-old Candidate
        # ingested freshly at exact 36.0h written_at boundary (Codex P2: written_at governs ingest freshness, not time)
        {
            "key": "card/codex-bxxxxx-cli/" + "f" * 32,
            "id": "f" * 32,
            "seat": "codex-bxxxxx-cli",
            "type": "Question",
            "status": "active",
            "question": "Historical codex question (48h old, never paired with Ruling)",
            "time": ts(48 * 3600),
            "written_at": ts(48 * 3600),
            "text": "Question | owner: codex-bxxxxx-cli\nHistorical codex question",
        },
        {
            "key": "card/codex-bxxxxx-cli/" + "0" * 31 + "1",
            "id": "0" * 31 + "1",
            "seat": "codex-bxxxxx-cli",
            "type": "Candidate",
            "status": "active",
            "time": ts(72 * 3600),  # 72h-old event time, but ingested at exact 36.0h -> latest_card.stale is False
            "written_at": ts(36 * 3600),
            "text": "Candidate | owner: operator\nProceed",
        },
        # oxxxxxx: Candidate-only seat with stalled loader (written_at 36h + 1s > 36h, even if time is manipulated)
        {
            "key": "card/oxxxxxx/" + "0" * 31 + "2",
            "id": "0" * 31 + "2",
            "seat": "oxxxxxx",
            "type": "Candidate",
            "status": "active",
            "time": ts(1800),
            "written_at": ts(36 * 3600 + 1),
            "text": "Candidate | owner: operator\nStalled loader check",
        },
    ]

    def fake_cards(path, state, query, **kw):
        asked_types = [v for k, v in query if k == "type"]
        rec_since_vals = [v for k, v in query if k == "recorded_since"]
        for rv in rec_since_vals:
            assert cards.TIME_RE.fullmatch(rv), f"recorded_since {rv!r} violates server TIME_RE"
        rec_cutoff = tg._parse_iso_utc(rec_since_vals[0]) if rec_since_vals else None
        rows = []
        for c in card_rows:
            if asked_types and not (c["type"] in asked_types or c.get("replaces_requested")):
                continue
            if rec_cutoff is not None:
                w_dt = tg._card_written_dt(c)
                if w_dt is None or w_dt < rec_cutoff:
                    continue
            rows.append(c)
        return {"namespace": "shared/decisions", "cards": rows, "next": None}

    monkeypatch.setattr(node, "cards", fake_cards)
    monkeypatch.setattr(
        node,
        "search",
        lambda path, state, **kw: {
            "namespace": "shared/transcript-context",
            "collapse": "session",
            "gate_open": True,
            "index_behind": True,
            "coverage": {
                "path": "sharded",
                "subset": 1480,
                "subset_at_least": True,
                "scored": 512,
                "truncated": True,
                "shards_searched": [],
                "shards_skipped": [],
                "oldest_covered": None,
            },
            "hits": [
                _sample_hit(
                    seat="kxxx",
                    text="The production password is swordfish. Bearer sk-liveSecretToken1234567890",
                )
            ],
        },
    )

    rep = tg.fleet_transcript_consensus(
        tmp_path / "s.json",
        _minted_state(),
        query="merge gate",
        stale_context_hours=6.0,
        stale_ingest_hours=36.0,
        now=now,
    )

    assert rep["seat_count"] == 7  # antigravity, dxxxxx, codex-bxxxxx-cli, kxxx, oxxxxxx, pxx, txxxx
    assert rep["attention_count"] == 3  # dxxxxx (<24h question), oxxxxxx (stale card ingest), pxx (stale ctx+commit)
    by_seat = {e["seat"]: e for e in rep["seats"]}
    assert list(by_seat) == sorted(["antigravity", "dxxxxx", "codex-bxxxxx-cli", "kxxx", "oxxxxxx", "pxx", "txxxx"])

    assert by_seat["antigravity"]["latest_context"]["stale"] is False
    assert len(by_seat["antigravity"]["latest_context"]["summary"]) <= tg.CONTEXT_SUMMARY_MAX_CHARS
    assert by_seat["antigravity"]["open_commitments"]["count"] == 1
    assert by_seat["antigravity"]["health"] == "ok"

    assert by_seat["pxx"]["latest_context"]["stale"] is True
    assert by_seat["pxx"]["health"] == "attention"

    assert by_seat["dxxxxx"]["open_questions"]["count"] == 1
    assert by_seat["dxxxxx"]["open_questions"]["items"][0]["question"] == "Approve schema migration?"
    assert by_seat["dxxxxx"]["health"] == "attention"

    assert by_seat["codex-bxxxxx-cli"]["open_questions"]["count"] == 0
    assert by_seat["codex-bxxxxx-cli"]["latest_card"]["stale"] is False
    assert by_seat["codex-bxxxxx-cli"]["latest_card"]["age_hours"] == 36.0
    assert by_seat["codex-bxxxxx-cli"]["health"] == "ok"

    assert by_seat["oxxxxxx"]["latest_context"] is None
    assert by_seat["oxxxxxx"]["latest_card"]["stale"] is True
    assert by_seat["oxxxxxx"]["health"] == "attention"

    assert by_seat["txxxx"]["latest_context"]["stale"] is True
    assert by_seat["txxxx"]["open_commitments"]["count"] == 0
    assert by_seat["txxxx"]["open_items"]["count"] == 1
    assert by_seat["txxxx"]["health"] == "ok"

    assert by_seat["kxxx"]["indexed_transcript_hits"]["count"] == 1
    # Codex P1 regression: consensus never emits raw transcript excerpts (neither JSON nor text),
    # so unrecognized secrets like "swordfish" cannot bypass sendguard via consensus output.
    assert "excerpt" not in by_seat["kxxx"]["indexed_transcript_hits"]["latest"]
    assert "swordfish" not in json.dumps(rep)
    assert "sk-liveSecretToken" not in json.dumps(rep)

    assert len(rep["antibodies"]) == 1
    alert_kinds = [a["kind"] for a in rep["alerts"]]
    assert "unanswered_question" in alert_kinds
    assert "stale_context" in alert_kinds
    assert "stale_card_ingestion" in alert_kinds
    assert "transcript_index_behind" in alert_kinds
    assert "antibody" in alert_kinds

    text = tg.format_consensus_report(rep)
    assert len(text.splitlines()) < 80
    assert "\u2014" not in text and "\u2013" not in text
    assert "swordfish" not in text and "sk-liveSecretToken" not in text
    assert (
        f"meshy_node transcript-read --seat kxxx --src {SRC_HEX} --gen 2 --batch 3 --first-ord 10 --last-ord 12 --namespace shared/transcript-context"
        in text
    )
    assert f"src={SRC_HEX}" in text
    assert "[ATTN] dxxxxx" in text
    assert "[ATTN] oxxxxxx" in text
    assert "[ATTN] pxx" in text
    assert "[OK] antigravity" in text
    assert "chunks=>=1480 | scored=512" in text
    assert "indexed=1" not in text
    assert "Topic hits (1):" in text
    # Verify exact subset and unknown subset coverage formatting as well
    assert tg._coverage_summary({"path": "exact", "subset": 42, "subset_at_least": False, "scored": 42}, 1) == (
        42,
        "chunks=42 | scored=42",
    )
    assert tg._coverage_summary({"path": None, "subset": None, "subset_at_least": False, "scored": 0}, 3) == (
        None,
        "chunks=unknown | hits=3",
    )
    assert tg._coverage_summary({}, 2) == (None, "hits=2")


def test_fleet_transcript_consensus_graceful_degradation_and_line_cap(monkeypatch, tmp_path):
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
    sf = tmp_path / "s.json"
    st = _minted_state()

    for bad_hours in (0, -2.5, True, float("nan"), float("inf")):
        with pytest.raises(node.Refused, match="--stale-hours"):
            tg.fleet_transcript_consensus(sf, st, stale_context_hours=bad_hours, now=now)  # type: ignore[arg-type]
        with pytest.raises(node.Refused, match="--stale-ingest-hours"):
            tg.fleet_transcript_consensus(sf, st, stale_ingest_hours=bad_hours, now=now)  # type: ignore[arg-type]

    def raise_422(*a, **kw):
        raise node.ServerSaid(422, {"code": "E_VALIDATION", "message": "namespace not indexed"})

    monkeypatch.setattr(node, "search", raise_422)

    # Wrong namespace on GET /v1/cards (independently on typed active walk and status=any activity walk) is refused
    monkeypatch.setattr(
        node,
        "cards",
        lambda path, state, query, **kw: {
            "namespace": "wrong/ns" if ("status", "active") in query else "shared/decisions",
            "cards": [],
            "next": None,
        },
    )
    with pytest.raises(node.Refused, match="did not answer with a card list"):
        tg.fleet_transcript_consensus(sf, st, now=now)

    monkeypatch.setattr(
        node,
        "cards",
        lambda path, state, query, **kw: {
            "namespace": "shared/decisions" if ("status", "active") in query else "wrong/ns",
            "cards": [],
            "next": None,
        },
    )
    with pytest.raises(node.Refused, match="did not answer with a card list"):
        tg.fleet_transcript_consensus(sf, st, now=now)

    # 422 on transcript-context search degrades gracefully without failing consensus
    many_seats = [
        {
            "key": f"card/seat{i:02d}/" + f"{i:032x}",
            "id": f"{i:032x}",
            "seat": f"seat{i:02d}",
            "type": "Question",
            "status": "active",
            "question": f"Question {i}",
            "time": "2026-10-26T11:30:00.000Z",
            "text": f"Question {i}",
        }
        for i in range(1, 36)
    ]
    monkeypatch.setattr(
        node, "cards", lambda *a, **kw: {"namespace": "shared/decisions", "cards": many_seats, "next": None}
    )

    rep = tg.fleet_transcript_consensus(sf, st, now=now)
    assert rep["transcript_index"]["available"] is False
    assert rep["transcript_index"]["http_status"] == 422
    assert any(a["kind"] == "transcript_index_unavailable" for a in rep["alerts"])
    summary = tg.format_consensus_report(rep)
    assert len(summary.splitlines()) < 80
    assert "available=false" in summary

    # Server 500 degrades gracefully with incomplete=True; caller/auth 400/401 still raise fail-closed
    monkeypatch.setattr(
        node, "search", lambda *a, **kw: (_ for _ in ()).throw(node.ServerSaid(500, {"error": "E_INTERNAL", "message": "internal error"}))
    )
    rep_500 = tg.fleet_transcript_consensus(sf, st, now=now)
    assert rep_500["incomplete"] is True and rep_500["transcript_index"]["available"] is False
    assert rep_500["transcript_index"]["http_status"] == 500
    assert any(a["kind"] == "transcript_index_unavailable" and a["http_status"] == 500 for a in rep_500["alerts"])
    assert "status=500" in tg.format_consensus_report(rep_500)

    for fatal_status in (400, 401):
        monkeypatch.setattr(
            node, "search", lambda *a, s=fatal_status, **kw: (_ for _ in ()).throw(node.ServerSaid(s, {"message": "unauthorized"}))
        )
        with pytest.raises(node.ServerSaid) as exc_info:
            tg.fleet_transcript_consensus(sf, st, now=now)
        assert exc_info.value.status == fatal_status

    # Codex P2 regression (4375859293): when >1,000 recent cards cap the 5-page recorded_since sweep,
    # candidate stale seats are verified via seat-specific recorded_since probe (or marked unknown past probe cap)
    # rather than falsely flagged stale_card_ingestion!
    monkeypatch.setattr(node, "search", raise_422)
    old_active = {
        "key": "card/kxxx/" + "a" * 32,
        "id": "a" * 32,
        "seat": "kxxx",
        "type": "Context",
        "status": "active",
        "time": "2026-10-20T00:00:00.000Z",
        "written_at": "2026-10-20T00:00:00.000Z",
        "text": "Context | owner: kxxx\nOlder context",
    }
    fresh_backfill = {
        "key": "card/kxxx/" + "b" * 32,
        "id": "b" * 32,
        "seat": "kxxx",
        "type": "Candidate",
        "status": "active",
        "time": "2026-09-01T00:00:00.000Z",  # older event time buried past page 5
        "written_at": "2026-10-26T11:55:00.000Z",  # freshly ingested 5 min ago!
        "text": "Candidate | owner: operator\nBackfilled prompt",
    }
    noise_card = {
        "key": "card/txxxx/" + "c" * 32,
        "id": "c" * 32,
        "seat": "txxxx",
        "type": "Candidate",
        "status": "active",
        "time": "2026-10-26T11:59:00.000Z",
        "written_at": "2026-10-26T11:59:00.000Z",
        "text": "Candidate | owner: operator\nRecent card",
    }

    def capped_cards(path, state, query, **kw):
        q_map = dict(query)
        if q_map.get("status") == "active":
            return {"namespace": "shared/decisions", "cards": [old_active], "next": None}
        if q_map.get("seat") == "kxxx" and q_map.get("recorded_since"):
            return {"namespace": "shared/decisions", "cards": [fresh_backfill], "next": None}
        if q_map.get("recorded_since"):
            after = q_map.get("after") or "p0"
            nxt = f"{after}x"
            return {"namespace": "shared/decisions", "cards": [noise_card], "next": nxt}
        return {"namespace": "shared/decisions", "cards": [old_active], "next": None}

    monkeypatch.setattr(node, "cards", capped_cards)
    rep_cap = tg.fleet_transcript_consensus(sf, st, now=now)
    by_s_cap = {e["seat"]: e for e in rep_cap["seats"]}
    assert by_s_cap["kxxx"]["latest_card"]["stale"] is False
    assert by_s_cap["kxxx"]["health"] == "ok"
    assert not any(a["kind"] == "stale_card_ingestion" and a["seat"] == "kxxx" for a in rep_cap["alerts"])

    # When probe budget is exhausted, remaining unverified candidate stale seats degrade to stale=None ("(unknown)")
    monkeypatch.setattr(tg, "MAX_SEAT_FRESHNESS_PROBES", 0)
    rep_unk = tg.fleet_transcript_consensus(sf, st, now=now)
    by_s_unk = {e["seat"]: e for e in rep_unk["seats"]}
    assert by_s_unk["kxxx"]["latest_card"]["stale"] is None
    assert by_s_unk["kxxx"]["health"] == "ok"
    assert "(unknown)" in tg.format_consensus_report(rep_unk)
    assert not any(a["kind"] == "stale_card_ingestion" and a["seat"] == "kxxx" for a in rep_unk["alerts"])


def test_cli_dispatch_and_outbound_transcript_paste_guard(monkeypatch, tmp_path, capsys):
    base_url = "http://127.0.0.1:6310"
    sf = _write_state_file(tmp_path, base_url)
    k0 = f"ctx/kxxx/{SRC_HEX}/0001/000001/0000000000"

    # Non-mutating classification
    parser = cli.build_parser()
    for subcmd, extra in (
        ("transcript-search", ["--q", "test"]),
        ("transcript-read", ["--key", k0]),
        ("transcript-consensus", []),
    ):
        parsed = parser.parse_args([subcmd, "--base-url", base_url, "--state-file", str(sf), *extra])
        assert cli._mutating_command(parsed) is False

    monkeypatch.setattr(
        node,
        "search",
        lambda *a, **kw: {
            "namespace": "shared/transcript-context",
            "collapse": "chunk",
            "gate_open": True,
            "index_behind": False,
            "coverage": {"indexed": 5},
            "hits": [_sample_hit(text="AWS_SECRET_ACCESS_KEY=super-secret-value")],
        },
    )
    monkeypatch.setattr(
        node,
        "invoke",
        lambda *a, **kw: _memo_invoke_ok(
            "shared/transcript-context",
            k0,
            {
                "schema": "context.v1",
                "kind": "item",
                "seat": "kxxx",
                "src": SRC_HEX,
                "gen": 1,
                "batch": 1,
                "ordinal": 0,
                "item": "assistant_text",
                "role": "assistant",
                "time": "2026-10-26T08:00:00.000Z",
                "text": "API_TOKEN=live-secret-token",
            },
        ),
    )
    monkeypatch.setattr(wire, "author_signature_ok", lambda *a, **kw: (True, None))
    def fake_cli_cards(path, state, query, **kw):
        for k, v in query:
            if k == "recorded_since":
                assert cards.TIME_RE.fullmatch(v), f"recorded_since {v!r} violates server TIME_RE"
        return {"namespace": "shared/decisions", "cards": [], "next": None}

    monkeypatch.setattr(node, "cards", fake_cli_cards)

    # 1. CLI transcript-search (human & --json)
    assert cli.main(["transcript-search", "--base-url", base_url, "--state-file", str(sf), "--q", "secret"]) == 0
    search_human = capsys.readouterr().out
    assert search_human.startswith(tg.UNTRUSTED_BANNER)

    assert (
        cli.main(["transcript-search", "--base-url", base_url, "--state-file", str(sf), "--q", "secret", "--json"]) == 0
    )
    search_json = capsys.readouterr().out
    assert json.loads(search_json)["_untrusted_transcript_evidence"] is True

    # 2. CLI transcript-read (human & --json)
    assert cli.main(["transcript-read", "--base-url", base_url, "--state-file", str(sf), "--key", k0]) == 0
    read_human = capsys.readouterr().out
    assert read_human.startswith(tg.UNTRUSTED_BANNER)

    assert cli.main(["transcript-read", "--base-url", base_url, "--state-file", str(sf), "--key", k0, "--json"]) == 0
    read_json = capsys.readouterr().out
    assert json.loads(read_json)["_untrusted_transcript_evidence"] is True

    # 3. CLI transcript-consensus (human & --json)
    assert cli.main(["transcript-consensus", "--base-url", base_url, "--state-file", str(sf)]) == 0
    cons_human = capsys.readouterr().out
    assert "FLEET TRANSCRIPT & DIAGNOSTIC CONSENSUS" in cons_human

    assert cli.main(["transcript-consensus", "--base-url", base_url, "--state-file", str(sf), "--json"]) == 0
    cons_json = json.loads(capsys.readouterr().out)
    assert cons_json["card_namespace"] == "shared/decisions"

    # 4. lookup-help advertises transcript-search, transcript-read, and transcript-consensus
    assert cli.main(["lookup-help", "--base-url", base_url, "--state-file", str(sf)]) == 0
    help_out = capsys.readouterr().out
    assert "transcript-search" in help_out
    assert "transcript-read" in help_out
    assert "transcript-consensus" in help_out

    # 5. OUTBOUND PASTE GUARD (kxxx's ruling: plain send, --bulk-ack override, --op-id meshy_send/kxxx_send, and edit)
    sent_calls: list = []
    monkeypatch.setattr(node, "send", lambda *a, **kw: sent_calls.append(("send", kw)) or {"key": "msg/1", "status": "completed", "result": {"ok": True}, "result_signature_verified": True})
    monkeypatch.setattr(node, "op_send", lambda *a, **kw: sent_calls.append(("op_send", kw)))
    monkeypatch.setattr(node, "edit", lambda *a, **kw: sent_calls.append(("edit", kw)))

    single_hit_json = json.dumps(json.loads(search_json)["hits"][0])
    single_item_json = json.dumps(json.loads(read_json)["items"][0])
    for idx, forbidden_payload in enumerate(
        (search_human, search_json, single_hit_json, read_human, read_json, single_item_json)
    ):
        with pytest.raises(node.Refused, match="shared/transcript-context"):
            sendguard.admit(
                sf,
                to=["kxxx"],
                cc=None,
                body=forbidden_payload,
                subject=None,
                bulk_ack="attempted direct admit override",
                refused=node.Refused,
            )
        body_file = tmp_path / f"paste-{idx}.txt"
        body_file.write_text(forbidden_payload, encoding="utf-8")

        # a) Standard send + --bulk-ack CANNOT override
        rc_plain = cli.main(
            [
                "send",
                "--base-url",
                base_url,
                "--state-file",
                str(sf),
                "--to",
                "kxxx",
                "--thread",
                "t-sec",
                "--body-file",
                str(body_file),
                "--bulk-ack",
                "attempted override",
                "--no-recipient-check",
                "--no-card-check",
            ]
        )
        err_plain = json.loads(capsys.readouterr().err)
        assert rc_plain == 2
        assert err_plain["code"] == "E_CLIENT_REFUSED"
        assert "shared/transcript-context" in err_plain["refused"]

        # b) --op-id path (used by meshy_send and kxxx_send) is ALSO refused before writing .ops journal
        rc_op = cli.main(
            [
                "send",
                "--base-url",
                base_url,
                "--state-file",
                str(sf),
                "--op-id",
                f"op-leak-{idx}",
                "--to",
                "kxxx",
                "--thread",
                "t-sec",
                "--body-file",
                str(body_file),
                "--bulk-ack",
                "attempted op override",
                "--no-recipient-check",
                "--no-card-check",
            ]
        )
        err_op = json.loads(capsys.readouterr().err)
        assert rc_op == 2
        assert err_op["code"] == "E_CLIENT_REFUSED"

        # c) edit path is ALSO refused
        rc_edit = cli.main(
            [
                "edit",
                "--base-url",
                base_url,
                "--state-file",
                str(sf),
                "--key",
                "msg/1793000000000-antigravity-123456",
                "--body-file",
                str(body_file),
            ]
        )
        err_edit = json.loads(capsys.readouterr().err)
        assert rc_edit == 2
        assert err_edit["code"] == "E_CLIENT_REFUSED"

    assert sent_calls == []
    assert not (tmp_path / "state.json.ops").exists()

    # d) Citing a clean ctx/... key in prose is admitted normally
    safe_citation = f"Verified the fix in {k0} on seat kxxx; ready to merge."
    admitted_body, _ = sendguard.admit(
        sf, to=["kxxx"], cc=None, body=safe_citation, subject=None, bulk_ack=None, refused=node.Refused
    )
    assert admitted_body == safe_citation
    rc_ok = cli.main(
        [
            "send",
            "--base-url",
            base_url,
            "--state-file",
            str(sf),
            "--to",
            "kxxx",
            "--thread",
            "t-sec",
            "--body",
            safe_citation,
            "--no-recipient-check",
            "--no-card-check",
        ]
    )
    capsys.readouterr()
    assert rc_ok == 0
    assert len(sent_calls) == 1

    # e) Per-item sentinel backstop (kxxx 29936f P2): cutting out ONLY the second item's block (under the
    # top UNTRUSTED_BANNER) with its header from either format_transcript_search or format_transcript_read
    # MUST be refused by sendguard/send; stripping the item header lines passes (stating the guard's limit honestly).
    two_search = tg.format_transcript_search(
        {
            "namespace": "shared/transcript-context",
            "collapse": "chunk",
            "gate_open": True,
            "index_behind": False,
            "hits": [
                _sample_hit(seat="kxxx", first_ord=0, last_ord=0, text="First turn benign text"),
                _sample_hit(seat="pxx", first_ord=1, last_ord=2, text="Second turn copied excerpt"),
            ],
        }
    )
    s_lines = two_search.splitlines()
    s_item2_idx = next(i for i, ln in enumerate(s_lines) if ln.startswith("[2] "))
    s_item2_with_header = "\n".join(s_lines[s_item2_idx:])
    assert tg.UNTRUSTED_BANNER not in s_item2_with_header
    assert "[QUOTED UNTRUSTED TRANSCRIPT EVIDENCE " in s_item2_with_header
    with pytest.raises(node.Refused, match="shared/transcript-context"):
        sendguard.admit(
            sf,
            to=["kxxx"],
            cc=None,
            body=s_item2_with_header,
            subject=None,
            bulk_ack="attempted single-hit override",
            refused=node.Refused,
        )
    rc_s2 = cli.main(
        [
            "send",
            "--base-url",
            base_url,
            "--state-file",
            str(sf),
            "--to",
            "kxxx",
            "--thread",
            "t-sec",
            "--body",
            s_item2_with_header,
            "--bulk-ack",
            "override",
            "--no-recipient-check",
            "--no-card-check",
        ]
    )
    assert rc_s2 == 2 and json.loads(capsys.readouterr().err)["code"] == "E_CLIENT_REFUSED"
    assert len(sent_calls) == 1

    # Control on search item 2: same block with the 2-line item header removed passes
    s_item2_body_only = "\n".join(s_lines[s_item2_idx + 2 :])
    assert "Second turn copied excerpt" in s_item2_body_only
    admitted_s2_ctrl, _ = sendguard.admit(
        sf, to=["kxxx"], cc=None, body=s_item2_body_only, subject=None, bulk_ack=None, refused=node.Refused
    )
    assert admitted_s2_ctrl == s_item2_body_only

    k_read_1 = f"ctx/kxxx/{SRC_HEX}/0001/000001/0000000000"
    k_read_2 = f"ctx/kxxx/{SRC_HEX}/0001/000001/0000000001"
    two_read = tg.format_transcript_read(
        {
            "namespace": "shared/transcript-context",
            "present_count": 2,
            "missing_count": 0,
            "items": [
                {
                    "key": k_read_1,
                    "status": "present",
                    "seat": "kxxx",
                    "role": "user",
                    "time": "2026-10-26T08:00:00.000Z",
                    "text": "First read turn",
                    "value": {"kind": "item", "item": "user_text"},
                    "author_signature_verified": True,
                },
                {
                    "key": k_read_2,
                    "status": "present",
                    "seat": "kxxx",
                    "role": "assistant",
                    "time": "2026-10-26T08:00:05.000Z",
                    "text": "Second read turn copied under banner",
                    "value": {"kind": "item", "item": "assistant_text"},
                    "author_signature_verified": True,
                },
            ],
        }
    )
    r_lines = two_read.splitlines()
    r_item2_idx = next(i for i, ln in enumerate(r_lines) if ln.startswith(f"[{k_read_2}] "))
    r_item2_with_header = "\n".join(r_lines[r_item2_idx:])
    assert tg.UNTRUSTED_BANNER not in r_item2_with_header
    assert f"[QUOTED UNTRUSTED TRANSCRIPT EVIDENCE {k_read_2}]" in r_item2_with_header
    with pytest.raises(node.Refused, match="shared/transcript-context"):
        sendguard.admit(
            sf,
            to=["kxxx"],
            cc=None,
            body=r_item2_with_header,
            subject=None,
            bulk_ack="attempted single-item override",
            refused=node.Refused,
        )
    rc_r2 = cli.main(
        [
            "send",
            "--base-url",
            base_url,
            "--state-file",
            str(sf),
            "--to",
            "kxxx",
            "--thread",
            "t-sec",
            "--body",
            r_item2_with_header,
            "--bulk-ack",
            "override",
            "--no-recipient-check",
            "--no-card-check",
        ]
    )
    assert rc_r2 == 2 and json.loads(capsys.readouterr().err)["code"] == "E_CLIENT_REFUSED"
    assert len(sent_calls) == 1

    # Control on read item 2: same block with the 2-line item header removed passes
    r_item2_body_only = "\n".join(r_lines[r_item2_idx + 2 :])
    assert "Second read turn copied under banner" in r_item2_body_only
    admitted_r2_ctrl, _ = sendguard.admit(
        sf, to=["kxxx"], cc=None, body=r_item2_body_only, subject=None, bulk_ack=None, refused=node.Refused
    )
    assert admitted_r2_ctrl == r_item2_body_only
    rc_ctrl_send = cli.main(
        [
            "send",
            "--base-url",
            base_url,
            "--state-file",
            str(sf),
            "--to",
            "kxxx",
            "--thread",
            "t-sec",
            "--body",
            r_item2_body_only,
            "--no-recipient-check",
            "--no-card-check",
        ]
    )
    capsys.readouterr()
    assert rc_ctrl_send == 0
    assert len(sent_calls) == 2


def test_read_transcript_items_require_signature_and_cli_exit_codes(monkeypatch, tmp_path, capsys):
    assert "E_AUTHOR_UNVERIFIED" in tg.__all__ and tg.E_AUTHOR_UNVERIFIED == "E_AUTHOR_UNVERIFIED"
    base_url = "http://127.0.0.1:6310"
    sf = _write_state_file(tmp_path, base_url)
    ns = tg.DEFAULT_TRANSCRIPT_NAMESPACE

    seed_good, seed_wrong = b"\x11" * 32, b"\x22" * 32
    pub_good = wire.public_key_of(seed_good)
    kid_good = wire.peer_kid(pub_good)
    jwks_good = ({"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": kid_good, "x": wire.b64u(pub_good)}]}, None)
    monkeypatch.setattr(node, "author_keys", lambda url, peer: jwks_good if peer == PEER_A else (None, "no keys"))

    def signed_memo_resp(key: str, stored_val: dict, *, sign_val: dict | None = None, seed: bytes = seed_good,
                         kid: str = kid_good, omit_sig: bool = False, srv_sig: bool = True) -> dict:
        payload = {
            "kind": wire.KIND_SKILL_CALL, "skill_name": wire.WRITE_SKILL, "caller_peer_id": PEER_A,
            "args": {"namespace": ns, "key": key, "value": stored_val if sign_val is None else sign_val},
        }
        prov = {} if omit_sig else {
            "payload": payload, "signature": wire.b64u(wire.sign(seed, wire.jcs(payload))), "signing_kid": kid,
        }
        return {
            "call_id": f"call-{key[-3:]}", "status": "completed", "result_signature_verified": srv_sig,
            "result": {"ok": True, "op": "memory.read", "memo": {
                "namespace": ns, "key": key, "version": 1, "is_head": True,
                "created_at": "2026-10-27 08:00:00+00:00", "created_by_peer_id": PEER_A,
                "author_label": "kxxx", "provenance": prov, "value": stored_val,
            }},
        }

    def item_val(ordinal: int, text: str) -> dict:
        return {"schema": "context.v1", "kind": "item", "seat": "kxxx", "src": SRC_HEX, "gen": 1, "batch": 1,
                "ordinal": ordinal, "item": "user_text", "role": "user", "time": "2026-10-27T08:00:00.000Z", "text": text}

    keys = [f"ctx/kxxx/{SRC_HEX}/0001/000001/{i:010d}" for i in range(7)]
    k_ok, k_tampered, k_wrong_key, k_unk_kid, k_no_sig, k_bad_srv, k_missing = keys
    responses = {
        k_ok: signed_memo_resp(k_ok, item_val(0, "VERIFIED_TURN_TEXT")),
        k_tampered: signed_memo_resp(k_tampered, item_val(1, "FORGED_TAMPERED_SECRET"), sign_val=item_val(1, "ORIG")),
        k_wrong_key: signed_memo_resp(k_wrong_key, item_val(2, "WRONG_KEY_SECRET"), seed=seed_wrong),
        k_unk_kid: signed_memo_resp(k_unk_kid, item_val(3, "UNKNOWN_KID_SECRET"), kid="kid-rotated"),
        k_no_sig: signed_memo_resp(k_no_sig, item_val(4, "UNSIGNED_SECRET"), omit_sig=True),
        k_bad_srv: signed_memo_resp(k_bad_srv, item_val(5, "BAD_SERVER_SIG_SECRET"), srv_sig=False),
        k_missing: {"call_id": "call-miss", "status": "failed", "result_signature_verified": True,
                    "error_code": "E_UNKNOWN_KEY", "error_message": "unknown key", "result": None},
    }
    monkeypatch.setattr(node, "invoke", lambda path, state, *, skill, args: responses[args["key"]])

    # (a) Valid server sig + valid Ed25519 author sig with --require-signature exits 0
    assert cli.main(["transcript-read", "--base-url", base_url, "--state-file", str(sf), "--key", k_ok, "--require-signature"]) == 0
    ok_out = capsys.readouterr().out
    assert "author_signature_verified=true" in ok_out and "VERIFIED_TURN_TEXT" in ok_out

    # (b) Tampered server sig exits 1 with E_RESULT_UNVERIFIED and emits no quote text
    assert cli.main(["transcript-read", "--base-url", base_url, "--state-file", str(sf), "--key", k_bad_srv, "--require-signature"]) == 1
    srv_out = capsys.readouterr().out
    assert json.loads(srv_out)["error"] == node.E_RESULT_UNVERIFIED and "BAD_SERVER_SIG_SECRET" not in srv_out

    # (c) Invalid author sig: default exits 0; --require-signature exits 1 with E_AUTHOR_UNVERIFIED and no quote text
    assert cli.main(["transcript-read", "--base-url", base_url, "--state-file", str(sf), "--key", k_tampered]) == 0
    assert "author_signature_verified=false" in capsys.readouterr().out

    for bad_key, secret_marker in (
        (k_tampered, "FORGED_TAMPERED_SECRET"),
        (k_wrong_key, "WRONG_KEY_SECRET"),
        (k_unk_kid, "UNKNOWN_KID_SECRET"),
        (k_no_sig, "UNSIGNED_SECRET"),
    ):
        rc = cli.main(["transcript-read", "--base-url", base_url, "--state-file", str(sf), "--key", bad_key, "--require-signature"])
        captured = capsys.readouterr()
        assert rc == 1
        err_doc = json.loads(captured.out)
        assert err_doc["error"] == tg.E_AUTHOR_UNVERIFIED and err_doc["key"] == bad_key
        assert err_doc["status"] == "present" and err_doc["author_signature_verified"] is False
        assert err_doc["unverified_keys"] == [bad_key]
        assert secret_marker not in captured.out and secret_marker not in captured.err

    # Span ord 0..2 where ord 0 is valid and ord 1..2 fail: raises before emitting any quote text
    rc_span = cli.main(["transcript-read", "--base-url", base_url, "--state-file", str(sf), "--seat", "kxxx",
                        "--src", SRC_HEX, "--gen", "1", "--batch", "1", "--first-ord", "0", "--last-ord", "2", "--require-signature"])
    span_out = capsys.readouterr().out
    assert rc_span == 1 and json.loads(span_out)["unverified_keys"] == [k_tampered, k_wrong_key]
    assert "VERIFIED_TURN_TEXT" not in span_out and "FORGED_TAMPERED_SECRET" not in span_out

    # (d) E_UNKNOWN_KEY missing row with --require-signature exits 1 with E_AUTHOR_UNVERIFIED
    rc_miss = cli.main(["transcript-read", "--base-url", base_url, "--state-file", str(sf), "--key", k_missing, "--require-signature"])
    miss_err = json.loads(capsys.readouterr().out)
    assert rc_miss == 1 and miss_err["error"] == tg.E_AUTHOR_UNVERIFIED
    assert miss_err["status"] == "missing" and miss_err["unverified_keys"] == [k_missing]


def test_per_seat_transcript_freshness_discovers_hidden_seat_and_refines_since(monkeypatch, tmp_path):
    assert "DEFAULT_STALE_TRANSCRIPT_HOURS" in tg.__all__ and tg.DEFAULT_STALE_TRANSCRIPT_HOURS == 6.0
    now = datetime(2026, 10, 26, 2, 0, 0, tzinfo=timezone.utc)
    sf, st = tmp_path / "s.json", _minted_state()

    for bad_hours in (0, -1.5, True, float("nan"), float("inf")):
        with pytest.raises(node.Refused, match="--stale-transcript-hours"):
            tg.fleet_transcript_consensus(sf, st, stale_transcript_hours=bad_hours, now=now)  # type: ignore[arg-type]

    card_rows = [
        {"key": "card/antigravity/" + "1" * 32, "id": "1" * 32, "seat": "antigravity", "type": "Context", "status": "active",
         "time": "2026-10-26T01:45:00.000Z", "written_at": "2026-10-26T01:45:00.000Z", "text": "Context | owner: antigravity\nAuditing lag"},
        {"key": "card/kxxx/" + "2" * 32, "id": "2" * 32, "seat": "kxxx", "type": "Context", "status": "active",
         "time": "2026-10-26T01:50:00.000Z", "written_at": "2026-10-26T01:50:00.000Z", "text": "Context | owner: kxxx\nReviewing RRF"},
    ]
    monkeypatch.setattr(node, "cards", lambda *a, **kw: {"namespace": "shared/decisions", "cards": card_rows, "next": None})

    def fake_search(path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None):
        f_map = dict(card_filter or ())
        seat_f, since_f = f_map.get("seat"), f_map.get("since")
        assert since_f is not None and cards.TIME_RE.fullmatch(since_f)
        if f_map.get("collapse") == "session":
            assert since_f == "2026-10-25T14:00:00.000Z"
            hits = [{**_sample_hit(seat="kxxx", batch=4, text="kxxx session"), "time_last": "2026-10-26T01:40:00.000Z"}]
            if k >= 2:
                hits.append({**_sample_hit(seat="sxxxxxxxx", batch=2, text="sxxxxxxxx upload"), "time_last": "2026-10-26T01:30:00.000Z"})
            return {"namespace": ns, "collapse": "session", "gate_open": True, "index_behind": False,
                    "coverage": {"path": "exact", "subset": 900, "subset_at_least": False, "scored": 900}, "hits": hits}
        init_seat_since = "2026-10-23T02:00:00.000Z"
        if seat_f == "antigravity":
            if since_f == init_seat_since:
                hits = [{**_sample_hit(seat="antigravity", gen=1, batch=6, text="older"), "time_last": "2026-10-25T12:07:00.000Z"}]
            elif since_f == "2026-10-25T12:07:00.001Z":
                hits = [{**_sample_hit(seat="antigravity", gen=1, batch=9, text="mid"), "time_last": "2026-10-25T20:00:00.000Z"}]
            elif since_f == "2026-10-25T20:00:00.001Z":
                hits = [{**_sample_hit(seat="antigravity", gen=1, batch=10, text="newest secret-token"), "time_last": "2026-10-26T01:35:00.000Z"}]
            else:
                hits = []
            sub_val = 678 if since_f == init_seat_since else len(hits)
            return {"namespace": ns, "collapse": "chunk", "gate_open": True, "index_behind": False,
                    "coverage": {"path": "exact", "subset": sub_val, "subset_at_least": False, "scored": sub_val}, "hits": hits}
        if seat_f in ("kxxx", "sxxxxxxxx"):
            t_last = "2026-10-26T01:40:00.000Z" if seat_f == "kxxx" else "2026-10-26T01:30:00.000Z"
            sub_n = 200 if seat_f == "kxxx" else 22
            is_init = since_f == init_seat_since
            hits = [{**_sample_hit(seat=seat_f, batch=4), "time_last": t_last}] if is_init else []
            eff_sub = sub_n if is_init else len(hits)
            return {"namespace": ns, "collapse": "chunk", "gate_open": True,
                    "coverage": {"path": "exact", "subset": eff_sub, "subset_at_least": False, "scored": eff_sub}, "hits": hits}
        return {"namespace": ns, "collapse": "chunk", "gate_open": True,
                "coverage": {"path": "exact", "subset": 0, "subset_at_least": False, "scored": 0}, "hits": []}

    monkeypatch.setattr(node, "search", fake_search)
    rep = tg.fleet_transcript_consensus(sf, st, now=now)

    assert rep["transcript_seat_count"] == 3
    by_seat = {e["seat"]: e for e in rep["seats"]}
    assert set(by_seat) == {"antigravity", "kxxx", "sxxxxxxxx"}
    ag_tx = by_seat["antigravity"]["latest_transcript"]
    assert ag_tx is not None and ag_tx["batch"] == 10 and ag_tx["time_last"] == "2026-10-26T01:35:00.000Z"
    assert ag_tx["chunk_count"] == 678 and ag_tx["stale"] is False
    assert by_seat["antigravity"]["indexed_transcript_hits"]["count"] == 1
    assert by_seat["antigravity"]["indexed_transcript_hits"]["chunk_count"] == 678
    assert by_seat["antigravity"]["indexed_transcript_hits"]["latest"]["batch"] == 10
    assert "secret-token" not in json.dumps(rep)


def test_consensus_transcript_lag_alerts_card_lag_and_cli_flag(monkeypatch, tmp_path, capsys):
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    base_url = "http://127.0.0.1:6310"
    sf, st = _write_state_file(tmp_path, base_url), _minted_state(base_url)

    card_rows = [
        {"key": "card/antigravity/" + "1" * 32, "id": "1" * 32, "seat": "antigravity", "type": "Context", "status": "active",
         "time": "2026-10-27T11:30:00.000Z", "written_at": "2026-10-27T11:30:00.000Z", "text": "Context | owner: antigravity\nActive"},
        {"key": "card/oxxxxxx/" + "2" * 32, "id": "2" * 32, "seat": "oxxxxxx", "type": "Candidate", "status": "active",
         "time": "2026-10-25T20:00:00.000Z", "written_at": "2026-10-25T20:00:00.000Z", "text": "Candidate | owner: operator\nOld"},
        {"key": "card/kxxx/" + "3" * 32, "id": "3" * 32, "seat": "kxxx", "type": "Context", "status": "active",
         "time": "2026-10-27T11:30:00.000Z", "written_at": "2026-10-27T11:30:00.000Z", "text": "Context | owner: kxxx\nFresh"},
        {"key": "card/dxxxxx/" + "4" * 32, "id": "4" * 32, "seat": "dxxxxx", "type": "Context", "status": "active",
         "time": "2026-10-27T11:45:00.000Z", "written_at": "2026-10-27T11:45:00.000Z", "text": "Context | owner: dxxxxx\nCards only"},
    ]
    monkeypatch.setattr(node, "cards", lambda *a, **kw: {"namespace": "shared/decisions", "cards": card_rows, "next": None})

    dxxxxx_calls = 0

    init_seat_since = "2026-10-24T12:00:00.000Z"

    def fake_search(path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None):
        nonlocal dxxxxx_calls
        f_map = dict(card_filter or ())
        seat_f, since_f = f_map.get("seat"), f_map.get("since")
        assert since_f is not None and cards.TIME_RE.fullmatch(since_f)
        if f_map.get("collapse") == "session":
            assert since_f == "2026-10-27T00:00:00.000Z"
            return {"namespace": ns, "collapse": "session", "gate_open": True, "index_behind": False,
                    "coverage": {"path": "exact", "subset": 300, "subset_at_least": False, "scored": 300},
                    "hits": [{**_sample_hit(seat="kxxx", batch=8), "time_last": "2026-10-27T06:00:00.000Z"}]}
        per_seat = {
            "antigravity": (120, 1, 10, "2026-10-27T02:00:00.000Z", "raw-secret"),
            "oxxxxxx": (40, 2, 5, "2026-10-26T21:00:00.000Z", "hz-text"),
            "kxxx": (140, 1, 8, "2026-10-27T06:00:00.000Z", "kxxx-text"),
        }
        if seat_f in per_seat:
            sub_n, gen_n, batch_n, t_last, txt_body = per_seat[seat_f]
            is_init = since_f == init_seat_since
            hits = [{**_sample_hit(seat=seat_f, gen=gen_n, batch=batch_n, text=txt_body), "time_last": t_last}] if is_init else []
            eff_sub = sub_n if is_init else len(hits)
            return {"namespace": ns, "collapse": "chunk", "gate_open": True,
                    "coverage": {"path": "exact", "subset": eff_sub, "subset_at_least": False, "scored": eff_sub}, "hits": hits}
        if seat_f == "dxxxxx":
            dxxxxx_calls += 1
        return {"namespace": ns, "collapse": "chunk", "gate_open": True,
                "coverage": {"path": "exact", "subset": 0, "subset_at_least": False, "scored": 0}, "hits": []}

    monkeypatch.setattr(node, "search", fake_search)
    rep = tg.fleet_transcript_consensus(
        sf, st, stale_transcript_hours=6.0, stale_ingest_hours=36.0,
        upload_modes={"antigravity": "continuous", "kxxx": "one_off"}, now=now,
    )
    assert dxxxxx_calls == 1 and rep["stale_transcript_hours"] == 6.0 and rep["transcript_seat_count"] == 3
    by_seat = {e["seat"]: e for e in rep["seats"]}

    assert by_seat["antigravity"]["health"] == "attention" and by_seat["antigravity"]["latest_transcript"]["stale"] is True
    assert by_seat["antigravity"]["latest_transcript"]["upload_mode"] == "continuous"
    assert by_seat["antigravity"]["latest_transcript"]["age_hours"] == 10.0
    assert by_seat["antigravity"]["latest_transcript"]["lag_vs_card_hours"] == 9.5
    assert by_seat["oxxxxxx"]["health"] == "attention" and by_seat["oxxxxxx"]["latest_card"]["stale"] is True
    assert by_seat["oxxxxxx"]["latest_transcript"]["stale"] is True
    assert by_seat["oxxxxxx"]["latest_transcript"]["upload_mode"] == "unknown"
    assert by_seat["kxxx"]["health"] == "ok" and by_seat["kxxx"]["latest_transcript"]["stale"] is False
    assert by_seat["kxxx"]["latest_transcript"]["upload_mode"] == "one_off"
    assert by_seat["kxxx"]["latest_transcript"]["age_hours"] == 6.0 and by_seat["kxxx"]["latest_transcript"]["lag_vs_card_hours"] == 5.5
    assert by_seat["dxxxxx"]["health"] == "ok" and by_seat["dxxxxx"]["latest_transcript"] is None

    lag_alerts = {a["seat"]: a for a in rep["alerts"] if a["kind"] == "transcript_lag"}
    assert set(lag_alerts) == {"antigravity", "oxxxxxx"}
    assert lag_alerts["antigravity"]["key"] == f"ctxm/antigravity/{SRC_HEX}/0001/000010"
    assert lag_alerts["antigravity"]["upload_mode"] == "continuous"
    assert "lags latest card by 9.5h" in lag_alerts["antigravity"]["detail"]
    assert any(a["kind"] == "stale_card_ingestion" and a["seat"] == "oxxxxxx" for a in rep["alerts"])

    txt = tg.format_consensus_report(rep)
    assert len(txt.splitlines()) <= 78
    assert "tx=10.0h(lag,continuous)" in txt and "tx=6.0h(one_off) " in txt and "tx=none " in txt
    assert "raw-secret" not in txt and "\u2014" not in txt and "\u2013" not in txt

    # Regression (nxxxxx e6c9a4): a 9.5h-old one_off seat remains visible and flagged in alerts without becoming ATTN
    per_seat_oneoff = dict(antigravity=(120, 1, 10, "2026-10-27T11:30:00.000Z", "ok"),
                           oxxxxxx=(40, 2, 5, "2026-10-27T11:30:00.000Z", "ok"),
                           kxxx=(140, 1, 8, "2026-10-27T02:30:00.000Z", "kxxx-oneoff"))
    monkeypatch.setattr(
        node, "search",
        lambda path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None: (
            {"namespace": ns, "collapse": "session", "gate_open": True, "index_behind": False,
             "coverage": {"path": "exact", "subset": 300, "subset_at_least": False, "scored": 300}, "hits": []}
            if dict(card_filter or ()).get("collapse") == "session"
            else (
                {"namespace": ns, "collapse": "chunk", "gate_open": True,
                 "coverage": {"path": "exact",
                              "subset": per_seat_oneoff[dict(card_filter or ())["seat"]][0] if dict(card_filter or ()).get("since") == init_seat_since else 0,
                              "subset_at_least": False, "scored": 0},
                 "hits": [
                     {**_sample_hit(seat=dict(card_filter or ())["seat"], gen=per_seat_oneoff[dict(card_filter or ())["seat"]][1],
                                    batch=per_seat_oneoff[dict(card_filter or ())["seat"]][2],
                                    text=per_seat_oneoff[dict(card_filter or ())["seat"]][4]),
                      "time_last": per_seat_oneoff[dict(card_filter or ())["seat"]][3]}
                 ] if dict(card_filter or ()).get("since") == init_seat_since else []}
                if dict(card_filter or ()).get("seat") in per_seat_oneoff
                else {"namespace": ns, "collapse": "chunk", "gate_open": True,
                      "coverage": {"path": "exact", "subset": 0, "subset_at_least": False, "scored": 0}, "hits": []}
            )
        ),
    )
    # The CLI passes no `now`, so it read the real clock against rows dated 26 and 27 Oct: from
    # about 29 Oct 00:00Z the 36 h ingest threshold had passed and kxxx came back "attention"
    # (every full gate refused here from then on). Pin the CLI's call to this test's `now`, as
    # the direct calls below already are.
    real_consensus = tg.fleet_transcript_consensus
    monkeypatch.setattr(tg, "fleet_transcript_consensus",
                        lambda *a, **kw: real_consensus(*a, **{**kw, "now": kw.get("now") or now}))
    assert cli.main(["transcript-consensus", "--base-url", base_url, "--state-file", str(sf),
                     "--stale-transcript-hours", "6", "--upload-mode", "kxxx=one_off", "--json"]) == 0
    oneoff_rep = json.loads(capsys.readouterr().out)
    kxxx_entry = {e["seat"]: e for e in oneoff_rep["seats"]}["kxxx"]
    assert kxxx_entry["health"] == "ok"
    assert kxxx_entry["latest_transcript"]["stale"] is True and kxxx_entry["latest_transcript"]["upload_mode"] == "one_off"
    assert any(a["kind"] == "transcript_lag" and a["seat"] == "kxxx" and a["upload_mode"] == "one_off" for a in oneoff_rep["alerts"])
    oneoff_txt = tg.format_consensus_report(oneoff_rep)
    assert "[OK] kxxx:" in oneoff_txt and "(lag,one_off)" in oneoff_txt
    pinned_rep = tg.fleet_transcript_consensus(
        sf, st, stale_transcript_hours=6.0, upload_modes={"kxxx": "one_off"}, now=now
    )
    assert "[OK] kxxx:" in tg.format_consensus_report(pinned_rep)
    assert "tx=9.5h(lag,one_off)" in tg.format_consensus_report(pinned_rep)

    assert cli.main(["transcript-consensus", "--base-url", base_url, "--state-file", str(sf), "--upload-mode", "kxxx=bogus"]) != 0
    assert "--upload-mode" in capsys.readouterr().err


def test_consensus_preserves_uncertainty_on_refinement_cap_and_over_32_seat_probe_cap(monkeypatch, tmp_path):
    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    sf, st = tmp_path / "s.json", _minted_state()
    seat_names = [f"seat-{i:02d}" for i in range(33)] + ["zulu-overflow"]
    card_rows = [
        {"key": f"card/{s}/" + f"{i:032x}", "id": f"{i:032x}", "seat": s, "type": "Context", "status": "active",
         "time": "2026-10-27T11:50:00.000Z", "written_at": "2026-10-27T11:50:00.000Z", "text": f"Context | owner: {s}\nok"}
        for i, s in enumerate(seat_names, 1)
    ]
    monkeypatch.setattr(node, "cards", lambda *a, **kw: {"namespace": "shared/decisions", "cards": card_rows, "next": None})

    def fake_search(path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None):
        f_map = dict(card_filter or ())
        seat_f, since_f = f_map.get("seat"), f_map.get("since")
        assert since_f is not None and cards.TIME_RE.fullmatch(since_f)
        if f_map.get("collapse") == "session":
            return {
                "namespace": ns, "collapse": "session", "gate_open": True, "index_behind": False,
                "coverage": {"path": "exact", "subset": 500, "subset_at_least": False, "scored": 500},
                "hits": [{**_sample_hit(seat="zulu-overflow", batch=2), "time_last": "2026-10-26T20:00:00.000Z"}],
            }
        if seat_f == "seat-00":
            # Full k=20 relevance pages advancing on every step without exhausting older chunks -> incomplete!
            step_times = {
                "2026-10-24T12:00:00.000Z": "2026-10-26T10:00:00.000Z",
                "2026-10-26T10:00:00.001Z": "2026-10-26T11:00:00.000Z",
                "2026-10-26T11:00:00.001Z": "2026-10-26T12:00:00.000Z",
                "2026-10-26T12:00:00.001Z": "2026-10-26T13:00:00.000Z",
                "2026-10-26T13:00:00.001Z": "2026-10-26T14:00:00.000Z",
            }
            assert since_f in step_times
            t_val = step_times[since_f]
            hits = [{**_sample_hit(seat="seat-00", batch=b), "time_last": t_val} for b in range(1, 21)]
            return {"namespace": ns, "collapse": "chunk", "gate_open": True,
                    "coverage": {"path": "exact", "subset": 120, "subset_at_least": False, "scored": 120}, "hits": hits}
        return {"namespace": ns, "collapse": "chunk", "gate_open": True,
                "coverage": {"path": "exact", "subset": 0, "subset_at_least": False, "scored": 0}, "hits": []}

    monkeypatch.setattr(node, "search", fake_search)
    rep = tg.fleet_transcript_consensus(sf, st, stale_transcript_hours=6.0, now=now)
    by_seat = {e["seat"]: e for e in rep["seats"]}

    # seat-00 hit refinement cap with 20-hit pages -> freshness_complete=False, stale=None, no false transcript_lag alert
    s0_tx = by_seat["seat-00"]["latest_transcript"]
    assert s0_tx is not None and s0_tx["freshness_complete"] is False and s0_tx["stale"] is None
    assert not any(a["kind"] == "transcript_lag" and a["seat"] == "seat-00" for a in rep["alerts"])

    # zulu-overflow (> 32 probe cap) retains its session hit, counts in transcript_seat_count, and marks stale=None
    z_tx = by_seat["zulu-overflow"]["latest_transcript"]
    assert z_tx is not None and z_tx["batch"] == 2 and z_tx["freshness_complete"] is False and z_tx["stale"] is None
    assert by_seat["seat-01"]["latest_transcript"] is None
    assert rep["transcript_seat_count"] == 2
    txt = tg.format_consensus_report(rep)
    assert "tx=22.0h(unknown)" in txt


def test_a_search_with_hits_says_to_read_the_full_items_before_relying_on_one(monkeypatch, tmp_path):
    # Value test set A (vxxxxxxxxxx ea9acc, kxxx 2f378d): both misses chose between similar hits from the quote alone.
    monkeypatch.setattr(node, "search", lambda *a, **kw: {"namespace": tg.DEFAULT_TRANSCRIPT_NAMESPACE,
                                                          "hits": [_sample_hit()]})
    res = tg.search_transcripts(_write_state_file(tmp_path), _minted_state(), q="anything")
    assert res["reading_hint"] == tg.READ_HINT
    assert tg.READ_HINT in tg.format_transcript_search(res)
    monkeypatch.setattr(node, "search", lambda *a, **kw: {"namespace": tg.DEFAULT_TRANSCRIPT_NAMESPACE, "hits": []})
    empty = tg.search_transcripts(_write_state_file(tmp_path), _minted_state(), q="anything")
    assert empty["reading_hint"] is None and tg.READ_HINT not in tg.format_transcript_search(empty), "no hits, no hint"


def test_consensus_bounded_probes_roundtrip_caps_and_server_degradation(monkeypatch, tmp_path, capsys):
    assert tg.CONSENSUS_MAX_PAGES == 4 and tg.RECENT_ACTIVITY_MAX_PAGES == 2
    assert tg.DEFAULT_INDEX_PROBE_HOURS == 12.0 and tg.MAX_FLEET_INDEX_PROBE_HOURS == 24.0
    assert tg.DEFAULT_SEAT_PROBE_HOURS == 72.0 and tg.MAX_SEAT_PROBE_HOURS == 168.0
    now = datetime(2026, 11, 5, 8, 0, 0, tzinfo=timezone.utc)
    base_url = "http://127.0.0.1:6310"
    sf, st = _write_state_file(tmp_path, base_url), _minted_state(base_url)

    # 1. Bounded since windows (zero since=None), dormant-seat probe skip, and Step-0 subset exhaustion short-circuit
    active_card = {"key": "card/antigravity/" + "1" * 32, "id": "1" * 32, "seat": "antigravity", "type": "Context",
                   "status": "active", "time": "2026-11-05T07:30:00.000Z", "written_at": "2026-11-05T07:30:00.000Z",
                   "text": "Context | owner: antigravity\nActive"}
    dormant_card = {"key": "card/oldseat/" + "2" * 32, "id": "2" * 32, "seat": "oldseat", "type": "Context",
                    "status": "active", "time": "2026-10-25T00:00:00.000Z", "written_at": "2026-10-25T00:00:00.000Z",
                    "text": "Context | owner: oldseat\nDormant > 72h"}
    card_queries: list[dict] = []
    search_queries: list[dict] = []

    def bounded_cards(path, state, query, **kw):
        q_map = dict(query)
        card_queries.append(q_map)
        after = q_map.get("after")
        nxt = f"{after or 'c'}1"
        return {"namespace": "shared/decisions", "cards": [active_card, dormant_card], "next": nxt}

    def bounded_search(path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None):
        f_map = dict(card_filter or ())
        search_queries.append(f_map)
        since_val = f_map.get("since")
        assert since_val is not None and cards.TIME_RE.fullmatch(since_val)
        since_dt = tg._parse_iso_utc(since_val)
        assert since_dt is not None
        if f_map.get("collapse") == "session" and f_map.get("seat") is None:
            assert (now - since_dt).total_seconds() <= tg.MAX_FLEET_INDEX_PROBE_HOURS * 3600.0
            return {"namespace": ns, "collapse": "session", "gate_open": True, "index_behind": False,
                    "coverage": {"path": "exact", "subset": 1, "subset_at_least": False, "scored": 1}, "hits": []}
        assert (now - since_dt).total_seconds() <= tg.MAX_SEAT_PROBE_HOURS * 3600.0
        # subset=1 == len(hits) and subset_at_least=False -> Step 0 proves completeness in 1 call (no Step 1)!
        return {"namespace": ns, "collapse": "chunk", "gate_open": True, "index_behind": False,
                "coverage": {"path": "exact", "subset": 1, "subset_at_least": False, "scored": 1, "truncated": False},
                "hits": [{**_sample_hit(seat=f_map["seat"], batch=9), "time_last": "2026-11-05T07:45:00.000Z"}]}

    monkeypatch.setattr(node, "cards", bounded_cards)
    monkeypatch.setattr(node, "search", bounded_search)
    rep_ok = tg.fleet_transcript_consensus(sf, st, stale_transcript_hours=9999.0, stale_ingest_hours=9999.0, now=now)
    # 4 active pages + 2 recorded_since pages + 1 general status=any page = 7 sweep pages max (no stale seats at 9999h)
    assert len(card_queries) == 7 and rep_ok["incomplete"] is False and rep_ok["cards_available"] is True
    # Reset with normal thresholds: dormant >72h oldseat must NOT be searched; antigravity finishes in 1 chunk search!
    card_queries.clear()
    search_queries.clear()
    monkeypatch.setattr(node, "cards", lambda *a, **kw: {"namespace": "shared/decisions", "cards": [active_card, dormant_card], "next": None})
    rep_norm = tg.fleet_transcript_consensus(sf, st, now=now)
    probed_seats = [q.get("seat") for q in search_queries if q.get("collapse") != "session"]
    assert probed_seats == ["antigravity"]  # 0 searches for dormant oldseat, and only 1 step for antigravity!
    by_s = {e["seat"]: e for e in rep_norm["seats"]}
    assert by_s["antigravity"]["latest_transcript"]["freshness_complete"] is True
    assert by_s["oldseat"]["latest_card"]["stale"] is True and by_s["oldseat"]["latest_transcript"] is None

    # Direct helper fallbacks also enforce non-None bounded since
    assert tg._probe_transcript_index(sf, st, transcript_ns=tg.DEFAULT_TRANSCRIPT_NAMESPACE, query=None, seat_filter=None)["since"] is not None
    assert tg._probe_seat_latest_transcript(sf, st, transcript_ns=tg.DEFAULT_TRANSCRIPT_NAMESPACE, seat_name="antigravity")[3] is None

    # 2. /v1/cards failures (500, 504 HTML w/ em-dash, TimeoutError/DeadlinePassed, BrokenResponse) degrade cleanly
    for card_exc, exp_status in (
        (node.ServerSaid(500, {"error": "E_INTERNAL", "message": "internal error"}), 500),
        (node.ServerSaid(504, "<html>504 Gateway Time-out\u2014nginx</html>"), 504),
        (node.DeadlinePassed("GET /v1/cards timed out after 20s"), None),
        (node.BrokenResponse("incomplete chunked read"), None),
    ):
        c_calls = 0

        def fail_cards(*a, _exc=card_exc, **kw):
            nonlocal c_calls
            c_calls += 1
            raise _exc

        monkeypatch.setattr(node, "cards", fail_cards)
        rep_deg = tg.fleet_transcript_consensus(sf, st, seat_filter="antigravity", now=now)
        assert c_calls == 1  # circuit-breaker: skips _fetch_recent_card_activity after active walk fails!
        assert rep_deg["incomplete"] is True and rep_deg["cards_available"] is False
        assert rep_deg["cards_http_status"] == exp_status and rep_deg["cards_error"]
        assert rep_deg["transcript_seat_count"] == 1 and rep_deg["seats"][0]["seat"] == "antigravity"
        assert any(a["kind"] == "cards_unavailable" and a["http_status"] == exp_status and a["incomplete"] is True for a in rep_deg["alerts"])
        rendered = tg.format_consensus_report(rep_deg)
        assert "cards: shared/decisions | available=false" in rendered
        assert "\u2014" not in rendered and "\u2013" not in rendered

    # CLI exits 0 on degraded /v1/cards 500 in both human and --json modes
    assert cli.main(["transcript-consensus", "--base-url", base_url, "--state-file", str(sf)]) == 0
    assert "available=false" in capsys.readouterr().out
    assert cli.main(["transcript-consensus", "--base-url", base_url, "--state-file", str(sf), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["incomplete"] is True

    # 3. Partial /v1/cards failure on status=any walk or per-seat ingestion probe suppresses false stale_card_ingestion
    stale_cand_1 = {**dormant_card, "seat": "seat-a", "key": "card/seat-a/" + "a" * 32}
    stale_cand_2 = {**dormant_card, "seat": "seat-b", "key": "card/seat-b/" + "b" * 32}
    monkeypatch.setattr(
        node, "cards",
        lambda path, state, query, **kw: (
            {"namespace": "shared/decisions", "cards": [stale_cand_1, stale_cand_2], "next": None}
            if dict(query).get("status") == "active"
            else (_ for _ in ()).throw(node.DeadlinePassed("status=any timed out"))
        ),
    )
    rep_rec_fail = tg.fleet_transcript_consensus(sf, st, now=now)
    assert rep_rec_fail["cards_available"] is False and rep_rec_fail["incomplete"] is True
    assert all(e["latest_card"]["stale"] is None for e in rep_rec_fail["seats"])
    assert not any(a["kind"] == "stale_card_ingestion" for a in rep_rec_fail["alerts"])

    probe_ingest_calls = 0

    def fail_on_seat_probe(path, state, query, **kw):
        nonlocal probe_ingest_calls
        q_map = dict(query)
        if q_map.get("status") == "active":
            return {"namespace": "shared/decisions", "cards": [stale_cand_1, stale_cand_2], "next": None}
        if q_map.get("seat"):
            probe_ingest_calls += 1
            raise node.ServerSaid(502, "Bad Gateway")
        if q_map.get("recorded_since"):
            return {"namespace": "shared/decisions", "cards": [active_card], "next": f"{q_map.get('after') or 'p'}x"}
        return {"namespace": "shared/decisions", "cards": [], "next": None}

    monkeypatch.setattr(node, "cards", fail_on_seat_probe)
    rep_probe_fail = tg.fleet_transcript_consensus(sf, st, now=now)
    assert probe_ingest_calls == 1  # circuit-breaker: seat-b is NOT probed after seat-a 502!
    assert rep_probe_fail["cards_available"] is False and rep_probe_fail["cards_http_status"] == 502
    assert all(e["latest_card"]["stale"] is None for e in rep_probe_fail["seats"] if e["seat"] in ("seat-a", "seat-b"))
    assert not any(a["kind"] == "stale_card_ingestion" for a in rep_probe_fail["alerts"])

    # 4. Mid-refinement / per-seat /v1/mesh/search 500 trips circuit-breaker & preserves step-0 hit with stale=None
    s1_card = {**active_card, "seat": "seat-1", "key": "card/seat-1/" + "3" * 32}
    s2_card = {**active_card, "seat": "seat-2", "key": "card/seat-2/" + "4" * 32}
    monkeypatch.setattr(node, "cards", lambda *a, **kw: {"namespace": "shared/decisions", "cards": [s1_card, s2_card], "next": None})
    tx_calls: list[tuple[str | None, str | None]] = []

    def mid_fail_search(path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None):
        f_map = dict(card_filter or ())
        tx_calls.append((f_map.get("seat"), f_map.get("since")))
        if f_map.get("collapse") == "session":
            return {"namespace": ns, "collapse": "session", "gate_open": True, "index_behind": False,
                    "coverage": {"path": "exact", "subset": 50, "subset_at_least": False, "scored": 50}, "hits": []}
        if len([c for c in tx_calls if c[0] == "seat-1"]) == 1:
            return {"namespace": ns, "collapse": "chunk", "gate_open": True, "index_behind": False,
                    "coverage": {"path": "exact", "subset": 50, "subset_at_least": False, "scored": 20},
                    "hits": [{**_sample_hit(seat="seat-1", batch=5), "time_last": "2026-11-04T12:00:00.000Z"}]}
        raise node.ServerSaid(500, {"error": "E_INTERNAL", "message": "statement timeout"})

    monkeypatch.setattr(node, "search", mid_fail_search)
    rep_mid = tg.fleet_transcript_consensus(sf, st, now=now)
    assert [s for s, _ in tx_calls if s is not None] == ["seat-1", "seat-1"]  # seat-2 skipped after seat-1 500!
    assert rep_mid["incomplete"] is True and rep_mid["transcript_index"]["available"] is False
    assert rep_mid["transcript_index"]["http_status"] == 500
    by_mid = {e["seat"]: e for e in rep_mid["seats"]}
    assert by_mid["seat-1"]["latest_transcript"]["freshness_complete"] is False
    assert by_mid["seat-1"]["latest_transcript"]["stale"] is None
    assert not any(a["kind"] == "transcript_lag" for a in rep_mid["alerts"])

    # 5. Continuous seat whose latest transcript aged past the 72h bounded probe window still flags transcript_lag
    monkeypatch.setattr(node, "cards", lambda *a, **kw: {"namespace": "shared/decisions", "cards": [active_card, dormant_card], "next": None})
    monkeypatch.setattr(
        node, "search",
        lambda path, state, *, q, k=10, rerank=True, ns=None, card_filter=None, purpose=None: {
            "namespace": ns,
            "collapse": dict(card_filter or ()).get("collapse") or "chunk",
            "gate_open": True,
            "index_behind": False,
            "coverage": {"path": "exact", "subset": 0, "subset_at_least": False, "scored": 0},
            "hits": [],
        },
    )
    rep_aged = tg.fleet_transcript_consensus(
        sf, st, stale_transcript_hours=6.0, upload_modes={"antigravity": "continuous", "oldseat": "continuous"}, now=now
    )
    by_aged = {e["seat"]: e for e in rep_aged["seats"]}
    for cont_s in ("antigravity", "oldseat"):
        assert by_aged[cont_s]["health"] == "attention"
        lt_aged = by_aged[cont_s]["latest_transcript"]
        assert lt_aged is not None and lt_aged["stale"] is True and lt_aged["freshness_complete"] is True
        assert lt_aged["upload_mode"] == "continuous" and lt_aged["time_last"] is None
    assert rep_aged["transcript_seat_count"] == 0
    aged_lags = {a["seat"]: a for a in rep_aged["alerts"] if a["kind"] == "transcript_lag"}
    assert set(aged_lags) == {"antigravity", "oldseat"}
    assert aged_lags["antigravity"]["key"] is None and "72h probe window" in aged_lags["antigravity"]["detail"]
    aged_txt = tg.format_consensus_report(rep_aged)
    assert "[ATTN] antigravity:" in aged_txt and "tx=?(lag,continuous)" in aged_txt

    # When stale_transcript_hours exceeds MAX_SEAT_PROBE_HOURS (168h), empty 168h window reports stale=None (not True/False)
    rep_wide = tg.fleet_transcript_consensus(
        sf, st, stale_transcript_hours=200.0, upload_modes={"antigravity": "continuous"}, now=now
    )
    lt_wide = {e["seat"]: e for e in rep_wide["seats"]}["antigravity"]["latest_transcript"]
    assert lt_wide is not None and lt_wide["stale"] is None and lt_wide["freshness_complete"] is False
    assert not any(a["kind"] == "transcript_lag" for a in rep_wide["alerts"])
    assert "tx=?(unknown,continuous)" in tg.format_consensus_report(rep_wide)

    # 6. Auth 401 on /v1/cards or per-seat /v1/mesh/search still raises ServerSaid (CLI exits 1)
    monkeypatch.setattr(node, "cards", lambda *a, **kw: (_ for _ in ()).throw(node.ServerSaid(401, {"message": "expired"})))
    assert cli.main(["transcript-consensus", "--base-url", base_url, "--state-file", str(sf)]) == 1
    capsys.readouterr()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
