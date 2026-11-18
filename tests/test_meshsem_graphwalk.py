"""The graph walker (divergence 060, docs/design/wiki-in-graph.md): graph namespaces are embedded in
chunks and lexed, reconciled by set, and never leak into the mail search's indexes.

Providers are the deterministic fake of tests/fixtures/meshsem_fake.py over real HTTP, as in
tests/test_meshsem_embedjob.py, whose fixtures this reuses. Each test claims its OWN namespace, so
the walker's set comparison sees only the rows the test wrote.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import numpy as np
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_meshsem_embedjob import Env, app, env, owner  # noqa: E402,F401  (fixtures; django.setup)

from django.test import override_settings  # noqa: E402

from apps.core import rls  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE  # noqa: E402
from apps.meshsem import graphtext, graphwalk, lexical, search, spaces  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

LONG = "\n\n".join(f"Paragraph {i}. " + "The quick brown fox jumps over the lazy dog. " * 12 for i in range(8))


# ------------------------------------------------------------------ the text rules (no database)

def test_the_longest_content_class_string_wins_and_a_longer_field_outside_it_does_not():
    value = {"body_md": "the entry " * 20, "summary": "short", "review": "x" * 5000, "title": "T"}
    field, text = graphtext.row_text(value)
    assert field == "body_md" and text == "T\n\n" + "the entry " * 20


def test_the_names_break_a_tie_in_their_order():
    assert graphtext.row_text({"summary": "abcd", "body": "wxyz"})[0] == "body"


@pytest.mark.parametrize("value", [{"data": 1}, {"body_md": "   "}, {"body": 7}, [], "text", None])
def test_a_row_without_a_content_class_string_has_no_text(value):
    assert graphtext.row_text(value) is None


def test_chunks_never_exceed_the_window_cover_the_text_and_overlap():
    spans = graphtext.chunk_spans(LONG)
    assert len(spans) > 1 and all(b - a <= graphtext.MAX_CHARS for a, b in spans)
    assert spans[0][0] == 0 and spans[-1][1] == len(LONG)
    for (a0, b0), (a1, _b1) in zip(spans, spans[1:]):
        assert a1 == b0 - graphtext.OVERLAP, "consecutive chunks overlap by exactly OVERLAP"


def test_a_break_prefers_the_paragraph_then_the_sentence():
    spans = graphtext.chunk_spans(LONG)
    assert LONG[:spans[0][1]].endswith("\n\n"), "the first chunk should end at a paragraph break"
    no_para = "Sentence number one is here. " * 80
    first = graphtext.chunk_spans(no_para)[0]
    assert no_para[:first[1]].endswith(". ")


def test_the_measured_longest_entry_yields_the_count_the_design_predicts():
    assert len(graphtext.chunk_spans("word " * 11406)) == 44          # 57,030 chars (design 4.2)


def test_a_short_text_is_one_whole_chunk():
    assert graphtext.chunks("one line") == ["one line"]


@pytest.mark.parametrize("raw,expected", [
    (None, (MESH_CHAT_NAMESPACE,)),
    ("shared/wiki", ("shared/wiki",)),
    ("shared/mesh-chat, shared/wiki,shared/wiki", ("shared/mesh-chat", "shared/wiki")),
    (["shared/wiki", " "], ("shared/wiki",)),
])
def test_the_setting_parses_and_the_walker_never_takes_the_mail(raw, expected):
    with override_settings(MESHY_GRAPH_NAMESPACES=raw):
        assert graphwalk.graph_namespaces() == expected
        assert MESH_CHAT_NAMESPACE not in graphwalk.walked_namespaces()


def test_the_walker_refuses_the_mail_namespace(owner):
    with pytest.raises(ValueError):
        graphwalk.catch_up_namespace(owner, MESH_CHAT_NAMESPACE)


# ------------------------------------------------------------------ the database

def _claim(owner) -> tuple[str, dict]:
    p = db.peer(owner, "graph")
    ns = f"test/graph-{uuid.uuid4().hex[:10]}"
    with owner.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id,"
                    " claimant_plane, ephemeral, retention_policy) VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                    (ns, p["peer_id"], p["owner_id"]))
    return ns, p


def _row(owner, p, ns, key, value, *, policy="allowed", version=1, parent_id=None):
    rid = uuid.uuid4()
    sr.insert(owner, sr.row(p["peer_id"], p["owner_id"], ns, key, value, rid=rid, call_id=f"call-{rid}",
                            version=version, parent_id=parent_id, is_head=True, author_label=p["label"],
                            embed_policy=policy))
    return rid


def _retire(owner, rid):
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (rid,))


def _vectors(owner, space, ns) -> dict:
    with owner.cursor() as cur:
        cur.execute("SELECT src_id, chunk_no, namespace FROM mesh_embeddings WHERE space = %s AND namespace = %s "
                    "ORDER BY src_id, chunk_no", (space.name, ns))
        out: dict[str, list] = {}
        for sid, n, got_ns in cur.fetchall():
            assert got_ns == ns
            out.setdefault(str(sid), []).append(n)
        return out


def _lexed(owner, space, ns) -> set:
    with owner.cursor() as cur:
        cur.execute("SELECT src_id FROM mesh_lexical WHERE space = %s AND namespace = %s", (space.name, ns))
        return {str(r[0]) for r in cur.fetchall()}


def _tick(owner, ns, **kw):
    return graphwalk.catch_up_namespace(owner, ns, **kw)


def test_the_default_setting_walks_nothing(owner, env):
    with override_settings(MESHY_GRAPH_NAMESPACES=None):
        assert graphwalk.catch_up(owner) == []


def test_a_graph_namespace_is_embedded_in_chunks_and_lexed_and_the_rest_is_counted(owner, env):
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    long_id = _row(owner, p, ns, "wiki/long", {"body_md": LONG, "title": "Long"})
    short_id = _row(owner, p, ns, "short-slug", {"body": "a short entry"})
    data_id = _row(owner, p, ns, "wiki/data", {"status": "ok", "count": 3})
    gated_id = _row(owner, p, ns, "wiki/gated", {"body_md": "kept out"}, policy="forbidden")
    r = _tick(owner, ns)
    expected_chunks = len(graphtext.chunks(graphtext.row_text({"body_md": LONG, "title": "Long"})[1]))
    assert (r.heads, r.embedded_rows, r.embedded_chunks) == (4, 2, expected_chunks + 1)
    assert (r.skipped_no_content, r.skipped_forbidden, r.pending, r.stopped) == (1, 1, 0, None)
    vecs = _vectors(owner, env.space, ns)
    assert vecs == {str(long_id): list(range(expected_chunks)), str(short_id): [0]}
    assert _lexed(owner, env.space, ns) == {str(long_id), str(short_id)}
    assert str(data_id) not in vecs and str(gated_id) not in vecs
    again = _tick(owner, ns)
    assert (again.embedded_rows, again.lexed, again.unchanged, again.deleted_rows) == (0, 0, 2, 0), "idempotent"


def test_the_mail_indexes_load_no_row_of_a_graph_namespace(owner, env):
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    rid = _row(owner, p, ns, "wiki/isolated", {"body_md": LONG})
    assert _tick(owner, ns).embedded_rows == 1
    idx = search.VectorIndex(env.space)
    lex = lexical.LexicalIndex(env.space.name)
    with rls.server_internal(owner):
        idx.refresh(owner, ceiling=100000)
        lex.refresh(owner, ceiling=100000)
    assert str(rid) not in idx.pos and str(rid) not in lex.pos
    wiki_idx = search.VectorIndex(env.space, namespace=ns)          # control: the rows are there
    with rls.server_internal(owner):
        wiki_idx.refresh(owner, ceiling=100000)
    assert str(rid) in wiki_idx.pos


def test_a_new_version_replaces_the_old_ones_derived_rows(owner, env):
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    v1 = _row(owner, p, ns, "wiki/versioned", {"body_md": "first text"})
    _tick(owner, ns)
    _retire(owner, v1)
    v2 = _row(owner, p, ns, "wiki/versioned", {"body_md": "second text, longer"}, version=2, parent_id=v1)
    r = _tick(owner, ns)
    assert r.deleted_rows == 1 and r.embedded_rows == 1
    assert set(_vectors(owner, env.space, ns)) == {str(v2)} and _lexed(owner, env.space, ns) == {str(v2)}


def test_a_head_blanked_in_place_loses_its_derived_rows(owner, env):
    """A redaction blanks a head in place (sql_meshychat/027). Whatever path blanked it, the walker
    finds a head with no content that still holds derived rows, and deletes them (kxxx's review of
    the design, 4.3 step 2)."""
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    rid = _row(owner, p, ns, "wiki/redacted", {"body_md": LONG})
    assert _tick(owner, ns).embedded_rows == 1
    with owner.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_redaction (memory_id, namespace, key, version, redacted_by_peer_id,"
                    " redacted_by_owner_id, acted_as, reason_code, request_signed_bytes) VALUES"
                    " (%s,%s,%s,1,%s,%s,'author','client_name','\\x00') RETURNING redacted_at",
                    (rid, ns, "wiki/redacted", p["peer_id"], p["owner_id"]))
        (at,) = cur.fetchone()
        cur.execute("UPDATE shared_memory SET value = %s::jsonb, signed_bytes = NULL, tags = ARRAY['redacted']::text[],"
                    " provenance_jsonb = (provenance_jsonb - 'payload') || jsonb_build_object('payload_redacted', true,"
                    "  'payload', jsonb_build_object('call_id', provenance_jsonb -> 'payload' -> 'call_id'))"
                    " WHERE id = %s", (json.dumps({"redacted": True, "reason_code": "client_name",
                                                  "redacted_at": at.isoformat()}), rid))
    r = _tick(owner, ns)
    assert (r.deleted_rows, r.skipped_no_content) == (1, 1)
    assert _vectors(owner, env.space, ns) == {} and _lexed(owner, env.space, ns) == set()


def test_closing_the_gate_removes_the_forbidden_rows(owner, env):
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=True)
    rid = _row(owner, p, ns, "wiki/forbidden", {"body_md": "embedded while open"}, policy="forbidden")
    assert _tick(owner, ns).embedded_rows == 1
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    r = _tick(owner, ns)
    assert (r.deleted_rows, r.skipped_forbidden) == (1, 1) and str(rid) not in _vectors(owner, env.space, ns)


def test_a_row_is_written_whole_or_not_at_all_and_the_lexical_arm_does_not_wait(owner, env):
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    first = _row(owner, p, ns, "wiki/a", {"body_md": LONG})
    second = _row(owner, p, ns, "wiki/b", {"body_md": LONG + " more"})
    r = _tick(owner, ns, embed_per_tick=1)          # smaller than one row: the first row still goes
    assert (r.embedded_rows, r.pending, r.stopped, r.more) == (1, 1, "embed_bound", True)
    vecs = _vectors(owner, env.space, ns)
    assert list(vecs) == [str(first)] and len(vecs[str(first)]) == len(graphtext.chunks(LONG))
    assert _lexed(owner, env.space, ns) == {str(first), str(second)}
    r2 = _tick(owner, ns, embed_per_tick=1)
    assert r2.embedded_rows == 1 and set(_vectors(owner, env.space, ns)) == {str(first), str(second)}


def test_an_outage_writes_no_vector_and_still_lexes(owner, env):
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    _row(owner, p, ns, "wiki/warm", {"body_md": "records the reference"})
    _tick(owner, ns)
    env.settings([env.embed("down", env.hosts["down"])], gate=False)
    rid = _row(owner, p, ns, "wiki/out", {"body_md": "during the outage"})
    r = _tick(owner, ns)
    assert r.stopped == "provider_unavailable" and r.pending == 1
    assert str(rid) not in _vectors(owner, env.space, ns) and str(rid) in _lexed(owner, env.space, ns)


def test_the_database_refuses_a_second_chunk_of_a_mail_row(owner, env):
    """sql_meshychat/030's mail CHECK. CONTROL first: the same insert as chunk 0 is admitted, so the
    refusal of chunk 1 is that constraint's and not a malformed row's (a first version of this test
    used an invalid digest and passed with the constraint removed)."""
    p = db.peer(owner, "mailchunk")
    db.ensure_namespace(owner)
    m = db.row(owner, p, {"body": "a mail"}, embed_policy="allowed")
    insert = ("INSERT INTO mesh_embeddings (src_id, src_key, src_version, space, dims, text_sha256, vec,"
              " provider_id, recorded_at, chunk_no) VALUES (%s,%s,1,%s,%s,%s,%s,'p',now(),%s)")
    vec = spaces.encode(np.ones(db.DIMS, dtype="float32"))
    with owner.cursor() as cur:
        cur.execute(insert, (m["id"], m["key"], env.space.name, db.DIMS, "a" * 64, vec, 0))
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        with owner.cursor() as cur:
            cur.execute(insert, (m["id"], m["key"], env.space.name, db.DIMS, "b" * 64, vec, 1))
    assert exc.value.diag.constraint_name == "mesh_embeddings_chunk_no_chk"


def test_an_outage_in_the_middle_of_a_row_writes_none_of_it(owner, env, monkeypatch):
    """A row whose chunks span several calls, the second of which fails: none of its chunks is
    written (a partly embedded row would read as derived and rank by the part it has)."""
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    _row(owner, p, ns, "wiki/warm2", {"body_md": "records the reference"})
    _tick(owner, ns)
    rid = _row(owner, p, ns, "wiki/split", {"body_md": LONG})
    assert len(graphtext.chunks(LONG)) > 2, "control: the row spans more than one call"
    monkeypatch.setattr(graphwalk, "SUB_BATCH", 2)
    real = graphwalk.providers.embed_in_space
    calls = []

    def second_call_down(space, texts, reference, **kw):
        calls.append(len(texts))
        if len(calls) >= 2:
            raise graphwalk.providers.EmbedUnavailable("down mid-row", attempts=[])
        return real(space, texts, reference, **kw)
    monkeypatch.setattr(graphwalk.providers, "embed_in_space", second_call_down)
    r = _tick(owner, ns)
    assert len(calls) == 2 and r.stopped == "provider_unavailable" and r.pending == 1
    assert str(rid) not in _vectors(owner, env.space, ns), "a partly embedded row was written"
    assert str(rid) in _lexed(owner, env.space, ns)


def test_the_registered_job_runs_as_the_app_role(owner, app, env):
    from apps.jobs import registry, runner
    ns, p = _claim(owner)
    env.settings([env.embed("same", env.hosts["same"])], gate=False)
    rid = _row(owner, p, ns, "wiki/job", {"body_md": "through the runner"})
    job = next(j for j in registry.JOBS if j.name == "mesh_graph_embed")
    with override_settings(MESHY_GRAPH_NAMESPACES=f"{MESH_CHAT_NAMESPACE},{ns}"):
        result = job.run(app, job)
    assert result.rows >= 1 and str(rid) in _vectors(owner, env.space, ns)
    assert runner is not None


def test_the_cli_runs_the_walker_as_a_module_entry_point(owner):
    """`python -m apps.meshsem graph-catch-up` in a fresh process, as the app role: with the setting at
    its default it says there is nothing to do; with a (claimed, empty) graph namespace listed in the
    environment it prints that namespace's report."""
    import os
    import subprocess

    from tests.test_meshsem_embedjob import DSN
    base = {k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "DJANGO_SETTINGS_MODULE", "MESHY_DSN", "MESHY_GRAPH_NAMESPACES")}
    cwd = str(Path(__file__).resolve().parent.parent)
    argv = [sys.executable, "-m", "apps.meshsem", "--dsn", DSN, "graph-catch-up"]
    out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=base, timeout=120)
    assert out.returncode == 0 and "nothing to do" in out.stdout, (out.returncode, out.stdout, out.stderr[-400:])
    ns, _p = _claim(owner)
    out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120,
                         env=dict(base, MESHY_GRAPH_NAMESPACES=f"{MESH_CHAT_NAMESPACE},{ns}"))
    assert out.returncode == 0, (out.stdout, out.stderr[-400:])
    assert f"graph-catch-up [{ns} in " in out.stdout and "heads 0" in out.stdout, out.stdout
