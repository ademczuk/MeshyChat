"""The LEXICAL arm: TF-IDF cosine over word tokens, computed in process. Divergence 026.

WHY THIS ARM EXISTS. On the A/B of 2026-09-23 (94 real queries, thread-membership proxy)
plain TF-IDF matched EmbeddingGemma on MRR and beat it on recall@5 by 0.12 to 0.14, and
reciprocal-rank fusion of the two recovered the recall the embedding alone lost
(apps/meshsem/fusion.py). So search ranks by fusing this arm with the vector arm.

THE TOKENS are the A/B's: runs of two or more word characters of the lowercased text
(`terms`). On the A/B's 929 messages this gives the harness tokenizer's exact token
multiset on 928 (`[a-z0-9_]{2,}`; the one difference is a message with non-ASCII
letters, which `\\w` keeps and the harness dropped), and scored by the harness's own
TF-IDF the two give identical per-query metrics. Postgres's `to_tsvector('simple', ...)`
was the first implementation and measured worse on the same queries: the harness TF-IDF
over its tokens scored MRR 0.545 and recall@5 0.504 against 0.572 and 0.541 (not
significant alone, p = 0.16 and 0.15), but through the real search path its FUSED
recall@5 fell 0.030 below the A/B's fusion (paired p = 0.010). Its parser splits
snake_case identifiers, keeps one-letter words, and indexes a path or URL as one lexeme;
this tokenizer keeps `mesh_embed_catch_up` whole and finds `search.py` inside
`apps/meshsem/search.py`.
Divergence 026 has both measurements.

ONE TOKENIZER, BOTH SIDES. The job stores `terms(text)` in mesh_lexical
(sql_meshychat/010) for exactly the text it embeds (the stripped body cut to 1,500
characters), and a query is tokenized by the same function in the serving process.
`DERIVATION` names it; a change to it restarts the job's walk (apps/meshsem/embedjob.py).

THE SCORE is the A/B's TF-IDF: raw term frequency times smoothed idf,
ln((1 + n) / (1 + df)) + 1, L2-normalised per document, and the cosine with the query
weighted the same way. A query term no document holds gets df = 0: it matches nothing and
only scales the query norm, which changes no order. Scores are in [0, 1]; a document
sharing no term with the query scores 0 and is NOT ranked by this arm (the A/B ranked
zero-score candidates in hash order; here they add nothing to the fusion rather than
noise). No stopword list: idf does that down-weighting.

IN PROCESS, LIKE THE VECTORS (apps/meshsem/search.py VectorIndex): refreshed
incrementally by (indexed_at, src_id) with an overlap window, reconciled every
FULL_RELOAD_S (one read of the live (src_id, digest) pairs drops deleted rows and loads
missing or changed ones by id; until 29 Oct 2026 this was a full reload of every row's
terms), and fully loaded only when new or over the ceiling, which is shared, newest kept. The postings are rebuilt lazily after a refresh that changed something, which the
job's 60 s tick bounds to about once a minute. The index is never the authority on what
may be returned: search.py joins every candidate back to shared_memory (head, gate,
retention).
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import Counter

import numpy as np
from psycopg.rows import dict_row

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from . import fullload

#: How mesh_lexical's terms are made. A change to `terms` is a new derivation: bump this,
#: and the job restarts its walk and drops the space's rows of the old one.
DERIVATION = "word2-lower/v1"

#: Two or more word characters (Unicode letters, digits, underscore), on lower().
_WORD = re.compile(r"\w{2,}")

OVERLAP_S = 60
FULL_RELOAD_S = 600


def terms(text: str) -> tuple[list[str], list[int]]:
    """(distinct terms, their frequencies) of `text`, terms sorted. THE tokenizer, for
    documents (the job) and queries (a request) alike."""
    counts = Counter(_WORD.findall((text or "").lower()))
    lexemes = sorted(counts)
    return lexemes, [counts[t] for t in lexemes]


class IndexWarming(Exception):
    """No build of this index exists yet and another request is making the first one. The caller answers 503 with
    Retry-After rather than queue behind it (13 Nov 2026 stalls, pxx point 4)."""


#: Retry-After seconds for IndexWarming: a cold full load measured about a minute on HQD (12-13 Nov stack dumps).
WARMING_RETRY_AFTER_S = 15
#: A refresh slower than this logs its parts (database read, puts, postings) so a slow database and a paging box
#: can be told apart.
SLOW_REFRESH_LOG_S = 2.0
log = logging.getLogger(__name__)


class LexicalIndex:
    """One space's terms, with an inverted index for scoring.

    BUILD, THEN SWAP (13 Nov 2026; pxx). A refresh used to hold `lock` across its database read and the
    per-row rebuild, and every concurrent rank() queued behind it: on HQD those queued searches filled the server's
    thread pool and /v1/livez missed (stack dumps 12 Nov 23:43, 23:45; 13 Nov 00:17). Now:
    - `_refresh_lock` makes refresh single-flight; a request that finds it taken does not wait (`wait=False`): it
      serves the last build, or raises IndexWarming if there is none yet;
    - the refresh runs the same incremental / reconcile / full policy on a SHALLOW COPY (`_clone`), with no serving
      lock held, builds the postings on the copy, then swaps the copy's state in under `lock` (`_adopt`);
    - rank() only reads a build made at swap time, so it never builds postings under the lock.
    Only refresh mutates the index, and refresh is single-flight, so the copy is taken from a state nobody is
    changing."""

    def __init__(self, space_name: str, namespace: str = MESH_CHAT_NAMESPACE):
        self.space_name = space_name
        #: One namespace's rows, the mail's unless named (sql_meshychat/030, divergence 060).
        self.namespace = namespace
        self.lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.vocab: dict[str, int] = {}
        self.terms: list[np.ndarray] = []      # per document: term ids, unique
        self.tfs: list[np.ndarray] = []        # per document: term frequencies
        self.ids: list[str] = []
        self.shas: list[str] = []
        self.pos: dict[str, int] = {}
        self.watermark = None                  # (indexed_at, src_id) of the newest row
        self.loaded_mono: float | None = None
        self.over_ceiling = False
        self._built = None

    @property
    def n(self) -> int:
        return len(self.ids)

    def put(self, src_id: str, sha: str, lexemes, tfs) -> None:
        """Add or replace one document. An unchanged digest is a no-op."""
        i = self.pos.get(src_id)
        if i is not None and self.shas[i] == sha:
            return
        lexemes = list(lexemes or [])
        t = np.fromiter((self.vocab.setdefault(x, len(self.vocab)) for x in lexemes),
                        dtype=np.int64, count=len(lexemes))
        f = np.asarray(list(tfs or []), dtype=np.float64)
        if t.shape != f.shape:
            raise ValueError(f"{src_id}: {t.shape[0]} terms but {f.shape[0]} frequencies")
        if i is None:
            self.pos[src_id] = len(self.ids)
            self.ids.append(src_id)
            self.shas.append(sha)
            self.terms.append(t)
            self.tfs.append(f)
        else:
            self.terms[i], self.tfs[i], self.shas[i] = t, f, sha
        self._built = None

    def _build(self):
        """(n, term pointer, posting docs, posting unit weights, idf per term)."""
        if self._built is not None:
            return self._built
        n, n_terms = self.n, len(self.vocab)
        if n:
            lens = np.fromiter((t.shape[0] for t in self.terms), dtype=np.int64, count=n)
            terms_ = np.concatenate(self.terms)
            tfs = np.concatenate(self.tfs)
        else:
            lens = np.zeros(0, dtype=np.int64)
            terms_, tfs = np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
        doc = np.repeat(np.arange(n, dtype=np.int64), lens)
        df = np.bincount(terms_, minlength=n_terms).astype(np.float64)
        idf = np.log((1.0 + n) / (1.0 + df)) + 1.0
        w = tfs * idf[terms_]
        norm = np.sqrt(np.bincount(doc, weights=w * w, minlength=n))
        unit = w / norm[doc] if w.shape[0] else w      # every posted doc has norm > 0
        order = np.argsort(terms_, kind="stable")
        ptr = np.zeros(n_terms + 1, dtype=np.int64)
        np.cumsum(np.bincount(terms_, minlength=n_terms), out=ptr[1:])
        self._built = (n, ptr, doc[order], unit[order], idf)
        return self._built

    def rank(self, lexemes, tfs, *, limit: int) -> tuple[list[str], np.ndarray]:
        """(src_ids best first, their cosines): only documents sharing a term with the
        query, at most `limit`. Ties keep index order (oldest first)."""
        with self.lock:
            n, ptr, pdoc, punit, idf = self._build()
            if n == 0 or not lexemes:
                return [], np.zeros(0, dtype=np.float64)
            oov_idf = math.log(1.0 + n) + 1.0          # df = 0
            wq, hits = [], []
            for lx, tf in zip(lexemes, tfs):
                t = self.vocab.get(lx)
                w = float(tf) * (float(idf[t]) if t is not None else oov_idf)
                wq.append(w)
                if t is not None and ptr[t + 1] > ptr[t]:
                    hits.append((t, w))
            qnorm = math.sqrt(sum(w * w for w in wq))
            scores = np.zeros(n, dtype=np.float64)
            for t, w in hits:
                s, e = ptr[t], ptr[t + 1]
                scores[pdoc[s:e]] += w * punit[s:e]    # a doc appears once per term
            if qnorm > 0:
                scores /= qnorm
            cand = np.flatnonzero(scores > 0.0)
            order = cand[np.argsort(-scores[cand], kind="stable")][:max(0, int(limit))]
            return [self.ids[i] for i in order], scores[order]

    def _mark(self, row) -> None:
        mark = (row["indexed_at"], str(row["src_id"]))
        if self.watermark is None or mark > self.watermark:
            self.watermark = mark

    def _reconcile(self, cur, ceiling: int) -> bool:
        """The periodic step once older than FULL_RELOAD_S, in place of a full reload of every
        document's terms: one read of the live (src_id, text_sha256) pairs; drop what is gone; load
        by id what is missing or whose digest differs from the loaded one (a row committed more than
        OVERLAP_S late, or a late-committed change, pxx P2-2). False, for the full load, when
        the index is over its ceiling (the full load keeps the newest `ceiling` and refills after
        deletes, P2-1) or the loads would pass it. The vocabulary keeps a dropped document's terms
        until the next full load: memory only, a df-0 term scores as an unknown one (pxx INFO)."""
        if self.over_ceiling:
            return False
        cur.execute("SELECT src_id, text_sha256 FROM mesh_lexical WHERE space = %s AND namespace = %s "
                    "  AND derivation = %s",
                    (self.space_name, self.namespace, DERIVATION))
        live = {str(r["src_id"]): r["text_sha256"] for r in cur.fetchall()}
        keep = [i for i, s in enumerate(self.ids) if s in live]
        if len(keep) != self.n:
            self.ids = [self.ids[i] for i in keep]
            self.shas = [self.shas[i] for i in keep]
            self.terms = [self.terms[i] for i in keep]
            self.tfs = [self.tfs[i] for i in keep]
            self.pos = {s: i for i, s in enumerate(self.ids)}
            self._built = None
        want = [s for s, sha in live.items() if s not in self.pos or self.shas[self.pos[s]] != sha]
        if self.n + sum(1 for s in want if s not in self.pos) > ceiling:
            return False
        if want:
            cur.execute("SELECT src_id, text_sha256, indexed_at, lexemes, tfs "
                        "FROM mesh_lexical WHERE space = %s AND namespace = %s "
                        "  AND derivation = %s AND src_id = ANY(%s::uuid[]) "
                        "ORDER BY indexed_at ASC, src_id ASC",
                        (self.space_name, self.namespace, DERIVATION, want))
            for r in cur.fetchall():
                self.put(str(r["src_id"]), r["text_sha256"], r["lexemes"], r["tfs"])
                self._mark(r)
        self.loaded_mono = time.monotonic()
        return True

    #: The fields a refresh changes; `_clone` copies them shallowly and `_adopt` swaps them in.
    _STATE = ("vocab", "terms", "tfs", "ids", "shas", "pos", "watermark", "loaded_mono", "over_ceiling", "_built")

    def _clone(self) -> "LexicalIndex":
        """A copy a refresh can change freely: new containers, shared per-document arrays (which put() replaces,
        never edits in place)."""
        twin = LexicalIndex.__new__(LexicalIndex)
        twin.space_name, twin.namespace = self.space_name, self.namespace
        twin.vocab, twin.pos = dict(self.vocab), dict(self.pos)
        twin.terms, twin.tfs, twin.ids, twin.shas = list(self.terms), list(self.tfs), list(self.ids), list(self.shas)
        twin.watermark, twin.loaded_mono = self.watermark, self.loaded_mono
        twin.over_ceiling, twin._built = self.over_ceiling, self._built
        return twin

    def _adopt(self, twin: "LexicalIndex") -> None:
        with self.lock:
            for name in self._STATE:
                setattr(self, name, getattr(twin, name))

    def refresh(self, conn, *, ceiling: int, wait: bool = False) -> None:
        """Bring the index up to date. `conn` must be inside server_internal. The same incremental / reconcile /
        full policy as the vector index, run on a copy and swapped in (class docstring).

        `wait=False` (requests): if another refresh is running, return at once and serve the last build; with no
        build yet, raise IndexWarming. `wait=True` (background jobs): queue for the refresh lock."""
        if not self._refresh_lock.acquire(blocking=wait):
            if self.loaded_mono is None:
                raise IndexWarming(f"lexical index {self.space_name}/{self.namespace} is being built")
            return
        try:
            pre = None
            if self.loaded_mono is not None and time.monotonic() - self.loaded_mono <= FULL_RELOAD_S:
                # The usual case: an incremental step. Read it against the serving index first (safe: refresh is
                # single-flight and only refresh mutates), and copy nothing when no row changes anything: the
                # overlap window re-reads the last minute's rows on every search (pxx P3-2).
                with conn.cursor(row_factory=dict_row) as cur:
                    pre = self._incremental(cur)
                if not any(self._changes(r) for r in pre):
                    return
            twin = self._clone()
            twin._refresh_rows(conn, ceiling=ceiling, pre_rows=pre)
            t2 = time.monotonic()
            twin._build()
            build_s = time.monotonic() - t2
            self._adopt(twin)
        finally:
            self._refresh_lock.release()
        read_s, put_s, n_rows = twin._timing
        if read_s + put_s + build_s >= SLOW_REFRESH_LOG_S:
            log.warning("lexical refresh %s/%s slow: db read %.2fs, %d rows put %.2fs, postings %.2fs (%d docs)",
                        self.space_name, self.namespace, read_s, n_rows, put_s, build_s, twin.n)

    def _incremental(self, cur) -> list:
        """The incremental read: the overlap window behind the watermark. Reads only."""
        wm_at = self.watermark[0] if self.watermark else None
        cur.execute("SELECT src_id, text_sha256, indexed_at, lexemes, tfs "
                    "FROM mesh_lexical WHERE space = %s AND namespace = %s "
                    "  AND derivation = %s "
                    "  AND (%s::timestamptz IS NULL OR indexed_at >= "
                    "       %s::timestamptz - make_interval(secs => %s)) "
                    "ORDER BY indexed_at ASC, src_id ASC",
                    (self.space_name, self.namespace, DERIVATION, wm_at, wm_at, OVERLAP_S))
        return cur.fetchall()

    def _changes(self, row) -> bool:
        """Whether this row changes the index or moves its watermark: a new document, a changed digest, or a row
        newer than the watermark (put() is a no-op on an unchanged digest, but _mark() still moves the mark; pxx
        35bb76 P2-1: without this a re-indexed row with the same text left the watermark stuck)."""
        i = self.pos.get(str(row["src_id"]))
        if i is None or self.shas[i] != row["text_sha256"]:
            return True
        return self.watermark is None or (row["indexed_at"], str(row["src_id"])) > self.watermark

    def _refresh_rows(self, conn, *, ceiling: int, pre_rows: list | None = None) -> None:
        """The refresh policy itself, on an index no request reads (a `_clone`), so it takes no lock. It logs the
        database read and the rebuild apart, to tell a slow database from a paging box (pxx point 5).
        `pre_rows`: the incremental read refresh() already made; then no reconcile and no second read."""
        t0 = time.monotonic()
        with conn.cursor(row_factory=dict_row) as cur:
            rows = None
            if (pre_rows is None and self.loaded_mono is not None
                    and time.monotonic() - self.loaded_mono > FULL_RELOAD_S
                    and not self._reconcile(cur, ceiling)):
                self.loaded_mono = None                    # the full load below
            if pre_rows is not None or (self.loaded_mono is not None
                                        and time.monotonic() - self.loaded_mono <= FULL_RELOAD_S):
                rows = pre_rows if pre_rows is not None else self._incremental(cur)
                fresh = sum(1 for r in rows if str(r["src_id"]) not in self.pos)
                if self.n + fresh > ceiling:
                    rows = None
            if rows is None:
                # The full load: its one statement runs under fullload.FULL_LOAD_TIMEOUT_MS, not the request's 30 s.
                rows = fullload.fetch_all(
                    cur, "SELECT src_id, text_sha256, indexed_at, lexemes, tfs "
                         "FROM mesh_lexical WHERE space = %s AND namespace = %s "
                         "  AND derivation = %s "
                         "ORDER BY indexed_at DESC, src_id DESC LIMIT %s",
                    (self.space_name, self.namespace, DERIVATION, ceiling + 1))
                self._reset()
                self.over_ceiling = len(rows) > ceiling
                rows = list(reversed(rows[:ceiling]))
                self.loaded_mono = time.monotonic()
            t1 = time.monotonic()
            for r in rows:
                self.put(str(r["src_id"]), r["text_sha256"], r["lexemes"], r["tfs"])
                self._mark(r)
        self._timing = (t1 - t0, time.monotonic() - t1, len(rows))

    def describe(self, ceiling: int) -> dict:
        with self.lock:
            return {"documents": self.n, "terms": len(self.vocab), "ceiling": ceiling,
                    "over_ceiling": self.over_ceiling, "derivation": DERIVATION}


__all__ = ["DERIVATION", "FULL_RELOAD_S", "IndexWarming", "LexicalIndex", "OVERLAP_S", "WARMING_RETRY_AFTER_S",
           "terms"]
