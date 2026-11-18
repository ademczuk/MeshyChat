"""Cases that isolate what tests/test_meshsem_graphpilot.py leaves to chance, from a non-author review of #37 by mutation (kxxx, 2026-09-30).

Each test below kills one of the five mutants of graphpilot.py and its command that survived the PR's own file, and each passes on the PR's head:

  P1  the seed is ignored                                         (every entry is sampled in the PR's world, so no seed can change the page)
  P2  one link too many                                           (the PR's world has exactly k other entries for each one)
  P3  the entries are read in no key order                        (a tie between entries is broken by that order, and the page "can be rebuilt exactly")
  P6  a zero vector is kept                                       (it becomes a NaN cosine that sorts anywhere)
  P8  the command's `SET TRANSACTION READ ONLY` line is deleted   (the PR's test applies that statement itself, so it cannot see it missing from the command)

The fixtures and helpers are the PR's own.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_meshsem_embedjob import DSN, Env, app, env, owner  # noqa: E402,F401  (fixtures; django.setup)
from tests.test_meshsem_graphpilot import _links, _page, _world  # noqa: E402
from tests.test_meshsem_graphwalk import _claim, _row, _tick  # noqa: E402

from apps.core import rls  # noqa: E402
from apps.meshsem import __main__ as cli  # noqa: E402
from apps.meshsem import graphpilot, spaces  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402


def test_p1_the_seed_chooses_the_sample():
    entries = [{"id": str(i), "key": f"wiki/k{i:02d}", "text": "x" * (10 + i), "spans": [(0, 10)]} for i in range(12)]
    assert graphpilot._sample(entries, 3, 1) == graphpilot._sample(entries, 3, 1), "control: the same seed gives the same sample"
    assert len({tuple(graphpilot._sample(entries, 3, s)) for s in range(10)}) > 1, "ten seeds gave one sample: the seed changes nothing"


def test_p2_an_entry_lists_exactly_k_links(owner, env):
    ns, _p, _ids = _world(owner, env)                       # four embedded entries: each has three others
    links = _links(_page(owner, env, ns, entries_n=10, k=2))
    assert links and all(len(targets) == 2 for targets in links.values()), links


def test_p3_entries_are_read_in_key_order_whatever_the_chunk_numbers(owner, env):
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    for key in ("wiki/c", "wiki/a", "wiki/b"):
        _row(owner, p, ns, key, {"body_md": f"the words of {key}"})
    assert _tick(owner, ns).pending == 0
    with owner.cursor() as cur:                              # a's only stored chunk is numbered 1: ordering by chunk number alone puts it last, and
        cur.execute(                                         # among equal chunk numbers the order is the table's, which no test can pin
            "UPDATE mesh_embeddings SET chunk_no = 1 WHERE namespace = %s AND src_key = 'wiki/a'", (ns,))
        assert cur.rowcount == 1
    with rls.server_internal(owner):
        entries, _vecs, _owner = graphpilot._vectors(owner, env.space, ns, True)
    assert [e["key"] for e in entries] == ["wiki/a", "wiki/b", "wiki/c"]


def test_p6_a_zero_vector_is_left_out_and_never_reaches_a_cosine(owner, env):
    ns, p, _ids = _world(owner, env)
    zero = _row(owner, p, ns, "wiki/zero", {"body_md": "an entry whose stored vector is all zeros"})
    vec = spaces.encode(np.zeros(db.DIMS, dtype="float32"))
    with owner.cursor() as cur:
        cur.execute("INSERT INTO mesh_embeddings (src_id, src_key, src_version, space, dims, text_sha256, vec, provider_id, recorded_at,"
                    " chunk_no, namespace) VALUES (%s,%s,1,%s,%s,%s,%s,'p',now(),0,%s)",
                    (zero, "wiki/zero", env.space.name, db.DIMS, "0" * 64, vec, ns))
    page = _page(owner, env, ns, entries_n=10, k=10)         # k above the entries there are: every entry lists every other
    assert "cosine nan" not in page, "a zero vector reached a cosine"
    assert all("wiki/zero" not in targets for targets in _links(page).values())


def test_p8_the_command_builds_in_a_read_only_session(owner, env, tmp_path, monkeypatch):
    ns, _p = _claim(owner)

    def write(conn, *args, **kw):                            # stands in for a later edit of build() that writes
        conn.execute("DELETE FROM mesh_lexical WHERE false")
    monkeypatch.setattr(graphpilot, "build", write)
    page = tmp_path / "page.md"
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        cli.main(["--dsn", DSN, "graph-pilot", "--ns", ns, "--out", str(page)])
    assert not page.exists(), "the command wrote its page after a refused write"
