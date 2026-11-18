"""The two admission screens a context.v1 item passes in the write that makes it readable (docs/design/context-v1.md 3, 7).

  secrets       the loader's own detectors (tools/transcript_cards, txxxx's), so the server refuses what the seat's loader
                would have dropped and the two can never disagree about what a secret is;
  client names  the denylist file the server is configured with (MESHY_CONTEXT_DENYLIST). It is never in the repository:
                the names are the thing being kept out.

Both FAIL CLOSED, and a screen that cannot answer is its own code: E_CONTEXT_SCREEN_UNAVAILABLE is retried by the writer,
never turned into an omission, because write-once makes an omission permanent (msg/<key>). A refusal
never names what matched: the message would put the secret, or the denylisted name, into the caller's error.
"""
from __future__ import annotations

import json
import unicodedata

from apps.core import env
from apps.memoryskills.errors import HandlerError

SECRET = "E_CONTEXT_SECRET"
CLIENT_NAME = "E_CONTEXT_CLIENT_NAME"
UNAVAILABLE = "E_CONTEXT_SCREEN_UNAVAILABLE"
#: A marker shorter than this, once reduced to letters and digits, would refuse ordinary words. The file is refused whole.
MIN_MARKER_CHARS = 4


def _detectors():
    """The loader's DETECTORS, imported when first needed (the server never imports tools/ at start-up)."""
    try:
        from tools.transcript_cards import transcript_cards as tc
    except Exception as exc:  # noqa: BLE001 - any failure to load the detectors is "cannot screen", never "clean"
        raise HandlerError(UNAVAILABLE, f"the secret screen could not load its detectors ({type(exc).__name__}); "
                                        "retry later") from None
    return tc.DETECTORS


def _reduced(text: str) -> str:
    """Letters and digits only, after NFKC and case folding: `Zor blax-Corp` and `zorblaxcorp` are one name."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def _markers() -> list[str]:
    path = env.setting("MESHY_CONTEXT_DENYLIST", "MESHY_CONTEXT_DENYLIST", default="")
    if not path:
        raise HandlerError(UNAVAILABLE, "the client-name screen has no denylist configured (MESHY_CONTEXT_DENYLIST); "
                                        "retry later")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raise HandlerError(UNAVAILABLE, "the client-name screen could not read its denylist; retry later") from None
    if not isinstance(raw, list) or not raw or not all(isinstance(m, str) for m in raw):
        raise HandlerError(UNAVAILABLE, "the client-name denylist must be a non-empty JSON list of strings; retry later")
    markers = [_reduced(m) for m in raw]
    if any(len(m) < MIN_MARKER_CHARS for m in markers):
        raise HandlerError(UNAVAILABLE, f"a client-name marker is shorter than {MIN_MARKER_CHARS} letters and digits; "
                                        "the denylist is refused whole; retry later")
    return markers


def screen(*texts: str) -> None:
    """Refuse, with a typed code, when any of `texts` (each one WHOLE) holds a secret or a denylisted name.

    Both screens are loaded before either runs, so an unavailable screen is reported as such even when the other would
    have refused: the writer must not record a permanent omission while a screen is down."""
    detectors = _detectors()
    markers = _markers()
    for text in texts:
        for _name, rx in detectors:
            if rx.search(text):
                raise HandlerError(SECRET, "the item holds a secret; write it as an omitted item (reason \"secret\")")
    for text in texts:
        flat = _reduced(text)
        if any(m in flat for m in markers):
            raise HandlerError(CLIENT_NAME, "the item names a client or engagement; write it as an omitted item "
                                            "(reason \"client_name\")")


__all__ = ["CLIENT_NAME", "MIN_MARKER_CHARS", "SECRET", "UNAVAILABLE", "screen"]
