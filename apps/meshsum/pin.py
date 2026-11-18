"""The model pin: what the chat provider reports it is, as one digest.

Divergence 037; docs/design/thread-summaries.md 5.5. A measurement is valid for one model
file, one chat template and one sampler configuration on one server build, not for a name
in a configuration: "a declared provider is not an available one"
(docs/MODEL-CAPABILITIES.md). So the measurement records `model_pin`, and the server
answers 503 E_SUMMARY_MODEL_CHANGED when the provider reports anything else.

WHAT IS PINNED (measured on HQB's llama-server b10566, 2026-09-24):
  from `/props`       model_path, build_info, total_slots,
                      default_generation_settings.n_ctx, sha256(chat_template),
                      sha256(default_generation_settings.params)
  from `/v1/models`   data[0].meta: size, n_params, n_vocab, ftype

Why each (critique C5): a path and a build do not name a FILE (a quantisation
re-published under the same name keeps its path); the chat template frames every prompt
and travels inside the file; the byte size and the template digest move when either
does. The sampler defaults are pinned because a value a request leaves out is the
server's to change. n_ctx and the slot count are pinned because the token budget and the
in-flight cap are fitted to 2,816 and 6. NOT pinned: `is_sleeping` (a state, not an
identity), and there is no content digest to pin (`/v1/models`' `digest` is empty on
this build). A same-size replacement with the same template is not excluded; that is
stated in the design, not solved here.

A PROVIDER THAT REPORTS ANY FIELD MISSING IS REFUSED, not assumed (PinIncomplete, which
the route answers as E_SUMMARY_MODEL_CHANGED). One that does not answer is PinUnavailable
(E_CHAT_UNAVAILABLE). Read at most once a minute per provider, cached and dated, as
apps/modelhost's sweep is.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time

from apps.meshsem import providers

#: How long a read pin is trusted before `current_pin` reads it again.
PIN_MAX_AGE_S = 60.0
PIN_TIMEOUT_S = 5.0


class PinUnavailable(RuntimeError):
    """The provider did not answer `/props` or `/v1/models`."""


class PinIncomplete(RuntimeError):
    """The provider answered without a field the pin covers: refused, not assumed."""


@dataclasses.dataclass(frozen=True)
class Pin:
    digest: str
    fields: dict
    read_at: float


def _sha(obj) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fields_of(props: dict, models: dict) -> dict:
    """The pinned fields, by explicit path. Raises PinIncomplete on any absence: a
    present-and-null value is absent too."""
    def need(obj, *path):
        cur = obj
        for part in path:
            if isinstance(part, int):
                if not isinstance(cur, list) or len(cur) <= part:
                    raise PinIncomplete(f"no {'.'.join(map(str, path))}")
                cur = cur[part]
            else:
                if not isinstance(cur, dict) or cur.get(part) is None:
                    raise PinIncomplete(f"no {'.'.join(map(str, path))}")
                cur = cur[part]
        return cur

    return {
        "model_path": need(props, "model_path"),
        "build_info": need(props, "build_info"),
        "total_slots": need(props, "total_slots"),
        "n_ctx": need(props, "default_generation_settings", "n_ctx"),
        "chat_template_sha256": _sha(need(props, "chat_template")),
        "default_params_sha256": _sha(need(props, "default_generation_settings", "params")),
        "size": need(models, "data", 0, "meta", "size"),
        "n_params": need(models, "data", 0, "meta", "n_params"),
        "n_vocab": need(models, "data", 0, "meta", "n_vocab"),
        "ftype": need(models, "data", 0, "meta", "ftype"),
    }


def read_pin(provider, *, timeout: float = PIN_TIMEOUT_S, clock=time.monotonic) -> Pin:
    try:
        props = providers.provider_info(provider, "/props", timeout=timeout)
        models = providers.provider_info(provider, "/v1/models", timeout=timeout)
    except providers.ContentRefused:
        raise
    except providers.ProviderError as exc:
        raise PinUnavailable(str(exc)) from None
    fields = fields_of(props, models)
    return Pin(digest="sha256:" + _sha(fields), fields=fields, read_at=clock())


_cache: dict[tuple[str, str], Pin] = {}
_lock = threading.Lock()


def current_pin(provider, *, max_age: float = PIN_MAX_AGE_S, clock=time.monotonic) -> Pin:
    """`read_pin`, at most once per `max_age` per provider (id and base_url)."""
    key = (provider.id, provider.base_url)
    now = clock()
    with _lock:
        hit = _cache.get(key)
    if hit is not None and now - hit.read_at < max_age:
        return hit
    got = read_pin(provider, clock=clock)
    with _lock:
        _cache[key] = got
    return got


def forget() -> None:
    """Drop every cached pin (tests; an operator after swapping a model on purpose)."""
    with _lock:
        _cache.clear()


__all__ = ["PIN_MAX_AGE_S", "Pin", "PinIncomplete", "PinUnavailable", "current_pin",
           "fields_of", "forget", "read_pin"]
