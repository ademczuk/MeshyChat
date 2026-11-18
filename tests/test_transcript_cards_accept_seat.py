"""Checks for tools/transcript_cards/accept_seat.py: two loader versions over one frozen copy of a seat's
transcripts, compared as counts. Needs no database.

    pytest tests/test_transcript_cards_accept_seat.py -q -p no:cacheprovider --noconftest

Every transcript here is synthetic and written into tmp_path. A changed loader is the real loader with a few lines
put in front of its `if __name__ == "__main__":` block, so the change is known exactly and so are its counts.
ACCEPT_SEAT_PY points the checks at another copy of accept_seat.py (a mutant); the loader and the scans used are the
ones beside that copy. Each MUTANT named below turns these checks red:

    python -B tools/transcript_cards/mutate_accept_seat.py
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import traceback

ACCEPT = os.environ.get("ACCEPT_SEAT_PY") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "transcript_cards", "accept_seat.py")
LOADER = os.path.join(os.path.dirname(ACCEPT), "transcript_cards.py")
MAIN_BLOCK = '\nif __name__ == "__main__":'
SEAT = "txxxx"
# Text that must never reach accept_seat's output. Each sits in a card a store keeps.
CANARIES = ("zebra-otter-canary", "otter-canary-two", "canary-heron", "zebra.canary")
# A 28-character mixed token: too short for the loader's opaque_token, long enough for second_scan, which prints
# its first four characters and the 24 characters before it. Built at run time so this file holds no such string.
TOKEN = "aB3dE5fG7hJ9" + "kL2mN4pQ6rS8tU1v"


def loader_version(path=LOADER):
    with open(path, encoding="utf-8") as f:
        return re.search(r'^LOADER_VERSION = "([^"]+)"', f.read(), re.M).group(1)


def typed(uuid, text, ts="2026-10-08T05:00:00.000Z", session="S1"):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": session, "promptSource": "typed",
            "origin": {"kind": "human"}, "parentUuid": "p-" + uuid, "message": {"role": "user", "content": text}}


def answered(uuid, pairs, ts="2026-10-08T05:15:00.000Z", session="S1"):
    questions = [{"question": q, "header": "h", "multiSelect": False,
                  "options": [{"label": lab, "description": "what " + lab + " does"} for lab in labels]}
                 for q, labels, _ in pairs]
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": session, "sourceToolAssistantUUID": "a-" + uuid,
            "toolUseResult": {"questions": questions, "answers": {q: a for q, _, a in pairs}, "annotations": {}},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t-" + uuid, "content": "ok"}]}}


def write_jsonl(path, records, mode="w"):
    with open(path, mode, encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def seat_transcripts(root):
    """a.jsonl holds the seat's marker: 6 Candidates and 2 Rulings (one call, two questions). b.jsonl has no
    marker and one Candidate, read only with --all-files or --marker legacy-mark-31."""
    project = os.path.join(root, "project")
    os.makedirs(project)
    write_jsonl(os.path.join(project, "a.jsonl"), [
        typed("u-launch", "MANDATORY session-start action: echo %s-shell-ok and resume" % SEAT, "2026-10-08T04:59:00.000Z"),
        typed("u-keep", "keep the deploy gate green before any merge", "2026-10-08T05:01:00.000Z"),
        typed("u-drop", "hold the release until the rehearsal is done", "2026-10-08T05:02:00.000Z"),
        typed("u-change", "use the small model for the search step", "2026-10-08T05:03:00.000Z"),
        typed("u-canary", "zebra-otter-canary: the operator wants plan blue", "2026-10-08T05:04:00.000Z"),
        typed("u-token", "seen otter-canary-two: " + TOKEN, "2026-10-08T05:05:00.000Z"),
        typed("u-email", "send the summary to zebra.canary@example.org", "2026-10-08T05:06:00.000Z"),
        {"type": "assistant", "uuid": "u-asst", "timestamp": "2026-10-08T05:07:00.000Z", "sessionId": "S1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "noted"}]}},
        answered("u-ask", [("Which store holds the canary-heron cards?", ["Postgres", "Files"], "Postgres"),
                           ("Ship it today?", ["Yes", "No"], "Yes")]),
    ])
    write_jsonl(os.path.join(project, "b.jsonl"), [
        typed("u-other", "a prompt from an older session legacy-mark-31", "2026-10-07T05:00:00.000Z", "S0")])
    return project


def changed_loader(root, name, tail, siblings=None):
    """A copy of the real loader with `tail` put in front of its main block, in a folder of its own beside the
    real loader's other modules (and any `siblings` given, name -> source)."""
    folder = os.path.join(root, name)
    os.makedirs(folder)
    for n in os.listdir(os.path.dirname(LOADER)):
        if n.endswith(".py") and n != "transcript_cards.py":
            shutil.copyfile(os.path.join(os.path.dirname(LOADER), n), os.path.join(folder, n))
    for n, text in (siblings or {}).items():
        with open(os.path.join(folder, n), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    with open(LOADER, encoding="utf-8") as f:
        src = f.read()
    assert src.count(MAIN_BLOCK) == 1
    path = os.path.join(folder, "transcript_cards.py")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(src.replace(MAIN_BLOCK, "\n" + tail + "\n" + MAIN_BLOCK))
    return path


# A stricter rule and a changed cut: u-drop and the second question of u-ask are no longer cards, and u-change
# keeps different text under the same id.
RULE_TAIL = '''
LOADER_VERSION = "9.99-accept-test"
_accept_test_cards_from_record = cards_from_record


def cards_from_record(o, seat, *args, **kwargs):
    raws, cls = _accept_test_cards_from_record(o, seat, *args, **kwargs)
    kept = []
    for raw in raws:
        if raw["uuid"] == "u-drop" or (raw["uuid"] == "u-ask" and raw["block_index"] == 1):
            cls = "accept_test_rule"
            continue
        if raw["uuid"] == "u-change":
            raw["kept"] = dict(raw["kept"], text=raw["kept"]["text"] + " (cut again)")
        kept.append(raw)
    return kept, cls
'''

# Only the card text of a Candidate changes; what it was made from does not.
TEXT_TAIL = '''
_accept_test_candidate_text = _candidate_text


def _candidate_text(seat, date, text):
    card_text, truncated = _accept_test_candidate_text(seat, date, text)
    return card_text + " .", truncated
'''

# The seat's session goes on writing: after this loader has read, one more prompt lands in every live transcript.
LATE_WRITE_TAIL = '''
_accept_test_run = run


def run(*args, **kwargs):
    report = _accept_test_run(*args, **kwargs)
    live = os.environ.get("ACCEPT_TEST_LIVE_DIR")
    for name in sorted(os.listdir(live)) if live else []:
        if name.endswith(".jsonl"):
            with open(os.path.join(live, name), "a", encoding="utf-8", newline="\\n") as f:
                f.write(json.dumps({"type": "user", "uuid": "u-late-" + name, "timestamp": "2026-10-08T06:00:00.000Z",
                                    "sessionId": "S1", "promptSource": "typed", "origin": {"kind": "human"},
                                    "message": {"role": "user", "content": "a prompt typed while the loaders ran"}}) + "\\n")
    return report
'''

# A loader that dies on a record, quoting it over several lines, one of which looks like an exception's name.
CRASH_TAIL = '''
_accept_test_cards_from_record = cards_from_record


def cards_from_record(o, seat, *args, **kwargs):
    raws, cls = _accept_test_cards_from_record(o, seat, *args, **kwargs)
    for raw in raws:
        if raw["uuid"] == "u-canary":
            raise RuntimeError("cannot read\\nzebraCanaryError: " + raw["text"])
    return raws, cls
'''


# A loader that dies on one record and quotes the record whole in its message.
QUOTE_TAIL = '''
_accept_test_cards_from_record = cards_from_record


def cards_from_record(o, seat, *args, **kwargs):
    if o.get("uuid") == "u-pasted":
        raise RuntimeError("cannot read the record " + o["message"]["content"])
    return _accept_test_cards_from_record(o, seat, *args, **kwargs)
'''

# A scan run through this wrapper prints the real scan's output with every line ending in CRLF (as on Windows) or LF.
SCAN_WRAPPER = '''import subprocess
import sys
p = subprocess.run([sys.executable, "-B", %r] + sys.argv[1:], capture_output=True, stdin=subprocess.DEVNULL)
out = p.stdout.replace(b"\\r\\n", b"\\n")
sys.stdout.buffer.write(out.replace(b"\\n", b"\\r\\n") if %r else out)
sys.stdout.flush()
sys.exit(p.returncode)
'''


def accept_module():
    """accept_seat.py (ACCEPT, so a mutant when one is under test) imported as a module, writing no bytecode."""
    spec = importlib.util.spec_from_file_location("accept_seat_under_test", ACCEPT)
    module = importlib.util.module_from_spec(spec)
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


def pasted_traceback():
    """A real traceback, as an operator pastes one into a prompt, of an exception whose name and message are
    private. Its module is builtins, so Python prints the bare name, as it does for a built-in exception."""
    private = type("SyntheticPrivateToken12345", (Exception,), {"__module__": "builtins"})
    try:
        raise private("forged-canary-kestrel is the private value")
    except Exception:
        return traceback.format_exc()


def accept(*args, env=None):
    p = subprocess.run([sys.executable, "-B", ACCEPT] + [str(a) for a in args], capture_output=True, text=True,
                       encoding="utf-8", stdin=subprocess.DEVNULL,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", **(env or {})))
    return p.returncode, p.stdout, p.stderr


def accept_json(*args, env=None):
    rc, out, err = accept(*(list(args) + ["--json"]), env=env)
    assert rc == 0, (rc, err[-400:])
    return json.loads(out), out + err


def zeros(result):
    """Every count that says the two loaders differ."""
    cells = [(t, k, v) for t, row in result["cards"].items() for k, v in row.items() if k not in ("old", "new") and v]
    deltas = [(g, k, d["delta"]) for g in ("report", "dropped_by_class", "redaction_drops_by_detector")
              for k, d in result[g].items() if d["delta"]]
    deltas += [("second_scan", k, d["delta"]) for k, d in result["second_scan"].items() if d["delta"]]
    deltas += [("third_scan", c, d[k]["delta"]) for c, d in result["third_scan"].items() for k in ("values", "cards")
               if d[k]["delta"]]
    return cells + deltas


def test_identical_versions_give_all_zero_deltas(tmp_path):
    """The same loader as old and new: every added, removed and changed count is 0, every delta is 0, both runs
    read the same input and made the same cards, and the scans counted something.

    MUTANT: both loaders write into one store (the second run only adds to the first: new_cards 8 then 0).
    MUTANT: the second_scan count is read from its cards-scanned line (8, not 1)."""
    project = seat_transcripts(str(tmp_path))
    result, _ = accept_json("--old", LOADER, "--new", LOADER, "--project-dir", project, "--seat", SEAT)
    assert zeros(result) == [], zeros(result)
    assert result["differences"] == 0 and result["compared"] is True
    assert result["loader_exit"] == {"old": 0, "new": 0}
    assert result["loader_version"] == {"old": loader_version(), "new": loader_version()}
    assert result["cards"]["Candidate"]["old"] == 6 and result["cards"]["Candidate"]["new"] == 6
    assert result["cards"]["Ruling"]["old"] == 2 and result["cards"]["Ruling"]["new"] == 2
    assert result["report"]["new_cards"] == {"old": 8, "new": 8, "delta": 0}
    assert result["report"]["cursor_resets"] == {"old": 0, "new": 0, "delta": 0}
    assert result["report"]["files"] == {"old": 1, "new": 1, "delta": 0}
    assert result["input"] == {"files": 2, "bytes": sum(os.path.getsize(os.path.join(project, n)) for n in os.listdir(project))}
    assert result["second_scan"]["tokens_not_flagged"] == {"old": 1, "new": 1, "delta": 0}
    assert result["third_scan"]["personal: email address"]["values"] == {"old": 1, "new": 1, "delta": 0}
    rc, text, _ = accept("--old", LOADER, "--new", LOADER, "--project-dir", project, "--seat", SEAT)
    assert rc == 0 and "result: no difference between the two loaders on this input" in text


def test_a_changed_loader_rule_shows_as_the_right_counts(tmp_path):
    """A stricter rule (one Candidate and one of two Rulings of a call no longer cards) and a changed cut (one
    Candidate's kept text) are counted per type, in both directions; the drop class, new_cards and both
    loader_version values move with them. A loader that only re-words card text shows as other_fields_changed,
    not as changed.

    MUTANT: added and removed swapped. MUTANT: changed is decided by text_sha256 instead of source.source_sha256 (the re-worded text
    counts as changed). MUTANT: loader_version counted as another field (every card other_fields_changed).
    MUTANT: the dropped_by_class delta is old minus new."""
    project = seat_transcripts(str(tmp_path))
    stricter = changed_loader(str(tmp_path), "stricter", RULE_TAIL)
    result, _ = accept_json("--old", LOADER, "--new", stricter, "--project-dir", project, "--seat", SEAT)
    assert result["cards"]["Candidate"] == {"old": 6, "new": 5, "added": 0, "removed": 1, "changed": 1,
                                            "other_fields_changed": 0}
    assert result["cards"]["Ruling"] == {"old": 2, "new": 1, "added": 0, "removed": 1, "changed": 0,
                                         "other_fields_changed": 0}
    assert result["dropped_by_class"]["accept_test_rule"] == {"old": 0, "new": 2, "delta": 2}
    assert result["report"]["new_cards"] == {"old": 8, "new": 6, "delta": -2}
    assert result["report"]["cards_total"]["delta"] == -2
    assert result["report"]["cursor_resets"]["delta"] == 0
    assert result["loader_version"] == {"old": loader_version(), "new": "9.99-accept-test"}
    assert result["compared"] is True and result["differences"] == 6, result["differences"]

    back, _ = accept_json("--old", stricter, "--new", LOADER, "--project-dir", project, "--seat", SEAT)
    assert back["cards"]["Candidate"] == {"old": 5, "new": 6, "added": 1, "removed": 0, "changed": 1,
                                          "other_fields_changed": 0}
    assert back["cards"]["Ruling"]["added"] == 1 and back["cards"]["Ruling"]["removed"] == 0
    assert back["dropped_by_class"]["accept_test_rule"] == {"old": 2, "new": 0, "delta": -2}

    reworded = changed_loader(str(tmp_path), "reworded", TEXT_TAIL)
    text_only, _ = accept_json("--old", LOADER, "--new", reworded, "--project-dir", project, "--seat", SEAT)
    assert text_only["cards"]["Candidate"] == {"old": 6, "new": 6, "added": 0, "removed": 0, "changed": 0,
                                               "other_fields_changed": 6}
    assert text_only["cards"]["Ruling"]["other_fields_changed"] == 0

    rc, text, _ = accept("--old", LOADER, "--new", stricter, "--project-dir", project, "--seat", SEAT)
    assert rc == 0
    assert re.search(r"^  Candidate +6 +5 +0 +1 +1 +0$", text, re.M), text
    assert re.search(r"^  Ruling +2 +1 +0 +1 +0 +0$", text, re.M), text
    assert re.search(r"^  accept_test_rule +0 +2 +\+2$", text, re.M), text
    assert "result: 6 count(s) differ" in text


def test_card_text_never_appears_in_the_output(tmp_path):
    """Neither the table nor the JSON, nor anything on stderr, holds a word of a card, though second_scan prints
    the text before a token and a loader's traceback quotes its record.

    MUTANT: print a card's text. MUTANT: second_scan's own output passed through. MUTANT: a failed loader's
    stderr passed through. MUTANT: a failed loader's exit makes accept_seat exit non-zero. MUTANT: the
    exception's name taken from the last stderr line shaped like one (a line of the quoted record)."""
    project = seat_transcripts(str(tmp_path))
    stricter = changed_loader(str(tmp_path), "stricter", RULE_TAIL)
    crashing = changed_loader(str(tmp_path), "crashing", CRASH_TAIL)
    seen = []
    for old, new in ((LOADER, stricter), (stricter, LOADER), (LOADER, crashing)):
        for mode in ([], ["--json"]):
            rc, out, err = accept("--old", old, "--new", new, "--project-dir", project, "--seat", SEAT, *mode)
            assert rc == 0, (rc, err[-300:])
            seen.append(out + err)
            for canary in CANARIES + ("zebraCanaryError", TOKEN[:4] + "'", TOKEN):
                assert canary not in out + err, (canary, old, new, mode)
    crashed, _ = accept_json("--old", LOADER, "--new", crashing, "--project-dir", project, "--seat", SEAT)
    assert crashed["loader_exit"] == {"old": 0, "new": 1}
    assert crashed["loader_error"] == {"old": None, "new": "RuntimeError"}
    assert crashed["compared"] is False
    assert "result: NOT COMPARED" in seen[4]


def test_a_traceback_pasted_into_a_quoted_record_shows_no_name_of_its_own(tmp_path):
    """The operator pasted a traceback into a prompt, and a loader dies quoting that record whole. Python prints
    the loader's own traceback, and inside its message the pasted header and exception line, so the last
    traceback header on stderr is the pasted one and the line after it names the pasted exception. Neither that
    name nor its message reaches the output, in the text or the JSON: a name that is not a built-in exception's
    is shown as a fixed label.

    MUTANT: a name not on the list of built-in exceptions is shown."""
    project = seat_transcripts(str(tmp_path))
    pasted = pasted_traceback()
    assert pasted.startswith("Traceback (most recent call last):\n")
    assert "\nSyntheticPrivateToken12345: forged-canary-kestrel" in pasted
    write_jsonl(os.path.join(project, "a.jsonl"),
                [typed("u-pasted", "this broke again:\n" + pasted, "2026-10-08T05:08:00.000Z")], mode="a")
    quoting = changed_loader(str(tmp_path), "quoting", QUOTE_TAIL)
    for mode in ([], ["--json"]):
        rc, out, err = accept("--old", LOADER, "--new", quoting, "--project-dir", project, "--seat", SEAT, *mode)
        assert rc == 0, (rc, err[-300:])
        assert "new loader exit 1" in out or '"new": 1' in out, out[-600:]
        for canary in ("SyntheticPrivateToken12345", "forged-canary-kestrel", "this broke again"):
            assert canary not in out + err, (canary, mode)
    result, _ = accept_json("--old", LOADER, "--new", quoting, "--project-dir", project, "--seat", SEAT)
    assert result["loader_exit"] == {"old": 0, "new": 1}
    assert result["loader_error"] == {"old": None, "new": "an exception (not a built-in name, not shown)"}
    assert result["compared"] is False


def test_scan_counts_are_read_the_same_from_lf_and_crlf_lines(tmp_path):
    """On Windows a scan's lines end in CRLF. The counts read from them are the ones read from LF lines, so a
    comparison there is not NOT COMPARED for a lost count line.

    MUTANT: a scan's output read as bytes and decoded, its CR kept."""
    project = seat_transcripts(str(tmp_path))
    store = str(tmp_path / "store")
    p = subprocess.run([sys.executable, "-B", LOADER, "--project-dir", project, "--seat", SEAT, "--out", store],
                       capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
    assert p.returncode == 0, p.stderr[-400:]
    module = accept_module()
    counts = {}
    for ending, crlf in (("lf", False), ("crlf", True)):
        folder = str(tmp_path / ("scans-" + ending))
        os.makedirs(folder)
        for name in module.SCANS:
            real = os.path.join(os.path.dirname(ACCEPT), name)
            with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(SCAN_WRAPPER % (real, crlf))
            raw = subprocess.run([sys.executable, "-B", os.path.join(folder, name), os.path.join(store, "cards.jsonl")],
                                 capture_output=True, stdin=subprocess.DEVNULL,
                                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")).stdout
            assert raw.count(b"\n") >= 1 and (raw.count(b"\r\n") == raw.count(b"\n")) is crlf, (name, ending, raw[:80])
        module.HERE = folder
        counts[ending] = module.run_scans(store)
    assert counts["lf"]["second"] == {"tokens_not_flagged": 1}
    assert counts["lf"]["third"]["personal: email address"] == (1, 1)
    assert counts["crlf"] == counts["lf"], counts


BASE_REPORT = {"loader_version": "1.16", "files": 1, "lines_read": 9, "new_cards": 8, "cursor_resets": 0,
               "cards_total": 8, "redaction_drops_total": 2, "rescreen_drops": 0, "cards_over_server_limit": 0,
               "dropped_by_class": {"assistant": 1, "launch_prompt": 1}, "redaction_drops_by_detector": {"email": 2}}
BASE_SCANS = {"second": {"tokens_not_flagged": 1}, "third": {"personal: email address": (1, 1)}}


def _without(field):
    def change(report):
        del report[field]
    return change


def _setting(field, value):
    def change(report):
        report[field] = value
    return change


def _in_map(field, key, value):
    def change(report):
        report[field] = dict(report[field], **{key: value})
    return change


# (what is wrong, the side, how the report is changed, the one label it must give)
BAD_REPORTS = [
    ("absent counter", "new", _without("redaction_drops_total"),
     "new report.json: redaction_drops_total absent or not a count"),
    ("absent counter, old side", "old", _without("cards_over_server_limit"),
     "old report.json: cards_over_server_limit absent or not a count"),
    ("numeric string", "new", _setting("cards_total", "8"), "new report.json: cards_total absent or not a count"),
    ("private string", "new", _setting("lines_read", "private-canary-31"),
     "new report.json: lines_read absent or not a count"),
    ("true for 1", "new", _setting("files", True), "new report.json: files absent or not a count"),
    ("false for 0", "new", _setting("cursor_resets", False), "new report.json: cursor_resets absent or not a count"),
    ("negative", "new", _setting("rescreen_drops", -1), "new report.json: rescreen_drops absent or not a count"),
    ("float", "new", _setting("new_cards", 8.0), "new report.json: new_cards absent or not a count"),
    ("null", "new", _setting("cards_over_server_limit", None),
     "new report.json: cards_over_server_limit absent or not a count"),
    ("absent map", "new", _without("dropped_by_class"), "new report.json: dropped_by_class absent or not an object"),
    ("map as a list", "new", _setting("redaction_drops_by_detector", ["email"]),
     "new report.json: redaction_drops_by_detector absent or not an object"),
    ("map value a numeric string", "new", _in_map("dropped_by_class", "assistant", "1"),
     "new report.json: dropped_by_class holds a value that is not a count"),
    ("map value a private string", "new", _in_map("redaction_drops_by_detector", "email", "private-canary-32"),
     "new report.json: redaction_drops_by_detector holds a value that is not a count"),
    ("map value true", "new", _in_map("dropped_by_class", "assistant", True),
     "new report.json: dropped_by_class holds a value that is not a count"),
    ("map value an object", "new", _in_map("dropped_by_class", "launch_prompt", {"n": 1}),
     "new report.json: dropped_by_class holds a value that is not a count"),
    ("map value negative", "old", _in_map("redaction_drops_by_detector", "email", -2),
     "old report.json: redaction_drops_by_detector holds a value that is not a count"),
]


def _source(uuid, block):
    return {"record_uuid": uuid, "block_index": block, "source_sha256": "0" * 64}


def _store(root, name, report, cards=None):
    """A store with report.json (a dict as JSON, a str written as it is, None for none) and the cards given, by
    default one Candidate."""
    store = os.path.join(root, name)
    os.makedirs(store)
    write_jsonl(os.path.join(store, "cards.jsonl"),
                cards if cards is not None else [{"type": "Candidate", "text": "a card", "source": _source("u-1", 0)}])
    if report is not None:
        with open(os.path.join(store, "report.json"), "w", encoding="utf-8", newline="\n") as f:
            f.write(report if isinstance(report, str) else json.dumps(report))
    return store


def _counts_only(result):
    """Every old, new and delta of report.json is an int or None: nothing a report holds is copied as it is."""
    for group in ("report", "dropped_by_class", "redaction_drops_by_detector"):
        for key, d in result[group].items():
            for k in ("old", "new", "delta"):
                v = d[k]
                assert v is None or (type(v) is int), (group, key, k, v)


def test_a_report_count_absent_or_not_a_count_is_not_compared(tmp_path):
    """Two otherwise identical report.json files, one with a counter or counter map absent, renamed or corrupted:
    the result is NOT COMPARED with one fixed label for the side and field, never 'no difference'. A value that
    is not a count is shown as None ('-'), never as itself. A report.json that is not an object, not JSON or
    absent is NOT COMPARED too.

    MUTANT: a report problem does not stop the comparison. MUTANT: an absent counter passes.
    MUTANT: a bool is taken for a count. MUTANT: a negative value is taken for a count.
    MUTANT: a counter map's values are not checked. MUTANT: a value that is not a count is copied into the output."""
    module = accept_module()
    root = str(tmp_path)

    def run(name, old_report, new_report):
        old = _store(root, name + "-old", old_report)
        new = _store(root, name + "-new", new_report)
        result = module.compare(old, new, 0, 0, None, None, BASE_SCANS, BASE_SCANS)
        text = module.render(result, SEAT, "old", "new", (1, 1))
        _counts_only(result)
        for canary in ("private-canary-31", "private-canary-32"):
            assert canary not in json.dumps(result) + text, (name, canary)
        return result, text

    same, text = run("control", BASE_REPORT, BASE_REPORT)
    assert same["compared"] is True and same["differences"] == 0 and same["report_problems"] == []
    assert text.splitlines()[-1] == "result: no difference between the two loaders on this input"
    for i, (what, side, change, label) in enumerate(BAD_REPORTS):
        bad = json.loads(json.dumps(BASE_REPORT))
        change(bad)
        result, text = run("case%d" % i, *((bad, BASE_REPORT) if side == "old" else (BASE_REPORT, bad)))
        assert result["report_problems"] == [label], (what, result["report_problems"])
        assert result["compared"] is False, what
        assert text.splitlines()[-1].startswith("result: NOT COMPARED"), (what, text.splitlines()[-1])
        assert "cannot compare: " + label in text.splitlines(), what
    for what, report, label in (("not an object", "[1, 2]", "new report.json: not a JSON object"),
                                ("not JSON", "{not json", "new report.json: absent or not JSON"),
                                ("absent", None, "new report.json: absent or not JSON")):
        result, text = run(what.replace(" ", "-"), BASE_REPORT, report)
        assert result["report_problems"] == [label], (what, result["report_problems"])
        assert result["compared"] is False and text.splitlines()[-1].startswith("result: NOT COMPARED"), what


def test_both_loaders_read_one_frozen_copy(tmp_path):
    """The seat's session writes on while the loaders run: the old loader adds a prompt to every live transcript
    after it has read. Both loaders read the copy taken before either ran, so nothing differs, and the live
    folder is only ever read.

    MUTANT: each loader reads the live folder. MUTANT: the copy is taken again for the new loader."""
    project = seat_transcripts(str(tmp_path))
    late = changed_loader(str(tmp_path), "late", LATE_WRITE_TAIL)
    before = {n: os.path.getsize(os.path.join(project, n)) for n in os.listdir(project)}
    result, _ = accept_json("--old", late, "--new", LOADER, "--project-dir", project, "--seat", SEAT,
                            env={"ACCEPT_TEST_LIVE_DIR": project})
    assert all(os.path.getsize(os.path.join(project, n)) > before[n] for n in before), "the late write did not happen"
    assert zeros(result) == [], zeros(result)
    assert result["cards"]["Candidate"]["new"] == 6


def test_all_files_and_marker_are_passed_to_both_loaders(tmp_path):
    """b.jsonl holds no seat marker. With --all-files, or --marker naming its text, both loaders read it.

    MUTANT: --all-files not passed. MUTANT: --marker not passed."""
    project = seat_transcripts(str(tmp_path))
    plain, _ = accept_json("--old", LOADER, "--new", LOADER, "--project-dir", project, "--seat", SEAT)
    assert plain["report"]["files"] == {"old": 1, "new": 1, "delta": 0}
    for extra in (["--all-files"], ["--marker", "legacy-mark-31"]):
        wide, _ = accept_json("--old", LOADER, "--new", LOADER, "--project-dir", project, "--seat", SEAT, *extra)
        assert wide["report"]["files"] == {"old": 2, "new": 2, "delta": 0}, extra
        assert wide["cards"]["Candidate"]["old"] == 7 and wide["cards"]["Candidate"]["new"] == 7, extra
        assert zeros(wide) == []


def _git(repo, *args):
    # No GIT_* variable: run from a git hook, GIT_DIR names the hook's repository and `git -C repo` writes there
    # (#206, where a pre-push hook's run committed this file's fixture onto the branch being pushed).
    p = subprocess.run(["git", "-C", repo, "-c", "user.name=accept-test", "-c", "user.email=accept-test@example.invalid",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull] + list(args),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    assert p.returncode == 0, (args, p.stderr)
    return p.stdout


def test_two_git_revisions_of_the_loader(tmp_path):
    """--old and --new can be revisions of a clone (--repo). The newer revision's rule lives in a module beside
    the loader, so the run proves the revision's other modules are staged with it; the same holds for a loader
    named by path. A revision that does not exist is a missing input.

    MUTANT: only transcript_cards.py staged from a revision (the new loader cannot import its module).
    MUTANT: a path's other modules not copied beside it."""
    project, repo, changed = _loader_repo(tmp_path)
    result, _ = accept_json("--old", "HEAD~1", "--new", "HEAD", "--repo", repo, "--project-dir", project, "--seat", SEAT)
    assert result["loader_exit"] == {"old": 0, "new": 0}, result["loader_error"]
    assert result["cards"]["Candidate"] == {"old": 6, "new": 5, "added": 0, "removed": 1, "changed": 1,
                                            "other_fields_changed": 0}
    assert result["loader_version"] == {"old": loader_version(), "new": "9.99-accept-test"}
    by_path, _ = accept_json("--old", LOADER, "--new", changed, "--project-dir", project, "--seat", SEAT)
    assert by_path["loader_exit"] == {"old": 0, "new": 0}, by_path["loader_error"]
    assert by_path["cards"] == result["cards"]
    rc, _, err = accept("--old", "no-such-revision", "--new", "HEAD", "--repo", repo, "--project-dir", project, "--seat", SEAT)
    assert rc == 2 and "no-such-revision" in err


def _loader_repo(tmp_path):
    """(project, repo, changed): a clone whose HEAD~1 is the real loader and HEAD the stricter one, its rule in a
    module beside it; changed is that newer loader's path outside the clone. HEAD~1 holds every module of the real
    loader's folder, not the loader alone: since #188 the loader imports kimi_dialect from its own folder, so a
    clone with the loader alone could not start either revision (txxxx, on #206 merged onto b6eb3959)."""
    project = seat_transcripts(str(tmp_path))
    repo = str(tmp_path / "repo")
    folder = os.path.join(repo, "tools", "transcript_cards")
    os.makedirs(folder)
    _git(repo, "init", "-q")
    for name in sorted(os.listdir(os.path.dirname(LOADER))):
        if name.endswith(".py"):
            shutil.copyfile(os.path.join(os.path.dirname(LOADER), name), os.path.join(folder, name))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", "old")
    sibling = "RULE = %r\n\n\ndef apply(namespace):\n    exec(RULE, namespace)\n" % RULE_TAIL
    changed = changed_loader(str(tmp_path), "staged", "import accept_test_rule\naccept_test_rule.apply(globals())\n",
                             siblings={"accept_test_rule.py": sibling})
    for name in ("transcript_cards.py", "accept_test_rule.py"):
        shutil.copyfile(os.path.join(os.path.dirname(changed), name), os.path.join(folder, name))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", "new")
    return project, repo, changed


def test_a_git_hooks_variables_reach_neither_git_here_nor_accept_seat(tmp_path, monkeypatch):
    """A git hook runs with GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE naming its own repository, and `git -C <dir>`
    then reads and writes THAT repository: on 10 Oct this file, run by a pre-push hook, committed its fixture onto
    the branch being pushed (#206). Here the three name a decoy with one commit: the fixture's commits land in the
    fixture, accept_seat reads --repo's two revisions, and the decoy is untouched.

    MUTANT: git is run with the caller's GIT_* variables (accept_seat looks for HEAD~1 in the decoy, which has no
    parent commit, and exits 2). The decoy's unchanged head and count show this file's own _git drops them too."""
    decoy = str(tmp_path / "decoy")
    os.makedirs(decoy)
    _git(decoy, "init", "-q")
    with open(os.path.join(decoy, "decoy.txt"), "w", encoding="utf-8") as f:
        f.write("decoy\n")
    _git(decoy, "add", "-A")
    _git(decoy, "commit", "-q", "--no-verify", "-m", "decoy")
    head = _git(decoy, "rev-parse", "HEAD").strip()
    monkeypatch.setenv("GIT_DIR", os.path.join(decoy, ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", decoy)
    monkeypatch.setenv("GIT_INDEX_FILE", os.path.join(decoy, ".git", "index"))
    project, repo, _ = _loader_repo(tmp_path)
    result, _ = accept_json("--old", "HEAD~1", "--new", "HEAD", "--repo", repo, "--project-dir", project, "--seat", SEAT)
    assert result["loader_exit"] == {"old": 0, "new": 0}, result["loader_error"]
    assert result["loader_version"] == {"old": loader_version(), "new": "9.99-accept-test"}
    monkeypatch.undo()
    assert _git(decoy, "rev-parse", "HEAD").strip() == head
    assert _git(decoy, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "2"


def test_a_card_is_matched_by_its_id_and_without_one_by_record_and_block(tmp_path):
    """The two stores' cards are matched by each card's own id, the server's key. Two Rulings with one (type,
    record, block) but two ids, as one call id met in two sessions would give, stay two cards; cards without an id
    are matched by record AND block.

    MUTANT: a card is keyed by its record and block even when it has an id (the two Rulings become one).
    MUTANT: a card without an id is keyed without its block index (its record's two Candidates become one)."""
    module = accept_module()
    root = str(tmp_path)
    twins = [{"id": "a" * 32, "type": "Ruling", "text": "one", "source": _source("call-1", 0)},
             {"id": "b" * 32, "type": "Ruling", "text": "two", "source": _source("call-1", 0)}]
    plain = [{"type": "Candidate", "text": "x", "source": _source("u-1", 0)},
             {"type": "Candidate", "text": "y", "source": _source("u-1", 1)}]
    old = _store(root, "old", BASE_REPORT, twins + plain)
    new = _store(root, "new", BASE_REPORT, twins[:1] + plain[:1])
    cards = module.compare(old, new, 0, 0, None, None, BASE_SCANS, BASE_SCANS)["cards"]
    assert cards["Ruling"] == {"old": 2, "new": 1, "added": 0, "removed": 1, "changed": 0, "other_fields_changed": 0}
    assert cards["Candidate"] == {"old": 2, "new": 1, "added": 0, "removed": 1, "changed": 0,
                                  "other_fields_changed": 0}


def test_a_missing_input_exits_2_and_the_scratch_folder_goes(tmp_path):
    """A missing transcript folder or loader exits 2. Otherwise exit 0, and the scratch folder (which holds the
    transcript copies) is gone at the end unless --keep, which names it.

    MUTANT: a missing input exits 0. MUTANT: the scratch folder is kept without --keep."""
    project = seat_transcripts(str(tmp_path))
    work = str(tmp_path / "work")
    rc, _, err = accept("--old", LOADER, "--new", LOADER, "--project-dir", str(tmp_path / "absent"), "--seat", SEAT)
    assert rc == 2 and "no transcript folder" in err
    rc, _, err = accept("--old", str(tmp_path / "absent.py"), "--new", LOADER, "--project-dir", project,
                        "--seat", SEAT, "--repo", str(tmp_path), "--work", work)
    assert rc == 2
    os.makedirs(str(tmp_path / "empty-folder"))
    rc, _, err = accept("--old", LOADER, "--new", str(tmp_path / "empty-folder"), "--project-dir", project,
                        "--seat", SEAT, "--work", work)
    assert rc == 2 and "no transcript_cards.py" in err
    assert os.listdir(work) == []
    rc, _, _ = accept("--old", LOADER, "--new", LOADER, "--project-dir", project, "--seat", SEAT, "--work", work)
    assert rc == 0 and os.listdir(work) == []
    kept, _ = accept_json("--old", LOADER, "--new", LOADER, "--project-dir", project, "--seat", SEAT, "--work", work,
                          "--keep")
    assert os.listdir(work) == [os.path.basename(kept["work"])]
    assert sorted(os.listdir(os.path.join(kept["work"], "input"))) == ["a.jsonl", "b.jsonl"]


def test_a_codex_seat_is_copied_with_its_subfolders(tmp_path):
    """--codex-sessions: rollout files sit in dated subfolders, and the copy keeps them, so both loaders read the
    same sessions.

    MUTANT: a Codex sessions folder is copied top level only (no cards in either store)."""
    day = tmp_path / "sessions" / "2026" / "10" / "08"
    os.makedirs(str(day))
    write_jsonl(str(day / "rollout-2026-10-08T05-00-00-aaaa.jsonl"), [
        {"timestamp": "2026-10-08T05:00:00.000Z", "type": "session_meta",
         "payload": {"id": "S-cx", "source": "cli", "originator": "codex-tui"}},
        {"timestamp": "2026-10-08T05:20:00.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "id": "msg_a1",
                     "content": [{"type": "input_text", "text": "keep the zebra-otter-canary gate"}],
                     "internal_chat_message_metadata_passthrough": {"turn_id": "t1", "content_item_kinds": ["user.text"]}}},
    ])
    result, seen = accept_json("--old", LOADER, "--new", LOADER, "--codex-sessions", str(tmp_path / "sessions"),
                               "--seat", "codex-bxxxxx-cli")
    assert result["cards"]["Candidate"]["old"] == 1 and result["cards"]["Candidate"]["new"] == 1
    assert zeros(result) == [] and result["input"]["files"] == 1
    assert "zebra-otter-canary" not in seen
