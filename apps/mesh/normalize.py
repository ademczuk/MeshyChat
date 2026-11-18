"""One mesh row in, typed edges out. Pure: no database, no I/O, never raises on data.

THIS IS THE ONLY PLACE THE FOUR SPELLINGS ARE KNOWN. Measured on production 2026-09-21,
all of these are live in the same namespace on the same day:

    to          string (16,181 rows) or array (5,617) or null (3) or absent (1,655)
    cc          array (5,022) or string (617) or absent (17,817)
    thread      `thread_id` (13,725) or `thread` (6,166) or both (1,960) or neither (5,525)
    reply       `in_reply_to` (942) or `reply_to` (674)
    requires_ack  bool, or the strings 'true' / 'True'

Every reader in the fleet that needed these wrote its own subset of this function. Two
shared helpers each covered two of the four thread spellings (`mesh_inbox_peek.py:127`,
`gemma_mesh_ownership.py:186`), nothing anywhere read `reply_to`, and one writer
(`pong_rollcall.py:8-10`) emitted `thread`, `thread_id` AND `reply_to` into a single row
because it could not know which one a reader would check. This module exists so that
the next reader calls it instead of writing the fifth copy.

CANONICAL WINS, AND DISAGREEMENT IS COUNTED, NOT MERGED. When a row carries both
`thread_id` and `thread`, the v1 canonical (`thread_id`,
apps/memoryskills/validation.py MESH_MESSAGE_V1_REQUIRED_FIELDS) is the edge. If the
two values differ, that fact goes into `Projected.conflicts` for the caller to surface.
Emitting both as edges would assert that one message is in two threads on the strength
of a writer's hedge, which is exactly the invention this package refuses.

THE AUTHOR IS THE AUTHENTICATED LABEL, NEVER `value['from']`. Same rule as
apps/board/projection.py and for the same measured reason: a projector over the body
attributed nine acts to the peer REPORTING them. `author_label` on the row is what the
daemon stamped from the roster at write time. `value['from']` is kept only as a
diagnostic: when it disagrees with the stamped label the disagreement is counted.

TOTAL AND NON-RAISING BY CONTRACT. A projector that raises on one malformed historical
row halts the catch-up for every row after it. So every branch here returns something,
and what it could not use is counted in `Projected.skipped` with a reason, so the
projector can report it rather than silently dropping it.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import re
from typing import Any, Iterator

#: The closed set of relations. Mirrors the CHECK constraint in sql_meshychat/004.
EDGE_TYPES = ("authored_by", "addressed_to", "copied_to", "in_thread", "replies_to",
              "owes_ack", "closes", "declines", "mentions")

# Measured in 1,851 previews: 184 bare issue/PR numbers, 29 OI ids and 87 msg keys. The
# bounds keep an accidental prose number or hash-like timestamp from becoming a graph node.
_REPO_REF = re.compile(r"(?<![\w/])([A-Za-z0-9_-]{1,39})/([A-Za-z0-9_.-]{1,100})#([0-9]{1,6})(?!-[0-9])\b")
_BARE_REF = re.compile(r"(?<![\w/])(?:#([0-9]{1,6})(?!-[0-9])\b|PRs?\s+#?([0-9]{1,6})(?!-[0-9])\b|(?:issue|pull)\s+#([0-9]{1,6})(?!-[0-9])\b)", re.I)
_OI_REF = re.compile(r"(?<!\w)(OI-[0-9]{1,6})\b")
_MSG_REF = re.compile(r"(?<![\w/])(msg/[0-9]{13}-[A-Za-z0-9][A-Za-z0-9_.:-]{0,200})(?![A-Za-z0-9_.:-])")
_SHA_REF = re.compile(r"(?<!\w)([0-9a-f]{7,40})(?!\w)")
_URL = re.compile(r"https?://\S+", re.I)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_FILE_EXTENSION = (".md", ".py", ".txt", ".json", ".html", ".rs", ".ts", ".js", ".yml", ".yaml", ".toml")
_COMMIT_PREFIXES = ("nonce", "sha256", "sha-256", "sha512", "digest", "hash of", "card", "checksum", "sha256sums")


#: The src_field of the bare `gh:#N` edge a repo-qualified reference writes beside itself when the body never
#: names `#N` bare. It is an index key, so `/mentions?ref=#N` finds the qualified reference by equality, and not
#: evidence of a bare reference: a search for one repository's `#N` must not take another repository's for it.
QUALIFIED_ONLY_FIELD = "body:qualified"

#: The src_fields of the index-only keys a longer artifact reference writes beside itself, as a repo-qualified
#: reference writes `gh:#N` (issue #159): `hash:<first 8>` beside a longer hex token, and `path:<file name>` beside
#: a path with directories. A short query then finds the longer reference by equality on mesh_edges_dst_idx; the
#: key is not evidence that the body named the short form, so graph.mentions never reports it as what a row named.
HASH_PREFIX_FIELD = "body:hash-prefix"
PATH_NAME_FIELD = "body:path-name"
INDEX_ONLY_FIELDS = (QUALIFIED_ONLY_FIELD, HASH_PREFIX_FIELD, PATH_NAME_FIELD)
#: The fleet cites a digest by its first 8 characters ("sha256 09cb6def..."), so that is the shared index key.
HASH_PREFIX_LEN = 8
#: The longest `dst` an edge may carry. edges_for drops a longer one, and a path reference tests this same bound
#: before it writes its file-name key, so the two are kept or dropped together (memory-bxxxxx on #163).
MAX_DST_LEN = 512

# hash: and path: (issue #159). A hex token of 8 to 64 characters as written, in lowercase only: an uppercase run is
# more often a serial number or an ECID than a digest. The hyphen bounds keep a UUID's or a label's hex parts out.
_HASH_REF = re.compile(r"(?<![\w-])([0-9a-f]{8,64})(?![\w-])")
#: A word that says the token is a digest lets an all-digit or all-letter token through (an 8-character prefix of a
#: sha256 is all digits about once in 43); `commit:` skips these same words, because a digest is not a commit.
_HASH_WORDS = re.compile(r"\b(?:sha(?:-?(?:1|224|256|384|512))?(?:sums?)?|digests?|hash(?:es)?|checksums?)\b", re.I)
# A hex value right after an identifier's name, in prose, Markdown or pasted JSON (`peer 610170d5`,
# `peer_id: a2b5be8f3`, `"kid": "10b1dc0834865bab"`, kid `10b1...`, `kid: ["10b1..."]`), is that peer or key, not a
# digest (memory-bxxxxx on #161). The name is often the tail of a longer field (`signing_kid`, `result_signing_kid`,
# `caller_peer_id`), where `\b` finds no boundary after the `_` (memory-bxxxxx on #163): group 1 is the rest of that
# field, and the whole field must begin at the start of a word, so `skid` and `kidney` are other words and their
# hex stays a digest. After the name come its closing quotes or Markdown marks, an optional `:` or `=`, and any
# opening quotes, marks and brackets of the value: `**signing_kid**: `10b1...``, `kid: ["10b1..."]` (codex).
_HASH_SKIP_KEY = re.compile(
    r"(?<![\w.-])((?:[\w.-]*[_.-])?)(?:peer(?:_id)?|kid|key_?id)[\"'`*_]*\s*(?:[:=]\s*)?(?:[\"'`*(\[{<]\s*)*$", re.I)
#: A word in the field's name that says its value is a digest after all. It is wider than _HASH_WORDS, which also
#: decides when an all-digit token is a digest; this one only keeps a named digest out of the identifier rule.
#: `sha` counts with digits or alone, so `shared_peer` is still a peer (codex on the second follow-up).
_DIGEST_FIELD = re.compile(
    r"\b(?:md5(?:sums?)?|sha(?:-?\d[\d-]*)?(?:sums?)?|blake\d\w*|crc\d*|\w*hash(?:es)?|digests?|checksums?)\b", re.I)
#: How far back the identifier's field may start. It is read on the body itself, never on a slice: a slice that
#: began inside `skid` made a boundary of its own, and one of 20 characters lost `"signing_kid":` before an aligned
#: or wrapped value (codex on the second follow-up).
KEY_CONTEXT = 96
# A relative path or a bare file name, with an extension from this list only. There is no key, certificate or
# environment-file type on it: a mention is an index, and the fleet does not need one of where its secrets live.
_PATH_EXTENSIONS = (
    "md", "txt", "log", "csv", "tsv", "json", "jsonc", "yml", "yaml", "toml", "ini", "cfg", "conf", "xml", "plist",
    "html", "css", "svg", "png", "jpg", "pdf", "py", "sh", "bash", "zsh", "ps1", "bat", "c", "h", "cc", "cpp", "hpp",
    "rs", "go", "swift", "kt", "java", "rb", "pl", "lua", "qml", "js", "mjs", "cjs", "jsx", "ts", "tsx", "sql",
    "patch", "diff", "dts", "dtsi", "bin", "img", "iso", "efi", "tar", "gz", "tgz", "xz", "zst", "zip", "service",
    "timer", "rules", "lock", "mk", "cmake", "nix", "ipynb")
_PATH_SEGMENT = r"\.?[A-Za-z0-9_][A-Za-z0-9_.+-]{0,99}"
# No absolute or home path: the lookbehind refuses a token that starts after `/`, `~`, `.` or `\`, so `/Users/x/a.md`
# and `~/.ssh/config.toml` give nothing rather than a tail of themselves. They name one machine, not an artifact.
# The extension must end the name: a dot may follow it only as punctuation, so `README.md.old`, `config.json.key` and
# the backups `config.json~` and `README.md.~1~` are not `README.md` and `config.json` (codex on #159), while
# `v1.2.tar.gz` and a sentence's `esp-v5.sh.` still are.
_PATH_REF = re.compile(r"(?<![\w/.~\\-])((?:" + _PATH_SEGMENT + r"/){0,15}" + _PATH_SEGMENT
                       + r"\.(?i:" + "|".join(_PATH_EXTENSIONS) + r"))(?![\w/+~-])(?!\.[A-Za-z0-9_~])")
# An absolute, home or drive path as one machine writes it: a run without spaces, or a whole quoted or backticked span
# that starts like one, since such a span may hold spaces (`/Users/a/My Project/x.json`). Neither kind of reference
# is taken from inside one (codex on #159: the tail after a space read as the relative path `Project/x.json`).
_LOCAL_RUN = re.compile(r"(?<![\w.])(?:~|[A-Za-z]:)?[/\\][^\s`'\"]*")
_QUOTED = re.compile(r"`[^`\n]*`|\"[^\"\n]*\"|(?<!\w)'[^'\n]*'(?!\w)")  # an apostrophe opens no quote
_LOCAL_START = re.compile(r"(?:~|[A-Za-z]:)?[/\\]")
_HASH_ONLY = re.compile(r"[0-9a-f]{8,64}")


def mention_refs(body: object, *, own_key: str | None = None,
                 reply_to: str | None = None) -> tuple[str, ...]:
    """Canonical artifact references in a body, at most 32, in textual order (mention_ref_fields)."""
    return tuple(ref for ref, _ in mention_ref_fields(body, own_key=own_key, reply_to=reply_to))


def mention_ref_fields(body: object, *, own_key: str | None = None,
                       reply_to: str | None = None) -> tuple[tuple[str, str], ...]:
    """Canonical artifact references in a body, at most 32, in textual order, each with its src_field:
    `body`, or QUALIFIED_ONLY_FIELD for a bare `gh:#N` that only a repo-qualified reference supplied.

    Fenced code is intentionally scanned: this is an index of named artifacts, not a
    renderer that decides which prose is executable. Repo-qualified refs win before bare
    refs; SHA candidates need both a letter and a digit, which excludes English a-f words
    and 13-digit timestamps. URL paths and matched msg keys are excluded from SHA scanning.
    """
    if not isinstance(body, str):
        return ()
    found: list[tuple[int, int, str, bool]] = []  # (start, end, ref, written only as a qualified ref's index key)
    occupied: list[tuple[int, int]] = []
    for m in _REPO_REF.finditer(body):
        if m.group(2).lower().endswith(_FILE_EXTENSION):
            continue
        found.append((m.start(), m.end(), f"gh:{m.group(1).lower()}/{m.group(2).lower()}#{m.group(3)}", False))
        # Store the bare issue key too. Both `/mentions?ref=#N` and the qualified
        # spelling can then use equality on mesh_edges_dst_idx, not a collation-sensitive LIKE.
        found.append((m.start(), m.end() + 1, f"gh:#{m.group(3)}", True))
        occupied.append((m.start(), m.end()))
    for m in _BARE_REF.finditer(body):
        if any(a <= m.start() < b for a, b in occupied):
            continue
        number = m.group(1) or m.group(2) or m.group(3)
        found.append((m.start(), m.end(), f"gh:#{number}", False))
        occupied.append((m.start(), m.end()))
    for m in _OI_REF.finditer(body):
        found.append((m.start(), m.end(), f"oi:{m.group(1)}", False))
        occupied.append((m.start(), m.end()))
    for m in _MSG_REF.finditer(body):
        key = m.group(1).rstrip(".,;:!?)]}\"")
        if key != own_key and key != reply_to:
            found.append((m.start(), m.end(), f"msg:{key}", False))
        occupied.append((m.start(), m.end()))
    urls = [(m.start(), m.end()) for m in _URL.finditer(body)]
    uuids = [(m.start(), m.end()) for m in _UUID.finditer(body)]
    for m in _SHA_REF.finditer(body):
        if any(a <= m.start() < b for a, b in occupied + urls + uuids):
            continue
        value = m.group(1)
        preceding = body[max(0, m.start() - 20):m.start()].lower()
        uuid_tail = body[m.end():m.end() + 31]
        if (re.match(r"-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])", uuid_tail, re.I)
                or preceding.endswith(("peer-", "desktop-", "node-"))
                or any(prefix in preceding for prefix in _COMMIT_PREFIXES)):
            continue
        if any(c.isalpha() for c in value) and any(c.isdigit() for c in value):
            found.append((m.start(), m.end(), f"commit:{value[:7].lower()}", False))
    # A bare #N that appears anywhere is evidence, wherever a qualified reference also supplied the key.
    named = {ref for _, _, ref, index_only in found if not index_only}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, _, ref, _ in sorted(found):
        if ref not in seen:
            seen.add(ref)
            out.append((ref, "body" if ref in named else QUALIFIED_ONLY_FIELD))
            if len(out) == 32:
                break
    return tuple(out)


def _identifier_value(body: str, start: int) -> bool:
    """Is the hex token at `start` the value of a key or peer field, and so not a digest?

    A field that names a digest keeps its value: `sha256_of_peer=<hex>`, `md5_of_peer=<hex>` and
    `checksum_for_kid: <hex>` end in an identifier's name and are digests all the same (codex on the second
    follow-up).
    """
    found = _HASH_SKIP_KEY.search(body, max(0, start - KEY_CONTEXT), start)
    return bool(found) and not _DIGEST_FIELD.search(found.group(1).replace("_", " "))  # `\b` needs the space


def _excluded_spans(body: str) -> list[tuple[int, int]]:
    """The URLs, UUIDs, msg keys and local paths of a body: no digest or path reference is read inside one."""
    spans = [(m.start(), m.end()) for pattern in (_URL, _UUID, _MSG_REF, _LOCAL_RUN) for m in pattern.finditer(body)]
    return spans + [(m.start(), m.end()) for m in _QUOTED.finditer(body) if _LOCAL_START.match(body, m.start() + 1)]


def _hex_tokens(body: str, excluded: list[tuple[int, int]]) -> Iterator[tuple[int, str, bool]]:
    """(start, hex token, whether it is an identifier's value) for each hex token that is a digest, or would be one
    but for the identifier rule, in textual order."""
    for m in _HASH_REF.finditer(body):
        if any(a <= m.start() < b for a, b in excluded) or _after_local_path(body, m.start()):
            continue
        value = m.group(1)
        preceding = body[max(0, m.start() - 20):m.start()].lower()
        if "nonce" in preceding:
            continue
        if not (len(value) in (40, 64) or _HASH_WORDS.search(preceding)
                or (any(c.isalpha() for c in value) and any(c.isdigit() for c in value))):
            continue
        yield m.start(), value, _identifier_value(body, m.start())


def _digests(body: str, excluded: list[tuple[int, int]]) -> Iterator[tuple[int, str]]:
    """(start, hex token) of each digest in a body, in textual order."""
    return ((start, value) for start, value, identifier in _hex_tokens(body, excluded) if not identifier)


def digest_positions(body: object) -> list[tuple[int, str]]:
    """Where a body names the digests artifact_ref_fields WRITES: (start, hex token), in textual order. A body with
    more references than the cap of 32 loses the later ones, and a report of what a backfill would write must lose
    them too (codex on #166); a digest that is written counts at each place the body names it. For
    apps/mesh/audit.py, which shows an operator the text in front of each."""
    if not isinstance(body, str):
        return []
    written = {ref for ref, _ in artifact_ref_fields(body)}   # a candidate that is written is always named
    return [(start, value) for start, value in _digests(body, _excluded_spans(body)) if f"hash:{value}" in written]


def identifier_positions(body: object) -> list[tuple[int, str]]:
    """The other side of digest_positions: the hex tokens that would be digests but for the identifier rule, which
    reads them as a peer's or a key's id. The audit shows these too, because a digest skipped in error leaves no
    edge to find it by (memory-bxxxxx on #166)."""
    if not isinstance(body, str):
        return []
    return [(start, value) for start, value, identifier in _hex_tokens(body, _excluded_spans(body)) if identifier]


def artifact_ref_fields(body: object) -> tuple[tuple[str, str], ...]:
    """The `hash:` and `path:` references in a body (issue #159), at most 32, in textual order, each with its
    src_field: `body`, or HASH_PREFIX_FIELD / PATH_NAME_FIELD for an index-only key.

    Separate from mention_ref_fields, with its own cap, so the four older kinds stay exactly what they were: a body
    full of digests cannot push a `msg:` or `gh:` reference past the 32. `commit:` still takes a 7-to-40 hex token
    as before; `hash:` keeps the token whole, so a full sha256 or commit id is an edge for the first time, and it
    reads a token after `sha256` or `digest` as well, which `commit:` skips. URLs, UUIDs and msg keys are excluded
    from both kinds, and a nonce, a peer id or a key id from hashes.
    """
    if not isinstance(body, str):
        return ()
    excluded = _excluded_spans(body)
    # Each reference with the index key it needs: graph.mentions finds every hash: and path: row through that key,
    # so the cap keeps or drops the two together (codex on #159), never a reference no query can reach.
    groups: list[tuple[int, list[tuple[str, str | None]]]] = []  # (start, [(ref, the index-only field or None)])
    for start, value in _digests(body, excluded):
        group = [(f"hash:{value}", None)]
        if len(value) > HASH_PREFIX_LEN:
            group.append((f"hash:{value[:HASH_PREFIX_LEN]}", HASH_PREFIX_FIELD))
        groups.append((start, group))
    for m in _PATH_REF.finditer(body):
        if any(a <= m.start() < b for a, b in excluded) or _after_local_path(body, m.start()):
            continue
        path = m.group(1)
        # A bare name whose whole stem is digits is prose numbering (`3.c`, `4.h`), not a file (memory-bxxxxx on #161);
        # `1.2.3.tar.gz` and `2026.report.json` are files (codex on the follow-up).
        # A reference too long for an edge (MAX_DST_LEN) would lose itself and keep its file-name key, which no
        # query could then resolve, so the two are dropped together.
        if ("/" not in path and path.rsplit(".", 1)[0].isdigit()) or len(f"path:{path}") > MAX_DST_LEN:
            continue
        group = [(f"path:{path}", None)]
        if "/" in path:
            group.append((f"path:{path.rsplit('/', 1)[1]}", PATH_NAME_FIELD))
        groups.append((m.start(), group))
    # As for gh:#N: a short form the body names anywhere is evidence, even where a longer reference also wrote it.
    named = {ref for _, group in groups for ref, index_field in group if index_field is None}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, group in sorted(groups, key=lambda g: g[0]):
        fresh = [(ref, index_field) for ref, index_field in group if ref not in seen]
        if len(out) + len(fresh) > 32:
            break
        for ref, index_field in fresh:
            seen.add(ref)
            out.append((ref, "body" if ref in named else index_field))
    return tuple(out)


def _after_local_path(body: str, start: int) -> bool:
    """Whether a reference at `start` continues, past one space, an unquoted absolute or home path: in
    `/Users/a/My Project/x.json` the `Project/x.json` is that machine's directory, not a repository path, and so is
    the digest in `/Users/a/My 09cb6def1234/out.txt`. A reference after punctuation (`/tmp/x, docs/a.md`) or after
    a closed quote (`cp "/tmp/x" docs/a.md`, codex on #159) is a new one; an unclosed quote is still the path."""
    if not body[:start].endswith(" ") or body[:start].endswith("  "):
        return False
    previous = body[:start - 1].rsplit(None, 1)[-1] if body[:start - 1].strip() else ""
    if previous.endswith((",", ";", ":", ".", ")", "]", "`", "\"", "'")):
        return False
    return bool(_LOCAL_START.match(previous.lstrip("`'\"([")))


def all_mention_fields(body: object, *, own_key: str | None = None,
                       reply_to: str | None = None) -> tuple[tuple[str, str], ...]:
    """Every mention a body projects: the four older kinds (mention_ref_fields), then hash: and path:."""
    return mention_ref_fields(body, own_key=own_key, reply_to=reply_to) + artifact_ref_fields(body)


def canonical_mention_ref(raw: object) -> str | None:
    """The route's one-reference parser, sharing mention_refs' canonical vocabulary."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().rstrip(".,;:!?)]}\"")
    # hash: and path: (issue #159) parse only from what artifact_ref_fields writes: lowercase hex of 8 to 64, or a
    # relative path with a listed extension. A query in uppercase hex is the same digest, so it is lowered.
    if text.startswith("hash:"):
        payload = text[len("hash:"):].lower()
        return f"hash:{payload}" if _HASH_ONLY.fullmatch(payload) else None
    if text.startswith("path:"):
        match = _PATH_REF.fullmatch(text[len("path:"):])
        return f"path:{match.group(1)}" if match else None
    # The API returns canonical prefixed refs. Accept them verbatim-equivalent so a
    # caller can paste a response's `ref` or `also_matched` value into its next query.
    for prefix, pattern, render in (
            ("gh:", None, None), ("commit:", _SHA_REF, None),
            ("oi:", _OI_REF, None), ("msg:", _MSG_REF, None)):
        if text.startswith(prefix):
            payload = text[len(prefix):]
            if prefix == "gh:":
                repo = _REPO_REF.fullmatch(payload)
                bare = _BARE_REF.fullmatch(payload)
                if repo:
                    return f"gh:{repo.group(1).lower()}/{repo.group(2).lower()}#{repo.group(3)}"
                if bare:
                    return f"gh:#{bare.group(1) or bare.group(2) or bare.group(3)}"
                return None
            match = pattern.fullmatch(payload)
            # The prefix says it is a commit, so no letter is needed: the index writes commit:1234567 for a sha
            # whose first seven are digits, and that ref must query back (codex on #93). Bare text still needs both.
            if prefix == "commit:" and match:
                return f"commit:{match.group(1)[:7].lower()}"
            if prefix == "oi:" and match:
                return f"oi:{match.group(1)}"
            if prefix == "msg:" and match:
                return f"msg:{match.group(1)}"
            return None
    m = _REPO_REF.fullmatch(text)
    if m:
        return f"gh:{m.group(1).lower()}/{m.group(2).lower()}#{m.group(3)}"
    m = _BARE_REF.fullmatch(text)
    if m:
        # three alternatives, three groups: `#N`, `PR N`, and `issue N` / `pull N` (codex on #89: the
        # third was never read, so `issue #73` became gh:#None and matched nothing)
        return f"gh:#{m.group(1) or m.group(2) or m.group(3)}"
    m = _OI_REF.fullmatch(text)
    if m:
        return f"oi:{m.group(1)}"
    m = _MSG_REF.fullmatch(text)
    if m:
        return f"msg:{m.group(1)}"
    m = _SHA_REF.fullmatch(text)
    if m and any(c.isalpha() for c in m.group(1)) and any(c.isdigit() for c in m.group(1)):
        return f"commit:{m.group(1)[:7].lower()}"
    # A bare token of 7 to 40 hex characters stays a commit, as above; only a longer one, which no commit id is,
    # is read as a hash. Any length can be asked for with the `hash:` prefix.
    m = _HASH_ONLY.fullmatch(text)
    if m and len(text) > 40:
        return f"hash:{text}"
    m = _PATH_REF.fullmatch(text)
    if m:
        return f"path:{m.group(1)}"
    return None


@dataclasses.dataclass(frozen=True)
class Edge:
    edge_type: str
    dst: str
    #: The spelling and shape the writer used, e.g. "to:string", "thread", "reply_to".
    src_field: str


@dataclasses.dataclass(frozen=True)
class Projected:
    """Everything one row yields, plus what it could not."""

    edges: tuple[Edge, ...]
    #: reason -> count, for values that were present but unusable (a non-string
    #: recipient, an empty label, a `reply_to` that is not a msg/ key).
    skipped: dict[str, int]
    #: Named disagreements between two spellings of one relation on the same row.
    conflicts: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Field readers. Each returns (value_or_None, shape_label).
# --------------------------------------------------------------------------- #

def _labels(raw: Any, field: str) -> tuple[list[str], str, dict[str, int]]:
    """A recipient field as a list of labels, plus the shape it arrived in.

    Shapes: `<field>:array`, `<field>:string`, `<field>:absent`, `<field>:null`,
    `<field>:invalid`. Non-string elements inside an array are skipped and counted; an
    empty string is skipped and counted; duplicates are collapsed because two edges to
    one recipient are one relation.
    """
    skipped: dict[str, int] = {}
    if raw is None:
        return [], f"{field}:null", skipped
    if isinstance(raw, str):
        label = raw.strip()
        if not label:
            skipped[f"{field}:empty"] = 1
            return [], f"{field}:string", skipped
        return [label], f"{field}:string", skipped
    if isinstance(raw, list):
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                skipped[f"{field}:non_string_element"] = skipped.get(
                    f"{field}:non_string_element", 0) + 1
                continue
            label = item.strip()
            if not label:
                skipped[f"{field}:empty"] = skipped.get(f"{field}:empty", 0) + 1
                continue
            if label in seen:
                continue
            seen.add(label)
            out.append(label)
        return out, f"{field}:array", skipped
    skipped[f"{field}:invalid_type"] = 1
    return [], f"{field}:invalid", skipped


def recipients(value: dict, field: str) -> tuple[list[str], str, dict[str, int]]:
    """`to` or `cc` as labels, or ([], '<field>:absent', {}) when the key is missing."""
    if field not in value:
        return [], f"{field}:absent", {}
    return _labels(value.get(field), field)


def thread_of(value: dict) -> tuple[str | None, str, str | None]:
    """(thread, src_field, conflict). Canonical `thread_id` wins over legacy `thread`.

    `conflict` is a sentence when both are present and differ, else None.
    """
    canonical = value.get("thread_id")
    legacy = value.get("thread")
    c_ok = isinstance(canonical, str) and canonical.strip()
    l_ok = isinstance(legacy, str) and legacy.strip()
    if c_ok:
        conflict = None
        if l_ok and legacy.strip() != canonical.strip():
            conflict = (f"thread_id={canonical.strip()!r} and thread={legacy.strip()!r} "
                        "disagree; the canonical thread_id was projected")
        return canonical.strip(), "thread_id", conflict
    if l_ok:
        return legacy.strip(), "thread", None
    return None, "absent", None


def reply_to_of(value: dict) -> tuple[str | None, str, str | None]:
    """(msg key, src_field, conflict). Canonical `in_reply_to` wins over legacy `reply_to`.

    Either spelling must name a `msg/` key to count; a thread id in `reply_to` (which
    `pong_rollcall.py` writes) is not a reply pointer and is refused with a reason.
    """
    canonical = value.get("in_reply_to")
    legacy = value.get("reply_to")

    def usable(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip().startswith("msg/") else None

    c, l = usable(canonical), usable(legacy)
    if c:
        conflict = None
        if l and l != c:
            conflict = (f"in_reply_to={c!r} and reply_to={l!r} disagree; the canonical "
                        "in_reply_to was projected")
        return c, "in_reply_to", conflict
    if l:
        return l, "reply_to", None
    return None, "absent", None


def requires_ack(value: dict) -> bool:
    """True for the bool True and for the legacy strings 'true' / 'True'. Nothing else.

    `type(x) is bool` rather than truthiness: the string 'false' is truthy and is not an
    ack requirement, and the integer 1 is not a claim anybody made.
    """
    raw = value.get("requires_ack")
    if type(raw) is bool:
        return raw
    return isinstance(raw, str) and raw.strip() in ("true", "True")


def closes_of(value: dict) -> tuple[list[str], dict[str, int]]:
    """Explicit settlement targets from `closes`, preserving the projector's totality.

    The write seam accepts only non-empty, distinct msg/ keys with an in-band epoch-ms
    segment. Historical rows bypassed that seam, so projection applies the unambiguous
    subset and counts every non-msg value instead of letting one malformed close halt the
    commit-order walk. The edge's primary key absorbs a repeated historical target.
    """
    if "closes" not in value:
        return [], {}
    raw = value.get("closes")
    if not isinstance(raw, list):
        return [], {"closes:not_an_array": 1}
    out: list[str] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.startswith("msg/"):
            skipped["closes:not_a_msg_key"] = skipped.get("closes:not_a_msg_key", 0) + 1
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out, skipped


def declines_of(value: dict) -> tuple[list[str], dict[str, int]]:
    """Explicit decline targets from `declines`, mirroring `closes_of`."""
    if "declines" not in value:
        return [], {}
    raw = value.get("declines")
    if not isinstance(raw, list):
        return [], {"declines:not_an_array": 1}
    out: list[str] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.startswith("msg/"):
            skipped["declines:not_a_msg_key"] = skipped.get("declines:not_a_msg_key", 0) + 1
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out, skipped


# --------------------------------------------------------------------------- #
# The projection of one row
# --------------------------------------------------------------------------- #

def edges_for(value: Any, *, author_label: str | None) -> Projected:
    """Every edge one mesh row yields. `author_label` is the row's stamped label.

    A value that is not an object yields nothing and says so. That is the honest answer
    for the 1,655 rows in the namespace that carry no `to`/`body` at all (cursors and
    state rows): they are not messages and produce no message edges.
    """
    if not isinstance(value, dict):
        return Projected(edges=(), skipped={"value:not_an_object": 1}, conflicts=())

    edges: list[Edge] = []
    skipped: dict[str, int] = {}
    conflicts: list[str] = []

    def _merge(extra: dict[str, int]) -> None:
        for k, n in extra.items():
            skipped[k] = skipped.get(k, 0) + n

    # authored_by: the AUTHENTICATED label. `from` is checked only to count disagreement.
    if isinstance(author_label, str) and author_label.strip():
        edges.append(Edge("authored_by", author_label.strip(), "author_label"))
        claimed = value.get("from")
        if isinstance(claimed, str) and claimed.strip() and claimed.strip() != author_label.strip():
            skipped["from:disagrees_with_author_label"] = 1
    else:
        skipped["author_label:absent"] = 1

    to_labels, to_shape, to_skipped = recipients(value, "to")
    _merge(to_skipped)
    for label in to_labels:
        edges.append(Edge("addressed_to", label, to_shape))

    cc_labels, cc_shape, cc_skipped = recipients(value, "cc")
    _merge(cc_skipped)
    for label in cc_labels:
        # A label in both `to` and `cc` is one recipient with two edges of different
        # types; the primary key (src, type, dst) keeps them distinct on purpose.
        edges.append(Edge("copied_to", label, cc_shape))

    thread, thread_field, thread_conflict = thread_of(value)
    if thread:
        edges.append(Edge("in_thread", thread, thread_field))
    if thread_conflict:
        conflicts.append(thread_conflict)

    reply, reply_field, reply_conflict = reply_to_of(value)
    if reply:
        edges.append(Edge("replies_to", reply, reply_field))
    if reply_conflict:
        conflicts.append(reply_conflict)
    elif reply is None and "reply_to" in value and isinstance(value.get("reply_to"), str):
        # Present, a string, and not a msg/ key: pong_rollcall's thread-in-reply_to shape.
        skipped["reply_to:not_a_msg_key"] = 1

    # A tombstone keeps `requires_ack` among its routing fields (sql_meshychat/034's key list), but a
    # redacted ask is no longer an ask: nobody owes an answer to withdrawn text. The redaction deletes
    # the row's owes_ack edges (redact.delete_derived, asks=True), and this keeps a rebuild from
    # writing them back, so incremental and rebuilt state agree.
    if requires_ack(value) and value.get("redacted") is True:
        skipped["requires_ack:redacted"] = 1
    elif requires_ack(value):
        for label in to_labels:
            edges.append(Edge("owes_ack", label, to_shape))
        if not to_labels:
            skipped["requires_ack:no_recipients"] = 1

    closes, closes_skipped = closes_of(value)
    _merge(closes_skipped)
    closes_set = set(closes)
    for close in closes:
        edges.append(Edge("closes", close, "closes"))

    declines, declines_skipped = declines_of(value)
    _merge(declines_skipped)
    for decline in declines:
        if decline in closes_set:
            skipped["declines:also_closed"] = skipped.get("declines:also_closed", 0) + 1
            continue
        edges.append(Edge("declines", decline, "declines"))

    for mention, field in all_mention_fields(value.get("body"), own_key=None, reply_to=reply):
        edges.append(Edge("mentions", mention, field))

    bounded: list[Edge] = []
    for edge in edges:
        if len(edge.dst) > MAX_DST_LEN:
            skipped["dst:too_long"] = skipped.get("dst:too_long", 0) + 1
            continue
        bounded.append(edge)
    return Projected(edges=tuple(bounded), skipped=skipped, conflicts=tuple(conflicts))


@dataclasses.dataclass(frozen=True)
class RowEdges:
    """`edges_for` bound to the row it came from, ready to insert."""

    src_id: str
    src_key: str
    src_version: int
    recorded_at: _dt.datetime
    projected: Projected

    def rows(self) -> list[tuple]:
        """(src_id, src_key, src_version, edge_type, dst, src_field, recorded_at) tuples."""
        return [
            (self.src_id, self.src_key, self.src_version, e.edge_type, e.dst, e.src_field,
             self.recorded_at)
            for e in self.projected.edges
        ]


def project_row(row: dict, *, mentions_allowed: bool = True) -> RowEdges:
    """A `shared_memory` row (dict_row) to its edges. Keys: id, key, version, value,
    author_label, created_at. The projection, not this pure normalizer, decides whether
    body-derived mentions are allowed by embed policy."""
    return RowEdges(
        src_id=str(row["id"]),
        src_key=str(row["key"]),
        src_version=int(row["version"]),
        recorded_at=row["created_at"],
        projected=_edges_for_row(row, mentions_allowed=mentions_allowed),
    )


def _edges_for_row(row: dict, *, mentions_allowed: bool = True) -> Projected:
    """Bind the self-key exclusion only where the complete stored row is available."""
    projected = edges_for(row.get("value"), author_label=row.get("author_label"))
    value = row.get("value")
    if not isinstance(value, dict):
        return projected
    reply, _, _ = reply_to_of(value)
    mentions = (tuple(Edge("mentions", ref, field)
                      for ref, field in all_mention_fields(value.get("body"), own_key=str(row["key"]), reply_to=reply))
                if mentions_allowed else ())
    kept = tuple(edge for edge in projected.edges if edge.edge_type != "mentions")
    return Projected(edges=kept + mentions, skipped=projected.skipped, conflicts=projected.conflicts)
