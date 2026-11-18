"""A deterministic fake model host for apps/meshsem tests: embeddings, rerank, chat.

A REAL HTTP server on 127.0.0.1, so every test exercises the transport, the MUST-8
chokepoint and the response parsing the production path uses. No live model is touched
(tests/test_meshsem_live.py is the one that is, and it is skipped by default).

EMBEDDINGS are a bag of hashed words: each lowercase word maps to a fixed pseudo-random
unit vector seeded by (salt, word), and a text's vector is the normalised sum. Texts that
share words are therefore genuinely closer, so a ranking test ranks something real. The
`mode` decides which "model" answers:

    same    salt "model-A", the baseline the other modes are compared to.
    near    model-A plus a small text-seeded perturbation: cosine ~0.9998 to `same`,
            the measured distance between the same GGUF on two GPUs.
    other   salt "model-B": same width, different model. A DIFFERENT SPACE.
    noprompt  model-A with the space prompt stripped before embedding: the same model
            used with the wrong prompt style.
    prompted  model-A with EmbeddingGemma's retrieval document prompt ADDED before
            embedding: the same model behind a server that applies a prompt, pointed
            at a raw-text space (divergence 026).
    zeros   all-zero vectors (the degenerate llama.cpp response apps/modelhost measured).
    down    HTTP 503 on every call.

RERANK scores a document by how many query words it contains; `down` answers 503,
`short` returns one result fewer than asked, and `hang` records the request and answers
nothing until close() (a reranker that accepts connections and never answers). CHAT imitates a reasoning model: it reasons
into `reasoning_content` and only emits `content` when max_tokens >= 128.

Additive rerank controls for the persisted related_to edges (divergence 032), all read per
request so a test can flip them mid-test:

    rerank_table      {(query, document): score}, exact strings: a scripted score, so an
                      ASYMMETRIC pair (A->B high, B->A low) can be written down
    rerank_default    a score for every pair the table does not name (None: word overlap)
    shifted           a rerank_mode: every score plus 1.0 logit, the "different model
                      behind the same name" that a canary check must refuse
    rerank_fail_after N: after N rerank calls, every call fails as `rerank_fail_mode`
                      ("down" 503, "short" one result fewer, or "refuse" the 500 below)
    rerank_refuse     a predicate (query, documents) -> bool: True answers HTTP 500 with
                      the body HQB's llama.cpp reranker sends for a pair too large for its
                      physical batch (measured 2026-09-23, synthetic text), so a test can
                      make exactly one pair fail every time it is asked

`score_documents(query, docs)` is the scoring as a pure function, so a test can compute
the expected logits a calibration must hold for this host's canary answers.

THE COMMITTED CANARIES. Asked EXACTLY one of the committed calibration's canary sets
(apps/meshsem/calibration/related_calibration.json), the fake answers that set's
reference logits: it stands in for HQB's reranker the way its embeddings stand in for
HQB's embedder, so a flow that runs the related_to job against it (the fresh-install
runbook) is admitted. `shifted` still moves them, and no other input is affected.

Every request is recorded (path + JSON payload) so a test can assert what was SENT, which
is the only way to prove a forbidden row's text never left the process.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

WORD = re.compile(r"[a-z0-9]+")

CALIBRATION = (Path(__file__).resolve().parent.parent.parent / "apps" / "meshsem"
               / "calibration" / "related_calibration.json")


def committed_canaries() -> dict:
    """(query, documents) -> reference logits, from the committed calibration, or {}."""
    try:
        doc = json.loads(CALIBRATION.read_text(encoding="utf-8"))
        return {(c["query"], tuple(c["documents"])): [float(x) for x in c["reference"]]
                for c in doc["canaries"]["sets"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def word_vector(salt: str, word: str, dims: int) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(f"{salt}|{word}".encode()).digest()[:8], "little")
    v = np.random.default_rng(seed).standard_normal(dims)
    return v / np.linalg.norm(v)


def embed_text(text: str, dims: int, *, salt: str = "model-A") -> np.ndarray:
    words = WORD.findall(text.lower()) or ["<empty>"]
    v = sum(word_vector(salt, w, dims) for w in words)
    n = np.linalg.norm(v)
    return v / n if n else v


def near_text(text: str, dims: int, eps: float = 0.02) -> np.ndarray:
    base = embed_text(text, dims)
    seed = int.from_bytes(hashlib.sha256(("near|" + text).encode()).digest()[:8], "little")
    noise = np.random.default_rng(seed).standard_normal(dims)
    noise = noise / np.linalg.norm(noise) * eps
    v = base + noise
    return v / np.linalg.norm(v)


def strip_prompt(text: str) -> str:
    for marker in (" | text: ", " | query: ", "doc: ", "q: "):
        if marker in text:
            return text.split(marker, 1)[1]
    return text


class FakeModelHost:
    """One fake server. `mode` is read per request, so a test can flip it mid-test."""

    def __init__(self, dims: int = 64, mode: str = "same", rerank: str = "good",
                 chat: str = "reasoning"):
        self.dims, self.mode, self.rerank_mode, self.chat_mode = dims, mode, rerank, chat
        self.rerank_table: dict[tuple[str, str], float] = {}
        self.rerank_default: float | None = None
        self.rerank_fail_after: int | None = None
        self.rerank_fail_mode = "down"
        self.rerank_refuse = None
        self.canary_answers = committed_canaries()
        self.requests: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._released = threading.Event()      # ends every `hang` answer (close())
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _send(self, obj, status=200, headers=None):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):                      # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                with host._lock:
                    host.requests.append((self.path, payload))
                return host.answer(self, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ answers
    def answer(self, h, payload):
        path = h.path
        if path.endswith("/v1/embeddings"):
            return self._embeddings(h, payload)
        if path.endswith("/rerank"):
            return self._rerank(h, payload)
        if path.endswith("/v1/chat/completions"):
            return self._chat(h, payload)
        return h._send({"error": "not found"}, 404)

    def vector(self, text: str) -> np.ndarray:
        if self.mode == "near":
            return near_text(text, self.dims)
        if self.mode == "other":
            return embed_text(text, self.dims, salt="model-B")
        if self.mode == "noprompt":
            return embed_text(strip_prompt(text), self.dims)
        if self.mode == "prompted":
            return embed_text("title: none | text: " + text, self.dims)
        if self.mode == "zeros":
            return np.zeros(self.dims)
        return embed_text(text, self.dims)

    def _embeddings(self, h, payload):
        if self.mode == "down":
            return h._send({"error": "model not loaded"}, 503)
        inputs = payload.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        data = [{"index": i, "object": "embedding",
                 "embedding": [float(x) for x in self.vector(t)]}
                for i, t in enumerate(inputs or [])]
        # Reverse the order on the wire: a client that ignores `index` mis-assigns every
        # vector, and the tests would see it.
        return h._send({"model": payload.get("model"), "data": list(reversed(data))})

    def score_documents(self, query: str, docs: list) -> list[float]:
        """The scores this host answers for (query, docs), as a pure function."""
        q = set(WORD.findall(str(query).lower()))
        canary = self.canary_answers.get((str(query), tuple(str(d) for d in docs)))
        if canary is not None:
            return [x + (1.0 if self.rerank_mode == "shifted" else 0.0) for x in canary]
        out = []
        for i, d in enumerate(docs):
            scripted = self.rerank_table.get((str(query), str(d)))
            if scripted is not None:
                s = float(scripted)
            elif self.rerank_default is not None:
                s = float(self.rerank_default)
            else:
                s = float(len(q & set(WORD.findall(str(d).lower())))) - i * 1e-6
            if self.rerank_mode == "shifted":
                s += 1.0
            out.append(s)
        return out

    def _rerank(self, h, payload):
        if self.rerank_mode == "hang":
            self._released.wait(60)
            return None
        mode = self.rerank_mode
        if self.rerank_fail_after is not None:
            with self._lock:
                done = sum(1 for p, _ in self.requests if p.endswith("/rerank"))
            if done > self.rerank_fail_after:        # this request is already recorded
                mode = self.rerank_fail_mode
        if mode == "down":
            return h._send({"error": "down"}, 503)
        docs = payload.get("documents") or []
        refuse = self.rerank_refuse
        if mode == "refuse" or (refuse is not None and refuse(payload.get("query", ""), docs)):
            return h._send({"error": {"code": 500, "type": "server_error", "message": (
                "input (2270 tokens) is too large to process. increase the physical batch "
                "size (current batch size: 2048)")}}, 500)
        scores = self.score_documents(payload.get("query", ""), docs)
        results = [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]
        if mode == "short":
            results = results[:-1]
        return h._send({"results": results})

    def _chat(self, h, payload):
        if self.chat_mode == "down":
            return h._send({"error": "down"}, 503)
        budget = int(payload.get("max_tokens") or 16)
        reasoning = "The user wants a single word. " * 4
        if budget < 128:
            msg = {"role": "assistant", "content": "", "reasoning_content": reasoning[:40]}
            finish = "length"
        else:
            msg = {"role": "assistant", "content": "ready",
                   "reasoning_content": reasoning}
            finish = "stop"
        return h._send({"choices": [{"index": 0, "finish_reason": finish, "message": msg}]})

    # ------------------------------------------------------------------ helpers
    def embed_inputs(self) -> list[str]:
        """Every string ever sent for embedding, in order."""
        out = []
        for path, payload in self.requests:
            if path.endswith("/v1/embeddings"):
                inp = payload.get("input")
                out.extend([inp] if isinstance(inp, str) else list(inp or []))
        return out

    def calls(self, suffix: str) -> int:
        return sum(1 for p, _ in self.requests if p.endswith(suffix))

    def rerank_requests(self) -> list[dict]:
        """Every rerank payload, in order: what was SENT for scoring."""
        return [payload for p, payload in self.requests if p.endswith("/rerank")]

    def close(self) -> None:
        self._released.set()
        self.server.shutdown()
        self.server.server_close()


class Recorder:
    """A server that records anything sent to it and answers 200 {}. Stands in for an
    external host, a proxy, or a redirect target: a test asserts it saw NOTHING."""

    def __init__(self):
        self.hits: list[str] = []
        rec = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _any(self):
                rec.hits.append(f"{self.command} {self.path}")
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                raw = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = _any        # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def providers_json(*entries) -> str:
    return json.dumps(list(entries))
