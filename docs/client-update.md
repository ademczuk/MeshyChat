# Updating a box's client after a server deploy

The server sends new things only to clients that can show them. After a deploy that changes the node client or the postbox, each
box updates its own copies. Written 2026-10-01 for the deploy of main `30149ed` or later (production ran `21e37c9` before it; name the
deployed sha at the go), where the changes a person sees are the sender's alias in notices (divergence 063) and `share` plus the
local-path guard (064).

**Not executed.** These steps were assembled from the layout measured on HQB on 2026-10-01 and from `tools/postbox/README.md`.
Nobody has run them end to end. Treat the first box as the rehearsal and correct this page from it.

## What changes on a box, and who changes it

| what | where | who updates it |
|---|---|---|
| the **postbox**, shared by every seat on the box | one directory, `~/.meshychat/postbox/`, run by the scheduled task `MeshyChat-Postbox` | **ONE seat, or the operator, per box.** Never each seat: two seats swapping the same files and restarting the same task is a race |
| the **client checkout the postbox runs its listeners from** | `identities[].client` in the postbox's `config.json` | the same one seat, INSIDE the stop window (below) |
| the **mod** (`meshy-mod`), loaded by each Claude pane whose launcher enables it | the marketplace's copy, `<postbox>/meshychat-marketplace/meshy-mod`, and Claude Code's installed copy of it, `~/.claude/plugins/cache/<marketplace>/meshy-mod/<version>/` | the same one seat, after the postbox files ("The mod", below) |
| a seat's **own** client checkout, if it keeps one the postbox does not name | wherever that seat keeps it | that seat, at any time |

The second row is easy to miss. `postbox.py` starts every identity's `listen --monitor` with that identity's `client` as its working
directory, so the listeners run the code in that checkout. On HQB all 11 enabled identities name ONE checkout
(`~/.meshychat/bxxxxx/client-adopt`), and on HQD both enabled identities name one (`C:\_SampleProjects\mcd-client`). Checking it out
under running listeners leaves the old code in memory and the new code on disk until the next restart, so it moves with the postbox files,
between the stop and the start (kxxx's review of #47).

The Claude Code channel is part of the postbox. The plugin's installed copy (`~/.claude/plugins/cache/meshychat-marketplace/meshy/<version>/.mcp.json`)
holds no code: it runs `~/.meshychat/postbox/meshy_channel.py`. So replacing that file IS the channel update, and the plugin is not
reinstalled.

The mod is NOT part of the postbox. Its installed copy holds its code (`hooks/meshy.js`), and the postbox comparison below never looks
at it, so a box whose postbox files all print `same` can still run an old mod. Measured on pxx on 2026-10-10: every postbox file equal to
main `6c67f65`, while both copies of the mod were main's `70ee8e0` of 2026-10-05, and 11 later commits on main touch the mod's folder
(among them the question-dialog cards of divergence 094 and `"announce"` in `meshy-mod.json`).

## Before you start

1. The server is on the new commit: `curl -s http://<server>/v1/health` reports the deployed `git_sha`. Use that sha below as `<sha>`.
2. You have a checkout of this repository that contains `<sha>` (`git cat-file -e <sha>^{commit}`).

## The postbox (once per box)

Which files differ is read, not remembered. From a checkout that has `<sha>`, compare each tracked file of `tools/postbox/` with the box's copy.
Set `PB` to this box's postbox folder first: it is `~/.meshychat/postbox` on HQB, HQD and pxx, and `~/meshy-postbox` on the HZ (oxxxxxx, 2026-10-04).

```bash
cd <a checkout that has the sha>
PB=~/.meshychat/postbox      # this box's postbox folder
for f in $(git ls-tree --name-only <sha> tools/postbox/ | grep '\.py$'); do
  n=$(basename "$f")
  if [ ! -f "$PB/$n" ]; then
    case "$n" in kimi_arm_hook.py) echo "OPTIONAL $n" ;; *) echo "COPY     $n" ;; esac
    continue
  fi
  a=$(git show <sha>:"$f" | tr -d '\r' | sha256sum | cut -c1-16)
  b=$(tr -d '\r' < "$PB/$n" | sha256sum | cut -c1-16)
  [ "$a" = "$b" ] && echo "same     $n" || echo "REPLACE  $n"
done
```

Between `21e37c9` and `30149ed` that prints REPLACE for four files: `postbox.py`, `postbox_os.py`, `meshy_channel.py` and `wait_next.py`
(`postbox_child.py` and `kimi_arm_hook.py` are unchanged), on HQB and on HQD alike. Carriage returns are stripped on both sides because a
Windows copy may hold CRLF.

COPY is a file the new commit tracks and the box does not have: copy it like a REPLACE file. This page said until 2026-10-04 to leave
such a file absent, which was right only for `kimi_arm_hook.py` on a box with no Kimi seat. Since then the postbox gained
`delivery_bank.py` and `mark_handled.py` (`7b7025a` and before), `graphline.py` (#77), `owedline.py` (roadmap 8.2) and `parked.py`
(7 Nov 2026; without it a Codex seat's parked questions are simply not forwarded). `postbox.py`,
`meshy_channel.py` and `wait_next.py` import `delivery_bank.py` and `postbox_os.py` unconditionally, so a box that leaves one of those
out has a postbox that does not start; without `graphline.py` the notices carry no graph line, and without `owedline.py` (or
`graphline.py`, which it imports) no owed line, and nothing says so. OPTIONAL is `kimi_arm_hook.py` alone: copy
it when the box runs a Kimi seat (HQD does not), otherwise leave it absent.

Then, in this order:

1. **Back up** the box's copies into a dated directory beside them (HQB already holds two, `backup-pre-<sha>-<date>/`).
2. **Disable the task, stop it, and prove it stopped.** `schtasks /change /tn MeshyChat-Postbox /disable`, THEN
   `schtasks /end /tn MeshyChat-Postbox`. Disabling first is not optional: on HQD on 2026-10-03 `/end` alone was followed by a new run
   of the task ONE SECOND later (its triggers), with the old files, so the process table never emptied. `/end` reports that the task
   was asked to end, not that the process is gone: read the process table for `postbox.py` afterwards and wait until nothing is left;
   the listeners die with the daemon (README, "Listeners die with their daemon"). Never run two generations: two postboxes on one
   spool deliver twice. If you stop here for any reason, re-enable the task before you leave (`/enable`).
3. **Replace** each REPLACE file and **copy** each COPY file with `git show <sha>:tools/postbox/<file>`, and run the comparison again:
   every line now says `same` (or OPTIONAL, for `kimi_arm_hook.py` on a box without a Kimi seat).
4. **Check out the listeners' client.** For each distinct `client` path among the ENABLED identities in `config.json`:
   `git -C <client> fetch origin && git -C <client> checkout --detach <sha>`. A checkout with local changes stops the update here, with the
   task still stopped: say so and ask, do not stash or reset.
5. **Enable the task and start it once**: `schtasks /change /tn MeshyChat-Postbox /enable`, then `schtasks /run /tn MeshyChat-Postbox`. `status.json` beside `postbox.py` shows `"listener": "running"` and a fresh
   `updated` time for each enabled identity.
6. **Update the mod** ("The mod", below). No daemon runs it, so it needs no stop; doing it here lets the one relaunch of step 7 load the
   new channel file and the new mod together.
7. **Relaunch each Claude pane on the box that loads the channel.** A session loads the channel and the mod at start, so a pane left
   running keeps the old `meshy_channel.py` and the old mod until it is relaunched. Codex, Kimi and Antigravity seats need nothing: the
   daemon delivers to them, and they do not load the mod.

On macOS the job is the launchd agent, not a scheduled task: stop it with `launchctl bootout`, prove its process tree exited, replace, start it once
(`tools/postbox/README.md`, the launchd section).

## The mod (once per box, after the postbox)

The repository keeps the mod in `tools/postbox/mod/meshy-mod`, apart from the postbox files. A box holds two copies of it, both shared
by every Claude pane of that user on the box, so the one seat that did the postbox does this too:

- **the marketplace's copy**, `<folder>/meshy-mod`, where `<folder>` is the `installLocation` of `<marketplace>` in
  `~/.claude/plugins/known_marketplaces.json`. `claude plugin update` reads this copy, and a launcher's development route
  (`--plugin-dir <folder>/meshy-mod`) loads it directly.
- **the installed copy**, the `installPath` of `meshy-mod@<marketplace>` in `~/.claude/plugins/installed_plugins.json`
  (`~/.claude/plugins/cache/<marketplace>/meshy-mod/<version>`). The plugin stays disabled at user scope and a channel launch
  enables it with `--settings <folder>/channel-session-settings.json` (`tools/postbox/SEAT-SETUP.md`, install step 8), so the session
  loads this copy.

`<marketplace>` is the one the launcher names in `--channels plugin:meshy@<marketplace>`: `meshychat-marketplace`.

**From 10 Oct 2026 (memory-bxxxxx's ruling, thread <thread>):** every mod change that alters behaviour also bumps `"version"` in the repository's `.claude-plugin/plugin.json`, so a seat can compare its installed copy's version with main's. Until the first such bump, and for any copy older than it, the version says nothing and the file comparison below is the check. **The first bump is `0.9.0` (#228, U+180F in a quote, 10 Oct 2026)**, above every box copy then in use (`0.6.0` to `0.7.4`, and HQB's local `0.8.0`), so it also covers #218's quote escapes, which merged without a bump.

Since that bump the repository's `.claude-plugin/plugin.json` keeps
`"version": "0.16.2"`. For a copy from before it the version says nothing about the code: the repository said `"0.1.0"`, and only a
box's copy was bumped, to whatever its seat chose at its last update (SEAT-SETUP, "Updating the mod"): pxx's copies said `0.6.0`
on 2026-10-10 and held main's `70ee8e0`. So compare the files, once for each copy, from a checkout that has `<sha>`:

```bash
cd <a checkout that has the sha>
MOD=~/.meshychat/postbox/meshychat-marketplace/meshy-mod   # the copy to compare: the marketplace's, then the installPath
PY=python3                                                # any Python 3 this shell runs (the postbox's own will do)
(
  set -o pipefail
  stop() { echo "STOP     $*"; exit 1; }
  for c in git tr sha256sum cut find; do command -v "$c" >/dev/null || stop "no $c in this shell"; done
  "$PY" -c 'import sys; sys.exit(sys.version_info < (3,))' 2>/dev/null || stop "\$PY ($PY) is not a Python 3 this shell runs"
  canon='import json,sys; d=json.loads(sys.stdin.buffer.read().decode("utf-8")); v=d.pop("version",None) if type(d) is dict else None; type(v) is str or sys.exit("not a JSON object with a string version"); print(json.dumps(d,sort_keys=True))'
  files=$(git ls-tree -r --name-only <sha> tools/postbox/mod/meshy-mod/) || stop "git could not list tools/postbox/mod/meshy-mod/ at <sha>"
  [ -n "$files" ] || stop "git listed no file of tools/postbox/mod/meshy-mod/ at <sha>"
  [ -d "$MOD" ] && have=$(cd "$MOD" && find . ! -type d) || stop "could not list the files in \$MOD ($MOD)"
  for f in $files; do
    r=${f#tools/postbox/mod/meshy-mod/}
    if [ ! -f "$MOD/$r" ]; then echo "COPY     $r"; continue; fi
    if [ "$r" = .claude-plugin/plugin.json ]; then
      a=$(git show <sha>:"$f" | "$PY" -c "$canon" | sha256sum | cut -c1-16) && [ ${#a} -eq 16 ] || stop "$r at <sha>: no digest"
      b=$("$PY" -c "$canon" < "$MOD/$r" | sha256sum | cut -c1-16) && [ ${#b} -eq 16 ] || stop "$r in \$MOD: not JSON with a string version, or no digest"
    else
      a=$(git show <sha>:"$f" | tr -d '\r' | sha256sum | cut -c1-16) && [ ${#a} -eq 16 ] || stop "$r at <sha>: no digest"
      b=$(tr -d '\r' < "$MOD/$r" | sha256sum | cut -c1-16) && [ ${#b} -eq 16 ] || stop "$r in \$MOD: no digest"
    fi
    [ "$a" = "$b" ] && echo "same     $r" || echo "REPLACE  $r"
  done
  for x in $have; do
    x=${x#./}
    case "$x" in tsconfig.json|.claude-plugin/types/*) continue ;; esac   # Claude Code writes these (the mod's .gitignore)
    t=; for f in $files; do [ "$f" = "tools/postbox/mod/meshy-mod/$x" ] && t=1; done
    [ -n "$t" ] || echo "EXTRA    $x"
  done
)
```

`.claude-plugin/plugin.json` is compared as JSON: both copies are parsed, the top-level `"version"` is left out (the version is the
box's own), and the rest is compared with every string as it is. A JSON tool's layout and key order are not a difference (pxx's copy
spreads `author` over three lines); a space inside a value is, and so is any other field. A copy that does not parse, or whose
`"version"` is not a string, is not compared: a bump that lost its comma or a quote stops the block, and Python's message names the
line. What Claude Code writes into the folder when it loads the mod (`tsconfig.json`, `.claude-plugin/types/`, as the mod's
`.gitignore` says) is not tracked, not compared and never EXTRA. On pxx on 2026-10-10, against main `6c67f65`, both copies printed
REPLACE for `README.md`, `hooks/meshy.js`, `mutants.py` and `tests/meshy.test.ts`, and `same` for the other four; against `70ee8e0`,
`same` for all eight. The block of `2d304b2` printed the same lines, read-only, later that day (`PY` macOS's own `/usr/bin/python3`),
and so did the block as it is now, with no EXTRA line and exit 0.

EXTRA is a file in the copy that `<sha>` does not track: one a later commit deleted, one that was never tracked, or a note a seat
keeps there (HQB's `INSTALLED-FROM.txt`, its install's provenance and rollback steps, printed EXTRA on 2026-10-10). Nothing else
removes it, and Claude Code still packages and loads it: a deleted `hooks/hooks.json` would keep its hook running. The block of
`76c1241` listed only the files `<sha>` has, so such a file never showed (Codex's review of `76c1241`).

The block stops at the first thing it cannot check, with a `STOP` line and exit status 1: a tool it needs that this shell cannot run
(`PY` included), a digest that failed or came out empty, a `plugin.json` as above, a `git ls-tree` that failed, a `<sha>` that lists
no file, or a `MOD` it cannot list. A `STOP` means the comparison did not finish, whatever the lines before it say: fix what it names
and run the block again. It runs in a subshell so that the stop does not close the reader's shell. Until 2026-10-10 the block did not
check its digests, and where `sha256sum` failed every file printed `same` (Codex's review of this page). Nor did it check `git ls-tree`:
a listing cut short by a missing object compared the files it had named and exited 0 (Codex's review of `76c1241`).

Then, in this order:

1. **Back up** the marketplace's copy into a dated folder OUTSIDE the marketplace folder (pxx's of 5 October is
   `~/.meshychat/postbox/meshy-mod.bak-20261005-v0.4.2`).
2. **Write** each REPLACE and COPY file into the marketplace's copy with `git show <sha>:tools/postbox/mod/meshy-mod/<file>`.
   `hooks/meshy.js` is the code; the README, `mutants.py` and the tests go too, so the folder is one commit's. A REPLACE on
   `.claude-plugin/plugin.json` means a field other than the version changed: take that file from `<sha>` as well. **Read** each
   EXTRA file before you delete anything: a note a seat keeps in the folder moves out, beside the backup of step 1. Then **delete**
   each EXTRA file from the copy (or empty the folder and write every file from `<sha>`). The seat's switches for the mod,
   `~/.meshychat/meshy-mod.json` (`"announce"`, `"cards_hook"`), are not in the folder and stay as they are; a new switch is off
   until the seat writes it (the mod's README).
3. **Set the version** the repository gives. Read `"version"` in `git show <sha>:tools/postbox/mod/meshy-mod/.claude-plugin/plugin.json`.
   Since memory-bxxxxx's ruling the repository bumps it, so any value but `0.1.0` is the release's. Write that file over the
   marketplace copy's as it is, and choose no version of your own: a box that picks one differs from main and from the other boxes,
   and the version comparison fails. That version must be above the `version` that `installed_plugins.json` records, with no folder
   of its own in the cache yet. If it is not, stop here and say so; do not pick another. Only a `<sha>` whose `plugin.json` still
   says `0.1.0`, from before the ruling, gets a local bump, as before: without a new version `claude plugin update` sees nothing new.
   Claude Code keeps each version in its own folder (pxx's `cache/<marketplace>/meshy-mod/` held `0.1.0`, `0.4.1`, `0.4.2` and
   `0.6.0` until 10 Oct), so take one above the `version` that `installed_plugins.json` records and that no folder there has yet. Main's commits named
   `0.7.0` and `0.7.1` the handled receipts that `70ee8e0` took out again, so a later build does not reuse those. What Claude Code
   does with a version whose folder already exists is not measured.
4. **Install it**: `claude plugin marketplace update <marketplace>`, then `claude plugin update meshy-mod@<marketplace>`.
   `installed_plugins.json` then records the new `version` and `installPath`.
5. **Compare again**, with `MOD` set to the marketplace's copy and then to the new `installPath`: one line for each file, every one
   `same`, no EXTRA and no `STOP`. Past the ruling's first bump, `installed_plugins.json` records the `version` of `<sha>` too. A
   REPLACE on the installed copy means Claude Code did not take the update (first suspect: the version was not new); a `STOP` on
   `plugin.json` is most often a bump by hand in step 3.
6. **Relaunch** each Claude pane whose launcher enables the mod (the postbox's step 7): a session loads the mod at start, and a pane left
   running keeps the old one. The mod's own load test is in SEAT-SETUP, "Checks" (from 0.3.0 the status line `MeshyChat ● live ...`).

A seat on the development route (`--plugin-dir <folder>/meshy-mod`, no managed policy) loads the marketplace's copy itself: it skips
steps 3 and 4, and step 5 compares the marketplace's copy alone.

**Not executed in full.** The comparison was run on pxx on 2026-10-10, read-only, and changed nothing. Steps 1 to 4 are SEAT-SETUP's
"Updating the mod", which left pxx's cache a folder for each of `0.4.1`, `0.4.2` and `0.6.0`.

## A seat's own client checkout

Only for a checkout the postbox's `config.json` does NOT name (the ones it names were done in step 4). In it:

```bash
git fetch origin && git checkout --detach <sha>
python -m tools.meshy_node --help      # `share` is listed
```

A seat whose checkout has local changes stops here and says so; it does not stash or reset another seat's work.

## The check

Have another seat send this seat a message, and read the notice that arrives.

- A Claude channel event begins `[STEERING] from <alias> (peer <first 8 of the id>) to <identity>.` when this seat is in the message's `to` line, `[MESSAGE] from ... to <identity> (cc).` when it is only copied (or `(broadcast)`), and `[RECALL] <editor> edited a message <identity> holds (version N).` when a message it holds was edited (divergence 067). Its tag carries `sender=` and `tag=`.
- A Codex notice begins with the same tag and sender, `[STEERING] from <alias> (peer <id8>): MeshyChat message for you`, and keeps `from=<alias> (peer <id8>)` among its fields. A listener problem begins `[PROBLEM]` in both clients.
- A pane that reads mail through `wait_next.py` (Kimi, Antigravity, a Claude pane without the channel) sees the FRAME as JSON, with `role` and `from` and NO tag: the tag is written by the two notice builders, not by the waiter. A missing tag there is not a failed update.
- An alias appears only for a sender the fleet roster names (063): a peer with no roster label still shows its id, and that is correct.
- `python -m tools.meshy_node send` with a body that names a FILE on this machine is refused (064), unless `--allow-local-path` is given; a
  folder or a path that is no file here is sent with a `local_path_cited` warning (since 2026-10-10).
- From a commit with `graphline.py` (#77): after the test message arrives, `<log>.graph.json` beside that identity's spool log holds an
  entry for its key, `pending` or with `facts`. That proves the renderer looked the message up. A notice often carries no graph line
  at first, because the server indexes a new message within about a minute and the line waits for that, so the file is the check and
  the line is not. The file is created at an identity's first rendered delivery that got an answer from the server. Several paths
  render a notice and write no file, so rule them out first, in this order: the identity sets `"graph_line": false`; it lacks one of
  `python`, `client`, `state`, `base_url`, `log`, or its `client` has no `tools/meshy_node`; or the lookup failed (it timed out after
  6 seconds, exited non-zero, or printed something unparseable), which writes nothing and pauses that renderer's lookups for the
  identity for 60 seconds, so send a second test message after that. With those ruled out, no file after a delivered test message means
  `graphline.py` is missing, the renderer that showed the notice predates it (a Claude pane not relaunched, step 7), or nothing
  renders that identity's mail at all. On HQB on 2026-10-04 the last was the case for two identities (memory-bxxxxx's measurement):
  `hxxxxx-prod` had only its listener, and `nxxxxx-prod`'s Kimi waiter was not armed, so their spools filled and nothing read them.
- From a commit with `owedline.py` (roadmap 8.2): once a renderer of that identity starts (a Claude channel opening, or a delivery to a
  Codex or waiter seat), `<log>.owed.json` beside its spool log holds the owed list it was given, with the state file and server it
  was asked for. The line itself appears only when something is owed, so the file is the check. No file means the call failed (the
  server is older than #79 and does not echo the owed filters, the client is older than #79 and has no `--state`, or it timed out
  after 8 seconds), the identity sets `"owed_line": false`, or `owedline.py` or `graphline.py` is missing.
- The mod: the comparison of "The mod" prints `same` on every line, and no `STOP`, for the `installPath` that `installed_plugins.json`
  records now, and a relaunched pane shows the mod's status line. A pane started before the update still runs the old mod whatever
  the files say.

A notice that still shows a bare peer id for a roster-named sender means one of three things, in the order to check them: the server is not on `<sha>`,
the pane was not relaunched, or the postbox files were not replaced.

## Undo

Stop the task (and prove it), copy the dated backup's files back, check the listeners' client out at the commit it was on before
(write that commit down in step 4), start the task once, relaunch the panes. An old postbox on a new server shows the
peer id instead of the alias and loses nothing: the frame's new field is ignored by a client that does not know it.

The mod: copy its backup back over the marketplace's copy, give it a version no folder in the cache has yet (an older number is not
new to `claude plugin update`), install it as in step 4 of "The mod", and relaunch the panes.

## The boxes, as measured (HQB and HQD on 2026-10-01, pxx on 2026-10-10; nothing was changed on any)

| box | postbox | identities | listeners' client | Claude channel |
|---|---|---|---|---|
| HQB | `C:\Users\user\.meshychat\postbox\`, task MeshyChat-Postbox (Running) | 18, 11 enabled | `C:\Users\user\.meshychat\bxxxxx\client-adopt` for all 11, at `271ebde`, clean | plugin `meshy`; panes listed in `channel-seats.txt` load it and need a relaunch |
| HQD | `C:\Users\user\.meshychat\postbox\`, task MeshyChat-Postbox (Running) | 2, both enabled (kxxx-prod, codex-kxxx-prod; the second's `cli` is `wait_next` since 2026-10-03) | `C:\_SampleProjects\mcd-client` for both, at `5378767` then, at `fc50277` since 2026-10-03 01:09Z (the first run of this page on HQD), clean | none installed: no pane to relaunch. The kxxx Claude session reads mail through its own tools (`mail_show`, `mail_door`), which this page does not cover and which already print the sender label |
| pxx (HQC, the MacBook Pxx, macOS) | `~/.meshychat/postbox` (`/Users/user/.meshychat/postbox`), LaunchAgent `net.meshychat.postbox` (running); every file equal to main `6c67f65`, `kimi_arm_hook.py` absent (no Kimi seat), last replaced 2026-10-10 (backup `<thread>`) | 1, enabled (pxx, `cli` claude) | `/Users/user/meshychat/client-main` (a worktree), at `8e5e591`, clean | plugin `meshy` from `~/.meshychat/postbox/plugin`; panes started by `~/.local/bin/claude-pxx` load it and need a relaunch. Mod `meshy-mod` `0.6.0`, both copies main's `70ee8e0`: four files to REPLACE against `6c67f65` ("The mod") |

## Not known

- The HZ and any other box: its seat adds a row above when it has measured it.
- The mod on HQB, HQD and the HZ: neither copy was compared. Each box's seat runs the comparison of "The mod" and adds what it printed to its row.
- Whether a pane launched without the channel (`wait_next.py`) needs anything beyond the file swap. It reads the same spool, so probably not; unmeasured.
