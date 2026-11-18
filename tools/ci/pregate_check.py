"""Fast DB-free union/pair pre-gate collision filter (about 12 s warm, default budget 60 s for a cold first run; never replaces full local_gate.py).

Run it with the repo's own python (.venv): an interpreter that cannot import the repo's dependencies is refused at once with a named error."""
from __future__ import annotations
import argparse, ast, concurrent.futures as _cf, contextlib, ctypes, hashlib, importlib.util, json, os, posixpath, re, shutil, signal, subprocess, sys, tempfile, time, xml.etree.ElementTree as ET; from pathlib import Path; from typing import Any
EXIT_PASS, EXIT_FAILURE, EXIT_CONFLICT, EXIT_INCONCLUSIVE = 0, 1, 2, 3; _NO_DB_DSN = "postgresql://pregate_no_db:pregate_no_db@127.0.0.1:1/pregate_no_db?connect_timeout=1"
_PYTHON_KEPT = {"PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO"}; _SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"})
_DB_FIXTURES = frozenset({"db", "transactional_db", "django_db_setup", "django_db_blocker", "live_server", "admin_client", "admin_user", "django_user_model"}); _DB_TESTCASE_BASES = frozenset({"TestCase", "TransactionTestCase", "LiveServerTestCase", "StaticLiveServerTestCase"})
_IGNORED_STR_TOKENS = frozenset({"utf-8", "ascii", "replace", "ignore", "strict", "true", "false", "none", "json", "text"}); _GENERIC_STEMS = frozenset({"__init__", "views", "models", "urls", "admin", "apps", "utils", "base", "cli", "node", "schema", "settings", "types", "errors", "config"})
_MOD_LOADERS, _FILE_LOADERS = frozenset({"import_module", "__import__", "run_module"}), frozenset({"run_path", "spec_from_file_location", "SourceFileLoader"}); _DYN_LOADERS, _EXT_MOD_CACHE = _MOD_LOADERS | _FILE_LOADERS, {}
_PER_SHARD_ENV, _DYN_BYTES = frozenset({"TEMP", "TMP", "TMPDIR", "PREGATE_SKIP_CONFTESTS"}), tuple(k.encode("ascii") for k in _DYN_LOADERS); _HEX40_RE, _WORD_TOK_RE = re.compile(r"^[0-9a-f]{40}$"), re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REQUIRED_MODULES = ("django", "pytest")
def _is_doc_path(rel: str) -> bool: return rel.startswith("docs/") or ("/" not in rel and rel.endswith(".md"))
def _check_interpreter() -> None:
    missing = [m for m in _REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    if missing: raise InterpreterError(f"this interpreter ({sys.executable}) cannot import {', '.join(missing)}; run pregate_check.py with the repo's own python (.venv), not the system python")
def _whole_files(raw_nodes: list[str], total_by_rel: dict[str, int]) -> list[str]:
    """The test files of which EVERY test was selected. A file with one selected test of two is not in it."""
    return [x for x in _collapse_shard_nodes(raw_nodes, total_by_rel) if "::" not in x]
def _doc_reader_tests(wt: Path, doc_files: list[str], ran_whole: list[str]) -> list[str]:
    """Test files whose text names a changed doc file by path or by file name (local_gate.part_b_files' rule, names of 6+ characters) and that this run did not run WHOLE (`ran_whole`: a file with only some of its tests selected still counts as a reader, since the unselected test may be the one that reads the doc). Such a test can fail on the doc edit, so a run that skipped it is not a pass. Not seen: a doc whose file name is shorter than 6 characters and is named without its folder, and a name a test builds from fragments."""
    needles = {n for d in doc_files for n in (d, d.replace("/", "\\"), d.replace("/", "\\\\"), d.rsplit("/", 1)[-1]) if len(n) >= 6}
    if not needles or not (wt / "tests").is_dir(): return []
    return sorted(rel for t in (wt / "tests").rglob("test_*.py") if (rel := t.relative_to(wt).as_posix()) not in ran_whole and any(n in t.read_text(encoding="utf-8", errors="replace") for n in needles))
def _ek(k: str) -> str: return k.upper() if os.name == "nt" else k
class PregateError(Exception): pass
class InterpreterError(PregateError): pass
_IMPORT_RE, _CONT_IMP_RE = re.compile(r"^[ \t]*(?:from[ \t]+(\.+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_.]*)[ \t]+import[ \t]+(?:\(([^)]*)\)|([^\n#]+))|import[ \t]+([A-Za-z_][A-Za-z0-9_., \t]*))", re.MULTILINE), re.compile(r"^[ \t]*(?:from\b|import\b)[^\n#]*\\\r?$", re.MULTILINE)
_INLINE_IMP_RE, _PAREN_CMT_RE, _COMMENT_RE = re.compile(r"[:;][ \t]*(?:from[ \t]+[.\w]+[ \t]+import|import)\b"), re.compile(r"from[ \t]+[.\w]+[ \t]+import[ \t]*\([^)]*#[^\n]*\)"), re.compile(r"#[^\n]*")
_FAST_DB_RE, _DB_FIX_RE = re.compile(r"^(?:@pytest\.mark\.django_db\b|pytestmark\s*=\s*pytest\.mark\.django_db\b|(?:import|from)\s+(?:psycopg|meshychat\.asgi)\b|from\s+meshychat\s+import\b[^\n]*\basgi\b)", re.MULTILINE), re.compile(r"\b(?:db|transactional_db|django_db_setup|django_db_blocker|live_server|admin_client|admin_user|django_user_model)\b")
_DOTTED_STR_RE, _PY_PATH_RE = re.compile(r"['\"]([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)['\"]"), re.compile(r"['\"]([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+\.py)['\"]")
_HUNK_RE, _DIFF_DEF_RE, _DIFF_TOKEN_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"), re.compile(r"^[-+]\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)"), re.compile(r"['\"]([A-Za-z][A-Za-z0-9_.:-]{3,39})['\"]")
_ENV_WORDS, _ENV_TOK_RE, _ENV_ESC_RE = ("environ", "setenv", "unsetenv", "putenv", "delenv"), re.compile(r"\b(?:environb?|setenv|unsetenv|putenv|delenv)\b"), re.compile(r"\bgetattr\s*\(\s*(?:os|posix|nt)\s*,\s*(?:['\"](?:environb?|putenv|unsetenv)['\"]|[^'\"\s])|^[ \t]*import\b[^\n]*\b(?:os|posix|nt)[ \t]+as\b|\bos\.environb?\s*\n\s*(?:[.=\[]|\|=)|\\\r?\n[^\n]*(?:environb?|setenv|unsetenv|putenv|delenv|getattr)|(?:environb?|setenv|unsetenv|putenv|delenv|getattr)[^\n]*\\\r?$", re.MULTILINE)
_SAFE_LN_RE = re.compile(r"\.(?:setenv|delenv)\(\s*['\"]([A-Za-z_]\w*)['\"]\s*(?:,[^()*\n]*)?\)|(?<![.\w])dict\(\s*os\.environ\s*\)|\bos\.environ\.(?:get|copy|items|keys|values)\(|\bos\.environ\.(?:pop|setdefault)\(\s*['\"]([A-Za-z_]\w*)['\"]\s*(?:,[^()*\n]*)?\)|\bin[ \t]+os\.environ(?![ \t]*(?:[.=\[]|\|=|\w))|\bos\.environ\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\]")
_MAX_SHARDS, _MAX_LIGHT_NODES, _MAX_HEAVY_NODES, _HUB_IMPORTER_THRESHOLD = 2, 24, 2, 6; _STD_NON_TEST_BASES = frozenset({"object", "Exception", "BaseException", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "NamedTuple", "TypedDict", "Protocol", "ABC", "Generic"})
_UNITTEST_TC_FULL = frozenset({"unittest.TestCase", "unittest.case.TestCase", "unittest.IsolatedAsyncioTestCase", "unittest.async_case.IsolatedAsyncioTestCase", "django.test.SimpleTestCase", "django.test.testcases.SimpleTestCase"}); _SAFE_PYTEST_HOOKS = frozenset({"pytest_report_header", "pytest_terminal_summary", "pytest_addoption", "pytest_sessionfinish", "pytest_unconfigure"})
def _has_pytest_asyncio() -> bool:
    try: return importlib.util.find_spec("pytest_asyncio.plugin") is not None
    except (ImportError, ValueError): return False
_HAS_PYTEST_ASYNCIO, (_GIT_EXE, _GIT_FAST_DIR) = _has_pytest_asyncio(), (lambda g=shutil.which("git") or "git": (str(mg), str(mg.parent)) if (os.name == "nt" and len(ps := Path(g).resolve().parents) >= 2 and (mg := ps[1] / "mingw64" / "bin" / "git.exe").is_file()) else (g, None))()
def _rem(deadline: float) -> float: return left if (left := deadline - time.perf_counter()) > 0 else (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="pregate_check", timeout=0.0))
def _node_start(node: ast.AST) -> int: return min([getattr(node, "lineno", 1), *(getattr(d, "lineno", 10**9) for d in getattr(node, "decorator_list", ()))])
def _slice_node(lines: list[str], node: ast.AST) -> str: return "\n".join(lines[max(0, _node_start(node) - 1) : getattr(node, "end_lineno", 0)])
def _walk_pmap(root: ast.AST) -> tuple[list[ast.AST], dict[ast.AST, ast.AST]]:
    nodes, pmap, i = [root], {}, 0
    while i < len(nodes): p = nodes[i]; i += 1; [(pmap.__setitem__(v, p), nodes.append(v)) if isinstance(v := getattr(p, f, None), ast.AST) else ([pmap.__setitem__(c, p) or nodes.append(c) for c in v if isinstance(c, ast.AST)] if isinstance(v, list) else None) for f in p._fields]
    return nodes, pmap
def _norm_rel(raw: str) -> str: return "" if (np := posixpath.normpath(raw)) in (".", "..") or np.startswith("../") else np.removeprefix("./")
def _git_bytes(cwd: Path, *args: str, check: bool = True, timeout: float | None = None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]: return subprocess.run([_GIT_EXE, "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.quotePath=false", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "color.ui=false", "-c", "color.diff=false", *args], cwd=str(cwd), env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="pregate", GIT_AUTHOR_EMAIL="pregate@local.invalid", GIT_COMMITTER_NAME="pregate", GIT_COMMITTER_EMAIL="pregate@local.invalid", **(extra_env or {})), capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, check=check)
def _git_text(cwd: Path, *args: str, check: bool = True, timeout: float | None = None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]: p = _git_bytes(cwd, *args, check=check, timeout=timeout, extra_env=extra_env); return subprocess.CompletedProcess(p.args, p.returncode, p.stdout.decode("utf-8", errors="replace"), p.stderr.decode("utf-8", errors="replace"))
def _parse_head_spec(spec: str) -> tuple[str, str]:
    if not (raw := spec.strip()): raise PregateError("empty --head specification")
    label, rev = (p.strip() for p in raw.split("=", 1)) if "=" in raw else (raw[:12], raw); return (label, rev) if (label and rev) else (_ for _ in ()).throw(PregateError(f"malformed --head specification: {spec!r}"))
def _resolve_inputs(repo: Path, base: str, head_specs: list[str], deadline: float) -> tuple[Path, str, str, list[tuple[str, str]]]:
    specs = [_parse_head_spec(s) for s in head_specs]; fast = _git_text(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", f"{base}^{{commit}}", f"{base}^{{tree}}", *(f"{r}^{{commit}}" for _, r in specs), check=False, timeout=_rem(deadline))
    if fast.returncode == 0 and len(lines := [ln.strip() for ln in fast.stdout.splitlines() if ln.strip()]) == 3 + len(specs) and all(_HEX40_RE.match(x) for x in lines[1:]): return Path(lines[0]).resolve(), lines[1], lines[2], [(specs[i][0], lines[3 + i]) for i in range(len(specs))]
    if (cgd := _git_text(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False, timeout=_rem(deadline))).returncode != 0 or not cgd.stdout.strip(): raise PregateError(f"not a git repository: {repo}")
    def _one(rev: str) -> str: return sha if (p := _git_text(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False, timeout=_rem(deadline))).returncode == 0 and _HEX40_RE.match(sha := p.stdout.strip()) else (_ for _ in ()).throw(PregateError(f"cannot resolve commit {rev!r} in {repo}"))
    return Path(cgd.stdout.strip()).resolve(), (base_sha := _one(base)), _git_text(repo, "rev-parse", f"{base_sha}^{{tree}}", timeout=_rem(deadline)).stdout.strip(), [(lbl, _one(rev)) for lbl, rev in specs]
def _ensure_scratch_clone(common_git_dir: Path, wt: Path, timeout: float) -> None:
    def _norm(raw: str | Path, base_objs: Path | None = None) -> str: p = Path(raw) if Path(raw).is_absolute() or base_objs is None else (base_objs / raw); return s[4:] if (s := p.resolve().as_posix()).startswith("//?/") else s
    git_dir, owned_marker = wt / ".git", wt / ".git" / "pregate_owned"
    if git_dir.exists() and (not git_dir.is_dir() or not owned_marker.is_file()): raise PregateError(f"refusing unowned git directory at --worktree-dir: {wt}")
    if not git_dir.exists():
        if wt.exists() and any(wt.iterdir()): raise PregateError(f"refusing non-empty --worktree-dir: {wt}")
        wt.parent.mkdir(parents=True, exist_ok=True); _git_text(wt.parent, "clone", "-q", "--no-checkout", "--shared", "-c", "user.name=pregate", "-c", "user.email=pregate@local.invalid", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", str(common_git_dir), str(wt), timeout=timeout); owned_marker.write_text("1\n", encoding="utf-8")
    (alt_file := git_dir / "objects" / "info" / "alternates").parent.mkdir(parents=True, exist_ok=True); common_objs, ordered = common_git_dir / "objects", [_norm(common_git_dir / "objects")]
    for src_alt, base_dir in ((common_objs / "info" / "alternates", common_objs), (alt_file, git_dir / "objects")):
        if src_alt.is_file(): [ordered.append(norm) for ln in src_alt.read_bytes().decode("utf-8", errors="replace").splitlines() if (s := ln.strip()) and not s.startswith("#") and (norm := _norm(s, base_dir)) not in ordered]
    if not alt_file.is_file() or alt_file.read_bytes() != (desired := ("\n".join(ordered) + "\n").encode("utf-8")): alt_file.write_bytes(desired)
def _lock_file_nb(fh: Any, unlock: bool = False) -> None: fh.seek(0); (msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1) if (msvcrt := __import__("msvcrt")) else None) if os.name == "nt" else __import__("fcntl").flock(fh.fileno(), __import__("fcntl").LOCK_UN if unlock else (__import__("fcntl").LOCK_EX | __import__("fcntl").LOCK_NB))
def _default_scratch_root(common_git_dir: Path, repo: Path) -> Path: return next((cand / "meshy-pregate" for cand in (*(a for a in common_git_dir.parents if a.name == "_scratch"), common_git_dir.parent.parent / "_scratch") if cand.is_dir() and cand != repo and repo not in cand.parents and cand != common_git_dir and common_git_dir not in cand.parents), Path(tempfile.gettempdir()) / "meshy-pregate")
@contextlib.contextmanager
def _acquire_worktree(common_git_dir: Path, requested: Path | None, repo: Path, timeout: float):
    if requested is not None:
        if repo in (wt := requested.resolve()).parents or common_git_dir in wt.parents or wt in (*repo.parents, repo, *common_git_dir.parents, common_git_dir): raise PregateError(f"--worktree-dir {wt} must not overlap repository {repo}")
        wt.parent.mkdir(parents=True, exist_ok=True); _ensure_scratch_clone(common_git_dir, wt, timeout); yield wt; return
    key, root = hashlib.sha256(str(common_git_dir).lower().encode("utf-8")).hexdigest()[:12], _default_scratch_root(common_git_dir, repo); root.mkdir(parents=True, exist_ok=True); wt, lock_path, locked, lock_fh = root / f"wt-{key}", root / f"wt-{key}.lck", False, open(root / f"wt-{key}.lck", "a+b")
    try:
        (lock_fh.write(b"0") or lock_fh.flush() if os.path.getsize(lock_path) == 0 else None)
        with contextlib.suppress(OSError): _lock_file_nb(lock_fh, unlock=False); locked = True
        if locked: _ensure_scratch_clone(common_git_dir, wt, timeout); yield wt
        else:
            with tempfile.TemporaryDirectory(prefix="iso-", dir=str(root), ignore_cleanup_errors=True) as tmp_dir: _ensure_scratch_clone(common_git_dir, iso_wt := Path(tmp_dir) / "wt", timeout); yield iso_wt
    finally:
        for fn in ((lambda: _lock_file_nb(lock_fh, unlock=True)) if locked else None, lock_fh.close):
            with contextlib.suppress(OSError): (fn() if fn else None)
def _sync_union_worktree(wt: Path, union_tree: str, deadline: float) -> None:
    if not (prev := stamp.read_text(encoding="utf-8").strip() if (stamp := wt / ".git" / "pregate_last_tree").is_file() else "") and not any(p.name != ".git" for p in wt.iterdir()): _git_text(wt, "read-tree", "--reset", "-u", union_tree, timeout=_rem(deadline)); stamp.write_text(union_tree + "\n", encoding="utf-8"); return
    _git_text(wt, "clean", "-fdxq", timeout=_rem(deadline)); clean_tracked = _git_text(wt, "diff-files", "--quiet", check=False, timeout=_rem(deadline)).returncode == 0
    if prev and _HEX40_RE.match(prev) and clean_tracked and ((prev == union_tree and any(p.name != ".git" for p in wt.iterdir())) or (_git_text(wt, "read-tree", "-m", "-u", prev, union_tree, check=False, timeout=_rem(deadline)).returncode == 0 and bool(stamp.write_text(union_tree + "\n", encoding="utf-8")))): return
    _git_text(wt, "read-tree", "--reset", "-u", union_tree, timeout=_rem(deadline)); (_git_text(wt, "checkout-index", "-f", "-a", timeout=_rem(deadline)) if not clean_tracked else None); stamp.write_text(union_tree + "\n", encoding="utf-8")
def _merge_heads(wt: Path, base_sha: str, base_tree: str, heads: list[tuple[str, str]], deadline: float) -> tuple[str | None, list[dict[str, str]], dict[str, Any] | None]:
    current_commit, current_tree, merged = base_sha, base_tree, []
    for idx, (label, head_sha) in enumerate(heads):
        proc = _git_bytes(wt, "merge-tree", "--write-tree", "--name-only", "-z", current_commit, head_sha, check=False, timeout=_rem(deadline), extra_env={"GIT_ATTR_SOURCE": current_commit})
        if proc.returncode == 1: parts = [p.decode("utf-8", errors="replace") for p in proc.stdout.partition(b"\x00\x00")[0].split(b"\x00") if p]; return None, merged, {"conflict_head": label, "conflict_head_sha": head_sha, "conflicting_files": sorted(set(parts[1:])) if len(parts) > 1 else []}
        if proc.returncode != 0: raise PregateError(f"git merge-tree failed ({proc.returncode}) on {label}={head_sha}: {proc.stderr.decode('utf-8', errors='replace').strip()}")
        if not _HEX40_RE.match(tree_oid := proc.stdout.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()): raise PregateError(f"unexpected merge-tree output on {label}: {tree_oid!r}")
        if idx + 1 < len(heads): current_commit = _git_text(wt, "commit-tree", tree_oid, "-p", current_commit, "-p", head_sha, "-m", f"pregate {label}", timeout=_rem(deadline)).stdout.strip()
        current_tree = tree_oid; merged.append({"label": label, "sha": head_sha, "tree": tree_oid})
    return current_tree, merged, None
def _is_pytest_file(rel: str) -> bool: return rel.endswith(".py") and not rel.startswith("tools/mutants/") and ((fn := rel.rsplit("/", 1)[-1]).startswith("test_") or fn.endswith("_test.py"))
def _is_conftest(rel: str) -> bool: return rel == "conftest.py" or rel.endswith("/conftest.py")
def _conftest_applies(cf_rel: str, rel: str) -> bool: return cf_rel == "conftest.py" or rel.startswith(cf_rel.removesuffix("conftest.py"))
def _rel_to_mod(rel: str) -> str: return mod[:-9] if (mod := rel[:-3].replace("/", ".")).endswith(".__init__") else mod
def _rel_pkg(rel: str) -> str: return rel.rsplit("/", 1)[0].replace("/", ".") if "/" in rel else ""
def _resolve_rel_mod(pkg: str, level: int, rest: str) -> str | None: return None if level - 1 > len(parts := pkg.split(".") if pkg else []) else ".".join([*parts[: len(parts) - (level - 1)], *([rest] if rest else [])])
def _parse_diff_signals(wt: Path, base_sha: str, union_tree: str, deadline: float) -> tuple[dict[str, set[int]], dict[str, set[int]], dict[str, set[str]], dict[str, set[str]]]:
    # The patch's shape is fixed on the command line, which beats any git config a seat holds: no colour codes
    # around the headers and the a/ b/ prefixes the parser reads (REFACTOR-QUEUE A2, kxxx's review of #645:
    # color.ui=always and diff.srcPrefix hid a breaking change as diff.noprefix did).
    proc = _git_text(wt, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--inter-hunk-context=0", "-U0", base_sha, union_tree, "--", "*.py", timeout=_rem(deadline)); old_lines, new_lines, diff_syms, def_syms, cur_path, seen, in_hunks, o_rem, n_rem = {}, {}, {}, {}, None, set(), False, 0, 0
    # WHERE a line sits decides what it is (nxxxxx's review of #652). A file's "--- a/" and "+++ b/" headers come between
    # its "diff --git" line and its first hunk; after that a line is a hunk header or hunk body, and the hunk header says
    # how many body lines follow. So an added docstring line "++ b/fake.py", which the patch prints as "+++ b/fake.py",
    # stays body and cannot move the hunks after it to another file. The split is on "\n" only: git ends every patch
    # line with it, and splitlines() would also cut a body line at a form feed, which loses the count. With
    # --inter-hunk-context=0 a seat's diff.interHunkContext cannot fuse hunks and put unchanged lines into them.
    for line in proc.stdout.split("\n"):
        if o_rem or n_rem:
            if line.startswith("\\"): continue  # "\ No newline at end of file" is not one of the counted lines
            if (kind := line[:1]) == "-" and o_rem: o_rem -= 1
            elif kind == "+" and n_rem: n_rem -= 1
            elif kind == " " and o_rem and n_rem: o_rem, n_rem = o_rem - 1, n_rem - 1
            else: raise PregateError(f"the patch body of {cur_path or 'a file with no header'} does not match its hunk header; refusing rather than guessing which file its lines belong to")
            if not cur_path or kind == " ": continue
            bucket = diff_syms.setdefault(cur_path, set()); (bucket.add(dm.group(1)) or def_syms.setdefault(cur_path, set()).add(dm.group(1)) if (dm := _DIFF_DEF_RE.match(line)) else None)
            if not _is_pytest_file(cur_path): bucket.update(tok for tok in _DIFF_TOKEN_RE.findall(line) if tok.lower() not in _IGNORED_STR_TOKENS and not tok.startswith(("apps/", "tools/", "tests/")))
        elif line.startswith("diff --git "): cur_path, in_hunks = None, False
        elif line.startswith("@@ ") and (m := _HUNK_RE.match(line)):
            o_start, o_cnt, n_start, n_cnt = int(m.group(1)), int(m.group(2) or "1"), int(m.group(3)), int(m.group(4) or "1"); in_hunks, o_rem, n_rem = True, o_cnt, n_cnt
            if cur_path: (old_lines.setdefault(cur_path, set()).update(range(o_start, o_start + o_cnt)) if o_cnt > 0 else None); new_lines.setdefault(cur_path, set()).update(range(n_start, n_start + n_cnt) if n_cnt > 0 else [max(1, n_start)])
        elif not in_hunks and line.startswith(("--- a/", "+++ b/")): cur_path = line[6:].strip(); seen.add(cur_path)
    if o_rem or n_rem: raise PregateError("the patch ends inside a hunk; refusing rather than reading a cut patch")
    # Fail closed on a patch the parser could not read: every .py file whose CONTENT changed (numstat, which no
    # display setting shapes; a mode-only change has no hunk and is not listed) must have had a file header, or
    # a setting nobody thought of would again turn a breaking change into a pass.
    numstat = _git_bytes(wt, "diff", "--numstat", "--no-renames", "-z", base_sha, union_tree, "--", "*.py", timeout=_rem(deadline)).stdout.decode("utf-8", errors="replace")
    content_changed = {rec.split("\t", 2)[2] for rec in numstat.split("\x00") if rec.count("\t") >= 2 and rec.split("\t", 2)[:2] != ["0", "0"]}
    if (unread := sorted(content_changed - seen)): raise PregateError(f"the patch parse found no file header for {len(unread)} changed .py file(s) ({', '.join(unread[:5])}); refusing rather than reading the change as none")
    return old_lines, new_lines, diff_syms, def_syms
def _stmt_names(n: ast.AST, touched: set[int] | None = None) -> list[str]:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): return [n.name, *([nm for m in n.body if touched is None or any(_node_start(m) <= ln <= getattr(m, "end_lineno", 0) for ln in touched) for nm in _stmt_names(m, touched) if not nm.startswith("__")] if isinstance(n, ast.ClassDef) else [])]
    if isinstance(n, (ast.Import, ast.ImportFrom)): return [a.asname or a.name.split(".", 1)[0] for a in n.names if a.name != "*"]
    if hasattr(ast, "TypeAlias") and isinstance(n, ast.TypeAlias): return [n.name.id]
    if isinstance(n, (ast.Assign, ast.Delete, ast.AnnAssign, ast.AugAssign)): return [x.id for t in (n.targets if isinstance(n, (ast.Assign, ast.Delete)) else [n.target]) for x in ast.walk(t) if isinstance(x, ast.Name)]
    return [nm for c in ast.iter_child_nodes(n) for nm in _stmt_names(c, touched)] if (isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler)) or (hasattr(ast, "TryStar") and isinstance(n, ast.TryStar))) else []
def _parse_mod_info(src: str, tree: ast.Module | None = None) -> tuple[ast.Module, list[ast.AST], list[tuple[set[str], list[str], bool]]] | None:
    with contextlib.suppress(SyntaxError):
        tree, lines, imps, body_info = (tree if tree is not None else ast.parse(src)), src.splitlines(), [], []
        for n in (s for s in tree.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))):
            if isinstance(n, (ast.Import, ast.ImportFrom)): imps.append(n); continue
            if "import" in (seg := _slice_node(lines, n)): imps.extend(x for x in ast.walk(n) if isinstance(x, (ast.Import, ast.ImportFrom)))
            body_info.append((set(_WORD_TOK_RE.findall(seg)) | set(_DIFF_TOKEN_RE.findall(seg)), sn := _stmt_names(n), not sn and _has_side_effect([n])))
        return tree, imps, body_info
    return None
def _has_side_effect(stmts: list[ast.stmt]) -> bool: return any((isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)) or (not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)) and (not isinstance(n, (ast.Assign, ast.AnnAssign)) or any(isinstance(x, (ast.Call, ast.Attribute, ast.Subscript)) for x in [*(n.targets if isinstance(n, ast.Assign) else [n.target]), *(ast.walk(n.value) if n.value is not None else ())]))) for n in stmts)
def _extract_changed_symbols(src: str, touched: set[int], pkg: str = "", parsed: Any = None) -> tuple[set[str], bool]:
    if not touched or (info := parsed if parsed is not None else _parse_mod_info(src)) is None: return set(), bool(touched)
    hit = [n for n in info[0].body if not touched.isdisjoint(range(_node_start(n), getattr(n, "end_lineno", 0) + 1))]; seed = {nm for n in hit for nm in _stmt_names(n, touched)}; cs = _caller_defs(src, seed, pkg=pkg, parsed=info); return (seed | cs) - {"__top_se__"}, _has_side_effect(hit) or ("__top_se__" in cs)
def _worker_base_env() -> dict[str, str]:
    env = {**{k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTEST_", "PYTHON")) and k.upper() not in ("DJANGO_SETTINGS_MODULE", "MESHY_TEMPLATE_DSN", "MESHY_TEST_DSN", "MESHY_TEST_DSN_EXPLICIT", "MESHY_DSN", "DATABASE_URL", "PREGATE_SKIP_CONFTESTS")}, **{k: v for k, v in os.environ.items() if k.upper() in _PYTHON_KEPT}, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "GIT_CONFIG_NOSYSTEM": "1", "MESHYCHAT_DPOP_ENFORCE": "false", "MESHY_SURFACES": "all", "MESHY_TEST_DSN_EXPLICIT": "", "MESHY_DSN": _NO_DB_DSN, "MESHY_TEST_DSN": _NO_DB_DSN, "DATABASE_URL": _NO_DB_DSN}
    return {**env, "PATH": f"{_GIT_FAST_DIR}{os.pathsep}{env.get('PATH', '')}"} if _GIT_FAST_DIR else env
def _scan_raw_imports(src: str, pkg: str = "", rel: str = "", wenv_map: dict[str, str] | None = None, tree_out: dict[str, ast.Module] | None = None, mod: str = "") -> list[str]:
    out: set[str] = {m.group(1) for m in _DOTTED_STR_RE.finditer(src)} | ({f"__litpy__:{m.group(1).replace(chr(92), '/')}" for m in _PY_PATH_RE.finditer(src)} if ".py" in src else set()); has_dyn, has_em, em_tmp = any(k in src for k in _DYN_LOADERS), False, set()
    if not has_dyn and (any(w in src for w in _ENV_WORDS) or "getattr" in src):
        if _ENV_ESC_RE.search(src): has_em = True
        elif any(w in src for w in _ENV_WORDS):
            def _sub_em(m: re.Match[str]) -> str:
                if sum((s0 := m.group(0)).count(w) for w in (*_ENV_WORDS, "getattr")) > 1: return s0
                (em_tmp.add(f"__emut__:{_ek(k)}") if (k := m.group(1) or m.group(2) or m.group(3)) else None); return " "
            has_em = any(any(w in ln for w in _ENV_WORDS) and not ln.lstrip().startswith("#") and bool(_ENV_TOK_RE.search(_SAFE_LN_RE.sub(_sub_em, ln))) for ln in src.splitlines())
    if not (has_dyn or has_em): out.update(em_tmp)
    if "import" not in src and not has_dyn and not has_em: return sorted(out)
    if not (has_dyn or has_em or ("\\" in src and _CONT_IMP_RE.search(src)) or _INLINE_IMP_RE.search(src) or _PAREN_CMT_RE.search(src)):
        for m in _IMPORT_RE.finditer(src):
            if fm := m.group(1):
                if (im := _resolve_rel_mod(pkg, lvl, rest) if (lvl := len(fm) - len(rest := fm.lstrip("."))) else rest) is not None:
                    (out.add(im) if im else None); raw = ((_COMMENT_RE.sub("", m.group(2)) if "#" in m.group(2) else m.group(2)).replace("\\", " ")) if m.group(2) is not None else (m.group(3) or "")
                    out.update((f"{im}.{t[0]}" if im else t[0]) for p in raw.split(",") if (t := p.strip().split()) and t[0] != "*")
            elif im := m.group(4): out.update(t[0] for p in im.split(",") if (t := p.strip().split()))
        return sorted(out)
    try: tree = tree_out[mod] if (tree_out is not None and mod in tree_out) else ast.parse(src)
    except SyntaxError:
        (out.add("__udyn__") if has_dyn else None); (out.add("__emut__:*") if has_em else None); [out.add(m.group(1)) if (m.group(1) and not m.group(1).startswith(".")) else (out.update(tok for p in m.group(4).split(",") if (tok := p.strip().split(" as ", 1)[0].strip())) if m.group(4) else None) for m in _IMPORT_RE.finditer(src)]; return sorted(out)
    if tree_out is not None and mod: tree_out[mod] = tree
    nodes, pmap = _walk_pmap(tree); amap, mod_vmap, attr_vmap, fn_calls, env_mut, called_ids, dcall_ids, alias_ids, glob_ns, load_ns, os_al, path_al, pjoin_al, env_al, ge_al, pe_al, env_wild, _unk, env_binds, act_n = {}, {}, {}, {}, set(), set(), set(), set(), set(), set(), {"os", "posix", "nt"}, {"os.path", "posixpath", "ntpath"}, set(), {"environ", "environb"}, {"getenv", "getenvb"}, {"putenv", "unsetenv"}, False, ast.AST(), [], []
    def _is_env(x: ast.AST | None) -> bool: return (isinstance(x, ast.Name) and x.id in env_al) or (isinstance(x, ast.Attribute) and x.attr in ("environ", "environb") and isinstance(x.value, ast.Name) and x.value.id in os_al)
    def _rec_em(k_node: ast.AST | None) -> None:
        nonlocal env_wild
        if isinstance(k_node, ast.Constant) and isinstance(k_node.value, (str, bytes)): ek = _ek(k_node.value.decode("utf-8", errors="replace") if isinstance(k_node.value, bytes) else k_node.value); env_mut.add(ek); out.add(f"__emut__:{ek}")
        else: env_wild = True; out.add("__emut__:*")
    def _rec_map(m_node: ast.AST | None, kws: list[ast.keyword] | tuple[ast.keyword, ...] = ()) -> None:
        (None if m_node is None else ([_rec_em(k) for k in m_node.keys] if isinstance(m_node, ast.Dict) else _rec_em(None))); [_rec_em(ast.Constant(kw.arg) if kw.arg else None) for kw in kws]
    for n in nodes:
        if isinstance(n, ast.Import): out.update(a.name for a in n.names); os_al.update(a.asname or a.name for a in n.names if a.name in ("os", "posix", "nt")); path_al.update(a.asname or a.name for a in n.names if a.name in ("os.path", "posixpath", "ntpath"))
        elif isinstance(n, ast.ImportFrom):
            if (im := _resolve_rel_mod(pkg, n.level, n.module or "") if n.level else (n.module or "")) is not None:
                (out.add(im) if im else None); out.update((f"{im}.{a.name}" if im else a.name) for a in n.names if a.name != "*")
                if im in ("os", "posix", "nt"): env_al.update(a.asname or a.name for a in n.names if a.name in ("environ", "environb")); ge_al.update(a.asname or a.name for a in n.names if a.name in ("getenv", "getenvb")); pe_al.update(a.asname or a.name for a in n.names if a.name in ("putenv", "unsetenv")); path_al.update(a.asname or a.name for a in n.names if a.name == "path")
                elif im in ("os.path", "posixpath", "ntpath"): pjoin_al.update(a.asname or a.name for a in n.names if a.name == "join")
            amap.update({a.asname or a.name: a.name for a in n.names if a.name in _DYN_LOADERS})
        else: (load_ns.add(n.id) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) else None); (called_ids.add(id(n.func)) or (dcall_ids.add(id(n.func)) if isinstance(n.func, ast.Name) else None) if isinstance(n, ast.Call) else None); (env_binds.append(n) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else None); (act_n.append(n) if isinstance(n, (ast.Global, ast.Nonlocal, ast.Call, ast.Assign, ast.Subscript, ast.AugAssign, ast.Attribute, ast.Name)) else None)
    for _ in range(2):
        for n in env_binds:
            if isinstance(n, ast.Assign): (env_al.update(t.id for t in n.targets if isinstance(t, ast.Name)) if _is_env(n.value) else None); fr = n.value.id if isinstance(n.value, ast.Name) else getattr(n.value, "attr", ""); [amap.__setitem__(t.id, amap.get(fr, fr)) for t in n.targets if isinstance(t, ast.Name) and amap.get(fr, fr) in _DYN_LOADERS]
            elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and isinstance(n.target, ast.Name) and n.value is not None: (env_al.add(n.target.id) if _is_env(n.value) else None); fr = n.value.id if isinstance(n.value, ast.Name) else getattr(n.value, "attr", ""); (amap.__setitem__(n.target.id, amap.get(fr, fr)) if amap.get(fr, fr) in _DYN_LOADERS else None)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                pa_l = n.args.posonlyargs + n.args.args; [env_al.add(pa.arg) for pa, dv in [*zip(pa_l[-len(n.args.defaults):] if n.args.defaults else (), n.args.defaults), *((a, d) for a, d in zip(n.args.kwonlyargs, n.args.kw_defaults) if d is not None)] if _is_env(dv)]
    for n in act_n:
        if isinstance(n, (ast.Global, ast.Nonlocal)): glob_ns.update(n.names)
        elif isinstance(n, ast.Call):
            cattr, fval, fid, kw_c = getattr(n.func, "attr", ""), getattr(n.func, "value", None), getattr(n.func, "id", ""), {k.arg: k.value for k in n.keywords if k.arg}; has_st = any(isinstance(a, ast.Starred) for a in n.args) or any(k.arg is None for k in n.keywords)
            if isinstance(n.func, ast.Name): fn_calls.setdefault(fid, []).append(n)
            if cattr in ("setenv", "delenv", "putenv", "unsetenv") or fid in pe_al or (cattr in ("setdefault", "pop") and _is_env(fval)): _rec_em(None if has_st else (n.args[0] if n.args else (kw_c.get("name") or kw_c.get("key"))))
            elif cattr in ("update", "clear", "popitem") and _is_env(fval): _rec_em(None) if (cattr != "update" or len(n.args) > 1 or any(isinstance(a, ast.Starred) for a in n.args)) else _rec_map(n.args[0] if n.args else None, n.keywords)
            elif cattr == "dict" and (getattr(fval, "id", "") == "patch" or getattr(fval, "attr", "") == "patch") and (_is_env(in_d := (n.args[0] if n.args else kw_c.get("in_dict"))) or (isinstance(in_d, ast.Constant) and in_d.value in ("os.environ", "os.environb"))):
                if any(isinstance(a, ast.Starred) for a in n.args) or len(n.args) > 3 or (len(n.args) >= 3 and not (isinstance(n.args[2], ast.Constant) and n.args[2].value is False)) or ("clear" in kw_c and not (isinstance(kw_c["clear"], ast.Constant) and kw_c["clear"].value is False)): _rec_em(None)
                _rec_map(n.args[1] if len(n.args) >= 2 else kw_c.get("values"), [k for k in n.keywords if k.arg not in ("in_dict", "values", "clear")])
            elif fid == "getattr" and len(n.args) >= 2 and (_is_env(n.args[0]) or (isinstance(n.args[0], ast.Name) and n.args[0].id in os_al)) and (not (isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)) or n.args[1].value in ("update", "setdefault", "pop", "popitem", "clear", "putenv", "unsetenv", "environ", "environb")): _rec_em(None)
            elif cattr == "add_argument" and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                dest = kw_c["dest"].value if isinstance(kw_c.get("dest"), ast.Constant) and isinstance(kw_c["dest"].value, str) else n.args[0].value.lstrip("-").replace("-", "_")
                if dest and "default" in kw_c: attr_vmap.setdefault(dest, []).append(kw_c["default"])
        elif isinstance(n, ast.Assign) and all(isinstance(t, ast.Name) for t in n.targets): alias_ids.add(id(n.value))
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and isinstance(n.target, ast.Name) and n.value is not None: alias_ids.add(id(n.value))
        elif isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)) and _is_env(n.value): _rec_em(n.slice)
        elif isinstance(n, ast.AugAssign) and _is_env(n.target): _rec_map(n.value)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)): attr_vmap.setdefault(n.attr, []).append(_unk)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and id(n) not in called_ids and ((n.attr in ("update", "setdefault", "pop", "popitem", "clear") and _is_env(n.value)) or n.attr in ("setenv", "delenv", "putenv", "unsetenv")): _rec_em(None)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and id(n) not in called_ids and n.id in pe_al: _rec_em(None)
    if not has_dyn: return sorted(out)
    wenv_map = {_ek(k): v for k, v in _worker_base_env().items() if k.upper() not in _PER_SHARD_ENV} if wenv_map is None else wenv_map; (out.add("__skip_env__") if ("skipUnless" in src and "environ.get" in src) else None)
    has_s, bad_s, non_s, esc_ids, adj_a, bind_cnt, bad_fn, dyn_ga = set(), set(), set(), set(), {}, {}, set(), False
    def _bc(nm: str) -> None: bind_cnt[nm] = bind_cnt.get(nm, 0) + 1
    for n in nodes:
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)): _bc(n.id)
        if isinstance(n, ast.Assign): ((has_s.update(t.id for t in n.targets if isinstance(t, ast.Name)), (bad_s.update(t.id for t in n.targets if isinstance(t, ast.Name)) if any(k in n.value.value for k in (*_DYN_LOADERS, "getattr")) else None)) if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str) and all(isinstance(t, ast.Name) for t in n.targets) else non_s.update(x.id for t in n.targets for x in _walk_pmap(t)[0] if isinstance(x, ast.Name)))
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)): (has_s.add(n.target.id) if isinstance(n.target, ast.Name) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str) else non_s.update(x.id for x in _walk_pmap(n.target)[0] if isinstance(x, ast.Name)))
        elif isinstance(n, ast.AugAssign): non_s.update(x.id for x in _walk_pmap(n.target)[0] if isinstance(x, ast.Name)); (esc_ids.add(n.target.id) if isinstance(n.target, ast.Name) else None)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): non_s.add(n.name); _bc(n.name); (bad_fn.add(n.name) if getattr(n, "decorator_list", ()) or isinstance(pmap.get(n), ast.ClassDef) else None)
        elif isinstance(n, ast.arg): non_s.add(n.arg); _bc(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            (out.add("__udyn__") if any(a.name == "*" for a in n.names) else None); [non_s.add(nm) or _bc(nm) for a in n.names if a.name != "*" for nm in (a.asname or a.name.split(".", 1)[0],)]
            if isinstance(n, ast.ImportFrom) and any(a.name in ("getattr", "attrgetter", "eval", "exec") for a in n.names): dyn_ga = True
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)): non_s.update(x.id for x in _walk_pmap(n.target)[0] if isinstance(x, ast.Name))
        elif isinstance(n, ast.withitem) and n.optional_vars is not None: non_s.update(x.id for x in _walk_pmap(n.optional_vars)[0] if isinstance(x, ast.Name))
        elif isinstance(n, ast.Delete): non_s.update(x.id for t in n.targets for x in _walk_pmap(t)[0] if isinstance(x, ast.Name))
        elif isinstance(n, ast.ExceptHandler) and n.name: non_s.add(n.name); _bc(n.name)
        elif hasattr(ast, "TypeAlias") and isinstance(n, ast.TypeAlias) and isinstance(n.name, ast.Name): non_s.add(n.name.id)
        elif (nm := getattr(n, "name", None) or getattr(n, "rest", None)) and (type(n).__name__.startswith("Match") or type(n).__name__ in ("TypeVar", "ParamSpec", "TypeVarTuple")): non_s.add(nm); _bc(nm)
    bound_n, str_v = has_s | non_s, has_s - non_s; pure_bi = {"len", "tuple", "sorted", "reversed", "enumerate", "iter", "bool"} - bound_n
    def _lk_a(a: str, b: str) -> None: adj_a.setdefault(a, set()).add(b); adj_a.setdefault(b, set()).add(a)
    def _safe_p(ex: ast.AST, vid: str) -> bool:
        p = pmap.get(ex)
        if isinstance(p, ast.Assign) and ex is p.value and all(isinstance(t, ast.Name) for t in p.targets): [_lk_a(vid, t.id) for t in p.targets]; return True
        if isinstance(p, ast.AnnAssign) and ex is p.value and isinstance(p.target, ast.Name): _lk_a(vid, p.target.id); return True
        if isinstance(p, ast.NamedExpr) and ex is p.value and isinstance(p.target, ast.Name): _lk_a(vid, p.target.id); return _safe_p(p, p.target.id)
        if isinstance(p, (ast.Tuple, ast.List)) and isinstance(p.ctx, ast.Load) and isinstance(gp := pmap.get(p), ast.Assign) and p is gp.value and len(gp.targets) == 1 and isinstance(gt := gp.targets[0], (ast.Tuple, ast.List)) and len(gt.elts) == len(p.elts) and not any(isinstance(x, ast.Starred) for x in gt.elts + p.elts) and (ix := next((i for i, el in enumerate(p.elts) if el is ex), -1)) >= 0 and isinstance(gt.elts[ix], ast.Name): _lk_a(vid, gt.elts[ix].id); return True
        if isinstance(p, (ast.For, ast.AsyncFor, ast.comprehension)) and ex is p.iter: return True
        if isinstance(p, ast.Subscript) and ex is p.value and isinstance(p.ctx, ast.Load): return _safe_p(p, vid)
        if isinstance(p, ast.BinOp) and isinstance(p.op, ast.Add) and (isinstance(ot := (p.right if ex is p.left else p.left), (ast.List, ast.Name, ast.BinOp)) or (isinstance(ot, ast.Call) and isinstance(ot.func, ast.Name) and ot.func.id in pure_bi)): return _safe_p(p, vid)
        if isinstance(p, ast.Call):
            if isinstance(p.func, ast.Name) and p.func.id in pure_bi and len(p.args) == 1 and ex is p.args[0] and not p.keywords: return True
            if isinstance(p.func, ast.Name) and p.func.id == "__import__" and "__import__" not in bound_n and ((len(p.args) >= 4 and ex is p.args[3]) or any(k.arg == "fromlist" and k.value is ex for k in p.keywords)): return True
            if isinstance(p.func, ast.Attribute) and p.func.attr == "join" and not p.keywords and ((len(p.args) == 1 and ex is p.args[0] and ((isinstance(p.func.value, ast.Constant) and isinstance(p.func.value.value, str)) or (isinstance(p.func.value, ast.Name) and p.func.value.id in str_v))) or (len(p.args) == 2 and ex is p.args[1] and isinstance(p.func.value, ast.Name) and p.func.value.id == "str" and "str" not in bound_n)): return True
            return getattr(p.func, "attr", getattr(p.func, "id", "")) == "parametrize" and len(p.args) >= 2 and ex is p.args[1]
        return (isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not)) or (isinstance(p, ast.Compare) and all(isinstance(op, (ast.Is, ast.IsNot)) for op in p.ops))
    for n in nodes:
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if not n.args and not n.keywords and (n.func.id in ("locals", "vars") or (n.func.id == "globals" and not (isinstance(cp := pmap.get(n), ast.Call) and len(cp.args) >= 2 and cp.args[1] is n and amap.get(getattr(cp.func, "id", ""), getattr(cp.func, "id", "")) == "__import__"))): out.add("__udyn__")
            elif n.func.id in ("eval", "exec"): dyn_ga = True; (None if (n.args and not any(isinstance(a, ast.Starred) for a in n.args) and ((isinstance(a0 := n.args[0], ast.Constant) and isinstance(a0.value, str) and not any(k in a0.value for k in (*_DYN_LOADERS, "getattr"))) or (isinstance(a0, ast.Name) and a0.id in str_v and a0.id not in bad_s))) else out.add("__udyn__"))
            elif n.func.id == "getattr": bad_fn.add(n.args[1].value) if (2 <= len(n.args) <= 3 and not n.keywords and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)) else None; dyn_ga = dyn_ga or not (2 <= len(n.args) <= 3 and not n.keywords and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str))
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            (esc_ids.add(n.id) if not _safe_p(n, n.id) else None); (bad_fn.add(n.id) if id(n) not in dcall_ids else None)
            if n.id in ("getattr", "attrgetter", "eval", "exec") and id(n) not in dcall_ids: dyn_ga = True
        elif isinstance(n, ast.Attribute): bad_fn.add(n.attr); dyn_ga = dyn_ga or n.attr in ("getattr", "attrgetter", "__dict__", "__getattribute__")
        elif isinstance(n, (ast.List, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.DictComp)) and not _safe_p(n, f"#{id(n)}"): esc_ids.add(f"#{id(n)}")
    eq, _scp_ids, keep_ns = list(esc_ids), {}, (load_ns | glob_ns)
    while eq: [esc_ids.add(nb) or eq.append(nb) for nb in adj_a.get(eq.pop(), ()) if nb not in esc_ids]
    class _Scp(ast.AST):
        def __init__(self, e: ast.AST, vm: dict[str, list[ast.AST]], nc: frozenset[str], it: bool = False) -> None: self.e, self.vm, self.nc, self.it = e, vm, nc, it; self.sig = _scp_ids.setdefault((id(e), it, tuple((k, tuple(getattr(x, "sig", id(x)) for x in vm[k])) for k in sorted(vm)), tuple(sorted(nc))), -len(_scp_ids) - 1)
    def _wrap(v: ast.AST, vm: dict[str, list[ast.AST]], nc: set[str] | frozenset[str], it: bool = False) -> ast.AST:
        if isinstance(v, ast.Constant) and not it: return v
        if isinstance(v, _Scp): return _Scp(v.e, v.vm, v.nc, True) if (it and not v.it) else v
        fns = {x.id for x in _walk_pmap(v)[0] if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)} - {"__file__", "__package__"}; return _Scp(v, {k: list(vm[k]) for k in fns if k in vm}, frozenset(nc & fns), it) if (fns or it) else v
    def _add_v(cur: list[ast.AST], v: ast.AST) -> None: hv = getattr(v, "sig", id(v)); (cur.append(v) if not any(hv == getattr(x, "sig", id(x)) for x in cur) else None)
    def _bind_val(tgt: ast.AST, val: ast.AST | None, vm: dict[str, list[ast.AST]], nc: set[str] | None = None, gs: set[str] = set()) -> None:
        if isinstance(tgt, ast.Name): wv = _unk if val is None else _wrap(val, vm, nc or set()); _add_v(vm.setdefault(tgt.id, []), wv); (_add_v(mod_vmap.setdefault(tgt.id, []), wv) if tgt.id in gs else None); (nc.discard(tgt.id) if nc is not None and val is not None else None)
        elif isinstance(tgt, (ast.Tuple, ast.List)) and isinstance(val, (ast.Tuple, ast.List)) and len(tgt.elts) == len(val.elts) and not any(isinstance(x, ast.Starred) for x in tgt.elts + val.elts): [_bind_val(ti, vi, vm, nc, gs) for ti, vi in zip(tgt.elts, val.elts)]
        else: [_add_v(vm.setdefault(x.id, []), _unk) or (_add_v(mod_vmap.setdefault(x.id, []), _unk) if x.id in gs else None) for x in _walk_pmap(tgt)[0] if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del))]
    for n in tree.body:
        if isinstance(n, ast.Assign): [_bind_val(t, n.value, mod_vmap) for t in n.targets]
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.Delete)): (_bind_val(n.target, n.value, mod_vmap) if n.value is not None else None) if isinstance(n, ast.AnnAssign) else [_bind_val(t, None, mod_vmap) for t in ([n.target] if isinstance(n, ast.AugAssign) else n.targets)]
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): (_add_v(mod_vmap.setdefault(n.name, []), _unk) if n.name in keep_ns else None)
        elif isinstance(n, (ast.Import, ast.ImportFrom)): [_add_v(mod_vmap.setdefault(nm, []), _unk) for a in n.names if a.name != "*" and (nm := a.asname or a.name.split(".", 1)[0]) in keep_ns]
    if glob_ns: ok_g = {id(t) for gn in nodes if isinstance(gn, (ast.Assign, ast.AnnAssign)) and gn.value is not None for t in (gn.targets if isinstance(gn, ast.Assign) else [gn.target]) if isinstance(t, ast.Name) and t.id in glob_ns}; [_add_v(mod_vmap.setdefault(gn.id, []), _unk) for gn in nodes if isinstance(gn, ast.Name) and isinstance(gn.ctx, (ast.Store, ast.Del)) and gn.id in glob_ns and id(gn) not in ok_g]
    cand_fns, fcall_ids, rel_fids, call_envs, nxt_ce, fclosed_s, dyn_calls = set(), set(), set(), {}, {}, set(), []
    def _fn_lg(body: list[ast.stmt]) -> tuple[set[str], set[str]]:
        loc, gn, q = set(), set(), list(body)
        while q:
            x = q.pop()
            if isinstance(x, (ast.Global, ast.Nonlocal)): gn.update(x.names)
            elif isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)): loc.add(x.id)
            elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): loc.add(x.name); q.extend([*x.decorator_list, *getattr(x, "bases", ()), *getattr(x, "keywords", ()), *(x.args.defaults if hasattr(x, "args") else ()), *(d for d in (x.args.kw_defaults if hasattr(x, "args") else ()) if d is not None)])
            elif isinstance(x, (ast.Import, ast.ImportFrom)): loc.update(a.asname or a.name.split(".", 1)[0] for a in x.names if a.name != "*")
            elif isinstance(x, (ast.Lambda, ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)): q.extend([*(x.args.defaults if isinstance(x, ast.Lambda) else ()), *(d for d in (x.args.kw_defaults if isinstance(x, ast.Lambda) else ()) if d is not None), *(x.generators[0].iter for _ in (1,) if hasattr(x, "generators") and x.generators)]); (None if isinstance(x, ast.Lambda) else [loc.add(w.target.id) for w in _walk_pmap(x)[0] if isinstance(w, ast.NamedExpr) and isinstance(w.target, ast.Name)])
            else: (loc.add(x.name) if isinstance(x, ast.ExceptHandler) and x.name else (loc.add(nm) if (nm := getattr(x, "name", None) or getattr(x, "rest", None)) and type(x).__name__.startswith("Match") else None)); q.extend(ast.iter_child_nodes(x))
        return loc - gn, gn
    def _bind_for(tgt: ast.AST, it: ast.AST, vm: dict[str, list[ast.AST]], nc: set[str]) -> None:
        if isinstance(tgt, ast.Name): vm[tgt.id] = ([_wrap(el, vm, nc) for el in it.elts] if (isinstance(it, (ast.Tuple, ast.List, ast.Set)) and f"#{id(it)}" not in esc_ids and it.elts and not any(isinstance(x, ast.Starred) for x in it.elts)) else [_wrap(it, vm, nc, True)]); nc.discard(tgt.id)
        elif isinstance(tgt, (ast.Tuple, ast.List)) and isinstance(it, (ast.Tuple, ast.List)) and f"#{id(it)}" not in esc_ids and not any(isinstance(x, ast.Starred) for x in tgt.elts + it.elts):
            rows = [e.elts for e in it.elts if isinstance(e, (ast.Tuple, ast.List)) and f"#{id(e)}" not in esc_ids and len(e.elts) == len(tgt.elts)]; rem = len(rows) != len(it.elts) or not rows; wrows = [[_wrap(v, vm, nc) for v in r] for r in rows]
            [(vm.__setitem__(ti.id, [*(r[i] for r in wrows), *([_unk] if rem else [])]), nc.discard(ti.id)) if isinstance(ti, ast.Name) else _bind_val(ti, None, vm, nc) for i, ti in enumerate(tgt.elts)]
        else: [vm.__setitem__(x.id, [_unk]) or nc.discard(x.id) for x in _walk_pmap(tgt)[0] if isinstance(x, ast.Name)]
    def _scan_expr(e: ast.AST, vm: dict[str, list[ast.AST]], nc: set[str], pre: bool = False, gs: set[str] = set()) -> None:
        if isinstance(e, ast.NamedExpr): _scan_expr(e.value, vm, nc, pre, gs); _bind_val(e.target, e.value, vm, nc, gs); return
        if isinstance(e, ast.Lambda):
            _scan_expr(e.args, vm, nc, pre, gs); lv, lnc = {k: list(v) for k, v in vm.items()}, set(nc); [(lv.__setitem__(a.arg, [_unk]), lnc.discard(a.arg)) for a in (*e.args.posonlyargs, *e.args.args, *e.args.kwonlyargs, *([e.args.vararg] if e.args.vararg else ()), *([e.args.kwarg] if e.args.kwarg else ()))]; _scan_expr(e.body, lv, lnc, pre); return
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            cv, cnc = {k: list(v) for k, v in vm.items()}, set(nc); gts = {x.id for g in e.generators for x in _walk_pmap(g.target)[0] if isinstance(x, ast.Name)}; [(_scan_expr(g.iter, cv, cnc, pre, gs), _bind_for(g.target, g.iter, cv, cnc), [_scan_expr(iff, cv, cnc, pre, gs) for iff in g.ifs]) for g in e.generators]; [_scan_expr(c, cv, cnc, pre, gs) for c in ast.iter_child_nodes(e) if not isinstance(c, ast.comprehension)]; [_add_v(vm.setdefault(k, []), v) for w in _walk_pmap(e)[0] if isinstance(w, ast.NamedExpr) and isinstance(w.target, ast.Name) and (k := w.target.id) not in gts for v in cv.get(k, ())]; return
        if isinstance(e, ast.Call) and not pre:
            (nxt_ce.setdefault(id(e), []).append(({k: list(v) for k, v in vm.items()}, set(nc))) if id(e) in fcall_ids else None); rfn = e.func.id if isinstance(e.func, ast.Name) else (e.func.attr if isinstance(e.func, ast.Attribute) else (e.func.args[1].value if isinstance(e.func, ast.Call) and getattr(e.func.func, "id", "") == "getattr" and len(e.func.args) >= 2 and isinstance(e.func.args[1], ast.Constant) and isinstance(e.func.args[1].value, str) else ""))
            (dyn_calls.append((e, lfn, {k: list(v) for k, v in vm.items()}, set(nc))) if (lfn := amap.get(rfn, rfn)) in _DYN_LOADERS else None)
        elif isinstance(e, (ast.Name, ast.Attribute)) and isinstance(e.ctx, ast.Load) and id(e) not in called_ids and id(e) not in alias_ids and amap.get(tok := (e.id if isinstance(e, ast.Name) else e.attr), tok) in _DYN_LOADERS: out.add("__udyn__")
        [_scan_expr(c, vm, nc, pre, gs) for c in ast.iter_child_nodes(e) if not isinstance(c, ast.stmt)]
    def _walk_stmts(stmts: list[ast.stmt], base_vm: dict[str, list[ast.AST]], base_nc: set[str], pre: bool = False, enc_vm: dict[str, list[ast.AST]] | None = None, gs: set[str] = set()) -> tuple[dict[str, list[ast.AST]], set[str]]:
        vm, nc, fn_defs = {k: list(v) for k, v in base_vm.items()}, set(base_nc), []
        def _mrg(*brs: tuple[dict[str, list[ast.AST]], set[str]]) -> None: [nc.update(bnc) or [_add_v(vm.setdefault(k, []), v) for k, vs in bvm.items() for v in vs] for bvm, bnc in brs]
        for s in stmts:
            [_scan_expr(c, vm, nc, pre, gs) for c in ast.iter_child_nodes(s) if not isinstance(c, (ast.stmt, ast.withitem, ast.ExceptHandler)) and type(c).__name__ != "match_case"]
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)): (_add_v(vm.setdefault(s.name, []), _unk) if s.name in keep_ns else None); (fn_defs.append((s, {k: list(v) for k, v in vm.items()}, set(nc))) if (not pre and id(s) in rel_fids) else None); continue
            if isinstance(s, ast.ClassDef): (_add_v(vm.setdefault(s.name, []), _unk) if s.name in keep_ns else None); _walk_stmts(s.body, vm, nc, pre, enc_vm or vm); continue
            if isinstance(s, (ast.Import, ast.ImportFrom)): [_add_v(vm.setdefault(nm, []), _unk) for a in s.names if a.name != "*" and (nm := a.asname or a.name.split(".", 1)[0]) in keep_ns]; continue
            if hasattr(ast, "TypeAlias") and isinstance(s, ast.TypeAlias) and isinstance(s.name, ast.Name): (_add_v(vm.setdefault(s.name.id, []), _unk) if s.name.id in keep_ns else None); continue
            if isinstance(s, ast.Assign): [_bind_val(t, s.value, vm, nc, gs) for t in s.targets]
            elif isinstance(s, (ast.AnnAssign, ast.AugAssign, ast.Delete)): (_bind_val(s.target, s.value, vm, nc, gs) if s.value is not None else None) if isinstance(s, ast.AnnAssign) else [_bind_val(t, None, vm, nc, gs) for t in ([s.target] if isinstance(s, ast.AugAssign) else s.targets)]
            elif isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
                fv, fnc = {k: list(v) for k, v in vm.items()}, set(nc); (_bind_for(s.target, s.iter, fv, fnc) if isinstance(s, (ast.For, ast.AsyncFor)) else None)
                if not pre: bvm0, bnc0 = _walk_stmts(s.body, fv, fnc, True, enc_vm, gs); fnc.update(bnc0); [_add_v(fv.setdefault(k, []), v) for k, vs in bvm0.items() for v in vs]; (_scan_expr(s.test, fv, fnc, False, gs) if isinstance(s, ast.While) else None)
                _mrg(_walk_stmts(s.body, fv, fnc, pre, enc_vm, gs), _walk_stmts(s.orelse, vm, nc, pre, enc_vm, gs))
            elif isinstance(s, (ast.With, ast.AsyncWith)): wv, wnc = {k: list(v) for k, v in vm.items()}, set(nc); [(_scan_expr(it.context_expr, wv, wnc, pre, gs), (_bind_val(it.optional_vars, None, wv, wnc, gs) if it.optional_vars is not None else None)) for it in s.items]; _mrg(_walk_stmts(s.body, wv, wnc, pre, enc_vm, gs))
            elif isinstance(s, ast.If):
                iv, inc, nar = {k: list(v) for k, v in vm.items()}, set(nc), {}
                for tc in (s.test.values if (isinstance(s.test, ast.BoolOp) and isinstance(s.test.op, ast.And)) else [s.test]):
                    if isinstance(tc, ast.Call) and isinstance(tc.func, ast.Attribute) and tc.func.attr == "startswith" and isinstance(tc.func.value, ast.Name) and tc.args and isinstance(tc.args[0], ast.Constant) and isinstance(tc.args[0].value, str): nar[tc.func.value.id] = tok_c = ast.Constant(tc.args[0].value + "*"); iv[tc.func.value.id] = [tok_c]; inc.discard(tc.func.value.id)
                bvm, bnc = _walk_stmts(s.body, iv, inc, pre, enc_vm, gs); evm, enc = _walk_stmts(s.orelse, vm, nc, pre, enc_vm, gs); [bvm.__setitem__(k, [x for x in bvm[k] if x is not tok_c]) for k, tok_c in nar.items() if k in bvm]; _mrg((bvm, bnc), (evm, enc))
            elif hasattr(ast, "Match") and isinstance(s, ast.Match):
                brs = []; [(cv := {k: list(v) for k, v in vm.items()}, cnc := set(nc), [_add_v(cv.setdefault(nm, []), _unk) or cnc.discard(nm) for p in _walk_pmap(mc.pattern)[0] if (nm := getattr(p, "name", None) or getattr(p, "rest", None))], (_scan_expr(mc.guard, cv, cnc, pre, gs) if mc.guard else None), brs.append(_walk_stmts(mc.body, cv, cnc, pre, enc_vm, gs))) for mc in s.cases]; _mrg(*brs)
            else:
                brs = [_walk_stmts(sub, vm, nc, pre, enc_vm, gs) for attr in ("body", "orelse", "finalbody") if (sub := getattr(s, attr, None))]
                for h in getattr(s, "handlers", ()): hv, hnc = {k: list(v) for k, v in vm.items()}, set(nc); (_scan_expr(h.type, vm, nc, pre, gs) if h.type else None); (_add_v(hv.setdefault(h.name, []), _unk) or hnc.discard(h.name) if h.name else None); brs.append(_walk_stmts(h.body, hv, hnc, pre, enc_vm, gs))
                _mrg(*brs)
        lvm = enc_vm if enc_vm is not None else vm
        for fn_n, def_vm, def_nc in ([] if pre else fn_defs):
            pos_a, n_po = fn_n.args.posonlyargs + fn_n.args.args, len(fn_n.args.posonlyargs); kw_ok = {a.arg for a in fn_n.args.args + fn_n.args.kwonlyargs}
            p_defs = {k: _wrap(dv, def_vm, def_nc) for k, dv in [*zip([a.arg for a in (pos_a[-len(fn_n.args.defaults):] if fn_n.args.defaults else ())], fn_n.args.defaults), *((a.arg, d) for a, d in zip(fn_n.args.kwonlyargs, fn_n.args.kw_defaults) if d is not None)]}
            fn_loc, fn_gn = _fn_lg(fn_n.body); fv = {k: [*lvm.get(k, ()), *(x for x in mod_vmap.get(k, ()) if not any(getattr(x, "sig", id(x)) == getattr(y, "sig", id(y)) for y in lvm.get(k, ())))] for k in (set(lvm) | set(mod_vmap)) - fn_loc}; fnc, pb, pargs = set(), set(), [a for a in pos_a if a.arg not in ("self", "cls")]
            for d in fn_n.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "attr", getattr(d.func, "id", "")) == "parametrize" and len(d.args) >= 2 and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
                    pns = [p for p in re.split(r"[,\s]+", d.args[0].value.strip()) if p]
                    if len(pns) == 1: _bind_for(ast.Name(pns[0], ast.Store()), _wrap(d.args[1], def_vm, def_nc), fv, fnc); pb.add(pns[0])
                    elif len(pns) > 1 and isinstance(d.args[1], (ast.List, ast.Tuple)): [([_add_v(fv.setdefault(pn, []), _wrap(pv, def_vm, def_nc)) or pb.add(pn) for pn, pv in zip(pns, vr)] if len(vr) == len(pns) else None) for row in d.args[1].elts for vr in ((row.args if (isinstance(row, ast.Call) and getattr(row.func, "attr", "") == "param") else (row.elts if isinstance(row, (ast.List, ast.Tuple)) else ())),)]
            if fn_n.name in cand_fns and len(pargs) == len(pos_a) and (fcs := fn_calls.get(fn_n.name)) and all(id(c) in call_envs and not any(isinstance(a, ast.Starred) for a in c.args) and not any(k.arg is None for k in c.keywords) and (fn_n.args.vararg is not None or len(c.args) <= len(pos_a)) and (fn_n.args.kwarg is not None or all(k.arg in kw_ok for k in c.keywords)) and len({k.arg for k in c.keywords}) == len(c.keywords) and all(idx >= len(c.args) or idx < n_po for idx, pa in enumerate(pos_a) if any(k.arg == pa.arg for k in c.keywords)) for c in fcs):
                for idx, pa in enumerate(pos_a + fn_n.args.kwonlyargs):
                    cvs, ok_p = [], True
                    for c in fcs:
                        ae = c.args[idx] if idx < len(pos_a) and idx < len(c.args) else next((k.value for k in c.keywords if k.arg == pa.arg and idx >= n_po), None)
                        ([_add_v(cvs, _wrap(ae, c_vm, c_nc)) for c_vm, c_nc in call_envs[id(c)]] if ae is not None else (_add_v(cvs, p_defs[pa.arg]) if pa.arg in p_defs else None)); ok_p = ok_p and (ae is not None or pa.arg in p_defs)
                    if ok_p and cvs: fv[pa.arg] = cvs; pb.add(pa.arg)
                (fclosed_s.add(fn_n.name) if pb and pmap.get(fn_n) is tree else None)
            for ug in [*(a.arg for a in pargs + fn_n.args.kwonlyargs if a.arg not in pb), *([fn_n.args.vararg.arg] if fn_n.args.vararg else []), *([fn_n.args.kwarg.arg] if fn_n.args.kwarg else [])]: fv[ug] = [_unk, *([p_defs[ug]] if ug in p_defs else [])]
            _walk_stmts(fn_n.body, fv, fnc, False, None, fn_gn)
        return vm, nc
    def _eval_one(n: ast.Call, fn: str, vm: dict[str, list[ast.AST]], nc: set[str]) -> set[str]:
        toks: set[str] = set()
        def _seq_v(sn: ast.AST, sd: int = 0, cvm: dict[str, list[ast.AST]] = vm, cnc: set[str] | frozenset[str] = nc) -> list[list[ast.AST]] | None:
            if sd > 16: return None
            if isinstance(sn, _Scp): return None if sn.it else _seq_v(sn.e, sd + 1, sn.vm, sn.nc)
            if isinstance(sn, (ast.List, ast.Tuple)) and f"#{id(sn)}" not in esc_ids and not any(isinstance(x, ast.Starred) for x in sn.elts): return [[_wrap(el, cvm, cnc) for el in sn.elts]]
            if isinstance(sn, ast.BinOp) and isinstance(sn.op, ast.Add) and (ls := _seq_v(sn.left, sd + 1, cvm, cnc)) is not None and (rs := _seq_v(sn.right, sd + 1, cvm, cnc)) is not None and len(ls) * len(rs) <= 4: return [a + b for a in ls for b in rs]
            return rows if (isinstance(sn, ast.Name) and sn.id not in cnc and (sn.id in cvm or (sn.id in glob_ns and sn.id in mod_vmap)) and 0 < len(vs := [*cvm.get(sn.id, ()), *(x for x in (mod_vmap.get(sn.id, ()) if sn.id in glob_ns else ()) if not any(getattr(x, "sig", id(x)) == getattr(y, "sig", id(y)) for y in cvm.get(sn.id, ())))]) <= 4 and None not in (subs := [_seq_v(r, sd + 1, cvm, cnc) for r in vs]) and len(rows := [row for s in subs if s is not None for row in s]) <= 4) else None
        def _ev(e: ast.AST | None, d: int = 0, cvm: dict[str, list[ast.AST]] = vm, cnc: set[str] | frozenset[str] = nc) -> set[str]:
            if d > 18 or e is None: return set()
            if isinstance(e, _Scp): return _ev(e.e, d + 1, e.vm, e.nc)
            if isinstance(e, ast.Constant) and isinstance(e.value, str): return {e.value.replace("\\", "/")}
            if isinstance(e, ast.Name):
                if e.id in ("__file__", "__package__"): return {rel if e.id == "__file__" else pkg}
                return (set().union(*ps) if all(ps := [_ev(r, d + 1, cvm, cnc) for r in vs]) else (set().union(*ps) | {"*"})) if (e.id not in cnc and (e.id in cvm or (e.id in glob_ns and e.id in mod_vmap)) and 0 < len(vs := [*cvm.get(e.id, ()), *(x for x in (mod_vmap.get(e.id, ()) if e.id in glob_ns else ()) if not any(getattr(x, "sig", id(x)) == getattr(y, "sig", id(y)) for y in cvm.get(e.id, ())))]) <= 24) else set()
            if isinstance(e, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                if f"#{id(e)}" in esc_ids: return set()
                ps = [_ev(x, d + 1, cvm, cnc) for x in (e.keys if isinstance(e, ast.Dict) else e.elts)]; return set().union(*ps) if (ps and all(ps)) else (set().union(*ps) | ({"*"} if ps else set()))
            if isinstance(e, ast.IfExp): return (b | o) if (b := _ev(e.body, d + 1, cvm, cnc)) and (o := _ev(e.orelse, d + 1, cvm, cnc)) else set()
            if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) and f"#{id(e)}" not in esc_ids: cv, nc2 = {k: list(v) for k, v in cvm.items()}, set(cnc); [_bind_for(g.target, g.iter, cv, nc2) for g in e.generators]; return _ev(e.elt, d + 1, cv, nc2)
            if isinstance(e, ast.BoolOp) and isinstance(e.op, ast.Or):
                acc: set[str] = set()
                for v in e.values:
                    if not (sv := _ev(v, d + 1, cvm, cnc)): return acc | {"*"}
                    acc |= sv - {"__none__", ""}
                    if not (sv & {"__none__", ""}): return acc
                return acc or {"__none__"}
            if isinstance(e, ast.JoinedStr):
                pts = [""]
                for v in e.values:
                    sv = (_ev(v.value, d + 1, cvm, cnc) - {"__none__"}) if (isinstance(v, ast.FormattedValue) and v.format_spec is None and v.conversion in (-1, 115)) else ({v.value.replace("\\", "/")} if isinstance(v, ast.Constant) and isinstance(v.value, str) else set())
                    if len(pts) * len(cur := sv or {"*"}) > 24: return set()
                    pts = [re.sub(r"\*+", "*", p + c) for p in pts for c in cur]
                return js if (js := set(pts)) != {"*"} else set()
            if isinstance(e, ast.Call):
                cfn, kw_e, has_st_c = (e.func.attr if isinstance(e.func, ast.Attribute) else (e.func.id if isinstance(e.func, ast.Name) else "")), {k.arg: k.value for k in e.keywords if k.arg}, (any(isinstance(a, ast.Starred) for a in e.args) or any(k.arg is None for k in e.keywords))
                if (isinstance(e.func, ast.Name) and e.func.id in ge_al) or (isinstance(e.func, ast.Attribute) and ((cfn == "getenv" and isinstance(e.func.value, ast.Name) and e.func.value.id in os_al) or (cfn == "get" and _is_env(e.func.value)))):
                    if has_st_c or not (1 <= len(e.args) <= 2) or (len(e.args) == 2 and "default" in kw_e) or set(kw_e) - {"default"} or not (isinstance(e.args[0], ast.Constant) and isinstance(e.args[0].value, str)): return set()
                    ek = _ek(e.args[0].value); toks.add(f"__eread__:{ek}")
                    if env_wild or ek in env_mut or ek.upper() in _PER_SHARD_ENV: return set()
                    if ek in wenv_map: return {wenv_map[ek].replace("\\", "/")}
                    d_arg = e.args[1] if len(e.args) == 2 else kw_e.get("default"); return {"__none__"} if d_arg is None or (isinstance(d_arg, ast.Constant) and d_arg.value is None) else _ev(d_arg, d + 1, cvm, cnc)
                if has_st_c: return set()
                if cfn == "SourceFileLoader" and (len(e.args) >= 2 or "path" in kw_e) and set(kw_e) <= {"name", "path"}: return _ev(e.args[1] if len(e.args) >= 2 else kw_e["path"], d + 1, cvm, cnc)
                if cfn in ("str", "fspath", "abspath", "realpath", "normpath", "sorted") and len(e.args) == 1 and not kw_e and (cfn not in ("str", "sorted") or isinstance(e.func, ast.Name)): return _ev(e.args[0], d + 1, cvm, cnc)
                if cfn in ("resolve", "absolute", "expanduser") and isinstance(e.func, ast.Attribute) and not e.args and (not kw_e or (cfn == "resolve" and set(kw_e) <= {"strict"})): return _ev(e.func.value, d + 1, cvm, cnc)
                if cfn == "dirname" and len(e.args) == 1 and not kw_e: return {(posixpath.dirname(s) if "*" not in s else (f"{dp}/*" if (dp := posixpath.dirname(s).rstrip("/")) else "*")) for s in _ev(e.args[0], d + 1, cvm, cnc) - {"__none__", ""}}
                p_args = [e.func.value, *e.args] if (cfn == "joinpath" and isinstance(e.func, ast.Attribute)) else (list(e.args) if (cfn in ("Path", "PurePath", "PurePosixPath", "PureWindowsPath", "PosixPath", "WindowsPath") or (isinstance(e.func, ast.Name) and cfn in pjoin_al) or (cfn == "join" and isinstance(e.func, ast.Attribute) and ((df := _dotted_expr(e.func.value)) in path_al or any(df == f"{oa}.path" for oa in os_al)))) else None)
                if p_args is not None and p_args and not kw_e and not any(isinstance(a, (ast.List, ast.Tuple, ast.Set, ast.Dict)) for a in p_args):
                    vals = {""}
                    for a in p_args:
                        if len(vals) * len(nxt := _ev(a, d + 1, cvm, cnc) - {"__none__"} or {"*"}) > 24: return set()
                        vals = {re.sub(r"\*+", "*", f"{x.rstrip('/')}/{y.lstrip('/')}" if (x and y) else (x or y)) for x in vals for y in nxt}
                    return vals if vals != {"*"} else set()
                if cfn == "join" and isinstance(e.func, ast.Attribute) and not kw_e:
                    sep_n, seq_n = (e.func.value, e.args[0]) if len(e.args) == 1 else ((e.args[0], e.args[1]) if (isinstance(e.func.value, ast.Name) and e.func.value.id == "str" and len(e.args) == 2) else (None, None))
                    if sep_n is not None and seq_n is not None and (seps := _ev(sep_n, d + 1, cvm, cnc) - {"__none__"}) and (rows := _seq_v(seq_n, d + 1, cvm, cnc)) is not None:
                        j_out: set[str] = set()
                        for elts in rows:
                            if not elts: j_out.add(""); continue
                            acc = _ev(elts[0], d + 1, cvm, cnc) - {"__none__"} or {"*"}
                            for el in elts[1:]:
                                if len(acc) * len(seps) * len(nx := _ev(el, d + 1, cvm, cnc) - {"__none__"} or {"*"}) > 24: return set()
                                acc = {re.sub(r"\*+", "*", f"{a}{sp}{b}") for a in acc for sp in seps for b in nx}
                            j_out |= acc
                        return j_out if j_out != {"*"} else set()
                    return set()
            if isinstance(e, ast.Attribute):
                if e.attr == "parent": return {(posixpath.dirname(s) if "*" not in s else (f"{dp}/*" if (dp := posixpath.dirname(s).rstrip("/")) else "*")) for s in _ev(e.value, d + 1, cvm, cnc) - {"__none__", ""}}
                if e.attr in attr_vmap and 0 < len(attr_vmap[e.attr]) <= 6: return set().union(*ps) if all(ps := [_ev(r, d + 1, mod_vmap, set()) for r in attr_vmap[e.attr]]) else (set().union(*ps) | {"*"})
            if isinstance(e, ast.Subscript) and _is_env(e.value):
                if not (isinstance(e.slice, ast.Constant) and isinstance(e.slice.value, str)): return set()
                ek = _ek(e.slice.value); toks.add(f"__eread__:{ek}"); return {wenv_map[ek].replace("\\", "/")} if (not env_wild and ek not in env_mut and ek.upper() not in _PER_SHARD_ENV and ek in wenv_map) else set()
            if isinstance(e, ast.Subscript) and isinstance(e.value, ast.Attribute) and e.value.attr == "parents" and isinstance(e.slice, ast.Constant) and isinstance(e.slice.value, int) and 0 <= e.slice.value <= 6:
                sv = _ev(e.value.value, d + 1, cvm, cnc) - {"__none__", ""}; [sv := {(posixpath.dirname(s) if "*" not in s else (f"{dp}/*" if (dp := posixpath.dirname(s).rstrip("/")) else "*")) for s in sv} for _ in range(e.slice.value + 1)]; return sv
            if isinstance(e, ast.BinOp) and isinstance(e.op, (ast.Div, ast.Add)):
                if isinstance(e.op, ast.Add) and (isinstance(e.left, (ast.List, ast.Tuple)) or isinstance(e.right, (ast.List, ast.Tuple)) or _seq_v(e.left, d + 1, cvm, cnc) is not None or _seq_v(e.right, d + 1, cvm, cnc) is not None): l, r = _ev(e.left, d + 1, cvm, cnc), _ev(e.right, d + 1, cvm, cnc); return (l | r) if (l and r) else (l | r | {"*"})
                lv, rv = _ev(e.left, d + 1, cvm, cnc) - {"__none__"}, _ev(e.right, d + 1, cvm, cnc) - {"__none__"}
                if (lv or rv) and len(le := lv or {"*"}) * len(re_ := rv or {"*"}) <= 24: bs = {re.sub(r"\*+", "*", (f"{a.rstrip('/')}/{b.lstrip('/')}" if (a and b) else (a or b)) if isinstance(e.op, ast.Div) else (a + b)) for a in le for b in re_}; return bs if bs != {"*"} else set()
            return set()
        if any(isinstance(a, ast.Starred) for a in n.args) or any(k.arg is None for k in n.keywords): return toks | {"__udyn__"}
        kw = {k.arg: k.value for k in n.keywords if k.arg}; targ = (n.args[1] if len(n.args) >= 2 else (kw.get("location") or kw.get("loader") if fn == "spec_from_file_location" else kw.get("path"))) if fn in ("spec_from_file_location", "SourceFileLoader") else (n.args[0] if n.args else kw.get("path_name" if fn == "run_path" else ("mod_name" if fn == "run_module" else "name")))
        lvl_n, fl_arg, gl_arg = ((n.args[4] if len(n.args) >= 5 else kw.get("level")), (n.args[3] if len(n.args) >= 4 else kw.get("fromlist")), (n.args[1] if len(n.args) >= 2 else kw.get("globals"))) if fn == "__import__" else (None, None, None)
        if lvl_n is not None and not (isinstance(lvl_n, ast.Constant) and isinstance(lvl_n.value, int) and not isinstance(lvl_n.value, bool) and lvl_n.value >= 0): return toks | {"__udyn__"}
        imp_lvl, raw_v = (lvl_n.value if isinstance(lvl_n, ast.Constant) else 0), (_ev(targ) - {"__none__"})
        if (imp_lvl > 0 and gl_arg is not None and not ((isinstance(gl_arg, ast.Constant) and gl_arg.value is None) or (isinstance(gl_arg, ast.Call) and isinstance(gl_arg.func, ast.Name) and gl_arg.func.id == "globals" and not gl_arg.args and not gl_arg.keywords))) or not (vals := {("." * imp_lvl + v.lstrip(".")) if imp_lvl else v for v in (raw_v if imp_lvl else (raw_v - {""}))}): return toks | {"__udyn__"}
        if fn in _FILE_LOADERS: [toks.add(f"__path__:{v}" if "*" not in v else (f"__dir__:{dp}" if ("/" in (pf := v.split("*", 1)[0]) and (dp := _norm_rel(pf.rsplit("/", 1)[0])) and "*" not in dp) else (f"__fn__:{bn[:-3]}" if "*" not in (bn := v.rsplit("/", 1)[-1]) and bn.endswith(".py") and bn != "__init__.py" else "__udyn__"))) for v in vals]; return toks
        parg = (n.args[1] if len(n.args) >= 2 else kw.get("package")) if fn == "import_module" else None; pvals = (_ev(parg) - {"__none__"}) if parg is not None else ({pkg} if pkg else set())
        fl_empty = fl_arg is None or (isinstance(fl_arg, ast.Constant) and not fl_arg.value) or (isinstance(fl_arg, (ast.Tuple, ast.List, ast.Set)) and not fl_arg.elts); fl_v = set() if fl_empty else (_ev(fl_arg) - {"__none__", ""})
        def _emit_mod(bm: str) -> None:
            if "*" in bm: pf = bm.split("*", 1)[0]; mp = pf.rsplit(".", 1)[0] if "." in pf else ""; toks.add(f"__mpfx__:{mp}" if mp and "*" not in mp else "__udyn__"); return
            toks.add(f"__mod__:{bm}"); (None if fl_empty else (toks.add(f"__mpfx__:{bm}") if (not fl_v or "*" in fl_v) else [toks.add(f"__fl__:{sb}" if "*" not in (sb := f"{bm}.{fi}".strip(".")) else f"__mpfx__:{sb.split('*', 1)[0].rsplit('.', 1)[0] if '.' in sb.split('*', 1)[0] else bm}") for fi in fl_v]))
        [(_emit_mod(v) if not v.startswith(".") else (toks.add("__udyn__") if (not pvals or "*" in pvals) else [_emit_mod(rm) if (rm := _resolve_rel_mod(pv, len(v) - len(v.lstrip(".")), v.lstrip("."))) else toks.add("__udyn__") for pv in pvals])) for v in vals]; return toks
    def _run_rounds(fns: set[str], n_it: int) -> tuple[list[set[str]], set[int]]:
        nonlocal cand_fns, fcall_ids, call_envs, nxt_ce
        cand_fns, fcall_ids, call_envs, rel_toks = fns, {id(c) for fn in fns for c in fn_calls.get(fn, ())}, {}, (_DYN_LOADERS | set(amap) | fns); rel_fids.clear()
        for x in nodes:
            if (isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name in fns) or isinstance(x, (ast.Global, ast.Nonlocal)) or (isinstance(x, ast.Name) and x.id in rel_toks) or (isinstance(x, ast.Attribute) and x.attr in rel_toks) or (isinstance(x, ast.Constant) and x.value in _DYN_LOADERS):
                cur = x
                while cur is not None and id(cur) not in rel_fids: (rel_fids.add(id(cur)) if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)) else None); cur = pmap.get(cur)
        for _ in range(n_it): dyn_calls.clear(); fclosed_s.clear(); nxt_ce = {}; _walk_stmts(tree.body, mod_vmap, set(), False, None, glob_ns); call_envs = nxt_ce
        res = [_eval_one(*c) for c in dyn_calls]; return res, {id(c[0]) for c, ts in zip(dyn_calls, res) if "__udyn__" in ts}
    res, r_ud = _run_rounds(set(), 1)
    if r_ud and not dyn_ga:
        c_base = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("test_") and bind_cnt.get(n.name, 0) == 1 and n.name not in bad_fn]
        if d0 := {n.name for n in c_base if any(id(x) in r_ud for x in _walk_pmap(n)[0]) and fn_calls.get(n.name)}: res, r_ud = _run_rounds(d0, 2)
        if r_ud and d0 and (d1 := {n.name for n in c_base if n.name not in d0 and (n.args.posonlyargs or n.args.args or n.args.kwonlyargs) and fn_calls.get(n.name) and any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id in d0 and any(isinstance(y, ast.Name) for a in [*x.args, *(k.value for k in x.keywords)] for y in _walk_pmap(a)[0]) for x in _walk_pmap(n)[0])}): res, r_ud = _run_rounds(d0 | d1, 3)
    out.update(f"__fclosed__:{fn}" for fn in fclosed_s); [out.update(ts) for ts in res]; return sorted(out)
def _classify_test_module(src: str, rel: str, raw_imps: list[str] | tuple[str, ...] = (), top_roots: set[str] | frozenset[str] = frozenset({"tests"}), tree_fn: Any = None) -> tuple[set[str], str]:
    if not _is_conftest(rel) and '"""' not in (hdr := re.sub(r'\A\s*"""[\s\S]*?"""', "", src.split("\ndef ", 1)[0].split("\nclass ", 1)[0], count=1)) and "'''" not in hdr and _FAST_DB_RE.search(hdr): return set(), "fast_db"
    if not any(t in src for t in ("psycopg", "django", "asgi", "MESHY_TEST_DSN", "MESHY_TEMPLATE_DSN", "MESHY_BISCUIT_CONFORMANCE_BIN", "MESHY_LIVE_MODELS", "dbconn", "Connection", "pytest_plugins")) and ("TestCase" not in src or ("from unittest" not in src and "TestCase" not in re.sub(r"\bunittest(?:\.case)?\.TestCase\b", "", src))) and not _DB_FIX_RE.search(src): return {u for x in raw_imps if (u := x.split(":", 1)[1] if x.startswith(("__mod__:", "__fl__:")) else x) == "tests" or u.startswith("tests.")}, ""
    try:
        if (tree := tree_fn() if tree_fn is not None else ast.parse(src, filename=rel)) is None: return set(), ""
    except SyntaxError: return set(), ""
    lines, pkg, test_imports, amap, cls_bases, call_fns, reasons, uses_django_client = src.splitlines(), _rel_pkg(rel), set(), {}, [], [], [], False
    for node in _walk_pmap(tree)[0]:
        if isinstance(node, ast.Import):
            test_imports.update(a.name for a in node.names if a.name.startswith("tests.")); amap.update({a.asname or a.name.split(".", 1)[0]: a.name if a.asname else a.name.split(".", 1)[0] for a in node.names})
            (reasons.append("psycopg") if any(a.name == "psycopg" or a.name.startswith(("psycopg.", "psycopg_pool")) for a in node.names) else None); (reasons.append("meshychat.asgi") if any(a.name == "meshychat.asgi" for a in node.names) else None)
        elif isinstance(node, ast.ImportFrom):
            mod = (_resolve_rel_mod(pkg, node.level, node.module or "") or "") if node.level else (node.module or "")
            for a in node.names:
                if a.name == "*" and mod.startswith(("django.test", "django.contrib")): [amap.setdefault(db_b, f"{mod}.{db_b}") for db_b in _DB_TESTCASE_BASES]
                elif a.name == "*" and (mod == "unittest" or mod.startswith("unittest.")): amap["TestCase"] = f"{mod}.TestCase"
                elif a.name != "*": amap[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name
            (reasons.append("psycopg") if mod == "psycopg" or mod.startswith(("psycopg.", "psycopg_pool")) else None); uses_django_client = uses_django_client or (mod.startswith(("django.test", "django.contrib")) and any(a.name in ("Client", "AsyncClient") for a in node.names))
            if mod == "tests" or mod.startswith("tests."): test_imports.update(([mod] if mod != "tests" else []) + [f"{mod}.{a.name}" for a in node.names if a.name != "*"])
            elif mod == "meshychat.asgi" or (mod == "meshychat" and any(a.name == "asgi" for a in node.names)): reasons.append("meshychat.asgi")
        elif isinstance(node, ast.ClassDef): cls_bases.extend(node.bases)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            param_names, is_fixture = set(), False
            for d in node.decorator_list:
                t = d.func if isinstance(d, ast.Call) else d; attr = t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", "")
                if attr == "django_db": reasons.append("django_db")
                elif attr == "fixture": is_fixture = True
                elif attr == "parametrize" and isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str): param_names.update(p for p in re.split(r"[,\s]+", d.args[0].value.strip()) if p)
            if node.name.startswith("test_") or is_fixture: reasons.extend(f"fixture:{arg.arg}" for arg in node.args.args + node.args.kwonlyargs if arg.arg in _DB_FIXTURES and arg.arg not in param_names)
        elif isinstance(node, ast.Assign):
            t_ids = {t.id for t in node.targets if isinstance(t, ast.Name)}; (reasons.append("django_db") if "pytestmark" in t_ids and "django_db" in _slice_node(lines, node) else None); (test_imports.update(x.value for x in _walk_pmap(node.value)[0] if isinstance(x, ast.Constant) and isinstance(x.value, str) and " " not in x.value and "/" not in x.value) if "pytest_plugins" in t_ids else None)
        elif isinstance(node, ast.Call):
            (call_fns.append(d_fn) if (d_fn := _dotted_expr(node.func)) else None)
            if isinstance(node.func, ast.Attribute): fn, val_id = node.func, getattr(node.func.value, "id", ""); (reasons.append("psycopg.connect") if fn.attr == "connect" and val_id in ("psycopg", "Connection", "AsyncConnection") else (reasons.append(str(node.args[0].value)) if fn.attr in ("get", "pop") and node.args and isinstance(node.args[0], ast.Constant) and ((node.args[0].value == "MESHY_TEST_DSN" and rel != "tests/live_env.py") or node.args[0].value == "MESHY_TEMPLATE_DSN") else None))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str): (reasons.append("meshychat.asgi") if "meshychat.asgi" in node.value else (reasons.append("conformance_bin") if node.value == "MESHY_BISCUIT_CONFORMANCE_BIN" and rel != "tests/test_local_gate.py" else (test_imports.add(node.value) if node.value.startswith("tests.") and " " not in node.value and "/" not in node.value else None)))
    for d in (x for b in cls_bases if (x := _dotted_expr(b))): hd, _, tl = d.partition("."); res = (f"{amap[hd]}.{tl}" if tl else amap[hd]) if hd in amap else d; ((test_imports.add(bm) if (bm := res.rsplit(".", 1)[0]).split(".", 1)[0] in top_roots else reasons.append("django_testcase")) if (res.rsplit(".", 1)[-1] in _DB_TESTCASE_BASES and "." in res and not res.startswith("unittest.")) else None)
    for d in call_fns:
        hd, _, tl = d.partition("."); rc = (f"{amap[hd]}.{tl}" if tl else amap[hd]) if hd in amap else d
        if rc in ("django.test.Client", "django.test.AsyncClient", "django.test.client.Client", "django.test.client.AsyncClient"): uses_django_client = True
        elif rc.rsplit(".", 1)[-1] in ("dsn", "serving_connection", "owner_connection", "connect_as_role") and (rc.startswith("dbconn.") or ".dbconn." in rc): reasons.append(f"dbconn.{rc.rsplit('.', 1)[-1]}")
    (reasons.append("django_client_db") if uses_django_client and not any(k in src for k in ("serving_connection", "MESHY_MODEL_PROVIDERS", "/v1/health")) else None); (reasons.append("live_models") if "MESHY_LIVE_MODELS" in src and rel.endswith("_live.py") else None)
    [reasons.append(tag) for k, tag in ((('environ["MESHY_TEST_DSN"]', "environ['MESHY_TEST_DSN']"), "MESHY_TEST_DSN"), (('environ["MESHY_TEMPLATE_DSN"]', "environ['MESHY_TEMPLATE_DSN']"), "MESHY_TEMPLATE_DSN")) if any(x in src for x in k) and (tag != "MESHY_TEST_DSN" or rel != "tests/live_env.py")]; return test_imports, ",".join(sorted(set(reasons)))
def _ext_mod_installed(top: str) -> bool:
    if top in sys.stdlib_module_names: return True
    with contextlib.suppress(ImportError, ValueError): (_EXT_MOD_CACHE.__setitem__(top, importlib.util.find_spec(top) is not None) if top not in _EXT_MOD_CACHE else None)
    return _EXT_MOD_CACHE.setdefault(top, False)
def _index_python_imports(wt: Path, changed_files: list[str] | tuple[str, ...] = (), deadline: float | None = None, union_tree: str = "", tree_out: dict[str, ast.Module] | None = None) -> tuple[dict[str, str], dict[str, set[str]], set[tuple[str, str]], set[str], dict[str, set[str]], dict[str, str], dict[str, list[Any]], Path, dict[str, Any], Path, bool, str, set[str]]:
    cache_path, blob_path, old_cache, blobs = wt / ".git" / "pregate_scan_cache_v31.json", wt / ".git" / "pregate_blobs_v31.json", {}, {}; wenv_map = {_ek(k): v for k, v in _worker_base_env().items() if k.upper() not in _PER_SHARD_ENV}
    env_sig = hashlib.blake2b("\0".join(f"{k}={v}" for k, v in sorted(wenv_map.items())).encode("utf-8", errors="surrogateescape"), digest_size=6).hexdigest(); top_roots = {p.name[:-3] if p.name.endswith(".py") else p.name for p in wt.iterdir() if p.name not in _SKIP_DIRS} | {r.split("/", 1)[0].removesuffix(".py") for r in changed_files if r}; roots_sig = hashlib.blake2b(",".join(sorted(top_roots)).encode("utf-8"), digest_size=6).hexdigest()
    for cp, tgt in ((cache_path, old_cache), (blob_path, blobs)):
        with contextlib.suppress(OSError, ValueError, TypeError): (tgt.update(ld) if cp.is_file() and isinstance(ld := json.loads(cp.read_text(encoding="utf-8")), dict) else None)
    ctx_tag, prev_parts = (f"{union_tree}:{roots_sig}:{env_sig}" if union_tree else ""), str(old_cache.pop("_ctx", "")).split(":"); prev_tree = prev_parts[0] if prev_parts else ""; dirty_rels: set[str] | None = None
    if len(prev_parts) == 3 and prev_parts[1] == roots_sig and prev_parts[2] == env_sig and _HEX40_RE.match(union_tree) and _HEX40_RE.match(prev_tree):
        dirty_rels = set() if union_tree == prev_tree else ({s for s in dt.stdout.decode("utf-8", errors="surrogateescape").split("\0") if s} if (dt := _git_bytes(wt, "diff-tree", "-r", "--name-only", "-z", prev_tree, union_tree, check=False, timeout=_rem(deadline) if deadline is not None else 5.0)).returncode == 0 else None)
    new_cache, py_rel_by_mod, raw_imps_by_mod, preloaded_src, stem_to_mods, misses, dirty, stack = {}, {}, {}, {}, {}, [], (":".join(prev_parts) != ctx_tag), [("", str(wt))]
    while stack:
        (_rem(deadline) if deadline is not None else None); rel_dir, abs_dir = stack.pop(); subdirs, py_ents = [], []
        with os.scandir(abs_dir) as it:
            for ent in it: (subdirs.append((f"{rel_dir}/{ent.name}" if rel_dir else ent.name, ent.path)) if (ent.is_dir(follow_symlinks=False) and ent.name not in _SKIP_DIRS) else (py_ents.append(ent) if ent.name.endswith(".py") else None))
        stack.extend(sorted(subdirs, reverse=True))
        for ent in sorted(py_ents, key=lambda e: e.name):
            fname, rel = ent.name, (f"{rel_dir}/{ent.name}" if rel_dir else ent.name); py_rel_by_mod[mod := _rel_to_mod(rel)] = rel; (stem_to_mods.setdefault(fname[:-3], set()).add(mod) if fname != "__init__.py" else None); st, prev = ent.stat(follow_symlinks=False), old_cache.get(rel)
            if dirty_rels is not None and rel not in dirty_rels and isinstance(prev, list) and len(prev) >= 4 and prev[0] == st.st_mtime_ns and prev[1] == st.st_size: raw_imps_by_mod[mod], new_cache[rel] = prev[3], prev
            else: misses.append((mod, rel, ent.path, st.st_mtime_ns, st.st_size, prev))
    def _read_miss(it: tuple[str, str, str, int, int, Any]) -> tuple[str, str, int, int, Any, bytes, str]: m, r, ep, mt, sz, pv = it; raw = Path(ep).read_bytes(); ne = (b"getenv" in raw or b"environ" in raw) and any(kb in raw for kb in _DYN_BYTES); return m, r, mt, sz, pv, raw, f"{r}:{roots_sig}:{env_sig if ne else '-'}:{sz}:{hashlib.blake2b(raw, digest_size=8).hexdigest()}"
    if misses:
        with (_cf.ThreadPoolExecutor(max_workers=8) if len(misses) > 8 else contextlib.nullcontext()) as ex: dirty, rd_rows = True, (list(ex.map(_read_miss, misses)) if ex else [_read_miss(x) for x in misses])
        for m, r, mt, sz, pv, raw, bk in rd_rows:
            (_rem(deadline) if deadline is not None else None)
            if isinstance(pv, list) and len(pv) >= 4 and pv[2] == bk: raw_imps_by_mod[m], new_cache[r] = pv[3], (ce := [mt, *pv[1:]]); blobs[bk] = pv[3:]; continue
            if isinstance(bh := blobs.get(bk), list) and bh: raw_imps_by_mod[m], new_cache[r] = bh[0], [mt, sz, bk, *bh]; continue
            src = raw.decode("utf-8", errors="replace"); imps = [x for x in _scan_raw_imports(src, _rel_pkg(r), r, wenv_map, tree_out, m) if x.startswith("__") or "." not in x or x.split(".", 1)[0] in top_roots]; raw_imps_by_mod[m], new_cache[r], blobs[bk], preloaded_src[m] = imps, [mt, sz, bk, imps], [imps], src
    cache_dirty = dirty or len(new_cache) != len(old_cache)
    for rel in (r for r in changed_files if r.endswith(".py") and _rel_to_mod(r) not in py_rel_by_mod): py_rel_by_mod[mod := _rel_to_mod(rel)] = rel; (stem_to_mods.setdefault(Path(rel).stem, set()).add(mod) if Path(rel).name != "__init__.py" else None)
    mod_by_rel, rev_deps, dyn_edges, udyn_mods, ereads, fclosed = {r: m for m, r in py_rel_by_mod.items()}, {m: set() for m in py_rel_by_mod}, set(), set(), {}, {}
    def _link(tm: str, im: str, is_dyn: bool = False) -> None: pts = tm.split("."); [rev_deps[anc].add(im) or (dyn_edges.add((anc, im)) if is_dyn else None) for k in range(len(pts), 0, -1) if (anc := ".".join(pts[:k])) in py_rel_by_mod and anc != im]
    for m, imps in raw_imps_by_mod.items():
        m_dir, has_skip_env = posixpath.dirname(py_rel_by_mod[m]), ("__skip_env__" in imps)
        for imp in imps:
            if imp == "__skip_env__" or imp.startswith("__emut__:"): continue
            if imp.startswith("__fclosed__:"): fclosed.setdefault(m, set()).add(imp[12:]); continue
            if imp.startswith("__eread__:"): ereads.setdefault(m, set()).add(imp[10:]); continue
            if imp == "__udyn__": udyn_mods.add(m); continue
            if imp.startswith(("__path__:", "__litpy__:")):
                is_ld = imp.startswith("__path__:"); rp = imp.split(":", 1)[1].lstrip("/"); hits = [mod_by_rel[k] for x in ((rp, f"{m_dir}/{rp}") if m_dir else (rp,)) if (c := _norm_rel(x)) for k in (c, f"{c}/__init__.py") if k in mod_by_rel]; [_link(tm, m, is_ld) for tm in hits]; (udyn_mods.add(m) if is_ld and not hits else None); continue
            if imp.startswith("__dir__:"):
                dp = imp[8:].strip("/"); hits = [tm for tm, tr in py_rel_by_mod.items() if tm != m and (tr.startswith(f"{dp}/") or (not dp and "/" not in tr))]; [_link(tm, m, True) for tm in hits]; (udyn_mods.add(m) if not hits and not (wt / dp).is_dir() else None); continue
            if imp.startswith("__fn__:"): st = imp[7:]; [_link(tm, m, True) for tm in stem_to_mods[st]] if st in stem_to_mods else (None if has_skip_env else udyn_mods.add(m)); continue
            if imp.startswith("__mpfx__:"): mp = imp[9:].strip("."); hits = [tm for tm in py_rel_by_mod if tm != m and (tm == mp or tm.startswith(f"{mp}."))]; [_link(tm, m, True) for tm in hits]; (udyn_mods.add(m) if not hits and not _ext_mod_installed(mp.split(".", 1)[0]) else None); continue
            is_m, is_fl = imp.startswith("__mod__:"), imp.startswith("__fl__:"); is_d = is_m or is_fl; imp = imp.split(":", 1)[1] if is_d else imp; exact, cands_m, matched = (imp in py_rel_by_mod), [imp], False
            while cands_m and (cand := cands_m.pop()):
                if cand in py_rel_by_mod and cand != m: rev_deps[cand].add(m); (dyn_edges.add((cand, m)) if is_d else None); matched = True
                if "." in cand: cands_m.append(cand.rsplit(".", 1)[0])
            if not matched and "." not in imp and imp in stem_to_mods: [_link(tm, m, is_d) for tm in stem_to_mods[imp]]; matched, exact = True, True
            if (is_m and not exact and (imp.split(".", 1)[0] in py_rel_by_mod or not _ext_mod_installed(imp.split(".", 1)[0]))) or (is_fl and not matched and not _ext_mod_installed(imp.split(".", 1)[0])): udyn_mods.add(m)
    if fclosed or ereads:
        def _clos(seeds: set[str], adj: dict[str, set[str]]) -> set[str]:
            seen, q = set(seeds), list(seeds)
            while q: [seen.add(nx) or q.append(nx) for nx in adj.get(q.pop(), ()) if nx not in seen]
            return seen
        for m, fns in fclosed.items():
            for im in _clos({m}, rev_deps) - {m}:
                s_im = preloaded_src.get(im) or ((wt / py_rel_by_mod[im]).read_text(encoding="utf-8", errors="replace") if (wt / py_rel_by_mod[im]).is_file() else "")
                if "__udyn__" in raw_imps_by_mod.get(im, ()) or any(w in s_im for w in ("import *", "getattr", "attrgetter", "__dict__", "globals(", "vars(", "eval(", "exec(")) or any(re.search(rf"\b{re.escape(fn)}\b", s_im) for fn in fns): udyn_mods.add(m); udyn_mods.add(im)
    if ereads:
        fwd_d: dict[str, set[str]] = {u: {pk for k in range(1, u.count(".") + 1) if (pk := ".".join(u.split(".")[:k])) in py_rel_by_mod} for u in py_rel_by_mod}; [fwd_d[im].add(u) for u, ims in rev_deps.items() for im in ims]; base_ud = set(udyn_mods)
        pure_cfs = {cm: cr for cm, cr in py_rel_by_mod.items() if _is_conftest(cr) and (wt / cr).is_file() and not _classify_test_module(preloaded_src.get(cm) or (wt / cr).read_text(encoding="utf-8", errors="replace"), cr, raw_imps_by_mod.get(cm, ()), top_roots)[1]}
        for m, eks in ereads.items():
            imps_m = _clos({m} | base_ud, rev_deps); reach_m = _clos(imps_m | {cm for cm, cr in pure_cfs.items() if any(_conftest_applies(cr, py_rel_by_mod[x]) for x in imps_m if x in py_rel_by_mod)}, fwd_d)
            if (em_k := {i[9:] for x in reach_m for i in raw_imps_by_mod.get(x, ()) if i.startswith("__emut__:")}) and ("*" in em_k or (eks & em_k)): udyn_mods.add(m)
    return py_rel_by_mod, rev_deps, dyn_edges, udyn_mods, stem_to_mods, preloaded_src, new_cache, cache_path, blobs, blob_path, cache_dirty, ctx_tag, top_roots
def _is_extreme_loop_node(node_name: str, fn_src: str) -> bool: return fn_src.count('"git"') >= 8 or fn_src.count("run_orient(") >= 3 or fn_src.count("git(") >= 6 or ("mutant" in node_name and ("parametrize" in fn_src or "for " in fn_src or "mutants/lists" in fn_src)) or ("_run_gate(" in fn_src) or ("time.sleep(30)" in fn_src) or ("count=256" in fn_src) or ("for _ in range(" in fn_src and "run_pass(" in fn_src) or (("range(257)" in fn_src or "range(256)" in fn_src) and ("run_pass(" in fn_src or "FrozenOutbox(" in fn_src))
def _is_heavy_outlier_node(node_name: str, fn_src: str) -> bool: return _is_extreme_loop_node(node_name, fn_src) or any(tok in node_name for tok in ("_killed_by_mutant", "each_mutant", "every_mutant")) or "mutants/lists" in fn_src or fn_src.count("run_pass(") >= 3 or fn_src.count("sleep(") >= 2 or "multiprocessing" in fn_src or (fn_src.count("tc.run(") + fn_src.count("lh.run_manual(")) >= 8
def _caller_defs(src: str, syms: set[str], pkg: str = "", all_defs: bool = False, parsed: Any = None, cur_mod: str = "") -> set[str]:
    if (not syms and not all_defs) or (info := parsed if parsed is not None else _parse_mod_info(src)) is None: return set()
    _, imps, body_info = info; out, active = set(), set(syms)
    while True:
        prev_n = len(active)
        for n in imps:
            if isinstance(n, ast.Import): [active.add(tok := a.asname or a.name.split(".", 1)[0]) or (out.add(tok) if a.asname else None) for a in n.names if a.name == cur_mod or a.name in active or a.name.rsplit(".", 1)[-1] in active]
            elif isinstance(n, ast.ImportFrom):
                mod = (_resolve_rel_mod(pkg, n.level, n.module or "") or "") if n.level else (n.module or "")
                [active.add(tok := a.asname or a.name) or out.add(tok) for a in n.names if a.name != "*" and mod != "__future__" and ((all_defs and n.level > 0) or a.name in active or mod in active or (f"{mod}.{a.name}" if mod else a.name) in active)]
        [out.update(defs) or active.update(defs) or (out.add("__top_se__") if top_se else None) for refs, defs, top_se in body_info if all_defs or not refs.isdisjoint(active)]
        if len(active) == prev_n or all_defs: break
    return out
def _dotted_expr(e: ast.AST) -> str: e = e.value if isinstance(e, ast.Subscript) else e; return e.id if isinstance(e, ast.Name) else (f"{p}.{e.attr}" if isinstance(e, ast.Attribute) and (p := _dotted_expr(e.value)) else "")
def _usefix_from_decs(decs: list[ast.AST]) -> set[str]: return {a.value for d in decs for c in ast.walk(d) if isinstance(c, ast.Call) and (c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")) == "usefixtures" for a in c.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
def _param_names_from_decs(decs: list[ast.AST]) -> set[str]: return {p for d in decs if isinstance(d, ast.Call) and (d.func.attr if isinstance(d.func, ast.Attribute) else getattr(d.func, "id", "")) == "parametrize" and d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str) for p in re.split(r"[,\s]+", d.args[0].value.strip()) if p}
def _scope_fixture_meta(stmts: list[ast.stmt]) -> tuple[dict[str, tuple[bool, set[str]]], set[str], list[str], bool]:
    fx_map, mark_ufx, plugins, has_hook = {}, set(), [], False
    def _walk(body: list[ast.stmt]) -> None:
        nonlocal has_hook
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.name.startswith("pytest_") and n.name not in _SAFE_PYTEST_HOOKS: has_hook = True
                for d in (d for d in n.decorator_list if (getattr(d.func, "attr", getattr(d.func, "id", "")) if isinstance(d, ast.Call) else getattr(d, "attr", getattr(d, "id", ""))) == "fixture"):
                    fx_map[n.name] = (isinstance(d, ast.Call) and any(kw.arg == "autouse" and (not isinstance(kw.value, ast.Constant) or bool(kw.value.value)) for kw in d.keywords), ({a.arg for a in n.args.args + n.args.kwonlyargs if a.arg not in ("self", "cls", "request")} - _param_names_from_decs(n.decorator_list)) | _usefix_from_decs(n.decorator_list))
            elif isinstance(n, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets): mark_ufx.update(_usefix_from_decs([n.value]))
                if any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in n.targets): plugins.extend(x.value for x in ast.walk(n.value) if isinstance(x, ast.Constant) and isinstance(x.value, str) and " " not in x.value and "/" not in x.value)
            elif isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler)) or (hasattr(ast, "TryStar") and isinstance(n, ast.TryStar)): _walk([c for c in ast.iter_child_nodes(n) if isinstance(c, ast.stmt)])
    _walk(stmts); return fx_map, mark_ufx, plugins, has_hook
def _rr_interleave(items: list[tuple[int, int, str]]) -> list[str]:
    buckets: dict[str, list[tuple[int, int, str]]] = {}; [buckets.setdefault(it[2].split("::", 1)[0], []).append(it) for it in sorted(items)]; lists = sorted(buckets.values(), key=lambda lst: (lst[0][0], lst[0][1])); return [lst[i][2] for i in range(max((len(lst) for lst in lists), default=0)) for lst in lists if i < len(lst)]
def _batch_base_blobs(wt: Path, base_sha: str, rels: list[str], deadline: float) -> dict[str, str]:
    if not rels: return {}
    p = subprocess.run([_GIT_EXE, "--no-optional-locks", "cat-file", "--batch"], cwd=str(wt), input="".join(f"{base_sha}:{r}\n" for r in rels).encode("utf-8"), capture_output=True, timeout=_rem(deadline), check=False); out, buf, pos = {}, p.stdout, 0
    for r in rels:
        if (nl := buf.find(b"\n", pos)) < 0: break
        hdr, pos = buf[pos:nl].decode("utf-8", errors="replace").split(), nl + 1
        if len(hdr) >= 3 and hdr[1] == "blob": sz = int(hdr[2]); out[r], pos = buf[pos : pos + sz].decode("utf-8", errors="replace"), pos + sz + 1
    return out
def _select_affected_tests(wt: Path, base_sha: str, union_tree: str, changed_files: list[str], src_cache: dict[str, str], deadline: float, init_trees: dict[str, ast.Module] | None = None) -> dict[str, Any]:
    tree_cache: dict[str, ast.Module | None] = dict(init_trees or {}); old_lines, new_lines, diff_syms, def_syms = _parse_diff_signals(wt, base_sha, union_tree, deadline)
    py_rel_by_mod, rev_deps, dyn_edges, udyn_mods, stem_to_mods, preloaded, scan_cache, cache_path, blobs, blob_path, idx_dirty, ctx_tag, top_roots = _index_python_imports(wt, changed_files, deadline, union_tree, tree_cache)  # type: ignore[arg-type]
    src_cache.update(preloaded); existing_py, ast_cache = set(scan_cache), {}
    def _get_src(mod: str) -> str: (src_cache.__setitem__(mod, (wt / py_rel_by_mod[mod]).read_text(encoding="utf-8", errors="replace")) if mod not in src_cache and mod in py_rel_by_mod and py_rel_by_mod[mod] in existing_py else None); return src_cache.get(mod, "")
    def _get_tree(mod: str) -> ast.Module | None:
        if mod not in tree_cache:
            with contextlib.suppress(SyntaxError): tree_cache[mod] = ast.parse(_get_src(mod))
            tree_cache.setdefault(mod, None)
        return tree_cache[mod]
    def _get_ast(mod: str) -> Any: (ast_cache.__setitem__(mod, _parse_mod_info(_get_src(mod), t) if (t := _get_tree(mod)) is not None else None) if mod not in ast_cache else None); return ast_cache[mod]
    def _has_py_imp(m: str) -> bool: return any(not py_rel_by_mod[im].startswith("tools/mutants/") for im in rev_deps.get(m, ()))
    unmapped_files: set[str] = {rel for rel in changed_files if not rel.endswith(".py")}; changed_source_mods, ast_syms_by_mod, all_changed_symbols, origins, mapped_py = {}, {}, set(), {}, set(); tainted_mods = {_rel_to_mod(rel) for rel in changed_files if rel == "__init__.py" or rel.endswith("/__init__.py")}
    base_blobs = _batch_base_blobs(wt, base_sha, [r for r in changed_files if r.endswith(".py") and not r.startswith("tools/mutants/") and old_lines.get(r) and (not _is_pytest_file(r) or _has_py_imp(_rel_to_mod(r)))], deadline)
    for rel in (r for r in changed_files if r.endswith(".py")):
        _rem(deadline); mod, exists, is_pt = _rel_to_mod(rel), (rel in existing_py), _is_pytest_file(rel)
        if is_pt and exists:
            mapped_py.add(rel)
            if not _has_py_imp(mod): continue
        new_ast, new_side, old_ast, old_side = (set(), True, set(), False) if rel.startswith("tools/mutants/") else (*(_extract_changed_symbols(_get_src(mod), new_lines.get(rel, set()), _rel_pkg(rel), parsed=_get_ast(mod)) if exists else ({mod.rsplit(".", 1)[-1]}, True)), *(_extract_changed_symbols(b_src, old_lines[rel], _rel_pkg(rel)) if (b_src := base_blobs.get(rel)) is not None else (set(), False)))
        ast_s = {s for s in (new_ast | old_ast | def_syms.get(rel, set())) if not (is_pt and s.startswith("test_"))}; (tainted_mods.add(mod) if (new_side or old_side or not exists) else None)
        syms = ({s for s in diff_syms.get(rel, set()) if not s.startswith("test_")} if is_pt else set(diff_syms.get(rel, set()))) | ast_s | (set() if exists else ({Path(rel).stem, mod.rsplit(".", 1)[-1]} - {"__init__"})); changed_source_mods[mod], ast_syms_by_mod[mod] = syms, ast_s; origins.setdefault(mod, set()).add(rel)
        if not is_pt and not rel.startswith("tools/mutants/"): all_changed_symbols |= syms | ({stem} if (stem := Path(rel).stem) not in _GENERIC_STEMS and len(stem) > 2 else set())
    direct_test_rels, inferred_test_rels = {rel for rel in changed_files if _is_pytest_file(rel) and rel in existing_py}, set()
    for rel in changed_files:
        _rem(deadline); is_py, bname, pdir = rel.endswith(".py"), Path(rel).name, (rel.rsplit("/", 1)[0] if "/" in rel else ""); check_dir = "migrations" in pdir.split("/") if pdir else False
        allow_bname = not is_py and len(bname) >= 4 and not bname.upper().startswith(("README", "CHANGELOG", "AGENTS", "CLAUDE", "GEMINI", "LICENSE"))
        if is_py and not check_dir: continue
        for m, m_rel in ((m, mr) for m, mr in py_rel_by_mod.items() if mr != rel and mr in existing_py):
            if (rel in (msrc := _get_src(m))) or (allow_bname and bname in msrc and _is_pytest_file(m_rel)) or (check_dir and (f'"{pdir}"' in msrc or f"'{pdir}'" in msrc or f"{pdir}/" in msrc)):
                if _is_pytest_file(m_rel): inferred_test_rels.add(m_rel); (mapped_py.add(rel) if is_py else None)
                elif rel in msrc or check_dir:
                    r_ast, r_side = _extract_changed_symbols(msrc, {i for i, ln in enumerate(msrc.splitlines(), 1) if (rel in ln) or (check_dir and pdir in ln)}, _rel_pkg(m_rel), parsed=_get_ast(m))
                    changed_source_mods.setdefault(m, set()).update(r_ast | {Path(m_rel).stem}); ast_syms_by_mod.setdefault(m, set()).update(r_ast); (tainted_mods.add(m) if r_side else None); (origins.setdefault(m, set()).add(rel) if is_py else None)
    if changed_source_mods and udyn_mods: [rev_deps.setdefault(cm, set()).add(dm) or dyn_edges.add((cm, dm)) for cm in list(changed_source_mods) for dm in udyn_mods if dm != cm]
    init_syms = {m: (set(ast_s) if len(rev_deps.get(m, ())) > _HUB_IMPORTER_THRESHOLD and (ast_s := ast_syms_by_mod.get(m)) else set(s)) for m, s in changed_source_mods.items()}
    touched_conftests: dict[str, set[str]] = {rel: {rel} for rel in changed_files if _is_conftest(rel)}; seen_syms, frontier, prop_ast_syms, hop = {m: set(s) for m, s in init_syms.items()}, {m: set(s) for m, s in init_syms.items()}, {s for v in ast_syms_by_mod.values() for s in v}, 0
    while frontier and hop < 32:
        _rem(deadline); next_frontier: dict[str, set[str]] = {}
        for cur, raw_cur_syms in frontier.items():
            _rem(deadline); cur_syms, cur_rel, cur_orig = raw_cur_syms - {"__pkg_init__"}, py_rel_by_mod.get(cur, ""), origins.get(cur, set())
            if _is_conftest(cur_rel): touched_conftests.setdefault(cur_rel, set()).update(cur_orig)
            importers, init_pkg, is_hub, eff_syms = rev_deps.get(cur, set()), (cur in tainted_mods), (len(rev_deps.get(cur, set())) > _HUB_IMPORTER_THRESHOLD), cur_syms
            if not init_pkg and (hop > 0 or is_hub) and not eff_syms and not _is_conftest(cur_rel): continue
            cur_stem = Path(cur_rel).stem if cur_rel else ""; seed_syms = eff_syms if (hop > 0 or is_hub) else (eff_syms | {cur, cur.rsplit(".", 1)[-1]} | ({cur_stem} if cur_stem and cur_stem not in _GENERIC_STEMS else set()))
            for r in importers:
                r_src, r_rel, is_dyn = _get_src(r), py_rel_by_mod[r], ((cur, r) in dyn_edges)
                if not init_pkg and not is_dyn and not _is_conftest(r_rel) and (hop > 0 or is_hub) and "import *" not in r_src and f"import {cur} as " not in r_src and not any(sym in r_src for sym in eff_syms): continue
                new_orig = cur_orig - origins.get(r, set()); origins.setdefault(r, set()).update(cur_orig)
                if _is_conftest(r_rel): touched_conftests.setdefault(r_rel, set()).update(origins[r])
                if (r_is_pt := _is_pytest_file(r_rel)) and r_rel in existing_py: inferred_test_rels.add(r_rel); mapped_py |= origins[r]
                if r_rel in existing_py and (not r_is_pt or _has_py_imp(r)):
                    r_is_hub, r_support, r_info = len(rev_deps.get(r, ())) > _HUB_IMPORTER_THRESHOLD, (r_rel.startswith("tests/") and not r_is_pt), _get_ast(r)
                    raw_cs = _caller_defs(r_src, seed_syms | (set(_DYN_LOADERS) if is_dyn else set()), pkg=_rel_pkg(r_rel), all_defs=init_pkg and not r_is_hub, parsed=r_info, cur_mod=cur); top_se = "__top_se__" in raw_cs; raw_cs -= {"__top_se__"}; caller_syms = {s for s in raw_cs if not s.startswith("test_")} if r_is_pt else raw_cs
                    mod_syms = ({r, r.rsplit(".", 1)[-1]} if (r_support or is_dyn) and not r_is_hub else ({r} if hop == 0 and not r_is_hub and not r_is_pt else set()))
                    stem_syms = ({r_stem} if (hop == 0 or r_support) and not r_is_hub and not r_is_pt and (r_stem := Path(r_rel).stem) not in _GENERIC_STEMS and len(r_stem) > 2 else set()); prop = (eff_syms if "import *" in r_src else set()) | caller_syms | mod_syms | stem_syms
                    if top_se or ((r_rel == "__init__.py" or r_rel.endswith("/__init__.py")) and (r_info is None or _has_side_effect(r_info[0].body)) and (init_pkg or (eff_syms and any(sym in r_src for sym in eff_syms)))): tainted_mods.add(r); prop = prop | {"__pkg_init__"}
                    if (new_syms := prop - seen_syms.get(r, set())) or new_orig:
                        seen_syms.setdefault(r, set()).update(new_syms); next_frontier.setdefault(r, set()).update(new_syms or seen_syms[r])
                        if not r_is_pt and not r_rel.startswith("tools/mutants/"): all_changed_symbols |= caller_syms; prop_ast_syms |= caller_syms
        frontier, hop = next_frontier, hop + 1
    bfs_truncated, conftest_rels, test_cache, cache_dirty, fwd_tdeps = bool(frontier), sorted(r for r in existing_py if _is_conftest(r)), {}, idx_dirty, {}
    for rel in (r for r in changed_files if r.endswith(".py") and not r.startswith(("tests/", "tools/mutants/"))):
        pts = Path(rel).with_suffix("").parts; (inferred_test_rels.update(hits) or mapped_py.add(rel) if (hits := [c for c in ([f"tests/test_{pts[-1]}.py"] + ([f"tests/test_{pts[1]}_{pts[-1]}.py"] if len(pts) >= 3 else [])) if c in existing_py]) else None)
    [fwd_tdeps.setdefault(im, set()).add(tm) for tm, ims in rev_deps.items() if tm in py_rel_by_mod and (_is_pytest_file(py_rel_by_mod[tm]) or py_rel_by_mod[tm].startswith("tests/")) for im in ims if im != tm]
    def _test_ancestors(imp: str) -> list[str]: pts = imp.split("."); return [c for i in range(len(pts), 0, -1) if "." in (c := ".".join(pts[:i])) or c in py_rel_by_mod]
    def _ensure_classified(mod: str, force: bool = False) -> None:
        nonlocal cache_dirty
        if mod in test_cache or mod not in py_rel_by_mod or (rel := py_rel_by_mod[mod]) not in existing_py or (not force and not (rel.startswith("tests/") or _is_pytest_file(rel) or _is_conftest(rel))): return
        entry = scan_cache.get(rel); raw_r = entry[3] if isinstance(entry, list) and len(entry) >= 4 else ()
        if isinstance(entry, list) and len(entry) == 6: imps, why = set(entry[4]), str(entry[5])
        else:
            imps, why = _classify_test_module(_get_src(mod), rel, raw_r, top_roots, lambda: _get_tree(mod))
            if isinstance(entry, list) and len(entry) == 4: entry.extend([sorted(imps), why]); blobs[entry[2]], cache_dirty = entry[3:], True
        imps |= fwd_tdeps.get(mod, set()) | {c for x in raw_r if not x.startswith("__") for t in ([x] if "." in x else stem_to_mods.get(x, ())) for c in _test_ancestors(t) if c != mod and c in py_rel_by_mod and (_is_pytest_file(py_rel_by_mod[c]) or py_rel_by_mod[c].startswith("tests/"))}; test_cache[mod] = (imps, why)
        if not why: [_ensure_classified(cand, True) for imp in imps for cand in _test_ancestors(imp) if cand in py_rel_by_mod]
    def _propagate_db() -> dict[str, str]:
        dbr, chg = {m: why for m, (_, why) in test_cache.items() if why}, True
        while chg:
            chg = False
            for m, (imps, _) in ((m, v) for m, v in test_cache.items() if m not in dbr):
                if hit := next((c for imp in imps for c in _test_ancestors(imp) if c in dbr), None): dbr[m], chg = f"imports {hit} ({dbr[hit]})", True
        return dbr
    [_ensure_classified(_rel_to_mod(cf_rel)) for cf_rel in conftest_rels]; cf_db = _propagate_db(); [inferred_test_rels.update(hits) or mapped_py.update(cf_orig) for cf_rel, cf_orig in sorted(touched_conftests.items()) if _rel_to_mod(cf_rel) not in cf_db and (hits := [r for r in existing_py if _is_pytest_file(r) and _conftest_applies(cf_rel, r)])]
    candidate_rels = sorted(direct_test_rels | inferred_test_rels); [(_rem(deadline), _ensure_classified(_rel_to_mod(rel))) for rel in candidate_rels]; [_ensure_classified(dm, True) for dm in (udyn_mods if changed_source_mods else ())]
    def _flush_cache() -> None:
        with contextlib.suppress(OSError): (cache_path.write_text(json.dumps({"_ctx": ctx_tag, **scan_cache}, separators=(",", ":")), encoding="utf-8"), blob_path.write_text(json.dumps(dict(list(blobs.items())[-2500:]) if len(blobs) > 3000 else blobs, separators=(",", ":")), encoding="utf-8")) if cache_dirty else None
    db_reasons, ordered_cfs, cf_meta = _propagate_db(), sorted(conftest_rels, key=lambda r: (r.count("/"), r)), {}
    for cf in ordered_cfs:
        ct = _get_tree(_rel_to_mod(cf)); fx_m, _, plugs, h_hk = _scope_fixture_meta(ct.body) if ct is not None else ({}, set(), [], False)
        for pm in (p for p in plugs if p in py_rel_by_mod and (pt := _get_tree(p)) is not None): pfx, _, _, phk = _scope_fixture_meta(pt.body); fx_m.update(pfx); h_hk = h_hk or phk
        cf_meta[cf] = (fx_m, h_hk)
    skip_cfs: set[str] = {cf for cf in ordered_cfs if _rel_to_mod(cf) in db_reasons}
    def _eval_cf_chain(cfs: list[str]) -> tuple[set[str], set[str], set[str], bool]:
        avail, miss, om_auto, om_hk = set(), set(), set(), False
        for c in cfs:
            cfx, chk = cf_meta[c]
            if c in skip_cfs: miss |= set(cfx) - avail; om_auto.update(k for k, (ia, _) in cfx.items() if ia); om_hk = om_hk or chk
            else:
                brk: set[str] = set()
                for _ in range(len(cfx) + 1): brk.update(k for k, (_, dp) in cfx.items() if dp & ((miss - set(cfx)) | brk))
                [(miss.add(k), avail.discard(k)) if k in brk else (avail.add(k), miss.discard(k), om_auto.discard(k)) for k in cfx]
        return avail, miss, om_auto, om_hk
    for cf in (c for c in ordered_cfs if c not in skip_cfs):
        _, anc_miss, _, _ = _eval_cf_chain([a for a in ordered_cfs if a != cf and _conftest_applies(a, cf)]); cfx, _ = cf_meta[cf]; brk_cf: set[str] = set()
        for _ in range(len(cfx) + 1): brk_cf.update(k for k, (_, dp) in cfx.items() if dp & ((anc_miss - set(cfx)) | brk_cf))
        if any(ia and k in brk_cf for k, (ia, _) in cfx.items()): skip_cfs.add(cf)
    use_noconftest = bool(skip_cfs) and all(cf in skip_cfs for cf in (cf for cf in ordered_cfs if cf in touched_conftests or any(_conftest_applies(cf, r) for r in candidate_rels)))
    unmapped_files.update(cf for cf in touched_conftests if cf in skip_cfs and cf in changed_files); unmapped_files.update(r for r in changed_files if r.endswith(".py") and r not in mapped_py)
    if not candidate_rels: unmapped_files.update(changed_files); _flush_cache(); return {"pure_files": [], "raw_nodes": [], "weights": {}, "total_by_rel": {}, "skipped_db": {}, "deferred_slow": [], "unmapped_files": sorted(unmapped_files), "bfs_truncated": bfs_truncated, "use_noconftest": use_noconftest, "skip_conftests": sorted(skip_cfs), "omitted_cf": any(cf in skip_cfs for cf in touched_conftests)}
    tc_memo: dict[str, tuple[set[str], set[str], dict[str, str]]] = {}
    def _tc_info(mod: str, vis: frozenset[str] = frozenset()) -> tuple[set[str], set[str], dict[str, str]]:
        if mod in tc_memo or mod in vis or mod not in py_rel_by_mod or (tree_m := _get_tree(mod)) is None: return tc_memo[mod] if mod in tc_memo else (set(), set(), {})
        pkg_m, amap, star_tc, star_mt = _rel_pkg(py_rel_by_mod[mod]), {}, set(), set()
        for n in (x for s in tree_m.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for x in ast.walk(s) if isinstance(x, (ast.Import, ast.ImportFrom))):
            if isinstance(n, ast.Import): amap.update({a.asname or a.name.split(".", 1)[0]: a.name if a.asname else a.name.split(".", 1)[0] for a in n.names})
            else:
                rm = (_resolve_rel_mod(pkg_m, n.level, n.module or "") or "") if n.level else (n.module or "")
                for a in n.names:
                    if a.name == "*" and rm in py_rel_by_mod: stc, smt, _ = _tc_info(rm, vis | {mod}); star_tc |= stc; star_mt |= smt
                    elif a.name != "*": amap[a.asname or a.name] = f"{rm}.{a.name}" if rm else a.name
        cls_nodes = [n for n in tree_m.body if isinstance(n, ast.ClassDef)]
        tcs, mts = set(), {c.name for c in cls_nodes if any((isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test_")) or (isinstance(m, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("test_") for t in m.targets)) for m in c.body)}
        while True:
            prev = (len(tcs), len(mts))
            for c in cls_nodes:
                for d in (x for b in c.bases if (x := _dotted_expr(b))):
                    hd, _, tl = d.partition("."); res = (f"{amap[hd]}.{tl}" if tl else amap[hd]) if hd in amap else d
                    if hd in tcs or hd in star_tc or res in _UNITTEST_TC_FULL or res.endswith((".TestCase", ".IsolatedAsyncioTestCase", ".SimpleTestCase")): tcs.add(c.name)
                    if hd in mts or hd in star_mt: mts.add(c.name)
                    if "." in res and (rm_cls := res.rsplit(".", 1))[0] in py_rel_by_mod: rtc, rmt, _ = _tc_info(rm_cls[0], vis | {mod}); (tcs.add(c.name) if rm_cls[1] in rtc else None); (mts.add(c.name) if rm_cls[1] in rmt else None)
            if (len(tcs), len(mts)) == prev: break
        tc_memo[mod] = (tcs, mts, amap); return tcs, mts, amap
    all_pure, skipped_db = [rel for rel in candidate_rels if _rel_to_mod(rel) not in db_reasons], {rel: db_reasons[_rel_to_mod(rel)] for rel in candidate_rels if _rel_to_mod(rel) in db_reasons}
    tier1, tier2_scored, tier3, deferred_slow, total_nodes_by_rel, weights, seq = [], [], [], [], {}, {}, 0; word_syms = {s for s in all_changed_symbols if _WORD_TOK_RE.fullmatch(s)}; punct_syms, rank_syms = sorted(all_changed_symbols - word_syms), sorted(prop_ast_syms or all_changed_symbols)[:40]
    ranked_pure, file_cap = sorted(all_pure, key=lambda r: (0 if r in direct_test_rels else 1, -sum(1 for s in rank_syms if s in _get_src(_rel_to_mod(r))), r)), max(len(direct_test_rels), 10); pure_files, node_costs, rel_omitted_cf = ranked_pure[:file_cap], {}, {}; deferred_slow.extend(ranked_pure[file_cap:])
    (deferred_slow.extend(f"unresolved_loader:{py_rel_by_mod[dm]}" for dm in sorted(udyn_mods) if dm in py_rel_by_mod and py_rel_by_mod[dm] in existing_py) if changed_source_mods else None)
    for rel in pure_files:
        _rem(deadline); src = _get_src(mod_rel := _rel_to_mod(rel))
        if (tree := _get_tree(mod_rel)) is None: tier1.append((-10**9, 0, rel)); weights[rel], node_costs[rel] = 1, (1, 1); continue
        lines, touched = src.splitlines(), new_lines.get(rel, set()); cf_avail, cf_miss, om_auto, om_hk = _eval_cf_chain([cf for cf in ordered_cfs if _conftest_applies(cf, rel)]); lfx_map, mod_ufx, _, _ = _scope_fixture_meta(tree.body); brk_lfx: set[str] = set()
        for _ in range(len(lfx_map) + 1): brk_lfx.update(k for k, (_, dp) in lfx_map.items() if dp & ((cf_miss - set(lfx_map)) | brk_lfx))
        if (mod_ufx & ((cf_miss - set(lfx_map)) | brk_lfx)) or any(ia and k in brk_lfx for k, (ia, _) in lfx_map.items()): deferred_slow.append(rel); continue
        avail_mod = (cf_avail - brk_lfx) | (set(lfx_map) - brk_lfx); miss_mod = (cf_miss - avail_mod) | brk_lfx; (rel_omitted_cf.__setitem__(rel, True) if ((om_auto - avail_mod) or om_hk) else None)
        tcs, mts, amap = _tc_info(mod_rel); local_cls_names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        subproc_helpers = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("test_") and any(tok in _slice_node(lines, n) for tok in ("subprocess.", "Popen("))]; test_nodes, unk_cls_def = [], False
        for n in tree.body:
            if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")) or (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id.startswith("test_")): test_nodes.append((n, n.name if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else n.targets[0].id, _node_start(n), getattr(n, "end_lineno", 0), _slice_node(lines, n), None))
            elif isinstance(n, ast.ClassDef):
                has_init = any(isinstance(m, ast.FunctionDef) and m.name == "__init__" for m in n.body)
                def _res_b(b: ast.AST) -> str | None: hd, _, tl = (d := _dotted_expr(b)).partition("."); return ((f"{amap[hd]}.{tl}" if tl else amap[hd]) if hd in amap else d) if d else None
                def _is_ext(b: ast.AST) -> bool: return (res := _res_b(b)) is None or ((_dotted_expr(b) or "").partition(".")[0] not in local_cls_names and res not in _STD_NON_TEST_BASES and res not in _UNITTEST_TC_FULL and not res.endswith((".TestCase", ".IsolatedAsyncioTestCase", ".SimpleTestCase")) and not ("." in res and res.rsplit(".", 1)[0] in py_rel_by_mod))
                if (n.name in tcs and n.name in mts) or (n.name not in tcs and n.name.startswith("Test") and not has_init and n.name in mts):
                    dm = [m for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test_")]
                    test_nodes.extend((m, f"{n.name}::{m.name}", _node_start(m), getattr(m, "end_lineno", 0), _slice_node(lines, m), n) for m in dm) if (len(dm) >= 6 and all((rb := _res_b(b)) in _UNITTEST_TC_FULL or rb == "object" for b in n.bases)) else test_nodes.append((n, n.name, _node_start(n), getattr(n, "end_lineno", 0), _slice_node(lines, n), None))
                elif any(_is_ext(b) for b in n.bases) and ((n.name in tcs) or (n.name.startswith("Test") and not has_init) or (n.name in mts)): deferred_slow.append(f"{rel}::{n.name}"); unk_cls_def = True
        if not test_nodes: (deferred_slow.append(rel) if not unk_cls_def and not (_has_py_imp(mod_rel) and not any(isinstance(n, ast.ImportFrom) and any(a.name == "*" or (a.asname or a.name).startswith(("test_", "Test")) for a in n.names) for n in tree.body) and not any(k in src for k in ("globals(", "setattr(", "exec(", "pytest_"))) else None); continue
        total_nodes_by_rel[rel], any_body_hit = len(test_nodes), (rel in direct_test_rels and any(not touched.isdisjoint(range(s, e + 1)) for _, _, s, e, _, _ in test_nodes))
        for node_ast, name, start, end, seg, p_cls in test_nodes:
            seq += 1; nid, cls_ctx, cls_sub_h = f"{rel}::{name}", (node_ast if isinstance(node_ast, ast.ClassDef) else p_cls), []
            if cls_ctx is not None:
                cfx_map, cls_ufx, _, _ = _scope_fixture_meta(cls_ctx.body); cls_ufx |= _usefix_from_decs(cls_ctx.decorator_list); brk_cls: set[str] = set()
                for _ in range(len(cfx_map) + 1): brk_cls.update(k for k, (_, dp) in cfx_map.items() if dp & ((miss_mod - set(cfx_map)) | brk_cls))
                miss_node, test_fns = (miss_mod - (set(cfx_map) - brk_cls)) | brk_cls, ([node_ast] if p_cls is not None else [m for m in ast.walk(node_ast) if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test_")])
                fn_reqs = cls_ufx | {k for k, (ia, _) in cfx_map.items() if ia and k in brk_cls}; cls_sub_h = [m.name for m in cls_ctx.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("test_") and any(tok in _slice_node(lines, m) for tok in ("subprocess.", "Popen("))]
                for fn in test_fns: fn_reqs |= _usefix_from_decs(fn.decorator_list) | (({a.arg for a in fn.args.args + fn.args.kwonlyargs if a.arg not in ("self", "cls", "request")} - _param_names_from_decs(cls_ctx.decorator_list + fn.decorator_list)) if cls_ctx.name not in tcs else set())
            elif isinstance(node_ast, (ast.FunctionDef, ast.AsyncFunctionDef)): miss_node, test_fns, fn_reqs = miss_mod, [node_ast], ({a.arg for a in node_ast.args.args + node_ast.args.kwonlyargs if a.arg != "request"} - _param_names_from_decs(node_ast.decorator_list)) | _usefix_from_decs(node_ast.decorator_list)
            else: miss_node, test_fns, fn_reqs = miss_mod, [], set()
            if miss_node and (fn_reqs & miss_node): deferred_slow.append(nid); continue
            calls = seg.count("subprocess.") + seg.count("Popen(") + sum(seg.count(f"{h}(") for h in subproc_helpers) + sum(seg.count(f"self.{h}(") + seg.count(f"cls.{h}(") for h in cls_sub_h) + (max(1, seg.count("run(")) if fn_reqs.intersection(subproc_helpers) else 0)
            slow_ops, is_param = sum(seg.count(k) for k in ("HTTPServer", "socket.", "stalled", "sleep(", "Process(")), ("parametrize" in seg); weights[nid] = (1 + 6 * (calls + slow_ops) + 10 * seg.count('"git"') + 8 * seg.count("run_orient(") + 4 * seg.count("cli.main(")) * (2 if is_param else 1)
            node_costs[nid] = (max(1, len(test_fns)) * (4 if is_param else 1), max(1, calls + slow_ops // 2) * (2 if is_param else 1)); sym_hits = (len(word_syms.intersection(_WORD_TOK_RE.findall(seg))) if word_syms else 0) + sum(1 for s in punct_syms if s in seg)
            if rel in direct_test_rels and (not touched.isdisjoint(range(start, end + 1)) or not any_body_hit): deferred_slow.append(nid) if _is_extreme_loop_node(name, seg) else tier1.append((-sym_hits, seq, nid))
            elif _is_heavy_outlier_node(name, seg) or calls >= 6: deferred_slow.append(nid)
            else: tier2_scored.append((-sym_hits, seq, nid)) if sym_hits > 0 else tier3.append((0, seq, nid))
    raw_selected, light_n, heavy_n = [], 0, 0
    for nid in [*_rr_interleave(tier1), *_rr_interleave(tier2_scored), *_rr_interleave(tier3)]:
        is_heavy, (cl, ch) = weights.get(nid, 1) >= 7, node_costs.get(nid, (1, 1))
        if (is_heavy and heavy_n + ch <= _MAX_HEAVY_NODES) or (not is_heavy and light_n + cl <= _MAX_LIGHT_NODES) or not raw_selected: raw_selected.append(nid); heavy_n, light_n = heavy_n + (ch if is_heavy else 0), light_n + (0 if is_heavy else cl)
        else: deferred_slow.append(nid)
    sel_pure = sorted({n.split("::", 1)[0] for n in raw_selected}); (None if raw_selected else _flush_cache()); return {"pure_files": sel_pure, "raw_nodes": raw_selected, "weights": weights, "total_by_rel": total_nodes_by_rel, "skipped_db": skipped_db, "deferred_slow": deferred_slow, "unmapped_files": sorted(unmapped_files), "bfs_truncated": bfs_truncated, "use_noconftest": use_noconftest, "skip_conftests": sorted(skip_cfs), "omitted_cf": any(rel_omitted_cf.get(r, False) for r in sel_pure) or any(cf in skip_cfs for cf in touched_conftests), "flush_cache": _flush_cache if raw_selected else None}
def _collapse_shard_nodes(nids: list[str], total_nodes_by_rel: dict[str, int]) -> list[str]: by_rel: dict[str, list[str]] = {}; [by_rel.setdefault(nid.split("::", 1)[0], []).append(nid) for nid in nids]; return [x for rel, items in by_rel.items() for x in ([rel] if (total_nodes_by_rel.get(rel, 0) > 0 and len(items) == total_nodes_by_rel[rel] and all("::" in y for y in items)) else items)]
def _partition_shards(raw_nodes: list[str], weights: dict[str, int], total_nodes_by_rel: dict[str, int]) -> list[list[str]]:
    if not raw_nodes or len(raw_nodes) <= 1 or sum(weights.get(n, 1) for n in raw_nodes) <= 6: return [_collapse_shard_nodes(raw_nodes, total_nodes_by_rel)] if raw_nodes else []
    rank, by_file, split_items, whole_units = {n: i for i, n in enumerate(raw_nodes)}, {}, [], []; [by_file.setdefault(nid.split("::", 1)[0], []).append(nid) for nid in raw_nodes]
    [split_items.extend(items) if (len(items) >= 2 and (len(items) > 3 or (fw := sum(weights.get(x, 1) for x in items)) > 6)) else whole_units.append((sum(weights.get(x, 1) for x in items), items)) for items in by_file.values()]
    num_shards = min(_MAX_SHARDS, len(split_items) + len(whole_units)); shard_bins, shard_weights = [[] for _ in range(num_shards)], [0] * num_shards
    for uw, uitems in sorted([*((weights.get(x, 1), [x]) for x in split_items), *whole_units], key=lambda t: t[0], reverse=True): bi = min(range(num_shards), key=lambda i: shard_weights[i]); shard_bins[bi].extend(uitems); shard_weights[bi] += uw
    return [_collapse_shard_nodes(sorted(b, key=lambda n: (weights.get(n, 1) >= 7, rank.get(n, 0))), total_nodes_by_rel) for b in shard_bins if b]
def _build_pytest_invocation(wt: Path, boot_py: Path, shard_nodes: list[str], worker_tmp: Path, junit: Path, use_noconftest: bool = True, skip_conftests: list[str] | tuple[str, ...] = ()) -> tuple[list[str], dict[str, str]]:
    env, needs_async = {**_worker_base_env(), "TEMP": str(worker_tmp), "TMP": str(worker_tmp), "TMPDIR": str(worker_tmp), **({"PREGATE_SKIP_CONFTESTS": "\n".join(skip_conftests)} if skip_conftests else {})}, any((p := wt / rel).is_file() and any(tok in p.read_text(encoding="utf-8", errors="replace") for tok in ("async def test_", ".asyncio")) for rel in {n.split("::", 1)[0] for n in shard_nodes})
    return [sys.executable, str(boot_py), "-q", "-p", "no:cacheprovider", *(["--noconftest"] if use_noconftest else []), *(["-p", "pytest_asyncio.plugin", "-o", "asyncio_mode=auto"] if (needs_async and _HAS_PYTEST_ASYNCIO) else []), "-x", f"--junitxml={junit}", *shard_nodes], env
def _parse_junit(junit_path: Path) -> tuple[dict[str, int], list[str], str | None]:
    if not junit_path.is_file(): return {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}, [], "missing JUnit XML report"
    try: cases = list(ET.parse(junit_path).getroot().iter("testcase"))
    except ET.ParseError as exc: return {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}, [], f"invalid JUnit XML: {exc}"
    counts, failed_names = {"tests": len(cases), "passed": 0, "failed": 0, "skipped": 0}, []; [(counts.__setitem__("failed", counts["failed"] + 1) or failed_names.append(f"{case.get('classname', '')}::{case.get('name', '')}".strip(":"))) if any(c.tag in ("failure", "error") for c in case) else counts.__setitem__("skipped" if any(c.tag == "skipped" for c in case) else "passed", counts["skipped" if any(c.tag == "skipped" for c in case) else "passed"] + 1) for case in cases]; return counts, failed_names, ("0 tests recorded in JUnit XML" if not cases else None)
if os.name == "nt":
    from ctypes import wintypes; _K32 = ctypes.WinDLL("kernel32", use_last_error=True); [setattr(getattr(_K32, _fn), "argtypes", _at) or setattr(getattr(_K32, _fn), "restype", _rt) for _fn, _at, _rt in (("CreateJobObjectW", [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE), ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL), ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL), ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL), ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL))]
    class _JobBasic(ctypes.Structure): _fields_ = [("t1", ctypes.c_int64), ("t2", ctypes.c_int64), ("flags", wintypes.DWORD), ("w1", ctypes.c_size_t), ("w2", ctypes.c_size_t), ("lim", wintypes.DWORD), ("aff", ctypes.c_size_t), ("pri", wintypes.DWORD), ("sch", wintypes.DWORD)]
    class _JobExt(ctypes.Structure): _fields_ = [("basic", _JobBasic), ("io", ctypes.c_uint64 * 6), ("pm", ctypes.c_size_t), ("jm", ctypes.c_size_t), ("ppm", ctypes.c_size_t), ("pjm", ctypes.c_size_t)]
def _close_win_job(job_box: list[int | None]) -> None: (_K32.TerminateJobObject(wintypes.HANDLE(job_box[0]), 1), _K32.CloseHandle(wintypes.HANDLE(job_box[0])), job_box.__setitem__(0, None)) if job_box[0] else None
def _spawn_shard(cmd: list[str], wt: Path, env: dict[str, str], log: Path) -> tuple[subprocess.Popen[bytes], int | None]:
    with open(log, "wb") as lf:
        if os.name != "nt": return subprocess.Popen(cmd, cwd=str(wt), env=env, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True), None
        p, job = subprocess.Popen(cmd, cwd=str(wt), env=env, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP), _K32.CreateJobObjectW(None, None)
        if job: info = _JobExt(); info.basic.flags = 0x2000; _K32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)); ok = bool(_K32.AssignProcessToJobObject(job, wintypes.HANDLE(int(p._handle)))); (None if ok else _K32.CloseHandle(job)); job = int(job) if ok else None
        return p, job
def _poll_shard(p: subprocess.Popen[bytes], job_box: list[int | None]) -> int | None:
    if p.returncode is not None: return p.returncode
    if os.name != "nt" and hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        try:
            if os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None: return None
        except OSError: return p.poll()
        with contextlib.suppress(OSError): os.killpg(p.pid, signal.SIGKILL)
        return p.wait()
    (_close_win_job(job_box) if (rc := p.poll()) is not None and os.name == "nt" else None); return rc
def _kill_procs(procs: list[tuple[int, subprocess.Popen[bytes], list[int | None], Path, Path]], skip: set[int] | None = None) -> None:
    for _, p, job_box, _, _ in (rem_rows := [row for row in procs if row[0] not in (skip or set())]):
        if os.name == "nt" and job_box[0]: _close_win_job(job_box)
        elif p.returncode is None:
            with contextlib.suppress(OSError, subprocess.SubprocessError): subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, stdin=subprocess.DEVNULL, timeout=2, check=False) if os.name == "nt" else os.killpg(p.pid, signal.SIGKILL)
        with contextlib.suppress(OSError): (p.kill() if p.returncode is None else None)
    for _, p, _, _, _ in rem_rows:
        with contextlib.suppress(OSError, subprocess.SubprocessError): p.wait(timeout=1)
_BOOT_CODE = "\n".join(("import contextlib, os, sqlite3, subprocess, sys, pytest", "if _scf := {os.path.normcase(os.path.abspath(p)) for p in os.environ.pop('PREGATE_SKIP_CONFTESTS', '').splitlines() if p}:", "    import types as _T; from pathlib import Path as _P; from _pytest.config import PytestPluginManager as _PM; _oic = _PM._importconftest", "    def _sic(self, cp, *a, **kw):", "        if os.path.normcase(os.path.abspath(cp)) not in _scf: return _oic(self, cp, *a, **kw)", "        if (ex := self.get_plugin(nm := str(cp))) is not None: return ex", "        m, dp = _T.ModuleType(nm), _P(cp).parent; m.__file__ = nm; self._conftest_plugins.add(m)", "        for p, ms in self._dirpath2confmods.items():", "            if (dp == p or dp in p.parents) and m not in ms: ms.append(m)", "        self.consider_conftest(m, registration_name=nm); return m", "    _PM._importconftest = _sic", "if os.name == 'nt':", "    _fstat, _oc, _orun, _wcache = os.fstat, sqlite3.connect, subprocess.run, {}; os.fsync = lambda fd: (_fstat(fd), None)[1]", "    def _fc(*a, **kw):", "        c = _oc(*a, **kw)", "        with contextlib.suppress(Exception): c.execute('PRAGMA synchronous=OFF')", "        return c", "    def _frun(*a, **kw):", "        cmd = a[0] if a else kw.get('args')", "        if isinstance(cmd, (list, tuple)) and len(cmd) == 5 and str(cmd[0]).lower().endswith('whoami.exe') and list(cmd[1:]) == ['/user', '/fo', 'csv', '/nh'] and 'env' not in kw:", "            if (wk := (bool(kw.get('text') or kw.get('universal_newlines')), kw.get('encoding'))) not in _wcache:", "                r = _orun(*a, **kw); (_wcache.__setitem__(wk, r) if r.returncode == 0 else None); return r", "            return _wcache[wk]", "        return _orun(*a, **kw)", "    sqlite3.connect, subprocess.run = _fc, _frun", "sys.path[0] = os.getcwd()", "raise SystemExit(pytest.main(sys.argv[1:]))", ""))
def _run_pytest_shards(wt: Path, shards: list[list[str]], deadline: float, use_noconftest: bool = True, skip_conftests: list[str] | tuple[str, ...] = (), on_spawned: Any = None) -> dict[str, Any]:
    (boot_py.write_text(_BOOT_CODE, encoding="utf-8") if not (boot_py := wt / ".git" / "_pregate_pytest_boot_v3.py").is_file() or boot_py.read_text(encoding="utf-8", errors="replace") != _BOOT_CODE else None); (runs_root := wt.parent / f".runs-{wt.name}").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=str(runs_root), ignore_cleanup_errors=True) as run_dir_str:
        run_dir, procs, rc_by_idx = Path(run_dir_str), [], {}
        try:
            for idx, shard in enumerate(shards): (wtmp := run_dir / f"tmp-{idx}").mkdir(parents=True, exist_ok=True); junit, log = run_dir / f"junit-{idx}.xml", run_dir / f"out-{idx}.log"; cmd, env = _build_pytest_invocation(wt, boot_py, shard, wtmp, junit, use_noconftest=use_noconftest, skip_conftests=skip_conftests); p, job = _spawn_shard(cmd, wt, env, log); procs.append((idx, p, [job], junit, log))
            (on_spawned() if on_spawned is not None else None)
            while len(rc_by_idx) < len(procs):
                if time.perf_counter() >= deadline: _kill_procs(procs); return {"timed_out": True}
                failed_early = False
                for idx, p, job_box, _, _ in procs:
                    if idx not in rc_by_idx and (rc := _poll_shard(p, job_box)) is not None: rc_by_idx[idx], failed_early = rc, (failed_early or rc != 0)
                if failed_early: _kill_procs(procs, skip=set(rc_by_idx)); break
                time.sleep(0.02)
            totals, failed_tests, problems, tails = {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}, [], [], []
            for idx, _, _, junit, log in (row for row in procs if row[0] in rc_by_idx):
                rc, (counts, fnames, jerr) = rc_by_idx[idx], _parse_junit(junit); failed_tests.extend(fnames); [totals.__setitem__(k, totals[k] + counts[k]) for k in totals]
                if rc != 0 or jerr or counts["failed"] > 0: problems.append(f"shard {idx} exited {rc}" if rc != 0 else f"shard {idx}: {jerr or 'failed in JUnit XML'}"); (tails.append(log.read_text(encoding="utf-8", errors="replace")[-1200:]) if log.is_file() else None)
            return {"timed_out": False, "ok": not problems, "counts": totals, "failed_tests": failed_tests, "problems": problems, "log_tail": "\n".join(tails)[-2000:]}
        finally: _kill_procs(procs)
def run_pregate(repo: Path, base: str, head_specs: list[str], timeout: float = 60.0, worktree_dir: Path | None = None) -> dict[str, Any]:
    started, deadline, report = time.perf_counter(), time.perf_counter() + timeout, {"status": "error", "exit_code": EXIT_FAILURE, "base_ref": base, "base_sha": None, "heads": [], "union_tree": None, "changed_files": [], "unmapped_files": [], "doc_files": [], "doc_reader_tests": [], "selected_test_files": [], "selected_tests": [], "tests_ran": 0, "tests_passed": 0, "tests_failed": 0, "tests_skipped": 0, "failed_tests": [], "skipped_db_tests": [], "skipped_db_reasons": {}, "deferred_slow_tests": [], "conflict_head": None, "conflict_head_sha": None, "conflicting_files": [], "reason": "", "elapsed_s": 0.0}
    try:
        if timeout <= 0 or not head_specs or not (repo := repo.resolve()).is_dir(): raise PregateError("--timeout must be > 0, --head required, and --repo must be a directory")
        _check_interpreter()
        common_git_dir, base_sha, base_tree, parsed_heads = _resolve_inputs(repo, base, head_specs, deadline); report["base_sha"] = base_sha
        with _acquire_worktree(common_git_dir, worktree_dir, repo, _rem(deadline)) as wt:
            union_tree, merged_heads, conflict = _merge_heads(wt, base_sha, base_tree, parsed_heads, deadline); report["heads"] = merged_heads
            if conflict is not None: cfiles = conflict["conflicting_files"]; report.update(conflict, status="merge_conflict", exit_code=EXIT_CONFLICT, reason=f"merge conflict on {conflict['conflict_head']} ({len(cfiles)} path(s): {', '.join(cfiles[:10])})"); return report
            assert union_tree is not None; report["union_tree"] = union_tree; report["changed_files"] = changed_files = sorted(p.decode("utf-8", errors="replace") for p in _git_bytes(wt, "diff", "--name-only", "--no-renames", "-z", base_sha, union_tree, timeout=_rem(deadline)).stdout.split(b"\x00") if p)
            if not changed_files: report.update(status="pass", exit_code=EXIT_PASS, reason="empty union delta against base (full local_gate.py still required)"); return report
            _sync_union_worktree(wt, union_tree, deadline); src_cache, init_trees = {}, {}
            for rel in (r for r in changed_files if r.endswith(".py") and (wt / r).is_file()):
                src_cache[mod_c := _rel_to_mod(rel)] = src = (wt / rel).read_text(encoding="utf-8", errors="replace")
                try: init_trees[mod_c] = ast.parse(src, filename=rel)
                except SyntaxError as exc: report.update(status="test_failure", exit_code=EXIT_FAILURE, failed_tests=[rel], reason=f"SyntaxError in {rel}:{exc.lineno}: {exc.msg}"); return report
            sel = _select_affected_tests(wt, base_sha, union_tree, changed_files, src_cache, deadline, init_trees); pure_files, raw_nodes, weights, total_by_rel, skipped_db, deferred_slow, unmapped = sel["pure_files"], sel["raw_nodes"], sel["weights"], sel["total_by_rel"], sel["skipped_db"], sel["deferred_slow"], sel["unmapped_files"]
            doc_files = [u for u in unmapped if _is_doc_path(u)]; unmapped = [u for u in unmapped if not _is_doc_path(u)]; report["doc_files"] = doc_files; report["doc_reader_tests"] = doc_readers = _doc_reader_tests(wt, doc_files, _whole_files(raw_nodes, total_by_rel))
            report.update(unmapped_files=unmapped, selected_test_files=pure_files, selected_tests=_collapse_shard_nodes(raw_nodes, total_by_rel), skipped_db_tests=sorted(skipped_db), skipped_db_reasons=skipped_db, deferred_slow_tests=deferred_slow)
            if raw_nodes:
                if (run_res := _run_pytest_shards(wt, _partition_shards(raw_nodes, weights, total_by_rel), deadline, use_noconftest=sel["use_noconftest"], skip_conftests=sel.get("skip_conftests", ()), on_spawned=sel.get("flush_cache"))).get("timed_out"): report.update(status="timeout", exit_code=EXIT_FAILURE, reason=f"exceeded timeout of {timeout:.1f}s"); return report
                counts = run_res["counts"]; report.update(tests_ran=counts["tests"], tests_passed=counts["passed"], tests_failed=counts["failed"], tests_skipped=counts["skipped"], failed_tests=run_res["failed_tests"])
                if not run_res["ok"]: report.update(status="test_failure", exit_code=EXIT_FAILURE, reason="; ".join(run_res["problems"]) + (f" ({', '.join(run_res['failed_tests'][:5])})" if run_res["failed_tests"] else ""), **({"log_tail": run_res["log_tail"]} if run_res.get("log_tail") else {})); return report
            if unmapped: report.update(status="unmapped", exit_code=EXIT_INCONCLUSIVE, reason=f"unmapped files require full local_gate.py: {', '.join(unmapped[:8])}")
            elif doc_readers: report.update(status="partial", exit_code=EXIT_INCONCLUSIVE, reason=f"{len(doc_readers)} test file(s) read a changed doc file and were not run ({', '.join(doc_readers[:5])}); run local_gate.py")
            elif not raw_nodes or report["tests_passed"] == 0 or deferred_slow or skipped_db or sel["bfs_truncated"] or sel.get("omitted_cf"): report.update(status="partial", exit_code=EXIT_INCONCLUSIVE, reason=f"partial coverage ({report['tests_passed']} passed, {len(deferred_slow)} deferred, {len(skipped_db)} DB skipped, omitted_conftest={bool(sel.get('omitted_cf'))}, bfs_truncated={sel['bfs_truncated']}); run full local_gate.py")
            else: report.update(status="pass", exit_code=EXIT_PASS, reason=f"no collision found among the tests it could map ({report['tests_passed']} passed, {report['tests_skipped']} skipped across {len(pure_files)} file(s); full local_gate.py still required)")
            return report
    except subprocess.TimeoutExpired: report.update(status="timeout", exit_code=EXIT_FAILURE, reason=f"exceeded timeout of {timeout:.1f}s")
    except subprocess.CalledProcessError as exc: err = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""); report.update(status="error", exit_code=EXIT_FAILURE, reason=f"git command failed ({exc.returncode}): {err.strip()}")
    except InterpreterError as exc: report.update(status="interpreter", exit_code=EXIT_INCONCLUSIVE, reason=str(exc))
    except PregateError as exc: report.update(status="error", exit_code=EXIT_FAILURE, reason=str(exc))
    finally: report["elapsed_s"] = round(time.perf_counter() - started, 4)
    return report
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); [ap.add_argument(flag, **kw) for flag, kw in (("--repo", {"type": Path, "default": Path(__file__).resolve().parents[2]}), ("--base", {"default": "origin/main"}), ("--head", {"action": "append", "dest": "heads", "required": True, "metavar": "[LABEL=]COMMIT"}), ("--timeout", {"type": float, "default": 60.0}), ("--worktree-dir", {"type": Path, "default": None}), ("--json", {"action": "store_true"}))]
    try: args = ap.parse_args(argv)
    except SystemExit as exc: return EXIT_PASS if exc.code == 0 else EXIT_FAILURE
    report = run_pregate(repo=args.repo, base=args.base, head_specs=args.heads, timeout=args.timeout, worktree_dir=args.worktree_dir); print(json.dumps(report, indent=2, sort_keys=True) if args.json else f"PREGATE {report['status'].upper()} (tree={(report['union_tree'] or '-')[:12]}, {report['elapsed_s']:.2f}s): {report['reason']}"); (print(report["log_tail"], file=sys.stderr) if (not args.json and report.get("log_tail")) else None); return int(report["exit_code"])
if __name__ == "__main__": raise SystemExit(main())
