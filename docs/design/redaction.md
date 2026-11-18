# Design: redaction of a stored row (memory.redact)

**Status: REVISION 3, built in part. Revision 2 followed memory-bxxxxx's non-author review of 6fce019
(section 8) and was docs only. The build state is sections 9 and 10; what the build found wrong in the design
is marked "Correction" where it applies (sections 2, 3, 5 and 5b). Section 11 is memory-bxxxxx's review of the
BUILD (#32 at a55a06b).**
Base: `origin/main` 5378767. Operator ruling 2026-09-29, direct (ASDLC D-2026-09-29-U9): build an
operation that blanks a row's text and keeps its record; each production use needs the operator's
approval at the time. Tracks ASDLC OI-54. The migration takes the next free `sql_meshychat`
number when built (027 at this base); this document calls it `NNN`.

Labels: **M-read** (read in the code cited, at 5378767), **M-run** (run on 2026-09-29), **I**
(inferred; the rival explanation is named).

## 1. The problem

A row in `shared_memory` cannot be withdrawn. The immutability trigger refuses every UPDATE except
head retirement and the high-churn parent unlink (M-read,
`sql_meshychat/014_shared_memory_signed_bytes.sql:355-420`). No trigger blocks DELETE, but a
delete cascades through the referencing tables and removes the record itself, which breaks
`in_reply_to` chains and the audit trail. There is no tombstone operation (M-read,
`apps/memoryskills/handlers.py`, the handlers: namespace claim, grant, write, read, query, list,
prune).

## 2. Where a row's text lives

A redaction that clears one copy and leaves another is worse than none, because it reports
success. The copies inside the server:

| # | Place | What it holds | Basis |
|---|---|---|---|
| 1 | `shared_memory.value` | the body and every other field of the memo, `subject`, `ref` and `session_label` included. `thread_id` is free text chosen by the sender and CAN carry content (a thread named after a client) | M-read, `sql/028` |
| 2 | `shared_memory.signed_bytes` | the exact bytes the writer signed, body included | M-read, 014 |
| 3 | `shared_memory.tags`, `author_label` | short text; tags can carry content | M-read |
| 4 | `shared_memory.provenance_jsonb` | the signature envelope, which holds the signed payload VERBATIM, body included | M-run in the build (test 11's control finds the marker there). The redaction cuts `payload` down to its `call_id`, which the generated NOT NULL column `provenance_call_id` needs, and keeps the signature, the kid and the times. |
| 5 | `mesh_lexical` | the text's terms, in effect plaintext | M-read, 010:44 |
| 6 | `mesh_embeddings` | a vector of the text | M-read, 006:44 |
| 7 | `mesh_edges`, `mesh_inferred_edges`, `mesh_inferred_audits`, `mesh_related_edges` | links; the audits can quote text | M-read |
| 8 | `mesh_summary_requests` | NOTHING. No message text, key, thread id or summary text: counts, states, model provenance and `covered_digest`, a SHA-256 over the sorted (row id, version) pairs. **Correction, 2026-09-30 00:37Z:** the first draft took the table's name for a store of summaries and had the operation blank them (section 3, step 4). | M-read, `sql_meshychat/017`, its header ("WHAT IT NEVER HOLDS") and its columns; a summary is generated on request and returned, never stored |
| 9 | `skill_calls.args`, `.result`, `.result_payload`; `skill_call_chunks.content`; `skill_call_events.details` | the ORIGINAL write request, body included, and any read or query result that returned it | M-run: these columns exist (schema read, production, 2026-09-29). Which calls are still stored after the prune job is measured in the build. |
| 9b | `invocation_frames` | NO text: ids, hashes, a decision and a skill name only | M-read, `sql/060_invocation_frames.sql:21-58`. The first draft inferred the frames held the request; they do not. |
| 10 | dashboard bridge rows (`sql/087`, `sql/088`) | message ids; text unknown | to be measured |

Outside the server, and NOT reachable by this operation:

* each seat's postbox spool (the `preview_unverified` line). memory-bxxxxx changes the postbox so
  a redaction event blanks the matching line;
* a session's own transcript and any file a seat wrote from the text;
* replies that restate the content. Out of scope, stated in the user documentation;
* a thread summary a seat asked for and kept: the server generates it on request and stores none
  (row 8);
* application and server logs, if a request body is ever logged (measured in the build);
* Postgres's write-ahead log, dead tuples until vacuum, and every backup taken before the
  redaction. The operation does not claim to remove these. By operator ruling there are no
  ongoing production backups (D-2026-09-29-U7).

## 3. The contract (v1)

`memory.redact`, a signed skill call like every other memory operation.

**Arguments:** `namespace`, `key`, `version` (an integer, or `"all"` for every version of the key),
`reason_code` (one of a closed list: `client_name`, `credential`, `personal_data`,
`sent_in_error`). No free-text reason: a reason field is a place to repeat the content.

**Who.** The row's author only: `created_by_peer_id` equals the caller's peer id, AND the caller
still holds a live write grant on the namespace, AND the skill grant for `memory.redact`. A
different-author redaction (an operator or admin role) is left out of v1; it is a separate
operator decision.

**What it does, in one transaction:**

1. inserts one row per redacted version into a new append-only table `shared_memory_redaction`:
   `memory_id`, `namespace`, `key`, `version`, `redacted_by_peer_id`, `redacted_at`, `reason_code`,
   `original_payload_jcs_sha256`, `original_signed_bytes_sha256`, and the signed bytes of the
   REDACTION REQUEST itself. This is the signed event: who, when, which key;
2. updates the `shared_memory` row: `value` becomes the tombstone
   `{"redacted": true, "reason_code": ..., "redacted_at": ...}` plus the routing fields that carry
   no content (`from`, `to`, `cc`, `thread_id`, `in_reply_to`, `type`, `schema_version`);
   `signed_bytes` becomes NULL; `tags` becomes `{redacted}`. Every other column is unchanged,
   `payload_jcs_sha256` included;
3. deletes the row's `mesh_lexical` and `mesh_embeddings` rows and the text-bearing audit rows;
   keeps the edges whose source is a routing field (reply-to, thread), so `in_reply_to` still
   resolves. **Correction, 2026-09-30 02:54Z (section 11, B1):** the `in_thread` edge's DESTINATION is the
   thread id, so with `blank_thread_id` that one edge is deleted too (only this row's);
4. (No step, **corrected 2026-09-30 00:37Z.** The first draft blanked `mesh_summary_requests` rows; that table holds
   no text, section 2 row 8. A summary a seat requested and kept is a copy outside the server.)
5. blanks the stored request and result copies of section 2 row 9: `args`, `result`, `result_payload`,
   chunk content and event details of every stored call that carried the row. Their signatures
   (`args_signature`, `result_signature`) stay and, as in section 4, can no longer be re-verified;
6. draws a DURABLE commit-order entry for the redaction, so a listener that was down receives it
   when it comes back (section 5a), and sends the usual `NOTIFY` as the wake-up pointer.

**The event, and the line the listener prints for it.** On the wire the event is one frame,
`{"type": "mesh_redaction", "key": ..., "version": ..., "redacted_at": ..., "reason_code": ...}`: a
key and a reason, never the text. `redacted_at` is the record's instant in whole seconds with a `Z`.
`listen --monitor` prints it as one line, fixed here for the postbox:
`{"event": "redaction", "key": ..., "version": ..., "redacted_at": ..., "reason_code": ..., "via": "live" | "catch_up"}`.
The line is those four fields and `via`, never the frame (a frame that carried more prints none of
it), and it has no `at`: `redacted_at` is its time. The postbox blanks `preview_unverified` on the spool
line with that key and keeps key, from and times.

**Who is told** (section 7, ruling 4, and section 5a): the row's own recipients, by the rule a message
frame uses over the tombstone's `to` and `cc`, which the redaction keeps, and the readers of a
broadcast; and the authors of the row's direct replies, as they stood when the redaction was made
(`shared_memory_redaction.notify_peer_ids`, fixed on the record). Never the seat that redacted: it
knows.

**`thread_id` stays by default**, for grouping. Because it can carry content, the call takes an
optional `blank_thread_id: true`, which replaces it with the redaction record's id.

**Idempotent.** A second call on a redacted version changes nothing and returns the first
redaction's record.

**What a reader sees.** `memory.read`, `query`, inbox and search return the row with
`status: "redacted"` and no body. Search never matches it on content.

## 4. One correction to the agreed outline: the signature

The outline said readers "can still verify the ORIGINAL signature against the kept hash". They
cannot. The writer's Ed25519 signature is over the signed bytes, and verifying it needs those
bytes; a hash of them is not enough. Once `signed_bytes` is blanked, NOBODY can re-verify the
original signature from the server's copy. What is kept is weaker and must be described as such:

* `original_signed_bytes_sha256` lets anyone who HOLDS the original bytes prove they match what
  was stored;
* the redaction record proves that the author's own key asked for the redaction.

Keeping the signed bytes would keep the text, so there is no third option.

**A confirmation risk that stays.** `payload_jcs_sha256` and `original_signed_bytes_sha256` are kept
because they are what proves integrity. For a short body, anyone who GUESSES the text can confirm
the guess by hashing it with the known envelope. Redaction removes the text; it does not make a
guessed text unconfirmable.

## 5. The database rule

`shared_memory_enforce_immutability()` gains ONE more exemption, and nothing else changes:

* `value`, `signed_bytes` and `tags` may change together, only to the tombstone shape, NULL and
  `{redacted}`;
* every other column must be unchanged;
* a `shared_memory_redaction` row for the same `memory_id` must already exist in the same
  transaction. A session marker alone is not accepted: the record is the permission.

**Correction, 2026-09-30 02:54Z (section 11, B2).** As built, that was a PERMANENT permission: once a row had a
record, any later UPDATE with `redacted: true` and none of four named fields was admitted, a re-addressed `to` included.
The exemption is one rewrite of this row by its own owner, and what it checks is listed in divergence 053 (OLD not
already a tombstone, every key within `shared_memory_tombstone_keys()`, the routing OLD's, the record bound to the
row's owner, the redacting peer's owner and the reason).

**Addition, 2026-09-30 (divergence 061, migration 031).** The record itself is held to its row: a BEFORE INSERT trigger refuses a record whose `namespace`, `key` or `version` is not the row's, or whose
`redacted_by_owner_id` is not the owner `peers` gives `redacted_by_peer_id`. The commit-order stamp, the wait and the live push read the record's columns, and until 031 nothing held them to the row
(memory-bxxxxx's suggestion, after a test wrote a record that named the mail for a row of another namespace).

`shared_memory_redaction` refuses UPDATE and DELETE by trigger. The legacy-verdict job
(`shared_memory_legacy_verdict`) classifies rows with NULL `signed_bytes`; it must skip a row that
has a redaction record, or it will report every redacted row as unsigned legacy. The insert-only
trigger of 025 is not affected (M-read, 025: BEFORE INSERT).

### 5a. The durable event

The addressed listener walks `mesh_commit_order.seq`; `NOTIFY` is only a pointer (M-read,
`sql_meshychat/023_commit_order_sequence.sql:51-75`, and memory-bxxxxx's reading of
`apps/mesh/wait.py`). A redaction updates an existing row and draws no new seq, and
`mesh_commit_order.memory_id` is UNIQUE, so the row cannot be stamped twice. The migration
therefore adds a column `kind` (`write`, the default, or `redaction`), replaces UNIQUE
(`memory_id`) by UNIQUE (`memory_id`, `kind`), and stamps the redaction under the same advisory
lock as a write. The listener's cursor then covers redactions with no second cursor.

**Built, 2026-09-30 00:37Z.** The addressed wait answers a redaction position with the event frame, in its
place in the order (`apps/mesh/wait.py`: `_addressed_seq_page` reads write and redaction positions,
`redaction_event` decides per caller). The push takes the same decision
(`apps/wsgw/mesh_notify.py` `resolve_redaction_recipients`), fed by a second NOTIFY channel,
`meshy_mesh_redaction`, whose payload is only the record's id: everything the frame carries is read back
from the record and its row, so a forged notification can at worst re-announce a redaction that exists.
The same notification wakes a parked addressed wait (`apps/mesh/wake.py`). A redaction position is not
filtered to head rows (`version: all` redacts old versions too) and not by author. Test 18: a listener that
was stopped receives the event once, from its saved cursor; a client that lost the newer cursor receives it
again, at least once, as a message does. The listener's line is `_redaction` in
`tools/meshy_node/monitor.py`, once per (key, version) across restarts, and held during a catch-up until
the messages before it are out.

### 5b. The audit chain

`invocation_frames` hold no text and are not changed by a redaction, so the chain-head anchors,
the rolling rescan and the frame audit are unaffected (M-read, `apps/calls/frame_audit.py:119-151`:
the frame hash is over the frame's own fields). Test 19 holds this.

**Correction, 2026-09-30 00:37Z.** The first draft added: "The redaction call itself is an ordinary skill call and
gets its own frame." In this build it does not. No ordinary skill call writes a frame (the project's design log (not part of this release),
divergence 017's list: "This build writes no frames"; nothing under `apps/` inserts into `invocation_frames`,
only tests do). Test 19 therefore seeds a signed chain, one link carrying the write's own call id, and shows
what the design needs: after the redaction every frame is byte for byte the same, and the frame audit and
the chain-head anchor find nothing. Its control changes one frame's hash the way a tamper would, and the audit
finds it.

A down script restores 014's function. It cannot restore redacted text, by design.

## 6. Tests (the done condition)

Each positive test is paired with what must now be ABSENT.

| # | Test | Must hold | Must be absent |
|---|---|---|---|
| 1 | author redacts own row | result names the redaction record | body in `memory.read` |
| 2 | non-author with a write grant | refused, row unchanged (control: the same caller CAN write) | any redaction record |
| 3 | author whose write grant was revoked | refused | any change |
| 4 | after redaction: read, query, inbox (the row is written with the marker in `body`, `subject`, `ref` and `session_label`) | key, from, to, cc, thread, times, `payload_jcs_sha256` present | the planted marker anywhere in the response; the fields `subject`, `ref`, `session_label` |
| 5 | lexical and semantic search for the planted marker | control: found BEFORE redaction | found after |
| 6 | table census: the planted marker, plain AND base64-encoded, searched in EVERY text, jsonb and bytea column of every table | control: the census finds it before redaction, in at least rows 1, 2 and 5 of section 2 | any hit after |
| 7 | `in_reply_to` of a reply to the redacted row | still resolves | none |
| 8 | repeat redaction | same record returned, one record stored | a second record |
| 9 | `version: "all"` on a key with three versions | three records | the marker in any version |
| 10 | a listener on a recipient | receives the redaction event with the key | the body in the event |
| 11 | raw UPDATE of `value` without a redaction record, as the owner role | refused by the trigger | none |
| 12 | raw UPDATE with a record but a changed `created_at` | refused | none |
| 13 | UPDATE or DELETE on `shared_memory_redaction` | refused | none |
| 14 | the legacy-verdict job after a redaction | the row is not classified as legacy | none |
| 15 | a write to the same key after redaction | new version stored normally | the old body |
| 16 | the pre-fix control: tests 4, 5 and 6 run against a build WITHOUT the operation | they FAIL | none |
| 17 | postbox (memory-bxxxxx): a spool line for the key | blanked on the event | the preview text |
| 18 | listener stopped, redaction, listener restarted | the event arrives exactly once | a second delivery; a missed one |
| 19 | chain-head reconcile, rolling rescan and frame audit after a redaction | all pass, no finding | a tamper finding |
| 20 | stored calls: the write call and one read call that returned the row | their rows remain, signatures kept | the marker in `args`, `result`, `result_payload`, chunks, events |
| 21 | `blank_thread_id: true` on a row whose thread id holds the marker | thread id replaced | the marker |

Test 6 decides whether section 2 is complete. Its list of columns is generated from
`information_schema`, never typed.

## 7. The operator's rulings (direct, 2026-09-29 about 06:45Z, in kxxx's session)

These replace the reviewer's proposals of revision 2. Where the text of sections 3 and 6 still
says "author only" or "mail only", this section governs; revision 3 of those sections follows.

1. **Who: the author OR the same owner.** A seat may redact a row when `created_by_peer_id` is its
   own peer, or when `created_by_owner_id` is the caller's owner. A seat that re-registered under
   the same owner can therefore redact its old rows. The redaction record stores both the
   redacting peer and whether it acted as author or as owner. Tests 2 and 3 change: the refused
   caller is a seat of a DIFFERENT owner holding a write grant.
2. **`to` and `cc` stay.**
3. **Wiki and the graph: to be worked out by kxxx and memory-bxxxxx.** In the operator's words:
   "work this out with bxxxxx as I want a fully capable graph not just a wiki, embeddings
   re-ranking the work". So "mail only" is NOT the ruling. The redaction design must cover the
   whole knowledge graph: wiki entries and their confirmations, the derived edges, the
   embeddings and what the re-ranker reads. That part is designed jointly before it is built;
   the mail path is built first and must not close it off (the namespace check is one list).
4. **Reply authors are notified.** The redaction event also goes to the author of every row whose
   `in_reply_to` is the redacted key. New test 22: a reply's author who is not a recipient
   receives the event exactly once.

## 8. Review record

memory-bxxxxx, non-author, on 6fce019 (production mail, 2026-09-29): good design, three changes
before building. (1) The event on the durable path: section 5a, test 18. (2) Step 5 against the
audit chain: measured, the frames hold no text: section 2 rows 9 and 9b, section 5b, tests 19 and
20. (3) `thread_id` can carry content: section 2 row 1, section 3, tests 4 and 21. Smaller points
4 to 6 (the confirmation risk, base64 in the census and logs, the event line): sections 4, 2, 3
and test 6.

## 9. Build state, 2026-09-29 12:50Z

Built and tested on `fin/redaction` (39 tests in four files; the full gate has not run on it):

| design | built | held by |
|---|---|---|
| section 5, the database rule; tests 11, 12, 13 | `sql_meshychat/027` and its down script | `tests/test_redaction_db_rule.py` |
| section 5a, a commit-order position per redaction | 027, part 3 | the same file |
| section 3 steps 1 to 3; tests 1, 2, 3, 4, 8, 9, 15, 21 | `apps/memoryskills/redact.py` | `tests/test_redaction.py` |
| section 7 ruling 1, the same owner may redact | the same | the same |
| section 3 step 5, stored calls; test 20 | the same, `_blank_stored_calls` | both files and `tests/test_redaction_invoke.py` |
| test 6, the census, plain and base64, every text column of every table | `tests/test_redaction.py::census` | run in-process AND over the wire |
| the skill reachable over the wire | `apps/calls/bootstrap.py`, the manifest's seventh skill | `tests/test_redaction_invoke.py` |
| a seat's client command, `redact` (added 2026-10-10; before it a seat needed a hand-built invoke) | `tools/meshy_node/node.py` `redact`, `cli.py` | `tests/test_meshy_node_redact.py` |

**Measured in the build, and changed from the design:**

* The envelope (`provenance_jsonb`) holds the signed payload verbatim, body included. It is cut
  to its `call_id` (section 2 row 4).
* A stored call holds the WHOLE signed payload in `skill_calls.args`, and a write's `result` and
  `result_payload` repeat its tags. On production `skill_calls` is 3.95 million rows, 14 GB, and
  a write call is kept 90 days. The write call is found by the row's own `call_id`.
* A read or query result is replaced by the existing prune an hour after the call ends, so only
  the unreplaced ones can hold the text, and sql/092's partial index holds exactly those.
* `skill_call_events.details` and `skill_call_chunks` held no text in any test (production:
  19.7 million events, none with details; 0 chunks).

**CLOSED 2026-09-29 13:05Z, by the reviewer's choice** (memory-bxxxxx: the prune, "because it removes the
CLASS, not only redacted rows"): `sql_meshychat/028` adds `memory.list` to the read-result prune's
partial index, `retention.READ_SKILLS` names it, and the redaction's search covers it. Production's
backlog is 7,139 list calls, 287 MB, none newer than 2026-09-24: 72 prune ticks. Two tests hold it.
The paragraph below is the gap as first recorded.

**A GAP FOUND: `memory.list`.** It answers with a preview of up to 160 characters
of each row, the read-result prune does not cover it, and its stored results stay for the call
retention, 90 days. No index finds the list calls that named a key. The handler says so in its
result (`stored_list_results`). Closing it needs either the prune extended to `memory.list` (an
index built CONCURRENTLY on a 14 GB table) or a background sweep per redaction. Not decided.

**Not built yet (as of 2026-09-29 12:50Z; SUPERSEDED by section 10):** the listener's event line and its
delivery on return (tests 10, 18), the notice to reply authors (test 22), the legacy-verdict job's skip
(test 14), the audit chain check (test 19), the postbox change (test 17, memory-bxxxxx), the wiki and graph
part (section 7 ruling 3, designed jointly first), and edit (D-2026-09-29-U14).

## 10. Build state, 2026-09-30 00:37Z

Built since section 9 on `fin/redaction`: 44 tests in seven files (61 after section 11). The first whole gate on it (06cd425, 2026-09-30 01:43Z) REFUSED, 8 failed of 4,691;
the findings and their fixes are in divergence 053's Proof paragraph. A second gate was stopped when the review of section 11 changed the head, and the whole gate runs again on the reviewed head.

| design | built | held by |
|---|---|---|
| test 14, the legacy-verdict job skips a redacted row | `apps/memoryskills/legacy_verdict.py` (`_select_unclassified`, `census`) | `tests/test_redaction_followups.py` |
| test 19, the frames and their audit are untouched (and section 5b's correction) | nothing: measured | the same |
| section 7 ruling 4, reply authors named on the record | `sql_meshychat/027` (`notify_peer_ids`), `apps/memoryskills/redact.py` | `tests/test_redaction_delivery.py`, `tests/test_redaction.py` |
| tests 10, 18 and 22, the event through the addressed wait | `apps/mesh/wait.py` (`redaction_event`, `_addressed_seq_page`), `apps/wsgw/envelope.py` (`mesh_redaction`) | `tests/test_redaction_delivery.py` |
| test 10, the live push | `apps/wsgw/mesh_notify.py` (`Redaction`, `_on_redaction`), `sql_meshychat/027` (the NOTIFY trigger), `apps/mesh/wake.py` | `tests/test_redaction_push.py` |
| the listener's `redaction` line | `tools/meshy_node/monitor.py` (`_redaction`, `ORDERED_FRAMES`) | `tests/test_meshy_node_monitor.py`: the redaction tests, one of them through the real server and socket |
| tests 5 and 7, the derived rows are deleted and the reply still resolves | `apps/memoryskills/redact.py` (`handle_redact` in the projection-writer context) | `tests/test_redaction_derived.py` |

**Found 2026-09-30 01:23Z, and a correction to section 9.** Section 3 step 3 was "built and tested" in section 9, and it
deleted nothing on a real database. The derived tables' DELETE policies (`sql_meshychat/008`, divergence 022) need
`app.projection_writer` as well as `app.server_internal`, and a DELETE whose policy is false matches zero rows and
raises nothing. The handler had only the second, and no test had a derived row to notice: test 6's census found the
text in `value`, `signed_bytes` and the envelope, where the design's section 6 asked for `mesh_lexical` as well. The
handler now runs in `rls.projection_writer` (which includes `server_internal`); `tests/test_projection_writer.py` names
it as the one request path allowed in; `tests/test_redaction_derived.py` seeds a row in each derived table and failed
against the first build. The census is only as good as the rows it is given: a control that the text is FOUND in every
place the design names is part of the test, not a nicety.

**Not built yet:** the postbox change (test 17, memory-bxxxxx; the monitor's `redaction` line is its input),
the wiki and graph part (section 7 ruling 3, designed jointly first), edit (D-2026-09-29-U14), and the whole
gate on the branch. Nothing here has been used on production, and each production use needs the operator's
approval at the time (D-2026-09-29-U9).

## 11. The review of the build: #32 at a55a06b

Written 2026-09-30 02:54Z. memory-bxxxxx, non-author, read the build at a55a06b (production mail and the PR comment): the derived-row fix
right, then two BLOCKING findings, each reproduced on HQB's PG16 with a probe that failed at that head, and six
smaller points. Each is taken; divergence 053 says what the branch does now.

| # | Finding | Now | Held by |
|---|---|---|---|
| B1 | `blank_thread_id` left the thread id in `mesh_edges.dst`: the projection stores it as the destination of the row's `in_thread` edge, `thread_id` is the one routing field that is text, and test 21 checked only the memo | `delete_derived(..., blank_thread=True)` deletes THIS row's `in_thread` edge; the walk's second visit writes the edge to `redacted/<record id>` | `tests/test_redaction_derived.py` (census over a seeded edge), `tests/test_redaction_delivery.py` (the projection's own edge) |
| B2 | after one redaction the exemption admitted ANY later rewrite (a re-addressed `to`, a new `from`, a new content field): "the record is the permission" was permanent | one rewrite of this row by its own owner: OLD not already a tombstone, keys within `shared_memory_tombstone_keys()`, routing OLD's, record bound to the row's owner, the redacting peer's owner (from `peers`) and the reason | `tests/test_redaction_db_rule.py` |
| 3 | `kind` was filtered by the wait only | the legacy translation and the embed walk read `kind = 'write'` (the legacy walk's page query without a `kind` predicate, which broke its bound: divergence 053); the projection and the end of the order read every kind, on purpose | `tests/test_redaction_delivery.py`, `tests/test_meshsem_embedjob.py`, `tests/test_mesh_delivery_sequence.py` |
| 4 | no row-count check on the tombstone UPDATE or the write-call blanking | `rowcount != 1` is `E_INTERNAL`; each stored-call UPDATE and each derived delete ends in a postcondition | `tests/test_redaction_derived.py` |
| 5 | `json.dumps(key)` escapes a non-ASCII character that jsonb prints raw | `ensure_ascii=False` | `tests/test_redaction.py` (test 20c) |
| 6 | a failed CONCURRENTLY build leaves an INVALID index that `IF NOT EXISTS` skips | a runbook line: read `pg_index.indisvalid` after 028 | divergence 053, deploy |
| 8 | three tests failed where the session zone is not UTC: `_record_of` rendered `redacted_at` in the SESSION's zone, and the same string went into the tombstone | UTC with a `Z` in the record's answer and the tombstone | `tests/test_redaction_derived.py`; the suite run under `PGTZ=Europe/Berlin` |

What the review shows about the method: a census finds only what its rows hold. Test 6 found the text in every place a
seeded row had it, and no seeded row had an edge, so B1 was invisible to it; the first derived-row bug (section 10) was the
same lesson. A control that the text is FOUND in each place the design names is part of the test.
