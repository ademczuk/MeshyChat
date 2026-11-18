"""STATUS D08, divergence 052: invocation frames are deleted by DEAD ROOT GRANT, never by age alone.

Operator decision D-2026-09-27-20 (note 61): "a chain is deleted only after its root grant is
revoked or expired, plus a window, with an honest disposition of its anchor". Built as
`retire_invocation_chain` and `prune_standalone_invocation_frames` (sql_meshychat/026) and the
`frame_retention` job (apps/calls/frame_retention.py). These tests hold:

  * THE RULE, each refusal beside a control that differs in ONE variable and is deleted
    (docs/V1-DESIGN-RULES.md C1): a live grant, a grant dead inside the window, a future expiry,
    a chain with no grant row, a recent frame, no anchor, a stale anchor, a changed anchor, the
    policy switched off, the writer's lock held;
  * THE ANCHOR'S DISPOSITION: the anchor moves byte for byte into the ledger, still verifies,
    and the ledger says PRUNED_BY_RETENTION; a tampered retired anchor, a revived grant and a
    chain already short before retention are each reported;
  * NEWEST FIRST: between two batches what is left is a prefix that links to its root, the
    chain-head job anchors nothing for it (RETIRING, not TRUNCATED) and the frame audit
    reads it as intact even when a batch commits in the middle of its tick;
  * STOPS: a frame past the retired head, or a grant revived mid-prune, stops the chain, and
    what is left is anchored again with no refusal;
  * THE APP ROLE: may run both functions, may not write the ledger or the policy, and still
    may not delete a frame or run 012's `prune_invocation_frames`;
  * STANDALONE frames: kept unless the policy sets their window, then by their own age;
  * BOUNDED: a tick deletes at most its budget, and the delete is an ordered index range.

ISOLATION. The run's database is its own (tests/conftest.py). Every chain here is keyed by a
fresh grant; the tick tests use their own cursor prefix, and assert only about their chains.
Tests that change the policy row restore it.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_frame_retention.py -q
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.calls import chain_head as CH  # noqa: E402
from apps.calls import frame_audit as FA  # noqa: E402
from apps.calls import frame_retention as FR  # noqa: E402
from apps.calls.result_signing import ResultSigner  # noqa: E402
from apps.core import dbconn  # noqa: E402
from apps.jobs import registry  # noqa: E402
from tests.fixtures import frames as F  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
SIGNER = ResultSigner(private_key=Ed25519PrivateKey.from_private_bytes(F.SEED), kid=F.KID)


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def app():
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def policy(owner):
    """The policy row, restored after the test whatever it set."""
    before = owner.execute("SELECT chain_window_days, standalone_retain_days "
                           "FROM invocation_frame_retention_policy").fetchone()
    yield owner
    owner.execute("UPDATE invocation_frame_retention_policy SET chain_window_days = %s, "
                  "standalone_retain_days = %s", before)


# ------------------------------------------------------------------ the world

def _tenant(owner, tag: str) -> tuple[uuid.UUID, uuid.UUID]:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    owner.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                  "VALUES (%s, %s, 'product', NULL)",
                  (owner_id, f"d08-{tag}-{owner_id.hex[:8]}@example.test"))
    owner.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s, %s, %s, %s)",
                  (peer_id, owner_id, f"d08-{tag}-{peer_id.hex[:8]}", tag))
    return owner_id, peer_id


def _grant(owner, *, revoked_days_ago=None, expires_days=None, grant_id=None) -> uuid.UUID:
    """A root grant. `expires_days` < 0: expired that many days ago; > 0: expires in that many."""
    grantor, peer = _tenant(owner, "grantor")
    grantee, _ = _tenant(owner, "grantee")
    gid = grant_id or uuid.uuid4()
    owner.execute(
        "INSERT INTO skill_grants (id, grantor_owner_id, grantee_owner_id, granter_peer_id, "
        "skill_name, source, granted_at, revoked_at, expires_at) VALUES (%s, %s, %s, %s, %s, "
        "'direct', now() - interval '500 days', "
        "CASE WHEN %s::int IS NULL THEN NULL ELSE now() - make_interval(days => %s::int) END, "
        "CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END)",
        (gid, grantor, grantee, peer, f"d08.{gid.hex[:12]}", revoked_days_ago,
         revoked_days_ago, expires_days, expires_days))
    return gid


def _age(owner, chain_id, days: int) -> None:
    """Backdate a chain's frames. created_at is not in the frame hash (frame_audit.py), so the
    chain still verifies."""
    owner.execute("UPDATE invocation_frames SET created_at = now() - make_interval(days => %s) "
                  "WHERE chain_id = %s", (days, chain_id))


def _chain(owner, app, gid, n: int, *, age_days: int = 400, anchor: bool = True) -> list[dict]:
    frames = F.chain(owner, n, chain_id=gid, sign=True)
    _age(owner, gid, age_days)
    if anchor:
        o = CH.reconcile(app, gid, signer=SIGNER, keys=F.KEYS, limit=10_000)
        assert o.verdict == CH.ANCHORED and o.frame_count == n, o
    return frames


def _anchor(owner, chain_id):
    return owner.execute("SELECT chain_id, head_frame_id, head_hash, frame_count, "
                         "head_signature, signing_kid, updated_at FROM invocation_chain_heads "
                         "WHERE chain_id = %s", (chain_id,)).fetchone()


def _ids(owner, chain_id) -> list[int]:
    return [r[0] for r in owner.execute("SELECT id FROM invocation_frames WHERE chain_id = %s "
                                        "ORDER BY id", (chain_id,)).fetchall()]


def _ledger(owner, chain_id) -> dict | None:
    cur = owner.execute("SELECT * FROM invocation_chain_retention WHERE chain_id = %s",
                        (chain_id,))
    row = cur.fetchone()
    return None if row is None else dict(zip([d.name for d in cur.description], row))


def _retire(app, gid, *, batch: int = 1000, head=None, count=None) -> FR.Retired:
    a = app.execute("SELECT head_frame_id, frame_count FROM invocation_chain_heads "
                    "WHERE chain_id = %s", (gid,)).fetchone()
    if head is None:
        head = a[0] if a else 0
    if count is None:
        count = a[1] if a else 0
    return FR.retire(app, gid, expect_head=head, expect_count=count, batch=batch)


def _drain(app, gid, *, batch: int = 1000) -> list[FR.Retired]:
    out = [_retire(app, gid, batch=batch)]
    while out[-1].outcome in FR.DELETING:
        out.append(_retire(app, gid, batch=batch))
    return out


# ------------------------------------------------------------------ the rule, with controls

def test_a_chain_whose_root_grant_is_revoked_past_the_window_goes_and_a_live_ones_stays(
        owner, app):
    """One variable: revoked_at. Both chains 400 days quiet, anchored, signed."""
    dead, live = _grant(owner, revoked_days_ago=200), _grant(owner)
    dead_frames = _chain(owner, app, dead, 3)
    _chain(owner, app, live, 3)
    live_ids, live_anchor = _ids(owner, live), _anchor(owner, live)

    r_dead, r_live = _drain(app, dead), _retire(app, live)

    assert [r.outcome for r in r_dead] == [FR.COMPLETE] and r_dead[0].frames_deleted == 3
    assert _ids(owner, dead) == [] and _anchor(owner, dead) is None
    assert _ledger(owner, dead)["anchor_head_frame_id"] == dead_frames[-1]["id"]
    assert (r_live.outcome, r_live.reason, r_live.frames_deleted) == (
        FR.NOT_ELIGIBLE, "grant_live", 0)
    assert _ids(owner, live) == live_ids and _anchor(owner, live) == live_anchor, (
        "a live grant's chain or anchor changed")
    assert _ledger(owner, live) is None


def test_the_window_is_counted_in_days_from_the_grants_death(owner, app):
    """Default window 90 days: revoked 89 days ago stays, 91 days ago goes."""
    inside, outside = _grant(owner, revoked_days_ago=89), _grant(owner, revoked_days_ago=91)
    _chain(owner, app, inside, 2)
    _chain(owner, app, outside, 2)
    r_in, r_out = _retire(app, inside), _retire(app, outside)
    assert (r_in.outcome, r_in.reason) == (FR.NOT_ELIGIBLE, "grant_dead_within_window")
    assert len(_ids(owner, inside)) == 2
    assert r_out.outcome == FR.COMPLETE and _ids(owner, outside) == []


def test_an_expired_grant_is_dead_and_a_future_expiry_is_live(owner, app):
    """revoked_at NULL in both; one variable: expires_at."""
    expired, future = _grant(owner, expires_days=-100), _grant(owner, expires_days=10)
    _chain(owner, app, expired, 2)
    _chain(owner, app, future, 2)
    r_exp, r_fut = _retire(app, expired), _retire(app, future)
    assert r_exp.outcome == FR.COMPLETE and _ids(owner, expired) == []
    assert (r_fut.outcome, r_fut.reason) == (FR.NOT_ELIGIBLE, "grant_live")
    assert len(_ids(owner, future)) == 2


def test_a_chain_with_no_grant_row_is_never_taken(owner, app):
    """An absent grant is unknown, not dead. Control: the same chain shape under a revoked
    grant goes."""
    orphan_id, control = uuid.uuid4(), _grant(owner, revoked_days_ago=200)
    _chain(owner, app, orphan_id, 2)
    _chain(owner, app, control, 2)
    r = _retire(app, orphan_id)
    assert (r.outcome, r.reason) == (FR.NOT_ELIGIBLE, "no_root_grant")
    assert len(_ids(owner, orphan_id)) == 2 and _anchor(owner, orphan_id) is not None
    assert _retire(app, control).outcome == FR.COMPLETE


def test_a_frame_inside_the_window_keeps_the_chain(owner, app):
    """One variable: the age of the newest frame (10 days against 400)."""
    recent, quiet = _grant(owner, revoked_days_ago=200), _grant(owner, revoked_days_ago=200)
    _chain(owner, app, recent, 2, age_days=10)
    _chain(owner, app, quiet, 2, age_days=400)
    r = _retire(app, recent)
    assert (r.outcome, r.reason) == (FR.NOT_ELIGIBLE, "recent_frame")
    assert len(_ids(owner, recent)) == 2
    assert _retire(app, quiet).outcome == FR.COMPLETE


def test_a_chain_without_a_current_anchor_is_never_taken(owner, app):
    """No anchor; an anchor behind the newest frame; an anchor whose head is gone (the known
    orphans' shape, decision 53); an anchor other than the one the caller verified. Control:
    a current anchor goes."""
    unanchored = _grant(owner, revoked_days_ago=200)
    _chain(owner, app, unanchored, 2, anchor=False)
    behind = _grant(owner, revoked_days_ago=200)
    fb = _chain(owner, app, behind, 2)
    F.extend(owner, fb, chain_id=behind, sign=True)
    _age(owner, behind, 400)
    headless = _grant(owner, revoked_days_ago=200)
    fh = _chain(owner, app, headless, 3)
    owner.execute("DELETE FROM invocation_frames WHERE id = %s", (fh[-1]["id"],))
    changed = _grant(owner, revoked_days_ago=200)
    _chain(owner, app, changed, 2)
    control = _grant(owner, revoked_days_ago=200)
    _chain(owner, app, control, 2)

    assert (_retire(app, unanchored).reason, len(_ids(owner, unanchored))) == ("no_anchor", 2)
    assert (_retire(app, behind).reason, len(_ids(owner, behind))) == ("anchor_not_current", 3)
    assert (_retire(app, headless).reason, len(_ids(owner, headless))) == (
        "anchor_not_current", 2)
    a = _anchor(owner, changed)
    r = _retire(app, changed, head=a[1], count=a[3] + 1)
    assert (r.outcome, r.reason, len(_ids(owner, changed))) == (
        FR.NOT_ELIGIBLE, "anchor_changed", 2)
    assert _anchor(owner, headless) is not None, "an orphan anchor was moved"
    assert _retire(app, control).outcome == FR.COMPLETE


def test_the_policy_turns_chain_retention_off(owner, app, policy):
    gid = _grant(owner, revoked_days_ago=200)
    _chain(owner, app, gid, 2)
    policy.execute("UPDATE invocation_frame_retention_policy SET chain_window_days = NULL")
    r = _retire(app, gid)
    assert (r.outcome, r.frames_deleted, len(_ids(owner, gid))) == (FR.OFF, 0, 2)
    assert _ledger(owner, gid) is None
    policy.execute("UPDATE invocation_frame_retention_policy SET chain_window_days = 90")
    assert _retire(app, gid).outcome == FR.COMPLETE, "control: the default window takes it"


def test_the_writers_lock_uses_the_expected_key(owner, app):
    """The advisory-lock key must be the one the frame writers take: the first 8 bytes of
    the grant uuid, big-endian, as a signed 64-bit integer. The grant id starts 0xf0 so the
    key is negative; a sign error would take a different lock."""
    gid = _grant(owner, revoked_days_ago=200,
                 grant_id=uuid.UUID("f0" + uuid.uuid4().hex[2:]))
    _chain(owner, app, gid, 2)
    key = int.from_bytes(gid.bytes[:8], "big", signed=True)
    assert key < 0
    with psycopg.connect(DSN) as writer:
        writer.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        r = _retire(app, gid)
        assert (r.outcome, len(_ids(owner, gid))) == (FR.BUSY, 2)
        writer.rollback()
    assert _retire(app, gid).outcome == FR.COMPLETE, "control: the lock released, it goes"


@pytest.mark.parametrize("args", [(None, 0, 0, 10), ("chain", 0, 0, 0),
                                  ("chain", 0, 0, 50001), ("chain", 0, 0, None)])
def test_the_function_refuses_an_unbounded_call(owner, args):
    chain = uuid.uuid4() if args[0] == "chain" else None
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute("SELECT * FROM retire_invocation_chain(%s::uuid, %s, %s, %s::int)",
                      (chain, *args[1:]))


# ------------------------------------------------------------------ the anchor's disposition

def test_the_anchor_moves_whole_into_the_ledger_and_still_verifies(owner, app):
    gid = _grant(owner, revoked_days_ago=200)
    frames = _chain(owner, app, gid, 4)
    before = _anchor(owner, gid)
    _drain(app, gid)
    led = _ledger(owner, gid)
    assert _anchor(owner, gid) is None
    assert (led["chain_id"], led["anchor_head_frame_id"], led["anchor_head_hash"],
            led["anchor_frame_count"], bytes(led["anchor_signature"]),
            led["anchor_signing_kid"], led["anchor_updated_at"]) == (
        before[0], before[1], before[2], before[3], bytes(before[4]), before[5], before[6])
    assert (led["status"], led["frames_deleted"], led["window_days"]) == ("complete", 4, 90)
    assert led["grant_revoked_at"] is not None and led["dead_since"] == led["grant_revoked_at"]
    from nacl.signing import VerifyKey
    VerifyKey(F.PUBLIC).verify(CH.payload_bytes(gid, frames[-1]["id"], frames[-1]["frame_hash"],
                                                4), bytes(led["anchor_signature"]))
    assert FR.verify_record(led, F.KEYS, grant_live=False) == FR.PRUNED_BY_RETENTION


def test_the_ledger_reports_a_tampered_anchor_a_revived_grant_and_a_short_chain(owner, app):
    """Four completed chains, one variable each against the clean one."""
    clean, forged, revived, short = (_grant(owner, revoked_days_ago=200) for _ in range(4))
    revived_exp = _grant(owner, expires_days=-200)
    for g in (clean, forged, revived, short, revived_exp):
        fr = _chain(owner, app, g, 3)
        if g == short:          # a middle frame deleted BEFORE retention: the anchor is current
            owner.execute("DELETE FROM invocation_frames WHERE id = %s", (fr[1]["id"],))
        _drain(app, g)
    owner.execute("UPDATE invocation_chain_retention SET anchor_frame_count = 4 "
                  "WHERE chain_id = %s", (forged,))
    owner.execute("UPDATE skill_grants SET expires_at = now() + interval '30 days' "
                  "WHERE id = %s", (revived_exp,))

    def verdict(g, live=False):
        return FR.verify_record(_ledger(owner, g), F.KEYS, grant_live=live)

    assert verdict(clean) == FR.PRUNED_BY_RETENTION
    assert verdict(forged) == FR.BAD_SIGNATURE
    assert FR.verify_record(_ledger(owner, clean), {}, grant_live=False) == FR.UNKNOWN_KID
    assert verdict(revived, live=True) == FR.GRANT_LIVE_AGAIN
    assert verdict(short) == FR.COUNT_MISMATCH
    assert _ledger(owner, short)["frames_deleted"] == 2
    # The tick computes liveness itself: the expiry moved forward reads as live again.
    report = FR.tick(app, keys=F.KEYS, cursor_prefix=f"t{uuid.uuid4().hex[:8]}.",
                     ledger_per_tick=10_000)
    got = {v.chain_id: v.verdict for v in report.ledger}
    assert got[str(revived_exp)] == FR.GRANT_LIVE_AGAIN
    assert got[str(clean)] == FR.PRUNED_BY_RETENTION
    assert got[str(forged)] == FR.BAD_SIGNATURE


# ------------------------------------------------------------------ newest first, and the verifiers

def _links(owner, chain_id) -> list[str]:
    """The chain's frames checked as the frame audit checks them, each against its
    predecessor and root in the table as it is now: the reasons, [] when intact."""
    out = []
    with owner.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(FA._SELECT + " WHERE chain_id = %s ORDER BY id", (chain_id,))
        rows = cur.fetchall()
        chains = FA._Chains(cur)
        for row in rows:
            reason = FA.check_frame(row, chains, F.KEYS, {})
            if reason:
                out.append(reason)
    return out


def test_batches_go_newest_first_and_leave_a_prefix_that_verifies(owner, app):
    gid = _grant(owner, revoked_days_ago=200)
    frames = _chain(owner, app, gid, 7)
    ids = [f["id"] for f in frames]
    first = _retire(app, gid, batch=3)
    assert (first.outcome, first.frames_deleted) == (FR.STARTED, 3)
    assert _ids(owner, gid) == ids[:4], "the batch did not take the newest frames"
    assert _links(owner, gid) == [], "the prefix left between batches does not verify"
    second = _retire(app, gid, batch=3)
    assert (second.outcome, second.frames_deleted, _ids(owner, gid)) == (
        FR.PRUNING, 3, ids[:1])
    third = _retire(app, gid, batch=3)
    assert (third.outcome, third.frames_deleted, _ids(owner, gid)) == (FR.COMPLETE, 1, [])
    assert _ledger(owner, gid)["frames_deleted"] == 7
    assert _retire(app, gid).outcome == FR.FINISHED


def test_the_chain_head_job_anchors_nothing_while_a_chain_is_pruned(owner, app):
    """Between two batches the chain has frames and no anchor. Before 052, the reconcile
    anchored that prefix, and the next batch deleted its head: TRUNCATED, the tamper verdict,
    made by the two jobs together. Control: an unanchored chain with no ledger row is
    anchored in the same way."""
    gid = _grant(owner, revoked_days_ago=200)
    _chain(owner, app, gid, 6)
    control = uuid.uuid4()
    _chain(owner, app, control, 2, anchor=False)
    assert _retire(app, gid, batch=2).outcome == FR.STARTED

    o = CH.reconcile(app, gid, signer=SIGNER, keys=F.KEYS, limit=10_000)
    assert _anchor(owner, gid) is None, (
        f"the reconcile anchored the prefix of a chain retention is pruning ({o.verdict})")
    assert o.verdict == CH.RETIRING and o.verdict not in CH.REFUSALS
    oc = CH.reconcile(app, control, signer=SIGNER, keys=F.KEYS, limit=10_000)
    assert oc.verdict == CH.ANCHORED

    # The whole job, every pass, while the prune goes on: no finding about this chain.
    prefix = f"t{uuid.uuid4().hex[:8]}."
    seen = []
    while True:
        left = _ids(owner, gid)
        report = CH.tick(app, signer=SIGNER, keys=F.KEYS, cursor_prefix=prefix,
                         frames_per_tick=100_000, anchors_per_tick=100_000)
        seen += [x for x in report.findings if x.chain_id == str(gid)]
        # The passes step over the chain rather than stopping at it (SETTLED): the frames
        # cursor ends past every frame the prune has not reached yet.
        assert report.frames_position >= max(left), (report.frames_position, left)
        r = _retire(app, gid, batch=2)
        if r.outcome not in FR.DELETING:
            break
    assert r.outcome == FR.COMPLETE and seen == [], seen
    report = CH.tick(app, signer=SIGNER, keys=F.KEYS, cursor_prefix=prefix,
                     frames_per_tick=100_000, anchors_per_tick=100_000)
    assert [x for x in report.outcomes if x.chain_id == str(gid)
            and x.verdict in CH.REFUSALS] == []
    assert _anchor(owner, gid) is None


def _race_audit(owner, app, monkeypatch, *, snapshot: str | None):
    """A frame-audit recheck batch that starts at frames[2] of a 4-frame chain, and a
    retention batch that commits between the batch's read and frames[2]'s predecessor probe,
    deleting frames[3], [2] and [1] (newest first). Returns the audit's findings about it.
    `snapshot`: the tick's first statement in place of the module's; None leaves it."""
    gid = _grant(owner, revoked_days_ago=200)
    frames = _chain(owner, app, gid, 4)
    prefix = f"t{uuid.uuid4().hex[:8]}."
    owner.execute("INSERT INTO background_job_cursors (cursor_name, position, laps) VALUES "
                  "(%s, %s, 0), (%s, %s, 0)",
                  (prefix + FA.CURSOR_NEW, frames[-1]["id"],
                   prefix + FA.CURSOR_RECHECK, frames[1]["id"]))
    original = FA._Chains.predecessor_hash
    fired = []
    other = dbconn.connect(DSN)                # the retention job's own connection

    def racing(self, chain_id, frame_id):
        if str(chain_id) == str(gid) and not fired:
            fired.append(_retire(other, gid, batch=3))       # commits before the probe
        return original(self, chain_id, frame_id)

    monkeypatch.setattr(FA._Chains, "predecessor_hash", racing)
    if snapshot is not None:
        monkeypatch.setattr(FA, "_SNAPSHOT", snapshot)
    try:
        report = FA.audit_tick(app, new_per_tick=10, recheck_per_tick=2, keys=F.KEYS,
                               cursor_prefix=prefix)
    finally:
        other.close()
    assert fired and fired[0].frames_deleted == 3, "the race did not happen"
    assert _ids(owner, gid) == [frames[0]["id"]]
    return [f.reason for f in report.findings if f.chain_id == str(gid)]


def test_the_frame_audit_reads_one_snapshot_so_a_prune_mid_tick_is_not_tampering(
        owner, app, monkeypatch):
    """CONTROL, one variable (the tick's isolation): the same interleaving under READ
    COMMITTED reports LinkMismatch for a frame retention deleted."""
    assert _race_audit(owner, app, monkeypatch, snapshot=None) == []
    assert _race_audit(owner, app, monkeypatch, snapshot="SELECT 1") == [FA.LINK_MISMATCH]


def test_a_frame_past_the_retired_head_stops_the_prune(owner, app):
    """A writer adds a frame to what is left: retention stops, and the linked prefix plus
    the new frame is anchored again with no refusal."""
    gid = _grant(owner, revoked_days_ago=200)
    frames = _chain(owner, app, gid, 5)
    assert _retire(app, gid, batch=2).outcome == FR.STARTED
    left = frames[:3]
    new = F.extend(owner, left, chain_id=gid, sign=True)
    r = _retire(app, gid, batch=2)
    assert (r.outcome, r.reason, r.frames_deleted) == (
        FR.STOPPED, "frame_past_retired_head", 0)
    assert _ids(owner, gid) == [f["id"] for f in left] + [new["id"]]
    o = CH.reconcile(app, gid, signer=SIGNER, keys=F.KEYS, limit=10_000)
    assert (o.verdict, o.frame_count, o.head_frame_id) == (CH.ANCHORED, 4, new["id"])
    led = _ledger(owner, gid)
    assert (led["status"], led["stop_reason"], led["frames_deleted"]) == (
        "stopped", "frame_past_retired_head", 2)
    assert _retire(app, gid).outcome == FR.FINISHED


def test_a_grant_revived_mid_prune_stops_it(owner, app):
    """An expired grant can be revived (apps/grants/repository.py update_grant: any
    unrevoked grant). Control: the same chain with the grant left expired completes."""
    gid, control = _grant(owner, expires_days=-200), _grant(owner, expires_days=-200)
    frames = _chain(owner, app, gid, 5)
    _chain(owner, app, control, 5)
    assert _retire(app, gid, batch=2).outcome == FR.STARTED
    assert _retire(app, control, batch=2).outcome == FR.STARTED
    owner.execute("UPDATE skill_grants SET expires_at = now() + interval '30 days' "
                  "WHERE id = %s", (gid,))
    r = _retire(app, gid, batch=2)
    assert (r.outcome, r.reason) == (FR.STOPPED, "grant_live_again")
    assert _ids(owner, gid) == [f["id"] for f in frames[:3]]
    assert [x.outcome for x in _drain(app, control, batch=2)][-1] == FR.COMPLETE


# ------------------------------------------------------------------ the app role

def test_the_app_role_runs_retention_but_cannot_write_its_ledger_or_policy(app):
    """Controls first: meshychat_app reads both tables and may execute both functions."""
    assert app.execute("SELECT current_user").fetchone()[0] == "meshychat_app"
    app.execute("SELECT count(*) FROM invocation_chain_retention").fetchone()
    assert app.execute("SELECT chain_window_days FROM invocation_frame_retention_policy"
                       ).fetchone()[0] == 90
    for fn in ("retire_invocation_chain(uuid, bigint, bigint, integer)",
               "prune_standalone_invocation_frames(integer)"):
        assert app.execute("SELECT has_function_privilege(%s, 'EXECUTE')",
                           (fn,)).fetchone()[0], fn
    refused = [
        ("INSERT INTO invocation_chain_retention (chain_id, status, dead_since, window_days, "
         "newest_frame_at, anchor_head_frame_id, anchor_head_hash, anchor_frame_count, "
         "anchor_signature, anchor_signing_kid) VALUES (gen_random_uuid(), 'complete', now(), "
         "1, now(), 1, 'x', 1, '\\x00', 'k')"),
        "UPDATE invocation_chain_retention SET frames_deleted = 0",
        "DELETE FROM invocation_chain_retention",
        "UPDATE invocation_frame_retention_policy SET chain_window_days = 1",
        "DELETE FROM invocation_frames WHERE id = -1",
        "SELECT prune_invocation_frames(3650, 1)",
    ]
    for sql in refused:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(sql)


# ------------------------------------------------------------------ standalone frames

def _standalone(owner, age_days: int) -> int:
    f = F.standalone(owner)
    owner.execute("UPDATE invocation_frames SET created_at = now() - make_interval(days => %s) "
                  "WHERE id = %s", (age_days, f["id"]))
    return f["id"]


def test_standalone_frames_are_kept_unless_the_policy_sets_their_window(owner, app, policy):
    old, young = _standalone(owner, 400), _standalone(owner, 364)
    gid = _grant(owner)                       # a live chain as old as the old frame
    _chain(owner, app, gid, 2, age_days=400)
    chained = _ids(owner, gid)
    prefix = f"t{uuid.uuid4().hex[:8]}."

    off = FR.tick(app, keys=F.KEYS, cursor_prefix=prefix)
    assert off.standalone_deleted is None
    assert owner.execute("SELECT count(*) FROM invocation_frames WHERE id = ANY(%s)",
                         ([old, young],)).fetchone()[0] == 2
    assert app.execute("SELECT prune_standalone_invocation_frames(10)").fetchone()[0] is None

    policy.execute("UPDATE invocation_frame_retention_policy SET standalone_retain_days = 365")
    on = FR.tick(app, keys=F.KEYS, cursor_prefix=prefix, frames_per_tick=1_000_000)
    left = {r[0] for r in owner.execute("SELECT id FROM invocation_frames WHERE id = ANY(%s)",
                                        ([old, young],)).fetchall()}
    assert on.standalone_deleted >= 1
    assert old not in left, "a standalone frame one day past its window was kept"
    assert young in left, "a standalone frame one day inside its window was deleted"
    assert _ids(owner, gid) == chained, "the standalone rule reached a chained frame"


# ------------------------------------------------------------------ the tick and the job

def test_a_tick_is_bounded_and_the_next_one_finishes(owner, app, caplog):
    """First the ticks finish whatever earlier tests left eligible, so this test's chain is
    the only work; then a budget of 4 deletes exactly 4 and says more is pending. A live
    chain beside it is untouched throughout."""
    for _ in range(50):
        if not FR.tick(app, keys=F.KEYS, cursor_prefix=f"t{uuid.uuid4().hex[:8]}.",
                       frames_per_tick=10_000).more:
            break
    gid, live = _grant(owner, revoked_days_ago=200), _grant(owner)
    _chain(owner, app, gid, 10)
    _chain(owner, app, live, 3)
    live_ids = _ids(owner, live)
    prefix = f"t{uuid.uuid4().hex[:8]}."
    with caplog.at_level(logging.INFO, logger="meshychat.calls.frame_retention"):
        one = FR.tick(app, keys=F.KEYS, cursor_prefix=prefix, frames_per_tick=4)
        assert (one.chain_frames_deleted, one.more, len(_ids(owner, gid))) == (4, True, 6)
        for _ in range(10):
            if not FR.tick(app, keys=F.KEYS, cursor_prefix=prefix, frames_per_tick=4).more:
                break
    assert _ids(owner, gid) == [] and _ledger(owner, gid)["status"] == "complete"
    assert _ids(owner, live) == live_ids
    assert any("PRUNED BY RETENTION" in m and str(gid) in m for m in caplog.messages)


def test_a_chain_whose_anchor_does_not_verify_is_not_retired(owner, app, caplog):
    """Retention never destroys a chain the chain-head job refuses. Control: a clean dead
    chain in the same tick goes."""
    bad, good = _grant(owner, revoked_days_ago=200), _grant(owner, revoked_days_ago=200)
    _chain(owner, app, bad, 2)
    _chain(owner, app, good, 2)
    owner.execute("UPDATE invocation_chain_heads SET head_signature = %s WHERE chain_id = %s",
                  (b"\x00" * 64, bad))
    prefix = f"t{uuid.uuid4().hex[:8]}."
    with caplog.at_level(logging.ERROR, logger="meshychat.calls.frame_retention"):
        refused = []
        for _ in range(50):
            report = FR.tick(app, keys=F.KEYS, cursor_prefix=prefix, frames_per_tick=10_000)
            refused += report.refused
            if not report.more and _ids(owner, good) == []:
                break
    assert (str(bad), CH.BAD_SIGNATURE) in refused
    assert len(_ids(owner, bad)) == 2 and _ledger(owner, bad) is None
    assert _ids(owner, good) == [], "control: the clean chain was not retired"
    assert any("NOT retired" in m and str(bad) in m for m in caplog.messages)


def test_the_job_is_registered_on_the_retention_cadence_with_a_bound():
    job = registry.by_name("frame_retention")
    assert (job.interval_secs, job.timeout_secs, job.max_batch) == (900, 30, 5000)
    assert job.run is FR.run_job


def test_the_delete_is_an_ordered_range_on_the_chain_index(owner):
    """The function's own DELETE, read from the catalog, planned generically with sequential
    scans and sorts switched off: an ordered range on invocation_frames_chain_idx
    (chain_id, id DESC) and no Sort node. Planned as the OWNER, because a SECURITY DEFINER
    body runs as its owner; invocation_frames has no row-level security, so no role adds a
    qual the planner misjudges (the OI-45 trap c4d2125 fixed was a policy qual under
    meshy_app, now meshychat_app). CONTROL: ordered by a column no index leads with, a Sort appears."""
    import re
    src = owner.execute("SELECT prosrc FROM pg_proc WHERE proname = 'retire_invocation_chain'"
                        ).fetchone()[0]
    # The body names its tables schema-qualified since sql_meshychat/042 (divergence 096).
    stmt = re.search(r"DELETE FROM public\.invocation_frames WHERE id IN \((.*?)\);", src, re.S)
    assert stmt, "the function's DELETE was not found"
    inner = (stmt.group(1).replace("p_chain_id", "$1::uuid").replace("v_bound", "$2::bigint")
             .replace("p_batch", "$3::int"))
    assert "ORDER BY id DESC" in inner, inner

    def plan(inner_sql: str) -> str:
        with owner.transaction():
            owner.execute("SET LOCAL enable_seqscan = off")
            owner.execute("SET LOCAL enable_sort = off")
            rows = owner.execute("EXPLAIN (GENERIC_PLAN) DELETE FROM invocation_frames "
                                 "WHERE id IN (" + inner_sql + ")").fetchall()
        return " | ".join(r[0] for r in rows)

    got = plan(inner)
    assert "invocation_frames_chain_idx" in got and "Sort" not in got, got
    assert "Sort" in plan(inner.replace("ORDER BY id DESC", "ORDER BY skill_name")), (
        "control: a Sort must be visible when no index gives the order")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
