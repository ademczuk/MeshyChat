"""The question the auditor asks, and the version stamp every answer is filed under.

The prompt asks the model to SELECT from lists and says so three ways, but nothing here
relies on the model obeying it: apps/meshaudit/validate.py refuses whatever it returns
that is not both offered and a fact. The body is untrusted text from any peer on the
mesh and may itself contain instructions; that is why the defence is the validation and
not the wording.

PROMPT_VERSION CARRIES A DIGEST OF THE WHOLE QUESTION, the template AND the generation
settings in REQUEST, the way divergence 019 files a space's reference beside a digest of
its canary inputs. Every inferred edge and every audit ledger row records it, so an edited
template or a changed budget is a new version automatically: the rows audited under the
old one are asked again, and the edges each question produced stay distinguishable. A
hand-maintained version string is one that somebody forgets to bump.

THINKING IS OFF (REQUEST["chat_template_kwargs"]). Measured 2026-09-23 on HQB's
gemma-4-26b-a4b with a synthetic three-message conversation: with thinking on, the model
deliberated over which earlier message a "done" answers and ended `finish_reason: length`
with EMPTY content at 1,536 tokens (3 of 3 runs) and at 2,048 (2 of 2), and finished once
in 2 runs at 2,200, the most its n_ctx of 2816 leaves after a 548-token prompt. With
`enable_thinking: false` it answered in 0.3-0.6 s, 60 completion tokens, the same
selection each run. The task is to PICK among a handful of listed values, and every pick
is checked against the database afterwards, so the reasoning bought latency and
truncations rather than safety.

SIZE. Bodies are cut at BODY_CHARS and context rows at SNIPPET_CHARS, so a prompt stays
near 500 tokens against that 2816 window.
"""
from __future__ import annotations

import hashlib
import json

from .validate import Candidates, ContextMessage

BODY_CHARS = 1200
SNIPPET_CHARS = 160
ELLIPSIS = chr(0x2026)   # built, not typed: this file stays ASCII

TEMPLATE = """You are auditing ONE message from a team chat. Its sender did not record
some of: who it was addressed to, which thread it belongs to, which earlier message it
answers. Decide those from the message and the messages sent just before it.

Choose ONLY from the candidate lists below. Never write a value that is not listed. If
the message does not make a field clear, answer null for it (or [] for addressed_to).
Answering null is always acceptable; guessing is not.

MESSAGE {key} sent by {author} at {created_at}:
<<<
{body}
>>>

Messages sent shortly before it, oldest first:
{context}

Candidates:
{candidates}

Reply with ONE JSON object and nothing else, with exactly these keys: {keys}.
{field_rules}"""

_FIELD_RULES = {
    "addressed_to": '"addressed_to": a list of the peers this message is addressed to, '
                    'chosen from the addressed_to candidates, or [].',
    "in_thread": '"in_thread": the one thread this message belongs to, chosen from the '
                 'in_thread candidates, or null.',
    "replies_to": '"replies_to": the one earlier message this message answers, chosen from '
                  'the replies_to candidates, or null.',
}

#: How the question is sent (apps/meshsem/providers.chat). 512 is that function's floor
#: and eight times the longest answer measured without thinking (60 tokens).
REQUEST = {"max_tokens": 512, "temperature": 0.0,
           "chat_template_kwargs": {"enable_thinking": False}}

PROMPT_VERSION = "audit-v1+" + hashlib.sha256(
    (TEMPLATE + json.dumps(_FIELD_RULES, sort_keys=True) + json.dumps(REQUEST, sort_keys=True))
    .encode("utf-8")).hexdigest()[:12]


def _clip(text: str, n: int, *, one_line: bool) -> str:
    text = " ".join((text or "").split()) if one_line else (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + ELLIPSIS


def render(*, key: str, author: str, created_at: str, body: str,
           context: list[ContextMessage], candidates: Candidates,
           asked: tuple[str, ...]) -> str:
    ctx = "\n".join(
        f"- {c.key} by {c.author} [thread: {c.thread or 'none'}]: "
        f"{_clip(c.snippet, SNIPPET_CHARS, one_line=True)}" for c in context) or "- (none)"
    offered = {"addressed_to": candidates.recipients, "in_thread": candidates.threads,
               "replies_to": candidates.replies}
    cand = "\n".join(f"{t}: {json.dumps(list(offered[t]))}" for t in asked)
    return TEMPLATE.format(
        key=key, author=author, created_at=created_at,
        body=_clip(body, BODY_CHARS, one_line=False),
        context=ctx, candidates=cand, keys=", ".join(json.dumps(t) for t in asked),
        field_rules="\n".join(_FIELD_RULES[t] for t in asked))


__all__ = ["BODY_CHARS", "PROMPT_VERSION", "REQUEST", "SNIPPET_CHARS", "TEMPLATE",
           "render"]
