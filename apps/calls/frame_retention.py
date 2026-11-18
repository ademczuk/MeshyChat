"""Invocation-frame retention by DEAD ROOT GRANT (STATUS D08; divergence 052; operator decision
D-2026-09-27-20, note 61: "a chain is deleted only after its root grant is revoked or expired,
plus a window, with an honest disposition of its anchor").

The retired rule deleted a chain once its NEWEST FRAME was past a 90-day cutoff
(`MESHY_FRAME_RETENTION_DAYS`, every 15 minutes, 2000 frames a call; sql/111). `chain_id` is the
ROOT GRANT id (sql/060:24-27), so a live grant that went quiet lost its history while its signed
anchor survived, and the anchor job now calls those anchors truncated (ASDLC incidents
2026-09-21-retention-orphans-chain-anchors.md, -retention-erased-live-grant-audit-history.md;
the six known orphans, apps/calls/chain_head_known_orphans.json). That rule was wrong. This
server has pruned nothing since sql_meshychat/012 (divergence 029), and
`MESHY_FRAME_RETENTION_DAYS` is not read here.

THE RULE, enforced in SQL by `retire_invocation_chain` (sql_meshychat/026), which re-checks
every clause whatever this module checked: a chain is taken only when
  * its root grant row exists and has been DEAD (`LEAST(revoked_at, expires_at)`) for longer
    than the policy's `chain_window_days` (default 90), a grant with no row is never taken;
  * its newest frame is older than the window too;
  * it has a signed anchor whose head is its newest frame with the anchored hash, and (checked
    HERE, since Ed25519 is not in SQL) whose signature verifies under the server's keys: a
    chain the anchor job refuses is evidence, and retention never destroys it.
It is then deleted NEWEST FIRST, at most `CHAIN_BATCH` frames a call, so what is left between
two calls is a prefix that still links to its root (the frame audit reads it as intact).

THE ANCHOR'S DISPOSITION. In the call that takes a chain, its anchor row moves whole into
`invocation_chain_retention` (the ledger) and leaves `invocation_chain_heads`. The ledger row
keeps the signature, so `verify_record` can still say "this chain reached N frames up to head H,
signed by kid K, and retention deleted them because its grant was dead since D". The chain-head
job never meets an anchor whose head retention removed, and anchors nothing for a chain while
it is 'pruning' (chain_head.RETIRING), so neither job reports the prune as tampering.

STANDALONE FRAMES (chain_id NULL) have their own rule, off by default: the policy's
`standalone_retain_days`, when set, deletes a standalone frame older than that by its own id.

THE LEDGER IS VERIFIED EVERY TICK, a bounded rolling pass (`LEDGER_PER_TICK`, cursor
`frame_retention.ledger`): the retired anchor's signature (ERROR when it fails), the chain's
root grant still dead (WARNING when it is live again: a revived expiry, or an app-role write
that made a grant look dead; either way the history of a live grant is gone and an operator
must know), and for a complete row that the frames deleted equal the signed count (ERROR when
not: the chain was already short before retention took it).

The policy lives in the database (`invocation_frame_retention_policy`, owner-writable, read
here), not the environment: the app role runs retention and cannot widen it.
"""
from __future__ import annotations

import dataclasses
import logging

from psycopg.rows import dict_row

from apps.calls import chain_head, frame_audit
from apps.jobs import cursors
from apps.jobs.result import TickResult

log = logging.getLogger("meshychat.calls.frame_retention")

#: A bounded retention delete needs no fast tick.
RETENTION_TICK_SECS = 15 * 60
#: Frames deleted per tick, chains and standalone together (the job's max_batch, B5).
FRAMES_PER_TICK = 5000
#: Frames deleted per call, so one transaction holds a chain's writer lock briefly. Production
#: plans it as an index-only range on invocation_frames_chain_idx and a primary-key probe per
#: frame (EXPLAIN GENERIC_PLAN, 2026-09-27).
CHAIN_BATCH = 1000
STANDALONE_BATCH = 1000
#: New candidates examined per tick (a rolling cursor over the dead grants), and ledger rows
#: verified per tick.
CANDIDATES_PER_TICK = 100
LEDGER_PER_TICK = 500

CURSOR_CANDIDATES = "frame_retention.candidates"
CURSOR_LEDGER = "frame_retention.ledger"

#: What `retire_invocation_chain` answers (sql_meshychat/026).
STARTED, PRUNING, COMPLETE, STOPPED = "started", "pruning", "complete", "stopped"
FINISHED, NOT_ELIGIBLE, BUSY, OFF = "finished", "not_eligible", "busy", "off"
DELETING = frozenset({STARTED, PRUNING})

#: What `verify_record` says of a ledger row.
PRUNED_BY_RETENTION = "pruned_by_retention"
IN_PROGRESS = "pruning"
STOPPED_BY_RULE = "stopped"
GRANT_LIVE_AGAIN = "grant_live_again"
COUNT_MISMATCH = "count_mismatch"
#: The retired anchor's signature, as chain_head names the two failures.
BAD_SIGNATURE, UNKNOWN_KID = chain_head.BAD_SIGNATURE, chain_head.UNKNOWN_KID
LEDGER_ALERTS = frozenset({BAD_SIGNATURE, UNKNOWN_KID, COUNT_MISMATCH})


@dataclasses.dataclass(frozen=True)
class Retired:
    chain_id: str
    outcome: str
    reason: str | None
    frames_deleted: int


@dataclasses.dataclass(frozen=True)
class LedgerVerdict:
    chain_id: str
    verdict: str
    status: str


@dataclasses.dataclass(frozen=True)
class Report:
    calls: tuple[Retired, ...]
    #: Candidates whose anchor did not verify, never retired: (chain_id, verdict).
    refused: tuple[tuple[str, str], ...]
    #: Standalone frames deleted this tick; None when the policy leaves them off.
    standalone_deleted: int | None
    ledger: tuple[LedgerVerdict, ...]
    more: bool

    @property
    def chain_frames_deleted(self) -> int:
        return sum(c.frames_deleted for c in self.calls)

    @property
    def frames_deleted(self) -> int:
        return self.chain_frames_deleted + (self.standalone_deleted or 0)


# ------------------------------------------------------------------ one chain

def retire(conn, chain_id, *, expect_head: int, expect_count: int, batch: int) -> Retired:
    """One call of `retire_invocation_chain`: its own transaction on an autocommit `conn`."""
    if batch <= 0:
        raise ValueError("batch must be > 0; a zero bound would be a silent no-op")
    with conn.cursor() as cur:
        cur.execute("SELECT outcome, reason, frames_deleted "
                    "FROM retire_invocation_chain(%s, %s, %s, %s)",
                    (chain_id, int(expect_head), int(expect_count), int(batch)))
        outcome, reason, n = cur.fetchone()
    return Retired(str(chain_id), outcome, reason, int(n))


def _anchor_of(row: dict, *, retired: bool = False) -> dict:
    """The anchor fields `chain_head.verify_signature` reads: from an invocation_chain_heads
    row, or (`retired`) from the `anchor_*` columns of a ledger row."""
    if not retired:
        return row
    return {"chain_id": row["chain_id"], "head_frame_id": row["anchor_head_frame_id"],
            "head_hash": row["anchor_head_hash"], "frame_count": row["anchor_frame_count"],
            "head_signature": row["anchor_signature"],
            "signing_kid": row["anchor_signing_kid"]}


def verify_record(row: dict, keys: dict[str, bytes], *, grant_live: bool) -> str:
    """What a ledger row says, most serious first: the retired anchor's signature, then a root
    grant live again, then (complete rows) the frames deleted against the signed count."""
    refused = chain_head.verify_signature(_anchor_of(row, retired=True), keys)
    if refused is not None:
        return refused
    if grant_live:
        return GRANT_LIVE_AGAIN
    if row["status"] == "complete":
        if row["frames_deleted"] != row["anchor_frame_count"]:
            return COUNT_MISMATCH
        return PRUNED_BY_RETENTION
    return IN_PROGRESS if row["status"] == "pruning" else STOPPED_BY_RULE


# ------------------------------------------------------------------ the tick

def _drain(conn, chain_id, head: int, count: int, budget: list[int],
           calls: list[Retired]) -> None:
    """Call retire for one chain until it stops deleting or the tick's budget is spent."""
    while budget[0] > 0:
        r = retire(conn, chain_id, expect_head=head, expect_count=count,
                   batch=min(CHAIN_BATCH, budget[0]))
        budget[0] -= r.frames_deleted
        calls.append(r)
        _log_call(r)
        if r.outcome not in DELETING or r.frames_deleted == 0:
            return


def _log_call(r: Retired) -> None:
    if r.outcome == STARTED:
        log.info("frame retention: took chain %s (its root grant is dead past the window); its "
                 "signed anchor moved to invocation_chain_retention; %d frame(s) deleted, "
                 "newest first", r.chain_id, r.frames_deleted)
    elif r.outcome == COMPLETE:
        log.info("frame retention: chain %s PRUNED BY RETENTION (%d frame(s) this call); its "
                 "signed anchor is kept in invocation_chain_retention", r.chain_id,
                 r.frames_deleted)
    elif r.outcome == STOPPED:
        log.warning("frame retention: chain %s STOPPED (%s); the frames left are a linked "
                    "prefix the chain-head job anchors again", r.chain_id, r.reason)
    elif r.outcome in (NOT_ELIGIBLE, BUSY, FINISHED, OFF):
        log.debug("frame retention: chain %s %s (%s)", r.chain_id, r.outcome, r.reason)


def _log_ledger(v: LedgerVerdict) -> None:
    if v.verdict in LEDGER_ALERTS:
        log.error("frame retention ALERT: chain %s: its retention record reports %s (status "
                  "%s): %s", v.chain_id, v.verdict, v.status,
                  "the frames deleted differ from the retired anchor's signed count"
                  if v.verdict == COUNT_MISMATCH else "the retired anchor does not verify")
    elif v.verdict == GRANT_LIVE_AGAIN:
        log.warning("frame retention: chain %s was pruned by retention (status %s) and its root "
                    "grant is LIVE again: the history of a live grant is gone", v.chain_id,
                    v.status)


def policy(conn) -> dict:
    """The owner's policy row (sql_meshychat/026). Read by explicit column; a missing row is
    an error, never a default."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT chain_window_days, standalone_retain_days "
                    "FROM invocation_frame_retention_policy")
        row = cur.fetchone()
    if row is None:
        raise LookupError("invocation_frame_retention_policy has no row; retention refuses to "
                          "guess a window")
    return row


def tick(conn, *, keys: dict[str, bytes], frames_per_tick: int = FRAMES_PER_TICK,
         candidates_per_tick: int = CANDIDATES_PER_TICK, ledger_per_tick: int = LEDGER_PER_TICK,
         cursor_prefix: str = "") -> Report:
    """One tick. `conn`: autocommit; every retire call is a transaction of its own, so a
    statement timeout loses at most one batch. `keys`: kid -> raw public key."""
    if frames_per_tick <= 0 or candidates_per_tick <= 0 or ledger_per_tick <= 0:
        raise ValueError("every bound must be > 0; a zero bound would be a silent no-op")
    pol = policy(conn)
    budget = [frames_per_tick]
    calls: list[Retired] = []
    refused: list[tuple[str, str]] = []

    if pol["chain_window_days"] is not None:
        # ---- 1. chains already being pruned, oldest start first ---------------------------
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT chain_id, anchor_head_frame_id, anchor_frame_count "
                        "FROM invocation_chain_retention WHERE status = 'pruning' "
                        "ORDER BY started_at, chain_id LIMIT %s", (int(candidates_per_tick),))
            going = cur.fetchall()
        for row in going:
            if budget[0] <= 0:
                break
            _drain(conn, row["chain_id"], row["anchor_head_frame_id"],
                   row["anchor_frame_count"], budget, calls)

        # ---- 2. new candidates: dead grants with an anchor, by a rolling cursor --------------
        if budget[0] > 0:
            name = cursor_prefix + CURSOR_CANDIDATES
            state = cursors.read_or_start(conn, name)
            after, laps = state["position_key"], state["laps"]
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT h.chain_id, h.head_frame_id, h.head_hash, h.frame_count, "
                    "       h.head_signature, h.signing_kid "
                    "  FROM invocation_frame_retention_policy p "
                    "  JOIN skill_grants g ON LEAST(g.revoked_at, g.expires_at) "
                    "       < now() - make_interval(days => p.chain_window_days) "
                    "  JOIN invocation_chain_heads h ON h.chain_id = g.id "
                    " WHERE p.chain_window_days IS NOT NULL "
                    "   AND (%s::uuid IS NULL OR g.id > %s::uuid) "
                    "   AND NOT EXISTS (SELECT 1 FROM invocation_chain_retention r "
                    "                    WHERE r.chain_id = g.id) "
                    " ORDER BY g.id LIMIT %s",
                    (after, after, int(candidates_per_tick)))
                fresh = cur.fetchall()
            last = after
            for row in fresh:
                if budget[0] <= 0:
                    break
                last = str(row["chain_id"])
                verdict = chain_head.verify_signature(_anchor_of(row), keys)
                if verdict is not None:
                    refused.append((last, verdict))
                    log.error("frame retention: chain %s is NOT retired: its anchor does not "
                              "verify (%s); retention never deletes a chain the chain-head "
                              "job refuses", last, verdict)
                    continue
                _drain(conn, row["chain_id"], row["head_frame_id"], row["frame_count"],
                       budget, calls)
            else:
                if len(fresh) < candidates_per_tick:
                    last, laps = None, laps + 1
            cursors.save(conn, name, key=last, laps=laps)

    # ---- 3. standalone frames, when the policy sets a window -------------------------------
    standalone: int | None = None
    if pol["standalone_retain_days"] is not None:
        standalone = 0
        while budget[0] > 0:
            ask = min(STANDALONE_BATCH, budget[0])
            with conn.cursor() as cur:
                cur.execute("SELECT prune_standalone_invocation_frames(%s)", (ask,))
                n = cur.fetchone()[0]
            if n is None:                       # the owner turned it off since the read
                break
            standalone += n
            budget[0] -= n
            if n < ask:                         # nothing more is old enough
                break
        if standalone:
            log.info("frame retention: %d standalone frame(s) older than %d day(s) deleted",
                     standalone, pol["standalone_retain_days"])

    # ---- 4. the ledger, verified by a rolling cursor ----------------------------------------
    name = cursor_prefix + CURSOR_LEDGER
    state = cursors.read_or_start(conn, name)
    after, laps = state["position_key"], state["laps"]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT r.chain_id, r.status, r.anchor_head_frame_id, r.anchor_head_hash, "
            "       r.anchor_frame_count, r.anchor_signature, r.anchor_signing_kid, "
            "       r.frames_deleted, "
            "       (g.id IS NOT NULL AND g.revoked_at IS NULL "
            "        AND (g.expires_at IS NULL OR g.expires_at > now())) AS grant_live "
            "  FROM invocation_chain_retention r "
            "  LEFT JOIN skill_grants g ON g.id = r.chain_id "
            " WHERE (%s::uuid IS NULL OR r.chain_id > %s::uuid) "
            " ORDER BY r.chain_id LIMIT %s", (after, after, int(ledger_per_tick)))
        rows = cur.fetchall()
    verdicts = []
    for row in rows:
        v = LedgerVerdict(str(row["chain_id"]),
                          verify_record(row, keys, grant_live=bool(row["grant_live"])),
                          row["status"])
        verdicts.append(v)
        _log_ledger(v)
    if len(rows) < ledger_per_tick:
        after, laps = None, laps + 1
    else:
        after = str(rows[-1]["chain_id"])
    cursors.save(conn, name, key=after, laps=laps)

    return Report(calls=tuple(calls), refused=tuple(refused), standalone_deleted=standalone,
                  ledger=tuple(verdicts), more=budget[0] <= 0)


def run_job(conn, job) -> TickResult:
    """The registered body of `frame_retention` (apps/jobs/registry.py)."""
    report = tick(conn, keys=frame_audit.verifier_keys(), frames_per_tick=job.max_batch)
    if report.frames_deleted:
        log.info("frame retention: %d chained and %s standalone frame(s) deleted this tick",
                 report.chain_frames_deleted,
                 "no" if report.standalone_deleted is None else report.standalone_deleted)
    return TickResult(rows=report.frames_deleted, more_work_pending=report.more)


__all__ = ["BAD_SIGNATURE", "BUSY", "CANDIDATES_PER_TICK", "CHAIN_BATCH", "COMPLETE",
           "COUNT_MISMATCH", "FINISHED", "FRAMES_PER_TICK", "GRANT_LIVE_AGAIN", "IN_PROGRESS",
           "LEDGER_PER_TICK", "LedgerVerdict", "NOT_ELIGIBLE", "OFF", "PRUNED_BY_RETENTION",
           "PRUNING", "RETENTION_TICK_SECS", "Report", "Retired", "STANDALONE_BATCH", "STARTED",
           "STOPPED", "STOPPED_BY_RULE", "UNKNOWN_KID", "policy", "retire", "run_job", "tick",
           "verify_record"]
