"""What THIS node can actually do, as evidence rather than configuration.

A provider is DECLARED in configuration and ADVERTISED only after its functional probe
passes. The two are deliberately different words: declaring one costs nothing and is what
an operator writes down; advertising one is a claim other nodes will act on.

    MESHY_MODEL_PROVIDERS = [
        {"id": "rerank-hqb-5090", "kind": "rerank", "base_url": "http://192.168.50.10:7876",
         "model": "bge-reranker-v2-m3"},
    ]

or the environment equivalent, `MESHY_MODEL_PROVIDERS` as a JSON array. The providers are
HQB's, on the LAN: HQD runs NO model servers by operator ruling (2026-09-23), so a
127.0.0.1 entry there declares something that is not listening. apps/meshsem/README.md
has the full list (embed :7875, rerank :7876, chat :7872).

## Why this app holds no database handle

Capability is a property of a node and its GPU, not of a tenant's rows. Keeping Postgres
out means this surface still answers while the database is down - which is exactly when an
operator is asking what a node can still do - and it keeps the app out of the RLS and
connection-role discussion entirely. Nothing here is per-tenant, so nothing here needs a
tenant predicate.

## Why nothing here starts a model

On this node the GPU is 8 GB total and swaps are arbitrated by the VRAM manager at
127.0.0.1:7878, which serialises on a shared Postgres lease and answers HTTP 409 rather
than racing a concurrent swapper. Loading a model to find out whether it loads would evict
whatever the operator had resident. So `absent` is a final verdict here; acting on it is an
operator decision, and `arbiter_id` is recorded only so a human knows what to ask the
arbiter to start.

## The cost model this inherits

Measured during the earlier capability-routed inference work: a cold model swap took
21.4 s and the served call that followed took 5.0 s. Those are different quantities and a
single latency number hides the one that matters. `probe()` therefore measures only the
SERVED path against an already-resident model; it never triggers, and never times, a load.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from dataclasses import dataclass

from apps.modelhost import locality, probes

#: Kinds this node understands. An unrecognised kind is refused rather than skipped, so a
#: typo in configuration is visible instead of silently reducing what the node advertises.
KNOWN_KINDS = frozenset(probes.PROBES)


@dataclass(frozen=True)
class Provider:
    id: str
    kind: str
    base_url: str
    model: str = ""
    arbiter_id: str = ""
    #: The embedding SPACE this provider CLAIMS (the project's design log (not part of this release) 019), e.g.
    #: "embeddinggemma-300m/768/raw-v1". A claim, not a fact: apps/meshsem admits
    #: the provider to the space only when its canary vectors match the space's recorded
    #: reference. An embed provider with no space is never used for mesh content.
    space: str = ""

    @staticmethod
    def from_mapping(raw: dict) -> "Provider":
        missing = [k for k in ("id", "kind", "base_url") if not raw.get(k)]
        if missing:
            raise ValueError(f"provider is missing required field(s): {missing}")
        kind = str(raw["kind"])
        if kind not in KNOWN_KINDS:
            raise ValueError(f"unknown provider kind {kind!r}; known: {sorted(KNOWN_KINDS)}")
        return Provider(id=str(raw["id"]), kind=kind, base_url=str(raw["base_url"]),
                        model=str(raw.get("model") or ""),
                        arbiter_id=str(raw.get("arbiter_id") or ""),
                        space=str(raw.get("space") or ""))


def declared() -> tuple[list[Provider], list[str]]:
    """(providers, configuration errors). Errors are RETURNED, not raised and not logged
    away: a provider that cannot be parsed is a capability an operator believes they have,
    and the registry is the only place that discovers otherwise."""
    raw = None
    try:
        from django.conf import settings
        if settings.configured:
            raw = getattr(settings, "MESHY_MODEL_PROVIDERS", None)
    except Exception:
        raw = None
    if raw is None:
        text = os.environ.get("MESHY_MODEL_PROVIDERS")
        if not text:
            return [], []
        try:
            raw = json.loads(text)
        except ValueError as exc:
            return [], [f"MESHY_MODEL_PROVIDERS is not valid JSON: {exc}"]
    if not isinstance(raw, list):
        return [], ["MESHY_MODEL_PROVIDERS must be a JSON array of provider objects"]

    out, errors, seen = [], [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            errors.append(f"provider entry is not an object: {entry!r:.60}")
            continue
        try:
            provider = Provider.from_mapping(entry)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if provider.id in seen:
            # Two providers with one id would make the report ambiguous about which one
            # the verdict belongs to.
            errors.append(f"duplicate provider id {provider.id!r}")
            continue
        seen.add(provider.id)
        out.append(provider)
    return out, errors


def probe_one(provider: Provider, timeout_s: float = probes.TIMEOUT_S) -> dict:
    """One provider's verdict. `available` is the ONLY field a consumer should route on."""
    probe = probes.PROBES[provider.kind]
    result = probe(provider.base_url, provider.model or "probe", timeout_s)
    return _report(provider, available=result.ok, evidence=result.as_json())


def _report(provider: Provider, *, available: bool, evidence: dict) -> dict:
    """One provider's entry in `providers`, probed or not: the ONE literal `probe_one` and
    `_not_probed` each used to spell out, so a field added for one is reported for the
    other. Key order and values are each caller's as before."""
    return {
        "id": provider.id,
        "kind": provider.kind,
        "model": provider.model,
        "available": available,
        "evidence": evidence,
        # Recorded so an operator can ask the arbiter to start it. Never acted on here.
        "arbiter_id": provider.arbiter_id,
        **_content_fields(provider),
    }


def _content_fields(provider: Provider) -> dict:
    """Whether MUST-8 lets mesh content reach this provider, and the space it claims.

    Reported so an operator can see BEFORE a search fails that a declared provider will
    be refused for mesh content (apps/meshsem/providers.py enforces it; this only says
    so). The verdict names the locality KIND, never the host: base_url stays off the
    wire (apps/modelhost/views.py). Divergence 019.
    """
    where = locality.classify(provider.base_url)
    return {"space": provider.space, "mesh_content_eligible": where.content_ok,
            "locality": where.kind}


#: TOTAL budget for one probe sweep, and a short cache in front of it.
#:
#: MEASURED, and this is a defect I shipped in the first version: one provider pointed at a
#: blackholed host held the request for 8,064 ms, and three would serialise to ~24 s. The
#: route is unauthenticated by design, so anyone could hold a worker that long, repeatedly.
#:
#: Two bounds, because they stop different things:
#:   SWEEP_BUDGET_S  caps ONE sweep. Providers not reached inside it are reported as
#:                   `not probed within the budget` rather than probed - an honest
#:                   "unknown" beats a slow "absent", because those have different fixes.
#:   CACHE_TTL_S     caps the RATE. Concurrent callers queue on one lock and the later ones
#:                   get the just-computed result, so request volume cannot multiply GPU
#:                   work. There is no bypass parameter: a bypass would restore the hammer.
#:
#: A cache makes the answer a claim about the PAST, which is the thing this app exists to
#: refuse - so every response carries `measured_at` and `age_ms`. A dated claim is honest;
#: an undated one is what lets a consumer route to a model evicted an hour ago.
SWEEP_BUDGET_S = 10.0
CACHE_TTL_S = 10.0

_lock = threading.Lock()
_cached: tuple[float, str, dict] | None = None


def _not_probed(provider: Provider, why: str) -> dict:
    return _report(provider, available=False, evidence={"ok": False, "reason": why})


def _sweep(budget_s: float) -> dict:
    """Probe every declared provider, stopping cleanly when the budget is spent."""
    providers, errors = declared()
    deadline = time.monotonic() + budget_s
    reports, exhausted = [], False
    for provider in providers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            exhausted = True
            reports.append(_not_probed(
                provider, f"not probed: the {budget_s:g}s sweep budget was spent on "
                          "earlier providers"))
            continue
        # EACH PROBE GETS ONLY WHAT IS LEFT. Checking the deadline only BETWEEN providers
        # made the bound weaker than it read: measured, three blackholed providers under a
        # 10s budget took 16.1s, because two 8s probes both started before the deadline.
        reports.append(probe_one(provider, min(probes.TIMEOUT_S, remaining)))
    working = sorted({r["kind"] for r in reports if r["available"]})
    return {
        "capabilities": working,
        "providers": reports,
        "config_errors": errors,
        "declared_count": len(providers),
        # Stated so `capabilities` is never read as complete when it is not.
        "budget_exhausted": exhausted,
    }


def snapshot(*, budget_s: float = SWEEP_BUDGET_S, ttl_s: float = CACHE_TTL_S) -> dict:
    """Everything this node can do, each entry carrying why.

    `capabilities` lists only the kinds with at least one PASSING provider, so a consumer
    that reads nothing else still cannot act on a declared-but-broken one.
    """
    global _cached
    with _lock:
        now = time.monotonic()
        if _cached is not None and (now - _cached[0]) < ttl_s:
            measured_mono, measured_at, payload = _cached
        else:
            payload = _sweep(budget_s)
            measured_mono = time.monotonic()
            measured_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
            _cached = (measured_mono, measured_at, payload)
        out = dict(payload)
        out["measured_at"] = measured_at
        out["age_ms"] = int((time.monotonic() - measured_mono) * 1000)
        return out


def forget() -> None:
    """Drop the cached sweep. For tests, and for an operator who has just started a model
    and does not want to wait out the TTL."""
    global _cached
    with _lock:
        _cached = None


__all__ = ["CACHE_TTL_S", "KNOWN_KINDS", "Provider", "SWEEP_BUDGET_S",
           "declared", "forget", "probe_one", "snapshot"]
