"""The postbox blanks a redacted (or edited) message's spooled preview IN PLACE (divergence 053;
docs/design/redaction.md test 17, docs/design/edit.md test 16).

The spool is read by BYTE OFFSET: each consumer (the channel, the codex queue, wait_next, the legacy
tail) resumes at a position it saved, which must stay the start of a line. So the blank must keep every
byte count, keep every line valid JSON, touch only `preview_unverified`, and only for the key named.
Each property has a test, and the length check has a mutant below it (a shortening blank must fail).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_postbox_reconcile import POSTBOX, ident, pb  # noqa: E402,F401

TARGET = "msg/1790700000000-alice-aa11"
OTHER = "msg/1790700000001-bob-bb22"
#: Non-ASCII, a JSON escape, a quote and a backslash, so the blank is measured in BYTES of the line as
#: written (json.dumps escapes the non-ASCII), not in characters of the decoded preview.
PREVIEW = 'client name "Zürich" \\ path, café — zq-preview-marker'


def _msg(key, preview, **kw):
    return {"event": "message", "type": "mesh_message", "key": key, "role": "to", "from": "alice",
            "from_peer_id": "11111111-1111-1111-1111-111111111111", "to": ["bxxxxx"], "cc": [],
            "created_at": "2026-09-30T01:00:00Z", "preview_unverified": preview,
            "preview_truncated": False, "via": "live", "rx_at": "2026-09-30T01:00:01.000Z", **kw}


def _write_log(path, objs, crlf_at=()):
    """The log as the postbox writes it: one json.dumps per line. Lines whose index is in `crlf_at` end
    in CRLF, as a Windows child's print does when the listener line is written verbatim."""
    with open(path, "wb") as f:
        for i, o in enumerate(objs):
            f.write((json.dumps(o) + ("\r\n" if i in crlf_at else "\n")).encode("utf-8"))


def _line_starts(data: bytes) -> list[int]:
    starts, at = [0], 0
    for line in data.split(b"\n")[:-1]:
        at += len(line) + 1
        starts.append(at)
    return starts


def _lines(data: bytes) -> list[dict]:
    return [json.loads(l) for l in data.split(b"\n") if l.strip()]


def _spool(tmp_path):
    objs = [
        {"event": "supervisor", "state": "started", "by": "postbox", "at": "2026-09-30T00:59:00Z"},
        _msg(TARGET, PREVIEW),
        _msg(OTHER, "an unrelated message zq-other-marker"),
        _msg(TARGET, PREVIEW, via="catch_up", recovered=True),   # the same key twice (a takeover)
        {"event": "status", "state": "caught_up", "at": "2026-09-30T01:00:02Z"},
    ]
    path = tmp_path / "seat.log"
    _write_log(path, objs, crlf_at=(3,))
    return path, objs


def test_the_redacted_previews_are_blanked_and_every_byte_offset_is_kept(pb, tmp_path):
    path, objs = _spool(tmp_path)
    before = path.read_bytes()
    assert b"zq-preview-marker" in before, "control: the preview is on disk before the blank"
    assert pb.blank_preview(str(path), TARGET) == 2
    after = path.read_bytes()
    assert len(after) == len(before)
    assert _line_starts(after) == _line_starts(before), "a saved consumer offset would no longer be a line start"
    assert b"zq-preview-marker" not in after and "Z\\u00fcrich".encode() not in after
    got = _lines(after)
    for old, new in zip(objs, got):
        if old.get("key") == TARGET:
            assert new["preview_unverified"].strip() == "" and new["preview_unverified"]
            assert {k: v for k, v in new.items() if k != "preview_unverified"} == \
                   {k: v for k, v in old.items() if k != "preview_unverified"}
        else:
            assert new == old, "a line of another key, or a non-message line, changed"


def test_a_consumer_resuming_at_a_saved_offset_reads_the_same_lines(pb, tmp_path):
    path, _ = _spool(tmp_path)
    before = path.read_bytes()
    saved = _line_starts(before)[2]                # a consumer that has read two lines
    pb.blank_preview(str(path), TARGET)
    after = path.read_bytes()
    tail_before, tail_after = _lines(before[saved:]), _lines(after[saved:])
    assert [(d.get("event"), d.get("key")) for d in tail_after] == \
           [(d.get("event"), d.get("key")) for d in tail_before]


def test_a_key_the_log_does_not_hold_is_a_no_op(pb, tmp_path):
    path, _ = _spool(tmp_path)
    before = path.read_bytes()
    assert pb.blank_preview(str(path), "msg/1790700000009-nobody-cc33") == 0
    assert path.read_bytes() == before
    assert pb.blank_preview(str(tmp_path / "absent.log"), TARGET) == 0


def test_blanking_twice_is_the_same_as_once(pb, tmp_path):
    path, _ = _spool(tmp_path)
    pb.blank_preview(str(path), TARGET)
    once = path.read_bytes()
    pb.blank_preview(str(path), TARGET)
    assert path.read_bytes() == once


def test_a_preview_field_named_inside_another_value_is_not_what_gets_blanked(pb, tmp_path):
    """The locator looks for the FIELD `"preview_unverified": "`. A string value that quotes that text is
    escaped by json.dumps (`\\"preview_unverified\\": `), so it cannot match; the real field is the one
    blanked and the decoy survives."""
    decoy = 'says "preview_unverified": "not me"'
    path = tmp_path / "seat.log"
    _write_log(path, [_msg(TARGET, PREVIEW, **{"from": decoy})])
    assert pb.blank_preview(str(path), TARGET) == 1
    (line,) = _lines(path.read_bytes())
    assert line["from"] == decoy and line["preview_unverified"].strip() == ""


@pytest.mark.parametrize("event", ["redaction", "edit"])
def test_the_listener_line_is_written_then_the_earlier_previews_are_blanked(pb, tmp_path, event):
    path, _ = _spool(tmp_path)
    idt = ident(tmp_path)
    line = {"event": event, "key": TARGET, "version": 1, "redacted_at": "2026-09-30T01:05:00Z",
            "reason_code": "client_name", "via": "live"}
    with open(path, "ab") as out:
        got = pb.record_listener_line(idt, out, (json.dumps(line) + "\r\n").encode())
    assert got == line
    data = path.read_bytes()
    assert b"zq-preview-marker" not in data
    assert _lines(data)[-1] == line, "the event line itself is on disk, unchanged"
    status = json.loads(Path(pb.STATUS).read_text())[idt["name"]]
    assert status["last_blank"] == TARGET and status["last_blank_lines"] == 2


def test_a_message_line_blanks_nothing_and_is_still_written_once(pb, tmp_path):
    path, _ = _spool(tmp_path)
    idt = ident(tmp_path)
    before = path.read_bytes()
    new = _msg("msg/1790700000005-carol-dd44", "fresh text")
    with open(path, "ab") as out:
        assert pb.record_listener_line(idt, out, json.dumps(new).encode() + b"\n")["key"] == new["key"]
        assert pb.record_listener_line(idt, out, json.dumps(new).encode() + b"\n") is None   # a replay
    data = path.read_bytes()
    assert data.startswith(before) and b"zq-preview-marker" in data, "a message line must not blank anything"
    assert data.count(b"fresh text") == 1


def test_a_redaction_line_does_not_wake_the_seat(pb):
    """A redaction carries nothing to act on; waking for it is edit's (docs/design/edit.md test 16, with
    the edit build)."""
    assert not pb.wanted({"event": "redaction", "key": TARGET, "role": "to"})


def test_a_locator_that_misses_the_value_leaves_the_line_and_notes_it(pb, tmp_path, monkeypatch):
    """The check after the patch refuses any line it would not leave intact (same length, same key, a
    preview of spaces only). Here the locator is made to point at the opening quote, so the patch would
    change nothing: the line is left as it is and the note says so, rather than a wrong write."""
    path, _ = _spool(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(pb, "_PREVIEW_FIELD", '"preview_unverified": ')
    assert pb.blank_preview(str(path), TARGET) == 0
    assert path.read_bytes() == before
    assert "left as is" in Path(pb.ERR).read_text()


REDACTION = {"event": "redaction", "key": TARGET, "version": 1, "redacted_at": "2026-09-30T01:05:00Z",
             "reason_code": "client_name", "via": "live"}
#: The edit line as the edit build emits it (kxxx, review of #33): role is to, cc, broadcast or replied.
EDIT = {"event": "edit", "key": TARGET, "version": 2, "edited_at": "2026-09-30T01:06:00Z",
        "edited_by": "alice-2", "role": "to", "via": "live"}


@pytest.mark.parametrize("error", [OSError("disk trouble"), ValueError("bad bytes")],
                         ids=["OSError", "ValueError"])
def test_a_failed_blank_is_noted_and_recorded_and_does_not_leave_the_read_loop(pb, tmp_path, monkeypatch, error):
    """kxxx's probe 1, turned round: the blank raising must not propagate out of record_listener_line,
    which runs inside run_identity's read loop with a listener still attached. Two exception types, so a
    handler narrowed to one of them fails (kxxx's surviving mutant N1)."""
    path, _ = _spool(tmp_path)
    idt = ident(tmp_path)

    def broken(log, key):
        raise error
    monkeypatch.setattr(pb, "blank_preview", broken)
    with open(path, "ab") as out:
        assert pb.record_listener_line(idt, out, (json.dumps(REDACTION) + "\n").encode()) == REDACTION
    data = path.read_bytes()
    assert json.loads(data.rstrip(b"\n").split(b"\n")[-1]) == REDACTION, "the event line is on disk"
    assert b"zq-preview-marker" in data, "control: the blank really did not happen"
    status = json.loads(Path(pb.STATUS).read_text())[idt["name"]]
    assert status["last_blank_error"] == "%s: %s" % (type(error).__name__, error)
    assert status["last_blank_error_key"] == TARGET
    assert "redone at the next listener start" in Path(pb.ERR).read_text()


@pytest.mark.parametrize("event", [REDACTION, EDIT], ids=["redaction", "edit"])
def test_reblank_redoes_the_blanks_the_logs_own_events_call_for(pb, tmp_path, event):
    """kxxx's probe 2, turned round: an event line whose blank never happened (a failure, or a crash
    between the write and the blank) is repaired by reblank_log, and only its key is blanked. Both
    events, so a reblank collecting redactions only fails (kxxx's surviving mutant N4)."""
    path, objs = _spool(tmp_path)
    with open(path, "ab") as f:                         # the event is on disk; its blank never ran
        f.write((json.dumps(event) + "\n").encode())
    before = path.read_bytes()
    idt = ident(tmp_path)
    assert pb.reblank_log(idt) == 2
    after = path.read_bytes()
    assert len(after) == len(before) and _line_starts(after) == _line_starts(before)
    assert b"zq-preview-marker" not in after and b"zq-other-marker" in after
    status = json.loads(Path(pb.STATUS).read_text())[idt["name"]]
    assert status["reblanked_keys"] == 1 and status["reblanked_lines"] == 2
    assert pb.reblank_log(idt) == 2 and path.read_bytes() == after, "idempotent"


def test_reblank_with_no_event_changes_nothing(pb, tmp_path):
    path, _ = _spool(tmp_path)
    before = path.read_bytes()
    assert pb.reblank_log(ident(tmp_path)) == 0
    assert path.read_bytes() == before
    assert pb.reblank_log(ident(tmp_path, log=str(tmp_path / "absent.log"))) == 0


def test_run_identity_reblanks_before_reconcile_at_every_listener_start(pb, tmp_path, monkeypatch):
    path, _ = _spool(tmp_path)
    with open(path, "ab") as f:
        f.write((json.dumps(REDACTION) + "\n").encode())
    seen = {}

    class Stop(Exception):
        pass

    def fake_reconcile(ident_, base_url):
        seen["blank_before_reconcile"] = b"zq-preview-marker" not in path.read_bytes()
        raise Stop()                                    # end the loop here: the listener is not the subject
    monkeypatch.setattr(pb, "listeners_on", lambda state: [])
    monkeypatch.setattr(pb, "reconcile", fake_reconcile)
    monkeypatch.setattr(pb.traceback, "format_exc", lambda: "")
    monkeypatch.setattr(pb.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        pb.run_identity(ident(tmp_path), "http://x")
    assert seen == {"blank_before_reconcile": True}


@pytest.mark.parametrize("role,wakes", [("to", True), ("cc", True), ("replied", True), ("broadcast", False)])
def test_an_edit_wakes_the_seats_it_was_for_and_not_a_broadcast_reader(pb, role, wakes):
    assert bool(pb.wanted(dict(EDIT, role=role))) is wakes


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_under_test", Path(POSTBOX).parent / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(POSTBOX).parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def test_the_three_wake_rules_agree(pb):
    """wanted() is read by three consumers (the daemon for codex, the channel, wait_next). They must agree
    line for line, or one kind of seat is woken for what another is not. Kept as the behavioural check even
    now that the three names are one function (the test below): a script that grew its own copy again would
    still have to agree here."""
    others = [_load("meshy_channel"), _load("wait_next")]
    lines = [dict(EDIT, role=r) for r in ("to", "cc", "replied", "broadcast", None)] + [
        REDACTION, _msg(TARGET, "x"), _msg(TARGET, "x", role="broadcast"),
        {"event": "status", "state": "error"}, {"event": "status", "state": "caught_up"},
        {"event": "supervisor", "state": "listener_exited"}, {"event": "supervisor", "state": "started"}]
    for d in lines:
        want = bool(pb.wanted(d))
        assert [bool(m.wanted(d)) for m in others] == [want, want], d
    assert pb.EDIT_WAKE_ROLES == others[0].EDIT_WAKE_ROLES == others[1].EDIT_WAKE_ROLES


def test_the_three_consumers_share_one_wake_rule(pb):
    """One function, so the daemon, the channel and wait_next cannot drift: each script's module-level `wanted`
    and `EDIT_WAKE_ROLES` IS postbox_os's (the shape tests/test_sender_alias.py holds sender_text to). Three
    byte-identical copies pass the test above and fail this one; the names stay, so pb.wanted, the channel's
    and wait_next's callers, and the docstring in tools/meshy_node/node.py that cites `wait_next.py wanted`,
    all still resolve."""
    import postbox_os
    channel, wn = _load("meshy_channel"), _load("wait_next")
    assert pb.wanted is postbox_os.wanted is channel.wanted is wn.wanted
    assert pb.EDIT_WAKE_ROLES is postbox_os.EDIT_WAKE_ROLES is channel.EDIT_WAKE_ROLES is wn.EDIT_WAKE_ROLES


def test_the_codex_notice_for_an_edit_names_the_version_and_the_read_and_no_text(pb, tmp_path):
    notice = pb.codex_notice(ident(tmp_path), EDIT)
    assert "EDITED" in notice and TARGET in notice and "version=2" in notice and "edited_by=alice-2" in notice
    assert "listener reported" not in notice, "an edit must not read as a listener problem"
    assert "read" in notice and "preview" not in notice


def test_run_poll_reblanks_on_each_tick_before_reconcile(pb, tmp_path, monkeypatch):
    path, _ = _spool(tmp_path)
    with open(path, "ab") as f:
        f.write((json.dumps(EDIT) + "\n").encode())
    seen = []

    class Stop(Exception):
        pass

    def fake_sleep(s):
        if seen:
            raise Stop
    monkeypatch.setattr(pb.time, "sleep", fake_sleep)
    monkeypatch.setattr(pb, "reconcile", lambda i, u: seen.append(b"zq-preview-marker" not in path.read_bytes()))
    with pytest.raises(Stop):
        pb.run_poll(ident(tmp_path))
    assert seen == [True], "the first tick's reconcile ran before the blank was redone"


def test_a_reblank_failure_on_the_poll_timer_is_noted_and_the_reconcile_still_runs(pb, tmp_path, monkeypatch):
    """The poll's reblank is wrapped on its own: a failure of any type is noted and the tick's reconcile,
    which is the liveness signal, still runs (kxxx's surviving mutant: the except narrowed to OSError)."""
    seen = []

    class Stop(Exception):
        pass

    def fake_sleep(s):
        if seen:
            raise Stop

    def broken(ident_):
        raise ValueError("bad bytes")
    monkeypatch.setattr(pb.time, "sleep", fake_sleep)
    monkeypatch.setattr(pb, "reblank_log", broken)
    monkeypatch.setattr(pb, "reconcile", lambda i, u: seen.append("reconcile"))
    with pytest.raises(Stop):
        pb.run_poll(ident(tmp_path))
    assert seen == ["reconcile"]
    assert "reblank on the poll timer failed" in Path(pb.ERR).read_text()
