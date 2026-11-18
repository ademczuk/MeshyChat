"""Recall end to end: synthetic cards written as signed rows, walked, found through GET /v1/mesh/search on the card
namespace, looked up through GET /v1/cards, and scored by tools/transcript_cards/recall/judge.py.

The second half of exxxx-session's gap register item 10. The first half put the judge in the repository
(tests/test_recall_judge.py); this runs it against the server's own search, so a change to the card status rules,
the walker, the search or the card route that loses a card, serves a superseded one, or drops an overruled mark
fails here.

WHAT IT CANNOT SEE, said plainly: the model hosts are the deterministic fakes (tests/fixtures/meshsem_fake.py), whose
embeddings are a bag of hashed words. A question that shares no word with its answer cannot be found here, so the
labels share words with their cards, and recall of a REWORDED question is not measured. That needs the real models,
and it is measured outside the repository against production with the real labels (recall/README.md).

Labels: tools/transcript_cards/recall/synthetic/dev.jsonl (tune against these) and eval.jsonl (never tune against
these). Cards: synthetic/cards.json. Every word is invented.

Run:  .venv/Scripts/python.exe -m pytest tests/test_recall_cards_search.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_cards_routes import _card, deck  # noqa: E402,F401  (fixtures)
from tests.test_meshsem_graphsearch import _bearer, _walk, client, graph, owner, world  # noqa: E402,F401

RECALL = ROOT / "tools" / "transcript_cards" / "recall"
sys.path.insert(0, str(RECALL))
import judge  # noqa: E402

SYNTHETIC = RECALL / "synthetic"
K = 5


def _labels(name: str) -> list[dict]:
    return [json.loads(line) for line in (SYNTHETIC / name).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _write_cards(owner, holder, ns) -> dict[str, str]:
    """{local name: card key}, written in file order so a card that supersedes another comes after it."""
    keys: dict[str, str] = {}
    for c in json.loads((SYNTHETIC / "cards.json").read_text(encoding="utf-8"))["cards"]:
        supersedes = [keys[n] for n in c.get("supersedes", [])] or None
        keys[c["name"]] = _card(owner, holder, ns, c["text"], time=c["time"], card_type=c["type"], seat=c["seat"],
                                supersedes=supersedes)
    return keys


def _catalog(client, reader) -> dict[str, dict]:
    response = client.get("/v1/cards?status=any&limit=200", **_bearer(reader["peer_id"]))
    assert response.status_code == 200, response.content
    body = response.json()
    assert not body.get("next"), "the synthetic set fits one page"
    return {c["key"]: c for c in body["cards"]}


def _run(client, reader, ns, labels: list[dict], catalog: dict[str, dict]) -> dict[str, list[dict]]:
    """{label id: ranked cards}, exactly as a reader asks: default status (active), no type filter."""
    rows = []
    for label in labels:
        response = client.get(f"/v1/mesh/search?q={quote(label['query'])}&ns={ns}&k=10",
                              **_bearer(reader["peer_id"]))
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["filter"]["status"] == "active", body["filter"]
        unknown = [h["key"] for h in body["hits"] if h["key"] not in catalog]
        assert not unknown, f"{label['id']}: search returned keys the card route does not list: {unknown}"
        rows.append({"id": label["id"], "cards": [catalog[h["key"]] for h in body["hits"]]})
    return judge.results_by_id(rows)


def _report_line(report: dict) -> str:
    return json.dumps({k: report[k] for k in ("queries", "passed", "recall_at_k", "mrr_active", "by_reason")})


def test_recall_over_the_card_search(client, owner, deck):
    ns, holder, reader = deck
    keys = _write_cards(owner, holder, ns)
    assert _walk(owner, ns).pending == 0
    catalog = _catalog(client, reader)
    assert set(keys.values()) <= set(catalog), "control: every written card is in the catalog"
    dev, held_out = _labels("dev.jsonl"), _labels("eval.jsonl")
    assert dev and held_out and not {e["id"] for e in dev} & {e["id"] for e in held_out}

    runs = {}
    for name, labels in (("dev", dev), ("eval", held_out)):
        runs[name] = _run(client, reader, ns, labels, catalog)
        report = judge.judge(labels, runs[name], k=K)
        print(name, _report_line(report))
        failed = [p for p in report["per_query"] if not p["passed"]]
        assert not failed, f"{name}: " + json.dumps(failed, default=str)

    # The Claim that repeats the kettle Ruling's words is RETURNED for eval-003 and ranked below the Ruling, so the
    # judge's claim_above rule is exercised end to end and passes on what the server ranks, not because no Claim came.
    kettle = runs["eval"]["eval-003"]
    types = [c["type"] for c in kettle]
    assert "Claim" in types and types.index("Ruling") < types.index("Claim"), types

    # SENSITIVITY: the run above can fail on each thing it checks. Each control changes one input and must turn a
    # passing query red for the expected reason.
    labels = {e["id"]: e for e in dev}
    unmarked = {k: {**c, "overruled_by": []} for k, c in catalog.items()}
    report = judge.judge([labels["dev-001"]], _run(client, reader, ns, [labels["dev-001"]], unmarked), k=K)
    assert report["by_reason"] == {"wrong_status": 1}, _report_line(report)          # the overruled mark is read

    missing = {**labels["dev-003"], "id": "dev-003-missing",
               "expect": [{**labels["dev-003"]["expect"][0], "must_contain": ["a phrase no card holds"]}]}
    report = judge.judge([missing], _run(client, reader, ns, [missing], catalog), k=K)
    assert report["by_reason"].get("active_missing") == 1, _report_line(report)      # a lost card is seen

    # This control edits a copy of the catalog row, so it proves the JUDGE sees a superseded card served as active,
    # not that the server serves one; the server side is held by tests/test_cards_routes.py's status tests.
    served_old = {k: ({**c, "status": "active"} if k == keys["lamp-weekly"] else c) for k, c in catalog.items()}
    any_status = []
    for label in [labels["dev-002"]]:
        response = client.get(f"/v1/mesh/search?q={quote(label['query'])}&ns={ns}&k=10&status=any",
                              **_bearer(reader["peer_id"]))
        assert response.status_code == 200, response.content
        any_status.append({"id": label["id"], "cards": [served_old[h["key"]] for h in response.json()["hits"]]})
    report = judge.judge([labels["dev-002"]], judge.results_by_id(any_status), k=K)
    assert "wrong_status" in report["by_reason"], _report_line(report)               # a superseded card served active
