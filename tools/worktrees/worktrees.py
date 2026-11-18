"""`python -m tools.worktrees list|prune`: a seat's git worktrees, with status, and a conservative clean-up.

Built 2026-11-02 after HQD was found holding 232 worktrees of this repository, 158 of them with nothing that
origin/main lacked. The conventions are worktrunk's (max-sixty/worktrunk, MIT or Apache-2.0): `wt list` shows each
worktree's branch, ahead/behind main and dirty state; `wt remove` removes a worktree and a branch that main already
holds. No code is taken from it.

`list` is read-only. `prune` is a dry run unless `--apply`, and a worktree is REMOVABLE only when every check holds:
  - it is not the main checkout, not locked, not named by `--protect`, and not the head of an open pull request;
  - its HEAD is an ancestor of origin/main, or (`--allow-pushed`) exactly the tip of its own branch on origin,
    or (`--allow-merged-pr`) exactly a merged PR's head for its own branch;
  - `git status --porcelain --ignored` shows nothing but caches (__pycache__, .pytest_cache, .mypy_cache, .ruff_cache,
    *.pyc);
  - its git dir was not touched in the last `--idle-hours` (default 24): a seat may be working in it.
`--apply` writes each worktree's path, branch and sha to `--manifest` BEFORE removing it (`git worktree add <path>
<sha>` brings it back), removes it with `git worktree remove` (never --force), and deletes the local branch only when
its tip is an ancestor of origin/main and it was not matched by `--allow-merged-pr`. Remote branches are never touched.
With `--health-url` it checks that URL before
every `--batch` removals and stops if the answer fails or takes over 1 s (a box that also serves production).

The open pull requests come from `gh pr list`; when gh fails, prune refuses rather than guessing, unless
`--no-pr-check` is given. The merged PR lookup always fails closed when `--allow-merged-pr` is enabled.
A seat's own in-use worktrees (a running server, a scheduled task, a client named in a config)
are not visible to git: name them with `--protect`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

CACHE_PARTS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")


def git(*args: str, cwd: str, timeout: int = 120) -> tuple[int, str, str]:
    # The same git invocation as orient's _git_run (tools/meshy_node/orient.py:602, found with the code graph):
    # --no-optional-locks and GIT_OPTIONAL_LOCKS=0, so a read (status) never rewrites the index (else every tree it
    # reads looks freshly touched to the idle check of the next run, and a dry run followed by --apply keeps
    # everything); and no console window per call on Windows (a seat runs this from its Bash tool).
    from tools.meshy_node.orient import _no_window_flags
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    r = subprocess.run(["git", "--no-optional-locks", "-C", cwd, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=env, creationflags=_no_window_flags())
    return r.returncode, r.stdout, r.stderr


@dataclass
class Tree:
    path: str
    branch: str  # "" when detached
    sha: str
    locked: bool = False
    prunable: bool = False
    keep: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def parse_porcelain(text: str) -> list[Tree]:
    trees, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line:
            if cur:
                trees.append(Tree(path=cur["worktree"], branch=str(cur.get("branch", "")).removeprefix("refs/heads/"),
                                  sha=str(cur.get("HEAD", "")), locked="locked" in cur, prunable="prunable" in cur))
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    return trees


def only_caches(status: str) -> tuple[bool, list[str]]:
    """True when `git status --porcelain --ignored` names nothing but caches; else the first offending lines."""
    other = []
    for line in status.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"').replace("\\", "/")
        parts = path.rstrip("/").split("/")
        if line.startswith("!!") and (any(p in CACHE_PARTS for p in parts) or path.endswith(".pyc")):
            continue
        other.append(line.strip())
    return not other, other[:3]


def idle_hours(path: str, now: float | None = None) -> float:
    rc, gd, _ = git("rev-parse", "--absolute-git-dir", cwd=path)
    if rc != 0:
        return 0.0  # unreadable: treat as just touched, so it is kept
    newest = 0.0
    for f in ("HEAD", "index", "logs/HEAD"):
        p = os.path.join(gd.strip(), f)
        if os.path.exists(p):
            newest = max(newest, os.path.getmtime(p))
    return ((now or time.time()) - newest) / 3600 if newest else 1e9


PR_LIST_LIMIT = 1000


def pr_heads(rows: list, limit: int = PR_LIST_LIMIT) -> set[str]:
    """Open-PR head branches from gh's rows. A page that comes back FULL may be cut, and a missed open head would make
    its worktree look removable, so a full page refuses rather than guesses (Codex on #482, worktrees.py:106)."""
    if len(rows) >= limit:
        raise RuntimeError(f"gh pr list returned {len(rows)} rows, the limit: the open-PR list may be incomplete")
    return {p["headRefName"] for p in rows}


def open_pr_branches(repo: str) -> set[str]:
    r = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", str(PR_LIST_LIMIT), "--json", "headRefName"],
                       cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        raise RuntimeError("gh pr list failed: " + (r.stderr or "").strip()[-200:])
    return pr_heads(json.loads(r.stdout or "[]"))


def remote_tips(repo: str) -> dict[str, str]:
    rc, out, err = git("ls-remote", "--heads", "origin", cwd=repo)
    if rc != 0:
        raise RuntimeError("git ls-remote failed: " + err.strip()[-200:])
    tips = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        tips[ref.removeprefix("refs/heads/")] = sha
    return tips


def merged_pr_heads(repo: str) -> set[tuple[str, str]]:
    """One bounded lookup; keep every merged head when a branch name was reused."""
    try:
        r = subprocess.run(["gh", "pr", "list", "--state", "merged", "--limit", "1000", "--json",
                            "headRefName,headRefOid"], cwd=repo, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("gh pr list merged failed: " + type(exc).__name__) from exc
    if r.returncode != 0:
        raise RuntimeError("gh pr list merged failed: " + (r.stderr or "").strip()[-200:])
    try:
        rows = json.loads(r.stdout)
        if not isinstance(rows, list) or any(
            not isinstance(p, dict) or not isinstance(p.get("headRefName"), str) or not p["headRefName"]
            or not isinstance(p.get("headRefOid"), str) or len(p["headRefOid"]) != 40
            or any(c not in "0123456789abcdef" for c in p["headRefOid"]) for p in rows
        ):
            raise ValueError("invalid merged PR heads")
        return {(p["headRefName"], p["headRefOid"]) for p in rows}
    except (ValueError, TypeError) as exc:
        raise RuntimeError("gh pr list merged returned invalid data") from exc


def judge(repo: str, trees: list[Tree], *, protect: set[str], prs: set[str], tips: dict[str, str] | None,
          idle_min: float, now: float | None = None, merged_heads: set[tuple[str, str]] | None = None) -> None:
    """Fill each tree's `keep` reasons; an empty list means removable. Checks run cheapest first and stop early."""
    main = os.path.normcase(os.path.abspath(repo))
    for t in trees:
        if os.path.normcase(os.path.abspath(t.path)) == main:
            t.keep.append("main checkout")
        if t.locked:
            t.keep.append("locked")
        if t.prunable:
            t.keep.append("prunable (its folder is gone: `git worktree prune`)")
        if t.name in protect:
            t.keep.append("protected (in use)")
        if t.branch and t.branch in prs:
            t.keep.append("open PR head")
        if t.keep:
            continue
        rc, _, _ = git("merge-base", "--is-ancestor", t.sha, "origin/main", cwd=repo)
        if rc != 0 and not (tips is not None and t.branch and tips.get(t.branch) == t.sha) and not (
            merged_heads is not None and t.branch and (t.branch, t.sha) in merged_heads
        ):
            t.keep.append("has commits not on origin/main" + ("" if tips is None else " or on its own remote branch"))
            continue
        # idle BEFORE status: `git status` refreshes the index, so measured after it every tree looks just touched
        # (2 Nov: a census's status run made 32 HQD worktrees read "touched 0.3 h ago")
        idle = idle_hours(t.path, now)
        if idle < idle_min:
            t.keep.append(f"touched {idle:.1f} h ago")
            continue
        # Explicit modes: a repo or user setting such as status.showUntrackedFiles=no would otherwise hide untracked and
        # ignored files, and prune would delete an ignored local.env with the tree (Codex P1 on #482, worktrees.py:150).
        rc, st, _ = git("status", "--porcelain", "--untracked-files=all", "--ignored=traditional", cwd=t.path)
        clean, other = only_caches(st) if rc == 0 else (False, ["git status failed"])
        if not clean:
            t.keep.append("has files: " + "; ".join(other))


def health_ok(url: str) -> tuple[bool, float | None]:
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            ok = 200 <= r.status < 300
    except Exception:  # noqa: BLE001 - any failure is a stop
        return False, None
    dt = time.monotonic() - t0
    return ok and dt < 1.0, dt


def cmd_list(args) -> int:
    """One row per worktree from orient's own inspector (`orient._inspect_single_git_dir`, the source 3 that
    `meshy_node orient` already reports): no second copy of the branch / ahead / behind / unpushed / dirty logic."""
    from pathlib import Path
    from tools.meshy_node.orient import _inspect_single_git_dir

    repo = os.path.abspath(args.repo)
    _, out, _ = git("worktree", "list", "--porcelain", cwd=repo)
    print(f"{'worktree':<34} {'branch':<44} {'behind':>6} {'ahead':>5} {'unpushed':>8} {'dirty':>5}")
    for t in parse_porcelain(out):
        info = None if t.prunable else _inspect_single_git_dir(Path(t.path))
        if info is None:
            print(f"{t.name[:34]:<34} {(t.branch or '(detached)')[:44]:<44} {'-':>6} {'-':>5} {'-':>8} {'?':>5}"
                  + ("  (prunable: its folder is gone)" if t.prunable else "  (not readable)"))
            continue
        print(f"{t.name[:34]:<34} {(t.branch or '(detached)')[:44]:<44} {info['behind_trunk']:>6} "
              f"{info['ahead_of_trunk']:>5} {info['unpushed_commits']:>8} {info['dirty_count']:>5}")
    return 0


def cmd_prune(args) -> int:
    repo = os.path.abspath(args.repo)
    rc, _, err = git("fetch", "-q", "origin", "main", cwd=repo)
    if rc != 0:
        print("refused: git fetch origin main failed: " + err.strip()[-200:])
        return 2
    try:
        prs = set() if args.no_pr_check else open_pr_branches(repo)
        tips = remote_tips(repo) if args.allow_pushed else None
        merged_heads = merged_pr_heads(repo) if args.allow_merged_pr else None
    except RuntimeError as exc:
        print(f"refused: {exc}")
        return 2
    _, out, _ = git("worktree", "list", "--porcelain", cwd=repo)
    trees = parse_porcelain(out)
    protect = {p.strip() for p in args.protect.split(",") if p.strip()}
    judge(repo, trees, protect=protect, prs=prs, tips=tips, idle_min=args.idle_hours, merged_heads=merged_heads)
    removable = [t for t in trees if not t.keep]
    reasons: dict[str, int] = {}
    for t in trees:
        if t.keep:
            k = t.keep[0].split(":")[0]
            reasons[k] = reasons.get(k, 0) + 1
    print(json.dumps({"worktrees": len(trees), "removable": len(removable), "kept": len(trees) - len(removable),
                      "kept_by_first_reason": reasons}))
    if not args.apply:
        for t in removable:
            print("WOULD REMOVE", t.name, t.branch or "(detached)", t.sha[:10])
        return 0
    if not args.manifest:
        print("refused: --apply needs --manifest")
        return 2
    # The manifest is the only way back: it must not live inside a tree this run removes (Codex on #482, :221).
    man = os.path.normcase(os.path.abspath(args.manifest))
    for t in removable:
        root = os.path.normcase(os.path.abspath(t.path)).rstrip("\\/") + os.sep
        if man.startswith(root):
            print(f"refused: the manifest {args.manifest} is inside {t.name}, a worktree this run would remove")
            return 2
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(args.manifest, "a", encoding="utf-8") as m:
        for t in removable:
            m.write(json.dumps({"path": t.path, "branch": t.branch, "sha": t.sha, "recorded_at": stamp}) + "\n")
    removed = refused = 0
    for i, t in enumerate(removable):
        if args.health_url and i % args.batch == 0:
            ok, dt = health_ok(args.health_url)
            if not ok:
                print(f"STOP: {args.health_url} {'failed' if dt is None else f'took {dt:.2f} s'}; {removed} removed")
                return 3
        rc, _, err = git("worktree", "remove", t.path, cwd=repo)
        if rc != 0:
            refused += 1
            print("REFUSED", t.name, err.strip()[-160:])
            continue
        removed += 1
        if t.branch and not (merged_heads is not None and (t.branch, t.sha) in merged_heads) and git(
            "merge-base", "--is-ancestor", t.branch, "origin/main", cwd=repo
        )[0] == 0:
            git("branch", "-D", t.branch, cwd=repo)
        print("REMOVED", t.name, t.branch or "(detached)", t.sha[:10])
        if args.health_url and (i + 1) % args.batch == 0:
            time.sleep(args.pause)
    print(json.dumps({"removed": removed, "refused": refused}))
    return 0 if refused == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.worktrees", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("list", help="each worktree: branch, behind/ahead of origin/main, tracked changes")
    lp.add_argument("--repo", required=True, help="the main checkout")
    pp = sub.add_parser("prune", help="remove worktrees that hold nothing origin/main (or their remote) lacks")
    pp.add_argument("--repo", required=True, help="the main checkout")
    pp.add_argument("--protect", default="", help="comma-separated worktree folder names a seat uses")
    pp.add_argument("--idle-hours", type=float, default=24.0)
    pp.add_argument("--allow-pushed", action="store_true",
                    help="also remove a worktree whose HEAD equals its own branch's tip on origin")
    pp.add_argument("--allow-merged-pr", action="store_true",
                    help="also remove a worktree at its own branch's exact merged PR head; keep the local branch")
    pp.add_argument("--no-pr-check", action="store_true", help="do not ask gh for open pull requests")
    pp.add_argument("--apply", action="store_true")
    pp.add_argument("--manifest", default="", help="JSON lines file: path, branch, sha of each removed worktree")
    pp.add_argument("--health-url", default="", help="checked before each batch; a failure or >1 s stops the run")
    pp.add_argument("--batch", type=int, default=5)
    pp.add_argument("--pause", type=float, default=3.0)
    args = ap.parse_args(argv)
    return cmd_list(args) if args.cmd == "list" else cmd_prune(args)


if __name__ == "__main__":
    sys.exit(main())
