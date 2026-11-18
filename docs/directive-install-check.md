# directive_install_check.py (DRAFT, SD 265 item 6)

Replaces one anchored section of one rule file and proves the edit replaced that
section and nothing else. Built for the 7 Nov failure, where an install appended
the new steering block and left the old directive-rule bullet live. Python 3, standard
library only. Revised to pxx's design review and to the static reviews of drafts
910e44fa (F1 to F9) and a570980d (R1 to R6).

## The section

`[start line, end line)`: from the first byte of the start-anchor line up to, but
not including, the end-anchor line. The end-anchor line and everything after it
is the suffix and is kept byte for byte. `old_section_sha256` is the hash of that
span. To get it, run `--check` on the unedited file and read `found_section_sha256`.

Anchors match whole lines, compared after removing the line ending only (LF or
CRLF; no other trimming). A UTF-8 BOM at the start of the file is ignored for the
first line's comparison and stays in the prefix.

The block must begin with the start-anchor line, must not contain the end-anchor
line, and must end with a line terminator. All three are enforced by re-parsing the
planned file (prefix + block + suffix) and requiring it to give back the same
prefix, the block as the section, and the same suffix. A second run then finds
the anchors again and returns ALREADY_INSTALLED.

## Section to end of file

`--end-of-file` replaces `--end` when the section is the last one in the file.
Exactly one of the two must be given; "given" means the option is present, so
`--end '' --end-of-file` is refused as both. Both or neither is USAGE_ERROR (exit
2) and the message names both options. An empty `--end` on its own is also
USAGE_ERROR. With `--end-of-file` the section runs from the start-anchor line to
the last byte of the file, the suffix is empty, and the JSON carries
`"end_of_file": true` (`false` otherwise, with `end_anchor` null when true). The
block only has to begin with the start-anchor line; it may end without a newline.
The stray and duplicate checks still scan the whole prefix (and the empty suffix).

## CLI

```
python -I directive_install_check.py --file RULES.md --start "## Steering" {--end "## Next" | --end-of-file} \
    --old-sha256 HEX --block block.md --block-sha256 HEX \
    [--old-section old.md] [--expected-final-sha256 HEX] \
    [--check | --apply] [--backup-dir DIR] [--receipt PATH] \
    [--allow-receipt-in-target-dir] [--allow-symlink-ancestor]
python -I directive_install_check.py --verify-receipt PATH [--file F and the directive pins, see below]
python -I directive_install_check.py --selftest
```

`--check` is the default and writes nothing (except a receipt, if asked for).
stdout is exactly one JSON document on every path, argument errors, internal
errors and `--selftest` included; warnings and the selftest's per-case lines go
to stderr. The one exception is `--help`, which prints argparse's plain-text help
and exits 0.

## Paths

The paths this tool writes into (`--file`, `--backup-dir`, `--receipt`) must not
contain a `..` component: it is refused outright (PATH_REFUSED, or
RECEIPT_PATH_REFUSED for the receipt), because normalizing `link/..` as text and
following it in the kernel name different directories. A link anywhere in those
paths is refused (SYMLINK_TARGET for the target itself, SYMLINK_ANCESTOR for a
directory) unless `--allow-symlink-ancestor`. On macOS `/tmp` and `/var` are
links, so pass real paths or the flag. The block and old-section inputs are only
read, and are read through links.

## Bound directories (dir_fd)

The target's directory, the backup directory and the receipt directory are each
opened once, then verified (still link-free, still the same directory by
`st_dev`/`st_ino`). Where the platform supports `dir_fd` (POSIX: Linux, macOS)
every effect inside them (reads, exclusive creates, temp files, `os.replace`,
directory fsync) is relative to that held directory fd, so a directory renamed or
replaced afterwards cannot redirect a write. The JSON reports `"dir_fd": true`.
Support is detected from `os.supports_dir_fd` (CPython lists `os.rename`, not
`os.replace`, there; both take the same dir_fd arguments). The selftest has run
with `dir_fd` true on Linux (WSL, Python 3.12) and false on Windows.

Windows has no `dir_fd` for `os.open`. There the tool falls back to paths and
re-checks, right before each effect, that the path still names the directory it
verified; a mismatch is PATH_REFUSED. That narrows the window but does not close
it. The JSON reports `"dir_fd": false`.

Reported paths (`path`, `backup_path`, `left_in_place`) are the paths as given.
If a directory is renamed during a POSIX run, the effect still lands in the bound
directory, under its new name, and the reported path is stale.

## What --apply does, in order

1. Backup `<file>.bak-<UTC stamp>[-N]` in the backup directory (default: the
   file's own directory), created with `O_CREAT|O_EXCL`, so an existing name is
   never replaced; on a clash it tries `-2`, `-3`, ... It is mode 0600, written,
   fsynced, read back through the same fd, and its directory is fsynced. If that
   directory fsync fails (POSIX), nothing is replaced (IO_ERROR).
2. Temp file beside the target, created exclusively, with the target's mode copied
   (owner too when root), the mode read back, and data plus metadata fsynced. A
   failed chmod or a wrong read-back is MODE_NOT_SET; nothing replaced.
3. Just before `os.replace`: if the target became a symlink, SYMLINK_TARGET; if its
   hash or file identity moved, SECTION_CHANGED. Nothing replaced.
4. `os.replace`, then fsync of the directory. Then re-reads and checks prefix,
   suffix, section == block, the final hash, and (POSIX) the mode. Any mismatch
   rolls back: the original bytes go into a new temp, which replaces the target,
   and the directory is fsynced again after that rollback rename (`restore_dir_fsync`;
   "unsupported" on Windows). Result FINAL_MISMATCH, or MODE_NOT_SET when only the
   mode was wrong; RESTORED_DURABILITY_UNKNOWN when the rollback's directory fsync
   failed; ROLLBACK_FAILED when the rollback itself failed. The backup stays.

The pre-replace re-read is not a compare-and-swap: a writer can still slip in
between that read and `os.replace`. It narrows the window; it does not close it.

## Nothing is unlinked by name after a check

A name checked a moment ago may belong to someone else by the time it is
unlinked, and a stat-then-unlink pair is not atomic. So:
- A backup is never removed. After any refusal that comes after its creation it is
  left in place and listed in `left_in_place`.
- A backup or receipt whose exclusive write fails after creation (write, fsync or
  read-back error) is left in place and listed in `left_in_place`.
- A temp file is removed only while this run still holds its fd and only when the
  name still is that file (`lstat` identity equals `fstat` of the held fd), on
  POSIX. A tiny window between that comparison and the unlink remains; the temp
  name is random (`.dic-<16 hex>.tmp`, mode 0600). On Windows an open file cannot
  be unlinked, so a temp from a refused run is left in place and listed.
- A failed `os.replace` itself leaves both the temp and the backup in place
  (IO_ERROR, `wrote: false`, target untouched).
- A failed rollback replace leaves its temp (holding the original bytes) and the
  backup in place, both listed (ROLLBACK_FAILED).

## Result codes

| result | exit | meaning |
|---|---|---|
| OK | 0 | check: would apply cleanly. apply: written, verified, directory fsynced (POSIX) |
| ALREADY_INSTALLED | 0 | section equals the block, `--old-section` given, stray and duplicate checks clean, and `--expected-final-sha256` (if given) matches |
| OK_DURABILITY_UNKNOWN | 1 | written and verified, but the POSIX directory fsync after the replace failed; a crash could lose the rename. Backup kept |
| SECTION_CHANGED | 1 | section is neither old nor block; or the file moved during apply |
| ANCHOR_MISSING | 1 | an anchor absent, or the end anchor is not after the start anchor |
| ANCHOR_DUPLICATE | 1 | an anchor matches more than one line |
| BLOCK_MISMATCH | 1 | block hash wrong, block shape wrong, or block line endings differ from the file's (never converted) |
| DUPLICATE_BLOCK | 1 | a long line of the block already appears outside the section |
| STRAY_OLD_TEXT | 1 | a long line of the old section also appears outside the section |
| UNVERIFIED_STRAY | 1 | ALREADY_INSTALLED reached without `--old-section`, or `--old-section` does not hash to `old_section_sha256` |
| SYMLINK_TARGET | 1 | the target is a symlink (checked at the start and again before the replace) |
| SYMLINK_ANCESTOR | 1 | a directory in the target's or backup directory's path is a link |
| PATH_REFUSED | 1 | a `..` component in a written path, or a verified directory no longer matches |
| MODE_NOT_SET | 1 | the target's mode could not be set on the replacement or did not read back |
| FINAL_MISMATCH | 1 | `wrote: false`: the final hash differs from `--expected-final-sha256`, nothing written. `wrote: true`: the post-write re-read failed; original restored and its rollback directory fsynced (or unsupported on Windows), backup at `backup_path` |
| RESTORED_DURABILITY_UNKNOWN | 1 | the post-write re-read failed and the original was restored (`restored: true`), but the directory fsync after the rollback rename failed; a crash could lose the rollback. Backup kept |
| ROLLBACK_FAILED | 1 | the post-write re-read failed and the rollback failed (`restored: false`); the rollback temp and the backup are listed in `left_in_place` |
| RECEIPT_PATH_REFUSED | 1 | the `--receipt` destination is unsafe (see Receipt); nothing run, nothing written |
| RECEIPT_VERIFIED | 0 | `--verify-receipt` with `--file` and pins: everything re-checked |
| CHECKSUM_ONLY | 1 | `--verify-receipt` without pins: the file still hashes to the receipt, nothing else was checked |
| RECEIPT_INVALID | 1 | `--verify-receipt`: unreadable, wrong shape, a different file, disagrees with the pins, fails the re-run, or the file changed |
| USAGE_ERROR, IO_ERROR, INTERNAL_ERROR | 2 | bad arguments, unreadable file or failed write, unexpected exception |

Every result carries `left_in_place`: files this run created and deliberately did
not remove. A non-empty list after a refusal is normal; clean them up by hand once
you have checked what they are.

"Long line": a line longer than 40 characters after trimming ASCII whitespace
(Python `bytes.strip`; a non-breaking space is not trimmed), compared trimmed and
whole against the trimmed lines of the prefix and suffix. Short lines (a bare "-",
most headings) are ignored. Both OK and ALREADY_INSTALLED run the stray and
duplicate checks before they can exit 0.

On the ALREADY_INSTALLED path the old section is no longer in the file, so the
stray check needs `--old-section FILE` (the old section text from the directive).
Without it the result is UNVERIFIED_STRAY, exit 1, never 0. On the replace path
the current section is the old section, so a supplied `--old-section` is not read
there. The block file must still be present and correct on the ALREADY_INSTALLED
path, because the duplicate check reads it.

## Platform differences

- Windows: no `dir_fd` (see Bound directories), no directory fsync
  (`dir_fsync: "unsupported"`, `dir_fsynced: false`, a `platform_note`, result
  stays OK), mode bits are only the read-only flag so the mode is not read back
  (`mode_checked: false`), temps of a refused run are left in place, and
  `os.replace` fails when another process holds the file open (IO_ERROR, target
  untouched). Directory junctions count as links.
- POSIX: a failed directory fsync is OK_DURABILITY_UNKNOWN (after the replace) or a
  refusal (for the backup, before the replace); `mode_checked` is true only after
  the replacement's mode read back equal to the original.

## Receipt

`--receipt PATH` writes the JSON for every result, refusals included. Before
anything runs, the destination is refused (RECEIPT_PATH_REFUSED) when it:
- has a `..` component,
- names the target, the block or the old-section file: by its caseless path
  (case-folded and Unicode-normalized, so NFC and NFD spellings match, as on APFS)
  whether or not that file exists yet, then by `samefile` for an existing alias (so
  on a volume that keeps case or NFC/NFD apart, such a receipt is refused as well),
- would take a backup name (`<file>.bak-...` in the backup directory; the name is
  compared case-insensitively, which only ever refuses more),
- lies in or under the target's directory, unless `--allow-receipt-in-target-dir`
  (even then it can never be the target or a backup). Containment is decided by
  directory identity (`st_dev`, `st_ino`) of the receipt directory and each of its
  ancestors, so a case alias on a case-insensitive volume is still recognized,
- already exists (receipts are created with `O_EXCL`, never overwritten),
- has a link in its path, unless `--allow-symlink-ancestor`.

The receipt directory is then opened and bound like the others, and the receipt
is created relative to it after the run. The receipt is ASCII JSON, mode 0644,
fsynced. Fields: `result`, `detail`, `mode`, `path`, `start_anchor`,
`end_anchor`, `end_of_file`, `old_section_sha256`, `old_section_file`,
`found_section_sha256`, `block_sha256`, `block_file_sha256`, `prefix_sha256`,
`suffix_sha256`, `file_sha256_before`, `final_sha256`, `expected_final_sha256`,
`wrote`, `backup_path`, `backup_dir_fsync`, `restored`, `dir_fsync`,
`dir_fsynced`, `dir_fd`, `platform_note`, `mode_checked`, `owner_preserved`,
`left_in_place`, `checked_at` (UTC), `checked_at_vienna`, `tool`, `tool_version`,
`tool_sha256` (hash of this script), `receipt_format` (2). A failed receipt write
exits 2 even after a successful install, and the partial receipt is left in place.

## --verify-receipt, and how directive-done would consume it

A receipt on its own proves little: it is a local file anyone can write. So
`--verify-receipt PATH` is a directive-done gate only with the intended rule file
and the directive's pins given again: `--file`, `--start`, `--end` or
`--end-of-file`, `--old-sha256`, `--old-section`, `--block`, `--block-sha256`
(optionally `--expected-final-sha256`; pass `--allow-symlink-ancestor` again if the
install needed it). Any pin without the rest, `--file` included, is USAGE_ERROR.
It then:
1. requires a JSON object with the expected typed fields, an absolute `path`,
   `receipt_format` 2, `mode` apply, and result OK or ALREADY_INSTALLED,
2. requires `--file` and the receipt's `path` to be the same file (`st_dev`,
   `st_ino`), neither a link, so a receipt for a staged copy cannot vouch for the
   intended file,
3. requires the receipt's anchors, `end_of_file`, `old_section_sha256` and
   `block_sha256` to equal the pins,
4. reads `--file` once and runs every check on that one snapshot: the full install
   check must give ALREADY_INSTALLED (so stray and duplicate text is caught again)
   and the snapshot must hash to `final_sha256`,
5. re-reads the file at the end without following a link and refuses, as "file was
   replaced during verification", if it is now a link (on Linux the no-follow open
   fails with ELOOP, reported the same way) or a different file (`st_dev`, `st_ino`),
   and as "file changed during verification" if its bytes moved. On POSIX the first
   read's descriptor stays open until that comparison, so an unlink-and-recreate
   cannot be given the snapshot's freed inode number and pass as the same file.
Only RECEIPT_VERIFIED exits 0. A write after the verifier returns can of course
still change the file; the answer is about the moment it was given.

Without pins the answer is CHECKSUM_ONLY (exit 1) when the file still hashes to
`final_sha256`, and RECEIPT_INVALID when it does not. CHECKSUM_ONLY is not a
directive-done gate. Receipts from the earliest draft (`receipt_format` 1) are
refused as an unknown format.

`meshy_node directive-done --key K --outcome done --install-receipt FILE` is not
built and meshy_node is untouched. It should accept a receipt only through the
pinned verification above, and otherwise send `--outcome refused` with the code.

## Named limits (from the round-4 review)

- R4, receipt containment between preflight and bind, and physical receipt
  ancestry and parent continuity: "Receipt containment is a preflight check over
  lexical ancestors; allowed ancestor links and directory topology changes can
  place a receipt under the target directory despite the separate allow flag being
  absent. Use stable direct receipt paths."
- Verifier identity and no-link continuity through the final read: closed for
  POSIX by the two Codex follow-ups on PR 556 (the final read is no-follow and compared by identity, and
  the snapshot descriptor is held open through it). On Windows the handle is closed
  at once, because an open handle blocks the replacement instead of letting it be
  seen; there the identity compare assumes NTFS does not reuse the file id within
  one verification. Not measured.
- Windows `dir_fd` window: Windows has no `dir_fd`, so each effect re-checks the
  directory identity first and a window remains between that re-check and the
  effect (`dir_fd: false`).
- RED-proofs that prove by crash: some mutated checks fail their case by raising
  (missing anchor IndexError, the result boundary, the neither-`--end` case, the
  receipt-schema cases reported as INTERNAL_ERROR) rather than by giving a
  different refusal; and some overlapping checks are disabled jointly. The RED
  count is not an independent mutation score.
- Backups are mode 0600, not the target's mode.
- `meshy_node directive-done --install-receipt` is not built; meshy_node is
  untouched.

## Limits

- One file per run. A directive covering several files needs a run per file.
- It does not prove the runtime reloaded the file; loaded-path hashing covers that.
- The stray and duplicate checks are line-based: a short conflicting rule (40
  characters or less) is not seen, and a long line legitimately repeated elsewhere
  in the file refuses.
- The pre-replace re-read narrows the race but is not a compare-and-swap; the temp
  cleanup compare-then-unlink has a tiny window; on Windows the directory re-check
  before each effect has a window too.
- A post-write restore drops any concurrent writer's change too; the backup keeps
  the pre-install bytes. `restored: true` proves the bytes, not every metadata bit.
- Ownership is preserved only when running as root; otherwise a different owner is
  warned on stderr and `owner_preserved` is false.
- Re-running on an already-installed file does not re-fsync it, so ALREADY_INSTALLED
  does not upgrade an earlier OK_DURABILITY_UNKNOWN.
- Some checks overlap, and the selftest says so: an existing-file receipt alias is
  caught by both the alias check and exclusive creation (RED-proved jointly).
- The 7 Nov selftest fixture is a reconstruction of that failure's shape, not the
  real file's bytes.
