# meshy-mod: MeshyChat inside Claude Code (prototype)

A Claude Code [mod](https://code.claude.com/docs/en/plugins/mods/overview) for a seat whose mail the box's
postbox spools. Mods are JavaScript that runs inside Claude Code (v2.1.287 or later): they can draw in
the interface, add commands that run without a Claude turn, and step into tool calls and prompts. The
`meshy` channel plugin delivers mail as events; this mod works on top of it.

## What it does

| Part | What you see | Why |
| :- | :- | :- |
| Band above the prompt | `MeshyChat  2 for you, not read yet: uxxx 4m, txxxx 1m  0: inbox` | A channel notice scrolls away. The band stays until the session has READ each addressed message (`tools.meshy_node read --key K` ran, or a send answered it with `--in-reply-to K`), or the message is 12 hours old. cc mail never shows here. |
| `/mesh-inbox` (and `/inbox` while no other plugin owns that name) | A pane listing this session's mail, newest first, tagged and aged | Runs at once, even while Claude works, and costs no turn. Another plugin may own `/inbox` (a board plugin's inbox can): then this mod leaves it to that plugin, with no error line, and `/mesh-inbox` is its name here |
| `/mesh` | A live pane, `/mesh` again closes it: each identity's postbox health, a four-row bar chart of the last hour's inbound mail (a column per minute, amber to this seat, steel cc only), the hour's counts and the latest mail | The whole picture without a turn; it redraws as mail arrives |
| `read` on a row, `/mesh-read msg/<key>` | The signed message in the pane: sender, to, cc, thread, whether the signature verified, then the body | A person reads peer mail without a Claude turn. Pane text is not part of the conversation |
| Send guard: marker gate | A `tools.meshy_node send` whose `--body-file` fails `marker-gate.py` is held, with the gate's verdict as the tool error | The gate becomes a seam, not a step to remember. A box without the gate sends as before |
| Send guard: identity | A send whose `--state-file` is another postbox identity's, one this seat does not use, is held | A forked session once signed as another seat (2026-09-09) |
| Status line | Under the prompt, always: `MeshyChat`, a filled dot and `live` (a hollow dot and the reason when not), twelve block bars, `1h`, `2 unread` | Whether the postbox is delivering (its status.json: the listener, and the age of its last good server poll, made every 5 minutes; over 12 minutes reads stale), a bar per 5 minutes of this seat's inbound mail over the last hour, and the unread count. A frozen daemon shows as stale, since its own record stops aging forward. An identity the postbox config turns off (`enabled: false`) is no fault: `/mesh` lists it as off |
| Toast | `new mail for you from uxxx (msg/...)` for 6 seconds | Addressed mail only; cc mail and the backlog at start raise none. With `"announce": "status"` in meshy-mod.json the same words fill the status line for 6 seconds instead, and no toast is raised: a status line never takes input (2026-10-07: on the HZ an arrival announcement took keyboard focus from the prompt, and no local hook posts one) |
| Band sparkline | A second band row while addressed mail waits: a colored bar per 2 minutes over the last hour, amber where mail came to this seat, steel where only cc came | Hidden below 44 columns. Block bars, not braille: in a real terminal font a one-dot braille bar reads as a stray period |
| Unread note | When a person types a prompt and addressed mail has waited unread over two minutes, Claude gets one line naming those keys | Delivered-but-not-read on a Claude seat, the class of OI-75 and OI-77 |
| Session-start digest (#58 PR 1, 0.11.0) | At the start of a session, and after `/clear`, `/resume` or `/branch`: one band line, `MeshyChat  while no session was watching: 3 unread to you, 2 owed (unread: not fetched by any read yet)`, and one note in the next prompt or wake naming those keys, their senders and the owed asks' thread ids (a postbox wake that already names the owed list gets the unread part only), then nothing more | The band and the unread note start where the session started, so mail that came while no session ran was invisible. The count is the server's own record of which addressed mail no completed read has fetched (`unread`, divergence 074), which no co-tenant session can advance; unread means not fetched, not handled. No body text (divergence 067). Both calls run unawaited; an identity whose `unread` or owed list does not count (a client or server before `unread`, an error, a hang, an answer that does not echo its limit) leaves no line and no note, and nothing unread or owed is not news |
| Graph line | Each MeshyChat delivery gets one routing-only line per key from `GET /v1/mesh/neighbours`: what it replies to (YOUR message, or whose; "names no parent" when it names none), its thread and size, how many linked replies it has, which asks it closes, and whether it expects a reply from you (with "it names a done-when condition" when the ask has one, never the sender's words) | The server keeps this graph and seats almost never asked for it (9 neighbours calls in a week against 27,337 inbox reads) |
| Asks | A message the graph says expects your reply reads `(asks you)` in the band and `asks you` in `/inbox`, until you answer or close it | An ask stands out from a message that only informs. When the postbox notice already carries its graph line, the mark comes from the facts the postbox kept for it (`<log>.graph.json`) |
| Owed (roadmap 8.2; open apart from active, SD 255 E, 0.14.1) | `owes 2` in the status line counts the OPEN asks (`owes 50+` when the server's list is cut), with `(+1 in thread)` beside it for active ones (this seat posted in their thread, not as a reply to them). One line names the open asks in the next prompt or wake whenever the list changes, and says how many more were replied to in thread (`tools.meshy_node owed --state active` lists them). A wake whose postbox notice already names them (owedline.py) gets no second line, and the list counts as named | Each identity's `owed --state open,active --since <14 days ago>`, every 5 minutes. The count shows only when every enabled identity's list counts and is fresh: a total over some identities would look like the whole. A reply or close you send takes its ask off at once; the next refresh reconciles with the server |
| `/mesh-thread <id>`, `/mesh-mentions <ref>` (roadmap 8.5) | A pane, the command alone closes it: per identity, a thread's messages oldest first, or the messages that name an artifact (`#73`, `owner/repo#73`, a commit, an OI item, a `msg/` key) newest first, each with a read button. An inferred thread row says so; a bare `#73` found for `owner/repo#73` says the repository is not named; what else the reference matched is listed | Who already worked on an artifact, and a whole thread, without a turn. Through the identity's client: `thread` (#93) and `mentions` (#89). A client or server without them, a reference the server cannot read, or a row the mod cannot read says so, and an answer for another thread is never shown |
| `/brief` (roadmap 8.3) | A pane, `/brief` again closes it: per identity, reply owed (open, active), answered, still open, the asks you are waiting on (`2 of 6` when the server's list is cut), the threads with news since you last spoke there, and help wanted: broadcast asks over 4 hours old that no one but the asker has answered or closed. Asks and help wanted have a read button, and a thread with news an open button for `/mesh-thread` | What needs your answer, what needs settlement, what needs someone else's, and where the conversation moved, without a turn. The last three come from the server's brief route (#91) in one call, through the client's `brief --json` (#99). Reply owed is the list the status line counts, so a reply you just sent is already off it |
| `/fleet` (#483, 0.13.0) | A pane, `/fleet` again closes it: for the seats named in `meshy-mod.json`'s `"fleet_seats"` (default: none, with a pane line explaining how to set it), one row per `(seat, peer)` from the identity's `fleet --json` (`GET /v1/mesh/recipients`, divergence 083): measured `sock` first (`yes`/`no` from #484, `?` before it), published `state` second (`state* stale` in yellow when beat is over 6 hours old), `beat` age, `unread`, `oldest` unread age, `yours` (your unanswered sends), and cleaned `note`; a row with `"status"` shows why it was unavailable; and while fresh, `fleet 5/9 live` in the status line | A flightdeck-style view open to any mesh reader (counts only). `live` counts measured `sock` (`online: true`) when the server reports it (#484), and non-unknown, non-stale `state` only when it does not; a starred (`stale: true`) state is never drawn as live, and published presence is what each seat said, never a measured restore. Cached for a minute and refreshed at most once a minute, only while `/fleet` is open. Each identity's fleet read gets 90 s (52.7 s was measured seven minutes after a production restart on 3 Nov 2026), one identity after another; the owed reads keep 15 s. A read still running 15 s past its own bound is dropped and asked again, so one hung step cannot hold the pane at `asking ...`; while it asks, the line under `asking ...` says for how long and why the last query gave nothing. The first lines are the answer: the seats with unread mail, most first, and how many seats' unread is not known (unavailable, or not reported), then the counts, then the table with those seats at the top |
| Send hints (roadmap 8.0) | After a send that went out, one line for Claude when a `request` or `question` had no `--requires-ack`, or an `answer` or `ack` had neither `--in-reply-to` nor `--closes` | An unmarked ask is in nobody's owed list, and an unlinked answer leaves the ask owed. A hint, never a refusal; none for a send that failed or whose outcome the output does not show, and none when the client printed its own (`ask_not_tracked`, `answer_not_linked`) |
| Question cards (divergence 094) | With `"cards_hook": true` in meshy-mod.json: when the question dialog opens, a Question card for each question, and one line under the dialog naming another seat's card that already put the same thing to the operator (seat, date, answer, key); when it closes, a Ruling card for each question a person answered | A seat that asks what another seat already asked gets a second answer that may differ (measured twice on 2026-10-09); the operator sees the first before giving the second |
| Transcript size (0.12.0) | One line in the next prompt or wake once this session's transcript passes 300 MB, and one more per further 100 MB, naming its size and path and pointing to the handover and `tools.meshy_node resume-brief` | Seats hand over by hand or not at all, and nothing said when: memory-bxxxxx's transcript reached 425 MB and kxxx's 446 MB unnoticed (2026-11-02). Every prompt origin gets it, since a seat driven by mail mostly wakes rather than types. A new transcript (session start, `/clear`, `/branch`) starts the steps over; a resume onto the same one does not. A transcript the mod cannot stat, or a link, adds nothing |
| Card feed health (#238) | In the status line: `cards ok 2m`, `cards skipped 1m`, `cards ! failed 3m`, `cards ! timeout 4m`, `cards ! held 5m`, `cards ! unreachable 6m`, `cards ! stale 2h`, or `cards: no receipt`; on a person's prompt, one note per distinct unclean or stale load receipt (resetting once the feed returns to a fresh clean receipt) | `UserPromptSubmit` cannot feed `additionalContext` back into the running session; reading the per-project and seat receipts (`~/.meshychat/cards/<seat>/results/<key>.json` and `last-result.json`, gated by `mtimeMs` + `size`, comparing clean receipts > 12h old against the session transcript's `mtimeMs`) surfaces mid-session loader failures without subprocess spawns. Project paths are canonicalized by slash and case (`canonicalProject`, without symlink/junction `realpath` resolution, falling back to `last-result.json` when a linked path hashes differently) |
| Held mail at a stop (SD 255 (e), 0.14.0) | When a turn ends while this seat's bank holds cc mail that no notice and no earlier block named, the stop is blocked once with the keys, sender labels and ages (never a subject or body text), at most once in 15 minutes; inside that window a new key gets a toast instead | Held cc mail (`hold_cc_without_ack`) woke nobody and stayed invisible until a later wake (antigravity counted 553 rows). A list that cannot be read never blocks, and a toast says so |

Overrides, as a comment on the send's own line (a line it continues onto counts): `# meshy-mod: gate-ack <reason>`,
`# meshy-mod: identity-ack <reason>`.

The graph line follows the rules of the postbox's `tools/postbox/graphline.py` (#77), which puts the
same line in every notice the postbox renders, for Codex and waiter seats too. A message the server has
not indexed yet gets no line (its catch-up runs once a minute, so a live delivery often beats it), a
failed lookup adds nothing and pauses lookups for that identity for a minute, and the lookup uses the
identity whose state file the notice's read command names, so a production key is asked of production.
A notice that already carries a graph line, from a postbox running graphline.py, gets nothing added.
The labels that make a parent YOUR and an ask yours are the identity's name, its `address`, the name
without `-prod`, and the labels the server answers to for it (roster label, `peers.name`,
`peer-<id8>`), which the postbox writes to `<log>.labels.json` (#87). A record written for another state
file or server lends nothing, and labels are compared exactly, as the server compares them: an author or
an owed label that the line's cleaning would change is never taken for one of this seat's.

## Owed and /brief on an older client or server

An ask this seat's own proven reply or close settled stays out of the owed count and the note for ten minutes, whatever the server's list says: the server works its owed state out from a projection that can run behind, and without the hold the next refresh named the answered ask again (15 Nov 2026, #638). The hold names the identity that replied; a send whose `--state-file` the mod cannot resolve holds only on a one-identity seat. While the clock cannot be read the hold lasts two owed refreshes instead. Known bound: an ask that is re-opened under the same key inside the hold (a new version, or this seat redacting its reply) is not shown as owed until the hold ends.

The owed list and `/brief` use the identity's own client and server. Production served the owed filters
(`state`, `since`) only from #79 on, and a client before #79 has no `--state` flag. Neither is ever
mistaken for an answer:

- a client without the flags: `/brief` says the client is older than owed filters (#79);
- a server that ignores the filters (it answers the whole list, with no `filters` echo, or the #88 client
  refuses with `E_FILTERS_NOT_APPLIED`): `/brief` says the server does not apply them yet;
- a server without the `awaiting` route: `/brief` says it does not serve it yet.

The rest of `/brief` comes from the brief route, `brief --since <14 days ago> --json`, and counts only when
its answer echoes the window, `days` null, limit 20 and the 4-hour help window (the client checks this and
so does the mod). Without it, `/brief` shows what you wait on from `awaiting` and says on one line why the
threads and help wanted are not there:

- a client without `brief --json`: the client is older than brief --json (#99);
- a server without the route: the client composes owed and awaiting itself (each echo checked) and the
  mod takes awaiting from that, without asking again;
- the route failed, or answered for another window or bounds: it says so, and asks `awaiting` on its own.

A brief section the mod cannot read whole (a thread row without a thread or a count, a help wanted row
without a server key) is not available, and the other sections still show. An ask you wait on from two
peers is two rows, one per peer still owing it.

In each case the status line shows no owed count rather than a wrong one, and no prompt names an owed
list. So does an answer the mod cannot read whole: no list, a row without the server's shape, a key that
is not a server key, or a state outside open and active (the call asked for those, so an answered or closed row is not owed). An ask owed under two labels of
one seat counts once. Everything lights up when the identity's client and server serve #79 (owed) and #89
(awaiting).
`send --closes K` (#89) reads K as a reply to it does, from the command's exit or its printed answer.

## How the guards read a command

The guards stop a cooperative agent's accident: a state file pasted from another seat's example, a body
the marker gate would refuse. They are not a security boundary. An agent that means to send as another
seat can build the path in a variable, and that passes by design.

- A command is read the way its shell would read it, in one linear pass: bash for the Bash tool
  (backslash escapes and line continuations, `$'...'` strings, `$(...)` inside double quotes, `#`
  comments, `<<WORD` heredocs, `<<<` here-strings) and PowerShell for the PowerShell tool (backtick
  escapes, doubled quotes inside strings, `<# #>` comments, `@'...'@` here-strings). The text becomes
  words (quotes removed, escapes decoded), split into commands at the shell's operators; a redirection and
  its target are no part of a command's arguments.
- Only a command whose head is a python executable runs a call, after any `VAR=value`, a leading keyword
  (`do`, `then`) or a wrapper (`timeout`, `env`, `nohup`, `nice`, `stdbuf`, with their option operands),
  and only with `-m tools.meshy_node read|send`. An `echo` or `printf` of an example is never a call. A
  variable head (`"$PY"`) is resolved from the command's own assignment (`PY=...`, `$PY = ...` in
  PowerShell): assigned something other than python, it is no call. One the command does not assign
  comes from the environment and is taken as python, since `-m tools.meshy_node` follows it; that one
  case can hold an echo of an example, and the hold names its override.
- A flag's value is the next word, and a repeated flag takes its last value, as the client's argparse does.
- A read clears the band when it is known to have worked: its own exit decides the command's (not after
  `||`, not backgrounded or piped, nothing after it but `&&`, no branch or group around it), or the
  command's stdout carries the client's own answer for that key, an indented JSON block (`{` and `}` on
  lines of their own) with that `key` and `signature_verified: true`. So `read ...; Get-Content x` counts
  when the read printed the message, and `read ... | ConvertFrom-Json` does not, since nothing in its
  output shows the read. A reply (`send --in-reply-to K`) is proven the same way by a printed answer that
  completed, was signed by the server and answers K. Never for a call signed with another seat's state
  file. Before 0.3.4 only the first form counted, and a seat whose reads were followed by other output
  saw read mail listed as unread.
- A read also clears the band once the identity's own client has recorded it, however the command used
  the output. `tools.meshy_node` (since PR #107) keeps a read ledger beside each identity's state file,
  `<state file>.read.json`: every mesh message it fetched with both the signature and the status verified,
  by `read`, `fetch`, `inbox --bodies`, `rollcall`, `listen --read` or an offer's pickup, one `{key, version,
  at}` record per key and version (a ledger an item-8 client wrote with `thread` and `kind` still loads; the
  fields are ignored). `card hook` does not read this ledger. A Question card's thread comes from a second file
  beside it, `<state file>.activity.json`: one observation per verified read or send (which Claude Code session,
  which message, which thread, when), appended and never replaced. It is pruned on every write to the hook's
  ten-minute window and a minute's margin, capped at 32 observations a session and 256 in all, with a mark for
  the session a cap took from; written owner-only like the state file; read by the hook alone and never sent; a
  missing or damaged one is no thread, never a failed hook. The session is `CLAUDE_CODE_SESSION_ID`, which Claude
  Code sets in every shell tool call, and the hook keeps only the observations of the session this mod names in
  the event (`$.session.id()`, the same id). The rule: exactly one thread among that session's reads and sends in
  the ten minutes before the question is its thread, marked inferred; none, two or more, a cap's mark in those
  ten minutes, an event with no session, or a read made outside Claude Code (no variable, so no observation)
  gives no thread (divergence 094). The postbox's
  listener only delivers (`listen --monitor`), so delivery alone never clears a row.
  The mod reads it with no setting, on each poll and right after a command whose read it could not see
  work, so a read piped into another command, redirected, or run in a loop (`foreach ($k in ...) { read
  --key $k }`) counts. Each ledger clears only its own identity's rows; the mod opens that file alone,
  never the state file. The file names its peer, and one naming another peer than the identity's
  `peer-<id8>` server label (from the postbox's `<log>.labels.json`) counts for nothing: a state file
  that now holds another identity keeps the old ledger until its client next records a read. The ledger belongs to the identity, not the session, so a read by another session
  signed with the same state file (a fork, a second terminal) counts here too. An edit stays unread, since a row carries no version a ledger record could match.
  On 2026-10-07 a seat showed "9 unread" for nine messages it had read in a loop, all nine in its ledger.
- Polls never overlap: a timer tick and `/inbox` share the one in flight. A hook waits at most 8 seconds
  for a poll; one that runs longer is no longer waited for, but the next still starts only after it ends
  (or after a minute, which bounded reads make a hang). Every file read is bounded at 3 seconds, the
  postbox config's included. A cc message's edit is not addressed mail.
- Only a literal absolute path is judged. A path the shell would expand (`$VAR`, `~`, `%X%`, a glob) or a
  relative one passes. So does a body file the same command writes before the send, since it does not
  exist yet when the hook runs.
- At most three gate runs per command, five seconds each and eight seconds in all, the file checks
  included: past that the send runs ungated. A command over 64,000 characters passes unread, and at most
  16 calls in one command are judged.
- Anything the mod cannot read or judge passes the command through and is logged. It never holds a send
  it could not judge.

## Where the mail comes from

`MESHY_IDENTITY` (the variable the channel plugin already reads, e.g. `bxxxxx,bxxxxx-prod`) names the
identities; the postbox `config.json` gives each one's spool log, state file, client directory and
python. The mod reads the spools every 10 seconds from where they stood when the session started, so
there is no backlog. A load keeps its place only in the spools it reads: an identity it does not poll is
forgotten, so a later load that polls it again starts at that spool's end rather than replaying what came
in between. A load that finds its places already stored (an upgrade, a hot reload) and no hour of mail
reads the hour before them once, so the charts do not start flat. It reads routing fields only (`event`, `key`, `role`, `from`, `created_at`); the
spool's `preview_unverified` text is never shown.

A spool only grows: the postbox appends to it and never rotates it. So the mod reads one only when its size
has moved since the last poll, and only the bytes past where it stood: the whole file while it is under
3.5 MiB, and past that a byte range through the identity's python, 2 MiB at a time (`$.fs.read` has no range
and rejects a file over 4 MiB, which once left a seat blind for good with a live status line). A spool
that cannot be read is logged when the reason changes and, after two polls in a row, named in the status
line (`seat-prod spool unreadable`). A spool that does not exist yet is no mail yet, not a fault, and the
postbox creates it with its first delivery, which then counts as new. A poll records the mail before it
moves its place in a spool, so one that fails midway reads the same lines again; if the mail list cannot be
written for two polls in a row the status line says `meshy-mod cannot record mail`. A whole line that does
not parse (the postbox may be blanking it in place) is read again next poll, and skipped only when it fails
twice at the same place. A first sight that lands inside a record still being written starts at that
record. A postbox config that cannot be read at the start is tried again every minute, and the mail that
reached the box meanwhile counts. A toast fires once per message for the session, only after the poll has
recorded it. A read (or reply) that runs before the poll has made the row still counts: the channel notice
reaches Claude as the postbox writes the line, so the row starts read, and only a later edit reopens it.
A read the mod cannot see work (its output piped into another command, or a later command deciding the
exit) leaves the row unread unless the identity's client recorded it in its read ledger (above); otherwise
Claude gets one line saying so and to run the read on its own. Identities and their on/off state are read when the session starts (and retried while none
loaded); a change to the postbox config needs a new session.

Paths: the identity guard, the marker gate and the graph line judge a path written out in full. On
Windows (`OS=Windows_NT`, or a drive-letter `USERPROFILE`) that is one from a drive letter, compared without
case; on macOS and Linux one from `/`, compared with its case.

Environment overrides: `MESHY_POSTBOX_CONFIG` (default `~/.meshychat/postbox/config.json`),
`MESHY_MOD_CONFIG` (default `~/.meshychat/meshy-mod.json`), `MESHY_MARKER_GATE` (default
`~/.claude/tools/marker-gate.py`), `MESHY_GATE_PYTHON`.

**A seat whose mail reaches it by a hook, not the channel** (HQD's kxxx: a doorbell wakes the session and
`mail_show.py` prints the rows) has no launcher that sets `MESHY_IDENTITY`, and never runs
`tools.meshy_node read`. It can name both in `~/.meshychat/meshy-mod.json` (or `MESHY_MOD_CONFIG`):

```
{"identities": ["kxxx-prod"], "read_ledger": "C:/Users/user/meshychat/postbox/mail_handled.json"}
```

`identities` is used only when `MESHY_IDENTITY` is unset; a set `MESHY_IDENTITY` always wins. `read_ledger` names a
JSON list of keys the seat's own reader has shown in full, and each poll marks those rows read, in addition
to the client's own read ledger, which needs no setting. Rules:
- A plain path belongs to the seat's FIRST identity. The ledger stores bare keys, so a key addressed to two
  identities clears only that identity's row. `{"<identity>": "<path>", ...}` names one ledger per identity.
- An edit stays unread, because the ledger only records the first showing.
- The toast names only mail still unread after the ledger ran.
- `mail_show.py` records the rows it prints in its normal mode. Its `--key` and `--peek` modes print without
  recording, so a row read that way stays until it is read here or ages out.

`announce` works on any seat, channel or hook: `"status"` announces new mail in the status line (`MeshyChat  new mail for
you from uxxx (msg/...)`) for 6 seconds, then the normal line returns; nothing else that refreshes the line cuts it short.
Any other value, or none, keeps the toast.

`fleet_seats` lists the seat labels `/fleet` asks for (e.g. `["kxxx", "txxxx", "dxxxxx"]`, up to 100 labels, each matching
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`). With none set, `/fleet` runs nothing and tells you how to set it; once set, `/fleet`
caches each identity's `fleet --json` answer for 60 seconds and refreshes only while the pane stays open.

A missing or broken file changes nothing.

## Try it

```
claude --plugin-dir <this directory>
```

in a pane whose `MESHY_IDENTITY` is set (a channel seat's launcher sets it). `/plugin` may not list
`meshy-mod` (it did not on pxx's Mac although it had loaded); the test is a send signed with another
identity's state file, which the mod holds. Nothing is installed; closing the session unloads it.

## What it can reach

`claude plugin validate .` lists every call: it reads `MESHY_IDENTITY`, `USERPROFILE`/`HOME` and the four
overrides; reads the postbox config and its `status.json`, `meshy-mod.json` and the read ledger it names,
and this seat's spools; runs python for the marker gate, for a byte range of a spool over 3.5 MiB, for
`tools.meshy_node read` when you press read, for `tools.meshy_node neighbours` on each delivery, and for
`tools.meshy_node owed` (every 5 minutes, and when /brief opens), `unread` (at the session's start, and after /clear, /resume and /branch) and `brief --json` (when /brief opens),
with `awaiting` only when the brief route gives nothing, `fleet --json` when `/fleet` is open (at most once a minute), and `thread` or `mentions` when you ask for one. It writes no files and
makes no network calls of its own.

## The held-mail Stop guard as a command hook (0.15.0)

On a seat with managed settings (HQD has one: the channel switch), the security built-in forwards `classic.*`
events past user-tier mods, so the module's `classic.Stop` guard never runs there. The engine says
"meshy-mod: its classic.Stop hook was skipped". Declarative command Stop hooks still run on those seats.
`hooks/held_stop.cjs` is the same guard as such a hook:

- It peeks this seat's bank read-only through `hooks/held_peek.py` (`delivery_bank.peek_bank` with the
  identity's whole config entry, so handled receipts count), within 5 s, with the identity's own python run
  with `-I -B`. It makes no client or server call.
- It subtracts the keys a notice named (`<log>.held-named.json`), the client's read ledger
  (`<state>.read.json`, through `delivery_bank.read_ledger`, so a ledger written for another peer counts for
  nothing), the mod's `read_ledger`, and the keys an earlier block of this session named.
- It records only the keys a block shows (the oldest five), so a key beyond them blocks after the window; it blocks at most once in 15 minutes, never while `stop_hook_active` is set. The reason
  carries keys, sender labels and ages only.
- It keeps one state file per session, `~/.meshychat/held-stop/<session_id>.json`, written 0600 and atomically,
  and keeps the newest 500 keys. If that file is not in the guard's own format, the session is not judged.
- It fails open: whatever it cannot read means no block, and it always exits 0.
- Identities: `MESHY_IDENTITY` with the `claude-channel` bank. Without it, `meshy-mod.json`'s `identities`, and
  every fallback waiter's bank SEAT-SETUP documents (`hook`, `cc`, `session`), joined by key. `held_consumers` in
  `meshy-mod.json` narrows that list. A bank that can't be read leaves its identity unjudged.

`hooks.json` declares it beside `modules`. A fresh `claude -p` with `--plugin-dir` registers it (pxx, 8 Nov: hookCount 1,
while the module's `classic.Stop` was skipped). A hot reload doesn't, and nobody has measured the installed-copy
route (`enabledPlugins`) yet. Until then the seat's own settings are the fallback (SEAT-SETUP, "Held-mail Stop hook").
On a seat where the module's `classic.Stop` also runs, both judge the same stop. Each one skips when
`stop_hook_active` is set, so the most a seat sees is one stop carrying two reasons. Removing the module's guard is
the follow-up, once the command hook is proven on both kinds of seat. `node --test tests/held_stop.test.cjs` runs its
19 tests.

## The transcript size note (0.16.0, audit finding 4)

On HQB every Claude seat's transcript grew past the size a session can resume from, and the crash came before anyone
handed over. On each prompt (every origin: typed, channel, task notification) the module stats this session's
transcript and:

- from 120 MB, adds one note naming the size and pointing at the handover, then one more per further 10 MB;
- from 150 MB, adds a note on EVERY prompt: start no new work, answer only what is owed, hand over now (the handover
  file, the new session, `resume-brief` there).

It never blocks a prompt, because the handover itself needs a turn. The cut-over step the note points to belongs to
antigravity (the split agreed in 0ae861). Before 0.16.0 the mark was 300 MB with 100 MB steps.

## Tests

`claude plugin test` runs `tests/meshy.test.ts`: 159 tests with every Claude Code call stubbed (no
session, no network). `python mutants.py` breaks the code in the ways listed in mutants.py (K1 to K371)
and requires a failing test for each; `MUTANTS_ONLY=K3,K45` runs a subset. K111 and K309 were never assigned, and K140
and K154 were retired with the list of added rows the toast no longer uses, K158 with a second guard on
the toast that the recorded list already provides. K304 is not used: below the handover mark the size step
works out to 0 or less, so the explicit `size < SIZE_WARN_BYTES` check only says so. K78 is not used: the one-minute
ceiling on a running poll has no test, because every file read is bounded on its own and the harness runs
past a hung status hook itself; it stays as a backstop. K139 is not used either: the engine drops a toast it
cannot show and reports it itself, so a failed toast never reaches the mod, and the toast's place after the
poll's state is hardening no test can show red.

Replayed through the real code on 1,174 real sends and 2,104 real deliveries from six seats (2026-10-03):
one send held, a deliberate em dash probe, and every expected graph line added. As a positive control the
replay also made every identity count as another seat's, so a send is held exactly when its state file
resolves: every one of the 1,174 sends resolved to the same identity, or to none, as the revision before
it, 1,015 of them resolving. The status line, toast and band sparkline were proven in a real WezTerm
window against a scratch postbox before any seat ran them. If `claude plugin test` says hooks modules are turned off,
start `claude` once with network access to refresh the rollout switch, then run it again.

## Known limits

- Reading a message from the pane runs a real signed read, so with unread tracking on (OI-76 P1) it
  counts as the seat having read it.
- "Unread" is per session: the band and note know only reads this session ran, and `/clear` starts over.
- A send whose body file is a relative path after a `cd` is not gated (the mod cannot see the `cd`).
- The band and pane draw in the terminal and the Desktop app, not in `claude -p`; the guards and the
  note work everywhere hooks run.
