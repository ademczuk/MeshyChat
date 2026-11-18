"""Synthetic PR354 regressions; run ONLY this file with --noconftest.

PR354_TREE=l1l2 selects the existing .wt-l1l2 product code; default is base.
Imports happen before the runtime IO guard. Every data path, including symlink
targets and HOME, is beneath tmp_path. No original cursor or transcript is read.
Passing controls deliberately accompany the failing regression assertions.
"""

import builtins
import inspect
import io
import json
import os
from pathlib import Path
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
TREE = os.environ.get("PR354_TREE", "base")
assert TREE in {"base", "l1l2"}
PRODUCT_ROOT = REPO if TREE == "base" else REPO / ".wt-l1l2"
sys.path.insert(0, str(PRODUCT_ROOT / "tools" / "transcript_cards"))
import transcript_cards as tc  # noqa: E402

assert Path(tc.__file__).resolve().parent == PRODUCT_ROOT / "tools" / "transcript_cards"


@pytest.fixture(autouse=True)
def confined_io(tmp_path, monkeypatch):
    """Reject every runtime open outside tmp_path, including resolved symlinks.

    Also retain violations so a product catch-all cannot hide a guard failure.
    Framework/module collection occurs before this per-test guard is installed.
    """
    root = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("WRITE", raising=False)
    monkeypatch.chdir(root)
    violations = []
    opened_fds = set()

    def check(path):
        if isinstance(path, int):
            allowed = path in opened_fds
        else:
            resolved = Path(os.fsdecode(path)).resolve()
            allowed = resolved == root or root in resolved.parents
        if not allowed:
            violations.append(str(path))
            raise AssertionError("IO GUARD: open outside synthetic tmp_path: " + str(path))

    def wrap(original):
        def guarded(path, *args, **kwargs):
            check(path)
            assert kwargs.get("dir_fd") is None, "IO GUARD: unsupported relative dir_fd"
            result = original(path, *args, **kwargs)
            if isinstance(result, int):
                opened_fds.add(result)
            return result
        return guarded

    monkeypatch.setattr(builtins, "open", wrap(builtins.open))
    monkeypatch.setattr(io, "open", wrap(io.open))
    monkeypatch.setattr(os, "open", wrap(os.open))
    yield
    assert not violations, "IO GUARD violations: " + repr(violations)


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def claude(project, name="alpha.jsonl", sid="session-alpha", seat="txxxx"):
    prompts = ([f"echo {seat}-shell-ok"] if seat else []) + ["Please review the synthetic example."]
    return write_jsonl(project / name, [
        {"type": "user", "uuid": f"{sid}-{i}", "sessionId": sid,
         "timestamp": f"2026-10-23T08:00:0{i}.000Z",
         "message": {"role": "user", "content": text}}
        for i, text in enumerate(prompts)
    ])


def codex(project, folder="day", sid="actual-session"):
    return write_jsonl(project / folder / f"rollout-2026-10-23T08-00-00-{sid}.jsonl", [
        {"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": str(project)}},
    ])


def run(project, out, seat="txxxx", **kwargs):
    return tc.run(str(project), seat, str(out), **kwargs)


def strings(value):
    """Schema-independent check: identity may be stored in keys or values."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, val in value.items():
            yield from strings(key)
            yield from strings(val)
    elif isinstance(value, list):
        for val in value:
            yield from strings(val)


@pytest.mark.parametrize("identity", ["realpath", "session_id"])
def test_exclusion_persists_resolved_identity(tmp_path, identity):
    project, out = tmp_path / "project", tmp_path / "out"
    path = claude(project)
    report = run(project, out, exclude_files=["./alpha.jsonl"])
    assert report["files_read"] == [], "precondition: exclusion must actually match"
    policy = json.loads((out / "cursor.json").read_text())["excluded_files"]
    expected = str(path.resolve()) if identity == "realpath" else "session-alpha"
    assert expected in set(strings(policy)), f"persisted exclusion lacks {identity}: {policy!r}"


def test_symlink_retarget_cannot_rebind_persisted_exclusion(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    alpha = claude(project)
    beta = claude(project, "beta.jsonl", "session-beta")
    alias = tmp_path / "exclude-alias.jsonl"
    alias.symlink_to(alpha)
    first = run(project, out, exclude_files=[str(alias)])
    assert first["files_read"] == ["beta.jsonl"]
    alias.unlink()
    alias.symlink_to(beta)
    second = run(project, out)
    assert second["files_read"] == ["beta.jsonl"], "persisted exclusion rebound from alpha to beta"


def test_renamed_transcript_stays_excluded_by_stored_session(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    path = claude(project)
    assert run(project, out, exclude_files=[str(path)])["files_read"] == []
    path.rename(project / "renamed.jsonl")
    report = run(project, out)
    assert report["files_read"] == [], "rename escaped exclusion: session identity was not retained"


def test_session_identity_rewrite_cannot_undo_canonical_path_exclusion(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    path = claude(project)
    assert run(project, out, exclude_files=["session-alpha"])["files_read"] == []
    claude(project, path.name, "session-replacement")
    report = run(project, out)
    assert report["files_read"] == [], "matcher reread identity instead of retaining the excluded realpath"


def test_codex_pin_selector_and_run_exclusion_disagree(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    # A directory name is NOT this Codex rollout's session_meta identity.
    target = codex(project, "session_shadow-session", "actual-session")
    codex(project, "other", "shadow-session")
    specs = ["shadow-session"]
    assert not tc._matches_file_spec(str(target), specs, base_dir=str(project), session_id="actual-session")
    picked, _ = tc.select_codex_files(str(project), session_files=[str(target.resolve())], exclude_files=specs)
    assert picked == [str(target)], "precondition: shared Codex selector accepts the realpath pin"
    report = tc.run(None, "txxxx", str(out), codex_dir=str(project),
                    codex_session_files=[str(target.resolve())], exclude_files=specs)
    assert report["files_read"] == [target.name], "run's Kimi path heuristic overrode the Codex matcher"


def test_session_prefix_resolves_once_not_to_future_sessions(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    claude(project, "original.jsonl", "shared-prefix-original")
    assert run(project, out, exclude_files=["shared-prefix"])["files_read"] == []
    claude(project, "later.jsonl", "shared-prefix-later")
    report = run(project, out)
    assert report["files_read"] == ["later.jsonl"], "stored prefix expanded to a session absent at resolution time"


@pytest.mark.parametrize("dialect", ["kimi", "antigravity"])
def test_dialect_exclusion_matcher_agrees_with_shared_matcher(tmp_path, dialect):
    project = tmp_path / "project"
    if dialect == "kimi":
        path = write_jsonl(project / "session-example" / "agents" / "main" / "wire.jsonl",
                           [{"type": "metadata", "protocol_version": "1"}])
        module, pattern = tc.kimi_dialect, "wire"
    else:
        path = write_jsonl(project / "example" / ".system_generated" / "logs" / "transcript.jsonl",
                           [{"step_index": 0, "content": "synthetic"}])
        module, pattern = tc.antigravity_dialect, "transcript"
    assert tc._matches_file_spec(str(path), [pattern], base_dir=str(project), session_id="example")
    picked, _ = module.select_files(str(project), exclude_files=[pattern])
    assert picked == [], f"{dialect} selector disagrees with the shared matcher for the same file/spec"


def test_unresolvable_explicit_exclusion_is_rejected(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    project.mkdir()
    missing = project / "missing.jsonl"
    missing.symlink_to(tmp_path / "never-created.jsonl")
    assert not missing.exists()
    rejection = None
    try:
        run(project, out, files=[str(missing)], exclude_files=[str(missing)])
    except (ValueError, OSError) as exc:
        rejection = exc
    assert rejection is not None, "unresolvable exclusion accepted solely because its spelling matched --files"


def test_unresolvable_persisted_alias_does_not_silently_reinclude(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    path = claude(project)
    alias = tmp_path / "exclude-alias.jsonl"
    alias.symlink_to(path)
    assert run(project, out, exclude_files=[str(alias)])["files_read"] == []
    alias.unlink()
    # Either retain the resolved exclusion or reject the unresolved policy.
    try:
        report = run(project, out)
    except (ValueError, OSError):
        return
    assert report["files_read"] == [], "vanished exclusion alias silently allowed the excluded transcript"


def test_relative_symlink_exclude_beats_realpath_pin_control(tmp_path):
    project, out = tmp_path / "project", tmp_path / "out"
    path = codex(project)
    alias = project / "relative-alias.jsonl"
    alias.symlink_to(path)
    picked, _ = tc.select_codex_files(str(project), session_files=[str(path.resolve())])
    assert picked == [str(path)]
    report = tc.run(None, "txxxx", str(out), codex_dir=str(project),
                    codex_session_files=[str(path.resolve())], exclude_files=["./relative-alias.jsonl"])
    assert report["files_read"] == []


def test_unmatched_new_exclusion_is_rejected_control(tmp_path):
    project = tmp_path / "project"
    claude(project)
    with pytest.raises(ValueError, match="matched no transcript"):
        run(project, tmp_path / "out", exclude_files=[str(project / "missing.jsonl")])


def test_deleted_between_select_and_read_does_not_abort_survivors(tmp_path, monkeypatch):
    project, out = tmp_path / "project", tmp_path / "out"
    gone = claude(project, "a-gone.jsonl")
    kept = claude(project, "b-kept.jsonl", "session-kept")
    original_select = tc.select_files
    deletion = []

    def select_then_delete(*args, **kwargs):
        picked = original_select(*args, **kwargs)
        assert str(gone) in picked and str(kept) in picked
        gone.unlink()
        deletion.append(True)
        return picked

    monkeypatch.setattr(tc, "select_files", select_then_delete)
    error, report = None, None
    try:
        report = run(project, out)
    except FileNotFoundError as exc:
        assert exc.filename == str(gone), "only the deliberately deleted transcript may be missing"
        error = exc
    assert deletion == [True], "precondition: deletion happened after actual selection"
    assert error is None, "selected transcript deletion aborted the run before the surviving file was read"
    assert kept.name in report["files_read"]


def test_two_seats_share_project_but_not_exclusion_policy_control(tmp_path):
    project = tmp_path / "project"
    own = claude(project)
    foreign = claude(project, "foreign.jsonl", "session-foreign", seat="uxxx")
    first = run(project, tmp_path / "txxxx-out", exclude_files=[foreign.name])
    second = run(project, tmp_path / "uxxx-out", seat="uxxx")
    assert first["files_read"] == [own.name]
    assert second["files_read"] == [foreign.name]
    assert run(project, tmp_path / "txxxx-out")["files_read"] == [own.name]


def single_seat_kwargs():
    if "single_seat_dir" not in inspect.signature(tc.run).parameters:
        pytest.skip("L2-only adversarial contract: base has no single-seat declaration")
    return {"single_seat_dir": True}


def test_l2_single_seat_rejects_foreign_marker_control(tmp_path):
    options = single_seat_kwargs()
    project = tmp_path / "project"
    claude(project)
    claude(project, "unmarked.jsonl", "session-unmarked", seat=None)
    claude(project, "foreign.jsonl", "session-foreign", seat="uxxx")
    report = run(project, tmp_path / "out", **options)
    assert report["files_read"] == ["alpha.jsonl", "unmarked.jsonl"]


def test_l2_single_seat_does_not_follow_symlink_outside_declared_folder(tmp_path):
    options = single_seat_kwargs()
    project = tmp_path / "project"
    claude(project)
    claude(project, "foreign.jsonl", "session-foreign", seat="uxxx")
    outside = claude(tmp_path / "outside-project", "outside.jsonl", "session-outside", seat=None)
    alias = project / "linked-outside.jsonl"
    alias.symlink_to(outside)
    assert outside.parent != project and tmp_path in outside.parents
    report = run(project, tmp_path / "out", **options)
    assert "foreign.jsonl" not in report["files_read"], "foreign-marker control must remain excluded"
    assert alias.name not in report["files_read"], "single-seat declaration escaped its folder through a symlink"
