"""Accept a new loader version on one seat: run the old and the new transcript_cards.py over the same frozen copy
of the seat's transcripts and print what changed, as counts only.

    python tools/transcript_cards/accept_seat.py --old <loader> --new <loader> --project-dir <folder> --seat <seat>
        [--all-files] [--marker TEXT ...] [--seats LABEL ...] [--own-name NAME ...]
        [--repo <clone>] [--work <folder>] [--keep] [--json]

A loader is a path (a transcript_cards.py file, or the folder that holds one) or a git revision of this clone
(its tools/transcript_cards folder at that revision; --repo names another clone). A path is tried first. The other
.py files of the loader's folder go beside it, because the loader may import a module beside it.

What it does, in this order:
  1. copies the seat's transcripts ONCE into a scratch folder (the top-level *.jsonl of --project-dir, or every
     *.jsonl under --codex-sessions with its subfolders): a live session keeps writing, and both loaders must
     read the same bytes;
  2. runs each loader over that copy into its own new store (--project-dir COPY --seat S --out STORE, plus
     --all-files, --marker, --seats and --own-name as given);
  3. runs second_scan.py and third_scan.py (the ones beside this file, the same for both) over each store;
  4. prints counts: per card type the cards added, removed and changed (a card is its type and id, or without an
     id its source.record_uuid and source.block_index; changed = same card, different source.source_sha256; other_fields_changed = same
     source, another field differs, loader_version not counted); the deltas of report.json (dropped_by_class,
     redaction_drops_by_detector, new_cards, cursor_resets and a few totals); the deltas of both scans' counts;
     and both loader_version values.

It never prints card text, and never passes through what a loader or a scan printed: second_scan shows the start
of a token and the text before it, and a loader's traceback can quote a record. Only exit codes, an exception's
class name when it is one of Python's built-in exceptions (any other name is a fixed label: a traceback quoted
inside a message is read like a real one), and counts parsed from the scans' count lines are shown.

A count is a non-negative integer. When a report.json is absent or not an object, or one of its counters or counter
maps is absent or holds anything but counts, the result is NOT COMPARED, with a fixed label naming the side and the
field; a value that is not a count is shown as "-", never as itself.

The scratch folder holds copies of the operator's transcripts and is deleted at the end, unless --keep.

Exit 0 always, also when the loaders differ or one of them fails (the output says so), except exit 2 when an input
is missing: the transcript folder, a loader, or a scan script.
"""
import argparse
import builtins
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LOADER = "transcript_cards.py"
LOADER_FOLDER = "tools/transcript_cards"
SCANS = ("second_scan.py", "third_scan.py")
REPORT_COUNTS = ("files", "lines_read", "new_cards", "cursor_resets", "cards_total", "redaction_drops_total",
                 "rescreen_drops", "cards_over_server_limit")
COUNTER_MAPS = ("dropped_by_class", "redaction_drops_by_detector")
CARD_COLUMNS = ("old", "new", "added", "removed", "changed", "other_fields_changed")
SECOND_COUNT = re.compile(r"distinct high-entropy mixed tokens not flagged: (\d+)")
THIRD_COUNT = re.compile(r"^(.+): (\d+) value\(s\) in (\d+) card\(s\)$", re.M)
EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?::|$)")
# The exception names a failed run may show: Python's built-in exceptions, and the JSON decoder's. A message can
# quote a transcript that holds a pasted traceback, header and all, so the line a name is read from may be quoted
# text; any name not on this list is shown as UNNAMED_EXCEPTION.
KNOWN_EXCEPTIONS = frozenset(
    [n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, BaseException)]
    + ["json.decoder.JSONDecodeError"])
UNNAMED_EXCEPTION = "an exception (not a built-in name, not shown)"


class MissingInput(Exception):
    pass


def _env():
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")


def _git(repo, *args):
    # No GIT_* variable reaches git. Run from a git hook, GIT_DIR (with GIT_INDEX_FILE and GIT_WORK_TREE) names the
    # hook's repository, and `git -C repo` then reads that one instead of --repo (#206: this file's tests, run by a
    # pre-push hook, wrote their fixture commits into the branch being pushed).
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, stdin=subprocess.DEVNULL, env=env)


def stage_loader(spec, repo, dest):
    """Put the loader that `spec` names into dest as transcript_cards.py, with the other .py files of its folder."""
    os.makedirs(dest)
    if spec.startswith("-"):
        raise MissingInput("a loader is a path or a revision, not an option: " + spec)
    if os.path.isdir(spec) or os.path.isfile(spec):
        src = os.path.join(spec, LOADER) if os.path.isdir(spec) else spec
        if not os.path.isfile(src):
            raise MissingInput("no %s in the folder %s" % (LOADER, spec))
        folder = os.path.dirname(os.path.abspath(src))
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.endswith(".py") and os.path.isfile(path) and os.path.abspath(path) != os.path.abspath(src):
                shutil.copyfile(path, os.path.join(dest, name))
        shutil.copyfile(src, os.path.join(dest, LOADER))          # last, so a sibling of that name never wins
        return
    top = _git(repo, "rev-parse", "--show-toplevel") if repo and os.path.isdir(repo) else None
    if top is None or top.returncode != 0:
        raise MissingInput("%s is not a path, and %s is not a git clone to read a revision from" % (spec, repo))
    root = top.stdout.decode("utf-8", "replace").strip()
    rev = _git(root, "rev-parse", "--verify", "--quiet", spec + "^{commit}")
    if rev.returncode != 0:
        raise MissingInput("%s is neither a path nor a revision of %s" % (spec, root))
    commit = rev.stdout.decode().strip()
    listing = _git(root, "ls-tree", "--full-name", "--name-only", commit, "--", LOADER_FOLDER + "/")
    names = [n for n in listing.stdout.decode("utf-8", "replace").splitlines() if n.endswith(".py")]
    if LOADER_FOLDER + "/" + LOADER not in names:
        raise MissingInput("revision %s has no %s/%s" % (spec, LOADER_FOLDER, LOADER))
    for name in names:
        blob = _git(root, "show", commit + ":" + name)
        if blob.returncode != 0:
            raise MissingInput("cannot read %s at %s" % (name, spec))
        with open(os.path.join(dest, os.path.basename(name)), "wb") as f:
            f.write(blob.stdout)


def freeze(source, dest, codex):
    """Copy the transcripts once. A Claude Code project folder: its top-level *.jsonl, which is all the loader
    opens. A Codex sessions folder: every *.jsonl at any depth, with its subfolders. Returns (files, bytes)."""
    files = size = 0
    os.makedirs(dest)
    walk = os.walk(source) if codex else [(source, [], os.listdir(source))]
    for folder, _, names in sorted(walk):
        for name in sorted(names):
            path = os.path.join(folder, name)
            if not (name.lower().endswith(".jsonl") and os.path.isfile(path)):
                continue
            target = os.path.join(dest, os.path.relpath(path, source))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(path, target)
            files += 1
            size += os.path.getsize(target)
    return files, size


def _error_name(returncode, stderr):
    """What a failed run may show: an exception's class name, or that the loader refused its arguments. Never
    the message, which can quote a transcript record over several lines. The name is read from the first line
    that is not indented after the last traceback header, which is where Python prints it, and is shown only
    when it is in KNOWN_EXCEPTIONS: the message can quote a pasted traceback, and then the last header and the
    line after it are quoted text. Such a quote can change which built-in name is shown, never show its own."""
    if returncode == 0:
        return None
    lines = stderr.decode("utf-8", "replace").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("Traceback (most recent call last):")]
    if starts:
        for line in lines[starts[-1] + 1:]:
            if line and not line[0].isspace():
                m = EXCEPTION_LINE.match(line)
                return m.group(1) if m and m.group(1) in KNOWN_EXCEPTIONS else UNNAMED_EXCEPTION
        return UNNAMED_EXCEPTION
    if returncode == 2 and any(line.startswith("usage:") for line in lines):
        return "usage error (an argument this loader does not take)"
    return None


def run_loader(loader_dir, input_dir, codex, store, a):
    args = [("--codex-sessions" if codex else "--project-dir"), input_dir, "--seat", a.seat, "--out", store]
    if a.all_files:
        args.append("--all-files")
    for flag, values in (("--marker", a.marker), ("--seats", a.seats), ("--own-name", a.own_name)):
        for v in values or ():
            args += [flag, v]
    p = subprocess.run([sys.executable, "-B", os.path.join(loader_dir, LOADER)] + args, capture_output=True,
                       stdin=subprocess.DEVNULL, cwd=loader_dir, env=_env())
    return p.returncode, _error_name(p.returncode, p.stderr)


def card_key(card):
    """(type, identity, place). The identity is the card's own id: the server keys a card by it, and the loader makes
    it from the record or the question call and the block, never from the text (card_identity), so it is exact and
    the same in both stores. A card without one is keyed by its source record and block."""
    kind = str(card.get("type"))
    cid = card.get("id")
    if isinstance(cid, str) and cid:
        return kind, "id:" + cid, None
    src = card.get("source") if isinstance(card.get("source"), dict) else {}
    return kind, "source:" + str(src.get("record_uuid")), src.get("block_index")


def read_store(store):
    """(report.json, or None when it is absent or not JSON; {card key: card}). Nothing here is printed."""
    report, cards = None, {}
    path = os.path.join(store, "report.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
        except ValueError:
            report = None
    path = os.path.join(store, "cards.jsonl")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    card = json.loads(line)
                    cards[card_key(card)] = card
    return report, cards


def _source_sha(card):
    src = card.get("source") if isinstance(card.get("source"), dict) else {}
    return src.get("source_sha256")


def _form(card):
    return {k: v for k, v in card.items() if k != "loader_version"}


def compare_cards(old, new):
    rows = {}
    for key in sorted(set(old) | set(new), key=lambda k: (k[0], k[1], str(k[2]))):
        row = rows.setdefault(key[0], dict.fromkeys(CARD_COLUMNS, 0))
        if key in old:
            row["old"] += 1
        if key in new:
            row["new"] += 1
        if key not in new:
            row["removed"] += 1
        elif key not in old:
            row["added"] += 1
        elif _source_sha(old[key]) != _source_sha(new[key]):
            row["changed"] += 1
        elif _form(old[key]) != _form(new[key]):
            row["other_fields_changed"] += 1
    return rows


def _count(v):
    """v when it is a count (an int, not a bool, not negative), else None: nothing else is ever shown."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _delta(old, new):
    a, b = _count(old), _count(new)
    return {"old": a, "new": b, "delta": None if a is None or b is None else b - a}


def _object(value):
    return value if isinstance(value, dict) else {}


def _counts(old, new, keys):
    return {k: _delta(_object(old).get(k), _object(new).get(k)) for k in keys}


def _table(old, new, field):
    a = _object(_object(old).get(field))
    b = _object(_object(new).get(field))
    return {k: _delta(a.get(k, 0), b.get(k, 0)) for k in sorted(set(a) | set(b))}


def report_problems(side, report):
    """Why a store's report.json cannot be compared, as fixed labels built from the side and the field names
    here, never from what the report holds: absent or not JSON, not an object, a counter of REPORT_COUNTS absent
    or not a count, a map of COUNTER_MAPS absent or not an object, or a map holding a value that is not a count.
    A loader that drops, renames or corrupts a count is not compared, rather than equal by omission."""
    where = side + " report.json"
    if report is None:
        return [where + ": absent or not JSON"]
    if not isinstance(report, dict):
        return [where + ": not a JSON object"]
    problems = [where + ": " + k + " absent or not a count" for k in REPORT_COUNTS if _count(report.get(k)) is None]
    for field in COUNTER_MAPS:
        table = report.get(field)
        if not isinstance(table, dict):
            problems.append(where + ": " + field + " absent or not an object")
        elif any(_count(v) is None for v in table.values()):
            problems.append(where + ": " + field + " holds a value that is not a count")
    return problems


def run_scans(store):
    """Counts from second_scan.py and third_scan.py over the store's cards; their own output is never shown.
    Read as text with universal newlines: on Windows a scan's lines end in CRLF, and THIRD_COUNT anchors at the
    end of a line, so a kept \\r would match no count line and every comparison there would be NOT COMPARED."""
    cards = os.path.join(store, "cards.jsonl")
    if not os.path.isfile(cards):
        return None
    out = {}
    for name in SCANS:
        p = subprocess.run([sys.executable, "-B", os.path.join(HERE, name), cards], capture_output=True,
                           stdin=subprocess.DEVNULL, env=_env(), text=True, encoding="utf-8", errors="replace")
        text = p.stdout
        if name == "second_scan.py":
            m = SECOND_COUNT.search(text) if p.returncode == 0 else None
            out["second"] = {"tokens_not_flagged": int(m.group(1))} if m else None
        else:
            found = {m.group(1): (int(m.group(2)), int(m.group(3))) for m in THIRD_COUNT.finditer(text)}
            out["third"] = found if (p.returncode == 0 and found) else None
    return out


def compare_scans(old, new):
    old, new = old or {}, new or {}
    second = None
    if old.get("second") is not None and new.get("second") is not None:
        second = _counts(old["second"], new["second"], ["tokens_not_flagged"])
    third = None
    if old.get("third") is not None and new.get("third") is not None:
        third = {}
        for cls in list(old["third"]) + [c for c in new["third"] if c not in old["third"]]:
            a, b = old["third"].get(cls, (0, 0)), new["third"].get(cls, (0, 0))
            third[cls] = {"values": _delta(a[0], b[0]), "cards": _delta(a[1], b[1])}
    return {"second_scan": second, "third_scan": third}


def compare(old_store, new_store, old_exit, new_exit, old_error, new_error, old_scans, new_scans):
    old_report, old_cards = read_store(old_store)
    new_report, new_cards = read_store(new_store)
    result = {
        "loader_version": {"old": _object(old_report).get("loader_version"),
                           "new": _object(new_report).get("loader_version")},
        "loader_exit": {"old": old_exit, "new": new_exit},
        "loader_error": {"old": old_error, "new": new_error},
        "cards": compare_cards(old_cards, new_cards),
        "report": _counts(old_report, new_report, REPORT_COUNTS),
        "dropped_by_class": _table(old_report, new_report, "dropped_by_class"),
        "redaction_drops_by_detector": _table(old_report, new_report, "redaction_drops_by_detector"),
        "report_problems": report_problems("old", old_report) + report_problems("new", new_report),
    }
    result.update(compare_scans(old_scans, new_scans))
    cells = [v for row in result["cards"].values() for k, v in row.items() if k not in ("old", "new")]
    deltas = [d["delta"] for group in ("report", "dropped_by_class", "redaction_drops_by_detector")
              for d in result[group].values()]
    deltas += [d["delta"] for d in (result["second_scan"] or {}).values()]
    deltas += [d[k]["delta"] for d in (result["third_scan"] or {}).values() for k in ("values", "cards")]
    result["differences"] = sum(1 for v in cells + deltas if v)
    result["compared"] = (old_exit == 0 and new_exit == 0 and not result["report_problems"]
                          and result["second_scan"] is not None and result["third_scan"] is not None)
    return result


def _num(v):
    if v is None:
        return "-"
    return str(v)


def _signed(v):
    if v is None:
        return "-"
    return ("+%d" % v) if v > 0 else str(v)


def _triple(d):
    return "/".join((_num(d["old"]), _num(d["new"]), _signed(d["delta"])))


def render(result, seat, old_spec, new_spec, frozen):
    lines = ["accept_seat: seat %s" % seat,
             "  old loader: %s  (%s)" % (_num(result["loader_version"]["old"]), old_spec),
             "  new loader: %s  (%s)" % (_num(result["loader_version"]["new"]), new_spec),
             "  input: %d transcript file(s), %d bytes, copied once; both loaders read that copy" % frozen]
    for side in ("old", "new"):
        error = result["loader_error"][side]
        if result["loader_exit"][side] != 0:
            lines.append("  %s loader exit %s%s" % (side, result["loader_exit"][side], (": " + error) if error else ""))
    w = max([len("cards by type")] + [len(t) + 2 for t in result["cards"]])
    lines += ["", "%-*s %6s %6s %6s %8s %8s %13s" % ((w, "cards by type") + CARD_COLUMNS[:5] + ("other_fields",))]
    for typ, row in result["cards"].items():
        lines.append("%-*s %6d %6d %6d %8d %8d %13d" % ((w, "  " + typ) + tuple(row[c] for c in CARD_COLUMNS)))
    if not result["cards"]:
        lines.append("  (no cards in either store)")

    def block(title, rows, indent="  "):
        width = max([len(title)] + [len(indent + k) for k in rows])
        out = ["", "%-*s %6s %6s %6s" % (width, title, "old", "new", "delta")]
        for k, d in rows.items():
            out.append("%-*s %6s %6s %6s" % (width, indent + k, _num(d["old"]), _num(d["new"]), _signed(d["delta"])))
        if not rows:
            out.append(indent + "(none)")
        return out

    lines += block("report.json", result["report"])
    lines += block("dropped_by_class", result["dropped_by_class"])
    lines += block("redaction_drops_by_detector", result["redaction_drops_by_detector"])
    if result["second_scan"] is None:
        lines += ["", "second_scan: not compared (a store has no cards.jsonl, or the scan's count line was not found)"]
    else:
        lines += block("second_scan", {"tokens not flagged": result["second_scan"]["tokens_not_flagged"]})
    if result["third_scan"] is None:
        lines += ["", "third_scan: not compared (a store has no cards.jsonl, or the scan's count lines were not found)"]
    else:
        width = max(len(k) + 2 for k in result["third_scan"])
        lines += ["", "%-*s %13s %13s" % (width, "third_scan", "values", "cards"),
                  "%-*s %13s %13s" % (width, "", "old/new/delta", "old/new/delta")]
        for cls, d in result["third_scan"].items():
            lines.append("%-*s %13s %13s" % (width, "  " + cls, _triple(d["values"]), _triple(d["cards"])))
    lines.append("")
    for problem in result["report_problems"]:
        lines.append("cannot compare: " + problem)
    if not result["compared"]:
        lines.append("result: NOT COMPARED - a loader failed, a scan gave no count, or a report.json count is absent "
                     "or not a count; the counts above are not a comparison")
    elif result["differences"] == 0:
        lines.append("result: no difference between the two loaders on this input")
    else:
        lines.append("result: %d count(s) differ; read every non-zero line above before accepting the new loader"
                     % result["differences"])
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Compare two transcript-loader versions on one seat's transcripts, counts only.")
    ap.add_argument("--old", required=True, help="the loader in use: a path (file or folder) or a git revision")
    ap.add_argument("--new", required=True, help="the loader to accept: a path (file or folder) or a git revision")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--project-dir", help="the seat's Claude Code project folder")
    src.add_argument("--codex-sessions", help="for a Codex seat: the sessions folder of its Codex home")
    ap.add_argument("--seat", required=True, help="the seat's roster label, passed to both loaders")
    ap.add_argument("--all-files", action="store_true", help="passed to both loaders")
    ap.add_argument("--marker", action="append", help="passed to both loaders (repeatable)")
    ap.add_argument("--seats", action="append", help="passed to both loaders (repeatable)")
    ap.add_argument("--own-name", action="append", help="passed to both loaders (repeatable)")
    ap.add_argument("--repo", default=HERE, help="the git clone a revision is read from (default: this file's clone)")
    ap.add_argument("--work", help="the folder to make the scratch folder in (default: the system's temp folder); "
                                   "keep it outside any git tree, it holds transcript copies")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder and print where it is")
    ap.add_argument("--json", action="store_true", help="print the counts as JSON")
    a = ap.parse_args(argv)
    source = a.codex_sessions or a.project_dir
    codex = bool(a.codex_sessions)
    if not os.path.isdir(source):
        sys.stderr.write("accept_seat: no transcript folder at %s\n" % source)
        return 2
    for name in SCANS:
        if not os.path.isfile(os.path.join(HERE, name)):
            sys.stderr.write("accept_seat: %s is missing beside this file\n" % name)
            return 2
    if a.work:
        os.makedirs(a.work, exist_ok=True)
    work = os.path.abspath(tempfile.mkdtemp(prefix="accept_seat-", dir=a.work))
    try:
        try:
            stage_loader(a.old, a.repo, os.path.join(work, "loader-old"))
            stage_loader(a.new, a.repo, os.path.join(work, "loader-new"))
        except MissingInput as e:
            sys.stderr.write("accept_seat: %s\n" % e)
            return 2
        input_dir = os.path.join(work, "input")
        frozen = freeze(source, input_dir, codex)
        runs = {}
        for side in ("old", "new"):
            store = os.path.join(work, "store-" + side)
            code, error = run_loader(os.path.join(work, "loader-" + side), input_dir, codex, store, a)
            runs[side] = (store, code, error, run_scans(store))
        result = compare(runs["old"][0], runs["new"][0], runs["old"][1], runs["new"][1], runs["old"][2],
                         runs["new"][2], runs["old"][3], runs["new"][3])
        result.update({"seat": a.seat, "loader_spec": {"old": a.old, "new": a.new},
                       "input": {"files": frozen[0], "bytes": frozen[1]}, "work": work if a.keep else None})
        if a.json:
            print(json.dumps(result, indent=1, sort_keys=True))
        else:
            print(render(result, a.seat, a.old, a.new, frozen))
            if a.keep:
                print("scratch folder kept (it holds transcript copies; delete it when done): %s" % work)
        return 0
    finally:
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)
            if os.path.exists(work):
                sys.stderr.write("accept_seat: could not remove the scratch folder %s; it holds transcript copies, "
                                 "delete it by hand\n" % work)


if __name__ == "__main__":
    sys.exit(main())
