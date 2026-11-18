"""The transcript-context index (v1.1 step 6, PR B; docs/design/context-v1.1-transcript-ingest.md): a walk over committed
context MANIFESTS in commit order into mesh_ctx_chunks and mesh_ctx_vectors (sql_meshychat/047).

Registered in apps/jobs/registry.py as `ctx_embed_catch_up`. OFF unless MESHY_CONTEXT_INDEX is explicitly true: merging
this embeds nothing until the operator turns it on, and turning it off stops the walk where it is (nothing is deleted).

ONE TICK:
  0. Refuse to run when ctx_index_config names a namespace other than the server's context namespace
     (apps/context/validation.namespace): the trigger and the walker must agree on what they index.
  1. Sweep orphaned vectors (pxx point 1): a manifest row deleted outside the walker takes its chunk rows with it
     (ON DELETE CASCADE) and leaves their vectors behind; each tick deletes up to ORPHAN_SWEEP of them.
  2. Yield to the mail: when mesh_embed_catch_up is more than one tick behind, this tick does nothing else.
  3. (transaction) read up to MAX_MANIFESTS manifests after the watermark, by ctx_commit_order.seq, and each one's items.
  4. (no transaction) cut each manifest's items into turn-aware chunks (`turn_chunks`) and embed the chunks whose digest
     has no vector yet, on HQB's providers (providers.embed_in_space), outside any transaction.
  5. (one transaction PER MANIFEST) re-read the watermark FOR UPDATE and refuse if it moved (ConcurrentWalk), replace
     this batch's chunk rows (any earlier manifest of the same (src, gen, batch) too: only one manifest per batch can
     commit, frozen_outbox.py:108 plus E_CONTEXT_WRITE_ONCE, so that delete is a guard), write the new vectors, delete
     the vectors this batch's old rows left orphaned, and advance the watermark past exactly this seq.
  A TRANSIENT failure (no admitted provider, the deadline) never advances the watermark: the next tick retries the same
  manifest. A NON-TRANSIENT failure (a manifest or item that cannot be read as context.v1) counts an attempt on the
  stuck seq; at MAX_ATTEMPTS the dead-letter row and the watermark move commit TOGETHER, so the watermark never passes a
  manifest that is neither indexed nor dead-lettered.
  6. Replay: dead-letter rows an operator set to 'replay' are walked outside the watermark and set 'done' (or back to
     'dead' with one more attempt).

TURN-AWARE CHUNKS. A turn is a user_text item and every item after it up to the next user_text, in ordinal order, within
the batch (items before the batch's first user_text are a turn of their own). Its text is each item's `text` with a
header line "[<item kind>]", items joined by a blank line, deterministically, so a chunk's (first_ord, last_ord,
char_start, char_end) inside that turn text collapse it back to its exact items. Omitted items carry no text and are
skipped. The turn text is cut by graphtext.chunk_spans at CHUNK_CHARS with CHUNK_OVERLAP.

THE GATE. Every shared_memory writer stamps embed_policy 'forbidden'; with MESHY_EMBED_FORBIDDEN_ROWS closed no item's
text is embedded (store.forbidden_rows_open, the mail job's gate), and the walk still advances.

The vectors are never read by search here: step 7 adds the route. They are NOT in mesh_embeddings: search's in-process
index loads that table (100,000-vector ceiling) and F3 transcript volume would fill it (kxxx, pxx).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import time

from psycopg.rows import dict_row

from apps.context import validation as ctx_validation
from apps.core import env, rls
from apps.jobs.result import TickResult

from . import graphtext, lexical, providers, spaces, store

#: Manifests read per tick: the declared cost bound (B5).
MAX_MANIFESTS = 32
#: Texts per embed call (plus the space's canaries).
SUB_BATCH = 32
#: Chunks embedded per tick, across manifests.
EMBED_PER_TICK = 256
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200
#: Non-transient failures of one manifest before it is dead-lettered (design step 6.6).
MAX_ATTEMPTS = 5
#: Orphaned vectors deleted per sweep.
ORPHAN_SWEEP = 1000
#: The sweep runs at most this often per space (Codex on #386: not an anti-join over every vector on every tick). A
#: sweep that hits ORPHAN_SWEEP leaves the clock alone, so the backlog is worked off on the following ticks.
SWEEP_EVERY_S = 3600
#: A vector younger than this is never swept: it may be part of a manifest whose embedding spans ticks (the vectors a
#: deadline cut short are kept, Codex on #386), and its chunks are not written yet. The kept ones are ALSO spared past
#: it, while their manifest is unfinished (sql_meshychat/050, Codex on #389): ctx_index_state's kept_seqs and kept_at.
ORPHAN_GRACE_S = 3600
#: The mail embedder is "more than one tick behind" when its next run is this far overdue (its interval is 60 s).
MAIL_BEHIND_S = 120

_SELECT_MANIFESTS = (
    "SELECT o.seq, sm.id, sm.key, sm.value FROM ctx_commit_order o "
    "JOIN shared_memory sm ON sm.id = o.memory_id "
    "WHERE o.seq > %(after)s AND o.kind = 'write' AND sm.namespace = %(ns)s "
    # Dead-lettered in this space (dead or awaiting replay): replay is their only way back in, so a re-walk from the
    # start does not stall MAX_ATTEMPTS ticks on each old poison manifest (a reviewer on #386).
    "AND NOT EXISTS (SELECT 1 FROM ctx_dead_letter d WHERE d.space = %(space)s AND d.seq = o.seq "
    "AND d.state IN ('dead', 'replay')) "
    "ORDER BY o.seq ASC LIMIT %(limit)s"
)
_SELECT_ITEMS = ("SELECT key, value, embed_policy FROM shared_memory "
                 "WHERE namespace = %s AND key = ANY(%s) AND is_head")


def enabled() -> bool:
    """MESHY_CONTEXT_INDEX, explicitly true; anything else (unset, "0", a typo) reads OFF."""
    return env.env_flag("MESHY_CONTEXT_INDEX", "MESHY_CONTEXT_INDEX", default=False)


class ConcurrentWalk(RuntimeError):
    """The watermark moved between this tick's read and a write: another walker ran. Nothing of this manifest is written."""


class ConfigMismatch(RuntimeError):
    """ctx_index_config names another namespace than the server's context namespace."""


class Unreadable(ValueError):
    """A manifest or one of its items cannot be read as context.v1: a NON-transient failure."""


@dataclasses.dataclass(frozen=True)
class Chunk:
    chunk_no: int
    first_ord: int
    last_ord: int
    char_start: int
    char_end: int
    time_first: str
    time_last: str
    text: str


@dataclasses.dataclass(frozen=True)
class Report:
    space: str
    enabled: bool
    gate_open: bool
    manifests_read: int
    manifests_indexed: int
    chunks_written: int
    embedded: int
    reused: int
    dead_lettered: int
    replayed: int
    orphans_swept: int
    stopped: str | None
    more: bool


# ------------------------------------------------------------------ the text rules (no database)

def _item_key(seat, src, gen, batch, ordinal) -> str:
    return f"ctx/{seat}/{src}/{int(gen):04d}/{int(batch):06d}/{int(ordinal):010d}"


def manifest_coordinates(value) -> tuple[str, str, int, int, list[int]]:
    """(seat, src, gen, batch, ordinals) of a stored manifest value, or Unreadable."""
    if not isinstance(value, dict) or value.get("kind") != "manifest":
        raise Unreadable("not a context.v1 manifest")
    try:
        seat, src = str(value["seat"]), str(value["src"])
        gen, batch = int(value["gen"]), int(value["batch"])
        ordinals = [int(entry[0]) for entry in value["items"]]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise Unreadable(f"manifest field missing or malformed ({type(exc).__name__})") from None
    if ordinals != sorted(ordinals) or len(set(ordinals)) != len(ordinals):
        raise Unreadable("manifest ordinals are not strictly increasing")
    return seat, src, gen, batch, ordinals


def _instant(t: str) -> _dt.datetime:
    """An item's ISO-8601 time as an aware instant, for ordering (a trailing Z reads as UTC)."""
    v = _dt.datetime.fromisoformat(t[:-1] + "+00:00" if t.endswith("Z") else t)
    return v if v.tzinfo is not None else v.replace(tzinfo=_dt.timezone.utc)


def in_turn_text(v: dict) -> bool:
    """Whether an item is part of its turn's text. A hit's item_kinds (ctxsearch._texts) reads this same rule."""
    return v.get("item") != "omitted" and isinstance(v.get("text"), str) and bool(v["text"].strip())


def turn_chunks(items: list[dict], *, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    """Turn-aware chunks of one batch's items (dicts with ordinal, item, text, time; in any order). See the module doc."""
    ordered = sorted(items, key=lambda v: int(v["ordinal"]))
    turns: list[list[dict]] = []
    for v in ordered:
        if not in_turn_text(v):
            continue
        if not turns or v.get("item") == "user_text":
            turns.append([])
        turns[-1].append(v)
    out: list[Chunk] = []
    for turn in turns:
        parts, ranges, pos = [], [], 0
        for v in turn:
            block = f"[{v.get('item')}]\n{v['text']}"
            if parts:
                pos += 2                                             # the blank line joining two items
            ranges.append((pos, pos + len(block), int(v["ordinal"]), str(v["time"])))
            parts.append(block)
            pos += len(block)
        text = "\n\n".join(parts)
        for start, end in graphtext.chunk_spans(text, max_chars=max_chars, overlap=overlap):
            inside = [r for r in ranges if r[0] < end and r[1] > start]
            # The chunk spans the EARLIEST and LATEST of its items' times, not the first and last item's: times
            # inside a turn need not increase with the ordinal (pxx, Codex on #389/#390), and the `since`
            # filter and retention read these bounds. Text and provenance stay in ordinal order.
            times = [r[3] for r in inside]
            out.append(Chunk(chunk_no=len(out), first_ord=inside[0][2], last_ord=inside[-1][2],
                             char_start=start, char_end=end, time_first=min(times, key=_instant),
                             time_last=max(times, key=_instant),
                             text=text[start:end]))
    return out


def _gated(items: list[dict], gate: bool) -> list[dict]:
    """The items whose text may be embedded: all of them with the gate open, else none stamped 'forbidden'."""
    return [v for v in items if gate or v.get("_embed_policy") != "forbidden"]


def chunk_digest(space: spaces.Space, text: str) -> tuple[str, str]:
    """(prompted, sha256 of prompted): the digest is of the exact string embedded, as in embedjob."""
    prompted = space.document(text)
    return prompted, hashlib.sha256(prompted.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ the database steps

def _check_config(cur) -> str:
    cur.execute("SELECT namespace FROM ctx_index_config WHERE id = 1")
    row = cur.fetchone()
    want = ctx_validation.namespace()
    got = None if row is None else (row["namespace"] if isinstance(row, dict) else row[0])
    if got != want:
        raise ConfigMismatch(f"ctx_index_config names {got!r}, the server's context namespace is {want!r}: "
                             "the commit trigger and the walker would index different namespaces")
    return want


def _ensure_state(cur, space: spaces.Space) -> dict:
    cur.execute("INSERT INTO ctx_index_state (space) VALUES (%s) ON CONFLICT (space) DO NOTHING", (space.name,))
    cur.execute("SELECT * FROM ctx_index_state WHERE space = %s", (space.name,))
    return cur.fetchone()


def _space_lock(cur, space: spaces.Space) -> None:
    """One transaction-scoped lock per space, taken by the orphan sweep and by EVERY write transaction (main walk and
    replay), so the sweep can never delete a vector between a writer's check and its commit (pxx point 1).
    Key 1835233134, not the commit stamps' 1835233133, so the two families never share a slot."""
    cur.execute("SELECT pg_advisory_xact_lock(1835233134, hashtext(%s))", (space.name,))


def _sweep_orphans(cur, space: spaces.Space) -> int:
    """Vectors (and their lexical terms, sql_meshychat/048) that no chunk occurrence names any more. Returns vectors."""
    _space_lock(cur, space)
    cur.execute("INSERT INTO ctx_index_state (space) VALUES (%s) ON CONFLICT (space) DO NOTHING", (space.name,))
    cur.execute("SELECT swept_at IS NULL OR swept_at <= now() - make_interval(secs => %s) AS due "
                "FROM ctx_index_state WHERE space = %s FOR UPDATE", (SWEEP_EVERY_S, space.name))
    if not cur.fetchone()["due"]:
        return 0
    # Unfinished manifests' kept vectors (sql_meshychat/050): every kept seq still to walk (above the watermark with its
    # commit-order row present, or a dead letter marked for replay) keeps the set alive, and the set spares everything
    # unreferenced since kept_at, whatever its age. Seqs written, dead-lettered or gone drop out here; an empty set clears.
    cur.execute("SELECT s.kept_seqs, s.kept_at, ARRAY(SELECT k FROM unnest(s.kept_seqs) AS k WHERE "
                " (k > COALESCE(s.watermark_seq, 0) AND EXISTS (SELECT 1 FROM ctx_commit_order o WHERE o.seq = k)) "
                " OR EXISTS (SELECT 1 FROM ctx_dead_letter d WHERE d.space = s.space AND d.seq = k AND d.state = 'replay')"
                " ORDER BY k) AS live FROM ctx_index_state s WHERE s.space = %s", (space.name,))
    k = cur.fetchone()
    live = list(k["live"])
    spare = k["kept_at"] if live else None
    if k["kept_at"] is not None and not live:
        cur.execute("UPDATE ctx_index_state SET kept_seqs = NULL, kept_at = NULL WHERE space = %s", (space.name,))
    elif live != sorted(k["kept_seqs"] or []):
        cur.execute("UPDATE ctx_index_state SET kept_seqs = %s WHERE space = %s", (live, space.name))
    cur.execute("DELETE FROM mesh_ctx_vectors v WHERE (v.space, v.text_sha256) IN ("
                " SELECT x.space, x.text_sha256 FROM mesh_ctx_vectors x WHERE x.space = %s AND NOT EXISTS ("
                "  SELECT 1 FROM mesh_ctx_chunks c WHERE c.space = x.space AND c.text_sha256 = x.text_sha256)"
                " AND x.created_at <= now() - make_interval(secs => %s)"
                " AND (%s::timestamptz IS NULL OR x.created_at < %s::timestamptz) LIMIT %s)",
                (space.name, ORPHAN_GRACE_S, spare, spare, ORPHAN_SWEEP))
    swept = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    # Lexical rows are written only with their chunks (never ahead, as kept vectors are), so they need no grace.
    cur.execute("DELETE FROM mesh_ctx_lexical l WHERE (l.space, l.text_sha256) IN ("
                " SELECT x.space, x.text_sha256 FROM mesh_ctx_lexical x WHERE x.space = %s AND NOT EXISTS ("
                "  SELECT 1 FROM mesh_ctx_chunks c WHERE c.space = x.space AND c.text_sha256 = x.text_sha256)"
                " LIMIT %s)", (space.name, ORPHAN_SWEEP))
    lex = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if swept < ORPHAN_SWEEP and lex < ORPHAN_SWEEP:
        cur.execute("UPDATE ctx_index_state SET swept_at = now() WHERE space = %s", (space.name,))
    return swept


def _mail_behind(cur) -> bool:
    cur.execute("SELECT next_run_at < now() - make_interval(secs => %s) AS behind FROM background_job_leases "
                "WHERE job_name = 'mesh_embed_catch_up'", (MAIL_BEHIND_S,))
    row = cur.fetchone()
    return bool(row and (row["behind"] if isinstance(row, dict) else row[0]))


def _item_keys(manifest_value) -> list[str]:
    seat, src, gen, batch, ordinals = manifest_coordinates(manifest_value)
    return [_item_key(seat, src, gen, batch, o) for o in ordinals]


def _validated_item(row: dict) -> dict:
    """Validate one stored context.v1 item row (`value` + `embed_policy`), or raise Unreadable."""
    v = row["value"]
    if not isinstance(v, dict) or v.get("kind") != "item" or not isinstance(v.get("time"), str):
        raise Unreadable("an item is not a context.v1 item")
    # The validator binds these, but a row written around it must dead-letter, not fail the tick (pxx).
    if type(v.get("ordinal")) is not int:
        raise Unreadable("an item's ordinal is not an integer")
    try:
        _instant(v["time"])
    except ValueError as exc:
        raise Unreadable(f"an item's time is not ISO-8601: {exc}") from None
    return {**v, "_embed_policy": row["embed_policy"]}


def _load_items(cur, ns: str, manifest: dict) -> tuple[tuple, list[dict]]:
    """((seat, src, gen, batch), the item values with their embed_policy) or Unreadable."""
    seat, src, gen, batch, _ordinals = manifest_coordinates(manifest["value"])
    keys = _item_keys(manifest["value"])
    cur.execute(_SELECT_ITEMS, (ns, keys))
    found = {r["key"]: r for r in cur.fetchall()}
    missing = [k for k in keys if k not in found]
    if missing:
        raise Unreadable(f"{len(missing)} of the manifest's items are not stored")
    return (seat, src, gen, batch), [_validated_item(found[k]) for k in keys]


class VectorGone(RuntimeError):
    """A digest planned as reusable has no vector at write time (swept before this transaction took the space lock).
    TRANSIENT: nothing of the manifest is written and the watermark stays; the next tick re-plans and re-embeds it."""


def _write_manifest(cur, space: spaces.Space, ns: str, manifest: dict, coords: tuple, project, chunks: list[Chunk],
                    digests: list[str], vectors: dict[str, bytes]) -> tuple[int, int]:
    """One manifest's rows, in the caller's transaction, which already holds the space lock. Returns (chunks written,
    orphans removed). Re-checks, inside this transaction, that every planned item is still stored (pxx point
    2: an item deleted while the chunks were embedded must not be indexed) and that every digest it names has a
    vector (point 1)."""
    # The manifest row itself, FOR SHARE (a reviewer on #386): deleted, it is unreadable; no longer the head of its key,
    # a newer version of the batch exists (walked later, or already), so this older one writes nothing. A replay or a
    # re-walk of an old seq therefore never puts a superseded batch back over the current one, even when the current
    # version produced no chunks.
    cur.execute("SELECT is_head FROM shared_memory WHERE id = %s FOR SHARE", (manifest["id"],))
    row = cur.fetchone()
    if row is None:
        raise Unreadable("the manifest was deleted between planning and writing")
    if not row["is_head"]:
        return 0, 0
    keys = _item_keys(manifest["value"])
    # FOR SHARE, not a count (pxx point 1): the planned items stay locked until this transaction ends, so a
    # delete that starts after the check waits for the commit instead of slipping between the check and the write.
    cur.execute("SELECT key FROM shared_memory WHERE namespace = %s AND key = ANY(%s) AND is_head FOR SHARE",
                (ns, keys))
    if len(cur.fetchall()) != len(keys):
        raise Unreadable("the manifest's items changed between planning and writing")
    seat, src, gen, batch = coords
    # Bound to the seat too (Codex on #387): two seats with the same src, gen and batch never replace each other's.
    cur.execute("SELECT DISTINCT text_sha256 FROM mesh_ctx_chunks WHERE space = %s AND namespace = %s AND seat = %s "
                "AND src = %s AND gen = %s AND batch = %s", (space.name, ns, seat, src, gen, batch))
    old = {r["text_sha256"] for r in cur.fetchall()}
    cur.execute("DELETE FROM mesh_ctx_chunks WHERE space = %s AND namespace = %s AND seat = %s AND src = %s AND gen = %s "
                "AND batch = %s", (space.name, ns, seat, src, gen, batch))
    for sha, raw in vectors.items():
        cur.execute("INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (space, text_sha256) DO NOTHING", (space.name, sha, space.dims, raw))
    wanted = sorted(set(digests))
    if wanted:
        cur.execute("SELECT text_sha256 FROM mesh_ctx_vectors WHERE space = %s AND text_sha256 = ANY(%s)",
                    (space.name, wanted))
        present = {r["text_sha256"] for r in cur.fetchall()}
        if present != set(wanted):
            raise VectorGone(f"{len(set(wanted) - present)} planned vector(s) were swept before the write")
    # The lexical arm's terms, one row per digest, of the chunk's text (not the prompt), as the vectors are shared.
    seen_lex: set[str] = set()
    for ch, sha in zip(chunks, digests):
        if sha in seen_lex:
            continue
        seen_lex.add(sha)
        lexemes, tfs = lexical.terms(ch.text)
        cur.execute("INSERT INTO mesh_ctx_lexical (space, text_sha256, derivation, lexemes, tfs) VALUES (%s, %s, %s, "
                    "%s, %s) ON CONFLICT (space, text_sha256) DO UPDATE SET derivation = EXCLUDED.derivation, "
                    "lexemes = EXCLUDED.lexemes, tfs = EXCLUDED.tfs WHERE mesh_ctx_lexical.derivation <> "
                    "EXCLUDED.derivation",
                    (space.name, sha, lexical.DERIVATION, lexemes, tfs))
    for ch, sha in zip(chunks, digests):
        cur.execute("INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, project, "
                    "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256) VALUES (%s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (manifest["id"], space.name, ns, ch.chunk_no, seat, src, gen, batch, project, ch.first_ord,
                     ch.last_ord, ch.char_start, ch.char_end, ch.time_first, ch.time_last, sha))
    # The newest INDEXED gen of this source (pxx point 2): recorded for every manifest, chunks or not, so a
    # newest gen made only of omissions retires the older gen from retrieval.
    cur.execute("INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (space, namespace, seat, src) DO UPDATE SET "
                "gen = GREATEST(ctx_source_gen.gen, EXCLUDED.gen), updated_at = now() RETURNING gen",
                (space.name, ns, seat, src, gen))
    newest = int(cur.fetchone()["gen"])
    # Only the newest indexed gen's rows stay `current` (sql_meshychat/051, pxx): keyed on the gen the upsert
    # LEAVES, so a manifest of an older gen written after a newer one was recorded has its own rows (just inserted,
    # DEFAULT true) flipped here too. Same transaction as the upsert and the chunk writes, under the space lock.
    cur.execute("UPDATE mesh_ctx_chunks SET current = false WHERE space = %s AND namespace = %s AND seat = %s "
                "AND src = %s AND gen < %s AND current", (space.name, ns, seat, src, newest))
    removed = 0
    gone = sorted(old - set(digests))
    if gone:
        cur.execute("DELETE FROM mesh_ctx_vectors v WHERE v.space = %s AND v.text_sha256 = ANY(%s) AND NOT EXISTS ("
                    " SELECT 1 FROM mesh_ctx_chunks c WHERE c.space = v.space AND c.text_sha256 = v.text_sha256)",
                    (space.name, gone))
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        cur.execute("DELETE FROM mesh_ctx_lexical l WHERE l.space = %s AND l.text_sha256 = ANY(%s) AND NOT EXISTS ("
                    " SELECT 1 FROM mesh_ctx_chunks c WHERE c.space = l.space AND c.text_sha256 = l.text_sha256)",
                    (space.name, gone))
    return len(chunks), removed


def _dead_letter(cur, space: spaces.Space, manifest: dict, reason: str, attempts: int) -> None:
    cur.execute("INSERT INTO ctx_dead_letter (seq, memory_id, space, reason, attempts) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (space, seq) DO UPDATE SET reason = EXCLUDED.reason, attempts = EXCLUDED.attempts, "
                "state = 'dead', at = now()", (manifest["seq"], manifest["id"], space.name, reason[:500], attempts))


def _reference(conn, space: spaces.Space, budget_s: float):
    """The space's reference canaries, recorded (write-once, store.record_reference) when none exists yet. Returns the
    STORED reference, so vectors embedded after this are admitted against what the space actually holds, whoever
    recorded it first. None when no provider is admitted. Used by the main walk and by replay alike (pxx point
    4: a replay with no main work must still be able to run)."""
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            store.ensure_state(cur, space)
            ref = store.reference_of(store.read_state(cur, space), space)
    if ref is not None:
        return ref
    try:
        got = providers.embed_in_space(space, [], None, budget_s=budget_s, record_if_unreferenced=True)
    except providers.EmbedUnavailable:
        return None
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            return store.record_reference(cur, space, got.canary_vectors, got.provider_id)


#: The clock the embed loop checks its deadline against (a seam for tests; production is time.monotonic).
_now = time.monotonic


class _Deadline(RuntimeError):
    """The tick's deadline came between sub-batches. `vectors` holds those already embedded, which the caller keeps
    (`_keep_vectors`) so the next tick resumes instead of starting the manifest over (Codex on #386)."""

    def __init__(self, vectors: dict[str, bytes] | None = None):
        super().__init__("the tick's deadline came before the manifest was embedded")
        self.vectors = dict(vectors or {})


def _keep_vectors(conn, space: spaces.Space, vectors: dict[str, bytes], seq: int) -> None:
    """Store vectors a deadline cut short, ahead of their chunks, and mark manifest `seq` as holding kept work, so the
    sweep spares them until it is finished (sql_meshychat/050). Content-addressed and idempotent."""
    if not vectors:
        return
    with rls.projection_writer(conn):
        with conn.cursor() as cur:
            _space_lock(cur, space)
            for sha, raw in vectors.items():
                cur.execute("INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT (space, text_sha256) DO NOTHING", (space.name, sha, space.dims, raw))
            # now() is this transaction's start, so every vector inserted above has created_at >= kept_at. `seq` joins
            # the set of unfinished owners; it never replaces another (codex-pxx-cli round 2, P2-1).
            cur.execute("UPDATE ctx_index_state SET kept_seqs = CASE WHEN %s = ANY(COALESCE(kept_seqs, '{}')) "
                        "THEN kept_seqs ELSE array_append(COALESCE(kept_seqs, '{}'), %s) END, "
                        "kept_at = COALESCE(kept_at, now()) WHERE space = %s", (seq, seq, space.name))


def _embed(space: spaces.Space, todo: list[tuple[str, str]], reference, deadline: float | None) -> dict[str, bytes]:
    """Vectors for (prompt, digest) pairs, SUB_BATCH at a time, checking the deadline before EVERY call (pxx
    point 5). EmbedUnavailable and _Deadline propagate; nothing is written by this function.

    A call that does not fit what is left of the tick fails inside the provider as EmbedUnavailable (its timeout is the
    remaining budget). When sub-batches finished before it, that is a deadline with progress, not an outage: it is
    raised as _Deadline carrying them, so the caller keeps them (pxx, Codex on #387/#390). With nothing
    finished it stays EmbedUnavailable, which the job reports as an error every tick."""
    out: dict[str, bytes] = {}
    for start in range(0, len(todo), SUB_BATCH):
        if deadline is not None and _now() >= deadline:
            raise _Deadline(out)
        part = todo[start:start + SUB_BATCH]
        budget = 20.0 if deadline is None else max(0.1, deadline - _now())
        try:
            got = providers.embed_in_space(space, [p for p, _d in part], reference, budget_s=budget)
        except providers.EmbedUnavailable as exc:
            if out:
                raise _Deadline(out) from exc
            raise
        for (_p, d), vec in zip(part, got.vectors):
            out[d] = spaces.encode(vec)
    return out


def _known(cur, space: spaces.Space, digests: list[str]) -> set[str]:
    """Which of `digests` already have a vector in the space: one indexed lookup of exactly those digests."""
    if not digests:
        return set()
    cur.execute("SELECT text_sha256 FROM mesh_ctx_vectors WHERE space = %s AND text_sha256 = ANY(%s)",
                (space.name, digests))
    return {r["text_sha256"] for r in cur.fetchall()}


def _plan(space: spaces.Space, items: list[dict], gate: bool, known: set) -> tuple[list[Chunk], list[str], list]:
    chunks = turn_chunks(_gated(items, gate))
    prompted = [chunk_digest(space, c.text) for c in chunks]
    digests = [d for _p, d in prompted]
    prompt_of: dict[str, str] = {}
    for p, d in prompted:
        prompt_of.setdefault(d, p)
    return chunks, digests, [(p, d) for d, p in prompt_of.items() if d not in known]


# ------------------------------------------------------------------ the tick

#: space -> monotonic time the index first yielded to the mail in an unbroken run of yields.
_yield_since: dict[str, float] = {}
#: Ticks in a row that stopped on the deadline having indexed nothing AND kept no vector (pxx): the first
#: sub-batch alone does not fit the tick, so the walk cannot move. After DEADLINE_STUCK_TICKS the job fails loudly
#: instead of reporting rows 0 as ok. Ticks that keep vectors are progress and reset the count. The count lives in this
#: process only (like _yield_since): a restart starts it at zero, so a stuck walk fails again DEADLINE_STUCK_TICKS
#: ticks after a restart, not at once (pxx).
_deadline_stuck: dict[str, int] = {}
DEADLINE_STUCK_TICKS = 3
#: After this long of unbroken yielding the walk runs anyway, and the job reports an error so the starvation is seen
#: (pxx point 6). The mail job's own lease shows why it is overdue.
YIELD_MAX_S = 1800


def catch_up(conn, *, space: spaces.Space | None = None, max_manifests: int = MAX_MANIFESTS,
             embed_per_tick: int = EMBED_PER_TICK, deadline_s: float | None = 30.0) -> Report:
    """One bounded tick. Idempotent. `conn`: autocommit, no open transaction (rls.py).

    THE EMBED BOUND: the first manifest of a tick is embedded whole however many chunks it has (graphwalk's rule): a
    manifest larger than the bound would otherwise never be indexed. Every later manifest stops the tick when it would
    pass `embed_per_tick`. The deadline is checked before every embed call."""
    space = space or store.configured_space()
    gate = store.forbidden_rows_open()
    if not enabled():
        return Report(space.name, False, gate, 0, 0, 0, 0, 0, 0, 0, 0, "disabled", False)
    deadline = None if deadline_s is None else _now() + deadline_s
    overridden = False

    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            ns = _check_config(cur)
            swept = _sweep_orphans(cur, space)
            if _mail_behind(cur):
                since = _yield_since.setdefault(space.name, time.monotonic())
                if time.monotonic() - since < YIELD_MAX_S:
                    return Report(space.name, True, gate, 0, 0, 0, 0, 0, 0, 0, swept, "yield_to_mail", True)
                overridden = True
            else:
                _yield_since.pop(space.name, None)
            state = _ensure_state(cur, space)
            if bool(state["gate_open"]) != gate:
                # The embed_policy gate changed. OPENING it restarts the walk: manifests walked past while it was closed
                # were indexed with no text, and must be walked again (Codex on #390; a re-walk is idempotent, and
                # unchanged digests make no embed call). CLOSING it walks on; search refuses to serve while closed.
                # Either way the epoch moves (a reviewer on #386): a walker that started before the change cannot
                # commit after it, even where the reset puts the watermark back at that walker's own position.
                if gate:
                    # The lexical rows walked past while closed are missing too: the arm is partial until the
                    # re-walk gets back to where the walk had been (a reviewer on #390).
                    cur.execute("UPDATE ctx_index_state SET lexical_rewalk_until = GREATEST(watermark_seq, "
                                "lexical_rewalk_until), watermark_seq = NULL, stuck_seq = NULL, stuck_attempts = 0, "
                                "gate_open = TRUE, walk_epoch = walk_epoch + 1, updated_at = now() WHERE space = %s",
                                (space.name,))
                else:
                    cur.execute("UPDATE ctx_index_state SET gate_open = FALSE, walk_epoch = walk_epoch + 1, "
                                "updated_at = now() WHERE space = %s", (space.name,))
                state = _ensure_state(cur, space)
            if state["lexical_derivation"] != lexical.DERIVATION:
                # The lexical rows were built before 048 or with another tokenizer (pxx point 1): drop the
                # space's rows of any other derivation and restart the walk, recording where it had reached so search
                # can say the lexical arm is partial until it gets there again.
                cur.execute("DELETE FROM mesh_ctx_lexical WHERE space = %s AND derivation <> %s",
                            (space.name, lexical.DERIVATION))
                cur.execute("UPDATE ctx_index_state SET lexical_rewalk_until = GREATEST(watermark_seq, "
                            "lexical_rewalk_until), watermark_seq = NULL, stuck_seq = NULL, stuck_attempts = 0, "
                            "lexical_derivation = %s, walk_epoch = walk_epoch + 1, updated_at = now() WHERE space = %s",
                            (lexical.DERIVATION, space.name))
                state = _ensure_state(cur, space)
            after = int(state["watermark_seq"] or 0)
            epoch = int(state["walk_epoch"])
            cur.execute(_SELECT_MANIFESTS, {"after": after, "ns": ns, "space": space.name, "limit": int(max_manifests)})
            manifests = cur.fetchall()
            plans = []
            for m in manifests:
                try:
                    coords, items = _load_items(cur, ns, m)
                    plans.append((m, coords, items, None))
                except Unreadable as exc:
                    plans.append((m, None, None, str(exc)))
            # Only the digests this tick will write, not every vector in the space (Codex on #386/#387/#389).
            wanted = sorted({chunk_digest(space, c.text)[1] for _m, _c, items, bad in plans if bad is None
                             for c in turn_chunks(_gated(items, gate))})
            known = _known(cur, space, wanted)

    reference = None
    if any(p[3] is None for p in plans):
        reference = _reference(conn, space, 20.0 if deadline is None else max(0.1, deadline - _now()))

    indexed = written = embedded = reused = dead = kept = 0
    stopped: str | None = None
    position = after
    for m, coords, items, unreadable in plans:
        if unreadable is None:
            chunks, digests, todo = _plan(space, items, gate, known)
            vectors: dict[str, bytes] = {}
            if todo:
                if reference is None:
                    stopped = "provider_unavailable"
                    break
                if indexed > 0 and embedded + len(todo) > embed_per_tick:
                    stopped = "embed_bound"
                    break
                try:
                    vectors = _embed(space, todo, reference, deadline)
                except providers.EmbedUnavailable:
                    stopped = "provider_unavailable"
                    break
                except _Deadline as exc:
                    _keep_vectors(conn, space, exc.vectors, m["seq"])
                    kept += len(exc.vectors)
                    stopped = "deadline"
                    break
            try:
                with rls.projection_writer(conn):
                    with conn.cursor(row_factory=dict_row) as cur:
                        _space_lock(cur, space)
                        cur.execute("SELECT watermark_seq, walk_epoch FROM ctx_index_state WHERE space = %s "
                                    "FOR UPDATE", (space.name,))
                        st = cur.fetchone()
                        if int(st["watermark_seq"] or 0) != position or int(st["walk_epoch"]) != epoch:
                            raise ConcurrentWalk(f"space {space.name!r}: the ctx watermark moved during this tick")
                        n, _removed = _write_manifest(cur, space, ns, m, coords, m["value"].get("project"), chunks,
                                                      digests, vectors)
                        cur.execute("UPDATE ctx_index_state SET watermark_seq = %s, stuck_seq = NULL, "
                                    "stuck_attempts = 0, gate_open = %s, updated_at = now() WHERE space = %s",
                                    (m["seq"], gate, space.name))
            except VectorGone:
                stopped = "vector_swept"
                break
            except Unreadable as exc:
                unreadable = str(exc)
            else:
                position = m["seq"]
                known |= set(vectors)
                indexed += 1
                written += n
                embedded += len(vectors)
                reused += len(set(digests)) - len(vectors)
                continue
        with rls.projection_writer(conn):
            with conn.cursor(row_factory=dict_row) as cur:
                position = _commit_failure(cur, space, m, position, unreadable, epoch)
        if position != m["seq"]:
            stopped = "unreadable_retry"
            break
        dead += 1

    replayed, replay_stopped, replay_kept = _replay(conn, space, gate, deadline, epoch)
    kept += replay_kept
    if stopped is None and replay_stopped is not None:
        # Only replay had work, and it stopped: say why, so a provider outage on a replay-only tick is an error
        # (run_catch_up_job) and a replay that cannot fit the tick feeds DEADLINE_STUCK_TICKS (pxx).
        stopped = replay_stopped
    report = Report(space.name, True, gate, len(manifests), indexed, written, embedded, reused, dead, replayed, swept,
                    stopped, len(manifests) >= max_manifests or stopped is not None)
    if stopped == "deadline" and indexed == 0 and replayed == 0 and kept == 0:
        _deadline_stuck[space.name] = _deadline_stuck.get(space.name, 0) + 1
    else:
        _deadline_stuck.pop(space.name, None)
    if _deadline_stuck.get(space.name, 0) >= DEADLINE_STUCK_TICKS:
        raise CtxJobFailed(f"{space.name}: {_deadline_stuck[space.name]} ticks in a row hit the deadline before one "
                           f"sub-batch of {SUB_BATCH} chunks was embedded; the walk cannot move (seq after "
                           f"{position})")
    if overridden:
        raise CtxJobFailed(f"{space.name}: mesh_embed_catch_up has been overdue for over {YIELD_MAX_S} s, so the index "
                           f"walked anyway ({indexed} manifest(s)); its progress is committed")
    return report


def _commit_failure(cur, space: spaces.Space, manifest: dict, position: int, reason: str, epoch: int) -> int:
    """A non-transient failure of the manifest after `position`. Returns the new position: its seq when this attempt
    dead-letters it (the dead-letter row and the watermark move in THIS transaction), else `position` unchanged."""
    cur.execute("SELECT watermark_seq, stuck_seq, stuck_attempts, walk_epoch FROM ctx_index_state WHERE space = %s "
                "FOR UPDATE", (space.name,))
    st = cur.fetchone()
    if int(st["watermark_seq"] or 0) != position or int(st["walk_epoch"]) != epoch:
        raise ConcurrentWalk(f"space {space.name!r}: the ctx watermark moved during this tick")
    attempts = (int(st["stuck_attempts"]) if st["stuck_seq"] == manifest["seq"] else 0) + 1
    if attempts >= MAX_ATTEMPTS:
        _dead_letter(cur, space, manifest, reason, attempts)
        cur.execute("UPDATE ctx_index_state SET watermark_seq = %s, stuck_seq = NULL, stuck_attempts = 0, "
                    "updated_at = now() WHERE space = %s", (manifest["seq"], space.name))
        return manifest["seq"]
    cur.execute("UPDATE ctx_index_state SET stuck_seq = %s, stuck_attempts = %s, updated_at = now() WHERE space = %s",
                (manifest["seq"], attempts, space.name))
    return position


def _epoch_fence(cur, space: spaces.Space, epoch: int) -> None:
    """Under the state row's lock: refuse a durable write planned under another walk epoch (pxx)."""
    cur.execute("SELECT walk_epoch FROM ctx_index_state WHERE space = %s FOR UPDATE", (space.name,))
    st = cur.fetchone()
    if st is None or int(st["walk_epoch"]) != epoch:
        raise ConcurrentWalk(f"space {space.name!r}: the walk was reset while a replay was planned")


def _replay(conn, space: spaces.Space, gate: bool, deadline: float | None, epoch: int) -> tuple[int, str | None, int]:
    """Walk dead-letter rows marked 'replay' outside the watermark. Returns (rows set 'done', why it stopped early or
    None, vectors kept by a deadline). The reasons are the main walk's: provider_unavailable, deadline, vector_swept.

    Each row is CLAIMED in its write transaction (FOR UPDATE, still 'replay'): a second walker that planned the same
    row finds it 'done' and writes nothing (pxx point 3). The reference comes from `_reference`, so replay runs
    when there is no main work (point 4); embedding is sub-batched under the tick's deadline (point 5)."""
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            ns = ctx_validation.namespace()
            cur.execute("SELECT d.seq, sm.id, sm.value FROM ctx_dead_letter d JOIN shared_memory sm "
                        "ON sm.id = d.memory_id WHERE d.space = %s AND d.state = 'replay' AND sm.namespace = %s "
                        "ORDER BY d.seq LIMIT 8", (space.name, ns))
            rows = cur.fetchall()
    if not rows:
        return 0, None, 0
    reference = None
    done = kept = 0
    stopped: str | None = None
    for r in rows:
        m = {"seq": r["seq"], "id": r["id"], "value": r["value"]}
        try:
            with rls.projection_writer(conn):
                with conn.cursor(row_factory=dict_row) as cur:
                    coords, items = _load_items(cur, ns, m)
                    known = _known(cur, space, sorted({chunk_digest(space, c.text)[1]
                                                       for c in turn_chunks(_gated(items, gate))}))
            chunks, digests, todo = _plan(space, items, gate, known)
            vectors: dict[str, bytes] = {}
            if todo:
                if reference is None:
                    reference = _reference(conn, space, 20.0 if deadline is None
                                           else max(0.1, deadline - _now()))
                    if reference is None:
                        stopped = "provider_unavailable"
                        break
                vectors = _embed(space, todo, reference, deadline)
            with rls.projection_writer(conn):
                with conn.cursor(row_factory=dict_row) as cur:
                    _space_lock(cur, space)
                    _epoch_fence(cur, space, epoch)               # planned under the tick's gate and epoch
                    cur.execute("SELECT state FROM ctx_dead_letter WHERE space = %s AND seq = %s FOR UPDATE",
                                (space.name, r["seq"]))
                    claimed = cur.fetchone()
                    if claimed is None or claimed["state"] != "replay":
                        continue                                  # another walker replayed it first
                    _write_manifest(cur, space, ns, m, coords, r["value"].get("project"), chunks, digests, vectors)
                    cur.execute("UPDATE ctx_dead_letter SET state = 'done', at = now() WHERE space = %s AND seq = %s",
                                (space.name, r["seq"]))
            done += 1
        except _Deadline as exc:
            _keep_vectors(conn, space, exc.vectors, r["seq"])
            kept += len(exc.vectors)
            stopped = "deadline"
            break
        except providers.EmbedUnavailable:
            stopped = "provider_unavailable"
            break
        except VectorGone:
            stopped = "vector_swept"
            break
        except Unreadable as exc:
            with rls.projection_writer(conn):
                with conn.cursor(row_factory=dict_row) as cur:
                    _epoch_fence(cur, space, epoch)
                    cur.execute("UPDATE ctx_dead_letter SET state = 'dead', attempts = attempts + 1, reason = %s, "
                                "at = now() WHERE space = %s AND seq = %s AND state = 'replay'",
                                (str(exc)[:500], space.name, r["seq"]))
    return done, stopped, kept


class CtxJobFailed(RuntimeError):
    """Raised AFTER the tick's progress is committed, so the runner records an error rather than a starved walk as ok."""


def run_catch_up_job(conn, job) -> TickResult:
    report = catch_up(conn, max_manifests=job.max_batch, deadline_s=job.timeout_secs)
    if report.stopped == "provider_unavailable":
        raise CtxJobFailed(f"{report.space}: {report.manifests_indexed} manifest(s) indexed, then no admitted embed "
                           "provider")
    return TickResult(rows=report.manifests_indexed, more_work_pending=report.more)


__all__ = ["CHUNK_CHARS", "CHUNK_OVERLAP", "Chunk", "ConcurrentWalk", "ConfigMismatch", "CtxJobFailed", "MAX_ATTEMPTS",
           "MAX_MANIFESTS", "Report", "Unreadable", "VectorGone", "YIELD_MAX_S", "catch_up", "chunk_digest", "enabled",
           "manifest_coordinates", "run_catch_up_job", "turn_chunks"]
