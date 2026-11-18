"""The embed job: a bounded keyset walk over shared/mesh-chat HEAD rows into mesh_embeddings.

Registered in apps/jobs/registry.py as `mesh_embed_catch_up`, beside mesh_edges_catch_up,
and runnable by hand as `python -m apps.meshsem catch-up`. The only writer of
mesh_embeddings and mesh_embeddings_projection (divergence 019), and of mesh_lexical, the
lexical arm's terms (divergence 026).

ONE TICK, in order:
  1. (transaction) Read the space's state row. If the embed_policy gate differs from the
     one the current walk ran under, restart the walk from the beginning; on a CLOSE,
     first delete every vector and lexical row of a forbidden row, so closing the gate is
     never half-applied. If the lexical derivation differs (a state row from before
     divergence 026, or a code change to lexical.DERIVATION), restart too and drop the
     space's lexical rows of any other derivation. Read up to `max_batch` head rows after
     the watermark.
  2. (no transaction) Decide each row: skipped_forbidden (gate closed),
     skipped_no_schema_version (no text and no schema_version: a legacy row, divergence
     023), skipped_empty (no text, schema_version declared), unchanged (a vector with
     this text_sha256 exists), or to-embed. "No text" is store.has_text_sql, the SAME
     predicate coverage counts by, so a skipped row is never reported as pending.
     Record the space's reference first if it has none (canaries only, no content).
     Embed the to-embed rows in sub-batches, the space's canaries riding in every call
     (apps/meshsem/providers.embed_in_space), until done, the embed bound, the deadline,
     or no provider is admitted.
  3. (transaction) Check the watermark has not moved (a second walker would have moved
     it), write the vectors, write the lexical rows, and advance the watermark ONLY past
     the prefix of rows whose outcome is final. A row that could not be embedded is never
     walked past, so a provider outage delays rows and never loses them.

THE LEXICAL ROWS DO NOT WAIT FOR THE EMBEDDER (divergence 026). Tokenizing needs no model,
so every row this tick READ that has text and passes the gate is lexed, including rows
past the first one the embedder could not take; only a row whose stored lexical digest
(under the current derivation) differs is written. That is what keeps the lexical arm
useful in the one situation search falls back to it alone: while HQB's embedder is down,
new messages are still found lexically, up to one tick's read past the stalled
watermark, and coverage.lexical_indexed says how many. A restarted walk backfills terms
for unchanged vectors without re-embedding them.

The HTTP calls run OUTSIDE any transaction: a transaction held open across a model call
is a lock held for as long as the slowest GPU on the LAN decides.

THE WALK IS IN COMMIT ORDER (STATUS D19, sql_meshychat/023), as the mesh_edges projection's
is: keyed on `mesh_commit_order.seq`, drawn when the row's transaction commits, so a row
whose writer started before a row already walked and committed after it is still walked.
The state row's `watermark_seq` is the position; `watermark_at` / `watermark_id` stay the
GREATEST (created_at, id) processed, because apps/meshsem/related.py bounds its own walk by
them, and they never move backwards (apps/mesh/projection.greatest_key). They do NOT imply
that every row at or below them has a vector: a late-committing row below them that this
walk has not reached yet has none (apps/mesh/projection.py says what related.py then does).

TEXT: the string `body`, stripped, truncated to MAX_CHARS, with the space's DOCUMENT
prompt. text_sha256 is the digest of that exact string. Rows are immutable, so the digest
changes only when this derivation does, and then only the affected rows are re-embedded.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import time

import numpy as np
from psycopg.rows import dict_row

from apps.core import rls
from apps.jobs.result import TickResult
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from . import lexical, providers, spaces, store

#: Rows READ per tick: the job's declared cost bound (B5), and what the report's `more`
#: compares rows read with (the job hands `more` to the runner, STATUS D18).
DEFAULT_BATCH = 2000
#: Rows EMBEDDED per tick. Reading a forbidden row is a comparison; embedding one is a
#: GPU call, so the two get separate bounds.
EMBED_PER_TICK = 256
#: Texts per HTTP call (plus the space's five canaries).
SUB_BATCH = 32
#: The brief's truncation: 1500 characters of body. llama-server on HQB and HQD embedded
#: 1500-character code-dense inputs without refusing (measured 2026-09-23).
MAX_CHARS = 1500

#: `AND o.kind = 'write'`: a redaction position is a second visit to a tombstone (divergence 053), and this walk counts every row it
#: reads and moves its watermark past it. The predicate does not change the plan (measured; tests/test_redaction_delivery.py holds
#: it): with statistics, as production has them, the walk reads the tail of the primary key's range, 11 tuples for the last three
#: positions of a 2,400-row order, with the predicate or without it; a freshly built, unanalyzed database reads every position
#: either way, which the walk did before any redaction existed.
_SELECT_ROWS = (
    "SELECT o.seq, sm.id, sm.key, sm.version, sm.embed_policy, sm.created_at, "
    "  CASE WHEN jsonb_typeof(sm.value->'body') = 'string' "
    "       THEN sm.value->>'body' END AS body, "
    f"  {store.has_text_sql('sm')} AS has_text, "
    f"  {store.has_schema_version_sql('sm')} AS has_schema_version "
    "FROM mesh_commit_order o JOIN shared_memory sm ON sm.id = o.memory_id "
    "WHERE o.seq > %(after_seq)s AND o.kind = 'write' "
    "  AND sm.namespace = %(ns)s AND sm.key LIKE 'msg/%%' AND sm.is_head "
    "ORDER BY o.seq ASC "
    "LIMIT %(limit)s"
)


def _position(state: dict) -> tuple:
    """Everything that says where the walk is. A second walker moves at least one of them;
    a restart NULLs all three."""
    return (state.get("watermark_seq"), state["watermark_at"], state["watermark_id"])


def document_text(body) -> str | None:
    """The text a row contributes, before the prompt. None when there is none."""
    if not isinstance(body, str):
        return None
    text = body.strip()[:MAX_CHARS]
    return text or None


def text_sha256(prompted: str) -> str:
    return hashlib.sha256(prompted.encode("utf-8")).hexdigest()


class ConcurrentWalk(RuntimeError):
    """The watermark moved between this tick's read and its write: another walker ran.
    Nothing is written; the next tick starts from wherever the other one left it."""


@dataclasses.dataclass(frozen=True)
class Report:
    space: str
    rows_read: int
    #: Rows whose outcome is final this tick; the watermark moved past exactly these.
    rows_processed: int
    embedded: int
    unchanged: int
    skipped_forbidden: int
    skipped_empty: int
    #: No text and no schema_version: a legacy row (divergence 023). Counted, never pending.
    skipped_no_schema_version: int
    gate_open: bool
    gate_changed: bool
    #: Vectors deleted because the gate CLOSED this tick.
    purged: int
    #: Lexical rows written this tick: rows READ with text that passes the gate whose
    #: stored lexical digest differed, whether or not the embedder took them.
    lexed: int
    #: The walk restarted because the state row's lexical derivation was not the code's.
    derivation_changed: bool
    reference_recorded: bool
    providers_used: tuple[str, ...]
    #: None, or why the tick stopped before processing every row it read:
    #: "embed_bound", "deadline", "provider_unavailable".
    stopped: str | None
    attempts: tuple[dict, ...]
    watermark_at: _dt.datetime | None
    watermark_id: str | None
    #: True when the log may hold more: the read hit max_batch or the tick stopped early.
    more: bool


def _restart_walk(cur, space: spaces.Space, gate: bool, *, closing: bool,
                  derivation_changed: bool) -> int:
    """Restart the walk: the operator flipped the gate, or the lexical derivation is not
    the one the walk wrote. Returns vectors purged."""
    purged = 0
    if closing:
        # CLOSING: the vectors and terms of forbidden rows go now, not after a re-walk.
        # Reads already hide them (store.coverage, search.py), this makes the tables agree.
        cur.execute("DELETE FROM mesh_embeddings me USING shared_memory sm "
                    "WHERE me.src_id = sm.id AND me.space = %s "
                    "AND sm.embed_policy = 'forbidden'", (space.name,))
        purged = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        cur.execute("DELETE FROM mesh_lexical ml USING shared_memory sm "
                    "WHERE ml.src_id = sm.id AND ml.space = %s "
                    "AND sm.embed_policy = 'forbidden'", (space.name,))
    if derivation_changed:
        # Two derivations never share a space's lexical arm: the rows of the old one go,
        # and the restarted walk writes the new one (divergence 026).
        cur.execute("DELETE FROM mesh_lexical WHERE space = %s AND derivation <> %s",
                    (space.name, lexical.DERIVATION))
    # OPENING or CLOSING, the walk restarts: rows skipped under the old gate were walked
    # past, and a gate change must reach them. Unchanged vectors cost a digest compare.
    cur.execute(f"UPDATE {store.STATE_TABLE} SET forbidden_rows_open = %s, "
                "lexical_derivation = %s, watermark_seq = NULL, "
                "watermark_at = NULL, watermark_id = NULL, rows_seen = 0, "
                "rows_embedded = 0, rows_skipped_forbidden = 0, rows_skipped_empty = 0, "
                "rows_skipped_no_schema_version = 0, "
                "updated_at = now() WHERE space = %s", (gate, lexical.DERIVATION, space.name))
    return purged


def catch_up(conn, *, space: spaces.Space | None = None, max_batch: int = DEFAULT_BATCH,
             embed_per_tick: int = EMBED_PER_TICK, deadline_s: float | None = 30.0,
             namespace: str = MESH_CHAT_NAMESPACE) -> Report:
    """One bounded tick. Idempotent. `conn`: autocommit, no open transaction (rls.py)."""
    from apps.mesh import projection as mesh_projection
    if namespace != MESH_CHAT_NAMESPACE:
        raise ValueError(f"the commit order is kept for {MESH_CHAT_NAMESPACE!r} only; "
                         f"{namespace!r} has none to walk (sql_meshychat/023)")
    space = space or store.configured_space()
    gate = store.forbidden_rows_open()
    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    # ---- 1. read ------------------------------------------------------------------
    # projection_writer, not server_internal: this transaction also writes (the state
    # row, a gate restart, a purge), and sql_meshychat/008 lets only the job write
    # either table (divergence 022).
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            store.ensure_state(cur, space)
            state = store.read_state(cur, space)
            gate_changed = bool(state["forbidden_rows_open"]) != gate
            derivation_changed = state.get("lexical_derivation") != lexical.DERIVATION
            purged = 0
            if gate_changed or derivation_changed:
                purged = _restart_walk(cur, space, gate, closing=gate_changed and not gate,
                                       derivation_changed=derivation_changed)
                state = store.read_state(cur, space)
            reference = store.reference_of(state, space)
            after_at, after_id = state["watermark_at"], state["watermark_id"]
            position = _position(state)
            cur.execute(_SELECT_ROWS, {"ns": namespace,
                                       "after_seq": int(state.get("watermark_seq") or 0),
                                       "limit": int(max_batch)})
            rows = cur.fetchall()
            shas = store.existing_shas(cur, space, [r["id"] for r in rows])
            lexed_shas = store.existing_lexical(cur, space, [r["id"] for r in rows])

    # ---- 2. decide and embed (no transaction held) ---------------------------------
    # (verdict, row, prompted, sha of prompted, text before the prompt)
    plan: list[tuple[str, dict, str | None, str | None, str | None]] = []
    for r in rows:
        if r["embed_policy"] == "forbidden" and not gate:
            plan.append(("skipped_forbidden", r, None, None, None))
            continue
        if not r["has_text"]:
            # Decided by the SQL predicate coverage also counts by (divergence 023), so
            # this skip and every response's denominators agree row for row.
            plan.append(("skipped_empty" if r["has_schema_version"]
                         else "skipped_no_schema_version", r, None, None, None))
            continue
        # SQL says there is text. Python's strip() treats a few more code points as
        # whitespace than the database's [[:space:]] may; embed the raw prefix then,
        # rather than let the two predicates disagree about whether the row was skipped.
        text = document_text(r["body"]) or r["body"][:MAX_CHARS]
        prompted = space.document(text)
        digest = text_sha256(prompted)
        if shas.get(str(r["id"])) == digest:
            plan.append(("unchanged", r, None, None, text))
            continue
        plan.append(("embed", r, prompted, digest, text))

    to_embed = [i for i, p in enumerate(plan) if p[0] == "embed"]
    attempts: list[dict] = []
    used: list[str] = []
    new_reference: tuple[np.ndarray, str] | None = None
    vectors: dict[int, tuple[np.ndarray, str]] = {}
    stopped: str | None = None

    def _budget() -> float:
        return 20.0 if deadline is None else max(0.0, deadline - time.monotonic())

    if reference is None:
        # Canaries only: recording the reference sends no mesh content anywhere.
        try:
            got = providers.embed_in_space(space, [], None, budget_s=max(0.1, _budget()),
                                           record_if_unreferenced=True)
            new_reference = (got.canary_vectors, got.provider_id)
            reference = got.canary_vectors
            attempts.extend(got.attempts)
        except providers.EmbedUnavailable as exc:
            attempts.extend(exc.attempts)

    first_unembedded = None
    if to_embed and reference is None:
        stopped, first_unembedded = "provider_unavailable", to_embed[0]
    else:
        for start in range(0, len(to_embed), SUB_BATCH):
            chunk = to_embed[start:start + SUB_BATCH]
            if len(vectors) >= embed_per_tick:
                stopped, first_unembedded = "embed_bound", chunk[0]
                break
            chunk = chunk[:max(1, embed_per_tick - len(vectors))]
            if deadline is not None and time.monotonic() >= deadline:
                stopped, first_unembedded = "deadline", chunk[0]
                break
            try:
                got = providers.embed_in_space(space, [plan[i][2] for i in chunk], reference,
                                               budget_s=max(0.1, _budget()))
            except providers.EmbedUnavailable as exc:
                attempts.extend(exc.attempts)
                stopped, first_unembedded = "provider_unavailable", chunk[0]
                break
            attempts.extend(got.attempts)
            if got.provider_id not in used:
                used.append(got.provider_id)
            for i, vec in zip(chunk, got.vectors):
                vectors[i] = (vec, got.provider_id)
            if len(chunk) < len(to_embed[start:start + SUB_BATCH]):
                stopped = "embed_bound"
                first_unembedded = to_embed[start + len(chunk)]
                break

    cut = len(plan) if first_unembedded is None else first_unembedded
    done = plan[:cut]

    # ---- 3. write ------------------------------------------------------------------
    reference_recorded = False
    with rls.projection_writer(conn):             # divergence 022: the job-only writer
        with conn.cursor(row_factory=dict_row) as cur:
            now_state = store.read_state(cur, space, for_update=True)
            if _position(now_state) != position:
                raise ConcurrentWalk(f"space {space.name!r}: the watermark moved during this "
                                     "tick; another walker is running")
            if new_reference is not None:
                stored = store.record_reference(cur, space, new_reference[0],
                                                new_reference[1])
                if not np.array_equal(stored, np.asarray(new_reference[0], dtype="<f4")):
                    # Someone else recorded first. Our vectors were admitted against OUR
                    # canaries, not theirs, so none of them may be written.
                    raise ConcurrentWalk(f"space {space.name!r}: another walker recorded "
                                         "the reference first")
                reference_recorded = True
            write = []
            for i, (verdict, r, _prompted, digest, _text) in enumerate(done):
                if verdict == "embed":
                    vec, pid = vectors[i]
                    write.append((r["id"], r["key"], r["version"], digest, vec, pid,
                                  r["created_at"]))
            # Every row READ with text that passes the gate, NOT only the final prefix:
            # the lexical arm needs no model, so an embedder outage must not stall it
            # (divergence 026). Same gate and same text as the vectors; a row whose
            # lexical digest is already stored is not rewritten.
            lex = []
            for verdict, r, _prompted, _digest, text in plan:
                if verdict not in ("embed", "unchanged"):
                    continue
                sha = text_sha256(text)
                if lexed_shas.get(str(r["id"])) != sha:
                    lex.append((r["id"], r["key"], r["version"], text, sha, r["created_at"]))
            store.upsert_vectors(cur, space, write)
            store.upsert_lexical(cur, space, lex)
            counts = {k: sum(1 for p in done if p[0] == k)
                      for k in ("skipped_forbidden", "skipped_empty",
                                "skipped_no_schema_version")}
            if done:
                last = done[-1][1]
                key = mesh_projection.greatest_key((after_at, after_id), [p[1] for p in done])
                cur.execute(
                    f"UPDATE {store.STATE_TABLE} SET watermark_seq = %s, "
                    "watermark_at = %s, watermark_id = %s, "
                    "rows_seen = rows_seen + %s, rows_embedded = rows_embedded + %s, "
                    "rows_skipped_forbidden = rows_skipped_forbidden + %s, "
                    "rows_skipped_empty = rows_skipped_empty + %s, "
                    "rows_skipped_no_schema_version = rows_skipped_no_schema_version + %s, "
                    "updated_at = now() WHERE space = %s",
                    (last["seq"], key[0], key[1], len(done), len(write),
                     counts["skipped_forbidden"], counts["skipped_empty"],
                     counts["skipped_no_schema_version"], space.name))
                wm_at, wm_id = key[0], str(key[1])
            else:
                wm_at, wm_id = after_at, (str(after_id) if after_id is not None else None)

    return Report(
        space=space.name, rows_read=len(rows), rows_processed=len(done),
        embedded=sum(1 for p in done if p[0] == "embed"),
        unchanged=sum(1 for p in done if p[0] == "unchanged"),
        skipped_forbidden=sum(1 for p in done if p[0] == "skipped_forbidden"),
        skipped_empty=sum(1 for p in done if p[0] == "skipped_empty"),
        skipped_no_schema_version=sum(1 for p in done
                                      if p[0] == "skipped_no_schema_version"),
        gate_open=gate, gate_changed=gate_changed, purged=purged, lexed=len(lex),
        derivation_changed=derivation_changed,
        reference_recorded=reference_recorded, providers_used=tuple(used), stopped=stopped,
        attempts=tuple(attempts), watermark_at=wm_at, watermark_id=wm_id,
        more=(len(rows) >= max_batch or stopped is not None),
    )


class EmbedJobFailed(RuntimeError):
    """Raised by the job body AFTER committing the progress it made, so the runner
    records an error and backs off rather than reporting a starved walk as ok."""


def run_catch_up_job(conn, job) -> TickResult:
    """The registered job body. Returns rows PROCESSED and the report's own `more`: a
    tick that stopped on its embed bound or deadline read fewer than `job.max_batch`
    rows and still has work, which `rows >= max_batch` could not see (STATUS D18). A
    tick that could not embed rows it had to is an ERROR (E1: a failure must not read as
    a pass), raised after its progress is committed."""
    report = catch_up(conn, max_batch=job.max_batch, deadline_s=job.timeout_secs)
    if report.stopped == "provider_unavailable":
        # Every declared embed entry is in `attempts`, one naming another space as a
        # `space_mismatch` with both spaces (E20); so this fallback means what it says.
        # Each distinct attempt once, in first-seen order: every embed_in_space call of the
        # tick names a misdeclared entry again (review LOW-3 of fin/w3-ops). The report
        # keeps the whole record.
        seen = dict.fromkeys(f"{a.get('id')}: {a.get('verdict')} ({a.get('reason', '')})"
                             for a in report.attempts)
        tried = "; ".join(seen) or ("no embed provider declared "
                                    "(MESHY_MODEL_PROVIDERS has no kind=embed entry)")
        raise EmbedJobFailed(f"{report.space}: {report.rows_processed} row(s) processed, "
                             f"then no admitted embed provider. {tried}")
    return TickResult(rows=report.rows_processed, more_work_pending=report.more)


__all__ = ["ConcurrentWalk", "DEFAULT_BATCH", "EMBED_PER_TICK", "EmbedJobFailed",
           "MAX_CHARS", "Report", "SUB_BATCH", "catch_up", "document_text",
           "run_catch_up_job", "text_sha256"]
