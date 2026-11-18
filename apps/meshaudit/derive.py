"""One audit sweep: select legacy rows, ask the LAN model, validate, and (only with
`write=True`) record. The CLI is `python -m apps.meshaudit derive [--write]`.

WRITING IS OFF BY DEFAULT, as `python -m apps.board derive` (divergence 008). A dry run
reads, asks the model and prints what WOULD be recorded, and it does so inside READ ONLY
transactions: Postgres itself refuses a write on that path, so "the dry run is dry" is a
property of the database session and not of this module remembering an `if`.

THREE PHASES, AND NO TRANSACTION IS HELD ACROSS A MODEL CALL (divergence 019's rule, for
the same reason: a GPU call measured in seconds would pin a snapshot and a connection).

  1. read     one server_internal transaction: the watermark, the eligible rows, the
              known labels, each row's context and candidates. Then it closes.
  2. ask      per row, outside any transaction, through apps/meshsem/providers.chat, the
              single MUST-8 chokepoint: only an IP-literal loopback or RFC1918 host gets
              mesh text, never through a proxy, never after a redirect.
  3. check    per row, one server_internal transaction that re-reads every fact it
              validates against, then (write) inserts the survivors and the ledger row in
              that same transaction, or (dry run) is READ ONLY and inserts nothing.

A row the model could not be asked about is NOT recorded, so it is asked again next run.
A run that had rows to ask and no chat provider that answered STOPS and says so
(`stopped`); the CLI exits non-zero (V1-DESIGN-RULES E1: a sweep that asked nobody must
not look like a sweep that found nothing). A provider that answered HTTP 200 with EMPTY
content did answer: the row is refused `answer_empty` and ledgered like any other answer,
so one row a model cannot answer does not stop every later sweep at the same place.

NOT A SCHEDULED JOB. Divergence 021 says why: the write decision belongs to the operator
who runs `--write`, and a scheduled dry run would spend GPU minutes producing nothing
anyone reads.
"""
from __future__ import annotations

import contextlib
import dataclasses
import time

from apps.core import rls
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.mesh import normalize

from . import prompt as P
from . import store
from .validate import (ANSWER_EMPTY, EDGE_TYPES, Candidates, Proposal, Refusal, Subject,
                       build_candidates, parse_answer, text_anchors, validate)

DEFAULT_LIMIT = 20
#: Wall budget for one row's model call (every provider tried), and for the whole sweep.
CHAT_BUDGET_S = 90.0
RUN_BUDGET_S = 900.0


@dataclasses.dataclass
class RowResult:
    key: str
    outcome: str                        #: asked | no_body | nothing_to_ask | not_asked
    asked: tuple[str, ...] = ()
    accepted: tuple[Proposal, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    inserted: int = 0
    already_held: int = 0
    provider_id: str | None = None


@dataclasses.dataclass
class Report:
    write: bool
    prompt_version: str
    rows_selected: int = 0
    rows_asked: int = 0
    rows_no_body: int = 0
    rows_nothing_to_ask: int = 0
    accepted: dict = dataclasses.field(default_factory=lambda: {t: 0 for t in EDGE_TYPES})
    inserted: int = 0
    already_held: int = 0
    refused: dict = dataclasses.field(default_factory=dict)
    rows: list = dataclasses.field(default_factory=list)
    providers_used: list = dataclasses.field(default_factory=list)
    attempts: list = dataclasses.field(default_factory=list)
    #: Why the sweep ended before its last row, or None.
    stopped: str | None = None
    #: True when it ended because no chat provider answered: the CLI exits 1 on it. A
    #: sweep that ran out of its time budget is bounded, not failed.
    provider_failed: bool = False


@dataclasses.dataclass(frozen=True)
class _Prepared:
    row: dict
    subject: Subject
    candidates: Candidates | None
    prompt: str | None
    outcome: str


@contextlib.contextmanager
def _txn(conn, *, write: bool):
    """server_internal and READ ONLY on a dry run; the job-only writer on a --write run.

    SET TRANSACTION READ ONLY is legal after the set_config query that server_internal
    runs first (measured: `transaction_read_only` reads `on` and a CREATE is refused with
    ReadOnlySqlTransaction), and it cannot be switched back inside the transaction.

    A writing run enters `rls.projection_writer` instead, because sql_meshychat/008 lets
    only that context write mesh_inferred_edges or mesh_inferred_audits (divergence 022).
    So a dry run is dry twice over: its transaction is read-only, AND it lacks the flag
    the tables' write policies require."""
    if write:
        with rls.projection_writer(conn):
            yield conn
        return
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        yield conn


def _asked_for(value) -> tuple[str, ...]:
    """The inferable relations the row does NOT declare, by the same normalizer the
    declared projection uses, so "lacking" means lacking under every spelling.

    Empty unless the row lacks a thread or a recipient: that is the eligibility rule in
    store._ELIGIBLE, computed again here FROM THE ROW, so a row whose edges the projection
    never wrote (committed after the catch-up passed its timestamp, divergence 014's
    blind spot) is judged by what its sender declared and not by an absence in mesh_edges.
    """
    declared = {e.edge_type for e in normalize.edges_for(value, author_label=None).edges}
    if "in_thread" in declared and "addressed_to" in declared:
        return ()
    return tuple(t for t in EDGE_TYPES if t not in declared)


def _prepare(conn, row: dict, labels: frozenset[str], namespace: str) -> _Prepared:
    value = row.get("value")
    author_labels = frozenset(x for x in (row.get("author_label"), row.get("author_name"))
                              if isinstance(x, str) and x)
    asked = _asked_for(value) if isinstance(value, dict) else ()
    subject = Subject(src_key=row["key"], created_at=row["created_at"],
                      author_labels=author_labels, asked=asked)
    body = value.get("body") if isinstance(value, dict) else None
    if not isinstance(body, str) or not body.strip():
        return _Prepared(row, subject, None, None, "no_body")
    if not asked:
        return _Prepared(row, subject, None, None, "nothing_to_ask")
    context = store.context_for(conn, row, namespace=namespace)
    mentioned = text_anchors(body, labels)["keys"]
    candidates = build_candidates(body, known_labels=labels, context=context,
                                  threads_of_mentioned_keys=store.threads_of_keys(
                                      conn, mentioned))
    text = P.render(key=row["key"], author=row.get("author_label") or "?",
                    created_at=row["created_at"].isoformat(), body=body, context=context,
                    candidates=candidates, asked=asked)
    return _Prepared(row, subject, candidates, text, "asked")


def _model_of(provider_id: str) -> str:
    from apps.modelhost import registry
    declared, _errors = registry.declared()
    for p in declared:
        if p.id == provider_id:
            return p.model or p.id
    return provider_id


def derive(conn, *, write: bool = False, limit: int = DEFAULT_LIMIT,
           keys: list[str] | None = None, chat=None, run_budget_s: float = RUN_BUDGET_S,
           namespace: str = MESH_CHAT_NAMESPACE) -> Report:
    """One sweep. `conn` is an autocommit, RLS-bound connection with no open transaction
    (dbconn.serving_connection). `chat` defaults to apps/meshsem/providers.chat and is a
    parameter only so a test can count calls; the tests drive the real one against a
    local fake host."""
    from apps.meshsem import providers
    if chat is None:
        chat = providers.chat
    report = Report(write=write, prompt_version=P.PROMPT_VERSION)

    with _txn(conn, write=False):
        rows = store.select_subjects(conn, prompt_version=P.PROMPT_VERSION, limit=limit,
                                     keys=keys, namespace=namespace)
        labels = store.known_labels(conn) if rows else frozenset()
        prepared = [_prepare(conn, r, labels, namespace) for r in rows]
    report.rows_selected = len(prepared)

    deadline = time.monotonic() + run_budget_s
    for item in prepared:
        result = RowResult(key=item.row["key"], outcome=item.outcome,
                           asked=item.subject.asked)
        report.rows.append(result)
        if item.outcome != "asked":
            if item.outcome == "no_body":
                report.rows_no_body += 1
            else:
                report.rows_nothing_to_ask += 1
            if write:
                with _txn(conn, write=True):
                    store.record(conn, item.row, outcome=item.outcome,
                                 prompt_version=P.PROMPT_VERSION)
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result.outcome = "not_asked"
            report.stopped = report.stopped or f"run budget of {run_budget_s:g}s spent"
            continue
        if report.stopped:
            result.outcome = "not_asked"
            continue
        try:
            reply = chat([{"role": "user", "content": item.prompt}],
                         max_tokens=P.REQUEST["max_tokens"],
                         temperature=P.REQUEST["temperature"],
                         template_kwargs=P.REQUEST["chat_template_kwargs"],
                         budget_s=min(CHAT_BUDGET_S, remaining))
        except providers.ChatUnavailable as exc:
            attempts = list(getattr(exc, "attempts", []) or [])
            empty = [a for a in attempts if a.get("verdict") == "failed"
                     and str(a.get("reason", "")).startswith("empty content")]
            if not empty:
                result.outcome = "not_asked"
                report.attempts.extend(attempts)
                report.stopped = f"no chat provider answered: {exc}"[:300]
                report.provider_failed = True
                continue
            provider_id = empty[-1]["id"]
            proposals, refusals = [], [Refusal(None, "", ANSWER_EMPTY)]
        else:
            provider_id = reply.provider_id
            proposals, refusals = parse_answer(reply.text, item.subject,
                                               finish_reason=reply.finish_reason)
        report.rows_asked += 1
        result.provider_id = provider_id
        if provider_id not in report.providers_used:
            report.providers_used.append(provider_id)
        with _txn(conn, write=write):
            verdict = validate(proposals, item.subject, item.candidates,
                               store.DbFacts(conn, namespace=namespace),
                               prior_refusals=refusals)
            if write:
                written = store.record(
                    conn, item.row, outcome="asked", prompt_version=P.PROMPT_VERSION,
                    accepted=verdict.accepted, refused=verdict.refused(),
                    asked=item.subject.asked, model=_model_of(provider_id),
                    provider_id=provider_id)
                result.inserted, result.already_held = written.inserted, written.already_held
        result.accepted, result.refusals = verdict.accepted, verdict.refusals
        for p in verdict.accepted:
            report.accepted[p.edge_type] += 1
        for reason, n in verdict.refused().items():
            report.refused[reason] = report.refused.get(reason, 0) + n
        report.inserted += result.inserted
        report.already_held += result.already_held
    return report


__all__ = ["CHAT_BUDGET_S", "DEFAULT_LIMIT", "Report", "RowResult", "derive"]
