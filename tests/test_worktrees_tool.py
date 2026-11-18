"""tools/worktrees: list and conservative prune, on a throwaway repository with a local bare origin (no network)."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from tools.worktrees import worktrees as wt


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    main = tmp_path / "main"
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True)
    _git(main, "config", "user.email", "t@example.invalid")
    _git(main, "config", "user.name", "t")
    _git(main, "checkout", "-q", "-b", "main")
    (main / "a.txt").write_text("a\n")
    _git(main, "add", "a.txt")
    _git(main, "commit", "-q", "-m", "a")
    _git(main, "push", "-q", "origin", "main")
    return tmp_path, main


def _add_tree(main, tmp_path, name, *, commit=False, push=False):
    path = tmp_path / name
    _git(main, "worktree", "add", "-q", "-b", f"seat/{name}", str(path), "origin/main")
    if commit:
        (path / f"{name}.txt").write_text("x\n")
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", name)
    if push:
        _git(path, "push", "-q", "origin", f"seat/{name}")
    return path


def _age(path, hours):
    gd = _git(path, "rev-parse", "--absolute-git-dir")
    old = time.time() - hours * 3600
    for f in ("HEAD", "index", "logs/HEAD"):
        p = os.path.join(gd, f)
        if os.path.exists(p):
            os.utime(p, (old, old))


def _judge(main, **kw):
    trees = wt.parse_porcelain(_git(main, "worktree", "list", "--porcelain") + "\n")
    _git(main, "fetch", "-q", "origin")
    wt.judge(str(main), trees, protect=kw.get("protect", set()), prs=kw.get("prs", set()), tips=kw.get("tips"),
             idle_min=kw.get("idle_min", 24), merged_heads=kw.get("merged_heads"))
    return {t.name: t.keep for t in trees}


def test_only_a_tree_with_nothing_to_lose_is_removable(repo):
    tmp, main = repo
    merged = _add_tree(main, tmp, "merged")
    ahead = _add_tree(main, tmp, "ahead", commit=True)
    dirty = _add_tree(main, tmp, "dirty")
    (dirty / "notes.txt").write_text("unsaved\n")
    cached = _add_tree(main, tmp, "cached")
    (cached / "__pycache__").mkdir()
    (cached / "__pycache__" / "m.pyc").write_bytes(b"x")
    (cached / ".gitignore").write_text("")  # untracked: must keep it
    for p in (merged, ahead, dirty, cached):
        _age(p, 48)
    keep = _judge(main)
    assert keep["merged"] == []
    assert keep["ahead"][0].startswith("has commits not on origin/main")
    assert keep["dirty"][0].startswith("has files: ?? notes.txt")
    assert keep["cached"][0].startswith("has files")  # the untracked .gitignore, not the cache
    assert keep["main"] == ["main checkout"]


def test_caches_alone_do_not_keep_a_tree(repo):
    tmp, main = repo
    p = _add_tree(main, tmp, "pyc")
    _git(main, "config", "core.excludesFile", str(tmp / "ignore"))
    (tmp / "ignore").write_text("__pycache__/\n.pytest_cache/\n")
    (p / "__pycache__").mkdir()
    (p / "__pycache__" / "m.pyc").write_bytes(b"x")
    (p / ".pytest_cache").mkdir()
    (p / ".pytest_cache" / "v").write_text("x")
    _age(p, 48)
    assert _judge(main)["pyc"] == []
    # CONTROL: an ignored file that is not a cache keeps it
    (tmp / "ignore").write_text("__pycache__/\n.pytest_cache/\n*.env\n")
    (p / "local.env").write_text("SECRET=1\n")
    assert _judge(main)["pyc"][0].startswith("has files: !! local.env")


def test_protected_open_pr_locked_and_recently_used_trees_are_kept(repo):
    tmp, main = repo
    for n in ("served", "pr", "locked", "fresh"):
        _age(_add_tree(main, tmp, n), 48)
    _git(main, "worktree", "lock", str(tmp / "locked"))
    _age(tmp / "fresh", 0)  # touched just now
    keep = _judge(main, protect={"served"}, prs={"seat/pr"})
    assert keep["served"] == ["protected (in use)"]
    assert keep["pr"] == ["open PR head"]
    assert keep["locked"] == ["locked"]
    assert keep["fresh"][0].startswith("touched 0.0 h ago")


def test_allow_pushed_removes_only_a_tree_equal_to_its_own_remote_tip(repo):
    tmp, main = repo
    pushed = _add_tree(main, tmp, "pushed", commit=True, push=True)
    local = _add_tree(main, tmp, "local", commit=True)
    _age(pushed, 48)
    _age(local, 48)
    tips = wt.remote_tips(str(main))
    keep = _judge(main, tips=tips)
    assert keep["pushed"] == []
    assert keep["local"][0].startswith("has commits not on origin/main or on its own remote branch")
    # CONTROL: without --allow-pushed the pushed tree is kept too
    assert _judge(main)["pushed"][0].startswith("has commits not on origin/main")


def test_prune_dry_run_changes_nothing_and_apply_writes_the_manifest_first(repo, monkeypatch, capsys):
    tmp, main = repo
    gone = _add_tree(main, tmp, "gone")
    kept = _add_tree(main, tmp, "kept", commit=True)
    _age(gone, 48)
    _age(kept, 48)
    monkeypatch.setattr(wt, "open_pr_branches", lambda repo: set())
    assert wt.main(["prune", "--repo", str(main)]) == 0
    out = capsys.readouterr().out
    assert "WOULD REMOVE gone seat/gone" in out and gone.exists()
    manifest = tmp / "manifest.jsonl"
    assert wt.main(["prune", "--repo", str(main), "--apply", "--manifest", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert "REMOVED gone seat/gone" in out
    assert not gone.exists() and kept.exists()
    rows = [json.loads(l) for l in manifest.read_text().splitlines()]
    assert [r["branch"] for r in rows] == ["seat/gone"] and len(rows[0]["sha"]) == 40
    branches = _git(main, "branch", "--format=%(refname:short)").split()
    assert "seat/gone" not in branches and "seat/kept" in branches  # a merged branch goes, an unmerged one stays
    # the manifest brings it back
    _git(main, "worktree", "add", "-q", str(gone), rows[0]["sha"])
    assert (gone / "a.txt").exists()


def test_prune_refuses_without_the_open_pr_list(repo, monkeypatch, capsys):
    tmp, main = repo
    _age(_add_tree(main, tmp, "x"), 48)

    def boom(repo):
        raise RuntimeError("gh pr list failed: not logged in")
    monkeypatch.setattr(wt, "open_pr_branches", boom)
    assert wt.main(["prune", "--repo", str(main), "--apply", "--manifest", str(tmp / "m.jsonl")]) == 2
    assert "refused: gh pr list failed" in capsys.readouterr().out
    assert (tmp / "x").exists()


def test_allow_merged_pr_requires_exact_head_and_own_branch(repo):
    tmp, main = repo
    merged = _add_tree(main, tmp, "squashed", commit=True)
    merged_sha = _git(merged, "rev-parse", "HEAD")
    _age(merged, 48)
    heads = {("seat/squashed", merged_sha)}
    assert _judge(main, merged_heads=heads)["squashed"] == []
    assert _judge(main)["squashed"][0].startswith("has commits not on origin/main")
    assert _judge(main, merged_heads={("seat/other", merged_sha)})["squashed"]
    (merged / "later.txt").write_text("not in the merged PR\n")
    _git(merged, "add", "later.txt")
    _git(merged, "commit", "-q", "-m", "past merged head")
    _age(merged, 48)
    assert _judge(main, merged_heads=heads)["squashed"][0].startswith("has commits not on origin/main")


@pytest.mark.parametrize("guard", ["protected", "open_pr", "locked", "recent", "dirty"])
def test_merged_pr_still_obeys_existing_guards(repo, guard):
    tmp, main = repo
    p = _add_tree(main, tmp, "guarded", commit=True)
    _age(p, 48)
    kw = {"merged_heads": {("seat/guarded", _git(p, "rev-parse", "HEAD"))}}
    if guard == "protected":
        kw["protect"] = {"guarded"}
    elif guard == "open_pr":
        kw["prs"] = {"seat/guarded"}
    elif guard == "locked":
        _git(main, "worktree", "lock", str(p))
    elif guard == "recent":
        _age(p, 0)
    else:
        (p / "unsaved.txt").write_text("keep\n")
    assert _judge(main, **kw)["guarded"]


def test_merged_pr_flag_dry_run_and_apply_keep_local_branch(repo, monkeypatch, capsys):
    tmp, main = repo
    p = _add_tree(main, tmp, "squashed", commit=True)
    sha = _git(p, "rev-parse", "HEAD")
    _age(p, 48)
    seen = []
    monkeypatch.setattr(wt, "open_pr_branches", lambda repo: set())
    monkeypatch.setattr(wt, "merged_pr_heads", lambda repo: seen.append(repo) or {("seat/squashed", sha)})
    args = ["prune", "--repo", str(main)]
    assert wt.main(args) == 0
    assert '"removable": 0' in capsys.readouterr().out and seen == []
    assert wt.main(args + ["--allow-merged-pr"]) == 0
    assert "WOULD REMOVE squashed seat/squashed" in capsys.readouterr().out and p.exists()
    assert len(seen) == 1
    manifest = tmp / "merged-manifest.jsonl"
    real_git = wt.git

    def inspect_remove(*argv, **kwargs):
        if argv[:2] == ("worktree", "remove"):
            assert "--force" not in argv
            assert json.loads(manifest.read_text().strip())["sha"] == sha
        return real_git(*argv, **kwargs)

    monkeypatch.setattr(wt, "git", inspect_remove)
    assert wt.main(args + ["--allow-merged-pr", "--apply", "--manifest", str(manifest)]) == 0
    assert not p.exists()
    assert _git(main, "rev-parse", "seat/squashed") == sha
    assert len(seen) == 2


def test_merged_pr_even_an_ancestor_keeps_local_branch(repo, monkeypatch):
    tmp, main = repo
    p = _add_tree(main, tmp, "merged")
    sha = _git(p, "rev-parse", "HEAD")
    _age(p, 48)
    monkeypatch.setattr(wt, "open_pr_branches", lambda repo: set())
    monkeypatch.setattr(wt, "merged_pr_heads", lambda repo: {("seat/merged", sha)})
    assert wt.main(["prune", "--repo", str(main), "--allow-merged-pr", "--apply",
                    "--manifest", str(tmp / "manifest.jsonl")]) == 0
    assert not p.exists() and _git(main, "rev-parse", "seat/merged") == sha


@pytest.mark.parametrize("failure", ["exit", "json", "shape", "timeout", "missing"])
def test_merged_pr_gh_failure_refuses_even_with_no_open_pr_check(repo, monkeypatch, capsys, failure):
    tmp, main = repo
    p = _add_tree(main, tmp, "safe")
    _age(p, 48)
    real_run = wt.subprocess.run

    def run(argv, **kwargs):
        if argv[0] != "gh":
            return real_run(argv, **kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        if failure == "missing":
            raise FileNotFoundError("gh")
        return subprocess.CompletedProcess(argv, 1 if failure == "exit" else 0,
                                           "not json" if failure == "json" else "{}", "unavailable")

    monkeypatch.setattr(wt.subprocess, "run", run)
    manifest = tmp / "manifest.jsonl"
    assert wt.main(["prune", "--repo", str(main), "--allow-merged-pr", "--no-pr-check", "--apply",
                    "--manifest", str(manifest)]) == 2
    assert "refused: gh pr list merged" in capsys.readouterr().out
    assert p.exists() and not manifest.exists()


def test_merged_pr_lookup_is_one_call_and_keeps_reused_branch_heads(monkeypatch):
    seen = []
    rows = [{"headRefName": "seat/reused", "headRefOid": s * 40} for s in ("a", "b")]

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

    monkeypatch.setattr(wt.subprocess, "run", run)
    assert wt.merged_pr_heads(".") == {("seat/reused", "a" * 40), ("seat/reused", "b" * 40)}
    assert seen == [["gh", "pr", "list", "--state", "merged", "--limit", "1000", "--json",
                     "headRefName,headRefOid"]]


@pytest.mark.parametrize("guard", ["main", "prunable", "status_failed"])
def test_merged_pr_main_missing_and_unreadable_trees_are_kept(monkeypatch, tmp_path, guard):
    main = str(tmp_path / "main")
    tree = wt.Tree(main if guard == "main" else str(tmp_path / "other"), "seat/x", "a" * 40,
                   prunable=guard == "prunable")
    monkeypatch.setattr(wt, "git", lambda *a, **kw:
                        (1, "", "failed") if guard == "status_failed" and a[0] == "status" else (0, "", ""))
    monkeypatch.setattr(wt, "idle_hours", lambda *a: 48)
    wt.judge(main, [tree], protect=set(), prs=set(), tips=None, idle_min=24,
             merged_heads={("seat/x", tree.sha)})
    assert tree.keep


@pytest.mark.parametrize("guard", ["none", "protected", "open_pr", "locked", "recent", "dirty"])
def test_fast_guard_controls(monkeypatch, tmp_path, guard):
    main = str(tmp_path / "main")
    tree = wt.Tree(str(tmp_path / "other"), "seat/x", "a" * 40, locked=guard == "locked")
    monkeypatch.setattr(wt, "git", lambda *a, **kw: (0, "?? notes.txt\n" if guard == "dirty" else "", ""))
    monkeypatch.setattr(wt, "idle_hours", lambda *a: 0 if guard == "recent" else 48)
    wt.judge(main, [tree], protect={"other"} if guard == "protected" else set(),
             prs={"seat/x"} if guard == "open_pr" else set(), tips=None, idle_min=24,
             merged_heads={("seat/x", tree.sha)})
    assert bool(tree.keep) == (guard != "none")


def test_apply_stops_when_the_health_check_fails(repo, monkeypatch, capsys):
    tmp, main = repo
    for n in ("h1", "h2"):
        _age(_add_tree(main, tmp, n), 48)
    monkeypatch.setattr(wt, "open_pr_branches", lambda repo: set())
    monkeypatch.setattr(wt, "health_ok", lambda url: (False, 2.5))
    rc = wt.main(["prune", "--repo", str(main), "--apply", "--manifest", str(tmp / "m.jsonl"),
                  "--health-url", "http://127.0.0.1:1/livez"])
    assert rc == 3
    assert "STOP: http://127.0.0.1:1/livez took 2.50 s; 0 removed" in capsys.readouterr().out
    assert (tmp / "h1").exists() and (tmp / "h2").exists()


def test_list_reports_through_orients_inspector(repo, capsys, monkeypatch):
    """`list` reuses orient's source-3 inspector instead of a second copy (found with the code graph): its numbers
    are orient's, including unpushed commits."""
    tmp, main = repo
    ahead = _add_tree(main, tmp, "ahead", commit=True)
    (ahead / "wip.txt").write_text("x\n")
    from tools.meshy_node import orient
    seen = []
    real = orient._inspect_single_git_dir
    monkeypatch.setattr(orient, "_inspect_single_git_dir", lambda p, **kw: seen.append(p.name) or real(p, **kw))
    assert wt.main(["list", "--repo", str(main)]) == 0
    out = capsys.readouterr().out
    assert "ahead" in seen and "main" in seen
    row = next(ln for ln in out.splitlines() if ln.startswith("ahead "))
    behind, ahead_n, unpushed, dirty = row.split()[-4:]
    assert (behind, ahead_n, dirty) == ("0", "1", "1")


def test_a_config_that_hides_untracked_files_cannot_hide_them_from_prune(repo):
    """Codex P1 on #482: with status.showUntrackedFiles=no, a plain `git status` lists no untracked or ignored files,
    so an ignored local.env looked like nothing to lose. prune passes the modes explicitly."""
    tmp, main = repo
    p = _add_tree(main, tmp, "hidden")
    _git(main, "config", "status.showUntrackedFiles", "no")
    _git(main, "config", "core.excludesFile", str(tmp / "ignore2"))
    (tmp / "ignore2").write_text("*.env\n")
    (p / "local.env").write_text("SECRET=1\n")
    (p / "scratch.txt").write_text("x\n")
    _age(p, 48)
    keep = _judge(main)["hidden"]
    assert keep and keep[0].startswith("has files:")
    assert "local.env" in keep[0] or "scratch.txt" in keep[0]


def test_a_full_open_pr_page_refuses_instead_of_guessing():
    """Codex P2 on #482: a page that comes back at the limit may be cut, and a missed open head looks removable."""
    rows = [{"headRefName": f"b{i}"} for i in range(5)]
    assert wt.pr_heads(rows, limit=6) == {f"b{i}" for i in range(5)}
    with pytest.raises(RuntimeError, match="may be incomplete"):
        wt.pr_heads(rows, limit=5)


def test_apply_refuses_a_manifest_inside_a_tree_it_would_remove(repo, monkeypatch, capsys):
    """Codex P2 on #482: the manifest is the only way back, so it must not be deleted with the tree."""
    tmp, main = repo
    gone = _add_tree(main, tmp, "gone")
    _age(gone, 48)
    monkeypatch.setattr(wt, "open_pr_branches", lambda repo: set())
    inside = gone / "manifest.jsonl"
    rc = wt.main(["prune", "--repo", str(main), "--apply", "--manifest", str(inside)])
    assert rc == 2
    assert "is inside gone" in capsys.readouterr().out
    assert gone.exists() and not inside.exists()


def test_a_never_pushed_commit_on_a_branch_without_upstream_lists_as_unpushed(repo, capsys):
    """uxxx on #482 (8e470f): a branch made from a LOCAL base has no `@{u}`, and orient read that as unpushed 0, so a
    commit that exists nowhere else looked safe. _add_tree branches from origin/main and so always had an upstream."""
    tmp, main = repo
    path = tmp / "localbase"
    _git(main, "worktree", "add", "-q", "-b", "seat/localbase", str(path), "main")  # local start: no upstream
    assert subprocess.run(["git", "-C", str(path), "rev-parse", "@{u}"], capture_output=True).returncode != 0
    (path / "only-here.txt").write_text("x\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "only here")
    assert wt.main(["list", "--repo", str(main)]) == 0
    row = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("localbase "))
    behind, ahead_n, unpushed, dirty = row.split()[-4:]
    assert (ahead_n, unpushed) == ("1", "1")
    # CONTROL: once that commit is on a remote branch, it is no longer unpushed
    _git(path, "push", "-q", "origin", "seat/localbase")
    assert wt.main(["list", "--repo", str(main)]) == 0
    row = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("localbase "))
    assert row.split()[-2] == "0"


def test_every_git_call_disables_optional_locks_both_ways(monkeypatch):
    """The flag on the argv AND GIT_OPTIONAL_LOCKS=0 in the env, as orient._git_run does: either alone is one edit away
    from a read that rewrites the index and makes every tree look freshly touched. Checked on the call itself, so a
    mutant that drops either one fails here even while the other still keeps the behaviour."""
    seen = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(wt.subprocess, "run", lambda argv, **kw: seen.append((argv, kw.get("env") or {})) or R())
    wt.git("status", cwd=".")
    (argv, env), = seen
    assert argv[:2] == ["git", "--no-optional-locks"]
    assert env.get("GIT_OPTIONAL_LOCKS") == "0"


def test_porcelain_parse_and_cache_rule():
    text = ("worktree /r\nHEAD aaa\nbranch refs/heads/main\n\nworktree /r/w\nHEAD bbb\ndetached\nlocked reason\n\n"
            "worktree /gone\nHEAD ccc\nbranch refs/heads/x\nprunable gitdir file points to non-existent location\n")
    trees = wt.parse_porcelain(text)
    assert [(t.name, t.branch, t.locked, t.prunable) for t in trees] == [
        ("r", "main", False, False), ("w", "", True, False), ("gone", "x", False, True)]
    assert wt.only_caches("!! a/__pycache__/\n!! b.pyc\n") == (True, [])
    assert wt.only_caches("!! .venv/\n")[0] is False
