"""FUNCTIONAL probes: does this endpoint actually do the thing, or does it just answer?

WHY THIS FILE IS THE WHOLE POINT, and it is not a hypothetical. Measured on this node,
2026-09-21, against the live reranker at 127.0.0.1:8100:

    GET  /health          -> {"status":"ok"}
    POST /v1/embeddings   -> HTTP 200, {"data":[{"embedding":[-0.0, 0.0, -0.0, ..., null,
                                                              ...]}]}

Both look healthy. The second is worthless: the process is llama-server started with
`--reranking --pooling rank`, so the model has no embedding head and the server answers
anyway with a degenerate vector. A registry that advertised "this node can embed" on the
strength of a 200 would have every consumer silently indexing zeros - and cosine similarity
over zero vectors is not an error, it is a number. Nothing downstream would report a fault.

So a capability here is only ever advertised on EVIDENCE OF FUNCTION:

    rerank    a document known to be relevant must score STRICTLY HIGHER than one known
              to be irrelevant. Catches a constant-scorer, which a shape check would pass.
    embed     the vector must have the right shape, be non-degenerate, AND DISCRIMINATE -
              a related pair must be closer than an unrelated pair. The shape check alone
              passes the all-zeros case above; the discrimination check is the one that
              actually fails it.
    generate  non-empty text back.
    chat      an instruction FOLLOWED, read from `content` and never from
              `reasoning_content`, with a budget a reasoning model can answer inside
              (divergence 019).

Each probe returns WHY it decided, because "capability absent" and "probe broken" are
different operational facts and a bare False conflates them.

NO PROBE STARTS A MODEL. Loading a model on this box is arbitrated by the VRAM manager
(127.0.0.1:7878), which serialises swaps on a shared Postgres lease and returns HTTP 409
rather than racing. A probe that started a model to see whether it could would evict
whatever the operator was using, on a card with 8 GB total. Absent is a verdict, not a
problem to fix here.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: Probe budget. Deliberately small: a probe is a liveness question, not a workload. The
#: reranker was measured at 8.7 s for 40 documents, so a 2-document probe has wide margin.
TIMEOUT_S = 8.0

#: TWO cases, each with EIGHT candidates, and the relevant document must rank FIRST in both.
#: Each case is a query, the one document that answers it, and seven distractors that no
#: lexical overlap rescues; chosen from this project's own domain so a model tuned on
#: generic web text cannot pass by accident.
#:
#: THE ONE-PAIR VERSION OF THIS PROBE PASSED A MODEL THAT RANKS AT CHANCE. Measured, not
#: imagined: Qwen3-Embedding-0.6B served with `--reranking --pooling rank` loads, emits
#: plausible scores, and scored top-1 0.12 against a RANDOM FLOOR of 0.11 over 94 queries
#: built from real mesh conversations - while bge-reranker-v2-m3 scored 0.64 on the same
#: set. llama.cpp accepts the flag; that model has no ranking head.
#:
#: One pair is one coin flip, and the chance ranker won it. Eight candidates put a chance
#: top-1 at 1/8, and requiring BOTH cases puts it near 1.6%. That is what this probe is
#: worth, stated rather than implied.
_RERANK_DISTRACTORS_A = (
    "Bananas ripen faster when stored beside apples.",
    "The uxxx comes in twice a day on most coastlines.",
    "Sourdough needs a starter kept at room temperature.",
    "Cast iron pans should be dried immediately after washing.",
    "Most laptop batteries prefer a partial charge for storage.",
    "The library closes early on public holidays.",
    "Repainting a hull is easier in dry weather.",
)
_RERANK_DISTRACTORS_B = (
    "Cycling uphill is easier in a lower gear.",
    "Bread dough rises faster in a warm kitchen.",
    "A kettle boils sooner with less water in it.",
    "Train tickets are cheaper booked in advance.",
    "Cut flowers last longer with fresh water.",
    "Wool jumpers should be dried flat.",
    "Coffee beans go stale faster once ground.",
)
_RERANK_CASES = (
    ("how does a peer rotate its signing key",
     "Key rotation is co-signed by the outgoing and incoming key, so the server can prove "
     "continuity of the peer identity.", _RERANK_DISTRACTORS_A),
    ("what does row level security do to a query",
     "An RLS policy filters rows per tenant, so a query returns only the rows the current "
     "role is allowed to see.", _RERANK_DISTRACTORS_B),
)

EMBED_ANCHOR = "row level security restricts which tenant can read a row"
EMBED_RELATED = "RLS policies decide row visibility per tenant"
EMBED_UNRELATED = "the uxxx comes in twice a day on most coastlines"


@dataclass(frozen=True)
class ProbeResult:
    """Why a capability is or is not advertised. `ok` alone would lose the reason."""
    ok: bool
    reason: str
    latency_ms: int | None = None
    detail: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        out = {"ok": self.ok, "reason": self.reason}
        if self.latency_ms is not None:
            out["latency_ms"] = self.latency_ms
        if self.detail:
            out["detail"] = self.detail
        return out


def _post(url: str, payload: dict, timeout: float = TIMEOUT_S) -> tuple[dict | None, str]:
    """POST JSON, return (parsed, error). Never raises: a probe that raises is a probe
    that takes the registry down with the thing it was measuring."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"unreachable: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"transport: {type(exc).__name__}"
    except ValueError:
        return None, "response was not JSON"


def _cosine(a: list[float], b: list[float]) -> float | None:
    if len(a) != len(b) or not a:
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return None                      # a zero vector has no direction, not zero angle
    return dot / (na * nb)


def _vector_of(parsed: dict) -> tuple[list[float] | None, str]:
    """Pull the embedding out, tolerating both OpenAI and llama.cpp shapes, and treating a
    null inside the vector as a hard fault rather than coercing it to 0.0 - the measured
    degenerate response contained nulls, and coercing would have hidden them."""
    data = parsed.get("data")
    if not isinstance(data, list) or not data:
        return None, "no `data` array in response"
    first = data[0]
    vec = first.get("embedding") if isinstance(first, dict) else None
    if isinstance(vec, list) and vec and isinstance(vec[0], list):
        vec = vec[0]                     # llama.cpp may nest one level
    if not isinstance(vec, list) or not vec:
        return None, "no `embedding` array in response"
    if any(v is None for v in vec):
        return None, "embedding contains nulls"
    if not all(isinstance(v, (int, float)) for v in vec):
        return None, "embedding contains non-numeric values"
    return [float(v) for v in vec], ""


def probe_rerank(base_url: str, model: str = "probe",
                 timeout_s: float = TIMEOUT_S) -> ProbeResult:
    """The relevant document must rank FIRST among eight, in BOTH cases.

    Ranking first, rather than merely out-scoring one distractor: out-scoring one distractor
    is a coin flip, and a chance-level model won it. See `_RERANK_CASES`.
    """
    started = time.monotonic()
    detail: dict = {}
    for case_no, (query, relevant, distractors) in enumerate(_RERANK_CASES, start=1):
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            ms = int((time.monotonic() - started) * 1000)
            return ProbeResult(False, "rerank probe ran out of time", ms, detail)
        # Relevant goes LAST, so a server echoing input order fails.
        documents = list(distractors) + [relevant]
        gold = len(documents) - 1
        parsed, err = _post(f"{base_url.rstrip('/')}/rerank", {
            "model": model, "query": query, "documents": documents,
            "top_n": len(documents),
        }, timeout=max(0.1, remaining))
        ms = int((time.monotonic() - started) * 1000)
        if parsed is None:
            return ProbeResult(False, f"rerank call failed ({err})", ms, detail)
        results = parsed.get("results")
        if not isinstance(results, list) or len(results) != len(documents):
            return ProbeResult(False, "rerank returned a different number of results than "
                                      "documents", ms, detail)
        scored = []
        for item in results:
            if not isinstance(item, dict):
                return ProbeResult(False, "rerank result entries are not objects", ms, detail)
            idx, score = item.get("index"), item.get("relevance_score")
            if not isinstance(idx, int) or not isinstance(score, (int, float)):
                return ProbeResult(False, "rerank result missing index or relevance_score",
                                   ms, detail)
            scored.append((float(score), idx))
        scored.sort(key=lambda t: -t[0])
        top_index = scored[0][1]
        detail[f"case{case_no}_top"] = top_index
        detail[f"case{case_no}_gold"] = gold
        if top_index != gold:
            return ProbeResult(False,
                               f"rerank did not rank the known-relevant document first in "
                               f"case {case_no} (ranked index {top_index} of "
                               f"{len(documents)})", ms, detail)
    ms = int((time.monotonic() - started) * 1000)
    return ProbeResult(True, "rerank ranked the relevant document first in both cases",
                       ms, detail)


def probe_embed(base_url: str, model: str = "probe",
                timeout_s: float = TIMEOUT_S, min_dim: int = 64) -> ProbeResult:
    """Shape, non-degeneracy, AND discrimination.

    THE THIRD CHECK IS THE LOAD-BEARING ONE. The measured failure on this node returns a
    full-length vector of signed zeros and nulls: it has the right shape and would pass any
    "did I get a vector back" test. Only asking whether the vectors MEAN anything - related
    closer than unrelated - rejects it.
    """
    started = time.monotonic()
    vectors: dict[str, list[float]] = {}
    for label, text in (("anchor", EMBED_ANCHOR), ("related", EMBED_RELATED),
                        ("unrelated", EMBED_UNRELATED)):
        # THE BUDGET IS FOR THE WHOLE PROBE, not per call. This probe makes three requests,
        # so a per-call timeout would let one provider spend three times its share.
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            ms = int((time.monotonic() - started) * 1000)
            return ProbeResult(False, "embeddings probe ran out of time", ms)
        parsed, err = _post(f"{base_url.rstrip('/')}/v1/embeddings",
                            {"model": model, "input": text}, timeout=max(0.1, remaining))
        if parsed is None:
            ms = int((time.monotonic() - started) * 1000)
            return ProbeResult(False, f"embeddings call failed ({err})", ms)
        vec, why = _vector_of(parsed)
        if vec is None:
            ms = int((time.monotonic() - started) * 1000)
            return ProbeResult(False, f"embeddings response unusable: {why}", ms)
        vectors[label] = vec
    ms = int((time.monotonic() - started) * 1000)

    dims = {k: len(v) for k, v in vectors.items()}
    if len(set(dims.values())) != 1:
        return ProbeResult(False, f"inconsistent embedding dimensions {dims}", ms)
    dim = next(iter(dims.values()))
    if dim < min_dim:
        return ProbeResult(False, f"embedding dimension {dim} below the {min_dim} floor",
                           ms, {"dim": dim})

    anchor = vectors["anchor"]
    magnitude = math.sqrt(sum(x * x for x in anchor))
    if magnitude == 0.0:
        return ProbeResult(False, "embedding is the zero vector", ms, {"dim": dim})
    spread = max(anchor) - min(anchor)
    if spread == 0.0:
        return ProbeResult(False, "embedding is constant across every dimension", ms,
                           {"dim": dim})

    near = _cosine(anchor, vectors["related"])
    far = _cosine(anchor, vectors["unrelated"])
    if near is None or far is None:
        return ProbeResult(False, "could not compare embeddings (zero-magnitude vector)",
                           ms, {"dim": dim})
    detail = {"dim": dim, "cos_related": round(near, 4), "cos_unrelated": round(far, 4)}
    if near <= far:
        return ProbeResult(False, "embeddings do not discriminate: a related sentence is "
                                  "no closer than an unrelated one", ms, detail)
    return ProbeResult(True, "embeddings are well-formed and discriminate", ms, detail)


def probe_generate(base_url: str, model: str = "probe",
                   timeout_s: float = TIMEOUT_S) -> ProbeResult:
    """Non-empty completion. Deliberately the weakest probe of the three: judging the
    QUALITY of generated text is not something a liveness check can do honestly, so this
    claims only that the endpoint produces text."""
    started = time.monotonic()
    parsed, err = _post(f"{base_url.rstrip('/')}/v1/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
        "max_tokens": 16,
        "temperature": 0.0,
    }, timeout=max(0.1, timeout_s))
    ms = int((time.monotonic() - started) * 1000)
    if parsed is None:
        return ProbeResult(False, f"chat completion call failed ({err})", ms)
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return ProbeResult(False, "completion returned no choices", ms)
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    text = (msg or {}).get("content") if isinstance(msg, dict) else None
    if not isinstance(text, str) or not text.strip():
        return ProbeResult(False, "completion returned empty text", ms)
    return ProbeResult(True, "completion returned text", ms, {"chars": len(text.strip())})


#: The token budget a chat probe grants. NOT 16, and the number is measured rather than
#: chosen: gemma-4-26b-a4b on HQB (192.168.50.10:7872, 2026-09-23) answers
#: "Reply with the single word: ready" by REASONING first, into `reasoning_content`, and
#: spent 62 completion tokens before emitting `content: "ready"`. With `max_tokens: 16`
#: the same call returned finish_reason "length", `content: ""` and a truncated
#: `reasoning_content` - which `probe_generate` above reads as "empty text" and refuses,
#: so a working reasoning model is reported absent. 512 is the floor the chat kind uses
#: everywhere a reasoning model may answer (apps/meshsem/providers.py CHAT_MIN_TOKENS).
CHAT_MAX_TOKENS = 512

#: The word the chat probe asks for. The answer must CONTAIN it, case-insensitively:
#: a chat endpoint that returns any non-empty string (an error page rendered as text, a
#: canned greeting) has not followed an instruction, and following one is the capability.
CHAT_WORD = "ready"


def probe_chat(base_url: str, model: str = "probe",
               timeout_s: float = TIMEOUT_S) -> ProbeResult:
    """A chat model that FOLLOWS an instruction, reasoning models included.

    WHY A SEPARATE KIND FROM `generate`. `generate` claims only that text comes back and
    grants 16 tokens, which a reasoning model spends thinking. `chat` grants
    CHAT_MAX_TOKENS and reads ONLY `content`, never `reasoning_content`: reasoning is the
    model's scratchpad, not its answer, and a consumer that shows it as the reply has
    shown the user the wrong text. A reply whose content is empty while reasoning is
    present is refused with a reason that says so, because "ran out of budget while
    thinking" and "no model here" need different fixes.
    """
    started = time.monotonic()
    parsed, err = _post(f"{base_url.rstrip('/')}/v1/chat/completions", {
        "model": model,
        "messages": [{"role": "user",
                      "content": f"Reply with the single word: {CHAT_WORD}"}],
        "max_tokens": CHAT_MAX_TOKENS,
        "temperature": 0.0,
    }, timeout=max(0.1, timeout_s))
    ms = int((time.monotonic() - started) * 1000)
    if parsed is None:
        return ProbeResult(False, f"chat completion call failed ({err})", ms)
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ProbeResult(False, "chat completion returned no choices", ms)
    first = choices[0]
    msg = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = msg.get("content")
    reasoning = msg.get("reasoning_content")
    detail = {"finish_reason": first.get("finish_reason"),
              "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0}
    if not isinstance(content, str) or not content.strip():
        if detail["reasoning_chars"]:
            return ProbeResult(False, "chat model returned reasoning but no content "
                                      f"(finish_reason={detail['finish_reason']!r}); it "
                                      "spent its budget thinking", ms, detail)
        return ProbeResult(False, "chat completion returned empty content", ms, detail)
    detail["chars"] = len(content.strip())
    if CHAT_WORD not in content.lower():
        return ProbeResult(False, f"chat model did not follow the instruction (content "
                                  f"does not contain {CHAT_WORD!r})", ms, detail)
    return ProbeResult(True, "chat model followed the instruction in its content", ms,
                       detail)


#: kind -> probe. The registry never invents a kind: an unknown kind is refused rather
#: than defaulted to something permissive.
PROBES = {"rerank": probe_rerank, "embed": probe_embed, "generate": probe_generate,
          "chat": probe_chat}

__all__ = ["CHAT_MAX_TOKENS", "PROBES", "ProbeResult", "TIMEOUT_S", "probe_chat",
           "probe_embed", "probe_generate", "probe_rerank"]
