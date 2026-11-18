"""`meshy_node owed --reconcile`: every ask a seat's `owed` lists, sorted into exactly one class, with its evidence
(SD 265 item 3; nxxxxx item 3, R5; design note f3a6ce, pxx's read d910ea, pxx's review d95791).

Why. nxxxxx counted seven open or active asks with a median age of 23 h, and four of them were already closed by
signed messages. In a 78-hit sample, three duplicate-body pairs were intentional fan-out to different recipients,
not retransmissions. A count that mixes these says little about the work a seat owes, and answering each copy of a
fan-out risks acknowledgement loops.

What it reads, and nothing else:
- #544's owed, one call per state (open, active, answered) over the same window and thread, so answered volume never
  pushes an open ask past the limit;
- the server's unread list for this seat (GET /v1/mesh/unread), so that it never makes the FIRST read of a message
  (pxx P1). On the server every completed memory.read is a fetch: it takes the message out of the seat's unread
  list and its session-start digest. A receipt still unread is not read and closes nothing; a fan-out candidate still
  unread is not read and keeps its class, flagged. When that list comes back cut, nothing is read at all. The list
  covers at most UNREAD_GATE_S (14 days, the owed default), since a longer window timed out on production, so a
  message older than that is not read either: whether it is still unread cannot be told;
- at most `max_reads` signed reads, made with node.read(record=False), so the local fetched and read ledgers are not
  touched and no relay or directive is evaluated: the --closures receipts first, then the fan-out candidates, oldest
  first. What the cap leaves unread is named. Every read is of an exact version, never the head (pxx): a
  candidate at the version the owed snapshot listed, so an edit landing between the unread GET and the read is never
  fetched unseen; a receipt at the head version GET /v1/mesh/neighbours gives, whose time, the server's own, places it
  against the gate (the writer's clock in its key does not). A receipt without both is not read. The command takes
  every receipt's anchor BEFORE the unread GET (pxx), so a version committed after the unread list was taken
  is never the one read;
- the recipients route, so that authors and recipient sets compare as peers (pxx P2-2; GitHub Codex on #547:
  one peer posting under two labels is one author), at most RESOLVE_MAX_LABELS labels a run.
It sends nothing and acknowledges nothing. Message content is hashed here (sha256) and never printed.

Each row gets ONE class, decided in this order:
1. unknown: the row is malformed (no key, no time, no src_field, or listed under a state it was not asked for).
2. closed: a --closures receipt closes it. The receipt's signature verified, oxxxxxx's pinned peer or this seat signed
   it, and its `closes` field names the ask's key exactly (pxx B). Like the server's projection
   (apps/mesh/normalize.closes_of), `closes` is a list of msg/ keys; a key in a receipt's body does not count. The
   server already settles a close by the owing seat or by the ask's author, so those asks never reach this list.
3. unknown: a receipt names it but cannot close it: not read, not verified, or signed by another peer. Never dropped.
4. answered: the server's state `answered`, a signed reply from this seat in_reply_to the ask. A reply is not a
   settlement (apps/mesh/graph.py owed_acks), so it stays apart from closed (pxx A).
5. retransmission: an ask from the same signer, with the same content as an earlier ask in its group and the SAME
   recipients. It is a copy, and the earlier key is its evidence (pxx D).
6. intentional_fanout: asks from the same signer with the same content, each within FANOUT_WINDOW_S of the one
   before, to DIFFERENT recipient sets. One fanout_group (its first key) for the counts, and every row kept, so a
   targeted directive is never merged. Each row carries the class it would otherwise have (base_class).
7. edge_only: the owes_ack edge did not come from the ask's `to` (src_field not `to:`), so this seat is only copied or
   reached by a broadcast. That is the edge's obligation, not this seat's work. Today's server makes owes_ack edges
   only from `to` (apps/mesh/normalize.py), so this is 0 unless an older edge says otherwise.
8. expired_unreviewed: state open (no reply, and nothing from this seat in the thread since) and older than the
   expiry. It is listed for triage, it is still owed, and the tool never closes it.
9. live_work: the rest. Addressed in `to`, and either open and not older than the expiry, or active (this seat posted
   in the thread after the ask, not as a reply to it). A done_when that names another roster seat is annotated
   (done_when_names), never reclassed (pxx).

The CONTENT is the whole signed value except NOT_CONTENT (to, cc and from, compared as peers below, and ts and
session_label, the send's own stamp), hashed as one canonical JSON object. So a directive's words count, not only its
boilerplate body (pxx P2-1), and so do the subject, the ref, the thread, the reply target, requires_ack and
closes: the same words about another object, in another thread or in reply to another message are another
obligation (GitHub Codex on #547). The RECIPIENTS are `to` and `cc` as the peers the recipients route maps each label to, so
`kxxx` and `peer-a2b5be8f` are one recipient (P2-2); a label it does not know compares as itself, and when the route
fails nothing is grouped.

Only content whose read verified is hashed, so only verified asks are grouped. A fan-out read that fails or does not
verify groups nothing: the row keeps the class above, flagged `fanout_unchecked` with the reason, as a row left unread
by the cap or the unread gate is. It does not become unknown: over all of kxxx's history on production (8 Nov 2026),
29 of 60 such reads were E_SIGNATURE_KEY_UNAVAILABLE (the old writers' keys), and as unknown those rows lost the class
the server gives them and buried the real unknowns.

The counts balance over EVERY row the server returned, a key listed twice included (pxx P2-3; a cut list says
so):
open + active + answered = live_work + edge_only + expired_unreviewed + intentional_fanout + retransmission
                           + answered + closed + unknown + duplicate
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Callable

from . import rounds

#: The three lists, asked for one at a time, in this order (bare `owed`'s primary first).
STATES = ("open", "active", "answered")
#: Every class a row can get, in the order the summary prints them.
CLASSES = ("live_work", "edge_only", "expired_unreviewed", "intentional_fanout", "retransmission", "answered",
           "closed", "unknown")
DEFAULT_EXPIRE = "72h"
#: Signed reads made at most: receipts first, then fan-out candidates oldest first (the `round` bound).
DEFAULT_MAX_READS = 60
#: Rows asked for per state when no --limit is given: the bound bare `owed`'s add-ons use.
LIST_LIMIT = 500
#: The unread list asked for at most: the route's own bound. A cut list stops every read.
UNREAD_LIMIT = 500
#: The unread gate's widest window: the owed default (14 d) and twice the session-start digest's 7 d. Over a longer one
#: the server's unread query hit its statement timeout for a busy seat (8 Nov 2026, 89 days for kxxx: QueryCanceled,
#: HTTP 500), so a message older than the gate is never read: whether it is still unread cannot be told cheaply.
UNREAD_GATE_S = 14 * 86400
#: Two asks from one signer with one content are one group when each is at most this many seconds after the one before.
FANOUT_WINDOW_S = 600
#: The seat whose receipts close another seat's asks: oxxxxxx owns the closure receipts (pxx B).
RECEIPT_SEAT = "oxxxxxx"
#: Labels resolved to peers at most in one run (authors and recipients): the recipients route takes 10 a call and 30
#: calls a minute per peer (apps/mesh/views.RECIPIENTS_PER_MINUTE), so this stays at half of that.
RESOLVE_MAX_LABELS = 150
#: The signed fields that are NOT what is asked. `to`, `cc` and `from` are compared as peers instead (P2-2, and the
#: cross-label finding on #547); `ts` and `session_label` are the send's own stamp, when and from which session it
#: went. Every other field of the signed value is content, so a field this list does not name makes two asks differ,
#: never alike. A list of fields to INCLUDE failed the other way three times: subject and ref, then the thread, then
#: the reply target were missed, and each miss made two obligations one (GitHub Codex on #547).
NOT_CONTENT = ("to", "cc", "from", "ts", "session_label")
#: The reasons a fan-out candidate goes unchecked, as `fanout_unchecked` begins.
UNCHECKED_CAP, UNCHECKED_UNREAD, UNCHECKED_FAILED, UNCHECKED_UNVERIFIED, UNCHECKED_RECIPIENTS = (
    "not read", "unread", "its read failed", "its read did not verify", "recipients not resolved")
UNREAD_CUT = "the server's unread list was cut, so reconcile cannot tell what is unread and reads nothing"
UNREAD_NO_WINDOW = "the server's unread answer did not say the window it covered, so reconcile reads nothing"
UNREAD_STILL = "still unread on the server, and reconcile never makes the first read: read it, then run again"
UNREAD_OLDER = "older than the unread gate's window, so whether it is still unread cannot be told: not read"


class Malformed(ValueError):
    """The server's answer lacks a field this view must read; a view built from a guess would look complete."""


def _when(raw) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo is not None and when.utcoffset() is not None else None


def _labels(raw) -> tuple[str, ...]:
    """A to or cc field as sorted, casefolded labels; one string is one label."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({item.strip().casefold() for item in raw if isinstance(item, str) and item.strip()}))


def _closes(raw) -> set[str]:
    """The keys a receipt closes, by the projector's rule: a list, and only its msg/ strings."""
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str) and item.startswith("msg/")}


def _why(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0][:160] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def content_sha256(value: dict) -> str:
    """The sha256 of an ask's content: its signed value without the NOT_CONTENT fields, as one canonical JSON
    object. A field absent is left out and a field present is hashed as it is, null included."""
    content = {field: item for field, item in value.items() if field not in NOT_CONTENT}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def done_when_names(text, roster: set[str], me: str | None) -> list[str]:
    """The roster labels a done_when names, other than this seat's own, as whole words: `codex-kxxx-cli` is not kxxx."""
    if not isinstance(text, str) or not text:
        return []
    mine = (me or "").casefold()
    return [label for label in sorted(roster) if label.casefold() != mine and re.search(
        r"(?<![A-Za-z0-9_-])" + re.escape(label) + r"(?![A-Za-z0-9_-])", text, re.IGNORECASE)]


def anchor_of(answer) -> tuple[int, datetime] | None:
    """(head version, the server's write time of it) from a GET /v1/mesh/neighbours answer's `anchor`, or None. The
    server's own time, never the writer's clock in the key (pxx P2)."""
    a = answer.get("anchor") if isinstance(answer, dict) else None
    version = a.get("version") if isinstance(a, dict) else None
    at = _when(a.get("recorded_at")) if isinstance(a, dict) else None
    if type(version) is not int or version < 1 or at is None:
        return None
    return version, at


def unread_since(answer) -> datetime | None:
    """The window the server's unread answer ACTUALLY covered (`filters.since`), or None. With a call retention
    shorter than the gate, the server moves an earlier `since` up and says so (apps/mesh/views._unread_window), so
    the gate starts no earlier than this (GitHub Codex on #547 at d3a42158)."""
    filters = answer.get("filters") if isinstance(answer, dict) else None
    return _when(filters.get("since")) if isinstance(filters, dict) else None


def unread_keys(answer) -> set[str] | None:
    """The keys the server still counts unread for this seat (GET /v1/mesh/unread), or None when its list was cut."""
    box = answer.get("unread") if isinstance(answer, dict) else None
    if not isinstance(box, dict) or not isinstance(box.get("items"), list) or not isinstance(box.get("truncated"), bool):
        raise Malformed("the server's unread answer has no unread.items list or unread.truncated flag")
    if box["truncated"]:
        return None
    return {item["key"] for item in box["items"] if isinstance(item, dict) and isinstance(item.get("key"), str)}


def _row(raw, listed: str, now: datetime) -> dict:
    hit = raw.get("hit") if isinstance(raw, dict) else None
    hit = hit if isinstance(hit, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    key = hit.get("key") if isinstance(hit.get("key"), str) and hit.get("key") else None
    at = _when(hit.get("recorded_at"))
    src = hit.get("src_field")
    problems = []
    if key is None:
        problems.append("no key")
    if raw.get("state") is not None and raw.get("state") != listed:
        problems.append(f"state {raw.get('state')!r} in the {listed} list")
    if at is None:
        problems.append("no recorded_at with a timezone")
    if not isinstance(src, str) or not src:
        problems.append("no src_field")
    return {"key": key, "version": hit.get("version"), "state": listed, "author": hit.get("author"),
            "thread_id": raw.get("thread_id"),
            "recorded_at": hit.get("recorded_at"), "src_field": src, "msg_type": raw.get("msg_type"),
            "done_when": raw.get("done_when"),
            "_at": at, "_age_s": (now - at).total_seconds() if at else None, "_problems": problems}


def _rows(lists: dict, now: datetime) -> tuple[list[dict], dict[str, bool], list[str], dict[str, int]]:
    """The rows, each key once; whether each list was cut; the keys listed again; and every row fetched, by list."""
    rows, seen, duplicates, truncated, fetched = [], set(), [], {}, {}
    for listed in STATES:
        answer = lists.get(listed)
        if not isinstance(answer, dict) or not isinstance(answer.get("owed"), list):
            raise Malformed(f"the server's {listed} list has no `owed` array")
        if not isinstance(answer.get("truncated"), bool):
            raise Malformed(f"the server's {listed} list does not say whether it was cut (`truncated`)")
        truncated[listed] = answer["truncated"]
        fetched[listed] = len(answer["owed"])
        for raw in answer["owed"]:
            row = _row(raw, listed, now)
            if row["key"] is not None and row["key"] in seen:
                duplicates.append(row["key"])
                continue
            if row["key"] is not None:
                seen.add(row["key"])
            rows.append(row)
    return rows, truncated, duplicates, fetched


def _receipts(closures: list[str], read, anchor, budget: dict, listed: set[str], signers: dict[str, str],
              peer_labels: dict[str, str], unread: set[str] | None, gate_since: datetime, blocked: str | None
              ) -> tuple[list[dict], dict[str, str], dict[str, list[str]]]:
    """Each receipt is placed by the SERVER's head version and write time (`anchor`, GET /v1/mesh/neighbours: no
    memory.read), and read pinned to that version (pxx P2). Without both it is not read. `blocked`, the
    reason no read may be made at all."""
    out, closed_by, refused_by = [], {}, {}
    for rkey in dict.fromkeys(closures):
        if blocked or unread is None:
            out.append({"key": rkey, "status": "not_read", "detail": blocked or UNREAD_CUT})
            continue
        try:
            pinned = anchor_of(anchor(rkey))
        except Exception as exc:                            # noqa: BLE001 - one receipt, never the whole view
            pinned, why = None, _why(exc)
        else:
            why = "the server's answer gave no head version and time"
        if pinned is None:
            out.append({"key": rkey, "status": "version_not_known", "detail": f"not read: {why}"})
            continue
        version, written = pinned
        if written < gate_since:
            out.append({"key": rkey, "status": "not_read", "detail": UNREAD_OLDER})
            continue
        if rkey in unread:
            out.append({"key": rkey, "status": "unread", "detail": UNREAD_STILL})
            continue
        if budget["reads"] >= budget["max"]:
            out.append({"key": rkey, "status": "not_read", "detail": f"the {budget['max']}-read cap"})
            continue
        budget["reads"] += 1
        try:
            msg = read(rkey, version)
        except Exception as exc:                            # noqa: BLE001 - one receipt, never the whole view
            out.append({"key": rkey, "status": "unreadable", "detail": _why(exc)})
            continue
        value = msg.get("_value") if isinstance(msg, dict) and isinstance(msg.get("_value"), dict) else {}
        names = _closes(value.get("closes"))
        signer = msg.get("signed_by") if isinstance(msg, dict) else None
        if not isinstance(msg, dict) or msg.get("signature_verified") is not True or not signer:
            status = "unverified"
            detail = str((msg.get("signature_error") if isinstance(msg, dict) else None) or "the signature did not verify")
        elif signer not in signers:
            status = "signer_not_allowed"
            detail = (f"signed by {peer_labels.get(signer, signer)}; only {' or '.join(sorted(set(signers.values())))} "
                      "close another seat's asks here")
        elif not names:
            status, detail = "names_no_ask", "its closes field names no msg/ key"
        else:
            status, detail = "ok", None
        entry = {"key": rkey, "status": status, "signer": peer_labels.get(signer, signer) if signer else None,
                 "closes_listed": sorted(names & listed), "names_unlisted": sorted(names - listed)}
        if detail:
            entry["detail"] = detail
        out.append(entry)
        for ask in sorted(names & listed):
            if status == "ok":
                closed_by.setdefault(ask, rkey)
            else:
                refused_by.setdefault(ask, []).append(f"{rkey} ({status})")
    return out, closed_by, refused_by


class _Peers:
    """Labels to peer ids through the recipients route (rounds.roster_peers: LABELS_PER_CALL a call), each label asked
    once a run and at most RESOLVE_MAX_LABELS in all. The first failure is kept, and nothing is resolved after it."""

    def __init__(self, recipients: Callable[[list[str]], dict]):
        self._recipients = recipients
        self._known: dict[str, set[str]] = {}
        self.error: str | None = None

    def resolve(self, labels) -> bool:
        missing = sorted({label for label in labels if label not in self._known})
        if self.error or not missing:
            return self.error is None
        if len(self._known) + len(missing) > RESOLVE_MAX_LABELS:
            self.error = f"more than {RESOLVE_MAX_LABELS} labels to resolve"
            return False
        try:
            self._known.update(rounds.roster_peers(missing, self._recipients))
        except Exception as exc:                            # noqa: BLE001 - grouping stops; the view goes on
            self.error = f"the recipients route failed ({_why(exc)})"
            return False
        return True

    def of(self, label: str) -> frozenset:
        """The peers a resolved label maps to; one the route does not know stands for itself."""
        return frozenset(self._known.get(label) or {f"label:{label}"})


def _within(a: dict, b: dict) -> bool:
    return abs((b["_at"] - a["_at"]).total_seconds()) <= FANOUT_WINDOW_S


def _candidates(rows: list[dict], peers: _Peers) -> list[dict]:
    """The rows worth a read for fan-out: another ask from the same author PEER within the window, oldest first. One
    peer posting under two labels is one author (GitHub Codex on #547), so the authors of the rows that have any other
    ask near them are resolved to peers first. When that fails, those rows are flagged and none is read."""
    timed = sorted((r for r in rows if not r["_problems"] and isinstance(r["author"], str) and r["author"]),
                   key=lambda r: (r["_at"], r["key"]))
    near = [r for i, r in enumerate(timed) if (i > 0 and _within(timed[i - 1], r))
            or (i + 1 < len(timed) and _within(r, timed[i + 1]))]
    if not peers.resolve([r["author"].casefold() for r in near]):
        for row in near:
            row["_unchecked"] = f"{UNCHECKED_RECIPIENTS}: {peers.error}"
        return []
    by_author: dict[frozenset, list[dict]] = {}
    for row in near:
        by_author.setdefault(peers.of(row["author"].casefold()), []).append(row)
    out = []
    for group in by_author.values():                        # each already oldest first
        for i, row in enumerate(group):
            if (i > 0 and _within(group[i - 1], row)) or (i + 1 < len(group) and _within(row, group[i + 1])):
                out.append(row)
    return sorted(out, key=lambda r: (r["_at"], r["key"]))


def _hash_reads(candidates: list[dict], read, budget: dict, unread: set[str] | None, gate_since: datetime,
                blocked: str | None) -> list[dict]:
    """One signed read per candidate, oldest first, while the cap allows, and never of a message still unread or older
    than the unread gate. Only verified content is hashed; any other outcome leaves the row ungrouped, with the reason
    in `_unchecked`. Returns the rows the cap left unread."""
    capped = []
    for row in candidates:
        if blocked or unread is None:
            row["_unchecked"] = f"{UNCHECKED_UNREAD}: {blocked or UNREAD_CUT}"
            continue
        if row["_at"] < gate_since:
            row["_unchecked"] = f"{UNCHECKED_UNREAD}: {UNREAD_OLDER}"
            continue
        if row["key"] in unread:
            row["_unchecked"] = f"{UNCHECKED_UNREAD}: {UNREAD_STILL}"
            continue
        if type(row["version"]) is not int or row["version"] < 1:
            row["_unchecked"] = f"{UNCHECKED_UNREAD}: the owed list gave no version to read, so none is read"
            continue
        if budget["reads"] >= budget["max"]:
            row["_unchecked"] = f"{UNCHECKED_CAP}: the {budget['max']}-read cap"
            capped.append(row)
            continue
        budget["reads"] += 1
        try:
            # The version the owed snapshot listed, never the head: an edit landing between the unread GET and this
            # read would otherwise be fetched unseen and leave the seat's unread list (pxx P1).
            msg = read(row["key"], row["version"])
        except Exception as exc:                            # noqa: BLE001 - one row, never the whole view
            row["_unchecked"] = f"{UNCHECKED_FAILED}: {_why(exc)}"
            continue
        if not isinstance(msg, dict) or msg.get("signature_verified") is not True or not msg.get("signed_by"):
            error = msg.get("signature_error") if isinstance(msg, dict) else None
            row["_unchecked"] = f"{UNCHECKED_UNVERIFIED}: {error or 'no signature_error given'}"
            continue
        value = msg.get("_value")
        if isinstance(value, dict):                         # verified but nothing to compare: no group, base class
            row["_sha"] = content_sha256(value)
            row["_signer"] = msg["signed_by"]
            row["_to"], row["_cc"] = _labels(value.get("to")), _labels(value.get("cc"))
    return capped


def _resolve(candidates: list[dict], peers: _Peers) -> None:
    """Each hashed candidate's recipients as peers (`_recipients`, a (to, cc) pair of frozensets); a label the route
    does not know stays as itself. When the route fails, no candidate keeps its hash, so nothing is grouped."""
    hashed = [row for row in candidates if row.get("_sha")]
    if not peers.resolve({label for row in hashed for label in (*row["_to"], *row["_cc"])}):
        for row in hashed:
            row["_sha"] = None
            row["_unchecked"] = f"{UNCHECKED_RECIPIENTS}: {peers.error}"
        return

    def as_peers(names: tuple[str, ...]) -> frozenset:
        out: set[str] = set()
        for name in names:
            out |= peers.of(name)
        return frozenset(out)
    for row in hashed:
        row["_recipients"] = (as_peers(row["_to"]), as_peers(row["_cc"]))


def _groups(candidates: list[dict], peer_labels: dict[str, str]) -> list[dict]:
    by_content: dict[tuple[str, str], list[dict]] = {}
    for row in candidates:
        if row.get("_sha"):
            by_content.setdefault((row["_signer"], row["_sha"]), []).append(row)
    groups = []
    for (signer, sha), members in by_content.items():
        chain: list[dict] = []
        for row in members:                                 # already oldest first
            if chain and (row["_at"] - chain[-1]["_at"]).total_seconds() > FANOUT_WINDOW_S:
                _settle(chain, signer, sha, groups, peer_labels)
                chain = []
            chain.append(row)
        _settle(chain, signer, sha, groups, peer_labels)
    return sorted(groups, key=lambda g: g["id"])


def _settle(chain: list[dict], signer: str, sha: str, groups: list[dict], peer_labels: dict[str, str]) -> None:
    if len(chain) < 2:
        return
    first_of: dict[tuple, str] = {}
    for row in chain:
        if row["_recipients"] in first_of:
            row["_copy_of"] = first_of[row["_recipients"]]
        else:
            first_of[row["_recipients"]] = row["key"]
    fanout = len(first_of) > 1
    for row in chain:
        row["_group"] = chain[0]["key"]
        if fanout and "_copy_of" not in row:
            row["_fanout"] = True
    groups.append({"id": chain[0]["key"], "kind": "fanout" if fanout else "retransmission",
                   "signer": peer_labels.get(signer, signer), "content_sha256": sha[:16],
                   "recipient_sets": len(first_of), "keys": [row["key"] for row in chain]})


def _base(row: dict, expire_s: int) -> str:
    src = row["src_field"]
    if not (src == "to" or src.startswith("to:")):
        return "edge_only"
    if row["state"] == "open" and row["_age_s"] > expire_s:
        return "expired_unreviewed"
    return "live_work"


def reconcile(lists: dict, read: Callable[[str, int], dict], *, unread: dict, gate_since: datetime,
              anchor: Callable[[str], dict], recipients: Callable[[list[str]], dict], me_label: str | None,
              signers: dict[str, str], peer_labels: dict[str, str], closures: list[str], expire_s: int,
              max_reads: int, now: datetime) -> dict:
    """The view. `lists` holds node.owed's answer for each of STATES, and `unread` node.unread's answer from
    `gate_since` (at most UNREAD_GATE_S back); `read(key, version)` is one signed read of that exact version
    (node.read with include_value and record=False); `anchor(key)` is GET /v1/mesh/neighbours, the server's head
    version and time of a receipt; `recipients(labels)` is the recipients route; `signers` maps the peer ids whose
    receipts may close an ask to their labels; `peer_labels` names peers (the seat roster), and its labels are the
    ones a done_when is searched for."""
    rows, truncated, duplicates, fetched = _rows(lists, now)
    still_unread = unread_keys(unread)
    requested = gate_since
    effective = unread_since(unread)
    blocked = UNREAD_CUT if still_unread is None else UNREAD_NO_WINDOW if effective is None else None
    if effective is not None:
        gate_since = max(gate_since, effective)     # the window the server actually covered, never more
    budget = {"reads": 0, "max": max_reads}
    listed = {row["key"] for row in rows if row["key"]}
    receipts, closed_by, refused_by = _receipts(closures, read, anchor, budget, listed, signers, peer_labels,
                                                still_unread, gate_since, blocked)
    peers = _Peers(recipients)
    candidates = _candidates(rows, peers)
    capped = _hash_reads(candidates, read, budget, still_unread, gate_since, blocked)
    _resolve(candidates, peers)
    groups = _groups(candidates, peer_labels)
    roster = set(peer_labels.values())
    shown = []
    for row in sorted(rows, key=lambda r: (r["_at"] is None, r["_at"] or now, r["key"] or "")):
        if row["_problems"]:
            cls, evidence = "unknown", {"why": "; ".join(row["_problems"])}
        elif row["key"] in closed_by:
            cls, evidence = "closed", {"receipt": closed_by[row["key"]]}
        elif row["key"] in refused_by:
            cls, evidence = "unknown", {"why": "a receipt names it but cannot close it: "
                                               + "; ".join(refused_by[row["key"]])}
        elif row["state"] == "answered":
            cls, evidence = "answered", {"server_state": "answered"}
        elif row.get("_copy_of"):
            cls, evidence = "retransmission", {"copy_of": row["_copy_of"]}
        elif row.get("_fanout"):
            cls, evidence = "intentional_fanout", {"fanout_group": row["_group"]}
        else:
            cls = _base(row, expire_s)
            evidence = ({"src_field": row["src_field"]} if cls == "edge_only" else
                        {"server_state": row["state"], "older_than_expiry": cls == "expired_unreviewed"})
        out = {"key": row["key"], "class": cls, "state": row["state"], "author": row["author"],
               "thread_id": row["thread_id"], "recorded_at": row["recorded_at"],
               "age_h": None if row["_age_s"] is None else round(row["_age_s"] / 3600, 1),
               "src_field": row["src_field"], "evidence": evidence}
        if cls in ("intentional_fanout", "retransmission"):
            out["base_class"] = _base(row, expire_s)
        if row.get("_group"):
            out["fanout_group"] = row["_group"]
        if row.get("_unchecked"):
            out["fanout_unchecked"] = row["_unchecked"]
        if cls == "live_work":
            named = done_when_names(row["done_when"], roster, me_label)
            if named:
                out["done_when_names"] = named
        shown.append(out)
    classes = {cls: sum(1 for row in shown if row["class"] == cls) for cls in CLASSES}
    balanced = (sum(fetched.values()) == sum(classes.values()) + len(duplicates)) and len(shown) == len(rows)
    equation = (" + ".join(f"{state} {fetched[state]}" for state in STATES) + " = "
                + " + ".join(f"{cls} {classes[cls]}" for cls in CLASSES) + f" + duplicate {len(duplicates)}")
    unchecked = {why: sum(1 for row in rows if str(row.get("_unchecked") or "").startswith(why))
                 for why in (UNCHECKED_CAP, UNCHECKED_UNREAD, UNCHECKED_FAILED, UNCHECKED_UNVERIFIED,
                             UNCHECKED_RECIPIENTS)}
    cut = [state for state in STATES if truncated[state]]
    result = {"equation": equation, "balanced": balanced, "states": fetched, "classes": classes,
              "expire_s": expire_s, "max_reads": max_reads, "reads": budget["reads"], "rows": shown,
              "receipts": receipts, "fanout_groups": groups, "truncated": truncated,
              "unread_gate": {"checked": blocked is None, "cut": still_unread is None,
                              "since": gate_since.isoformat(), "requested": requested.isoformat(),
                              **({"blocked": blocked} if blocked else {})},
              "fanout_unchecked": {"cap": unchecked[UNCHECKED_CAP], "unread": unchecked[UNCHECKED_UNREAD],
                                   "failed": unchecked[UNCHECKED_FAILED],
                                   "unverified": unchecked[UNCHECKED_UNVERIFIED],
                                   "recipients": unchecked[UNCHECKED_RECIPIENTS]},
              "not_read": None, "duplicate_keys": duplicates}
    unread_receipts = [r["key"] for r in receipts if r["status"] == "not_read" and r["detail"].endswith("read cap")]
    if capped or unread_receipts:
        result["not_read"] = {
            "fanout_candidates": len(capped), "receipts": unread_receipts,
            "oldest": capped[0]["key"] if capped else None, "newest": capped[-1]["key"] if capped else None,
            "note": (f"the {max_reads}-read cap: a fan-out candidate it left unread keeps the class it has without a "
                     "fan-out check (fanout_unchecked); a receipt it left unread closes nothing")}
    if cut:
        result["cut"] = (f"the server cut the {', '.join(cut)} list{'s' if len(cut) > 1 else ''} at the limit: the "
                         "counts are over the newest rows it returned; --since or --thread narrows the window")
    return result


def _age(hours) -> str:
    if hours is None:
        return "age ?"
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.1f}d"


def _row_text(row: dict) -> str:
    ev = row["evidence"]
    parts = [str(row["key"]), f"from {row['author']}", _age(row["age_h"]), row["state"]]
    if row["thread_id"]:
        parts.append(f"thread {row['thread_id']}")
    if "receipt" in ev:
        parts.append(f"closed by {ev['receipt']}")
    if "copy_of" in ev:
        parts.append(f"copy of {ev['copy_of']}")
    if "fanout_group" in ev:
        parts.append(f"group {ev['fanout_group']}")
    if "why" in ev:
        parts.append(ev["why"])
    if "src_field" in ev:
        parts.append(f"edge from {ev['src_field']}")
    if row.get("base_class"):
        parts.append(f"otherwise {row['base_class']}")
    if row.get("done_when_names"):
        parts.append("done_when names " + ", ".join(row["done_when_names"]))
    if row.get("fanout_unchecked"):
        parts.append(f"fan-out not checked ({row['fanout_unchecked']})")
    return ", ".join(parts)


def render(result: dict, *, since: str | None, thread: str | None, answered_list: str) -> str:
    """The text view: the balance first, then each class's rows oldest first (answered as a count and the command
    that lists them), the receipts, the groups, and what was not read or was cut. Keys and routing only."""
    head = f"owed --reconcile: {result['equation']}"
    lines = [head if result["balanced"] else head + "  NOT BALANCED: a row was lost or counted twice"]
    lines.append(f"window: since {since or 'all'}" + (f", thread {thread}" if thread else "")
                 + f"; expiry {result['expire_s'] // 3600}h; {result['reads']} of {result['max_reads']} signed reads")
    if result["unread_gate"].get("blocked"):
        lines.append(f"unread gate: {result['unread_gate']['blocked']}")
    for cls in CLASSES:
        rows = [row for row in result["rows"] if row["class"] == cls]
        if not rows:
            continue
        if cls == "answered":
            lines.append(f"answered {len(rows)}: replied to directly, not settled: `{answered_list}` lists them")
            continue
        lines.append(f"{cls} {len(rows)}, oldest first:")
        lines.extend("  " + _row_text(row) for row in rows)
    for receipt in result["receipts"]:
        text = f"receipt {receipt['key']}: {receipt['status']}"
        if receipt.get("closes_listed"):
            text += f", closes {len(receipt['closes_listed'])} listed"
        if receipt.get("names_unlisted"):
            text += f", names {len(receipt['names_unlisted'])} not listed"
        if receipt.get("detail"):
            text += f" ({receipt['detail']})"
        lines.append(text)
    for group in result["fanout_groups"]:
        lines.append(f"{group['kind']} group {group['id']}: {len(group['keys'])} asks from {group['signer']}, "
                     f"{group['recipient_sets']} recipient set{'s' if group['recipient_sets'] != 1 else ''}, "
                     f"content sha256 {group['content_sha256']}")
    if result["not_read"]:
        nr = result["not_read"]
        if nr["fanout_candidates"]:
            lines.append(f"not read (the {result['max_reads']}-read cap): {nr['fanout_candidates']} fan-out "
                         f"candidates, oldest {nr['oldest']}, newest {nr['newest']}")
        if nr["receipts"]:
            lines.append(f"not read (the {result['max_reads']}-read cap): receipts {', '.join(nr['receipts'])}")
    skipped = result["fanout_unchecked"]
    if skipped["unread"]:
        lines.append(f"fan-out candidates the unread gate kept from a read (still unread, or older than its window): "
                     f"{skipped['unread']}; they keep their class, flagged")
    bad = skipped["failed"] + skipped["unverified"] + skipped["recipients"]
    if bad:
        lines.append(f"fan-out checks that could not run: {bad} (read failed {skipped['failed']}, unverified "
                     f"{skipped['unverified']}, recipients not resolved {skipped['recipients']}); those rows keep "
                     "their class, flagged")
    if result.get("cut"):
        lines.append(f"cut: {result['cut']}")
    if result["duplicate_keys"]:
        lines.append(f"listed twice, counted once in the classes and once as duplicate: "
                     f"{', '.join(result['duplicate_keys'])}")
    return "\n".join(lines)
