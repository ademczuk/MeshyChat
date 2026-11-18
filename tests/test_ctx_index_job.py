"""v1.1 step 6, PR B: the transcript-context index job (apps/meshsem/ctxjob.py over sql_meshychat/047).

One test per acceptance line of docs/design/context-v1.1-transcript-ingest.md step 6, plus pxx's 2a94c2 additions, each
against a control. Providers are the deterministic fake of tests/fixtures/meshsem_fake.py over real HTTP, as in
tests/test_meshsem_embedjob.py, whose fixtures this reuses. Each test writes its OWN context namespace (made the
configured one under tests/test_ctx_commit_order.py's lock) and its own fake space, so the watermark starts at 0 and
the walk sees only this test's manifests. Rows are inserted as the database owner (signed bytes, no context validation):
the walker reads what memory.write would have stored.

Run:  pytest tests/test_ctx_index_job.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_meshsem_embedjob import DSN, Env, app, env, owner  # noqa: E402,F401  (fixtures; django.setup)
from apps.meshsem import ctxjob, graphtext  # noqa: E402
from tests import test_mesh_authorship as mesh  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

CONFIG_LOCK = (1835233133, 990)          # tests/test_ctx_commit_order.py's: the config is one shared row
T0 = "2026-10-24T08:00:00.000Z"
LONG = "\n\n".join(f"Paragraph {i}. " + "The quick brown fox jumps over the lazy dog. " * 12 for i in range(6))


# ------------------------------------------------------------------ the text rules (no database)

def _item(ordinal, kind, text, time=T0):
    return {"ordinal": ordinal, "item": kind, "text": text, "time": time}


def test_a_turn_starts_at_each_user_text_and_omitted_items_carry_nothing():
    items = [_item(0, "assistant_text", "before any user"), _item(1, "user_text", "question one"),
             _item(2, "assistant_text", "answer one"), {"ordinal": 3, "item": "omitted", "time": T0},
             _item(4, "user_text", "question two"), _item(5, "tool_exchange", "ran a tool")]
    chunks = ctxjob.turn_chunks(items)
    assert [(c.first_ord, c.last_ord) for c in chunks] == [(0, 0), (1, 2), (4, 5)]
    assert chunks[1].text == "[user_text]\nquestion one\n\n[assistant_text]\nanswer one"
    assert all("omitted" not in c.text for c in chunks)
    assert [c.chunk_no for c in chunks] == [0, 1, 2]
    # Control: input order does not matter, ordinals decide.
    assert ctxjob.turn_chunks(list(reversed(items))) == chunks


def test_a_long_turn_is_cut_with_overlap_and_each_chunk_names_the_items_it_covers():
    items = [_item(10, "user_text", "short question"), _item(11, "assistant_text", LONG),
             _item(12, "assistant_text", "the last word")]
    chunks = ctxjob.turn_chunks(items)
    assert len(chunks) > 1 and all(len(c.text) <= graphtext.MAX_CHARS for c in chunks)
    assert chunks[0].first_ord == 10 and chunks[-1].last_ord == 12
    for a, b in zip(chunks, chunks[1:]):
        assert b.char_start == a.char_end - ctxjob.CHUNK_OVERLAP
    # Provenance is exact: each chunk's text is its span of the deterministic turn text.
    turn = "\n\n".join(f"[{v['item']}]\n{v['text']}" for v in items)
    assert all(turn[c.char_start:c.char_end] == c.text for c in chunks)


def test_offsets_are_code_points_and_round_trip_non_ascii_text():
    """pxx 3231fb INFO: accents, CJK, emoji, combining marks and Thai keep exact spans."""
    text = "Café, naïve façade. 漢字テキスト. 🙂👍🏽 e\u0301 ภาษาไทย. " * 40
    chunks = ctxjob.turn_chunks([_item(0, "user_text", "q"), _item(1, "assistant_text", text)])
    turn = "[user_text]\nq\n\n[assistant_text]\n" + text
    assert len(chunks) > 1 and all(turn[c.char_start:c.char_end] == c.text for c in chunks)


def test_a_malformed_manifest_is_unreadable_not_a_crash():
    for bad in (None, {"kind": "item"}, {"kind": "manifest", "seat": "s"},
                {"kind": "manifest", "seat": "s", "src": "x", "gen": 1, "batch": 1, "items": [[2, None], [1, None]]}):
        with pytest.raises(ctxjob.Unreadable):
            ctxjob.manifest_coordinates(bad)


# ------------------------------------------------------------------ the walk (database, fake providers)

@pytest.fixture()
def world(owner, env, monkeypatch):
    """A context namespace made the configured one, the job switched on with the gate open, and a writer."""
    ns = f"shared/ctxjob-{uuid.uuid4().hex[:10]}"
    owner.execute("SELECT pg_advisory_lock(%s, %s)", CONFIG_LOCK)
    prior = owner.execute("SELECT namespace FROM ctx_index_config WHERE id = 1").fetchone()[0]
    try:
        owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (ns,))
        monkeypatch.setenv("MESHY_CONTEXT_NAMESPACE", ns)
        monkeypatch.setenv("MESHY_CONTEXT_INDEX", "true")
        monkeypatch.setattr(ctxjob, "_mail_behind", lambda cur: False)
        monkeypatch.setattr(ctxjob, "_yield_since", {})
        monkeypatch.setattr(ctxjob, "_deadline_stuck", {})
        monkeypatch.setattr(ctxjob, "SWEEP_EVERY_S", 0)        # every tick sweeps, and fresh orphans go at once,
        monkeypatch.setattr(ctxjob, "ORPHAN_GRACE_S", 0)       # unless a test sets the production values
        env.settings([env.embed("same", env.hosts["same"])], gate=True)
        author = mesh._peer(owner, "cxj")
        mesh._admit(owner, ns, author)
        yield {"ns": ns, "author": author, "env": env, "owner": owner, "src": uuid.uuid4().hex}
    finally:
        owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (prior,))
        owner.execute("SELECT pg_advisory_unlock(%s, %s)", CONFIG_LOCK)


def _row(w, key, value, **override):
    a = w["author"]
    return sr.row(a["peer_id"], a["owner_id"], w["ns"], key, value, author_label=a["fallback"], **override)


def _batch(w, batch, texts, *, first=None, write_items=True, conn=None, commit=True, manifest_override=None, gen=1):
    """Write a batch's items and its manifest; returns the manifest's id. texts: [(kind, text)]."""
    seat, src = w["author"]["fallback"], w["src"]
    first = (batch - 1) * 100 if first is None else first
    entries, values = [], []
    for i, (kind, text) in enumerate(texts):
        o = first + i
        v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": gen, "batch": batch,
             "ordinal": o, "item": kind, "dialect": "claude", "adapter_version": "1.23", "time": T0, "text": text,
             "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
        values.append((f"ctx/{seat}/{src}/{gen:04d}/{batch:06d}/{o:010d}", v))
        entries.append([o, v["text_sha256"]])
    manifest = {"schema": "context.v1", "kind": "manifest", "seat": seat, "src": src, "gen": gen, "batch": batch,
                "first": first, "last": first + len(texts) - 1, "items": entries,
                "counts": {"written": len(texts), "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 0}}
    c = conn or psycopg.connect(DSN, autocommit=True)
    try:
        if write_items:
            for key, v in values:
                sr.insert(c, _row(w, key, v))
        mid = sr.insert(c, _row(w, f"ctxm/{seat}/{src}/{gen:04d}/{batch:06d}", manifest, **(manifest_override or {})))
        if conn is not None and commit:
            conn.commit()
    finally:
        if conn is None:
            c.close()
    return mid


def _tick(app_conn, w, **kw):
    return ctxjob.catch_up(app_conn, space=w["env"].space, **kw)


def _state(w):
    return w["owner"].execute("SELECT watermark_seq, stuck_seq, stuck_attempts FROM ctx_index_state WHERE space = %s",
                              (w["env"].space.name,)).fetchone()


def _seq(w, mid):
    return w["owner"].execute("SELECT seq FROM ctx_commit_order WHERE memory_id = %s", (mid,)).fetchone()[0]


def _chunks(w, mid=None):
    q = "SELECT src_id, chunk_no, first_ord, last_ord, text_sha256 FROM mesh_ctx_chunks WHERE space = %s"
    args = [w["env"].space.name]
    if mid is not None:
        q += " AND src_id = %s"
        args.append(mid)
    return w["owner"].execute(q + " ORDER BY src_id, chunk_no", args).fetchall()


def _current_violations(w) -> int:
    """sql_meshychat/051's invariant (pxx e0cf57): a chunk is `current` exactly when its gen is its source's newest
    indexed gen. Rows of a source with no ctx_source_gen row count as violations: the walker always records one."""
    return w["owner"].execute(
        "SELECT count(*) FROM mesh_ctx_chunks c LEFT JOIN ctx_source_gen s ON s.space = c.space "
        "AND s.namespace = c.namespace AND s.seat = c.seat AND s.src = c.src "
        "WHERE c.space = %s AND (s.gen IS NULL OR c.current <> (c.gen = s.gen))",
        (w["env"].space.name,)).fetchone()[0]


def _current_by_gen(w) -> dict:
    return {(g, cur): n for g, cur, n in w["owner"].execute(
        "SELECT gen, current, count(*) FROM mesh_ctx_chunks WHERE space = %s GROUP BY gen, current",
        (w["env"].space.name,)).fetchall()}


def _vectors(w):
    return w["owner"].execute("SELECT count(*) FROM mesh_ctx_vectors WHERE space = %s",
                              (w["env"].space.name,)).fetchone()[0]


def _embed_calls(w):
    return sum(1 for path, payload in w["env"].hosts["same"].requests if path.endswith("/v1/embeddings")
               and any("[user_text]" in str(x) for x in (payload or {}).get("input", [])))


def test_off_by_default_reads_and_writes_nothing(app, world, monkeypatch):
    _batch(world, 1, [("user_text", "hello"), ("assistant_text", "hi")])
    monkeypatch.delenv("MESHY_CONTEXT_INDEX")
    report = _tick(app, world)
    assert report.stopped == "disabled" and report.manifests_read == 0
    assert _chunks(world) == [] and _state(world) is None, "a disabled tick created state or rows"


def test_a_config_that_names_another_namespace_refuses_to_walk(app, world, monkeypatch):
    monkeypatch.setenv("MESHY_CONTEXT_NAMESPACE", "shared/somewhere-else")
    with pytest.raises(ctxjob.ConfigMismatch):
        _tick(app, world)


def test_committed_manifests_are_walked_once_in_commit_order_with_provenance(app, world):
    """Transaction A starts first and commits after B: both are walked exactly once (design acceptance line 1)."""
    a_conn = psycopg.connect(DSN)
    try:
        a = _batch(world, 1, [("user_text", "first question"), ("assistant_text", "first answer")], conn=a_conn,
                   commit=False)
        b = _batch(world, 2, [("user_text", "second question"), ("assistant_text", "second answer")])
        r1 = _tick(app, world)
        assert r1.manifests_indexed == 1 and _state(world)[0] == _seq(world, b), "B is walked at its commit"
        a_conn.commit()
    finally:
        a_conn.close()
    r2 = _tick(app, world)
    assert r2.manifests_indexed == 1 and _state(world)[0] == _seq(world, a) > _seq(world, b)
    rows = _chunks(world)
    assert {r[0] for r in rows} == {a, b} and len(rows) == 2, rows
    assert [(r[2], r[3]) for r in _chunks(world, a)] == [(0, 1)], "first_ord..last_ord of batch 1's one turn"
    r3 = _tick(app, world)
    assert r3.manifests_read == 0 and len(_chunks(world)) == 2, "control: a third tick re-walks nothing"


def test_a_duplicate_chunk_makes_no_embed_call(app, world):
    _batch(world, 1, [("user_text", "same words"), ("assistant_text", "same reply")])
    _tick(app, world)
    calls, vectors = _embed_calls(world), _vectors(world)
    _batch(world, 2, [("user_text", "same words"), ("assistant_text", "same reply")])
    report = _tick(app, world)
    assert report.manifests_indexed == 1 and report.embedded == 0 and report.reused == 1
    assert _embed_calls(world) == calls, "a chunk whose digest has a vector was embedded again"
    assert _vectors(world) == vectors and len(_chunks(world)) == 2, "two occurrences, one shared vector"


def test_an_embedder_outage_never_advances_the_watermark_and_the_backlog_drains(app, world):
    world["env"].hosts["same"].mode = "down"
    m = _batch(world, 1, [("user_text", "while the GPU is away"), ("assistant_text", "still here")])
    for _ in range(3):
        report = _tick(app, world)
        assert report.stopped == "provider_unavailable" and report.manifests_indexed == 0
    assert (_state(world)[0] or 0) == 0 and _chunks(world) == [], "an unembedded manifest was walked past"
    world["env"].hosts["same"].mode = "same"
    report = _tick(app, world)
    assert report.manifests_indexed == 1 and _state(world)[0] == _seq(world, m), "control: it drains once back"


def test_a_poison_manifest_is_dead_lettered_together_with_the_watermark_and_later_ones_walk(app, world):
    bad = _batch(world, 1, [("user_text", "this item never arrives")], write_items=False)
    good = _batch(world, 2, [("user_text", "a later question"), ("assistant_text", "its answer")])
    for attempt in range(1, ctxjob.MAX_ATTEMPTS):
        report = _tick(app, world)
        assert report.manifests_indexed == 0 and report.stopped == "unreadable_retry"
        assert _state(world)[1:] == (_seq(world, bad), attempt), "a transient count or a skipped manifest"
    report = _tick(app, world)
    dead = world["owner"].execute("SELECT state, attempts FROM ctx_dead_letter WHERE seq = %s",
                                  (_seq(world, bad),)).fetchone()
    assert dead == ("dead", ctxjob.MAX_ATTEMPTS)
    assert report.dead_lettered == 1 and report.manifests_indexed == 1
    assert _state(world)[0] == _seq(world, good), "the later manifest is walked after the dead letter"
    assert _chunks(world, bad) == [], "a dead-lettered manifest wrote nothing"


def test_a_dead_letter_marked_replay_is_walked_once_repaired(app, world):
    bad = _batch(world, 1, [("user_text", "late item")], write_items=False)
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)
    seq = _seq(world, bad)
    assert world["owner"].execute("SELECT state FROM ctx_dead_letter WHERE seq = %s", (seq,)).fetchone()[0] == "dead"
    # Repair: the missing item arrives (the manifest itself is immutable), and an operator marks the row for replay.
    seat, src = world["author"]["fallback"], world["src"]
    v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": 1, "batch": 1, "ordinal": 0,
         "item": "user_text", "dialect": "claude", "adapter_version": "1.23", "time": T0, "text": "late item",
         "text_sha256": hashlib.sha256(b"late item").hexdigest()}
    with psycopg.connect(DSN, autocommit=True) as c:
        sr.insert(c, _row(world, f"ctx/{seat}/{src}/0001/000001/0000000000", v))
    world["owner"].execute("UPDATE ctx_dead_letter SET state = 'replay' WHERE seq = %s", (seq,))
    report = _tick(app, world)
    assert report.replayed == 1
    assert world["owner"].execute("SELECT state FROM ctx_dead_letter WHERE seq = %s", (seq,)).fetchone()[0] == "done"
    assert len(_chunks(world, bad)) == 1, "the replayed manifest is indexed"


def test_a_deleted_manifest_leaves_no_orphaned_vector_after_the_next_tick(app, world):
    """pxx 2a94c2 point 1: a manifest row deleted outside the walker cascades its chunks; the next tick removes the
    vectors no occurrence names any more. Control: a vector another occurrence still uses stays."""
    m1 = _batch(world, 1, [("user_text", "only in batch one")])
    _batch(world, 2, [("user_text", "shared text")])
    m3 = _batch(world, 3, [("user_text", "shared text")])
    _tick(app, world)
    assert _vectors(world) == 2
    world["owner"].execute("DELETE FROM shared_memory WHERE id = ANY(%s)", ([m1, m3],))
    assert world["owner"].execute("SELECT ctx_vectors_orphaned(%s)", (world["env"].space.name,)).fetchone()[0] == 1
    report = _tick(app, world)
    assert report.orphans_swept == 1 and _vectors(world) == 1, "the shared vector must survive, the orphan must go"


def test_with_the_gate_closed_nothing_is_embedded_and_opening_it_rewalks(app, world):
    world["env"].settings([world["env"].embed("same", world["env"].hosts["same"])], gate=None)
    m = _batch(world, 1, [("user_text", "kept out by the gate")])
    report = _tick(app, world)
    assert report.gate_open is False and report.embedded == 0 and _chunks(world) == []
    assert _state(world)[0] == _seq(world, m), "the gate skips text; it never stalls the walk"
    # Opening the gate restarts the walk, so the manifest walked past while closed is indexed (Codex on #390).
    world["env"].settings([world["env"].embed("same", world["env"].hosts["same"])], gate=True)
    report = _tick(app, world)
    assert report.gate_open is True and report.manifests_indexed == 1 and len(_chunks(world, m)) == 1


def test_the_job_yields_while_the_mail_embedder_is_behind(app, world, monkeypatch):
    _batch(world, 1, [("user_text", "can wait")])
    monkeypatch.setattr(ctxjob, "_mail_behind", lambda cur: True)
    report = _tick(app, world)
    assert report.stopped == "yield_to_mail" and report.manifests_read == 0 and _chunks(world) == []
    monkeypatch.setattr(ctxjob, "_mail_behind", lambda cur: False)
    assert _tick(app, world).manifests_indexed == 1, "control: it walks once the mail is caught up"


def test_a_second_walker_moving_the_watermark_mid_tick_writes_nothing(app, world, monkeypatch):
    _batch(world, 1, [("user_text", "contended")])
    real = ctxjob.providers.embed_in_space

    def racing(*a, **kw):
        got = real(*a, **kw)
        if a and a[1]:                                    # the content call, not the canary-only reference call
            world["owner"].execute("INSERT INTO ctx_index_state (space, watermark_seq) VALUES (%s, 999999999) "
                                   "ON CONFLICT (space) DO UPDATE SET watermark_seq = 999999999",
                                   (world["env"].space.name,))
        return got

    monkeypatch.setattr(ctxjob.providers, "embed_in_space", racing)
    with pytest.raises(ctxjob.ConcurrentWalk):
        _tick(app, world)
    assert _chunks(world) == [], "a tick that lost the race wrote chunks"


def test_the_job_is_registered_bounded_and_after_the_mail_embedder():
    from apps.jobs import registry
    job = registry.by_name("ctx_embed_catch_up")
    assert job.run is ctxjob.run_catch_up_job and job.max_batch == ctxjob.MAX_MANIFESTS
    names = [j.name for j in registry.JOBS]
    assert names.index("ctx_embed_catch_up") > names.index("mesh_embed_catch_up")


# ------------------------------------------------------------------ pxx 3231fb, round 2

def test_a_vector_swept_between_planning_and_writing_stops_the_tick_and_writes_nothing(app, world, monkeypatch):
    """Point 1: B plans to REUSE A's vector; before B writes, A's manifest is deleted and the sweep runs. B must not
    commit a chunk naming a missing vector: the tick stops (transient), and the next tick re-embeds and indexes B."""
    a = _batch(world, 1, [("user_text", "the same words")])
    _tick(app, world)
    b = _batch(world, 2, [("user_text", "the same words")])
    real = ctxjob._plan

    def plan_then_sweep(*args, **kw):
        out = real(*args, **kw)
        world["owner"].execute("DELETE FROM shared_memory WHERE id = %s", (a,))
        with world["owner"].cursor(row_factory=ctxjob.dict_row) as cur:
            assert ctxjob._sweep_orphans(cur, world["env"].space) == 1
        return out

    monkeypatch.setattr(ctxjob, "_plan", plan_then_sweep)
    report = _tick(app, world)
    assert report.stopped == "vector_swept" and _chunks(world, b) == []
    monkeypatch.setattr(ctxjob, "_plan", real)
    report = _tick(app, world)
    assert report.manifests_indexed == 1 and report.embedded == 1 and len(_chunks(world, b)) == 1


def test_an_item_deleted_while_embedding_is_not_indexed(app, world, monkeypatch):
    """Point 2: the write transaction re-checks the planned items."""
    m = _batch(world, 1, [("user_text", "about to vanish"), ("assistant_text", "gone soon")])
    seat, src = world["author"]["fallback"], world["src"]
    real = ctxjob._embed

    def embed_then_delete(*args, **kw):
        out = real(*args, **kw)
        world["owner"].execute("DELETE FROM shared_memory WHERE namespace = %s AND key = %s",
                               (world["ns"], f"ctx/{seat}/{src}/0001/000001/0000000001"))
        return out

    monkeypatch.setattr(ctxjob, "_embed", embed_then_delete)
    report = _tick(app, world)
    assert report.manifests_indexed == 0 and report.stopped == "unreadable_retry"
    assert _chunks(world, m) == [] and _state(world)[1:] == (_seq(world, m), 1), "counted as an attempt, not indexed"


def test_a_replay_row_already_done_by_another_walker_is_not_rewritten(app, world, monkeypatch):
    """Point 3: the claim is taken in the write transaction."""
    bad = _batch(world, 1, [("user_text", "late item two")], write_items=False)
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)
    seq = _seq(world, bad)
    seat, src = world["author"]["fallback"], world["src"]
    v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": 1, "batch": 1, "ordinal": 0,
         "item": "user_text", "dialect": "claude", "adapter_version": "1.23", "time": T0, "text": "late item two",
         "text_sha256": hashlib.sha256(b"late item two").hexdigest()}
    with psycopg.connect(DSN, autocommit=True) as c:
        sr.insert(c, _row(world, f"ctx/{seat}/{src}/0001/000001/0000000000", v))
    world["owner"].execute("UPDATE ctx_dead_letter SET state = 'replay' WHERE seq = %s", (seq,))
    real = ctxjob._plan

    def plan_then_other_walker_finishes(*args, **kw):
        out = real(*args, **kw)
        world["owner"].execute("UPDATE ctx_dead_letter SET state = 'done' WHERE seq = %s", (seq,))
        return out

    monkeypatch.setattr(ctxjob, "_plan", plan_then_other_walker_finishes)
    report = _tick(app, world)
    assert report.replayed == 0 and _chunks(world, bad) == [], "a replay row another walker finished was rewritten"


def test_the_first_manifest_goes_whole_and_a_later_one_stops_at_the_embed_bound(app, world):
    """Point 5, the bound: the first manifest of a tick is embedded whole (else a manifest larger than the bound
    would never index); the second stops the tick when it would pass the bound."""
    _batch(world, 1, [("user_text", "one"), ("assistant_text", "a"), ("user_text", "two"), ("assistant_text", "b")])
    _batch(world, 2, [("user_text", "three")])
    report = _tick(app, world, embed_per_tick=1)
    assert report.manifests_indexed == 1 and report.embedded == 2 and report.stopped == "embed_bound"


def test_the_deadline_is_checked_before_every_embed_call(app, world, monkeypatch):
    """Point 5, the deadline: with one text per call and a deadline already spent, nothing is embedded or written."""
    monkeypatch.setattr(ctxjob, "SUB_BATCH", 1)
    _batch(world, 1, [("user_text", "slow one"), ("assistant_text", "x"), ("user_text", "slow two")])
    report = _tick(app, world, deadline_s=0.0)
    assert report.stopped == "deadline" and _chunks(world) == []


def test_a_mail_job_overdue_past_the_limit_is_walked_anyway_and_reported_as_an_error(app, world, monkeypatch):
    """Point 6: yielding is bounded; past YIELD_MAX_S the index walks and the job reports an error."""
    m = _batch(world, 1, [("user_text", "starved")])
    monkeypatch.setattr(ctxjob, "_mail_behind", lambda cur: True)
    assert _tick(app, world).stopped == "yield_to_mail"
    monkeypatch.setattr(ctxjob, "YIELD_MAX_S", 0)
    with pytest.raises(ctxjob.CtxJobFailed, match="overdue"):
        _tick(app, world)
    assert _state(world)[0] == _seq(world, m), "the walk ran before the error was raised"


def test_a_newest_gen_with_no_chunks_is_still_recorded_as_the_sources_gen(app, world):
    """pxx 51fbd8 point 2: gen 2 whose only item is an omission writes no chunk, but ctx_source_gen says 2, which is
    what retrieval filters on, so gen 1 stops being served."""
    seat, src = world["author"]["fallback"], world["src"]
    _batch(world, 1, [("user_text", "gen one text")])
    v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": 2, "batch": 1, "ordinal": 0,
         "item": "omitted", "omitted_reason": "secret", "dialect": "claude", "adapter_version": "1.23", "time": T0}
    m = {"schema": "context.v1", "kind": "manifest", "seat": seat, "src": src, "gen": 2, "batch": 1, "first": 0,
         "last": 0, "items": [[0, None]],
         "counts": {"written": 0, "omitted_secret": 1, "omitted_client_name": 0, "omitted_too_large": 0}}
    with psycopg.connect(DSN, autocommit=True) as c:
        sr.insert(c, _row(world, f"ctx/{seat}/{src}/0002/000001/0000000000", v))
        gen2 = sr.insert(c, _row(world, f"ctxm/{seat}/{src}/0002/000001", m))
    report = _tick(app, world)
    assert report.manifests_indexed == 2 and _chunks(world, gen2) == []
    assert world["owner"].execute("SELECT gen FROM ctx_source_gen WHERE namespace = %s AND seat = %s AND src = %s",
                                  (world["ns"], seat, src)).fetchone()[0] == 2
    # 051: the omissions-only newest gen retires every gen-1 row from `current` too (pxx e0cf57).
    by_gen = _current_by_gen(world)
    assert by_gen.get((1, False), 0) >= 1 and by_gen.get((1, True), 0) == 0, by_gen
    assert _current_violations(world) == 0


def test_an_older_gen_written_after_a_newer_one_is_not_current(app, world):
    """sql_meshychat/051 (pxx e0cf57): gen 2 is indexed first, then a gen-1 manifest of the same source. The
    ctx_source_gen upsert keeps 2 (GREATEST), and the flip is keyed on that, so the gen-1 rows just inserted (DEFAULT
    true) end the transaction not current. CONTROL: the gen-1 rows were written, so the flip had rows to act on."""
    _batch(world, 1, [("user_text", "the newer gen")], gen=2)
    _tick(app, world)
    _batch(world, 1, [("user_text", "the older gen, walked last")], gen=1)
    report = _tick(app, world)
    assert report.manifests_indexed == 1
    by_gen = _current_by_gen(world)
    assert by_gen.get((1, False), 0) >= 1, f"control: the gen-1 rows were written ({by_gen})"
    assert by_gen.get((1, True), 0) == 0 and by_gen.get((2, True), 0) >= 1 and by_gen.get((2, False), 0) == 0, by_gen
    assert _current_violations(world) == 0


def test_a_new_gen_retires_the_older_rows_in_the_same_commit(app, world):
    """The flip commits with the upsert and the chunks: after the gen-2 tick, gen 1 is not current and gen 2 is, and the
    invariant holds. CONTROL: after the gen-1 tick alone, gen 1 is current."""
    _batch(world, 1, [("user_text", "first gen words")], gen=1)
    _tick(app, world)
    assert _current_by_gen(world).get((1, True), 0) >= 1 and _current_violations(world) == 0, "control"
    _batch(world, 1, [("user_text", "second gen words")], gen=2)
    _tick(app, world)
    by_gen = _current_by_gen(world)
    assert by_gen.get((1, True), 0) == 0 and by_gen.get((1, False), 0) >= 1 and by_gen.get((2, True), 0) >= 1, by_gen
    assert _current_violations(world) == 0


def test_two_seats_with_the_same_src_gen_and_batch_keep_their_own_chunks(app, world, owner):
    """Codex on #387: the batch replacement is bound to the seat."""
    a = _batch(world, 1, [("user_text", "seat one's words")])
    other = mesh._peer(owner, "cxj2")
    mesh._admit(owner, world["ns"], other)
    w2 = dict(world, author=other)
    b = _batch(w2, 1, [("user_text", "seat two's words")])
    report = _tick(app, world)
    assert report.manifests_indexed == 2 and len(_chunks(world, a)) == 1 and len(_chunks(world, b)) == 1


# ------------------------------------------------------------------ a reviewer's cross-family read of #386

def test_a_walker_from_before_a_reset_cannot_commit_even_at_the_same_position(app, world, monkeypatch):
    """a reviewer P2-3: a reset that puts the watermark back where a stale walker stands; the epoch refuses it."""
    _batch(world, 1, [("user_text", "epoch contended")])
    real = ctxjob.providers.embed_in_space

    def resetting(*a, **kw):
        got = real(*a, **kw)
        if a and a[1]:                                    # the content call: another walker reset the walk meanwhile
            world["owner"].execute("UPDATE ctx_index_state SET walk_epoch = walk_epoch + 1 WHERE space = %s",
                                   (world["env"].space.name,))
        return got

    monkeypatch.setattr(ctxjob.providers, "embed_in_space", resetting)
    with pytest.raises(ctxjob.ConcurrentWalk):
        _tick(app, world)
    assert _chunks(world) == [], "a walker from an older epoch wrote chunks"
    assert _state(world)[0] is None, "the watermark moved under an older epoch"


def test_an_older_version_of_a_batch_never_overwrites_the_current_one(app, world):
    """a reviewer P2-2: replay or a re-walk of an old seq is skipped once its manifest row is no longer the head."""
    old = _batch(world, 1, [("user_text", "first version words")])
    world["owner"].execute("UPDATE shared_memory SET is_head = false WHERE id = %s", (old,))
    new = _batch(world, 1, [("user_text", "second version words")], first=50,
                 manifest_override={"version": 2, "parent_id": old})
    _tick(app, world)
    assert _chunks(world, old) == [] and len(_chunks(world, new)) == 1
    world["owner"].execute("UPDATE ctx_index_state SET watermark_seq = NULL WHERE space = %s",
                           (world["env"].space.name,))
    _tick(app, world, max_manifests=1)                    # a re-walk's first tick reaches only the old version
    assert _chunks(world, old) == [] and len(_chunks(world, new)) == 1, "the old version came back over the new"


def test_a_rewalk_passes_a_dead_lettered_manifest_without_stalling(app, world):
    """a reviewer P2-4: a dead manifest is replay's business; the walk does not retry it MAX_ATTEMPTS times again."""
    poison = _batch(world, 1, [("user_text", "never arrives")], write_items=False)
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)
    assert _state(world)[0] == _seq(world, poison), "control: the poison manifest is dead-lettered"
    good = _batch(world, 2, [("user_text", "after the poison")])
    world["owner"].execute("UPDATE ctx_index_state SET watermark_seq = NULL WHERE space = %s",
                           (world["env"].space.name,))
    report = _tick(app, world)
    assert report.manifests_indexed == 1 and _state(world)[0] == _seq(world, good)
    assert _state(world)[2] == 0, "the re-walk counted attempts on the dead manifest again"


@pytest.mark.parametrize("repair", [True, False])
def test_a_replay_planned_before_a_reset_writes_nothing(app, world, monkeypatch, repair):
    """pxx 69c96a: walk_epoch fences replay too, on the success write and on the failure write."""
    text = "replay fenced " + ("ok" if repair else "bad")
    bad = _batch(world, 1, [("user_text", text)], write_items=False)
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)
    seq = _seq(world, bad)
    if repair:
        seat, src = world["author"]["fallback"], world["src"]
        v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": 1, "batch": 1, "ordinal": 0,
             "item": "user_text", "dialect": "claude", "adapter_version": "1.23", "time": T0, "text": text,
             "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
        with psycopg.connect(DSN, autocommit=True) as c:
            sr.insert(c, _row(world, f"ctx/{seat}/{src}/0001/000001/0000000000", v))
    world["owner"].execute("UPDATE ctx_dead_letter SET state = 'replay' WHERE seq = %s", (seq,))
    before = world["owner"].execute("SELECT state, attempts FROM ctx_dead_letter WHERE seq = %s", (seq,)).fetchone()
    real = ctxjob._load_items

    def load_then_reset(*a, **kw):                        # only replay loads items here: the walk has nothing new
        try:
            return real(*a, **kw)
        finally:
            world["owner"].execute("UPDATE ctx_index_state SET walk_epoch = walk_epoch + 1 WHERE space = %s",
                                   (world["env"].space.name,))

    monkeypatch.setattr(ctxjob, "_load_items", load_then_reset)
    with pytest.raises(ctxjob.ConcurrentWalk):
        _tick(app, world)
    assert world["owner"].execute("SELECT state, attempts FROM ctx_dead_letter WHERE seq = %s",
                                  (seq,)).fetchone() == before, "the dead letter moved under an older epoch"
    assert _chunks(world, bad) == [], "a replay from an older epoch wrote chunks"


def test_a_manifest_cut_by_the_deadline_keeps_its_embedded_vectors_and_resumes(app, world, monkeypatch):
    """Codex on #386: sub-batches finished before the deadline are kept, so the next tick embeds only the rest."""
    monkeypatch.setattr(ctxjob, "SUB_BATCH", 1)
    monkeypatch.setattr(ctxjob, "ORPHAN_GRACE_S", 3600)   # production: the kept vectors are not swept meanwhile
    texts = [("user_text", f"turn {i} " + "x" * 3000) for i in range(3)]
    m = _batch(world, 1, texts)
    calls = []
    real = ctxjob.providers.embed_in_space

    def counting(*a, **kw):
        got = real(*a, **kw)
        if a and a[1]:
            calls.append(len(a[1]))
            if len(calls) == 1:                           # the deadline lands after the first content call
                monkeypatch.setattr(ctxjob, "_now", lambda: float("inf"))
        return got

    monkeypatch.setattr(ctxjob.providers, "embed_in_space", counting)
    first = _tick(app, world, deadline_s=60)
    assert first.stopped == "deadline" and _chunks(world, m) == []
    kept = world["owner"].execute("SELECT count(*) FROM mesh_ctx_vectors WHERE space = %s",
                                  (world["env"].space.name,)).fetchone()[0]
    assert kept == 1, "the finished sub-batch was thrown away"
    monkeypatch.setattr(ctxjob, "_now", ctxjob.time.monotonic)            # the real clock and provider again
    monkeypatch.setattr(ctxjob.providers, "embed_in_space", real)
    second = _tick(app, world)
    assert second.manifests_indexed == 1 and second.reused >= 1 and len(_chunks(world, m)) >= 1


def test_an_unfinished_manifests_kept_vectors_survive_a_sweep_past_the_grace(app, world, monkeypatch):
    """Codex on #389: a manifest embedded over more than ORPHAN_GRACE_S of ticks keeps its earliest kept vectors
    (sql_meshychat/050). CONTROL, in a rolled-back transaction: with the marker cleared the same sweep deletes them, so
    the marker is what spares them. Once the manifest is written the next sweep clears it."""
    from psycopg.rows import dict_row
    from apps.core import rls
    monkeypatch.setattr(ctxjob, "SUB_BATCH", 1)
    monkeypatch.setattr(ctxjob, "ORPHAN_GRACE_S", 3600)
    m = _batch(world, 1, [("user_text", f"turn {i} " + "y" * 3000) for i in range(3)])
    real = ctxjob.providers.embed_in_space
    calls = []

    def counting(*a, **kw):
        got = real(*a, **kw)
        if a and a[1]:
            calls.append(1)
            monkeypatch.setattr(ctxjob, "_now", lambda: float("inf"))      # the deadline lands after the first call
        return got

    monkeypatch.setattr(ctxjob.providers, "embed_in_space", counting)
    assert _tick(app, world, deadline_s=60).stopped == "deadline" and _vectors(world) == 1
    space = world["env"].space.name
    seqs, kept_at = world["owner"].execute("SELECT kept_seqs, kept_at FROM ctx_index_state WHERE space = %s",
                                           (space,)).fetchone()
    assert seqs == [_seq(world, m)] and kept_at is not None
    # Two hours later, ticks still unfinished: the vector and the marker are both past the grace.
    world["owner"].execute("UPDATE mesh_ctx_vectors SET created_at = created_at - interval '2 hours' WHERE space = %s",
                           (space,))
    world["owner"].execute("UPDATE ctx_index_state SET kept_at = kept_at - interval '2 hours' WHERE space = %s", (space,))
    space_obj = world["env"].space
    with rls.projection_writer(app):
        with app.cursor(row_factory=dict_row) as cur:
            assert ctxjob._sweep_orphans(cur, space_obj) == 0
    assert _vectors(world) == 1, "the sweep took an unfinished manifest's kept vector"
    with world["owner"].transaction():
        world["owner"].execute("UPDATE ctx_index_state SET kept_seqs = NULL, kept_at = NULL WHERE space = %s", (space,))
        with world["owner"].cursor(row_factory=dict_row) as cur:
            assert ctxjob._sweep_orphans(cur, space_obj) == 1, "control: without the marker the grace alone sweeps it"
        raise psycopg.Rollback()
    assert _vectors(world) == 1

    monkeypatch.setattr(ctxjob, "_now", ctxjob.time.monotonic)
    monkeypatch.setattr(ctxjob.providers, "embed_in_space", real)
    resumed = _tick(app, world)
    assert resumed.manifests_indexed == 1 and resumed.reused >= 1, "the kept vector was not reused"
    _tick(app, world)                                                    # the next sweep sees the manifest finished
    assert world["owner"].execute("SELECT kept_seqs, kept_at FROM ctx_index_state WHERE space = %s",
                                  (space,)).fetchone() == (None, None)


def _kept_world(app, world, monkeypatch):
    """The state row, the production grace, and helpers: keep one vector for a seq, age everything, sweep, count."""
    import numpy as np
    from psycopg.rows import dict_row
    from apps.core import rls
    from apps.meshsem import spaces
    monkeypatch.setattr(ctxjob, "ORPHAN_GRACE_S", 3600)
    sp = world["env"].space
    o = world["owner"]
    o.execute("INSERT INTO ctx_index_state (space) VALUES (%s) ON CONFLICT (space) DO NOTHING", (sp.name,))

    def keep(sha, seq):
        ctxjob._keep_vectors(app, sp, {sha: spaces.encode(np.ones(sp.dims, dtype=np.float32))}, seq)

    def age():
        o.execute("UPDATE mesh_ctx_vectors SET created_at = created_at - interval '2 hours' WHERE space = %s", (sp.name,))
        o.execute("UPDATE ctx_index_state SET kept_at = kept_at - interval '2 hours' WHERE space = %s", (sp.name,))

    def sweep(conn=app):
        if conn is app:
            with rls.projection_writer(app):
                with app.cursor(row_factory=dict_row) as cur:
                    return ctxjob._sweep_orphans(cur, sp)
        with conn.cursor(row_factory=dict_row) as cur:
            return ctxjob._sweep_orphans(cur, sp)

    def has(sha):
        return o.execute("SELECT count(*) FROM mesh_ctx_vectors WHERE space = %s AND text_sha256 = %s",
                         (sp.name, sha)).fetchone()[0] == 1

    return sp, o, keep, age, sweep, has


def test_a_finished_replay_does_not_release_an_unfinished_main_manifests_kept_vectors(app, world, monkeypatch):
    """codex-pxx-cli round 2 P2-1: replay 50 keeps after main 100, then completes while 100 is still unfinished; 100's
    vector A survives the sweep past the grace. CONTROL, rolled back: with only replay 50 in the set (the single-seq
    marker before the fix) the same sweep deletes A."""
    sp, o, keep, age, sweep, has = _kept_world(app, world, monkeypatch)
    m50 = _batch(world, 1, [("user_text", "replayed")])
    m100 = _batch(world, 2, [("user_text", "still embedding")])
    s50, s100 = _seq(world, m50), _seq(world, m100)
    o.execute("UPDATE ctx_index_state SET watermark_seq = %s WHERE space = %s", (s50, sp.name))
    o.execute("INSERT INTO ctx_dead_letter (seq, memory_id, space, reason, attempts, state) VALUES "
              "(%s, %s, %s, 'r', 5, 'replay')", (s50, m50, sp.name))
    a, b = "a" * 64, "b" * 64
    keep(a, s100)
    keep(b, s50)
    assert o.execute("SELECT kept_seqs FROM ctx_index_state WHERE space = %s", (sp.name,)).fetchone()[0] == [s100, s50]
    o.execute("UPDATE ctx_dead_letter SET state = 'done' WHERE space = %s AND seq = %s", (sp.name, s50))
    age()
    with o.transaction():
        o.execute("UPDATE ctx_index_state SET kept_seqs = ARRAY[%s]::bigint[] WHERE space = %s", (s50, sp.name))
        sweep(o)
        assert not has(a), "control: a marker naming only the finished replay releases A"
        raise psycopg.Rollback()
    sweep()
    assert has(a), "a finished replay released an unfinished main manifest's kept vector"
    assert o.execute("SELECT kept_seqs FROM ctx_index_state WHERE space = %s", (sp.name,)).fetchone()[0] == [s100]


def test_a_deleted_kept_manifest_releases_the_marker(app, world, monkeypatch):
    """codex-pxx-cli round 2 P2-2: the kept manifest is deleted and no later manifest moves the watermark; the next
    sweep clears the marker and the grace applies again. CONTROL: before the delete the same sweep spares it."""
    sp, o, keep, age, sweep, has = _kept_world(app, world, monkeypatch)
    m = _batch(world, 1, [("user_text", "never finished")])
    a = "c" * 64
    keep(a, _seq(world, m))
    age()
    sweep()
    assert has(a), "control: while the manifest is still to walk, its kept vector is spared"
    o.execute("DELETE FROM shared_memory WHERE id = %s", (m,))
    sweep()
    assert o.execute("SELECT kept_seqs, kept_at FROM ctx_index_state WHERE space = %s",
                     (sp.name,)).fetchone() == (None, None)
    sweep()
    assert not has(a), "a gone manifest still pinned its kept vector"


def test_the_sweep_runs_at_most_every_sweep_interval(app, world, monkeypatch):
    """Codex on #386: the anti-join over the space's vectors is not paid on every tick."""
    monkeypatch.setattr(ctxjob, "SWEEP_EVERY_S", 3600)
    m = _batch(world, 1, [("user_text", "swept later")])
    _tick(app, world)                                     # sweeps (never swept yet) and indexes
    world["owner"].execute("DELETE FROM shared_memory WHERE id = %s", (m,))
    assert _tick(app, world).orphans_swept == 0, "swept again inside the interval"
    world["owner"].execute("UPDATE ctx_index_state SET swept_at = now() - interval '2 hours' WHERE space = %s",
                           (world["env"].space.name,))
    assert _tick(app, world).orphans_swept == 1, "the interval passed and the orphan stayed"


def test_ticks_that_cannot_embed_one_sub_batch_fail_loudly(app, world, monkeypatch):
    """pxx aad257: a provider slower than the tick makes no progress; after DEADLINE_STUCK_TICKS that is an error,
    not rows 0 reported as ok. Control: the ticks before it return normally."""
    _batch(world, 1, [("user_text", "too slow to embed")])
    real = ctxjob.providers.embed_in_space

    def slow(*a, **kw):
        if a and a[1]:                                    # the content call: the deadline passes before it returns
            monkeypatch.setattr(ctxjob, "_now", lambda: float("inf"))
            raise ctxjob._Deadline({})
        return real(*a, **kw)

    monkeypatch.setattr(ctxjob.providers, "embed_in_space", slow)
    for _ in range(ctxjob.DEADLINE_STUCK_TICKS - 1):
        monkeypatch.setattr(ctxjob, "_now", ctxjob.time.monotonic)
        assert _tick(app, world).stopped == "deadline"
    monkeypatch.setattr(ctxjob, "_now", ctxjob.time.monotonic)
    with pytest.raises(ctxjob.CtxJobFailed):
        _tick(app, world)


def _slow_provider(monkeypatch, per_call_s):
    """A provider whose content calls each take `per_call_s` of a fake clock (the ctxjob._now seam), and that fails
    as EmbedUnavailable when the budget it is given is shorter, as embed_in_space's own timeout does."""
    real = ctxjob.providers.embed_in_space
    clock = {"t": ctxjob.time.monotonic()}

    def fake(*a, budget_s=20.0, **kw):
        if not (a and a[1]):                              # the reference call: not timed
            return real(*a, budget_s=budget_s, **kw)
        if budget_s < per_call_s:
            raise ctxjob.providers.EmbedUnavailable("timed out", [{"provider": "fake", "outcome": "timeout"}])
        clock["t"] += per_call_s
        return real(*a, budget_s=budget_s, **kw)

    monkeypatch.setattr(ctxjob.providers, "embed_in_space", fake)
    monkeypatch.setattr(ctxjob, "_now", lambda: clock["t"])
    return clock


@pytest.mark.parametrize("path", ["walk", "replay"])
def test_a_call_that_does_not_fit_the_tick_keeps_what_finished_and_the_manifest_commits(app, world, monkeypatch,
                                                                                          path):
    """pxx 9a27ae: the provider's timeout (EmbedUnavailable) after finished sub-batches is progress, not an outage."""
    monkeypatch.setattr(ctxjob, "SUB_BATCH", 1)
    monkeypatch.setattr(ctxjob, "ORPHAN_GRACE_S", 3600)
    texts = [("user_text", f"slow turn {i} " + "y" * 3000) for i in range(4)]
    m = _batch(world, 1, texts, write_items=(path == "walk"))
    if path == "replay":                                  # dead-letter it, repair it, mark it for replay
        for _ in range(ctxjob.MAX_ATTEMPTS):
            _tick(app, world)
        seat, src = world["author"]["fallback"], world["src"]
        with psycopg.connect(DSN, autocommit=True) as c:
            for i, (kind, text) in enumerate(texts):
                v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": 1, "batch": 1,
                     "ordinal": i, "item": kind, "dialect": "claude", "adapter_version": "1.23", "time": T0,
                     "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
                sr.insert(c, _row(world, f"ctx/{seat}/{src}/0001/000001/{i:010d}", v))
        world["owner"].execute("UPDATE ctx_dead_letter SET state = 'replay' WHERE seq = %s", (_seq(world, m),))
    clock = _slow_provider(monkeypatch, per_call_s=8.0)
    counts = []
    for _ in range(10):
        _tick(app, world, deadline_s=30)                 # each tick's deadline is _now() + 30 on the fake clock
        counts.append(world["owner"].execute("SELECT count(*) FROM mesh_ctx_vectors WHERE space = %s",
                                             (world["env"].space.name,)).fetchone()[0])
        if _chunks(world, m):
            break
    assert _chunks(world, m), f"the manifest never committed; vectors per tick {counts}"
    assert counts[0] >= 1 and len(counts) >= 2, f"control: the first tick was cut short and kept work ({counts})"


def test_a_chunk_spans_its_items_earliest_and_latest_time_not_their_order():
    """pxx 4dd00a: times need not increase with the ordinal; the since filter and retention read these bounds."""
    items = [{"ordinal": 0, "item": "user_text", "text": "asked at ten", "time": "2026-10-24T10:00:00Z"},
             {"ordinal": 1, "item": "assistant_text", "text": "answered at nine", "time": "2026-10-24T09:00:00Z"}]
    (c,) = ctxjob.turn_chunks(items)
    assert (c.time_first, c.time_last) == ("2026-10-24T09:00:00Z", "2026-10-24T10:00:00Z")
    assert (c.first_ord, c.last_ord) == (0, 1) and c.text.index("ten") < c.text.index("nine"), "text keeps ordinal order"


def test_an_item_with_a_bad_ordinal_dead_letters_instead_of_failing_the_tick(app, world):
    """pxx 4dd00a INFO: a row written around the validator is Unreadable, so the walk records it and moves on."""
    seat, src = world["author"]["fallback"], world["src"]
    m = _batch(world, 1, [("user_text", "bad ordinal")], write_items=False)
    v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": 1, "batch": 1, "ordinal": "zero",
         "item": "user_text", "dialect": "claude", "adapter_version": "1.23", "time": T0, "text": "bad ordinal",
         "text_sha256": hashlib.sha256(b"bad ordinal").hexdigest()}
    with psycopg.connect(DSN, autocommit=True) as c:
        sr.insert(c, _row(world, f"ctx/{seat}/{src}/0001/000001/0000000000", v))
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)                                 # no exception escapes
    assert _state(world)[0] == _seq(world, m), "the manifest was dead-lettered and the walk moved past it"


def _replay_only(world, app, text):
    """A dead-lettered manifest, repaired and marked 'replay', with nothing else to walk. Returns its id."""
    bad = _batch(world, 1, [("user_text", text)], write_items=False)
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)
    seat, src = world["author"]["fallback"], world["src"]
    v = {"schema": "context.v1", "kind": "item", "seat": seat, "src": src, "gen": 1, "batch": 1, "ordinal": 0,
         "item": "user_text", "dialect": "claude", "adapter_version": "1.23", "time": T0, "text": text,
         "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    with psycopg.connect(DSN, autocommit=True) as c:
        sr.insert(c, _row(world, f"ctx/{seat}/{src}/0001/000001/0000000000", v))
    world["owner"].execute("UPDATE ctx_dead_letter SET state = 'replay' WHERE seq = %s", (_seq(world, bad),))
    return bad


def test_a_replay_only_tick_reports_a_provider_outage(app, world):
    """pxx 52bfc9: with only replay work and the embedder down, the tick says provider_unavailable, not ok."""
    _replay_only(world, app, "replay with the embedder down")
    world["env"].hosts["same"].mode = "down"
    report = _tick(app, world)
    assert report.replayed == 0 and report.stopped == "provider_unavailable"
    world["env"].hosts["same"].mode = "same"                 # control: back up, the replay completes
    assert _tick(app, world).replayed == 1


def test_a_replay_that_cannot_fit_the_tick_fails_loudly_after_the_stuck_limit(app, world, monkeypatch):
    """pxx 52bfc9 / 52be95: a replay-only stall on the deadline feeds DEADLINE_STUCK_TICKS like the main walk."""
    _replay_only(world, app, "replay too slow to embed")
    real = ctxjob.providers.embed_in_space

    def slow(*a, **kw):
        if a and a[1]:                                    # the content call: the deadline passes first
            raise ctxjob._Deadline({})
        return real(*a, **kw)

    monkeypatch.setattr(ctxjob.providers, "embed_in_space", slow)
    for _ in range(ctxjob.DEADLINE_STUCK_TICKS - 1):
        assert _tick(app, world).stopped == "deadline"
    with pytest.raises(ctxjob.CtxJobFailed):
        _tick(app, world)
