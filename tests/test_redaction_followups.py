"""memory.redact, the pieces that sit beside the operation (divergence 053; design docs/design/redaction.md section 6).

  * TEST 14: the legacy-verdict job classifies every row with NULL `signed_bytes`, and a redacted row has
    NULL `signed_bytes` by construction (its signed bytes ARE the text), so without a skip it would be
    reported as an unsigned historical row (`no_envelope`), and a reader of the verdict would take a
    deliberate redaction for a row that never had a signature. A redaction record for the row means the
    job leaves it alone. CONTROL, in the same run: a genuine historical row IS classified.
  * TEST 19: the audit chain. `invocation_frames` hold ids, hashes, a decision and a skill name and no
    text (design section 5b), and a redaction changes none of them, so the frame audit and the chain-head
    anchor find nothing wrong with the frames of a write, a read and a redaction of the same row. CONTROL:
    a frame changed after the fact IS found, so the empty answer is not an audit that cannot fire.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_redaction_followups.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_memory_skills import (  # noqa: E402,F401
    _call, app_conn, owner_conn,
)
from tests.test_redaction import _redact, _world, _write  # noqa: E402


def _row_id(owner_conn, ns, key):
    with owner_conn.cursor() as cur:
        cur.execute("SELECT id FROM shared_memory WHERE namespace = %s AND key = %s AND is_head", (ns, key))
        (rid,) = cur.fetchone()
    owner_conn.commit()
    return rid


def _verdict_rows(owner_conn, rid) -> int:
    from apps.memoryskills import legacy_verdict
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory_legacy_verdict WHERE row_id = %s AND classifier_version = %s",
                    (rid, legacy_verdict.CLASSIFIER_VERSION))
        (n,) = cur.fetchone()
    owner_conn.commit()
    return n


def test_14_a_redacted_row_is_not_classified_as_an_unsigned_historical_row(app_conn, owner_conn):
    from tests.test_signed_bytes_classifier import _classify, _namespace, _signer, legacy

    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd14")
    key = _write(app_conn, author, ns)
    rid = _row_id(owner_conn, ns, key)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT signed_bytes IS NOT NULL FROM shared_memory WHERE id = %s", (rid,))
        assert cur.fetchone()[0] is True, "control: a row written by this build carries its signed bytes"
    owner_conn.commit()

    _redact(app_conn, author, ns, key)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT signed_bytes IS NULL FROM shared_memory WHERE id = %s", (rid,))
        assert cur.fetchone()[0] is True, "the redacted row has NULL signed bytes: it is a candidate for the job"
    owner_conn.commit()

    # CONTROL, in the same drain: a genuine historical row (NULL bytes, no redaction record) is classified.
    signer = _signer(owner_conn, "rd14L")
    historical = legacy(owner_conn, signer, _namespace(owner_conn, signer))
    _classify(app_conn)
    assert _verdict_rows(owner_conn, historical) == 1, "control: the historical row was classified"
    assert _verdict_rows(owner_conn, rid) == 0, "the redacted row was classified as unsigned legacy"


# ============================================================================================ test 19
#
# What the design's section 5b said and what was measured. It said the redaction call "is an ordinary
# skill call and gets its own frame". MEASURED on this build (a wire write and a wire redaction by an
# ordinary seat): NO frame is written for either. This server does not write `invocation_frames`; the
# table holds frames recorded for mandate and capability chains, and this build audits and anchors
# them (apps/calls/frame_audit.py, chain_head.py). The claim that matters is that a frame is ids,
# hashes, a decision and a skill name and no text, and a redaction does not touch the table. The test
# seeds a signed chain that records the write's
# own call, fingerprints every frame, redacts the row, and shows the frames byte for byte the same and
# the audit and the anchor finding nothing. CONTROL: a frame changed after the fact IS found.

import os  # noqa: E402

import psycopg  # noqa: E402

from apps.core import dbconn  # noqa: E402
from tests.fixtures import frames as F  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN", "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


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


def _fingerprint(owner, ids) -> list[tuple]:
    """Every column of every frame, as text, in id order: what 'byte for byte the same' compares."""
    return [tuple(r) for r in owner.execute(
        "SELECT id, md5(f::text) FROM invocation_frames f WHERE id = ANY(%s) ORDER BY id", (list(ids),)).fetchall()]


def test_19_a_redaction_leaves_the_frames_and_their_audit_and_anchor_alone(app_conn, owner_conn, owner, app):
    from tests import test_chain_head as CHT
    from tests import test_frame_audit as FAT

    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd19")
    key = _write(app_conn, author, ns)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT provenance_jsonb -> 'payload' ->> 'call_id' FROM shared_memory "
                    "WHERE namespace = %s AND key = %s", (ns, key))
        (write_call_id,) = cur.fetchone()
    owner_conn.commit()
    assert write_call_id, "control: the write's own call id is known"

    frames = F.chain(owner, 4, sign=True, call_id=write_call_id, skill_name="memory.write")
    ids = [f["id"] for f in frames]
    before = _fingerprint(owner, ids)
    assert len(before) == 4

    # BEFORE the redaction: the chain is sound to the audit and to the anchor (so an empty answer after is a
    # fact about the redaction and not about a chain nobody could verify).
    prefix_a, prefix_c = FAT._prefix(), CHT._prefix()
    assert FAT._mine(FAT._lap(app, prefix_a), ids) == {}, "control: the audit finds nothing wrong with the seeded chain"
    CHT._drain(app, prefix_c)
    CHT._assert_exact(owner, frames)

    out = _redact(app_conn, author, ns, key)
    assert [r["version"] for r in out["redacted"]] == [1]

    assert _fingerprint(owner, ids) == before, "a redaction changed an invocation frame"
    assert FAT._mine(FAT._lap(app, FAT._prefix()), ids) == {}, "the frame audit found something after the redaction"
    later = CHT._drain(app, CHT._prefix())
    CHT._assert_exact(owner, frames)
    outcome = CHT._outcome(later, frames[0]["chain_id"])
    assert outcome is None or outcome.verdict == CHT.CH.CURRENT, outcome      # nothing to extend, nothing refused

    # CONTROL: the audit can fire. One frame's hash is changed the way a tamper would.
    owner.execute("UPDATE invocation_frames SET frame_hash = %s WHERE id = %s", ("0" * 64, ids[2]))
    found = FAT._mine(FAT._lap(app, FAT._prefix()), ids)
    assert found, "control: a changed frame was not found, so the empty answers above prove nothing"
