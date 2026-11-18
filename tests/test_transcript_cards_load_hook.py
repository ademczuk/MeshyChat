"""tools/transcript_cards/load_hook.py: a seat's card load, started from its Claude Code hooks (exxxx-session's gap
register, gap 1: the stores lagged the sessions by 32 to 48 hours because the load was run by hand).

Both stages are fake scripts written into tmp_path, with three exceptions that touch nothing real: one run of the
real loader over a synthetic transcript in tmp_path, and two runs of the real `card write` that cannot reach any
server (a state file naming a closed local port; a state file that is not there). No test reads a transcript
folder of this machine or talks to a server, and no test runs git.

Times: a receipt is written at the REAL clock minus an offset, and the worker reads the real clock. The exact
bounds are tested on the pure functions (`throttled`, `summary`), which are given both instants.

Run:  python -m pytest tests/test_transcript_cards_load_hook.py -q -p no:cacheprovider
"""
from __future__ import annotations

import builtins
import io
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools.transcript_cards import load_hook as lh

#: A card's words that must never reach a receipt or a printed line.
CANARY = "zqcanary7f3e the operator said burn the boats"
CANARY_MARK = "zqcanary7f3e"
STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
SCRIPT = os.path.abspath(lh.__file__)
ROOT = lh.REPO

FAKE = r'''import json, os, sys, time
CFG = __CFG__
argv = sys.argv[1:]


def opt(name):
    for i, a in enumerate(argv):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


if CFG.get("log"):
    with open(CFG["log"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
inside = None
if CFG.get("inside"):
    inside = os.path.join(CFG["inside"], "inside")
    try:
        os.close(os.open(inside, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        open(os.path.join(CFG["inside"], "OVERLAP"), "w").close()
        inside = None
# The hook passes the folder as canonical() gives it: normcase lowers it on Windows (txxxx, #217 on HQB), so
# the gate compares both sides normcased.
gated = not CFG.get("gate_on") or os.path.normcase(CFG["gate_on"]) in os.path.normcase(opt("--project-dir") or "")
if CFG.get("started") and gated:
    open(CFG["started"], "w").close()
if CFG.get("wait_for") and gated:
    end = time.time() + 60
    while not os.path.exists(CFG["wait_for"]) and time.time() < end:
        time.sleep(0.02)
if CFG.get("sleep"):
    time.sleep(CFG["sleep"])
if CFG.get("done"):
    open(CFG["done"], "w").close()
if CFG.get("cards") is not None:
    out = opt("--out")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "cards.jsonl"), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(c) + "\n" for c in CFG["cards"]))
if inside:
    os.unlink(inside)
sys.stdout.buffer.write(CFG.get("stdout", "").encode("utf-8"))  # bytes: no CRLF on Windows
sys.stderr.write(CFG.get("stderr", ""))
sys.stdout.flush()
code = CFG.get("exit", 0)
if CFG.get("fail_on") and CFG["fail_on"] in (opt("--project-dir") or ""):
    code = CFG.get("fail_exit", 1)
sys.exit(code)
'''


def card(n, text=CANARY, seat="pxx"):
    """A card of the loader's shape; its words are the canary."""
    return {"schema": "card.v1", "id": ("%x" % n) * 32, "type": "Candidate", "owner": "operator", "seat": seat,
            "date": "2026-10-10", "time": "2026-10-10T05:0%d:00.000Z" % n, "status": "active",
            "text": "Candidate | owner: operator | seat: %s | %s" % (seat, text)}


REPORT = {"cards_total": 2, "new_cards": 1, "files": 1, "seat": "pxx", "files_read": [CANARY]}
SUMMARY = {"cards": 2, "outcomes": {"written": 1, "exists": 1}, "written": ["card/pxx/" + "1" * 32],
           "problems": [], "seconds": 0.1}


def _no_git_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class Seat:
    """One seat's folders in tmp_path, a fake loader that writes two cards, and a fake `card write` that reports
    one written and one there already."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.proj = tmp_path / "proj"
        self.proj.mkdir()
        self.store = tmp_path / "store"
        self.loader_log = tmp_path / "loader.log"
        self.writer_log = tmp_path / "writer.log"
        self.loader = self.fake("loader.py", log=str(self.loader_log), cards=[card(1), card(2)],
                                stdout=json.dumps(REPORT))
        self.writer = self.fake("writer.py", log=str(self.writer_log), stdout=json.dumps(SUMMARY))
        self.cleanups = []

    def fake(self, name, declares_seat_file=False, **cfg) -> str:
        path = self.tmp / name
        text = FAKE.replace("__CFG__", repr(cfg))
        if declares_seat_file == "append":         # txxxx's flag, taken more than once
            text += '\n# ap.add_argument("--seat-file", action="append",\n#                 help="a transcript (of this seat)")\n'
        elif declares_seat_file:                   # txxxx's flag, taken once
            text += '\n# ap.add_argument("--seat-file", help="a transcript of this seat\'s")\n'
        path.write_text(text, encoding="utf-8")
        return str(path)

    def project(self, name) -> Path:
        p = self.tmp / name
        p.mkdir(exist_ok=True)
        return p

    def argv(self, event="UserPromptSubmit", *more, inline=True, project=True, loader=None, writer=None,
             timeout=30, project_dir=None):
        a = ([] if event is None else ["--event", event]) + [
            "--seat", "pxx", "--store", str(self.store), "--base-url", "http://stand-in.invalid",
            "--state-file", str(self.tmp / "state.json"), "--stage-timeout", str(timeout),
            "--loader", loader or self.loader, "--writer", writer or self.writer]
        if project:
            a += ["--project-dir", str(project_dir or self.proj)]
        if inline:
            a.append("--inline")
        return a + list(more)

    def hook(self, event="UserPromptSubmit", *more, stdin="{}", **kw) -> str:
        out = io.StringIO()
        assert lh.main(self.argv(event, *more, **kw), stdin=io.StringIO(stdin), stdout=out) == 0
        return out.getvalue()

    def cfg(self, **kw):
        ns = lh.parse(self.argv(**kw))
        ns.identity = "pxx"
        ns.store = str(self.store)
        ns.project_dir = str(kw.get("project_dir") or self.proj)
        return ns

    def key(self, project=None):
        return lh.request_key("pxx", lh.canonical(str(project or self.proj)))

    def pstore(self, project=None) -> Path:
        """The loader's own store of one project folder: <root>/projects/<key>/."""
        return self.store / lh.PROJECTS / self.key(project)

    def loads(self) -> list[dict]:
        return _lines(self.loader_log)

    def loaded(self) -> list[str]:
        """The project folder of each loader run, in order."""
        return [x["argv"][0].split("=", 1)[1] for x in self.loads()]

    def writes(self) -> list[dict]:
        return _lines(self.writer_log)

    def result(self, project=None) -> dict | None:
        return lh.read_json(lh.result_path(str(self.store), self.key(project)))

    def receipt(self) -> dict | None:
        return lh.read_receipt(str(self.store))

    def put_result(self, status, age_s, project=None):
        self.mkdirs(lh.RESULTS)
        Path(lh.result_path(str(self.store), self.key(project))).write_text(json.dumps(
            {"time": lh.stamp(time.time() - age_s), "status": status, "detail": "earlier", "seat": "pxx",
             "project": lh.canonical(str(project or self.proj))}), encoding="utf-8")

    def mkdirs(self, *names):
        """The store, and the named folders in it, as the worker makes them: this user's alone (0700)."""
        os.makedirs(self.store, mode=0o700, exist_ok=True)
        for name in names:
            os.makedirs(self.store / name, mode=0o700, exist_ok=True)

    def hold_lock(self):
        self.mkdirs()
        fd = lh.take_lock(str(self.store))
        assert fd is not None
        self.cleanups.append(lambda: lh.release_lock(fd))
        return fd

    def enqueue(self, project, **fields):
        """Queue a request as a hook's worker would, with any `fields` put into it as well."""
        self.mkdirs(lh.PENDING)
        req = {"project_dir": C(project), "time": time.time()}
        req.update(fields)
        assert lh.write_json(str(self.store / lh.PENDING / (self.key(project) + ".json")), req)
        return req

    def pending(self) -> list[str]:
        """The queue: requests waiting or claimed, not those set aside as .bad."""
        folder = self.store / lh.PENDING
        return sorted(n for n in os.listdir(folder) if not n.endswith(lh.BAD)) if folder.exists() else []

    def set_aside(self) -> list[str]:
        folder = self.store / lh.PENDING
        return sorted(n for n in os.listdir(folder) if n.endswith(lh.BAD)) if folder.exists() else []


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _lock_free(store) -> bool:
    """Whether the store's lock can be taken now (taken and given back at once)."""
    fd = lh.take_lock(str(store))
    if fd is None:
        return False
    lh.release_lock(fd)
    return True


def _wait_for(pred, seconds=60.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


#: No test here takes more than a few seconds; one that waits this long is hung.
TEST_BOUND_S = 90


class _Overran(Exception):
    pass


@pytest.fixture(autouse=True)
def _bounded():
    """No test of this file waits forever. POSIX: SIGALRM fails the test that runs past TEST_BOUND_S, whatever it
    is blocked in (a lock, a pipe, a child). Every platform: past twice that, faulthandler prints every thread's
    stack and ends the run with exit 1, so a hang fails the run instead of holding it."""
    import faulthandler
    import signal
    faulthandler.dump_traceback_later(2 * TEST_BOUND_S, exit=True)
    alarm = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    previous = None
    if alarm:
        def overran(signum, frame):
            raise _Overran("the test ran past %d s" % TEST_BOUND_S)

        previous = signal.signal(signal.SIGALRM, overran)
        signal.setitimer(signal.ITIMER_REAL, TEST_BOUND_S)
    try:
        yield
    finally:
        if alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture(autouse=True)
def pane(monkeypatch, tmp_path):
    """Every test runs in a pane whose identity is pxx, with the cards root and the postbox config in its own
    tmp_path: no test reads this machine's postbox config or writes under its home."""
    monkeypatch.setenv("MESHY_IDENTITY", "pxx")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MESHY_CARDS_ROOT", str(tmp_path / "cards-root"))
    monkeypatch.setenv("MESHY_POSTBOX_CONFIG", str(tmp_path / "postbox-config.json"))
    lh._SEAT_FILE.clear()
    return tmp_path


@pytest.fixture(autouse=True)
def successors(monkeypatch):
    """A worker run in this process hands off to this recorder, never to a real detached successor (a test that wants
    a real one runs its worker as a process of its own). Each call: its wait, whether the lock was free and the queue
    held something at that moment, and the successor's command."""
    calls = []

    def record(cfg, wait):
        calls.append({"wait": wait, "lock_free": _lock_free(cfg.store), "pending": lh.has_pending(cfg.store),
                      "argv": lh.successor_argv(cfg, wait)})

    monkeypatch.setattr(lh, "start_successor", record)
    return calls


@pytest.fixture()
def seat(tmp_path):
    s = Seat(tmp_path)
    yield s
    for undo in reversed(s.cleanups):
        undo()


def C(p) -> str:
    return lh.canonical(str(p))


# ---- the receipts


def test_a_load_writes_its_projects_receipt_and_the_seats(seat):
    assert seat.hook() == ""
    r = seat.result()
    assert sorted(r) == ["cards_total", "detail", "new_cards", "outcomes", "project", "seat", "status", "time"]
    assert r["status"] == "ok" and r["seat"] == "pxx" and r["project"] == C(seat.proj)
    assert r["cards_total"] == 2 and r["new_cards"] == 1 and r["outcomes"] == {"written": 1, "exists": 1}
    assert STAMP_RE.match(r["time"]) and abs(lh.instant(r["time"]) - time.time()) < 60
    assert re.fullmatch(r"written 1, exists 1 of 2 cards \(1 new\); load \d+\.\d s, write \d+\.\d s", r["detail"])
    assert seat.receipt() == r, "with one project, the seat's receipt is that project's"
    # the lock file stays; no request is left waiting; nothing temporary is left anywhere; the loader's files are in
    # the project's own store, not at the root
    assert sorted(p.name for p in seat.store.iterdir()) == ["last-result.json", "load.lock", "pending", "projects",
                                                            "results"]
    assert os.listdir(seat.store / lh.PROJECTS) == [seat.key()]
    assert sorted(os.listdir(seat.pstore())) == ["cards.jsonl"]
    assert seat.pending() == [] and os.listdir(seat.store / lh.RESULTS) == [seat.key() + ".json"]


def test_receipt_records_structured_count_fields_under_b5(seat):
    # 0 cards -> cards_total: 0, new_cards: 0, outcomes: {}
    seat.loader = seat.fake("loader0.py", cards=[], stdout=json.dumps({"cards_total": 0, "new_cards": 0}))
    assert seat.hook() == ""
    r0 = seat.result()
    assert r0["status"] == "ok"
    assert r0["cards_total"] == 0 and r0["new_cards"] == 0 and r0["outcomes"] == {}
    assert seat.receipt() == r0

    # write timeout after loader ran -> cards_total and new_cards preserved, no outcomes
    (seat.store / lh.RESULTS / (seat.key() + ".json")).unlink()
    seat.loader = seat.fake("loader3.py", cards=[card(1), card(2), card(3)],
                            stdout=json.dumps({"cards_total": 3, "new_cards": 2}))
    seat.writer = seat.fake("writer_slow.py", sleep=10.0)
    assert seat.hook(timeout=2.0) == ""
    rt = seat.result()
    assert rt["status"] == "timeout" and rt["detail"].startswith("write ran past")
    assert rt["cards_total"] == 3 and rt["new_cards"] == 2 and "outcomes" not in rt


def test_meshy_mod_receipt_constants_match_load_hook():
    js_path = Path(__file__).resolve().parent.parent / "tools" / "postbox" / "mod" / "meshy-mod" / "hooks" / "meshy.js"
    js = js_path.read_text(encoding="utf-8")
    m_marker = re.search(r'const CARDS_NO_MARKER = "([^"]+)"', js)
    assert m_marker is not None and m_marker.group(1) == lh.NO_MARKER
    m_statuses = re.search(r"const CARDS_LOAD_STATUSES = new Set\(\[([^\]]+)\]\)", js)
    assert m_statuses is not None
    js_statuses = {s.strip().strip("'\"") for s in m_statuses.group(1).split(",") if s.strip()}
    assert js_statuses == set(lh.STATUSES)
    m_clean = re.search(r"const CARDS_LOAD_CLEAN = new Set\(\[([^\]]+)\]\)", js)
    assert m_clean is not None
    js_clean = {s.strip().strip("'\"") for s in m_clean.group(1).split(",") if s.strip()}
    assert js_clean == set(lh.CLEAN)



def test_a_file_is_replaced_whole_or_not_at_all(seat, monkeypatch):
    seat.put_result("ok", 5000)
    path = lh.result_path(str(seat.store), seat.key())
    before = Path(path).read_text(encoding="utf-8")

    def refuse(src, dst):
        raise OSError("the disk said no")

    monkeypatch.setattr(lh.os, "replace", refuse)
    assert lh.write_json(path, {"status": "failed"}) is False
    assert Path(path).read_text(encoding="utf-8") == before
    assert os.listdir(seat.store / lh.RESULTS) == [seat.key() + ".json"], "the temporary file is removed"


def test_a_replace_windows_refuses_while_a_reader_has_the_file_is_tried_again(seat, monkeypatch):
    seat.mkdirs()
    real, calls = os.replace, []

    def busy_twice(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("in use")
        return real(src, dst)

    monkeypatch.setattr(lh.os, "replace", busy_twice)
    assert lh.write_json(str(seat.store / lh.RECEIPT), {"status": "ok"}) is True
    assert len(calls) == 3 and seat.receipt() == {"status": "ok"}


def test_the_receipt_time_is_utc_with_z():
    assert lh.stamp(0) == "1970-01-01T00:00:00Z"
    assert lh.instant("1970-01-01T00:00:10Z") == 10.0
    assert lh.instant("1970-01-01T01:00:10+01:00") == 10.0
    assert lh.instant("1970-01-01T00:00:10") is None and lh.instant(10) is None and lh.instant("soon") is None


def test_the_seats_receipt_shows_a_projects_failure_over_anothers_later_success(seat):
    b = seat.project("projB")
    seat.hook("SessionEnd", project_dir=b, loader=seat.fake("lb.py", exit=1))
    seat.hook("SessionEnd")
    assert seat.result()["status"] == "ok" and seat.result(b)["status"] == "failed"
    assert seat.receipt() == seat.result(b), "a good load of one project does not hide another's failed one"
    seat.hook("SessionEnd", project_dir=b)
    assert seat.receipt()["status"] == "ok" and seat.receipt()["project"] in (C(seat.proj), C(b))


def test_the_summary_bounds_exactly():
    base = lh.instant("2026-10-10T08:00:00Z")

    def r(status, age, project="p"):
        return {"time": lh.stamp(base - age), "status": status, "project": project}

    ok_new, ok_old, failed, skipped = r("ok", 10, "a"), r("ok", 100, "b"), r("failed", 50, "c"), r("skipped", 50)
    assert lh.summary([ok_old, ok_new], base) == ok_new
    assert lh.summary([ok_new, failed, ok_old], base) == failed
    assert lh.summary([skipped, ok_new], base) == ok_new, "skipped is clean: an older skipped is not shown over ok"
    day = lh.SUMMARY_WINDOW_S
    assert day == 24 * 3600, "the summary looks back one day"
    assert lh.summary([ok_new, r("failed", day)], base) == ok_new, "a failure older than a day is left out"
    assert lh.summary([ok_new, r("failed", day - 1)], base)["status"] == "failed"
    assert lh.summary([r("ok", day)], base) is None and lh.summary([], base) is None
    assert lh.summary([r("failed", -5), ok_new], base) == ok_new, "a receipt from the future is left out"
    assert lh.summary([None, {"time": "soon"}, ok_new], base) == ok_new


def test_the_summary_leaves_out_a_folder_that_holds_none_of_the_seats_transcripts():
    # vxxxxxxxxxx 10f93b, kxxx 9010e4 (#238 B3): a session started from the seat's shell in another folder writes a
    # "no transcript holds this seat's marker" receipt there; it is not the seat's project and must not hide a
    # healthy feed, nor hide a real failure of the seat's own project. Alone, it still shows.
    base = lh.instant("2026-10-12T12:00:00Z")

    def r(status, age, project, detail=""):
        return {"time": lh.stamp(base - age), "status": status, "project": project, "detail": detail}

    own_ok = r("ok", 600, "own")
    side = r("failed", 60, "side", lh.NO_MARKER)
    assert lh.summary([own_ok, side], base) == own_ok
    own_failed = r("failed", 900, "own2", "loader exit 1 after 0.4 s; nothing written")
    assert lh.summary([own_ok, side, own_failed], base) == own_failed
    held = r("held", 1200, "own3")
    assert lh.summary([side, held], base) == held
    assert lh.summary([side], base) == side, "with no project of its own, the seat's summary still says it failed"
    older_side = r("failed", 120, "side2", lh.NO_MARKER)
    assert lh.summary([older_side, side], base) == side
    assert lh.summary([own_ok, r("failed", 30, "x", "loader exit 3 after 1 s")], base)["status"] == "failed", \
        "only the exact no-marker detail is left out"
    noted = r("failed", 30, "own4", lh.NO_MARKER + "; --seat-file not passed (the loader has no such flag)")
    assert lh.summary([own_ok, noted], base) == noted, "a note means the hook named a transcript: it counts"


def test_the_summary_asks_the_project_store_whether_the_folder_ever_held_the_seats_transcripts(tmp_path):
    # local Codex review: an exit 3 can be the seat's own project. Only a folder whose loader store never recognised a
    # transcript of the seat's (cursor.json with no seat_files, or none at all) is a side session's.
    store = tmp_path / "store"
    for name in (lh.RESULTS, lh.PROJECTS):
        (store / name).mkdir(parents=True)
    now = time.time()

    def receipt(project, status, detail, age, cursor):
        r = {"time": lh.stamp(now - age), "status": status, "detail": detail, "seat": "pxx", "project": project}
        key = lh.request_key("pxx", project)
        assert lh.write_json(lh.result_path(str(store), key), r)
        if cursor is not None:
            (store / lh.PROJECTS / key).mkdir(exist_ok=True)
            (store / lh.PROJECTS / key / "cursor.json").write_text(cursor, encoding="utf-8")
        return r

    own_ok = receipt("/p/own", "ok", "exists 3 of 3 cards", 600, '{"files": {"a.jsonl": 1}, "seat_files": {"a.jsonl": 1}}')
    side = receipt("/p/side", "failed", lh.NO_MARKER, 60, '{"files": {}, "seat_files": {}}')
    assert lh.write_summary(str(store)) == own_ok
    fresh = receipt("/p/fresh-side", "failed", lh.NO_MARKER, 50, None)
    assert lh.write_summary(str(store)) == own_ok, "a folder the loader never wrote a cursor for is a side session's"
    known = receipt("/p/own2", "failed", lh.NO_MARKER, 40, '{"files": {"b.jsonl": 1}, "seat_files": {"b.jsonl": 1}}')
    assert lh.write_summary(str(store)) == known, "a folder that held the seat's transcripts is its own: it counts"
    os.unlink(lh.result_path(str(store), lh.request_key("pxx", "/p/own2")))
    broken = receipt("/p/broken", "failed", lh.NO_MARKER, 30, "not json")
    assert lh.write_summary(str(store)) == broken, "a cursor that cannot be read counts as the seat's"
    assert json.loads((store / lh.RECEIPT).read_text(encoding="utf-8")) == broken
    assert side and fresh


def test_the_stages_get_their_arguments_and_never_rebuild(seat):
    seat.hook("UserPromptSubmit", "--marker", "echo pxx-shell-ok", "--own-name", "pxxcc")
    [load] = seat.loads()
    assert load["argv"] == ["--project-dir=" + C(seat.proj), "--seat=pxx", "--out=" + str(seat.pstore()),
                            "--marker=echo pxx-shell-ok", "--own-name=pxxcc"]
    [write] = seat.writes()
    assert write["argv"] == ["card", "write", "--file", str(seat.pstore() / "cards.jsonl"),
                             "--base-url", "http://stand-in.invalid", "--state-file", str(seat.tmp / "state.json")]
    assert not any("rebuild" in a for a in load["argv"] + write["argv"])


def test_the_real_commands_are_the_loader_beside_it_and_the_client_from_its_checkout(seat):
    cfg = seat.cfg()
    cfg.loader = cfg.writer = None
    loader = lh.loader_cmd(cfg)
    assert loader[:2] == [sys.executable, os.path.join(lh.HERE, "transcript_cards.py")]
    assert os.path.isfile(loader[1])
    assert lh.writer_cmd(cfg) == [sys.executable, "-m", "tools.meshy_node", "card", "write", "--file",
                                  os.path.join(str(seat.store), "cards.jsonl"), "--base-url",
                                  "http://stand-in.invalid", "--state-file", str(seat.tmp / "state.json")]
    assert os.path.isfile(os.path.join(lh.REPO, "tools", "meshy_node", "__main__.py"))


def test_the_project_folder_is_the_hook_inputs_transcript_folder(seat):
    out = seat.hook("SessionEnd", project=False,
                    stdin=json.dumps({"hook_event_name": "SessionEnd", "transcript_path": str(seat.proj / "S1.jsonl"),
                                      "prompt": CANARY}))
    assert out == "card load: started\n"
    assert seat.loaded() == [C(seat.proj)]


def test_a_given_project_folder_is_kept_whatever_the_hook_input_says(seat):
    elsewhere = seat.project("elsewhere")
    seat.hook("SessionEnd", stdin=json.dumps({"transcript_path": str(elsewhere / "S2.jsonl")}))
    assert seat.loaded() == [C(seat.proj)]


def test_the_event_can_come_from_the_hook_input(seat):
    out = seat.hook(None, stdin=json.dumps({"hook_event_name": "PreCompact"}))
    assert out == "card load: started\n" and len(seat.loads()) == 1


def test_the_hook_input_is_read_as_utf8_whatever_the_console_says(seat):
    """Claude Code writes UTF-8; a Windows console's stdin decodes cp1252 unless PYTHONIOENCODING says otherwise."""
    folder = seat.project("proj-Zo\u00eb")
    data = json.dumps({"hook_event_name": "SessionEnd", "transcript_path": str(folder / "S1.jsonl")},
                      ensure_ascii=False).encode("utf-8")
    stdin = io.TextIOWrapper(io.BytesIO(data), encoding="cp1252")
    out = io.StringIO()
    assert lh.main(seat.argv(None, project=False), stdin=stdin, stdout=out) == 0
    assert out.getvalue() == "card load: started\n"
    assert seat.loaded() == [C(folder)]


# ---- what each outcome is called


def _status_case(seat, *, loader=None, writer=None, timeout=90):
    # 90 s per stage (was 30): issue #451, a status case read 'failed' where 'held' was expected under a full gate's load
    # (mg440a) and passed alone; the fakes return at once, so the bound only matters when the box is slow.
    seat.hook("SessionEnd", loader=loader, writer=writer, timeout=timeout)
    return seat.result()


@pytest.mark.parametrize("exit_code, stderr, status, said", [
    (0, "", "ok", "written 1, exists 1 of 2 cards"),
    (1, "", "failed", "write exit 1: written 1, exists 1 of 2 cards"),
    (2, json.dumps({"unreachable": "URLError: " + CANARY}) + "\n", "unreachable", "write exit 2, server unreachable"),
    (2, "", "unreachable", "write exit 2, server unreachable"),
    (2, json.dumps({"refused": CANARY}) + "\n", "failed", "write exit 2, refused before sending"),
    (3, "", "held", "a detector held a card: written 1, exists 1 of 2 cards"),
    (7, "", "failed", "write exit 7"),
])
def test_each_exit_of_card_write_has_its_status(seat, exit_code, stderr, status, said):
    writer = seat.fake("w.py", log=str(seat.writer_log), stdout=json.dumps(SUMMARY), stderr=stderr, exit=exit_code)
    r = _status_case(seat, writer=writer)
    assert (r["status"], r["detail"].startswith(said)) == (status, True), r
    assert len(seat.writes()) == 1


@pytest.mark.parametrize("exit_code, said", [
    (1, "loader exit 1 after "),
    (3, "loader exit 3: no transcript holds this seat's marker"),
    (9, "loader exit 9 after "),
])
def test_a_failed_loader_is_failed_and_nothing_is_written(seat, exit_code, said):
    loader = seat.fake("l.py", log=str(seat.loader_log), stdout=CANARY, stderr=CANARY, exit=exit_code)
    r = _status_case(seat, loader=loader)
    assert r["status"] == "failed" and r["detail"].startswith(said) and r["detail"].endswith("nothing written")
    assert seat.writes() == []


def test_a_loader_past_its_time_is_stopped_and_called_timeout(seat):
    done = seat.tmp / "done"
    loader = seat.fake("l.py", log=str(seat.loader_log), sleep=3, done=str(done))
    t0 = time.monotonic()
    r = _status_case(seat, loader=loader, timeout=1)
    assert time.monotonic() - t0 < 10
    assert r["status"] == "timeout" and r["detail"] == "loader ran past 1 s and was stopped; nothing written"
    assert seat.writes() == []
    assert not _wait_for(done.exists, 4), "the stopped loader went on running"


def test_a_write_past_its_time_is_stopped_and_called_timeout(seat):
    # Each stage gets the same limit, and the fake loader is a Python child that must start and finish within it: at
    # 1 s a loaded box timed the LOADER out first, 7 of 14 runs on HQB at 71-85 % CPU (memory-bxxxxx e10d8e). 6 s
    # leaves the loader room; the writer sleeps 30 s, so only the write can run past it.
    writer = seat.fake("w.py", log=str(seat.writer_log), sleep=30, stdout=json.dumps(SUMMARY))
    t0 = time.monotonic()
    r = _status_case(seat, writer=writer, timeout=6)
    assert time.monotonic() - t0 < 25
    assert r["status"] == "timeout" and r["detail"].startswith("write ran past 6 s and was stopped"), r


def test_no_cards_means_nothing_is_sent(seat):
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[], stdout=json.dumps(dict(REPORT, cards_total=0)))
    r = _status_case(seat, loader=loader)
    assert r["status"] == "ok" and r["detail"].startswith("0 cards, nothing to write")
    assert seat.writes() == [], "card write refuses an empty file with exit 2, which would read as unreachable"


def test_a_missing_project_folder_is_skipped(seat):
    gone = seat.tmp / "gone"
    assert seat.hook("SessionEnd", project_dir=gone) == "card load: started\n"
    assert seat.result(gone)["status"] == "skipped" and seat.loads() == []
    assert seat.result(gone)["detail"] == "no project folder to read"


def test_no_project_folder_at_all_is_skipped_and_said(seat):
    assert seat.hook("SessionEnd", project=False) == \
        "card load: started, with no project folder named (give --project-dir)\n"
    r = lh.read_json(lh.result_path(str(seat.store), lh.request_key("pxx", None)))
    assert r["status"] == "skipped" and r["project"] is None and seat.loads() == []


def test_a_stage_that_cannot_start_is_failed_by_its_kind_only(seat, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(seat.tmp / "no-python-here" / CANARY_MARK))
    r = lh.run_worker(seat.cfg(), "SessionEnd")
    assert r["status"] == "failed" and r["detail"] == "load could not run (FileNotFoundError)"
    assert _lock_free(seat.store)


def test_a_single_card_report_is_counted_too(seat):
    one = {"key": "card/pxx/" + "1" * 32, "outcome": "screened", "detectors": ["jwt"], "note": CANARY}
    writer = seat.fake("w.py", stdout=json.dumps(one), exit=3)
    r = _status_case(seat, writer=writer)
    assert r["status"] == "held" and r["detail"].startswith("a detector held a card: screened 1 of 2 cards")


def test_only_known_outcomes_and_whole_counts_reach_the_detail():
    answer = {"cards": 9, "outcomes": {"written": 2, CANARY: 5, "exists": "many", "refused": True, "closed": 0,
                                       "unanswered": 1}}
    assert lh.outcome_counts(answer) == {"written": 2, "unanswered": 1}
    assert lh.outcome_counts({"outcome": CANARY}) == {} and lh.outcome_counts([1]) == {}


# ---- the throttle of a prompt, per project


@pytest.mark.parametrize("status, age, runs", [
    ("ok", 890, False), ("ok", 910, True),
    ("failed", 290, False), ("failed", 310, True),
    ("unreachable", 290, False), ("held", 600, True), ("timeout", 310, True),
    ("ok", 100, False),
])
def test_a_prompt_loads_only_when_its_projects_receipt_is_old_enough(seat, status, age, runs):
    seat.put_result(status, age)
    assert seat.hook("UserPromptSubmit") == ""
    assert len(seat.loads()) == (1 if runs else 0)


def test_the_throttle_bounds_exactly():
    base = lh.instant("2026-10-10T08:00:00Z")
    ok = {"time": "2026-10-10T08:00:00Z", "status": "ok"}
    failed = dict(ok, status="failed")
    assert lh.throttled(ok, base + 899.999) == "ok"
    assert lh.throttled(ok, base + 900) is None
    assert lh.throttled(failed, base + 299.999) == "recent"
    assert lh.throttled(failed, base + 300) is None
    assert lh.throttled(failed, base + 600) is None
    assert lh.throttled(ok, base) == "ok"
    assert lh.throttled(failed, base - 1) is None, "a receipt from the future throttles nothing"
    assert lh.throttled(None, base) is None and lh.throttled({"time": "soon", "status": "ok"}, base) is None


def test_the_throttle_is_the_projects_own(seat):
    """Project A has just loaded (its receipt and the seat's summary say ok); project B never was. B's prompt loads
    B, and A's stays quiet."""
    b = seat.project("projB")
    seat.hook("SessionEnd")
    assert seat.receipt()["status"] == "ok" and seat.loaded() == [C(seat.proj)]
    seat.hook("UserPromptSubmit", project_dir=b)
    assert seat.loaded() == [C(seat.proj), C(b)]
    seat.hook("UserPromptSubmit")
    assert seat.loaded() == [C(seat.proj), C(b)]


def test_a_project_folder_is_one_folder_however_it_is_spelled(seat):
    """The worker names a folder canonically, so two spellings of one folder are one request and one receipt."""
    spellings = [str(seat.tmp / "x" / ".." / "proj")]
    try:
        os.symlink(str(seat.proj), str(seat.tmp / "link"), target_is_directory=True)
        spellings.append(str(seat.tmp / "link"))
    except (OSError, NotImplementedError):
        pass                                       # Windows without the symlink right: the first spelling only
    for spelling in spellings:
        lh.run_worker(seat.cfg(project_dir=spelling), "SessionEnd")
    assert seat.loaded() == [C(seat.proj)] * len(spellings)
    assert os.listdir(seat.store / lh.RESULTS) == [seat.key() + ".json"]


def test_a_quiet_prompt_still_takes_what_others_left_waiting(seat):
    b = seat.project("projB")
    seat.put_result("ok", 60)
    seat.enqueue(b)
    seat.hook("UserPromptSubmit")
    assert seat.loaded() == [C(b)] and seat.pending() == []


@pytest.mark.parametrize("event", ["PreCompact", "SessionEnd", "SessionStart"])
@pytest.mark.parametrize("status", ["ok", "unreachable"])
def test_the_other_events_load_whatever_the_receipt_says(seat, event, status):
    seat.put_result(status, 5)
    seat.hook(event, "--no-brief")
    assert len(seat.loads()) == 1 and seat.result()["status"] == "ok"


# ---- the lock


def test_the_lock_has_one_holder_at_a_time_and_its_file_stays(seat):
    seat.mkdirs()
    first = lh.take_lock(str(seat.store))
    assert first is not None
    assert lh.take_lock(str(seat.store)) is None, "a second open of the file does not get the lock"
    lh.release_lock(first)
    assert (seat.store / lh.LOCK).exists(), "release leaves the file: a deleted lock file is a second lock"
    second = lh.take_lock(str(seat.store))
    assert second is not None
    lh.release_lock(second)


def test_a_dead_holder_holds_no_lock(seat):
    seat.mkdirs()
    code = ("import sys, time; sys.path.insert(0, %r); from tools.transcript_cards import load_hook as lh; "
            "fd = lh.take_lock(%r); print('held' if fd is not None else 'not', flush=True); time.sleep(60)"
            % (ROOT, str(seat.store)))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=_no_git_env())
    try:
        assert p.stdout.readline().strip() == "held"
        assert lh.take_lock(str(seat.store)) is None
    finally:
        p.kill()
        p.wait(30)
        p.stdout.close()
    assert _wait_for(lambda: _lock_free(seat.store), 10)


def test_two_workers_never_load_at_once(seat):
    """Two worker processes started together on one store, for two projects: one load at a time, each project
    loaded once."""
    b = seat.project("projB")
    inside = seat.project("inside-probe")
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       inside=str(inside), sleep=1)
    procs = []
    for project in (seat.proj, b):
        argv = seat.argv(None, inline=False, loader=loader, project_dir=project) + ["--run", "--event=SessionEnd"]
        procs.append(subprocess.Popen([sys.executable, SCRIPT] + argv, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_no_git_env()))
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert (p.returncode, err) == (0, b"")
    assert not (inside / "OVERLAP").exists(), "two loaders ran at once"
    assert sorted(seat.loaded()) == sorted([C(seat.proj), C(b)])
    assert seat.pending() == []


@pytest.mark.parametrize("event", ["UserPromptSubmit", "SessionStart"])
def test_a_running_load_means_no_load_and_no_request_for_prompt_and_start(seat, event):
    seat.hold_lock()
    assert seat.hook(event, "--no-brief") == ""
    assert seat.loads() == [] and seat.pending() == []


@pytest.mark.parametrize("event", ["PreCompact", "SessionEnd"])
def test_compact_and_end_wait_in_the_queue_while_a_load_runs(seat, event):
    fd = seat.hold_lock()
    assert seat.hook(event) == "card load: started\n"
    assert seat.loads() == [] and seat.pending() == [seat.key() + ".json"]
    lh.release_lock(fd)
    seat.cleanups.clear()
    seat.hook("SessionStart", "--no-brief", project_dir=seat.project("other"))
    assert seat.loaded() == [C(seat.proj), C(seat.tmp / "other")], "the waiting request first, then its own"


def test_a_stage_never_writes_after_its_workers_lock_is_gone(seat):
    """A worker killed in the middle of its loader stage. POSIX: the stage holds the locked descriptor too, so the
    lock stays held until the stage ends and no second loader starts beside it. Windows: the stage is in the
    worker's kill-on-close job, so it dies with the worker, and the lock with both (to be run on HQB)."""
    started, release, done = seat.tmp / "started", seat.tmp / "release", seat.tmp / "done"
    loader = seat.fake("l.py", log=str(seat.loader_log), started=str(started), wait_for=str(release),
                       done=str(done), cards=[card(1)], stdout=json.dumps(REPORT))
    argv = seat.argv(None, inline=False, loader=loader) + ["--run", "--event=SessionEnd"]
    p = subprocess.Popen([sys.executable, SCRIPT] + argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=_no_git_env())
    try:
        assert _wait_for(started.exists, 30)
        p.kill()
        p.wait(30)
        if os.name == "nt":
            assert _wait_for(lambda: _lock_free(seat.store), 10), "the worker's death frees its lock"
        else:
            assert not _lock_free(seat.store), "the killed worker's stage still holds the lock"
    finally:
        release.touch()
    if os.name == "nt":
        assert not _wait_for(done.exists, 3), "the stage died with its worker: it never wrote after the lock went"
    else:
        assert _wait_for(done.exists, 30), "the stage ran on, holding the lock"
    assert _wait_for(lambda: _lock_free(seat.store), 30)


def test_a_stage_shares_the_locked_descriptor_on_posix():
    assert lh.stage_kwargs(windows=False, lock_fd=7) == {"pass_fds": (7,)}
    assert lh.stage_kwargs(windows=False) == {}
    assert lh.stage_kwargs(windows=True, lock_fd=7) == {"creationflags": 0x08000000}


# ---- the queue


def test_a_project_that_ends_during_another_projects_load_is_loaded(seat):
    """Project A's load is in its writer stage; project B's session ends; B is loaded, with its own folder."""
    b = seat.project("projB")
    in_write, release = seat.tmp / "in-write", seat.tmp / "release"
    writer = seat.fake("w.py", log=str(seat.writer_log), stdout=json.dumps(SUMMARY), started=str(in_write),
                       wait_for=str(release))
    cfg = seat.cfg(writer=writer)
    t = threading.Thread(target=lh.run_worker, args=(cfg, "SessionEnd"), name="card-load-A")
    t.start()
    try:
        assert _wait_for(in_write.exists, 30)
        assert seat.hook("SessionEnd", project_dir=b, writer=writer) == "card load: started\n"
        assert seat.pending() == sorted([seat.key() + ".json" + lh.CLAIMED, seat.key(b) + ".json"]), \
            "A's request is claimed for its pass; B's waits"
    finally:
        release.touch()
        t.join(60)
    assert not t.is_alive()
    assert seat.loaded() == [C(seat.proj), C(b)]
    assert seat.result()["status"] == seat.result(b)["status"] == "ok"
    assert seat.result(b)["project"] == C(b) and seat.pending() == []


def test_a_request_names_its_folder_and_nothing_else(seat):
    """Whatever else a request file says (a seat, a server, a state file, markers, programs), the pass is the
    worker's own: a file in the queue can choose a folder to read, never whose key signs or what runs."""
    b = seat.project("projB")
    seat.enqueue(b, seat="someone-else", base_url="http://other.invalid", state_file=str(seat.tmp / "their.json"),
                 loader=str(seat.tmp / "evil.py"), writer=str(seat.tmp / "evil.py"), marker=["echo b-shell-ok"],
                 own_name=["intruder"], stage_timeout=99999)
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded() == [C(b), C(seat.proj)] and seat.result(b)["status"] == "ok"
    assert seat.result(b)["seat"] == "pxx"
    assert seat.loads()[0]["argv"] == ["--project-dir=" + C(b), "--seat=pxx", "--out=" + str(seat.pstore(b))]
    assert seat.writes()[0]["argv"][-4:] == ["--base-url", "http://stand-in.invalid", "--state-file",
                                             str(seat.tmp / "state.json")]


def test_a_request_left_by_a_dead_worker_is_taken_first(seat):
    b = seat.project("projB")
    seat.mkdirs(lh.PENDING)
    req = {"project_dir": C(b), "time": time.time()}
    lh.write_json(str(seat.store / lh.PENDING / (seat.key(b) + ".json" + lh.CLAIMED)), req)
    seat.hook("SessionEnd")
    assert seat.loaded() == [C(b), C(seat.proj)] and seat.pending() == []


def test_passes_are_bounded_and_the_rest_is_handed_to_a_successor(seat, successors):
    projects = [seat.project("p%d" % i) for i in range(4)]
    for i, p in enumerate(projects):
        seat.enqueue(p, time=1000 + i)
    seat.put_result("ok", 60)                      # the worker's own project is quiet: it takes only the queue
    lh.run_worker(seat.cfg(), "UserPromptSubmit")
    assert seat.loaded() == [C(p) for p in projects[:3]] and len(seat.loaded()) == lh.MAX_PASSES == 3
    assert seat.pending() == [seat.key(projects[3]) + ".json"], "what the budget did not reach waits"
    assert [(c["wait"], c["lock_free"], c["pending"]) for c in successors] == [(0, True, True)], \
        "one successor, at once, after the lock was released"
    lh.run_worker(seat.cfg(), "UserPromptSubmit")
    assert seat.loaded()[3:] == [C(projects[3])] and seat.pending() == []


def test_a_request_made_as_the_lock_was_released_is_still_taken(seat, monkeypatch):
    b = seat.project("projB")
    real, asked = lh.release_lock, []

    def ask_then_release(fd):
        if not asked:
            asked.append(1)
            seat.enqueue(b)
        real(fd)

    monkeypatch.setattr(lh, "release_lock", ask_then_release)
    lh.run_worker(seat.cfg(), "SessionEnd")
    assert seat.loaded() == [C(seat.proj), C(b)] and seat.pending() == []


def test_a_request_for_the_same_project_is_one_request(seat):
    fd = seat.hold_lock()
    seat.hook("SessionEnd")
    seat.hook("PreCompact")
    assert seat.pending() == [seat.key() + ".json"]
    lh.release_lock(fd)
    seat.cleanups.clear()
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded() == [C(seat.proj)]


def test_a_file_in_the_queue_that_is_no_request_is_dropped(seat):
    seat.mkdirs(lh.PENDING)
    (seat.store / lh.PENDING / "junk.json").write_text("{", encoding="utf-8")
    (seat.store / lh.PENDING / "other.json").write_text(json.dumps({"seat": "pxx"}), encoding="utf-8")
    seat.hook("SessionEnd")
    assert seat.loaded() == [C(seat.proj)] and seat.pending() == []


# ---- the foreground touches no store


def test_the_foreground_never_touches_the_store(seat, monkeypatch):
    store = str(seat.store)
    touched, spawned = [], []

    def guard(name, real):
        def wrapped(*a, **kw):
            p = a[0] if a else kw.get("path", kw.get("file"))
            if isinstance(p, (str, bytes, os.PathLike)) and os.fsdecode(os.fspath(p)).startswith(store):
                touched.append((name, os.fsdecode(os.fspath(p))))
            return real(*a, **kw)
        return wrapped

    for name in ("open", "stat", "lstat", "listdir", "scandir", "makedirs", "mkdir", "replace", "rename",
                 "unlink", "utime"):
        monkeypatch.setattr(os, name, guard("os." + name, getattr(os, name)))
    monkeypatch.setattr(builtins, "open", guard("open", builtins.open))
    monkeypatch.setattr(lh, "spawn", lambda cfg, event: spawned.append((event, cfg.store, cfg.project_dir)))
    outs = []
    for event in lh.EVENTS:
        out = io.StringIO()
        assert lh.main(seat.argv(event, "--no-brief", inline=False), stdin=io.StringIO("{}"), stdout=out) == 0
        outs.append(out.getvalue())
    assert touched == []
    assert spawned == [(e, store, str(seat.proj)) for e in lh.EVENTS]
    assert outs == ["", "card load: started\n", "card load: started\n", ""]
    assert not seat.store.exists()


def test_a_pipe_where_a_receipt_goes_holds_neither_the_hook_nor_the_worker(seat):
    """The real hook and its detached worker. Where the platform has named pipes, the project's receipt and the
    seat's are pipes nobody writes: the worker opens neither as a file (a plain open would wait for ever), takes
    them for no receipt, loads, and puts real receipts in their place."""
    pipes = []
    if hasattr(os, "mkfifo"):
        seat.mkdirs(lh.PENDING, lh.RESULTS)
        pipes = [lh.result_path(str(seat.store), seat.key()), str(seat.store / lh.RECEIPT)]
        for pipe in pipes:
            os.mkfifo(pipe)
    argv = seat.argv("UserPromptSubmit", inline=False)
    try:
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, SCRIPT] + argv, input="{}", capture_output=True, text=True, timeout=60,
                           env=_no_git_env())
        assert (p.returncode, p.stdout, p.stderr) == (0, "", "") and time.monotonic() - t0 < 10
        # The project's receipt is written first and the seat's summary just after it: wait for both.
        assert _wait_for(lambda: _regular(lh.result_path(str(seat.store), seat.key()))
                         and (seat.result() or {}).get("status") == "ok"
                         and _regular(seat.store / lh.RECEIPT) and (seat.receipt() or {}).get("status") == "ok", 60)
        assert seat.loaded() == [C(seat.proj)]
    finally:
        for pipe in pipes:
            if not _regular(pipe):
                _open_writer(pipe)                 # nothing should wait on it; if a regression did, it is let go


def _regular(path) -> bool:
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _open_writer(fifo):
    """Unblock the worker's read: open the pipe for writing (only once a reader has it open), write {}, close."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.write(fd, b"{}")
    os.close(fd)
    return True


def test_a_stdin_nobody_closes_cannot_hold_the_hook(seat, monkeypatch):
    monkeypatch.setattr(lh, "STDIN_WAIT_S", 0.5)
    r, w = os.pipe()
    stdin = io.TextIOWrapper(os.fdopen(r, "rb"), encoding="utf-8")
    closed = []

    def close_writer():                            # once: by the timer (a hook with no bound), or by the test
        if not closed:
            closed.append(1)
            os.close(w)

    closer = threading.Timer(6, close_writer)
    closer.start()
    try:
        out = io.StringIO()
        t0 = time.monotonic()
        assert lh.main(seat.argv("SessionEnd"), stdin=stdin, stdout=out) == 0
        assert time.monotonic() - t0 < 4
        assert out.getvalue() == "card load: started\n" and len(seat.loads()) == 1
    finally:
        closer.cancel()
        closer.join()
        close_writer()
        for t in [t for t in threading.enumerate() if t.name == "card-load-stdin"]:
            t.join(10)
        stdin.close()
    assert not [t for t in threading.enumerate() if t.name == "card-load-stdin"]


def test_stdin_is_read_up_to_its_limit(seat, monkeypatch):
    monkeypatch.setattr(lh, "STDIN_LIMIT", 64)
    big = json.dumps({"transcript_path": str(seat.proj / "S1.jsonl"), "pad": "x" * 200})
    assert seat.hook("SessionEnd", project=False, stdin=big) == \
        "card load: started, with no project folder named (give --project-dir)\n"


# ---- what is printed, and the exit code


def _scenario(seat, monkeypatch, name, event):
    """Run the hook for `event` in one of the situations a seat meets; return (stdout, main's return)."""
    argv = seat.argv(event)
    stdin = "{}"
    if name == "throttled":
        seat.put_result("ok", 10)
    elif name == "locked":
        seat.hold_lock()
    elif name == "no project":
        argv = seat.argv(event, project=False)
    elif name == "bad stdin":
        stdin = "{not json" + CANARY
    elif name == "stdin not an object":
        stdin = json.dumps([CANARY])
    elif name == "bad args":
        argv = argv + ["--stage-timeout", "soon"]
    elif name == "unknown option":
        argv = argv + ["--no-such-option"]
    elif name == "unwritable store":
        (seat.tmp / "a-file").write_text("x", encoding="utf-8")
        argv = [str(seat.tmp / "a-file" / "store") if a == str(seat.store) else a for a in argv]
    elif name == "stage fails":
        argv = seat.argv(event, loader=seat.fake("l.py", exit=1, stdout=CANARY, stderr=CANARY))
    elif name == "it raises":
        def boom(cfg, event):
            raise RuntimeError(CANARY)
        monkeypatch.setattr(lh, "hook", boom)
    if event == "SessionStart":
        argv.append("--no-brief")
    out = io.StringIO()
    rc = lh.main(argv, stdin=io.StringIO(stdin), stdout=out)
    return out.getvalue(), rc


SCENARIOS = ["starts", "throttled", "locked", "no project", "bad stdin", "stdin not an object", "bad args",
             "unknown option", "unwritable store", "stage fails", "it raises"]


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("event", ["UserPromptSubmit", "SessionStart"])
def test_the_prompt_and_no_brief_start_hooks_print_nothing_at_all(seat, monkeypatch, capfd, name, event):
    out, rc = _scenario(seat, monkeypatch, name, event)
    captured = capfd.readouterr()
    assert (out, rc, captured.out, captured.err) == ("", 0, "", "")


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("event", ["PreCompact", "SessionEnd"])
def test_the_compact_and_end_hooks_print_exactly_one_line(seat, monkeypatch, capfd, name, event):
    out, rc = _scenario(seat, monkeypatch, name, event)
    captured = capfd.readouterr()
    assert rc == 0 and captured.out == "" and captured.err == ""
    assert out.count("\n") == 1 and out.endswith("\n") and out.startswith("card load: "), out
    assert CANARY_MARK not in out


def test_the_lines_say_what_happened(seat, monkeypatch):
    assert seat.hook("SessionEnd") == "card load: started\n"
    out, _ = _scenario(seat, monkeypatch, "no project", "PreCompact")
    assert out == "card load: started, with no project folder named (give --project-dir)\n"
    out, _ = _scenario(seat, monkeypatch, "bad args", "SessionEnd")
    assert out.startswith("card load: not started, bad arguments (")
    out, _ = _scenario(seat, monkeypatch, "it raises", "SessionEnd")
    assert out == "card load: not started (RuntimeError)\n"


@pytest.mark.parametrize("event", [None, "Stop", "SubagentStop"])
def test_an_event_it_does_not_serve_does_nothing(seat, event):
    out = io.StringIO()
    argv = [a for a in seat.argv(None)] + ([] if event is None else ["--event=" + event])
    assert lh.main(argv, stdin=io.StringIO(json.dumps({"hook_event_name": event or ""})), stdout=out) == 0
    assert out.getvalue() == "" and seat.loads() == [] and not seat.store.exists()


def test_a_missing_store_is_made_by_the_worker(seat):
    assert not seat.store.exists()
    seat.hook("UserPromptSubmit")
    assert seat.result()["status"] == "ok"


def test_an_unwritable_store_is_given_up_quietly(seat):
    (seat.tmp / "a-file").write_text("x", encoding="utf-8")
    cfg = seat.cfg()
    cfg.store = str(seat.tmp / "a-file" / "store")
    assert lh.run_worker(cfg, "SessionEnd") is None and seat.loads() == []


def _run_script(argv, stdin_text, timeout=60):
    return subprocess.run([sys.executable, SCRIPT] + argv, input=stdin_text, capture_output=True, text=True,
                          timeout=timeout, env=_no_git_env())


@pytest.mark.parametrize("event, lines", [("UserPromptSubmit", 0), ("SessionEnd", 1)])
def test_the_script_exits_0_whatever_it_is_given(seat, event, lines):
    """As a process, the way Claude Code runs it: bad JSON on stdin, missing arguments, an unwritable store."""
    (seat.tmp / "a-file").write_text("x", encoding="utf-8")
    cases = [
        (seat.argv(event), "{not json"),
        ([a for a in seat.argv(event) if a not in ("--store", str(seat.store))], "{}"),
        ([str(seat.tmp / "a-file" / "store") if a == str(seat.store) else a for a in seat.argv(event)], "{}"),
        (["--event", event], ""),
    ]
    for argv, stdin_text in cases:
        p = _run_script(argv, stdin_text)
        assert (p.returncode, p.stderr) == (0, ""), (argv, p)
        assert p.stdout.count("\n") == lines and (lines == 0 or p.stdout.startswith("card load: ")), (argv, p)


# ---- a card's words never reach the receipt


CANARY_STAGES = {
    "ok": dict(),
    "held": dict(w_exit=3),
    "unreachable": dict(w_exit=2, w_err=json.dumps({"unreachable": CANARY}) + "\n"),
    "refused": dict(w_exit=2, w_err=json.dumps({"refused": CANARY}) + "\n"),
    "write failed": dict(w_exit=1),
    "loader failed": dict(l_exit=1),
    "loader timeout": dict(l_sleep=30),
    "write timeout": dict(w_sleep=30),
}


@pytest.mark.parametrize("name", sorted(CANARY_STAGES))
def test_the_detail_never_holds_a_cards_words(seat, name):
    case = CANARY_STAGES[name]
    seat.mkdirs()
    seat.mkdirs(lh.PROJECTS, os.path.join(lh.PROJECTS, seat.key()))
    (seat.pstore() / "cards.jsonl").write_text(json.dumps(card(3)) + "\n", encoding="utf-8")   # a store with cards
    noisy = CANARY + "\n" + json.dumps({"text": CANARY}) + "\n"
    loader = seat.fake("l.py", cards=[card(1), card(2)], exit=case.get("l_exit", 0), sleep=case.get("l_sleep", 0),
                       stdout=json.dumps(dict(REPORT, note=CANARY)), stderr=noisy)
    summary = dict(SUMMARY, outcomes={"written": 1, CANARY: 1, "exists": 1},
                   problems=[{"key": "card/pxx/x", "note": CANARY}])
    writer = seat.fake("w.py", exit=case.get("w_exit", 0), sleep=case.get("w_sleep", 0),
                       stdout=json.dumps(summary), stderr=case.get("w_err", noisy))
    out = seat.hook("SessionEnd", loader=loader, writer=writer, timeout=2 if "timeout" in name else 30)
    assert CANARY_MARK in (seat.pstore() / "cards.jsonl").read_text(encoding="utf-8")
    for path in (seat.store / lh.RECEIPT, Path(lh.result_path(str(seat.store), seat.key()))):
        text = path.read_text(encoding="utf-8")
        assert CANARY_MARK not in text and "burn the boats" not in text
    assert CANARY_MARK not in out and len(seat.result()["detail"]) <= 200


# ---- detached, not inline


def test_the_detached_start_flags():
    assert lh.detach_kwargs(windows=True) == {"creationflags": 0x8 | 0x200}
    assert lh.detach_kwargs(windows=False) == {"start_new_session": True}
    assert lh.stage_kwargs(windows=True) == {"creationflags": 0x08000000}, "a stage opens no console window"
    assert lh.stage_kwargs(windows=False) == {}
    assert lh.WINDOWS is (os.name == "nt")


class _Recorded(Exception):
    pass


@pytest.mark.parametrize("windows", [True, False])
def test_each_process_is_started_with_its_platforms_flags(seat, monkeypatch, windows):
    """What Popen is handed, on either platform (a stand-in Popen records it and starts nothing)."""
    seen = []

    def popen(cmd, **kw):
        seen.append(kw)
        raise _Recorded

    monkeypatch.setattr(lh, "WINDOWS", windows)
    monkeypatch.setattr(lh.subprocess, "Popen", popen)
    with pytest.raises(_Recorded):
        lh.spawn(seat.cfg(), "SessionEnd")
    with pytest.raises(_Recorded):
        lh.run_stage([sys.executable, "-c", "pass"], 5, lock_fd=9)
    detached, stage = seen
    assert detached.get("creationflags") == (0x8 | 0x200 if windows else None)
    assert detached.get("start_new_session") is (None if windows else True)
    assert stage.get("creationflags") == (0x08000000 if windows else None)
    assert stage.get("pass_fds") == (None if windows else (9,)) and "start_new_session" not in stage
    for kw in seen:
        assert kw["stdin"] == subprocess.DEVNULL
    assert (detached["stdout"], detached["stderr"]) == (subprocess.DEVNULL, subprocess.DEVNULL)
    assert (stage["stdout"], stage["stderr"]) == (subprocess.PIPE, subprocess.PIPE)


def test_the_child_is_handed_everything_the_load_needs(seat):
    cfg = seat.cfg()
    cfg.marker, cfg.own_name = ["echo -x-shell-ok"], ["pxxcc"]
    argv = lh.child_argv(cfg, "PreCompact")
    assert argv[:5] == [sys.executable, SCRIPT, "--run", "--event=PreCompact", "--identity=pxx"]
    assert "--inline" not in argv
    again = lh.parser().parse_args(argv[2:])
    assert (again.event, again.seat, again.store, again.project_dir, again.base_url, again.state_file,
            again.stage_timeout, again.marker, again.own_name, again.loader, again.writer) == (
        "PreCompact", "pxx", str(seat.store), str(seat.proj), "http://stand-in.invalid",
        str(seat.tmp / "state.json"), 30.0, ["echo -x-shell-ok"], ["pxxcc"], seat.loader, seat.writer)


def test_the_hook_returns_while_the_load_it_started_is_still_running(seat):
    """The real process: the hook's stdout is a pipe, as under Claude Code, and the load's loader waits for a file
    this test writes only after the hook has ended. Had the load run inline, or kept the hook's stdout, the hook
    would not end first."""
    started, release = seat.tmp / "started", seat.tmp / "release"
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       started=str(started), wait_for=str(release))
    argv = seat.argv("SessionEnd", inline=False, loader=loader)
    p = subprocess.Popen([sys.executable, SCRIPT] + argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=_no_git_env())
    try:
        out, err = p.communicate("{}", timeout=30)
        assert (p.returncode, out, err) == (0, "card load: started\n", "")
        assert _wait_for(started.exists, 30), "the load did not start"
        assert seat.result() is None and not _lock_free(seat.store), "the load is still running"
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
        release.touch()
    assert _wait_for(lambda: seat.result() is not None, 60)
    assert seat.result()["status"] == "ok"
    assert _wait_for(lambda: _lock_free(seat.store), 30)


def test_spawn_starts_the_worker_in_a_session_of_its_own(seat):
    release = seat.tmp / "release"
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       wait_for=str(release))
    cfg = seat.cfg(loader=loader)
    child = lh.spawn(cfg, "SessionEnd")
    try:
        assert _wait_for(lambda: len(seat.loads()) == 1, 30)
        assert child.poll() is None
        if os.name != "nt":
            assert os.getsid(child.pid) == child.pid != os.getsid(0)
    finally:
        release.touch()
        child.wait(60)
    assert child.returncode == 0 and seat.result()["status"] == "ok"


# ---- the real loader and the real client, on nothing real


def _typed(uuid, text, ts):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": "S1", "promptSource": "typed",
            "origin": {"kind": "human"}, "parentUuid": "p-" + uuid, "message": {"role": "user", "content": text}}


def test_the_real_loader_over_a_synthetic_transcript(seat):
    lines = [_typed("u1", "echo pxx-shell-ok", "2026-10-10T05:00:00.000Z"),
             _typed("u2", "Keep the north fence closed until Monday, then open it for the sheep.",
                    "2026-10-10T05:01:00.000Z")]
    (seat.proj / "S1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    real_loader = os.path.join(lh.HERE, "transcript_cards.py")
    seat.hook("SessionEnd", loader=real_loader)
    first = seat.result()
    stored = (seat.pstore() / "cards.jsonl").read_text(encoding="utf-8")
    assert first["status"] == "ok" and "north fence" in stored
    assert re.match(r"written 1, exists 1 of \d+ cards \(\d+ new\); ", first["detail"]), first
    assert (seat.pstore() / "cursor.json").exists()
    assert seat.writes()[0]["argv"][3] == str(seat.pstore() / "cards.jsonl")
    seat.hook("SessionEnd", loader=real_loader)
    assert "new)" not in seat.result()["detail"], "the second load reads on from the cursor and finds nothing new"


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_the_real_client_unreachable_and_refused(seat):
    """`card write` itself, from the client checkout: a state file naming a closed local port, then none."""
    base = "http://127.0.0.1:%d" % _closed_port()
    state = seat.tmp / "state.json"
    state.write_text(json.dumps({"schema": "meshy-node-state/1", "base_url": base, "status": "minted",
                                 "peer_id": "0123456789abcdef0123456789abcdef", "jwt": "x",
                                 "jwt_expires_at": "2099-01-01T00:00:00Z"}), encoding="utf-8")

    def run():
        argv = [a for a in seat.argv("SessionEnd") if a not in ("--writer", seat.writer)]
        argv[argv.index("http://stand-in.invalid")] = base
        out = io.StringIO()
        assert lh.main(argv, stdin=io.StringIO("{}"), stdout=out) == 0
        return seat.result()

    r = run()
    assert r["status"] == "unreachable" and r["detail"].startswith("write exit 2, server unreachable"), r
    state.unlink()
    r = run()
    assert r["status"] == "failed" and r["detail"].startswith("write exit 2, refused before sending"), r
    assert CANARY_MARK not in json.dumps(r)


# ---- round 2, finding 1: a load by hand goes through the same lock


def _by_hand(seat, *more, project=None, loader=None):
    """`load_hook.py --run` with no --event: a load by hand, as the README tells a person to run it."""
    argv = seat.argv(None, inline=False, project_dir=project, loader=loader) + ["--run"] + list(more)
    out = io.StringIO()
    assert lh.main(argv, stdin=io.StringIO(""), stdout=out) == 0
    return out.getvalue()


def test_a_load_by_hand_runs_under_the_lock_with_rebuild_and_all_files(seat):
    receipt = json.loads(_by_hand(seat, "--rebuild", "--all-files"))
    assert receipt["status"] == "ok" and receipt["project"] == C(seat.proj)
    assert seat.loads()[0]["argv"][-2:] == ["--rebuild", "--all-files"]
    seat.hook("SessionEnd")
    assert seat.loads()[1]["argv"] == ["--project-dir=" + C(seat.proj), "--seat=pxx", "--out=" + str(seat.pstore())], \
        "a hook's worker never rebuilds"


def test_a_load_by_hand_waits_for_the_lock_and_says_when_it_did_not_get_it(seat):
    fd = seat.hold_lock()
    t0 = time.monotonic()
    assert _by_hand(seat, "--wait", "0.5") == "card load: the store's lock was not free within 0.5 s; nothing run\n"
    assert time.monotonic() - t0 < 10 and seat.loads() == []
    lh.release_lock(fd)
    seat.cleanups.clear()
    assert json.loads(_by_hand(seat, "--wait", "5"))["status"] == "ok"


def test_a_load_by_hand_takes_what_is_queued_after_its_own_pass(seat):
    b = seat.project("projB")
    seat.enqueue(b)
    assert json.loads(_by_hand(seat))["project"] == C(seat.proj)
    assert seat.loaded() == [C(seat.proj), C(b)] and seat.pending() == []


def test_a_load_by_hand_waits_for_a_running_load_and_then_runs(seat):
    fd = seat.hold_lock()
    seat.cleanups.clear()
    freer = threading.Timer(1.0, lambda: lh.release_lock(fd))
    freer.start()
    try:
        t0 = time.monotonic()
        assert json.loads(_by_hand(seat, "--wait", "20"))["status"] == "ok"
        assert time.monotonic() - t0 >= 0.9, "it waited for the lock"
    finally:
        freer.join()


def test_rebuild_and_all_files_are_refused_for_a_hook(seat):
    assert seat.hook("SessionEnd", "--rebuild").startswith(
        "card load: not started, bad arguments (--rebuild and --all-files are for a load by hand")
    assert seat.hook("UserPromptSubmit", "--all-files") == ""
    out = io.StringIO()
    assert lh.main(seat.argv(None, inline=False) + ["--run", "--event=SessionEnd", "--rebuild"],
                   stdin=io.StringIO(""), stdout=out) == 0
    assert out.getvalue().startswith("card load: not started, bad arguments") and seat.loads() == []


def test_the_stage_time_limit_is_longer_by_hand(seat):
    base = seat.argv(None, inline=False)
    base = base[:base.index("--stage-timeout")] + base[base.index("--stage-timeout") + 2:]
    assert lh.parse(base + ["--run"]).stage_timeout == lh.MANUAL_STAGE_TIMEOUT_S == 1800
    assert lh.parse(base + ["--run", "--event=SessionEnd"]).stage_timeout == lh.STAGE_TIMEOUT_S == 120
    assert lh.parse(base + ["--event=SessionEnd"]).stage_timeout == 120
    assert lh.parse(base + ["--run", "--stage-timeout", "7"]).stage_timeout == 7


def test_a_load_by_hand_and_a_hooks_worker_never_overlap(seat):
    """Two real processes on one store: a hook's worker for A and a rebuild by hand for B. One loader at a time."""
    b = seat.project("projB")
    inside = seat.project("inside-probe")
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       inside=str(inside), sleep=1)
    worker = seat.argv(None, inline=False, loader=loader) + ["--run", "--event=SessionEnd"]
    by_hand = seat.argv(None, inline=False, loader=loader, project_dir=b) + ["--run", "--rebuild"]
    procs = [subprocess.Popen([sys.executable, SCRIPT] + argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=_no_git_env()) for argv in (worker, by_hand)]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert (p.returncode, err) == (0, b"")
        outs.append(out.decode("utf-8"))
    assert not (inside / "OVERLAP").exists(), "the load by hand ran beside the hook's worker"
    assert sorted(seat.loaded()) == sorted([C(seat.proj), C(b)])
    rebuilt = [x["argv"] for x in seat.loads() if x["argv"][0] == "--project-dir=" + C(b)]
    assert rebuilt == [["--project-dir=" + C(b), "--seat=pxx", "--out=" + str(seat.pstore(b)), "--rebuild"]]
    assert json.loads(outs[1])["project"] == C(b)


def test_a_pass_does_not_start_while_another_loader_writes_the_store(seat, successors):
    """A loader run directly on a project's store shows while it writes: a fresh <file>.tmp beside its files."""
    seat.mkdirs(lh.PROJECTS, os.path.join(lh.PROJECTS, seat.key()))
    tmp = seat.pstore() / "cards.jsonl.tmp"
    tmp.write_text("half a file", encoding="utf-8")
    assert seat.hook("SessionEnd") == "card load: started\n"
    said = "another loader is writing this store (cards.jsonl.tmp); not loaded"
    assert seat.loads() == [] and seat.result() == dict(seat.result(), status="failed", detail=said)
    assert seat.pending() == [seat.key() + ".json"], "the request is put back for a later worker"
    assert [(c["wait"], c["lock_free"]) for c in successors] == [(lh.SUCCESSOR_WAIT_S, True)], \
        "after a stall, one successor that waits for the evidence to go stale"
    assert json.loads(_by_hand(seat))["detail"] == said and seat.loads() == []
    assert [c["wait"] for c in successors] == [lh.SUCCESSOR_WAIT_S] * 2, "a load by hand hands on the same way"
    old = time.time() - 200
    os.utime(tmp, (old, old))
    seat.hook("SessionStart", "--no-brief")
    assert seat.loaded() == [C(seat.proj)] and seat.result()["status"] == "ok" and seat.pending() == []


def test_the_evidence_of_another_loader_is_bounded_in_time(seat):
    seat.mkdirs()
    now = time.time()
    for name in lh.LOADER_FILES:
        path = seat.store / (name + ".tmp")
        path.write_text("x", encoding="utf-8")
        os.utime(path, (now - 119, now - 119))
        assert lh.another_loader_writing(str(seat.store), now) == name + ".tmp"
        os.utime(path, (now - 120, now - 120))
        assert lh.another_loader_writing(str(seat.store), now) is None
        os.unlink(path)


# ---- round 2, finding 2: Windows, a stage dies with its worker (ctypes mocked; the real run is HQB's)


class _FakeKernel32:
    """kernel32 as stage_job and tie_to_worker call it, recording each call in `log`."""

    def __init__(self, log, create=777, set_ok=1, assign_ok=1):
        import ctypes

        def CreateJobObjectW(attributes, name):
            log.append(("CreateJobObjectW", attributes, name))
            return create

        def SetInformationJobObject(job, info_class, ref, size):
            log.append(("SetInformationJobObject", job, info_class, ref._obj.BasicLimitInformation.LimitFlags,
                        size == ctypes.sizeof(ref._obj)))
            return set_ok

        def AssignProcessToJobObject(job, handle):
            log.append(("AssignProcessToJobObject", job, handle))
            return assign_ok

        def CloseHandle(handle):
            log.append(("CloseHandle", handle))
            return 1

        for f in (CreateJobObjectW, SetInformationJobObject, AssignProcessToJobObject, CloseHandle):
            setattr(self, f.__name__, f)


class _FakeStage:
    def __init__(self, log, handle):
        self._handle, self.pid, self.returncode, self.log = handle, handle, 0, log

    def communicate(self, timeout=None):
        self.log.append(("communicate", self._handle))
        return json.dumps(REPORT).encode(), b""

    def kill(self):
        self.log.append(("kill", self._handle))


def _windows_stand_in(monkeypatch, **kernel):
    log = []
    monkeypatch.setattr(lh, "WINDOWS", True)
    monkeypatch.setattr(lh, "_JOB", None)
    monkeypatch.setattr(lh, "_kernel32", lambda: _FakeKernel32(log, **kernel))

    def popen(cmd, **kw):
        log.append(("Popen", kw.get("creationflags"), "pass_fds" in kw))
        return _FakeStage(log, 4242 + sum(1 for c in log if c[0] == "Popen") - 1)

    monkeypatch.setattr(lh.subprocess, "Popen", popen)
    return log


def test_on_windows_each_stage_goes_into_the_workers_kill_on_close_job(monkeypatch):
    log = _windows_stand_in(monkeypatch)
    assert lh.run_stage(["the loader"], 5)[0] == 0
    assert lh.run_stage(["the writer"], 5)[0] == 0
    assert log == [
        ("Popen", 0x08000000, False),
        ("CreateJobObjectW", None, None),
        ("SetInformationJobObject", 777, 9, 0x2000, True),       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, extended info
        ("AssignProcessToJobObject", 777, 4242),
        ("communicate", 4242),
        ("Popen", 0x08000000, False),                             # one job for the worker's life
        ("AssignProcessToJobObject", 777, 4243),
        ("communicate", 4243),
    ]


@pytest.mark.parametrize("kernel, closed, job_kept", [(dict(assign_ok=0), False, True),
                                                      (dict(create=0), False, False),
                                                      (dict(set_ok=0), True, False)])
def test_on_windows_a_stage_that_cannot_join_the_job_is_stopped_and_not_run(monkeypatch, kernel, closed, job_kept):
    log = _windows_stand_in(monkeypatch, **kernel)
    with pytest.raises(lh.JobError):
        lh.run_stage(["the loader"], 5)
    assert ("kill", 4242) in log and log[-1] == ("communicate", 4242), "stopped, and waited for"
    assert (("CloseHandle", 777) in log) is closed, "a job that could not be made kill-on-close is closed"
    assert (lh._JOB is not None) is job_kept, "only a job made whole is kept for the next stage"


def test_on_windows_a_stage_outside_the_job_fails_the_pass(seat, monkeypatch):
    seat.mkdirs(lh.PENDING, lh.RESULTS, lh.PROJECTS)
    _windows_stand_in(monkeypatch, assign_ok=0)
    r = lh.run_pass(seat.cfg(), C(seat.proj), None)
    assert r["status"] == "failed" and r["detail"] == "load could not run (JobError)"


def test_a_rebuild_that_clears_the_cursor_before_an_exit_3_still_counts_as_the_seats_own_folder(seat, monkeypatch):
    # local Codex review r2: --rebuild clears the loader's cursor before the loader exits 3, so the cursor after the
    # pass says "never" for a folder that held the seat's transcripts. run_pass reads it BEFORE the pass and notes it,
    # so the receipt counts and the seat's summary shows the failure over another folder's ok.
    seat.mkdirs(lh.PENDING, lh.RESULTS, lh.PROJECTS)
    project = C(seat.proj)
    folder = os.path.join(str(seat.store), lh.PROJECTS, lh.request_key(seat.cfg().seat, project))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "cursor.json"), "w", encoding="utf-8") as f:
        json.dump({"files": {"a.jsonl": 1}, "seat_files": {"a.jsonl": 1}}, f)

    def rebuild_then_exit_3(pass_cfg):
        with open(os.path.join(pass_cfg.store, "cursor.json"), "w", encoding="utf-8") as f:
            json.dump({"files": {}, "seat_files": {}}, f)
        return "failed", lh.NO_MARKER
    monkeypatch.setattr(lh, "one_pass", rebuild_then_exit_3)
    other = {"time": lh.stamp(time.time() - 600), "status": "ok", "detail": "exists 1 of 1 cards",
             "seat": seat.cfg().seat, "project": "/elsewhere"}
    assert lh.write_json(lh.result_path(str(seat.store), lh.request_key(seat.cfg().seat, "/elsewhere")), other)
    r = lh.run_pass(seat.cfg(), project, None)
    assert r["detail"] == lh.NO_MARKER + lh.KNEW_SEAT
    assert lh.write_summary(str(seat.store)) == r
    # Codex r3: the next pass finds the cursor already empty; the last receipt still carries the evidence.
    again = lh.run_pass(seat.cfg(), project, None)
    assert again["detail"] == lh.NO_MARKER + lh.KNEW_SEAT
    assert lh.write_summary(str(seat.store)) == again
    # A folder the store never knew: the same exit 3 is a side session's, and the other folder's ok is the summary.
    side = seat.tmp / "side"
    side.mkdir()
    os.unlink(lh.result_path(str(seat.store), lh.request_key(seat.cfg().seat, project)))
    s = lh.run_pass(seat.cfg(), C(side), None)
    assert s["detail"] == lh.NO_MARKER
    assert lh.write_summary(str(seat.store)) == other


def test_only_explicit_evidence_in_the_last_receipt_makes_a_folder_the_seats_own(seat, monkeypatch):
    # GitHub Codex on #273: an ok load of a folder with no transcript at all exits 0 with an empty cursor, so a bare
    # ok is no evidence. A receipt carries "knew_seat" when the folder's store knew a seat transcript, and only that
    # (or the KNEW_SEAT note) carries forward.
    seat.mkdirs(lh.PENDING, lh.RESULTS, lh.PROJECTS)
    project = C(seat.proj)
    key = lh.request_key(seat.cfg().seat, project)

    def prior(**fields):
        assert lh.write_json(lh.result_path(str(seat.store), key), dict({"time": lh.stamp(time.time() - 60),
                             "detail": "x", "seat": seat.cfg().seat, "project": project}, **fields))
    monkeypatch.setattr(lh, "one_pass", lambda pass_cfg: ("failed", lh.NO_MARKER))
    for status in ("ok", "held", "unreachable"):
        prior(status=status)
        r = lh.run_pass(seat.cfg(), project, None)
        assert r["detail"] == lh.NO_MARKER and "knew_seat" not in r, status
    prior(status="ok", knew_seat=True)
    r = lh.run_pass(seat.cfg(), project, None)
    assert r["detail"] == lh.NO_MARKER + lh.KNEW_SEAT and r["knew_seat"] is True

    # An ok pass whose cursor names a seat file records the evidence on its receipt.
    def loads_the_seats_transcript(pass_cfg):
        with open(os.path.join(pass_cfg.store, "cursor.json"), "w", encoding="utf-8") as f:
            json.dump({"files": {"a.jsonl": 1}, "seat_files": {"a.jsonl": 1}}, f)
        return "ok", "exists 1 of 1 cards"
    other = C(seat.tmp / "other")
    (seat.tmp / "other").mkdir()
    monkeypatch.setattr(lh, "one_pass", loads_the_seats_transcript)
    assert lh.run_pass(seat.cfg(), other, None)["knew_seat"] is True


def test_an_unreadable_cursor_once_does_not_make_a_side_folder_the_seats_for_good(seat, monkeypatch):
    # memory-bxxxxx a87738: "unknown" counted as evidence, was recorded as knew_seat and carried forward, so one
    # transient read failure kept a side folder deciding the seat's summary. Now it counts only while unreadable.
    seat.mkdirs(lh.PENDING, lh.RESULTS, lh.PROJECTS)
    side = seat.tmp / "side"
    side.mkdir()
    folder = os.path.join(str(seat.store), lh.PROJECTS, lh.request_key(seat.cfg().seat, C(side)))
    os.makedirs(folder, exist_ok=True)
    cursor = os.path.join(folder, "cursor.json")
    other = {"time": lh.stamp(time.time() - 600), "status": "ok", "detail": "exists 1 of 1 cards",
             "seat": seat.cfg().seat, "project": "/own", "knew_seat": True}
    assert lh.write_json(lh.result_path(str(seat.store), lh.request_key(seat.cfg().seat, "/own")), other)
    monkeypatch.setattr(lh, "one_pass", lambda pass_cfg: ("failed", lh.NO_MARKER))
    with open(cursor, "w", encoding="utf-8") as f:
        f.write("not json")
    r = lh.run_pass(seat.cfg(), C(side), None)
    assert "knew_seat" not in r and r["detail"] == lh.NO_MARKER
    assert lh.write_summary(str(seat.store)) == r, "while the cursor cannot be read, the failure still shows"
    with open(cursor, "w", encoding="utf-8") as f:
        json.dump({"files": {}, "seat_files": {}}, f)
    for _ in range(2):
        r = lh.run_pass(seat.cfg(), C(side), None)
        assert "knew_seat" not in r and r["detail"] == lh.NO_MARKER
        assert lh.write_summary(str(seat.store)) == other


def test_on_posix_no_job_is_made(monkeypatch):
    log = []
    monkeypatch.setattr(lh, "WINDOWS", False)
    monkeypatch.setattr(lh, "_kernel32", lambda: _FakeKernel32(log))
    assert lh.run_stage([sys.executable, "-c", "pass"], 30)[0] == 0
    assert log == []


# ---- round 2, finding 3: the store is this user's alone


def test_the_worker_makes_the_store_and_its_folders_private(seat):
    seat.hook("SessionEnd")
    for path in (seat.store, seat.store / lh.PENDING, seat.store / lh.RESULTS, seat.store / lh.PROJECTS,
                 seat.pstore()):
        assert path.is_dir()
        if os.name != "nt":
            assert stat.S_IMODE(os.lstat(path).st_mode) == 0o700, path


STORE_FAULTS = {
    "pending group-writable": "pending/ can be written by other users",
    "results world-writable": "results/ can be written by other users",
    "pending another user's": "pending/ belongs to another user",
    "pending a link": "pending/ is a link",
    "store group-writable": "the store can be written by other users",
    "store a link": "the store is a link",
    "store another user's": "the store belongs to another user",
    "projects group-writable": "projects/ can be written by other users",
    "projects a link": "projects/ is a link",
}


@pytest.mark.parametrize("fault", sorted(STORE_FAULTS))
def test_a_store_another_user_could_write_to_is_refused(seat, monkeypatch, fault):
    if os.name == "nt":                            # Windows: the store is trusted to the profile's ACL, by design
        assert lh._private_dir_problem(str(seat.tmp)) is None
        return
    seat.mkdirs(lh.PENDING, lh.RESULTS)
    b = seat.project("projB")
    seat.enqueue(b)
    elsewhere = seat.project("elsewhere")
    pending, results = seat.store / lh.PENDING, seat.store / lh.RESULTS
    store_sound = not fault.startswith("store")
    projects = seat.store / lh.PROJECTS
    seat.mkdirs(lh.PROJECTS)
    if fault == "projects group-writable":
        os.chmod(projects, 0o770)
    elif fault == "projects a link":
        os.rename(projects, elsewhere / "pr")
        os.symlink(str(elsewhere / "pr"), str(projects), target_is_directory=True)
    elif fault == "pending group-writable":
        os.chmod(pending, 0o770)
    elif fault == "results world-writable":
        os.chmod(results, 0o702)
    elif fault == "pending a link":
        os.rename(pending, elsewhere / "p")
        os.symlink(str(elsewhere / "p"), str(pending), target_is_directory=True)
    elif fault == "store group-writable":
        os.chmod(seat.store, 0o770)
    elif fault == "store a link":
        os.rename(seat.store, elsewhere / "s")
        os.symlink(str(elsewhere / "s"), str(seat.store), target_is_directory=True)
    else:                                          # another user's: the folder's stat says another uid
        target = str(seat.store if fault == "store another user's" else pending)
        real = os.lstat

        def lstat(path, *a, **kw):
            st = real(path, *a, **kw)
            if os.fspath(path) == target:
                fields = list(st)
                fields[4] = st.st_uid + 1
                return os.stat_result(fields)
            return st

        monkeypatch.setattr(lh.os, "lstat", lstat)
    lh.run_worker(seat.cfg(), "SessionEnd")
    assert seat.loads() == [], "nothing is loaded from a store another user could write to"
    said = "store refused: %s; nothing loaded" % STORE_FAULTS[fault]
    if store_sound:
        assert lh.read_json(str(seat.store / lh.RECEIPT)) == dict(lh.read_json(str(seat.store / lh.RECEIPT)),
                                                                  status="failed", detail=said)
    else:
        assert not os.path.lexists(seat.store / lh.RECEIPT), "nothing is written into a store that is not sound"
    assert _by_hand(seat, "--wait", "1") == "card load: store refused: %s\n" % STORE_FAULTS[fault]
    assert seat.loads() == []


def test_a_request_file_of_another_user_is_set_aside(seat, monkeypatch):
    b = seat.project("projB")
    seat.enqueue(b)
    foreign = os.lstat(seat.store / lh.PENDING / (seat.key(b) + ".json")).st_ino
    real = lh._mine
    monkeypatch.setattr(lh, "_mine", lambda st: st.st_ino != foreign and real(st))
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded() == [C(seat.proj)] and seat.set_aside() == [seat.key(b) + ".json" + lh.BAD]


# ---- round 2, finding 4: a malformed request is set aside and the next is loaded


BAD_REQUESTS = {
    "a lone surrogate": b'{"project_dir": "/tmp/\\ud800", "time": 1}',
    "a folder that is no string": b'{"project_dir": 5, "time": 1}',
    "an empty folder": b'{"project_dir": "", "time": 1}',
    "a relative folder": b'{"project_dir": "rel/path", "time": 1}',
    "a NUL in the folder": b'{"project_dir": "/a\\u0000b", "time": 1}',
    "a folder too long": json.dumps({"project_dir": "/" + "x" * 5000, "time": 1}).encode(),
    "a time that is text": b'{"project_dir": null, "time": "soon"}',
    "a time that is NaN": b'{"project_dir": null, "time": NaN}',
    "a time that is infinite": b'{"project_dir": null, "time": Infinity}',
    "a time beyond any clock": b'{"project_dir": null, "time": 100000000001}',
    "a time too large for a float": json.dumps({"project_dir": "/tmp/project", "time": 10 ** 400}).encode(),
    "a time that is true": b'{"project_dir": null, "time": true}',
    "a time below zero": b'{"project_dir": null, "time": -1}',
    "a list": b'[1, 2]',
    "not JSON": b'{',
    "not UTF-8": b'{"project_dir": "\xff", "time": 1}',
    "a transcript up a folder": b'{"project_dir": "/tmp", "transcript": "../x.jsonl", "time": 1}',
    "a transcript in a folder": b'{"project_dir": "/tmp", "transcript": "a/b.jsonl", "time": 1}',
    "a transcript with a backslash": b'{"project_dir": "/tmp", "transcript": "a\\\\b.jsonl", "time": 1}',
    "a transcript that is no .jsonl": b'{"project_dir": "/tmp", "transcript": "x.json", "time": 1}',
    "a transcript with no folder": b'{"project_dir": null, "transcript": "x.jsonl", "time": 1}',
    "a transcript that is no string": b'{"project_dir": "/tmp", "transcript": 5, "time": 1}',
}


@pytest.mark.parametrize("name", sorted(BAD_REQUESTS))
def test_a_malformed_request_is_set_aside_and_the_next_still_loads(seat, name):
    b = seat.project("projB")
    seat.enqueue(b, time=time.time() + 5)
    (seat.store / lh.PENDING / "00malformed.json").write_bytes(BAD_REQUESTS[name])
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.set_aside() == ["00malformed.json" + lh.BAD]
    assert seat.loaded() == [C(seat.proj), C(b)] and seat.pending() == []
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.set_aside() == ["00malformed.json" + lh.BAD], "a request set aside is never read again"


def test_a_request_whose_extra_fields_cannot_be_encoded_is_still_one_folders_request(seat):
    """Codex's case: {"seat": "\\ud800", "project_dir": null, ...}. The seat in a file is never used, so the request
    is a well-formed one with no folder: it is skipped, and the queue goes on."""
    b = seat.project("projB")
    seat.enqueue(b, time=time.time() + 5)
    (seat.store / lh.PENDING / "00nofolder.json").write_bytes(b'{"seat": "\\ud800", "project_dir": null, "time": 1}')
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded() == [C(seat.proj), C(b)] and seat.pending() == [] and seat.set_aside() == []
    assert lh.read_json(lh.result_path(str(seat.store), lh.request_key("pxx", None)))["status"] == "skipped"


def test_a_failure_inside_one_request_costs_that_request_only(seat, monkeypatch):
    b = seat.project("projB")
    seat.enqueue(b, time=1)
    real = lh.write_result

    def fails_for_b(store, seat_label, project, status, detail, now=None, knew_seat=False, counts=None):
        if project == C(b):
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "a receipt that cannot be written")
        return real(store, seat_label, project, status, detail, now, knew_seat, counts)

    monkeypatch.setattr(lh, "write_result", fails_for_b)
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded() == [C(b), C(seat.proj)] and seat.result()["status"] == "ok"
    assert seat.set_aside() == [seat.key(b) + ".json" + lh.CLAIMED + lh.BAD] and seat.pending() == []
    monkeypatch.setattr(lh, "write_result", real)
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded()[2:] == [C(seat.proj)], "the next worker does not meet it again"


# ---- round 2, finding 5: nothing in the store can make the worker wait


def test_a_pipe_a_link_a_folder_or_a_large_file_in_the_queue_is_set_aside(seat):
    seat.mkdirs(lh.PENDING, lh.RESULTS)
    pending = seat.store / lh.PENDING
    oddities = ["00big.json", "00folder.json"]
    (pending / "00big.json").write_bytes(b'{"project_dir": null, "time": 1, "pad": "' + b"x" * 20000 + b'"}')
    (pending / "00folder.json").mkdir()
    if hasattr(os, "mkfifo"):
        os.mkfifo(pending / "00pipe.json")
        oddities.append("00pipe.json")
    c = seat.project("projC")
    lh.write_json(str(seat.tmp / "elsewhere.json"), {"project_dir": C(c), "time": 1})
    try:
        os.symlink(str(seat.tmp / "elsewhere.json"), str(pending / "00link.json"))
        oddities.append("00link.json")
    except (OSError, NotImplementedError):
        pass                                       # Windows without the symlink right
    b = seat.project("projB")
    seat.enqueue(b, time=1)
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded() == [C(b), C(seat.proj)], "the link's request is never followed"
    assert seat.set_aside() == sorted(n + lh.BAD for n in oddities) and seat.pending() == []


def test_read_small_reads_only_a_small_regular_file_of_this_users(seat, monkeypatch):
    small, big = seat.tmp / "small.json", seat.tmp / "big.json"
    small.write_bytes(b"{}")
    big.write_bytes(b"x" * (lh.MAX_FILE_BYTES + 1))
    assert lh.read_small(str(small)) == b"{}"
    with pytest.raises(lh.Unsafe):
        lh.read_small(str(big))
    with pytest.raises(FileNotFoundError):
        lh.read_small(str(seat.tmp / "none.json"))
    with pytest.raises(lh.Unsafe):
        lh.read_small(str(seat.proj))
    if hasattr(os, "mkfifo"):
        os.mkfifo(seat.tmp / "pipe.json")
        t0 = time.monotonic()
        with pytest.raises(lh.Unsafe):
            lh.read_small(str(seat.tmp / "pipe.json"))
        assert time.monotonic() - t0 < 5
    try:
        os.symlink(str(small), str(seat.tmp / "link.json"))
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(lh.Unsafe):
            lh.read_small(str(seat.tmp / "link.json"))
    monkeypatch.setattr(lh, "_mine", lambda st: False)
    with pytest.raises(lh.Unsafe):
        lh.read_small(str(small))


def test_a_lock_file_that_is_no_regular_file_is_never_taken_and_never_waited_on(seat):
    seat.mkdirs()
    (seat.store / lh.LOCK).mkdir()
    assert lh.take_lock(str(seat.store)) is None
    os.rmdir(seat.store / lh.LOCK)
    (seat.tmp / "elsewhere.lock").write_text("", encoding="utf-8")
    try:
        os.symlink(str(seat.tmp / "elsewhere.lock"), str(seat.store / lh.LOCK))
    except (OSError, NotImplementedError):
        pass
    else:
        assert lh.take_lock(str(seat.store)) is None, "a lock file that is a link is not followed"
        os.unlink(seat.store / lh.LOCK)
    if hasattr(os, "mkfifo"):
        os.mkfifo(seat.store / lh.LOCK)
        t0 = time.monotonic()
        assert lh.take_lock(str(seat.store)) is None and time.monotonic() - t0 < 5


def test_one_scan_looks_at_no_more_than_max_scan_entries(seat):
    seat.mkdirs(lh.PENDING)
    for i in range(lh.MAX_SCAN + 20):
        (seat.store / lh.PENDING / ("%04d.json" % i)).write_bytes(b"{")
    assert lh.queued(str(seat.store)) == []
    assert len(seat.set_aside()) == lh.MAX_SCAN == 256 and len(seat.pending()) == 20
    lh.queued(str(seat.store))
    assert len(seat.set_aside()) == lh.MAX_SCAN + 20 and seat.pending() == []


def test_the_summary_reads_no_more_than_max_scan_receipts(seat, monkeypatch):
    seat.mkdirs(lh.RESULTS)
    for i in range(lh.MAX_SCAN + 10):
        lh.write_json(str(seat.store / lh.RESULTS / ("%032x.json" % i)),
                      {"time": lh.stamp(time.time()), "status": "ok", "detail": "", "seat": "pxx", "project": None})
    real, read = lh.read_json, []
    monkeypatch.setattr(lh, "read_json", lambda path: read.append(path) or real(path))
    assert lh.write_summary(str(seat.store))["status"] == "ok"
    assert len(read) == lh.MAX_SCAN


def test_the_summary_reads_the_receipts_written_last_not_the_first_names(seat):
    """GitHub Codex on 47fe56e: receipts are never expired, and the summary read the first MAX_SCAN by name (a
    hash). A seat with more project folders than that never saw a new failure whose name sorted late, and an older
    clean receipt hid it. It now reads the MAX_SCAN written last, by mtime.

    MUTANT: choose by name again (sorted names, first MAX_SCAN); the late-sorting failure is not seen.
    MUTANT: oldest first (no reverse); the failure is not seen.
    """
    seat.mkdirs(lh.RESULTS)
    folder = seat.store / lh.RESULTS
    old = time.time() - 600
    for i in range(lh.MAX_SCAN + 10):                      # clean receipts, written earlier, names sorting first
        path = folder / ("%032x.json" % i)
        lh.write_json(str(path), {"time": lh.stamp(time.time()), "status": "ok", "detail": "", "seat": "pxx",
                                  "project": None})
        os.utime(path, (old + i * 0.001, old + i * 0.001))
    late = folder / ("f" * 32 + ".json")                   # the newest receipt, its name sorting last
    lh.write_json(str(late), {"time": lh.stamp(time.time()), "status": "failed", "detail": "the new failure",
                              "seat": "pxx", "project": None})
    pick = lh.write_summary(str(seat.store))
    assert pick["status"] == "failed" and pick["detail"] == "the new failure", pick
    assert lh.read_json(str(seat.store / lh.RECEIPT))["detail"] == "the new failure"


def test_draining_stops_when_nothing_can_be_claimed(seat, monkeypatch):
    seat.enqueue(seat.project("projB"))
    scans = []
    real = lh.queued
    monkeypatch.setattr(lh, "queued", lambda store: scans.append(1) or real(store))
    monkeypatch.setattr(lh, "claim", lambda path: None)
    assert lh.drain(seat.cfg(), None, lh.MAX_PASSES) == (0, None, True), "nothing claimable is a stall"
    assert len(scans) == 1 and seat.loads() == []


def test_a_file_that_grew_after_its_stat_is_still_cut_at_the_limit(seat, monkeypatch):
    """The size is checked on what is read, not only on what fstat said: a file can grow in between."""
    grown = seat.tmp / "grown.json"
    grown.write_bytes(b"x" * (lh.MAX_FILE_BYTES + 100))
    real = os.fstat

    def small_stat(fd):
        st = real(fd)
        fields = list(st)
        fields[6] = 10                             # st_size, as it was a moment before
        return os.stat_result(fields)

    monkeypatch.setattr(lh.os, "fstat", small_stat)
    with pytest.raises(lh.Unsafe):
        lh.read_small(str(grown))


def test_the_card_file_is_read_only_as_a_regular_file(seat):
    cards = seat.tmp / "real-cards.jsonl"
    cards.write_text(json.dumps(card(1)) + "\n", encoding="utf-8")
    assert lh.has_cards(str(cards)) is True
    try:
        os.symlink(str(cards), str(seat.tmp / "cards.jsonl"))
    except (OSError, NotImplementedError):
        pass
    else:
        assert lh.has_cards(str(seat.tmp / "cards.jsonl")) is False, "a card file that is a link is not followed"
        os.unlink(seat.tmp / "cards.jsonl")
    if hasattr(os, "mkfifo"):
        os.mkfifo(seat.tmp / "cards.jsonl")
        t0 = time.monotonic()
        assert lh.has_cards(str(seat.tmp / "cards.jsonl")) is False and time.monotonic() - t0 < 5


# ---- round 3: one loader store a project folder, and a scan that never raises


REAL_LOADER = os.path.join(lh.HERE, "transcript_cards.py")


def _transcript(folder, name, *prompts):
    lines = [_typed("%s-%d" % (folder.name, i), text, "2026-10-10T05:%02d:00.000Z" % i) for i, text in enumerate(prompts)]
    (folder / name).write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def _all_cards(seat) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted((seat.store / lh.PROJECTS).glob("*/cards.jsonl")))


def test_a_file_of_the_same_name_in_another_folder_is_never_taken_for_this_seats(seat):
    """The real loader. Folder A's shared.jsonl holds this seat's marker; folder B's shared.jsonl holds another
    seat's. In one shared loader store, B's file passed by its name alone and its prompts became this seat's cards.
    Each folder's own store reads B's file afresh: it is not this seat's, and nothing of it is a card or is sent."""
    a, b = seat.project("projA"), seat.project("projB")
    _transcript(a, "shared.jsonl", "echo pxx-shell-ok", "Paint the A barn red before Friday, please.")
    _transcript(b, "shared.jsonl", "echo txxxx-shell-ok", "BONLY: move the hives to the east field tonight.",
                "BONLY: and tell nobody about the hives.")
    seat.hook("SessionEnd", project_dir=a, loader=REAL_LOADER)
    seat.hook("SessionEnd", project_dir=b, loader=REAL_LOADER)
    cards = _all_cards(seat)
    assert "A barn" in cards and "BONLY" not in cards
    assert seat.result(a)["status"] == "ok"
    assert seat.result(b)["status"] == "failed" and seat.result(b)["detail"].startswith("loader exit 3")
    assert [w["argv"][3] for w in seat.writes()] == [str(seat.pstore(a) / "cards.jsonl")], "B wrote nothing"


def test_a_rebuild_of_one_project_leaves_every_other_projects_store_as_it_was(seat):
    """The real loader. A and B loaded into the one root; then A rebuilt by hand. B's loader store, cards and cursor
    included, is byte for byte what it was; A's is rebuilt with its cards."""
    a, b = seat.project("projA"), seat.project("projB")
    _transcript(a, "a1.jsonl", "echo pxx-shell-ok", "Paint the A barn red before Friday, please.")
    _transcript(b, "b1.jsonl", "echo pxx-shell-ok", "Move the B hives to the east field tonight.")
    seat.hook("SessionEnd", project_dir=a, loader=REAL_LOADER)
    seat.hook("SessionEnd", project_dir=b, loader=REAL_LOADER)
    files = ("cards.jsonl", "cursor.json", "drops.jsonl", "report.json")
    before = {n: (seat.pstore(b) / n).read_bytes() for n in files}
    assert b"B hives" in before["cards.jsonl"]
    rebuilt = json.loads(_by_hand(seat, "--rebuild", project=a, loader=REAL_LOADER))
    assert rebuilt["status"] == "ok" and rebuilt["project"] == C(a)
    assert {n: (seat.pstore(b) / n).read_bytes() for n in files} == before
    assert "A barn" in (seat.pstore(a) / "cards.jsonl").read_text(encoding="utf-8")
    assert json.loads((seat.pstore(a) / "report.json").read_text(encoding="utf-8"))["new_cards"] >= 1


@pytest.mark.parametrize("name", [n for n in lh.LOADER_FILES] + ["cards.jsonl.tmp"])
def test_a_store_with_a_loaders_files_at_its_root_is_refused(seat, name):
    """A root that holds the loader's files is a store from before projects/: refused with its reason, not guessed
    at."""
    seat.mkdirs()
    (seat.store / name).write_text("{}", encoding="utf-8")
    base = name[:-len(".tmp")] if name.endswith(".tmp") else name
    said = ("the store holds a loader's %s at its root (one loader store for every project, the layout before "
            "projects/); move the loader's files out of it" % base)
    assert seat.hook("SessionEnd") == "card load: started\n"
    assert seat.loads() == []
    assert lh.read_receipt(str(seat.store))["detail"] == ("store refused: %s; nothing loaded" % said)[:200]
    assert _by_hand(seat, "--wait", "1") == "card load: store refused: %s\n" % said and seat.loads() == []


def test_a_projects_store_another_user_could_write_to_is_refused_and_the_others_still_load(seat):
    b = seat.project("projB")
    seat.enqueue(b, time=1)
    if os.name == "nt":                            # Windows: trusted to the profile's ACL, by design
        lh.run_worker(seat.cfg(), "SessionStart")
        assert seat.loaded() == [C(b), C(seat.proj)]
        return
    seat.mkdirs(lh.PROJECTS, os.path.join(lh.PROJECTS, seat.key(b)))
    os.chmod(seat.pstore(b), 0o770)
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded() == [C(seat.proj)]
    assert seat.result(b) == dict(seat.result(b), status="failed",
                                  detail="store refused: projects/%s/ can be written by other users; not loaded"
                                         % seat.key(b))


def test_a_time_is_bounded_without_turning_it_into_a_float():
    assert lh.MAX_TIME == 10 ** 11
    for at, ok in [(10 ** 400, False), (lh.MAX_TIME, True), (lh.MAX_TIME + 1, False), (0, True), (-1, False),
                   (1.5, True), (-1.5, False), (float(lh.MAX_TIME) + 1.0, False), (float("inf"), False),
                   (float("nan"), False), (True, False)]:
        assert lh.valid_request({"project_dir": None, "time": at}) is ok, at


def test_an_oversized_integer_time_beside_a_valid_request(seat):
    """Codex's case: the valid project loads in the same worker; the bad file is set aside and never read again."""
    b = seat.project("projB")
    seat.enqueue(b, time=time.time() + 5)
    (seat.store / lh.PENDING / "00bad.json").write_text(
        json.dumps({"project_dir": "/tmp/project", "time": 10 ** 400}), encoding="utf-8")
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.loaded() == [C(seat.proj), C(b)] and seat.set_aside() == ["00bad.json" + lh.BAD]
    lh.run_worker(seat.cfg(), "SessionStart")
    assert seat.set_aside() == ["00bad.json" + lh.BAD] and seat.pending() == []


def test_reading_a_request_never_raises(seat, monkeypatch):
    seat.enqueue(seat.proj)
    path = str(seat.store / lh.PENDING / (seat.key() + ".json"))
    assert lh.read_request(path)[0] == "ok"

    def boom(obj):
        raise RuntimeError("a check that broke")

    monkeypatch.setattr(lh, "valid_request", boom)
    assert lh.read_request(path) == ("bad", None)

    def read_breaks(path, limit=lh.MAX_FILE_BYTES):
        raise RuntimeError("a read that broke")

    monkeypatch.setattr(lh, "read_small", read_breaks)
    assert lh.read_request(path) == ("bad", None)


def test_the_queue_scan_never_raises(seat, monkeypatch):
    b = seat.project("projB")
    seat.enqueue(b)
    (seat.store / lh.PENDING / "00first.json").write_text("{}", encoding="utf-8")
    real = lh.read_request

    def breaks_on_first(path):
        if path.endswith("00first.json"):
            raise OverflowError("a check that broke")
        return real(path)

    monkeypatch.setattr(lh, "read_request", breaks_on_first)
    assert [req["project_dir"] for _, req in lh.queued(str(seat.store))] == [C(b)]
    assert seat.set_aside() == ["00first.json" + lh.BAD]


# ---- #217 (GitHub Codex, P1): what a worker leaves, a successor drains


def test_a_request_queued_while_the_holder_finishes_its_last_pass_is_drained_by_a_successor(seat):
    """Real processes. A worker is in the third and last pass of its budget; a session ends meanwhile and its worker
    queues B, finds the lock held, and leaves. The holder reaches its limit with B queued: it releases the lock,
    starts one successor, and the successor loads B. Without it, B would wait for some later hook."""
    projects = [seat.project("p%d" % i) for i in range(3)]
    for i, p in enumerate(projects):
        seat.enqueue(p, time=1000 + i)
    seat.put_result("ok", 60)                      # the holder's own project is quiet: it takes only the queue
    started, release = seat.tmp / "started", seat.tmp / "release"
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       gate_on=os.sep + "p2", started=str(started), wait_for=str(release))
    holder = subprocess.Popen([sys.executable, SCRIPT] + seat.argv(None, inline=False, loader=loader)
                              + ["--run", "--event=UserPromptSubmit"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_no_git_env())
    b = seat.project("projB")
    try:
        assert _wait_for(started.exists, 30), "the holder is in its last pass"
        assert seat.hook("SessionEnd", project_dir=b, loader=loader) == "card load: started\n"
        assert seat.key(b) + ".json" in seat.pending() and not _lock_free(seat.store)
    finally:
        release.touch()
        _, err = holder.communicate(timeout=60)
    assert (holder.returncode, err) == (0, b"")
    assert _wait_for(lambda: (seat.result(b) or {}).get("status") == "ok", 60), "a successor loaded B"
    assert seat.loaded() == [C(p) for p in projects] + [C(b)]
    assert _wait_for(lambda: _lock_free(seat.store) and seat.pending() == [], 30)


def test_no_successor_when_the_queue_is_left_empty(seat, successors):
    seat.hook("SessionEnd")
    seat.hook("PreCompact", project_dir=seat.project("projB"))
    _by_hand(seat)
    assert len(seat.loads()) == 3 and successors == []


def test_a_successor_starts_after_the_release_with_the_workers_own_arguments_only(seat, successors):
    projects = [seat.project("p%d" % i) for i in range(4)]
    for i, p in enumerate(projects):
        seat.enqueue(p, time=1000 + i)
    lh.run_worker(seat.cfg(), "SessionStart")
    [call] = successors
    assert call["lock_free"] and call["pending"] and call["wait"] == 0
    assert call["argv"] == [sys.executable, SCRIPT, "--run", "--successor", "--after=0.0", "--identity=pxx",
                            "--seat=pxx",
                            "--store=" + str(seat.store), "--base-url=http://stand-in.invalid",
                            "--state-file=" + str(seat.tmp / "state.json"), "--stage-timeout=30.0",
                            "--loader=" + seat.loader, "--writer=" + seat.writer]
    again = lh.parse(call["argv"][2:])
    assert again.successor and again.event is None and again.project_dir is None and not again.rebuild


def test_a_request_arriving_between_the_last_scan_and_the_release_is_not_stranded(seat, monkeypatch, successors):
    """The scan finds the queue empty (another worker took it); B arrives before the release. The worker looks at
    the queue after the release, goes round again, and loads B itself."""
    b = seat.project("projB")
    seat.put_result("ok", 60)
    seat.enqueue(seat.project("projX"))
    real_queued, real_release, firsts = lh.queued, lh.release_lock, []

    def queued(store):
        if not firsts:
            firsts.append("scan")
            return []
        return real_queued(store)

    def release(fd):
        if firsts == ["scan"]:
            firsts.append("release")
            seat.enqueue(b)
        real_release(fd)

    monkeypatch.setattr(lh, "queued", queued)
    monkeypatch.setattr(lh, "release_lock", release)
    lh.run_worker(seat.cfg(), "UserPromptSubmit")
    assert C(b) in seat.loaded() and seat.pending() == [] and successors == []


def test_a_successor_with_nothing_to_do_ends_at_once(seat, successors):
    seat.mkdirs(lh.PENDING, lh.RESULTS, lh.PROJECTS)
    cfg = seat.cfg()
    cfg.successor = True
    t0 = time.monotonic()
    assert lh.run_worker(cfg, None) is None
    assert time.monotonic() - t0 < 5 and seat.loads() == [] and successors == []
    assert not (seat.store / lh.LOCK).exists(), "it did not even take the lock"


def test_a_successor_that_makes_no_progress_starts_none(seat, successors):
    seat.enqueue(seat.proj)
    seat.mkdirs(lh.PROJECTS, os.path.join(lh.PROJECTS, seat.key()))
    (seat.pstore() / "cards.jsonl.tmp").write_text("x", encoding="utf-8")       # a direct run is writing
    cfg = seat.cfg()
    cfg.successor = True
    lh.run_worker(cfg, None)
    assert seat.loads() == [] and successors == [], "an idle successor ends the chain"
    lh.run_worker(seat.cfg(), "SessionStart")
    assert [c["wait"] for c in successors] == [lh.SUCCESSOR_WAIT_S], "a hook's worker after the same stall hands on"


def test_a_successor_runs_its_wait_first_and_takes_the_queue(seat, monkeypatch):
    b = seat.project("projB")
    seat.enqueue(b)
    monkeypatch.setattr(lh, "SUCCESSOR_WAIT_S", 0.3)            # the wait is capped by it
    argv = seat.argv(None, inline=False, project=False) + ["--run", "--successor", "--after", "50"]
    out = io.StringIO()
    t0 = time.monotonic()
    assert lh.main(argv, stdin=io.StringIO(""), stdout=out) == 0
    assert 0.25 <= time.monotonic() - t0 < 20
    assert json.loads(out.getvalue())["project"] == C(b) and seat.loaded() == [C(b)]


def test_successor_is_a_workers_own_flag(seat):
    for extra in (["--event=SessionEnd"], ["--rebuild"]):
        out = io.StringIO()
        assert lh.main(seat.argv(None, inline=False) + ["--run", "--successor"] + extra, stdin=io.StringIO(""),
                       stdout=out) == 0
        assert out.getvalue().startswith("card load: not started, bad arguments") or \
            extra == ["--event=SessionEnd"] and out.getvalue().startswith("card load: not started, bad arguments")
    assert seat.loads() == []


# ---- txxxx on #217, 1: the seat is the pane's identity (one Claude Code profile serves several seats on HQB)


def _postbox(pane, identities, base_url="http://stand-in.invalid"):
    """A postbox config.json in the test's own folder (MESHY_POSTBOX_CONFIG points at it)."""
    path = pane / "postbox-config.json"
    path.write_text(json.dumps({"base_url": base_url, "identities": identities}), encoding="utf-8")
    return path


def _identity_argv(seat, event, **kw):
    """The hook as a shared profile wires it: no --seat, --store, --base-url or --state-file."""
    return ["--event", event, "--stage-timeout", "30", "--loader", kw.get("loader", seat.loader),
            "--writer", seat.writer, "--project-dir", str(kw.get("project_dir", seat.proj)), "--inline"]


# ---- #236: one shared SessionStart line loads and briefs the pane's seat


@pytest.fixture
def recent_children(monkeypatch):
    """Track real briefing children, and always close them even when a mutant makes an assertion fail."""
    children = []
    real = subprocess.Popen

    def popen(cmd, **kw):
        p = real(cmd, **kw)
        # Only the briefing child (`... card recent ...`). A loader stage's pipe must stay untouched: on Windows a dup
        # shares the handle, and the stage's communicate() then reads nothing (memory-bxxxxx on #263, HQB).
        if not (isinstance(cmd, (list, tuple)) and "card" in cmd and "recent" in cmd):
            return p
        if p.stdout is not None:
            # An independent read handle lets group tests observe EOF after the hook closes its handle.
            # Their fake descendants retain stdout for their entire lifetime, so EOF proves they ended.
            p._test_stdout = os.fdopen(os.dup(p.stdout.fileno()), "rb", buffering=0)
            os.set_blocking(p._test_stdout.fileno(), False)
        children.append((p, cmd, kw))
        return p

    monkeypatch.setattr(lh.subprocess, "Popen", popen)
    try:
        yield children
    finally:
        for p, _, kw in children:
            p.poll()
            if not lh.WINDOWS and kw.get("start_new_session"):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except PermissionError:
                    real.wait(p, timeout=10)
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                except ProcessLookupError:
                    pass
            if p.poll() is None:
                p.kill()
            real.wait(p, timeout=10)
            if p.stdout is not None:
                p.stdout.close()
            if getattr(p, "_test_stdout", None) is not None:
                p._test_stdout.close()


@pytest.fixture
def brief_spawns(monkeypatch):
    calls = []
    monkeypatch.setattr(lh, "spawn", lambda cfg, event: calls.append((cfg, event, lh.child_argv(cfg, event))))
    return calls


@pytest.mark.parametrize("label, seat_name", [("txxxx", "txxxx"), ("uxxx", "uxxx"),
                                              ("vxxxxxxxxxx", "vxxxxxxxxxx"), ("dxxxxx", "dxxxxx"),
                                              ("exxxx", "exxxx-session")])
def test_session_start_briefs_the_resolved_seat_after_starting_the_load(
        seat, pane, monkeypatch, recent_children, brief_spawns, label, seat_name):
    state = str(pane / (label + "-state.json"))
    base = "http://%s.invalid" % label
    _postbox(pane, [{"name": label, "enabled": False, "state": str(pane / "retired-state.json")},
                    {"name": label + "-prod", "seat": seat_name, "state": state,
                     "base_url": base, "markers": ["echo %s-shell-ok" % label]}])
    monkeypatch.setenv("MESHY_IDENTITY", label + "," + label + "-prod")
    text = "decision cards: quoted data\nRuling: café\nlast line without newline"
    recent = seat.fake("recent.py", log=str(pane / "recent.log"), stdout=text, stderr=CANARY)
    real = lh.subprocess.Popen

    def after_load(cmd, **kw):
        assert len(brief_spawns) == 1, "the worker is started before the briefing child"
        return real(cmd, **kw)

    monkeypatch.setattr(lh.subprocess, "Popen", after_load)
    # The same settings command for all five seats, with no seat-specific flags. Event comes from hook stdin.
    argv = _identity_argv(seat, "SessionStart")[2:-1] + ["--recent", recent]
    out = io.StringIO()
    assert lh.main(argv, stdin=io.StringIO('{"hook_event_name": "SessionStart"}'), stdout=out) == 0
    assert out.getvalue() == text
    store = str(pane / "cards-root" / seat_name)
    args = ["card", "recent", "--base-url", base, "--state-file", state, "--seat", seat_name,
            "--load-result", os.path.join(store, lh.RECEIPT), "--deadline", "5"]
    assert _lines(pane / "recent.log") == [{"argv": args, "cwd": lh.REPO}]
    [(p, cmd, kw)] = recent_children
    assert cmd == [sys.executable, recent] + args
    assert (kw["cwd"], kw["env"], kw["stdin"], kw["stdout"], kw["stderr"]) == (
        lh.REPO, lh._env(), subprocess.DEVNULL, subprocess.PIPE, subprocess.DEVNULL)
    if lh.WINDOWS:
        assert kw["creationflags"] == lh.CREATE_NEW_PROCESS_GROUP
    else:
        assert kw["start_new_session"] is True
    assert p.returncode == 0
    [(cfg, event, worker_argv)] = brief_spawns
    assert event == "SessionStart" and cfg.identity == label + "," + label + "-prod"
    assert cfg.seat is cfg.store is cfg.base_url is cfg.state_file is cfg.marker is None
    assert worker_argv == [sys.executable, SCRIPT, "--run", "--event=SessionStart",
                           "--identity=" + cfg.identity, "--stage-timeout=30.0", "--loader=" + seat.loader,
                           "--writer=" + seat.writer, "--project-dir=" + str(seat.proj)]


def test_session_start_unresolvable_identity_prints_one_unavailable_line(seat, pane, brief_spawns, recent_children):
    _postbox(pane, [{"name": "pxx", "enabled": False}])
    out = io.StringIO()
    assert lh.main(_identity_argv(seat, "SessionStart")[:-1], stdin=io.StringIO("{}"), stdout=out) == 0
    assert out.getvalue() == ("decision cards: not available (no label of pxx has an enabled entry in the "
                              "postbox config, which names its state file and server)\n")
    assert len(brief_spawns) == 1 and recent_children == []


def test_session_start_the_cap_holds_for_the_hooks_own_lines_too(seat, pane, brief_spawns, recent_children,
                                                                 monkeypatch):
    # A resolve() reason names the pane's labels, so it is as long as MESHY_IDENTITY: the cap covers it too.
    monkeypatch.setattr(lh, "BRIEF_MAX_BYTES", 40)
    _postbox(pane, [{"name": "pxx", "enabled": False}])
    out = io.BytesIO()
    stream = io.TextIOWrapper(out, encoding="utf-8")
    assert lh.main(_identity_argv(seat, "SessionStart")[:-1], stdin=io.StringIO("{}"), stdout=stream) == 0
    stream.flush()
    assert out.getvalue() == b"decision cards: not available (no label "
    assert recent_children == []


def test_session_start_bad_arguments_print_the_briefing_failure_line(monkeypatch):
    # GitHub Codex on #263: a broken SessionStart settings line must not fail silently on a seat's pane.
    monkeypatch.setenv("MESHY_IDENTITY", "pxx")
    out = io.StringIO()
    assert lh.main(["--event", "SessionStart", "--bogus"], stdin=io.StringIO("{}"), stdout=out) == 0
    assert out.getvalue().startswith("decision cards: not available (bad arguments: unrecognized arguments: --bogus")
    assert out.getvalue().count("\n") == 1
    monkeypatch.delenv("MESHY_IDENTITY")
    out = io.StringIO()
    assert lh.main(["--event", "SessionStart", "--bogus"], stdin=io.StringIO("{}"), stdout=out) == 0
    assert out.getvalue() == ""                                   # a pane with no identity: nothing at all
    out = io.StringIO()
    assert lh.main(["--event", "SessionStart", "--identity", "pxx", "--bogus"], stdin=io.StringIO("{}"),
                   stdout=out) == 0
    assert out.getvalue().startswith("decision cards: not available (bad arguments: ")
    out = io.StringIO()
    assert lh.main(["--event", "SessionStart", "--identity", "pxx", "--no-brief", "--bogus"],
                   stdin=io.StringIO("{}"), stdout=out) == 0
    assert out.getvalue() == ""                                   # --no-brief: a seat that wants no briefing line
    # GitHub Codex on #265: a misspelled --event on a real SessionStart (the hook input says so) is still seen.
    out = io.StringIO()
    assert lh.main(["--event", "SessionStrat", "--identity", "pxx"],
                   stdin=io.StringIO('{"hook_event_name": "SessionStart"}'), stdout=out) == 0
    assert out.getvalue().startswith("decision cards: not available (bad arguments: argument --event: invalid choice")


def test_session_start_recent_timeout_kills_and_reaps_the_child(seat, monkeypatch, recent_children, brief_spawns):
    # A budget a loaded Windows box still starts the child within (memory-bxxxxx on #265: 0.25 s ran out before the
    # spawn on a busy HQB), and a sleeper far longer than it; the child's own "started" file, not a clock, shows the
    # kill path ran. 4 s (was 1.5 s, issue #451): on a loaded gate box the child had not started inside 1.5 s (mg433a);
    # the sleeper is still five times longer, so the timeout still kills it.
    monkeypatch.setattr(lh, "BRIEF_TIMEOUT_S", 4.0)
    done, started = seat.tmp / "recent-done", seat.tmp / "recent-started"
    recent = seat.fake("recent.py", sleep=20, done=str(done), started=str(started), stderr=CANARY)
    t0 = time.monotonic()
    out = seat.hook("SessionStart", "--recent", recent, inline=False)
    elapsed = time.monotonic() - t0
    assert out == "decision cards: not available (card recent did not finish within 4 s)\n"
    assert elapsed < 4.0 + 3.0, "the timeout must kill, not wait for the sleeper to finish"
    assert len(brief_spawns) == 1
    [(p, _, _)] = recent_children
    assert _wait_for(lambda: p.poll() is not None, 10)
    assert started.exists(), "the child ran: this is the kill path, not a spawn that never happened"
    assert p.returncode != 0 and not done.exists()
    assert _wait_for(lambda: not [t for t in threading.enumerate() if t.name == "card-recent-brief"], 10)


@pytest.mark.parametrize("code, text, reason", [(3, "", "card recent exited 3"),
                                               (3, "partial briefing\n", "card recent exited 3"),
                                               (0, "", "card recent printed nothing")])
def test_session_start_recent_failure_is_one_line(seat, recent_children, brief_spawns, code, text, reason):
    recent = seat.fake("recent.py", exit=code, stdout=text, stderr=CANARY)
    assert seat.hook("SessionStart", "--recent", recent, inline=False) == \
        "decision cards: not available (%s)\n" % reason
    assert len(brief_spawns) == 1 and recent_children[0][0].returncode == code


@pytest.mark.parametrize("flags, text, loads, reads", [(["--no-brief"], "", 1, 0),
                                                      (["--brief-only"], "briefing\n", 0, 1),
                                                      (["--no-brief", "--brief-only"], "", 0, 0)])
def test_session_start_brief_flags(seat, recent_children, brief_spawns, flags, text, loads, reads):
    recent = seat.fake("recent.py", stdout="briefing\n")
    assert seat.hook("SessionStart", "--recent", recent, *flags, inline=False) == text
    assert len(brief_spawns) == loads and len(recent_children) == reads
    if len(flags) == 2:
        cfg = lh.parse(flags)
        assert cfg.no_brief and cfg.brief_only


@pytest.mark.parametrize("event, expected", [("UserPromptSubmit", ""), ("PreCompact", "card load: started\n"),
                                            ("SessionEnd", "card load: started\n")])
@pytest.mark.parametrize("flags", [["--no-brief"], ["--brief-only"], ["--no-brief", "--brief-only"]])
def test_other_events_never_brief(seat, recent_children, brief_spawns, event, expected, flags):
    recent = seat.fake("recent.py", stdout="briefing\n")
    assert seat.hook(event, "--recent", recent, *flags, inline=False) == expected
    assert len(brief_spawns) == 1 and recent_children == []


def test_session_start_with_no_identity_never_loads_or_briefs(seat, monkeypatch, recent_children, brief_spawns):
    monkeypatch.delenv("MESHY_IDENTITY")
    recent = seat.fake("recent.py", stdout="briefing\n")
    assert seat.hook("SessionStart", "--recent", recent, inline=False) == ""
    assert brief_spawns == recent_children == []


def _group_gone(p):
    if p.poll() is None:
        return False
    # Drain buffered bytes, then require EOF: a sleeping descendant retaining stdout gives None.
    # This avoids macOS killpg(0)'s EPERM for zombie-only groups and proves no fake sleeper survives.
    for _ in range(64):
        chunk = p._test_stdout.read(8192)
        if chunk is None:
            return False
        if chunk == b"":
            return True
    return False


def test_session_start_recent_output_is_cut_at_the_byte_cap(seat, recent_children, brief_spawns):
    assert lh.BRIEF_MAX_BYTES == 64 * 1024
    recent = seat.tmp / "recent.py"
    descendant = "" if lh.WINDOWS else "if os.fork() == 0:\n    time.sleep(60)\n    os._exit(0)\n"
    recent.write_text("import os, sys, time\n" + descendant +
                      "sys.stdout.buffer.write(b'x' * %d)\nsys.stdout.flush()\ntime.sleep(60)\n" %
                      (4 * lh.BRIEF_MAX_BYTES), encoding="utf-8")
    t0 = time.monotonic()
    out = seat.hook("SessionStart", "--recent", str(recent), inline=False)
    elapsed = time.monotonic() - t0
    assert out == "x" * lh.BRIEF_MAX_BYTES
    assert elapsed < lh.BRIEF_TIMEOUT_S / 2, "the cap must stop a child whose stdout stays open"
    [(p, _, _)] = recent_children
    assert p.poll() is not None
    if not lh.WINDOWS:
        assert _wait_for(lambda: _group_gone(p), 2), "the cap must kill descendants too"


@pytest.mark.skipif(lh.WINDOWS, reason="POSIX process-group descendants")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_session_start_timeout_kills_descendants_even_if_leader_exited(
        seat, monkeypatch, recent_children, brief_spawns, parent_exits):
    monkeypatch.setattr(lh, "BRIEF_TIMEOUT_S", 0.5)
    recent = seat.tmp / "recent.py"
    recent.write_text("import os, time\n"
                      "if os.fork() == 0:\n    time.sleep(60)\n    os._exit(0)\n" +
                      ("os._exit(0)\n" if parent_exits else "time.sleep(60)\n"), encoding="utf-8")
    t0 = time.monotonic()
    assert seat.hook("SessionStart", "--recent", str(recent), inline=False) == lh._brief_timeout()
    assert time.monotonic() - t0 < 0.8
    [(p, _, _)] = recent_children
    assert _wait_for(lambda: _group_gone(p), 2), "timeout must kill the group even after the leader exits"


def test_session_start_lifecycle_runs_in_one_worker_and_waits_are_bounded(
        seat, monkeypatch, recent_children, brief_spawns):
    real_resolve, real_popen = lh.resolve, lh.subprocess.Popen
    calls, waits = [], []

    def record_resolve(cfg):
        calls.append(("resolve", threading.current_thread()))
        return real_resolve(cfg)

    def record_popen(*a, **kw):
        calls.append(("start", threading.current_thread()))
        p = real_popen(*a, **kw)
        real_wait, raw = p.wait, p.stdout

        def wait(timeout=None):
            assert timeout is not None and 0 <= timeout <= lh.BRIEF_TIMEOUT_S
            waits.append(timeout)
            calls.append(("wait", threading.current_thread()))
            return real_wait(timeout=timeout)

        class Pipe:
            def fileno(self):
                return raw.fileno()

            def read(self, n):
                calls.append(("read", threading.current_thread()))
                return raw.read(n)

            def close(self):
                calls.append(("close", threading.current_thread()))
                return raw.close()

        p.wait, p.stdout = wait, Pipe()
        return p

    monkeypatch.setattr(lh, "resolve", record_resolve)
    monkeypatch.setattr(lh.subprocess, "Popen", record_popen)
    recent = seat.fake("recent.py", stdout="ready\n")
    assert seat.hook("SessionStart", "--recent", recent, inline=False) == "ready\n"
    assert {name for name, _ in calls} == {"resolve", "start", "read", "wait", "close"}
    worker = calls[0][1]
    assert worker.daemon and worker is not threading.current_thread()
    assert all(thread is worker for _, thread in calls)
    assert waits


def test_session_start_cleanup_wait_cannot_hold_main_past_deadline(
        seat, monkeypatch, recent_children, brief_spawns):
    monkeypatch.setattr(lh, "BRIEF_TIMEOUT_S", 0.25)
    real_popen = lh.subprocess.Popen
    release, finished, stopped = threading.Event(), threading.Event(), threading.Event()
    workers = []
    real_stop = lh._stop_recent

    def stop(p):
        real_stop(p)
        stopped.set()

    monkeypatch.setattr(lh, "_stop_recent", stop)

    def popen(*a, **kw):
        p = real_popen(*a, **kw)
        real_wait = p.wait

        def wait(timeout=None):
            workers.append(threading.current_thread())
            if stopped.is_set() and not finished.is_set():   # stall the cleanup wait only, whenever it comes
                try:
                    assert timeout is not None and timeout <= lh.BRIEF_TIMEOUT_S
                    release.wait(1.2)
                finally:
                    finished.set()
            return real_wait(timeout=timeout)

        p.wait = wait
        return p

    monkeypatch.setattr(lh.subprocess, "Popen", popen)
    recent = seat.fake("recent.py", stdout="ready\n")
    t0 = time.monotonic()
    try:
        assert seat.hook("SessionStart", "--recent", recent, inline=False) == lh._brief_timeout()
        assert time.monotonic() - t0 < 0.55
    finally:
        release.set()
        assert finished.wait(2)
        workers[0].join(timeout=2)


def test_session_start_resolve_is_inside_the_deadline_and_never_writes_late(
        seat, monkeypatch, brief_spawns, recent_children):
    monkeypatch.setattr(lh, "BRIEF_TIMEOUT_S", 0.15)
    release, finished = threading.Event(), threading.Event()
    threads, writes = [], []

    def stalled(cfg):
        threads.append(threading.current_thread())
        release.wait(1.2)                           # also finite when resolution is moved to main by a mutant
        finished.set()
        return "late resolution result"

    class Output(io.StringIO):
        def write(self, text):
            writes.append((threading.current_thread(), text))
            return super().write(text)

    monkeypatch.setattr(lh, "resolve", stalled)
    out = Output()
    t0 = time.monotonic()
    try:
        assert lh.main(seat.argv("SessionStart", inline=False), stdin=io.StringIO("{}"), stdout=out) == 0
        elapsed = time.monotonic() - t0
        assert elapsed < 0.6, "a stalled postbox config read must spend only the briefing deadline"
        assert out.getvalue() == "decision cards: not available (card recent did not finish within 0.15 s)\n"
        assert len(threads) == 1 and threads[0].daemon and threads[0] is not threading.current_thread()
        assert writes == [(threading.current_thread(), out.getvalue())]
        assert recent_children == []
    finally:
        release.set()
        assert finished.wait(2)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
    assert len(writes) == 1, "the released worker must never print after main returns"


def test_session_start_resolve_and_child_share_one_deadline(seat, monkeypatch, brief_spawns, recent_children):
    # resolve() takes 1.2 s of a 1.5 s budget and the child would take 20 s. One shared deadline ends the run near
    # 1.5 s; a deadline of its own for each part would run past 1.2 + 1.5 = 2.7 s. The bound sits between the two,
    # with room for a loaded box (memory-bxxxxx on #265: the 0.5 s version failed on a busy HQB).
    monkeypatch.setattr(lh, "BRIEF_TIMEOUT_S", 1.5)
    real = lh.resolve

    def slow(cfg):
        time.sleep(1.2)
        return real(cfg)

    monkeypatch.setattr(lh, "resolve", slow)
    recent = seat.fake("recent.py", sleep=20, stdout="too late\n")
    t0 = time.monotonic()
    assert seat.hook("SessionStart", "--recent", recent, inline=False) == lh._brief_timeout()
    assert time.monotonic() - t0 < 2.6
    # A child may or may not have started in the 0.3 s left; one that did is ended.
    assert all(_wait_for(lambda: p.poll() is not None, 10) for p, _, _ in recent_children)


@pytest.mark.parametrize("kind, expected", [("ValueError", "ValueError"), ("pkg.Bad_Answer", "pkg.Bad_Answer"),
                                           ("123bad", "unknown"), ("", "unknown")])
def test_session_start_child_failure_line_never_exposes_secrets(
        seat, recent_children, brief_spawns, kind, expected):
    recent = seat.fake("recent.py", stdout="before café\ndecision cards: not available (%s: CANARY_TOKEN)\nafter" % kind)
    out = seat.hook("SessionStart", "--recent", recent, inline=False)
    assert "CANARY_TOKEN" not in out
    assert out == ("before café\ndecision cards: not available (%s; run `card recent` by hand to see why)\nafter" % expected)


def test_session_start_replacement_utf8_is_capped_on_printed_bytes(seat, recent_children, brief_spawns):
    recent = seat.tmp / "recent.py"
    recent.write_text("import sys; sys.stdout.buffer.write(b'\\xff' * 65536)", encoding="utf-8")
    out = seat.hook("SessionStart", "--recent", str(recent), inline=False)
    assert len(out.encode("utf-8")) <= lh.BRIEF_MAX_BYTES
    assert out == "\ufffd" * (lh.BRIEF_MAX_BYTES // 3)


def test_session_start_writes_utf8_even_to_a_cp1252_stdout(seat, recent_children, brief_spawns):
    recent = seat.fake("recent.py", stdout="café")
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252")
    assert lh.main(seat.argv("SessionStart", "--recent", recent, inline=False),
                   stdin=io.StringIO("{}"), stdout=out) == 0
    assert raw.getvalue() == b"caf\xc3\xa9"


def test_session_start_recent_decodes_invalid_utf8_with_replacement(seat, recent_children, brief_spawns):
    recent = seat.tmp / "recent.py"
    recent.write_text("import sys; sys.stdout.buffer.write(b'brief \\xff')", encoding="utf-8")
    assert seat.hook("SessionStart", "--recent", str(recent), inline=False) == "brief \ufffd"


@pytest.mark.parametrize("where", ["resolve", "Popen"])
def test_session_start_brief_exception_names_only_its_type(seat, monkeypatch, brief_spawns, where):
    def boom(*a, **kw):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(lh if where == "resolve" else lh.subprocess, where, boom)
    assert seat.hook("SessionStart", inline=False) == "decision cards: not available (RuntimeError)\n"
    assert len(brief_spawns) == 1


@pytest.mark.parametrize("flags", [["--no-brief"], ["--brief-only"], ["--no-brief", "--brief-only"]])
def test_recent_real_command_and_worker_mode_never_briefs(seat, recent_children, flags):
    cfg = seat.cfg()
    assert lh.recent_cmd(cfg) == [sys.executable, "-m", "tools.meshy_node", "card", "recent", "--base-url",
                                   cfg.base_url, "--state-file", cfg.state_file, "--seat", cfg.seat,
                                   "--load-result", os.path.join(cfg.store, lh.RECEIPT), "--deadline", "5"]
    recent = seat.fake("recent.py", stdout="briefing\n")
    out = seat.hook("SessionStart", "--run", *flags, "--recent", recent, inline=False)
    assert json.loads(out)["status"] == "ok" and len(seat.loads()) == 1
    assert all(cmd[1] != recent for _, cmd, _ in recent_children)


def test_session_start_worker_exception_never_prints_a_briefing(seat, monkeypatch, brief_spawns):
    def boom(*a, **kw):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(lh, "run_worker", boom)
    assert seat.hook("SessionStart", "--run", "--brief-only", inline=False) == ""
    assert brief_spawns == []


@pytest.mark.parametrize("value", [None, "", " , ", "../escape", "a/b"])
def test_a_pane_with_no_identity_does_nothing_at_all(seat, pane, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MESHY_IDENTITY")
    else:
        monkeypatch.setenv("MESHY_IDENTITY", value)
    spawned = []
    monkeypatch.setattr(lh, "spawn", lambda cfg, event: spawned.append(event))
    for event in lh.EVENTS:
        out = io.StringIO()
        assert lh.main(seat.argv(event, inline=False), stdin=io.StringIO("{}"), stdout=out) == 0
        assert out.getvalue() == ""
        assert lh.main(_identity_argv(seat, event), stdin=io.StringIO("{}"), stdout=out) == 0
        assert out.getvalue() == ""
    assert spawned == [] and seat.loads() == [] and not seat.store.exists()
    assert not (pane / "cards-root").exists()
    p = subprocess.run([sys.executable, SCRIPT] + seat.argv("SessionEnd", inline=False), input="{}",
                       capture_output=True, text=True, timeout=60,
                       env={k: v for k, v in _no_git_env().items() if k != "MESHY_IDENTITY"})
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


def test_two_identities_in_one_profile_load_into_their_own_stores(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "alpha", "state": str(pane / "alpha-state.json")},
                    {"name": "beta", "state": str(pane / "beta-state.json"), "base_url": "http://beta.invalid"}])
    root = pane / "cards-root"
    for value in ("alpha", "beta,beta-prod"):
        monkeypatch.setenv("MESHY_IDENTITY", value)
        out = io.StringIO()
        assert lh.main(_identity_argv(seat, "SessionEnd"), stdin=io.StringIO("{}"), stdout=out) == 0
        assert out.getvalue() == "card load: started\n"
    alpha, beta = seat.loads()
    key = {s: lh.request_key(s, C(seat.proj)) for s in ("alpha", "beta")}
    assert alpha["argv"] == ["--project-dir=" + C(seat.proj), "--seat=alpha",
                             "--out=" + str(root / "alpha" / lh.PROJECTS / key["alpha"])]
    assert beta["argv"] == ["--project-dir=" + C(seat.proj), "--seat=beta",
                            "--out=" + str(root / "beta" / lh.PROJECTS / key["beta"])]
    assert [w["argv"][-4:] for w in seat.writes()] == [
        ["--base-url", "http://stand-in.invalid", "--state-file", str(pane / "alpha-state.json")],
        ["--base-url", "http://beta.invalid", "--state-file", str(pane / "beta-state.json")]]
    for ident in ("alpha", "beta"):
        assert lh.read_receipt(str(root / ident))["seat"] == ident


def test_an_identitys_seat_label_and_a_given_flag_win(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "bxxxxx", "seat": "memory-bxxxxx", "state": str(pane / "bxxxxx-state.json")}])
    monkeypatch.setenv("MESHY_IDENTITY", "bxxxxx")
    lh.main(_identity_argv(seat, "SessionEnd"), stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert seat.loads()[0]["argv"][1] == "--seat=memory-bxxxxx"
    explicit = pane / "explicit-store"
    lh.main(_identity_argv(seat, "SessionEnd") + ["--store", str(explicit), "--seat", "mine"],
            stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert seat.loads()[1]["argv"][1:] == ["--seat=mine", "--out=" + str(explicit / lh.PROJECTS /
                                                                         lh.request_key("mine", C(seat.proj)))]


def test_an_identity_the_postbox_does_not_name_is_not_loaded_and_says_why(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "alpha", "state": str(pane / "alpha-state.json")}])
    monkeypatch.setenv("MESHY_IDENTITY", "gamma")
    assert lh.main(_identity_argv(seat, "SessionEnd"), stdin=io.StringIO("{}"), stdout=io.StringIO()) == 0
    assert seat.loads() == []
    receipt = lh.read_receipt(str(pane / "cards-root" / "gamma"))
    assert receipt["status"] == "failed" and receipt["detail"] == (
        "not loaded: no label of gamma has an enabled entry in the postbox config, which names its state file and "
        "server; nothing loaded")
    out = io.StringIO()
    lh.main(_identity_argv(seat, "SessionEnd")[2:-1] + ["--run"], stdin=io.StringIO(""), stdout=out)
    assert out.getvalue().startswith("card load: not loaded: no label of gamma has an enabled entry")


def test_a_postbox_config_that_is_not_this_users_regular_file_names_no_one(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "pxx", "state": str(pane / "pxx-state.json")}])
    assert lh.postbox_identity(["pxx"])[0]["state"] == str(pane / "pxx-state.json")
    monkeypatch.setattr(lh, "_mine", lambda st: False)
    assert lh.postbox_identity(["pxx"]) == (None, None)


def test_the_pane_identity_is_the_first_plain_label():
    assert lh.pane_identity("txxxx,txxxx-prod") == "txxxx"
    assert lh.pane_identity(" pxx ") == "pxx" and lh.pane_identity(",beta") == "beta"
    for bad in (None, "", " , ", "../x", "a/b", "a\\b", ".", "..", "x" * 65):
        assert lh.pane_identity(bad) is None, bad


# ---- txxxx on #217, 2: a continuation transcript, named to a loader that takes --seat-file


def test_a_continuation_transcript_is_named_to_a_loader_that_takes_seat_file(seat):
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       declares_seat_file=True)
    (seat.proj / "S2.jsonl").write_text("{}\n", encoding="utf-8")       # a continuation: no seat marker in it
    seat.hook("SessionEnd", project=False, loader=loader,
              stdin=json.dumps({"transcript_path": str(seat.proj / "S2.jsonl")}))
    assert seat.loads()[0]["argv"] == ["--project-dir=" + C(seat.proj), "--seat=pxx", "--out=" + str(seat.pstore()),
                                       "--seat-file=S2.jsonl"]
    assert "seat-file" not in seat.result()["detail"]


def test_a_loader_without_seat_file_gets_none_and_the_receipt_says_so(seat):
    (seat.proj / "S2.jsonl").write_text("{}\n", encoding="utf-8")
    seat.hook("SessionEnd", project=False, stdin=json.dumps({"transcript_path": str(seat.proj / "S2.jsonl")}))
    assert seat.loads()[0]["argv"] == ["--project-dir=" + C(seat.proj), "--seat=pxx", "--out=" + str(seat.pstore())]
    assert seat.result()["detail"].endswith("; --seat-file not passed (the loader has no such flag)")


def test_a_transcript_not_written_yet_is_not_named_and_the_folder_still_loads(seat):
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       declares_seat_file=True)
    seat.hook("SessionStart", "--no-brief", project=False, loader=loader,
              stdin=json.dumps({"transcript_path": str(seat.proj / "new.jsonl")}))
    assert seat.loaded() == [C(seat.proj)] and not any("seat-file" in a for a in seat.loads()[0]["argv"])
    assert seat.result()["detail"].endswith("; --seat-file not passed (a transcript is not there yet)")


def test_the_real_loaders_flag_is_found_by_reading_its_source():
    source = open(REAL_LOADER, encoding="utf-8").read()
    assert lh.loader_takes_seat_file(REAL_LOADER) is bool(lh.SEAT_FILE_FLAG_RE.search(source))


@pytest.mark.parametrize("name", ["../x.jsonl", "a/b.jsonl", "x.json", "C:x.jsonl", "nul\x00.jsonl", ".jsonl"])
def test_a_transcript_name_that_is_not_a_plain_jsonl_name_is_never_passed(seat, name):
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       declares_seat_file=True)
    out = io.StringIO()
    assert lh.main(seat.argv("SessionEnd", loader=loader) + ["--transcript", name], stdin=io.StringIO("{}"),
                   stdout=out) == 0
    assert out.getvalue() == "card load: started\n"
    assert not any("seat-file" in a for a in seat.loads()[0]["argv"])
    assert lh.plain_transcript(name) is None


def test_a_transcript_that_is_a_link_or_a_folder_sets_the_request_aside(seat):
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       declares_seat_file=True)
    b, c = seat.project("projB"), seat.project("projC")
    (b / "S9.jsonl").mkdir()
    seat.enqueue(b, transcript="S9.jsonl", time=1)
    oddities = [seat.key(b) + ".json" + lh.CLAIMED + lh.BAD]
    (seat.tmp / "outside.jsonl").write_text("{}\n", encoding="utf-8")
    try:
        os.symlink(str(seat.tmp / "outside.jsonl"), str(c / "S8.jsonl"))
    except (OSError, NotImplementedError):
        pass
    else:
        seat.enqueue(c, transcript="S8.jsonl", time=2)
        oddities.append(seat.key(c) + ".json" + lh.CLAIMED + lh.BAD)
    lh.run_worker(seat.cfg(loader=loader), "SessionStart")
    assert seat.loaded() == [C(seat.proj)] and seat.set_aside() == sorted(oddities)


def test_an_identitys_markers_come_from_its_postbox_entry_unless_given(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "exxxx", "seat": "exxxx-session", "state": str(pane / "exxxx-state.json"),
                     "markers": ["echo exxxx-shell-ok"]}])
    monkeypatch.setenv("MESHY_IDENTITY", "exxxx")
    lh.main(_identity_argv(seat, "SessionEnd"), stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert seat.loads()[0]["argv"][1] == "--seat=exxxx-session"
    assert seat.loads()[0]["argv"][-1] == "--marker=echo exxxx-shell-ok"
    lh.main(_identity_argv(seat, "SessionEnd") + ["--marker", "mine"], stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert seat.loads()[1]["argv"][-1] == "--marker=mine"


def test_a_queue_that_never_yields_a_request_does_not_keep_the_worker_going_round(seat, monkeypatch, successors):
    """Something stays in pending/ that no scan can take or set aside. The worker goes round at most once more,
    calls it held up, and hands on to one successor that waits; it does not spin on the lock."""
    seat.enqueue(seat.project("projB"))
    scans = []

    def nothing_to_take(store):
        scans.append(1)
        assert len(scans) < 10, "the worker is spinning"
        return []

    monkeypatch.setattr(lh, "queued", nothing_to_take)
    seat.put_result("ok", 60)
    lh.run_worker(seat.cfg(), "UserPromptSubmit")
    assert len(scans) == 2 and seat.loads() == []
    assert [(c["wait"], c["lock_free"]) for c in successors] == [(lh.SUCCESSOR_WAIT_S, True)]



# ---- #217 (GitHub Codex, P1 on 221137d): no queued transcript name is ever lost


def _seat_files(seat, project):
    """For each loader stage of `project`, in order, the transcript names it was given."""
    return [[a.split("=", 1)[1] for a in x["argv"] if a.startswith("--seat-file=")]
            for x in seat.loads() if x["argv"][0] == "--project-dir=" + C(project)]


def test_two_continuations_queued_while_the_lock_is_held_both_reach_the_loader(seat):
    """Real processes. A holder is in its pass of folder X. Two continuation sessions of folder P end meanwhile:
    each worker queues its request and finds the lock held. On 221137d both wrote the one file of folder P and the
    later replaced the earlier, so one transcript name was lost for good (a continuation holds no seat marker).
    Now each (folder, transcript) has its own file, and the holder's pass of P gives the loader both names."""
    x, p = seat.project("projX"), seat.project("projP")
    for name in ("T1.jsonl", "T2.jsonl"):
        (p / name).write_text("{}\n", encoding="utf-8")
    started, release = seat.tmp / "started", seat.tmp / "release"
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       gate_on=os.sep + "projX", started=str(started), wait_for=str(release),
                       declares_seat_file="append")
    holder = subprocess.Popen([sys.executable, SCRIPT] + seat.argv(None, inline=False, loader=loader, project_dir=x)
                              + ["--run", "--event=SessionEnd"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_no_git_env())
    try:
        assert _wait_for(started.exists, 30), "the holder is in its pass of X"
        for name in ("T1.jsonl", "T2.jsonl"):
            worker = subprocess.run([sys.executable, SCRIPT] + seat.argv(None, inline=False, loader=loader,
                                                                         project_dir=p)
                                    + ["--run", "--event=SessionEnd", "--transcript=" + name],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=60, env=_no_git_env())
            assert (worker.returncode, worker.stderr) == (0, b"")
        assert not _lock_free(seat.store), "both queued while the holder held the lock"
    finally:
        release.touch()
        _, err = holder.communicate(timeout=60)
    assert (holder.returncode, err) == (0, b"")
    assert _wait_for(lambda: (seat.result(p) or {}).get("status") == "ok", 60)
    names = [n for stage in _seat_files(seat, p) for n in stage]
    assert sorted(names) == ["T1.jsonl", "T2.jsonl"], names
    assert _wait_for(lambda: seat.pending() == [] and _lock_free(seat.store), 30)


def _queue_two_continuations(seat, project, *, mode="append"):
    for name in ("T1.jsonl", "T2.jsonl"):
        (project / name).write_text("{}\n", encoding="utf-8")
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       declares_seat_file=mode)
    fd = seat.hold_lock()
    for name in ("T1.jsonl", "T2.jsonl"):
        assert seat.hook("SessionEnd", "--transcript", name, loader=loader, project_dir=project) == \
            "card load: started\n"
    lh.release_lock(fd)
    seat.cleanups.clear()
    return loader


def test_a_loader_that_takes_the_flag_more_than_once_gets_every_name_in_one_stage(seat):
    loader = _queue_two_continuations(seat, seat.proj, mode="append")
    assert len(seat.pending()) == 2, "one queue file a (folder, transcript)"
    lh.run_worker(seat.cfg(loader=loader), "SessionStart")
    assert _seat_files(seat, seat.proj) == [["T1.jsonl", "T2.jsonl"]] and len(seat.writes()) == 1
    assert seat.pending() == []


def test_a_loader_that_takes_the_flag_once_gets_one_stage_a_name_and_one_write(seat):
    loader = _queue_two_continuations(seat, seat.proj, mode=True)
    lh.run_worker(seat.cfg(loader=loader), "SessionStart")
    assert _seat_files(seat, seat.proj) == [["T1.jsonl"], ["T2.jsonl"]] and len(seat.writes()) == 1
    assert seat.result()["status"] == "ok" and seat.pending() == []


def test_the_flag_is_found_as_it_is_declared(seat):
    once = seat.fake("once.py", declares_seat_file=True)
    many = seat.fake("many.py", declares_seat_file="append")
    none = seat.fake("none.py")
    assert (lh.seat_file_mode(once), lh.seat_file_mode(many), lh.seat_file_mode(none)) == ("each", "append", None)
    other = seat.tmp / "other.py"
    other.write_text('ap.add_argument("--seat-file", help="one (only)")\nap.add_argument("--x", action="append")\n',
                     encoding="utf-8")
    assert lh.seat_file_mode(str(other)) == "each", "another flag's action is not this one's"


def test_one_folders_requests_go_in_one_pass_and_a_bad_one_does_not_hold_the_others(seat):
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       declares_seat_file="append")
    for name in ("T1.jsonl", "T2.jsonl"):
        (seat.proj / name).write_text("{}\n", encoding="utf-8")
    (seat.proj / "T3.jsonl").mkdir()                         # names no regular file
    seat.mkdirs(lh.PENDING)
    for i, name in enumerate([None, "T1.jsonl", "T2.jsonl", "T3.jsonl"]):
        req = {"project_dir": C(seat.proj), "time": 1000 + i}
        if name:
            req["transcript"] = name
        assert lh.write_json(str(seat.store / lh.PENDING / lh.queue_name("pxx", C(seat.proj), name)), req)
    seat.put_result("ok", 60)
    lh.run_worker(seat.cfg(loader=loader), "UserPromptSubmit")
    assert _seat_files(seat, seat.proj) == [["T1.jsonl", "T2.jsonl"]], "one pass, both good names"
    assert seat.pending() == [] and seat.set_aside() == [lh.queue_name("pxx", C(seat.proj), "T3.jsonl") + lh.CLAIMED
                                                         + lh.BAD]


def test_a_pass_takes_no_more_than_max_group_requests_of_one_folder(seat):
    loader = seat.fake("l.py", log=str(seat.loader_log), cards=[card(1)], stdout=json.dumps(REPORT),
                       declares_seat_file="append")
    seat.mkdirs(lh.PENDING)
    names = ["T%02d.jsonl" % i for i in range(lh.MAX_GROUP + 2)]
    for i, name in enumerate(names):
        (seat.proj / name).write_text("{}\n", encoding="utf-8")
        lh.write_json(str(seat.store / lh.PENDING / lh.queue_name("pxx", C(seat.proj), name)),
                      {"project_dir": C(seat.proj), "transcript": name, "time": 1000 + i})
    seat.put_result("ok", 60)
    lh.run_worker(seat.cfg(loader=loader), "UserPromptSubmit")
    stages = _seat_files(seat, seat.proj)
    assert lh.MAX_GROUP == 8 and len(stages[0]) == lh.MAX_GROUP
    assert sorted(n for stage in stages for n in stage) == names and seat.pending() == []


def test_one_queue_file_a_folder_and_a_transcript():
    p = "/x/proj"
    assert lh.queue_name("pxx", p, None) == lh.request_key("pxx", p) + ".json"
    one, two = lh.queue_name("pxx", p, "T1.jsonl"), lh.queue_name("pxx", p, "T2.jsonl")
    assert one != two and one == lh.queue_name("pxx", p, "T1.jsonl")
    assert one.startswith(lh.request_key("pxx", p) + ".t") and one.endswith(".json")



# ---- txxxx's re-review of 221137d (1b): the serving identity is the first ENABLED label; the seat, the first label


def _hqb_postbox(pane):
    """HQB's shape, as txxxx read it: the bare seat is an old test-server identity, turned off and with no server of
    its own, so the config's top-level server is the test server; <seat>-prod is on, with its own server and state."""
    return _postbox(pane, [
        {"name": "txxxx", "enabled": False, "state": str(pane / "txxxx-node.json")},
        {"name": "txxxx-prod", "enabled": True, "base_url": "http://prod.invalid:6310",
         "state": str(pane / "txxxx-prod-state.json")},
    ], base_url="http://test-server.invalid:17070")


def _one_load(seat, value, monkeypatch, **kw):
    monkeypatch.setenv("MESHY_IDENTITY", value)
    out = io.StringIO()
    assert lh.main(_identity_argv(seat, "SessionEnd", **kw), stdin=io.StringIO("{}"), stdout=out) == 0
    return out.getvalue()


def test_a_hqb_pane_loads_as_its_seat_against_its_enabled_production_identity(seat, pane, monkeypatch):
    _hqb_postbox(pane)
    assert _one_load(seat, "txxxx,txxxx-prod", monkeypatch) == "card load: started\n"
    [load], [write] = seat.loads(), seat.writes()
    assert load["argv"][1] == "--seat=txxxx", "the bare roster label: existing card keys are card/txxxx/..."
    assert write["argv"][-4:] == ["--base-url", "http://prod.invalid:6310", "--state-file",
                                  str(pane / "txxxx-prod-state.json")], "never the test server or the retired state"
    assert lh.read_receipt(str(pane / "cards-root" / "txxxx"))["seat"] == "txxxx"


def test_the_enabled_label_may_come_first(seat, pane, monkeypatch):
    _hqb_postbox(pane)
    _one_load(seat, "txxxx-prod,txxxx", monkeypatch)
    assert seat.loads()[0]["argv"][1] == "--seat=txxxx-prod", "the seat is the first label of the pane"
    assert seat.writes()[0]["argv"][-3] == "http://prod.invalid:6310"


def test_the_serving_entrys_own_seat_wins(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "bxxxxx", "enabled": False, "state": str(pane / "bxxxxx-node.json")},
                    {"name": "bxxxxx-prod", "seat": "memory-bxxxxx", "base_url": "http://prod.invalid:6310",
                     "state": str(pane / "bxxxxx-prod-state.json")}], base_url="http://test-server.invalid:17070")
    _one_load(seat, "bxxxxx,bxxxxx-prod", monkeypatch)
    assert seat.loads()[0]["argv"][1] == "--seat=memory-bxxxxx"
    assert seat.loads()[0]["argv"][2].startswith("--out=" + str(pane / "cards-root" / "memory-bxxxxx"))
    assert seat.writes()[0]["argv"][-1] == str(pane / "bxxxxx-prod-state.json")


def test_an_entry_with_no_enabled_key_is_on_as_meshy_mod_reads_it(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "uxxx", "state": str(pane / "uxxx-state.json")}])
    _one_load(seat, "uxxx", monkeypatch)
    assert seat.writes()[0]["argv"][-1] == str(pane / "uxxx-state.json")


def test_a_pane_whose_every_label_is_off_loads_nothing_and_says_why(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "txxxx", "enabled": False, "state": str(pane / "a.json")},
                    {"name": "txxxx-prod", "enabled": False, "state": str(pane / "b.json")}])
    assert _one_load(seat, "txxxx,txxxx-prod", monkeypatch) == "card load: started\n"
    assert seat.loads() == []
    assert lh.read_receipt(str(pane / "cards-root" / "txxxx"))["detail"] == (
        "not loaded: no label of txxxx,txxxx-prod has an enabled entry in the postbox config, which names its state "
        "file and server; nothing loaded")


def test_the_pane_labels_are_every_plain_label_in_order():
    assert lh.pane_labels("txxxx,txxxx-prod") == ["txxxx", "txxxx-prod"]
    assert lh.pane_labels(" a , b/../c , d ") == ["a", "d"], "a label that is not plain is left out"
    assert lh.pane_labels("../x,txxxx") == [], "the first label names the seat: it must be plain"
    assert lh.pane_labels("") == [] and lh.pane_labels(None) == []


def test_of_two_enabled_labels_the_first_serves(seat, pane, monkeypatch):
    _postbox(pane, [{"name": "jxxx", "base_url": "http://one.invalid", "state": str(pane / "jxxx-state.json")},
                    {"name": "jxxx-prod", "base_url": "http://two.invalid", "state": str(pane / "jxxx-prod.json")}])
    _one_load(seat, "jxxx,jxxx-prod", monkeypatch)
    assert seat.writes()[0]["argv"][-4:] == ["--base-url", "http://one.invalid", "--state-file",
                                             str(pane / "jxxx-state.json")]


# ---- #297: --emit-context in load_hook.py and seat-wide Context head supersession


def test_emit_context_is_passed_to_a_loader_that_declares_it_and_noted_when_it_does_not(seat, pane, monkeypatch):
    assert lh.loader_takes_emit_context(REAL_LOADER) is True
    assert lh.loader_takes_emit_context(seat.loader) is False

    # A comment mentioning --emit-context does not count as declaring the flag (#326).
    commented_loader = seat.tmp / "commented_ctx_loader.py"
    commented_loader.write_text(
        Path(seat.loader).read_text(encoding="utf-8")
        + '\n# ap.add_argument("--emit-context", action="store_true")\n',
        encoding="utf-8",
    )
    assert lh.loader_takes_emit_context(str(commented_loader)) is False

    # A loader that does not declare --emit-context gets none, and the receipt says so.
    seat.hook("SessionEnd", "--emit-context")
    assert seat.loads()[0]["argv"] == ["--project-dir=" + C(seat.proj), "--seat=pxx", "--out=" + str(seat.pstore())]
    assert seat.result()["detail"].endswith("; --emit-context not passed (the loader has no such flag)")

    # A loader that declares --emit-context receives it at the end of argv, whether given by CLI or postbox config.
    ctx_loader = seat.tmp / "ctx_loader.py"
    ctx_loader.write_text(
        Path(seat.loader).read_text(encoding="utf-8")
        + '\nif False:\n    ap.add_argument("--emit-context", action="store_true")\n',
        encoding="utf-8",
    )
    assert lh.loader_takes_emit_context(str(ctx_loader)) is True
    _postbox(pane, [{"name": "exxxx", "seat": "exxxx-session", "state": str(pane / "exxxx-state.json"),
                     "emit_context": True}])
    monkeypatch.setenv("MESHY_IDENTITY", "exxxx")
    lh.main(_identity_argv(seat, "SessionEnd", loader=str(ctx_loader)), stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert seat.loads()[-1]["argv"][-1] == "--emit-context"

    # Also run REAL_LOADER via postbox emit_context=True so transcript_cards.py's real argparse parses --emit-context.
    _transcript(seat.proj, "real_ctx.jsonl", "echo exxxx-shell-ok", "Check the eastern weir before noon.")
    lh.main(_identity_argv(seat, "SessionEnd", loader=REAL_LOADER), stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert seat.result()["status"] == "ok"

    cfg = seat.cfg()
    cfg.emit_context = True
    assert "--emit-context" in lh._trusted_args(cfg)


def test_emit_context_chains_supersedes_across_project_folders_and_rolls_back_on_refused_write(seat):
    a, b, empty = seat.project("projA"), seat.project("projB"), seat.project("projEmpty")
    _transcript(a, "a1.jsonl", "echo pxx-shell-ok", "Paint the A barn red before Friday, please.")

    # 1. Load project A with --emit-context: emits first Context head id_a (supersedes absent).
    seat.hook("SessionEnd", "--emit-context", project_dir=a, loader=REAL_LOADER)
    a_cards = [json.loads(x) for x in (seat.pstore(a) / "cards.jsonl").read_text(encoding="utf-8").splitlines() if x]
    a_ctx = [c for c in a_cards if c.get("type") == "Context"]
    assert len(a_ctx) == 1 and "supersedes" not in a_ctx[0]
    id_a = a_ctx[0]["id"]
    root_head = json.loads((seat.store / lh.CONTEXT_HEAD).read_text(encoding="utf-8"))
    assert root_head["id"] == id_a

    # 2. Load empty project folder (no transcripts, no context signal): foreign Context card is unseeded, so 0 cards are written.
    seat.hook("SessionEnd", "--emit-context", project_dir=empty, loader=REAL_LOADER)
    assert seat.result(empty)["status"] == "ok"
    assert seat.result(empty)["detail"].startswith("0 cards, nothing to write;")
    assert (seat.pstore(empty) / "cards.jsonl").read_text(encoding="utf-8").strip() == ""

    # 3. Add operator prompt in project B and load with --emit-context: B's Context card supersedes A's head id_a.
    _transcript(b, "b1.jsonl", "echo pxx-shell-ok", "Move the B hives to the east field tonight.")
    seat.hook("SessionEnd", "--emit-context", project_dir=b, loader=REAL_LOADER)
    b_cards = [json.loads(x) for x in (seat.pstore(b) / "cards.jsonl").read_text(encoding="utf-8").splitlines() if x]
    b_ctx = [c for c in b_cards if c.get("type") == "Context"]
    assert len(b_ctx) == 1
    id_b = b_ctx[0]["id"]
    assert id_b != id_a
    assert b_ctx[0].get("supersedes") == ["card/pxx/" + id_a]
    assert json.loads((seat.store / lh.CONTEXT_HEAD).read_text(encoding="utf-8"))["id"] == id_b

    # 4. Add new prompt in project A and load with a writer that fails: A rolls back and root head stays id_b.
    _transcript(a, "a1.jsonl", "echo pxx-shell-ok", "Paint the A barn red before Friday, please.",
                "Lock the south gate after sunset every evening.")
    bad_writer = seat.fake("bad_writer.py", log=str(seat.writer_log), exit=1,
                           stdout=json.dumps({"cards": 3, "outcomes": {"refused": 1}, "written": [],
                                              "problems": [{"outcome": "refused", "error_code": "E_CARD_SCHEMA"}]}))
    seat.hook("SessionEnd", "--emit-context", project_dir=a, loader=REAL_LOADER, writer=bad_writer)
    assert seat.result(a)["status"] == "failed"
    assert json.loads((seat.store / lh.CONTEXT_HEAD).read_text(encoding="utf-8"))["id"] == id_b
    a_cursor = json.loads((seat.pstore(a) / "cursor.json").read_text(encoding="utf-8"))
    assert a_cursor.get("context_head_id") == id_b

    # 5. Re-run project A with working writer: A's Context card supersedes B's head id_b and updates root head.
    seat.hook("SessionEnd", "--emit-context", project_dir=a, loader=REAL_LOADER)
    assert seat.result(a)["status"] == "ok"
    a_cards_after = [json.loads(x) for x in (seat.pstore(a) / "cards.jsonl").read_text(encoding="utf-8").splitlines() if x]
    a_ctx_after = [c for c in a_cards_after if c.get("type") == "Context"]
    assert len(a_ctx_after) == 1
    id_a2 = a_ctx_after[0]["id"]
    assert id_a2 not in (id_a, id_b)
    assert a_ctx_after[0].get("supersedes") == ["card/pxx/" + id_b]
    assert json.loads((seat.store / lh.CONTEXT_HEAD).read_text(encoding="utf-8"))["id"] == id_a2


def _make_supersedes_writer(tmp: Path, name: str, active_card_file: Path) -> str:
    """Fake writer that enforces server-side Context supersession against ``active_card_file`` and answers
    ``card list --type Context`` with the current active Context card."""
    script = tmp / name
    script.write_text(
        f"""import json, sys
active_path = {repr(str(active_card_file))}
argv = sys.argv[1:]
if "list" in argv:
    active = json.loads(open(active_path, encoding="utf-8").read())
    sys.stdout.buffer.write(json.dumps({{"cards": [active], "count": 1}}).encode("utf-8"))
    sys.exit(0)
in_path = None
for i, a in enumerate(argv):
    if a.startswith("--file=") or a.startswith("--in="):
        in_path = a.split("=", 1)[1]
    elif a in ("--file", "--in") and i + 1 < len(argv):
        in_path = argv[i + 1]
active = json.loads(open(active_path, encoding="utf-8").read())
expected = ["card/" + active["seat"] + "/" + active["id"]]
cards = [json.loads(line) for line in open(in_path, encoding="utf-8").read().splitlines() if line.strip()]
ctx_cards = [c for c in cards if c.get("type") == "Context"]
for c in ctx_cards:
    if c.get("supersedes") != expected:
        msg = "active Context card " + expected[0] + " must be superseded"
        out = {{"cards": len(cards), "outcomes": {{"refused": 1}}, "written": [],
               "problems": [{{"outcome": "refused", "error_code": "E_CARD_SUPERSEDES", "error_message": msg}}]}}
        sys.stdout.buffer.write(json.dumps(out).encode("utf-8"))
        sys.exit(1)
    open(active_path, "w", encoding="utf-8").write(json.dumps(c))
out = {{"cards": len(cards), "outcomes": {{"written": len(cards)}},
       "written": ["card/" + c["seat"] + "/" + c["id"] for c in cards], "problems": [], "seconds": 0.1}}
sys.stdout.buffer.write(json.dumps(out).encode("utf-8"))
sys.exit(0)
""",
        encoding="utf-8",
    )
    return str(script)


def test_emit_context_seed_and_settle_failure_guards(seat, monkeypatch):
    """Codex inline review on #319 + #326:
    1. If seeding cursor.json fails after cards.jsonl was rewritten, cards.jsonl is restored and the pass stops cleanly.
    2. If a project store exceeds MAX_STORE_FILE_BYTES (read_small raises Unsafe), seeding fails the pass with an
       explicit receipt rather than running the loader against a stale local Context head.
    3. If writing <root>/context-head.json fails after the server accepts a new Context card B, B's project store marks
       B as settled (popping context_prev_head), the pass reports failed, and the next pass heals <root>/context-head.json
       from B's settled store so the next Context card C supersedes B (#326).
    4. Even if no local store has the server's active Context head, a pass that hits E_CARD_SUPERSEDES recovers the
       active Context head from the server and succeeds on retry in the same pass (#326)."""
    a, b = seat.project("projA"), seat.project("projB")
    _transcript(a, "a1.jsonl", "echo pxx-shell-ok", "Paint the A barn red before Friday, please.")
    seat.hook("SessionEnd", "--emit-context", project_dir=a, loader=REAL_LOADER)
    id_a = json.loads((seat.store / lh.CONTEXT_HEAD).read_text(encoding="utf-8"))["id"]

    # Prepare project B with an existing non-Context card row
    b_store = seat.pstore(b)
    b_store.mkdir(parents=True, exist_ok=True)
    orig_cards = json.dumps(card(1), sort_keys=True) + "\n"
    (b_store / "cards.jsonl").write_text(orig_cards, encoding="utf-8")
    orig_cursor = json.dumps({"files": {}, "seat_files": {}, "id_rule": "1.7"}, sort_keys=True, indent=1)
    (b_store / "cursor.json").write_text(orig_cursor, encoding="utf-8")

    # Case 1: _write_text_atomic succeeds for cards.jsonl and fails for cursor.json -> cards.jsonl is restored
    real_write = lh._write_text_atomic
    monkeypatch.setattr(
        lh, "_write_text_atomic",
        lambda path, text: False if os.path.basename(path) == "cursor.json" and "context_head_id" in text else real_write(path, text),
    )
    backup, err = lh._seed_project_context_head(str(seat.store), str(b_store))
    assert backup is None and err == "project store cursor.json could not be seeded; nothing written"
    assert (b_store / "cards.jsonl").read_text(encoding="utf-8") == orig_cards
    monkeypatch.setattr(lh, "_write_text_atomic", real_write)

    # Case 2: project store exceeds MAX_STORE_FILE_BYTES -> pass stops with explicit failed receipt before running loader
    (b_store / "cards.jsonl").write_text(orig_cards * 20, encoding="utf-8")
    monkeypatch.setattr(lh, "MAX_STORE_FILE_BYTES", 2048)
    _transcript(b, "b1.jsonl", "echo pxx-shell-ok", "Move the B hives to the east field tonight.")
    seat.hook("SessionEnd", "--emit-context", project_dir=b, loader=REAL_LOADER)
    assert seat.result(b)["status"] == "failed"
    assert seat.result(b)["detail"] == "project store could not be read for context-head seeding; nothing written"
    monkeypatch.setattr(lh, "MAX_STORE_FILE_BYTES", 16 << 20)
    (b_store / "cards.jsonl").write_text(orig_cards, encoding="utf-8")

    # Case 3: server accepts B's new Context card, but updating <root>/context-head.json fails once.
    # B's project store settles B (context_prev_head popped), and the next pass heals <root>/context-head.json from B
    # so C supersedes B (#326).
    monkeypatch.setattr(
        lh, "_write_text_atomic",
        lambda path, text: False if os.path.basename(path) == lh.CONTEXT_HEAD else real_write(path, text),
    )
    seat.hook("SessionEnd", "--emit-context", project_dir=b, loader=REAL_LOADER)
    assert seat.result(b)["status"] == "failed"
    assert seat.result(b)["detail"].startswith("context-head.json could not be updated (")
    b_cursor = json.loads((b_store / "cursor.json").read_text(encoding="utf-8"))
    id_b = b_cursor["context_head_id"]
    assert id_b != id_a
    assert "context_prev_head" not in b_cursor
    assert json.loads((seat.store / lh.CONTEXT_HEAD).read_text(encoding="utf-8"))["id"] == id_a

    # Restore _write_text_atomic and emit C from project A against a writer that strictly enforces superseding B.
    monkeypatch.setattr(lh, "_write_text_atomic", real_write)
    b_ctx_card = [
        json.loads(line) for line in (b_store / "cards.jsonl").read_text(encoding="utf-8").splitlines()
        if line and json.loads(line).get("type") == "Context"
    ][-1]
    active_file = seat.tmp / "server_active_ctx.json"
    active_file.write_text(json.dumps(b_ctx_card), encoding="utf-8")
    strict_writer = _make_supersedes_writer(seat.tmp, "strict_writer.py", active_file)

    _transcript(a, "a1.jsonl", "echo pxx-shell-ok", "Paint the A barn red before Friday, please.",
                "stack the C timber in the north shed before dusk.")
    seat.hook("SessionEnd", "--emit-context", project_dir=a, loader=REAL_LOADER, writer=strict_writer)
    assert seat.result(a)["status"] == "ok"
    a_ctx_c = [
        json.loads(line) for line in (seat.pstore(a) / "cards.jsonl").read_text(encoding="utf-8").splitlines()
        if line and json.loads(line).get("type") == "Context"
    ][-1]
    id_c = a_ctx_c["id"]
    assert id_c not in (id_a, id_b)
    assert a_ctx_c.get("supersedes") == ["card/pxx/" + id_b]
    assert json.loads((seat.store / lh.CONTEXT_HEAD).read_text(encoding="utf-8"))["id"] == id_c

    # Case 4: server has moved ahead to an out-of-band Context head id_oob that NO local store knows.
    # On E_CARD_SUPERSEDES, one_pass recovers id_oob from the server and succeeds on retry in the same pass (#326).
    oob_card = dict(a_ctx_c, id="d" * 32, supersedes=["card/pxx/" + id_c])
    active_file.write_text(json.dumps(oob_card), encoding="utf-8")
    _transcript(b, "b1.jsonl", "echo pxx-shell-ok", "Move the B hives to the east field tonight.",
                "Check the D well pump before sunrise tomorrow.")
    seat.hook("SessionEnd", "--emit-context", project_dir=b, loader=REAL_LOADER, writer=strict_writer)
    assert seat.result(b)["status"] == "ok"
    b_ctx_d = [
        json.loads(line) for line in (b_store / "cards.jsonl").read_text(encoding="utf-8").splitlines()
        if line and json.loads(line).get("type") == "Context"
    ][-1]
    assert b_ctx_d.get("supersedes") == ["card/pxx/" + ("d" * 32)]
    assert json.loads((seat.store / lh.CONTEXT_HEAD).read_text(encoding="utf-8"))["id"] == b_ctx_d["id"]

    # Case 5: server moves to id_oob2, and the retry loader invocation fails (exit 5).
    # one_pass reports the retry loader exit failure and rolls back the store without leaving the foreign server card.
    oob2_card = dict(b_ctx_d, id="e" * 32, supersedes=["card/pxx/" + b_ctx_d["id"]])
    active_file.write_text(json.dumps(oob2_card), encoding="utf-8")
    _transcript(b, "b1.jsonl", "echo pxx-shell-ok", "Move the B hives to the east field tonight.",
                "Check the D well pump before sunrise tomorrow.", "Lock the E gate after sunset.")
    real_run_stage = lh.run_stage
    loader_calls = []

    def fail_second_loader(cmd, timeout, cwd=None, lock_fd=None):
        if any("transcript_cards.py" in str(part) for part in cmd):
            loader_calls.append(cmd)
            if len(loader_calls) == 2:
                return 5, 0.1, "", "retry loader boom"
        return real_run_stage(cmd, timeout, cwd=cwd, lock_fd=lock_fd)

    monkeypatch.setattr(lh, "run_stage", fail_second_loader)
    seat.hook("SessionEnd", "--emit-context", project_dir=b, loader=REAL_LOADER, writer=strict_writer)
    assert seat.result(b)["status"] == "failed"
    assert seat.result(b)["detail"].startswith("loader exit 5 after ")
    b_ctx_after_fail = [
        json.loads(line) for line in (b_store / "cards.jsonl").read_text(encoding="utf-8").splitlines()
        if line and json.loads(line).get("type") == "Context"
    ]
    assert [c["id"] for c in b_ctx_after_fail] == [b_ctx_d["id"]]
    assert "context_prev_head" not in json.loads((b_store / "cursor.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(lh, "run_stage", real_run_stage)
