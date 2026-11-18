"""The wall between what a model WRITES and what a node is shown. Pure: no I/O.

Divergence 037; the rules are docs/design/thread-summaries.md section 4.2 and 4.3.

A summary is the one place in this project where a model PRODUCES text, against the rule
that "the model SELECTS, it never PRODUCES" (docs/DIVERGENCES.md 021). So everything a
summary says that CAN be checked mechanically is checked here, and the rest is labelled
unverified by the server, never by the model:

  * citations are SELECTED: the model cites numbers 1..N from the prompt, and the server
    maps them to keys, so a citation is in scope by construction (4.1);
  * every identifier-like token in a sentence must occur in the messages it cites;
  * the sentence's subject, when it opens "<label> said/decided/...", must be the
    authenticated author of a message it cites (`misattributed`, critique C2);
  * every sentence OPENS with its speaker, as reported speech (`unattributed`, added by
    the adversarial review: without it, a sentence naming no peer passed every rule);
  * nothing aimed at the reader, nothing hidden, no look-alike label, no leaked prompt.

What these rules cannot establish is that a sentence MEANS what its sources mean (design
3.3's last two rows). That is measured (design section 7), never claimed.

TOTAL (E1): `validate` and `settle` return a Verdict for any input, and never an empty
default in place of a refusal. Every refusal is NAMED and counted.

ORDER. Whole-answer refusals first (empty, cut off, not the schema, too many sentences,
a leaked prompt: checked over EVERY sentence, dropped or not), then the per-sentence
rules in SENTENCE_RULES order, the first failure naming the drop. `source_changed` (rule
10) and the more-than-half rule are applied by `settle`, per request, because whether a
cited row changed during the model call is a fact about the request, not the answer.
"""
from __future__ import annotations

import dataclasses
import re
import unicodedata

from apps.meshaudit.validate import strict_json_object

# ---------------------------------------------------------------- sentence rules (4.2)
CITES_EMPTY = "cites_empty"
CITE_OUT_OF_RANGE = "cite_out_of_range"
TOO_MANY_CITES = "too_many_cites"
TOO_LONG = "too_long"
HIDDEN_CHARACTERS = "hidden_characters"
CONTROL_CHARACTERS = "control_characters"
MIXED_SCRIPT = "mixed_script"
ADDRESSES_READER = "addresses_reader"
IDENTIFIER_NOT_IN_CITED = "identifier_not_in_cited"
MISATTRIBUTED = "misattributed"
UNATTRIBUTED = "unattributed"
SOURCE_CHANGED = "source_changed"

#: In the order they run. Part of `summary_version` (apps/meshsum/prompt.py).
SENTENCE_RULES = (CITES_EMPTY, CITE_OUT_OF_RANGE, TOO_MANY_CITES, TOO_LONG,
                  HIDDEN_CHARACTERS, CONTROL_CHARACTERS, MIXED_SCRIPT, ADDRESSES_READER,
                  IDENTIFIER_NOT_IN_CITED, MISATTRIBUTED, UNATTRIBUTED, SOURCE_CHANGED)

# ---------------------------------------------------------- whole-answer refusals (4.3)
ANSWER_EMPTY = "answer_empty"
ANSWER_TRUNCATED = "answer_truncated"
ANSWER_NOT_STRICT_JSON = "answer_not_strict_json"
TOO_MANY_SENTENCES = "too_many_sentences"
PROMPT_LEAK = "prompt_leak"
MOSTLY_DROPPED = "mostly_dropped"

ANSWER_RULES = (ANSWER_EMPTY, ANSWER_TRUNCATED, ANSWER_NOT_STRICT_JSON,
                TOO_MANY_SENTENCES, PROMPT_LEAK, MOSTLY_DROPPED)

#: Bounds the caller cannot change (design 1.1).
MAX_SENTENCES = 8
MAX_SENTENCE_CHARS = 240
MAX_CITES = 4
#: A run of this many words shared with the system message, and supplied by no covered
#: body, is a leaked prompt (3.3).
LEAK_RUN_WORDS = 8

# ------------------------------------------------------------------- hidden characters

#: Beyond general category Cf (format), two blocks a person does not see and a model reads:
#: TAG characters, which spell ASCII (ASCII smuggling), and the variation selectors.
HIDDEN_RANGES = ((0xE0000, 0xE007F), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


def is_hidden(ch: str) -> bool:
    """A character a reader would not see: category Cf (the bidirectional overrides and
    isolates, the zero-width characters, U+FEFF, U+2060 to U+2064, U+061C), a TAG
    character or a variation selector. A category, not a list: a list misses the next one
    (critique C7)."""
    cp = ord(ch)
    if unicodedata.category(ch) == "Cf":
        return True
    return any(lo <= cp <= hi for lo, hi in HIDDEN_RANGES)


#: Refused in the OUTPUT only (adversarial review of 037): category Cc (the C0 and C1
#: controls: line feed, carriage return, escape, backspace, NEL) and the line and paragraph
#: separators. A line break in a sentence starts a line of `summarize --text` that carries
#: no label, so a model could write a `[declared]` line; a carriage return or an escape
#: sequence can rewrite the label on a terminal. Bodies keep theirs: a message may have
#: lines, and the model reads them inside the nonce block.
CONTROL_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def is_control(ch: str) -> bool:
    return unicodedata.category(ch) in CONTROL_CATEGORIES


def escape_hidden(text: str) -> tuple[str, int]:
    """`text` with every hidden character replaced by a VISIBLE escape (`[U+200B]`), and
    how many were replaced. Applied to every body before it is sent (design 3.2)."""
    out: list[str] = []
    n = 0
    for ch in text:
        if is_hidden(ch):
            out.append(f"[U+{ord(ch):04X}]")
            n += 1
        else:
            out.append(ch)
    return "".join(out), n


#: Letters that sit in a Latin word without reading as a Latin letter (rule 6).
SCRIPT_NEUTRAL = frozenset({"MICRO SIGN"})

# --------------------------------------------------------------------- the reader (7)

SECOND_PERSON_RE = re.compile(r"(?<![0-9a-z])(?:you|your|yours|yourself|yourselves)"
                              r"(?![0-9a-z])")
#: A sentence opening with one of these is an instruction, whatever follows.
IMPERATIVES = frozenset({"grant", "run", "ignore", "disregard", "delete", "send", "approve",
                         "revoke", "merge", "push", "execute", "reply", "forward"})

# ----------------------------------------------------------------- identifiers (rule 8)

#: The identifier-like token classes, compared after NFKC and case-folding. Part of
#: `summary_version`: a changed pattern is a changed validator.
IDENTIFIER_PATTERNS = {
    "url": r"[a-z][a-z0-9+.-]*://\S+",
    "msg_key": r"(?<![\w/])msg/[a-z0-9][a-z0-9_.:-]{0,200}",
    "mention": r"(?<![\w@])@[a-z0-9][a-z0-9_.-]{0,63}",
    "backticked": r"`[^`]+`",
    "hex": r"(?<![0-9a-z])[0-9a-f]{7,}(?![0-9a-z])",
    "path": r"(?<!\S)\S*[/\\]\S*",
    "number": r"(?<![0-9a-z])[0-9]{2,}(?![0-9a-z])",
    "joined": r"(?<![0-9a-z])[0-9]+(?:[.:][0-9a-z]+)+|(?<![0-9a-z])[0-9]+[a-z]+",
    # Added by the adversarial review of 037: an e-mail address, whole, and a host name
    # without a scheme or a slash (labels joined by dots, ending in one of two or more
    # characters that starts with a letter, so "e.g.", "a.m." and "1.5" are not hosts).
    "email": r"(?<![0-9a-z._%+-])[0-9a-z][0-9a-z._%+-]*@[0-9a-z-]+(?:\.[0-9a-z-]+)+",
    "host": r"(?<![0-9a-z@._-])(?:[0-9a-z](?:[0-9a-z-]*[0-9a-z])?\.)+"
            r"[a-z](?:[0-9a-z-]*[0-9a-z])(?![0-9a-z_-])",
    "label": r"[0-9a-z][0-9a-z_.-]*",
    "image": r"!\[",
}
_RE = {name: re.compile(p) for name, p in IDENTIFIER_PATTERNS.items()}
_TRAILING = ".,;:!?)]}'\"\u2019"

# ---------------------------------------------------------------- attribution (rule 9)

#: A reporting verb within this many words after the sentence's first known label makes
#: that label the sentence's subject.
ATTRIBUTION_WINDOW = 3
#: Rule 9 reads the subject by these; rule 9b requires one of them after the opening
#: label. A verb missing here costs a faithful sentence (a drop, counted), never lets a
#: false one through: the list errs toward refusing. Widened by the adversarial review
#: with the verbs of reported speech and action a summary uses ("stated", "noted",
#: "replied", "linked" ...; "stated" is gemma-4-26b-a4b's, measured on synthetic text).
REPORTING_VERBS = frozenset({
    "said", "says", "say", "asked", "asks", "ask", "wrote", "writes", "write", "written",
    "decided", "decides", "decide", "approved", "approves", "approve", "agreed", "agrees",
    "agree", "confirmed", "confirms", "confirm", "refused", "refuses", "refuse",
    "reported", "reports", "report", "proposed", "proposes", "propose", "requested",
    "requests", "request", "announced", "announces", "announce",
    "stated", "states", "state", "noted", "notes", "note", "mentioned", "mentions",
    "mention", "replied", "replies", "reply", "answered", "answers", "answer",
    "responded", "responds", "respond", "added", "adds", "add", "explained", "explains",
    "explain", "suggested", "suggests", "suggest", "recommended", "recommends",
    "recommend", "warned", "warns", "warn", "objected", "objects", "object", "claimed",
    "claims", "claim", "argued", "argues", "argue", "offered", "offers", "offer",
    "promised", "promises", "promise", "acknowledged", "acknowledges", "acknowledge",
    "acked", "acks", "ack", "accepted", "accepts", "accept", "rejected", "rejects",
    "reject", "declined", "declines", "decline", "insisted", "insists", "insist",
    "clarified", "clarifies", "clarify", "reminded", "reminds", "remind", "thanked",
    "thanks", "thank", "flagged", "flags", "flag", "raised", "raises", "raise",
    "questioned", "questions", "question", "wondered", "wonders", "wonder", "shared",
    "shares", "share", "posted", "posts", "post", "linked", "links", "link", "copied",
    "copies", "copy", "cited", "cites", "cite", "quoted", "quotes", "quote",
    "described", "describes", "describe", "estimated", "estimates", "estimate",
    "disagreed", "disagrees", "disagree", "told", "tells", "tell",
    "informed", "informs", "inform", "volunteered", "volunteers", "volunteer",
    "planned", "plans", "plan", "repeated", "repeats", "repeat", "corrected", "corrects",
    "correct", "followed", "follows", "follow"})
_WORD_RE = re.compile(r"[0-9a-z@][0-9a-z_.@'\u2019-]*")


@dataclasses.dataclass(frozen=True)
class Source:
    """One covered message, exactly as the model was shown it (the body after the cut and
    the escapes). `n` is its number in the prompt; `author` the AUTHENTICATED label the
    header carried, `aliases` every label that peer is known by."""

    n: int
    author: str
    aliases: frozenset
    body: str
    to: tuple
    cc: tuple
    thread: str | None
    at: str


@dataclasses.dataclass(frozen=True)
class Context:
    sources: tuple            # of Source, numbered 1..N in order
    #: Known peer labels, lower-cased: the covered authors' and addressees' labels and
    #: every peers.name (design 4.2 rule 8).
    known_labels: frozenset
    system_text: str
    canary: str
    nonce: str


@dataclasses.dataclass(frozen=True)
class Sentence:
    position: int             # 1-based, in the model's order
    text: str
    cites: tuple              # of int, the model's numbers


@dataclasses.dataclass(frozen=True)
class Dropped:
    """A refused sentence: its position, the rule and its citations. NEVER its text: it
    failed validation, and text carrying an injected instruction is what fails."""

    position: int
    reason: str
    cites: tuple


@dataclasses.dataclass(frozen=True)
class Verdict:
    kept: tuple = ()
    dropped: tuple = ()
    #: A whole-answer refusal (ANSWER_RULES), or None.
    refused: str | None = None
    #: How many sentences the answer had, dropped included.
    total: int = 0

    def histogram(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.dropped:
            out[d.reason] = out.get(d.reason, 0) + 1
        return out


def fold(text: str) -> str:
    """NFKC, then case-folded: how every comparison here sees text."""
    return unicodedata.normalize("NFKC", text).casefold()


def _words(text: str) -> list[str]:
    return re.findall(r"[0-9a-z]+", fold(text))


def _grams(words: list[str], n: int = LEAK_RUN_WORDS) -> set[tuple]:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _trim(tok: str) -> str:
    return tok.rstrip(_TRAILING)


def _label_token(tok: str) -> str:
    tok = _trim(tok.lstrip("@"))
    for suffix in ("'s", "\u2019s"):
        if tok.endswith(suffix):
            tok = tok[: -len(suffix)]
    return _trim(tok)


# ------------------------------------------------------------------------ the answer

def _strict_object(text: str) -> dict | None:
    """The whole reply as ONE JSON object, one ``` fence tolerated: 021's rule, read by
    021's own function (apps/meshaudit/validate.strict_json_object): prose around it, two
    objects or a key given twice is refused, never dug out. The one catch of this module's
    own: nesting past the interpreter's limit is RecursionError from json.loads, not
    ValueError (apps/memoryskills/views._json_body), and `validate` is TOTAL (E1), so such
    an answer is ANSWER_NOT_STRICT_JSON, never a 500."""
    try:
        return strict_json_object(text)
    except RecursionError:
        return None


def _parse(text) -> tuple[list | None, str | None]:
    """(raw sentences, None) or (None, whole-answer refusal)."""
    obj = _strict_object(text)
    if obj is None or set(obj) != {"sentences"} or not isinstance(obj["sentences"], list):
        return None, ANSWER_NOT_STRICT_JSON
    items = obj["sentences"]
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"text", "cites"}
                or not isinstance(item["text"], str) or not item["text"].strip()
                or not isinstance(item["cites"], list)):
            return None, ANSWER_NOT_STRICT_JSON
    if len(items) > MAX_SENTENCES:
        return None, TOO_MANY_SENTENCES
    return items, None


def _leaks(texts: list[str], ctx: Context) -> bool:
    """The canary or the nonce anywhere, or LEAK_RUN_WORDS consecutive words of the
    system message that no covered body contains (critique C6: the system message is in
    the repository, so a body quoting it is the body's words)."""
    secrets = [fold(s) for s in (ctx.canary, ctx.nonce) if s]
    system = _grams(_words(ctx.system_text))
    supplied: set = set()
    for s in ctx.sources:
        supplied |= _grams(_words(s.body))
    leaked = system - supplied
    for t in texts:
        ft = fold(t)
        if any(sec in ft for sec in secrets):
            return True
        if _grams(_words(t)) & leaked:
            return True
    return False


# ----------------------------------------------------------------- per-sentence rules

def _cites(raw: list, n: int) -> tuple[tuple | None, str | None]:
    if not raw:
        return None, CITES_EMPTY
    out: list[int] = []
    for c in raw:
        if not isinstance(c, int) or isinstance(c, bool) or not 1 <= c <= n or c in out:
            return None, CITE_OUT_OF_RANGE
        out.append(c)
    if len(out) > MAX_CITES:
        return None, TOO_MANY_CITES
    return tuple(out), None


def _script(ch: str) -> str | None:
    """The script a letter reads as: LATIN when its Unicode name says LATIN (fullwidth
    Latin included), else the name's first word (CYRILLIC, GREEK, ARMENIAN, LISU,
    MATHEMATICAL, CJK ...). None for a non-letter or a SCRIPT_NEUTRAL letter."""
    if not ch.isalpha():
        return None
    name = unicodedata.name(ch, "")
    if name in SCRIPT_NEUTRAL:
        return None
    if "LATIN" in name.split():
        return "LATIN"
    return name.split(" ", 1)[0] or "UNNAMED"


def _mixed_script(text: str) -> bool:
    """A word with Latin letters and a letter of ANY other script (adversarial review of
    037: the rule named Cyrillic and Greek, and an Armenian oh made a look-alike label).
    A category, not a list of scripts; the one list is of letters that are not a
    look-alike, and a letter missing from it costs a drop, never a pass."""
    for word in re.findall(r"\w+", text):
        scripts = {s for s in map(_script, word) if s is not None}
        if "LATIN" in scripts and len(scripts) > 1:
            return True
    return False


def _addresses_reader(text: str) -> bool:
    ft = fold(text)
    if SECOND_PERSON_RE.search(ft):
        return True
    first = re.match(r"[^0-9a-z]*([0-9a-z]+)", ft)
    return bool(first and first.group(1) in IMPERATIVES)


def _haystack(cited: list[Source]) -> str:
    parts: list[str] = []
    for s in cited:
        parts.extend([s.body, s.author, *sorted(s.aliases), *s.to, *s.cc, s.thread or "",
                      s.at])
    return fold("\n".join(parts))


def _present(tok: str, hay: str) -> bool:
    if not tok:
        return True
    left = r"(?<![0-9a-z])" if tok[0].isalnum() else ""
    right = r"(?![0-9a-z])" if tok[-1].isalnum() else ""
    return re.search(left + re.escape(tok) + right, hay) is not None


def identifiers(text: str, known_labels: frozenset) -> list[tuple[str, str]]:
    """[(class, token)] of every identifier-like token in `text`, folded. A URL is ONE
    token from its scheme to the next whitespace, compared whole."""
    ft = fold(text)
    out: list[tuple[str, str]] = []
    urls = [_trim(u) for u in _RE["url"].findall(ft)]
    out.extend(("url", u) for u in urls)
    rest = _RE["url"].sub(" ", ft)
    for cls in ("msg_key", "mention", "backticked", "hex", "path", "number", "joined",
                "email", "host"):
        for m in _RE[cls].findall(rest):
            tok = m[1:-1].strip() if cls == "backticked" else _trim(m)
            if cls == "mention":
                tok = tok[1:]
            if tok:
                out.append((cls, tok))
    for m in _RE["label"].findall(rest):
        tok = _label_token(m)
        if tok in known_labels:
            out.append(("label", tok))
    return out


def _identifier_missing(text: str, cited: list[Source], known_labels: frozenset) -> bool:
    if _RE["image"].search(text):
        return True                       # markdown image syntax: refused outright
    hay = _haystack(cited)
    hay_urls = {_trim(u) for u in _RE["url"].findall(hay)}
    for cls, tok in identifiers(text, known_labels):
        if cls == "url":
            if tok not in hay_urls:
                return True
        elif not _present(tok, hay):
            return True
    return False


def _misattributed(text: str, cited: list[Source], known_labels: frozenset) -> bool:
    words = [_label_token(w) for w in _WORD_RE.findall(fold(text))]
    for i, w in enumerate(words):
        if w in known_labels:
            following = words[i + 1:i + 1 + ATTRIBUTION_WINDOW]
            if not any(f in REPORTING_VERBS for f in following):
                return False
            return not any(w == fold(s.author) or w in {fold(a) for a in s.aliases}
                           for s in cited)
    return False


def _unattributed(text: str, known_labels: frozenset) -> bool:
    """Rule 9b (adversarial review of 037): the sentence OPENS with its speaker, a known
    label followed within ATTRIBUTION_WINDOW words by a reporting verb ("otter asked
    ...", "@heron said ...", "heron and otter agreed ..."). Rule 9 has then already held
    that label, the sentence's first, to a cited author. A sentence in no one's voice
    ("The reader should ...", "Please run ...", "The server verified ...", "The backfill
    runs first") is refused: it is exactly what the prompt forbids, and the one form in
    which a planted instruction or claim reaches a node without the name of its writer. A
    pattern, not a parser: a second clause after an attributed opening is not re-read."""
    words = [_label_token(w) for w in _WORD_RE.findall(fold(text))]
    if not words or words[0] not in known_labels:
        return True
    return not any(w in REPORTING_VERBS for w in words[1:1 + ATTRIBUTION_WINDOW])


def _check(text: str, cites: tuple, ctx: Context) -> str | None:
    """Rules 4 to 9b for one sentence whose citations passed rules 1 to 3."""
    if len(text) > MAX_SENTENCE_CHARS:
        return TOO_LONG
    if any(is_hidden(ch) for ch in text):
        return HIDDEN_CHARACTERS
    if any(is_control(ch) for ch in text):
        return CONTROL_CHARACTERS
    if _mixed_script(text):
        return MIXED_SCRIPT
    if _addresses_reader(text):
        return ADDRESSES_READER
    by_n = {s.n: s for s in ctx.sources}
    cited = [by_n[c] for c in cites]
    if _identifier_missing(text, cited, ctx.known_labels):
        return IDENTIFIER_NOT_IN_CITED
    if _misattributed(text, cited, ctx.known_labels):
        return MISATTRIBUTED
    if _unattributed(text, ctx.known_labels):
        return UNATTRIBUTED
    return None


def validate(text, finish_reason, ctx: Context) -> Verdict:
    """The model's answer, checked: kept sentences, dropped ones (no text), or a
    whole-answer refusal. Rules 1 to 9; `settle` applies rule 10 and the half rule."""
    if not isinstance(text, str) or not text.strip():
        return Verdict(refused=ANSWER_EMPTY)
    if finish_reason != "stop":
        # A cut answer is refused even when it parses (the project's design log (not part of this release) 021): its JSON
        # is a thought mid-flight.
        return Verdict(refused=ANSWER_TRUNCATED)
    items, refused = _parse(text)
    if refused is not None:
        return Verdict(refused=refused)
    if _leaks([i["text"] for i in items], ctx):
        return Verdict(refused=PROMPT_LEAK, total=len(items))
    kept: list[Sentence] = []
    dropped: list[Dropped] = []
    n = len(ctx.sources)
    for pos, item in enumerate(items, start=1):
        raw_cites = item["cites"]
        cites, reason = _cites(raw_cites, n)
        shown = tuple(c for c in raw_cites if isinstance(c, int) and not isinstance(c, bool)
                      and 1 <= c <= n)
        if reason is None:
            reason = _check(item["text"].strip(), cites, ctx)
        if reason is None:
            kept.append(Sentence(pos, item["text"].strip(), cites))
        else:
            dropped.append(Dropped(pos, reason, cites if cites is not None else shown))
    return Verdict(kept=tuple(kept), dropped=tuple(dropped), total=len(items))


def settle(verdict: Verdict, changed) -> Verdict:
    """Rule 10 for THIS request (a sentence citing a row that changed during the model
    call is dropped as `source_changed`), then the more-than-half rule over every drop."""
    if verdict.refused is not None:
        return verdict
    changed = set(changed or ())
    kept: list[Sentence] = []
    dropped = list(verdict.dropped)
    for s in verdict.kept:
        if changed & set(s.cites):
            dropped.append(Dropped(s.position, SOURCE_CHANGED, s.cites))
        else:
            kept.append(s)
    dropped.sort(key=lambda d: d.position)
    if verdict.total and 2 * len(dropped) > verdict.total:
        return Verdict(dropped=tuple(dropped), refused=MOSTLY_DROPPED, total=verdict.total)
    return Verdict(kept=tuple(kept), dropped=tuple(dropped), total=verdict.total)


__all__ = ["ADDRESSES_READER", "ANSWER_EMPTY", "ANSWER_NOT_STRICT_JSON", "ANSWER_RULES",
           "ANSWER_TRUNCATED", "CITES_EMPTY", "CITE_OUT_OF_RANGE", "CONTROL_CHARACTERS",
           "Context", "Dropped",
           "HIDDEN_CHARACTERS", "IDENTIFIER_NOT_IN_CITED", "IDENTIFIER_PATTERNS",
           "MAX_CITES", "MAX_SENTENCES", "MAX_SENTENCE_CHARS", "MISATTRIBUTED",
           "MIXED_SCRIPT", "MOSTLY_DROPPED", "PROMPT_LEAK", "SENTENCE_RULES",
           "SOURCE_CHANGED", "Sentence", "Source", "TOO_LONG", "TOO_MANY_CITES",
           "TOO_MANY_SENTENCES", "UNATTRIBUTED", "Verdict", "escape_hidden", "fold",
           "identifiers",
           "is_control", "is_hidden", "settle", "validate"]
