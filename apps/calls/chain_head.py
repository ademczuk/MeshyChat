"""The signed chain-head anchor, reconciled INCREMENTALLY (STATUS E10; rule ii).

`invocation_chain_heads` (sql/071) holds one signed row per chain: the head frame id, head hash,
and frame count. A live table shorter than the signed count, or missing the signed head, is
TRUNCATION; the frame audit (D09) cannot detect a clean tail cut because the remaining frames
still link.

Nothing counts a whole chain. Recounting on every authorize was measured at O(n) per call and
about 490 GB/day of production reads. The anchor
is EXTENDED: from the anchored head, read later frames through `(chain_id, id DESC)`, check each
link, and add the frames read. A chain with no anchor is anchored from its root in bounded steps:
an anchor at the k-th frame with count k truthfully says the chain reached at least k frames up
to id M, and the next tick continues it.

WHY THE COUNT IS EXACT WITHOUT COUNTING: the frame writer, outside these modules, holds a
per-chain advisory lock through commit, so within one chain id order is commit order and each
frame links to the one before it; the no-fork and one-root unique indexes (sql/062) keep it a
line. This job's count is exact only while that holds: counting frames after the head, while
checking each link, then yields the count through the new head.

WHO CALLS IT. `apps/calls/pipeline.py` does not write frames or anchors, so this background job,
`chain_head_reconcile`, anchors existing frames and history. Three passes a tick use cursors in
`background_job_cursors`:
  1. FRAMES, by id past `chain_head.frames` (from 0: history is anchored too): every chained
     frame's chain is extended until its anchor covers that frame, within the tick's frame
     budget; the cursor stops before a frame whose chain the budget did not reach.
  2. ANCHORS, rolling verification by chain_id past `chain_head.anchors`: the signature under
     the key its kid names, the signed head still present with its signed hash, and extension
     when frames were committed after pass 1 passed their ids.
  3. RESCAN, rolling re-reads of frame ids past `chain_head.rescan`, up to pass 1's cursor,
     then from 0 again (`laps`), for chains with NO anchor (review finding 2, 2026-09-26).
      Pass 2 only visits chains that already have an anchor. A NEW chain whose frames drew ids
      before pass 1 passed them and committed afterward would otherwise remain unanchored until
      another frame arrived, and a truncation of it could not be detected. Each row reads its
      anchor state through one primary-key probe; only unanchored chains are reconciled, and the
      cursor stops before a frame whose chain the budget did not reach. A lap is
     (frames / RESCAN_PER_TICK) ticks, so the delay it closes is a lap, not forever.
The job intentionally does not compare a live `COUNT(*)` to the signed count because that would
be a whole-chain count. The head check detects a tail cut, and deleting a middle frame breaks a
link that the frame audit re-checks every lap.

REFUSALS, each logged at ERROR. Three leave the anchor unchanged: a signature that does not
verify (`bad_signature`) or names an unavailable key (`unknown_signing_kid`), because extending
it would re-sign a tampered count; and a missing or changed signed head (`truncated`), because
re-anchoring below it would launder the cut. Two stop extension at a break: a frame that does not
link to the one before it (`link_break`: earlier frames are anchored, it and later frames are
not), and a chain with no anchor whose first frame is not a root (`root_missing`: nothing is
anchored).

THE KNOWN ORPHANS (STATUS D08, operator decision 53, 2026-09-27). Production's frame
retention (sql/111's chained pass, stopped 2026-09-23) deleted every frame of chains whose
anchors survived, so their signed heads are missing: `truncated` by this module's own rule,
and not tampering. The read of the production standby on 2026-09-27 (07:44Z) found six. This
job is the first reader those anchors ever had, and one ERROR per orphan per lap (a lap is a
tick at production's 49 anchors) would teach its readers to skip the one verdict that flags a
real cut. The six are in a COMMITTED list, `chain_head_known_orphans.json` beside this module,
each with its incident. A `truncated` verdict whose anchor matches an entry EXACTLY (chain_id,
head_frame_id, frame_count and signing_kid, as the anchor row holds them) AND whose chain is
still in the state the list records (the signed head missing and NO live frame of the chain,
`Outcome.head_state` CHAIN_EMPTY) is logged at WARNING naming that incident; nothing else
changes: the verdict stays in the report, the anchor is not moved, no row is written. An
anchor not on the list, a listed chain whose anchor differs from its entry in any of the
four, a listed chain whose signed head id is back with another hash (HEAD_CHANGED, the
tampering case the verdict exists for) or which holds live frames again (HEAD_MISSING), and
every other refusal of a listed chain stay ERROR. Telling a headless chain with live frames
from an empty one costs one probe of the chain index (`LIMIT 1`), made only for a truncation
whose head row is absent, for every such anchor, listed or not (its state is in the ERROR). The
WARNING is rate-limited per anchor: once the first time a process sees it, then at most once
every `KNOWN_ORPHAN_WARN_EVERY` (an hour), timed by the database's clock.
Six anchors are then about six lines an hour, not six a minute; a restart states them again,
so every process's log names them. The list is validated whole when it is read (the exact
keys, a canonical uuid, positive integers, a 16-hex kid, one entry per chain, no duplicated
JSON key); a malformed list is refused, logged at ERROR every tick, and excuses NOTHING (an
empty list is used), so a bad edit can only make the job louder.

THE WALK'S PLAN (divergence 050, ASDLC OI-51). The walk past a head must be served by the chain
index, and the planner does not choose it on its own. With `chain_id = %s AND id > %s ORDER BY
id`, `chain_id` is a constant to the planner, so ORDER BY id is all it has to produce, and the
primary key produces it: an Index Scan on `invocation_frames_pkey` from the head with the chain
as a Filter. The planner costs that from the chain's share of the table, spread evenly over the
ids; on production (2026-09-27, as `meshy_app`, now `meshychat_app`) a chain holding 2.6% of the frames, anchored at a
head with 1.9M ids after it, was planned that way (it expected 49k of its frames there), a read
of index entries until 5000 of the chain's frames turn up or the table ends: ~1.9M entries in
the OI's measurement, with the anchor row held FOR UPDATE. A chain whose frames stopped long ago
has none there, which is the case this read is worst at. So `frames_past` spells the chain as
the range `chain_id >= %s AND chain_id <= %s` (the same rows: a uuid equal to itself and to
nothing else) and orders by `chain_id DESC, id`. Nothing but the chain index (chain_id, id
DESC), read backward, yields that order without a Sort: no other index leads with chain_id
except partial ones this query does not imply. Sorts are then switched off for the walk's own
transaction (`set_config('enable_sort', 'off', true)`, the mechanism of divergence 046), which
takes away the one alternative left, a bitmap read of the chain's frames past the head sorted
whole (what the planner picks for a chain it believes small, unbounded by the LIMIT when it is
not). What remains is the ordered range of the chain index, which starts at the head and stops
at the LIMIT. Proven on production's own planner as `meshy_app` (now `meshychat_app`), EXPLAIN only
(docs/DIVERGENCES.md 050).

RETENTION (STATUS D08, divergence 052). `apps/calls/frame_retention.py` retires a chain whose
root grant has been dead past the owner's window: its anchor row moves into
`invocation_chain_retention` and its frames are deleted newest first, a batch a call. Between
two batches the chain holds a linked prefix and NO anchor, so pass 1 or 3 would anchor that
prefix from its root, and the next batch would delete the new head: a `truncated` verdict this
job made itself. So a chain with no anchor whose ledger row is 'pruning' is not anchored: the
verdict is RETIRING, not a refusal and not logged. It costs one probe of the ledger's primary
key, made only for a chain that has no anchor. The two cannot race into an anchor: retention
takes a chain only while holding its anchor row FOR UPDATE and deletes that row in the same
transaction as it writes the ledger row, so a reconcile that finds no anchor also sees the
ledger row. A chain whose ledger row is 'complete' or 'stopped' is anchored as any other (a
new epoch, or the linked prefix a stopped prune left).

CONCURRENCY with another shared-database writer: this job reads the anchor row FOR UPDATE and
writes it by compare-and-swap on (head_frame_id, frame_count). The frame writer's upsert only
moves the anchor forward (`frame_count >=`, with the old head still existing), so whichever
writes second writes a count at least as large.

SIGNING. `ChainHeadPayload` holds chain_id, head_frame_id, head_hash, and frame_count in RFC 8785
form (`apps.crypto.primitives.jcs`), signed with the server key
(`apps.calls.result_signing.server_result_signer`) and its `signing_kid`. The bytes used to
create and verify stored signatures must agree, or every stored signature fails verification.
`tests/test_chain_head.py` pins the bytes and signature. Frame ids above 2**53 - 1 cannot be
written in RFC 8785 form (`P.jcs` refuses them); a BIGSERIAL would need nine quadrillion frames
to reach one.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
import uuid
from pathlib import Path

from psycopg.rows import dict_row

from apps.calls import frame_audit
from apps.crypto import primitives as P
from apps.jobs import cursors
from apps.jobs.result import TickResult

log = logging.getLogger("meshychat.calls.chain_head")

#: Frames read per tick by each pass's reads; anchors verified per tick. At production's 49
#: anchors (2026-09-27) one tick is a whole lap of pass 2, so a finding recurs every minute:
#: the reason the known-orphans WARNING has a cadence of its own (below), not one per lap.
FRAMES_PER_TICK = 5000
ANCHORS_PER_TICK = 500
#: Frame ids re-read per tick by the rescan (pass 3).
RESCAN_PER_TICK = 5000

CURSOR_FRAMES = "chain_head.frames"
CURSOR_ANCHORS = "chain_head.anchors"
CURSOR_RESCAN = "chain_head.rescan"

ANCHORED, EXTENDED, CURRENT, RACED = "anchored", "extended", "current", "raced"
TRUNCATED, BAD_SIGNATURE, UNKNOWN_KID = "truncated", "bad_signature", "unknown_signing_kid"
LINK_BREAK, ROOT_MISSING = "link_break", "root_missing"
#: STATUS D08 (divergence 052): the chain is being pruned by retention; nothing is anchored.
RETIRING = "retiring"
#: What a TRUNCATED verdict found (`Outcome.head_state`): the signed head's id is in the chain
#: with another hash; the head is gone and the chain still holds frames; the head is gone and
#: the chain holds none (the shape production's retention left, STATUS D08).
HEAD_CHANGED, HEAD_MISSING, CHAIN_EMPTY = "head_changed", "head_missing", "chain_empty"
#: Verdicts that are findings, logged at ERROR (the module docstring says what each moves).
REFUSALS = frozenset({TRUNCATED, BAD_SIGNATURE, UNKNOWN_KID, LINK_BREAK, ROOT_MISSING})
#: Verdicts after which a pass stops considering the chain this tick: the refusals, and a chain
#: retention is pruning (its frames are not this job's to anchor until the prune ends).
SETTLED = REFUSALS | {RETIRING}

#: STATUS D08: the committed known-orphans list, and the least time between two WARNINGs
#: about one listed anchor in one process (the first sighting always logs).
KNOWN_ORPHANS_PATH = Path(__file__).resolve().parent / "chain_head_known_orphans.json"
KNOWN_ORPHAN_WARN_EVERY = dt.timedelta(hours=1)


def payload_bytes(chain_id, head_frame_id: int, head_hash: str, frame_count: int) -> bytes:
    """Return the RFC 8785 bytes for a chain-head payload."""
    return P.jcs({"chain_id": str(uuid.UUID(str(chain_id))), "head_frame_id": int(head_frame_id),
                  "head_hash": head_hash, "frame_count": int(frame_count)})


def verify_signature(anchor: dict, keys: dict[str, bytes]) -> str | None:
    """None when the stored signature verifies under the key its kid names."""
    public = keys.get(anchor["signing_kid"] or "")
    if public is None:
        return UNKNOWN_KID
    msg = payload_bytes(anchor["chain_id"], anchor["head_frame_id"], anchor["head_hash"],
                        anchor["frame_count"])
    if not P.verify_strict(public, msg, bytes(anchor["head_signature"])):
        return BAD_SIGNATURE
    return None


@dataclasses.dataclass(frozen=True)
class Outcome:
    chain_id: str
    verdict: str
    #: Frames added to the anchor by this call.
    advanced: int
    #: Frames READ by this call (its bound), whatever it advanced.
    frames_read: int
    head_frame_id: int
    frame_count: int
    #: The read hit its limit: the chain may hold more frames past the new head.
    more: bool
    #: The kid of the anchor this outcome leaves (None when there is none): with chain_id,
    #: head_frame_id and frame_count, the key a known-orphans entry is matched on.
    signing_kid: str | None = None
    #: TRUNCATED only: HEAD_CHANGED, HEAD_MISSING or CHAIN_EMPTY (None for every other
    #: verdict). Only CHAIN_EMPTY is the state a known-orphans entry records.
    head_state: str | None = None


def frames_past(cur, chain_id, head: int, limit: int) -> list[dict]:
    """The frames of `chain_id` with an id greater than `head`, in id order, at most `limit`:
    the rows and order of `WHERE chain_id = %s AND id > %s ORDER BY id LIMIT %s`, read on the
    chain index (module docstring, THE WALK'S PLAN; divergence 050). `cur` must be inside a
    transaction: the plan pin is local to it, and on an autocommit statement of its own it
    would end with that statement."""
    cur.execute("SELECT set_config('enable_sort', 'off', true)")
    cur.execute("SELECT id, frame_hash, previous_frame_hash FROM invocation_frames "
                "WHERE chain_id >= %s AND chain_id <= %s AND id > %s "
                "ORDER BY chain_id DESC, id LIMIT %s", (chain_id, chain_id, head, int(limit)))
    return cur.fetchall()


def reconcile(conn, chain_id, *, signer, keys: dict[str, bytes], limit: int) -> Outcome:
    """Verify `chain_id`'s anchor and extend it by up to `limit` frames, in one transaction.
    `limit` 0 verifies only. `signer` is an `apps.calls.result_signing.ResultSigner`."""
    chain = str(chain_id)
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT chain_id, head_frame_id, head_hash, frame_count, "
                        "head_signature, signing_kid FROM invocation_chain_heads "
                        "WHERE chain_id = %s FOR UPDATE", (chain_id,))
            anchor = cur.fetchone()
            if anchor is not None:
                head, head_hash, count = (anchor["head_frame_id"], anchor["head_hash"],
                                          anchor["frame_count"])
                refused, head_state = verify_signature(anchor, keys), None
                if refused is None:
                    cur.execute("SELECT frame_hash FROM invocation_frames "
                                "WHERE id = %s AND chain_id = %s", (head, chain_id))
                    now = cur.fetchone()
                    if now is not None and now["frame_hash"] != head_hash:
                        refused, head_state = TRUNCATED, HEAD_CHANGED
                    elif now is None:
                        cur.execute("SELECT id FROM invocation_frames WHERE chain_id = %s "
                                    "LIMIT 1", (chain_id,))
                        refused = TRUNCATED
                        head_state = CHAIN_EMPTY if cur.fetchone() is None else HEAD_MISSING
                if refused is not None:
                    return Outcome(chain, refused, 0, 0, head, count, False,
                                   anchor["signing_kid"], head_state)
            else:
                cur.execute("SELECT status FROM invocation_chain_retention WHERE chain_id = %s",
                            (chain_id,))
                ledger = cur.fetchone()
                if ledger is not None and ledger["status"] == "pruning":
                    return Outcome(chain, RETIRING, 0, 0, 0, 0, False)
                head, head_hash, count = 0, None, 0
            rows = frames_past(cur, chain_id, head, limit) if limit > 0 else []
            verdict, advanced = None, 0
            new_head, new_hash = head, head_hash
            for r in rows:
                if r["previous_frame_hash"] != new_hash:
                    verdict = ROOT_MISSING if anchor is None and advanced == 0 else LINK_BREAK
                    break
                new_head, new_hash, advanced = r["id"], r["frame_hash"], advanced + 1
            more = verdict is None and len(rows) >= limit > 0
            if advanced == 0:
                return Outcome(chain, verdict or CURRENT, 0, len(rows), head, count, more,
                               None if anchor is None else anchor["signing_kid"])
            new_count = count + advanced
            sig = signer.private_key.sign(payload_bytes(chain_id, new_head, new_hash,
                                                        new_count))
            if anchor is None:
                cur.execute("INSERT INTO invocation_chain_heads (chain_id, head_frame_id, "
                            "head_hash, frame_count, head_signature, signing_kid) "
                            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (chain_id) DO NOTHING",
                            (chain_id, new_head, new_hash, new_count, sig, signer.kid))
                if cur.rowcount != 1:       # another writer anchored it first; next tick
                    return Outcome(chain, RACED, 0, len(rows), 0, 0, True)
            else:
                cur.execute("UPDATE invocation_chain_heads SET head_frame_id = %s, "
                            "head_hash = %s, frame_count = %s, head_signature = %s, "
                            "signing_kid = %s, updated_at = now() "
                            "WHERE chain_id = %s AND head_frame_id = %s AND frame_count = %s",
                            (new_head, new_hash, new_count, sig, signer.kid, chain_id, head,
                             count))
    return Outcome(chain, verdict or (ANCHORED if anchor is None else EXTENDED), advanced,
                   len(rows), new_head, new_count, more, signer.kid)


# ------------------------------------------------------------------ the known orphans (D08)

class KnownOrphansError(ValueError):
    """The known-orphans list is malformed: refused whole, never half-loaded."""


_ENTRY_KEYS = frozenset({"chain_id", "head_frame_id", "frame_count", "signing_kid", "incident"})
_KID = re.compile(r"[0-9a-f]{16}")


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def parse_known_orphans(doc) -> dict[tuple[str, int, int, str], str]:
    """(chain_id, head_frame_id, frame_count, signing_kid) -> incident, from the parsed JSON
    document, or KnownOrphansError naming the first thing wrong with it."""
    if not isinstance(doc, dict) or set(doc) != {"about", "orphans"}:
        raise KnownOrphansError("the document must be an object with exactly 'about' and "
                                "'orphans'")
    if not isinstance(doc["about"], str) or not doc["about"].strip():
        raise KnownOrphansError("'about' must be a non-empty string")
    if not isinstance(doc["orphans"], list):
        raise KnownOrphansError("'orphans' must be a list")
    out: dict[tuple[str, int, int, str], str] = {}
    chains: set[str] = set()
    for i, e in enumerate(doc["orphans"]):
        where = f"orphans[{i}]"
        if not isinstance(e, dict) or set(e) != _ENTRY_KEYS:
            raise KnownOrphansError(f"{where} must be an object with exactly the keys "
                                    f"{sorted(_ENTRY_KEYS)}")
        chain = e["chain_id"]
        try:
            canonical = str(uuid.UUID(chain)) if isinstance(chain, str) else None
        except ValueError:
            canonical = None
        if canonical is None or canonical != chain:
            raise KnownOrphansError(f"{where}.chain_id must be a uuid in canonical lowercase "
                                    f"hyphenated form, not {chain!r}")
        for k in ("head_frame_id", "frame_count"):
            if not _positive_int(e[k]):
                raise KnownOrphansError(f"{where}.{k} must be an integer > 0, not {e[k]!r}")
        if not isinstance(e["signing_kid"], str) or not _KID.fullmatch(e["signing_kid"]):
            raise KnownOrphansError(f"{where}.signing_kid must be 16 lowercase hex characters "
                                    f"(the server kid rule), not {e['signing_kid']!r}")
        if not isinstance(e["incident"], str) or not e["incident"].strip():
            raise KnownOrphansError(f"{where}.incident must be a non-empty string")
        if chain in chains:
            raise KnownOrphansError(f"{where}: chain {chain} is listed twice")
        chains.add(chain)
        out[(chain, e["head_frame_id"], e["frame_count"], e["signing_kid"])] = e["incident"]
    return out


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise KnownOrphansError(f"a JSON object repeats a key: {sorted(keys)}")
    return dict(pairs)


def load_known_orphans(path: Path | None = None) -> dict[tuple[str, int, int, str], str]:
    """Read and validate the list at `path` (default: the committed one)."""
    path = KNOWN_ORPHANS_PATH if path is None else path
    try:
        text = Path(path).read_text(encoding="utf-8")
        doc = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except KnownOrphansError:
        raise
    except (OSError, ValueError) as exc:
        raise KnownOrphansError(f"{type(exc).__name__}: {exc}") from exc
    return parse_known_orphans(doc)


class KnownOrphans:
    """A validated known-orphans list and, per listed anchor, the database time this
    process last logged its WARNING. One per process (`run_job`); a test makes its own."""

    def __init__(self, entries: dict[tuple[str, int, int, str], str]):
        self.entries = dict(entries)
        self.by_chain = {k[0]: k for k in entries}
        #: listed anchor key -> database time of its last WARNING. Keys only ever come from
        #: `entries` (`due` refuses any other), so it holds at most one per list entry.
        self._warned = {}

    def incident(self, o: Outcome) -> str | None:
        """The incident of `o`'s list entry when `o` is a truncation of an anchor that
        matches an entry exactly AND is in the state the list records (the signed head gone
        and no live frame in the chain, CHAIN_EMPTY); else None."""
        if o.verdict != TRUNCATED or o.head_state != CHAIN_EMPTY:
            return None
        return self.entries.get((o.chain_id, o.head_frame_id, o.frame_count, o.signing_kid))

    def due(self, o: Outcome, now: dt.datetime) -> bool:
        """True when a WARNING about listed `o` should be logged at database time `now`: the
        first time this process sees it, and then once `KNOWN_ORPHAN_WARN_EVERY` has passed
        (or the clock went back). Records `now` when it says True.

        `now` is compared and stored in UTC: Python adds and compares two datetimes of the same
        zone on their WALL clock, so in a session zone with summer time two instants an hour
        apart across the autumn change (02:30 CEST, then 02:30 CET) compare equal and the
        second WARNING was suppressed (memory-bxxxxx, mg388 on 25 Oct)."""
        if now.tzinfo is not None:
            now = now.astimezone(dt.timezone.utc)
        key = (o.chain_id, o.head_frame_id, o.frame_count, o.signing_kid)
        if key not in self.entries:
            return False
        last = self._warned.get(key)
        if last is not None and last <= now < last + KNOWN_ORPHAN_WARN_EVERY:
            return False
        self._warned[key] = now
        return True


#: The process's list, read by `run_job` on its first tick; None until then, and while the
#: committed file is malformed (each tick tries again and logs the refusal).
_ORPHANS: KnownOrphans | None = None


def _known_orphans() -> KnownOrphans | None:
    global _ORPHANS
    if _ORPHANS is None:
        try:
            entries = load_known_orphans(KNOWN_ORPHANS_PATH)
        except KnownOrphansError as exc:
            log.error("invocation chain-head: the known-orphans list %s is malformed and was "
                      "refused (%s); NOTHING is excused, every truncation is logged at ERROR "
                      "until it is fixed", KNOWN_ORPHANS_PATH, exc)
            return None
        _ORPHANS = KnownOrphans(entries)
    return _ORPHANS


# ------------------------------------------------------------------ the tick

@dataclasses.dataclass(frozen=True)
class Report:
    frames_scanned: int
    frames_read: int
    anchors_verified: int
    outcomes: tuple[Outcome, ...]
    frames_position: int
    anchors_position: str
    anchor_laps: int
    more: bool
    frames_rescanned: int = 0
    rescan_position: int = 0
    rescan_laps: int = 0
    #: The findings that matched a known-orphans entry exactly (logged at WARNING when due);
    #: still in `findings`, which keeps every refusal.
    known_orphans: tuple[Outcome, ...] = ()

    @property
    def findings(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if o.verdict in REFUSALS)


def _db_now(conn) -> dt.datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT now() AS now")
        row = cur.fetchone()
    return row["now"] if isinstance(row, dict) else row[0]


def _log(o: Outcome, orphans: KnownOrphans | None, when) -> bool:
    """Log a refusal. True when `o` is a known orphan (`when()`: the tick's database time)."""
    if o.verdict not in REFUSALS:
        return False
    incident = None if orphans is None else orphans.incident(o)
    if incident is not None:
        if orphans.due(o, when()):
            log.warning("invocation chain-head: chain %s: %s (anchored head %s, count %s, kid "
                        "%s) is a KNOWN ORPHAN, see %s; the anchor was not moved; repeated at "
                        "most once every %s while it stays so", o.chain_id, o.verdict,
                        o.head_frame_id, o.frame_count, o.signing_kid, incident,
                        KNOWN_ORPHAN_WARN_EVERY)
        return True
    listed = None if orphans is None else orphans.by_chain.get(o.chain_id)
    if listed is None:
        note = ""
    elif listed == (o.chain_id, o.head_frame_id, o.frame_count, o.signing_kid):
        note = ("; its known-orphans entry matches this anchor, but the list excuses only a "
                f"{TRUNCATED} verdict whose signed head is gone from a chain with no live "
                f"frame ({CHAIN_EMPTY}), so it is NOT excused")
    else:
        note = (f"; a known-orphans entry names this chain at head {listed[1]}, count "
                f"{listed[2]}, kid {listed[3]}, which this anchor does not match, so it is NOT "
                "excused")
    verdict = o.verdict if o.head_state is None else f"{o.verdict} ({o.head_state})"
    log.error("invocation chain-head ALERT: chain %s: %s (anchored head %s, count %s, kid %s); "
              "the anchor was not moved%s", o.chain_id, verdict, o.head_frame_id,
              o.frame_count, o.signing_kid, note)
    return False


def tick(conn, *, signer, keys: dict[str, bytes], frames_per_tick: int = FRAMES_PER_TICK,
         anchors_per_tick: int = ANCHORS_PER_TICK, rescan_per_tick: int = RESCAN_PER_TICK,
         cursor_prefix: str = "", orphans: KnownOrphans | None = None,
         now: dt.datetime | None = None) -> Report:
    """One tick of the three passes. `conn`: autocommit; each chain's reconcile is a
    transaction of its own, and each cursor is saved after the work it covers. `orphans`:
    the known-orphans list (None: every refusal is an ERROR). `now`: the database time the
    WARNING cadence is judged at; None reads `now()` once, when a listed orphan is seen."""
    if frames_per_tick <= 0 or anchors_per_tick <= 0 or rescan_per_tick <= 0:
        raise ValueError("every bound must be > 0; a zero bound would be a silent no-op")
    name_f, name_a = cursor_prefix + CURSOR_FRAMES, cursor_prefix + CURSOR_ANCHORS
    name_r = cursor_prefix + CURSOR_RESCAN
    outcomes: list[Outcome] = []
    known: list[Outcome] = []
    clock = [now]

    def when() -> dt.datetime:
        if clock[0] is None:
            clock[0] = _db_now(conn)
        return clock[0]

    def report(o: Outcome) -> None:
        outcomes.append(o)
        if _log(o, orphans, when):
            known.append(o)
    budget = frames_per_tick
    more = False

    # ---- 1. frames past the cursor, each chain extended to cover them --------------
    pos = cursors.read_or_start(conn, name_f)["position"]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id, chain_id FROM invocation_frames WHERE id > %s "
                    "ORDER BY id LIMIT %s", (pos, int(frames_per_tick)))
        scanned = cur.fetchall()
    covered = pos
    done: dict[str, Outcome] = {}
    for row in scanned:
        if row["chain_id"] is not None:
            chain = str(row["chain_id"])
            o = done.get(chain)
            if o is None or (o.verdict not in SETTLED and o.head_frame_id < row["id"]):
                if budget <= 0:
                    more = True
                    break
                o = reconcile(conn, row["chain_id"], signer=signer, keys=keys, limit=budget)
                budget -= o.frames_read
                done[chain] = o
                report(o)
            if o.verdict not in SETTLED and o.head_frame_id < row["id"]:
                more = True                       # the budget ran out inside this chain
                break
        covered = row["id"]
    else:
        more = more or len(scanned) >= frames_per_tick
    cursors.save(conn, name_f, position=covered)

    # ---- 2. the rolling verification of every anchor --------------------------------
    state = cursors.read_or_start(conn, name_a)
    after, laps = state["position_key"], state["laps"]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT chain_id FROM invocation_chain_heads "
                    "WHERE (%s::uuid IS NULL OR chain_id > %s::uuid) "
                    "ORDER BY chain_id LIMIT %s", (after, after, int(anchors_per_tick)))
        anchors = [r["chain_id"] for r in cur.fetchall()]
    for chain_id in anchors:
        o = reconcile(conn, chain_id, signer=signer, keys=keys, limit=max(0, budget))
        budget -= o.frames_read
        if str(chain_id) not in done or o.advanced or o.verdict in REFUSALS:
            report(o)
    if len(anchors) < anchors_per_tick:
        after, laps = None, laps + 1
    else:
        after = str(anchors[-1])
    cursors.save(conn, name_a, key=after, laps=laps)

    # ---- 3. the rolling rescan of frame ids, for chains with no anchor ---------------
    state = cursors.read_or_start(conn, name_r)
    r_pos, r_laps = state["position"], state["laps"]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT f.id, f.chain_id, h.chain_id AS anchor FROM invocation_frames f "
                    "LEFT JOIN invocation_chain_heads h ON h.chain_id = f.chain_id "
                    "WHERE f.id > %s AND f.id <= %s ORDER BY f.id LIMIT %s",
                    (r_pos, covered, int(rescan_per_tick)))
        rescanned = cur.fetchall()
    for row in rescanned:
        chain = None if row["chain_id"] is None else str(row["chain_id"])
        if chain is not None and row["anchor"] is None and chain not in done:
            if budget <= 0:
                break                              # the next tick starts at this frame
            o = reconcile(conn, row["chain_id"], signer=signer, keys=keys, limit=budget)
            budget -= o.frames_read
            done[chain] = o
            report(o)
            if o.verdict not in SETTLED and o.head_frame_id < row["id"]:
                break                              # the budget ran out inside this chain
        r_pos = row["id"]
    else:
        if len(rescanned) < rescan_per_tick:
            r_pos, r_laps = 0, r_laps + 1
    cursors.save(conn, name_r, position=r_pos, laps=r_laps)

    return Report(frames_scanned=len(scanned),
                  frames_read=sum(o.frames_read for o in outcomes),
                  anchors_verified=len(anchors), outcomes=tuple(outcomes),
                  frames_position=covered, anchors_position=after or "",
                  anchor_laps=laps, more=more, frames_rescanned=len(rescanned),
                  rescan_position=r_pos, rescan_laps=r_laps, known_orphans=tuple(known))


def run_job(conn, job) -> TickResult:
    """The registered body of `chain_head_reconcile`. Without a server signing key nothing
    can be anchored: it warns and does nothing (the anchors pass needs the key too, since a
    verified anchor may have to be extended)."""
    from apps.calls.result_signing import NoResultSigningKey, server_result_signer
    try:
        signer = server_result_signer()
    except NoResultSigningKey as exc:
        log.warning("chain-head reconcile: %s; no anchor is written or verified", exc)
        return TickResult(rows=0, more_work_pending=False)
    report = tick(conn, signer=signer, keys=frame_audit.verifier_keys(),
                  frames_per_tick=job.max_batch, anchors_per_tick=ANCHORS_PER_TICK,
                  orphans=_known_orphans())
    unexcused = len(report.findings) - len(report.known_orphans)
    if unexcused:
        log.error("invocation chain-head reconcile: %d anchor(s) refused this tick (%d more "
                  "on the known-orphans list)", unexcused, len(report.known_orphans))
    return TickResult(rows=report.frames_scanned + report.frames_rescanned
                      + report.frames_read, more_work_pending=report.more)


__all__ = ["ANCHORS_PER_TICK", "CHAIN_EMPTY", "FRAMES_PER_TICK", "HEAD_CHANGED",
           "HEAD_MISSING", "KNOWN_ORPHANS_PATH", "KNOWN_ORPHAN_WARN_EVERY", "KnownOrphans",
           "KnownOrphansError", "Outcome", "REFUSALS", "RESCAN_PER_TICK", "RETIRING", "Report",
           "SETTLED", "frames_past", "load_known_orphans", "parse_known_orphans", "payload_bytes",
           "reconcile", "run_job", "tick", "verify_signature"]
