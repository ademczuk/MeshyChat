"""Which backups to keep: 14 daily + 4 weekly, on both HQD and HQB (E02, operator decision
2026-09-25).

    python -m tools.backup prune --dir DIR [--dir DIR ...] [--keep-daily 14] [--keep-weekly 4]
                                 [--now 2026-09-25T12:00:00+00:00] [--dry-run]

THE RULE, per database name, from the UTC stamp tools/backup puts in every file name
(`<database>-YYYYMMDDTHHMMSSZ.manifest.json`, the snapshot's time):

  * daily: the newest backup of each of the `keep_daily` most recent UTC days that HAVE a
    backup;
  * weekly: the newest backup of each of the `keep_weekly` most recent ISO weeks (Monday to
    Sunday) that have a backup OLDER than the oldest kept day;
  * a backup stamped after `now` is kept and not counted, so a clock that jumps cannot push
    real backups out;
  * everything else of that database is deleted: its manifest first, then its dump (a dump
    without a manifest is not a backup to anything that reads this folder).

It counts days that have backups, not calendar days: when backups stop, pruning stops, and the
last 14 + 4 stay however old they get. A file whose name does not parse is never touched.
tools/backup/pull_to_hqb.ps1 implements the same rule for HQB; tests/test_backup_offsite.py
runs both on the same names and requires the same answer.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

KEEP_DAILY = 14
KEEP_WEEKLY = 4
MANIFEST_SUFFIX = ".manifest.json"
_NAME = re.compile(r"^(?P<db>.+)-(?P<stamp>\d{8}T\d{6}Z)\.manifest\.json$")


def parse(name: str) -> tuple[str, dt.datetime] | None:
    m = _NAME.match(name)
    if not m:
        return None
    try:
        ts = dt.datetime.strptime(m.group("stamp"), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return m.group("db"), ts


def plan(names, now: dt.datetime, keep_daily: int = KEEP_DAILY,
         keep_weekly: int = KEEP_WEEKLY) -> dict[str, list[str]]:
    """{"keep", "delete", "future", "ignored"}: manifest file names, each sorted."""
    if now.tzinfo is None:
        raise ValueError("now must carry a timezone")
    if keep_daily < 1 or keep_weekly < 0:
        raise ValueError("keep_daily must be at least 1 and keep_weekly at least 0")
    by_db: dict[str, list[tuple[dt.datetime, str]]] = {}
    ignored: list[str] = []
    for name in set(names):
        parsed = parse(name)
        if parsed is None:
            ignored.append(name)
            continue
        by_db.setdefault(parsed[0], []).append((parsed[1], name))
    keep: set[str] = set()
    future: list[str] = []
    for entries in by_db.values():
        past = sorted((e for e in entries if e[0] <= now), reverse=True)
        for ts, name in entries:
            if ts > now:
                keep.add(name)
                future.append(name)
        days: list[dt.date] = []
        for ts, name in past:
            day = ts.date()
            if day not in days and len(days) < keep_daily:
                days.append(day)
                keep.add(name)
        if not days:
            continue
        oldest = days[-1]                  # added newest first, so the last is the oldest
        weeks: list[dt.date] = []
        for ts, name in past:
            day = ts.date()
            if day < oldest:
                monday = day - dt.timedelta(days=day.weekday())
                if monday not in weeks and len(weeks) < keep_weekly:
                    weeks.append(monday)
                    keep.add(name)
    parsed_names = {n for entries in by_db.values() for _, n in entries}
    return {"keep": sorted(keep), "delete": sorted(parsed_names - keep),
            "future": sorted(future), "ignored": sorted(ignored)}


def prune(folder: Path, now: dt.datetime, keep_daily: int = KEEP_DAILY,
          keep_weekly: int = KEEP_WEEKLY, dry_run: bool = False) -> dict:
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"no such directory: {folder}")
    names = [p.name for p in folder.iterdir() if p.is_file() and p.name.endswith(MANIFEST_SUFFIX)]
    p = plan(names, now, keep_daily, keep_weekly)
    if not dry_run:
        for name in p["delete"]:
            (folder / name).unlink()
            (folder / (name[:-len(MANIFEST_SUFFIX)] + ".dump")).unlink(missing_ok=True)
    return {"dir": str(folder), "dry_run": dry_run, "keep": p["keep"], "delete": p["delete"],
            "future": p["future"], "ignored": p["ignored"]}
