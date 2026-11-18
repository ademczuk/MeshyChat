"""The graph walker: vectors and lexical terms for namespaces other than the mail (divergence 060).

Design: docs/design/wiki-in-graph.md (OI-57). Registered in apps/jobs/registry.py as
`mesh_graph_embed`, and runnable by hand as `python -m apps.meshsem graph-catch-up`. It writes
only the rows of the namespaces MESHY_GRAPH_NAMESPACES lists besides shared/mesh-chat, which keeps
its own job (apps/meshsem/embedjob.py) and is never touched here. With the setting at its default
(the mail only) a tick reads nothing and writes nothing: the rollback is to unset it.

RECONCILE BY SET, NOT BY WATERMARK (design 4.3). The mail is high-volume and append-only, so its
job walks the commit order. The wiki is neither: two writes in the month after the cutover, and a
new version of an entry is a new row that makes the old one stop being head. So each tick compares
the COMPLETE set of the namespace's head rows with the COMPLETE set of rows holding derived rows,
and has nothing to skip past and no late commit to miss:

  1. (transaction) read every head of the namespace, and every derived digest of it;
  2. (no transaction) decide each head: kept out by the gate, no content (a tombstone, a data row),
     unchanged, or to derive; and each derived row whose source is no longer a head: stale;
  3. (no transaction) embed the chunks of the rows to derive, a WHOLE row at a time, until the
     chunk bound, the deadline, or no provider is admitted;
  4. (transaction) delete the stale, the ineligible and the replaced, write the finished rows'
     chunks, and lex every row to derive whose text passed the gate (the lexical arm needs no
     model, so an embedder outage does not stall it, as in the mail job).

A row is written whole or not at all: a row with some chunks embedded would read as derived and
search would rank it by the part it has. The first row of a tick is embedded however many chunks
it has, so one long entry cannot stall the walk behind a small bound.

THE COST is two reads of about a thousand rows per tick, the heads' values included (the text rule
is Python's, graphtext.row_text), so the job runs every five minutes, not every minute. If a graph
namespace ever grows to mail volume, it moves to a commit-order walk then.

The space's reference canaries are the mail job's: one space, one reference, recorded by whichever
job embeds first, under the same write-once rule (store.record_reference).
"""
from __future__ import annotations

import dataclasses
import hashlib
import time
from typing import NamedTuple

import numpy as np
from psycopg.rows import dict_row

from apps.core import env, rls
from apps.jobs.result import TickResult
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from . import graphtext, providers, spaces, store

#: Chunks EMBEDDED per namespace per tick (the mail job bounds rows; a graph row is many chunks).
EMBED_PER_TICK = 256
#: Texts per HTTP call, as the mail job sends.
SUB_BATCH = 32


def graph_namespaces() -> tuple[str, ...]:
    """MESHY_GRAPH_NAMESPACES: a list, or a comma-separated string. Default: the mail only."""
    raw = env.setting("MESHY_GRAPH_NAMESPACES", "MESHY_GRAPH_NAMESPACES", default=None)
    if raw is None:
        return (MESH_CHAT_NAMESPACE,)
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return tuple(out) or (MESH_CHAT_NAMESPACE,)


def walked_namespaces() -> tuple[str, ...]:
    """The namespaces this walker derives: the listed ones except the mail, which has its own job."""
    return tuple(ns for ns in graph_namespaces() if ns != MESH_CHAT_NAMESPACE)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class Report:
    namespace: str
    space: str
    heads: int
    #: Heads whose vectors and terms were written this tick, and how many chunks that was.
    embedded_rows: int
    embedded_chunks: int
    #: Heads whose lexical row was written this tick (whether or not the embedder took them).
    lexed: int
    unchanged: int
    skipped_forbidden: int
    #: Heads with no content-class string (graphtext.row_text is None): data rows and tombstones.
    skipped_no_content: int
    #: Rows whose derived rows were deleted: no longer head, gone, now without content, or gated.
    deleted_rows: int
    #: Heads still to derive after this tick.
    pending: int
    gate_open: bool
    providers_used: tuple[str, ...]
    #: None, or why the tick stopped with rows pending: "embed_bound", "deadline",
    #: "provider_unavailable".
    stopped: str | None
    attempts: tuple[dict, ...]
    more: bool


_HEADS = ("SELECT id, key, version, embed_policy, created_at, value FROM shared_memory "
          "WHERE namespace = %s AND is_head ORDER BY created_at, id")


class _Todo(NamedTuple):
    """A head the tick derives something for: its vectors, its lexical row, or both."""
    head: dict
    text: str
    #: The chunk texts, and the digests their vectors are stored under, in chunk order.
    parts: list[str]
    digests: tuple[str, ...]
    #: The digest the lexical row is stored under.
    lex_sha: str
    needs_vectors: bool
    needs_lex: bool


def catch_up_namespace(conn, namespace: str, *, space: spaces.Space | None = None,
                       embed_per_tick: int = EMBED_PER_TICK,
                       deadline_s: float | None = 30.0) -> Report:
    """One bounded tick over one graph namespace. Idempotent. `conn`: autocommit, no open
    transaction (rls.py)."""
    if namespace == MESH_CHAT_NAMESPACE:
        raise ValueError(f"{MESH_CHAT_NAMESPACE!r} has its own job (apps/meshsem/embedjob.py); "
                         "the graph walker never writes it")
    space = space or store.configured_space()
    gate = store.forbidden_rows_open()
    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    # ---- 1. read ------------------------------------------------------------------------
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            store.ensure_state(cur, space)
            reference = store.reference_of(store.read_state(cur, space), space)
            cur.execute(_HEADS, (namespace,))
            heads = cur.fetchall()
            derived = store.derived_digests(cur, space, namespace)

    # ---- 2. decide ----------------------------------------------------------------------
    head_ids = {str(h["id"]) for h in heads}
    delete = [sid for sid in derived if sid not in head_ids]            # no longer head, or gone
    counts = {"unchanged": 0, "skipped_forbidden": 0, "skipped_no_content": 0}
    todo: list[_Todo] = []
    for h in heads:
        sid = str(h["id"])
        if h["embed_policy"] == "forbidden" and not gate:
            counts["skipped_forbidden"] += 1
            if sid in derived:
                delete.append(sid)
            continue
        found = graphtext.row_text(h["value"])
        if found is None:
            counts["skipped_no_content"] += 1
            if sid in derived:
                delete.append(sid)
            continue
        text = found[1]
        parts = graphtext.chunks(text)
        digests = tuple(_sha(space.document(p)) for p in parts)
        lex_sha = _sha(text)
        have_vecs, have_lex = derived.get(sid, ((), None))
        if have_vecs == digests and have_lex == lex_sha:
            counts["unchanged"] += 1
            continue
        todo.append(_Todo(h, text, parts, digests, lex_sha, have_vecs != digests, have_lex != lex_sha))

    # ---- 3. embed, whole rows only ------------------------------------------------------
    attempts: list[dict] = []
    used: list[str] = []
    stopped: str | None = None
    new_reference: tuple[np.ndarray, str] | None = None
    finished: dict[int, list[tuple[np.ndarray, str]]] = {}      # todo index -> vectors in chunk order

    def _budget() -> float:
        return 20.0 if deadline is None else max(0.0, deadline - time.monotonic())

    wants = [i for i, t in enumerate(todo) if t.needs_vectors]
    if wants and reference is None:
        try:
            got = providers.embed_in_space(space, [], None, budget_s=max(0.1, _budget()),
                                           record_if_unreferenced=True)
            new_reference = (got.canary_vectors, got.provider_id)
            reference = got.canary_vectors
            attempts.extend(got.attempts)
        except providers.EmbedUnavailable as exc:
            attempts.extend(exc.attempts)
            stopped = "provider_unavailable"
    spent = 0
    for i in wants:
        if stopped is not None:
            break
        parts = todo[i].parts
        if finished and spent + len(parts) > embed_per_tick:
            stopped = "embed_bound"
            break
        vecs: list[tuple[np.ndarray, str]] = []
        for start in range(0, len(parts), SUB_BATCH):
            if deadline is not None and time.monotonic() >= deadline:
                stopped = "deadline"
                break
            texts = [space.document(p) for p in parts[start:start + SUB_BATCH]]
            try:
                got = providers.embed_in_space(space, texts, reference, budget_s=max(0.1, _budget()))
            except providers.EmbedUnavailable as exc:
                attempts.extend(exc.attempts)
                stopped = "provider_unavailable"
                break
            attempts.extend(got.attempts)
            if got.provider_id not in used:
                used.append(got.provider_id)
            vecs.extend((v, got.provider_id) for v in got.vectors)
        if len(vecs) == len(parts):
            finished[i] = vecs
            spent += len(parts)

    # ---- 4. write -----------------------------------------------------------------------
    lexed = 0
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            if new_reference is not None:
                stored = store.record_reference(cur, space, new_reference[0], new_reference[1])
                if not np.array_equal(stored, np.asarray(new_reference[0], dtype="<f4")):
                    # Another job recorded first; our vectors were admitted against OUR canaries.
                    finished = {}
            store.delete_derived_in(cur, space, namespace, delete)
            replaced = [str(todo[i].head["id"]) for i in finished]
            if replaced:
                # A row's chunk count can change with the derivation; its old chunks go first.
                cur.execute("DELETE FROM mesh_embeddings WHERE space = %s AND namespace = %s "
                            "AND src_id = ANY(%s::uuid[])", (space.name, namespace, replaced))
            rows = []
            for i, vecs in finished.items():
                h, digests = todo[i].head, todo[i].digests
                for n, ((vec, pid), digest) in enumerate(zip(vecs, digests)):
                    rows.append((h["id"], h["key"], h["version"], n, digest, vec, pid, h["created_at"]))
            store.upsert_chunk_vectors(cur, space, namespace, rows)
            lex = [(t.head["id"], t.head["key"], t.head["version"], t.text, t.lex_sha,
                    t.head["created_at"]) for t in todo if t.needs_lex]
            lexed = store.upsert_lexical_in(cur, space, namespace, lex)

    pending = sum(1 for i in wants if i not in finished)
    return Report(
        namespace=namespace, space=space.name, heads=len(heads), embedded_rows=len(finished),
        embedded_chunks=sum(len(v) for v in finished.values()), lexed=lexed,
        unchanged=counts["unchanged"], skipped_forbidden=counts["skipped_forbidden"],
        skipped_no_content=counts["skipped_no_content"], deleted_rows=len(set(delete)),
        pending=pending, gate_open=gate, providers_used=tuple(used), stopped=stopped,
        attempts=tuple(attempts), more=pending > 0 and stopped in ("embed_bound", "deadline"),
    )


def catch_up(conn, **kw) -> list[Report]:
    """One tick over every walked namespace, in the setting's order."""
    return [catch_up_namespace(conn, ns, **kw) for ns in walked_namespaces()]


class GraphJobFailed(RuntimeError):
    """Raised by the job body AFTER its progress is committed, so the runner records an error:
    rows were pending and no embed provider was admitted (a failure must not read as a pass)."""


def run_graph_job(conn, job) -> TickResult:
    reports = catch_up(conn, deadline_s=job.timeout_secs)
    starved = [r for r in reports if r.stopped == "provider_unavailable" and r.pending]
    if starved:
        seen = dict.fromkeys(f"{a.get('id')}: {a.get('verdict')} ({a.get('reason', '')})"
                             for r in starved for a in r.attempts)
        raise GraphJobFailed(
            "; ".join(f"{r.namespace}: {r.pending} row(s) pending" for r in starved)
            + ", and no admitted embed provider. " + ("; ".join(seen) or "no embed provider declared"))
    rows = sum(r.embedded_rows + r.lexed + r.deleted_rows for r in reports)
    return TickResult(rows=rows, more_work_pending=any(r.more for r in reports))


__all__ = ["EMBED_PER_TICK", "GraphJobFailed", "Report", "SUB_BATCH", "catch_up",
           "catch_up_namespace", "graph_namespaces", "run_graph_job", "walked_namespaces"]
