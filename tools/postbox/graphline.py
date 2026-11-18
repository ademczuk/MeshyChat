"""graphline: one line of message-graph context in a MeshyChat wake notice.

The server keeps a typed message graph (GET /v1/mesh/neighbours: what a message replies to, its
thread and that thread's size, the replies it already has), and seats almost never ask for it: 9
neighbours calls in a week against 27,337 inbox reads (2026-10-03). So the three renderers
(postbox.codex_notice, meshy_channel.message_event, wait_next) put one line of it in front of the
seat when a message arrives, e.g.

    graph (routing only, read the message for its content): msg/A replies to YOUR msg/P; thread ops/x has 7 message(s).

ROUTING ONLY. The line carries keys, labels, thread ids and counts, never message text or the
listener's unverified preview: a peer's text reaches the agent only through a signed read. A
sender's `done_when` is message text, so the line says only that the ask names one, and the
cache keeps a yes or no, never the words (memory-bxxxxx's ruling, 2026-10-04). Every
string in it comes from the server's graph and is cut to 120 characters with control and format
characters removed, because a thread id and an in-reply-to key are chosen by the sender and must
not be able to start a line of their own in the notice.

A HELP, NEVER A GATE. `line_for` never raises. A timeout, a non-zero exit, unparseable output or a
missing field gives no line for that key, and the notice goes out exactly as it did without this
module. After a failure the identity makes no further lookups for FAIL_BACKOFF_S, so a server that
does not answer costs a notice at most one CALL_TIMEOUT_S wait per minute instead of one per key.

ONE CALL PER KEY. Answers are kept in <log>.graph.json (the newest CACHE_MAX keys, replaced
atomically), shared by the three renderers, so the Codex path's two renders of one batch
(delivery_bank.collect renders to size it) and a channel and a waiter on one identity cost one
neighbours call between them. A failed lookup is not kept. A kept answer is used for
FACTS_TTL_S and then asked again: a thread grows and a message gets answered, and until
2026-10-04 a cached answer was used with no age test at all, so a later render of the same key
kept saying what was true at the first one. An answer the server marked as not projected yet
is kept only UNINDEXED_TTL_S, because its thread size is partial by its own account. A render
after the TTL costs what the first one did, one bounded call; a refresh that fails gives no
line, never the stale one. An entry dated further from now than its TTL in EITHER direction is
stale: a clock that stepped back a second must not empty the cache, and one that stepped back a
day must not make an entry immortal.

WHO IS "YOU". A parent reads YOUR, and an ask reads "expects a reply from you", when the label
the server stamped is one of this seat's. The server answers for a peer under its roster
label, its peers.name and `peer-<id8>` (apps/mesh/views.py labels_for), and the config knows
only the identity name and a hand-set `address`, so until 2026-10-04 both clauses stayed
silent for a seat whose roster label is neither. The daemon now writes the `labels` of each
inbox answer to <log>.labels.json (`save_server_labels`, called from postbox.reconcile), and
`own_labels` adds them. The file names the state file and server it was written for and is
ignored for any other, so a log path reused by another identity does not lend it labels.
Labels are compared EXACTLY, as the server compares them (apps/mesh/graph.py, dst = ANY):
folding case or cutting a long label could make another seat's label read as this one's, so a
label that cleaning would change, or one as long as the TEXT_MAX cut, is never an own label.

COUNTED. Until 2026-10-05 a failed lookup left no trace, so a seat could not tell a lookup that
keeps failing from a message the graph has not indexed yet, or from a seat with no mail (uxxx,
graph-adoption finding 9). Each render now adds to per-identity counters in
<log>.graph-stats.json: `renders` (calls with at least one key to look up), `lines` (calls that
returned a line), `ok`, `pending` and `failed` (fresh neighbours calls, by outcome), `cached` (keys
answered from the cache) and `backed_off` (keys skipped while the identity backs off), plus
`last_error` and `last_error_at` for the newest failure. The error is a short reason (an exit code
and the server's HTTP status, "timeout", an exception's class name), never message text. The file
is replaced atomically like the cache, and a failure is still never written to the cache itself.
The three renderers are separate processes, so two renders landing at the same instant can lose
one increment: the counters are for health, not accounting. The daemon copies them into
status.json (`graph_line_stats`) at each reconcile, so `postbox.py --status` shows them.

NOT INDEXED YET. The graph is a projection the server builds from the log every 60 s
(apps/jobs/registry.py, mesh_edges_catch_up), and a live delivery usually arrives before the
projection has seen the message. Every message a client sends has a thread, so an anchor with no
thread is one the projection has not reached: its replies_to and thread would read as empty, and
"names no parent" would be false. Such a key gets no line, and the answer is kept for only
PENDING_TTL_S so a later render asks again.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import postbox_os  # noqa: E402  (NO_WINDOW, the same creation flag every postbox subprocess uses)

PREFIX = "graph (routing only, read the message for its content): "
MAX_KEYS = 3
MAX_RELATED = 3
CALL_TIMEOUT_S = 6
FAIL_BACKOFF_S = 60
PENDING_TTL_S = 30
FACTS_TTL_S = 600
UNINDEXED_TTL_S = PENDING_TTL_S
LABELS_MAX = 16
CACHE_MAX = 500
TEXT_MAX = 120
ID_MAX = 512
ERROR_MAX = 160
STDOUT_MAX = 1_000_000
MAX_LABELS_BYTES = 65536
MAX_GRAPH_CACHE_BYTES = postbox_os.MAX_CACHE_BYTES
MAX_STATS_BYTES = 65536
#: The counters in <log>.graph-stats.json, each a non-negative int (see COUNTED above).
STAT_FIELDS = ("renders", "lines", "ok", "pending", "failed", "cached", "backed_off")

#: Per log path: the time before which this process makes no lookups for that identity, set by a
#: failed call. In memory only: a failure is never written to the cache file.
_backoff_until = {}


def _labels_path(ident):
    return ident["log"] + ".labels.json"


def _own_label(value):
    """A label that may decide ownership: a non-empty string that cleaning leaves unchanged and
    that is shorter than the TEXT_MAX cut. The facts hold CLEANED, CUT strings, so a label cleaning
    would alter, or one long enough to equal another label's cut form, could match a label that
    is not this seat's."""
    return isinstance(value, str) and value != "" and len(value) < TEXT_MAX and clean(value) == value


def save_server_labels(ident, labels, base_url=None):
    """Keep the labels the server answers to for THIS identity, from its own inbox answer (the
    `labels` list of GET /v1/mesh/inbox: roster label, peers.name, `peer-<id8>`). Called by the
    daemon after each reconcile; the channel and the waiter are other processes and read the
    file. The record names the state file and server the answer came from. Never raises, and an
    answer with no usable label leaves the file as it was."""
    tmp = None
    try:
        names = []
        for item in labels if isinstance(labels, list) else []:
            label = item.get("label") if isinstance(item, dict) else None
            if _own_label(label) and label not in names:
                names.append(label)
        names = names[:LABELS_MAX]
        if not names:
            return
        record = {"schema_version": 1, "state": ident.get("state"),
                  "base_url": base_url or ident.get("base_url"), "labels": names}
        path = _labels_path(ident)
        try:
            if postbox_os.read_bounded_json(path, max_bytes=MAX_LABELS_BYTES) == record:
                return                      # unchanged: no write on every poll
        except Exception:
            pass
        postbox_os.write_private_json_atomic(path, record, prefix=".labels-", max_bytes=MAX_LABELS_BYTES)
    except Exception:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _server_labels(ident):
    """The labels `save_server_labels` kept for this identity, or an empty set when the file is
    absent, unreadable, not the expected shape (an older daemon never writes it), or written for
    another state file or server than this identity's."""
    try:
        data = postbox_os.read_bounded_json(_labels_path(ident), max_bytes=MAX_LABELS_BYTES)
        names = data.get("labels") if isinstance(data, dict) else None
        if not isinstance(names, list):
            return set()
        if data.get("state") != ident.get("state") or data.get("base_url") != ident.get("base_url"):
            return set()
        return {x for x in names[:LABELS_MAX] if _own_label(x)}
    except Exception:
        return set()


def own_labels(ident):
    """The labels this seat answers to: the identity name, its hand-set roster address, the name
    without a -prod suffix (the production identity `bxxxxx-prod` is the seat `bxxxxx`), and the
    labels the server itself answers to for this identity (`save_server_labels`). Compared
    exactly."""
    name = ident.get("name") or ""
    labels = {name, ident.get("address") or ""}
    if name.endswith("-prod"):
        labels.add(name[:-len("-prod")])
    labels.update(_server_labels(ident))
    return {label for label in labels if _own_label(label)}


def clean(value, limit=TEXT_MAX):
    """A server string made safe for one line: control, format and line/paragraph separator
    characters removed, then cut to `limit` characters. None for anything that is not a string or
    is empty once cleaned."""
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp"))
    text = text.strip()[:limit]
    return text or None


def _clean_related(items, anchor_thread=None):
    """Bounded routing-only cross-thread related_to facts: keys, authors, thread_ids, and pair scores.
    Compares complete cleaned IDs (up to ID_MAX=512) before cutting displayed text to TEXT_MAX=120,
    and never keeps body text or unverified previews."""
    if not isinstance(items, list):
        return []
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        rk_full = clean(item.get("key_full"), ID_MAX) or clean(item.get("key"), ID_MAX)
        if not rk_full or not rk_full.startswith("msg/") or rk_full in seen:
            continue
        rthread_full = (
            clean(item.get("thread_id"), ID_MAX)
            or clean(item.get("thread_full"), ID_MAX)
            or clean(item.get("thread"), ID_MAX)
        )
        if anchor_thread and rthread_full == anchor_thread:
            continue
        scores = item.get("scores")
        raw_pair = scores.get("pair") if isinstance(scores, dict) else item.get("pair")
        pair = None
        if isinstance(raw_pair, (int, float)) and not isinstance(raw_pair, bool) and math.isfinite(float(raw_pair)):
            pair = round(float(raw_pair), 2)
        seen.add(rk_full)
        out.append({
            "key": rk_full[:TEXT_MAX],
            "key_full": rk_full,
            "author": clean(item.get("author")),
            "thread": rthread_full[:TEXT_MAX] if rthread_full else None,
            "thread_full": rthread_full,
            "pair": pair,
        })
        if len(out) >= MAX_RELATED:
            break
    return out


def _cache_path(ident):
    return ident["log"] + ".graph.json"


def _load(path):
    """The cache entries, or {} when the file is absent, unreadable or not the expected shape."""
    try:
        data = postbox_os.read_bounded_json(path, max_bytes=MAX_GRAPH_CACHE_BYTES)
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        kept = {}
        for k, v in entries.items():
            if not (isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("at"), (int, float))):
                continue
            facts = v.get("facts")
            if isinstance(facts, dict):
                if not isinstance(facts.get("done_when"), bool):
                    # Before 2026-10-04 this field held the sender's words. They are dropped on load, so
                    # they are never printed and the next write of the file no longer holds them.
                    facts["done_when"] = bool(facts.get("done_when"))
                if "thread_full" in facts or "thread" in facts:
                    t_full = clean(facts.get("thread_full"), ID_MAX) or clean(facts.get("thread"), ID_MAX)
                    facts["thread_full"] = t_full
                    facts["thread"] = t_full[:TEXT_MAX] if t_full else None
                if "related" in facts:
                    facts["related"] = _clean_related(facts.get("related"), anchor_thread=facts.get("thread_full"))
            kept[k] = v
        return kept
    except Exception:
        return {}


def _save(path, entries):
    """Keep the newest CACHE_MAX entries and replace the file in one step; a failure is ignored."""
    try:
        newest = sorted(entries.items(), key=lambda kv: kv[1]["at"], reverse=True)[:CACHE_MAX]
        postbox_os.write_private_json_atomic(
            path, {"schema_version": 1, "entries": dict(newest)},
            prefix=".graph-", max_bytes=MAX_GRAPH_CACHE_BYTES)
    except Exception:
        pass


def _stats_path(ident):
    return ident["log"] + ".graph-stats.json"


def _read_stats(path):
    try:
        data = postbox_os.read_bounded_json(path, max_bytes=MAX_STATS_BYTES)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def stats(ident):
    """This identity's graph-line counters, or None when there are none yet (no file, an unreadable
    one, or not the expected shape). Never raises. Read by the daemon for status.json."""
    try:
        data = _read_stats(_stats_path(ident))
        out = {k: data[k] for k in STAT_FIELDS if type(data.get(k)) is int and data[k] >= 0}
        if not out:
            return None
        for k in ("since", "updated_at", "last_error_at"):
            if isinstance(data.get(k), (int, float)) and not isinstance(data.get(k), bool):
                out[k] = data[k]
        if clean(data.get("last_error"), ERROR_MAX):
            out["last_error"] = clean(data["last_error"], ERROR_MAX)
        return out
    except Exception:
        return None


def _record(ident, delta, error, t):
    """Add `delta` to this identity's counters and replace the file in one step. `error` (or None) becomes
    last_error at time t. Never raises: a counter that cannot be written must not cost the notice."""
    tmp = None
    try:
        path = _stats_path(ident)
        cur = _read_stats(path)
        out = {"schema_version": 1}
        for k in STAT_FIELDS:
            base = cur.get(k) if type(cur.get(k)) is int and cur.get(k) >= 0 else 0
            out[k] = base + int(delta.get(k, 0))
        since = cur.get("since")
        out["since"] = since if isinstance(since, (int, float)) and not isinstance(since, bool) else t
        if error:
            out["last_error"], out["last_error_at"] = clean(error, ERROR_MAX), t
        elif clean(cur.get("last_error"), ERROR_MAX) and isinstance(cur.get("last_error_at"), (int, float)):
            out["last_error"], out["last_error_at"] = clean(cur["last_error"], ERROR_MAX), cur["last_error_at"]
        out["updated_at"] = t
        postbox_os.write_private_json_atomic(path, out, prefix=".graph-stats-", max_bytes=MAX_STATS_BYTES)
    except Exception:
        pass


def _facts(body):
    """The routing facts of one neighbours answer: {"pending": True} when the projection has not
    reached the message yet, a facts dict when it has, None when the answer is not the shape the
    server sends (apps/meshsem/related_reads.py, neighbours)."""
    if not isinstance(body, dict):
        return None
    anchor, declared = body.get("anchor"), body.get("declared")
    if not isinstance(anchor, dict) or not isinstance(declared, dict):
        return None
    replied_by = declared.get("replied_by")
    if not isinstance(replied_by, list):
        return None
    if anchor.get("thread_id") is None:
        return {"pending": True}
    thread, parent = declared.get("thread"), declared.get("replies_to")
    if (thread is not None and not isinstance(thread, dict)) or (parent is not None and not isinstance(parent, dict)):
        return None
    size = thread.get("size") if thread else None
    owes = anchor.get("owes_ack")
    anchor_thread_full = clean(thread.get("thread_id"), ID_MAX) if thread else clean(anchor.get("thread_id"), ID_MAX)
    anchor_thread = anchor_thread_full[:TEXT_MAX] if anchor_thread_full else None
    inferred = body.get("inferred")
    rel_raw = inferred.get("related_to") if isinstance(inferred, dict) and inferred.get("included") is True else None
    return {
        "parent": clean(parent.get("key")) if parent else None,
        "parent_author": clean(parent.get("author")) if parent else None,
        "thread": anchor_thread,
        "thread_full": anchor_thread_full,
        "size": size if type(size) is int and size >= 0 else None,
        "replies": len(replied_by),
        "replies_truncated": declared.get("replied_by_truncated") is True,
        "owes_ack": [clean(x) for x in owes if clean(x)] if isinstance(owes, list) else [],
        # The sender's words are message text: keep only whether the ask names a condition.
        "done_when": clean(anchor.get("done_when")) is not None,
        # A server with the unprojected fallback answers before its once-a-minute catch-up reaches the
        # message, from the row's own thread and reply fields, and says so: then the thread size counts
        # only the messages already indexed (apps/meshsem/related_reads.py, neighbours).
        "unindexed": anchor.get("projected") is False,
        "related": _clean_related(rel_raw, anchor_thread=anchor_thread_full),
    }


def _segment(key, facts, mine):
    if facts.get("parent"):
        if facts.get("parent_author") in mine:
            parent = "replies to YOUR %s" % facts["parent"]
        else:
            parent = "replies to %s by %s" % (facts["parent"], facts.get("parent_author") or "unknown")
    else:
        # Not "starts no reply chain": the eighth message of a thread that names no parent starts
        # nothing, and this is printed for it too.
        parent = "names no parent"
    thread = ""
    if facts.get("thread"):
        thread = "; thread %s" % facts["thread"]
        if facts.get("size") is not None:
            thread += " has %d message(s)" % facts["size"]
            if facts.get("unindexed"):
                thread += " (counting indexed messages only)"
    replies = ""
    if facts.get("replies"):
        # "linked replies", not "answered": a reply edge is not a settled ask, and an ask can be closed
        # without one (memory-bxxxxx, review of #87).
        replies = "; has %s%d linked reply(ies)" % ("at least " if facts.get("replies_truncated") else "",
                                                       facts["replies"])
    ack = ""
    if mine.intersection(x for x in facts.get("owes_ack") or [] if isinstance(x, str)):
        ack = "; expects a reply from you"
        if facts.get("done_when"):          # a yes or no (_load turns an older entry's text into one)
            ack += " (it names a done-when condition; read the message)"
    related = ""
    rel_items = facts.get("related")
    if isinstance(rel_items, list) and rel_items:
        parts = []
        for r in rel_items[:MAX_RELATED]:
            if not isinstance(r, dict) or not r.get("key"):
                continue
            who = "YOUR %s" % r["key"] if r.get("author") in mine else (
                "%s by %s" % (r["key"], r.get("author") or "unknown")
            )
            if r.get("thread"):
                who += " in %s" % r["thread"]
            if isinstance(r.get("pair"), (int, float)) and not isinstance(r.get("pair"), bool):
                who += " (pair %.2f)" % r["pair"]
            parts.append(who)
        if parts:
            related = "; related: " + ", ".join(parts)
    return "%s %s%s%s%s%s." % (key, parent, thread, replies, ack, related)


def _exit_reason(r):
    """'exit N', with the server's HTTP status when the client printed one: codes only, never text."""
    reason = "exit %d" % r.returncode
    try:
        status = json.loads(r.stdout).get("http_status")
        if type(status) is int:
            reason += ", http %d" % status
    except Exception:
        pass
    return reason


def _lookup(ident, key, run):
    """One neighbours call: (facts, error), where error is None on success and otherwise a short reason
    that carries no message text."""
    argv = [ident["python"], "-m", "tools.meshy_node", "neighbours", "--base-url", ident["base_url"],
            "--state-file", ident["state"], "--key", key]
    if ident.get("graph_line_inferred") is True:
        argv.extend(["--limit", "20", "--include-inferred"])
    hdir = postbox_os.helper_dir_for_log(ident["log"])
    try:
        settled = postbox_os.settle_helper_dir(hdir, only_kind="neighbours")
        if not settled.get("settled"):            # admin 361a7e P2-2: no new lookup over an unsettled one
            return None, "an earlier lookup is not settled"
        r = postbox_os.run_bounded(
            argv, cwd=ident["client"], max_stdout=STDOUT_MAX, max_stderr=65536, text=True,
            encoding="utf-8", errors="replace", timeout=CALL_TIMEOUT_S, run_fn=run or subprocess.run,
            helper_dir=hdir, helper_kind="neighbours",
            stdin=subprocess.DEVNULL, creationflags=postbox_os.NO_WINDOW,
            # The client checkout is integrity-pinned (postbox.py sets this for its own children); the
            # channel and the waiter do not, so set it here or a lookup leaves __pycache__ in it.
            # MESHYCHAT_VIA: this lookup is the notice's, not the seat's, so the server counts it as
            # neighbours_notice (divergence 072); a client from before the variable ignores it.
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", MESHYCHAT_VIA="notice"))
        if r.returncode != 0:
            return None, _exit_reason(r)
        if (getattr(r, "stdout_truncated", False) or not isinstance(r.stdout, str)
                or len(r.stdout) > STDOUT_MAX or len(r.stdout.encode("utf-8")) > STDOUT_MAX):
            return None, "stdout exceeded %d bytes" % STDOUT_MAX
        facts = _facts(json.loads(r.stdout))
    except subprocess.TimeoutExpired:
        return None, "timeout after %ds" % CALL_TIMEOUT_S
    except Exception as e:
        return None, type(e).__name__
    return facts, (None if facts is not None else "answer not in the neighbours shape")


def line_for(ident, keys, *, run=None, now=time.time):
    """The graph line for up to MAX_KEYS distinct `msg/` keys, or None when nothing could be looked up.
    Never raises."""
    try:
        if not ident.get("graph_line", True):
            return None
        if not all(isinstance(ident.get(f), str) and ident.get(f) for f in ("python", "client", "state", "base_url", "log")):
            return None
        if not os.path.isdir(os.path.join(ident["client"], "tools", "meshy_node")):
            return None          # no client checkout there: a call could only fail, after starting a process
        wanted = []
        for key in keys or []:
            if isinstance(key, str) and key.startswith("msg/") and clean(key) == key and key not in wanted:
                wanted.append(key)
        wanted = wanted[:MAX_KEYS]
        if not wanted:
            return None
        path, mine, t = _cache_path(ident), own_labels(ident), now()
        entries, changed, segments = _load(path), False, []
        delta, error = {"renders": 1}, None
        for key in wanted:
            entry = entries.get(key) or {}
            kept = entry.get("facts") if isinstance(entry.get("facts"), dict) else None
            age = t - entry["at"] if entry else None
            if kept is not None and abs(age) < (UNINDEXED_TTL_S if kept.get("unindexed") else FACTS_TTL_S):
                facts = kept
                delta["cached"] = delta.get("cached", 0) + 1
            elif entry.get("pending") is True and abs(age) < PENDING_TTL_S:
                facts = None
                delta["cached"] = delta.get("cached", 0) + 1
            elif t < _backoff_until.get(path, 0):
                delta["backed_off"] = delta.get("backed_off", 0) + 1
                continue
            else:
                facts, error_now = _lookup(ident, key, run)
                if error_now:
                    _backoff_until[path] = t + FAIL_BACKOFF_S
                    delta["failed"] = delta.get("failed", 0) + 1
                    error = error_now
                    continue
                if facts.get("pending"):
                    entries[key] = {"at": t, "pending": True}
                    facts = None
                    delta["pending"] = delta.get("pending", 0) + 1
                else:
                    entries[key] = {"at": t, "facts": facts}
                    delta["ok"] = delta.get("ok", 0) + 1
                changed = True
            if isinstance(facts, dict):
                try:
                    segments.append(_segment(key, facts, mine))
                except Exception:        # a hand-edited or damaged cache entry: no line for that key
                    pass
        if changed:
            _save(path, entries)
        if segments:
            delta["lines"] = 1
        _record(ident, delta, error, t)
        return PREFIX + " ".join(segments) if segments else None
    except Exception:
        return None


def for_event(ident, d):
    """The graph line for one delivered log line, or None. Only a message gets one: an edit, a
    redaction or a listener problem is not a new arrival with a place in the graph to show."""
    if not isinstance(d, dict) or d.get("event") != "message":
        return None
    try:
        return line_for(ident, [d.get("key")])
    except Exception:            # line_for never raises; this keeps a renderer alive if that is ever wrong
        return None
