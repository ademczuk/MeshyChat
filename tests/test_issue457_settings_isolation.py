"""Regression tests for issue #457: transcript-cards upload and parser paths
must not freeze process-global django.conf.settings to bare global_settings.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from django.conf import UserSettingsHolder, empty, global_settings, settings
from django.test import override_settings
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools" / "transcript_cards") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools" / "transcript_cards"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.context import validation as ctx_validation  # noqa: E402
from apps.core import mesh_unversioned  # noqa: E402
import context_parser as cp  # noqa: E402
import context_upload as cu  # noqa: E402

SEAT = "fixture-seat"
STAMP = "2026-10-10T11:00:00Z"
LATER = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


class _MemoryTransport:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.writes: list[str] = []

    def write(self, key: str, value: dict) -> dict:
        self.writes.append(key)
        if key in self.rows:
            return {"status": "failed", "result_signature_verified": True, "error_code": "E_USE_MEMORY_EDIT"}
        self.rows[key] = value
        return {"status": "completed", "result_signature_verified": True, "result": {"key": key, "version": 1}}

    def reconcile(self, outbox, batch_key: str):
        action = outbox.next_action(batch_key)
        assert action.kind == "read"
        value = self.rows.get(action.record.key)
        artifact = json.dumps({"key": action.record.key, "token": action.attempt.token}).encode()
        return outbox.observe(
            action.attempt,
            value=None if value is None else cp.serialize(value),
            evidence_sha256=hashlib.sha256(artifact).hexdigest(),
            evidence_artifact=artifact,
        )


def _write_idle_codex_rollout(path: Path) -> None:
    records = [
        {
            "type": "session_meta",
            "timestamp": STAMP,
            "payload": {"id": "sess-457", "originator": "codex-tui", "source": "cli"},
        },
        {
            "type": "response_item",
            "timestamp": STAMP,
            "payload": {
                "type": "message",
                "id": "u1",
                "role": "user",
                "content": [{"type": "input_text", "text": "check settings isolation"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": STAMP,
            "payload": {
                "type": "message",
                "id": "a1",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "settings remain untouched"}],
            },
        },
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    old = (LATER - timedelta(hours=1)).timestamp()
    os.utime(path, (old, old))


def test_context_upload_and_parser_leave_unconfigured_settings_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    monkeypatch.delenv("MESHY_MESH_ACCEPT_UNVERSIONED", raising=False)
    saved = settings._wrapped
    settings._wrapped = empty
    try:
        cu._configure_django()
        assert settings.configured is False

        src = tmp_path / "rollout.jsonl"
        _write_idle_codex_rollout(src)
        parser, _ = cp.parse_file(str(src), SEAT, "codex", namespace=cu.NAMESPACE)
        assert len(parser.items) == 2
        for item in parser.items:
            assert ctx_validation.validate_context(cu.NAMESPACE, cp.item_key(item), item, now=LATER) == "item"

        transport = _MemoryTransport()
        report = cu.run_pass(cu.Config(SEAT, "codex", tmp_path / "store", [str(src)]), transport, now=LATER)
        assert report.conflicts == []
        assert report.batches_committed == 1
        assert report.items_committed == 2

        assert settings.configured is False
        assert settings._wrapped is empty
        assert mesh_unversioned.accept_from_settings() is False

        monkeypatch.setenv("MESHY_MESH_ACCEPT_UNVERSIONED", "true")
        assert mesh_unversioned.accept_from_settings() is True
        monkeypatch.delenv("MESHY_MESH_ACCEPT_UNVERSIONED", raising=False)
        assert mesh_unversioned.accept_from_settings() is False

        # Positive control: bare settings.configure() reproduces issue #457 immediately.
        settings.configure()
        try:
            assert settings.configured is True
            with pytest.raises(AttributeError, match="MESHY_MESH_ACCEPT_UNVERSIONED"):
                mesh_unversioned.accept_from_settings()
        finally:
            settings._wrapped = empty
    finally:
        settings._wrapped = saved


def test_context_upload_main_leaves_unconfigured_settings_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    monkeypatch.delenv("MESHY_MESH_ACCEPT_UNVERSIONED", raising=False)
    transport = _MemoryTransport()
    monkeypatch.setattr(cu, "NodeTransport", lambda state_file, base_url, seat: transport)

    src = tmp_path / "rollout.jsonl"
    _write_idle_codex_rollout(src)
    saved = settings._wrapped
    settings._wrapped = empty
    try:
        rc = cu.main([
            "--state-file", str(tmp_path / "unused-state.json"),
            "--base-url", "http://127.0.0.1:9",
            "--store-dir", str(tmp_path / "store"),
            "--seat", SEAT,
            "--dialect", "codex",
            "--source", str(src),
        ])
        assert rc == cu.EXIT_OK
        assert len(transport.rows) == 3
        assert settings.configured is False
        assert mesh_unversioned.accept_from_settings() is False
    finally:
        settings._wrapped = saved


@pytest.mark.parametrize("accept", [True, False])
def test_context_upload_preserves_preconfigured_django_settings(tmp_path, accept):
    src = tmp_path / "rollout.jsonl"
    _write_idle_codex_rollout(src)
    holder = UserSettingsHolder(global_settings)
    holder.MESHY_MESH_ACCEPT_UNVERSIONED = accept

    saved = settings._wrapped
    settings._wrapped = holder
    try:
        cu._configure_django()
        transport = _MemoryTransport()
        report = cu.run_pass(cu.Config(SEAT, "codex", tmp_path / "store", [str(src)]), transport, now=LATER)
        assert report.conflicts == []
        assert report.items_committed == 2
        assert settings._wrapped is holder
        assert mesh_unversioned.accept_from_settings() is accept

        with override_settings(MESHY_MESH_ACCEPT_UNVERSIONED=not accept):
            assert mesh_unversioned.accept_from_settings() is (not accept)
        assert mesh_unversioned.accept_from_settings() is accept
    finally:
        settings._wrapped = saved


def test_prior_context_modules_did_not_leak_bare_global_settings():
    """When run after test_transcript_cards_context_parser and test_context_upload
    without DJANGO_SETTINGS_MODULE, accept_from_settings() must still resolve cleanly.
    """
    if not settings.configured and not os.environ.get("DJANGO_SETTINGS_MODULE"):
        assert mesh_unversioned.accept_from_settings() is False
    else:
        assert isinstance(mesh_unversioned.accept_from_settings(), bool)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
