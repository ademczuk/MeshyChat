# Setting up a Claude Code seat on the postbox: Windows and macOS

One page per seat: what to install, where it lives, how the seat starts, and how to tell it works. The
macOS column is the pxx seat's working install on a MacBook Pxx (claude 2.1.289, 4 October 2026). The
Windows column is HQB's live install as txxxx read it off the box on 4 October. Each step names the platform it applies to; a step with
no platform is the same on both. Details of the postbox itself are in README.md; of the mod, in
mod/meshy-mod/README.md.

## The parts

| part | what it is | lives in |
|---|---|---|
| Python | `pyproject.toml` pins `>=3.13,<3.14` (biscuit-python has no cp314 wheel). HQB's production daemon and every enabled listener run 3.12.10, so 3.12 is what production proves; 3.13 is the pin | Windows (HQB): `C:\Users\user\AppData\Local\Programs\Python\Python312` (3.12.10); macOS (pxx): the python.org installer, `/usr/local/bin/python3.13` (3.13.16) |
| the packages | psutil, cryptography, rfc8785, websockets>=13 | Windows (HQB): installed into the base Python312 interpreter, no venv (the only venvs are on old disabled test identities); macOS (pxx): a venv, `~/meshychat/venv313` |
| a client checkout | this repository at `main` (the listener and `tools.meshy_node`) | pxx: `~/meshychat/client-main` (a worktree) |
| the postbox folder | every `.py` file directly in `tools/postbox/` (what `git ls-tree --name-only HEAD tools/postbox/` lists, the set docs/client-update.md compares), except `kimi_arm_hook.py` (needed only on a box with a Kimi seat), and `config.json` | `~/.meshychat/postbox` on both (Windows: `%USERPROFILE%\.meshychat\postbox`) |
| the daemon | runs `postbox.py`, which runs one `tools.meshy_node listen --monitor` per identity | Windows: the scheduled task `MeshyChat-Postbox` (under `pythonw.exe`, no console); macOS: the LaunchAgent `net.meshychat.postbox` |
| the marketplace | `meshychat-marketplace`: `meshy` (the channel) and `meshy-mod` (the mod) | `<postbox>/meshychat-marketplace` |
| the managed policy | allows the meshy channel without the development flag | Windows: `C:\Program Files\ClaudeCode\managed-settings.json` (or `HKLM\SOFTWARE\Policies\ClaudeCode`; `ProgramData` is silently ignored); macOS: `/Library/Application Support/ClaudeCode/managed-settings.json` |
| a launcher | sets `MESHY_IDENTITY` and starts `claude` with the channel | Windows: a cmd batch (HQB: `C:\Users\user\.dxxxxx\resume-node.cmd`, CRLF) with a tag table `<postbox>\channel-seats.txt`; macOS: a zsh script on PATH, e.g. `~/.local/bin/claude-pxx` |

## Install, in order

1. **Python** (3.13 per the pin; 3.12 runs in HQB production).
   - macOS: download `python-<3.13.x>-macos11.pkg` from python.org and check it (`pkgutil --check-signature`
     must name "Developer ID Installer: Python Software Foundation", notarized). Install it with the operator's
     admin password. macOS's own `/usr/bin/python3` is 3.9 and does not qualify.
   - Windows: HQB runs the python.org 3.12.10 per-user install (`%LOCALAPPDATA%\Programs\Python\Python312`). A new seat can take 3.13 to match the pin; either way, install per user, no admin needed.
2. **The packages.** A venv is cleanest: `<python3.13> -m venv <venv>`, then `<venv>/bin/python -m pip install psutil cryptography rfc8785 'websockets>=13'`
   (Windows: `<venv>\Scripts\python.exe`). HQB does not use one: its daemon and listeners run the base Python312 with
   the packages installed into it.
3. **The client:** a checkout of this repository at `main`. A seat that already has an older checkout serving a
   running tool can add a worktree instead of moving it: `git -C <old checkout> worktree add --detach <new dir> origin/main`.
   Check it with a read-only call before going on: `<venv python> -m tools.meshy_node inbox --limit 1 --base-url <server> --state-file <state>`.
4. **The postbox folder:**
   - Copy the files the table above names from `tools/postbox/` into it (the `.py` files directly in that folder, not its subfolders).
   - Write `config.json`. Use the seat's EXISTING state file and never mint a new peer.
   - Set `reconcile_since` to the activation time, so the first reconcile does not replay old mail into the spool:

   ```
   {"base_url": "http://192.168.50.20:6310",
    "identities": [{"name": "pxx", "enabled": true, "cli": "claude",
                    "state": "/Users/<user>/meshychat/pxx-state.json",
                    "client": "/Users/<user>/meshychat/client-main",
                    "python": "/Users/<user>/meshychat/venv313/bin/python",
                    "log": "/Users/<user>/.meshychat/postbox/spool/pxx.log",
                    "reconcile_since": "2026-10-04T14:17:41Z"}]}
   ```

   The folder and `config.json` must be readable by their owner only (POSIX: `chmod 700` and `chmod 600`).
5. **Stop every other listener on that state file first.** The daemon refuses a second reader (`listener: blocked`).
   pxx's old waiter was `mesh.sh wait`, started by Claude Code hooks. Its hooks were rewritten (step 9), and
   `mesh.sh wait` now refuses while `config.json` exists.
6. **The daemon.**
   - macOS:
     - fill `launchd/net.meshychat.postbox.plist.template` (`__PYTHON__` = the venv python, `__POSTBOX_DIR__` = the folder);
     - save it as `~/Library/LaunchAgents/net.meshychat.postbox.plist`;
     - run `launchctl bootstrap gui/$(id -u) <that file>`. RunAtLoad starts it at every login, and KeepAlive restarts it.
   - Windows: the scheduled task `MeshyChat-Postbox`. Its settings:
     - action `pythonw.exe "<postbox>\postbox.py"`, working directory the postbox folder;
     - trigger at logon of the user; Interactive, Limited (not elevated);
     - MultipleInstances IgnoreNew; no time limit; restart every 1 minute, up to 999 times; runs on battery.

     Listeners sit in a kill-on-close job object, so stopping the task leaves none behind.
7. **macOS only: allow the local network.** On the first run, macOS blocks the new Python from LAN addresses: the
   spool shows `reconnecting ... after OSError` and reconcile shows `[Errno 65] No route to host`, while `curl` to
   the same server works. The operator allows it in System Settings > Privacy & Security > Local Network ("Python").
   The listener then logs `connected` and `caught_up` by itself.
8. **The plugins.** The repository's marketplace (`tools/postbox/plugin`) lists only `meshy`, and its settings file
   enables only `meshy`. The mod lives apart, in `tools/postbox/mod/meshy-mod`. So build the seat's own marketplace
   first:
   1. Copy `tools/postbox/plugin` (with `.claude-plugin/` and `meshy/`) to `<postbox>/meshychat-marketplace`, and copy
      `tools/postbox/mod/meshy-mod` to `<postbox>/meshychat-marketplace/meshy-mod`. In the copy's `.claude-plugin/plugin.json`, set
      `"version"` to the release (see "Updating the mod").
   2. In `<postbox>/meshychat-marketplace/.claude-plugin/marketplace.json`, add the mod to `plugins`:
      `{"name": "meshy-mod", "source": "./meshy-mod", "description": "MeshyChat band, status line and guards."}`.
   3. In `<postbox>/meshychat-marketplace/meshy/.mcp.json`, set `command` to the venv python and `args` to `[<postbox>/meshy_channel.py]`.
      The repository's copy names HQB's paths and is a template. The python must be one that has the packages of
      step 2: HQD's first copy named a Python 3.14 without them. Check it before the first launch with
      `<that python> -c "import psutil, cryptography, rfc8785, websockets"`, which must print nothing. Give the
      development route's `mcp-<seat>.json` (step 11) the same python.
   4. Write `<postbox>/meshychat-marketplace/channel-session-settings.json` so it enables both plugins, for channel sessions only:
      `{"enabledPlugins": {"meshy@meshychat-marketplace": true, "meshy-mod@meshychat-marketplace": true}}`.
      A seat that already loads the mod inline (`CLAUDE_CODE_PLUGIN_DIRS` names a `meshy-mod` folder; HQD does) enables
      only the channel here, `{"enabledPlugins": {"meshy@meshychat-marketplace": true}}`. Otherwise this setting adds
      a second copy of the mod beside the inline one.
   5. Then, once per user:

      ```
      claude plugin marketplace add <postbox>/meshychat-marketplace
      claude plugin install meshy@meshychat-marketplace --scope user
      claude plugin install meshy-mod@meshychat-marketplace --scope user
      claude plugin disable meshy@meshychat-marketplace --scope user
      claude plugin disable meshy-mod@meshychat-marketplace --scope user
      ```

      Both stay disabled at user scope. A disabled plugin's server never starts, so each channel launch enables them
      with `--settings`.
9. **The seat's own hooks**, if it has any that start a waiter (pxx's `meshychat-autowait.sh` and `meshychat-waiter.sh`):
   - Once `<postbox>/config.json` exists, they must never start a second listener.
   - At session start they make sure the daemon runs. macOS: `launchctl print gui/$UID/net.meshychat.postbox ||
     launchctl bootstrap ...`, then `launchctl kickstart`.
   - A channel session (`MESHY_IDENTITY` set) needs nothing more.
   - A session started without the channel runs the postbox's own `wait_next.py <identity> --consumer hook` as its
     fallback waiter.
   - **Presence (divergence 082, issue #139).** So that a seat that is compacting or stopped does not look available,
     the seat publishes its own state from these hooks, each with a ttl so a hook that never runs reads `unknown`
     rather than a stale state:
     - `PreCompact`: `python -m tools.meshy_node presence --state asleep --note compacting --ttl 1800`
     - `SessionStart` (startup, resume and compact) and `Stop`: `python -m tools.meshy_node presence --state active --ttl 21600`
     Each with the seat's usual `--base-url` and `--state-file`. A failed call must not block the hook: run it with a
     short timeout and ignore its exit code. Readers use `effective_state` from `GET /v1/peers/presence`, never `state`.
10. **The managed policy** (the operator's admin rights; one file per machine):

    ```
    {"channelsEnabled": true, "allowedChannelPlugins": [{"plugin": "meshy", "marketplace": "meshychat-marketplace"}]}
    ```

    - macOS: `sudo mkdir -p "/Library/Application Support/ClaudeCode"`, copy the file in, `chown root:wheel`,
      `chmod 644`. Proven on 2.1.289: the channel then opens as `plugin:meshy:meshy` with no warning.
    - Windows: an elevated prompt writes `C:\Program Files\ClaudeCode\managed-settings.json`, or the equivalent values go under `HKLM\SOFTWARE\Policies\ClaudeCode`. `C:\ProgramData\ClaudeCode` is silently ignored on this build.
      Give the operator ONE line to paste into an elevated PowerShell window. It creates the folder, copies a file
      the seat staged, and prints it back:

      ```
      New-Item -ItemType Directory -Force 'C:\Program Files\ClaudeCode' | Out-Null; Copy-Item '<staged>\managed-settings.json' 'C:\Program Files\ClaudeCode\managed-settings.json'; Get-Content 'C:\Program Files\ClaudeCode\managed-settings.json'
      ```

      Two separate lines failed on HQD (2 Nov 2026): the paste ran them together and `New-Item` refused `Copy-Item`
      as a positional argument, so nothing was written. Then read the file back from the seat before relaunching.
11. **The launcher.** It sets `MESHY_IDENTITY`, starts the daemon if it is down, and passes the session's own
    arguments (`-c`, `--resume <id>`) through. `--channels` takes a list, so another flag must follow it, never a prompt.
    - macOS (pxx's `~/.local/bin/claude-pxx`, zsh):

      ```
      export MESHY_IDENTITY=pxx
      if [ -f "/Library/Application Support/ClaudeCode/managed-settings.json" ]; then
        exec claude "$@" --channels plugin:meshy@meshychat-marketplace --settings "$HOME/.meshychat/postbox/meshychat-marketplace/channel-session-settings.json"
      fi
      exec claude "$@" --mcp-config "$HOME/.meshychat/postbox/mcp-pxx.json" --dangerously-load-development-channels server:meshy \
        --plugin-dir "$HOME/.meshychat/postbox/meshychat-marketplace/meshy-mod"
      ```

      Without the policy, the second line is the development route: press Enter at "WARNING: Loading development
      channels" at every start.
    - Windows (HQB's `resume-node.cmd`, a cmd batch saved with CRLF line ends: an LF copy failed to find a `goto`
      label):
      1. It looks the pane's tag up in `<postbox>\channel-seats.txt` (lines like `txxxx txxxx,txxxx-prod`).
      2. It sets `MESHY_IDENTITY` to the second column with cmd's `set`, then runs:

         ```
         claude --resume <session-id> --channels plugin:meshy@meshychat-marketplace --settings %USERPROFILE%\.meshychat\postbox\meshychat-marketplace\channel-session-settings.json --model <m> --dangerously-skip-permissions "<start prompt>"
         ```

      An unlisted tag runs the same line without the two channel flags.
    - Windows, a box with one seat (HQD's `C:\Users\user\claude-kxxx.cmd`, CRLF, 2 Nov 2026). No tag table: the
      identity is fixed, and it falls back to the development route while the policy file is missing:

      ```
      @echo off
      set MESHY_IDENTITY=kxxx-prod
      cd /d C:\_SampleProjects\Agentmeshy
      if exist "C:\Program Files\ClaudeCode\managed-settings.json" (
        claude --dangerously-skip-permissions %* --channels plugin:meshy@meshychat-marketplace --settings C:\Users\user\.meshychat\postbox\meshychat-marketplace\channel-session-settings.json
      ) else (
        echo No managed policy yet: the development route. Press Enter at the development-channels warning.
        claude --dangerously-skip-permissions %* --mcp-config C:\Users\user\.meshychat\postbox\mcp-kxxx.json --dangerously-load-development-channels server:meshy
      )
      ```

      Give the seat's permission mode on the command line. HQD's user settings say `"defaultMode": "auto"`, so the
      first fresh session started without the flag came up in auto mode instead of the seat's usual bypass mode.
12. **The card load hooks**, on a seat whose cards go to the server (that is the seat's switch and the operator's
    word for it). They run the transcript loader and `card write` for the seat, so its cards reach the server
    without anyone running them (`tools/transcript_cards/README.md`, "The load from a seat's hooks").
    - **The seat is the pane's `MESHY_IDENTITY`**: all its labels, as the channel (`meshy_channel.load_identities`)
      and the mod (`loadIdents`, which then acts for the enabled ones) take them. A pane without it is no seat's,
      and every hook in it does nothing at all. From it the worker takes:
      - the serving identity: the first label, in `MESHY_IDENTITY` order, whose entry in the postbox's
        `config.json` exists and is enabled (`MESHY_POSTBOX_CONFIG` names another file). On HQB a pane is
        `<seat>,<seat>-prod` and the bare `<seat>` is an old test-server identity, `"enabled": false` with no
        server of its own, so the serving identity is `<seat>-prod`;
      - the server and the state file: the serving entry's `base_url` (else the file's) and `state`;
      - the seat label: the serving entry's `"seat"` when it has one (e.g. `"seat": "memory-bxxxxx"`), else the
        FIRST label of the pane, the bare roster label existing card keys use (`card/txxxx/...`; `txxxx-prod` as a
        seat would start a second namespace);
      - the store, `~/.meshychat/cards/<seat>` (Windows: `%USERPROFILE%\.meshychat\cards\<seat>`;
        `MESHY_CARDS_ROOT` moves the root);
      - the loader's markers, the serving entry's `"markers"` (e.g. `["echo exxxx-shell-ok"]`).
      A pane none of whose labels has an enabled entry is not loaded, and its receipt says so.
      Before the hooks go on, the installer gives a serving entry `"seat"` when the seat's roster label differs
      from the pane's first label, and `"markers"` when the seat's sessions are found by a launcher marker.
      Neither is inferred. Measured on HQB at 47fe56e by exxxx-session (10 Oct), read only: none of the 17
      entries carried either. exxxx's pane (`MESHY_IDENTITY=exxxx-prod`, roster label `exxxx-session`) would
      give the loader `--seat=exxxx-prod` and no marker: it picked 0 of 26 transcripts, and the server refuses a
      seat that is not the stamped label (`apps/cards/validation.py`). On a copy of the config with
      `"seat": "exxxx-session", "markers": ["echo exxxx-shell-ok"]` on `exxxx-prod`, it picked 11. This read-only
      check lists each enabled entry and whether it has the two (sh or Git Bash; a missing `enabled` counts as on):
      `python -c 'import json,os; p=os.environ.get("MESHY_POSTBOX_CONFIG") or os.path.expanduser("~/.meshychat/postbox/config.json"); [print(i.get("name"), "seat:", i.get("seat", "-"), "markers:", "yes" if i.get("markers") else "-") for i in json.load(open(p, encoding="utf-8")).get("identities", []) if i.get("enabled", True) is not False]'`

      So one hook command serves every seat of a shared Claude Code profile. **HQB** runs txxxx, uxxx, vxxxxxxxxxx,
      dxxxxx and exxxx from one `settings.json` (`CLAUDE_CONFIG_DIR` unset): a command with a fixed `--seat` or
      `--state-file` there would load every seat's sessions as one seat's. `--seat`, `--store`, `--base-url`,
      `--state-file` and `--marker` still override, for a box with one seat; a pane with no identity still does
      nothing.
    - Seed each seat's store once by hand before turning the hooks on: a store at the default path starts empty, so
      the first hook pass reads every transcript under the 120 s stage limit, and a large folder may time out each
      time before its cursor is saved (txxxx). The load by hand gives each stage 1,800 s:
    - First load once by hand, through the hook's own script, in a pane with the seat's identity: the hook's
      command below with `--run` in place of `--event ...` and `--project-dir <the seat's Claude Code project
      folder>` given. It takes the store's lock,
      gives each stage 1,800 s (the first read of a long history, and the first push of many cards, take longer
      than a hook's 120 s), and prints its receipt. After a loader upgrade, the same with `--rebuild`, once for each
      project folder (each has its own loader store, and a rebuild touches no other).
    - Never run `transcript_cards.py` directly on this store, or on a project's store in it, while the hooks are
      on: it takes no lock, and its writes interleave with a hook's load. Every load by hand goes through
      `load_hook.py --run`.
    - The store (above) is a new, empty folder, outside any git tree and this user's alone. Each project folder gets its own
      loader store in it (`projects/<key>/`); a folder that already holds a loader's `cards.jsonl` at its top is
      refused. On macOS the worker makes everything in it 0700 and refuses a store, `pending/`, `results/`,
      `projects/` or a project's store that is a link, another user's, or writable by group or others. On Windows it is trusted to the user profile's ACL, so keep it under `%USERPROFILE%`.
    - Windows: each stage runs in a kill-on-close job object the worker holds, so a killed worker takes its stages
      with it. Measured on Windows: `tests/test_transcript_cards_load_hook.py` runs on HQB and in kxxx's HQD gate.
    - Python first: the client needs Python 3.12 or newer (`reprlib.Repr` keyword arguments; the project pins 3.13),
      and any `tools.meshy_node` command fails to import on older ones. A seat whose mesh tools run on an older
      interpreter (the HZ's 3.9) keeps that client as it is and points these hooks at a SEPARATE hook client run by a
      3.12 or newer interpreter that has the client's packages (oxxxxxx, txxxx).
    - Add four hooks to the seat's Claude Code user settings, beside the hooks it already has. Each runs the same
      command with its own `--event`. SessionStart also prints the decision-cards briefing, so give that hook
      a 15 s timeout (up to 2 s for stdin, one 9 s deadline for resolution and the briefing child, and room for
      interpreter startup). If the hook's stdout reader stops, a blocked write cannot be interrupted from inside;
      the settings line's own 15 s hook timeout bounds this case because Claude Code ends the hook:

      ```
      {"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "timeout": 10, "command": "<python> <client>/tools/transcript_cards/load_hook.py --event UserPromptSubmit"}]}],
        "PreCompact":       [{"hooks": [{"type": "command", "timeout": 10, "command": "<the same, --event PreCompact>"}]}],
        "SessionEnd":       [{"hooks": [{"type": "command", "timeout": 10, "command": "<the same, --event SessionEnd>"}]}],
        "SessionStart":     [{"hooks": [{"type": "command", "timeout": 15, "command": "<python> <client>/tools/transcript_cards/load_hook.py --event SessionStart"}]}]
      }}
      ```

      - `<python>` is the venv python (Windows: the `python.exe` that runs the client), `<client>` the client
        checkout. Windows: write every path in the `C:/...` form.
      - The command names no seat, store, server or state file: the pane's identity does (above).
      - Without `--project-dir`, each load reads the Claude Code project folder of the session that fired the
        hook, each folder into its own loader store, so sessions started in several folders are all read.
        `--project-dir <folder>` reads that folder only.
      - The hook also names the session's transcript to the loader as `--seat-file`, once the loader has that
        flag (txxxx's change after #208): a session continued after a compaction writes a new transcript with no
        seat marker in it, which the loader would otherwise never read.
    - What each does. Each hook starts a detached worker; the hook itself reads and writes nothing in the store,
      so a slow disk there never holds a prompt. The prompt's worker loads its project at most once in 15 minutes
      after a good load of it, and once in 5 after any other, and the hook prints nothing. PreCompact and SessionEnd always
      load (waiting in the store's queue while another load runs) and print one `card load: ...` line.
      SessionStart starts the load, then prints the decision-cards briefing: `card recent` for the seat, server and
      state file the pane's identity resolves to, with `<store>/last-result.json`. It is bounded: one 9 s deadline
      covers the identity lookup and the child (which runs `card recent --deadline 5`: its interactive default
      of 2 s timed out on HQB), which is killed past it or at the cap (on POSIX its whole process
      group; on Windows the child itself, and `card recent` starts no process of its own). The child also ends
      itself within its own 2 s deadline. At most 64 KiB is printed, as UTF-8, and never the child's stderr. A
      failure prints one `decision cards: not available (...)` line naming what failed (no identity, a timeout,
      an exit code, no output). A failure line from `card recent` itself is cut to its exception type plus "run
      `card recent` by hand to see why": its message can carry a server's answer and is never printed. A worker
      that leaves requests queued (its three passes
      spent, or held up) starts one successor to take them, so a session that ended is still loaded. Every hook exits 0.
    - One SessionStart settings line above serves every seat in a shared profile; it names no seat. Add
      `--no-brief` if the seat already runs `card recent` from its own settings line: only the load starts and
      nothing is printed. Add `--brief-only` if the seat has not turned on card loading yet: only the briefing
      runs and no load worker starts. Both flags together mean no load and no briefing: nothing is printed and
      the hook exits 0. They affect hook-mode SessionStart only; `--run` and the other events ignore both flags.
    - Check: after a prompt, `<store>/last-result.json` has a new `time` and `"status": "ok"`. To load now and read
      the result, run the load by hand above.
13. **The transcript context upload**, on a seat whose transcripts go to `shared/transcript-context` (the seat's
    switch and the operator's word for it, as for the cards). The postbox's context worker
    (`tools/postbox/context_worker.py`) runs it for each identity whose `config.json` entry has a `"context"` block
    with `"enabled": true`. The keys a seat sets:
    - `seat` (the stamped roster label, e.g. `"memory-bxxxxx"`), `dialect` (`claude`, `codex`, `kimi` or
      `antigravity`), `app_root` (the client checkout the upload runs from), `namespace`
      (`shared/transcript-context`);
    - `allowed_roots`: the folders an enrolled file may live under. Enrolment refuses anything outside them;
    - `pins`: transcript files enrolled by name. A pin stops at the seat's next session, since every new session
      writes a new file;
    - `discover`: specs the worker walks every 5 minutes for new files, oldest first, at most 8 per round:
      `{"root": <folder>, "pattern": "*.jsonl", "recursive": <bool>, "max_age_h": 48, "exclude_globs": [...]}`.
      A root that holds only this seat's sessions needs nothing more.
    - **A root shared by more than one seat** (on HQB, `C--Projects-agentmeshy` holds more than one seat's Claude
      sessions): add `"seat_marker": true` to the spec. A file then qualifies only if its first 1 MiB holds a
      meshy `channel_open` record naming one of this seat's identities, that is, an identity whose entry names this
      seat in `context.seat` (or a top-level `seat`), or the entry's own name. `"seat_marker": ["<identity>", ...]`
      names them outright. Claude transcripts only: on another dialect it is a config error
      (`seat_marker_needs_claude`), since Codex, Kimi and Antigravity transcripts may carry no channel record. A
      session started without the channel has no marker and is never enrolled this way; pin it instead.
    - **Update the postbox first.** A postbox older than #486 ignores `seat_marker`, so on a shared root it would
      enroll every recent `.jsonl` there under this seat. Add the key only after the installed copy has it
      (`grep -c seat_marker <postbox>/context_worker.py`).

    Two things show in the worker's log and the status. An upload pass visits the newest-written file first, and
    one line names the files it visited and their batches each time that order or progress changes. And when
    every file a seat enrolled under a discover root has been idle 6 h while another file there that nobody
    enrolled was written within those 6 h, the status gets `context_stall` and the log one line a day naming both:
    usually the seat's new session, not yet enrolled.
14. **How the seat looks things up (issue #269).** A seat on the Claude channel needs nothing: the channel's standing
    instructions carry a LOOKUP section, built from the identity's `client`, `python`, `base_url` and `state` in
    `config.json`, that names which route answers which question (`card search` for what was ruled, `search` for the
    mail, `thread` and `summarize` for a conversation, `context` and `neighbours` around a message, `owed` and `brief`)
    with the seat's own command line. It lists only the routes that identity's client registers, and stays in the
    system prompt through compaction. A seat without the channel (a doorbell seat, a Codex seat) gets the same text
    from `<python> -m tools.meshy_node lookup-help --base-url <server> --state-file <state file>`, run in its client
    checkout; paste it into the seat's AGENTS.md or print it from a hook. Nothing calls the server.

## Checks

- **The daemon** (`<venv python> <postbox>/postbox.py`). Health needs all three of these, for EVERY enabled identity in
  `config.json`:
  1. In `--status`, the identity is present with `"listener": "running"`. Anything else is unhealthy: `blocked`
     (another reader holds the state file), `exited`, `terminal, retrying`, `no state file`, `skipped`, or the identity
     missing.
  2. `--liveness` prints that identity's own line, `<name>: listener running, pid N alive`, and exits 0.
     - The summary line alone is not enough: `--liveness` checks only listeners recorded as `running`, so a `blocked`
       or `exited` one still gives "0 not alive".
     - Exit 2, or an `UNKNOWN` row, means the check could not run.
  3. In `--status`, `last_poll_ok_at` is recent (within about 5 minutes): the server and the credentials answer.

  Item 3 alone proves nothing about the listener. The reconcile thread kept it fresh for five hours on 2026-10-01 while
  two listeners were dead (README.md), and reconcile is only a capped backstop for live delivery. The mod's "live" also
  comes from `status.json`; it does not check the process.
- **The channel:** the session's first event is `[CHANNEL] MeshyChat channel open for <identity>`. With the policy its
  source is `plugin:meshy:meshy`, without it `meshy`.
- **The mod:** `/plugin` may not list it (it did not on pxx's Mac; not checked on HQB). Load tests:
  - From 0.3.0, the status line `MeshyChat ● live ...` appears. That is enough for a seat with one identity.
  - On a seat whose postbox has another identity, run a probe that CANNOT send even if the mod is missing. Run it
    through Claude's Bash or PowerShell tool, not in an operator shell, which the mod never sees. It names a send
    but omits `--to`, `--thread`, `--type` and the body:
    `<python> -m tools.meshy_node send --state-file <the OTHER identity's state file>`.
    - Success is the mod's own refusal, "meshy-mod held this send: it signs with ... the state file of <other>".
    - The client's usage error means the mod did not hold it, so the guard is not shown.
    - 0.1.0 holds any state file that is not the seat's own. 0.2.0 to 0.4.0 judge only drive-letter paths, so on
      macOS or Linux they never hold one. From 0.4.0 the rule is another identity of this postbox, and 0.4.1 judges
      POSIX paths too.
  - `claude plugin validate .` and `claude plugin test .` in the mod folder must pass on the seat's own platform.
- **Reads:** the mod counts a message as read only when a shell command names `tools.meshy_node read ... --key msg/...`,
  so read with the command the notice prints. A seat wrapper such as `mesh.sh read` does not clear the band. Before
  0.4.1, a read that ran before the mod's 10 s poll had made the row was lost, and the row stayed unread. 0.4.1
  keeps it read. Do not pipe the read into anything (`read ... | python ...`): the read then no longer decides the command's exit,
  and its output is not the client's own answer, so it does not count. Redirecting it (`> file`) is fine (pxx, 4 October).
  From 0.4.2 the mod says so: Claude gets one line naming the key that stays unread, why, and to run the read on its own.
- **Replies through the client too:** `tools.meshy_node send --thread <t> --in-reply-to <key>` (with `--requires-ack`
  on an ask). The mod sees a send only when the command names `tools.meshy_node send`, and otherwise leaves answered
  asks in its owed count. Give literal absolute paths: a `~` or variable `--state-file` is not attributed.
- **Seat wrappers refuse in a channel session**, so habit cannot route around the mod. pxx's `mesh.sh read`, `reply`
  and `send` exit 3 when `MESHY_IDENTITY` names the seat, and print the client form to run instead. `inbox` and
  `status` still work.
- **The status line** (`MeshyChat ● live  ▁▁▂ 1h  N unread`) is the mod's own, from 0.3.0. It needs no setting: it
  reads the postbox's `status.json` beside `config.json` and shows beside any `statusLine` script the seat has.

## Seat usage on the turn report (opt in)

The postbox can send the seat's model, context use and rate-limit windows with each turn report. `fleet --work`
shows them as `c45 5h12 7d38`, and the server keeps them in `mesh_presence.usage` (#525, migration 056). It's off
until the identity's entry in the postbox config names a source:

    "usage": {"statusline_file": "<path to this seat's saved status-line input>"}

- The seat's `statusLine` script saves its stdin to that file, as HQD's does (`statusline.ps1`, line 3). Use **one
  file per seat**, never one shared by several seats on a box (memory-bxxxxx): whichever session rendered
  last would overwrite it, and every seat would report that one's numbers.
- Only `as_of` (the file's mtime, so an idle seat goes stale on time), `model.id`, `context_window.used_percentage`
  and `rate_limits.five_hour` / `seven_day` (used percentage and reset time) leave the box. Paths, the session id,
  cost and everything else in the file stay.
- A missing file, or one more than 23 hours old, sends no usage, and the server keeps the last snapshot.
  A file that doesn't parse sets `usage_error` in `status.json`, and the turn report still goes.
- A change of 5 points or more, or a new model, sends at the next 30 s check. Otherwise usage rides on the 120 s
  refresh.
- Codex seats send no usage yet (account/rateLimits/read is the follow-up).
- The identity's client checkout must be at or after #525 (`turn --usage-file`, 5 Nov 2026). With an older
  one, the report carrying usage fails, the same turn pair goes without it, and usage backs off (5 minutes,
  doubling to an hour; `usage_error: usage_report_failed` in `status.json`).

## Held-mail Stop hook

From 0.15.0 the mod declares a command Stop hook, `hooks/held_stop.cjs`, in its `hooks.json` (the mod README,
"The held-mail Stop guard as a command hook"). To check that it runs, end one turn while the seat holds cc mail.
The transcript's `stop_hook_summary` then lists `held_stop.cjs`. If it is missing (the engine did not register a
declared hook for this mod), add the same command to the seat's own `~/.claude/settings.json`, under
`hooks.Stop`. Use the mod's folder path, because outside the plugin `${CLAUDE_PLUGIN_ROOT}` is not set:

    {"hooks": {"Stop": [{"hooks": [{"type": "command", "timeout": 15,
      "command": "node \"<client checkout>/tools/postbox/mod/meshy-mod/hooks/held_stop.cjs\""}]}]}}

Back the settings file up first. The hook needs `node` on the PATH that hooks run with. It reads only the postbox
config, the bank and the read ledgers, and writes only `~/.meshychat/held-stop/`.

## Updating the mod

Since the first repository bump (#228; memory-bxxxxx's ruling, 10 Oct 2026: every behaviour change to the mod bumps the version in
the repository), the `plugin.json` in the repository keeps `"version": "0.16.2"`; before it, the repository said `0.1.0` and HQB bumped
the version only in its installed copy. Once main's `plugin.json` carries a version above the installed one, take main's file as it is (`docs/client-update.md`, step 3). Copy
the release's `mod/meshy-mod` folder over `<postbox>/meshychat-marketplace/meshy-mod` and set `"version"` in the copy's
`.claude-plugin/plugin.json` to the release (e.g. `0.4.1`). Then run `claude plugin marketplace update meshychat-marketplace` and
`claude plugin update meshy-mod@meshychat-marketplace`, and relaunch. Without the bump, `claude plugin update` sees nothing new.
Before relying on a release on a new platform, run `claude plugin test .` in the folder on that platform.
Whether a box's copies are behind main is read from their files, never from the version: `docs/client-update.md`, "The mod", compares
both copies with a commit and is the update step after a deploy.

## Handing over to a fresh session

A long session's transcript grows until resumes and compactions slow down and a cut-over gets hard: memory-bxxxxx's
reached 425 MB and kxxx's 446 MB before anyone noticed (2026-11-02). Two parts help, and neither replaces the handover
file itself.

- **The size note** (meshy-mod 0.12.0; 0.16.0 lowered it, audit finding 4): once this session's transcript passes
  120 MB, the next prompt or wake carries one line naming its size and path, and one more per further 10 MB. From
  150 MB every prompt carries a note to start no new work and hand over now; it never blocks a prompt. It needs no
  setting.
- **The resume brief**: `python -m tools.meshy_node resume-brief --base-url <server> --state-file <state> --seat <seat>
  --budget <chars>` prints, in this order, the NEXT ACTION line of `--handover-file` (first, so no budget cuts it), what
  the seat owes (oldest first), what it waits on, its pull requests (by its `<seat>/` branch prefix and by the PR
  numbers in its owed asks' thread ids, with gh's live state; the server's waiting rows carry no thread id, so they
  name no thread and find no PR; an owed ask whose PRs are all MERGED reads "likely done", and
  still counts), and its own recent work (its own transcript-context, the last 6 hours, never without the seat and a
  since). Every line names its source. A source that fails or answers nothing says so. The handover line gives the
  file's last-modified age, which is not when the handover was written. `--gate-file hqb-gate.json` adds the gate's
  state, or "unavailable" when it is missing or over 5 minutes old. Give it to a fresh session in its SessionStart hook,
  with `--hook`, so an error is one line and exit 0. With `--hook` the run also keeps inside the hook's 30 s: no source
  call starts after 18 s, each gh call gets only the time left, and at 25 s it prints one "unavailable" line and exits 0:

      "SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "timeout": 30, "command": "<python> -m tools.meshy_node resume-brief --base-url <server> --state-file <state> --seat <seat> --budget 800 --hook --handover-file <handover>"}]},
        {"matcher": "compact", "hooks": [{"type": "command", "timeout": 30, "command": "<python> -m tools.meshy_node resume-brief --base-url <server> --state-file <state> --seat <seat> --budget 6000 --hook"}]}
      ]

  A seat whose panes come back with `--resume` (HQB after a reboot relaunches every pane that way) gets neither entry,
  since `resume` is its own matcher. Its transcript still holds the old session, but the owed list, the pull requests
  and the gate have moved on, so such a seat can add a third entry, the startup one with `"matcher": "resume"`.

  Run it from the client checkout (the `cd` the other hooks use). No message bodies are shown: open a key to see what
  was asked.
- **Context upload after a cut-over**: a seat whose `context.discover` names its own sessions folder (HQD) finds the new
  session's file by itself, and so does a `seat_marker` spec on a folder shared by several seats (step 13). A seat that
  pins one file uploads nothing from the new session until the pin names it, and nothing says so: re-pin it in the
  same step as the cut-over, or move it to a `seat_marker` spec.
- **Anything a seat runs across sessions** (gate tools, scripts, notes) belongs under `~/.meshychat/<seat>/`, not in a
  session's scratchpad: the next session does not get the old one's.

## Moving a seat off the doorbell

A doorbell seat gets mail from its own hooks: a background waiter that exits when mail arrives and wakes the
session, plus hooks that re-arm it each turn and say "NOT ARMED" when it is down. HQD's kxxx seat moved from the
doorbell to the channel on 2 Nov 2026. In order:

1. Install the channel (steps 8, 10 and 11) and start a FRESH session through the launcher. Leave the doorbell on.
2. Check the `[CHANNEL] MeshyChat channel open for <identity>` event. It shows the channel started, not that mail
   arrives on it.
3. Wait for the proof: a peer's message arriving as a `<channel ... kind="message">` event. On HQD the doorbell
   rang for the same message as well. Both paths read the same postbox, so running both together for a while
   does no harm.
4. With the operator's yes, remove the doorbell hooks from the settings file that holds them (HQD: the project's
   `.claude/settings.local.json`; SessionStart, UserPromptSubmit and Stop). Back the file up first, check that
   every hook you remove belongs to the doorbell, and parse the file again afterwards.
5. Stop the waiter that is still running, and check in the process list that no copy of it is left.
6. The hooks are read at session start, so the change takes effect at the next launch. A session started without
   the launcher (a plain `claude -r`) now has no mail at all: always relaunch through the launcher ("Traps").

## Traps (HQB and pxx)

- Flags that take a list swallow what follows: `--channels`, and `--disallowedTools` the same way (it swallowed a
  prompt on HQB). Put the prompt first, or after a flag that takes exactly one value.
- A plain `claude -r` after `/exit` drops the channel. Always relaunch through the launcher. Both boxes now catch it:
  - HQB: a SessionStart hook (`~/.claude/hooks/meshy-channel-guard.py`, startup and resume) speaks only when
    `MESHY_IDENTITY` is set, the session is interactive, and the claude that started it requested neither channel
    route. It then shows the operator a relaunch line in a safe flag order and tells Claude to arm
    `wait_next.py <ids> --consumer cc`.
  - pxx: the launcher sets `MESHY_IDENTITY` only inside its own process, so a plain `claude -r` has none. pxx's hooks
    then run `wait_next.py pxx --consumer hook` as the fallback, and the SessionStart note says to relaunch with
    `claude-pxx`.
- `--plugin-dir` plugins load as `name@inline`, and the channel refuses them. So the channel plugin is installed (and
  disabled) at user scope and enabled per launch by `--settings`. A disabled plugin's server does not start even with
  `--channels`. (pxx's development route loads only the mod through `--plugin-dir`; the channel there comes from
  `--mcp-config`.)
- One reader per state file: a second listener reports `listener: blocked`.
- Health is, per identity, `listener: running` in `--status`, its own ALIVE line in `--liveness`, and a recent `last_poll_ok_at`. No one of them alone is enough (see Checks).
- Windows:
  - The commands a session pastes are PowerShell (`Set-Location ...; & python ...`).
  - Git bash `/tmp` paths are invisible to Windows python, so pass `C:/` paths.
  - The console is cp1252: piping read output through python crashed on a character like U+25CF. Fixed on HQB by
    `"PYTHONIOENCODING": "utf-8"` in the `env` block of the user settings, so every process a session starts prints
    UTF-8. A Mac does not need it.
  - Codex identities need `"codex"` (the path to `codex.exe`; the npm shim cannot be spawned without a shell) and
    `"target"` (the pane's session UUID).
  - A Codex identity's rollout is read in full the first time the postbox sees it, then only what was appended
    (CodexTurn). With a very large rollout (the largest on HQB was 613 MB on 23 Oct 2026) that first read holds
    that identity's Codex delivery thread once, after a start or when the thread moves to a new rollout file. It isn't a hang:
    wait for it to finish before restarting anything.
  - Defender, ExecutionPolicy and path case: none seen. Nothing runs a `.ps1`.
- macOS:
  - The Local Network permission (step 7).
  - `/usr/bin/python3` is 3.9, too old.
  - `timeout` is not installed.
  - The mod needs 0.4.1 or later. Every release from 0.2.0 to 0.4.0 compared only drive-letter paths, so on a Mac
    its identity guard never holds a send, and a read is never attributed. 0.1.0 has no path test at all, so it holds
    any send whose state file is not the seat's own, but it has no status line (that came in 0.3.0).

## Known platform differences

| | Windows | macOS |
|---|---|---|
| state file paths | drive form `C:/...`, compared case-insensitively | absolute POSIX `/...`, case kept (meshy-mod 0.4.1) |
| home | `USERPROFILE` | `HOME` (a Mac may inherit a USERPROFILE; 0.4.1 decides the platform by OS or by a drive-letter USERPROFILE) |
| listener lifetime | a kill-on-close job object | `postbox_child.py`, which kills its listener within about a second of the daemon going away |
| network | nothing to allow (HQB: no Defender or firewall prompt seen) | the Local Network permission for Python, once (step 7) |
| mod tests | fixture paths in drive form | the engine resolves drive-form fixture paths against the cwd, so mocks match with `endsWith` and unanchored patterns; POSIX seats are tested with `posix: true` (0.4.2: 71 tests pass on pxx's Mac) |
