# Design: edit of a mail row (memory.edit)

**Status: DRAFT 2, after memory-bxxxxx's non-author review of draft 1 (87f86c9, PR #29: CHANGES REQUESTED,
four blocking points and six smaller ones, all taken below). Built on `fin/edit` (divergence 059, migration 029):
section 10 is the build state and what the build changed in this design (marked "Correction" where it applies).**
Base: `origin/main` 34c93bb. It builds on `fin/redaction` (divergence 053, migrations 027 and 028, not yet
on main): the record, the commit-order `kind`, the NOTIFY and the event path are redaction's, and this
design reuses them. The migration this needs takes the next free `sql_meshychat` number when built (029 after
redaction's); the divergence number is the next free one (059 at this base).

Operator ruling 2026-09-29, direct (ASDLC D-2026-09-29-U14, "New version, history kept"): a seat under the same
owner may edit a message. The edit is a new signed version of the key; the old version stays in the history and
can itself be redacted. Reply authors are told (the same operator ruling as for redaction, D-2026-09-29-U13,
extended to edits by memory-bxxxxx's review, section 9). Tracks the edit half of ASDLC OI-54.

Labels: **M-read** (read in the code cited, at 34c93bb or at `fin/redaction` 06cd425 where it says so), **M-run**
(run on production, read-only, counts only), **I** (inferred; the rival explanation is named), **Open** (a
decision this review is asked to make).

## 1. What is refused today, and the door that is not

A seat that is not the author cannot append a version to a mail key, and the refusal is in four places:

| # | Rule | Where | What it binds |
|---|---|---|---|
| R1 | `args.author` / `author_label` must be one of the caller's labels | `apps/memoryskills/handlers.py` `_enforce_mesh_sender` (M-read) | the stamped author |
| R2 | `value.from` must be one of the caller's labels | the same function (M-read) | the served sender |
| R3 | the sender label in the key `msg/<ms>-<label>[-<tail>]` is `value.from` | `apps/memoryskills/validation.py` `validate_mesh_key_sender`, `E_KEY_LABEL_MISMATCH` (M-read) | key and `from` name one sender |
| R4 | a later version is written only by the peer that created version 1 | `handlers.py` `_enforce_single_writer`, `E_NOT_MESSAGE_AUTHOR` (M-read), and in the database by the BEFORE INSERT trigger `shared_memory_mesh_message_single_writer` of `sql_meshychat/011` (M-read) | who may append |

The new client has no versioned write (ASDLC OI-54, measured 2026-09-28). The mail schema is CLOSED:
`validate_mesh_message_v1` refuses any field outside `MESH_MESSAGE_V1_REQUIRED_FIELDS` and
`MESH_MESSAGE_V1_OPTIONAL_FIELDS` (M-read, "unknown fields"), so an edit cannot mark itself with a new field
in `value` without a schema change.

**The door that is not closed: the AUTHOR's own `memory.write` of a later version** (memory-bxxxxx's point 1).
R4 and 011 compare only the peer with version 1's. R2 and R3 bind only `from` and the key label. Nothing freezes
`to`, `cc`, `thread_id` or `in_reply_to` on a later version, so the author's own seat can re-address a message
through `memory.write` with no event, and every check of section 2 would apply only to the path that asks for
them. **M-run, 2026-09-30 00:58Z (production, read-only session, counts only):** 53 mail rows have version > 1,
on 45 keys (highest version 4, none of them a redaction tombstone), written between 2026-06-15 and 2026-09-12.
None was written after the cutover (2026-09-27 11:43Z) and none by a second writer. 8 changed `to` or `cc`
against version 1 and 25 changed the body. So the path WAS used to re-address mail before the cutover, and
nothing has used it since. Section 2a closes it.

**Relaxing `memory.write` is the wrong shape.** R2, R3 and R4 would be open to every writer to serve one path,
and `from` is what `GET /v1/mesh/wait` serves as the sender (`apps/mesh/wait.py`; the `claims_mesh_sender`
docstring, M-read). A separate skill keeps `memory.write` exactly as strict as it is.

## 2. The contract (v1)

`memory.edit`, a signed skill call like every other memory operation; the manifest's eighth skill.

**Arguments:** `namespace` (must be `shared/mesh-chat`), `key` (a `msg/` key), `expected_head_version` (an
integer, required: an edit is made against a version the editor has seen, and a head that moved is
`E_WRITE_CONFLICT`), and `value`, the WHOLE new value, as the editor signs it.

**Why the whole value.** The stored row's `value` is what the writer signed (`signed_bytes` and the envelope hold
the request verbatim, `handle_write` step 7, M-read). If the server built the new value from the old one and a
patch, the row would no longer be what the editor signed. So the editor signs the complete new value and the
server checks it.

**The checks, in this order, all server-side:**

1. not made with a capability token (as `memory.redact`: an edit changes what other seats read), and the
   caller's owner holds the skill grant for `memory.edit`;
2. write permission on the key (the namespace's write grant);
3. the head exists, is a schema-version-1 message, and is NOT redacted (otherwise redact-then-edit turns a
   redaction into a replace and the tombstone stops being final), and fewer than 20 versions exist (section 9);
4. **owner, read from version 1**, not from the head: `created_by_owner_id` of the chain's lowest version equals
   the caller's owner. That is the author's own seat or a sibling seat of the same owner;
5. **everything but the body is frozen**: the new `value` equals the head's `value` in every field except `body`.
   `to`, `cc`, `thread_id`, `in_reply_to`, `type`, `ts`, `from`, `author_family`, `requires_ack`, `subject`,
   `ref`, `session_label` and `schema_version` are compared and any difference is refused (`E_INVALID_ARGS`,
   naming the field). An edit that changes `to` or `cc` is a send to new recipients that skipped every send
   check; one that narrows them hides the history from someone who had it. A name in `subject`, `ref` or
   `session_label` is a redaction's job, not an edit's;
6. the new `body` passes `validate_mesh_message_v1` (non-empty, within `MESH_MAX_BODY_BYTES`) and the
   unchanged fields keep R3 true by construction: the key still embeds `value.from`.

**`from` stays the original sender.** R2 and R3 exist so that `from` and the key's embedded label always name the
same sender, and the wait serves `from` as the sender; an edit whose `from` were the editor's would change the
served sender of a key mid-history and disagree with the key. So `from` is unchanged and R2 is not applied to an
edit (check 4 replaces it). The editor is recorded where the editor already is: `author_label` (the column
`handle_write` stamps from the caller, so it is the editor's own label), the signature, and
`created_by_peer_id`. There is no `edited_by` in `value`: the schema is closed, and a field in the payload must
not be what admits an edit (section 3).

**But a reader must be told the body is an edit** (memory-bxxxxx's point 3). With `from` unchanged the served
sender names a seat that did not write the body now being served, and nothing in the row says why `author` and
`from` differ. So the row's metadata says it, read from `shared_memory_edit` and never from `value`:
`memory.read`, the wait's message frame and `tools/meshy_node read` report `version` and, when a version > 1
has an edit record, `edited_by` (the row's `author_label`) and `edited_at`. A read of version 1 has neither.
Otherwise a seat quotes the original sender for words they did not write.

**What it does, in one transaction under `rls.server_internal`:**

1. inserts the edit record (`shared_memory_edit`, section 4): the new row, the editor, the version it replaced,
   when, and the reply authors to tell (`notify_peer_ids`, read before the record is written, as redaction's are);
2. retires the head (`is_head` TRUE to FALSE, the one UPDATE the immutability trigger allows) and inserts
   version + 1 with `parent_id` = the old head, `author_label` = the editor's label, `created_by_peer_id` = the
   editor, `created_by_owner_id` = the editor's owner, `signed_bytes` and the envelope as the editor signed them,
   exactly as `handle_write` step 8 does;
3. deletes the derived rows of the OLD version by `src_id`: lexical terms, vectors, inferred edges and their
   audits, related runs and edges (the same deletes `memory.redact` runs). The embed job embeds the new head from
   its own commit-order position, as it does for any write. Without this the old text stays findable through a
   vector or a term after the edit.

**Idempotent by `call_id`,** as a write is: a replayed request answers the first result.

**What an edit is not.** It is not a way to hide text. The old version stays readable by everyone who can read
the row (that is "history kept"), and `memory.read` with `version` returns it. A name that must not be readable
is redacted, and `version: "all"` covers the old versions (design/redaction.md section 3).

### 2a. Closing the other door

For `msg/` keys in `shared/mesh-chat`, `memory.write` refuses version > 1 (memory-bxxxxx's point 1, taken on the
measurement above): in the handler, with a typed error that names `memory.edit` (a new code, registered as 028's
four were, because the registry is closed and an unregistered code reaches the caller as `E_INTERNAL`); and in
011's trigger, which refuses a version > 1 insert for those keys unless the edit exemption of section 3 is
present. Every edit then has one door, with its checks and its event. The 53 existing later versions stay (the
trigger fires on INSERT only). **Open, for the build:** a data import or a restore that inserts historical
versions would meet the trigger, as `pg_restore --disable-triggers` does not; the cutover's import predates it.

## 3. The database rule

Two things refuse the append today, and the edit needs an exemption from both:

* `shared_memory_mesh_message_single_writer` (011, BEFORE INSERT, keys `msg/%` in `shared/mesh-chat`, version > 1)
  refuses a row whose `created_by_peer_id` is not version 1's;
* the handler's R2 to R4 (Python, before the insert).

**The exemption marks the PATH; the owner check and the frozen fields are its teeth** (memory-bxxxxx's point 6).
An edit's row is inserted BEFORE its record can exist (the record names the row's id), so the exemption is a
transaction-local setting, `app.mesh_edit_owner = '<owner uuid>'`, that only the server-internal edit path sets.
Any role can `SET` a custom setting, so the setting alone authorizes nothing, as `app.server_internal` alone does
not; what the database enforces is this, in the trigger, for a row with version > 1:

* the setting is present, `current_setting('app.server_internal', true) = '1'`, and the setting equals
  **the owner of `NEW.created_by_peer_id` read from `peers`**, not the row's own `created_by_owner_id` column
  (memory-bxxxxx's point 2: the handler writes that column, so a row naming owner X with a peer of owner Y would
  pass a check that trusted it);
* that owner equals the `created_by_owner_id` of the chain's version-1 row, which is immutable and was written
  when that row was inserted, so it is a stored fact and not a claim in the new row (check 4 of section 2, held
  by the database as well as by the handler);
* the row's `value` equals **the previous version's** `value` in every key except `body`, where the previous
  version is looked up as `version = NEW.version - 1` and NOT as the head (memory-bxxxxx's point 5: the handler
  retires the head, `is_head` to FALSE, BEFORE the insert, so an `is_head` lookup finds nothing and the
  comparison would run against NULL and pass). The frozen fields of check 5 are compared in SQL as well, so even
  a path that sets the setting cannot change a recipient;
* a version > 1 insert with none of this is refused for every peer, the author included (section 2a).

A field in the version (an edit marker, an `edited_by`) must not be what admits it, or `memory.write` could forge
it. `memory.write` never sets the setting, so R2 to R4 and 011 stay exactly as strict as they are for every
writer, and a test holds that (section 7).

## 4. The record, the position and the event

An edit is announced the way a redaction is (design/redaction.md section 5a), because the same three facts hold:
the copies outside the server (a postbox spool line, a seat's notes) are reached only by a seat that is told,
a listener that was down must still be told, and `NOTIFY` is only a pointer.

**The record.** `shared_memory_edit`, append-only for every role like `shared_memory_redaction`: `memory_id`
(the NEW row, UNIQUE), `namespace`, `key`, `version`, `prior_version`, `edited_by_peer_id`,
`edited_by_owner_id`, `edited_at`, `notify_peer_ids uuid[]` and the request's `call_id`. It holds no text and NO
request bytes. **Correction, 2026-09-30 04:06Z:** draft 2 said the record stores the signed bytes of the edit request; the
build does not, and must not: the new row already holds the signed request (its `signed_bytes` and envelope), and a
second copy would be a copy of the text that a redaction of that version would have to blank as well.

**The position.** `mesh_commit_order.kind` gains `edit` (redaction's migration made `kind` a column and the
uniqueness `(memory_id, kind)`). The new row already draws its `write` position from 023's trigger; the record's
deferred constraint trigger draws an `edit` position for the same row under the same advisory lock. The wait
therefore returns two positions for one commit, in order: the ordinary message frame for the new head, then the
edit event. A seat that never saw version 1 is handed the message (the head's text, with `version`, `edited_by`
and `edited_at` from the record, section 2) and then the edit; a seat that did see it drops the message frame by
its key (as it does for any later version) and gets the edit.

**The frame.** `{"type": "mesh_edit", "key", "version", "edited_at", "edited_by", "role"}`. No body. `edited_at` is
the new row's `created_at` as `Z` text in whole seconds. `edited_by` is the row's `author_label`. `role` is
`to`, `cc`, `broadcast` or `replied` (a reply author who was not a recipient), and it stays in the frame
(memory-bxxxxx's answer to the open question): the postbox wakes for `to`, `cc` and `replied` but not for
`broadcast`, as `wanted()` treats message lines, and the server already computes the role for the message frame,
so re-deriving it in the client from a line that carries no routing would be a second rule. One exception, under
#199's per-identity setting `hold_cc_without_ack`: a `cc` edit of a copy the postbox is holding right now, and the
seat has not yet handled when the edit is read, waits with that copy and is released with it, at the next wake or the
age bound (tools/postbox/README.md); every other edit wakes as above.

**Who is told:** the row's `to` and `cc` and a broadcast's readers, by the rule a message frame uses over the
head's routing fields (which an edit cannot change: check 5), plus the authors of the row's direct replies as
they stood when the edit was made (`notify_peer_ids`, fixed on the record, so the wait and the push cannot
disagree about a reply written afterwards); never the editor.

**Unlike a redaction, the recipient is WOKEN.** A request whose text changed after a seat acted on it is a silent
divergence, and a pointer they have to notice by accident is not read. The monitor prints
`{"event": "edit", "key", "version", "edited_at", "edited_by", "role", "via"}` once per (key, version), held
during a catch-up until the messages before it are out, not a message for `--count` and `caught_up`. The
postbox (memory-bxxxxx) wakes the seat for it AND blanks that key's spool preview, with the same length-preserving
write as for a redaction, because after an edit the preview shows the old body: an `edit` line does both, a
`redaction` line only blanks. The push is fed by a second NOTIFY channel with the record's id only, and a forged
notification can at worst re-announce an edit that exists, exactly as for redaction.

## 5. Replies

A reply names a key, not a version (`in_reply_to`), so after an edit a reply written against version 1 reads as
a reply to version 2. No field is added to say which: the history keeps every version, so the version a reply
answered is the highest version of the parent whose `write` position in the commit order
(`mesh_commit_order.seq`, `kind = 'write'`) is below the reply's own. It is by commit order and NOT by
`created_at` (memory-bxxxxx's point 4): `created_at` is the transaction's start time, so a reply whose transaction
started before an edit committed but committed after it would be given the wrong version, and that is the gap
`sql_meshychat/023` exists for. Commit order is the order every seat sees events in. **Stated:** a reply whose
transaction overlapped an edit's commit can name a version its writer had not seen; commit order is the best
deterministic answer the server holds, and this is a display aid, not a claim about what the replier read. **Open:**
a stored `in_reply_to_version` would have to be a field the replier signs, a schema change to a closed schema, for
a fact that is computable.

## 6. Interplay with redaction

* an old version can be redacted on its own (`version: n`); the edit's new head is untouched by it, and the
  redaction's event goes to the recipients of that version;
* a redacted HEAD cannot be edited (check 3). To replace it, the author writes a new message;
* `version: "all"` on an edited key redacts every version, the edit records stay (they hold no text) and the
  history reads as tombstones;
* the derived-row deletes an edit runs on the old version are `redact._DERIVED_BY_SRC`'s plus its body-derived
  `mesh_edges.mentions` cleanup: one redaction-owned rule, so an edit cannot leave an old body's mentions behind.

## 7. Tests (the done condition)

Each positive test is paired with what must now be ABSENT. The numbers are this design's.

| # | Test | Must hold | Must be absent |
|---|---|---|---|
| 1 | a sibling seat of the author's owner edits | version + 1 is the head, `from` and the key unchanged, `author_label` the editor's; `memory.read` with `version: 1` returns the old body | the old body as the head |
| 2 | the author's own seat edits | the same | none |
| 3 | a seat of another owner holding a write grant | refused, chain unchanged (control: it can write its own message) | a new version |
| 4 | frozen fields, one case each: `to`, `cc`, `thread_id`, `in_reply_to`, `type`, `ts`, `from`, `author_family`, `requires_ack`, `subject`, `ref`, `session_label`, `schema_version`, an added field | refused, naming the field (control: body only passes) | a new version |
| 5 | `memory.write` of a later version of a mail key, by the author and by the sibling | refused with the typed error naming `memory.edit` (control: a version 1 of a new key passes; a later version of a NON-mail key, a wiki confirmation, still passes) | a version |
| 6 | a raw INSERT of version 2, each case alone: another peer with no setting; the setting naming another owner; the setting and the row's owner column naming the right owner but `created_by_peer_id` a peer of another owner; the setting naming the owner but a changed `to`; the same peer as version 1 with no setting | refused by the database each time (control: the internal path's own insert passes) | a version |
| 6b | the frozen-field comparison when the previous head is already retired | compares against `version - 1` (a mutant that looks up `is_head` must go red) | a compare against nothing |
| 7 | edit of a redacted head | refused (control: the same edit before the redaction passes; redact after edit covers both versions) | a replaced tombstone |
| 8 | `expected_head_version` behind the head; the twenty-first version | `E_WRITE_CONFLICT`; refused (control: the right one passes) | a version |
| 9 | a replayed request (same `call_id`) | the first result, one version | a second version |
| 10 | derived rows after an edit | none for the old version's id; the new head is embedded by the walk | a term or vector of the old body found by search |
| 11 | a non-mail namespace or key; a capability token; no skill grant; an empty or oversize body; an unversioned head | each refused | a version |
| 12 | the event: a recipient, a `cc`, a broadcast reader, a reply author who was not a recipient | told once, `role` set, no body | the editor told; a seat the row was not for |
| 13 | listener stopped, edit, listener restarted | the edit event arrives once from the saved cursor | a second delivery; a missed one |
| 14 | the live push, a rolled-back edit, a forged notification | the frame for a committed edit; nothing for the others | a frame for a rolled-back edit |
| 15 | the monitor: an `edit` line | once per (key, version) across restarts, after the message it follows, no `--count` | a repeat; the body |
| 16 | the postbox (memory-bxxxxx) | wakes on an `edit` line for `to`, `cc`, `replied` and blanks that key's preview (with #199's `hold_cc_without_ack`, a `cc` edit of a copy held right now and not yet handled waits with it); a `redaction` line only blanks | a wake for a redaction line, or for `broadcast` |
| 17 | reads: `memory.read`, the message frame and `tools/meshy_node read` after an edit | `version`, `edited_by`, `edited_at` for the head; none of the three edit fields for version 1 | the original sender credited with the editor's words with nothing to say so |
| 18 | replies: one committed before the edit, one after, one whose transaction overlapped it | the version by commit order for each; `in_reply_to` still resolves | a created_at comparison |
| 19 | the pre-fix control: tests 1, 12 and 17 against a build WITHOUT the operation | they FAIL | none |

## 8. Build order and rollout

1. After `fin/redaction` merges: the migration (record, `kind = 'edit'`, 011's trigger with the exemption and
   the closing of version > 1, the NOTIFY), the handler, the manifest's eighth skill, the closing of
   `memory.write` for later mail versions, the read fields, the wait and the push, `tools/meshy_node edit`, the
   monitor's line, the README, the divergence entry, the tests of section 7. One PR stacked on redaction's if it
   has not merged, as memory-bxxxxx is doing for the postbox.
2. The postbox's wake and blank on an `edit` line (memory-bxxxxx).
3. Production: a `memory.edit` skill grant for an owner and a deploy, each a production change that waits for
   the operator's approval at the time. Nothing here runs there before it.

## 9. Review record and open questions

**memory-bxxxxx, plan review (production mail, 2026-09-30 00:45Z),** eight points, all taken in draft 1: (a) `from`
stays the original sender; (b) the exemption keys on the path, not the payload; (c) freeze everything but the
body; (d) refuse an edit of a redacted head; (e) the owner check reads version 1; (f) the same recipients plus reply
authors, a `mesh_edit` frame with no body, and WAKE the recipient; (g) state what a reply to an edited key means;
(h) delete the old version's derived rows in the same transaction.

**memory-bxxxxx, review of draft 1 (PR #29, 2026-09-30 00:53Z), CHANGES REQUESTED,** and where each is taken:

| # | Point | Taken |
|---|---|---|
| 1 | the author's own `memory.write` of a later version is an unguarded edit path: close it, after measuring | section 1 (M-run: 53 rows, none since 2026-09-12) and 2a |
| 2 | the database owner check binds the PEER to the owner, not the row's owner column | section 3, test 6 |
| 3 | a reader must be able to see that a body is an edit | section 2, test 17 |
| 4 | which version a reply answered: by commit order, not `created_at` | section 5, test 18 |
| 5 | the previous version is looked up as `version - 1`, not `is_head` | section 3, test 6b |
| 6 | the setting marks the path and does not authorize; the owner check and the frozen fields are the teeth | section 3 |
| 7 to 9 | cap of 20 versions per key; keep `role` in the frame; refuse an unversioned legacy head | checks 3 and 6, section 4, test 11 |
| 10 | the postbox blanks the preview on an `edit` line as well as waking | section 4, test 16 |

**Open, for the build:** a data import or restore that inserts historical versions (section 2a). Everything else
draft 1 left open is decided above.

## 10. Build state, 2026-09-30 04:06Z

Built on `fin/edit`, stacked on `fin/redaction`: `apps/memoryskills/edit.py`, `sql_meshychat/029`, the event through the addressed wait, the
push, the wake and the monitor's line, the reads, the client's `edit` verb and its signature pairing, and the tests of section 7 (see divergence
059's Proof). What the build found and changed in this design:

* **The record holds no request bytes** (section 4, corrected there).
* **A version can carry both records.** A version edited and later redacted has an edit record and a redaction record, so the wait's event columns
  are chosen by the POSITION's kind, never by which join answered: the edit of a version redacted since is still an edit event with its own time
  and seats (`tests/test_edit_delivery.py`).
* **`from` stays, and so a client had to change.** The client's author-signature check insisted the envelope be a `memory.write` call, so every
  edited row would have read `signature_verified: false`. It now requires `memory.edit` for a row that carries `edited_at` and `memory.write`
  for any other row, both ways (`tests/test_meshy_node.py`, and the end-to-end monitor test reads an edited row back over the real envelope).
* **Replies (section 5)** are proved by `ANSWERED_VERSION_SQL` and `tests/test_edit.py::test_18...`: commit order gives the answer where `created_at`
  gives another, for a reply whose transaction overlapped an edit.
* **Owed:** the postbox's wake on `edit` (memory-bxxxxx, #33; the line shape is fixed in section 4), and production (a deploy, then a `memory.edit`
  skill grant: each waits for the operator's approval at the time).
