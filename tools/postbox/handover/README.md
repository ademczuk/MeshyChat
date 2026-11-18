# Codex handover source prototype

This package detects compaction pressure and defines a durable transition from
one Codex conversation to a fresh one. It addresses sessions whose post-compact
input leaves too little working space and repeatedly compacts during one task.
It does not change the compact limit.

**All installed platform adapters currently return HOLD.** This PR provides
shared policy, checkpoint validation, a journal, pure platform route plans and
fixture tests. It does not yet provide an unattended live handover. No existing
postbox, launcher, skill directory, service, client checkout or configuration is
changed merely by adding or importing this package.

The agreed design is [contract-r2.md](contract-r2.md), byte-for-byte SHA256
`2af7befc2166f2218204708c80e512a6e3a637fbb7d590ae4e503f952450c458`.
Its historical heading says r1; r2 changes the review/gate ownership line.
Design GO authorizes source work, not installation or live qualification.

Milestone 1 of 2 is this shared core; no seat hands over yet. Kxxx agreed the
split on Tuesday, 3 November 2026. Milestone 2 starts with a qualified HQD pilot,
then each other seat on its own host. Merging this core does not complete the
operator task. All five installed adapters remain HOLD.

The opt-in HQD `watch-once` source candidate adds installed-route binding,
private observation generations and a durable gated-notice journal. Its
[phase B proposal](phase-b-install-proposal.md) describes the policy and recovery
contract. Scheduling is a proposal only; accounting remains UNQUALIFIED and
phase C stays HOLD. With dispatch disabled it writes private observation only;
an owner-enabled dispatch sends at most one notice through the existing gated
sender. Importing the package installs and launches nothing.

## Read-only status and local observation

From a checkout with the repository's existing Python environment:

```powershell
python -m tools.postbox.handover status --platform hqd
python -m tools.postbox.handover observe --rollout C:/temporary/rollout.jsonl --state C:/temporary/metrics.json --thread FIXTURE_THREAD --epoch FIXTURE_EPOCH
python -m tools.postbox.handover observe-bounded --rollout C:/temporary/rollout.jsonl --state C:/temporary/metrics.json --fault-state C:/temporary/faults.json --thread FIXTURE_THREAD --epoch FIXTURE_EPOCH
python -m tools.postbox.handover validate-checkpoint C:/temporary/checkpoint-bundle.json
```

Use `hqb`, `hz-admin`, `hz-builder` or `pxx` for the other status inventories.
`status` has no effects. `observe` writes only its explicit local metrics file
and lock; it must not point at a live configuration or identity file. Its public
output excludes transcript text and partial records. The private metrics file
contains a bounded base64 partial record and must remain local with restricted
access. CLI observation deliberately cannot qualify a numeric compact limit.
`validate-checkpoint` checks structure and digests only, not authority or live
quiescence. None of these commands launches or retires Codex or sends mesh mail.

The [seat skill](skill/SKILL.md) and a future opt-in postbox caller use the same
`observe.observe` interface. An existing scheduler must supply bounded scans;
there is no new background polling loop or default postbox hook in this PR.
Observation returns a durable notice outbox keyed by thread/generation/kind.
The future controller must durably deduplicate notice consumption separately;
repeated outbox visibility is not another launch authorization.

## Policy and recovery boundaries

`metrics.py` persists cursor/file identity, bounded partial records, event
deduplication, thread/model/config epoch, floors and warning history. A scan is
at most 262144 bytes. Records exceeding 262144 bytes use a streaming JSON
validator with bounded UTF-8 carry, nesting (128), primitive length (128) and
header prefix (4096 bytes). Payload strings are validated without retention.
Only timestamp/type/optional ordinal before a final object payload qualify:
root compacted/context_compacted records count after the complete valid newline;
known irrelevant response_item/world_state/token_usage_record payloads are
validated and skipped. An event_msg also qualifies for skipping when its first
payload key is type and that value is outside the detector's relevant type set.
Relevant oversized events remain UNKNOWN; duplicate payload type keys, including
escaped spellings, are rejected. Direct payload keys are bounded to 128 raw
characters in this path. Unsupported layouts or malformed tails latch UNKNOWN.
Large records continue across scans and observer restarts without a total size
ceiling or premature compaction count. Replacement/truncation resets only
telemetry, preserving warning history and all mail state. A telemetry gap
latches UNKNOWN for this epoch: no guessed floor or replay of old requests.
There is intentionally no automatic repair/reset command. Owner-reviewed
requalification must preserve old state and notice history.
Decoder recursion/allocation errors also latch UNKNOWN and persist the consumed
record cursor without clearing warning or notice history. Invalid saved JSON,
including decoder resource errors or malformed saved fields, instead returns
HOLD and preserves the file for recovery. The bounded caller routes ordinary
observer exceptions to its independent durable fault outbox. Process termination
exceptions are not suppressed, and fault outbox commit failures propagate.

`observe_bounded` catches up through at most 64 scans or two seconds checked
between scans. Missing idle rollout files remain quiet and existing outbox
notices remain visible. A missing rollout with an existing metrics state records
a missing-telemetry fault without overwriting that state. It persists one fault
notice per thread/generation/epoch/degraded-set, observer-error, or
missing-telemetry, in a separate
explicit fault outbox. This also reports an occupied metrics lock without
stealing it. Fault outbox commit failures propagate to the caller. Outbox
visibility is not delivery: the future installed scheduler must durably consume,
deduplicate and route these notices to Kxxx. That route remains unqualified.

Ratio checks require an installed-version-qualified limit, accounting scope and
matching epoch supplied by a trusted caller. A window size is not a limit.
Body accounting requires measured accounted-body samples. Default reserve is
max(16000, 20% of limit), warning after two floors within another 5% of the limit,
request after two floors reaching the reserve. Rate (three compactions in ten
minutes within one known task) and size (150000000 decimal bytes) are independent
fallbacks. Unknown task gives a review warning. Size requests maintenance.
Combined triggers keep the strongest action (REQUEST above WARN), with the first
reason retained on ties in floor, rate, size order. Invalid-budget and cooldown
HOLD checks still take precedence.
These are provisional trial thresholds, not calibrated fleet settings.
An unmeasured compaction breaks the consecutive-floor sequence; independent
compaction-rate evidence remains available.

`protocol.py` saves an immutable checkpoint bundle with fifteen completeness
sections and digests, bounded bootstrap text, message keys AND versions, and
canonical open-work ledger references. Paths stand in for identity/config state;
never copy their secret contents. Counts/hashes validate retained indexes, not
the semantic truth of prose or freshness of authority. Trusted adapters must
verify those against the actual local/signed sources before accepting readiness.

Journal phases are DETECTED, CHECKPOINTED, OLD_QUIESCED, SUCCESSOR_STANDBY,
ROUTE_COMMITTED, DELIVERY_PROVEN, SUCCESSOR_ACTIVE, OLD_RETIRED. Each step holds an
exclusive controller lock, compares generation and saves ownership intent before
the effect. The five-minute ready deadline includes checkpoint verification.
There is one launch attempt. A successful activation records a 30-minute cooldown.
An uncertain effect, changed clock epoch, invalid proof or unsupported dependency
latches HOLD and retains the inflight intent. There is no automatic replay,
rollback, stale-lock stealing or force switch. A controller lock does not fence
model execution. The adapter must enforce old/standby fences across controller
crashes and recheck all activation prerequisites before releasing ordinary work.

The adapter is trusted OS/protocol code, not an agent-supplied bag of booleans.
Journal receipt checks bind the transaction, generation, seat, home, old/new
threads, checkpoint and nonce, then call the adapter's observed verifier. A
delivery proof needs a distinct controlled sender, authenticated linked reply,
real new-rollout consumption, routing checks, no replay and a valid deadline.
An enqueue or send receipt alone cannot qualify it. Outgoing requires_ack work
must enter the durable ACK ledger immediately, with its explicit deadline.

`cutover.py` produces non-effecting plans from adapter-extracted normalized
snapshots. All actual consumers must be inventoried. It preserves complete
native/installed queues, offsets, statuses and unknown fields, and invalidates
the plan if arrivals/recalls or any snapshot fact changes. It does not serialize
installed config formats or prove a service loaded the intended route.

## Platform work and qualification still required

The proposed milestone split is subject to Kxxx's agreement: accept this PR as
the shared prototype only, with a separate implementation milestone for the
five opt-in platform adapters and their disposable qualification. The frozen
contract still requires those platform sources; this proposal does not fulfill
or change that contract. The full handover directive remains open until its
required implementation, review and gate are complete.

| Platform | Source inventory/plan | Missing installed implementation and evidence |
|---|---|---|
| HQD | Target, bank consumer, default pointer and discovered consumers | Exact Task Scheduler ownership; pause/drain; config extraction and journaled CAS writes; running loaded-route receipt; Windows crash durability; enforced fences and owned fresh launcher |
| HQB | Includes raw-transcript consumer and native SQLite/WAL queue | Singleton launcher must settle and stop old execution before launch; queue extraction; service/route verification; same fence and durability evidence |
| HZ Admin | launchd route, bank and same-home native queue | Actual installed domain/label; persistent unattended launch and standby; crash durability; installed resume dependency; queue/route/probe verification |
| HZ Builder | System KeepAlive inventory; no invented bank consumer | Actual system-domain control permissions; installed offset semantics; reviewed signed bounded resume-brief/owed fallback; unattended launch/fences/queue/probe evidence |
| Pxx | Includes stale hardcoded default-resume UUID as a consumer | Replace owner-operated Terminal/AppleScript restart with qualified headless path; discover launchd service; persistent queue/intake/fence proof; no shared-daemon retirement |

Each implementation must also verify children/unknown operations, same home and
policy, persistent idle delivery, an active-work lease CAS, ordinary-work fence
release and retirement of the owned old thread only. Capability inventory is
not qualification. Windows fsync/replace alone is not a power-loss durability
claim. PTY and app-server APIs are candidates until disposable installed-version
tests prove their behavior. Installation and pilot remain separate owner acts:
Kxxx changes HQD config; HZ needs Admin GO plus Bxxxxx's yes.

## Validation and review

Run the standalone fixtures on an approved capable workstation. HQD's temporary
fixture authorization under directive 916ff2 has expired; this text does not
renew it. Both unittest and pytest must exercise the same imported module:

```powershell
python -m unittest tools.postbox.handover.test_handover -q
python -m pytest tools/postbox/handover/test_handover.py -q
```

Fixtures use temporary directories and fake identities, clocks, adapters and
queues. They cover bounded parsing, pressure policy, unresolved/duplicate data,
clock/model/config changes, durable notices/checkpoints, crash-after-effect at
all seven boundaries, single-launch/generation/lock/cooldown rules, attempted
concurrent execution, bad/missing/forged/replayed proofs, mail/recall preservation
and pure plans for each platform. These are protocol tests, not real subprocess,
service, native queue, filesystem power-loss or end-to-end delivery qualification.
Broader repository checks and the gate belong on HQB with memory-bxxxxx.
Non-author review belongs to pxx; codex-pxx-cli provides review legwork.
