"""load_hook.py - a seat's card load, started from its Claude Code hooks, and the one door for a load by hand.

Why (exxxx-session's gap register, gap 1, 10 Oct 2026): the loader and `card write` were run by hand, so a seat's
cards on the server lagged its sessions by 32 to 48 hours, and one ruling reached the server 32 hours late. This
script is what a seat's hooks call instead, so nobody has to remember the load.

    hook:    <python> <client>/tools/transcript_cards/load_hook.py --event <hook event> [--project-dir <dir>]
    by hand: the same, with --run in place of --event, and --project-dir given; --rebuild and --all-files go to the
             loader for that run only.

THE SEAT IS THE PANE'S IDENTITY, MESHY_IDENTITY, whose labels the postbox channel and meshy-mod take in full (the
mod then acts for the enabled ones). One Claude Code profile can serve several seats (HQB), so the command names
none. A pane with no identity is no seat's: the hook does nothing at all. The server and the state file are those of
the first label, in order, whose entry in the postbox's config.json exists and is enabled (on HQB, <seat>-prod: the
bare <seat> is an old test-server identity, turned off). The seat is that entry's "seat", else the FIRST label (the
bare roster label existing card keys use); the store is <MESHY_CARDS_ROOT or ~/.meshychat/cards>/<seat>; the
loader's markers are that entry's "markers". --seat, --store, --base-url, --state-file and --marker, given, win.

THE HOOK ITSELF reads the hook's JSON from stdin (at most 1 MB, at most 2 s), starts a detached load worker, and
for PreCompact and SessionEnd prints one line. At SessionStart one daemon briefing worker resolves a copy of the
arguments and runs `card recent`, under one 9 s deadline including start, read, wait and cleanup. Main alone
prints its result or one timeout line; a late worker never prints and os._exit(0) ends it. On POSIX the child has
its own session and its whole process group is killed at timeout or the cap; Windows uses a new process group
and p.kill(). Waits are bounded by the remaining deadline with a 0.2 s cleanup floor. Child failure lines are
reduced to their leading type and a suggestion to run `card recent` by hand. Decoding replaces invalid UTF-8;
the cap is 64 KiB of PRINTED UTF-8 bytes, cut at a character boundary, and binary stdout is used when available.
The load worker keeps the original arguments. --no-brief starts only the load; --brief-only prints only the
briefing; both together do nothing. These flags affect hook-mode SessionStart only. Give its settings line a
15 s hook timeout: if the stdout reader stops, a blocked write cannot be interrupted from inside, and Claude
Code ends the hook. The prompt hook itself never touches the store, so a stalled store disk cannot hold a prompt.

THE WORKER (`--run --event E`) does the rest:
  - its request goes into <store>/pending/, one file per project folder, so a request is never lost to another
    project's load. A request names its project folder and nothing else: the seat, the server, the state file,
    the markers and the programs are always the worker's own arguments, never a file's;
  - under the store's lock it takes the requests one by one, oldest first, and loads each project folder into its
    own loader store, <store>/projects/<key>/: two stages, each stopped after --stage-timeout seconds (120):
      1. transcript_cards.py --project-dir <dir> --seat <seat> --out <store>/projects/<key>   (incremental)
      2. python -m tools.meshy_node card write --file <store>/projects/<key>/cards.jsonl --base-url ... --state-file ...
         (a key already on the server is left as it is, so writing the whole file again is idempotent)
    One loader store a project folder, because the loader keys its cursor, and its record of which transcripts
    hold this seat's marker, by file name, and its --rebuild clears its whole store: in one shared store a file of
    another folder with the same name would pass as the seat's, and a rebuild of one folder would drop the others.
  - at most MAX_PASSES passes a run. What it leaves queued (its passes spent, or held up) it hands, after the
    release, to one successor it starts as a hook starts a worker; a successor with nothing to take ends at once,
    and one that loads nothing starts none;
  - a request names the session's transcript file too, given to the loader as --seat-file when the loader has
    that flag (a continuation after a compaction holds no seat marker), found by a scan of the loader's source:
    declared action="append", every name of a pass goes to one loader stage; otherwise one loader stage a name.
    Each (folder, transcript) is its own queue file, so no name is lost to another, and a pass takes one folder's
    requests together (up to MAX_GROUP).
A hook's worker never passes --rebuild or --all-files. A LOAD BY HAND (`--run` with no --event) is the same pass
under the same lock: it waits for the lock (--wait, 900 s), runs its own pass with those two flags if given, and
then takes what is queued. Running transcript_cards.py directly on a store the hooks load is NOT safe: the two
runs' writes of cards, drops and cursor interleave. A pass that finds a loader's temporary file written in the last
two minutes beside the store's files does not start (that catches a direct run only while it writes, at its end).

THE LOCK is an operating-system lock on <store>/load.lock (flock on POSIX, msvcrt's byte lock on Windows, the
pattern of tools/postbox/postbox_os.try_lock), held for the worker's whole run. The file is never deleted. A process
that dies holds no lock, so there is no stale lock to judge. A stage must not outlive its worker's lock: on POSIX
each stage inherits the locked descriptor, so the lock stays held until the stage ends too; on Windows each stage
is put in a kill-on-close job object the worker holds (the pattern of tools/postbox/postbox.tie_to_daemon), so the
worker's death ends its stages. The Windows path is unrun until HQB runs its test.

THE STORE is this user's alone. On POSIX the worker makes the store, pending/, results/, projects/ and each
project's loader store with mode 0700 and refuses them (no pass, a failed receipt where the store itself is sound)
when one is a link, another user's, or writable by group or others; a store that holds a loader's files at its root
(one loader store for every project, the layout before projects/) is refused too; a queue entry or receipt must be a regular file of this user's, is opened without
following links or waiting on a pipe, and is at most 16 KiB. On Windows the store is trusted to the ACL of the
user's profile it lives in. A queue entry that is not a well-formed request is renamed <name>.bad and never read
again, and each request's pass, receipt included, is one unit: a failure in it costs that request only.

RECEIPTS. Each pass writes <store>/results/<key>.json for its project, atomically:
  {"time": "YYYY-MM-DDTHH:MM:SSZ", "status": ..., "detail": ..., "seat": ..., "project": <folder or null>}
  ok           the cards were loaded and every one written or already there
  held         `card write` exit 3: a redaction detector held a card
  unreachable  `card write` exit 2 saying it could not reach the server
  failed       the loader failed, `card write` exit 1, or exit 2 because the client refused before sending; or the
               store was refused, another loader was writing it, or a stage could not be started
  timeout      a stage ran past its time limit and was stopped
  skipped      there was nothing to read: no project folder
The detail is counts, seconds and exit codes; it never holds a card's text. <store>/last-result.json, the receipt
`card recent --load-result` reads, is the seat's summary: the newest project receipt of the last 24 hours, unless
one of those did not finish cleanly (neither ok nor skipped); then the newest such one, so one project's good load
does not hide another's failed one. A folder that holds none of the seat's transcripts (loader exit 3, NO_MARKER:
a session started from the seat's shell elsewhere) is not its project and counts only when no other receipt is
there (#238 B3).

The events, decided by the worker:
  UserPromptSubmit  no load of its own while its project's receipt says ok and is younger than 900 s, or any
                    receipt of its project is younger than 300 s (a failing server is not asked again at every
                    prompt), or a load is running. Requests other hooks left are still taken.
  PreCompact,       a load whatever the receipts say (these end a stretch of transcript, and the cards since the
  SessionEnd        last load are the ones that came late); while a load is running, the request waits in pending/
                    and that load takes it before it ends.
  SessionStart      a load, unless a load is running; the foreground then prints the decision-cards briefing
                    (--no-brief suppresses it; --brief-only suppresses the load).
PreCompact and SessionEnd print one line; SessionStart prints the bounded, sanitized briefing. UserPromptSubmit prints
nothing. Every path exits 0: a hook must never block or fail a session.

Standard library only. The stages run with the same Python as this script.
"""
import argparse
import copy
import datetime
import hashlib
import itertools
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))          # the client checkout: `python -m tools.meshy_node` runs here
SELF = os.path.abspath(__file__)

try:
    from .transcript_cards import (
        _foreign_by_precedence,
        _is_summary_record,
        _marker_names_in_record,
        PolicyUnreadable,
        exclusion_policy,
        _card_matches_exclusion,
        exclusion_specs,
        _card_producer_family,
        _card_strings,
        _matches_file_spec,
        _pending_spool_path,
        detect,
        write_pending_spool,
    )
except ImportError:
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from transcript_cards import (  # noqa: E402
        _foreign_by_precedence,
        _is_summary_record,
        _marker_names_in_record,
        PolicyUnreadable,
        exclusion_policy,
        _card_matches_exclusion,
        exclusion_specs,
        _card_producer_family,
        _card_strings,
        _matches_file_spec,
        _pending_spool_path,
        detect,
        write_pending_spool,
    )

RECEIPT = "last-result.json"
CONTEXT_HEAD = "context-head.json"
RESULTS = "results"
PENDING = "pending"
PROJECTS = "projects"     # one loader store a project folder: <root>/projects/<key>/
CLAIMED = ".claimed"
BAD = ".bad"
LOCK = "load.lock"
CARDS = "cards.jsonl"
LOADER_FILES = ("cards.jsonl", "drops.jsonl", "cursor.json", "report.json")   # the loader writes each via <name>.tmp

OK_FRESH_S = 900          # a prompt loads nothing for its project while an ok receipt is younger than this
ANY_FRESH_S = 300         # ... nor while any receipt of its project is younger than this
SUMMARY_WINDOW_S = 24 * 3600
STAGE_TIMEOUT_S = 120.0   # a hook's worker
MANUAL_STAGE_TIMEOUT_S = 1800.0   # a load by hand, which may be a full read (--rebuild)
LOCK_WAIT_S = 900.0       # how long a load by hand waits for the lock
TMP_FRESH_S = 120         # a loader's temporary file younger than this: another loader is writing the store
MAX_PASSES = 3            # passes a worker runs before it hands the rest of pending/ to a successor
SUCCESSOR_WAIT_S = TMP_FRESH_S    # how long a successor waits first when its worker stopped on a stall
MAX_FILE_BYTES = 16 * 1024        # a request or a receipt is a few hundred bytes
MAX_STORE_FILE_BYTES = 16 << 20   # a project store's cards.jsonl or cursor.json
MAX_SCAN = 256            # queue entries, or receipts, looked at in one scan
MAX_GROUP = 8             # queued requests of one project folder taken together in one pass
MAX_PATH_CHARS = 4096
MAX_TIME = 10 ** 11       # a request's time, in seconds since 1970, is below this (the year 5138)
STDIN_LIMIT = 1 << 20
STDIN_WAIT_S = 2.0
#: The deadline the hook gives `card recent` (--deadline): its 2 s default timed out on HQB, a Windows seat (txxxx
#: 819b6e/8e1f22, 12 Oct, a loaded HQB: the interpreter's start and imports alone 1 to 2.8 s, whole runs 2.6 to 6.3 s).
#: card recent's deadline starts after its own interpreter start and imports, so the reads alone took about 1.8 s
#: there (memory-bxxxxx). 5 s for card recent, inside a BRIEF_TIMEOUT_S of that plus 4 s, which covers the
#: child's start and imports (up to 2.8 s on HQB); the reads do not change (kxxx, f8543a: the server-load
#: bound is about reads, not wait time).
BRIEF_RECENT_DEADLINE_S = 5.0
BRIEF_TIMEOUT_S = 9.0     # one deadline for resolution and the entire card recent lifecycle
BRIEF_MAX_BYTES = 64 * 1024
BRIEF_CLEANUP_FLOOR_S = 0.2

MAX_CONFIG_BYTES = 1 << 20      # the postbox's config.json
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")   # a label that is safe as a folder name
TRANSCRIPT_RE = re.compile(r"^[^\\/:\x00]{1,250}\.jsonl$")      # a plain file name, no folder in it
SEAT_FILE_FLAG_RE = re.compile(r"""add_argument\(\s*["']--seat-file["']""")
EMIT_CONTEXT_FLAG_RE = re.compile(r"""add_argument\(\s*["']--emit-context["']""")
_SHELL_OK_MARKER_RE = re.compile(r"\becho\s+([A-Za-z0-9][A-Za-z0-9_-]*)-shell-ok\b", re.IGNORECASE)

STATUSES = ("ok", "held", "unreachable", "failed", "timeout", "skipped")
CLEAN = ("ok", "skipped")
#: The detail of a pass whose project holds none of this seat's transcripts (loader exit 3). A session started from
#: the seat's shell in another folder writes one: that folder is not the seat's project (vxxxxxxxxxx, #238).
NO_MARKER = "loader exit 3: no transcript holds this seat's marker (--marker); nothing written"
EVENTS = ("UserPromptSubmit", "PreCompact", "SessionEnd", "SessionStart")
THROTTLED = ("UserPromptSubmit",)
NO_QUEUE = ("UserPromptSubmit", "SessionStart")     # these load only when they get the lock; they never wait
SPEAKS = ("PreCompact", "SessionEnd")               # the events that print a card-load line (not the briefing)
# The outcome names `card write` counts (tools/meshy_node/cards.py), in the order the detail gives them. Nothing
# else of its answer reaches the detail.
OUTCOMES = ("written", "exists", "skipped", "screened", "refused", "unread", "foreign", "closed", "rate_limited",
            "unanswered")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
WINDOWS = os.name == "nt"


class Unsafe(Exception):
    """A path that is not what the store may hold: a link, a pipe, another user's, or too large."""


class JobError(OSError):
    """Windows: a stage could not be tied to its worker's job object, so it was not run."""


# ---- files

def stamp(t):
    """An instant as the receipt spells it: UTC, to the second, with Z."""
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(raw):
    """A receipt's time as epoch seconds, or None when it is not an ISO instant with an offset."""
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.timestamp() if at.tzinfo is not None else None


def _uid():
    """This user's id on POSIX; None on Windows, where the store is trusted to the profile's ACL."""
    return None if WINDOWS else os.getuid()


def _mine(st):
    return WINDOWS or st.st_uid == _uid()


def _open_regular(path, flags=os.O_RDONLY):
    """(descriptor, stat) of `path` when it is a regular file of this user's. Never follows a link and never waits
    on a pipe (O_NOFOLLOW | O_NONBLOCK, then fstat). FileNotFoundError when there is none; Unsafe for the rest."""
    if WINDOWS:                                  # Windows has no O_NOFOLLOW: a link is refused before the open
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if not flags & os.O_CREAT:
                raise
        else:
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise Unsafe("not a regular file")
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_BINARY", 0), 0o600)
    except FileNotFoundError:
        raise
    except OSError as exc:                       # ELOOP: a link; anything else the open refused
        raise Unsafe(type(exc).__name__) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unsafe("not a regular file")
        if not _mine(st):
            raise Unsafe("another user's file")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def read_small(path, limit=MAX_FILE_BYTES):
    """The bytes of a small regular file of this user's (see _open_regular); Unsafe when it is over `limit`."""
    fd, st = _open_regular(path)
    try:
        if st.st_size > limit:
            raise Unsafe("over %d bytes" % limit)
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            raise Unsafe("over %d bytes" % limit)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_json(path):
    """A small JSON object of this user's, or None: missing, not a regular file, too large, or not an object."""
    try:
        obj = json.loads(read_small(path).decode("utf-8"))
    except (OSError, Unsafe, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def write_json(path, obj):
    """Write through a temporary file in the same folder (mode 0600) and one replace, so a reader sees the old file
    or the new one and never half of one. False when it cannot be written."""
    folder = os.path.dirname(path)
    try:
        text = json.dumps(obj, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=folder)
    except (OSError, ValueError, TypeError):
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        for attempt in range(3):        # Windows refuses the replace while a reader holds the file open
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1)
    except (OSError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return False


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# ---- the identity

def pane_labels(raw):
    """The labels of MESHY_IDENTITY, in order ("txxxx,txxxx-prod" is [txxxx, txxxx-prod]), each a plain label (it
    may become a folder name; one that is not is left out), or [] when the first is unset, empty or not plain. The
    postbox channel takes every label too (meshy_channel.load_identities), and so does meshy-mod (loadIdents), which
    then acts for the enabled ones only."""
    labels = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    plain = [label for label in labels if IDENTITY_RE.match(label) and label not in (".", "..")]
    return plain if labels and plain and plain[0] == labels[0] else []


def pane_identity(raw):
    """The first label of MESHY_IDENTITY, or None (see pane_labels)."""
    labels = pane_labels(raw)
    return labels[0] if labels else None


def cards_root():
    """Where each identity's store lives unless --store says otherwise: MESHY_CARDS_ROOT, else ~/.meshychat/cards
    (inside the user's profile on every platform)."""
    return os.environ.get("MESHY_CARDS_ROOT") or os.path.join(os.path.expanduser("~"), ".meshychat", "cards")


def postbox_config_path():
    """The postbox's config.json, as meshy-mod finds it: MESHY_POSTBOX_CONFIG, else ~/.meshychat/postbox/."""
    return os.environ.get("MESHY_POSTBOX_CONFIG") or os.path.join(os.path.expanduser("~"), ".meshychat", "postbox",
                                                                  "config.json")


def _enabled(entry):
    """An identity is on unless its entry says "enabled": false, as meshy-mod reads it (i.enabled !== false)."""
    return entry.get("enabled") is not False


def postbox_identity(labels):
    """(the entry that serves this pane, the config's base_url): the first label, in MESHY_IDENTITY order, whose entry
    exists in the postbox config and is enabled. On HQB a pane is <seat>,<seat>-prod and <seat> is an old test-server
    identity, off and with no server of its own: taking the first label would load against the test server with a
    retired state file. (None, None) when no label has an enabled entry, or when the config cannot be read (it must
    be a regular file of this user's)."""
    try:
        cfg = json.loads(read_small(postbox_config_path(), MAX_CONFIG_BYTES).decode("utf-8"))
    except Exception:
        return None, None
    entries = cfg.get("identities") if isinstance(cfg, dict) else None
    named = {e["name"]: e for e in (entries if isinstance(entries, list) else ())
             if isinstance(e, dict) and isinstance(e.get("name"), str)}
    for label in labels:
        entry = named.get(label)
        if entry is not None and _enabled(entry):
            return entry, cfg.get("base_url")
    return None, None


def _effective_write_mode(cfg):
    wm = getattr(cfg, "write_mode", None)
    if wm in ("direct", "spool"):
        return wm
    for var in ("MESHY_CARDS_WRITE", "WRITE"):
        if (os.environ.get(var) or "").strip().lower() == "spool":
            return "spool"
    return "direct"


def resolve(cfg):
    """Fill the seat, the store, the server and the state file from the pane's labels (cfg.identity, comma-
    separated); a flag that was given wins.
      - the entry that serves the pane: the first label, in order, whose postbox entry exists and is enabled;
      - the server and the state file: that entry's (its base_url, else the config's);
      - the seat: that entry's "seat" when it has one, else the FIRST label of the pane: on HQB the bare roster
        label (txxxx), which existing card keys use, and not txxxx-prod, which would start a second namespace;
      - the store: <cards root>/<seat>;
      - the loader's markers: that entry's "markers" when no --marker was given (one command line in a shared
        profile cannot carry each seat's).
    None when all are known; else why not."""
    labels = cfg.identity.split(",")
    entry, base = (None, None) if (cfg.seat and cfg.base_url and cfg.state_file and cfg.marker) else \
        postbox_identity(labels)
    named_seat = entry.get("seat") if entry is not None else None
    cfg.seat = cfg.seat or (named_seat if isinstance(named_seat, str) and IDENTITY_RE.match(named_seat)
                            else labels[0])
    if not cfg.store:
        cfg.store = os.path.join(cards_root(), cfg.seat)
    if entry is not None and getattr(cfg, "write_mode", None) is None and entry.get("write_mode") in ("direct", "spool"):
        cfg.write_mode = entry["write_mode"]
    eff_mode = _effective_write_mode(cfg)
    if entry is None:
        if (cfg.base_url and cfg.state_file) or eff_mode == "spool":
            return None                             # the server and the state file given by flag, or spool mode
        return ("no label of %s has an enabled entry in the postbox config, which names its state file and server"
                % cfg.identity)
    markers = entry.get("markers")
    if not cfg.marker and isinstance(markers, list) and len(markers) <= 8 and all(
            isinstance(m, str) and 0 < len(m) <= 200 for m in markers):
        cfg.marker = list(markers) or None
    if not getattr(cfg, "emit_context", False) and entry.get("emit_context") is True:
        cfg.emit_context = True
    url = entry.get("base_url") if isinstance(entry.get("base_url"), str) and entry.get("base_url") else base
    state = entry.get("state")
    cfg.base_url = cfg.base_url or (url if isinstance(url, str) and url else None)
    cfg.state_file = cfg.state_file or (state if isinstance(state, str) and state else None)
    if not (cfg.base_url and cfg.state_file) and eff_mode != "spool":
        return "the postbox config names no %s for identity %s" % ("server" if not cfg.base_url else "state file",
                                                                    entry.get("name"))
    return None


# ---- the store

def _private_dir_problem(path):
    """None when `path` is a real folder of this user's that no other user can write to; else what is wrong."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return "cannot be read (%s)" % type(exc).__name__
    if stat.S_ISLNK(st.st_mode):
        return "is a link"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a folder"
    if WINDOWS:
        return None
    if st.st_uid != _uid():
        return "belongs to another user"
    if st.st_mode & 0o022:
        return "can be written by other users"
    return None


def secure_store(store):
    """Make the store, pending/, results/ and projects/ (mode 0700) where missing, and check them. (None, True) when
    they are sound; else (what is wrong, whether the store itself is sound enough to hold a receipt). A store that
    holds a loader's files at its root is one loader store for every project, the layout before one store per
    project: it is refused, not guessed at."""
    try:
        os.makedirs(store, mode=0o700, exist_ok=True)
    except OSError as exc:
        return "the store cannot be made (%s)" % type(exc).__name__, False
    why = _private_dir_problem(store)
    if why:
        return "the store %s" % why, False
    for name in LOADER_FILES:
        if os.path.lexists(os.path.join(store, name)) or os.path.lexists(os.path.join(store, name + ".tmp")):
            return ("the store holds a loader's %s at its root (one loader store for every project, the layout "
                    "before projects/); move the loader's files out of it" % name), True
    for name in (PENDING, RESULTS, PROJECTS):
        path = os.path.join(store, name)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            return "%s/ cannot be made (%s)" % (name, type(exc).__name__), True
        why = _private_dir_problem(path)
        if why:
            return "%s/ %s" % (name, why), True
    return None, True


def project_store(root, seat, project):
    """(path, None) for the loader's own store of one project folder, <root>/projects/<key>/, made 0700 where missing
    and checked like the root; (path, what is wrong) otherwise. Each project folder has its own: the loader keys its
    cursor and its record of which transcripts are the seat's by file name, and a rebuild clears its whole store, so
    two folders in one store would share both."""
    key = request_key(seat, project)
    path = os.path.join(root, PROJECTS, key)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        return path, "projects/%s/ cannot be made (%s)" % (key, type(exc).__name__)
    why = _private_dir_problem(path)
    return path, ("projects/%s/ %s" % (key, why) if why else None)


def refuse_store(store, seat, why, prefix="store refused"):
    """A refused store (or identity): a failed receipt in the store itself when it is sound, and nothing anywhere
    else."""
    write_json(os.path.join(store, RECEIPT), {"time": stamp(time.time()), "status": "failed",
                                              "detail": ("%s: %s; nothing loaded" % (prefix, why))[:200],
                                              "seat": seat, "project": None})


def another_loader_writing(store, now=None):
    """The name of a loader's temporary file written in the last TMP_FRESH_S beside the store's files, or None.
    It is evidence of a loader run directly on this store; it shows only while that run writes, at its end."""
    now = time.time() if now is None else now
    for name in LOADER_FILES:
        try:
            age = now - os.lstat(os.path.join(store, name + ".tmp")).st_mtime
        except OSError:
            continue
        if -60 < age < TMP_FRESH_S:
            return name + ".tmp"
    return None


# ---- receipts

def canonical(project_dir):
    """The one spelling of a project folder: links resolved, and case folded where the platform ignores it."""
    return os.path.normcase(os.path.realpath(project_dir)) if project_dir else None


def request_key(seat, project_dir):
    """The name of a seat and project folder's request and receipt (`project_dir` already canonical)."""
    return hashlib.sha256(("%s\0%s" % (seat, project_dir or "")).encode("utf-8", "surrogatepass")).hexdigest()[:32]


def result_path(store, key):
    return os.path.join(store, RESULTS, key + ".json")


def read_receipt(store):
    return read_json(os.path.join(store, RECEIPT))


def write_result(store, seat, project, status, detail, now=None, knew_seat=False, counts=None):
    """The project's receipt, then the seat's summary from all of them. knew_seat (run_pass): the folder's loader
    store knew a transcript of the seat's, before or after the pass; it is kept as "knew_seat": true. counts:
    structured {"cards_total": int, "new_cards": int, "outcomes": {outcome: int}} from one_pass (#238 B.5)."""
    receipt = {"time": stamp(time.time() if now is None else now), "status": status, "detail": detail[:200],
               "seat": seat, "project": project}
    if knew_seat:
        receipt["knew_seat"] = True
    if isinstance(counts, dict):
        for k in ("cards_total", "new_cards"):
            v = counts.get(k)
            if type(v) is int and v >= 0:
                receipt[k] = v
        out = counts.get("outcomes")
        if isinstance(out, dict):
            receipt["outcomes"] = {k: v for k, v in out.items() if k in OUTCOMES and type(v) is int and v > 0}
    if not write_json(result_path(store, request_key(seat, project)), receipt):
        return None
    write_summary(store)
    return receipt


#: The note run_pass adds to a NO_MARKER detail when the folder's loader store knew the seat before the pass: a
#: rebuild clears the cursor before the loader exits 3, so only the state before the pass proves it (Codex r2).
KNEW_SEAT = "; the folder's loader store knew this seat's transcripts before the pass"


def seat_history(project_store):
    """What one folder's loader store says of the seat's transcripts: "held" (its cursor.json names a seat file),
    "never" (it names none, or the loader wrote no cursor there), or "unknown" (a cursor that cannot be read)."""
    path = os.path.join(project_store, "cursor.json")
    if not os.path.lexists(path):
        return "never"
    cursor = read_json(path)
    if cursor is None:
        return "unknown"
    return "held" if cursor.get("seat_files") else "never"


def knew_seat(project_store, prior):
    """Whether this folder was known as the seat's before a pass: its loader store holds (or may hold) a seat
    transcript, or its last receipt recorded that it did ("knew_seat", or the KNEW_SEAT note). A receipt is never
    expired, so the evidence outlasts a --rebuild that empties the cursor and every exit 3 after it (local Codex
    review r3). Only explicit evidence counts: an ok load of a folder with no transcript at all exits 0 with an
    empty cursor, which says nothing about the seat (GitHub Codex on #273). An unreadable cursor is no evidence
    here: recorded as knew_seat it would stick to a side folder for good; not_this_seats already counts it as the
    seat's at summary time, for as long as it stays unreadable (memory-bxxxxx)."""
    if project_store and seat_history(project_store) == "held":
        return True
    return isinstance(prior, dict) and (prior.get("knew_seat") is True or KNEW_SEAT in str(prior.get("detail", "")))


def not_this_seats(receipt, store=None):
    """A side session's receipt: a folder that holds none of this seat's transcripts. Its detail is NO_MARKER
    exactly: a note after it says the hook named a transcript (a continuation of the seat's) or the folder's store
    knew the seat before the pass (KNEW_SEAT). With the store given, that folder's loader store must also say
    "never" now; a cursor that cannot be read counts as the seat's (local Codex review: an exit 3 can also be the
    seat's own project)."""
    if receipt.get("status") != "failed" or receipt.get("detail") != NO_MARKER:
        return False
    if store is None:
        return True
    key = request_key(receipt.get("seat"), receipt.get("project"))
    return seat_history(os.path.join(store, PROJECTS, key)) == "never"


def summary(receipts, now, store=None):
    """The seat's receipt from its projects' receipts: of those of the last SUMMARY_WINDOW_S, the newest, unless
    some did not finish cleanly; then the newest of those. None when there is none. A folder that holds none of the
    seat's transcripts (not_this_seats: a session started from the seat's shell elsewhere) is not its project and
    counts only when no other receipt is there, so it cannot hide a healthy feed (vxxxxxxxxxx, kxxx;
    #238 B3), nor a real failure of the seat's own project be hidden by it."""
    dated = [(instant(r.get("time")), r) for r in receipts if isinstance(r, dict)]
    recent = [(at, r) for at, r in dated if at is not None and 0 <= now - at < SUMMARY_WINDOW_S]
    own = [(at, r) for at, r in recent if not not_this_seats(r, store)]
    recent = own or recent
    unclean = [(at, r) for at, r in recent if r.get("status") not in CLEAN]
    pool = unclean or recent
    return max(pool, key=lambda x: x[0])[1] if pool else None


def newest_receipts(folder):
    """The names of the MAX_SCAN receipts in `folder` written last (by their file's mtime, newest first), regular
    files only and no link followed. Receipts are never expired, so a seat with more project folders than MAX_SCAN
    keeps more than it reads: choosing by name (a hash) would leave a new failure out for good if its hash sorted
    late (GitHub Codex on 47fe56e)."""
    dated = []
    with os.scandir(folder) as entries:
        for entry in entries:
            try:
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    dated.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
            except OSError:
                continue
    return [name for _, name in sorted(dated, reverse=True)[:MAX_SCAN]]


def write_summary(store):
    folder = os.path.join(store, RESULTS)
    try:
        names = newest_receipts(folder)
    except OSError:
        return None
    pick = summary([read_json(os.path.join(folder, n)) for n in names], time.time(), store)
    if pick is not None:
        write_json(os.path.join(store, RECEIPT), pick)
    return pick


def throttled(receipt, now):
    """Why a prompt loads nothing for its project, or None. Only the receipt and `now` decide, so the bounds can
    be tested exactly. A receipt from the future (a clock set back) throttles nothing: the load writes a new one."""
    at = instant(receipt.get("time")) if isinstance(receipt, dict) else None
    if at is None:
        return None
    age = now - at
    if age < 0:
        return None
    if receipt.get("status") == "ok" and age < OK_FRESH_S:
        return "ok"
    if age < ANY_FRESH_S:
        return "recent"
    return None


# ---- the lock

def _try_lock(fd):
    """A non-blocking exclusive lock on the open file `fd` (tools/postbox/postbox_os.try_lock). Windows: msvcrt's
    one-byte lock. POSIX: flock on the whole file, which another descriptor's close does not drop."""
    if WINDOWS:
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def take_lock(store):
    """A descriptor holding the store's lock, or None while another process holds it (or load.lock is not a
    regular file of this user's). Released by `release_lock`, or by the operating system when the holder dies."""
    try:
        fd, _ = _open_regular(os.path.join(store, LOCK), os.O_RDWR | os.O_CREAT)
    except (OSError, Unsafe):
        return None
    if _try_lock(fd):
        return fd
    os.close(fd)
    return None


def release_lock(fd):
    """Release the lock this process holds on `fd`, and nothing else: the file stays."""
    try:
        if WINDOWS:
            import msvcrt
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---- the queue

def plain_transcript(name):
    """`name` when it is a plain .jsonl file name UTF-8 can carry (no folder, no drive, no NUL), else None."""
    if not isinstance(name, str) or not TRANSCRIPT_RE.match(name) or name in (".jsonl",):
        return None
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return name


def request_of(cfg):
    """A request: the canonical project folder, the session's transcript file name in it (a continuation after a
    compaction holds no seat marker, so the loader is told it is the seat's), and when it was asked. Nothing else;
    see run_pass."""
    return {"project_dir": canonical(cfg.project_dir), "transcript": plain_transcript(getattr(cfg, "transcript", None)),
            "time": time.time()}


TRANSCRIPT_MISSING = "not there"


def transcript_problem(project, name):
    """None when `name` is a regular file of this user's in the project folder (opened as every store file is: no
    link followed, no pipe waited on); TRANSCRIPT_MISSING when there is no such file yet (a session's transcript
    can be written after its first hook); else what it is instead."""
    if not project:
        return "no project folder"
    try:
        fd, _ = _open_regular(os.path.join(project, name))
    except FileNotFoundError:
        return TRANSCRIPT_MISSING
    except (OSError, Unsafe) as exc:
        return str(exc) or type(exc).__name__
    os.close(fd)
    return None


def valid_request(obj):
    """A request the worker may act on: an object whose project folder is null or an absolute path of at most
    MAX_PATH_CHARS that UTF-8 can carry, with no NUL, whose transcript is null or a plain .jsonl file name (with a
    folder), and whose time is a number from 0 to MAX_TIME. Other fields are ignored. An int is compared as it is, never turned into a float first: 10**400 would overflow."""
    if not isinstance(obj, dict):
        return False
    project, at = obj.get("project_dir"), obj.get("time")
    if project is not None:
        if not isinstance(project, str) or not project or len(project) > MAX_PATH_CHARS or "\0" in project:
            return False
        try:
            project.encode("utf-8")
        except UnicodeEncodeError:
            return False
        if not os.path.isabs(project):
            return False
    transcript = obj.get("transcript")
    if transcript is not None and (plain_transcript(transcript) is None or project is None):
        return False
    if isinstance(at, bool):
        return False
    if isinstance(at, int):
        return 0 <= at <= MAX_TIME
    return isinstance(at, float) and 0 <= at <= MAX_TIME          # NaN and the infinities fail the range


def read_request(path):
    """("ok", request) | ("bad", None) | ("gone", None) for a queue entry. Whatever its bytes, it never raises: any
    failure to read, decode or check it makes it "bad"."""
    try:
        data = read_small(path)
    except FileNotFoundError:
        return "gone", None
    except Exception:
        return "bad", None
    try:
        obj = json.loads(data.decode("utf-8"))
        return ("ok", obj) if valid_request(obj) else ("bad", None)
    except Exception:
        return "bad", None


def quarantine(path):
    """Set a queue entry aside as <name>.bad, never to be read again; remove it when even that fails."""
    try:
        os.replace(path, path + BAD)
    except OSError:
        _unlink(path)


def queue_name(seat, project, transcript):
    """A request's file name in pending/: <key>.json for a folder, <key>.t<16 hex of the name>.json for a folder and
    one transcript in it. The hook takes no lock, so two hooks of one folder that name different transcripts (two
    continuations) must never write the same file: a merge there would race and lose a name, and a continuation
    holds no seat marker, so a lost name is a transcript never read. A second request for the same folder and the
    same transcript (or for the folder alone) replaces the first, which is the same request."""
    key = request_key(seat, project)
    if transcript:
        return "%s.t%s.json" % (key, hashlib.sha256(transcript.encode("utf-8", "surrogatepass")).hexdigest()[:16])
    return key + ".json"


def enqueue(store, cfg):
    req = request_of(cfg)
    return write_json(os.path.join(store, PENDING, queue_name(cfg.seat, req["project_dir"], req["transcript"])), req)


def has_pending(store):
    try:
        return any(n.endswith(".json") or n.endswith(".json" + CLAIMED)
                   for n in os.listdir(os.path.join(store, PENDING)))
    except OSError:
        return False


def queued(store):
    """[(path, request)], oldest first, with a claimed request a dead worker left before the rest. At most MAX_SCAN
    entries are looked at; one that is not a well-formed request of this user's is quarantined."""
    folder = os.path.join(store, PENDING)
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".json") or n.endswith(".json" + CLAIMED))
    except Exception:
        return []
    found = []
    for name in names[:MAX_SCAN]:
        path = os.path.join(folder, name)
        try:
            kind, req = read_request(path)
            if kind == "ok":
                found.append((0 if name.endswith(CLAIMED) else 1, req["time"], name, path, req))
                continue
        except Exception:                         # the scan itself never raises: an entry it cannot judge is bad
            kind = "bad"
        if kind == "bad":
            quarantine(path)
    return [(path, req) for _, _, _, path, req in sorted(found, key=lambda x: x[:3])]


def claim(path):
    """Rename a request to <name>.claimed before its pass: one that arrives during the pass is a new file beside
    it, and is not lost. The claimed file is removed after the pass, so a worker that dies leaves it to the next."""
    if path.endswith(CLAIMED):
        return path
    try:
        os.replace(path, path + CLAIMED)
    except OSError:
        return None
    return path + CLAIMED


def unclaim(claimed):
    """Put a claimed request back for a later worker (a newer request for the same folder wins)."""
    queued_path = claimed[:-len(CLAIMED)]
    if os.path.lexists(queued_path):
        _unlink(claimed)
    else:
        try:
            os.replace(claimed, queued_path)
        except OSError:
            pass


# ---- Windows: a stage dies with its worker

_JOB = None


def _kernel32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _last_error():
    import ctypes
    return getattr(ctypes, "get_last_error", lambda: 0)()


def stage_job():
    """Windows: (kernel32, the worker's one kill-on-close job), made on first use and held open for the worker's
    life, so the worker's death, however it dies, ends every stage in it (tools/postbox/postbox.tie_to_daemon)."""
    global _JOB
    if _JOB is None:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = _kernel32()
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k.CreateJobObjectW(None, None)
        if not job:
            raise JobError("CreateJobObjectW failed (error %d)" % _last_error())
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                         ctypes.sizeof(info)):
            error = _last_error()
            k.CloseHandle(job)
            raise JobError("SetInformationJobObject failed (error %d)" % error)
        _JOB = (k, job)
    return _JOB


def tie_to_worker(proc):
    """Windows: put a stage just started into the worker's job. It is done right after the start, so a worker
    killed in the moment between the two leaves that stage outside the job (it starts nothing of its own)."""
    k, job = stage_job()
    if not k.AssignProcessToJobObject(job, int(proc._handle)):
        raise JobError("AssignProcessToJobObject failed (error %d)" % _last_error())


# ---- the stages

_SEAT_FILE = {}
_EMIT_CONTEXT = {}
_APPEND_RE = re.compile(r"""\baction\s*=\s*["']append["']""")


def _call_text(source, start):
    """The text of the call that opens at or after `start`, up to its own closing parenthesis (quoted text and
    nested calls skipped), or the rest of the source."""
    depth, i, quote = 0, source.index("(", start), None
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    return source[start:]


def seat_file_mode(loader):
    """How the loader takes --seat-file (txxxx's flag, which marks a transcript as the seat's): None when it has no
    such flag; "append" when its add_argument says action="append", so every name goes to one loader stage as
    --seat-file once a name; "each" for any other declaration, so the pass runs one loader stage a name. Found once a
    worker by reading the script's own argument definition, a source scan: nothing is run."""
    if loader not in _SEAT_FILE:
        try:
            source = read_small(loader, 8 << 20).decode("utf-8", "replace")
            match = SEAT_FILE_FLAG_RE.search(source)
            _SEAT_FILE[loader] = (None if match is None else
                                  "append" if _APPEND_RE.search(_call_text(source, match.start())) else "each")
        except Exception:
            _SEAT_FILE[loader] = None
    return _SEAT_FILE[loader]


def loader_takes_seat_file(loader):
    return seat_file_mode(loader) is not None


def loader_takes_emit_context(loader):
    """Whether the loader takes --emit-context (#297), found once a worker by reading the script's own argument
    definition (ignoring comment lines), a source scan: nothing is run."""
    if loader not in _EMIT_CONTEXT:
        try:
            source = read_small(loader, 8 << 20).decode("utf-8", "replace")
            code_lines = "\n".join(ln for ln in source.splitlines() if not ln.lstrip().startswith("#"))
            _EMIT_CONTEXT[loader] = bool(EMIT_CONTEXT_FLAG_RE.search(code_lines))
        except Exception:
            _EMIT_CONTEXT[loader] = False
    return _EMIT_CONTEXT[loader]


def _read_store_json(path):
    try:
        obj = json.loads(read_small(path, MAX_STORE_FILE_BYTES).decode("utf-8"))
    except (OSError, Unsafe, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _read_store_jsonl(path):
    try:
        raw = read_small(path, MAX_STORE_FILE_BYTES).decode("utf-8")
    except FileNotFoundError:
        return []
    except (OSError, Unsafe, ValueError):
        return None
    rows = []
    for line in raw.splitlines():
        if line.strip():
            try:
                obj = json.loads(line)
            except ValueError:
                return None
            if not isinstance(obj, dict):
                return None
            rows.append(obj)
    return rows


def _write_text_atomic(path, text):
    folder = os.path.dirname(path)
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=folder)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return False


def _recent_project_stores(root_store, exclude_store=None):
    """Up to MAX_GROUP project store directories under <root_store>/projects/, newest cursor.json first, scanning at
    most MAX_SCAN directory entries (#326)."""
    projects_dir = os.path.join(root_store, PROJECTS) if root_store else None
    if not projects_dir or not os.path.isdir(projects_dir):
        return []
    scored = []
    try:
        with os.scandir(projects_dir) as it:
            for entry in itertools.islice(it, MAX_SCAN):
                pdir = entry.path
                if pdir == exclude_store:
                    continue
                try:
                    st = os.lstat(os.path.join(pdir, "cursor.json"))
                    if stat.S_ISREG(st.st_mode):
                        scored.append((st.st_mtime, pdir))
                except OSError:
                    continue
    except OSError:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)
    return [pdir for _, pdir in scored[:MAX_GROUP]]


def _uninspected_project_stores(root_store, inspected_dirs):
    """Project store directories under <root_store>/projects/ outside `inspected_dirs`."""
    projects_dir = os.path.join(root_store, PROJECTS) if root_store else None
    if not projects_dir or not os.path.isdir(projects_dir):
        return []
    inspected_norm = {os.path.normcase(os.path.abspath(d)) for d in (inspected_dirs or ()) if d}
    out = []
    try:
        with os.scandir(projects_dir) as it:
            for entry in itertools.islice(it, MAX_SCAN):
                pdir = entry.path
                if os.path.normcase(os.path.abspath(pdir)) in inspected_norm:
                    continue
                try:
                    st = os.lstat(os.path.join(pdir, "cursor.json"))
                    if stat.S_ISREG(st.st_mode):
                        out.append(pdir)
                except OSError:
                    continue
    except OSError:
        return []
    return out


def _settled_project_head(store_dir):
    """Return {"id": ..., "sha256": ..., "time": ..., "card": ...} from <store_dir>/cursor.json when its cursor has a
    settled Context head (`context_head_card` present and no unconfirmed `context_prev_head`). Never reads cards.jsonl
    (#326)."""
    if not store_dir or not os.path.isdir(store_dir):
        return None
    cur = _read_store_json(os.path.join(store_dir, "cursor.json"))
    if not isinstance(cur, dict) or "context_prev_head" in cur:
        return None
    cid = cur.get("context_head_id")
    card = cur.get("context_head_card")
    if not (isinstance(cid, str) and cid and isinstance(card, dict)
            and card.get("id") == cid and card.get("type") == "Context"):
        return None
    return {
        "id": cid,
        "sha256": cur.get("context_head_sha256"),
        "time": cur.get("context_head_time") or card.get("time"),
        "card": card,
    }


def _seed_project_context_head(root_store, project_store):
    """Seed <root>/context-head.json into <project_store> before a pass with --emit-context so a seat that loads
    several project folders maintains one superseded Context head on the server (#297). Returns (backup, err_detail)
    where backup is (cursor_bytes, cards_bytes, foreign_id) when seeded, and err_detail is non-None when seeding
    failed and the pass must stop rather than run with a stale local Context head."""
    if not root_store or not project_store:
        return None, None
    head_path = os.path.join(root_store, CONTEXT_HEAD)
    head = None
    if os.path.lexists(head_path):
        head = _read_store_json(head_path)
        if not isinstance(head, dict):
            return None, "context-head.json could not be read; nothing written"
        hid, hcard = head.get("id"), head.get("card")
        if not (isinstance(hid, str) and hid and isinstance(hcard, dict)
                and hcard.get("id") == hid and hcard.get("type") == "Context"):
            return None, "context-head.json is invalid; nothing written"
    # Issue #326: if a previous pass accepted a newer Context head B but failed to update <root>/context-head.json,
    # recover B from the bounded set of recent settled project cursors without reading any cards.jsonl.
    candidates = [project_store] + _recent_project_stores(root_store, exclude_store=project_store)
    all_heads = [head] if head is not None else []
    for cand_dir in candidates:
        cand = _settled_project_head(cand_dir)
        if cand is not None and all(h["id"] != cand["id"] for h in all_heads):
            all_heads.append(cand)
    if not all_heads:
        return None, None
    superseded_ids = {
        s.rsplit("/", 1)[-1]
        for h in all_heads
        for s in (h["card"].get("supersedes") or ())
        if isinstance(s, str)
    }
    leaves = [h for h in all_heads if h["id"] not in superseded_ids] or all_heads
    best = max(leaves, key=lambda h: (h.get("time") or h["card"].get("time") or "", h["id"]))
    if head is None or best["id"] != head["id"]:
        head = best
        _write_text_atomic(head_path, json.dumps(head, sort_keys=True, indent=1) + "\n")
    hid, hcard = head["id"], head["card"]
    cursor_path = os.path.join(project_store, "cursor.json")
    cards_path = os.path.join(project_store, CARDS)
    try:
        cursor_bytes = read_small(cursor_path, MAX_STORE_FILE_BYTES) if os.path.lexists(cursor_path) else None
        cards_bytes = read_small(cards_path, MAX_STORE_FILE_BYTES) if os.path.lexists(cards_path) else None
    except (OSError, Unsafe):
        return None, "project store could not be read for context-head seeding; nothing written"
    if cursor_bytes is not None:
        try:
            cursor = json.loads(cursor_bytes.decode("utf-8"))
        except ValueError:
            return None, "project store cursor.json is invalid; nothing written"
        if not isinstance(cursor, dict):
            return None, "project store cursor.json is invalid; nothing written"
    else:
        cursor = {"files": {}, "seat_files": {}, "id_rule": "1.7"}
    rows = _read_store_jsonl(cards_path)
    if rows is None:
        return None, "project store cards.jsonl is invalid; nothing written"
    had_card = any(r.get("id") == hid and r.get("type") == "Context" for r in rows)
    if cursor.get("context_head_id") == hid and had_card and "context_prev_head" not in cursor:
        return None, None
    backup = (cursor_bytes, cards_bytes, None if had_card else hid)
    cursor.setdefault("id_rule", "1.7")
    cursor.pop("context_prev_head", None)
    cursor["context_head_id"] = hid
    cursor["context_head_sha256"] = head.get("sha256")
    cursor["context_head_time"] = head.get("time") or hcard.get("time")
    cursor["context_head_card"] = hcard
    seeded_rows = [r for r in rows if r.get("type") != "Context"] + [hcard]
    seeded_rows.sort(key=lambda c: (c.get("time") or "", c.get("id") or ""))
    if not _write_text_atomic(cards_path, "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n"
                                                  for c in seeded_rows)):
        return None, "project store cards.jsonl could not be seeded; nothing written"
    if not _write_text_atomic(cursor_path, json.dumps(cursor, sort_keys=True, indent=1)):
        _restore_project_context_seed(project_store, backup)
        return None, "project store cursor.json could not be seeded; nothing written"
    return backup, None


def _restore_project_context_seed(project_store, backup):
    """Restore <project_store> after a failed or exit-3 loader stage when _seed_project_context_head modified it."""
    if not project_store or backup is None:
        return
    cursor_bytes, cards_bytes, _ = backup
    cursor_path = os.path.join(project_store, "cursor.json")
    cards_path = os.path.join(project_store, CARDS)
    if cards_bytes is None:
        if os.path.lexists(cards_path):
            rows = _read_store_jsonl(cards_path)
            if rows is not None:
                kept = [r for r in rows if r.get("type") != "Context"]
                _write_text_atomic(cards_path, "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n"
                                                       for c in kept))
    else:
        _write_text_atomic(cards_path, cards_bytes.decode("utf-8", "replace"))
    if cursor_bytes is None:
        cur = _read_store_json(cursor_path)
        if isinstance(cur, dict):
            for k in ("context_head_id", "context_head_sha256", "context_head_time", "context_head_card",
                      "context_prev_head"):
                cur.pop(k, None)
            _write_text_atomic(cursor_path, json.dumps(cur, sort_keys=True, indent=1))
    else:
        _write_text_atomic(cursor_path, cursor_bytes.decode("utf-8", "replace"))


def _unseed_foreign_context_card(project_store, backup, report, total):
    """When a seeded Context head from another project folder was not replaced by a newly emitted Context card in
    this folder, remove the foreign Context card from <project_store>/cards.jsonl before `card write`."""
    if not project_store or backup is None:
        return total
    _, _, foreign_id = backup
    if not foreign_id:
        return total
    ctx_info = report.get("context_card") if isinstance(report, dict) else None
    if isinstance(ctx_info, dict) and ctx_info.get("status") == "emitted":
        return total
    cards_path = os.path.join(project_store, CARDS)
    rows = _read_store_jsonl(cards_path)
    if rows is None or not any(r.get("id") == foreign_id for r in rows):
        return total
    kept = [r for r in rows if r.get("id") != foreign_id]
    _write_text_atomic(cards_path, "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in kept))
    rep_path = os.path.join(project_store, "report.json")
    rep = _read_store_json(rep_path)
    if isinstance(rep, dict):
        rep["cards_total"] = len(kept)
        by_type = rep.get("cards_by_type")
        if isinstance(by_type, dict) and "Context" in by_type:
            if by_type["Context"] <= 1:
                by_type.pop("Context", None)
            else:
                by_type["Context"] -= 1
        _write_text_atomic(rep_path, json.dumps(rep, sort_keys=True, indent=1))
    return len(kept) if total is not None else None


def _settle_project_context_head(root_store, project_store, seat, code2, answer, backup=None):
    """Confirm or roll back the emitted Context card in <project_store> after `card write`, and persist the accepted
    head in <root>/context-head.json (#297). Returns an error string when persisting the accepted head failed."""
    if not project_store:
        return None
    cursor_path = os.path.join(project_store, "cursor.json")
    cards_path = os.path.join(project_store, CARDS)
    cursor = _read_store_json(cursor_path)
    rows = _read_store_jsonl(cards_path)
    if cursor is None or rows is None:
        return None
    ctx_id = cursor.get("context_head_id")
    ctx_cards = [r for r in rows if r.get("type") == "Context"]
    ctx_card = next((r for r in ctx_cards if r.get("id") == ctx_id), ctx_cards[-1] if ctx_cards else None)
    if ctx_card is None or not isinstance(ctx_card.get("id"), str) or not ctx_card["id"]:
        return None
    ctx_key = "card/%s/%s" % (seat, ctx_card["id"])
    accepted = False
    err_code = "write_timeout" if code2 is None else "write_failed"
    if isinstance(answer, dict):
        probs = answer.get("problems")
        prob = next((p for p in (probs if isinstance(probs, list) else ())
                     if isinstance(p, dict) and p.get("key") == ctx_key), None)
        if prob is not None:
            accepted = False
            err_code = prob.get("error_code") or prob.get("outcome") or err_code
        elif answer.get("outcome") in ("written", "exists"):
            accepted = True
        elif answer.get("outcome") is not None:
            accepted = False
            err_code = answer.get("error_code") or answer.get("outcome") or err_code
        elif code2 == 0:
            accepted = True
        elif isinstance(answer.get("written"), list) and ctx_key in answer["written"]:
            accepted = True
    if accepted:
        cursor_err = None
        if "context_prev_head" in cursor or cursor.get("context_head_card") != ctx_card:
            cursor_copy = dict(cursor)
            cursor_copy.pop("context_prev_head", None)
            cursor_copy["context_head_card"] = ctx_card
            if not _write_text_atomic(cursor_path, json.dumps(cursor_copy, sort_keys=True, indent=1)):
                cursor_err = "cursor.json could not be updated"
        if root_store:
            head_payload = json.dumps({
                "id": ctx_card["id"],
                "sha256": cursor.get("context_head_sha256"),
                "time": cursor.get("context_head_time") or ctx_card.get("time"),
                "card": ctx_card,
            }, sort_keys=True, indent=1) + "\n"
            if not _write_text_atomic(os.path.join(root_store, CONTEXT_HEAD), head_payload):
                return "context-head.json could not be updated"
        return cursor_err
    if "context_prev_head" not in cursor:
        return None
    prev = cursor.pop("context_prev_head", None) or {}
    foreign_id = backup[2] if backup is not None else None
    kept = [r for r in rows if r.get("type") != "Context"]
    prev_card = prev.get("card")
    if isinstance(prev_card, dict) and prev_card.get("id"):
        if prev_card["id"] != foreign_id:
            kept.append(prev_card)
        cursor["context_head_id"] = prev.get("id")
        cursor["context_head_sha256"] = prev.get("sha256")
        cursor["context_head_time"] = prev.get("time")
        cursor["context_head_card"] = prev_card
    else:
        cursor.pop("context_head_id", None)
        cursor.pop("context_head_sha256", None)
        cursor.pop("context_head_time", None)
        cursor.pop("context_head_card", None)
    kept.sort(key=lambda c: (c.get("time") or "", c.get("id") or ""))
    _write_text_atomic(cards_path, "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in kept))
    _write_text_atomic(cursor_path, json.dumps(cursor, sort_keys=True, indent=1))
    rep_path = os.path.join(project_store, "report.json")
    rep = _read_store_json(rep_path)
    if isinstance(rep, dict) and isinstance(rep.get("context_card"), dict):
        rep["context_card"]["status"] = "refused"
        rep["context_card"]["error_code"] = str(err_code)
        _write_text_atomic(rep_path, json.dumps(rep, sort_keys=True, indent=1))
    return None


_ACTIVE_CONTEXT_KEY_RE = re.compile(r"card/([a-z0-9][a-z0-9_.-]{0,63})/([0-9a-f]{32})")


def active_context_cmd(cfg):
    head = [sys.executable, cfg.writer] if cfg.writer else [sys.executable, "-m", "tools.meshy_node"]
    return head + ["card", "list", "--type", "Context", "--seat", cfg.seat, "--status", "active",
                   "--limit", "200", "--base-url", cfg.base_url, "--state-file", cfg.state_file]


def _context_supersedes_refusal(project_store, seat, answer):
    """When `answer` refuses the project store's Context card with E_CARD_SUPERSEDES, return (ctx_card, prob_dict)."""
    if not project_store or not isinstance(answer, dict):
        return None, None
    cursor = _read_store_json(os.path.join(project_store, "cursor.json"))
    rows = _read_store_jsonl(os.path.join(project_store, CARDS))
    if cursor is None or not rows:
        return None, None
    ctx_id = cursor.get("context_head_id")
    ctx_cards = [r for r in rows if r.get("type") == "Context"]
    ctx_card = next((r for r in ctx_cards if r.get("id") == ctx_id), ctx_cards[-1] if ctx_cards else None)
    if ctx_card is None or not isinstance(ctx_card.get("id"), str) or not ctx_card["id"]:
        return None, None
    ctx_key = "card/%s/%s" % (seat, ctx_card["id"])
    probs = answer.get("problems")
    prob = next((p for p in (probs if isinstance(probs, list) else ())
                 if isinstance(p, dict) and p.get("key") in (ctx_key, None)), None)
    target = prob if prob is not None else answer
    if (target.get("error_code") or target.get("outcome")) != "E_CARD_SUPERSEDES":
        return None, None
    return ctx_card, target


def _recover_server_context_head(cfg, refused_ctx, refusal_obj, lock_fd=None):
    """Issue #326: after an E_CARD_SUPERSEDES refusal on a Context card, read the seat's active Context head from the
    server (`card list --type Context --seat <seat> --status active`) or from the refusal message itself, update
    <root>/context-head.json and <project_store>, and return a seed backup tuple when a different active head was
    seeded for retry (or None)."""
    active_card = None
    code, _, out, _ = run_stage(active_context_cmd(cfg), cfg.stage_timeout, cwd=None if cfg.writer else REPO,
                                lock_fd=lock_fd)
    if code == 0:
        body = _json(out)
        cards_list = body.get("cards") if isinstance(body, dict) else None
        if isinstance(cards_list, list):
            valid = [
                c for c in cards_list
                if isinstance(c, dict) and c.get("type") == "Context"
                and c.get("status", "active") == "active"
                and not c.get("superseded_by")
                and isinstance(c.get("id"), str) and c["id"]
            ]
            if valid:
                valid.sort(key=lambda c: (c.get("time") or "", c.get("id") or ""))
                active_card = dict(valid[-1])
                for server_only in ("key", "version", "written_at", "superseded_by", "superseded_by_count",
                                    "overruled_by", "overruled_by_count", "named_by_redacted",
                                    "named_by_redacted_count", "truncated_lists", "replaces_requested", "thread_tier"):
                    active_card.pop(server_only, None)
    if active_card is None:
        msg = refusal_obj.get("error_message") if isinstance(refusal_obj, dict) else None
        m = _ACTIVE_CONTEXT_KEY_RE.search(msg) if isinstance(msg, str) else None
        if m and m.group(1) == cfg.seat:
            aid = m.group(2)
            root_store = getattr(cfg, "root_store", None)
            for pdir in _recent_project_stores(root_store):
                settled = _settled_project_head(pdir)
                if settled is not None and settled["id"] == aid:
                    active_card = settled["card"]
                    break
            if active_card is None:
                active_card = {
                    "schema": "card.v1",
                    "id": aid,
                    "type": "Context",
                    "owner": cfg.seat,
                    "seat": cfg.seat,
                    "date": "1970-01-01",
                    "time": "1970-01-01T00:00:00.000Z",
                    "status": "active",
                    "text": "Context | owner: %s | seat: %s | 1970-01-01 00:00\nTask: active head" % (cfg.seat, cfg.seat),
                    "refs": {"branch": None, "prs": [], "threads": [], "files": []},
                }
    if not isinstance(active_card, dict) or not active_card.get("id"):
        return None
    aid = active_card["id"]
    if refused_ctx.get("supersedes") == ["card/%s/%s" % (cfg.seat, aid)]:
        return None
    src = active_card.get("source")
    asha = src.get("source_sha256") if isinstance(src, dict) else None
    atime = active_card.get("time")
    root_store = getattr(cfg, "root_store", None)
    if root_store:
        _write_text_atomic(os.path.join(root_store, CONTEXT_HEAD), json.dumps({
            "id": aid,
            "sha256": asha,
            "time": atime,
            "card": active_card,
        }, sort_keys=True, indent=1) + "\n")
    cursor_path = os.path.join(cfg.store, "cursor.json")
    cards_path = os.path.join(cfg.store, CARDS)
    try:
        cursor_bytes = read_small(cursor_path, MAX_STORE_FILE_BYTES) if os.path.lexists(cursor_path) else None
        cards_bytes = read_small(cards_path, MAX_STORE_FILE_BYTES) if os.path.lexists(cards_path) else None
    except (OSError, Unsafe):
        return None
    cursor = _read_store_json(cursor_path)
    rows = _read_store_jsonl(cards_path)
    if cursor is None or rows is None:
        return None
    prev = cursor.get("context_prev_head")
    prev_card = prev.get("card") if isinstance(prev, dict) else None
    had_card = isinstance(prev_card, dict) and prev_card.get("id") == aid
    rec_backup = (cursor_bytes, cards_bytes, None if had_card else aid)
    cursor.pop("context_prev_head", None)
    cursor["context_head_id"] = aid
    cursor["context_head_sha256"] = asha
    cursor["context_head_time"] = atime
    cursor["context_head_card"] = active_card
    seeded_rows = [r for r in rows if r.get("type") != "Context"] + [active_card]
    seeded_rows.sort(key=lambda c: (c.get("time") or "", c.get("id") or ""))
    if not _write_text_atomic(cards_path, "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n"
                                                  for c in seeded_rows)):
        return None
    if not _write_text_atomic(cursor_path, json.dumps(cursor, sort_keys=True, indent=1)):
        _restore_project_context_seed(cfg.store, rec_backup)
        return None
    return rec_backup


def loader_cmd(cfg):
    cmd = [sys.executable, cfg.loader or os.path.join(HERE, "transcript_cards.py"),
           "--project-dir=" + cfg.project_dir, "--seat=" + cfg.seat, "--out=" + cfg.store]
    cmd += ["--marker=" + m for m in cfg.marker or ()]
    cmd += ["--own-name=" + n for n in cfg.own_name or ()]
    return cmd + list(getattr(cfg, "extra", ()) or ())


def writer_cmd(cfg):
    head = [sys.executable, cfg.writer] if cfg.writer else [sys.executable, "-m", "tools.meshy_node"]
    return head + ["card", "write", "--file", os.path.join(cfg.store, CARDS),
                   "--base-url", cfg.base_url, "--state-file", cfg.state_file]


def _env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("WRITE", None)
    env.pop("MESHY_CARDS_WRITE", None)
    return env


def stage_kwargs(windows, lock_fd=None):
    """Popen arguments for a stage. Windows: the detached worker has no console, and a console program it starts
    would open a console window on the operator's desktop at every load. POSIX: the stage shares the locked
    descriptor, so the lock outlives a worker killed in the middle of the stage."""
    if windows:
        return {"creationflags": CREATE_NO_WINDOW}
    return {"pass_fds": (lock_fd,)} if lock_fd is not None else {}


def run_stage(cmd, timeout, cwd=None, lock_fd=None):
    """(exit code, or None when it ran past `timeout` and was stopped; seconds; stdout; stderr). Windows: JobError
    when the stage could not be put in the worker's job; the stage is stopped before it is reported."""
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, cwd=cwd, env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, **stage_kwargs(WINDOWS, lock_fd))
    if WINDOWS:
        try:
            tie_to_worker(p)
        except BaseException:
            p.kill()
            try:
                p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return None, time.monotonic() - t0, "", ""
    return p.returncode, time.monotonic() - t0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def _count(obj, name):
    v = obj.get(name) if isinstance(obj, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def outcome_counts(answer):
    """{outcome: count} from `card write`'s answer: the summary of a file, or the report of a single card. Only
    the names in OUTCOMES and whole numbers are kept."""
    if not isinstance(answer, dict):
        return {}
    if isinstance(answer.get("outcomes"), dict):
        return {k: _count(answer["outcomes"], k) for k in OUTCOMES if _count(answer["outcomes"], k)}
    if answer.get("outcome") in OUTCOMES:
        return {answer["outcome"]: 1}
    return {}


def refused_before_sending(stderr):
    """True when `card write` said on stderr that it refused before sending ({"refused": ...}). Only the key is
    looked at, never what it says."""
    for line in stderr.splitlines():
        obj = _json(line.strip()) if line.strip().startswith("{") else None
        if isinstance(obj, dict) and "refused" in obj:
            return True
    return False


def _store_excluded_fn(store_dir):
    """The store's exclusion matcher, or None when it excludes nothing. A missing cursor.json is a store with no
    exclusions. One that exists but is unreadable or malformed RAISES PolicyUnreadable: it must stop spooling that
    store, never read as "no exclusions" and re-spool what it excluded (R3)."""
    path = os.path.join(store_dir, "cursor.json")
    if not os.path.lexists(path):
        return None
    cur = _read_store_json(path)
    if cur is None:
        raise PolicyUnreadable(path)
    excluded = exclusion_policy(cur, path)
    ex_specs = tuple(exclusion_specs(excluded))
    if not ex_specs:
        return None
    base = cur.get("exclude_base")
    # The same matcher and the same base the loader used (R2): a relative provenance path is read against the
    # store's recorded base, so an absolute exclusion matches it.
    return lambda p: _matches_file_spec(p, ex_specs, base_dir=base)


def _store_excluded_fn_or_skip(store_dir):
    """For root aggregation: (fn, ok). A store whose policy is unreadable contributes NO cards (fail closed)."""
    try:
        return _store_excluded_fn(store_dir), True
    except PolicyUnreadable as exc:
        sys.stderr.write("load_hook: %s is unreadable; its cards are not spooled this run\n" % exc)
        return None, False


def _sync_spool_stores(cfg, root_store, *, spool_project=True):
    rows = _read_store_jsonl(os.path.join(cfg.store, CARDS))
    if rows is None:
        return None
    store_ex_fn, ok = _store_excluded_fn_or_skip(cfg.store)
    if not ok:
        return None
    screened_rows = [
        c for c in rows
        if isinstance(c, dict) and c.get("id") and not c.get("repeat_of") and c.get("type") != "Context"
        and not _card_matches_exclusion(c, store_ex_fn)
        and not detect(_card_strings(c), (), cfg.seat)
    ]
    spooled = None
    if spool_project:
        spooled = write_pending_spool(
            cfg.store, screened_rows, replace=True, preserve_external=True,
            producer=("transcript", cfg.seat), is_excluded_fn=store_ex_fn,
        )
    if root_store and os.path.abspath(root_store) != os.path.abspath(cfg.store):
        root_pending_path = _pending_spool_path(root_store)
        if spool_project or os.path.isfile(root_pending_path):
            by_id = {}
            recent_pdirs = _recent_project_stores(root_store, exclude_store=cfg.store)
            for pdir in reversed(recent_pdirs):
                p_ex_fn, p_ok = _store_excluded_fn_or_skip(pdir)
                if not p_ok:
                    continue
                for c in _read_store_jsonl(os.path.join(pdir, CARDS)) or ():
                    if (
                        isinstance(c, dict)
                        and c.get("id")
                        and not c.get("repeat_of")
                        and c.get("type") != "Context"
                        and not _card_matches_exclusion(c, p_ex_fn)
                        and not detect(_card_strings(c), (), cfg.seat)
                    ):
                        by_id[c["id"]] = c
            for c in screened_rows:
                by_id[c["id"]] = c
            if os.path.isfile(root_pending_path):
                existing_root = _read_store_jsonl(root_pending_path) or ()
                missing_existing = [
                    c for c in existing_root
                    if isinstance(c, dict)
                    and c.get("id")
                    and c["id"] not in by_id
                    and _card_producer_family(c) == "transcript"
                    and not c.get("repeat_of")
                    and c.get("type") != "Context"
                    and not detect(_card_strings(c), (), cfg.seat)
                ]
                if missing_existing:
                    uninspected_pdirs = _uninspected_project_stores(root_store, list(recent_pdirs) + [cfg.store])
                    if uninspected_pdirs:
                        uninspected_by_id = {}
                        for updir in uninspected_pdirs:
                            up_ex_fn, up_ok = _store_excluded_fn_or_skip(updir)
                            if not up_ok:
                                continue
                            for uc in _read_store_jsonl(os.path.join(updir, CARDS)) or ():
                                if (
                                    isinstance(uc, dict)
                                    and uc.get("id")
                                    and not uc.get("repeat_of")
                                    and uc.get("type") != "Context"
                                    and not _card_matches_exclusion(uc, up_ex_fn)
                                    and not detect(_card_strings(uc), (), cfg.seat)
                                ):
                                    uninspected_by_id[uc["id"]] = uc
                        for old_c in missing_existing:
                            oid = old_c["id"]
                            if oid in uninspected_by_id:
                                by_id[oid] = uninspected_by_id[oid]
            merged = sorted(by_id.values(), key=lambda c: (c.get("time") or "", c.get("id") or ""))
            write_pending_spool(
                root_store, merged, replace=True, preserve_external=True, producer=("transcript", cfg.seat)
            )
    return spooled


def has_cards(path):
    """Whether the store's card file holds a card. It is read only as a regular file of this user's."""
    try:
        fd, _ = _open_regular(path)
    except (OSError, Unsafe):
        return False
    with os.fdopen(fd, "rb") as f:
        return any(line.strip() for line in f)


def one_pass(cfg):
    """Load and write once: (status, detail)."""
    if not cfg.project_dir or not os.path.isdir(cfg.project_dir):
        return "skipped", "no project folder to read"
    limit = "%g s" % cfg.stage_timeout
    lock_fd = getattr(cfg, "lock_fd", None)
    names = list(getattr(cfg, "seat_files", None) or ())
    mode = getattr(cfg, "seat_file_mode", None)
    emit_ctx = bool(getattr(cfg, "emit_context", False))
    eff_mode = _effective_write_mode(cfg)
    if emit_ctx and eff_mode == "spool":
        return "failed", "--emit-context cannot be combined with spool write mode; nothing written"
    root_store = getattr(cfg, "root_store", None)
    if names and mode == "each":                  # a flag taken once: one loader stage a transcript
        runs = [["--seat-file=" + n] for n in names]
    elif names and mode == "append":              # a flag taken more than once: one stage, every transcript
        runs = [["--seat-file=" + n for n in names]]
    else:
        runs = [[]]
    seed_backup, seed_err = _seed_project_context_head(root_store, cfg.store) if emit_ctx else (None, None)
    if seed_err is not None:
        return "failed", seed_err
    secs, total, new, report = 0.0, None, 0, None
    for idx, flags in enumerate(runs):
        stage_flags = list(flags) + (["--emit-context"] if emit_ctx and idx == len(runs) - 1 else [])
        stage = argparse.Namespace(**dict(vars(cfg), extra=list(getattr(cfg, "extra", ()) or ()) + stage_flags))
        code, took, out, _ = run_stage(loader_cmd(stage), cfg.stage_timeout, lock_fd=lock_fd)
        secs += took
        if code is None:
            _restore_project_context_seed(cfg.store, seed_backup)
            return "timeout", "loader ran past %s and was stopped; nothing written" % limit
        if code == 3:
            _restore_project_context_seed(cfg.store, seed_backup)
            if os.path.isfile(os.path.join(cfg.store, CARDS)):
                _sync_spool_stores(cfg, root_store, spool_project=(eff_mode == "spool"))
            return "failed", NO_MARKER
        if code != 0:
            _restore_project_context_seed(cfg.store, seed_backup)
            return "failed", "loader exit %d after %.1f s; nothing written" % (code, secs)
        report = _json(out)
        total, new = _count(report, "cards_total"), new + (_count(report, "new_cards") or 0)
    if emit_ctx:
        total = _unseed_foreign_context_card(cfg.store, seed_backup, report, total)
    loaded = "load %.1f s" % secs
    if eff_mode == "spool":
        try:
            _store_excluded_fn(cfg.store)
        except PolicyUnreadable as exc:
            return "failed", "unreadable exclusion policy in cursor.json (%s); nothing written" % exc
        spooled = _sync_spool_stores(cfg, root_store, spool_project=True)
        if spooled is None:
            if emit_ctx:
                _restore_project_context_seed(cfg.store, seed_backup)
            return "failed", "cards.jsonl could not be read for spooling; %s" % loaded
        settle_err = (
            _settle_project_context_head(root_store, cfg.store, cfg.seat, 0, {"outcome": "written"}, seed_backup)
            if emit_ctx else None
        )
        cfg.counts = ({"cards_total": total} if total is not None else {}) | {
            "new_cards": new,
            "spooled": spooled,
            "outcomes": {},
        }
        if settle_err is not None:
            return "failed", "%s (spooled %d cards to pending.jsonl; %s)" % (settle_err, spooled, loaded)
        return "ok", "spooled %d cards to pending.jsonl; %s" % (spooled, loaded)
    if root_store and os.path.isfile(_pending_spool_path(root_store)) and os.path.isfile(os.path.join(cfg.store, CARDS)):
        _sync_spool_stores(cfg, root_store, spool_project=False)
    if not has_cards(os.path.join(cfg.store, CARDS)):
        cfg.counts = {"cards_total": 0 if total is None else total, "new_cards": new, "outcomes": {}}
        return "ok", "0 cards, nothing to write; %s" % loaded
    try:
        _store_excluded_fn(cfg.store)
    except PolicyUnreadable as exc:
        return "failed", "unreadable exclusion policy in cursor.json (%s); nothing written" % exc
    code2, secs2, out2, err2 = run_stage(writer_cmd(cfg), cfg.stage_timeout, cwd=None if cfg.writer else REPO,
                                         lock_fd=lock_fd)
    if code2 is None:
        if emit_ctx:
            _settle_project_context_head(root_store, cfg.store, cfg.seat, None, None, seed_backup)
        cfg.counts = ({"cards_total": total} if total is not None else {}) | {"new_cards": new}
        return "timeout", "write ran past %s and was stopped; the next load goes on; %s" % (limit, loaded)
    answer = _json(out2)
    if emit_ctx:
        refused_ctx, refusal_obj = _context_supersedes_refusal(cfg.store, cfg.seat, answer)
        if refused_ctx is not None:
            rec_backup = _recover_server_context_head(cfg, refused_ctx, refusal_obj, lock_fd=lock_fd)
            if rec_backup is not None:
                retry_flags = list(runs[-1]) + ["--emit-context"]
                retry_stage = argparse.Namespace(**dict(vars(cfg),
                                                        extra=list(getattr(cfg, "extra", ()) or ()) + retry_flags))
                rcode, rtook, rout, _ = run_stage(loader_cmd(retry_stage), cfg.stage_timeout, lock_fd=lock_fd)
                secs += rtook
                loaded = "load %.1f s" % secs
                if rcode is None:
                    _restore_project_context_seed(cfg.store, rec_backup)
                    _settle_project_context_head(root_store, cfg.store, cfg.seat, code2, answer, seed_backup)
                    return "timeout", "loader ran past %s and was stopped; nothing written" % limit
                if rcode == 3:
                    _restore_project_context_seed(cfg.store, rec_backup)
                    _settle_project_context_head(root_store, cfg.store, cfg.seat, code2, answer, seed_backup)
                    return "failed", NO_MARKER
                if rcode != 0:
                    _restore_project_context_seed(cfg.store, rec_backup)
                    _settle_project_context_head(root_store, cfg.store, cfg.seat, code2, answer, seed_backup)
                    return "failed", "loader exit %d after %.1f s; nothing written" % (rcode, secs)
                seed_backup = rec_backup
                report = _json(rout)
                total = _count(report, "cards_total")
                total = _unseed_foreign_context_card(cfg.store, seed_backup, report, total)
                if not has_cards(os.path.join(cfg.store, CARDS)):
                    cfg.counts = {"cards_total": 0 if total is None else total, "new_cards": new, "outcomes": {}}
                    return "ok", "0 cards, nothing to write; %s" % loaded
                code2, rsecs2, out2, err2 = run_stage(writer_cmd(cfg), cfg.stage_timeout,
                                                      cwd=None if cfg.writer else REPO, lock_fd=lock_fd)
                secs2 += rsecs2
                if code2 is None:
                    _settle_project_context_head(root_store, cfg.store, cfg.seat, None, None, seed_backup)
                    cfg.counts = ({"cards_total": total} if total is not None else {}) | {"new_cards": new}
                    return "timeout", "write ran past %s and was stopped; the next load goes on; %s" % (limit, loaded)
                answer = _json(out2)
    settle_err = (
        _settle_project_context_head(root_store, cfg.store, cfg.seat, code2, answer, seed_backup)
        if emit_ctx else None
    )
    counts = outcome_counts(answer)
    cfg.counts = ({"cards_total": total} if total is not None else {}) | {"new_cards": new, "outcomes": counts}
    said = ", ".join("%s %d" % kv for kv in counts.items()) or "no counts"     # known names, in OUTCOMES order
    of = " of %d cards" % total if total is not None else ""
    fresh = " (%d new)" % new if new else ""
    timing = "%s, write %.1f s" % (loaded, secs2)
    if code2 == 0:
        if settle_err is not None:
            return "failed", "%s (%s%s%s; %s)" % (settle_err, said, of, fresh, timing)
        return "ok", "%s%s%s; %s" % (said, of, fresh, timing)
    if code2 == 3:
        return "held", "a detector held a card: %s%s; %s" % (said, of, timing)
    if code2 == 2 and not refused_before_sending(err2):
        return "unreachable", "write exit 2, server unreachable; %s" % timing
    if code2 == 2:
        return "failed", "write exit 2, refused before sending; %s" % timing
    return "failed", "write exit %d: %s%s; %s" % (code2, said, of, timing)


# ---- the worker

SEAT_LAUNCHER_NAMES = {"memory-bxxxxx": ("bxxxxx",), "exxxx-session": ("exxxx",)}


def _transcript_has_foreign_marker(project, name, seat, extra_markers=(), extra_own=()):
    """True when `project/name` holds no session-start marker of this seat and holds another seat's
    `echo <other>-shell-ok` marker in a user record. Real prompts decide when any of them carries a marker; a
    compaction summary or meta record counts only when none does (transcript_cards._foreign_by_precedence, the
    loader's own rule, imported so the two scans cannot drift)."""
    if not project or not name or not seat:
        return False
    seat_norm = seat.strip().lower()
    own = {seat_norm} | set(SEAT_LAUNCHER_NAMES.get(seat_norm, ()))
    for item in extra_own or ():
        if isinstance(item, str) and item.strip():
            own.add(item.strip().lower())
    for m in extra_markers or ():
        if isinstance(m, str):
            hit = _SHELL_OK_MARKER_RE.search(m)
            if hit:
                own.add(hit.group(1).lower())
    seen = [False, False, False, False]    # real own, real foreign, summary own, summary foreign
    try:
        fd, _ = _open_regular(os.path.join(project, name))
    except (OSError, Unsafe):
        return False
    try:
        with os.fdopen(fd, "rb") as f:
            for line in f:
                if b"-shell-ok" not in line.lower():
                    continue
                try:
                    o = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                base, hits = _marker_names_in_record(o)
                if base is None:
                    continue
                for hit in hits:
                    seen[base + (0 if hit.group(1).lower() in own else 1)] = True
    except OSError:
        return False
    return _foreign_by_precedence(seen)


def run_pass(cfg, project, lock_fd, extra=(), transcripts=(), note=""):
    """One project folder's pass, in that folder's own loader store (project_store): the loader's --out and the
    writer's --file are there, so a rebuild or another folder's file of the same name touches no other project.
    Everything but the folder is this worker's own: the seat, the server, the state file, the markers, the programs
    and their time limit. The receipt goes to <root>/results/, the seat's summary to <root>."""
    store = None
    if project:
        store, why = project_store(cfg.store, cfg.seat, project)
        if why:
            return write_result(cfg.store, cfg.seat, project, "failed", "store refused: %s; not loaded" % why)
    loader_path = cfg.loader or os.path.join(HERE, "transcript_cards.py")
    raw_names = [t for t in transcripts if t]
    names = [t for t in raw_names
             if not _transcript_has_foreign_marker(project, t, cfg.seat, cfg.marker, cfg.own_name)]
    skipped_foreign = len(raw_names) - len(names)
    mode = seat_file_mode(loader_path) if names else None
    if names and mode is None:
        note += "; --seat-file not passed (the loader has no such flag)"
    want_ctx = bool(getattr(cfg, "emit_context", False))
    eff_mode = _effective_write_mode(cfg)
    if want_ctx and eff_mode == "spool":
        return write_result(
            cfg.store, cfg.seat, project, "failed",
            "--emit-context cannot be combined with spool write mode; nothing written" + note,
        )
    takes_ctx = loader_takes_emit_context(loader_path) if want_ctx else False
    if want_ctx and not takes_ctx:
        note += "; --emit-context not passed (the loader has no such flag)"
    pass_cfg = argparse.Namespace(store=store, root_store=cfg.store, stage_timeout=cfg.stage_timeout,
                                  loader=cfg.loader, writer=cfg.writer, lock_fd=lock_fd, seat=cfg.seat,
                                  base_url=cfg.base_url, state_file=cfg.state_file, marker=cfg.marker,
                                  own_name=cfg.own_name, project_dir=project, extra=list(extra),
                                  seat_files=names if mode else [], seat_file_mode=mode, emit_context=takes_ctx,
                                  write_mode=eff_mode)
    known = knew_seat(store, read_json(result_path(cfg.store, request_key(cfg.seat, project))))
    try:
        status, detail = one_pass(pass_cfg)
    except Exception as exc:                      # a stage that cannot even start
        status, detail = "failed", "load could not run (%s)" % type(exc).__name__
    if skipped_foreign and detail != NO_MARKER:
        note += "; --seat-file not passed (foreign seat marker)"
    if detail == NO_MARKER and known:
        note += KNEW_SEAT
    known = known or bool(store and seat_history(store) == "held")
    return write_result(cfg.store, cfg.seat, project, status, detail + note, knew_seat=known,
                        counts=getattr(pass_cfg, "counts", None))


def loader_writing(cfg, project):
    """another_loader_writing for the loader store of `project` (None for no folder, which has none)."""
    if not project:
        return None
    return another_loader_writing(os.path.join(cfg.store, PROJECTS, request_key(cfg.seat, project)))


def not_loaded(cfg, project, evidence):
    """The receipt of a pass that did not start because another loader is writing the project's store."""
    return write_result(cfg.store, cfg.seat, project, "failed",
                        "another loader is writing this store (%s); not loaded" % evidence)


def drain(cfg, lock_fd, budget):
    """Take the queued requests while the budget lasts: (passes, the last receipt, stalled). A pass is one project
    folder: the oldest request, and every other request queued for the same folder (up to MAX_GROUP), so all the
    transcripts named for that folder go to the loader in that one pass. Each request is claimed and read again; one
    that is not well formed, or whose transcript is not a regular file of this user's, is set aside and the others
    go on. Every claimed request is removed only after the pass; anything that goes wrong inside the pass sets the
    claimed requests aside and the next folder is taken. A store another loader is writing stops the draining,
    the request put back; that, or a queue none of whose entries can be claimed, is a stall: the requests left are
    not going anywhere soon, so the worker does not go round again for them."""
    passes, last, looked = 0, None, 0
    while passes < budget and looked < budget + MAX_SCAN:
        entries = queued(cfg.store)
        if not entries:
            break
        looked += 1
        claimed, head = None, None
        for path, _ in entries:
            claimed = claim(path)
            if claimed is not None:
                head = path
                break
        if claimed is None:
            return passes, last, True
        group = [claimed]
        try:
            kind, req = read_request(claimed)
            if kind != "ok":
                quarantine(claimed)
                continue
            project = req["project_dir"]
            members = [(claimed, req)]
            for path, other in entries:            # the same folder's other requests, into this one pass
                if len(members) >= MAX_GROUP:
                    break
                if path == head or other.get("project_dir") != project:
                    continue
                taken = claim(path)
                if taken is None:
                    continue
                group.append(taken)
                kind, fresh = read_request(taken)
                if kind == "ok" and fresh.get("project_dir") == project:
                    members.append((taken, fresh))
                elif kind == "ok":                  # rewritten for another folder meanwhile: its own pass later
                    group.remove(taken)
                    unclaim(taken)
                else:
                    group.remove(taken)
                    quarantine(taken)
            names, note = [], ""
            for taken, member in members:
                name = member.get("transcript")
                if not name or name in names:
                    continue
                problem = transcript_problem(project, name)
                if problem == TRANSCRIPT_MISSING:   # not written yet: the folder is loaded, the name is not passed
                    note = "; --seat-file not passed (a transcript is not there yet)"
                elif problem:                       # a link, a pipe, a folder, another user's: not a request to act on
                    group.remove(taken)
                    quarantine(taken)
                else:
                    names.append(name)
            if not group:
                continue
            evidence = loader_writing(cfg, project)
            if evidence:
                last = not_loaded(cfg, project, evidence) or last
                for taken in group:
                    unclaim(taken)
                return passes, last, True
            last = run_pass(cfg, project, lock_fd, transcripts=names, note=note) or last
            for taken in group:
                _unlink(taken)
        except Exception:
            for taken in group:
                if os.path.lexists(taken):
                    quarantine(taken)
            continue
        passes += 1
    return passes, last, False


def run_worker(cfg, event):
    """A hook's worker: store check, throttle, queue, lock, passes, hand-off. The last receipt it wrote, or None.
    A successor (cfg.successor) has no request of its own: it takes what is queued, and on an empty queue it ends
    at once."""
    unresolved = resolve(cfg)
    store = cfg.store
    why, store_sound = secure_store(store)
    if unresolved and not why:
        refuse_store(store, cfg.seat or cfg.identity, unresolved, "not loaded")
        return None
    if why:
        if store_sound:
            refuse_store(store, cfg.seat, why)
        return None
    project = canonical(cfg.project_dir)
    own = not getattr(cfg, "successor", False) and not (
        event in THROTTLED and throttled(read_json(result_path(store, request_key(cfg.seat, project))), time.time()))
    waiting = False
    if own and event not in NO_QUEUE:
        waiting = enqueue(store, cfg)               # asked first, the lock looked at second
    elif not own and not has_pending(store):
        return None
    passes, last, stalled, idle_rounds = 0, None, False, 0
    while True:
        fd = take_lock(store)
        if fd is None:
            return last                             # the load that holds it hands on what it leaves
        try:
            if own and not waiting:
                waiting = enqueue(store, cfg)
            done, receipt, stalled = drain(cfg, fd, MAX_PASSES - passes)
        finally:
            release_lock(fd)
        passes, last = passes + done, receipt or last
        # Looked at AFTER the release: a hook's worker queues first and takes the lock second, so a request is
        # either seen here or found the lock free and has its own worker.
        if not has_pending(store):
            return last
        idle_rounds += done == 0
        stalled = stalled or idle_rounds > 1        # queued, yet nothing to take twice running: held up
        if stalled or passes >= MAX_PASSES:
            break                                   # what is left is handed on below
        # Not stalled, budget left: a request arrived between the last scan and the release. Round again.
    hand_off(cfg, passes, stalled)
    return last


def hand_off(cfg, passes, stalled):
    """Called with the lock released and requests left: start one successor, so the queue is always drained by
    someone after the hooks that asked have gone (a session that ended has no worker left). After a full budget it
    starts at once; after a stall it waits SUCCESSOR_WAIT_S first, the time another loader's evidence takes to go
    stale. A successor that made no progress starts none: one successor an exiting worker, never a chain of idle
    ones."""
    if getattr(cfg, "successor", False) and passes == 0:
        return None
    return start_successor(cfg, SUCCESSOR_WAIT_S if stalled else 0)


def successor_argv(cfg, wait):
    """The successor's command: this worker's own trusted arguments, no request and no project folder."""
    return [sys.executable, SELF, "--run", "--successor", "--after=%r" % float(wait)] + _trusted_args(cfg)


def start_successor(cfg, wait):
    """Start the successor detached, exactly as a hook starts a worker (spawn), and do not wait for it."""
    return subprocess.Popen(successor_argv(cfg, wait), cwd=HERE, env=_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                            **detach_kwargs(WINDOWS))


def run_manual(cfg):
    """A load by hand (`--run` with no --event): the same pass under the same lock, waited for up to cfg.wait, with
    --rebuild and --all-files passed to the loader for this pass only; then what is queued. (receipt, None) or
    (None, why nothing ran)."""
    unresolved = resolve(cfg)
    store = cfg.store
    why, store_sound = secure_store(store)
    if unresolved and not why:
        refuse_store(store, cfg.seat or cfg.identity, unresolved, "not loaded")
        return None, "not loaded: %s" % unresolved
    if why:
        if store_sound:
            refuse_store(store, cfg.seat, why)
        return None, "store refused: %s" % why
    deadline = time.monotonic() + cfg.wait
    fd = take_lock(store)
    while fd is None and time.monotonic() < deadline:
        time.sleep(0.25)
        fd = take_lock(store)
    if fd is None:
        return None, "the store's lock was not free within %g s; nothing run" % cfg.wait
    done, stalled = 0, False
    try:
        extra = (["--rebuild"] if cfg.rebuild else []) + (["--all-files"] if cfg.all_files else [])
        project = canonical(cfg.project_dir)
        evidence = loader_writing(cfg, project)
        transcript = plain_transcript(getattr(cfg, "transcript", None))
        if evidence:
            receipt, stalled = not_loaded(cfg, project, evidence), True
        else:
            receipt = run_pass(cfg, project, fd, extra,
                               [transcript] if transcript and not transcript_problem(project, transcript) else [])
            done, _, stalled = drain(cfg, fd, MAX_PASSES)
            done += 1
    finally:
        release_lock(fd)
    if has_pending(store):                         # looked at after the release, as a worker does
        hand_off(cfg, done, stalled)
    return receipt, (None if receipt else "the receipt could not be written")


# ---- the hook

def detach_kwargs(windows):
    """Popen arguments that start the worker on its own: it outlives the hook, and the hook does not wait for it."""
    if windows:
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _trusted_args(cfg):
    """The arguments a worker passes on: the pane's identity, and the seat's own values as far as they are known (a
    hook passes what it was given; a worker passes what it resolved)."""
    argv = ["--identity=" + cfg.identity]
    argv += ["--%s=%s" % (flag, getattr(cfg, attr)) for flag, attr in
             (("seat", "seat"), ("store", "store"), ("base-url", "base_url"), ("state-file", "state_file"))
             if getattr(cfg, attr)]
    argv += ["--stage-timeout=%r" % cfg.stage_timeout]
    argv += ["--marker=" + m for m in cfg.marker or ()]
    argv += ["--own-name=" + n for n in cfg.own_name or ()]
    if getattr(cfg, "emit_context", False):
        argv.append("--emit-context")
    if getattr(cfg, "write_mode", None) in ("direct", "spool"):
        argv.append("--write-mode=" + cfg.write_mode)
    elif _effective_write_mode(cfg) == "spool":
        argv.append("--write-mode=spool")
    if cfg.loader:
        argv.append("--loader=" + cfg.loader)
    if cfg.writer:
        argv.append("--writer=" + cfg.writer)
    return argv


def child_argv(cfg, event):
    argv = [sys.executable, SELF, "--run", "--event=" + event] + _trusted_args(cfg)
    if cfg.project_dir:
        argv.append("--project-dir=" + cfg.project_dir)
    if plain_transcript(getattr(cfg, "transcript", None)):
        argv.append("--transcript=" + cfg.transcript)
    return argv


def spawn(cfg, event):
    """Start the worker detached. Its stdin, stdout and stderr are the null device: a child that held the hook's
    stdout would keep Claude Code waiting for the hook to end. Its folder is this script's, not the store's."""
    return subprocess.Popen(child_argv(cfg, event), cwd=HERE, env=_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                            **detach_kwargs(WINDOWS))


def hook(cfg, event):
    """The foreground: start the worker, and say so. It reads and writes nothing under the store."""
    if event not in EVENTS:
        return None
    if getattr(cfg, "emit_context", False) and _effective_write_mode(cfg) == "spool":
        return "card load: not started, bad arguments (--emit-context cannot be combined with spool write mode)"
    if cfg.inline:
        run_worker(cfg, event)
    else:
        spawn(cfg, event)
    if not cfg.project_dir:
        return "card load: started, with no project folder named (give --project-dir)"
    return "card load: started"


def recent_cmd(cfg):
    head = [sys.executable, cfg.recent] if cfg.recent else [sys.executable, "-m", "tools.meshy_node"]
    return head + ["card", "recent", "--base-url", cfg.base_url, "--state-file", cfg.state_file,
                   "--seat", cfg.seat, "--load-result", os.path.join(cfg.store, RECEIPT),
                   "--deadline", "%g" % BRIEF_RECENT_DEADLINE_S]


def _brief_left(deadline):
    return max(BRIEF_CLEANUP_FLOOR_S, deadline - time.monotonic())


def _brief_timeout():
    return "decision cards: not available (card recent did not finish within %g s)\n" % BRIEF_TIMEOUT_S


def _stop_recent(p):
    # Kill the group even if its leader has exited: descendants can still hold stdout open.
    p.poll()                                       # reap an exited leader before signaling its group
    try:
        if WINDOWS:
            p.kill()
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except PermissionError:
        # macOS can refuse SIGKILL to a group containing only an unreaped zombie. The leader may
        # have exited between poll and killpg; reap it and retry, still targeting any descendants.
        if WINDOWS or p.poll() is None:
            raise
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


def _brief_text(raw):
    text = raw.decode("utf-8", "replace")
    lines = []
    prefix = "decision cards: not available ("
    for line in text.splitlines(keepends=True):
        if line.startswith(prefix):
            kind = re.match(r"[A-Za-z_][A-Za-z0-9_.]*", line[len(prefix):])
            ending = "\n" if line.endswith("\n") else ""
            line = "decision cards: not available (%s; run `card recent` by hand to see why)%s" % (
                kind.group() if kind else "unknown", ending)
        lines.append(line)
    # Replacement characters and sanitized lines can expand the bytes read from the pipe.
    # Ignore only an incomplete final UTF-8 character after cutting the PRINTED byte sequence.
    return "".join(lines).encode("utf-8")[:BRIEF_MAX_BYTES].decode("utf-8", "ignore")


def brief_output(cfg, deadline):
    """Called only by the briefing worker: start, read and clean up within its shared deadline.
    Nonblocking pipe reads work on POSIX and, since Python 3.12, on Windows (the project runs 3.13)."""
    # Leave time to kill/reap before main reaches its join deadline and the CLI calls os._exit(0).
    read_deadline = deadline - min(BRIEF_CLEANUP_FLOOR_S, BRIEF_TIMEOUT_S / 4)
    if time.monotonic() >= read_deadline:
        return _brief_timeout()
    group = {"creationflags": CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
    p = subprocess.Popen(recent_cmd(cfg), cwd=REPO, env=_env(), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, **group)
    out = bytearray()
    try:
        os.set_blocking(p.stdout.fileno(), False)
        while len(out) < BRIEF_MAX_BYTES:
            remaining = read_deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(p.args, BRIEF_TIMEOUT_S)
            chunk = p.stdout.read(min(8192, BRIEF_MAX_BYTES - len(out)))
            if chunk is None:
                time.sleep(min(0.01, remaining))
                continue
            if not chunk:
                break
            out.extend(chunk)
        if len(out) == BRIEF_MAX_BYTES:
            return _brief_text(out)                 # finally stops the whole group immediately
        p.wait(timeout=max(0.0, read_deadline - time.monotonic()))
        if p.returncode:
            return "decision cards: not available (card recent exited %d)\n" % p.returncode
        return _brief_text(out) if out else "decision cards: not available (card recent printed nothing)\n"
    except subprocess.TimeoutExpired:
        return _brief_timeout()
    finally:
        try:
            _stop_recent(p)
            p.wait(timeout=_brief_left(deadline))
        finally:
            p.stdout.close()


def brief(cfg, stdout):
    """The main thread owns the sole write. A stalled worker is a daemon ended by os._exit(0).
    The lock and done flag discard results arriving after the main thread chose its output."""
    deadline = time.monotonic() + BRIEF_TIMEOUT_S
    lock = threading.Lock()
    done = False
    result = None

    def work():
        nonlocal result
        try:
            resolved = copy.copy(cfg)               # the load worker keeps the original arguments
            why = resolve(resolved)
            text = ("decision cards: not available (%s)\n" % why if why
                    else brief_output(resolved, deadline))
        except BaseException as exc:
            text = "decision cards: not available (%s)\n" % type(exc).__name__
        with lock:
            if not done:
                result = text

    try:
        worker = threading.Thread(target=work, name="card-recent-brief", daemon=True)
        worker.start()
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
        with lock:
            done = True
            text = result if result is not None and time.monotonic() < deadline else _brief_timeout()
    except BaseException as exc:
        with lock:
            done = True
        text = "decision cards: not available (%s)\n" % type(exc).__name__
    # The cap holds for every line printed here, not only the child's: a resolve() reason names the pane's labels.
    data = text.encode("utf-8")[:BRIEF_MAX_BYTES].decode("utf-8", "ignore").encode("utf-8")
    text = data.decode("utf-8")
    try:
        buffer = getattr(stdout, "buffer", None)
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:
            stdout.write(text)
            stdout.flush()
    except BaseException:
        pass                                       # a failed write cannot cause a second write


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Usage(message)


def parser():
    ap = _Parser(description="Start this seat's card load from a Claude Code hook, or load by hand with --run "
                             "(always exits 0).")
    ap.add_argument("--event", choices=EVENTS, help="the hook event; default: hook_event_name of the hook input")
    ap.add_argument("--run", action="store_true",
                    help="load now in this process: a hook's worker with --event, a load by hand without it")
    ap.add_argument("--inline", action="store_true", help="run the worker inside the hook instead of detached")
    ap.add_argument("--no-brief", action="store_true", help="SessionStart: start the load without a briefing")
    ap.add_argument("--brief-only", action="store_true", help="SessionStart: print the briefing without a load")
    ap.add_argument("--identity", help="this pane's identities, comma-separated as in MESHY_IDENTITY (the default). "
                                         "With neither, the hook does nothing")
    ap.add_argument("--seat", help="the seat's roster label; default: the serving entry's seat, else the first label")
    ap.add_argument("--store", help="default: <MESHY_CARDS_ROOT or ~/.meshychat/cards>/<seat>; outside any git tree")
    ap.add_argument("--base-url", help="default: the identity's server (postbox config)")
    ap.add_argument("--state-file", help="default: the identity's state file (postbox config)")
    ap.add_argument("--transcript", help="the session's transcript file name (default: from the hook input), "
                                         "passed to the loader as --seat-file when it has that flag")
    ap.add_argument("--project-dir", help="default: the folder of the hook input's transcript_path")
    ap.add_argument("--marker", action="append", help="passed to the loader (repeatable)")
    ap.add_argument("--own-name", action="append", help="passed to the loader (repeatable)")
    ap.add_argument("--emit-context", action="store_true",
                    help="passed to the loader when it has that flag (#297)")
    ap.add_argument("--write-mode", choices=("direct", "spool"), default=None,
                    help="stage 2 mode: 'direct' runs `card write`, 'spool' writes pending.jsonl atomically "
                         "(default: 'spool' when MESHY_CARDS_WRITE=spool or WRITE=spool, else 'direct')")
    ap.add_argument("--stage-timeout", type=float, default=None,
                    help="seconds for each stage (default %g for a hook, %g by hand)"
                         % (STAGE_TIMEOUT_S, MANUAL_STAGE_TIMEOUT_S))
    ap.add_argument("--rebuild", action="store_true", help="by hand only: the loader's --rebuild, for this run")
    ap.add_argument("--all-files", action="store_true", help="by hand only: the loader's --all-files, for this run")
    ap.add_argument("--wait", type=float, default=LOCK_WAIT_S, help="by hand only: seconds to wait for the lock")
    ap.add_argument("--successor", action="store_true",
                    help="internal: a worker started by a worker to drain what it left (with --run)")
    ap.add_argument("--after", type=float, default=0.0, help="internal: seconds a successor waits first")
    ap.add_argument("--loader", help="another loader script (tests)")
    ap.add_argument("--writer", help="a script run in place of `python -m tools.meshy_node` (tests)")
    ap.add_argument("--recent", help="a script run in place of `python -m tools.meshy_node` for card recent (tests)")
    return ap


def parse(argv):
    """The arguments, checked: --rebuild and --all-files are for a load by hand only (a hook that rebuilt the store
    at every prompt would never finish one)."""
    cfg = parser().parse_args(argv)
    if cfg.successor and (not cfg.run or cfg.event is not None):
        raise _Usage("--successor is a worker's own: --run with no --event")
    manual = cfg.run and cfg.event is None and not cfg.successor
    if (cfg.rebuild or cfg.all_files) and not manual:
        raise _Usage("--rebuild and --all-files are for a load by hand (--run with no --event)")
    if cfg.stage_timeout is None:
        cfg.stage_timeout = MANUAL_STAGE_TIMEOUT_S if manual else STAGE_TIMEOUT_S
    return cfg


def _hook_input(stdin, wait=None):
    """The hook's JSON object on stdin, or {} for anything else: at most STDIN_LIMIT bytes, waited for at most
    STDIN_WAIT_S (a stdin nobody closes cannot hold the hook). A terminal is not read. The bytes are read as UTF-8,
    which Claude Code writes, whatever the console's code page (cp1252 on a Windows seat)."""
    try:
        if stdin is None or stdin.isatty():
            return {}
    except Exception:
        return {}
    box = []

    def read():
        try:
            box.append(stdin.buffer.read(STDIN_LIMIT) if hasattr(stdin, "buffer") else stdin.read(STDIN_LIMIT))
        except Exception:
            pass

    reader = threading.Thread(target=read, name="card-load-stdin", daemon=True)
    reader.start()
    reader.join(STDIN_WAIT_S if wait is None else wait)
    if not box:
        return {}
    raw = box[0].decode("utf-8", "replace") if isinstance(box[0], bytes) else box[0]
    try:
        obj = json.loads(raw or "{}")
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _event_named(argv, given):
    for i, a in enumerate(argv):
        if a == "--event" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--event="):
            return a.split("=", 1)[1]
    return given.get("hook_event_name") if isinstance(given.get("hook_event_name"), str) else None


def _say(stdout, line):
    try:
        stdout.write(line + "\n")
        stdout.flush()
    except Exception:
        pass


def main(argv=None, stdin=None, stdout=None):
    """Always 0. SessionStart prints the briefing; PreCompact and SessionEnd a load line; --run its receipt."""
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = sys.stdout if stdout is None else stdout
    event = None
    cfg = None
    try:
        given = {} if "--run" in argv else _hook_input(sys.stdin if stdin is None else stdin)
        event = _event_named(argv, given)
        try:
            cfg = parse(argv)
        except _Usage as exc:
            if event in SPEAKS or ("--run" in argv and event is None):
                _say(stdout, "card load: not started, bad arguments (%s)" % str(exc)[:120])
            elif "SessionStart" in (event, given.get("hook_event_name")) and "--run" not in argv \
                    and "--no-brief" not in argv and (
                    pane_labels(os.environ.get("MESHY_IDENTITY"))
                    or any(a == "--identity" or a.startswith("--identity=") for a in argv)):
                # A broken SessionStart line costs the seat its load and its briefing: say so in the briefing's own
                # failure line, so the deployment error is seen. A pane with no identity still gets nothing.
                _say(stdout, "decision cards: not available (bad arguments: %s)" % str(exc)[:120])
            return 0
        labels = pane_labels(cfg.identity if cfg.identity is not None else os.environ.get("MESHY_IDENTITY"))
        cfg.identity = ",".join(labels) or None
        if cfg.identity is None:
            if cfg.run and cfg.event is None and not cfg.successor:
                _say(stdout, "card load: no identity (MESHY_IDENTITY is unset and --identity not given); nothing run")
            return 0                                # a pane with no identity is not a seat's: nothing at all
        cfg.store = os.path.abspath(os.path.expanduser(cfg.store)) if cfg.store else None
        if cfg.run:
            cfg.project_dir = os.path.abspath(os.path.expanduser(cfg.project_dir)) if cfg.project_dir else None
            if cfg.successor:
                time.sleep(max(0.0, min(cfg.after, SUCCESSOR_WAIT_S)))
                receipt = run_worker(cfg, None)
                _say(stdout, json.dumps(receipt, sort_keys=True) if receipt else "card load: no pass ran here")
            elif cfg.event is None:
                receipt, why = run_manual(cfg)
                _say(stdout, json.dumps(receipt, sort_keys=True) if receipt
                     else "card load: " + (why or "the receipt could not be written"))
            else:
                receipt = run_worker(cfg, cfg.event)
                _say(stdout, json.dumps(receipt, sort_keys=True) if receipt else "card load: no pass ran here")
            return 0
        event = cfg.event or event
        if cfg.project_dir is None and isinstance(given.get("transcript_path"), str) and given["transcript_path"]:
            cfg.project_dir = os.path.dirname(os.path.expanduser(given["transcript_path"]))
        if cfg.transcript is None and isinstance(given.get("transcript_path"), str):
            cfg.transcript = plain_transcript(os.path.basename(given["transcript_path"]))
        cfg.project_dir = os.path.abspath(os.path.expanduser(cfg.project_dir)) if cfg.project_dir else None
        if event == "SessionStart" and cfg.no_brief and cfg.brief_only:
            return 0
        if event == "SessionStart" and not cfg.no_brief:
            # The load and the briefing are independent: a load that could not be started does not cost the
            # session its briefing (brief() never raises).
            if not cfg.brief_only:
                try:
                    hook(cfg, event)
                except BaseException:                   # noqa: BLE001 - SessionStart prints no load line
                    pass
            brief(cfg, stdout)
            return 0
        line = hook(cfg, event)
        if line and event in SPEAKS:
            _say(stdout, line)
    except BaseException as exc:            # a hook never fails its session, whatever went wrong
        if event in SPEAKS:
            _say(stdout, "card load: not started (%s)" % type(exc).__name__)
        elif event == "SessionStart" and "--run" not in argv and not getattr(cfg, "no_brief", False):
            _say(stdout, "decision cards: not available (%s)" % type(exc).__name__)
    return 0


if __name__ == "__main__":
    main()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except BaseException:
        pass
    os._exit(0)          # a stream that cannot be flushed at exit would turn exit 0 into 120
