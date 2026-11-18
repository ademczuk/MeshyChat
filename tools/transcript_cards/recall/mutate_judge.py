"""Each mutant of judge.py must make `judge.py --selftest` exit nonzero with a FAIL line. Works on a copy."""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "judge.py"), encoding="utf-8").read()
MUTANTS = {
    "order check removed": ('        reasons.append(f"superseded card above the active one, at rank {order}")', "        pass"),
    "claim check removed": ('        reasons.append(f"Claim above the active card, at rank {claim_above}")', "        pass"),
    "status check removed": ('        reasons.append(f"matched card with the wrong status, at rank {sorted(set(wrong_status))}")',
                             "        pass"),
    "k ignored": ("if active_rank is None or active_rank > k:", "if active_rank is None:"),
    "seat ignored in a match": ('for k in ("type", "seat"))', 'for k in ("type",))'),
    "date ignored in a match": ('    elif when.date().isoformat() != str(expected["date"]):', "    elif False:"),
    "labelled time ignored": ('        if abs((when - _when(expected["at"])).total_seconds()) > AT_TOLERANCE_S:',
                              "        if False:"),
    "the tolerance is one-sided": ('        if abs((when - _when(expected["at"])).total_seconds()) > AT_TOLERANCE_S:',
                                   '        if (when - _when(expected["at"])).total_seconds() > AT_TOLERANCE_S:'),
    "the tolerance is zero": ("AT_TOLERANCE_S = 5\n", "AT_TOLERANCE_S = 0\n"),
    "the tolerance is six seconds": ("AT_TOLERANCE_S = 5\n", "AT_TOLERANCE_S = 6\n"),
    "a chain the phrases cannot separate is accepted": (
        "            if _inside(words_a, words_b) or _inside(words_b, words_a):", "            if False:"),
    "a near pair is refused on timing alone": (
        "            if _inside(words_a, words_b) or _inside(words_b, words_a):", "            if True:"),
    "only identical phrases count as inseparable": (
        "            if _inside(words_a, words_b) or _inside(words_b, words_a):", "            if words_a == words_b:"),
    "the chain check uses the tolerance once, not twice": (
        "abs((at_b - at_a).total_seconds()) > 2 * AT_TOLERANCE_S:", "abs((at_b - at_a).total_seconds()) > AT_TOLERANCE_S:"),
    "the chain check reaches past twice the tolerance": (
        "abs((at_b - at_a).total_seconds()) > 2 * AT_TOLERANCE_S:", "abs((at_b - at_a).total_seconds()) > 4 * AT_TOLERANCE_S:"),
    "the chain check ignores the seat": ("            if (type_a, seat_a) != (type_b, seat_b) or abs(", "            if type_a != type_b or abs("),
    "a hook card with a transcript record is called a hook clock": (
        '    if source.get("transcript_uuid"):\n        return "record"', '    if False:\n        return "record"'),
    "every card without a record is called a hook clock": (
        '    return "hook_clock" if str(source.get("kind", "")).endswith("_hook") else "unknown"', '    return "hook_clock"'),
    "a tolerated match on a record time is not counted": (
        'sum(1 for t, src in zip(tolerated, sources) if t and src == "record")}', "0}"),
    "every tolerated match is counted as on a record time": (
        'sum(1 for t, src in zip(tolerated, sources) if t and src == "record")}', "sum(tolerated)}"),
    "the offset has the wrong sign": ('    return round((_when(expected["at"]) - when).total_seconds() * 1000)',
                                      '    return round((when - _when(expected["at"])).total_seconds() * 1000)'),
    "an exact match is counted as tolerated": ('    return when.replace(microsecond=0) != _when(expected["at"]).replace(microsecond=0)',
                                               "    return True"),
    "a match in another second is not counted as tolerated": (
        '    return when.replace(microsecond=0) != _when(expected["at"]).replace(microsecond=0)',
        '    return abs((when - _when(expected["at"])).total_seconds()) >= 1'),
    "no match is counted as tolerated": ('            "within_tolerance": sum(tolerated),', '            "within_tolerance": 0,'),
    "tolerated matches are never counted": ('"matched_within_tolerance": sum(p["within_tolerance"] for p in per),',
                                            '"matched_within_tolerance": 0,'),
    "the largest offset is not reported": ('max((abs(o) for p in per for o in p["at_offsets_ms"] if o is not None), default=0),', "0,"),
    "a card with no time matches": ("    if when is None:\n        return False", "    if when is None:\n        return True"),
    "card time not read in UTC": (
        "    return when.astimezone(_dt.timezone.utc) if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)",
        "    return when if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)"),
    "chain order by time unchecked": (
        '    if len(times) == len(entry["expect"]) and any(b < a for a, b in zip(times, times[1:])):', "    if False:"),
    "equal times in a chain are refused": (
        '    if len(times) == len(entry["expect"]) and any(b < a for a, b in zip(times, times[1:])):',
        '    if len(times) == len(entry["expect"]) and any(b <= a for a, b in zip(times, times[1:])):'),
    "a time off its date accepted": ('            if when is None or when.date().isoformat() != str(card["date"]):',
                                     "            if when is None:"),
    "a date that does not exist accepted": (' or _when(card["date"]) is None:', ":"),
    "phrases ignored in a match": ('return all(_norm(phrase) in text for phrase in expected["must_contain"])', "return True"),
    "duplicates never counted": ("duplicates += max(0, len(hits) - 1)", "duplicates += 0"),
    "strict duplicates ignored": ("if strict_duplicates and duplicates:", "if False:"),
    "two active cards accepted": ('if sum(card["status"] == "active" for card in entry["expect"]) != 1:',
                                  'if sum(card["status"] == "active" for card in entry["expect"]) < 1:'),
    "a missing result passes": ('per.append({"id": entry["id"], "passed": False, "reasons": ["no result for this query"],',
                                'per.append({"id": entry["id"], "passed": True, "reasons": ["no result for this query"],'),
    "mrr counts a miss as rank 1": ("sum(1 / r for r in found) / n", "sum(1 for r in found) / n"),
    "the overruled mark is ignored": (
        '    return "overruled" if status == "active" and card.get("overruled_by") else status', "    return status"),
    "the mark turns a superseded card into an overruled one": (
        '    return "overruled" if status == "active" and card.get("overruled_by") else status',
        '    return "overruled" if card.get("overruled_by") else status'),
    "an overruled card above the active one fails": ('    order = [r for status, r in above if status == "superseded"]',
                                                    "    order = [r for status, r in above]"),
    "an overruled card above the active one is not counted": (
        '    overruled_above = [r for status, r in above if status == "overruled"]', "    overruled_above = []"),
    "a superseded card above the active one is counted as overruled": (
        '    overruled_above = [r for status, r in above if status == "overruled"]',
        "    overruled_above = [r for status, r in above]"),
    "an unknown status accepted": ('        if card["status"] not in ("active", "superseded", "overruled"):', "        if False:"),
    # pxx's and exxxx-session's reviews of #213
    "an empty phrase accepted": ("                or any(not isinstance(x, str) or not _norm(x) for x in phrases)):", "                ):"),
    "a bare string accepted as phrases": ("if (not isinstance(phrases, list) or not 1", "if (False or not 1"),
    "any number of phrases accepted": ("not 1 <= len(phrases) <= MAX_PHRASES", "not 1 <= len(phrases)"),
    "a gold file with one id twice accepted": ('        if entry["id"] in seen:', "        if False:"),
    "an expected card that is not an object accepted": ("        if not isinstance(card, dict):", "        if False:"),
    "a label that is not an object accepted": ("    if not isinstance(entry, dict):", "    if False:"),
    "a date with a time part accepted": ('if not _DATE.fullmatch(str(card["date"])) or ', "if "),
    "an empty kind accepted": ('    if "kind" in entry and (', "    if False and ("),
    "failures not counted by reason": ('"by_reason": _count(code for p in per for code in p["reason_codes"]),', '"by_reason": {},'),
    "the seat counts take the first card, not the active one": ('"seat": entry["expect"][active_index]["seat"]',
                                                                '"seat": entry["expect"][0]["seat"]'),
    "a passing query is not counted as passed": ('row["passed"] += bool(p["passed"])', 'row["passed"] += 0'),
    "a stated overruled status is taken at its word": ('        return "overruled (stated, no mark)"', "        return status"),
    "a results file with one id twice keeps the last row": ('        if row.get("id") in out:', "        if False:"),
    "a results row without a cards list accepted": ('if not isinstance(row, dict) or not isinstance(row.get("cards"), list):',
                                                     "if not isinstance(row, dict):"),
    "a rank beyond k is reported as missing": ('codes.append("active_missing" if active_rank is None else "active_beyond_k")',
                                               'codes.append("active_missing")'),
}
bad = 0
tmp = tempfile.mkdtemp(prefix="judge-mut-")
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
try:
    for name, (old, new) in MUTANTS.items():
        assert SRC.count(old) == 1, (name, SRC.count(old))
        path = os.path.join(tmp, "judge.py")
        open(path, "w", encoding="utf-8").write(SRC.replace(old, new))
        r = subprocess.run([sys.executable, path, "--selftest"], capture_output=True, text=True, env=env)
        fails = [line for line in r.stdout.splitlines() if line.startswith("FAIL")]
        crashed = "Traceback" in r.stderr
        verdict = "KILLED" if r.returncode != 0 and fails and not crashed else ("CRASH" if crashed else "SURVIVED")
        print(f"{name}: {verdict} | {fails[0][:90] if fails else r.stderr.strip()[-90:]}")
        bad += verdict != "KILLED"
finally:
    shutil.rmtree(tmp, ignore_errors=True)
r = subprocess.run([sys.executable, os.path.join(HERE, "judge.py"), "--selftest"], capture_output=True, text=True, env=env)
print("original selftest rc", r.returncode)
print("mutants", len(MUTANTS))
sys.exit(1 if bad or r.returncode else 0)
