"""Mutants of `card recent`'s follow-up to #224: the overruled_by count and the receipt that is there but refused
(tools/meshy_node/cards.py).

Each mutant name is `<set>:<id> <description>`; `<set>` is its mutation-set name. The before
and after texts are literals, unchanged. #224's own mutants are not here.
Run: python tools/mutants/run.py --list meshy_node_card_recent
"""

CARDS = "tools/meshy_node/cards.py"

T_RECENT = ["tests/test_meshy_node_card_recent.py"]

MUTANTS = [
    ("crf:C1 subtracts seats, not entries", CARDS,
     "unlisted = count - len(listed) if",
     "unlisted = count - len(seats) if",
     T_RECENT),
    ("crf:C2 junk not counted as listed", CARDS,
     "listed = by if isinstance(by, list) else []",
     "listed = [k for k in by if isinstance(k, str) and key_parts(k)] if isinstance(by, list) else []",
     T_RECENT),
    ("crf:C3 no count read", CARDS,
     "unlisted = count - len(listed) if type(count) is int and count > len(listed) else 0",
     "unlisted = 0",
     T_RECENT),
    ("crf:C4 bool is a count", CARDS,
     "type(count) is int and count",
     "isinstance(count, int) and count",
     T_RECENT),
    ("crf:C5 million boundary", CARDS,
     "if unlisted <= 1_000_000 else",
     "if unlisted < 1_000_000 else",
     T_RECENT),
    ("crf:C6 no huge guard", CARDS,
     'if unlisted <= 1_000_000 else "over 1,000,000 more"',
     'if True else ""',
     T_RECENT),
    ("crf:C7 seats dropped on a cut", CARDS,
     "if seats and unlisted:",
     "if False:",
     T_RECENT),
    ("crf:C8 no thousands separator", CARDS,
     'f"{unlisted:,} more"',
     'f"{unlisted} more"',
     T_RECENT),
    ("crf:C9 one unlisted card is plural", CARDS,
     'noun = "card" if unlisted == 1 else "cards"',
     'noun = "cards"',
     T_RECENT),
    ("crf:R1 refused regular file is no result", CARDS,
     "return None, UNOPENABLE_RECEIPT if stat.S_ISREG(mode) else NOT_A_REGULAR_RECEIPT",
     'return None, "no result recorded" if stat.S_ISREG(mode) else NOT_A_REGULAR_RECEIPT',
     T_RECENT),
    ("crf:R2 refused stat claims the file is there", CARDS,
     '            return None, "the result path could not be looked at, so it was not read"',
     "            return None, UNOPENABLE_RECEIPT",
     T_RECENT),
    ("crf:R3 refused stat says no result", CARDS,
     '            return None, "the result path could not be looked at, so it was not read"',
     '            return None, "no result recorded"',
     T_RECENT),
]
