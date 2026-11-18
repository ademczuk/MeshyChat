"""The board deriver's model client: `generate(prompt) -> str` for `--model-url`.

Separate from `derive.py`, which takes `generate` INJECTED so that it never opens a socket,
and from `__main__.py`, which is an entry point that only parses, dispatches and prints.
`__main__` re-exports both names, so `from apps.board.__main__ import answer_text` keeps
working.
"""
from __future__ import annotations


def answer_text(message: dict) -> str:
    """The model's ANSWER, which is `content` and NOTHING ELSE.

    CORRECTED 2026-09-21, and the correction reversed what this file used to do. An earlier
    version concatenated `reasoning_content` as a fallback, on the reasoning that this
    node's 26B model routes output to the reasoning channel until it stops thinking, so a
    caller reading only `content` would record the node as unable to generate.

    A peer challenged it with a falsifiable question: is there ever a STOP-terminated answer
    whose content is empty and whose pick is only in the reasoning? Measured on 17 real
    prompts against the live model rather than argued:

        finish=stop    14 of 17, and ALL FOURTEEN carry parseable JSON in `content`
        finish=length   3 of 17, and NONE carries parseable JSON in `content`
        the cell in question (stop, empty content, JSON only in reasoning): ZERO

    So the fallback could only ever fire on a truncated generation, which is exactly where
    a JSON object in the thinking is a thought MID-FLIGHT rather than an answer, and the
    select-only rule downstream cannot tell a considered pick from an abandoned one. Two of
    the seventeen were that shape, so the fallback was not merely useless, it was the one
    path by which an abandoned thought could become a board proposal.

    The budget finding it was built on still stands and is separate: at 640 tokens this
    model returns nothing usable, at 2000 it answers. Give it room, then read only what it
    actually answered.
    """
    return message.get("content") or ""


def _http_generate(base_url: str, model: str, max_tokens: int):
    """A `generate(prompt) -> str` against an OpenAI-compatible chat endpoint.

    THROUGH THE MUST-8 CHOKEPOINT (divergence 019), `apps.meshsem.providers.post_content`.
    The prompt carries a sentence of a mesh message body, so this is mesh content leaving
    the process. It used to go out through a bare `urllib.request.urlopen`: no locality
    check on `--model-url`, the environment's HTTP_PROXY honoured, redirects followed.
    post_content refuses anything but an IP-literal loopback or RFC1918 host before it
    opens a socket, never uses a proxy and refuses a redirect
    (tests/test_board_model_url_must8.py).
    """
    from apps.meshsem import providers
    from apps.modelhost import registry

    provider = registry.Provider(id="board-derive --model-url", kind="chat",
                                 base_url=base_url, model=model)

    def generate(prompt: str) -> str:
        # Generous timeout ON PURPOSE. A client that cancels mid-generation leaves the
        # server's slot unreleased, and six of those exhaust the dispatch path; a timeout
        # here is the mechanism that wedges the shared model host.
        payload = providers.post_content(
            provider, "/v1/chat/completions",
            {"model": model, "max_tokens": max_tokens, "temperature": 0.0,
             "messages": [{"role": "user", "content": prompt}]}, timeout=600)
        return answer_text(payload["choices"][0]["message"])
    return generate
