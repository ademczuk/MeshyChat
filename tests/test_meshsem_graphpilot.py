"""The related-entries value pilot page (apps/meshsem/graphpilot.py): entries not chunks, never an entry
with itself, a stratified and reproducible sample, only heads past the gate, and nothing written.
Rows are embedded by the graph walker against the deterministic fake embedder (bag of hashed words),
so entries that share words are genuinely closer."""
from __future__ import annotations

import re

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_meshsem_embedjob import Env, app, env, owner  # noqa: E402,F401  (fixtures; django.setup)
from tests.test_meshsem_graphwalk import _claim, _retire, _row, _tick  # noqa: E402

from apps.core import rls  # noqa: E402
from apps.meshsem import graphpilot  # noqa: E402

PARA = "The weather report covered rain and wind across the valley. " * 30


def _world(owner, env):
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    ids = {
        "lighthouse-a": _row(owner, p, ns, "wiki/lighthouse-a", {"body_md": "lighthouse keeper lens storm night"}),
        "lighthouse-b": _row(owner, p, ns, "wiki/lighthouse-b", {"body_md": "the lighthouse keeper polished the lens"}),
        "bread": _row(owner, p, ns, "wiki/bread", {"body_md": "bread flour yeast oven baking"}),
        "long": _row(owner, p, ns, "wiki/long", {"body_md": PARA + "\n\n" + PARA + "\n\nlighthouse keeper lens"}),
        "gated": _row(owner, p, ns, "wiki/gated", {"body_md": "lighthouse keeper lens"}, policy="forbidden"),
    }
    assert _tick(owner, ns).pending == 0
    return ns, p, ids


def _page(owner, env, ns, **kw):
    with rls.server_internal(owner):
        return graphpilot.build(owner, env.space, ns, **kw)


def _links(page: str) -> dict[str, list[str]]:
    out, cur = {}, None
    for line in page.splitlines():
        m = re.match(r"^## \d+\. `([^`]+)`", line)
        if m:
            cur = m.group(1)
            out[cur] = []
        m = re.match(r"^### \d+\.\d+ to `([^`]+)`", line)
        if m and cur:
            out[cur].append(m.group(1))
    return out


def test_every_embedded_entry_is_sampled_and_never_linked_to_itself(owner, env):
    ns, _p, _ids = _world(owner, env)
    links = _links(_page(owner, env, ns, entries_n=10, k=3))
    assert set(links) == {"wiki/lighthouse-a", "wiki/lighthouse-b", "wiki/bread", "wiki/long"}
    for key, targets in links.items():
        assert key not in targets, f"{key} is linked to itself"
        assert len(targets) == len(set(targets)), f"{key} lists an entry twice (chunks were not collapsed)"


def test_the_closest_entry_comes_first(owner, env):
    ns, _p, _ids = _world(owner, env)
    links = _links(_page(owner, env, ns, entries_n=10, k=3))
    assert links["wiki/lighthouse-a"][0] == "wiki/lighthouse-b"
    assert links["wiki/bread"][-1] != "wiki/bread"


def test_a_forbidden_row_embedded_while_the_gate_was_open_is_not_proposed_once_it_closes(owner, env):
    """The read-time gate: the row HAS vectors (embedded with the gate open), and the page, built with the gate
    closed, must still leave it out. (A first version never embedded it, so it passed with the check removed.)"""
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=True)
    _row(owner, p, ns, "wiki/open-a", {"body_md": "lighthouse keeper lens"})
    _row(owner, p, ns, "wiki/forbidden", {"body_md": "lighthouse keeper lens storm"}, policy="forbidden")
    assert _tick(owner, ns).embedded_rows == 2, "control: both rows have vectors"
    open_page = _page(owner, env, ns, entries_n=10, k=3)
    assert "wiki/forbidden" in open_page, "control: proposed while the gate is open"
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    assert "wiki/forbidden" not in _page(owner, env, ns, entries_n=10, k=3)


def test_a_superseded_version_still_holding_vectors_is_not_shown(owner, env):
    """Between a new version and the walker's next tick, the old version still has vectors. The page reads heads
    only, so the old text is not shown. (A first version ticked in between, so the walker had already deleted them.)"""
    ns, p, ids = _world(owner, env)
    _retire(owner, ids["bread"])
    _row(owner, p, ns, "wiki/bread", {"body_md": "sourdough starter"}, version=2, parent_id=ids["bread"])
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM mesh_embeddings WHERE src_id = %s", (ids["bread"],))
        assert cur.fetchone()[0] > 0, "control: the old version still holds vectors"
    assert "flour yeast" not in _page(owner, env, ns, entries_n=10, k=3)


def test_a_link_shows_the_best_matching_chunk_of_a_long_entry(owner, env):
    """Two entries score by their BEST chunk pair, and the page shows that pair: the long entry's weather
    chunks are not what links it to the lighthouse entry."""
    ns, _p, _ids = _world(owner, env)
    page = _page(owner, env, ns, entries_n=10, k=3)
    heading = re.search(r"^## \d+\. `wiki/lighthouse-a`", page, re.M)
    assert heading, "control: the entry has its own section"
    section = page[heading.end():].split("\n## ", 1)[0]
    link = section.split("to `wiki/long`", 1)[1].split("\n### ", 1)[0]
    related_line = next(l for l in link.splitlines() if l.startswith("> related entry"))
    assert "lighthouse keeper lens" in related_line, related_line          # the matching sentence is shown


def test_the_sample_is_stratified_and_reproducible(owner, env):
    ns, _p, _ids = _world(owner, env)
    first = _page(owner, env, ns, entries_n=3, k=1, seed=7)
    again = _page(owner, env, ns, entries_n=3, k=1, seed=7)
    strip = lambda s: re.sub(r"^Built \S+", "Built", s, flags=re.M)  # noqa: E731
    assert strip(first) == strip(again)
    sampled = list(_links(first))
    assert len(sampled) == 3 and "wiki/long" in sampled, "the longest third is represented"


def test_the_page_changes_nothing(owner, env):
    ns, _p, _ids = _world(owner, env)
    def counts():
        with owner.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM mesh_embeddings), (SELECT count(*) FROM mesh_lexical),"
                        " (SELECT count(*) FROM shared_memory)")
            return cur.fetchone()
    before = counts()
    _page(owner, env, ns)
    assert counts() == before


def test_the_preview_is_cut(owner, env):
    ns, _p, _ids = _world(owner, env)
    page = _page(owner, env, ns, entries_n=10, k=3)
    quoted = [l for l in page.splitlines() if l.startswith("> ")]
    assert quoted and all(len(l) < graphpilot.PREVIEW + 80 for l in quoted)


def test_the_preview_finds_a_match_in_the_middle_of_a_long_chunk():
    filler = "The weather report covered rain and wind across the valley. " * 10
    text = filler + "The lighthouse keeper polished the lens. " + filler
    assert len(text) <= 1500 and len(text) > graphpilot.PREVIEW
    cut = graphpilot._cut(text, "lighthouse keeper lens")
    assert "lighthouse keeper polished the lens" in cut and cut.startswith("[...]") and cut.endswith("[...]")


def test_an_entry_with_many_self_similar_chunks_still_gets_its_links():
    """Review of #37 (Codex, P2): the candidates were the top 50 * k chunk matches, so an entry with 51
    near-identical chunks saw only itself there and got no link at all. Each other entry's best pair is now
    taken over ALL chunk pairs before k is applied."""
    import numpy as np
    dims = 8
    many = np.tile(np.eye(dims)[0], (51, 1))                       # 51 identical chunks of entry 0
    other = np.array([[0.8, 0.6] + [0.0] * (dims - 2)])            # entry 1, one chunk, close but not identical
    vecs = np.vstack([many, other])
    owner = np.array([[0, n] for n in range(51)] + [[1, 0]])
    entries = [{"id": "a"}, {"id": "b"}]
    links = graphpilot.related(entries, vecs, owner, 0, 1)
    assert [j for j, *_ in links] == [1], links
    assert graphpilot.related(entries, vecs, owner, 1, 1)[0][0] == 0


def test_a_long_source_entry_shows_its_own_best_chunk():
    """The best pair is chosen on BOTH sides: a many-chunk source links by its best chunk, not by whichever of
    its chunks the other entry matches worst."""
    import numpy as np
    dims = 8
    e0, e1, e2 = np.eye(dims)[0], np.eye(dims)[1], np.eye(dims)[2]
    vecs = np.vstack([e1, e2, e0,                                    # entry 0: chunks 0, 1 off-topic, chunk 2 on
                      0.9 * e0 + 0.1 * e1])                          # entry 1: one chunk close to entry 0's chunk 2
    vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    owner = np.array([[0, 0], [0, 1], [0, 2], [1, 0]])
    (j, s, mine, theirs), = graphpilot.related([{}, {}], vecs, owner, 0, 1)
    assert (j, mine, theirs) == (1, 2, 0) and s > 0.9


def test_the_command_runs_read_only_as_a_module_entry_point(owner, tmp_path):
    """`python -m apps.meshsem graph-pilot` in a fresh process, as the app role, writes its page; and the session it
    builds in is READ ONLY (kxxx's review of #37): inside the same context, a write is refused."""
    import os
    import subprocess

    import psycopg
    from apps.core import dbconn
    from tests.test_meshsem_embedjob import DSN
    ns, _p = _claim(owner)
    out = tmp_path / "pilot.md"
    env_ = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "DJANGO_SETTINGS_MODULE", "MESHY_DSN")}
    r = subprocess.run([sys.executable, "-m", "apps.meshsem", "--dsn", DSN, "graph-pilot", "--ns", ns, "--out", str(out)],
                       cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, env=env_, timeout=120)
    assert r.returncode == 0 and "graph-pilot: wrote" in r.stdout, (r.stdout, r.stderr[-400:])
    assert out.read_text(encoding="utf-8").startswith(f"# Related entries pilot: {ns}")
    with dbconn.serving_connection(DSN) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with dbconn.request_server_internal(conn) as c:
                c.execute("SET TRANSACTION READ ONLY")
                c.execute("DELETE FROM mesh_lexical WHERE false")
