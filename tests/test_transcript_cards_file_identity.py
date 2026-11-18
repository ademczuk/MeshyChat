"""Filesystem aliases must not bypass transcript exclusions or move a frozen exclusion."""
import json
import os
import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "transcript_cards"))
import load_hook as lh
import transcript_cards as tc


def _resolved(path):
    return tc.exclusion_specs({str(path): {"resolved": True, "realpaths": [str(path)],
                                         "sessions": [], "card_refs": []}})


def _aliases(tmp_path):
    original = tmp_path / "private.jsonl"
    original.write_text("private", encoding="utf-8")
    alias = tmp_path / "other.jsonl"
    os.link(original, alias)
    return original, alias


@pytest.mark.parametrize("resolved", [False, True])
def test_file_alias_is_excluded(tmp_path, resolved):
    original, alias = _aliases(tmp_path)
    specs = _resolved(original) if resolved else [str(original)]
    assert tc._matches_file_spec(str(alias), specs)


def test_relative_alias_uses_recorded_base(tmp_path, monkeypatch):
    original, alias = _aliases(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert tc._matches_file_spec(alias.name, [original.name], base_dir=str(tmp_path))
    assert tc._matches_file_spec(alias.name, _resolved(original), base_dir=str(tmp_path))


def test_hook_resume_keeps_alias_excluded(tmp_path):
    original, alias = _aliases(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    (store / "cursor.json").write_text(json.dumps({
        "excluded_files": {str(original): {"resolved": True, "realpaths": [str(original)],
                                          "sessions": [], "card_refs": []}},
        "exclude_base": str(tmp_path),
    }), encoding="utf-8")
    excluded = lh._store_excluded_fn(str(store))
    assert excluded(alias.name)
    assert not excluded("public.jsonl")


@pytest.mark.parametrize("legacy_resolved", [False, True])
def test_loader_resumes_legacy_exclusion_with_hardlink_alias(tmp_path, legacy_resolved):
    base = tmp_path / "transcripts"
    base.mkdir()
    private = base / "private.jsonl"
    keep = base / "public.jsonl"
    for path, label in ((private, "DROP"), (keep, "KEEP")):
        record = {"type": "user", "uuid": label, "timestamp": "2026-10-24T07:00:00.000Z",
                  "message": {"role": "user", "content": "echo txxxx-shell-ok\nAB " + label}}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    alias = base / "alias.jsonl"
    os.link(private, alias)
    out = tmp_path / "store"
    out.mkdir()
    value = ({"resolved": True, "realpaths": [str(private)], "sessions": [], "card_refs": []}
             if legacy_resolved else True)
    (out / "cursor.json").write_text(json.dumps({"files": {}, "excluded_files": {str(private): value}}),
                                   encoding="utf-8")
    kwargs = dict(project_dir=str(base), seat="txxxx", files=[str(private), str(alias), str(keep)],
                  out_dir=str(out), spool_pending=True)
    for _ in range(2):
        assert tc.run(**kwargs)["cards_total"] == 1
        for filename in ("cards.jsonl", "pending.jsonl"):
            content = (out / filename).read_text(encoding="utf-8")
            assert "AB KEEP" in content
            assert "AB DROP" not in content
        cursor = json.loads((out / "cursor.json").read_text(encoding="utf-8"))
        assert cursor["excluded_files"][str(private)]["resolution"] == "exact-v1"


@pytest.mark.parametrize("identity_available", [False, True])
def test_loader_rejects_distinct_files_for_legacy_basename(tmp_path, monkeypatch, identity_available):
    base = tmp_path / "transcripts"
    files = [base / folder / "private.jsonl" for folder in ("first", "second")]
    for path in files:
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    if not identity_available:
        monkeypatch.setattr(tc, "_file_identity", lambda path: None)
    out = tmp_path / "store"
    out.mkdir()
    (out / "cursor.json").write_text(json.dumps({"files": {}, "excluded_files": {"private.jsonl": True}}),
                                   encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous") as exc:
        tc.run(project_dir=str(base), seat="txxxx", files=[str(path) for path in files], out_dir=str(out))
    for path in files:
        assert os.path.normcase(os.path.realpath(path)) in str(exc.value)


@pytest.mark.parametrize("resolved", [False, True])
def test_same_contents_do_not_make_distinct_files_aliases(tmp_path, resolved):
    original, _ = _aliases(tmp_path)
    other = tmp_path / "public.jsonl"
    other.write_bytes(original.read_bytes())
    specs = _resolved(original) if resolved else [str(original)]
    assert not tc._matches_file_spec(str(other), specs)


@pytest.mark.parametrize("resolved", [False, True])
def test_zero_inode_keeps_distinct_files_separate(tmp_path, monkeypatch, resolved):
    original = tmp_path / "private.jsonl"
    other = tmp_path / "public.jsonl"
    original.write_text("private", encoding="utf-8")
    other.write_text("public", encoding="utf-8")
    original_stat = os.stat
    paths = {os.path.normcase(os.path.abspath(str(path))) for path in (original, other)}

    def zero_inode_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if not isinstance(path, int) and os.path.normcase(os.path.abspath(os.fspath(path))) in paths:
            fields = list(result)
            fields[1] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(tc.os, "stat", zero_inode_stat)
    specs = _resolved(original) if resolved else [str(original)]
    assert tc._matches_file_spec(str(original), specs)
    assert not tc._matches_file_spec(str(other), specs)


def test_unavailable_stat_keeps_exact_path_fallback(tmp_path, monkeypatch):
    original = tmp_path / "private.jsonl"
    other = tmp_path / "public.jsonl"
    monkeypatch.setattr(tc, "_file_identity", lambda path: None)
    assert tc._matches_file_spec(str(original), [str(original)])
    assert tc._matches_file_spec(str(original), _resolved(original))
    assert not tc._matches_file_spec(str(other), _resolved(original))


@pytest.mark.parametrize("parent_link", [False, True])
def test_frozen_path_does_not_rebind_through_new_symlink(tmp_path, parent_link):
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    original = old_dir / "private.jsonl"
    original.write_text("old", encoding="utf-8")
    target_dir = tmp_path / "new"
    target_dir.mkdir()
    target = target_dir / "private.jsonl"
    target.write_text("new", encoding="utf-8")
    specs = _resolved(original)
    moved = tmp_path / "moved"
    try:
        if parent_link:
            old_dir.rename(moved)
            old_dir.symlink_to(target_dir, target_is_directory=True)
        else:
            original.rename(moved)
            original.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert not tc._matches_file_spec(str(target), specs)
    assert not tc._matches_file_spec(str(original), specs)


@pytest.mark.parametrize("alias_kind", ["case", "unicode"])
def test_native_case_or_unicode_alias(tmp_path, alias_kind):
    name = "Private.jsonl" if alias_kind == "case" else "caf\u00e9.jsonl"
    alias_name = name.lower() if alias_kind == "case" else unicodedata.normalize("NFD", name)
    original = tmp_path / name
    original.write_text("private", encoding="utf-8")
    alias = tmp_path / alias_name
    if not alias.exists() or not os.path.samefile(original, alias):
        pytest.skip("filesystem does not alias these spellings")
    assert tc._matches_file_spec(str(alias), [str(original)])
    assert tc._matches_file_spec(str(alias), _resolved(original))


@pytest.mark.parametrize("dialect", ["antigravity", "kimi", "claude"])
@pytest.mark.parametrize("alias_kind", ["case", "NFD"])
def test_alias_exclusion_end_to_end(tmp_path, dialect, alias_kind):
    base = tmp_path / "transcripts"
    private_folder = "session_Case-B" if alias_kind == "case" else "session_caf\u00e9-b"
    alias_folder = (
        private_folder.lower()
        if alias_kind == "case"
        else unicodedata.normalize("NFD", private_folder)
    )

    def transcript(folder, label):
        text = f"AB {label} operator prompt for native alias exclusion"
        timestamp = "2026-10-24T07:00:00.000Z"
        if dialect == "antigravity":
            path = base / folder / ".system_generated/logs/transcript.jsonl"
            records = [{
                "step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                "status": "DONE", "created_at": timestamp, "content": text,
            }]
        elif dialect == "kimi":
            path = base / folder / "agents/main/wire.jsonl"
            records = [
                {"type": "metadata", "protocol_version": "1.5"},
                {"type": "context.append_message", "time": 1787064216000,
                 "message": {"role": "user", "id": label,
                             "content": [{"type": "text", "text": text}],
                             "origin": {"kind": "user"}}},
            ]
        else:
            path = base / folder / f"{label.lower()}.jsonl"
            # Deliberately omit sessionId: the file exclusion must suffice.
            records = [{"type": "user", "uuid": label, "timestamp": timestamp,
                        "message": {"role": "user", "content": "echo txxxx-shell-ok\n" + text}}]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
                        encoding="utf-8")
        return path

    keep = transcript("session_keep-a", "KEEP")
    private = transcript(private_folder, "DROP")
    exclusion = "./" + private.relative_to(base).as_posix().replace(private_folder, alias_folder)
    alias = base / exclusion
    if not alias.exists() or not os.path.samefile(private, alias):
        if alias_kind == "NFD":
            pytest.skip("filesystem does not make the NFD spelling an alias")
        pytest.skip(f"filesystem does not make the {alias_kind} spelling an alias")

    kwargs = dict(project_dir=str(base) if dialect == "claude" else None,
                  seat="txxxx", files=[str(keep), str(private)])
    if dialect != "claude":
        kwargs[f"{dialect}_dir"] = str(base)
    control = tc.run(out_dir=str(tmp_path / "control"), **kwargs)
    assert control["cards_total"] == 2

    out_dir = tmp_path / "excluded"
    result = tc.run(out_dir=str(out_dir), exclude_files=[exclusion], spool_pending=True, **kwargs)
    assert result["cards_total"] == 1
    for filename in ("cards.jsonl", "pending.jsonl"):
        content = (out_dir / filename).read_text(encoding="utf-8")
        assert "AB KEEP" in content
        assert "AB DROP" not in content
        assert len(content.splitlines()) == 1
    resumed = tc.run(out_dir=str(out_dir), exclude_files=[exclusion], spool_pending=True, **kwargs)
    assert resumed["cards_total"] == 1
    for filename in ("cards.jsonl", "pending.jsonl"):
        content = (out_dir / filename).read_text(encoding="utf-8")
        assert "AB DROP" not in content
        assert len(content.splitlines()) == 1


def test_missing_frozen_exclusion_preserves_spelling_session_and_ref_rules(tmp_path):
    original = tmp_path / "private.jsonl"
    original.write_text("private", encoding="utf-8")
    realpath = os.path.realpath(original)
    specs = tc.exclusion_specs({str(original): {
        "resolved": True, "realpaths": [realpath],
        "sessions": ["private-session"], "card_refs": ["planboard:private-ref"],
    }})
    original.unlink()
    assert tc._matches_file_spec(str(original), specs)
    assert tc._matches_file_spec("private.jsonl", specs, base_dir=str(tmp_path))
    assert tc._matches_file_spec(str(tmp_path / "other.jsonl"), specs,
                                 session_id="private-session")
    assert tc._matches_file_spec("planboard:private-ref", specs)
    assert not tc._matches_file_spec(str(tmp_path / "other.jsonl"), specs)
    assert not tc._matches_file_spec(str(tmp_path / "private.jsonl.extra"), specs)
    assert not tc._matches_file_spec(str(tmp_path / "other.jsonl"), specs,
                                     session_id="private-session-extra")
    assert not tc._matches_file_spec("planboard:private-ref-extra", specs)


def test_dialect_session_rel_matchers_and_basename_isolation(tmp_path):
    import antigravity_dialect as ag
    import kimi_dialect as km

    ag_target = str(tmp_path / "brain_a" / "cid-1111" / ".system_generated" / "logs" / "transcript.jsonl")
    ag_other = str(tmp_path / "brain_a" / "cid-2222" / ".system_generated" / "logs" / "transcript.jsonl")
    ag_rel_spec = "CID-1111/.system_generated/logs/transcript.jsonl"
    assert ag._is_excluded(ag_target, str(tmp_path / "brain_a"), "cid-1111", [ag_rel_spec])
    assert not ag._is_excluded(ag_other, str(tmp_path / "brain_a"), "cid-2222", [ag_rel_spec])
    ag_foreign_root_spec = "/old/root/brain_old/CID-1111/.system_generated/logs/transcript.jsonl"
    assert not ag._is_excluded(ag_target, str(tmp_path / "brain_a"), "cid-1111", [ag_foreign_root_spec])
    # Bare filename exclusion remains fail-closed across nested dialect paths (kxxx Option (a)).
    assert "transcript.jsonl" in ag._canonical_path_forms(ag_target, str(tmp_path / "brain_a"))
    assert "transcript.jsonl" not in ag._pattern_path_forms(ag_rel_spec, str(tmp_path / "brain_a"))
    assert ag._is_excluded(ag_target, str(tmp_path / "brain_a"), "cid-1111", ["transcript.jsonl"])
    assert ag._is_excluded(ag_other, str(tmp_path / "brain_a"), "cid-2222", ["transcript.jsonl"])

    km_target = str(tmp_path / "sessions_a" / "wt1" / "session_abc-123" / "agents" / "main" / "wire.jsonl")
    km_other = str(tmp_path / "sessions_a" / "wt1" / "session_def-456" / "agents" / "main" / "wire.jsonl")
    km_rel_spec = "wt1/SESSION_ABC-123/agents/main/wire.jsonl"
    km_sess_rel_spec = "SESSION_ABC-123/agents/main/wire.jsonl"
    assert km._is_excluded(km_target, str(tmp_path / "sessions_a"), "abc-123", [km_rel_spec])
    assert km._is_excluded(km_target, str(tmp_path / "sessions_a"), "abc-123", [km_sess_rel_spec])
    assert not km._is_excluded(km_other, str(tmp_path / "sessions_a"), "def-456", [km_rel_spec])
    assert not km._is_excluded(km_other, str(tmp_path / "sessions_a"), "def-456", [km_sess_rel_spec])
    km_foreign_ws_spec = "/old/home/sessions/wt_old/SESSION_ABC-123/agents/main/wire.jsonl"
    assert not km._is_excluded(km_target, str(tmp_path / "sessions_a"), "abc-123", [km_foreign_ws_spec])
    assert "wire.jsonl" in km._canonical_path_forms(km_target, str(tmp_path / "sessions_a"))
    assert "wire.jsonl" not in km._pattern_path_forms(km_rel_spec, str(tmp_path / "sessions_a"))
    assert km._is_excluded(km_target, str(tmp_path / "sessions_a"), "abc-123", ["wire.jsonl"])
    assert km._is_excluded(km_other, str(tmp_path / "sessions_a"), "def-456", ["wire.jsonl"])

    # Ancestor directories starting with `session_` (e.g. `/home/session_archive/.kimi/sessions/...`)
    # must never be extracted as Kimi session IDs or cause unrelated sessions under the same ancestor to match.
    anc_abc = "/home/session_archive/.kimi/sessions/wt/session_abc/agents/main/wire.jsonl"
    anc_def = "/home/session_archive/.kimi/sessions/wt/session_def/agents/main/wire.jsonl"
    _, anc_abc_sids = km._kimi_rel_and_sessions(anc_abc)
    _, anc_def_sids = km._kimi_rel_and_sessions(anc_def)
    assert "archive" not in anc_abc_sids
    assert "session_archive" not in anc_abc_sids
    assert "abc" in anc_abc_sids
    assert "def" in anc_def_sids
    assert not (anc_abc_sids & anc_def_sids)
    assert km._is_excluded(anc_abc, "/home/session_archive/.kimi/sessions", "abc", [anc_abc])
    assert not km._is_excluded(anc_def, "/home/session_archive/.kimi/sessions", "def", [anc_abc])


def test_workspace_qualified_pattern_does_not_bleed_across_shared_session_id(tmp_path, monkeypatch):
    import antigravity_dialect as ag
    import kimi_dialect as km

    sessions_root = tmp_path / "sessions"
    ws_a = sessions_root / "ws-a" / "session_s1" / "agents" / "main" / "wire.jsonl"
    ws_b = sessions_root / "ws-b" / "session_s1" / "agents" / "main" / "wire.jsonl"
    for path, ws, text in ((ws_a, "ws-a", "AB DROP ws-a"), (ws_b, "ws-b", "AB KEEP ws-b")):
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"type": "metadata", "protocol_version": "1.5", "created_at": 1760000000000}) + "\n"
            + json.dumps({
                "type": "context.append_message",
                "time": 1760000200000,
                "message": {
                    "id": "m-" + ws,
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    "origin": {"kind": "user", "variant": "interactive"},
                },
            }) + "\n",
            encoding="utf-8",
        )

    pat = "ws-a/session_s1/agents/main/wire.jsonl"
    pat_win = r"ws-a\session_s1\agents\main\wire.jsonl"
    assert "session_s1/agents/main/wire.jsonl" not in km._pattern_path_forms(pat, str(sessions_root))
    assert km._is_excluded(str(ws_a), str(sessions_root), "s1", [pat])
    assert not km._is_excluded(str(ws_b), str(sessions_root), "s1", [pat])
    assert km._is_excluded(str(ws_a), str(sessions_root), "s1", [pat_win])
    assert not km._is_excluded(str(ws_b), str(sessions_root), "s1", [pat_win])

    # Bare session identifiers remain session-scoped and match both workspaces sharing session_s1.
    for bare_sid in ("session_s1", "s1"):
        assert km._is_excluded(str(ws_a), str(sessions_root), "s1", [bare_sid])
        assert km._is_excluded(str(ws_b), str(sessions_root), "s1", [bare_sid])

    # End-to-end loader upgrade / resolution must resolve ws-a uniquely without ambiguity error on ws-b.
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out_kimi"
    out.mkdir()
    (out / "cursor.json").write_text(json.dumps({
        "files": {},
        "seat_files": {},
        "id_rule": tc.ID_RULE,
        "excluded_files": {pat: True},
        "kimi_files": {os.path.join("sessions", "ws-b", "session_s1", "agents", "main", "wire.jsonl"): True},
    }), encoding="utf-8")
    report = tc.run(None, "nxxxxx", str(out), kimi_dir="sessions")
    assert not any("ws-a" in f for f in report["files_read"])
    assert any("ws-b" in f for f in report["files_read"])
    cards_text = (out / "cards.jsonl").read_text(encoding="utf-8")
    assert "AB KEEP ws-b" in cards_text
    assert "AB DROP ws-a" not in cards_text

    # Symmetry check for Antigravity nested workspaces sharing a conversation directory name.
    brain_root = tmp_path / "brain"
    ag_a = brain_root / "ws-a" / "cid-s1" / ".system_generated" / "logs" / "transcript.jsonl"
    ag_b = brain_root / "ws-b" / "cid-s1" / ".system_generated" / "logs" / "transcript.jsonl"
    ag_pat = "ws-a/cid-s1/.system_generated/logs/transcript.jsonl"
    assert "cid-s1/.system_generated/logs/transcript.jsonl" not in ag._pattern_path_forms(ag_pat, str(brain_root))
    assert ag._is_excluded(str(ag_a), str(brain_root), "cid-s1", [ag_pat])
    assert not ag._is_excluded(str(ag_b), str(brain_root), "cid-s1", [ag_pat])
    assert ag._is_excluded(str(ag_a), str(brain_root), "cid-s1", ["cid-s1"])
    assert ag._is_excluded(str(ag_b), str(brain_root), "cid-s1", ["cid-s1"])
