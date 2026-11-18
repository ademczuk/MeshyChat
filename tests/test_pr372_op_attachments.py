"""PR #372: an op binds its attachments by name and content, and a retry never uploads them again (pxx 9136f7)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from tools.meshy_node import cli, files, node, sendguard


def _state():
    return {"peer_id": "00000000-0000-0000-0000-000000000001", "label": "uxxx", "status": "minted",
            "kid": "test-kid", "signing_seed_b64url": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "memory_peer_id": "29d6a9f7-ed27-5153-a9f2-f6feb3175e77"}


def _ok(key):
    return {"key": key, "status": "completed", "result": {"ok": True, "key": key, "version": 1},
            "result_signature_verified": True}


def _entry(path, op_id):
    return json.loads((Path(f"{path}.ops") / f"{op_id}.json").read_text(encoding="utf-8"))


def _matching_read(entry):
    return {"key": entry["key"], "version": 1, "_value": entry["value"],
            "signature_verified": True, "status_verified": True}


def _send(path, op_id, attach, *extra, body="file attached"):
    return ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(path), "--to", "heron",
            "--thread", "ops/pr372", "--type", "note", "--body", body, "--op-id", op_id,
            "--attach", str(attach), "--no-recipient-check", "--no-card-check", *extra]


def _fake_uploads(monkeypatch):
    uploads = []

    def upload(_state_file, _state, path, *_a, **_k):
        data = Path(path).read_bytes()
        ref = {"file_id": f"00000000-0000-0000-0000-{len(uploads) + 1:012d}", "name": Path(path).name,
               "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        uploads.append(ref)
        return ref

    monkeypatch.setattr(files, "upload", upload)
    return uploads


def _common(monkeypatch):
    state = _state()
    monkeypatch.setattr(node, "load_state", lambda *a, **k: state)
    monkeypatch.setattr(node, "note_sent", lambda *a, **k: True)
    return state


def test_a_renamed_byte_identical_attachment_is_refused_on_a_retried_op(tmp_path, monkeypatch):
    _common(monkeypatch)
    _fake_uploads(monkeypatch)
    writes = []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    path, first = tmp_path / "seat.json", tmp_path / "a.txt"
    first.write_bytes(b"same bytes")
    renamed = tmp_path / "b.txt"
    shutil.copyfile(first, renamed)

    assert cli.main(_send(path, "named", first)) == 0
    assert cli.main(_send(path, "named", renamed)) == 2
    assert len(writes) == 1


def test_an_unchanged_attachment_resolves_the_stored_message(tmp_path, monkeypatch):
    _common(monkeypatch)
    uploads = _fake_uploads(monkeypatch)
    writes = []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    path, first = tmp_path / "seat.json", tmp_path / "a.txt"
    first.write_bytes(b"same bytes")

    assert cli.main(_send(path, "named", first)) == 0
    assert cli.main(_send(path, "named", first)) == 0
    assert len(writes) == 1 and len(uploads) == 1


def test_a_dropped_reply_retry_uploads_nothing_and_resolves_to_the_stored_message(tmp_path, monkeypatch):
    _common(monkeypatch)
    uploads = _fake_uploads(monkeypatch)
    writes = []

    def lost_reply(*_a, **k):
        writes.append(k["args"]["value"])
        raise TimeoutError("stored, but the reply was dropped")

    monkeypatch.setattr(node, "invoke", lost_reply)
    path, first = tmp_path / "seat.json", tmp_path / "a.txt"
    first.write_bytes(b"report")
    assert cli.main(_send(path, "dropped", first)) == 2
    assert len(uploads) == 1
    monkeypatch.setattr(node, "read", lambda *_a, **_k: _matching_read(_entry(path, "dropped")))

    assert cli.main(_send(path, "dropped", first)) == 0
    assert len(uploads) == 1 and len(writes) == 1
    entry = _entry(path, "dropped")
    assert entry["state"] == "succeeded"
    assert entry["value"]["attachments"] == [uploads[0]]


def test_a_held_op_retry_uploads_once_more(tmp_path, monkeypatch):
    """Control: a held op never left the client, so its retry uploads its files."""
    _common(monkeypatch)
    uploads = _fake_uploads(monkeypatch)
    writes = []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    path, first = tmp_path / "seat.json", tmp_path / "a.txt"
    first.write_bytes(b"report")
    for number in range(sendguard.MAX_PER_RECIPIENT):
        sendguard.record(path, to=["heron"], cc=None, key=f"msg/{number}", body_bytes=4096, body="x" * 4096)
    large = "y" * sendguard.COUNTED_MIN_BYTES

    assert cli.main(_send(path, "held", first, body=large)) != 0
    assert _entry(path, "held")["state"] == node.OP_HELD
    uploaded_before = len(uploads)

    assert cli.main(_send(path, "held", first, "--bulk-ack", "a test of the held path", body=large)) == 0
    assert len(uploads) == uploaded_before + 1
    assert len(writes) == 1


def test_a_new_op_refused_by_the_guard_uploads_nothing(tmp_path, monkeypatch):
    """The guard runs before the upload on a new or held op, not only inside op_send (GitHub Codex r4366756038)."""
    _common(monkeypatch)
    uploads = _fake_uploads(monkeypatch)
    writes = []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    path, attach = tmp_path / "seat.json", tmp_path / "a.txt"
    attach.write_bytes(b"attached bytes")
    for number in range(sendguard.MAX_PER_RECIPIENT):
        sendguard.record(path, to=["heron"], cc=None, key=f"msg/{number}", body_bytes=4096, body="x" * 4096)
    large = "y" * 4096

    assert cli.main(_send(path, "guarded", attach, body=large)) == 2
    assert uploads == [] and writes == []
    assert _entry(path, "guarded")["state"] == node.OP_HELD

    # The held op is retried with the override: now it uploads once, and the message cites that upload.
    assert cli.main(_send(path, "guarded", attach, "--bulk-ack", "the peer asked for it", body=large)) == 0
    assert len(uploads) == 1 and len(writes) == 1
    assert _entry(path, "guarded")["value"]["attachments"] == [uploads[0]]


def test_the_plain_path_still_checks_the_guard_before_uploading(tmp_path, monkeypatch):
    _common(monkeypatch)
    uploads = _fake_uploads(monkeypatch)
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: _ok(k["args"]["key"]))
    path, attach = tmp_path / "seat.json", tmp_path / "a.txt"
    attach.write_bytes(b"attached bytes")
    for number in range(sendguard.MAX_PER_RECIPIENT):
        sendguard.record(path, to=["heron"], cc=None, key=f"msg/{number}", body_bytes=4096, body="x" * 4096)
    argv = [a for a in _send(path, "unused", attach, body="y" * 4096)]
    i = argv.index("--op-id")
    del argv[i:i + 2]

    assert cli.main(argv) == 2
    assert uploads == []


def test_two_interleaved_sends_of_one_op_upload_once(tmp_path, monkeypatch):
    """A second send of the same op id that starts while the first is uploading never uploads its own copy: it waits
    for the op lock or is refused as busy, and a later retry reuses the one upload (GitHub Codex r4366812520)."""
    _common(monkeypatch)
    writes, uploads, nested = [], [], []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    monkeypatch.setattr(node, "OPS_LOCK_WAIT", 0.2)
    path, attach = tmp_path / "seat.json", tmp_path / "a.txt"
    attach.write_bytes(b"attached bytes")

    def upload(_state_file, _state, upath, *_a, **_k):
        if not nested:                                     # the timeout-then-retry shape: same op, mid-upload
            nested.append(cli.main(_send(path, "racing", attach)))
        data = Path(upath).read_bytes()
        ref = {"file_id": f"00000000-0000-0000-0000-{len(uploads) + 1:012d}", "name": Path(upath).name,
               "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        uploads.append(ref)
        return ref

    monkeypatch.setattr(files, "upload", upload)
    assert cli.main(_send(path, "racing", attach)) == 0
    assert nested == [2]                                   # busy, retry it with the same --op-id
    assert len(uploads) == 1 and len(writes) == 1

    assert cli.main(_send(path, "racing", attach)) == 0    # the retry settles on the first message
    assert len(uploads) == 1 and len(writes) == 1
    assert _entry(path, "racing")["value"]["attachments"] == [uploads[0]]


def _upload_recorder(monkeypatch, uploads, before=None):
    def upload(_state_file, _state, upath, *_a, **_k):
        if before is not None:
            before()
        data = Path(upath).read_bytes()
        ref = {"file_id": f"00000000-0000-0000-0000-{len(uploads) + 1:012d}", "name": Path(upath).name,
               "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        uploads.append(ref)
        return ref

    monkeypatch.setattr(files, "upload", upload)


def test_another_send_cannot_bind_the_op_between_preflight_and_upload(tmp_path, monkeypatch):
    """The op lock covers preflight too, so a second send of the same id with other files cannot bind it while the
    first is between its preflight and its upload, and the first never settles on the second one's files (pxx 8b1dbd #1)."""
    _common(monkeypatch)
    writes, uploads, nested = [], [], []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    monkeypatch.setattr(node, "OPS_LOCK_WAIT", 0.2)
    _upload_recorder(monkeypatch, uploads)
    path = tmp_path / "seat.json"
    mine, theirs = tmp_path / "mine" / "a.txt", tmp_path / "theirs" / "a.txt"
    mine.parent.mkdir(); theirs.parent.mkdir()
    mine.write_bytes(b"my bytes")
    theirs.write_bytes(b"other bytes")
    real = node.op_preflight

    def preflight(*a, **k):
        result = real(*a, **k)
        if not nested:
            nested.append(cli.main(_send(path, "bound", theirs)))
        return result

    monkeypatch.setattr(node, "op_preflight", preflight)
    assert cli.main(_send(path, "bound", mine)) == 0
    assert nested == [2]                                   # busy: it waited for the lock, uploaded nothing
    assert len(writes) == 1 and len(uploads) == 1
    assert _entry(path, "bound")["value"]["attachments"][0]["sha256"] == hashlib.sha256(b"my bytes").hexdigest()


def _hold_after_upload(monkeypatch):
    """The pre-upload guard check passes, then op_send's own admission refuses: the op is held with its upload."""
    calls = []

    def admit(state_file, value, bulk_ack=None):
        calls.append(1)
        if len(calls) == 2:
            raise node.Refused("refused before sending: the window filled meanwhile")

    monkeypatch.setattr(cli, "_admit_op_sendguard", admit)
    return calls


def test_a_held_op_reuses_its_own_upload_for_the_same_files(tmp_path, monkeypatch):
    _common(monkeypatch)
    writes, uploads = [], []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    _upload_recorder(monkeypatch, uploads)
    path, attach = tmp_path / "seat.json", tmp_path / "a.txt"
    attach.write_bytes(b"attached bytes")
    _hold_after_upload(monkeypatch)

    assert cli.main(_send(path, "held", attach)) == 2
    assert len(uploads) == 1 and writes == []
    assert _entry(path, "held")["state"] == node.OP_HELD

    monkeypatch.setattr(cli, "_admit_op_sendguard", lambda *a, **k: None)
    assert cli.main(_send(path, "held", attach)) == 0
    assert len(uploads) == 1 and len(writes) == 1          # not uploaded twice (pxx 8b1dbd #2)
    assert _entry(path, "held")["value"]["attachments"] == [uploads[0]]


def test_a_held_op_rebound_with_other_files_names_its_old_upload(tmp_path, monkeypatch, capsys):
    _common(monkeypatch)
    writes, uploads = [], []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    _upload_recorder(monkeypatch, uploads)
    path, first, second = tmp_path / "seat.json", tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_bytes(b"first bytes")
    second.write_bytes(b"second bytes")
    _hold_after_upload(monkeypatch)
    assert cli.main(_send(path, "rebound", first)) == 2
    capsys.readouterr()

    monkeypatch.setattr(cli, "_admit_op_sendguard", lambda *a, **k: None)
    assert cli.main(_send(path, "rebound", second)) == 0
    warnings = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    uncited = [w for w in warnings if w.get("warning") == "held_op_attachments_uncited"]
    assert uncited and uncited[0]["file_ids"] == [uploads[0]["file_id"]]
    assert _entry(path, "rebound")["value"]["attachments"] == [uploads[1]]


def test_a_refused_new_op_is_held_in_one_write(tmp_path, monkeypatch):
    """No journal write ever leaves a never-presented op as refused-by-guard, so a crash between two writes cannot
    strand the id as 'different arguments' (pxx 8b1dbd #3)."""
    state = _common(monkeypatch)
    written = []
    real = node._op_write
    monkeypatch.setattr(node, "_op_write", lambda p, entry: written.append(entry.get("state")) or real(p, entry))
    path = tmp_path / "seat.json"

    def refuse(value, bulk_ack):
        raise node.Refused("refused before sending: window full")

    for op_id in ("new", "new"):                           # a new op, then its retry while still held
        got = node.op_send(path, state, op_id=op_id, to=["heron"], thread_id="ops/pr372", body="x",
                           msg_type="note", author_family="test", admit=refuse)
        assert got["state"] == "refused-by-guard"
    assert "refused-by-guard" not in written
    assert _entry(path, "new")["state"] == node.OP_HELD


def test_the_op_lock_is_released_when_the_send_raises_after_taking_it(tmp_path, monkeypatch):
    _common(monkeypatch)
    monkeypatch.setattr(node, "OPS_LOCK_WAIT", 0.2)
    path, attach = tmp_path / "seat.json", tmp_path / "a.txt"
    attach.write_bytes(b"attached bytes")
    monkeypatch.setattr(node, "op_journalled_attachments", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    cli.main(_send(path, "raised", attach))
    held = node._op_lock(path, "raised")
    assert held is not None                                # nothing kept it (pxx 8b1dbd INFO a)
    node._sidecar_unlock(held)


def test_a_held_op_retried_without_attach_names_its_old_upload(tmp_path, monkeypatch, capsys):
    _common(monkeypatch)
    writes, uploads = [], []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    _upload_recorder(monkeypatch, uploads)
    path, attach = tmp_path / "seat.json", tmp_path / "a.txt"
    attach.write_bytes(b"attached bytes")
    _hold_after_upload(monkeypatch)
    assert cli.main(_send(path, "dropped", attach)) == 2
    capsys.readouterr()

    monkeypatch.setattr(cli, "_admit_op_sendguard", lambda *a, **k: None)
    argv = _send(path, "dropped", attach)
    i = argv.index("--attach")
    del argv[i:i + 2]
    assert cli.main(argv) == 0
    warnings = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    uncited = [w for w in warnings if w.get("warning") == "held_op_attachments_uncited"]
    assert uncited and uncited[0]["file_ids"] == [uploads[0]["file_id"]]
    assert len(uploads) == 1 and len(writes) == 1
    assert not _entry(path, "dropped")["value"].get("attachments")


def test_a_never_presented_op_is_never_journalled_as_pending(tmp_path, monkeypatch):
    """A refused new op is written once, as OP_HELD; an admitted one is first written as presenting. No write leaves a
    never-presented op as pending, so a crash cannot strand it (pxx 4a19a7 #2)."""
    state = _common(monkeypatch)
    writes, written = [], []
    monkeypatch.setattr(node, "invoke", lambda *_a, **k: writes.append(k) or _ok(k["args"]["key"]))
    real = node._op_write
    monkeypatch.setattr(node, "_op_write", lambda p, entry: written.append(entry.get("state")) or real(p, entry))
    path = tmp_path / "seat.json"

    def refuse(value, bulk_ack):
        raise node.Refused("refused before sending: window full")

    send = dict(to=["heron"], thread_id="ops/pr372", body="x", msg_type="note", author_family="test")
    assert node.op_send(path, state, op_id="fresh", admit=refuse, **send)["state"] == "refused-by-guard"
    assert written == [node.OP_HELD]
    node.op_send(path, state, op_id="fresh", admit=lambda value, bulk_ack: None, **send)
    assert written[1] == "presenting" and "pending" not in written and len(writes) == 1


def _plain(argv):
    out = list(argv)
    i = out.index("--op-id")
    del out[i:i + 2]
    return out


def _refusal(capsys):
    return json.loads(capsys.readouterr().err.strip().splitlines()[-1])["refused"]


def test_an_op_refuses_a_bad_attachment_before_hashing_or_locking(tmp_path, monkeypatch, capsys):
    """A path _attach would refuse is refused on the op path before preflight hashes it or the op lock is taken, with
    the plain path's own message, and the lock is free afterwards (GitHub Codex r4366931417)."""
    _common(monkeypatch)
    monkeypatch.setattr(node, "OPS_LOCK_WAIT", 0.2)
    monkeypatch.setattr(cli, "_attachment_fingerprints",
                        lambda paths: (_ for _ in ()).throw(AssertionError("hashed before the local refusals")))
    path = tmp_path / "seat.json"
    folder = tmp_path / "folder"
    folder.mkdir()
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 64)
    monkeypatch.setattr(files.wire, "MAX_ATTACHMENT_SIZE_BYTES", 16)
    for bad in (folder, big):
        assert cli.main(_plain(_send(path, "badfile", bad))) == 2
        plain = _refusal(capsys)
        assert cli.main(_send(path, "badfile", bad)) == 2
        assert _refusal(capsys) == plain
        held = node._op_lock(path, "badfile")
        assert held is not None
        node._sidecar_unlock(held)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_an_op_refuses_a_fifo_at_once(tmp_path, monkeypatch, capsys):
    _common(monkeypatch)
    path, fifo = tmp_path / "seat.json", tmp_path / "pipe"
    os.mkfifo(fifo)
    assert cli.main(_send(path, "fifo", fifo)) == 2
    assert "not a regular file" in _refusal(capsys)
