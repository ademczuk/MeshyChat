"""Third-method scan of a loader's cards.jsonl: the secret shapes a person types, which entropy cannot see.

    python third_scan.py <cards.jsonl>

It looks for a secret WORD followed by a value (short passwords, PINs and codes, command-line flags with a space,
tokens after an auth scheme, hex after `secret` or `token`, hook URLs, service-variable families) and for a few
classes of personal identifier. It never prints a value: each hit is shown as its class, the card's id prefix,
type and date, and a mask (length and character classes). Counts are per class.
"""
import json
import re
import sys
from collections import Counter, defaultdict

VAL = r"""["'`]?([^\s"'`,;)\]]{4,})"""
CLASSES = {
    "password word, then : or =": re.compile(
        r"(?i)\b(?:pass(?:word|wd|wort|phrase|code)?|pwd|pw|kennwort)s?\b[\"'`]?\s*[:=]\s*" + VAL),
    "password word, then is/was": re.compile(
        r"(?i)\b(?:pass(?:word|wd|wort|phrase|code)?|pwd|pw|kennwort)\b[^.\n:=]{0,30}?\b(?:is|ist|was|lautet)\s+" + VAL),
    "pin or one-time code": re.compile(
        r"(?i)\b(?:pin|otp|2fa|mfa|verification code|recovery code|backup code|device code|auth code|code)\b"
        r"\s*(?:is|ist|:|=)?\s*((?:\d[ -]?){4,10}\b|[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){1,5}\b)"),
    "command-line flag with a space": re.compile(
        r"(?i)(?:--(?:password|passwd|pass|token|secret|api-?key|key|pat|auth)|(?<=\s)-p)\s+" + VAL),
    "authorization scheme, then a value": re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[\"']?\w+\s+" + VAL),
    "secret word, then long hex": re.compile(
        r"(?i)\b(?:secret|token|key|credential|password)s?\b[^=:\n]{0,16}?[\s:=\"'`]([0-9a-f]{32,})\b"),
    "hook or bot url": re.compile(r"(?i)https?://[^\s\"'`]*(?:webhooks?/|hooks\.[^\s/]+/|/bot\d+:)([^\s\"'`]{8,})"),
    "service variable family": re.compile(r"\b(?:BRAIN_SVC_[A-Z0-9_]+|CF-Access-Client-(?:Id|Secret))\b[\"']?\s*[:=]?\s*" + VAL),
    "secret word, then : or =, any value": re.compile(
        r"(?i)(?<![A-Za-z])(?:[A-Za-z0-9]+[_.-])*(?:api[_-]?key|secret|token|credential|private[_-]?key|auth)"
        r"(?:[_.-][A-Za-z0-9]+)*[\"'`]?\s*[:=]\s*" + VAL),
    "personal: payment card shape": re.compile(r"(?<![\d.])((?:\d[ -]?){15}\d)(?![\d.])"),
    "personal: IBAN shape": re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?)\b"),
    "personal: phone shape": re.compile(r"(?<![\w.])(\+\d{1,3}[ -]?(?:\(?\d{1,4}\)?[ -]?){2,5}\d{2,4})(?![\w.])"),
    "personal: email address": re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"),
}
# Values that are plainly not secrets: placeholders, option names, words the operator uses about secrets.
BENIGN = re.compile(r"(?i)^(?:<.*|\$.*|\{.*|\*+|x+|none|null|true|false|yes|no|the|a|an|in|is|it|not|and|or|for|from|"
                    r"that|this|with|will|was|were|when|what|which|where|your|their|my|our|file|files|store|stored|"
                    r"rotation|rotated|prompt|required|needed|manager|field|reset|login|protected|based|only|auth|"
                    r"sign-?in|less|free)[.:!?]*$")


def strings(card):
    out = [card.get("text", ""), card.get("question", ""), card.get("answer", ""), card.get("notes", "")]
    for o in card.get("options", []) or []:
        out += [o.get("label", ""), o.get("description", "")]
    return [s for s in out if s]


def mask(value):
    kinds = "".join(k for k, p in (("a", r"[a-z]"), ("A", r"[A-Z]"), ("9", r"\d"), ("#", r"[^A-Za-z0-9]")) if re.search(p, value))
    return f"{len(value)} chars [{kinds}]"


cards = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
hits = defaultdict(list)
for card in cards:
    seen = set()
    for s in strings(card):
        for name, rx in CLASSES.items():
            for m in rx.finditer(s):
                value = m.group(1)
                if BENIGN.match(value) or (name, value) in seen:
                    continue
                seen.add((name, value))
                hits[name].append((card["id"][:8], card["type"], card["date"], mask(value)))
print("cards scanned:", len(cards), dict(Counter(c["type"] for c in cards)))
for name in CLASSES:
    found = hits.get(name, [])
    print(f"\n{name}: {len(found)} value(s) in {len({h[0] for h in found})} card(s)")
    for cid, typ, date, m in sorted(found, key=lambda h: h[2])[:12]:
        print(f"   {cid} {typ} {date} | {m}")
    if len(found) > 12:
        print(f"   ... and {len(found) - 12} more")
