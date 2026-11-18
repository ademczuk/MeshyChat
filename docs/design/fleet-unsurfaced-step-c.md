# Step C: dispatched notices without session evidence

Brief: Kxxx's authenticated request `msg/<key>`, on main
`9826ee538f7b4e9bcaa28631363bdf20153733f1`. The measured Codex notice was queued
at 10:26:24Z and first surfaced at 11:07:08Z, despite another turn in between.
The Claude example had 27 dispatched notices absent from the session; its later
successful messages and a nearby login establish correlation, not a cause.

## Qualification and display (including Kxxx 1165eb amendment)

A dispatch qualifies only when it is at least 300 seconds old **and the reported
turn is closed**. An open turn, including a quiet open turn, defers loss classification;
it does not settle or hide pending keys. Signed Kxxx `1165eb` adds a separate
`held` count/oldest time for notices aged at least 300 seconds on open turns.
The same two wire/database scalars carry the aged pending count/time; the
token-bound reported turn distinguishes held/open from lost/closed. A hung
open turn therefore never looks clear just because loss qualification is deferred.
This lets notices land at the next turn without claiming an active session lost
them. The `lost` column is count/oldest age,
for example `3/41m`. Its legend says delayed or possibly missing, never proof of
loss or a judgment about a person. The count may include delays that later clear.

`--idle-over` prints qualifying notices separately from idle seats, with separate
unknown turn, held/open and incomplete-observation sections. It makes no sends.
Positive delayed or held counts, or the existing idle backlog condition, return exit 3; incomplete
coverage alone does not. Plain `fleet` is unchanged.

## Local evidence and bounds

`tools/postbox/unsurfaced.py` observes existing consumer dispatch/skip logs next
to the identity's configured spool. It never edits a delivery offset, bank,
handled file, ACK, cursor, state file or lookup ledger. No delivery code changes.

The independent Step B thread owns `unsurfaced.json` in the identity's context
store. The checkpoint atomically stores pending keys/times/versions and its own
read offsets. It is separate from delivery and survives reporter restarts.
Identity names must match; transcript inodes must match Step B's verified enrolled
sources. Symlinks and non-regular files are refused by the existing private-file
helpers. Codex children remain excluded by Step B's first-record check.

Per identity and pass:

- Dispatch and skip reads share 256 KiB; transcript growth shares another 256 KiB.
  Only pending exact keys are matched, in timestamped JSON records after dispatch.
  Nothing scans historical transcript bytes or a whole transcript. A snapshot of
  transcript sizes precedes log ingestion. A log backlog defers transcript scanning
  so bounded ingestion does not consume session evidence before its key is known.
- A complete JSONL record is bounded at 256 KiB. Partial rows retain their boundary;
  oversized or malformed rows mark coverage incomplete. No unbounded row buffer.
- At most 64 log streams, 384 stored cursors, 4096 directory entries and 2048 pending
  keys. Overflow keeps the newest dispatches and marks incomplete coverage on the
  eviction pass, even if all retained keys settle in that pass. A later fault-free
  pass may start an explicit new coverage window under the recovery rules below;
  the dropped window is never relabeled complete. The checkpoint is capped
  at 2 MiB. Unchanged state is not rewritten.
- The existing identity's `.read.json` ledger settles a key only at a covering
  version and matching peer ID. Reads may precede dispatch, matching delivery's
  already-read rule. Its bounded private JSON is cached by inode/size, capped at
  1 MiB and 2000 records; no network lookup or signed-read polling is added.
- An `already_read` skip settles only the same key at a covering version. Other
  skip reasons or another identity's lookup do not. Transcript appearances must
  match the exact key and have a timestamp at or after its latest dispatch.
  Duplicate dispatches preserve the first pending age and latest settlement bound.
- Archives are observed before current log names, preserving inode cursors across
  rotation. Disappearing unread streams, damaged checkpoints or unreadable evidence
  cannot claim complete coverage.

On the first observation both existing log and transcript cursors start at EOF.
Historical dispatches are excluded, as directed by authenticated Kxxx review
6b9bc6. The durable unsurfaced_since baseline is shown as an age in fleet and an
exact timestamp in JSON. Zero means none since that baseline, never an all-time
all-clear. A new enrolled session born after since starts at zero without taint;
filesystem birth metadata or a bounded first timestamped record establishes age.
The header fallback reads at most 4 KiB, charged against the transcript budget.
An older or unverifiable file enrolled late starts at EOF and taints pending evidence. A replaced/truncated transcript
starts a new EOF baseline and marks current pending evidence uncertain. It never
inherits an earlier session's certainty. After a gap or overflow, empty pending and drained backlog can recover on a pass
with no new fault. Otherwise 30 minutes without a new fault starts recovery even
with pending notices or a torn tail. The durable last_fault_at clock survives
restart; a new fault restarts the interval. Pending keys and original dispatch
ages are retained and uncertainty flags clear; backlog cursors adopt captured
EOF, discarding a torn continuation. Continuing evidence faults remain partial. A new
since baseline then starts; the old window is never relabeled complete. The
previous since, issue kind and rebase time persist in the observer checkpoint and
sanitized local status. Inactive missing cursors are pruned after reporting their
gap. A damaged ledger that faults on every pass remains incomplete until readable.
After quiet recovery, an arrival inside the gap can remain counted until a
covering own read or later session appearance settles it. This is an explicit
visibility trade accepted in Kxxx review58f2ac; the fleet legend explains it.

## Count, oldest, since and migration 055

`POST /v1/peers/me/turn` accepts the existing pair or exactly five fields:
`state`, `item_at`, `unsurfaced`, `unsurfaced_oldest_at`, `unsurfaced_since`.
The requested baseline is a third nullable observation scalar; there are no
separate held columns or duplicate pending maps. Every non-null observation
requires a timestamp with timezone no more than 60 seconds in the future. It remains token-bound,
four reports/minute, with a 600-second lease and no presence NOTIFY. The fresh
primary-key work snapshot reads these fields outside the heavy mail-facts cache.
Tracker exceptions are isolated from the Step B report: the reporter sends the
bare state/item_at pair, clearing the server's old observation to unknown, and
records only the exception type in local unsurfaced_error status. The next normal
cadence can recover observation fields without transport-failure backoff. A send
failure still uses the existing bounded backoff.

`unsurfaced` is nullable. Nonnegative values are known qualifying counts up to
2048; zero requires a null oldest time. A positive count needs
an oldest timestamp at least 300 seconds old on the reporter clock. The server
allows 60 seconds of clock skew (rejecting ages below 240 seconds) so a small
clock difference at the boundary does not reject the entire turn report. The
tracker's 300-second threshold and fleet display qualification remain unchanged.
Open counts are held, closed counts
are qualified delayed/missing evidence; these are not separate duplicate maps. **-1 is the explicit incomplete
coverage sentinel**, not a count; it displays `? partial`. It covers overflow,
reset uncertainty and incomplete scans without leaking keys. Its oldest time, when available, is only the oldest known candidate.
Local sanitized status separately records overflow, backlog, incomplete and the
observation start time. An open turn publishes its aged pending count as `held`;
its `lost` cell is `deferred`, with an explicit held count rather than a hidden zero.
Old clients omit all three observation fields and clear an earlier observation to null, never keep
an obsolete zero. Expired/missing reports are unknown. Invalid counts, booleans,
extra fields, paths or keys in the payload are rejected.

Migration `055_mesh_presence_unsurfaced.sql` adds three nullable scalar columns
and pair/count constraints. Its down script includes the required rollback runner
header. Both use a short lock timeout. Apply 055 before serving the new code.
Kxxx owns migration and production deployment, Oxxx its authorized installation.
This source change installs or restarts nothing on HQD, HZ or production.

## Validation route and limits

New fixture files cover the delayed 33d6ae timeline, 27 persistently absent notices,
both transcript shapes, exact 299/300 boundary, open deferral, own lookup/version,
skip reasons/version, reset, overflow, persistent offsets, rotation, prefix/old-time
negative controls, bounded backlog and corrupted/missing evidence. Route tests
cover privacy, validation, expiry/cache independence and no presence notification.
CLI tests distinguish delay, held, idle and partial coverage without sends. An
end-to-end fixture checks held/open -> lost/closed, arrival -> zero, and a second
notice arriving while still open -> held zero -> no loss on close. Thirty
Step C mutants target these guards. Added review fixtures cover the real dispatch
shape without version (canonical default 1, malformed explicit versions refused),
cold-start EOF, durable since, 27 pending across a new session using birth metadata
or bounded header fallback, and gap/overflow recovery into an audited new window. Step B's changed privacy anchor is updated;
its other anchors remain applicable.

Only static AST, exact-anchor and diff checks run on HQD. Dxxxxx performs source
review; HQB runs scratch-Postgres tests with migration 055, all new mutation
controls/kills and inherited anchors. Then exact-head Codex and one full gate.
No current execution or gate pass is claimed by this draft.

Dispatch logs are best-effort hand-over records, not a transaction with transcript
appends. The observer cannot detect an entirely absent dispatch-log append.
Similarly, same-inode same-size transcript rewrites are outside the append-only
growth assumption. Multiple enrolled sessions are observed; a matching key in any
eligible own session is session evidence, not proof of a reply or completed work.

Kxxx review58f2ac confirms prior P1/three P2 fixes and adds quiet recovery with
pending retained. Fixtures cover malformed/torn logs, reset, overflow and late
old enrollment, exact1799/1800 boundary, restart and a new fault resetting quiet.

HQB finala211e6 on51e4cbe3:340passed/2failed across15 files; mutants not run
on red controls. Rotation now looks up inode cursors for a new archive path as
well as a replaced path, preventing settled notices from replaying. Both birth
metadata fixture wrappers forward private-file keyword arguments across restart.
The existing functional rotation fixture covers reuse plus new delivery; U25
restores the observed defect. Rebased onto mainb7ecb7bf after merged516. Those
historical failures are not relabeled by these source fixes; new execution is required.

The follow-up to the8ddce32d focused run fixes archive-chain rotation, which
can overwrite a pathname cursor before its old inode is visited under .2.
A bounded per-pass copy of prior cursor positions preserves that inode evidence;
lookup picks the furthest matching offset among current and saved cursors. The
new second-rotation fixture requires both old notices remain settled and new
delivery still counts; U26 removes that snapshot. HQB subsequently validated
7560fa2d: observer36 and reporter15 passed, all7 mutation control files green,
and26+20 mutants caught on assertions includingU26. This is delta evidence,
not a full gate or validation of later changes.

Dxxxxx source review 52e218 on7560fa2d found three P2 issues. The next delta
isolates tracker errors from the plain turn report, permits bounded server clock
skew, and marks eviction as a fresh fault before recovery. Regression fixtures
cover error status/privacy and recovery, the five-second-skew endpoint case and
239/240 server boundary, and same-pass overflow/covering skips at capacities2 and
2048. U27/U28/U29 remove those three guards; U14 tracks the revised server boundary.
New-head source and executable validation remain required. Nonblocking P3 notes
from that review remain limits: strict transcript timestamps/raw JSON key matching,
unreachable-through-reporter stream-limit latch, sustained-backlog quiet recovery,
rotation races within a pass, and append/replace assumptions for local evidence.

The follow-up to Codex finding4478977141 compares each matched inode's size with
its previously observed size before updating that value. A shrink marks a fresh
coverage gap for dispatch logs, skip logs and transcripts even if the new EOF is
at or above the consumed offset. Existing offsets and I/O bounds are unchanged.
Six regression cases retain a complete JSONL prefix, truncate in place to or above
the read offset, restart from the checkpoint and settle any known pending key;
the loss must still report incomplete before a later explicit recovery window.
U30 removes the size comparison and must fail those assertions. This child needs
its own execution, source review, exact-head Codex review and full landing gate;
the earlier gate on2fd5d195 does not validate it.
