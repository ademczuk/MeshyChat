"""`meshy_node handoff-preflight`: before a seat records a review verdict on the board, say whether the board can record
it, and if not, what to do first (SD 265 item 1; nxxxxx item 2, R5; design note 97284f, pxx's read 2c3a0a).

Why. TKT-970's round-3 CHANGES verdict was written and refused: 409 review_handoff_unbound, then 422, with
maxReviewRounds 3 reached. Its result also still carried the $60 cap after a recorded decision made it one shared $100
across TKT-958 and TKT-831 (oxxxxxx, 3b47a8). The check runs before a review and stops it when the verdict
could not be recorded.

What it reads, and nothing else:
- the FACTS file F, which this seat writes from its own read-only board calls. An endpoint can replace F later without
  changing the answer (pxx (1));
- the CAP REGISTRY: oxxxxxx's decision cards on one thread (REGISTRY_THREAD), listed by GET /v1/cards and each read
  signed (pxx (2)).
It calls no board, holds no board key and writes nothing. F is gathered by the seat itself, so the check prevents
mistakes, not misreporting. It prints ids, counts and hashes only, never a key or a document body (pxx D).

F, schema handoff-facts/1. Exactly these fields, and any other is refused, so F never carries a document body:
  issue              the issue the verdict is for, like TKT-970
  verdict            accept, reject or changes
  execution_run_id   the issue's current executionRunId
  bound_handoff      {"id", "execution_run_id"}: the review handoff the board has bound, or null when none is
  rounds_recorded    the review rounds the board has already recorded on the issue (0 before the first)
  max_review_rounds  the board's maxReviewRounds for the issue
  result_cap         the cap the result document carries, {"scope": [issue ids], "amount_cents": int, "revision": int},
                     or null when it carries none
  reads              each board read F was taken from: {"what": a short label, "at": ISO-8601 with an offset,
                     "sha256": of the response text exactly as received}. At least one, none ahead of this clock by
                     more than FACTS_SKEW_S.

THE ROUND (pxx A). `round` is the round this verdict records: rounds_recorded + 1. A changes verdict asks for one more
round, so it is refused at round == max_review_rounds and recordable one below (TKT-970: changes at round 3 of 3,
refused with 422). Any verdict past the max is refused. An accept or reject AT the max is recordable here. That part is
an ASSUMPTION: the refusal seen was a changes verdict, and the rival (the board refuses any verdict once round == max)
is not excluded. The answer lists it under `assumptions` whenever it applies. rounds_remaining = max - round, never
below 0.

REASONS AND ROUTES (pxx B). Every reason that holds is listed, in this order, and the first one names next_route:
  facts_stale  -> refresh_facts   the oldest read in F is more than FACTS_MAX_AGE_S old
  unbound      -> rebind          no handoff is bound, or it is bound to another execution run (TKT-970's 409)
  cap_stale    -> refresh_cap     the result's cap is not the registry's current one (below)
  exhausted    -> close_on_delta  the rounds are spent: the author fixes it, and it closes on a delta read
None of them holds: recordable, and next_route is record. refresh_facts comes first because the other three are judged
from F.

THE CAP (pxx C). Amounts are integer cents, never floats. cap_hash is the sha256 of the canonical JSON
{"amount_cents", "revision", "scope"} (sorted keys, no whitespace, the scope a sorted list of issue ids), so two seats
get one hash for one cap. The registry's current cap for an issue is the highest REVISION among the cards whose scope
names it; two cards with that revision and different caps make the check refuse to judge. The result's cap is stale
when any issue of its scope has another current cap or none, or when the verdict's own issue has a current cap the
result does not carry.

THE REGISTRY CARDS. A decision card on REGISTRY_THREAD, written by oxxxxxx, whose text's first line is
  CAP: TKT-831,TKT-958; CENTS: 10000; REVISION: 2; ROW: 1258
and whose signed read verifies with signed_by oxxxxxx's pinned peer, its signed value a card.v1 card of oxxxxxx's
whose thread_id is still the registry thread. A card withdrawn (redacted) after the listing is left out and counted
(withdrawn_after_listing); one withdrawn before it is never listed (GET /v1/cards leaves out redacted cards), so
nothing here counts it. A card whose signed head has moved to another thread since the listing is left out and
counted (moved). Any other card that does not verify, that another peer signed, or that is not oxxxxxx's card.v1, makes
the check refuse to judge, because a registry that cannot be trusted cannot say that a cap is current. A oxxxxxx card
on the thread without a CAP line is counted and ignored; a CAP line that does not parse refuses. The registry's
listing must end in an exact `next` of null; any other form refuses, as does a listing that fails. oxxxxxx owns the
registry and its format: this is the shape the check reads until oxxxxxx fixes another.

THE CLOCK. The facts' age is judged by the clock read AFTER the last registry read, so registry I/O cannot let
facts just under FACTS_MAX_AGE_S pass when they are past it by the time of the answer (pxx P2 F1).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Callable

from . import rounds

SCHEMA = "handoff-facts/1"
FACTS_FIELDS = frozenset({"schema", "issue", "verdict", "execution_run_id", "bound_handoff", "rounds_recorded",
                          "max_review_rounds", "result_cap", "reads"})
VERDICTS = ("accept", "reject", "changes")
#: F older than this is not judged on (pxx (1): "about 10 minutes").
FACTS_MAX_AGE_S = 600
#: A read time this far ahead of this clock is a malformed fact, not a fresh one.
FACTS_SKEW_S = 120
REGISTRY_THREAD = "cap-registry"
REGISTRY_SEAT = "oxxxxxx"
#: The card schema a registry card's signed value must carry (tools/meshy_node/cards.SCHEMA).
CARD_SCHEMA = "card.v1"
#: Cards on the registry thread read at most; more refuses rather than judge on a prefix.
REGISTRY_MAX_CARDS = 50
ISSUE_RE = re.compile(r"^[A-Z][A-Z0-9]{0,15}-[1-9][0-9]{0,8}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGITS = re.compile(r"^[0-9]{1,12}$")
#: Each reason and the route it names, in the order they are listed; the first that holds names next_route (pxx B).
REASONS = (("facts_stale", "refresh_facts"), ("unbound", "rebind"), ("cap_stale", "refresh_cap"),
           ("exhausted", "close_on_delta"))
ROUTE_TEXT = {
    "record": "record the verdict",
    "refresh_facts": f"read the board again and rewrite the facts: the oldest read is more than "
                     f"{FACTS_MAX_AGE_S // 60} min old",
    "rebind": "ask the binding owner (oxxxxxx) to bind the review handoff to the issue's current execution run",
    "refresh_cap": "re-export the result with the registry's current cap",
    "close_on_delta": "the rounds are spent: the author fixes it, and it closes on a delta read",
}
NOTE = "F is gathered by this seat's own board reads, so this check prevents mistakes, not misreporting."
ASSUME_AT_MAX = ("an accept or reject at round == max_review_rounds is taken as recordable; the refusal seen "
                 "(TKT-970) was a changes verdict, and the rival (the board refuses any verdict at the max) is not "
                 "excluded")


class Unjudgeable(ValueError):
    """F or the registry cannot be judged, so the check says nothing about the verdict (the command exits 2)."""


def _when(raw) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo is not None and when.utcoffset() is not None else None


def _int(value, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:            # type(): a bool is not a count, a float not cents
        raise Unjudgeable(f"{name} must be an integer of at least {minimum}")
    return value


def _id(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise Unjudgeable(f"{name} must be a non-empty id of at most 128 characters")
    return value


def _scope(raw, name: str) -> list[str]:
    if (not isinstance(raw, list) or not raw or len(set(map(str, raw))) != len(raw)
            or not all(isinstance(s, str) and ISSUE_RE.fullmatch(s) for s in raw)):
        raise Unjudgeable(f"{name} must be a list of distinct issue ids, like TKT-958")
    return sorted(raw)


def _cap(raw, name: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"scope", "amount_cents", "revision"}:
        raise Unjudgeable(f"{name} must be {{scope, amount_cents, revision}}, or null")
    return {"scope": _scope(raw["scope"], f"{name}.scope"),
            "amount_cents": _int(raw["amount_cents"], f"{name}.amount_cents", 0),
            "revision": _int(raw["revision"], f"{name}.revision", 1)}


def cap_hash(cap: dict | None) -> str | None:
    """The sha256 of the cap's canonical JSON: sorted keys, no whitespace, the scope a sorted list (pxx C)."""
    if cap is None:
        return None
    canon = {"amount_cents": cap["amount_cents"], "revision": cap["revision"], "scope": sorted(cap["scope"])}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def facts(raw, now: datetime) -> dict:
    """F checked against handoff-facts/1, or Unjudgeable naming the first thing wrong (a field name, never a value)."""
    if not isinstance(raw, dict):
        raise Unjudgeable("the facts must be one JSON object")
    unknown = sorted(set(raw) - FACTS_FIELDS)
    if unknown:
        raise Unjudgeable(f"the facts hold a field this check does not know: {unknown[0]} (F carries ids, counts "
                          "and hashes, never a document)")
    missing = sorted(FACTS_FIELDS - set(raw))
    if missing:
        raise Unjudgeable(f"the facts lack {missing[0]}")
    if raw["schema"] != SCHEMA:
        raise Unjudgeable(f"the facts' schema must be {SCHEMA}")
    if not isinstance(raw["issue"], str) or not ISSUE_RE.fullmatch(raw["issue"]):
        raise Unjudgeable("issue must be an issue id, like TKT-970")
    if raw["verdict"] not in VERDICTS:
        raise Unjudgeable(f"verdict must be one of {', '.join(VERDICTS)}")
    bound = raw["bound_handoff"]
    if bound is not None:
        if not isinstance(bound, dict) or set(bound) != {"id", "execution_run_id"}:
            raise Unjudgeable("bound_handoff must be {id, execution_run_id}, or null")
        bound = {"id": _id(bound["id"], "bound_handoff.id"),
                 "execution_run_id": _id(bound["execution_run_id"], "bound_handoff.execution_run_id")}
    reads = raw["reads"]
    if not isinstance(reads, list) or not reads:
        raise Unjudgeable("the facts name no board read (reads): a fact without its read is not one")
    times, hashes = [], []
    for i, read in enumerate(reads):
        if not isinstance(read, dict) or set(read) != {"what", "at", "sha256"}:
            raise Unjudgeable(f"reads[{i}] must be {{what, at, sha256}}")
        if not isinstance(read["what"], str) or not read["what"].strip() or len(read["what"]) > 40:
            raise Unjudgeable(f"reads[{i}].what must be a short label")
        at = _when(read["at"])
        if at is None:
            raise Unjudgeable(f"reads[{i}].at must be an ISO-8601 time with an offset")
        if (at - now).total_seconds() > FACTS_SKEW_S:
            raise Unjudgeable(f"reads[{i}].at is more than {FACTS_SKEW_S} s ahead of this clock")
        if not isinstance(read["sha256"], str) or not SHA256_RE.fullmatch(read["sha256"]):
            raise Unjudgeable(f"reads[{i}].sha256 must be 64 lower-case hex")
        times.append(at)
        hashes.append(read["sha256"])
    return {"issue": raw["issue"], "verdict": raw["verdict"],
            "execution_run_id": _id(raw["execution_run_id"], "execution_run_id"), "bound_handoff": bound,
            "rounds_recorded": _int(raw["rounds_recorded"], "rounds_recorded", 0),
            "max_review_rounds": _int(raw["max_review_rounds"], "max_review_rounds", 1),
            "result_cap": _cap(raw["result_cap"], "result_cap"), "oldest_read": min(times), "read_sha256": hashes}


def cap_line(text) -> dict | None:
    """A registry card's cap from its first line, None when that line is not a CAP line, Unjudgeable when it is one
    that does not parse."""
    line = rounds.first_line(text)
    if not line.startswith("CAP:"):
        return None
    fields = rounds.parse(line)
    if fields is None or set(fields) - {"CAP", "CENTS", "REVISION", "ROW"} or not {"CAP", "CENTS", "REVISION"} <= set(fields):
        raise Unjudgeable("a registry card's CAP line does not parse (CAP: issues; CENTS: n; REVISION: n; ROW: row)")
    if not _DIGITS.fullmatch(fields["CENTS"]) or not _DIGITS.fullmatch(fields["REVISION"]) or int(fields["REVISION"]) < 1:
        raise Unjudgeable("a registry card's CENTS and REVISION must be whole numbers (cents, never a decimal)")
    scope = [s.strip() for s in fields["CAP"].split(",")]
    return {"scope": _scope(scope, "a registry card's CAP"), "amount_cents": int(fields["CENTS"]),
            "revision": int(fields["REVISION"]), "row": fields.get("ROW")}


def registry(keys: list[str], read: Callable[[str], dict], signer: str, thread: str = REGISTRY_THREAD) -> dict:
    """The cap cards among `keys`, each read signed at its head. A card withdrawn since the listing is left out and
    named (the listing itself never shows one withdrawn before it: GET /v1/cards leaves out redacted cards); one that
    does not verify as `signer`'s, or whose signed card is not oxxxxxx's card.v1, refuses (Unjudgeable). A card whose
    signed head is no longer on `thread` (moved after the listing) is not a registry card: it is left out and counted
    (pxx 2a P2 F2)."""
    entries, withdrawn, moved, other = [], [], [], 0
    for key in keys:
        try:
            msg = read(key)
        except Exception as exc:                            # noqa: BLE001 - the whole check refuses, typed
            raise Unjudgeable(f"the registry card {key} could not be read ({type(exc).__name__})") from None
        if not isinstance(msg, dict):
            raise Unjudgeable(f"the registry card {key} came back without a message")
        if msg.get("redacted"):
            withdrawn.append(key)
            continue
        if msg.get("signature_verified") is not True:
            raise Unjudgeable(f"the registry card {key} did not verify, so the registry cannot be trusted")
        if msg.get("signed_by") != signer:
            raise Unjudgeable(f"the registry card {key} is signed by another peer, not {REGISTRY_SEAT}")
        value = msg.get("value") if isinstance(msg.get("value"), dict) else {}
        if value.get("schema") != CARD_SCHEMA or value.get("seat") != REGISTRY_SEAT:
            raise Unjudgeable(f"the registry card {key} is not a {CARD_SCHEMA} card of {REGISTRY_SEAT}'s")
        if value.get("thread_id") != thread:
            moved.append(key)
            continue
        cap = cap_line(value.get("text"))
        if cap is None:
            other += 1
            continue
        entries.append({"key": key, **cap})
    return {"entries": entries, "withdrawn_after_listing": withdrawn, "moved": moved, "other": other,
            "cards": len(keys)}


def current_cap(entries: list[dict], issue: str) -> dict | None:
    """The registry's current cap for `issue`: the highest revision among the cards naming it."""
    named = [e for e in entries if issue in e["scope"]]
    if not named:
        return None
    top = max(e["revision"] for e in named)
    tops = [e for e in named if e["revision"] == top]
    if len({cap_hash(e) for e in tops}) > 1:
        raise Unjudgeable(f"two registry cards claim revision {top} for {issue} with different caps")
    return tops[0]


def _cap_check(f: dict, entries: list[dict]) -> dict:
    carried = f["result_cap"]
    own = current_cap(entries, f["issue"])
    out = {"carried_hash": cap_hash(carried), "carried_revision": carried["revision"] if carried else None,
           "current_hash": None, "current_revision": None, "current_key": None, "stale": False, "why": None}
    if carried is None:
        if own is not None:
            out.update(current_hash=cap_hash(own), current_revision=own["revision"], current_key=own["key"],
                       stale=True, why=f"the registry caps {f['issue']}, and the result carries no cap")
        else:
            out["why"] = "no cap carried and none listed for the issue: nothing to compare"
        return out
    currents = {s: current_cap(entries, s) for s in carried["scope"]}
    if own is not None:
        currents[f["issue"]] = own
    missing = sorted(s for s, c in currents.items() if c is None)
    found = {cap_hash(c): c for c in currents.values() if c is not None}
    if len(found) == 1:
        cur = next(iter(found.values()))
        out.update(current_hash=cap_hash(cur), current_revision=cur["revision"], current_key=cur["key"])
    if missing:
        out.update(stale=True, why=f"the registry lists no cap for {', '.join(missing)}")
    elif len(found) > 1:
        out.update(stale=True, why="the registry's current caps for the result's scope and the issue are not one cap")
    elif out["current_hash"] != out["carried_hash"]:
        out.update(stale=True, why="the result carries another cap than the registry's current one")
    else:
        out["why"] = "the result carries the registry's current cap"
    return out


def judge(f: dict, reg: dict, now: datetime, *, thread: str = REGISTRY_THREAD) -> dict:
    """The answer for checked facts `f` and the registry `reg`: every reason that holds, the route, the round, the cap
    hashes and the counts. Ids, counts and hashes only."""
    held = set()
    age = (now - f["oldest_read"]).total_seconds()
    if age > FACTS_MAX_AGE_S:
        held.add("facts_stale")
    bound = f["bound_handoff"]
    if bound is None or bound["execution_run_id"] != f["execution_run_id"]:
        held.add("unbound")
    cap = _cap_check(f, reg["entries"])
    if cap["stale"]:
        held.add("cap_stale")
    rnd, top = f["rounds_recorded"] + 1, f["max_review_rounds"]
    assumptions = []
    if rnd > top or (f["verdict"] == "changes" and rnd == top):
        held.add("exhausted")
    elif rnd == top:
        assumptions.append(ASSUME_AT_MAX)
    reasons = [name for name, _ in REASONS if name in held]
    route = next((r for name, r in REASONS if name in held), "record")
    return {"recordable": not reasons, "refusal_reasons": reasons, "next_route": route, "route": ROUTE_TEXT[route],
            "issue": f["issue"], "verdict": f["verdict"], "round": rnd, "max_review_rounds": top,
            "rounds_remaining": max(0, top - rnd),
            "binding": {"handoff_id": bound["id"] if bound else None,
                        "bound_to_current_run": bool(bound and bound["execution_run_id"] == f["execution_run_id"])},
            "cap_scope_hash": cap["current_hash"], "carried_cap_hash": cap["carried_hash"], "cap": cap,
            "facts": {"reads": len(f["read_sha256"]), "oldest_read_age_s": int(age), "max_age_s": FACTS_MAX_AGE_S,
                      "read_sha256": f["read_sha256"]},
            "registry": {"thread": thread, "cards": reg["cards"], "caps": len(reg["entries"]),
                         "withdrawn_after_listing": reg["withdrawn_after_listing"], "moved": reg["moved"],
                         "other": reg["other"],
                         "note": "a card withdrawn before the listing is not listed by the server, so not counted"},
            "assumptions": assumptions, "note": NOTE}


def render(result: dict) -> str:
    """The text view: the verdict line, the route, the round, the cap, the facts' age, assumptions, the note."""
    state = "recordable" if result["recordable"] else f"NOT recordable ({', '.join(result['refusal_reasons'])})"
    lines = [f"handoff-preflight {result['issue']}, {result['verdict']} verdict, round {result['round']} of "
             f"{result['max_review_rounds']}: {state}",
             f"next: {result['next_route']}: {result['route']}",
             f"rounds remaining after this one: {result['rounds_remaining']}"]
    cap = result["cap"]
    carried = f"revision {cap['carried_revision']} ({cap['carried_hash'][:12]})" if cap["carried_hash"] else "none"
    current = (f"revision {cap['current_revision']} ({cap['current_hash'][:12]}, card {cap['current_key']})"
               if cap["current_hash"] else "none")
    lines.append(f"cap: the result carries {carried}; the registry's current is {current}: {cap['why']}")
    reg = result["registry"]
    lines.append(f"registry: {reg['cards']} card(s) on {reg['thread']}, {reg['caps']} with a CAP line"
                 + (f", {len(reg['withdrawn_after_listing'])} withdrawn since the listing"
                    if reg["withdrawn_after_listing"] else "")
                 + (f", {len(reg['moved'])} moved off the thread" if reg["moved"] else ""))
    facts_ = result["facts"]
    lines.append(f"facts: {facts_['reads']} board read(s), the oldest {facts_['oldest_read_age_s'] // 60} min old "
                 f"(limit {facts_['max_age_s'] // 60} min)")
    lines.extend(f"assumption: {a}" for a in result["assumptions"])
    lines.append(result["note"])
    return "\n".join(lines)
