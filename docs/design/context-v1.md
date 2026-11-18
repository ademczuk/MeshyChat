# context.v1: the transcript-context wire schema (DRAFT r4, not enabled)

Status: draft for review, r4 2026-10-19. Owner of this schema: kxxx. Nothing here is enabled: the namespace is
unclaimed on production, no grant exists, and no seat may write context records to production until the operator
gives a go on a reviewed version (section 9).

**r4 (1d0f98): v1 never keeps a prefix of an oversized item, and manifests commit in strict successor order.** **r3 changed the unit of storage: ONE DIALOGUE ITEM IS ONE WRITE.** codex-kxxx-cli's re-review of r2
(msg/<key>) showed that an item written in several parts cannot be admitted whole
without a private staging boundary. A marker can span three short parts, so the `lead` overlap did not cover it,
and a writer killed after a refused part leaves the earlier parts readable. Both are true of ANY multi-write item.
r3 removes the cause instead of patching it: the server screens each item's whole text in the single write that
makes it readable, so nothing partial is ever published and no client clean-up is relied on. The mesh already
accepts 64 KiB in one write (`MESH_MAX_BODY_BYTES = 65_536`, apps/memoryskills/validation.py), so this needs no new
size class. "Changes in r2 and r3" at the end maps every finding to its fix.

This is the server/schema half of the automatic transcript context. The capture, queue and hook half is
codex-kxxx-cli's `docs/design/automatic-transcript-context.md` (branch `codex-kxxx/transcript-automatic-context`,
queue source baseline 7e41018d), and this document builds on its "Proposed shared schema" section rather than
beside it. The parser and redaction half stays with txxxx (Kimi dialect: nxxxxx).

## 1. What it is, and what it is not

- **context.v1** stores the VISIBLE DIALOGUE of a seat's sessions (what the operator typed, what the seat answered,
  plus task and artifact items) so a resuming seat, or another seat, can rebuild what was said, in order and whole.
- It is NOT the decision cards (`card.v1`, `shared/decisions`, `docs/design/decision-cards.md`): a card is one
  on record operator decision. Dialogue is evidence, never a ruling, and nothing here promotes an assistant's text
  to authority.
- It is NOT #297's `Context` card: that is one short pruned digest per seat run (`tools/transcript_cards/
  context_snapshot.py`, at most 600 characters, inside `shared/decisions`). context.v1 is the full text it digests.

## 2. Decided inputs (the evidence this schema rests on)

| Decision | Source |
|---|---|
| Assistant turns may be stored on the mesh server and read by ALL seats, like the cards. The secrets detector still fails closed. | a recorded decision in kxxx's session 2026-10-10 ~20:50Z (AskUserQuestion: "All seats") |
| The context namespace gets the NORMAL check: client and engagement names are refused. Only the card namespace keeps its 2026-10-08 exemption. | a recorded decision in kxxx's session 2026-10-10, reported in msg/<key> |
| SUPERSEDES the two rows above for `shared/transcript-context`: no client-name check (2026-10-23, kxxx-8136bc) and no secret check (2026-10-24, "Store everything, secrets included"). Items there are stored as written and may hold LIVE secrets; the outbound guards keep them off PRs, GitHub and the wiki. Other namespaces, scratch and test context namespaces included, keep both screens. | a recorded decision in kxxx's session 2026-10-24 about (time withheld) (AskUserQuestion); v1.1 step 2, #380 |
| Subagent files give ARTIFACT items only. `agent_id` is non-null only on artifact items; the server refuses an assistant_text or task item carrying one. | msg/<key>, msg/<key> |
| No silent cut: an item that cannot be stored whole is an explicit omission carrying its byte count, never a quiet loss. The screens see the WHOLE text, so a hit anywhere drops the whole item. | 05f572; txxxx agrees, msg/<key> (r3: one write per item replaces ordered parts; r4: v1 omits an oversized item whole instead of keeping a prefix) |
| A task (update_plan) step is reported completed only when its completed count rises and its total does not fall; a removal is never a completion. | txxxx |

The earlier per-seat reader table in codex-kxxx-cli's document predates the "All seats" ruling and is history.

## 3. Namespace and who may do what

- Namespace `shared/transcript-context` (setting `MESHY_CONTEXT_NAMESPACE`, so a test claims its own). It does not
  exist on production today (checked 2026-10-19: no rows).
- **Write:** a seat writes only its own records. The `seat` in the key and the value must be the caller's
  server-stamped seat label; the peer UUID from the signature is the identity, the label is display. No foreign-seat
  write, no operator override.
- **Read:** every seat (the ruling above). No public read.
- **Write-once:** every key is written once (version 1) through `memory.write`, with no exception. A later version
  is refused, and a write to an existing key is refused, never merged. Redaction does not come through
  `memory.write` at all: it is `memory.redact` (section 8). An uploader that does not know whether its write landed
  settles it with a signed read and a byte-compare.
- **Admission, server-side, in the one write** (not on `shared/transcript-context` since 2026-10-24, see section 2):
  the secrets detector (fail closed) and the client/engagement-name
  check (refuse) run on the item's WHOLE text, in the same write that would make it readable. A refused write adds
  NOTHING TO THE NAMESPACE, and no other seat can read it. There is no staging area, no deferred check, no streaming
  context and no overlap to trust, because the detector always sees the entire item at once.
- **But the call log keeps the request (memory-bxxxxx, P1-1).** The calls pipeline stores every signed request,
  args included, in `skill_calls` before the handler runs (pipeline.py:366, repository.py:170), and keeps it for the
  call retention (90 days). The database alone does not keep other seats out: `sql/006`'s `skill_calls_peer_select`
  shows a row to its caller or target peer, but `sql/016`'s `skill_calls_server_internal_select` ORs in every row under
  `app.server_internal` (memory-bxxxxx). What holds the line is the application. The calls read route serves a
  call only to its caller or target (`apps/calls/reads.py` `for_viewer`), the memory peer is keyless, and the dashboard
  shows counts only. So a refused item's text is readable by its own writer, who already holds it, and by server code
  and a database administrator, never by another seat. **DECIDED by the operator, 2026-10-19
  ~11:05a recorded decision in kxxx's session (AskUserQuestion): "Keep as is".** The refused request stays in `skill_calls`
  for its retention, readable only by its writer and an administrator, and no scrub is built. This section's claim is
  the narrower one above.

## 4. Keys

    ctx/<seat>/<src>/<gen>/<batch>/<ordinal>     one dialogue item
    ctxm/<seat>/<src>/<gen>/<batch>              that batch's manifest

- `<seat>`: the cards' seat label rule (`[a-z0-9][a-z0-9_.-]{0,63}`).
- `<src>`: 32 lowercase hex, an OPAQUE source id: sha256 of the native session id where the dialect has one, else
  of a locally assigned random source-file id; the first 32 hex digits. Raw paths and raw-text digests stay on the
  seat.
- `<gen>`: 4 decimal digits, 1-based, the WIRE generation of that source. A rewritten or replaced source file is
  the next generation, so its first item never collides with the first generation's. The writer keeps the mapping
  from its local opaque generation id (64 hex, never sent) to the wire number in its checkpoint, allocates the next
  number in the same atomic checkpoint commit that records the rewrite, and never reuses a number.
- `<batch>`: 6 decimal digits, 1-based, per generation. **The batch is in the item's key**, so an item belongs to
  exactly one batch by construction. A batch has one manifest key, written once. Two manifests can therefore never
  list the same item, with no first-claim race to arbitrate.
- `<ordinal>`: 10 decimal digits, the item's position in the generation (0-based, zero-padded, source-wide, so
  ordinals keep counting across batches), so key order is dialogue order.
- **Allocation:** generation, batch and ordinal are assigned when the local batch is committed (codex-kxxx-cli's
  `incremental_store.commit`), in the same atomic write as the source offset, never anew per capture wake. A retry,
  a crash and restart, or a replayed uploader recomputes the SAME keys and the SAME bytes. The write-once rule turns
  a duplicate into a refusal that the uploader settles by a byte-compare read, never an overwrite. The uploader marks
  a batch confirmed on the server only after it has read back every key it wrote, the manifest included, and matched
  the bytes.
- **Uploader obligations** (agreed with codex-kxxx-cli, msg/<key>; this is the
  canonical copy, and the queue document refers to it):
  1. Before the first send, freeze the wire generation, batch, source-wide ordinal range, adapter version and the
     final serialized bytes of every item and of the manifest, durably, in the same local atomic output and
     checkpoint commit. That includes `omitted` records, whatever the reason.
  2. After a restart, reuse the same keys, timestamps and bytes. Never regenerate a time, a key or a value.
  3. On transport uncertainty, on a write refused because the key exists, or on `E_WRITE_CONFLICT` (two first
     writes of one new key racing on `shared_memory_nkv_unique`, handlers.py:1189; its "retry" means re-check, not
     re-send), read the exact key with an authenticated read and compare the bytes. Equal means the write landed. A server-redacted key is recorded as
     redacted and never re-uploaded. Anything else is a conflict, reported, never overwritten.
  4. Commit manifests in successor order (section 6). Mark a batch confirmed only after every item and its manifest
     have reconciled. A local atomic checkpoint is not server confirmation.

## 5. Item (one dialogue item, one write)

| Field | Type | Rule |
|---|---|---|
| `schema` | "context.v1" | required |
| `kind` | "item" | required |
| `seat`, `src`, `gen`, `batch`, `ordinal` | as the key | required; must equal the key |
| `item` | "user_text" \| "assistant_text" \| "task" \| "artifact" \| "omitted" | required |
| `role` | "user" \| "assistant" | required for text items; absent on task, artifact and omitted |
| `dialect` | "claude" \| "codex" \| "kimi" \| "antigravity" | required |
| `adapter_version` | version, `^\d{1,4}(\.\d{1,4}){0,3}$` | required (the loader version that produced it, e.g. 1.23) |
| `time` | `YYYY-MM-DDTHH:MM:SS.mmmZ` | required: the record's own time in the cards' one spelling; at most 5 min ahead of the server (the cards' rule) |
| `native_ref` | 32 hex | optional: the first 32 hex of the sha256 of the dialect RECORD's id (Claude: the record uuid), never the id itself, so no text rides in it and the secret detectors, which match real id shapes such as `toolu_01...`, never see it. Several items share one when a record holds several blocks; it identifies the record, not the item. A reader maps it back locally by hashing |
| `text` | string | required except on `omitted`: the item's WHOLE text (v1 never stores a prefix) |
| `text_sha256` | 64 hex | with `text`: sha256 of `text` (UTF-8); the server recomputes it |
| `omitted_reason` | "secret" \| "client_name" \| "too_large" | ONLY on `item: omitted` |
| `omitted_bytes` | int >= 1 | ONLY on `item: omitted` with reason "too_large": the original item's UTF-8 size (05f572's byte count), nothing else |
| `agent_id` | 32 hex | ONLY on `item: artifact` (f10449); refused on any other item. The sha256 prefix of the subagent id, as `native_ref` |

**Budget: the serialized value, and v1 never cuts.** The server serializes a value as the cards do
(`json.dumps(value, ensure_ascii=False, separators=(",", ":"))`, UTF-8) and refuses one over 65,536 bytes. JSON
escaping can double a quote and multiply control characters by six, so the WRITER decides by serializing the complete
candidate value (every field, maximal metadata) with that same serializer. If it does not fit, the item is NOT cut:
it is written as `item: omitted`, `omitted_reason: "too_large"`, `omitted_bytes` = the original's UTF-8 size, and no
text and no digest. **Why no retained prefix (r3 review, codex-kxxx-cli 1d0f98):** the server never sees a dropped
tail, so it cannot screen it. Two originals that differ only after the cut produce byte-identical values, and a
secret the writer's screen missed in the tail would publish the prefix of an item the whole-text rule says must be
omitted. Keeping a prefix would mean the policy trusts the client screen for that tail. That is the operator's call,
not this schema's, and v1 does not make it. **Measured exposure** (txxxx, msg/<key>; HQB, 278
main-session Claude transcripts since 19 Sep, this serializer plus 700 bytes of maximal metadata): assistant_text
n=115,183, p99 2,867 B raw (3,606 serialized), max 22,844 serialized, 0 over 65,536; user_text n=23,216, p99 9,768 B
raw, max 61,763 serialized, 0 over 65,536, 14 over 32 KiB (pasted documents). So `too_large` should be close to never
for assistant items and occasional for user turns. **Codex** (txxxx; HQB, 366 rollout files since 19 Sep, same
serializer): assistant_text n=6,365, max 59,221 serialized, 0 over; user_text n=1,618, p99 23,738 B raw, max 110,035
serialized, **4 over 65,536 (0.25%)**. Those four are dispatched briefs and handovers sent as one prompt.
**Antigravity** (a reviewer, msg/<key>; HQB, 253 session files, same serializer):
assistant_text n=4,011, p99 7,043 B raw, max 34,675 serialized, 0 over; user_text n=564, max 207,468 serialized,
**13 over 65,536 (2.30%)**. All 13 are subagent and benchmark prompts with pasted source or system prompts. Kimi is
still unmeasured, and so are artifact and task payloads.

**Known loss in v1, not a rare edge:** a Codex brief or handover over the budget reaches a resuming seat only as
`omitted, too_large` with its byte count. That is the item a reader most wants, and the price of the no-prefix rule
above. Section 10 carries it.
Tests: quotes, control characters, non-ASCII (2-, 3- and 4-byte), and maximal metadata, each at the budget (stored)
and one byte over (omitted, too_large).

**A screen hit: `omitted`, with nothing derived.** The writer runs both screens on the whole text before writing.
On a hit, it writes `item: omitted` with `omitted_reason` and NO `text`, NO `text_sha256`, NO byte count and nothing
else derived from the dropped text. If the writer's screen misses and the SERVER refuses the write, the writer writes
the same `omitted` item for that ordinal. **Only two codes become an omission:** `E_CONTEXT_SECRET` (reason
"secret") and `E_CONTEXT_CLIENT_NAME` (reason "client_name"). A fail-closed refusal, where the detector is
unavailable or errors, is a DIFFERENT code, `E_CONTEXT_SCREEN_UNAVAILABLE`, and the writer RETRIES it later and never
writes an omission for it. Write-once makes an omission permanent, so treating an outage as a hit would drop clean
text for good. Any other refusal is a conflict the writer reports. A writer killed between the
refusal and the omitted write leaves nothing readable for that ordinal. On restart it recomputes the same item, the
server refuses it again (the screens are deterministic), and it writes the omitted item. So the outcome converges
with no text ever stored, and no clean-up call is needed for safety.

Every stored text is therefore a WHOLE item the server itself screened. There is no path where a server-unscreened
byte of an item's text, or a digest of one, becomes readable.

Subagent (sidechain) files contribute only `artifact` items with `agent_id` set; their prose is not dialogue.
**What an artifact holds (decided 2026-10-19, kxxx to txxxx, built in #348):** ONE artifact item per subagent
source, its FINAL report: the last non-empty assistant text block of the sidechain, whole, screened like any text,
emitted when the uploader declares the source complete. `agent_id` is the 32-hex sha256 prefix of the subagent id;
`native_ref` and `time` are those of that final record. Nothing else from the sidechain is stored: no intermediate
prose, tool calls or plan items. A subagent with no final text gives no item (the parser reports it as skipped,
"no_final_text"); a final report over budget or hit by a screen gives `omitted` as any text does. The parent seat
acted on what the subagent returned, and that is what a resuming seat needs.

**A record with no usable time takes no ordinal.** It is skipped and reported, and the next item takes the next
ordinal, so ordinals stay gap-free (a gap would read as a missing item, `E_CONTEXT_MANIFEST_ITEM_MISSING`).

**Lone surrogates (#322).** A string holding a lone UTF-16 surrogate cannot be encoded as UTF-8, so a naive digest
raises instead of refusing. The writer replaces each lone surrogate with U+FFFD before screening, hashing and
freezing the bytes, so the frozen bytes are the cleaned ones. The server refuses any value that still holds one with
the typed code `E_CONTEXT_TEXT_ENCODING`, never a 500.

**Item granularity (txxxx, parser owner).** An assistant item is one TEXT BLOCK, not one message: a Claude
assistant record can hold several text blocks between tool_use blocks, and separate items keep that order with the
artifact items between them. Items of one record share its `time` and `native_ref`, so **`time` is not unique within
a source: `ordinal` is the order key, and readers must never sort by time.**

**Task items: structured in v1 (txxxx, parser owner; the b2d1a0 rule).** Every `item: task` carries:

| Field | Type | Rule |
|---|---|---|
| `step` | string <= 1,000 | required: the step text as written; screened like `text` |
| `event` | "added" \| "completed" \| "removed" \| "ambiguous" | required |
| `counts` | `{pending, in_progress, completed}`, ints >= 0 | ONLY when `event` is "ambiguous": the counts for that step text after the change |
| `text` | string | required: a one-line rendering for display, screened like any text item |

There is one item per event, so one plan change can produce several items in a row. A reorder produces none, and a
reword produces "removed" then "added". The server checks the enum, the shape of `counts`, and that `counts`
appears only on "ambiguous". A removal is never reported as "completed". Tests: a duplicate-text plan, a reorder
(no items), a reword, and the ambiguous case.

`step` is free text, so it is under EVERY rule `text` is under (codex-kxxx-cli 26354e, txxxx):
- **Admission:** both screens run on the whole `step` AND the whole `text`, in the one admission write. A hit in
  either makes the whole item `omitted`, with no `step`, `text`, `event`, `counts`, digest or anything else derived
  from it.
- **Budget and freeze:** the 65,536-byte budget is the complete serialized task value, every task field included.
  The whole task payload is frozen with the other final bytes in the local atomic checkpoint, and is never
  regenerated on a restart or a retry.
- **Redaction:** strips `step`, `event`, `counts`, `text` and every digest from every version (section 8).
Tests: a synthetic marker only in `step` with a clean `text` (omitted, nothing kept); an admitted synthetic task
then redacted, checked by direct read, list and query, earlier versions included, showing no task text or digest.

## 6. Manifest (what makes a batch complete)

| Field | Type | Rule |
|---|---|---|
| `schema` / `kind` | "context.v1" / "manifest" | required |
| `seat`, `src`, `gen`, `batch` | as the key | required |
| `first`, `last` | ordinals | required: the batch covers ordinals `first..last`, contiguous, at most 256 items |
| `items` | list of `[ordinal, text_sha256 or null]` | required: one entry per ordinal `first..last`, in order; null for an `omitted` item |
| `counts` | `{written, omitted_secret, omitted_client_name, omitted_too_large}` | required; must agree with `items` |
| `redacted` | list of ordinals | set ONLY in the new manifest version that `memory.redact` writes (section 8). A manifest carrying `redacted` is refused by `memory.write` at any version: it is a known field, so the unknown-field rule would not catch it |

- **Every ordinal has an item.** A refused item is an `omitted` item (section 5), so a batch whose every item was
  refused still has a manifest with all-null entries and its counts. There is no empty manifest and no silently
  skipped batch.
- **Validation at the manifest write.** `enforce_context` reads every item key `ctx/<seat>/<src>/<gen>/<batch>/<o>`
  for `o` in `first..last`. It refuses the manifest when one is missing, when an entry's digest differs from the
  stored item's, or when the counts disagree. The batch is in the key, so a manifest can only ever name its own
  batch's items.
- **Strict successor order, source-wide contiguity.** Batch 000001 must have `first = 0`. Every batch n > 1 requires
  batch n-1's manifest to be committed, with `first` = its `last` + 1. So ordinals are contiguous across the whole
  generation, with no gap and no overlap. The check runs in the manifest's write transaction while it holds a row
  lock on the predecessor's manifest (`SELECT ... FOR UPDATE` on `ctxm/<seat>/<src>/<gen>/<n-1>`). A manifest that
  arrives before its predecessor is refused, and the uploader commits manifests in batch order and retries. Two
  concurrent manifests for different batches serialize on their predecessors, and two for the same batch number
  are settled by write-once.
- **What the manifest is for.** The manifest marks a batch complete and in order, so a reader knows it has not
  stopped mid-batch. It is NOT the screening control: every readable item already passed both screens in its own
  write (section 3). An item without a manifest yet is admitted text, simply not confirmed complete. Readers that
  rebuild a dialogue use committed manifests.

## 7. Server enforcement (mirrors `apps/cards/validation.py`)

A new `apps/context/validation.py` with the cards' two halves, called from `apps/memoryskills/handlers.py` for this
namespace exactly where `validate_card` is called for the card namespace:

- `validate_context(ns, key, value)`, no database: the key shape, every field above with its type and limit, the
  65,536-byte serialized budget, the `text_sha256` recomputation, `agent_id` only on artifacts, `omitted_reason` only
  on omitted items, `omitted_bytes` only with reason "too_large", `redacted` refused, lone surrogates refused
  (`E_CONTEXT_TEXT_ENCODING`), and the 5-minute `time` skew. An UNKNOWN FIELD IS REFUSED (a new field is a reviewed
  server change, as for cards). NO SCREENS here.
- `enforce_context`, beside the row, run AFTER `check_permission` (handlers.py:1000) and the product-plane ban: the
  key's seat is the caller's; strictly version 1, no exception; for a manifest, the validation of section 6. **Both
  screens run here, and only here**, fail closed (`E_CONTEXT_SECRET`, `E_CONTEXT_CLIENT_NAME`;
  `E_CONTEXT_SCREEN_UNAVAILABLE` when the detector cannot answer). The order matters. `validate_card` runs before
  the permission check (handlers.py:934), which is harmless for cards. For context, a caller with no write grant, or
  a product tenant, could otherwise tell a screen code from `E_PERMISSION` and probe the client-name denylist.
  (memory-bxxxxx, msg/<key>.)

Tests, each with a positive control in the same setup:
- every refusal above;
- a secret and a client name at the start, middle and end of a 60,000-byte item, each refused, with a direct
  authenticated `memory.read`, `memory.list` and `memory.query` afterwards showing nothing stored for that key;
- a writer killed right after a refusal, then restarted: the same ordinal converges to an `omitted` item, and
  nothing else is readable in between;
- the budget cases of section 5;
- a rewrite to generation 2, a crash and retry, and a replayed uploader, each yielding no overwrite and the same keys;
- two manifests for one batch (the second refused by write-once); a manifest whose `first` is not its predecessor's
  `last` + 1, refused; batch 2's manifest arriving before batch 1's, refused, then accepted after it; two adjacent
  manifests committed concurrently; a drop-only batch, accepted with all-null entries;
- the cut boundary (1d0f98's synthetic pair): an item one byte over the budget, with a denied marker wholly after the
  point where a prefix would end, and one crossing it, with the writer's screen disabled. Each becomes `omitted,
  too_large` with no text or digest. After a writer death at that point, direct `memory.read`, `memory.list` and
  `memory.query` show no text and no digest of either item;
- a version-2 write refused; an `agent_id` on assistant_text refused;
- a product-plane caller and a caller without a grant, each posting a text with a denylisted term: each gets the
  plane or permission refusal, never a screen code;
- the detector made to fail: the refusal is `E_CONTEXT_SCREEN_UNAVAILABLE`, and the writer writes NO omitted item;
- two concurrent first writes of one item key with different call_ids: exactly one row, the other `E_WRITE_CONFLICT`,
  settled by the uploader's byte-compare;
- a manifest carrying `redacted` through `memory.write`: refused at version 1, and refused as a version 2;
- a lone surrogate in `text`: `E_CONTEXT_TEXT_ENCODING`, not a 500;
- one seat's attempt to redact another seat's item: refused;
- two items of one batch redacted one after the other, then a direct `memory.read` of EVERY manifest version showing
  neither digest; the same with the two redactions concurrent;
- a COLD reader (no cached earlier version) validates the latest manifest after one, two sequential and two concurrent
  redactions, through the server-signed read result. It refuses a version > 1 whose provenance names no redaction
  record, one whose redaction request was signed by a seat other than the item's own and not the operator, and a read
  result whose server signature does not verify. It never fetches or stores the withdrawn text or its digest;
- server side: after each of those redaction sequences, every untouched entry is byte-identical to the pre-redaction
  version 1.

**Implementation (validator PR, stacked on this document):**
- The screens live in `apps/context/screens.py`.
- **Secrets:** the server runs the loader's OWN `DETECTORS` list (`tools/transcript_cards/transcript_cards.py`,
  txxxx's), imported on first use, so the server and the loader cannot disagree about what a secret is. A test pins
  that it is the same object. Production runs from a full git worktree, so `tools/` is present.
- **Client names:** a JSON list of markers in the file `MESHY_CONTEXT_DENYLIST` names. The file is server
  configuration, never in the repository. Text and markers are compared after NFKC, case folding and keeping only
  letters and digits, so `Zor blax-Corp` matches `zorblaxcorp`. A marker under 4 such characters refuses the whole
  file.
- **Unavailable:** a missing, unreadable, empty or refused denylist, or detectors that cannot load, return
  `E_CONTEXT_SCREEN_UNAVAILABLE` on any text, clean text included.
- **Error codes** (registered in `apps/memoryskills/errors.py`, or they reach the caller as `E_INTERNAL`):
  `E_CONTEXT_KEY`, `E_CONTEXT_SCHEMA`, `E_CONTEXT_TIME`, `E_CONTEXT_DIGEST`, `E_CONTEXT_TOO_LARGE`,
  `E_CONTEXT_TEXT_ENCODING`, `E_CONTEXT_SEAT_MISMATCH`, `E_CONTEXT_WRITE_ONCE`, `E_CONTEXT_MANIFEST`,
  `E_CONTEXT_MANIFEST_ORDER`, `E_CONTEXT_MANIFEST_PREDECESSOR`, `E_CONTEXT_MANIFEST_ITEM_MISSING`,
  `E_CONTEXT_SECRET`, `E_CONTEXT_CLIENT_NAME`, `E_CONTEXT_SCREEN_UNAVAILABLE`, `E_CONTEXT_REDACT_UNAVAILABLE`.
- **Retry or stop (memory-bxxxxx):** an uploader resends the same bytes on `E_CONTEXT_SCREEN_UNAVAILABLE`,
  `E_CONTEXT_MANIFEST_PREDECESSOR` (batch n-1's manifest is not committed yet) and `E_CONTEXT_MANIFEST_ITEM_MISSING`
  (an item of the batch is not committed yet). `E_CONTEXT_MANIFEST_ORDER` (a range that does not follow its
  predecessor) and `E_CONTEXT_MANIFEST` (a digest, count or version that disagrees with the stored items) are final:
  the manifest itself is wrong, and the uploader reports it rather than resending.
  **The retry is bounded (memory-bxxxxx).** PREDECESSOR and ITEM_MISSING can be permanent: the item or
  manifest they wait for may have been refused with a final code, or never sent after a crash. So an uploader retries
  them at most 5 times with backoff, up to a minute in all, and only while the write it waits for is still pending.
  If that write was refused with a final code, or the retries run out, it stops, reports the batch as incomplete with
  the code, and never sends that manifest again.
- **A lone surrogate** cannot reach the server in a signed call: RFC 8785 refuses it before the client signs. The
  server's `E_CONTEXT_TEXT_ENCODING` is a backstop for any other path.
- **Detector speed (was the blocker):** three of the loader's detectors (`named_secret`, `password_assignment`,
  `url_credentials`) were quadratic on long runs of letters and digits, up to 74 s for 64 KB measured on HQD (kxxx to
  txxxx, msg/<key>). Fixed by #347; a test pins linear growth (4x the text, under 8x the time).
- Also refused: `memory.redact` on this namespace (`E_CONTEXT_REDACT_UNAVAILABLE`; not built, section 8), and
  `memory.prune` (retention is indefinite, section 10, so pruning is never built for v1).

## 8. Redaction

**NOT BUILT, by operator decision, 2026-10-19 ~15:30a recorded decision in kxxx's session (AskUserQuestion on the rollout
order): "no redaction necessary".** Redaction does not gate production writes, and `memory.redact` keeps refusing
this namespace. A leaked item can then be removed only by a database administrator. The design below is kept as a
record, in case the operator asks for it later.

`memory.redact` exists on production (the `skills` table lists it, checked 2026-10-19). Today it writes a tombstone
over each row in place and keeps `ROUTING_FIELDS` (plus `CARD_FIELDS` for a card; redact.py:65, 93), and it touches
one key. **This section is NEW CODE in that operation, not existing behaviour** (memory-bxxxxx):
1. **Who may redact:** the item's own seat (the caller's server-stamped seat equals the key's `<seat>`) or the
   operator. `handle_redact`'s namespace "write" check (redact.py:170) is not enough, because every seat writes this
   namespace and could otherwise redact any other seat's item.
2. **One transaction, one global lock order:** the item's versions ascending, then its manifest's versions
   ascending, then the insert of the new manifest version (step 4). The manifest is exactly one key, because the
   batch is in the key. Every context redaction takes locks in that order, so two redactions cannot deadlock.
3. **The item** is tombstoned in place by a new `CONTEXT_FIELDS` branch of `tombstone()`: it keeps the key fields and
   `item`, and loses `text`, `text_sha256` and every task field.
4. **The manifest gets a NEW version on every redaction**, written by the server inside `memory.redact` (never
   through `memory.write`, whose version-1 rule is untouched). It is the head with that item's digest set to null and
   its ordinal added to `redacted`, and the previous manifest version is then redacted. In-place tombstoning of the
   manifest does NOT work (memory-bxxxxx, msg/<key>):
   - `shared_memory_redaction.memory_id` is UNIQUE (sql_meshychat/027:34), so a row can be redacted once;
   - `handle_redact` skips a row that already has a record (redact.py:207-221);
   - so the second redaction in one batch would leave its item's digest readable;
   - and `_redact_one` nulls `signed_bytes`, which would leave the manifest's other entries unverifiable.
   With a new version each time, every manifest row is redacted at most once.
5. **After a redaction, the trust boundary is the SERVER (codex-kxxx-cli 016d78, memory-bxxxxx).** No
   writer-signature chain survives a redaction, and this contract does not pretend one does:
   - `tombstone()` drops the entries of version n-1 in the same transaction, so a cold reader has nothing to compare
     version n against;
   - `_redact_one` nulls `signed_bytes` on every version it redacts, version 1 included, so after the first redaction
     in a batch no manifest version carries a seat signature. The redaction record keeps only a HASH of the original
     signed bytes, and a hash cannot be verified as a signature.

   So the reader contract is:
   1. **Unredacted version 1:** checked as today, by the seat's own signature.
   2. **Version n > 1:** accepted only from an authenticated `memory.read` whose RESULT is server-signed
      as the client's read path already checks it (`tools/meshy_node/wire.py` `result_signature_ok`; codex-kxxx-cli
      0c1ddf). The signature covers the RFC 8785 `result_payload`: kind, call_id, status, result and the optional
      error pair. `node.invoke` checks the fresh call_id, and the row is bound by its namespace, key and version
      inside the signed `result.memo`. `result_signing_kid` only SELECTS the public key in the server's trusted
      JWKS; it is not itself a signed field. That signed result is the server's attestation of the current manifest. The reader also checks that the row's provenance names a redaction record, and that the
      record's `request_signed_bytes` verifies as the item's own seat or the operator (step 1).
   3. **"Every other entry is unchanged" is a SERVER invariant, not a reader check.** `memory.redact` builds version
      n from n-1 in the same transaction, nulling exactly one entry. Tests pin it: one, two sequential and two
      concurrent redactions, each asserting through a scratch server that every untouched entry is byte-identical to
      the pre-redaction version 1, and that no digest of a withdrawn item is readable anywhere.

   **DECIDED by the operator, 2026-10-19 ~08:00a recorded decision in kxxx's session (AskUserQuestion): "Server-signed
   read".** The server's signed read result, plus the redactor's own signed request, is the trust boundary for a
   redacted manifest. There is no new signing code and no new use of the server key. The alternative (a server ROW
   signature via `server_result_signer`, as `chain_head.py` signs chain heads) was offered and not chosen.
   Unredacted manifests are unaffected.
6. No digest of the removed text remains anywhere: not in any manifest version, not in a log.

A reader that read the manifest and then the item, with a redaction landing between the two reads, sees an item
without text or digest. It treats that as redacted, re-reads the manifest's head, and never reads it as corruption.
An uploader never re-uploads a redacted item: its key exists, so a write is refused, and the byte-compare read shows
the redacted state. It records the item as redacted, not as failed.

**Two hazards the redaction PR must handle** (memory-bxxxxx, forward P2s):
- **The head re-check.** The redaction transaction must re-check `is_head` under `FOR UPDATE` after it takes its
  locks, as `enforce_card` does. Otherwise it can redact a version that a concurrent write has already superseded.
- **A manifest after a redaction.** An item redacted BEFORE its batch's manifest is written leaves the writer unable to
  commit a manifest whose entry matches the stored item, since the digest is gone. The manifest check must accept a
  redacted item's entry as null, with the ordinal in `redacted`, written by the same server path. Otherwise one early
  redaction blocks the rest of the generation.

## 9. Rollout

1. Review: this document (txxxx for the parser fit, memory-bxxxxx as implementation reviewer, codex-kxxx-cli for the
   queue fit), then the validator PR with its tests.
2. Scratch server only, with synthetic fixtures, until step 4.
3. Two independently authenticated readers rebuild the same fixture dialogue and agree on keys, versions, digests and
   order (codex-kxxx-cli's agreement test).
4. Operator go on the namespace claim and the grants (every seat reads, each seat writes its own). Already decided
   (operator, 2026-10-19 ~15:30a recorded decision in kxxx's session): retention indefinite, grants with no expiry, and no
   redaction step before the go. The go itself is still to be asked, after step 3.

## 10. Open, with owners

| Open | Owner |
|---|---|
| ~~Retention~~ DECIDED 2026-10-19 ~15:30Z (a recorded decision in kxxx's session): "Keep indefinitely"; grants "No expiry"; rollout "no redaction necessary" | closed |
| Item size for Kimi, and for artifact and task payloads (Claude, Codex and Antigravity measured: section 5) | txxxx, with nxxxxx for Kimi |
| KNOWN LOSS: oversized user turns (0.25% of Codex user items, the briefs and handovers; 2.30% of Antigravity user items, pasted prompts) are `omitted, too_large`. A later version could store them whole under a larger per-item budget, or keep a prefix if the operator accepts trusting the client screen for the tail | operator (policy), kxxx (design) |
| Graph indexing of committed items (not in v1; v1 is read by key); a 64 KiB item is many graph chunks | kxxx, after v1 |
| Readers who are not seats (none in v1) | operator |

## Changes in r2 and r3

| Finding | Fix | Section |
|---|---|---|
| r1-1. A rewrite kept src and keys, so it collided with generation 1 | `<gen>` in every key; local-to-wire mapping; atomic allocation; retries and replays give the same keys and bytes | 4 |
| r1-2. 6,000 text bytes do not bound an 8,192-byte JSON value | the budget is the complete serialized value with the server's serializer; one that does not fit is `omitted, too_large` (r4), never cut; budget tests | 5 |
| r1-3. Contradictory omission fields and parts limit | r3: no parts; r4: no prefixes either; `omitted` carries only its reason (and its size for too_large) | 5 |
| r1-4. The screen was deferred to the manifest while parts were readable | r3: one write per item, screened whole in that write | 3, 5 |
| r1-5. Redaction left digests on siblings and manifests | one item, one manifest (batch in the key), redacted in one transaction including old manifest versions | 8 |
| r2-1. A marker spanning three or more short parts passed every `lead` check | r3: there are no parts, and the detector sees the whole item | 3, 5 |
| r2-2. A writer killed after a refused part left earlier parts readable | r3: a refused write stores nothing; the ordinal converges to `omitted` on restart, with no clean-up relied on | 5, 7 |
| r2-3. Batch membership was not represented | the batch is in the item key; one manifest key per batch, write-once | 4, 6 |
| r2-4. A drop-only batch had no valid manifest | every refused item is an `omitted` item, so every batch has a full manifest | 5, 6 |
| r3-1. A retained prefix was screened server-side without its dropped tail (identical values for clean and dirty tails) | v1 never cuts: an oversized item is `omitted, too_large` with only its size; keeping prefixes is left to an operator policy decision in a later version | 5, 6, 7 |
| m-a 1. Screens in the no-database half would run before the permission check and let anyone probe the denylist | screens only in `enforce_context`, after `check_permission` and the plane ban | 7 |
| m-a 2. Which refusals become an omission | only `E_CONTEXT_SECRET` and `E_CONTEXT_CLIENT_NAME`; `E_CONTEXT_SCREEN_UNAVAILABLE` is retried, never omitted | 5, 7 |
| m-a 3, 4. A redaction exception in the write path; `redacted` writable | `memory.write` is strictly version 1; `redacted` is refused through `memory.write` at any version | 3, 6, 7 |
| m-a 5. Lone surrogates raised instead of refusing (#322) | the writer cleans them before freezing; the server refuses with `E_CONTEXT_TEXT_ENCODING` | 5, 7 |
| m-a 6. The race code | `E_WRITE_CONFLICT` is named in uploader obligation 3 as re-check, not re-send | 4 |
| m-a 7. Section 8 described as existing redact; any seat could redact | marked as new code: who may redact, the global lock order, an in-place `CONTEXT_FIELDS` tombstone of the item | 8 |
| m-a 8 (6501b2). An in-place manifest tombstone fails from the second redaction in a batch (one redaction record per row; already-recorded rows skipped) and nulls the signature | a new server-written manifest version on every redaction, the previous version redacted; sequential and concurrent two-item tests | 8 |
| m-a 9 (a5117e). "Server-signed" later versions named a signature that does not exist | replaced by the next row | 8 |
| cj 016d78 + m-a 83f4bb. Option (a)'s "differs by one entry" check cannot be run by a cold reader (n-1 tombstoned, every seat signature nulled after the first redaction) | stated plainly: after a redaction the trust boundary is the server's signed read result; "untouched entries unchanged" is a server invariant pinned by tests; the operator chose the server-signed read (2026-10-19) over a new server row signature | 8 |
| r3-2. Checking overlap with the previous batch only let a later manifest land before its predecessor | strict successor order, `first` = predecessor's `last` + 1, under a row lock on the predecessor manifest; reversed and concurrent tests | 6, 7 |
