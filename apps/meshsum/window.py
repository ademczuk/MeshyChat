"""Which messages a summary covers, and how many fit in 2,816 tokens.

Divergence 037; docs/design/thread-summaries.md 1.1, 1.2 and 2.3.

READING (in the caller's transaction, the one that also asks the body gate, 2.2):

  thread   head `msg/` rows of shared/mesh-chat with a DECLARED `in_thread` edge to the
           thread (mesh_edges), newest first from `before`, at most SCAN_LIMIT examined.
           `unprojected` counts head rows whose own value names the thread and that lie
           above the edges projection's watermark: not summarised, because the graph
           does not yet declare them part of the thread (critique C16).
  backlog  exactly the rows the ADDRESSED wait (divergence 031) would answer this caller
           after `since`, read by that wait's own page query (`wait.addressed_page`) and
           kept by the push's own decision (`mesh_notify.role_for`), never re-expressed;
           no row past the settle horizon (`wait.settle_horizon`), no row the caller
           wrote; oldest first.

THE PER-ROW RULES, in order (2.3): head, shared/mesh-chat and `msg/` are the query; then
the embed_policy gate (a row that is not `allowed` is left out while the gate is closed:
`excluded.embed_forbidden`); then a body that is not a string or is empty after stripping
(`excluded.no_text`). ONLY A ROW THAT PASSES THEM ALL IS EVER TOKENIZED: `/tokenize` is
the model's own server, so a row the gate keeps from the model never reaches it either
(critique C4). The loop is per row so that an object-level rule (design B07, not merged)
becomes one more row here.

PACKING (outside any transaction: 019's rule, no transaction across a network call).
TOKENS ARE COUNTED, NOT ESTIMATED: the pinned provider's `/tokenize` counts each candidate's
rendered block, once per (model pin, row id, version, shown length): a repeated request,
a cache hit included, asks only about rows it has not counted before. The thread packs
newest first and the backlog oldest first (so `through` is a place to resume from),
until the next message would not fit; a first message that does not fit even alone is
cut to the budget (`excluded.cut_to_fit`, `body_truncated`). The nonce and canary that
frame the final prompt are derived from the covered set, so each block is counted with a
placeholder of the same length and the difference is counted once the set is known; a
set that then overflows loses its last-packed message and is counted again. A provider
that cannot tokenize is `TokenizeFailed` (the route's 503 E_CHAT_UNAVAILABLE), never a
character estimate: HQB's tokenizer measured 1.44 to 4.82 characters a token on
synthetic text, so no estimate is safe for both base64 and prose.
"""
from __future__ import annotations

import collections
import dataclasses
import datetime as _dt
import hashlib
import threading
import time
import uuid

from psycopg.rows import dict_row

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.mesh import normalize
from apps.mesh import wait as mesh_wait
from apps.meshsem import providers, search
from apps.wsgw import mesh_notify

from . import prompt as P
from . import validate as V

MAX_MESSAGES = 40
DEFAULT_MESSAGES = 20
#: Thread rows examined per request, newest first from `before` (B5). A window that
#: examined this many without filling is reported (`window.scan_limited`).
SCAN_LIMIT = 400
#: Rows naming a thread above the projection's watermark, counted at most (B5).
UNPROJECTED_LIMIT = 1000
#: What a block is counted with before its real number and nonce are known.
PLACEHOLDER_N = 40
PLACEHOLDER_NONCE = hashlib.sha256(b"meshsum placeholder nonce").hexdigest()[:32]
PLACEHOLDER_CANARY = "CANARY-" + hashlib.sha256(b"meshsum placeholder canary").hexdigest()[:16]
#: The token-count map (B8: bounded), and one call's ceiling.
COUNTS_MAX = 8192
TOKENIZE_TIMEOUT_S = 10.0

WINDOW_EMBED_FORBIDDEN = "embed_forbidden"
WINDOW_NO_TEXT = "no_text"


class TokenizeFailed(RuntimeError):
    """The pinned provider did not count a text within the request's budget."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


@dataclasses.dataclass
class Row:
    id: uuid.UUID
    key: str
    version: int
    created_at: _dt.datetime
    author: str | None
    author_peer_id: uuid.UUID
    value: dict
    embed_policy: str
    raw_body: str | None
    #: The DECLARED thread (mesh_edges), for the backlog's headers and facts.
    thread_id: str | None = None
    #: The push's role for this caller (backlog only).
    role: str | None = None
    #: The body as the model is shown it: stripped, cut at 1,500 characters
    #: (search.body_fields), hidden characters escaped, and cut to fit when it must be.
    body: str = ""
    body_truncated: bool = False
    escaped: int = 0

    def cursor(self) -> str:
        return mesh_wait.encode_cursor(mesh_wait.micros_of(self.created_at), self.id)

    def at(self) -> str:
        return self.created_at.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclasses.dataclass
class Candidates:
    kind: str
    #: Rows that passed every per-row rule, in PACKING order (thread newest first,
    #: backlog oldest first), at most max_messages.
    rows: list
    excluded: dict
    #: thread: head rows in the thread, all rules aside, older than `before` (or all).
    in_scope: int = 0
    #: thread: each candidate's index among every scanned row (newest first), so the
    #: count of older rows left out is `in_scope - index - 1`.
    scan_index: dict = dataclasses.field(default_factory=dict)
    scan_limited: bool = False
    #: thread: how many rows the scan read, rules aside.
    scanned_count: int = 0
    unprojected: int = 0
    declared_total: int = 0
    #: backlog: where the scan stopped (a resume point, never past the settle horizon),
    #: and whether rows may remain after it.
    position: object = None
    scan_more: bool = False
    #: backlog: every addressed row the scan read (rules aside), for `facts`.
    scanned: list = dataclasses.field(default_factory=list)
    #: peers.name of each candidate author, for its labels.
    names: dict = dataclasses.field(default_factory=dict)


def _passes(row: Row, gate_open: bool) -> str | None:
    """The per-row rules after the query's own (2.3), in order: None, or the exclusion."""
    if row.embed_policy != "allowed" and not gate_open:
        return WINDOW_EMBED_FORBIDDEN
    if row.raw_body is None or not row.raw_body.strip():
        return WINDOW_NO_TEXT
    return None


def _shown(row: Row) -> Row:
    text, truncated = search.body_fields(row.raw_body)
    text, n = V.escape_hidden(text)
    row.body, row.body_truncated, row.escaped = text, truncated, n
    return row


_ROW_COLS = ("sm.id, sm.key, sm.version, sm.created_at, sm.author_label, "
             "sm.created_by_peer_id, sm.value, sm.embed_policy, "
             "CASE WHEN jsonb_typeof(sm.value->'body') = 'string' "
             "THEN sm.value->>'body' END AS raw_body")


def _row(r: dict, **extra) -> Row:
    value = r["value"] if isinstance(r["value"], dict) else {}
    return Row(id=r["id"], key=r["key"], version=r["version"], created_at=r["created_at"],
               author=r["author_label"], author_peer_id=r["created_by_peer_id"],
               value=value, embed_policy=r["embed_policy"], raw_body=r["raw_body"], **extra)


def _names(conn, peer_ids) -> dict:
    ids = sorted({str(p) for p in peer_ids})
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM peers WHERE id = ANY(%s::uuid[])", (ids,))
        found = {str(i): n for i, n in cur.fetchall()}
    from apps.peers import names as peer_names
    return peer_names.uncontested(conn, found)                # divergence 066: not an alias when contested


def _watermark(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT watermark_at, watermark_id FROM mesh_edges_projection "
                    "WHERE projection = 'mesh_edges'")
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def unprojected(conn, thread_id: str) -> int:
    """Head rows naming `thread_id` (as the projection reads a thread: normalize.thread_of)
    that lie above the edges projection's watermark."""
    wm_at, wm_id = _watermark(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sm.value FROM shared_memory sm "
            "WHERE sm.namespace = %(ns)s AND sm.is_head AND sm.key LIKE 'msg/%%' "
            "  AND (sm.value->>'thread_id' = %(t)s OR sm.value->>'thread' = %(t)s) "
            "  AND (%(wm_at)s::timestamptz IS NULL "
            "       OR (sm.created_at, sm.id) > (%(wm_at)s::timestamptz, %(wm_id)s::uuid)) "
            "LIMIT %(lim)s",
            {"ns": MESH_CHAT_NAMESPACE, "t": thread_id, "wm_at": wm_at,
             "wm_id": str(wm_id) if wm_id else None, "lim": UNPROJECTED_LIMIT})
        values = [r[0] for r in cur.fetchall()]
    return sum(1 for v in values
               if isinstance(v, dict) and normalize.thread_of(v)[0] == thread_id)


def read_thread(conn, thread_id: str, *, before, max_messages: int,
                gate_open: bool) -> Candidates:
    """The thread's candidates. `before`: a decoded wait cursor or None. `conn` is inside
    the caller's server_internal transaction."""
    params = {"t": thread_id, "ns": MESH_CHAT_NAMESPACE,
              "b_at": None if before is None else mesh_wait._bind_instant(
                  before.created_at_micros),
              "b_id": None if before is None else str(before.id)}
    scope = ("FROM mesh_edges e JOIN shared_memory sm ON sm.id = e.src_id AND sm.is_head "
             "WHERE e.edge_type = 'in_thread' AND e.dst = %(t)s "
             "  AND sm.namespace = %(ns)s AND sm.key LIKE 'msg/%%' ")
    older = ("  AND (%(b_at)s::timestamptz IS NULL "
             "       OR (sm.created_at, sm.id) < (%(b_at)s::timestamptz, %(b_id)s::uuid)) ")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n " + scope, params)
        declared_total = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n " + scope + older, params)
        in_scope = cur.fetchone()["n"]
        cur.execute("SELECT " + _ROW_COLS + " " + scope + older +
                    "ORDER BY sm.created_at DESC, sm.id DESC LIMIT %(lim)s",
                    {**params, "lim": SCAN_LIMIT})
        scanned = cur.fetchall()
    excluded = {WINDOW_EMBED_FORBIDDEN: 0, WINDOW_NO_TEXT: 0}
    rows: list[Row] = []
    index: dict = {}
    for i, r in enumerate(scanned):
        if len(rows) >= max_messages:
            break
        row = _row(r, thread_id=thread_id)
        why = _passes(row, gate_open)
        if why is not None:
            excluded[why] += 1
            continue
        rows.append(_shown(row))
        index[str(row.id)] = i
    return Candidates(kind="thread", rows=rows, excluded=excluded, in_scope=in_scope,
                      scan_index=index,
                      scan_limited=len(scanned) >= SCAN_LIMIT and len(rows) < max_messages,
                      scanned_count=len(scanned),
                      unprojected=unprojected(conn, thread_id),
                      declared_total=declared_total,
                      names=_names(conn, [r.author_peer_id for r in rows]))


def _order(c) -> tuple:
    return c.created_at_micros, c.id.int


def read_backlog(conn, peer_id, *, since, max_messages: int, gate_open: bool) -> Candidates:
    """The backlog's candidates: the addressed wait's rows after `since` (a decoded
    cursor), under its settle horizon, oldest first, at most `max_messages` passing the
    per-row rules. Page by page with the wait's own query; each page's addressed rows are
    read whole in one more query of the same transaction.

    `since` may be the addressed wait's seq cursor (STATUS E13, a monitor's saved cursor):
    it is placed on this walk's `(created_at, id)` order by `mesh_wait.legacy_position`,
    just before every row past it in commit order, so no such row is left out (rows already
    delivered may be summarised again)."""
    if isinstance(since, mesh_wait.SeqCursor):
        since = mesh_wait.legacy_position(conn, since)
    horizon = mesh_wait.settle_horizon(conn)
    labels = mesh_wait.caller_labels(conn, peer_id)
    position = since
    rows: list[Row] = []
    scanned: list[Row] = []
    excluded = {WINDOW_EMBED_FORBIDDEN: 0, WINDOW_NO_TEXT: 0}
    more = False
    for _ in range(mesh_wait.ADDRESSED_SCAN_PAGES):
        page = mesh_wait.addressed_page(conn, peer_id, position)
        if not page:
            break
        roles = {}
        for key, author, _created, rid, *cols in page:
            role = mesh_notify.nudge_from_columns(key, author, cols).role(labels)
            if role is not None:
                roles[str(rid)] = role
        full = _full_rows(conn, list(roles))
        stop = False
        for _key, _author, created, rid, *_cols in page:
            here = mesh_wait.Cursor(mesh_wait.micros_of(created), rid)
            if _order(here) > _order(horizon):
                stop = True                     # not yet settled: a later request
                break
            if len(rows) >= max_messages:
                more = stop = True              # the window is full
                break
            position = here
            r = full.get(str(rid))
            if r is None:
                continue
            row = _row(r, thread_id=r["thread"], role=roles[str(rid)])
            scanned.append(row)
            why = _passes(row, gate_open)
            if why is not None:
                excluded[why] += 1
                continue
            rows.append(_shown(row))
        if stop or len(page) < mesh_wait.ROW_LIMIT:
            break
    else:
        more = True                             # the page budget was spent
    return Candidates(kind="backlog", rows=rows, excluded=excluded, position=position,
                      scan_more=more, scanned=scanned,
                      names=_names(conn, [r.author_peer_id for r in rows]))


def _full_rows(conn, ids: list) -> dict:
    if not ids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT " + _ROW_COLS + ", "
                    "(SELECT t.dst FROM mesh_edges t WHERE t.src_id = sm.id "
                    "   AND t.edge_type = 'in_thread' ORDER BY t.dst LIMIT 1) AS thread "
                    "FROM shared_memory sm WHERE sm.id = ANY(%s::uuid[]) AND sm.is_head",
                    (ids,))
        return {str(r["id"]): r for r in cur.fetchall()}


# ------------------------------------------------------------------ token counting

_COUNTS: "collections.OrderedDict[tuple, int]" = collections.OrderedDict()
_COUNTS_LOCK = threading.Lock()


def forget_counts() -> None:
    with _COUNTS_LOCK:
        _COUNTS.clear()


class TokenCounter:
    """Counts on the pinned provider's tokenizer, remembered per model pin (B8: at most
    COUNTS_MAX entries, least recently used first out). `calls` is how many texts THIS
    counter sent."""

    def __init__(self, provider, model_pin: str, *, deadline: float):
        self.provider, self.model_pin, self.deadline = provider, model_pin, deadline
        self.calls = 0

    def _get(self, key: tuple, text: str) -> int:
        key = (self.model_pin,) + key
        with _COUNTS_LOCK:
            if key in _COUNTS:
                _COUNTS.move_to_end(key)
                return _COUNTS[key]
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TokenizeFailed("the request's budget was spent counting tokens",
                                 [{"id": self.provider.id, "verdict": "not_tried",
                                   "reason": "budget spent"}])
        try:
            n = providers.tokenize(self.provider, text,
                                   timeout=min(TOKENIZE_TIMEOUT_S, remaining))
        except providers.ContentRefused as exc:
            raise TokenizeFailed(str(exc), [{"id": self.provider.id,
                                             "verdict": "refused_must8"}]) from None
        except providers.ProviderError as exc:
            raise TokenizeFailed(f"tokenize failed: {exc}",
                                 [{"id": self.provider.id, "verdict": "failed",
                                   "reason": f"tokenize: {exc}"}]) from None
        self.calls += 1
        with _COUNTS_LOCK:
            _COUNTS[key] = n
            _COUNTS.move_to_end(key)
            while len(_COUNTS) > COUNTS_MAX:
                _COUNTS.popitem(last=False)
        return n

    def text(self, text: str) -> int:
        return self._get(("text", hashlib.sha256(text.encode("utf-8")).hexdigest()), text)

    def row(self, row: Row, *, show_thread: bool) -> int:
        header = P.render_header(PLACEHOLDER_N, author=row.author, at=row.at(),
                                 value=row.value, show_thread=show_thread).text
        block = P.render_block(PLACEHOLDER_N, header=header, body=row.body,
                               nonce=PLACEHOLDER_NONCE)
        return self._get(("row", str(row.id), int(row.version), len(row.body),
                          bool(show_thread)), block)


# ------------------------------------------------------------------------- packing

@dataclasses.dataclass
class Packed:
    #: Presentation order: oldest first, numbered 1..N.
    rows: list
    cut_to_fit: int
    tokens: int
    budget: int
    #: backlog: whether candidates remained unpacked after the last covered row.
    unpacked: bool = False


def _cut(row: Row, room: int, counter: TokenCounter, show_thread: bool):
    """`row` with its body cut until its block fits `room` tokens, and that count; or
    None when not even the header fits."""
    body = row.body
    for _ in range(8):
        trial = dataclasses.replace(row, body=body, body_truncated=True)
        t = counter.row(trial, show_thread=show_thread)
        if t <= room:
            return trial, t
        new_len = int(len(body) * room / max(t, 1) * 0.9)
        if new_len <= 0 or new_len >= len(body):
            new_len = len(body) // 2
        if new_len <= 0:
            return None
        body = body[:new_len].rstrip()
    return None


def budget_for(counter: TokenCounter) -> int:
    """Tokens left for the message blocks: n_ctx minus the answer, the template margin,
    the system message (with a placeholder canary) and the user message's framing."""
    b = P.BUDGET
    system = counter.text(P.system_text(PLACEHOLDER_CANARY))
    user = counter.text(P.render_user([]))
    return b["n_ctx"] - b["max_tokens"] - b["template_margin"] - system - user


def pack(cands: Candidates, counter: TokenCounter) -> Packed:
    show_thread = cands.kind == "backlog"
    budget = budget_for(counter)
    chosen: list[tuple[Row, int]] = []
    used, cut = 0, 0
    unpacked = False
    for row in cands.rows:
        t = counter.row(row, show_thread=show_thread)
        if used + t <= budget:
            chosen.append((row, t))
            used += t
            continue
        if not chosen:
            got = _cut(row, budget, counter, show_thread)
            if got is not None:
                chosen.append(got)
                used += got[1]
                cut = 1
        unpacked = True
        break
    # The real nonce and canary are known only for a set; count the difference and give
    # back the last-packed message until the set fits.
    ph_nonce, ph_canary = counter.text(PLACEHOLDER_NONCE), counter.text(PLACEHOLDER_CANARY)
    while chosen:
        ordered = _present([r for r, _ in chosen])
        digest = P.covered_digest((r.id, r.version) for r in ordered)
        corr = (2 * len(chosen) * (counter.text(P.nonce_for(digest)) - ph_nonce)
                + counter.text(P.canary_for(digest)) - ph_canary)
        if used + corr <= budget:
            used += corr
            break
        _row_, t = chosen.pop()
        used -= t
        unpacked = True
    rows = _present([r for r, _ in chosen])
    return Packed(rows=rows, cut_to_fit=cut if chosen else 0, tokens=used, budget=budget,
                  unpacked=unpacked)


def _present(rows: list) -> list:
    """Oldest first, whatever order they were packed in: by INSTANT. Two times of one zone sort on the wall clock, so
    in a session zone with summer time a CET row after the autumn change sorted before an earlier CEST one
    (codex-pxx-cli TASK AL)."""
    utc = _dt.timezone.utc
    return sorted(rows, key=lambda r: (r.created_at.astimezone(utc) if r.created_at.tzinfo else r.created_at,
                                       str(r.id)))


# ------------------------------------------------------------------------ composing

@dataclasses.dataclass
class Composed:
    covered_digest: str
    nonce: str
    canary: str
    messages: list
    prompt_digest: str
    sources: tuple
    covered: list
    unrenderable: int
    escaped: int


def _labels_of(row: Row, names: dict) -> frozenset:
    out = set(mesh_notify.peer_labels(uuid.UUID(str(row.author_peer_id)),
                                      names.get(str(row.author_peer_id))))
    if row.author:
        out.add(row.author.lower())
    return frozenset(out)


def compose(rows: list, kind: str, *, names: dict, thread_id: str | None) -> Composed:
    """The prompt for `rows` (presentation order), the validator's view of each message,
    and the `covered` list the response carries."""
    digest = P.covered_digest((r.id, r.version) for r in rows)
    nonce, canary = P.nonce_for(digest), P.canary_for(digest)
    blocks, sources, covered = [], [], []
    bad = escaped = 0
    for n, row in enumerate(rows, start=1):
        header = P.render_header(n, author=row.author, at=row.at(), value=row.value,
                                 show_thread=kind == "backlog")
        bad += header.unrenderable
        escaped += row.escaped
        blocks.append(P.render_block(n, header=header.text, body=row.body, nonce=nonce))
        sources.append(V.Source(
            n=n, author=header.author, aliases=_labels_of(row, names), body=row.body,
            to=header.to, cc=header.cc,
            thread=thread_id if kind == "thread" else header.thread, at=row.at()))
        covered.append({"n": n, "key": row.key, "version": row.version,
                        "recorded_at": row.created_at.isoformat(), "author": row.author,
                        "body_truncated": bool(row.body_truncated)})
    messages = P.messages_for(P.system_text(canary), P.render_user(blocks))
    return Composed(covered_digest=digest, nonce=nonce, canary=canary, messages=messages,
                    prompt_digest=P.prompt_digest(messages), sources=tuple(sources),
                    covered=covered, unrenderable=bad, escaped=escaped)


__all__ = ["Candidates", "Composed", "DEFAULT_MESSAGES", "MAX_MESSAGES", "Packed", "Row",
           "SCAN_LIMIT", "TokenCounter", "TokenizeFailed", "budget_for", "compose",
           "forget_counts", "pack", "read_backlog", "read_thread", "unprojected"]
