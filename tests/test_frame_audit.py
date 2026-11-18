"""STATUS D09: the incremental invocation-frame audit (rule ii).

Re-hashing the whole table every hour reads ~44 GB/day and times out on 1.7M standalone frames.
`apps/calls/frame_audit.py` checks each frame once on arrival and re-verifies the history
with a bounded rolling recheck. These tests hold:

  * THE HASH: `compute_frame_hash` is pinned against known-answer hashes and canonical bytes
    captured on 2026-09-26. The pins prevent a serializer and hash function from agreeing on
    the wrong byte form;
  * EVERY BREAK is reported at the frame that has it, each beside a sound
    frame of the same shape that is NOT reported (the positive control);
  * INCREMENTAL: a tick reads only frames past its cursor, never more than its bound, and a
    second tick with nothing new reads nothing new;
  * THE RECHECK finds what the new pass cannot: a frame changed after it was audited, and a
    frame that committed below the new cursor (a BIGSERIAL id is drawn at INSERT).

ISOLATION. `invocation_frames` is shared by the run. Every test uses a cursor pair of its
own (`cursor_prefix`), and every assertion is about its own frames' ids.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_frame_audit.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.calls import frame_audit as FA  # noqa: E402
from apps.core import dbconn  # noqa: E402
from apps.jobs import registry, runner  # noqa: E402
from tests.fixtures import frames as F  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


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


def _prefix() -> str:
    return f"t{uuid.uuid4().hex[:10]}."


def _tick(app, prefix, **kw) -> FA.Report:
    return FA.audit_tick(app, keys=kw.pop("keys", F.KEYS), cursor_prefix=prefix, **kw)


def _mine(report_or_findings, ids) -> dict[int, str]:
    findings = getattr(report_or_findings, "findings", report_or_findings)
    ids = set(ids)
    return {f.frame_id: f.reason for f in findings if f.frame_id in ids}


def _lap(app, prefix, *, per_tick=5000, **kw) -> list[FA.Finding]:
    """Ticks until the recheck wraps once; every finding along the way."""
    found: list[FA.Finding] = []
    start = None
    for _ in range(500):
        r = _tick(app, prefix, new_per_tick=per_tick, recheck_per_tick=per_tick, **kw)
        found.extend(r.findings)
        if start is None:
            start = r.laps
        elif r.laps > start:
            return found
    raise AssertionError("the recheck never wrapped")


# -------------------------------------------------------------- canonical-hash known answers

U = {"a": "11111111-2222-4333-8444-555555555555", "b": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
     "c": "0f0f0f0f-0000-4000-8000-000000000001", "d": "deadbeef-0000-4000-8000-00000000abcd"}

#: (row, previous_frame_hash, sha256 hex, canonical bytes), known answers captured from the
#: same inputs. They prevent a serializer and hash function from agreeing on the wrong bytes.
GOLDEN = [
    (dict(chain_id=None, capability_ref=None, delegation_depth=0, invocation_nonce=None,
          decision="deny", deny_reason="no grant", call_id=None, caller_owner_id=U["a"],
          caller_peer_id=U["b"], target_owner_id=U["c"], target_peer_id=U["d"],
          skill_name="memory.query", mandate_hash=None, flow_guard_mode=None,
          flow_root_kind=None, continuation_nonce=None), None,
     "641b951109a1b73d4d8a4979e3e43dc5b5d4c9f7701cb25a4c5adf383593927b",
     '{"chain_id":null,"capability_ref":null,"delegation_depth":0,"invocation_nonce":null,'
     '"decision":"deny","deny_reason":"no grant","call_id":null,"caller_owner_id":'
     '"11111111-2222-4333-8444-555555555555","caller_peer_id":'
     '"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","target_owner_id":'
     '"0f0f0f0f-0000-4000-8000-000000000001","target_peer_id":'
     '"deadbeef-0000-4000-8000-00000000abcd","skill_name":"memory.query",'
     '"previous_frame_hash":null}'),
    (dict(chain_id=U["c"], capability_ref=U["d"], delegation_depth=2,
          invocation_nonce='n"once\\/\x01\t\x7f', decision="allow", deny_reason=None,
          call_id="call-é\U0001F600", caller_owner_id=U["a"], caller_peer_id=U["b"],
          target_owner_id=U["c"], target_peer_id=U["d"], skill_name="skill\n\r\b\f</x>",
          mandate_hash="ab12", flow_guard_mode="enforce", flow_root_kind="external",
          continuation_nonce="cn-1"), "00ff",
     "f7a5aa6f877d5613b35c5994685a6ad217aad31b89ec665079c3b93390d5ab70", None),
    (dict(chain_id=U["c"], capability_ref=None, delegation_depth=-1, invocation_nonce=None,
          decision="allow", deny_reason=None, call_id="c3", caller_owner_id=U["a"],
          caller_peer_id=U["b"], target_owner_id=U["c"], target_peer_id=U["d"],
          skill_name="s", mandate_hash=None, flow_guard_mode="audit", flow_root_kind=None,
          continuation_nonce=None), "beef",
     "c51b88907bc6c09ebe5321b52446ef6e08fe26b2b99286d206bba4fbcf6d1a9d", None),
]


@pytest.mark.parametrize("row,prev,digest,canonical", GOLDEN, ids=["standalone-deny",
                                                                   "every-field-escaped",
                                                                   "some-optionals"])
def test_d09_the_frame_hash_has_canonical_bytes(row, prev, digest, canonical):
    """Case 2 carries every required JSON escape (\\" \\\\ \\u0001 \\t \\n \\r \\b \\f),
    a raw DEL, a raw '/', raw non-ASCII and an astral code point; case 3 leaves two of the
    four optional fields out. CONTROL: one byte of the input changed changes the hash."""
    if canonical is not None:
        assert FA.canonical_frame_bytes(row, prev) == canonical.encode("utf-8")
    assert FA.compute_frame_hash(row, prev) == digest
    assert FA.compute_frame_hash({**row, "skill_name": row["skill_name"] + "x"}, prev) != digest


def test_d09_the_self_check_reasons_are_expected():
    """FA.self_check, as a pure function, each reason beside its sound control."""
    row = F.base_row()
    fh = FA.compute_frame_hash(row, None)
    sound = {**row, "previous_frame_hash": None, "frame_hash": fh, "frame_signature": None,
             "signing_kid": None}
    signed = {**sound, "frame_signature": F.P.sign(F.SEED, FA.signature_material(fh)),
              "signing_kid": F.KID}
    assert FA.self_check(sound, F.KEYS) is None
    assert FA.self_check(signed, F.KEYS) is None
    assert FA.self_check({**sound, "decision": "maybe"}, F.KEYS) == FA.MALFORMED_DECISION
    assert FA.self_check({**sound, "skill_name": "other"}, F.KEYS) == FA.HASH_MISMATCH
    assert FA.self_check({**signed, "signing_kid": None}, F.KEYS) == FA.MISSING_SIGNING_KID
    assert FA.self_check({**signed, "signing_kid": ""}, F.KEYS) == FA.MISSING_SIGNING_KID
    assert FA.self_check(signed, {}) == FA.UNKNOWN_SIGNING_KID
    other = F.P.public_key_of(bytes(32))
    assert FA.self_check(signed, {F.KID: other}) == FA.SIGNATURE_MISMATCH
    bad = bytearray(signed["frame_signature"])
    bad[0] ^= 1
    assert FA.self_check({**signed, "frame_signature": bytes(bad)}, F.KEYS) == (
        FA.SIGNATURE_MISMATCH)


# ------------------------------------------------------------------ breaks, in the database

def test_d09_a_sound_history_reports_nothing_and_every_break_is_found_where_it_is(owner, app):
    """One recheck lap over: a sound chain of four (signed), sound standalone frames, and
    beside them a frame with each reported break: a field changed in place
    (HashMismatch), a link to the wrong predecessor (LinkMismatch), a parent that is not the
    root (ParentHashMismatch), a standalone frame that carries a link (UnexpectedLink), a
    signature under another key (SignatureMismatch). Every break is reported at its own
    frame and nowhere else; the sound frames are not reported. UnexpectedLink is planted
    twice, once per half of its clause (review finding 6): a standalone frame with a
    `previous_frame_hash`, and one with only a `parent_hash`, which is not in the hashed
    payload, so its self check passes and the clause is the only thing that reports it."""
    prefix = _prefix()
    sound = F.chain(owner, 4, sign=True) + [F.standalone(owner), F.standalone(owner, sign=True)]
    edited = F.chain(owner, 3)
    owner.execute("UPDATE invocation_frames SET skill_name = 'edited' WHERE id = %s",
                  (edited[1]["id"],))
    linked = F.chain(owner, 2)
    wrong_link = F.insert(owner, F.base_row(chain_id=linked[0]["chain_id"]),
                          previous_frame_hash="f" * 64, parent_hash=linked[0]["frame_hash"])
    parented = F.chain(owner, 2)
    wrong_parent = F.insert(owner, F.base_row(chain_id=parented[0]["chain_id"]),
                            previous_frame_hash=parented[-1]["frame_hash"],
                            parent_hash="e" * 64)
    loose = F.insert(owner, F.base_row(), previous_frame_hash="d" * 64)
    parent_only = F.insert(owner, F.base_row(), parent_hash="c" * 64)
    forged = F.standalone(owner, sign=True)
    owner.execute("UPDATE invocation_frames SET frame_signature = %s WHERE id = %s",
                  (F.P.sign(bytes(32), FA.signature_material(forged["frame_hash"])),
                   forged["id"]))

    everything = [f["id"] for f in sound + edited + linked + parented] + [
        wrong_link["id"], wrong_parent["id"], loose["id"], parent_only["id"], forged["id"]]
    found = _mine(_lap(app, prefix), everything)

    assert found == {edited[1]["id"]: FA.HASH_MISMATCH,
                     wrong_link["id"]: FA.LINK_MISMATCH,
                     wrong_parent["id"]: FA.PARENT_HASH_MISMATCH,
                     loose["id"]: FA.UNEXPECTED_LINK,
                     parent_only["id"]: FA.UNEXPECTED_LINK,
                     forged["id"]: FA.SIGNATURE_MISMATCH}, found


def test_d09_a_signed_frame_whose_key_the_server_does_not_hold_is_reported(owner, app):
    """Fail closed: with no verifier key for its kid a
    signed frame reports UnknownSigningKid, and an unsigned one beside it reports nothing."""
    prefix = _prefix()
    signed, unsigned = F.standalone(owner, sign=True), F.standalone(owner)
    found = _mine(_lap(app, prefix, keys={}), [signed["id"], unsigned["id"]])
    assert found == {signed["id"]: FA.UNKNOWN_SIGNING_KID}


# ------------------------------------------------------------------ incremental (rule ii)

def test_d09_the_new_pass_reads_only_what_arrived_and_never_more_than_its_bound(owner, app):
    """The first tick starts the new cursor at the newest frame, so it reads nothing new.
    Five frames arrive: a tick bounded at 3 reads 3 and says more; the next reads 2; the
    next reads 0. A frame broken on arrival is reported by the tick that reads it."""
    prefix = _prefix()
    first = _tick(app, prefix, new_per_tick=3, recheck_per_tick=1)
    assert first.frames_new == 0 and first.more is False
    c = F.chain(owner, 4)
    broken = F.insert(owner, F.base_row(), previous_frame_hash="a" * 64)
    ids = [f["id"] for f in c] + [broken["id"]]

    r1 = _tick(app, prefix, new_per_tick=3, recheck_per_tick=1)
    r2 = _tick(app, prefix, new_per_tick=3, recheck_per_tick=1)
    r3 = _tick(app, prefix, new_per_tick=3, recheck_per_tick=1)
    assert (r1.frames_new, r1.more) == (3, True)
    assert (r2.frames_new, r2.more) == (2, False)
    assert r3.frames_new == 0
    assert r1.frames_rechecked <= 1 and r2.frames_rechecked <= 1, "the recheck is bounded"
    new_found = {f.frame_id: f.reason for f in r1.findings + r2.findings
                 if f.found_by == "new" and f.frame_id in set(ids)}
    assert new_found == {broken["id"]: FA.UNEXPECTED_LINK}


def test_d09_the_audit_holds_no_aggregate_and_no_whole_chain_read():
    """Rule ii as a property of the source: no SQL aggregate, and every read of
    invocation_frames is bounded by a LIMIT (a batch, or one probe of the chain index)."""
    import inspect
    import re
    src = inspect.getsource(FA)
    code = "\n".join(F.code_strings(FA))
    assert "invocation_frames" in code, "control: the scan reads the module's SQL"
    assert not re.search(r"\b(count|sum|max|min|array_agg)\s*\(", code, re.I)
    reads = re.findall(r'"SELECT[^"]*FROM invocation_frames[^"]*"(?:\s*"[^"]*")*', src)
    batches = re.findall(r'_SELECT \+ "[^"]*"', src)
    assert len(reads) >= 3 and len(batches) == 2, "control: the scan finds the audit's reads"
    for q in reads + batches:
        assert "LIMIT" in q, q


# ------------------------------------------------------------------ what only the recheck sees

def test_d09_a_frame_changed_after_it_was_audited_is_found_by_the_recheck(owner, app):
    """An hourly whole-table pass finds an edit made after a frame's first audit;
    an audit that looks at each frame once would not. The bounded recheck audits the chain sound;
    the root is then edited in place; within one recheck lap it is reported."""
    prefix = _prefix()
    _tick(app, prefix)
    c = F.chain(owner, 3)
    r = _tick(app, prefix)
    assert _mine(r, [f["id"] for f in c]) == {}, "control: sound on arrival"
    owner.execute("UPDATE invocation_frames SET delegation_depth = 9 WHERE id = %s",
                  (c[0]["id"],))
    assert _mine(_lap(app, prefix), [f["id"] for f in c]) == {c[0]["id"]: FA.HASH_MISMATCH}


def test_d09_a_frame_that_committed_below_the_new_cursor_is_found_by_the_recheck(owner, app):
    """A BIGSERIAL id is drawn at INSERT, so a frame can commit after the new pass has
    passed its id. Planted: a broken standalone frame with an id below the new cursor. The
    new pass never reads it; the recheck lap does."""
    prefix = _prefix()
    _tick(app, prefix)
    gap_id = owner.execute("SELECT nextval(pg_get_serial_sequence('invocation_frames', 'id'))"
                           ).fetchone()[0]
    F.chain(owner, 2)
    r = _tick(app, prefix)
    assert r.new_position > gap_id, "control: the new pass is past the gap"
    late = F.insert(owner, F.base_row(), previous_frame_hash="b" * 64, frame_id=gap_id)
    r = _tick(app, prefix)
    assert _mine([f for f in r.findings if f.found_by == "new"], [late["id"]]) == {}
    assert _mine(_lap(app, prefix), [late["id"]]) == {late["id"]: FA.UNEXPECTED_LINK}


# ------------------------------------------------------------------ registered

def test_d09_the_job_is_registered_bounded_and_runs_as_the_app_role():
    """Through the runner, as meshychat_app: the job reads frames it may only SELECT (sql/062)
    and writes only its own cursors."""
    base = registry.by_name("frame_audit")
    assert base.max_batch == FA.NEW_PER_TICK and base.interval_secs == 300
    job = registry.Job(name=f"t_frame_audit_{uuid.uuid4().hex[:8]}",
                       interval_secs=base.interval_secs, timeout_secs=base.timeout_secs,
                       jitter_frac=base.jitter_frac, max_batch=base.max_batch, run=base.run)
    try:
        (out,) = runner.run_due(lambda **kw: dbconn.connect(DSN, **kw), jobs=(job,),
                                holder="frame-audit-test")
    finally:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job.name,))
    assert out.claimed is True and out.result == "ok", out
