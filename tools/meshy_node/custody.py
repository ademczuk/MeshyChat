"""Archive custody, tar/zip member safety, inner manifest verification, and test-input provenance.

Addresses Fleet Pain (c) (1,525 mesh messages and 124+ inline SHA-256 / tarfile scripts):
1. Outer file custody: opens regular files without following symlinks/reparse points (`offer.open_regular`),
   computes outer `sha256` and `size_bytes`, and verifies `expected_sha256` when supplied.
2. Streaming member safety (`E_BUNDLE_UNSAFE_MEMBER`): inspects `.tar.gz`/`.tgz`/`.tar` and `.zip`
   archives in streaming mode and refuses symlinks, hardlinks, FIFOs, character/block devices,
   sparse members, absolute paths, Windows drive letters, UNC paths, `..` traversal segments,
   control/format characters, backslashes, and NFC case-folded duplicate member paths.
3. Inner `SHA256SUMS` and `MANIFEST.json` reconciliation (`E_BUNDLE_MANIFEST_MISMATCH`): automatically
   parses inner manifest files when present and verifies 1-to-1 member digest coverage, reporting
   `missing_from_manifest`, `missing_from_archive`, and `digest_mismatches`.
4. Test-input provenance (`E_PROVENANCE_STALE_INPUT`): cross-checks recorded test input file
   SHA-256 digests (`inputs` / `AFTER` / `files`) against the archive's actual source file SHA-256
   digests so stale pre-fix test outputs cannot be attributed to current code.
"""
from __future__ import annotations

import bz2
import hashlib
import io
import json
import lzma
import os
import re
import struct
import tarfile
import unicodedata
import zipfile
import zlib
from pathlib import Path

from . import node, offer

E_BUNDLE_NOT_REGULAR = "E_BUNDLE_NOT_REGULAR"
E_BUNDLE_HASH_MISMATCH = "E_BUNDLE_HASH_MISMATCH"
E_BUNDLE_FORMAT = "E_BUNDLE_FORMAT"
E_BUNDLE_UNSAFE_MEMBER = "E_BUNDLE_UNSAFE_MEMBER"
E_BUNDLE_MANIFEST_MISMATCH = "E_BUNDLE_MANIFEST_MISMATCH"
E_MANIFEST_INVALID = "E_MANIFEST_INVALID"
E_PROVENANCE_STALE_INPUT = "E_PROVENANCE_STALE_INPUT"
E_PROVENANCE_INVALID = "E_PROVENANCE_INVALID"

MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024  # 256 MiB
MAX_MEMBER_COUNT = 10_000
MAX_PATH_LENGTH = 1024
MAX_PATH_DEPTH = 64

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SHA256SUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+[* ]?(.+)$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024

_MANIFEST_BASENAMES = frozenset({"sha256sums", "sha256sums.txt", "manifest.json"})
_WINDOWS_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


def _loads_no_duplicates(text: str) -> object:
    """Parse JSON while rejecting duplicate keys in any JSON object."""
    def _hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in pairs:
            if k in out:
                raise ValueError(f"duplicate JSON key: {k!r}")
            out[k] = v
        return out

    return json.loads(text, object_pairs_hook=_hook)


def _nfc_casefold(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def validate_member_path(raw_name: str) -> tuple[str | None, str | None]:
    """Validate and normalize an archive member path.

    Returns `(normalized_posix_path, None)` when safe, or `(None, reason)` when unsafe.
    """
    if not isinstance(raw_name, str) or not raw_name:
        return None, "empty member name"
    if len(raw_name) > MAX_PATH_LENGTH:
        return None, f"member path exceeds {MAX_PATH_LENGTH} character limit"
    for ch in raw_name:
        cp = ord(ch)
        if cp < 0x20 or (0x7F <= cp <= 0x9F) or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp", "Cs"):
            return None, f"control or format character U+{cp:04X} in path"
    if "\\" in raw_name:
        return None, "backslash path separator is not allowed"
    if ":" in raw_name:
        return None, "Windows drive prefix or colon stream is not allowed"
    if raw_name.startswith("/") or raw_name.startswith("//"):
        return None, "absolute or UNC path is not allowed"
    if _DRIVE_RE.match(raw_name):
        return None, "Windows drive prefix is not allowed"

    trimmed = raw_name
    while trimmed.startswith("./"):
        trimmed = trimmed[2:]
    trimmed = trimmed.rstrip("/")
    if not trimmed:
        return None, "empty normalized path"
    if _DRIVE_RE.match(trimmed) or trimmed.startswith("/"):
        return None, "leading root or drive after normalization"

    parts = trimmed.split("/")
    if len(parts) > MAX_PATH_DEPTH:
        return None, f"member path exceeds {MAX_PATH_DEPTH} directory segments"
    for seg in parts:
        if not seg or seg == "." or seg == "..":
            return None, f"unsafe path segment {seg!r}"
        if _DRIVE_RE.match(seg):
            return None, f"Windows drive segment {seg!r}"
        if seg.endswith(".") or seg.endswith(" "):
            return None, f"trailing dot or space in path segment {seg!r}"
        stem = seg.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED_NAMES:
            return None, f"Windows reserved device name {seg!r}"
    return "/".join(parts), None


def _parse_provenance_digests(data: object) -> tuple[dict[str, str], str | None]:
    """Strictly parse and validate a `{path: sha256}` test-input provenance map.

    Returns `(digests, error_reason)`. Any non-dict root, empty map, or malformed entry returns an error.
    """
    if not isinstance(data, dict) or not data:
        return {}, "provenance map must be a non-empty JSON object"

    out: dict[str, str] = {}
    container_keys = ("input_digests", "inputs", "file_hashes", "source_hashes", "before_after", "AFTER", "after")
    present_containers = [k for k in container_keys if k in data]

    if present_containers:
        allowed_top = set(container_keys) | {"BEFORE", "before", "version", "schema"}
        for top_k in data:
            if top_k not in allowed_top:
                return {}, f"unexpected top-level key {top_k!r} alongside provenance container"
        for key in present_containers:
            sub = data.get(key)
            if not isinstance(sub, dict):
                return {}, f"provenance field {key!r} must be a JSON object"
            if key == "before_after" and ("AFTER" in sub or "after" in sub):
                after_sub = sub.get("AFTER") if "AFTER" in sub else sub.get("after")
                if not isinstance(after_sub, dict):
                    return {}, "provenance before_after.AFTER must be a JSON object"
                sub = after_sub
            if not sub:
                return {}, f"provenance field {key!r} contains no entries"
            for k, v in sub.items():
                if not isinstance(k, str) or not k.strip():
                    return {}, f"invalid provenance file key {k!r}"
                norm_k = k
                while norm_k.startswith("./"):
                    norm_k = norm_k[2:]
                if not norm_k:
                    return {}, f"empty normalized provenance file key {k!r}"
                dig: str | None = None
                if isinstance(v, str) and _HEX64_RE.match(v):
                    dig = v.lower()
                elif isinstance(v, dict):
                    raw_dig = v.get("sha256") if "sha256" in v else (v.get("after") if "after" in v else v.get("AFTER"))
                    if isinstance(raw_dig, str) and _HEX64_RE.match(raw_dig):
                        dig = raw_dig.lower()
                if dig is None:
                    return {}, f"invalid sha256 digest for provenance input {norm_k!r}"
                if norm_k in out and out[norm_k] != dig:
                    return {}, f"conflicting sha256 digests for provenance input {norm_k!r}"
                out[norm_k] = dig
    else:
        for k, v in data.items():
            if not isinstance(k, str) or not k.strip():
                return {}, f"invalid provenance file key {k!r}"
            norm_k = k
            while norm_k.startswith("./"):
                norm_k = norm_k[2:]
            if not norm_k:
                return {}, f"empty normalized provenance file key {k!r}"
            if not (isinstance(v, str) and _HEX64_RE.match(v)):
                return {}, f"invalid sha256 digest for provenance input {norm_k!r}"
            dig = v.lower()
            if norm_k in out and out[norm_k] != dig:
                return {}, f"conflicting sha256 digests for provenance input {norm_k!r}"
            out[norm_k] = dig

    if not out:
        return {}, "provenance map contains no valid file digest entries"
    return out, None


def _is_manifest_member(norm_path: str) -> bool:
    if "/" in norm_path:
        return False
    return norm_path.casefold() in ("sha256sums", "sha256sums.txt", "manifest.json")


def _single_common_top_dir(members: dict[str, dict]) -> str | None:
    """Return the single common top-level directory `<top>` only when EVERY non-manifest member
    in the archive sits under `<top>/...` (never when root-level files also exist)."""
    non_manifests = [m for m in members if not _is_manifest_member(m)]
    if not non_manifests or not all("/" in m for m in non_manifests):
        return None
    top_dirs = {m.split("/", 1)[0] for m in non_manifests}
    return next(iter(top_dirs)) if len(top_dirs) == 1 else None


def _resolve_archive_member_key(rel_path: str, members: dict[str, dict], manifest_dir: str = "") -> str | None:
    """Resolve a manifest or provenance path against `members` (manifest-relative first when
    `manifest_dir` is set, then exact match or single common top-level directory prefix).
    """
    norm = rel_path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    top = _single_common_top_dir(members)
    if manifest_dir:
        m_prefix = manifest_dir.rstrip("/")
        prefixed = f"{m_prefix}/{norm}"
        if prefixed in members:
            return prefixed
        if norm.startswith(m_prefix + "/") and norm in members:
            return norm
        if top is not None:
            candidate = f"{top}/{norm}"
            if candidate.startswith(m_prefix + "/") and candidate in members:
                return candidate
        return None
    if norm in members:
        return norm
    if top is not None:
        candidate = f"{top}/{norm}"
        if candidate in members:
            return candidate
    return None


def _parse_sha256sums_entries(text: str) -> tuple[list[tuple[str, str]], str | None]:
    """Strictly parse `SHA256SUMS` lines into `[(rel_path, sha256), ...]`, returning `(entries, error)`."""
    if any(ch in text for ch in ("\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x00")):
        return [], "SHA256SUMS contains forbidden line-separator or control character"
    entries: list[tuple[str, str]] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r").strip()
        if not line or line.startswith("#"):
            continue
        m = _SHA256SUM_LINE_RE.match(line)
        if not m:
            return [], f"malformed SHA256SUMS line: {line!r}"
        digest, raw_path = m.group(1).lower(), m.group(2).strip()
        while raw_path.startswith("./"):
            raw_path = raw_path[2:]
        if not raw_path:
            return [], "empty path in SHA256SUMS line"
        entries.append((raw_path, digest))
    if not entries:
        return [], "SHA256SUMS contains no valid entries"
    return entries, None


def _parse_manifest_json_entries(text: str) -> tuple[list[tuple[str, str]], str | None]:
    """Strictly parse `MANIFEST.json` into `[(rel_path, sha256), ...]`, returning `(entries, error)`."""
    try:
        data = _loads_no_duplicates(text)
    except ValueError as exc:
        return [], f"invalid JSON in MANIFEST.json: {exc}"
    if not isinstance(data, dict):
        return [], "MANIFEST.json root must be a JSON object"
    if "files" in data and "members" in data:
        return [], "MANIFEST.json cannot contain both 'files' and 'members'"

    entries: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    if "files" in data or "members" in data:
        files_node = data.get("files") if "files" in data else data.get("members")
        if isinstance(files_node, dict):
            for k, v in files_node.items():
                if not isinstance(k, str) or not k.strip():
                    return [], f"invalid file key {k!r} in MANIFEST.json"
                norm_k = k
                while norm_k.startswith("./"):
                    norm_k = norm_k[2:]
                if norm_k in seen_paths:
                    return [], f"duplicate normalized file key {norm_k!r} in MANIFEST.json"
                seen_paths.add(norm_k)
                if isinstance(v, str) and _HEX64_RE.match(v):
                    entries.append((norm_k, v.lower()))
                elif isinstance(v, dict):
                    dig = v.get("sha256") or v.get("digest")
                    if isinstance(dig, str) and _HEX64_RE.match(dig):
                        entries.append((norm_k, dig.lower()))
                    else:
                        return [], f"missing or invalid sha256 for {norm_k!r} in MANIFEST.json"
                else:
                    return [], f"invalid digest entry for {norm_k!r} in MANIFEST.json"
        elif isinstance(files_node, list):
            for item in files_node:
                if not isinstance(item, dict):
                    return [], "MANIFEST.json files list element must be an object"
                p = item.get("path") or item.get("name") or item.get("file")
                dig = item.get("sha256") or item.get("digest")
                if not (isinstance(p, str) and p.strip() and isinstance(dig, str) and _HEX64_RE.match(dig)):
                    return [], f"invalid file item {item!r} in MANIFEST.json"
                norm_p = p
                while norm_p.startswith("./"):
                    norm_p = norm_p[2:]
                if norm_p in seen_paths:
                    return [], f"duplicate file path {norm_p!r} in MANIFEST.json"
                seen_paths.add(norm_p)
                entries.append((norm_p, dig.lower()))
        else:
            return [], "MANIFEST.json files/members field must be an object or array"
    else:
        for k, v in data.items():
            if k in ("sha256", "bundle_sha256", "archive_sha256", "tar_sha256", "version", "created_at"):
                continue
            if isinstance(k, str) and isinstance(v, str) and _HEX64_RE.match(v):
                norm_k = k
                while norm_k.startswith("./"):
                    norm_k = norm_k[2:]
                if norm_k in seen_paths:
                    return [], f"duplicate normalized file key {norm_k!r} in MANIFEST.json"
                seen_paths.add(norm_k)
                entries.append((norm_k, v.lower()))
            else:
                return [], f"unrecognized or invalid MANIFEST.json entry {k!r}"

    if not entries:
        return [], "MANIFEST.json contains no file digest entries"
    return entries, None


def _check_and_record_tree_path(
    norm_path: str,
    *,
    is_dir: bool,
    seen_files_folded: dict[str, str],
    seen_dirs_folded: dict[str, str],
) -> str | None:
    """Detect NFC case-folded duplicate paths and file-vs-directory ancestor collisions.

    Returns a refusal reason string when `norm_path` collides with an earlier file or directory,
    or `None` when safe (recording `norm_path` and its ancestor directories).
    """
    parts = norm_path.split("/")
    ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]

    for anc in ancestors:
        anc_folded = _nfc_casefold(anc)
        if anc_folded in seen_files_folded:
            return f"file_dir_conflict_with:{seen_files_folded[anc_folded]}"

    folded = _nfc_casefold(norm_path)
    if is_dir:
        if folded in seen_files_folded:
            return f"file_dir_conflict_with:{seen_files_folded[folded]}"
        for anc in ancestors:
            seen_dirs_folded.setdefault(_nfc_casefold(anc), norm_path)
        seen_dirs_folded.setdefault(folded, norm_path)
        return None

    if folded in seen_files_folded:
        return f"case-folded duplicate path colliding with {seen_files_folded[folded]!r}"
    if folded in seen_dirs_folded:
        return f"file_dir_conflict_with:{seen_dirs_folded[folded]}"

    for anc in ancestors:
        seen_dirs_folded.setdefault(_nfc_casefold(anc), norm_path)
    seen_files_folded[folded] = norm_path
    return None


class _SingleStreamDecompressor(io.RawIOBase):
    """Decompresses a single gzip/bzip2/xz stream without chaining concatenated streams,
    enforcing `MAX_DECOMPRESSED_BYTES` and exposing `unused_data` / `eof` for trailing-data checks."""

    def __init__(self, raw_stream: io.BufferedReader, kind: str) -> None:
        self._raw = raw_stream
        self._kind = kind
        if kind == "gzip":
            self._decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif kind == "bz2":
            self._decomp = bz2.BZ2Decompressor()
        elif kind == "xz":
            self._decomp = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        else:
            raise ValueError(f"unsupported compression kind {kind!r}")
        self._buf = bytearray()
        self._recent = bytearray()
        self.total_out = 0
        self.emitted_bytes = 0
        self.eof = False

    def readable(self) -> bool:
        return True

    def readinto(self, b: memoryview | bytearray) -> int:
        while not self._buf and not self.eof:
            if getattr(self._decomp, "eof", False):
                self.eof = True
                break
            chunk = self._raw.read(_CHUNK_BYTES)
            if not chunk:
                if self._kind == "gzip":
                    tail = self._decomp.flush()
                    if tail:
                        self._buf.extend(tail)
                if not getattr(self._decomp, "eof", False):
                    raise tarfile.ReadError("truncated compressed archive stream")
                self.eof = True
                break
            data = self._decomp.decompress(chunk)
            if data:
                self.total_out += len(data)
                if self.total_out > MAX_DECOMPRESSED_BYTES:
                    raise tarfile.ReadError(
                        f"decompressed archive exceeds {MAX_DECOMPRESSED_BYTES} byte limit"
                    )
                self._buf.extend(data)
            if getattr(self._decomp, "eof", False):
                if self._kind == "gzip":
                    tail = self._decomp.flush()
                    if tail:
                        self.total_out += len(tail)
                        self._buf.extend(tail)
                self.eof = True
                break
        n = min(len(b), len(self._buf))
        if n == 0:
            return 0
        b[:n] = self._buf[:n]
        self._recent.extend(self._buf[:n])
        del self._buf[:n]
        self.emitted_bytes += n
        max_recent = _CHUNK_BYTES * 4
        if len(self._recent) > max_recent:
            del self._recent[: len(self._recent) - max_recent]
        return n

    def verify_clean_eof(self, end_offset: int | None = None) -> None:
        """Drain remaining decompressed tar padding (including any bytes already buffered ahead
        past `end_offset` by `tarfile` or `BufferedReader`) and verify zero trailing non-zero bytes
        and zero trailing compressed members."""
        if isinstance(end_offset, int) and self.emitted_bytes > end_offset:
            already_read = self.emitted_bytes - end_offset
            if already_read > len(self._recent):
                raise tarfile.ReadError("uninspected buffered data after tar end-of-archive marker")
            if any(byte != 0 for byte in self._recent[-already_read:]):
                raise tarfile.ReadError("trailing non-zero data after tar end-of-archive marker")
        while True:
            rem = self.read(_CHUNK_BYTES)
            if not rem:
                break
            if any(byte != 0 for byte in rem):
                raise tarfile.ReadError("trailing non-zero data after tar end-of-archive marker")
        unused = getattr(self._decomp, "unused_data", b"")
        extra = self._raw.read(1)
        if unused or extra:
            raise tarfile.ReadError("trailing data or concatenated compressed stream after end of archive")


def _scan_tar(stream: io.BufferedReader) -> tuple[dict[str, dict], list[dict], dict[str, tuple[str, str | None, str | None]]]:
    """Stream-scan a tar/tar.gz/tar.bz2/tar.xz archive. Returns `(members, unsafe_members, captured_manifests)`."""
    members: dict[str, dict] = {}
    unsafe: list[dict] = []
    manifests: dict[str, tuple[str, str | None, str | None]] = {}
    seen_files_folded: dict[str, str] = {}
    seen_dirs_folded: dict[str, str] = {}

    stream.seek(0)
    magic = stream.peek(6)[:6]
    decomp_raw: _SingleStreamDecompressor | None = None
    if magic.startswith(b"\x1f\x8b"):
        decomp_raw = _SingleStreamDecompressor(stream, "gzip")
    elif magic.startswith(b"BZh"):
        decomp_raw = _SingleStreamDecompressor(stream, "bz2")
    elif magic.startswith(b"\xfd7zXZ\x00"):
        decomp_raw = _SingleStreamDecompressor(stream, "xz")

    if decomp_raw is not None:
        tar_source: io.BufferedReader = io.BufferedReader(decomp_raw, buffer_size=_CHUNK_BYTES)
        tar_mode = "r|"
    else:
        tar_source = stream
        tar_mode = "r:"

    total_member_bytes = 0
    entry_count = 0

    with tarfile.open(fileobj=tar_source, mode=tar_mode) as tf:
        for info in tf:
            entry_count += 1
            if entry_count > MAX_MEMBER_COUNT:
                raise tarfile.ReadError(f"archive exceeds {MAX_MEMBER_COUNT} member limit")

            raw_name = info.name
            pax = getattr(info, "pax_headers", None) or {}
            if (
                info.issparse()
                or getattr(info, "sparse", None) is not None
                or any(k.startswith("GNU.sparse") for k in pax)
            ):
                raise tarfile.ReadError(f"sparse tar member {raw_name!r} is forbidden")

            if info.size < 0 or info.size > MAX_DECOMPRESSED_BYTES or (total_member_bytes + info.size) > MAX_DECOMPRESSED_BYTES:
                raise tarfile.ReadError(
                    f"tar member {raw_name!r} size {info.size} exceeds {MAX_DECOMPRESSED_BYTES} byte limit"
                )

            if info.isdir():
                if raw_name not in (".", "./"):
                    norm_dir, reason = validate_member_path(raw_name)
                    if reason:
                        unsafe.append({"name": raw_name, "reason": reason})
                    elif norm_dir is not None:
                        conflict = _check_and_record_tree_path(
                            norm_dir,
                            is_dir=True,
                            seen_files_folded=seen_files_folded,
                            seen_dirs_folded=seen_dirs_folded,
                        )
                        if conflict:
                            unsafe.append({"name": raw_name, "reason": conflict})
                continue

            norm_path, reason = validate_member_path(raw_name)
            if reason:
                unsafe.append({"name": raw_name, "reason": reason})
                continue

            if info.issym() or info.islnk():
                unsafe.append({"name": raw_name, "reason": "symbolic or hard link member is forbidden"})
                continue
            if not info.isreg():
                unsafe.append({"name": raw_name, "reason": "non-regular tar member (FIFO/device/special) is forbidden"})
                continue

            assert norm_path is not None
            conflict = _check_and_record_tree_path(
                norm_path,
                is_dir=False,
                seen_files_folded=seen_files_folded,
                seen_dirs_folded=seen_dirs_folded,
            )
            if conflict:
                unsafe.append({"name": raw_name, "reason": conflict})
                continue

            extracted = tf.extractfile(info)
            if extracted is None:
                unsafe.append({"name": raw_name, "reason": "unreadable regular tar member"})
                continue

            is_manifest = _is_manifest_member(norm_path)
            m_dir = norm_path.rsplit("/", 1)[0] if "/" in norm_path else ""
            hasher = hashlib.sha256()
            size = 0
            capture_buf: list[bytes] | None = [] if (is_manifest and info.size <= _MAX_MANIFEST_BYTES) else None
            with extracted:
                while True:
                    chunk = extracted.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    total_member_bytes += len(chunk)
                    if total_member_bytes > MAX_DECOMPRESSED_BYTES:
                        raise tarfile.ReadError(
                            f"decompressed archive exceeds {MAX_DECOMPRESSED_BYTES} byte limit"
                        )
                    hasher.update(chunk)
                    if capture_buf is not None:
                        capture_buf.append(chunk)

            digest = hasher.hexdigest()
            members[norm_path] = {"sha256": digest, "size_bytes": size}
            if is_manifest:
                if capture_buf is None or size > _MAX_MANIFEST_BYTES:
                    manifests[norm_path] = (m_dir, None, f"manifest exceeds {_MAX_MANIFEST_BYTES} byte limit")
                else:
                    try:
                        text = b"".join(capture_buf).decode("utf-8")
                        manifests[norm_path] = (m_dir, text, None)
                    except UnicodeDecodeError as exc:
                        manifests[norm_path] = (m_dir, None, f"manifest is not valid UTF-8: {exc}")

        end_offset = getattr(tf, "offset", None)

    if decomp_raw is not None:
        decomp_raw.verify_clean_eof(end_offset)
        while True:
            rem = tar_source.read(_CHUNK_BYTES)
            if not rem:
                break
            if any(b != 0 for b in rem):
                raise tarfile.ReadError("trailing non-zero data after tar end-of-archive marker")
        decomp_raw.verify_clean_eof()
    elif isinstance(end_offset, int):
        stream.seek(end_offset)
        scanned_tail = 0
        while True:
            rem = stream.read(_CHUNK_BYTES)
            if not rem:
                break
            scanned_tail += len(rem)
            if scanned_tail > MAX_DECOMPRESSED_BYTES:
                raise tarfile.ReadError("excessive trailing padding after tar end-of-archive marker")
            if any(b != 0 for b in rem):
                raise tarfile.ReadError("trailing non-zero data after tar end-of-archive marker")

    return members, unsafe, manifests


def _verify_zip_structure_and_raw_names(
    stream: io.BufferedReader,
    infolist: list[zipfile.ZipInfo],
) -> dict[int, bytes]:
    """Verify that the zip central directory covers the archive from offset 0 through the
    end-of-central-directory record with no orphan local file headers (`PK\\x03\\x04`) or trailing
    bytes, and return `{header_offset: raw_local_filename_bytes}`."""
    if len(infolist) > MAX_MEMBER_COUNT:
        raise zipfile.BadZipFile(f"zip archive exceeds {MAX_MEMBER_COUNT} member limit")

    stream.seek(0, io.SEEK_END)
    file_size = stream.tell()
    tail_window = min(file_size, 65535 + 22)
    stream.seek(file_size - tail_window)
    tail_bytes = stream.read(tail_window)
    eocd_rel = tail_bytes.rfind(b"PK\x05\x06")
    if eocd_rel < 0 or (len(tail_bytes) - eocd_rel) < 22:
        raise zipfile.BadZipFile("missing or truncated zip end-of-central-directory record")
    disk_no, cd_disk, disk_entries, total_entries, cd_size, cd_offset, comment_len = struct.unpack_from(
        "<HHHHIIH", tail_bytes, eocd_rel + 4
    )
    if comment_len != 0 or (eocd_rel + 22) != len(tail_bytes):
        raise zipfile.BadZipFile("unreferenced trailing bytes after zip end-of-central-directory")

    eocd_abs = file_size - tail_window + eocd_rel
    if not infolist:
        if (
            eocd_abs == 0
            and file_size == 22
            and disk_no == 0
            and cd_disk == 0
            and disk_entries == 0
            and total_entries == 0
            and cd_size == 0
            and cd_offset == 0
        ):
            return {}
        if eocd_abs == 76 and file_size == 98:
            stream.seek(0)
            z64 = stream.read(76)
            if len(z64) == 76 and z64[:4] == b"PK\x06\x06" and z64[56:60] == b"PK\x06\x07":
                z64_size, _, _, _, _, z64_entries, z64_total, z64_cd_size, z64_cd_off = struct.unpack_from(
                    "<QHHIIQQQQ", z64, 4
                )
                if z64_size == 44 and z64_entries == 0 and z64_total == 0 and z64_cd_size == 0 and z64_cd_off == 0:
                    return {}
        raise zipfile.BadZipFile("unreferenced bytes or orphan local header in zip with empty central directory")

    sorted_infos = sorted(infolist, key=lambda x: x.header_offset)
    if sorted_infos[0].header_offset != 0:
        raise zipfile.BadZipFile(
            "unreferenced leading bytes or orphan local header before first central directory entry"
        )

    raw_names: dict[int, bytes] = {}
    for idx, info in enumerate(sorted_infos):
        off = info.header_offset
        stream.seek(off)
        lhdr = stream.read(30)
        if len(lhdr) != 30 or lhdr[:4] != b"PK\x03\x04":
            raise zipfile.BadZipFile(f"missing or corrupt local file header at offset {off}")
        flags, _, _, _, _, _, _, namelen, extralen = struct.unpack("<HHHHIIIHH", lhdr[6:30])
        if (flags & 0x0001) or (info.flag_bits & 0x0001):
            raise zipfile.BadZipFile(f"encrypted zip entry at offset {off} is not supported")
        raw_name_bytes = stream.read(namelen)
        if len(raw_name_bytes) != namelen:
            raise zipfile.BadZipFile(f"truncated local filename at offset {off}")
        if (flags & 0x0800) != (info.flag_bits & 0x0800):
            raise zipfile.BadZipFile(
                f"zip UTF-8 filename flag mismatch between local and central header at offset {off}"
            )
        try:
            local_name = raw_name_bytes.decode("utf-8") if (flags & 0x0800) else raw_name_bytes.decode("cp437")
        except UnicodeDecodeError as exc:
            raise zipfile.BadZipFile(f"invalid UTF-8 local filename at offset {off}") from exc
        if "\x00" in local_name or local_name != info.orig_filename or local_name != info.filename:
            raise zipfile.BadZipFile(
                f"local file header name {local_name!r} does not match central directory name {info.filename!r} at offset {off}"
            )
        raw_names[off] = raw_name_bytes

        data_end = off + 30 + namelen + extralen + info.compress_size
        max_desc = 24 if (flags & 0x08) else 0
        if idx + 1 < len(sorted_infos):
            next_off = sorted_infos[idx + 1].header_offset
            if not (data_end <= next_off <= data_end + max_desc):
                raise zipfile.BadZipFile(
                    f"unreferenced bytes or orphan local header between zip entries at {data_end}..{next_off}"
                )
        else:
            if cd_offset != 0xFFFFFFFF and not (data_end <= cd_offset <= data_end + max_desc):
                raise zipfile.BadZipFile(
                    "unreferenced bytes or orphan local header between last zip entry and central directory"
                )
            stream.seek(data_end)
            tail_probe = stream.read(max_desc + 4)
            if b"PK\x01\x02" not in tail_probe and b"PK\x06\x06" not in tail_probe:
                raise zipfile.BadZipFile(
                    "unreferenced bytes or orphan local header between last zip entry and central directory"
                )
    return raw_names


def _scan_zip(stream: io.BufferedReader) -> tuple[dict[str, dict], list[dict], dict[str, tuple[str, str | None, str | None]]]:
    """Scan a zip archive. Returns `(members, unsafe_members, captured_manifests)`."""
    members: dict[str, dict] = {}
    unsafe: list[dict] = []
    manifests: dict[str, tuple[str, str | None, str | None]] = {}
    seen_files_folded: dict[str, str] = {}
    seen_dirs_folded: dict[str, str] = {}
    total_member_bytes = 0

    with zipfile.ZipFile(stream, mode="r") as zf:
        infolist = zf.infolist()
        raw_local_names = _verify_zip_structure_and_raw_names(stream, infolist)

        for info in infolist:
            raw_name = info.filename
            raw_bytes = raw_local_names.get(info.header_offset, b"")
            if b"\\" in raw_bytes:
                unsafe.append({
                    "name": raw_bytes.decode("utf-8", errors="replace"),
                    "reason": "backslash path separator is not allowed",
                })
                continue

            if info.file_size < 0 or info.file_size > MAX_DECOMPRESSED_BYTES or (total_member_bytes + info.file_size) > MAX_DECOMPRESSED_BYTES:
                raise zipfile.BadZipFile(
                    f"zip member {raw_name!r} size {info.file_size} exceeds {MAX_DECOMPRESSED_BYTES} byte limit"
                )

            unix_mode = (info.external_attr >> 16) & 0o170000
            if info.is_dir():
                if unix_mode and unix_mode != 0o040000:
                    unsafe.append({
                        "name": raw_name,
                        "reason": f"non-directory zip mode 0o{unix_mode:o} on directory entry is forbidden",
                    })
                    continue
                if raw_name not in (".", "./"):
                    norm_dir, reason = validate_member_path(raw_name)
                    if reason:
                        unsafe.append({"name": raw_name, "reason": reason})
                    elif norm_dir is not None:
                        conflict = _check_and_record_tree_path(
                            norm_dir,
                            is_dir=True,
                            seen_files_folded=seen_files_folded,
                            seen_dirs_folded=seen_dirs_folded,
                        )
                        if conflict:
                            unsafe.append({"name": raw_name, "reason": conflict})
                continue

            norm_path, reason = validate_member_path(raw_name)
            if reason:
                unsafe.append({"name": raw_name, "reason": reason})
                continue

            # Check Unix mode bits in upper 16 bits of external_attr for symlink (0o120000) or special file
            if unix_mode and unix_mode != 0o100000:
                unsafe.append({"name": raw_name, "reason": f"non-regular zip member mode 0o{unix_mode:o} is forbidden"})
                continue

            assert norm_path is not None
            conflict = _check_and_record_tree_path(
                norm_path,
                is_dir=False,
                seen_files_folded=seen_files_folded,
                seen_dirs_folded=seen_dirs_folded,
            )
            if conflict:
                unsafe.append({"name": raw_name, "reason": conflict})
                continue

            is_manifest = _is_manifest_member(norm_path)
            m_dir = norm_path.rsplit("/", 1)[0] if "/" in norm_path else ""
            hasher = hashlib.sha256()
            size = 0
            capture_buf: list[bytes] | None = (
                [] if (is_manifest and info.file_size <= _MAX_MANIFEST_BYTES) else None
            )
            with zf.open(info, mode="r") as zmember:
                while True:
                    chunk = zmember.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    total_member_bytes += len(chunk)
                    if total_member_bytes > MAX_DECOMPRESSED_BYTES:
                        raise zipfile.BadZipFile(
                            f"decompressed zip archive exceeds {MAX_DECOMPRESSED_BYTES} byte limit"
                        )
                    hasher.update(chunk)
                    if capture_buf is not None:
                        capture_buf.append(chunk)

            digest = hasher.hexdigest()
            members[norm_path] = {"sha256": digest, "size_bytes": size}
            if is_manifest:
                if capture_buf is None or size > _MAX_MANIFEST_BYTES:
                    manifests[norm_path] = (m_dir, None, f"manifest exceeds {_MAX_MANIFEST_BYTES} byte limit")
                else:
                    try:
                        text = b"".join(capture_buf).decode("utf-8")
                        manifests[norm_path] = (m_dir, text, None)
                    except UnicodeDecodeError as exc:
                        manifests[norm_path] = (m_dir, None, f"manifest is not valid UTF-8: {exc}")

    return members, unsafe, manifests


def verify_bundle(
    archive_path: Path | str,
    *,
    expected_sha256: str | None = None,
    provenance_map: dict | Path | str | None = None,
) -> dict:
    """Verify outer custody, inner member safety, manifest completeness, and test-input provenance
    of a `.tar.gz`/`.tgz`/`.tar` or `.zip` bundle.
    """
    path = Path(archive_path)
    errors: list[str] = []

    try:
        snap = offer.Snapshot(path)
    except (node.Refused, OSError) as exc:
        return {
            "ok": False,
            "error": E_BUNDLE_NOT_REGULAR,
            "errors": [E_BUNDLE_NOT_REGULAR],
            "detail": str(exc),
            "archive_path": os.fspath(path),
            "size_bytes": None,
            "sha256": None,
            "member_count": 0,
            "members": {},
            "unsafe_members": [],
            "manifest_found": False,
            "missing_from_manifest": [],
            "missing_from_archive": [],
            "digest_mismatches": [],
            "provenance_checked": False,
            "stale_inputs": [],
        }

    with snap:
        size_bytes, outer_sha256 = snap.size, snap.sha256
        if expected_sha256 is not None:
            exp = expected_sha256.strip().lower()
            if not _HEX64_RE.match(exp) or outer_sha256 != exp:
                errors.append(E_BUNDLE_HASH_MISMATCH)

        snap.file.seek(0)
        lower_name = path.name.lower()
        is_zip = lower_name.endswith(".zip")
        try:
            if is_zip:
                members, unsafe_members, captured_manifests = _scan_zip(snap.file)
            else:
                members, unsafe_members, captured_manifests = _scan_tar(snap.file)
        except (
            tarfile.TarError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            EOFError,
            OSError,
            RuntimeError,
            NotImplementedError,
            ValueError,
            struct.error,
            zlib.error,
            lzma.LZMAError,
            UnicodeError,
            OverflowError,
        ) as exc:
            return {
                "ok": False,
                "error": E_BUNDLE_FORMAT,
                "errors": [*errors, E_BUNDLE_FORMAT],
                "detail": str(exc),
                "archive_path": os.fspath(path),
                "size_bytes": size_bytes,
                "sha256": outer_sha256,
                "member_count": 0,
                "members": {},
                "unsafe_members": [],
                "manifest_found": False,
                "manifest_errors": [],
                "missing_from_manifest": [],
                "missing_from_archive": [],
                "digest_mismatches": [],
                "provenance_checked": False,
                "stale_inputs": [],
            }

    if unsafe_members:
        errors.append(E_BUNDLE_UNSAFE_MEMBER)

    # Reconcile inner SHA256SUMS and/or MANIFEST.json if present
    manifest_found = bool(captured_manifests)
    manifest_errors: list[dict] = []
    missing_from_archive_set: set[str] = set()
    missing_from_manifest: list[str] = []
    digest_mismatches: list[dict] = []
    covered_archive_paths: set[str] = set()
    seen_declarations: dict[str, dict[str, str]] = {}  # resolved_path -> {manifest_path: expected_dig}

    for m_path, (m_dir, m_text, read_err) in sorted(captured_manifests.items()):
        if read_err is not None or m_text is None:
            manifest_errors.append({"manifest": m_path, "reason": read_err or "unreadable manifest"})
            continue
        base = m_path.rsplit("/", 1)[-1].casefold()
        parsed_entries, parse_err = (
            _parse_manifest_json_entries(m_text)
            if base == "manifest.json"
            else _parse_sha256sums_entries(m_text)
        )
        if parse_err is not None:
            manifest_errors.append({"manifest": m_path, "reason": parse_err})
            continue

        for rel_p, exp_dig in parsed_entries:
            resolved = _resolve_archive_member_key(rel_p, members, m_dir)
            if resolved is None:
                qualified = f"{m_dir}/{rel_p}" if (m_dir and not rel_p.startswith(m_dir + "/")) else rel_p
                missing_from_archive_set.add(qualified)
                continue
            covered_archive_paths.add(resolved)
            actual_dig = members[resolved]["sha256"]
            prior_decls = seen_declarations.setdefault(resolved, {})
            if actual_dig != exp_dig:
                digest_mismatches.append({
                    "path": resolved,
                    "manifest": m_path,
                    "expected": exp_dig,
                    "actual": actual_dig,
                })
            elif any(prev_dig != exp_dig for prev_dig in prior_decls.values()):
                # Another manifest (or earlier line in the same manifest) declared a conflicting digest
                digest_mismatches.append({
                    "path": resolved,
                    "manifest": m_path,
                    "expected": exp_dig,
                    "actual": actual_dig,
                    "reason": "conflicting_manifest_declarations",
                })
            prior_decls[m_path] = exp_dig

    missing_from_archive = sorted(missing_from_archive_set)
    if manifest_found:
        for arch_p in sorted(members):
            if _is_manifest_member(arch_p):
                continue
            if arch_p not in covered_archive_paths:
                missing_from_manifest.append(arch_p)

        if manifest_errors:
            errors.append(E_MANIFEST_INVALID)
        if manifest_errors or missing_from_archive or missing_from_manifest or digest_mismatches:
            errors.append(E_BUNDLE_MANIFEST_MISMATCH)

    # Verify BEFORE/AFTER test-input provenance map if supplied
    provenance_checked = False
    stale_inputs: list[dict] = []
    if provenance_map is not None:
        prov_data: object = None
        prov_err: str | None = None
        if isinstance(provenance_map, (Path, str)):
            try:
                prov_data = _loads_no_duplicates(Path(provenance_map).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                prov_err = f"unreadable provenance map: {exc}"
        else:
            prov_data = provenance_map

        input_digests: dict[str, str] = {}
        if prov_err is None:
            input_digests, prov_err = _parse_provenance_digests(prov_data)

        if prov_err is not None:
            errors.append(E_PROVENANCE_INVALID)
        else:
            provenance_checked = True
            for rel_p, recorded_dig in sorted(input_digests.items()):
                resolved = _resolve_archive_member_key(rel_p, members)
                if resolved is None:
                    stale_inputs.append({
                        "path": rel_p,
                        "recorded_sha256": recorded_dig,
                        "archive_sha256": None,
                        "reason": "missing_in_archive",
                    })
                    continue
                actual_dig = members[resolved]["sha256"]
                if actual_dig != recorded_dig:
                    stale_inputs.append({
                        "path": resolved,
                        "recorded_sha256": recorded_dig,
                        "archive_sha256": actual_dig,
                        "reason": "digest_mismatch",
                    })
            if stale_inputs:
                errors.append(E_PROVENANCE_STALE_INPUT)

    return {
        "ok": len(errors) == 0,
        "error": errors[0] if errors else None,
        "errors": errors,
        "archive_path": os.fspath(path),
        "size_bytes": size_bytes,
        "sha256": outer_sha256,
        "member_count": len(members),
        "members": members,
        "unsafe_members": unsafe_members,
        "manifest_found": manifest_found,
        "manifest_errors": manifest_errors,
        "missing_from_manifest": missing_from_manifest,
        "missing_from_archive": missing_from_archive,
        "digest_mismatches": digest_mismatches,
        "provenance_checked": provenance_checked,
        "stale_inputs": stale_inputs,
    }
