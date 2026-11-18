"""Derive the state of a unit of work by QUERYING declared acts, not reading prose.

This is the read half of the board. `sampleco/ASDLC` `intent.md` states the problem it
exists for, in the words of the seat who measured it:

    "'uxxx is holding' and 'the fix is unopened' read the same from the outside and they
     are not the same."

Nothing was wrong with any message. There was simply no place to ASK what the state of a
unit of work was, so a seat had to reconstruct it from traffic.

WHAT A DECLARED ACT IS. A mesh row carrying `subject`. The verb is `type`, the object is
`subject`, the version it was performed at is `ref`, and THE ACTOR IS THE AUTHENTICATED
PEER ON THE ROW rather than any name in the body. That last point is the whole reason the
fields exist: a read-time projector over prose attributed all nine `reviewed` edges on
`pr/882` to the peer REPORTING a review rather than the one who ran it. This module reads
`created_by_peer_id` and never `value['from']`.

TWO DEFECTS OF THE EARLIER READER (`scripts/board_read.py` in `sampleco/ASDLC`) ARE DELIBERATELY
EXCLUDED. Both are recorded because they are easy to reintroduce.

  1. IT ANSWERED ABOUT A WINDOW AND SOUNDED LIKE IT ANSWERED ABOUT THE SUBJECT. It pulled
     the most recent 500 rows and filtered by subject in Python, so `--subject pr/924`
     printing nothing meant EITHER "no acts" OR "its acts are older than the window". That
     is kxxx's failure recurring inside the tool built to remove it. Here the subject is a
     predicate in the query, so absence is absence, and it is TYPED (`NoDeclaredActs`)
     rather than an empty list a caller can truth-test by accident.
  2. IT ORDERED BY THE ACTOR'S OWN CLOCK. It sorted on `value['ts']`, a client-supplied
     envelope field validated for shape and not for truth, falling back to `created_at`.
     Sender clocks on this fleet have been measured future-dated by up to 67 minutes, so an
     actor could reorder history by setting a field. Ordering here is `created_at`, assigned
     by the database doing the ordering, and the claimed client time is kept alongside so
     the disagreement is visible instead of silently deciding the sequence.

THIS READER QUERIES THE DATABASE DIRECTLY. It does not require a command-line call before the
read.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from typing import Iterable

#: The namespace mesh traffic lives in. Imported rather than redeclared so a change to the
#: validator's idea of the namespace cannot leave the reader looking at the wrong one.
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

#: A claimed client timestamp further than this from the server's own clock is reported.
#: Not refused: a skewed clock is the sender's problem and the act still happened. Set at
#: five minutes because the measured fleet excursions were tens of minutes, not seconds.
CLOCK_SKEW_REPORT_SECONDS = 300.0

#: The subject spelling a writer uses when the prose said only `#936`: the number is known
#: and whether it names a pull request or an issue is not. `apps/board/derive.py` emits it
#: rather than guessing `pr/`, which is right, and it creates a second spelling for one
#: object, which is a problem this module has to answer for rather than hide.
UNSPECIFIED_KIND_PREFIX = "ref/"

#: The spellings that can name one object. `pr/936` and `issue/936` are NOT among each
#: other's: those are different objects that happen to share a number, and merging them
#: would be exactly the invention this file refuses. Only `ref/936` bridges, because it is
#: the spelling that means "this number, kind unstated".
_KIND_PREFIXES = ("pr/", "issue/")


def same_number_subjects(subject: str) -> tuple[str, ...]:
    """Other spellings that may name the SAME object as `subject`.

    Asked about `pr/936`, a reader must be shown acts recorded against `ref/936`, because a
    seat wrote `#936` and the deriver honestly declined to say which kind it was. Not
    showing them is a silent filter, and showing them merged into `acts` would assert an
    identity nobody established. So they are returned separately and the caller keeps them
    apart.
    """
    for prefix in _KIND_PREFIXES:
        if subject.startswith(prefix):
            return (UNSPECIFIED_KIND_PREFIX + subject[len(prefix):],)
    if subject.startswith(UNSPECIFIED_KIND_PREFIX):
        n = subject[len(UNSPECIFIED_KIND_PREFIX):]
        return tuple(p + n for p in _KIND_PREFIXES)
    return ()


#: An act whose verb carries this prefix was PROPOSED by a deriver reading prose, not
#: declared by the actor (`derive.py`). It lives under its own verb on purpose: an inferred
#: review must never satisfy `has("reviewed")`, because "the fix is unopened" and "a model
#: thinks somebody reviewed it" are not the same state, and collapsing them is the exact
#: failure the board exists to remove.
INFERRED_PREFIX = "inferred."


@dataclasses.dataclass(frozen=True)
class DeclaredAct:
    """One act, as the database records it."""

    subject: str
    verb: str | None
    ref: str | None
    actor_peer_id: str
    actor_label: str | None
    recorded_at: _dt.datetime          #: server clock, and the ordering key
    claimed_ts: str | None             #: the actor's own clock, reported never trusted
    key: str
    schema_version: int | None
    source_key: str | None = None      #: `in_reply_to`: the row an inferred act was read from

    @property
    def inferred(self) -> bool:
        """Proposed by a deriver from prose, not declared by the actor on the row."""
        return bool(self.verb) and self.verb.startswith(INFERRED_PREFIX)

    @property
    def declared_verb(self) -> str | None:
        """The verb the proposal is ABOUT, for display beside a real declaration."""
        if not self.verb:
            return None
        return self.verb[len(INFERRED_PREFIX):] if self.inferred else self.verb

    @property
    def validated(self) -> bool:
        """Whether the daemon type-checked this row on the way in.

        A row without `schema_version` was stored before validation existed or was written
        outside v1, so its `subject` was never checked against MESH_SUBJECT_RE. It is still
        shown, flagged, because hiding it would be a silent filter.
        """
        return self.schema_version == 1

    @property
    def clock_skew_seconds(self) -> float | None:
        """Claimed time minus recorded time, or None when no claim was made."""
        if not self.claimed_ts:
            return None
        try:
            claimed = _dt.datetime.fromisoformat(self.claimed_ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (claimed - self.recorded_at).total_seconds()

    @property
    def clock_is_suspect(self) -> bool:
        skew = self.clock_skew_seconds
        return skew is not None and abs(skew) > CLOCK_SKEW_REPORT_SECONDS


@dataclasses.dataclass(frozen=True)
class SubjectState:
    """Acts exist for this subject. Ordered oldest first."""

    subject: str
    acts: tuple[DeclaredAct, ...]

    @property
    def latest_by_verb(self) -> dict[str, DeclaredAct]:
        """The most recent act of each verb. This is what answers 'what is the state'.

        An inferred act is keyed under its own prefixed verb (`inferred.reviewed`), so it
        is visible here and cannot stand in for `reviewed`.
        """
        out: dict[str, DeclaredAct] = {}
        for act in self.acts:          # acts are oldest first, so later wins
            if act.verb:
                out[act.verb] = act
        return out

    @property
    def declared(self) -> tuple[DeclaredAct, ...]:
        return tuple(a for a in self.acts if not a.inferred)

    @property
    def inferred(self) -> tuple[DeclaredAct, ...]:
        return tuple(a for a in self.acts if a.inferred)

    def has(self, verb: str) -> bool:
        """Whether an act with exactly this verb was DECLARED. `has("reviewed")` is False
        when the only review on the subject is an inferred one."""
        return verb in self.latest_by_verb


@dataclasses.dataclass(frozen=True)
class NoDeclaredActs:
    """No act has ever been declared for this subject.

    A DISTINCT TYPE, not an empty `SubjectState` and not `[]`. This is B3, and it is the
    precise distinction the board exists for: a subject that has been reviewed and is being
    held has acts, and a fix that was never opened has none. An empty list makes a caller
    write `if not acts:` and collapse the two back together, which is the failure this
    module is a response to.
    """

    subject: str


def _row_to_act(row: dict) -> DeclaredAct:
    value = row["value"] or {}
    return DeclaredAct(
        subject=value["subject"],
        verb=value.get("type"),
        ref=value.get("ref"),
        # The authenticated peer, never value['from']. See the module docstring.
        actor_peer_id=str(row["created_by_peer_id"]),
        actor_label=row.get("author_label"),
        recorded_at=row["created_at"],
        claimed_ts=value.get("ts"),
        key=row["key"],
        schema_version=value.get("schema_version"),
        source_key=value.get("in_reply_to"),
    )


def acts_for(conn, subject: str, *, namespace: str = MESH_CHAT_NAMESPACE) -> list[DeclaredAct]:
    """Every declared act for one subject, oldest first. No window, no limit.

    Unbounded BY DESIGN and bounded IN PRACTICE by the partial index in
    `sql_meshychat/003`: the number of acts on one unit of work is small, while the number
    of messages in the namespace is not, and it is the second one a limit would be hiding.
    A limit here would reintroduce defect 1 from the module docstring.
    """
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT key, value, author_label, created_by_peer_id, created_at "
            "FROM shared_memory "
            "WHERE namespace = %s AND value ? 'subject' AND value ->> 'subject' = %s "
            "  AND is_head = TRUE "
            "ORDER BY created_at ASC, id ASC",
            (namespace, subject),
        )
        return [_row_to_act(r) for r in cur.fetchall()]


def state_for(conn, subject: str, *, namespace: str = MESH_CHAT_NAMESPACE):
    """`SubjectState` when acts exist, `NoDeclaredActs` when none do. Never a bare list."""
    acts = acts_for(conn, subject, namespace=namespace)
    if not acts:
        return NoDeclaredActs(subject=subject)
    return SubjectState(subject=subject, acts=tuple(acts))


def same_number_acts(conn, subject: str, *, namespace: str = MESH_CHAT_NAMESPACE
                     ) -> dict[str, list[DeclaredAct]]:
    """Acts recorded against the same number under a DIFFERENT spelling, keyed by spelling.

    Kept out of `state_for` on purpose. Folding them in would make `has("merged")` true for
    `pr/936` on the strength of a row that never said `pr`, which is an identity this module
    has no evidence for. Returned beside the state instead, so a reader sees both and
    decides.
    """
    out: dict[str, list[DeclaredAct]] = {}
    for other in same_number_subjects(subject):
        acts = acts_for(conn, other, namespace=namespace)
        if acts:
            out[other] = acts
    return out


def subjects_seen(conn, *, namespace: str = MESH_CHAT_NAMESPACE) -> list[str]:
    """Every subject that has at least one declared act."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT value ->> 'subject' FROM shared_memory "
            "WHERE namespace = %s AND value ? 'subject' AND is_head = TRUE "
            "ORDER BY 1",
            (namespace,),
        )
        return [r[0] for r in cur.fetchall()]


def adoption(conn, *, namespace: str = MESH_CHAT_NAMESPACE) -> tuple[int, int]:
    """(rows carrying a subject, rows in the namespace).

    Returned as a PAIR because the numerator alone is the number this project has already
    been burned reporting without its denominator. "No declared acts" is a fact about
    adoption, not about whether work happened, and only the ratio says which.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE value ? 'subject'), count(*) "
            "FROM shared_memory WHERE namespace = %s AND is_head = TRUE",
            (namespace,),
        )
        declared, total = cur.fetchone()
        return int(declared), int(total)


def render_same_number(subject: str, found: dict[str, list[DeclaredAct]]) -> Iterable[str]:
    """Say it in words. THE PROSE RESTATEMENT RULE APPLIES HERE HARDEST: a caller that
    reads only the prose and is not told about these acts has been silently filtered, and a
    caller told they are the same object has been lied to. So the line says what is known,
    that the number matches, and what is not, that nobody said which kind."""
    for other, acts in found.items():
        yield (f"  Also on the same number, spelled {other}: {len(acts)} act(s). "
               f"That spelling means the writer said only the number and did not say "
               f"whether it names a pull request or an issue, so these are NOT counted as "
               f"acts on {subject}. Read them and decide.")


def render(state, *, adoption_pair: tuple[int, int] | None = None) -> Iterable[str]:
    """Human-readable projection.

    THE PROSE RESTATEMENT RULE APPLIES TO THE READER TOO. The board spec records that an
    attribution carried only in a structured field is, to a reader whose reading is the
    prose, ABSENT. So this prints what happened in words rather than emitting a table of
    field values a reader has to decode.
    """
    if isinstance(state, NoDeclaredActs):
        yield f"{state.subject}: no act has ever been declared."
        if adoption_pair:
            declared, total = adoption_pair
            pct = (100.0 * declared / total) if total else 0.0
            yield (f"  Adoption context: {declared} of {total} rows in this namespace carry "
                   f"a subject ({pct:.1f}%). An empty result here is a fact about adoption, "
                   f"not evidence that no work happened.")
        return

    declared, inferred = state.declared, state.inferred
    if inferred:
        yield (f"{state.subject}: {len(declared)} declared act(s), plus {len(inferred)} "
               f"inferred (unconfirmed).")
    else:
        yield f"{state.subject}: {len(state.acts)} declared act(s)."
    for act in state.acts:
        flags = []
        if not act.validated:
            flags.append("UNVALIDATED, no schema_version")
        if act.clock_is_suspect:
            flags.append(f"CLOCK SKEW {act.clock_skew_seconds:+.0f}s vs server")
        suffix = ("  [" + "; ".join(flags) + "]") if flags else ""
        who = act.actor_label or act.actor_peer_id[:8]
        if act.inferred:
            # Said in words, and said as a proposal: the reader must not carry away
            # "reviewed" from a line a model wrote.
            yield (f"  INFERRED, not declared: {who} proposes that it was "
                   f"{act.declared_verb or '<no verb>'} at ref {act.ref or '<none>'}, read from "
                   f"row {act.source_key or '<unknown>'}, recorded "
                   f"{act.recorded_at.isoformat()}. Unconfirmed until the actor declares "
                   f"it.{suffix}")
            continue
        yield (f"  {who} {act.verb or '<no verb>'} it "
               f"at ref {act.ref or '<none>'}, recorded {act.recorded_at.isoformat()}{suffix}")
    verbs = sorted(state.latest_by_verb)
    yield f"  Latest act of each kind: {', '.join(verbs) if verbs else '<none>'}."
