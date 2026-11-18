"""Mutation check for the context.v1 validator: each mutant removes one guard, and tests/test_context_write.py must FAIL.

Run from the repository root:  python tests/context_mutants.py   (MESHY_TEMPLATE_DSN set; MUTANTS_ONLY=M3,M7 for a subset)
Each file is patched in place and restored in a finally block; the restore is checked by hash at the end.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

V = "apps/context/validation.py"
S = "apps/context/screens.py"
R = "apps/memoryskills/redact.py"
H = "apps/memoryskills/handlers.py"
E = "apps/memoryskills/errors.py"
M = {
    "M1 screens run before the permission check": (V, "    if item in TEXT_ITEMS:\n",
                                                    "    screens.screen(text)\n    if item in TEXT_ITEMS:\n"),
    "M2 write-once removed": (V, "    if row is not None:\n        _refuse(f\"{key} exists",
                              "    if False:\n        _refuse(f\"{key} exists"),
    "M3 seat check removed": (V, "    if seat != caller_label:\n", "    if False:\n"),
    "M4 digest unchecked": (V, "hexdigest() != digest:", "hexdigest() != digest and False:"),
    "M5 budget doubled": (V, "if len(_serialized(value)) > MAX_VALUE_BYTES:", "if len(_serialized(value)) > 2 * MAX_VALUE_BYTES:"),
    "HZ predecessor not required": (V, "            if prev is None:\n", "            if False:\n"),
    "M7 gaps between batches allowed": (V, "first != prev_last + 1", "first < prev_last + 1"),
    "M8 counts not tallied": (V, "    if tally != value[\"counts\"]:\n", "    if False:\n"),
    "M9 omitted may carry text": (V, "        extra = fields & {", "        extra = set() & {"),
    "M10 a screened omission keeps a size": (V, "        elif \"omitted_bytes\" in fields:\n", "        elif False:\n"),
    "M11 agent_id anywhere": (V, "        if item != \"artifact\":\n", "        if False:\n"),
    "M12 counts on any task": (V, "        elif \"counts\" in fields:\n", "        elif False:\n"),
    "M13 step not screened": (V, "SCREENED_FIELDS = (\"text\", \"step\")", "SCREENED_FIELDS = (\"text\",)"),
    "M23 tags allowed": (H, "        if args.get(\"tags\") is not None:\n",
                         "        if False:\n"),
    "M24 native_ref may be any string": (V, "_shaped(value[\"native_ref\"], \"native_ref\", OPAQUE_RE,", "_shaped(value[\"native_ref\"], \"native_ref\", re.compile(\".+\"),"),
    "M25 adapter_version may be any string": (V, "\"adapter_version\", VERSION_RE,", "\"adapter_version\", re.compile(\".+\"),"),
    "M26 agent_id may be any string": (V, "_shaped(value[\"agent_id\"], \"agent_id\", OPAQUE_RE,", "_shaped(value[\"agent_id\"], \"agent_id\", re.compile(\".+\"),"),
    "M27 no per-source lock": (V, "    cur.execute(\"SELECT pg_advisory_xact_lock(", "    return\n    cur.execute(\"SELECT pg_advisory_xact_lock("),
    "M28 no head check at memory.write's read": (H, "                context_rules.refuse_existing_head(key, head)\n",
                                                "                pass\n"),
    "M29 a missing predecessor reported as final": (V, "\"E_CONTEXT_MANIFEST_PREDECESSOR\")", "\"E_CONTEXT_MANIFEST_ORDER\")"),
    "M30 a missing item reported as final": (V, "\"E_CONTEXT_MANIFEST_ITEM_MISSING\")", "\"E_CONTEXT_MANIFEST\")"),
    "M31 retryable codes unregistered": (E,
                                         "E_CONTEXT_MANIFEST_ITEM_MISSING = \"E_CONTEXT_MANIFEST_ITEM_MISSING\"\n", ""),
    "M32 predecessor code unregistered": (E,
                                          "E_CONTEXT_MANIFEST_PREDECESSOR = \"E_CONTEXT_MANIFEST_PREDECESSOR\"\n", ""),
    "M14 no normalisation": (S, "if ch.isalnum())", "if True)"),
    "M15 an empty denylist screens nothing": (S, "if not isinstance(raw, list) or not raw or", "if not isinstance(raw, list) or"),
    "M16 one-letter markers allowed": (S, "if any(len(m) < MIN_MARKER_CHARS for m in markers):", "if any(len(m) < 1 for m in markers):"),
    "M17 time may run ahead": (V, "    if t > now + MAX_AHEAD:\n", "    if False:\n"),
    "M18 redaction not refused": (R, "    if _context.is_context_namespace(namespace):\n", "    if False:\n"),
    "M19 manifest digest unchecked": (V, "            if digest != stored.get(\"text_sha256\"):\n", "            if False:\n"),
    "M20 unknown fields allowed": (V, "    if unknown:\n", "    if False:\n"),
    "M21 redacted writable": (V, "    if \"redacted\" in value:\n", "    if False:\n"),
    "M22 batch 1 may start anywhere": (V, "    if batch_parts[3] == 1 and first != 0:\n", "    if False:\n"),
}


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    only = {x.strip() for x in os.environ.get("MUTANTS_ONLY", "").split(",") if x.strip()}
    before = {p: sha(p) for p in (V, S, R, H, E)}
    survived, bad = [], []
    for name, (path, a, b) in M.items():
        if only and name.split()[0] not in only:
            continue
        src = open(path, encoding="utf-8").read()
        if src.count(a) != 1:
            bad.append(name)
            print("BAD ANCHOR", name, src.count(a))
            continue
        try:
            open(path, "w", encoding="utf-8", newline="").write(src.replace(a, b))
            r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_context_write.py", "-q", "-x",
                                "-p", "no:cacheprovider"], capture_output=True, text=True, timeout=900)
        finally:
            open(path, "w", encoding="utf-8", newline="").write(src)
        # Only pytest's "tests failed" (1) is a kill. 0 is a survivor; anything else (collection error, crash,
        # timeout) is the runner failing, never read as either.
        if r.returncode == 1:
            print("KILLED   " + name)
        elif r.returncode == 0:
            print("SURVIVED " + name)
            survived.append(name)
        else:
            print(f"RUNNER-FAILED (exit {r.returncode}) " + name)
            bad.append(name)
    after = {p: sha(p) for p in (V, S, R, H, E)}
    assert after == before, "a file was not restored"
    print(f"survived {len(survived)}, bad anchors {len(bad)}; files restored")
    return 1 if survived or bad else 0


if __name__ == "__main__":
    sys.exit(main())
