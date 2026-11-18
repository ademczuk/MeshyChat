"""Frame retention deletes by the frame's own identity and age, never by a live object's id.

STATUS D07 / divergence 029. `sql/111_call_and_frame_retention.sql` ships
`prune_invocation_frames(retain_days, batch_size)`, a SECURITY DEFINER function whose
chained pass groups `invocation_frames` by `chain_id` and deletes EVERY frame of a chain once
the chain's newest frame is older than the cutoff (`:92-107`). `chain_id` is the ROOT GRANT id
(`sql/060:24-27`; "a long-lived root grant", `sql/095:12`), so the delete key is a live
object's identity: an idle, never-expiring grant loses its entire audit history the day its
newest frame crosses the cutoff. Production stopped it with a REVOKE on that database only;
`sql/111:121` re-grants EXECUTE to the application role on every fresh install built from here (measured
on a fresh database from c5457ac).

The rule this file holds: retention must never delete by a key that is also a live object's
identity. `sql_meshychat/012` revokes the app role's EXECUTE, and replaces the body so the
only rows it can delete are STANDALONE frames (chain_id IS NULL), chosen by their own `id`
under an age bound. A chained frame cannot be removed by its own id either without cutting
its chain's head, which `verify_chain` reads as tampering (111's own header), so chained
frames have no retention path at all until a design re-anchors a chain.

No Python code calls the function (grepped apps/ and tools/: zero hits), so the only caller
is an operator, as the owning role.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.core import dbconn  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

SIGNATURE = "prune_invocation_frames(integer, integer)"


@pytest.fixture()
def owner_conn():
    with psycopg.connect(DSN) as conn:
        yield conn


def test_the_app_role_cannot_execute_frame_retention(owner_conn):
    """Before: has_function_privilege was TRUE and the call ran as the function's owner."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT has_function_privilege('meshychat_app', %s, 'EXECUTE'), "
                    "       has_function_privilege(current_user, %s, 'EXECUTE'), "
                    "       has_function_privilege('meshychat_app', "
                    "           'meshychat_same_tenant_or_devgroup(uuid, uuid)', 'EXECUTE'), "
                    "       (SELECT proacl::text FROM pg_proc "
                    "         WHERE oid = %s::regprocedure)",
                    (SIGNATURE, SIGNATURE, SIGNATURE))
        app_may, owner_may, app_may_other, acl = cur.fetchone()
    # Positive controls: the function still exists and its owner may run it, and the
    # revoke was surgical: the app role keeps EXECUTE on the functions its policies call.
    assert owner_may, "the owning role lost EXECUTE: retention would have no caller at all"
    assert app_may_other, "the app role lost EXECUTE on the tenant predicate its RLS calls"
    assert not app_may, f"meshychat_app may still execute {SIGNATURE}; proacl={acl}"
    assert "meshychat_app=" not in acl and not acl.lstrip("{").startswith("=") and ",=" not in acl, (
        f"an ACL entry still grants the app role or PUBLIC: {acl}")

    # The refusal as the serving path sees it.
    conn = dbconn.connect(DSN)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("SELECT prune_invocation_frames(3650, 1)")
    finally:
        conn.close()


def _frame(cur, *, chain_id, previous, age_days: int) -> tuple[int, str]:
    who = uuid.uuid4()
    frame_hash = f"rls31-{uuid.uuid4().hex}"
    cur.execute(
        "INSERT INTO invocation_frames (created_at, chain_id, previous_frame_hash, "
        "frame_hash, decision, caller_owner_id, caller_peer_id, target_owner_id, "
        "target_peer_id, skill_name) "
        "VALUES (now() - make_interval(days => %s), %s, %s, %s, 'allow', %s, %s, %s, %s, "
        "'rls31.retention') RETURNING id",
        (age_days, chain_id, previous, frame_hash, who, who, who, who))
    return cur.fetchone()[0], frame_hash


def test_retention_never_deletes_by_chain_and_still_ages_out_standalone_frames(owner_conn):
    """Before: the whole 400-day-old chain went; the rule is that its root grant id is not a
    delete key. The standalone pass is the positive control: it must still delete the old
    standalone frame, and the age bound must keep the young one."""
    root_grant = uuid.uuid4()          # stands for a live grant: chain_id IS its id (060:24)
    with owner_conn.transaction(force_rollback=True), owner_conn.cursor() as cur:
        chain, prev = [], None
        for _ in range(3):
            fid, prev = _frame(cur, chain_id=root_grant, previous=prev, age_days=400)
            chain.append(fid)
        old, _ = _frame(cur, chain_id=None, previous=None, age_days=400)
        young, _ = _frame(cur, chain_id=None, previous=None, age_days=0)

        cur.execute("SELECT prune_invocation_frames(365, 1000)")
        deleted = cur.fetchone()[0]
        cur.execute("SELECT id FROM invocation_frames WHERE id = ANY(%s)",
                    (chain + [old, young],))
        left = {r[0] for r in cur.fetchall()}

    assert set(chain) <= left, (
        f"retention deleted {len(set(chain) - left)} of the chain's 3 frames: it deleted by "
        "chain_id, a live grant's identity")
    assert old not in left, "positive control: the aged standalone frame was not deleted"
    assert young in left, "the age bound failed: a new standalone frame was deleted"
    assert deleted >= 1


def test_retention_age_bound_is_counted_in_days(owner_conn):
    """The young frame above is inserted at now(), so any bound of zero or more keeps it and
    only DROPPING the bound could fail that test. Review of 8c3150f, F2: `days =>` changed
    to `hours =>` (mutant MA) passed, and under retain_days = 365 it deleted a 20-day-old
    frame. Here the two frames sit one day either side of the cutoff."""
    with owner_conn.transaction(force_rollback=True), owner_conn.cursor() as cur:
        inside, _ = _frame(cur, chain_id=None, previous=None, age_days=364)
        outside, _ = _frame(cur, chain_id=None, previous=None, age_days=366)
        cur.execute("SELECT prune_invocation_frames(365, 50000)")
        cur.execute("SELECT id FROM invocation_frames WHERE id = ANY(%s)", ([inside, outside],))
        left = {r[0] for r in cur.fetchall()}
    assert outside not in left, "positive control: a frame one day past the cutoff was kept"
    assert inside in left, "a frame one day inside the 365-day window was deleted"


def test_retention_deletes_at_most_one_batch_per_call(owner_conn):
    """`deleted >= 1` never measured the batch bound. Review of 8c3150f, F3: dropping
    `LIMIT batch_size` (mutant MB) passed, and deleted 5 of 5 eligible frames with
    batch_size = 2. Repeated calls must still drain every eligible frame."""
    with owner_conn.transaction(force_rollback=True), owner_conn.cursor() as cur:
        mine = [_frame(cur, chain_id=None, previous=None, age_days=400)[0] for _ in range(5)]
        cur.execute("SELECT prune_invocation_frames(365, 2)")
        first = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM invocation_frames WHERE id = ANY(%s)", (mine,))
        left_after_one = cur.fetchone()[0]
        cur.execute("SELECT prune_invocation_frames(365, 50000)")
        cur.execute("SELECT count(*) FROM invocation_frames WHERE id = ANY(%s)", (mine,))
        left_after_all = cur.fetchone()[0]
    assert first == 2, f"one call with batch_size = 2 deleted {first} frames"
    assert left_after_one >= 3, f"one batch of 2 left {left_after_one} of my 5 frames"
    assert left_after_all == 0, "positive control: further calls must drain the rest"


@pytest.mark.parametrize("args", [(0, 10), (30, 0), (30, 50001), (None, 10)])
def test_retention_still_refuses_an_unbounded_call(owner_conn, args):
    """The replacement keeps 111's argument guards: no zero-day window, no unbounded batch."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with owner_conn.transaction(force_rollback=True), owner_conn.cursor() as cur:
            cur.execute("SELECT prune_invocation_frames(%s::int, %s::int)", args)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
