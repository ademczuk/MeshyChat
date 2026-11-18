"""The wall between what a model SAYS and what the auditor RECORDS. Pure: no I/O.

THE FLEET'S MEASURED RULE (apps/board/derive.py, divergence 008): the model SELECTS, it
never PRODUCES. A produced value cannot be checked by a reader once it is published,
because nobody re-derives a thread id or a message key from prose, they read it. So the
model is shown candidates and asked to pick, and every pick passes TWO independent checks
before it can become an inferred edge:

  1. IT IS A FACT.   A recipient is a known peer label; a thread id already exists in the
                     DECLARED projection (mesh_edges); a reply target is an existing msg/
                     key written before the row that would answer it.
  2. IT WAS OFFERED. The value is one of the candidates this module put in front of the
                     model for THIS row. A real peer the prompt never mentioned is a
                     guess that happens to be spelled correctly.

Neither check implies the other, and that is on purpose, so each is load-bearing and each
has a test that goes red without it. Candidates come from two sources: facts about the
rows sent just before this one (their keys, authors and declared threads), which pass (1)
by construction, and ANCHORS the text itself contains (`@name`, `msg/...`, `thread: ...`),
which are offered UNVERIFIED because the text contains them. A body saying "@ghost" offers
`ghost`, and only check (1) refuses it. A model picking a real peer that neither the text
nor the context named passes (1), and only check (2) refuses it.

Every refusal has a NAMED reason and is counted, never dropped: "the model declined" and
"the model invented a thread" are different facts about the model, and only the histogram
tells a reader which one a sweep produced.

THE FACT SET IS DECLARED EDGES ONLY. A thread exists when mesh_edges names it, never when
mesh_inferred_edges does, so one inference can never become the evidence for the next.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import re
from typing import Protocol

#: The closed set. Mirrors the CHECK constraint in sql_meshychat/007. `authored_by` is the
#: authenticated label and is never missing; `copied_to` is not a thing prose settles; and
#: `owes_ack` must never be inferred, because an inferred ack debt would page a peer on a
#: model's word.
EDGE_TYPES = ("addressed_to", "in_thread", "replies_to")

#: A message is in ONE thread and answers ONE message. The partial unique index in
#: sql_meshychat/007 makes a second value unrepresentable; `parse_answer` refuses it first.
SINGLE_VALUED = frozenset({"in_thread", "replies_to"})

# ---------------------------------------------------------------- refusal reasons
ANSWER_NOT_STRICT_JSON = "answer_not_strict_json"
ANSWER_TRUNCATED = "answer_truncated"
#: The provider answered HTTP 200 with no `content` at all (a reasoning model that spent
#: its budget thinking). The row WAS asked; it is refused like an unparseable answer.
ANSWER_EMPTY = "answer_empty"
UNKNOWN_FIELD = "unknown_field"
ALREADY_DECLARED = "already_declared"
MALFORMED_VALUE = "malformed_value"
RECIPIENT_NOT_A_KNOWN_PEER = "recipient_not_a_known_peer"
RECIPIENT_IS_AUTHOR = "recipient_is_author"
RECIPIENT_NOT_OFFERED = "recipient_not_offered"
THREAD_NOT_IN_MESH_EDGES = "thread_not_in_mesh_edges"
THREAD_NOT_OFFERED = "thread_not_offered"
REPLY_TARGET_NOT_A_MSG_KEY = "reply_target_not_a_msg_key"
REPLY_TARGET_IS_SELF = "reply_target_is_self"
REPLY_TARGET_DOES_NOT_EXIST = "reply_target_does_not_exist"
REPLY_TARGET_NOT_EARLIER = "reply_target_not_earlier"
REPLY_TARGET_NOT_OFFERED = "reply_target_not_offered"

#: Bounds on what one prompt offers. The HQB chat model runs with n_ctx 2816 (measured
#: 2026-09-23 from its /v1/models), and a reasoning model spends much of that thinking, so
#: the candidate lists are capped rather than grown with the corpus.
MAX_RECIPIENTS = 16
MAX_THREADS = 12
MAX_REPLIES = 12
MAX_PROPOSED_RECIPIENTS = 8

MSG_KEY_RE = re.compile(r"(?<![\w/])(msg/[A-Za-z0-9][A-Za-z0-9_.:-]{0,200})")
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})")
#: `thread: lease-0921`, `thread_id=t-x`, `thread #t/9`. The token must carry a separator
#: or a digit so ordinary words after "thread" ("the thread is quiet") are not offered.
THREAD_ANCHOR_RE = re.compile(
    r"(?i)\bthread(?:_id)?\s*[:=#]?\s*([A-Za-z0-9][A-Za-z0-9_.:/-]{0,255})")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _trim(token: str) -> str:
    """A token as prose ends it: `txxxx.` and `msg/1-a,` lose the trailing punctuation."""
    return token.rstrip(".,;:!?)]}'\"")


@dataclasses.dataclass(frozen=True)
class Subject:
    """The row being audited, as the checks need it."""

    src_key: str
    created_at: _dt.datetime
    #: Every label the author is known by (author_label, and peers.name when it differs).
    author_labels: frozenset[str]
    #: The edge types this row does NOT declare, which are the only ones it may be asked.
    asked: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ContextMessage:
    """A row sent shortly before the subject: a FACT, offered as a candidate."""

    key: str
    author: str
    thread: str | None
    snippet: str


@dataclasses.dataclass(frozen=True)
class Candidates:
    recipients: tuple[str, ...]
    threads: tuple[str, ...]
    replies: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Proposal:
    edge_type: str
    dst: str


@dataclasses.dataclass(frozen=True)
class Refusal:
    edge_type: str | None
    value: str
    reason: str


@dataclasses.dataclass(frozen=True)
class Verdict:
    accepted: tuple[Proposal, ...]
    refusals: tuple[Refusal, ...]

    def refused(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.refusals:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out


class Facts(Protocol):
    """What the database says exists. store.DbFacts is the real one."""

    def is_known_label(self, label: str) -> bool: ...

    def thread_exists(self, thread_id: str) -> bool: ...

    def message_first_seen(self, key: str) -> _dt.datetime | None: ...


def _unique(values, limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
        if len(out) >= limit:
            break
    return tuple(out)


def text_anchors(body: str, known_labels: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """What the text itself names. NOT VERIFIED here: the fact check does that.

    Labels: every `@mention`, plus any word that is a known label (case-insensitively,
    mapped back to the label's own spelling, since a label is compared byte for byte with
    what senders wrote). Keys: every `msg/...`. Threads: the token after "thread".
    """
    text = body or ""
    by_lower: dict[str, list[str]] = {}
    for label in known_labels:
        by_lower.setdefault(label.lower(), []).append(label)
    labels: list[str] = [_trim(m) for m in MENTION_RE.findall(text)]
    for word in _WORD_RE.findall(text):
        labels.extend(sorted(by_lower.get(_trim(word).lower(), ())))
    keys = [_trim(m) for m in MSG_KEY_RE.findall(text)]
    threads = [t for t in (_trim(m) for m in THREAD_ANCHOR_RE.findall(text))
               if re.search(r"[0-9_.:/-]", t)]
    return {"labels": _unique(labels, MAX_RECIPIENTS), "keys": _unique(keys, MAX_REPLIES),
            "threads": _unique(threads, MAX_THREADS)}


def build_candidates(body: str, *, known_labels: frozenset[str],
                     context: list[ContextMessage],
                     threads_of_mentioned_keys: tuple[str, ...] = ()) -> Candidates:
    """The ONLY values a proposal may select, for one row.

    Text anchors first (what the sender wrote), then the context facts, newest context
    message first. Bounded, so a busy hour cannot push the prompt past the model's
    context window.
    """
    anchors = text_anchors(body, known_labels)
    newest_first = list(reversed(context))
    recipients = _unique(list(anchors["labels"]) + [c.author for c in newest_first],
                         MAX_RECIPIENTS)
    threads = _unique(list(anchors["threads"]) + list(threads_of_mentioned_keys)
                      + [c.thread for c in newest_first if c.thread], MAX_THREADS)
    replies = _unique(list(anchors["keys"]) + [c.key for c in newest_first], MAX_REPLIES)
    return Candidates(recipients=recipients, threads=threads, replies=replies)


# --------------------------------------------------------------------- the answer

class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs):
    seen: dict = {}
    for k, v in pairs:
        if k in seen:
            raise _DuplicateKey(k)
        seen[k] = v
    return seen


_FENCE_RE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```\Z", re.S)


def strict_json_object(text) -> dict | None:
    """The answer as ONE JSON object, or None.

    STRICT, unlike apps/board/derive.py's first-object scan: the whole reply must be the
    object, optionally inside a single ``` fence (the wrapping a chat model adds however
    it is asked). Prose before or after, two objects, or a key given twice is not an
    answer to parse around; it is refused, because a parser that digs an object out of a
    paragraph also digs one out of a thought the model abandoned.
    """
    if not isinstance(text, str):
        return None
    body = text.strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        obj = json.loads(body, object_pairs_hook=_no_duplicates)
    except (ValueError, _DuplicateKey):
        return None
    return obj if isinstance(obj, dict) else None


def parse_answer(text, subject: Subject, *, finish_reason: str | None = None
                 ) -> tuple[list[Proposal], list[Refusal]]:
    """The model's reply as proposals, and everything unusable in it as refusals.

    A reply cut off by the token limit is refused WHOLE even when it parses: divergence
    008 measured that a truncated generation's JSON is a thought mid-flight, and the
    select-only rule cannot tell a considered pick from an abandoned one.
    """
    if finish_reason == "length":
        return [], [Refusal(None, "", ANSWER_TRUNCATED)]
    obj = strict_json_object(text)
    if obj is None:
        return [], [Refusal(None, str(text)[:80], ANSWER_NOT_STRICT_JSON)]
    proposals: list[Proposal] = []
    refusals: list[Refusal] = []
    for field, raw in obj.items():
        if field not in EDGE_TYPES:
            refusals.append(Refusal(None, str(field)[:80], UNKNOWN_FIELD))
            continue
        if field not in subject.asked:
            # The row DECLARES this relation. An inference never sits beside a
            # declaration, let alone overrides it; a null answer for it is harmless.
            if raw not in (None, [], ""):
                refusals.append(Refusal(field, str(raw)[:80], ALREADY_DECLARED))
            continue
        if raw is None:
            continue
        if field == "addressed_to":
            if not isinstance(raw, list):
                refusals.append(Refusal(field, str(raw)[:80], MALFORMED_VALUE))
                continue
            picked: list[str] = []
            for item in raw:
                if not isinstance(item, str) or not item.strip() or item != item.strip():
                    refusals.append(Refusal(field, str(item)[:80], MALFORMED_VALUE))
                    continue
                if item not in picked:
                    picked.append(item)
            for item in picked[MAX_PROPOSED_RECIPIENTS:]:
                refusals.append(Refusal(field, item[:80], MALFORMED_VALUE))
            proposals.extend(Proposal(field, item)
                             for item in picked[:MAX_PROPOSED_RECIPIENTS])
            continue
        if field in SINGLE_VALUED:
            # in_thread / replies_to: ONE string. A list is two threads for one message.
            if not isinstance(raw, str) or not raw.strip() or raw != raw.strip():
                refusals.append(Refusal(field, str(raw)[:80], MALFORMED_VALUE))
                continue
            proposals.append(Proposal(field, raw))
    return proposals, refusals


# ---------------------------------------------------------------------- the checks

def check(proposal: Proposal, subject: Subject, candidates: Candidates,
          facts: Facts) -> str | None:
    """None when the proposal may be recorded, else the refusal reason.

    ORDER: the fact check first, then whether it was offered. Both run for every edge
    type; neither is skipped because the other would have caught the value.
    """
    t, v = proposal.edge_type, proposal.dst
    if t == "addressed_to":
        if not facts.is_known_label(v):
            return RECIPIENT_NOT_A_KNOWN_PEER
        if v in subject.author_labels:
            return RECIPIENT_IS_AUTHOR
        if v not in candidates.recipients:
            return RECIPIENT_NOT_OFFERED
        return None
    if t == "in_thread":
        if not facts.thread_exists(v):
            return THREAD_NOT_IN_MESH_EDGES
        if v not in candidates.threads:
            return THREAD_NOT_OFFERED
        return None
    if t == "replies_to":
        if not v.startswith("msg/"):
            return REPLY_TARGET_NOT_A_MSG_KEY
        if v == subject.src_key:
            return REPLY_TARGET_IS_SELF
        first_seen = facts.message_first_seen(v)
        if first_seen is None:
            return REPLY_TARGET_DOES_NOT_EXIST
        if first_seen >= subject.created_at:
            # A reply is written after reading what it answers, so the answered row's
            # created_at (its writer's transaction start) is strictly earlier.
            return REPLY_TARGET_NOT_EARLIER
        if v not in candidates.replies:
            return REPLY_TARGET_NOT_OFFERED
        return None
    return UNKNOWN_FIELD


def validate(proposals: list[Proposal], subject: Subject, candidates: Candidates,
             facts: Facts, *, prior_refusals: list[Refusal] = ()) -> Verdict:
    accepted: list[Proposal] = []
    refusals: list[Refusal] = list(prior_refusals)
    for p in proposals:
        reason = check(p, subject, candidates, facts)
        if reason is None:
            accepted.append(p)
        else:
            refusals.append(Refusal(p.edge_type, p.dst[:80], reason))
    return Verdict(accepted=tuple(accepted), refusals=tuple(refusals))


__all__ = ["ALREADY_DECLARED", "ANSWER_EMPTY", "ANSWER_NOT_STRICT_JSON", "ANSWER_TRUNCATED",
           "Candidates",
           "ContextMessage", "EDGE_TYPES", "Facts", "MALFORMED_VALUE", "Proposal",
           "RECIPIENT_IS_AUTHOR", "RECIPIENT_NOT_A_KNOWN_PEER", "RECIPIENT_NOT_OFFERED",
           "REPLY_TARGET_DOES_NOT_EXIST", "REPLY_TARGET_IS_SELF", "REPLY_TARGET_NOT_A_MSG_KEY",
           "REPLY_TARGET_NOT_EARLIER", "REPLY_TARGET_NOT_OFFERED", "Refusal", "SINGLE_VALUED",
           "Subject", "THREAD_NOT_IN_MESH_EDGES", "THREAD_NOT_OFFERED", "UNKNOWN_FIELD",
           "Verdict", "build_candidates", "check", "parse_answer", "strict_json_object",
           "text_anchors", "validate"]
