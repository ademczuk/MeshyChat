"""Synthetic context-contract corpus and role-aware recovery oracle.

This does not parse transcripts, screen text, upload records or prove graph
access. The existing loader/native adapter must produce the output; a reviewed
test bridge translates its schema into the observations described below.
The default policy is today's unscreened shared/transcript-context namespace.
No synthetic secret/name marker in this file is a real credential or client.
Plan-state fixtures are acceptance expectations, not proof of parser recovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

STAMP = "2026-10-10T11:00:00Z"
PRIVATE = "SYNTHETIC-PRIVATE-ANALYSIS-MUST-NOT-BE-GRAPHED"
NAMESPACE = "shared/transcript-context"
CLIENT = "SYNTHETIC-OPERATOR-CLIENT-ALPHA"
TAIL = "\nConstraint: keep the cache local. Artifact: amber-report.json. OPEN: verify AMBER-TAIL."
VISIBLE = "Visible progress: " + "Vienna \U0001f9ed caf\u00e9. " * 900 + TAIL


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(kind, payload):
    return {"type": kind, "timestamp": STAMP, "payload": payload}


def assistant(item, text, phase="final_answer"):
    return record("response_item", {"type": "message", "id": item,
                  "role": "assistant", "phase": phase,
                  "content": [{"type": "output_text", "text": text}]})


def user(identity, text):
    return record("response_item", {"type": "message", "id": identity,
                  "role": "user", "content": [{"type": "input_text", "text": text}]})


def plan(call, steps):
    return record("response_item", {"type": "function_call", "call_id": call,
                  "name": "update_plan", "arguments": json.dumps({"plan": [
                      {"step": text, "status": status} for text, status in steps]})})


def meta(session, *, subagent=False):
    return record("session_meta", {"id": session, "originator": "codex-tui",
                  "source": {"subagent": {"thread_spawn": {"parent_thread_id": "synthetic-parent"}}}
                  if subagent else "cli"})


def item(identity, text, role="assistant"):
    return {"id": identity, "role": role, "text": text,
            "text_sha256": digest(text), "omitted_bytes": 0}


def corpus(namespace=NAMESPACE):
    """Expected outcomes are explicit owner decisions, not a second parser."""
    text = "Visible commentary: retain the deployment decision."
    counts = [
        {"A": {"pending": 2, "in_progress": 0, "completed": 0}},
        {"A": {"pending": 1, "in_progress": 0, "completed": 1}},
        {"A": {"pending": 1, "in_progress": 0, "completed": 1}},
        {"A": {"pending": 0, "in_progress": 0, "completed": 1}},
        {"B": {"pending": 1, "in_progress": 0, "completed": 0}},
    ]
    plan_rows = [
        plan("plan-initial", [("A", "pending"), ("A", "pending")]),
        plan("plan-complete-one", [("A", "completed"), ("A", "pending")]),
        plan("plan-reorder", [("A", "pending"), ("A", "completed")]),
        plan("plan-remove", [("A", "completed")]),
        plan("plan-reword", [("B", "pending")]),
    ]
    states = [{"call_id": row["payload"]["call_id"], "counts": count,
               "new_completions": 1 if index == 1 else 0,
               "ambiguous": False, "unresolved_steps": ["A", "B"] if index == 4 else ["A"]}
              for index, (row, count) in enumerate(zip(plan_rows, counts))]
    ambiguous = [plan("ambiguous-initial", [("A", "pending"), ("A", "pending")]),
                 plan("ambiguous-change", [("A", "completed")])]
    cases = [
        {"case": "codex-visible-long-unicode", "records": [meta("synthetic-visible"),
            assistant("private-analysis", PRIVATE, "analysis"),
            assistant("visible-commentary", text, "commentary"),
            assistant("visible-long", VISIBLE),
            record("event_msg", {"type": "agent_message", "message": VISIBLE})],
         "items": [item("visible-commentary", text), item("visible-long", VISIBLE)], "plan_states": []},
        {"case": "codex-plan-duplicates-reorder-removal-reword", "records":
            [meta("synthetic-plan")] + plan_rows, "items": [], "plan_states": states},
        {"case": "codex-plan-ambiguous-removal-completion", "records":
            [meta("synthetic-ambiguous")] + ambiguous, "items": [], "plan_states": [
                {"call_id": "ambiguous-initial", "counts": counts[0], "new_completions": 0,
                 "ambiguous": False, "unresolved_steps": ["A"]},
                {"call_id": "ambiguous-change", "counts": counts[3], "new_completions": 0,
                 "ambiguous": True, "unresolved_steps": ["A"]}]},
        {"case": "codex-client-name-in-long-tail", "records": [meta("synthetic-name"),
            assistant("blocked-name", VISIBLE + "\n" + CLIENT)], "items": [], "plan_states": []},
        {"case": "codex-secret-marker-in-long-tail", "records": [meta("synthetic-secret"),
            assistant("blocked-secret", VISIBLE + "\n-----BEGIN PRIVATE KEY-----")], "items": [], "plan_states": []},
        {"case": "codex-subagent-prose-and-plan", "records": [meta("synthetic-child", subagent=True),
            assistant("child-prose", VISIBLE), plan("child-plan", [("child work", "pending")])],
         "items": [], "plan_states": []},
        {"case": "codex-user-and-assistant-authority", "records": [meta("synthetic-dialogue"),
            user("operator-long", VISIBLE), assistant("assistant-repeats-operator", VISIBLE)],
         "items": [item("operator-long", VISIBLE, "user"),
                   item("assistant-repeats-operator", VISIBLE)], "plan_states": []},
        {"case": "codex-user-client-name-in-long-tail", "records": [meta("synthetic-user-name"),
            user("blocked-user-name", VISIBLE + "\n" + CLIENT)], "items": [], "plan_states": []},
        {"case": "codex-user-secret-marker-in-long-tail", "records": [meta("synthetic-user-secret"),
            user("blocked-user-secret", VISIBLE + "\n-----BEGIN PRIVATE KEY-----")],
         "items": [], "plan_states": []},
    ]
    # Today's automatic transcript-context namespace keeps text as written.
    # The historical blocked-name/secret corpus applies only to screened output;
    # it must never silently reinstate an obsolete admission policy.
    if namespace == NAMESPACE:
        for case, identity, tail, role in [
            (cases[3], "blocked-name", "\n" + CLIENT, "assistant"),
            (cases[4], "blocked-secret", "\n-----BEGIN PRIVATE KEY-----", "assistant"),
            (cases[7], "blocked-user-name", "\n" + CLIENT, "user"),
            (cases[8], "blocked-user-secret", "\n-----BEGIN PRIVATE KEY-----", "user"),
        ]:
            case["items"] = [item(identity, VISIBLE + tail, role)]
    for case in cases:
        case["namespace"] = namespace
    return cases



def write_corpus(target):
    target.mkdir(parents=True, exist_ok=False)
    cases = corpus()
    manifests = []
    for case in cases:
        source = target / (case["case"] + ".jsonl")
        encoded = [(json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8") for row in case["records"]]
        source.write_bytes(b"".join(encoded))
        offsets, end = [], 0
        for row in encoded:
            offsets.append(end)
            end += len(row)
        # Native windows must hold an incomplete UTF-8/JSON record. Try every
        # byte in the final record in addition to boundaries between records.
        expected = {key: value for key, value in case.items() if key != "records"}
        expected.update({"source_file": source.name, "record_offsets": offsets,
                         "record_ends": offsets[1:] + [end], "source_bytes": end,
                         "split_last_record": {"start": offsets[-1], "end_exclusive": end},
                         "lifecycle_checks": ["fresh_process_between_windows", "partial_record_restart"],
                         "allowed_artifact_only_subagent": case["case"].endswith("subagent-prose-and-plan")})
        manifests.append(expected)
    manifest = {"format": "context-acceptance-fixtures.v1", "synthetic_only": True,
                "graph_recovery_proved": False, "host_reboot_proved": False,
                "namespace": NAMESPACE, "operator_client_names_for_test_only": [CLIENT], "cases": manifests,
                "policy": "Current transcript-context keeps text as written; this oracle writes no graph data"}
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def verify(expected, observation):
    """Check a TEST BRIDGE observation; no authority/signature claims.

    Parts: {id, role: user|assistant, part: 1-based integer, parts: integer, text,
            text_sha256: SHA256 of complete UTF-8 text,
            omitted_bytes: 0}. Plan states match the manifest's ordered states.
    These bridge fields are deliberately not a proposed production schema.
    """
    errors = []
    if observation.get("namespace") != expected["namespace"]:
        errors.append("namespace_mismatch")
    if observation.get("case") != expected["case"]:
        errors.append("case_mismatch")
    wanted = {row["id"]: row for row in expected["items"]}
    groups = {}
    rows = observation.get("items")
    if not isinstance(rows, list) or len(rows) > 4096:
        return ["invalid_or_unbounded_items"]
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in wanted:
            errors.append("unexpected_item")
            continue
        groups.setdefault(row["id"], []).append(row)
    if set(groups) != set(wanted):
        errors.append("missing_item")
    for identity, parts in groups.items():
        expected_item = wanted[identity]
        count = parts[0].get("parts")
        if type(count) is not int or not 1 <= count <= 4096 or len(parts) != count:
            errors.append("incomplete_parts")
            continue
        valid = all(type(row.get("part")) is int and 1 <= row["part"] <= count
                    and type(row.get("parts")) is int and row["parts"] == count
                    and isinstance(row.get("text"), str)
                    and row.get("role") == expected_item["role"]
                    and len(row["text"].encode("utf-8")) <= 8192
                    and row.get("text_sha256") == expected_item["text_sha256"]
                    and type(row.get("omitted_bytes")) is int and row["omitted_bytes"] == 0
                    for row in parts)
        if not valid or {row.get("part") for row in parts} != set(range(1, count + 1)):
            errors.append("invalid_parts_digest_or_omission")
            continue
        joined = "".join(row["text"] for row in sorted(parts, key=lambda row: row["part"]))
        if joined != expected_item["text"] or digest(joined) != expected_item["text_sha256"]:
            errors.append("approved_text_mismatch")
    # Python equality considers True == 1; canonical JSON keeps task counters
    # distinct from flags and also preserves the ordered change sequence.
    if json.dumps(observation.get("plan_states"), sort_keys=True) != json.dumps(expected["plan_states"], sort_keys=True):
        errors.append("plan_state_mismatch")
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--observations", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "generate":
        manifest = write_corpus(args.output)
        print(json.dumps({"synthetic_only": True, "cases": len(manifest["cases"]), "graph_recovery_proved": False}))
        return 0
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    observations = json.loads(args.observations.read_text(encoding="utf-8"))
    if not isinstance(observations, list) or len(observations) != len(manifest["cases"]):
        print(json.dumps({"synthetic_only": True, "error": "missing_or_duplicate_cases"}))
        return 2
    cases = {row["case"]: row for row in manifest["cases"]}
    if {row.get("case") for row in observations} != set(cases):
        print(json.dumps({"synthetic_only": True, "error": "case_set_mismatch"}))
        return 2
    results = [{"case": row["case"], "errors": verify(cases[row["case"]], row)} for row in observations]
    print(json.dumps({"synthetic_only": True, "graph_recovery_proved": False,
                      "host_reboot_proved": False, "results": results}, indent=2))
    return 2 if any(row["errors"] for row in results) else 0


if __name__ == "__main__":
    sys.exit(main())
