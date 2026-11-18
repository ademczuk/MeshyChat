"""Opt-in large SYNTHETIC local queue acceptance; no server or native transcripts.

Run directly with --mib 778. Creates and removes a private temporary fixture.
Progress reports contain sizes/counts only. This is not a host reboot test.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

import capture_queue as queue
import incremental_queue as iq
import incremental_store as ist
import native_window as nw
import transcript_cards as loader
from test_incremental_queue import line, meta, pair


def verify(mib):
    wanted = mib * 1024 * 1024
    # Include the fixture, output store and generous headroom; never use live roots.
    if shutil.disk_usage(tempfile.gettempdir()).free < wanted * 2 + 256 * 1024 * 1024:
        raise RuntimeError("insufficient synthetic fixture space")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="meshy-synthetic-backlog-") as directory:
        root = Path(directory)
        queue.private_scratch(root)
        source, spool = root / "source", root / "spool"
        source.mkdir()
        path = source / "rollout-backlog.jsonl"
        dialect, metadata = "codex", {"session": "synthetic-session"}
        checkpoint = loader.new_checkpoint(dialect, metadata)
        expected, count = {}, 0
        with path.open("wb") as stream:
            first = meta()
            stream.write(first)
            _, _, checkpoint = loader.read_file([(0, first)], checkpoint, seat="codex-kxxx-cli")
            while stream.tell() < wanted:
                # Padding is synthetic unknown native metadata, never retained text.
                records = []
                stamp = (datetime(2026, 10, 10, 9, tzinfo=timezone.utc) + timedelta(seconds=count)).isoformat()
                for data in pair("retain synthetic backlog constraint " + str(count), stamp):
                    value = json.loads(data)
                    value["synthetic_padding"] = " " * (128 * 1024)
                    padded = line(value)
                    records.append((stream.tell(), padded))
                    stream.write(padded)
                raws, stats, checkpoint = loader.read_file(records, checkpoint, seat="codex-kxxx-cli")
                assert not stats["bad_lines"]
                cards, drops = loader.approved_cards(raws, "codex-kxxx-cli", path.name)
                assert not drops and len(cards) == 1
                expected.update(cards)
                count += 1
        target = path.stat().st_size
        assert target >= wanted and len(expected) == count
        job = queue.enqueue(spool, {"kind": "codex", "seat": "codex-kxxx-cli",
                                    "root": str(source), "files": [path.name]})["job"]
        info = path.stat()
        source_id = iq.opaque([job, path.name])
        generation = iq.opaque([info.st_dev, info.st_ino])
        settings = root / "window.json"
        settings.write_text(json.dumps({"read_bytes": 8 * 1024 * 1024,
                                       "record_bytes": 256 * 1024, "records": 1024, "seconds": 5}))
        budget = nw.Budget(read_bytes=8 * 1024 * 1024)
        observed, fresh, passes, previous, largest_read = 0, 0, 0, 0, 0
        native_read = nw.read_window

        def checked_read(*args, **kwargs):
            nonlocal largest_read
            result = native_read(*args, **kwargs)
            actual_budget = args[4] if len(args) > 4 else kwargs.get("budget", nw.Budget())
            assert result.bytes_read <= 2 * actual_budget.read_bytes + 2
            assert len(result.records) <= actual_budget.records
            largest_read = max(largest_read, result.bytes_read)
            return result

        print(json.dumps({"phase": "fixture_ready", "source_bytes": target, "prompts": count}), flush=True)
        with mock.patch.object(queue, "source_inventory", side_effect=AssertionError("full inventory")), \
                mock.patch.object(queue, "run_bounded_loader", side_effect=AssertionError("full loader")), \
                mock.patch.object(nw, "read_window", side_effect=checked_read):
            while passes < 2048:
                # First pass proves default limits. Periodic fresh actual CLI processes
                # prove the in-memory loop does not own parser/queue progress.
                if passes and passes % 25 == 0:
                    result = subprocess.run([sys.executable, "-B", str(Path(queue.__file__)),
                                             "incremental", "--spool", str(spool), "--window", str(settings)],
                                            capture_output=True, text=True, timeout=30,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    assert result.returncode == 0, "fresh CLI refused synthetic backlog"
                    status = json.loads(result.stdout)[0]
                    fresh += 1
                else:
                    status = iq.stage_incremental(spool, budget=nw.Budget() if passes == 0 else budget)[0]
                    observed += 1
                offset = status["safe_offset"]
                assert previous < offset <= target, "backlog stalled or checkpoint regressed"
                if passes == 0:
                    assert offset <= 1024 * 1024
                    assert status["phase"] == ("pending" if target > 1024 * 1024 else "staged")
                previous, passes = offset, passes + 1
                if passes % 25 == 0:
                    print(json.dumps({"phase": "draining", "passes": passes, "safe_offset": offset}), flush=True)
                if status["phase"] == "staged":
                    break
            else:
                raise AssertionError("synthetic backlog exceeded bounded passes")
        assert not queue.status(spool)[0]["pending"]
        # A fresh reader reopens the durable store and checks every retained page.
        store = ist.Store(spool / "incremental-store")
        snapshot = store.resume(source_id, generation, iq.parser_id())
        assert snapshot.offset == target
        actual, revision, output_rows = {}, 0, 0
        while True:
            page = store.batches(source_id, generation, iq.parser_id(), after_revision=revision)
            for _, output, rejected in page.items:
                assert not rejected
                for data in output.splitlines():
                    card = json.loads(data)
                    # Re-read held records may repeat an identical upsert; variants
                    # or missing content cannot be hidden by counting only IDs.
                    assert card == expected[card["id"]], "backlog card differs from bounded owner reference"
                    actual[card["id"]] = card
                    output_rows += 1
            revision = page.next_revision
            if not page.has_more:
                break
        assert actual == expected
        report = {"phase": "passed", "source_bytes": target, "unique_cards": len(actual),
                  "output_rows": output_rows, "passes": passes, "observed_passes": observed,
                  "fresh_cli_passes": fresh, "largest_observed_native_read": largest_read,
                  "final_offset": snapshot.offset, "revisions": snapshot.revision,
                  "seconds": round(time.monotonic() - started, 3),
                  "graph_recovery": "unverified", "host_reboot": "unverified"}
        print(json.dumps(report), flush=True)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mib", type=int, default=778)
    args = parser.parse_args()
    if not 1 <= args.mib <= 1024:
        parser.error("synthetic size must be between 1 and 1024 MiB")
    verify(args.mib)
