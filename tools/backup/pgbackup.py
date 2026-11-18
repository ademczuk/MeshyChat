"""The backup, verify and restore commands. See tools/backup/__init__.py for usage.

Exit codes: 0 done and verified; 2 the backup files are not what the manifest says (or no
manifest); 3 refused before touching the target (not empty, tools missing or too old);
4 pg_dump or pg_restore failed; 5 restored, and the restored database disagrees with the
manifest. Every exit prints one JSON object as its last stdout line. `serve` refuses to start
with 3 (a wildcard --bind, a --dir that is not a directory) and exits 0 on Ctrl+C
(tools/backup/serve.py).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import IsolationLevel
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from apps.core import backup_manifest as bm


class Refused(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _tool(name: str, pg_bin: str | None) -> str:
    base = pg_bin or os.environ.get("MESHY_PG_BIN")
    if base:
        for cand in (Path(base) / name, Path(base) / f"{name}.exe"):
            if cand.is_file():
                return str(cand)
        raise Refused(3, f"{name} not found in {base}")
    found = shutil.which(name)
    if not found:
        raise Refused(3, f"{name} not found on PATH; pass --pg-bin DIR or set MESHY_PG_BIN")
    return found


def _major(version_text: str) -> int:
    m = re.search(r"(\d+)(?:\.\d+)?", version_text)
    if not m:
        raise Refused(3, f"cannot read a version from {version_text!r}")
    return int(m.group(1))


def _check_tool_version(tool: str, conn) -> str:
    out = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=30)
    text = (out.stdout or out.stderr).strip()
    server = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000
    if _major(text.rsplit(" ", 1)[-1]) < server:
        raise Refused(3, f"{text} is older than the server (major {server}); a dump from an "
                         "older pg_dump is not guaranteed to restore")
    return text


def _split_dsn(dsn: str) -> tuple[str, dict[str, str]]:
    """(conninfo without the password, environment carrying it). A password on a command
    line is readable by every process on the host; PGPASSWORD is not."""
    params = conninfo_to_dict(dsn)
    env = dict(os.environ)
    password = params.pop("password", None)
    if password is not None:
        env["PGPASSWORD"] = str(password)
    return make_conninfo(**params), env


def _snapshot_conn(dsn: str):
    conn = psycopg.connect(dsn)
    conn.isolation_level = IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    return conn


def backup(dsn: str, out: Path, copy_to: list[Path], pg_bin: str | None) -> dict:
    for target in copy_to:
        if target.resolve() == out.resolve():
            raise Refused(3, f"--copy-to {target} is the --out directory; a copy there is not a "
                             "second copy")
    pg_dump = _tool("pg_dump", pg_bin)
    out.mkdir(parents=True, exist_ok=True)
    conninfo, env = _split_dsn(dsn)
    with _snapshot_conn(dsn) as conn:
        dump_version = _check_tool_version(pg_dump, conn)
        # One snapshot for both halves: pg_dump reads the exported snapshot while this
        # transaction holds it open, and the counts below are read in the same transaction,
        # so they describe exactly the rows in the dump, not the rows a moment later.
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        # The backup's time is the snapshot's: the start of this transaction on the server's
        # clock, which is before the snapshot was taken. The rollback gate measures the age
        # on the same clock. Stamped after pg_dump finished, a long dump would understate
        # how many rows a restore loses.
        created = conn.execute("SELECT now()").fetchone()[0].astimezone(dt.timezone.utc)
        desc = bm.describe(conn)
        stamp = created.strftime("%Y%m%dT%H%M%SZ")
        base = f"{desc['database']}-{stamp}"
        dump_path = out / f"{base}.dump"
        partial = out / f"{base}.dump.partial"
        proc = subprocess.run(
            [pg_dump, "--format=custom", f"--snapshot={snapshot}", "--no-password",
             f"--file={partial}", f"--dbname={conninfo}"],
            env=env, capture_output=True, text=True)
        conn.rollback()
    if proc.returncode != 0:
        partial.unlink(missing_ok=True)
        raise Refused(4, f"pg_dump exited {proc.returncode}: {proc.stderr.strip()}")
    partial.replace(dump_path)
    manifest = {
        "format": bm.FORMAT,
        "created_at": created.isoformat(),
        "database": desc["database"],
        "database_oid": desc["database_oid"],
        "system_identifier": desc["system_identifier"],
        "server_version": desc["server_version"],
        "pg_dump_version": dump_version,
        "snapshot": snapshot,
        "dump_file": dump_path.name,
        "dump_bytes": dump_path.stat().st_size,
        "dump_sha256": bm.sha256_file(dump_path),
        "tables": desc["tables"],
        "ledgers": desc["ledgers"],
    }
    manifest_path = out / f"{base}.manifest.json"
    tmp = out / f"{base}.manifest.json.partial"
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(manifest_path)
    bm.load(manifest_path)                     # re-read what was written, hash included
    copies = []
    for target in copy_to:
        target.mkdir(parents=True, exist_ok=True)
        # The dump first and the manifest last: a manifest present at the copy means the
        # dump it names was already written there.
        shutil.copyfile(dump_path, target / dump_path.name)
        shutil.copyfile(manifest_path, target / manifest_path.name)
        bm.load(target / manifest_path.name)   # re-hash AT the copy
        copies.append(str(target / manifest_path.name))
    return {"ok": True, "manifest": str(manifest_path), "dump": str(dump_path),
            "dump_bytes": manifest["dump_bytes"], "tables": len(manifest["tables"]),
            "rows": sum(manifest["tables"].values()), "copies": copies}


def _is_empty(conn) -> list[str]:
    return [f"{s}.{t}" for s, t in conn.execute(bm._TABLES).fetchall()]


def restore(manifest_path: Path, dsn: str, pg_bin: str | None) -> dict:
    manifest = bm.load(manifest_path)          # BackupInvalid before anything else happens
    pg_restore = _tool("pg_restore", pg_bin)
    with psycopg.connect(dsn) as conn:
        _check_tool_version(pg_restore, conn)
        present = _is_empty(conn)
        if present:
            raise Refused(3, f"the target database is not empty ({len(present)} tables, e.g. "
                             f"{present[:3]}); restore only into a database created for it")
    conninfo, env = _split_dsn(dsn)
    dump = Path(manifest_path).parent / manifest["dump_file"]
    proc = subprocess.run(
        [pg_restore, "--exit-on-error", "--single-transaction", "--no-password",
         f"--dbname={conninfo}", str(dump)],
        env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise Refused(4, f"pg_restore exited {proc.returncode}: {proc.stderr.strip()}")
    with _snapshot_conn(dsn) as conn:
        observed = bm.describe(conn)
        conn.rollback()
    mismatches = bm.compare(manifest, observed)
    pending: dict | str
    try:
        from apps.core import schema_sets
        with psycopg.connect(dsn) as conn:
            pending = schema_sets.pending(conn)
    except Exception as exc:  # reported, not raised: the restore itself is what is verified
        pending = f"{type(exc).__name__}: {exc}"
    return {"ok": not mismatches, "verified": not mismatches, "mismatches": mismatches,
            "tables": len(observed["tables"]), "rows": sum(observed["tables"].values()),
            "backup_created_at": manifest["created_at"],
            "schema_pending_against_this_checkout": pending}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.backup", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="dump the database and write its manifest")
    b.add_argument("--dsn", required=True)
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--copy-to", action="append", default=[], type=Path,
                   help="also copy both files here and re-hash them (repeatable)")
    b.add_argument("--pg-bin")
    v = sub.add_parser("verify", help="check a backup's files against its manifest")
    v.add_argument("--manifest", required=True, type=Path)
    r = sub.add_parser("restore", help="restore into an EMPTY database and verify it")
    r.add_argument("--manifest", required=True, type=Path)
    r.add_argument("--dsn", required=True)
    r.add_argument("--pg-bin")
    pr = sub.add_parser("prune", help="apply retention (14 daily + 4 weekly) to a folder")
    pr.add_argument("--dir", required=True, action="append", type=Path)
    pr.add_argument("--keep-daily", type=int, default=14)
    pr.add_argument("--keep-weekly", type=int, default=4)
    pr.add_argument("--now", help="ISO time with a timezone (tests); default: now")
    pr.add_argument("--dry-run", action="store_true")
    rh = sub.add_parser("rehearse", help="restore into a NEW scratch cluster with the given "
                                         "roles and verify it")
    rh.add_argument("--manifest", required=True, type=Path)
    rh.add_argument("--role", required=True, action="append",
                    help="a role to create first (repeatable): meshychat_app and the owner role")
    rh.add_argument("--pg-bin")
    rh.add_argument("--keep-cluster", action="store_true")
    sv = sub.add_parser("serve", help="serve an export folder READ-ONLY over HTTP for HQB's "
                                      "pull; no credential, test data only "
                                      "(tools/backup/serve.py)")
    sv.add_argument("--dir", required=True, type=Path, help="the export folder")
    sv.add_argument("--bind", required=True,
                    help="the ONE address to listen on, e.g. 192.168.50.20; a wildcard "
                         "(0.0.0.0, ::, empty) is refused")
    sv.add_argument("--port", required=True, type=int)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "prune":
            from tools.backup import retention
            now = (dt.datetime.fromisoformat(a.now) if a.now
                   else dt.datetime.now(dt.timezone.utc))
            result = {"ok": True, "dirs": [
                retention.prune(d, now, a.keep_daily, a.keep_weekly, a.dry_run)
                for d in a.dir]}
            code = 0
        elif a.cmd == "rehearse":
            from tools.backup import rehearse
            result = rehearse.rehearse(a.manifest, a.role, a.pg_bin, a.keep_cluster)
            code = 0 if result["verified"] else 5
        elif a.cmd == "serve":
            from tools.backup import serve
            result, code = serve.serve(a.dir, a.bind, a.port), 0
        elif a.cmd == "backup":
            result, code = backup(a.dsn, a.out, a.copy_to, a.pg_bin), 0
        elif a.cmd == "verify":
            m = bm.load(a.manifest)
            result, code = {"ok": True, "manifest": str(a.manifest),
                            "dump_sha256": m["dump_sha256"], "created_at": m["created_at"],
                            "tables": len(m["tables"])}, 0
        else:
            result = restore(a.manifest, a.dsn, a.pg_bin)
            code = 0 if result["verified"] else 5
    except bm.BackupInvalid as exc:
        result, code = {"ok": False, "error": str(exc)}, 2
    except Refused as exc:
        result, code = {"ok": False, "error": str(exc)}, exc.code
    except (FileNotFoundError, ValueError) as exc:
        if a.cmd != "prune":
            raise
        result, code = {"ok": False, "error": str(exc)}, 3
    if code:
        print(result.get("error") or "; ".join(result.get("mismatches", [])), file=sys.stderr)
    print(json.dumps(result, sort_keys=True))
    return code
