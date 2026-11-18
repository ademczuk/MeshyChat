"""The client's read ledger: `<state file>.read.json`, the reads this identity's client VERIFIED, for
meshy-mod's unread band (tools/meshy_node/node.note_read).

Why it exists (txxxx, 2026-10-05): the mod judged a read by parsing the shell line around it and counted
it only when the read decided the command's exit or printed the message, so a read piped, redirected or
followed by anything left the band up. Of 2,363 reads in six HQB transcripts, 2,170 were in such a form,
and seats showing 55, 4 and 10 unread had in fact read their mail. The process that verified the read
now says so, whatever the shell around it did.

kxxx's conditions (msg/<key>, msg/<key>): a record only after BOTH
the writer's signature and the status check verified; one record per key and version; written atomically
beside the state file; a failed verification writes nothing; a lock held by another process costs a read
at most READ_LEDGER_LOCK_WAIT and the record is simply missing. On its own the file is never a receipt.

No server, no database: `invoke` and the two checks are faked, as tests/test_postbox_fetch_gap.py does.

Run:  bash ../runt-wt.sh <worktree> tests/test_meshy_node_read_ledger.py -q
Mutants (each a one-line break in node.py, each seen to fail a test here):
  L1  the writer's signature is not required          test_both_checks_are_required_on_their_own
  L2  the status check is not required                test_both_checks_are_required_on_their_own,
                                                       test_a_read_that_did_not_verify_records_nothing
  L3  a re-read of a recorded version adds a record   test_one_record_per_key_and_version
  L4  the ledger is not bounded                        test_the_ledger_keeps_the_newest_records
  L5  a ledger written for another peer is kept        test_a_damaged_or_foreign_ledger_is_replaced
  L6  the ledger is written in place, not replaced     test_the_ledger_is_replaced_through_a_temp_file
  L7  the record is written without the lock           test_a_lock_held_by_another_process_skips_the_record_within_the_bound
  L8  a read waits for the lock without a bound        test_a_lock_held_by_another_process_skips_the_record_within_the_bound
  L9  read() does not record                           test_a_verified_read_records_key_and_version_under_the_peer
  L10 a damaged ledger raises instead of reading empty test_a_damaged_or_foreign_ledger_is_replaced

WHAT A RECORD HOLDS (kxxx, msg/<key>): {key, version, at}, his original rule. Item 8 added the
message's thread and a kind (read or send); kxxx dropped both once `card hook` read the activity file below instead,
and the ledger holds READS only again. A record an earlier client wrote with them still loads, without them.
  T3  a loaded record keeps every field it had                 test_no_body_text_reaches_either_file
  T4  an old-format record is rejected on load                 test_an_old_ledger_with_thread_and_kind_still_loads
  T8  a send is recorded in the ledger                         test_a_verified_send_is_observed_and_never_in_the_ledger
  T9  a record holding thread and kind fails to load           test_an_old_ledger_with_thread_and_kind_still_loads

SESSION ACTIVITY (Codex rounds 2 and 3 on item 8; kxxx's conditions, a26d9a): beside the ledger,
`<state file>.activity.json` holds which Claude Code session read or sent which message, in which thread, and when:
{session, key, at, thread, kind}, one observation per verified read or send (the client's own sends included),
APPENDED under the ledger's lock and never replaced. The session is the local CLAUDE_CODE_SESSION_ID, never a
message's session_label. Pruned on every write to the hook's window and a margin; bounded per session and per file,
and what a bound takes is marked per session (`dropped`), so the hook abstains over it. Written with the state
file's permissions. `card hook` infers from it (tests/test_meshy_node_card_hook.py).
  T1  sends are not observed (send() does not call note_sent)  test_a_verified_send_is_observed_and_never_in_the_ledger
  T2  a body is recorded as the thread (read or send)          test_no_body_text_reaches_either_file
  T5  the thread is not bounded by the server's rule
                                                     test_a_thread_over_the_cap_or_of_the_wrong_type_is_not_recorded
  T6  an unverified or refused send is observed                test_a_send_that_did_not_verify_records_nothing
  R1  a reread is not observed                    test_a_reread_is_an_observation_and_never_touches_the_receipt
  R3  a reread replaces the earlier observation   test_a_reread_is_an_observation_and_never_touches_the_receipt
  B2  a message's session_label is the binding    test_a_session_label_never_binds_the_activity
  B4  an entry is written with no session         test_no_session_no_activity
  P1  no prune on write                           test_every_write_prunes_to_the_window_and_its_margin
  P2  marks are never pruned                      test_what_a_bound_takes_is_marked_for_its_session
  C1  no cap (neither bound)                      test_the_bounds_are_the_chosen_ones_and_mark_what_they_take
  A2  a bound takes evidence unmarked             test_what_a_bound_takes_is_marked_for_its_session,
                                                  test_the_bounds_are_the_chosen_ones_and_mark_what_they_take
  F1  the file gets other permissions             test_the_activity_file_has_the_state_files_permissions
  F2  a damaged file raises instead of reading empty
                                                  test_the_activity_file_is_replaced_whole_and_a_damaged_one_replaced
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.memoryskills.validation import MESH_THREAD_RE  # noqa: E402
from tools.meshy_node import cards, node, wire  # noqa: E402

PEER = "0447bf92-25fb-5230-b531-5a0d9c00d8c7"
WRITER = "11111111-1111-1111-1111-111111111111"
STATE = {"base_url": "http://fixture", "peer_id": PEER, "status": "bound"}
THREAD = "gap-register-item-8"
#: Planted in every text field a message has. It matches the thread rule on purpose, so a body or subject recorded
#: AS the thread would reach the file and be found.
MARKER = "PLANTED-BODY-MARKER-7f3a"


S1 = "6f1e0c2a-1111-4c3b-9a5d-0000000000a1"


@pytest.fixture(autouse=True)
def _outside_any_session(monkeypatch):
    """Every test starts outside a Claude Code session, as on CI, whatever shell runs pytest; a test that needs one
    sets it."""
    monkeypatch.delenv(node.SESSION_ENV, raising=False)


def _memo(key, version, thread=THREAD):
    value = {"body": MARKER, "subject": MARKER, "from": "writer", "to": ["seat"], "session_label": MARKER}
    if thread is not None:
        value["thread_id"] = thread
    return {"namespace": wire.MESH_NAMESPACE, "key": key, "version": version, "is_head": True,
            "created_at": "2026-10-05 06:00:00+00:00", "author_label": "writer", "value": value,
            "created_by_peer_id": WRITER}


@pytest.fixture
def fake(monkeypatch):
    """node.read against a fake server. `fake.outcome` picks what the next read meets:
    ok, writer_signature, status_check, refused or unverified_result. `fake.thread` is the thread_id the message
    carries (None: the value has none)."""
    class Fake:
        outcome = "ok"
        thread = THREAD

    monkeypatch.setattr(node, "require_peer", lambda *a, **k: None)

    def invoke(path, state, *, skill, args, **kw):
        if Fake.outcome == "refused":
            return {"status": "failed", "result_signature_verified": True, "call_id": "c1",
                    "error_code": "E_NOT_FOUND", "error_message": "no such key"}
        if Fake.outcome == "unverified_result":
            return {"status": "completed", "result_signature_verified": False, "call_id": "c1",
                    "result_signature_detail": "bad signature", "result": {"memo": _memo(args["key"], 1)}}
        return {"status": "completed", "result_signature_verified": True, "call_id": "c1",
                "result": {"memo": _memo(args["key"], args.get("version") or 1, Fake.thread)}}

    monkeypatch.setattr(node, "invoke", invoke)
    monkeypatch.setattr(wire, "author_signature_ok",
                        lambda m, keys: (False, "E_SIG: fixture") if Fake.outcome == "writer_signature" else (True, None))
    monkeypatch.setattr(wire, "status_check", lambda m, verified, why: (
        {"status_verified": False, "status_error": "fixture"} if (Fake.outcome == "status_check" or not verified)
        else {"status_verified": True, "author_signed": True}))
    return Fake


def read(state_path, key, version=None):
    return node.read(state_path, STATE, key=key, version=version)


def ledger(state_path):
    p = Path(f"{state_path}{node.READ_LEDGER_SUFFIX}")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def recorded(state_path):
    body = ledger(state_path)
    return [(r["key"], r["version"]) for r in body["reads"]] if body else []


def test_a_verified_read_records_key_and_version_under_the_peer(fake, tmp_path):
    """A read of a message whose signature and status both verified leaves one record, keys only.
    MUTANT L9: read() does not call note_read."""
    state = tmp_path / "seat-prod-state.json"
    message = read(state, "msg/1-a")
    assert message["signature_verified"] is True and message["status_verified"] is True
    body = ledger(state)
    assert set(body) == {"schema_version", "peer_id", "reads"}
    assert body["schema_version"] == 1 and body["peer_id"] == PEER
    assert [(r["key"], r["version"]) for r in body["reads"]] == [("msg/1-a", 1)]
    assert set(body["reads"][0]) == {"key", "version", "at"}, "no body, no sender, no preview, no thread"


@pytest.mark.parametrize("outcome", ["writer_signature", "status_check", "refused", "unverified_result"])
def test_a_read_that_did_not_verify_records_nothing(fake, tmp_path, outcome):
    """CONTROL first: a verified read of msg/1-ok is recorded, so the ledger path works. Then a read that
    fails one check adds nothing, whether it still printed the message (a writer signature or status
    that did not verify) or raised (a refusal, an unverified result).
    MUTANTS L1 (the writer's signature is not required) and L2 (the status check is not required)."""
    state = tmp_path / "seat-prod-state.json"
    read(state, "msg/1-ok")
    assert recorded(state) == [("msg/1-ok", 1)]
    fake.outcome = outcome
    if outcome in ("refused", "unverified_result"):
        with pytest.raises(node.ReadFailed):
            read(state, "msg/2-bad")
    else:
        message = read(state, "msg/2-bad")
        assert message["key"] == "msg/2-bad", "the message is still printed; it is only not recorded"
    assert recorded(state) == [("msg/1-ok", 1)]


def test_one_record_per_key_and_version(fake, tmp_path):
    """Reading a version again adds nothing; another version of the same key adds its own record, which
    is what lets the mod clear an edited message's row only when the edit itself was read.
    MUTANT L3: a re-read of a recorded version adds a record."""
    state = tmp_path / "seat-prod-state.json"
    read(state, "msg/1-a")
    read(state, "msg/1-a")
    read(state, "msg/1-a", version=1)
    assert recorded(state) == [("msg/1-a", 1)]
    read(state, "msg/1-a", version=2)
    assert recorded(state) == [("msg/1-a", 1), ("msg/1-a", 2)]


def test_the_ledger_keeps_the_newest_records(fake, tmp_path, monkeypatch):
    """MUTANT L4: the ledger is not bounded."""
    monkeypatch.setattr(node, "READ_LEDGER_MAX", 3)
    state = tmp_path / "seat-prod-state.json"
    for i in range(5):
        read(state, f"msg/{i}-a")
    assert recorded(state) == [("msg/2-a", 1), ("msg/3-a", 1), ("msg/4-a", 1)]


@pytest.mark.parametrize("old", ["{not json", json.dumps({"schema_version": 1, "peer_id": "33333333-3333-3333-3333-333333333333",
                                                           "reads": [{"key": "msg/0-old", "version": 1, "at": "x"}]}),
                                 json.dumps({"schema_version": 2, "peer_id": PEER,
                                             "reads": [{"key": "msg/0-old", "version": 1, "at": "x"}]}),
                                 json.dumps([{"key": "msg/0-old", "version": 1}])])
def test_a_damaged_or_foreign_ledger_is_replaced(fake, tmp_path, old):
    """A file that is not JSON, was written for another peer, has another schema or another shape reads as
    empty, and the next verified read replaces it. A state file reused by a new identity must not inherit
    the old identity's reads. MUTANTS L5 (another peer's ledger is kept) and L10 (a damaged file raises)."""
    state = tmp_path / "seat-prod-state.json"
    Path(f"{state}{node.READ_LEDGER_SUFFIX}").write_text(old, encoding="utf-8")
    read(state, "msg/1-a")
    assert recorded(state) == [("msg/1-a", 1)]
    assert ledger(state)["peer_id"] == PEER


def test_the_ledger_is_replaced_through_a_temp_file(fake, tmp_path, monkeypatch):
    """The ledger is written whole to a temp file in the same folder and os.replace'd, so the mod never
    reads half a file. MUTANT L6: the ledger is written in place."""
    state = tmp_path / "seat-prod-state.json"
    replaced = []
    real = node.os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        return real(src, dst)
    monkeypatch.setattr(node.os, "replace", spy)
    read(state, "msg/1-a")
    to_ledger = [(s, d) for s, d in replaced if Path(d) == Path(f"{state}{node.READ_LEDGER_SUFFIX}")]
    assert len(to_ledger) == 1
    src, dst = to_ledger[0]
    assert Path(src) != Path(dst) and Path(src).parent == Path(dst).parent
    assert not list(tmp_path.glob(".read-*")), "no temp file left behind"


HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from tools.meshy_node import node
fd = node._sidecar_lock(sys.argv[2], 5.0)
print("locked" if fd is not None else "not-locked", flush=True)
time.sleep(float(sys.argv[3]))
"""


def test_a_lock_held_by_another_process_skips_the_record_within_the_bound(fake, tmp_path):
    """kxxx's ask (msg/<key>): another PROCESS holds the ledger's lock. The read still
    returns its message, after waiting about READ_LEDGER_LOCK_WAIT and no longer, and no record is written.
    The wait is measured as well as bounded, so the test cannot pass on a lock that was never contended.
    CONTROL: once the holder exits, the same read is recorded.
    MUTANTS L7 (the record is written without the lock) and L8 (the wait has no bound)."""
    state = tmp_path / "seat-prod-state.json"
    lock = f"{state}{node.READ_LEDGER_SUFFIX}{node.READ_LEDGER_LOCK_SUFFIX}"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), lock, "30"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        got, started = {}, time.monotonic()
        t = threading.Thread(target=lambda: got.update(message=read(state, "msg/1-a"),
                                                       took=time.monotonic() - started), daemon=True)
        t.start()
        t.join(timeout=20)
        assert not t.is_alive(), "the read is still waiting on the lock after 20 s"
        assert got["message"]["key"] == "msg/1-a", "the read itself succeeds"
        assert node.READ_LEDGER_LOCK_WAIT - 0.3 <= got["took"] < node.READ_LEDGER_LOCK_WAIT + 5
        assert ledger(state) is None, "no record without the lock"
    finally:
        holder.kill()
        holder.wait(timeout=10)
    read(state, "msg/1-a")
    assert recorded(state) == [("msg/1-a", 1)]


@pytest.mark.parametrize("checks", [{"signature_verified": False, "status_verified": True},
                                    {"signature_verified": True, "status_verified": False},
                                    {"signature_verified": True}, {"status_verified": True}, {}])
def test_both_checks_are_required_on_their_own(tmp_path, checks):
    """wire.status_check is never true when the signature did not verify, so through read() the status
    check alone would also keep an unsigned message out. kxxx's condition is BOTH, so each is checked
    here on its own, against a message the other check passed. CONTROL: both true records.
    MUTANTS L1 (the writer's signature is not required) and L2 (the status check is not required)."""
    state = tmp_path / "seat-prod-state.json"
    assert node.note_read(state, STATE, {"key": "msg/1-a", "version": 1, **checks}) is False
    assert ledger(state) is None
    assert node.note_read(state, STATE, {"key": "msg/1-a", "version": 1, "signature_verified": True,
                                         "status_verified": True}) is True


def test_a_wiki_row_or_a_message_without_a_version_records_nothing(tmp_path):
    """Only a msg/ key with a positive integer version is mail the band can clear."""
    state = tmp_path / "seat-prod-state.json"
    ok = {"signature_verified": True, "status_verified": True}
    assert node.note_read(state, STATE, {"key": "wiki/x", "version": 1, **ok}) is False
    assert node.note_read(state, STATE, {"key": "msg/1-a", "version": None, **ok}) is False
    assert node.note_read(state, STATE, {"key": "msg/1-a", "version": True, **ok}) is False
    assert node.note_read(state, {"peer_id": None}, {"key": "msg/1-a", "version": 1, **ok}) is False
    assert ledger(state) is None
    assert node.note_read(state, STATE, {"key": "msg/1-a", "version": 1, **ok}) is True


# ---- sends, and what each file holds (gap register item 8; kxxx, msg/<key> and
# msg/<key>)

@pytest.fixture
def sends(fake, monkeypatch):
    """node.send against a fake memory.write, beside `fake`'s reads. `sends.outcome` picks what the next write
    meets: ok, refused, unverified, running, ok_false, other_key or no_version."""
    class Sends:
        outcome = "ok"

    read_invoke = node.invoke

    def invoke(path, state, *, skill, args, **kw):
        if skill != "memory.write":
            return read_invoke(path, state, skill=skill, args=args, **kw)
        answer = {"op": "memory.write", "namespace": args["namespace"], "key": args["key"], "version": 1,
                  "is_head": True}
        base = {"call_id": "c2", "invoke": {"http": 202, "status": "accepted"}, "error_code": None,
                "error_message": None, "result_signing_kid": "k1", "result_signature_detail": None}
        o = Sends.outcome
        if o == "refused":
            return {**base, "status": "failed", "result": None, "error_code": "E_MESH_SCHEMA",
                    "error_message": "refused", "result_signature_verified": True}
        if o == "unverified":
            return {**base, "status": "completed", "result": answer, "result_signature_verified": False,
                    "result_signature_detail": "bad signature"}
        if o == "running":
            return {**base, "status": "running", "result": None, "result_signature_verified": False}
        if o == "ok_false":
            return {**base, "status": "completed", "result": {**answer, "ok": False}, "result_signature_verified": True}
        if o == "other_key":
            return {**base, "status": "completed", "result": {**answer, "key": "msg/9-other"},
                    "result_signature_verified": True}
        if o == "no_version":
            return {**base, "status": "completed", "result": {k: v for k, v in answer.items() if k != "version"},
                    "result_signature_verified": True}
        return {**base, "status": "completed", "result": answer, "result_signature_verified": True}

    monkeypatch.setattr(node, "invoke", invoke)
    return Sends


def send(state_path, thread=THREAD):
    """A send whose every text field carries MARKER."""
    return node.send(state_path, {**STATE, "label": "seat"}, to=["other"], thread_id=thread, body=MARKER,
                     msg_type="note", author_family="claude", subject=MARKER, session_label=MARKER)


def activity(state_path):
    p = Path(f"{state_path}{node.ACTIVITY_SUFFIX}")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def observed(state_path):
    body = activity(state_path)
    return [(e["session"], e["key"], e["kind"], e.get("thread")) for e in body["activity"]] if body else []


def test_the_clients_thread_rule_is_the_servers():
    """An observation keeps a thread only when the server would take it as one (MESH_THREAD_RE, 1 to 256 characters)."""
    assert node.ACTIVITY_THREAD_RE.pattern == MESH_THREAD_RE.pattern


def test_a_verified_send_is_observed_and_never_in_the_ledger(sends, tmp_path, monkeypatch):
    """kxxx: the seat's own sends are in the hook's window (bd5a4b), and the ledger holds reads only, {key, version,
    at} (a26d9a). A verified send is an observation of this session, kind "send", with the thread it was sent on; the
    ledger has no record of it. A read after it is a "read" observation and a ledger record.
    MUTANTS T1 (send() does not call note_sent) and T8 (a send is recorded in the ledger)."""
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    sent = send(state)
    assert node.succeeded(sent)
    assert ledger(state) is None, "a send is not a read"
    assert observed(state) == [(S1, sent["key"], "send", THREAD)]
    read(state, "msg/1-a")
    assert recorded(state) == [("msg/1-a", 1)]
    assert observed(state) == [(S1, sent["key"], "send", THREAD), (S1, "msg/1-a", "read", THREAD)]


@pytest.mark.parametrize("outcome", ["refused", "unverified", "running", "ok_false", "other_key", "no_version"])
def test_a_send_that_did_not_verify_records_nothing(sends, tmp_path, monkeypatch, outcome):
    """txxxx's input: a refused or unverified send is never observed, or the window would hold a thread the seat is
    not on. Nor is one still running, one the server answered ok:false, or a signed answer that names another key or
    no version. CONTROL first: a verified send is observed. MUTANT T6: note_sent records without `succeeded`."""
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    first = send(state, thread="the-control-thread")
    assert observed(state) == [(S1, first["key"], "send", "the-control-thread")]
    sends.outcome = outcome
    send(state, thread="a-thread-not-joined")
    assert observed(state) == [(S1, first["key"], "send", "the-control-thread")]
    assert "a-thread-not-joined" not in Path(f"{state}{node.ACTIVITY_SUFFIX}").read_text(encoding="utf-8")
    assert ledger(state) is None


def test_a_send_takes_the_reads_lock(sends, tmp_path, monkeypatch):
    """One lock for both files: a lock not taken skips the observation, and the send itself still succeeds."""
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    monkeypatch.setattr(node, "_sidecar_lock", lambda *a, **k: None)
    assert node.succeeded(send(state)) and activity(state) is None and ledger(state) is None


def test_an_old_ledger_with_thread_and_kind_still_loads(fake, tmp_path):
    """kxxx (a26d9a): the ledger is {key, version, at} again, and a file that carries item 8's fields must still
    load. A record an older client wrote ({key, version, at}) and records item 8's client wrote ({key, version, at,
    thread, kind}: a read, a send, a kind no client knows, a thread that is no thread) all load as {key, version,
    at}; the next write keeps them in that form and adds its own beside them.
    MUTANTS T4 (an old record is rejected on load) and T9 (a record holding thread and kind fails to load)."""
    state = tmp_path / "seat-prod-state.json"
    old = [{"key": "msg/0-old", "version": 1, "at": "2026-10-05T06:00:00+00:00"},
           {"key": "msg/0-old", "version": 2, "at": "2026-10-05T06:01:00+00:00", "thread": THREAD, "kind": "read"},
           {"key": "msg/0-sent", "version": 1, "at": "2026-10-10T14:00:00+00:00", "thread": THREAD, "kind": "send"},
           {"key": "msg/0-odd", "version": 1, "at": "2026-10-10T14:01:00+00:00", "thread": 5, "kind": "peek"}]
    Path(f"{state}{node.READ_LEDGER_SUFFIX}").write_text(
        json.dumps({"schema_version": 1, "peer_id": PEER, "reads": old}), encoding="utf-8")
    loaded = [{"key": r["key"], "version": r["version"], "at": r["at"]} for r in old]
    assert node.ledger_records(state, STATE) == loaded
    read(state, "msg/1-a")
    assert ledger(state)["reads"][:4] == loaded
    assert recorded(state) == [("msg/0-old", 1), ("msg/0-old", 2), ("msg/0-sent", 1), ("msg/0-odd", 1), ("msg/1-a", 1)]
    assert all(set(r) == {"key", "version", "at"} for r in ledger(state)["reads"])


def test_no_body_text_reaches_either_file(sends, tmp_path, monkeypatch):
    """kxxx's third test, for both files: a marker planted in the body, subject and session label of a message READ
    and of a message SENT is nowhere in the ledger or the activity file. The marker would pass as a thread id, so
    text recorded as the thread is caught too. A ledger record that already holds a field it should not (a hand edit,
    item 8's client) loses it on the next write. MUTANTS T2 (the body is recorded as the thread) and T3 (a loaded
    record keeps every field)."""
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    path = Path(f"{state}{node.READ_LEDGER_SUFFIX}")
    path.write_text(json.dumps({"schema_version": 1, "peer_id": PEER, "reads": [
        {"key": "msg/0-old", "version": 1, "at": "2026-10-05T06:00:00+00:00", "kind": "read", "thread": THREAD,
         "body": MARKER}]}), encoding="utf-8")
    read(state, "msg/1-a")
    send(state)
    assert MARKER not in path.read_text(encoding="utf-8")
    assert MARKER not in Path(f"{state}{node.ACTIVITY_SUFFIX}").read_text(encoding="utf-8")
    assert all(set(r) == {"key", "version", "at"} for r in ledger(state)["reads"])
    assert [e["kind"] for e in activity(state)["activity"]] == ["read", "send"]
    assert all(set(e) <= {"session", "key", "at", "thread", "kind"} for e in activity(state)["activity"])


@pytest.mark.parametrize("thread", ["t" * 257, "", " padded", "has space", "-leading-dash", "ends-in-newline\n",
                                    123, True, ["x"], {"t": 1}, None])
def test_a_thread_over_the_cap_or_of_the_wrong_type_is_not_recorded(fake, sends, tmp_path, monkeypatch, thread):
    """A read or a send whose thread is not one by the server's rule is still observed (and the read still goes in
    the ledger, which the band needs), with no thread. CONTROL: a thread of exactly 256 characters, the server's cap,
    is recorded. MUTANT T5: the thread is not bounded."""
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    fake.thread = "t" * 256
    read(state, "msg/0-cap")
    fake.thread = thread
    read(state, "msg/1-a")
    sent = send(state, thread=thread)
    obs = activity(state)["activity"]
    assert [(e["key"], e["kind"]) for e in obs] == [("msg/0-cap", "read"), ("msg/1-a", "read"), (sent["key"], "send")]
    assert obs[0]["thread"] == "t" * 256
    assert "thread" not in obs[1] and "thread" not in obs[2]
    assert recorded(state) == [("msg/0-cap", 1), ("msg/1-a", 1)]


# ---- the session's activity (Codex rounds 2 and 3 on item 8; kxxx's conditions, msg/<key>)

@pytest.fixture
def clock(monkeypatch):
    """clock(minutes): node's clock set `minutes` after a fixed start; returns that instant."""
    start = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

    class Clock(datetime):
        at = start

        @classmethod
        def now(cls, tz=None):
            return cls.at.astimezone(tz) if tz is not None else cls.at

    monkeypatch.setattr(node, "datetime", Clock)

    def set_to(minutes):
        Clock.at = start + timedelta(minutes=minutes)
        return Clock.at
    return set_to


def test_a_reread_is_an_observation_and_never_touches_the_receipt(sends, tmp_path, monkeypatch, clock):
    """Round 2's finding 1: a verified reread is recent activity, an observation of its own at the reread's time.
    Round 3's finding 1: it never replaces the earlier one, which a question asked between the two still needs. The
    ledger's receipt keeps the first read's time and adds nothing (L3 still holds). The send-to-read transition: the
    seat's own send, read back, is a "read" observation beside the "send" one, and the read's receipt is the
    ledger's only record of that key. MUTANTS R1 and R3."""
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    first = clock(0)
    read(state, "msg/1-a")
    again = clock(5)
    assert node.read(state, STATE, key="msg/1-a")["key"] == "msg/1-a"
    assert ledger(state)["reads"] == [{"key": "msg/1-a", "version": 1, "at": first.isoformat()}]
    assert activity(state)["activity"] == [
        {"session": S1, "key": "msg/1-a", "at": first.isoformat(), "thread": THREAD, "kind": "read"},
        {"session": S1, "key": "msg/1-a", "at": again.isoformat(), "thread": THREAD, "kind": "read"}]

    sent_at = clock(6)
    sent = send(state, thread="the-own-send-thread")
    read_back = clock(8)
    node.note_read(state, STATE, {"key": sent["key"], "version": 1, "thread_id": "the-own-send-thread",
                                  "signature_verified": True, "status_verified": True})
    assert [r for r in ledger(state)["reads"] if r["key"] == sent["key"]] == [
        {"key": sent["key"], "version": 1, "at": read_back.isoformat()}]
    assert activity(state)["activity"][-2:] == [
        {"session": S1, "key": sent["key"], "at": sent_at.isoformat(), "thread": "the-own-send-thread", "kind": "send"},
        {"session": S1, "key": sent["key"], "at": read_back.isoformat(), "thread": "the-own-send-thread",
         "kind": "read"}]
    assert activity(state)["dropped"] == {}


def test_a_session_label_never_binds_the_activity(sends, tmp_path, monkeypatch):
    """Finding 2: the binding is this process's own CLAUDE_CODE_SESSION_ID. The message read and the message sent
    each carry MARKER as their session_label (a peer's, or the seat's own --session-label), shaped to pass as a
    session id: it is nowhere in the activity file, and a hook asking as MARKER is given no thread. CONTROL: asking
    as the local session, the thread. MUTANT B2."""
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    read(state, "msg/1-a")
    send(state)
    body = activity(state)
    assert [e["session"] for e in body["activity"]] == [S1, S1] and body["peer_id"] == PEER
    assert MARKER not in Path(f"{state}{node.ACTIVITY_SUFFIX}").read_text(encoding="utf-8")
    assert all(set(e) <= {"session", "key", "at", "thread", "kind"} for e in body["activity"])
    asked = datetime.now(timezone.utc).isoformat()
    assert node.valid_session(MARKER)
    assert cards.activity_thread(state, STATE, asked, MARKER) is None
    assert cards.activity_thread(state, STATE, asked, S1) == THREAD


@pytest.mark.parametrize("session", [None, "", "has space", "x" * 129, "-leading-dash"])
def test_no_session_no_activity(sends, tmp_path, monkeypatch, session):
    """A client run outside Claude Code (no CLAUDE_CODE_SESSION_ID), or with one not of a session id's shape, binds
    its reads and sends to no session: the ledger records the read as before and the activity file is not written.
    CONTROL: the same in a session writes it. MUTANT B4."""
    state = tmp_path / "seat-prod-state.json"
    if session is not None:
        monkeypatch.setenv(node.SESSION_ENV, session)
    read(state, "msg/1-a")
    send(state)
    assert recorded(state) == [("msg/1-a", 1)]
    assert activity(state) is None
    monkeypatch.setenv(node.SESSION_ENV, S1)
    read(state, "msg/1-a")
    assert [(e["session"], e["key"]) for e in activity(state)["activity"]] == [(S1, "msg/1-a")]


def test_every_write_prunes_to_the_window_and_its_margin(fake, tmp_path, monkeypatch, clock):
    """kxxx's condition a): observations older than the hook's window and a margin go on EVERY write, any session's,
    so the file never becomes a history of the threads the seat touched. The window is the hook's own
    (cards.INFER_WINDOW); the margin covers the mod's whole `card hook` run (CARD_HOOK_MS, read from meshy.js), which
    reads this file first. Kept at 10.9 minutes, gone at 11.1. MUTANT P1."""
    js = (ROOT / "tools/postbox/mod/meshy-mod/hooks/meshy.js").read_text(encoding="utf-8")
    card_hook_ms = int(re.search(r"const CARD_HOOK_MS = ([\d_]+)", js).group(1).replace("_", ""))
    assert node.ACTIVITY_MARGIN >= timedelta(milliseconds=card_hook_ms)
    assert node.ACTIVITY_KEEP == cards.INFER_WINDOW + node.ACTIVITY_MARGIN == timedelta(minutes=11)
    state = tmp_path / "seat-prod-state.json"
    S2 = "6f1e0c2a-2222-4c3b-9a5d-0000000000b2"
    monkeypatch.setenv(node.SESSION_ENV, S2)
    clock(0)
    read(state, "msg/0-b")
    monkeypatch.setenv(node.SESSION_ENV, S1)
    clock(0.5)
    read(state, "msg/1-a")
    clock(10.9)
    read(state, "msg/2-a")
    assert [e["key"] for e in activity(state)["activity"]] == ["msg/0-b", "msg/1-a", "msg/2-a"]
    clock(11.1)
    read(state, "msg/3-a")
    assert [e["key"] for e in activity(state)["activity"]] == ["msg/1-a", "msg/2-a", "msg/3-a"], "S2's went too"
    clock(30)
    read(state, "msg/4-a")
    assert [e["key"] for e in activity(state)["activity"]] == ["msg/4-a"]


def test_the_bounds_are_the_chosen_ones_and_mark_what_they_take(fake, tmp_path, monkeypatch, clock):
    """kxxx's condition a), the hard caps, with their real values: 32 observations a session (the window has no count
    cap of its own; 32 is above the most first reads pxx's ledger shows in 11 minutes, 20) and 256 in the file. A
    session's 33rd read takes its oldest and marks it; seven more sessions fill the file to 256, and one read past it
    takes the oldest observation in the file, S1's, and marks S1 again. Nothing else is marked.
    MUTANTS C1 (no cap) and A2 (a bound takes evidence unmarked)."""
    assert (node.ACTIVITY_SESSION_MAX, node.ACTIVITY_MAX) == (32, 256)
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    times = []
    for i in range(33):
        times.append(clock(i / 100))
        read(state, f"msg/{i}-s1")
    body = activity(state)
    assert len(body["activity"]) == 32 and body["activity"][0]["key"] == "msg/1-s1"
    assert body["dropped"] == {S1: times[0].isoformat()}
    n = 33
    for k in range(7):
        monkeypatch.setenv(node.SESSION_ENV, f"6f1e0c2a-{k:04d}-4c3b-9a5d-00000000000{k}")
        for j in range(32):
            clock(n / 100)
            read(state, f"msg/{n}-x{k}")
            n += 1
    assert len(activity(state)["activity"]) == 256 and activity(state)["dropped"] == {S1: times[0].isoformat()}
    monkeypatch.setenv(node.SESSION_ENV, "6f1e0c2a-ffff-4c3b-9a5d-0000000000ff")
    clock(n / 100)
    read(state, f"msg/{n}-y")
    body = activity(state)
    assert len(body["activity"]) == 256 and body["activity"][0]["key"] == "msg/2-s1"
    assert body["dropped"] == {S1: times[1].isoformat()}


def test_the_activity_file_has_the_state_files_permissions(fake, tmp_path, monkeypatch):
    """kxxx's condition c): the file gets the permissions of the state file beside it, which the client writes
    owner-only (`_write_private`: 0600 where the platform honours a mode). Compared with a state file the client
    itself wrote, and 0600 on POSIX, also when the file was left wider by a hand. On Windows os.chmod sets only the
    read-only flag, and both files take their folder's ACL. MUTANT F1."""
    state = tmp_path / "seat-prod-state.json"
    node._write_private(state, {"peer_id": PEER})
    monkeypatch.setenv(node.SESSION_ENV, S1)
    path = Path(f"{state}{node.ACTIVITY_SUFFIX}")
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    read(state, "msg/1-a")
    assert activity(state)["activity"], "the read was observed"
    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
    assert mode(path) == mode(state)
    if os.name != "nt":
        assert mode(path) == 0o600


def test_the_activity_file_is_replaced_whole_and_a_damaged_one_replaced(fake, tmp_path, monkeypatch, clock):
    """A damaged or foreign file reads as empty and the next observation replaces it (kxxx's condition c: damage is
    never a failure); the file is written whole through a temp file; a lock not taken writes neither file.
    MUTANT F2 (a damaged file raises)."""
    state = tmp_path / "seat-prod-state.json"
    monkeypatch.setenv(node.SESSION_ENV, S1)
    path = Path(f"{state}{node.ACTIVITY_SUFFIX}")
    for damaged in ("{not json", "[]", json.dumps({"schema_version": 2, "peer_id": PEER, "activity": []}),
                    json.dumps({"schema_version": 1, "peer_id": "33333333-3333-3333-3333-333333333333", "activity": [
                        {"session": S1, "key": "msg/0-foreign", "at": "2026-10-10T12:00:00+00:00", "kind": "read"}]})):
        path.write_text(damaged, encoding="utf-8")
        clock(0)
        read(state, "msg/1-a")
        assert [e["key"] for e in activity(state)["activity"]] == ["msg/1-a"] and activity(state)["peer_id"] == PEER
    path.write_bytes(b"\xff\xfe\x00 not text")
    read(state, "msg/1-a")
    assert [e["key"] for e in activity(state)["activity"]] == ["msg/1-a"]
    assert not list(tmp_path.glob(".activity-*")), "no temp file left behind"

    monkeypatch.setattr(node, "_sidecar_lock", lambda *a, **k: None)
    read(state, "msg/8-a")
    assert [e["key"] for e in activity(state)["activity"]] == ["msg/1-a"] and ("msg/8-a", 1) not in recorded(state)


def test_what_a_bound_takes_is_marked_for_its_session(fake, tmp_path, monkeypatch, clock):
    """Round 3's finding 2: a session's observations past ACTIVITY_SESSION_MAX go, and only that session's mark says
    when (the newest time taken), so another session's traffic never takes its evidence; the file's own bound marks
    whichever session it takes from. The hook abstains while a mark reaches into a question's window (CONTROL: once
    the window starts after it, the one thread). A mark is pruned as an observation is, past the window and its
    margin (kept at 10.9 minutes, gone at 11.1). MUTANTS A2 and P2."""
    state = tmp_path / "seat-prod-state.json"
    S2 = "6f1e0c2a-2222-4c3b-9a5d-0000000000b2"
    monkeypatch.setattr(node, "ACTIVITY_SESSION_MAX", 2)
    monkeypatch.setenv(node.SESSION_ENV, S1)
    fake.thread = "thread-a"
    clock(0)
    read(state, "msg/1-a")
    fake.thread = THREAD
    taken = clock(1)
    read(state, "msg/2-a")
    monkeypatch.setenv(node.SESSION_ENV, S2)
    for i in range(3, 6):
        clock(i)
        read(state, f"msg/{i}-b")
    monkeypatch.setenv(node.SESSION_ENV, S1)
    clock(6)
    read(state, "msg/6-a")
    body = activity(state)
    assert [(e["session"], e["key"]) for e in body["activity"]] == [
        (S1, "msg/2-a"), (S2, "msg/4-b"), (S2, "msg/5-b"), (S1, "msg/6-a")]
    assert body["dropped"][S1] == clock(0).isoformat() and body["dropped"][S2] == clock(3).isoformat()
    asked = clock(7).isoformat()
    assert cards.activity_thread(state, STATE, asked, S1) is None, "thread-a was taken from the window: abstain"
    assert cards.activity_thread(state, STATE, clock(10.5).isoformat(), S1) == THREAD

    monkeypatch.setattr(node, "ACTIVITY_SESSION_MAX", 500)
    monkeypatch.setattr(node, "ACTIVITY_MAX", 4)
    clock(8)
    read(state, "msg/8-a")
    body = activity(state)
    assert [e["key"] for e in body["activity"]] == ["msg/4-b", "msg/5-b", "msg/6-a", "msg/8-a"]
    assert body["dropped"][S1] == taken.isoformat() and body["dropped"][S2] == clock(3).isoformat()

    monkeypatch.setattr(node, "ACTIVITY_MAX", 256)
    clock(1 + 10.9)
    read(state, "msg/9-a")
    assert activity(state)["dropped"] == {S1: taken.isoformat(), S2: clock(3).isoformat()}
    clock(1 + 11.1)
    read(state, "msg/10-a")
    assert activity(state)["dropped"] == {S2: clock(3).isoformat()}, "S1's mark is past the window and its margin"
    clock(3 + 11.1)
    read(state, "msg/11-a")
    assert activity(state)["dropped"] == {}
