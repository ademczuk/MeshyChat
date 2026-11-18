# Transcript ingest into the MeshyChat graph, all nodes: final plan

Written 23 Oct 2026, about (time withheld), read-only. Author: memory-bxxxxx, the v1.1 design owner under the operator's
decision 4 (msg kxxx-8136bc). Reviewers named there: kxxx and pxx. Supersedes memory-bxxxxx's v1 draft. Inputs: v1, a pipeline map, the sizing census
(an internal project document (not part of this release), beside this file), and Codex's adversarial review of v1 (gpt-6.1-sol, high effort,
read-only, sanitized brief). The map, the review and the census scripts are on HQB and can be sent on request.

**Revised about (time withheld).** Bxxxxx answered D1 to D4 on record in memory-bxxxxx's session (section 5): F3,
a 30-day backfill, no redaction or withdraw route because this is our own local test, and items kept with vectors
kept 180 days. The executive summary, section 1, steps 3, 4, 5, 6b, 8 and 10, and sections 5 and 6 follow from
those answers. F3 record counts come from a second census pass over the same 14 days (kept with the census scripts on HQB).

Code is this repository at 71176b80 (main) unless the branch
`origin/codex-kxxx/transcript-automatic-context` at 65b849e7 (draft PR #194) is named. Every disputed fact in the
critique was re-read at source for this version; those reads are tagged [re-checked].

Operator, 07:53: "Full agent transcripts should already be embedded and reranked into the graph constantly so u need
to fix this with kxxx as it's better than delta.dev and key to sustaining shared context with meshychat! Work it out
so all nodes do it in an efficient manner".

## 0. Executive summary

**Reading this namespace:** anything read or searched from `shared/transcript-context` may contain LIVE secrets (API keys, tokens, config dumps), stored as written by the operator's 24 Oct decision. The outbound guards (the mesh sender's guard, the wiki write gate, the repo-write guard) are what keep them off PRs, GitHub and the wiki: never paste a search result or context block past them.

- **Today no dialogue reaches the graph.** Uploaders are off on every seat, the parser covers claude and codex only,
  and production refuses graph search on `shared/transcript-context` (422). Cards are the only transcript product in
  search.
- **Already decided, not reopened here** (msg kxxx-8136bc, signature a recorded decision in kxxx's session at about
  08:05): no client-name check on this namespace; and since 24 Oct about 10:25 (on record in kxxx's session,
  "Store everything, secrets included") no secret check either, so items are stored as written; rows 6-7 wait for nxxxxx's Kimi read;
  uploaders seat by seat, kxxx first, then box by box (HQB, HZ, pxx's laptop); v1.1 is chunk, embed centrally, and
  rerank at query time; no embedding on HQD.
- **The design.** One capture worker per box, inside the existing postbox, tails each enrolled seat's transcripts by
  offset and uploads context.v1 items under that seat's identity. The server walks committed manifests in commit order
  on a loop of its own, embeds turn-aware chunks on HQB's GPU, and search filters in SQL before ranking, then reranks
  at query time.
- **At F3 (decided) embedding is still not the budget; storage is.** The fleet makes about 33,000 items and
  63,000 to 86,000 chunks a day, about 20 minutes of embedder time (serial upper bound) on HQB's GPU. The
  100,000-vector in-process index fills in under two days, and 180 days of vectors is 11 to 15 million, 35 to
  48 GB as float32. So a vector store past the in-process ceiling (step 6b, either of its two routes) is the new prerequisite for fleet
  enable. Rerank stays at query time, which avoids the 10.5 h/day an ingest-time rerank would cost at F3.
- **What Codex changed:** lifecycle rules now come before any production write (generation and adapter pinning,
  terminal states for refused items, a poison-manifest rule, a leak remedy), one worker per box instead of one per
  identity, turn-aware chunks with a project id, coverage metadata on every filtered search, a SessionStart that injects
  a compact state record rather than search hits, and a value test against the existing cards and mail baseline that
  must pass before fleet enable. Section 6 lists what it changed and what the code answered.
- **The four operator decisions are made** (section 5, a recorded decision in memory-bxxxxx's session about
  09:00): F3, 30-day backfill, no redaction route, items kept and vectors kept 180 days.

## 1. Measured load

All HQB, 14 full days (9 to 22 Oct), full census of 5.3 GB of JSONL (an internal project document (not part of this release)). HQB holds about 10 of the fleet's
12 seats, so fleet is about 1.2 x HQB.

| Scope | What is embedded | Fleet items/day | Fleet chunks/day | Embed/day, serial upper bound | Days to 100k ceiling |
|---|---|---|---|---|---|
| F1 (decided context.v1) | user and assistant text, boilerplate dropped | about 6,000 | 1,300 to 2,500 | about 17 s | 40 to 75 |
| F2 | F1 plus tool calls and plain thinking | needs a new item kind | about 6,500 | about 2.1 min | about 15 |
| F3 | F2 plus tool results (22% of raw bytes) | needs a new item kind | about 63,000 | about 20 min | under 2 |

- F3 record counts, HQB, 14 days (mean / max per day): tool calls 8,400 / 15,400; tool results 8,500 / 16,400;
  plain thinking blocks 5,800 / 11,700. Codex reasoning is encrypted (1,400 / 4,600 blocks a day) and cannot be
  embedded. With F1's 5,000, HQB makes about 28,000 items a day at F3 if each block is its own item, or about
  19,000 if a tool call and its result share one item (step 3, item 8). Fleet is about 1.2 x HQB.
- F3 text: HQB tool results average 94 MB/day (max 338 MB on 20 Oct), about 23 M tokens. At 1,500-character
  chunks with 200 overlap that is about 72,000 chunks a day for HQB and about 86,000 for the fleet; at 512-token
  chunks about 63,000. The fleet's busiest day is about 3.6 x the mean, about 310,000 chunks, under the embed tick
  bound of 368,000 a day.
- Query-time rerank, measured on production this morning: 429 to 1,046 ms per search. The reranker log shows it busy
  about 1% of a 9.2 h window.
- Upload, F1: HQB is about 5,000 items plus about 500 manifests a day, about 0.06 writes/s against mail's 0.3.
- Backfill at the per-box bucket of 2 writes/s: HQB's last 7 days is about 38,500 writes, about 5.3 h; its last
  30 days is about 165,000 writes, about 23 h. v1 said "a seat's month in 5.5 h" and Codex scaled that to 55.6 h for
  ten seats; both priced the bucket per seat when it is per box. 23 h is the corrected HQB figure.
- Embed backfill, fleet month at F1: about 39k to 75k chunks, 7.5 to 15 min of model time, 2.4 to 4.9 h at the tick
  bound (256 chunks per 60 s).
- GPU outage recovery: at F1 a 24 h outage leaves 1.3k to 2.5k chunks, drained in under 10 minutes. At F3 it leaves
  about 63k, about 4.1 h. So the recovery budget only bites at F3; it is folded into D1 rather than asked separately.
- Embed gate: production reports `forbidden_rows_open: true` [measured, mail search coverage block, (time withheld)].
- Storage at F1: items about 6 MB/day (about 2.2 GB/year); vectors at 3,072 B each, 1.5 to 2.8 GB/year, before
  indexes, WAL and backups.

## 2. Ranked rollout

Order follows the operator's gates (8136bc) and Codex's smallest-path verdict: lifecycle first, one seat end to end,
a value test, then the fleet. Every acceptance test runs against real artifacts and must be able to go red.

### Step 1. Rows 6-7 scratch run (the gate already set)
- **Owner:** nxxxxx (Kimi read of outbox 65b849e7, tree e124faa8, is the gate); txxxx (scratch server, writer peer);
  memory-bxxxxx (runner on HQB); vxxxxxxxxxx and oxxxxxx (readers); kxxx and codex-kxxx-cli (review). Pins as msg
  kxxx-9dd284.
- **Change:** none to production.
- **Acceptance:** context-v1.md step 3, two independent readers agree on the 13-key fixture; plus kxxx-0827cc's
  negative control, the same fault schedule against a retry that re-mints ids must FAIL.
- **Cost:** scratch only, no GPU.

### Step 2. Drop the client-name refusal for this namespace, both ends (8136bc decision 1)
- **Owner:** kxxx (server), codex-kxxx-cli (outbox successor to 65b849e7), txxxx (parser). Review: pxx, memory-bxxxxx.
- **Change:**
  - Server: `screens.screen` skips the marker list when the namespace is the context namespace. This has to be code,
    not an emptied file. [re-checked] `screens.py:47-53` raises `E_CONTEXT_SCREEN_UNAVAILABLE` on a missing,
    unreadable or EMPTY denylist, so emptying it would stall every context write in retry.
  - Outbox and parser: `client_name` stops being a precommit screen for this namespace.
  - **Widened 24 Oct (a recorded decision in kxxx's session about 10:25: "Store everything, secrets included"):** the
    secret screen is off for this namespace too, on all three ends. The server skips both screens for the NAME
    `shared/transcript-context` (`validation.screens_apply`, #380); the outbox drops both precommits and the parser
    stops emitting `omitted, secret` there. Every other namespace, scratch and test context namespaces included,
    keeps both screens.
- **Acceptance (scratch):** an item holding a live denylist marker and an item holding a planted fake secret are
  both stored as written in the production namespace, while the same items are refused with each screen's code in
  any other context namespace; the rows 6-7 fixture still passes on the new outbox head; the diff touches no other
  namespace's screening. (`replace_refused` fires only on detector drift between a client checkout and the server; its opt-in
  wire test covers it.)
- **Cost:** code only.

### Step 2b. A secret screen widened for tool output (pxx L1; no longer gates uploads to shared/transcript-context)
- **Since 24 Oct:** this namespace stores secrets as written (step 2), so 2b does not gate its uploads. It still
  applies to every other namespace that uses `DETECTORS`.
- **Owner:** txxxx (parser, `DETECTORS`), kxxx (server import of the same list). Review: pxx, with codex-pxx-cli's
  L evidence (l-design-evidence.zip, sha256 5029d27b) as the failing inputs.
- **Why:** with F3, tool results, env dumps and config files go up, and the shared `DETECTORS`
  (tools/transcript_cards/transcript_cards.py at 71176b80) are the only admission check. Four forms pass today
  [re-checked at source]: a bare 64-character hex value (the opaque-token detector skips all-hex tokens, :135, and
  so does the quoted check, :285); a letters-only value of 16 or more after a key or token label, in env form or
  JSON (`_SECRET_VALUE` at :61 needs a digit as well as a letter); and a long random value in a URL query
  parameter or fragment (anything after `://` in the same chunk is skipped, :143-144).
- **Change:** three detectors added to the shared list, so both ends refuse the same bytes.
  1. **Labelled value, any character class.** After a credential label (`_KW` plus password, passwd, pwd, auth,
     bearer, session, cookie, client_secret), in `K=V`, `K: V`, `"K": "V"` or `--K V` form, a value of 16 or
     more characters is a hit whether it is hex, letters only or mixed. Hex is caught here, after a label, and NOT
     bare, because bare hex is mostly commit and file hashes.
     - Two exemptions. A MeshyChat `msg/` key stays exempt, as today. A UUID in the canonical 8-4-4-4-12 hex form
       after a session label (session_id, sessionId, parentUuid, leafUuid and the like) is out of the screen
       (kxxx): those fields carry a UUID on almost every record, and a screen that refused every F3 item
       would be switched off. A 32-hex value or any other form after a session label stays caught. The exemption
       belongs to the session-family labels only: after a key, token, secret, password, auth or client_secret
       label a UUID stays caught, because some services issue UUIDs as API keys (pxx). The test pairs the
       cases: `"sessionId": "<uuid>"` clean, `"session": "<32 hex>"` refused, `"api_key": "<uuid>"` refused.
  2. **Named URL parameters.** In a query string or fragment, the value of sig, signature, token, access_token,
     id_token, refresh_token, session, sessionid, key, apikey, api_key, code, client_secret, password,
     X-Amz-Signature, X-Amz-Credential, X-Goog-Signature, and the SAS fields sig and se together, at 12 or more
     characters, is a hit even after `://`.
  3. **Known prefixes**, already partly present: ghp_, github_pat_, gho_, sk-, sk-ant-, xox[abpr]-, AKIA, ASIA,
     AIza, glpat-, and a three-part JWT starting eyJ.
- **On a hit:** unchanged. Just that item is dropped at its ordinal as `omitted, secret`
  (context_parser.py:416-460), and the source keeps uploading.
- **Accepted gaps, written down so nobody takes the screen for complete:** bare hex with no label (commit and
  file hashes look the same); a letters-only value with no label; an unlabelled base64 blob inside file content; a
  secret split across lines or across two tool results; a secret in a binary or image payload. D3 means no
  withdraw route sits behind these: a miss is a DBA delete.
- **Acceptance:** every string in the L evidence is caught after the change and passed before it (red at
  71176b80); negative controls stay clean (a commit sha, a sha256 file hash, a UUID, a git remote URL with no
  parameters, a `?page=2&sort=asc` query, ordinary prose with the word token); the false-positive rate on one
  day of kxxx's real tool results is reported, item by item, before the first F3 upload; server and parser give
  the same verdict on every case.
- **Cost:** code only. It gates step 5 at F3.

### Step 3. Uploader lifecycle rules, in the capture worker
- **Owner:** codex-kxxx-cli (outbox), antigravity (postbox worker, continuing #355), txxxx (parser pinning),
  memory-bxxxxx (enrollment in `load_hook.py`). Review: kxxx.
- **Change:**
  1. **One capture worker per box,** not one thread per identity: round-robin over enabled identities, a hard cap on
     memory and on records per pass, one SQLite store per identity, uploads run under each identity's own `python`
     and `state` as #355 does. A worker fault never touches the mail threads.
  2. **Enrollment carries file identity.** Recorded: path, seat, volume plus file id (inode on darwin), first-seen
     size. Refused: symlinks, non-regular files, a path outside that CLI's session root, a second seat enrolling the
     same file, and a file whose identity changed. Sources are still enrolled only by the seat's own hook or an
     explicit pin; unenrolled files are never read.
  3. **Adapter pinned per (src, gen).** A parser upgrade applies only to sources opened after it. A checkpoint from
     another `STATE_VERSION` is finished by its own pinned adapter or refused, never reinterpreted. Frozen batches
     always drain unchanged.
  4. **Rewritten or truncated source** (size below the stored offset, or a prefix-hash mismatch) opens gen+1.
     Retrieval serves the newest committed gen per src; older gens stay readable by key.
  5. **Terminal states.** A definite screen refusal on a first send already has one: `frozen_outbox.replace_refused`
     swaps the frozen item for a minimal omitted item at the same key and fixes the manifest digest and counts in an
     audited overlay [re-checked, 65b849e7, frozen_outbox.py:71-110, 333-380]. Any OTHER permanent refusal of an item does NOT
     abandon the generation (pxx L2), and it does NOT skip the batch either, because the server commits batch n only
     after batch n-1's manifest, with first == prev.last + 1 (`_enforce_manifest`, validation.py:309-321,
     E_CONTEXT_MANIFEST_PREDECESSOR), so a skipped batch would stall every later batch of the source (GitHub Codex
     r4361506666, pxx). The batch closes with a TERMINAL manifest instead: each refused ordinal is written as
     an `omitted` item at its own key, items already committed stay, and the manifest commits at the same
     first..last with its counts, so the next batch's predecessor and ordinal checks hold. A zero-item manifest is
     not an option: the validator wants one entry per ordinal (validation.py:204) and a stored item for each
     (:331-336). Two cases:
     - `E_CONTEXT_TOO_LARGE` after freeze needs no server change: the omitted item takes `omitted_reason`
       `too_large` with `omitted_bytes`, the path the size refusal itself names (validation.py:232).
     - A refusal with no matching reason (for example a later validator change) is NOT given a catch-all reason
       (kxxx refused a generic `refused` reason: it would widen the schema for an unseen case and turn an
       unexpected refusal into a silent skip). The batch stays pending, never skipped, and the report is loud: a
       batch held pending for more than 1 hour is written to the worker's entry in the box's postbox `status.json`
       as `context_held` (source, gen, batch, ordinal range, refusal code; no content), so `postbox.py --status`
       shows it. If it fires in practice, the validator change that caused it adds its own omitted reason, reviewed
       with that change.
     Retrieval treats an omitted ordinal as a known gap and says so in the context block. Records before and after
     it stay searchable, and the same generation goes on. Generations are only for a rewritten or truncated source
     (item 4). The terminal manifest changes a frozen batch's items, digest and counts before it commits, so it goes
     through the same audited overlay as `replace_refused` (frozen_outbox.py:333-380), and an audit can tell it from
     the frozen manifest. A committed manifest is never edited.
  6. **Batch cut** adds a maximum batch age of 5 minutes and prefers a cut just before a `user_text` item, so a
     question and its answer usually share a batch.
  7. **Per-record accounting:** every native record is emitted, excluded (with class), pending, or failed (with
     code), in a local daily report. This replaces v1's "uploaded within 10% of the parser's count", which used the
     parser as its own oracle.
  8. **F3 item kinds (D1).** context.v1 items gain `tool_exchange` (a tool call and its result in one item, in
     call order, so a result is never embedded without the call that asked for it) and `thinking` (plain
     reasoning only; encrypted Codex reasoning is a reported exclusion class). Both are reviewed schema changes on
     both ends, since unknown kinds are refused. A tool result is kept whole in the item; chunking happens at
     index time. The secret screen runs on every byte of every result, and a result over the item size cap is
     split into ordered parts rather than truncated.
- **Acceptance (scratch, on copies of kxxx's real transcripts):** a hand-checked fixture has no missing, duplicated or
  misattributed item; a kill at local commit and again during an uncertain upload leaves frozen bytes unchanged and
  nothing lost; a planted too-large-after-freeze item is committed as an omitted `too_large` item under a terminal manifest
  for its batch, a record BEFORE it is still searchable afterwards, and the NEXT batch's manifest commits (no
  E_CONTEXT_MANIFEST_PREDECESSOR) and its records upload; a planted refusal with no omitted reason leaves its batch
  pending, nothing after it is marked uploaded, and after 1 hour `postbox.py --status` shows a `context_held` entry
  naming the source, ordinal range and refusal code with no content; a
  rewritten file opens gen+1 and the old gen drops out of retrieval; a second seat enrolling the same file is refused;
  accounting sums to the native record count; postbox mail delivery latency and reconcile age are unchanged.
- **Acceptance (secret screen in tool output, issuecomment-6367521832):** the step-2b screen refuses API keys in tool
  results: every vendor prefix the loader's `vendor_key` detector implements (tools/transcript_cards/transcript_cards.py
  DETECTORS on main: `sk-` (so `sk-ant-` and `sk-proj-`), `ghp_` `gho_` `ghu_` `ghs_` `ghr_`, `github_pat_`, `xox[abposr]-`,
  `AKIA` `ASIA` `ABIA` `ACCA`, `AIza`, `xai-`, `hf_`, `glpat-`, `lin_api_`, `npm_`, `pypi-`, and the generic
  `<2-8 letters>_<24+ mixed>` form), one case each with a synthetic value; labelled values (`KEY=...`, `"api_key": ...`);
  the exact shape of the 23 Oct leak rebuilt with synthetic values, an unindented `<NAME>_API_KEY = "<value>"`
  assignment line of a config file printed inside a tool result, at 108 and at 51 characters; and an UNLABELLED value
  in a URL query string (`https://host/path?<value>&page=2`, no parameter name), since a labelled-only test once gave a false GO (AM #84); and
  a synthetic key planted in a tool result of a SUBAGENT transcript three folders down
  (`<session>/subagents/workflows/<run>/agent-<id>.jsonl`) makes that item refused, with a control that the same
  transcript without the key is accepted.
- **Cost:** no GPU. At F3 with paired exchanges HQB uploads about 19,000 items plus about 1,900 manifests a day,
  about 0.24 writes/s on average and about 0.5 on its busiest day, inside the per-box bucket of 2 writes/s.

### Step 4. No withdraw route (D3, decided)
- **Decision:** no redaction on this namespace (a recorded decision). It was taken for one
  local test deployment and its own transcripts; whoever runs another deployment decides
  this for themselves.
- **What that leaves (since 24 Oct):** no admission screen at all on this namespace (step 2), and nothing else is
  built. Secrets are stored as written by decision; the outbound guards keep them off PRs, GitHub and the wiki. A leaked secret is removed by a DBA delete, which ON DELETE CASCADE carries to the vector row.
  Until then every seat can read it, and an in-process index can still score the deleted vector for up to
  600 s (`search.py:131` FULL_RELOAD_S). Step 2's planted-secret test is the guard, and at F3 it covers tool
  results.
- **Not a gate** on step 5 any more.

### Step 5. One seat uploads to production (8136bc decision 3)
- **Owner:** the seat itself; a second seat supports the worker.
- **Change:** one Claude seat enrolled; 30-day backfill (D2), newest first, mail first; nothing indexed yet,
  so items are read by key only.
- **Acceptance:** one day's items read back by key byte-equal; that day's accounting complete; the step 3 kill tests
  repeated on the real seat; mail lag unchanged.
- **Cost:** at F3 about 2,000 items and 200 manifests a day for one seat; its 30-day backfill is about 60,000
  writes, about 8 h at 2 writes/s.

### Step 6. Server indexing (v1.1)
- **Owner:** kxxx. Review: pxx, memory-bxxxxx.
- **Change:**
  1. **Commit order.** `ctx_commit_order(seq, memory_id, kind)`, stamped on MANIFESTS only
     (`key LIKE 'ctxm/%'`) by a copy of 023's `meshy_mesh_commit_stamp`.
     - [re-checked] That function takes `pg_advisory_xact_lock` BEFORE the `bigserial` draw inside a
       `DEFERRABLE INITIALLY DEFERRED` constraint trigger, and holds it to commit (023:16-24, 52, 69, 81).
     - That is the protocol Codex asked for; v1's shorthand "deferred trigger" did not say so.
     - Lock slot `(1835233133, 2)`: every existing stamper uses slot 1 (023, 027, 029, 042) [re-checked, grep].
     - kxxx OK in principle (230cf5). pxx's review confirmed the ordering holds (023 takes the lock before the
       INSERT draws the seq, and the trigger is deferred), and asked that the ctx stamp copy 042's hardened
       function shape rather than 023's original.
     - A sibling NOTIFY on the same condition.
  2. **Walk:** `seq > watermark AND kind = 'write'`, as `embedjob.py:95`; gaps from aborted transactions are normal.
     A `redaction` kind deletes that item's chunks. Per manifest, chunks, vectors, lexical rows and the watermark
     commit in one transaction, idempotent on `(src_id, space, chunk_no)`.
  3. **Chunks are turn-aware:** a user item plus the replies that follow it, split deterministically at 1,500 chars
     with 200 overlap by `graphtext.chunk_spans`. Each chunk records `first_ord, last_ord` and character offsets, so
     collapse to item is exact. Manifests, omitted items and superseded gens carry no text.
  4. **Side table** `mesh_ctx_chunks(src_id, seat, src, gen, batch, project, first_ord, last_ord, offsets, time_first,
     time_last, text_sha256)`, indexed on (seat, time_last), (project, time_last) and (src, gen, batch).
     - `project` is a new optional manifest field: the sha256 prefix of the session's working directory. It is a
       reviewed schema change, because unknown fields are refused.
  5. **Reuse:** a chunk whose digest already has a vector in the space reuses it, with no GPU call. Occurrence rows
     stay separate, so provenance and filters still work.
  6. **Poison manifests (pxx L3).** A manifest that fails 5 times for a non-transient reason gets a durable
     terminal row, `ctx_dead_letter(seq, memory_id, reason, attempts, at, state)`, written in the SAME transaction
     that advances the watermark past it. So the watermark never passes a manifest that is neither embedded nor
     dead-lettered. Coverage reports each dead-lettered seq as a hole. Replay: a repaired manifest is a new write
     with a new seq (manifests are immutable), or an operator sets the row to `replay`, and a side loop walks
     `replay` rows outside the watermark and marks them done. Transient failures (embedder down, timeout) stay
     retryable and never dead-letter.
  7. **Loop and gate:**
     - Its own loop: LISTEN plus a 60 s backstop, one sub-batch of 32 in flight.
     - It yields to mail when `mesh_embed_catch_up` has more than one tick pending.
     - A transient failure never advances the watermark. The only thing that passes an unembedded manifest is
       its dead-letter row, committed with the watermark.
     - It reports `gate_open` each tick.
  8. **Model change:** a new space, re-walked from seq 0 with its own progress, and served only after an explicit
     cutover.
- **Acceptance:** transaction A starts first and commits after B, and both manifests are walked exactly once; a kill
  between the chunk write and the watermark leaves no duplicate and no gap; a duplicate chunk makes no embed call; a
  poison manifest is dead-lettered while later ones are walked; with the embedder stopped for an hour, the backlog
  drains after it returns and the watermark passes an unembedded manifest only together with its dead-letter row;
  a dead-lettered seq shows as a hole in coverage and replays once marked; mail embed lag is unchanged; a
  space re-walk is idempotent.
- **Cost:** F1 about 17 s/day of embedder time for the fleet; 1.5 to 2.8 GB/year of vectors.

### Step 6b. A vector store that does not live in process (prerequisite for F3 fleet enable)
- **Owner:** kxxx. Review: pxx, memory-bxxxxx.
- **Why:** vectors are float32 BYTEA rows in `mesh_embeddings` (006), loaded into an in-process index capped at
  `DEFAULT_CEILING = 100_000`, about 300 MB (`apps/meshsem/search.py:127`). F3 reaches that in under two days.
  With vectors kept 180 days (D4) the namespace holds 11 to 15 million vectors.
- **Change, recommended:** the context namespace's vectors go into a pgvector `halfvec` column with an HNSW index
  in Postgres, beside the existing rows; mail keeps its in-process path unchanged. Step 7's filters apply inside
  the query (pgvector 0.8 iterative scan), so a filtered search never silently loses its subset. EmbeddingGemma's
  Matryoshka truncation to 256 dimensions is the size lever: 768-d halfvec is about 17 to 23 GB for 180 days
  before the index, 256-d about 6 to 8 GB. Measure recall on the step 8 questions before choosing.
- **pgvector is not on production today** (kxxx, read-only check about (time withheld)):
  `pg_available_extensions` lists pg_trgm 1.6 and no `vector`; the server is the stock postgres 16.14 image with
  no vector files. Loading it means a different image (pgvector's pg16 image, or ours built FROM the current
  digest plus postgresql-16-pgvector) restarted on the same data directory. That is a short production outage
  and a production change, so it needs Bxxxxx's go and a rehearsal off HQD first.
- **So 6b has two routes, and fleet enable waits on either, not on the image swap:**
  - Route A, pgvector HNSW as above, once Bxxxxx approves the image swap after a rehearsal.
  - Route B, no Postgres image or extension change (it is still a new search implementation, so it ships with
    a normal deploy and Bxxxxx's go): shards with a hard size, plus exact scoring for small filtered sets.
    pxx L4: a plain (seat, month) shard would hold about 157,000 to 215,000 vectors at the sizing doc's 11 to 15
    million over 12 seats and 6 months, over the 100,000 ceiling, so the rules are:
    1. **Shard size is capped, not the month.** A shard is (seat, month, n), and a new n starts at 50,000
       vectors, so no shard ever reaches the 100,000 ceiling and none drops rows.
    2. **A small filtered set never depends on residency.** When step 7's SQL filters give 20,000 chunks or
       fewer, their vectors are read straight from `mesh_embeddings` (about 60 MB at 768-d) and scored exactly.
       This is the path the filtered old-needle acceptance tests, so a busy month cannot hide a small set.
    3. **Resident memory budget 2 GB** for context shards (about 13 shards of 50,000 at 768-d float32), with
       least-recently-used eviction. At most 4 shards load for one query; a fifth waits or is skipped.
    4. **A query across many shards** searches the newest shards first within the budget, and its coverage
       block names shards searched, shards skipped and the oldest time covered. It never reports a skipped
       shard as searched.
    5. **The size lever** is the same as route A's: 256-d Matryoshka vectors cut every figure above by three,
       to be chosen on the step 8 recall measurement.
  Route B is the default until Route A is approved and rehearsed.
- **Retention (D4):** items are kept; a daily job deletes context vectors older than 180 days, and read by key
  still covers everything. That is a job of its own: lifting `memory.prune`'s guard (`handlers.py:1672-1737`)
  prunes items, not vectors, and items are kept (pxx, INFO).
- **Acceptance:** recall@10 on the step 8 questions within 2 points of exact scoring; p95 filtered search under
  1.5 s with 15 million synthetic vectors loaded; mail search latency unchanged; a vector older than 180 days is
  gone while its item still reads by key.
- **Cost:** about 20 to 50 GB of disk for vectors plus index at steady state, by dimension and precision.

### Step 7. Search route, filters and per-arm timings
- **Owner:** kxxx. Review: pxx.
- **Change:**
  - `ns` allowlist adds the namespace.
  - Filters `seat, src, project, since, until` resolve in SQL against `mesh_ctx_chunks` BEFORE ranking, on BOTH arms.
  - A matching subset of 20,000 chunks or fewer is scored exactly. A larger subset returns a coverage note and never
    a silent truncation.
  - The coverage block, which already reports `ceiling` and `over_ceiling` for mail, adds subset size, scored count,
    truncated, and outside-ceiling.
  - `collapse=chunk|item|session`.
  - Per-arm server timings: index load, lexical, vector, fusion, rerank, body join. kxxx agreed (230cf5):
    read-only instrumentation in the response's debug block, shipped at the next normal deploy with Bxxxxx's go.
- **Acceptance:** a sparse seat's planted needle is found with `seat=` set while the unfiltered top 2,000 misses it; a
  needle older than the newest 100,000 vectors is found under a filter, including inside a month whose shards
  overflowed the resident budget (route B rule 2); the coverage block says truncated when it is;
  p50 and p95 of 20 filtered searches on production are reported by arm.
- **Cost:** query-time rerank, measured at 0.4 to 1 s a search. The timing part is independent and can ship today.

### Step 8. Value test (a report, no longer the scope gate)
- **Owner:** kxxx writes 10 to 15 resumption questions about his seat's own last week before seeing any results;
  memory-bxxxxx builds the harness; vxxxxxxxxxx grades blind.
- **Acceptance:**
  - A fresh kxxx session given his Context card plus `context search` answers more questions correctly, with correct
    source keys, than the same session given his Context card plus card and mail search only.
  - Any older relevant item that cache limits hide counts as a miss.
  - Bxxxxx chose F3 directly rather than "F2 after the value test", so the result no longer decides scope. It is
    still run and reported, and it sets the step 6b dimension choice.
- **Cost:** about 30 searches.

### Step 9. Parser dialects (runs in parallel from today)
- **Owner:** txxxx (codex), nxxxxx (kimi), antigravity (a reviewer); txxxx reviews.
- **Change:** drop Codex's re-injected `# AGENTS.md instructions` and `<environment_context>` as a reported drop class;
  add the Kimi and antigravity context dialects.
- **Acceptance:** the 14 Oct rollout `6b474a1a...` gives 2 such items on the current parser and 0 after; each new
  dialect passes `mutate_context_parser.py` and the opt-in wire test against #346's validator.
- **Cost:** code only. Gates only the seats that use that dialect.

#### Step 9b. What each record becomes at F3 (pxx, INFO)

Today's parser drops tool results, Codex analysis messages, isMeta records and compaction summaries, and collapses
a subagent to one artifact (context_parser.py:275-398). Adding item kinds does not undo those branches, so the F3
parser states each one:

| Record | Today | At F3 |
|---|---|---|
| user and assistant text | item | item, as F1 |
| tool call plus its result | dropped | one `tool_exchange` item, paired by tool-use id |
| plain thinking | dropped | `thinking` item |
| Codex encrypted reasoning | dropped | dropped, reported exclusion class |
| Codex analysis messages | dropped | `thinking` item |
| isMeta records | dropped | dropped, reported (harness noise) |
| compaction summaries | dropped | dropped, reported (near-duplicates of earlier dialogue) |
| re-injected AGENTS.md, environment context | item | dropped, reported (step 9) |
| subagent sidechain | one artifact | its own src, enrolled with the parent's seat, with a parent link |

Pairing: a call with no result yet waits up to the batch age, then goes up as `tool_exchange` with
`result: missing`, and a late result is its own `tool_exchange` with `call: missing` and the same tool-use id.
Results out of order pair by id, never by position. A duplicate result for one id: the first is kept, and later
ones are a reported duplicate class.

Duplicates across records upload as separate occurrences (provenance and filters need them), and only vectors are
shared by digest (step 6 item 5). The sizing census dedupes by uuid for counting only, so its figures are per
unique record.

### Step 10. Enable by box
- **Owner:** each seat; memory-bxxxxx coordinates HQB.
- **Order:**
  - HQB: Claude seats, then codex-bxxxxx-cli, nxxxxx and antigravity as their dialects land.
  - HZ: only after a real send from HZ through #355 is shown. Interpreter equality is a prerequisite, not evidence.
  - pxx's laptop.
- **Acceptance, per box:**
  - One day's accounting is complete.
  - Freshness: native record to searchable, p95 under 15 minutes.
  - Mail embed pending stays under one tick at every sample, and postbox reconcile age stays under 10 minutes.
  - The 30-day backfill drains (D2). HQB's is about 570,000 item writes plus manifests at F3 with paired
    exchanges, about 630,000 writes with manifests. kxxx approved 8 writes/s for backfill (230cf5) with three
    limits: 00:00 to (time withheld) only; a mail write never waits behind a backfill write; and it drops back to
    2/s for the rest of the night if /v1/livez takes over 2 s twice in a row. Daytime stays 2/s. That is about
    200,000 writes a night plus about 120,000 by day, so HQB's month drains in about 2 days on the healthy path, and
    about 4 days on nights the livez rule drops it to 2/s. livez is sampled every 60 s during the backfill window,
    and an error or a timeout counts as slow. The fleet's month
    is about 1.9 to 2.6 million chunks, 10 to 14 h of embedder time and 5 to 7 days at the current tick bound,
    so the tick bound is raised for the backfill window.
- **Cost:** see section 1.

### Step 11. Session use
- **Owner:** memory-bxxxxx (hook), kxxx (client route), the meshy-mod owner.
- **Change:**
  - SessionStart injects a compact state record under 4 KiB: the seat's latest Context card (it already exists, at
    600 chars or less) plus its last committed manifest keys for this project.
  - The record is framed as quoted evidence with keys, never as instructions.
  - No historical search at SessionStart, since that prompt is not yet known. A `context search` route in meshy-mod is
    used once the real prompt arrives.
  - v1's top-5 prefetch is dropped.
- **Acceptance:** the hook adds under 200 ms; the block carries keys; a planted instruction inside a past item is shown
  as quoted text and not acted on (manual check).

## 3. Per-node setup

Nothing embeds on any seat box. HQB's GPU serves the embedder (:7875) and reranker (:7876) the production server
already uses.

Every box gets three things:
1. the postbox at the version with the capture worker (#355 line, off by default per identity);
2. the client and parser at one pinned commit from a clean checkout (exit 6 if dirty, as the card jobs do);
3. per identity in `config.json`: `context: {enabled, dialect, pins, backfill_days}`.

| Box | Seats | Dialect | Enrollment and wake | Before enable |
|---|---|---|---|---|
| HQD | kxxx (first), codex-kxxx-cli | claude, codex | Claude hook; codex pin plus 60 s timer (not verified from HQB) | steps 2 to 4; codex needs step 9 |
| HQB | memory-bxxxxx, dxxxxx, exxxx-session, txxxx, uxxx, vxxxxxxxxxx | claude | hooks already installed (settings.json 159/190/231/313), enrolling `transcript_path` | step 8 passed |
| HQB | codex-bxxxxx-cli | codex | pin, timer, new-session guard enrolls new rollouts | step 9 codex fix |
| HQB | nxxxxx | kimi | pin and timer on `wire.jsonl` | step 9 Kimi dialect |
| HQB | antigravity | antigravity | timer on `transcript.jsonl` | step 9 dialect; antigravity confirms `transcript.jsonl` is complete without the SQLite store |
| HQB | hxxxxx | claude | hook | no card store on HQB yet; check before enable |
| HZ | oxxxxxx, codex-oxxxxxx-cli, codex-oxxxxxx-admin-cli | claude, codex | hook; pin | a shown #355 send from HZ (Local Network) |
| pxx's laptop | pxx, codex-pxx-cli | claude, codex | hook; pin | not verified from HQB |

## 4. Decided, and not reopened

| Decision | Source |
|---|---|
| context.v1 rollout step 4 done: deployed, `shared/transcript-context` claimed, every seat granted read plus own-records write, grants read back, probe written and read by a second seat | a recorded decision in kxxx's session, 20 Oct about 20:17 ("Deploy, claim, grant all"); kxxx |
| No client-name check on `shared/transcript-context`; other lanes stay strict | 8136bc item 1, on record in kxxx's session about 08:05 |
| No secret check either on `shared/transcript-context` ("Store everything, secrets included"): items stored as written, outbound guards unchanged; every other namespace keeps both screens | a recorded decision in kxxx's session by AskUserQuestion, 24 Oct about (time withheld), with the risk stated (real keys, every seat reads and searches); #380 |
| Rows 6-7 wait for nxxxxx's Kimi read | 8136bc item 2 |
| Uploaders seat by seat, kxxx first, one day checked, then HQB, HZ, pxx's laptop | 8136bc item 3 |
| v1.1: chunk, embed centrally, rerank at query time (so "reranked into the graph" means query-time rerank, not ingest edges) | 8136bc item 4 |
| No embedding on HQD | a04faf, README:94-97 |
| Every seat reads every seat's context | 10 Oct ruling, context-v1.md section 2 |

## 5. Operator decisions (made)

Asked by AskUserQuestion in memory-bxxxxx's session and answered by Bxxxxx about (time withheld), 23 Oct. Each
option was priced in the question as in section 1.

| | Question | Bxxxxx's answer | What follows |
|---|---|---|---|
| D1 | What "full" covers | **F3 everything**: dialogue, tool calls, plain thinking and tool results | step 3 item 8, step 6b |
| D2 | Backfill depth at enable | **30 days** | steps 5 and 10 |
| D3 | Leak remedy | no redaction on this namespace (a recorded decision for one local test deployment) | step 4: no withdraw route; the secret screen stays |
| D4 | Retention | **Items all, vectors 180 days** | step 6b retention job; prune refusal lifted for vectors |

Recommendations he did not take, for the record: F1 now, 7 days, the R1 withdraw route, and keep_all. Nothing
in the plan waits on him now except the per-seat uploader GOs he already set (8136bc item 3).

## 6. What the critique changed

**Accepted and built into the plan**
- Lifecycle rules ahead of any production write: per-source adapter pinning, generations for rewritten sources, an
  abandonment rule for non-screen refusals, the poison-manifest dead-letter, and watermark-after-commit (steps 3, 6).
- One capture worker per box instead of one per identity, file-identity enrollment with symlink and replacement
  checks, and a 5-minute maximum batch age (step 3).
- Turn-aware chunks with offsets, a `project` filter, filters on both arms, coverage and truncation metadata, and
  defined behavior above 20,000 chunks (steps 6, 7).
- SessionStart injects a compact state record as untrusted evidence, and historical search waits for the real prompt
  (step 11).
- Acceptance by per-record accounting and a held-out resumption test against the cards and mail baseline. This
  replaces v1's parser-against-itself count (steps 3, 8).
- Deferred: subagent artifact synthesis and its 30-minute rule, cross-session record suppression (digest reuse covers
  the vectors), three collapse modes beyond `chunk|item|session`, prefetch, million-vector work, F2/F3, and in-item
  replacement (B2). F3 itself is no longer deferred: Bxxxxx chose it (D1).
- Per-arm timings come before any storage redesign, and can ship today (step 7).

**Narrowed by the code [re-checked]**
- Commit order: 023 already takes the lock before the sequence draw and holds it to commit. The fix is to copy that
  function, not to design a new protocol. Gaps are already handled by `seq > watermark`.
- Refused-item terminal state: `replace_refused` on 65b849e7 already covers a definite screen refusal. Only
  non-screen refusals needed a new rule. Codex reviewed a brief that did not mention it.
- Suppression: redaction machinery exists for mail (027, `delete_derived`). The gap is the context namespace's
  manifest digest, so D3 offers finishing it rather than a new table.
- Backlog: 55.6 h assumed ten seats at 40,000 items each. The plan's own fleet rate gives about 23 h for HQB's month.

**Overtaken by the operator**
- Client names. The operator removed the check at 08:05 (8136bc). The brief, written about 08:25, missed that and
  still described whole-item refusal, so Codex advised against an exemption (B4). The ruling stands. Its consequence, an
  admission path with only a secret screen, was why a leak remedy was offered. Bxxxxx declined it (D3).

**Still unproven, carried as tests rather than assumptions**
- Whether transcript context helps at all (step 8).
- Whether the secret screen is adequate on tool results, which carry file contents and config dumps (the step 2
  planted secret, run on tool results too). There is no withdraw route behind it (D3).
- The fleet x1.2 load estimate (step 10 accounting per box).
- Batched embedder throughput: the serial probe is a floor.
