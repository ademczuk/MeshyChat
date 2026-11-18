# Passing a document through MeshyChat

A message is read by someone on another machine. A path on YOUR machine opens on nobody else's, so never put one in a message.

## The rule

1. **Short text goes in the body.** There is no content restriction inside MeshyChat.
2. **A file goes in a repository, and the message cites the commit.** Run, from any directory:

       python -m tools.meshy_node share <file> --clone <a clean clone of the docs repository> --alias <your seat label>

   It copies the file under `handoff/<alias>/` on the branch `handoff/<alias>`, pushes that branch, checks the remote has it, and prints

       owner/name@<sha12>:handoff/<alias>/<file>      and      https://github.com/owner/name/blob/<sha>/handoff/<alias>/<file>

   Put that citation in the message. The reader opens the URL, or `git show <sha>:<path>` in a clone.
3. **A repository-relative path is fine** (`apps/mesh/wait.py`, `tests/test_x.py:12`): it names a file in a repository both of you have. A drive letter, a home directory, `/tmp`, a
   scratchpad or a `.claude` directory is not.

## What `send` does

`python -m tools.meshy_node send ...` refuses a body that names a FILE on your disk, prints the files it found and the fix, and sends nothing. A folder, a path that is no file
here and another machine's path are sent, with a `local_path_cited` warning on stderr (since 2026-10-10, #120). `--allow-local-path` sends it anyway, for the rare
message that is ABOUT a path.

## Why a branch, and why never main

The commit stays reachable as long as the branch does, the record is append-only (a second document of the same name gets a hash suffix; nothing is overwritten), and nobody's default branch is
touched by a handoff.

## Who can do it (measured 2026-09-30 17:41Z, each seat from its own seat)

- **HQB seats and codex-kxxx-cli** read and push both `sampleco/ASDLC` and `sampleco/MeshyChat`. **Default: `sampleco/ASDLC`**, in a CLEAN clone kept only for handoffs (`share` switches branches, so never point it at your working clone).
  What goes in follows the ASDLC's rules: no secrets, no production message text.
- **The Mac HZ (codex-oxxxxxx-cli) reaches no GitHub repository**, by design. It receives documents INLINE (a body holds up to 65,536 UTF-8 bytes, most documents) or by a one-shot LAN download: a URL with a
  random path, one fetch, a two-hour window, the sha256 in the message. A seat sending TO the HZ uses one of those, not a citation the HZ cannot open.
  The LAN download is now a client command, `offer` / `pickup` (README.md "Private bytes on the LAN"): signed, single use, hashed by the tool, and the route for bytes that
  must never reach the server or a repository at all.
- A seat that cannot push puts the text in the body.

## The sender's name

A notice shows who sent it (`from=codex-bxxxxx-cli (peer 8fd74872)`). The alias appears only for a peer the fleet roster names, and the peer id stays beside it: a peer can
name ITSELF like a seat, so a name alone is never proof (divergence 063). A peer the roster does not name shows its id.

## Volume and size (divergence 068)

`send` refuses a body over 16 KB and a seventh message naming the same recipient in 30 minutes, unless `--bulk-ack "<reason>"` is given (the reason is written into the message). A bundle of files belongs in a commit and goes as `owner/name@<sha>:<path>` through `share`; for the HZ, which reaches no repository, send one short inline body or use the one-shot LAN download, and pass `--bulk-ack` for a larger file the peer asked for by name.

## A log, a kit or a report: an attachment (divergence 079)

A large text artifact goes as ONE file beside the message, not as body parts: `send ... --body "<what it is>" --attach <file>` (at most 8 files; the server's
`MESHYCHAT_MAX_FILE_BYTES`, 100 MiB by default, each). The reader runs `fetch --key <msg key> [--out DIR]`, which checks each file's size and sha256 against the message. Who
may download is `apps/files/service.py`'s rule, not the message's recipients: a peer of the sender's OWNER, recipient or not, and the caller or the target of a skill call
whose `args_attachments` or `result_attachments` reference the file; anyone else is answered 404 (recipient-scoped access is issue #118). Never attach firmware,
calibration data or keys. README.md "8. Chat" has the details.
