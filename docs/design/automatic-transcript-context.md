# Automatic transcript context: proposal, not production authorization

Seats: codex-kxxx-cli, codex-oxxxxxx-admin-cli, codex-oxxxxxx-cli, Nxxxxx
(Kimi), and Antigravity (agy 2.0 / Gemini JSONL, reported by its authenticated owner;
native installation and full-context recovery remain to be independently proved).
Baseline: main 6c67f65f7a415ca5cbd6e73b72fc2963f90dbd65, loader 1.16.
Nxxxxx owns the existing Kimi dialect in PR188; do not duplicate it.

VxxxxxXxxxx and kxxx independently confirmed that current transcript-derived
production data is card-only. The existing loader's exclusions are intentional.
The Obsidian hosted extraction hook is a separate path and is not reused here.
This proposal preserves Question/Ruling/Candidate semantics and their existing
namespace. Dialogue must never be disguised as a Candidate or written there.

## Capture and local persistence

Keep the current loader as the parser/redaction owner. Extend its adapter layer
only through the responsible owner review. Capture displayed user and assistant
messages and explicitly allowed tool results/artifact references. Exclude hidden
reasoning, credentials, foreign homes, harness injections and unattested peer
content. Tool dialogue is evidence from that seat, not a recorded decision operator ruling.
Keep native parent/subagent relationships only when the source actually provides
them; do not infer Mesh thread IDs from local session IDs.

Native lifecycle hooks enqueue a small wake into a local durable queue. A separate
worker performs capture/upload. SessionEnd alone is insufficient; start/resume,
turn completion and compaction wakes plus bounded local catch-up are required.
Codex transcript_path may be null and its format is not a stable API. Each runtime
needs versioned synthetic fixtures and an observable unsupported-format error.
The exact installed hook contract must be measured on each host before enabling.

The initial `capture_queue.py` is a local staging prototype around the unchanged
loader. It stores an operator-supplied seat/kind/root and explicit relative file
list, never hook payloads, uses
SQLite FULL synchronization, a kernel worker lock and separate requested/staged
generations. A wake during capture remains pending. A killed worker is replayable.
A card snapshot and digest manifest are flushed before its staged checkpoint.
Malformed complete records and empty selection fail without acknowledging capture.
This first version rescans the source and keeps generation snapshots, avoiding
reliance on the loader's separately replaced cards/cursor files after power loss.
The separate incremental storage primitive below is not wired into this queue.
Operator-approved retention and parser integration remain open.
Only the approved files are inventoried and copied; unrelated files in a shared
folder cannot consume the source budget or invalidate capture. Legacy configs
without a file list fail closed. The unchanged loader receives a local snapshot
of those files' complete JSONL prefixes. Later appends remain for the next wake;
replacement, truncation or a changed captured prefix leaves capture pending.
Staging guards reject excessive selected source entries, individual/aggregate
source bytes, card/report bytes and retained spool bytes/entries. Source/spool
overlap and selected symlinks/reparse points are rejected.
No previous generation is pruned. Each manifest records the applied limits.
The unchanged loader runs in a windowless child with a 60-second deadline and is
killed/reaped on timeout; each pass handles at most 128 pending source jobs.
Durably recorded attempt counts rotate failed jobs behind less-attempted work,
including across worker restarts. A failed capture does not prevent independent
jobs in that pass from staging. The pass still reports failure and preserves the
failed generation as pending; partial progress is not a healthy-upload claim.
Hooks do not parse transcripts and pending work still requires another wake.

The `scan` command provides a bounded local catch-up pass over existing enrolled
jobs. It checks only the explicit files' identity, size and modification/change
timestamps; it neither discovers additional histories nor reads transcript text.
Each changed observation and requested-generation increment commit together in
SQLite. Repeated unchanged observations do not queue extra wakes. The first
observation queues one conservative wake even if a hook already queued capture.
Durable scan attempt counts rotate jobs across bounded passes; a missing source
does not starve healthy sources. A scan during capture leaves its wake pending.
An existing approved scheduler can invoke `scan` followed by `stage`; this patch
does not install a scheduler or hook. Metadata is a wake hint, not an immutability
guarantee or semantic generation identifier. On Windows, `st_ctime` is creation
time: an in-place, same-size rewrite with its modification time restored can
preserve every observed field and escape this scan. Independent review reproduced
that case. The next append queues capture; a rewrite inside the captured prefix
then fails the existing prefix check. This delays detection and does not prove
protection for rewrites elsewhere. Source-generation trust and incremental parsing
remain separate owner-reviewed work. Scan failures leave the observation intact
for retry. This is local synthetic staging, not server persistence or graph proof.

### Bounded raw input prerequisite

`native_window.py` is a separate raw complete-record reader for the proposed
incremental interface. It is not connected to `capture_queue.py` or loader 1.16.
The caller supplies an explicitly enrolled file's device/inode identity, a
committed complete-record byte offset and a captured high-water target. The
reader preserves original offsets and CRLF bytes, returning at most the record
and byte budgets. It leaves incomplete and oversized records at their original
offset and returns an observable reason. Later appends are outside that target.
It interprets neither JSON nor native dialogue and writes no raw snapshots.

Each call reads at most `2 * read_bytes + 2` bytes, including an exact reread of
the observed window and its preceding boundary byte. Handle and named-file
identity, direct regular-file checks and captured length are rechecked before
return. Replacement, truncation or an observed rewrite fails without returning
records or advancing an offset. Raw bytes are excluded from record/window repr;
OS access errors expose a generic diagnostic instead of a native path.

These checks cover the observed window only. Older in-place rewrites, ancestor
containment, approved source-generation trust, post-verification mutation and hard
OS quotas remain separate concerns. The deadline is cooperative, not an OS read
interrupt. Defaults permit 1 MiB observed bytes, 256 KiB per record including its
newline, 1024 records and five seconds. A native record above the configured cap
requires review; it must not be silently skipped or split. The caller must commit
reviewed parser state and durable output atomically before adopting `next_offset`.
Returned raw records are not approved dialogue, cards or persistence receipts.

Fourteen targeted synthetic reader checks pass on Windows, including actual read
call instrumentation, rewrites during capture, partial tails and a fresh Python
process continuing at the original offset. An opt-in generated 778 MiB source
fully drains in 778 windows with matching whole-source SHA256 and at most
2,097,154 actual read bytes per call. A separate small source can be read between
those windows. This is reader boundedness, not queue scheduling fairness, parser
checkpoint/replay integrity or graph recovery. No native history is read by these
checks. Independent review and the existing-loader interface remain prerequisites
for integrating this component.

### Atomic output and parser checkpoint prerequisite

`incremental_store.py` is a separate private SQLite component. It commits a
bounded output batch, drop report, original source offset and parser checkpoint
in one transaction. A stable batch identity and comparison with the expected
snapshot permit an identical retry and refuse a conflicting stale writer.
Re-enrollment preserves progressed state; incompatible generation, parser,
metadata or database format requires explicit review instead of a silent reset.
Only committed batches can be retrieved, and their output digests are checked.

The directory receives the existing current-user/SYSTEM private Windows ACL
before SQLite or its rollback journal is created. Pending dialogue stays private;
status representations omit checkpoint and metadata bytes. OS/SQLite failures
return a generic error without retaining a sensitive exception chain. Direct
file checks reject symlinks/reparse points and unexpected WAL sidecars, while
same-user path replacement races remain outside this directory's trust boundary.

Defaults permit 256 KiB checkpoint JSON, 64 KiB metadata JSON, 8 MiB combined
output/drop bytes per batch, 4096 sources, 4096 retained batches and a 64 MiB
SQLite database page budget. These are local synthetic limits, not deployment
policy. JSON limits apply after serialization; they are not hard heap quotas.
The database page limit does not cap aggregate rollback-journal disk use.
SQLite FULL synchronization is not a measured hardware power-loss guarantee.
Retention is not implemented; reaching a limit preserves existing data and
requires review. Callers must validate redaction, native identity/generation and
complete-record offsets before committing; the store enforces monotonic offsets
but does not prove their native record boundaries.

Fourteen synthetic storage checks cover Windows ACLs, process exits at four
transaction boundaries, fresh-process recovery, identical concurrent writers,
stale writes, rollback, limits and corrupt/missing committed output. These prove
local atomicity for the supplied synthetic state, not native semantic recovery.
The component is not connected to `capture_queue.py`, the owner parser, a hook,
scheduler, server or graph. The existing full-rescan queue remains unchanged.

### Frozen outbox implementation status

`frozen_outbox.py` now builds a private, transport-free state machine on the
incremental store. Exact prepared item/manifest bytes and parser position commit
together. A durable attempt precedes transport; interrupted attempts require
caller-authenticated exact-key readback. Equal strict JSON values advance despite
object member order, whitespace or escaping differences; numeric types and array
order must match. Malformed or ambiguous readback stops as a durable conflict.
Frozen bytes remain unchanged, and absence permits
the identical write-once replay, and conflict or authorized redaction stops for
review. Stale attempt tokens are refused. Item receipts gate the manifest, and
the previous manifest receipt gates the next batch. Wire coordinates and ordinal
continuity remain frozen across restart. Local structural checks do not replace
the complete admission validator or either privacy screen.

JSON coordinates and manifest ordinals are true integers; only key segments
use decimal padding. Old frozen string-coordinate records fail closed without
migration or rewriting. An opt-in compatibility test validates synthetic batches,
numeric limits and both minimal refusal replacements against the exact reviewed
PR #346 validator (`ef344eee87ae4313d32fb48ffe7b140b3c18ff18`).
This covers schema compatibility and local privacy-screen preflight with synthetic
records; the database admission path is not exercised.

Synthetic tests exercise process exits around batch, attempt and observation
commits, lost replies, exact replay, stale observations, concurrent send claims,
ordering and corrupted output. They establish local transaction behavior only.
Definite first-attempt precommit screening refusals can now atomically replace
an item with a minimal omission and update its manifest. Private overlays retain
the original capture and checkpoint. Crash tests cover both sides of the commit;
earlier uncertain sends, retry refusals and historical unguarded attempts fail
closed. Both replacement envelopes still require full admission and screening,
including the retained metadata's fixed shapes. This operation does not implement redaction.
The caller still owns signed-read verification (including seat/author, exact
namespace/key/head/version and redaction provenance), durable evidence artifacts,
bounded retries and refusal classification. Evidence digests are references,
not authentication. Server redaction, parser/source identity integration,
full admission integration and native uploads remain
unimplemented. No hook, network transport or real-history ingestion is enabled.
See the tool README for the API and private store limits.

Defaults are local synthetic-staging safeguards, not approved deployment policy:
4096 source entries, 64 MiB aggregate input, 8 MiB per file/cards, 2 MiB report,
16384 spool entries and 256 MiB retained spool. An operator-owned `--limits` JSON
can override positive integer limits; it cannot choose unlimited values. Existing
over-budget spool data is preserved for review and the wake remains pending.
Snapshots bound parser input to selected complete prefixes; they do not provide
hard OS memory/disk quotas or protection against every filesystem race. Expanding
loader output can exceed its byte budget before rejection. Temporary snapshots
contain raw selected prefixes locally. Before copying, the worker durably records
the direct-child directory's device/inode identity and sets private permissions:
0700 on POSIX; a protected Windows DACL allowing only the current user and SYSTEM,
inherited by copied files. A permission failure prevents the raw copy. Normal exit
removes the recorded directory. A killed-worker fixture independently checks the
actual raw file's DACL and proves cleanup by the next worker holding the kernel
lock, without deleting source files, batches or unrecorded legacy directories.
Recovery validates containment, identity and a bounded non-reparse inventory;
replacement or a tampered ownership record never permits deletion. New scratch
rows record their capture job before copying. If cleanup fails for a verified
job, recovery durably quarantines that job and reports review required while
independent healthy jobs can stage. Review state survives later wakes and does
not clear automatically even if the path identity is repaired. Status exposes a
per-job `scratch_review_count`; no release/deletion command is implemented.
Legacy rows without a verified job still require global review. All retained
scratch, including quarantined bytes, remains subject to global spool size and
non-reparse traversal guards; quarantine cannot bypass resource containment.
A crash still leaves private raw scratch until another worker wake. Cleanup is
not secure erasure, an immediate crash-time guarantee or protection against an
administrator overriding permissions. Legacy unrecorded scratch needs separate
operator review. Production cleanup/retention policy remains unapproved.
The stdlib-only loader uses the base Python executable on Windows so timeout
kill/reap targets the parser rather than a virtualenv redirector. The deadline bounds each
loader child, not all filesystem operations or the entire pass. Do not enable
this full-rescan prototype on real histories until bounded incremental capture,
hard resource containment and the approved retention/recovery path are proven.
The process-crash test does not establish durability across a physical power cut.

Partial final JSONL records remain for a later wake/catch-up. Only complete native
records are captured. Stable loader card IDs make replay deterministic. Staged
does not mean uploaded, persisted, indexed, or recovered. No network path or
automatic deployment is present in this prototype.

## Proposed shared schema and permission boundary

Beyond-card records require a **new** namespace, proposed
`shared/transcript-context`, and server-enforced `context.v1` validation. Kxxx owns
the server/schema lane. This draft owns the queue/hooks lane; parser/redaction
changes require txxxx's review, with Nxxxxx retaining the Kimi dialect lane.
Memory-bxxxxx reviews each implementation before its gate.
Namespace claim/grants, retention and any production write require the operator's
go on a reviewed design. Existing card namespace permissions do not grant this.
For development use synthetic fixtures and a scratch server only.

The canonical wire contract is Kxxx's
`context.v1` draft, PR344 at 81ab39f01cee069290e7d7cb8f84cfe21d2b3ca1 (not public).
Section 4 owns the uploader obligations: freeze wire identities and final bytes
with the local atomic checkpoint before sending; preserve them on restart;
reconcile uncertain/existing keys through signed reads and byte comparison;
record redacted keys without re-upload; report conflicts without overwriting;
commit manifests in successor order and confirm only after full reconciliation.
Follow that canonical contract rather than maintaining a second copy here.
The static queue-fit review covers the contract, including the task-field and
redaction amendments; implementation, transaction tests, remaining parser fit and
native lifecycle evidence remain prerequisites.

Of the screen refusals, only `E_CONTEXT_SECRET` and `E_CONTEXT_CLIENT_NAME` can trigger
the precommit omitted-item transition described below. The replacement must also
pass validation and screening. `E_CONTEXT_SCREEN_UNAVAILABLE` means retry later without writing an
omission. On `E_CONTEXT_WRITE_ONCE` or `E_WRITE_CONFLICT`, reconcile the exact key
and bytes before deciding the outcome. Clean lone surrogates to U+FFFD before
screening, hashing and freezing;
the server's `E_CONTEXT_TEXT_ENCODING` refusal is not a screen hit.

The static queue review of validator PR346 at
ef344eee87ae4313d32fb48ffe7b140b3c18ff18 (not public)
requires the following uploader dispositions. These are implementation requirements;
the local capture queue and incremental store do not implement this remote uploader.
The frozen outbox above implements only its local durable transitions; the
remote verification and policy actions in this table remain integration work.

| Refusal or uncertain outcome | Required queue action |
| --- | --- |
| `E_CONTEXT_WRITE_ONCE`, `E_WRITE_CONFLICT` | Authenticated exact-key readback; compare strict JSON values with the frozen record and validate namespace, seat/author, key, version and redaction provenance. Object member order, whitespace and escaping may differ; numeric types and array order must match. A matching committed item can reconcile successfully; a mismatch stops the batch without overwrite. Frozen retry bytes stay unchanged. A redacted item stays redacted. |
| `E_CONTEXT_MANIFEST_ORDER`, `E_CONTEXT_MANIFEST` | Validate the frozen batch locally, then verify its predecessor and all item dependencies through signed reads. Retry only for a verified missing dependency once it commits. A malformed batch, wrong digest/count/version, or continued refusal after verified dependencies stops the batch for review. These codes each cover both dependency gaps and invalid data; do not classify by code alone or parse prose messages. |
| `E_CONTEXT_SECRET`, `E_CONTEXT_CLIENT_NAME` | For a definitely uncommitted item, atomically checkpoint a minimal omitted replacement at the same identity/ordinal and update the unsent manifest's digest/counts. Strip optional derived fields, including `native_ref`, and validate/screen the replacement. Invalid required metadata needs reviewed preparation/configuration repair; do not loop through omissions or disguise its identity. Reconcile uncertain attempts before changing frozen bytes. Never replace a committed or redacted item. |
| `E_CONTEXT_SCREEN_UNAVAILABLE` | Keep pending and retry later with bounded backoff; never convert a screen outage into an omission. |
| `E_CONTEXT_TIME` | Invalid syntax or source time stops preparation. For a locally valid future timestamp, reconcile server time and wait within a bounded policy; never rewrite frozen event time to make it pass. |
| `E_CONTEXT_KEY`, `E_CONTEXT_SCHEMA`, `E_CONTEXT_DIGEST`, `E_CONTEXT_TEXT_ENCODING`, `E_CONTEXT_SEAT_MISMATCH` | Stop/quarantine the affected batch for preparation or identity repair; no blind retry or screen-hit omission. |
| `E_CONTEXT_TOO_LARGE` | Compare the exact compact UTF-8 serialization with local preflight. A server refusal after a passing preflight is serialization drift requiring review. Apply whole-item omission only through the explicit precommit transition; never truncate or silently replace frozen bytes. An oversized manifest stops preparation and cannot become an omitted item. |
| `E_CONTEXT_REDACT_UNAVAILABLE` | Keep the redaction operation blocked; do not route around it with a normal write or prune. This is not an item omission. |
| Authentication/permission/plane refusal | Block for configuration or grant repair; never omit content to bypass it. |
| Transport failure, `E_INTERNAL`, or another ambiguous failure | Keep pending and reconcile through signed reads before resending or changing bytes. Missing or unverifiable readback is not a commit receipt. |

This validator screens free-text `text` and `step`. Optional `native_ref` and
artifact-only `agent_id` must each be 32 lowercase hexadecimal characters:
SHA-256 of the raw local record or subagent ID, truncated to 32 characters.
Compute these locally before preparation; readers map local IDs using the same
hash. Raw native IDs do not belong in a wire record. Required `adapter_version`
accepts one to four dot-separated ASCII numeric components, each one to four
digits, including on omissions. Preflight every field before freezing the batch.
Previously frozen records with raw IDs or legacy version strings fail admission
without rewriting their bytes. Reconcile uncertain writes before considering any
replacement; this patch does not migrate existing frozen records.

Omit the `tags` argument entirely on context writes, including manifest writes.
The handler rejects any non-null `tags` value, including an empty list, with
`E_CONTEXT_SCHEMA`. A screen-hit omission carries neither `omitted_bytes` nor
content-derived optional fields; `omitted_bytes` belongs only to `too_large`.

An envelope replay may return its original result, whereas a new envelope for an
existing key can return `E_CONTEXT_WRITE_ONCE`, even for identical bytes. Neither
response replaces exact-key reconciliation. Verify all items and the immediate
predecessor before submitting a manifest; advance the remote committed cursor
only after verifying that manifest. A local SQLite commit is not a remote receipt.
Required uploader tests include lost replies/restart, same-envelope replay versus
new-envelope write-once, conflicting/redacted keys, dependency gaps versus invalid
manifests, screen outages, atomic omission/manifest replacement, invalid required
metadata on omitted items, opaque ID acceptance, raw ID refusal, and absent versus
empty tags. No runtime
acceptance or rollout approval follows from this static review.

Kxxx accepted that queue contract in authenticated message
`msg/<key>` and proposed splitting the dependency refusals
on the next PR346 revision:

| Proposed code | Required queue action |
| --- | --- |
| `E_CONTEXT_MANIFEST_PREDECESSOR` | Verify the missing predecessor through signed reads; retry after its commit is verified. |
| `E_CONTEXT_MANIFEST_ITEM_MISSING` | Verify the missing item through signed reads; retry after its commit is verified. |
| `E_CONTEXT_MANIFEST_ORDER` | Stop for review: batch 1 does not start at zero, or the successor starts at the wrong ordinal. |
| `E_CONTEXT_MANIFEST` | Stop for review: digest, count or version fault. |

These are proposed semantics, not the behavior of the reviewed `ef344eee`.
At the repository check on Monday, 19 October 2026, PR346 had that head: its opaque
ID contract and tags refusal do not implement the dependency-code split.
Keep the state-based handling above for it; verify the replacement revision and
gap-versus-fault tests before relying on the split codes. Signed reconciliation
remains required with either revision.

Txxxx's detector fix is PR347 at
b5745d3f331bfaee2e753cd5e978f0f353bc5941 (not public),
open and unmerged at that check. Its 34 focused detector tests passed locally
against a hash-verified snapshot in 2.66 seconds. This is synthetic detector
evidence, not native transcript acceptance. Earlier authenticated owner report
`msg/<key>` reports a worst per-rule timing of 39 ms across
the 64,000-character sweep and zero detection differences across 147,821 real
items. Kxxx's independent scratch-merge report
`msg/<key>` reports a worst total of 172 ms across all
detectors and three strict-xfail timing tests becoming strict XPASS. These are
attributed measurements, not tests run by this queue review. Integration still
requires the fixed detector revision, removal of the timing xfails and their
allowed-skip entry, and review of the rebased validator's screens.

Kxxx's authenticated message `msg/<key>`, version 1,
reports a recorded decision operator ruling in Kxxx's session on 10 October: the normal
check applies here, refusing client and engagement names; only the existing card
namespace retains its 8 October exemption. This resolves the design's policy
question. Preserve the earlier custodian/reviewer reports as attributed history
(card-only exemption versus a reported 30 September unrestricted policy),
superseded for this namespace by that reported ruling. This draft did not observe
the operator a recorded decision and does not claim deployed enforcement.
Enforce this policy server-side on every context write; client filtering
is additional protection. SUPERSEDED 24 Oct 2026 for `shared/transcript-context`
(a recorded decision in kxxx's session: "Store everything, secrets included"):
neither screen applies there, items are stored as written, and anything read
from it may hold live secrets; the outbound guards keep them off PRs, GitHub and
the wiki. Elsewhere, secrets fail closed server-side on every write, including
manifests and recovery records.
Sanitized fixtures must test both name and secret rejection without exposing
real client identifiers or credentials.

The draft's canonical item and manifest shapes supersede this proposal's earlier
segment/recovery sketches:

- Each dialogue item is one write and is screened whole on admission. An item
  whose complete serialized value exceeds the 65,536-byte limit is omitted whole
  with its original UTF-8 byte count; no text prefix or text digest is retained.
- Task items freeze the complete structured payload. Both `step` and display
  `text` receive both screens; a hit omits the whole item, including event/counts
  and digests. Redaction removes every task field from every version.
- A batch manifest binds the generation, batch and contiguous source ordinals.
  It requires the committed immediate predecessor as specified in section 6.
- Recovery remains source-linked and attributed; a recorded decision cards retain
  their authority. Dialogue does not become a ruling through retrieval.

Txxxx's authenticated Claude and Codex measurements are owner reports using a
fixed 700-byte metadata allowance, not native acceptance tests. No sampled Claude
item exceeded the limit. Four of 1,618 Codex user items did (about 0.25%): dispatched
briefs and handovers. Their whole-item omission is a known continuity loss in v1.
A report from an Antigravity user `msg/<key>`
measured 253 sessions with the same serializer and allowance: none of 4,011
assistant items and 13 of 564 user items (2.30%) exceeded 65,536 bytes. Those 13
were subagent/benchmark prompts containing pasted source or system prompts;
their omission is another known continuity loss, not an approved omission policy.
Kimi, artifact and task sizes remain unmeasured. A larger budget or
prefix policy requires a separate reviewed decision; this draft authorizes neither.
One assistant text block is one item. `ordinal` orders items; timestamps and
`native_ref` record IDs may be shared and cannot replace that order.

Prefer native record IDs for identity. If absent, use a locally assigned opaque
source-file identity plus complete-record byte offset and role/chunk index; record
replacement/rewrite as a new source generation. Raw paths and raw-text digests
stay local. A source digest in shared data refers to approved redacted content.
IDs do not change when summaries change. Replacement/rewrite allocates a new wire
generation. Existing keys are write-once; a conflicting value is reported and
never overwritten. `memory.write` remains strictly version 1; redaction is a
separate server operation (section 8).

Readers rebuilding dialogue use committed batches whose source records pass
authenticated version/digest checks. An admitted item can be readable before its
manifest commits; the manifest establishes completeness, not screening.
Upload retry must distinguish an unknown write outcome from
a verified failure; settle an ambiguous write through the existing signed read
contract before deciding to retry. A completion manifest is committed only after
all required source records are verified. This is an application protocol, not a
claim that memory.write already supplies a multi-record transaction.

Permission model: each seat writes only its own server-stamped context. Kxxx's
authenticated `msg/<key>` reports a a recorded decision operator
ruling in his session: assistant turns stored for context are readable by ALL
seats. It supersedes this document's earlier five-seat reader table. This draft
did not observe that operator a recorded decision. The canonical schema uses
all-seat reads; exact namespace/grants and rollout still require operator go.
No public read, foreign-seat write or privilege inferred from a Chrome profile.

Use the canonical draft's complete serialized-value budget, not the earlier
reported 8 KiB assumption. Confirm applicable live rate limits with the custodian;
the earlier reported burst of 600 writes at 5 writes/second is not verified
production configuration. Bound uploads/backoff and reconcile ambiguous outcomes.

Retention and tombstones are operator decisions. Offer these choices for review:
keep approved context indefinitely; keep bounded-age excerpts while retaining
minimal provenance/task state; or retain context until its related work closes
plus an operator-chosen grace period. Each choice needs explicit source, manifest,
derived-state and local-spool limits. Existing cards remain keep_all and their
namespace refuses memory.prune; do not change that policy or inherit it here.
Unfinished work must not silently expire. No retention option is selected yet.

Kxxx's authenticated `msg/<key>` reports that production now
lists `memory.redact`, superseding the earlier report that its skill row was
absent. That is a custodian report, not a context-specific acceptance test.
Canonical section 8 defines NEW context redaction code. Only the item's own seat
or the operator may redact it. In one transaction it locks item versions, then
manifest versions, in ascending order; tombstones the item; writes a new
manifest version; and redacts the previous manifest version. The first redaction
also erases version 1's writer signature; later versions have NULL `signed_bytes`.
A fresh reader cannot reconstruct a writer-signature chain or compare entries
against the tombstoned predecessor. The revised draft explicitly proposes server
trust: authenticated `memory.read` results, linked redaction provenance, and
verification that the redactor is the item's own seat or the operator.
The existing client verifies the signed result payload and its fresh call ID,
then matches the nested memo's namespace, key and requested version or head.
`result_signing_kid` selects the server JWKS verification key; it is not itself a
signed payload field. This is source evidence, not a context-reader acceptance test.

Kxxx's authenticated `msg/<key>`, reaffirmed in
`msg/<key>`, reports a recorded decision operator choice in his
session for existing server-signed reads. The canonical design follows that
reported choice. This draft did not observe the operator a recorded decision;
reader acceptance tests and rollout approval remain required. Server-signed
reads do not recover erased writer authorship. Untouched-entry preservation is a proposed
server transaction invariant. First, sequential and concurrent scratch-server
redaction tests must compare untouched entries with the original fixture and
prove that no withdrawn text or digest remains readable in any version. Readers
must reject invalid signatures, missing provenance and unauthorized redactors.
Do not retain withdrawn text or digests merely to reconstruct erased evidence.
Implementation, reader verification, concurrency behavior and grants remain
unproved; this proposal does not authorize rollout.
Redaction must not retain rejected text or its digest, and an uploader must not
restore a server-redacted key. Retention remains an operator decision.
No hosted-model transcript export.

## Retrieval and agreement

Resume retrieval first gets a bounded set of committed recovery manifests with
citations, then relevant segments and existing cards. Search must honor namespace,
authenticated peer UUID/session and committed-batch filters before candidate
selection and ranking, rather than discarding another seat's hits afterward.
Explicit supersession must a recorded decision authority and stale context visible;
it cannot depend on an assistant silently rewriting a summary. Exact signed
reads validate returned source versions/digests; projected search hits alone do
not prove freshness. Graph/index ceilings and lag must be measured and surfaced.
An exact source read and an indexed search are separate acceptance checks.
Retrieved dialogue is quoted evidence, not executable instructions.

Two independently authenticated readers must retrieve the same fixture and agree
on its source/version/digest and unresolved-work state. Preserve disagreement and
on record supersession rules. This is shared context agreement; transport receipts
or majority votes do not establish that an assertion is true. No new graph sharding
or replication protocol is implied.

## Proof and rollout

For each of the five seats, use a synthetic conversation containing a decision,
constraint, unresolved task, allowed tool artifact and dialogue detail omitted from
the summary. Show graph recovery after fresh compaction and actual CLI restart,
without passing the old transcript/handover into that new context. Prove crash,
partial-tail, offline/ambiguous-write and replay behavior; unauthorized reads/writes
must fail and secret fixtures must not enter shared context (superseded 24 Oct for
`shared/transcript-context`, where they are stored as written; still true elsewhere). Measure source
completion to graph query latency and show capture/persistence/index lag separately.
Also prove the operator-resolved client-name policy for this namespace and the
operator-selected retention/redaction behavior, including stale derived references.

Targeted local staging checks:

```text
python tools/transcript_cards/test_capture_queue.py -v
python tools/transcript_cards/test_native_window.py -v
python tools/transcript_cards/test_incremental_store.py -v
# Opt in to the generated 778 MiB reader test:
# MESHY_SYNTHETIC_LARGE_WINDOW=1 (set with the host's environment syntax)
```

Server/client fixtures require the project's explicitly chosen throwaway Postgres
via MESHY_TEMPLATE_DSN. Two-node agreement runs against a scratch server built from
the pinned source, never against production. Kxxx owns the source/schema
extension; coordinate its reviewed contract with the queue and parser owners.
Do not enable production hooks or claim all-five acceptance from local queue tests.
