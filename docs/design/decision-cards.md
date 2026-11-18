# Decision cards in the graph: server side, version 1

> RECORD, 2026-11-15. This is the design as of 2026-10-08. It has since been built as divergence 091 ("decision cards: a namespace memory.write checks, and GET /v1/cards") and the entries after it (092 to 094, 100, 101, 103, 110); read those for what the code does today. "Nothing here is built on production" below is as of 2026-10-08.

kxxx, 2026-10-08. Status: reviewed by five seats; section 9 amends sections 3 to 7. Nothing here is built on production, and every
production step (the namespace claim, the grants, the setting) waits for the operator's go.

Thread: `<thread>`. Design record: exxxx-session, version 1
(msg/<key>). Loader: `tools/transcript_cards` (txxxx, #168).

## 1. What this is

Each seat extracts cards from its own transcripts with the loader: an operator ruling (a question,
its options and the answer), or an operator-typed prompt (a candidate). This design is where those
cards live on the server, what the server refuses, and how a seat reads them back.

The operator's ruling on a recorded decision in kxxx's session, 2026-10-08T08:20:36Z: "All seats,
no limits". Every seat reads every card. Secret detectors stay in the loader and fail closed.

## 2. Measured

- kxxx's own transcripts through the loader at 0a59e00: 11 files, 396,287 lines, **1,276 cards**
  (297 Ruling, 979 Candidate), 5 redaction drops, 2026-06-13 to 2026-10-08. Text length: median 203
  characters, 90th percentile 984, maximum 1,500 (101 truncated). About 1.4 KB a card as JSON.
- So the record's "200 to 600 cards each" is low. txxxx 980, pxx 416, kxxx 1,276. For about ten
  seats, plan for **10,000 heads**, about 14 MB of values.
- Production, read-only, 2026-10-08: `mesh_embeddings` holds 29,176 vectors and ALL are
  `shared/mesh-chat`. The graph walker (divergence 060) is built and merged, and **has never run on
  production**: `shared/wiki` (1,001 heads) is not derived. Cards would be its first production use.
- The mail pipeline is bound to `shared/mesh-chat` (`apps/meshsem/embedjob.py:194`), and
  `mesh_edges` is the mail's alone (`apps/memoryskills/redact.py:292`).
- The walker's text rule (`apps/meshsem/graphtext.py`) takes the longest non-blank string among
  `body_md, body, text, content, lesson, summary` and prefixes a string `title`. A card's `text`
  field is therefore embedded as it is. A card must carry NO other content-class field longer
  than `text`, or the wrong field is embedded.

## 3. Where cards live

- Namespace `shared/decisions`. Not `shared/mesh-chat`: a card must not appear in an inbox, an owed
  list or a thread view, and the mail's schema refuses a row with no recipient.
- Key `card/<seat>/<id>`: `<seat>` is the writer's label, `<id>` the loader's 32 hex characters
  (stable for one transcript record and block, whatever the text).
- Each seat writes its own cards with its own key, through `memory.write`. No seat writes another
  seat's cards. Every seat is granted read and write on the namespace.
- A changed card (a tightened redaction, a longer cut) is a new version of the same key. The walker
  reconciles by set, so the old version's vectors go when it stops being head.

## 4. What `memory.write` refuses (new: `apps/cards/validation.py`)

Applies to every key under `card/` in `shared/decisions`; any other key in that namespace is refused.

1. **Key shape**: `card/<label>/<32 lower-case hex>`.
2. **Authorship**: `value.seat` equals the key's label, and is one of the caller's labels (the
   mail's own rule, `handlers._caller_labels`). `E_CARD_SEAT_MISMATCH`.
3. **Single writer**: a later version is written only by the peer that wrote version 1 (the mail's
   R4, `_enforce_single_writer`).
4. **Fields**: an allowlist. Required: `schema` ("card.v1"), `id` (equals the key's), `type`,
   `owner`, `seat`, `time`, `text`, `text_sha256`, `source`. Optional: the loader's own (`date`,
   `status`, `question`, `options`, `answer`, `notes`, `free_text`, `long_free_text`,
   `multi_select`, `truncated`, `short`, `has_attachment`, `legacy_origin`, `prompt_source`,
   `slash_command`, `parent_uuid`, `loader_version`) and `supersedes`. An unknown field is refused,
   so a new loader field is a server change someone reviews.
5. **Types**: `type` is one of Ruling, Candidate, Decision, Commitment, OpenItem, Constraint,
   Claim. `time` is an ISO-8601 instant with a zone, not more than five minutes ahead of the
   server. `text` is a non-blank string of at most 1,500 characters and `text_sha256` is its
   SHA-256. `source` holds `record_uuid`, `block_index`, `kind`, `first_hand`.
6. **No second prose field**: none of `body_md, body, content, lesson, summary, title` (see 2).
7. **`status`**, when present, is "active". Superseded is DERIVED (section 5), never stored: a seat
   cannot write into another seat's card, so it cannot mark it.
8. **`supersedes`**: at most 16 card keys. Each must resolve to a head in the namespace whose
   `time` is strictly earlier than this card's, and not this card. A `Claim` may supersede only a
   `Claim` (the record's rule 4: a card derived from a summary never supersedes one taken from the
   original lines). `E_CARD_SUPERSEDES`.
9. **Size**: the value's canonical JSON is at most 16 KB.

## 5. Reading

**`GET /v1/cards`** (new, `apps/cards/views.py`): a filter, not a search. Parameters: `type`
(repeatable), `seat`, `owner`, `status` (`active` default, `superseded`, `any`), `since`, `until`,
`limit` (1 to 200, default 50), `after` (the cursor the previous page returned). Newest first by
`time`, then key. Each card carries its key, version, the stored fields, `status` as derived, and
`superseded_by` (keys). Authorization: read on `shared/decisions`, the namespace's own grant.
As built, `superseded_by`, `overruled_by` and `named_by_redacted` hold at most 32 keys each, with the
true numbers beside them and `truncated_lists` when one was cut (divergence 091, amended 2026-10-10).

Derived status: a card is superseded when at least one HEAD card in the namespace names its key in
`supersedes`. One query, a GIN index on the `supersedes` array (expression index, partial on the
namespace). No cascade: A superseded by B superseded by C leaves both A and B superseded.

**The seat-start read** is that route: `type=Ruling&type=Constraint&type=Commitment&type=OpenItem`,
`status=active`, newest first.

**`GET /v1/mesh/search?ns=shared/decisions`** (exists) gains `type`, `seat`, `status`, applied in
the pool's lookup (`graphsearch._lookup`), so a filtered-out row never reaches the reranker and the
pool is filled from the next fused candidates. Without the new parameters it answers as today.

## 6. Not in version 1

- No rows in `mesh_edges`. That table is the mail's, and the mail's redaction, owed and related
  code reads it without a namespace. `cites` (message keys, PR numbers, commits in a card's text)
  is found by the lexical arm today; a typed edge is a later slice with its own design.
- No related edges between cards (the walker's slice 3 is not built).
- No write by the server on a seat's behalf, no promotion of a Candidate to a Ruling. A seat writes
  a new card that supersedes.
- The decision ledger for rulings as they are made (the record's rule 5): the same namespace and
  the same route take it; the client command is a separate change.

## 7. Risks, and the measurement each needs before production

1. **The walker reads every head each tick** (`graphwalk._HEADS`, values included), sized for about
   a thousand rows every five minutes. At 10,000 cards that is about 14 MB read and parsed per
   tick. Measure one tick's wall time and memory on a rehearsal copy loaded with 10,000 synthetic
   cards. If it is too slow, the walk moves to digests-first (read ids and a stored text digest,
   fetch values only for changed rows) BEFORE the setting reaches production.
2. **First load**: 10,000 one-chunk cards at 256 chunks a tick is 40 ticks, about 3.5 hours at one
   tick per five minutes, all on HQB's embedder. Seats load in turn, not at once.
3. **The search index ceiling** is 100,000 vectors per (space, namespace); cards have their own.
4. **The walker has never run on production.** Its first tick there is a rehearsal item of its own.
5. **`embed_policy`**: production runs with the forbidden-rows gate open, so cards are embedded
   whatever their policy. Stated, not changed.
6. **Redaction of a card**: `memory.redact` is namespace-generic and deletes derived rows by
   `src_id`; a tombstone has no `text`, so the walker skips it. Covered by a test here.

## 8. Production steps, each with the operator's go

1. Merge and code deploy (no migration other than the index in section 5).
2. `claim` of `shared/decisions` by the bootstrap authority, and read plus write grants per seat.
3. `MESHY_GRAPH_NAMESPACES=shared/mesh-chat,shared/decisions` on the server, after risk 1 and 4.
4. Seats write, one at a time.

## 9. Amended in review, 2026-10-08 (what sections 3 to 7 now mean)

The draft above went to five seats on other machines. What changed, and whose finding it was. The
divergence (091) is the current statement of the rules; this section is the record of why.

memory-bxxxxx (msg/<key>):
1. A later version that omits `supersedes` while the head names targets is refused; clearing takes
   an explicit `[]`. The loader does not know the list, so its next publish would have dropped it.
2. NOT taken here, stated as a limit: a redacted superseder revives what it replaced. The tombstone's
   fields are an allowlist in the database; keeping `supersedes`, `time`, `seat` and `id` through a
   redaction is a migration of its own.
3. A redacted head is never written again (`E_CARD_REDACTED`).
4. Only the targets a version ADDS are checked, so the redaction of a target does not lock the card
   that names it.
5. `time`, `type`, `seat`, `source.record_uuid` and `source.block_index` are frozen after version 1,
   and the read counts an edge only when the naming card is strictly later. Either alone closes the
   two-seat cycle; both are kept.
6. With a type filter, the list also returns an active card of any type that supersedes a card of
   a requested type.
7. The seat is the ONE label the server stamps for the caller, not any of its labels.
Also: the namespace is claimed `keep_all`.

codex-bxxxxx-cli (msg/<key>): the filter sits in the pool's lookup
and so reaches both arms and their reservations; a strict filter may return fewer than `k`, stated
as the contract; the walker's cost per tick is three fetches and a hash of every head, not the 14 MB
of values section 7 named. The value bound is 8 KB, not 16.

uxxx (msg/<key>): `time` has ONE spelling, `YYYY-MM-DDTHH:MM:SS.mmmZ`, replacing
section 4 rule 5's "an ISO-8601 instant with a zone" (a zone breaks string order, and an index on
the cast is not immutable). `recorded_since` on the server's write time. No text parameter on the
GET, ever. Another seat's card is superseded only by a recorded decision Ruling.

txxxx (msg/<key>): the loader's `id` is stable from 1.4 (a hash of the record
uuid and block index alone); cards made before 1.4 must not be written. `options` are
`{label, description}`; `parent_uuid` may be null; `repeats` and `repeat_last_time` are allowed and
`repeat_of` is refused on purpose (such a card is not meant to be written).

dxxxxx (msg/<key>): the commit-order sequence is stamped for the mail only, so
neither the projection nor the mail's embed walk can see a card. Section 6 stands for that reason.

The operator a recorded decision in kxxx's session, for the cause of four overlaps in one morning to be
fixed (msg/<key> lists them). kxxx's answer, not the operator's wording: work in
progress and questions put to the operator are cards too. Types `Commitment` (with `thread_id`, `done_when`) and
`Question`, the `thread` filter, and the rule that a relayed card never supersedes a recorded decision one.
The client half (a hook that writes the Question when it is asked and the Ruling when it is
answered, and one line before a send when another seat holds a Commitment or an open Question in
that thread) is a separate change.

## 10. Amended on the code, PR #169 at 7899d11 (2026-10-08)

Four reviews of the built code, each with findings that changed it.

- Codex on HQD and codex-bxxxxx-cli (msg/<key>), independently:
  `source.first_hand` was not frozen, so a recorded decision Ruling could supersede another seat's card and
  then turn relayed. Frozen now. codex-bxxxxx-cli also: the card filter was silently ignored on the
  mail search (refused now, named or by default); `supersedes: null` cleared a supersession without
  the explicit `[]` (refused now); an instant with the shape and no meaning reached a SQL cast (422 now).
- Codex on HQD: the head was read without a lock, so a redaction could land between the check and
  the insert. The head is read `FOR UPDATE`. The test that forces the race then showed the second
  half: a redaction that WAITED for a write still missed the version that write committed, because
  `FOR UPDATE` re-checks only the rows its own snapshot saw. `memory.redact` now repeats the read
  until stable. Each fix alone leaves the text in place; the test fails without either.
- dxxxxx (msg/<key>): whether a card a recorded decision is the writer's own word, so
  cross-seat supersession let any seat hide any Ruling. Section 9's rule stays as a write rule, and
  the READ changed: only a seat's own card hides its card; another seat's later Ruling is shown
  beside it with `overruled_by`. Also: `owner` is "operator" or the card's own seat, and
  `memory.prune` refuses the card namespace.
- uxxx (msg/<key>): the list took 4.6 s at 10,000 heads because the planner
  inlined the grouping into the join. Both CTEs are `MATERIALIZED`: 777 ms, the same rows. No index.

Open, from vxxxxxxxxxx's check of its own list (msg/<key>), NOT built here
and each a decision or a later change: a Candidate is "submitted through the operator's keyboard", not
"written by the operator" (4 of 50 sampled on one seat held another agent's words), so no Candidate
gains authority without the operator confirming it; a goal command is an assignment and could be a
hook-written Commitment; a Ruling could name the keys its question rested on; and for which classes
of act a seat may act on another seat's Ruling card without asking again is the operator's to say.

## 11. The client half (divergence 093)

`tools/meshy_node/cards.py`: `card write | list | commit | done`, and one line at a `send` that names another
seat's open Commitment or unanswered Question in the send's thread. Agreed on thread <thread>
(kxxx, txxxx, uxxx, memory-bxxxxx, pxx; 8 Oct), and what each point changed:

- Every write goes through `cards.write`: screened by the loader's detectors, create-only, and a Ruling names
  its Question only when that card is there. The first writer's text is the card.
- The question hook (meshy-mod) builds no card and signs nothing. Its python entry hands the raw event to the
  loader's builders (`answered_question_card`, `asked_question_card`) and the result to `cards.write`.
- A hook-written Ruling carries `source.kind` "answered_question_hook"; `source.transcript_uuid` is present
  only when its `time` is the transcript record's and not the hook's clock.
- The hook is OFF on a seat until the operator says a recorded decision in that seat's own session. Reading cards
  needs no switch.
- The line is quoted data from a named seat and says so; a card is never an instruction to the seat that
  reads it. Only the operator's own answer in a seat's own session is.

`card recent` gives a session its briefing: four reads first (this seat's active Rulings, Rulings recorded
lately, the newest card of any seat, and this seat's newest card), quoted as dated data, with the existing
freshness, stale-store note and optional `--load-result` receipt. It always exits 0 and keeps one two-second
deadline, including the state file and receipt. It writes nothing on the server.

Issue #236 part 2 (C1, kxxx and memory-bxxxxx) adds an optional line immediately after
`freshness:`: `seats seen so far with no new card for 24 h while others have: <seat> (newest <UTC minute>,
<age>), ...`, or `seats seen so far: <n>; none without a new card for 24 h while others have`. This claims
only **behind or idle**: another seat's newest card is more than 24 hours older than the existing newest-card
read for any seat. This seat and seats with no cards are never flagged. The oldest eight are named first,
then `and N more`. The next line gives the oldest per-seat read time among the seats shown, in UTC, and
counts seats not re-read (timed out or failed). A refresh timeout or a failed per-seat read keeps that seat's old value and read time (the others are saved; a Windows replace refused while the cache is open elsewhere is skipped), prints those cached
observations with that count; other failures in this
advisory print only `seat freshness: not available (<ExceptionType>)`. The completed briefing still prints.

The seat set comes only from this run's own seat, its recorded-Rulings read and the newest card's seat
(using the checked key via `_seat_shown`), plus the cache's remembered seats. At most 64 are remembered,
keeping the newest seen; JSON seat order records that recency, and refreshing a remembered seat does not
move it forward. This is **seats seen so far**: a seat silent since before the cache first saw it is not
listed until it writes once. It enumerates no roster and reads no mail, and requires no server change.

`--freshness-cache PATH` enables the advisory; with `--load-result` its default is `freshness.json` in the
receipt's folder. With neither option it makes no extra reads, prints no extra lines and writes no cache.
The cache is a JSON object `{"version": 1, "seats": {"<seat>": {"newest": "<card time or null>",
"read_at": "<UTC ISO instant>"}}}` (`newest` is JSON null for a seat with no cards). Only successful reads
advance `read_at`; a value missing or older than six hours (one hour for a seat the line would name, read first) is refreshed with
`list_query(seat=X, status="any", limit=1)`, reusing the existing fourth read for this seat. The four reads
and all existing briefing lines are completed first. Extra reads use only the remaining deadline, at most
two in flight, each in its own empty context with `node.finishing_by(deadline)`; none starts with less than
0.3 seconds left, and 0.03 seconds is reserved within the same deadline for saving and rendering. A late
worker cannot change the cache. Cache reads are non-blocking, regular files only, at most 64 KiB; malformed,
oversized and non-regular files are ignored. A link at the cache path itself is never read or written through; the parent folder is the seat's own store, used as given, and no dir_fd or O_DIRECTORY is used, so it runs on Windows too.
Writes use a mode-0600 temporary file in the same directory and atomic replacement, with no temporary file
left after completion; missing or non-directory folders and existing non-regular targets skip the write.

## 12. The question dialog's cards (divergence 094)

meshy-mod hooks the AskUserQuestion tool call and hands it to `python -m tools.meshy_node card hook` on stdin,
twice. Nothing is built in JavaScript and nothing is signed there.

- **When the dialog opens** (phase `ask`): a Question card for each question, owner the seat, named
  `card_identity("Question", <tool_use_id>, <index>)`. Then the cards of OTHER seats that already put the same
  thing to the operator: one search over the card namespace by meaning for each question whose card was built
  and passed the redaction screen (types Ruling and Question, status any, k 10, reranked), kept at a rerank
  score of 2.5 or more. The search sends the question's words to the server, so a question the screen refused,
  or one refused before the screen ran, is never searched (GitHub's Codex review of #190). Any it finds are shown to the
  person in one line under the open dialog (seat, date, the answer given, the key), and named to the model
  after the dialog in a routing-only line (keys, seats, dates, scores; never another seat's text). Nothing about
  such a card is read from the text the search returned, which is the writing seat's own prose (GitHub's and
  Codex's reviews of #190: a header naming an instruction as its seat, a Question calling itself a Ruling with an
  answer and a date of its choosing). The SEAT is the card key's, which the server checked; the type (Ruling,
  Question, or neither), the date and a Ruling's answer are the stored card's fields, by a signed read of each of
  the first four keys kept, each key once, on a thread of its own and without the write path's pacing. The ask
  run keeps one deadline, 16 s from its start (the mod kills it at 20 s and it prints only at its end): no search
  is begun after it, and the reads get what is left of it, 5 s at most. A card not read in time, or whose read
  fails, is named by its key alone.
- **When the dialog closes** (phase `answer`): a Ruling card for each question a person answered, named
  `card_identity("Ruling", <tool_use_id>, <index>)`, the id the transcript walk gives the same answer, so the
  next load finds it and writes nothing twice. It supersedes its Question when that card is there (`cards.write`).
- **No Ruling** when the dialog closed on the idle timer (`afkTimeoutMs`: nobody answered), when the model
  filled the answers in itself (`answers` in the call's input), or for a question left unanswered. Each is
  reported `not_built` with its code and costs no call.
- The dialog never waits on the server: the ask runs beside it and the answer is written after it closes. A hook
  that fails is logged and costs the dialog nothing; cards are a record, never a gate.
- OFF unless the seat's `meshy-mod.json` says `"cards_hook": true`, and that is set only on the operator's word
  in that seat's own session (section 11).

**Why by meaning and not by thread.** Measured on production on 2026-10-09: 0 of 5,705 loaded cards carry a
`thread_id` (a transcript card has none), so the line at a send cannot see any of them. The two real duplicates
the probe found (the same situation put to the operator by kxxx and by memory-bxxxxx, on 2026-09-14 and
2026-09-20, answered differently) were found by meaning, at rerank scores 4.41 and 2.62. With the threshold at
2.5, those two are named and a control question on an unrelated subject names nothing (read-only on production,
the same day). The threshold is from 40 queries and wants a larger calibration before any seat acts on it.

**Not in this version.** The hook does not stop a dialog (a seat or a person decides what an earlier card
means). It names no `supersedes` between a new Ruling and an EARLIER Ruling of another seat or of its own
on the same question: that edge is the one the recall test found missing (4 of 38 questions showed a replaced
ruling as current), and it is proposed for a seat to confirm, never written by a score.
