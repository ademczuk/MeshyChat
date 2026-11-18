"""The invocation-frame audit, INCREMENTAL (STATUS D09; rule ii).

Nothing is read twice in a tick, and no tick reads more than its bound. A full pass (every chain
walked from its root, every standalone frame re-hashed, hourly) was measured at about 44 GB/day
of reads and timed out on 1.7M standalone frames (docs/V1-DESIGN-RULES.md B5,
retention-mitigation-list.md), so this audit is incremental.

WHAT IS CHECKED, per frame, in this order:
  * link: a chained frame's `previous_frame_hash` equals the frame_hash of the frame before
    it in its chain (the greatest id below its own), NULL when there is none (the root);
  * anchor: its `parent_hash` equals the chain root's frame_hash (the least id in the
    chain), NULL at the root;
  * a standalone frame (`chain_id IS NULL`) carries neither (UnexpectedLink);
  * self: the decision is `allow` or `deny`, the stored frame_hash recomputes from the
    row, and a stored signature verifies under the key its `signing_kid` names.
Each frame is checked on its own against its neighbours, by two index probes on
`invocation_frames_chain_idx` (chain_id, id DESC), so a break is reported at the frame that
has it and at no other.

THE PROBES' PLAN (divergence 050, ASDLC OI-51). Written as `chain_id = %s ... ORDER BY id`, the
root read and the predecessor read leave the planner only ORDER BY id to produce (the chain is a
constant to it), and for a chain that holds a large share of the table it produces that from the
primary key with the chain as a Filter, costing it as if the chain's frames were spread evenly
over the ids. On production (2026-09-27, as `meshy_app`, now `meshychat_app`) the root read of the largest chain, and
of one holding 2.6% of the frames, was planned as a walk of the primary key from the table's
first id, and the predecessor read of the latter as a walk backward from the frame: a scan of
every id until the chain turns up. So both spell the chain as the range `chain_id >= %s AND
chain_id <= %s` (the same rows) and order by `chain_id` and `id` in the chain index's own
directions, which nothing but the chain index yields without a Sort, and `audit_tick` switches
sorts off for its own transaction (`set_config('enable_sort', 'off', true)`, as divergence 046
does), which takes away the bitmap-and-sort read of a whole chain. The probe is then one descent
of the chain index. The tick's other reads are primary-key ranges already in the order they ask
for, which a Sort could only add to.

TWO PASSES A TICK, each bounded, each with a cursor in `background_job_cursors`
(sql_meshychat/023):
  1. NEW FRAMES, by id past `frame_audit.new`. Its first tick starts it at the newest frame
     that exists, so the job audits what arrives from then on without first walking the
     history in this pass.
  2. THE ROLLING RECHECK, by id past `frame_audit.recheck`, up to the new pass's cursor,
     then from 0 again (`laps` counts the wraps). It is how the history is audited, how a
     frame mutated AFTER it was first audited is found, and how a frame that
     committed after the new pass had passed its id is found: a BIGSERIAL id is drawn at
     INSERT, so frames of two chains can commit out of id order (the property D19 fixed for
     the mesh walks; frame writes occur outside this module).
A lap takes (frames / RECHECK_PER_TICK) ticks: 2.6M frames at 5000 a tick every 5 minutes
is about 1.8 days. The trade is that a frame mutated long ago can go unseen for up to a lap in
exchange for bounded reads per tick. The constants are here to be tuned.

A FINDING IS AN ALERT, NOT A FAILED TICK: each is logged at ERROR with its frame and
reason and returned in the report. The recheck reports it again every lap, so a break stays
loud. Nothing is written about a frame.

KEYS. Signed frames name their key by `signing_kid`, the SERVER kid rule
(`apps.crypto.primitives.server_kid`: sha256(raw public key)[..8], lower hex), resolved
against the server's current JWT public key and its history
(`MESHYCHAT_JWT_PUBLIC_KEY_HISTORY_PEM_PATHS`). With no key configured every
signed frame reports UnknownSigningKid. Signatures are checked with `verify_strict`.

`invocation_frames` has no row-level security and meshychat_app holds SELECT on it (sql/060,
062), so the passes read it on the job's own connection with no context.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging

from psycopg.rows import dict_row

from apps.crypto import primitives as P
from apps.jobs import cursors
from apps.jobs.result import TickResult

log = logging.getLogger("meshychat.calls.frame_audit")

#: Invocation-frame signature domain; the `meshy:` prefix is a compatibility identifier.
FRAME_SIGNATURE_DOMAIN = b"meshy:invocation-frame:1:"

#: Per tick: frames audited on arrival, and frames re-verified by the rolling recheck.
NEW_PER_TICK = 5000
RECHECK_PER_TICK = 5000

CURSOR_NEW = "frame_audit.new"
CURSOR_RECHECK = "frame_audit.recheck"

# Break reasons used in findings and logs.
HASH_MISMATCH = "HashMismatch"
LINK_MISMATCH = "LinkMismatch"
PARENT_HASH_MISMATCH = "ParentHashMismatch"
UNEXPECTED_LINK = "UnexpectedLink"
MALFORMED_DECISION = "MalformedDecision"
SIGNATURE_MISMATCH = "SignatureMismatch"
MISSING_SIGNING_KID = "MissingSigningKid"
UNKNOWN_SIGNING_KID = "UnknownSigningKid"

FRAME_COLUMNS = (
    "id", "chain_id", "capability_ref", "delegation_depth", "invocation_nonce",
    "previous_frame_hash", "parent_hash", "frame_hash", "decision", "deny_reason", "call_id",
    "caller_owner_id", "caller_peer_id", "target_owner_id", "target_peer_id", "skill_name",
    "mandate_hash", "flow_guard_mode", "flow_root_kind", "continuation_nonce",
    "frame_signature", "signing_kid")
_SELECT = "SELECT " + ", ".join(FRAME_COLUMNS) + " FROM invocation_frames"
#: The tick's first statement: every read of one tick sees one snapshot (audit_tick says why).
_SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"


# ------------------------------------------------------------------ the hash, the self check

def _uuid_or_none(v):
    return None if v is None else str(v).lower()


def canonical_frame_bytes(row: dict, previous_frame_hash: str | None) -> bytes:
    """Encode the frame-hash payload with fixed field order and compact JSON.

    The four optional metadata fields are left OUT when None so a frame from before them
    hashes as it did. Every other None is written as null, UUIDs are lowercase hyphenated
    strings, and `json.dumps(ensure_ascii=False)` supplies the JSON minimal escapes (`\"`,
    `\\`, `\b`, `\f`, `\n`, `\r`, `\t`, and `\\u00xx` for other controls; everything else raw).
    The bytes must match stored hashes and signatures, or sound frames report a mismatch.
    `tests/test_frame_audit.py` pins the resulting bytes.
    """
    payload = {
        "chain_id": _uuid_or_none(row["chain_id"]),
        "capability_ref": _uuid_or_none(row["capability_ref"]),
        "delegation_depth": int(row["delegation_depth"]),
        "invocation_nonce": row["invocation_nonce"],
        "decision": row["decision"],
        "deny_reason": row["deny_reason"],
        "call_id": row["call_id"],
        "caller_owner_id": _uuid_or_none(row["caller_owner_id"]),
        "caller_peer_id": _uuid_or_none(row["caller_peer_id"]),
        "target_owner_id": _uuid_or_none(row["target_owner_id"]),
        "target_peer_id": _uuid_or_none(row["target_peer_id"]),
        "skill_name": row["skill_name"],
    }
    for optional in ("mandate_hash", "flow_guard_mode", "flow_root_kind",
                     "continuation_nonce"):
        if row.get(optional) is not None:
            payload[optional] = row[optional]
    payload["previous_frame_hash"] = previous_frame_hash
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def compute_frame_hash(row: dict, previous_frame_hash: str | None) -> str:
    """Return the SHA-256 hex digest of the canonical payload."""
    return hashlib.sha256(canonical_frame_bytes(row, previous_frame_hash)).hexdigest()


def signature_material(frame_hash: str) -> bytes:
    """Return the domain separator followed by the hash's hex bytes."""
    return FRAME_SIGNATURE_DOMAIN + frame_hash.encode("ascii")


def self_check(row: dict, keys: dict[str, bytes]) -> str | None:
    """Check decision validity, the recomputed hash, and the signature.
    `keys` maps a server kid to its 32 raw public-key bytes. None when the frame is sound."""
    if row["decision"] not in ("allow", "deny"):
        return MALFORMED_DECISION
    if compute_frame_hash(row, row["previous_frame_hash"]) != row["frame_hash"]:
        return HASH_MISMATCH
    sig = row["frame_signature"]
    if sig is None:
        return None                       # a legacy unsigned frame
    kid = row["signing_kid"]
    if not kid:
        return MISSING_SIGNING_KID
    public = keys.get(kid)
    if public is None:
        return UNKNOWN_SIGNING_KID
    if not P.verify_strict(public, signature_material(row["frame_hash"]), bytes(sig)):
        return SIGNATURE_MISMATCH
    return None


def verifier_keys() -> dict[str, bytes]:
    """Return kid-to-raw-public-key mappings from the server's current key and its history.
    A key that does not parse is skipped; the boot path validates the configured key and
    history is best effort here."""
    from apps.authapi import tokens
    from apps.authapi.config import get_config
    keys: dict[str, bytes] = {}
    try:
        pems = get_config().verify_keys()
    except Exception as exc:  # noqa: BLE001 - an unconfigured server audits with no keys
        log.warning("frame audit: no verifier keys (%s: %s); signed frames will report %s",
                    type(exc).__name__, exc, UNKNOWN_SIGNING_KID)
        return keys
    for pem in pems:
        try:
            raw = tokens.public_key_raw(pem)
        except Exception:  # noqa: BLE001
            continue
        keys[P.server_kid(raw)] = raw
    return keys


# ------------------------------------------------------------------ one frame, in its chain

class _Chains:
    """Per-tick lookups of a chain's root hash and a frame's predecessor, each one probe of
    (chain_id, id DESC). The root is cached for the tick; a predecessor inside the batch is
    taken from the batch. The probes are one descent of the chain index only under the plan
    pin `audit_tick` sets for its transaction (module docstring, THE PROBES' PLAN)."""

    def __init__(self, cur):
        self.cur = cur
        self.roots: dict[str, tuple[int, str]] = {}

    def root(self, chain_id) -> tuple[int, str]:
        key = str(chain_id)
        if key not in self.roots:
            # the least id of the chain: the chain index read backward (divergence 050)
            self.cur.execute("SELECT id, frame_hash FROM invocation_frames "
                             "WHERE chain_id >= %s AND chain_id <= %s "
                             "ORDER BY chain_id DESC, id ASC LIMIT 1", (chain_id, chain_id))
            r = self.cur.fetchone()
            self.roots[key] = (r["id"], r["frame_hash"])
        return self.roots[key]

    def predecessor_hash(self, chain_id, frame_id: int) -> str | None:
        # the greatest id below `frame_id`: the chain index read forward (divergence 050)
        self.cur.execute("SELECT frame_hash FROM invocation_frames "
                         "WHERE chain_id >= %s AND chain_id <= %s AND id < %s "
                         "ORDER BY chain_id ASC, id DESC LIMIT 1", (chain_id, chain_id, frame_id))
        r = self.cur.fetchone()
        return None if r is None else r["frame_hash"]


def check_frame(row: dict, chains: _Chains, keys: dict[str, bytes],
                prev_in_batch: dict[str, tuple[int, str]]) -> str | None:
    """Check one frame in order: link, anchor, then self."""
    if row["chain_id"] is None:
        if row["previous_frame_hash"] is not None or row["parent_hash"] is not None:
            return UNEXPECTED_LINK
        return self_check(row, keys)
    chain = str(row["chain_id"])
    seen = prev_in_batch.get(chain)
    if seen is not None and seen[0] < row["id"]:
        # The batch is one statement's snapshot, read in id order, and a chain's writers
        # are serialized under a per-chain advisory lock held to commit, so a chain's
        # frame is visible only after every earlier frame of that chain is: the last one
        # of this chain in the batch IS the predecessor, and no probe is needed.
        pred = seen[1]
    else:
        pred = chains.predecessor_hash(row["chain_id"], row["id"])
    prev_in_batch[chain] = (row["id"], row["frame_hash"])
    if row["previous_frame_hash"] != pred:
        return LINK_MISMATCH
    expected_parent = None if pred is None else chains.root(row["chain_id"])[1]
    if row["parent_hash"] != expected_parent:
        return PARENT_HASH_MISMATCH
    return self_check(row, keys)


# ------------------------------------------------------------------ the tick

@dataclasses.dataclass(frozen=True)
class Finding:
    frame_id: int
    chain_id: str | None
    reason: str
    found_by: str          #: "new" or "recheck"


@dataclasses.dataclass(frozen=True)
class Report:
    frames_new: int
    frames_rechecked: int
    findings: tuple[Finding, ...]
    new_position: int
    recheck_position: int
    laps: int
    #: The new pass read its whole bound: more frames may be waiting.
    more: bool


def _audit_rows(rows: list[dict], chains: _Chains, keys, found_by: str) -> list[Finding]:
    out: list[Finding] = []
    prev: dict[str, tuple[int, str]] = {}
    for row in rows:
        reason = check_frame(row, chains, keys, prev)
        if reason is not None:
            f = Finding(frame_id=row["id"], chain_id=_uuid_or_none(row["chain_id"]),
                        reason=reason, found_by=found_by)
            out.append(f)
            log.error("invocation-frame integrity ALERT: frame %s (chain %s) broken: %s "
                      "(found by the %s pass)", f.frame_id, f.chain_id, f.reason, found_by)
    return out


def audit_tick(conn, *, new_per_tick: int = NEW_PER_TICK,
               recheck_per_tick: int = RECHECK_PER_TICK, keys: dict[str, bytes] | None = None,
               cursor_prefix: str = "") -> Report:
    """One tick: the new frames past the new cursor, then the rolling recheck. `conn` is an
    autocommit connection; the tick is one transaction, so its two cursors move together
    with what it read or not at all. `cursor_prefix` names a separate pair of cursors (the
    tests' isolation; the job uses none)."""
    if new_per_tick <= 0 or recheck_per_tick <= 0:
        raise ValueError("both bounds must be > 0; a zero bound would be a silent no-op")
    keys = verifier_keys() if keys is None else keys
    name_new, name_re = cursor_prefix + CURSOR_NEW, cursor_prefix + CURSOR_RECHECK
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            # ONE SNAPSHOT FOR THE TICK (divergence 052). The batch is read by one statement
            # and each frame's predecessor and root by later ones; under READ COMMITTED a
            # retention batch committing in between (frame_retention.py deletes a dead chain
            # newest first) would leave a frame read before it with its predecessor gone, and
            # the check would report LinkMismatch for a frame retention removed. Every read
            # of the tick now sees the table as of its first statement.
            cur.execute(_SNAPSHOT)
            # The chain probes' plan pin, for this transaction only (divergence 050).
            cur.execute("SELECT set_config('enable_sort', 'off', true)")
            chains = _Chains(cur)
            # The cursors are read and saved on `conn` inside this transaction (apps/jobs/
            # cursors.py opens none of its own), so they see and move with its snapshot.
            state = cursors.read(conn, name_new)
            if state is None:
                # First tick: new frames are the ones from here on; the recheck walks the
                # history. The newest id is one probe of the primary key, not an aggregate.
                cur.execute("SELECT id FROM invocation_frames ORDER BY id DESC LIMIT 1")
                top = cur.fetchone()
                new_pos = top["id"] if top else 0
                cursors.save(conn, name_new, position=new_pos)
            else:
                new_pos = state["position"]
            cur.execute(_SELECT + " WHERE id > %s ORDER BY id LIMIT %s",
                        (new_pos, int(new_per_tick)))
            new_rows = cur.fetchall()
            findings = _audit_rows(new_rows, chains, keys, "new")
            if new_rows:
                new_pos = new_rows[-1]["id"]
                cursors.save(conn, name_new, position=new_pos)

            re_state = cursors.read_or_start(conn, name_re)
            re_pos, laps = re_state["position"], re_state["laps"]
            cur.execute(_SELECT + " WHERE id > %s AND id <= %s ORDER BY id LIMIT %s",
                        (re_pos, new_pos, int(recheck_per_tick)))
            re_rows = cur.fetchall()
            findings += _audit_rows(re_rows, chains, keys, "recheck")
            if len(re_rows) < recheck_per_tick:
                re_pos, laps = 0, laps + 1          # reached the new pass: wrap
            else:
                re_pos = re_rows[-1]["id"]
            cursors.save(conn, name_re, position=re_pos, laps=laps)
    return Report(frames_new=len(new_rows), frames_rechecked=len(re_rows),
                  findings=tuple(findings), new_position=new_pos, recheck_position=re_pos,
                  laps=laps, more=len(new_rows) >= new_per_tick)


def run_job(conn, job) -> TickResult:
    """The registered body of `frame_audit` (apps/jobs/registry.py): `max_batch` bounds the
    new pass and the recheck alike. Returns frames read; `more_work_pending` when the new
    pass read its whole bound."""
    report = audit_tick(conn, new_per_tick=job.max_batch, recheck_per_tick=job.max_batch)
    if report.findings:
        log.error("invocation-frame audit: %d integrity problem(s) found this tick",
                  len(report.findings))
    return TickResult(rows=report.frames_new + report.frames_rechecked,
                      more_work_pending=report.more)


__all__ = ["CURSOR_NEW", "CURSOR_RECHECK", "FRAME_SIGNATURE_DOMAIN", "Finding", "Report",
           "audit_tick", "canonical_frame_bytes", "check_frame", "compute_frame_hash",
           "run_job", "self_check", "signature_material", "verifier_keys"]
