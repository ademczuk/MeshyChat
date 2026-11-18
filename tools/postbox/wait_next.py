"""wait_next: wake a CLI session once when its identity's postbox log has mail. Any CLI that can run a
background shell command and gets told when it exits can use this (Kimi Code, Antigravity, or a
Claude Code pane launched without the meshy channel).

    python wait_next.py <identity> [--consumer NAME]

Prints the new addressed lines and exits 0; exits 0 with a "postbox_not_running" line if no postbox
holds its lock (checked every 30 s). Resumes from <log>.wait-<consumer>.offset, saved AFTER the
print, so a late re-arm delivers the backlog at once and a killed waiter re-delivers rather than
drops. Refuses (exit 3) when stdout is discarded, since a delivery into NUL would advance the offset
unread. One waiter per identity and consumer (a lock file); a second exits 2.
"""
import argparse
import datetime
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import delivery_bank  # noqa: E402
import postbox_os  # noqa: E402  (the OS seams: locks, commands; macOS support)
try:
    import graphline  # noqa: E402  (one line of message-graph context per arriving message)
except ImportError:     # an install that copied the other scripts but not graphline.py still delivers
    graphline = None
try:
    import owedline  # noqa: E402  (the asks this seat owes a reply to, roadmap 8.2)
except ImportError:     # likewise for owedline.py
    owedline = None


def utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


#: postbox_os.wanted, the one wake rule shared with postbox.py (tests/test_postbox_redaction.py holds the three names to it).
EDIT_WAKE_ROLES = postbox_os.EDIT_WAKE_ROLES
wanted = postbox_os.wanted


def stdout_discarded():
    return postbox_os.stdout_discarded()   # the Windows console test, moved verbatim; /dev/null on POSIX


def read_command(ident, key):
    return postbox_os.read_command(ident, key)


def postbox_running():
    if not postbox_os.IS_WINDOWS:
        # POSIX: the process table, never the lock (a probe holding postbox.lock for an instant can make
        # a daemon that is starting exit; postbox_os condition b). Windows keeps its probe below (a).
        return postbox_os.daemon_running(HERE)
    import msvcrt
    path = os.path.join(HERE, "postbox.lock")
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return True
    else:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return False
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("identity", help="one identity, or several comma-separated (e.g. antigravity,antigravity-prod)")
    ap.add_argument("--consumer", default="session")
    ap.add_argument("--once", action="store_true",
                    help="check once and exit (prints an 'empty' line when nothing is new): for a CLI "
                         "that can only poll, e.g. a Kimi cron job. Same offset and lock as the "
                         "blocking waiter, so the two never deliver one message twice.")
    ap.add_argument("--peek-bank", action="store_true",
                    help="inspect held cc items and uncollected spool bytes without taking the waiter lock "
                         "or advancing the offset; prints one JSON line per identity and exits 0.")
    ap.add_argument("--flush-held", action="store_true",
                    help="release banked cc items on each poll pass of this waiter (pair with --once for a one-shot turn-boundary drain).")
    a = ap.parse_args()
    if stdout_discarded():
        sys.stderr.write("wait_next: REFUSED, stdout is discarded; run it where its output is captured\n")
        return 3
    if not a.peek_bank and not delivery_bank._valid_consumer_name(a.consumer):
        print("wait_next: invalid consumer %r" % (a.consumer,), flush=True)
        return 2
    cfg_path = os.environ.get("MESHY_POSTBOX_CONFIG") or os.path.join(HERE, "config.json")
    init_stat_sig = postbox_os.config_stat(cfg_path)
    raw_cfg, cfg_info = postbox_os.read_bounded_bytes(cfg_path, max_bytes=postbox_os.MAX_CONFIG_BYTES)
    if init_stat_sig is None:
        init_stat_sig = (cfg_info.st_mtime_ns, cfg_info.st_size)
    cfg = json.load(io.StringIO(raw_cfg.decode("utf-8")))
    if a.peek_bank:
        rc = 0
        for name in [n.strip() for n in a.identity.split(",") if n.strip()]:
            ident = next((i for i in cfg["identities"] if i["name"] == name), None)
            if ident is None:
                print("wait_next: no identity %r in postbox config.json" % name, flush=True)
                return 2
            if ident.get("source") == "legacy":
                print("wait_next: %s is skipped because the legacy source was retired on 2026-09-28 and removed from the postbox on 2026-10-03; "
                      "this identity needs the new client (tools.meshy_node adopt; see README)" % name, flush=True)
                return 2
            ident.setdefault("base_url", cfg["base_url"])
            info = delivery_bank.peek_bank(ident["log"], consumer=a.consumer, ident=ident)
            print(json.dumps(info), flush=True)
            if info.get("error"):
                rc = 1
        return rc
    watched = []                                        # [ident, off_file, off, offset is on disk, bank, hold_opts, stat_sig]
    report_fn = lambda t: sys.stderr.write("wait_next: %s\n" % t)
    for name in [n.strip() for n in a.identity.split(",") if n.strip()]:
        ident = next((i for i in cfg["identities"] if i["name"] == name), None)
        if ident is None:
            print("wait_next: no identity %r in postbox config.json" % name, flush=True)
            return 2
        if ident.get("source") == "legacy":
            print("wait_next: %s is skipped because the legacy source was retired on 2026-09-28 and removed from the postbox on 2026-10-03; "
                  "this identity needs the new client (tools.meshy_node adopt; see README)" % name, flush=True)
            return 2
        ident.setdefault("base_url", cfg["base_url"])
        log = ident["log"]
        lock = os.open("%s.wait-%s.lock" % (log, a.consumer), os.O_RDWR | os.O_CREAT)
        if not postbox_os.try_lock(lock):
            print("wait_next: another waiter for %s/%s is armed" % (name, a.consumer), flush=True)
            return 2
        off_file = "%s.wait-%s.offset" % (log, a.consumer)
        off = delivery_bank.legacy_offset(off_file, log)
        off_saved = off is not None
        if off is None:
            off = os.path.getsize(log) if os.path.exists(log) else 0
        # A consumer with NO offset file starts at the end of the log ("from now"). Its first pass must
        # SAVE that position even when nothing is new: a --once poller that saved only on movement
        # started at the new end on every run, and never saw mail that arrived between two runs (HQB,
        # 2026-10-01; tests/test_postbox_wait_next_once.py). A file that exists but holds no number
        # replays the log's last day (delivery_bank.legacy_offset).
        ident["_config_stat"] = init_stat_sig
        hold_opts = postbox_os.hold_options(ident, report_fn)
        postbox_os.settle_seconds(ident, "wait_settle_s", report_fn)     # a malformed setting: said once, here
        stat_sig, hold_opts = postbox_os.refresh_hold_options(cfg_path, ident, init_stat_sig, hold_opts, report_fn)
        watched.append([ident, off_file, off, off_saved,
                        delivery_bank.Bank(ident, off_file, off, "wait:" + a.consumer), hold_opts, stat_sig])
        print("wait_next: %s armed at byte %d" % (name, off), flush=True)
    last_probe = 0.0
    while True:
        woke = False
        woke_keys = []
        woke_idents = set()
        graph_lines = {}
        for w in watched:
            ident, off_file, off, off_saved, bank, hold_opts, stat_sig = w
            stat_sig, hold_opts = postbox_os.refresh_hold_options(cfg_path, ident, stat_sig, hold_opts, report_fn)
            w[5], w[6] = hold_opts, stat_sig
            new_off, hits = bank.collect(wanted, at_turn_boundary=a.flush_held,
                                         fetched=delivery_bank.fetched_reader(ident, getattr(bank, "report", None)), **hold_opts)
            # Never with --once: a one-pass poll has no next pass of its own, and its caller's interval would add
            # to the wait, past the cap. Off unless `wait_settle_s` is set (postbox_os.settle_hold).
            window = postbox_os.settle_seconds(ident, "wait_settle_s") if hits and not a.once else 0
            if window and postbox_os.settle_hold(hits, bank.received(), ident, window, cut=getattr(bank, "cut", True)):
                if not off_saved:
                    # A consumer with no saved position must save where it STARTED before it defers anything
                    # (the GitHub Codex review of 3fdf617b): a waiter that exited inside the window had saved
                    # nothing, so its next run started at the new end of the log and never showed the deferred
                    # mail. This is the bank's start-up save: the cursor stays where it is, nothing is delivered.
                    bank.commit(bank.offset, [])
                    w[3] = True
                continue                 # still settling: nothing printed or delivered; collected again next pass
            for d in hits:
                print(json.dumps(dict(d, identity=ident["name"])), flush=True)
                # One line of message-graph context (routing only; graphline.py), absent when the lookup fails.
                graph = graphline.for_event(ident, d) if graphline else None
                if graph:
                    print(json.dumps({"event": "graph", "identity": ident["name"], "key": d.get("key"),
                                      "line": graph}), flush=True)
                    # By identity, then key: two watched seats can hold the same message, and "YOUR"
                    # and "from you" in a line mean the seat it was rendered for.
                    graph_lines.setdefault(ident["name"], {})[d.get("key")] = graph
                if d.get("event") in ("message", "edit"):
                    woke_keys.append(d.get("key"))
                    # The exact signed read for THIS identity (its own state file and server).
                    print(json.dumps({"event": "read_with", "identity": ident["name"], "key": d.get("key"),
                                      "cmd": read_command(ident, d.get("key"))}), flush=True)
                    if d.get("event") == "message":
                        # The reply command with --in-reply-to already filled in, as the Codex notice has
                        # had: only that field makes the replies_to edge that marks an ask answered, and a
                        # waiter seat was shown no reply command at all (roadmap item 8.0).
                        print(json.dumps({"event": "reply_with", "identity": ident["name"], "key": d.get("key"),
                                          "when": "only if, after reading it, you decide a reply is needed",
                                          "cmd": postbox_os.send_command(ident, d.get("key"))}), flush=True)
            if new_off != bank.offset or not off_saved or hits or bank.held_changed:
                bank.commit(new_off, hits)
                w[3] = True
            w[2] = new_off
            woke = woke or bool(hits)
            if hits:
                woke_idents.add(ident["name"])
        if woke:
            # What each identity that woke owes (roadmap 8.2, owedline.py): every wake, since a waiter is one
            # process per wake and its seat has no other place to see it.
            owed_lines = {}
            for w in watched:
                if owedline and w[0]["name"] in woke_idents:
                    owed, _ = owedline.wake_line(w[0], "wait:" + a.consumer, every=True)
                    if owed:
                        owed_lines[w[0]["name"]] = owed
                        print(json.dumps({"event": "owed", "identity": w[0]["name"], "line": owed}), flush=True)
            # The LAST line names what woke it (ASDLC OI-76 P4): a harness that shows only the tail of a
            # finished task (Claude's task notice, Kimi's background-task wake) still shows the keys.
            last = {"event": "waiter", "state": "woke", "woke_at": utc(),
                    "count": len(woke_keys), "keys": woke_keys,
                    "instructions": delivery_bank.BATCH_INSTRUCTIONS.strip()}
            if graph_lines:
                # The same lines the "graph" events carried, for a harness that shows only this line.
                last["graph"] = graph_lines
            if owed_lines:
                last["owed"] = owed_lines
            print(json.dumps(last), flush=True)
            return 0
        if a.once:
            state = "empty" if postbox_running() else "postbox_not_running"
            print(json.dumps({"event": "waiter", "state": state, "checked_at": utc()}), flush=True)
            return 0
        if time.time() - last_probe >= 30:
            last_probe = time.time()
            if not postbox_running():
                print(json.dumps({"event": "waiter", "state": "postbox_not_running", "woke_at": utc()}), flush=True)
                return 0
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
