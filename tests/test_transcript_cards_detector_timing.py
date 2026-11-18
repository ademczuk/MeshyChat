"""The loader's secret detectors stay linear on long texts (kxxx c80f5c, context.v1 screens whole 64 KiB items).

Before the fix, one search on 64,000 characters took 52.9 s (named_secret, "aB3_" repeated), 21.6 s
(password_assignment) and 9.6 s (url_credentials, "a" repeated); "key_" repeated took 35 s on only 4,000 for
named_secret, and "token=" and "http://" repeated took 5.9 s and 8.9 s at 64,000. After it, every detector and
seat detector is under 60 ms on HQB for each shape below. The bound here leaves room for a loaded machine.
The name and webhook rules have no length limit (a reviewer 75e4b6), so the long-name cases below must still match.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "transcript_cards"))
import transcript_cards as tc  # noqa: E402
import detector_timing as dt  # noqa: E402

N = 64 * 1024
BUDGET_MS = 250
SHAPES = list(dt.SHAPES.values())
RULES = list(tc.DETECTORS) + [(f"{s}:{n}", r) for s, rs in tc.SEAT_DETECTORS.items() for n, r in rs]


@pytest.mark.parametrize("unit", SHAPES, ids=list(dt.SHAPES))
def test_every_detector_is_fast_on_a_long_repeated_text(unit):
    text = dt.text_of(unit, N)
    slow = []
    for name, rx in RULES:
        t = time.perf_counter()
        rx.search(text)
        ms = 1000 * (time.perf_counter() - t)
        if ms >= BUDGET_MS:
            slow.append(f"{name} {ms:.0f} ms")
    assert not slow, f"{unit!r} x {N}: " + ", ".join(slow)


@pytest.mark.parametrize("text,name", [
    ("export DB_API_KEY=Zq81kd0aPLm2xx91", "named_secret"),
    ("my_service.access-key = 'Zq81kd0aPLm2xx91'", "named_secret"),
    ("GH_PAT_PROD=ghxZq81kd0aPLm2xx91", "pat_assignment"),
    ("DB_PASSWORD=s3cretWinter", "password_assignment"),
    ("mysql://root:hunter22@db.local/x", "url_credentials"),
    ("https://discord.com/api/webhooks/123/abcdefghijklmno", "hook_url"),
    # no length limit on the name or before the webhook marker
    ("API_KEY" + "_x" * 17 + "=Zq81kd0aPLm2xx91", "named_secret"),
    ("TOKEN_" + "a" * 70 + "=Zq81kd0aPLm2xx91", "named_secret"),
    ("PAT" + "_X" * 17 + "=Zq81kd0aPLm2xx91", "pat_assignment"),
    ("password" + "_x" * 17 + "=s3cretWinter", "password_assignment"),
    ("https://example.com/" + "p" * 150 + "/webhooks/123/abcdefghijklmno", "hook_url"),
    ("https://a.com/r?u=https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXX", "hook_url"),
    # a keyword part inside the name ends the walk; the match starts again at it
    ("token_key_value=Zq81kd0aPLm2xx91", "named_secret"),
    # a value with a ":" in it is still a value
    ("token=user:pass1234567", "named_secret"),
    ("api_key=sk-proj:Zq81kd0aPLm", "named_secret"),
])
def test_the_bounded_rules_still_catch_ordinary_secrets(text, name):
    assert dict(tc.DETECTORS)[name].search(text), (name, text)


def test_a_keyword_glued_to_a_word_is_still_not_a_name():
    # control for the dropped prefix: the keyword must follow a separator or a non-letter, as before
    assert not dict(tc.DETECTORS)["named_secret"].search("monkey=Zq81kd0aPLm2xx91")
