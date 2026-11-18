# F23: hub skill-call frames, `call:` subscriptions and invocation webhooks

> RECORD, landed 2026-09-29 by operator ruling. This is the design as of 2026-09-24 (base 0b6767f). It has since been built as divergence 041 (the project's design log (not part of this release)); read that for what the code does today. The status and "today" statements below are as of 2026-09-24.
>
> Role names: where the text below says `meshy_app` and `meshy` for database roles, read the serving role `meshychat_app` and the owner login `meshychat_owner` (`sql_meshychat/046`). The event source value `meshy` is data.

**Status: DESIGN ONLY.** No code, no migration, nothing pushed. Branch `fin/design-skill-frames`,
first cut from `c5457ac82a3b3113a6b3aa6f7f57478be07bf07c` on 2026-09-23, rebased onto `origin/main`
at `0b6767f7de1f6101f7270085f873d4cd1c3a0b71` on 2026-09-24. Every citation into this repository
was re-opened at 0b6767f with `git show origin/main:<path>`. Where main moved a line, the number
here is main's.

**Revised twice.** (1) On 2026-09-23 the designer revised it after a first review and left the
revision uncommitted and unfinished: it pointed at a section 10, a risk R12 and a question Q14
that did not exist. (2) On 2026-09-24 an adversarial critique against main at 0b6767f kept every
part of (1) that re-measured true and finished it. It changed the design wherever main had moved
underneath it. Divergence 030 now closes revoked sockets. Divergence 036 (`fin/ops-replay2`) owns
the replay reader. `sql_meshychat/012` bound the `skill_calls` write policies. Divergence 032's
link job shares the janitor's loop. Section 10 is the critique log: each finding, its severity,
and what changed.

**What was asked.** The operator chose the skill-frame design. Today only `memory.*` calls
execute (in-process, divergence 017). Every WebSocket `skill_*` frame and every `call:` channel
subscription is refused with a typed error (`apps/wsgw/frame_handlers.py:9-23`), and there is no
webhook sender (the project's design log (not part of this release)). This
document states the frame contract, records what this repository already has, and designs
the implementation in landable slices.

## 0. How to read this document

**Evidence.** The repository and dev-database observations below are read at `0b6767f`.

**Instruments.**

- The `meshychat-codebase-memory` graph was NOT used for any conclusion. `index_status` returned
  `freshness: "stale"`, `root_match: false`, and the warning "graph repository root does not
  match the configured checkout; refuse graph conclusions". Everything below was read from source.
- Database figures are read-only counts, constraint definitions and column names from the dev
  database `meshy_django` on `127.0.0.1:5434`, taken 2026-09-23 16:27Z. No row content was read.
  That database has `sql_meshychat` 001 to 004 only, nine migrations behind main (3.2).
- The 2026-09-24 critique re-read the same catalog read-only (migration versions, policies,
  triggers, RLS flags, counts), read `fin/ops-replay2` at `d447af0` by `git diff`, never its
  worktree, and read `C:/_SampleProjects/agentmeshy-mesh-migration/cutover-list.md` at `30b048e`.
  It did not use the graph either.

**Legend.** **[M]** measured: the file and line were opened, or the query was run. **[I]**
inferred, and each inference names the rival hypothesis its evidence does not exclude.

---

## 1. Summary

1. MeshyChat client and server skill frames use the documented wire shapes,
   except `skill_resume`, which stays refused because no call can pause (2.10).
2. Keep ONE pipeline. A WebSocket `skill_call` runs the existing `apps/calls/pipeline.start_call`
   with `transport=ws`. There is no second gate order.
3. Step 9 gets a third destination. The memory authority still runs in-process. Any other target
   becomes `routed` when it holds a live socket in any worker, and `queued` otherwise, instead of
   today's immediate `E_TARGET_UNREACHABLE`.
4. Delivery follows the divergence-020 model. The database is the truth. A `pg_notify` sent by a
   trigger at COMMIT is only a pointer. Each ASGI worker runs a `CallBridge` that reads the row
   back and writes only to its OWN sockets. No worker ever writes to another worker's socket.
5. A small `hub_connections` table gives cross-worker presence, so the pipeline can tell `routed`
   from `queued` even when the HTTP request and the target's socket live in different workers. A
   worker writes a row only once its CallBridge is listening. A CallBridge also claims a `queued`
   call for a target it holds, so a presence miss delays a call instead of stranding it.
6. Every target frame is ONE guarded SQL statement: `call_id`, `target_peer_id = me`, the expected
   status and `peers.status = 'active'` sit in one `WHERE`. A peer can only answer calls routed to
   it. A suspended peer cannot answer at all, even in the window before divergence 030's sweep
   closes its socket: a suspension sends no notification, so that window is up to 10 s (R12). A
   socket whose bound key was revoked is refused the same way until its 4412 close.
7. `skill.result` and `skill.chunk` envelopes are verified, AND their signed `call_id` (plus
   `sequence` and `chunk_type` for a chunk) must equal the frame's.
8. Replay: per the operator ruling, envelope replay enforcement is ON by default for `skill.call`
   on every transport, read through ONE settings reader. `fin/ops-replay2` builds that reader
   (`nonce_ledger.enforce_from_settings()`, divergence 036, not yet on main), and makes
   `CallsContext.envelope_nonce_enforce` a required keyword. The WS `skill_call` passes it like
   every other transport, and this design builds no second reader (5.3). Results and chunks are
   made replay-inert by the state guards, `UNIQUE (call_id, sequence)` and the call_id binding,
   not by the ledger.
9. `call:<id>` subscriptions are caller-only and registered BEFORE the row is read. Persisted
   chunks are replayed, and an already-terminal call returns its terminal frame at once.
10. Timeouts stay with the deadline janitor. Its status change fires the same trigger, so a
    caller's socket learns of a `timeout` that a different process wrote. Once `queued` exists the
    janitor also runs every 5 s in its own loop, because the shared `run_jobs` loop also runs
    divergence 032's link job and can hold one tick for over a minute (4.10).
11. Webhooks: the target's own invocation webhook only, a wake-up doorbell for an offline
    target. It is registered in a NEW table, not the `peers.invocation_delivery_webhook`
    column. It goes through
    a new `apps/core/egress.py`: public addresses only, resolved once, connected to the validated
    IP, no proxy, no redirect, port 443. It is signed with an EdDSA JWT (`iss`, `sub`, `aud`,
    `iat`, `exp`, `jti`, `body_sha256`) under a DEDICATED webhook key. That key is generated on
    each host and published in the server JWKS under its own kid, and it is never the key that
    mints peer tokens. The body is a pointer and carries no args. A URL is rung only after it has
    answered a challenge (proof of control). A target has at most one doorbell outstanding.
12. The webhook is sent by ONE new process, `run_call_worker`, which also runs the 5 s deadline
    sweep. Each doorbell is claimed through `SKIP LOCKED` with the claim written before the POST
    (rule B7), never in the request path.
13. WS target frames and WS cancel give one uniform refusal for an unknown call and a call that is
    not yours. That does NOT close the call-existence oracle. Step 2's idempotency conflict and the
    HTTP read routes' 404 versus 403 answer the same question on every transport (5.4). Whether
    to close those is an operator question (Q7). The recommendation is to stop at the WS door.
14. Tests are RED-first on the real `meshychat.asgi.application` with two connected fake peers,
    real EdDSA JWTs, DPoP ON, the production bridges, and a two-worker simulation.
15. The build is nine slices (S1-S9). The replay reader that was S0 is divergence 036
    (`fin/ops-replay2`), and the WS `skill_call` slice (S6) waits for it. Each slice lands alone
    with its own divergence entry, and the WS frames work end to end before any webhook code
    exists.
16. Every protection here assumes this server is the only writer of the call tables. Production
    operation requires that one-writer rule.

---

## 2. Frame inventory (f6bb721)

### 2.1 Client frames (client to server)

The tag is `type`, in snake_case. The signed envelope inside a frame
uses dotted `kind` values (`skill.call`), a different spelling in the same JSON object, which this
repository already pins (`apps/wsgw/envelope.py:3-17`).

| frame | fields | who may send | MeshyChat action | error codes on the socket |
|---|---|---|---|---|
| `skill_call` | `envelope`, `mode_override?` | any authenticated peer; it becomes the caller | start the call with WebSocket transport | `SKILL_CALL_BAD`, `SKILL_CALL_FAILED`, `E_APPROVAL_PENDING`, `SKILL_CALL_BUSY` |
| `skill_ack` | `call_id` | target | record acknowledgment | `SKILL_ACK_FAILED` |
| `skill_running` | `call_id` | target | record running state | `SKILL_RUNNING_FAILED` |
| `skill_chunk` | `call_id`, `sequence: i32`, `chunk_type`, `envelope` | target | record a chunk | `SKILL_CHUNK_BAD`, `_BAD_FILE_REF`, `_FORBIDDEN`, `_NOT_RUNNING`, `_FAILED`, `E_SIG_*` |
| `skill_result` | `call_id`, `envelope` | target | finish the call | `SKILL_RESULT_FAILED`, `E_SIG_*` |
| `skill_error` | `call_id`, `error_code`, `error_message` | target | fail the call | `SKILL_ERROR_FAILED` |
| `skill_cancel` | `call_id`, `reason?` | caller | cancel the call | `SKILL_CANCEL_FAILED` |
| `skill_resume` | `call_id`, `context_id`, `action`, `supplied_input?`, `continuation_token` | caller | resume the call | `SKILL_RESUME_FAILED` |
| `subscribe` / `unsubscribe` | `channels` | anyone | register a `call:` channel | none; the ack echoes what was sent |

`skill_call` specifics [M]:

- `target_peer_id`, `skill_name`, `skill_version` and `call_id` are read from the SIGNED payload,
  not from frame fields. The pipeline's rebind step is a no-op on WebSocket.
- `mode_override` accepts `sync`, `async`, `stream` and `lesson`.
- One `calls_per_cycle` quota unit is reserved before the pipeline and refunded on replay or error.
- The dispatch is spawned under a per-connection semaphore of 16 because the route step can block
  up to 15 s on a webhook and an inline await would stall that socket's `skill_cancel`.

### 2.2 Server frames (server to client), and who emits each

| frame | fields | emitted by [M] |
|---|---|---|
| `skill_invoke` | `call_id`, `caller_peer_id`, `target_peer_id`, `skill_name`, `skill_version`, `continuation_nonce?`, `continuation_token?`, `mode`, `envelope`, `deadline_at` | online routing, reconnect, and lag recovery; `envelope.payload` is the caller's complete signed payload |
| `skill_acked` | `call_id` | the caller receives "your call was accepted"; this is not the target's ack |
| `skill_chunk` | `call_id`, `sequence`, `chunk_type`, `envelope` | live delivery on `meshy:chunks:<id>` and replay |
| `skill_result` | `call_id`, `status`, `envelope`, `payload_reconstructed?` | `terminal_frame` for completed calls and signed failures |
| `skill_error` | `call_id`, `error_code`, `error_message` | `terminal_frame` for unsigned failures and `abandoned` |
| `skill_timeout` | `call_id` | `terminal_frame` |
| `skill_elicit` | `call_id`, `context_id`, `kind`, `requested_schema`, `continuation_token`, `deadline_at` | only from `report_elicit` (see 2.10) |
| `skill_running` | `call_id` | **nothing**: this design has no constructor |
| `error` | `code`, `message` | every handler on failure |

### 2.3 The call state machine

The ten statuses are the live `skill_calls_status_check` [M, `pg_constraint` on `meshy_django`]:
`pending, routed, queued, running, input_required, auth_required, completed, failed, timeout,
abandoned`.

| edge | trigger | guard |
|---|---|---|
| none to `pending` | step 8 insert | call_id advisory lock, idempotency, concurrent-calls admission |
| `pending` to `routed` | step 9, target online | plain `update_status`, written BEFORE the publish so the woken target sees `routed` |
| `pending` to `routed` or `queued` | step 9, target offline: webhook 2xx gives `routed`, else `queued` | plain `update_status` |
| `queued`/`pending` to `routed` | redelivery (reconnect, lag) | `update_status_guarded([Queued, Pending])` |
| `routed` stays | `skill_ack` | `require_target`, `require_status([Routed])`; event only |
| `routed` to `running` | `skill_running` | guarded; sets `started_at` |
| `running` to `completed`/`failed` | `skill_result`; the terminal is the signed `status` | `finish_call_atomic ... WHERE status='running'` |
| `running` to `failed` | `skill_error`, unsigned | `fail_call_atomic` |
| `pending`/`routed`/`queued`/`running` to `abandoned` | `skill_cancel` or `POST /v1/calls/{id}/cancel` | caller only; guarded |
| six in-flight statuses to `timeout` | janitor, `deadline_at < NOW()` | `FOR UPDATE SKIP LOCKED` |

A guarded transition that matched no row is re-read. If the row already holds the DESIRED status
the answer is an idempotent success, otherwise `InvalidTransition`.
So a duplicate `skill_result` after `completed` succeeds and writes nothing.

The target-side contract is `skill_ack`, then `skill_running`, then `skill_result`; skipping
`skill_running` is refused. Targets send acknowledgment and running frames back to back.

### 2.4 Routing a call to the target's socket or webhook

- **Presence** is the Redis key `meshy:presence:<target>`,
  set on connect and refreshed by ping, TTL 120 s.
- **Online:** `routed` is written, then `skill_invoke` is published on `meshy:notify:<target>`.
  Every instance's `pubsub_bridge` re-checks `peers.status = 'active'` and forwards to that peer's
  local sockets.
- **Offline, `peers.invocation_delivery_webhook` set:** apply the SSRF guard, then make a
  synchronous POST. A 2xx gives `routed` and a `webhook_fired` event; anything else,
  an SSRF refusal included, gives `queued`.
- **Offline, no webhook:** `queued`.
- **Reconnect:** `redeliver_pending_for_peer` runs on every accepted socket over
  `pending, routed, queued, running` with `deadline_at > NOW()`.
- **Lost notifications:** a broadcast overflow (`Lagged`) triggers a coalesced redelivery sweep
  over every locally connected peer.

### 2.5 `call:` subscriptions and the caller bridge

- The call registry maps `call_id` to a list of `(connection_id, send queue)`; `register` is
  idempotent per `(call, connection)`.
- A `skill_call` registers its own connection, then pushes `skill_acked`.
  A `subscribe` with `call:<id>` registers only when the peer is the call's caller
  . An unknown call and a not-yours call are both silent on the wire because saying anything to the
  caller leaks call existence.
- After registering, `subscribe` replays every persisted chunk from sequence 0.
- `caller_bridge` pattern-subscribes `meshy:chunks:*` and `meshy:result:*`.
  A chunk body is forwarded verbatim. A result body is only a nudge: the row is re-read, mapped by
  `terminal_frame` and delivered, and the call is unregistered. On overflow it re-checks every
  registered call.
- Cleanup: `unregister_call` after the terminal frame and `unregister_connection` on socket
  teardown.

### 2.6 Timeouts

- `deadline_at = now + skills.max_runtime_ms`, default 30 000 ms.
- The janitor flips every in-flight status past its deadline to `timeout` and publishes
  `meshy:result:<id>`, so a WS caller receives `skill_timeout`.
- [M] **The target is never told.** Nothing publishes to `meshy:notify:<target>` on a timeout; the
  target learns when its next frame is refused with `InvalidTransition`.
- Production ticks every 5 s.

### 2.7 Cancellation

- Only the caller may cancel, from `pending`, `routed`, `queued` or `running`. [M] Not from
  `input_required` or `auth_required`: both are absent from the
  check and from the guarded allow-list.
- Outcome: `abandoned`, a `caller_disconnected` event, and a publish on `meshy:result:<id>`.
- [M] **The target is never told**, as with a timeout.

### 2.8 Chunk streaming

- `chunk_type` is `text`, `json` or `file_ref`. A `file_ref` must name a `ready` file artifact
  owned by the target's owner.
- The envelope is verified as kind `skill.chunk`, with `payload.target_peer_id` equal to the socket
  peer.
- `report_chunk` requires socket peer == target and status `running`, `sequence >= 0`, inline
  content at most 512 KiB.
- Rows go to `skill_call_chunks`, which has `UNIQUE (call_id, sequence)` (`sql/008:13`, present on
  the live table [M]). Content is `envelope.payload.to_string()`.
- Then the frame is published on `meshy:chunks:<id>`. A failed publish is ignored because the chunk
  is persisted and replayable.
- Retention keeps the last 200 chunks once terminal.

### 2.9 Back-pressure and bounds

| bound | value | source |
|---|---|---|
| per-socket send queue | 256 frames, drop-OLDEST | `apps/wsgw/constants.py:30` |
| in-flight `skill_call` per socket | 16; over the cap, `SKILL_CALL_BUSY` without blocking the reader | not yet assigned |
| sockets per peer | 8 | `apps/wsgw/constants.py:15` |
| inbound message | 16 MiB | `apps/wsgw/constants.py:59` |
| pubsub broadcast buffer | 8192; overflow triggers a sweep | not yet assigned |
| concurrent calls per caller owner | tier cap, admitted under an advisory lock | not yet assigned |
| chunk inline content | 512 KiB | not yet assigned |

### 2.10 Interactive pause and resume: nothing produces it

`report_elicit` is the only producer of `input_required`,
`auth_required` and `skill_elicit`. [M] `grep -rln report_elicit` over the f6bb721 tree finds it
only in the call runtime; no frame handler and no route calls it. No Python file under
`tools/` at b578b5c3a mentions `skill_elicit` or `skill_resume`. In production nothing can pause a
call, so `skill_resume` has nothing to resume.

### 2.11 Webhooks

- **Invocation delivery webhook**, `peers.invocation_delivery_webhook`.
  TASK-40 `check_egress_url` runs before the POST, and the TASK-41 `SsrfResolver` validates at
  connect time. The client never follows a redirect and times out at 15 s.
- [M] **It is not signed.** The POST carries `Content-Type` and `X-Meshy-Call-Id` and nothing else.
  The migration comment describes an `Authorization: Bearer <hmac>` that
  the CALLER computes with the plaintext secret and the server forwards; the server stores only an
  Argon2 hash of that secret (`migrations/009:11-12`). No field of the invoke body carries such an
  HMAC (`webhook_url` and `webhook_secret_hash` are the only fields),
  and the route sends none. The comment and the code disagree; the code is what runs.
- [M] **Nothing sets it.** The field has readers but no production writer. The only writer is a
  test, `UPDATE peers SET invocation_delivery_webhook`.
- [M] **The per-call `skill_calls.webhook_url` is stored and never read.** The invoke body accepts
  it and step 8 persists it; delivery reads only the peer column.
- **Push-notification callbacks** (TASK-27, `push_notification_configs`) are a second sender, to
  caller-registered URLs, each signed with a server EdDSA JWT with `aud` = URL, `jti`, a 300 s
  TTL, and `kid` in the header; they are SSRF-guarded and capped at 32 per call.

### 2.12 Design defects to avoid

Each is [M] from the observed behavior; section 4 says what the design does instead.

1. **An existence oracle on target frames and on cancel.** An unknown call and a known call with a
   different target return distinct messages. The handler sends that message to the socket, and
   `cancel_call` does the same for a non-caller. A `call_id` is a caller-chosen idempotency key,
   so any peer can probe another peer's call by name. Subscriptions must use the uniform refusal.
2. **Result and chunk envelopes are not bound to the frame.** `verify_payload` binds the signer to
   the socket peer; nothing compares `payload.call_id` with the frame's `call_id`, or
   `payload.sequence` with its `sequence`. A
   target can file its signed result for call A as the result of call B, and the row then stores a
   signature over a different call: rule A3's failure in miniature.
3. **Redelivery must advance the row before publishing.** A fast target's
   `skill_running` can meet `queued` and be refused.
4. **A subscriber to an already-terminal call must get the terminal frame.** `register_call`
   replays chunks, and the terminal frame is delivered directly.
5. **Registration must precede the row read.** A terminal published between the row read and
   registration is otherwise lost to that socket.
6. **`unsubscribe` must remove a `call:` registration** so the socket stops receiving the channel
   it left.
7. **`mode_override: "lesson"` must be rejected** because the live
   `skill_calls_mode_check` admits only `sync`, `async`, `stream` [M, `pg_constraint`]; the insert
   raises a check violation.
8. **An unrecognised signed `status` must be refused.** Treating it as `completed` makes the row
   contradict the signature it stores, which rule E2 forbids. The resulting terminal refusal is
   accepted as the stated cost (Q6).
9. **The memory daemon's comment overstates the ledger.** It says its results "pass through
   enforce_nonce too"; `enforce_nonce` runs only for `skill.call`.
10. **The invocation webhook is unsigned** (2.11): a receiver cannot tell the server from anyone who
    knows the URL.
11. **Chunk replay must include sequence 0.** `list_since` must include the supplied sequence
    bound, and subscribe passes 0. A chunk stored at sequence 0 is therefore replayed.
    `GET /v1/calls/{id}/chunks` must use the same bound.
12. **[I] The egress client must turn proxies off.** `build_egress_client` turns redirects off and
    disables proxy environment variables. A proxied request would resolve only the proxy name
    through `SsrfResolver`, and the proxy could resolve the target past the pin.
13. **The existence oracle has more than one door.** The idempotency check answers a call_id that
    another caller owns with `IdempotencyConflict`.
    It does so at step 2, before the signature is checked. Every `/v1/calls/{id}` read answers 404
    for an unknown call and 403 for someone else's. Both behaviors exist in MeshyChat:
    `apps/calls/repository.py:126-132` at `pipeline.py:187`, and `apps/calls/views.py:248-258`.
    Closing the target-frame door alone closes nothing (5.4, Q7).

---

## 3. What this repository has at 0b6767f, and exactly what is missing

### 3.1 Present [M]

| piece | where | state |
|---|---|---|
| frame tags, parse, `UNKNOWN` fallback | `apps/wsgw/envelope.py:30-86` | all eight skill tags parsed; `SKILL_CLIENT_FRAMES` is defined there |
| server frame builders | `apps/wsgw/envelope.py:101-206` | `welcome`, `pong`, `subscribed`, `unsubscribed`, `error`, presence, `mesh_message` (with divergence 033's `role`, `cc` and preview). **No skill builder** |
| refusal of every skill frame and every `call:` subscribe | `apps/wsgw/frame_handlers.py:39-65` | raises `SkillFrameNotImplemented`; the consumer sends `UNIMPLEMENTED` and closes 1011 |
| consumer gate chain (Origin, JWT, active, DPoP + jti ledger, bound key not revoked, 0RTT) | `apps/wsgw/consumer.py:168-349` | implemented; DPoP enforced by default; a token bound to a revoked key is refused at the upgrade with 4412 |
| named gaps: in-flight redelivery, caller bridge | `apps/wsgw/consumer.py:39-40` | stated, not built. The revocation listener is no longer a gap (next row) |
| revocation reaches an open socket (divergence 030) | `apps/wsgw/revocation.py:12-24` | a suspension closes with 4410, a lost read on shared/mesh-chat with 4411, and a revoked bound key with 4412. A suspension is written by SQL and sends no NOTIFY, so the sweep finds it every `REVOCATION_SWEEP_SECS` = 10 s |
| per-process registry, per-peer cap, `publish_to_peer` | `apps/wsgw/connection_manager.py:85-264` | implemented; each handle records the key its token is bound to; `publish_to_peer(by_key=)` sends a per-key frame (divergence 033); no call registry |
| send queue, drop-oldest, loss record | `apps/wsgw/send_queue.py:89-122` | implemented, loss is logged only (`consumer.py:368-379`) |
| in-flight constant | `apps/wsgw/constants.py:34-38` | `MAX_INFLIGHT_SKILL_CALLS = 16`, unused |
| LISTEN/NOTIFY bridge (mesh, beat presence, revocation) | `apps/wsgw/mesh_notify.py:591-909` | one thread and two sync connections per process; payload is a pointer; started by the first socket; sweeps BEFORE it reports listening |
| presence | `apps/wsgw/presence.py:59-105` | single-process only |
| the pipeline, gate order, `transport` constants | `apps/calls/pipeline.py:160-293` | `TRANSPORT_WS` exists and nothing uses it |
| nonce gate 8 | `apps/calls/pipeline.py:221-226`, `apps/calls/nonce_ledger.py:42-78` | default OFF on main: `pipeline.py:88`, `views.py:81-82`, `a2a/views.py:520-521`. Divergence 036 (`fin/ops-replay2`) turns it ON with one reader |
| step 9 | `apps/calls/route.py:110-174` | memory authority in-process; anything else `E_TARGET_UNREACHABLE` |
| guarded transitions with atomic events | `apps/calls/route.py:232-295`, `reads.py:290-327` | the shape every frame handler will reuse |
| deadline janitor | `apps/calls/janitor.py:44-83`; job at `apps/jobs/registry.py:103-115` | bounded, typed `E_DEADLINE_EXCEEDED`, every 30 s. The README runbook schedules `run_jobs --loop --interval 30` (README.md, "5. Schedule the jobs"); the line in the project's design log (not part of this release) that nothing schedules it is stale. The loop runs every job in one sequence (`apps/jobs/runner.py:55-95`), divergence 032's link job included (4.10) |
| `skill_calls` write policies | `sql_meshychat/012_write_policies_bound_and_frame_retention.sql:83-88` | UPDATE and DELETE only under `app.server_internal` (divergence 029). Every statement in this design that writes `skill_calls` runs there |
| envelope verify for all three kinds | `apps/calls/envelope.py:43-54` | `skill.chunk` and `skill.result` bind `target_peer_id`; a revoked signing key is refused at check 6 |
| server result signer (server kid, JWKS) | `apps/calls/result_signing.py:48-88` | signs with the token-minting key. Webhook JWTs use a dedicated key (4.11, F3) |
| chunk reads and the A2A SSE stream | `apps/calls/reads.py:237-248`, `apps/a2a/views.py:238-304` | read the chunk TABLE, so WS-produced chunks appear there with no change |
| cancel over HTTP | `apps/calls/views.py:583-634`, `reads.py:290-327` | caller only, guarded, event in the same transaction. **The caller check belongs to the VIEW** (`views.py:603-607`). `cancel_to_abandoned` itself runs `UPDATE ... WHERE call_id AND status` under `server_internal` with no caller predicate (`reads.py:315-320`); its docstring says the route has already authorized the actor |
| a 202 body that advertises WS and webhook | `apps/calls/views.py:329-332` | `return_via: ["ws","webhook","poll"]`; neither exists today |
| MUST-8 content chokepoint | `apps/modelhost/locality.py:48-76`, `apps/meshsem/providers.py:97-131` | admits ONLY loopback/RFC1918. It is the INVERSE of an SSRF guard and must not be reused for webhooks |
| the fleet's result verifier | `tools/meshy_node/wire.py:230-275` | checks a result only against the server JWKS and binds `status`, `call_id`, `result`, `error_code`, and `error_message` to the row. A target-signed result needs the target's published key (4.7) |

### 3.2 Database facts [M, `meshy_django`, 2026-09-23, re-read 2026-09-24]

**Which schema these describe.** `meshy_django` has `sql_meshychat` 001 to 004 applied
(`meshychat_migrations` versions 1 to 4, re-read 2026-09-24), nine behind main's 001 to 013.
`meshy_template` is the same. Every count below is the DEV database, never production. A policy
or constraint that main changed is taken from main's migration file instead, and the row says so.
No figure here says anything about production. The counts production needs are listed in 5.5.

| fact | value |
|---|---|
| `skill_calls` by status and transport | `pending`: 1782 http, 150 ws, 1 dashboard_test; `completed` 469; `abandoned` 130; `queued` 56; `timeout` 56; `failed` 24; `running` 14; `routed` 12; `input_required` 12; `auth_required` 12 (all http) |
| in-flight rows with `deadline_at > NOW()` | **0** of 2,039, so a redelivery sweep on this database would deliver nothing |
| `skill_call_chunks` | 159 rows; `UNIQUE (call_id, sequence)` index present; largest `content` 11 bytes; 11 rows have NULL content |
| `peers` with `invocation_delivery_webhook` / its secret hash / `hmac_secret` | 0 / 0 / 0, of 18,873 peers |
| `skill_calls` with `webhook_url` | 0 |
| `push_notification_configs` | 0 rows, 0 active |
| `envelope_nonce_seen` | 847 rows |
| RLS | only `skill_calls` has `relrowsecurity` and `relforcerowsecurity`; `skill_call_chunks`, `skill_call_events`, `peers`, `push_notification_configs`, `envelope_nonce_seen` have neither |
| `skill_calls` policies | on `meshy_django` (at 004): UPDATE and DELETE `USING (true)`. **On main that is gone**: `sql_meshychat/012:83-88` binds UPDATE and DELETE to `app.server_internal` only. SELECT is the caller or the target (`app.current_peer_id`) or `server_internal`; INSERT is the caller or `server_internal` (001) |
| `skill_call_chunks` constraints (added by the review) | FK to `skill_calls(call_id)`, `UNIQUE (call_id, sequence)`, `chunk_type` CHECK. Nothing bounds `sequence` or `content` size. `trim_chunks_for_call()` exists |
| `skills.max_runtime_ms` (added by the review) | at most 60 000; 6,562 of 6,604 are NULL, which defaults to 30 000 (`apps/calls/repository.py:42`) |
| `skill_calls_mode_check` | `sync`, `async`, `stream` (no `lesson`) |
| `skill_call_events.event_type` | no CHECK constraint (only `source` is checked: `caller`, `target`, `meshy`) |
| triggers on `skill_calls`, `skill_call_chunks`, `skill_call_events` | none |
| longest `call_id` | 41 characters (the validator caps at 200 bytes, `apps/calls/repository.py:45`) |
| `pg_notification_queue_usage()` | 1.9e-06 |
| `max_connections` | 500 |
| a presence table (`hub_connections` or similar) | none |

### 3.3 Missing, and nothing else

1. Server frame builders for `skill_invoke`, `skill_acked`, `skill_chunk`, `skill_result`,
   `skill_error`, `skill_timeout`.
2. Frame handlers for `skill_call`, `skill_ack`, `skill_running`, `skill_chunk`, `skill_result`,
   `skill_error`, `skill_cancel`, plus the `call:` half of `subscribe`/`unsubscribe`.
3. A call registry and its cleanup on teardown.
4. Step 9 for a remote target (`routed` or `queued`), and a presence source that works across
   workers.
5. A delivery path from a committed row to a socket in another worker (invoke, terminal, chunk),
   with recovery after a lost notification.
6. Redelivery on connect and on LISTEN (re)start.
7. The WS transport's use of the single nonce-enforcement reader. The reader itself, default ON
   (operator ruling), is divergence 036 on `fin/ops-replay2`, not this design.
8. An SSRF egress module, the doorbell sender in `run_call_worker`, its signer, its registration
   route and table, and the proof of control a URL gives before it is rung.
9. A caller predicate inside the cancel statement itself (3.1). A WS handler that called
   `cancel_to_abandoned` as the HTTP view does, without the view's own check, would let any peer
   cancel any call.
10. A webhook signing key separate from the token-minting key, and a JWKS that publishes it. Today
    `apps/authapi/views.py:633-648` publishes only the current token key.
11. A deadline sweep that runs every 5 s, in a loop the link job does not share (4.10).
12. The active and live-key clauses in every target statement, for the window before 030 closes
    a revoked socket (4.7, R12).

---

## 4. The design

### 4.1 Principles, each from a rule this repository already holds

- **The database is the truth; a notification is a pointer** (`apps/wsgw/mesh_notify.py:21-25`).
  A call frame is always rebuilt from the committed row, never from a NOTIFY payload, because
  NOTIFY is unauthenticated (any session on the database can send one).
- **One pipeline** (`apps/calls/views.py:3-6`, rule E3). HTTP invoke, A2A `message:send` and WS
  `skill_call` all call `pipeline.start_call`.
- **Every edge is one guarded statement with its event in the same transaction**
  (`apps/calls/route.py:27-35`). The losing side re-reads.
- **Fail closed and typed** (rules A4, E1). No handler acks a frame it did not act on; that
  property is what `frame_handlers.py:14-18` protects today, and the implementation must keep it.
- **Background work never shares a request connection** (rule E7).

### 4.2 Which process owns what

```
 ASGI worker k (daphne), one per process                         run_jobs --loop       run_call_worker (new)
 ---------------------------------------------------            ----------------      ---------------------
 consumer tasks (one per socket)                                 every job, 032's      deadline sweep every 5 s
   frame handlers -> guarded SQL on the socket's own connection  link job included;    (writes status, the
   skill_call -> pipeline.start_call in a thread                  the janitor stays     trigger notifies);
 ConnectionManager (sockets), CallRegistry (call subscriptions)  registered here and   from S9: LISTEN
 LiveBridge (mesh_msg, beat, revocation)   -- unchanged          the lease decides     meshy_call_webhook, claim
 CallBridge (meshy_call, meshy_call_chunk) -- NEW, own thread     who runs a tick       (SKIP LOCKED), POST via
   and own LISTEN connection                                                           apps/core/egress, record,
 hub_connections rows for its own sockets, UPSERT every 10 s                          webhook_fired on 2xx
```

- **Who decides the route:** whichever process runs `start_call`. That is the HTTP request thread,
  or a consumer task's worker thread for a WS `skill_call`. The decision is DB writes only.
- **Who delivers to a socket:** only the worker holding that socket, through its own `CallBridge`,
  exactly as `LiveBridge` does for mesh nudges (`mesh_notify.py:18-20`).
- **Who times out:** the deadline sweep, run every 5 s by `run_call_worker` (4.10). The shared
  loop keeps it registered, and the job lease decides which process runs a given tick.
- **Who sends webhooks:** the same `manage.py run_call_worker`, from S9 (Q4).

`CallBridge` is a second instance of the `LiveBridge` pattern (`mesh_notify.py:591-909`: its own
daemon thread, a LISTEN connection and a query connection, handing work to the loop with
`run_coroutine_threadsafe`). It is NOT added to `LiveBridge`. [I] A chunk stream can produce
notifications far faster than the mesh does, and one listener thread would then delay mesh nudges
behind chunk reads. Rival: at today's volume (159 chunks in the whole dev database) one thread
would keep up. The split still costs only two more connections per worker, and it keeps the two
failure domains apart, so it is chosen anyway.

Like `LiveBridge`, it starts with the first socket in the process (`consumer.py:345-349`). That is
correct here: only a worker holding sockets has anything to deliver. It is also why the webhook
sender cannot live in it. An offline target has no socket anywhere, so nothing would start it.
It copies LiveBridge's order on every (re)LISTEN: sweep first, then report listening
(`mesh_notify.py:703-707`). A presence row (4.3) is therefore written only by a worker that has
already swept.

### 4.3 Cross-worker presence: `hub_connections`

MeshyChat needs an online decision that works across workers. This repository's presence is
per process (`apps/wsgw/presence.py:21-35`, the project's design log (not part of this release)). [M] Django runs each
HTTP request under its own `ThreadSensitiveContext` (`django/core/handlers/asgi.py:172`, Django
6.1.1 in `.venv`), so an HTTP invoke is served in a thread that does not own the event loop the
  sockets live on. [I] In a multi-worker deployment the invoke and the target's socket are usually
  in DIFFERENT processes, so the in-process registry cannot answer "is the target connected".
  HQD runs one daphne process today (cutover-list 7a), where the registry is exact. The table is
  chosen so the design remains correct when a second worker starts.

Schema (a new file in `sql_meshychat/`; the number is taken at merge, rule E5. [M] Main holds
011 to 013, so the first free number is 014 unless another branch lands first. Divergence 035
and 036 are reserved for `fin/ops-livez` and `fin/ops-replay2`, and neither adds a migration):

```sql
CREATE TABLE hub_connections (
    connection_id  uuid PRIMARY KEY,
    peer_id        uuid NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    worker_token   uuid NOT NULL,          -- random per process start
    connected_at   timestamptz NOT NULL DEFAULT now(),
    seen_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX hub_connections_peer_seen_idx ON hub_connections (peer_id, seen_at);
```

- Insert only AFTER this worker's `CallBridge` reports LISTENING, that is after the bridge start
  at `consumer.py:345-349`. Do not insert after `register` (`consumer.py:296`). The bridge start is
  non-fatal (`consumer.py:346-349`). A row written for a socket whose worker cannot deliver would
  route calls into a black hole until their deadline. A target can SEE its inbound calls over
  HTTP (`GET /v1/calls?role=target`, `apps/calls/views.py:471-500`), but it has no HTTP route to
  ACT on one: ack, running and result exist only as frames. Delete in `disconnect`
  (`consumer.py:462-475`). A revocation close (030) ends in `disconnect` too, so a revoked
  socket's row goes with it.
- Each worker UPSERTS a row for every socket in its registry every 10 s, on the CallBridge thread,
  instead of only updating `seen_at` on rows that exist. An insert that failed once is then
  repaired on the next beat. A worker whose LISTEN connection is down stops beating, so its rows
  age out. The beat reads the registry through the event loop (`run_coroutine_threadsafe`, as
  `LiveBridge._sweep` does at `mesh_notify.py:865`), so a wedged loop also stops the beat.
- **Online** means `EXISTS (SELECT 1 FROM hub_connections WHERE peer_id = $t AND seen_at > now() -
  interval '30 seconds')`, three beats. That is deliberately shorter than the 120 s Redis
  TTL (`PRESENCE_TTL_SECS`, `apps/wsgw/constants.py:67-68`). [I] With 120 s, a crashed worker's
  rows say "online" for four times the 30 s default deadline. A call routed in that window also
  gets no webhook doorbell, because only `queued` rings. Rival: crashes are rare enough that the
  window never matters. The shorter TTL costs one small UPSERT per socket every 10 s either way.
- A crashed worker's rows stop being refreshed and age out. A call routed to a dead socket in that
  window is recovered by reconnect redelivery or times out at its deadline.
- **RLS on, writes bound to a context** (changed by the critique, F13). The first version had no
  RLS, "as `peers` has none". But a presence row is a routing input: any `meshy_app` connection,
  a request's included, could then write one and make an offline peer look online. Its calls
  would then sit `routed` with no socket and no doorbell. Divergence 022 refuses a request's
  write to a projection for exactly this reason, and 029 binds every UPDATE and DELETE policy to
  a context. So INSERT, UPDATE and DELETE require `app.server_internal`, and SELECT does too.
  The CallBridge thread and step 9 already run there.
- It also lets the dashboard's `ws_online` stop reading a Redis key that nothing writes
  (the project's design log (not part of this release)). That is left for a later slice.
- **A safety net, so presence is an optimisation and not a correctness condition.** The
  CallBridge also acts on a `queued` notification (4.6). If the row's target holds a socket in
  THIS worker, the bridge runs the guarded `queued` to `routed` and delivers. A presence miss (a
  row not written yet, a failed insert, a heartbeat late under load) then delays a call by one
  notification instead of stranding it until the target reconnects. Q14 asks whether the table is
  worth keeping at all once this net exists.

### 4.4 Step 9 for a remote target

`apps/calls/route.py:118-123` becomes:

1. **Memory authority:** unchanged.
2. **Any other active target**, in ONE transaction under `rls.server_internal`:
   - `UPDATE skill_calls SET status = CASE WHEN <online> THEN 'routed' ELSE 'queued' END WHERE
     call_id = $1 AND status = 'pending' RETURNING status`.
   - Insert the matching `routed` or `queued` event, `source = 'meshy'`.
   - If `queued` and the target has a verified row in `peer_invocation_webhooks` (4.11), insert a
     `peer_webhook_attempts` row in state `due`, unless the peer already has one outstanding
     (a partial unique index refuses the second) or was rung in the last 60 s (4.11).
   - COMMIT. The status trigger (4.5) sends the pointer at commit, so the target can never see a
     `skill_invoke` for a row that is not yet `routed`. That is the property
     works to keep, and a commit-time NOTIFY gives it by construction.
3. The call is no longer terminal when `start_call` returns. The HTTP views already handle that:
   async answers 202 (`views.py:454-458`), and sync polls for `MESHY_SYNC_TIMEOUT_SECONDS`
   (default 0, `views.py:85-99`) and then answers 202 with `Location`. Q1 asks whether the
    operator wants a 30 s sync wait for remote calls. The recommendation is to
   keep 0 and let the client poll.

A suspended target never reaches step 9. [M] Step 5 resolves the target's owner with
`status = 'active'` in the predicate and raises `NotAuthorized` otherwise
(`apps/calls/authorize.py:93-103`, called for the target and the caller at
`apps/calls/pipeline.py:233-234`). A target suspended in
the milliseconds between step 5 and step 9 is caught by the delivery side instead: the CallBridge
re-checks `active` before sending (4.6), and the target's frames carry the active clause (4.7).
`E_TARGET_UNREACHABLE` therefore leaves the remote branch entirely.

**In-process calls are never offered to a socket.** A call whose target is the memory authority
runs in-process between `pending` and its terminal (`apps/calls/route.py:145-174`). If a memory
daemon connected as that peer, connect-time redelivery (4.8) would select the authority's
`pending` and `running` rows. The daemon could run the write a second time while the in-process
run is still going, and its `skill_result` could win the guarded `running` to terminal race. So
redelivery, the CallBridge, and every target frame exclude `target_peer_id = <memory authority>`,
using the same `is_memory_authority` test as step 9 (`route.py:118`). A target frame naming such
a call gets the uniform refusal.

**A `dashboard_test` call to a remote target fails at step 9** with `E_UNVERIFIED_CALL`. Its row
stores no signature, so its `skill_invoke` would carry `signature: null`. The target verifies the
caller's signature before it runs anything. Refusing at the router gives a typed answer at once
instead of a target-side refusal after delivery.

### 4.5 Notifications: two triggers, pointer payloads

```sql
-- AFTER UPDATE OF status ON skill_calls, when NEW.status IS DISTINCT FROM OLD.status:
PERFORM pg_notify('meshy_call', json_build_object('call_id', NEW.call_id,
                                                  'status', NEW.status,
                                                  'target', NEW.target_peer_id)::text);
-- AFTER INSERT ON skill_call_chunks:
PERFORM pg_notify('meshy_call_chunk', json_build_object('call_id', NEW.call_id,
                                                        'sequence', NEW.sequence)::text);
```

- **Why triggers and not `pg_notify` calls in application code.** Status changes are written by at
  least five code paths in three processes: `route.py`, the cancel view, the frame handlers, the
  janitor (`run_jobs` and `run_call_worker`) and the doorbell. [I] One of them would forget. The
  janitor is the likely one, and then a caller's socket would never hear of a `timeout`. Rival:
  application calls are easier to see in a diff. The trigger wins because a forgotten writer
  fails silently, and a test can prove the trigger fires on every edge.
- **Payload size.** `call_id` is at most 200 bytes (`apps/calls/repository.py:45`), and
  one uuid rides with it, far under NOTIFY's 8000-byte limit. Nothing caller-controlled beyond the
  call_id goes in.
- **`target` is a HINT, not a fact.** It lets a worker skip a notification for a target it does not
  hold without a database read. A forged hint can at most cause one wasted read, because the row
  is re-read and decides.
- **Cost.** One NOTIFY per status change and per chunk. Postgres folds identical payloads within
  one transaction. The queue gauge read 1.9e-06 [M].
- **Any writer fires them.** A trigger does not know which server wrote the row. With a second
  server on the same database, its writes notify this one's bridges too (5.5).

### 4.6 The CallBridge

On each notification, the payload is parsed for the pointer only, and malformed payloads are
counted and skipped, as `mesh_notify.py:753-758` does:

| notification | local precondition, checked BEFORE any DB read | work |
|---|---|---|
| `meshy_call`, `status = routed` | the payload's `target` hint has a socket in THIS worker | read the row under `server_internal` by the UNIQUE `call_id`; require `routed`, `deadline_at > now()`, the ROW's target active, connected here and not the memory authority (4.4); build `skill_invoke` from the ROW; `publish_to_peer(row target)` |
| `meshy_call`, `status = queued` | the payload's `target` hint has a socket in THIS worker | the presence safety net (4.3): `UPDATE skill_calls SET status = 'routed' WHERE call_id = $1 AND status = 'queued' AND deadline_at > now() AND target_peer_id = ANY($locally_connected)`, with its event, committed. The ROW's target decides, never the hint, so a forged notification can only route a real queued call to a target that really is connected here. That commit's own `routed` notification then delivers through the row above. Never for the memory authority (4.4) |
| `meshy_call`, a terminal status | `call_id` is registered in this worker's `CallRegistry` | re-read; build `terminal_frame(row)` from the signed status; deliver; unregister |
| `meshy_call_chunk` | `call_id` is registered here | read the chunk row by `(call_id, sequence)`; build `skill_chunk`; deliver |
| LISTEN (re)established | always, BEFORE listening is reported | **recovery sweep**: (a) for every locally connected peer, redeliver its in-flight calls (4.8); (b) for every registered call, replay the chunks after the highest sequence this registration has already delivered (the registry keeps that high-water mark), then deliver the terminal frame if the row is terminal. This applies the `Lagged` handling to a LISTEN connection that was down |
| a timer every 10 s (the revocation sweep's cadence), and at once after a send-queue loss on a local socket | a locally connected target, or a call registered here | **re-offer sweep** (NEW, critique F8): `routed` rows of locally connected targets whose deadline is in the future, older than 10 s, with no target event yet, are sent again, bounded by `LIMIT`; for registered calls, a terminal re-check. Without it a `skill_invoke` lost to the drop-oldest queue or to a loop hop that timed out leaves a connected target silent until the deadline, and 4.9's at-least-once would not hold. Targets already dedupe on `call_id` |

A forged notification can only cause a real row to be delivered to its real party again, the same
guarantee divergence 020 proves for mesh nudges (the project's design log (not part of this release)). Stats are counted per
reason on the loop as each frame is queued. That fixes the counter race divergence 020 hit
(the project's design log (not part of this release)).

`skill_invoke` is built from the row: `call_id`, `caller_peer_id`, `target_peer_id`, `skill_name`,
`skill_version`, `mode`, `deadline_at`, and `envelope = {payload: args, signature: args_signature,
signing_kid: args_signing_kid}`. `args` holds the whole signed payload, so the target can
re-verify the caller's signature. The `continuation_nonce` and `continuation_token` fields are
omitted. The flow guard that mints them is not implemented (`apps/calls/pipeline.py:50-54`), and
serialization omits them when absent.

It goes to EVERY socket of the target, with no per-key gate. The args are addressed to the peer,
and a socket whose key was revoked is no longer in the registry, because 030's drop removed it
(`connection_manager.py:213-228`). `publish_to_peer`'s `by_key` (divergence 033) is therefore not
used for calls. That is a decision, not an oversight: 033's per-key frame exists because a mesh
preview depends on memory.read's gates, and a call's args depend on nothing but the target.

### 4.7 Frame handlers

Common rules for every frame:

- **The socket's own connection.** Each socket lazily opens ONE role-bound autocommit connection
  (`apps/core/dbconn.connect`) for its target-side frames and closes it on disconnect. Channels
  dispatches a consumer's messages one at a time: [M] `channels/utils.py:43-51`
  (`await_many_dispatch` awaits `dispatch` before taking the next message). So these frames stay
  in wire order, which the contract needs (running before chunk before result), and the
  connection is never shared. Today every helper in `apps/wsgw/db.py` opens a new connection per
  statement. Since 030 an upgrade alone opens five. [I] At chunk rates that is a connect, a
  `SET ROLE`, and a probe per token. The per-socket connection is cheap either way.
- **Uniform refusal (closes 2.12.1).** "No such call" and "not your call" produce the SAME code and
  the SAME message per frame, for example `SKILL_ACK_FAILED` with "call is not routed to this
  peer". The two cases stay distinguishable in the server log. After ownership is proven, a
  refusal may say more (`InvalidTransition` text).
- **Active peer, and a live bound key, in the same statement.** Every target statement carries
  `AND EXISTS (SELECT 1 FROM peers p WHERE p.id = $me AND p.status = 'active')`. Divergence 030
  now closes a suspended peer's socket with 4410. That line in divergence 020
  (the project's design log (not part of this release), "Revocation still has no listener") is history. But a suspension is
  written by SQL and sends no notification, so the close waits for the next sweep, up to
  `REVOCATION_SWEEP_SECS` = 10 s (`apps/wsgw/constants.py:101-106`). It waits longer while the
  LISTEN connection is down (R12). This clause refuses the socket's frames in that window. When
  the socket's token is bound to a key (`ConnectionHandle.bound_key`), the statement also requires
  that key to be live by the rule `revocation.key_states` applies (`apps/wsgw/revocation.py:43-47`).
  A frame sent between a key revocation and its 4412 close is then refused too. `skill_chunk` and
  `skill_result` already refuse a revoked SIGNING key at `verify_payload`'s check 6
  (`apps/calls/envelope.py:220-222`). The new clause covers `skill_ack`, `skill_running` and
  `skill_error`, which carry no signature. The cost is two index probes inside a statement that
  runs anyway.
- **Error frames go through the queue.** Only `SkillFrameNotImplemented` keeps its direct send and
  close (`consumer.py:449-458`), and it now covers `skill_resume` alone.

| frame | check (in order) | SQL | reply on success |
|---|---|---|---|
| `skill_call` | semaphore of 16 per socket, else `SKILL_CALL_BUSY` without blocking the reader; a process-wide cap on concurrent pipeline threads (R1); `mode_override` in {sync, async, stream}, and `lesson` is refused with `SKILL_CALL_BAD` (2.12.7); then `start_call(transport=ws)` in a thread, on a fresh role-bound connection | the pipeline's | see "registration order" below |
| `skill_ack` | target, `routed`, peer active | `INSERT INTO skill_call_events ... SELECT ... WHERE EXISTS (call with target = me AND status = 'routed') AND <active>` | none |
| `skill_running` | target, `routed` to `running`, peer active | guarded UPDATE plus a `running` event, `source='target'`, `source_peer_id = me`, in one transaction | none |
| `skill_chunk` | `chunk_type` in {text, json}; `file_ref` refused `SKILL_CHUNK_BAD` until its slice. `verify_payload(kind=skill.chunk, peer=me)`. **Binding:** `payload.call_id == frame.call_id`, `payload.sequence == frame.sequence`, `payload.chunk_type == frame.chunk_type`. `0 <= sequence <= 2147483647`, checked at parse to keep it in the int4 range; a larger value would otherwise fail as a database error. Content at most 512 KiB | `INSERT INTO skill_call_chunks (...) SELECT ... WHERE EXISTS (call with target = me AND status = 'running') AND <active>`. A unique violation gives `SKILL_CHUNK_BAD` "duplicate sequence". Zero rows gives the uniform refusal, or `SKILL_CHUNK_NOT_RUNNING` once ownership is proven | none |
| `skill_result` | `verify_payload(kind=skill.result, peer=me)`; **binding** `payload.call_id == frame.call_id`; terminal from the signed `status`: absent or `completed` gives completed, `failed` gives failed, **anything else is refused `SKILL_RESULT_BAD`** (2.12.8, Q6) | one guarded UPDATE (`finish_call_atomic` shape, `apps/calls/route.py:273-295`), plus its event. It writes `result` = `payload.result`, `result_payload` = the verified payload, and `result_signature` and `result_signing_kid`. It writes `error_code` and `error_message` as the signed payload's own fields, NULL when absent. For `failed`, `extract_terminal` reads those fields; for `completed`, the row retains the signed fields. The fleet's verifier binds all five of status, call_id, result, error_code and error_message to the row (`tools/meshy_node/wire.py:233`, `:272-274`). A row field taken from anywhere else would read as a forged result | none |
| `skill_error` | target, `running`, peer active; `error_code` at most 128 and `error_message` at most 4096 characters | guarded UPDATE to `failed`, unsigned, plus its event | none |
| `skill_cancel` | caller; in flight; peer active | a NEW guarded statement, `cancel_by_caller`: `UPDATE skill_calls SET status = 'abandoned', completed_at = NOW() WHERE call_id = $1 AND caller_peer_id = $me AND status = ANY(in_flight) AND <active>`, with the `caller_disconnected` event in the same transaction. NOT `reads.cancel_to_abandoned` (`apps/calls/reads.py:290-327`). That statement has no caller predicate (`:317-320`) and relies on the HTTP view's own check (`apps/calls/views.py:603-607`), so a handler that called it without repeating that check would let any peer cancel any call (critique F1). Zero rows: the uniform refusal before ownership is proven (2.12.1) | none; the trigger delivers `skill_error ABANDONED` to the caller's registered sockets |
| `skill_resume` | none | none | still `UNIMPLEMENTED` and close (2.10, Q11) |
| `subscribe` with `call:<id>` | at most 1000 call subscriptions per socket, the presence cap reused (`constants.py`) | register FIRST, then `load_call`; if the peer is not the caller, unregister and stay silent, counting the reason; if it is, replay persisted chunks with `sequence >= 0`. That is NOT `reads.list_chunks`, whose `sequence > since` skips 0 (2.12.11). At most 200 rows are replayed, the newest 200 when there are more, the same bound as retention at terminal (5.2). A client that needs older chunks pages `GET /v1/calls/{id}/chunks`, where `since=-1` includes sequence 0 (`apps/calls/views.py:208-215`, `:564`). When the row is terminal, push `terminal_frame` | ack echoes the channels |
| `unsubscribe` with `call:<id>` | none | remove this socket's registration (2.12.6) | ack echoes |

**Registration order for `skill_call` (a Django-specific hazard).** A call to the memory authority
is TERMINAL when `start_call` returns (`apps/calls/route.py:144-174`), and its terminal trigger has
already fired. Registering afterwards and waiting would hang the caller. That is the in-process
twin of 2.12.4. The handler therefore:

1. If the result is replayed and terminal, or simply terminal: push `skill_acked`, then
   `terminal_frame(row)`, and do not register. This covers both replayed and non-replayed terminal
   calls.
2. Otherwise: register the connection, push `skill_acked`, then RE-READ the row. If it became
   terminal meanwhile, push `terminal_frame` and unregister. A duplicate terminal frame is
   possible and harmless, because the caller keys on `call_id`. A missing one is not.

The handler hard-codes `transport=ws`; nothing in the frame chooses a transport, so no WS frame
can claim `dashboard_test`. It builds the `CallsContext` with the two required keywords,
`envelope_nonce_enforce=nonce_ledger.enforce_from_settings()` (required once 036 lands) and
`memory_peer_id=routing.memory_peer_id_from_settings()`.

**One terminal mapping.** `terminal_frame(row)` and the HTTP terminal body (`_terminal_body`,
`apps/calls/views.py:363`) take status, result and the error fields from ONE function. A WS caller
and an HTTP poller then cannot be told different outcomes for one row. It is the rule 031 applied
to the push and the addressed long-poll, which both call one `role_for` (`mesh_notify.py:243`).

**Who signed a result, and how a caller checks it (divergence 034's rule).** A memory-authority
result is signed by the SERVER key and resolves in `/.well-known/jwks.json` (017). A remote result
is signed by the TARGET's key and resolves in that peer's published keys. The fleet's verifier
today looks only in the server JWKS (`tools/meshy_node/wire.py:237-265`). It would report every
remote result as unverifiable. A client that checks remote results must choose the key source by signer, name the
signer as 034's `signed_by` does, and report a key it could not fetch as
`E_SIGNATURE_KEY_UNAVAILABLE`, not as an invalid signature (`wire.py:289-291`). That client change
is outside F23 and listed in 7.

**Stored chunk content.** Store the RFC 8785 bytes of the verified payload (`apps/crypto`'s `jcs`),
not `json.dumps`. [I] Those are the exact bytes the signature covers, so a verifier can check
`chunk_signature` over `content` with no re-canonicalisation step (rule A3). Replay rebuilds the
frame's `envelope.payload` by parsing the stored bytes.

### 4.8 Redelivery

- **On connect:** after the `hub_connections` insert and AFTER the welcome frame is queued, so
  `welcome` stays the first frame a client reads (`apps/wsgw/consumer.py:325-328`). The selection
  is the peer's calls in `pending`, `routed`, `queued` or `running` with `deadline_at > now()`,
  bounded with `LIMIT` (rule B5). It runs after the welcome frame is queued, so direct frames
  arrive after `welcome`.
- **`pending` rows race step 9.** Step 8 commits the row before step 9 runs
  (`apps/calls/repository.py:142-157`), so a reconnecting target can see a `pending` row
  mid-pipeline. Whichever guarded UPDATE wins, the loser re-reads: step 9 then reports the
  `routed` it found, and redelivery skips a row that is no longer `pending`.
- **Order fixed (2.12.3):** first `queued`/`pending` to `routed` guarded, with its event,
  committed; only then queue `skill_invoke`. `routed` and `running` rows are re-sent unchanged; the
  target resumes or dedupes them by `call_id`.
- **On LISTEN (re)start:** the same selection for every locally connected peer (4.6).
- **Never** a row whose target is the memory authority (4.4), and never a row whose deadline has
  passed.
- [M] On the dev database today this delivers nothing: 0 in-flight rows have a future deadline.

### 4.9 Delivery guarantees, stated per frame

| to | frame | guarantee | recovery if a notification is lost | client obligation |
|---|---|---|---|---|
| target | `skill_invoke` | at-least-once while the target holds a socket before the deadline | reconnect redelivery; LISTEN-restart sweep; the 10 s re-offer sweep (4.6) | dedupe on `call_id` |
| caller | `skill_acked` | exactly once per accepted `skill_call`, on the same socket, from the handler | none needed: it is not notification-driven | none |
| caller | `skill_chunk` | at-least-once to sockets registered when the chunk commits; persisted | re-subscribe replays from the table; `GET /v1/calls/{id}/chunks` | dedupe and order on `sequence` |
| caller | terminal (`skill_result` / `skill_error` / `skill_timeout`) | at-least-once to sockets registered at commit, and to any later subscriber | LISTEN-restart sweep; subscribe-time push; `GET /v1/calls/{id}` | treat the first terminal as final |
| target | cancel or timeout | **none** (Q12) | the target's next frame is refused | none |

**Execution** is at-most-once per call on the server side, because every terminal edge is guarded
and only the winner writes. Whether a target runs the skill twice depends on its own `call_id`
dedupe.

**What is lost, and where it is recovered.** A frame dropped by the drop-oldest queue is lost to
that socket (R3). The table remains authoritative, and the two read routes that already exist
recover it. A dropped `skill_invoke` or terminal frame is also re-offered by the sweep the loss
triggers (4.6). A dropped chunk is not; the caller sees the sequence gap.

### 4.10 A call to an offline target

- **Recommended (Q1).** `queued`, then a webhook doorbell if one is registered,
  then either the target connects before the deadline (redelivery flips the row to `routed` and
  delivers) or the janitor times it out with `E_DEADLINE_EXCEEDED` and "passed while the call was
  queued" (`apps/calls/janitor.py:66-76`). The caller learns why from the row.
- **The alternative:** keep today's immediate `E_TARGET_UNREACHABLE` when no socket and no webhook
  exist. It is faster to fail, but an agent that reconnects within the deadline would then never
  receive a call it could have answered.
- **Lifecycle (E10):** the janitor must be running, and on time. Without it a `queued` or
  `routed` call waits forever. The README runbook already schedules `run_jobs --loop --interval
  30` (README.md, "5. Schedule the jobs"), and it did at c5457ac too. The line in
  the project's design log (not part of this release) that nothing schedules it is stale. The hazard is cadence, not
  absence.
  - [M] `run_due` runs every registered job in one sequence (`apps/jobs/runner.py:55-95`), and
    the loop sleeps its interval AFTER the tick (`run_jobs.py:88-90`). The tick includes
    divergence 032's link job. The operator keeps that job ON as inferred hints (cutover-list
    decision 15, 2026-09-24), and its tick may take 45 s plus a 20 s per-source budget
    (`apps/jobs/registry.py:133-148`). The embed job's tick may take 30 s (`:116-132`).
  - [I] So one deadline sweep can come up to about two minutes after the one before it. The link
    job has nothing to do on most ticks, which bounds the delay in the common case only.
  - The slice that introduces `queued` therefore does three things. (a) It sets
    `call_deadline_sweep.interval_secs` to 5. (b) It runs that job in `run_call_worker`'s own
    5 s loop that runs only `call_deadline_sweep`, and leaves the shared loop and the
    link job unchanged; the lease decides which process runs a given tick. (c) It adds a health
    signal for "oldest in-flight row past its deadline" (rule B9).

### 4.11 Webhooks

**Scope.** Only the target's own wake-up URL, the `peers.invocation_delivery_webhook` idea,
kept in a new table (below) and used only when the call is `queued`. It is not a result channel.
A woken target connects its socket and answers over frames. Results are not posted to an HTTP route.
Out of scope,
each by recommendation (Q10, Q11):

- **`skill_calls.webhook_url`.** It is caller-supplied; sending to it would make every caller an
  SSRF client. It stays stored and unread, and the divergence says so.
- **Push-notification configs.** The table exists (`sql/051`), but there is no route, and the agent
  card advertises `pushNotifications: false` (`apps/peers/cards.py:58`). The egress module and the
  signer below are built so push configs can reuse them later.

**Registration.** `PUT /v1/peers/me/invocation-webhook` with body
`{url}`, and `DELETE` on the same path. Both go through the one peer extractor,
`authenticate_peer` with DPoP (`apps/peers/auth.py:115-160`), the write boundary, and there is no
cookie path. The URL is checked at registration: https, port 443, no userinfo, no fragment, at
most 2048 characters, and a host that is not a blocked IP literal. The network check is repeated
at every send, so registration-time validation is a convenience, not the guard.

- **Its own table, not the column** (critique F10). Registrations go in a new
  `peer_invocation_webhooks` table: `peer_id` (PK, FK to `peers` ON DELETE CASCADE), `url`,
  `challenge`, `requested_at`, `verified_at`, `disabled_at`, RLS on, written under
  `server_internal`. They do NOT go in `peers.invocation_delivery_webhook`. The column stays
  unread, and registrations remain isolated in their own table (5.5).
- **Proof of control before the first ring** (the designer's revision promised it; the critique
  specifies it). A PUT stores the URL unverified, with a fresh 32-byte `challenge`, and its
  commit NOTIFYs `run_call_worker`. The worker POSTs `{type: "invocation_webhook_verify",
  challenge}`, signed like a doorbell, through the egress module. It sets `verified_at` only when
  the 2xx body echoes the challenge exactly. The request thread never makes the POST (rule E7).
  A URL that fails stays unverified and is never rung. [I] Without the check a peer could point
  its doorbell at a third party and turn every call to itself into a POST at that party. Port 443
  and one doorbell per peer per 60 s bound the rate. The check costs one POST per registration.
- **What a proof does not cover.** The host's DNS can change after verification. The public-only
  check at every send still holds, so what remains is a reflection risk at the rate floor, not an
  SSRF one.

**The egress module `apps/core/egress.py`.** It is new, and deliberately NOT
`apps/modelhost/locality.py`. MUST-8 admits only loopback and RFC1918 for content
(`locality.py:10-17`). An SSRF guard admits only PUBLIC addresses for peer-controlled URLs. The two
policies are opposites; one shared helper would be wrong for one of them.

- **Blocklist:** It covers loopback, RFC1918,
  link-local and metadata `169.254/16`, unspecified, broadcast, documentation, `0/8`, CGNAT
  `100.64/10`, `192.0.0.0/24`, `240/4`, v6 loopback and unspecified, ULA `fc00::/7`, link-local
  `fe80::/10`, site-local `fec0::/10`, `2001:db8::/32`, and Teredo `2001::/32`. It unwraps an IPv4
  embedded in a v6 literal: mapped, compatible, NAT64 `64:ff9b::/96`, SIIT `::ffff:0:0/96` and 6to4
  `2002::/16`. As a second, independent check, anything Python's
  `ipaddress` does not call `is_global` is also refused. The address is blocked if EITHER says so.
  `is_global` must NOT be the guard on its own. [M] On this repository's interpreter
  (`.venv`, Python 3.14.3) it calls `fec0::1` (site-local), `::7f00:1` (v4-compatible
  127.0.0.1) and `64:ff9b::7f00:1` (NAT64 127.0.0.1) GLOBAL. Only the explicit port blocks them.
  The two disagree in the other direction on 6to4 with a public v4: Python calls
  `2002:808:808::` not global. The explicit blocklist controls the decision, and the divergence
  entry records it.
- **Resolve once, then connect to what was validated.** `getaddrinfo` runs in a thread with a 5 s
  cap. If ANY returned address is blocked, or none is returned, the send is refused. The connection
  then opens to the first validated
  address with an `http.client.HTTPSConnection` subclass whose `connect()` dials that IP. It
  passes the hostname as TLS `server_hostname`, so SNI and certificate verification are for the
  name, and sends `Host:` as the name. A second DNS answer never reaches a socket, which is the
  DNS-rebinding defence without a new dependency.
- **No proxy, no redirect.** [I] `http.client`, unlike `urllib`, reads no proxy environment and
  follows no redirect; a 3xx is a failure. This is library behaviour, not a measurement, so a test
  proves both against a recording server rather than trusting it, the way
  `apps/meshsem/providers.py:12-23` proves them for MUST-8.
- **https only, port 443 only.** Plain http is allowed only by the test flag below. [I] A bearer
  JWT over cleartext is capturable. `aud`, `exp`, `jti` and the body hash bound its reuse. https
  is still cheap, and the receiver is a peer's own server. One port keeps a
  registration from aiming the doorbell at an arbitrary service on a public host.
- **Timeout 15 s** total. The response is read to at most 64 KiB, then
  discarded.
- **Test hatch:** `MESHY_EGRESS_ALLOW_LOOPBACK`. It opens ONLY loopback and also permits http. The deploy check refuses
  to boot a production settings module with it set.

**Signing (Q3).** `Authorization: Bearer <JWT>`, EdDSA, under a DEDICATED Ed25519 webhook key.
The first version of this section used `result_signing.server_result_signer()` while the summary
already said "a dedicated key". The critique resolved the contradiction for the dedicated key
(F3):

- **Not the token key.** `server_result_signer()` signs with the key that mints peer tokens: it
  reads `get_config().jwt_signing_key_pem` (`apps/calls/result_signing.py:68-88`). The one JWT
  verifier requires only `exp` and `iss`, reads no `typ`, and leaves `aud` to the extractors
  (`apps/authapi/tokens.py:237-250`). Under that key a doorbell would be a valid token that only
  the extractors' `aud == "peer"` rule refuses (`apps/peers/auth.py:132`,
  `apps/wsgw/auth.py:168-173`). Under its own key, no verifier configured with the token key can
  accept it at all, whatever its claims say.
- **Custody.** The key is generated on each host when `run_call_worker` first starts. It is
  written with an exclusive create, never overwritten, never copied between hosts, and rotated by
  deleting it. A doorbell lives 120 s and a receiver resolves the kid from the JWKS, so no
  webhook key has to survive a host move. Nothing is added to the secrets the operator copies by
  hand at cutover (cutover-list decision 7).
- **Published** in `/.well-known/jwks.json` as a second entry under its own server-rule kid
  (`apps/authapi/views.py:633-648`). That route publishes only the current token key today. The
  added entry is a wire change, and the divergence states it.
- Header: `{alg: EdDSA, kid: <webhook kid>, typ: "meshy-invoke+jwt"}`.
- Claims: `{iss, sub: <target peer id>, aud: <exact URL>, iat, exp: iat+120, jti: uuid4,
  body_sha256: <hex of the exact body bytes>}`. The body carries the `call_id`, and the hash
  binds it.
- It includes `body_sha256`, so a captured token cannot carry a different body, and `typ`.
- The receiver verifies against `/.well-known/jwks.json`, checks `aud` equals its own URL, checks
  `exp`, and dedupes `jti`.
- An HMAC with a per-peer shared secret (`peers.hmac_secret`, `sql/011`, 0 rows set on the dev
  database [M]) needs the server to hold a symmetric secret per peer in plaintext. One
  Ed25519 key per host holds one secret for every peer.

**Body (Q2).** Recommended: a pointer-only doorbell,
`{type: "skill_invoke_pending", call_id, target_peer_id, skill_name, deadline_at}`. The target
connects, and redelivery hands it the full `skill_invoke` over its authenticated socket.

- [I] This sends no caller args to a public host. The target receives the full invocation after it
  connects. Production use remains unmeasured (5.5).
- The pointer body also keeps F23 clear of MUST-8. Call args can quote mesh content, and MUST-8
  forbids mesh content to an external API (`apps/meshsem/providers.py:1-6`).

**Delivery record (rule B7).** New table `peer_webhook_attempts`: `id`, `peer_id` (FK), `call_id`
(FK, the call that rang it), `state` (due, claimed, sent, failed, skipped), `claimed_by`,
`claimed_at`, `finished_at`, `http_status`, `error_class`. A partial unique index
`(peer_id) WHERE state IN ('due', 'claimed')` allows at most one doorbell outstanding per target.
The first version named this table `skill_call_webhook_attempts` with `UNIQUE (call_id, id)`,
which bounds nothing, because `id` is already unique. The designer's revision renamed it in 4.4
only.

- The pipeline inserts `due` in the step-9 transaction (4.4), `ON CONFLICT DO NOTHING` on that
  index. It inserts nothing when the peer was rung in the last 60 s (the rate floor). An insert
  trigger NOTIFYs `meshy_call_webhook` at commit (4.5's rule: a trigger, not application code).
- The worker claims with
  `UPDATE ... SET state='claimed' ... WHERE id = (SELECT id ... WHERE state='due' ORDER BY id
  LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING ...`. The claim is written BEFORE the POST.
- It re-checks that the peer still has a `queued` call with a future deadline, POSTs, and
  records the outcome. On a 2xx it writes a `webhook_fired` event on the call that rang it, and
  NOTHING else: the row stays `queued`. `routed` keeps one meaning, "a socket of the target was
  offered this call", and the CallBridge and redelivery rely on it (4.6, 4.8). The woken target
  connects, and redelivery moves the row. A 2xx records `webhook_fired` only, as the diagram in
  4.2 states. The event name is reused; `event_type` has no CHECK [M].
- It also sweeps every 5 s for `due` rows (a lost NOTIFY) and for `claimed` rows older than 30 s
  (a crash between claim and send). A second doorbell is harmless because it is only a pointer.
- One attempt per sweep: a failure leaves the call `queued`, and reconnect
  redelivery still works.

**Where it runs (Q4).** `manage.py run_call_worker`, one long-running command for the call plane.
It runs the 5 s deadline sweep from S5 (4.10), then the doorbell and its URL verification from
S9. It is one process, not two. HQD is dedicated to the mesh on 10 GiB, and its job ticker alone
measured 284 MB private (cutover-list 7a, 2026-09-23).

- [M] `run_jobs --loop` sleeps a fixed interval, 30 s by default
  (`apps/jobs/management/commands/run_jobs.py:37-38`, `:90`), AFTER a tick that can itself run
  over a minute (4.10). A call's default deadline is also 30 s (`apps/calls/repository.py:42`).
  A doorbell sent from that loop would regularly arrive after the call had timed out.
- Teaching `run_jobs --loop` to wait on LISTEN instead of sleeping would use fewer processes, but
  it changes the lease semantics of every job. It remains an option if the operator refuses
  another process.

---

## 5. Security

### 5.1 Authorization per frame

| frame | principal | enforced by |
|---|---|---|
| upgrade | JWT subject, active, DPoP proof and jti unspent, bound key not revoked | `apps/wsgw/consumer.py:168-257`, unchanged |
| `skill_call` | caller = socket peer = signed `caller_peer_id` | `verify_payload` (`apps/calls/envelope.py:196-212`) inside the one pipeline; grant via `authorize` built from the authenticated identity (`apps/calls/authorize.py:1-17`); a suspended caller is refused at step 5 (`authorize.py:93-103`) |
| `skill_ack`, `skill_running`, `skill_chunk`, `skill_result`, `skill_error` | socket peer = `skill_calls.target_peer_id`, the peer is active, and the socket's bound key is live | the guarded statement's `WHERE` (4.7), not an application `if` before it |
| `skill_chunk`, `skill_result` payload | signer = socket peer = `payload.target_peer_id`; `payload.call_id` = frame call_id | `verify_payload` plus the new binding |
| `skill_cancel`, `subscribe call:` | socket peer = `skill_calls.caller_peer_id` | the `caller_peer_id = $me` predicate inside `cancel_by_caller`'s UPDATE, NOT `reads.cancel_to_abandoned`, which has no caller predicate (`apps/calls/reads.py:317-320`); and the subscribe check (4.7) |
| delivery of `skill_invoke` | the row's target only | CallBridge builds it from the row and publishes to `target_peer_id` alone, to every socket it holds; a socket dropped by 030 is no longer in the registry |
| delivery of chunk and terminal frames | sockets that proved caller ownership at registration | `CallRegistry` holds only such sockets |

**A peer can only answer calls routed to it.** The target's identity is in the same `UPDATE ...
WHERE target_peer_id = $me AND status = $expected` that performs the transition, so no
time-of-check/time-of-use gap exists between "is this mine" and "change it". A peer cannot move a
call it merely knows the id of, cannot answer before `routed` (no `running`), cannot answer twice
(a guarded terminal), and cannot answer after a timeout or cancel (the status moved on).

### 5.2 Chunk injection

- **A non-target** cannot insert a chunk. The INSERT's `EXISTS` requires `target_peer_id = me` and
  `running`, and the envelope must verify under the socket peer's key with `target_peer_id = me`.
- **The target itself** cannot place a chunk signed for call A or sequence N into call B or
  sequence M: the binding rule.
- **Duplicates or rewrites** of a sequence hit `UNIQUE (call_id, sequence)`.
- **A flood** is bounded by 512 KiB per chunk, the 16 MiB message cap, and the socket's
  sequential dispatch. Retention trims to 200 rows at terminal in the terminal transaction.
- **A `file_ref` chunk** is refused until its slice (Q11). It needs the owner and `ready` check of
   against `apps/files`.

### 5.3 Replay (operator ruling: envelope replay enforcement ON by default)

[M] **On main at 0b6767f the default is still OFF**, and it is read in three places:

- `pipeline.CallsContext.envelope_nonce_enforce = False` (`apps/calls/pipeline.py:88`).
- `getattr(settings, "MESHY_ENVELOPE_NONCE_ENFORCE", False)` in `apps/calls/views.py:81-82`.
- The same expression, separately, in `apps/a2a/views.py:520-521`.
- `meshychat/settings.py` has no entry. Five test modules pin it False (`tests/test_a2a.py:107`,
  `test_call_routing.py:90`, `test_calls_http.py:100`, `test_mesh_live.py:107`,
  `test_ws_revocation.py:125`).
- an internal project document (not part of this release) lists it.

[M] **`fin/ops-replay2` fixes it** (`d447af0`, divergence 036, read by `git diff`, not on main).
`MESHYCHAT_ENVELOPE_NONCE_ENFORCE` is parsed once in settings: ON when unset, and a non-boolean value
refuses to load. `nonce_ledger.enforce_from_settings()` is the one reader, and
`CallsContext.envelope_nonce_enforce` becomes a required keyword.

Design:

- **No S0.** The reader is 036's. The WS `skill_call` handler passes
  `nonce_ledger.enforce_from_settings()`. Because 036 makes the field required, a handler that
  forgot it fails at construction, not open. S6 lands after 036 (Q13).
- **`skill.call` over WS** passes through the same gate 8 as HTTP, because it is the same
  `start_call`.
- **What a replay test can see.** Step 2, idempotency, runs before gate 4c
  (`apps/calls/pipeline.py:183-192`). A byte-identical resend of an envelope whose first
  presentation CREATED a call is therefore answered from that row as a replay, ON or OFF. The
  ledger decides only three cases: a resend whose first presentation created no call, a nonce
  re-signed under a new call_id, and a payload with no nonce (036's own account, in the
  `nonce_ledger.py` docstring at `d447af0`). The first version's T16 replayed a byte-identical
  envelope. It would have passed with the ledger OFF too, so it could not kill its own mutant.
  T16 is rewritten (section 6).
- **`skill.result` and `skill.chunk` get NO nonce ledger (Q5).** Replaying either is already
  inert: a second result meets a non-`running` row and is an idempotent no-op; a second chunk hits
  the unique constraint; a result or chunk
  moved to another call fails the binding; and the 120 s `issued_at` window still applies
  (`apps/calls/envelope.py:185-194`). [M] Requiring a nonce would also refuse a current daemon:
  the inference daemon's result payload has no `nonce` field, while the memory daemon's does. The
  ledger is not applied there (2.12.9).
- **Frames themselves** are not signed, and need not be. The socket is the credential: DPoP-bound
  at upgrade with a spent jti (`consumer.py:211-240`), closed at JWT expiry (`:337-339`), and
  closed on a revocation (030). A frame captured off one socket cannot be sent on another
  without that socket's credentials.

### 5.4 Other attacks, and the answer to each

| attack | answer |
|---|---|
| probe whether call X exists | uniform refusal on target frames, cancel and subscribe (4.7). NOT closed: step 2's idempotency conflict and the HTTP routes' 404 versus 403 still answer (2.12.13). Recommended: accept those. A call_id can be guessed only when its caller picks a guessable one (Q7) |
| forged NOTIFY on `meshy_call` or `meshy_call_chunk` | the payload is a pointer; frames are rebuilt from the row and go only to the row's parties (4.6) |
| a suspended target answers from its still-open socket, or a socket whose bound key was revoked | 030 closes both (4410, 4412); the active and live-key clauses in every target statement cover the window before the close (R12) |
| one socket floods `skill_call` | per-socket semaphore of 16; process-wide pipeline cap (R1). Quota has no port anywhere yet (`apps/calls/views.py:33-40`); when it lands it goes in `start_call`, so WS is metered too (rule E3; the WS path was once unmetered, E3's own example) |
| a subscriber forces a replay storm | 1000 call subscriptions per socket; a replay is at most 200 rows, for a running call as for a terminal one (4.7); drop-oldest bounds frames in memory (R3 covers bytes) |
| SSRF through the webhook | 4.11: public-only, resolve-once-and-pin, no proxy, no redirect, https on port 443, and re-checked at send |
| a webhook receiver impersonated or its request replayed | EdDSA JWT with `aud`, `exp`, `jti` and `body_sha256`, under a dedicated key that no token verifier trusts (4.11) |
| a doorbell JWT presented as a peer token | signed by a key the token verifier is not configured with; and its `aud` is a URL, never `peer` |
| a peer points its doorbell at a third party | proof of control before the first ring; port 443 only; one doorbell outstanding and one per 60 s per peer (4.11) |
| a request path writes a presence row and makes an offline peer look online | RLS on `hub_connections`: writes only under `server_internal` (4.3) |
| a replayed WS `skill_call` | one pipeline: step 2 answers a byte-identical resend from its row; gate 4c refuses a reused nonce under a new call_id (5.3) |
| a caller-supplied `webhook_url` | never read (4.11) |
| a second server on the same database | 5.5 |

### 5.5 One writer during a cutover

The protections above require this server to be the only writer of `skill_calls`,
`skill_call_chunks` and `skill_call_events`.

[I] Concurrent writers can route one row twice, send an unsigned doorbell, or apply weaker frame
validation. A target connected to more than one writer can run a call twice unless it dedupes.

What that means for the design:

1. Registrations live in `peer_invocation_webhooks` (4.11).
2. The triggers (4.5) fire on call-table writes. One writer prevents duplicate routing.
3. At a switch, the memory daemon is stopped. This server runs memory in-process (divergence 017),
   and 4.4's guard would leave the daemon connected and idle. The inference daemon becomes a WS
   target of this server, and C5 (section 6) proves that before the switch.
4. Redelivery picks up in-flight rows only while their deadline is in the future. The janitor times
   the rest out. Nothing else needs migrating.
5. The production counts behind 3.2's dev-database figures are re-measured on the standby before
   S9 is enabled there, counts only: webhook values in the column, push configs, in-flight rows
   with a future deadline, and chunks at sequence 0. This seat did not read production, and the
   standby is not this design's to touch.

---

## 6. Test plan (RED first)

Every test runs against `meshychat.asgi.application` with `WebsocketCommunicator`, real EdDSA JWTs
signed by the test server key, a real Postgres, and the PRODUCTION `CallBridge` and `LiveBridge`,
the same harness `tests/test_mesh_live.py:99-276` uses. Two fake peers:

- **C (caller)** connects and sends frames, or invokes over HTTP through `django.test.Client`.
- **T (target)** has a registered Ed25519 key in `peer_keys` and a skill row. It runs a small loop
  that reads `skill_invoke`, verifies the caller's signature, and answers ack, running, chunks and
  result with envelopes signed by its own key.

DPoP: the suite disables it by default (`tests/conftest.py:252-277`). The end-to-end tests in each
slice run with `MESHY_DPOP_ENFORCE=True` and real proofs, because the WS upgrade is the write
boundary. Autouse fixtures clear `MANAGER`, the new `CallRegistry` and `hub_connections` after each
test (the harness gap recorded at the project's design log (not part of this release)). Each test asserts that it RAN
(rule C6), and each negative carries a positive control in the same test (rule C1).

**RED.** Before its slice, each test is run on the unchanged tree and must fail for the stated
reason: most fail on the `UNIMPLEMENTED` error and close 1011 that `frame_handlers.py` sends today;
routing tests fail on `E_TARGET_UNREACHABLE`. The failure text is recorded in the slice's
divergence entry.

| # | test | proves | mutant it must kill |
|---|---|---|---|
| T1 | HTTP invoke from C to T while T is connected: T gets exactly one `skill_invoke` whose `envelope` verifies with C's key; T acks, runs, returns a signed result; `GET /v1/calls/{id}` is `completed` with `result_payload` equal to T's signed payload | routing, invoke delivery, target frames, A3 | skip the step-9 remote branch; build the frame from the NOTIFY payload |
| T2 | the same with C over WS `skill_call`: C gets `skill_acked`, then `skill_result` with `status: completed`, once | caller registration, terminal delivery | drop the registry registration; register after the terminal |
| T3 | WS `skill_call` to the MEMORY authority: C gets `skill_acked` and the terminal frame | the registration-order hazard (4.7) | the "already terminal" branch removed |
| T4 | stream: T sends 5 chunks; C, subscribed before, receives them in order; a second caller socket subscribing after chunk 3 receives 1-3 replayed, then 4-5 live, then the terminal frame | replay plus live, subscribe to a running call | replay removed; subscribe-then-read order swapped |
| T5 | a peer X (not the target) sends `skill_ack`/`running`/`result`/`chunk` for T's call: refused with the SAME code and message as for a random call_id; the row is unchanged; T's own frames succeed (control) | 5.1, the oracle closed | `target_peer_id = me` removed from the WHERE; distinct messages |
| T6 | T sends a result whose signed `call_id` names another of its calls: refused, row unchanged | binding | binding check removed |
| T7 | a chunk with a duplicate `sequence`: refused; the first stays | UNIQUE | constraint dropped (fixture break, rule C3) |
| T8 | `skill_result` straight after `skill_ack`, with no `running`: refused `InvalidTransition`; then after `running` it succeeds | the state machine | status guard widened to `routed` |
| T9 | T suspended by SQL after connecting, with the revocation sweep interval raised so the close has not happened: T's `skill_ack`, `skill_running` and `skill_result` are refused and the row is unchanged; after the sweep T's socket closes with 4410; reactivated and reconnected, T is accepted (control). The same with T's bound key revoked: frames refused until the 4412 close | active and live-key clauses (4.7, R12) | either clause removed |
| T10 | C cancels over WS while T is `running`: C gets `skill_error ABANDONED`; T's later result is refused; a stranger's cancel of the same call gets the uniform refusal and the row is unchanged; the target's own cancel is refused too | cancel (F1) | `caller_peer_id = $me` removed from `cancel_by_caller`; the handler switched to `reads.cancel_to_abandoned` |
| T11 | T offline: call is `queued`; T connects before the deadline: T gets the invoke, the row becomes `routed` BEFORE the frame is read | offline, redelivery order | publish-before-update (2.12.3) |
| T12 | T offline past the deadline: the janitor job body times it out; C, subscribed, receives `skill_timeout` although the janitor ran on a different connection | trigger on every writer | trigger removed; app-level NOTIFY only in `route.py` |
| T13 | **two workers**: two `ConnectionManager` + `CallBridge` pairs in one test process; T on worker A, C on worker B; C's `skill_call` reaches T and T's result reaches C | cross-worker delivery | bridge delivers from payload; presence read from the local registry |
| T14 | forged `pg_notify('meshy_call', ...)` naming a real call routed to T: at most a duplicate invoke to T; naming a call of another peer: nothing to anyone else | pointer-only | frame built from payload |
| T15 | LISTEN connection killed and restored: a terminal committed while it was down still reaches the registered caller | recovery sweep | sweep removed |
| T16 | the WS path uses 036's one reader. With enforcement ON (the default once 036 lands), a `skill_call` re-signed with a USED nonce under a NEW call_id is refused over WS, and over HTTP (control of the same gate); with `MESHYCHAT_ENVELOPE_NONCE_ENFORCE=false` it is accepted. A byte-identical resend of an envelope that created a call is answered as a replay (`skill_acked`, no second row) in both modes, which is why the first version of this test could not fail (5.3) | 036 reaches WS | the handler passing a literal `False`; the handler building its own reader |
| T17 | 17 concurrent `skill_call`s on one socket: exactly one `SKILL_CALL_BUSY`, and a `skill_cancel` sent during them is answered promptly | back-pressure, no head-of-line blocking | semaphore removed; spawn removed |
| T18 | T's socket drops a `skill_invoke` (its queue forced to overflow before T reads): the re-offer sweep sends it again within one sweep interval, and T answers once | 4.6 re-offer sweep (F8) | sweep removed; loss not triggering it |
| T19 | the memory authority's peer connects over WS while an in-process call to it is running: it receives no `skill_invoke`, and a `skill_result` it sends for that call gets the uniform refusal; the in-process result stands | 4.4 exclusion | the authority predicate removed from redelivery or from the target statements |
| T20 | T connected with its presence row suppressed: an HTTP call goes `queued`, then T's worker's CallBridge routes it on the `queued` notification, and T answers (the safety net); a `hub_connections` insert from a request-context connection is refused (RLS) | 4.3 (F13) | safety net removed; RLS off |
| T21 | a remote result's row carries `result`, `status`, `error_code` and `error_message` exactly as T signed them (NULL when absent), checked with the fleet's own bound-field list (`wire.RESULT_BOUND_FIELDS`) against T's published key; a `failed` result with and without the error pair | 4.7 result fields (F7) | error fields taken from anywhere but the payload |
| T22 | chunks at sequence 0, 1 and 2, and a 300-chunk stream: a late subscriber's replay includes 0 and holds at most 200 frames, the newest; `sequence` 2147483648 is refused `SKILL_CHUNK_BAD` at parse, not as a database error | 4.7 replay (F9) | replay through `list_chunks(since=0)`; the i32 bound removed |
| T23 | a deadline passes while the shared `run_jobs` loop is held by a slow job body (a fake that sleeps): `run_call_worker`'s 5 s sweep times the call out within one of its ticks, and C receives `skill_timeout` | 4.10 (F5) | the worker's loop removed |
| T24 | a `dashboard_test` call to a remote target fails `E_UNVERIFIED_CALL` at step 9, and no `skill_invoke` is sent | 4.4 (F16) | the check removed |
| E1-E9 | egress: blocked literals are refused and public literals pass; a resolver returning public-then-loopback connects only to the validated public address (the connector records the dialed IP); a 302 is a failure and the recording server sees no second request; `HTTPS_PROXY` set, and the proxy sees nothing; userinfo host `expected.com@169.254.169.254` blocked; an empty resolution refused; a mixed public+private resolution refused | 4.11 | each blocklist arm removed; pin removed; redirect followed |
| W1-W9 | webhook: an offline T with a verified registration gets one POST to a loopback recorder (test hatch on); its JWT verifies against the served JWKS under the WEBHOOK kid with `aud`, `sub`, `exp`, `jti` and a matching `body_sha256`, and a one-byte body change fails; the call stays `queued` with a `webhook_fired` event; two workers claim the same attempt and exactly one POSTs; a crash after claim is re-sent by the sweep; `skill_calls.webhook_url` and `peers.invocation_delivery_webhook` are never contacted; an unverified registration is never rung, a correct challenge echo verifies it and a wrong one does not; two queued calls for one offline peer produce ONE due attempt, and a third within 60 s none; the doorbell JWT does not verify under the token key, and `authenticate_peer` refuses it as a bearer | 4.11 (F3, F11) | claim after POST; body hash omitted; per-call URL used; the token key signing; the partial unique index dropped; verification skipped |

**End-to-end instrument (rule C5).** After S6, run target T against a booted server with DPoP at
its production default. Call it over HTTP and over WS. It exercises ack, running, chunk and result
as a real client does, sends no nonce on results (5.3), numbers liveness chunks from 1, and signs
them with the caller's signed `call_id`. That equals the frame's `call_id` here, because step 4b
forces the two to match, so the binding rule (4.7) admits it. The run is also the proof required
before production use. The memory daemon is NOT used as a target: 4.4 keeps it idle.

---

## 7. Build order: small landable slices

Each slice is one branch and one intent (rule E9), carries its RED run and its mutation list in a
the project's design log (not part of this release) entry (number taken at merge), and leaves every earlier behaviour working.

| slice | content | lands when | depends on |
|---|---|---|---|
| **S1** | server frame builders; typed parsing of every skill client frame (`mode_override` domain, `sequence` as i32, field types); frames STILL refused | pure, tests only | none |
| **S2** | `hub_connections` table with RLS, insert/delete/UPSERT beat; no routing change | T13's presence half; T20's RLS half | S1 |
| **S3** | the two triggers plus a `CallBridge` skeleton (LISTEN, pointer parsing, counters, recovery and re-offer sweeps, no frames sent); T14's no-op half | the triggers can be proven alone | S2 |
| **S4** | target frames `skill_ack`, `skill_running`, `skill_result`, `skill_error` against rows a test inserts as `routed`, with the active and live-key clauses; T5, T6, T8, T9, T21 | no routing yet | S1; divergence 030 (on main) |
| **S5** | step 9 remote branch (`routed`/`queued`, and the `dashboard_test` refusal), invoke delivery through the CallBridge, redelivery on connect and on LISTEN restart, the re-offer sweep, and `run_call_worker` with the 5 s deadline sweep; T1, T11-T13, T15, T18-T20, T23, T24 | after S5, HTTP caller to WS target works end to end by polling | S2-S4 |
| **S6** | caller side: WS `skill_call` (spawned, semaphore, `enforce_from_settings()`), `CallRegistry`, terminal delivery through the one terminal mapping, `subscribe`/`unsubscribe call:`, `skill_cancel` through `cancel_by_caller`; T2, T3, T10, T16, T17, T19; then the C5 run | the frames are complete for non-streaming skills | S5; divergence 036 landed (Q13) |
| **S7** | `skill_chunk` (text, json), live delivery, replay (`sequence >= 0`, at most 200), trim at terminal; T4, T7, T22 | the A2A SSE stream picks the chunks up with no change | S6 |
| **S8** | `apps/core/egress.py`, pure plus the pinning connector; E1-E9; no caller | pure, no product change | none (can run in parallel) |
| **S9** | the webhook: `peer_invocation_webhooks` with proof of control, the registration route, `peer_webhook_attempts` with its partial unique index, step 9 inserting the `due` attempt, the doorbell in `run_call_worker`, the dedicated key and its JWKS entry; W1-W9 | last | S5, S8 |

The replay reader that was S0 is divergence 036 on `fin/ops-replay2`, built by another workflow.

Deferred, each with a recommendation in section 8: `file_ref` chunks, push-notification configs,
elicit/resume, telling the target about cancel and timeout.

Outside F23, listed so it is not lost: a client that verifies REMOTE results (4.7). It must take
the key from the signer's published keys, name the signer, and report a key fetch failure as
`E_SIGNATURE_KEY_UNAVAILABLE`, which is 034's rule for authors applied to targets.

---

## 8. Risks and open questions

### 8.1 Risks

| # | risk | evidence | mitigation |
|---|---|---|---|
| R1 | connection exhaustion: no pool; each spawned `skill_call` opens a connection, and 16 per socket times 8 sockets per peer is 128 per peer. The A2A SSE stream also holds a second connection up to 180 s | `apps/calls/views.py` and an internal project document (not part of this release) (unpooled per request); `max_connections` 500 [M] | a process-wide semaphore on concurrent pipeline threads (proposal 32), plus the per-socket connection for target frames (4.7) |
| R2 | one NOTIFY and one keyed read per chunk; a busy stream delays the call listener | [I]; volume stays low (159 chunks in the dev DB [M]) | a separate CallBridge thread (4.2); skip the read when no local subscriber holds the call |
| R3 | drop-oldest loses chunk frames to a slow caller without telling it, and a queue of 256 chunk frames at up to 512 KiB each is about 128 MiB per socket | `apps/wsgw/send_queue.py:89-111`;  | recovery by sequence gap plus re-subscribe or `GET .../chunks`; propose a BYTE budget per queue in P-001's follow-up (the project's design log (not part of this release)), not in F23 |
| R4 | the deadline sweep runs late: it shares one sequential loop with the link job (032, ON by decision 15), so a timeout can land about two minutes after its deadline | `apps/jobs/runner.py:55-95`, `apps/jobs/registry.py:133-148`, `run_jobs.py:88-90` | `run_call_worker` runs it every 5 s (4.10); a stale-in-flight health signal |
| R5 | a crashed worker's `hub_connections` rows say "online" for up to 30 s, three missed 10 s beats | 4.3 | reconnect redelivery, the re-offer sweep, or the deadline; the Redis TTL is 120 s |
| R6 | an in-process memory call is terminal before registration | 4.7 | T3 |
| R7 | `lesson` mode violates the live CHECK | 2.12.7 | refused at parse |
| R8 | TLS to a pinned IP: SNI and hostname verification must use the NAME | 4.11 | E-tests against a local TLS server with a certificate for a test name |
| R9 | webhook args versus MUST-8 | 4.11 | pointer-only body (Q2) |
| R10 | parallel branches take the same `sql_meshychat` and divergence numbers | [M] main holds 011 to 013; divergence 035 and 036 are reserved for `fin/ops-livez` and `fin/ops-replay2` | numbers assigned at merge (E5) |
| R11 | shared dev database: other workflows run tests against `meshy_django` concurrently | [M] 150 `pending` ws rows with 150 distinct targets. Two tests insert exactly that shape (`tests/test_rls_isolation.py:91`, `tests/test_write_side_ownership.py:119`), and no application code uses `TRANSPORT_WS` (`apps/calls/pipeline.py:75` is its only mention) | every test keys its assertions on rows and peers it created; no global counts |
| R12 | the revocation window: a suspension is SQL and sends no NOTIFY, so 030 closes the socket only at the next sweep, up to 10 s later, and not while the LISTEN connection is down | `apps/wsgw/constants.py:101-106`, `apps/wsgw/mesh_notify.py:703-722` | the active and live-key clauses in every target statement (4.7); T9 |
| R13 | concurrent writers on the same database can route calls twice, ring unsigned doorbells, and weaken frame validation | 5.5 | one writer per database at any switch (Q16); registrations in a dedicated table |
| R14 | RAM on HQD: each new long-running process costs about what the job ticker does | cutover-list 7a (the ticker at 284 MB private, daphne 344 MB) | one `run_call_worker`, not a dispatcher plus a janitor loop |
| R15 | a peer online only as a mesh listener looks online to step 9, and its calls sit `routed` until the deadline if it does not handle skill frames | [I]; whether such a peer registers a skill is unmeasured | the janitor's message names `routed`; nothing else in F23 |

### 8.2 Open questions, each with a recommendation

The workflow request asked for questions to be put to the operator with the question tool. No
such tool was available to the session that wrote this document, nor to the critique session of
2026-09-24, so each question is listed here with a recommendation, for the operator to answer.
One operator answer reached this work meanwhile. The cutover list's decision 15 (2026-09-24)
ships divergence 032's link job ON, as hints. This design builds on it: the link job stays where
it is, and the deadline sweep moves to its own loop (4.10).

| # | question | recommendation | why |
|---|---|---|---|
| Q1 | A call to an offline target: queue it until its deadline, or fail it at once (`E_TARGET_UNREACHABLE`, today)? And should a sync HTTP invoke wait up to 30 s for a remote result? | **Queue until the deadline; keep the sync wait at 0 and let the client poll.** | Agents reconnect on JWT refresh; the janitor message says "while the call was queued" |
| Q2 | Webhook body: the full `skill_invoke` with the caller's args, or a pointer-only doorbell? | **Pointer-only.** | No args to a public host; keeps MUST-8 intact; no client consumes the full body today [M] |
| Q3 | Webhook authentication: an EdDSA JWT with a body hash under a DEDICATED per-host key, under the token-minting key, or an HMAC over a shared secret (the unimplemented migration-009 idea)? | **EdDSA JWT, a dedicated per-host key, never copied.** | Under the token key a doorbell is a valid token that only the `aud` rule refuses. A per-host key adds nothing to the secrets copied at cutover, because a doorbell lives 120 s |
| Q4 | Where the 5 s deadline sweep and the webhook sender run: one new `run_call_worker`, two new processes, or inside `run_jobs --loop`? | **One new `run_call_worker` for both.** | The shared loop can hold one tick over a minute (the link job). HQD's RAM is tight, and each process costs about 0.3 GB |
| Q5 | Does the replay ruling extend to `skill.result`/`skill.chunk` envelopes? | **No: `skill.call` only, plus the call_id/sequence binding.** | Replays are already inert; the inference daemon signs no nonce on results [M] |
| Q6 | A signed `skill.result` with an unrecognised `status`: coerce to `completed` or refuse? | **Refuse `SKILL_RESULT_BAD`.** | Rule E2; both daemons send only completed or failed |
| Q7 | The call-existence oracle has three doors: the WS frames' error text, step 2's idempotency conflict, and HTTP 404 versus 403. Close only the first, or all three? | **Only the WS door; document the other two.** | The WS door is cheap. Closing step 2's door means scoping call_ids per caller, which is a schema change. A call_id can be guessed only when its caller picks a guessable one, and the client uses uuid4 |
| Q8 | Cross-worker presence table now, or single-worker only? | **The table (S2).** | The presence table must work when a second worker starts |
| Q9 | A registration route for the invocation webhook? | **Yes: `PUT`/`DELETE /v1/peers/me/invocation-webhook`, https on port 443, DPoP, proof of control before the first ring, stored in a new table.** | Without it the feature needs SQL, as the only writer is a test. Without the proof, a peer can aim the doorbell at a third party |
| Q10 | `skill_calls.webhook_url` from the invoke body: keep ignoring, or refuse with 400? | **Keep ignoring for now; refuse in a later slice once clients are checked.** | An SSRF vector if ever wired |
| Q11 | `file_ref` chunks, push-notification configs, elicit/resume: in F23? | **No, all three deferred.** | `file_ref` needs `apps/files` ownership checks; push configs have no route or users (0 rows on the dev database [M]); elicit has no producer (2.10) |
| Q12 | Tell the target when its call is cancelled or times out (a new server frame)? | **Not in F23; revisit if the inference daemon is measured burning GPU on abandoned calls.** | A new frame is a wire change for every client |
| Q13 | Should the WS `skill_call` slice (S6) wait for divergence 036 (`fin/ops-replay2`) to land? (This replaces "S0 as its own slice": 036 is that slice.) | **Yes.** | 036 makes the nonce switch a required argument. S6 built first would pass the old three-reader value and be changed again |
| Q14 | Keep `hub_connections` now that the `queued` safety net makes presence an optimisation? | **Keep it (S2).** | Without it every remote call passes through `queued`. Its events and the janitor's "passed while queued" text would stop meaning "the target was offline", and the doorbell would have to wait out a grace period |
| Q15 | `GET /v1/calls/{id}/chunks` defaults `since=0`, which skips a chunk at sequence 0 (the bound, 2.12.11). Keep the wire default and fix only the WS replay, or change the default? | **Keep the default; fix the WS replay; document `since=-1`.** | The default is the wire contract. The SDK leaves sequence numbering to the target, so refusing sequence 0 would break a valid target |
| Q16 | At any production switch: one writer on one database, and the memory daemon stopped? | **Yes to both.** | Two writers route one row twice. The daemon would be connected and idle |
| Q17 | A re-offer sweep every 10 s that sends an unacknowledged `skill_invoke` again? | **Yes.** | Without it a frame lost to the drop-oldest queue, or to a failed loop hop, leaves a connected target silent until the deadline. Targets already dedupe on call_id |

---

## 9. Measured facts this design rests on (index)

1. Every skill frame and every `call:` subscribe is refused today: `apps/wsgw/frame_handlers.py:39-65`,
   `:94-98`, `:121-122`; consumer close 1011, `consumer.py:449-458`.
2. Step 9 fails every non-memory target: `apps/calls/route.py:118-123`.
3. Nonce enforcement defaults OFF on main in three separate readers: `pipeline.py:88`,
   `views.py:81-82`, `a2a/views.py:520-521`; no settings entry; five fixtures pin it off.
   Divergence 036 on `fin/ops-replay2` (`d447af0`) replaces all three with one reader, ON.
4. The frame set and state machine are defined in sections 4.2 and 4.7.
5. The invocation webhook is unsigned and has no writer outside a test (2.11).
6. The egress guard validates URLs before sending (4.11).
7. Nothing produces an elicit: `grep -rln report_elicit` (2.10).
8. The inference daemon signs no nonce on results; the memory daemon does.
9. Channels dispatches one message at a time per consumer: `channels/utils.py:43-51`.
10. Django gives each request its own `ThreadSensitiveContext`: `django/core/handlers/asgi.py:172`.
11. Dev database, at `sql_meshychat` 004: 0 webhooks, 0 push configs, 0 in-flight rows with a
    future deadline, 0 chunks at sequence 0, no triggers on the call tables,
    `UNIQUE (call_id, sequence)` present, the mode CHECK without `lesson` (3.2). None of it is a
    production figure.
12. `run_jobs --loop` sleeps a fixed 30 s by default after each tick: `run_jobs.py:37-38`,
    `:88-90`. The README runbook schedules it. The tick runs every job in one sequence,
    `runner.py:55-95`, divergence 032's link job included (`registry.py:133-148`).
13. A suspended target is refused at step 5, before routing: `apps/calls/authorize.py:93-103`.
14. Python's `ipaddress.is_global` admits `fec0::1`, `::7f00:1` and `64:ff9b::7f00:1` on the
    repository's interpreter (3.14.3), so it cannot be the SSRF guard (4.11).
15. No application file mentions `invocation_delivery_webhook` or `X-Meshy-Call-Id`. Re-run
    2026-09-24: zero hits for the names, while the positive control found `skill_invoke`.
16. `reads.cancel_to_abandoned` has no caller predicate (`apps/calls/reads.py:317-320`); the HTTP
    view checks the caller itself (`apps/calls/views.py:603-607`).
17. The result signer uses the token-minting key (`apps/calls/result_signing.py:80-88`). The one
    JWT verifier checks no `typ` and no `aud` (`apps/authapi/tokens.py:237-250`); the extractors
    require `aud == "peer"` (`apps/peers/auth.py:132`, `apps/wsgw/auth.py:168-173`).
18. Divergence 030 closes revoked sockets with 4410, 4411 and 4412, and finds a suspension by a
    sweep every 10 s: `apps/wsgw/revocation.py:12-24`, `apps/wsgw/constants.py:101-106`,
    `:137-139`.
19. Step 2 answers a byte-identical resend before gate 4c: `apps/calls/pipeline.py:183-192`.
20. `sql_meshychat/012:83-88` binds `skill_calls` UPDATE and DELETE to `server_internal`.
21. The fleet's result verifier looks only in the server JWKS and binds five row fields:
    `tools/meshy_node/wire.py:233`, `:237-275`.

**What is inferred, not measured,** and flagged where it is used: the chunk-rate cost of one
connection per statement (4.7), the value of a separate CallBridge thread (4.2), that a
forgotten `pg_notify` call is the likely failure of application-level notification (4.5),
cross-worker placement of an invoke and its target (4.3), `http.client`'s proxy and redirect
behaviour (4.11), the cleartext-bearer argument for https-only (4.11), how late the deadline
sweep can run behind the link job (4.10), the third-party reflection that proof of control
prevents (4.11), the effect of concurrent writers on one database (5.5), and whether a mesh-only
listener ever registers a skill (R15).

---

## 10. Critique log

### 10.1 How the critique was run

An adversarial critique on 2026-09-24, against `origin/main` at 0b6767f. The workflow brief
reports main's suite at 1736 passed and 5 skipped; this critique did not re-run it. Every
repository file below was read with `git show origin/main:<path>`, never from this branch's older
tree. The cited implementation evidence was re-opened. The dev database was read for catalog facts
and counts only: migration versions, policies, triggers, RLS flags and counts. No row content was
read. No test was run: the change is a document.

### 10.2 What was kept from the designer's uncommitted revision

The revision was found uncommitted in the worktree and is committed unchanged as the commit
before this one. Each citation it added was re-opened and held, including its dev-database
counts for `skills.max_runtime_ms` (60 000 at most; 6,562 NULL of 6,604, re-read 2026-09-24) and
`meshy_memory_daemon.py:1012-1013` (the daemon sends `skill_ack` then `skill_running`). Every
design change in it is kept:

- 2.11: the HMAC in the migration comment is the CALLER's, and the invoke body carries none.
- 2.12.8: the unknown-status coercion is a documented choice.
- 2.12.11: chunk replay skips sequence 0 (`apps/calls/reads.py:237-244`).
- 2.12.12: the egress client sets no `no_proxy()`. It stays [I].
- 2.12.13: the existence oracle has additional doors in `apps/calls/repository.py:126-132` and
  `apps/calls/views.py:248-258`.
- 3.1: `cancel_to_abandoned` has no caller predicate (`reads.py:317-320`, `views.py:603-607`).
- 3.3.9 and 3.3.10: the cancel predicate and a separate webhook key are needed.
- 4.3: a presence row only after the CallBridge listens, the UPSERT beat, the 30 s TTL, and the
  `queued` safety net.
- 4.4: the memory authority is excluded from every delivery path.
- 4.6: the recovery sweep keeps a per-registration chunk high-water mark.

Three things in the revision were wrong or unfinished, and are fixed below. Its new row in 3.2,
"`skill_calls_internal_update`: `USING true`", described the stale dev database, not main (F6).
It numbered the replay reader "divergence 034", which is 036 (F18). It pointed at a section 10,
an R12 and a Q14 that did not exist, and it left 4.7, 4.11 and 5.1 contradicting its own
summary (F1, F3, F11, F19).

### 10.3 Citations re-opened (at least ten, as the brief required)

| citation | described correctly? | still true on main? |
|---|---|---|
| `apps/wsgw/frame_handlers.py:39-65`, `:94-98`, `:121-122` | yes | yes, the file is unchanged |
| `apps/calls/route.py:118-123`, `:125-131`, `:273-295` | yes | yes, unchanged |
| `apps/calls/reads.py:237-244`, `:315-320`; `apps/calls/views.py:603-607`, `:248-258` | yes | yes, unchanged |
| `apps/wsgw/consumer.py:245`, `:290-298`, `:395-408`, `:382-391` | yes at c5457ac | MOVED to `:296`, `:345-349`, `:462-475`, `:449-458`. The "revocation listener" named as a gap is now built (F4) |
| `apps/wsgw/mesh_notify.py:21-25`; `:367-570` | yes | `:21-25` yes; the bridge MOVED to `:591-909` and now also LISTENs for revocation |
| `apps/calls/pipeline.py:87`, `:220-225`, `:232` | yes | MOVED by one line (`:88`, `:221-226`, `:233`); still OFF by default |
| `apps/calls/repository.py:36`, `:39`, `:117-123`, `:136-148` | yes | MOVED to `:42`, `:45`, `:126-132`, `:142-157` |
| `apps/calls/result_signing.py:68-88` | the lines, yes; its use for webhooks, no: it is the token-minting key (F3) | yes |
| `apps/authapi/views.py:633-645` | yes, it publishes only the current key | yes |
| `apps/jobs/registry.py:102-111`; the project's design log (not part of this release) | the 30 s tick, yes; "nothing schedules it", no: the README runbook does (F5) | MOVED to `:103-115` and `:1319-1320` |
| the project's design log (not part of this release) ("Revocation still has no listener") | it was true at c5457ac | NO: divergence 030 superseded it (F4); now at `:1913-1914` |
| `channels/utils.py:43-51`, `django/core/handlers/asgi.py:172` | yes | yes (.venv: channels 4.3.2, Django 6.1.1) |
| `apps/wsgw/constants.py:15`, `:30`, `:34-38`, `:59`, `:67-68`; `:112` | yes | yes; `:112` MOVED to `:131` |
| `apps/calls/envelope.py:50-54`; `:121-202`, `:149-158`, `:160-176` | yes | `:50-54` yes; the rest MOVED to `:157-238`, `:185-194`, `:196-212` |
| `apps/wsgw/db.py:63-66`, `:70`, `:84`, `:105` | yes | MOVED to `:67-70`, `:74`, `:88`, `:109` |
| 3.2's `skill_calls` UPDATE policy "`USING true`" | yes, for `meshy_django` | NO: `sql_meshychat/012:83-88` (F6) |

Every moved line number is corrected in place above.

### 10.4 Findings

Severity: HIGH means the design as written would ship a hole or a test that cannot fail.
MEDIUM means wrong behaviour or a false guarantee under a reachable condition. LOW means
citation, consistency or hygiene.

| # | sev | finding | what changed |
|---|---|---|---|
| F1 | HIGH | Confused deputy in cancel. 4.7 and 5.1 still sent WS `skill_cancel` through `reads.cancel_to_abandoned` and credited it with a "caller check". It has none (`reads.py:317-320`); the HTTP view checks the caller (`views.py:603-607`). The revision had found this in 3.1 and 3.3.9, but never changed the mechanism | a new `cancel_by_caller` statement with `caller_peer_id = $me` and the active clause in its `WHERE`; 4.7, 5.1 and T10 (with its mutant) changed |
| F2 | HIGH | T16 could not fail. It replayed a byte-identical `skill_call`. Step 2 (`pipeline.py:183-192`) answers that from the row before gate 4c, ON or OFF, as 036's own docstring says. S0 also duplicated 036 | T16 rewritten: a used nonce under a new call_id; S0 removed; S6 depends on 036; 5.3 rewritten; Q13 replaced |
| F3 | HIGH | Key custody contradiction. The summary said "a dedicated key", but 4.11 signed with `server_result_signer()`, the token-minting key (`result_signing.py:80-88`). The one verifier checks neither `typ` nor `aud` (`tokens.py:237-250`), so only the extractors' `aud == "peer"` would stand between a doorbell and a token | a dedicated Ed25519 key per host, never copied, published under its own kid; claims reconciled (`iss`, `sub`, `aud`, `iat`, `exp`, `jti`, `body_sha256`); W1-W9 prove the token key never signs a doorbell; Q3 changed |
| F4 | HIGH | Stale premise. "Revocation has no listener, so a suspended peer keeps its socket" (4.7) and "the listener `fin/sec-code` adds (R12)" (summary) predate divergence 030, which closes sockets with 4410, 4411 and 4412. A key revocation was not covered at all: `skill_ack`, `skill_running` and `skill_error` carry no signature | the active clause kept for 030's sweep window, with the reason restated (a suspension is SQL and sends no NOTIFY; `constants.py:101-106`); a live-bound-key clause added; R12 written; T9 extended |
| F5 | MEDIUM | Janitor cadence. "Nothing schedules the janitor" was stale: the README runbook schedules `run_jobs --loop --interval 30`, and did at c5457ac. The real hazard is that `run_due` is sequential (`runner.py:55-95`) and the loop sleeps after the tick. Divergence 032's link job, kept ON by cutover decision 15, can hold one tick 45 + 20 s (`registry.py:133-148`), so a timeout can land about two minutes late | the deadline sweep at 5 s in `run_call_worker`'s own loop; the link job untouched; R4 rewritten; T23 added |
| F6 | MEDIUM | Measured on the wrong schema. 3.2 describes `meshy_django`, which is at `sql_meshychat` 004, nine behind main. The revision's policy row was false on main. The dev counts were also used as if they described the world ("0 peers have one set") | 3.2 labelled as a dev database at 004; the policy row replaced by 012's definitions; the production counts moved into 5.5 as a measurement owed before S9 is enabled |
| F7 | MEDIUM | The result contract toward callers was unspecified. The fleet's verifier binds status, call_id, result, error_code and error_message to the row, and checks only the SERVER JWKS (`wire.py:233-275`). A remote result is signed by the target | the row's fields are copied from the signed payload, as the `extract_terminal` does; divergence 034's signer rule applied to targets; the client change listed outside F23; T21 added |
| F8 | MEDIUM | Overstated guarantee. 4.9 promised at-least-once `skill_invoke` while connected, but a frame lost to the drop-oldest queue or to a timed-out loop hop is re-offered by nothing until a reconnect | a 10 s re-offer sweep, also triggered by a queue loss (4.6); 4.9 amended; T18; Q17 |
| F9 | MEDIUM | Unbounded and lossy replay. Subscribe replay was bounded only once terminal, "from sequence 0" read through a `>` query that skips 0, and `sequence` had no upper bound before the int4 column | replay `sequence >= 0`, at most the newest 200; the i32 bound at parse; T22; Q15 |
| F10 | MEDIUM | Concurrent writers on one schema can route one row twice, ring an unsigned legacy-column doorbell, and weaken frame validation | new section 5.5; registrations in a dedicated table; the one-writer rule and stopping the memory daemon at any switch (Q16); R13 |
| F11 | MEDIUM | The webhook mechanism was missing or self-contradictory. Proof of control and "one doorbell outstanding" were promised with no mechanism. Two table names were used. `UNIQUE (call_id, id)` bounded nothing. A 2xx flipped `queued` to `routed` in 4.11, while the 4.2 diagram said `webhook_fired` only | `peer_invocation_webhooks` with a challenge echo run by the worker; `peer_webhook_attempts` with a partial unique index and a 60 s floor; a 2xx records `webhook_fired` only, so `routed` keeps one meaning; Q9 changed |
| F12 | MEDIUM | RAM on HQD. The design added one process, and F5 adds a second loop, on a 10 GiB box dedicated to the mesh, whose job ticker alone is 284 MB private (cutover-list 7a) | one `run_call_worker` for both; R14; Q4 changed |
| F13 | LOW | `hub_connections` had no RLS, so any `meshy_app` connection could fake presence and strand calls in `routed` | RLS on, every access under `server_internal`; T20 |
| F14 | LOW | Citation drift: line numbers into every file that changed since c5457ac were off (10.3), and main now has five fixtures pinning the nonce off, not four | all corrected in place |
| F15 | LOW | The design did not build on main's own patterns: 033's `by_key` (a decision was needed), 031's one-decision rule, and LiveBridge's sweep-before-listening | `skill_invoke` goes to every socket, explicitly; one terminal mapping for WS and HTTP; the CallBridge sweeps before it reports listening |
| F16 | LOW | Missing failure modes: a `dashboard_test` call to a remote target would deliver a null signature; a mesh-only listener looks online to step 9 | the `E_UNVERIFIED_CALL` refusal at step 9 (T24); R15 |
| F17 | LOW | Q7 recommended "close the oracle" after the revision had shown that closing one door closes nothing | recommendation changed: close the WS door only, and document the other two |
| F18 | LOW | Numbering: "divergence 034" for the replay reader is 036; "011 and 013 on other branches" is now main's 011 to 013; 035 is reserved for ops-livez | corrected in 1, 4.3, 5.3, 7 and R10 |
| F19 | LOW | The revision was unfinished: dangling section 10, R12 and Q14; R5 still said 120 s; the 4.2 diagram said a 30 s heartbeat where 4.3 says 10 s | all written or corrected |

### 10.5 Not changed, and why

- The frame set, the one pipeline, the pointer-only notifications, the trigger choice, the
  binding rule, the refusal of an unknown signed status (Q6), pointer-only doorbells (Q2) and
  the egress module's blocklist. None of main's changes since c5457ac touches them. The
  blocklist and `is_global` measurements were not re-run.
- The link job (divergence 032). The operator's decision 15 keeps it ON as hints. This design
  moves the deadline sweep out of its loop and does not touch the job.
- F23 is code in this repository only. Its operational constraints are the one-writer rule (5.5)
  and HQD's RAM (R14).

### 10.6 Open questions left for the operator

These are Q1 to Q17 in 8.2, each with a recommendation. The ones the critique added or changed
are Q3, Q4, Q7, Q9, Q13 and Q14 to Q17. No question tool was available to this session.
