"""PROPOSE acts from prose that is already on the mesh, without ever declaring one.

THE PROBLEM THIS IS FOR. Measured on the 300 most recent production rows (2026-09-21, 28
hours, 10 authors): 3 carried a declared act and 208 carried an anchor a reader could have
pointed at. `declare.py` makes the correct path cheap for the NEXT message; it does nothing
for the ones already sent, and it cannot make a seat that never opts in declare anything.
So this reads what is there.

THE ONE RULE THAT MAKES THIS SAFE, and it is why the module is separate from `declare.py`:
A PROPOSAL IS NOT A DECLARATION. Everything here produces `inferred.<verb>` acts, which
`projection.py` keys separately, renders as "INFERRED, not declared", and excludes from
`SubjectState.has()`. An inferred review never satisfies `has("reviewed")`. That distinction
is the whole board: "uxxx is holding" and "the fix is unopened" read the same from the
outside, and so do "somebody reviewed this" and "a model thinks somebody reviewed this".

THE SECOND RULE, which removes the worst failure mode available to a language model here:
A PROPOSAL MAY ONLY SELECT AMONG ANCHORS THE SOURCE TEXT ACTUALLY CONTAINS. `ref` is chosen
from shas extracted by regex from the body, never generated. A fabricated 40-character sha
is invisible once published, because nobody re-derives forty characters from a message, they
read it. So the model is never asked to produce one; it is asked to pick, and a pick that is
not in the extracted set is refused here rather than trusted.

TWO PASSES, CHEAPEST FIRST.

  `propose(body)`      deterministic. Requires exactly one subject, one sha and one verb,
                       all in the FIRST LINE. Refuses on ambiguity rather than picking, so
                       its proposals need no model and no GPU.
  `propose_with_model` the rows the first pass refuses. Gated on `apps.modelhost` reporting
                       a WORKING generate provider (evidence of function, not a 200), and
                       constrained to select from the same extracted anchors.

The second pass exists because the first one is deliberately narrow, and the measurement
that justifies building it at all is in `docs/DIVERGENCES.md` 008: how many rows carry
anchors but fail the single-candidate test.
"""
from __future__ import annotations

import dataclasses
import json
import re

#: Closed vocabulary. A verb outside it is not proposed at all, because the board's verbs
#: are read by people deciding what to do next and an open vocabulary makes two rows that
#: mean the same thing look like different states. Surface form -> canonical verb.
VERB_FORMS: dict[str, str] = {
    "reviewed": "reviewed", "review": "reviewed", "reviewing": "reviewed",
    "merged": "merged", "merge": "merged",
    "landed": "landed",
    "pushed": "pushed",
    "rebased": "rebased",
    "tested": "tested",
    "measured": "measured",
    "verified": "verified",
    "confirmed": "confirmed",
    "refuted": "refuted",
    "retracted": "retracted",
    "corrected": "corrected",
    "held": "held", "holding": "held",
    "blocked": "blocked",
    "closed": "closed",
    "opened": "opened",
    "fixed": "fixed",
    "shipped": "shipped",
    "committed": "committed",
}

#: A commit sha: hex, 7 to 40, and REQUIRING at least one letter. Without the letter
#: requirement a 13-digit epoch-ms mesh key matches and every message proposes its own key
#: as a version. Measured: that alone moved the sha count on 300 rows from 207 to 175.
SHA_RE = re.compile(r"(?<![\w/])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w])")

#: `PR 851`, `pr/851`, `pull 851`, `#851`, `issue 12`. Deliberately narrow: pr and issue
#: are the two objects this fleet names constantly and can be normalised without guessing.
PR_RE = re.compile(r"(?i)\b(?:pr|pull(?:\s*request)?)\s*[#/ -]?\s*(\d{2,6})\b")
ISSUE_RE = re.compile(r"(?i)\bissues?\s*[#/ -]?\s*(\d{2,6})\b")
HASH_NUM_RE = re.compile(r"(?<![\w])#(\d{2,6})\b")

#: Prose that names a subject without acting on it. A body saying "this is NOT a review of
#: pr/851" must not propose `reviewed`. Cheap, lexical, and deliberately blunt: the cost of
#: a false negative here is a proposal nobody makes, and the cost of a false positive is a
#: wrong state on the board.
NEGATION_RE = re.compile(
    r"(?i)\b(?:not|never|cannot|can't|won't|refus\w*|declin\w*|without|un(?:reviewed|merged"
    r"|tested|verified))\b")

#: Language that makes a sentence a QUESTION, a REQUEST or an INSTRUCTION rather than a
#: report of a completed act.
#:
#: The instruction half was added after reading all seven proposals this made on the real
#: corpus rather than counting them. One was `ACTION is "check whether #936 has two
#: approves at head <sha> and merge it"`, proposed as a MERGE that had not happened. An
#: instruction to do a thing and a report of having done it carry identical anchors, and
#: only the framing separates them, so the framing is what is matched.
INTENT_RE = re.compile(
    r"(?i)(\?|\b(?:should|could|would|will|plan|planning|going to|about to|please|can you"
    r"|shall|propose|proposing|suggest\w*|action is|check whether|check that|make sure"
    r"|ensure|todo|next step|needs? to|has to|waiting (?:on|for)|once you|when you)\b)")


@dataclasses.dataclass(frozen=True)
class Anchors:
    """What the text actually contains. The ONLY legal values a proposal may select."""

    subjects: tuple[str, ...]
    refs: tuple[str, ...]
    verbs: tuple[str, ...]

    @property
    def unambiguous(self) -> bool:
        return len(self.subjects) == 1 and len(self.refs) == 1 and len(self.verbs) == 1

    @property
    def empty(self) -> bool:
        return not (self.subjects or self.refs or self.verbs)


@dataclasses.dataclass(frozen=True)
class Proposal:
    """One inferred act, with the evidence it was read from.

    `source_key` is the mesh row this was read from, and it travels on the written act as
    `in_reply_to`, so a reader can go and check the prose rather than take the proposal's
    word for it. A proposal that cannot be traced back is worse than none.
    """

    subject: str
    verb: str
    ref: str
    source_key: str
    evidence: str                   #: the line the act was read from, verbatim
    by: str                         #: "rules" or the model id that proposed it
    #: Other rows that produced this SAME triple. Kept rather than discarded because three
    #: seats independently reporting one merge is stronger evidence than one, and because
    #: without it a sweep writes three acts for one event. Measured on the real corpus: one
    #: merge was proposed from three different messages and another from two.
    corroborations: tuple[str, ...] = ()

    @property
    def inferred_verb(self) -> str:
        from .projection import INFERRED_PREFIX
        return f"{INFERRED_PREFIX}{self.verb}"

    @property
    def triple(self) -> tuple[str, str, str]:
        return (self.subject, self.verb, self.ref)


@dataclasses.dataclass(frozen=True)
class Refused:
    """No proposal, and WHY. A named reason, not None: "nothing here" and "three candidate
    subjects and I would have had to pick" are different facts, and only one of them is a
    case the model pass can help with."""

    reason: str
    anchors: Anchors


NO_ANCHORS = "no_anchors"           #: nothing to point at
NO_SUBJECT = "no_subject"
NO_REF = "no_ref"
NO_VERB = "no_verb"
AMBIGUOUS = "ambiguous"             #: more than one candidate; the model pass may resolve it
NEGATED = "negated"                 #: the sentence denies or requests rather than reports


def first_line(body: str) -> str:
    """The first line, kept because the refusal reasons are reported against it in the
    2026-09-21 measurement recorded in `docs/DIVERGENCES.md` 008."""
    return (body or "").strip().split("\n", 1)[0]


#: Sentence boundary: a full stop, question or exclamation mark followed by whitespace, or
#: a newline. Not perfect on abbreviations, and it does not need to be: a wrongly split
#: sentence loses a candidate rather than inventing one, and losing one is the direction
#: this module is allowed to fail in.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(body: str) -> list[str]:
    """THE UNIT OF AN ACT IS A SENTENCE, and this is the correction that made the module
    work at all.

    The first version required subject, version and verb all in the FIRST LINE. Measured
    on 300 production rows it proposed NOTHING, and the reason was not the corpus: 47 of
    those bodies carry all three somewhere, only 11 in the first line. Widening to the
    whole body is the wrong repair, because a version in paragraph five and a pull request
    in paragraph one are not thereby one act, and binding them would be the reconstruction
    the board exists to stop. A sentence is the unit that is neither too narrow nor a
    guess.

    It also fixes a second defect the same measurement exposed. The negation filter ran
    over the whole line, so a line reading "#931 BLOCKED at <sha>, node kxxx" was refused
    for a word in a LATER clause. Scoped to the sentence carrying the anchors, the filter
    judges the clause that makes the claim.
    """
    return [s.strip() for s in _SENTENCE_SPLIT.split((body or "").strip()) if s.strip()]


def subjects_in(text: str) -> tuple[str, ...]:
    out: list[str] = []
    for m in PR_RE.finditer(text):
        out.append(f"pr/{int(m.group(1))}")
    for m in ISSUE_RE.finditer(text):
        out.append(f"issue/{int(m.group(1))}")
    if not out:
        # `#851` alone is a PR or an issue and the text does not say which. Taken only
        # when nothing more specific was found, and normalised to the weaker `ref/`
        # namespace rather than guessing `pr/`, so a later reader is not told which.
        for m in HASH_NUM_RE.finditer(text):
            out.append(f"ref/{int(m.group(1))}")
    return tuple(dict.fromkeys(out))


def refs_in(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(SHA_RE.findall(text)))


def verbs_in(text: str) -> tuple[str, ...]:
    low = text.lower()
    found = [canon for form, canon in VERB_FORMS.items()
             if re.search(r"\b" + re.escape(form) + r"\b", low)]
    return tuple(dict.fromkeys(found))


def anchors_of(line: str) -> Anchors:
    return Anchors(subjects=subjects_in(line), refs=refs_in(line), verbs=verbs_in(line))


def bearing_sentences(body: str) -> list[tuple[str, Anchors]]:
    """Every sentence carrying a subject, a version AND a verb, with its anchors.

    The triple must co-occur in ONE sentence. That is the whole discipline: it is what
    separates reading an act from assembling one out of a message that mentions several
    things.
    """
    out = []
    for s in sentences(body):
        a = anchors_of(s)
        if a.subjects and a.refs and a.verbs:
            out.append((s, a))
    return out


def propose(body: str, *, source_key: str) -> Proposal | Refused:
    """The deterministic pass: exactly one bearing sentence, unambiguous, not negated.

    REFUSES ON AMBIGUITY RATHER THAN PICKING. Two versions in one sentence and a rule that
    takes the first would bind an act to whichever the author happened to mention earlier,
    silently and unfalsifiably. `AMBIGUOUS` is the case the model pass is for, and it is
    the commonest real case in the corpus: "#937 fixed and pushed. New head <sha>" carries
    two verbs and one of them is the act.
    """
    bearing = bearing_sentences(body)
    if not bearing:
        # Report the first line's anchors: that is what the refusal-reason measurement in
        # divergence 008 is expressed against, and it is where a reader looks first.
        a = anchors_of(first_line(body))
        if a.empty:
            return Refused(NO_ANCHORS, a)
        if not a.subjects:
            return Refused(NO_SUBJECT, a)
        if not a.refs:
            return Refused(NO_REF, a)
        return Refused(NO_VERB, a)
    if len(bearing) > 1:
        merged = Anchors(
            subjects=tuple(dict.fromkeys(s for _, a in bearing for s in a.subjects)),
            refs=tuple(dict.fromkeys(r for _, a in bearing for r in a.refs)),
            verbs=tuple(dict.fromkeys(v for _, a in bearing for v in a.verbs)))
        return Refused(AMBIGUOUS, merged)
    sentence, a = bearing[0]
    if NEGATION_RE.search(sentence) or INTENT_RE.search(sentence):
        return Refused(NEGATED, a)
    if not a.unambiguous:
        return Refused(AMBIGUOUS, a)
    return Proposal(subject=a.subjects[0], verb=a.verbs[0], ref=a.refs[0],
                    source_key=source_key, evidence=sentence, by="rules")


# --------------------------------------------------------------------------- #
# The model pass, for the rows the rules refuse
# --------------------------------------------------------------------------- #

#: The model is shown the candidates and asked to SELECT. It is never asked to produce a
#: sha, a subject or a verb, because a produced sha cannot be checked by a reader and a
#: produced verb leaves the closed vocabulary. Anything it returns that is not in the
#: candidate lists is refused by `_validate_selection` below.
PROMPT = """You read one line from a team chat and decide whether it REPORTS a completed act.

Line:
{line}

Candidate subjects (choose exactly one, or null): {subjects}
Candidate versions (choose exactly one, or null): {refs}
Candidate verbs (choose exactly one, or null): {verbs}

Answer with JSON only, no prose:
{{"subject": <one candidate or null>, "verb": <one candidate or null>, "ref": <one candidate or null>}}

Rules:
- Choose ONLY from the candidates given. Never write a value that is not listed.
- If the line asks for something, plans something, or denies something, answer all nulls.
- If you cannot tell WHICH candidate the act is about, answer all nulls.
"""


def available_generate_provider(snapshot: dict | None = None) -> dict | None:
    """A provider whose `generate` capability is AVAILABLE on evidence of function.

    Reads `apps.modelhost`, which probes rather than trusting configuration: it exists
    because a server answered `POST /v1/embeddings` with HTTP 200 and a vector of zeros,
    and a registry that believed the 200 would have had every consumer indexing them.
    """
    if snapshot is None:
        from apps.modelhost import registry
        snapshot = registry.snapshot()
    for entry in snapshot.get("providers", []) or []:
        if entry.get("kind") == "generate" and entry.get("available") is True:
            return entry
    return None


def _validate_selection(raw: dict, anchors: Anchors) -> tuple[str, str, str] | None:
    """Every field must be one of the candidates WE extracted. This is the check that
    makes the model pass safe rather than merely useful, so it is a hard refusal and not
    a warning: a returned sha that is not in `anchors.refs` was invented."""
    subject, verb, ref = raw.get("subject"), raw.get("verb"), raw.get("ref")
    if subject is None or verb is None or ref is None:
        return None
    if subject not in anchors.subjects or ref not in anchors.refs or verb not in anchors.verbs:
        return None
    return str(subject), str(verb), str(ref)


def propose_with_model(body: str, *, source_key: str, generate, model_id: str = "model",
                       ) -> Proposal | Refused:
    """The second pass. `generate(prompt) -> str` is injected, so this module never opens a
    socket and the tests never need one.

    Only ever called for a row the rules refused; a row the rules RESOLVED is not sent to a
    model, because a model cannot improve on an unambiguous read and can only disagree
    with it.

    The model sees ONE sentence, never the message. A message is a place to find several
    acts and the prompt asks for one, so handing it the whole body invites it to blend
    two. Where several sentences bear a triple, each is offered separately and the first
    one the model resolves is taken.
    """
    bearing = bearing_sentences(body)
    if not bearing:
        return Refused(NO_ANCHORS, anchors_of(first_line(body)))
    last: Refused = Refused("model_declined_or_invented", bearing[0][1])
    for sentence, a in bearing:
        result = _ask(sentence, a, source_key=source_key, generate=generate,
                      model_id=model_id)
        if isinstance(result, Proposal):
            return result
        last = result
    return last


def _ask(line: str, a: Anchors, *, source_key: str, generate, model_id: str):
    prompt = PROMPT.format(line=line, subjects=list(a.subjects), refs=list(a.refs),
                           verbs=list(a.verbs))
    try:
        text = generate(prompt)
    except Exception as exc:                       # a model failure is a refusal, never a guess
        return Refused(f"model_error: {type(exc).__name__}: {exc}"[:200], a)
    raw = _first_json_object(text)
    if raw is None:
        return Refused("model_returned_no_json", a)
    picked = _validate_selection(raw, a)
    if picked is None:
        return Refused("model_declined_or_invented", a)
    subject, verb, ref = picked
    return Proposal(subject=subject, verb=verb, ref=ref, source_key=source_key,
                    evidence=line, by=model_id)


def recent_rows(conn, *, limit: int = 300, namespace: str = None) -> list[dict]:
    """The most recent mesh rows, newest first, for a derive sweep.

    A WINDOW IS HONEST HERE AND IS NOT HONEST IN `projection.acts_for`. There the window
    was a defect, because "no acts for this subject" and "its acts are older than my
    window" read the same and one of them is a lie. Here the question is "what has been
    said lately that nobody declared", which is a question ABOUT a window, so the bound is
    the subject of the query rather than a hidden qualifier on the answer.
    """
    from psycopg.rows import dict_row
    from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT key, value, author_label, created_at FROM shared_memory "
            "WHERE namespace = %s AND is_head = TRUE AND key LIKE 'msg/%%' "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (namespace or MESH_CHAT_NAMESPACE, int(limit)),
        )
        return list(cur.fetchall())


def undeclared(rows) -> list[dict]:
    """Rows that do NOT already carry a declared act. A row whose author declared the act
    themselves is never a candidate for a proposal about it: the declaration is the better
    record and a proposal beside it is noise that a reader has to reconcile."""
    return [r for r in rows if not (isinstance(r.get("value"), dict)
                                    and r["value"].get("subject"))]


def sweep(rows, *, generate=None, model_id: str = "model") -> tuple[list[Proposal], dict]:
    """Propose over a batch. Returns (proposals, counts-by-refusal-reason).

    The counts are returned rather than logged because they are the measurement that says
    whether this is worth running: a sweep proposing nothing and a sweep finding nothing to
    propose about are different states, and only the reason histogram tells them apart.
    """
    import collections
    found: dict[tuple[str, str, str], Proposal] = {}
    reasons = collections.Counter()
    for row in rows:
        body = (row.get("value") or {}).get("body") or ""
        result = propose(body, source_key=row["key"])
        if isinstance(result, Refused) and result.reason == AMBIGUOUS and generate is not None:
            result = propose_with_model(body, source_key=row["key"], generate=generate,
                                        model_id=model_id)
        if not isinstance(result, Proposal):
            reasons[result.reason] += 1
            continue
        # DEDUPLICATE ON THE TRIPLE. Without this a sweep writes one act per MESSAGE rather
        # than per ACT, and the corpus repeats itself: one merge was reported in three
        # separate messages and another in two, so a naive sweep would have put five rows
        # on the board for two events.
        existing = found.get(result.triple)
        if existing is None:
            found[result.triple] = result
            reasons["PROPOSED"] += 1
        else:
            found[result.triple] = dataclasses.replace(
                existing, corroborations=existing.corroborations + (result.source_key,))
            reasons["corroborated"] += 1
    return list(found.values()), dict(reasons)


def _first_json_object(text: str) -> dict | None:
    """The first balanced `{...}` in the output, parsed. Models wrap JSON in prose and in
    code fences however they were asked not to, and a parser that fails on that turns a
    correct answer into a refusal."""
    if not isinstance(text, str):
        return None
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except ValueError:
                    start = -1
                    continue
                return obj if isinstance(obj, dict) else None
    return None
