"""The projection against a real database: rebuildable, incremental, indexed, RLS-bound.

THE THREE DISCIPLINES, each a test rather than a docstring:
  1. discardable   `test_rebuild_is_idempotent` rebuilds twice, edge sets identical.
  2. no other writer  `test_the_projection_is_unreadable_and_unwritable_without_server_internal`
  3. exercised     every test here runs the rebuild or the catch-up; the CLI test runs
                   the entry point as a subprocess.

ISOLATION. Seeds go in on an owner connection inside a transaction that is rolled back,
the same pattern as tests/test_board_projection.py, so the shared dev database keeps its
rows. `rls.server_internal` needs an autocommit connection with no open transaction, and
the projection functions enter it themselves, so THEY are given an autocommit connection
and the seeds are COMMITTED on a second connection with unique labels per test. The
per-run database (tests/conftest.py) makes that safe.

Run:  pytest tests/test_mesh_projection.py -v
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.core import rls                          # noqa: E402
from apps.mesh import audit as A                   # noqa: E402
from apps.mesh import graph as G                   # noqa: E402
from apps.mesh import normalize as N               # noqa: E402
from apps.mesh import projection as P              # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS  # noqa: E402
from tests.fixtures import history_rows  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def owner():
    """Owner connection, AUTOCOMMIT: the projector opens its own transaction and must see
    what this one wrote. Cleanup is by the per-run database, not by rollback."""
    with psycopg.connect(DSN, autocommit=True) as c:
        _ensure_namespace(c)
        yield c


@pytest.fixture()
def app():
    """The role a request actually runs under. Autocommit so server_internal can bind."""
    with psycopg.connect(DSN, autocommit=True) as c:
        rls.set_role(c)
        yield c


def _ensure_namespace(conn) -> None:
    p = _peer(conn, "nsowner")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO shared_memory_namespace"
            " (namespace, owner_peer_id, owner_owner_id, claimant_plane, ephemeral,"
            "  retention_policy) VALUES (%s,%s,%s,'dev',FALSE,'keep_all')"
            " ON CONFLICT DO NOTHING", (NS, p["peer_id"], p["owner_id"]))


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"mesh-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name)"
                    " VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"mesh-{tag}-{peer_id.hex[:8]}", f"mesh {tag}"))
    return {"peer_id": peer_id, "owner_id": owner_id, "label": tag}


def _row(conn, peer, value: dict, *, created_at=None, version: int = 1,
          key: str | None = None, is_head: bool = True,
          rid: uuid.UUID | None = None, parent_id: uuid.UUID | None = None,
          embed_policy: str | None = None) -> str:
    """One mesh row as the daemon would store it. Returns the key.

    `sql/064` enforces the root-parent invariant on INSERT: version 1 must have a NULL
    parent and version > 1 must name its parent. A version-2 seed therefore passes the
    v1 row's id as `parent_id`, which is the real version chain and not a test shortcut.
    """
    rid = rid or uuid.uuid4()
    when = created_at or _dt.datetime.now(_dt.timezone.utc)
    key = key or f"msg/{int(when.timestamp() * 1000)}-{rid.hex}"
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without). A later version of a mail key is
    # seeded as history (divergence 059: tests/fixtures/history_rows.py), as the old rows on production reached it.
    extra = {"embed_policy": embed_policy} if embed_policy is not None else {}
    history_rows.insert_seed(conn, sr.row(peer["peer_id"], peer["owner_id"], NS, key, value, rid=rid,
                                           call_id=f"call-{rid}", version=version, parent_id=parent_id,
                                           is_head=is_head, author_label=peer["label"], created_at=when, **extra))
    return key


def _lbl(tag: str) -> str:
    """A label unique to this test, so a sibling's rows cannot answer its query."""
    return f"{tag}-{uuid.uuid4().hex[:6]}"


def _edges(conn) -> set[tuple]:
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT src_id, edge_type, dst, src_field FROM mesh_edges")
            return {tuple(map(str, r)) for r in cur.fetchall()}


def test_edge_type_catalog_includes_closes_and_mentions(owner):
    """MUTANT: remove either 034 CHECK literal; that direct insert must be refused.

    The Python catalog and the database's CLOSED set are one contract. A fresh runner
    migration must admit both new declared relations, while drift can expose their empty
    buckets before any writer uses them.
    """
    from apps.mesh.normalize import EDGE_TYPES

    assert {"closes", "mentions"} <= set(EDGE_TYPES)
    p = _peer(owner, "edge-catalog")
    key = _row(owner, p, {"body": "catalog control"})
    row_id = owner.execute("SELECT id FROM shared_memory WHERE key = %s", (key,)).fetchone()[0]
    with rls.projection_writer(owner):
        owner.execute(
            "INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type, dst, src_field, recorded_at) "
            "VALUES (%s,%s,1,'closes','msg/1700000000000-target','test',now()), "
            "(%s,%s,1,'mentions','gh:#73','test',now())",
            (row_id, key, row_id, key),
        )
    with rls.server_internal(owner):
        # Only this test writes src_field "test"; other tests in the same database may have
        # projected real closes/mentions edges, so the other buckets are not asserted.
        assert G.drift(owner)["closes"].get("test") == 1
        assert G.drift(owner)["mentions"].get("test") == 1


def test_034_migration_has_a_local_lock_bound_and_backfill_rollback_note():
    """MUTANT: remove 034's first lock bound or its down-script replay instruction."""
    root = Path(__file__).resolve().parent.parent
    up = (root / "sql_meshychat" / "034_mesh_edges_closes_mentions.sql").read_text(encoding="utf-8")
    down = (root / "sql_meshychat" / "down" / "034_mesh_edges_closes_mentions_down.sql").read_text(encoding="utf-8")
    assert up.splitlines()[0] == "SET LOCAL lock_timeout = '5s';"
    assert "python -m apps.mesh backfill --types closes,mentions" in down.splitlines()[0]


# ====================================================================== disciplines

def test_rebuild_is_idempotent_and_discardable(owner):
    """Discipline 1. Truncate-and-replay twice must yield the identical edge set, and a
    catch-up after a rebuild must find nothing left to do."""
    p = _peer(owner, "reb")
    me = _lbl("me")
    _row(owner, p, {"to": me, "thread_id": "t-reb"})
    _row(owner, p, {"to": [me, "other"], "thread": "t-reb", "cc": "cc1"})

    first = P.rebuild(owner)
    after_first = _edges(owner)
    second = P.rebuild(owner)
    after_second = _edges(owner)

    assert first.rows_read >= 2 and first.edges_written >= 5, "positive control: it projected"
    assert after_first == after_second, "two rebuilds from the same log disagreed"
    assert P.catch_up(owner).rows_read == 0, "a fresh rebuild left the watermark behind"


def test_catch_up_is_incremental_and_idempotent(owner):
    """A new row after the watermark is projected once; re-running projects nothing."""
    p = _peer(owner, "inc")
    me = _lbl("me")
    P.rebuild(owner)
    before = len(_edges(owner))

    _row(owner, p, {"to": me, "thread_id": "t-inc"})
    r1 = P.catch_up(owner)
    assert r1.rows_read == 1 and r1.edges_written == 3   # authored_by, addressed_to, in_thread
    mid = len(_edges(owner))
    assert mid == before + 3

    r2 = P.catch_up(owner)
    assert r2.rows_read == 0 and len(_edges(owner)) == mid, "the second pass re-projected"


def test_a_historical_oversized_edge_is_skipped_and_does_not_wedge_the_watermark(owner):
    """MUTANT: omit normalize's destination bound; the edge insert exceeds the btree limit."""
    p = _peer(owner, "oversized")
    P.rebuild(owner)
    long_target = "msg/" + "x" * 6000
    _row(owner, p, {"to": _lbl("safe"), "body": "see " + "a" * 6000 + "/repo#1",
                    "closes": [long_target]})
    result = P.catch_up(owner)
    assert result.rows_read == 1
    assert result.skipped["dst:too_long"] == 1
    assert P.catch_up(owner).rows_read == 0


def test_mentions_reads_bare_and_repo_qualified_refs_from_head_rows(owner):
    """MUTANT: remove the repo wildcard for a bare issue; its qualified mention vanishes.

    MUTANT: remove `sm.is_head`; a superseded mention remains in the answer. A qualified
    search includes its bare reference but marks that hit ambiguous rather than inventing a
    repository association.
    """
    p = _peer(owner, "mentions")
    bare = _row(owner, p, {"body": "PR #73"}, embed_policy="allowed")
    qualified = _row(owner, p, {"body": "Acme/Widget#73"}, embed_policy="allowed")
    P.rebuild(owner)
    with rls.server_internal(owner):
        result = G.mentions(owner, "#73")
        scoped = G.mentions(owner, "acme/widget#73")
    assert {h.hit.key for h in result.hits} == {bare, qualified}
    assert result.also_matched == ("gh:acme/widget#73",)
    by_key = {h.hit.key: h for h in scoped.hits}
    assert by_key[bare].ambiguous is True and by_key[qualified].ambiguous is False


def test_mentions_projection_obeys_the_embed_policy_gate(owner, monkeypatch):
    """MUTANT: omit the projection's mention-policy flag; closed-gate text leaks an edge.

    The normalizer remains a pure parser. The projector decides whether this body-derived
    relation exists from the same gate that protects every other body derivative.
    """
    p = _peer(owner, "mention-policy")
    key = _row(owner, p, {"body": "PR #731"})  # production default: forbidden
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: False)
    P.rebuild(owner)
    assert not {dst for kind, dst in _edges_of(owner, key) if kind == "mentions"}
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    P.rebuild(owner)
    assert {dst for kind, dst in _edges_of(owner, key) if kind == "mentions"} == {"gh:#731"}


def test_mentions_follow_the_embed_gate_at_read_time_not_only_at_projection(owner, monkeypatch):
    """Codex on #89 (P1): a forbidden row projected while the gate was open keeps its mentions edges,
    so the reads apply the gate as every body derivative's read does: closing it hides them at once,
    from graph.mentions and from neighbours' mentions list. An 'allowed' row stays readable.

    MUTANT: drop the gate clause from graph.mentions; the closed-gate read still finds the row.
    MUTANT: neighbours lists mentions whatever the gate; the closed-gate list is not empty.
    """
    from apps.meshsem import related_reads
    p = _peer(owner, "mention-gate-read")
    key = _row(owner, p, {"body": "PR #7310"})  # production default: forbidden
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    P.rebuild(owner)                            # projected while the gate was open
    allowed = _row(owner, p, {"body": "PR #7311"}, embed_policy="allowed")
    P.rebuild(owner)
    with rls.server_internal(owner):
        assert {h.hit.key for h in G.mentions(owner, "#7310", gate=True).hits} == {key}
        assert len(G.mentions(owner, "#7310", gate=False).hits) == 0
        assert len(G.mentions(owner, "#7310").hits) == 0, "closed when not given"
        assert {h.hit.key for h in G.mentions(owner, "#7311", gate=False).hits} == {allowed}
        seen = related_reads.neighbours(owner, None, key, include_inferred=False, limit=20, gate=True)
        hidden = related_reads.neighbours(owner, None, key, include_inferred=False, limit=20, gate=False)
    assert seen["declared"]["mentions"] == ["gh:#7310"]
    assert hidden["declared"]["mentions"] == []


def test_qualified_mention_adds_a_bare_index_key_without_duplicate_hits(owner):
    """MUTANT: omit the bare edge, or remove mention-query deduplication.

    Both forms are equality-indexable; a message which projects both must still be served once.
    """
    p = _peer(owner, "mention-index")
    number = "987654"
    key = _row(owner, p, {"body": f"Acme/Widget#{number}"}, embed_policy="allowed")
    P.rebuild(owner)
    edges = _edges_of(owner, key)
    with rls.server_internal(owner):
        bare = G.mentions(owner, f"#{number}")
        scoped = G.mentions(owner, f"acme/widget#{number}")
    assert {dst for kind, dst in edges if kind == "mentions"} == {f"gh:#{number}", f"gh:acme/widget#{number}"}
    assert [h.hit.key for h in bare.hits] == [key]
    assert [h.hit.key for h in scoped.hits] == [key]


def test_a_qualified_search_takes_no_other_repository_for_its_own(owner):
    """kxxx's Codex note on #89 (graph.py mentions): every qualified reference also writes a bare gh:#N key, so a
    search for acme/widget#N returned messages naming only other/repo#N, and they could crowd the real ones
    out of the limit. Only a bare #N the text names stands in, flagged ambiguous; the bare search is unchanged.

    MUTANT: drop the src_field condition from the qualified query; the other-repo message comes back."""
    p = _peer(owner, "mention-exact")
    number = "975310"
    mine = _row(owner, p, {"body": f"Acme/Widget#{number}"}, embed_policy="allowed")
    theirs = _row(owner, p, {"body": f"Other/Repo#{number}"}, embed_policy="allowed")
    both = _row(owner, p, {"body": f"Other/Repo#{number}, also #{number}"}, embed_policy="allowed")
    P.rebuild(owner)
    with rls.server_internal(owner):
        scoped = G.mentions(owner, f"acme/widget#{number}")
        bare = G.mentions(owner, f"#{number}")
    by_key = {h.hit.key: h for h in scoped.hits}
    assert set(by_key) == {mine, both}
    assert by_key[mine].ambiguous is False and by_key[both].ambiguous is True
    assert scoped.also_matched == (f"gh:#{number}",)
    assert {h.hit.key for h in bare.hits} == {mine, theirs, both}
    fields = dict(owner.execute(
        "SELECT src_key, src_field FROM mesh_edges WHERE edge_type = 'mentions' AND dst = %s",
        (f"gh:#{number}",)).fetchall())
    assert fields == {mine: N.QUALIFIED_ONLY_FIELD, theirs: N.QUALIFIED_ONLY_FIELD, both: "body"}


# The digest every hash test below starts from, and one that shares its first 12 characters: a different payload whose
# citations an 8-character key cannot tell apart.
_PIN159 = "159a6def2cd18bb01d7c4579c16822e7cedaf23ce440d82fbfa87d464078e8a0"
_TWIN159 = "159a6def2cd1" + "0" * 51 + "1"


def test_a_digest_query_keeps_consistent_citations_and_drops_a_colliding_one(owner):
    """Issue #159. Four rows share the key hash:159a6def: the full pin, its 8- and 12-character citations, and a
    different digest with the same first 12. A query for the pin keeps the three that agree, flags the two that named
    less as ambiguous, and drops the twin; a query for the 8 takes all four, none ambiguous.

    MUTANT: drop the join to the row's named references; the twin is a hit of the pin. MUTANT: report every
    consistent row as exact; the 8-character citation reads as proof of the full pin. MUTANT: let the join take an
    index-only key; the twin's hash:159a6def makes it consistent."""
    p = _peer(owner, "hash-query")
    full = _row(owner, p, {"body": f"PAYLOAD sha256 {_PIN159}"}, embed_policy="allowed")
    eight = _row(owner, p, {"body": f"boot.bin v5 {_PIN159[:8]}..."}, embed_policy="allowed")
    twelve = _row(owner, p, {"body": f"index head {_PIN159[:12]}"}, embed_policy="allowed")
    twin = _row(owner, p, {"body": f"other build sha256 {_TWIN159}"}, embed_policy="allowed")
    P.rebuild(owner)
    with rls.server_internal(owner):
        by_pin = {h.hit.key: h for h in G.mentions(owner, f"hash:{_PIN159}").hits}
        by_eight = G.mentions(owner, f"hash:{_PIN159[:8]}")
        by_twelve = {h.hit.key: h for h in G.mentions(owner, f"hash:{_PIN159[:12]}").hits}
    assert set(by_pin) == {full, eight, twelve}
    assert by_pin[full].ambiguous is False and by_pin[full].hit.dst == f"hash:{_PIN159}"
    assert by_pin[eight].ambiguous is True and by_pin[twelve].ambiguous is True
    assert {h.hit.key for h in by_eight.hits} == {full, eight, twelve, twin}
    assert not [h for h in by_eight.hits if h.ambiguous]
    assert set(by_eight.also_matched) == {f"hash:{_PIN159}", f"hash:{_PIN159[:12]}", f"hash:{_TWIN159}"}
    assert set(by_twelve) == {full, twelve, twin} | {eight}
    assert by_twelve[eight].ambiguous is True and by_twelve[full].ambiguous is False
    assert by_twelve[twin].ambiguous is False, "the twin does start with these 12"


def test_a_path_query_matches_by_file_name_and_by_directory_tail(owner):
    """Issue #159. A file-name query takes every row naming that file, in any directory; a path query keeps the
    rows that named that path or a longer one ending in it, flags a row that named only a shorter tail, and drops
    another directory's file of the same name.

    MUTANT: drop the file-name key; the file-name query misses every path. MUTANT: compare only whole paths; a
    longer path ending in the query is missed. MUTANT: report every consistent row as exact. MUTANT (memory-bxxxxx on
    #161): match the tail without its `/`; xsitting-v5/<name> is a plain hit for sitting-v5/<name>."""
    p = _peer(owner, "path-query")
    name = "esp-t159.sh"
    deep = _row(owner, p, {"body": f"run pxx-linux/sitting-v5/{name} as root"}, embed_policy="allowed")
    tail = _row(owner, p, {"body": f"see sitting-v5/{name}"}, embed_policy="allowed")
    bare = _row(owner, p, {"body": f"{name} passed 64 of 64"}, embed_policy="allowed")
    other = _row(owner, p, {"body": f"unrelated other/{name}"}, embed_policy="allowed")
    # memory-bxxxxx on #161: a directory that only ENDS like the query's (x + sitting-v5) is not that directory.
    lookalike = _row(owner, p, {"body": f"see xsitting-v5/{name}"}, embed_policy="allowed")
    P.rebuild(owner)
    with rls.server_internal(owner):
        by_name = G.mentions(owner, f"path:{name}")
        by_path = {h.hit.key: h for h in G.mentions(owner, f"pxx-linux/sitting-v5/{name}").hits}
        by_tail = {h.hit.key: h for h in G.mentions(owner, f"path:sitting-v5/{name}").hits}
    assert {h.hit.key for h in by_name.hits} == {deep, tail, bare, other, lookalike}
    assert not [h for h in by_name.hits if h.ambiguous]
    assert set(by_path) == {deep, tail, bare}
    assert by_path[deep].ambiguous is False and by_path[deep].hit.dst == f"path:pxx-linux/sitting-v5/{name}"
    assert by_path[tail].ambiguous is True and by_path[bare].ambiguous is True
    assert set(by_tail) == {deep, tail, bare}
    assert by_tail[deep].ambiguous is False and by_tail[tail].ambiguous is False and by_tail[bare].ambiguous is True


def test_digest_and_path_mentions_obey_the_read_gate_and_the_limit(owner, monkeypatch):
    """As for the older kinds (codex on #89): a forbidden row projected while the gate was open is hidden once it
    closes. And the read is visibly bounded.

    MUTANT: drop the gate clause from _artifact_mentions; the closed-gate read finds the row. MUTANT: drop the
    limit + 1 read; a cut answer says it is whole."""
    p = _peer(owner, "artifact-gate")
    digest = "159b" + "c0de" * 15
    forbidden = _row(owner, p, {"body": f"sha256 {digest} gate-t159.json"})  # production default: forbidden
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    P.rebuild(owner)
    allowed = _row(owner, p, {"body": f"sha256 {digest} gate-t159.json"}, embed_policy="allowed")
    P.rebuild(owner)
    with rls.server_internal(owner):
        assert {h.hit.key for h in G.mentions(owner, f"hash:{digest}", gate=True).hits} == {forbidden, allowed}
        assert {h.hit.key for h in G.mentions(owner, f"hash:{digest}", gate=False).hits} == {allowed}
        assert {h.hit.key for h in G.mentions(owner, "path:gate-t159.json").hits} == {allowed}
        cut = G.mentions(owner, f"hash:{digest[:8]}", limit=1, gate=True)
    assert len(cut.hits) == 1 and cut.truncated is True


def test_backfill_adds_digest_and_path_edges_to_a_row_projected_before_them(owner):
    """The deploy step: a row projected by the older code has its four older kinds and none of the new; the existing
    `backfill --types mentions` adds hash: and path: and changes no other edge.

    MUTANT: write the new kinds only on catch-up; the backfill adds nothing."""
    p = _peer(owner, "artifact-backfill")
    key = _row(owner, p, {"body": "PR #7159 sha256 159d" + "beef" * 15 + " fill-t159.md"}, embed_policy="allowed")
    P.rebuild(owner)
    with rls.projection_writer(owner):
        owner.execute("DELETE FROM mesh_edges WHERE src_key = %s AND edge_type = 'mentions' "
                      "AND (dst LIKE 'hash:%%' OR dst LIKE 'path:%%')", (key,))
    before = _edges_of(owner, key)
    assert ("mentions", "gh:#7159") in before and not [d for k, d in before if d.startswith(("hash:", "path:"))]
    P.backfill(owner, types=("mentions",), batch=10_000)
    after = _edges_of(owner, key)
    assert after - before == {("mentions", "hash:159d" + "beef" * 15), ("mentions", "hash:159dbeef"),
                              ("mentions", "path:fill-t159.md")}
    assert before <= after


def _audit_cli(*args: str) -> dict:
    """`python -m apps.mesh mentions-audit ...` as an operator runs it: through the serving connection, as the app
    role."""
    out = subprocess.run([sys.executable, "-m", "apps.mesh", "--dsn", DSN, "mentions-audit", *args],
                         cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout)


def test_the_audit_lists_the_artifact_edges_the_extractor_no_longer_writes_and_changes_nothing(owner):
    """The input of a cleanup (divergence 089, second follow-up). An extractor before the fix read a pasted row's key
    id as a digest, and the projection only inserts, so those edges outlive the fix. The audit names exactly them.

    MUTANT: list every artifact edge of a row; the digest the row does name is listed. MUTANT: delete what it finds;
    the edges are gone after the read. MUTANT: no truncation mark; a capped read looks complete."""
    p = _peer(owner, "audit-stale")
    kid = uuid.uuid4().hex[:16]
    start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)
    key = _row(owner, p, {"body": f'"signing_kid": "{kid}", sha256 5ca1ab1e0dd5 in audit-s1.md'},
               embed_policy="allowed")
    clean = _row(owner, p, {"body": "sha256 5ca1ab1e0dd6 only"}, embed_policy="allowed")
    P.rebuild(owner)
    junk = [(f"hash:{kid[:8]}", N.HASH_PREFIX_FIELD), (f"hash:{kid}", "body")]   # what the older extractor wrote
    with rls.projection_writer(owner):
        for dst, field in junk:
            owner.execute("INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type, dst, src_field, "
                          "recorded_at) SELECT id, key, version, 'mentions', %s, %s, created_at FROM shared_memory "
                          "WHERE namespace = %s AND key = %s", (dst, field, NS, key))
    before = {key: _edges_of(owner, key), clean: _edges_of(owner, clean)}
    assert {("mentions", dst) for dst, _ in junk} <= before[key]              # control: the junk is there to find
    assert ("mentions", "hash:5ca1ab1e0dd5") in before[key] and ("mentions", "hash:5ca1ab1e0dd6") in before[clean]

    def mine(report: dict) -> list[tuple]:
        return [(f["src_key"], f["dst"], f["src_field"]) for f in report["stale"] if f["src_key"] in (key, clean)]

    with rls.server_internal(owner):
        report = A.stale(owner)
        recent = A.stale(owner, since=start)
        later = A.stale(owner, since=start + _dt.timedelta(days=1))
        capped = A.stale(owner, cap=1)
    expected = [(key, dst, field) for dst, field in junk]
    assert mine(report) == expected and mine(recent) == expected and mine(later) == []
    assert report["stale_count"] == len(report["stale"]) and report["stale_by_kind"]["hash"] >= 2
    assert not report["truncated"] and report["edges_read"] >= 7 and report["rows_read"] >= 2
    assert capped["truncated"] and capped["edges_read"] == 1
    assert mine(_audit_cli("stale", "--since", start.isoformat())) == expected  # and as the app role
    assert {key: _edges_of(owner, key), clean: _edges_of(owner, clean)} == before


def test_the_audit_counts_the_digests_a_backfill_would_write_with_the_text_before_each(owner, monkeypatch):
    """What an operator reads before an insert-only backfill: the digests by length, the commonest first, and what
    stands in front of them. It follows the embed gate exactly as the backfill does.

    MUTANT: read every row; the forbidden row's digest is counted with the gate closed. MUTANT: count a key id; the
    `signing_kid` row adds one. MUTANT: one context per row; the second citation in a row is lost."""
    p = _peer(owner, "audit-frequency")
    token = "a1" + uuid.uuid4().hex[:11]          # 13 characters, a length few other rows use
    start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)
    twice = f"sha256 {token} here, and once more the digest {token}"
    _row(owner, p, {"body": twice}, embed_policy="allowed")
    _row(owner, p, {"body": f"checksum {token}"})                               # production default: forbidden
    _row(owner, p, {"body": f'"signing_kid": "{token}"'}, embed_policy="allowed")  # a key id, not a digest

    def count(report: dict) -> int:
        return {v["value"]: v["count"] for v in report["by_length"]["13"]["top_values"]}[token]

    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: False)
    with rls.server_internal(owner):
        closed = A.frequency(owner, since=start, top=100_000)
    assert count(closed) == 2 and closed["rows_read"] >= 3 and closed["rows_with_a_digest"] >= 1
    assert not closed["truncated"] and closed["digests"] >= 2
    befores = {b["before"]: b["count"] for b in closed["by_length"]["13"]["top_before"]}
    assert befores[twice[:twice.rindex(token)][-24:]] == 1 and befores["sha256 "] >= 1
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    with rls.server_internal(owner):
        opened = A.frequency(owner, since=start, top=100_000)
        capped = A.frequency(owner, since=start, cap=1)
    assert count(opened) == 3                                                # control: the gate decides, not the row
    kept_out = {v["value"]: v["count"] for v in closed["skipped_by_length"]["13"]["top_values"]}
    assert kept_out[token] == 1                                              # the key id, on the report's other side
    for total, side in (("digests", "by_length"), ("skipped_as_identifier", "skipped_by_length")):
        assert closed[total] == sum(row["count"] for row in closed[side].values()), total   # memory-bxxxxx on #167
    assert capped["truncated"] and capped["rows_read"] == 1
    assert count(_audit_cli("frequency", "--since", start.isoformat(), "--top", "100000")) >= 2  # as the app role


def test_frequency_reads_only_head_rows_of_the_mesh_namespace(owner, monkeypatch):
    """memory-bxxxxx's test from the #166 review, as written. The report's counts decide an insert-only backfill, so
    it must read what the backfill walks: head rows of shared/mesh-chat.

    MUTANT: drop `sm.is_head`; the superseded version is counted. MUTANT: drop the namespace filter; the same key
    in another namespace is counted."""
    p = _peer(owner, "audit-scope")
    old, new, other = ("b2" + uuid.uuid4().hex[:11] for _ in range(3))
    start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)
    v1 = uuid.uuid4()
    key = _row(owner, p, {"body": f"sha256 {old}"}, version=1, is_head=False, rid=v1, embed_policy="allowed")
    _row(owner, p, {"body": f"sha256 {new}"}, key=key, version=2, is_head=True, parent_id=v1, embed_policy="allowed")
    _same_key_elsewhere(owner, p, key, {"body": f"sha256 {other}"})
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    with rls.server_internal(owner):
        report = A.frequency(owner, since=start, top=100_000)
    counts = {v["value"]: v["count"] for v in report["by_length"]["13"]["top_values"]}
    assert counts.get(new) == 1, "control: the head row is read"
    assert old not in counts, "a superseded version was read"
    assert other not in counts, "another namespace was read"


def test_custody_groups_citations_by_signing_peer_through_the_read_gate(owner, monkeypatch):
    """Divergence 090: who cites an artifact. One seat writing under two labels is one holder; a forbidden row's
    signer appears only while the gate is open, as the row itself does in graph.mentions.

    MUTANT: look the signer up by label; the alias becomes a second holder. MUTANT: read custody without the gate;
    the forbidden row's signer is listed with the gate closed. MUTANT: take the type from the body; types are lost."""
    a, b = _peer(owner, "custody-a"), _peer(owner, "custody-b")
    path = "pxx-linux/sitting-t160/esp-t160.sh"
    _row(owner, a, {"body": f"run {path}", "type": "request"}, embed_policy="allowed")
    _row(owner, dict(a, label="custody-a-alias"), {"body": "esp-t160.sh passed", "type": "answer"},
         embed_policy="allowed")
    _row(owner, b, {"body": f"reviewed {path}"})   # production default: forbidden
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    P.rebuild(owner)
    with rls.server_internal(owner):
        closed = G.custody(owner, f"path:{path}", gate=False)
        opened = G.custody(owner, f"path:{path}", gate=True)
    (only,) = closed.holders
    assert only.peer_id == str(a["peer_id"]) and only.labels == ("custody-a", "custody-a-alias")
    assert only.mentions == 2 and only.ambiguous == 1 and only.types == {"answer": 1, "request": 1}
    assert closed.hits_read == 2 and opened.hits_read == 3
    assert {h.peer_id for h in opened.holders} == {str(a["peer_id"]), str(b["peer_id"])}



def test_custody_credits_the_signer_of_the_version_that_named_the_ref(owner, monkeypatch):
    """Codex on #159 phase 2: the signer lookup read the key's head, and the request holds no repeatable snapshot, so
    an edit committed between the hits and the lookup, signed by a sibling peer of the same owner, took the citation
    (its signer, its type) although its own body names nothing. The race is made deterministic: the edit commits, on
    another connection, right after graph.mentions returns.

    MUTANT: look the signer up by key and is_head again; the sibling holds the citation, as an `answer`."""
    a = _peer(owner, "custody-race")
    sibling = uuid.uuid4()
    owner.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                  (sibling, a["owner_id"], f"mesh-custody-sib-{sibling.hex[:8]}", "mesh sibling"))
    b = {"peer_id": sibling, "owner_id": a["owner_id"], "label": "custody-race-sibling"}
    digest = "160c" + "d00d" * 15
    v1 = uuid.uuid4()
    key = _row(owner, a, {"body": f"sha256 {digest}", "type": "status"}, embed_policy="allowed", rid=v1)
    P.rebuild(owner)
    real = G.mentions

    def mentions_then_edit(conn, ref, **kw):
        found = real(conn, ref, **kw)
        with psycopg.connect(DSN, autocommit=True) as other:
            other.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (v1,))
            _row(other, b, {"body": "replaced", "type": "answer"}, key=key, version=2, parent_id=v1)
        return found
    monkeypatch.setattr(G, "mentions", mentions_then_edit)
    with rls.server_internal(owner):
        held = G.custody(owner, f"hash:{digest}", gate=False)
    (holder,) = held.holders
    assert holder.peer_id == str(a["peer_id"]) and holder.labels == ("custody-race",)
    assert holder.types == {"status": 1} and holder.keys == (key,)



def test_custody_takes_the_signer_from_the_mesh_namespace_only(owner):
    """memory-bxxxxx's review of #162: keys are unique only within a namespace, so a row with the SAME key and version
    in another namespace, which any peer with a writable namespace can create, would take over the citation; the
    signer lookup's namespace filter is all that stops it, and nothing pinned it. The lookup's own rows are checked,
    not only the holder: custody keeps one row per key in whatever order they come, so a holder assertion alone
    passes the mutant whenever the mesh row happens to come last (codex on the first version of this test).

    MUTANT: drop `sm.namespace = %s` from the signer lookup; it returns two rows for the key, in either order."""
    a, b = _peer(owner, "custody-ns-a"), _peer(owner, "custody-ns-b")
    digest = "163a" + "beef" * 15
    key = _row(owner, a, {"body": f"sha256 {digest}", "type": "status"}, embed_policy="allowed")
    _same_key_elsewhere(owner, b, key, {"body": "not the mesh's", "type": "answer"})
    P.rebuild(owner)
    with rls.server_internal(owner):
        rows = G._signer_rows(owner, G.mentions(owner, f"hash:{digest}").hits)
        held = G.custody(owner, f"hash:{digest}")
    assert rows == [(key, str(a["peer_id"]), "status")], rows
    (holder,) = held.holders
    assert holder.peer_id == str(a["peer_id"]) and holder.labels == ("custody-ns-a",)
    assert holder.types == {"status": 1}



def test_awaiting_open_starts_from_the_ask_edge_not_all_peer_chatter(owner):
    """MUTANT: begin awaiting's reply/close checks at authored_by; this takes ~10x owed.

    This is the review scale shape: 300 asks and 3,000 unrelated messages by the owing peer.
    The bound is deliberately relative to owed rather than a machine-specific wall-clock limit.
    """
    asker, owing = _peer(owner, "scale-ask"), _peer(owner, "scale-owe")
    for n in range(300):
        _row(owner, asker, {"to": owing["label"], "thread_id": f"scale/{n % 10}",
                             "requires_ack": True, "body": f"ask {n}"})
    for n in range(3000):
        _row(owner, owing, {"to": _lbl("other"), "thread_id": f"other/{n % 50}",
                            "body": f"chatter {n}"})
    P.catch_up(owner, max_batch=10_000)
    with rls.server_internal(owner):
        owner.execute("ANALYZE mesh_edges")
        owner.execute("ANALYZE shared_memory")
        G.awaiting(owner, asker["label"], states=["open"], limit=100)
        start = time.perf_counter()
        awaiting = G.awaiting(owner, asker["label"], states=["open"], limit=100)
        awaiting_ms = (time.perf_counter() - start) * 1000
        G.owed_acks(owner, owing["label"], states=["open"], limit=100)
        start = time.perf_counter()
        owed = G.owed_acks(owner, owing["label"], states=["open"], limit=100)
        owed_ms = (time.perf_counter() - start) * 1000
    print(f"B3 scale awaiting_open_ms={awaiting_ms:.1f} owed_open_ms={owed_ms:.1f}")
    assert len(awaiting) == len(owed) == 100
    assert awaiting_ms <= owed_ms * 5 + 20


def test_backfill_new_types_is_idempotent_and_does_not_move_projection_state(owner):
    """MUTANT: update the watermark in backfill; the saved state changes.

    MUTANT: remove ON CONFLICT from its insert; the second run raises instead of writing
    zero. A targeted replay can therefore run beside live catch-up without a rebuild.
    """
    p = _peer(owner, "backfill")
    key = _row(owner, p, {"body": "PR #73", "closes": ["msg/1760000000000-target"]},
               embed_policy="allowed")
    P.rebuild(owner)
    with rls.projection_writer(owner):
        owner.execute("DELETE FROM mesh_edges WHERE src_key = %s AND edge_type IN ('closes', 'mentions')", (key,))
    before = _proj_state(owner)
    with rls.server_internal(owner):
        other_before = set(owner.execute("SELECT edge_type, dst FROM mesh_edges WHERE src_key = %s "
                                         "AND edge_type NOT IN ('closes', 'mentions')", (key,)).fetchall())
    first = P.backfill(owner, batch=1)
    second = P.backfill(owner, batch=1)
    assert first.edges_written == 2 and second.edges_written == 0
    assert _proj_state(owner) == before
    with rls.server_internal(owner):
        other_after = set(owner.execute("SELECT edge_type, dst FROM mesh_edges WHERE src_key = %s "
                                        "AND edge_type NOT IN ('closes', 'mentions')", (key,)).fetchall())
    assert other_after == other_before


def test_backfill_stops_at_the_catch_up_watermark(owner):
    """MUTANT: leave backfill's upper sequence bound out; future rows inflate coverage.

    A targeted replay fills a missing edge in the already covered prefix. It must leave a
    committed row after that prefix to the normal catch-up path.
    """
    p = _peer(owner, "backfill-watermark")
    P.rebuild(owner)
    covered = _row(owner, p, {"body": "PR #740"}, embed_policy="allowed")
    P.catch_up(owner)
    before = _proj_state(owner)
    with rls.projection_writer(owner):
        owner.execute("DELETE FROM mesh_edges WHERE src_key = %s AND edge_type = 'mentions'", (covered,))
    future = _row(owner, p, {"body": "PR #741"}, embed_policy="allowed")
    report = P.backfill(owner, types=("mentions",), batch=10_000)
    with rls.server_internal(owner):
        got = {key for (key,) in owner.execute("SELECT src_key FROM mesh_edges WHERE edge_type = 'mentions' "
                                               "AND src_key = ANY(%s)", ([covered, future],)).fetchall()}
    assert report.rows_read >= 1 and _proj_state(owner) == before
    assert got == {covered}


def test_awaiting_mirrors_owed_state_for_each_owing_label(owner):
    """MUTANT: compare authored_by to the caller label; the owing label's close is missed."""
    asker, owing = _peer(owner, "awaiting-ask"), _peer(owner, "awaiting-owe")
    caller, other = _lbl("awaiting-caller"), _lbl("awaiting-other")
    ask = _row(owner, dict(asker, label=caller), {"to": [other], "requires_ack": True,
                                                   "body": "please settle"})
    _row(owner, dict(owing, label=other), {"to": [caller], "body": "settled", "closes": [ask]})
    P.rebuild(owner)
    with rls.server_internal(owner):
        rows = G.awaiting(owner, caller, states=["closed"])
    assert [(row.hit.key, row.owing_label, row.state) for row in rows] == [(ask, other, "closed")]


def test_the_asker_withdraws_its_own_ask_with_closes_and_nobody_else_can(owner):
    """oxxxxxx, 2026-10-30: 75 stale asks it could not retire, because only the owing label's close counted. The
    asker's own close, addressed to itself, now settles the ask for every owing label, in owed and awaiting alike. A
    third peer's close still settles nothing, and the asker's plain reply is not a close.

    MUTANT: drop `OR ca.dst = a.dst` from _OWED_CLOSED; the withdrawn ask stays open in owed. MUTANT: drop it from
    awaiting's `closed`; it stays open in awaiting. MUTANT: replace either with `OR TRUE`; the outsider closes."""
    asker, owing, outsider = (_peer(owner, "withdraw-ask"), _peer(owner, "withdraw-owe"),
                              _peer(owner, "withdraw-other"))
    caller, first, second, stranger = (_lbl("withdraw-caller"), _lbl("withdraw-first"), _lbl("withdraw-second"),
                                       _lbl("withdraw-stranger"))
    now = _dt.datetime.now(_dt.timezone.utc)
    asker = dict(asker, label=caller)
    withdrawn = _row(owner, asker, {"to": [first, second], "requires_ack": True, "body": "stale"}, created_at=now)
    foreign = _row(owner, asker, {"to": [first], "requires_ack": True, "body": "still wanted"}, created_at=now)
    replied = _row(owner, asker, {"to": [first], "requires_ack": True, "body": "follow-up only"}, created_at=now)
    later = now + _dt.timedelta(seconds=1)
    _row(owner, asker, {"to": [caller], "body": "withdrawn", "closes": [withdrawn]}, created_at=later)
    _row(owner, dict(outsider, label=stranger), {"to": [caller], "body": "not mine", "closes": [foreign]},
         created_at=later)
    _row(owner, asker, {"to": [first], "body": "any news?", "in_reply_to": replied}, created_at=later)
    P.rebuild(owner)
    with rls.server_internal(owner):
        states = ["open", "active", "answered", "closed"]
        awaiting = {(a.hit.key, a.owing_label): a.state for a in G.awaiting(owner, caller, states=states, limit=None)}
        owed_first = {o.hit.key: o.state for o in G.owed_acks(owner, first, states=states)}
        owed_second = {o.hit.key: o.state for o in G.owed_acks(owner, second, states=states)}
        counts = G.owed_counts(owner, second)
    assert awaiting[(withdrawn, first)] == awaiting[(withdrawn, second)] == "closed"
    assert owed_first[withdrawn] == owed_second[withdrawn] == "closed"
    assert counts["closed"] == 1 and counts["open"] == 0
    assert awaiting[(foreign, first)] == owed_first[foreign] == "open"
    assert awaiting[(replied, first)] == owed_first[replied] == "open"


def test_awaiting_uses_all_labels_of_the_owing_peer_and_excludes_the_asker(owner):
    """MUTANT: compare reply authors to e.dst; a roster alias reply remains open."""
    from apps.memoryskills import handlers

    asker, owing = _peer(owner, "awaiting-label-ask"), _peer(owner, "awaiting-label-owe")
    owing_name = owner.execute("SELECT name FROM peers WHERE id = %s", (owing["peer_id"],)).fetchone()[0]
    asker_label, owing_alias = _lbl("asker"), _lbl("owing-cc")
    handlers.set_roster_labels({str(asker["peer_id"]): asker_label,
                                str(owing["peer_id"]): owing_alias})
    try:
        ask = _row(owner, dict(asker, label=asker_label), {"to": [owing_name], "requires_ack": True,
                                                            "body": "please settle"})
        _row(owner, dict(owing, label=owing_alias), {"to": [asker_label], "in_reply_to": ask,
                                                      "body": "settled"})
        self_ask = _row(owner, dict(asker, label=asker_label), {"to": [asker_label, owing_name],
                                                                 "requires_ack": True, "body": "note"})
        P.rebuild(owner)
        with rls.server_internal(owner):
            awaiting = {(a.hit.key, a.owing_label): a.state for a in G.awaiting(owner, asker_label, limit=None)}
        assert awaiting[(ask, owing_name)] == "answered"
        assert (self_ask, asker_label) not in awaiting
    finally:
        handlers.prime_roster_labels(b"{}")


def test_awaiting_resolves_a_roster_label_and_ignores_a_contested_name(owner):
    """codex on #89. Finding 3: an ask addressed to the owing peer's ROSTER label, answered under its
    peers.name, read open in awaiting (the roster label resolved only if it was also some peer's name)
    while owed read answered. Finding 2: another peer whose peers.name equals that roster label (a name
    divergence 066 refuses to answer for) could answer the ask for it.

    MUTANT: resolve owing labels by raw peers.name again; the roster ask reads open, and the contested
    peer's reply answers the second ask."""
    from apps.memoryskills import handlers
    from apps.mesh import views

    asker, owing, other = _peer(owner, "rost-ask"), _peer(owner, "rost-owe"), _peer(owner, "rost-other")
    asker_label, roster = _lbl("rost-asker"), _lbl("rost-label")
    owing_name = owner.execute("SELECT name FROM peers WHERE id = %s", (owing["peer_id"],)).fetchone()[0]
    handlers.set_roster_labels({str(asker["peer_id"]): asker_label, str(owing["peer_id"]): roster})
    try:
        # finding 3: addressed to the roster label, answered under the owing peer's own name
        ask = _row(owner, dict(asker, label=asker_label), {"to": [roster], "requires_ack": True, "body": "a"})
        _row(owner, dict(owing, label=owing_name), {"to": [asker_label], "in_reply_to": ask, "body": "b"})
        P.rebuild(owner)
        with rls.server_internal(owner):
            mine = [x["label"] for x in views.labels_for(owner, owing["peer_id"])]
            owed = {o.hit.key: o.state for o in G.owed_acks(owner, mine, limit=None,
                                                            states=["open", "active", "answered", "closed"])}
            waiting = {(a.hit.key, a.owing_label): a.state for a in G.awaiting(
                owner, asker_label, limit=None, states=["open", "active", "answered", "closed"])}
        assert owed[ask] == "answered"
        assert waiting[(ask, roster)] == "answered"
        # finding 2: another peer takes the roster label as its peers.name, a contested name, and answers
        owner.execute("UPDATE peers SET name = %s WHERE id = %s", (roster, other["peer_id"]))
        second = _row(owner, dict(asker, label=asker_label), {"to": [roster], "requires_ack": True, "body": "c"})
        _row(owner, dict(other, label=f"peer-{str(other['peer_id'])[:8]}"),
             {"to": [asker_label], "in_reply_to": second, "body": "d"})
        P.rebuild(owner)
        with rls.server_internal(owner):
            waiting = {(a.hit.key, a.owing_label): a.state for a in G.awaiting(
                owner, asker_label, limit=None, states=["open", "active", "answered", "closed"])}
        assert waiting[(second, roster)] == "open"
    finally:
        handlers.prime_roster_labels(b"{}")


def test_catch_up_is_bounded_and_reports_more_pending(owner):
    """B5. The batch bound is honored and `more` says the log has more."""
    p = _peer(owner, "bound")
    P.rebuild(owner)
    for i in range(5):
        _row(owner, p, {"to": _lbl("b"), "body": str(i)})
    r = P.catch_up(owner, max_batch=3)
    assert r.rows_read == 3 and r.more is True
    r = P.catch_up(owner, max_batch=3)
    assert r.rows_read == 2 and r.more is False


def test_the_projection_is_unreadable_and_unwritable_without_server_internal(owner, app):
    """Discipline 2, on the database rather than in a docstring.

    The app role with NO context sees nothing and can insert nothing. The same role
    UNDER server_internal sees the rows, which is the positive control proving the
    refusal is the policy and not a broken table.
    """
    p = _peer(owner, "rls")
    me = _lbl("me")
    _row(owner, p, {"to": me})
    P.rebuild(owner)

    with app.cursor() as cur:
        cur.execute("SELECT count(*) FROM mesh_edges WHERE dst = %s", (me,))
        assert cur.fetchone()[0] == 0, "the projection leaked to a contextless connection"
        with pytest.raises(psycopg.Error):
            cur.execute("INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type,"
                        " dst, src_field, recorded_at) VALUES (gen_random_uuid(), 'k', 1,"
                        " 'addressed_to', %s, 'test', now())", (me,))
    with rls.server_internal(app):
        with app.cursor() as cur:
            cur.execute("SELECT count(*) FROM mesh_edges WHERE dst = %s", (me,))
            assert cur.fetchone()[0] >= 1, "positive control: server_internal reads it"
    rls.assert_rls_active(app, table="mesh_edges")


# ======================================================================= the promise

def test_inbox_finds_a_message_whichever_way_to_was_spelled(owner):
    """THE QUERY THE PROJECTION EXISTS FOR, against the two live shapes of `to`."""
    p = _peer(owner, "inbox")
    me = _lbl("me")
    k_str = _row(owner, p, {"to": me, "body": "string-to"})
    k_arr = _row(owner, p, {"to": ["nobody", me], "body": "array-to"})
    k_cc = _row(owner, p, {"to": ["nobody"], "cc": me, "body": "cc-only"})
    _row(owner, p, {"to": ["nobody"], "body": "not mine"})
    P.rebuild(owner)

    with rls.server_internal(owner):
        both = G.inbox(owner, me)
        no_cc = G.inbox(owner, me, include_cc=False)
    assert {h.key for h in both.hits} == {k_str, k_arr, k_cc}
    assert {h.key for h in no_cc.hits} == {k_str, k_arr}
    assert {h.src_field for h in both.hits} == {"to:string", "to:array", "cc:string"}


def test_thread_walk_unites_all_spellings(owner):
    p = _peer(owner, "thr")
    t = _lbl("t")
    a = _row(owner, p, {"to": ["x"], "thread_id": t})
    b = _row(owner, p, {"to": ["x"], "thread": t})
    c = _row(owner, p, {"to": ["x"], "thread_id": t, "thread": t, "reply_to": t})
    P.rebuild(owner)
    with rls.server_internal(owner):
        res = G.thread(owner, t)
    assert [h.key for h in res.hits] == [a, b, c], "oldest first, all three spellings"
    assert {h.src_field for h in res.hits} == {"thread_id", "thread"}


def test_a_superseded_version_drops_out_of_every_answer(owner):
    """The catch-up cannot see an is_head flip; the read-time join must."""
    p = _peer(owner, "head")
    me = _lbl("me")
    key = f"msg/{int(_dt.datetime.now().timestamp() * 1000)}-{uuid.uuid4().hex}"
    v1 = uuid.uuid4()
    # v2 names v1 as its parent: sql/064 refuses a version > 1 with no parent, and the
    # chain is the real structure the projection joins across, not a seeding shortcut.
    _row(owner, p, {"to": me, "body": "v1"}, key=key, version=1, is_head=False, rid=v1)
    _row(owner, p, {"to": me, "body": "v2"}, key=key, version=2, is_head=True, parent_id=v1)
    P.rebuild(owner)
    with rls.server_internal(owner):
        res = G.inbox(owner, me)
    assert [(h.key, h.version) for h in res.hits] == [(key, 2)]


def test_owed_acks_computes_open_and_reports_activity_separately(owner):
    """OPEN(P, M): asked of me, requires_ack, not by me. Activity since is a sibling
    fact, never merged into 'acked'."""
    asker, me_peer = _peer(owner, "asker"), _peer(owner, "owes")
    me = _lbl("me")
    t = _lbl("t")
    asked = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": t})
    _row(owner, asker, {"to": [me], "requires_ack": False, "thread_id": t})   # no ack asked
    silent = _row(owner, asker, {"to": [me], "requires_ack": "true", "thread_id": _lbl("t2")})
    # I speak in thread t AFTER the ask; I say nothing in t2.
    me_peer = dict(me_peer, label=me)
    _row(owner, me_peer, {"to": ["asker"], "thread_id": t},
         created_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=1))
    P.rebuild(owner)
    with rls.server_internal(owner):
        owed = {o.hit.key: o for o in G.owed_acks(owner, me)}
    assert set(owed) == {asked, silent}
    assert owed[asked].answered_in_thread is True
    assert owed[silent].answered_in_thread is False


def test_owed_acks_names_direct_active_and_open_states_and_completion_condition(owner):
    """An explicit close from ANY caller label beats direct `answered`; later same-thread
    speech is only `active`; neither is `open`, and an outsider's direct reply answers nobody.

    MUTANT: change `rr.dst = e.src_key` to `rr.dst <> e.src_key`; the `answered` key
    becomes open. MUTANT: remove `ra.dst = ANY(%s)`; the outsider's reply falsely answers
    its ask. MUTANT: remove the closes CASE branch; the explicit close becomes answered.
    MUTANT: drop `sm.value->>'done_when'`; the completion condition vanishes.
    """
    asker, owing, outsider = _peer(owner, "state-ask"), _peer(owner, "state-me"), _peer(owner, "state-other")
    me, second = _lbl("state-me"), _lbl("state-second")
    now = _dt.datetime.now(_dt.timezone.utc)
    open_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": _lbl("open"),
                                   "done_when": "post the receipt"}, created_at=now)
    active_thread = _lbl("active")
    active_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": active_thread},
                      created_at=now)
    answered_thread = _lbl("answered")
    answered_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": answered_thread},
                        created_at=now)
    closed_thread = _lbl("closed")
    closed_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": closed_thread},
                      created_at=now)
    outsider_thread = _lbl("outsider")
    outsider_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": outsider_thread},
                        created_at=now)
    owing = dict(owing, label=second)
    _row(owner, owing, {"to": ["asker"], "thread_id": active_thread}, created_at=now + _dt.timedelta(seconds=1))
    _row(owner, owing, {"to": ["asker"], "thread_id": answered_thread,
                        "in_reply_to": answered_ask}, created_at=now + _dt.timedelta(seconds=2))
    _row(owner, owing, {"to": ["asker"], "thread_id": closed_thread,
                        "in_reply_to": closed_ask, "closes": [closed_ask]},
         created_at=now + _dt.timedelta(seconds=3))
    _row(owner, outsider, {"to": ["asker"], "thread_id": outsider_thread,
                           "in_reply_to": outsider_ask}, created_at=now + _dt.timedelta(seconds=3))
    P.rebuild(owner)
    with rls.server_internal(owner):
        owed = {o.hit.key: o for o in G.owed_acks(owner, [me, second],
                                                   states=["open", "active", "answered", "closed"])}
    assert owed[open_ask].state == "open" and owed[open_ask].done_when == "post the receipt"
    assert owed[active_ask].state == "active" and owed[active_ask].answered_directly is False
    assert owed[answered_ask].state == "answered" and owed[answered_ask].answered_directly is True
    assert owed[closed_ask].state == "closed" and owed[closed_ask].answered_directly is True
    assert owed[outsider_ask].state == "open" and owed[outsider_ask].answered_directly is False


def test_owed_acks_judges_a_reply_by_its_head_version(owner):
    """kxxx's review of #79: a reply's SUPERSEDED version keeps its edges, so without a head join an old version
    that replied to an ask, or spoke in its thread, answers it even after the reply was re-addressed. Here my
    reply's v1 answered one ask and spoke in the other's thread; its head v2 moved to another thread with no
    in_reply_to. Both asks are open again.

    MUTANT: drop the head join from `direct`; the first ask reads answered. MUTANT: drop it from `activity`; the
    second reads active."""
    asker, owing = _peer(owner, "head-ask"), _peer(owner, "head-me")
    me = _lbl("head-me")
    now = _dt.datetime.now(_dt.timezone.utc)
    direct_thread, active_thread = _lbl("direct"), _lbl("active")
    direct_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": direct_thread}, created_at=now)
    active_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": active_thread}, created_at=now)
    owing = dict(owing, label=me)
    reply = f"msg/{int((now + _dt.timedelta(seconds=2)).timestamp() * 1000)}-{uuid.uuid4().hex}"
    v1 = uuid.uuid4()
    _row(owner, owing, {"to": ["asker"], "thread_id": active_thread, "in_reply_to": direct_ask}, key=reply,
         version=1, is_head=False, rid=v1, created_at=now + _dt.timedelta(seconds=2))
    _row(owner, owing, {"to": ["asker"], "thread_id": _lbl("elsewhere")}, key=reply, version=2, is_head=True,
         parent_id=v1, created_at=now + _dt.timedelta(seconds=3))
    P.rebuild(owner)
    with rls.server_internal(owner):
        owed = {o.hit.key: o for o in G.owed_acks(owner, me)}
    assert owed[direct_ask].state == "open" and owed[direct_ask].answered_directly is False
    assert owed[active_ask].state == "open" and owed[active_ask].answered_in_thread is False


def test_owed_and_awaiting_judge_a_reply_and_a_close_by_the_head_version(owner):
    """The B branch rebased onto main's head join: its own checks (closes, and every check in awaiting) must join
    the reply or close to its head too, or owed and awaiting disagree about the same (ask, peer). My reply's v1
    answered the first ask, spoke in the second's thread and closed the third; its head v2 moved to another thread
    and carries neither in_reply_to nor closes. All three asks are open again, from both sides.

    MUTANTS, each in owed_acks or in awaiting: drop the head join from `direct` (the first reads answered), from
    `activity` (the second reads active) or from `closed` (the third reads closed)."""
    asker, owing = _peer(owner, "head2-ask"), _peer(owner, "head2-me")
    me, them = _lbl("head2-me"), _lbl("head2-ask")
    now = _dt.datetime.now(_dt.timezone.utc)
    asker = dict(asker, label=them)
    threads = {kind: _lbl(kind) for kind in ("direct", "active", "closed")}  # one label per thread: _lbl is unique per call
    asks = {}
    for kind in ("direct", "active", "closed"):
        asks[kind] = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": threads[kind]},
                          created_at=now)
    owing = dict(owing, label=me)
    reply = f"msg/{int((now + _dt.timedelta(seconds=2)).timestamp() * 1000)}-{uuid.uuid4().hex}"
    v1 = uuid.uuid4()
    _row(owner, owing, {"to": [them], "thread_id": threads["active"], "in_reply_to": asks["direct"],
                        "closes": [asks["closed"]]}, key=reply, version=1, is_head=False, rid=v1,
         created_at=now + _dt.timedelta(seconds=2))
    _row(owner, owing, {"to": [them], "thread_id": _lbl("elsewhere2")}, key=reply, version=2, is_head=True,
         parent_id=v1, created_at=now + _dt.timedelta(seconds=3))
    P.rebuild(owner)
    with rls.server_internal(owner):
        owed = {o.hit.key: o.state for o in G.owed_acks(owner, me, limit=None, states=["open", "active", "answered", "closed"])}
        waiting = {w.hit.key: w.state for w in G.awaiting(owner, them, limit=None, states=["open", "active", "answered", "closed"])}
    assert {k: owed[a] for k, a in asks.items()} == {"direct": "open", "active": "open", "closed": "open"}
    assert {k: waiting[a] for k, a in asks.items()} == {"direct": "open", "active": "open", "closed": "open"}


def test_owed_acks_applies_state_and_since_before_limit(owner):
    """A state bound and age bound are predicates of the SQL question, before LIMIT.

    MUTANT: complement the SQL state predicate; the newer answered asks hide the older
    open ask. MUTANT: remove `e.recorded_at >= %s`; the old ask survives `since`.
    """
    asker, owing = _peer(owner, "bound-ask"), _peer(owner, "bound-me")
    me = _lbl("bound-me")
    base = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)
    old = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": _lbl("old")},
               created_at=base)
    open_recent = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": _lbl("recent")},
                       created_at=base + _dt.timedelta(days=2))
    for i in range(3):
        thread = _lbl(f"answered-{i}")
        ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": thread},
                   created_at=base + _dt.timedelta(days=2, seconds=i + 10))
        _row(owner, dict(owing, label=me), {"to": ["asker"], "thread_id": thread,
                                             "in_reply_to": ask},
             created_at=base + _dt.timedelta(days=2, seconds=i + 20))
    P.rebuild(owner)
    with rls.server_internal(owner):
        state_limited = G.owed_acks(owner, me, states=["open"], limit=1)
        recent = G.owed_acks(owner, me, since=base + _dt.timedelta(days=1), limit=None)
    assert [item.hit.key for item in state_limited] == [open_recent]
    assert old not in {item.hit.key for item in recent}
    with pytest.raises(ValueError, match="at least one owed state"):
        G.owed_acks(owner, me, states=[])


def test_drift_and_coverage_are_numbers(owner):
    p = _peer(owner, "drift")
    _row(owner, p, {"to": _lbl("a"), "thread": "legacy"})
    _row(owner, p, {"to": [_lbl("b")], "thread_id": "canon"})
    P.rebuild(owner)
    with rls.server_internal(owner):
        d = G.drift(owner)
        projected, total = G.coverage(owner)
    assert d["in_thread"].get("thread", 0) >= 1 and d["in_thread"].get("thread_id", 0) >= 1
    assert d["addressed_to"].get("to:string", 0) >= 1 and d["addressed_to"].get("to:array", 0) >= 1
    assert 0 < projected <= total


def test_drift_counts_head_versions_only(owner):
    """Finding 23. A superseded version keeps its edges, so drift counted a rewritten message once
    per version, and the legacy spellings its head had dropped kept counting. Measured as a delta,
    since other tests' rows are in the same projection.

    CONTROL: the head version's own spellings are counted (the +1 asserts).
    MUTANT: drop the head join from graph.drift; the superseded version adds 1 to to:string and thread."""
    p = _peer(owner, "drift-head")
    v1 = uuid.uuid4()
    key = f"msg/{int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)}-{v1.hex}"
    P.rebuild(owner)
    with rls.server_internal(owner):
        before = G.drift(owner)
    _row(owner, p, {"to": _lbl("legacy"), "thread": "legacy"}, key=key, version=1, is_head=False, rid=v1)
    _row(owner, p, {"to": [_lbl("canon")], "thread_id": "canon"}, key=key, version=2, is_head=True,
         parent_id=v1)
    P.rebuild(owner)
    with rls.server_internal(owner):
        after = G.drift(owner)

    def delta(edge_type, field):
        return after[edge_type].get(field, 0) - before[edge_type].get(field, 0)
    assert delta("addressed_to", "to:array") == 1 and delta("in_thread", "thread_id") == 1
    assert delta("addressed_to", "to:string") == 0 and delta("in_thread", "thread") == 0


def test_the_inbox_lookup_does_not_scan(owner):
    """B5. The inbox must be served by mesh_edges_dst_idx, not a scan of the projection."""
    p = _peer(owner, "cost")
    me = _lbl("me")
    _row(owner, p, {"to": me})
    for i in range(200):
        _row(owner, p, {"to": [_lbl("filler")], "body": str(i)})
    P.rebuild(owner)
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("ANALYZE mesh_edges")
            cur.execute("EXPLAIN (FORMAT JSON) SELECT src_key FROM mesh_edges "
                        "WHERE edge_type = ANY(%s) AND dst = %s ORDER BY recorded_at DESC",
                        (["addressed_to", "copied_to"], me))
            plan = json.dumps(cur.fetchone()[0])
    assert "mesh_edges_dst_idx" in plan, f"the inbox is not using its index. Plan: {plan}"


def test_rebuild_and_catch_up_run_AS_THE_APP_ROLE_not_just_as_the_owner(owner, app):
    """THE ROLE THE COMMAND ACTUALLY USES, which is not the role the other tests use.

    Every other write-path test here takes `owner` - the bootstrap owner role (`meshychat_owner`, formerly `meshy`), which
    bypasses RLS and holds every privilege. `python -m apps.mesh rebuild` does not: it goes
    through `dbconn.serving_connection()`, which does `SET ROLE meshychat_app`.

    THAT DIFFERENCE WAS NOT THEORETICAL. `rebuild()` opened with `TRUNCATE mesh_edges`, and
    TRUNCATE is a SEPARATE Postgres privilege that `meshychat_app` does not hold on any table in
    this schema - the default privileges in sql/010 grant SELECT/INSERT/UPDATE/DELETE and
    stop. So the suite was green and the command was broken:

        $ python -m apps.mesh rebuild
        psycopg.errors.InsufficientPrivilege: permission denied for table mesh_edges

    A green suite about a role nobody serves with is the same shape as an RLS test that runs
    as the bypassing user, which this project has already been bitten by twice.

    This test would have failed before `TRUNCATE` became `DELETE`.
    """
    p = _peer(owner, "approle")
    _row(owner, p, {"to": _lbl("me"), "thread_id": "t-approle"})

    # CONTROL FIRST: confirm this connection really is the restricted role, not the owner
    # by another name. Without it, the test passes just as happily on a `meshy` connection
    # and proves nothing about privileges.
    with app.cursor() as cur:
        cur.execute("SELECT current_user, "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)")
        role, bypasses = cur.fetchone()
    assert role == rls.APP_ROLE and not bypasses, (
        f"control failed: this fixture is connected as {role!r}, so it cannot detect a "
        "privilege the app role lacks")

    report = P.rebuild(app)
    assert report.rows_read >= 1, report

    # And the incremental path, which the 60-second job runs on the same role.
    report2 = P.catch_up(app)
    assert report2.rows_read == 0, f"a rebuild-then-catch-up re-projected rows: {report2}"


def test_the_cli_runs_as_a_module_entry_point():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([sys.executable, "-m", "apps.mesh", "--help"],
                         cwd=str(Path(__file__).resolve().parent.parent),
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, f"rc={out.returncode} stderr={out.stderr[:400]}"
    for verb in ("rebuild", "catch-up", "inbox", "owed", "drift"):
        assert verb in out.stdout


def test_owed_cli_does_not_report_explicitly_closed_asks_as_debt(owner):
    """MUTANT: call owed_acks without its default non-closed state set in the CLI path.

    A direct reply is still reported as `answered`; only a `closes` edge settles an ask.
    """
    asker, owing = _peer(owner, "cli-ask"), _peer(owner, "cli-owing")
    now = _dt.datetime.now(_dt.timezone.utc)
    closed = _row(owner, asker, {"to": owing["label"], "thread_id": _lbl("cli-closed"),
                                  "requires_ack": True}, created_at=now)
    answered = _row(owner, asker, {"to": owing["label"], "thread_id": _lbl("cli-answered"),
                                    "requires_ack": True}, created_at=now + _dt.timedelta(seconds=1))
    _row(owner, owing, {"to": asker["label"], "closes": [closed]},
         created_at=now + _dt.timedelta(seconds=2))
    _row(owner, owing, {"to": asker["label"], "in_reply_to": answered},
         created_at=now + _dt.timedelta(seconds=3))
    P.rebuild(owner)
    out = subprocess.run([sys.executable, "-m", "apps.mesh", "--dsn", DSN, "owed", "--label", owing["label"]],
                         cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert answered in out.stdout and closed not in out.stdout


# ============================================================ D19, the commit order

def _drain_projection(conn) -> None:
    for _ in range(200):
        if not P.catch_up(conn).more:
            return


def _edges_of(conn, key: str) -> set[tuple]:
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT edge_type, dst FROM mesh_edges WHERE src_key = %s", (key,))
            return {tuple(map(str, r)) for r in cur.fetchall()}


def test_d19_a_row_that_commits_after_a_later_stamped_row_was_walked_is_projected(owner):
    """STATUS D19. `created_at` is the writer's TRANSACTION START. Transaction A starts, then
    B writes a row and commits, the catch-up walks B's row, and only then does A write and
    commit its own: A's row carries the EARLIER created_at, below the watermark the walk
    already holds. The walk must still visit it on its next tick. CONTROL: B's row is
    projected by the first tick, and A's row is not visible to it at all (A had not
    committed), so the first tick could not have projected it.

    ONE CLOCK. Both created_at values are on the DATABASE's clock, as this build's writer's
    is (apps/memoryskills/handlers.py inserts no created_at, so the column default now()
    applies): A's is its transaction's now(), B's is 1 ms after it. B's used to be _row's
    default, THIS PROCESS's clock, and the dev server's clock is a Linux VM's, not Windows'.
    Measured 2026-09-26 by replaying these steps: quiet, B's client stamp came 0.2 ms or more
    after A's server now(); under the combined run's load A's now() came up to 4.5 ms AFTER
    B's stamp in 40% of 37,800 samples. Then A's "earlier" key was the greater one, the
    catch-up that walked A correctly moved the reader-facing key FORWARD to it, and the last
    assertion failed: 6 of 10 combined runs, in this test or its embed twin. CONTROL: the
    stored created_at values are in the order this test is about."""
    p = _peer(owner, "late")
    me_a, me_b = _lbl("late-a"), _lbl("late-b")
    _drain_projection(owner)

    early = psycopg.connect(DSN)                  # a transaction, not autocommit
    try:
        started = early.execute("SELECT now()").fetchone()[0].astimezone(_dt.timezone.utc)
        key_b = _row(owner, p, {"to": me_b, "thread_id": "t-late"},
                     created_at=started + _dt.timedelta(milliseconds=1))
        first = P.catch_up(owner)
        assert first.rows_read >= 1
        assert ("addressed_to", me_b) in _edges_of(owner, key_b), "control: B projected"

        key_a = _row(early, p, {"to": me_a, "thread_id": "t-late"}, created_at=started)
        assert _edges_of(owner, key_a) == set(), "control: A was not committed yet"
        early.commit()
    finally:
        early.close()

    with owner.cursor() as cur:
        cur.execute("SELECT key, created_at FROM shared_memory WHERE key = ANY(%s)",
                    ([key_a, key_b],))
        at = dict(cur.fetchall())
    assert at[key_a] < at[key_b], f"control: A's created_at is the earlier one: {at}"

    after_b = _state_key(owner)
    P.catch_up(owner)
    assert ("addressed_to", me_a) in _edges_of(owner, key_a), (
        "a row that committed after a later-stamped row was walked was never projected")
    assert P.catch_up(owner).rows_read == 0, "and it is projected once, not on every tick"
    now = _state_key(owner)
    assert now == after_b, (
        f"the reader-facing (watermark_at, watermark_id) moved from {after_b} to {now} when "
        "only an older row was walked; readers compare rows with it and it must only move "
        "forward")


def _state_key(conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute("SELECT watermark_at, watermark_id FROM mesh_edges_projection "
                    "WHERE projection = %s", (P.PROJECTION_NAME,))
        return tuple(cur.fetchone())


#: Review finding 8: A and B run on ONE thread, so a stamp that took its lock at INSERT
#: (the trigger made IMMEDIATE) would leave B's insert waiting on A, which commits only
#: after B's insert returns: a hang, not a failure. A bounded lock wait turns it into one.
BOUNDED_LOCK_WAIT = "-c lock_timeout=5s"


def test_d19_the_commit_order_follows_commits_not_inserts(owner):
    """The mechanism under the walk (sql_meshychat/023): a mesh row's seq is drawn when its
    transaction COMMITS. A inserts first and B second; B commits first, so B's seq is lower.
    CONTROL: a row outside shared/mesh-chat gets no seq at all."""
    p = _peer(owner, "order")
    a = psycopg.connect(DSN, options=BOUNDED_LOCK_WAIT)
    b = psycopg.connect(DSN, options=BOUNDED_LOCK_WAIT)
    try:
        key_a = _row(a, p, {"to": _lbl("oa")})
        key_b = _row(b, p, {"to": _lbl("ob")})
        b.commit()
        a.commit()
    finally:
        a.close()
        b.close()
    with owner.cursor() as cur:
        cur.execute("SELECT sm.key, o.seq FROM mesh_commit_order o JOIN shared_memory sm "
                    "ON sm.id = o.memory_id WHERE sm.key = ANY(%s)", ([key_a, key_b],))
        seq = dict(cur.fetchall())
        cur.execute("SELECT count(*) FROM mesh_commit_order o JOIN shared_memory sm "
                    "ON sm.id = o.memory_id WHERE sm.namespace <> %s", (NS,))
        outside = cur.fetchone()[0]
    assert seq[key_b] < seq[key_a], seq
    assert outside == 0


def test_d19_a_stamp_waits_for_the_stamp_before_it_to_commit(owner):
    """The lock in sql_meshychat/023's stamp is what makes seq order COMMIT order. A test-only
    deferred trigger, sorting after the stamp, stalls A for 2 s AFTER A has drawn its seq
    and BEFORE A commits. B inserts and commits meanwhile, and the catch-up runs while A is
    still stalled. With the lock, B's stamp waits for A's commit, so the catch-up cannot see
    B before A and every row is walked. Without it, B commits with the higher seq, the walk
    passes it, and A's lower seq becomes visible behind the watermark: A is never projected.
    CONTROL: the stall really happened (A's commit took at least 1.5 s)."""
    import threading
    import time
    tag = uuid.uuid4().hex[:8]
    fn, trg = f"test_d19_stall_{tag}", f"zz_test_d19_stall_{tag}"
    owner.execute(f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                  f"IF current_setting('meshy.test_d19_stall', true) = 'on' THEN "
                  f"PERFORM pg_sleep(2); END IF; RETURN NULL; END $$")
    owner.execute(f"CREATE CONSTRAINT TRIGGER {trg} AFTER INSERT ON shared_memory "
                  f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {fn}()")
    try:
        p = _peer(owner, "stall")
        me_a, me_b = _lbl("stall-a"), _lbl("stall-b")
        _drain_projection(owner)
        a = psycopg.connect(DSN, options=BOUNDED_LOCK_WAIT)
        b = psycopg.connect(DSN, options=BOUNDED_LOCK_WAIT)
        try:
            a.execute("SET LOCAL meshy.test_d19_stall = 'on'")
            key_a = _row(a, p, {"to": me_a})
            key_b = _row(b, p, {"to": me_b})
            took = {}

            def commit(conn, name):
                t0 = time.monotonic()
                conn.commit()
                took[name] = time.monotonic() - t0

            ta = threading.Thread(target=commit, args=(a, "a"))
            ta.start()
            time.sleep(0.5)                   # A has drawn its seq and is stalled
            tb = threading.Thread(target=commit, args=(b, "b"))
            tb.start()
            time.sleep(0.5)
            P.catch_up(owner)                 # while A is still stalled
            ta.join(10)
            tb.join(10)
        finally:
            # A failure above must not leave A's insert holding shared_memory open, or
            # the DROP TRIGGER below waits on it for ever (review finding 8).
            a.close()
            b.close()
        _drain_projection(owner)
        assert took["a"] >= 1.5, f"control: the stall did not happen ({took})"
        assert ("addressed_to", me_b) in _edges_of(owner, key_b)
        assert ("addressed_to", me_a) in _edges_of(owner, key_a), (
            "A drew its seq before B and committed after the walk passed B: never projected")
    finally:
        owner.execute(f"DROP TRIGGER IF EXISTS {trg} ON shared_memory")
        owner.execute(f"DROP FUNCTION IF EXISTS {fn}()")


# ================================================ D21, two passes over one watermark

def _proj_state(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT watermark_seq, watermark_at, watermark_id, rows_projected "
                    "FROM mesh_edges_projection WHERE projection = %s", (P.PROJECTION_NAME,))
        seq, at, rid, rows = cur.fetchone()
    return {"seq": seq, "at": at, "id": rid, "rows": rows}


def _seq_of(conn, key: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT o.seq FROM mesh_commit_order o JOIN shared_memory sm "
                    "ON sm.id = o.memory_id WHERE sm.key = %s", (key,))
        return cur.fetchone()[0]


def test_d21_a_pass_that_read_an_older_watermark_cannot_set_it_back(owner, monkeypatch):
    """STATUS D21. The lease keeps two JOB ticks apart; `python -m apps.mesh catch-up` takes
    no lease, and a lease can be taken over from a holder that is still running. Two passes
    then read the SAME watermark. Pass A reads one row (max_batch=1); before A projects it,
    pass B on a second connection reads both rows, projects them and commits its position.
    A writes second, with the OLDER position. The saved position must not go back, and A's
    rows, all of which B already counted, must not be counted twice.

    Deterministic, one thread: B runs inside A's pass (hooked at A's `_project`, after A has
    read the watermark and its rows, before it writes anything), so the interleaving is
    fixed rather than hoped for. ONE CLOCK: the rows' created_at come from the database, and
    the test compares only values the database stored. CONTROLS: A read exactly the older
    row, B read both from the same watermark, B's commit landed before A wrote, and a later
    pass still moves the position forward."""
    p = _peer(owner, "d21")
    _drain_projection(owner)
    db_now = owner.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(_dt.timezone.utc)
    me_1, me_2 = _lbl("d21-1"), _lbl("d21-2")
    key_1 = _row(owner, p, {"to": me_1}, created_at=db_now)
    key_2 = _row(owner, p, {"to": me_2},
                 created_at=db_now + _dt.timedelta(milliseconds=1))
    seq_1, seq_2 = _seq_of(owner, key_1), _seq_of(owner, key_2)
    assert seq_1 < seq_2, "control: the rows are walked in this order"
    before = _proj_state(owner)

    a = psycopg.connect(DSN, autocommit=True, options=BOUNDED_LOCK_WAIT)
    b = psycopg.connect(DSN, autocommit=True, options=BOUNDED_LOCK_WAIT)
    real = P._project
    seen: dict = {}

    def interleave(cur, rows):
        if "a_keys" not in seen:
            seen["a_keys"] = [r["key"] for r in rows]
            seen["b"] = P.catch_up(b)                 # the LATER pass, committed first
            seen["after_b"] = _proj_state(owner)
        return real(cur, rows)

    monkeypatch.setattr(P, "_project", interleave)
    refused = None
    try:
        try:
            P.catch_up(a, max_batch=1)                # the EARLIER pass, writes second
        except RuntimeError as exc:
            refused = exc
    finally:
        a.close()
        b.close()
    monkeypatch.setattr(P, "_project", real)

    assert seen["a_keys"] == [key_1], f"control: A read the older row only: {seen}"
    assert seen["b"].rows_read == 2, "control: B read both rows from the same watermark"
    assert seen["after_b"]["seq"] == seq_2, "control: B's position committed before A wrote"

    after = _proj_state(owner)
    assert after["seq"] == seq_2, (
        f"the watermark went BACK from {seq_2} to {after['seq']}: a pass that read an older "
        "position overwrote a newer one")
    assert (after["at"], after["id"]) == (seen["after_b"]["at"], seen["after_b"]["id"]), (
        "the reader-facing key moved when the older pass wrote")
    assert after["rows"] - before["rows"] == 2, (
        f"rows_projected moved by {after['rows'] - before['rows']} for 2 rows: the older "
        "pass counted rows the newer one had already counted")
    assert isinstance(refused, P.ConcurrentWalk), f"the older pass was not refused: {refused!r}"
    assert ("addressed_to", me_1) in _edges_of(owner, key_1), "control: B projected row 1"
    assert ("addressed_to", me_2) in _edges_of(owner, key_2), "control: B projected row 2"

    # POSITIVE CONTROL: the guard refuses only a stale position; a later pass still moves.
    assert P.catch_up(owner).rows_read == 0, "nothing was lost and nothing is re-walked"
    key_3 = _row(owner, p, {"to": _lbl("d21-3")},
                 created_at=db_now + _dt.timedelta(milliseconds=2))
    assert P.catch_up(owner).rows_read == 1
    assert _proj_state(owner)["seq"] == _seq_of(owner, key_3), "a newer pass moved it forward"


def test_d21_a_pass_that_read_more_rows_cannot_count_them_twice(owner, monkeypatch):
    """STATUS D21, the OTHER direction. The older pass read MORE rows than the one that
    committed first, so its write would move the position FORWARD, and a forward-only
    guard (`watermark_seq < new seq`) would let it land and count row 1 a second time.
    The write is a compare-and-set on the position the pass READ, not a monotonic max: A
    reads both rows (max_batch=2); B, on a second connection inside A's pass, reads row 1
    only (max_batch=1) and commits; A writes second and must be refused and roll back.

    Deterministic, one thread, one clock (see the test above). CONTROLS: A read both rows,
    B read row 1 only from the same watermark and committed before A wrote, and the next
    pass walks row 2 exactly once."""
    p = _peer(owner, "d21r")
    _drain_projection(owner)
    db_now = owner.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(_dt.timezone.utc)
    me_1, me_2 = _lbl("d21r-1"), _lbl("d21r-2")
    key_1 = _row(owner, p, {"to": me_1}, created_at=db_now)
    key_2 = _row(owner, p, {"to": me_2},
                 created_at=db_now + _dt.timedelta(milliseconds=1))
    seq_1, seq_2 = _seq_of(owner, key_1), _seq_of(owner, key_2)
    assert seq_1 < seq_2, "control: the rows are walked in this order"
    before = _proj_state(owner)

    a = psycopg.connect(DSN, autocommit=True, options=BOUNDED_LOCK_WAIT)
    b = psycopg.connect(DSN, autocommit=True, options=BOUNDED_LOCK_WAIT)
    real = P._project
    seen: dict = {}

    def interleave(cur, rows):
        if "a_keys" not in seen:
            seen["a_keys"] = [r["key"] for r in rows]
            seen["b"] = P.catch_up(b, max_batch=1)    # the pass that read LESS, first
            seen["after_b"] = _proj_state(owner)
        return real(cur, rows)

    monkeypatch.setattr(P, "_project", interleave)
    refused = None
    try:
        try:
            P.catch_up(a, max_batch=2)                # the pass that read MORE, second
        except RuntimeError as exc:
            refused = exc
    finally:
        a.close()
        b.close()
    monkeypatch.setattr(P, "_project", real)
    after_a = _proj_state(owner)
    rest = P.catch_up(owner)                          # whatever A left, walked once

    assert seen["a_keys"] == [key_1, key_2], f"control: A read both rows: {seen}"
    assert seen["b"].rows_read == 1, "control: B read row 1 only from the same watermark"
    assert seen["after_b"]["seq"] == seq_1, "control: B's position committed before A wrote"

    final = _proj_state(owner)
    assert final["rows"] - before["rows"] == 2, (
        f"double counted: rows_projected moved by {final['rows'] - before['rows']} for 2 "
        "rows; a pass that read the position B had already moved wrote over it")
    assert isinstance(refused, P.ConcurrentWalk), f"the older pass was not refused: {refused!r}"
    assert after_a["seq"] == seq_1, "the refused pass changed the position"
    assert rest.rows_read == 1, "control: the next pass walked row 2, once"
    assert final["seq"] == seq_2, "control: the walk still reached row 2"
    assert ("addressed_to", me_1) in _edges_of(owner, key_1), "control: row 1 projected"
    assert ("addressed_to", me_2) in _edges_of(owner, key_2), "control: row 2 projected"


def test_d21_rebuild_walks_many_batches_through_the_compare_and_set(owner, monkeypatch):
    """STATUS D21. rebuild calls `_advance` once per batch, each with the position the
    PREVIOUS batch wrote. With the default batch (2000) a test database holds one batch,
    so this path never ran; production (~23k rows) runs about a dozen. batch=1 makes every
    row its own batch: every compare-and-set after the first must match the position the
    rebuild itself just wrote, or every production rebuild fails on its second batch.

    CONTROLS: the spy proves one `_advance` per row, starting at 0 and strictly rising
    (the multi-batch path ran); the totals are compared to a count the database makes."""
    p = _peer(owner, "d21b")
    db_now = owner.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(_dt.timezone.utc)
    keys = [_row(owner, p, {"to": _lbl(f"d21b-{i}")},
                 created_at=db_now + _dt.timedelta(milliseconds=i)) for i in range(3)]
    real = P._advance
    from_seqs: list[int] = []

    def spy(cur, rows, *args, **kw):
        from_seqs.append(int(kw["from_seq"]))
        return real(cur, rows, *args, **kw)

    monkeypatch.setattr(P, "_advance", spy)
    report = P.rebuild(owner, batch=1)
    monkeypatch.setattr(P, "_advance", real)

    with owner.cursor() as cur:
        cur.execute("SELECT count(*), max(o.seq) FROM mesh_commit_order o JOIN shared_memory"
                    " sm ON sm.id = o.memory_id WHERE sm.namespace = %s"
                    " AND sm.key LIKE 'msg/%%'", (NS,))
        n, max_seq = cur.fetchone()
    assert n >= 3, "control: at least three rows, so at least three batches"
    assert len(from_seqs) == n, f"control: one batch per row ({len(from_seqs)} vs {n})"
    assert from_seqs[0] == 0 and from_seqs == sorted(set(from_seqs)), (
        f"control: each batch starts where the last one ended: {from_seqs[:5]}...")
    state = _proj_state(owner)
    assert report.rows_read == n and state["rows"] == n, (
        f"rebuild counted {report.rows_read}/{state['rows']} rows of {n}")
    assert state["seq"] == max_seq, f"rebuild stopped at {state['seq']}, the log ends at {max_seq}"
    assert all(_edges_of(owner, k) for k in keys), "control: the seeded rows were projected"
    assert P.catch_up(owner).rows_read == 0, "a batched rebuild left the watermark behind"


# ====================================================================== a reply inherits its parent's thread (073)

def _threads(conn, key: str) -> list[tuple[str, str]]:
    """(dst, src_field) of every in_thread edge of every version of `key`, sorted."""
    with rls.server_internal(conn):
        return sorted(tuple(map(str, r)) for r in conn.execute(
            "SELECT dst, src_field FROM mesh_edges WHERE src_key = %s AND edge_type = 'in_thread'", (key,)).fetchall())


def _all_threads(conn, keys) -> dict:
    return {k: _threads(conn, k) for k in keys}


def _redact(conn, peer, key: str, *, version: int = 1, blank_thread: bool, ns: str = NS) -> dict:
    """Redact one version of `key` with the handler's own `_redact_one`, committed (as test_redaction_delivery does)."""
    from psycopg.rows import dict_row

    from apps.memoryskills import redact
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        redact.serialize_with_projection(cur, ns, blank_thread)       # as handle_redact does, before the row lock
        cur.execute("SELECT id, version, value, signed_bytes, payload_jcs_sha256, created_by_peer_id "
                    "FROM shared_memory WHERE namespace = %s AND key = %s AND version = %s", (ns, key, version))
        row = cur.fetchone()
        return redact._redact_one(cur, row, ns, key, peer["peer_id"], peer["owner_id"], "sent_in_error",
                                  blank_thread, b"the request as signed", f"call-{uuid.uuid4()}", [])


def _pre_key() -> str:
    return f"msg/{int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex}"


def test_a_threadless_reply_inherits_its_parents_thread_whichever_commits_first(owner):
    """memory-bxxxxx's test: a reply with no thread gets its parent's, also when the parent is projected AFTER it,
    and a rebuild (one batch, or one row per batch) gives what the incremental walk gave. A reply to that reply
    inherits down the chain; a reply with a thread of its own keeps only its own.
    CONTROL: before the parent exists nothing is inherited, so the edge comes from the parent and not from a default.
    MUTANT: drop `OR r.dst = ANY(keys)` from the candidates; a parent projected after its reply never reaches it.
    MUTANT: no second round in _inherit_threads; the grandchild never inherits."""
    p = _peer(owner, "inherit")
    t, own_t = _lbl("t-inh"), _lbl("t-own")
    parent = _pre_key()
    reply = _row(owner, p, {"to": "x", "in_reply_to": parent, "body": "written before its parent"})
    grand = _row(owner, p, {"to": "x", "in_reply_to": reply})
    P.catch_up(owner)
    assert _all_threads(owner, [reply, grand]) == {reply: [], grand: []}, "control: nothing to inherit yet"
    own = _row(owner, p, {"to": "x", "in_reply_to": parent, "thread_id": own_t})
    _row(owner, p, {"to": "x", "thread_id": t}, key=parent)
    P.catch_up(owner)
    keys = [parent, reply, grand, own]
    incremental = _all_threads(owner, keys)
    assert incremental == {parent: [(t, "thread_id")], reply: [(t, "inherited")], grand: [(t, "inherited")],
                           own: [(own_t, "thread_id")]}, incremental
    with rls.server_internal(owner):
        assert {h.key for h in G.thread(owner, t).hits} == {parent, reply, grand}
        # The reply's own time, never the pass's: an old reply must not read as fresh activity (memory-bxxxxx).
        stamped, created = owner.execute(
            "SELECT e.recorded_at, sm.created_at FROM mesh_edges e JOIN shared_memory sm ON sm.id = e.src_id "
            "WHERE e.src_key = %s AND e.edge_type = 'in_thread'", (reply,)).fetchone()
    assert stamped == created, (stamped, created)
    P.rebuild(owner)
    assert _all_threads(owner, keys) == incremental, "a rebuild disagreed with the incremental walk"
    P.rebuild(owner, batch=1)
    assert _all_threads(owner, keys) == incremental, "a rebuild one row per batch disagreed"


def test_a_blanked_reply_is_not_given_its_parents_thread_back_before_its_own_edge_is_written(owner):
    """memory-bxxxxx's sequence. The reply inherited the parent's thread. The parent is edited (its version 2 is in the
    log, not yet projected), then the reply is redacted with blank_thread_id, which deletes its edge at once. The walk
    reaches the parent's edit first: the reply has no edge then, but its tombstone declares redacted/<id>, so it does not
    inherit; at the redaction's position it gets that edge, and only that one. A rebuild agrees.
    MUTANT: decide eligibility from the missing edge alone (drop the normalize.thread_of test); the reply ends with two
    in_thread edges, the blanked thread and redacted/<id>."""
    p = _peer(owner, "inh-blank")
    t = _lbl("t-blank")
    parent = _pre_key()
    v1 = uuid.uuid4()
    _row(owner, p, {"to": "x", "thread_id": t, "body": "v1"}, key=parent, rid=v1)
    reply = _row(owner, p, {"to": "x", "in_reply_to": parent, "body": "a reply"})
    grand = _row(owner, p, {"to": "x", "in_reply_to": reply})
    P.catch_up(owner)
    assert _all_threads(owner, [reply, grand]) == {reply: [(t, "inherited")], grand: [(t, "inherited")]}, "control"
    owner.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (v1,))
    _row(owner, p, {"to": "x", "thread_id": t, "body": "v2"}, key=parent, version=2, parent_id=v1)
    record = _redact(owner, p, reply, blank_thread=True)
    assert _all_threads(owner, [reply, grand]) == {reply: [], grand: []}, "the redaction left a copy of the thread id"
    # One position per pass, so the state BETWEEN the parent's edit and the reply's redaction is observable: in one
    # pass the shadowed-edge delete at the redaction's position would hide the wrong edge before anyone read it.
    first = P.catch_up(owner, max_batch=1)
    assert first.rows_read == 1
    assert _threads(owner, reply) == [], "the walk gave the blanked reply its parent's thread back"
    P.catch_up(owner)
    blanked = f"redacted/{record['redaction_id']}"
    incremental = _all_threads(owner, [parent, reply, grand])
    assert incremental == {parent: [(t, "thread_id"), (t, "thread_id")], reply: [(blanked, "thread_id")],
                           grand: []}, incremental
    P.rebuild(owner)
    assert _all_threads(owner, [parent, reply, grand]) == incremental, "a rebuild disagreed"


def test_blanking_a_parents_thread_takes_every_copy_below_it_and_a_rebuild_agrees(owner):
    """The edges replies inherited are copies of the parent's thread id, so blank_thread_id on the parent deletes them,
    down the chain, with the parent's own; a reply with its own thread is left alone. With version 2 of the parent in
    another thread, the replies take that one at the redaction's position, which is what a rebuild gives.
    MUTANT: drop the downstream delete; the replies keep the blanked id until a rebuild.
    MUTANT: walk one level only (no recursion); the grandchild keeps it."""
    p = _peer(owner, "inh-parent")
    t, t2, own_t = _lbl("t-p1"), _lbl("t-p2"), _lbl("t-own")
    parent = _pre_key()
    v1 = uuid.uuid4()
    _row(owner, p, {"to": "x", "thread_id": t, "body": "v1"}, key=parent, rid=v1)
    reply = _row(owner, p, {"to": "x", "in_reply_to": parent})
    grand = _row(owner, p, {"to": "x", "in_reply_to": reply})
    own = _row(owner, p, {"to": "x", "in_reply_to": parent, "thread_id": own_t})
    P.catch_up(owner)
    keys = [reply, grand, own]
    assert _all_threads(owner, keys) == {reply: [(t, "inherited")], grand: [(t, "inherited")],
                                         own: [(own_t, "thread_id")]}, "control"

    record = _redact(owner, p, parent, blank_thread=True)
    assert _all_threads(owner, keys) == {reply: [], grand: [], own: [(own_t, "thread_id")]}
    P.catch_up(owner)
    single = _all_threads(owner, [parent] + keys)
    assert single == {parent: [(f"redacted/{record['redaction_id']}", "thread_id")], reply: [], grand: [],
                      own: [(own_t, "thread_id")]}, single
    P.rebuild(owner)
    assert _all_threads(owner, [parent] + keys) == single, "a rebuild disagreed"

    # The same, where the parent has a second version in another thread: the replies inherit that one.
    p2 = _peer(owner, "inh-parent2")
    parent2, w1 = _pre_key(), uuid.uuid4()
    _row(owner, p2, {"to": "x", "thread_id": t, "body": "v1"}, key=parent2, rid=w1)
    reply2 = _row(owner, p2, {"to": "x", "in_reply_to": parent2})
    P.catch_up(owner)
    owner.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (w1,))
    _row(owner, p2, {"to": "x", "thread_id": t2, "body": "v2, in another thread"}, key=parent2, version=2,
         parent_id=w1)
    P.catch_up(owner)
    assert _threads(owner, reply2) == [(t, "inherited")], "a reply keeps the parent's first thread across an edit"
    _redact(owner, p2, parent2, version=1, blank_thread=True)
    P.catch_up(owner)
    assert _threads(owner, reply2) == [(t2, "inherited")], _threads(owner, reply2)
    P.rebuild(owner)
    assert _threads(owner, reply2) == [(t2, "inherited")], "a rebuild disagreed"


def test_a_reply_written_before_its_parent_and_the_parents_edit_keeps_the_first_thread(owner):
    """Why the LOWEST version: the reply, the parent and the parent's edit into another thread all commit before a
    rebuild reads them in one batch. The incremental walk gave the reply version 1's thread (version 2 did not exist
    yet), and the rebuild must give the same one.
    MUTANT: order the parent's versions descending; the rebuild hands the reply version 2's thread."""
    p = _peer(owner, "inh-first")
    t1, t2 = _lbl("t-first"), _lbl("t-second")
    parent, v1 = _pre_key(), uuid.uuid4()
    reply = _row(owner, p, {"to": "x", "in_reply_to": parent})
    P.catch_up(owner)
    _row(owner, p, {"to": "x", "thread_id": t1, "body": "v1"}, key=parent, rid=v1)
    P.catch_up(owner)
    owner.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (v1,))
    _row(owner, p, {"to": "x", "thread_id": t2, "body": "v2"}, key=parent, version=2, parent_id=v1)
    P.catch_up(owner)
    assert _threads(owner, reply) == [(t1, "inherited")], "incremental: version 1 was the only one"
    P.rebuild(owner)
    assert _threads(owner, reply) == [(t1, "inherited")], "a rebuild in one batch took another version's thread"


def test_blanking_a_parents_thread_stops_at_a_reply_with_its_own_thread(owner):
    """memory-bxxxxx's mutant E. P is in thread T; R replies to P with its own thread T2; C replies to R with none, so C
    inherits T2 from R, not T. Blanking P's thread is not C's business: the walk below P stops at R, and C keeps T2,
    incrementally and on a rebuild. (Were it deleted, nothing would give it back: at P's redaction position only replies to
    P are candidates, and R has its own thread.)
    MUTANT E: drop the inherited-edge JOIN from _INHERITED_BELOW, so the walk passes R; C loses T2 and the rebuild gives it."""
    p = _peer(owner, "inh-stop")
    t, t2 = _lbl("t-stop-p"), _lbl("t-stop-r")
    parent = _pre_key()
    _row(owner, p, {"to": "x", "thread_id": t}, key=parent)
    r = _row(owner, p, {"to": "x", "in_reply_to": parent, "thread_id": t2})
    c = _row(owner, p, {"to": "x", "in_reply_to": r})
    P.catch_up(owner)
    assert _all_threads(owner, [r, c]) == {r: [(t2, "thread_id")], c: [(t2, "inherited")]}, "control"
    _redact(owner, p, parent, blank_thread=True)
    P.catch_up(owner)
    incremental = _all_threads(owner, [r, c])
    assert incremental == {r: [(t2, "thread_id")], c: [(t2, "inherited")]}, incremental
    P.rebuild(owner)
    assert _all_threads(owner, [r, c]) == incremental, "a rebuild disagreed"


def _inherited(conn) -> set[tuple]:
    with rls.server_internal(conn):
        return {tuple(map(str, r)) for r in conn.execute(
            "SELECT src_id, dst FROM mesh_edges WHERE edge_type = 'in_thread' AND src_field = 'inherited'").fetchall()}


def test_backfill_in_thread_inherited_gives_history_what_a_rebuild_gives(owner):
    """kxxx and memory-bxxxxx: production's history gets 073 from `backfill --types in_thread_inherited`, not a full
    rebuild. A chain written child first (the reply, then its threadless parent, then the grandparent with the thread),
    walked ONE ROW PER BATCH, so the child's batch comes before anything it could inherit: the later batches reach it
    through the keys that just inherited. The result is the rebuild's inherited set for EVERY row in the database, the
    watermark does not move, a second run writes nothing, and a closes/mentions backfill writes no inherited edge.
    MUTANT: leave in_thread_inherited out of backfill's batch loop; nothing comes back."""
    p = _peer(owner, "inh-backfill")
    t = _lbl("t-backfill")
    grand_key, parent_key = _pre_key(), _pre_key()
    child = _row(owner, p, {"to": "x", "in_reply_to": parent_key})
    _row(owner, p, {"to": "x", "in_reply_to": grand_key}, key=parent_key)
    _row(owner, p, {"to": "x", "thread_id": t}, key=grand_key)
    P.rebuild(owner)
    rebuilt = _inherited(owner)
    assert _all_threads(owner, [parent_key, child]) == {parent_key: [(t, "inherited")], child: [(t, "inherited")]}

    with rls.projection_writer(owner):                     # the projection as it was before 073
        owner.execute("DELETE FROM mesh_edges WHERE edge_type = 'in_thread' AND src_field = 'inherited'")
    before = _proj_state(owner)
    assert P.backfill(owner, types=("closes", "mentions"), batch=1).edges_written == 0
    assert _inherited(owner) == set(), "a closes/mentions backfill wrote inherited edges"
    first = P.backfill(owner, types=("in_thread_inherited",), batch=1)
    assert first.edges_written == len(rebuilt) >= 2, (first, len(rebuilt))
    assert _inherited(owner) == rebuilt, "the backfill disagreed with the rebuild"
    assert P.backfill(owner, types=("in_thread_inherited",), batch=1).edges_written == 0
    assert _proj_state(owner) == before, "the backfill moved the projection's state"


def test_a_blanking_redaction_cannot_land_between_a_passs_read_and_its_insert(owner, monkeypatch):
    """Codex on #106, via kxxx. Two connections. The parent is projected first, so a later pass reads its thread from
    mesh_edges without its row in the batch. That pass is paused after reading its candidates and before inserting,
    while another connection blanks the parent's thread. With BLANKING_LOCK the redaction waits for the pass to commit,
    and its cascade then removes the copy the pass wrote. The reply ends with no copy of the withdrawn thread, and a
    rebuild agrees. CONTROL: the pause was reached with a candidate, and the redaction finished.
    MUTANT: the pass takes no lock (drop hold_against_blanking from catch_up); the redaction commits inside the pause
    and the pass copies the withdrawn thread onto the reply for good.
    MUTANT: serialize_with_projection takes nothing; the same."""
    import threading

    p = _peer(owner, "inh-race")
    t = _lbl("t-race")
    parent = _pre_key()
    _row(owner, p, {"to": "x", "thread_id": t}, key=parent)
    P.catch_up(owner)
    reply = _row(owner, p, {"to": "x", "in_reply_to": parent})
    done = {}

    def blank_elsewhere():
        with psycopg.connect(DSN, autocommit=True) as other:
            done["record"] = _redact(other, p, parent, blank_thread=True)

    worker = threading.Thread(target=blank_elsewhere)

    class Paused:
        """The projection's view of normalize. Its first eligibility check, after the candidate read and before the
        insert, starts the redaction on the other connection and gives it three seconds to finish."""
        fired = False

        def __getattr__(self, name):
            return getattr(N, name)

        def thread_of(self, value):
            if not Paused.fired:
                Paused.fired = True
                worker.start()
                worker.join(timeout=3.0)
            return N.thread_of(value)

    monkeypatch.setattr(P, "normalize", Paused())
    P.catch_up(owner)
    worker.join(timeout=30)
    monkeypatch.setattr(P, "normalize", N)
    assert Paused.fired, "control: the pass reached the inheritance with a candidate"
    assert not worker.is_alive() and "record" in done, "control: the redaction finished"
    assert _threads(owner, reply) == [], "the pass copied the withdrawn thread onto the reply"
    P.catch_up(owner)
    single = _all_threads(owner, [parent, reply])
    assert single == {parent: [(f"redacted/{done['record']['redaction_id']}", "thread_id")], reply: []}, single
    P.rebuild(owner)
    assert _all_threads(owner, [parent, reply]) == single, "a rebuild disagreed"


def _same_key_elsewhere(conn, peer, key: str, value: dict) -> str:
    """A row under `key` in a namespace of its own, not shared/mesh-chat. Keys are unique only within a namespace."""
    other = f"test/inherit-ns-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id, claimant_plane, "
                    "ephemeral, retention_policy) VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                    (other, peer["peer_id"], peer["owner_id"]))
    rid = uuid.uuid4()
    history_rows.insert_seed(conn, sr.row(peer["peer_id"], peer["owner_id"], other, key, value, rid=rid,
                                           call_id=f"call-{rid}", author_label=peer["label"]))
    return other


def test_blanking_the_same_key_in_another_namespace_touches_no_mesh_edge(owner, monkeypatch):
    """Codex on #106, via kxxx. The mesh's msg/P has a reply that inherited its thread; a row with the SAME key in another
    namespace is blanked. That redaction takes no lock (it is not the mesh's), so its walk must not reach the mesh's
    edges: the reply keeps its thread, and the redaction's postcondition does not trip over it. Then the same with a
    pass paused between its candidate read and its insert: the other namespace's redaction does not wait for the pass,
    and the reply still ends with its thread.
    MUTANT: drop the namespace predicate from _INHERITED_BELOW's root; the mesh reply loses its thread.
    MUTANT: drop it from _INHERITED_DIRECTLY_BELOW; the other namespace's redaction is refused (E_INTERNAL)."""
    import threading

    p = _peer(owner, "inh-ns")
    t = _lbl("t-ns")
    parent = _pre_key()
    _row(owner, p, {"to": "x", "thread_id": t}, key=parent)
    reply = _row(owner, p, {"to": "x", "in_reply_to": parent})
    P.catch_up(owner)
    assert _threads(owner, reply) == [(t, "inherited")], "control"
    other = _same_key_elsewhere(owner, p, parent, {"thread_id": "another-namespaces-thread", "body": "not mesh mail"})
    _redact(owner, p, parent, blank_thread=True, ns=other)
    assert _threads(owner, reply) == [(t, "inherited")], "blanking another namespace's msg/P took the mesh reply's thread"
    P.catch_up(owner)
    assert _threads(owner, reply) == [(t, "inherited")]

    # The same with a pass paused mid-way: a second mesh parent and its reply, the reply's candidate read done.
    parent2 = _pre_key()
    _row(owner, p, {"to": "x", "thread_id": t}, key=parent2)
    P.catch_up(owner)
    reply2 = _row(owner, p, {"to": "x", "in_reply_to": parent2})
    other2 = _same_key_elsewhere(owner, p, parent2, {"thread_id": "elsewhere", "body": "not mesh mail"})
    done = {}

    def blank_elsewhere():
        with psycopg.connect(DSN, autocommit=True) as conn:
            done["record"] = _redact(conn, p, parent2, blank_thread=True, ns=other2)

    worker = threading.Thread(target=blank_elsewhere)

    class Paused:
        fired = False
        finished_inside = False

        def __getattr__(self, name):
            return getattr(N, name)

        def thread_of(self, value):
            if not Paused.fired:
                Paused.fired = True
                worker.start()
                worker.join(timeout=10.0)
                Paused.finished_inside = not worker.is_alive()
            return N.thread_of(value)

    monkeypatch.setattr(P, "normalize", Paused())
    P.catch_up(owner)
    worker.join(timeout=30)
    monkeypatch.setattr(P, "normalize", N)
    assert Paused.fired and "record" in done, "control: the pass paused and the redaction finished"
    assert Paused.finished_inside, "another namespace's redaction waited for a mesh projection pass"
    assert _threads(owner, reply2) == [(t, "inherited")]
    P.rebuild(owner)
    assert _all_threads(owner, [reply, reply2]) == {reply: [(t, "inherited")], reply2: [(t, "inherited")]}


def test_a_redacted_reply_answers_nothing_from_either_side(owner):
    """a reviewer's cherry-pick report P1.1 (13 Nov): a redacted reply's tombstone keeps the routing fields
    (apps/memoryskills/redact.py ROUTING_FIELDS, in_reply_to and thread_id included), so without a redacted filter
    an ask whose only answer was redacted read 'answered' forever, and the tombstone in its thread read 'active'.
    Here my reply's head IS the tombstone. Both asks are open again, in owed_acks and in awaiting.

    MUTANTS, each in owed_acks (_OWED_DIRECT, _OWED_ACTIVITY) or in awaiting (direct, activity): drop the redacted
    filter and the first ask reads answered or the second reads active."""
    asker, owing = _peer(owner, "redact-ask"), _peer(owner, "redact-me")
    me, them = _lbl("redact-me"), _lbl("redact-ask")
    now = _dt.datetime.now(_dt.timezone.utc)
    asker = dict(asker, label=them)
    direct_thread, active_thread = _lbl("rdirect"), _lbl("ractive")
    direct_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": direct_thread}, created_at=now)
    active_ask = _row(owner, asker, {"to": [me], "requires_ack": True, "thread_id": active_thread}, created_at=now)
    owing = dict(owing, label=me)
    for n, (thread, extra) in enumerate(((direct_thread, {"in_reply_to": direct_ask}), (active_thread, {}))):
        key = f"msg/{int((now + _dt.timedelta(seconds=2 + n)).timestamp() * 1000)}-{uuid.uuid4().hex}"
        v1 = uuid.uuid4()
        _row(owner, owing, dict({"to": [them], "thread_id": thread, "body": "words"}, **extra), key=key,
             version=1, is_head=False, rid=v1, created_at=now + _dt.timedelta(seconds=2 + n))
        _row(owner, owing, dict({"redacted": True, "reason_code": "test", "redacted_at": now.isoformat(),
                                 "to": [them], "thread_id": thread}, **extra), key=key, version=2, is_head=True,
             parent_id=v1, created_at=now + _dt.timedelta(seconds=4 + n))
    P.rebuild(owner)
    with rls.server_internal(owner):
        owed = {o.hit.key: o.state for o in G.owed_acks(owner, me, limit=None,
                                                         states=["open", "active", "answered", "closed"])}
        waiting = {w.hit.key: w.state for w in G.awaiting(owner, them, limit=None,
                                                          states=["open", "active", "answered", "closed"])}
    assert (owed[direct_ask], owed[active_ask]) == ("open", "open")
    assert (waiting[direct_ask], waiting[active_ask]) == ("open", "open")
