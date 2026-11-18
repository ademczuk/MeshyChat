"""A Codex turn that ends with a question for Bxxxxx, forwarded to MeshyChat once.

a recorded decision in kxxx's session on 7 Nov 2026: "we should have meshychat fixed so that any node can be steered
without having me to write my authorisation". On 6 Nov a Codex seat asked him in its own window at 22:41 and 22:46,
and nobody saw it for 8.5 hours. The framing in every notice now tells a seat to ask on MeshyChat
(postbox_os.DIRECTIVE_EXCEPTION). This is the backstop for a turn that asks in its window anyway.

When a Codex turn ends (task_complete) and its last agent message asks the operator something, and the turn did not
already ask on MeshyChat, the question is posted ONCE per turn to the seat's coordinator, with --requires-ack, as the
seat itself, through the seat's own sender. The coordinator answers with a seat_directive or an operator_relay,
which the framing tells the seat to accept as Bxxxxx's answer.

Opt-in per identity, in the postbox config:
    "parked_question": {"to": "<coordinator label>", "sender": [<argv of the seat's gated sender>], "seat": "<label>",
                        "status": true, "enabled": true}
`"status": true` reads the identity's own op journal through its client's `send-status --peek`, built from the
identity's python, client, state and base_url (peek_spec); nothing else is run as a status.
With no "parked_question", without "to" or "sender", or with "enabled" anything but true when present, nothing runs.
The question text leaves the box only to that coordinator and only through that sender, which screens it. The
rollout is only read, never written, and only the last message of the turn is quoted.

HARDENED (8 Nov 2026, after codex-oxxxxxx-cli's source read a20d84, 4 P1 and 3 P2, line references in a reviewer's
intake 25d3d5; the owner plan in that packet):
- F1, the sender's answer. Only stdout counts, read bounded, and it must be ONE JSON object, correlated first: one
  that names another op id is UNKNOWN, a refusal included. SENT (receipt_problem) is exit 0, status `completed`, a
  msg/ `key`, the op state `succeeded` when given, and nothing that says no: `ok`, `sent` or a verified flag present
  and not true, an `error`, `error_code`, `refused` or `value_mismatch`, or `result.ok` false. REFUSED, the only
  answer that sends the pointer, is an explicit {"refused": <why>, "sent": false} with no key or status. Everything
  else is UNKNOWN: no substring of any output decides (pxx A).
- F2, an unknown outcome. Before transport the turn's intent is written to the ledger (state `sending`, the op_id,
  the thread and the body's sha256). An UNKNOWN outcome is HELD there and never sent again by this module. A held
  operation is OBSERVED, at most every RECONCILE_EVERY_S, through the identity's own op journal, read only (the
  client's `send-status --peek`, built from the identity by peek_spec, the op id appended; nothing else is run,
  since plain send-status may present an op again: codex-oxxxxxx-cli 29cd67, pxx R2-4).
  `succeeded` records it as sent, `held-by-guard` sends the pointer, `refused` is final, and anything else stays held
  and reported, for the postbox's own `send drain` to settle as it settles every journalled send. A pointer refused
  too is final. With no `status` configured a held operation stays held: the STOP the owner plan asks for.
- F3, the ledger. A missing ledger is a first run. One that cannot be read, is not JSON, is not a map of rows, or
  holds a row with an unknown state or no op id is CORRUPT: nothing is sent, nothing is written over it, and the
  status says so. Rows are pruned by age only, a final row older than LEDGER_RETAIN_S, never a held one and never one
  inside MAX_AGE_S; past LEDGER_MAX_ROWS no row is added and nothing is sent. Every write is a fresh mkstemp file
  (0600), flushed and fsynced, then replaced, and the rows in memory change only once the file has.
- F4, the kill. A tree kill counts as done only when taskkill (Windows) or killpg (elsewhere) itself succeeded, and
  the sender's exit is confirmed; a leader that exited while the tree kill failed is not "ended".
- F5, the output. At most OUTPUT_LIMIT bytes are read from each stream; a sender that writes more is UNKNOWN.
- F6, a turn that already asked. A tool call is parsed, never matched as bytes: Codex `exec` code
  (`tools.exec_command({cmd: "..."})`, what this fleet's rollouts hold, with JS comments and strings left out),
  function_call and local_shell_call. A call is send-like only when a Python interpreter runs a script or a module
  with --requires-ack, --expects-reply or --type question among the script's own arguments, and without --help or
  --dry-run; a heredoc's body, a here-string's body and a comment are text, not commands. And the turn counts as
  having asked only when (bound_receipt, pxx R2-1 and R2-2, 216b4e R3-2): the call is ONE simple command, that
  send, and its own --to or --cc names the coordinator (argv only picks the candidate); its own output (its call_id,
  used once) is, parsed strictly, exactly one JSON answer from a known wrapper with exit code 0 (sole_answer); that
  answer passes receipt_problem, matches the call's --op-id when it names one, and CARRIES THE SIGNED VALUE, which
  decides: it names the coordinator (exactly) in its to or cc and is an ask (requires_ack true or type question).
  Anything else counts as no send, since a question nobody sees (the 6 Nov case) is worse than a second copy.
  Forwarded twice, by design: a send in a compound command, a send whose output carries anything beside its one JSON
  answer (stderr hints included) or no signed value (kxxx_send's summary), a send that outlasts exec_command's yield
  (its answer comes under write_stdin's call id), a send built from a JS argv array, a sender that is not Python. Not
  caught (a named limit, pxx R3-1): a script that prints a forged receipt; only the op journal could tell.
- F7, the time. A turn with no valid time, or one more than FUTURE_SKEW_S ahead of the clock, is never forwarded.
- `"enabled": false` disables it.
"""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CHECK_SECONDS = 30.0
READ_LIMIT = 4 * 1024 * 1024        # bytes read per update; the rest is read on the next one
MAX_LINE = 1048576                  # a longer line is skipped, like CodexTurn
QUOTE_CHARS = 1500
SEND_TIMEOUT_S = 120
#: On a (re)start, or when the rollout is replaced or shrinks, the watch reads back this many bytes before the end, so a
#: turn that finished while the postbox was down (a restart, a sleep, an update) is still seen (pxx P3 1-2).
#: The ledger already skips a turn forwarded before.
BACKLOG_BYTES = READ_LIMIT
#: A turn whose end is older than this is history, never forwarded, read back or not. The 6 Nov question waited
#: 8.5 hours; half a day is past that, and past a night's stop.
MAX_AGE_S = 12 * 3600
#: F7: a turn time this far ahead of the clock is not trusted, and its turn is not forwarded.
FUTURE_SKEW_S = 300
#: F3: a final ledger row is kept at least this long. Past MAX_AGE_S no read-back turn is forwarded, so its row is no
#: longer needed; the margin covers a clock step and a slow restart.
LEDGER_RETAIN_S = MAX_AGE_S + 12 * 3600
#: F3: past this many rows the ledger takes no new row, so nothing is sent, rather than drop a row still needed.
LEDGER_MAX_ROWS = 5000
#: F5: bytes read from each of the sender's two outputs.
OUTPUT_LIMIT = 64 * 1024
#: F2: a held operation's status is asked at most this often, and each status call is bounded like a send.
RECONCILE_EVERY_S = 300
STATUS_TIMEOUT_S = 60
#: How often a running send looks at the postbox's stop flag.
STOP_POLL_S = 0.5
#: The cleanup of a stopped or timed-out send waits at most KILL_TIMEOUT_S for the tree kill and REAP_TIMEOUT_S for
#: the sender's exit: with the poll, about 5.5 s, well inside main's shared join of about 15 s (postbox.py main), so
#: the worker removes the body file before the daemon exits (codex-kxxx-cli 8390ed, pxx).
KILL_TIMEOUT_S = 3
REAP_TIMEOUT_S = 2

#: The outcome of one send or status call.
SENT, REFUSED, UNKNOWN = "sent", "refused", "unknown"
#: An urgent send the server refused for the seat's hourly allowance (signed): check() sends it again as ordinary.
RATE_LIMITED = "rate-limited"
#: Ledger states. A held row is one whose operation's outcome is not known; it is never pruned and never resent.
FINAL = ("sent", "sent-pointer", "refused-by-server", "pointer-refused")
#: The final states in which the question never reached the coordinator.
UNSENT = ("refused-by-server", "pointer-refused")
#: "sending-declined": an urgent question the server turned down for the allowance, being sent again as ordinary under
#: its `declined_op_id` (the urgent lane, kxxx).
HELD = ("sending", "unknown", "sending-pointer", "unknown-pointer", "sending-declined")
#: The client's op state for an op its own send guard refused before sending (tools/meshy_node/node.OP_HELD).
OP_HELD_BY_GUARD = "held-by-guard"

#: A question for the operator: a question mark near the end, and one of these asks for a decision or permission.
CUES = re.compile(r"\b(authori[sz]e|approv(?:e|al)|permission|confirm|may i|shall i|should i|do you want|"
                  r"ok to|okay to|go ahead|your (?:ok|okay|go|yes|answer|decision|call)|decide|which (?:option|one))\b",
                  re.IGNORECASE)
#: The tool-call payloads parsed for a send (F6), and the bytes a line must hold before it is parsed at all.
CALL_MARKERS = (b'"function_call"', b'"custom_tool_call"', b'"local_shell_call"', b'"exec_command"')
ASK_BYTES = (b"--requires-ack", b"--expects-reply", b"question")
#: Arguments with which a sender sends nothing.
_NO_SEND_ARGS = {"--help", "-h", "--dry-run"}
#: Programs that print or search their arguments, never send them.
#: A seat's senders are Python (`-m tools.meshy_node send`, kxxx_send.py, codex_kxxx_cli_send.py). A command counts as
#: a send only when an interpreter of this name runs a script or a module with the flag among the script's OWN
#: arguments. Anything else (a printer, a wrapper such as env or xargs, a heredoc's text, a comment, a sender in
#: another language) counts as no send, so the turn's question is forwarded, at worst twice, never suppressed
#: (pxx: a list of printers failed the other way, for every printer it did not name).
_PYTHON = re.compile(r"python[0-9.]*w?|pyw?")
#: Interpreter options that take the next argument as their value.
_PY_VALUE_OPTS = {"-X", "-W", "--check-hash-based-pycs"}
SHELLS = {"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd"}
#: A heredoc's start: << (or <<-, which strips leading tabs) and its delimiter word, quoted or not.
_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z0-9_.-]+))")
#: A PowerShell here-string's start: @' or @" ending its line. It runs to a line that starts with '@ or "@.
_HERE_STRING = re.compile(r"@(['\"])[ \t]*$")
#: A backslash that separates a Windows path: one before a letter, a digit, _, . or -.
_PATH_BACKSLASH = re.compile(r"\\(?=[A-Za-z0-9_.\-])")
_OPERATORS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"}
_EXEC_CMD = re.compile(r"exec_command\s*\(\s*\{")
_CMD_KEY = re.compile(r"\bcmd\s*:\s*")


def enabled(ident) -> bool:
    cfg = ident.get("parked_question")
    return (isinstance(cfg, dict) and cfg.get("enabled", True) is True
            and isinstance(cfg.get("to"), str) and cfg["to"].strip() != ""
            and isinstance(cfg.get("sender"), list) and len(cfg["sender"]) > 0
            and all(isinstance(a, str) and a for a in cfg["sender"]))


def asks_operator(text) -> bool:
    """True when the last lines of `text` ask for a decision or permission and end in a question."""
    if not isinstance(text, str):
        return False
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    tail = " ".join(lines[-3:])[-600:]
    return "?" in tail and CUES.search(tail) is not None


def _code_of(line: str):
    """(the line without its comment, the heredoc it opens as (word, strip_tabs) or None). A comment starts at an
    unquoted # that begins a word; a heredoc at an unquoted << that is not <<< (pxx)."""
    quote, prev, heredoc, i = None, " ", None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"':
                i += 1
        elif ch in "'\"":
            quote = ch
        elif ch == "\\":
            i += 1
        elif ch == "#" and (prev.isspace() or prev in ";&|()"):
            return line[:i], heredoc
        elif ch == "<" and heredoc is None and line.startswith("<<", i) and not line.startswith("<<<", i):
            m = _HEREDOC.match(line, i)
            if m:
                heredoc = (m.group(2) or m.group(3) or m.group(4), m.group(1) == "-")
                prev, i = line[m.end() - 1], m.end()
                continue
        prev = ch
        i += 1
    return line, heredoc


def _tokens(code: str):
    """The simple commands of one line of code, each a list of tokens. A backslash before a path character is read as
    a path separator (the seats run on Windows), so C:\\py\\python.exe keeps its name; one before a quote, a
    backslash or $ stays an escape, so a quoted script keeps its \\" (a reviewer d29a54 P2). Raises ValueError on an
    unbalanced quote."""
    lex = shlex.shlex(_PATH_BACKSLASH.sub("/", code), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""                     # comments were cut by _code_of, where a # inside a word stays
    out, current = [], []
    for tok in lex:
        if tok in _OPERATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out


def _simple_commands(cmd: str):
    """The simple commands of a shell command line, each a list of tokens. Lines are split first (a trailing
    backslash continues one). A heredoc's body and a PowerShell here-string's body are text, never commands, and so
    is a comment (pxx). Raises ValueError on an unbalanced quote."""
    lines = re.sub(r"\\\r?\n", " ", cmd).splitlines()
    out, i = [], 0
    while i < len(lines):
        line, i = lines[i], i + 1
        here = _HERE_STRING.search(line)
        if here:
            closing = here.group(1) + "@"
            line = line[:here.start()]
            while i < len(lines) and not lines[i].startswith(closing):
                i += 1
            if i < len(lines):
                lines[i] = lines[i][len(closing):]          # what follows '@ on its line is code again
        code, heredoc = _code_of(line)
        out += _tokens(code)
        if heredoc:
            word, strip_tabs = heredoc
            while i < len(lines) and (lines[i].lstrip("\t") if strip_tabs else lines[i]).rstrip("\r") != word:
                i += 1
            i += 1                                          # the delimiter line
    return out


def _script_args(args):
    """The script's own arguments when an interpreter's `args` run a script (*.py, *.pyw) or a module (-m), else
    None (-c, stdin, nothing)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-m":
            return args[i + 2:] if i + 1 < len(args) else None
        if a in ("-c", "-"):
            return None
        if a in _PY_VALUE_OPTS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return args[i + 1:] if a.lower().endswith((".py", ".pyw")) else None
    return None


def _shell_script(args):
    """The command line a shell is asked to run (bash -lc "...", pwsh -Command "...", cmd /c ...), or None."""
    for i, a in enumerate(args):
        low = a.lower()
        if low in ("/c", "/k"):
            return " ".join(args[i + 1:]) or None
        if low == "-command" or (low.startswith("-") and not low.startswith("--") and low.endswith("c")):
            return args[i + 1] if i + 1 < len(args) else None
    return None


def _asks(own) -> bool:
    """Whether a sender's own arguments ask on the mesh and send something."""
    return _NO_SEND_ARGS.isdisjoint(own) and (
        "--requires-ack" in own or "--expects-reply" in own or "--type=question" in own
        or any(a == "--type" and b == "question" for a, b in zip(own, own[1:])))


def _labels_in(value: str) -> list:
    return [x.strip() for x in value.split(",") if x.strip()]


def _intent(own) -> dict:
    """What a send asks of whom, from its own arguments: --to and --cc (repeated or comma-separated) and --op-id."""
    out, i = {"to": [], "cc": [], "op_id": None}, 0
    while i < len(own):
        a = own[i]
        for flag, field in (("--to", "to"), ("--cc", "cc")):
            if a == flag and i + 1 < len(own):
                out[field] += _labels_in(own[i + 1])
                i += 1
                break
            if a.startswith(flag + "="):
                out[field] += _labels_in(a[len(flag) + 1:])
                break
        else:
            if a == "--op-id" and i + 1 < len(own):
                out["op_id"] = own[i + 1]
                i += 1
            elif a.startswith("--op-id="):
                out["op_id"] = a[len("--op-id="):]
        i += 1
    return out


def sole_send_intent(cmd, _depth=0):
    """The intent of a command that is ONE simple command, a Python sender asking on the mesh (a shell running
    exactly such a command counts), or None. Only such a call's output can stand for its send: in a compound command
    (`send ...; cat receipt.json`) the output cannot be tied to the send (pxx R2-1)."""
    if _depth > 3:
        return None
    if isinstance(cmd, list):
        commands = [[a for a in cmd if isinstance(a, str)]]
    elif isinstance(cmd, str):
        try:
            commands = _simple_commands(cmd)
        except ValueError:
            return None
    else:
        return None
    if len(commands) != 1:
        return None
    tokens, i = commands[0], 0
    while i < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[i]):
        i += 1
    words = tokens[i:]
    if not words:
        return None
    program = os.path.basename(words[0].replace("\\", "/")).lower().removesuffix(".exe")
    if program in SHELLS:
        script = _shell_script(words[1:])
        return None if script is None else sole_send_intent(script, _depth + 1)
    own = _script_args(words[1:]) if _PYTHON.fullmatch(program) else None
    return _intent(own) if own and _asks(own) else None


def asks_on_mesh(cmd, _depth=0) -> bool:
    """True when one simple command of `cmd` (a string, or an argv list) is a Python sender: an interpreter running
    a script or a module with --requires-ack, --type question or --type=question among the script's own arguments.
    A shell's script (bash -lc, pwsh -Command, cmd /c) is judged as a command line of its own. Anything else counts
    as no send, so the question is forwarded, at worst twice (pxx)."""
    if _depth > 3:
        return False
    if isinstance(cmd, list):
        commands = [[a for a in cmd if isinstance(a, str)]]
    elif isinstance(cmd, str):
        try:
            commands = _simple_commands(cmd)
        except ValueError:
            return False                    # unparseable: count no send, so a real question is still forwarded
    else:
        return False
    for tokens in commands:
        i = 0
        while i < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[i]):
            i += 1                          # FOO=1 python ...: leading assignments only
        words = tokens[i:]
        if not words:
            continue
        program = os.path.basename(words[0].replace("\\", "/")).lower().removesuffix(".exe")
        if program in SHELLS:
            script = _shell_script(words[1:])
            if script is not None and asks_on_mesh(script, _depth + 1):
                return True
            continue
        if not _PYTHON.fullmatch(program):
            continue
        own = _script_args(words[1:])
        if own and _asks(own):
            return True
    return False


def _js_string(text: str, start: int):
    """The JS string literal at `start` ("...", '...' or `...`), decoded, or None."""
    if start >= len(text) or text[start] not in "\"'`":
        return None
    quote, i, out = text[start], start + 1, []
    escapes = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", "'": "'", '"': '"', "`": "`"}
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2:i + 6] or ""):
                out.append(chr(int(text[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append(escapes.get(nxt, nxt))
            i += 2
            continue
        if ch == quote:
            return "".join(out)
        out.append(ch)
        i += 1
    return None


def _js_code_only(code: str) -> str:
    """`code` with its comments blanked and its string literals' contents blanked (quotes kept), every character
    still at its index, so a search in it finds only code: `// tools.exec_command({cmd: ...})` and a string that
    merely names exec_command are not calls (pxx B)."""
    out, i, n, quote = list(code), 0, len(code), None
    while i < n:
        ch = code[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                out[i] = out[i + 1] = "_"
                i += 2
                continue
            if ch == quote:
                quote = None
            elif ch != "\n":
                out[i] = "_"
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
        elif code.startswith("//", i) or code.startswith("/*", i):
            if code[i + 1] == "/":
                end = code.find("\n", i)
                end = n if end < 0 else end
            else:
                end = code.find("*/", i + 2)
                end = n if end < 0 else end + 2
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def tool_commands(payload) -> list:
    """The shell commands one tool-call payload ran (strings or argv lists); [] when it holds none it can read."""
    if not isinstance(payload, dict):
        return []
    kind = payload.get("type")
    found = []
    if kind == "custom_tool_call" and isinstance(payload.get("input"), str):
        code = payload["input"]
        bare = _js_code_only(code)
        for m in _EXEC_CMD.finditer(bare):
            key = _CMD_KEY.search(bare, m.end(), m.end() + 4000)
            if key:
                cmd = _js_string(code, key.end())
                if cmd is not None:
                    found.append(cmd)
    elif kind == "function_call" and isinstance(payload.get("arguments"), str):
        try:
            args = json.loads(payload["arguments"])
        except ValueError:
            return []
        if isinstance(args, dict):
            found += [args[k] for k in ("command", "cmd") if isinstance(args.get(k), (str, list))]
    elif kind == "local_shell_call" and isinstance(payload.get("action"), dict):
        cmd = payload["action"].get("command")
        if isinstance(cmd, (str, list)):
            found.append(cmd)
    return found


def _exit_zero(value) -> bool:
    return type(value) is int and value == 0             # never True or False, which compare equal to 1 and 0


#: A Codex CLI exec_command output as plain text, as HQB's rollouts hold it (a reviewer d29a54, 40 rollouts):
#: Chunk ID / Wall time / Process exited with code N / Original token count / Output:, then the stdout.
_EXEC_TEXT = re.compile(r"\A(?:Chunk ID: [^\n]*\n)?Wall time: [^\n]*\nProcess exited with code (-?\d+)\n"
                        r"(?:Original token count: [^\n]*\n)?Output:\n(.*)\Z", re.S)


def sole_answer(output):
    """The sender's ONE JSON answer in a tool call's output, parsed strictly, or None (pxx R2-1: a scan for any
    JSON object took a receipt quoted inside other text). Three wrapper shapes are known: a shell tool's output string
    {"output": <stdout>, "metadata": {"exit_code": 0}}; the same tool's plain text, "Process exited with code 0" and
    then "Output:" (_EXEC_TEXT); and a Codex exec output, a list of input_text items of which exactly one is an
    exec_command result {"exit_code": 0, "output": <stdout>, ...}. The stdout, stripped, must be exactly one JSON
    object, at most OUTPUT_LIMIT bytes. Any other shape, or any exit code but 0, is no answer."""
    if isinstance(output, str):
        try:
            outer = json.loads(output)
        except ValueError:
            m = _EXEC_TEXT.match(output)
            if not m or m.group(1) != "0":
                return None
            outer = {"output": m.group(2), "metadata": {"exit_code": 0}}
        if not isinstance(outer, dict) or not isinstance(outer.get("output"), str):
            return None
        meta = outer.get("metadata")
        if not isinstance(meta, dict) or not _exit_zero(meta.get("exit_code")):
            return None
        stdout = outer["output"]
    elif isinstance(output, list):
        results = []
        for item in output:
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(text, str) or len(text) > OUTPUT_LIMIT * 2:
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if isinstance(obj, dict) and "exit_code" in obj and isinstance(obj.get("output"), str):
                results.append(obj)
        if len(results) != 1 or not _exit_zero(results[0]["exit_code"]):
            return None
        stdout = results[0]["output"]
    else:
        return None
    if len(stdout.encode("utf-8", "replace")) > OUTPUT_LIMIT:
        return None
    try:
        answer = json.loads(stdout.strip())
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


def bound_receipt(output, intent, coordinator) -> bool:
    """True when a send's own output is a receipt for THIS send, an ask, to THIS coordinator (pxx R2-2: a
    receipt for a send to another peer, or for another op, was taken as the turn having asked Bxxxxx's question). The
    call's own --to or --cc must name the coordinator, which only picks the candidate call: argv can mislead (pxx
    216b4e R3-2: `--body '--requires-ack'` reads as the flag, though the client's parser makes it the body). So the
    SIGNED VALUE decides, as both client send paths print it (node.send, node._op_sent): its one JSON answer must pass
    receipt_problem, carry no nonzero exit_code, match the call's --op-id when it names one, and carry the signed
    value, which must name the coordinator in its to or cc and be an ask (requires_ack true or type question). Labels
    compare exactly, as the server keeps them lowercase. A receipt without the value (kxxx_send's summary, say) is
    no receipt, so the question is forwarded, at worst twice. A script that prints a forged receipt is not caught
    here; only the op journal could tell (a named limit, pxx R3-1)."""
    want = str(coordinator or "").strip()
    if not want or want not in intent["to"] + intent["cc"]:
        return False
    obj = sole_answer(output)
    if obj is None or receipt_problem(obj) is not None:
        return False
    if "exit_code" in obj and not _exit_zero(obj["exit_code"]):
        return False
    if intent["op_id"] is not None and obj.get("op_id") != intent["op_id"]:
        return False
    value = obj.get("value")
    if not isinstance(value, dict):
        return False
    named = [x for field in ("to", "cc") if isinstance(value.get(field), list) for x in value[field]]
    if want not in named:
        return False
    return value.get("requires_ack") is True or value.get("type") == "question"


class TurnWatch:
    """The rollout read incrementally: complete lines only, a replaced or shrunk file read again from the start.
    Collects each finished turn as {"turn_id", "at", "message", "mesh_sent"}; take them with `finished()`."""

    def __init__(self, path, *, from_end=False, backlog=0, coordinator=None):
        """`from_end`: start near the file's current end, so its history is not replayed (a 60 MB rollout read 4 MB
        at a time would otherwise replay old turns as new). `backlog` bytes before the end are read back, from the
        next line start, so a turn that ended while nothing watched is still seen; MAX_AGE_S keeps old ones out."""
        self.path = path
        self.offset = 0
        self.file_id = None
        self.turn_id = None
        self.mesh_sent = False
        self.backlog = backlog
        self.attempts = {}
        self._done = []
        #: The coordinator a send must name for its receipt to count (pxx R2-2); None counts no send at all.
        self.coordinator = coordinator
        self.pending = {}                       # call id -> its send's intent, while its output is not read yet
        self.seen_calls = set()                 # every call id this turn: one used twice binds nothing
        if from_end:
            try:
                st = os.stat(path)
                self.offset, self.file_id = self._start(st.st_size), (st.st_dev, st.st_ino)
            except OSError:
                pass

    def _start(self, size):
        """Where reading starts in a file of `size` bytes: `backlog` before its end, moved to the next line start."""
        offset = max(0, size - self.backlog)
        if offset == 0:
            return 0
        try:
            with open(self.path, "rb") as f:
                f.seek(offset - 1)
                buf = f.read(MAX_LINE + 1)
        except OSError:
            return size
        cut = buf.find(b"\n")
        return size if cut < 0 else offset + cut       # buf[0] is the byte before `offset`: a line start stays put

    def update(self):
        try:
            st = os.stat(self.path)
            file_id = (st.st_dev, st.st_ino)
            if file_id != self.file_id or st.st_size < self.offset:
                # A replaced or shrunk rollout is read like a start: its backlog only, never all of it from 0
                # (pxx P3 2: that forwarded every old asking turn not yet in the ledger).
                self.file_id, self.turn_id, self.mesh_sent = file_id, None, False
                self.pending, self.seen_calls = {}, set()
                self.offset = self._start(st.st_size)
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                buf = f.read(min(READ_LIMIT, max(0, st.st_size - self.offset)))
        except OSError:
            return
        cut = buf.rfind(b"\n")
        if cut < 0:
            if len(buf) >= READ_LIMIT:          # one line longer than a whole read: skip past it
                self.offset += len(buf)
            return
        for raw in buf[:cut].split(b"\n"):
            if len(raw) <= MAX_LINE:
                self._line(raw)
        self.offset += cut + 1

    def _line(self, raw):
        """One rollout line. A turn counts as having asked on the mesh only when a send-like call's OWN output (the
        same call_id) is a receipt bound to that send and the coordinator (bound_receipt): a send that was refused,
        failed, never answered, or went to someone else does not count, so the turn's question is forwarded, at worst
        twice (pxx B, 6ca3d4 R2-1 and R2-2). Every call line is read for its id, so one id used twice is seen."""
        task = b'"task_started"' in raw or b'"task_complete"' in raw
        marker = any(m in raw for m in CALL_MARKERS)
        output = bool(self.pending) and b'_output"' in raw
        if not (task or marker or output):
            return
        try:
            d = json.loads(raw)
        except ValueError:
            return
        p = d.get("payload") if isinstance(d, dict) else None
        if not isinstance(p, dict):
            return
        kind = p.get("type")
        if d.get("type") == "event_msg" and kind == "task_started":
            self.turn_id = p.get("turn_id") if isinstance(p.get("turn_id"), str) else None
            self.mesh_sent, self.pending, self.seen_calls = False, {}, set()
        elif d.get("type") == "event_msg" and kind == "task_complete":
            tid = p.get("turn_id") if isinstance(p.get("turn_id"), str) else self.turn_id
            msg = p.get("last_agent_message")
            self._done.append({"turn_id": tid, "at": d.get("timestamp"),
                               "message": msg if isinstance(msg, str) else "", "mesh_sent": self.mesh_sent})
            self.turn_id, self.mesh_sent, self.pending, self.seen_calls = None, False, {}, set()
        elif isinstance(kind, str) and kind.endswith("_output"):
            intent = self.pending.pop(p.get("call_id"), None) if isinstance(p.get("call_id"), str) else None
            if intent is not None and bound_receipt(p.get("output"), intent, self.coordinator):
                self.mesh_sent = True
        elif kind in ("custom_tool_call", "function_call", "local_shell_call"):
            call_id = p.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                return
            if call_id in self.seen_calls:              # two calls under one id: neither output stands for a send
                self.pending.pop(call_id, None)
                return
            self.seen_calls.add(call_id)
            if any(b in raw for b in ASK_BYTES):
                commands = tool_commands(p)
                intent = sole_send_intent(commands[0]) if len(commands) == 1 else None
                if intent is not None:
                    self.pending[call_id] = intent

    def finished(self):
        out, self._done = self._done, []
        return out


class LedgerCorrupt(RuntimeError):
    """The ledger could not be read as one; nothing is sent and nothing is written over it."""


def _aged(row, now) -> bool:
    """Whether a row is past LEDGER_RETAIN_S by its own time. A row whose time cannot be read is not aged."""
    at = _when(row.get("at"))
    return at is not None and (now - at).total_seconds() > LEDGER_RETAIN_S


def _row_problem(row) -> str | None:
    """Why `row` is not a ledger row this module writes, or None: a known state and the op id that state needs."""
    if not isinstance(row, dict):
        return "not an object"
    state = row.get("state")
    if state not in FINAL + HELD:
        return "state %r" % (state,)
    if not isinstance(row.get("op_id"), str) or not row["op_id"]:
        return "no op_id"
    if state in ("sending-pointer", "unknown-pointer") and not isinstance(row.get("pointer_op_id"), str):
        return "no pointer_op_id"
    if state == "sending-declined" and not isinstance(row.get("declined_op_id"), str):
        return "no declined_op_id"
    return None


def _when(stamp):
    """A timezone-aware time from an ISO-8601 stamp, or None."""
    try:
        then = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return then if then.tzinfo is not None and then.utcoffset() is not None else None


class Ledger:
    """turn_id -> what was done, so a turn is forwarded once, across restarts (F3: durable, never reset)."""

    def __init__(self, path):
        #: The clock pruning judges ages by: check() sets the check's own time, so a test's fixed clock holds.
        self.path, self.rows, self.corrupt, self.now = path, {}, None, None
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return                              # a first run
        except OSError as exc:
            self.corrupt = "unreadable (%s)" % type(exc).__name__
            return
        try:
            rows = json.loads(data)
        except ValueError:
            self.corrupt = "not JSON"
            return
        if not isinstance(rows, dict) or not all(isinstance(k, str) for k in rows):
            self.corrupt = "not a map of rows"
            return
        for tid, row in rows.items():
            why = _row_problem(row)
            if why:                             # visible, never read as a turn already sent (pxx C)
                self.corrupt = "row %r is not a ledger row (%s)" % (tid[:40], why)
                return
        self.rows = rows

    def has(self, turn_id):
        return turn_id in self.rows

    def held(self):
        """The rows whose operation's outcome is not known, oldest first."""
        return sorted(((tid, row) for tid, row in self.rows.items() if row.get("state") in HELD),
                      key=lambda kv: str(kv[1].get("at", "")))

    def open_questions(self):
        """The questions not yet in front of the coordinator: held ones, and final ones that were never sent (a
        pointer refused too, a server refusal) until they are LEDGER_RETAIN_S old, judged by the clock rather than by
        the next write's prune (pxx INFO). The status keeps them failing, so a later turn that went out never
        hides one (pxx R2-5)."""
        current = self.now or dt.datetime.now(dt.timezone.utc)
        return [tid for tid, row in self.rows.items()
                if row.get("state") in HELD or (row.get("state") in UNSENT and not _aged(row, current))]

    def put(self, turn_id, row):
        """Write the ledger with `row` for `turn_id`. The rows in memory change only once the file has: a write that
        fails leaves no phantom row behind (GitHub Codex on #549). The file is written through a fresh mkstemp file
        (0600 whatever an old .tmp was), fsynced, then replaced (codex-pxx-cli F)."""
        if self.corrupt:
            raise LedgerCorrupt("the ledger %s is %s" % (self.path, self.corrupt))
        rows = dict(self.rows)
        if turn_id not in rows and len(rows) >= LEDGER_MAX_ROWS:
            self._prune(rows)
            if len(rows) >= LEDGER_MAX_ROWS:
                raise LedgerCorrupt("the ledger holds %d rows, its limit; none is dropped" % len(rows))
        rows[turn_id] = row
        self._prune(rows)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(self.path) + ".", suffix=".tmp",
                                   dir=os.path.dirname(os.path.abspath(self.path)))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(rows, sort_keys=True).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.rows = rows

    def _prune(self, rows):
        """Drop final rows older than LEDGER_RETAIN_S from `rows`. A held row, or one whose time cannot be read,
        stays."""
        current = self.now or dt.datetime.now(dt.timezone.utc)
        for tid in [t for t, row in rows.items() if row.get("state") in FINAL]:
            at = _when(rows[tid].get("at"))
            if at is not None and (current - at).total_seconds() > LEDGER_RETAIN_S:
                del rows[tid]


def _hhmm(stamp):
    at = _when(stamp)
    return at.astimezone(dt.timezone.utc).strftime("%H:%MZ") if at else "an unknown time"


#: Why a pointer carries no quote. The ledger keeps a question's hash, never its text, so a resend that reconcile
#: makes (the urgent lane's declined send after a lost answer) can only point at the seat's window.
GATE_UNQUOTED = "Its text was refused by the outbound gate, so it isn't quoted: read the seat's window."
RECOVERED_UNQUOTED = ("It went first as urgent, the seat's urgent allowance was spent and that answer was lost, so this "
                      "is the ordinary resend; the ledger keeps no text, so it isn't quoted: read the seat's window.")


def body_for(seat, turn, quoted=True, unquoted=GATE_UNQUOTED):
    head = ("%s ended a turn with a question for Bxxxxx in its own window at %s (turn %s), and the turn sent "
            "nothing on MeshyChat, so nobody may see it." % (seat, _hhmm(turn.get("at")), str(turn.get("turn_id"))[:8]))
    tail = ("Answer with a seat_directive to %s, or an operator_relay for a hard limit: its framing takes either as "
            "Bxxxxx's answer. Forwarded once per turn by the postbox." % seat)
    if not quoted:
        return head + " " + unquoted + "\n\n" + tail + "\n"
    msg = turn.get("message") or ""
    cut = msg if len(msg) <= QUOTE_CHARS else "... " + msg[-QUOTE_CHARS:]
    quote = "\n".join("> " + ln for ln in cut.strip().splitlines())
    return head + " Its last message:\n\n" + quote + "\n\n" + tail + "\n"


class _Job:
    """A Windows job object made BEFORE the sender starts, so that the only call between Popen's return and the
    sender being in the job is AssignProcessToJobObject (GitHub Codex on 9a7d4d7b, pxx: the ctypes imports and
    the prototypes after Popen had widened the window by milliseconds). Ending the JOB ends every process in it, a
    child the sender starts while taskkill walks the tree included: taskkill /T kills only what its one walk saw
    (gate mg602, 11 Nov 2026: the fake sender's grandchild wrote its mark after a 1 s timeout). A child started in
    the instant before the assign is NOT in the job; taskkill /T, which _kill_tree runs first, reaches that one. The
    gap is narrowed, not closed; closing it fully means a suspended start resumed after the assign.
    No kill-on-close: a send that ends normally changes nothing. `handle` is None off Windows or when no job can
    be made, and then taskkill /T alone is used, as before."""

    def __init__(self):
        self.handle, self.assigned, self._k = None, False, None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateJobObjectW.restype = wintypes.HANDLE
            k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k.AssignProcessToJobObject.restype = wintypes.BOOL
            k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k.TerminateJobObject.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            self._k, self.handle = k, k.CreateJobObjectW(None, None) or None
        except Exception:                                  # noqa: BLE001 - no job: the taskkill path stands
            self.handle = None

    def assign(self, proc) -> bool:
        if self.handle is None:
            return False
        try:
            self.assigned = bool(self._k.AssignProcessToJobObject(self.handle, int(proc._handle)))
        except Exception:                                  # noqa: BLE001
            self.assigned = False
        return self.assigned

    def terminate(self) -> bool:
        if self.handle is None or not self.assigned:
            return False
        try:
            return bool(self._k.TerminateJobObject(self.handle, 1))
        except Exception:                                  # noqa: BLE001
            return False

    def close(self) -> None:
        if self.handle is not None:
            try:
                self._k.CloseHandle(self.handle)
            except Exception:                              # noqa: BLE001
                pass
            self.handle = None


def _kill_tree(proc, job=None):
    """End the sender and everything it started: on Windows taskkill /T (while the sender lives, so its walk reaches
    a child started before the job assign) and then the job (every process in it, those started during the walk
    included); its own process group elsewhere; then wait for its exit. With a job, the tree counts as ended only
    when BOTH took (pxx). True only when the TREE kill itself succeeded and the sender's exit is confirmed
    (F4: a leader that exited
    while the group signal failed says nothing about its children). Never raises, and takes at most
    KILL_TIMEOUT_S + REAP_TIMEOUT_S."""
    tree = False
    try:
        if os.name == "nt":
            # taskkill /T FIRST, while the sender still lives: its walk finds a child started before the job assign
            # (not in the job). Then the job ends whatever is in it, a child started during the walk included. Either
            # one succeeding counts; with no job, taskkill alone decides, as before.
            walked = False
            try:
                done = subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=KILL_TIMEOUT_S, creationflags=NO_WINDOW)
                walked = done.returncode == 0
            except (OSError, subprocess.SubprocessError):
                walked = False
            if job is not None and getattr(job, "assigned", False):
                tree = job.terminate() and walked
            else:
                tree = walked
        else:
            os.killpg(proc.pid, getattr(signal, "SIGKILL", 9))
            tree = True
    except ProcessLookupError:
        tree = True                             # POSIX: no process is left in the group
    except (OSError, subprocess.SubprocessError):
        tree = False
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=REAP_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        pass
    return tree and proc.poll() is not None


#: Fields that, when present, must be exactly true: one that says otherwise makes a receipt no receipt.
_MUST_BE_TRUE = ("ok", "sent", "signature_verified", "result_signature_verified", "status_verified")
#: Fields that, when present and not empty, say the operation failed or was refused.
_FAILURE_FIELDS = ("error", "error_code", "refused", "value_mismatch")


def _is_key(key) -> bool:
    """The one msg/ key rule every receipt here is read by."""
    return isinstance(key, str) and key.startswith("msg/") and len(key) > 4 and not re.search(r"\s", key)


def _says_no(obj) -> str | None:
    """What in a JSON answer says the operation did not succeed, or None (pxx A: one strict contract)."""
    for field in _MUST_BE_TRUE:
        if field in obj and obj[field] is not True:
            return "%s is %r" % (field, obj[field])
    for field in _FAILURE_FIELDS:
        if obj.get(field) not in (None, False, "", [], {}):
            return "%s is set" % field
    result = obj.get("result")
    if isinstance(result, dict) and result.get("ok") is False:
        return "result.ok is false"
    return None


def receipt_problem(obj) -> str | None:
    """Why `obj` is not a confirmed send's receipt, or None: one JSON object with status completed and a msg/ key, in
    no state but succeeded, and with nothing in it saying no. classify (which checks the op id before it) and the
    turn watch reading a send's own output both use it."""
    if not isinstance(obj, dict):
        return "not one JSON object"
    if obj.get("status") != "completed":
        return "status %r" % obj.get("status")
    if not _is_key(obj.get("key")):
        return "no msg/ key"
    if obj.get("state") not in (None, "succeeded"):
        return "state %r" % obj.get("state")
    return _says_no(obj)


def classify(rc, stdout, op_id=None, *, overflow=False):
    """(outcome, key, detail) from a sender's exit code and stdout (F1). Never a substring: SENT and REFUSED need the
    one JSON object their contract names, correlated to this op, and anything else is UNKNOWN."""
    if overflow:
        return UNKNOWN, None, "the sender wrote more than %d bytes" % OUTPUT_LIMIT
    try:
        obj = json.loads(stdout)
    except ValueError:
        return UNKNOWN, None, "exit %s, no JSON answer" % rc
    if not isinstance(obj, dict):
        return UNKNOWN, None, "exit %s, the answer is not one JSON object" % rc
    if op_id is not None and obj.get("op_id") not in (None, op_id):
        return UNKNOWN, None, "exit %s, the answer names another operation" % rc     # before any outcome, refusal too
    if (obj.get("refused") and obj.get("sent") is False and "key" not in obj and "status" not in obj
            and obj.get("ok") is not True):
        return REFUSED, None, "refused: %s" % str(obj.get("refused"))[:120]
    problem = receipt_problem(obj) if rc == 0 else "exit %s" % rc
    if problem is None:
        return SENT, obj["key"], obj["key"]
    return UNKNOWN, None, "exit %s, the answer is not a confirmed send (%s)" % (rc, problem)


def _run_bounded(argv, *, cwd=None, timeout, stopped):
    """Run argv hidden, its outputs to temporary files, bounded by `timeout` and the stop flag. Returns
    (rc or None, stdout text, overflow, detail): rc None when it was ended (detail says whether the end was
    confirmed)."""
    outs, job = [], None
    try:
        outs = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]
        job = _Job()                                       # all setup BEFORE the sender runs
        try:
            proc = subprocess.Popen(list(argv), stdout=outs[0], stderr=outs[1], cwd=cwd, creationflags=NO_WINDOW,
                                    start_new_session=os.name != "nt")
        except OSError as exc:
            return None, "", False, type(exc).__name__
        job.assign(proc)                                   # the one call between Popen and the job
        deadline = time.monotonic() + timeout
        while True:
            try:
                proc.wait(timeout=STOP_POLL_S)
                break
            except subprocess.TimeoutExpired:
                if stopped():
                    if _kill_tree(proc, job):
                        return None, "", False, "stopped: the postbox is stopping, the send was ended"
                    return None, "", False, "stopped: the sender did not confirm its exit (pid %s)" % proc.pid
                if time.monotonic() >= deadline:
                    if _kill_tree(proc, job):
                        return None, "", False, "TimeoutExpired"
                    return None, "", False, "TimeoutExpired: the sender did not confirm its exit (pid %s)" % proc.pid
        texts, overflow = [], False
        for o in outs:
            o.seek(0)
            data = o.read(OUTPUT_LIMIT + 1)
            overflow = overflow or len(data) > OUTPUT_LIMIT
            texts.append(data[:OUTPUT_LIMIT].decode("utf-8", "replace"))
        first = next((ln.strip() for ln in (texts[0] + "\n" + texts[1]).splitlines() if ln.strip()),
                     "exit %s, no output" % proc.returncode)
        return proc.returncode, texts[0], overflow, first[:200]
    finally:
        if job is not None:
            job.close()
        for o in outs:
            try:
                o.close()
            except OSError:
                pass


def rate_limited(stdout, op_id=None) -> bool:
    """True when a sender's answer is the server's SIGNED E_RATE_LIMITED refusal of THIS urgent send: the one answer
    the urgent lane resends as ordinary (kxxx, 5). An answer naming another operation is not (it stays UNKNOWN,
    as classify() reads it: GitHub Codex on e70b2c21). Anything else keeps its own outcome; UNKNOWN is never retried."""
    try:
        obj = json.loads(stdout)
    except ValueError:
        return False
    return (isinstance(obj, dict) and (op_id is None or obj.get("op_id") in (None, op_id))
            and obj.get("status") == "failed" and obj.get("result_signature_verified") is True
            and obj.get("error_code") == "E_RATE_LIMITED")


def send_via(argv, to, thread, body, op_id, *, stopped=lambda: False, urgent=False, declined=False):
    """(ok, refused, detail, outcome): run the seat's own sender once, hidden, bounded, and classify its answer (F1).
    `ok` is a confirmed send and `refused` a structured refusal; anything else is UNKNOWN, never retried here (F2).

    Stop-aware (codex-kxxx-cli 28100b, pxx): the sender is polled every STOP_POLL_S; on the stop flag, or past
    SEND_TIMEOUT_S, its whole process tree is ended (F4: reported as ended only when the tree kill took), and the body
    file is removed either way. The sender's output goes to temporary files, never pipes (on Windows a captured pipe
    cannot be closed while the child runs), and at most OUTPUT_LIMIT bytes of each are read (F5).

    The urgent lane (kxxx): `urgent` adds --urgent; the server's SIGNED E_RATE_LIMITED answer to it is the
    outcome RATE_LIMITED, and check() records a second op id before it sends again with `declined` (--urgent-declined),
    so that second send's intent is in the ledger like every other (F2; pxx P2-1)."""
    extra = ["--urgent-declined"] if declined else (["--urgent"] if urgent else [])
    fd, path = tempfile.mkstemp(prefix="parked-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        rc, stdout, overflow, detail = _run_bounded(
            list(argv) + ["--to", to, "--thread", thread, "--type", "question", "--requires-ack", "--body-file",
                          path, "--op-id", op_id] + extra, timeout=SEND_TIMEOUT_S, stopped=stopped)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if rc is None:
        return False, False, detail, UNKNOWN
    if urgent and not declined and not overflow and rate_limited(stdout, op_id):
        return False, False, "the seat's urgent allowance is spent (signed E_RATE_LIMITED)", RATE_LIMITED
    outcome, _key, why = classify(rc, stdout, op_id, overflow=overflow)
    return outcome == SENT, outcome == REFUSED, (why if outcome != UNKNOWN else detail if rc else why), outcome


#: The shape of the one status command this loop runs: the client's read-only `send-status --peek`.
_PEEK_HEAD = ["-m", "tools.meshy_node", "send-status", "--peek", "--base-url"]


def peek_spec(ident):
    """The identity's own read-only op status, BUILT here from its python, client checkout, state file and base URL
    (the fields the postbox's own send drain runs with), never from free-form config: a configured argv could pass
    --peek as another option's value and run the mutating form (pxx R2-4). None when a field is missing."""
    fields = [ident.get(k) if isinstance(ident, dict) else None for k in ("python", "client", "state", "base_url")]
    if not all(isinstance(v, str) and v and not v.startswith("-") for v in fields):
        return None
    python, client, state, base_url = fields
    return {"argv": [python] + _PEEK_HEAD + [base_url, "--state-file", state], "cwd": client}


def status_via(spec, op_id, *, stopped=lambda: False):
    """(state, key, detail) of one operation from the identity's own op journal, READ ONLY (F2). `spec` is what
    peek_spec builds, {"argv": [python, -m, tools.meshy_node, send-status, --peek, --base-url, URL, --state-file,
    STATE], "cwd": client}, and anything else of another shape is never run (pxx R2-4). The op id goes last,
    as send-status takes it (a reviewer 7cb004 P1). Plain send-status may present an unsettled op again, so this loop
    only observes (codex-oxxxxxx-cli 29cd67). An op still unsettled stays UNKNOWN here; the postbox's own `send drain`
    settles it as it does every journalled send. state is SENT (succeeded, this op id, a msg/ key, already_sent true,
    nothing saying no), "held-by-guard" (the client's guard refused it before it was sent), "refused-by-server", or
    UNKNOWN."""
    argv = spec.get("argv") if isinstance(spec, dict) else None
    if (not isinstance(argv, list) or len(argv) != 9 or not all(isinstance(a, str) and a for a in argv)
            or argv[1:6] != _PEEK_HEAD or argv[7] != "--state-file" or argv[6].startswith("-")
            or argv[8].startswith("-")):
        return UNKNOWN, None, "no read-only status command (send-status --peek, built from the identity): not run"
    rc, stdout, overflow, detail = _run_bounded(argv + [op_id], cwd=spec.get("cwd"),
                                                timeout=STATUS_TIMEOUT_S, stopped=stopped)
    if rc is None or overflow:
        return UNKNOWN, None, detail if rc is None else "the status answer was over %d bytes" % OUTPUT_LIMIT
    try:
        obj = json.loads(stdout)
    except ValueError:
        return UNKNOWN, None, "status exit %s, no JSON answer: %s" % (rc, detail)
    if not isinstance(obj, dict) or obj.get("op_id") != op_id:
        return UNKNOWN, None, "the status answer is not for this operation"
    state, key, no = obj.get("state"), obj.get("key"), _says_no(obj)
    if rc == 0 and state == "succeeded" and _is_key(key) and obj.get("already_sent") is True and no is None:
        return SENT, key, key
    if state == OP_HELD_BY_GUARD and obj.get("already_sent") is False:
        return OP_HELD_BY_GUARD, None, "held by the client's guard, never sent"
    if (state == "refused" and obj.get("already_sent") is False and obj.get("error_code") == "E_RATE_LIMITED"
            and obj.get("urgent") is True):                     # send-status --peek's two fields (node.op_peek)
        # An urgent send turned down for the allowance, whose own answer was lost: reconcile starts the declined send
        # (GitHub Codex on e70b2c21), never a final refusal.
        return RATE_LIMITED, None, "the seat's urgent allowance was spent (signed E_RATE_LIMITED)"
    if state == "refused" and obj.get("already_sent") is False:
        return "refused-by-server", None, "refused by the server, never accepted"
    return UNKNOWN, None, "status %r%s" % (state, "; %s" % no if no else "")


def _outcome(got):
    """A send's outcome from what `send` returned: its 4th element, else the (ok, refused) pair."""
    if len(got) > 3 and got[3] in (SENT, REFUSED, UNKNOWN, RATE_LIMITED):
        return got[3]
    return SENT if got[0] else REFUSED if got[1] else UNKNOWN


def _ids(seat, turn, at):
    op_id = "parked." + hashlib.sha256(("%s|%s" % (seat, turn["turn_id"])).encode("utf-8")).hexdigest()[:32]
    return op_id, "parked-%s-%s" % (seat, at.astimezone(dt.timezone.utc).strftime("%Y%m%d"))


def _pointer(cfg, seat, tid, row, turn, send, ledger, current):
    """Send the pointer for a turn whose quoted text was refused; record the outcome. True when it was sent."""
    pointer_op = row["op_id"] + ".pointer"
    row = dict(row, state="sending-pointer", pointer_op_id=pointer_op)
    ledger.put(tid, row)
    got = send(cfg["sender"], cfg["to"], row["thread"], body_for(seat, turn, quoted=False), pointer_op)
    outcome = _outcome(got)
    detail = got[2] if len(got) > 2 else ""
    if outcome == RATE_LIMITED:
        # The urgent lane, as in check() (pxx P2): the pointer again as ordinary under its own op id, which
        # replaces pointer_op_id in the ledger BEFORE the send, so reconcile asks about the send that may have gone.
        row = dict(row, pointer_op_id=pointer_op + "-declined")
        ledger.put(tid, row)
        got = send(cfg["sender"], cfg["to"], row["thread"], body_for(seat, turn, quoted=False), row["pointer_op_id"],
                   declined=True)
        outcome = _outcome(got)
        detail = got[2] if len(got) > 2 else ""
    if outcome == SENT:
        ledger.put(tid, dict(row, state="sent-pointer"))
        return True, detail
    if outcome == REFUSED:                      # nothing shorter is left to send (a reviewer 7cb004 P2-3)
        ledger.put(tid, dict(row, state="pointer-refused", detail=detail))
        return False, "the pointer was refused too, so nothing is sent: read the seat's window (%s)" % detail
    ledger.put(tid, dict(row, state="unknown-pointer", detail=detail))
    return False, "held: the pointer's outcome is unknown: %s" % detail


def reconcile(ident, ledger, *, status, send, now=None, report=lambda tid, detail, attempts: None):
    """Ask the identity's op status for each held operation, at most every RECONCILE_EVERY_S (F2). Returns the turn
    ids it found sent. Nothing is sent again, except the pointer for an operation the client's guard held unsent."""
    cfg = ident["parked_question"]
    seat = cfg.get("seat") or ident.get("name") or "a Codex seat"
    current = now or dt.datetime.now(dt.timezone.utc)
    done = []
    for tid, row in ledger.held():
        checked = _when(row.get("checked_at"))
        if checked is not None and (current - checked).total_seconds() < RECONCILE_EVERY_S:
            continue
        pointer = row.get("state") in ("sending-pointer", "unknown-pointer")
        # An urgent question the server turned down for the allowance went again under its declined op id, recorded
        # before that send (pxx P2-1): that op, not the refused first one, is the one to ask about.
        op_id = row.get("pointer_op_id") if pointer else (row.get("declined_op_id") or row.get("op_id"))
        state, key, detail = status(op_id) if status is not None else (UNKNOWN, None, "no status command is "
                                                                                      "configured")
        row = dict(row, checked_at=current.isoformat())
        if state == SENT:
            ledger.put(tid, dict(row, state="sent-pointer" if pointer else "sent", key=key))
            done.append(tid)
        elif state == OP_HELD_BY_GUARD and pointer:
            ledger.put(tid, dict(row, state="pointer-refused", detail=detail))
            report(tid, "not sent: the client's guard held the pointer too; read the seat's window", row.get("attempts", 1))
        elif state == OP_HELD_BY_GUARD:
            turn = {"turn_id": tid, "at": row.get("at")}
            sent, why = _pointer(cfg, seat, tid, row, turn, send, ledger, current)
            if sent:
                done.append(tid)
            else:
                report(tid, why, row.get("attempts", 1))
        elif state == RATE_LIMITED and pointer and not str(row.get("pointer_op_id", "")).endswith("-declined"):
            # A rate-limited pointer whose answer was lost: as _pointer does when the answer arrives, its declined op
            # replaces pointer_op_id BEFORE the send (F2).
            row = dict(row, state="sending-pointer", pointer_op_id=row["pointer_op_id"] + "-declined")
            ledger.put(tid, row)
            turn = {"turn_id": tid, "at": row.get("at")}
            got = send(cfg["sender"], cfg["to"], row["thread"], body_for(seat, turn, quoted=False), row["pointer_op_id"],
                       declined=True)
            outcome, why = _outcome(got), (got[2] if len(got) > 2 else "")
            if outcome == SENT:
                ledger.put(tid, dict(row, state="sent-pointer"))
                done.append(tid)
            else:
                ledger.put(tid, dict(row, state="unknown-pointer", detail=why))
                report(tid, "held: the declined pointer's outcome is %s (%s)" % (outcome, why), row.get("attempts", 1))
        elif state == RATE_LIMITED and not pointer and not row.get("declined_op_id"):
            # The urgent question's first answer was lost and its op says rate limited: send it again as ordinary,
            # the declined op recorded first, exactly as check() does when the answer arrives (F2).
            row = dict(row, state="sending-declined", declined_op_id=row["op_id"] + "-declined")
            ledger.put(tid, row)
            got = send(cfg["sender"], cfg["to"], row["thread"], body_for(seat, {"turn_id": tid, "at": row.get("at")}, quoted=False,
                                                                  unquoted=RECOVERED_UNQUOTED),
                       row["declined_op_id"], declined=True)
            outcome, why = _outcome(got), (got[2] if len(got) > 2 else "")
            if outcome == SENT:
                ledger.put(tid, dict(row, state="sent"))
                done.append(tid)
            else:
                ledger.put(tid, dict(row, state="unknown", detail=why))
                report(tid, "held: the declined resend's outcome is %s (%s)" % (outcome, why), row.get("attempts", 1))
        elif state == "refused-by-server":
            ledger.put(tid, dict(row, state="refused-by-server", detail=detail))
            report(tid, "not sent: %s" % detail, row.get("attempts", 1))
        else:
            ledger.put(tid, row)
            report(tid, "held: %s; not sent again (%s)" % (detail, op_id), row.get("attempts", 1))
    return done


def check(ident, watch, ledger, *, send=send_via, status=None, now=None,
          report=lambda tid, detail, attempts: None):
    """Forward each newly finished turn that asks the operator and did not ask on MeshyChat. Returns the turn ids
    sent. A turn older than MAX_AGE_S, or one without a valid time, is never forwarded (F7). Each send's intent is
    written to the ledger before it (F2); an UNKNOWN outcome is held there, never sent again, and reconciled through
    `status`. A corrupt ledger stops everything (F3)."""
    if getattr(ledger, "corrupt", None):
        report(None, "the parked ledger is %s: nothing is sent until it is repaired by hand" % ledger.corrupt, 0)
        return []
    cfg = ident["parked_question"]
    seat = cfg.get("seat") or ident.get("name") or "a Codex seat"
    current = now or dt.datetime.now(dt.timezone.utc)
    if hasattr(ledger, "now"):
        ledger.now = current                    # pruning judges ages by this check's own clock
    try:
        sent = reconcile(ident, ledger, status=status, send=send, now=current, report=report) \
            if hasattr(ledger, "held") else []
    except (LedgerCorrupt, OSError) as exc:
        report(None, "the parked ledger could not be written (%s): nothing is sent this check" % exc, 0)
        return []
    turns = watch.finished()
    for i, turn in enumerate(turns):
        tid = turn.get("turn_id")
        if not tid or turn.get("mesh_sent") or not asks_operator(turn.get("message")) or ledger.has(tid):
            continue
        at = _when(turn.get("at"))
        if at is None:
            report(tid, "the turn has no valid time, so its age is unknown: not forwarded", 0)
            continue
        age = (current - at).total_seconds()
        if age < -FUTURE_SKEW_S:
            report(tid, "the turn's time is ahead of this clock: not forwarded", 0)
            continue
        if age > MAX_AGE_S:
            continue                            # read back from before a restart, and too old to be a live question
        op_id, thread = _ids(seat, turn, at)
        body = body_for(seat, turn)
        row = {"at": turn.get("at"), "state": "sending", "op_id": op_id, "thread": thread,
               "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "attempts": 1}
        try:
            ledger.put(tid, row)
        except (LedgerCorrupt, OSError) as exc:
            report(tid, "the intent could not be recorded (%s): not sent" % exc, 0)
            watch._done[:0] = turns[i:]
            break
        got = send(cfg["sender"], cfg["to"], thread, body, op_id)
        outcome, detail = _outcome(got), (got[2] if len(got) > 2 else "")
        if outcome == RATE_LIMITED:
            # The urgent lane: the same question again as ordinary, under its own op id, written to the ledger
            # BEFORE the send (F2), so a lost answer is reconciled against this op (pxx P2-1).
            row = dict(row, state="sending-declined", declined_op_id=op_id + "-declined")
            try:
                ledger.put(tid, row)
            except (LedgerCorrupt, OSError) as exc:
                report(tid, "the declined send's intent could not be recorded (%s): not sent" % exc, 0)
                watch._done[:0] = turns[i + 1:]
                break
            got = send(cfg["sender"], cfg["to"], thread, body, row["declined_op_id"], declined=True)
            outcome, detail = _outcome(got), (got[2] if len(got) > 2 else "")
        try:
            if outcome == SENT:
                ledger.put(tid, dict(row, state="sent"))
                watch.attempts.pop(tid, None)
                sent.append(tid)
                continue
            if outcome == REFUSED:
                ok, detail = _pointer(cfg, seat, tid, row, turn, send, ledger, current)
                if ok:
                    sent.append(tid)
                    continue
            else:
                ledger.put(tid, dict(row, state="unknown", detail=detail))
                detail = "held: %s; not sent again until its status says so (%s)" % (
                    detail or "the outcome is unknown", op_id)
        except (LedgerCorrupt, OSError) as exc:
            # The intent written before the send stays on disk, held, so its status settles it; the turns after this
            # one go back for the next check instead of being dropped (a reviewer 7cb004 P2-3).
            detail = "the ledger could not record the outcome (%s): the intent stays held for its status" % exc
        watch.attempts[tid] = 1
        report(tid, detail, 1)
        # The later turns go back, in order, for the next check, so one stuck sender does not take a batch with it
        # (pxx); the held one stays in the ledger and only its status can move it.
        watch._done[:0] = turns[i + 1:]
        break
    return sent


def run(ident, *, rollout_for, stopped, sleep, note=lambda m: None, set_status=lambda name, **kw: None):
    """The loop: every CHECK_SECONDS, read the identity's rollout and forward parked questions."""
    ledger = Ledger(ident["log"] + ".parked.json")
    cfg = ident["parked_question"]
    # a stop ends a running send too (codex-kxxx-cli 28100b); "urgent": true sends the question in the urgent lane
    send = functools.partial(send_via, stopped=stopped, **({"urgent": True} if cfg.get("urgent") is True else {}))
    # `"status": true` opts in to the identity's own read-only op status, built from the identity (peek_spec), never
    # a configured command (pxx R2-4).
    spec = peek_spec(ident) if cfg.get("status") is True else None
    status = functools.partial(status_via, spec, stopped=stopped) if spec else None
    # A question that never reached the coordinator is published again after a restart: the postbox's status starts
    # empty, and only held rows are reported again by their reconcile (GitHub Codex on #549 at c019e07c).
    started = dt.datetime.now(dt.timezone.utc)
    unsent = sorted(((tid, row) for tid, row in ledger.rows.items()
                     if row.get("state") in UNSENT and not _aged(row, started)),
                    key=lambda kv: str(kv[1].get("at", "")))
    if unsent:
        tid, row = unsent[0]
        set_status(ident["name"], parked_question_failing=tid,
                   parked_question_error="not sent (%s): %s" % (row.get("state"), row.get("detail") or
                                                                 "read the seat's window"),
                   parked_question_attempts=row.get("attempts", 1))
    watch, path = None, None
    while not stopped():
        current = rollout_for(ident)
        if current != path:
            # A new rollout is read from BACKLOG_BYTES before its end, so a turn that ended while the postbox was down
            # is still seen; older history is kept out by MAX_AGE_S and the ledger (pxx P3 1).
            path, watch = current, (TurnWatch(current, from_end=True, backlog=BACKLOG_BYTES, coordinator=cfg["to"])
                                    if current else None)
        if watch is not None:
            watch.update()

            def failed(tid, detail, attempts):
                note("%s: a parked question (turn %s): %s" % (ident["name"], (tid or "-")[:8], detail))
                set_status(ident["name"], parked_question_failing=tid, parked_question_error=detail,
                           parked_question_attempts=attempts)
            for tid in check(ident, watch, ledger, send=send, status=status, report=failed):
                note("%s: forwarded a parked question (turn %s)" % (ident["name"], tid[:8]))
                # A held row, or a final one never sent, is still failing: a later turn that went out does not clear it
                # (a reviewer 7cb004 P2-1, pxx R2-5).
                clear = {} if ledger.open_questions() else {"parked_question_failing": None,
                                                            "parked_question_error": None,
                                                            "parked_question_attempts": None}
                set_status(ident["name"], parked_question_last=tid, **clear)
        if sleep(CHECK_SECONDS):
            return
