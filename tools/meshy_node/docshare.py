"""Passing a DOCUMENT through MeshyChat without pointing at the sender's own disk (divergence 064).

THE PROBLEM, measured 2026-09-30 on production mail (read-only, counts only): of 24,316 message heads, 1,981 contained a Windows local path, 137 a Unix path and 279 a scratchpad or
`.claude` path, against 447 that linked a repository page. A path on the sender's machine opens on nobody else's, so the reader got a pointer to nothing.

THE RULE. A message carries its text INLINE, or cites a COMMIT in a repository the reader can fetch: `owner/name@<sha>:<path>`, which `share` prints, with the github URL. A repository-
relative path (`apps/mesh/wait.py`) is not a local path and is fine; a drive letter, a home directory, `/tmp`, a scratchpad or a `.claude` directory is.

TWO PARTS.
  local_paths(text)        the local paths a body names. `send` refuses a body that names a FILE on this machine among them (refuse_local_files, #120, 2026-10-10) and warns about
                           the rest, unless `--allow-local-path`, so the mistake is caught where the message is written. `offer` refuses any (refuse_local_paths).
  share(file, clone, ...)  copies the file into a clone of the docs repository, commits it on the branch `handoff/<alias>`, pushes that branch and returns the citation. The branch is
                           kept (not main), so the commit stays reachable and the record stays append-only; nothing already in the repository is overwritten.

It changes no server behaviour and needs no node identity.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

_BS = chr(92)
#: What counts as a path only the sender's machine can open. Each alternative is anchored so a repository path or a URL does not match.
_PATTERNS = (
    r"(?<![A-Za-z0-9])[A-Za-z]:[/" + _BS + _BS + r"][^" + r"\s`'\")<>|]+",                      # C:\Users\x, C:/_SampleProjects/y
    r"(?<![A-Za-z0-9:/])" + _BS + _BS + _BS + _BS + r"[A-Za-z0-9_.$-]+" + _BS + _BS + r"[^\s`'\")<>|]+",   # \\host\share\x
    r"(?:(?<=[\s(`'\"=])|^)/(?:Users|home|tmp|var|Volumes|private|opt|mnt)/[^\s`'\")<>|]+",   # /Users/x, /home/x, /tmp/x
    r"(?:(?<=[\s(`'\"=])|^)~/[^\s`'\")<>|]+",                                                   # ~/notes
    r"(?<![A-Za-z0-9])file:/{1,3}[^" + r"\s`'\")<>|]+",                                                                    # file:///anything
    r"[^\s`'\")<>|]*(?:scratchpad|[.]claude)[/" + _BS + _BS + r"][^\s`'\")<>|]*",               # a scratchpad or .claude path in any form
)
_LOCAL = re.compile("|".join("(?:%s)" % p for p in _PATTERNS), re.IGNORECASE)
#: What is NOT a local path whatever it contains: a URL of a remote scheme (a `file:` URL IS a path on the sender's disk and is kept), a git remote in scp form, and a
#: repository citation `owner/name@<sha>:<path>`. A reader can open each of them from anywhere, so a `.claude` or `scratchpad` directory INSIDE one is a repository's folder,
#: not the sender's (review of #42, P2 3). They are cut out of the text before the local patterns look at it.
_STOP = r"[^\s`'\")<>|]"
_REMOTE = re.compile(r"(?<![A-Za-z0-9])(?!file:)[A-Za-z][A-Za-z0-9+.-]*://" + _STOP + r"+|git@[A-Za-z0-9.-]+:" + _STOP + r"+|[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*@[0-9a-f]{7,40}:" + _STOP + r"+", re.IGNORECASE)
_MAX_SHOWN = 5


def local_paths(text: str) -> list[str]:
    """The distinct local paths `text` names, in order of first appearance (at most _MAX_SHOWN plus a count via len of the full list is not kept: callers show the first few)."""
    seen: list[str] = []
    for m in _LOCAL.finditer(_REMOTE.sub(" ", text or "")):
        s = m.group(0).rstrip(".,;:)")
        if s and s not in seen:
            seen.append(s)
    return seen


def refuse_local_paths(text: str, refused_type) -> None:
    """Raise `refused_type` (node.Refused) when the body names a path on the sender's disk. Nothing has been sent when this runs."""
    found = local_paths(text)
    if not found:
        return
    shown = "; ".join(found[:_MAX_SHOWN]) + ("; ..." if len(found) > _MAX_SHOWN else "")
    raise refused_type(
        "the message names a path on YOUR machine, which no reader can open: " + shown + ". Put the text inline, or publish the file with "
        "`python -m tools.meshy_node share <file> --clone <docs repo clone>` and cite the repo@sha:path it prints. "
        "To send it as it is anyway: --allow-local-path.")


#: A citation suffix after a file name: `:95`, `:95:3`, `:95-120`, `#L95`, `#L95-L120`. Stripped before the name is looked for (review r1 of #120's PR).
_CITE_SUFFIX = re.compile(r"(?:#L\d+(?:-L?\d+)?|:\d+(?::\d+)?(?:-\d+)?)$")
#: How far a path cut at a space is extended, in space-separated pieces, to find a file whose name has spaces.
_MAX_SPACE_PIECES = 8
_QUOTES = "`'\""
#: Prose punctuation that ends a sentence or a bracket after a path, cut from every name before it is looked up (GitHub Codex
#: on #195: `see /tmp/a.md!` looked up `/tmp/a.md!`, so a real file was sent).
_TRAIL = ".,;:!?)]}>"


def _local_spans(text: str) -> list[tuple[str, int]]:
    """(path, start) for each local path, as local_paths finds them; remote parts are blanked with spaces, so the offsets are the text's own."""
    blanked = _REMOTE.sub(lambda m: " " * len(m.group(0)), text or "")
    out: list[tuple[str, int]] = []
    for m in _LOCAL.finditer(blanked):
        s = m.group(0).rstrip(".,;:)")
        if s:
            out.append((s, m.start()))
    return out


#: Distinct names one message may make the guard look up. Past it, a path not yet settled is refused as before 2026-10-10:
#: a body cannot slow `send` without bound, and nothing unchecked is waved through (review r2 of #120's PR).
_MAX_PROBES = 128
_WINDOWS = os.name == "nt"


def _unc(name: str) -> bool:
    """A network share in either spelling (`\\\\host\\share`, `//host/share`): never probed, an unreachable host stalls on SMB timeouts."""
    return name[:2] in (_BS * 2, "//")


def _file_url_path(url: str, *, windows: bool = _WINDOWS, to_path=url2pathname) -> str | None:
    """A `file:` URL as this machine's path: the citation suffix cut, percent escapes decoded, `localhost` or no authority only. None for a
    remote authority, a URL that decodes to a network share, or one that does not parse: each is warned about and never probed."""
    try:
        parts = urlsplit(_CITE_SUFFIX.sub("", url))
        if parts.netloc.lower() not in ("", "localhost"):
            return None
        path = parts.path
        if not windows and re.match(r"^/?[A-Za-z]:", path):
            local = path.lstrip("/")                               # file:///C:/x on a machine without drives: C:/x, which is no file here
        else:
            local = to_path(path)
    except (ValueError, OSError):                                  # file://[x, or a converter that refuses (nturl2path: "Bad URL")
        return None
    return None if not local or _unc(local) else local


def _candidates(found: str, text: str, start: int, *, url_path=_file_url_path) -> list[str]:
    """The names `found` may stand for on this disk, in the order they are looked up.

    A quoted or backticked path is ended by its quote: the whole of it, with and without a citation suffix, and nothing shorter or longer
    (review r2: a shorter prefix that is a file must not refuse a quoted folder). Otherwise: the extractor's fragment, the first
    space-free token (the extractor stops at an apostrophe or a parenthesis: `Kxxx's.md`, `report(final).md`), and that token extended
    across up to 8 space-separated pieces (`My Documents`), each with and without a citation suffix (`:95`, `#L95`). A `file:` URL is
    rebuilt the same way and then decoded (review r3: `file:///tmp/Kxxx's.md` was decoded from its fragment). A network share gives none."""
    if _unc(found):
        return []
    line_end = text.find("\n", start)
    rest = text[start:] if line_end < 0 else text[start:line_end]
    before = text[start - 1] if start > 0 else ""
    if before and before in _QUOTES and before in rest:
        names = [rest[:rest.index(before)]]                        # "C:/My Documents/a.md", `~/x y/z.md`: the quotes end it
    else:
        pieces = rest.split(" ")
        names = [found] + [" ".join(pieces[:k]) for k in range(1, min(len(pieces), _MAX_SPACE_PIECES) + 1)]
    out: list[str] = []
    for name in names:
        name = name.rstrip(_TRAIL + _QUOTES)
        for n in (name, _CITE_SUFFIX.sub("", name)):
            if n[:5].lower() == "file:":
                n = url_path(n) or ""
            if n and not _unc(n) and n not in out:
                out.append(n)
    return out


def _is_file_here(name: str) -> bool:
    """Whether `name` is a file on this machine, `~` expanded. Never raises: an unknown ~user, a NUL or an OS error is no file."""
    try:
        return Path(name).expanduser().is_file()
    except (OSError, ValueError, RuntimeError):
        return False


def refuse_local_files(text: str, refused_type, *, is_file=_is_file_here, max_probes: int = _MAX_PROBES) -> list[str]:
    """`send`'s guard (#120, option 2; divergence 064, narrowed 2026-10-10). Raise `refused_type` when the body names a FILE that exists on this
    machine: that is a document handed over as a pointer, which no reader can open. Each found path is looked up under the names
    `_candidates` gives; a name is looked up once per message, and at most `max_probes` names are (past that, an unsettled path is refused,
    the guard's behaviour before 2026-10-10). Return the other local paths it names (a folder, a path that does not exist here, another
    machine's path, a network share) for the caller to WARN about: those cite, they do not hand over, and refusing them made the override a
    habit (14 overrides against 5 refusals on one seat). Nothing has been sent when this runs."""
    files: list[str] = []
    cited: list[str] = []
    known: dict[str, bool] = {}
    for found, start in _local_spans(text):
        hit, settled = None, True
        for name in _candidates(found, text, start):
            if name not in known:
                if len(known) >= max_probes:
                    settled = False
                    break
                known[name] = bool(is_file(name))
            if known[name]:
                hit = name
                break
        target, shown = (files, hit) if hit is not None else ((cited, found) if settled else (files, found))
        if shown not in target:
            target.append(shown)
    if files:
        shown = "; ".join(files[:_MAX_SHOWN]) + ("; ..." if len(files) > _MAX_SHOWN else "")
        raise refused_type(
            "the message names a FILE on YOUR machine, which no reader can open: " + shown + ". Put the text inline, or publish the file with "
            "`python -m tools.meshy_node share <file> --clone <docs repo clone>` and cite the repo@sha:path it prints. "
            "To send it as it is anyway: --allow-local-path.")
    return cited


def _git(clone: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise ShareFailed("git %s failed: %s" % (" ".join(args[:2]), (r.stderr or r.stdout).strip()[-300:]))
    return r


class ShareFailed(Exception):
    """A document could not be published. Nothing was sent."""


def _repo_slug(remote_url: str) -> str | None:
    """`owner/name` from a github remote URL (https or ssh), or None for any other host."""
    m = re.match(r"^(?:https://(?:[^/@]+@)?github[.]com/|git@github[.]com:|ssh://git@github[.]com/)([^/]+/[^/]+?)(?:[.]git)?/?$", remote_url.strip())
    return m.group(1) if m else None


def share(file: Path, clone: Path, *, alias: str, dest: str = "handoff", push: bool = True, remote: str = "origin", now: _dt.datetime | None = None) -> dict:
    """Commit `file` into `clone` under `<dest>/<alias>/` on branch `handoff/<alias>`, push it, and return the citation. Never overwrites: a name already there gets the
    first 8 hex of its content hash appended, so two different documents never collide and the same one is not duplicated."""
    file, clone = Path(file), Path(clone)
    if not file.is_file():
        raise ShareFailed("no such file: %s" % file)
    if not (clone / ".git").exists():
        raise ShareFailed("%s is not a git clone" % clone)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias):
        raise ShareFailed("--alias must be your seat's label (letters, digits, . _ -)")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}", dest) or ".." in dest.split("/"):
        raise ShareFailed("--dest must be a relative folder name")
    if _git(clone, "status", "--porcelain").stdout.strip():
        raise ShareFailed("the clone has uncommitted changes; use a clean clone of the docs repository")
    stamp = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%dT%H%MZ")
    data = file.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    branch = "handoff/" + alias
    have = _git(clone, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch, check=False)
    start = "HEAD"
    if have.returncode == 0:
        _git(clone, "checkout", "-q", branch)
    else:
        _git(clone, "checkout", "-q", "-b", branch, start)
    folder = clone / dest / alias
    folder.mkdir(parents=True, exist_ok=True)
    # The destination is the first candidate that is free or ALREADY holds exactly these bytes: the plain name, then the name with 8, 16 and 64 hex of the content hash. An occupied
    # candidate with other bytes is never cited (review of #42, P2 1: a file of that name could be an unrelated document). None left: refuse.
    target = None
    for suffix in ("", digest[:8], digest[:16], digest):
        name = file.name if not suffix else "%s-%s%s" % (file.stem, suffix, file.suffix)
        candidate = folder / name
        if not candidate.exists() or candidate.read_bytes() == data:
            target = candidate
            break
    if target is None:
        raise ShareFailed("every name for %s under %s/%s is taken by a different document" % (file.name, dest, alias))
    if not target.exists():
        target.write_bytes(data)                                      # the bytes captured before the checkout, never the source path reopened after it (review of #42, P2 2)
    rel = target.relative_to(clone).as_posix()
    _git(clone, "add", "--", rel)
    if _git(clone, "diff", "--cached", "--quiet", check=False).returncode != 0:
        _git(clone, "commit", "-q", "-m", "handoff from %s: %s (%s)" % (alias, file.name, stamp))
    sha = _git(clone, "rev-parse", "HEAD").stdout.strip()
    url_remote = _git(clone, "remote", "get-url", remote, check=False).stdout.strip()
    slug = _repo_slug(url_remote) if url_remote else None
    pushed = False
    if push:
        _git(clone, "push", "-q", remote, "%s:refs/heads/%s" % (branch, branch))
        back = _git(clone, "ls-remote", remote, "refs/heads/" + branch).stdout.split()
        if not back or back[0] != sha:
            raise ShareFailed("pushed, but the remote's %s is not %s; do not cite it" % (branch, sha[:8]))
        pushed = True
    cite = "%s@%s:%s" % (slug or url_remote or str(clone), sha[:12], rel)
    return {"citation": cite, "repo": slug, "sha": sha, "path": rel, "branch": branch, "pushed": pushed,
            "url": ("https://github.com/%s/blob/%s/%s" % (slug, sha, rel)) if slug else None, "sha256": digest}


def add_parser(sub) -> None:
    p = sub.add_parser("share", help="publish a file as a commit in a docs repository and print the citation to put in a message")
    p.add_argument("file", type=Path)
    p.add_argument("--clone", required=True, type=Path, help="a CLEAN clone of the docs repository (one you may push a branch to)")
    p.add_argument("--alias", required=True, help="your seat's label; the file goes under <dest>/<alias>/ on branch handoff/<alias>")
    p.add_argument("--dest", default="handoff")
    p.add_argument("--no-push", action="store_true", help="commit only (the citation is then NOT readable by anyone else)")


def run(args) -> int:
    try:
        out = share(args.file, args.clone, alias=args.alias, dest=args.dest, push=not args.no_push)
    except ShareFailed as exc:
        sys.stderr.write(json.dumps({"error": "share_failed", "detail": str(exc)}) + "\n")
        return 1
    sys.stdout.write(json.dumps(out, indent=2) + "\n")
    return 0
