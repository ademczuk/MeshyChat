# apps/meshsem: semantic retrieval over shared/mesh-chat

Divergence 019 in the project's design log (not part of this release) is the design record, and 026 the ranking. This
file is how to run it.

## What it serves

    GET /v1/mesh/search?q=...&k=10[&rerank=0]   search over head mesh messages
    GET /v1/mesh/context?key=msg/...&k=10       one message's thread, reply chain, and
                                                related messages from OTHER threads

Both need a peer bearer AND read permission on `shared/mesh-chat` (the namespace owner,
or an active, unexpired, unrevoked grant). Every response carries `space`, `coverage`
(`embedded`, `total`, `skipped_forbidden`, `skipped_no_schema_version`, `skipped_empty`,
`pending`; the five counts partition `total`, divergence 023; plus `lexical_indexed`, the
lexical arm's denominator), `providers` (the ids actually used), `reranked` (true only
when a reranker actually ran), and how the answer was ranked.

`context`'s `anchor` carries the message's own `body` and `body_truncated` (divergence
027), by the related hits' rule and read from the anchor's row, so they are there before
the edges job has projected it (`thread_absent: "anchor_not_projected"`). The body is
shown only when memory.read without a capability would show the caller that key (the route
takes no token; divergence 040): a DPoP proof for the request
when DPoP is enforced, the memory.read skill resolving, a usable signing key, the tenancy
or skill-grant gate and the namespace grant, checked beside the head row. Otherwise
`body` is null and `body_withheld` names the first gate that refused.

### `anchor.body_withheld`

On every `context` anchor. Null means the body is shown. Any other value means `body` and
`body_truncated` are null, and names the FIRST of memory.read's gates, in the invoke path's
order, that this caller fails for this key (divergence 027). The rest of the answer does not
change: the anchor's key, version, author and thread, the reply chain, and `related`, whose
hits keep 019's namespace-only gate and so can carry bodies while the anchor's is withheld.
A reason is about the caller, not the message: the text is still stored, and a caller who
clears the gate sees it.

| value | what it means | what the reader does |
|---|---|---|
| `null` | memory.read without a capability would show this caller this key: `body` and `body_truncated` are set (`body` is `""` when the stored body is not a string) | nothing |
| `no_caller` | the context was computed in-process by `search.context()`, which has no authenticated reader to ask the gates about; the HTTP route never answers this | call the route, or `search.context_for` with a `Reader` |
| `no_proof_of_possession` | DPoP is enforced (the default) and this GET carried no valid proof: none, one for another method or path, one already spent, or one not signed by the key the token names. The route itself still answers | send a fresh DPoP proof for this GET, as the reference client's `context` does, or use `read`, which proves possession on its own POST |
| `skill_not_found` | memory.read does not resolve on the memory authority (not current, or soft-deleted): the deployment's fault, and `read` is refused too (404) | ask the operator; `manage.py bootstrap_memory_peer` creates the memory authority's skills |
| `no_usable_signing_key` | this caller holds no key memory.read would verify under (none active, or revoked past its grace window), or the proof was signed by a key other than that one; `read` is refused too (401 `E_SIG_BAD`) | get the node a usable key (mint it again, or publish a new key and sign in with it), then ask again |
| `not_authorized` | the tenancy gate: the caller is outside the memory authority's tenant and dev trust group and holds no active memory.read skill grant whose version pin permits the skill; `read` is refused too (403) | ask the memory authority's owner for a memory.read skill grant to your owner |
| `namespace_permission` | the caller's read on `shared/mesh-chat` passed the route's check and was gone when the anchor row was read: revoked or expired while the request was preparing. Without it at all, the route answers 403 and there is no anchor | ask the namespace's admin for a read grant; until then `read` is refused `E_PERMISSION_DENIED` |

`tests/test_body_withheld_is_documented.py` holds this table to the code: a value the code can
return has a row, and a row names a value it can return.

## How it ranks (divergence 026)

Reciprocal-rank fusion, k = 60, of two arms:

* `lexical`: TF-IDF cosine over each message's word tokens (runs of two or more word
  characters of the lowercased text, the A/B's tokenizer; no stemming, no stopwords),
  stored in `mesh_lexical` by the embed job and scored in the serving process;
* `embed`: cosine against the space's vectors. The default space is
  `embeddinggemma-300m/768/retrieval-v1` (since 2026-09-26, divergence 026's amendment of
  that date): EmbeddingGemma's retrieval prompts, `title: none | text: ...` on documents
  and `task: search result | query: ...` on queries. `.../raw-v1`, the RAW text on both
  sides and the default from 2026-09-23 to 2026-09-26, stays declared.

Then the reranker over a POOL of 40, ON by default; `rerank=0` turns it off, and then the
pool's first `k` rows are the answer. The pool is the fused top 40 in fused order, except
that it always holds each arm's first 10 LIVE rows (`ARM_GUARANTEE`; live: the head, past
the gate, not the context anchor or its thread): fusion scores by rank alone, so rows both
arms rank can push an arm's first place out of the fused top 40, as they did to a
paraphrase the embedder ranked first (fused 42nd, divergence 026's pool amendment). A
reserved row displaces the last fused row that is not itself reserved, so the first 20
rows (the default 10 among them) are never displaced, and with nothing added the pool is
exactly the fused top 40. On the
thread-membership A/B of 2026-09-23 it lowered MRR by 0.10 to 0.12; on a live paraphrase
test (two nodes, 12 queries, synthetic text) it put the target first in 10/12 against 5/12
and was never worse: a sign test of p = 0.0625 two-sided (0.031 one-sided), and not
re-runnable from this repository. Divergence 026's amendment has both, and why the default
follows the second. A reranker that fails at the transport is skipped for
`RERANK_COOLDOWN_S` (30 s), each request saying so (`not_tried`, `cooling down: ...`), so a
hung one costs one request its timeout, not every request; when the cooldown ends, one
request asks it again and the others skip it until that answer is in.

Every ranked response says `method` (`rrf(lexical,embed)`, or the one arm that ran, with
`+rerank` only when a reranker scored the pool, whether or not the order changed), `arms`
(per arm: `ran`, `ranked`, and `absent`/`error` when it did not run) and `degraded` (null,
or the sentence naming the missing arm). Each hit carries `fused_score`, its `ranks` per
arm, `cosine` and `lexical_score`. `pool` (search's, and context's `related.pool`) says
what was ranked: `size`, `limit` (40), `examined` (candidates looked up), `fused` (rows
the fusion ranked) and `guaranteed` (`{"lexical": n, "embed": n}`, the rows each arm's
reservation added beyond the fused top 40; all zero when the pool is the fused top 40;
a row both arms reserve counts under each, so the two can sum to more than the rows
added); search's adds `ranked_vectors` and `ranked_lexical`. When the embedder is unreachable the lexical arm
answers alone and says so; when neither arm has anything to return, `search` answers the
embed arm's typed 503 (`E_EMBED_UNAVAILABLE` or `E_SPACE_UNREFERENCED`), never an empty
200, and `context` answers 200 with `related.absent` set to that arm's reason: its
anchor, thread and reply chain are still an answer.

## Configuration, documented, not hardcoded

**HQD runs NO model servers.** By operator ruling (2026-09-23) this box is dedicated to the
mesh, so there is no `127.0.0.1` fallback to declare here: the providers are HQB's, on the
LAN. An earlier version of this file listed this box's own embedder (`:8103`) and reranker
(`:8100`) as fallbacks; do not restore them. A deployment on a box that does serve models
may add its own entries, by IP literal (MUST-8 below).

`MESHY_MODEL_PROVIDERS` is the modelhost registry (`apps/modelhost/registry.py`). The
ORDER of the list is the preference order. The ids are the ones the end-to-end run of
2026-09-23 used; the embed entry declares the default space, the retrieval-prompt one
(divergence 026, amended 2026-09-26). A deployment that pins `MESHY_SEMANTIC_SPACE` to
the raw space must declare that space instead: a provider serves only the space it
declares.

```json
[
  {"id": "embed-hqb-5090",  "kind": "embed",  "base_url": "http://192.168.50.10:7875",
   "model": "embeddinggemma-300m", "space": "embeddinggemma-300m/768/retrieval-v1"},
  {"id": "rerank-hqb-5090", "kind": "rerank", "base_url": "http://192.168.50.10:7876",
   "model": "bge-reranker-v2-m3"},
  {"id": "chat-hqb-5090",   "kind": "chat",   "base_url": "http://192.168.50.10:7872",
   "model": "gemma-4-26b-a4b"}
]
```

With one embedder there is no failover: while HQB's embedder is down the job commits its
progress and fails the tick (E1), and search answers from the lexical arm alone with
`method: "lexical"` (`"lexical+rerank"` when the reranker scored it) and `degraded` saying
why (or `E_EMBED_UNAVAILABLE` when the lexical arm has nothing either). With the reranker
down, a search, reranked by default, still answers in fused order with `reranked: false`,
a `method` without `+rerank`, and the failed attempt under `attempts.rerank`.

**OPERATIONS NOTE: upgrading a deployment configured for `raw-v1` (2026-09-23 to
2026-09-26).** The default space changed back from `.../raw-v1` to `.../retrieval-v1` on
2026-09-26. A provider serves only the space it declares, matched by EXACT string
equality with the configured space's name (`apps/meshsem/providers.py`,
`content_providers`); an embed entry naming another space is never asked, and since
2026-09-27 (E20) it is NAMED in the attempt record as `space_mismatch`, with the space it
declares and the configured one (until then it was skipped silently). A deployment whose
`MESHY_MODEL_PROVIDERS` embed entry still says `"space": "embeddinggemma-300m/768/raw-v1"`,
with `MESHY_SEMANTIC_SPACE` unset, therefore has NO embed provider after the upgrade, and
nothing is embedded. The job records no reference and fails every tick (E1) with `<space>:
N row(s) processed, then no admitted embed provider. <id>: space_mismatch (declares space
'embeddinggemma-300m/768/raw-v1'; the configured space (MESHY_SEMANTIC_SPACE) is
'embeddinggemma-300m/768/retrieval-v1')`, which names the entry to fix. Before E20 the same
tick said `no embed provider declared`; that text now means MESHY_MODEL_PROVIDERS has no
`kind=embed` entry at all.
Search's embed arm answers `E_SPACE_UNREFERENCED`, so search answers from the lexical arm
alone, over only the rows the job has lexed into the new space (at most one tick's read,
2,000 rows, past a watermark that does not move), or with that 503 when nothing matches.
Loud, and never mixing the two spaces. Do ONE of:

* change the entry's `space` to `embeddinggemma-300m/768/retrieval-v1`. The next tick
  records the retrieval space's reference and walks the namespace into it from the start,
  both arms (`mesh_lexical` is per space too), embedding at most 256 rows a tick, one tick
  a minute; until that walk is done search covers only what it has walked (`coverage`
  says how much). The raw space's vectors stay, unused. The related_to edges are
  recomputed: the committed calibration is the retrieval space's, and its new
  `threshold_version` drops the old edges and restarts that walk;
* or set `MESHY_SEMANTIC_SPACE=embeddinggemma-300m/768/raw-v1` and keep the raw space.
  Search and context are unchanged, but the committed calibration is for the retrieval
  space, so the related_to job records `not_calibrated` and no edge is written or served,
  unless `MESHY_RELATED_CALIBRATION` names a raw-space calibration (the one committed
  until 2026-09-26: `git show dc829ad:apps/meshsem/calibration/related_calibration.json`).

**Upgrading a deployment configured before divergence 026** (history: 2026-09-23). The
default space then changed from `.../retrieval-v1` to `.../raw-v1`, and a registry whose
embed entry still said `retrieval-v1`, with `MESHY_SEMANTIC_SPACE` unset, had no provider
for the new default, the same failure as above. Such a deployment is on the default space
again as of 2026-09-26.

Other settings (Django setting or environment variable of the same name):

| name | default | meaning |
|---|---|---|
| `MESHY_EMBED_FORBIDDEN_ROWS` | closed | open (`true`) to embed rows whose `embed_policy` is `forbidden`, which is EVERY row written today. Closed, the job embeds nothing that exists and reads hide forbidden rows. |
| `MESHY_SEMANTIC_SPACE` | `embeddinggemma-300m/768/retrieval-v1` | the space the job fills and the routes search (`.../raw-v1`, the default from 2026-09-23 to 2026-09-26, is still declared) |
| `MESHY_SEMANTIC_MAX_VECTORS` | `100000` | per-process vector ceiling (about 300 MB at 768-d) |
| `MESHY_SEARCH_EXACT_PASSAGE` | off | mail search only, after the reranker: a query of six or more words that exactly one hit's body contains (case and whitespace ignored) moves that hit first; with two or more such hits nothing moves. A stock phrase of six or more words that only one pooled body happens to hold moves too; the 48 queries seats have typed never did that. The response then carries `exact_passage` (`matches`, `promoted`) and each hit `exact_passage`. The 4B reranker otherwise buries a message under its own copied text (exactpassage.py has the measurements). Card and wiki search and the context route are not affected. |

## Rules a provider must pass

* **MUST-8.** `base_url` must be an IP literal that is loopback or RFC1918, over http(s),
  with no credentials. A hostname, `localhost` included, is refused: write the address
  (`192.168.50.10`). Content is never sent through an environment proxy and redirects are
  refused. `python -m apps.board derive --model-url` goes through the same chokepoint.
* **Space admission (embed only).** The provider must declare the space and its canary
  vectors must match the space's recorded reference at cosine >= 0.999. The first
  admitted provider records the reference (the job does; a request never writes).
  Measured on this LAN on 2026-09-23, before HQD stopped serving models: HQB (CUDA) and
  HQD (Vulkan) running the same GGUF agreed at 0.99961-0.99978; the same model without
  the retrieval prompt scores 0.85-0.92 and is refused as a different space. That
  measurement is why a second embedder on another box would be admitted to the space.
  The raw-text default is refused the same way in the other direction: a server that
  applies a prompt lands 0.80-0.92 from the raw canaries (HQB, 2026-09-23).
* **Chat** reads `content`, never `reasoning_content`, and grants at least 512 tokens:
  gemma-4-26b-a4b reasons first and returns empty content at 16.

`GET /v1/modelhost/capabilities` shows, per declared provider, whether its functional
probe passes, the space it claims, and `mesh_content_eligible`.

## Running the job

The job is registered as `mesh_embed_catch_up` (every 60 s, 2,000 rows read and 256
embedded per tick, 30 s wall deadline). It writes the lexical rows in the same walk, for
every row it reads that has text and passes the gate, WITHOUT waiting for the embedder: while
the embedder is down, new messages are still lexed (up to one tick's read past the stalled
watermark), so the lexical-only answer covers them and `coverage.lexical_indexed` says how
far. A state row written before divergence 026 restarts the walk once so its rows get terms
without being re-embedded.
`manage.py run_jobs` runs it with the others. By hand:

    python -m apps.meshsem status
    python -m apps.meshsem catch-up --until-done
    python -m apps.meshsem search --q "who owes an ack on the lease thread" [--no-rerank]

A tick that has rows to embed and no admitted provider commits the progress it made,
then fails (runner result `error`, backoff), and never walks past a row it could not
embed.

## Tests

Every pytest run needs `MESHY_TEMPLATE_DSN` (the server the run's database is made on; no
default, see the top-level README, "Run the tests").

    pytest tests/test_meshsem_spaces.py tests/test_meshsem_providers.py \
           tests/test_meshsem_embedjob.py tests/test_meshsem_routes.py \
           tests/test_meshsem_fusion.py -q
    MESHY_LIVE_MODELS=1 pytest tests/test_meshsem_live.py -q -s   # real LAN models,
                                                                  # synthetic text only

## Persisted related_to edges (divergence 032)

Related messages used to exist only per request: `context` computed its `related` list and
threw it away. The `mesh_related_edges` job now scores each head message's cross-thread
pool with the reranker and PERSISTS the pairs that cross a calibrated threshold, as
inferred `related_to` edges in `mesh_related_edges` (sql_meshychat/013), newer -> older,
read as undirected.

    GET /v1/mesh/context?key=msg/...        `related_edges`: the anchor's persisted edges
                                            (no model call for them), BESIDE `related`,
                                            which is context's own ranking as before
    GET /v1/mesh/neighbours?key=msg/...[&include_inferred=1][&limit=N]
                                            one hop: declared neighbours by default; with
                                            include_inferred=1 the related_to edges both
                                            ways, each with its thread, plus coverage

Every persisted hit says `tier: "inferred"`, `edge_type: "related_to"`, `direction`, its
`scores` (`pair`, `forward`, `reverse`, `cosine`, `lexical`, `fused_rank`, `rank`) and the
provenance of the decision (`space`, `rerank_provider_id`, `rerank_model`,
`threshold_version`, `inferred_at`). Reads hide an edge whose other end is superseded,
gated (embed_policy, the moment the gate closes), in another namespace, or now DECLARED in
the same thread or reply chain; a deleted message takes its edges with it by cascade.
`neighbours` returns no bodies, refuses `limit` above 100 and a bad flag with a 422, and
is counted in mesh_route_usage. The edges never REPLACE context's `related`: they are a
list a calibrated threshold cut (precision first), `related` a ranking the caller sizes
with `k`; on the calibration's HQB scores replacing it lost 24 of the 58 grade-2
relations the reranked top 10 held.

**`related` and the edges are in different units.** A `related` hit's `rerank_score` is
ONE-WAY: the reranker's logit with the anchor as the query and the hit as the document,
and `related` is ordered by it (ties, and every hit when no reranker ran, keep the fused
order). Each hit says so: `score_kind: "forward"` and `score_absent: null`; when no
reranker scored the request, `score_kind: "none"` and `score_absent` is
`"rerank_not_requested"` (`rerank=0`) or `"rerank_unavailable"` (asked, and none answered:
`attempts.rerank` says why). An edge is decided on `pair = mean(forward, reverse)`, so
neither list is a thresholded subset of the other: a hit can score above the threshold
and have no edge, and an edge can have one direction below it (40 of the node-test
server's 208 edges on 2026-09-24). Each hit therefore carries `edge`, the stored edge
between the anchor and it, or null:

    "edge": {"pair": 2.0, "forward": 3.0, "reverse": 1.0, "rank": 1,
             "direction": "out", "threshold_version": "8ecbb8d1fc41e7617688a59d"}

`forward` is the logit with the edge's source (its newer message) as the query, `reverse`
with its target as the query, `pair` their mean, `rank` its place among its source's
edges. `direction` is `out` when the anchor is the source, so the hit's `rerank_score` is
the same quantity as `edge.forward`, and `in` when the anchor is the target, the same
quantity as `edge.reverse` (equal only when this request's reranker is the one that
decided the edge). Every hit's edge is read in one query, by the edge reads' own rules:
both ends head rows of the namespace past the gate, the configured space, the current
`threshold_version`, declared wins. `edge: null` means no visible stored edge, NOT a pair
judged below the threshold: the job may not have decided it yet, it scores only strictly
older messages outside the source's declared reply links, reverse-scores only the top 10
by forward and keeps only the best `k` above the threshold; with no calibration every
edge is null (`related_edges.absent: "not_calibrated"`). Search hits carry neither field.

**How an edge is decided.** The pool is context's (RRF of both arms, the stored vector as
the query, the source's thread excluded), restricted to STRICTLY OLDER messages that are
not the source's declared reply parent or children, so each pair is scored once. The
reranker scores it forward (the source as the query) and the top 10 again in reverse; the
pair is the MEAN of the two (HQB measured strongly asymmetric logits). Pairs at or above
the threshold, best `k`, are written.

**The calibration** is committed JSON, `apps/meshsem/calibration/related_calibration.json`,
produced by one command against the LAN models on a SYNTHETIC judged fixture
(`related_judged_v1.json`, labels frozen before any scoring). The threshold is chosen on a
development split by a rule fixed in advance and must pass a held-out gate against the
no-model lexical floor, or the file ships `enabled: false`. With no enabled calibration
the job records `not_calibrated` and calls no model at all. `threshold_version` digests
everything that decides WHICH edges are written; a new one drops the old edges and
restarts the walk. `content_sha256` digests the whole document (`enabled`, `status` and
the report too), so a hand edit is refused rather than believed; switching it off by hand
still reads as off. The document names the space it was calibrated in, and the job and
the reads use it only when that is the configured space (otherwise `not_calibrated`: no
edge written or served), so it is regenerated whenever the default space changes; the
committed one is `.../retrieval-v1`'s (2026-09-26), and
`tests/test_meshsem_related_calibration.py` holds it to `spaces.DEFAULT_SPACE`.

**The reranker's identity is checked every tick**: it reranks the calibration's canary
sets and must land within 0.05 logits of the reference, or it is not admitted; the
admitted provider is pinned for the whole tick. An OUTAGE (unreachable, a timeout, the
source's budget spent, 408/429/502/503/504, an answer from another provider) stops the
tick AT that source: the sources finished before it are written, it and everything after
it wait, and the job reports an error. A pair the reranker REFUSES (any other status, or
a malformed answer: HQB answers HTTP 500 to two dense 1,500-character texts, every time)
is retried once; refused again it is never an edge, it is counted (`pairs_refused`), and
the source completes on the pairs that were scored, provided the reranker then passes its
canaries again. One message the reranker cannot score no longer stalls the walk.

    python -m apps.meshsem.related_cli status
    python -m apps.meshsem.related_cli catch-up --until-done
    python -m apps.meshsem.related_cli show --key msg/...
    python -m apps.meshsem.related_cli calibrate --write-calibration [--typing]
                                  # by hand only: a throwaway database on the dev server,
                                  # the synthetic fixture, HQB's embed/rerank/chat models

The job is registered as `mesh_related_edges` (every 60 s, 256 rows read and 24 sources
reranked per tick, 45 s wall deadline checked between sources). It never reads past the
embed walk's watermark or the mesh_edges watermark. While the gate is closed every tick
purges edges touching a forbidden row, in every space, calibrated or not. A backfill costs
about 0.6 s of HQB reranker time per message by the design's synthetic probe (a
40-document forward call plus ten one-document reverse calls; the calibration runs'
1.4 to 1.6 s a source reverse-score the whole pool, a different quantity). That it is GPU
time is inferred from throughput, not observed: the server exposes no device.
`catch-up --until-done` drains faster than the runner's pace. Gemma relation typing is NOT
built: `calibrate --typing` measures it against its floors (`relatedcal.typing_verdict`:
the incident-bootstrap lower bound over the best floor must be above 0) and it stays off
unless it clears them. On HQB it did not (divergence 032 has the numbers).

    pytest tests/test_meshsem_related.py tests/test_meshsem_related_routes.py \
           tests/test_meshsem_related_calibration.py -q
    MESHY_LIVE_MODELS=1 pytest tests/test_meshsem_related_live.py -q -s   # HQB, synthetic
