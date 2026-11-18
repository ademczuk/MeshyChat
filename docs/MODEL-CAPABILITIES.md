# Node-local model capabilities: what is here, what works, and what is not built

**SUPERSEDED FOR HQD, 2026-09-23.** By operator ruling HQD is dedicated to the mesh and runs
NO model servers. Do not declare a `127.0.0.1` provider on HQD: the providers are HQB's, on
the LAN, `192.168.50.10:7875` (embed), `:7876` (rerank) and `:7872` (chat); the registry
example is in `apps/meshsem/README.md`. The table below is kept as the dated record of what
this node served on 2026-09-21, not as configuration.

Written for whoever picks up the inference work next. Everything in the first section was
MEASURED on this node on 2026-09-21, not read off a config file. Re-measure before relying
on it; the whole point of the section below is that configuration and reality disagree.

## What is actually resident

| thing | endpoint | status | how that was established |
|---|---|---|---|
| VRAM arbiter | `127.0.0.1:7878` | **up** | `GET /api/status` -> GPU 8192 MB total, 6900 free |
| reranker `bge-reranker-v2-m3-Q8_0` | `127.0.0.1:8100` | **up, genuinely works** | a known-relevant doc scored **2.59**, a known-irrelevant one **-11.01** |
| embedding models | none served | **on disk only** | `Qwen3-Embedding-0.6B-Q8_0.gguf`, `bge-m3-Q8_0.gguf` present in `vramMan/models/`, no listener |
| chat `Qwen2.5-3B-Instruct` | `127.0.0.1:8099` | down | connection refused |
| gemma (GemmaSwarm auditor's model) | `127.0.0.1:7872` | down | connection refused |
| GemmaSwarm auditor loop | - | not running here | no scheduled task, no process |

The GPU is a **GTX 1070, 8 GB, Pascal**. That is the constraint behind every other choice:
`vramMan/models.toml` records that the big models and the CUDA-wheel stacks were triaged
off this card, and that llama.cpp on Vulkan is the only path that runs.

## THE TRAP: health is not capability

The reranker answers `GET /health` with `{"status":"ok"}`. It also answers
`POST /v1/embeddings` with **HTTP 200 and a full-length vector of signed zeros containing
nulls**, because it was started `--reranking --pooling rank` and the model has no embedding
head. The server replies anyway.

A registry that advertised "this node can embed" on the strength of that 200 would have
every consumer indexing zero vectors. **Cosine similarity over zero vectors is not an error,
it is a number**, so nothing downstream would ever raise. The failure would surface months
later as "retrieval is bad".

This is why `apps/modelhost` advertises a capability only on evidence of FUNCTION:

- **rerank** - a known-relevant document must score strictly higher than a known-irrelevant
  one. Rejects a constant-scorer, which a response-schema check accepts.
- **embed** - right shape, non-degenerate, AND **discriminating**: a related sentence must
  be closer than an unrelated one. The shape check alone passes the all-zeros case; only
  the discrimination check fails it.
- **generate** - non-empty text. Deliberately the weakest of the three: a liveness probe
  cannot honestly judge generation quality, so it claims only that text came back.

Verified end to end against the live services: `rerank` available (405 ms), `embed` refused
("embedding contains nulls", 81 ms), `generate` unreachable - three different verdicts with
three different reasons, because "absent", "broken" and "unreachable" have different fixes.

## Rules that are not negotiable on this box

1. **Never start a model.** Swaps are arbitrated by the VRAM manager, which serialises on a
   shared Postgres lease (`vram_switch`) and returns **HTTP 409** rather than racing. On an
   8 GB card, a probe that loaded a model to see whether it could would evict whatever the
   operator was using. `apps/modelhost` records `arbiter_id` so a human knows what to ask
   the arbiter to start, and never acts on it.
2. **Cold load and served latency are different numbers.** Measured in the earlier
   capability-routed inference work: a cold swap took **21.4 s**, the served call after it
   took **5.0 s**. A single latency figure hides the one that matters, and a deadline that
   starts before the model is resident will time out a healthy call.
3. **Single-GPU fairness.** One card, so concurrency of 1-2 and no long call allowed to
   head-of-line block a queue behind a swap mutex.
4. **Gate on liveness, not on configuration.** A declared provider is not an available one.

## What is built, and what is honestly not

**Built** (`apps/modelhost`, `tests/test_modelhost.py`, 19 tests):
`GET /v1/modelhost/capabilities` - what THIS node can currently do, each provider carrying
the evidence for its verdict. No database handle, so it still answers when Postgres is down,
which is when an operator most wants to know what a node can still do - **verified, not
asserted**: `test_capabilities_answer_while_the_database_is_unreachable` points the app at a
dead DSN and requires a database-backed route to FAIL under the same settings, because
without that control the test passes just as happily against a healthy database. Unauthenticated and
read-only: a peer needs to know where work can go BEFORE it holds a grant for that work, so
putting discovery behind authentication would make it depend on the thing it enables. The
response deliberately omits `base_url` and never mentions the arbiter port.

**NOT built - this is the ADVERTISE half only.** A node can now say what it can do. Nothing
yet ASKS another node. To make "detected and used across the mesh" true, the missing pieces
are:

- **Discovery.** Peers already publish signed agent cards (`apps/peers`). Capabilities
  belong there, or beside them, so a peer learns them the same way it learns a peer's keys -
  and so the claim is signed rather than asserted by an open endpoint.
- **Freshness.** A capability is a claim about a moment. Advertised capabilities need a
  measured-at stamp and a TTL, or a consumer will route to a model that was evicted an hour
  ago. This is the `peers.status='active'` trap in a new costume: a field that is almost
  always true discriminates nothing.
- **Routing.** Choosing among peers that claim the same capability - free-local-first,
  health-gated, deadline-aware, swap-aware.
- **Authorization.** Using another node's GPU is a cross-owner call, so it runs through the
  existing signed-envelope + grant path rather than a new one. Grants must carry a non-NULL
  future `expires_at`; a perpetual grant to someone else's GPU is the defect class this
  project has already been bitten by.

## The reranker is a BATCH tool, not a prompt hook

Measured on this card: 40 documents at 1800 chars took **8.7 s**; at 8000 chars, **24 s**.
A labelled 8-query evaluation moved 3 queries up, 1 down, 4 unchanged - the best case lifted
the correct entry from lexical rank 6 to rank 1, which matters because rank 6 is outside the
prior-art gate's top-5 cutoff, so the gate could never have surfaced it. That is a real win
for an offline pass and far too slow for an interactive path.

`vramMan/models.toml` also records why a reranker was chosen over a local embedder in the
first place, and it is worth not relitigating by accident: the fleet's existing vector cache
holds 768-dimension vectors from a different model, **vectors from two different models are
not comparable**, so swapping the embedder is a fleet-wide re-embed. A reranker needs no
shared vector space and is purely additive to the lexical arm.

## The availability bug I shipped in the first version, and how it was bounded

`GET /v1/modelhost/capabilities` is unauthenticated by design, and the first version probed
every provider on every request with an 8 s per-call timeout. Measured against providers
pointed at a blackholed host:

| version | 3 blackholed providers |
|---|---|
| no bound | ~24,000 ms |
| deadline checked only BETWEEN providers | 16,066 ms |
| remaining budget passed INTO each probe | **10,010 ms** against a 10 s budget |

The middle row is the instructive one: the bound existed and read correctly, and it was
worth about a third of what it claimed, because two 8 s probes both started before the
deadline. A test asserting `elapsed < TIMEOUT_S + 2` is green for all three rows, so the
test now asserts against the budget itself.

A second bound caps the RATE: one sweep is cached for a few seconds and concurrent callers
queue on a single lock, so request volume cannot multiply GPU work on a one-card box. There
is deliberately **no cache-bypass parameter** - a bypass hands the hammer straight back.

Caching makes the answer a claim about the past, which is the thing this app exists to
refuse, so every response carries `measured_at` and `age_ms`. **A dated claim can be judged;
an undated one is what lets a consumer route to a model evicted an hour ago.** That is the
same freshness requirement listed above for cross-node discovery, applied here first.

## Files touched, so a concurrent edit does not merge into something nobody wrote

`apps/modelhost/*` (new), `tests/test_modelhost.py` (new), and ONE added `path(...)` line in
`meshychat/urls.py`. If you are editing `meshychat/urls.py` too, read the merged file end to
end rather than the diff - two clean additions to one function have already produced a
result neither author wrote once in this repo.

## MEASURED: can a Qwen model be used as a reranker here? No, not with a standard GGUF

Asked directly, tested directly, on 2026-09-22. The short answer is that `--reranking` is a
FLAG, not a capability, and llama.cpp will accept it on a model that cannot do the job.

**The harness**: 3,000 rows paged out of `shared/mesh-chat`, 929 usable messages, 94 queries
with 8 candidates each. Ground truth is THREAD MEMBERSHIP - taken from the data rather than
from anyone's judgement - with auto-acks and status rows excluded, because an ack quoting its
parent would flatter every model. A RANDOM baseline is reported beside every result, because
a retrieval number with no floor cannot distinguish a working reranker from an arbitrary one.

| model | top-1 | top-3 | MRR | median |
|---|---:|---:|---:|---:|
| random (floor) | 0.11 | 0.37 | 0.339 | - |
| **bge-reranker-v2-m3 (Q8, the resident one)** | **0.64** | **0.88** | **0.761** | 2125 ms |
| Qwen3-Embedding-0.6B + `--reranking` | 0.12 | 0.40 | 0.351 | 4054 ms |
| Qwen3-Reranker-0.6B-Q8, standard GGUF | 0.14 | 0.39 | 0.370 | 4028 ms |

**Both Qwen configurations sit at the random floor, at twice the latency.** The second one
matters most: it is the CORRECT model family - a cross-encoder reranker, not an embedder -
and its off-the-shelf GGUF still does not work. On load, llama.cpp says why:

    model default pooling_type is [-1], but [4] was specified

Rank pooling is FORCED, not native. `--reranking` needs a classification head
(`cls.output.weight`); Qwen3-Reranker scores relevance through yes/no TOKEN LOGITS instead,
so a standard conversion produces no such head and the server returns near-zero scores for
everything (~4e-8 vs ~1e-9). That is llama.cpp issue 16407, and getting a working Qwen
reranker needs the special conversion described there, not a flag.

**A trap that costs an afternoon**: `--reranking` without explicit `-b/-ub` clamps `n_batch`
to 512, and every real-length document then fails with HTTP 500 "input (728 tokens) is too
large to process". The working reranker runs `-b 8192 -ub 8192`. A first pass at this
evaluation lost all six of its queries to exactly that, which reads as "the model failed"
rather than "the server was misconfigured".

**What this says about the resident reranker**: it is genuinely good on this corpus - 0.64
top-1 against a 0.11 floor, on real conversations. That is a measurement about
`shared/mesh-chat` and NOT about any other corpus; a reranker that helps here is not thereby
shown to help on a wiki or a code index, and the sign has to be measured per corpus.

**And it exposed a defect in this repo's own probe.** `probe_rerank` asked ONE question with
TWO documents. That is a coin flip, and the chance-level Qwen model won it - the probe
reported `ok=True, "rerank separated the known pair"`. It now asks two questions with eight
candidates each and requires the relevant document to rank FIRST in both, so a chance ranker
passes about 1.6% of the time rather than 50%. Verified in both directions against live
servers: bge passes, both Qwen configurations fail.
