# transcript_cards: loader, version 1.23

Turns one seat's Claude Code, Codex, Kimi, or Antigravity transcripts into local decision cards for the transcript graph
(thread `<thread>`, design record version 1). Plain code: no model, no network.

This is the LOCAL step only. It writes files on the seat's own machine and talks to no server. Writing cards to
the server comes later, in its own change, after the namespace exists.

## What it extracts

| Card type | Source record | Notes |
|---|---|---|
| `Ruling` | a question the operator answered (question, options, chosen answer) | one card per answered question; `free_text` and `long_free_text` flag an answer that is not an offered label |
| `Candidate` | a prompt the operator typed, a message he typed while a turn was running, or the text of his `/goal` | a candidate, not yet a ruling; `prompt_source`, `short`, `legacy_origin`, `slash_command` are flags |

A card is made only from a recorded decision record in the seat's own transcript. A relayed quote is never a card.

What counts as a typed prompt: a user record whose `origin.kind` is `human`. Where a record has no origin, a prompt
source of `typed`, `queued` or `suggestion_accepted` is accepted, and a record with neither field is an older-format
prompt, kept and flagged `legacy_origin`. Sidechain, meta and compaction-summary records are dropped whatever their
label, and so is any wrapper text (local commands, hooks, channel events, launcher prompts).

Text that a machine or another seat wrote is never the operator's card, whatever the record says about who typed
it (measured on one seat, where 75 of 817 Candidates were such text):

- **A message from another seat** (`peer_message`). In an older transcript it has no origin and no prompt source,
  so it looked like an old typed prompt: on that seat all 51 unlabelled "prompts" were messages from other seats.
  The same class takes a typed prompt that opens with a seat's name as a header (`From uxxx: ...`, also with a
  bracket or a clause before the colon): that is a seat's message put into the pane, by a hand or by a seat,
  and a relay is never a card (8 on one seat, all on one day). The name must be a known seat: `From memory:
  we agreed ...` is the operator's own sentence and is kept. The roster is `KNOWN_SEATS` in the loader; add
  a label with `--seats LABEL` (repeatable).
- **A seat's report pasted by hand** (`peer_report_pasted`). It opens with the seat's label and a colon and has
  no `From` before it: `kxxx: Sent and read-back verified ...`, `VxxxxxXxxxx: Status report ...`. The label is
  written in any case, a bracket may follow it, and the colon needs no space after it. 49 such prompts were read
  on four seats: 48 were a seat's own report and none was the operator's line to or about another seat. The
  49th opened with the reading seat's own launcher name and was an instruction to it, so a seat's own names
  never count: its `--seat` label, its launcher name where that differs, and any name given with
  `--own-name NAME` (repeatable). The launcher name is built in for the two seats that have one (`bxxxxx` for
  `memory-bxxxxx`, `exxxx` for `exxxx-session`) and is also read from a `--marker "echo <name>-shell-ok"`. A
  hyphen part of a label is not a name of the seat: `codex-bxxxxx-cli` is a different seat from `bxxxxx`. The
  report lists the names used as `seat_own_names`; read that line before you trust a rebuild. Only a roster label counts, because the operator opens his own lines the same way:
  `Urgent: ...`, `important: ...` and `update: ...` are his and are kept.
- **Text a machine typed into the terminal** (`wake_injection`, `launcher_prompt`, `machine_text_cut`). The wake
  daemon, retired on 10 Sept 2026, typed its line into the seat's own terminal, and the fleet launcher types its
  session-start prompt the same way. The record says a human typed it, and a wake line quotes a peer's message.
  It can land in a line the operator had begun, and it runs to the end of that line because the machine also
  pressed Enter. So the text is cut from the machine's first word to the end: the operator's words before it are
  kept with `[machine-typed text removed]` and the record is counted as `machine_text_cut`. A prompt with nothing
  before the machine's text is dropped whole, and so is a free-text answer that holds a wake line.
- **A pasted block** (`<pasted_content>`). The block is cut out and `[pasted content removed]` is left in its
  place; the words typed around it are kept. A prompt that is nothing but the block is dropped as
  `pasted_content_only`.
- Harness text (`harness_text`), a prompt that is only an image marker (`attachment_only`), and a question
  submitted with nothing chosen and no note (`answer_empty`) are dropped too.

What the operator types WHILE A TURN IS RUNNING is not a user record. It is a record of type `attachment` whose
attachment is a `queued_command` with origin `human`, his words in `attachment.prompt`. Up to 1.10 every attachment
was dropped, and with it 172 such messages on one seat and 162 on another, rulings among them. They are now
Candidates with `prompt_source: queued_command`, read under the same rules as a typed prompt. The same record
type from a channel, a peer or a task notification is never a card. The oldest transcripts wrote no origin on
this record at all (June 2026 on one seat); there a command mode of `prompt` is the test, and the card says
`prompt_source: queued_command_legacy` and `legacy_origin: true`.

Three more rules, from the review of version 1.1:

- **Pasted terminal output is not a decision.** A free-text answer or a typed prompt that holds a carriage return
  or a terminal's own lines (`Last login:`, a shell prompt, sudo's `Password:` line) is dropped as `pasted_output`
  before redaction. On one seat 85 of 313 answers were free text, most of them pasted terminal sessions.
- **`/goal` is the operator's.** Its arguments become a Candidate with `prompt_source: slash_command`. Other
  commands' arguments (`/model`, `/effort`) are settings and stay dropped, and so does a prompt that is only a
  slash-command name.
- **An answer the model wrote is never a Ruling.** If the question call's own input already carried `answers`, the
  result is refused as `ruling_prefilled_by_model`. The pairing is kept in `cursor.json` across runs.

Dropped and counted by class in `report.json`: tool results, assistant text, thinking, attachments, harness records,
channel events, peer messages, task notifications, auto-continuations, launcher prompts, slash-command wrappers and
names, pasted output, compaction summaries, meta records, sidechains, SDK prompts with no origin, and a record
whose time is missing or has no zone (`no_timestamp`: the server refuses such a card). Closing text and
compaction summaries (as `Claim`) are not in this version.

The counts by class are lines read, not distinct records: a record that sits in two transcript files is counted
twice (on one seat, 2,030 lines for 1,638 distinct records of one class).

## Codex transcripts

A Codex seat keeps its sessions as rollout files under the `sessions` folder of its Codex home. Run the loader
on that folder with `--codex-sessions` in place of `--project-dir`:

```
python tools/transcript_cards/transcript_cards.py --codex-sessions <Codex home>/sessions --seat <seat> --out <local folder>
```

**A Codex home is not a seat.** One home can serve several seats and the Codex runs a seat starts (HQD: one home
for codex-kxxx-cli and kxxx's own Codex runs). Name the seat's folders with `--codex-cwd` (repeatable, absolute):
only sessions whose recorded working folder (`session_meta.cwd`) is one of them, or under one, are read. Every
other interactive session is listed in the report's `codex_unmapped` with its cwd and never read. Case is folded
for Windows paths only. Without `--codex-cwd` every interactive session is taken as the seat's, which is right only
when the home serves that seat alone. A seat whose folders hold no Codex session yet exits 3 on every run ("NONE ran
in a --codex-cwd folder") while the home holds other seats' sessions: that is the nothing-mapped signal, not a
fault, until the seat's first Codex session in one of its folders.

| Card type | Source record | Notes |
|---|---|---|
| `Candidate` | a prompt typed into a Codex client | `prompt_source: codex_typed`; every rule for a typed prompt applies |
| `Question` | a question the seat put to the operator (`request_user_input`, `request_user_input_async`) | owner is the seat; the id rule of the builders, so the hook and the walk name one card |

**No Ruling comes from a Codex transcript.** The question call returns at once with `{"accepted":true}`, and
the answer comes back later as an ordinary typed message that names no call. Pairing the next typed message
with an open question would be a guess, so the answer is a Candidate and the question a Question.

Which files are read. Only a session a person sat in: `session_meta.source` is a plain word other than `exec`,
and the originator is not `Claude Code`. On one machine that was 123 of 6,387 rollout files: 5,501 were `exec`
runs and 663 subagent threads (prompts a seat wrote), and 95 were copies of Claude Code sessions, whose cards
come from the Claude transcripts. There is no session-start marker for Codex: every such session under the
folder is the seat's. `files_not_read` counts the rest.

What counts as typed. Codex writes what the harness puts into a session (AGENTS.md, the environment, a
subagent's notice, a goal, an imported session) as user message items too: 13,219 of 13,766 user items on that
machine. Two marks say an item was typed into the client, and either is enough:

- the item names its own kind: `internal_chat_message_metadata_passthrough.content_item_kinds` holds only
  `user.*` entries. Context items name another kind (`agents_md.instructions`, `goal.internal_context`) or none;
- the record after it is an event of type `user_message` with the same text. An older terminal wrote no kinds and
  always wrote the event; a newer one writes the kinds, and on another seat (CLI 0.161.0) no event at all.

The card is named by the item's id. An older item has none and is named `codex:<session id>:<event time>`. A run
that ends on a user item reads that item again next time, a card already or not, so an event written a moment
later is read with its item and never makes a second card.

What a machine types into a Codex pane. A Codex seat has no channel, so the postbox TYPES each delivery into
its pane, and the client records it as typed. Of 547 typed records on that machine, 384 are dropped by
class: 148 deliveries (`postbox_delivery`), 156 tagged channel lines (`channel_line_typed`), 45 launcher
orientations (`launcher_prompt`), 15 handoffs of the older wake daemon (`wake_injection`), 9 fixtures and
delegation wrappers (`harness_text`), 10 pasted seat reports and one lone picture. The shapes are the postbox's
own templates, every version of them. This list is read for Codex transcripts only: a Claude seat has a channel,
and its operator can quote a tag.

A typed record that holds a delivery ANYWHERE is dropped whole, words in front of it included. A list of
templates is never complete: on a second machine the postbox wrote a batch header this list did not know, in
front of the delivery, and a cut kept that header as the operator's words (2 of that seat's 8 Candidates). On
the two Codex seats measured, nothing the operator typed ever stood in front of a delivery. The price is a line
he had begun when a delivery landed in it. The other machine-typed kinds (launcher, wake handoff, fixtures) are
still cut where they start, his words before them kept. Test group T26 is the attack list: every template, two
headers the list does not know, and each of them with words in front.

Limits, measured by reading all 159 Candidates of that machine. 12 are a seat's words that the operator pasted
with no frame and no lead-in of his own (a handover, a status, an onboarding prompt): the same limit as on a
Claude seat, and more common here, because pasting was how a Codex seat was reached before it had mail. A
prompt typed into a Codex client that writes neither mark is not read, and the loader cannot count such
prompts: an unmarked item looks the same whether a person typed it or the harness put it in.

## Kimi transcripts

A Kimi Code seat keeps its sessions as wire files under the `sessions` folder of its Kimi home: one
`agents/<agent>/wire.jsonl` per agent of one session. Run the loader on that folder with `--kimi-sessions` in
place of `--project-dir`:

```
python tools/transcript_cards/transcript_cards.py --kimi-sessions <Kimi home>/sessions --seat <seat> --out <local folder>
```

| Card type | Source record | Notes |
|---|---|---|
| `Candidate` | a message with `origin.kind` `user` (a typed turn), or a `goal.create` record (the `/goal` objective, verbatim and once) | every rule for a typed prompt applies; no origin at all is the older format, kept and flagged `legacy_origin` |
| `Ruling` | an `AskUserQuestion` tool call answered by the tool result of the same call | the result's `answers` map names the question text: the record itself ties answer to question, the bar for a dialect Ruling |
| `Question` | never from the walk | asked questions come from the live hook; a call open at the file's end is counted (`kimi_question_open`) and kept in the cursor, so a result appended later still ties |

What a machine puts into a Kimi pane. Every origin that is not `user` is a machine rendering and drops whole,
by class: `kimi_cron_job` (scheduled deliveries), `kimi_system_trigger` (goal continuations),
`kimi_injection_<variant>` (goal reminders re-rendered every goal turn, todo reminders, permission changes),
`kimi_origin_task`, `kimi_origin_skill_activation`. A compaction record is model-written (`kimi_compaction`).
The fifty-odd remaining record types are harness bookkeeping (`kimi_bookkeeping`). A machine envelope pasted
into an otherwise typed line (`<notification`, `<cron-fire`, `<system-reminder`) is cut from the machine's
first word.

Which files are read. A wire file opens with a `metadata` record carrying a protocol version; that is the mark.
Only `agents/main` wires are read: a subagent's user records are the parent's dispatch text, and a subagent wire
named by hand with `--files` drops every prompt as `kimi_subagent_prompt`. A call the harness refused (auto
permission mode) makes no card and no Question: the operator never saw it.

Enrollment, for a capture queue that reads a frozen scratch copy: `kimi_dialect.file_state(path, original_path=…)`
reads the bounded copy at `path` but derives the session slug and the main/subagent mark from `original_path`,
never from the scratch location (txxxx's split; codex-kxxx-cli's frozen-copy finding). A file whose first record
is not a Kimi metadata record is not enrolled, whatever its name. The transcript walk passes no `original_path`
and reads the live wire, so its answers are unchanged.

Measured on the seat it was built for (nxxxxx, HQB): 1,816 main wires of 5,081 (the other 3,265 are subagent
wires), 521,482 lines, 2,459 cards (2,452 Candidate, 7 Ruling), 23 redaction drops, zero over the server's
limit, 23 seconds.

## Antigravity transcripts

An Antigravity seat keeps its sessions under the `brain` folder of its Antigravity CLI home: each session has a
folder with `.system_generated/logs/transcript.jsonl`. Run the loader on that folder with `--antigravity-brain`
in place of `--project-dir`:

```
python tools/transcript_cards/transcript_cards.py --antigravity-brain <Antigravity CLI home>/brain --seat <seat> --out <local folder>
```

| Card type | Source record | Notes |
|---|---|---|
| `Candidate` | a record with `type: "USER_INPUT"` and `source: "USER_EXPLICIT"`, or `/goal` command | strips harness envelopes (`<USER_REQUEST>`, `<ADDITIONAL_METADATA>`); preserves exact prompt text |
| `Ruling` | an `ask_question` tool call answered by the operator via the modal UI or exact option selection | answers mapped by question title; tied by `call_id` |
| `Question` | never from the walk | asked questions come from the live hook; a call open at the file's end is counted (`antigravity_question_open`) and kept in cursor |

What drops by class:
- `antigravity_thinking`: internal model scratchpad reasoning
- `antigravity_subagent_record`: subagent execution transcripts under delegated tasks
- `antigravity_system_message`: system harness messages, waiter wakeups, reactive notifications
- `antigravity_tool_result`: generic tool output records without modal answer bindings
- `antigravity_bookkeeping`: execution status markers and non-dialogue records

Enrollment: `antigravity_dialect.file_state(path, original_path=…)` reads the transcript file, deriving the
conversation UUID from the path hierarchy. If `original_path` is given (frozen scratch copy), the session
comes from `original_path`. Subagent transcripts are identified and excluded.

## Run

```
python tools/transcript_cards/transcript_cards.py --project-dir <the seat's Claude Code project folder> --seat <seat> --out <local folder>
```

- `--seat` is the seat's roster label, the name the server stamps and every card carries. Where a seat's launcher
  tag differs from its label (tag `exxxx`, label `exxxx-session`), the marker is the tag's: name it with
  `--marker "echo exxxx-shell-ok"`.
- Files are picked by the seat's session-start marker (`echo <seat>-shell-ok`), and only when one of the
  transcript's own user prompts holds it: a transcript that merely quotes the marker belongs to another seat.
  Only top-level `*.jsonl` files are read; subagent folders are never opened.
- `report.json` names the files read and counts the top-level transcripts NOT read (`files_not_read`). To take
  older sessions in, add `--marker TEXT` (repeatable, text that this seat's own prompts hold) or name them with
  `--files`. `--all-files` takes every top-level transcript. When transcripts exist and none is read, the run says
  so on stderr and exits 3.
- `--seat-file NAME` (repeatable) names a top-level transcript in `--project-dir` that is this seat's although none
  of its prompts holds the marker, such as the new file a continued session writes. The name goes into
  `cursor.json`'s `seat_files`, the same cache marker discovery keeps, so the file is read on this run and every
  later one, `--rebuild` included. The name must be exactly as the folder lists it (case included), a regular
  `*.jsonl` file and not a link; anything else exits 2 and writes nothing. `report.json` lists the names in
  `seat_files_named`. Only with `--project-dir`. The load hook passes it once per name, in one loader stage.
- `--rebuild` writes the store again from a full read (on a store a seat's hooks load, only through
  `load_hook.py --run --rebuild`: see "The load from a seat's hooks"). Use it after every loader upgrade: an ordinary run only
  adds, merges and re-screens, so a card made under an older rule would otherwise stay. A store whose card ids
  were made under an earlier rule (before 1.4, and Rulings before 1.7) is rebuilt without being asked, and so is
  a store whose `cursor.json` is gone (`report.json` says so in `store_rebuilt_from_older_format`).
- Keep `--out` outside any git tree: cards hold operator text.

Output in `--out`: `cards.jsonl` (one card per line, sorted by time), `drops.jsonl` (redaction drops: card id and
detector names, never text), `cursor.json` (read offsets and paired question calls), `report.json` (counts).

## The load from a seat's hooks: `load_hook.py`

Loaded by hand, a seat's store lags its sessions: on 10 Oct 2026 the stores were 32 to 48 hours behind, and one
ruling reached the server 32 hours late (exxxx-session's gap register, gap 1). `load_hook.py` is what a seat's
Claude Code hooks call instead. Standard library only; both stages run with the Python that started it.

```
<python> <client>/tools/transcript_cards/load_hook.py --event <hook event> [--project-dir <dir>]
    [--seat <seat>] [--store <store>] [--base-url <server>] [--state-file <state>] [--marker TEXT] [--own-name NAME]
```

- **The seat is the pane's identity**, `MESHY_IDENTITY`, which the launcher sets; the channel and meshy-mod take all
  its labels (the mod then acts for the enabled ones). A pane without one (or whose first label is not a plain
  label) is no seat's: the hook does nothing at all, on every event. One Claude Code profile can serve several
  seats (on HQB five seats share one `settings.json`), so the hook's command names no seat. The worker reads the
  postbox's `config.json` (`MESHY_POSTBOX_CONFIG`, else `~/.meshychat/postbox/config.json`, as a regular file of
  this user's): the serving identity is the first label, in order, whose entry exists and is enabled (a missing
  `enabled` is on, as in the mod), and the server (`base_url` of that entry, else of the file), the state file
  (`state`) and the loader's markers (`markers`) are its. The seat is its `seat` when set, else the pane's FIRST
  label; the store is `<MESHY_CARDS_ROOT or ~/.meshychat/cards>/<seat>`. On HQB a pane is `txxxx,txxxx-prod`, the
  bare `txxxx` is an old test-server identity (off, no server of its own), and so the load goes to `txxxx-prod`'s
  server and state file as seat `txxxx`. Each flag, given, wins. A pane none of whose labels has an enabled entry,
  with no flags, is not loaded and its receipt says so.

- A load is two stages, each stopped after 120 s (`--stage-timeout`): this loader, incremental through
  `cursor.json`, then `python -m tools.meshy_node card write --file <its cards.jsonl>` from the checkout the
  script sits in. A key already on the server is left as it is, so sending the whole file again writes only the
  new cards. A store with no card is not sent.
- **One loader store a project folder.** `--store` is the hook's store; each canonical project folder gets its own
  loader store under it, `<store>/projects/<key>/`, which is that folder's `--out` and holds its `cards.jsonl`,
  `drops.jsonl`, `cursor.json` and `report.json`. The loader keys its cursor, and its record of which transcripts
  hold the seat's marker, by file name, and `--rebuild` clears its whole `--out`: in one shared store another
  folder's file of the same name would pass as this seat's (its prompts becoming this seat's cards), and a rebuild
  of one folder would drop every other folder's cards, unsent ones included. A store that holds a loader's files at
  its root (one loader store for every project, the layout before `projects/`) is refused with that reason: move
  the loader's files out of it.
- A hook never passes `--rebuild` or `--all-files`: a rebuild is a full read, which on a long history runs past
  120 s and would be stopped every time.
- **Every load by hand of a store the hooks load goes through `load_hook.py`**: the same command with `--run` in
  place of `--event`, `--project-dir` given, and `--rebuild` or `--all-files` when wanted. It waits for the
  store's lock (`--wait`, 900 s), so it never runs beside a hook's load, and its stages get 1,800 s each unless
  `--stage-timeout` says otherwise; it prints the receipt of its pass. Use it after a loader upgrade (`--rebuild`,
  once for each project folder: it rebuilds that folder's store and touches no other), for a seat's first load,
  and for a first push of many cards (the server takes about one write a second). A write stopped part way loses
  nothing; the next load goes on.
- **Running `transcript_cards.py` directly on such a store, or on a project's store in it, is not safe while the
  hooks are on.** It takes no lock,
  and its writes of `cards.jsonl`, `drops.jsonl` and `cursor.json` would interleave with a hook's load: a card can
  vanish while the cursor moves past it. A pass that finds one of the loader's `<file>.tmp` written in the last two
  minutes does not start (its receipt says another loader is writing the store), but that shows a direct run only
  in the moment it writes, at its end, and is no protection. Turn the hooks off first, or use `--run`.
- The hook names the session's transcript file (the base name of `transcript_path`) in its request, and the pass
  gives it to the loader as `--seat-file NAME` when the loader has that flag (txxxx's, after #208: a continuation
  after a compaction is a new transcript with no seat marker, which the loader would otherwise never read). Whether
  it has the flag, and how it is declared, is found once a worker by reading the loader's own `add_argument` call
  (a source scan; nothing is run). Declared with `action="append"`, every name of the pass goes to one loader stage,
  `--seat-file` once a name; declared any other way, the pass runs one loader stage a name, each under the stage's
  time limit, and then one write. Without the flag the names are not passed and the receipt's detail says so.
- No transcript name is ever lost to another. The hook takes no lock, so a request is never merged into another
  file: each (folder, transcript) has its own queue file, `<key>.t<16 hex of the name>.json` (a request with no
  transcript keeps `<key>.json`), and two continuations of one folder that end together queue two files. A pass
  is one folder: the oldest request and every other queued request of the same folder, up to eight, so all their
  names go to the loader in that pass; each is checked on its own (one that is bad is set aside, and the others go
  on), and every one is removed only after the pass. The name must be a plain `.jsonl`
  file name; at the pass it must be a regular file of this user's in the project folder, read without following a
  link (a request naming anything else is set aside as `.bad`); one not written yet is simply not passed.
- `--project-dir` defaults to the folder of the session's transcript (`transcript_path` in the hook's input), so
  a seat whose sessions start in several folders loads each of them, as its sessions run, each into its own loader
  store. Given, it pins every load to that folder. `--marker` and `--own-name` go to the loader.
- `--store` holds every project's loader store, so the rule in "Run" holds for it: outside any git tree.

The hook itself reads its JSON from stdin (at most 1 MB, at most 2 s), starts a detached worker, and for
`PreCompact` and `SessionEnd` prints one line. It touches nothing under the store, so a slow or stalled disk there
cannot hold a prompt: Claude Code waits for a `UserPromptSubmit` hook. The worker does the rest:

- It puts its request in `<store>/pending/`, one file a project folder, and under the store's lock takes the
  requests one by one, oldest first. A request names its canonical project folder and nothing else: the seat, the
  server, the state file, the markers and the programs are always the worker's own arguments, so a file in the
  queue can choose a folder to read and never whose key signs or what runs. A project whose session ends while
  another project's load runs is loaded after it, not lost. A request is renamed `.claimed` for its pass and
  removed after it, so a worker that dies leaves it to the next. At most three passes a worker.
- A worker that leaves requests queued releases the lock and then starts one successor (the hook's own detached
  start, with the worker's own arguments and no request of its own): at once after its three passes, or after two
  minutes when it was held up (another loader's evidence, or entries it could not claim). It also looks at the
  queue after every release and goes round again for a request that came in after its last look. A successor that
  finds the queue empty ends at once, and one that loads nothing starts none, so there is never a chain of idle
  ones. Without this, a session that ended while another load used its last pass would wait for some later hook.
- A queue entry that is not a well-formed request (an object whose folder is null or an absolute path UTF-8 can
  carry, under 4,096 characters and with no NUL, and whose time is a number from 0 to 10^11, an integer compared as
  it is) is renamed `<name>.bad` and never read again; the queue goes on, and nothing in an entry can stop a scan. Each request's pass, its receipt included, is one unit: a failure inside it
  sets that request aside and costs no other.
- The lock is an operating-system lock on `load.lock` (flock on POSIX, msvcrt's byte lock on Windows, the pattern
  of `tools/postbox/postbox_os.try_lock`), held for the worker's whole run. The file is never deleted, and a
  process that dies holds no lock, so there is no stale lock to judge. A stage never outlives its worker's lock.
  On POSIX each stage shares the locked descriptor, so a worker killed in the middle of a stage leaves the lock
  held until the stage ends too. On Windows each stage goes into a kill-on-close job object the worker holds open
  (the pattern of `tools/postbox/postbox.tie_to_daemon`), so the worker's death ends its stages; a stage that cannot
  be put in the job is stopped and not run. The stage is put in the job right after it starts, so a worker killed
  in that moment leaves that one stage outside it (it starts nothing of its own). **The Windows path is tested with
  its system calls stood in for, and since 11 Oct 2026 it has run on Windows too: on HQB the load-hook suite passes
  (252 tests at 0e4a7382), a by-hand `--run` and a PreCompact fire each ran both stages to status ok, and six seats
  seeded their stores before the hooks went on in HQB's shared settings. A worker dying while a stage runs has not
  been seen on Windows.**
- The store is this user's alone. On POSIX the worker makes the store, `pending/`, `results/`, `projects/` and
  each project's loader store with mode 0700, and refuses them when one is a link, another user's, or writable by
  group or others (a project's store refused fails that project's pass only): no pass, and a failed
  receipt where the store itself is sound (a load by hand prints the reason). Every queue entry, receipt and the
  lock file are opened without following a link or waiting on a pipe (`O_NOFOLLOW | O_NONBLOCK`), must be a regular
  file of this user's, and a request or receipt is read only up to 16 KiB; anything else in the queue is set aside
  as `.bad`. One scan looks at no more than 256 entries. On Windows the store is trusted to the ACL of the user's
  profile it lives in, and only links and non-files are refused.

Every pass leaves `<store>/results/<key>.json` for its project, written whole or not at all: `time` (UTC, `Z`),
`status`, `detail`, `seat`, `project`, and `knew_seat` (see below) when true. `<store>/last-result.json` is the seat's summary, the file `card recent
--load-result` (a separate change) shows when a session starts: the newest project receipt of the last 24 hours,
unless one of those did not finish cleanly (neither `ok` nor `skipped`); then the newest such one, so one
project's good load does not hide another's failed one. With one project folder it is that project's receipt.
A folder that holds none of the seat's transcripts is not the seat's project: its receipt is `failed` with the
loader's exit-3 detail and no note after it, and the folder's loader store has never recognised a transcript of the
seat's (`cursor.json` names no seat file, or there is none). The hook reads that store and the folder's last receipt
before each pass too, and a folder it knew (a seat file in the cursor, or a last receipt that recorded one,
`"knew_seat": true`) gets a note on its exit-3 receipt, since a `--rebuild` clears the cursor first. Every receipt
of a folder whose store knew a seat file carries `"knew_seat": true`; a bare `ok` is no evidence, since a folder
with no transcript at all loads `ok`. That is a session started from the seat's shell in
another folder. It counts only when no other receipt is there, so it neither hides a healthy feed nor a real
failure of the seat's own project (#238 B3).

| status | when |
|---|---|
| `ok` | the cards were loaded, and each one written or already there |
| `held` | `card write` exit 3: a redaction detector held a card |
| `unreachable` | `card write` exit 2: the server could not be reached |
| `failed` | the loader failed (exit 3: no transcript holds the seat's marker), `card write` exit 1, or exit 2 with the client's `{"refused": ...}`: it refused before sending (a missing state file, say), which is not the server's fault |
| `timeout` | a stage ran past its time and was stopped |
| `skipped` | there was no project folder to read |

The detail is counts, seconds and exit codes, never a card's words: of `card write`'s answer only the outcome names
it counts are copied, and nothing of either stage's other output.

| event | the worker loads its project | prints |
|---|---|---|
| `UserPromptSubmit` | unless the project's receipt is `ok` and younger than 15 minutes, any receipt of the project is younger than 5 minutes, or a load is running; requests others left waiting are taken either way | nothing: this hook's output goes into the model's context |
| `PreCompact`, `SessionEnd` | always; while a load is running, the request waits in `pending/` and that load takes it | one line, `card load: started` |
| `SessionStart` | unless a load is running | nothing; the briefing is `card recent` |

- `PreCompact` and `SessionEnd` do not wait out the 15 minutes: each closes a stretch of transcript, and what was
  said since the last load is exactly what came late. A load already running may have read the transcript before
  the last answers, which is why their request waits for it.
- The worker runs detached (POSIX: a session of its own; Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`),
  with the null device for its input and output, so no hook waits for it. On Windows its two stages start with
  `CREATE_NO_WINDOW`: a detached worker has no console, and a console program it started would open a window.
- Every prompt starts a worker (a Python process that ends at once when its project is fresh): the throttle reads
  the store, and the hook does not.
- Every path exits 0, a bad argument and an unwritable store included.
- To load now and see the receipt: the load by hand above.

Limits. A loader run directly on the store, with the hooks on, is caught only while it writes (see above). On
Windows a worker killed between starting a stage and putting it in the job leaves that stage running, outside the
lock. A project folder whose name UTF-8 cannot carry is never queued. The summary shows one receipt; the others are
in `results/`.

## Accepting a new loader version

Added 10 Oct 2026 at txxxx's request, so that a seat stops comparing two loader versions by hand.
`accept_seat.py` runs the loader in use and the new one over the same transcripts and prints what changed, as
counts only:

```
python tools/transcript_cards/accept_seat.py --old <loader> --new <loader> --project-dir <the seat's project folder> --seat <seat>
python tools/transcript_cards/accept_seat.py --old origin/main~1 --new origin/main --project-dir <folder> --seat <seat>
```

- A loader is a path (a `transcript_cards.py`, or the folder that holds one) or a git revision of this clone (its
  `tools/transcript_cards` folder at that revision; `--repo` names another clone). A path is tried first. The
  other `.py` files of the loader's folder are put beside it, because a loader may import a module beside it.
- The seat's transcripts are copied ONCE into a scratch folder first (the top-level `*.jsonl` of `--project-dir`,
  or every `*.jsonl` under `--codex-sessions` with its subfolders), and both loaders read that copy: the seat's
  own session goes on writing while the two runs happen. Each loader writes a new store of its own. `--all-files`,
  `--marker`, `--seats` and `--own-name` are passed to both. The scratch folder holds copies of the operator's
  transcripts and is deleted at the end; `--keep` keeps it and prints where it is (`--work` picks the folder it
  is made in, keep it outside any git tree).
- Each store is scanned with `second_scan.py` and `third_scan.py`, the copies beside `accept_seat.py`, so both
  stores are measured by the same scans.
- Printed: both `loader_version` values; per card type the cards in each store and the cards `added`, `removed`,
  `changed` and `other_fields`. A card is its type and its `id` (the server's key, made from the record or question call and
  the block, never from the text), or without an `id` its `source.record_uuid` and `source.block_index`; `changed` is the
  same card with a different `source.source_sha256` (it was made from different kept text); `other_fields` is the
  same source with another field different, such as a re-worded `text`, a cut or a flag (`loader_version` is not
  counted). Then old, new and delta of `report.json` (`files`, `lines_read`, `new_cards`, `cursor_resets`,
  `cards_total`, redaction drops and cards over the server's limit), of every class in `dropped_by_class` and
  every detector in `redaction_drops_by_detector`, and of the two scans' counts (tokens not flagged; values and
  cards per class). The last line says `no difference`, how many counts differ, or `NOT COMPARED` when a loader
  failed, a scan gave no count, or a `report.json` is unusable. `--json` prints the same as JSON.
- A count is a non-negative integer. A `report.json` that is absent, not JSON or not an object, a counter above
  that is absent or not a count (a numeric string, `true`, a float, a negative number, `null`), and a
  `dropped_by_class` or `redaction_drops_by_detector` that is absent, not an object or holds a value that is not a
  count, each make the result `NOT COMPARED`, with one fixed line per problem (`cannot compare: new report.json:
  redaction_drops_total absent or not a count`; `report_problems` in the JSON). Such a value is shown as `-`
  (`null` in the JSON), never as itself.
- Card text is never printed, and neither is anything a loader or a scan printed: `second_scan.py` shows the start
  of a token and the 24 characters before it, and a loader's traceback can quote a record. A failed loader shows
  its exit code and its exception's class name only, and the name only when it is one of Python's built-in
  exceptions (or `json.decoder.JSONDecodeError`); any other name shows as `an exception (not a built-in name, not
  shown)`. A record can hold a pasted traceback, and a message that quotes it puts a traceback header and an
  exception line of the operator's on stderr, read like the loader's own.
- Exit 0, whatever the counts say and also when a loader fails; exit 2 when an input is missing (the transcript
  folder, a loader or a revision, a scan script).

A difference is not a verdict. Read each non-zero line against the new version's History entry: a count the entry
does not explain is a question for the loader's author before the seat accepts it. Then rebuild the seat's own
store with the new loader (`--rebuild`). The checks are `tests/test_transcript_cards_accept_seat.py` (synthetic
transcripts, no database), and `mutate_accept_seat.py` runs each mutant its docstrings name (29, each must turn
it red).

10 Oct 2026, from Codex's review of the first version (f3f102f), three changes. A failed loader's exception name
is shown only from the built-in list above: the first version showed whatever name followed the last traceback
header on stderr, and a pasted traceback quoted in a message put the operator's text there. The scans' output is
read with universal newlines: on Windows its lines end in CRLF, no count line of `third_scan.py` matched, and every
comparison there was `NOT COMPARED`. A `report.json` counter or counter map that is absent or not a count makes the
result `NOT COMPARED`: the first version skipped it, so a loader that dropped `redaction_drops_total` showed as no
difference, and it printed a value that was not a count as it was. Comparing against a loader older than 1.5,
which has no `cards_over_server_limit`, is therefore `NOT COMPARED` (the line names the counter).

## Card fields

Every card: `schema` (`card.v1`), `id`, `type`, `owner` (operator), `seat`, `date` (UTC day), `time` (the record's
own time, always spelled `YYYY-MM-DDTHH:MM:SS.mmmZ` so that string order is time order), `status` (always
`active` here: supersedes is decided across seats, not by the
loader), `text` (at most 1,500 characters, prefixed with type, owner, seat and time), `text_sha256`, `truncated`,
`loader_version`, `attribution`, and `source` (`record_uuid`, `block_index`, `source_sha256`, `kind`, `first_hand`,
`sessions`, `files`, and when present `transcript_uuid` and `other_content`).

**Attribution** (1.19). `attribution` says who said the card's text, whose words it quotes, how it was read and
whether the operator confirmed it, in the four keys the server checks (#232):

| card | `speaker` | `confirmation` | `source.first_hand` |
|---|---|---|---|
| Candidate (a typed prompt) | `operator` | `typed` | true, unless pasted |
| Ruling (an answer) | `operator` | `answered` | true, unless pasted |
| Question (the seat's model asking) | `agent` | `asked` | **false** |

`quoted_author` is null, or `"unknown"` when the operator's text reads as a pasted document; `first_hand` is then
false as well. A typed prompt counts as pasted at 1,000 characters, 10 lines or 2 markdown headings; a free-text
answer and its note only at 2 headings or 5,000 characters, because a long answer over many lines is often his own
(one labelled so is 1,422 characters over 25 lines). The rule reads the full text before any cut. An answer that
only names offered options, and every Question, is never marked. Measured, counts only: the typed-prompt rule
marks all 5 cards a blind rater labelled as another's words and none of the 39 other Candidates in exxxx's
76-card sample, and 73 of dxxxxx's 1,285 Candidates and 50 of txxxx's 795; that is a smoke test of five positives,
not a precision figure. The card is kept either way: its words may still be the decision.

`extraction` is `<dialect>/<kind>` (`claude/typed_prompt`, `codex/question_codex`, `kimi/answered_question`,
`claude/question_hook`), in the server's alphabet: lower-case letters and `_`, with any other character turned
into `_`. The server refuses a first_hand that disagrees with attribution on a card first written with it,
and freezes first_hand on every card. So `card write --update` over a card written before 1.19 is refused for a
Question and for a newly marked paste (their first_hand changes); leave those as they are. Without `--update` an
existing key is not touched.

A Ruling also carries `question`, `options`, `answer`, `notes`, `multi_select`, `free_text`, `long_free_text`.
A Candidate also carries `prompt_source`, `legacy_origin`, `short`, `has_attachment`, `parent_uuid`, and
`slash_command` when it came from `/goal`. No card carries a second prose field (`body`, `content`, `summary`,
`title`): the server embeds the longest of those, and it must be `text`.

**Size.** The server refuses a card over 8 KB. `text` is capped, but a Ruling's answer, note and question are
stored whole, and a free-text answer can be a whole pasted document (one seat had an answer of 51,152 bytes).
A card over 7,000 bytes has its `answer`, then `notes`, then `question` cut until it fits; the cut leaves a line
saying how long the field was and sets `truncated`. Redaction runs over the full fields before the cut, and
`source.source_sha256` is the hash of the full fields.

**Identity.** `id` is 32 hex characters that name where the card came from, and nothing about what it says. It
does not change when the card's text does, so a card cut differently by a later loader is a new version of the
same card on the server and never a second card.

- A Candidate: `sha256("v1|<record uuid>|<block index>")[:32]`, with `source.record_uuid` the transcript record.
- A Ruling: `sha256("v1|ask|<question call id>|<question index>")[:32]`, with `source.record_uuid` the question
  call's id, `source.block_index` the question's place in that call, and the transcript record's own uuid in
  `source.transcript_uuid`. A client hook that writes a ruling at the moment it is answered knows the call id
  and the question order and not the transcript record, so this is the one name both can make. One call carries
  up to four questions (on one seat 45 of 78 calls carry more than one), so the index is part of the name. An
  answered record with no call id falls back to the Candidate rule.

What a card says is in `text_sha256` and `source.source_sha256`. A record copied into another session file is
one card with two sessions in its provenance. The same record seen with different content (none among 980 cards
on the one seat measured) keeps the earlier content and lists the other hash in `source.other_content`.
Deleting `cursor.json` and re-running leaves `cards.jsonl` unchanged.

**Repeats.** The operator types some prompts many times, and in a search those copies crowd out everything else
(on one seat 91 of 817 Candidates repeat an earlier one). Nothing is removed. The earliest Candidate of each text
carries `repeats` (the number of later copies) and `repeat_last_time`; each later copy carries `repeat_of` (the
earliest card's id). A reader that ranks cards should skip cards with `repeat_of`, and the write step will not
send them. Rulings are never grouped. The match is exact text on one seat; near-copies and copies on other seats
are for the search to collapse (`source.source_sha256` is equal wherever the kept text is equal).

## Redaction

Fails closed: a card that trips a detector is dropped whole. Every run also re-screens the cards already stored, so
a tightened detector removes what an earlier run let through.

| Detector | Catches |
|---|---|
| `pem_block`, `jwt`, `bearer_header`, `url_credentials` | the standard shapes |
| `vendor_key` | known key prefixes, plus any short lower-case prefix, then `_`, then a long mixed run |
| `named_secret`, `pat_assignment` | a generated-looking value after a key, token, secret or credential name |
| `opaque_token`, `quoted_code` | a standalone code of 32+ characters, or a quoted one of 24+, that looks generated |
| `pairing_code`, `lan_capability_url` | a code after the words "pairing code"; a link to an address-and-port host with a path segment of 20+ that mixes both cases and a digit |
| `mac_address`, `hardware_hash` | a MAC-shaped value; 64 hex near a word that names a hardware object |
| `password_assignment`, `pass_assignment`, `password_phrase_wide` | a password on the same line as its name, in English and German |
| `password_block_value`, `password_next_line` | the YAML block form; a value alone on the line after its name |
| `cli_secret_flag`, `auth_scheme_value`, `auth_assignment` | `--password X`, `sshpass -p X`, `Authorization: Token X`, `auth=X` |
| `secret_word_hex`, `slash_prefixed_token`, `hook_url` | hex after a secret word, `1//...` refresh tokens, webhook and bot URLs |
| `service_variable`, `otp_or_pin` | this fleet's service-credential variables; PINs, one-time and recovery codes |
| `aws_secret_label` | an AWS secret access key after its label in any spelling (`aws_secret_access_key`, `awsSecretAccessKey`) |

`vendor_key` counts all four AWS key-id prefixes (`AKIA`, `ASIA`, `ABIA`, `ACCA`), and `jwt` takes the empty payload
`e30`. Both gaps, and the camelCase AWS label, were found on 13 Oct 2026 by testing this loader against the key shapes a
plugin's credential filter had just learned.

The password, command-line, link and code rules from `password_assignment` down are memory-bxxxxx's, written from
three red-teams of this loader. With them none of 37 planted typed shapes passes and none of 26 lines of ordinary
operator prose is dropped.

**Per-seat detectors.** `SEAT_DETECTORS` adds rules for one seat, chosen by `--seat`, so a seat does not edit the
shared list. Today: `pxx` (8+ hex with a letter and a digit near a hardware word, and an upper-case UUID) and `dxxxxx` (64 hex near the
plain words mac and serial as well, and 8+ hex with a letter and a digit near those words or u-boot). On dxxxxx
that second rule also drops a card naming "Mac" (the OS; the words match in any case) within 200 characters of a
mixed 8-hex string such as a short git sha, the fail-closed direction; none of its 1,464 cards from 1.15 did. The standing list leaves plain "mac" and "serial" out: on one seat they are
"your Mac" and the serial console.

Limits, stated plainly. A Candidate records what was submitted through the operator's keyboard, which is not
always what he wrote. Machine and peer text with a known frame is removed; a peer's message he pasted by hand
under a typed lead-in has no frame and is kept (4 of a random 50 on one seat, all from before 10 Sept 2026).
So a Candidate carries no authority by itself.

A card over the server's limit. `fit_to_limit` cuts the answer, the note and the question, each to a floor, and
then, only if the card is still too large, the option descriptions, all to one shared length with a floor of 200
characters. Every cut field ends in a line that says how long it was. Option labels are never cut, because the
answer names them. A cut is measured without the file names and sessions a card was seen in, so a card built by
a hook and the card mined later are cut at the same place, and a cut does not move when a later run sees the
answer in another session. A question with very many options or very long labels can still be over the limit:
the walk keeps it in the local store and counts it (`cards_over_server_limit`), and a builder refuses it as
`card_too_large`.

The pasted-report rule takes a roster label and nothing else, so these shapes, each seen on a seat, stay cards: a
report under a nickname (`trav: ...`) or a misspelled label (`Antigraavity: ...`), another model's answer
(`chatgpt: Done ...`), a pasted document (`readme: ...`), a report with no label, and a pasted exchange
(`Human: ... Kxxx: ...`). Two names cut the other way. `bxxxxx` is a seat and also a person's first name: a
chat he pasted from a messenger that opens `Bxxxxx: ...` is dropped as `peer_report_pasted` on every seat but
that one, under a class name that does not fit it. And a seat's launcher name can be a person's name too: such a
person's pasted message opens with the seat's own name and is kept.

Three edges of the same rule, from pxx's and memory-bxxxxx's review. A new seat whose launcher name differs
from its label, and which gives that name neither as a marker nor with `--own-name`, loses the prompts that
address it by that name. Only a round bracket may follow the label, which is how seats sign: `kxxx [HQD]: ...`
and `**kxxx:** ...` are kept. And a roster label followed by a colon is taken whatever comes next, so a made-up
`uxxx://...` would be dropped; no such prompt has been seen.

Patterns reduce exposure and cannot prove a card clean. Five shapes are known to pass and
are kept visible in `attack_redaction.py` as `accepted`: a letters-only password after "is", a bare 31-character
token, a bare cloud secret with slashes in it, credentials given as two plain words, and a plain word alone on the
line after a password prompt. Document links on named hosts, file paths, UUIDs and personal identifiers (email
addresses, phone numbers) are kept.

## Builders for another writer

A writer that is not the transcript walk (the answer-time hook, through `meshy_node card write`) imports three
functions and builds no card of its own:

```python
card, refusal = answered_question_card(seat, call_id, index, question, options, answer, time,
                                       notes="", multi_select=False, prefilled=False,
                                       kind="answered_question", session="", transcript_uuid=None,
                                       dialect="claude")
card, refusal = asked_question_card(seat, call_id, index, question, options, time,
                                    multi_select=False, thread_id=None, kind="question_hook", session="",
                                    dialect="claude")
names = screen_prose(card, seat=None)        # detector names; [] when the prose is clean
```

- `answered_question_card` makes the Ruling the walk would mine for the same answer, through the same code: the
  drop rules for an answer, the detectors (the seat's own included) over the FULL question, answer, note and
  options before anything is cut, the text, the id and the cut to the server's limit. Built with the record's
  time, session and `transcript_uuid`, it equals the mined card field for field but for the file name.
- `asked_question_card` makes a Question card for a question a seat has put to the operator. Its owner is the
  seat that asked. Its id is `card_identity("Question", None, index, call_id=call_id)`: the same call and place
  as the Ruling that answers it, so a writer can name the Question a Ruling closes without having seen it.
- A refusal is one of `CARD_REFUSALS`, short enough to show a seat in one line: `no_call_id`, `time_format`,
  `ruling_prefilled_by_model`, `answer_empty`, `wake_injection`, `pasted_output`, `question_empty`,
  `card_too_large` (over the server's limit after every cut), or
  `redaction:` followed by the detector names.
- `time` has one spelling, `YYYY-MM-DDTHH:MM:SS.mmmZ`; any other is refused as `time_format`. The caller decides
  which instant it is. `transcript_uuid` is given only when the caller read the answer's record in the
  transcript and took the time from it; its absence on a hook-built card says the time is the hook's clock.
- `screen_prose` is for a card no builder made, a Commitment or a Decision written by hand. It reads the card's
  prose (text, question, answer, notes, done_when, options) and not its ids, hashes and record references: a
  question call's id has the shape of a vendor key, and a scan of every string of a card refused all 162
  Rulings of one seat.
- `dialect` names the harness the hook ran in, for `attribution.extraction` (1.19); a Claude Code hook leaves
  it at `claude`.
- No builder writes, signs, reads a head or adds `supersedes`.

What follows from a writer that creates a key once and never rewrites it: the first writer's text is the card.
A Ruling the hook wrote is not cut again by a later loader, and a tightened rule does not reach it (the server
freezes a card's time after its first version, and the hook's clock is not the record's); taking such a card
back is a redaction on the server. A check that reads cards back after a push compares keys, not text hashes.

## One file at a time, for a queue

`run()` reads a folder and keeps its own cursor. A caller with its own bounded reader and its own transaction
(the capture queue of #194, `native_window.py` and `incremental_store.py`) uses the same parser one window at a
time instead:

```python
dialect, meta = enroll(path)                       # once per file and generation, from its first record and path
checkpoint = new_checkpoint(dialect, meta)         # also after a replacement: no pending ask crosses generations
raws, stats, checkpoint = read_file(records, checkpoint, seat=seat)   # records: (offset, line) from checkpoint["offset"]
cards, drops = approved_cards(raws, seat, file_name)                  # the loader's detectors, before anything is stored
```

- `read_file` parses and does nothing else: it never opens a file, never decides a file was replaced, and never
  writes. `run()` is a loop over it, so every check above holds it. It never changes the checkpoint passed in.
- The checkpoint is a JSON object, and every piece of state that crosses a window is in it: `offset` (where the
  next window starts), `prefilled_asks` (a Claude Code ask whose call already carried answers, until its result
  is read), and `open_asks` (a Kimi call not answered yet). A Codex user item whose event is not written yet holds
  `offset` at the item, so the next window reads it again and the same card can come back: store cards by id.
- Commit the cards, the drops and the returned checkpoint together, and use `checkpoint["offset"]` as the safe
  offset, never the end of the window. A window that adds nothing and does not move the offset is nothing to commit.
- Refusals change nothing: `CheckpointIncompatible` for another `schema`, `loader_version` or `id_rule` (rebuild
  that file from offset 0 as a new generation), `PendingStateFull` past `MAX_PENDING_PREFILLED` (1,024; an ask is
  never dropped for being old), and `ValueError` for a record out of order or without its newline.
- `raws` are not screened. Only `approved_cards` output may be stored; it drops a whole id when any of its content
  trips a detector, since a caller with no store to compare against fails closed.
- Parser identity for a store: `transcript_cards:<LOADER_VERSION>:<ID_RULE>:<CHECKPOINT_SCHEMA>`.

For a frozen Kimi first-record copy, `enroll(frozen_path,
original_path=live_wire_path)` delegates identity to today's
`kimi_dialect.file_state`: metadata is read from the copy, while the original
path supplies the session and main/subagent mark without being opened. Callers
must bound the copy and enforce their enrollment policy. This does not install
Kimi ingestion or change the current dialect's `wire_identity` rules.

`card_projection.Projection(seat, parser)` is an opt-in local card consumer for
`Store.commit(..., projection=projection)`. Output, merged card provenance, and
parser position commit in one SQLite transaction. Replays verify coverage;
mixed legacy/unprojected batches require explicit migration. Consumers call
`projection.read(store, card_id)` rather than treating the last raw batch as a
complete card. Earlier record time/content hash wins and approved files,
sessions, and alternative hashes are retained. A later detector drop blocks
the ID locally; immutable old batches remain, and any server cleanup is still
unverified. `projection.capacity(store)` reports the store-wide lifetime ID cap,
remaining capacity and blocked IDs without card text. The default 4,096-ID cap
is a store-lifetime budget across all seats and parsers, including blocked IDs;
there is no retention that frees slots. At capacity, new IDs refuse while
updates to existing IDs remain allowed. Stop new-ID intake, inspect `capacity`,
and preserve the store/checkpoints. Continuing with new IDs requires an explicit
review of storage limits and a larger bounded `Limits.cards` configuration for
the callers; no automatic rollover, pruning or migration is implemented. Never
remove blocked IDs to bypass the cap or infer server cleanup from local changes.
No pruning or upload is performed. Projection is for screened `card.v1`; it is not applied to the
unscreened automatic transcript-context namespace.

An already enrolled job can be woken without reopening its transcript:
`python tools/transcript_cards/capture_queue.py wake --spool <existing-spool>
--job <enrolled-job-id>`. It increments only the requested generation and returns
after SQLite commit, preserving review/error/staged state. It creates neither
spool nor enrollment and does not dispatch a worker. The existing `scan` path
already handles missed wakes. Wakes, enqueue and scan are format-neutral hints:
`wake` works for an unbound, snapshot or incremental spool and does not choose
or change its format. The `stage` and `incremental` workers check the saved
`capture_mode` before advancing staged state, so only the matching worker can
complete that wake. A wake accepts no source, limits or window overrides.

`context_acceptance.py generate --output <new-directory>` emits a synthetic
role-aware corpus; `check --manifest <manifest.json> --observations <bridge.json>`
checks a test bridge's exact text, per-part roles, omissions, ordered task states
and namespace. Identical assistant text cannot stand in for an operator turn.
The default is today's unscreened `shared/transcript-context` policy, retaining
synthetic secret/name markers as written. This utility changes no production
admission policy. The test bridge covers actual current-parser dialogue output;
plan-state expectations, real graph recovery, native histories, two independent
readers and host reboot remain unqualified.

Enrollment, projection and wake are independent APIs. Automatic Kimi queue
scheduling and wiring the projection into the incremental worker remain
unimplemented. These APIs do not enable live seat enrollment or complete
dialogue recovery.

Record size, the owner's policy: a supported line is read whole, never skipped.

| dialect | largest line supported | reader budget (record / window) | measured on HQB, 10 Oct 2026 |
|---|---|---|---|
| Claude Code | 4 MiB | 4 MiB / 8 MiB | largest 2,728,515 bytes (all seats); on one seat 306 of 278,311 lines over 256 KiB, 22 of them typed prompts with a pasted image |
| Codex | 16 MiB | 16 MiB / 32 MiB | 6,411 files, 1,097,169 lines: 1,071 over 256 KiB in 471 files, 60 over 4 MiB, 0 over 16 MiB, largest 13,280,217 bytes; 38 of the 1,071 are typed user messages (36 with an image) |

A line over its dialect's limit stays PENDING and visible, with its offset, length and sha256, and its file does not
advance; every wake retries it. It is never skipped by the reader, the queue or this loader, because the large lines
include operator words: the default 256 KiB would have held 471 Codex files and 18 of one seat's Claude Code files.
Skipping any line is the operator's decision, not the reader's. None has been seen over these limits.

## Checks

The draft automatic-context queue, bounded raw-input reader and private atomic
output/checkpoint store have targeted synthetic checks. They do not enable
dialogue ingestion or install hooks. The opt-in `capture_queue.py incremental`
path connects `native_window.py` and `incremental_store.py` to the current
loader's `enroll`, `read_file`, and `approved_cards` APIs for explicitly selected
Claude and interactive Codex files. It uses a separately enrolled spool; snapshot
and incremental workers cannot complete wakes for the other staging format. Reader
bytes pass through the loader's existing parsing and privacy policy before commit.
The store verifies local batch digests and replay. Resume, existing enrollment
and subsequent commits also verify that the latest output matches its saved
offset, revision and semantic checkpoint; missing or corrupt output fails closed.
Resume performs those reads in one SQLite snapshot. Verification reads at most
the configured batch-output cap, rather than scanning retained history.
The store supplies no graph receipt, retention policy or proof of native
source-generation trust. Its store-wide batch/database caps stop capture until
reviewed cleanup; acknowledged pruning remains required integration work.
SQLite BUSY/LOCKED failures raise path-free `StoreBusy` and SQLite FULL /
`ENOSPC` failures raise path-free `StoreFull` (both `StoreIOError`
subclasses), allowing a caller to bound retries on contention and report
storage exhaustion as a deterministic conflict without treating corruption or
other I/O failures as contention. The store itself does not schedule retries.

Incremental staging visits one file per job per pass, with durable file/job
rotation and fixed high-water targets per wake. A job with N selected files
needs at least N passes to report staged, even if every file is caught up.
A later `enqueue` or `scan`
extends targets without resetting semantic state. Output and parser position
commit together before queue completion; restart replays the retained store.
Partial records and held pairs remain pending. Oversized records expose durable
per-file offset/observed-length/hash diagnostics in `status`, distinguishing a
complete record from an observed prefix. Equal-scope changed observations refuse
before commit; this is not a whole-file rewrite detector. Replacements, truncation,
parser upgrades and store exhaustion require review, with no automatic reset.

```
python tools/transcript_cards/capture_queue.py enqueue --spool <new-private-spool> --source <selected-source.json>
python tools/transcript_cards/capture_queue.py incremental --spool <new-private-spool>
python tools/transcript_cards/capture_queue.py status --spool <new-private-spool>
```

`--window` supplies an explicit JSON `native_window.Budget` override; defaults
allow 4 MiB Claude / 16 MiB Codex records. Limits bound individual reads and
retained store sizes, not total wall time or OS resources. Append-only source
history is assumed; an older prefix rewrite outside the current window can go
undetected. There is no uploader, hook installation, graph receipt or pruning.
The local cards API does not promise complete visible-dialogue recovery.
Run `python -W error -B -m unittest discover -s tools/transcript_cards -p
test_incremental_queue.py` for synthetic local integration. Opt-in
`verify_incremental_backlog.py --mib 16` checks bounded backlog/replay;
`verify_dialogue_tail.py --output <new-directory>` reports missing tail content
with exit 2, rather than treating successful staging as dialogue recovery.

Codex overrides require `records >= 2` and `read_bytes >= 2 * record_bytes`,
so the held user item and its following event can fit together. Incompatible
settings refuse with `CodexPairBudgetError`; increase the indicated budgets.
The parser checkpoint retains the legacy timestamp fallback naming counter
across windows, including held-item replay. Incremental parser identity uses
the `bounded-v3` suffix, refusing stores made before that counter was durable
or before built-in machine-prefix filtering was applied;
such stores need explicit migration/rebuild review rather than silent reuse.

`Store.batches(source, generation, parser, after_revision=0, limit=64,
byte_limit=...)` lists retained batches in revision order after a restart,
checking each returned output digest and the latest checkpoint in one SQLite
read transaction. Pages have a count cap and a total output-byte budget; an
oversized next batch or a revision gap fails rather than claiming a drain.
`BatchPage.next_revision` is a local paging position, never a delivery or index
receipt. Reads do not consume output or advance capture state. This provides
bounded replay for a future uploader; graph receipts and pruning remain open.

`frozen_outbox.py` adds a transport-free prototype on that same private store.
`FrozenOutbox.freeze(snapshot, safe_offset, items, manifest, checkpoint)` commits
the exact prepared UTF-8 wire bytes with the parser position. Items are `Record`
instances in a tuple; the manifest is another `Record`. Structural linkage and
cross-batch ordinal continuity are checked, but complete schema validation and
privacy screens remain the caller's responsibility. One local revision is one
wire batch, starting at batch one/ordinal zero in a fresh wire generation.
Existing non-outbox revisions cannot be adopted automatically.

Keys retain their padded decimal coordinates, but JSON `gen`, `batch`,
`ordinal`, `first`, `last` and manifest item ordinals must be integers.
Strings, booleans and floats are refused. Existing frozen records with string
coordinates stop for review without rewriting their bytes or parser position.

`next_action(batch_key)` returns `send`, `read`, `wait_predecessor`,
`review_conflict`, `review_redacted` or `complete`. Before transport, call
`begin_send(batch_key)` and use its returned record and attempt. It durably marks
uncertainty before returning bytes; an interrupted attempt requires readback.
`observe(attempt, value=..., evidence_sha256=...)` accepts a caller-verified
exact-key read, not a send receipt. Equal strict JSON values advance despite
object member order, whitespace or escape differences. Numeric types, array
order and string code points must match; malformed, duplicate-member, non-finite,
non-UTF-8 or oversized readback stops as a durable conflict. This comparison does
not authenticate or canonicalize signatures. Authenticated absence
permits replay of the same bytes under the server's write-once contract. Missing
or unverifiable signatures must never be converted to absence. Conflicting or
authorized-redacted readback stops for review. Items precede their manifest;
the next batch waits for the previous manifest's matching readback.

The `redacted=True` input is defensive coverage only. Context.v1 refuses
`memory.redact` with `E_CONTEXT_REDACT_UNAVAILABLE`; its server does not return
redacted context rows. Synthetic redaction tests do not establish a live path.

`replace_refused(attempt, code=..., item=..., manifest=...,
evidence_sha256=...)` accepts only a definite precommit `E_CONTEXT_SECRET` or
`E_CONTEXT_CLIENT_NAME` refusal for the current first send attempt. It atomically
stores a minimal omission, the corresponding manifest digest/count changes and
the ready state. Required identity metadata stays unchanged; optional and derived
metadata is removed. The caller must authenticate the refusal and fully validate
and screen both replacement envelopes. Invalid required metadata requires
quarantine or repair. Earlier uncertainty, retries,
historical attempts without the first-attempt guard, and manifest refusals cannot
authorize replacement. Original capture bytes and parser checkpoint remain
immutable; bounded, digest-checked private overlays supply the replacement bytes.
This retains the original private capture and does not redact it or the server.

This module does not verify signatures, source identity, authorship or redaction
provenance. Its evidence digest alone proves none of those. Optional
`evidence_artifact=bytes` on `observe` checks the SHA-256 digest and atomically
retains the private artifact with the receipt. `evidence(digest)` retrieves and
rechecks it. Artifacts are bounded to 512 KiB and share the store's 64 MiB
database cap; capacity failure leaves reconciliation pending. Digest-only
callers must independently verify and durably retain their proof. The module
uses the store's private transaction helpers and adds
receipt, first-attempt guard and omission-overlay tables. Shared limits still
apply; base64 packaging counts toward the output cap, and retained overlay bytes
have an additional per-batch output cap. It supplies no network transport,
native parser, hook, retry scheduler, receipt pruning or server
redaction integration. It remains disconnected from real transcript uploads.

`screened_outbox.freeze_screened` runs the canonical application schema validator
and its namespace screening policy before calling the outbox commit. The current
validator skips both secret and client-name admission screens for
`shared/transcript-context`, loading neither detectors nor the denylist. Other
namespaces, including scratch, retain both screens. Older pinned validators without
the namespace-policy switch retain both screens. It checks the trusted
seat and compares manifest counts with all items. A refusal, missing policy,
unavailable required screen or unexpected policy failure leaves the whole batch and parser
position uncommitted. Schema and manifest checks apply in every namespace; metadata on minimal omissions must
pass the canonical fixed-shape constraints too. Native and agent IDs must be
locally hashed to 32 lowercase hexadecimal characters before admission, and
adapter versions use numeric dotted components. Existing frozen bytes are never
rewritten to migrate an older schema; callers must validate them before sending.
Errors expose bounded codes without matched text or policy exception paths.
The canonical validator is an application dependency; it is not bundled here.
This optional entry point does not change direct `FrozenOutbox.freeze` callers,
implement server grants or source identity, or authorize production uploads.

Callers must distinguish retryable dependency failures from final refusals.
This module supplies no automatic retry scheduler or transport classifier:

| Code | Caller action |
| --- | --- |
| `E_CONTEXT_SCREEN_UNAVAILABLE` | Retry unchanged once the required configuration or policy is available; rerun preflight. The pinned validator also returns `None` when the context namespace is not configured. This mapping keeps the batch pending until its claim/configuration is available; it does not establish a screening outage. |
| `E_CONTEXT_MANIFEST_PREDECESSOR`, `E_CONTEXT_MANIFEST_ITEM_MISSING` | Server-only dependency codes in the pinned registry; retry unchanged after the predecessor manifest or item commits. Confirm the deployed server contract. The pinned pure validator below cannot emit these codes. |
| `E_CONTEXT_SECRET`, `E_CONTEXT_CLIENT_NAME` | Stop; only a definite first-attempt precommit refusal can permit the validated omission replacement described above. |
| `E_CONTEXT_KEY`, `E_CONTEXT_SCHEMA`, `E_CONTEXT_TIME`, `E_CONTEXT_DIGEST`, `E_CONTEXT_TOO_LARGE`, `E_CONTEXT_TEXT_ENCODING`, `E_CONTEXT_SEAT_MISMATCH`, `E_CONTEXT_MANIFEST`, `E_CONTEXT_MANIFEST_ORDER`, `E_CONTEXT_WRITE_ONCE`, `E_CONTEXT_REDACT_UNAVAILABLE` | Final refusal; review or repair instead of blindly retrying the same bytes. |
| `E_CONTEXT_LOCAL_POLICY` | Final local error for an unknown policy code or unexpected policy failure; investigate before retrying. An unknown code is not evidence of an unavailable screen. |

Every refusal leaves the batch and parser position uncommitted. Recognizing
server-only codes here does not implement database admission or transport retry.

`authenticated_readback.reconcile_present` provides a read-only bridge for this
identity's own frozen records. It calls the existing signed `memory.read`
transport with private verification evidence enabled, fetches fresh writer keys,
checks server and writer signatures and binds the request, namespace, key,
writer, target, seat and strict JSON value. Only present, unedited version-one
heads can produce a matching receipt or a durable value conflict. Missing,
refused, replayed, malformed or redacted answers leave the attempt pending.
The signed envelopes and public keys are retained atomically with the receipt;
no signing seed, JWT or DPoP token is retained. Saved keys document verification
against the configured server's current key endpoint; they are not an independent
trust root. The bridge does not authorize absence, retry, refusal replacement,
redaction, namespace admission, privacy screens or production uploads.

```text
python tools/transcript_cards/test_capture_queue.py -v
python tools/transcript_cards/test_native_window.py -v
python tools/transcript_cards/test_incremental_store.py -v
python tools/transcript_cards/test_frozen_outbox.py -v
python tools/transcript_cards/test_authenticated_readback.py -v
```

The private store checks every existing ancestor and refuses symlinks. On macOS,
the default temporary directory often starts with `/var`, a symlink to
`/private/var`. For these offline tests, create a private temporary directory
under the direct path and set `TMPDIR` before starting Python:

```sh
context_test_tmp=$(mktemp -d /private/var/tmp/meshy-context.XXXXXX)
TMPDIR="$context_test_tmp" python tools/transcript_cards/test_incremental_store.py -v
# Use the same TMPDIR prefix for the other commands above and the optional test below.
rmdir "$context_test_tmp"
```

The chosen directory and all its ancestors must be direct directories. The store
does not resolve symlinks to bypass this check. This workaround supplies no macOS
validation evidence for a run performed on another host.

`test_context_wire_compatibility.py` optionally checks synthetic batches and
minimal refusal replacements against the hash-pinned pure validator from
canonical main at `11a86f54e616b3dcf2fd7293ee45d0b56a45d45a`, with
`apps/context/validation.py` re-pinned to the v1.1 step 2 server (no admission
screens on `shared/transcript-context`; other namespaces unchanged). Set
`MESHY_CONTEXT_VALIDATOR_SNAPSHOT` to the directory containing its flattened
`apps__context__validation.py`, `apps__context__screens.py` and
`apps__memoryskills__errors.py` snapshots, then run
`python -m unittest discover -s tools/transcript_cards -p test_context_wire_compatibility.py -v`.
The test skips without that explicit snapshot and refuses different file hashes.
It requires Django and the repository's Python dependencies. Its seventeen tests
cover synthetic schema compatibility and local preflight using the real secret
detectors with a temporary synthetic client-name denylist. They check whole-batch
rollback on late refusals, opaque ID acceptance, raw ID and legacy-version refusal,
minimal omission metadata constraints, missing/unavailable policy,
trusted-seat binding, manifest counts and durable successful reopen. A malformed
first-batch manifest reproduces a real final refusal from the pinned validator.
Injected server-only and unknown errors check bounded code preservation and
final fallback classification; they do not exercise server admission. They do not
establish database admission, authorization, signed readback or production
acceptance.

Set `MESHY_SYNTHETIC_LARGE_WINDOW=1` in the host environment to include the generated
778 MiB reader test. It proves bounded input reads and original-offset continuity,
not graph recovery, parser checkpointing or queue scheduling fairness. See
`docs/design/automatic-transcript-context.md` for the integration limits.

```
python tests/test_transcript_cards.py                           # 358 checks; no database
pytest tests/test_transcript_cards.py -q                        # the same, as one test per group (T1 to T27)
pytest tests/test_transcript_cards_read_file.py -q              # read_file, checkpoints and approved_cards
python tools/transcript_cards/mutate_loader.py                  # 167 mutants of the loader, each must fail a check
python tools/transcript_cards/attack_redaction.py tools/transcript_cards   # planted typed secrets and clean prose
python tools/transcript_cards/second_scan.py <cards.jsonl>      # token-shape scan of your cards, masked
python tools/transcript_cards/third_scan.py <cards.jsonl>       # keyword-context scan of your cards, masked
pytest tests/test_transcript_cards_load_hook.py -q              # load_hook.py: fake stages; reaches no server
pytest tests/test_transcript_cards_accept_seat.py -q -p no:cacheprovider --noconftest   # accept_seat.py; no database
python -B tools/transcript_cards/mutate_accept_seat.py          # 29 mutants of accept_seat.py, each must fail a check
```

Every detector is red-proved in the test file: a planted string that only that detector catches is dropped from the
cards, and with the detector disabled it reaches them. The two scans are different methods on purpose. Each review
round so far found a real miss with them: two on the first seat, a pairing code on the second, a redeem code and
an offer link on the third. Run both scans on your own cards and read every line they print before you trust the
cards.

`attack_redaction.py` and `third_scan.py` are memory-bxxxxx's.

## History

- 1.23 (15 Oct 2026, #315): a second Codex prompt in the same millisecond is a card of its own. The fallback id
  `codex:<session>:<ts>` gets `#n` for the n-th distinct prompt at one timestamp (`state["fallback_last"]`), so
  two prompts no longer collide on one card. Closes #314 defect 2. Checks T25m and T25n.
- 1.22 (13 Oct 2026): an Antigravity step_index reused after a restart is a card of its own; a cursor from before
  1.22 rereads that file from the start.
- 1.21 (13 Oct 2026): `--machine-prefix` drops a seat's own automation text (class `machine_prefix`), so an
  injected wake-up line never becomes an operator Candidate.
- 1.20 (11 Oct 2026): the Antigravity dialect (`--antigravity-brain`), its reading half in `antigravity_dialect.py`
  beside the loader: reads Antigravity CLI JSON Lines brain transcripts (`transcript.jsonl`). Extracts
  operator-typed prompts from `USER_EXPLICIT` / `USER_INPUT` (with harness wrappers `<USER_REQUEST>`,
  `<ADDITIONAL_METADATA>` stripped), `/goal` objectives, and answered modal rulings from `ask_question` tool calls
  paired with human answers. Drops model thinking blocks (`antigravity_thinking`), generic tool outputs, subagent
  sessions, and harness system messages. Open asks persist across capture windows in `cursor["antigravity_open_asks"]`.
  Attribution extraction `antigravity/<kind>`. Checks in `tests/test_transcript_cards_antigravity.py` and
  `tests/test_transcript_cards_attribution.py`.
- 1.19 (11 Oct 2026): `attribution` on every card (gap register item 7; the server half is #232). A Question is
  the seat's model asking, so its `source.first_hand` is now false. A pasted document keeps its card and says
  `quoted_author: "unknown"`. The dialect is recorded on each raw card by `read_file` and passed to the builders
  as `dialect`. Checks: tests/test_transcript_cards_attribution.py (27 tests, 15 mutants, each seen to fail it),
  T23h and T25e updated. What a seat sees after updating: new cards write as before, but `card write --update`
  over a Question written before 1.19, or over a card 1.19 now marks as a paste, is refused with E_CARD_FROZEN
  naming `source.first_hand`. Leave those cards as they are.
- read_file (10 Oct 2026, loader version unchanged at 1.17): the per-line parsing moved out of `run()` into
  `read_file`, with `enroll`, `new_checkpoint` and `approved_cards` beside it, for a caller that reads one bounded
  window at a time. No card changes: on one seat's 26 transcripts (792 MB, 278,811 lines) the loader before and
  after gave the same 812 Candidates and 169 Rulings, 0 differences in accept_seat.py, both scans equal. The
  version stays 1.17 on purpose, because a new version re-versions every card already on the server.
- 1.18 (10 Oct 2026): dxxxxx's seat table gains `seat_hardware_short_hex`: 8+ hex with a letter and a digit,
  within 200 characters of a hardware word, mac, serial or u-boot. Before it, dxxxxx held 9 cards back by hand,
  which the seat hook load of #217 (load_hook.py) would not do. On dxxxxx's 1,464 cards from 1.15 it drops 8 of the 9 and
  no other card; the 9th names no identifier. Checks: T15c, and a seat case in attack_redaction.py that only
  this rule catches (exxxx-session found the gap).
- 1.17 (9 Oct 2026): the Kimi dialect (`--kimi-sessions`), its reading half in `kimi_dialect.py` beside the
  loader: which files are a seat's, and what one wire record is. It builds no card. Typed turns
  (`origin.kind` `user`), `/goal` objectives (`goal.create`), and AskUserQuestion answers tied to their calls by
  `toolCallId` are the cards; every machine origin, compaction and bookkeeping record drops by class. Measured
  on nxxxxx's seat: 1,816 main wires, 521,482 lines, 2,459 cards, zero over the server limit. Checks in
  tests/test_transcript_cards_kimi.py; mutate_loader.py mutates both loader files and runs both suites.
- 1.16 (8 Oct 2026): the Codex dialect. Typed prompts and asked questions from Codex rollout files
  (`--codex-sessions`), with the postbox's typed deliveries dropped by class. `screen_prose` no longer raises on
  a hand-written card whose options are not a list (pxx), and reads every string of an option. Two labels
  added to the roster: `codex-kxxx`, `codex-kxxx-cli`. A Claude seat's cards are the same as under 1.15.
- 1.15 (8 Oct 2026): from memory-bxxxxx's review of 1.14. `fit_to_limit` also cuts option descriptions, last,
  and measures a cut without file names and sessions; a builder refuses a card still over the server's limit
  as `card_too_large`. Checks for a negative index, a cut Question card and the stored transcript record. A
  card that was not cut by 1.14 is the same card; one that was cut is cut a few characters later.
- 1.14 (8 Oct 2026): importable builders for a writer that is not the transcript walk (`answered_question_card`,
  `asked_question_card`, `screen_prose`), and the id rule of a Question in `card_identity`. The walk mines the
  same cards as 1.13; only `loader_version` in them changes.
- 1.13 (8 Oct 2026): a seat's report pasted by hand, opening with the seat's label and a colon, is dropped as
  `peer_report_pasted` (memory-bxxxxx found the class; four seats read their 49 instances before the rule was
  written). `--own-name`, `seat_own_names` in the report, `jxxx` on the roster.
- 1.12 (8 Oct 2026): the oldest shape of a mid-turn message has no origin at all and was dropped by 1.11
  (memory-bxxxxx: 30 on its seat, all from June 2026). Prompt mode with no origin is now read too.
- 1.11 (8 Oct 2026): what the operator types while a turn is running is now a card (exxxx-session: such a message
  is an `attachment` record of type `queued_command`, and every attachment was being dropped). `hxxxxx` joins the
  roster (pxx). `--seat` is documented as the roster label.
- 1.10 (8 Oct 2026): the relay header must name a known seat (pxx: 1.9 dropped `From memory: we agreed ...`, the
  operator's own sentence). `KNOWN_SEATS` and `--seats`.
- 1.9 (8 Oct 2026): a typed prompt that opens with a seat's name as a header is a relay and is dropped as
  `peer_message` (vxxxxxxxxxx found the shape on two seats; 8 records on a third). The limits section says what a
  Candidate does and does not prove.
- 1.8 (8 Oct 2026): two findings from seats that ran 1.7. A card is cut to fit the server's value limit (dxxxxx:
  one Ruling of 54,532 bytes). Machine-typed text is cut from where it starts, and the operator's words before it
  are kept; the launcher's prompt is looked for anywhere in a typed prompt (memory-bxxxxx: 19 characters of the
  operator's, then the launcher's text, in one record).
- 1.7 (8 Oct 2026): a Ruling is named by its question call and the question's place in it, so the server's
  answer-time hook and this loader make the same key (kxxx's rule, with the index added after measuring).
  `source.transcript_uuid`; the id rule is recorded in `cursor.json` and an older store is rebuilt.
- 1.6 (8 Oct 2026): text a machine or another seat wrote is no longer taken for the operator's. New drop classes
  `peer_message`, `wake_injection`, `harness_text`, `pasted_content_only`, `attachment_only`, `answer_empty`; a
  pasted block is cut out of a prompt. Found by checking this loader's cards against the injected-text list in
  a reviewer's miner and the fleet's transcript digest.
- 1.5 (8 Oct 2026): two server rules met before any seat writes (kxxx). Card times have one spelling, and a
  record with no time or no zone is dropped as `no_timestamp`; `report.json` gives the largest card and counts
  cards over the server's 8 KB limit.
- 1.4 (8 Oct 2026): for the server design (kxxx). The card id no longer includes the content hash, so it is
  stable for one record and block whatever the text; `schema` field; a store in the older format is rebuilt.
  Repeated Candidates are labelled (`repeats`, `repeat_last_time`, `repeat_of`), from dxxxxx's recall run.
- 1.3 (8 Oct 2026): from pxx's acceptance run on 1.2. `lan_capability_url` corrected by memory-bxxxxx (an offer
  link carries its capability in a middle path segment of 24, and a dated slug of 32 was being dropped); an
  upper-case UUID is a seat rule for `pxx`.
- 1.2 (8 Oct 2026): from pxx's run on a long compacted session. `pasted_output` drop class; `/goal` text as a
  Candidate; bare slash names dropped; `ruling_prefilled_by_model`; per-seat detector table; exit 3 when nothing is
  read; base64url codes in `opaque_token`. Password rules corrected by memory-bxxxxx (the value must be on the same
  line as its name: sudo's prompt was taking the next line of terminal output as a password), with
  `password_block_value`, `password_next_line`, `lan_capability_url` and `quoted_code` added. One pytest test per
  group (kxxx).
- 1.1 (8 Oct 2026): typed-prompt test by `origin.kind` (pxx); `pairing_code`, `mac_address`, `hardware_hash` and
  two-class codes in `opaque_token` (dxxxxx); the 11 typed-shape detectors and the removal of a rule that dropped
  prose (memory-bxxxxx); `files_not_read`, `files_read` and `--marker` (vxxxxxxxxxx, memory-bxxxxx); `--rebuild`.
- 1 (8 Oct 2026): first version, never committed.
