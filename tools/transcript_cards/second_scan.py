"""Second-method scan of cards.jsonl for secret-like tokens the detectors did not flag.
Different method on purpose: token shape and character mix, no key names. Prints masked tokens only."""
import json
import math
import re
import sys

cards = [json.loads(x) for x in open(sys.argv[1], encoding="utf-8") if x.strip()]
TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{24,}")
HEX = re.compile(r"^[0-9a-fA-F-]+$")


def entropy(s):
    n = len(s)
    return -sum((s.count(ch) / n) * math.log2(s.count(ch) / n) for ch in set(s))


def strings(c):
    out = [c.get("text", ""), c.get("question", ""), c.get("answer", ""), c.get("notes", "")]
    for o in c.get("options", []) or []:
        out += [o.get("label", ""), o.get("description", "")]
    return out


found = {}
for c in cards:
    for s in strings(c):
        for m in TOKEN.finditer(s or ""):
            t = m.group(0)
            if HEX.match(t):
                continue                      # hashes, commit ids, uuids
            classes = sum(bool(re.search(p, t)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
            if classes < 3 or entropy(t) < 3.8:
                continue                      # words joined by - or _, paths, identifiers
            before = s[max(0, m.start() - 24): m.start()].replace("\n", " ")
            found.setdefault(t, (c["id"][:8], c["type"], c["date"], before))
print("cards scanned:", len(cards), "| distinct high-entropy mixed tokens not flagged:", len(found))
for t, (cid, typ, date, before) in sorted(found.items(), key=lambda kv: kv[1][2]):
    print("  %s %s %s | len %d | starts %r | preceded by %r" % (cid, typ, date, len(t), t[:4], before[-24:]))
