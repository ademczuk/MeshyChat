# Task leases: pull-based work for agent fleets, version 1 (design)

memory-bxxxxx, 2026-11-10. Status: read by the backend owner on 2026-11-10 (sections 7 and 9 decided, additions folded in). Nothing here changes the server. The client-side adapter
for a hosted issue board is already built (section 6); the server module in section 7 is a proposal, and every
server-side step waits for the backend owner's agreement and, on production, the operator's go.

Origin: the operator's two choices of 2026-11-08, "Harden claims, no takeover" for the board in use that week, and
"Design PR to kxxx first" for the MeshyChat path. This is that design PR.

## 1. What this is

A fleet of agents pulls work instead of being assigned it. Each agent looks at a queue, takes an item, does it, and
reports. The hard part is not the taking. It is what happens after: is the item still being worked, who may hand
the result in, and what stops two agents landing the same work.

This document gives one record (a claim), one state machine, one acceptance rule, and a plan for moving the
rules from a board adapter into a MeshyChat `tasks` module that a release can ship without naming any company, board
or seat.

The one rule above all: **a claim says who holds an item. It never proves the holder is still working, and on its own
it never makes a result acceptable.**

## 2. Measured: the first two hours of the pull model on a hosted board (2026-11-08)

- No item was worked twice. 15 claims (9 claim documents, 2 re-claims, 4 row claims), no real 409, no duplicate pull
  request. It held partly by luck: two agents were pointed at the same row 32 s apart, and the second never wrote
  because it was reading an older revision where "row 2" meant a different item.
- Nothing expired a claim. No lease length was written anywhere (one instruction said 2 h, a later one 6 h), and no
  field was ever read for liveness. A dead agent kept its item until a person noticed.
- Claim state lived in three places that already disagreed in five spots: the claim document, the queue table, and
  the issue description.
- All agents on one workstation wrote as the same board user, so a claim proved the write was fresh, not who made it.
- The queue document was a hot spot (10 revisions in 37 minutes, row numbers shifting), and one claim could cover
  four issues, so opening the other three showed no claim.
- The work (a pull request) was not linked from the claim.
- Idle checks were nudge messages from the coordinator: 14 in the first stretch. That is the spam pattern to design
  out, not to tune.
- A later audit of the whole pull window (17:23 to 19:35, 132 minutes) found the median wait from queue to claim was
  2.05 minutes with no HTTP 409, and nudges fell from 14.0 an hour to 1.4. So claiming is cheap and safe at this size;
  the weak spots are expiry, identity and acceptance.

## 3. Principles

1. One writer wins. Claim, renew and every other change is a compare-and-swap from the revision just read. A 409 ends
   the operation and says whether the generation moved. Never retry onto a newer generation.
2. A fencing number. `generation` goes up by exactly one on a claim and on nothing else, and travels with the result.
   A result carrying an older generation is refused. The fence moves only when someone makes a new claim. Until the
   queue owner reopens an expired item, the exact head the holder registered can still land, on purpose: a pull request
   waiting in a gate queue can outlive its lease. Any other head, or a result from an older generation, cannot.
3. Time comes from the store, never from a participant. The board gives each revision a `createdAt`; a database gives
   `now()`. A typed timestamp in a body is advisory at most.
4. Renewal is a write, not a message. A holder renews by writing; a failed renewal tells it to stop. Nobody sends
   liveness mail.
5. Expiry is a review state, not a handover. An expired claim is not reassigned automatically. A person (the queue
   owner) reopens it or records it done. Automatic takeover waits until the merge step can refuse stale work.
6. Identity is a signing key plus a per-session nonce, never a display label. Renew, release and done must come from
   the same holder and the same session.
7. One item per claim and per pull request, until a reservation protocol exists for covering several.

## 4. The record and its states

```json
{
  "schema": "claim.v2",
  "item": "<item id>",
  "state": "open | claimed | released | expired | done",
  "holder": "<seat name>",
  "holder_session": "<session id>",
  "generation": 0,
  "claimed_at": "<store time of the claim revision, read-only>",
  "renewed_at": "<store time of the holder's latest revision, read-only>",
  "lease_seconds": 7200,
  "work_ref": "<pull request URL or null>",
  "work_head": "<the pull request's head SHA when registered, or null>",
  "attempts": 0,
  "note": "<short free text>"
}
```

`claimed_at` and `renewed_at` are never typed by a participant: on the board they are the `createdAt` of the claim
revision and of the holder's latest revision, read back after the write; on a server they are `now()` in the
transaction. Expiry is `renewed_at + lease_seconds`.

Claim records are never deleted and the adapter refuses to recreate one. If the record or any revision of its history
cannot be read, the adapter stops and changes nothing; a person rebuilds it by hand.

### 4a. claim.v3 (13 Nov 2026): the record people and seats now share

The claim.v2 record above is superseded for writing by `claim.v3`. It is the one body that the person-facing claim
skill and the board agents' claim tool (`tools/meshy_node/board_claim.py`, #632) both write and read, and that the
inactivity release job (claim-release r6.2) reads. The native `tasks` module (section 7) implements v3, not v2.

**Status: target contract until #632 merges.** Before #632, `board_claim.py` on main still sets `SCHEMA = "claim.v2"`,
its `parse()` refuses a `claim.v3` body and its writes are v2, so the board commands on main cannot read or renew a
v3 record. This section describes what holds once #632 is on main; it merges with #632 or after it, never before.

- **21 required fields:** `schema` ("claim.v3"), `item`, `state`, `taker_kind`, `holder`, `seat_name`,
  `board_user_id`, `holder_session`, `generation`, `attempts`, `briefRevision`, `lease_seconds`, `firstClaimedAt`,
  `expiresAt`, `releasedAt`, `release_reason`, `handedBackAt`, `work_doc`, `work_doc_revision`, `work_ref` and
  `work_head`. The optional fields are `note`, plus `reserved_for` and `reserved_by` only while `state` is
  "reserved". For the skill, claim-release and the native `tasks` module, any other body (v1, v2, the old
  five-field skill body, fenced or malformed JSON, a missing or unknown field, another item) is foreign: they report
  it and write nothing to it. The one exception is `board_claim.py` (#632). It reads a legacy SEAT record (v1, v2,
  including a single-fence v1) and upgrades it to v3 on its next write. It still refuses the five-field skill body
  and any unknown body.
- **States:** open, reserved, claimed, released, handed_back, done, expired. **handed_back** is new: the holder's
  result is in (`work_doc` and `work_doc_revision` name it, and `handedBackAt` and `expiresAt` hold the same one
  hand-back time). The task then waits for a review that the board's own review decides; v3 records no "reviewed"
  state.
- **Takers:** in open and reserved, `taker_kind`, `holder`, `seat_name`, `board_user_id` and `holder_session` are
  all null. Otherwise `taker_kind` is "person" with `holder` "person:<board user id>" and `seat_name` null, or "seat"
  with `holder` "seat:<label>" and `seat_name` the same label. Activity and assignment match on `board_user_id`,
  never on the prefixed `holder`.
- **Null rules:** `releasedAt` and `release_reason` are set together on released and open, and are null on every
  other state. `handedBackAt` is set only on handed_back. `firstClaimedAt` is a Z timestamp in every state except
  open and reserved; a converted v2 record backfills it from the claim revision's server time (#632 P1).
- **One lease, by decision:** `lease_seconds` and `expiresAt` serve as both the deadline and the liveness signal
  (172800 s, or 48 h, for people; 7200 s or 600 s for seats and the dry-run project). Renew moves `expiresAt`.
  oxxxxxx's TKT-7908/1215 spec fixed this for the Monday dry run. A split into a fixed `due_at` and a short
  `heartbeat_expires_at` (cherry-pick P3.1) is deferred to claim.v4 and the server module.
- **Who releases a person's claim:** the holder (the skill), or the inactivity job once the claim's own lease has
  passed with no activity. The skill (patch 0013) also lets the holder release an unchanged claimed pair after
  `expiresAt`. `board_claim.py` never releases a person's claim, live or expired: its queue-owner release refuses
  every person claim (#632 P2-1).

## 5. Transitions

| change | from | who may | must be true first | writes |
|---|---|---|---|---|
| claim | open, released, none | any seat with the item's area | the seat holds fewer than 2 claims (stated in the contract; the adapter does not count them yet) | state claimed, holder, session, generation + 1, work_ref and work_head cleared, attempts + 1 |
| register | claimed | holder, current session | no work_ref yet, or the same one | work_ref, work_head = the pull request's head now |
| renew | claimed | holder, current session | lease not passed | a new revision, optional note |
| re-register | claimed | holder, current session | work_ref set | work_head = the new head after a pushed fix |
| adopt | claimed | same seat and box, new session | a handover note names the item | holder_session and the session nonce (a fresh one, returned once; the old one stops working); work_head cleared |
| release | claimed | holder, or the queue owner | none | state released, note why |
| expire | claimed | board: any reader (lazy); server: the sweeper only | renewed_at + lease passed | state expired, note "needs review" |
| reopen | expired | queue owner only | old pull request closed, merged, or adopted on purpose | state open |
| done | claimed | holder, current session | work_ref names the merged pull request, merged at the registered work_head | state done, merge sha |
| done | expired | queue owner only | the pull request at work_head was merged | state done, merge sha |
| done by document | claimed | holder, current session | no work_ref; the named document on the same issue was last written after the claim began, by this box's board user | state done, work_doc and work_doc_revision |
| done by document | expired | queue owner only | no work_ref; the named document was last written after the claim began (the writer is not checked: the owner is on another box) | state done, work_doc and work_doc_revision |

No release from expired. No automatic takeover.

Done by document (`board-claim done --work-doc <key>`, #599) is for rows whose result is a board document, not a pull
request. Both of its checks use the board's own times and writer, never the client's clock. Where several seats write
as one board user, the writer check cannot tell them apart, so the time check is the real guard.

Why `adopt` does not raise the generation: it is the same seat on the same box taking over after a handover, and a
pull request waiting in a gate queue must stay mergeable. The old session is stopped another way: `adopt` clears
`work_head`, so anything the old session pushes fails the head check until the new session re-registers. On a server
with a session nonce (section 7, item 2) the old session's calls fail as well, because `adopt` replaces the stored
nonce hash in the same transaction.

### Acceptance (the merge check)

Right before merging, the merger reads the claim and merges only if all hold: the pull request is the claim's
`work_ref` and its current head equals `work_head`; its body has exactly one canonical line `claim: <item> gen <n>`
with the claim's generation; and the state is `claimed` or `done`, or `expired` with the same generation.

Limit, stated plainly: with a manual merge the claim can change between the read and the merge, and a store with no
"now" can let a check read a valid lease that ends a moment later. Both windows are short and visible in the revision
history. Closing them needs a merge-intent write inside the same transaction as the merge decision, which only the
server module (section 7) can offer.

### Renewal, honestly

An agent renews between turns, and a turn can run 60 minutes, so a 2 h lease leaves about one missed renewal of slack.
The 2 h figure comes from the observed pull-request cycle (about 1 to 2 h), not from a safety proof. An agent stuck in
one long turn expires, which is intended.

## 6. What is built today (client side, no server change)

- `tools/meshy_node/board_tasks.py`: the board client with bounded, paged reads.
- `tools/meshy_node/board_claim.py`: `meshy_node board-claim {take, renew, register, adopt, release, done, show, check}`.
  Every change is one compare-and-swap write checked by the returned body and a new revision id; the generation read at
  the start fences the whole command; lazy expiry runs on every read; the description line is a best-effort mirror
  written last and never lowers a newer generation.
- Tests in `tests/test_meshy_node_board_claim.py` and `tests/test_meshy_node_board_tasks.py` run on a fake board that
  copies the real board's measured behavior, and are labelled as simulated where they are. Real runs were made on
  scratch issues.
- Known gaps the adapter reports rather than hides: the description mirror can lose a concurrent human edit (the board
  has no conditional write), the lease is judged against the local clock, and holder and session are cooperative
  labels within one workstation.

## 7. Proposal: a `tasks` module in MeshyChat

Nothing in it names a company, a board or a seat. The board becomes one adapter.

1. **Home.** A `tasks` app on the server and `meshy_node task` subcommands. The adapter in section 6 keeps its CLI and
   becomes a thin client of the same state machine.
2. **One transaction per transition.** It checks state, holder and session, generation, expiry and permission, then
   updates the task and its lease together. The versioned write (expected head version, unique head index) is
   necessary but not enough alone: two writes in sequence can still make an illegal change.
   The server issues the session nonce: the claim transaction returns it, and register, renew, release and done must
   echo it. A second session under the same key then cannot act on the claim by accident, which the board adapter
   can only approximate with a per-session file. Only the nonce's hash is stored, and the nonce is returned once, at
   claim: a nonce readable from a list route would be a credential.
   `adopt` is the one other place a nonce is issued. In one transaction it replaces the stored hash with that of a
   fresh nonce and returns the fresh nonce, once, to the adopting session. The old nonce is never handed over: the
   adopting session could not otherwise renew, re-register, release or complete what it inherited, and passing the
   old one on would leave the stopped session holding a working credential. `adopt` is safe to repeat: if its
   response is lost, the adopting session has no nonce and the old one is dead, and a second `adopt` by the same
   seat and box replaces the hash again and returns another fresh nonce. The nonce swap is enforced by the adopt
   statement itself, not by the transition CHECK below, which sees adopt as claimed to claimed, the same as renew.
   The tables use FORCE row-level security with the application role, as the shared memory tables do, and ship a
   migration with its down script. The transition table of section 5 is also a CHECK on (from_state, to_state), so an
   illegal change fails in the database too.
3. **Permissions.** Separate grants to create, claim, renew, complete, cancel and administer. "Any granted peer may
   write the next version" is the biggest risk for an open release. A signature proves who wrote a change, not that
   they were allowed to make it. Each transition maps to one grant:

   | transition | grant |
   |---|---|
   | claim, adopt | `tasks.claim` |
   | register, re-register, renew | `tasks.renew` (the holder, with the nonce) |
   | release by the holder, done by the holder | `tasks.complete` (with the nonce) |
   | release by the queue owner, reopen, done from expired | `tasks.administer` |
   | expire | none: only the sweeper writes it |
   | create, cancel | `tasks.create`, `tasks.cancel` |

   "Queue owner" always means a holder of `tasks.administer`, never a seat name in code. A grant is checked the way
   the fleet's routes check `mesh:backlog:read` (`_holds_scope_now` in `apps/mesh/views.py`): the token's claim AND the
   peer row now. Either alone outlives a revocation or a later grant.
4. **Clock.** The database clock decides expiry, as `apps/jobs/leases.py` already does for jobs (a single conditional
   UPDATE, `now()` evaluated server-side, a displaced holder reported rather than absorbed).
5. **Sweeper.** One job in the existing registry (`apps/jobs/registry.py`, run by `run_jobs`) marks expired tasks, and
   it is the only writer of the expired state. A read derives expiry (`now()` past `renewed_at + lease_seconds`) and
   shows it without writing, so read load never becomes write load. (The board adapter writes the state on read because
   the board has no sweeper; that is a board limit, not the design.) It is one conditional UPDATE per tick, bounded (LIMIT, oldest first), and goes into `tests/test_query_cost_bounds.py`
   from its first PR: production runs one jobs ticker, and an unbounded sweep there is the 041 stall again. It
   reassigns nothing. Its notice to the old holder is best effort, one message per expiry, and correctness never
   depends on it arriving.
6. **Fencing at acceptance.** The server refuses a result whose generation or `work_head` no longer matches. Only after
   this exists can automatic reclaim be considered.
7. **Graph.** The graph is a projection of task state that catches up later, for search and links (blocks, parent,
   supersedes). The transactional state stays the authority. A task's thread is its MeshyChat thread, so the mail and
   the task stay one conversation.
8. **Adapters** own their conflict rules, so a board edit can never bring a completed task back.
9. **Limits.** Body size, renewal history, attempts per task, dependency depth.
10. **Discovery: how a seat learns there is work to claim, with no polling** (backend owner's proposal, 2026-11-10).
    The server keeps one revision counter of claimable work per eligibility set. The existing mesh nudge carries a
    `tasks` hint when that revision moves: one frame, no text, the pattern `directive` already uses. The postbox wake
    notice then gets one "work you can claim" line, from ONE read (`GET /v1/tasks?claimable=me`) made only when the hint
    moved and held for 2 minutes. Production load stays flat: one read per change, no loops.
11. **Eligibility.** A task names what it needs (skills or labels), and a seat may claim only what it is eligible for.
    Without that, the first idle seat takes everything, including work only one seat can do. The board's skill
    fields are the model.
12. **Rate.** Claim and renew go through `peer_limit.admit` like every route. Renew has a floor (at most one per
    lease/4), or a loop of renews becomes the new poll.
13. **Before a public release.** Decide whether the server, the client or both go public; move hard-coded rosters into
    configuration; deal with repository history and defaults, not only today's fixtures.

## 8. Phasing

1. Done: the board adapter and the written state contract (this document is its generic form).
2. Next: the first server PR, behind a flag and gated off HQD. It must hold discovery, eligibility and rate (items 10
   to 12), the tables and grants, and the sweeper, with the four scenarios the board run used (collision, pause past
   expiry, handover, stale view) as database tests.
3. Then the smaller release: a documented state contract and the module, not a general scheduler. Its date depends on
   which parts go public.

## 9. Decisions for the backend owner

Decided by the backend owner on 2026-11-10, with additions folded into section 7. Each line gives the choice.

1. Dedicated tables or a memory namespace with heads? **Dedicated tables.** The transition rules need a transaction;
   the graph reads a projection.
2. Where does the sweeper run? **`apps/jobs`.** It already has leases, a registry and a tick loop; a board routine
   stays an option for the board adapter only.
3. Server, client or both in the open release? **Client and the state contract first.** The server module needs the
   permission model reviewed.
4. Who administers (reopens expired work)? **A grant named `tasks.administer`, held by a configured owner,** not a seat
   name in code.
5. When may reclaim become automatic? **Not before fencing at acceptance (section 7, item 6) passes its tests.**

## 10. Not in scope

Priority ranking, dependency scheduling beyond a block link, cost accounting, and any push assignment. Pull stays
pull.

## 11. Prior art

Kubernetes Leases (holder, lease seconds, renew time, transitions); Kleppmann, "How to do distributed locking"
(fencing numbers); Amazon SQS visibility timeout and pgmq (attempt counts, a dead-letter state); Temporal, detecting
activity failures (heartbeat as a write, a failed heartbeat means stop); River's maintenance services (one sweeper that
itself holds a lease); the OpenAI Symphony specification (check what you hold first, then claim up to a slot cap,
capped exponential backoff, and the same backoff when nothing is claimable); skein (a fleet epoch card that stops
everything with one write); beads (dependency gating). Rejected: message-driven patrols and nudges, push assignment,
in-memory claim sets, a time-limited lock with no fencing number, and task-master (Commons Clause licence).

Our addition, which none of them has: the holder is the signing key of the claiming message plus a per-session nonce,
never a display label.
