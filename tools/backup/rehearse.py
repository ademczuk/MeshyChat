"""Restore rehearsal: restore a backup into a NEW, throwaway cluster that has only the roles it
is given, and verify it against its manifest (E02, operator decision 2026-09-25).

    python -m tools.backup rehearse --manifest DIR/NAME.manifest.json \
        --role meshychat_app --role OWNER [--pg-bin DIR] [--keep-cluster]

pg_dump does not carry roles, and pg_restore --exit-on-error stops on the first object whose
owner or grantee role is missing (the review of E02). A restore onto a new host therefore
needs the roles first; this command proves a backup restores on a cluster that has exactly
the roles named, and nothing else of the source's:

  1. initdb a scratch cluster in a new temporary directory (superuser `rehearsal_admin`,
     trust authentication, listening on 127.0.0.1 only, a free port);
  2. CREATE ROLE ... NOLOGIN for every --role (meshychat_app and the role that owns the tables);
  3. restore with `python -m tools.backup restore` semantics: the dump's sha256 first, an
     empty target, `pg_restore --exit-on-error --single-transaction`, then every table's count
     and every ledger compared with the manifest in both directions;
  4. stop the cluster and delete it (unless --keep-cluster).

Exit codes as `restore`: 0 verified; 2 the backup files are not what the manifest says;
3 tools missing, or the scratch directory cannot be opened to this user (below); 4 initdb,
the server or pg_restore failed (a missing role lands here, named in the error); 5 restored
and the counts or ledgers disagree. It needs a PostgreSQL installation of the server's major
or newer (initdb, pg_ctl, postgres and its share/ directory), from --pg-bin, MESHY_PG_BIN or
PATH.

WINDOWS, ELEVATED (E17). Since CPython 3.13, tempfile.mkdtemp's 0o700 is an ACL on Windows:
SYSTEM, Administrators and OWNER RIGHTS. From an elevated shell whose new objects are owned
by Administrators, OWNER RIGHTS is Administrators too, and initdb (and the server pg_ctl
starts) runs with the Administrators group removed from its token, so the directory admitted
nothing it held: initdb failed with `could not create directory ...: File exists`. The
scratch directory is therefore granted to this process's USER SID, which those restricted
tokens keep, before initdb runs; the grant adds no one the directory did not already admit.
When the grant cannot be made the rehearsal refuses (exit 3) before initdb, naming the fix:
run it from a non-elevated shell.
"""
from __future__ import annotations

import csv
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg
from psycopg import sql

from apps.core import backup_manifest as bm
from tools.backup.pgbackup import Refused, _tool, restore

ADMIN = "rehearsal_admin"
DATABASE = "meshy_rehearsal"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(cmd: list[str], log: Path, timeout: int = 180) -> int:
    # A file, never a pipe: pg_ctl start leaves the server running with inherited handles,
    # and a pipe held by a detached grandchild never reaches EOF.
    with open(log, "ab") as f:
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout).returncode


def _system32(exe: str) -> str:
    # By absolute path: a PATH with Git's usr/bin first finds a POSIX `whoami` there.
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / exe)


def _grant_to_this_user(work: Path) -> None:
    """Windows: give this process's user SID full control of `work`, inherited by all it
    will hold, so initdb and the server keep access with Administrators dropped from their
    tokens (E17, module docstring). Refused(3) when the SID or the grant cannot be had."""
    if os.name != "nt":
        return

    fix = (f"the scratch directory {work} cannot be opened to this user, and initdb, which "
           "drops the Administrators group from its token, would fail in it. Run the "
           "rehearsal from a non-elevated shell")

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        # A tool that hangs or cannot be started is the same refusal as one that exits
        # non-zero, not a traceback (review LOW-2 of fin/w3-ops).
        tool = Path(cmd[0]).name
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise Refused(3, f"{fix} ({tool} did not answer in {exc.timeout:g} s)")
        except OSError as exc:
            raise Refused(3, f"{fix} ({tool} could not be run: {exc})")

    who = run([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"])
    rows = list(csv.reader(who.stdout.splitlines())) if who.returncode == 0 else []
    sid = rows[0][-1].strip() if rows and rows[0] else ""
    if not sid.startswith("S-1-"):
        raise Refused(3, f"{fix} (whoami /user exited {who.returncode}: "
                         f"{(who.stderr or who.stdout).strip()[:500]})")
    got = run([_system32("icacls.exe"), str(work), "/grant", f"*{sid}:(OI)(CI)F"])
    if got.returncode != 0:
        raise Refused(3, f"{fix} (icacls /grant {sid} exited {got.returncode}: "
                         f"{(got.stderr or got.stdout).strip()[:500]})")


def rehearse(manifest_path: Path, roles: list[str], pg_bin: str | None,
             keep_cluster: bool = False) -> dict:
    manifest = bm.load(manifest_path)                     # BackupInvalid before a cluster is made
    initdb, pg_ctl = _tool("initdb", pg_bin), _tool("pg_ctl", pg_bin)
    bin_dir = str(Path(pg_ctl).parent)         # restore's pg_restore from the same install
    work = Path(tempfile.mkdtemp(prefix="meshy-rehearsal-"))
    data, log = work / "data", work / "rehearsal.log"
    started = False
    try:
        _grant_to_this_user(work)
        if _run([initdb, "-D", str(data), "-U", ADMIN, "--auth=trust", "--encoding=UTF8",
                 "--no-instructions"], log) != 0:
            raise Refused(4, f"initdb failed: {log.read_text(errors='replace')[-2000:]}")
        port = _free_port()
        if _run([pg_ctl, "-D", str(data), "-l", str(work / "server.log"), "-w", "-t", "120",
                 "-o", f"-p {port} -c listen_addresses=127.0.0.1", "start"], log) != 0:
            raise Refused(4, "the scratch cluster did not start: "
                             f"{(work / 'server.log').read_text(errors='replace')[-2000:]}")
        started = True
        base = f"postgresql://{ADMIN}@127.0.0.1:{port}"
        with psycopg.connect(f"{base}/postgres", autocommit=True) as c:
            for role in roles:
                c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
        result = restore(manifest_path, f"{base}/{DATABASE}", bin_dir)
        with psycopg.connect(f"{base}/{DATABASE}") as c:
            result["scratch_system_identifier"] = str(bm.identity(c)["system_identifier"])
        result["source_system_identifier"] = str(manifest.get("system_identifier", ""))
        result.update({"rehearsal": True, "roles_created": list(roles),
                       "cluster": str(data) if keep_cluster else None})
        return result
    finally:
        if started:
            _run([pg_ctl, "-D", str(data), "-m", "fast", "-w", "-t", "120", "stop"], log)
        if not keep_cluster:
            shutil.rmtree(work, ignore_errors=True)
