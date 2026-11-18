"""Judge a transcript-graph retrieval run against labelled queries. Standard library only.

    python judge.py --gold gold.jsonl --results results.jsonl [--k 5]
    python judge.py --selftest

GOLD, one JSON object per line:
    {"id": "bxxxxx-001", "query": "...", "labelled_by": "memory-bxxxxx",
     "expect": [ {card}, {card}, ... ]}          oldest first; exactly one card has status "active"
  status is "active", "superseded" (a later card of the SAME seat replaced it; the server hides it from the
  default read) or "overruled" (only a later ruling of ANOTHER seat names it; the server keeps it in the default
  read and marks it with `overruled_by`).
  an expected card:
    {"type": "Ruling", "seat": "pxx", "date": "2026-10-08", "at": "2026-10-08T07:32:13Z",
     "status": "superseded", "must_contain": ["Wait for the patch"]}
  `seat` is the seat whose own transcript holds a recorded decision record. `date` is the UTC date of the answer.
  `at` is optional: the answer's own time. Give it whenever a recorded decision record is at hand, because a day cannot
  order two rulings made on it, and when every card of a chain has one the chain must be oldest first by it.
  `must_contain` are the operator's own words, matched without regard to case or runs of whitespace.

RESULTS, one JSON object per line, cards in ranked order, best first:
    {"id": "bxxxxx-001", "cards": [ {"type": ..., "owner": ..., "seat": ..., "date": ..., "status": ...,
                                     "text": ..., "source": {...}}, ... ]}

A returned card MATCHES an expected card when type and seat are equal, every must_contain phrase is in its text,
and its time (the card's `at`, `time` or `date` field, read as UTC) is within AT_TOLERANCE_S seconds of the
label's `at`, or falls on the label's `date` when no `at` is given. (A card the question hook writes carries the
hook's clock, the label's `at` comes from the transcript record, and the two are one answer.) Every match reports
its offset, and `matched_within_tolerance` counts the matches the old to-the-second rule would have missed. Per query the judge reports, and FAILS the query on any of the first four:
    active_rank     the rank of the active expected card; missing or beyond k fails
    order           a superseded expected card ranked above the active one fails
    status          a matched card whose status differs from the label fails (a superseded ruling served as
                    active, or an overruled one served without its `overruled_by` mark)
    overruled_above an overruled expected card ranked above the active one: counted and reported, never failed,
                    because the server shows both and the mark tells the reader which one stands
    claim_above     a card of type Claim ranked above the active card fails (a model's summary outranks the ruling)
    duplicates      returned cards beyond the first that match one expected card (a relay minted as a second
                    Ruling); counted and reported, and failed when --strict-duplicates is given
Exit code: 0 every query passed, 1 at least one failed, 2 the inputs could not be read or a label is malformed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys

_SPACE = re.compile(r"\s+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
CARD_KEYS = ("type", "seat", "date", "status", "must_contain")
#: How far a card's time may lie from a label's `at`, either side, and still be that answer. ONE constant: the
#: label check below refuses two cards of one seat and type that this distance could confuse.
AT_TOLERANCE_S = 5
#: A label names one to this many phrases. Measured on the real labels when written: 50 cards with one, 1 with two.
MAX_PHRASES = 3


def _norm(text: object) -> str:
    return _SPACE.sub(" ", str(text or "")).strip().casefold()


def _when(value: object) -> _dt.datetime | None:
    """An ISO date or time as UTC, or None. A value without an offset is UTC."""
    try:
        when = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.astimezone(_dt.timezone.utc) if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)


def card_time(card: dict) -> _dt.datetime | None:
    """When the operator answered, from whichever field the loader used for it."""
    return next((t for t in (_when(card.get(k)) for k in ("at", "time", "date") if card.get(k)) if t), None)


def card_status(card: dict) -> str:
    """The status a reader sees. A card that only ANOTHER seat's later ruling names stays "active" on the server
    and carries a non-empty `overruled_by`; the judge calls that "overruled". The server never sends a literal status
    "overruled", so a card that says so without the mark is not taken at its word (codex-kxxx-cli, review of #213):
    results carry the server's own status, and the mark is what makes a card overruled."""
    status = str(card.get("status", "")).casefold()
    if status == "overruled":
        return "overruled (stated, no mark)"
    return "overruled" if status == "active" and card.get("overruled_by") else status


def at_offset_ms(card: dict, expected: dict) -> int | None:
    """Label `at` minus the card's time, signed, in milliseconds. None when the label gives no `at` or the card
    has no time."""
    when = card_time(card)
    if when is None or not expected.get("at"):
        return None
    return round((_when(expected["at"]) - when).total_seconds() * 1000)


def needed_tolerance(card: dict, expected: dict) -> bool:
    """True when this card matches the label's `at` only because of the tolerance: the two times are not in one
    second, which is what the rule before the tolerance asked for."""
    when = card_time(card)
    if when is None or not expected.get("at"):
        return False
    return when.replace(microsecond=0) != _when(expected["at"]).replace(microsecond=0)


def matches(card: dict, expected: dict) -> bool:
    """Type and seat equal, every phrase in the text, and the time: within AT_TOLERANCE_S seconds when the label
    gives `at` (two rulings of one seat on one day differ only there and in their words, as pxx's two of 8 Oct
    do), else the UTC date."""
    if any(str(card.get(k, "")).casefold() != str(expected[k]).casefold() for k in ("type", "seat")):
        return False
    when = card_time(card)
    if when is None:
        return False
    if expected.get("at"):
        if abs((when - _when(expected["at"])).total_seconds()) > AT_TOLERANCE_S:
            return False
    elif when.date().isoformat() != str(expected["date"]):
        return False
    text = _norm(card.get("text"))
    return all(_norm(phrase) in text for phrase in expected["must_contain"])


def _inside(inner: list[str], outer: list[str]) -> bool:
    """Every phrase of `inner` is contained in some phrase of `outer`: a text that holds all of `outer` holds all of
    `inner`."""
    return all(any(a in b for b in outer) for a in inner)


def time_source(card: dict) -> str:
    """Where a card's time comes from (kxxx, msg/<key>): "record" when the card carries the
    transcript record it was read from, "hook_clock" for a card the question hook wrote without one, else
    "unknown". A match that needs the tolerance is expected on a hook clock and not on a record time."""
    source = card.get("source") if isinstance(card.get("source"), dict) else {}
    if source.get("transcript_uuid"):
        return "record"
    return "hook_clock" if str(source.get("kind", "")).endswith("_hook") else "unknown"


def check_label(entry: dict) -> None:
    """Raise ValueError on a label the judge cannot score honestly."""
    if not isinstance(entry, dict):
        raise ValueError(f"a label must be an object, not {type(entry).__name__}")
    if "kind" in entry and (not isinstance(entry["kind"], str) or not entry["kind"].strip()):
        raise ValueError(f"label {entry.get('id')!r}: kind, when given, must be a non-empty string")
    for key in ("id", "query", "expect"):
        if not entry.get(key):
            raise ValueError(f"label {entry.get('id')!r}: missing {key}")
    for card in entry["expect"]:
        if not isinstance(card, dict):
            raise ValueError(f"label {entry['id']}: an expected card must be an object, not {type(card).__name__}")
        missing = [k for k in CARD_KEYS if not card.get(k)]
        if missing:
            raise ValueError(f"label {entry['id']}: an expected card lacks {missing}")
        if card["status"] not in ("active", "superseded", "overruled"):
            raise ValueError(f"label {entry['id']}: status {card['status']!r}")
        # A phrase that normalizes to nothing is inside every text, so the card would match ANY card of that seat
        # and date (pxx, review of #213). A bare string would be read one character at a time.
        phrases = card["must_contain"]
        if (not isinstance(phrases, list) or not 1 <= len(phrases) <= MAX_PHRASES
                or any(not isinstance(x, str) or not _norm(x) for x in phrases)):
            raise ValueError(f"label {entry['id']}: must_contain must be 1 to {MAX_PHRASES} non-empty phrases")
    if sum(card["status"] == "active" for card in entry["expect"]) != 1:
        raise ValueError(f"label {entry['id']}: exactly one expected card must be active")
    times = []
    for card in entry["expect"]:
        if not _DATE.fullmatch(str(card["date"])) or _when(card["date"]) is None:
            raise ValueError(f"label {entry['id']}: date {card['date']!r} is not a YYYY-MM-DD date")
        if card.get("at"):
            when = _when(card["at"])
            if when is None or when.date().isoformat() != str(card["date"]):
                raise ValueError(f"label {entry['id']}: at {card['at']!r} is not a time on {card['date']}")
            times.append(when)
    # Equal times may stand: the answers of one question call share one timestamp (uxxx: 51 of 108 Rulings on one
    # seat). Time cannot order those; their phrases tell them apart, which the check below insists on.
    if len(times) == len(entry["expect"]) and any(b < a for a, b in zip(times, times[1:])):
        raise ValueError(f"label {entry['id']}: the chain is not oldest first by `at`")
    # One card can lie within the tolerance of two labelled times only when those two are within twice it. Such a
    # pair is refused only when the phrases cannot separate it: every phrase of one is inside a phrase of the other,
    # so any card that matches the one matches the other too.
    timed = [(str(c["type"]).casefold(), str(c["seat"]).casefold(), _when(c["at"]), [_norm(x) for x in c["must_contain"]])
             for c in entry["expect"] if c.get("at")]
    for i, (type_a, seat_a, at_a, words_a) in enumerate(timed):
        for type_b, seat_b, at_b, words_b in timed[i + 1:]:
            if (type_a, seat_a) != (type_b, seat_b) or abs((at_b - at_a).total_seconds()) > 2 * AT_TOLERANCE_S:
                continue
            if _inside(words_a, words_b) or _inside(words_b, words_a):
                raise ValueError(f"label {entry['id']}: two cards of one seat and type lie within "
                                 f"{2 * AT_TOLERANCE_S} s of each other and their phrases do not tell them apart")


def judge_one(entry: dict, cards: list[dict], *, k: int = 5, strict_duplicates: bool = False) -> dict:
    check_label(entry)
    ranks: list[int | None] = []
    offsets: list[int | None] = []
    sources: list[str | None] = []
    tolerated: list[bool] = []
    duplicates = 0
    wrong_status: list[int] = []
    for expected in entry["expect"]:
        hits = [i for i, card in enumerate(cards, 1) if matches(card, expected)]
        ranks.append(hits[0] if hits else None)
        offsets.append(at_offset_ms(cards[hits[0] - 1], expected) if hits else None)
        sources.append(time_source(cards[hits[0] - 1]) if hits else None)
        tolerated.append(bool(hits) and needed_tolerance(cards[hits[0] - 1], expected))
        duplicates += max(0, len(hits) - 1)
        wrong_status += [i for i in hits if card_status(cards[i - 1]) != expected["status"]]
    active_index = next(i for i, card in enumerate(entry["expect"]) if card["status"] == "active")
    active_rank = ranks[active_index]
    above = [(entry["expect"][i]["status"], r) for i, r in enumerate(ranks)
             if i != active_index and r is not None and (active_rank is None or r < active_rank)]
    order = [r for status, r in above if status == "superseded"]
    overruled_above = [r for status, r in above if status == "overruled"]
    claim_above = [i for i, card in enumerate(cards, 1)
                   if str(card.get("type", "")).casefold() == "claim" and (active_rank is None or i < active_rank)]
    reasons, codes = [], []
    if active_rank is None or active_rank > k:
        reasons.append(f"active card at rank {active_rank}, need 1 to {k}")
        codes.append("active_missing" if active_rank is None else "active_beyond_k")
    if order:
        reasons.append(f"superseded card above the active one, at rank {order}")
        codes.append("superseded_above")
    if wrong_status:
        reasons.append(f"matched card with the wrong status, at rank {sorted(set(wrong_status))}")
        codes.append("wrong_status")
    if claim_above:
        reasons.append(f"Claim above the active card, at rank {claim_above}")
        codes.append("claim_above")
    if strict_duplicates and duplicates:
        reasons.append(f"{duplicates} duplicate card(s) of one expected card")
        codes.append("duplicates")
    return {"id": entry["id"], "passed": not reasons, "reasons": reasons, "reason_codes": codes,
            "kind": entry.get("kind"), "seat": entry["expect"][active_index]["seat"], "active_rank": active_rank,
            "ranks": ranks, "duplicates": duplicates, "overruled_above": overruled_above,
            "at_offsets_ms": offsets, "at_sources": sources,
            "within_tolerance": sum(tolerated),
            "tolerated_on_record_time": sum(1 for t, src in zip(tolerated, sources) if t and src == "record")}


def judge(gold: list[dict], results: dict[str, list[dict]], *, k: int = 5, strict_duplicates: bool = False) -> dict:
    per = []
    seen: set = set()
    for entry in gold:
        check_label(entry)
        if entry["id"] in seen:
            raise ValueError(f"label id {entry['id']!r} appears twice")
        seen.add(entry["id"])
        if entry["id"] not in results:
            active = next(c for c in entry["expect"] if c["status"] == "active")
            per.append({"id": entry["id"], "passed": False, "reasons": ["no result for this query"],
                        "reason_codes": ["no_result"], "kind": entry.get("kind"), "seat": active["seat"],
                        "active_rank": None, "ranks": [], "duplicates": 0, "overruled_above": [],
                        "at_offsets_ms": [], "at_sources": [], "within_tolerance": 0, "tolerated_on_record_time": 0})
            continue
        per.append(judge_one(entry, results[entry["id"]], k=k, strict_duplicates=strict_duplicates))
    found = [p["active_rank"] for p in per if p["active_rank"] is not None and p["active_rank"] <= k]
    n = len(per)
    return {"queries": n, "passed": sum(p["passed"] for p in per), "k": k,
            "recall_at_k": round(len(found) / n, 4) if n else None,
            "mrr_active": round(sum(1 / r for r in found) / n, 4) if n else None,
            "duplicates": sum(p["duplicates"] for p in per),
            "overruled_above": sum(len(p["overruled_above"]) for p in per),
            "matched_within_tolerance": sum(p["within_tolerance"] for p in per),
            "tolerated_on_record_time": sum(p["tolerated_on_record_time"] for p in per),
            "largest_at_offset_ms": max((abs(o) for p in per for o in p["at_offsets_ms"] if o is not None), default=0),
            "at_tolerance_s": AT_TOLERANCE_S,
            "by_reason": _count(code for p in per for code in p["reason_codes"]),
            "by_seat": _split(per, "seat"), "by_kind": _split(per, "kind"), "per_query": per}


def _count(items) -> dict:
    out: dict = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items()))


def _split(per: list[dict], field: str) -> dict:
    """{value: {"queries": n, "passed": m}} over the queries, by the active card's seat or the label's kind."""
    out: dict = {}
    for p in per:
        row = out.setdefault(str(p[field]) if p[field] is not None else "(none)", {"queries": 0, "passed": 0})
        row["queries"] += 1
        row["passed"] += bool(p["passed"])
    return dict(sorted(out.items()))


def results_by_id(rows: list[dict]) -> dict[str, list[dict]]:
    """{query id: ranked cards}. One id twice is refused, identical rows included: a dict would keep the last row, so a
    replayed or concatenated results file could erase a failed observation and pass on row order alone (codex-kxxx-cli,
    review of #213)."""
    out: dict[str, list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("cards"), list):
            raise ValueError(f"a results row must be an object with a cards list: {str(row)[:80]}")
        if row.get("id") in out:
            raise ValueError(f"results name query {row.get('id')!r} twice")
        out[row["id"]] = row["cards"]
    return out


def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip() and not line.lstrip().startswith("#")]


def selftest() -> int:
    """Every check is shown to pass on a good run and to fail on the one defect it exists for."""
    old = {"type": "Ruling", "seat": "pxx", "date": "2026-10-08", "status": "superseded",
           "must_contain": ["Wait for the patch"]}
    new = {"type": "Ruling", "seat": "kxxx", "date": "2026-10-08", "status": "active",
           "must_contain": ["Ship build 4c1d now"]}
    entry = {"id": "t1", "query": "release of the sample branches", "expect": [old, new]}

    def card(expected: dict, **over) -> dict:
        base = {"type": expected["type"], "owner": "operator", "seat": expected["seat"], "date": expected["date"],
                "status": expected["status"], "text": "RULING operator: " + expected["must_contain"][0].upper() + "\n ok"}
        return {**base, **over}

    noise = {"type": "Decision", "owner": "txxxx", "seat": "txxxx", "date": "2026-10-01", "status": "active",
             "text": "unrelated"}
    claim = {"type": "Claim", "owner": "pxx", "seat": "pxx", "date": "2026-10-08", "status": "active",
             "text": "summary: the operator paused the release"}
    cases = {
        "good run passes": ([card(new), noise, card(old)], {}, True, None),
        "active missing fails": ([noise, card(old)], {}, False, "active card at rank None"),
        "active beyond k fails": ([noise, noise, card(new)], {"k": 2}, False, "active card at rank 3"),
        "superseded above active fails": ([card(old), card(new)], {}, False, "superseded card above"),
        "wrong status fails": ([card(new, status="superseded")], {}, False, "wrong status"),
        "claim above ruling fails": ([claim, card(new)], {}, False, "Claim above"),
        "claim below ruling passes": ([card(new), claim], {}, True, None),
        "wrong seat is not a match": ([card(new, seat="pxx")], {}, False, "active card at rank None"),
        "wrong date is not a match": ([card(new, date="2026-10-07")], {}, False, "active card at rank None"),
        "a card with no time is not a match": ([card(new, date="")], {}, False, "active card at rank None"),
        "missing phrase is not a match": ([card(new, text="RULING operator: ship later")], {}, False,
                                          "active card at rank None"),
        "duplicate is counted, not failed": ([card(new), card(new)], {}, True, None),
        "duplicate fails when strict": ([card(new), card(new)], {"strict_duplicates": True}, False, "duplicate"),
    }
    bad = 0
    for name, (cards, kwargs, want_pass, want_reason) in cases.items():
        got = judge_one(entry, cards, **kwargs)
        ok = got["passed"] is want_pass and (want_reason is None or any(want_reason in r for r in got["reasons"]))
        if name.startswith("duplicate is counted"):
            ok = ok and got["duplicates"] == 1
        if name == "active missing fails":
            ok = ok and got["reason_codes"][0] == "active_missing" and "active_beyond_k" not in got["reason_codes"]
        if name == "active beyond k fails":
            ok = ok and got["reason_codes"] == ["active_beyond_k"]
        ok = ok and got["overruled_above"] == []      # no label here has an overruled card
        print(("ok   " if ok else "FAIL ") + name, "" if ok else got)
        bad += not ok
    # Another seat's ruling overrules; it does not supersede (the server's read rule since 8 Oct 2026).
    theirs = {"type": "Ruling", "seat": "exxxx-session", "date": "2026-10-06", "status": "overruled",
              "must_contain": ["Keep the archive whole"]}
    across = {"id": "t4", "query": "what the archive shows", "expect": [theirs, new]}
    mark = ["card/kxxx/" + "0" * 32]
    seats = {
        "an overruled card with its mark passes": (across, [card(new), card(theirs, status="active", overruled_by=mark)],
                                                   True, None),
        "an overruled card served as plain active fails": (across, [card(new), card(theirs, status="active")], False,
                                                           "wrong status"),
        "an overruled card served as superseded fails": (across, [card(new), card(theirs, status="superseded")], False,
                                                         "wrong status"),
        "an overruled card above the active one is counted, not failed": (
            across, [card(theirs, status="active", overruled_by=mark), card(new)], True, None),
        "a superseded card that also carries the mark stays superseded": (
            entry, [card(new), card(old, overruled_by=mark)], True, None),
        "an active card with an empty mark is active": (entry, [card(new, overruled_by=[])], True, None),
        "a card that states overruled without the mark fails": (across, [card(new), card(theirs, status="overruled")],
                                                                False, "wrong status"),
    }
    for name, (label, cards, want_pass, want_reason) in seats.items():
        got = judge_one(label, cards)
        ok = got["passed"] is want_pass and (want_reason is None or any(want_reason in r for r in got["reasons"]))
        ok = ok and got["overruled_above"] == ([1] if name.startswith("an overruled card above") else [])
        print(("ok   " if ok else "FAIL ") + name, "" if ok else got)
        bad += not ok
    # Times. Three rulings on one day, two of them in one seat (the deploy chain of 8 Oct 2026).
    first = {"type": "Ruling", "seat": "pxx", "date": "2026-10-08", "at": "2026-10-08T04:31:33Z",
             "status": "superseded", "must_contain": ["ship every branch"]}
    old_at = {**old, "at": "2026-10-08T07:32:13Z"}
    new_at = {**new, "at": "2026-10-08T07:37:00Z"}
    chain = {"id": "t3", "query": "release of the sample branches", "expect": [first, old_at, new_at]}
    timed = {
        "a card time to the second matches": ([card(new_at, date="2026-10-08T07:37:00Z")], True, None),
        "the same instant in another offset matches": ([card(new_at, date="2026-10-08T09:37:00+02:00")], True, None),
        "a card 100 ms before the labelled second matches and is counted": (
            [card(new_at, date="2026-10-08T07:36:59.900Z")], True, None),
        "a card 900 ms into the labelled second is an exact match": ([card(new_at, date="2026-10-08T07:37:00.900Z")], True, None),
        "five seconds before the labelled time still matches": ([card(new_at, date="2026-10-08T07:36:55Z")], True, None),
        "five seconds after the labelled time still matches": ([card(new_at, date="2026-10-08T07:37:05Z")], True, None),
        "six seconds off is not a match": ([card(new_at, date="2026-10-08T07:36:54Z")], False, "active card at rank None"),
        "six seconds late is not a match": ([card(new_at, date="2026-10-08T07:37:06Z")], False, "active card at rank None"),
        "a bare date cannot stand for a labelled time": ([card(new_at)], False, "active card at rank None"),
        "the time may be in an `at` field": ([card(new_at, date="2026-10-08", at="2026-10-08T07:37:00Z")], True, None),
        "an earlier ruling of the same seat and day above the active one fails": (
            [card(first, date=first["at"]), card(new_at, date=new_at["at"])], False, "superseded card above"),
    }
    for name, (cards, want_pass, want_reason) in timed.items():
        got = judge_one(chain, cards)
        ok = got["passed"] is want_pass and (want_reason is None or any(want_reason in r for r in got["reasons"]))
        if name.startswith("five seconds before"):      # the label is 5 s LATER than the card: +5000, and counted
            ok = ok and got["at_offsets_ms"][-1] == 5000 and got["within_tolerance"] == 1
        if name.startswith("five seconds after"):
            ok = ok and got["at_offsets_ms"][-1] == -5000 and got["within_tolerance"] == 1
        if name.startswith("a card 100 ms before"):       # another second: the old rule would have missed it
            ok = ok and got["at_offsets_ms"][-1] == 100 and got["within_tolerance"] == 1
        if name.startswith("a card 900 ms into"):         # the same second: exact under the old rule too
            ok = ok and got["at_offsets_ms"][-1] == -900 and got["within_tolerance"] == 0
        if name.startswith("a card time to the second"):  # an exact match is not counted as tolerated
            ok = ok and got["at_offsets_ms"][-1] == 0 and got["within_tolerance"] == 0
        print(("ok   " if ok else "FAIL ") + name, "" if ok else got)
        bad += not ok
    got = judge_one(entry, [card(new, date="2026-10-08T23:30:00-03:00")])   # 9 Oct in UTC
    ok = not got["passed"]
    print(("ok   " if ok else "FAIL ") + "a card's date is read in UTC")
    bad += not ok
    got = judge_one(entry, [card(new, date="2026-10-08T07:37:00Z")])
    ok = got["passed"]
    print(("ok   " if ok else "FAIL ") + "a full timestamp matches a label that gives only the date")
    bad += not ok
    same_words = {**old_at, "at": "2026-10-08T07:32:03Z"}                    # 10 s before old_at, same seat, SAME phrase
    part_words = {**old_at, "at": "2026-10-08T07:32:03Z", "must_contain": ["for the patch"]}      # its phrase is inside old_at's
    near = {**first, "at": "2026-10-08T07:32:03Z"}                           # 10 s before old_at, another phrase
    same_instant = {**first, "at": old_at["at"]}                             # one question call, two answers
    far = {**old_at, "at": "2026-10-08T07:32:02Z"}                           # 11 s before, same phrase: far enough
    for label, fine in (("ten seconds apart with different phrases", [near, old_at, new_at]),
                        ("at ONE instant with different phrases, as one question call gives", [same_instant, old_at, new_at]),
                        ("eleven seconds apart with the same phrase", [far, old_at, new_at])):
        try:
            check_label({**chain, "expect": fine})
            print("ok   two cards of one seat are accepted:", label)
        except ValueError as exc:
            print("FAIL two cards of one seat are refused:", label, exc)
            bad += 1
    hook = {"kind": "answered_question_hook", "record_uuid": "call"}
    mined = {"kind": "answered_question", "record_uuid": "call", "transcript_uuid": "u"}
    src = judge([chain, {**chain, "id": "t6"}, {**chain, "id": "t7"}],
                {"t3": [card(new_at, date="2026-10-08T07:37:03Z", source=hook)],
                 "t6": [card(new_at, date="2026-10-08T07:37:03Z", source=mined)],
                 "t7": [card(new_at, date="2026-10-08T07:37:00Z")]}, k=5)
    got_sources = [p["at_sources"][-1] for p in src["per_query"]]
    ok = (got_sources == ["hook_clock", "record", "unknown"] and src["matched_within_tolerance"] == 2
          and src["tolerated_on_record_time"] == 1)
    print(("ok   " if ok else "FAIL ") + "the report says whether a tolerated match sat on a record time or a hook clock",
          "" if ok else (got_sources, src["matched_within_tolerance"], src["tolerated_on_record_time"]))
    bad += not ok
    other_seat = {"type": "Ruling", "seat": "exxxx-session", "date": "2026-10-08", "at": "2026-10-08T07:36:55Z",
                  "status": "overruled", "must_contain": list(new["must_contain"])}          # 5 s before new_at, another seat, SAME phrase
    try:
        check_label({"id": "t5", "query": "q", "expect": [other_seat, new_at]})
        print("ok   two cards five seconds apart in DIFFERENT seats are accepted")
    except ValueError as exc:
        print("FAIL two cards five seconds apart in different seats are refused:", exc)
        bad += 1
    whole_tol = judge([chain], {"t3": [card(new_at, date="2026-10-08T07:37:03.250Z")]}, k=5)
    ok = (whole_tol["matched_within_tolerance"], whole_tol["largest_at_offset_ms"], whole_tol["at_tolerance_s"]) == (1, 3250, AT_TOLERANCE_S)
    print(("ok   " if ok else "FAIL ") + "the report counts a tolerated match and gives the largest offset", "" if ok else whole_tol)
    bad += not ok
    for label, broken in (("chain not oldest first by at", {**chain, "expect": [old_at, first, new_at]}),
                          ("two cards ten seconds apart with the same phrase", {**chain, "expect": [same_words, old_at, new_at]}),
                          ("two cards ten seconds apart, one phrase inside the other", {**chain, "expect": [part_words, old_at, new_at]}),
                          ("a chain newest first by at", {**chain, "expect": [new_at, old_at, first]}),
                          ("at on another day than date", {**chain, "expect": [{**new_at, "at": "2026-10-09T00:00:01Z"}]}),
                          ("date that is no date", {**entry, "expect": [{**new, "date": "8 Oct"}]})):
        try:
            check_label(broken)
            print("FAIL malformed label accepted:", label)
            bad += 1
        except ValueError:
            print("ok   malformed label refused:", label)
    whole = judge([entry, {**entry, "id": "t2"}], {"t1": [card(new)], "t2": [noise, card(new)]}, k=5)
    ok = (whole["passed"], whole["recall_at_k"], whole["mrr_active"]) == (2, 1.0, 0.75)
    print(("ok   " if ok else "FAIL ") + "aggregate recall and mrr", "" if ok else whole)
    bad += not ok
    missing = judge([entry], {}, k=5)
    ok = missing["passed"] == 0 and missing["recall_at_k"] == 0.0
    print(("ok   " if ok else "FAIL ") + "a query with no result fails")
    bad += not ok
    for label, broken in (("two active cards", {**entry, "expect": [new, new]}),
                          ("no active card", {**entry, "expect": [old]}),
                          ("a status the judge does not know", {**entry, "expect": [{**old, "status": "retired"}, new]}),
                          ("empty must_contain", {**entry, "expect": [{**new, "must_contain": []}]}),
                          ("a phrase that is None", {**entry, "expect": [{**new, "must_contain": [None]}]}),
                          ("a phrase that is empty", {**entry, "expect": [{**new, "must_contain": [""]}]}),
                          ("a phrase that is only spaces", {**entry, "expect": [{**new, "must_contain": ["  "]}]}),
                          ("must_contain as a bare string", {**entry, "expect": [{**new, "must_contain": "now"}]}),
                          ("more phrases than MAX_PHRASES", {**entry, "expect": [{**new, "must_contain": ["a", "b", "c", "d"]}]}),
                          ("an expected card that is not an object", {**entry, "expect": ["Ruling"]}),
                          ("expect that is not a list", {**entry, "expect": {"type": "Ruling"}}),
                          ("a label that is not an object", ["t1"]),
                          ("a date with a time part", {**entry, "expect": [{**new, "date": "2026-10-08T07:37:00Z"}]}),
                          ("a date that does not exist", {**entry, "expect": [{**new, "date": "2026-02-30"}]}),
                          ("a kind that is empty", {**entry, "kind": " "})):
        try:
            check_label(broken)
            print("FAIL malformed label accepted:", label)
            bad += 1
        except ValueError:
            print("ok   malformed label refused:", label)
        except Exception as exc:                  # a traceback in main(): exit 1, not the promised 2
            print("FAIL malformed label crashed instead of being refused:", label, type(exc).__name__)
            bad += 1
    try:
        judge([entry, entry], {"t1": [card(new)]}, k=5)
        print("FAIL a gold file with one id twice is accepted")
        bad += 1
    except ValueError:
        print("ok   a gold file with one id twice is refused")
    counts = judge([{**entry, "kind": "chain"}, {**entry, "id": "t2", "kind": "chain"}, {**entry, "id": "t3"}],
                   {"t1": [card(new)], "t2": [card(old), card(new)]}, k=5)
    ok = (counts["by_reason"] == {"no_result": 1, "superseded_above": 1}
          and counts["by_kind"] == {"(none)": {"queries": 1, "passed": 0}, "chain": {"queries": 2, "passed": 1}}
          and counts["by_seat"] == {"kxxx": {"queries": 3, "passed": 1}})
    print(("ok   " if ok else "FAIL ") + "the report counts failures by reason, seat and kind",
          "" if ok else (counts["by_reason"], counts["by_kind"], counts["by_seat"]))
    bad += not ok
    for label, rows in (("one id twice", [{"id": "t1", "cards": []}, {"id": "t1", "cards": [card(new)]}]),
                        ("one id twice, identical rows", [{"id": "t1", "cards": []}, {"id": "t1", "cards": []}]),
                        ("a row with no cards list", [{"id": "t1"}])):
        try:
            results_by_id(rows)
            print("FAIL results accepted:", label)
            bad += 1
        except ValueError:
            print("ok   results refused:", label)
        except Exception as exc:                  # a traceback in main(): exit 1, not the promised 2
            print("FAIL results crashed instead of being refused:", label, type(exc).__name__)
            bad += 1
    ok = results_by_id([{"id": "t1", "cards": []}, {"id": "t2", "cards": [card(new)]}]) == {"t1": [], "t2": [card(new)]}
    print(("ok   " if ok else "FAIL ") + "results with distinct ids are read as given")
    bad += not ok
    three = {**entry, "expect": [{**new, "must_contain": ["Ship", "build 4c1d", "now"]}]}
    try:
        check_label(three)
        print("ok   three phrases are accepted")
    except ValueError as exc:
        print("FAIL three phrases are refused:", exc)
        bad += 1
    print("SELFTEST", "passed" if not bad else f"FAILED ({bad})")
    return 0 if not bad else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold")
    ap.add_argument("--results")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--strict-duplicates", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.gold or not a.results:
        ap.error("--gold and --results are required")
    try:
        gold = _read_jsonl(a.gold)
        results = results_by_id(_read_jsonl(a.results))
        report = judge(gold, results, k=a.k, strict_duplicates=a.strict_duplicates)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"judge: cannot score: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1))
    return 0 if report["passed"] == report["queries"] else 1


if __name__ == "__main__":
    sys.exit(main())
