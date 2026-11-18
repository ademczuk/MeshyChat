"""The order the measurement's inputs were fixed in, held by git (design 7.1).

032's calibration found its rule's constants reached git after its scores existed, so
"fixed in advance" rested on the author's account (docs/DIVERGENCES.md 032). Here:

   * every frozen input (the sample, reference items, the validation plants, the link
    grading sample) is digested into `apps/meshsum/measure/FROZEN.json`;
  * `require_frozen` refuses an input whose bytes do not match its digest, or whose digest
    is not in FROZEN.json AS COMMITTED AT HEAD (a digest written and not committed is not
    frozen);
  * `committed_at` answers WHEN a digest first reached git, so `score` can refuse reference
    items committed after the first generated summary.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FROZEN_REL = "apps/meshsum/measure/FROZEN.json"


class NotFrozen(RuntimeError):
    """An input the measurement must not use: unfrozen, uncommitted or changed."""


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _frozen_path(repo: Path) -> Path:
    return Path(repo) / FROZEN_REL


def _load(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("artifacts"), dict):
        raise NotFrozen(f"{FROZEN_REL} is not a frozen-artifacts manifest")
    return data


def record(name: str, path: Path, *, repo: Path = ROOT) -> str:
    """Write `name`'s digest into FROZEN.json (the operator then COMMITS it). Refuses to
    change a digest already recorded: a new sample is a new name (`sample_v2.json`)."""
    fp = _frozen_path(repo)
    data = _load(fp.read_text(encoding="utf-8")) if fp.exists() else {
        "schema": "meshsum-frozen/1", "artifacts": {}}
    digest = file_sha256(path)
    old = data["artifacts"].get(name)
    if old is not None and old != digest:
        raise NotFrozen(f"{name} is already frozen as {old}; a changed input is a new name")
    data["artifacts"][name] = digest
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return digest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def require_frozen(name: str, path: Path, *, repo: Path = ROOT) -> str:
    """The digest of `path`, when it is frozen as `name` in FROZEN.json at HEAD."""
    digest = file_sha256(path)
    try:
        committed = _load(_git(repo, "show", f"HEAD:{FROZEN_REL}"))
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise NotFrozen(f"{FROZEN_REL} is not committed at HEAD: {exc}") from None
    want = committed["artifacts"].get(name)
    if want is None:
        raise NotFrozen(f"{name} is not in {FROZEN_REL} as committed")
    if want != digest:
        raise NotFrozen(f"{name} does not match its committed digest ({digest} != {want})")
    return digest


def committed_at(digest: str, *, repo: Path = ROOT) -> _dt.datetime | None:
    """When `digest` first appeared in FROZEN.json in git history (the oldest commit whose
    change to the file adds or removes that string), or None."""
    out = _git(repo, "log", "--format=%cI", "-S", digest, "--", FROZEN_REL).split()
    return _dt.datetime.fromisoformat(out[-1]) if out else None


__all__ = ["FROZEN_REL", "NotFrozen", "committed_at", "file_sha256", "record",
           "require_frozen"]
