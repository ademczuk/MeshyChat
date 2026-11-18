"""v1.1 step 7: search over the transcript-context index (apps/meshsem/ctxsearch.py; route wiring in views.py).

End to end on the fake providers: manifests are written, apps/meshsem/ctxjob.py indexes them, and the search answers
with provenance (seat, src, gen, batch, ordinals, item keys) and the exact chunk text. Fixtures are
tests/test_ctx_index_job.py's (its own namespace and space per test, the job switched on).

Run:  pytest tests/test_ctx_search.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_ctx_index_job import DSN, _batch, _row, _seq, _tick, app, env, owner, world  # noqa: E402,F401
import psycopg  # noqa: E402
from apps.meshsem import ctxjob, ctxsearch, lexical  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from apps.meshsem import ctxindex, ctxsearch, views  # noqa: E402
from apps.peers import errors  # noqa: E402


# ------------------------------------------------------------------ parameters (no database)

def test_filters_parse_and_refuse_what_they_cannot_mean():
    f = ctxsearch.filters_from({"seat": "kxxx", "since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00+02:00"})
    assert f.seat == "kxxx" and f.since.tzinfo is not None and f.until == _dt.datetime(2026, 10, 1, 22, tzinfo=_dt.timezone.utc)
    for bad in ({"since": "yesterday"}, {"since": "2026-10-01T00:00:00"},
                {"since": "2026-10-02T00:00:00Z", "until": "2026-10-01T00:00:00Z"}):
        with pytest.raises(errors.ApiError):
            ctxsearch.filters_from(bad)
    assert ctxsearch.filters_from({"seat": "  "}).seat is None, "a blank filter is no filter"


def test_card_only_parameters_are_refused_by_name_on_this_namespace():
    for p in ("type", "owner", "status", "thread"):
        with pytest.raises(errors.ApiError) as exc:
            ctxsearch.filters_from({p: "x"})
        assert p in exc.value.message


def test_collapse_is_one_of_three():
    assert [ctxsearch.collapse_of({"collapse": c}) for c in ("chunk", "item", "session")] == ["chunk", "item", "session"]
    assert ctxsearch.collapse_of({}) == "chunk"
    with pytest.raises(errors.ApiError):
        ctxsearch.collapse_of({"collapse": "thread"})


def test_the_namespace_resolves_before_the_read_check_without_telling_whether_it_is_indexed(monkeypatch):
    """pxx 075b32: _search_namespace runs BEFORE the read check, so it must answer the same whether the index is on
    or off; the 'not indexed' refusal comes only from the handler, after the check. Control: an unknown namespace is
    still refused there, as before."""
    monkeypatch.setenv("MESHY_CONTEXT_NAMESPACE", "shared/transcript-context")
    req = RequestFactory().get("/v1/mesh/search", {"q": "x", "ns": "shared/transcript-context"})
    for on in (None, "true"):
        if on is None:
            monkeypatch.delenv("MESHY_CONTEXT_INDEX", raising=False)
        else:
            monkeypatch.setenv("MESHY_CONTEXT_INDEX", on)
        assert views._search_namespace(req) == "shared/transcript-context"
    with pytest.raises(errors.ApiError):
        views._search_namespace(RequestFactory().get("/v1/mesh/search", {"q": "x", "ns": "shared/nowhere"}))


def test_the_not_indexed_refusal_sits_after_the_read_check_in_the_handler():
    """By reading the source: the enabled() refusal is inside `search`, which `_route` calls only after
    check_permission passed; it is not in `_search_namespace`."""
    import inspect
    import re
    src = inspect.getsource(views)                        # the module, not views.search: that name is _route's wrapper
    def body(name):
        m = re.search(rf"^def {name}\(.*?(?=^def |^@|\Z)", src, re.S | re.M)
        assert m, name
        return m.group(0)
    assert "ctxjob.enabled()" not in body("_search_namespace")
    assert "ctxjob.enabled()" in body("search")


# ------------------------------------------------------------------ end to end (database, fake providers)

def _search(app, w, q, **kw):
    return ctxsearch.search(app, w["env"].space, w["ns"], q, kw.pop("f", ctxindex.Filters()), rerank=False, **kw)


def test_a_hit_quotes_the_exact_turn_and_names_its_items(app, world):
    _batch(world, 1, [("user_text", "how do we rotate the signing key"),
                      ("assistant_text", "run the rotate command, then wait for the grace window")])
    _batch(world, 2, [("user_text", "what is for lunch"), ("assistant_text", "soup")])
    assert _tick(app, world).manifests_indexed == 2
    body = _search(app, world, "how do we rotate the signing key", k=1)
    hit = body["hits"][0]
    assert hit["text"] == ("[user_text]\nhow do we rotate the signing key\n\n"
                           "[assistant_text]\nrun the rotate command, then wait for the grace window")
    assert (hit["first_ord"], hit["last_ord"], hit["batch"]) == (0, 1, 1)
    seat, src = world["author"]["fallback"], world["src"]
    assert hit["item_keys"] == [f"ctx/{seat}/{src}/0001/000001/0000000000", f"ctx/{seat}/{src}/0001/000001/0000000001"]
    # One kind per item, in ordinal order: a reader tells the seat's own words from tool output without a read (#480 P3).
    assert hit["item_kinds"] == ["user_text", "assistant_text"]
    assert body["coverage"]["path"] == "exact" and body["coverage"]["truncated"] is False
    assert body["arms"]["lexical"]["ran"] is True and body["arms"]["vector"]["ran"] is True
    assert body["method"] == "rrf(lexical,embed)"
    # queue_ms (#538): the wait for a transcript-search slot, before the embed.
    assert set(body["timings"]) == {"queue_ms", "embed_ms", "store_ms", "body_ms", "rerank_ms"}


def test_item_kinds_mark_the_items_the_text_never_holds(app, world):
    """kxxx on #502, P2-502-4, his case E through the real index and search: turn_chunks leaves omitted and blank items
    out of the text, so item_kinds says None for them, and own_words stays in step and keeps the tool output out."""
    from tools.meshy_node import resume_brief
    _batch(world, 1, [("user_text", "please check the log"), ("assistant_text", "Checking the log now."),
                      ("omitted", ""), ("assistant_text", "   "), ("tool_exchange", "TOOL OUTPUT LINE"),
                      ("assistant_text", "The log is clean.")])
    _tick(app, world)
    hit = _search(app, world, "please check the log", k=1)["hits"][0]
    assert hit["item_kinds"] == ["user_text", "assistant_text", None, None, "tool_exchange", "assistant_text"]
    assert len(hit["item_kinds"]) == len(hit["item_keys"])
    words = resume_brief.own_words(hit)
    assert "TOOL OUTPUT" not in words and words == "Checking the log now. The log is clean."


def test_a_seat_filter_keeps_other_seats_out(app, world):
    _batch(world, 1, [("user_text", "the shared question")])
    _tick(app, world)
    body = _search(app, world, "the shared question", f=ctxindex.Filters(seat="nobody-else"))
    assert body["hits"] == [] and body["coverage"]["subset"] == 0
    body = _search(app, world, "the shared question", f=ctxindex.Filters(seat=world["author"]["fallback"]))
    assert len(body["hits"]) == 1, "control: its own seat finds it"


def test_collapse_session_returns_one_hit_per_source(app, world):
    for b in range(1, 4):
        _batch(world, b, [("user_text", f"question {b} about deploys"), ("assistant_text", f"answer {b}")])
    _tick(app, world)
    chunks = _search(app, world, "deploys", k=10)
    sessions = _search(app, world, "deploys", k=10, collapse="session")
    assert len(chunks["hits"]) == 3 and len(sessions["hits"]) == 1
    assert sessions["hits"][0]["score"] == max(h["score"] for h in chunks["hits"]), "the group keeps its best hit"


def test_a_hit_whose_items_are_gone_says_so_instead_of_quoting(app, world):
    _batch(world, 1, [("user_text", "soon to be deleted")])
    _tick(app, world)
    seat, src = world["author"]["fallback"], world["src"]
    world["owner"].execute("DELETE FROM shared_memory WHERE namespace = %s AND key = %s",
                           (world["ns"], f"ctx/{seat}/{src}/0001/000001/0000000000"))
    hit = _search(app, world, "soon to be deleted", k=1)["hits"][0]
    assert hit["text"] is None and hit["text_available"] is False
    assert hit["item_kinds"] is None, "no kinds are claimed for items that cannot be read"


# ------------------------------------------------------------------ the route, with and without the grant (pxx da5557)

def test_another_seats_chunks_are_found_with_the_read_grant_and_nothing_leaks_without_it(app, world):
    """Every seat reads every seat's context (operator, 10 Oct; context-v1.md section 2), so seat= naming ANOTHER seat
    is allowed for a caller holding the read grant. A caller without it gets the namespace layer's 403 and nothing
    else: no hits, no counts, no coverage, no timings, and no word on whether the index is on."""
    from django.test import Client, override_settings
    from tests.fixtures import meshsem_db as db
    from tests.test_meshsem_routes import DSN as ROUTE_DSN, ISSUER, PUB, _bearer
    _batch(world, 1, [("user_text", "the deploy checklist for friday"), ("assistant_text", "freeze at noon")])
    _tick(app, world)
    o = world["owner"]
    holder = o.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace WHERE namespace = %s",
                       (world["ns"],)).fetchone()
    reader, stranger = db.peer(o, "ctxreader"), db.peer(o, "ctxstranger")
    o.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, permission, "
              "granted_by_peer_id, granted_by_owner_id) VALUES (%s, %s, %s, 'read', %s, %s)",
              (world["ns"], reader["peer_id"], reader["owner_id"], holder[0], holder[1]))
    url = (f"/v1/mesh/search?ns={world['ns']}&q=deploy+checklist&rerank=0"
           f"&seat={world['author']['fallback']}")
    client = Client()
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"], MESHY_DSN=ROUTE_DSN,
                           MESHY_JWT_PUBLIC_KEY_PEM=PUB, MESHY_JWT_ISSUER=ISSUER):
        allowed = client.get(url, **_bearer(reader["peer_id"]))
        denied = client.get(url, **_bearer(stranger["peer_id"]))
    assert allowed.status_code == 200, allowed.content[:300]
    body = allowed.json()
    assert body["hits"] and body["hits"][0]["seat"] == world["author"]["fallback"], "another seat's chunk is found"
    assert denied.status_code == 403 and denied.json()["error"] == "E_PERMISSION_DENIED"
    assert set(denied.json()) <= {"error", "message", "detail"}, f"a refused caller learned more: {denied.json()}"


def test_a_full_pool_that_collapses_short_says_so(app, world, monkeypatch):
    """pxx 075b32 P2-2: when the candidate pool is full and collapse leaves fewer than k groups, the answer says the
    pool was truncated instead of reporting a complete one."""
    monkeypatch.setattr(ctxsearch, "POOL", 3)
    monkeypatch.setattr(ctxsearch, "POOL_PER_GROUP", 1)
    for b in range(1, 6):
        _batch(world, b, [("user_text", f"deploy question {b}")])
    _tick(app, world)
    body = _search(app, world, "deploy question", k=3, collapse="session")
    assert len(body["hits"]) == 1 and body["pool"]["truncated"] is True and body["truncated"] is True


# ------------------------------------------------------------------ the lexical arm (sql_meshychat/048)

def test_the_job_writes_one_lexical_row_per_digest_and_the_sweep_removes_orphans(app, world):
    m = _batch(world, 1, [("user_text", "rotate the zq7xkey signing key")])
    _tick(app, world)
    rows = world["owner"].execute("SELECT lexemes FROM mesh_ctx_lexical WHERE space = %s",
                                  (world["env"].space.name,)).fetchall()
    assert len(rows) == 1 and "zq7xkey" in rows[0][0]
    world["owner"].execute("DELETE FROM shared_memory WHERE id = %s", (m,))
    _tick(app, world)
    assert world["owner"].execute("SELECT count(*) FROM mesh_ctx_lexical WHERE space = %s",
                                  (world["env"].space.name,)).fetchone()[0] == 0, "an orphaned lexical row stayed"


def test_an_exact_identifier_is_found_by_the_lexical_arm_alone_when_the_embedder_is_down(app, world):
    _batch(world, 1, [("user_text", "where is mesh_embed_catch_up configured"), ("assistant_text", "in the registry")])
    _batch(world, 2, [("user_text", "what is for lunch")])
    _tick(app, world)
    world["env"].hosts["same"].mode = "down"
    body = _search(app, world, "mesh_embed_catch_up", k=3)
    assert body["arms"]["vector"]["ran"] is False and body["method"] == "lexical"
    assert body["hits"] and "mesh_embed_catch_up" in body["hits"][0]["text"]
    assert body["coverage"]["truncated"] is True, "with no vector arm the coverage cannot claim completeness"


def test_the_lexical_arm_stays_inside_the_namespace_and_the_newest_gen(app, world):
    """The same bindings as the vector arm (pxx 075b32): a term in another namespace or an older gen is not a hit."""
    _batch(world, 1, [("user_text", "unique token qq9lex here")])
    _tick(app, world)
    o = world["owner"]
    o.execute("UPDATE mesh_ctx_chunks SET namespace = 'shared/elsewhere' WHERE space = %s", (world["env"].space.name,))
    world["env"].hosts["same"].mode = "down"
    with pytest.raises(errors.ApiError):                   # vector down and the lexical arm finds nothing here
        _search(app, world, "qq9lex", k=3)


def test_project_is_refused_until_a_writer_sends_it():
    with pytest.raises(errors.ApiError) as exc:
        ctxsearch.filters_from({"project": "abc"})
    assert "not offered yet" in exc.value.message


def test_with_the_gate_closed_search_serves_nothing(app, world):
    """Codex on #390: rows indexed while the gate was open are not served after it closes."""
    _batch(world, 1, [("user_text", "indexed while open")])
    _tick(app, world)
    assert _search(app, world, "indexed while open", k=1)["hits"], "control: served while open"
    world["env"].settings([world["env"].embed("same", world["env"].hosts["same"])], gate=None)
    body = _search(app, world, "indexed while open", k=1)
    assert body["hits"] == [] and body["gate_open"] is False and body["truncated"] is True


def test_a_dead_lettered_newer_gen_is_flagged_in_coverage(app, world):
    """pxx 300eeb: the older gen is still served, but the answer says a newer one is unindexed."""
    _batch(world, 1, [("user_text", "older gen words")])
    _tick(app, world)
    seat, src = world["author"]["fallback"], world["src"]
    m = {"schema": "context.v1", "kind": "manifest", "seat": seat, "src": src, "gen": 2, "batch": 1, "first": 0,
         "last": 0, "items": [[0, "0" * 64]],
         "counts": {"written": 1, "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 0}}
    with psycopg.connect(DSN, autocommit=True) as c:
        sr.insert(c, _row(world, f"ctxm/{seat}/{src}/0002/000001", m))      # its item never arrives: poison
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)
    body = _search(app, world, "older gen words", k=1)
    assert body["hits"] and body["sources_with_unindexed_newer_gen"] == 1 and body["truncated"] is True


def _clone_lexical_chunks(world, n, term, tf):
    """n extra chunks holding `term` tf times, cloned from the space's first chunk with fake digests (text unavailable)."""
    o, sp = world["owner"], world["env"].space.name
    o.execute("INSERT INTO mesh_ctx_lexical (space, text_sha256, derivation, lexemes, tfs) "
              "SELECT %s, lpad(to_hex(g), 64, '0'), %s, ARRAY[%s], ARRAY[%s] FROM generate_series(1, %s) g",
              (sp, lexical.DERIVATION, term, tf, n))
    o.execute("INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, project, "
              "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256) "
              "SELECT c.src_id, c.space, c.namespace, 1000 + g, c.seat, c.src, c.gen, c.batch, c.project, c.first_ord, "
              "c.last_ord, c.char_start, c.char_end, c.time_first, c.time_last, lpad(to_hex(g), 64, '0') "
              "FROM (SELECT * FROM mesh_ctx_chunks WHERE space = %s ORDER BY id LIMIT 1) c, generate_series(1, %s) g",
              (sp, n))


def test_the_best_lexical_chunk_is_never_dropped_before_scoring(app, world):
    """pxx 34b9c6 point 2: 2,001 chunks hold the term and the one with tf 100 used to be cut before scoring."""
    _batch(world, 1, [("user_text", " ".join(["zzneedle"] * 100))])
    _tick(app, world)
    _clone_lexical_chunks(world, 2000, "zzneedle", 1)
    world["env"].hosts["same"].mode = "down"
    for _ in range(3):                                     # an unordered cut lost it in every run; repeat anyway
        body = _search(app, world, "zzneedle", k=1)
        assert body["hits"][0]["text"] and body["hits"][0]["text"].count("zzneedle") == 100
        assert body["arms"]["lexical"]["candidates"] == 2001 and body["arms"]["lexical"]["scored_exactly"] is True


def test_over_the_exact_bound_the_cut_is_by_relevance_and_says_so(app, world, monkeypatch):
    monkeypatch.setattr(ctxsearch, "LEX_EXACT_MAX", 50)
    _batch(world, 1, [("user_text", " ".join(["zzneedle"] * 100))])
    _tick(app, world)
    _clone_lexical_chunks(world, 80, "zzneedle", 1)
    world["env"].hosts["same"].mode = "down"
    body = _search(app, world, "zzneedle", k=1)
    assert body["hits"][0]["text"].count("zzneedle") == 100, "the relevance pre-order kept the best chunk"
    lex = body["arms"]["lexical"]
    assert lex["candidates"] == 50 and lex["scored_exactly"] is False and lex["truncated"] is True


def test_past_the_scan_cap_the_lexical_arm_says_it_is_truncated(app, world, monkeypatch):
    """pxx ab9411: the pre-order reads at most LEX_SCAN_MAX matching chunks. CONTROL: under the cap the same set is
    scored exactly."""
    _batch(world, 1, [("user_text", " ".join(["zzcap"] * 3))])
    _tick(app, world)
    _clone_lexical_chunks(world, 30, "zzcap", 1)
    world["env"].hosts["same"].mode = "down"
    lex = _search(app, world, "zzcap", k=1)["arms"]["lexical"]
    assert lex["scored_exactly"] is True and lex["truncated"] is False, "control: 31 matches, under every cap"
    monkeypatch.setattr(ctxsearch, "LEX_SCAN_MAX", 10)
    lex = _search(app, world, "zzcap", k=1)["arms"]["lexical"]
    assert lex["candidates"] <= 11 and lex["scored_exactly"] is False and lex["truncated"] is True


def test_past_the_dead_letter_cap_the_count_is_marked_at_least(app, world, monkeypatch):
    """pxx ab9411: the unindexed-gen count reads at most DEAD_SCAN_MAX dead-letter rows. CONTROL: under it, exact."""
    _batch(world, 1, [("user_text", "served cap words")])
    _tick(app, world)
    seat, src = world["author"]["fallback"], world["src"]
    with psycopg.connect(DSN, autocommit=True) as c:
        bad = [sr.insert(c, _row(world, f"ctxm/{seat}/{src}/00{9 - i:02d}/000001",
                                 {"schema": "context.v1", "kind": "manifest", "seat": seat, "src": src, "gen": "abc"}))
               for i in range(3)]
    for i, mid in enumerate(bad):
        world["owner"].execute("INSERT INTO ctx_dead_letter (seq, memory_id, space, reason, attempts) VALUES "
                               "(%s, %s, %s, 'malformed gen', 5)", (999999900 + i, mid, world["env"].space.name))
    body = _search(app, world, "served cap words", k=1)
    assert body["sources_with_unindexed_newer_gen"] == 1 and body["sources_with_unindexed_newer_gen_at_least"] is False
    monkeypatch.setattr(ctxsearch, "DEAD_SCAN_MAX", 2)
    body = _search(app, world, "served cap words", k=1)
    assert body["sources_with_unindexed_newer_gen"] == 1 and body["sources_with_unindexed_newer_gen_at_least"] is True
    assert body["truncated"] is True


def test_an_index_built_before_048_gets_its_lexical_rows_on_the_next_tick(app, world):
    """pxx 34b9c6 point 1: 048 down then up leaves the space's watermark; the next tick re-walks and fills the rows."""
    _batch(world, 1, [("user_text", "historical zq8ident here")])
    _batch(world, 2, [("user_text", "a later batch")])
    _tick(app, world)
    o, sp = world["owner"], world["env"].space.name
    o.execute("DELETE FROM mesh_ctx_lexical WHERE space = %s", (sp,))            # as 048 up leaves it: no rows,
    o.execute("UPDATE ctx_index_state SET lexical_derivation = NULL, lexical_rewalk_until = NULL WHERE space = %s",
              (sp,))                                                             # and no derivation recorded
    world["env"].hosts["same"].mode = "down"
    with pytest.raises(errors.ApiError):                   # the control: before the re-walk, nothing to find
        _search(app, world, "zq8ident", k=1)
    world["env"].hosts["same"].mode = "same"
    report = _tick(app, world, max_manifests=1)            # half the walk: the arm says it is partial
    assert report.embedded == 0, "a re-walk of unchanged digests makes no embed call"
    world["env"].hosts["same"].mode = "down"
    body = _search(app, world, "zq8ident", k=1)
    assert body["hits"] and body["arms"]["lexical"]["partial"] is True and body["truncated"] is True
    world["env"].hosts["same"].mode = "same"
    _tick(app, world)
    world["env"].hosts["same"].mode = "down"
    body = _search(app, world, "zq8ident", k=1)
    assert body["hits"] and body["arms"]["lexical"]["partial"] is False
    assert o.execute("SELECT count(*) FROM mesh_ctx_lexical WHERE space = %s", (sp,)).fetchone()[0] == 2


# ------------------------------------------------------------------ a reviewer's cross-family read of #389/#390

def test_a_dead_lettered_batch_of_the_served_gen_is_flagged(app, world):
    """a reviewer P1-1 (1): batch 1 of gen 1 is served, batch 2 of gen 1 is dead; the gen is incomplete."""
    _batch(world, 1, [("user_text", "batch one words")])
    _batch(world, 2, [("user_text", "never arrives")], write_items=False)
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)
    body = _search(app, world, "batch one words", k=1)
    assert body["hits"] and body["sources_with_unindexed_newer_gen"] == 1 and body["truncated"] is True


def test_a_dead_letter_with_an_unreadable_gen_is_counted_not_a_crash(app, world):
    """a reviewer P1-1 (4): a manifest dead-lettered for a malformed gen must not make the search fail."""
    _batch(world, 1, [("user_text", "served words")])
    _tick(app, world)
    seat, src = world["author"]["fallback"], world["src"]
    with psycopg.connect(DSN, autocommit=True) as c:
        bad = sr.insert(c, _row(world, f"ctxm/{seat}/{src}/0009/000001",
                                {"schema": "context.v1", "kind": "manifest", "seat": seat, "src": src, "gen": "abc"}))
    world["owner"].execute("INSERT INTO ctx_dead_letter (seq, memory_id, space, reason, attempts) VALUES "
                           "(999999999, %s, %s, 'malformed gen', 5)", (bad, world["env"].space.name))
    body = _search(app, world, "served words", k=1)
    assert body["hits"] and body["sources_with_unindexed_newer_gen"] == 1 and body["truncated"] is True


def test_another_spaces_dead_letters_do_not_flag_this_space(app, world):
    """a reviewer P1-1 (3): dead letters are keyed by (space, seq); another space's are not this answer's."""
    m = _batch(world, 1, [("user_text", "clean here")])
    _tick(app, world)
    world["owner"].execute("INSERT INTO ctx_dead_letter (seq, memory_id, space, reason, attempts) VALUES "
                           "(999999998, %s, 'other-space', 'elsewhere', 5)", (m,))
    body = _search(app, world, "clean here", k=1)
    assert body["hits"] and body["sources_with_unindexed_newer_gen"] == 0


def test_an_unwalked_old_manifest_makes_the_answer_say_the_index_is_behind(app, world):
    """a reviewer P2-1/P2-2: catch-up lag and a re-walk are reported, not a complete-looking answer."""
    _batch(world, 1, [("user_text", "walked words")])
    _tick(app, world)
    assert _search(app, world, "walked words", k=1)["index_behind"] is False, "control: caught up"
    late = _batch(world, 2, [("user_text", "not walked yet")])
    world["owner"].execute("UPDATE ctx_commit_order SET stamped_at = now() - interval '1 hour' WHERE memory_id = %s",
                           (late,))
    body = _search(app, world, "walked words", k=1)
    assert body["index_behind"] is True and body["truncated"] is True
    _tick(app, world)
    assert _search(app, world, "walked words", k=1)["index_behind"] is False


def test_item_collapse_keeps_two_seats_with_the_same_src_apart():
    """a reviewer P2-3: an item is (seat, src, gen, ordinal)."""
    import types
    a = types.SimpleNamespace(seat="a", src="s", gen=1, first_ord=0)
    b = types.SimpleNamespace(seat="b", src="s", gen=1, first_ord=0)
    assert ctxsearch._collapse([a, b], "item") == [a, b]


def test_opening_the_gate_marks_the_lexical_arm_partial_until_the_rewalk_catches_up(app, world):
    """a reviewer on #390 P2-1: the gate-open re-walk rebuilds lexical rows too, so the arm says it is partial."""
    _batch(world, 1, [("user_text", "first gate words")])
    _batch(world, 2, [("user_text", "second gate words")])
    _tick(app, world)
    env = world["env"]
    env.settings([env.embed("same", env.hosts["same"])], gate=None)       # close
    _tick(app, world)
    env.settings([env.embed("same", env.hosts["same"])], gate=True)       # open: the walk restarts
    _tick(app, world, max_manifests=1)
    body = _search(app, world, "first gate words", k=1)
    assert body["arms"]["lexical"]["partial"] is True and body["truncated"] is True
    _tick(app, world)
    assert _search(app, world, "first gate words", k=1)["arms"]["lexical"]["partial"] is False


def test_a_dead_lettered_last_seq_does_not_hold_the_lexical_arm_partial(app, world):
    """Codex on #390: the re-walk skips a dead seq, so `watermark < until` alone would stay true for good."""
    _batch(world, 1, [("user_text", "kept lexical words")])
    poison = _batch(world, 2, [("user_text", "never arrives")], write_items=False)
    for _ in range(ctxjob.MAX_ATTEMPTS):
        _tick(app, world)
    o, sp = world["owner"], world["env"].space.name
    o.execute("UPDATE ctx_index_state SET lexical_derivation = NULL WHERE space = %s", (sp,))   # as 048 up leaves it
    _tick(app, world)                                        # reset; until = the poison seq; the walk skips it
    until = o.execute("SELECT lexical_rewalk_until FROM ctx_index_state WHERE space = %s", (sp,)).fetchone()[0]
    assert until == _seq(world, poison), "control: the reset recorded the dead seq as the target"
    world["env"].hosts["same"].mode = "down"
    body = _search(app, world, "kept lexical words", k=1)
    assert body["hits"] and body["arms"]["lexical"]["partial"] is False


# ------------------------------------------------------------------ rerank-before-collapse, arm guarantee & SQL fault isolation

def _enable_rerank(world):
    e = world["env"]
    host = e.hosts["same"]
    e.settings([e.embed("same", host), {"id": "rr-same", "kind": "rerank", "base_url": host.base_url}], gate=True)
    return host


def test_fused_pool_reserves_arm_guarantee_and_rerank_candidates_bound_groups():
    """Unit check (no DB): ARM_GUARANTEE reserves single-arm top hits past dual-arm noise, and _rerank_candidates caps
    any single noisy group at RERANK_PER_GROUP while keeping the first k groups and arm-reserved hits."""
    from apps.meshsem import fusion

    assert ctxsearch.ARM_GUARANTEE == 10 and ctxsearch.RERANK_PER_GROUP == 3
    now = _dt.datetime(2026, 11, 7, tzinfo=_dt.timezone.utc)

    def mk_hit(cid: int, src: str, first_ord: int = 0) -> ctxindex.Hit:
        return ctxindex.Hit(chunk_id=cid, src_id=f"00000000-0000-0000-0000-{cid:012d}", seat="a", src=src, gen=1,
                            batch=1, first_ord=first_ord, last_ord=first_ord, char_start=0, char_end=10,
                            time_first=now, time_last=now, score=0.0)

    # 1..30 appear on both arms; 900 is #1 on embed alone, so plain RRF pushes 900 to rank 31 (> limit=25).
    embed_ids = ["900"] + [str(i) for i in range(1, 31)]
    lex_ids = [str(i) for i in range(1, 31)]
    fused = fusion.rrf([
        fusion.Ranking(fusion.EMBED, embed_ids, [1.0 - i * 0.01 for i in range(len(embed_ids))]),
        fusion.Ranking(fusion.LEXICAL, lex_ids, [1.0 - i * 0.01 for i in range(len(lex_ids))]),
    ])
    by_id = {cid: mk_hit(cid, f"s-{cid}") for cid in [900, *range(1, 31)]}
    ranked, guaranteed, reserved_ids = ctxsearch._fused_pool(fused, by_id, limit=25)
    assert len(ranked) == 25
    assert 900 in [h.chunk_id for h in ranked] and 900 in reserved_ids
    assert guaranteed[fusion.EMBED] == 1 and guaranteed[fusion.LEXICAL] == 0

    # Session "noisy" holds 12 top chunks; sessions s1..s4 sit behind it, plus reserved chunk 99 at the tail.
    many = ([mk_hit(i, "noisy", first_ord=i) for i in range(1, 13)]
            + [mk_hit(20 + j, f"s{j}") for j in range(1, 5)]
            + [mk_hit(99, "reserved-sess")])
    cands = ctxsearch._rerank_candidates(many, "session", k=3, limit=9, per_group=3, reserved_ids={99})
    assert len(cands) == 8, "3 from noisy + 4 from s1..s4 + 1 reserved = 8, even with limit=9"
    assert [c.chunk_id for c in cands] == sorted(c.chunk_id for c in cands), "fused rank order is preserved"
    assert sum(1 for c in cands if c.src == "noisy") == 3, "noisy group is capped at RERANK_PER_GROUP"
    assert {"noisy", "s1", "s2", "s3", "s4", "reserved-sess"} <= {c.src for c in cands}


def test_collapse_runs_after_reranking_for_session_and_item_and_falls_back_when_down(app, world):
    """Within one session or item, a noisy tool_exchange/header chunk can win first-stage RRF while the assistant
    answer wins cross-encoder reranking; collapsing AFTER reranking returns the answer, and a down reranker preserves
    the exact first-stage collapse winner."""
    from apps.meshsem import graphtext, providers

    host = _enable_rerank(world)
    _batch(world, 1, [("user_text", "deploy checklist"),
                      ("tool_exchange", "deploy checklist deploy checklist tool_json_blob")])
    _batch(world, 2, [("user_text", "deploy checklist"),
                      ("assistant_text", "Run migrations first and freeze merges at noon.")])
    _tick(app, world)

    ans_text = "[user_text]\ndeploy checklist\n\n[assistant_text]\nRun migrations first and freeze merges at noon."
    host.rerank_table[("deploy checklist", ans_text)] = 25.0

    no_rr = ctxsearch.search(app, world["env"].space, world["ns"], "deploy checklist", ctxindex.Filters(),
                             k=5, rerank=False, collapse="session")
    assert len(no_rr["hits"]) == 1 and no_rr["hits"][0]["batch"] == 1
    assert "tool_json_blob" in no_rr["hits"][0]["text"]
    assert no_rr["arms"]["rerank"]["ran"] is False and no_rr["method"] == "rrf(lexical,embed)"

    with_rr = ctxsearch.search(app, world["env"].space, world["ns"], "deploy checklist", ctxindex.Filters(),
                               k=5, rerank=True, collapse="session")
    assert len(with_rr["hits"]) == 1 and with_rr["hits"][0]["batch"] == 2
    assert "freeze merges at noon" in with_rr["hits"][0]["text"]
    assert with_rr["hits"][0]["rerank_score"] == pytest.approx(25.0)
    assert with_rr["arms"]["rerank"]["ran"] is True and with_rr["method"] == "rrf(lexical,embed)+rerank"

    # Reranker down: falls back cleanly to the first-stage collapsed winner without claiming rerank ran.
    host.rerank_mode = "down"
    providers.forget()
    down_rr = ctxsearch.search(app, world["env"].space, world["ns"], "deploy checklist", ctxindex.Filters(),
                               k=5, rerank=True, collapse="session")
    assert len(down_rr["hits"]) == 1 and down_rr["hits"][0]["batch"] == 1
    assert down_rr["hits"][0]["rerank_score"] is None and down_rr["arms"]["rerank"]["ran"] is False
    assert down_rr["method"] == "rrf(lexical,embed)"
    host.rerank_mode = "good"
    providers.forget()

    # Item collapse: a single long item split across 2 chunks sharing (seat, src, gen, first_ord) promotes chunk 2.
    pad = "fillerword " * ((graphtext.MAX_CHARS // 11) + 20)
    long_reply = "itemprobe itemprobe itemprobe noisy_prefix " + pad + " concise_item_resolution"
    _batch(world, 3, [("user_text", long_reply)])
    _tick(app, world)
    all_chunks = ctxsearch.search(app, world["env"].space, world["ns"], "itemprobe", ctxindex.Filters(),
                                  k=10, rerank=False, collapse="chunk")["hits"]
    b3_chunks = [h for h in all_chunks if h["batch"] == 3]
    assert len(b3_chunks) >= 2 and len({h["first_ord"] for h in b3_chunks}) == 1
    tail_chunk = next(h for h in b3_chunks if "concise_item_resolution" in h["text"])
    host.rerank_table[("itemprobe", tail_chunk["text"])] = 40.0

    item_no_rr = ctxsearch.search(app, world["env"].space, world["ns"], "itemprobe", ctxindex.Filters(),
                                  k=5, rerank=False, collapse="item")
    item_rr = ctxsearch.search(app, world["env"].space, world["ns"], "itemprobe", ctxindex.Filters(),
                               k=5, rerank=True, collapse="item")
    assert "noisy_prefix" in item_no_rr["hits"][0]["text"]
    assert "concise_item_resolution" in item_rr["hits"][0]["text"]


def test_arm_guarantee_delivers_embed_only_hit_past_dual_arm_noise(app, world, monkeypatch):
    """End to end: 10 dual-arm keyword chunks fill POOL=8, while an embed-only chunk sits #1 on embed and absent on
    lexical; ARM_GUARANTEE reserves it into the pool so reranking surfaces it first."""
    host = _enable_rerank(world)
    monkeypatch.setattr(ctxsearch, "POOL", 8)

    for b in range(1, 11):
        filler = " ".join(f"filler{b}_{w}" for w in range(35))
        _batch(world, b, [("user_text", f"kwonly {filler}")])
    # Shares the query prompt's hashed words ("task search result query") without containing "kwonly".
    _batch(world, 11, [("user_text", "task search result query paraphrase_winner")])
    _tick(app, world)

    target_text = "[user_text]\ntask search result query paraphrase_winner"
    host.rerank_table[("kwonly", target_text)] = 99.0

    body = ctxsearch.search(app, world["env"].space, world["ns"], "kwonly", ctxindex.Filters(), k=3, rerank=True)
    assert body["pool"]["guaranteed"]["embed"] >= 1
    assert "paraphrase_winner" in body["hits"][0]["text"]


def test_lexical_arm_timeout_rolls_back_savepoint_and_serves_vector_hits(app, world, monkeypatch):
    """Real PostgreSQL statement_timeout inside _run_lexical_arm's SAVEPOINT raises QueryCanceled, rolls back cleanly
    without poisoning the outer serving transaction, degrades to vector-only, and raises 503 only if embed also failed."""
    _batch(world, 1, [("user_text", "savepoint vector survivor")])
    _tick(app, world)

    monkeypatch.setattr(ctxsearch, "LEX_TIMEOUT_MS", 40)

    def slow_lexical(cur, *args, **kwargs):
        cur.execute("SELECT pg_sleep(0.3)")
        return [], {}  # pragma: no cover

    monkeypatch.setattr(ctxsearch, "_lexical_arm", slow_lexical)
    body = ctxsearch.search(app, world["env"].space, world["ns"], "savepoint vector survivor",
                            ctxindex.Filters(), k=3, rerank=False)
    assert len(body["hits"]) == 1 and "savepoint vector survivor" in body["hits"][0]["text"]
    assert body["arms"]["vector"]["ran"] is True
    assert body["arms"]["lexical"] == {
        "ran": False, "candidates": 0, "truncated": True, "partial": False,
        "timed_out": True, "reason": "lexical query timed out",
    }
    assert body["method"] == "embed" and body["truncated"] is True

    world["env"].hosts["same"].mode = "down"
    with pytest.raises(errors.ApiError) as exc:
        ctxsearch.search(app, world["env"].space, world["ns"], "savepoint vector survivor",
                         ctxindex.Filters(), k=3, rerank=False)
    assert exc.value.status == 503 and exc.value.code == "E_EMBED_UNAVAILABLE"
    assert "lexical query timed out" in exc.value.message


def test_unfiltered_narrow_cap_and_pk_point_probe_avoid_gin_and_seq_scan(app, world, monkeypatch):
    """Unfiltered lexical searches bound chunk reads to newest LEX_NARROW_MAX rows, flag truncation, and probe
    mesh_ctx_lexical via mesh_ctx_lexical_pkey (never mesh_ctx_lexical_terms GIN or a Seq Scan)."""
    _batch(world, 1, [("user_text", "oldest narrowcap_term turn")])
    for b in range(2, 14):
        _batch(world, b, [("user_text", f"recent {b} narrowcap_term turn")])
    _tick(app, world)

    monkeypatch.setattr(ctxsearch, "LEX_NARROW_MAX", 8)
    world["env"].hosts["same"].mode = "down"
    body = _search(app, world, "narrowcap_term", k=10)
    lex = body["arms"]["lexical"]
    assert lex["candidates"] == 8 and lex["truncated"] is True and lex["scored_exactly"] is False
    assert all("oldest" not in (h["text"] or "") for h in body["hits"])

    captured: list[tuple[str, tuple]] = []
    with psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
        with c.cursor() as cur:
            cur.execute("SELECT id, src_id::text AS src_id, seat, src, gen, batch, project, first_ord, last_ord, "
                        "char_start, char_end, time_first, time_last, text_sha256 "
                        "FROM mesh_ctx_chunks WHERE space = %s ORDER BY id DESC LIMIT 5",
                        (world["env"].space.name,))
            sample = cur.fetchall()

            class SpyCursor:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, params=None):
                    if "FROM mesh_ctx_lexical" in sql:
                        captured.append((sql, params))
                    return self._inner.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(self._inner, name)

            ctxsearch._score_lexical_chunks(SpyCursor(cur), world["env"].space, sample, ["narrowcap_term"], False)
            assert len(captured) == 1
            sql, params = captured[0]
            cur.execute("SET enable_seqscan = on")
            cur.execute("EXPLAIN (FORMAT TEXT) " + sql, params)
            plan = "\n".join(next(iter(r.values())) for r in cur.fetchall())
    assert "mesh_ctx_lexical_pkey" in plan, plan
    assert "mesh_ctx_lexical_terms" not in plan and "Seq Scan on mesh_ctx_lexical" not in plan, plan


# ------------------------------------------------------------------ admission gate and arm budgets (7 Nov 2026)
# Production on HQD stopped answering from 08:16 to 08:27Z: 14 transcript searches at once, idle in transaction after
# their vector fetch for up to 22 minutes while daphne decoded on a box at 95% RAM. No statement_timeout could fire.

def test_the_gate_admits_its_slots_refuses_the_next_and_gives_a_slot_back_on_error():
    """No database. Two slots: a third search is refused with 503 E_CTX_SEARCH_BUSY and a Retry-After, and a search
    that raises still gives its slot back (control: both slots are free again afterwards)."""
    gate = ctxsearch._Gate(2)
    with gate.admit(0.01), gate.admit(0.01):
        with pytest.raises(errors.ApiError) as exc:
            with gate.admit(0.01):
                pass                                                      # pragma: no cover
    assert exc.value.status == 503 and exc.value.code == ctxsearch.E_BUSY == "E_CTX_SEARCH_BUSY"
    assert exc.value.retry_after == ctxsearch.BUSY_RETRY_AFTER_S and type(exc.value).__name__ == "ApiError"
    with pytest.raises(RuntimeError):
        with gate.admit(0.01):
            raise RuntimeError("the search failed")
    with gate.admit(0.01), gate.admit(0.01):                              # both slots came back
        pass


def test_the_concurrency_comes_from_the_env_and_nonsense_keeps_the_default(monkeypatch):
    monkeypatch.delenv("MESHY_CTX_SEARCH_CONCURRENCY", raising=False)
    assert ctxsearch.configured_concurrency() == ctxsearch.STORE_CONCURRENCY == 2
    monkeypatch.setenv("MESHY_CTX_SEARCH_CONCURRENCY", "3")
    assert ctxsearch.configured_concurrency() == 3
    for bad in ("0", "-1", "two", "1.5", " "):
        monkeypatch.setenv("MESHY_CTX_SEARCH_CONCURRENCY", bad)
        assert ctxsearch.configured_concurrency() == 2, bad


def test_the_vector_fetch_checks_its_deadline_before_every_batch(monkeypatch):
    """No database: a cursor that records statements. A deadline already past runs no statement; one that passes after
    the first batch runs exactly one; no deadline runs every batch (the behaviour before 7 Nov)."""
    class _Space:
        name, dims = "test-space", 4

    class _Cur:
        def __init__(self):
            self.statements = 0

        def execute(self, sql, params=None):
            self.statements += 1

        def fetchall(self):
            return []

    rows = [{"text_sha256": f"{i:064x}"} for i in range(3)]
    monkeypatch.setattr(ctxindex, "VECTOR_FETCH_BATCH", 1)

    cur = _Cur()
    with pytest.raises(ctxindex.Deadline):
        ctxindex._fetch_vectors_by_digest(cur, _Space, [dict(r) for r in rows], deadline=0.0)
    assert cur.statements == 0

    cur = _Cur()
    clock = iter([0.0, 2.0])                                              # batch 1 at t=0, batch 2 at t=2

    class _Time:
        @staticmethod
        def monotonic():
            return next(clock)

    real_time = ctxindex.time
    monkeypatch.setattr(ctxindex, "time", _Time)
    with pytest.raises(ctxindex.Deadline) as exc:
        ctxindex._fetch_vectors_by_digest(cur, _Space, [dict(r) for r in rows], deadline=1.0)
    assert cur.statements == 1 and "vector batch 2" in str(exc.value)
    monkeypatch.setattr(ctxindex, "time", real_time)                     # VECTOR_FETCH_BATCH stays 1

    cur = _Cur()
    ctxindex._fetch_vectors_by_digest(cur, _Space, [dict(r) for r in rows])
    assert cur.statements == 3


def test_the_lexical_chunk_collection_checks_its_deadline_before_every_read():
    """No database: a scripted cursor. Past the deadline neither the seat path's shard read nor the cross-seat path's
    window read runs (a reviewer ede749 on merging #537 R2); without one, each runs (the control)."""
    class _Cur:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql)

        def fetchone(self):
            return {"shard_max": ctxindex.SHARD_MAX}

        def fetchall(self):
            last = self.sql[-1]
            if "FROM ctx_shard_plan" in last and "month, lo, hi" in last:
                return [{"month": _dt.datetime(2026, 11, 1, tzinfo=_dt.timezone.utc), "lo": 1, "hi": 10}]
            if "FROM ctx_shard_plan" in last:
                return [{"lo": 1, "hi": 10}]
            return []

    class _Space:
        name, dims = "test-space", 4

    ns = "shared/transcript-context"
    for f, what in ((ctxindex.Filters(seat="kxxx"), "lexical shard 1"), (ctxindex.Filters(), "lexical window 1")):
        cur = _Cur()
        with pytest.raises(ctxindex.Deadline) as exc:
            ctxsearch._collect_lexical_chunks(cur, _Space, ns, f, 50, deadline=0.0)
        assert what in str(exc.value) and not any("FROM mesh_ctx_chunks" in s for s in cur.sql), cur.sql
        cur = _Cur()
        assert ctxsearch._collect_lexical_chunks(cur, _Space, ns, f, 50) == ([], False)
        assert any("FROM mesh_ctx_chunks" in s for s in cur.sql), "without a deadline the read runs"


def test_the_search_route_sends_retry_after_for_a_busy_refusal_only():
    """By reading the source, as the not-indexed test above does: the route catches only E_CTX_SEARCH_BUSY and gives it
    a Retry-After; every other ApiError is re-raised to _route unchanged."""
    import inspect
    src = inspect.getsource(views)
    assert "if exc.code != ctxsearch.E_BUSY:\n                raise" in src
    assert 'headers={"Retry-After": str(exc.retry_after)}' in src


def test_a_full_gate_refuses_before_the_embed(app, world, monkeypatch):
    from apps.meshsem import providers
    _batch(world, 1, [("user_text", "gate probe words")])
    _tick(app, world)
    embeds = []
    real_embed = providers.embed_in_space
    monkeypatch.setattr(providers, "embed_in_space", lambda *a, **kw: embeds.append(1) or real_embed(*a, **kw))
    monkeypatch.setattr(ctxsearch, "_gate", ctxsearch._Gate(1))
    monkeypatch.setattr(ctxsearch, "STORE_QUEUE_S", 0.05)
    with ctxsearch._gate.admit(0):                                        # another search holds the one slot
        with pytest.raises(errors.ApiError) as exc:
            _search(app, world, "gate probe words", k=3)
    assert exc.value.code == ctxsearch.E_BUSY and exc.value.status == 503 and embeds == []
    body = _search(app, world, "gate probe words", k=3)                   # control: a free slot answers
    assert embeds == [1] and "gate probe words" in body["hits"][0]["text"] and "queue_ms" in body["timings"]


def test_the_vector_arm_past_its_budget_leaves_the_lexical_arm_to_answer(app, world, monkeypatch):
    _batch(world, 1, [("user_text", "vector budget probe")])
    _tick(app, world)
    control = _search(app, world, "vector budget probe", k=3)
    assert control["arms"]["vector"]["ran"] is True and control["method"] == "rrf(lexical,embed)"
    monkeypatch.setattr(ctxsearch, "VECTOR_BUDGET_S", -1.0)              # the deadline has passed before the first check
    body = _search(app, world, "vector budget probe", k=3)
    assert body["arms"]["vector"]["ran"] is False and body["arms"]["vector"]["timed_out"] is True
    assert "vector arm stopped before" in body["arms"]["vector"]["reason"]
    assert body["method"] == "lexical" and body["truncated"] is True
    assert "vector budget probe" in body["hits"][0]["text"]


def test_the_lexical_arm_past_its_budget_leaves_the_vector_arm_to_answer(app, world, monkeypatch):
    _batch(world, 1, [("user_text", "lexical budget probe")])
    _tick(app, world)
    monkeypatch.setattr(ctxsearch, "LEX_BUDGET_S", -1.0)
    body = _search(app, world, "lexical budget probe", k=3)
    lex = body["arms"]["lexical"]
    assert lex["ran"] is False and lex["timed_out"] is True and "lexical arm stopped before" in lex["reason"]
    assert body["arms"]["vector"]["ran"] is True and body["method"] == "embed" and body["truncated"] is True
    assert "lexical budget probe" in body["hits"][0]["text"]


def test_both_arms_past_their_budgets_is_a_503_naming_both(app, world, monkeypatch):
    _batch(world, 1, [("user_text", "both budgets probe")])
    _tick(app, world)
    monkeypatch.setattr(ctxsearch, "VECTOR_BUDGET_S", -1.0)
    monkeypatch.setattr(ctxsearch, "LEX_BUDGET_S", -1.0)
    with pytest.raises(errors.ApiError) as exc:
        _search(app, world, "both budgets probe", k=3)
    assert exc.value.status == 503 and "vector arm stopped" in exc.value.message
    assert "lexical arm stopped" in exc.value.message


def test_lexical_arm_sql_gin_fallback_runs_when_prefilter_cold_shards_is_off(app, world, monkeypatch):
    """kxxx 28ff33 condition 2: _lexical_arm_sql stays exercised when PREFILTER_COLD_SHARDS is False and matches the
    prefiltered PK-probe scorer."""
    _batch(world, 1, [("user_text", "gin_fallback_token alpha beta")])
    _batch(world, 2, [("user_text", "gin_fallback_token alpha")])
    _tick(app, world)
    world["env"].hosts["same"].mode = "down"

    prefiltered = _search(app, world, "gin_fallback_token alpha", k=5)
    monkeypatch.setattr(ctxindex, "PREFILTER_COLD_SHARDS", False)
    sql_fallback = _search(app, world, "gin_fallback_token alpha", k=5)

    assert [(h["batch"], h["score"]) for h in sql_fallback["hits"]] == [
        (h["batch"], h["score"]) for h in prefiltered["hits"]
    ]
    assert sql_fallback["arms"]["lexical"]["scored_exactly"] is True
    assert sql_fallback["arms"]["lexical"]["truncated"] is False


def test_collect_lexical_chunks_bounds_unfiltered_and_seat_reads_without_sort_or_subplan(app, world, monkeypatch):
    """kxxx 28ff33 condition 1: _collect_lexical_chunks reads unfiltered (backward PK windows) and seat-filtered
    (shard-bounded, with and without since) chunks via ctx_shard_plan + _latest_gens, with no SQL Sort on seat shards
    and no correlated SubPlan."""
    for b in range(1, 10):
        _batch(world, b, [("user_text", f"shardbound_probe turn {b}")])
    _tick(app, world)
    seat = world["author"]["fallback"]
    since = _dt.datetime(2026, 10, 23, tzinfo=_dt.timezone.utc)

    monkeypatch.setattr(ctxsearch, "LEX_ID_WINDOW", 3)
    chunk_Queries: list[tuple[str, list]] = []

    with dbconn_internal(app) as c:
        with c.cursor(row_factory=psycopg.rows.dict_row) as cur:
            class SpyCur:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, params=None):
                    if "FROM mesh_ctx_chunks c WHERE" in sql:
                        chunk_Queries.append((sql, list(params or [])))
                    return self._inner.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(self._inner, name)

            spy = SpyCur(cur)
            unf_rows, unf_capped = ctxsearch._collect_lexical_chunks(
                spy, world["env"].space, world["ns"], ctxindex.Filters(), 5
            )
            seat_since_rows, seat_since_capped = ctxsearch._collect_lexical_chunks(
                spy, world["env"].space, world["ns"], ctxindex.Filters(seat=seat, since=since), 50
            )
            seat_all_rows, seat_all_capped = ctxsearch._collect_lexical_chunks(
                spy, world["env"].space, world["ns"], ctxindex.Filters(seat=seat), 5
            )

            assert len(unf_rows) == 5 and unf_capped is True
            assert [int(r["id"]) for r in unf_rows] == sorted((int(r["id"]) for r in unf_rows), reverse=True)
            assert len(seat_since_rows) == 9 and seat_since_capped is False
            assert len(seat_all_rows) == 5 and seat_all_capped is True
            assert [int(r["id"]) for r in seat_all_rows] == sorted((int(r["id"]) for r in seat_all_rows), reverse=True)

            assert chunk_Queries, "expected chunk queries"
            for sql, params in chunk_Queries:
                assert "ctx_source_gen" not in sql, sql
                cur.execute("EXPLAIN (FORMAT TEXT) " + sql, params)
                plan = "\n".join(next(iter(r.values())) for r in cur.fetchall())
                assert "SubPlan" not in plan, plan


def dbconn_internal(conn):
    from apps.core import dbconn
    return dbconn.request_server_internal(conn)
