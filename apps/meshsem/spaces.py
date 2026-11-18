"""Embedding SPACES, and the admission test that decides which provider may speak for one.

A SPACE, NOT A MODEL (docs/DIVERGENCES.md 019). A space is (model family, dims, prompt
style). Two vectors are comparable only inside one space, and nothing a provider SAYS
establishes which space it is in:

  * llama-server echoes the `model` the client sent. Measured 2026-09-23 on both
    embedders on this LAN: a request saying "x" came back `"model":"x"`.
  * apps/modelhost's embed probe checks shape, non-degeneracy and discrimination. A
    different 768-d model passes all three, so the registry would advertise an
    embedder whose vectors are incomparable with every one already stored.
  * The prompt is part of the space. The same EmbeddingGemma GGUF with no prompt scored
    cosine 0.8538-0.9170 against itself WITH the retrieval prompt, over four sentences.

So a provider is ADMITTED to a space only on evidence: it embeds a fixed canary set with
the space's own prompts, and every canary must match the space's REFERENCE at cosine
>= ADMISSION_COSINE. The first admitted provider records the reference (the job does
that, apps/meshsem/embedjob.py; a request never writes it).

THE THRESHOLD, measured rather than chosen. The same EmbeddingGemma-300M Q8_0 GGUF on
HQB (CUDA) and HQD (Vulkan), five canaries under both prompts, each embedded alone, in a
five-canary batch, and at the front and back of a 16-document batch: 45 condition pairs,
worst 0.999613, best 1.000000. Batch composition moves a vector about as far as changing
the host does, which is why the canaries can ride along inside a content batch (the
admission is then proven on the SAME call that produced the content vectors, with no
time-of-check gap) without the threshold having to loosen. 0.999 leaves 0.0006 of margin
under the worst same-model pair and is 0.08 above the nearest different-space
measurement.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

import numpy as np

#: Every canary must meet this against the reference. `>=`, and a mutation to `>` or to
#: a lower number is caught by tests/test_meshsem_spaces.py's boundary cases.
ADMISSION_COSINE = 0.999

#: The canaries must DISAGREE with each other at least this much before they may become
#: a reference. A constant embedder (apps/modelhost/probes.py measured one returning the
#: same vector for everything) would otherwise record a reference that every other
#: constant embedder then matches perfectly.
REFERENCE_MAX_PAIRWISE_COSINE = 0.95


@dataclasses.dataclass(frozen=True)
class Space:
    name: str
    family: str
    dims: int
    #: str.format templates with one `{text}` field.
    document_prompt: str
    query_prompt: str
    #: (prompt kind, sentence). Kinds are "document" or "query", so both prompt styles
    #: are proven on every admission, not only the one a given call happens to use.
    canaries: tuple[tuple[str, str], ...]
    #: The Matryoshka widths a RESIDENT VIEW of this space may hold (7 Nov 2026, pxx's design A on
    #: <thread>): the stored vectors' first `w` floats, renormalised, kept in
    #: memory only to PICK candidates; every score a caller sees is rescored at `dims` from the
    #: stored row. A view is not a new space: nothing is written at its width, and it is not part of
    #: the identity (canary_sha256 leaves it out). Empty: the model was not trained to be truncated.
    views: tuple[int, ...] = ()

    def document(self, text: str) -> str:
        return self.document_prompt.format(text=text)

    def query(self, text: str) -> str:
        return self.query_prompt.format(text=text)

    def canary_inputs(self) -> list[str]:
        """The exact strings sent to the model, prompts applied."""
        out = []
        for kind, sentence in self.canaries:
            out.append(self.document(sentence) if kind == "document" else self.query(sentence))
        return out

    def canary_sha256(self) -> str:
        """Digest of what the reference ANSWERS. Stored beside it (sql_meshychat/006), so
        a canary edit that did not rename the space is refused instead of comparing
        vectors of different sentences."""
        blob = json.dumps({"space": self.name, "dims": self.dims,
                           "inputs": self.canary_inputs()}, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_CANARIES = (
    ("document", "row level security restricts which tenant can read a row"),
    ("document", "a peer rotates its signing key with a co-signed handover"),
    ("document", "the uxxx comes in twice a day on most coastlines"),
    ("query", "which tenant can read a row"),
    ("query", "how does a node recover after a restart"),
)

#: EmbeddingGemma was trained with Matryoshka Representation Learning, so its 768-d vectors may be
#: truncated to 512, 256 or 128 dims and renormalised (Google's model card for EmbeddingGemma).
_EMBEDDINGGEMMA_VIEWS = (512, 256, 128)

#: THE DEFAULT since 2026-09-26 (divergence 026's amendment of that date, the operator's
#: decision). EmbeddingGemma's own retrieval templates: documents "title: none | text: ...",
#: queries "task: search result | query: ...". Divergence 019 shipped it; 026 replaced it
#: with the raw space below on 2026-09-23; three pre-registered measurements of 2026-09-26
#: brought it back. On 60 fresh queries sharing no word with their target, judged blind by
#: two nodes (F03, kappa 0.771), a prompted embed arm beat raw-v1 on 49 and lost on 9
#: (p = 9e-8). Through the server's own search code in both spaces (F04, at dc829ad): with
#: rerank=0 the target's fused rank was better on 27 queries and worse on 10 (p = 0.0076),
#: in the top 40 on 37 against 20; with rerank=1 the order was a null 17/14 but the target
#: reached the top 20 on 29 against 17, so the gain is recall into the rerank pool. On F02's
#: 30 ordinary queries (frozen labels) it is non-inferior: recall@5 identical at rerank=1,
#: +0.008 [-0.024, +0.040] at rerank=0.
EMBEDDINGGEMMA_300M = Space(
    name="embeddinggemma-300m/768/retrieval-v1",
    family="embeddinggemma-300m",
    dims=768,
    document_prompt="title: none | text: {text}",
    query_prompt="task: search result | query: {text}",
    canaries=_CANARIES,
    views=_EMBEDDINGGEMMA_VIEWS,
)

#: The same model on the RAW text, no prompt on either side. Still declared, so its vectors
#: and a deployment that pins MESHY_SEMANTIC_SPACE to it keep working.
#: HISTORY: the default from 2026-09-23 to 2026-09-26 (divergence 026). The A/B of
#: 2026-09-23 could not separate no prompt from the retrieval prompt on its own (MRR 0.529
#: vs 0.544, -0.016 [-0.058, +0.025]), but fused with the lexical arm the no-prompt space
#: was the better partner: recall@5 0.517 against 0.471, and the retrieval-prompt fusion
#: was significantly WORSE than TF-IDF alone on recall@5 (-0.070 [-0.135, -0.009]). Raw
#: text was also what the fleet's existing vectors were. F04 does not refute that result,
#: which was measured against another baseline (TF-IDF alone) with other labels (thread
#: membership).
#: The prompt style stays part of the identity: these canaries carry no prompt, so their
#: digest differs from the retrieval space's, and a provider that applies a prompt lands
#: 0.80-0.92 from this reference (measured on HQB, 2026-09-23) and is refused.
EMBEDDINGGEMMA_300M_RAW = Space(
    name="embeddinggemma-300m/768/raw-v1",
    family="embeddinggemma-300m",
    dims=768,
    document_prompt="{text}",
    query_prompt="{text}",
    canaries=_CANARIES,
    views=_EMBEDDINGGEMMA_VIEWS,
)

SPACES: dict[str, Space] = {s.name: s for s in (EMBEDDINGGEMMA_300M_RAW,
                                                EMBEDDINGGEMMA_300M)}

DEFAULT_SPACE = EMBEDDINGGEMMA_300M.name


def get(name: str) -> Space:
    """The declared space, or KeyError naming the ones that exist. Never a default: a
    misspelt space silently falling back to another would mix incomparable vectors."""
    try:
        return SPACES[name]
    except KeyError:
        raise KeyError(f"no embedding space named {name!r}; declared: {sorted(SPACES)}")


# ----------------------------------------------------------------- vector encoding

def encode(vec) -> bytes:
    """float32 little-endian, the storage format sql_meshychat/006 CHECKs the width of."""
    return np.asarray(vec, dtype="<f4").tobytes()


def decode(raw: bytes, dims: int) -> np.ndarray:
    arr = np.frombuffer(bytes(raw), dtype="<f4")
    if arr.shape[0] != dims:
        raise ValueError(f"vector has {arr.shape[0]} floats, space says {dims}")
    return arr


def encode_many(vecs: np.ndarray) -> bytes:
    return np.asarray(vecs, dtype="<f4").tobytes()


def decode_many(raw: bytes, dims: int) -> np.ndarray:
    arr = np.frombuffer(bytes(raw), dtype="<f4")
    if dims <= 0 or arr.shape[0] % dims:
        raise ValueError(f"{arr.shape[0]} floats do not divide into rows of {dims}")
    return arr.reshape(-1, dims)


# ----------------------------------------------------------------- admission

@dataclasses.dataclass(frozen=True)
class Admission:
    ok: bool
    reason: str
    #: The worst canary's cosine against the reference, when one could be computed.
    min_cosine: float | None = None


def malformed(vecs: np.ndarray, dims: int) -> str | None:
    """Why `vecs` cannot be vectors of this space, or None. Shape, finiteness, direction."""
    if vecs.ndim != 2 or vecs.shape[1] != dims:
        return f"expected {dims}-d vectors, got shape {tuple(vecs.shape)}"
    if not np.all(np.isfinite(vecs)):
        return "vectors contain NaN or infinity"
    norms = np.linalg.norm(vecs.astype(np.float64), axis=1)
    if np.any(norms == 0.0):
        return "a vector is all zeros and has no direction"
    return None


def _unit(vecs: np.ndarray) -> np.ndarray:
    v = vecs.astype(np.float64)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def may_become_reference(space: Space, canary_vecs: np.ndarray) -> Admission:
    """Whether the FIRST provider's canaries are fit to be the space's reference.

    There is nothing to compare against yet, so this asks the questions that do not need
    a reference: the right width, finite, and DISCRIMINATING (canaries on unrelated
    subjects must not collapse onto one direction).
    """
    if canary_vecs.shape[0] != len(space.canaries):
        return Admission(False, f"expected {len(space.canaries)} canary vectors, got "
                                f"{canary_vecs.shape[0]}")
    why = malformed(canary_vecs, space.dims)
    if why:
        return Admission(False, why)
    u = _unit(canary_vecs)
    sims = u @ u.T
    np.fill_diagonal(sims, -1.0)
    worst = float(sims.max())
    if worst >= REFERENCE_MAX_PAIRWISE_COSINE:
        return Admission(False, f"canaries do not discriminate (two unrelated canaries at "
                                f"cosine {worst:.4f})")
    return Admission(True, "fit to record as the reference")


def admit(space: Space, canary_vecs: np.ndarray, reference: np.ndarray) -> Admission:
    """THE identity test. Every canary must meet ADMISSION_COSINE against the reference."""
    if canary_vecs.shape[0] != len(space.canaries):
        return Admission(False, f"expected {len(space.canaries)} canary vectors, got "
                                f"{canary_vecs.shape[0]}")
    why = malformed(canary_vecs, space.dims)
    if why:
        return Admission(False, why)
    if reference.shape != canary_vecs.shape:
        return Admission(False, f"reference shape {tuple(reference.shape)} differs from "
                                f"canaries {tuple(canary_vecs.shape)}")
    cos = np.sum(_unit(canary_vecs) * _unit(reference), axis=1)
    worst = float(cos.min())
    if worst >= ADMISSION_COSINE:
        return Admission(True, f"every canary within the space (worst cosine {worst:.6f})",
                         worst)
    return Admission(False, f"a canary is at cosine {worst:.6f} to the reference, below "
                            f"{ADMISSION_COSINE}: this provider is a DIFFERENT space",
                     worst)


__all__ = ["ADMISSION_COSINE", "Admission", "DEFAULT_SPACE", "EMBEDDINGGEMMA_300M",
           "EMBEDDINGGEMMA_300M_RAW", "REFERENCE_MAX_PAIRWISE_COSINE", "SPACES", "Space",
           "admit", "decode", "decode_many", "encode", "encode_many", "get", "malformed",
           "may_become_reference"]
