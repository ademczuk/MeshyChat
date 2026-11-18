#!/usr/bin/env python3
"""directive_install_check.py - replace one anchored section of a rule file, provably.

DRAFT (SD 265 item 6), revised to pxx's design review and to the static reviews
of drafts 910e44fa (F1 to F9) and a570980d (R1 to R6). Standard library only.

Why: on 7 Nov a directive install appended a new block and left the old bullet
in place, so two conflicting rules were live. A final-file hash proves the file
is what someone wrote, not that the edit replaced the intended section and
nothing else. This tool checks section, block, prefix and suffix separately.

Section definition (the directive author hashes it the same way):
    [start line, end line): the bytes from the first byte of the start-anchor
    line up to, but NOT including, the first byte of the end-anchor line. The
    end-anchor line stays in the suffix and survives byte for byte. With
    --end-of-file instead of --end the section runs to the end of the file and
    the suffix is empty. Run --check on the current file: found_section_sha256
    is the value for old_section_sha256.

The block must begin with the start-anchor line; with --end it must also not
contain the end-anchor line and must end with a line terminator. Enforced by
re-parsing the planned file; any failure is BLOCK_MISMATCH.

Anchor matching: whole lines, compared after removing the line ending only
(LF, or CRLF). A UTF-8 BOM at the very start of the file is ignored for the
first line's comparison and stays in the prefix bytes.

Effects are bound to directories opened once and verified (dir_fd), where the
platform supports it. Nothing is ever unlinked by name after a check except a
temp file this run still holds open; a backup or receipt that cannot be
finished is left in place and reported in left_in_place.

stdout is exactly one JSON document on every path except --help. Warnings go
to stderr. Run --selftest for the fixtures and the RED-proof of every check.
"""

import argparse
import datetime
import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import unicodedata
import sys
import tempfile

TOOL = "directive_install_check"
TOOL_VERSION = "0.5.0-draft"
RECEIPT_FORMAT = 2
LONG_LINE = 40  # stray and duplicate checks look only at trimmed lines longer than this

OK = "OK"
ALREADY_INSTALLED = "ALREADY_INSTALLED"
OK_DURABILITY_UNKNOWN = "OK_DURABILITY_UNKNOWN"
SECTION_CHANGED = "SECTION_CHANGED"
ANCHOR_MISSING = "ANCHOR_MISSING"
ANCHOR_DUPLICATE = "ANCHOR_DUPLICATE"
BLOCK_MISMATCH = "BLOCK_MISMATCH"
DUPLICATE_BLOCK = "DUPLICATE_BLOCK"
FINAL_MISMATCH = "FINAL_MISMATCH"
STRAY_OLD_TEXT = "STRAY_OLD_TEXT"
UNVERIFIED_STRAY = "UNVERIFIED_STRAY"
SYMLINK_TARGET = "SYMLINK_TARGET"
SYMLINK_ANCESTOR = "SYMLINK_ANCESTOR"
PATH_REFUSED = "PATH_REFUSED"
MODE_NOT_SET = "MODE_NOT_SET"
RECEIPT_PATH_REFUSED = "RECEIPT_PATH_REFUSED"
RECEIPT_VERIFIED = "RECEIPT_VERIFIED"
CHECKSUM_ONLY = "CHECKSUM_ONLY"
RECEIPT_INVALID = "RECEIPT_INVALID"
USAGE_ERROR = "USAGE_ERROR"
IO_ERROR = "IO_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"
RESTORED_DURABILITY_UNKNOWN = "RESTORED_DURABILITY_UNKNOWN"
ROLLBACK_FAILED = "ROLLBACK_FAILED"

SUCCESS = (OK, ALREADY_INSTALLED, RECEIPT_VERIFIED)
REFUSALS = (RESTORED_DURABILITY_UNKNOWN, ROLLBACK_FAILED, OK_DURABILITY_UNKNOWN, SECTION_CHANGED, ANCHOR_MISSING, ANCHOR_DUPLICATE,
            BLOCK_MISMATCH, DUPLICATE_BLOCK, FINAL_MISMATCH, STRAY_OLD_TEXT,
            UNVERIFIED_STRAY, SYMLINK_TARGET, SYMLINK_ANCESTOR, PATH_REFUSED, MODE_NOT_SET,
            RECEIPT_PATH_REFUSED, CHECKSUM_ONLY, RECEIPT_INVALID)

# The individual checks. The selftest disables one at a time (a no-op) and
# requires the matching case to fail, so each is shown able to go red.
CHECKS = (
    "dotdot",               # a '..' component in a written path (target, backup dir, receipt)
    "symlink_target",       # target leaf is a symlink
    "symlink_ancestor",     # a directory in a written path is a link
    "symlink_late",         # target leaf became a symlink before os.replace
    "dir_bind",             # effects go to the directory opened and verified at the start
    "anchor_missing",       # an anchor absent, or end anchor not after start
    "anchor_duplicate",     # an anchor matches more than one line
    "block_hash",           # block file bytes != block_sha256
    "section_hash",         # current section != old and != block
    "already_installed",    # current section == block (a success path)
    "old_section_required", # ALREADY path reached without --old-section
    "old_section_hash",     # --old-section bytes != old_section_sha256
    "stray_old_text",       # a long old line also present outside the section
    "duplicate_block",      # a long block line already present outside the section
    "line_endings",         # block line endings differ from the file's
    "planned_reparse",      # planned file does not re-parse to section == block
    "expected_final",       # final != expected_final_sha256 (replace and ALREADY paths)
    "backup_exclusive",     # backup created with O_EXCL, never replacing a name
    "backup_durable",       # backup dir fsync must succeed before the replace (POSIX)
    "no_unlink_backup",     # a refusal leaves the backup in place, never unlinks by name
    "no_unlink_failed_create",  # a failed exclusive write leaves its file in place
    "mode_set",             # chmod onto the temp must succeed and read back
    "mode_readback",        # target mode after replace equals the original (POSIX)
    "concurrent_change",    # file hash or identity moved between first read and replace
    "post_write_verify",    # re-read after replace does not match the plan
    "target_dir_durable",   # target dir fsync failed after replace (POSIX)
    "restore_fsync",        # B1: the rollback rename is followed by a directory fsync
    "rollback_temp_reported",   # B2: a failed rollback replace lists its temp in left_in_place
    "end_mode_exclusive",   # CLI: exactly one of --end / --end-of-file (given = is not None)
    "end_nonempty",         # CLI: an empty --end value is a usage error
    "receipt_path",         # receipt aliases target, inputs, backup name, or target dir
    "receipt_dir_identity", # containment compares directory identities, not strings
    "receipt_exists",       # receipts are created exclusively
    "receipt_schema",       # receipt must be a JSON object with typed fields
    "receipt_pins_required",# verify without directive pins is CHECKSUM_ONLY
    "receipt_bare_hash",    # verify without pins: a changed file is RECEIPT_INVALID
    "receipt_target_bind",  # verify: --file and the receipt's path are the same file
    "receipt_pin_match",    # receipt fields must equal the directive pins
    "receipt_rerun_checks", # verify re-runs the stray and duplicate checks
    "receipt_rehash",       # verify: the file must not move during verification
    "receipt_reidentify",   # verify: the last read is the same file, opened without following a link
    "result_boundary",      # every dispatch path emits one JSON document
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
BOM = b"\xef\xbb\xbf"
O_BINARY = getattr(os, "O_BINARY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_PIN_SNAPSHOT_FD = os.name != "nt"  # hold the snapshot descriptor through verification (POSIX)
# os.replace takes src_dir_fd/dst_dir_fd wherever os.rename does, but CPython lists
# only os.rename in supports_dir_fd (measured on Linux 3.12), so test that one.
DIR_FD_OK = (hasattr(os, "O_DIRECTORY")
             and all(f in os.supports_dir_fd for f in (os.open, os.stat, os.rename, os.unlink)))

# Seams for the selftest only; nothing on the CLI reaches these.
_HOOKS = {"chmod": None, "fsync_fail": frozenset(), "stamp": None, "mkdtemp": None,
          "create_fault": None, "replace_fault": None, "before_backup": None, "before_receipt": None,
          "verify_mid": None}


class UsageError(Exception):
    pass


class ModeError(Exception):
    pass


class DirRefused(Exception):
    def __init__(self, code, detail):
        Exception.__init__(self, detail)
        self.code = code
        self.detail = detail


class _ParserRefusal(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


class _Refuse(Exception):
    def __init__(self, code, detail):
        Exception.__init__(self, detail)
        self.code = code
        self.detail = detail


def warn(msg):
    sys.stderr.write("warning: %s\n" % msg)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def now_stamps():
    utc = utc_now()
    iso = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        from zoneinfo import ZoneInfo
        local = utc.astimezone(ZoneInfo("Europe/Vienna"))
        vienna = local.strftime("%Y-%m-%d %H:%M:%S %Z") + " (Vienna)"
    except Exception:
        vienna = iso.replace("T", " ").replace("Z", "") + " UTC (Vienna zone data unavailable)"
    return iso, vienna


def _identity(st):
    return (st.st_dev, st.st_ino)


def has_dotdot(path):
    return ".." in re.split(r"[\\/]", path)


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _read_fd(fd):
    chunks = []
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks)


def _open_nofollow(path):
    """(fd, bytes, identity) of `path`, opened without following a final link where the OS has O_NOFOLLOW.
    The caller owns fd and closes it: keeping it open pins the inode, so it cannot be freed and reused
    by a replacement while the caller still compares identities (Codex on PR 556)."""
    fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | getattr(os, "O_BINARY", 0))
    try:
        return fd, _read_fd(fd), _identity(os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise


def _read_nofollow(path):
    """(bytes, identity) of `path`, opened without following a final link where the OS has O_NOFOLLOW."""
    fd, data, ident = _open_nofollow(path)
    os.close(fd)
    return data, ident


def _write_fd(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


# ---------------------------------------------------------------- links

def _is_link(path):
    try:
        if os.path.islink(path):
            return True
        isj = getattr(os.path, "isjunction", None)
        return bool(isj and isj(path))
    except OSError:
        return False


def link_chain(path):
    """(leaf_is_link, first linked ancestor or None). Callers refuse '..' first,
    so abspath only folds '.' and repeated separators here."""
    ap = os.path.abspath(path)
    chain = []
    head = ap
    while True:
        parent, _ = os.path.split(head)
        chain.append(head)
        if parent == head or not parent:
            break
        head = parent
    leaf = _is_link(chain[0])
    anc = next((p for p in chain[1:] if _is_link(p)), None)
    return leaf, anc


def dir_chain_link(directory):
    """First link in a directory path, the directory itself included."""
    leaf, anc = link_chain(directory)
    if leaf:
        return os.path.abspath(directory)
    return anc


def ancestor_identities(directory):
    """Identities of directory and every ancestor (following the path as given)."""
    ids = []
    head = os.path.abspath(directory)
    while True:
        try:
            ids.append(_identity(os.stat(head)))
        except OSError:
            pass
        parent = os.path.dirname(head)
        if parent == head or not parent:
            break
        head = parent
    return ids


# ---------------------------------------------------------------- bound directory

class Dir(object):
    """A directory opened once and verified; every effect inside it goes through here.

    With dir_fd support (POSIX), operations are relative to the held directory fd,
    so a parent renamed or replaced later does not redirect them. Without it
    (Windows), each effect first re-checks that the path still names the verified
    directory (a narrower guarantee; reported as dir_fd false).
    """

    def __init__(self, path, allow_symlink_ancestor=False, disabled=frozenset(), expect_ident=None):
        self.path = os.path.abspath(path)
        self.fd = None
        self.bound = "dir_bind" not in disabled
        self.left = []
        if not allow_symlink_ancestor and "symlink_ancestor" not in disabled:
            link = dir_chain_link(self.path)
            if link:
                raise DirRefused(SYMLINK_ANCESTOR, "directory path contains a link: %s" % link)
        try:
            if DIR_FD_OK and self.bound:
                # O_NOFOLLOW on the directory open is part of the symlink_ancestor check
                nofollow = not allow_symlink_ancestor and "symlink_ancestor" not in disabled
                flags = os.O_RDONLY | os.O_DIRECTORY | (O_NOFOLLOW if nofollow else 0)
                self.fd = os.open(self.path, flags)
                self.ident = _identity(os.fstat(self.fd))
                # open, then verify: the path must still be link-free and name this directory
                if not allow_symlink_ancestor and "symlink_ancestor" not in disabled:
                    link = dir_chain_link(self.path)
                    if link:
                        raise DirRefused(SYMLINK_ANCESTOR, "directory path contains a link: %s" % link)
                if _identity(os.stat(self.path)) != self.ident:
                    raise DirRefused(PATH_REFUSED, "directory changed while it was being opened: %s" % self.path)
            else:
                self.ident = _identity(os.stat(self.path))
        except DirRefused:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise DirRefused(IO_ERROR, "cannot open directory %s: %s" % (self.path, exc))
        if expect_ident is not None and self.bound and self.ident != expect_ident:
            self.close()
            raise DirRefused(PATH_REFUSED, "directory is not the one checked at preflight: %s" % self.path)

    @property
    def dir_fd(self):
        return self.fd is not None

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def p(self, name):
        return os.path.join(self.path, name)

    def recheck(self):
        if self.bound and self.fd is None and _identity(os.stat(self.path)) != self.ident:
            raise DirRefused(PATH_REFUSED, "directory at %s was replaced after it was verified" % self.path)

    def open(self, name, flags, mode=0o777):
        if self.fd is not None:
            return os.open(name, flags, mode, dir_fd=self.fd)
        self.recheck()
        return os.open(self.p(name), flags, mode)

    def lstat(self, name):
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return os.lstat(self.p(name))

    def stat(self, name):
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd)
        return os.stat(self.p(name))

    def is_link(self, name):
        try:
            if stat.S_ISLNK(self.lstat(name).st_mode):
                return True
        except OSError:
            return False
        return self.fd is None and _is_link(self.p(name))

    def replace(self, src, dst):
        if _HOOKS["replace_fault"]:
            _HOOKS["replace_fault"](src, dst)
        if self.fd is not None:
            os.replace(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            self.recheck()
            os.replace(self.p(src), self.p(dst))

    def unlink(self, name):
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            self.recheck()
            os.unlink(self.p(name))

    def fsync(self, role):
        """'ok', 'failed' or 'unsupported' (Windows cannot fsync a directory)."""
        if role in _HOOKS["fsync_fail"]:
            warn("directory fsync failed (%s, simulated)" % role)
            return "failed"
        if os.name == "nt":
            return "unsupported"
        try:
            if self.fd is not None:
                os.fsync(self.fd)
            else:
                dfd = os.open(self.path, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            return "ok"
        except OSError as exc:
            warn("directory fsync failed (%s): %s" % (role, exc))
            return "failed"

    def read(self, name, nofollow=True):
        """(bytes, stat) of a regular file, never following a final link (O_NOFOLLOW)."""
        fd = self.open(name, os.O_RDONLY | O_BINARY | (O_NOFOLLOW if nofollow else 0))
        try:
            st = os.fstat(fd)
            return _read_fd(fd), st
        finally:
            os.close(fd)

    def create_exclusive(self, name, data, mode, role, readback=False, disabled=frozenset()):
        """O_CREAT|O_EXCL create, write, fsync, optional read-back through the same fd.

        On a failure after creation the file is left in place (recorded in self.left),
        because its name may already belong to someone else.
        """
        fd = self.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | O_BINARY | O_NOFOLLOW, mode)
        try:
            if _HOOKS["create_fault"]:
                _HOOKS["create_fault"](role, self.p(name))
            _write_fd(fd, data)
            os.fsync(fd)
            if readback:
                os.lseek(fd, 0, os.SEEK_SET)
                if _read_fd(fd) != data:
                    raise OSError("read-back does not match the bytes written")
            ident = _identity(os.fstat(fd))
        except BaseException:
            os.close(fd)
            if "no_unlink_failed_create" in disabled:
                try:
                    self.unlink(name)  # the old behavior: unlink by name
                except OSError:
                    pass
            else:
                self.left.append(self.p(name))
            raise
        os.close(fd)
        return ident

    def create_temp(self, data, st=None, strict_mode=False):
        """New exclusive temp file; returns (name, fd, mode_checked, owner). fd stays open."""
        for _ in range(100):
            name = ".dic-%s.tmp" % os.urandom(8).hex()
            try:
                fd = self.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | O_BINARY | O_NOFOLLOW, 0o600)
                break
            except FileExistsError:
                continue
        else:
            raise OSError("no free temp name")
        try:
            _write_fd(fd, data)
            meta = (False, None)
            if st is not None:
                meta = self._apply_meta(fd, name, st, strict_mode)
            os.fsync(fd)
        except BaseException:
            self.discard_temp(name, fd)
            raise
        return name, fd, meta[0], meta[1]

    def _apply_meta(self, fd, name, st, strict):
        wanted = stat.S_IMODE(st.st_mode)
        try:
            if _HOOKS["chmod"]:
                _HOOKS["chmod"](self.p(name), wanted)
            elif hasattr(os, "fchmod"):
                os.fchmod(fd, wanted)
            else:
                os.chmod(self.p(name), wanted)
        except OSError as exc:
            if strict:
                raise ModeError("chmod failed: %s" % exc)
            warn("could not copy mode: %s" % exc)
        mode_checked = False
        if os.name != "nt":
            got = stat.S_IMODE(os.fstat(fd).st_mode)
            if got != wanted:
                if strict:
                    raise ModeError("mode read back as %o, wanted %o" % (got, wanted))
                warn("mode read back as %o, wanted %o" % (got, wanted))
            else:
                mode_checked = True
        owner = None
        if hasattr(os, "geteuid"):
            if os.geteuid() == 0:
                try:
                    os.fchown(fd, st.st_uid, st.st_gid)
                    owner = True
                except OSError as exc:
                    warn("could not chown: %s" % exc)
                    owner = False
            else:
                owner = (st.st_uid == os.geteuid())
                if not owner:
                    warn("not root; target owner uid %d will become %d" % (st.st_uid, os.geteuid()))
        return mode_checked, owner

    def discard_temp(self, name, fd):
        """Remove our temp only while still holding its fd and only if the name still
        is that file (POSIX). Otherwise (Windows, or the name moved) leave it in place."""
        removed = False
        if fd is not None and os.name != "nt":
            try:
                if _identity(self.lstat(name)) == _identity(os.fstat(fd)):
                    self.unlink(name)
                    removed = True
            except OSError:
                removed = False
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not removed:
            self.left.append(self.p(name))

    def commit_temp(self, name, fd, dst):
        """os.replace temp onto dst. Windows needs the temp closed first."""
        if os.name == "nt":
            os.close(fd)
            fd = None
        try:
            self.replace(name, dst)
        finally:
            if fd is not None:
                os.close(fd)


# ---------------------------------------------------------------- parsing

def split_lines(data):
    """List of (start, content_end, line_end, content); content has no terminator."""
    lines = []
    pos = 0
    n = len(data)
    while pos < n:
        lf = data.find(b"\n", pos)
        if lf == -1:
            lines.append((pos, n, n, data[pos:n]))
            break
        content_end = lf - 1 if lf > pos and data[lf - 1:lf] == b"\r" else lf
        lines.append((pos, content_end, lf + 1, data[pos:content_end]))
        pos = lf + 1
    return lines


def compare_content(line, index):
    start, _, _, content = line
    if index == 0 and start == 0 and content.startswith(BOM):
        return content[len(BOM):]
    return content


def find_anchor(lines, anchor):
    return [i for i, ln in enumerate(lines) if compare_content(ln, i) == anchor]


def locate_section(data, start, end, disabled=frozenset()):
    """Return (prefix_end, section_end, start_line). section = [start line, end line).

    end=None means --end-of-file: the section runs to the end of the data and the
    suffix is empty.
    """
    lines = split_lines(data)
    s_idx = find_anchor(lines, start)
    if "anchor_missing" not in disabled and not s_idx:
        raise _Refuse(ANCHOR_MISSING, "start anchor not found as a whole line")
    if "anchor_duplicate" not in disabled and len(s_idx) > 1:
        raise _Refuse(ANCHOR_DUPLICATE, "start anchor matches %d lines" % len(s_idx))
    if end is None:
        start_line = lines[s_idx[0]]
        prefix_end = start_line[0]
        if prefix_end == 0 and data.startswith(BOM):
            prefix_end = len(BOM)
        return prefix_end, len(data), start_line
    e_idx = find_anchor(lines, end)
    if "anchor_missing" not in disabled and not e_idx:
        raise _Refuse(ANCHOR_MISSING, "end anchor not found as a whole line")
    if "anchor_duplicate" not in disabled and len(e_idx) > 1:
        raise _Refuse(ANCHOR_DUPLICATE, "end anchor matches %d lines" % len(e_idx))
    si, ei = s_idx[0], e_idx[0]
    if "anchor_missing" not in disabled and ei <= si:
        raise _Refuse(ANCHOR_MISSING, "end anchor does not come after the start anchor")
    start_line = lines[si]
    prefix_end = start_line[0]
    if prefix_end == 0 and data.startswith(BOM):
        prefix_end = len(BOM)
    return prefix_end, lines[ei][0], start_line


def _trim(content):
    t = content.strip()  # ASCII whitespace only (bytes.strip)
    if t.startswith(BOM):
        t = t[len(BOM):].strip()
    return t


def long_lines(data):
    """Trimmed whole lines longer than LONG_LINE characters."""
    out = []
    for _, _, _, content in split_lines(data):
        t = _trim(content)
        if len(t.decode("utf-8", "replace")) > LONG_LINE:
            out.append(t)
    return out


def outside_set(prefix, suffix):
    s = set()
    for part in (prefix, suffix):
        for _, _, _, content in split_lines(part):
            s.add(_trim(content))
    return s


def line_ending_counts(data):
    crlf = data.count(b"\r\n")
    return crlf, data.count(b"\n") - crlf


def dominant_ending(data, start_line):
    crlf, lf = line_ending_counts(data)
    if crlf > lf:
        return "CRLF"
    if lf > crlf:
        return "LF"
    term = data[start_line[1]:start_line[2]]
    if term == b"\r\n":
        return "CRLF"
    if term == b"\n":
        return "LF"
    return None


def block_ending(block):
    crlf, lf = line_ending_counts(block)
    if crlf and lf:
        return "MIXED"
    if crlf:
        return "CRLF"
    if lf:
        return "LF"
    return None


# ---------------------------------------------------------------- core

def run(file_path, start, end, old_sha, block_path, block_sha, expected_final=None,
        apply=False, old_section_path=None, backup_dir=None, allow_symlink_ancestor=False,
        disabled=frozenset(), _data=None, _before_replace_hook=None, _after_replace_hook=None):
    """Decide and (with apply) perform the install. Returns the result dict.

    _data (verify only, never with apply): decide on these bytes instead of reading
    the file, so a verifier runs every check on one snapshot.
    """
    if _data is not None and apply:
        raise ValueError("_data is for check mode only")
    iso, vienna = now_stamps()
    out = {
        "tool": TOOL, "tool_version": TOOL_VERSION,
        "mode": "apply" if apply else "check",
        "result": None, "detail": "",
        "path": os.path.abspath(file_path),
        "start_anchor": start.decode("utf-8", "replace"),
        "end_anchor": None if end is None else end.decode("utf-8", "replace"),
        "end_of_file": end is None,
        "old_section_sha256": old_sha,
        "old_section_file": old_section_path,
        "found_section_sha256": None,
        "block_sha256": block_sha,
        "block_file_sha256": None,
        "prefix_sha256": None, "suffix_sha256": None,
        "file_sha256_before": None, "final_sha256": None,
        "expected_final_sha256": expected_final,
        "wrote": False, "backup_path": None, "backup_dir_fsync": None, "restored": None,
        "dir_fsync": None, "dir_fsynced": None, "dir_fd": None, "platform_note": None,
        "mode_checked": None, "owner_preserved": None, "left_in_place": [],
        "checked_at": iso, "checked_at_vienna": vienna,
    }
    dirs = []

    def done(code, detail=""):
        out["result"] = code
        out["detail"] = detail
        for d in dirs:
            for p in d.left:
                if p not in out["left_in_place"]:
                    out["left_in_place"].append(p)
        return out

    try:
        if "dotdot" not in disabled:
            if has_dotdot(file_path):
                return done(PATH_REFUSED, "the target path has a '..' component; give a direct path")
            if backup_dir and has_dotdot(backup_dir):
                return done(PATH_REFUSED, "the backup directory has a '..' component; give a direct path")
        leaf_link, anc = link_chain(file_path)
        if leaf_link and "symlink_target" not in disabled:
            return done(SYMLINK_TARGET, "target is a symlink; not resolved, nothing written")
        if anc and not allow_symlink_ancestor and "symlink_ancestor" not in disabled:
            return done(SYMLINK_ANCESTOR, "an ancestor of the target is a link: %s" % anc)
        directory, name = os.path.split(os.path.abspath(file_path))
        try:
            tdir = Dir(directory, allow_symlink_ancestor, disabled)
            dirs.append(tdir)
            out["dir_fd"] = tdir.dir_fd
            bdir = Dir(backup_dir or directory, allow_symlink_ancestor, disabled)
            dirs.append(bdir)
        except DirRefused as r:
            return done(r.code, r.detail)

        if _data is not None:
            data, st0 = _data, None
        else:
            try:
                # the O_NOFOLLOW read is part of the symlink_target check (POSIX)
                data, st0 = tdir.read(name, nofollow="symlink_target" not in disabled)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    return done(SYMLINK_TARGET, "target became a symlink; nothing written")
                return done(IO_ERROR, "cannot read file: %s" % exc)
            except DirRefused as r:
                return done(r.code, r.detail)
        before_sha = sha256(data)
        out["file_sha256_before"] = before_sha

        try:
            prefix_end, section_end, start_line = locate_section(data, start, end, disabled)
        except _Refuse as r:
            return done(r.code, r.detail)
        prefix, section, suffix = data[:prefix_end], data[prefix_end:section_end], data[section_end:]
        out["prefix_sha256"] = sha256(prefix)
        out["suffix_sha256"] = sha256(suffix)
        found = sha256(section)
        out["found_section_sha256"] = found

        try:
            block = read_bytes(block_path)
        except OSError as exc:
            return done(IO_ERROR, "cannot read block: %s" % exc)
        out["block_file_sha256"] = sha256(block)
        if out["block_file_sha256"] != block_sha and "block_hash" not in disabled:
            return done(BLOCK_MISMATCH, "block file hash differs from block_sha256")

        # The already-installed branch first: a directive may pin identical old-section and block hashes, and that
        # section is installed, with its old-section and stray checks still run (Codex on PR 556).
        if found == block_sha and "already_installed" not in disabled:
            kind = "already"
        elif found == old_sha:
            kind = "replace"
        elif "section_hash" in disabled:
            kind = "replace"
        else:
            return done(SECTION_CHANGED, "current section hash differs from old_section_sha256 and from block_sha256")

        if kind == "replace":
            old_section = section
        else:
            old_section = None
            if old_section_path is None:
                if "old_section_required" not in disabled:
                    return done(UNVERIFIED_STRAY, "section already equals the block, but no --old-section was "
                                                  "given, so stray old text cannot be ruled out")
            else:
                try:
                    old_section = read_bytes(old_section_path)
                except OSError as exc:
                    return done(IO_ERROR, "cannot read --old-section: %s" % exc)
                if sha256(old_section) != old_sha and "old_section_hash" not in disabled:
                    return done(UNVERIFIED_STRAY, "--old-section does not hash to old_section_sha256")

        outside = outside_set(prefix, suffix)
        if old_section is not None and "stray_old_text" not in disabled:
            strays = [t for t in long_lines(old_section) if t in outside]
            if strays:
                return done(STRAY_OLD_TEXT, "%d long line(s) of the old section also occur outside the section"
                            % len(strays))
        if "duplicate_block" not in disabled:
            dups = [t for t in long_lines(block) if t in outside]
            if dups:
                return done(DUPLICATE_BLOCK, "%d long line(s) of the block already occur outside the section"
                            % len(dups))

        if kind == "already":
            out["final_sha256"] = before_sha
            if expected_final is not None and before_sha != expected_final and "expected_final" not in disabled:
                return done(FINAL_MISMATCH, "section already equals the block, but the file differs from "
                                            "expected_final_sha256; nothing written")
            return done(ALREADY_INSTALLED, "section already equals the block; stray and duplicate checks clean")

        if "line_endings" not in disabled:
            want, have = dominant_ending(data, start_line), block_ending(block)
            if have == "MIXED":
                return done(BLOCK_MISMATCH, "block has mixed CRLF and LF line endings")
            if want is not None and have is not None and have != want:
                return done(BLOCK_MISMATCH, "block uses %s, file uses %s; not converted" % (have, want))

        planned = prefix + block + suffix
        if "planned_reparse" not in disabled:
            reason = None
            blines = split_lines(block)
            if not blines or blines[0][3] != start:
                reason = "block does not begin with the start-anchor line"
            elif end is not None and any(ln[3] == end for ln in blines):
                reason = "block contains the end-anchor line"
            elif end is not None and not block.endswith(b"\n"):
                reason = "block does not end with a line terminator"
            try:
                p, s, _ = locate_section(planned, start, end)
                if planned[:p] != prefix or planned[p:s] != block or planned[s:] != suffix:
                    reason = reason or "planned file re-parses to a different section"
            except _Refuse as r:
                reason = reason or ("planned file does not re-parse: %s: %s" % (r.code, r.detail))
            if reason:
                return done(BLOCK_MISMATCH, reason)

        planned_sha = sha256(planned)
        out["final_sha256"] = planned_sha
        if expected_final is not None and planned_sha != expected_final and "expected_final" not in disabled:
            return done(FINAL_MISMATCH, "planned final hash differs from expected_final_sha256; nothing written")

        if not apply:
            return done(OK, "check only; nothing written")
        return _apply(out, done, tdir, bdir, name, data, st0, before_sha, planned, planned_sha,
                      start, end, block_sha, expected_final, disabled,
                      _before_replace_hook, _after_replace_hook)
    finally:
        for d in dirs:
            d.close()


def _apply(out, done, tdir, bdir, name, data, st0, before_sha, planned, planned_sha,
           start, end, block_sha, expected_final, disabled, before_hook, after_hook):
    st = st0
    base = name

    # 1. backup: exclusive, durable, read back, in the verified backup directory
    if _HOOKS["before_backup"]:
        _HOOKS["before_backup"](bdir.path)
    stamp = _HOOKS["stamp"] or utc_now().strftime("%Y%m%dT%H%M%SZ")
    backup_name = None
    try:
        n = 1
        while True:
            cand = "%s.bak-%s" % (base, stamp) if n == 1 else "%s.bak-%s-%d" % (base, stamp, n)
            if "backup_exclusive" in disabled:  # the old check-then-replace behavior
                tname, tfd, _, _ = bdir.create_temp(data)
                bdir.commit_temp(tname, tfd, cand)
                backup_name = cand
                break
            try:
                bdir.create_exclusive(cand, data, 0o600, "backup", readback=True, disabled=disabled)
                backup_name = cand
                break
            except FileExistsError:
                n += 1
                if n > 1000:
                    raise OSError("no free backup name after 1000 tries")
    except DirRefused as r:
        return done(r.code, r.detail + "; nothing replaced")
    except OSError as exc:
        return done(IO_ERROR, "backup failed, nothing replaced: %s" % exc)
    backup_path = bdir.p(backup_name)
    out["backup_path"] = backup_path

    def keep_backup():
        """Refusal after the backup exists: leave it, report it. Never unlink by name."""
        if "no_unlink_backup" in disabled:
            try:
                bdir.unlink(backup_name)  # the old behavior
            except (OSError, DirRefused):
                pass
            out["backup_path"] = None
        elif backup_path not in out["left_in_place"]:
            out["left_in_place"].append(backup_path)

    b_fs = bdir.fsync("backup")
    out["backup_dir_fsync"] = b_fs
    if b_fs == "failed" and "backup_durable" not in disabled:
        keep_backup()
        return done(IO_ERROR, "backup directory fsync failed; the recovery copy is not durable, nothing replaced")

    # 2. temp with the target's mode, fsynced
    tname = tfd = None
    try:
        try:
            tname, tfd, mode_checked, owner = tdir.create_temp(planned, st, strict_mode="mode_set" not in disabled)
        except ModeError as exc:
            keep_backup()
            return done(MODE_NOT_SET, "%s; nothing replaced" % exc)
        out["mode_checked"] = mode_checked
        out["owner_preserved"] = owner
        if before_hook:
            before_hook(tdir.p(name))

        # 3. last checks before the replace
        if tdir.is_link(name) and "symlink_late" not in disabled:
            tdir.discard_temp(tname, tfd)
            tname = tfd = None
            keep_backup()
            return done(SYMLINK_TARGET, "target became a symlink before the replace; nothing written")
        if "concurrent_change" not in disabled:
            try:
                now, st_now = tdir.read(name)
                moved = _identity(st_now) != _identity(st0) or sha256(now) != before_sha
            except OSError:
                moved = True
            if moved:
                tdir.discard_temp(tname, tfd)
                tname = tfd = None
                keep_backup()
                return done(SECTION_CHANGED, "file changed between the first read and the replace; nothing "
                                             "written, backup and any temp listed in left_in_place")
        # 4. replace. commit_temp always closes the fd, so forget it whatever happens;
        # a failed replace then leaves the temp in place (discard_temp with no fd).
        try:
            tdir.commit_temp(tname, tfd, name)
        finally:
            tfd = None
        tname = None
        out["wrote"] = True
    except DirRefused as r:
        if tname is not None:
            tdir.discard_temp(tname, tfd)
        keep_backup()
        return done(r.code, r.detail + "; nothing replaced")
    except OSError as exc:
        if tname is not None:
            tdir.discard_temp(tname, tfd)
        if not out["wrote"]:
            keep_backup()
        return done(IO_ERROR, "write failed (%s); wrote=%s" % (exc, out["wrote"]))

    d_fs = tdir.fsync("target")
    out["dir_fsync"] = d_fs
    out["dir_fsynced"] = d_fs == "ok"
    if d_fs == "unsupported":
        out["platform_note"] = "directory fsync is not supported on this platform (Windows); not attempted"

    if after_hook:
        after_hook(tdir.p(name))

    # 5. verify after the replace
    problems, mode_problem, after = [], False, None
    if "post_write_verify" not in disabled:
        try:
            after, _ = tdir.read(name)
        except (OSError, DirRefused) as exc:
            problems.append("re-read failed: %s" % exc)
        if after is not None:
            if sha256(after) != planned_sha:
                problems.append("final hash differs from plan")
            if expected_final is not None and sha256(after) != expected_final:
                problems.append("final hash differs from expected")
            try:
                a_p, a_s, _ = locate_section(after, start, end)
                if sha256(after[:a_p]) != out["prefix_sha256"]:
                    problems.append("prefix changed")
                if sha256(after[a_s:]) != out["suffix_sha256"]:
                    problems.append("suffix changed")
                if sha256(after[a_p:a_s]) != block_sha:
                    problems.append("section != block")
            except _Refuse as r:
                problems.append("re-read does not parse: %s" % r.code)
    if os.name != "nt" and "mode_readback" not in disabled:
        try:
            if stat.S_IMODE(tdir.stat(name).st_mode) != stat.S_IMODE(st.st_mode):
                problems.append("mode after replace differs from the original")
                mode_problem = True
        except OSError as exc:
            problems.append("mode read failed: %s" % exc)
    if problems:
        restored = False
        r_fs = None
        rname = None
        try:
            rname, rfd, _, _ = tdir.create_temp(data, st, strict_mode=False)
            try:
                tdir.commit_temp(rname, rfd, name)  # always closes rfd
            except (OSError, DirRefused):
                if "rollback_temp_reported" not in disabled:
                    tdir.discard_temp(rname, None)  # no fd: left in place and listed
                raise
            rname = None
            if "restore_fsync" not in disabled:
                r_fs = tdir.fsync("restore")  # B1: the rollback rename needs its own directory fsync
            restored = sha256(tdir.read(name)[0]) == before_sha
        except (OSError, DirRefused) as exc:
            warn("restore failed: %s" % exc)
        out["restored"] = restored
        out["restore_dir_fsync"] = r_fs
        if r_fs == "unsupported":
            out["platform_note"] = "directory fsync is not supported on this platform (Windows); not attempted"
        if after is not None:
            out["final_sha256"] = sha256(after)
        if backup_path not in out["left_in_place"]:
            out["left_in_place"].append(backup_path)
        what = "; ".join(problems)
        if not restored:
            return done(ROLLBACK_FAILED, "post-write verify failed (%s); the write happened and the rollback "
                                         "failed; original NOT restored, see backup_path and left_in_place" % what)
        if r_fs == "failed":
            return done(RESTORED_DURABILITY_UNKNOWN, "post-write verify failed (%s); original restored, but the "
                                                     "directory fsync after the rollback failed; a crash could "
                                                     "lose the rollback rename. Backup kept." % what)
        code = MODE_NOT_SET if mode_problem and len(problems) == 1 else FINAL_MISMATCH
        return done(code, "post-write verify failed (%s); the write happened, original restored" % what)
    if d_fs == "failed" and "target_dir_durable" not in disabled:
        return done(OK_DURABILITY_UNKNOWN, "installed and verified, but the directory fsync failed; "
                                           "a crash could lose the rename. Backup kept.")
    return done(OK, "installed and verified")


def exit_code(result):
    if result in SUCCESS:
        return 0
    if result in REFUSALS:
        return 1
    return 2


# ---------------------------------------------------------------- receipt

def tool_sha256():
    try:
        return sha256(read_bytes(os.path.abspath(__file__)))
    except OSError:
        return None


def _caseless(s):
    """Unicode canonical caseless form: NFC and NFD spellings and case variants compare equal, as
    APFS and case-insensitive volumes treat them (pxx on PR 556)."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", s).casefold())


def _path_key(path):
    """(caseless absolute path, parent identity or None, caseless base name).
    Needs no file at `path`, so a receipt naming a target that does not exist yet is still seen."""
    a = os.path.abspath(path)
    par, base = os.path.split(a)
    try:
        pid = _identity(os.stat(par))
    except OSError:
        pid = None
    return _caseless(os.path.normcase(a)), pid, _caseless(base)


def _same_path_key(x, y):
    """Same spelled path, or the same base name in the same directory (by identity), both case-folded."""
    return x[0] == y[0] or (x[1] is not None and x[1] == y[1] and x[2] == y[2])


def check_receipt_path(receipt, target, inputs, backup_dir, allow_in_target_dir,
                       allow_symlink_ancestor, disabled=frozenset()):
    """Refusal reason for a receipt destination, or None. Runs before any effect.

    Containment is decided by directory identity (st_dev, st_ino), so a case
    alias on a case-insensitive volume is still recognized.
    """
    if "dotdot" not in disabled and has_dotdot(receipt):
        return "receipt path has a '..' component; give a direct path"
    parent, base = os.path.split(os.path.abspath(receipt))
    if not base:
        return "receipt path names a directory"
    if "receipt_path" not in disabled:
        # by name first, whether or not the files exist: a receipt naming a missing target would
        # otherwise create it (Codex and pxx on PR 556). Caseless always (case and Unicode NFC/NFD),
        # so on a volume that keeps those apart such a receipt is refused too (the safe side).
        rkey = _path_key(receipt)
        for p in [target] + [i for i in inputs if i]:
            if _same_path_key(rkey, _path_key(p)):
                return "receipt path names the target or an input file"
        if os.path.lexists(receipt):
            for p in [target] + [i for i in inputs if i]:
                try:
                    if os.path.exists(p) and os.path.samefile(receipt, p):
                        return "receipt path names the target or an input file"
                except OSError:
                    pass
        try:
            pid = _identity(os.stat(parent))
        except OSError:
            return "receipt directory does not exist"
        tdir_path = os.path.dirname(os.path.abspath(target))
        bdir_path = backup_dir or tdir_path
        if "receipt_dir_identity" in disabled:  # the old string comparison
            same_bdir = os.path.abspath(parent) == os.path.abspath(bdir_path)
            r_abs, t_abs = os.path.abspath(parent), os.path.abspath(tdir_path)
            under = r_abs == t_abs or r_abs.startswith(t_abs.rstrip(os.sep) + os.sep)
        else:
            try:
                same_bdir = pid == _identity(os.stat(bdir_path))
                t_id = _identity(os.stat(tdir_path))
            except OSError:
                return "cannot stat the target or backup directory"
            under = t_id in ancestor_identities(parent)
        prefix = (os.path.basename(target) + ".bak-").casefold()
        if same_bdir and base.casefold().startswith(prefix):
            return "receipt path would take a backup name"
        if under and not allow_in_target_dir:
            return "receipt path is under the target's directory; pass --allow-receipt-in-target-dir to allow it"
    if "receipt_exists" not in disabled and os.path.lexists(receipt):
        return "receipt path already exists; receipts are created exclusively"
    if "symlink_ancestor" not in disabled and not allow_symlink_ancestor:
        leaf, anc = link_chain(receipt)
        if leaf or anc:
            return "receipt path contains a link: %s" % (receipt if leaf else anc)
    return None


def receipt_bytes(out):
    rec = dict(out)
    rec["receipt_format"] = RECEIPT_FORMAT
    rec["tool_sha256"] = tool_sha256()
    return (json.dumps(rec, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")


def write_receipt(path, out, rdir=None, disabled=frozenset()):
    """Exclusive create in rdir (the verified receipt directory). Returns rdir.left."""
    own = rdir is None
    if own:
        rdir = Dir(os.path.dirname(os.path.abspath(path)), True, disabled)
    try:
        rdir.create_exclusive(os.path.basename(path), receipt_bytes(out), 0o644, "receipt", disabled=disabled)
        rdir.fsync("receipt")
        return list(rdir.left)
    finally:
        if own:
            rdir.close()


_RECEIPT_TYPES = {
    "receipt_format": int, "mode": str, "result": str, "path": str, "start_anchor": str,
    "end_of_file": bool, "final_sha256": str, "block_sha256": str, "old_section_sha256": str,
}


def verify_receipt(path, pins=None, disabled=frozenset()):
    """Re-verify an install receipt. Returns a result dict.

    pins: the directive values, given again by the caller: file (the intended rule
    file), start, end or None, old_sha, old_section, block, block_sha,
    expected_final, allow_symlink_ancestor. Without pins the best possible answer
    is CHECKSUM_ONLY. Only RECEIPT_VERIFIED is a directive-done gate.
    """
    res = {"tool": TOOL, "verify_receipt": path, "result": None, "detail": ""}

    def done(code, detail):
        res["result"] = code
        res["detail"] = detail
        return res

    try:
        rec = json.loads(read_bytes(path).decode("ascii"))
    except (OSError, ValueError) as exc:
        return done(RECEIPT_INVALID, "unreadable receipt: %s" % exc)
    if "receipt_schema" not in disabled:
        if not isinstance(rec, dict):
            return done(RECEIPT_INVALID, "receipt is not a JSON object")
        for k, typ in _RECEIPT_TYPES.items():
            if not isinstance(rec.get(k), typ) or (typ is int and isinstance(rec.get(k), bool)):
                return done(RECEIPT_INVALID, "receipt field %s missing or not %s" % (k, typ.__name__))
        if not rec["end_of_file"] and not isinstance(rec.get("end_anchor"), str):
            return done(RECEIPT_INVALID, "receipt field end_anchor missing")
        if not os.path.isabs(rec["path"]):
            return done(RECEIPT_INVALID, "receipt path is not absolute")
    if rec.get("receipt_format") != RECEIPT_FORMAT:
        return done(RECEIPT_INVALID, "unknown receipt_format")
    if rec.get("mode") != "apply":
        return done(RECEIPT_INVALID, "receipt is not from --apply")
    if rec.get("result") not in (OK, ALREADY_INSTALLED):
        return done(RECEIPT_INVALID, "receipt result is %s" % rec.get("result"))
    rec_path = rec["path"]
    res["path"] = rec_path

    if pins is None:
        if _is_link(rec_path):
            return done(RECEIPT_INVALID, "receipt target is a symlink")
        try:
            matches = sha256(read_bytes(rec_path)) == rec["final_sha256"]
        except OSError as exc:
            return done(RECEIPT_INVALID, "cannot read target: %s" % exc)
        if not matches and "receipt_bare_hash" not in disabled:
            return done(RECEIPT_INVALID, "file no longer matches final_sha256 (no directive pins given)")
        if "receipt_pins_required" not in disabled:
            return done(CHECKSUM_ONLY, "target matches final_sha256; no directive pins given, so stray and "
                                       "duplicate checks were NOT run; not a directive-done gate")
        return done(RECEIPT_VERIFIED, "checksum only (mutated)")

    target = pins["file"]
    res["path"] = os.path.abspath(target)
    if _is_link(target):
        return done(RECEIPT_INVALID, "--file is a symlink")
    if "receipt_target_bind" not in disabled:
        try:
            if _identity(os.lstat(target)) != _identity(os.lstat(rec_path)) or _is_link(rec_path):
                return done(RECEIPT_INVALID, "--file and the receipt's path are not the same file")
        except OSError as exc:
            return done(RECEIPT_INVALID, "cannot stat --file or the receipt path: %s" % exc)
    if "receipt_pin_match" not in disabled:
        want = {
            "start_anchor": pins["start"].decode("utf-8", "replace"),
            "end_of_file": pins["end"] is None,
            "end_anchor": None if pins["end"] is None else pins["end"].decode("utf-8", "replace"),
            "old_section_sha256": pins["old_sha"],
            "block_sha256": pins["block_sha"],
        }
        for k, v in want.items():
            if rec.get(k) != v:
                return done(RECEIPT_INVALID, "receipt %s does not equal the directive pin" % k)
    try:
        # one read; every check below uses these bytes. On POSIX snap_fd stays open to the end, so the
        # snapshot's inode cannot be freed and reused by an unlink-and-recreate before the final identity
        # comparison (Codex on PR 556). On Windows an open handle would block the replacement outright
        # (sharing violation) rather than let it be detected, so there it is closed at once as before.
        snap_fd, snapshot, snap_ident = _open_nofollow(target)
    except OSError as exc:
        return done(RECEIPT_INVALID, "cannot read --file: %s" % exc)
    if not _PIN_SNAPSHOT_FD:
        os.close(snap_fd)
        snap_fd = None
    try:
        return _verify_snapshot(target, pins, rec, snapshot, snap_ident, disabled, done)
    finally:
        if snap_fd is not None:
            os.close(snap_fd)


def _verify_snapshot(target, pins, rec, snapshot, snap_ident, disabled, done):
    """The rest of verify_receipt, run while the caller holds the snapshot's descriptor open."""
    snap_sha = sha256(snapshot)
    if "receipt_rerun_checks" not in disabled:
        r = run(target, pins["start"], pins["end"], pins["old_sha"], pins["block"], pins["block_sha"],
                pins.get("expected_final"), apply=False, old_section_path=pins["old_section"],
                allow_symlink_ancestor=bool(pins.get("allow_symlink_ancestor")), _data=snapshot)
        if r["result"] != ALREADY_INSTALLED:
            return done(RECEIPT_INVALID, "re-running the checks gives %s: %s" % (r["result"], r["detail"]))
    if snap_sha != rec["final_sha256"]:
        return done(RECEIPT_INVALID, "file no longer matches final_sha256")
    if _HOOKS["verify_mid"]:
        _HOOKS["verify_mid"](target)
    if "receipt_rehash" not in disabled or "receipt_reidentify" not in disabled:
        try:
            last, last_ident = _read_nofollow(target)
        except OSError as exc:
            # a final link refused by O_NOFOLLOW (ELOOP on Linux, EMLINK on some BSDs) is a replacement,
            # reported the same way on every platform (Codex on PR 556)
            if "receipt_reidentify" not in disabled and (exc.errno in (errno.ELOOP, errno.EMLINK) or _is_link(target)):
                return done(RECEIPT_INVALID, "file was replaced during verification")
            return done(RECEIPT_INVALID, "cannot re-read --file: %s" % exc)
        # O_NOFOLLOW is 0 on Windows, so the link test after the open stands in for it there (Codex on PR 556)
        if "receipt_reidentify" not in disabled and (_is_link(target) or last_ident != snap_ident):
            return done(RECEIPT_INVALID, "file was replaced during verification")
        if "receipt_rehash" not in disabled and sha256(last) != snap_sha:
            return done(RECEIPT_INVALID, "file changed during verification")
    return done(RECEIPT_VERIFIED, "same file, pins match, checks re-run clean on one snapshot, "
                                  "file matches final_sha256 and did not move")


# ---------------------------------------------------------------- CLI

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True, sort_keys=True) + "\n")
    sys.stdout.flush()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ParserRefusal(message)


def parse_hex(name, value):
    if value is None:
        return None
    v = value.strip().lower()
    if not HEX64.match(v):
        raise UsageError("%s must be 64 hex characters" % name)
    return v


def check_end_mode(end, end_of_file, disabled=frozenset()):
    """Exactly one of --end and --end-of-file; --end, when given, must not be empty.

    'Given' is end is not None, never the value's truthiness (F9: --end '' --end-of-file).
    """
    given = end is not None
    if "end_mode_exclusive" not in disabled and given == bool(end_of_file):
        raise UsageError("give exactly one of --end or --end-of-file (got %s)"
                         % ("both" if given else "neither"))
    if "end_nonempty" not in disabled and given and not end_of_file and end == "":
        raise UsageError("--end must not be empty; use --end-of-file for a section that runs to the end")


def _build_parser():
    ap = _Parser(description="Replace one anchored section of a rule file, provably.")
    ap.add_argument("--file")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--end-of-file", action="store_true",
                    help="section runs from the start anchor to the end of the file; excludes --end")
    ap.add_argument("--old-sha256")
    ap.add_argument("--old-section", help="old section bytes from the directive (needed on the ALREADY_INSTALLED path)")
    ap.add_argument("--block")
    ap.add_argument("--block-sha256")
    ap.add_argument("--expected-final-sha256")
    ap.add_argument("--backup-dir", help="default: the target file's own directory")
    ap.add_argument("--receipt")
    ap.add_argument("--allow-receipt-in-target-dir", action="store_true")
    ap.add_argument("--allow-symlink-ancestor", action="store_true")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="default: plan only, write nothing")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--selftest", action="store_true")
    g.add_argument("--verify-receipt", metavar="RECEIPT")
    return ap


def _dispatch(argv, disabled):
    ap = _build_parser()
    try:
        args = ap.parse_args(argv)
    except _ParserRefusal as exc:
        return {"tool": TOOL, "result": USAGE_ERROR, "detail": exc.message}, 2

    if args.selftest:
        return selftest()

    rdir = None
    try:
        if args.verify_receipt:
            names = ("file", "start", "old_sha256", "old_section", "block", "block_sha256")
            given = [n for n in names if getattr(args, n) not in (None, "")]
            if not given and args.end is None and not args.end_of_file:
                res = verify_receipt(args.verify_receipt, None, disabled)
                return res, exit_code(res["result"])
            check_end_mode(args.end, args.end_of_file, disabled)
            missing = [n for n in names if n not in given]
            if missing:
                raise UsageError("verify with pins needs all of them; missing: %s"
                                 % ", ".join("--" + m.replace("_", "-") for m in missing))
            pins = {
                "file": args.file,
                "start": args.start.encode("utf-8"),
                "end": None if args.end_of_file else args.end.encode("utf-8"),
                "old_sha": parse_hex("--old-sha256", args.old_sha256),
                "old_section": args.old_section,
                "block": args.block,
                "block_sha": parse_hex("--block-sha256", args.block_sha256),
                "expected_final": parse_hex("--expected-final-sha256", args.expected_final_sha256),
                "allow_symlink_ancestor": args.allow_symlink_ancestor,
            }
            res = verify_receipt(args.verify_receipt, pins, disabled)
            return res, exit_code(res["result"])

        check_end_mode(args.end, args.end_of_file, disabled)
        for name in ("file", "start", "old_sha256", "block", "block_sha256"):
            if getattr(args, name) in (None, ""):
                raise UsageError("--%s is required" % name.replace("_", "-"))
        if not args.end_of_file and args.start == args.end:
            raise UsageError("--start and --end must differ")
        old = parse_hex("--old-sha256", args.old_sha256)
        blk = parse_hex("--block-sha256", args.block_sha256)
        exp = parse_hex("--expected-final-sha256", args.expected_final_sha256)
        if args.backup_dir and not os.path.isdir(args.backup_dir):
            raise UsageError("--backup-dir is not a directory")
    except UsageError as exc:
        return {"tool": TOOL, "result": USAGE_ERROR, "detail": str(exc)}, 2

    if "dotdot" not in disabled:
        for label, p in (("target path", args.file), ("backup directory", args.backup_dir)):
            if p and has_dotdot(p):
                return {"tool": TOOL, "result": PATH_REFUSED,
                        "detail": "the %s has a '..' component; give a direct path; nothing run, "
                                  "nothing written" % label, "path": os.path.abspath(args.file)}, 1

    try:
        if args.receipt:
            why = check_receipt_path(args.receipt, args.file, [args.block, args.old_section], args.backup_dir,
                                     args.allow_receipt_in_target_dir, args.allow_symlink_ancestor, disabled)
            if not why:
                try:
                    rdir = Dir(os.path.dirname(os.path.abspath(args.receipt)), args.allow_symlink_ancestor, disabled)
                except DirRefused as r:
                    why = r.detail
            if why:
                return {"tool": TOOL, "result": RECEIPT_PATH_REFUSED,
                        "detail": why + "; nothing run, nothing written",
                        "receipt": args.receipt, "path": os.path.abspath(args.file)}, 1

        end_arg = None if args.end_of_file else args.end.encode("utf-8")
        out = run(args.file, args.start.encode("utf-8"), end_arg, old, args.block, blk, exp,
                  apply=args.apply, old_section_path=args.old_section, backup_dir=args.backup_dir,
                  allow_symlink_ancestor=args.allow_symlink_ancestor, disabled=disabled)
        code = exit_code(out["result"])
        if rdir is not None:
            if _HOOKS["before_receipt"]:
                _HOOKS["before_receipt"](rdir.path)
            if out.get("backup_path") and os.path.exists(out["backup_path"]) and os.path.lexists(args.receipt) \
                    and os.path.samefile(out["backup_path"], args.receipt):
                out["receipt_error"] = "receipt path equals the backup path; receipt not written"
                code = 2
            else:
                try:
                    left = write_receipt(args.receipt, out, rdir, disabled)
                    out["receipt"] = args.receipt
                    out["left_in_place"].extend(p for p in left if p not in out["left_in_place"])
                except (OSError, DirRefused) as exc:
                    out["receipt_error"] = str(exc)
                    out["left_in_place"].extend(p for p in rdir.left if p not in out["left_in_place"])
                    warn("receipt write failed: %s" % exc)
                    code = 2
        return out, code
    finally:
        if rdir is not None:
            rdir.close()


def main(argv=None, _disabled=frozenset()):
    """One structured result boundary: every path but --help emits one JSON document."""
    try:
        obj, code = _dispatch(argv, _disabled)
    except SystemExit:
        raise  # --help: argparse prints plain text and exits 0
    except Exception as exc:
        if "result_boundary" in _disabled:
            raise
        obj, code = {"tool": TOOL, "result": INTERNAL_ERROR,
                     "detail": "unexpected %s: %s" % (type(exc).__name__, exc)}, 2
    emit(obj)
    return code


# ---------------------------------------------------------------- selftest

START = b"## Steering"
END = b"## Next section"
OLD_LINE = b"- old directive-rule bullet: ask the operator in your own window first."
NEW_LINE = b"- new steering bullet: a seat_directive is the operator's own window."
OLD_SEC = START + b"\n" + OLD_LINE + b"\n"
NEW_BLOCK = START + b"\n" + NEW_LINE + b"\n"
HEAD = b"# Rules\nintro line\n"
TAIL = END + b"\nafter line\n"


def _nl(b, nl):
    return b.replace(b"\n", nl)


class _Fx(object):
    def __init__(self, root, name, content, block, old_sha, block_sha=None, old_section=None, sub=None):
        self.dir = os.path.join(root, name)
        os.makedirs(self.dir)
        self.tdir = os.path.join(self.dir, sub) if sub else self.dir
        if sub:
            os.makedirs(self.tdir)
        self.file = os.path.join(self.tdir, "RULES.md")
        self.blockf = os.path.join(self.dir, "block.md")
        self.oldf = None
        with open(self.file, "wb") as fh:
            fh.write(content)
        with open(self.blockf, "wb") as fh:
            fh.write(block)
        if old_section is not None:
            self.oldf = os.path.join(self.dir, "old-section.md")
            with open(self.oldf, "wb") as fh:
                fh.write(old_section)
        self.old_sha = old_sha
        self.block_sha = block_sha if block_sha is not None else sha256(block)
        self.rdir = os.path.join(self.dir, "receipts")
        os.makedirs(self.rdir)
        if sub:
            os.makedirs(os.path.join(self.dir, "receipts-out"))

    def run(self, disabled, apply=True, start=START, end=END, expected=None, path=None, **kw):
        return run(path or self.file, start, end, self.old_sha, self.blockf, self.block_sha, expected,
                   apply=apply, old_section_path=self.oldf, disabled=disabled, **kw)

    def content(self):
        return read_bytes(self.file)

    def temps(self):
        return set(n for n in os.listdir(self.tdir) if n.startswith(".dic-"))

    def backups(self):
        return set(n for n in os.listdir(self.tdir) if ".bak-" in n)

    def argv(self, *extra, **kw):
        a = ["--file", kw.get("file", self.file), "--start", START.decode(),
             "--old-sha256", self.old_sha, "--block", self.blockf, "--block-sha256", self.block_sha]
        if kw.get("eof"):
            a.append("--end-of-file")
        else:
            a += ["--end", END.decode()]
        return a + list(extra)

    def pins(self, end=END, file=None):
        return {"file": file or self.file, "start": START, "end": end, "old_sha": self.old_sha,
                "old_section": self.oldf, "block": self.blockf, "block_sha": self.block_sha,
                "expected_final": None}

    def pin_argv(self, rp):
        return ["--verify-receipt", rp, "--file", self.file, "--start", START.decode(), "--end", END.decode(),
                "--old-sha256", self.old_sha, "--old-section", self.oldf, "--block", self.blockf,
                "--block-sha256", self.block_sha]


def _left_names(r):
    return set(os.path.basename(p) for p in r.get("left_in_place", []))


def _cli(argv, d):
    buf, saved = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        code = main(argv, _disabled=d)
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.stdout = saved
    docs = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return code, docs


def _one(docs):
    if len(docs) != 1:
        raise AssertionError("stdout carried %d documents" % len(docs))
    return json.loads(docs[0])


def _try_symlink(src, dst, is_dir=False):
    try:
        os.symlink(src, dst, target_is_directory=is_dir)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


class _hook(object):
    """Set one _HOOKS entry for the duration of a with block."""
    def __init__(self, key, value):
        self.key, self.value = key, value

    def __enter__(self):
        self.saved = _HOOKS[self.key]
        _HOOKS[self.key] = self.value

    def __exit__(self, *a):
        _HOOKS[self.key] = self.saved


def selftest():
    mk = _HOOKS["mkdtemp"] or tempfile.mkdtemp
    root = os.path.realpath(mk(prefix="dic-selftest-"))  # macOS /var is a link
    counter = [0]
    cases = []

    def fx(content, block, old_sha, block_sha=None, old_section=None, sub=None):
        counter[0] += 1
        return _Fx(root, "c%02d" % counter[0], content, block, old_sha, block_sha, old_section, sub)

    def case(name, red=None):
        def deco(fn):
            cases.append((name, red, fn))
            return fn
        return deco

    lf_file = HEAD + OLD_SEC + TAIL
    lf_want = HEAD + NEW_BLOCK + TAIL

    def make_receipt(f, r, name="r.json"):
        rp = os.path.join(f.rdir, name)
        write_receipt(rp, r)
        return rp

    # ---- core install behavior

    @case("clean replace LF; end anchor and suffix byte for byte; backup; receipt verifies with pins")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r = f.run(d)
        if r["result"] != OK or f.content() != lf_want or r["end_of_file"] is not False:
            return False, r["result"]
        if not f.content().endswith(TAIL) or r["suffix_sha256"] != sha256(TAIL):
            return False, "suffix not preserved"
        bp = r["backup_path"]
        if not bp or read_bytes(bp) != lf_file or f.temps() or r["left_in_place"]:
            return False, "backup missing, temp left behind, or something left in place"
        if r["mode_checked"] is not (os.name != "nt") or r["dir_fd"] is not DIR_FD_OK:
            return False, "mode_checked or dir_fd wrong for this platform"
        rp = make_receipt(f, r)
        v = verify_receipt(rp, f.pins())
        if v["result"] != RECEIPT_VERIFIED:
            return False, "receipt did not verify: %s %s" % (v["result"], v["detail"])
        with open(f.file, "ab") as fh:
            fh.write(b"tamper\n")
        if verify_receipt(rp, f.pins())["result"] == RECEIPT_VERIFIED:
            return False, "receipt still verified after tamper"
        return True, "OK, dir_fd=%s, backup and receipt verified, tamper detected" % r["dir_fd"]

    @case("clean replace CRLF")
    def _(d):
        f = fx(_nl(lf_file, b"\r\n"), _nl(NEW_BLOCK, b"\r\n"), sha256(_nl(OLD_SEC, b"\r\n")))
        r = f.run(d)
        return r["result"] == OK and f.content() == _nl(lf_want, b"\r\n"), r["result"]

    @case("check mode writes nothing, no backup")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d, apply=False)
        return (r["result"] == OK and not r["wrote"] and f.content() == lf_file
                and not f.backups()), r["result"]

    @case("--backup-dir places the backup there")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        bdir = os.path.join(f.dir, "bk")
        os.makedirs(bdir)
        r = f.run(d, backup_dir=bdir)
        bp = r["backup_path"] or ""
        return (r["result"] == OK and os.path.dirname(bp) == bdir
                and os.path.basename(bp).startswith("RULES.md.bak-") and read_bytes(bp) == lf_file), bp

    @case("BOM file, start anchor on line 1")
    def _(d):
        content = BOM + OLD_SEC + TAIL
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == OK and f.content() == BOM + NEW_BLOCK + TAIL, r["result"]

    @case("Windows: dir fsync reported unsupported with a platform note, still OK")
    def _(d):
        if os.name != "nt":
            return True, "SKIPPED (POSIX host)"
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d)
        return (r["result"] == OK and r["dir_fsync"] == "unsupported" and r["dir_fsynced"] is False
                and bool(r["platform_note"]) and r["dir_fd"] is False), "%s dir_fsync=%s" % (r["result"], r["dir_fsync"])

    @case("missing start anchor", red="anchor_missing")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d, start=b"## Not here")
        return r["result"] == ANCHOR_MISSING and f.content() == lf_file, r["result"]

    @case("end anchor before start anchor", red="anchor_missing")
    def _(d):
        content = b"# Rules\n" + TAIL + OLD_SEC
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == ANCHOR_MISSING and f.content() == content, r["result"]

    @case("duplicate start anchor", red="anchor_duplicate")
    def _(d):
        content = lf_file + START + b"\nshort\n"
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == ANCHOR_DUPLICATE and f.content() == content, r["result"]

    @case("SECTION_CHANGED", red="section_hash")
    def _(d):
        content = HEAD + START + b"\n- someone edited this section by hand, long enough line\n" + TAIL
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == SECTION_CHANGED and f.content() == content, r["result"]

    @case("apply twice with --old-section: second is ALREADY_INSTALLED", red="already_installed")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r1 = f.run(frozenset())
        after1 = f.content()
        r2 = f.run(d)
        return (r1["result"] == OK and r2["result"] == ALREADY_INSTALLED and not r2["wrote"]
                and f.content() == after1), "%s then %s" % (r1["result"], r2["result"])

    @case("ALREADY path without --old-section is UNVERIFIED_STRAY", red="old_section_required")
    def _(d):
        f = fx(lf_want, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == UNVERIFIED_STRAY and exit_code(r["result"]) == 1, r["result"]

    @case("ALREADY path with a wrong --old-section is UNVERIFIED_STRAY", red="old_section_hash")
    def _(d):
        f = fx(lf_want, NEW_BLOCK, sha256(OLD_SEC), old_section=b"## Steering\nnot the old text\n")
        r = f.run(d)
        return r["result"] == UNVERIFIED_STRAY, r["result"]

    @case("F4: ALREADY path with a wrong --expected-final-sha256 is FINAL_MISMATCH", red="expected_final")
    def _(d):
        f = fx(lf_want, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r = f.run(d, expected="0" * 64)
        return (r["result"] == FINAL_MISMATCH and not r["wrote"] and f.content() == lf_want), r["result"]

    @case("ALREADY path with the right --expected-final-sha256 passes")
    def _(d):
        f = fx(lf_want, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r = f.run(d, expected=sha256(lf_want))
        return r["result"] == ALREADY_INSTALLED, r["result"]

    @case("append-and-keep (stray copy of the old bullet) gives STRAY_OLD_TEXT", red="stray_old_text")
    def _(d):
        content = HEAD + OLD_SEC + TAIL + b"later notes\n" + OLD_LINE + b"\n"
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == STRAY_OLD_TEXT and f.content() == content, r["result"]

    @case("short old lines are ignored by the stray check")
    def _(d):
        sec = START + b"\n-\nshort line\n"
        content = HEAD + b"-\nshort line\n" + sec + TAIL
        f = fx(content, NEW_BLOCK, sha256(sec))
        r = f.run(d, apply=False)
        return r["result"] == OK, r["result"]

    @case("block text already outside the section gives DUPLICATE_BLOCK", red="duplicate_block")
    def _(d):
        content = HEAD + OLD_SEC + TAIL + b"   " + NEW_LINE + b"   \n"
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == DUPLICATE_BLOCK and f.content() == content, r["result"]

    # 7 Nov reconstruction (not the real bytes): the new block was appended after the
    # old bullet and the old bullet was left in place.
    nov7 = b"# Rules\n" + OLD_LINE + b"\n" + NEW_BLOCK + TAIL

    @case("7 Nov model: anchors bound the new block, --old-section given: STRAY_OLD_TEXT", red="stray_old_text")
    def _(d):
        f = fx(nov7, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r = f.run(d)
        return r["result"] == STRAY_OLD_TEXT and f.content() == nov7, r["result"]

    @case("7 Nov model: anchors bound the old section: DUPLICATE_BLOCK", red="duplicate_block")
    def _(d):
        old_sec = b"# Rules\n" + OLD_LINE + b"\n"
        blk = b"# Rules\n" + NEW_LINE + b"\n"
        f = fx(nov7, blk, sha256(old_sec))
        r = f.run(d, start=b"# Rules", end=START)
        return r["result"] == DUPLICATE_BLOCK and f.content() == nov7, r["result"]

    @case("BLOCK_MISMATCH: wrong block hash", red="block_hash")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), block_sha=sha256(b"something else"))
        r = f.run(d)
        return r["result"] == BLOCK_MISMATCH and f.content() == lf_file, r["result"]

    @case("BLOCK_MISMATCH: block without the start anchor", red="planned_reparse")
    def _(d):
        f = fx(lf_file, b"short\n", sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == BLOCK_MISMATCH and f.content() == lf_file, r["result"]

    @case("BLOCK_MISMATCH: block contains the end anchor", red="planned_reparse")
    def _(d):
        f = fx(lf_file, START + b"\nshort\n" + END + b"\n", sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == BLOCK_MISMATCH and f.content() == lf_file, r["result"]

    @case("BLOCK_MISMATCH: block lacks its final newline", red="planned_reparse")
    def _(d):
        f = fx(lf_file, START + b"\nshort", sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == BLOCK_MISMATCH and f.content() == lf_file, r["result"]

    @case("BLOCK_MISMATCH: CRLF block into an LF file", red="line_endings")
    def _(d):
        f = fx(lf_file, _nl(NEW_BLOCK, b"\r\n"), sha256(OLD_SEC))
        r = f.run(d)
        return r["result"] == BLOCK_MISMATCH and f.content() == lf_file, r["result"]

    @case("FINAL_MISMATCH pre-write: wrong expected final, nothing written", red="expected_final")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d, expected=sha256(b"wrong"))
        return (r["result"] == FINAL_MISMATCH and not r["wrote"] and f.content() == lf_file
                and not f.backups()), r["result"]

    @case("FINAL_MISMATCH post-write: corrupted after replace, original restored", red="post_write_verify")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))

        def corrupt(path):
            with open(path, "ab") as fh:
                fh.write(b"concurrent writer\n")
        r = f.run(d, _after_replace_hook=corrupt)
        return (r["result"] == FINAL_MISMATCH and r["wrote"] and r["restored"] is True
                and f.content() == lf_file), "%s restored=%s" % (r["result"], r["restored"])

    def _corrupt(path):
        with open(path, "ab") as fh:
            fh.write(b"concurrent writer\n")

    class _Recorder(object):
        """Stands in for _HOOKS['fsync_fail']: records each fsync role, fails those in 'fail'."""
        def __init__(self, log, fail=()):
            self.log, self.fail = log, frozenset(fail)

        def __contains__(self, role):
            self.log.append(("fsync", role))
            return role in self.fail

    @case("B1: rollback order is second replace, then restore directory fsync, then return", red="restore_fsync")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        log = []

        def rec_replace(src, dst):
            log.append(("replace", os.path.basename(dst)))
        with _hook("replace_fault", rec_replace), _hook("fsync_fail", _Recorder(log)):
            r = f.run(d, _after_replace_hook=_corrupt)
        tail = [e for e in log if e[0] == "replace" or e == ("fsync", "restore")]
        want_fs = "unsupported" if os.name == "nt" else "ok"
        return (r["result"] == FINAL_MISMATCH and r["restored"] is True and f.content() == lf_file
                and r["restore_dir_fsync"] == want_fs
                and tail[-3:] == [("replace", "RULES.md"), ("replace", "RULES.md"), ("fsync", "restore")]), \
            "%s restore_dir_fsync=%s tail=%s" % (r["result"], r["restore_dir_fsync"], tail[-3:])

    @case("B1: restore directory fsync failure is RESTORED_DURABILITY_UNKNOWN, restored true, exit 1",
          red="restore_fsync")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        with _hook("fsync_fail", frozenset(["restore"])):
            r = f.run(d, _after_replace_hook=_corrupt)
        return (r["result"] == RESTORED_DURABILITY_UNKNOWN and exit_code(r["result"]) == 1
                and r["restored"] is True and f.content() == lf_file and r["wrote"]), \
            "%s restored=%s" % (r["result"], r["restored"])

    @case("B2: a failed rollback replace is ROLLBACK_FAILED and its temp is listed in left_in_place",
          red="rollback_temp_reported")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        count = [0]

        def second_fails(src, dst):
            if os.path.basename(dst) == "RULES.md":
                count[0] += 1
                if count[0] == 2:
                    raise OSError("simulated rollback replace failure")
        with _hook("replace_fault", second_fails):
            r = f.run(d, _after_replace_hook=_corrupt)
        left = _left_names(r)
        temps = f.temps()
        return (r["result"] == ROLLBACK_FAILED and exit_code(r["result"]) == 1 and r["restored"] is False
                and r["wrote"] and count[0] == 2 and len(temps) == 1 and temps <= left
                and read_bytes(os.path.join(f.tdir, list(temps)[0])) == lf_file
                and f.backups() <= left), "%s temps=%s left=%s" % (r["result"], sorted(temps), sorted(left))

    @case("race before os.replace: SECTION_CHANGED; backup left in place and reported; temps accounted for",
          red="concurrent_change")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        changed = lf_file + b"someone saved\n"

        def edit(path):
            with open(path, "wb") as fh:
                fh.write(changed)
        r = f.run(d, _before_replace_hook=edit)
        left = _left_names(r)
        return (r["result"] == SECTION_CHANGED and not r["wrote"] and f.content() == changed
                and f.backups() <= left and len(f.backups()) == 1 and f.temps() <= left), \
            "%s left=%s" % (r["result"], sorted(left))

    # ---- R1: never unlink by name after a check

    @case("R1: a refusal never deletes a backup name that now holds someone else's file", red="no_unlink_backup")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        holder = {}

        def swap_and_edit(path):
            names = [n for n in os.listdir(f.dir) if ".bak-" in n]
            bp = os.path.join(f.dir, names[0])
            other = os.path.join(f.dir, "other.tmp")
            with open(other, "wb") as fh:
                fh.write(b"not ours\n")
            os.replace(other, bp)
            holder["bp"] = bp
            with open(path, "ab") as fh:
                fh.write(b"edit\n")
        r = f.run(d, _before_replace_hook=swap_and_edit)
        bp = holder.get("bp")
        return (r["result"] == SECTION_CHANGED and bp and os.path.exists(bp)
                and read_bytes(bp) == b"not ours\n" and bp in r["left_in_place"]), r["result"]

    @case("R1: a backup whose write fails is left in place and reported, never unlinked",
          red="no_unlink_failed_create")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        seen = {}

        def fault(role, path):
            if role != "backup":
                return
            seen["path"] = path
            if os.name != "nt":  # POSIX: someone swaps the name while we hold the fd
                other = os.path.join(f.dir, "other.tmp")
                with open(other, "wb") as fh:
                    fh.write(b"someone else's file\n")
                os.replace(other, path)
            raise OSError("simulated write failure")
        with _hook("create_fault", fault):
            r = f.run(d)
        p = seen.get("path")
        ok = (r["result"] == IO_ERROR and not r["wrote"] and f.content() == lf_file
              and p and os.path.exists(p) and p in r["left_in_place"])
        if ok and os.name != "nt":
            ok = read_bytes(p) == b"someone else's file\n"
        return ok, "%s left=%s" % (r["result"], sorted(_left_names(r)))

    @case("R1: a receipt whose write fails is left in place and reported", red="no_unlink_failed_create")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        rpath = os.path.join(f.dir, "receipts-out", "r.json")

        def fault(role, path):
            if role == "receipt":
                raise OSError("simulated receipt write failure")
        with _hook("create_fault", fault):
            code, docs = _cli(f.argv("--check", "--receipt", rpath), d)
        doc = _one(docs)
        return (code == 2 and "receipt_error" in doc and os.path.exists(rpath)
                and rpath in doc["left_in_place"]), "exit=%s left=%s" % (code, doc.get("left_in_place"))

    # ---- F1 receipt destination

    def receipt_case(build, expect_written=False):
        def _(d):
            f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
            rpath, extra = build(f)
            snap = {p: read_bytes(p) for p in (f.file, f.blockf) if os.path.exists(p)}
            pre = read_bytes(rpath) if os.path.isfile(rpath) else None
            code, docs = _cli(f.argv("--check", "--receipt", rpath, *extra), d)
            doc = _one(docs)
            untouched = all(read_bytes(p) == b for p, b in snap.items())
            if pre is not None:
                untouched = untouched and read_bytes(rpath) == pre
            if expect_written:
                ok = code == 0 and doc["result"] == OK and os.path.isfile(rpath) and untouched
            else:
                ok = code == 1 and doc["result"] == RECEIPT_PATH_REFUSED and untouched
            return ok, "%s exit=%s %s" % (doc.get("result"), code, doc.get("detail", ""))
        return _

    case("F1: --receipt equal to --file is refused, target untouched",
         red=("receipt_path", "receipt_exists"))(receipt_case(lambda f: (f.file, [])))
    case("F1: --receipt equal to the block input is refused",
         red=("receipt_path", "receipt_exists"))(receipt_case(lambda f: (f.blockf, [])))
    case("F1: --receipt via a ./ alias of --file is refused",
         red=("receipt_path", "receipt_exists"))(receipt_case(lambda f: (os.path.join(f.tdir, ".", "RULES.md"), [])))
    def missing_target_receipt(rname):
        def _(d):
            f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
            missing = os.path.join(f.tdir, "CLAUDE.md")
            rpath = os.path.join(f.tdir, rname)
            code, docs = _cli(f.argv("--check", "--receipt", rpath, "--allow-receipt-in-target-dir",
                                     file=missing), d)
            doc = _one(docs)
            created = [q for q in (missing, rpath) if os.path.lexists(q)]
            return (code == 1 and doc["result"] == RECEIPT_PATH_REFUSED and not created),                 "%s exit=%s created=%s %s" % (doc.get("result"), code, created, doc.get("detail", ""))
        return _

    case("F1: --receipt equal to a --file that does not exist is refused and creates nothing",
         red="receipt_path")(missing_target_receipt("CLAUDE.md"))
    case("F1: --receipt naming a missing --file in other case is refused and creates nothing",
         red="receipt_path")(missing_target_receipt("claude.md"))

    @case("F1: --receipt naming a missing NFC --file in its NFD spelling is refused and creates nothing",
          red="receipt_path")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        nfc = unicodedata.normalize("NFC", "Regeln-é.md")
        nfd = unicodedata.normalize("NFD", nfc)
        if nfc == nfd:
            return False, "fixture spellings are equal"
        missing, rpath = os.path.join(f.tdir, nfc), os.path.join(f.tdir, nfd)
        code, docs = _cli(f.argv("--check", "--receipt", rpath, "--allow-receipt-in-target-dir", file=missing), d)
        doc = _one(docs)
        created = [n for n in os.listdir(f.tdir) if n.startswith("Regeln")]
        return (code == 1 and doc["result"] == RECEIPT_PATH_REFUSED and not created),             "%s exit=%s created=%s %s" % (doc.get("result"), code, created, doc.get("detail", ""))
    case("F1: --receipt taking a backup name is refused",
         red="receipt_path")(receipt_case(lambda f: (os.path.join(f.dir, "receipts-out", "RULES.md.bak-x"),
                                                     ["--backup-dir", os.path.join(f.dir, "receipts-out")])))
    case("F1: --receipt under the target's directory is refused without the allow flag",
         red="receipt_path")(receipt_case(lambda f: (os.path.join(f.tdir, "receipt.json"), [])))
    case("F1: --receipt under the target's directory is written with --allow-receipt-in-target-dir")(
        receipt_case(lambda f: (os.path.join(f.tdir, "receipt.json"), ["--allow-receipt-in-target-dir"]), True))

    def case_alias(f):
        """The target directory spelled with different case, if this volume folds case."""
        alias = os.path.join(f.dir, "T")
        return alias if os.path.isdir(alias) else None

    @case("R3: a case alias of the target directory is recognized by identity (case-insensitive volumes)",
          red="receipt_dir_identity")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        alias = case_alias(f)
        if alias is None:
            return True, "SKIPPED (case-sensitive volume: no case alias exists)"
        rpath = os.path.join(alias, "new-receipt.json")
        code, docs = _cli(f.argv("--check", "--receipt", rpath), d)
        doc = _one(docs)
        return (code == 1 and doc["result"] == RECEIPT_PATH_REFUSED and not os.path.exists(rpath)), \
            "%s exit=%s" % (doc["result"], code)

    @case("F1: an existing --receipt file is refused and left unchanged", red="receipt_exists")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        rpath = os.path.join(f.dir, "receipts-out", "r.json")
        with open(rpath, "wb") as fh:
            fh.write(b"someone else's file\n")
        code, docs = _cli(f.argv("--check", "--receipt", rpath), d)
        doc = _one(docs)
        return (code == 1 and doc["result"] == RECEIPT_PATH_REFUSED
                and read_bytes(rpath) == b"someone else's file\n"), "%s exit=%s" % (doc.get("result"), code)

    @case("F1: a clean --receipt outside the target dir is written (apply)")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        rpath = os.path.join(f.dir, "receipts-out", "r.json")
        code, docs = _cli(f.argv("--apply", "--receipt", rpath), d)
        doc = _one(docs)
        rec = json.loads(read_bytes(rpath).decode("ascii"))
        return (code == 0 and doc["result"] == OK and rec["result"] == OK
                and rec["receipt_format"] == RECEIPT_FORMAT), doc.get("result")

    # ---- F2 backup exclusivity

    @case("F2: an existing backup name is never replaced; counter suffix used", red="backup_exclusive")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        taken = os.path.join(f.dir, "RULES.md.bak-20991231T000000Z")
        with open(taken, "wb") as fh:
            fh.write(b"another run's recovery copy\n")
        with _hook("stamp", "20991231T000000Z"):
            r = f.run(d)
        return (r["result"] == OK and read_bytes(taken) == b"another run's recovery copy\n"
                and (r["backup_path"] or "").endswith("-2") and read_bytes(r["backup_path"]) == lf_file), \
            "%s backup=%s" % (r["result"], os.path.basename(r["backup_path"] or ""))

    # ---- R2 '..' components

    @case("R2: a '..' component in --file is PATH_REFUSED", red="dotdot")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        via = os.path.join(f.tdir, "..", "t", "RULES.md")
        r = f.run(d, path=via)
        return r["result"] == PATH_REFUSED and f.content() == lf_file, r["result"]

    @case("R2: a '..' component in --backup-dir is PATH_REFUSED", red="dotdot")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        os.makedirs(os.path.join(f.dir, "bk"))
        r = f.run(d, backup_dir=os.path.join(f.tdir, "..", "bk"))
        return r["result"] == PATH_REFUSED and f.content() == lf_file, r["result"]

    @case("R2: a '..' component in --receipt is refused", red="dotdot")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        rpath = os.path.join(f.tdir, "..", "receipts-out", "r.json")
        code, docs = _cli(f.argv("--check", "--receipt", rpath), d)
        doc = _one(docs)
        return (code == 1 and doc["result"] == RECEIPT_PATH_REFUSED
                and not os.path.exists(os.path.join(f.dir, "receipts-out", "r.json"))), doc["result"]

    @case("R2: a '..' --file with a --receipt is refused before the receipt preflight; no receipt", red="dotdot")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        rpath = os.path.join(f.dir, "receipts-out", "r.json")
        via = os.path.join(f.tdir, "..", "t", "RULES.md")
        code, docs = _cli(f.argv("--check", "--receipt", rpath, file=via), d)
        doc = _one(docs)
        return (code == 1 and doc["result"] == PATH_REFUSED and not os.path.exists(rpath)), \
            "%s exit=%s" % (doc["result"], code)

    @case("failed os.replace: IO_ERROR, target untouched, temp and backup left in place (no stale fd)")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))

        def fault(src, dst):
            if dst == "RULES.md" or dst.endswith("RULES.md"):
                raise OSError("simulated replace failure")
        with _hook("replace_fault", fault):
            r = f.run(d)
        left = _left_names(r)
        return (r["result"] == IO_ERROR and not r["wrote"] and f.content() == lf_file
                and len(f.temps()) == 1 and f.temps() <= left and f.backups() <= left
                and len(f.backups()) == 1), "%s left=%s" % (r["result"], sorted(left))

    @case("R2: a link plus '..' cannot reach a directory outside the checked path", red="dotdot")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        other = os.path.join(f.dir, "else", "sub")
        os.makedirs(other)
        outside = os.path.join(f.dir, "else", "RULES.md")
        with open(outside, "wb") as fh:
            fh.write(lf_file)
        link = os.path.join(f.tdir, "link")
        if not _try_symlink(other, link, True):
            return True, "SKIPPED (directory links not permitted here)"
        r = f.run(d, path=os.path.join(link, "..", "RULES.md"))
        return (r["result"] == PATH_REFUSED and read_bytes(outside) == lf_file
                and f.content() == lf_file), r["result"]

    # ---- F3 links

    @case("SYMLINK_TARGET: link refused, not resolved", red="symlink_target")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        link = os.path.join(f.dir, "LINK.md")
        if not _try_symlink(f.file, link):
            return True, "SKIPPED (symlinks not permitted here)"
        r = f.run(d, apply=False, path=link)  # check mode: no later guard can stand in
        return (r["result"] == SYMLINK_TARGET and os.path.islink(link)
                and f.content() == lf_file), r["result"]

    @case("F3: target under a linked directory is SYMLINK_ANCESTOR", red="symlink_ancestor")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        linkdir = os.path.join(f.dir, "linkdir")
        if not _try_symlink(f.dir, linkdir, True):
            return True, "SKIPPED (directory links not permitted here)"
        r = f.run(d, path=os.path.join(linkdir, "RULES.md"))
        return r["result"] == SYMLINK_ANCESTOR and f.content() == lf_file, r["result"]

    @case("F3: --allow-symlink-ancestor lets a linked-directory target through")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        linkdir = os.path.join(f.dir, "linkdir")
        if not _try_symlink(f.dir, linkdir, True):
            return True, "SKIPPED (directory links not permitted here)"
        r = f.run(d, apply=False, path=os.path.join(linkdir, "RULES.md"), allow_symlink_ancestor=True)
        return r["result"] == OK, r["result"]

    @case("F3: a linked --backup-dir is SYMLINK_ANCESTOR", red="symlink_ancestor")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        real = os.path.join(f.dir, "bk-real")
        os.makedirs(real)
        link = os.path.join(f.dir, "bk-link")
        if not _try_symlink(real, link, True):
            return True, "SKIPPED (directory links not permitted here)"
        r = f.run(d, backup_dir=link)
        return r["result"] == SYMLINK_ANCESTOR and f.content() == lf_file and not os.listdir(real), r["result"]

    @case("F3: a --receipt under a linked directory is refused", red="symlink_ancestor")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        link = os.path.join(f.dir, "rlink")
        if not _try_symlink(os.path.join(f.dir, "receipts-out"), link, True):
            return True, "SKIPPED (directory links not permitted here)"
        code, docs = _cli(f.argv("--check", "--receipt", os.path.join(link, "r.json")), d)
        doc = _one(docs)
        return (code == 1 and doc["result"] == RECEIPT_PATH_REFUSED
                and not os.listdir(os.path.join(f.dir, "receipts-out"))), doc.get("result")

    @case("F3: target swapped for a same-content symlink before replace is SYMLINK_TARGET", red="symlink_late")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        copy = os.path.join(f.dir, "copy.md")
        with open(copy, "wb") as fh:
            fh.write(lf_file)
        probe = os.path.join(f.dir, "probe-link")
        if not _try_symlink(copy, probe):
            return True, "SKIPPED (symlinks not permitted here)"
        os.unlink(probe)

        def swap(path):
            os.unlink(path)
            os.symlink(copy, path)
        r = f.run(d, _before_replace_hook=swap)
        return (r["result"] == SYMLINK_TARGET and os.path.islink(f.file)
                and read_bytes(copy) == lf_file and not r["wrote"]), r["result"]

    # ---- R4: a directory replaced after it was verified

    @case("R4: --backup-dir replaced after it was verified: nothing lands in the replacement", red="dir_bind")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        bk = os.path.join(f.dir, "bk")
        os.makedirs(bk)

        def swap_dir(path):
            os.rename(bk, bk + "-moved")
            os.makedirs(bk)
        with _hook("before_backup", swap_dir):
            r = f.run(d, backup_dir=bk)
        moved = os.listdir(bk + "-moved")
        return (not os.listdir(bk) and (
            (DIR_FD_OK and r["result"] == OK and len(moved) == 1) or
            (not DIR_FD_OK and r["result"] == PATH_REFUSED and f.content() == lf_file))), \
            "%s dir_fd=%s new=%s moved=%s" % (r["result"], DIR_FD_OK, os.listdir(bk), moved)

    @case("R4: receipt directory replaced after preflight: nothing lands in the replacement", red="dir_bind")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), sub="t")
        rd = os.path.join(f.dir, "receipts-out")
        rpath = os.path.join(rd, "r.json")

        def swap_dir(path):
            os.rename(rd, rd + "-moved")
            os.makedirs(rd)
        with _hook("before_receipt", swap_dir):
            code, docs = _cli(f.argv("--check", "--receipt", rpath), d)
        doc = _one(docs)
        moved = os.listdir(rd + "-moved")
        return (not os.listdir(rd) and (
            (DIR_FD_OK and code == 0 and moved == ["r.json"]) or
            (not DIR_FD_OK and code == 2 and "receipt_error" in doc))), \
            "exit=%s dir_fd=%s new=%s moved=%s" % (code, DIR_FD_OK, os.listdir(rd), moved)

    # ---- F5 / R5 / R6 receipt verification

    @case("F5: verify without pins is CHECKSUM_ONLY (exit 1)", red="receipt_pins_required")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        rp = make_receipt(f, f.run(frozenset()))
        code, docs = _cli(["--verify-receipt", rp], d)
        doc = _one(docs)
        return code == 1 and doc["result"] == CHECKSUM_ONLY, "%s exit=%s" % (doc["result"], code)

    @case("F5: bare verify of a changed file is RECEIPT_INVALID, not CHECKSUM_ONLY", red="receipt_bare_hash")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        rp = make_receipt(f, f.run(frozenset()))
        with open(f.file, "ab") as fh:
            fh.write(b"tamper\n")
        code, docs = _cli(["--verify-receipt", rp], d)
        doc = _one(docs)
        return code == 1 and doc["result"] == RECEIPT_INVALID, "%s exit=%s" % (doc["result"], code)

    @case("F5: verify with pins over a genuine install is RECEIPT_VERIFIED (exit 0)")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        rp = make_receipt(f, f.run(frozenset()))
        code, docs = _cli(f.pin_argv(rp), d)
        doc = _one(docs)
        return code == 0 and doc["result"] == RECEIPT_VERIFIED, "%s exit=%s %s" % (doc["result"], code, doc["detail"])

    @case("R5: verify with pins but no --file is a usage error")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        rp = make_receipt(f, f.run(frozenset()))
        argv = f.pin_argv(rp)
        i = argv.index("--file")
        del argv[i:i + 2]
        code, docs = _cli(argv, d)
        doc = _one(docs)
        return code == 2 and doc["result"] == USAGE_ERROR and "--file" in doc["detail"], doc["result"]

    @case("R5: a valid receipt for a staged copy does not verify the intended --file", red="receipt_target_bind")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        staged = os.path.join(f.dir, "staged")
        os.makedirs(staged)
        copy = os.path.join(staged, "RULES.md")
        with open(copy, "wb") as fh:
            fh.write(lf_want)
        good = dict(f.run(frozenset(), apply=False, path=copy))
        good.update({"mode": "apply", "result": ALREADY_INSTALLED})
        rp = make_receipt(f, good)
        with open(f.file, "wb") as fh:  # the intended target is installed too, so only identity differs
            fh.write(lf_want)
        v = verify_receipt(rp, f.pins(file=f.file), d)
        return v["result"] == RECEIPT_INVALID and "same file" in v["detail"], "%s: %s" % (v["result"], v["detail"])

    @case("R6: a file that moves during verification is RECEIPT_INVALID", red="receipt_rehash")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        rp = make_receipt(f, f.run(frozenset()))

        def prepend(path):
            data = read_bytes(path)
            with open(path, "wb") as fh:
                fh.write(b"x\n" + data)
        with _hook("verify_mid", prepend):
            v = verify_receipt(rp, f.pins(), d)
        return v["result"] == RECEIPT_INVALID and "during verification" in v["detail"], \
            "%s: %s" % (v["result"], v["detail"])

    @case("R6b: the target swapped for a same-content symlink during verification is RECEIPT_INVALID",
          red="receipt_reidentify")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        rp = make_receipt(f, f.run(frozenset()))
        copy = os.path.join(f.dir, "copy.md")
        with open(copy, "wb") as fh:
            fh.write(f.content())
        probe = os.path.join(f.dir, "probe-link")
        if not _try_symlink(copy, probe):
            return True, "SKIPPED (symlinks not permitted here)"
        os.unlink(probe)

        def swap(path):
            os.unlink(path)
            os.symlink(copy, path)
        with _hook("verify_mid", swap):
            v = verify_receipt(rp, f.pins(), d)
        return v["result"] == RECEIPT_INVALID and "replaced during verification" in v["detail"], \
            "%s: %s" % (v["result"], v["detail"])

    @case("R6c: the target swapped for a same-content copy during verification is RECEIPT_INVALID",
          red="receipt_reidentify")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        rp = make_receipt(f, f.run(frozenset()))

        def swap(path):
            data = read_bytes(path)
            os.unlink(path)
            with open(path, "wb") as fh:
                fh.write(data)
        with _hook("verify_mid", swap):
            v = verify_receipt(rp, f.pins(), d)
        return v["result"] == RECEIPT_INVALID and "replaced during verification" in v["detail"], \
            "%s: %s" % (v["result"], v["detail"])

    @case("identical old and new pins: the installed section is ALREADY_INSTALLED and its receipt verifies",
          red="already_installed")
    def _(d):
        f = fx(lf_want, NEW_BLOCK, sha256(NEW_BLOCK), old_section=NEW_BLOCK)
        before = f.content()
        r = f.run(d)
        if r["result"] != ALREADY_INSTALLED or r["wrote"] or f.content() != before:
            return False, "%s wrote=%s" % (r["result"], r["wrote"])
        v = verify_receipt(make_receipt(f, r), f.pins(), d)
        return v["result"] == RECEIPT_VERIFIED, "%s: %s" % (v["result"], v["detail"])

    @case("F5: forged self-consistent receipt with a stray old line is RECEIPT_INVALID", red="receipt_rerun_checks")
    def _(d):
        f = fx(nov7, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        forged = {"receipt_format": RECEIPT_FORMAT, "mode": "apply", "result": OK, "path": f.file,
                  "start_anchor": START.decode(), "end_anchor": END.decode(), "end_of_file": False,
                  "final_sha256": sha256(nov7), "block_sha256": f.block_sha, "old_section_sha256": f.old_sha}
        rp = os.path.join(f.rdir, "forged.json")
        with open(rp, "wb") as fh:
            fh.write(json.dumps(forged).encode("ascii"))
        v = verify_receipt(rp, f.pins(), d)
        return v["result"] == RECEIPT_INVALID and "STRAY_OLD_TEXT" in v["detail"], "%s: %s" % (v["result"], v["detail"])

    @case("F5: receipt field that disagrees with a pin is RECEIPT_INVALID", red="receipt_pin_match")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r = dict(f.run(frozenset()))
        r["old_section_sha256"] = "f" * 64
        rp = make_receipt(f, r)
        v = verify_receipt(rp, f.pins(), d)
        return v["result"] == RECEIPT_INVALID, "%s: %s" % (v["result"], v["detail"])

    @case("F3+F5: install under a linked directory with the allow flag verifies with the flag")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        linkdir = os.path.join(f.dir, "linkdir")
        if not _try_symlink(f.dir, linkdir, True):
            return True, "SKIPPED (directory links not permitted here)"
        via = os.path.join(linkdir, "RULES.md")
        r = f.run(d, path=via, allow_symlink_ancestor=True)
        rp = make_receipt(f, r)
        pins = f.pins(file=via)
        no_flag = verify_receipt(rp, pins, d)["result"]
        pins["allow_symlink_ancestor"] = True
        with_flag = verify_receipt(rp, pins, d)["result"]
        return (r["result"] == OK and no_flag == RECEIPT_INVALID and with_flag == RECEIPT_VERIFIED), \
            "%s, without flag %s, with flag %s" % (r["result"], no_flag, with_flag)

    # ---- F6 mode

    @case("F6: chmod failure on the temp is MODE_NOT_SET, nothing replaced", red="mode_set")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))

        def fail(p, m):
            raise OSError("simulated chmod failure")
        with _hook("chmod", fail):
            r = f.run(d)
        left = _left_names(r)
        return (r["result"] == MODE_NOT_SET and not r["wrote"] and f.content() == lf_file
                and f.backups() <= left and f.temps() <= left), r["result"]

    @case("F6: mode changed after replace is MODE_NOT_SET, original restored (POSIX)", red="mode_readback")
    def _(d):
        if os.name == "nt":
            return True, "SKIPPED on Windows (mode bits are only the read-only flag; mode_checked is false)"
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        os.chmod(f.file, 0o640)
        r = f.run(d, _after_replace_hook=lambda p: os.chmod(p, 0o600))
        return (r["result"] == MODE_NOT_SET and r["wrote"] and r["restored"] is True
                and f.content() == lf_file), "%s restored=%s" % (r["result"], r["restored"])

    @case("mode copied onto the replacement and read back (POSIX)")
    def _(d):
        if os.name == "nt":
            return True, "SKIPPED on Windows"
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        os.chmod(f.file, 0o640)
        r = f.run(d)
        return (r["result"] == OK and r["mode_checked"] is True
                and stat.S_IMODE(os.stat(f.file).st_mode) == 0o640), r["result"]

    # ---- F7 durability

    @case("F7: backup dir fsync failure refuses before the replace; backup left in place", red="backup_durable")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        with _hook("fsync_fail", frozenset(["backup"])):
            r = f.run(d)
        return (r["result"] == IO_ERROR and not r["wrote"] and f.content() == lf_file
                and f.backups() == _left_names(r)), "%s %s" % (r["result"], r["detail"])

    @case("F7: target dir fsync failure after replace is OK_DURABILITY_UNKNOWN (exit 1)", red="target_dir_durable")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        with _hook("fsync_fail", frozenset(["target"])):
            r = f.run(d)
        return (r["result"] == OK_DURABILITY_UNKNOWN and exit_code(r["result"]) == 1 and r["wrote"]
                and f.content() == lf_want and r["backup_path"] and os.path.exists(r["backup_path"])), r["result"]

    # ---- F8 one JSON document

    def bad_receipt_case(payload):
        def _(d):
            f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
            rp = os.path.join(f.rdir, "bad.json")
            with open(rp, "wb") as fh:
                fh.write(payload)
            code, docs = _cli(["--verify-receipt", rp], d)
            doc = _one(docs)
            return code == 1 and doc["result"] == RECEIPT_INVALID, "%s exit=%s %s" % (doc["result"], code, doc["detail"])
        return _

    case("F8: --verify-receipt on [] is one JSON RECEIPT_INVALID", red="receipt_schema")(bad_receipt_case(b"[]"))
    case("F8: --verify-receipt on null is one JSON RECEIPT_INVALID", red="receipt_schema")(bad_receipt_case(b"null"))
    case("F8: --verify-receipt on an object without anchors is RECEIPT_INVALID",
         red="receipt_schema")(bad_receipt_case(b'{"receipt_format": 2, "mode": "apply", "result": "OK"}'))

    @case("F8: --selftest with no usable temp dir is one JSON INTERNAL_ERROR", red="result_boundary")
    def _(d):
        def broken(**kw):
            raise OSError("no usable temporary directory (simulated)")
        with _hook("mkdtemp", broken):
            code, docs = _cli(["--selftest"], d)
        doc = _one(docs)
        return code == 2 and doc["result"] == INTERNAL_ERROR, "%s exit=%s" % (doc["result"], code)

    @case("F8: unknown argument is one JSON USAGE_ERROR")
    def _(d):
        code, docs = _cli(["--bogus"], d)
        doc = _one(docs)
        return code == 2 and doc["result"] == USAGE_ERROR, doc["result"]

    # ---- --end-of-file

    @case("EOF: clean replace, empty suffix, end_of_file true, receipt verifies")
    def _(d):
        f = fx(HEAD + OLD_SEC, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r = f.run(d, end=None)
        if not (r["result"] == OK and f.content() == HEAD + NEW_BLOCK and r["end_of_file"] is True
                and r["end_anchor"] is None and r["suffix_sha256"] == sha256(b"")):
            return False, r["result"]
        v = verify_receipt(make_receipt(f, r), f.pins(end=None))
        return v["result"] == RECEIPT_VERIFIED, "OK, %s" % v["result"]

    @case("EOF: block may end without a newline")
    def _(d):
        blk = START + b"\n" + NEW_LINE
        f = fx(HEAD + OLD_SEC, blk, sha256(OLD_SEC))
        r = f.run(d, end=None)
        return r["result"] == OK and f.content() == HEAD + blk, r["result"]

    @case("EOF: block without the start anchor is BLOCK_MISMATCH", red="planned_reparse")
    def _(d):
        f = fx(HEAD + OLD_SEC, b"short\n", sha256(OLD_SEC))
        r = f.run(d, end=None)
        return r["result"] == BLOCK_MISMATCH and f.content() == HEAD + OLD_SEC, r["result"]

    @case("EOF: ALREADY_INSTALLED with --old-section", red="already_installed")
    def _(d):
        f = fx(HEAD + NEW_BLOCK, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r = f.run(d, end=None)
        return (r["result"] == ALREADY_INSTALLED and r["end_of_file"] is True and not r["wrote"]
                and f.content() == HEAD + NEW_BLOCK), r["result"]

    @case("EOF: stray old line in the prefix gives STRAY_OLD_TEXT", red="stray_old_text")
    def _(d):
        content = b"# Rules\n" + OLD_LINE + b"\n" + OLD_SEC
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d, end=None)
        return r["result"] == STRAY_OLD_TEXT and f.content() == content, r["result"]

    @case("EOF: ALREADY path, stray old line in the prefix gives STRAY_OLD_TEXT", red="stray_old_text")
    def _(d):
        content = b"# Rules\n" + OLD_LINE + b"\n" + NEW_BLOCK
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC), old_section=OLD_SEC)
        r = f.run(d, end=None)
        return r["result"] == STRAY_OLD_TEXT and f.content() == content, r["result"]

    @case("EOF: block line already in the prefix gives DUPLICATE_BLOCK", red="duplicate_block")
    def _(d):
        content = b"# Rules\n" + NEW_LINE + b"\n" + OLD_SEC
        f = fx(content, NEW_BLOCK, sha256(OLD_SEC))
        r = f.run(d, end=None)
        return r["result"] == DUPLICATE_BLOCK and f.content() == content, r["result"]

    def usage_case(both):
        def _(d):
            f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
            argv = ["--file", f.file, "--start", START.decode(), "--old-sha256", f.old_sha,
                    "--block", f.blockf, "--block-sha256", f.block_sha]
            if both:
                argv += ["--end", END.decode(), "--end-of-file"]
            code, docs = _cli(argv, d)
            doc = _one(docs)
            det = doc.get("detail", "")
            ok = (code == 2 and doc.get("result") == USAGE_ERROR and "--end " in det + " "
                  and "--end-of-file" in det and f.content() == lf_file)
            return ok, "%s exit=%s: %s" % (doc.get("result"), code, det)
        return _

    case("CLI: --end and --end-of-file together is a usage refusal", red="end_mode_exclusive")(usage_case(True))
    case("CLI: neither --end nor --end-of-file is a usage refusal", red="end_mode_exclusive")(usage_case(False))

    @case("F9: --end '' --end-of-file is a usage refusal naming both", red="end_mode_exclusive")
    def _(d):
        f = fx(HEAD + OLD_SEC, NEW_BLOCK, sha256(OLD_SEC))
        argv = ["--file", f.file, "--start", START.decode(), "--end", "", "--end-of-file",
                "--old-sha256", f.old_sha, "--block", f.blockf, "--block-sha256", f.block_sha]
        code, docs = _cli(argv, d)
        doc = _one(docs)
        det = doc.get("detail", "")
        return (code == 2 and doc.get("result") == USAGE_ERROR and "--end " in det
                and "--end-of-file" in det), "%s exit=%s: %s" % (doc.get("result"), code, det)

    @case("F9: --end '' alone is a usage refusal", red="end_nonempty")
    def _(d):
        f = fx(lf_file, NEW_BLOCK, sha256(OLD_SEC))
        argv = ["--file", f.file, "--start", START.decode(), "--end", "",
                "--old-sha256", f.old_sha, "--block", f.blockf, "--block-sha256", f.block_sha]
        code, docs = _cli(argv, d)
        doc = _one(docs)
        return (code == 2 and doc.get("result") == USAGE_ERROR and f.content() == lf_file), \
            "%s exit=%s: %s" % (doc.get("result"), code, doc.get("detail", ""))

    @case("JSON output and receipt are ASCII with a non-ASCII anchor")
    def _(d):
        s = ("## Steuerung " + chr(0xFC) + "ber").encode("utf-8")
        if not any(b > 127 for b in s):
            return False, "fixture lost its non-ASCII byte; case would be vacuous"
        content = b"# x\n" + s + b"\n" + OLD_LINE + b"\n" + TAIL
        sec = s + b"\n" + OLD_LINE + b"\n"
        blk = s + b"\n" + NEW_LINE + b"\n"
        f = fx(content, blk, sha256(sec))
        r = f.run(d, apply=False, start=s)
        line = json.dumps(r, ensure_ascii=True, sort_keys=True)
        rp = make_receipt(f, r)
        try:
            read_bytes(rp).decode("ascii")
            ok_ascii = True
        except UnicodeDecodeError:
            ok_ascii = False
        return r["result"] == OK and all(ord(c) < 128 for c in line) and ok_ascii, r["result"]

    @case("Vienna stamp falls back cleanly without zone data")
    def _(d):
        iso, vienna = now_stamps()
        saved = sys.modules.get("zoneinfo", None)
        had = "zoneinfo" in sys.modules
        sys.modules["zoneinfo"] = None  # makes the import raise ImportError
        try:
            _, fallback = now_stamps()
        finally:
            if had:
                sys.modules["zoneinfo"] = saved
            else:
                del sys.modules["zoneinfo"]
        return iso.endswith("Z") and "unavailable" in fallback, "%s | %s | %s" % (iso, vienna, fallback)

    passed = failed = reds_ok = reds_bad = reds_skipped = 0
    err = sys.stderr
    try:
        for name, red, fn in cases:
            try:
                ok, detail = fn(frozenset())
            except Exception as exc:
                ok, detail = False, "raised %s: %s" % (type(exc).__name__, exc)
            err.write("%s  %s  [%s]\n" % ("PASS" if ok else "FAIL", name, detail))
            passed += 1 if ok else 0
            failed += 0 if ok else 1
            if not red:
                continue
            names = red if isinstance(red, tuple) else (red,)
            label = "+".join(names) + (" (jointly: overlapping checks)" if len(names) > 1 else "")
            if str(detail).startswith("SKIPPED"):
                reds_skipped += 1
                err.write("SKIP  RED-proof '%s': case skipped on this host\n" % label)
                continue
            try:
                mok, mdetail = fn(frozenset(names))
            except Exception as exc:
                mok, mdetail = False, "mutated run raised %s" % type(exc).__name__
            if mok:
                reds_bad += 1
                err.write("FAIL  RED-proof: with '%s' disabled the case still passes [%s]\n" % (label, mdetail))
            else:
                reds_ok += 1
                err.write("PASS  RED-proof: with '%s' disabled the case fails [%s]\n" % (label, mdetail))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    covered = set(n for _, r, _ in cases if r for n in (r if isinstance(r, tuple) else (r,)))
    missing = [c for c in CHECKS if c not in covered]
    if missing:
        reds_bad += 1
        err.write("FAIL  RED-proof coverage: no case for %s\n" % ", ".join(missing))
    else:
        err.write("PASS  RED-proof coverage: all %d checks have a case\n" % len(CHECKS))

    bad = failed + reds_bad
    summary = ("SELFTEST %s: %d cases passed, %d failed; %d RED-proofs went red, %d did not, %d skipped"
               % ("PASS" if bad == 0 else "FAIL", passed, failed, reds_ok, reds_bad, reds_skipped))
    err.write(summary + "\n")
    obj = {"tool": TOOL, "selftest": "PASS" if bad == 0 else "FAIL", "summary": summary,
           "cases_passed": passed, "cases_failed": failed, "red_proofs_red": reds_ok,
           "red_proofs_not_red": reds_bad, "red_proofs_skipped": reds_skipped}
    return obj, 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
