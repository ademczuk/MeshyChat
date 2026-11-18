"""STATUS E10 (#910): the signed chain-head anchor, reconciled INCREMENTALLY (rule ii).

`apps/calls/chain_head.py` extends an anchor from its signed head by the frames past it, never
counting a chain. Counting a whole chain on every grant-bound authorization would consume about
490 GB/day at the measured workload. These tests hold:

  * THE SIGNED BYTES ARE KNOWN ANSWERS: the payload and signature were produced on 2026-09-26
    by an independent implementation of the format, so agreement shows byte compatibility, not
    self-consistency;
  * AN ANCHOR IS EXACT: after any sequence of ticks its count equals the number of frames in
    the chain up to its head, and its signature verifies (read back by an independent check);
  * INCREMENTAL: extending an anchored chain reads only the frames past its head, however long
    the chain, and a partial anchor under a small budget is a true one that the next tick
    continues;
  * REFUSED, EACH BESIDE A CONTROL THAT IS EXTENDED IN THE SAME TICK: a truncated chain (the
    signed head deleted), a tampered anchor (its count rewritten), a frame that does not link;
  * THE ROLLING ANCHOR PASS extends a chain whose frame committed after the frames cursor had
    passed its id;
  * D08, THE KNOWN ORPHANS (operator decision 53, 2026-09-27): a truncated anchor that matches
    an entry of the committed list EXACTLY (chain, head, count, kid) is reported at WARN, at
    most once an hour per anchor on the database's clock, naming its incident; an unlisted
    one, or a listed chain whose anchor differs from its entry, is still an ERROR; a malformed
    list is refused at load and excuses nothing.

ISOLATION: each test runs its own cursor pair (`cursor_prefix`) and asserts only about its own
chains. The first tick of a fresh frames cursor walks every frame in the run's database from
id 0; the database is the run's own (tests/conftest.py), so that is a few hundred at most.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_chain_head.py -q
"""
from __future__ import annotations

import json
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
from apps.calls.result_signing import ResultSigner  # noqa: E402
from apps.core import dbconn  # noqa: E402
from apps.jobs import registry, runner  # noqa: E402
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


def _prefix() -> str:
    return f"t{uuid.uuid4().hex[:10]}."


def _tick(app, prefix, **kw) -> CH.Report:
    kw.setdefault("frames_per_tick", 5000)
    kw.setdefault("anchors_per_tick", 5000)
    return CH.tick(app, signer=SIGNER, keys=F.KEYS, cursor_prefix=prefix, **kw)


def _drain(app, prefix, **kw) -> list[CH.Report]:
    out = []
    for _ in range(200):
        r = _tick(app, prefix, **kw)
        out.append(r)
        if not r.more:
            return out
    raise AssertionError("the reconcile never caught up")


def _anchor(owner, chain_id) -> dict | None:
    row = owner.execute("SELECT head_frame_id, head_hash, frame_count, head_signature, "
                        "signing_kid FROM invocation_chain_heads WHERE chain_id = %s",
                        (chain_id,)).fetchone()
    if row is None:
        return None
    return dict(zip(("head_frame_id", "head_hash", "frame_count", "head_signature",
                     "signing_kid"), row))


def _assert_exact(owner, frames: list[dict]) -> None:
    """The anchor names the chain's last frame, counts every frame, and its signature
    verifies over the signed payload under the signer's PUBLIC key (an independent check:
    nacl, not the module's own verify)."""
    a = _anchor(owner, frames[0]["chain_id"])
    assert a is not None, "the chain has no anchor"
    assert (a["head_frame_id"], a["head_hash"], a["frame_count"]) == (
        frames[-1]["id"], frames[-1]["frame_hash"], len(frames))
    from nacl.signing import VerifyKey
    VerifyKey(F.PUBLIC).verify(CH.payload_bytes(frames[0]["chain_id"], a["head_frame_id"],
                                                a["head_hash"], a["frame_count"]),
                               bytes(a["head_signature"]))
    assert a["signing_kid"] == F.KID


def _outcome(report_or_list, chain_id) -> CH.Outcome | None:
    reports = report_or_list if isinstance(report_or_list, list) else [report_or_list]
    got = [o for r in reports for o in r.outcomes if o.chain_id == str(chain_id)]
    return got[-1] if got else None


# ------------------------------------------------------------------ the signed bytes

def test_e10_the_signed_payload_has_canonical_bytes():
    """The known-answer payload and its signature under F.SEED, with head_frame_id at
    2**53 - 1 and a non-ASCII head_hash.
    CONTROL: the count changed by one changes the bytes and fails the signature."""
    want = ('{"chain_id":"0f0f0f0f-0000-4000-8000-000000000001","frame_count":42,'
            '"head_frame_id":9007199254740991,"head_hash":"abécd"}').encode("utf-8")
    sig = bytes.fromhex("1961417df9bfaa65e85befb90bd12e529ebd7120d2b938fbb433bb9bf08baa8f"
                        "c53ed689646826e090f1d360927b234eb92064677da78347b0fba37e18273305")
    got = CH.payload_bytes("0f0f0f0f-0000-4000-8000-000000000001", 9007199254740991,
                           "abécd", 42)
    assert got == want
    assert SIGNER.private_key.sign(got) == sig, "Ed25519 is deterministic: same key, same bytes"
    anchor = {"chain_id": "0f0f0f0f-0000-4000-8000-000000000001",
              "head_frame_id": 9007199254740991, "head_hash": "abécd", "frame_count": 42,
              "head_signature": sig, "signing_kid": F.KID}
    assert CH.verify_signature(anchor, F.KEYS) is None
    assert CH.verify_signature({**anchor, "frame_count": 41}, F.KEYS) == CH.BAD_SIGNATURE
    assert CH.verify_signature(anchor, {}) == CH.UNKNOWN_KID


# ------------------------------------------------------------------ exact and incremental

def test_e10_a_chain_is_anchored_exactly_and_extended_by_only_its_new_frames(owner, app):
    """Five frames: one drain anchors them all, count 5. Three more: the next tick reads
    exactly those three for this chain (frames_read 3, advanced 3), not eight, and the
    anchor is exact at 8. A tick with nothing new reads no frame of the chain."""
    prefix = _prefix()
    c = F.chain(owner, 5, sign=True)
    _drain(app, prefix)
    _assert_exact(owner, c)

    for _ in range(3):
        c.append(F.extend(owner, c))
    r = _tick(app, prefix)
    o = _outcome(r, c[0]["chain_id"])
    assert (o.verdict, o.advanced, o.frames_read) == (CH.EXTENDED, 3, 3), o
    _assert_exact(owner, c)

    again = _tick(app, prefix)
    o = _outcome(again, c[0]["chain_id"])
    assert o is None or (o.frames_read, o.advanced) == (0, 0), o


def test_e10_a_small_budget_anchors_part_of_a_chain_truly_and_the_next_tick_continues(
        owner, app):
    """Budget 3 against a new chain of 7: the first tick anchors the first 3 (a true
    statement: count 3 at the third frame) and stops the frames cursor inside the chain; the
    ticks after continue from the partial anchor to 7 without re-reading a frame."""
    prefix = _prefix()
    _drain(app, prefix)                                  # past everything else in the table
    c = F.chain(owner, 7)
    r1 = _tick(app, prefix, frames_per_tick=3)
    assert r1.more is True
    a = _anchor(owner, c[0]["chain_id"])
    assert (a["head_frame_id"], a["frame_count"]) == (c[2]["id"], 3)
    reports = [r1] + _drain(app, prefix, frames_per_tick=3)
    _assert_exact(owner, c)
    read = sum(o.frames_read for r in reports for o in r.outcomes
               if o.chain_id == str(c[0]["chain_id"]))
    assert read == 7, f"{read} frames read to anchor a chain of 7: a frame was read twice"


def test_e10_the_reconcile_holds_no_aggregate():
    """Rule ii, as a property of the source: no SQL aggregate anywhere in the module, and
    every read of invocation_frames is keyed and bounded (a LIMIT, or the primary key)."""
    import inspect
    import re
    src = inspect.getsource(CH)
    code = "\n".join(F.code_strings(CH))
    assert "invocation_frames" in code, "control: the scan reads the module's SQL"
    assert not re.search(r"\b(count|sum|max|min|array_agg)\s*\(", code, re.I)
    reads = re.findall(r'"SELECT[^"]*FROM invocation_frames[^"]*"(?:\s*"[^"]*")*', src)
    # the head probe, the chain-empty probe (D08), the extension, pass 1, the rescan
    assert len(reads) == 5, reads
    for q in reads:
        assert "LIMIT" in q or "WHERE id = %s AND chain_id" in q, q


# ------------------------------------------------------------------ refusals, with controls

def test_e10_truncation_and_a_tampered_anchor_are_refused_and_never_re_anchored(owner, app):
    """Three chains anchored; then: TRUNCATED, the signed head frame deleted (a tail cut);
    TAMPERED, the anchor's count rewritten down without the key; CONTROL, a sound chain that
    gains a frame. One tick: the control is extended, the other two are refused with their
    anchors byte-identical, so neither the cut nor the forged count is re-signed."""
    prefix = _prefix()
    cut, forged, control = F.chain(owner, 4), F.chain(owner, 4), F.chain(owner, 2)
    _drain(app, prefix)
    for c in (cut, forged, control):
        _assert_exact(owner, c)
    owner.execute("DELETE FROM invocation_frames WHERE id = %s", (cut[-1]["id"],))
    owner.execute("UPDATE invocation_chain_heads SET frame_count = 2 WHERE chain_id = %s",
                  (forged[0]["chain_id"],))
    before = {k: _anchor(owner, c[0]["chain_id"]) for k, c in (("cut", cut),
                                                               ("forged", forged))}
    control.append(F.extend(owner, control))

    reports = _drain(app, prefix)
    assert _outcome(reports, cut[0]["chain_id"]).verdict == CH.TRUNCATED
    assert _outcome(reports, forged[0]["chain_id"]).verdict == CH.BAD_SIGNATURE
    assert _anchor(owner, cut[0]["chain_id"]) == before["cut"]
    assert _anchor(owner, forged[0]["chain_id"]) == before["forged"]
    _assert_exact(owner, control)


def test_e10_a_head_replaced_in_place_under_its_id_is_refused(owner, app):
    """Review finding 5 (LOW): the signed head is checked by its HASH, not only its id. The
    head frame of an anchored chain is replaced under the same id by a self-consistent
    unsigned frame (another skill, its hash recomputed, the link to its predecessor intact),
    which the frame audit's per-frame checks also pass. The anchor check is the one thing
    that catches it: TRUNCATED, anchor byte-identical, never re-signed. CONTROL in the same
    tick: a sound chain that gains a frame is extended."""
    from apps.calls import frame_audit as FA
    prefix = _prefix()
    swapped, control = F.chain(owner, 3), F.chain(owner, 2)
    _drain(app, prefix)
    for c in (swapped, control):
        _assert_exact(owner, c)
    head = dict(swapped[-1], skill_name="memory.write")
    new_hash = FA.compute_frame_hash(head, head["previous_frame_hash"])
    assert new_hash != swapped[-1]["frame_hash"], "control: the replacement differs"
    owner.execute("UPDATE invocation_frames SET skill_name = %s, frame_hash = %s WHERE id = %s",
                  (head["skill_name"], new_hash, head["id"]))
    from psycopg.rows import dict_row
    with app.cursor(row_factory=dict_row) as cur:
        cur.execute(FA._SELECT + " WHERE id = %s", (head["id"],))
        stored = cur.fetchone()
        assert FA.check_frame(stored, FA._Chains(cur), F.KEYS, {}) is None, (
            "the replacement passes every per-frame check: the anchor is the only witness")
    before = _anchor(owner, swapped[0]["chain_id"])
    control.append(F.extend(owner, control))

    reports = _drain(app, prefix)
    assert _outcome(reports, swapped[0]["chain_id"]).verdict == CH.TRUNCATED
    assert _anchor(owner, swapped[0]["chain_id"]) == before
    _assert_exact(owner, control)


def test_e10_a_frame_that_does_not_link_stops_the_anchor_before_it(owner, app):
    """Two frames anchored, then a third that links to nothing: the anchor stays at the two
    that link (link_break) and the frames cursor is not held on it. A chain with no anchor
    whose first frame is not a root is not anchored at all (root_missing). CONTROL: a sound
    new chain in the same tick is anchored."""
    prefix = _prefix()
    c = F.chain(owner, 2)
    _drain(app, prefix)
    F.insert(owner, F.base_row(chain_id=c[0]["chain_id"]), previous_frame_hash="c" * 64,
             parent_hash=c[0]["frame_hash"])
    rootless = F.insert(owner, F.base_row(chain_id=uuid.uuid4()),
                        previous_frame_hash="9" * 64, parent_hash="8" * 64)
    control = F.chain(owner, 3)

    reports = _drain(app, prefix)
    assert _outcome(reports, c[0]["chain_id"]).verdict == CH.LINK_BREAK
    _assert_exact(owner, c)
    assert _outcome(reports, rootless["chain_id"]).verdict == CH.ROOT_MISSING
    assert _anchor(owner, rootless["chain_id"]) is None
    _assert_exact(owner, control)
    assert reports[-1].more is False, "a refused chain must not hold the cursor"


def test_e10_an_existing_anchor_is_extended_to_the_expected_count(owner, app):
    """An anchor whose signed fields are the chain's COUNT and MAX at that moment is extended by
    later frames to the count from a one-time test COUNT(*)."""
    prefix = _prefix()
    c = F.chain(owner, 3)
    n, head = owner.execute("SELECT count(*), max(id) FROM invocation_frames "
                            "WHERE chain_id = %s", (c[0]["chain_id"],)).fetchone()
    sig = SIGNER.private_key.sign(CH.payload_bytes(c[0]["chain_id"], head, c[-1]["frame_hash"],
                                                   n))
    owner.execute("INSERT INTO invocation_chain_heads (chain_id, head_frame_id, head_hash, "
                  "frame_count, head_signature, signing_kid) VALUES (%s,%s,%s,%s,%s,%s)",
                  (c[0]["chain_id"], head, c[-1]["frame_hash"], n, sig, F.KID))
    for _ in range(2):
        c.append(F.extend(owner, c))
    _drain(app, prefix)
    expected_count = owner.execute("SELECT count(*) FROM invocation_frames WHERE chain_id = %s",
                                   (c[0]["chain_id"],)).fetchone()[0]
    assert _anchor(owner, c[0]["chain_id"])["frame_count"] == expected_count == 5
    _assert_exact(owner, c)


# ------------------------------------------------------------------ the rolling anchor pass

def test_e10_a_frame_committed_below_the_frames_cursor_is_anchored_by_the_anchor_pass(
        owner, app):
    """A BIGSERIAL id is drawn at INSERT, so a chain's next frame can commit with an id the
    frames cursor has already passed. Planted: the chain anchored at 2, the cursor moved past
    a reserved id, then the chain's third frame written WITH that id. Pass 1 never reads it;
    the anchors pass finds frames past the head and extends the anchor to 3."""
    prefix = _prefix()
    c = F.chain(owner, 2)
    _drain(app, prefix)
    gap = owner.execute("SELECT nextval(pg_get_serial_sequence('invocation_frames', 'id'))"
                        ).fetchone()[0]
    F.standalone(owner)
    r = _tick(app, prefix)
    assert r.frames_position > gap, "control: the frames cursor is past the reserved id"
    c.append(F.extend(owner, c, frame_id=gap))
    _drain(app, prefix)
    _assert_exact(owner, c)


def test_e10_a_new_chain_committed_below_the_frames_cursor_is_anchored_by_the_rescan(
        owner, app):
    """Review finding 2 (MEDIUM). The case the anchors pass cannot reach: a chain with NO
    anchor whose frames all drew ids before the frames cursor passed them and committed
    after. Pass 1 reads only ids above its cursor and pass 2 visits only anchored chains, so
    without a third reader such a chain is never anchored until it gains another frame, and
    its truncation cannot be detected. Planted as the reviewer planted it: a reserved id, a
    CONTROL chain written after it and anchored (so the cursor is past the reserved id),
    then a new chain's root written WITH the reserved id, and a second frame of it with a
    reserved id too. The rolling rescan of frame ids anchors the new chain, exactly."""
    prefix = _prefix()
    _drain(app, prefix)
    seq = "pg_get_serial_sequence('invocation_frames', 'id')"
    gaps = [owner.execute(f"SELECT nextval({seq})").fetchone()[0] for _ in range(2)]
    control = F.chain(owner, 1)
    r = _tick(app, prefix)
    assert r.frames_position > max(gaps), "control: the frames cursor is past both ids"
    _assert_exact(owner, control)
    late = [F.extend(owner, [], chain_id=uuid.uuid4(), frame_id=gaps[0])]
    late.append(F.extend(owner, late, frame_id=gaps[1]))
    for _ in range(5):
        _drain(app, prefix)
    _assert_exact(owner, late)


def test_e10_the_rescan_is_bounded_and_laps(owner, app):
    """The rescan reads at most its bound of frame ids a tick, by id past its own cursor, up
    to the frames cursor, then from 0 again (`laps`). CONTROL: with a bound of 1 a lap takes
    one tick per frame id the table holds up to the frames cursor, and it does complete."""
    prefix = _prefix()
    _drain(app, prefix)
    top = owner.execute("SELECT count(*) FROM invocation_frames").fetchone()[0]
    reports = [_tick(app, prefix, rescan_per_tick=1, anchors_per_tick=1)]
    assert reports[0].frames_rescanned <= 1
    while reports[-1].rescan_laps == reports[0].rescan_laps:
        reports.append(_tick(app, prefix, rescan_per_tick=1, anchors_per_tick=1))
        assert reports[-1].frames_rescanned <= 1
        assert len(reports) <= top + 2, "a lap must end once the rescan reaches the cursor"
    assert len(reports) >= top - 1, "a bound of 1 cannot lap in fewer ticks than ids"


def test_e10_the_job_is_registered_bounded_and_runs_as_the_app_role():
    """Through the runner as meshychat_app. Without a configured server signing key it writes
    nothing and still succeeds (its warning says why); with one it anchors."""
    base = registry.by_name("chain_head_reconcile")
    assert base.max_batch == CH.FRAMES_PER_TICK and base.interval_secs == 60
    job = registry.Job(name=f"t_chain_head_{uuid.uuid4().hex[:8]}",
                       interval_secs=base.interval_secs, timeout_secs=base.timeout_secs,
                       jitter_frac=base.jitter_frac, max_batch=base.max_batch, run=base.run)
    try:
        (out,) = runner.run_due(lambda **kw: dbconn.connect(DSN, **kw), jobs=(job,),
                                holder="chain-head-test")
    finally:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job.name,))
    assert out.claimed is True and out.result == "ok", out


# ------------------------------------------------------------------ D08, the known orphans

INCIDENT = "asdlc incidents/2026-09-21-retention-orphans-chain-anchors.md"

#: The six anchors the operator-approved read of the production standby found (2026-09-27,
#: replayed to 07:44Z): (chain_id, head_frame_id, signed frame_count), all under one kid.
PRODUCTION_ORPHANS = {
    ("9309b1aa-531e-42a6-a571-d4618f49c4c1", 74666, 862),
    ("259a99bf-7b90-4d45-bd59-cbd5cdec5075", 75551, 192),
    ("5476fcb2-28e9-4b0f-bb71-451dec8f9a5c", 75557, 49),
    ("6e9bda54-26c4-41e0-9fc1-3e4077934d45", 101143, 5),
    ("b1e70ac6-80c0-457c-8314-d8d45aa0defc", 116291, 2),
    ("d0d660a1-1522-4141-a24f-433070732f30", 116358, 3),
}
PRODUCTION_KID = "6dd0bdfd0cc2554b"


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def records():
    """Every record the module logs (its logger is under `meshychat`, which does not
    propagate to the root, so caplog alone would see nothing)."""
    lg = logging.getLogger(CH.log.name)
    h, level = _Records(), lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        yield h.records
    finally:
        lg.removeHandler(h)
        lg.setLevel(level)


def _lines(records, chain_id, start: int = 0) -> list[tuple[str, str]]:
    """(level, message) of each record from `start` on that names `chain_id`."""
    return [(r.levelname, r.getMessage()) for r in records[start:]
            if str(chain_id) in r.getMessage()]


def _levels(records, chain_id, start: int = 0) -> list[str]:
    return [lv for lv, _ in _lines(records, chain_id, start)]


def _orphan(owner, app, prefix, n: int = 3):
    """The production shape: a chain anchored at `n` frames, then EVERY frame of it deleted
    (head missing, 0 live frames). Returns (chain_id as str, its anchor row)."""
    c = F.chain(owner, n)
    _drain(app, prefix)
    _assert_exact(owner, c)
    owner.execute("DELETE FROM invocation_frames WHERE chain_id = %s", (c[0]["chain_id"],))
    assert owner.execute("SELECT id FROM invocation_frames WHERE chain_id = %s LIMIT 1",
                         (c[0]["chain_id"],)).fetchone() is None
    return str(c[0]["chain_id"]), _anchor(owner, c[0]["chain_id"])


def _entry(chain_id, anchor, **over) -> dict:
    e = {"chain_id": str(chain_id), "head_frame_id": anchor["head_frame_id"],
         "frame_count": anchor["frame_count"], "signing_kid": anchor["signing_kid"],
         "incident": INCIDENT}
    e.update(over)
    return e


def _doc(*entries) -> dict:
    return {"about": "test list", "orphans": list(entries)}


def _known(*entries) -> "CH.KnownOrphans":
    return CH.KnownOrphans(CH.parse_known_orphans(_doc(*entries)))


def _db_times(owner, *offsets: str) -> list:
    """Timestamps from ONE clock, the database's: now() and now() plus each interval."""
    cols = ", ".join(["t0"] + [f"t0 + interval '{o}'" for o in offsets])
    return list(owner.execute(f"SELECT {cols} FROM (SELECT now() AS t0) s").fetchone())


def test_d08_the_committed_list_holds_exactly_the_six_production_orphans():
    """The committed file passes its own validator (the positive control for every refusal
    below) and holds exactly the six anchors the standby read found, each under the one kid,
    each naming the incident. The list is committed data, not configuration: this test is
    what makes an edit to it visible."""
    got = CH.load_known_orphans()
    assert CH.KNOWN_ORPHANS_PATH.name == "chain_head_known_orphans.json"
    assert set(got) == {(c, h, n, PRODUCTION_KID) for c, h, n in PRODUCTION_ORPHANS}, got
    assert set(got.values()) == {INCIDENT}


_GOOD = {"chain_id": "9309b1aa-531e-42a6-a571-d4618f49c4c1", "head_frame_id": 74666,
         "frame_count": 862, "signing_kid": PRODUCTION_KID, "incident": INCIDENT}


def _without(key: str) -> dict:
    return {k: v for k, v in _GOOD.items() if k != key}


@pytest.mark.parametrize("doc", [
    pytest.param([dict(_GOOD)], id="top-level-not-an-object"),
    pytest.param({"orphans": [dict(_GOOD)]}, id="about-missing"),
    pytest.param({"about": "x", "orphans": [dict(_GOOD)], "extra": 1}, id="top-extra-key"),
    pytest.param({"about": "", "orphans": [dict(_GOOD)]}, id="about-empty"),
    pytest.param({"about": "x", "orphans": dict(_GOOD)}, id="orphans-not-a-list"),
    pytest.param(_doc(_without("signing_kid")), id="entry-missing-kid"),
    pytest.param(_doc(_without("frame_count")), id="entry-missing-count"),
    pytest.param(_doc({**_GOOD, "note": "x"}), id="entry-extra-key"),
    pytest.param(_doc({**_GOOD, "chain_id": "not-a-uuid"}), id="chain-not-a-uuid"),
    pytest.param(_doc({**_GOOD, "chain_id": _GOOD["chain_id"].upper()}), id="chain-uppercase"),
    pytest.param(_doc({**_GOOD, "chain_id": _GOOD["chain_id"].replace("-", "")}),
                 id="chain-no-hyphens"),
    pytest.param(_doc({**_GOOD, "head_frame_id": "74666"}), id="head-a-string"),
    pytest.param(_doc({**_GOOD, "head_frame_id": True}), id="head-a-bool"),
    pytest.param(_doc({**_GOOD, "head_frame_id": 0}), id="head-zero"),
    pytest.param(_doc({**_GOOD, "frame_count": -1}), id="count-negative"),
    pytest.param(_doc({**_GOOD, "frame_count": 862.0}), id="count-a-float"),
    pytest.param(_doc({**_GOOD, "signing_kid": "6DD0BDFD0CC2554B"}), id="kid-uppercase"),
    pytest.param(_doc({**_GOOD, "signing_kid": "6dd0bdfd0cc2554"}), id="kid-15-chars"),
    pytest.param(_doc({**_GOOD, "signing_kid": None}), id="kid-null"),
    pytest.param(_doc({**_GOOD, "incident": ""}), id="incident-empty"),
    pytest.param(_doc(dict(_GOOD), {**_GOOD, "frame_count": 861}), id="chain-listed-twice"),
    pytest.param(_doc(_GOOD["chain_id"]), id="entry-not-an-object"),
])
def test_d08_a_malformed_list_is_refused_at_load(doc):
    """Each malformation raises KnownOrphansError; nothing is half-loaded. CONTROL: the same
    shapes, correct, parse (the first two assertions), so each refusal is the malformation's."""
    assert CH.parse_known_orphans(_doc(dict(_GOOD))) == {
        (_GOOD["chain_id"], 74666, 862, PRODUCTION_KID): INCIDENT}
    assert CH.parse_known_orphans(_doc()) == {}, "control: an empty list is a valid list"
    with pytest.raises(CH.KnownOrphansError):
        CH.parse_known_orphans(doc)


def test_d08_a_list_file_that_is_not_json_or_is_missing_is_refused(tmp_path):
    """The file path: bad JSON, a duplicated key (json.loads would keep the last one
    silently), and a missing file each raise KnownOrphansError. CONTROL: the good document
    written to a file loads."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_doc(dict(_GOOD))), encoding="utf-8")
    assert len(CH.load_known_orphans(good)) == 1
    bad = tmp_path / "bad.json"
    bad.write_text('{"about": "x", "orphans": [', encoding="utf-8")
    dup = tmp_path / "dup.json"
    dup.write_text('{"about": "x", "about": "y", "orphans": []}', encoding="utf-8")
    for path in (bad, dup, tmp_path / "absent.json"):
        with pytest.raises(CH.KnownOrphansError):
            CH.load_known_orphans(path)


def test_d08_a_listed_orphan_warns_naming_its_incident_and_an_unlisted_one_errors(
        owner, app, records):
    """Two anchors in the production shape (every frame deleted); ONE is on the list. One
    tick: the listed one logs one WARN naming the incident and no ERROR; the unlisted one
    logs ERROR and no WARN, as before this change. Both are still TRUNCATED in the report and
    both anchors are byte-identical (no audit data moves). CONTROL in the same tick: a sound
    chain that gains a frame is extended."""
    prefix = _prefix()
    listed_id, listed = _orphan(owner, app, prefix)
    unlisted_id, unlisted = _orphan(owner, app, prefix, n=2)
    control = F.chain(owner, 2)
    _drain(app, prefix)
    control.append(F.extend(owner, control))
    known = _known(_entry(listed_id, listed))
    start = len(records)
    r = _tick(app, prefix, orphans=known)

    assert _outcome(r, listed_id).verdict == CH.TRUNCATED
    assert _outcome(r, unlisted_id).verdict == CH.TRUNCATED
    assert _levels(records, listed_id, start) == ["WARNING"], _lines(records, listed_id, start)
    assert INCIDENT in _lines(records, listed_id, start)[0][1]
    assert _levels(records, unlisted_id, start) == ["ERROR"], _lines(records, unlisted_id,
                                                                    start)
    assert [o.chain_id for o in r.known_orphans] == [listed_id]
    assert {o.chain_id for o in r.findings} >= {listed_id, unlisted_id}, "the verdict is kept"
    assert _anchor(owner, listed_id) == listed and _anchor(owner, unlisted_id) == unlisted
    _assert_exact(owner, control)


def test_d08_a_listed_chain_whose_anchor_differs_from_its_entry_still_errors(
        owner, app, records):
    """Match is EXACT on chain, head, count and kid. Three orphans each listed by its chain
    id with ONE thing different from its anchor (the head, the count, the kid), and one
    listed exactly whose anchor's signature was then tampered (bad_signature: only a
    truncation is ever excused). Each logs ERROR, no WARN, and each truncated one's ERROR
    names the entry it failed to match. CONTROL in the same tick: an orphan listed exactly
    WARNs."""
    prefix = _prefix()
    exact_id, exact = _orphan(owner, app, prefix)
    head_id, head = _orphan(owner, app, prefix)
    count_id, count = _orphan(owner, app, prefix)
    kid_id, kid = _orphan(owner, app, prefix)
    sig_id, sig = _orphan(owner, app, prefix)
    other_kid = "0123456789abcdef"
    assert other_kid != kid["signing_kid"]
    known = _known(_entry(exact_id, exact),
                   _entry(head_id, head, head_frame_id=head["head_frame_id"] + 1),
                   _entry(count_id, count, frame_count=count["frame_count"] + 1),
                   _entry(kid_id, kid, signing_kid=other_kid),
                   _entry(sig_id, sig))
    forged = bytes([sig["head_signature"][0] ^ 1]) + bytes(sig["head_signature"][1:])
    owner.execute("UPDATE invocation_chain_heads SET head_signature = %s WHERE chain_id = %s",
                  (forged, sig_id))
    start = len(records)
    r = _tick(app, prefix, orphans=known)

    assert _levels(records, exact_id, start) == ["WARNING"], _lines(records, exact_id, start)
    for cid in (head_id, count_id, kid_id):
        assert _outcome(r, cid).verdict == CH.TRUNCATED
        lines = _lines(records, cid, start)
        assert [lv for lv, _ in lines] == ["ERROR"], lines
        assert "known-orphans entry" in lines[0][1], lines
    assert _outcome(r, sig_id).verdict == CH.BAD_SIGNATURE
    assert _levels(records, sig_id, start) == ["ERROR"], _lines(records, sig_id, start)
    assert [o.chain_id for o in r.known_orphans] == [exact_id]


def test_d08_the_warn_is_once_per_anchor_at_start_then_at_most_hourly_on_the_db_clock(
        owner, app, records):
    """The cadence, bounded. Two listed orphans; the same KnownOrphans (one process) ticks at
    database times t0, +1 min, +59:59, +60 min, +61 min, +120 min. Each anchor WARNs at t0
    (process start), +60 and +120 and at no other tick: 3 lines each where one per lap would
    be 6. Per anchor, not one limiter for all: both WARN at t0. The verdict is still in every
    tick's report (the rate limit is on the log line, not the audit). CONTROL, a new process:
    a fresh KnownOrphans WARNs again at once (+61 min)."""
    prefix = _prefix()
    a_id, a = _orphan(owner, app, prefix)
    b_id, b = _orphan(owner, app, prefix, n=2)
    entries = (_entry(a_id, a), _entry(b_id, b))
    known = _known(*entries)
    times = _db_times(owner, "1 minute", "59 minutes 59 seconds", "60 minutes",
                      "61 minutes", "120 minutes")
    per_tick = []
    for t in times:
        start = len(records)
        r = _tick(app, prefix, orphans=known, now=t)
        assert {o.chain_id for o in r.known_orphans} >= {a_id, b_id}
        per_tick.append((_levels(records, a_id, start), _levels(records, b_id, start)))
    w = ["WARNING"]
    assert per_tick == [(w, w), ([], []), ([], []), (w, w), ([], []), (w, w)], per_tick
    start = len(records)
    _tick(app, prefix, orphans=_known(*entries), now=times[4])
    assert (_levels(records, a_id, start), _levels(records, b_id, start)) == (w, w)


def test_d08_the_job_counts_only_unlisted_refusals_and_a_malformed_list_excuses_nothing(
        owner, app, records, monkeypatch, tmp_path):
    """Through `run_job`, the path production runs, with the list read from a file. A listed
    orphan logs WARN and is not in the tick's "N anchor(s) refused" ERROR count. The same
    job with the list file MALFORMED: an ERROR names the list file, the orphan's line is
    ERROR (nothing excused), and the refused count is one higher, the orphan's. The two runs
    read the same database after a warm-up run (which lets pass 1's cursor, shared with the
    rest of the run's database, catch up), so the difference is the list's alone."""
    from types import SimpleNamespace
    from apps.calls import result_signing
    prefix = _prefix()
    cid, anchor = _orphan(owner, app, prefix)
    good = tmp_path / "orphans.json"
    good.write_text(json.dumps(_doc(_entry(cid, anchor))), encoding="utf-8")
    bad = tmp_path / "malformed.json"
    bad.write_text(json.dumps(_doc({**_entry(cid, anchor), "note": "x"})), encoding="utf-8")
    monkeypatch.setattr(result_signing, "server_result_signer", lambda: SIGNER)
    monkeypatch.setattr(CH.frame_audit, "verifier_keys", lambda: dict(F.KEYS))
    monkeypatch.setattr(CH, "ANCHORS_PER_TICK", 1_000_000)   # one pass covers every anchor
    job = SimpleNamespace(max_batch=CH.FRAMES_PER_TICK)

    def run(path, fresh: bool = True) -> tuple[list[str], list[str], int]:
        monkeypatch.setattr(CH, "KNOWN_ORPHANS_PATH", path)
        if fresh:
            monkeypatch.setattr(CH, "_ORPHANS", None)   # a new process: the list is read anew
        owner.execute("DELETE FROM background_job_cursors WHERE cursor_name = %s",
                      (CH.CURSOR_ANCHORS,))           # the anchors pass starts at the first
        start = len(records)
        CH.run_job(app, job)
        msgs = [(r.levelname, r.getMessage()) for r in records[start:]]
        summary = [m for lv, m in msgs if lv == "ERROR" and "refused this tick" in m]
        listing = [m for lv, m in msgs if lv == "ERROR" and "known-orphans list" in m
                   and "malformed" in m]
        n = int(summary[0].split(": ")[1].split(" ")[0]) if summary else 0
        return _levels(records, cid, start), listing, n

    run(good)             # warm-up: pass 1 catches up, so both measured runs see one state
    lv_good, listing_good, n_good = run(good)
    lv_bad, listing_bad, n_bad = run(bad)
    assert lv_good == ["WARNING"] and listing_good == [], (lv_good, listing_good)
    assert lv_bad == ["ERROR"], lv_bad
    assert len(listing_bad) == 1 and str(bad) in listing_bad[0], listing_bad
    assert n_bad == n_good + 1, (n_good, n_bad)
    # Review finding 2 (LOW): a refused list is RETRIED every tick, in the same process
    # (`_ORPHANS` not reset), so its ERROR recurs and a fixed file is picked up without a
    # restart. The next tick, still malformed: the list ERROR again, the orphan still ERROR.
    lv_again, listing_again, n_again = run(bad, fresh=False)
    assert lv_again == ["ERROR"], lv_again
    assert len(listing_again) == 1 and str(bad) in listing_again[0], listing_again
    assert n_again == n_bad, (n_bad, n_again)
    # CONTROL, the same process with the file fixed: it is read at the next tick and the
    # orphan is excused (a first sighting in this process, so its WARNING is due).
    lv_fixed, listing_fixed, n_fixed = run(good, fresh=False)
    assert lv_fixed == ["WARNING"] and listing_fixed == [], (lv_fixed, listing_fixed)
    assert n_fixed == n_good, (n_good, n_fixed)


def test_d08_due_counts_real_time_across_a_summer_time_change():
    """mg388 (25 Oct): in a Europe/Berlin session, the ticks an hour and two hours after 23:30Z
    on 24 Oct read 02:30 CEST and 02:30 CET, a real hour apart and equal on the wall. `due`
    must say True at each. CONTROL: compared as given (wall clock, same tzinfo), the second
    tick is inside the first's hour, which is the comparison that suppressed the WARNING."""
    import datetime as dt
    import zoneinfo
    berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    t0, t60, t120 = (dt.datetime(2026, 10, 24, 23, 30, tzinfo=dt.timezone.utc)
                     + dt.timedelta(minutes=m) for m in (0, 60, 120))
    w0, w60, w120 = (t.astimezone(berlin) for t in (t0, t60, t120))
    assert w60 <= w120 < w60 + CH.KNOWN_ORPHAN_WARN_EVERY, "control: the wall-clock window holds the 120 min tick"
    key = (_GOOD["chain_id"], 74666, 862, PRODUCTION_KID)
    known = CH.KnownOrphans({key: INCIDENT})
    listed = CH.Outcome(_GOOD["chain_id"], CH.TRUNCATED, 0, 0, 74666, 862, False, PRODUCTION_KID)
    assert [known.due(listed, w) for w in (w0, w60, w120)] == [True, True, True]
    assert known.due(listed, w120 + dt.timedelta(minutes=30)) is False, "inside the hour it stays quiet"
    # The old rule's other wrong edge (pxx 6ece88): 30 real minutes after the +60 tick reads 02:00 CET, which on the
    # wall is BEFORE 02:30 CEST, so it looked like the clock went back and warned early.
    early = CH.KnownOrphans({key: INCIDENT})
    assert early.due(listed, w60) is True
    assert early.due(listed, (t60 + dt.timedelta(minutes=30)).astimezone(berlin)) is False, "warned 30 min early"


def test_d08_due_refuses_an_anchor_the_list_does_not_hold(owner):
    """Review finding 3 (LOW): the bound the LEDGER row cites. `due` answers False for an
    anchor the list does not hold, and records nothing, so `_warned` holds at most one key
    per list entry whatever `incident()` lets through. Unlisted: another chain, and the
    listed chain at another count. CONTROL: the listed anchor is due at once, and is the
    only key recorded. Times from the database's clock."""
    key = (_GOOD["chain_id"], 74666, 862, PRODUCTION_KID)
    known = CH.KnownOrphans({key: INCIDENT})
    (t,) = _db_times(owner)
    other = CH.Outcome(str(uuid.uuid4()), CH.TRUNCATED, 0, 0, 74666, 862, False,
                       PRODUCTION_KID)
    recount = CH.Outcome(_GOOD["chain_id"], CH.TRUNCATED, 0, 0, 74666, 861, False,
                         PRODUCTION_KID)
    assert known.due(other, t) is False and known.due(recount, t) is False
    assert known._warned == {}, known._warned
    listed = CH.Outcome(_GOOD["chain_id"], CH.TRUNCATED, 0, 0, 74666, 862, False,
                        PRODUCTION_KID)
    assert known.due(listed, t) is True
    assert known.due(other, t) is False and known.due(recount, t) is False
    assert set(known._warned) == {key}, known._warned


def test_d08_a_listed_anchor_is_excused_only_while_its_head_is_gone_and_its_chain_empty(
        owner, app, records):
    """Review finding 1 (MEDIUM): the list records each orphan as signed head MISSING and 0
    live frames, and only that state is excused. Three orphans, each listed EXACTLY on the
    four fields. HEAD BACK: a self-consistent unsigned frame inserted under the signed
    head's id in its chain, with another hash (the frame audit's per-frame check passes it;
    the anchor is its only witness): the tampering case the verdict exists for. FRAMES BACK:
    the chain gains two new frames past the head. Each is TRUNCATED, logs ERROR and no
    WARNING, the ERROR names the entry, it is not in `known_orphans`, and its anchor is
    byte-identical. CONTROL in the same tick: the third, still head missing and empty,
    WARNs naming the incident."""
    from psycopg.rows import dict_row
    from apps.calls import frame_audit as FA
    prefix = _prefix()
    exact_id, exact = _orphan(owner, app, prefix)
    back_id, back = _orphan(owner, app, prefix)
    grown_id, grown = _orphan(owner, app, prefix)
    known = _known(_entry(exact_id, exact), _entry(back_id, back), _entry(grown_id, grown))
    put = F.insert(owner, F.base_row(chain_id=uuid.UUID(back_id)),
                   frame_id=back["head_frame_id"])
    assert put["frame_hash"] != back["head_hash"], "control: the head came back different"
    with app.cursor(row_factory=dict_row) as cur:
        cur.execute(FA._SELECT + " WHERE id = %s", (put["id"],))
        stored = cur.fetchone()
    assert FA.self_check(stored, F.KEYS) is None, "the frame audit sees a sound frame"
    F.chain(owner, 2, chain_id=uuid.UUID(grown_id))
    start = len(records)
    r = _tick(app, prefix, orphans=known)

    assert _levels(records, exact_id, start) == ["WARNING"], _lines(records, exact_id, start)
    assert INCIDENT in _lines(records, exact_id, start)[0][1]
    for cid, a in ((back_id, back), (grown_id, grown)):
        assert _outcome(r, cid).verdict == CH.TRUNCATED
        lines = _lines(records, cid, start)
        assert lines and {lv for lv, _ in lines} == {"ERROR"}, lines
        assert all("known-orphans entry" in m for _, m in lines), lines
        assert _anchor(owner, cid) == a
    assert [o.chain_id for o in r.known_orphans] == [exact_id]
