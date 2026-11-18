"""Summaries are OFF until a measurement passes, and the switch is a digested record.

Divergence 037; docs/design/thread-summaries.md 7.8 and Q7.

A flag can be switched on without a measurement; a record ties enablement to the exact
(`summary_version`, `model_pin`) that passed. The measurement harness
(tools/summaries_eval.py) writes the record: aggregates and per-summary ids only, no
message text, and a `content_sha256` over the whole record without that field, as 032
added after a calibration whose `enabled` flag sat outside every digest
(docs/DIVERGENCES.md 032). The server reads it, and serves summaries only when it:

  * exists and parses (`no_record`, `unreadable`),
  * verifies against its own digest (`not_verified`: an edit after digesting),
  * `passed` (`failed`: a measurement that failed a floor does not switch anything on),
  * is for THIS `summary_version` (`other_version`: a changed prompt, validator or budget
    is a new version, measured on a new sample).

Anything else answers 503 E_SUMMARY_DISABLED before the ledger, the tokenizer or the model
is touched. The record's `model_pin` is then held against what the provider reports
(apps/meshsum/pin.py): a mismatch is 503 E_SUMMARY_MODEL_CHANGED. Its `provider_id` names
the ONE chat provider the summary calls (providers.chat(provider_ids=...)).

WHERE. `apps/meshsum/measure/record.json`, committed by the measurement that passed, or the
path in MESHY_SUMMARY_RECORD_PATH. The repository ships NO record: built is not shipped
(design 8). Read at most once per file change (path and mtime), so a record committed and
deployed is read at the next request without a restart, and an unchanged one is not
re-parsed per request.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from pathlib import Path

from apps.core import env

from . import prompt as P

SCHEMA = "meshsum-record/1"
DEFAULT_PATH = Path(__file__).resolve().parent / "measure" / "record.json"


@dataclasses.dataclass(frozen=True)
class Record:
    summary_version: str
    model_pin: str
    provider_id: str
    content_sha256: str
    path: str


def content_sha256(body: dict) -> str:
    """sha256 over the record's canonical JSON, without `content_sha256` itself."""
    rest = {k: v for k, v in body.items() if k != "content_sha256"}
    raw = json.dumps(rest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _path() -> Path:
    raw = env.setting("MESHY_SUMMARY_RECORD_PATH", "MESHY_SUMMARY_RECORD_PATH")
    return Path(raw) if raw else DEFAULT_PATH


_memo: dict = {}
_lock = threading.Lock()


def _load(path: Path) -> tuple[Record | None, str | None]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "no_record"
    except (OSError, ValueError):
        return None, "unreadable"
    if not isinstance(body, dict) or body.get("schema") != SCHEMA:
        return None, "unreadable"
    if body.get("content_sha256") != content_sha256(body):
        return None, "not_verified"
    if body.get("passed") is not True:
        return None, "failed"
    for field in ("summary_version", "model_pin", "provider_id"):
        if not isinstance(body.get(field), str) or not body[field]:
            return None, "unreadable"
    return Record(summary_version=body["summary_version"], model_pin=body["model_pin"],
                  provider_id=body["provider_id"], content_sha256=body["content_sha256"],
                  path=str(path)), None


def current() -> tuple[Record | None, str | None]:
    """(the record that enables THIS summary_version, None) or (None, why not)."""
    path = _path()
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        stamp = (str(path), None)
    with _lock:
        hit = _memo.get("stamp") == stamp and _memo.get("value")
    if hit:
        rec, why = hit
    else:
        rec, why = _load(path)
        with _lock:
            _memo["stamp"], _memo["value"] = stamp, (rec, why)
    if rec is not None and rec.summary_version != P.SUMMARY_VERSION:
        return None, "other_version"
    return rec, why


def forget() -> None:
    with _lock:
        _memo.clear()


__all__ = ["DEFAULT_PATH", "Record", "SCHEMA", "content_sha256", "current", "forget"]
