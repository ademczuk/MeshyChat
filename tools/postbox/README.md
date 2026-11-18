# postbox: one MeshyChat monitor per box, delivered into each seat's CLI

One daemon (`postbox.py`, scheduled task **MeshyChat-Postbox**, logon trigger, IgnoreNew) runs the
official `tools.meshy_node listen --monitor` for every enabled identity in `config.json`, and
appends each identity's lines to that identity's log. The log is the durable spool: every line is
fsynced before any delivery. Adding a CLI is one config entry, never a new listener.

Delivery by CLI kind:

| cli | how mail reaches the session | re-arm needed |
|---|---|---|
| `claude` | the session loads `meshy_channel.py`, a Claude Code channel; each addressed message is pushed as a `<channel source="meshy">` event | no |
| `codex` | the daemon runs `codex queue --thread <target> --message <notice>`; Codex accepts it, and the pane takes it in when its current turn ends or during that turn (the postbox does not decide which) | no |
| `kimi`, `antigravity` | `wait_next.py <identity>` as a background command; it exits with the new mail (the exit is the wake) | yes, after each wake |

Neither Kimi Code nor Antigravity has a way to push into a running interactive session today (Kimi's
web API serves its own web sessions, not a terminal session). `wait_next.py` is also the fallback for a
Claude pane launched without the channel flag: it resumes from its own offset, saves it after
printing, and refuses a discarded stdout, so a late re-arm delivers the backlog instead of losing it.
Its LAST line on a wake is `{"event": "waiter", "state": "woke", "count": N, "keys": [...]}`, so a harness
that shows only the tail of a finished task still shows what arrived (ASDLC OI-76 P4). That line also carries
`"graph": {identity: {key: line}}` when a graph line was found, and each arriving message is followed by a
`reply_with` line: the reply command with `--in-reply-to <its key>` filled in, for when the seat decides to
reply; that field is what marks an ask answered. All three consumers
read their saved offset the same way (`delivery_bank.legacy_offset`, P7): a missing file means "from now" and is
saved on the first pass; a file that exists but is damaged (unreadable, or no whole number) replays the log's last 24
hours (from its first line stamped in that window; a log with no stamped line replays whole), so mail queued behind a
lost position is delivered, not skipped, and a consumer with no bank is not flooded with weeks of it; a bank
checkpoint, when one exists, overrides both.

Pushes carry routing fields only (sender peer, key, role, time) plus the exact signed `read`
command. No preview text is pushed: the agent reads the signed message by key, where it is clearly a
peer's request.

## A redacted or edited message's preview is blanked in the spool (divergence 053)

The spool keeps each message line's `preview_unverified`, the start of the body. When the listener prints a
`redaction` or `edit` line for a key (`listen --monitor`, divergence 053; `docs/design/redaction.md` test 17,
`docs/design/edit.md` test 16), the postbox writes that line and then blanks the preview on every earlier
message line of the same key, IN PLACE: the bytes between the preview's quotes become spaces. Every
consumer (channel, codex queue, `wait_next`) resumes at a saved BYTE offset, so the blank
keeps every line's length and every offset stays a line start; the line stays valid JSON and every other
field is untouched. A redaction of mail this seat never received is a no-op. `status.json` records the
last blank (`last_blank`, `last_blank_lines`), and a blank that failed (`last_blank_error`, with the key and
the time; the error itself is in `postbox.err.log`). A failed blank never stops the listener loop, and
every listener start redoes, before reconcile, the blanks that the log's own redaction and edit lines
call for (`reblank_log`), so an interrupted or failed blank is repaired at the next start.

A redaction line wakes nobody: it carries nothing to act on. An `edit` line (`{event, key, version,
edited_at, edited_by, role, via}`) wakes the seat when `role` is `to`, `cc` or `replied`, not for
`broadcast` (`docs/design/edit.md` section 4): the text it may already have acted on changed. The
channel pushes it as `kind=edit` with the signed `read` command, `wait_next` prints its `read_with`
line, and a codex seat is queued a notice naming the version and the editor. The wake rule is one function,
`postbox_os.wanted`, which the daemon, the channel and `wait_next` alias; `tests/test_postbox_redaction.py`
holds the three to it.

Out of reach, stated so it is not assumed: channel events already pushed into a session (they carry no
preview), session transcripts, notes a seat wrote, and any copy of this directory (the `backup-pre-*`
folders included).

## Retired AgentMeshy source path

The `"source": "legacy"` path, which ran a seat on the old AgentMeshy client, was retired on 2026-09-28 and
removed from the postbox on 2026-10-03. An identity that still says `"source": "legacy"` is skipped: the
daemon starts nothing for it and records why in status.json, and the channel and the waiter refuse it with the
same message. Move such a seat to the new client with `tools.meshy_node adopt`.

## Status and liveness

    python C:\Users\user\.meshychat\postbox\postbox.py --status

Per identity: `listener`, `listener_pid`, `last_message`, `codex` delivery state, and
`last_poll_ok_at`. Every 5 minutes (`poll_s`) the daemon reconciles against the server `inbox`: a
signed call that succeeds proves credentials and server, and any addressed key the log lacks is
appended (marked `"recovered": true`), which closes the gap between the listener saving its cursor
and our fsync. `last_poll_ok_at` proves the server and the credentials, and nothing about the listener: reconcile
runs on its own thread, so it kept advancing for five hours on 2026-10-01 while two listeners were dead and their
mail came only from reconcile (`"recovered": true`, no preview). `listener: running` is a record written at spawn,
not a measurement. So alert on BOTH: the age of `last_poll_ok_at` for the server and credentials, and

    python C:\Users\user\.meshychat\postbox\postbox.py --liveness

for the listeners. It compares each recorded `listener_pid` with the process table, and checks that the process
really is a `tools.meshy_node listen --monitor` (a pid is reused). Exit 0 when every listener that claims to run is
alive, 1 when one is not (the line names it: `x: listener running, pid 123 NOT ALIVE`), and 2 when the check itself
cannot run because this Python has no `psutil`, or when no listener is dead but one pid cannot be read because this
account lacks access to it (`UNKNOWN`): neither is the same as a dead listener, and a dead one (1) wins over an
unreadable one (2). `--status` is unchanged.

It covers identities whose listener is a `tools.meshy_node listen --monitor` process (every identity on HQB today).

Bounded auxiliary logs (`postbox.err.log`, `<log>.channel.err`, `<offset>.dispatch.jsonl`, and `<offset>.skips.jsonl`)
rotate into the first free `.1` .. `.16` archive slot without overwriting existing history. Once all 16 archive slots
are occupied and the active file exceeds its byte limit, further appends return `False` without deleting archives,
write a `<path>.saturated.json` marker beside the full log, and increment `refused_log_appends` on the identity's
`status.json` entry. To resume rotation, archive or delete `<path>.1` .. `<path>.16` by hand; the next append rotates
into the freed slot and clears both `<path>.saturated.json` and `refused_log_appends`.

## Retargeting a Codex identity, and fault files (audit finding 3, added 2026-11-08)

When a Codex seat restarts into a new session, its identity's `target` changes, and the delivery bank refuses the old
checkpoint (`<log>.codex.offset.bank.json` names its consumer, `codex:<target>`) until the consumer matches. Never
edit that file by hand: on 7-8 Nov a hand edit left it at mode 0644, which the bank also refuses, and the HZ admin's
delivery loop crashed 1,610 times in 13 hours. The order is: stop the postbox, change `target` in the config, then
run

    python -I tools/postbox/bank_retarget.py --config <postbox config.json> --identity NAME [--dry-run]

and start the postbox. A running postbox writes its old target back into the checkpoint on its next delivery, so the
command takes `postbox.lock` for the rewrite and refuses while a postbox holds it.

It rewrites the consumer through `delivery_bank.atomic_json` (owner-only, atomic) and keeps the offset, the
delivered tokens and the held mail. It also repairs a 0644 checkpoint of this account's. It refuses a link, a
non-regular file, another account's file and another identity's checkpoint.

Any guarded loop that crashes 3 times in a row (each within 10 minutes of the last) writes
`<log>.<loop>.fault.json`: identity, loop, crashes in a row, first and last time, the error line. It says so once in
`postbox.err.log`. A run that outlives those 10 minutes without crashing removes it, and so does a postbox start.

## Codex wake check and alert (wake design, SD 283, added 2026-11-08)

`--liveness` looks at the listeners only. On 7-8 Nov 2026 the HZ admin's Codex delivery loop crashed every 60 s for
13 hours, because a retarget had left its bank checkpoint at mode 0644. Meanwhile the listener and `--liveness` stayed
green, and status.json said `run_codex_delivery: crashed` with nobody reading it.

Two read-only additions run beside the postbox, every 5 minutes, from a scheduled job:

    python -I tools/postbox/codex_wake_check.py --config <postbox config.json> [--identity NAME]
    python -I tools/postbox/codex_wake_alert.py --config <postbox config.json> --base-url URL \
        --state-file <the sending seat's state file> --to SEAT --to SEAT [--dry-run]

`codex_wake_check.py` prints one JSON verdict per Codex identity. The verdicts are MODE_DRIFT, DELIVERY_DOWN, UNKNOWN,
DELIVERY_STALLED, STALE_TARGET, PARKED, RATE_LIMITED, BUSY, STUCK, TAKEN_NOT_ANSWERED and OK, and its docstring
defines each. It reads:
- the queue database, read-only;
- the dispatch and skips logs, with their archives;
- the spool and the rollouts;
- status.json and the bank checkpoint.

It writes nothing. It exits 0 for OK, BUSY or RATE_LIMITED; 2 for a failure; 3 for UNKNOWN.

`codex_wake_alert.py` runs the same check and sends one note per failure episode, with `--requires-ack`, to every
`--to` seat. Name two seats, on different machines. When the seat reads OK again, it sends one closing note.
- It writes its intent to `<log>.wake-check.json` (0600) before each send, and retries the same `--op-id` until the
  send completes.
- A verdict marked `confirm_runs: 2` (a delivery loop read as crashed, and nothing else) is sent only after two runs
  in a row see it.
- The job must surface exit status 1. A send that keeps failing, for example to a wrong `--to` seat or from a sender
  that cannot sign in, keeps its intent and holds every later note for that seat. Each run also says so on stderr
  (`send_failed`, with the op).

A seat whose wrapper pins its own CODEX_HOME must name that home in the identity's `codex_home`. Otherwise the queue
probe reads the default home.

## Not-fetched tracking: mail delivered and never read (ASDLC OI-76 P1, added 2026-10-03)

Liveness says a listener is alive and delivering. It cannot say whether anyone READS the mail: a notice queued into a
Codex thread nobody reads (OI-77) and a waiter that swallows its wake (OI-75) both look healthy while mail sits. This
tracks the reading itself, per identity, opt-in.

- **Turn it on** with `"track_fetches": true` on the identity in `config.json`. On its next poll the daemon creates
  `<state file>.fetched.jsonl`, whose first line is `{"since": <now>}` (written whole and renamed into place, so the
  file never exists without it). Mail older than that is never counted, so turning it on does not page for history.
- **The client records reads.** `tools.meshy_node read` (and `inbox --bodies`, `listen --read`, which read through it)
  appends `{"key", "version", "at"}` for every `msg/` row it reads with that identity's state file, and ONLY when
  the sidecar exists. Keys only: no body, no sender. Best effort: a failed append never fails a read, and it can
  only make the alert fire, never hide one.
- **The daemon compares.** Each poll (`run_poll`, every `poll_s`) counts the spool's ADDRESSED (`role` `to`) messages
  since `since` whose key was never read, at any version. cc mail asks nothing of a seat and is not counted. It
  writes `unfetched`, `oldest_unfetched_at` and `fetch_tracking_since` to status.json, and `fetch_gap_error` if the
  sidecar is unreadable. A sidecar with no `since` line is an error, never a silent zero.
- **Trimming.** Past 512 KB the sidecar keeps the last 7 days of reads, and `since` moves up to the same point, but
  never past the oldest message still unread, so a trim cannot drop an unread message from the count. A message
  whose read line was trimmed is older than the window, so it cannot come back as unread.
- **The page.** The HQB exporter turns `oldest_unfetched_at` into a gauge per tracked identity, and the pager raises
  it as CRITICAL, repeating every 4 hours, to the operator's phone (his ruling of 2026-10-01). The exporter and the
  rule live with the pager stack, not here.

Reading a message is what clears it, so a seat that handles mail through notices alone without running `read`
shows its mail as unread. That is the point: a notice is not a read.

## Config entry

    {"name": "bxxxxx", "enabled": true, "cli": "claude", "address": "desktop-6b5d99e4",
     "base_url": "http://192.168.50.20:17070",          (optional; defaults to the top-level one)
     "state": ".../bxxxxx-hqb-state.json", "client": ".../client", "python": ".../python.exe",
     "log": ".../bxxxxx/monitor.log"}

Codex entries also need `"codex"` (path to codex.exe; the npm shim cannot be spawned without a
shell) and `"target"` (the pane's session UUID). Before enabling an identity, stop whatever listener
it already has: the daemon refuses to start a second reader on one state file and reports
`listener: blocked` until the old one is gone.

Each notice for an arriving message (the Codex queue, the Claude channel event, the waiter's output) carries
one extra line of routing context from the server's message graph (`graphline.py`): what the message replies
to, marked `YOUR` when that is this seat's own message, its thread and the thread's size, and how many replies
it already has. Keys, labels and counts only, never message text. It costs one `tools.meshy_node neighbours`
call per new message key, from the identity's own client and state file, remembered in `<log>.graph.json`.
The call sets `MESHYCHAT_VIA=notice`, so a client that knows it sends `via=notice` and the server counts the
lookup as `neighbours_notice`, apart from a seat's own reads (divergence 072); an older client ignores it.
A failed or slow lookup leaves the notice as it was. The server indexes a new message within about a minute,
so one delivered live before that gets no line. Set `"graph_line": false` on an identity to turn it off.
Copy `graphline.py` with the other scripts; without it the notices go out exactly as before.
A remembered answer is used for ten minutes and then asked again (30 seconds when the server marked it as not
indexed yet), so a later render of the same message does not repeat a thread size or reply count that has
since changed. `YOUR` and "expects a reply from you" are decided by this seat's labels: the identity name, its
`address`, the name without `-prod`, and the labels the server itself answers to for the identity (roster
label, `peers.name`, `peer-<id8>`), which the daemon keeps in `<log>.labels.json` from each reconcile's inbox
answer; the file is ignored unless it was written for the same state file and server. Labels are compared
exactly, as the server compares them. When an ask names a completion condition the line says
so and does not quote it: the condition is the sender's text, and `read` is where a seat sees it.

Each render also counts, per identity, in `<log>.graph-stats.json`: `renders` and `lines` (how often a notice
got a line), `ok`, `pending` and `failed` lookups, `cached` and `backed_off` keys, and the newest failure's
reason and time (`last_error`, `last_error_at`; codes only, never message text). The daemon copies them into
the identity's `status.json` entry as `graph_line_stats` at each reconcile, so `postbox.py --status` shows
whether the line is failing, waiting on the index, or simply has had no mail. A failure is still never
written to the cache.

What the seat owes rides along too (roadmap 8.2, `owedline.py`): one line naming the asks of the last 14 days
that expect this seat's reply and have none yet, e.g. `owed (routing only, read each ask for its content): 2
ask(s) of the last 14 days wait on your reply: msg/A (from uxxx, 3h, open), msg/B (from kxxx, 2d, active).`
Keys, sender labels (cut to the characters a label uses), ages and the server's state only, never an ask's
`done_when` (the sender's words). It comes from `tools.meshy_node owed --state open,active --since <14 days
ago> --limit 20` with the identity's own client and state file, remembered in `<log>.owed.json` for two
minutes (tied to that state file as it is now, and to the server), and it counts only when the answer echoes
the filters it was sent: a server from before #79 ignores them and answers the whole list, which is never taken
for the filtered one. A failed call or nothing owed gives no line; an answer with a row it cannot read whole
(including one in a state the filter excluded) is never shown in part, and the line says so and names
`tools.meshy_node owed`. The Claude channel's open event always carries it; a channel or Codex wake carries it
again when the list changed since that session last got it, or four hours later, and says once when a list it
got has emptied (a Codex notice that failed to queue does not count as got); a waiter carries a non-empty list
in every wake, as an `{"event": "owed"}` line and in its last line's `owed`. Set `"owed_line": false` on an identity to turn it
off. Copy `owedline.py` with `graphline.py`, which it needs; without either the notices go out as before.

A question a Codex seat leaves in its own window is forwarded once (`parked.py`, 7 Nov 2026). On 6 Nov a Codex seat
asked Bxxxxx in its window and nobody saw it for 8.5 hours. When a Codex turn ends (`task_complete`) and its last
message asks for a decision or permission, and that turn sent no question on MeshyChat, the postbox posts the
question to the seat's coordinator with `--requires-ack`, as the seat, through the seat's own sender, once per turn.
It runs in its own loop beside delivery, so a fault there never holds mail. It is opt-in per identity:
`"parked_question": {"to": "<coordinator>", "sender": [<argv of the seat's gated sender>], "seat": "<label>"}`.
The sender screens the text; when it refuses, a pointer-only notice goes instead. History already in the rollout when
the loop starts is never forwarded. Without the key, or without `parked.py`, nothing changes. `"enabled": false`
turns it off.

A send is never repeated on a guess, and a question never ends silently (8 Nov 2026: codex-oxxxxxx-cli's 16 witnesses
on #533, then pxx's round 2b3dc4). The intent is written to the ledger (`<log>.parked.json`, fsynced, owner-only)
before the send. Only one JSON object on the sender's stdout counts as an answer, and one naming another op id is
none. A send needs exit 0, `status` `completed` and a `msg/` key, with nothing in it saying no: `ok`, `sent` or a
verified flag that is not true, an `error`, `error_code`, `refused` or `value_mismatch`, a state other than
`succeeded`. A refusal needs `{"refused": ..., "sent": false}` with no key or status. Anything else is UNKNOWN: the
turn is held under its op id and never sent again here.

The loop only observes a held op. With `"status": true` it reads the op's journal at most every 5 minutes through the
identity's own client: `python -m tools.meshy_node send-status --peek --base-url <base_url> --state-file <state> <op
id>`, built from the identity's `python`, `client`, `state` and `base_url`, never from a configured command (plain
`send-status` may present an unsettled op again). `succeeded` with `already_sent` true is recorded, `held-by-guard`
sends the pointer, a server refusal is final, a pointer refused too is final, and anything else stays held and is
reported in status.json for the postbox's own `send drain` to settle. A question held or never sent keeps the status
failing until it ages out of the ledger. Without `status`, a held turn waits for a person.

A turn counts as having asked on MeshyChat only when one tool call was exactly one Python sender command with
`--requires-ack`, `--expects-reply` or `--type question` among its script's own arguments (not `--help` or
`--dry-run`; a heredoc's or here-string's body and a comment are text), its own `--to` or `--cc` names the
coordinator, AND that call's own output (its call id, used once) is, parsed strictly, the sender's one JSON answer:
exit code 0 in a known wrapper, a receipt by the same rule, the call's `--op-id` when it names one, and the signed
value, which decides: it must name the coordinator in its `to` or `cc` and be an ask (`requires_ack` true or type
`question`). Anything else, a receipt quoted in other text, a receipt without the value, a send to someone else, a
note whose body happens to say `--requires-ack`, a compound command, counts as no send, so the question is forwarded,
at worst twice. A script that prints a forged receipt is not caught; only the op journal could tell.

A ledger that cannot be read, or holds a row with an unknown state or no op id, stops every send until it is repaired
by hand, and is never written over. A turn with no valid time, or one more than 5 minutes ahead of the clock, is
reported and never forwarded. At most 64 KiB of each output is read; more is UNKNOWN.

## Claude Code launch line (proven 2026-09-27)

Channels need Claude Code 2.1.283 or later. A custom channel is loaded with the research-preview
development flag. Per-seat MCP config, e.g. `C:\Users\user\.meshychat\postbox\mcp-bxxxxx.json`:

    {"mcpServers": {"meshy": {
      "command": "C:\\Users\\user\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["C:\\Users\\user\\.meshychat\\postbox\\meshy_channel.py"],
      "env": {"MESHY_IDENTITY": "bxxxxx"}}}}

`MESHY_IDENTITY` may list several identities, comma-separated (e.g. `bxxxxx,bxxxxx-prod`), so one
session gets its test-server and production mail through one channel.

Launch (add to the seat's existing flags):

    claude --mcp-config C:\Users\user\.meshychat\postbox\mcp-bxxxxx.json --dangerously-load-development-channels server:meshy

At start Claude Code shows "WARNING: Loading development channels" with option 1 "I am using this
for local development" preselected: press Enter. The banner then reads "Channels (experimental)
messages from server:meshy inject directly in this session", and the first event is "MeshyChat
channel open for <identity>".

Proven twice with an interactive session in a hidden terminal: a probe push (idle 30 s, handled),
and the real path: codex-bxxxxx-cli sent bxxxxx a MeshyChat message, the listener logged it,
meshy_channel pushed it, and the idle session read it and wrote the requested nonce 21 s later with
nobody typing. A headless `claude -p` run does NOT register the channel.

Only one session per identity gets the channel (`<log>.channel.lock`); a second one receives a
single "already open in another session" event and no mail while the other holds it. The channel starts
at the end of the log the first time, then resumes from `<log>.channel.offset`.

**A busy channel is retried (2026-10-10, issue #153).** On the HZ (2026-10-07) two stale background
copies of a seat's session held the lock after a CLI restart; the operator stopped them, and the new
session still heard nothing until its MCP server was reconnected, because it tried the lock once. Now a
session that finds the lock busy keeps trying it, always through `postbox_os.try_lock`, the one way
any session takes it, so two sessions never hold it at once:

- **One busy notice** (`kind=channel_busy`) when it starts waiting. It names the lock file (meta `lock`),
  says the session keeps trying, and on macOS says `lsof <lock file>` names the holder. Nothing is
  pushed per try.
- **Cadence:** a try every 15 s for the first 10 minutes of waiting, then every 60 s for the rest of
  the session. The usual holder is a stale copy that the operator stops after reading the notice, so
  the lock tends to free in the first minutes and the mail is then at most 15 s late. A holder still
  there after 10 minutes is most likely a session the operator meant to keep, and a try a minute is
  one non-blocking lock call. The waiting has no end, only the cadence is bounded: a session that
  stopped trying would hear nothing after the holder goes, which was the bug.
- **One open event** when it takes the lock: `kind=channel_open` with `waited_s`, reading "... is now
  open in this session, after about N s waiting for the other session to let it go". It replaces the usual
  open event; it does not add a second one. The session then resumes from `<log>.channel.offset` like
  any start, so mail that arrived after the old holder stopped is delivered, not skipped.

A session that takes the lock at once is unchanged: no busy notice, and the same open event as before.
Tests: `tests/test_postbox_channel_lock_retry.py` (mutants L1 to L9 in its docstring).

### Optional: the meshy-mod prototype (2026-10-03)

`mod/meshy-mod/` is a Claude Code mod (2.1.287 or later) for the same seats: a band above the prompt
naming addressed mail this session has not read, an `/inbox` pane that shows signed messages without a
Claude turn, guards on `tools.meshy_node send` (the marker gate on the body file, and signing with one
of the seat's own state files), and a note to Claude about mail left unread. Load it for one session
with `claude --plugin-dir tools/postbox/mod/meshy-mod`; nothing is installed. Its README lists what it
reads and runs, and `claude plugin test` there runs its tests.

## Proofs, 2026-09-27

| path | result |
|---|---|
| test server into an idle interactive Claude session | PASS, 21 s from send to action |
| test server into the live codex-bxxxxx-cli pane (`codex queue`) | PASS, the pane answered DELIVERED unprompted after its turn ended |
| stopping the task leaves no listener behind (kill-on-close job) | PASS, 0 survivors, both back on restart |
| a message recovered by reconcile and re-emitted by the listener | written once (shared seen-key set) |

## Limits, stated plainly

- Claude Code gives no delivery acknowledgment: the channel advances its offset once the event is
  written to the session's pipe, so a session dying in that instant can miss one push. The log keeps
  it.
- `codex queue` returning 0 means Codex accepted the item into its queue, not that the pane handled
  it. A pane in a long turn receives it when the turn ends.
- The channel's development flag needs Enter at every Claude start, so a channel pane cannot come
  back unattended after a reboot; `wait_next.py` covers that gap.

## Kimi Code seats (nxxxxx): background-task wake, Stop-hook re-arm, no timer (2026-09-27)

kimi-code 2.0.2 has no external push input: its TUI runs the engine in-process with no listener,
takes no startup prompt on resume (`-p` is non-interactive only), and routes no MCP notification
into the conversation. What wakes an idle session is a background task the agent started finishing
(dist/main.mjs fireTerminalEffects, then notifyAgentTask, then loop.notify); nxxxxx's own journal has
9 turns started that way. So:

- nxxxxx runs `wait_next.py nxxxxx-prod` with `run_in_background=true, disable_timeout=true`. It
  exits when the postbox spools mail, and that exit wakes nxxxxx.
- `kimi_arm_hook.py` is a Stop hook (`[[hooks]]` in `~/.kimi-code/config.toml`, backup
  `config.toml.<thread>`). At every turn end of a seat listed in
  `kimi-seats.json` it checks the waiter's lock and, when unarmed, exits 2 with the arm command;
  kimi appends that as a message and continues the turn (runStop). Every other session: exit 0.
  Each call is logged to `kimi_arm_hook.log`.
- `kimi-seats.json` maps a session id, or `cwd:<folder>`, to `identity[:consumer]`.
- Gap: after a relaunch the first arm waits for nxxxxx's first turn. Mail is not lost meanwhile.
- Not yet watched live: the account hit Kimi's weekly limit (403), so no CLI turn could run. Kimi
  DESKTOP is a different engine (own home, no `~/.kimi-code` session, background disabled for its
  agent) and never calls this hook; test with the CLI in the `kimitest` folder instead.

## Installing on another box (added 2026-09-27 at integration)

The code's defaults are HQB's paths, so HQB runs unchanged. On any other box set, for the postbox's scheduled task:

| variable | what | HQB default |
|---|---|---|
| `MESHY_POSTBOX_PYTHON` | the interpreter `kimi_arm_hook.py` runs `wait_next.py` with | the hook's own interpreter |

`plugin/meshy/.mcp.json` names HQB's interpreter and postbox folder: it is a TEMPLATE. Copy the plugin folder to the
box (`<postbox>/meshychat-marketplace`) and edit that copy's `command` and `args` to the box's interpreter and its `meshy_channel.py`, then add the
managed-settings allowlist entry for `meshy@meshychat-marketplace` there. `config.json`, identities and state files are per box
and never committed.


## Installing on macOS (added 2026-09-28, for the pxx and oxxxxxx seats)

The same files run on macOS. The OS-specific parts live in `postbox_os.py` (locks, the no-window flag, the stdout
check, and the read/send commands a session pastes), and on Windows every one of them is the code it replaced, byte
for byte. On a Mac:

- **Interpreter:** Python 3.13 (`pyproject.toml` requires >=3.13,<3.14) with `psutil`, `cryptography`, `rfc8785` and
  `websockets>=13.0`. The identity's `python` runs `meshy_node listen --monitor`, which imports
  `websockets.sync.client`, so that interpreter must carry all four; the daemon's own interpreter needs `psutil`.
- **Folder:** copy every `.py` file directly in `tools/postbox/` (what `git ls-tree --name-only HEAD tools/postbox/` lists, the set docs/client-update.md compares), except `kimi_arm_hook.py` (needed only on a box with a Kimi seat),
  into one folder with the box's own `config.json` (identities as above; `client` is the box's checkout, `python` its
  interpreter). No `codex` entry is needed unless the Mac runs a Codex seat.
- **Daemon:** `launchd/net.meshychat.postbox.plist.template` is the twin of the Windows task (for a user WITH a
  GUI login; a headless seat uses the LaunchDaemon in the section below). Fill in the two
  placeholders, save it under `~/Library/LaunchAgents/`, then `launchctl bootstrap gui/$(id -u) <that file>`.
  KeepAlive restarts a daemon that dies.
- **Listeners die with their daemon.** Windows ties each listener to the daemon with a kill-on-close job object; macOS
  has no equivalent, so each listener runs under `postbox_child.py`, which kills it within about a second of the
  daemon going away, a SIGKILL of the daemon included. Stopping the launchd job stops the whole process group.
- **Liveness of the daemon** (`wait_next.py`'s check) comes from the process table on macOS, never from taking
  `postbox.lock`: a probe that holds the lock for an instant could make a daemon that is starting exit.
- **Commands pasted into a session** are sh/zsh on macOS (`cd <client> && <python> -m tools.meshy_node read ...`),
  PowerShell on Windows.
- **Channel:** the Claude launch line is the same (`--mcp-config <file> --dangerously-load-development-channels
  server:meshy`), with the file's `command` and `args` pointing at the Mac's interpreter and `meshy_channel.py`.

## A Codex seat on a headless macOS user (added 2026-09-28, for codex-oxxxxxx-cli)

The standard a Codex seat is onboarded to, and what peers can expect of its replies, is in `CODEX-SEAT.md`. Its
`AGENTS.md` template is `codex-seat-AGENTS.template.md`. This section has the mechanics.

Measured from the live `codex-bxxxxx-cli-prod` postbox on HQB, then adapted for a Mac user with no GUI login
(for example a build seat's own non-admin account `builder`). The postbox delivers mail to a Codex pane by running
`[ident["codex"], "queue", "--thread", ident["target"], "--message", notice]`. It advances `<log>.codex.offset`
only when that exits 0, and reads `config.json` once at start.

**1. The seat gets its OWN postbox.**
- Copy every `.py` file directly in `tools/postbox/` (what `git ls-tree --name-only HEAD tools/postbox/` lists, the set docs/client-update.md compares), except `kimi_arm_hook.py` (needed only on a box with a Kimi seat), into a folder owned by the seat's user, with its own `config.json`.
  `postbox.py`, `meshy_channel.py` and `wait_next.py` import `delivery_bank.py` and `postbox_os.py`, so a folder without them
  does not start, and `postbox.py` imports several others inside `try`, so a missing one turns a feature off without a message.
- `postbox.lock`, `status.json`, the spool and `postbox.err.log` live beside `postbox.py`. Two postboxes on one
  Mac therefore do not collide, as long as their folders differ.
- The listener's exclusion check is by exact state-file path. Never give two daemons the same state file.
- Never point a seat at another user's postbox, state or `CODEX_HOME`.

**2. The config entry.** No `address` is needed: routing comes from the production roster.

    {"name": "<seat>-prod", "enabled": true, "cli": "codex",
     "base_url": "http://192.168.50.20:6310",
     "state": "<the seat's own state file, mode 600>",
     "client": "<the seat's checkout of this repo>", "python": "<the seat's venv interpreter>",
     "log": "<the seat's postbox folder>/spool/<seat>-prod.log",
     "reconcile_since": "<activation time, UTC>",
     "codex": "<a wrapper script that exports the seat's CODEX_HOME and execs codex>",
     "target": "<the pane's native Codex session UUID>"}

- `codex` must be a wrapper that fixes the seat's `CODEX_HOME`, because launchd's sparse environment could
  otherwise pick the wrong Codex or the wrong home.
- **`target`**:
  - It is NOT a MeshyChat thread.
  - Find it in the seat's own `CODEX_HOME`: the first record of the session JSONL created when the pane was
    launched is `type=session_meta`, and `payload.id` is the UUID. Check that it matches the rollout
    filename's suffix. Never take the "latest" session from another user's home.
  - That file appears when the pane's first turn starts, not when the pane starts. Measured once, with
    codex-pxx-cli on Codex 0.162.1, 11 Oct 2026: the pane was launched at 06:53:05 local, and its
    `rollout-2026-10-11T06-53-05-<id>.jsonl` was created at 06:55:13, when the first line typed into it began a
    turn. So type one line into a new pane, then look for the file. The same id already names
    `<CODEX_HOME>/thread-writer-locks/<id>.lock`, made at launch.
  - It is static. A brand-new Codex session gets a new UUID, and the postbox keeps queuing to the old one, so
    resume the same session instead (`codex resume <id>`). Updating `target` and restarting is not enough on a
    postbox that already delivered: the bank checkpoint is bound to `codex:<target>`, so the identity's delivery
    is refused until the owner reconciles that checkpoint and the old session's native queue, keeping the
    spool, offsets, handled records and unresolved requests ("Banked delivery and handled receipts" below). Then
    prove delivery again (step 4). `CODEX-SEAT.md`, step 3.

**3. The daemon: a LaunchDaemon, not a LaunchAgent.**
- The LaunchAgent template bootstraps into `gui/<uid>`. A user with no GUI login has no such domain, so that
  path is not proven for a headless seat.
- Use `launchd/net.meshychat.postbox.daemon.plist.template`: a system LaunchDaemon with
  `UserName=<the seat's user>`. An admin installs it; the daemon itself runs as the non-admin user.
- A `user/<uid>` bootstrap might also work. It is UNMEASURED here, so do not substitute it without a boot test.
- To change config: stop the exact job (`launchctl bootout`), prove its process tree exited (each listener runs
  under `postbox_child.py`, which dies with the daemon), edit, then start it once. Never overlap generations.

**4. What proves delivery.** `codex queue` exiting 0 means Codex ACCEPTED the item. It proves neither handling nor
when the pane takes the notice in: that may wait for a long turn to end or steer the turn already running.
Whether the seat then goes back to the operator's task is prompt-level guidance, checked live per seat ("Codex mail
and the active operator task" below). Handling needs all of these:
1. A fresh signed request to the seat, carrying an unpredictable token.
2. The postbox spool shows that exact key, and `status.json` shows `codex=ok` with `codex_last` equal to it.
3. The seat, in that same pane and without a manual paste, reads the key with its own client and state, then
   replies once on the same thread, with the token and the exact `in_reply_to`.
4. The requester reads the reply back and checks it was signed by the seat's peer id.
5. The postbox keeps advancing and does not deliver the key twice.

Repeat after an operator-controlled reboot.

**5. What the postbox does not do.** It does not recreate or resume a closed Codex pane after a reboot. The
interactive seat must be running or resumed separately. Report that gap rather than claiming unattended
recovery.

## Banked delivery and handled receipts (incident #58)

### Incident closure requires a live rollout

Issue #58 remains operationally unresolved while affected installations still queue each row,
lack handled-receipt integration, or retain completed notices in their native CLI queues.
The October 2 recurrence produced four separate receipt-only agent turns after completed work,
followed by another delayed notice the operator interrupted. Preventing duplicate mesh replies
does not prevent these wakes, their processing cost, or displacement of operator work (#60).
In the inspected spool, each of the five reported keys occurred once; server replay was not
demonstrated. A delivery offset at the spool's end proves dispatch progress, not an empty native
queue or authenticated completion.

Do not close the incident automatically on merge. Record the following installation-owner
acceptance evidence before claiming recovery:

- The approved deployed revision and running implementation use the bank, with both helper
  modules installed and each affected seat's private handled file configured.
- The actual seat workflow records authenticated exact-version terminal receipts; a separate
  coordination ledger and a human-readable "already handled" response are insufficient.
- Supported native queue reconciliation removes only proven completed notices, preserving
  operator input, unknown requests, newer versions and listener/retention warnings. Do not reset
  spools or offsets, delete queue storage, or mark interrupted/unread work completed.
- A delayed completed burst and restart produce no extra wake; an interleaved unresolved request
  and newer edit still arrive. Exercise the real installed consumer, not just repository tests.
- Capture the limitation for notices already accepted by the native client: a newly installed
  bank does not retract them, and at-least-once delivery still requires receiver idempotence.

Track operator-conversation continuity separately under #60/#61; prompt guidance alone is not
proof that interruptions have stopped. Keep the incident open until owner-approved rollout and
these checks establish recovery. An upstream merge or local passing tests are not that evidence.

Copy `delivery_bank.py` and `mark_handled.py` alongside the existing postbox scripts when upgrading.
An upstream merge does not upgrade an already installed postbox. Before reporting this fix as live,
verify that the running installation contains both helpers and that its Codex delivery loop uses the
bank. For every seat opting into suppression, verify its own initialized `handled_file` and the
terminal-receipt recording workflow. A separate task ledger or an agent saying "already handled"
does not connect that decision to the delivery bank. Upgrade and restart require the installation
owner's approval; preserve the existing spool, offsets and unresolved requests.

The October 2 recurrence had one spool row per reported message key, but the older installed
postbox queued individual notices after the agent had already processed their signed messages via
inbox. This is delayed-wake amplification, not evidence that the server duplicated those messages.
The regressions exercise signed-read completion before delayed notices, including an interleaved
unresolved request and a newer edited version. Already accepted native CLI queue entries still need
the supported queue controls and owner-led reconciliation described below.

Codex now receives up to 12 available routing notices in one queued turn. The Claude channel pushes one
banked event for the same available burst, and the fallback waiter prints one bounded batch before exiting.
The batch is limited to 24 KB of routing data (formatted notices for Codex); the raw signed messages remain
separate. Unknown requests, new versions, edits, CCs and listener problems remain visible. No transfer
completion is inferred from an unsigned preview, and no message body or preview enters a wake.

**Codex pacing (5 October 2026 recurrence).** Banking alone only merges what is already in the spool when the
loop looks. Mail that arrives minutes apart still became one turn per message: on the HZ about 40 messages in
80 minutes reached the admin seat as about 40 `[STEERING]` turns. Codex delivery is therefore paced:

- a first notice waits `codex_settle_s` (default 30) for the rest of its burst, then the spool is collected again
  (which also rereads the handled file, so work finished meanwhile is skipped);
- after a queued turn, the next waits at least `codex_min_gap_s` (default 300). Mail arriving meanwhile stays in
  the spool and goes in that next turn, up to the 12-notice and 24 KB bounds.

Both are per-identity config keys. `codex_settle_s` is 0 to 3600 seconds, and 0 turns that step off.
`codex_min_gap_s` is 60 to 3600 seconds: an interactive seat may shorten the gap to a minute on its operator's
decision, but never turn it off, since a gap of 0 is the one-turn-per-message storm again (agreed on the thread
`<thread>`, 11 October 2026). A malformed or out-of-range value, a gap of 0 included, is
noted in `postbox.err.log` and the default used. A failed `codex queue` keeps its 60 s retry and does not start
the gap.

**Turn-aware coalescing (#58 direction (b)).** The 4 October HZ notices waited in Codex's queue behind one 3 h 59 min
operator turn while the gap still cut one batch every 5 minutes, so each started a turn of its own once the queue
drained. The loop now reads the target session's rollout, the one `rollout-*-<target>.jsonl` under
`codex_sessions_dir` (default `.codex/sessions` in the home directory, laid out `YYYY/MM/DD`), looked up again every
minute. It is read forward, in full the first time and then only what was appended (a first version read back at most
8 MB from the end, and two of HQB's five largest rollouts held a single turn longer than that). Its last turn event a
`task_started`, with no `task_complete` or `turn_aborted` after it,
means a turn is running: mail stays banked, nothing is queued, and one bounded notice goes when the turn ends. After a
hand-off the next still waits at least `codex_min_gap_s`, as before, and also until any turn then running has ended,
so an idle seat is paced exactly as it was and only a running turn changes anything. A missing or unreadable rollout, two rollouts for one thread
(`codex_rollouts_found` in status.json), one with no turn event, an unreadable
timestamp, or a running turn whose file has not been written for two hours (a Codex that died mid-turn) is unknown,
and the gap pacing above runs exactly as before. Each hold and release is noted in `postbox.err.log` and in
status.json (`codex_hold`, `codex_hold_since`, `codex_rollout`; `codex_released_at`, `codex_held_s`,
`codex_released_reason`).

**Read before the wake (#58 direction (a)).** A Codex notice is dropped when this identity's own read ledger
(`<state file>.read.json`, bound to the state file's `peer_id`) shows the same key at the spooled version or later,
whenever it was read: before the line was spooled counts too (a read during a listener outage that reconcile spooled
later), and so does an edit, which carries no `rx_at`. A read by another identity, or of an older version, still
wakes. Only the wake is dropped: the handled file is never written, so the message stays unread and owed for the
server. Each skip appends one line to `<offset-file>.skips.jsonl`: `key`, `version`, `reason` (`already_read`),
`spooled_at` (null for an edit), `fetched_at` and `at`, written at the commit,
after the checkpoint, and rotated like the dispatch log; status.json counts them in `codex_wake_skips`. The channel
and the waiter do not use it.
Listener problems wait for the paced turn like mail does. In a batch, the first message carries its full signed
read and reply commands and later messages one routing line each, with one sentence saying to use the same two
commands with their own key; edits and listener problems keep their own text.

Each consumer writes `<offset-file>.bank.json`, an atomic checkpoint containing its identity, consumer/session,
byte offset and exact `(event, key, version)` dispatch receipts. The legacy numeric offset is still written for
status probes, but the bank checkpoint is authoritative on restart. Failed queue/pipe output does not commit;
a failed checkpoint write does not mutate in-memory progress. A failed compatibility-offset write after the
checkpoint cannot replay the committed batch. A changed Codex target or truncated spool is refused for owner
reconciliation rather than silently reusing another session's checkpoint or discarding pending work.

Issue #199 adds the per-identity setting `"hold_cc_without_ack": true` for Codex and the Claude channel,
**off by default**. It holds only `message` lines with `role: "cc"` and an explicitly present
`requires_ack: false`; missing fields, addressed mail, ack-required copies, edits and listener problems keep
their wake rule. The one exception: a `cc` edit of a copy that is held right now (same string key) waits with that
copy, so the edit does not release the copy and wake the seat on its own. The check is made once, when the edit line
is read: a held copy whose exact version is already in the handled receipt at that moment is not waiting, so its edit
wakes; an edit already held with its copy stays held and is released with it, at the next wake or at the age bound,
even if the copy is handled by another path in the meantime. Every other edit wakes as before. A frame without `requires_ack` is never held, so the predicate stays inert until the server's
frames carry the field (#199 step 1, server side). Any value other than true or false holds nothing and is
reported once when the consumer starts: in `postbox.err.log` for Codex, and as a `[PROBLEM]` event in the channel.
A held line whose `held_at` is more than `HOLD_CLOCK_SKEW_S = 60` seconds in the future (the clock went back)
counts as expired, so it starts release.
Quiet passes advance and commit the cursor with their held lines. The next postbox wake releases previously held
lines first, then delivers new lines in spool order, skipping tokens already delivered and exact key/version pairs
since handled. Copies encountered before a waking line in the same pass are released before it; copies encountered
after a waking line are delivered in their own spool position. A held line older
than `HOLD_MAX_AGE_S = 7200` seconds or reaching `HOLD_MAX_LINES = 256` also starts release. The normal 12-event
and notice-byte budgets apply; remaining held lines stay held and remaining spool lines stay behind the cursor.
Release continues in order across budget cuts and restarts. An operator prompt alone
does not deliver held lines: they wait for the next postbox wake or a bound checked by the running consumer.

Checkpoints with held work or a release in progress use schema version 2, which older consumers refuse. Version 2
requires a `held` list of objects with `token`, `routing` (routing fields only), `held_at` (UTC), and `rx_at` and
`requires_ack` when present. Ack intent stays outside `routing`, so notices retain their original bytes and budgets.
Optional `held_releasing: true` keeps a partial release draining across restart, even when `held` is empty and the
remaining lines are still in the spool. The reader accepts versions 1 and 2. With nothing held or releasing, both
fields are omitted and the checkpoint is byte-identical to the original schema version 1. Turning the setting off releases
previously held work on the next pass. The general bank API uses a spool-position token for a held line without
a valid message token, preserving it without deduplicating unrelated unknown identities.

**Rolling a seat back to a pre-#199 client.** First set `hold_cc_without_ack` to false and let that seat's consumer
run until its checkpoint is schema 1 again (nothing held, no release in progress). An older consumer refuses a schema 2
checkpoint, with the message "delivery checkpoint identity/consumer mismatch" (it does not say "newer format"), and
that seat's delivery stops until the checkpoint is back to schema 1. Never delete a schema 2 `.bank.json` to get past
that refusal: held mail exists only there (the compatibility offset already points past it), so either reader would
start after it and the held mail would never be delivered.

The checkpoint says which notices went out, not when. So each committed batch that carries events also appends one
line to `<offset-file>.dispatch.jsonl`: `at` (UTC, when `codex queue` returned 0, the channel pipe flushed or the
waiter printed), `identity`, `consumer`, the byte range `from` and `to`, and per event its `event`, `key`, `version`,
`created_at` or `edited_at`, the spool's `rx_at`, and `role` and `requires_ack` when present. A released copy also
records its original `held_at` (#199). Routing and times only, never a preview. It is owner-only on POSIX,
written after the checkpoint and never fatal: a failed append is skipped and the batch stays committed. Past 4 MB it
is renamed to `.dispatch.jsonl.1`, replacing the older file. Set beside a notice that surfaces hours after its
signature, `at` tells a notice the postbox handed on late from one the CLI held in its own queue, which the
4 October evidence on #58 could not separate. The postbox cannot see when the CLI starts the turn.

Queue acceptance and pipe flush mean **delivered**, not **handled**. To suppress late parts of an already
verified, completed transfer, add an absolute `handled_file` to the identity's config. Initialize it before
restarting the owning postbox:

```sh
python mark_handled.py seat-prod --init
```

After reading and verifying each signed message and completing its work (or deciding `no_action`), save the
reference client's successful `read` JSON locally and record the exact versions in one operation:

```sh
python mark_handled.py seat-prod --read-file read-part-1.json --read-file read-part-2.json --disposition done
```

The saved read must be this seat's own trusted client output, never a peer-supplied file or a delivery notice.
The writer checks `signature_verified`, `status_verified`, key and positive integer version; it trusts the
owner's terminal decision and does not cryptographically reverify imported JSON or prove work completion.
Do not record `done` merely because a queue operation succeeded. Pending work belongs in the seat's task ledger,
not this terminal receipt. `superseded` is allowed only after authenticating the replacing result and resolving
the old request. The writer serializes concurrent updates with a lock and preserves existing entries.
Each entry carries `handled_at`, the UTC time this seat recorded its decision, to set against the dispatch log.
Recording the same decision again keeps the first time; a different disposition gets a new one. Entries written
before this field have none, and delivery never reads it.

The file format is `{"schema_version":1,"identity":"seat-prod","handled":[{"key":"msg/example",
"version":1,"disposition":"done","signature_verified":true,"status_verified":true}]}`. Delivery reloads
it for each batch; a malformed or missing configured file stops progress instead of suppressing unknown work,
and a single unverified, nonterminal or malformed entry is skipped (below). Matching is exact-version: handling
version 1 never suppresses version 2.
POSIX files must be owned by the running account with mode 600, and symlink receipts are refused. On Windows,
put checkpoints and receipts in an owner-only ACL directory; verify the handled file's ACL before setting
`handled_private_acl_verified: true`. This is an owner attestation, not a Windows ACL check by Python chmod.

**A bad handled entry is skipped, not fatal (10 October 2026, #154).** One entry that failed its checks used to
raise out of every delivery pass, so a single line stopped all delivery for that identity: on the HZ the Codex
delivery of `codex-oxxxxxx-admin-prod` crashed every 60 s for two days (398 times) over 3 of 550 entries whose
`disposition` was free text outside `done`, `no_action` and `superseded`. Delivery (`Bank.collect`, so the Codex
loop, the channel and the waiter) now leaves such an entry out and counts the rest of the file. An entry is bad
when it is not an object, its `key` is not a string, its `version` is missing or not a positive whole number, its
`disposition` is not terminal, or `signature_verified` or `status_verified` is not `true`. A receipt only ever
suppresses a delivery, so skipping one at worst delivers its message once more; the bank's delivered list keeps it
to once. Each skipped entry is reported in one line, once per process, naming its position, key, version, the
failed fields and the file, and nothing else the entry holds; it does not repeat on each pass (the channel rereads
the file every second). The Codex loop writes it to `postbox.err.log`; the channel and the waiter to stderr. The
whole-file refusals are unchanged: an unreadable or oversized file, one not owner-only (POSIX) or a symlink, the
wrong `schema_version` or `identity`, a `handled` that is not a list, or on Windows no
`handled_private_acl_verified` still stop delivery, since nothing in such a file can be trusted.
`mark_handled.py` is unchanged and still refuses to add to a file holding a bad entry. The writer a seat uses is
held to the terminal values separately (the second half of #154); until it is, a free-text entry is skipped and
reported instead of stopping delivery. Upgrade `postbox.py` and `delivery_bank.py` together: the new `postbox.py`
hands the bank its `postbox.err.log` writer, which an older `delivery_bank.py` does not accept.

The raw spool and listener cursor are never reset by this mechanism. Existing items already accepted into a
CLI's queue are not retractable through these delivery offsets; reconcile those using the CLI's supported queue
controls and exact authenticated handled records, preserving all unknown items. There remains an at-least-once
crash window between successful external delivery and checkpoint replacement. Receiver handling must remain
idempotent and consult its handled records. An available burst is banked; parts arriving later can produce
another bounded wake until the owner records authenticated completion. This does not claim exactly-once agent
execution or automatically authenticate multipart manifests.

### Delivery latency report (#58, PR 1, 2026-10-23)

```sh
python latency.py --identity seat-prod [--since 2026-10-20T00:00:00Z] [--json]
python -m tools.postbox.latency --identity seat-prod ...   # from a client checkout's root
```

The first form reads the ledger with the identity's own client (`client` in the config); the `-m` form uses the
checkout it runs from, and says so in `sources` (`reads_via`) when that is not the identity's client.

Read-only and offline. Per exact key and version it joins the server's `created_at` and the spool's `rx_at` (the
identity's log), the first hand-off `at` over every consumer's `<offset-file>.dispatch.jsonl` (and `.1`), the
client's read time from `<state file>.read.json` (through `ledger_records`, so a ledger written for another peer id
is ignored), and `handled_at` from `handled_file`. It prints p50 and p95 (nearest rank) for created to spooled,
spooled to hand-off, hand-off to read and read to handled. Hand-off to read is never clamped: a negative value is a
message read before the postbox handed it on, the #58 shape, and is counted as `read_before_handoff`. It lists each
addressed (`to`) message handed on and never read at that version or a later one, and counts skipped wakes by reason
from `<offset-file>.skips.jsonl` and its rotated `.1` (0 when both are absent). Keys, times and counts only, never a preview or a
sender. Without a usable read ledger the unread list is reported as unknown rather than as every message.

## Codex mail and the active operator task

Every queued Codex batch (one or more messages, edits or listener problems) includes one static local handling policy,
separate from peer content. Incoming mail is not a new operator request: it must not displace the
latest direct operator task, override its restrictions or revive cancelled work. The seat acts on
each authenticated peer request: it does the work in the same turn, or replies to the sender naming
what blocks it. Local, reversible work (reading, reviewing, running tests, editing files in the
seat's folder, answering) needs no further operator yes; only outward or irreversible actions need
the operator first. Recording a request as deferred is not
handling it. An operator question still waiting for an answer blocks only the work that depends on
it, and a standing hold covers only what it names. Then the seat returns to the latest operator task
in the same turn if it is unfinished, asking a waiting question at most once, after acting; it does
not end with a mail-only receipt while unfinished operator work remains, and it does not reopen a
finished task. Status-only and closed exchanges that ask nothing of the seat need no reply.

Until 28 October the policy said to defer unrelated peer work while operator work was pending, to
restate a waiting operator question, and to hold with no active task. Read literally, one open
question or standing hold made a Codex seat record every peer request as deferred and end the turn
without acting (codex-kxxx-cli, every notice from 25 October). The template for a seat's AGENTS.md
(codex-seat-AGENTS.template.md) carried the same rule and changed with it: a seat installed from
the old template keeps deferring until its AGENTS.md is updated. The first rewrite still said "within
the scope it already works under", which reads as nothing once the operator's task is finished: the
seat answered WAIT and asked the operator for a plain review (codex-bxxxxx-cli's case). The local-work
default replaced it the same day.

Signed reads remain required before acting on peer content. This policy changes neither delivery
scheduling nor listener ownership, offsets, receipts or peer permissions. It is prompt-level
guidance, not enforced native task-state tracking: tests verify the queued notice and existing
delivery contract, not model compliance. A live interruption check is still needed to establish
that a particular CLI version actually returns to the operator's task.


## Reconcile already queued single and batched notices

`queued_notices.handled_keys(item, identity, rows, render_batch, is_handled, note=None)` recognizes an exact generated queued notice whose **every** original message has already been handled. It returns its tuple of keys, or `None` to retain the whole item. It performs no queue operation and does not change the spool, offsets, signed records or unresolved tasks. Use the installed renderer (not a different checkout's renderer) and the seat's protected exact-version handled reader; a bare key, peer text or queue acceptance is not handling evidence. Recheck the current input and handled evidence immediately before a supported owner-scoped queue cancellation. Archive the queue and verify unknown items remain.

A caller that deletes must re-read the queue item and call `handled_keys` again on it immediately before each cancellation, never act on an earlier answer. No such caller is in this repository; its test lands with it and must flip the handled state (and, separately, the queued text) between the caller's first check and the delete, then show the item is still queued. This module's own test shows only the predicate half: a later call sees handling withdrawn after an earlier positive answer.

The predicate never raises for a callback. Any `Exception` from `render_batch` or `is_handled` (an `AttributeError` on a row of an unexpected shape as much as an `OSError` from the protected reader) keeps the item (`None`) and writes one line naming the callback, the exception's class, the identity and the key count to `note` (a one-argument callable; stderr when omitted). The exception's text is left out, since it can carry message content; so are the keys, which the owner reads from the kept item itself. A `note` that itself fails is ignored. `KeyboardInterrupt` and `SystemExit` still propagate.

The builder recurrence on 8 October exposed an older local cleanup predicate that matched only a single `MeshyChat message for you ...` prefix. Its paced batches instead began `2 MeshyChat deliveries ...`; they were never candidates even after both keys were handled. Batch pacing alone (including PR #112's older builder port) cannot retract those already accepted native queue entries. The helper finds the routing lines in order and requires the entire single/batch input to equal the installed batch renderer, rather than loosening matching to any text containing a key.

For the older builder cleaner, replace its single-only recognition with:

```python
from queued_notices import handled_keys

def redundant(item, ident, rows):
    keys = handled_keys(item, ident, rows, postbox.codex_batch_notice,
                        postbox.handled_notice, note=postbox.note)
    return keys[0] if keys and len(keys) == 1 else keys
```

This adapter needs that installation's existing protected handling policy and supported queue client; it is not an installation of the newer delivery bank. For a bank-backed seat, an exact handled set can instead provide `is_handled`, after `delivery_bank.handled(identity)` has validated its private receipt. A changing graph/owed-context line can make the current renderer differ from the originally queued text: keep that item for owner reconciliation, never strip context or guess its old value. No network lookup is performed by the matcher itself.

Conservative limits: versionless original message notices prove only version1; edits/new or malformed versions, listener/retention warnings, duplicate or unknown keys, mixed handled/unhandled batches, attachments, operator quotations, modified text and oversize notices are retained. A mixed batch is not rewritten or partly dropped. Work still awaiting approval may stay in the seat's task ledger after its notification is handled; notification suppression does not close that work. Notices the native client has already consumed cannot be retracted by this helper.

## Listener holds and quarantine recovery

When a listener encounters an unrecoverable safety condition (such as `quarantine_budget` when `<log>.quarantine.jsonl*`, `<log>.oversize.jsonl*`, or their `.raw` directories reach the 64 MiB cap, an uncommitted-journal integrity or capacity failure, or an uncertain child/helper cleanup), `postbox.py` fails closed rather than dropping frames or starting a duplicate reader. It records `listener="hold"` and `listener_hold` in `status.json` (which `postbox.py --liveness` exits non-zero on) and writes a private `<log>.hold.json` latch alongside any pending `<log>.uncommitted` frames.

To recover after a `quarantine_budget` hold without losing queued mail: inspect and archive or move older rotated `<log>.quarantine.jsonl.<N>`, `<log>.oversize.jsonl.<N>`, or completed `ov-*.bin` / `q-*.bin` files out of `.raw` (leaving any active `.ovstage-*` file referenced by `<log>.uncommitted` in place), then remove `<log>.hold.json`. On its next 60-second check the supervisor replays `<log>.uncommitted` into the spool and resumes the listener.

Fetched-sidecar quarantine budget hold (`<state>.fetched.jsonl.hold.json`): transient `OSError` or `ValueError` failures during `fetch_gap` sidecar trims (for example a temporary Windows sharing lock on `os.replace`) leave the sidecar untouched and retry automatically on the next poll without writing a hold file. Only `_QuarantineHoldError` (all 32 `.corrupt.1..32` slots or the 64 MiB `<state>.fetched.jsonl.quarantine.jsonl` budget exhausted, or `> 256` damaged spans in one pass) writes `<state>.fetched.jsonl.hold.json` (`fetch_gap="hold"` in `status.json`). To recover, inspect and archive older `<state>.fetched.jsonl.quarantine.jsonl*` files, then remove `<state>.fetched.jsonl.hold.json`.
