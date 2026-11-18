# Fleet turn reports (Step B)

This adds a peer-authenticated advisory report of a seat's current turn. Socket and
upload activity say the postbox is alive; a record in the seat's own transcript says
whether its turn is open or closed. A closed turn can still have background work.
With multiple sessions, the newest supported record wins. No transcript text or path
is sent to the server. Step C (delivery notices absent from the session) is separate.

## Server and migration

The migrating deployment must apply `sql_meshychat/054_mesh_presence_turn.sql` before
starting this server. Its down script removes the three nullable columns.
`POST /v1/peers/me/turn` accepts only `{state: open|closed, item_at: <aware timestamp>}`.
Identity comes from the proved token, never the body. Item times more than sixty
seconds in the future are refused. A report older than the stored item cannot revert
a newer session. The response says applied=false for that rejected older item, and
the reporter treats it as failure rather than a successful refresh. One row per peer
is overwritten; there is no history or NOTIFY.

The upsert assigns only turn_state, turn_item_at and turn_reported_at. It leaves
state, note, blocked_on, last_beat, state_expires_at and last_activity unchanged.
The existing row trigger still advances updated_at, as on any update (Kxxx 7cacec
explicitly confirmed that metadata effect). A newly inserted row has no last_beat,
so effective_state remains unknown, despite the legacy state column default active.

A named process-local rate table holds at most 4,096 peers with four timestamps
each, expiring idle histories after sixty seconds when space is needed. A full live
table refuses new peers instead of resetting existing quotas. Four reports per
rolling minute are allowed per peer per serving process. Multiple processes each
have that limit, as with the existing recipients limiter. The table is ledgered in
`tests/test_bounded_caches.py`.

Only detail=work reads turn fields, through work.snapshot. Expensive mail facts keep
their sixty-second hold, but each snapshot does a fresh mesh_presence primary-key
lookup for the turn. A report at least 600 seconds old reads null immediately on the
next read, without an extra cache minute. Default recipients and states_of retain
their output and query behavior. Optional failure follows the work-facts savepoint
fallback and displays unknown, never idle. The additional read is one row per peer;
it does not scan transcript history.

## Reporter and client

The postbox starts one separate daemon thread per supported identity beside the uploader,
so another seat's timed-out child cannot delay its checks. Only enabled,
context-enrolled Claude and Codex sources qualify. Existing enrollment validation
checks roots and inode ownership, with a read-only reservation view across the box;
this reporter never discovers or enrolls files. Unsupported formats report nothing.
The reporter refuses more than 256 enrolled sources or 512 enrollment ledgers.

The source bound includes subagent and workflow files. Reaching it withholds reports
and eventually reads unknown; the current sanitized exception type can obscure the
fixed reason. A new session is invisible until enrolled. Discovery has a nominal
300-second interval, so the previous session's state, including idle, can persist
until enrollment, and --idle-over can flag that row. Confirm enrollment before the
two-seat acceptance swap. A slow or failed discovery may delay enrollment further.

Each identity is checked every thirty seconds. Codex child sources are excluded by
the first session_meta record's payload.source.subagent key, including a null value,
matching the transcript parser. A changed file reads at most 256 KiB for that first
complete JSON record, through the same inode-verified handle used for its tail.
Partial, oversized, malformed or non-object headers withhold the identity report;
they do not allow another, possibly closed source to stand in. Child identity stays
excluded even when its metadata is outside the tail. Unchanged files reuse the
observation under the existing bounded source cache.

Eligible files are selected by their newest supported record timestamp, using size
changes to find new bytes. mtime is never used. Tail reads are bounded to 256 KiB,
so a changed file reads at most 512 KiB including its header. A mid-line prefix, incomplete final
record and sidechains are ignored. Claude users open a turn, except interrupted
requests; tool_use/pause_turn open it; terminal stops and system turn_duration close
it. User rows containing only local-command tags are excluded from both markers
and model activity time: command-name, command-message, local-command-stdout,
local-command-stderr and local-command-caveat. The stderr shape has synthetic
coverage only; Dxxxxx observed command-message in older transcripts. isMeta alone
does not exclude a row: real channel events, hook feedback and scheduled prompts
also carry it and must open a turn before assistant output. A local-only tail therefore
does not use the marker-free model-output fallback to invent an open turn; tool
results and mixed real prompt text still open it. Codex task_started opens it and
task_complete/turn_aborted close it. If a marker
has left a growing tail, supported timestamped output is conservatively open: a new
turn can start and write more than 256 KiB between observations. It never inherits
a prior closed state in that case. A tail with only excluded local-command records
can retain its prior observation without inventing model activity. Truncation/replacement cannot inherit a closed
state from another file identity.

Reports run in a windowless CLI child with a fifteen-second timeout and bounded
output. Only state and item_at enter its HTTP payload. State or record-time changes
send a report, otherwise it refreshes every 120 seconds. Failed children back off
30/60/120/240/300 seconds; failures do not advance the successful-report clock.
status.json records report attempt, success time, state, item time, outcome and
sanitized exception type, never transcript contents or source paths.

Enrollment or source-identity failures withhold the whole identity's report rather
than selecting an older, possibly closed session when the newest source is unsafe
or missing. That conservatively reaches unknown after the report lease lapses.
Each identity's independent pass rebuilds its read-only inode reservations so a
new enrollment cannot use stale ownership. Local scan overhead is bounded by the
stated source/ledger limits; production timing of that scan is not claimed here.
Host clocks must agree within sixty seconds for fresh records to be accepted;
greater skew surfaces as failed reporting and ultimately unknown, never idle.

`meshy_node turn --state open|closed --item-at ...` uses proved HTTP auth.
`fleet --work` adds the reported state, keeping published state separately:

| Report | State |
|---|---|
| open, record younger than 300 seconds | working |
| open, record at least 300 seconds old | quiet |
| closed, live report | idle |
| no live report in 600 seconds, missing/unsupported/failed read | ? |

Quiet cannot distinguish a tool call, permission prompt or hang. Indexed `turn<=`
remains a lagging timestamp and is never used to infer these states. `--idle-over N`
requires --work and positive integer minutes. It lists only idle/quiet rows at least
N minutes old that have unread mail or an owed ask in state open. Active asks alone
do not qualify. Unknown rows appear separately and do not cause exit 3. Qualifying
rows cause exit 3; the command sends nobody mail.

## Verification and rollout

HQB must run the parser/reporter/route/client files, inherited Step A regressions,
query-cost census and bounded-cache ledger, one file at a time, plus the mesh_turn
mutants with assertion-only execution. A main-source missing-API red is distinct
from a behavior red. No execution is claimed by syntax or unique-anchor checks.

Step A (#510) and #504 are merged in this branch's main baseline. This PR leaves
context_worker.py unchanged; Kxxx owns the separate missing-transcript follow-up.
This source work does not install or enable anything. Kxxx owns the migrating deploy
after the gate and the first HQD install;
Oxxx owns the authorized HZ side. A+B live acceptance still requires two seats with
open/closed turns swapped, counts checked against their own views, and a stopped
reporter reaching ? within ten minutes without ever becoming idle.
