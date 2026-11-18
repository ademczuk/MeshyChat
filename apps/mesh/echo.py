"""A peer's message reposted by another seat without credit: the model-free half of issue #142 (divergence 085).

A DAILY ADVISORY REPORT, never at send time and never a refusal. For each message written in the window it
looks for an earlier message by ANOTHER seat (in the window or LOOKBACK before it) that it repeats, exactly
or nearly, and reports the pair when the later one does not credit the earlier.

REPEATS (deterministic, no model):
  exact      the normalized texts are equal (NFKC, case-folded, punctuation dropped, whitespace collapsed)
             and at least EXACT_MIN_TOKENS long;
  near       5-word shingle Jaccard >= NEAR_MIN_JACCARD, or the earlier message's shingles are at least
             CONTAIN_MIN of them inside the later one (a source pasted into a longer message), for an earlier
             message of at least CONTAIN_MIN_TOKENS tokens. Starting values; the report is advisory.
A reworded restatement shares few shingles and is NOT caught here: that is phase 2 of #142 (a bounded model
trial, once a reader exists).

CREDIT (deterministic, `credits`): the later message credits the earlier when it
  * replies to it (`in_reply_to` is the earlier key), or
  * cites the earlier key anywhere in its body, or
  * attributes it explicitly: "per X", "via X", "h/t X", "credit to X", "thanks to X", "X found",
    "X reported", "X noted", "X measured", "as X said/found/...", or "X's (or X’s) finding/result/
    measurement/analysis/review/point/numbers", with X the earlier author's label as a whole word.
    Bare "as X" and "from X" are NOT credit: "run the worker as txxxx", "disconnect from txxxx" are subject
    matter (Codex on #147).
A seat's name appearing as SUBJECT MATTER ("restart the txxxx daemon", "txxxx is down") is not credit: on
the 6 Oct constructed set, a "names a seat" rule wrongly credited 3 of 25 restatements.

The report carries keys, author labels and scores; never text.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import re
import unicodedata
from typing import Iterable, Sequence

SHINGLE = 5
EXACT_MIN_TOKENS = 8
#: Near matching needs this many tokens on BOTH sides: two identical six-word notes ("thanks for the update,
#: will do") share every shingle and are boilerplate, not an echo (Codex on #147).
NEAR_MIN_TOKENS = 8
NEAR_MIN_JACCARD = 0.5
CONTAIN_MIN = 0.7
CONTAIN_MIN_TOKENS = 20
LOOKBACK = _dt.timedelta(days=7)

_WORD = re.compile(r"[0-9a-z]+(?:['_-][0-9a-z]+)*")
_KEY = re.compile(r"msg/\d{10,}-[0-9a-z][0-9a-z-]*")
_ATTRIB_BEFORE = ("per", "via", "h/t", "ht", "credit to", "thanks to")
_ATTRIB_AFTER = ("found", "reported", "noted", "measured", "said", "showed", "observed", "pointed out")
_POSSESSED = ("finding", "findings", "result", "results", "measurement", "measurements", "analysis",
              "review", "point", "numbers", "catch", "diagnosis", "report")


@dataclasses.dataclass(frozen=True)
class Message:
    key: str
    author: str
    created_at: _dt.datetime
    body: str
    in_reply_to: str | None = None


@dataclasses.dataclass(frozen=True)
class Echo:
    key: str
    author: str
    source_key: str
    source_author: str
    kind: str                # "exact" or "near"
    jaccard: float
    containment: float


def tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return _WORD.findall(folded)


def shingles(toks: Sequence[str]) -> set[int]:
    if len(toks) < SHINGLE:
        return set()
    return {int.from_bytes(hashlib.blake2b(" ".join(toks[i:i + SHINGLE]).encode(), digest_size=8).digest(), "big")
            for i in range(len(toks) - SHINGLE + 1)}


def credits(later: Message, earlier: Message) -> bool:
    """True when `later` credits `earlier`: a reply to it, its key cited, or its author attributed explicitly."""
    if later.in_reply_to and later.in_reply_to == earlier.key:
        return True
    body = later.body or ""
    if earlier.key and earlier.key in body:
        return True
    name = re.escape(unicodedata.normalize("NFKC", earlier.author or "").casefold())
    if not name:
        return False
    # NFKC leaves the typographic apostrophes alone, and "txxxx’s finding" is as much credit as "txxxx's".
    text = unicodedata.normalize("NFKC", body).casefold().replace("\u2019", "'").replace("\u2018", "'")
    w = r"(?<![0-9a-z_-])" + name + r"(?![0-9a-z_-])"
    before = "|".join(re.escape(p) for p in _ATTRIB_BEFORE)
    after = "|".join(re.escape(p) for p in _ATTRIB_AFTER)
    possessed = "|".join(_POSSESSED)
    patterns = (
        rf"(?<![0-9a-z])(?:{before})\s+@?{w}",            # per txxxx / via @txxxx / thanks to txxxx
        rf"{w}\s+(?:{after})\b",                          # txxxx found / as txxxx said / txxxx measured
        rf"{w}'s\s+(?:{possessed})\b",                    # txxxx's finding / txxxx's numbers
    )
    return any(re.search(p, text) for p in patterns)


def _score(n_new: int, n_src: int, inter: int) -> tuple[float, float]:
    """(Jaccard, containment of the source) from set SIZES: |A or B| = |A| + |B| - |A and B|, no set built."""
    union = n_new + n_src - inter
    return (inter / union if union else 0.0), (inter / n_src if n_src else 0.0)


def find_echoes(window: Iterable[Message], sources: Iterable[Message]) -> tuple[list[Echo], int]:
    """(uncredited echoes, credited repeats seen). `window` are the messages judged; `sources` every message
    they may repeat (the window itself included, plus the lookback). Only an EARLIER message by ANOTHER
    author counts as a source; the earliest matching source per pair is not required, every match is reported."""
    srcs = sorted({m.key: m for m in sources}.values(), key=lambda m: (m.created_at, m.key))
    prepared = []
    index: dict[int, list[int]] = {}
    exact: dict[str, list[int]] = {}
    for i, m in enumerate(srcs):
        toks = tokens(m.body)
        sh = shingles(toks)
        prepared.append((toks, sh))
        for h in sh:
            index.setdefault(h, []).append(i)
        if len(toks) >= EXACT_MIN_TOKENS:
            exact.setdefault(" ".join(toks), []).append(i)
    pos = {m.key: i for i, m in enumerate(srcs)}
    out: list[Echo] = []
    credited = 0
    for m in sorted(window, key=lambda x: (x.created_at, x.key)):
        toks = tokens(m.body)
        sh = shingles(toks)
        hits: dict[int, str] = {}
        if len(toks) >= EXACT_MIN_TOKENS:
            for i in exact.get(" ".join(toks), ()):
                hits[i] = "exact"
        me = pos.get(m.key)

        def eligible(i: int) -> bool:
            """An EARLIER message by ANOTHER author: filtered before any scoring, so a heading shared by
            hundreds of messages costs a lookup each, not a set union each (Codex on #147: 16.9 s for 1000)."""
            src = srcs[i]
            if src.key == m.key or src.author == m.author:
                return False
            return (me is None or i < me) and (src.created_at, src.key) < (m.created_at, m.key)

        counts: dict[int, int] = {}
        if len(toks) >= NEAR_MIN_TOKENS:
            for h in sh:
                for i in index.get(h, ()):
                    if i in counts:
                        counts[i] += 1
                    elif eligible(i):
                        counts[i] = 1
        for i, inter in counts.items():
            if i in hits or len(prepared[i][0]) < NEAR_MIN_TOKENS:
                continue
            jac, cont = _score(len(sh), len(prepared[i][1]), inter)
            if jac >= NEAR_MIN_JACCARD or (cont >= CONTAIN_MIN and len(prepared[i][0]) >= CONTAIN_MIN_TOKENS):
                hits[i] = "near"
        for i, kind in sorted(hits.items()):
            s = srcs[i]
            if not eligible(i):
                continue
            ssh = prepared[i][1]
            inter = counts.get(i, len(sh & ssh))
            jac, cont = _score(len(sh), len(ssh), inter) if sh or ssh else (1.0, 1.0)
            if credits(m, s):
                credited += 1
                continue
            out.append(Echo(m.key, m.author, s.key, s.author, kind, round(jac, 3), round(cont, 3)))
    return out, credited


_FETCH_SQL = (
    "SELECT key, author_label, created_at, value->>'body', value->>'in_reply_to' FROM shared_memory "
    "WHERE namespace = 'shared/mesh-chat' AND is_head AND key LIKE 'msg/%%' AND created_at >= %s AND created_at < %s "
    "  AND value ? 'body' ORDER BY created_at, key"
)


def report(conn, *, since: _dt.datetime, until: _dt.datetime) -> dict:
    """The day's report: every message written in [since, until) judged against everything from since-LOOKBACK.
    `conn` inside server_internal (shared_memory under RLS). Keys, labels and scores only."""
    with conn.cursor() as cur:
        cur.execute(_FETCH_SQL, (since - LOOKBACK, until))
        rows = [Message(k, a or "", t, b or "", r) for k, a, t, b, r in cur.fetchall()]
    window = [m for m in rows if m.created_at >= since]
    echoes, credited = find_echoes(window, rows)
    return {"since": since.isoformat(), "until": until.isoformat(), "judged": len(window),
            "sources": len(rows), "credited_repeats": credited,
            "echoes": [dataclasses.asdict(e) for e in echoes]}
