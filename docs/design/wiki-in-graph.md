# The wiki in the graph: embeddings, lexical terms, related edges and search beyond mail (OI-57)

> RECORD, 2026-11-15. This is draft 2 of 2026-09-30 (base 34c93bb). Its slices 1 and 2 have since been built, both recorded as divergence 060 ("namespaces other than the mail are embedded in chunks and lexed"); slice 3, related edges within a namespace, is not built (060 says so). Read 060 for what the code does today. The status line below is as of 2026-09-30.

Status: DRAFT 2, reviewed by kxxx (non-author) with production measurements folded in (memory-bxxxxx,
2026-09-30). Nothing here is built. Written against
main 34c93bb. Joint with kxxx-cc; every production use needs the operator's approval at the time
(D-2026-09-29-U9).

## 1. What was decided, and what this settles

- **D-2026-09-29-U13** (operator): redaction's scope is the WHOLE graph, "a fully capable graph
  not just a wiki, embeddings re-ranking the work".
- **U16** withdrew U15 (rewriting the 998 wiki heads to `embed_policy='allowed'`). `forbidden` is
  the column default every writer hard-codes, mail included (sql/028:35-37 and
  106-108, cited in apps/meshsem/store.py:52-62). Mail embeds because production sets
  `MESHY_EMBED_FORBIDDEN_ROWS`, and that gate applies to the wiki unchanged. No data rewrite
  is needed.
- kxxx's order (2026-09-30 00:44Z): design this first, and put wiki redaction inside it rather
  than build a redaction of derived rows that do not exist yet.

This document settles how rows outside `shared/mesh-chat` get vectors, lexical terms, related
edges and search. It does not make the wiki mail: threads, summaries, the edge projection and
the addressed wait stay mail-only (section 6).

## 2. The corpus, as measured (kxxx, production)

Read-only sessions (`default_transaction_read_only` ON and read back first), SELECTs only, counts
and lengths only. 2026-09-29 06:50Z for the first rows, 2026-09-30 00:55Z for the rest.

| quantity | value |
|---|---|
| wiki head rows (09-30) | 1,000 |
| wiki rows, all versions (09-30) | 4,255 |
| heads with text in the content class (4.2) | 979 |
| characters in those heads | 4,153,382 |
| head length, mean / median / p90 / max (chars) | 4,242 / 3,322 / 8,299 / 57,030 |
| heads over MAX_CHARS (1,500) | 805 (82 percent) |
| keys not `wiki/`-prefixed (09-30) | 61: 49 bare slugs (no slash), 12 another prefix |
| wiki writes since the cutover (09-29) | 2 |

Heads with a non-empty string in each content-class field: `body_md` 978, `body` 18, `summary` 4,
`lesson` 3, `text` 0, `content` 0. Exactly one of the six: 955. Two: 24, in three pairs only:
`body`+`body_md` 18 (the two strings EQUAL in all 18), `body_md`+`lesson` 3, `body_md`+`summary` 3
(`body_md` the longer in all 6). None of the six: 21.

Two consequences drive the design. 82 percent of heads are longer than what the embed job reads
today, so one vector per row would embed the first 1,500 characters of most entries and miss the
rest: chunking is required. Writes are rare, so the walk can be simple.

## 3. The mail-only pins at 34c93bb

Each is a place where the mesh-chat namespace or the `msg/` prefix is written into a query, a
constraint or a trigger. Re-measured on 34c93bb, not copied from the 09-29 census (two of its
line numbers had moved).

| where | what it pins | change |
|---|---|---|
| apps/meshsem/embedjob.py:85-91 | reads `value->'body'` only, `namespace = %(ns)s AND key LIKE 'msg/%%' AND is_head` | a second walker for graph namespaces (4.3); the mail walker is unchanged |
| apps/meshsem/embedjob.py:81, :103-107 | `MAX_CHARS = 1500`, one text per row | chunks (4.2) |
| sql_meshychat/006 `mesh_embeddings` | `PRIMARY KEY (src_id, space)`: one vector per row | add `chunk_no` (4.2) |
| sql_meshychat/010 `mesh_lexical` | `PRIMARY KEY (src_id, space)` | terms stay per row, from the whole text; gains a `namespace` column (section 8: CORRECTED in the build, it was listed here as unchanged) |
| sql_meshychat/013 `mesh_related_edges` | `CHECK (src_key LIKE 'msg/%')`, same for `dst_key` | relax to a namespace column (4.5) |
| sql_meshychat/023 commit order | trigger `WHEN (NEW.namespace = 'shared/mesh-chat')` | NOT widened (4.3) |
| sql_meshychat/011 single writer | `WHEN namespace = 'shared/mesh-chat' AND key LIKE 'msg/%' AND version > 1` | unchanged: wiki rows are versioned by design |
| apps/meshsem/search.py:356-365 `_CANDIDATES` | reads `body`, `namespace = %(ns)s AND key LIKE 'msg/%%'` | namespace list from the caller's readable set (4.4) |
| apps/meshsem/views.py:77 | `check_permission(MESH_CHAT_NAMESPACE, ..., "read")` | per namespace (4.4) |
| apps/meshsem/related.py:247, related_reads.py:257, store.py:298 | `key LIKE 'msg/%%'`, mail namespace | per namespace (4.5, coverage) |

Rows are chosen by NAMESPACE, never by key prefix: 49 wiki heads have no `wiki/` prefix and a
prefix filter would silently drop them.

## 4. Design

### 4.1 Which namespaces

A setting, not a constant: `MESHY_GRAPH_NAMESPACES`, a list, default `["shared/mesh-chat"]`.
With the default nothing changes anywhere, which is the rollback. Production adds `shared/wiki`
when the operator says so. The mail namespace keeps its own walker and its own rules; every other
listed namespace goes through the graph walker below. A listed namespace that is unclaimed or empty derives
nothing, and coverage reports it by name rather than as zero rows.

### 4.2 The text of a row, and chunks

**Which field.** Mail has one text field, `body`. Wiki rows do not (section 2). The rule: take the LONGEST string among the content class
`body_md, body, text, content, lesson, summary`, with the names only as a tiebreak in that order.
Longest over ALL fields is wrong: an appended review field can outgrow the entry and be embedded
as its body (measured on the AgentMeshy graph, where a 928-byte review beat an 819-byte `body_md`).
On production this picks `body_md` for 978 of the 979 heads with text and `summary` for the one
that has only that; the review-field anomaly does not occur inside the class there, and the rule
guards against it for future writers. A row with no content-class string is recorded as
`skipped_no_content` and counted by coverage, never embedded as an empty string. There are 21 on
production: 13 hold no string of 100 characters (data), and 8 carry real text in ad-hoc field
names that differ head by head. v1 leaves those 8 out rather than fall back to "longest string
outside a metadata list", which is the longest-over-all-fields trap again. Coverage prints the
count so the gap is visible; renaming their field is a content fix for the authors, not a rule. `title`, when it is a
string, is prefixed to chunk 0 only.

**Chunks.** Split the text into windows of at most `MAX_CHARS` (1,500), breaking at the last
paragraph break, then the last sentence end, then the last space inside the window, with 200
characters of overlap. Expected count at the measured distribution: about 3,543 chunks over the
979 heads (3,260 without overlap), 3 to 4 for a typical entry and 44 for the longest
(57,030 / 1,300). Mail
rows are always one chunk, `chunk_no = 0`, and their text derivation does not change, so no mail
vector is re-embedded.

**Schema.** `ALTER TABLE mesh_embeddings ADD COLUMN chunk_no SMALLINT NOT NULL DEFAULT 0`, the
primary key becomes `(src_id, space, chunk_no)`, and `text_sha256` stays per chunk. Additive: every
existing row reads `chunk_no = 0`. Its down script deletes every row with `chunk_no > 0` (only graph rows
have them) and restores the old key, so the rollback gate has a real down script to run.

### 4.3 The walk: reconcile by set, not by watermark

The mail walk is a keyset walk in commit order (sql/023) because mail is high-volume and
append-only. The wiki is neither: 2 writes since the cutover, and a new version of an entry is a
new row that makes the old one stop being head. So the graph walker does not use a watermark and
023's trigger is NOT widened (widening it would put wiki rows into the sequence the addressed wait
and the edge projection walk, and both assume mail).

Each tick, per listed namespace and space:
1. Read the ids of the head rows (about 1,000 UUIDs for the wiki) and the set of `src_id`s holding
   vectors or lexical rows in that namespace.
2. Heads with no vector, or whose derived text digest changed: embed (chunks), lex, write. A head
   that now resolves to `skipped_no_content` (a tombstone, or a head that lost its content) and
   still holds derived rows has them deleted, so the walker converges by itself whatever path
   blanked the row (kxxx's review of draft 2; redaction's handler already deletes them in its own
   transaction, this covers any other path).
3. Rows that hold vectors but are no longer head (superseded) or no longer exist: delete their
   vectors, lexical rows and related edges in ONE transaction.
4. Model calls run outside any transaction, as in the mail job.

There is no late-commit hole to reason about, because nothing is skipped past: every tick compares
the complete head set with the complete derived set. The cost is two id scans of about a thousand
rows. If a graph namespace ever grows to mail volume, it moves to a commit-order walk then.

### 4.4 Search and its authorization

The authorization stays the namespace's (apps/meshsem/views.py:25-27): a peer that cannot
`memory.read` a namespace cannot read it through a vector either. The check moves from one
`check_permission(MESH_CHAT_NAMESPACE, ...)` to one per requested namespace. Results only come from
namespaces the caller can read. A namespace the caller cannot read is refused with a typed 403 when
it asks for one by name.

Compatibility: the existing search routes keep answering mail only unless the request names
namespaces (`ns=shared/wiki`, repeatable). A client written before this change sees no difference.
A wiki hit is one entry: the best chunk decides its rank (max over chunks), the reranker scores
that chunk's text, and the hit reports `chunk_no` and the chunk's character span so a caller can
open the entry at the right place. Hits carry `namespace`, which mail hits gain as well.

### 4.5 Related edges

Relax 013's `LIKE 'msg/%'` checks to a `namespace` column on `mesh_related_edges` (the key pair and
space as before). v1 draws related edges WITHIN a namespace only. A cross-namespace edge between
mail and wiki would reveal the existence and topic of a row to a reader of the other namespace who
cannot read it. That is a visibility rule, so it is left to the operator (section 7).

### 4.6 Redaction, inside this design

- Derived rows are deleted by `src_id` (kxxx's handler is already namespace-generic), and that
  covers every chunk of the row, since the chunks share it.
- Tombstone: a redacted row keeps only `redact.ROUTING_FIELDS` (schema_version, from, to, cc,
  type, thread_id, in_reply_to, ts, author_family), in routing shape. A wiki head's tombstone is
  therefore `{redacted, reason_code, redacted_at}` plus `author_family` (988 heads carry it) and
  `from` (41): labels, no content. It has no content-class field, so the walker records it as
  `skipped_no_content` and derives nothing; the same code path cannot re-embed it.
- The redaction trigger's shape check names the four mail text fields today. It becomes an
  ALLOWLIST (kxxx, 2026-09-30): the tombstone's keys must be a subset of
  `{redacted, reason_code, redacted_at}` plus `ROUTING_FIELDS`. That covers every namespace and
  any content field a future writer invents, with no per-namespace list; a test compares the
  trigger's list with `redact.ROUTING_FIELDS` so the two cannot drift. It is a new migration that
  replaces the function, built with this design's implementation (which is what turns wiki
  redaction on in practice), so it does not disturb redaction's 027 in review.
- Wiki redaction sends no event in v1: 023's commit order stays mail-only (4.3). A seat's cached
  copy of the graph refreshes on its own schedule. Stated in redaction's limits as well.
- Redacting an older version of an entry: its vectors are already gone (4.3 step 3 removed them
  when it stopped being head), so only the row itself is blanked.

## 5. Tests, each red-proved against the real mutant

- a mail row's text, chunk count (1) and digest are byte-identical before and after; no mail vector
  is re-embedded on first run (sql: count of rows with `embedded_at` after the change).
- a namespace not in the setting gets nothing; a listed but unclaimed or empty one derives nothing
  and coverage names it.
- field choice: longest content-class string wins over a longer non-content field; a row with none
  is `skipped_no_content`; a bare-slug key is embedded.
- chunking: windows never exceed MAX_CHARS, cover the whole text, break at paragraphs first, and a
  57,030-char entry yields the expected count.
- a new version replaces the old version's vectors, lexical rows and related edges in one
  transaction; the old ones are gone.
- search: a caller without read on `shared/wiki` gets a typed 403 when naming it and no wiki hits by
  default; one with read gets the best chunk with its span; a request without `ns` is answered
  exactly as before.
- redaction of a wiki head deletes every chunk; the tombstone is never re-embedded; the trigger
  refuses a tombstone keeping any key outside the allowlist (a `body_md`, an invented field), and
  its list equals `redact.ROUTING_FIELDS`.
- a head blanked by a path other than redaction (a raw tombstone written in a test) loses its
  vectors and lexical rows on the next tick; a mutant that skips the step-2 delete goes red.
- the down migration deletes chunk rows and restores the key; the rollback gate admits it.

Mutations: drop the namespace filter, pick longest over all fields, skip the superseded delete,
check permission once instead of per namespace, remove the overlap, and embed the tombstone.

## 6. Out of scope for v1

Thread windows and summaries (apps/meshsum), the mesh edge projection and graph routes
(apps/mesh), the addressed and plain waits: all are about mail threads and senders, and the wiki
has neither. The pins listed in section 3 that are NOT in the change column stay as they are.

## 7. Decided in review, and what is left

kxxx, non-author, on df554a4 (2026-09-30 00:56Z):

1. **Default search scope:** mail only unless `ns` is named. No caller changes behavior.
2. **Cross-namespace related edges:** not in v1.
3. **Backfill timing:** measure one tick first, under the mail job's per-tick bound; the run uses
   HQB's embedder only (nothing on HQD, standing rule). memory-bxxxxx picks the hour from that
   measurement.
4. **The 21 heads without a content-class field:** `skipped_no_content` in v1, counted (4.2).
5. **The redaction shape check:** an allowlist, built with this implementation (4.6).

Every production step (the setting, the migration, the backfill) waits for the operator's approval
at the time (U9).

## 8. Build state

**Slice 1, built** (branch `bxxxxx/oi57-graph-walker`): `MESHY_GRAPH_NAMESPACES`, the text rule and
the chunks (`apps/meshsem/graphtext.py`), the reconcile-by-set walker and its job `mesh_graph_embed`
(`apps/meshsem/graphwalk.py`), `python -m apps.meshsem graph-catch-up`, migration
`sql_meshychat/030` with its down script, divergence 060.

**One correction to section 3, found while building.** It listed `mesh_lexical` as unchanged. The
mail search keeps two in-process indexes (`search.VectorIndex`, `lexical.LexicalIndex`) that load a
space's rows newest first up to a ceiling, with no namespace in the query. Once the wiki is derived
into the same space, both would load its rows: a mail query's top ranks would fill with rows the
candidate query then drops, and a backfill of about 3,500 chunks could push older mail vectors out
of the ceiling. So 030 adds `namespace` to BOTH tables, and both indexes load one namespace, the
mail's unless named. The mail search answers exactly as before; the test that holds it loads the
indexes after a wiki walk and finds none of its rows (and a wiki-namespace index as the control).

**Slice 2, built** (branch `bxxxxx/oi57-graph-search`, stacked on slice 1): `GET /v1/mesh/search?ns=`,
`apps/meshsem/graphsearch.py`. One namespace per request (the two arms' rankings are fused within one
namespace); read is checked on the named namespace, so the route's frame now takes the namespace from
the request instead of fixing the mail; a namespace the walker does not derive is a 422. The vector
arm ranks chunks (`ChunkIndex`, keyed `<src_id>:<chunk_no>`) and a row takes its best chunk; a row only
the lexical arm found is shown by the chunk sharing most query terms, so the reranker judges the
passage that matched. The pool, the arm reservations and the reranker are the mail's own
(`search._pool` takes the lookup as a parameter), and the mail's hits and envelope are unchanged.

**Slice 3, next:** related edges within a namespace.
