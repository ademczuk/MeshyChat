"""Opt-in synthetic dialogue acceptance probe; no graph, network or installation.

Exercises the real incremental queue in fresh processes. A missing visible tail
is a feature failure (exit 2), even when bounded capture/replay are sound. This
checks the current cards output only; the proposed context output needs its
owner-approved contract before a recovery verdict is possible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import incremental_queue as iq
import incremental_store as ist

HERE = Path(__file__).resolve().parent
STAMP = "2026-10-10T10:20:00.000Z"
PRIVATE = "SYNTHETIC-PRIVATE-REASONING-DO-NOT-GRAPH"
TAIL = {
    "constraint": "Keep the synthetic cache local until the operator approves deployment.",
    "artifact": "The synthetic artifact reference is amber-report.json.",
    "open_task": "OPEN: verify the AMBER-TAIL checksum before closing this task.",
}
VISIBLE = "Synthetic visible progress: " + ("Vienna dialogue. " * 600) + "\n" + "\n".join(TAIL.values())


def encoded(record):
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def invoke(*args):
    child = subprocess.run([sys.executable, "-B", str(HERE / "capture_queue.py"), *args],
                           capture_output=True, text=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if child.returncode:
        raise RuntimeError("synthetic queue operation refused: " + child.stderr)
    return json.loads(child.stdout)


def fixture(kind):
    prompt = "Keep this synthetic deployment task open."
    if kind == "codex":
        prefix = [
            {"type": "session_meta", "timestamp": STAMP, "payload": {
                "id": "synthetic-tail", "source": "cli", "originator": "codex-tui"}},
            {"type": "response_item", "timestamp": STAMP, "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "user_message", "message": prompt}},
            {"type": "response_item", "timestamp": STAMP, "payload": {
                "type": "message", "role": "assistant", "phase": "analysis", "content": [
                    {"type": "output_text", "text": PRIVATE}]}},
        ]
        final = {"type": "response_item", "timestamp": STAMP, "payload": {
            "type": "message", "id": "synthetic-visible-tail", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": VISIBLE}]}}
        return "rollout-synthetic-tail.jsonl", prefix, final
    session = "11111111-2222-3333-4444-555555555555"
    prefix = [{"type": "user", "uuid": "synthetic-user", "parentUuid": None, "sessionId": session,
               "timestamp": STAMP, "message": {"role": "user", "content": prompt}}]
    final = {"type": "assistant", "uuid": "synthetic-visible-tail", "parentUuid": "synthetic-user",
             "sessionId": session, "timestamp": STAMP, "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": PRIVATE}, {"type": "text", "text": VISIBLE}]}}
    return session + ".jsonl", prefix, final


def run_lane(root, kind, seat):
    lane = root / (kind + "-" + seat)
    source = lane / "source"
    source.mkdir(parents=True)
    spool = lane / "spool"
    name, prefix, final = fixture(kind)
    leading = b"".join(encoded(row) for row in prefix)
    record = encoded(final)
    path = source / name
    path.write_bytes(leading + record[:-32])
    config = lane / "source.json"
    config.write_text(json.dumps({"kind": kind, "seat": seat, "root": str(source), "files": [name]}),
                      encoding="utf-8")
    job = invoke("enqueue", "--spool", str(spool), "--source", str(config))["job"]
    pending = invoke("incremental", "--spool", str(spool))[0]
    assert pending["phase"] == "pending" and pending["safe_offset"] <= len(leading), pending
    with path.open("ab") as stream:
        stream.write(record[-32:])
    assert invoke("enqueue", "--spool", str(spool), "--source", str(config))["job"] == job
    complete = invoke("incremental", "--spool", str(spool))[0]
    assert complete["phase"] == "staged" and complete["safe_offset"] == path.stat().st_size, complete
    assert invoke("incremental", "--spool", str(spool)) == []
    info = path.stat()
    store = ist.Store(spool / "incremental-store")
    source_id, generation = iq.opaque([job, name]), iq.opaque([info.st_dev, info.st_ino])
    first = store.batches(source_id, generation, iq.parser_id())
    second = ist.Store(spool / "incremental-store").batches(source_id, generation, iq.parser_id())
    assert first == second and not first.has_more
    cards = b"".join(output for _, output, _ in first.items)
    drops = b"".join(output for _, _, output in first.items)
    assert PRIVATE.encode() not in cards + drops, "private reasoning reached approved output"
    assert b"synthetic deployment task open" in cards, "human prompt missing"
    expectations = {
        "source_file": name, "source_bytes_sha256_local_only": hashlib.sha256(path.read_bytes()).hexdigest(),
        "visible_record_offset": len(leading), "visible_record_end": path.stat().st_size,
        "approved_visible_text_sha256_expected": hashlib.sha256(VISIBLE.encode()).hexdigest(),
        "visible_text_bytes": len(VISIBLE.encode()),
        "tail_first_byte": len(VISIBLE[:VISIBLE.index(TAIL["constraint"])].encode()),
        "required_recovery_text": TAIL,
        "private_reasoning_must_be_excluded": True,
    }
    (lane / "fixture-expectations.json").write_text(json.dumps(expectations, indent=2), encoding="utf-8")
    missing = {field: text.encode() not in cards for field, text in TAIL.items()}
    return {"kind": kind, "seat": seat, "partial_record_held": True, "append_caught_up_in_fresh_cli": True,
            "retained_output_repeatable": True, "private_reasoning_excluded": True,
            "missing_from_current_cards": missing,
            "dialogue_recovery": "FAIL_MISSING_VISIBLE_TAIL" if any(missing.values()) else "NEEDS_CONTEXT_CONTRACT",
            "expectations": str(lane / "fixture-expectations.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for synthetic fixtures and evidence")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    assert VISIBLE.index(TAIL["constraint"]) > 8192
    lanes = [run_lane(args.output, "codex", seat) for seat in (
        "codex-kxxx-cli", "codex-oxxxxxx-admin-cli", "codex-oxxxxxx-cli")]
    lanes.append(run_lane(args.output, "claude", "fixture"))
    result = {"synthetic_only": True, "network_calls": False, "native_histories_read": False,
              "graph_recovery_proved": False, "host_reboot_proved": False,
              "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "lanes": lanes}
    (args.output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 2 if any(row["dialogue_recovery"].startswith("FAIL") for row in lanes) else 0


if __name__ == "__main__":
    sys.exit(main())
