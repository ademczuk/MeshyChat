"""The ONE place mesh content leaves this process, and the MUST-8 guard in front of it.

WHAT MUST-8 SAYS (sql/028:35-37): "no Gemini key / no embedding" - no third-party key and
no mesh content to an external API. Divergence 019 lifts the "no embedding" half for a
separate table, under an operator gate, and keeps the other half absolute: mesh content
goes only to a model on this machine or on the private LAN.

`post_content` is the chokepoint. Every embed, rerank and chat call that carries mesh
text goes through it, and it refuses before a socket is opened when the provider's
base_url is not an IP-literal loopback or RFC1918 host (apps/modelhost/locality.py).
Two further leaks it closes, each of which a plain `urllib.request.urlopen` has:

  * PROXIES. urlopen honours HTTP_PROXY/HTTPS_PROXY from the environment (and, on
    Windows, the registry), so a LAN URL can be delivered to whatever host the proxy
    variable names. The opener here is built with an EMPTY ProxyHandler. A default
    ProxyHandler reads the environment when the OPENER IS BUILT, which for a module-level
    opener is process start-up; the test therefore builds it through `_content_opener()`
    with the variables already set, as a deployment would have them.
  * REDIRECTS. urllib already refuses a 307/308 to a POST, but it FOLLOWS a 301/302/303
    as a GET to whatever Location the LAN server names: a connection to an arbitrary
    host, carrying the path. Every redirect is refused outright.

Both are tested by pointing them at a recording server and asserting it saw nothing.

PROVIDERS come from MESHY_MODEL_PROVIDERS (apps/modelhost/registry.py), in DECLARATION
ORDER, which is the preference order: HQB first and the local GPU second is written as
that order in the list (see apps/meshsem/README.md). Kinds used here: `embed` (only
providers that declare the space), `rerank`, `chat`.
"""
from __future__ import annotations

import dataclasses
import json
import math
import threading
import time
import urllib.error
import urllib.request

import numpy as np

from apps.modelhost import locality, registry

from . import spaces

#: Per-call ceiling for a content call. A call gets min(this, what is left of its budget).
EMBED_TIMEOUT_S = 10.0
RERANK_TIMEOUT_S = 15.0
CHAT_TIMEOUT_S = 60.0

#: After a rerank provider fails at the TRANSPORT (timed out, refused, reset), it is not
#: asked again for this long; each request meanwhile records a typed `not_tried` attempt
#: saying so. Reranking is on by default (divergence 026, as amended), so without this a
#: hung reranker added RERANK_TIMEOUT_S to EVERY search and context request (measured by
#: the review of fin/read: 15.2 s a request, against 0.3 s with rerank=0). An HTTP error
#: status or a malformed answer comes back fast and does not start a cooldown. When the
#: cooldown ends, ONE request asks the provider again (the probe) and the others keep
#: skipping it until that answer is in: without that, every request in flight when the
#: cooldown lapsed asked a hung provider and waited RERANK_TIMEOUT_S (concurrent requests
#: are real: the served app runs each sync view on its own thread).
RERANK_COOLDOWN_S = 30.0

#: A reasoning model answers into `reasoning_content` first; below this it can spend the
#: whole budget thinking and return empty `content` (apps/modelhost/probes.py
#: CHAT_MAX_TOKENS, measured on gemma-4-26b-a4b).
CHAT_MIN_TOKENS = 512


class ContentRefused(RuntimeError):
    """MUST-8: this provider may not receive mesh content. Never retried, never logged
    as a transport fault: it is a policy verdict about the destination."""


class ProviderError(RuntimeError):
    """The provider was allowed and asked, and did not answer usefully."""


class ProviderUnreachable(ProviderError):
    """No answer at all: the connection was refused or reset, or the call timed out. The
    one kind of failure that costs its full timeout, so the rerank cooldown keys on it."""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None makes the 3xx surface as an HTTPError instead of being followed.
        return None


def _content_opener() -> urllib.request.OpenerDirector:
    """No proxies, no redirects. See the module docstring for why each is a MUST-8 leak."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


_OPENER = _content_opener()


def require_content_ok(provider: registry.Provider) -> None:
    where = locality.classify(provider.base_url)
    if not where.content_ok:
        raise ContentRefused(f"provider {provider.id!r} refused for mesh content (MUST-8, "
                             f"sql/028:35-37): {where.reason}")


def _open_json(req: urllib.request.Request, timeout: float) -> dict:
    """Send `req` through the content opener and parse a JSON object, or raise ProviderError
    (ProviderUnreachable for transport). The caller has already passed MUST-8. `_OPENER` is
    read at call time, so a test that replaces it is the opener every request uses."""
    try:
        with _OPENER.open(req, timeout=max(0.1, timeout)) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise ProviderUnreachable(f"unreachable: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise ProviderUnreachable(f"transport: {type(exc).__name__}") from None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ProviderError("response was not JSON") from None
    if not isinstance(parsed, dict):
        raise ProviderError("response was not a JSON object")
    return parsed


def post_content(provider: registry.Provider, path: str, payload: dict,
                 timeout: float) -> dict:
    """POST mesh content to `provider`. THE chokepoint: MUST-8 first, then the request.

    Raises ContentRefused (policy) or ProviderError (transport or shape). Never returns
    a partial answer.
    """
    require_content_ok(provider)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{provider.base_url.rstrip('/')}{path}", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    return _open_json(req, timeout)


def content_providers(kind: str, space: str | None = None
                      ) -> tuple[list[registry.Provider], list[dict]]:
    """(eligible in preference order, refused with reasons). Refusals are RETURNED so a
    response can say which declared provider MUST-8 kept out, rather than the provider
    list quietly being shorter than the operator believes.

    The same holds for an `embed` entry whose declared space is not `space` (E20): it is
    never asked, and it is returned as a `space_mismatch` naming the space it declares and
    the configured one. Skipped silently, a registry whose only embed entry named the
    wrong space failed every job tick with "no embed provider declared", naming nothing
    the operator could fix."""
    declared, _errors = registry.declared()
    eligible, refused = [], []
    for p in declared:
        if p.kind != kind:
            continue
        if kind == "embed" and p.space != space:
            declares = f"declares space {p.space!r}" if p.space else "declares no space"
            refused.append({"id": p.id, "verdict": "space_mismatch",
                            "declared_space": p.space, "configured_space": space,
                            "reason": f"{declares}; the configured space "
                                      f"(MESHY_SEMANTIC_SPACE) is {space!r}"})
            continue
        where = locality.classify(p.base_url)
        if not where.content_ok:
            refused.append({"id": p.id, "verdict": "refused_must8", "reason": where.reason})
            continue
        eligible.append(p)
    return eligible, refused


# ------------------------------------------------------------------------- embed

class EmbedUnavailable(RuntimeError):
    """No provider produced admitted vectors. `attempts` says what each one did."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


@dataclasses.dataclass(frozen=True)
class Embedded:
    vectors: np.ndarray              #: len(texts) x dims, float32, in input order
    provider_id: str
    #: Present when this call's canaries were checked against a reference.
    canary_min_cosine: float | None
    #: The canary vectors, returned so the job can RECORD them when the space has no
    #: reference yet. A request never does.
    canary_vectors: np.ndarray
    attempts: list[dict]


def _vectors_of(parsed: dict, n: int) -> np.ndarray:
    data = parsed.get("data")
    if not isinstance(data, list) or len(data) != n:
        raise ProviderError(f"expected {n} embeddings, got "
                            f"{len(data) if isinstance(data, list) else 'none'}")
    rows: list = [None] * n
    for pos, item in enumerate(data):
        if not isinstance(item, dict):
            raise ProviderError("embedding entry is not an object")
        idx = item.get("index", pos)
        vec = item.get("embedding")
        if isinstance(vec, list) and vec and isinstance(vec[0], list):
            vec = vec[0]                     # llama.cpp may nest one level
        if not isinstance(idx, int) or not 0 <= idx < n or rows[idx] is not None:
            raise ProviderError("embedding indexes are missing, out of range or repeated")
        if not isinstance(vec, list) or not vec or any(
                not isinstance(x, (int, float)) or isinstance(x, bool) for x in vec):
            # A null inside a vector is the measured degenerate response
            # (apps/modelhost/probes.py); it is refused, never coerced to 0.0.
            raise ProviderError("embedding is missing or contains non-numbers")
        rows[idx] = vec
    widths = {len(r) for r in rows}
    if len(widths) != 1:
        raise ProviderError(f"embeddings of differing widths {sorted(widths)}")
    return np.asarray(rows, dtype=np.float32)


def embed_in_space(space: spaces.Space, texts: list[str], reference: np.ndarray | None,
                   *, budget_s: float = 20.0, record_if_unreferenced: bool = False
                   ) -> Embedded:
    """Embed `texts` (prompts ALREADY applied) with the first provider ADMITTED to `space`.

    The space's canaries ride in the SAME request as the texts, so the vectors returned
    are proven to come from the admitted space by the very call that produced them: no
    cache, no time-of-check gap. Measured cost on HQB: 5 canaries plus one query in a
    single call, under 0.1 s.

    Failover is WITHIN the space only. A provider whose canaries miss the reference is
    refused and the next is tried; nothing else about it is trusted.

    `reference` None means the space has none yet. A request passes False for
    `record_if_unreferenced` and gets EmbedUnavailable; only the job may make the first
    well-formed, discriminating provider the reference (the caller stores it).
    """
    eligible, refused = content_providers("embed", space.name)
    attempts: list[dict] = list(refused)
    if reference is None and not record_if_unreferenced:
        raise EmbedUnavailable(f"space {space.name!r} has no recorded reference; the embed "
                               "job records it on its first run", attempts)
    canaries = space.canary_inputs()
    n_c = len(canaries)
    deadline = time.monotonic() + budget_s
    for p in eligible:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            attempts.append({"id": p.id, "verdict": "not_tried", "reason": "budget spent"})
            continue
        started = time.monotonic()
        try:
            parsed = post_content(p, "/v1/embeddings",
                                  {"model": p.model or space.family,
                                   "input": canaries + list(texts)},
                                  timeout=min(EMBED_TIMEOUT_S, remaining))
            vecs = _vectors_of(parsed, n_c + len(texts))
        except ContentRefused as exc:              # pragma: no cover - filtered above
            attempts.append({"id": p.id, "verdict": "refused_must8", "reason": str(exc)})
            continue
        except ProviderError as exc:
            attempts.append({"id": p.id, "verdict": "failed", "reason": str(exc),
                             "ms": int((time.monotonic() - started) * 1000)})
            continue
        canary_vecs, content = vecs[:n_c], vecs[n_c:]
        if reference is None:
            verdict = spaces.may_become_reference(space, canary_vecs)
        else:
            verdict = spaces.admit(space, canary_vecs, reference)
        if not verdict.ok:
            attempts.append({"id": p.id, "verdict": "not_admitted", "reason": verdict.reason,
                             "min_cosine": verdict.min_cosine})
            continue
        bad = spaces.malformed(content, space.dims) if len(texts) else None
        if bad:
            attempts.append({"id": p.id, "verdict": "failed", "reason": bad})
            continue
        attempts.append({"id": p.id, "verdict": "admitted", "min_cosine": verdict.min_cosine,
                         "ms": int((time.monotonic() - started) * 1000)})
        return Embedded(vectors=content, provider_id=p.id,
                        canary_min_cosine=verdict.min_cosine, canary_vectors=canary_vecs,
                        attempts=attempts)
    if not eligible:
        skipped = [a for a in refused if a["verdict"] == "space_mismatch"]
        named = "".join(f"; {a['id']!r} {a['reason'].split(';')[0]}" for a in skipped)
        raise EmbedUnavailable(f"no embed provider declares space {space.name!r} and may "
                               "receive mesh content"
                               + (f" (the configured space, MESHY_SEMANTIC_SPACE){named}"
                                  if skipped else ""), attempts)
    raise EmbedUnavailable(f"no provider was admitted to space {space.name!r}", attempts)


# ------------------------------------------------------------------------ rerank

@dataclasses.dataclass(frozen=True)
class Reranked:
    #: One score per document in input order, or None when no reranker ran. `ran` is
    #: what a response reports as `reranked`; it is True ONLY when scores exist.
    scores: list[float] | None
    provider_id: str | None
    attempts: list[dict]

    @property
    def ran(self) -> bool:
        return self.scores is not None


#: (provider id, base_url) -> (monotonic time the cooldown ends, the failure that began it,
#: whether one request is asking the provider again right now: the probe)
_rerank_down: dict[tuple[str, str], tuple[float, str, bool]] = {}
_rerank_down_lock = threading.Lock()


def forget() -> None:
    """Drop every rerank cooldown. Tests, and an operator after bringing a reranker back
    who does not want to wait out RERANK_COOLDOWN_S."""
    with _rerank_down_lock:
        _rerank_down.clear()


def _cooling(p: registry.Provider) -> tuple[str | None, bool]:
    """(why `p` is not being asked right now, or None; whether THIS request is the probe).
    A provider whose cooldown has ended is claimed by the first request to see that, in
    the lock, and every other request skips it until the probe's answer is in (`rerank`
    re-arms the cooldown on a transport failure and clears the entry on anything else)."""
    now = time.monotonic()
    key = (p.id, p.base_url)
    with _rerank_down_lock:
        entry = _rerank_down.get(key)
        if entry is None:
            return None, False
        until, why, probing = entry
        if probing:
            return f"cooling down: {why}; one request is asking it again", False
        if until <= now:
            _rerank_down[key] = (until, why, True)
            return None, True
    return (f"cooling down: {why}, {RERANK_COOLDOWN_S - (until - now):.0f}s ago; asked "
            f"again after {RERANK_COOLDOWN_S:.0f}s"), False


def rerank(query: str, documents: list[str], *, budget_s: float = 20.0,
           provider_ids: set[str] | None = None) -> Reranked:
    """Scores from the first rerank provider that returns one valid score per document.

    A response that does not score EVERY document exactly once is refused, not padded:
    a partially scored pool ordered as if reranked is the "claims reranked after falling
    back" trap, one level down. A provider that failed at the transport within the last
    RERANK_COOLDOWN_S is not asked (`not_tried`, and the reason says so); when that ends,
    one request asks it again and the others skip it until that answer is in.
    """
    eligible, refused = content_providers("rerank")
    # `provider_ids` (divergence 032) restricts the providers tried to those ids, still in
    # declaration order and still behind MUST-8: the related_to job pins the ONE reranker
    # whose canaries matched its calibration, so no call in its tick can fail over to a
    # model that was never admitted. None (the default) is every eligible provider.
    if provider_ids is not None:
        eligible = [p for p in eligible if p.id in provider_ids]
    attempts: list[dict] = list(refused)
    if not documents:
        return Reranked(None, None, attempts)
    deadline = time.monotonic() + budget_s
    for p in eligible:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            attempts.append({"id": p.id, "verdict": "not_tried", "reason": "budget spent"})
            continue
        cooling, probe = _cooling(p)
        if cooling is not None:
            attempts.append({"id": p.id, "verdict": "not_tried", "reason": cooling})
            continue
        started = time.monotonic()
        try:
            parsed = post_content(p, "/rerank", {"model": p.model or "rerank", "query": query,
                                                 "documents": documents,
                                                 "top_n": len(documents)},
                                  timeout=min(RERANK_TIMEOUT_S, remaining))
            scores = _scores_of(parsed, len(documents))
        except ProviderError as exc:
            if isinstance(exc, ProviderUnreachable):
                with _rerank_down_lock:
                    _rerank_down[(p.id, p.base_url)] = (
                        time.monotonic() + RERANK_COOLDOWN_S, str(exc), False)
                probe = False                   # re-armed: the claim is released with it
            attempts.append({"id": p.id, "verdict": "failed", "reason": str(exc),
                             "ms": int((time.monotonic() - started) * 1000)})
            continue
        finally:
            if probe:
                # The probe got an answer (scores, an HTTP error, a malformed body) or
                # raised: no transport failure, so no cooldown, and the claim ends here.
                with _rerank_down_lock:
                    _rerank_down.pop((p.id, p.base_url), None)
        attempts.append({"id": p.id, "verdict": "ran",
                         "ms": int((time.monotonic() - started) * 1000)})
        return Reranked(scores, p.id, attempts)
    return Reranked(None, None, attempts)


def _scores_of(parsed: dict, n: int) -> list[float]:
    results = parsed.get("results")
    if not isinstance(results, list) or len(results) != n:
        raise ProviderError("rerank did not return one result per document")
    scores: list = [None] * n
    for item in results:
        if not isinstance(item, dict):
            raise ProviderError("rerank result is not an object")
        idx, score = item.get("index"), item.get("relevance_score")
        if (not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < n
                or scores[idx] is not None):
            raise ProviderError("rerank indexes are missing, out of range or repeated")
        if (not isinstance(score, (int, float)) or isinstance(score, bool)
                or not math.isfinite(score)):
            raise ProviderError("rerank score is missing or not a finite number")
        scores[idx] = float(score)
    return scores


# -------------------------------------------------------------------------- chat

@dataclasses.dataclass(frozen=True)
class ChatReply:
    text: str
    provider_id: str
    finish_reason: str | None
    #: How much the model reasoned before answering. Reported, never returned as text.
    reasoning_chars: int
    attempts: list[dict]
    #: The answer's own `usage` object (prompt and completion tokens), or None when the
    #: provider sent none, and the `model` it named (divergence 037: a summary's
    #: provenance reports both).
    usage: dict | None = None
    model: str | None = None


class ChatUnavailable(RuntimeError):
    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


def chat(messages: list[dict], *, max_tokens: int = CHAT_MIN_TOKENS,
         temperature: float = 0.2, budget_s: float = 90.0,
         template_kwargs: dict | None = None, provider_ids: set[str] | None = None,
         params: dict | None = None, json_schema: dict | None = None,
         accept_empty: bool = False, timeout_s: float | None = None) -> ChatReply:
    """A chat completion over mesh content, from the first allowed provider that ANSWERS.

    For the graph chat that consumes /v1/mesh/context. Reads `content` only: a reasoning
    model's `reasoning_content` is its scratchpad, and a reply that is empty while
    reasoning is present is a failure of THAT provider (it ran out of budget thinking),
    so the next one is tried rather than the scratchpad being returned as the answer.

    `template_kwargs` is sent as llama-server's `chat_template_kwargs`. apps/meshaudit
    passes `{"enable_thinking": false}` (divergence 021): measured on HQB's
    gemma-4-26b-a4b, its select-among-candidates prompt ran out of 1,536 and of 2,048
    tokens still reasoning, while the same prompt without thinking answered in 0.4 s.

    Divergence 037 adds, each additive and off by default:
      * `provider_ids` restricts the providers tried to those ids, still in declaration
        order and still behind MUST-8, as `rerank`'s does for 032: a summary is measured
        for ONE model, and chat's default (try each until one answers) would let an
        unmeasured one answer;
      * `params`, every sampler setting sent as given (a value the request leaves out is
        the server's to change), overriding `temperature` and `template_kwargs`;
      * `json_schema`, passed through as llama-server's field of that name;
      * `accept_empty`: an empty `content` is RETURNED (as text "") rather than failed
        over, so a summary can refuse it as `answer_empty`;
      * `timeout_s`, the one call's ceiling in place of CHAT_TIMEOUT_S.
    The reply carries the answer's `usage` and `model`.
    """
    eligible, refused = content_providers("chat")
    if provider_ids is not None:
        eligible = [p for p in eligible if p.id in provider_ids]
    attempts: list[dict] = list(refused)
    deadline = time.monotonic() + budget_s
    ceiling = CHAT_TIMEOUT_S if timeout_s is None else float(timeout_s)
    for p in eligible:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            attempts.append({"id": p.id, "verdict": "not_tried", "reason": "budget spent"})
            continue
        try:
            payload = {"model": p.model or "chat", "messages": messages,
                       "max_tokens": max(int(max_tokens), CHAT_MIN_TOKENS),
                       "temperature": temperature}
            if template_kwargs:
                payload["chat_template_kwargs"] = dict(template_kwargs)
            if params:
                payload.update({k: v for k, v in params.items() if k != "max_tokens"})
            if json_schema is not None:
                payload["json_schema"] = json_schema
            parsed = post_content(p, "/v1/chat/completions", payload,
                                  timeout=min(ceiling, remaining))
        except ProviderError as exc:
            attempts.append({"id": p.id, "verdict": "failed", "reason": str(exc)})
            continue
        choices = parsed.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        msg = first.get("message") if isinstance(first, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        reasoning = msg.get("reasoning_content") if isinstance(msg, dict) else None
        finish = first.get("finish_reason") if isinstance(first, dict) else None
        usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else None
        model = parsed.get("model") if isinstance(parsed.get("model"), str) else None
        if accept_empty and (content is None or isinstance(content, str)):
            attempts.append({"id": p.id, "verdict": "answered", "finish_reason": finish})
            return ChatReply(text=(content or "").strip(), provider_id=p.id,
                             finish_reason=finish,
                             reasoning_chars=len(reasoning) if isinstance(reasoning, str)
                             else 0, attempts=attempts, usage=usage, model=model)
        if not isinstance(content, str) or not content.strip():
            attempts.append({"id": p.id, "verdict": "failed",
                             "reason": "empty content" + (
                                 " (reasoning present: the model spent its budget "
                                 "thinking)" if isinstance(reasoning, str) and reasoning
                                 else ""),
                             "finish_reason": finish})
            continue
        attempts.append({"id": p.id, "verdict": "answered", "finish_reason": finish})
        return ChatReply(text=content.strip(), provider_id=p.id, finish_reason=finish,
                         reasoning_chars=len(reasoning) if isinstance(reasoning, str) else 0,
                         attempts=attempts, usage=usage, model=model)
    raise ChatUnavailable("no chat provider answered", attempts)


# ----------------------------------------------------------------- tokenizer, info

def tokenize(provider: registry.Provider, text: str, *, timeout: float) -> int:
    """How many tokens `text` is on `provider`'s own tokenizer (llama-server's
    `POST /tokenize`, tokenizer only, no generation). Mesh text, so it goes through
    `post_content` and MUST-8 holds (divergence 037: a summary counts tokens, never
    estimates them from characters). Raises ContentRefused or ProviderError."""
    parsed = post_content(provider, "/tokenize", {"content": text}, timeout=timeout)
    tokens = parsed.get("tokens")
    if not isinstance(tokens, list):
        raise ProviderError("tokenize did not return a token list")
    return len(tokens)


def provider_info(provider: registry.Provider, path: str, *, timeout: float) -> dict:
    """A GET of a provider's description (`/props`, `/v1/models`). It carries no mesh text,
    but it is still a connection to the provider, so it is refused for a provider MUST-8
    refuses and made with the content opener (no proxy, no redirect): a pin read through
    a proxy would describe whatever the proxy chose."""
    require_content_ok(provider)
    req = urllib.request.Request(f"{provider.base_url.rstrip('/')}{path}", method="GET")
    return _open_json(req, timeout)


__all__ = ["CHAT_MIN_TOKENS", "ChatReply", "ChatUnavailable", "ContentRefused",
           "EmbedUnavailable", "Embedded", "ProviderError", "Reranked", "chat",
           "content_providers", "embed_in_space", "post_content", "provider_info",
           "require_content_ok", "rerank", "tokenize"]
