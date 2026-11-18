"""transcript_cards.py - turn a seat's Claude Code transcripts into local decision cards.

Version 1 scope (operator ruling 2026-10-08, thread <thread>):
  Ruling     one card per question the operator answered (question, options, chosen answer)
  Candidate  one card per prompt the operator typed (a candidate, not yet a ruling)

Everything else is dropped and counted: tool results, tool inputs, thinking, assistant text, harness
records, channel events, task notifications, launcher prompts, slash-command wrappers, compaction
summaries, meta records and sidechains. A card is made only from a FIRST-HAND record in the seat's own
transcript; a relayed quote in a channel event is never a card.

Plain code only: no model and no network. It opens nothing but *.jsonl transcripts at the top level of
the project directory it is given, and writes only inside --out.

Card fields the recall judge reads: type, seat, date (UTC day of the answer), status, text. `time` is the
record's full timestamp, kept so supersedes can be ordered across seats.

Identity: the card id is a hash of the record uuid and the block index, and of nothing else. The server keys
a card by it, so a changed card (a longer cut, a new prefix, a later loader) is a new version of the same key
and never a second card. Content travels as fields: `text_sha256` for the card text, `source.source_sha256`
for the kept source fields. Session ids are provenance, not identity, because a resumed or forked session
copies records (measured on one seat: 6,364 uuids in more than one file): a second sighting adds provenance
and no card. The same record and block with DIFFERENT content keeps the earlier one, by record time and then
by content hash, lists the other hash in `source.other_content`, and is counted as a conflict.

Redaction fails closed: a card whose kept fields trip any detector is dropped whole and listed in
drops.jsonl by id and detector name, never by text. Detectors match key names and token shapes, never
length alone.

The byte offset in cursor.json is only a read cursor. Deleting cursor.json and re-running must change
nothing in cards.jsonl.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

try:
    from . import kimi_dialect      # imported as a package module (the card client)
    from . import antigravity_dialect
    from . import context_snapshot
except ImportError as exc:
    if exc.name is not None:
        raise                       # a real failure inside the dialect, not a script context: do not mask it
    import kimi_dialect             # run as a script, or loaded by path: the loader's folder is on sys.path
    import antigravity_dialect
    import context_snapshot

LOADER_VERSION = "1.23"          # 1.23: a second Codex prompt in the same millisecond is a card of its own (#314); 1.22: an Antigravity step_index reused after a restart is a card of its own; 1.21: --machine-prefix drops a seat's own automation text; 1.20: antigravity dialect (JSON Lines brain transcripts); 1.19: attribution
MAX_CARD_TEXT = 1500
HEAD_BYTES = 4096


class PolicyUnreadable(ValueError):
    """cursor.json exists but is not a fail-closed exclusion policy."""


def exclusion_policy(cursor, cursor_path):
    """Validate and return excluded_files; both producers and the hook use this one fail-closed rule."""
    if not isinstance(cursor, dict):
        raise PolicyUnreadable(cursor_path)
    if "excluded_files" in cursor and not isinstance(cursor["excluded_files"], dict):
        raise PolicyUnreadable(cursor_path)
    if "exclude_base" in cursor and not isinstance(cursor["exclude_base"], str):
        raise PolicyUnreadable(cursor_path)
    excluded = cursor.get("excluded_files", {})
    for value in excluded.values():
        if value is True:
            continue
        if not isinstance(value, dict) or not value.get("resolved"):
            raise PolicyUnreadable(cursor_path)
        fields = ("realpaths", "sessions", "card_refs")
        if any(not isinstance(value.get(field), list) for field in fields):
            raise PolicyUnreadable(cursor_path)
        if not any(isinstance(item, str) and item for field in fields for item in value[field]):
            raise PolicyUnreadable(cursor_path)
    return excluded

# A value that looks like a secret: 12+ characters with at least one digit and one letter. MeshyChat message
# keys ("msg/...") are references, not secrets.
# The digit and the letter must show within its first 256 characters: unbounded, each "=" in a long run such as
# "token=token=..." re-scanned the run to its end (5.9 s on 64,000 characters).
_SECRET_VALUE = r"""["']?(?!msg/)(?=[^\s"',;]{0,256}\d)(?=[^\s"',;]{0,256}[A-Za-z])[^\s"',;]{12,}"""

# The rest of a secret's name after its keyword, possessive and with no length limit (a reviewer 75e4b6). A part that
# is itself a keyword followed by a separator or the "=" ends the walk, and the match starts again at that keyword,
# so a long run such as "key_key_key_..." costs one pass, not one per keyword. The same answer as the unbounded rule.
_KW = r"(?:api[_-]?key|key|secret|token|credential|private[_-]?key|signing[_-]?key|access[_-]?key)"
_SUFFIX = rf"(?:[_.-](?!{_KW}(?:[_.-]|[\"']?\s*[=:]))[A-Za-z0-9]++)*+"

DETECTORS = [
    ("pem_block", re.compile("-----BEGIN " + r"[A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE|KEY BLOCK)-----")),
    # its payload may be the empty object, "e30" (base64url of {}), as in test fixtures and some service tokens
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.(?:[A-Za-z0-9_-]{8,}|e30)\.[A-Za-z0-9_-]{4,}")),
    ("bearer_header", re.compile(r"(?i)\b(?:bearer|basic)\s+(?=[A-Za-z0-9._~+/=-]*[0-9=])[A-Za-z0-9._~+/=-]{20,}")),
    # The scheme is bounded: unbounded, a long lower-case run made every start position scan the run to its end
    # looking for "://" (9.6 s on 64,000 characters, kxxx). A longer scheme still matches from a later start.
    ("url_credentials", re.compile(r"[a-z][a-z0-9+.-]{0,30}://[^\s/:@]+:[^\s/:@]+@")),
    ("vendor_key", re.compile(
        r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|xox[abposr]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xai-[A-Za-z0-9]{20,}"
        r"|hf_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{16,}|lin_api_[A-Za-z0-9]{20,}|npm_[A-Za-z0-9]{30,}"
        r"|pypi-[A-Za-z0-9_-]{30,}"
        r"|[a-z]{2,8}_(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{24,}(?![A-Za-z0-9_]))")),
    # No name prefix before the keyword, and a bounded, possessive suffix after it (kxxx: 52.9 s on 64,000
    # characters; "key_" repeated took 35 s on 4,000). The prefix never decided a match: a keyword after a separator
    # already passes the lookbehind on its own. Giving back a suffix part cannot reach "=" or ":".
    ("named_secret", re.compile(
        r"""(?i)(?<![A-Za-z])(?:api[_-]?key|key|secret|token|credential"""
        r"""|private[_-]?key|signing[_-]?key|access[_-]?key)""" + _SUFFIX + r"""["']?\s*[=:]\s*""" + _SECRET_VALUE)),
    ("pat_assignment", re.compile(
        r"""(?<![A-Za-z])PAT(?:_(?!PAT(?:_|[\"']?\s*[=:]))[A-Z0-9]++)*+["']?\s*[=:]\s*""" + _SECRET_VALUE)),
]


class _OpaqueToken:
    """A standalone token of 32+ characters that mixes lower case, upper case and digits, holds an unbroken
    alphanumeric run of 20+, and has high character entropy. This is the net under the named detectors: on the
    first real run it was a scan of this shape that found two secrets the name and prefix rules had missed.
    Hashes, uuids and commit ids (hex) are not flagged. Neither is a path or URL segment: a token directly
    after a slash, or directly before a slash or a file extension, is a reference, and a secret inside a URL
    query is the named detector's job (key=, token=)."""
    _token = re.compile(r"[A-Za-z0-9+=_-]{32,}")
    _hex = re.compile(r"^[0-9a-fA-F-]+$")
    _run = re.compile(r"[A-Za-z0-9]{20,}")
    _after = re.compile(r"[\\/]|\.[A-Za-z0-9]{1,5}(?![A-Za-z0-9])")
    _chunk_before = re.compile(r"\S*$")

    @staticmethod
    def _random_like(t):
        """True for a generated code, false for joined words. Character entropy alone cannot tell them apart
        (measured: a 40-letter CamelCase name scores 4.24 bits, the median random base32 code 4.20), so the
        test is structural: either the letters are roughly half upper case, as in a base62 or base64 code and
        unlike CamelCase (about one capital in six), or digits and letters alternate often, as in a base32 or
        lower-case-and-digit code and unlike a name with a version number in it."""
        letters = [c for c in t if c.isalpha()]
        upper_ratio = (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0.0
        alnum = [c for c in t if c.isalnum()]
        flips = sum(1 for a, b in zip(alnum, alnum[1:]) if a.isdigit() != b.isdigit())
        n = len(t)
        entropy = -sum((t.count(ch) / n) * math.log2(t.count(ch) / n) for ch in set(t))
        return entropy >= 3.6 and ((len(letters) >= 16 and 0.25 <= upper_ratio <= 0.75) or flips >= 6)

    _split = re.compile(r"[-_+=]+")

    @classmethod
    def _mixed_segments(cls, t):
        """A base64url code is broken by - and _ and need not hold a run of 20 (pxx's miss, 8 Oct). It differs
        from a hyphenated name in that most of its pieces mix digits with letters, or cases inside a piece."""
        segs = [s for s in cls._split.split(t) if s]
        mixed = sum(1 for s in segs if (re.search(r"[0-9]", s) and re.search(r"[A-Za-z]", s)) or re.search(r"[a-z][A-Z]", s))
        return len(segs) >= 2 and mixed * 2 >= len(segs)

    def search(self, text):
        for m in self._token.finditer(text):
            t = m.group(0)
            if self._hex.match(t) or not (self._run.search(t) or self._mixed_segments(t)):
                continue
            if not self._random_like(self._split.sub("", t)):
                continue
            if m.start() > 0 and text[m.start() - 1] in "/\\":
                continue
            if self._after.match(text, m.end()):
                continue
            if "://" in self._chunk_before.search(text[: m.start()]).group(0):
                continue
            return m
        return None


DETECTORS.append(("opaque_token", _OpaqueToken()))

# A code named by its context, whatever its shape: the words "pairing code" or "minted you a meshychat" followed
# within 80 characters by a token of 16+ that holds a digit or a lower-to-upper step (dxxxxx's miss, 8 Oct).
DETECTORS.append(("pairing_code", re.compile(
    r"(?i:pairing[\s_-]*code|minted\s+you\s+a\s+meshychat)[^\n]{0,80}?"
    r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*(?:[0-9]|[a-z][A-Z]))[A-Za-z0-9_-]{16,}")))

# Hardware identifiers. Standing, for every seat: a MAC-shaped value anywhere, and a 64-hex string within 200
# characters of a word that names a hardware object. Plain "mac" and "serial" are NOT in the standing list: on
# one seat they are "your Mac" and the serial console (pxx's review). A seat with its own classes adds them in
# SEAT_DETECTORS, keyed by --seat, instead of editing the shared list.
DETECTORS.append(("mac_address", re.compile(
    r"(?<![0-9A-Fa-f:.-])[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}(?![0-9A-Fa-f:-])")))

_HARDWARE_WORDS = (r"dtb|device[ -]?tree|calibration|firmware|nvram|ecid|udid|serial[ -]number|mac[ -]address"
                   r"|chip[ -]id|board[ -]id|wi-?fi|wlan|bluetooth")


class _HexNear:
    """A hex string of at least min_len within 200 characters of one of the words. With mixed=True the hex must
    hold both a letter and a digit, so a plain number or a word is not taken for an identifier."""

    def __init__(self, min_len, words, mixed=False):
        self._hex = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{%d,}(?![0-9A-Fa-f])" % min_len)
        self._near = re.compile(r"(?i)\b(?:" + words + r")\b")
        self._mixed = mixed

    def search(self, text):
        for m in self._hex.finditer(text):
            t = m.group(0)
            if self._mixed and not (re.search(r"[0-9]", t) and re.search(r"[A-Fa-f]", t)):
                continue
            if self._near.search(text[max(0, m.start() - 200): m.end() + 200]):
                return m
        return None


DETECTORS.append(("hardware_hash", _HexNear(64, _HARDWARE_WORDS)))


# Per-seat detectors, applied in addition to DETECTORS when --seat names the seat.
SEAT_DETECTORS = {
    # pxx (review of 8 Oct): short hashes of the device's own objects, 8+ hex with a letter and a digit.
    # An upper-case UUID is a volume identifier there; lower-case ones are session ids and stay.
    "pxx": [("seat_hardware_short_hex", _HexNear(8, _HARDWARE_WORDS, mixed=True)),
            ("seat_uppercase_uuid", re.compile(
                r"(?<![0-9A-Fa-f-])(?=[0-9A-F-]{36}(?![0-9A-Fa-f-]))(?=[0-9A-F-]*[A-F])"
                r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}"))],
    # dxxxxx (review of 8 Oct): the wider word list it asked for, with 64 hex.
    # dxxxxx (10 Oct): and short hashes, 8+ hex with a letter and a digit, near those words or u-boot. Measured on
    # its 1,464 cards: 8 of the 9 it had held back by hand drop (the 9th holds no identifier), and no other card.
    "dxxxxx": [("seat_hardware_wide_words", _HexNear(64, _HARDWARE_WORDS + r"|mac|serial")),
               ("seat_hardware_short_hex", _HexNear(8, _HARDWARE_WORDS + r"|mac|serial|u-?boot", mixed=True))],
}

# Shapes a person types, which the generated-token rules above pass: 11 detectors written by memory-bxxxxx from
# a red-team of version 1 (attack_redaction.py, where 28 of 30 planted typed shapes passed). Taken as written.
# A password's name. Bare `pass` is not one here: `nextest_pass=true` and `tests pass: 163` are test results, and a
# real card of that shape was dropped by the first draft of this file. `pass` has its own, narrower rule below.
_PW = r"(?i:pass(?:word|wd|wort|phrase|code)|pwd|pw|kennwort)"
# A value that is not a placeholder (<x>, $X, {x}, ***) or a word that states a setting rather than a secret.
# Up to the next space, quote, comma or semicolon.
_ANY = (r"""(?![<$\{*])(?!(?i:true|false|yes|no|none|null|required|optional|unknown|unset|empty|n/a)(?![A-Za-z0-9]))"""
        r"""[^\s"'`,;]{4,}""")
# A value that does not look like a plain word: it holds a digit, a lower-then-upper step (hunterTwo), or a symbol
# with something after it (Sommer!Regen). Case-sensitive on purpose: REQUIRED and Winter are plain words here.
_ODD = (r"""(?![<$\{*])(?=[^\s"'`,;]*(?:\d|[a-z][A-Z]|[!@#$%^&*+=~|][^\s"'`,;]))"""
        r"""[^\s"'`,;]{4,}""")

_TYPED_SHAPES = [
    # password: hunter2   DB_PASSWORD=s3cret   Passwort: ...   pw: ...   Password=...;   passphrase: four plain words
    # password: "Winter 2026 x9"
    ("password_assignment", re.compile(
        r"(?<![A-Za-z])(?:[A-Za-z0-9]+[_.-])*" + _PW + r"(?i:s)?(?:[_.-][A-Za-z0-9]+)*[\"'`]?\s*[:=]\s*[\"'`]?" + _ANY)),
    # pass=hunter22   (the bare name: only with a value of 6+ that holds a letter and a digit)
    ("pass_assignment", re.compile(
        r"""(?<![A-Za-z0-9_.-])(?i:pass)["'`]?\s*[:=]\s*["'`]?(?=[^\s"'`,;]*\d)(?=[^\s"'`,;]*[A-Za-z])[^\s"'`,;]{6,}""")),
    # the password for the box is Winter2026x9   password for the box: Winter2026x9
    ("password_phrase_wide", re.compile(
        r"\b" + _PW + r"\b[^.\n:=]{0,30}?(?:\b(?i:is|ist|was|lautet)\s+|[:=]\s*)[\"'`]?" + _ODD)),
    # --password Winter2026x9   --token <40 hex>   sshpass -p ...   mysql -u root -p...
    ("cli_secret_flag", re.compile(
        r"(?i)(?:--(?:password|passwd|pass|token|secret|api-?key|pat|auth-?token)\s+[\"'`]?"
        r"(?![<$\{*])(?=[^\s\"'`]*\d|[^\s\"'`]{16,})[^\s\"'`]{6,}"
        r"|\b(?:sshpass|mysql|mysqldump|mariadb)\b[^\n]{0,40}?\s-p\s*[\"'`]?(?![<$\{*])[^\s\"'`]{4,})")),
    # Authorization: Token <hex>   Authorization: Basic <short base64>
    ("auth_scheme_value", re.compile(
        r"(?i)\bauthorization\b[\"']?\s*[:=]\s*[\"']?[A-Za-z]+\s+(?![<$\{*])[A-Za-z0-9._~+/=-]{8,}")),
    # client secret <64 hex>   private key <64 hex>   token <40 hex>   (no separator; bare `key` is left out on
    # purpose, because `key id` and `key fingerprint` introduce public hex)
    ("secret_word_hex", re.compile(
        r"(?i)\b(?:secret|token|credential|private[ _-]?key|api[ _-]?key|password)s?\b[^=:\n]{0,16}?[\s:=\"'`]"
        r"[0-9a-f]{32,}\b")),
    # webhook and bot URLs carry their credential in the path, where the opaque-token rule deliberately does not look
    ("hook_url", re.compile(
        # the walk to the marker stops at the next scheme or marker, so "http://" repeated is one pass (it was 8.9 s on
        # 64,000); the hostname before /services/ is at most 253 characters, DNS's own limit, so "hooks." repeated is too
        r"(?i)https?://(?:(?!https?://|/webhooks?/|hooks\.[a-z0-9.-]{1,253}+/services/|/bot\d+:)[^\s\"'`])*+(?:/webhooks?/|hooks\.[a-z0-9.-]{1,253}+/services/|/bot\d+:)[^\s\"'`]{8,}+")),
    # a refresh token of the 1//... shape starts right after a slash, which the opaque-token rule skips
    ("slash_prefixed_token", re.compile(r"(?<![A-Za-z0-9])\d//[A-Za-z0-9_-]{30,}")),
    # this fleet's service-credential variables, by name: any value, whatever its shape
    ("service_variable", re.compile(
        r"\b(?:BRAIN_SVC_[A-Z0-9_]+|CF-Access-Client-(?:Id|Secret))\b[\"']?\s*[:=]?\s*[\"']?(?![<$\{*])[^\s\"'`,;]{6,}")),
    # PIN: 482913   2FA code 482913   recovery code: abcd-efgh-1234   (a bare `code` is left out: exit and close codes)
    ("otp_or_pin", re.compile(
        r"(?i)\b(?:pin|otp|2fa|mfa|(?:verification|recovery|backup|device|auth(?:entication)?|security|one-time)\s+code)\b"
        r"(?:\s+code)?\s*(?:is|ist|:|=)?\s*(?:(?:\d[ -]?){3,9}\d\b|[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){1,5}\b)")),
    # auth=<12+ mixed>   (the name version 1's list lacks)
    ("auth_assignment", re.compile(
        r"""(?i)(?<![A-Za-z])auth(?:[_-]?token)?["']?\s*[:=]\s*["']?(?=[^\s"',;]*\d)(?=[^\s"',;]*[A-Za-z])[^\s"',;]{12,}""")),
]

DETECTORS.extend(_TYPED_SHAPES)

# Follow-up by memory-bxxxxx after pxx ran version 1.1 on a long session. As first written, the two password
# rules let the white space after the separator run over a line break, so sudo's own "Password:" prompt took
# the first word of the next output line as its value (66 drops on one seat, none a password). The value must
# now be on the same line; two narrower rules cover the forms that puts out of reach, and two more cover the
# misses that run found: an offer link on an address-and-port host, and a quoted code with no run of 20.
# For the line-anchored rules below, where the name starts at the line's first character: the prefix walk stops before
# a password word that ends a part, possessive, so "password_" repeated on one line is one pass (it was quadratic).
_PWSUF = rf"(?:[_.-](?!{_PW}(?i:s)?(?:[_.-]|[\"'`]?[ \t]*[:=]))[A-Za-z0-9]++)*+"
_NAME = rf"(?:(?!{_PW}(?i:s)?(?:[_.-]|[\"'`]?[ \t]*[:=]))[A-Za-z0-9]++[_.-])*+{_PW}(?i:s)?(?:[_.-][A-Za-z0-9]++)*+"


class _QuotedCode:
    """A token of 24+ characters from [A-Za-z0-9_-], alone between quotes or backticks, that looks generated:
    not hex, and either its letters are roughly half upper case or digits and letters alternate often. Thread
    ids and slugs (lower case words with hyphens and a date) and CamelCase names fail both tests."""
    _quoted = re.compile(r"""(["'`])([A-Za-z0-9_-]{24,})\1""")
    _hex = re.compile(r"^[0-9a-fA-F-]+$")

    def search(self, text):
        for m in self._quoted.finditer(text):
            t = m.group(2)
            if self._hex.match(t):
                continue
            letters = [c for c in t if c.isalpha()]
            if len(letters) < 12:
                continue
            upper = sum(1 for c in letters if c.isupper()) / len(letters)
            alnum = [c for c in t if c.isalnum()]
            flips = sum(1 for a, b in zip(alnum, alnum[1:]) if a.isdigit() != b.isdigit())
            n = len(t)
            entropy = -sum((t.count(ch) / n) * math.log2(t.count(ch) / n) for ch in set(t))
            if entropy >= 3.6 and (0.25 <= upper <= 0.75 or flips >= 6):
                return m
        return None


_FOLLOWUP_REPLACE = {
    # Unanchored, so no prefix: as in named_secret, a password word after a separator passes the lookbehind itself
    # (kxxx: 21.6 s on 64,000 characters; "password_" repeated took 6.1 s on 4,000).
    "password_assignment": re.compile(
        r"(?<![A-Za-z])" + _PW + r"(?i:s)?" + _PWSUF + r"[\"'`]?[ \t]*[:=][ \t]*[\"'`]?" + _ANY),
    # The same fault in the wide rule: after `:` or `is` its white space ran over the line break, so a prompt
    # followed by a line that starts with a timestamp or a count was a "password".
    "password_phrase_wide": re.compile(
        r"\b" + _PW + r"\b[^.\n:=]{0,30}?(?:\b(?i:is|ist|was|lautet)[ \t]+|[:=][ \t]*)[\"'`]?" + _ODD),
}
_FOLLOWUP_ADD = [
    ("password_block_value", re.compile(
        r"(?m)^([ \t]*)(?:-[ \t]+)?[\"'`]?" + _NAME + r"[\"'`]?[ \t]*:[ \t]*(?:[|>][-+]?[ \t]*)?\r?\n\1[ \t]{2,}[\"'`]?" + _ANY)),
    # Username:\nvexor\nPassword:\nWinter2026x9   a pasted form with each value on its own line. Only when the next
    # line is ONE token that does not look like a plain word, so "Sorry, try again." and a log line stay out.
    ("password_next_line", re.compile(
        r"(?m)^[ \t]*[\"'`]?" + _NAME + r"[\"'`]?[ \t]*:[ \t]*\r?\n[ \t]*[\"'`]?" + _ODD + r"[\"'`]?[ \t]*$")),
    ("lan_capability_url", re.compile(
        r"https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d{2,5})?/(?:[^\s/\"'`]+/)*"
        r"(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_.-])")),
    ("quoted_code", _QuotedCode()),
    # aws_secret_access_key, awsSecretAccessKey, "AWS Secret Access Key": the label in any spelling, then the
    # 40-character value. named_secret wants a separator before "access key", so the camelCase spelling passed it.
    ("aws_secret_label", re.compile(
        r"(?i)aws[-_ ]?secret[-_ ]?access[-_ ]?key[\"'\s:=]+[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
]

DETECTORS[:] = [(_n, _FOLLOWUP_REPLACE.get(_n, _rx)) for _n, _rx in DETECTORS] + _FOLLOWUP_ADD

# File names this loader refuses to open even if handed one directly.
_FORBIDDEN_NAME = re.compile(r"(?i)(state|credential|secret|token|keys?\.|\.env|\.pem|id_rsa|id_ed25519)")

_NOISE_PREFIXES = (
    ("<channel", "channel_event"),
    ("<system-reminder", "system_reminder"),
    ("<local-command", "local_command"),
    ("<command-name", "local_command"),
    ("<command-message", "local_command"),
    ("<command-args", "local_command"),
    ("<bash-input", "local_command"),
    ("<bash-stdout", "local_command"),
    ("<bash-stderr", "local_command"),
    ("Caveat:", "local_command"),
    ("<task-notification", "task_notification"),
    ("<user-prompt-submit-hook", "hook_text"),
    ("This session is being continued", "continuation_summary"),
    ("MANDATORY session-start", "launcher_prompt"),
    ("[Request interrupted", "interrupt_marker"),
    # Text another process put into the session, recorded as if it were a prompt. A message from another seat
    # arrives in an older transcript with no origin and no prompt source at all: on one seat every one of the 51
    # unlabelled "prompts" was such a message, a peer's words on a card that says the operator typed them.
    ("Another Claude session sent a message", "peer_message"),
    ("[mesh-wake-daemon", "wake_injection"),
    ("[mesh-watch:", "wake_injection"),
    ("[SYSTEM NOTIFICATION", "harness_text"),
    ("Mesh monitor keepalive", "harness_text"),
    ("[Your previous response had no visible output", "harness_text"),
)
_OPERATOR_SOURCES = ("typed", "queued", "suggestion_accepted")

# The wake daemon (retired 10 Sept 2026) TYPED its line into the seat's own terminal, so the record says a human
# typed it, and the line quotes a peer's message. It could land in the middle of what the operator was typing,
# which is why this is searched for anywhere in the text and the whole prompt goes (19 on one seat).
_WAKE_INJECTION = re.compile(r"mesh-wake: new [A-Za-z]+ mesh message for you|\[mesh-watch:[\w-]+\]|\[mesh-wake-daemon")
# The fleet launcher types its session-start prompt the same way, and it too can land in a line the operator
# had begun (memory-bxxxxx's seat: 19 characters of his, then 1,100 of the launcher's, in one typed record).
# Whatever a machine typed runs to the end of the line, because the machine also pressed Enter. So the text is
# cut from the machine's first word to the end, the operator's words before it are kept, and a mark is left.
_MACHINE_TYPED = re.compile(_WAKE_INJECTION.pattern + r"|MANDATORY session-start action[:,]")
# A Codex seat has no channel: the postbox TYPES each delivery into its pane, and the launcher types its
# orientation the same way, so the client records them as typed by a person. Measured on one seat: of 547 typed
# records, 148 were deliveries, 156 tagged channel lines, 45 the launcher's orientation and 15 handoffs of the
# older wake daemon. A Candidate from one would carry a peer's words as the operator's. The shapes are the
# postbox's own templates, every version of them (tools/postbox: "MeshyChat message for you (<identity>)", later
# with a tag and the sender in front, where the sender is a label, a label with "(peer <id>)", or a bare peer id;
# the batch instructions and the handling policy that follow a batch; the paced postbox's batch header,
# "<n> MeshyChat deliveries for <identity>, handled together in this one turn.", and its same-commands line).
# Read for Codex transcripts only: a Claude seat has a channel, and its operator can quote a tag in a sentence of
# his own.
#
# A list of templates is never complete: the postbox of another machine wrote a batch header this list did not
# know, in FRONT of the delivery, and the cut kept that header as the operator's words (2 of the 8 Candidates of
# that seat, codex-oxxxxxx-cli on 1.16). On the two Codex seats measured, no delivery landed in a line the
# operator had begun: every time something stood in front of one, the postbox had written it. So a record that
# holds a delivery ANYWHERE is dropped whole (_NEVER_CUT). The price is a line the operator had begun when a
# delivery landed in it; the other machine-typed kinds are still cut, his words before them kept.
_CODEX_MACHINE_TYPED = re.compile(
    _MACHINE_TYPED.pattern
    + r"|MeshyChat message for you \(|MeshyChat postbox for "
    + r"|\[(?:STEERING|MESSAGE|RECALL)\] from \S"
    + r"|Process this banked delivery in one turn\.|Local postbox handling policy \(not peer message content\)"
    + r"|\d+ MeshyChat deliveries for |Each further message below is read and answered with the two commands "
    + r"|Session-start orientation, read |nimbalyst://system-instructions"
    + r"|\[LOCAL-WAKE-FIXTURE |LOCAL_RESTART_RECEIPT_TEST |<realtime_delegation>"
    + r"|\[AGENTMESHY-NATIVE-WAKE ")
_CODEX_IMAGE_WRAP = re.compile(r"<image\b[^>]*>|</image>")


_NEVER_CUT = ("postbox_delivery", "channel_line_typed")

# The Kimi pane's machine text is the Codex list (the postbox types its deliveries into a Kimi pane the same
# way, and _NEVER_CUT drops them whole via _machine_class) plus the Kimi harness's own envelopes
# (background-task and mesh notifications, cron deliveries, reminder blocks), which are cut.
_KIMI_MACHINE_TYPED = re.compile(_CODEX_MACHINE_TYPED.pattern + r"|<notification\b|<cron-fire\b|<system-reminder>")


def _machine_class(found):
    """The drop class of a prompt that is machine-typed from its first word, by the words the machine opened with."""
    if found.startswith(("MANDATORY", "Session-start orientation")):
        return "launcher_prompt"
    if "MeshyChat" in found or found.startswith(("Process this banked delivery", "Local postbox handling policy",
                                                 "Each further message below")):
        return "postbox_delivery"
    if found.startswith(("[STEERING]", "[MESSAGE]", "[RECALL]")):
        return "channel_line_typed"
    if found.startswith(("nimbalyst://", "[LOCAL-WAKE-FIXTURE", "LOCAL_RESTART_RECEIPT_TEST", "<realtime_delegation>")):
        return "harness_text"
    if found.startswith(("<notification", "<cron-fire", "<system-reminder")):
        return "kimi_machine_envelope"
    return "wake_injection"
# A prompt that opens with a seat's name as a header ("From uxxx: ...") is that seat's message put into this pane,
# by a hand or by a seat: a relay either way, and a relay is never a card. Seen on two seats on one day (8 records
# on one, the same author's message on the other). One lower-case label, then the colon, with at most a bracket
# ("From uxxx (standing order): ...") or a clause after a comma ("From uxxx, with an apology: ...") between
# them. The label must be a KNOWN SEAT: "From memory: we agreed to hold the deploy ..." is the operator's own
# sentence and the kind a ruling starts with (pxx's finding on 1.9, which took any lower-case word for a seat).
_PEER_HEADER = re.compile(r"(?:From|Reply from|Message from|Relayed from) ([a-z][a-z0-9_.-]{1,30})"
                          r"(?: \([^)\n]{0,80}\)|,[^\n:]{0,80})?:\s")
# The labels this fleet's seats have used. A seat with another roster adds its own with --seats.
KNOWN_SEATS = frozenset(
    "bxxxxx antigravity dxxxxx exxxx exxxx-session codex-bxxxxx-cli codex-kxxx codex-kxxx-cli "
    "codex-oxxxxxx-admin-cli codex-oxxxxxx-cli hxxxxx "
    "kxxx jxxx nxxxxx memory-bxxxxx oxxxxxx pxx sxxxxxxxx qxxxxx txxxx uxxx vxxxxxxxxxx".split())
# A seat's report pasted by hand opens with the seat's label and a colon, with no "From" before it ("kxxx: Sent and
# read-back verified ...", "VxxxxxXxxxx: Status report ...", "Oxxxxxx: ..."). 49 such prompts were read on four seats
# on 8 Oct 2026: 48 were a seat's own report, and none was the operator's line to or about another seat. The 49th
# opened with the reading seat's own launcher name and was an instruction to it, so a seat's own names never count
# (own_names). Only a ROSTER label counts: "Urgent: the arena is blank ...", "important: ..." and "update: ..." are
# the operator's own lines, seen on two seats. The label is written in any case, a bracket may follow it, and the
# colon needs no space after it ("Vxxxxxxxxxx:Your stop was correct ...").
_LABEL_OPENER = re.compile(r"([A-Za-z][A-Za-z0-9_.-]{1,30})(?: ?\([^)\n]{0,80}\))? ?:")
# A seat whose launcher name differs from its roster label is addressed by either: bxxxxx is memory-bxxxxx's
# launcher name and exxxx is exxxx-session's. A hyphen part of a label proves nothing: codex-bxxxxx-cli is a
# different seat from bxxxxx, and codex-oxxxxxx-cli from oxxxxxx (memory-bxxxxx and pxx, on the first draft of this
# rule, which took every hyphen part and so kept those seats' reports on the two codex seats).
SEAT_LAUNCHER_NAMES = {"memory-bxxxxx": ("bxxxxx",), "exxxx-session": ("exxxx",)}
_MARKER_NAME = re.compile(r"\becho\s+([a-z][a-z0-9_.-]{1,30})-shell-ok\b", re.IGNORECASE)


def own_names(seat, extra=(), markers=()):
    """The labels that mean the reading seat itself: its roster label, its launcher name where that differs (built
    in for the two seats above, or read from a --marker of the form "echo <name>-shell-ok"), and the names given
    with --own-name. A prompt that opens with one of them is addressed to this seat, so it is not another seat's
    report."""
    seat = seat.strip().lower() if isinstance(seat, str) else ""
    names = {seat} | set(SEAT_LAUNCHER_NAMES.get(seat, ()))
    names |= {e.strip().lower() for e in extra if isinstance(e, str) and e.strip()}
    names |= {m.group(1) for m in (_MARKER_NAME.search(t) for t in markers if isinstance(t, str)) if m}
    return frozenset(names)



_MACHINE_MARK = "[machine-typed text removed]"
# A block the operator pasted from somewhere else is not his words. Its edges are marked, so the block is cut out
# and what he typed around it is kept; a prompt that is nothing but the block is dropped.
_PASTED_BLOCK = re.compile(r"<pasted_content\b[^>]*>.*?(?:</pasted_content\b[^>]*>|\Z)", re.S)
_PASTED_MARK = "[pasted content removed]"
_IMAGE_ONLY = re.compile(r"(?:\[Image #\d+\]\s*)+")


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8", "strict")).hexdigest()


def detect(text, disabled=(), seat=None):
    """Names of the detectors that fire on text: the standing ones, plus the seat's own when a seat is named."""
    return [name for name, rx in DETECTORS + SEAT_DETECTORS.get(seat, []) if name not in disabled and rx.search(text)]


# Terminal output pasted back as evidence is not a decision (pxx's review: 85 of 313 answers on one seat were
# free text, most of them pasted terminal sessions, and that is where the machine's identifiers sit). A carriage
# return is what a paste through the answer box leaves; the rest are a terminal's own lines.
_PASTED_OUTPUT = re.compile(
    r"\r|\x1b\[|^Last login: |; exit;|^\[sudo\] password for |^Password:[ \t]*$"
    r"|^[\w.-]+@[\w.-]+[ :][^\n]{0,80}[%$#] "
    r"|^PS [A-Za-z]:\\[^\n>]{0,120}> |^[A-Za-z]:\\[^\n>]{0,120}>", re.M)

# Slash commands whose ARGUMENTS are the operator's own words. /goal is his standing goal for the session.
# Other commands' arguments (/model, /effort, /plugin) are settings and stay dropped.
_SLASH_WITH_OPERATOR_TEXT = ("/goal",)
_CMD_NAME = re.compile(r"<command-name>\s*(/?[\w:-]+)\s*</command-name>")
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_BARE_SLASH = re.compile(r"/[\w:-]+")


def _label_only(answer, options, multi_select):
    """True when the answer is one offered label, or for a multi-select only offered labels and separators."""
    labels = [o.get("label") for o in options if o.get("label")]
    if answer in labels:
        return True
    if not multi_select:
        return False
    rest = answer
    for lab in sorted(labels, key=len, reverse=True):
        rest = rest.replace(lab, "")
    return not rest.strip(" ,;\n\r\t")


def prefilled_question_ids(o):
    """Ids of question tool calls in an assistant record whose own input already carries `answers`. Such answers
    were written by the model, not chosen by the operator, and must never become a Ruling."""
    msg = o.get("message") if isinstance(o, dict) and isinstance(o.get("message"), dict) else {}
    content = msg.get("content") if isinstance(msg.get("content"), list) else []
    return [b.get("id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion"
            and isinstance(b.get("input"), dict) and b["input"].get("answers") and b.get("id")]


def _typed_text(content):
    """(text, has_attachment) from a user record's content, ignoring injected reminder blocks."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    parts, attach = [], False
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            t = b.get("text") or ""
            if not t.lstrip().startswith("<system-reminder"):
                parts.append(t)
        elif b.get("type") != "tool_result":
            attach = True
    return "\n".join(parts), attach


def _clip(s, n):
    s = s.strip()
    return s if len(s) <= n else s[: max(0, n - 3)].rstrip() + "..."


def _no_lone_surrogates(s):
    """A lone surrogate is corrupt text: replace each with U+FFFD so the card still encodes. Python strings are
    code points, so a valid astral character is never in this range - only an unpaired half is. A transcript
    carries one through a JSON \\uD800 escape; the strict UTF-8 encode at _card_bytes then kills the whole run
    (gap audit 2026-10-11, lane 1a)."""
    if not isinstance(s, str) or not any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return s
    return "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in s)


def _ruling_text(seat, date, question, answer, options, notes):
    head = "Ruling | owner: operator | seat: %s | %s" % (seat, (date or "")[:16])
    parts = [head, "Q: " + _clip(question, 650), "A: " + _clip(answer, 380)]
    chosen = [o for o in options if o.get("label") and o.get("label") == answer and o.get("description")]
    if chosen:
        parts.append("Chosen means: " + _clip(chosen[0]["description"], 260))
    if notes:
        parts.append("Operator note: " + _clip(notes, 160))
    labels = [o.get("label") for o in options if o.get("label")]
    if labels:
        parts.append("Offered: " + "; ".join(labels))
    text = "\n".join(parts)
    return _clip(text, MAX_CARD_TEXT), len(text) > MAX_CARD_TEXT


def _candidate_text(seat, date, text):
    head = "Candidate | owner: operator | seat: %s | %s" % (seat, (date or "")[:16])
    room = MAX_CARD_TEXT - len(head) - 1
    body = _no_lone_surrogates(text).strip()
    return head + "\n" + _clip(body, room), len(body) > room


def _options_of(options):
    return [{"label": _no_lone_surrogates(x.get("label") or ""),
             "description": _no_lone_surrogates(x.get("description") or "")}
            for x in (options or []) if isinstance(x, dict)]


def _ruling_raw(seat, date, session, uuid, call_id, index, question, options, answer, note, multi_select,
                kind="answered_question"):
    """One answered question as a raw Ruling: (raw, None), or (None, the class it is dropped under). The transcript
    walk and answered_question_card both come through here, so the rules for an answer exist once."""
    nothing_chosen = answer in ("", [], {}, None) or (isinstance(answer, str) and not answer.strip())
    answer = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False, sort_keys=True)
    seat = _no_lone_surrogates(seat) if isinstance(seat, str) else ""
    kind = _no_lone_surrogates(kind) if isinstance(kind, str) else "answered_question"
    uuid = _no_lone_surrogates(uuid)
    call_id = _no_lone_surrogates(call_id) if call_id else None
    session = _no_lone_surrogates(session)
    question = _no_lone_surrogates(question)
    answer = _no_lone_surrogates(answer)
    note = _no_lone_surrogates(note)
    options = _options_of(options)
    if nothing_chosen and not note.strip():
        return None, "answer_empty"                            # submitted with nothing chosen: no decision to record
    free_text = not _label_only(answer, options, multi_select)
    if free_text and _WAKE_INJECTION.search(answer):
        return None, "wake_injection"
    if free_text and _PASTED_OUTPUT.search(answer):
        return None, "pasted_output"
    kept = {"kind": kind, "question": question, "options": options, "answer": answer,
            "notes": note, "multi_select": multi_select}
    text, truncated = _ruling_text(seat, date, question, answer, options, note)
    return {"type": "Ruling", "block_index": index, "kept": kept, "text": text, "truncated": truncated,
            "date": date, "session": session, "uuid": uuid, "call_id": call_id,
            "extra": {"free_text": free_text, "long_free_text": free_text and len(answer) > 300}}, None


def cards_from_record(o, seat, prefilled=(), seats=KNOWN_SEATS, own=None, machine_typed=_MACHINE_TYPED):
    """Classify one transcript record. Returns (list of raw cards, drop class, list of drop classes, or None).
    `prefilled` holds the ids of question calls whose input already carried answers (see prefilled_question_ids).
    `seats` are the labels a relay header or a pasted report may name; `own` are the labels that mean this seat
    itself (own_names), worked out from `seat` when not given. `machine_typed` finds text a machine typed into the
    pane (codex_cards passes the Codex list)."""
    if not isinstance(o, dict):
        return [], "not_a_record"
    uuid = _no_lone_surrogates(o.get("uuid"))
    if not uuid:
        return [], "harness_record"
    if o.get("isSidechain"):
        return [], "sidechain"
    if o.get("type") == "attachment":
        # What the operator types WHILE A TURN IS RUNNING is not a user record. It is an attachment of type
        # queued_command with his words in attachment.prompt (exxxx-session's finding on 1.8: 172 such messages with
        # no card on one seat, 162 on another, rulings among them). The same record type also carries channel
        # events, peer messages and task notifications, so only origin human with command mode "prompt" is his.
        # From here on it is read as a typed prompt, and every rule for typed prompts applies to it.
        att = o.get("attachment") if isinstance(o.get("attachment"), dict) else {}
        if att.get("type") != "queued_command":
            return [], "not_user_attachment"
        att_origin = att.get("origin") if isinstance(att.get("origin"), dict) else None
        mode = att.get("commandMode")
        # The oldest transcripts wrote NO origin on this record (memory-bxxxxx's seat: 30 such messages, all from
        # June 2026, in a month where others already carried origin human, and none after it). Without an origin
        # the command mode is the only test: "prompt" is what a person typed, "task-notification" is the harness.
        old_shape = att_origin is None and mode == "prompt"
        if not old_shape and (att_origin is None or att_origin.get("kind") != "human"):
            return [], "queued_command_" + str(att_origin.get("kind") if att_origin else "no_origin")
        if mode not in (None, "prompt"):
            return [], "queued_command_mode_" + str(mode)
        o = {"type": "user", "uuid": uuid, "timestamp": o.get("timestamp") or att.get("timestamp"),
             "sessionId": o.get("sessionId") or o.get("session_id"), "parentUuid": o.get("parentUuid"),
             "origin": att_origin, "promptSource": "queued_command_legacy" if old_shape else "queued_command",
             "isMeta": bool(att.get("isMeta") or o.get("isMeta")), "message": {"content": att.get("prompt")}}
    if o.get("type") != "user":
        return [], "not_user_" + str(o.get("type"))
    msg = o.get("message") if isinstance(o.get("message"), dict) else {}
    content = msg.get("content")
    date = utc_instant(o.get("timestamp"))
    session = _no_lone_surrogates(o.get("sessionId") or o.get("session_id") or "")

    tur = o.get("toolUseResult")
    if isinstance(tur, dict) and isinstance(tur.get("answers"), dict) and tur["answers"]:
        call_ids = [b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(content, list) else []
        if any(i in prefilled for i in call_ids if i):
            return [], "ruling_prefilled_by_model"
        raws, dropped = [], []
        notes_all = tur.get("annotations") if isinstance(tur.get("annotations"), dict) else {}
        call_id = next((i for i in call_ids if isinstance(i, str) and i), None)
        for qi, q in enumerate(tur.get("questions") or []):
            if not isinstance(q, dict):
                continue
            question = q.get("question") or ""
            if question not in tur["answers"]:
                continue
            note = notes_all.get(question)
            note = (note.get("notes") or "") if isinstance(note, dict) else (note if isinstance(note, str) else "")
            raw, cls = _ruling_raw(seat, date, session, uuid, call_id, qi, question, q.get("options"),
                                   tur["answers"][question], note, bool(q.get("multiSelect")))
            if raw is None:
                dropped.append(cls)
                continue
            raws.append(raw)
        return raws, (dropped or (None if raws else "question_without_answer"))

    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return [], "tool_result"
    source = o.get("promptSource")
    origin = o.get("origin") if isinstance(o.get("origin"), dict) else None
    kind = origin.get("kind") if origin else None
    if origin is not None and kind != "human":
        return [], "system_origin_" + str(kind)
    if o.get("isCompactSummary"):
        return [], "compaction_summary"
    if o.get("isMeta"):
        return [], "meta_record"
    # The positive test for an operator prompt is origin.kind == "human" wherever the record has an origin (pxx's
    # review: on a current transcript an unlabelled record is usually NOT a typed prompt). Without an origin, a
    # known prompt source is accepted, and a record with neither field is an older-format prompt, kept and flagged.
    if origin is not None:
        legacy = False
    elif source == "queued_command_legacy":
        legacy = True                                          # a mid-turn message from before origins were written
    elif source in _OPERATOR_SOURCES:
        legacy = False
    elif source is None:
        legacy = True
    else:
        return [], "prompt_source_" + str(source)

    text, has_attachment = _typed_text(content)
    stripped = text.strip()
    if not stripped:
        return [], "empty_prompt"
    slash = None
    if stripped.startswith(("<command-name", "<command-message")):
        name = _CMD_NAME.search(stripped)
        args = _CMD_ARGS.search(stripped)
        args = args.group(1).strip() if args else ""
        if not (name and name.group(1) in _SLASH_WITH_OPERATOR_TEXT and args):
            return [], "local_command"
        slash, stripped = name.group(1), args
    else:
        for prefix, cls in _NOISE_PREFIXES:
            if stripped.startswith(prefix):
                return [], cls
        header = _PEER_HEADER.match(stripped)
        if header and header.group(1) in seats:
            return [], "peer_message"
        opener = _LABEL_OPENER.match(stripped)
        if opener:
            label = opener.group(1).lower()
            if label in seats and label not in (own_names(seat) if own is None else own):
                return [], "peer_report_pasted"
        if _BARE_SLASH.fullmatch(stripped):
            return [], "slash_command_name"
    marks, cut_class = [], None
    machine = machine_typed.search(stripped)
    if machine:
        whole = _machine_class(machine.group(0))
        stripped = stripped[:machine.start()].strip()
        if not stripped or whole in _NEVER_CUT:
            return [], whole
        marks.append(_MACHINE_MARK)
        cut_class = "machine_text_cut"                         # counted, and the card is still made
    if "<pasted_content" in stripped:
        stripped = _PASTED_BLOCK.sub(" ", stripped).strip()
        if not stripped:
            return [], "pasted_content_only"
        marks.append(_PASTED_MARK)
    if _IMAGE_ONLY.fullmatch(stripped):
        return [], "attachment_only"
    if _PASTED_OUTPUT.search(stripped):
        return [], "pasted_output"
    typed_len = len(stripped)                                  # the operator's own words, before any mark
    stripped = _no_lone_surrogates("\n".join([stripped] + marks))
    slash = _no_lone_surrogates(slash) if slash else None
    kept = {"kind": "slash_command", "command": slash, "text": stripped} if slash else {"kind": "typed_prompt", "text": stripped}
    card_text, truncated = _candidate_text(seat, date, (slash + " " + stripped) if slash else stripped)
    extra = {"prompt_source": "slash_command" if slash else (source or ("legacy_unlabelled" if legacy else "human_unlabelled")),
             "legacy_origin": legacy and not slash, "short": typed_len < 20,
             "has_attachment": has_attachment, "parent_uuid": _no_lone_surrogates(o.get("parentUuid"))}
    if slash:
        extra["slash_command"] = slash
    return [{"type": "Candidate", "block_index": 0, "kept": kept, "text": card_text, "truncated": truncated,
             "date": date, "session": session, "uuid": uuid, "extra": extra}], cut_class


CODEX_ASK_TOOLS = ("request_user_input", "request_user_input_async")
_THREAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")


def _question_raw(seat, date, session, uuid, call_id, index, question, options, multi_select, kind, thread_id=None):
    """One asked question as a raw Question: (raw, None), or (None, "question_empty"). The Codex walk and
    asked_question_card both come through here. The owner is the seat that asked."""
    seat = _no_lone_surrogates(seat) if isinstance(seat, str) else ""
    kind = _no_lone_surrogates(kind) if isinstance(kind, str) else "question_hook"
    uuid = _no_lone_surrogates(uuid)
    call_id = _no_lone_surrogates(call_id) if call_id else None
    session = _no_lone_surrogates(session)
    question = question.strip() if isinstance(question, str) else ""
    question = _no_lone_surrogates(question)
    if not question:
        return None, "question_empty"
    options = _options_of(options)
    kept = {"kind": kind, "question": question, "options": options, "multi_select": bool(multi_select)}
    text, truncated = _question_text(seat, seat, date, question, options)
    thread = _no_lone_surrogates(thread_id.strip()) if isinstance(thread_id, str) and thread_id.strip() else ""
    extra = {"thread_id": thread} if thread and _THREAD_RE.fullmatch(thread) else {}
    return {"type": "Question", "owner": seat, "block_index": index, "kept": kept, "text": text,
            "truncated": truncated, "date": date, "session": session, "uuid": uuid, "call_id": call_id,
            "extra": extra}, None


def codex_cards(o, seat, state, seats=KNOWN_SEATS, own=None):
    """Classify one record of a Codex rollout file. Same return as cards_from_record. `state` is one dict per
    file: {"session": the session id from session_meta, "item": the user item of the record before this one}.

    Codex writes what the harness puts into a session (AGENTS.md, the environment, a subagent's notice, a goal,
    an imported session) as user message items too, so a user item alone proves nothing. Two things mark one as
    typed into the client, and either is enough:
    - the item names its own kind: internal_chat_message_metadata_passthrough.content_item_kinds holds only
      user.* entries (user.text, user.image). Context items name another kind or none;
    - the record after it is an event of type user_message with the same text. An older terminal wrote no kinds
      and always wrote the event (530 of 530 typed items on the seat measured); a newer one writes the kinds and,
      on another seat, no event at all (codex-oxxxxxx-cli, CLI 0.161.0).
    The typed text then goes through cards_from_record, so every rule for a typed prompt applies, with the Codex
    list of machine-typed text.

    A question the seat put to the operator (CODEX_ASK_TOOLS) is a Question card. NO Ruling comes from a Codex
    transcript: the call returns at once with {"accepted":true}, and the answer arrives later as an ordinary
    typed message that names no call. Pairing it with the open question would be a guess; it is a Candidate."""
    if not isinstance(o, dict):
        return [], "not_a_record"
    payload = o.get("payload") if isinstance(o.get("payload"), dict) else {}
    kind, ptype = o.get("type"), payload.get("type")
    before, state["item"] = state.get("item"), None
    session = state.get("session") or ""
    def typed(ref, text, image):
        content = [{"type": "text", "text": text}] + ([{"type": "image"}] if image else [])
        record = {"type": "user", "uuid": ref, "timestamp": o.get("timestamp"), "sessionId": session,
                  "origin": {"kind": "human"}, "promptSource": "codex_typed", "message": {"content": content}}
        return cards_from_record(record, seat, (), seats, own, machine_typed=_CODEX_MACHINE_TYPED)

    if kind == "response_item" and ptype == "message" and payload.get("role") == "user":
        parts = [c for c in payload.get("content") or [] if isinstance(c, dict)]
        texts = [c.get("text") for c in parts if isinstance(c.get("text"), str)]
        # Codex wraps an attached picture in two text parts of its own; they are not the operator's words.
        words = [t for t in texts if not _CODEX_IMAGE_WRAP.fullmatch(t.strip())]
        image = any(c.get("type") != "input_text" for c in parts)
        meta = payload.get("internal_chat_message_metadata_passthrough")
        kinds = meta.get("content_item_kinds") if isinstance(meta, dict) else None
        own_kind = (isinstance(kinds, list) and bool(kinds)
                    and all(isinstance(k, str) and k.startswith("user.") for k in kinds))
        item_id = payload.get("id") if isinstance(payload.get("id"), str) and payload.get("id") else None
        state["item"] = {"id": item_id, "texts": texts, "done": own_kind and item_id is not None}
        if state["item"]["done"]:
            return typed(item_id, "\n".join(words), image)
        return [], "codex_user_item"                       # put in, or typed on an older terminal: the event decides
    if kind == "event_msg" and ptype == "user_message":
        message = payload.get("message")
        if not isinstance(message, str):
            return [], "codex_event_without_text"
        same = before is not None and (message in before["texts"] or "".join(before["texts"]) == message)
        if same and before["done"]:
            return [], "codex_user_event"                  # its item was the card
        ref = before["id"] if same else None
        if ref is None:
            # An older terminal wrote no id on the item (45 of 530). The session and the event's time name it, and
            # the n-th distinct prompt at one timestamp gets #n: two prompts typed in the same millisecond (queued
            # together) shared one id, and the second was lost as a uuid conflict (29 on codex-kxxx-cli, #314). The
            # first keeps the bare id, so a card already written under it is the same card. Times only move forward
            # in a file, so the last id and its count are all that is kept. read_file's checkpoint carries this
            # counter across bounded windows; a held user item does not increment it before its event arrives.
            base = "codex:%s:%s" % (session, o.get("timestamp") or "")
            last, n = state.get("fallback_last") or (None, 0)
            n = n + 1 if last == base else 1
            state["fallback_last"] = (base, n)
            ref = base if n == 1 else "%s#%d" % (base, n)
        return typed(ref, message, bool(payload.get("images") or payload.get("local_images")))
    if kind == "response_item" and ptype == "function_call" and payload.get("name") in CODEX_ASK_TOOLS:
        args = payload.get("arguments")
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            args = None
        questions = args.get("questions") if isinstance(args, dict) else None
        call_id = payload.get("call_id")
        if not (isinstance(questions, list) and isinstance(call_id, str) and call_id.strip()):
            return [], "codex_question_unreadable"
        raws, dropped = [], []
        for qi, q in enumerate(questions):
            if not isinstance(q, dict):
                continue
            options = [{"label": x} if isinstance(x, str) else x for x in (q.get("options") or [])]
            raw, cls = _question_raw(seat, utc_instant(o.get("timestamp")), session, payload.get("id") or call_id,
                                     call_id, qi, q.get("question") or q.get("title"), options,
                                     q.get("multiSelect") or q.get("multi_select"), "question_codex")
            if raw is None:
                dropped.append(cls)
                continue
            raws.append(raw)
        return raws, (dropped or (None if raws else "codex_question_unreadable"))
    return [], "codex_" + str(kind)


def _kimi_raws(o, kimi, seat, seats, own):
    """Cards from one Kimi wire record. The dialect module only READS (kimi_dialect.classify); every card is
    built here, by the loader's own functions, exactly as for the Claude and Codex dialects."""
    desc, drop_class = kimi_dialect.classify(o, kimi)
    if desc is None:
        return [], drop_class
    kind, payload = desc
    if kind in ("typed", "goal"):
        content = [{"type": "text", "text": payload["text"]}] + (
            [{"type": "image"}] if payload.get("has_attachment") else [])
        record = {"type": "user", "uuid": payload["uuid"], "timestamp": payload["time"],
                  "sessionId": kimi["session"],
                  "promptSource": None if payload.get("legacy")
                  else ("kimi_goal" if kind == "goal" else "kimi_typed"),
                  "message": {"content": content}}
        if not payload.get("legacy"):
            record["origin"] = {"kind": "human"}
        return cards_from_record(record, seat, (), seats, own, machine_typed=_KIMI_MACHINE_TYPED)
    if kind == "answers":
        raws, dropped = [], []
        for qi, q in enumerate(payload["questions"]):
            if not isinstance(q, dict):
                continue
            question = q.get("question") or q.get("title") or ""
            answer = payload["answers"].get(question)
            if answer is None:
                dropped.append("kimi_question_unanswered")   # never put to the operator
                continue
            if isinstance(answer, list):
                # A multi-select answer arrives as a list of chosen labels; the card holds them joined,
                # like the Claude walk's answers, so _label_only reads the same shape on every dialect.
                answer = ", ".join(str(x) for x in answer)
            raw, cls = _ruling_raw(seat, payload["time"], kimi["session"], payload["uuid"], payload["call_id"],
                                   qi, question, q.get("options"), answer, "",
                                   bool(q.get("multiSelect") or q.get("multi_select")))
            (dropped if raw is None else raws).append(cls if raw is None else raw)
        return raws, (dropped or None)
    return [], drop_class


def _antigravity_raws(o, antigravity, seat, seats, own):
    """Cards from one Antigravity transcript record. The dialect module only READS (antigravity_dialect.classify);
    every card is built here, by the loader's own functions, exactly as for the other dialects."""
    desc, drop_class = antigravity_dialect.classify(o, antigravity)
    if desc is None:
        return [], drop_class
    kind, payload = desc
    if kind in ("typed", "goal"):
        content = [{"type": "text", "text": payload["text"]}] + (
            [{"type": "image"}] if payload.get("has_attachment") else [])
        record = {"type": "user", "uuid": payload["uuid"], "timestamp": payload["time"],
                  "sessionId": antigravity["session"],
                  "promptSource": "antigravity_goal" if kind == "goal" else "antigravity_typed",
                  "origin": {"kind": "human"},
                  "message": {"content": content}}
        return cards_from_record(record, seat, (), seats, own)
    if kind == "answers":
        raws, dropped = [], []
        for qi, q in enumerate(payload["questions"]):
            if not isinstance(q, dict):
                continue
            question = q.get("question") or q.get("title") or ""
            ans = payload["answers"].get(question)
            if ans is None:
                dropped.append("antigravity_question_unanswered")
                continue
            if isinstance(ans, list):
                ans = ", ".join(str(x) for x in ans)
            opts = [o if isinstance(o, dict) else {"label": str(o)} for o in q.get("options") or []]
            raw, cls = _ruling_raw(seat, utc_instant(payload["time"]), antigravity["session"], payload["uuid"], payload["call_id"],
                                   qi, question, opts, ans, "",
                                   bool(q.get("multiSelect") or q.get("is_multi_select")),
                                   kind="answered_question_modal")
            (dropped if raw is None else raws).append(cls if raw is None else raw)
        return raws, (dropped or None)
    return [], drop_class


_INSTANT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")
SERVER_VALUE_LIMIT = 8 * 1024          # the server refuses a card whose JSON is larger (design section 4, rule 9)


def utc_instant(ts):
    """A record's time in ONE spelling, YYYY-MM-DDTHH:MM:SS.mmmZ, or "" when it is missing or has no zone.
    One spelling makes string order the same as time order, which the sort here, the earliest-copy rule and the
    server's supersedes check all rely on. Claude Code already writes this spelling; an offset time is converted."""
    m = _INSTANT.match(ts) if isinstance(ts, str) else None
    if not m:
        return ""
    y, mo, d, h, mi, s, frac, zone = m.groups()
    try:
        t = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), int(((frac or "") + "000000")[:6]))
        if zone != "Z":
            offset = datetime.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
            t = t - offset if zone[0] == "+" else t + offset
    except (ValueError, OverflowError):
        return ""
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def _kept_strings(kept):
    out = []
    for v in kept.values():
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    out.extend(x for x in item.values() if isinstance(x, str))
    return "\n".join(out)


ID_RULE = 2


def card_identity(card_type, record_uuid, block_index, call_id=None):
    """(card id, the source reference the id is made from).

    A Candidate is named by its transcript record and block. A Ruling is named by the QUESTION CALL's id and the
    question's place in that call: a client hook that writes the ruling at the moment it is answered knows the call
    id and the question order, and does not know the transcript record's uuid, so this is the one name both can
    make and the server sees one card, not two. The place in the call is part of it because one call carries up to
    four questions (measured on one seat: 45 of 78 calls carry more than one). An answered record with no call id
    falls back to the record's own uuid."""
    record_uuid = _no_lone_surrogates(record_uuid)
    call_id = _no_lone_surrogates(call_id) if call_id else None
    if card_type == "Ruling" and call_id:
        return sha256_text("v1|ask|%s|%d" % (call_id, block_index))[:32], call_id
    if card_type == "Question" and call_id:
        # The question as ASKED, before any answer: the same call and place as its Ruling, under another word, so
        # the writer of a Ruling can name the Question it closes without having seen it.
        return sha256_text("v1|question|%s|%d" % (call_id, block_index))[:32], call_id
    return sha256_text("v1|%s|%d" % (record_uuid, block_index))[:32], record_uuid


def _raw_identity(raw):
    """(id, ref) of a raw card, for run() and approved_cards alike. The id names the transcript record and
    block, NOT its session or content: the server keys a card by it, and a changed card (a longer cut, a new
    prefix, a later loader) must be a new version of the same key, never a second card. The content hashes
    travel as fields."""
    return card_identity(raw["type"], raw["uuid"], raw["block_index"], raw.get("call_id"))


def _card_bytes(card):
    return len(json.dumps(card, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


FIT_BYTES = 7000                       # under SERVER_VALUE_LIMIT, with room for provenance that grows later
OPTION_FLOOR = 200                     # characters of an option description that a cut always leaves


def _fit_size(card):
    """The size a cut is measured against: the card without the file names and sessions it was seen in. Those
    differ by writer (a builder knows no file) and grow when a later run sees the answer again, and a cut that
    moved with them would give the same answer two texts. FIT_BYTES leaves them room under the server's limit."""
    source = card.get("source")
    if not isinstance(source, dict):
        return _card_bytes(card)
    return _card_bytes(dict(card, source=dict(source, files=[], sessions=[])))


def fit_to_limit(card):
    """Cut a card's long fields until it fits the server's value limit. `text` is already capped, but the answer,
    the note, the question and the option descriptions are stored whole, and a free-text answer can be a whole
    pasted document (one seat had a 51,152-byte answer). The cut leaves a line that says how long the field was
    and sets `truncated`. Option descriptions are cut last and evenly, to one shared length, and only when the
    other cuts were not enough, so a card that fitted without them is not changed (memory-bxxxxx: four options
    of 4,000 characters gave a 17,393-byte card the server refuses). Labels are never cut: the answer names
    them. Redaction has already run over the FULL fields by the time this is called, and
    `source.source_sha256` is the hash of the full fields, so a cut never hides what was scanned. Returns True
    when something was cut; a card can still be over the limit afterwards (see card_too_large)."""
    cut = False
    for field, floor in (("answer", 1000), ("notes", 500), ("question", 1000)):
        full = card.get(field)
        if not isinstance(full, str):
            continue
        keep = len(full)
        while _fit_size(card) > FIT_BYTES and keep > floor:
            keep = max(floor, keep - max(200, _fit_size(card) - FIT_BYTES))
            card[field] = full[:keep] + "\n[cut: the full %s was %d characters]" % (field, len(full))
            cut = True
    options = card.get("options")
    if _fit_size(card) > FIT_BYTES and isinstance(options, list):
        full = [o.get("description") if isinstance(o, dict) and isinstance(o.get("description"), str) else ""
                for o in options]
        options = card["options"] = [dict(o) if isinstance(o, dict) else o for o in options]
        cap = max([len(d) for d in full] or [0])
        while _fit_size(card) > FIT_BYTES and cap > OPTION_FLOOR:
            at_cap = sum(1 for d in full if len(d) >= cap) or 1
            cap = max(OPTION_FLOOR, cap - max(100, (_fit_size(card) - FIT_BYTES) // at_cap))
            for o, d in zip(options, full):
                if len(d) > cap:
                    o["description"] = d[:cap] + "\n[cut: the full description was %d characters]" % len(d)
                    cut = True
    if cut:
        card["truncated"] = True
    return cut


def _card_strings(card):
    """Every string a stored card would hand to a reader. Not its ids, hashes and record references: those are
    not prose, and a question call's id has the shape of a vendor key (a scan of every string refused all 162
    Rulings of one seat and no other card)."""
    out = [card.get(k) or "" for k in ("text", "question", "answer", "notes", "done_when")]
    options = card.get("options")
    # A card written by hand can hold anything here. The server takes an option as a plain string or as an object;
    # whatever else it is, this reads what strings there are and never raises (pxx: "options": 1 did).
    for o in options if isinstance(options, (list, tuple)) else [options]:
        if isinstance(o, dict):
            out.extend(o.values())
        else:
            out.append(o)
    refs = card.get("refs")
    if isinstance(refs, dict):
        for v in refs.values():
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, (list, tuple)):
                out.extend(str(x) for x in v if x is not None)
    return "\n".join(x for x in out if isinstance(x, str))


# ---- Attribution (loader 1.19, gap register item 7). Who said a card's text, whose words it quotes, how it was
# extracted and whether the operator confirmed it. The server's rules are apps/cards/validation._check_attribution:
# a Ruling is the operator's answer, a Question is the seat's own model asking, and on a card born with
# attribution source.first_hand must say what attribution says (the operator, quoting nobody).
_SPEAKER_AND_CONFIRMATION = {"Ruling": ("operator", "answered"), "Question": ("agent", "asked"),
                             "Candidate": ("operator", "typed")}
_NOT_EXTRACTION = re.compile(r"[^a-z_]+")
# A pasted document: long, many lines, or markdown headings. The loader keeps it (its words may still be the
# decision) and says the author is not known, so a reader does not take a document for the operator's own words.
# Measured, counts only (txxxx, 11 Oct): on exxxx's blind sample (76 cards, msg/<key>)
# the Candidate rule marks all 5 cards labelled agent_words and none of the 39 other Candidates; it marks 73 of
# dxxxxx's 1,285 Candidates and 50 of txxxx's 795. A Ruling answer has its own, stricter rule: one labelled the
# operator's own words in that sample is 1,422 characters over 25 lines with no heading, and dxxxxx's pasted
# answer is 51,149 characters with 16 headings (msg/<key>). A Question is the model's words
# and is never marked. A smoke test of five positives, not a measurement of the rule's precision.
PASTED_DOC_CHARS = 1000
PASTED_DOC_LINES = 10
PASTED_DOC_HEADINGS = 2
PASTED_ANSWER_CHARS = 5000
_MD_HEADING = re.compile(r"(?m)^ {0,3}#{1,6}[ \t]+\S")


def pasted_document(text, answer=False):
    """True when an operator's text reads as a pasted document rather than words he typed. A typed prompt: at
    least PASTED_DOC_CHARS characters, PASTED_DOC_LINES lines or PASTED_DOC_HEADINGS markdown headings. An answer
    to a question (`answer`): PASTED_DOC_HEADINGS headings or PASTED_ANSWER_CHARS characters, since a long answer
    in many lines is often his own."""
    text = text if isinstance(text, str) else ""
    if len(_MD_HEADING.findall(text)) >= PASTED_DOC_HEADINGS:
        return True
    if answer:
        return len(text) >= PASTED_ANSWER_CHARS
    return len(text) >= PASTED_DOC_CHARS or len(text.splitlines()) >= PASTED_DOC_LINES


def _operator_words(raw):
    """The text an operator wrote, FULL and before any cut: a Candidate's typed text, a Ruling's free-text answer
    and its note (an answer that only names offered options is the options' words, not a paste)."""
    kept = raw["kept"]
    if raw["type"] == "Question":
        return ""                                  # the model's words, never the operator's: never a paste of his
    if raw["type"] == "Candidate":
        return kept.get("text") or ""
    free = kept.get("answer") if raw["extra"].get("free_text") else ""
    return "\n".join(x for x in (free, kept.get("notes")) if isinstance(x, str) and x)


def extraction_of(dialect, kind):
    """<dialect>/<kind> in the server's alphabet: lower-case letters and underscores, 31 and 32 at most."""
    part = lambda s, n: (_NOT_EXTRACTION.sub("_", str(s).lower()).strip("_")[:n] or "unknown")  # noqa: E731
    return part(dialect, 31) + "/" + part(kind, 32)


def attribution_of(raw):
    """The `attribution` block of a raw card. A type with no rule here raises: no card is guessed at."""
    speaker, confirmation = _SPEAKER_AND_CONFIRMATION[raw["type"]]
    quoted = "unknown" if pasted_document(_operator_words(raw), answer=raw["type"] == "Ruling") else None
    return {"speaker": speaker, "quoted_author": quoted, "extraction": extraction_of(raw["dialect"], raw["kept"]["kind"]),
            "confirmation": confirmation}


def _card_from_raw(raw, seat, cid, ref, source_sha, files):
    """The stored card for one raw card that passed redaction, cut to fit the server's limit."""
    att = attribution_of(raw)
    card = {"schema": "card.v1", "id": cid, "type": raw["type"], "owner": raw.get("owner", "operator"), "seat": seat,
            "date": raw["date"][:10], "time": raw["date"],
            "status": "active", "text": raw["text"], "text_sha256": sha256_text(raw["text"]),
            "truncated": raw["truncated"], "loader_version": LOADER_VERSION, "attribution": att,
            "source": {"record_uuid": ref, "block_index": raw["block_index"],
                       "source_sha256": source_sha, "kind": raw["kept"]["kind"],
                       "first_hand": att["speaker"] == "operator" and att["quoted_author"] is None,
                       "sessions": [raw["session"]] if raw["session"] else [], "files": list(files)}}
    if ref != raw["uuid"]:
        card["source"]["transcript_uuid"] = raw["uuid"]
    if raw["type"] == "Ruling":
        card.update({k: raw["kept"][k] for k in ("question", "options", "answer", "notes", "multi_select")})
    elif raw["type"] == "Question":
        card.update({k: raw["kept"][k] for k in ("question", "options", "multi_select")})
    card.update(raw["extra"])
    fit_to_limit(card)
    return card


# ---- Builders for a writer that is not the transcript walk (the answer-time hook, through `card write`).
# One answered or asked question in, one card or one refusal out. They write nothing, sign nothing, read no head
# and add no `supersedes`: that is the writer's part. A refusal is a short stable code a seat can be shown in one
# line; "redaction:" is followed by the names of the detectors that fired, joined by commas.
CARD_REFUSALS = ("no_call_id", "time_format", "ruling_prefilled_by_model", "answer_empty", "wake_injection",
                 "pasted_output", "question_empty", "card_too_large", "redaction:")
_CARD_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def _builder_inputs(call_id, index, time):
    if not (isinstance(call_id, str) and call_id.strip()):
        return "no_call_id"
    if type(index) is not int or index < 0:
        return "no_call_id"
    # The server knows ONE spelling of a time. A transcript record and JavaScript's toISOString both write it, so
    # this refuses only a caller that hands in local time or a time with no zone (pxx).
    if not (isinstance(time, str) and _CARD_TIME.fullmatch(time) and utc_instant(time) == time):
        return "time_format"
    return None


def _fits(card):
    """A built card, or the refusal for one the server would not take. Every cut has a floor and labels are never
    cut, so a question with very many options, or very long labels, can stay over the limit. The transcript walk
    keeps such a card in the local store and counts it (cards_over_server_limit); a builder's caller is about to
    write, so it gets a code and makes no failed write."""
    if _card_bytes(card) > SERVER_VALUE_LIMIT:
        return None, "card_too_large"
    return card, None


def answered_question_card(seat, call_id, index, question, options, answer, time, notes="", multi_select=False,
                           prefilled=False, kind="answered_question", session="", transcript_uuid=None,
                           disabled_detectors=(), dialect="claude"):
    """One answered question as a Ruling card: (card, None), or (None, a code from CARD_REFUSALS).

    The rules are the transcript walk's own, through the same code: the drop rules for an answer, the detectors
    (the seat's own included) over the FULL question, answer, note and options before anything is cut, the text,
    the id and the cut to the server's limit. A card built here and the card mined later from the transcript for
    the same answer have one id; they differ in what only the caller knows (`time`, `kind`) and in the provenance
    only a transcript has. `prefilled` is true when the question call's own input already carried the answers:
    the model wrote them, not the operator. `transcript_uuid` is the uuid of the transcript record that holds the
    answer, for a caller that has read it (and so took `time` from that record); it is stored as
    source.transcript_uuid, as the walk stores it, and its absence says the time is the caller's clock."""
    bad = _builder_inputs(call_id, index, time)
    if bad:
        return None, bad
    if prefilled:
        return None, "ruling_prefilled_by_model"
    seat = _no_lone_surrogates(seat) if isinstance(seat, str) else ""
    dialect = _no_lone_surrogates(dialect) if isinstance(dialect, str) else "claude"
    note = notes if isinstance(notes, str) else ""
    record = transcript_uuid if isinstance(transcript_uuid, str) and transcript_uuid.strip() else call_id
    raw, cls = _ruling_raw(seat, time, session if isinstance(session, str) else "", record, call_id, index,
                           question if isinstance(question, str) else "", options, answer, note, bool(multi_select),
                           kind)
    if raw is None:
        return None, cls
    raw["dialect"] = dialect                     # the harness the hook ran in; its attribution.extraction names it
    fired = detect(_kept_strings(raw["kept"]), disabled_detectors, seat)
    if fired:
        return None, "redaction:" + ",".join(sorted(fired))
    source_sha = sha256_text(json.dumps(raw["kept"], sort_keys=True, ensure_ascii=False))
    cid, ref = card_identity("Ruling", call_id, index, call_id)
    return _fits(_card_from_raw(raw, seat, cid, ref, source_sha, ()))


def _question_text(seat, owner, date, question, options):
    head = "Question | owner: %s | seat: %s | %s" % (owner, seat, (date or "")[:16])
    parts = [head, "Q: " + _clip(question, 900)]
    labels = [o.get("label") for o in options if o.get("label")]
    if labels:
        parts.append("Offered: " + "; ".join(labels))
    text = "\n".join(parts)
    return _clip(text, MAX_CARD_TEXT), len(text) > MAX_CARD_TEXT


def asked_question_card(seat, call_id, index, question, options, time, multi_select=False, thread_id=None,
                        kind="question_hook", session="", disabled_detectors=(), dialect="claude"):
    """One question a seat has put to the operator, not yet answered, as a Question card: (card, None), or
    (None, a code from CARD_REFUSALS). The owner is the seat that asked: the operator has said nothing yet. Its id
    is card_identity("Question", ...) of the same call and place as the Ruling that will answer it."""
    bad = _builder_inputs(call_id, index, time)
    if bad:
        return None, bad
    seat = _no_lone_surrogates(seat) if isinstance(seat, str) else ""
    dialect = _no_lone_surrogates(dialect) if isinstance(dialect, str) else "claude"
    raw, cls = _question_raw(seat, time, session if isinstance(session, str) else "", call_id, call_id, index,
                             question, options, multi_select, kind, thread_id)
    if raw is None:
        return None, cls
    raw["dialect"] = dialect
    fired = detect(_kept_strings(raw["kept"]), disabled_detectors, seat)
    if fired:
        return None, "redaction:" + ",".join(sorted(fired))
    source_sha = sha256_text(json.dumps(raw["kept"], sort_keys=True, ensure_ascii=False))
    cid, ref = card_identity("Question", call_id, index, call_id)
    return _fits(_card_from_raw(raw, seat, cid, ref, source_sha, ()))


def screen_prose(card, seat=None, disabled_detectors=()):
    """Names of the detectors that fire on a finished card's prose: its text, question, answer, notes, done_when and
    options, with the seat's own detectors (the card's seat when none is named). For a card no builder made, a
    Commitment or a Decision written by hand; a card from a builder has already been through the same detectors
    over its full fields. Structural strings are left out on purpose (see _card_strings)."""
    if not isinstance(card, dict):
        return ["not_a_card"]
    return detect(_card_strings(card), disabled_detectors, seat if seat is not None else card.get("seat"))


_REPEAT_FIELDS = ("repeat_of", "repeats", "repeat_last_time")


def mark_repeats(cards):
    """Label Candidates that say the same thing again. The operator types some prompts many times (measured on
    one seat: 91 of 817 Candidates repeat an earlier one, 60 of them one short line), and in a search those copies
    crowd out everything else (dxxxxx's recall run, 8 Oct). Nothing is removed: the earliest card of each text
    gets `repeats` (how many later copies) and `repeat_last_time`; every later copy gets `repeat_of` (the
    earliest card's id). A reader that ranks cards skips the ones with `repeat_of`, and the write step does not
    send them. Rulings are never grouped: the same answer to two questions is two decisions. Computed from the
    whole set on every run, so the result does not depend on the order files or runs came in."""
    groups = {}
    for c in cards:
        for k in _REPEAT_FIELDS:
            c.pop(k, None)
        if c["type"] == "Candidate":
            groups.setdefault(c["source"]["source_sha256"], []).append(c)
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda c: (c["time"], c["id"]))
        group[0]["repeats"] = len(group) - 1
        group[0]["repeat_last_time"] = group[-1]["time"]
        for c in group[1:]:
            c["repeat_of"] = group[0]["id"]


def _note_variant(card, source_sha, session, file_name):
    """Record that the same transcript record and block was also seen with other content."""
    src = card["source"]
    if source_sha != src["source_sha256"]:
        src["other_content"] = sorted(set(src.get("other_content", [])) | {source_sha})
    if session:
        src["sessions"] = sorted(set(src["sessions"]) | {session})
    if file_name:
        src["files"] = sorted(set(src["files"]) | {file_name})


def _load_jsonl(path):
    rows = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


PENDING_SPOOL_FILE = "pending.jsonl"
PENDING_UPLOAD_LOCK = "upload.lock"


def _env_wants_spool():
    for var in ("MESHY_CARDS_WRITE", "WRITE"):
        if (os.environ.get(var) or "").strip().lower() == "spool":
            return True
    return False


def _pending_spool_path(out_dir):
    target = os.path.abspath(str(out_dir))
    if os.path.isfile(target) or target.lower().endswith((".jsonl", ".ndjson", ".json")):
        if os.path.basename(target).lower() == PENDING_SPOOL_FILE:
            return target
        return os.path.join(os.path.dirname(target), PENDING_SPOOL_FILE)
    return os.path.join(target, PENDING_SPOOL_FILE)


def _acquire_upload_lock(lock_path, timeout_s=10.0):
    import time as _time
    deadline = _time.monotonic() + timeout_s
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            if os.name == "nt":
                import msvcrt
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return fd
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    pass
            if _time.monotonic() >= deadline:
                raise TimeoutError("could not acquire upload lock %s within %gs" % (lock_path, timeout_s))
            _time.sleep(0.02)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _release_upload_lock(fd):
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _is_external_producer_card(card):
    src = card.get("source") if isinstance(card, dict) and isinstance(card.get("source"), dict) else {}
    kind = str(src.get("kind") or "")
    return kind == "approvals_row" or kind == "approvals_table" or kind.startswith("worker_run:")


def _card_producer_family(card):
    src = card.get("source") if isinstance(card, dict) and isinstance(card.get("source"), dict) else {}
    kind = str(src.get("kind") or "")
    if kind in ("approvals_row", "approvals_table"):
        return "approvals"
    if kind.startswith("worker_run:"):
        return "board_worker"
    return "transcript"


def _card_producer_scope(card):
    family = _card_producer_family(card)
    seat = str((card.get("seat") if isinstance(card, dict) else "") or "")
    src = card.get("source") if isinstance(card, dict) and isinstance(card.get("source"), dict) else {}
    if family == "approvals":
        repo_path = ""
        c_files = src.get("files") or ()
        if c_files and isinstance(c_files[0], str):
            repo_path = c_files[0].split("@", 1)[0]
        elif isinstance(src.get("file"), str) and src.get("file"):
            repo_path = src["file"].split("@", 1)[0]
        else:
            rec_uuid = str(src.get("record_uuid") or "")
            if rec_uuid.startswith("approval:"):
                parts = rec_uuid.split(":")
                if len(parts) >= 4:
                    repo_path = ":".join(parts[1:-1])
        return ("approvals", seat, repo_path)
    return (family, seat)


def _matches_producer(card, producer):
    if producer is None:
        return False
    if isinstance(producer, str):
        return _card_producer_family(card) == producer
    if isinstance(producer, tuple):
        scope = _card_producer_scope(card)
        return len(scope) >= len(producer) and scope[: len(producer)] == producer
    return False


def _sibling_output_card_keys(out_dir, current_out_path):
    """Return `(seat, id)` keys present in sibling card output files in `out_dir` (excluding `current_out_path`,
    `pending.jsonl`, `pending.inflight.jsonl`, `drops.jsonl`, and `runs.jsonl`) so multiple snapshots written to
    distinct `--out` files in one directory never evict each other's queued cards.
    """
    keys = set()
    if not out_dir or not os.path.isdir(out_dir):
        return keys
    cur_norm = os.path.normcase(os.path.abspath(current_out_path)) if current_out_path else ""
    skip_names = {
        PENDING_SPOOL_FILE.lower(),
        "pending.inflight.jsonl",
        "drops.jsonl",
        "runs.jsonl",
    }
    try:
        entries = sorted(os.listdir(out_dir))
    except OSError:
        return keys
    for name in entries:
        low = name.lower()
        if low in skip_names or low.endswith("_drops.jsonl") or not (low.endswith(".jsonl") or low.endswith(".ndjson")):
            continue
        cand = os.path.join(out_dir, name)
        if os.path.normcase(os.path.abspath(cand)) == cur_norm or not os.path.isfile(cand):
            continue
        for row in _load_jsonl(cand):
            if (
                isinstance(row, dict)
                and row.get("schema") == "card.v1"
                and row.get("id")
                and row.get("type") != "Context"
                and not row.get("repeat_of")
            ):
                keys.add((row.get("seat") or "", row.get("id") or ""))
    return keys


def _card_matches_exclusion(card, is_excluded_fn):
    if is_excluded_fn is None or not isinstance(card, dict):
        return False
    src = card.get("source") if isinstance(card.get("source"), dict) else {}
    c_files = src.get("files") or ()
    c_sessions = src.get("sessions") or ()
    c_file = (src.get("file"),) if isinstance(src.get("file"), str) and src.get("file") else ()
    return (
        any(is_excluded_fn(f) for f in c_files)
        or any(is_excluded_fn(s) for s in c_sessions)
        or any(is_excluded_fn(f) for f in c_file)
    )


def write_pending_spool(
    out_dir,
    cards,
    *,
    replace=False,
    preserve_external=False,
    producer=None,
    preserve_keys=None,
    is_excluded_fn=None,
):
    """Atomically write server-writable `cards` (excluding local-only `repeat_of` rows) to `<out_dir>/pending.jsonl`
    under `<out_dir>/upload.lock` via `<pending.jsonl>.tmp.<pid>`, `flush()` + `os.fsync()`, and `os.replace()`.
    By default (`replace=False`, `producer=None`), unions `cards` with any unclaimed cards already present in
    `<out_dir>/pending.jsonl` so multiple producers (`transcript_cards.py`, `approvals_cards.py`,
    `board_worker_cards.py`) sharing one seat directory never overwrite each other's queued batches before
    `postbox.py` claims `pending.jsonl`.
    When `producer` is set (either a family string or a scoped tuple such as `("transcript", seat)`,
    `("approvals", seat, repo_path)`, or `("board_worker", seat)`), replaces existing unclaimed cards matching that
    producer scope (unless their `(seat, id)` is in `preserve_keys`) while retaining unclaimed cards from other
    producer scopes that do not match `is_excluded_fn`.
    Raises `ValueError` if any card has `type == "Context"` (`Context` cards require single-writer `cursor.json`
    ownership in the loader and cannot be spooled to `postbox.py`).
    """
    card_list = [c for c in (cards or ()) if isinstance(c, dict)]
    if any(c.get("type") == "Context" for c in card_list):
        raise ValueError(
            "write_pending_spool refuses Context cards: --emit-context cannot be combined with "
            "--spool-pending / WRITE=spool because Context cards require single-writer cursor.json ownership"
        )
    pending_path = _pending_spool_path(out_dir)
    parent = os.path.dirname(pending_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    writable = [
        c for c in card_list
        if not c.get("repeat_of") and not _card_matches_exclusion(c, is_excluded_fn)
    ]
    preserved_set = set(preserve_keys or ())
    lock_fd = _acquire_upload_lock(os.path.join(parent, PENDING_UPLOAD_LOCK))
    try:
        merged_by_key = {}
        if os.path.isfile(pending_path):
            for existing in _load_jsonl(pending_path):
                if not isinstance(existing, dict) or existing.get("type") == "Context" or existing.get("repeat_of"):
                    continue
                if _card_matches_exclusion(existing, is_excluded_fn):
                    continue
                ekey = (existing.get("seat") or "", existing.get("id") or "")
                if producer is not None:
                    if _matches_producer(existing, producer) and ekey not in preserved_set:
                        continue
                elif replace and not (preserve_external and _is_external_producer_card(existing)):
                    if ekey not in preserved_set:
                        continue
                if ekey[1]:
                    merged_by_key[ekey] = existing
        for c in writable:
            ckey = (c.get("seat") or "", c.get("id") or "")
            if ckey[1]:
                merged_by_key[ckey] = c
            else:
                merged_by_key[(id(c), "")] = c
        final_cards = list(merged_by_key.values())
        payload = "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in final_cards)
        tmp_path = "%s.tmp.%d" % (pending_path, os.getpid())
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, pending_path)
        except BaseException:
            try:
                if os.path.lexists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return len(final_cards)
    finally:
        _release_upload_lock(lock_fd)


def reconcile_pending_spool(
    out_dir, allowed_cards, is_excluded_fn=None, *, producer="transcript", preserve_keys=None
):
    """Reconcile an existing `<out_dir>/pending.jsonl` after an exclusion, rescreen, or store rebuild so excluded,
    rescreened, or stale-content cards for `producer` never remain in an unclaimed ready spool when the current run
    is local-only (`WRITE=0`).
    Preserves unclaimed cards from other producer scopes (and any key in `preserve_keys`) unless they match
    `is_excluded_fn`.
    """
    pending_path = _pending_spool_path(out_dir)
    if not os.path.isfile(pending_path):
        return None
    parent = os.path.dirname(pending_path)
    allowed_by_key = {
        (c.get("seat") or "", c.get("id") or ""): c
        for c in (allowed_cards or ())
        if isinstance(c, dict) and c.get("id") and c.get("type") != "Context" and not c.get("repeat_of")
    }
    preserved_set = set(preserve_keys or ())
    lock_fd = _acquire_upload_lock(os.path.join(parent, PENDING_UPLOAD_LOCK))
    try:
        if not os.path.isfile(pending_path):
            return None
        existing = _load_jsonl(pending_path)
        kept = []
        changed = False
        for c in existing:
            if not isinstance(c, dict) or c.get("type") == "Context" or c.get("repeat_of"):
                changed = True
                continue
            if _card_matches_exclusion(c, is_excluded_fn):
                changed = True
                continue
            ckey = (c.get("seat") or "", c.get("id") or "")
            if producer is not None and not _matches_producer(c, producer):
                kept.append(c)
            elif producer is None and _is_external_producer_card(c):
                kept.append(c)
            elif ckey in allowed_by_key:
                fresh = allowed_by_key[ckey]
                if fresh != c:
                    changed = True
                kept.append(fresh)
            elif ckey in preserved_set:
                kept.append(c)
            else:
                changed = True
        if changed:
            payload = "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in kept)
            tmp_path = "%s.tmp.%d" % (pending_path, os.getpid())
            try:
                with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, pending_path)
            except BaseException:
                try:
                    if os.path.lexists(tmp_path):
                        os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        return len(kept)
    finally:
        _release_upload_lock(lock_fd)


def _check_input(path):
    name = os.path.basename(path)
    if not name.lower().endswith(".jsonl"):
        raise ValueError("refusing a file that is not a .jsonl transcript: " + name)
    if _FORBIDDEN_NAME.search(name):
        raise ValueError("refusing a file whose name looks like state or key material: " + name)


_ROLLOUT_SID_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$")


def _canonical_path_forms(p, base_dir=None):
    forms = set()
    if not isinstance(p, str) or not p.strip():
        return forms
    raw = p.strip().replace("\\", "/")
    norm = os.path.normpath(raw).replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    bname = os.path.basename(norm)
    forms.add(raw)
    forms.add(norm)
    forms.add(os.path.normcase(norm).replace("\\", "/"))
    if bname:
        forms.add(bname)
        forms.add(os.path.normcase(bname))
    try:
        ab = os.path.abspath(raw)
        forms.add(ab.replace("\\", "/"))
        forms.add(os.path.normcase(ab).replace("\\", "/"))
        rp = os.path.realpath(ab)
        forms.add(rp.replace("\\", "/"))
        forms.add(os.path.normcase(rp).replace("\\", "/"))
    except OSError:
        ab, rp = None, None
    if base_dir:
        try:
            b_ab = os.path.abspath(base_dir)
            b_rp = os.path.realpath(base_dir)
            if ab:
                rel_ab = os.path.normpath(os.path.relpath(ab, b_ab)).replace("\\", "/")
                forms.add(rel_ab)
                forms.add(os.path.normcase(rel_ab).replace("\\", "/"))
            if rp:
                rel_rp = os.path.normpath(os.path.relpath(rp, b_rp)).replace("\\", "/")
                forms.add(rel_rp)
                forms.add(os.path.normcase(rel_rp).replace("\\", "/"))
        except (OSError, ValueError):
            pass
    forms.update(f.lower() for f in list(forms))
    return forms


def _pattern_path_forms(raw_pat, base_dir=None):
    forms = set()
    if not isinstance(raw_pat, str) or not raw_pat.strip():
        return forms
    raw = raw_pat.strip().replace("\\", "/")
    norm = os.path.normpath(raw).replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    forms.add(raw)
    forms.add(norm)
    forms.add(os.path.normcase(norm).replace("\\", "/"))
    if "/" not in norm:
        forms.add(os.path.basename(norm))
        forms.add(os.path.normcase(os.path.basename(norm)))
    cand_inputs = [raw_pat.strip()]
    if base_dir and not os.path.isabs(raw_pat.strip()):
        cand_inputs.append(os.path.join(base_dir, raw_pat.strip()))
    b_ab = os.path.abspath(base_dir) if base_dir else None
    b_rp = os.path.realpath(base_dir) if base_dir else None
    for cand_in in cand_inputs:
        try:
            ab = os.path.abspath(cand_in)
            rp = os.path.realpath(cand_in)
            for p_cand in (ab, rp):
                forms.add(p_cand.replace("\\", "/"))
                forms.add(os.path.normcase(p_cand).replace("\\", "/"))
            if b_ab and b_rp:
                for rel_c in (os.path.relpath(ab, b_ab), os.path.relpath(rp, b_rp)):
                    rn = os.path.normpath(rel_c).replace("\\", "/")
                    forms.add(rn)
                    forms.add(os.path.normcase(rn).replace("\\", "/"))
        except (OSError, ValueError):
            pass
    forms.update(f.lower() for f in list(forms))
    return forms


#: Marks a resolved identity among the specs. _matches_file_spec compares a marked spec exactly (a realpath as a
#: string, a session id whole) and never through its name, prefix or path-form rules (pxx N1, #354).
RESOLVED_MARK = "\x00resolved:"


def exclusion_specs(excluded_files):
    """Every spec the matcher compares, from cursor["excluded_files"]; the one reader of that map, for the loader
    and the hook alike (pxx, #354). A RESOLVED entry contributes only the identities it matched when it was
    added (realpaths, session ids, stored-card refs), so a retargeted symlink, a renamed file, a later session
    sharing a prefix or a run from another cwd cannot move it. An entry not yet resolved (an older cursor's True,
    or a pattern that matched nothing when last checked) still matches by its typed text: it never silently
    stops excluding."""
    specs = set()
    for key, value in (excluded_files or {}).items():
        if isinstance(value, dict) and value.get("resolved"):
            for field, tag in (("realpaths", "rp"), ("sessions", "sid"), ("card_refs", "ref")):
                specs.update(RESOLVED_MARK + tag + ":" + r for r in value.get(field) or () if isinstance(r, str) and r)
        elif isinstance(key, str) and key.strip():
            specs.add(key)
    return sorted(specs)


def _file_identity(path):
    """Identity of an existing path, including case/Unicode aliases on the local filesystem."""
    try:
        st = os.stat(path)
        # Zero means the filesystem did not provide a usable file identity.
        return (st.st_dev, st.st_ino) if st.st_ino != 0 else None
    except (OSError, ValueError):
        return None


def _matches_resolved(path, marked, base_dir=None, session_id=None):
    """Exact match of `path` against RESOLVED_MARK specs: its realpath equal to a stored realpath, one of its session
    ids (or the string itself, for a card's session entry) equal to a stored id, or the string equal to a stored
    card ref. Existing realpath aliases also match by file identity, but a stored path that has become a
    symlink must not rebind the exclusion to its new target. No prefixes or basenames."""
    name = os.path.basename(path)
    stem = name[:-6] if name.lower().endswith(".jsonl") else name
    m = _ROLLOUT_SID_RE.match(stem)
    colon_sid = path.rsplit(":", 1)[-1] if path.startswith("planboard:") else ""
    sids = {s for s in (session_id, m.group(1) if m else "", colon_sid, path) if s}
    try:
        cand = path if os.path.isabs(path) or not base_dir else os.path.join(base_dir, path)
        rp = os.path.normcase(os.path.realpath(cand))
    except (OSError, ValueError):
        rp = ""
    identity = _file_identity(rp) if rp else None
    for spec in marked:
        tag, _, value = spec[len(RESOLVED_MARK):].partition(":")
        if not value:
            continue
        if tag == "rp" and rp and os.path.normcase(value) == rp:
            return True
        if tag == "rp" and identity is not None:
            try:
                unchanged = os.path.normcase(os.path.realpath(value)) == os.path.normcase(value)
            except (OSError, ValueError):
                unchanged = False
            if unchanged and _file_identity(value) == identity:
                return True
        if tag == "sid" and value in sids:
            return True
        if tag == "ref" and value in (path, session_id, colon_sid):
            return True
    return False


def _matches_file_spec(path, specs, base_dir=None, session_id=None):
    """True when `path` matches a file path or an exact identity in `specs`.

    Session ids, card refs and board identifiers are identities, not path prefixes: casing and every character are
    significant. Only real file paths use the platform's path-case rules.
    """
    if not specs:
        return False
    marked = [s for s in specs if isinstance(s, str) and s.startswith(RESOLVED_MARK)]
    if marked:
        if isinstance(path, str) and path and _matches_resolved(path, marked, base_dir, session_id):
            return True
        specs = [s for s in specs if not (isinstance(s, str) and s.startswith(RESOLVED_MARK))]
        if not specs:
            return False
    if not isinstance(path, str) or not path:
        return False
    name = os.path.basename(path)
    stem = name[:-6] if name.lower().endswith(".jsonl") else name
    rel = os.path.relpath(path, base_dir).replace("\\", "/") if base_dir else name
    m = _ROLLOUT_SID_RE.match(stem)
    rollout_sid = m.group(1) if m else ""
    colon_sid = path.rsplit(":", 1)[-1] if isinstance(path, str) and path.startswith("planboard:") else ""
    sid = session_id or rollout_sid or colon_sid
    is_ref = path.startswith("planboard:")
    is_identity = is_ref or ("/" not in path.replace("\\", "/") and not path.lower().endswith(".jsonl"))
    if is_identity:
        identities = {path}
        if session_id:
            identities.add(session_id)
        if colon_sid:
            identities.add(colon_sid)
        return any(isinstance(raw, str) and raw.strip() in identities for raw in specs)
    target_forms = _canonical_path_forms(path, base_dir)
    candidate = path if os.path.isabs(path) or not base_dir else os.path.join(base_dir, path)
    identity = _file_identity(candidate)
    for raw in specs:
        if not isinstance(raw, str):
            continue
        pat = raw.strip().replace("\\", "/")
        if not pat:
            continue
        pat_norm = os.path.normpath(pat).replace("\\", "/")
        if pat_norm.startswith("./"):
            pat_norm = pat_norm[2:]
        if identity is not None and (pat_norm.lower().endswith(".jsonl") or "/" in pat_norm):
            candidates = [raw.strip()]
            if base_dir and not os.path.isabs(raw.strip()):
                candidates.append(os.path.join(base_dir, raw.strip()))
            if any(_file_identity(p) == identity for p in candidates):
                return True
        pat_forms = _pattern_path_forms(raw, base_dir)
        if target_forms & pat_forms:
            return True
        if pat_norm.lower().endswith(".jsonl") or "/" in pat_norm:
            if "/" not in pat_norm:
                pat_bname = os.path.basename(pat_norm)
                pat_stem = pat_bname[:-6] if pat_bname.lower().endswith(".jsonl") else pat_bname
                m_b = _ROLLOUT_SID_RE.match(pat_stem)
                if m_b and m_b.group(1) and m_b.group(1) in (sid, rollout_sid):
                    return True
            continue
        m_pat = _ROLLOUT_SID_RE.match(pat_norm)
        pat_sid = m_pat.group(1) if m_pat else ""
        if pat_norm in (name, stem, rel) or os.path.normcase(pat_norm) in (os.path.normcase(name), os.path.normcase(stem)):
            return True
        if pat_sid and pat_sid in (name, stem, sid, rollout_sid):
            return True
        if sid and sid == pat_norm:
            return True
        if rollout_sid and rollout_sid == pat_norm:
            return True
        # A session id typed as a prefix (at least 4 characters, case kept) matches a transcript FILE on every call.
        # A resolved entry never reaches this branch: its identities are marked and compared exactly. An entry that
        # has not resolved yet (still its typed text) keeps excluding by that text until run() freezes it, so the
        # hook and the Kimi/Antigravity matchers, which match without a resolution pass, never load a session that
        # was just excluded (kxxx). It errs toward excluding: a later session sharing the prefix stays out
        # until run() resolves the entry, and run() refuses a prefix that reaches two files as ambiguous. Board ids
        # and planboard refs never get here: they are exact identities above.
        if len(pat_norm) >= 4 and ((sid and sid.startswith(pat_norm)) or (rollout_sid and rollout_sid.startswith(pat_norm))):
            return True
    return False


def _prompt_record_text(o):
    """A real human prompt's text, or None.

    The session marker rule and both foreign-marker scans use this exact definition.  A queued command is a prompt
    only when it is the same human ``queued_command`` shape cards_from_record turns into a user prompt; summaries,
    meta records and sidechains never count, even when they quote a marker.
    """
    if not isinstance(o, dict) or o.get("isSidechain"):
        return None
    if o.get("type") == "attachment":
        att = o.get("attachment") if isinstance(o.get("attachment"), dict) else {}
        origin = att.get("origin") if isinstance(att.get("origin"), dict) else None
        mode = att.get("commandMode")
        old_shape = origin is None and mode == "prompt"
        if (att.get("type") != "queued_command" or mode not in (None, "prompt")
                or (not old_shape and (origin is None or origin.get("kind") != "human"))):
            return None
        text = att.get("prompt")
        is_summary = bool(att.get("isMeta") or o.get("isMeta") or o.get("isCompactSummary"))
    elif o.get("type") == "user":
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        text, _ = _typed_text(msg.get("content"))
        is_summary = bool(o.get("isMeta") or o.get("isCompactSummary"))
    else:
        return None
    if not isinstance(text, str) or is_summary or text.lstrip().startswith("This session is being continued"):
        return None
    return text


def _marker_in_user_prompt(line, marker):
    """True when a real human prompt in this JSONL line holds `marker`."""
    try:
        return marker.decode("utf-8", "replace") in (_prompt_record_text(json.loads(line.decode("utf-8", "replace"))) or "")
    except Exception:
        return False


def _marker_names_in_record(o):
    """(base, marker-name iterator) for one JSONL record: the shared foreign-scan primitive of this worker and of
    load_hook. base 0 is a real prompt (_prompt_record_text: typed or queued), base 2 a compaction summary,
    continuation recap or meta user record, which decides only when no real prompt carries a marker
    (_foreign_by_precedence); (None, ()) for anything else."""
    text = _prompt_record_text(o)
    if text is not None:
        return 0, _MARKER_NAME.finditer(text)
    if isinstance(o, dict) and o.get("type") == "user" and not o.get("isSidechain"):
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        text, _ = _typed_text(msg.get("content"))
        if isinstance(text, str) and _is_summary_record(o, text):
            return 2, _MARKER_NAME.finditer(text)
    return None, ()


def _is_summary_record(o, text):
    """A compaction summary or a meta record: user-typed, but written by the harness, not typed at the prompt."""
    # Peer-delivered meta text can quote another seat; it is not harness provenance.
    if isinstance(o.get("origin"), dict) and o["origin"].get("kind") == "peer":
        return False
    return bool(o.get("isCompactSummary") or o.get("isMeta")) or text.lstrip().startswith(
        "This session is being continued")


def _foreign_by_precedence(seen):
    """Real prompts decide when any of them carries a marker; summaries decide only when none does. A summary
    that merely mentions this seat's marker cannot outvote another seat's real prompt (#374, Codex P2), and a
    continuation transcript whose only markers are in its summary is still judged by them."""
    real_own, real_foreign, sum_own, sum_foreign = seen
    if real_own or real_foreign:
        return real_foreign and not real_own
    return sum_foreign and not sum_own


def _has_foreign_seat_marker(path, own, markers):
    """True when `path` holds no session-start marker of this seat (`markers` or `own`), and holds another seat's
    `echo <other>-shell-ok` marker in a user record. Compaction summaries count only when no real prompt carries
    a marker (_foreign_by_precedence)."""
    seen = [False, False, False, False]    # real own, real foreign, summary own, summary foreign
    try:
        with open(path, "rb") as f:
            for line in f:
                if any(m in line and _marker_in_user_prompt(line, m) for m in markers):
                    return False
                if b"-shell-ok" not in line.lower():
                    continue
                try:
                    o = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                base, hits = _marker_names_in_record(o)
                if base is None:
                    continue
                for hit in hits:
                    seen[base + (0 if hit.group(1).lower() in own else 1)] = True
    except OSError:
        return False
    return _foreign_by_precedence(seen)


def _claude_session_ids(path):
    """Distinct top-level `sessionId` / `session_id` strings recorded in a Claude JSONL transcript."""
    sids = []
    seen = set()
    try:
        with open(path, "rb") as f:
            for line in f:
                if b"sessionId" not in line and b"session_id" not in line:
                    continue
                try:
                    o = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                sid = o.get("sessionId") or o.get("session_id")
                if isinstance(sid, str):
                    sid = sid.strip()
                    if sid and sid not in seen:
                        seen.add(sid)
                        sids.append(sid)
    except OSError:
        return ()
    return tuple(sids)


def select_files(project_dir, seat, all_files=False, cache=None, extra_markers=(), exclude_files=(), own=None,
                 sids_cache=None, single_seat=False):
    """Top-level *.jsonl transcripts of this seat: those whose own user prompts hold the seat's session-start
    marker, or any extra marker the caller names for sessions older than that marker. With `single_seat` (the
    folder is declared this seat's alone), also every other transcript there that holds no OTHER seat's marker
    and is a regular file whose real parent is the folder (no link out). That containment covers only this
    unmarked fallback: a file holding the seat's own marker is admitted by the marker rule as before:
    most of a seat's transcripts carry no marker at all (a resumed or continued session; 8 of kxxx's 10 newest
    on HQD, 23 Oct 2026), so without a hook naming each one as a --seat-file they were never read."""
    cache = cache if cache is not None else {}
    own_set = own_names(seat, (), extra_markers) if own is None else own
    markers = [("echo %s-shell-ok" % seat).encode()] + [m.encode() for m in extra_markers if m]
    for s_name in sorted(own_set):
        cand_m = ("echo %s-shell-ok" % s_name).encode()
        if cand_m not in markers:
            markers.append(cand_m)
    picked = []
    for name in sorted(os.listdir(project_dir)):
        path = os.path.join(project_dir, name)
        if not (os.path.isfile(path) and name.lower().endswith(".jsonl")):
            continue
        if _FORBIDDEN_NAME.search(name):
            continue
        if _matches_file_spec(path, exclude_files, base_dir=project_dir):
            cache.pop(name, None)
            continue
        if exclude_files:
            norm_p = os.path.normcase(os.path.abspath(path))
            if sids_cache is not None and norm_p in sids_cache:
                csids = sids_cache[norm_p]
            else:
                csids = _claude_session_ids(path)
                if sids_cache is not None:
                    sids_cache[norm_p] = csids
            if any(_matches_file_spec(path, exclude_files, base_dir=project_dir, session_id=csid) for csid in csids):
                cache.pop(name, None)
                continue
        if all_files:
            picked.append(path)
            continue
        contained = not os.path.islink(path) and os.path.dirname(os.path.realpath(path)) == os.path.realpath(project_dir)
        if cache.get(name) and contained and (single_seat or cache[name] != "single-seat"):
            if _has_foreign_seat_marker(path, own_set, markers):
                cache.pop(name, None)
                continue
            picked.append(path)
            continue
        if cache.get(name):
            # Admitted before but no longer contained (replaced by a link out of the folder): only its own
            # marker can admit it now (pxx N4).
            cache.pop(name, None)
        found = False
        with open(path, "rb") as f:
            for line in f:
                if any(_marker_in_user_prompt(line, m) for m in markers):
                    found = True
                    break
        if not found and single_seat and contained and not _has_foreign_seat_marker(path, own_set, markers):
            found = "single-seat"
        if found:
            # Preserve why an unmarked transcript was admitted: dropping the declaration revokes it.
            cache[name] = found
            picked.append(path)
    return picked


#: seat_file_problem's answer for a name that is simply not there (any more). main() skips such a pin with a
#: warning instead of refusing the run: Claude Code housekeeping deletes old transcripts, and one vanished pin
#: must not stop every other transcript of the seat (the HZ lost 12 h of cards that way on 22 Oct 2026).
SEAT_FILE_MISSING = "no file of exactly that name in --project-dir"


def seat_file_problem(project_dir, name):
    """Why `name` cannot be a --seat-file of `project_dir`, or None when it can: it must be the exact name of a
    top-level *.jsonl regular file there, as os.listdir spells it (select_files keys its cache by that name, so a
    name differing only in case on Windows would be recorded and never match), and not one select_files skips."""
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name or os.sep in name:
        return "a bare file name in --project-dir is wanted, not a path"
    if not name.lower().endswith(".jsonl"):
        return "not a .jsonl transcript"
    if _FORBIDDEN_NAME.search(name):
        return "the name looks like state or key material"
    try:
        listed = os.listdir(project_dir)
    except OSError as exc:
        return "--project-dir cannot be listed (%s)" % exc.__class__.__name__
    if name not in listed:
        return SEAT_FILE_MISSING
    path = os.path.join(project_dir, name)
    if os.path.islink(path) or not os.path.isfile(path):
        return "not a regular file"
    return None


def _first_record(path):
    try:
        with open(path, "rb") as f:
            o = json.loads(f.readline().decode("utf-8", "replace"))
        return o if isinstance(o, dict) else {}
    except (OSError, ValueError):
        return {}


def _codex_state(path):
    """The per-file state codex_cards needs when this file is a Codex rollout file, or None for any other file.
    A rollout file opens with a session_meta record; nothing else does."""
    first = _first_record(path)
    if first.get("type") != "session_meta":
        return None
    payload = first.get("payload") if isinstance(first.get("payload"), dict) else {}
    session = payload.get("id") or payload.get("session_id")
    return {"session": session if isinstance(session, str) else "", "item": None}


def _codex_interactive(path):
    """True for a session a person sat in. `codex exec` runs and subagent threads are prompts a seat wrote
    (source "exec", or a source that is not a plain word), and a file whose originator is Claude Code is a copy
    of a Claude Code session, whose cards come from the Claude transcript."""
    first = _first_record(path)
    payload = first.get("payload") if isinstance(first.get("payload"), dict) else {}
    source = payload.get("source")
    return (first.get("type") == "session_meta" and isinstance(source, str) and source != "exec"
            and payload.get("originator") != "Claude Code")


#: The public name for a caller that enrolls one file itself (the capture queue): whether a Codex rollout file
#: is a session a person sat in, the same test select_codex_files applies.
codex_interactive = _codex_interactive


_DRIVE = re.compile(r"^[A-Za-z]:")
_UNC = re.compile(r"^(?:\\\\|//[^/])")
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")


def _norm_dir(path):
    """A folder path with / separators and no trailing separator (except "/" itself). Case is folded for a Windows
    path (one that starts with a drive letter or a UNC share \\\\host\\share), so C:\\Projects\\x matches
    c:/projects/x and \\\\h\\S matches \\\\h\\s, while on a case-sensitive POSIX filesystem /home/a/Proj and
    /home/a/proj stay two folders (memory-bxxxxx's review of #271; #321): a mismatch then lands in codex_unmapped,
    never in this seat's cards."""
    if not isinstance(path, str):
        return ""
    if _UNC.match(path):
        body = path.replace("\\", "/").lstrip("/").rstrip("/")
        return ("//" + body).lower() if body else ""
    p = path.replace("\\", "/")
    if not p:
        return ""
    p = p.rstrip("/") or ("/" if p.startswith("/") else "")
    return p.lower() if _DRIVE.match(p) else p


def codex_cwd(path):
    """The working folder a Codex session recorded in its session_meta record, or "" when it records none."""
    first = _first_record(path)
    payload = first.get("payload") if isinstance(first.get("payload"), dict) else {}
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) else ""


def codex_subagent_parent(path):
    """Return (parent_thread_id, child_session_id) when `path` is a Codex subagent thread rollout, or None."""
    first = _first_record(path)
    if first.get("type") != "session_meta":
        return None
    payload = first.get("payload") if isinstance(first.get("payload"), dict) else {}
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn")
    if not isinstance(spawn, dict):
        return None
    parent_id = spawn.get("parent_thread_id")
    if not isinstance(parent_id, str) or not parent_id:
        return None
    child_id = payload.get("id") or payload.get("session_id")
    if not isinstance(child_id, str) or not child_id:
        stem = os.path.basename(path)[:-6] if os.path.basename(path).lower().endswith(".jsonl") else os.path.basename(path)
        m = _ROLLOUT_SID_RE.match(stem)
        child_id = m.group(1) if m else os.path.basename(path)
    return parent_id, child_id


def cwd_maps(cwd, prefixes):
    """True when `cwd` is one of `prefixes` or a folder under one of them."""
    c = _norm_dir(cwd)
    if not c:
        return False
    for pre in prefixes:
        q = _norm_dir(pre)
        if not q:
            continue
        if q == "/":
            if c.startswith("/") and not c.startswith("//"):
                return True
        elif c == q or c.startswith(q + "/"):
            return True
    return False


def _codex_rollout_names(sessions_dir):
    """Every rollout file name under a Codex home, at any depth, selected or not."""
    names = set()
    for _, _, files in os.walk(sessions_dir):
        names.update(n for n in files if n.startswith("rollout-") and n.lower().endswith(".jsonl"))
    return names


def _gone_or_directory(exc, path):
    """Whether an OSError reading a selected transcript means it vanished: deleted, or replaced by a directory.
    Opening a directory raises IsADirectoryError on macOS and Linux but PermissionError on Windows (HQB, HQD), so a
    PermissionError counts only when the path really is a directory now; any other permission error still raises."""
    if isinstance(exc, (FileNotFoundError, IsADirectoryError)):
        return True
    return isinstance(exc, PermissionError) and os.path.isdir(path)


def _missing_still_on_disk(prev_keys, paths, base_dir, vanished, excluded=lambda _p: False):
    """True when a previously ingested wire or transcript file is missing from today's selection but still on disk
    (the selection changed, so the store resets). Missing files gone from disk go to `vanished` and keep their
    cards. Kimi and Antigravity key the cursor by the file's path, the form `paths` holds."""
    selected = set(paths)
    changed = False
    for ck in sorted(prev_keys):
        if ck in selected:
            continue
        if excluded(ck):
            continue
        if os.path.isfile(ck):
            changed = True
        else:
            try:
                vanished.append(os.path.relpath(ck, base_dir))
            except ValueError:
                vanished.append(ck)
    return changed


def select_codex_files(sessions_dir, cache=None, cwds=None, cwd_cache=None, unmapped=None,
                       session_files=(), exclude_files=(), subagent_cache=None, subagents_by_parent=None,
                       unmapped_meta=None, pin_only=False):
    """(rollout files of interactive sessions, number of rollout files seen) under a Codex home's sessions
    folder, at any depth. A file's first record never changes, so the answer for a name is kept in `cache`.

    A Codex home is not a seat: one home serves several seats and the Codex runs a seat starts (kxxx, HQD, 12 Oct
    2026). With `cwds` given, an interactive session is this seat's only when the working folder its
    session_meta records is one of `cwds` or under one (or when the file is explicitly pinned in `session_files`);
    any other is skipped and its name and cwd are appended to `unmapped`, never guessed. When `session_files` is
    given without `cwds` (or `pin_only=True`), only the pinned session files are picked and other interactive
    sessions are listed in `unmapped`. cwds=None, `pin_only=False` and no `session_files` keeps every interactive
    session (the caller has said the home is one seat's). The recorded cwd is kept in `cwd_cache` by name, like the
    interactive answer."""
    cache = cache if cache is not None else {}
    cwd_cache = cwd_cache if cwd_cache is not None else {}
    sub_cache = subagent_cache if subagent_cache is not None else {}
    picked, seen = [], 0
    for folder, _, names in sorted(os.walk(sessions_dir)):
        for name in sorted(names):
            if not (name.startswith("rollout-") and name.lower().endswith(".jsonl")) or _FORBIDDEN_NAME.search(name):
                continue
            seen += 1
            path = os.path.join(folder, name)
            sid = None
            if exclude_files:
                if _matches_file_spec(path, exclude_files, base_dir=sessions_dir):
                    continue
                sid = (_codex_state(path) or {}).get("session")
                if sid and _matches_file_spec(path, exclude_files, base_dir=sessions_dir, session_id=sid):
                    continue
            if subagent_cache is not None or subagents_by_parent is not None:
                if name not in sub_cache:
                    sub_cache[name] = codex_subagent_parent(path)
                sub_info = sub_cache[name]
                if sub_info is not None and subagents_by_parent is not None:
                    parent_id, child_id = sub_info
                    subagents_by_parent.setdefault(parent_id, []).append(child_id)
            if name not in cache:
                cache[name] = _codex_interactive(path)
            if not cache[name]:
                continue
            pinned = False
            if session_files:
                if _matches_file_spec(path, session_files, base_dir=sessions_dir):
                    pinned = True
                else:
                    if sid is None:
                        sid = (_codex_state(path) or {}).get("session")
                    if sid and _matches_file_spec(path, session_files, base_dir=sessions_dir, session_id=sid):
                        pinned = True
            if not pinned:
                if cwds is not None:
                    if name not in cwd_cache:
                        cwd_cache[name] = codex_cwd(path)
                    if not cwd_maps(cwd_cache[name], cwds):
                        if unmapped is not None:
                            unmapped.append({"file": name, "cwd": cwd_cache[name] or None})
                        if unmapped_meta is not None:
                            unmapped_meta.append({"file": name, "path": path, "cwd": cwd_cache[name] or None})
                        continue
                elif session_files or pin_only:
                    if name not in cwd_cache:
                        cwd_cache[name] = codex_cwd(path)
                    if unmapped is not None:
                        unmapped.append({"file": name, "cwd": cwd_cache[name] or None})
                    if unmapped_meta is not None:
                        unmapped_meta.append({"file": name, "path": path, "cwd": cwd_cache[name] or None})
                    continue
            picked.append(path)
    if subagents_by_parent is not None:
        for pid in list(subagents_by_parent):
            subagents_by_parent[pid] = sorted(set(subagents_by_parent[pid]))
    return picked, seen


# --------------------------------------------------------------------------- one file, for a caller's queue
#
# read_file is the loader's one parsing entry point: run() below is a loop over it, so its tests and the attack
# file hold it too. A caller with its own bounded reader and its own transaction (the capture queue of #194) calls
# it per window and commits the returned checkpoint together with the cards, or neither. It never opens a file,
# never decides a file was replaced, and writes nothing.

#: The checkpoint's own schema. A checkpoint of another schema, loader version or id rule is refused
#: (CheckpointIncompatible): the caller starts that file again from offset 0 as a new generation, never this call.
CHECKPOINT_SCHEMA = 1
#: The most prefilled asks one file's checkpoint may hold. Past it read_file refuses (PendingStateFull), which is
#: backpressure: nothing is returned, nothing is committed, and an ask is never forgotten because it is old.
MAX_PENDING_PREFILLED = 1024
DIALECTS = ("claude", "codex", "kimi", "antigravity")


class CheckpointIncompatible(ValueError):
    """The checkpoint is not this loader's: another schema, loader_version or id_rule, or no known dialect."""


class PendingStateFull(ValueError):
    """More prefilled asks are pending than the caller allows: nothing is returned to commit."""


def enroll(path, kimi=True, antigravity=True, *, original_path=None):
    """(dialect, meta) of one transcript file, read once from its first record and its path. Claude Code needs
    no file metadata, since every record names its session. A Codex rollout's session is its session_meta record,
    a Kimi wire file's session and subagent mark come from its path, and an Antigravity transcript file's
    session and subagent mark come from its path and first record. The caller keeps meta in the checkpoint
    and never derives it again from a window in the middle of the file.

    A caller reading a bounded frozen Kimi first-record copy passes original_path
    for path-derived identity. That path is not opened. With no override the
    existing live-file behavior is unchanged; Codex/Claude ignore the override."""
    state = antigravity_dialect.file_state(path) if antigravity else None
    if state is not None:
        return "antigravity", {"session": state["session"], "subagent": state["subagent"]}
    state = kimi_dialect.file_state(path, original_path=original_path) if kimi else None
    if state is not None:
        return "kimi", {"session": state["session"], "subagent": state["subagent"]}
    state = _codex_state(path)
    if state is not None:
        return "codex", {"session": state["session"]}
    return "claude", {}


def new_checkpoint(dialect, meta):
    """The checkpoint of a file not read yet, or of a file whose generation changed (replaced, truncated or no
    longer the enrolled file): every pending ask of the old generation is gone with it."""
    if dialect not in DIALECTS:
        raise CheckpointIncompatible(f"unknown dialect {dialect!r}")
    return {"schema": CHECKPOINT_SCHEMA, "loader_version": LOADER_VERSION, "id_rule": ID_RULE,
            "dialect": dialect, "meta": dict(meta), "offset": 0, "prefilled_asks": [], "open_asks": {}}


def _check_compatible(cp):
    if not isinstance(cp, dict):
        raise CheckpointIncompatible("the checkpoint is not an object")
    found = (cp.get("schema"), cp.get("loader_version"), cp.get("id_rule"))
    if found != (CHECKPOINT_SCHEMA, LOADER_VERSION, ID_RULE):
        raise CheckpointIncompatible(f"checkpoint {found} is not this loader's "
                                     f"{(CHECKPOINT_SCHEMA, LOADER_VERSION, ID_RULE)}: rebuild the file")
    if cp.get("dialect") not in DIALECTS or not isinstance(cp.get("meta"), dict):
        raise CheckpointIncompatible("the checkpoint names no known dialect")
    if type(cp.get("offset")) is not int or cp["offset"] < 0:
        raise CheckpointIncompatible("the checkpoint's offset is not a byte offset")
    fallback = cp.get("codex_fallback_last")
    if fallback is not None and (not isinstance(fallback, list) or len(fallback) != 2
            or not isinstance(fallback[0], str) or type(fallback[1]) is not int or fallback[1] < 1):
        raise CheckpointIncompatible("invalid Codex fallback naming checkpoint")


def _answered_ids(o):
    """The tool_use ids a user record answers (its tool_result blocks)."""
    msg = o.get("message") if isinstance(o, dict) and isinstance(o.get("message"), dict) else {}
    content = msg.get("content") if isinstance(msg.get("content"), list) else []
    return {b.get("tool_use_id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)}


def read_file(records, checkpoint, *, seat, seats=KNOWN_SEATS, own=None, max_pending=MAX_PENDING_PREFILLED,
              prune_answered=True):
    """Cards from one window of one transcript file, and the checkpoint to commit with them.

    `records` are (offset, bytes) pairs: complete lines WITH their newline, at their ORIGINAL byte offsets, in
    order, the first at checkpoint["offset"]. A bounded reader yields these (tools/transcript_cards/native_window.py);
    run() yields them from its own file. Returns (raws, stats, checkpoint): raws are what run() makes cards of,
    stats counts lines_read, bad_lines and dropped_by_class, and checkpoint is a NEW object (the one passed in is
    never changed, so a refused or abandoned call has nothing to undo).

    The checkpoint holds every piece of state that crosses a window: the offset to resume at (a Codex user item
    whose event is not read yet is read again, so the offset stays at that item), the prefilled asks of a Claude
    Code file whose result is not read yet (prune_answered removes an id once its result is read), and the open
    asks of a Kimi wire file. Nothing is kept anywhere else.

    Raises CheckpointIncompatible for another schema, loader version or id rule, PendingStateFull when more than
    max_pending prefilled asks would be pending (None: no bound), and ValueError for a record that is out of order
    or has no newline."""
    _check_compatible(checkpoint)
    cp = json.loads(json.dumps(checkpoint))
    dialect, meta = cp["dialect"], cp["meta"]
    kimi = ({"session": meta.get("session", ""), "subagent": bool(meta.get("subagent")),
             "asks": cp.get("open_asks") or {}} if dialect == "kimi" else None)
    antigravity = ({"session": meta.get("session", ""), "subagent": bool(meta.get("subagent")),
                    "asks": cp.get("open_asks") or {}, "refs": cp.get("ag_refs") or {}}
                   if dialect == "antigravity" else None)
    codex = {"session": meta.get("session", ""), "item": None,
             "fallback_last": cp.get("codex_fallback_last")} if dialect == "codex" else None
    prefilled = set(cp.get("prefilled_asks") or ())
    stats = {"lines_read": 0, "bad_lines": 0, "dropped_by_class": {}}
    out, offset, held = [], cp["offset"], None
    for line_start, line in records:
        if line_start != offset:
            raise ValueError(f"a record at offset {line_start} where {offset} was expected")
        if not isinstance(line, (bytes, bytearray)) or not line.endswith(b"\n"):
            raise ValueError(f"the record at offset {line_start} is not a complete line")
        offset += len(line)
        stats["lines_read"] += 1
        try:
            o = json.loads(bytes(line).decode("utf-8", "replace"))
        except Exception:
            stats["bad_lines"] += 1
            if codex is not None:
                codex["item"], held = None, None
            continue
        if antigravity is not None:
            raws, drop_class = _antigravity_raws(o, antigravity, seat, seats, own)
        elif kimi is not None:
            raws, drop_class = _kimi_raws(o, kimi, seat, seats, own)
        elif codex is not None:
            raws, drop_class = codex_cards(o, seat, codex, seats, own)
            # A user item that ends a window is read again next time, a card already or not. Its event can
            # be written a moment later (34 of 531 pairs on one machine are more than 1 ms apart), and
            # an event read with no item before it would be named by session and time: a second card
            # for one prompt (memory-bxxxxx on 1.16).
            held = line_start if codex["item"] is not None else None
        else:
            if isinstance(o, dict) and o.get("type") == "assistant":
                prefilled.update(prefilled_question_ids(o))      # the call comes before its result
            raws, drop_class = cards_from_record(o, seat, prefilled, seats, own)
            if prune_answered and isinstance(o, dict) and o.get("type") == "user":
                prefilled -= _answered_ids(o)                     # its result is read: the ask is settled
        if raws and not raws[0]["date"]:
            # No time with a zone: the card could not be ordered against another, and the server refuses it.
            raws, drop_class = [], "no_timestamp"
        for cls in ([drop_class] if isinstance(drop_class, str) else (drop_class or [])):
            stats["dropped_by_class"][cls] = stats["dropped_by_class"].get(cls, 0) + 1
        for raw in raws:
            raw["dialect"] = dialect                   # attribution.extraction names the dialect the card came from
        out.extend(raws)
    if max_pending is not None and len(prefilled) > max_pending:
        raise PendingStateFull(f"{len(prefilled)} prefilled asks pending, more than {max_pending}")
    cp["offset"] = held if held is not None else offset
    cp["prefilled_asks"] = sorted(prefilled)
    cp["open_asks"] = antigravity["asks"] if antigravity is not None else kimi["asks"] if kimi is not None else {}
    if codex is not None:
        cp["codex_fallback_last"] = list(codex["fallback_last"]) if codex.get("fallback_last") else None
    if antigravity is not None:
        cp["ag_refs"] = antigravity["refs"]          # the step_index refs seen, so a reused index stays a new card
    return out, stats, cp


def approved_cards(raws, seat, file_name, disabled_detectors=()):
    """(cards, drops) for a caller that stores what read_file returned: every raw put through the SAME redaction
    detectors run() uses, so nothing unscreened is ever handed on. A raw whose kept fields trip a detector is a
    drop row by id and detector name, never by text, and no card; the rest are cards as run() builds them, keyed by
    id. Two raws of one id in one window keep the earlier one (by record time, then content hash) and list the other
    content's hash beside it, as run() does across files. Unlike run(), which keeps an existing clean card when a
    later variant trips a detector, a detector hit on ANY content of an id here drops the id for the window: a
    caller with no store to compare against fails closed."""
    cards, drops = {}, {}
    for raw in raws:
        source_sha = sha256_text(json.dumps(raw["kept"], sort_keys=True, ensure_ascii=False))
        cid, ref = _raw_identity(raw)
        fired = detect(_kept_strings(raw["kept"]), disabled_detectors, seat)
        if fired:
            drops[cid] = {"id": cid, "record_uuid": raw["uuid"], "block_index": raw["block_index"],
                          "type": raw["type"], "detectors": sorted(fired), "file": file_name}
            cards.pop(cid, None)
            continue
        if cid in drops:
            continue                                   # another content of this record tripped a detector
        existing = cards.get(cid)
        if existing is not None:
            if existing["source"]["source_sha256"] == source_sha:
                continue
            if (existing["time"], existing["source"]["source_sha256"]) <= (raw["date"], source_sha):
                _note_variant(existing, source_sha, raw["session"], file_name)
                continue
            card = _card_from_raw(raw, seat, cid, ref, source_sha, [file_name])
            _note_variant(card, existing["source"]["source_sha256"], "", "")
            cards[cid] = card
            continue
        cards[cid] = _card_from_raw(raw, seat, cid, ref, source_sha, [file_name])
    return cards, drops


def _lines_from(f, start):
    """(offset, line) for every complete line of an open file from `start`; a partial last line is not yielded."""
    f.seek(start)
    offset = start
    while True:
        line = f.readline()
        if not line or not line.endswith(b"\n"):
            return                                                 # a partial last line waits for the next run
        yield offset, line
        offset += len(line)


#: Openings that only a machine writes, on every seat, so they are never the operator's words. Codex puts its own
#: injected context in front of a session as a user turn (oxxxxxx's prefix check, 13 Oct: it opened 15 of the
#: admin seat's "operator" Candidates and 20 of the builder's).
BUILTIN_MACHINE_PREFIXES = ("<codex_internal_context source",)


def machine_prefix_of(raw, prefixes):
    """The listed opening a typed Candidate starts with, or None. A seat's launcher, wake job or relay bridge types
    its own text into the session (CODEX_KXXX_MESH_WAKEUP on codex-kxxx-cli, "You are antigravity, an AI node on
    the AgentMeshy mesh." on antigravity), and no fixed template list can know every seat's: the seat names them
    with --machine-prefix. The match is on the prompt's first characters after leading blanks, case-sensitive, so
    "You are ANTIGRAVITY, the reasoning node ..." that the operator pasted is not caught by the bridge's opening.
    Only a Candidate is judged: a Question or Ruling comes from a tool call, not from typed text."""
    if raw.get("type") != "Candidate":
        return None
    kept = raw.get("kept") if isinstance(raw.get("kept"), dict) else {}
    text = kept.get("text") if isinstance(kept.get("text"), str) else ""
    opening = text.lstrip()
    for p in prefixes:
        if opening.startswith(p):
            return p
    return None


def _context_write_ok(res):
    """Return (ok: bool, version, error_code) from a card client write report or result dict."""
    if not isinstance(res, dict):
        return bool(res), None, None if res else "write_failed"
    outcome = res.get("outcome")
    if outcome in ("written", "exists"):
        return True, res.get("version"), None
    if outcome is not None:
        return False, None, res.get("error_code") or outcome
    if res.get("written") is True or res.get("status") == "completed":
        return True, res.get("version") or (res.get("result") or {}).get("version"), None
    return False, None, res.get("error_code") or res.get("status") or "write_failed"


def record_context_write_result(out_dir, write_report):
    """Apply the server's write outcome for the emitted Context card in `out_dir`.

    If the server accepted the Context card (`written` or `exists`), clears `context_prev_head`.
    If the server refused it (e.g. `E_CARD_SCHEMA`), restores the previous Context head in `cards.jsonl`
    and `cursor.json` so the next emit supersedes the last accepted head (#297, kxxx msg/<key>).
    """
    cards_path = os.path.join(out_dir, "cards.jsonl")
    cursor_path = os.path.join(out_dir, "cursor.json")
    report_path = os.path.join(out_dir, "report.json")
    if not os.path.exists(cursor_path):
        return {"status": "no_cursor"}
    with open(cursor_path, "r", encoding="utf-8") as f:
        cursor = json.load(f)
    ok, version, err_code = _context_write_ok(write_report)
    if ok:
        cursor.pop("context_prev_head", None)
        _write_atomic(cursor_path, json.dumps(cursor, sort_keys=True, indent=1))
        return {"status": "confirmed", "id": cursor.get("context_head_id"), "version": version}

    prev = cursor.pop("context_prev_head", None) or {}
    cards = {c["id"]: c for c in _load_jsonl(cards_path)}
    for cid in [k for k, v in cards.items() if v.get("type") == "Context"]:
        cards.pop(cid, None)
    prev_card = prev.get("card")
    if isinstance(prev_card, dict) and prev_card.get("id"):
        cards[prev_card["id"]] = prev_card
        cursor["context_head_id"] = prev.get("id")
        cursor["context_head_sha256"] = prev.get("sha256")
        cursor["context_head_time"] = prev.get("time")
    else:
        cursor.pop("context_head_id", None)
        cursor.pop("context_head_sha256", None)
        cursor.pop("context_head_time", None)
    ordered = sorted(cards.values(), key=lambda c: (c["time"], c["id"]))
    _write_atomic(cards_path, "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in ordered))
    _write_atomic(cursor_path, json.dumps(cursor, sort_keys=True, indent=1))
    if os.path.exists(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                rep = json.load(f)
            if isinstance(rep.get("context_card"), dict):
                rep["context_card"]["status"] = "refused"
                rep["context_card"]["error_code"] = err_code
                _write_atomic(report_path, json.dumps(rep, sort_keys=True, indent=1))
        except (OSError, ValueError):
            pass
    return {"status": "refused", "error_code": err_code, "restored_head_id": cursor.get("context_head_id")}


def run(project_dir, seat, out_dir, files=None, all_files=False, disabled_detectors=(), rebuild=False,
        extra_markers=(), extra_seats=(), extra_own=(), codex_dir=None, kimi_dir=None, antigravity_dir=None,
        seat_files=(), codex_cwds=None, codex_session_files=(), exclude_files=(), machine_prefixes=(),
        emit_context=False, write_context_fn=None, spool_pending=False, single_seat_dir=False):
    """Read new transcript lines, update the card store in out_dir, return the run report.

    `seat_files`: top-level transcript names in project_dir that are this seat's though no prompt in them holds the
    marker (a continuation transcript, named by the load hook). Each is recorded in the cursor's seat_files, the
    cache select_files already keeps for files it found by marker, so it is read now and on every later run, a
    --rebuild included. main() checks each name with seat_file_problem first.

    `project_dir` is a Claude Code project folder. For a Codex seat give `codex_dir` instead, the sessions folder
    of its Codex home: every interactive session under it is read (see select_codex_files), there is no marker;
    `codex_cwds` limits that to sessions whose recorded working folder is one of those folders or under one (plus any
    explicitly pinned `codex_session_files`), and the report's codex_unmapped lists the others with their cwd.
    `exclude_files` skips matching transcript files across any dialect and persists in `cursor["excluded_files"]`.
    For a Kimi seat give `kimi_dir`, its Kimi home's sessions folder: every agents/main wire.jsonl under it is
    read (kimi_dialect.select_files).
    For an Antigravity seat give `antigravity_dir`, its Antigravity CLI home's brain folder: every
    conversation's .system_generated/logs/transcript.jsonl under it is read (antigravity_dialect.select_files).
    A file is read as Codex when it opens with a session_meta record, whichever way it was named.

    rebuild=True ignores the stored cards, drops and cursor and writes the store again from a full read. Use it
    after a loader upgrade that changes what counts as a card: an ordinary run only adds, merges and re-screens,
    so a card made under an older rule would otherwise stay."""
    os.makedirs(out_dir, exist_ok=True)
    if emit_context and (spool_pending or _env_wants_spool()):
        raise ValueError(
            "emit_context cannot be combined with spool_pending / WRITE=spool: Context cards require "
            "single-writer cursor.json ownership in the loader, not the postbox spool uploader"
        )
    cards_path = os.path.join(out_dir, "cards.jsonl")
    drops_path = os.path.join(out_dir, "drops.jsonl")
    cursor_path = os.path.join(out_dir, "cursor.json")
    # pxx N2 and N2-D: one absolute spelling of every root for every probe below.
    project_dir = os.path.abspath(project_dir) if project_dir else project_dir
    codex_dir = os.path.abspath(codex_dir) if codex_dir else codex_dir
    kimi_dir = os.path.abspath(kimi_dir) if kimi_dir else kimi_dir
    antigravity_dir = os.path.abspath(antigravity_dir) if antigravity_dir else antigravity_dir
    cursor = {"files": {}, "seat_files": {}}
    if os.path.exists(cursor_path):
        try:
            with open(cursor_path, "r", encoding="utf-8") as f:
                cursor = json.load(f)
        except (OSError, ValueError) as exc:
            raise PolicyUnreadable(cursor_path) from exc
        exclusion_policy(cursor, cursor_path)
    cards = {c["id"]: c for c in _load_jsonl(cards_path)}
    drops = {d["id"]: d for d in _load_jsonl(drops_path)}
    # A store written before version 1.4 keyed its cards by content as well as by record, and a Ruling's id changed
    # again in 1.7 (the rule in force is recorded in cursor.json). Merging into such a store would leave cards
    # twice, so it is rebuilt from a full read instead. A store whose cursor is gone is rebuilt as well: the
    # result is the same.
    store_rebuilt = bool(cards or drops) and (any(c.get("schema") != "card.v1" for c in cards.values())
                                              or cursor.get("id_rule") != ID_RULE)
    saved_ctx_id = cursor.get("context_head_id")
    saved_ctx_sha = cursor.get("context_head_sha256")
    saved_ctx_time = cursor.get("context_head_time")
    saved_ctx_card = cards.get(saved_ctx_id) if saved_ctx_id else None

    selection_reset = False

    def _reset_ingested_store(for_selection=False):
        nonlocal cards, drops, selection_reset
        if for_selection:
            selection_reset = True
        cards, drops = {}, {}
        cursor["files"] = {}
        cursor["prefilled_asks"] = []
        cursor.pop("kimi_open_asks", None)
        cursor.pop("antigravity_open_asks", None)
        cursor.pop("antigravity_refs", None)
        cursor.pop("doing_cursors", None)
        if (emit_context and isinstance(saved_ctx_card, dict) and saved_ctx_card.get("type") == "Context"
                and not detect(_card_strings(saved_ctx_card), disabled_detectors, seat)):
            cards[saved_ctx_id] = saved_ctx_card
            cursor["context_head_id"] = saved_ctx_id
            cursor["context_head_sha256"] = saved_ctx_sha
            cursor["context_head_time"] = saved_ctx_time

    if rebuild or store_rebuilt:
        preserved_policy = {
            k: cursor[k]
            for k in ("codex_seat_files", "codex_pin_only", "excluded_files")
            if k in cursor
        }
        cursor = {"files": {}, "seat_files": {}}
        cursor.update(preserved_policy)
        _reset_ingested_store()
    cursor["id_rule"] = ID_RULE
    seats = KNOWN_SEATS | frozenset(s.strip().lower() for s in extra_seats if isinstance(s, str) and s.strip())
    own = own_names(seat, extra_own, extra_markers)
    prefixes = tuple(BUILTIN_MACHINE_PREFIXES) + tuple(p.lstrip() for p in machine_prefixes if p and p.strip())

    had_codex_pins = bool(cursor.get("codex_seat_files")) or bool(cursor.get("codex_pin_only"))
    requested_codex_pins = any(isinstance(csf, str) and csf.strip() for csf in (codex_session_files or ()))

    excluded_policy = exclusion_policy(cursor, cursor_path)
    explicit_excludes = [ex.strip() for ex in (exclude_files or ()) if isinstance(ex, str) and ex.strip()]
    for ex in explicit_excludes:
        ex_map = cursor.setdefault("excluded_files", {})
        if not (isinstance(ex_map.get(ex), dict) and ex_map[ex].get("resolved")):
            ex_map[ex] = True      # resolved below, once, to the identities it matches
    excluded_policy = cursor.get("excluded_files", {})
    unresolved_excludes = [k for k, v in excluded_policy.items()
                            if isinstance(k, str) and k.strip() and not (
                                isinstance(v, dict) and v.get("resolved") and v.get("resolution") == "exact-v1")]
    # An entry frozen by an older head (no "exact-v1") is re-resolved below, but the upgrade only ADDS: while it
    # runs, its old realpaths and sessions stay enforced, so selection in this same pass cannot read a transcript
    # it excluded (GitHub Codex r4360873682, kxxx). Only its card_refs, where a prefix could have widened
    # the old policy, wait for the re-resolution.
    def _old_identities(v):
        rps = [x for x in (v.get("realpaths") or []) if isinstance(x, str) and x.strip()]
        sids = [x for x in (v.get("sessions") or []) if isinstance(x, str) and x.strip()]
        return {"resolved": True, "realpaths": rps, "sessions": sids, "card_refs": []} if (rps or sids) else None
    enforced_now = {}
    for k, v in excluded_policy.items():
        if not (isinstance(v, dict) and v.get("resolved")):
            continue
        if v.get("resolution") == "exact-v1":
            enforced_now[k] = v
        elif _old_identities(v) is not None:
            enforced_now[k] = _old_identities(v)
    all_excludes = exclusion_specs(enforced_now)
    base_dir_for_exclude = antigravity_dir or kimi_dir or codex_dir or project_dir
    if base_dir_for_exclude:
        # The base a relative card provenance path is read against, for the hook and root aggregation (R2).
        cursor["exclude_base"] = os.path.realpath(base_dir_for_exclude).replace("\\", "/")

    claude_sids_cache = {}
    path_excluded_cache = {}
    nested_by_name = None

    def _get_nested_by_name():
        nonlocal nested_by_name
        if nested_by_name is None:
            nested_by_name = {}
            if base_dir_for_exclude and os.path.isdir(base_dir_for_exclude) and (
                codex_dir or kimi_dir or antigravity_dir
            ):
                for folder, _, names in sorted(os.walk(base_dir_for_exclude)):
                    for n in sorted(names):
                        if n.lower().endswith(".jsonl"):
                            nested_by_name.setdefault(n, []).append(os.path.join(folder, n))
        return nested_by_name

    def _is_non_path_provenance(p):
        if not isinstance(p, str) or not p:
            return True
        return p == "approvals_row" or p.startswith("planboard:")

    def _resolve_candidate_paths(path):
        if _is_non_path_provenance(path):
            return []
        if os.path.isabs(path):
            return [path]
        cands = []
        if base_dir_for_exclude:
            joined = os.path.join(base_dir_for_exclude, path)
            if os.path.exists(joined):
                cands.append(joined)
        for fp in files or ():
            if fp:
                cur_dir = os.path.dirname(os.path.abspath(fp))
                for _ in range(5):
                    if not cur_dir:
                        break
                    joined = os.path.join(cur_dir, path)
                    if os.path.exists(joined) and joined not in cands:
                        cands.append(joined)
                    parent_dir = os.path.dirname(cur_dir)
                    if parent_dir == cur_dir:
                        break
                    cur_dir = parent_dir
        has_sep = "/" in path.replace("\\", "/")
        if not cands and (codex_dir or kimi_dir or antigravity_dir):
            norm_rel = os.path.normcase(os.path.normpath(path)).replace("\\", "/")
            for np in _get_nested_by_name().get(os.path.basename(path), ()):
                if has_sep:
                    np_norm = os.path.normcase(os.path.normpath(np)).replace("\\", "/")
                    if not (np_norm == norm_rel or np_norm.endswith("/" + norm_rel)):
                        continue
                if np not in cands:
                    cands.append(np)
        return cands or [path]

    def _matches_specs_for_path(path, specs):
        if not isinstance(path, str) or not path:
            return False
        if _matches_file_spec(path, specs, base_dir=base_dir_for_exclude):
            return True
        for cand_path in _resolve_candidate_paths(path):
            if _matches_file_spec(cand_path, specs, base_dir=base_dir_for_exclude):
                return True
            if antigravity_dir and (
                "/" in cand_path.replace("\\", "/") or cand_path.lower().endswith(".jsonl")
            ):
                sess = antigravity_dialect._session_from_path(cand_path)
                if antigravity_dialect._is_excluded(cand_path, antigravity_dir, sess, specs):
                    return True
            elif kimi_dir and (
                "/" in cand_path.replace("\\", "/") or cand_path.lower().endswith(".jsonl") or "session_" in cand_path
            ):
                sess = ""
                for comp in reversed(os.path.normpath(cand_path).split(os.sep)):
                    if comp.startswith("session_"):
                        sess = comp[len("session_"):]
                        break
                if kimi_dialect._is_excluded(cand_path, kimi_dir, sess, specs):
                    return True
            if os.path.isfile(cand_path):
                sid = (_codex_state(cand_path) or {}).get("session")
                if sid and _matches_file_spec(cand_path, specs, base_dir=base_dir_for_exclude, session_id=sid):
                    return True
                ag_st = antigravity_dialect.file_state(cand_path)
                if ag_st and ag_st.get("session") and _matches_file_spec(
                    cand_path, specs, base_dir=base_dir_for_exclude, session_id=ag_st["session"]
                ):
                    return True
                km_st = kimi_dialect.file_state(cand_path)
                if km_st and km_st.get("session") and _matches_file_spec(
                    cand_path, specs, base_dir=base_dir_for_exclude, session_id=km_st["session"]
                ):
                    return True
                norm_cp = os.path.normcase(os.path.abspath(cand_path))
                if norm_cp in claude_sids_cache:
                    csids = claude_sids_cache[norm_cp]
                else:
                    csids = _claude_session_ids(cand_path)
                    claude_sids_cache[norm_cp] = csids
                for csid in csids:
                    if _matches_file_spec(cand_path, specs, base_dir=base_dir_for_exclude, session_id=csid):
                        return True
        return False

    def _compute_path_excluded(path):
        if _matches_file_spec(path, all_excludes, base_dir=base_dir_for_exclude):
            return True
        for cand_path in _resolve_candidate_paths(path):
            if cand_path in path_excluded_cache:
                if path_excluded_cache[cand_path]:
                    return True
                continue
            cand_res = _matches_specs_for_path(cand_path, all_excludes)
            path_excluded_cache[cand_path] = cand_res
            if cand_res:
                return True
        return False

    def _is_path_excluded(path):
        if not all_excludes or not isinstance(path, str) or not path:
            return False
        if path in path_excluded_cache:
            return path_excluded_cache[path]
        res = _compute_path_excluded(path)
        path_excluded_cache[path] = res
        return res

    if explicit_excludes or unresolved_excludes:
        universe_paths = set()
        file_named = set(fp for fp in (files or ()) if isinstance(fp, str) and fp)
        if files is not None:
            for fp in files:
                if isinstance(fp, str) and fp:
                    universe_paths.add(fp)
        if base_dir_for_exclude and os.path.isdir(base_dir_for_exclude):
            if codex_dir or kimi_dir or antigravity_dir:
                for paths_list in _get_nested_by_name().values():
                    universe_paths.update(paths_list)
            else:
                for n in os.listdir(base_dir_for_exclude):
                    if n.lower().endswith(".jsonl"):
                        universe_paths.add(os.path.join(base_dir_for_exclude, n))
                        file_named.add(os.path.join(base_dir_for_exclude, n))
        for k_map in ("files", "seat_files", "codex_seat_files", "codex_files", "kimi_files", "antigravity_files"):
            for k in (cursor.get(k_map) or {}).keys():
                if isinstance(k, str) and k:
                    universe_paths.add(k)
        for sub_name, sub_info in (cursor.get("codex_subagents") or {}).items():
            if isinstance(sub_name, str) and sub_name:
                universe_paths.add(sub_name)
            if isinstance(sub_info, (list, tuple)) and len(sub_info) >= 2 and isinstance(sub_info[1], str):
                universe_paths.add(sub_info[1])
        for c in cards.values():
            c_src = c.get("source") if isinstance(c.get("source"), dict) else {}
            for cf in c_src.get("files") or ():
                if isinstance(cf, str) and cf:
                    universe_paths.add(cf)
            for cs in c_src.get("sessions") or ():
                if isinstance(cs, str) and cs:
                    universe_paths.add(cs)
        for d in drops.values():
            df = d.get("file")
            if isinstance(df, str) and df:
                universe_paths.add(df)
        pending_check = _pending_spool_path(out_dir)
        if os.path.isfile(pending_check):
            for pc in _load_jsonl(pending_check):
                if isinstance(pc, dict) and isinstance(pc.get("source"), dict):
                    for pf in pc["source"].get("files") or ():
                        if isinstance(pf, str) and pf:
                            universe_paths.add(pf)
                    for ps in pc["source"].get("sessions") or ():
                        if isinstance(ps, str) and ps:
                            universe_paths.add(ps)
        def _identity_of(up):
            """(realpaths, sessions) of a universe entry that is a real transcript now, else ((), ())."""
            cand = up if os.path.isabs(up) or not base_dir_for_exclude else os.path.join(base_dir_for_exclude, up)
            if not os.path.isfile(cand):
                return (), ()
            sids = set(_claude_session_ids(cand) or ())
            for st in ((_codex_state(cand) or {}), (kimi_dialect.file_state(cand) or {}),
                       (antigravity_dialect.file_state(cand) or {})):
                if isinstance(st, dict) and isinstance(st.get("session"), str) and st.get("session"):
                    sids.add(st["session"])
            return (os.path.realpath(cand),), tuple(sorted(sids))

        ex_map = cursor.setdefault("excluded_files", {})
        for ex in unresolved_excludes:
            matched = sorted(up for up in universe_paths if _matches_specs_for_path(up, (ex,)))
            if ex in explicit_excludes:
                explicit_paths = [ex]
                if base_dir_for_exclude:
                    explicit_paths.extend((os.path.join(base_dir_for_exclude, ex),
                                           os.path.join(os.path.dirname(base_dir_for_exclude), ex)))
                concrete = next((p for p in explicit_paths if os.path.isfile(p)), None)
                if concrete:
                    matched = [os.path.abspath(concrete)]
            # A bare name is a convenience only when it names exactly one distinct file. Every identity is exact,
            # and an older frozen prefix result is deliberately re-resolved through this same rule once.
            def _distinct_file(up):
                cand = up if os.path.isabs(up) or not base_dir_for_exclude else os.path.join(base_dir_for_exclude, up)
                if not os.path.isfile(cand):
                    return None
                realpath = os.path.normcase(os.path.realpath(cand))
                identity = _file_identity(cand)
                key = ("file",) + identity if identity is not None else ("path", realpath)
                return key, realpath
            # Only distinct transcript FILES can make a spec ambiguous. A session id, a store key or a bare name that
            # is an alias of the same file names one identity (wire.jsonl across sessions is still two files).
            distinct = sorted(dict(f for f in (_distinct_file(up) for up in matched) if f is not None).values())
            if len(distinct) > 1:
                raise ValueError("exclude_files pattern %r is ambiguous; matches: %s" % (ex, ", ".join(distinct)))
            realpaths, sessions, card_refs = set(), set(), set()
            # A path-typed exclusion keeps the session ids of what it matched only for Claude transcripts, where a
            # renamed file keeps its session. Kimi and Antigravity derive the session from a folder name that a copy
            # in another workspace shares, so there the realpath alone is the identity. An exclusion typed as a
            # session id keeps sessions in every dialect.
            path_typed = "/" in ex or "\\" in ex or ex.lower().endswith(".jsonl")
            keep_sessions = not path_typed or not (kimi_dir or antigravity_dir)
            for up in matched:
                rps, sids = _identity_of(up)
                realpaths.update(rps)
                if keep_sessions:
                    sessions.update(sids)
                if not rps and up not in file_named:
                    card_refs.add(up)
            old = ex_map.get(ex)
            if isinstance(old, dict) and old.get("resolved"):
                # Upgrading an older frozen entry never drops what it excluded: a retargeted symlink or a new file
                # taking a renamed file's name must not re-admit the original transcript.
                realpaths.update(x for x in (old.get("realpaths") or []) if isinstance(x, str) and x.strip())
                sessions.update(x for x in (old.get("sessions") or []) if isinstance(x, str) and x.strip())
            if realpaths or card_refs or sessions:
                ex_map[ex] = {"resolved": True, "resolution": "exact-v1", "realpaths": sorted(realpaths),
                              "sessions": sorted(sessions), "card_refs": sorted(card_refs)}
            elif ex in explicit_excludes:
                # Matched only by its own spelling (a --files entry that does not exist, a dangling link): an
                # exclusion that names nothing real is refused, never accepted on spelling alone.
                raise ValueError(
                    "exclude_files pattern %r matched no transcript file, session id, or stored card" % (ex,)
                )
            # A persisted entry that resolves to nothing now stays unresolved: it keeps excluding by its text.
        all_excludes = exclusion_specs(ex_map)
        path_excluded_cache.clear()

    if all_excludes:
        for sf_key in list((cursor.get("seat_files") or {}).keys()):
            if _is_path_excluded(sf_key):
                cursor["seat_files"].pop(sf_key, None)
        for cf_key in list((cursor.get("codex_seat_files") or {}).keys()):
            if _is_path_excluded(cf_key):
                cursor["codex_seat_files"].pop(cf_key, None)

    markers_bytes = (
        [("echo %s-shell-ok" % s).encode("utf-8") for s in sorted({seat} | set(own))]
        + [m.encode("utf-8") for m in extra_markers if m]
    )
    for name in seat_files:
        if _is_path_excluded(name):
            continue
        if project_dir:
            sf_path = os.path.join(project_dir, name)
            if os.path.isfile(sf_path) and _has_foreign_seat_marker(sf_path, own, markers_bytes):
                continue
        cursor.setdefault("seat_files", {})[name] = True

    for csf in codex_session_files or ():
        if isinstance(csf, str) and csf.strip() and not _is_path_excluded(csf.strip()):
            cursor.setdefault("codex_seat_files", {})[csf.strip()] = True
    all_codex_pins = sorted((cursor.get("codex_seat_files") or {}).keys())
    codex_pin_only = bool(codex_cwds is None and (had_codex_pins or requested_codex_pins or all_codex_pins))

    prev_cursor_files = set((cursor.get("files") or {}).keys())
    prev_seat_files = set((cursor.get("seat_files") or {}).keys())
    explicit_files = [p for p in files if not _is_path_excluded(p)] if files is not None else None

    vanished_before_walk = []      # previously ingested files gone from disk before this run's walk
    codex_unmapped = []
    codex_unmapped_meta = []
    raw_subagents_by_parent = {}
    if codex_dir:
        norm_cwds = sorted({_norm_dir(c) for c in codex_cwds if _norm_dir(c)}) if codex_cwds is not None else None
        prev_cwds = cursor.get("codex_cwds_norm")
        if (not rebuild and not store_rebuilt and explicit_files is None and norm_cwds is not None
                and bool(cards or drops or cursor.get("files"))
                and ("codex_cwds_norm" not in cursor or prev_cwds is None
                     or not all(cwd_maps(p, norm_cwds) for p in prev_cwds))):
            _reset_ingested_store(for_selection=True)
        if explicit_files is None:
            cursor["codex_cwds_norm"] = norm_cwds
            if codex_cwds is not None:
                cursor.pop("codex_pin_only", None)
            elif codex_pin_only:
                cursor["codex_pin_only"] = True
        found, top_level = select_codex_files(
            codex_dir, cursor.setdefault("codex_files", {}), codex_cwds,
            cursor.setdefault("codex_cwd", {}), codex_unmapped,
            session_files=all_codex_pins, exclude_files=all_excludes,
            subagent_cache=cursor.setdefault("codex_subagents", {}),
            subagents_by_parent=raw_subagents_by_parent,
            unmapped_meta=codex_unmapped_meta,
            pin_only=codex_pin_only,
        )
        paths = list(explicit_files) if explicit_files is not None else found
        if not rebuild and not store_rebuilt and explicit_files is None and prev_cursor_files:
            selected = {os.path.basename(p) for p in paths}
            missing = sorted(ck for ck in prev_cursor_files if ck not in selected)
            if missing:
                on_disk = _codex_rollout_names(codex_dir)
                still_selected_elsewhere = [ck for ck in missing if ck in on_disk and not _is_path_excluded(ck)]
                if still_selected_elsewhere:
                    _reset_ingested_store(for_selection=True)       # still there, so the selection changed
                else:
                    vanished_before_walk.extend(ck for ck in missing if ck not in on_disk)  # gone cards stay
    elif kimi_dir:
        found, top_level = kimi_dialect.select_files(
            kimi_dir, cursor.setdefault("kimi_files", {}), exclude_files=all_excludes
        )
        paths = list(explicit_files) if explicit_files is not None else found
        if not rebuild and not store_rebuilt and explicit_files is None and prev_cursor_files:
            if _missing_still_on_disk(prev_cursor_files, paths, kimi_dir, vanished_before_walk, _is_path_excluded):
                _reset_ingested_store(for_selection=True)
    elif antigravity_dir:
        found, top_level = antigravity_dialect.select_files(
            antigravity_dir, cursor.setdefault("antigravity_files", {}), exclude_files=all_excludes
        )
        paths = list(explicit_files) if explicit_files is not None else found
        if not rebuild and not store_rebuilt and explicit_files is None and prev_cursor_files:
            if _missing_still_on_disk(prev_cursor_files, paths, antigravity_dir, vanished_before_walk, _is_path_excluded):
                _reset_ingested_store(for_selection=True)
    else:
        paths = list(explicit_files) if explicit_files is not None else select_files(
            project_dir, seat, all_files, cursor.setdefault("seat_files", {}), extra_markers,
            exclude_files=all_excludes, own=own, sids_cache=claude_sids_cache, single_seat=single_seat_dir,
        )
        top_level = sum(1 for n in os.listdir(project_dir)
                        if n.lower().endswith(".jsonl") and os.path.isfile(os.path.join(project_dir, n)))
        if not rebuild and not store_rebuilt and explicit_files is None:
            vanished_before_walk.extend(
                ck for ck in sorted(prev_cursor_files)
                if not os.path.isfile(os.path.join(project_dir, ck)) and not _is_path_excluded(ck)
            )
        evicted_seat_files = prev_seat_files - set((cursor.get("seat_files") or {}).keys())
        if (not rebuild and not store_rebuilt and explicit_files is None and prev_cursor_files
                and ((evicted_seat_files & prev_cursor_files)
                     or (all_excludes and any(_is_path_excluded(ck) for ck in prev_cursor_files)))):
            _reset_ingested_store(for_selection=True)

    if all_excludes:
        paths = [p for p in paths if not _is_path_excluded(p)]
        excluded_subagent_ids = {
            sub_info[1]
            for sub_name, sub_info in (cursor.get("codex_subagents") or {}).items()
            if isinstance(sub_info, (list, tuple)) and len(sub_info) >= 2 and _is_path_excluded(sub_name)
        }
        for ck in list((cursor.get("files") or {}).keys()):
            if _is_path_excluded(ck):
                cursor["files"].pop(ck, None)
                if isinstance(cursor.get("kimi_open_asks"), dict):
                    cursor["kimi_open_asks"].pop(ck, None)
                if isinstance(cursor.get("antigravity_open_asks"), dict):
                    cursor["antigravity_open_asks"].pop(ck, None)
                if isinstance(cursor.get("antigravity_refs"), dict):
                    cursor["antigravity_refs"].pop(ck, None)
        known_candidates = list(paths)
        if codex_dir or kimi_dir or antigravity_dir:
            known_candidates.extend(found)
            known_candidates.extend(
                um["path"] for um in codex_unmapped_meta if isinstance(um, dict) and um.get("path")
            )

        def _resolve_replay_path(rf):
            rf_norm = os.path.normcase(os.path.normpath(rf)).replace("\\", "/")
            rf_has_sep = "/" in rf.replace("\\", "/")
            for kp in known_candidates:
                kp_norm = os.path.normcase(os.path.normpath(kp)).replace("\\", "/")
                rel_match = (
                    base_dir_for_exclude
                    and os.path.normcase(os.path.normpath(os.path.relpath(kp, base_dir_for_exclude))).replace("\\", "/")
                    == rf_norm
                )
                suffix_match = kp_norm == rf_norm or kp_norm.endswith("/" + rf_norm)
                if rel_match or suffix_match:
                    if os.path.isfile(kp) and not _is_path_excluded(kp):
                        return kp
            for cand_rf in _resolve_candidate_paths(rf):
                if rf_has_sep:
                    c_norm = os.path.normcase(os.path.normpath(cand_rf)).replace("\\", "/")
                    if not (c_norm == rf_norm or c_norm.endswith("/" + rf_norm)):
                        continue
                if os.path.isfile(cand_rf) and not _is_path_excluded(cand_rf):
                    return cand_rf
            if not rf_has_sep:
                for kp in known_candidates:
                    if os.path.normcase(os.path.basename(kp)) == rf_norm:
                        if os.path.isfile(kp) and not _is_path_excluded(kp):
                            return kp
            return None

        replay_file_names = set()
        for cid, c in list(cards.items()):
            if c.get("type") == "Context":
                continue
            c_src = c.get("source") if isinstance(c.get("source"), dict) else {}
            c_files = c_src.get("files") or ()
            c_sessions = c_src.get("sessions") or ()
            kept_files = [f for f in c_files if not _is_path_excluded(f)]
            kept_sessions = [
                s for s in c_sessions
                if s not in excluded_subagent_ids and not (_matches_file_spec(s, all_excludes) or _is_path_excluded(s))
            ]
            if (c_files and not kept_files) or (c_sessions and not kept_sessions):
                cards.pop(cid, None)
            elif len(kept_files) < len(c_files) or (
                len(kept_sessions) < len(c_sessions) and c_src.get("other_content")
            ):
                cards.pop(cid, None)
                replay_file_names.update(kept_files)
            else:
                if len(kept_sessions) < len(c_sessions):
                    c_src["sessions"] = kept_sessions
        if replay_file_names:
            existing_paths_norm = {os.path.normcase(os.path.abspath(p)) for p in paths}
            for rf in sorted(replay_file_names):
                cand_rf = _resolve_replay_path(rf)
                rf_norm = os.path.normcase(os.path.normpath(rf)).replace("\\", "/")
                rf_has_sep = "/" in rf.replace("\\", "/")
                cand_rf_abs = os.path.normcase(os.path.abspath(cand_rf)) if cand_rf is not None else None
                for ck in list((cursor.get("files") or {}).keys()):
                    ck_norm = os.path.normcase(os.path.normpath(ck)).replace("\\", "/")
                    ck_matches = (
                        ck == rf
                        or ck_norm == rf_norm
                        or ck_norm.endswith("/" + rf_norm)
                        or (cand_rf_abs is not None and os.path.normcase(os.path.abspath(ck)) == cand_rf_abs)
                        or (not rf_has_sep and os.path.normcase(os.path.basename(ck)) == rf_norm)
                    )
                    if ck_matches:
                        cursor["files"].pop(ck, None)
                        if isinstance(cursor.get("kimi_open_asks"), dict):
                            cursor["kimi_open_asks"].pop(ck, None)
                        if isinstance(cursor.get("antigravity_open_asks"), dict):
                            cursor["antigravity_open_asks"].pop(ck, None)
                        if isinstance(cursor.get("antigravity_refs"), dict):
                            cursor["antigravity_refs"].pop(ck, None)
                if cand_rf is not None:
                    norm_rf = os.path.normcase(os.path.abspath(cand_rf))
                    if norm_rf not in existing_paths_norm:
                        paths.append(cand_rf)
                        existing_paths_norm.add(norm_rf)
        for did, d in list(drops.items()):
            if _is_path_excluded(d.get("file") or "") or (d.get("file") or "") in replay_file_names:
                drops.pop(did, None)
    for p in paths:
        _check_input(p)

    codex_unmapped_newer_or_larger = []
    if codex_dir and codex_unmapped_meta:
        existing_paths = []
        for p in paths:
            try:
                os.path.getsize(p)
                existing_paths.append(p)
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                # A rollout may be removed or replaced by a folder between selection and this comparison.
                if p not in vanished_before_walk:
                    vanished_before_walk.append(p)
        ref_mtime = max((os.path.getmtime(p) for p in existing_paths), default=None)
        ref_size = max((os.path.getsize(p) for p in existing_paths), default=None)
        for um in codex_unmapped_meta:
            try:
                u_mtime = os.path.getmtime(um["path"])
                u_size = os.path.getsize(um["path"])
            except OSError:
                continue
            is_newer = True if ref_mtime is None else (u_mtime > ref_mtime)
            is_larger = True if ref_size is None else (u_size > ref_size)
            if is_newer or is_larger:
                codex_unmapped_newer_or_larger.append({
                    "file": um["file"],
                    "cwd": um["cwd"],
                    "size": u_size,
                    "mtime": round(u_mtime, 3),
                    "newer": is_newer,
                    "larger": is_larger,
                })

    mapped_codex_sessions = {
        (_codex_state(p) or {}).get("session") for p in paths if codex_dir
    } - {"", None}
    codex_subagents_by_parent = {
        pid: sorted(set(cids))
        for pid, cids in sorted(raw_subagents_by_parent.items())
        if pid in mapped_codex_sessions and cids
    }
    codex_mapped_children = sorted({cid for cids in codex_subagents_by_parent.values() for cid in cids})

    report = {"loader_version": LOADER_VERSION, "seat": seat, "seat_own_names": sorted(own),
              "files": len(paths), "lines_read": 0, "bad_lines": 0,
              # Transcripts in the folder that were not read: another seat's, or this seat's sessions from before
              # the session-start marker existed. Name those with --files if their rulings are wanted.
              "files_top_level": top_level, "files_not_read": max(0, top_level - len(paths)),
              "seat_files_named": sorted(set(seat_files)),
              "codex_session_files_named": all_codex_pins,
              "codex_pin_only": codex_pin_only,
              "excluded_files": sorted(k for k in (cursor.get("excluded_files") or {}) if isinstance(k, str)),
              "machine_prefixes": list(prefixes),
              "codex_cwds": sorted(codex_cwds) if codex_cwds is not None else None,
              "codex_unmapped": codex_unmapped,
              "codex_unmapped_newer_or_larger": codex_unmapped_newer_or_larger,
              "codex_unmapped_alert": bool(codex_unmapped_newer_or_larger),
              "codex_subagents_by_parent": codex_subagents_by_parent,
              "codex_mapped_children": codex_mapped_children,
              "files_read": sorted(os.path.relpath(p, kimi_dir) for p in paths) if kimi_dir
              else sorted(os.path.relpath(p, antigravity_dir) for p in paths) if antigravity_dir
              else sorted(os.path.basename(p) for p in paths),
              "files_codex": 0, "files_kimi": 0, "files_antigravity": 0,
              "new_cards": 0, "provenance_added": 0, "redaction_drops_new": 0, "dropped_by_class": {},
              "cursor_resets": 0, "rescreen_drops": 0, "store_rebuilt_from_older_format": store_rebuilt}
    if vanished_before_walk:
        report.setdefault("files_vanished", []).extend(vanished_before_walk)

    # Screen the cards already in the store against the CURRENT detectors, so a tightened rule also removes what
    # an earlier run let through. Without this a detector fix would only ever apply to new transcript lines.
    prefilled = set(cursor.setdefault("prefilled_asks", []))
    for cid in list(cards):
        fired = detect(_card_strings(cards[cid]), disabled_detectors, seat)
        if fired:
            old = cards.pop(cid)
            drops[cid] = {"id": cid, "record_uuid": old["source"]["record_uuid"], "block_index": old["source"]["block_index"],
                          "type": old["type"], "detectors": sorted(fired), "file": (old["source"].get("files") or ["context"])[0]}
            if cid == cursor.get("context_head_id") or old.get("type") == "Context":
                cursor.pop("context_head_id", None)
                cursor.pop("context_head_sha256", None)
                cursor.pop("context_head_time", None)
            report["rescreen_drops"] += 1

    for path in paths:
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            if not _gone_or_directory(exc, path):
                raise
            # Deleted or replaced by a directory after selection: other transcripts still load.
            report.setdefault("files_vanished", []).append(name)
            continue
        kimi = kimi_dialect.file_state(path) if kimi_dir else None
        antigravity = antigravity_dialect.file_state(path) if antigravity_dir else None
        if kimi is not None:
            report["files_kimi"] += 1
            kimi["asks"].update(cursor.setdefault("kimi_open_asks", {}).get(path, {}))
            name = os.path.relpath(path, kimi_dir)
        elif antigravity is not None:
            report["files_antigravity"] += 1
            antigravity["asks"].update(cursor.setdefault("antigravity_open_asks", {}).get(path, {}))
            antigravity["refs"] = dict(cursor.setdefault("antigravity_refs", {}).get(path, {}))
            name = os.path.relpath(path, antigravity_dir)
        ckey = path if (kimi is not None or antigravity is not None) else name       # every wire file is wire.jsonl, every transcript is transcript.jsonl: the path is the key
        state = cursor["files"].get(ckey, {"offset": 0, "head_len": 0, "head_sha256": ""})
        if antigravity is not None and state["offset"] and path not in cursor["antigravity_refs"]:
            # A cursor from before 1.22 has no refs for this file: read it again from the start so a step_index
            # reused later in it is told apart. Ids do not change: the first record at an index keeps its ref.
            state = {"offset": 0, "head_len": 0, "head_sha256": ""}
        codex = _codex_state(path)
        report["files_codex"] += 1 if codex is not None else 0
        try:
            handle = open(path, "rb")
        except OSError as exc:
            if not _gone_or_directory(exc, path):
                raise
            report.setdefault("files_vanished", []).append(name)     # deleted/replaced after getsize
            continue
        with handle as f:
            head = f.read(min(HEAD_BYTES, size))
            if state["offset"] > size or (state["head_len"] and
                                           hashlib.sha256(head[: state["head_len"]]).hexdigest() != state["head_sha256"]):
                state = {"offset": 0, "head_len": 0, "head_sha256": ""}   # replaced or truncated: read it again
                report["cursor_resets"] += 1
                if kimi is not None:
                    # A replaced or truncated wire file is a new source generation: the old file's open asks
                    # must not pair with this one's answers (codex-kxxx-cli's probe, txxxx's confirm).
                    kimi["asks"].clear()
                if antigravity is not None:
                    antigravity["asks"].clear()
                    antigravity["refs"] = {}
            dialect = "antigravity" if antigravity is not None else "kimi" if kimi is not None else "codex" if codex is not None else "claude"
            meta = ({"session": antigravity["session"], "subagent": antigravity["subagent"]} if antigravity is not None
                    else {"session": kimi["session"], "subagent": kimi["subagent"]} if kimi is not None
                    else {"session": codex["session"]} if codex is not None else {})
            cp = dict(new_checkpoint(dialect, meta), offset=state["offset"], prefilled_asks=sorted(prefilled),
                      open_asks=antigravity["asks"] if antigravity is not None else kimi["asks"] if kimi is not None else {})
            if antigravity is not None:
                cp["ag_refs"] = antigravity["refs"]
            # run() keeps every prefilled ask it has seen (prune_answered=False), as its store always has: a
            # resumed copy of a session holds the call and its result again, in another file.
            file_raws, stats, cp = read_file(_lines_from(f, state["offset"]), cp, seat=seat, seats=seats,
                                             own=own, max_pending=None, prune_answered=False)
        prefilled |= set(cp["prefilled_asks"])
        if kimi is not None:
            kimi["asks"] = cp["open_asks"]
        if antigravity is not None:
            antigravity["asks"] = cp["open_asks"]
            antigravity["refs"] = cp.get("ag_refs") or {}
        offset = cp["offset"]       # a Codex item whose event is not read yet is read again next run
        report["lines_read"] += stats["lines_read"]
        report["bad_lines"] += stats["bad_lines"]
        for cls, n in stats["dropped_by_class"].items():
            report["dropped_by_class"][cls] = report["dropped_by_class"].get(cls, 0) + n
        for raw in file_raws:
            if machine_prefix_of(raw, prefixes):
                # Machine text, never a card. A card an earlier run kept for it leaves the local store too (on a
                # --rebuild, the only run that reads old lines again), so
                # the next card write does not send it (a card already on the server stays: first version stands).
                report["dropped_by_class"]["machine_prefix"] = report["dropped_by_class"].get("machine_prefix", 0) + 1
                cards.pop(_raw_identity(raw)[0], None)
                continue
            source_sha = sha256_text(json.dumps(raw["kept"], sort_keys=True, ensure_ascii=False))
            cid, ref = _raw_identity(raw)               # the record and block, never session or content
            existing = cards.get(cid)
            other = existing is not None and existing["source"]["source_sha256"] != source_sha
            fired = detect(_kept_strings(raw["kept"]), disabled_detectors, seat)
            if fired:
                if other:                                      # another variant of this record is the card
                    _note_variant(existing, source_sha, raw["session"], name)
                    continue
                if cid not in drops:
                    report["redaction_drops_new"] += 1
                drops[cid] = {"id": cid, "record_uuid": raw["uuid"], "block_index": raw["block_index"],
                              "type": raw["type"], "detectors": sorted(fired), "file": name}
                cards.pop(cid, None)
                continue
            if existing is not None and not other:
                src = existing["source"]
                before = (len(src["sessions"]), len(src["files"]))
                src["sessions"] = sorted(set(src["sessions"]) | ({raw["session"]} if raw["session"] else set()))
                src["files"] = sorted(set(src["files"]) | {name})
                if (len(src["sessions"]), len(src["files"])) != before:
                    report["provenance_added"] += 1
                continue
            # Same record and block with DIFFERENT content (none among 980 cards on the one seat measured;
            # handled so the result never depends on file order): the earlier one is the card, by record
            # time and then by content hash, and the other content's hash is kept beside it.
            if other and (existing["time"], existing["source"]["source_sha256"]) <= (raw["date"], source_sha):
                _note_variant(existing, source_sha, raw["session"], name)
                continue
            card = _card_from_raw(raw, seat, cid, ref, source_sha, [name])
            if other:
                src = existing["source"]
                _note_variant(card, src["source_sha256"], "", "")
                for sha in src.get("other_content", []):
                    _note_variant(card, sha, "", "")
                card["source"]["sessions"] = sorted(set(card["source"]["sessions"]) | set(src["sessions"]))
                card["source"]["files"] = sorted(set(card["source"]["files"]) | set(src["files"]))
            else:
                report["new_cards"] += 1
            cards[cid] = card
            drops.pop(cid, None)                               # it passes now: no stale drop row
        head_len = min(HEAD_BYTES, size)
        cursor["files"][ckey] = {"offset": offset, "head_len": head_len,
                                 "head_sha256": hashlib.sha256(head[:head_len]).hexdigest()}
        if kimi is not None:
            # A call still open at the file's end is counted, not carded: Question cards come from the live
            # hook. Open asks persist, so the next run ties a result appended after this one.
            if kimi["asks"]:
                report["dropped_by_class"]["kimi_question_open"] = (
                    report["dropped_by_class"].get("kimi_question_open", 0)
                    + sum(len(a["questions"]) for a in kimi["asks"].values()))
            cursor["kimi_open_asks"][path] = kimi["asks"]
        if antigravity is not None:
            if antigravity["asks"]:
                report["dropped_by_class"]["antigravity_question_open"] = (
                    report["dropped_by_class"].get("antigravity_question_open", 0)
                    + sum(len(a["questions"]) for a in antigravity["asks"].values()))
            cursor["antigravity_open_asks"][path] = antigravity["asks"]
            cursor["antigravity_refs"][path] = antigravity["refs"]

    protected_sessions = {}        # card id -> the sessions a card had before children were added to it
    if codex_subagents_by_parent:
        for parent_id, child_ids in codex_subagents_by_parent.items():
            parent_cards = [
                c for c in cards.values()
                if c.get("type") != "Context" and parent_id in (c.get("source", {}).get("sessions") or ())
            ]
            if parent_cards:
                latest_parent = max(parent_cards, key=lambda c: (c["time"], c["id"]))
                own_sessions = set(latest_parent["source"]["sessions"])
                # At most MAX_CONTEXT_SESSIONS children are named (#374, Codex P2: 221 children made a 9,608-byte
                # card the server refuses); the size guard before the write is the backstop.
                extra = [c for c in sorted(set(child_ids)) if c not in own_sessions]
                latest_parent["source"]["sessions"] = sorted(
                    own_sessions | set(extra[:context_snapshot.MAX_CONTEXT_SESSIONS])
                )
                protected_sessions[latest_parent["id"]] = own_sessions

    ctx_head_invalid = False
    for cid, c in list(cards.items()):
        if c.get("type") != "Context":
            continue
        c_src = c.get("source") if isinstance(c.get("source"), dict) else {}
        c_files = c_src.get("files") or ()
        c_sessions = c_src.get("sessions") or ()
        if all_excludes:
            kept_ctx_files = [f for f in c_files if not _is_path_excluded(f)]
            kept_ctx_sessions = [
                s for s in c_sessions
                if not (_matches_file_spec(s, all_excludes) or _is_path_excluded(s))
            ]
            if len(kept_ctx_files) < len(c_files) or (c_sessions and not kept_ctx_sessions):
                cards.pop(cid, None)
                ctx_head_invalid = True
                continue
            if len(kept_ctx_sessions) < len(c_sessions):
                c_src["sessions"] = kept_ctx_sessions
        if (selection_reset or rebuild or store_rebuilt) and (
            not paths
            or any(f not in set(report["files_read"]) for f in c_files)
            or (codex_dir and c_sessions and not (set(c_sessions) & (mapped_codex_sessions | set(codex_mapped_children))))
        ):
            cards.pop(cid, None)
            ctx_head_invalid = True
    if ctx_head_invalid and not emit_context:
        cursor.pop("context_head_id", None)
        cursor.pop("context_head_sha256", None)
        cursor.pop("context_head_time", None)
        cursor.pop("context_prev_head", None)

    mark_repeats(cards.values())
    ordered = sorted(cards.values(), key=lambda c: (c["time"], c["id"]))

    if emit_context:
        run_dialect = ("antigravity" if antigravity_dir else "kimi" if kimi_dir
                       else "codex" if codex_dir else "claude")
        base_dir = antigravity_dir or kimi_dir or codex_dir or project_dir
        prev_ctx_id = cursor.get("context_head_id")
        prev_ctx_sha = None if ctx_head_invalid else cursor.get("context_head_sha256")
        prev_ctx_time = cursor.get("context_head_time")
        doing_cache = context_snapshot._deserialize_doing_cursor_cache(cursor.get("doing_cursors"))
        snapshot = context_snapshot.extract_seat_context(
            paths, run_dialect, seat, ordered,
            base_dir=base_dir, detect_fn=detect, disabled_detectors=disabled_detectors,
            utc_instant_fn=utc_instant, doing_cursor_cache=doing_cache,
        )
        cursor["doing_cursors"] = context_snapshot._serialize_doing_cursor_cache(doing_cache)
        if mapped_codex_sessions or codex_mapped_children:
            # Newest mapped session first, each followed by its own children, cut to the Context card's bound
            # (#374, Codex P2: an unbounded list made the card too large for the server).
            sid_mtime = {}
            for p in paths:
                sid = (_codex_state(p) or {}).get("session")
                if sid:
                    try:
                        sid_mtime[sid] = max(sid_mtime.get(sid, 0.0), os.path.getmtime(p))
                    except OSError:
                        sid_mtime.setdefault(sid, 0.0)
            newest_first = []
            for pid in sorted(mapped_codex_sessions, key=lambda s: (-sid_mtime.get(s, 0.0), s)):
                newest_first.append(pid)
                newest_first.extend(codex_subagents_by_parent.get(pid, ()))
            snapshot["sessions"] = list(
                dict.fromkeys(list(snapshot.get("sessions") or []) + newest_first + codex_mapped_children)
            )[:context_snapshot.MAX_CONTEXT_SESSIONS]
        ctx_card, ctx_sha, ctx_err = context_snapshot.build_context_card(
            seat, run_dialect, snapshot,
            loader_version=LOADER_VERSION, detect_fn=detect, disabled_detectors=disabled_detectors,
            utc_instant_fn=utc_instant, prev_context_id=prev_ctx_id, prev_context_time=prev_ctx_time,
        )
        if ctx_card is None:
            if ctx_head_invalid:
                cursor.pop("context_head_id", None)
                cursor.pop("context_head_sha256", None)
                cursor.pop("context_head_time", None)
                cursor.pop("context_prev_head", None)
            if isinstance(ctx_err, str) and ctx_err.startswith("redaction:"):
                fired_names = [x for x in ctx_err[len("redaction:"):].split(",") if x]
                drop_id = sha256_text("v1|context:%s:%s|0" % (seat, (ctx_sha or "redacted")[:16]))[:32]
                if drop_id not in drops:
                    report["redaction_drops_new"] += 1
                drops[drop_id] = {
                    "id": drop_id,
                    "record_uuid": "context:%s:%s" % (seat, (ctx_sha or "redacted")[:16]),
                    "block_index": 0,
                    "type": "Context",
                    "detectors": fired_names,
                    "file": (snapshot.get("source_files") or ["context"])[-1],
                }
            report["context_card"] = {"status": "skipped", "reason": ctx_err}
        elif ctx_sha == prev_ctx_sha and prev_ctx_id in cards:
            report["context_card"] = {"status": "unchanged", "id": prev_ctx_id}
        else:
            write_ok, write_ver, write_err = True, None, None
            if write_context_fn is not None:
                write_ok, write_ver, write_err = _context_write_ok(write_context_fn(ctx_card))
            if not write_ok:
                if ctx_head_invalid:
                    cursor.pop("context_head_id", None)
                    cursor.pop("context_head_sha256", None)
                    cursor.pop("context_head_time", None)
                    cursor.pop("context_prev_head", None)
                # Server refused or failed: keep the previous Context head and leave cursor in place (#297, MEDIUM 3)
                report["context_card"] = {
                    "status": "refused",
                    "id": ctx_card["id"],
                    "error_code": write_err,
                    "supersedes": ctx_card.get("supersedes", []),
                }
            else:
                prev_head_card = None if ctx_head_invalid else (cards.get(prev_ctx_id) if prev_ctx_id else None)
                if write_context_fn is None:
                    cursor["context_prev_head"] = {
                        "id": prev_ctx_id,
                        "sha256": prev_ctx_sha,
                        "time": prev_ctx_time,
                        "card": prev_head_card,
                    }
                else:
                    cursor.pop("context_prev_head", None)
                # Keep only the active Context head in the local store while superseding the prior head
                for old_cid in [k for k, v in cards.items() if v.get("type") == "Context" and k != ctx_card["id"]]:
                    cards.pop(old_cid, None)
                if ctx_card["id"] not in cards:
                    report["new_cards"] += 1
                cards[ctx_card["id"]] = ctx_card
                drops.pop(ctx_card["id"], None)
                cursor["context_head_id"] = ctx_card["id"]
                cursor["context_head_sha256"] = ctx_sha
                cursor["context_head_time"] = ctx_card["time"]
                ordered = sorted(cards.values(), key=lambda c: (c["time"], c["id"]))
                report["context_card"] = {
                    "status": "emitted",
                    "id": ctx_card["id"],
                    "supersedes": ctx_card.get("supersedes", []),
                    **({"version": write_ver} if write_ver is not None else {}),
                }

    # The one size guard every written card passes, so cards.jsonl and the spool never hold a card the server refuses
    # for its size (apps/cards/validation.py, MAX_VALUE_BYTES). A card that cannot fit is counted, not written changed.
    for c in ordered:
        if c.get("type") != "Context":
            if not context_snapshot.fit_card_sessions(c, protect=protected_sessions.get(c["id"], ())):
                report["oversize_cards"] = report.get("oversize_cards", 0) + 1
    _write_atomic(cards_path, "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in ordered))
    spoolable_ordered = [c for c in ordered if c.get("type") != "Context"]
    if spool_pending or _env_wants_spool():
        report["pending_spooled"] = write_pending_spool(
            out_dir, spoolable_ordered, replace=True, preserve_external=True,
            producer=("transcript", seat),
            preserve_keys=_sibling_output_card_keys(out_dir, cards_path),
            is_excluded_fn=_is_path_excluded,
        )
    else:
        reconciled = reconcile_pending_spool(
            out_dir, spoolable_ordered, is_excluded_fn=_is_path_excluded,
            producer=("transcript", seat),
            preserve_keys=_sibling_output_card_keys(out_dir, cards_path),
        )
        if reconciled is not None:
            report["pending_reconciled"] = reconciled
    _write_atomic(drops_path, "".join(json.dumps(drops[k], sort_keys=True) + "\n" for k in sorted(drops)))
    cursor["prefilled_asks"] = sorted(prefilled)
    _write_atomic(cursor_path, json.dumps(cursor, sort_keys=True, indent=1))

    by_type, by_detector = {}, {}
    for c in ordered:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    for d in drops.values():
        for name in d["detectors"]:
            by_detector[name] = by_detector.get(name, 0) + 1
    report.update({
        "cards_total": len(ordered), "cards_by_type": by_type,
        "candidates_short": sum(1 for c in ordered if c.get("short")),
        "candidates_from_slash_command": sum(1 for c in ordered if c.get("slash_command")),
        "candidates_typed_mid_turn": sum(1 for c in ordered
                                         if c.get("prompt_source") in ("queued_command", "queued_command_legacy")),
        "candidates_repeating_an_earlier_one": sum(1 for c in ordered if c.get("repeat_of")),
        "candidate_texts_repeated": sum(1 for c in ordered if c.get("repeats")),
        "rulings_free_text": sum(1 for c in ordered if c.get("free_text")),
        "rulings_long_free_text": sum(1 for c in ordered if c.get("long_free_text")),
        "candidates_legacy_origin": sum(1 for c in ordered if c.get("legacy_origin")),
        "cards_truncated": sum(1 for c in ordered if c.get("truncated")),
        "cards_in_more_than_one_file": sum(1 for c in ordered if len(c["source"]["files"]) > 1),
        "uuid_conflicts": sum(1 for c in ordered if c["source"].get("other_content")),
        "largest_card_bytes": max([_card_bytes(c) for c in ordered] or [0]),
        "cards_over_server_limit": sum(1 for c in ordered if _card_bytes(c) > SERVER_VALUE_LIMIT),
        "redaction_drops_total": len(drops), "redaction_drops_by_detector": by_detector,
        "time_first": ordered[0]["time"] if ordered else None, "time_last": ordered[-1]["time"] if ordered else None,
    })
    with open(cards_path, "rb") as f:
        report["cards_sha256"] = hashlib.sha256(f.read()).hexdigest()
    _write_atomic(os.path.join(out_dir, "report.json"), json.dumps(report, sort_keys=True, indent=1))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract first-hand operator decisions from a seat's transcripts into local cards.")
    ap.add_argument("--project-dir", help="the seat's Claude Code project folder (top-level *.jsonl only)")
    ap.add_argument("--codex-sessions",
                    help="for a Codex seat, in place of --project-dir: the sessions folder of its Codex home. Every "
                         "interactive session under it is read; exec runs, subagent threads and copies of Claude "
                         "Code sessions are not")
    ap.add_argument("--codex-cwd", action="append",
                    help="with --codex-sessions, repeatable: read only the sessions whose recorded working folder is "
                         "this folder or under it. A Codex home is not a seat (one home serves several seats and the "
                         "Codex runs they start), so a load for one seat names its folders; every other session is "
                         "skipped and listed in the report's codex_unmapped with its cwd, never guessed. Without it "
                         "every interactive session is the seat's, which says the home serves this seat alone")
    ap.add_argument("--kimi-sessions",
                    help="for a Kimi seat, in place of --project-dir: the sessions folder of its Kimi home. Every "
                         "agents/main wire.jsonl under it is read; subagent wires are not (their user records are "
                         "the parent's dispatch text)")
    ap.add_argument("--antigravity-brain",
                    help="for an Antigravity seat, in place of --project-dir: the brain folder of its Antigravity "
                         "CLI home. Every conversation's .system_generated/logs/transcript.jsonl under it is read; "
                         "subagent transcripts are not")
    ap.add_argument("--seat", required=True,
                    help="the seat's roster label, stamped on every card; files are picked by the session-start "
                         "marker 'echo <seat>-shell-ok' (a seat whose launcher tag differs from its label names the "
                         "tag's marker with --marker)")
    ap.add_argument("--out", required=True, help="local output folder for cards.jsonl, drops.jsonl, cursor.json, report.json")
    ap.add_argument("--all-files", action="store_true", help="take every top-level transcript, whatever the seat marker")
    ap.add_argument("--single-seat-dir", action="store_true",
                    help="--project-dir is this seat's alone: also take its transcripts with no marker, except one that holds another seat's marker")
    ap.add_argument("--files", nargs="*", help="explicit transcript files instead of discovery")
    ap.add_argument("--seats", action="append",
                    help="a seat label beyond the built-in roster (repeatable): a typed prompt that opens "
                         "'From <label>:' or '<label>:' is then taken for that seat's message and not for the operator's")
    ap.add_argument("--own-name", action="append",
                    help="another name this seat goes by (repeatable): a typed prompt that opens '<name>:' is then "
                         "addressed to this seat and kept. The seat label counts already, and so does a launcher "
                         "name given as --marker 'echo <name>-shell-ok'")
    ap.add_argument("--marker", action="append",
                    help="extra text that marks a transcript as this seat's when one of its own user prompts holds it "
                         "(repeatable); for sessions older than the session-start marker")
    ap.add_argument("--rebuild", action="store_true",
                    help="write the store again from a full read (after a loader upgrade that changes the card rules)")
    ap.add_argument("--machine-prefix", action="append",
                    help="an opening this seat's own automation types into its sessions (repeatable), such as a wake "
                         "job's or a relay bridge's first words: a typed prompt that starts with it is dropped as "
                         "machine_prefix, never a card. Matched on the first characters, case-sensitive. "
                         "Built in for every seat: " + ", ".join(repr(p) for p in BUILTIN_MACHINE_PREFIXES))
    ap.add_argument("--seat-file", action="append", help="a top-level *.jsonl in --project-dir that is this seat's though none of its prompts holds the marker, such as a continuation transcript (repeatable); it is read now and on every later run")
    ap.add_argument("--codex-session-file", action="append",
                    help="with --codex-sessions, repeatable: an interactive rollout file name, relative path or "
                         "session id to pin to this seat even when its recorded cwd is outside --codex-cwd (or "
                         "to select only pinned sessions when --codex-cwd is not given); persisted in cursor.json")
    ap.add_argument("--exclude-file", action="append",
                    help="a transcript file name, relative path or session id to skip across any dialect "
                         "(repeatable); persisted in cursor.json and evicts matching entries from seat_files")
    ap.add_argument("--emit-context", action="store_true",
                    help="emit at most one active Context card summarizing this seat's recent branch, PRs, threads, "
                         "repo-relative files, and latest task, superseding the prior Context head when changed (#297)")
    ap.add_argument("--spool-pending", action="store_true",
                    help="also write server-writable cards to <out>/pending.jsonl atomically (or when WRITE=spool "
                         "or MESHY_CARDS_WRITE=spool)")
    a = ap.parse_args(argv)
    if [bool(a.project_dir), bool(a.codex_sessions), bool(a.kimi_sessions), bool(a.antigravity_brain)].count(True) != 1:
        ap.error("give exactly one of --project-dir (a Claude Code seat), --codex-sessions (a Codex seat), "
                 "--kimi-sessions (a Kimi seat) and --antigravity-brain (an Antigravity seat)")
    if a.codex_cwd and not a.codex_sessions:
        ap.error("--codex-cwd selects Codex sessions; give it with --codex-sessions")
    if a.codex_cwd and any(not isinstance(c, str) or not c.strip() for c in a.codex_cwd):
        ap.error("--codex-cwd needs a folder")
    if a.codex_cwd and any(not _ABSOLUTE.match(c) for c in a.codex_cwd):
        ap.error("--codex-cwd needs an absolute folder (a drive letter, a leading / or a UNC path): a session "
                 "records its cwd as an absolute path, so a relative one would never match")
    if a.codex_session_file and not a.codex_sessions:
        ap.error("--codex-session-file selects Codex sessions; give it with --codex-sessions")
    if a.codex_session_file and any(not isinstance(s, str) or not s.strip() for s in a.codex_session_file):
        ap.error("--codex-session-file needs a file name or session id")
    if a.exclude_file and any(not isinstance(s, str) or not s.strip() for s in a.exclude_file):
        ap.error("--exclude-file needs a file name or session id")
    for p in a.machine_prefix or ():
        if len(p.strip()) < 12:
            ap.error("--machine-prefix %r is too short: a prefix this broad would also drop the operator's own "
                     "words (give at least 12 characters of the machine's fixed opening)" % p)
    if a.seat_file and not a.project_dir:
        ap.error("--seat-file names a transcript in --project-dir; a Codex, Kimi or Antigravity seat has no marker to stand in for")
    seat_files, seat_files_missing = [], []
    try:
        with open(os.path.join(a.out, "cursor.json"), encoding="utf-8") as f:
            recorded_pins = set((json.load(f).get("seat_files") or {}).keys())
    except (OSError, ValueError, AttributeError):
        recorded_pins = set()
    for name in a.seat_file or ():
        problem = seat_file_problem(a.project_dir, name)
        if problem == SEAT_FILE_MISSING and name in recorded_pins:
            seat_files_missing.append(name)
            sys.stderr.write("transcript_cards: --seat-file %s is not in --project-dir any more; skipped this run, "
                             "the seat's other transcripts still load\n" % name)
            continue
        if problem:
            ap.error("--seat-file %s: %s" % (name, problem))
        seat_files.append(name)
    if a.emit_context and (a.spool_pending or _env_wants_spool()):
        ap.error("--emit-context cannot be combined with --spool-pending or WRITE=spool: Context cards require "
                 "single-writer cursor.json ownership in the loader")
    try:
        report = run(a.project_dir, a.seat, a.out, files=a.files, all_files=a.all_files, rebuild=a.rebuild,
                     extra_markers=a.marker or (), extra_seats=a.seats or (), extra_own=a.own_name or (),
                     codex_dir=a.codex_sessions, kimi_dir=a.kimi_sessions, antigravity_dir=a.antigravity_brain,
                     seat_files=seat_files, codex_cwds=a.codex_cwd,
                     codex_session_files=a.codex_session_file or (), exclude_files=a.exclude_file or (),
                     machine_prefixes=a.machine_prefix or (), emit_context=a.emit_context,
                     spool_pending=a.spool_pending, single_seat_dir=a.single_seat_dir)
    except PolicyUnreadable as exc:
        sys.stderr.write("transcript_cards: cursor.json is unreadable as an exclusion policy (%s); nothing written\n" % exc)
        return 4
    except ValueError as exc:
        if "exclude_files pattern" in str(exc):
            ap.error(str(exc))
        raise
    if seat_files_missing:
        report["seat_files_missing"] = sorted(seat_files_missing)
    print(json.dumps(report, sort_keys=True, indent=1))
    if (report["files"] == 0 and report.get("files_vanished") and report.get("cards_total", 0) > 0
            and report["files_top_level"] == 0 and not report.get("codex_unmapped")):
        # Previously admitted sources disappeared; successfully retained cards can still be flushed.
        return 0
    if a.codex_sessions and report["files"] == 0 and a.codex_cwd and report["codex_unmapped"]:
        sys.stderr.write("transcript_cards: %d interactive Codex session(s) under %s and NONE ran in a --codex-cwd "
                         "folder; see codex_unmapped.\n" % (len(report["codex_unmapped"]), a.codex_sessions))
        return 3
    if a.codex_sessions and report["files"] == 0 and (a.codex_session_file or report.get("codex_pin_only")) and report["codex_unmapped"]:
        sys.stderr.write("transcript_cards: %d interactive Codex session(s) under %s and NONE matched "
                         "--codex-session-file; see codex_unmapped.\n"
                         % (len(report["codex_unmapped"]), a.codex_sessions))
        return 3
    if a.codex_sessions and report["files"] == 0:
        sys.stderr.write("transcript_cards: %d rollout file(s) under %s and NONE is an interactive Codex session.\n"
                         % (report["files_top_level"], a.codex_sessions))
        return 3
    if a.kimi_sessions and report["files"] == 0:
        sys.stderr.write("transcript_cards: %d wire file(s) under %s and NONE is an agents/main wire.jsonl of a "
                         "Kimi session.\n" % (report["files_top_level"], a.kimi_sessions))
        return 3
    if a.antigravity_brain and report["files"] == 0:
        sys.stderr.write("transcript_cards: %d transcript.jsonl file(s) under %s and NONE is an interactive "
                         "Antigravity session.\n" % (report["files_top_level"], a.antigravity_brain))
        return 3
    if a.project_dir and report["files"] == 0 and report["files_top_level"] > 0:
        sys.stderr.write(
            "transcript_cards: %d transcript(s) in %s and NONE was read: no user prompt holds this seat's marker "
            "(echo %s-shell-ok). Name the seat's own text with --marker, list files with --files, or take every "
            "top-level transcript with --all-files.\n" % (report["files_top_level"], a.project_dir, a.seat))
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
