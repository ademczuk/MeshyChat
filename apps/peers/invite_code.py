"""Invitation codes for `POST /v1/invites`.

A code is six words drawn uniformly, with the operating system's random source, from the EFF
Diceware long wordlist and joined with hyphens: `abacus-...`, lowercase ASCII. `invite_wordlist.txt`
has 7,772 words (sha256 `c475f10ffd19c4d1177294eb84af070e225a9b41ae98d106ed34eb51677f935e`), so a code
has 6 x log2(7772), about 77.5 bits. The redeem route folds a code with
`strip().lower()` before its lookup (apps/authapi/views.py), and every word here is already
lowercase, so a minted code redeems as minted.

The list is read once and checked on first use against its required floors: at least
7,000 entries, at least 75 bits for six words, every entry
non-empty lowercase ASCII, no duplicates. A list that fails them refuses to mint rather than
mint weak codes.
"""
from __future__ import annotations

import math
import secrets
from functools import lru_cache
from pathlib import Path

WORDS_PER_CODE = 6
MIN_WORDLIST_LEN = 7000
MIN_ENTROPY_BITS = 75.0
_PATH = Path(__file__).with_name("invite_wordlist.txt")


@lru_cache(maxsize=1)
def wordlist() -> tuple[str, ...]:
    words = tuple(w for w in _PATH.read_text(encoding="ascii").split() if w)
    if len(words) < MIN_WORDLIST_LEN:
        raise RuntimeError(f"invite wordlist has {len(words)} entries; want {MIN_WORDLIST_LEN}")
    if math.log2(len(words)) * WORDS_PER_CODE < MIN_ENTROPY_BITS:
        raise RuntimeError("invite wordlist gives less than the entropy floor")
    if len(set(words)) != len(words):
        raise RuntimeError("invite wordlist contains a duplicate entry")
    bad = [w for w in words if not (w.isascii() and w.isalpha() and w.islower())]
    if bad:
        raise RuntimeError(f"invite wordlist entry {bad[0]!r} is not lowercase ASCII")
    return words


def generate_code() -> str:
    """Return six words selected with `secrets.choice` from the OS random source."""
    words = wordlist()
    return "-".join(secrets.choice(words) for _ in range(WORDS_PER_CODE))


__all__ = ["MIN_ENTROPY_BITS", "MIN_WORDLIST_LEN", "WORDS_PER_CODE", "generate_code", "wordlist"]
