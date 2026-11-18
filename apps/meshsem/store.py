"""Reads and writes of mesh_embeddings and its per-space state row (sql_meshychat/006).

EVERY FUNCTION TAKES A CURSOR OR CONNECTION ALREADY INSIDE `rls.server_internal`, the
same convention as apps/mesh/graph.py: both tables are FORCE-RLS under that policy, so a
caller that forgot the context reads nothing and writes nothing (the RLS tripwire in
tests/test_meshsem_embedjob.py proves both, with the positive control beside it).

WRITES are made only by apps/meshsem/embedjob.py. No view imports a writer from here;
tests/test_meshsem_routes.py asserts the request path leaves every table unchanged, and
sql_meshychat/008 and 010 make the database refuse a request's write (divergences 022,
026). mesh_lexical, the lexical arm's terms (divergence 026), follows the same rules.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt

import numpy as np
from psycopg.rows import dict_row

from apps.core import env
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from . import lexical, spaces

STATE_TABLE = "mesh_embeddings_projection"


def has_text_sql(alias: str) -> str:
    """THE predicate for "this row has text to embed", as SQL over a shared_memory alias.

    The job DECIDES by it (embedjob._SELECT_ROWS) and coverage COUNTS by it, so the two
    cannot disagree about which rows were skipped (divergence 023). A Python strip() and
    a SQL btrim disagree about exotic whitespace; one predicate in one language cannot.

    COALESCED TO FALSE, and that is load-bearing: for a row with no `body` at all,
    jsonb_typeof(NULL) is NULL, `NOT NULL` is NULL, and a FILTER (WHERE NULL) drops the
    row, so the first draft counted exactly the rows this exists to count as `pending`.
    """
    return (f"coalesce(jsonb_typeof({alias}.value->'body') = 'string' "
            f"AND {alias}.value->>'body' ~ '[^[:space:]]', false)")


def has_schema_version_sql(alias: str) -> str:
    """Whether the row declares schema_version at all. The v1 validator (divergence 011)
    refuses a message without it, so a row lacking it is legacy: written before the
    validator existed, never checked by it."""
    return (f"coalesce(jsonb_typeof({alias}.value) = 'object' "
            f"AND {alias}.value ? 'schema_version', false)")


def forbidden_rows_open() -> bool:
    """THE embed_policy GATE (divergence 019). Default CLOSED.

    Every shared_memory writer hard-codes embed_policy = 'forbidden'
    (apps/memoryskills/handlers.py:792-796, the daemon's memory_handlers.py:1634-1640),
    because that column is v0.2's MUST-8 "no embedding" marker (sql/028:35-37,
    106-108). So with this gate closed the job embeds nothing that exists today, and
    that is the intended default: the operator's deployment config opens it, explicitly,
    with MESHY_EMBED_FORBIDDEN_ROWS=true (setting or environment). Anything other than
    an explicit true - unset, "0", a typo - reads CLOSED (apps/core/env.env_flag).

    The gate is enforced TWICE: the job does not embed a forbidden row while it is
    closed, and every read filters forbidden rows out while it is closed, so closing it
    hides existing vectors at once rather than after the next job tick.
    """
    return env.env_flag("MESHY_EMBED_FORBIDDEN_ROWS", "MESHY_EMBED_FORBIDDEN_ROWS",
                        default=False)


def configured_space() -> spaces.Space:
    """The space this deployment embeds into and searches. KeyError on an unknown name."""
    name = env.setting("MESHY_SEMANTIC_SPACE", "MESHY_SEMANTIC_SPACE",
                       default=spaces.DEFAULT_SPACE)
    return spaces.get(str(name))


# ------------------------------------------------------------------ state row

def ensure_state(cur, space: spaces.Space) -> None:
    cur.execute(f"INSERT INTO {STATE_TABLE} (space) VALUES (%s) "
                "ON CONFLICT (space) DO NOTHING", (space.name,))


def read_state(cur, space: spaces.Space, *, for_update: bool = False) -> dict | None:
    """The state row as a dict, or None when the job has never touched this space."""
    lock = " FOR UPDATE" if for_update else ""
    with cur.connection.cursor(row_factory=dict_row) as c:
        c.execute(f"SELECT * FROM {STATE_TABLE} WHERE space = %s{lock}", (space.name,))
        row = c.fetchone()
    return dict(row) if row else None


class ReferenceMismatch(RuntimeError):
    """The stored reference answers a different canary set or width than the code's
    space. Refused: comparing canaries of different sentences proves nothing."""


def reference_of(state: dict | None, space: spaces.Space) -> np.ndarray | None:
    """The recorded reference canaries, None when unrecorded, raising on a mismatch."""
    if not state or state.get("reference_vecs") is None:
        return None
    if state["canary_sha256"] != space.canary_sha256() or state["dims"] != space.dims:
        raise ReferenceMismatch(
            f"space {space.name!r}: the stored reference answers canary set "
            f"{str(state['canary_sha256'])[:12]} at {state['dims']}-d, the code declares "
            f"{space.canary_sha256()[:12]} at {space.dims}-d. A changed canary set is a "
            "NEW space; name it so rather than editing this one.")
    ref = spaces.decode_many(state["reference_vecs"], space.dims)
    if ref.shape[0] != len(space.canaries):
        raise ReferenceMismatch(f"space {space.name!r}: stored reference has "
                                f"{ref.shape[0]} canaries, the space declares "
                                f"{len(space.canaries)}")
    return ref


def record_reference(cur, space: spaces.Space, canary_vecs: np.ndarray,
                     provider_id: str) -> np.ndarray:
    """Record the first admitted provider's canaries as the space's reference, IF none is
    recorded yet, and return whatever the reference now is. A concurrent recorder that
    won the race is not overwritten (the WHERE below, and the write-once trigger in
    sql_meshychat/006 below that); the caller compares what came back."""
    ensure_state(cur, space)
    cur.execute(
        f"UPDATE {STATE_TABLE} SET dims = %s, canary_sha256 = %s, reference_vecs = %s, "
        "reference_provider = %s, reference_recorded_at = now(), updated_at = now() "
        "WHERE space = %s AND reference_vecs IS NULL",
        (space.dims, space.canary_sha256(), spaces.encode_many(canary_vecs), provider_id,
         space.name))
    ref = reference_of(read_state(cur, space), space)
    if ref is None:            # pragma: no cover - the UPDATE above just wrote it
        raise RuntimeError("reference did not persist")
    return ref


# ---------------------------------------------------------------- vectors

_UPSERT = (
    "INSERT INTO mesh_embeddings (src_id, src_key, src_version, space, dims, text_sha256,"
    " vec, provider_id, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (src_id, space, chunk_no) DO UPDATE SET text_sha256 = EXCLUDED.text_sha256, "
    " vec = EXCLUDED.vec, provider_id = EXCLUDED.provider_id, dims = EXCLUDED.dims, "
    " embedded_at = now() "
    "WHERE mesh_embeddings.text_sha256 IS DISTINCT FROM EXCLUDED.text_sha256"
)


def upsert_vectors(cur, space: spaces.Space, rows: list[tuple]) -> int:
    """rows: (src_id, src_key, src_version, text_sha256, vec, provider_id, recorded_at).

    Idempotent. An unchanged text_sha256 is a no-op, a changed one replaces the vector
    and moves embedded_at so the in-process index picks the new vector up.
    """
    if not rows:
        return 0
    params = [(r[0], r[1], r[2], space.name, space.dims, r[3], spaces.encode(r[4]), r[5],
               r[6]) for r in rows]
    cur.executemany(_UPSERT, params)
    return len(params)


_UPSERT_LEXICAL = (
    "INSERT INTO mesh_lexical (src_id, src_key, src_version, space, derivation, text_sha256,"
    " lexemes, tfs, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (src_id, space) DO UPDATE SET text_sha256 = EXCLUDED.text_sha256, "
    " lexemes = EXCLUDED.lexemes, tfs = EXCLUDED.tfs, derivation = EXCLUDED.derivation, "
    " indexed_at = now() "
    "WHERE mesh_lexical.text_sha256 IS DISTINCT FROM EXCLUDED.text_sha256 "
    "   OR mesh_lexical.derivation IS DISTINCT FROM EXCLUDED.derivation"
)


def upsert_lexical(cur, space: spaces.Space, rows: list[tuple]) -> int:
    """rows: (src_id, src_key, src_version, text, text_sha256, recorded_at).

    `text` is tokenized by apps/meshsem/lexical.terms, the same function a request runs
    on a query. Idempotent: an unchanged digest and derivation is a no-op, so indexed_at
    moves only for a real change and the in-process index re-reads only those rows.
    """
    if not rows:
        return 0
    params = []
    for r in rows:
        lexemes, tfs = lexical.terms(r[3])
        params.append((r[0], r[1], r[2], space.name, lexical.DERIVATION, r[4], lexemes,
                       tfs, r[5]))
    cur.executemany(_UPSERT_LEXICAL, params)
    return len(params)


def existing_shas(cur, space: spaces.Space, src_ids: list) -> dict[str, str]:
    """src_id -> stored text_sha256, for the rows the job is about to consider."""
    if not src_ids:
        return {}
    # Its OWN cursor, rows as tuples. The job hands in a dict_row cursor, and the first
    # version indexed it positionally: KeyError 0, but only on a tick that met an
    # already-embedded row, so every first-pass test was green. apps/mesh/projection.py
    # records the same defect in its watermark read; the shape recurs.
    with cur.connection.cursor() as c:
        c.execute("SELECT src_id, text_sha256 FROM mesh_embeddings "
                  "WHERE space = %s AND chunk_no = 0 AND src_id = ANY(%s::uuid[])",
                  (space.name, [str(s) for s in src_ids]))
        return {str(sid): sha for sid, sha in c.fetchall()}


def existing_lexical(cur, space: spaces.Space, src_ids: list) -> dict[str, str]:
    """src_id -> stored text_sha256 of the lexical rows under the CURRENT derivation, for
    the rows the job is about to consider. A row of another derivation is absent here, so
    the job rewrites it."""
    if not src_ids:
        return {}
    with cur.connection.cursor() as c:        # tuples, for the reason existing_shas gives
        c.execute("SELECT src_id, text_sha256 FROM mesh_lexical "
                  "WHERE space = %s AND derivation = %s AND src_id = ANY(%s::uuid[])",
                  (space.name, lexical.DERIVATION, [str(s) for s in src_ids]))
        return {str(sid): sha for sid, sha in c.fetchall()}


# ---------------------------------------------------------------- coverage

@dataclasses.dataclass(frozen=True)
class Coverage:
    """The denominators every read response carries (divergence 019).

    total              head mesh messages in shared/mesh-chat
    embedded           of those, with a SEARCHABLE vector in this space (a forbidden
                       row's vector does not count while the gate is closed, because
                       no read will return it)
    skipped_forbidden  of those, embed_policy 'forbidden' while the gate is CLOSED: the
                       rows the gate keeps out. Zero while the gate is open.
    skipped_no_schema_version
                       of the rest, rows with NO schema_version and no text to embed:
                       legacy rows the v1 validator never saw (divergence 023). The job
                       walks past them; before 023 they were counted as `pending`.
    skipped_empty      of the rest, rows that DO declare schema_version and still have
                       no text. The v1 validator refuses a blank body, so this should
                       read zero; it is counted rather than assumed.
    pending            the rest: eligible and not (yet) embedded

    The five counts partition `total`: embedded + skipped_forbidden +
    skipped_no_schema_version + skipped_empty + pending == total.

    lexical_indexed    NOT part of the partition: of `total`, rows with a SEARCHABLE
                       lexical row in this space under the current derivation (the
                       lexical arm's denominator, divergence 026). The job lexes every
                       row with text that it READS, before and whether or not the
                       embedder takes it, so this may run ahead of `embedded` by up to
                       one tick's read while the embedder is behind or down, and trails
                       it only while a walk restarted for a new derivation catches up.
    """

    embedded: int
    total: int
    skipped_forbidden: int
    pending: int
    gate_open: bool
    measured_at: _dt.datetime
    skipped_no_schema_version: int = 0
    skipped_empty: int = 0
    lexical_indexed: int = 0

    def as_json(self) -> dict:
        return {"embedded": self.embedded, "total": self.total,
                "skipped_forbidden": self.skipped_forbidden,
                "skipped_no_schema_version": self.skipped_no_schema_version,
                "skipped_empty": self.skipped_empty, "pending": self.pending,
                "lexical_indexed": self.lexical_indexed,
                "forbidden_rows_open": self.gate_open,
                "measured_at": self.measured_at.isoformat()}


def coverage(conn, space: spaces.Space, *, gate_open: bool,
             namespace: str = MESH_CHAT_NAMESPACE) -> Coverage:
    """One pass over the namespace's head messages. B5: the cost is the size of the
    namespace, the same order as apps/mesh/graph.coverage; apps/meshsem/search.py holds
    the answer for COVERAGE_TTL_S and dates it rather than re-counting per request."""
    # The skip categories are DISJOINT in SQL, in the job's own order: the gate first,
    # then "no text" (split by whether the row declares schema_version), and never a row
    # holding a searchable vector. So the subtraction below cannot double-count.
    no_text = ("NOT (NOT %(open)s AND sm.embed_policy = 'forbidden') "
               f"AND me.src_id IS NULL AND NOT {has_text_sql('sm')}")
    declares = has_schema_version_sql("sm")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), "
            "  count(*) FILTER (WHERE me.src_id IS NOT NULL "
            "                   AND (%(open)s OR sm.embed_policy = 'allowed')), "
            "  count(*) FILTER (WHERE NOT %(open)s AND sm.embed_policy = 'forbidden'), "
            f"  count(*) FILTER (WHERE {no_text} AND NOT {declares}), "
            f"  count(*) FILTER (WHERE {no_text} AND {declares}), "
            "  count(*) FILTER (WHERE ml.src_id IS NOT NULL "
            "                   AND (%(open)s OR sm.embed_policy = 'allowed')), "
            "  now() "
            "FROM shared_memory sm "
            "LEFT JOIN mesh_embeddings me ON me.src_id = sm.id AND me.space = %(space)s "
            "  AND me.chunk_no = 0 "
            "LEFT JOIN mesh_lexical ml ON ml.src_id = sm.id AND ml.space = %(space)s "
            "  AND ml.derivation = %(derivation)s "
            "WHERE sm.namespace = %(ns)s AND sm.is_head AND sm.key LIKE 'msg/%%'",
            {"open": gate_open, "space": space.name, "ns": namespace,
             "derivation": lexical.DERIVATION})
        total, embedded, skipped, no_sv, empty, lexed, now = cur.fetchone()
    total, embedded, skipped = int(total), int(embedded), int(skipped)
    no_sv, empty = int(no_sv), int(empty)
    return Coverage(embedded=embedded, total=total, skipped_forbidden=skipped,
                    skipped_no_schema_version=no_sv, skipped_empty=empty,
                    lexical_indexed=int(lexed),
                    pending=max(0, total - embedded - skipped - no_sv - empty),
                    gate_open=gate_open, measured_at=now)


# ---------------------------------------------------------------- graph namespaces (divergence 060)

_UPSERT_CHUNK = (
    "INSERT INTO mesh_embeddings (src_id, src_key, src_version, space, dims, text_sha256,"
    " vec, provider_id, recorded_at, chunk_no, namespace) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (src_id, space, chunk_no) DO UPDATE SET text_sha256 = EXCLUDED.text_sha256, "
    " vec = EXCLUDED.vec, provider_id = EXCLUDED.provider_id, dims = EXCLUDED.dims, "
    " namespace = EXCLUDED.namespace, embedded_at = now() "
    "WHERE mesh_embeddings.text_sha256 IS DISTINCT FROM EXCLUDED.text_sha256"
)


def upsert_chunk_vectors(cur, space: spaces.Space, namespace: str, rows: list[tuple]) -> int:
    """rows: (src_id, src_key, src_version, chunk_no, text_sha256, vec, provider_id, recorded_at).
    The graph walker's writer (apps/meshsem/graphwalk.py): one vector per chunk of a row."""
    if not rows:
        return 0
    params = [(r[0], r[1], r[2], space.name, space.dims, r[4], spaces.encode(r[5]), r[6], r[7], r[3],
               namespace) for r in rows]
    cur.executemany(_UPSERT_CHUNK, params)
    return len(params)


_UPSERT_LEXICAL_NS = (
    "INSERT INTO mesh_lexical (src_id, src_key, src_version, space, derivation, text_sha256,"
    " lexemes, tfs, recorded_at, namespace) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (src_id, space) DO UPDATE SET text_sha256 = EXCLUDED.text_sha256, "
    " lexemes = EXCLUDED.lexemes, tfs = EXCLUDED.tfs, derivation = EXCLUDED.derivation, "
    " namespace = EXCLUDED.namespace, indexed_at = now() "
    "WHERE mesh_lexical.text_sha256 IS DISTINCT FROM EXCLUDED.text_sha256 "
    "   OR mesh_lexical.derivation IS DISTINCT FROM EXCLUDED.derivation"
)


def upsert_lexical_in(cur, space: spaces.Space, namespace: str, rows: list[tuple]) -> int:
    """`upsert_lexical` for a graph namespace: rows (src_id, src_key, src_version, text,
    text_sha256, recorded_at), the whole text of the row, not a chunk."""
    if not rows:
        return 0
    params = []
    for r in rows:
        lexemes, tfs = lexical.terms(r[3])
        params.append((r[0], r[1], r[2], space.name, lexical.DERIVATION, r[4], lexemes, tfs, r[5],
                       namespace))
    cur.executemany(_UPSERT_LEXICAL_NS, params)
    return len(params)


def derived_digests(cur, space: spaces.Space, namespace: str) -> dict[str, tuple]:
    """src_id -> (tuple of the chunks' text_sha256 in chunk order, lexical text_sha256 or None) for
    every row of `namespace` holding a vector or a lexical row in this space. The walker compares
    it with what the row derives to today."""
    out: dict[str, list] = {}
    with cur.connection.cursor() as c:
        c.execute("SELECT src_id, chunk_no, text_sha256 FROM mesh_embeddings "
                  "WHERE space = %s AND namespace = %s ORDER BY src_id, chunk_no",
                  (space.name, namespace))
        for sid, _chunk, sha in c.fetchall():
            out.setdefault(str(sid), [[], None])[0].append(sha)
        c.execute("SELECT src_id, text_sha256 FROM mesh_lexical "
                  "WHERE space = %s AND namespace = %s AND derivation = %s",
                  (space.name, namespace, lexical.DERIVATION))
        for sid, sha in c.fetchall():
            out.setdefault(str(sid), [[], None])[1] = sha
    return {k: (tuple(v[0]), v[1]) for k, v in out.items()}


def delete_derived_in(cur, space: spaces.Space, namespace: str, src_ids: list) -> int:
    """Delete this space's vectors and lexical rows of `src_ids` in `namespace` (a row no longer
    head, gone, now without content, or kept out by the gate). Returns vectors deleted."""
    if not src_ids:
        return 0
    ids = [str(s) for s in src_ids]
    cur.execute("DELETE FROM mesh_embeddings WHERE space = %s AND namespace = %s "
                "AND src_id = ANY(%s::uuid[])", (space.name, namespace, ids))
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    cur.execute("DELETE FROM mesh_lexical WHERE space = %s AND namespace = %s "
                "AND src_id = ANY(%s::uuid[])", (space.name, namespace, ids))
    return n


__all__ = ["Coverage", "ReferenceMismatch", "STATE_TABLE", "configured_space", "coverage",
           "delete_derived_in", "derived_digests", "upsert_chunk_vectors", "upsert_lexical_in",
           "ensure_state", "existing_lexical", "existing_shas", "forbidden_rows_open",
           "has_schema_version_sql", "has_text_sql", "read_state", "record_reference",
           "reference_of", "upsert_lexical", "upsert_vectors"]
