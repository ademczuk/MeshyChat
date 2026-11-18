"""The offsite copy is HQB, over the LAN, pulled; both sides keep 14 daily + 4 weekly; and a
restore rehearsal into a scratch cluster that has this database's roles (E02, operator
decision 2026-09-25, item 2).

THE DESIGN. HQD (192.168.50.20) writes the daily dump and its manifest into an export folder
(`python -m tools.backup backup --out EXPORT`, then `prune --dir EXPORT`) and serves it
read-only over HTTP (`tools.backup serve`, LAN address, firewall-scoped to HQB). HQB
(192.168.50.10) PULLS from it with tools/backup/pull_to_hqb.ps1: it copies each new dump,
hashes the COPY against the manifest's sha256 and size before it names it a backup, writes the
manifest last (from the very bytes it verified), and applies the same retention there. No
credential of any kind (operator, 2026-09-26: the first design's SMB account password was the
step that failed): HQD holds nothing of HQB's, HQB needs no database credential and no HQD
credential. HQB runs one script from this repository, which it can read before it runs it.
Nothing here schedules anything; README "Offsite copy" says what to install and where.

RETENTION (tools/backup/retention.py, and the same rule in the PowerShell script; one test
runs both on the same names and requires the same answer). Per database name, from the UTC
stamp in the file name: the newest backup of each of the 14 most recent UTC days that HAVE a
backup, then the newest backup of each of the 4 most recent ISO weeks (Monday-based) older
than the oldest kept day. A backup stamped after `now` is kept and not counted (a clock that
jumps must not push real backups out). The rule counts days that have backups, not calendar
days, so when backups stop, pruning stops too: the last 18 stay.

What this file proves, each refusal beside its control:
  * the plan, on a fixed clock: daily backups for 61 days keep exactly 14 days + 4 weeks,
    named here by hand; a second backup the same day keeps only the newer; a future-stamped
    backup is kept and displaces nothing; stopping backups for months deletes nothing more;
    two databases in one folder are planned separately; a name that does not parse is never
    touched;
  * `prune` deletes a pruned backup's manifest AND dump, leaves everything else, and
    `--dry-run` deletes nothing;
  * the HQB pull (run with PowerShell 5.1, as HQB will): copies what retention keeps and
    nothing it would delete, verifies each copy, ends with exactly the set the Python plan
    keeps, never writes to the source, copies nothing on a second run; a dump whose bytes
    changed on the way is not a backup at HQB (no dump, no manifest, no partial file) and the
    run exits 1 naming it; a manifest naming a file other than its own dump is refused;
    -KeepDaily 0 or -1, -KeepWeekly -1 and -MaxAgeHours 0 are refused and delete nothing
    (review finding 3); the newest held backup older than 36 hours, a HQD clock stepped back,
    and an empty folder exit 2 with "ok":false (finding 4); database names that differ only
    in case are planned apart, as in Python (finding 7);
  * every one of those runs TWICE, from a folder and from an HTTP server keeping the serve
    contract; and over HTTP only: an index that is not {"files": [...]} is an unreadable
    source (exit 3), a listed name with a path in it is never used (the client applies the
    name rule itself, since it joins names onto its folder), names outside the rule never
    reach HQB, and a dump the server errors on or does not list is a failed copy with no
    partial file left; a HOSTILE server (no rule of its own, answering escaping names with a
    manifest and dump that verify) cannot make the pull write beside -Dest, which two
    independent client checks each prevent alone; and no redirect is followed, on the
    index, a manifest or a dump;
  * the rehearsal: a real backup of a seeded database restores into a NEW cluster (initdb)
    that has only the roles it was given, verifies every table's count and every ledger
    against the manifest; without meshychat_app, and without the owner role, pg_restore stops
    (the review's finding) and the rehearsal exits 4 naming the role.

Run:  MESHY_PG_BIN=<a PostgreSQL 16 installation's bin> .venv/Scripts/python.exe -m pytest tests/test_backup_offsite.py -q
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import schema_sets  # noqa: E402
from tools.backup import retention  # noqa: E402

PULL = ROOT / "tools" / "backup" / "pull_to_hqb.ps1"
DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)          # a Friday


def _unavailable(msg: str):
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(msg)
    pytest.skip(msg)


def _name(db: str, ts: dt.datetime) -> str:
    return f"{db}-{ts.strftime('%Y%m%dT%H%M%SZ')}.manifest.json"


def _daily(db: str, first: dt.date, last: dt.date, hour: int = 3) -> list[str]:
    out, d = [], first
    while d <= last:
        out.append(_name(db, dt.datetime(d.year, d.month, d.day, hour, tzinfo=UTC)))
        d += dt.timedelta(days=1)
    return out


def _days(names) -> list[str]:
    return sorted(n.split("-", 1)[1][:8] for n in names)


# ------------------------------------------------------------------ the plan, fixed clock

SIXTY_ONE = _daily("meshy_django", dt.date(2026, 7, 27), dt.date(2026, 9, 25))

EXPECTED_KEEP_DAYS = sorted(
    [f"202609{d:02d}" for d in range(12, 26)]            # 14 days: Sat 09-12 .. Fri 09-25
    + ["20260911",                                       # week of Mon 09-07: its newest older day
       "20260906",                                       # week of Mon 08-31: Sunday 09-06
       "20260830",                                       # week of Mon 08-24
       "20260823"])                                      # week of Mon 08-17


def test_daily_backups_for_61_days_keep_14_days_and_4_weeks():
    plan = retention.plan(SIXTY_ONE, NOW)
    assert _days(plan["keep"]) == EXPECTED_KEEP_DAYS
    assert len(plan["keep"]) == 18 and len(plan["delete"]) == 61 - 18
    assert set(plan["keep"]) | set(plan["delete"]) == set(SIXTY_ONE)
    assert plan["future"] == [] and plan["ignored"] == []


def test_a_second_backup_the_same_day_keeps_only_the_newer():
    early = _name("meshy_django", dt.datetime(2026, 9, 25, 1, tzinfo=UTC))
    plan = retention.plan(SIXTY_ONE + [early], NOW)
    assert early in plan["delete"]
    assert _days(plan["keep"]) == EXPECTED_KEEP_DAYS


def test_a_backup_stamped_in_the_future_is_kept_and_displaces_nothing():
    ahead = _name("meshy_django", dt.datetime(2026, 9, 27, 3, tzinfo=UTC))
    plan = retention.plan(SIXTY_ONE + [ahead], NOW)
    assert plan["future"] == [ahead] and ahead in plan["keep"]
    assert _days(set(plan["keep"]) - {ahead}) == EXPECTED_KEEP_DAYS


def test_when_backups_stop_pruning_stops():
    """Months without a new backup: the same 18 stay. A rule on calendar days would delete
    every one of them."""
    later = retention.plan(SIXTY_ONE, NOW + dt.timedelta(days=120))
    assert sorted(later["keep"]) == sorted(retention.plan(SIXTY_ONE, NOW)["keep"])


def test_two_databases_in_one_folder_are_planned_separately_and_odd_names_are_ignored():
    other = _daily("meshy_restore_check", dt.date(2026, 9, 20), dt.date(2026, 9, 25))
    odd = ["notes.txt.manifest.json", "meshy_django-2026-09-25.manifest.json"]
    plan = retention.plan(SIXTY_ONE + other + odd, NOW)
    assert set(other) <= set(plan["keep"]), "six days of another database are all dailies"
    assert _days(set(plan["keep"]) - set(other)) == EXPECTED_KEEP_DAYS
    assert sorted(plan["ignored"]) == sorted(odd)
    assert not set(odd) & (set(plan["keep"]) | set(plan["delete"]))


def test_the_plan_refuses_a_clock_without_a_timezone_and_a_zero_daily_count():
    with pytest.raises(ValueError):
        retention.plan(SIXTY_ONE, NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        retention.plan(SIXTY_ONE, NOW, keep_daily=0)


# ------------------------------------------------------------------ prune on disk

def _fake_backup(folder: Path, manifest_name: str, payload: bytes | None = None) -> Path:
    """A dump and a manifest in the shape tools/backup writes (format, dump_file, size,
    sha256); the pull and prune read no other field."""
    folder.mkdir(parents=True, exist_ok=True)
    base = manifest_name[:-len(".manifest.json")]
    body = payload if payload is not None else f"dump of {base}\n".encode() * 50
    (folder / f"{base}.dump").write_bytes(body)
    manifest = {"format": "meshychat-backup/1", "dump_file": f"{base}.dump",
                "dump_bytes": len(body), "dump_sha256": hashlib.sha256(body).hexdigest(),
                "created_at": "see the file name", "tables": {"public.peers": 3},
                "ledgers": {}}
    path = folder / manifest_name
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return path


def _cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-m", "tools.backup", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)


def _listing(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.iterdir()) if p.is_file()}


def test_prune_deletes_both_files_of_a_pruned_backup_and_nothing_else(tmp_path):
    folder = tmp_path / "export"
    for n in SIXTY_ONE:
        _fake_backup(folder, n)
    (folder / "README.txt").write_text("not a backup", encoding="utf-8")
    before = _listing(folder)

    dry = _cli("prune", "--dir", str(folder), "--now", NOW.isoformat(), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert _listing(folder) == before, "--dry-run deletes nothing"
    assert len(json.loads(dry.stdout.strip().splitlines()[-1])["dirs"][0]["delete"]) == 43

    real = _cli("prune", "--dir", str(folder), "--now", NOW.isoformat())
    assert real.returncode == 0, real.stderr
    left = _listing(folder)
    kept = retention.plan(SIXTY_ONE, NOW)["keep"]
    expect = {"README.txt"} | set(kept) | {k[:-len(".manifest.json")] + ".dump" for k in kept}
    assert set(left) == expect
    assert all(left[k] == before[k] for k in left), "what is kept is untouched"


# ------------------------------------------------------------------ the HQB pull (PowerShell)
#
# Every pull test runs TWICE: from a folder, and from an HTTP server that follows the
# contract HQD's `python -m tools.backup serve` implements (operator decision 2026-09-26,
# no credential): GET /index.json is {"files": [{"name", "bytes", "mtime"}]} listing only
# names matching SERVE_NAME, GET /<name> returns a listed file's bytes, anything else 404,
# any other method 405. The fixture below is that contract, written here so these tests do
# not depend on the server's own branch; the verification logic under test is the same.

import http.server  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402

SERVE_NAME = re.compile(r"^[a-z0-9_]+-\d{8}T\d{6}Z\.(dump|manifest\.json)$")


class _ExportServer:
    """Serves one folder by the contract. `index_override` replaces the index body,
    `status` forces an HTTP status for one name and `redirect` answers 302 to a URL: the
    failures a real server can produce.

    `hostile=True` drops the contract on the SERVER side: any requested path is answered
    with the file of the same last segment, no name rule, no listing check. Txxxx's review
    of d4b922a: a well-behaved stub that refuses an escaping name itself backstops a broken
    client, so a client-side defence can only be proven against a server that does not."""

    def __init__(self, folder: Path, *, hostile: bool = False):
        self.folder = folder
        self.hostile = hostile
        self.index_override: bytes | None = None
        self.status: dict[str, int] = {}
        self.redirect: dict[str, str] = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body: bytes = b"", ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                name = self.path.lstrip("/")
                if name in outer.redirect:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect[name])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if outer.hostile and name != "index.json":
                    from urllib.parse import unquote
                    leaf = re.split(r"[\\/]", unquote(name))[-1]
                    path = outer.folder / leaf
                    if not path.is_file():
                        return self._send(404, b"not found", "text/plain")
                    return self._send(200, path.read_bytes(), "application/octet-stream")
                if name == "index.json":
                    if outer.index_override is not None:
                        return self._send(200, outer.index_override)
                    files = [{"name": p.name, "bytes": p.stat().st_size,
                              "mtime": int(p.stat().st_mtime)}
                             for p in sorted(outer.folder.iterdir())
                             if p.is_file() and SERVE_NAME.match(p.name)] \
                        if outer.folder.is_dir() else []
                    return self._send(200, json.dumps({"files": files}).encode())
                if name in outer.status:
                    return self._send(outer.status[name], b"forced")
                path = outer.folder / name
                if not SERVE_NAME.match(name) or not path.is_file():
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, path.read_bytes(), "application/octet-stream")

            def _refuse(self):
                self._send(405, b"method not allowed", "text/plain")

            do_POST = do_PUT = do_DELETE = do_HEAD = _refuse

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(params=["folder", "http"])
def source_of(request):
    """Maps an export folder to the -Source the pull is given: the folder itself, or the
    URL of a contract server over it."""
    servers: list[_ExportServer] = []

    def make(folder: Path) -> str:
        if request.param == "folder":
            return str(folder)
        for s in servers:
            if s.folder == folder:
                return s.url
        s = _ExportServer(folder)
        servers.append(s)
        return s.url

    make.transport = request.param
    make.servers = servers
    yield make
    for s in servers:
        s.close()


def _powershell() -> str:
    found = shutil.which("powershell") or shutil.which("powershell.exe")
    if not found:
        _unavailable("powershell not found: the HQB pull script cannot be exercised")
    return found


def _pull_raw(source, dest: Path, now: dt.datetime, *extra: str
              ) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(PULL), "-Source", str(source), "-Dest", str(dest),
         "-Now", now.strftime("%Y-%m-%dT%H:%M:%SZ"), *extra],
        capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)


def _pull(source, dest: Path, now: dt.datetime = NOW, *extra: str
          ) -> tuple[int, dict, str]:
    proc = _pull_raw(source, dest, now, *extra)
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines, f"no output rc={proc.returncode}\n{proc.stderr}"
    return proc.returncode, json.loads(lines[-1]), proc.stdout + proc.stderr


def _manifests(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("*.manifest.json"))


def test_the_pull_ends_with_exactly_what_the_python_plan_keeps(source_of, tmp_path):
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    ahead = _name("meshy_django", dt.datetime(2026, 9, 27, 3, tzinfo=UTC))
    early = _name("meshy_django", dt.datetime(2026, 9, 25, 1, tzinfo=UTC))
    other = _daily("meshy_restore_check", dt.date(2026, 9, 1), dt.date(2026, 9, 25), hour=7)
    names = SIXTY_ONE + [ahead, early] + other
    for n in names:
        _fake_backup(src, n)
    source_before = _listing(src)

    rc, out, text = _pull(source_of(src), dest)
    assert rc == 0 and out["ok"] is True, text
    plan = retention.plan(names, NOW)
    assert _manifests(dest) == sorted(plan["keep"]), "PowerShell and Python disagree"
    assert sorted(out["copied"]) == sorted(plan["keep"]), "it copies only what it keeps"
    assert out["deleted"] == [] and out["failed"] == []
    for m in _manifests(dest):                            # every copy is the verified bytes
        assert (dest / m).read_bytes() == (src / m).read_bytes()
        base = m[:-len(".manifest.json")]
        assert (dest / f"{base}.dump").read_bytes() == (src / f"{base}.dump").read_bytes()
    assert not list(dest.glob("*.partial")), "no partial file survives"
    assert _listing(src) == source_before, "the pull never writes to HQD"

    rc2, out2, text2 = _pull(source_of(src), dest)                   # idempotent
    assert rc2 == 0 and out2["copied"] == [] and out2["deleted"] == [], text2
    assert out2["already_present"] == len(plan["keep"])


def test_the_pull_applies_retention_to_what_hqb_already_has(source_of, tmp_path):
    """A week later: HQB prunes its own copies by the same rule, and the source's newer
    days arrive."""
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    for n in SIXTY_ONE:
        _fake_backup(src, n)
    assert _pull(source_of(src), dest)[0] == 0
    week = _daily("meshy_django", dt.date(2026, 9, 26), dt.date(2026, 10, 2))
    for n in week:
        _fake_backup(src, n)
    later = NOW + dt.timedelta(days=7)
    rc, out, text = _pull(source_of(src), dest, later)
    assert rc == 0, text
    plan = retention.plan(SIXTY_ONE + week, later)
    assert _manifests(dest) == sorted(plan["keep"])
    assert out["deleted"], "the older copies at HQB were pruned"
    for gone in out["deleted"]:
        base = gone[:-len(".manifest.json")]
        assert not (dest / gone).exists() and not (dest / f"{base}.dump").exists()


def test_a_dump_that_changed_on_the_way_is_not_a_backup_at_hqb(source_of, tmp_path):
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    names = _daily("meshy_django", dt.date(2026, 9, 23), dt.date(2026, 9, 25))
    for n in names:
        _fake_backup(src, n)
    bad = names[1]
    bad_dump = src / (bad[:-len(".manifest.json")] + ".dump")
    raw = bytearray(bad_dump.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    bad_dump.write_bytes(bytes(raw))

    rc, out, text = _pull(source_of(src), dest)
    assert rc == 1 and out["ok"] is False, text
    assert [f.split(":")[0] for f in out["failed"]] == [bad], out
    assert "sha256" in " ".join(out["failed"]), out
    assert _manifests(dest) == sorted(set(names) - {bad}), "control: the others arrived"
    assert not (dest / bad).exists()
    assert not (dest / bad_dump.name).exists() and not list(dest.glob("*.partial"))


def test_a_manifest_naming_another_file_is_refused(source_of, tmp_path):
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    good, evil = _daily("meshy_django", dt.date(2026, 9, 24), dt.date(2026, 9, 25))
    _fake_backup(src, good)
    path = _fake_backup(src, evil)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["dump_file"] = "..\\" + manifest["dump_file"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    rc, out, text = _pull(source_of(src), dest)
    assert rc == 1 and [f.split(":")[0] for f in out["failed"]] == [evil], text
    assert _manifests(dest) == [good]


@pytest.mark.parametrize("arg,value", [("-KeepDaily", "0"), ("-KeepDaily", "-1"),
                                       ("-KeepWeekly", "-1"), ("-MaxAgeHours", "0")])
def test_the_pull_refuses_a_retention_that_would_keep_nothing(source_of, tmp_path, arg, value):
    """Review 2026-09-25, finding 3: -KeepDaily 0 deleted every backup HQB held and reported
    ok, where retention.plan refuses the same input."""
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    names = _daily("meshy_django", dt.date(2026, 9, 21), dt.date(2026, 9, 25))
    for n in names:
        _fake_backup(src, n)
    assert _pull(source_of(src), dest)[0] == 0 and _manifests(dest) == sorted(names)     # control
    proc = _pull_raw(source_of(src), dest, NOW, arg, value)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert '"ok":true' not in proc.stdout.replace(" ", ""), proc.stdout
    assert _manifests(dest) == sorted(names), "nothing held was deleted"


FORTY = _daily("meshy_django", dt.date(2026, 8, 17), dt.date(2026, 9, 25))


def test_the_pull_fails_when_the_newest_backup_held_is_too_old(source_of, tmp_path):
    """Review 2026-09-25, finding 4: when HQD's backups stop, every later pull exited 0 with
    "ok":true. The newest held backup of each database must be at most -MaxAgeHours (36)
    old, or the run exits 2 naming it."""
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    for n in FORTY:
        _fake_backup(src, n)
    assert _pull(source_of(src), dest)[0] == 0
    rc, out, text = _pull(source_of(src), dest, NOW + dt.timedelta(days=1))            # 33 h: control
    assert rc == 0 and out["ok"] is True and out["stale"] == [], text
    rc, out, text = _pull(source_of(src), dest, NOW + dt.timedelta(days=9))
    assert rc == 2 and out["ok"] is False, text
    assert len(out["stale"]) == 1 and "meshy_django" in out["stale"][0], out
    assert "20260925T030000Z" in out["stale"][0], out
    rc, out, text = _pull(source_of(src), dest, NOW + dt.timedelta(days=9), "-MaxAgeHours", "240")
    assert rc == 0 and out["ok"] is True, text                               # the knob


def test_a_hqd_clock_stepped_back_is_a_failure_not_ok(source_of, tmp_path):
    """Review 2026-09-25, finding 4: HQD writes a backup stamped 2025-01-01; it lands in
    outside_retention, is never copied, and the runs said ok. Nothing new arrives, so the
    freshness check fails it, and the verdict names the backups the source offers outside
    retention."""
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    for n in FORTY:
        _fake_backup(src, n)
    assert _pull(source_of(src), dest)[0] == 0
    back = _name("meshy_django", dt.datetime(2025, 1, 1, 3, tzinfo=UTC))
    _fake_backup(src, back)
    rc, out, text = _pull(source_of(src), dest, dt.datetime(2026, 9, 27, 12, tzinfo=UTC))
    assert rc == 2 and out["ok"] is False and out["copied"] == [], text
    assert back in out["outside_retention"], out
    assert "outside retention" in out["stale"][0], out


def test_a_pull_with_nothing_held_is_stale(source_of, tmp_path):
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    src.mkdir()
    rc, out, text = _pull(source_of(src), dest)
    assert rc == 2 and out["ok"] is False and out["stale"], text


def test_database_names_that_differ_only_in_case_are_planned_separately(tmp_path):
    """Review 2026-09-25, finding 7: Group-Object is case-insensitive, Python is not. 14 days
    of "meshy" and of "MESHY": Python keeps 28, HQB kept 14.

    FOLDER ONLY: over HTTP the server lists only lowercase database names (SERVE_NAME), so
    "MESHY" never reaches HQB there; test_http_names_outside_the_rule_never_reach_hqb pins
    that instead."""
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    first, last = dt.date(2026, 9, 12), dt.date(2026, 9, 25)
    names = _daily("meshy", first, last, hour=3) + _daily("MESHY", first, last, hour=5)
    for n in names:
        _fake_backup(src, n)
    assert len(retention.plan(names, NOW)["keep"]) == 28                    # control
    rc, out, text = _pull(src, dest)
    assert rc == 0, text
    assert _manifests(dest) == sorted(retention.plan(names, NOW)["keep"])
    assert len(_manifests(dest)) == 28


def test_the_pull_refuses_a_source_it_cannot_reach(tmp_path):
    rc, out, text = _pull(tmp_path / "no-such-share", tmp_path / "hqb-backups")
    assert rc != 0 and out["ok"] is False and "source" in out["error"].lower(), text


def test_the_pull_refuses_a_server_it_cannot_reach(tmp_path):
    """Exit 3 when nothing answers: a port just freed, so nothing listens on it."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rc, out, text = _pull(f"http://127.0.0.1:{port}", tmp_path / "hqb-backups")
    assert rc == 3 and out["ok"] is False and "source not reachable" in out["error"], text


# ------------------------------------------------------------------ the HQB pull, HTTP only

def _http_export(tmp_path, names) -> tuple[Path, Path, "_ExportServer"]:
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    for n in names:
        _fake_backup(src, n)
    return src, dest, _ExportServer(src)


def _index(folder: Path, extra=(), drop=()) -> bytes:
    files = [{"name": p.name, "bytes": p.stat().st_size, "mtime": 0}
             for p in sorted(folder.iterdir()) if p.is_file() and p.name not in drop]
    files += [{"name": n, "bytes": 1, "mtime": 0} for n in extra]
    return json.dumps({"files": files}).encode()


@pytest.mark.parametrize("body", [b"<html>not an index</html>", b'{"files": "x"}', b"[]",
                                  b'{"items": []}'])
def test_an_index_that_is_not_the_contract_is_an_unreadable_source(tmp_path, body):
    """A server answering 200 with something other than {"files": [...]} is not an empty
    export: exit 3, "ok":false, and nothing is copied or pruned. Control: the same server
    with its real index pulls."""
    names = _daily("meshy_django", dt.date(2026, 9, 23), dt.date(2026, 9, 25))
    src, dest, server = _http_export(tmp_path, names)
    try:
        server.index_override = body
        rc, out, text = _pull(server.url, dest)
        assert rc == 3 and out["ok"] is False and "index" in out["error"], text
        assert not dest.exists() or _manifests(dest) == []
        server.index_override = None
        rc, out, text = _pull(server.url, dest)
        assert rc == 0 and _manifests(dest) == sorted(names), text
    finally:
        server.close()


def test_an_http_name_that_would_escape_the_folder_is_never_used(tmp_path):
    """Over HTTP the names come from the server and are joined onto -Dest. A listed name
    with a path in it is ignored, never copied, never failed, never written: the client
    applies the server's name rule itself. Control: the well-formed backups arrive."""
    names = _daily("meshy_django", dt.date(2026, 9, 24), dt.date(2026, 9, 25))
    src, dest, server = _http_export(tmp_path, names)
    evil = ["..\\escaped-20260925T030000Z.manifest.json",
            "../escaped-20260925T030000Z.manifest.json",
            "sub\\escaped-20260925T030000Z.manifest.json",
            "Escaped-20260925T030000Z.manifest.json"]
    try:
        server.index_override = _index(src, extra=evil)
        rc, out, text = _pull(server.url, dest)
        assert rc == 0 and _manifests(dest) == sorted(names), text
        listed = json.dumps(out)
        assert "escaped" not in listed.lower(), out
        assert not list(tmp_path.glob("escaped*")) and not list(tmp_path.rglob("*escaped*"))
    finally:
        server.close()


def test_a_hostile_server_cannot_make_the_pull_write_outside_its_folder(tmp_path):
    """The client's defence against a HOSTILE source (txxxx, review of d4b922a). The server
    here lists two names that climb out of -Dest, one per separator, and ANSWERS them with a
    manifest and a dump that verify: dump_file names the escaping dump and its sha256 is
    right, so nothing downstream of the name would stop the write. Only the client's name
    checks stand between that listing and a file beside -Dest.

    The layout makes an escape visible: -Dest is hqb/backups, so a name starting "..\\" or
    "../" lands in hqb/, which holds nothing else. Control: the well-formed backups arrive."""
    good = _daily("meshy_django", dt.date(2026, 9, 24), dt.date(2026, 9, 25))
    stage = tmp_path / "hqd"
    for n in good:
        _fake_backup(stage, n)
    listed = list(good) + [n[:-len(".manifest.json")] + ".dump" for n in good]
    for climb, leaf in (("..\\", "escbs"), ("../", "escfs")):
        base = f"{climb}{leaf}-20260925T030000Z"
        body = f"hostile {leaf}\n".encode() * 40
        (stage / f"{leaf}-20260925T030000Z.dump").write_bytes(body)
        (stage / f"{leaf}-20260925T030000Z.manifest.json").write_text(json.dumps({
            "format": "meshychat-backup/1", "dump_file": f"{base}.dump",
            "dump_bytes": len(body), "dump_sha256": hashlib.sha256(body).hexdigest()}),
            encoding="utf-8")
        listed += [f"{base}.manifest.json", f"{base}.dump"]
    server = _ExportServer(stage, hostile=True)
    server.index_override = json.dumps(
        {"files": [{"name": n, "bytes": 1, "mtime": 0} for n in listed]}).encode()
    hqb = tmp_path / "hqb"
    dest = hqb / "backups"
    try:
        rc, out, text = _pull(server.url, dest)
        assert sorted(p.name for p in hqb.iterdir()) == ["backups"], \
            f"a file escaped -Dest: {sorted(p.name for p in hqb.iterdir())}\n{text}"
        assert rc == 0 and _manifests(dest) == sorted(good), text
        assert "esc" not in json.dumps(out), out
    finally:
        server.close()


@pytest.mark.parametrize("which", ["index", "manifest", "dump"])
def test_a_redirect_is_never_followed(tmp_path, which):
    """Txxxx, review of d4b922a: -MaximumRedirection 0 on all three requests, so the firewall
    scope stays the whole trust story. The redirect target here is a WORKING server over the
    same folder, so a client that followed it would succeed: the refusal is the only thing
    the test can see. The index answers exit 3; a manifest or dump is a failed copy."""
    names = _daily("meshy_django", dt.date(2026, 9, 24), dt.date(2026, 9, 25))
    src, dest, server = _http_export(tmp_path, names)
    elsewhere = _ExportServer(src)
    target = names[1] if which == "manifest" else (
        names[1][:-len(".manifest.json")] + ".dump" if which == "dump" else "index.json")
    try:
        server.redirect[target] = f"{elsewhere.url}/{target}"
        rc, out, text = _pull(server.url, dest)
        if which == "index":
            assert rc == 3 and out["ok"] is False, text
        else:
            assert rc == 1 and [f.split(":")[0] for f in out["failed"]] == [names[1]], text
            assert _manifests(dest) == [names[0]] and not list(dest.glob("*.partial"))
        server.redirect.clear()                                   # control: no redirect
        rc, out, text = _pull(server.url, dest)
        assert rc == 0 and _manifests(dest) == sorted(names), text
    finally:
        server.close()
        elsewhere.close()


def test_http_names_outside_the_rule_never_reach_hqb(tmp_path):
    """The serve contract lists lowercase database names only, so an uppercase or hyphenated
    database is never offered; HQB cannot know it exists. Control: the lowercase one
    arrives. (The freshness alarm cannot name a database HQB has never seen.)"""
    first, last = dt.date(2026, 9, 24), dt.date(2026, 9, 25)
    names = (_daily("meshy", first, last) + _daily("MESHY", first, last, hour=5)
             + _daily("meshy-dev", first, last, hour=7))
    src, dest, server = _http_export(tmp_path, names)
    try:
        rc, out, text = _pull(server.url, dest)
        assert rc == 0, text
        assert _manifests(dest) == sorted(_daily("meshy", first, last))
    finally:
        server.close()


def test_a_dump_the_server_fails_to_send_is_a_failed_copy(tmp_path):
    """A 500 on one dump, and a dump the index does not list: each is named in "failed", the
    run exits 1, no partial file survives, and the other backups arrive (the control)."""
    names = _daily("meshy_django", dt.date(2026, 9, 22), dt.date(2026, 9, 25))
    src, dest, server = _http_export(tmp_path, names)
    erroring = names[1][:-len(".manifest.json")] + ".dump"
    unlisted = names[2][:-len(".manifest.json")] + ".dump"
    try:
        server.status[erroring] = 500
        server.index_override = _index(src, drop=(unlisted,))
        rc, out, text = _pull(server.url, dest)
        assert rc == 1 and out["ok"] is False, text
        assert sorted(f.split(":")[0] for f in out["failed"]) == sorted([names[1], names[2]])
        assert f"no {unlisted} at the source" in " ".join(out["failed"]), out
        assert _manifests(dest) == sorted({names[0], names[3]})
        assert not list(dest.glob("*.partial"))
        assert not (dest / erroring).exists() and not (dest / names[1]).exists()
    finally:
        server.close()


# ------------------------------------------------------------------ the restore rehearsal

def _admin() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


def _server_bin() -> str:
    base = os.environ.get("MESHY_PG_BIN")
    if not base:
        found = shutil.which("initdb")
        base = str(Path(found).parent) if found else None
    if not base or not any((Path(base) / n).exists() for n in ("initdb", "initdb.exe")):
        _unavailable(f"initdb not found (MESHY_PG_BIN={base!r}): the restore rehearsal needs "
                     "a PostgreSQL installation")
    return base


@pytest.fixture(scope="module")
def backed_up(tmp_path_factory):
    """A migrated database with rows, backed up by the repository's own tool."""
    pg_bin = _server_bin()
    name = f"meshy_rehearse_src_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(_admin(), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(dsn) as conn:
            schema_sets.apply_all(conn, verbose=False)
            for i in range(4):
                owner, peer = uuid.uuid4(), uuid.uuid4()
                conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                             "VALUES (%s,%s,'dev',%s)",
                             (owner, f"rh-{i}-{owner.hex[:8]}@example.test", uuid.uuid4()))
                conn.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                             "VALUES (%s,%s,%s,%s)", (peer, owner, f"rh-{i}", f"rh {i}"))
            conn.commit()
            owners = sorted(r[0] for r in conn.execute(
                "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'"))
        out = tmp_path_factory.mktemp("rehearse")
        proc = _cli("backup", "--dsn", dsn, "--out", str(out), "--pg-bin", pg_bin)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        manifest = Path(json.loads(proc.stdout.strip().splitlines()[-1])["manifest"])
        yield manifest, owners, pg_bin
    finally:
        with psycopg.connect(_admin(), autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _rehearse(manifest: Path, pg_bin: str, roles: list[str]) -> tuple[int, dict]:
    args = ["rehearse", "--manifest", str(manifest), "--pg-bin", pg_bin]
    for r in roles:
        args += ["--role", r]
    proc = _cli(*args)
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_rehearsal_restores_into_a_new_cluster_with_the_roles_and_verifies_counts(backed_up):
    manifest_path, owners, pg_bin = backed_up
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert owners and "meshychat_app" not in owners, owners
    rc, out = _rehearse(manifest_path, pg_bin, ["meshychat_app", *owners])
    assert rc == 0, out
    assert out["verified"] is True and out["mismatches"] == [], out
    assert out["rows"] == sum(manifest["tables"].values()) > 0, "counts checked, and real"
    assert out["tables"] == len(manifest["tables"])
    assert manifest["tables"]["public.peers"] == 4
    assert out["scratch_system_identifier"] != manifest["system_identifier"], (
        "the rehearsal ran on a NEW cluster, not the one the backup came from")
    assert out["source_system_identifier"] == str(manifest["system_identifier"])
    assert sorted(out["roles_created"]) == sorted(["meshychat_app", *owners])


def test_the_rehearsal_exits_5_when_the_restored_counts_disagree(backed_up, tmp_path):
    """Review 2026-09-25, finding 8: rehearse's own exit-5 path. The same dump beside a
    manifest that claims one more peer: the restore succeeds, the count disagrees, exit 5
    naming the table."""
    manifest_path, owners, pg_bin = backed_up
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shutil.copy2(manifest_path.parent / manifest["dump_file"], tmp_path / manifest["dump_file"])
    manifest["tables"]["public.peers"] += 1
    lied = tmp_path / manifest_path.name
    lied.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    rc, out = _rehearse(lied, pg_bin, ["meshychat_app", *owners])
    assert rc == 5 and out["ok"] is False and out["verified"] is False, out
    assert any("public.peers" in m for m in out["mismatches"]), out["mismatches"]
    assert out["rows"] == sum(manifest["tables"].values()) - 1, "control: it did restore"


@pytest.mark.parametrize("missing", ["meshychat_app", "owner"])
def test_the_rehearsal_stops_when_a_role_is_missing(backed_up, missing):
    manifest_path, owners, pg_bin = backed_up
    roles = owners if missing == "meshychat_app" else ["meshychat_app"]
    rc, out = _rehearse(manifest_path, pg_bin, roles)
    assert rc == 4 and out["ok"] is False, out
    named = "meshychat_app" if missing == "meshychat_app" else owners[0]
    assert f'role "{named}" does not exist' in out["error"], out["error"][:2000]


# E17: the rehearsal's scratch directory on Windows. CPython 3.13 made tempfile.mkdtemp's
# 0o700 an ACL on Windows (SYSTEM, Administrators, OWNER RIGHTS); from an ELEVATED shell whose
# new objects are owned by Administrators, OWNER RIGHTS is Administrators too, and initdb (and
# the server pg_ctl starts) runs with Administrators removed from its token, so the directory
# admits nothing it holds and initdb fails ("could not create directory ...: File exists").
# Reproduced on HQD 2026-09-27 by setting a mkdtemp directory's owner to Administrators.
# Setting an owner needs elevation, so the test builds the SAME effective ACL without it:
# mkdtemp's directory with the OWNER RIGHTS entry removed admits only SYSTEM and
# Administrators, exactly what the Administrators-owned directory admits initdb.

_OWNER_RIGHTS = "*S-1-3-4"


def _icacls(*args: str) -> subprocess.CompletedProcess:
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    return subprocess.run([str(exe), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


def _initdb(pg_bin: str, data: Path) -> subprocess.CompletedProcess:
    exe = next(p for p in (Path(pg_bin) / "initdb.exe", Path(pg_bin) / "initdb") if p.exists())
    return subprocess.run([str(exe), "-D", str(data), "-U", "probe", "--auth=trust",
                           "--encoding=UTF8", "--no-instructions"], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=180)


class _ReachedAfterInitdb(Exception):
    pass


@pytest.mark.skipif(os.name != "nt", reason="the owner-only mkdtemp ACL is Windows'")
def test_the_rehearsal_initdb_runs_in_a_scratch_dir_that_admits_only_administrators(
        monkeypatch, tmp_path):
    from tools.backup import rehearse
    pg_bin = _server_bin()
    real_mkdtemp = tempfile.mkdtemp
    made: list[Path] = []

    def admins_only_mkdtemp(*a, **k):
        d = Path(real_mkdtemp(*a, **k))
        made.append(d)
        r = _icacls(str(d), "/remove:g", _OWNER_RIGHTS)
        assert r.returncode == 0, r.stdout + r.stderr
        return str(d)

    # Control: the fixture reproduces the fault. initdb, run directly in such a directory,
    # cannot use it.
    probe = Path(admins_only_mkdtemp(prefix="meshy-e17-control-"))
    try:
        got = _initdb(pg_bin, probe / "data")
        assert got.returncode != 0, "control: initdb used an Administrators-only directory"
    finally:
        _icacls(str(probe), "/grant", "*S-1-3-4:(OI)(CI)F")
        shutil.rmtree(probe, ignore_errors=True)

    monkeypatch.setattr(rehearse.bm, "load", lambda p: {})
    monkeypatch.setattr(rehearse.tempfile, "mkdtemp", admins_only_mkdtemp)

    def stop():
        assert (made[-1] / "data" / "PG_VERSION").is_file(), "initdb wrote no cluster"
        raise _ReachedAfterInitdb()
    monkeypatch.setattr(rehearse, "_free_port", stop)
    try:
        with pytest.raises(_ReachedAfterInitdb):
            rehearse.rehearse(tmp_path / "unused.manifest.json", [], pg_bin)
        assert not made[-1].exists(), "the scratch directory was not deleted"
    finally:
        if made[-1].exists():                  # RED: rehearse could not delete it either
            _icacls(str(made[-1]), "/grant", "*S-1-3-4:(OI)(CI)F")
            shutil.rmtree(made[-1], ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="the scratch directory's ACL is Windows'")
@pytest.mark.parametrize("fails", ["whoami", "icacls"])
def test_the_rehearsal_refuses_before_initdb_when_it_cannot_grant_itself_the_scratch_dir(
        monkeypatch, tmp_path, fails):
    """The grant cannot be made (the SID lookup or icacls fails): exit 3 before initdb runs,
    naming the directory and the fix, and the directory is gone. Positive control: the same
    call with both tools answering reaches initdb (here, a stub that records it)."""
    from tools.backup import rehearse
    real_run = rehearse.subprocess.run
    calls: list[str] = []
    broken = {fails}

    def fake_run(cmd, *a, **k):
        name = Path(cmd[0]).stem.lower()
        calls.append(name)
        if name in broken:
            return subprocess.CompletedProcess(cmd, 5, stdout="", stderr="Access is denied.")
        if name in ("whoami", "icacls"):
            return real_run(cmd, *a, **k)
        raise _ReachedAfterInitdb(name)

    made: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(rehearse.tempfile, "mkdtemp",
                        lambda *a, **k: made.append(Path(real_mkdtemp(*a, **k))) or str(made[-1]))
    monkeypatch.setattr(rehearse.bm, "load", lambda p: {})
    monkeypatch.setattr(rehearse, "_tool", lambda name, pg_bin: f"C:/pg/bin/{name}.exe")
    monkeypatch.setattr(rehearse.subprocess, "run", fake_run)

    with pytest.raises(rehearse.Refused) as exc:
        rehearse.rehearse(tmp_path / "unused.manifest.json", [], None)
    assert exc.value.code == 3, exc.value.code
    msg = str(exc.value)
    assert str(made[-1]) in msg and "non-elevated" in msg and "Access is denied." in msg, msg
    assert "initdb" not in calls, calls
    assert not made[-1].exists()

    broken.clear()
    calls.clear()
    with pytest.raises(_ReachedAfterInitdb):
        rehearse.rehearse(tmp_path / "unused.manifest.json", [], None)
    assert calls[-1] == "initdb" and fails in calls, calls


@pytest.mark.skipif(os.name != "nt", reason="the scratch directory's ACL is Windows'")
@pytest.mark.parametrize("fails", ["whoami", "icacls"])
@pytest.mark.parametrize("how", ["timeout", "oserror"])
def test_the_rehearsal_refuses_with_exit_3_when_a_grant_tool_hangs_or_cannot_be_run(
        monkeypatch, tmp_path, fails, how):
    """Review LOW-2 of fin/w3-ops: only a non-zero exit of whoami/icacls became Refused(3);
    a hang (subprocess.TimeoutExpired after 60 s) or a tool that cannot be started (an
    OSError, e.g. SystemRoot pointing nowhere) escaped as a traceback. Each is now the same
    exit 3 before initdb, naming the directory, the tool, the cause and the fix, and the
    directory is gone. Positive control: the same call with both tools answering reaches
    initdb (a stub that records it)."""
    from tools.backup import rehearse
    real_run = rehearse.subprocess.run
    calls: list[str] = []
    broken = {fails}

    def fake_run(cmd, *a, **k):
        name = Path(cmd[0]).stem.lower()
        calls.append(name)
        if name in broken:
            if how == "timeout":
                raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
            raise FileNotFoundError(2, "The system cannot find the file specified", cmd[0])
        if name in ("whoami", "icacls"):
            return real_run(cmd, *a, **k)
        raise _ReachedAfterInitdb(name)

    made: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(rehearse.tempfile, "mkdtemp",
                        lambda *a, **k: made.append(Path(real_mkdtemp(*a, **k))) or str(made[-1]))
    monkeypatch.setattr(rehearse.bm, "load", lambda p: {})
    monkeypatch.setattr(rehearse, "_tool", lambda name, pg_bin: f"C:/pg/bin/{name}.exe")
    monkeypatch.setattr(rehearse.subprocess, "run", fake_run)

    with pytest.raises(rehearse.Refused) as exc:
        rehearse.rehearse(tmp_path / "unused.manifest.json", [], None)
    assert exc.value.code == 3, exc.value.code
    msg = str(exc.value)
    cause = "did not answer in 60 s" if how == "timeout" else "cannot find the file"
    assert str(made[-1]) in msg and "non-elevated" in msg, msg
    assert f"{fails}.exe" in msg and cause in msg, msg
    assert "initdb" not in calls, calls
    assert not made[-1].exists()

    broken.clear()
    calls.clear()
    with pytest.raises(_ReachedAfterInitdb):
        rehearse.rehearse(tmp_path / "unused.manifest.json", [], None)
    assert calls[-1] == "initdb" and fails in calls, calls


NODETEST = _daily("meshy_nodetest", dt.date(2026, 9, 1), dt.date(2026, 9, 3))


def test_a_retired_database_is_kept_but_its_age_is_not_judged(source_of, tmp_path):
    """kxxx's ruling 2026-10-04: meshy_nodetest's server is stopped (decision 72) and its backups were
    ruled off on 09-29, so its newest dump will never be younger. -Retired names it: still copied and
    kept, never stale; a live database beside it is still judged."""
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    for n in FORTY + NODETEST:
        _fake_backup(src, n)
    rc, out, text = _pull(source_of(src), dest)                                   # control
    assert rc == 2 and len(out["stale"]) == 1 and "meshy_nodetest" in out["stale"][0], text
    rc, out, text = _pull(source_of(src), dest, NOW, "-Retired", "meshy_nodetest")
    assert rc == 0 and out["ok"] is True and out["stale"] == [] and out["retired"] == ["meshy_nodetest"], text
    assert set(NODETEST) <= set(_manifests(dest)), _manifests(dest)
    rc, out, text = _pull(source_of(src), dest, NOW + dt.timedelta(days=9), "-Retired", "meshy_nodetest")
    assert rc == 2 and len(out["stale"]) == 1 and "meshy_django" in out["stale"][0], text


def test_retired_takes_a_comma_list_and_matches_exact_names(tmp_path):
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    for n in FORTY + NODETEST:
        _fake_backup(src, n)
    rc, out, text = _pull(src, dest, NOW + dt.timedelta(days=9), "-Retired", "meshy_nodetest,meshy_django")
    assert rc == 0 and out["retired"] == ["meshy_nodetest", "meshy_django"], text
    rc, out, text = _pull(src, dest, NOW, "-Retired", "MESHY_NODETEST")         # not the same name
    assert rc == 2 and "meshy_nodetest" in out["stale"][0], text


def _held_only(tmp_path, names):
    """A HQB folder holding these backups, filled by one pull from a folder source."""
    src, dest = tmp_path / "hqd-export", tmp_path / "hqb-backups"
    for n in names:
        _fake_backup(src, n)
    _pull(src, dest, NOW, "-Retired", "meshy_nodetest,meshy_django")
    return dest


def test_an_unreadable_source_is_no_failure_when_everything_held_is_retired(tmp_path):
    dest = _held_only(tmp_path, NODETEST)
    gone = tmp_path / "no-such-share"
    rc, out, text = _pull(gone, dest)                                             # control
    assert rc == 3 and out["ok"] is False, text
    rc, out, text = _pull(gone, dest, NOW, "-Retired", "meshy_nodetest")
    assert rc == 0 and out["ok"] is True and out["retired"] == ["meshy_nodetest"], text
    assert "nothing is expected" in out["note"] and "not reachable" in out["note"], text
    assert _manifests(dest) == sorted(NODETEST), _manifests(dest)                 # nothing deleted


def test_an_unreachable_server_is_no_failure_when_everything_held_is_retired(tmp_path):
    import socket
    dest = _held_only(tmp_path, NODETEST)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rc, out, text = _pull(f"http://127.0.0.1:{port}", dest, NOW, "-Retired", "meshy_nodetest")
    assert rc == 0 and out["ok"] is True and "nothing is expected" in out["note"], text


def test_an_unreadable_source_still_fails_while_a_live_database_is_held(tmp_path):
    dest = _held_only(tmp_path, FORTY + NODETEST)
    rc, out, text = _pull(tmp_path / "no-such-share", dest, NOW, "-Retired", "meshy_nodetest")
    assert rc == 3 and out["ok"] is False, text


def test_an_unreadable_source_still_fails_when_nothing_is_held(tmp_path):
    dest = tmp_path / "hqb-backups"
    dest.mkdir()                     # an empty folder, so the guard itself is what refuses, not a missing path
    rc, out, text = _pull(tmp_path / "no-such-share", dest, NOW, "-Retired", "meshy_nodetest")
    assert rc == 3 and out["ok"] is False, text
