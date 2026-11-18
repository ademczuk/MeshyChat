"""Divergence 064: documents travel as commits, not as paths on the sender's disk (tools/meshy_node/docshare.py).

Measured 2026-09-30 on production mail: 1,981 of 24,316 message heads named a Windows local path. `send` now refuses such a body, and `share` publishes a file as a commit and prints the
citation. `share` is tested against a REAL bare repository on disk used as the remote, so the push and the read-back are the real git operations and nothing touches a network.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_docshare.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, docshare, node  # noqa: E402

BS = chr(92)

MUST_REFUSE = {
    "windows drive, backslashes": "see C:" + BS + "Users" + BS + "Kxxx" + BS + "notes.md",
    "windows drive, slashes": "verdict at C:/_SampleProjects/mcd-gate/verdict.txt please",
    "windows drive in parentheses": "(D:" + BS + "work" + BS + "a.txt)",
    "unc share": BS * 2 + "host" + BS + "share" + BS + "f.txt",
    "mac home": "file /Users/hz/notes/a.md here",
    "linux home": "in /home/seat/x.log",
    "tmp": "saved to /tmp/out.json",
    "tilde home": "saved to ~/notes/a.md",
    "scratchpad relative": "at scratchpad/msg_x.txt",
    "dot claude": "in .claude/projects/x",
    "start of text": "/Users/hz/a.md is the file",
    "a file: url is the sender's own disk": "file:///C:/Users/x/a.md",
    "a file: url on unix": "see file:///tmp/x.json",
    "a local path beside a url": "see https://x.org/a and C:/Users/x/b.md",
    "a local path after a citation": "o/r@abc123def456:a/b.md then /home/seat/x.log",
}
MUST_ALLOW = {
    "repo relative path": "apps/mesh/wait.py and tests/test_x.py:12",
    "a citation": "kxxx-owner/docs@abc123def456:handoff/kxxx/a.md",
    "a github url": "https://github.com/sampleco/ASDLC/blob/abc/x.md",
    "a lan url": "GET http://192.168.50.10:7399/v1/health answered 200",
    "times and ratios": "it ran at 10:30Z, ratio 3:1, a:b",
    "a one letter thing": "the c: drive letter alone is not a path",
    "a relative dotted path": "see ./docs/design/edit.md and ../x",
    "a github url whose repo has a .claude folder": "https://github.com/sampleco/ASDLC/blob/abc123/.claude/review.md",
    "a github url whose repo has a scratchpad folder": "https://github.com/o/r/blob/abc/scratchpad/x.md",
    "a citation whose path has a scratchpad folder": "o/r@abc123def456:handoff/reviewer/scratchpad/report.md",
    "a git remote in scp form": "git@github.com:sampleco/ASDLC.git",
}


@pytest.mark.parametrize("name", sorted(MUST_REFUSE))
def test_a_path_on_the_senders_disk_is_found(name):
    assert docshare.local_paths(MUST_REFUSE[name]), name


@pytest.mark.parametrize("name", sorted(MUST_ALLOW))
def test_a_repo_path_a_citation_or_a_url_is_not_a_local_path(name):
    assert docshare.local_paths(MUST_ALLOW[name]) == [], (name, docshare.local_paths(MUST_ALLOW[name]))


def test_refuse_names_the_fix_and_the_paths():
    with pytest.raises(node.Refused) as err:
        docshare.refuse_local_paths("see C:/x/a.md and /tmp/b.json", node.Refused)
    text = str(err.value)
    assert "C:/x/a.md" in text and "/tmp/b.json" in text and "share" in text and "--allow-local-path" in text


# ------------------------------------------------------------------------------------------------------------ `send` (no network: node.send is replaced)

@pytest.fixture()
def sent(monkeypatch, tmp_path):
    """cli.main's `send` with node.send replaced by a recorder and the state load skipped: what is asserted is the GUARD, which runs before either."""
    calls = []
    monkeypatch.setattr(node, "send", lambda *a, **kw: calls.append(kw) or {"status": "completed"})
    monkeypatch.setattr(node, "succeeded", lambda r: True)
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {}, raising=False)
    return calls


def _send(body, *extra, tmp_path=None):
    args = ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "state.json"), "--to", "bob", "--thread", "t", "--body", body, *extra]
    return cli.main(args)


def test_send_refuses_a_body_that_names_a_file_on_this_disk_and_sends_nothing(sent, tmp_path, capsys):
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    notes = tmp_path / "n.md"
    notes.write_text("x", encoding="utf-8")
    assert docshare.local_paths(str(notes)), "the temp folder must be one the guard recognises on this OS"
    rc = _send(f"my notes are at {notes}", tmp_path=tmp_path)
    assert rc == 2 and sent == [], (rc, sent)
    (refused,) = [json.loads(line)["refused"] for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert "no reader can open" in refused and str(notes) in refused      # parsed: on Windows the JSON doubles every backslash


def test_send_sends_a_cited_folder_or_a_path_that_is_no_file_here_with_a_warning(sent, tmp_path, capsys):
    """#120, option 2 (2026-10-10): only a FILE that exists on this machine is a document handed over as a pointer.
    A folder, another machine's path and a path that does not exist here cite; they are sent, and stderr says so.
    MUTANT: refuse every local path again (refuse_local_paths); this send exits 2 and sends nothing.
    MUTANT: drop the warning; stderr has no local_path_cited line."""
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    absent = tmp_path / "not-here.md"
    body = f"the postbox lives in {tmp_path} and the old notes were at {absent} and C:/Users/x/n.md"
    assert _send(body, tmp_path=tmp_path) == 0
    assert len(sent) == 1 and sent[0]["body"] == body
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    (cited,) = [line for line in lines if line.get("warning") == "local_path_cited"]
    assert str(tmp_path) in cited["paths"] and str(absent) in cited["paths"] and "C:/Users/x/n.md" in cited["paths"]


def test_refuse_local_files_refuses_only_files_and_names_only_them(tmp_path, monkeypatch):
    """`~` is expanded and a `file:` URL's scheme dropped before the check; the refusal lists the files, not the folders."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("x", encoding="utf-8")
    with pytest.raises(node.Refused) as err:
        docshare.refuse_local_files(f"see ~/notes/a.md, and the folder ~/notes", node.Refused)
    assert "~/notes/a.md" in str(err.value) and "the folder" not in str(err.value)
    with pytest.raises(node.Refused):
        docshare.refuse_local_files((tmp_path / "notes" / "a.md").as_uri(), node.Refused)
    assert docshare.refuse_local_files("see ~/notes and ~/nothing.md", node.Refused) == ["~/notes", "~/nothing.md"]


@pytest.mark.parametrize("form", ["line", "line and column", "line range", "github anchor", "quoted, spaces", "backticked, spaces",
                                  "unquoted, spaces", "file url, encoded space", "file url, localhost", "file url, encoded letter"])
def test_an_existing_file_cited_in_any_common_form_is_refused_before_any_send(sent, tmp_path, capsys, form):
    """Review r1 of #120's PR: the extractor yields fragments (it stops at a space and keeps a `:95` suffix), and a `file:` URL is percent-encoded,
    so probing the fragment as written let a real file through with a warning that called it no file. Each form must refuse, naming the file.
    MUTANT: drop the quoted and space-extended names; the three space forms are sent. MUTANT: drop the suffix strip; the four
    suffix forms are sent. MUTANT: return a file: URL's path undecoded; the encoded forms are sent. MUTANT: refuse `localhost` as an
    authority; the localhost form is sent."""
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    folder = tmp_path / "My Documents"
    folder.mkdir()
    doc = folder / "report.md"
    doc.write_text("x", encoding="utf-8")
    plain = tmp_path / "plain.md"
    plain.write_text("x", encoding="utf-8")
    url = doc.as_uri()
    body = {
        "line": f"see {plain}:95 please",
        "line and column": f"see {plain}:95:3",
        "line range": f"see {plain}:95-120",
        "github anchor": f"see {plain}#L95-L120",
        "quoted, spaces": f'see "{doc}" now',
        "backticked, spaces": f"see `{doc}` now",
        "unquoted, spaces": f"see {doc} now",
        "file url, encoded space": f"see {url}",
        "file url, localhost": "see " + url.replace("file:///", "file://localhost/", 1),
        "file url, encoded letter": "see " + plain.as_uri().replace("plain.md", "%70lain.md"),
    }[form]
    assert _send(body, tmp_path=tmp_path) == 2 and sent == [], (form, body)
    err = capsys.readouterr().err
    assert "names a FILE" in err and ("report.md" in err or "plain.md" in err), err


def test_a_folder_with_spaces_and_a_remote_file_url_are_warned_not_refused(tmp_path):
    folder = tmp_path / "My Documents"
    folder.mkdir()
    probed = []
    cited = docshare.refuse_local_files(f'the folder "{folder}" and file://fileserver/share/a.md', node.Refused,
                                        is_file=lambda name: probed.append(name) or docshare._is_file_here(name))
    assert len(cited) == 2 and cited[1].startswith("file://fileserver"), cited
    assert not any("fileserver" in name for name in probed), ("a remote file: URL is never probed, and a quoted path is not "
                                                              "extended past its closing quote", probed)


@pytest.mark.parametrize("form", ["url with a line", "apostrophe", "apostrophe then prose", "parentheses", "parentheses then prose",
                                  "parenthesised citation", "url, literal apostrophe", "url, literal parentheses",
                                  "exclamation", "question", "bracketed", "braced"])
def test_review_r2_forms_of_an_existing_file_are_refused(sent, tmp_path, capsys, form):
    """Review r2 of #120's PR (local Codex and the GitHub connector): a `file:` URL kept its `:95`, and the extractor stops at an apostrophe or
    a parenthesis, so `Kxxx's.md` and `report(final).md` were looked up as fragments. Each must refuse before any send.
    MUTANT: drop the first space-free token from _candidates (range from 2); the apostrophe and parenthesis forms are sent.
    MUTANT: decode a file: URL without cutting its citation suffix; the url form is sent.
    MUTANT (review r3): decode a file: URL from the extractor's fragment only; the two literal url forms are sent.
    MUTANT (GitHub Codex on #195): cut only `.,;:)` from a name; the exclamation, question, bracketed and braced forms are sent."""
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    plain, kxxxs, final = tmp_path / "plain.md", tmp_path / "Kxxx's.md", tmp_path / "report(final).md"
    for f in (plain, kxxxs, final):
        f.write_text("x", encoding="utf-8")
    literal = lambda f: "file://" + ("/" if not str(f).startswith("/") else "") + str(f).replace(BS, "/")   # unencoded, as people type it
    body = {"url with a line": f"see {plain.as_uri()}:95", "apostrophe": f"see {kxxxs}", "apostrophe then prose": f"see {kxxxs} please",
            "parentheses": f"see {final}", "parentheses then prose": f"see {final} please",
            "parenthesised citation": f"(see {final})",
            "url, literal apostrophe": f"see {literal(kxxxs)} please", "url, literal parentheses": f"see {literal(final)} please",
            "exclamation": f"see {plain}!", "question": f"is it {plain}?", "bracketed": f"[see {plain}]", "braced": f"{{see {plain}}}"}[form]
    assert _send(body, tmp_path=tmp_path) == 2 and sent == [], (form, body)
    assert "names a FILE" in capsys.readouterr().err


def test_a_quoted_folder_is_not_refused_for_a_shorter_prefix_that_is_a_file(sent, tmp_path, capsys):
    """Review r2: quotes end a path. A quoted folder whose first word is also a file must be sent with a warning, not refused.
    MUTANT: keep the extractor fragment beside the quoted name; this send exits 2 naming the prefix."""
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "My").write_text("x", encoding="utf-8")
    (tmp_path / "My Documents").mkdir()
    assert _send(f'the folder "{tmp_path / "My Documents"}" holds it', tmp_path=tmp_path) == 0 and len(sent) == 1
    assert "local_path_cited" in capsys.readouterr().err


def test_file_urls_that_do_not_parse_or_name_a_share_are_warned_not_probed(sent, tmp_path, capsys):
    """Review r2: a URL that does not parse raised out of the guard (exit 2 "unreachable"); one with four slashes decodes to a share."""
    import nturl2path
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    assert _send("see file://[broken/path and file:////server/share/report.md", tmp_path=tmp_path) == 0 and len(sent) == 1
    assert "local_path_cited" in capsys.readouterr().err
    assert docshare._file_url_path("file:////server/share/report.md") is None
    assert docshare._file_url_path("file:////server/share/report.md", windows=True, to_path=nturl2path.url2pathname) is None
    assert docshare._file_url_path("file:///C:/Users/user/report.md:95", windows=True,
                                   to_path=nturl2path.url2pathname) == "C:" + BS + "Users" + BS + "Kxxx" + BS + "report.md"
    assert docshare._file_url_path("file:///C:/Users/user/My%20Docs/a.md", windows=True,
                                   to_path=nturl2path.url2pathname) == "C:" + BS + "Users" + BS + "Kxxx" + BS + "My Docs" + BS + "a.md"


def test_a_name_is_looked_up_once_per_message_and_the_lookups_are_capped(tmp_path):
    """Review r2: a body repeating one path made hundreds of lookups. Each name is looked up once; past the cap an unsettled path is refused,
    the guard's behaviour before 2026-10-10, so a long body neither slows send without bound nor waves an unchecked path through.
    MUTANT: drop the per-message cache; the repeated path is looked up 40 times. MUTANT: wave unsettled paths through; the capped call returns."""
    calls = []
    body = " ".join(["see /tmp/meshy-guard-absent-a.md"] * 40)
    assert docshare.refuse_local_files(body, node.Refused, is_file=lambda n: calls.append(n) or False) == ["/tmp/meshy-guard-absent-a.md"]
    assert len(calls) == len(set(calls)), calls
    with pytest.raises(node.Refused) as err:
        docshare.refuse_local_files("see /tmp/meshy-guard-absent-a.md and /tmp/meshy-guard-absent-b.md", node.Refused, is_file=lambda n: False, max_probes=2)
    assert "/tmp/meshy-guard-absent-b.md" in str(err.value)


def test_a_unc_share_is_never_probed(tmp_path):
    """An unreachable SMB host stalls a probe for its timeouts, so a UNC path is warned about without one."""
    probed = []
    unc = BS * 2 + "host" + BS + "share" + BS + "f.txt"
    assert docshare.refuse_local_files("at " + unc, node.Refused, is_file=lambda p: probed.append(p) or True) == [unc]
    assert probed == []


def test_send_lets_an_inline_body_and_a_citation_through(sent, tmp_path):
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    assert _send("kxxx-owner/docs@abc123def456:handoff/kxxx/a.md is the plan", tmp_path=tmp_path) == 0
    assert len(sent) == 1


def test_the_override_sends_it_anyway(sent, tmp_path, capsys):
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    notes = tmp_path / "n.md"
    notes.write_text("x", encoding="utf-8")
    assert _send(f"at {notes}", "--allow-local-path", tmp_path=tmp_path) == 0
    assert len(sent) == 1
    assert "local_path_cited" not in capsys.readouterr().err, "the override asks for no warning either"


# -------------------------------------------------------------------------------------------------------------------------------------- share

def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.fixture()
def docs(tmp_path):
    """A bare 'remote' and a clean clone of it with one commit on main."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.name", "t")
    _git(seed, "config", "user.email", "t@example.invalid")
    (seed / "README.md").write_text("docs\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "HEAD:main")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.name", "t")
    _git(clone, "config", "user.email", "t@example.invalid")
    return bare, clone


def test_share_commits_pushes_and_returns_a_citation_that_reads_back(docs, tmp_path):
    bare, clone = docs
    f = tmp_path / "plan.md"
    f.write_text("the plan, v1\n", encoding="utf-8")
    out = docshare.share(f, clone, alias="kxxx")
    assert out["pushed"] is True and out["branch"] == "handoff/kxxx" and out["path"] == "handoff/kxxx/plan.md"
    # The remote really holds that commit on that branch, and the file at that commit is the file.
    assert _git(bare, "rev-parse", "refs/heads/handoff/kxxx") == out["sha"]
    assert _git(bare, "show", "%s:%s" % (out["sha"], out["path"])) == "the plan, v1"
    assert out["citation"].endswith("@%s:handoff/kxxx/plan.md" % out["sha"][:12])
    # main is untouched: a handoff never lands on the default branch.
    assert _git(bare, "log", "--format=%s", "refs/heads/main") == "init"


def test_the_same_document_twice_is_not_committed_twice(docs, tmp_path):
    _bare, clone = docs
    f = tmp_path / "plan.md"
    f.write_text("same\n", encoding="utf-8")
    first = docshare.share(f, clone, alias="kxxx")
    again = docshare.share(f, clone, alias="kxxx")
    assert again["sha"] == first["sha"] and again["path"] == first["path"]


def test_a_different_document_of_the_same_name_does_not_overwrite(docs, tmp_path):
    bare, clone = docs
    f = tmp_path / "plan.md"
    f.write_text("v1\n", encoding="utf-8")
    first = docshare.share(f, clone, alias="kxxx")
    f.write_text("v2, changed\n", encoding="utf-8")
    second = docshare.share(f, clone, alias="kxxx")
    assert second["path"] != first["path"] and second["path"].startswith("handoff/kxxx/plan-")
    assert _git(bare, "show", "%s:%s" % (second["sha"], first["path"])) == "v1", "the first document is still there, unchanged"
    assert _git(bare, "show", "%s:%s" % (second["sha"], second["path"])) == "v2, changed"


def test_share_refuses_a_dirty_clone_a_bad_alias_and_a_missing_file(docs, tmp_path):
    _bare, clone = docs
    f = tmp_path / "plan.md"
    f.write_text("x\n", encoding="utf-8")
    (clone / "stray.txt").write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(docshare.ShareFailed, match="uncommitted"):
        docshare.share(f, clone, alias="kxxx")
    (clone / "stray.txt").unlink()
    with pytest.raises(docshare.ShareFailed, match="alias"):
        docshare.share(f, clone, alias="../evil")
    with pytest.raises(docshare.ShareFailed, match="no such file"):
        docshare.share(tmp_path / "missing.md", clone, alias="kxxx")
    with pytest.raises(docshare.ShareFailed, match="relative folder"):
        docshare.share(f, clone, alias="kxxx", dest="../up")
    with pytest.raises(docshare.ShareFailed, match="relative folder"):
        docshare.share(f, clone, alias="kxxx", dest="docs/../../up")              # a `..` in the middle passes the character class; only the segment check stops it


def test_a_failed_push_is_reported_and_nothing_is_cited(docs, tmp_path):
    bare, clone = docs
    f = tmp_path / "plan.md"
    f.write_text("x\n", encoding="utf-8")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    with pytest.raises(docshare.ShareFailed, match="push"):
        docshare.share(f, clone, alias="kxxx")


def test_the_share_command_prints_json_and_exits_zero_without_a_node_identity(docs, tmp_path, capsys):
    _bare, clone = docs
    f = tmp_path / "plan.md"
    f.write_text("cli\n", encoding="utf-8")
    rc = cli.main(["share", str(f), "--clone", str(clone), "--alias", "kxxx"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["pushed"] is True and out["path"] == "handoff/kxxx/plan.md"


def test_repo_slug_parses_the_remotes_seats_use():
    s = docshare._repo_slug
    assert s("https://github.com/sampleco/MeshyChat.git") == "sampleco/MeshyChat"
    assert s("https://github.com/sampleco/ASDLC") == "sampleco/ASDLC"
    assert s("git@github.com:sampleco/ASDLC.git") == "sampleco/ASDLC"
    assert s("/some/local/path.git") is None


def test_a_remote_that_does_not_hold_the_commit_is_not_cited(docs, tmp_path, monkeypatch):
    """The push said yes, the read-back says the branch is somewhere else: the citation would point at nothing, so `share` refuses to print it."""
    _bare, clone = docs
    f = tmp_path / "plan.md"
    f.write_text("x\n", encoding="utf-8")
    real = docshare._git

    def lying(clone_, *args, **kw):
        r = real(clone_, *args, **kw)
        if args[:1] == ("ls-remote",):
            return subprocess.CompletedProcess(args, 0, stdout="0" * 40 + "\trefs/heads/handoff/kxxx\n", stderr="")
        return r
    monkeypatch.setattr(docshare, "_git", lying)
    with pytest.raises(docshare.ShareFailed, match="do not cite"):
        docshare.share(f, clone, alias="kxxx")


def test_an_occupied_hash_suffixed_name_with_other_bytes_is_never_cited(docs, tmp_path):
    """Review of #42, P2 1. The plain name holds one document, and the name with the first 8 hex of the NEW content's hash is already taken by an UNRELATED file. The citation must
    not point at that file: share moves on to a longer suffix, and the bytes at the cited path are the ones shared. (Mutant: accept any suffixed name: this fails.)"""
    import hashlib
    bare, clone = docs
    f = tmp_path / "report.md"
    f.write_text("A\n", encoding="utf-8")
    docshare.share(f, clone, alias="kxxx")                                # handoff/kxxx/report.md = A, and the clone is now on handoff/kxxx
    f.write_text("C, the change\n", encoding="utf-8")
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    squat = clone / "handoff" / "kxxx" / ("report-%s.md" % digest[:8])
    squat.write_text("an unrelated document\n", encoding="utf-8")
    _git(clone, "add", "--", squat.relative_to(clone).as_posix())
    _git(clone, "commit", "-q", "-m", "an unrelated file that happens to have that name")
    out = docshare.share(f, clone, alias="kxxx")
    assert out["path"] == "handoff/kxxx/report-%s.md" % digest[:16], out["path"]
    assert _git(bare, "show", "%s:%s" % (out["sha"], out["path"])) == "C, the change"
    assert _git(bare, "show", "%s:%s" % (out["sha"], "handoff/kxxx/report-%s.md" % digest[:8])) == "an unrelated document", "the unrelated file is untouched"


def test_the_bytes_committed_are_the_ones_read_before_the_checkout(docs, tmp_path):
    """Review of #42, P2 2. The source is a TRACKED file inside the clone, and the handoff branch holds an OLDER version of it: checking the branch out rewrites the source on disk.
    What is committed and cited must be what the caller shared (the newer bytes), not what the checkout left behind. (Mutant: read the source after the checkout: this fails.)"""
    bare, clone = docs
    _git(clone, "checkout", "-q", "-b", "handoff/kxxx")
    (clone / "doc.md").write_text("old\n", encoding="utf-8")
    _git(clone, "add", "doc.md")
    _git(clone, "commit", "-q", "-m", "an older doc.md on the handoff branch")
    _git(clone, "checkout", "-q", "main")
    (clone / "doc.md").write_text("new\n", encoding="utf-8")
    _git(clone, "add", "doc.md")
    _git(clone, "commit", "-q", "-m", "the newer doc.md on main")
    out = docshare.share(clone / "doc.md", clone, alias="kxxx")
    assert _git(bare, "show", "%s:%s" % (out["sha"], out["path"])) == "new"
