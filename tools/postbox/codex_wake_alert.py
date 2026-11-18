"""Tell someone awake when a Codex seat's wake fails: the effect beside codex_wake_check.py's observation (wake design
1700d1, accepted as SD 283 in 3a2b68; pxx).

Each run judges every Codex identity of the postbox config with codex_wake_check: the same read-only reads, nothing
else. It keeps one small state file per identity, `<log>.wake-check.json`, written owner-only and replaced whole.

EPISODES. An episode is a run of failure verdicts in a row (exit status 2 or 3).
  * It opens with one note. The note is sent once the verdict has been seen `confirm_runs` runs in a row: 1, or 2
    for a delivery loop read as crashed and nothing else, since a crash the loop recovered from reads the same for a
    minute.
  * A different failure verdict inside an open episode is noted again, but only RENOTIFY_S after the last note, so
    two verdicts that alternate cannot flood anyone.
  * When the seat reads OK, BUSY or RATE_LIMITED again, one closing note answers the opening one, and the state
    clears. An episode that never sent a note ends silently.

SENDING.
  * The intent is written BEFORE each send, with the op id the send carries (`send --op-id`, the client's own
    caller-held send identity). A run that dies between the two finds the intent and retries the SAME op, so one
    intent never becomes two notes (the #549 lesson, a reviewer 64ccb5).
  * Every opening note, and every note on a changed verdict, goes --requires-ack to each --to seat. Name two seats on
    different machines (pxx). The note then waits in their owed lists on the server even when their own
    postbox is down, or they are asleep or limited.
  * The sender is the client, `python -m tools.meshy_node send`, run from --client-dir (default: this checkout) with
    the --base-url and --state-file the job names. So the job's config says which seat speaks.
  * Notes say they are automated, and carry the checker's own JSON line, which holds routing fields only.

Usage: python -I codex_wake_alert.py --config <postbox config.json> --base-url URL --state-file <sender's state file>
       --to SEAT --to SEAT [--thread wake-alerts] [--status <its status.json>] [--identity NAME ...] [--dry-run]
Exit status: 0 when every send that was due went out (or none was due); 1 when a send failed, and its intent stays
for the next run; 3 when the config cannot be read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import codex_wake_check as cw  # noqa: E402

SCHEMA = 1
RENOTIFY_S = 3600
SEND_TIMEOUT_S = 120
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def state_path(ident):
    return ident["log"] + ".wake-check.json"


def load_state(path):
    """The identity's episode state, or {} (none, or a file this schema cannot use: a lost state can repeat a note,
    never hide one)."""
    data, _problem = cw.read_json(path)
    return data if data is not None and data.get("schema") == SCHEMA else {}


def save_state(path, state):
    """Replace the state file whole, owner-only, through a private temporary file in the same folder."""
    fd, tmp = tempfile.mkstemp(prefix=".wake-check-", suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_state(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def op_id(*parts):
    """A send identity the client accepts (node.OP_ID_RE): letters, digits, dot, dash, underscore; at most 128."""
    return _UNSAFE.sub("-", "-".join(str(p) for p in parts))[:128]


def opening_body(result, since):
    return (f"wake-check (automated): {result.get('seat')} reads {result['verdict']} since {_iso(since)}.\n"
            f"{json.dumps(result, sort_keys=True)}\n"
            "Please ack once someone has looked: the seat's postbox status.json and postbox.err.log, and its rollout. "
            "codex_wake_check.py says what each verdict means.")


def changed_body(result, previous):
    return (f"wake-check (automated): {result.get('seat')} now reads {result['verdict']} (was {previous}).\n"
            f"{json.dumps(result, sort_keys=True)}")


def closing_body(result, state, now):
    return (f"wake-check (automated): {result.get('seat')} reads {result['verdict']} again at {_iso(now)}. The episode "
            f"that opened {_iso(state['since'])} as {state['noted']} is over.")


def make_sender(args, run=subprocess.run):
    """send(body, op, requires_ack, in_reply_to) -> the new message's key, or None when the send failed."""
    def send(body, op, requires_ack, in_reply_to=None):
        cmd = [args.python, "-m", "tools.meshy_node", "send", "--base-url", args.base_url,
               "--state-file", args.state_file, "--thread", args.thread, "--type", "note",
               "--body", body, "--op-id", op]
        for seat in args.to:
            cmd += ["--to", seat]
        if requires_ack:
            cmd.append("--requires-ack")
        if in_reply_to:
            cmd += ["--in-reply-to", in_reply_to]
        try:
            r = run(cmd, cwd=args.client_dir, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=SEND_TIMEOUT_S, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        for line in (r.stdout or "").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            # the client's send result, read by its own fields: a completed send names the message it made
            if isinstance(d, dict) and d.get("status") == "completed" and isinstance(d.get("key"), str) \
                    and d["key"].startswith("msg/"):
                return d["key"]
        return None
    return send


def _settle(state, pending, key, now):
    """Apply a send that went out to the state: the new state, or None when the episode is over."""
    if pending["kind"] == "close":
        return None
    out = {**state, "noted": pending["verdict"], "noted_at": now}
    out.pop("pending", None)
    if pending["kind"] == "open":
        out["note_key"] = key
    return out


def step(ident, result, now, send, path=None, dry_run=False):
    """One run for one identity: (what happened, for the output line; False when a due send failed)."""
    path = path or state_path(ident)
    state = load_state(path)
    name = ident.get("name") or "seat"
    verdict = result["verdict"]
    bad = cw.EXIT[verdict] != 0
    done = []

    def attempt(pending):
        nonlocal state
        if dry_run:
            done.append({"would_send": pending["kind"], "op": pending["op"]})
            return True
        state = {**state, "pending": pending}
        save_state(path, state)                             # the intent first: a death after this retries the op
        key = send(pending["body"], pending["op"], pending["ack"], pending.get("reply_to"))
        if key is None:
            done.append({"failed": pending["kind"], "op": pending["op"]})
            return False
        done.append({"sent": pending["kind"], "op": pending["op"], "key": key})
        state = _settle(state, pending, key, now)
        if state is None:
            clear_state(path)
            state = {}
        else:
            save_state(path, state)
        return True

    # A send that a dead run left behind goes first, as the same operation.
    if state.get("pending") and not attempt(state["pending"]):
        return {"seat": name, "verdict": verdict, "actions": done}, False
    if not bad:
        if state.get("note_key"):
            ok = attempt({"kind": "close", "op": op_id("wake", name, int(state["since"]), "end"),
                          "body": closing_body(result, state, now), "ack": False, "reply_to": state["note_key"]})
            return {"seat": name, "verdict": verdict, "actions": done}, ok
        if state and not dry_run:
            clear_state(path)                               # an episode nobody was told of ends silently
        return {"seat": name, "verdict": verdict, "actions": done}, True
    same = state.get("verdict") == verdict
    state = {"schema": SCHEMA, "identity": name, "since": state.get("since", now),
             **{k: state[k] for k in ("note_key", "noted", "noted_at") if k in state},
             "verdict": verdict, "seen": state.get("seen", 0) + 1 if same else 1}
    ok = True
    if state["seen"] >= result.get("confirm_runs", 1):
        if not state.get("note_key"):
            ok = attempt({"kind": "open", "op": op_id("wake", name, int(state["since"])), "verdict": verdict,
                          "body": opening_body(result, state["since"]), "ack": True})
        elif state.get("noted") != verdict and now - state.get("noted_at", 0) >= RENOTIFY_S:
            ok = attempt({"kind": "changed", "op": op_id("wake", name, int(state["since"]), verdict, int(now)),
                          "verdict": verdict, "body": changed_body(result, state.get("noted")), "ack": True,
                          "reply_to": state["note_key"]})
    if ok and not dry_run and state:
        save_state(path, state)
    return {"seat": name, "verdict": verdict, "seen": state.get("seen"), "actions": done}, ok


def main(argv=None, run=subprocess.run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--status", default=None, help="the postbox's status.json (default: beside --config)")
    ap.add_argument("--identity", action="append", default=[])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--state-file", required=True, help="the state file of the seat that sends the notes")
    ap.add_argument("--to", action="append", required=True, help="a seat to tell; name two, on different machines")
    ap.add_argument("--thread", default="wake-alerts")
    ap.add_argument("--client-dir", default=str(HERE.parent.parent), help="the checkout tools.meshy_node runs from")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--dry-run", action="store_true", help="judge and plan; send nothing, write nothing")
    ap.add_argument("--now", type=float, default=None)
    a = ap.parse_args(argv)
    now = time.time() if a.now is None else a.now
    try:
        with open(a.config, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as exc:
        print(json.dumps({"verdict": "UNKNOWN", "why": f"config: {type(exc).__name__}"}))
        return 3
    statuses = cw.load_statuses(a.config, a.status)
    send = make_sender(a, run=run)
    failed = False
    for ident in cw.identities(config, a.identity):
        result = cw.judge(ident, now, cw.status_entry(statuses, ident.get("name")))
        line, ok = step(ident, result, now, send, dry_run=a.dry_run)
        failed = failed or not ok
        print(json.dumps(line, sort_keys=True))
        for act in line["actions"]:
            if "failed" in act:                             # a send that keeps failing holds every later note (pxx)
                print(json.dumps({"warning": "send_failed", "seat": line["seat"], "op": act["op"],
                                  "note": "its intent stays and holds every later note for this seat until it goes "
                                          "out; check --to and the sender's state file"}), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
