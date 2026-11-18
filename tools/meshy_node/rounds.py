"""`meshy_node round`: which seats have posted a round's fixed first line on one thread (SD 255; kxxx's round-1 item 5).

A round runs on one thread, and each seat's reply opens with a fixed first line such as
`VERDICT: INFO; ITEM: allhands-improve-r1; SEAT: txxxx; N: 6`. Collating it by hand missed late posts, so this reads
the thread and says, per roster seat, which post counts, and why every other post does not. Routing and first lines
only: it never posts, never collates bodies, and needs no server change (design 187da2; pxx, txxxx).

What counts, in order:
- the WHOLE thread (conformance.whole_thread: the route answers oldest first, so no limit is sent, and a truncated
  answer refuses rather than read a prefix as the thread);
- posts whose author label belongs to a roster seat's PEER (the recipients route maps labels to peer ids), each read
  signed (node.read), newest first, at most `max_reads` of them, an edited post's version-1 read included and made in
  its seat's own turn; what the cap leaves unread is named. The command reads with record=False (SD 265), so a
  round notes no post in the local fetched or read ledgers and never uses up a directive's one showing. On the
  server each read is still a completed fetch, so a post addressed to this seat leaves its server unread list;
- a read with signature_verified true, signed_by a roster peer (pxx B: a seat posting under a second label still
  counts as itself, and a label another peer holds never counts);
- a first line that parses as `KEY: value; KEY: value`, after a BOM and blank lines, whose ITEM is exactly `item`
  (txxxx: a seat's later collation on the same thread has another ITEM and must not replace its reply) and,
  with `prefix`, whose first key is `prefix`;
- a SEAT field, when present, equal to the label the roster gives the signer; a post without one counts under its
  signer (the signature names the seat; SEAT is a claim to check, not the identity).
Each seat's LATEST counted post wins, by the server's recorded_at and then the key (never by the msg/<ms> in the key,
which is the author's clock); an earlier one is shown as superseded. Everything else is listed by key: refused,
unparsed, other item, unreadable, unverified, not on the roster, not read.
"""
from __future__ import annotations

import re
from typing import Callable

#: Reads made at most, newest post first (pxx E: about 21 for a 13-seat round; one thread had 105 posts).
DEFAULT_MAX_READS = 60
#: Labels the recipients route takes in one call.
LABELS_PER_CALL = 10
_FIELD = re.compile(r"([A-Z][A-Z0-9_]*):\s*(.*)\Z", re.S)
#: A `;` ends a field only when an UPPER-CASE key and its colon follow, so `ITEM: a; b; SEAT: x` keeps `a; b`.
_SPLIT = re.compile(r";\s*(?=[A-Z][A-Z0-9_]*:)")


def first_line(body: object) -> str:
    """The first non-blank line of `body`, a leading BOM and whitespace removed; "" when there is none."""
    text = body if isinstance(body, str) else ""
    for line in text.lstrip("\ufeff").splitlines():
        if line.strip():
            return line.strip().lstrip("\ufeff").strip()
    return ""


def parse(line: str) -> dict[str, str] | None:
    """`KEY: value; KEY: value` as {KEY: value}, in order; None when any part is not a field or a key repeats."""
    if not line:
        return None
    fields: dict[str, str] = {}
    for part in _SPLIT.split(line):
        m = _FIELD.fullmatch(part.strip())
        if m is None or m.group(1) in fields:
            return None
        fields[m.group(1)] = m.group(2).strip()
    return fields


def roster_peers(labels: list[str], recipients: Callable[[list[str]], dict]) -> dict[str, set[str]]:
    """{label: peer ids} from the recipients route, LABELS_PER_CALL labels a call. A label it does not know maps to
    no peer, so it can only be MISSING."""
    out: dict[str, set[str]] = {label: set() for label in labels}
    for i in range(0, len(labels), LABELS_PER_CALL):
        got = recipients(labels[i:i + LABELS_PER_CALL]) or {}
        for row in got.get("recipients") or []:
            if isinstance(row, dict) and row.get("label") in out:
                out[row["label"]] |= {p["peer_id"] for p in row.get("peers") or []
                                      if isinstance(p, dict) and isinstance(p.get("peer_id"), str)}
    return out


def collate(hits: list[dict], seats: list[str], seat_peers: dict[str, set[str]], author_peers: dict[str, set[str]],
            read: Callable[..., dict], *, item: str, prefix: str | None = None,
            max_reads: int = DEFAULT_MAX_READS) -> dict:
    """The round's answer from the thread's `hits` (routing rows: key, author, recorded_at, version). `seat_peers`
    maps each roster seat to its peers; `author_peers` maps every author label in the thread to its peers. `read`
    is the signed read of one key, `read(key)` for its current version and `read(key, version=1)` for its first; it
    may raise, and that post is unreadable."""
    seats = list(dict.fromkeys(seats))                         # pxx P2-2: a repeated seat walked one queue twice
    peer_seat = {p: seat for seat in seats for p in seat_peers.get(seat, set())}
    rows = [h for h in hits if isinstance(h, dict) and isinstance(h.get("key"), str)]
    out = {"item": item, "prefix": prefix, "seats": len(seats), "thread_posts": len(rows), "max_reads": max_reads,
           "posted": {}, "superseded": [], "refused": [], "unparsed": [], "other_item": [], "unreadable": [],
           "unverified": [], "not_on_roster": [], "order_unchecked": [], "not_read": [], "reads": 0}
    queues: dict[str, list[dict]] = {seat: [] for seat in seats}
    outsiders: dict[str, int] = {}
    mine = []
    for h in rows:
        owners = {peer_seat[p] for p in author_peers.get(str(h.get("author")), set()) if p in peer_seat}
        if owners:
            mine.append((h, owners))
        else:
            outsiders[str(h.get("author"))] = outsiders.get(str(h.get("author")), 0) + 1
    out["not_on_roster"] = [{"author": a, "posts": n} for a, n in sorted(outsiders.items())]
    # pxx P2-1: the listing stamps a post with its HEAD version's time (normalize.project_row), so an edit of an
    # older post would pass a later correction. An edited post is placed by its version 1. That read is made lazily,
    # in the turn of the seat whose queue the post heads, under the fair scheduler below: reading every edited post's
    # version 1 first spent the shared cap, so 60 edited posts from one seat left another seat's reply unread (GitHub
    # Codex on e8f42783, pxx). The head time is an upper bound (the first version is older), so a post queued
    # by it is never placed too late; one whose first version cannot be read keeps that place and is named.
    entries = [({**h}, owners) for h, owners in mine]           # copies: placing one changes its time in place
    entries.sort(key=lambda pair: _when(pair[0]), reverse=True)  # newest first, an edited post by its head time
    for h, owners in entries:
        # pxx P2-3: a label two roster seats' peers share goes in BOTH queues (the same entry, read once, by
        # key), so neither seat settles on an older post while its newer one waits in the other's queue.
        for seat in seats:
            if seat in owners:
                queues[seat].append(h)
    unplaced = {h["key"] for h, _ in entries if str(h.get("version") or "1") != "1"}
    tried: dict[str, int] = {}                                 # per queue seat: posts read that could not be judged
    judged: dict[str, tuple[str, dict]] = {}                   # key -> verdict: each key is read once
    done: set[str] = set()                                     # keys whose verdict is already in `out`

    def place(h: dict) -> None:
        """One read of an edited post's version 1, in this turn; the post moves to its true place in every queue
        that holds it, and the seat takes its next post on its next turn."""
        unplaced.discard(h["key"])
        out["reads"] += 1
        try:
            first = read(h["key"], version=1)
            at = first.get("recorded_at") if isinstance(first, dict) else None
        except Exception:                                      # noqa: BLE001 - its edit's time stands, and it is named
            at = None
        if not at:
            out["order_unchecked"].append(h["key"])
            return
        h["head_recorded_at"], h["recorded_at"] = h.get("recorded_at"), at
        for queue in queues.values():
            if any(e is h for e in queue):
                queue.sort(key=_when, reverse=True)

    def take(seat: str) -> None:
        if queues[seat][0]["key"] in unplaced:
            place(queues[seat][0])
            return
        h = queues[seat].pop(0)
        if h["key"] not in judged:
            out["reads"] += 1
            judged[h["key"]] = _judge(h, read, peer_seat, item=item, prefix=prefix)
        name, row = judged[h["key"]]
        if name in ("unreadable", "unverified"):
            tried[seat] = tried.get(seat, 0) + 1
        if h["key"] in done:
            return
        done.add(h["key"])
        if name != "counted":
            out[name].append(row)
            return
        held = out["posted"].get(row["seat"])
        if held is None:
            out["posted"][row["seat"]] = row
        elif _when(row) > _when(held):                         # a later one, reached through another seat's queue
            out["superseded"].append({**held, "replaced_by": row["key"]})
            out["posted"][row["seat"]] = row
        else:
            out["superseded"].append({**row, "replaced_by": held["key"]})

    def can_take(seat: str) -> bool:
        """A judged key costs no read, so it goes past the cap. A key is placed before it is judged, in every queue,
        so a judged key needs no version-1 read either; any other head costs one read."""
        return bool(queues[seat]) and (queues[seat][0]["key"] in judged or out["reads"] < max_reads)

    def settled(seat: str) -> bool:
        """Counted, with no unread post in its own queue newer than the counted one. A count can arrive through
        another seat's queue (a shared label) while the seat's own newer post waits unread (pxx)."""
        held = out["posted"].get(seat)
        return held is not None and not any(h["key"] not in judged and _when(h) > _when(held) for h in queues[seat])

    # Phase 1, one read per seat in turn, newest first, a seat stopping once it is settled: under the cap the reads
    # go to seats not yet settled, so one busy seat's later posts cannot spend the cap before the others' replies
    # are read (a first run, newest first over all, read 30 follow-ups and none of the 12 replies).
    while True:
        open_seats = [s for s in seats if not settled(s) and can_take(s)]
        if not open_seats:
            break
        for seat in open_seats:
            if not settled(seat) and can_take(seat):
                take(seat)
    # Phase 2, with what the cap leaves: the settled seats' older posts, so an earlier reply shows as superseded.
    while any(can_take(s) for s in seats):
        for seat in seats:
            if can_take(seat):
                take(seat)
    left = {h["key"] for s in seats for h in queues[s]} - set(judged)
    out["not_read"] = [h["key"] for h, _ in sorted(entries, key=lambda pair: _when(pair[0]), reverse=True)
                       if h["key"] in left]
    # pxx D: a seat whose posts could not be read or did not verify has not been shown NOT to post
    unsettled = {s for s in seats if any(h["key"] in left for h in queues[s])}
    out["missing"] = [s for s in seats if s not in out["posted"] and not tried.get(s) and s not in unsettled]
    out["unreadable_only"] = [s for s in seats if s not in out["posted"] and tried.get(s)]
    out["not_settled"] = [s for s in seats if s not in out["posted"] and not tried.get(s) and s in unsettled]
    # A seat counted while a newer post of its own was still unread when the cap ran out keeps its row, flagged, and
    # is not settled either: the newer post could replace it (pxx (b)).
    for seat in seats:
        if seat in out["posted"] and not settled(seat):
            held = out["posted"][seat]
            held["newer_unread"] = sum(1 for h in queues[seat] if h["key"] not in judged and _when(h) > _when(held))
            out["not_settled"].append(seat)
    return out


def _when(row: dict) -> tuple[str, str]:
    """The server's recorded_at, then the key: never the msg/<ms> alone, which is the author's clock (pxx A)."""
    return str(row.get("recorded_at") or ""), str(row.get("key") or "")


def _judge(h: dict, read, peer_seat: dict[str, str], *, item: str, prefix: str | None) -> tuple[str, dict]:
    """One post: ("counted", row) or the list it belongs on, with its row."""
    key = h["key"]
    try:
        msg = read(key)
    except Exception as exc:                                   # noqa: BLE001 - a post that cannot be read is listed
        return "unreadable", {"key": key, "author": h.get("author"), "why": type(exc).__name__ + ": " + str(exc)[:120]}
    if not isinstance(msg, dict) or msg.get("signature_verified") is not True:
        return "unverified", {"key": key, "author": h.get("author")}
    seat = peer_seat.get(str(msg.get("signed_by")))
    if seat is None:                                           # the label maps to a roster peer, the signer does not
        return "refused", {"key": key, "author": h.get("author"), "why": "signed by a peer no roster seat holds"}
    line = first_line(msg.get("body"))
    fields = parse(line)
    row = {"key": key, "seat": seat, "recorded_at": h.get("recorded_at"), "first_line": line[:300]}
    version = msg.get("version", h.get("version"))
    if str(version) not in ("1", "None"):
        row["version"] = version
    if fields is None or (prefix is not None and next(iter(fields)) != prefix):
        return "unparsed", row
    if fields.get("ITEM") != item:
        return "other_item", {**row, "item": fields.get("ITEM")}
    if "SEAT" in fields and fields["SEAT"] != seat:
        return "refused", {**row, "why": f"SEAT {fields['SEAT']!r}, but signed by {seat}'s peer"}
    if "SEAT" not in fields:
        row["seat_field"] = "absent: counted under its signer"
    if "N" in fields:
        row["n"] = fields["N"]
    return "counted", row


def render(result: dict, thread: str) -> str:
    """The text view: one line per posted seat, then every list that is not empty, by key."""
    posted = result["posted"]
    lines = [f"round on {thread}, ITEM {result['item']}: {len(posted)} of {result['seats']} seats posted "
             f"({result['thread_posts']} posts in the thread, {result['reads']} read)"]
    for seat in sorted(posted):
        r = posted[seat]
        extra = (f"  N {r['n']}" if "n" in r else "") + (f"  v{r['version']}" if "version" in r else "")
        extra += "  (no SEAT field)" if "seat_field" in r else ""
        extra += f"  ({r['newer_unread']} newer post(s) unread)" if r.get("newer_unread") else ""
        lines.append(f"  {seat:<16} {r['key']}  {r.get('recorded_at') or '?'}{extra}")
    # A seat that posted only another item or an unparsed line has not posted the reply; say where its post is (pxx
    # e9adc1 INFO), so a mistyped ITEM is not read as silence.
    seen = {}
    for name in ("unparsed", "other_item"):
        for r in result[name]:
            seen.setdefault(r.get("seat"), f"see {name.replace('_', ' ')}: {r['key']}")
    missing = [s + (f" ({seen[s]})" if s in seen else "") for s in result["missing"]]
    for title, names in (("not posted", missing), ("only an unreadable or unverified post", result["unreadable_only"]),
                         ("not settled within the read cap", result["not_settled"])):
        if names:
            lines.append(f"{title} ({len(names)}): {', '.join(names)}")
    for name in ("superseded", "refused", "unparsed", "other_item", "unreadable", "unverified"):
        for r in result[name]:
            why = r.get("why") or (f"replaced by {r['replaced_by']}" if "replaced_by" in r else
                                   f"ITEM {r['item']!r}" if name == "other_item" else r.get("first_line", "")[:80])
            lines.append(f"{name.replace('_', ' ')}: {r['key']} {r.get('seat') or r.get('author') or ''}: {why}")
    if result["not_on_roster"]:
        lines.append("not on the roster (not read): " + ", ".join(f"{r['author']} ({r['posts']})"
                                                                   for r in result["not_on_roster"]))
    if result.get("order_unchecked"):
        lines.append("edited, placed by the edit's time (its first version could not be read): "
                     + ", ".join(result["order_unchecked"]))
    if result["not_read"]:
        lines.append(f"not read (the {result['max_reads']}-read cap): {len(result['not_read'])} posts, newest "
                     f"{result['not_read'][0]}, oldest {result['not_read'][-1]}")
    return "\n".join(lines)
