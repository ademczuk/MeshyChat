"""A fake HQB chat server for apps/meshsum tests: a REAL HTTP server on 127.0.0.1.

Real, so every test drives the production path: apps/meshsem/providers (its MUST-8
chokepoint, its opener, its parsing), the model pin read from `/props` and `/v1/models`,
and `/tokenize`. No live model is touched (tests/test_meshsum_live.py is the one that is,
skipped by default).

THE MODEL IS OBEDIENT (design 3.3): the validator is tested against the worst model, not
the one we have. When any message body in the prompt contains
`ANSWER-EXACTLY:<text>:END`, the fake returns exactly <text> as its answer. Otherwise it
answers literally and dully: one sentence per covered message (at most 8), "<author>
wrote to the thread.", citing that message.

It also:
  * tokenizes deterministically, one token per `chars_per_token` characters (ceil), and
    records every `/tokenize` payload, so a test can assert a gated row's canary was never
    tokenized (design 2.8);
  * records every chat payload and prompt;
  * serves `/props` and `/v1/models` shaped as HQB's llama-server b10566 does (measured
    2026-09-24), each field overridable, so the pin can change under a test;
  * can be down (503 on everything), hang for `delay` seconds, answer empty content, a
    chosen finish_reason, or refuse to tokenize.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OBEY_RE = re.compile(r"ANSWER-EXACTLY:(.*?):END", re.S)
HEADER_RE = re.compile(r"^\[(\d+)\] from (\S+) at ", re.M)

PROPS = {
    "model_path": "C:\\Projects\\llama-cpp\\models\\fake\\gemma-fake-Q4_K_XL.gguf",
    "build_info": "b10566-bb4caa754",
    "total_slots": 6,
    "model_alias": "gemma-4-26b-a4b",
    "is_sleeping": False,
    "chat_template": "{% for m in messages %}{{ m.content }}{% endfor %}",
    "default_generation_settings": {
        "n_ctx": 2816,
        "params": {"temperature": 1.0, "seed": 4294967295, "top_k": 64, "top_p": 0.95,
                   "min_p": 0.05},
    },
}
MODELS_META = {"size": 16995155064, "n_params": 25233142046, "n_vocab": 262144,
               "ftype": "Q4_K - Medium", "n_ctx_train": 262144}


class FakeSummaryModel:
    def __init__(self, *, chars_per_token: int = 4):
        self.chars_per_token = chars_per_token
        self.props = json.loads(json.dumps(PROPS))
        self.meta = dict(MODELS_META)
        self.down = False
        self.tokenize_down = False
        self.delay = 0.0
        #: Set to a string to answer exactly that, whatever the prompt says.
        self.fixed_answer: str | None = None
        self.finish_reason = "stop"
        self.empty = False
        self.prompts: list[str] = []
        self.payloads: list[dict] = []
        self.tokenized: list[str] = []
        self.gets: list[str] = []
        #: Called (with the payload) after a chat request is recorded, before answering:
        #: a test's hook for acting DURING the model call.
        self.during_call = None
        self._lock = threading.Lock()
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _send(self, obj, status=200):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):                       # noqa: N802
                with host._lock:
                    host.gets.append(self.path)
                if host.down:
                    return self._send({"error": "down"}, 503)
                if self.path.startswith("/props"):
                    return self._send(host.props)
                if self.path.startswith("/v1/models"):
                    return self._send({"object": "list", "data": [
                        {"id": host.props.get("model_alias", "fake"), "object": "model",
                         "meta": host.meta}],
                        "models": [{"name": "fake", "digest": ""}]})
                return self._send({"error": "not found"}, 404)

            def do_POST(self):                      # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                if host.down:
                    return self._send({"error": "down"}, 503)
                if self.path.endswith("/tokenize"):
                    if host.tokenize_down:
                        return self._send({"error": "no tokenizer"}, 500)
                    text = str(payload.get("content") or "")
                    with host._lock:
                        host.tokenized.append(text)
                    n = math.ceil(len(text) / host.chars_per_token) if text else 0
                    return self._send({"tokens": list(range(n))})
                if not self.path.endswith("/v1/chat/completions"):
                    return self._send({"error": "not found"}, 404)
                messages = payload.get("messages") or []
                prompt = "\n".join(str(m.get("content") or "") for m in messages)
                with host._lock:
                    host.prompts.append(prompt)
                    host.payloads.append(payload)
                if host.during_call is not None:
                    host.during_call(payload)
                if host.delay:
                    time.sleep(host.delay)
                content = host.answer_for(prompt)
                return self._send({
                    "model": host.props.get("model_alias", "fake"),
                    "choices": [{"index": 0, "finish_reason": host.finish_reason,
                                 "message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": math.ceil(len(prompt) / host.chars_per_token),
                              "completion_tokens": math.ceil(len(content) / 4),
                              "total_tokens": 0}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer_for(self, prompt: str) -> str:
        if self.empty:
            return ""
        if self.fixed_answer is not None:
            return self.fixed_answer
        obey = OBEY_RE.search(prompt)
        if obey:
            return obey.group(1)
        sentences = [{"text": f"{author} wrote to the thread.", "cites": [int(n)]}
                     for n, author in HEADER_RE.findall(prompt)][:8]
        return json.dumps({"sentences": sentences})

    def chats(self) -> int:
        return len(self.payloads)

    def provider(self, *, pid: str = "chat-fake", base_url: str | None = None) -> dict:
        return {"id": pid, "kind": "chat", "base_url": base_url or self.base_url,
                "model": "gemma-4-26b-a4b"}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
