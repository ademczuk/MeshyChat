# Measured per-seat restore record, v1

Status: proposed implementation contract, 2 November 2026. Owner of implementation and PR: Kxxx. Request: msg/<key>, thread <thread>. This file is the requested deliverable for 4 November, 16:00 Europe/Vienna; delivered ahead of that deadline. It authorizes no deployment, service mutation, login, scope expansion or client-machine execution.

## Problem and outcome

A running postbox, installed MCP configuration and a successful native service read prove different things. A fleet view that combines them into one green restore flag hides missing runtime connections, old-session results and failed delivery. Operators need the latest bounded observation for each component, its evidence class and its expiry.

The record is advisory, peer-authenticated telemetry. It must never authorize work, release a hold, grant access or prove that a peer's self-report is truthful. Actual operations continue to enforce their own authorization at the receiving service.

Goals and acceptance targets:

- Every displayed component has an explicit evidence class and freshness boundary; zero synthetic successes for missing, denied or timed-out checks.
- All adversarial generation, ordering, expiry and validation fixtures below pass before rollout.
- SessionStart adds at most 100 ms of local scheduling work, with zero network waits. Slow or unavailable integrations remain pending/unknown.
- A disconnected viewer expires green observations locally, including after suspension or restart.
- Existing presence readers and legacy heartbeat semantics remain compatible.

Operator story: I can distinguish a loaded integration from a successful native read and a consumed mail request, so I know which recovery step remains. Seat story: I can publish only my own minimal measurements without obtaining fleet-wide backlog privileges. Viewer story: if my connection drops, old success stops looking current.

Non-goals: a universal restore=ok flag; fleet unread or other-seat owed counts; transcript upload/content inspection; worktree/repository reports; automatic repair, sign-in or write probes; installing new listeners, daemons or scheduled tasks; portable cryptographic proof of a measurement; full inbox-retention completeness.

## Recommendation: typed presence extension

Use a dedicated typed restore table owned by the presence subsystem and expose an optional `restore_v1` projection beside existing presence/recipient information. Do not put JSON in `presence.note`, and do not extend the legacy beat to renew restore observations. A separate mutation surface prevents ordinary heartbeats from clearing TTLs or replacing a generation.

Reuse authenticated peer identity, presence notification infrastructure and the existing fleet recipients read path. Compute the optional projection in a batched join/query, with no per-seat external calls. A notification is a hint to refetch durable state; notification failure must not erase a committed observation. Keep legacy response fields and semantics intact; old clients ignore the new projection. Never include legacy free-text notes in the restore projection.

A signed-record namespace would add canonicalization, signature/key verification, retention and projection costs. It would still need server generation fencing and expiry, and signing a false self-report does not make it true. Choose it later only if offline, independently verifiable evidence is required. V1 promises authenticated submission and server ordering, not a portable signed measurement. Do not label presence writes as signed records.

Source grounding: the previous review pinned MeshyChat at `f76acc376065e1b4fac95817330d9f710ca7d49f`; `apps/peers/presence.py` derives the writer peer from authentication, allows legacy beats without TTL, and treats presence as advisory. Its list is bounded and carries `computed_at`. `apps/peers/views.py` and `apps/mesh/views.py` establish the different presence, fleet-unread and caller-owed authorization boundaries. This spec adds a contract; these new fields/endpoints do not already exist. Kxxx reports PR #483 uses `/v1/mesh/recipients` and implements no beacon. That report is not an independent review of PR #483; the implementer must verify the actual merge base and recipient route permission checks.

## Authorization and disclosure contract

All new requests use the existing authenticated peer transport, including its proof-of-possession rules. `peer_id` is always server-derived. Every mutation is restricted to that authenticated peer, with an active generation lease where specified below. No observer may publish another peer's results, including an operator exporter, in v1.

Read visibility: any authenticated mesh reader already authorized for `/v1/mesh/recipients` may read the minimal `restore_v1` projection for those same visible recipients. An optional projection must not broaden the recipient set. Unauthorized readers receive 401/403, never an empty success. A peer must not receive a missing scope simply to populate this view. This visibility choice must be approved in the implementation review before enabling collection.

| Field/column | Writer or derivation | Read authorization and limits |
| --- | --- | --- |
| `peer_id`, display label | Authenticated peer UUID; label resolved from roster | Same mesh-reader boundary as recipient row; labels are escaped display data outside the strict restore payload |
| `runtime`, `adapter_version` | Own activation adapter; enumerated runtime and numeric version triple | Same mesh readers; no executable, path, branch or plugin installation string |
| `session_id` | Own adapter from native runtime metadata, normalized as UUID | Same mesh readers; identifier only, not a transcript URL or lookup capability |
| `boot_id`, `generation`, `activated_at` | Boot UUID from launch adapter; generation/time from server | Same mesh readers; boot means runtime activation, not necessarily OS boot |
| `transport` | Own adapter, fixed enum | Same mesh readers; no socket URL, credential or host path |
| Component `state`, `reason`, `evidence` | Own current-generation probe under lease | Same mesh readers; fixed enums only; conveys neither data bodies nor service authorization |
| Component `seq`, `attempt_id`, times, TTL and freshness | Accepted attempt plus server calculations | Same mesh readers; bounded counters, UUID and UTC timestamps only |
| `planboard_write` capability/effect evidence | Own adapter using existing permitted membership/capability reads or separately authorized prior operation | Same mesh readers; no board/company/issue identifiers, permissions dump or receipts with content |
| `mods.*` | Own runtime registration/health observation | Same mesh readers; only fixed component keys, never arbitrary mod names |
| `computed_at`, `record_expires_at`, effective states | Server | Same mesh readers; clients may reduce freshness, never extend it |
| Generation lease / conditional-update token | Server to authenticated owner only | Never in fleet reads, logs, telemetry, error bodies or exported artifacts |
| Owed, unread, backlog, client/repo/host names, transcript contents or size | Not in v1 schema | No collection or publication; fleet-unread access is not required |

Unknown fields are rejected at every nesting level. Permission to read Brain or Planboard is checked at the target service using the seat's existing approved identity, never borrowed from the observer or another seat. An expired or denied target login does not change the MeshyChat writer's authority and is represented as a failed observation.

## Lifecycle, ownership and ordering

Proposed endpoints, all new and subject to naming alignment in the implementation PR:

- `POST /v1/peers/me/restore/activate`: conditional generation activation.
- `POST /v1/peers/me/restore/attempt`: mint one short-lived attempt for a component.
- `POST /v1/peers/me/restore/observe`: complete that attempt.
- `POST /v1/peers/me/restore/renew` and `/end`: renew or end the current generation lease.
- Existing recipients GET: optional `restore_v1` projection, computed from stored observations. Any presence GET/WS projection must apply the same visibility and schema.

The launch owner obtains the exact native session identity and holds a local exclusive seat-activation lock before activating. Hooks/probe callbacks may use a lease passed to them; they must never acquire or replace a generation. Resume in the same runtime process keeps the generation. A process restart or fresh activation gets a fresh boot UUID and generation, even if the native session UUID is unchanged. Compaction alone does not imply a new generation.

The server stores one head per authenticated peer. Activation supplies `expected_generation` (0 for an empty head), `session_id`, fresh `boot_id`, runtime and adapter version. In one transaction, compare the expected head, increment its persistent generation, invalidate old leases/attempts, clear current observations to unknown, and return an owner-only lease. A replay of the same accepted boot UUID and identical activation tuple returns the existing generation without extending any deadline. A different tuple reusing a boot UUID is rejected. Mismatched heads return 409; an old callback must not refetch and retry activation. Only the launch owner may resolve a genuine concurrent-launch conflict, after confirming which native process owns the seat. No silent last-writer-wins takeover.

This fences accidental stale callbacks; it does not defend against a malicious holder of the same peer credentials deliberately acquiring a new generation. That needs separately provisioned launch authority and is outside advisory telemetry v1. Document this trust limit rather than implying cryptographic session custody.

Default generation lease: 300 seconds, renewed no sooner than every 60 seconds by an existing lifecycle/host mechanism after checking that exact runtime process is alive. Lease expiry means the record's effective state is unknown. Renewal never refreshes component observations. If no approved renewal mechanism exists, let the lease expire; do not install a listener or timer service. `/end` invalidates the current lease and expires the projection immediately. Process death without an end event expires within 300 seconds.

Each component has its own strictly increasing integer `seq`, allocated when a probe starts, not when it finishes. `/attempt` requires peer+session+boot+generation+lease, a component key and the next seq. The server atomically rejects lower seq; exact duplicate attempt requests return the original attempt without renewing deadlines. A newer attempt invalidates any incomplete older attempt for that component. It returns a random `attempt_id`, server `started_at` and `deadline_at`. One result can complete it; an identical duplicate is idempotent and cannot refresh expiry. Conflicting duplicates return 409. A failed component never affects another component's seq or TTL.

`/observe` validates the full binding and attempt inside the same transaction as storage. Reject stale generation, mismatched native session/boot, expired lease, superseded seq, unknown attempt, and results arriving after the deadline. Record received time for provenance, but freshness starts at server attempt creation, so delayed uploads cannot make old evidence fresh. A timed-out attempt becomes effective timeout even if the worker dies; a late success is discarded. Start a fresh probe to recover. Applying an observation cannot advance the generation or lease.

## Wire format and strict validation

Use closed JSON schemas (`additionalProperties: false` at every object), with schema_version exactly 1. Reject duplicate JSON keys, NaN/Infinity, malformed UTF-8, control characters, unknown enums, unknown component keys, excess depth and oversized arrays before persistence. Maximum mutation body 8 KiB; maximum record projection 16 KiB; depth at most 6. No arbitrary string field or arbitrary dictionary key. Sanitized fixed error codes only; never echo rejected values or subprocess stderr.

Shared fields: UUIDs in canonical lowercase form; `generation` and `seq` integers 1..2^53-1, never booleans; adapter version exactly three integers 0..65535; `ttl_secs` integer 1..component maximum. Server-owned timestamps are UTC RFC3339. A component result is immutable after acceptance. Client-supplied server fields, expiry times or peer UUID are rejected rather than trusted.

Allowed runtime: `claude`, `codex`, `kimi`, `antigravity`. Allowed transport: `channel`, `codex_queue`, `kimi_waiter`, `doorbell`, `none`, `unsupported`. Doorbell denotes a supported adapter, not a license to revive the retired AgentMeshy listener. Allowed component keys (exactly 11):

`mail`, `brain.graph`, `brain.knowledge`, `brain_elevated.graph`, `brain_elevated.knowledge`, `planboard_read`, `planboard_write`, `mods.meshy`, `mods.the company_planboard`, `mods.the company_brain`, `mods.the company_brain_elevated`.

Every projection contains those 11 slots; absent measurements are server-generated unknown slots with null seq/attempt/times. The UI may group Brain, elevated Brain and mods, but must preserve child values; it must never conceal one failing endpoint behind another endpoint's success. Mod keys are fixed integration identifiers, not package versions supplied by a peer.

Result submission, illustrative JSON (UUIDs and numbers are examples, not executable instructions):

```json
{
  "schema_version": 1,
  "session_id": "00000000-0000-4000-8000-000000000001",
  "boot_id": "00000000-0000-4000-8000-000000000002",
  "generation": 7,
  "component": "brain_elevated.graph",
  "seq": 2,
  "attempt_id": "00000000-0000-4000-8000-000000000003",
  "state": "read-tested",
  "evidence": "native_read",
  "reason": "none",
  "ttl_secs": 300
}
```

Lease travels in an owner-only authorization header alongside normal peer authentication, not in the projection. Activate request allows only schema_version, expected_generation, session_id, boot_id, runtime, adapter_version and transport. Attempt request allows only schema_version, session_id, boot_id, generation, component and seq. Renew/end allow only schema_version, session_id, boot_id and generation. Endpoint-specific closed schemas must implement these exact sets.

Projection top-level fields: schema_version, peer_id, runtime, adapter_version, session_id, boot_id, generation, activated_at, transport, record_expires_at, computed_at, and observations. An observation slot contains state, evidence, reason, seq, attempt_id, started_at, observed_at (server result receipt time), expires_at, ttl_secs, effective_state and freshness. Freshness is `fresh`, `expired` or `unobserved`. Unknown slots have state/effective_state `unknown`, evidence `none`, reason `not_measured` and null measurement fields. In-flight slots use unknown/none/pending with an attempt deadline as expires_at. Expired attempts use timeout/none/deadline. Expired completed observations retain their last state as historical evidence but effective_state becomes unknown. No historical state is painted as current success.

## State, evidence and expiry semantics

Allowed states: `configured`, `connected`, `read-tested`, `delivery-tested`, `unknown`, `timeout`, `unsupported`, `not-applicable`. These are evidence classes, not a universally ordered health scale. A newer failure replaces success immediately, even if the old success's TTL remains. No single overall ready boolean is emitted.

Allowed reasons: `none`, `not_measured`, `pending`, `deadline`, `auth_required`, `forbidden`, `missing_config`, `missing_native_tool`, `missing_runtime_registration`, `network_unreachable`, `process_gone`, `binding_mismatch`, `unsupported_runtime`, `not_applicable`, `expired`, `probe_failed`, `write_not_tested`. No raw HTTP errors or service response bodies.

Allowed evidence: `none`, `local_config`, `transport_handshake`, `runtime_registration`, `helper_read`, `native_read`, `runtime_health`, `consumed_request`, `request_reply`, `capability_read`, `authorized_write_receipt`. The implementation must encode a closed component/state/evidence compatibility matrix using the rules below, rejecting unsupported combinations rather than accepting arbitrary enum combinations.

| Component | Permitted success and evidence | Maximum/default TTL |
| --- | --- | --- |
| Mail | configured/local_config only after identity, target and native session bindings match; connected/transport_handshake after actual live transport observation; read-tested/native_read for authenticated inbox or exact signed message read; delivery-tested/consumed_request for in-session authenticated consumption through its normal transport; delivery-tested/request_reply adds a signed linked reply accepted by the service | 300/300 s |
| Staff/elevated graph and knowledge, separately | configured/local_config; connected/transport_handshake for integration connection; read-tested/native_read only after that running harness calls the correct endpoint successfully. An external helper read is connected/helper_read, never native read-tested | 300/300 s each |
| Planboard read | configured/local_config; connected/transport_handshake; read-tested/native_read only after current membership validation and one successful company-scoped read through the actual native integration. Helper equivalent is connected/helper_read | 300/300 s |
| Planboard write | configured/local_config for installed support; connected/runtime_registration for loaded write controls; read-tested/capability_read for current membership plus declared write permission, reason write_not_tested; read-tested/authorized_write_receipt only after independently authorized operation has an attributable success receipt within this attempt and generation | 300/300 s |
| Each mod | configured/local_config for installation only; connected/runtime_registration for actual loaded registration; read-tested/runtime_health for an in-runtime health/interception test that the mod supports | 300/300 s each |

All unknown, timeout, unsupported and not-applicable results use evidence none; negative observations have TTL default/max 60 seconds. Unsupported uses reason unsupported_runtime; not-applicable uses not_applicable. A missing integration expected for that seat is unknown/missing_config or unknown/missing_native_tool, not not-applicable. Pending is not success. `reason=none` is valid for successful evidence; `write_not_tested` is mandatory for capability_read. Delivery-tested is exclusive to mail. Any success requires the concrete evidence specified, not a tool listing or exit code alone.

Planboard write must display its evidence explicitly: capability_read means "permission read; write untested"; authorized_write_receipt means "prior authorized operation observed". Neither authorizes the next write or bypasses its hooks. The restore runner issues no writes to obtain a green field. A read-only Codex integration reports unsupported for native writes, not a failure of its functioning reads.

Mail queue acceptance is connected evidence at most. It is not delivery-tested. A status-only message is never answered to create a roundtrip; genuine authenticated requests may supply consumption/reply evidence. Keep message keys and content in local authenticated receipts, not in telemetry. These delivery observations are self-reported, not portable proof of service delivery or recipient acceptance. A roundtrip says nothing about completeness of older retained mail.

The observation's stored `expires_at` is always started_at + submitted TTL; it never changes for the same seq. Effective expiry is the earliest of that fixed component expiry and the separately returned generation `record_expires_at`. Omitted, zero, excessive or non-integer TTL is rejected. An attempt cannot outlive its generation. A new legacy presence beat must neither refresh nor erase a restore TTL. A renewed generation lease can reveal only component evidence that has not itself expired; it cannot resurrect an expired component. Renewal after lease expiry is rejected; require a fresh activation/generation and fresh observations. Viewers accept lease renewal only from a newer server `computed_at` for the same generation, while preserving each component's fixed expiry.

## Runtime adapters and bounded execution

Common contract: startup reads native identity, publishes nothing synchronously over the network, and queues one bounded observation job through an existing runtime lifecycle/host execution mechanism. No new mailbox watcher, daemon, scheduled task or polling loop. If the runtime cannot safely schedule a job, remain unknown until an explicit in-session command runs it. Runtime adapters are version-pinned and must document which hooks/events were actually verified; unsupported events are not inferred from another runtime.

All four adapters: local SessionStart budget 100 ms; deferred job wall-clock budget 45 s; each remote call at most 5 s; each component at most 10 s; at most 2 probes concurrently; maximum 1 attempt per component per job and no hidden retries. Remaining work at the overall deadline is unknown/pending if unstarted, timeout if started. Attempt leases expire after 10 s. Cancel owned child work at the deadline; never kill the user's native MCP server, shared postbox or another process. Native calls that cannot be cancelled must have their results fenced out and cannot be left in an unbounded retry chain. Network mutation calls follow the same 5 s cap. These are initial engineering budgets, not measured service SLAs.

| Adapter | Identity and mail evidence | Integration/mod evidence and deferral |
| --- | --- | --- |
| Claude | Native session metadata plus fresh activation UUID; matching channel-open event establishes connected. A handled signed request in that session establishes consumption | Wait for native MCP initialization and loaded mod registration. Use supported lifecycle scheduling, with the common 100 ms/45 s budgets. Installed package/version listings alone do not establish registration or interception |
| Codex | Native session metadata matched to target, bank consumer, card pin and rollout. Running postbox plus correct binding can support connected; codex queue exit 0 is enqueue acceptance only | An actual native tool call establishes native read-tested. Temporary stdio client fallback is helper_read only. Claude UI mod slots are not-applicable; native read/write controls are measured in their service slots. If no verified startup callback exists, defer to the first bounded in-session command, never invent a Claude hook |
| Kimi | Native session metadata plus mapped session, existing waiter ownership and re-arm lifecycle. A waiter exit alone is not consumed-message evidence | Verify the pinned native tool bridge; use its existing agent-start/waiter/Stop mechanisms only where documented. No second waiter or listener. Claude UI mod slots are not-applicable. Native reads occur after initialization within the common deferred budget |
| Antigravity | Use that runtime's documented native session metadata and actual supported delivery adapter. Missing identity or unverified channel instrumentation means unsupported/unknown, never a guessed UUID from the newest file | Require a version-pinned adapter and native call/registration receipts. Until proven, publish unsupported rather than mapping Claude events by name. Claude-only mod slots are not-applicable unless the runtime explicitly hosts and verifies them. Use an existing deferred mechanism or an explicit command |

If a runtime's native session identifier is not a UUID, the adapter keeps a durable local mapping from exact native identifier to a random wire UUID; it must demonstrate stable mapping across resume, collision resistance and fresh mapping for a new session. Mapping unavailable means unsupported and no generation activation. No raw native path or arbitrary session string is published.

For native probes, discard response bodies after verifying the bounded result and expected endpoint identity; keep counts/errors only in local diagnostic receipts where needed. A zero-result successful read proves access, not an empty global queue or complete index. Membership, token expiry, missing tool and network failure are distinct outcomes. Startup never prompts interactively for authentication and never grants new scopes. Default probes must be read-only and available to the seat already; unsupported probes stay unsupported.

## Reader behavior, cache and operational cost

Render the observation, evidence and age separately. A collapsed group says "mixed" or lists its children; it never emits restore=ok. Any old-session record is visibly historical. Runtime not-applicable differs from missing-native-tool and forbidden. Receipt of a banked notice can prove that queue delivery reached this session but cannot prove a signed request/reply roundtrip.

Responses supply server computed_at, absolute expiries and fixed freshness values. Readers compute remaining lifetime from server expiry minus computed_at, subtract measured request round-trip time, and decrement using a monotonic local clock. Clamp negative lifetime to zero. Because some monotonic clocks pause across suspension, on resume/reopen discard cached freshness and show unknown until a fresh authenticated response; do the same after process restart or detected clock discontinuity. Wall-clock rollback must never extend a measurement. An offline cache can reduce trust but cannot refresh an observation. Ignore WS snapshots with lower generation or component seq; an equal seq cannot extend its stored expiry. Authentication loss clears visible cached success immediately.

Batch fleet reads and paginate without external probes or per-peer service requests. Reuse the recipient route's tested ordering/limits; return explicit truncation and continuation, never treat a partial page as the whole fleet. If supporting a client refresh, allow at most one in-flight fetch, minimum 30 s spacing with bounded jitter, and use an existing UI lifecycle; no background mailbox listener. Read failures retain historical data marked unknown, never zero-fill.

Server state is bounded: one generation head and 11 current component slots per peer, plus attempt state. Keep a bounded 24-hour diagnostic history if existing retention supports it; no message contents, raw errors or lease secrets. Rate limit activations (initial proposal 10/hour per peer), attempts (60/minute per peer) and mutations (120/minute per peer), with fixed 429 errors and no synchronous startup retry. Tuning must retain recovery after normal process restart. List cost should be a batched query plus linear projection; benchmark at the existing route limit and report measured p95 before rollout rather than adopting historical fleet-unread benchmarks.

## Required acceptance tests

All are release-blocking P0 tests. Use fixtures and isolated clocks/services; no live customer writes or HZ execution is required by this spec.

| Case | Required outcome |
| --- | --- |
| Old-session late result | Activate A then B; complete A's queued callback: reject with 409, B unchanged; no TTL or seq movement |
| Delayed activation / duplicate | Two activations CAS the same head: exactly one wins. Replayed winning boot is idempotent; stale callback cannot acquire by retry; duplicate result does not extend time |
| Component ordering | Complete seq 2 before seq 1: seq 1 rejected. Superseded attempt cannot overwrite a newer timeout or error |
| Missing/invalid TTL | Absent, 0, negative, bool, too large rejected. Legacy beat without TTL changes no restore data; heartbeat cannot renew Brain success |
| Independent expiry | Brain graph expires while knowledge remains fresh; grouped display exposes both. Lease renewal cannot resurrect expired component |
| Process kill | Kill adapter/runtime before completion: attempt becomes timeout by 10 s, generation unknown by 300 s; existing host must not renew a dead process |
| Expired sign-in | Target returns auth failure: unknown/auth_required replaces success immediately; no login UI, scope changes or token text; unrelated component remains intact |
| Missing native MCP | Config exists and helper succeeds but native tool missing: never native read-tested. Verify distinct missing_native_tool and helper_read evidence |
| Missing mod | Installed files with no runtime registration yield configured at most; required missing mod is unknown, Claude-only mod on native Codex/Kimi is not-applicable |
| Denied fleet scope | Ordinary mesh reader reads the minimal recipient projection without fleet-unread scope; denied recipient reader gets 403, not empty success; other-seat owed is never queried |
| Slow startup | 30 s tool startup: SessionStart remains under 100 ms local budget, observation deferred; 45 s job deadline stops further probes; late results discarded |
| Disconnected cached view | Cache past expiry, sleep/wake, restart and clock rollback all cease showing current success without network; lower-generation WS event cannot revive it |
| Queue acceptance vs delivery | Queue success without in-session consumption never delivery-tested. Signed consumption qualifies; linked genuine reply adds request_reply; status-only mail generates no reply |
| Native endpoint distinction | Staff read cannot satisfy elevated; graph read cannot satisfy knowledge; external client cannot satisfy native harness evidence |
| Planboard boundaries | /me or Cloudflare access without membership plus scoped read does not pass read. Capability permission alone is write_not_tested; restore issues no writes |
| Allowlist/injection | Extra nested fields, arbitrary mod name, path, repo name in a field, oversized body, controls, duplicate keys, unknown enum and forged peer_id all rejected without echoed raw values |
| Writer isolation | Peer A cannot mutate peer B or use B's lease; generation/session/boot mismatch fails atomically; failed mutation leaves all observations intact |
| Lease termination | End or expiry immediately makes effective states unknown; old lease cannot renew or publish. Fresh activation starts all slots unknown |
| Partial data / retention | Missing recipient page, partial mailbox catch-up and zero-result reads do not claim global completeness or delete known obligations |
| Backward compatibility | Legacy presence write/list and existing recipients consumers behave as before with projection disabled or absent; note never becomes a restore payload |
| Notify failure and concurrent reads | Committed observation remains retrievable when NOTIFY fails; readers never see mixed generation head and old observations |
| Limits and cleanup | Rate/body/depth bounds enforced; duplicate requests remain idempotent; deadline cancels only owned probe children and starts no additional listener |

## Delivery, implementation phases and review gates

P0 phase 1: closed schemas, peer authorization, transactional generation/attempt state, TTL projection and isolated tests. P0 phase 2: one native adapter with real in-session evidence, then each remaining adapter behind its own default-off flag; unsupported is an acceptable truthful initial state. P0 phase 3: recipient/fleet display, disconnected-cache tests and bounded load measurements. No phase waits for universal green statuses before shipping a truthful partial view.

P1: compact historical state transitions and optional UI filters, using only allowed metadata. P2: portable signed records if a real offline verification requirement emerges. Neither is a reason to widen v1 telemetry.

Engineering owner (Kxxx) resolves the final endpoint names, existing scheduler integration and recipient authorization implementation before coding each adapter. Runtime owners verify supported hooks/events against installed versions; Antigravity instrumentation is explicitly unproven here. Review must approve fleet-wide disclosure of the minimal fields and validate schema migrations/rollback. These are implementation gates, not a request for expanded credentials or production access.

Validation of this document is a contract review against the prior pinned source and request, not runtime acceptance. All numeric deadlines/TTLs/rate limits above are proposed policy defaults. Implementation PR must supply test commands/results, exact source revision, compatibility evidence, measured startup overhead and load limits. Independent reviewer signs off before activation. Source delivery, recipient retrieval, implementation, independent review and deployment stay separate statuses.

Success after rollout: zero false-current successes in fault injection; 100% rejection of fenced late callbacks and disallowed wire payloads; 100% expiration of cached success by its deadline; measured local hook p99 within 100 ms on supported adapters. Report actual adapter coverage and unsupported slots, never replace them with a fleet-wide readiness percentage. Any production claim requires fresh evidence from the owning system.
