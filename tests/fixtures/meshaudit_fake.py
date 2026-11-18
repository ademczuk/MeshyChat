"""A fake LAN chat model for apps/meshaudit tests: a REAL HTTP server on 127.0.0.1.

Real so every test drives the production path: apps/meshsem/providers.chat, its MUST-8
chokepoint, its opener, its response parsing. No live model is touched
(tests/test_meshaudit_live.py is the one that is, and it is skipped by default).

The answer is chosen by the MESSAGE the prompt is about: apps/meshaudit/prompt.py puts
`MESSAGE <key> sent by` on its own line, and a test registers what the model should say
about that key. An unregistered key gets `{}`, a strict JSON object proposing nothing,
which is what a model that finds nothing clear should say.

Like HQB's gemma-4-26b-a4b it is a REASONING model: unless the request carries
`chat_template_kwargs: {"enable_thinking": false}` it spends the whole budget in
`reasoning_content` and returns EMPTY content with `finish_reason: "length"`, which is
what the real one did on the auditor's prompt at 1,536 and 2,048 tokens (measured
2026-09-23). `ignores_no_think` makes it a server that ignores the flag. A test can also
make it truncate a parseable answer, or go down. Every prompt and payload is recorded so a
test can assert what was SENT, and to whom.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MESSAGE_RE = re.compile(r"^MESSAGE (msg/\S+) sent by", re.M)


class FakeChat:
    def __init__(self):
        #: key -> (content, finish_reason)
        self.answers: dict[str, tuple[str, str]] = {}
        self.down = False
        self.ignores_no_think = False
        self.prompts: list[str] = []
        self.payloads: list[dict] = []
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

            def do_POST(self):                      # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                if not self.path.endswith("/v1/chat/completions"):
                    return self._send({"error": "not found"}, 404)
                if host.down:
                    return self._send({"error": "model not loaded"}, 503)
                messages = payload.get("messages") or [{}]
                text = str(messages[-1].get("content") or "")
                with host._lock:
                    host.prompts.append(text)
                    host.payloads.append(payload)
                kwargs = payload.get("chat_template_kwargs") or {}
                if kwargs.get("enable_thinking") is not False or host.ignores_no_think:
                    msg = {"role": "assistant", "content": "",
                           "reasoning_content": "Which message does this answer? " * 60}
                    return self._send({"choices": [{"index": 0, "finish_reason": "length",
                                                    "message": msg}]})
                m = MESSAGE_RE.search(text)
                content, finish = host.answers.get(m.group(1) if m else "", ("{}", "stop"))
                msg = {"role": "assistant", "content": content}
                return self._send({"choices": [{"index": 0, "finish_reason": finish,
                                                "message": msg}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, key: str, obj=None, *, raw: str | None = None,
               finish: str = "stop") -> None:
        self.answers[key] = (raw if raw is not None else json.dumps(obj), finish)

    def prompts_about(self, key: str) -> list[str]:
        return [p for p in self.prompts if f"MESSAGE {key} sent by" in p]

    def provider(self, *, pid: str = "chat-fake", base_url: str | None = None) -> dict:
        return {"id": pid, "kind": "chat", "base_url": base_url or self.base_url,
                "model": "fake-gemma"}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
