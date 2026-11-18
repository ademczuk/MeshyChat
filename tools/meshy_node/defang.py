"""Frame-tag defanging and invisible/bidi character stripping for untrusted peer text.

Adapted from reason-network `src/render.rs:265-318` (`defang`, `spells_reason`) and
`src/daemon/ask.rs:381-389` (`plain`). Runs in single-pass O(N) time with a bounded
64-character visible lookahead window (`NEAR_WINDOW = 64`) and a bitmask NFA that detects
ASCII, fullwidth, NFKC/NFKD confusable, homoglyph, and leetspeak spellings of protected frame
tag prefixes (`meshy`, `reason`) even when broken up by separators, slashes, or zero-width characters.
"""
from __future__ import annotations

import unicodedata

NEAR_WINDOW = 64

#: Twenty-eight opening angle-bracket / bracket-like characters: ASCII '<' plus 27 Unicode
#: confusables (single/double angle quotation marks, math angle brackets, CJK angle/tortoise
#: brackets, small/fullwidth forms, Canadian syllabics 'ᐸ', Lisu 'ꓼ', etc.).
OPEN_ANGLES: frozenset[str] = frozenset({
    "<",       # U+003C LESS-THAN SIGN
    "\u00ab",  # U+00AB LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    "\u02c2",  # U+02C2 MODIFIER LETTER LEFT ARROWHEAD
    "\u1438",  # U+1438 CANADIAN SYLLABICS PA
    "\u2039",  # U+2039 SINGLE LEFT-POINTING ANGLE QUOTATION MARK
    "\u226e",  # U+226E NOT LESS-THAN
    "\u227a",  # U+227A PRECEDES
    "\u2282",  # U+2282 SUBSET OF
    "\u22b2",  # U+22B2 NORMAL SUBGROUP OF
    "\u2329",  # U+2329 LEFT-POINTING ANGLE BRACKET
    "\u276c",  # U+276C MEDIUM LEFT-POINTING ANGLE BRACKET ORNAMENT
    "\u276e",  # U+276E HEAVY LEFT-POINTING ANGLE QUOTATION MARK ORNAMENT
    "\u2770",  # U+2770 HEAVY LEFT-POINTING ANGLE BRACKET ORNAMENT
    "\u27e8",  # U+27E8 MATHEMATICAL LEFT ANGLE BRACKET
    "\u27ea",  # U+27EA MATHEMATICAL LEFT DOUBLE ANGLE BRACKET
    "\u27ec",  # U+27EC MATHEMATICAL LEFT WHITE TORTOISE SHELL BRACKET
    "\u27ee",  # U+27EE MATHEMATICAL LEFT FLATTENED PARENTHESIS
    "\u2985",  # U+2985 LEFT WHITE PARENTHESIS
    "\u29fc",  # U+29FC LEFT-POINTING CURVED ANGLE BRACKET
    "\u3008",  # U+3008 LEFT ANGLE BRACKET
    "\u300a",  # U+300A LEFT DOUBLE ANGLE BRACKET
    "\u3014",  # U+3014 LEFT TORTOISE SHELL BRACKET
    "\u3016",  # U+3016 LEFT WHITE LENTICULAR BRACKET
    "\u3018",  # U+3018 LEFT WHITE TORTOISE SHELL BRACKET
    "\u301a",  # U+301A LEFT WHITE SQUARE BRACKET
    "\ua4fc",  # U+A4FC LISU LETTER TONE MYA NA
    "\ufe64",  # U+FE64 SMALL LESS-THAN SIGN
    "\uff1c",  # U+FF1C FULLWIDTH LESS-THAN SIGN
})

#: Bidi overrides/isolates and invisible zero-width/format/filler characters stripped by `strip_invisible_and_bidi`.
_INVISIBLE_OR_BIDI: frozenset[int] = frozenset({
    0x00AD,  # SOFT HYPHEN
    0x034F,  # COMBINING GRAPHEME JOINER
    0x061C,  # ARABIC LETTER MARK
    0x115F,  # HANGUL CHOSEONG FILLER
    0x1160,  # HANGUL JUNGSEONG FILLER
    0x17B4,  # KHMER VOWEL INHERENT AQ
    0x17B5,  # KHMER VOWEL INHERENT AA
    0x180B,  # MONGOLIAN FREE VARIATION SELECTOR ONE
    0x180C,  # MONGOLIAN FREE VARIATION SELECTOR TWO
    0x180D,  # MONGOLIAN FREE VARIATION SELECTOR THREE
    0x180E,  # MONGOLIAN VOWEL SEPARATOR
    0x180F,  # MONGOLIAN FREE VARIATION SELECTOR FOUR
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x200E,  # LEFT-TO-RIGHT MARK
    0x200F,  # RIGHT-TO-LEFT MARK
    0x2028,  # LINE SEPARATOR
    0x2029,  # PARAGRAPH SEPARATOR
    0x202A,  # LEFT-TO-RIGHT EMBEDDING
    0x202B,  # RIGHT-TO-LEFT EMBEDDING
    0x202C,  # POP DIRECTIONAL FORMATTING
    0x202D,  # LEFT-TO-RIGHT OVERRIDE
    0x202E,  # RIGHT-TO-LEFT OVERRIDE
    0x2060,  # WORD JOINER
    0x2061,  # FUNCTION APPLICATION
    0x2062,  # INVISIBLE TIMES
    0x2063,  # INVISIBLE SEPARATOR
    0x2064,  # INVISIBLE PLUS
    0x2065,  # UNASSIGNED IN GENERAL PUNCTUATION FORMAT RANGE
    0x2066,  # LEFT-TO-RIGHT ISOLATE
    0x2067,  # RIGHT-TO-LEFT ISOLATE
    0x2068,  # FIRST STRONG ISOLATE
    0x2069,  # POP DIRECTIONAL ISOLATE
    0x206A,  # INHIBIT SYMMETRIC SWAPPING
    0x206B,  # ACTIVATE SYMMETRIC SWAPPING
    0x206C,  # INHIBIT ARABIC FORM SHAPING
    0x206D,  # ACTIVATE ARABIC FORM SHAPING
    0x206E,  # NATIONAL DIGIT SHAPES
    0x206F,  # NOMINAL DIGIT SHAPES
    0x2800,  # BRAILLE PATTERN BLANK
    0x3164,  # HANGUL FILLER
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
    0xFFA0,  # HALFWIDTH HANGUL FILLER
    0xFFF9,  # INTERLINEAR ANNOTATION ANCHOR
    0xFFFA,  # INTERLINEAR ANNOTATION SEPARATOR
    0xFFFB,  # INTERLINEAR ANNOTATION TERMINATOR
})

#: Single-character leetspeak, Cyrillic/Greek homoglyph, and Latin small-capital normalization table.
_LEET_AND_HOMOGLYPHS: dict[str, str] = {
    "0": "o",
    "1": "i",
    "!": "i",
    "|": "i",
    "3": "e",
    "4": "a",
    "@": "a",
    "5": "s",
    "$": "s",
    "7": "t",
    "8": "b",
    "9": "g",
    # Cyrillic lowercase & uppercase homoglyphs
    "\u0430": "a", "\u0410": "a",  # а / А
    "\u0435": "e", "\u0415": "e",  # е / Е
    "\u0454": "e", "\u0404": "e",  # є / Є
    "\u0438": "n", "\u0418": "n",  # и / И (visual confusable for n)
    "\u0456": "i", "\u0406": "i",  # і / І
    "\u043c": "m", "\u041c": "m",  # м / М
    "\u043d": "h", "\u041d": "h",  # н / Н
    "\u043e": "o", "\u041e": "o",  # о / О
    "\u0440": "r", "\u0420": "r",  # р / Р
    "\u0441": "s", "\u0421": "s",  # с / С (used for c/s)
    "\u0442": "t", "\u0422": "t",  # т / Т
    "\u0443": "y", "\u0423": "y",  # у / У
    "\u0445": "x", "\u0425": "x",  # х / Х
    "\u0455": "s", "\u0405": "s",  # ѕ / Ѕ
    # Greek homoglyphs
    "\u03b1": "a", "\u0391": "a",  # α / Α
    "\u03b5": "e", "\u0395": "e",  # ε / Ε
    "\u03b7": "n", "\u0397": "h",  # η / Η
    "\u03b9": "i", "\u0399": "i",  # ι / Ι
    "\u03bc": "m", "\u039c": "m",  # μ / Μ
    "\u03bd": "n", "\u039d": "n",  # ν / Ν
    "\u03bf": "o", "\u039f": "o",  # ο / Ο
    "\u03c1": "r", "\u03a1": "r",  # ρ / Ρ
    "\u03c3": "s", "\u03c2": "s", "\u03a3": "s",  # σ / ς / Σ
    "\u03c4": "t", "\u03a4": "t",  # τ / Τ
    "\u03c5": "y", "\u03a5": "y",  # υ / Υ
    # Latin small capitals & dotless/lookalike letters
    "\u0131": "i",  # ı
    "\u1d00": "a",  # ᴀ
    "\u0299": "b",  # ʙ
    "\u1d04": "c",  # ᴄ
    "\u1d05": "d",  # ᴅ
    "\u1d07": "e",  # ᴇ
    "\u0262": "g",  # ɢ
    "\u029c": "h",  # ʜ
    "\u026a": "i",  # ɪ
    "\u029f": "l",  # ʟ
    "\u1d0d": "m",  # ᴍ
    "\u0274": "n",  # ɴ
    "\u1d0f": "o",  # ᴏ
    "\u1d18": "p",  # ᴘ
    "\u0280": "r",  # ʀ
    "\ua731": "s",  # ꜱ
    "\u1d1b": "t",  # ᴛ
    "\u1d1c": "u",  # ᴜ
    "\u028f": "y",  # ʏ
    "\U0001d5f9": "r",  # math sans-serif bold confusable
}


def _is_invisible_or_bidi_char(ch: str) -> bool:
    cp = ord(ch)
    if 0x20 <= cp < 0x7F:
        return False
    if cp in _INVISIBLE_OR_BIDI:
        return True
    if 0xFE00 <= cp <= 0xFE0F:  # Variation Selectors 1..16
        return True
    if 0xE0000 <= cp <= 0xE01EF:  # Plane 14 Unicode Tag chars + Variation Selectors Supplement
        return True
    if cp < 0x20 and ch not in ("\n", "\r", "\t"):
        return True
    if 0x7F <= cp <= 0x9F:
        return True
    cat = unicodedata.category(ch)
    return cat in ("Cf", "Zl", "Zp", "Cs")


def strip_invisible_and_bidi(text: str) -> str:
    """Remove bidi overrides/isolates, zero-width/filler format chars, variation selectors,
    Unicode Tag chars, line/paragraph separators, and C0/C1 controls."""
    if not text:
        return ""
    return "".join(ch for ch in text if not _is_invisible_or_bidi_char(ch))


def _canonical_alpha(ch: str) -> str | None:
    """Map `ch` to its canonical lowercase ASCII letter, or None if it is a separator/non-letter."""
    cp = ord(ch)
    if 0x41 <= cp <= 0x5A:
        return chr(cp + 32)
    if 0x61 <= cp <= 0x7A:
        return ch
    if ch in _LEET_AND_HOMOGLYPHS:
        return _LEET_AND_HOMOGLYPHS[ch]
    # Fullwidth Latin uppercase (U+FF21..U+FF3A) and lowercase (U+FF41..U+FF5A)
    if 0xFF21 <= cp <= 0xFF3A:
        return chr(cp - 0xFF21 + 0x61)
    if 0xFF41 <= cp <= 0xFF5A:
        return chr(cp - 0xFF41 + 0x61)
    # Enclosed / Circled Latin uppercase (U+24B6..U+24CF) and lowercase (U+24D0..U+24E9)
    if 0x24B6 <= cp <= 0x24CF:
        return chr(cp - 0x24B6 + 0x61)
    if 0x24D0 <= cp <= 0x24E9:
        return chr(cp - 0x24D0 + 0x61)
    if cp > 0x7F:
        if _is_invisible_or_bidi_char(ch) or unicodedata.category(ch) in ("Mn", "Me", "Mc"):
            return None
        norm = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", ch))
        for sub in norm:
            if unicodedata.category(sub) in ("Mn", "Me", "Mc"):
                continue
            scp = ord(sub)
            if 0x41 <= scp <= 0x5A:
                return chr(scp + 32)
            if 0x61 <= scp <= 0x7A:
                return sub
            if sub in _LEET_AND_HOMOGLYPHS:
                return _LEET_AND_HOMOGLYPHS[sub]
    return None


def spells_word(window: str, target: str) -> bool:
    """Bitmask NFA over `window` (bounded by NEAR_WINDOW): returns True if `target` is spelled
    at the start of the tag in `window` with optional non-alphanumeric separators, zero-width
    chars, or repeated letters.

    Bit `k` of `state` is 1 when prefix `target[:k+1]` has been matched.
    Once the tag name begins, any non-matching letter aborts so ordinary prose after `<`
    (such as `if a<b and the reason is sound`) is never misclassified as a frame tag.
    """
    target = target.lower()
    m = len(target)
    if m == 0:
        return True
    full_mask = 1 << (m - 1)
    char_masks: dict[str, int] = {}
    for idx, ch in enumerate(target):
        char_masks[ch] = char_masks.get(ch, 0) | (1 << idx)

    state = 0
    started = False
    for ch in window:
        if ch in (">", "\uff1e", "\u27e9", "\u3009", "\u300b", "\u00bb"):
            return False
        letter = _canonical_alpha(ch)
        if letter is None:
            # Separators ('/', '-', '_', '.', whitespace, invisible/combining chars) preserve NFA progress
            continue
        mask = char_masks.get(letter, 0)
        if not started:
            started = True
            state = 1 & mask
            if state == 0:
                return False
        else:
            state = ((state << 1) & mask) | (state & mask)
            if state == 0:
                return False
        if state & full_mask:
            return True
    return False


def _visible_lookahead(body: str, start: int, max_visible: int = NEAR_WINDOW) -> str:
    """Collect up to `max_visible` non-invisible, non-whitespace characters starting at `start`
    so arbitrary padding with spaces or zero-width characters after `<` cannot push the tag name
    past `NEAR_WINDOW`."""
    buf: list[str] = []
    for idx in range(start, len(body)):
        ch = body[idx]
        if _is_invisible_or_bidi_char(ch) or ch.isspace():
            continue
        buf.append(ch)
        if len(buf) >= max_visible:
            break
    return "".join(buf)


_CLOSE_ANGLES: frozenset[str] = frozenset({">", "\uff1e", "\u27e9", "\u3009", "\u300b", "\u00bb"})


def _compile_prefix_nfa(target: str) -> tuple[str, int, dict[str, int]]:
    target = target.lower()
    m = len(target)
    full_mask = 1 << (m - 1) if m > 0 else 0
    char_masks: dict[str, int] = {}
    for idx, ch in enumerate(target):
        char_masks[ch] = char_masks.get(ch, 0) | (1 << idx)
    return target[0] if m > 0 else "", full_mask, char_masks


def _matches_active_tokens(
    vis_tok: list[str | None],
    next_active: list[int],
    first_m: int,
    limit: int,
    full_mask: int,
    char_masks: dict[str, int],
) -> bool:
    first_letter = vis_tok[first_m]
    if first_letter is None or first_letter == ">":
        return False
    state = 1 & char_masks.get(first_letter, 0)
    if state == 0:
        return False
    if state & full_mask:
        return True
    cur = next_active[first_m + 1]
    while cur < limit:
        tok = vis_tok[cur]
        if tok == ">":
            return False
        mask = char_masks.get(tok, 0)  # type: ignore[arg-type]
        state = ((state << 1) & mask) | (state & mask)
        if state == 0:
            return False
        if state & full_mask:
            return True
        cur = next_active[cur + 1]
    return False


def defang_frame_tags(body: str, protected_prefixes: tuple[str, ...] = ("meshy", "reason")) -> str:
    """Rewrite any opening angle bracket (ASCII '<' or any Unicode angle-bracket confusable in
    `OPEN_ANGLES`) to '[' when the following visible characters spell any prefix in `protected_prefixes`.

    Pre-indexes visible non-whitespace characters and their canonical alpha/close-angle tokens with a
    `next_active` jump table so arbitrary runs of spaces, NBSPs, zero-width characters, or dense `<`
    sequences after `<` are evaluated in O(N) time without repeated per-window scanning.
    """
    if not body:
        return ""
    prefixes = tuple(p.lower() for p in protected_prefixes if p)
    if not prefixes:
        return body
    if "<" not in body and not any(c in body for c in OPEN_ANGLES):
        return body

    nfas_by_first: dict[str, list[tuple[int, dict[str, int]]]] = {}
    for p in prefixes:
        first_ch, full_mask, char_masks = _compile_prefix_nfa(p)
        if first_ch:
            nfas_by_first.setdefault(first_ch, []).append((full_mask, char_masks))

    vis_pos: list[int] = []
    vis_tok: list[str | None] = []
    for idx, ch in enumerate(body):
        if not (_is_invisible_or_bidi_char(ch) or ch.isspace()):
            vis_pos.append(idx)
            if ch in _CLOSE_ANGLES:
                vis_tok.append(">")
            else:
                vis_tok.append(_canonical_alpha(ch))

    n_vis = len(vis_pos)
    next_active = [n_vis] * (n_vis + 1)
    for k in range(n_vis - 1, -1, -1):
        next_active[k] = k if vis_tok[k] is not None else next_active[k + 1]

    vp = 0
    out: list[str] = []
    for i, ch in enumerate(body):
        if ch in OPEN_ANGLES:
            while vp < n_vis and vis_pos[vp] <= i:
                vp += 1
            limit = vp + NEAR_WINDOW if vp + NEAR_WINDOW < n_vis else n_vis
            first_m = next_active[vp]
            if first_m < limit:
                first_tok = vis_tok[first_m]
                if first_tok in nfas_by_first:
                    if any(
                        _matches_active_tokens(vis_tok, next_active, first_m, limit, fm, cm)
                        for fm, cm in nfas_by_first[first_tok]
                    ):
                        out.append("[")
                        continue
        out.append(ch)
    return "".join(out)


def sanitize_untrusted_text(text: str, protected_prefixes: tuple[str, ...] = ("meshy", "reason")) -> str:
    """Strip invisible/bidi spoofing characters and defang protected `<meshy*>` / `<reason*>` frame tags."""
    if not text:
        return ""
    return defang_frame_tags(strip_invisible_and_bidi(text), protected_prefixes=protected_prefixes)
