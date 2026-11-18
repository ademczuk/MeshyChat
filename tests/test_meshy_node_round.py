"""`python -m tools.meshy_node round` (tools/meshy_node/rounds.py): which roster seats posted a round's first line.

No database and no server: the thread, the recipients route and the signed reads are stand-ins. Design 187da2 with
pxx's read a0adab (A-F) and txxxx's b04d7a folded in; a first run on the real round-1 thread is in the PR.

Run:  pytest tests/test_meshy_node_round.py -q     (needs no database)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, conformance, node, rounds  # noqa: E402

PEERS = {"txxxx": "p-txxxx", "pxx": "p-pxx", "kxxx": "p-kxxx", "uxxx": "p-uxxx"}


def _hit(key, author, at):
    return {"key": key, "author": author, "recorded_at": f"2026-11-07T{at}+00:00", "version": "1"}


def _msg(body, signer, *, verified=True, version=1):
    return {"body": body, "signed_by": PEERS.get(signer, signer), "signature_verified": verified, "version": version}


def _run(hits, bodies, *, seats=("txxxx", "pxx", "kxxx", "uxxx"), item="r1", prefix=None, max_reads=60,
         author_peers=None, firsts=None):
    seat_peers = {s: {PEERS[s]} for s in dict.fromkeys(seats)}
    labels = author_peers or {}
    authors = {h["author"]: labels.get(h["author"], {PEERS[h["author"]]} if h["author"] in PEERS else set())
               for h in hits}
    reads = []

    def read(key, version=None):
        """The signed read: the current version, or with version=1 the first, whose recorded_at `firsts` gives."""
        reads.append(key if version is None else f"{key}@v{version}")
        if version is not None:
            return {"recorded_at": (firsts or {})[key], "version": version}
        got = bodies[key]
        if isinstance(got, Exception):
            raise got
        return got
    return rounds.collate(hits, list(seats), seat_peers, authors, read, item=item, prefix=prefix,
                          max_reads=max_reads), reads


def test_the_first_line_parses_after_a_bom_and_blank_lines_and_a_semicolon_inside_a_value_stays():
    """pxx F: `ITEM: a; b; SEAT: x` keeps `a; b`, and `ITEM: a;b` keeps `a;b`; a `;` ends a field only before an
    UPPER-CASE key and its colon. MUTANT: split on every `;`; both values break. MUTANT: keep the BOM; no field."""
    assert rounds.first_line("\ufeff\n\n  VERDICT: GO; ITEM: x  \nsecond") == "VERDICT: GO; ITEM: x"
    assert rounds.parse("ITEM: a; b; SEAT: x") == {"ITEM": "a; b", "SEAT": "x"}
    assert rounds.parse("ITEM: a;b") == {"ITEM": "a;b"}
    assert rounds.parse("VERDICT: GO; ITEM: r1;SEAT: pxx; N: 6") == {"VERDICT": "GO", "ITEM": "r1", "SEAT": "pxx",
                                                                    "N": "6"}
    assert rounds.parse(rounds.first_line("\ufeffVERDICT: INFO; ITEM: r1")) == {"VERDICT": "INFO", "ITEM": "r1"}
    for bad in ("", "txxxx to oxxxxxx, on 397e4f: done", "ITEM: a; ITEM: b", "verdict: go"):
        assert rounds.parse(bad) is None, bad


def test_a_later_post_with_another_item_never_replaces_the_reply():
    """txxxx b04d7a and pxx c3331a, with txxxx's REAL pair from the round-1 thread (<thread>):
    28b4de is the reply (ITEM allhands-improve-r1, N 6) and e49b3c the later collation (ITEM
    allhands-improve-r1-collation, N 27). Only the exact ITEM counts; the collation is under other items.
    MUTANT: match ITEM by prefix (startswith); the collation replaces the reply and N reads 27."""
    reply, coll = "msg/1793882228621-txxxx-28b4de", "msg/1793894636483-txxxx-e49b3c"
    hits = [_hit(reply, "txxxx", "13:18:08.418474"), _hit(coll, "txxxx", "16:44:56.403517")]
    bodies = {reply: _msg("VERDICT: INFO; ITEM: allhands-improve-r1; SEAT: txxxx; N: 6", "txxxx"),
              coll: _msg("VERDICT: INFO; ITEM: allhands-improve-r1-collation; SEAT: txxxx; N: 27", "txxxx")}
    got, _ = _run(hits, bodies, item="allhands-improve-r1", prefix="VERDICT")
    assert got["posted"]["txxxx"]["key"] == reply and got["posted"]["txxxx"]["n"] == "6"
    assert [(r["key"], r["item"]) for r in got["other_item"]] == [(coll, "allhands-improve-r1-collation")]
    assert got["superseded"] == []


def test_the_latest_counted_post_wins_by_the_servers_time_not_the_key_and_the_earlier_is_shown():
    """pxx A: "latest" is the server's recorded_at, then the key, never the msg/<ms> (the author's clock). Here
    the earlier post carries the larger key. An edit shows its version. MUTANT: order by key; the wrong one wins.
    MUTANT: drop superseded; the earlier key is not shown."""
    hits = [_hit("msg/9-pxx-first", "pxx", "13:15:00"), _hit("msg/1-pxx-correction", "pxx", "14:00:00")]
    bodies = {"msg/9-pxx-first": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 5", "pxx"),
              "msg/1-pxx-correction": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 6", "pxx", version=2)}
    got, _ = _run(hits, bodies)
    assert got["posted"]["pxx"]["key"] == "msg/1-pxx-correction" and got["posted"]["pxx"]["version"] == 2
    assert [(r["key"], r["replaced_by"]) for r in got["superseded"]] == [("msg/9-pxx-first", "msg/1-pxx-correction")]


def test_a_post_counts_by_its_signing_peer_never_by_its_label():
    """pxx B: a seat posting under a second label still counts as itself; a label held by a peer no roster seat
    holds never counts; SEAT is checked against the signer's roster label. MUTANT: count by the listing's author
    label; kxxx-prod is not on the roster and kxxx reads not posted. MUTANT: skip the SEAT check; pxx's line
    claiming uxxx counts."""
    hits = [_hit("msg/1-kxxx-prod-a", "kxxx-prod", "13:00:00"), _hit("msg/2-uxxx-b", "uxxx", "13:01:00"),
            _hit("msg/3-pxx-c", "pxx", "13:02:00")]
    bodies = {"msg/1-kxxx-prod-a": _msg("VERDICT: INFO; ITEM: r1; SEAT: kxxx; N: 6", "kxxx"),
              "msg/2-uxxx-b": _msg("VERDICT: INFO; ITEM: r1; SEAT: uxxx; N: 5", "p-stranger"),
              "msg/3-pxx-c": _msg("VERDICT: INFO; ITEM: r1; SEAT: uxxx; N: 4", "pxx")}
    got, _ = _run(hits, bodies, author_peers={"kxxx-prod": {"p-kxxx"}})
    assert sorted(got["posted"]) == ["kxxx"] and got["posted"]["kxxx"]["key"] == "msg/1-kxxx-prod-a"
    refused = {r["key"]: r["why"] for r in got["refused"]}
    assert set(refused) == {"msg/2-uxxx-b", "msg/3-pxx-c"}
    assert "no roster seat" in refused["msg/2-uxxx-b"] and "SEAT 'uxxx'" in refused["msg/3-pxx-c"]
    assert sorted(got["missing"]) == ["pxx", "txxxx", "uxxx"]


def test_an_unparsed_line_is_listed_by_key_and_a_line_without_seat_counts_under_its_signer():
    """pxx C: a first line that is not KEY: value (a stand-in's token line) is listed as unparsed, never skipped
    in silence. A line without SEAT counts under its signer: the signature names the seat, SEAT is only a claim
    to check. With --prefix, a line whose first key is another is unparsed too.
    MUTANT: drop unparsed rows; the token line disappears. MUTANT: require SEAT; uxxx reads not posted."""
    hits = [_hit("msg/1-txxxx-token", "txxxx", "13:00:00"), _hit("msg/2-uxxx-noseat", "uxxx", "13:01:00"),
            _hit("msg/3-pxx-result", "pxx", "13:02:00")]
    bodies = {"msg/1-txxxx-token": _msg("token 7f3a: RESULT follows", "txxxx"),
              "msg/2-uxxx-noseat": _msg("VERDICT: INFO; ITEM: r1; N: 5", "uxxx"),
              "msg/3-pxx-result": _msg("RESULT: GO; ITEM: r1; SEAT: pxx", "pxx")}
    got, _ = _run(hits, bodies, prefix="VERDICT")
    assert sorted(r["key"] for r in got["unparsed"]) == ["msg/1-txxxx-token", "msg/3-pxx-result"]
    assert got["posted"]["uxxx"]["seat_field"].startswith("absent") and "pxx" not in got["posted"]
    assert sorted(got["missing"]) == ["kxxx", "pxx", "txxxx"], "an unparsed post is listed; its seat has not posted"


def test_a_seat_whose_posts_cannot_be_read_or_verified_is_never_shown_as_not_posted():
    """pxx D: a read that errs (a tombstone, a redaction, a server error) or a signature that does not verify is
    listed with its key, and that seat is "only an unreadable or unverified post", not MISSING.
    MUTANT: count unreadable seats as missing; txxxx is in missing."""
    hits = [_hit("msg/1-txxxx-gone", "txxxx", "13:00:00"), _hit("msg/2-pxx-forged", "pxx", "13:01:00")]
    bodies = {"msg/1-txxxx-gone": node.ServerSaid(410, {"error": "E_TOMBSTONED"}),
              "msg/2-pxx-forged": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 6", "pxx", verified=False)}
    got, _ = _run(hits, bodies)
    assert [r["key"] for r in got["unreadable"]] == ["msg/1-txxxx-gone"]
    assert [r["key"] for r in got["unverified"]] == ["msg/2-pxx-forged"]
    assert sorted(got["unreadable_only"]) == ["pxx", "txxxx"] and sorted(got["missing"]) == ["kxxx", "uxxx"]


def test_the_read_cap_goes_to_seats_not_yet_settled_and_names_what_it_left():
    """pxx E, and the first real run: newest first over the whole thread, a 30-read cap went to one seat's later
    posts and read none of the replies. Now each unsettled seat gets one read in turn, a seat stops at its latest
    counted post, and what the cap left is named. MUTANT: read newest first over all; pxx's reply is not read.
    MUTANT: leave out not_read; the cap is silent."""
    hits = [_hit(f"msg/{i:02d}-txxxx-later", "txxxx", f"15:{i:02d}:00") for i in range(10)]
    hits += [_hit("msg/90-txxxx-reply", "txxxx", "13:00:00"), _hit("msg/91-pxx-reply", "pxx", "13:01:00")]
    bodies = {h["key"]: _msg("txxxx to oxxxxxx, on 397e4f: done", "txxxx") for h in hits}
    bodies["msg/90-txxxx-reply"] = _msg("VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 6", "txxxx")
    bodies["msg/91-pxx-reply"] = _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 6", "pxx")
    got, reads = _run(hits, bodies, seats=("txxxx", "pxx"), max_reads=4)
    assert reads[:2] == ["msg/09-txxxx-later", "msg/91-pxx-reply"], "one read for each unsettled seat, in turn"
    assert sorted(got["posted"]) == ["pxx"] and got["not_settled"] == ["txxxx"] and got["missing"] == []
    assert len(got["not_read"]) == 8 and got["not_read"][-1] == "msg/90-txxxx-reply"
    got, _ = _run(hits, bodies, seats=("txxxx", "pxx"), max_reads=20)
    assert sorted(got["posted"]) == ["pxx", "txxxx"] and got["not_read"] == [] and got["reads"] == 12


def test_the_roster_maps_labels_ten_a_call_and_an_outsider_is_named_not_read():
    """The recipients route takes ten labels a call; a post by an author that maps to no roster peer is counted
    by name and never read (pxx a0adab (1)). MUTANT: one call for all labels; the call sizes differ."""
    calls = []

    def recipients(labels):
        calls.append(list(labels))
        return {"recipients": [{"label": lb, "peers": [{"peer_id": "p-" + lb}]} for lb in labels]}
    labels = [f"seat{i}" for i in range(23)]
    got = rounds.roster_peers(labels, recipients)
    assert [len(c) for c in calls] == [10, 10, 3] and got["seat7"] == {"p-seat7"}
    result, reads = _run([_hit("msg/1-oxxxxxx-call", "oxxxxxx", "12:00:00")], {}, seats=("txxxx",))
    assert result["not_on_roster"] == [{"author": "oxxxxxx", "posts": 1}] and reads == []


def test_the_command_reads_the_whole_thread_and_a_truncated_one_refuses(tmp_path, monkeypatch, capsys):
    """The command sends no limit (the route answers oldest first, so a limit drops the newest posts, #539), and a
    truncated answer exits 1 rather than read a prefix. MUTANT: pass limit=50 to node.thread; the stand-in
    sees it. MUTANT: catch ThreadTruncated and go on; the exit is 0."""
    state_file = tmp_path / "state.json"
    asked = []
    hits = [_hit("msg/1-txxxx-reply", "txxxx", "13:00:00")]
    monkeypatch.setattr(cli.node, "load_state", lambda path, base_url: {"base_url": base_url})
    monkeypatch.setattr(node, "thread", lambda path, state, **kw: asked.append(kw) or {"hits": hits,
                                                                                      "truncated": False})
    monkeypatch.setattr(node, "recipients", lambda path, state, labels: {"recipients": [
        {"label": lb, "peers": [{"peer_id": PEERS.get(lb, "p-x")}]} for lb in labels]})
    recorded = []
    monkeypatch.setattr(node, "read", lambda path, state, key, version=None, record=True: recorded.append(record) or _msg(
        "VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 6", "txxxx"))
    argv = ["round", "--base-url", "http://127.0.0.1:9", "--state-file", str(state_file), "--thread", "allhands-r1",
            "--seats", "txxxx,pxx", "--item", "r1", "--json"]
    assert cli.main(argv) == 0
    shown = json.loads(capsys.readouterr().out)
    assert asked == [{"thread_id": "allhands-r1"}] and shown["posted"]["txxxx"]["key"] == "msg/1-txxxx-reply"
    # SD 265: a round's reads only count, so none is recorded as read or fetched, and no directive is used up.
    # MUTANT: drop record=False from _round's read; the stand-in sees record=True.
    assert recorded == [False]
    assert shown["missing"] == ["pxx"] and shown["thread"] == "allhands-r1" and shown["prefix"] == "VERDICT"
    # pxx c3331a: --prefix defaults to VERDICT, so a stand-in's RESULT line counts only when asked for
    monkeypatch.setattr(node, "read", lambda path, state, key, version=None, record=True: _msg(
        "RESULT: GO; ITEM: r1; SEAT: txxxx", "txxxx"))
    assert cli.main(argv) == 0 and json.loads(capsys.readouterr().out)["posted"] == {}
    assert cli.main([*argv, "--prefix", "RESULT"]) == 0
    assert json.loads(capsys.readouterr().out)["posted"]["txxxx"]["key"] == "msg/1-txxxx-reply"
    assert cli.main([*argv, "--prefix", "any"]) == 0
    assert json.loads(capsys.readouterr().out)["prefix"] is None
    monkeypatch.setattr(node, "thread", lambda path, state, **kw: {"hits": hits, "truncated": True})
    assert cli.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "thread_truncated"
    assert issubclass(conformance.ThreadTruncated, RuntimeError)


def test_the_text_view_names_every_list(capsys):
    """render: a line per posted seat, then each non-empty list by key, and the cap when it left posts unread."""
    hits = [_hit("msg/1-txxxx-reply", "txxxx", "13:00:00"), _hit("msg/2-pxx-token", "pxx", "13:01:00"),
            _hit("msg/3-x-call", "oxxxxxx", "12:00:00")]
    bodies = {"msg/1-txxxx-reply": _msg("VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 6", "txxxx"),
              "msg/2-pxx-token": _msg("token 12ab", "pxx")}
    got, _ = _run(hits, bodies, seats=("txxxx", "pxx", "kxxx"))
    text = rounds.render(got, "allhands-r1")
    assert text.splitlines()[0].startswith("round on allhands-r1, ITEM r1: 1 of 3 seats posted")
    assert "  txxxx " in text and "N 6" in text
    assert "not posted (2): pxx (see unparsed: msg/2-pxx-token), kxxx" in text, "pxx e9adc1 INFO: say where it is"
    assert "unparsed: msg/2-pxx-token pxx" in text and "not on the roster (not read): oxxxxxx (1)" in text


def test_an_edited_post_keeps_the_place_of_its_first_version(capsys):
    """pxx e9adc1 P2-1: the listing stamps a post with its HEAD version's time (normalize.project_row), so editing an
    older reply moved it past a later correction. A v1 at 13:00, B v1 at 14:00, A edited to v2 at 15:00: B wins. One
    read of version 1 per edited post places it. MUTANT: skip the version-1 read; A wins by its edit's time."""
    hits = [{**_hit("msg/1-pxx-a", "pxx", "15:00:00"), "version": "2"}, _hit("msg/2-pxx-b", "pxx", "14:00:00")]
    bodies = {"msg/1-pxx-a": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 5", "pxx", version=2),
              "msg/2-pxx-b": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 6", "pxx")}
    got, reads = _run(hits, bodies, firsts={"msg/1-pxx-a": "2026-11-05T12:19:00+00:00"})
    assert got["posted"]["pxx"]["key"] == "msg/2-pxx-b"
    assert [(r["key"], r["version"]) for r in got["superseded"]] == [("msg/1-pxx-a", 2)]
    assert "msg/1-pxx-a@v1" in reads and got["order_unchecked"] == []
    # pxx c2f454: placed in its turn, A moves behind B, so B is read before A (MUTANT: no move after placing)
    assert reads == ["msg/1-pxx-a@v1", "msg/2-pxx-b", "msg/1-pxx-a"]


def test_a_seat_named_twice_counts_once_and_does_not_crash():
    """pxx e9adc1 P2-2 (codex-pxx-cli cf73fe): `--seats pxx,pxx` with one unparsed post walked the one queue twice and
    raised IndexError. The roster is deduplicated, order kept. MUTANT: drop the dedupe; IndexError."""
    got, _ = _run([_hit("msg/1-pxx-token", "pxx", "13:00:00")], {"msg/1-pxx-token": _msg("token 9f", "pxx")},
                  seats=("pxx", "pxx"))
    assert got["seats"] == 1 and got["missing"] == ["pxx"] and [r["key"] for r in got["unparsed"]] == ["msg/1-pxx-token"]


def test_a_label_two_seats_share_is_queued_for_both_and_read_once():
    """pxx e9adc1 P2-3 (codex-pxx-cli's cap-2 case): a label both seats' peers hold (a shared peer-<id8> alias) put
    txxxx's newer N7 only in pxx's queue, so with a cap of 2 txxxx settled on its older N6 and was not called
    unsettled. The post now waits in both queues, and its key is read once. MUTANT: queue it for the first seat
    only; txxxx's N6 wins."""
    hits = [_hit("msg/3-pxx-later", "pxx", "15:00:00"), _hit("msg/2-alias-n7", "peer-ab12", "14:30:00"),
            _hit("msg/1-txxxx-n6", "txxxx", "13:00:00")]
    bodies = {"msg/3-pxx-later": _msg("pxx to oxxxxxx: done", "pxx"),
              "msg/2-alias-n7": _msg("VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 7", "txxxx"),
              "msg/1-txxxx-n6": _msg("VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 6", "txxxx")}
    got, reads = _run(hits, bodies, seats=("pxx", "txxxx"), max_reads=2,
                      author_peers={"peer-ab12": {"p-pxx", "p-txxxx"}})
    assert got["posted"]["txxxx"]["key"] == "msg/2-alias-n7" and got["posted"]["txxxx"]["n"] == "7"
    assert reads.count("msg/2-alias-n7") == 1 and got["reads"] == 2
    assert got["not_read"] == ["msg/1-txxxx-n6"] and got["missing"] == ["pxx"]


def test_a_seat_is_not_settled_while_its_own_newer_post_is_unread():
    """pxx d6b601 (codex-pxx-cli 148a6c): txxxx's own N7 (15:00) and a shared-label N6 (14:00, signed by txxxx) and
    pxx's N1 (13:00). At cap 2 the shared N6, read through pxx's queue, counted txxxx as settled and N7 was never read.
    Now a seat counted that way stays open while its own newer post is unread (a), and when the cap still stops it,
    it is flagged and not settled (b). MUTANT: settle on any counted post; at cap 2 txxxx reads N6."""
    hits = [_hit("msg/3-txxxx-n7", "txxxx", "15:00:00"), _hit("msg/2-alias-n6", "peer-ab12", "14:00:00"),
            _hit("msg/1-pxx-n1", "pxx", "13:00:00")]
    bodies = {"msg/3-txxxx-n7": _msg("VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 7", "txxxx"),
              "msg/2-alias-n6": _msg("VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 6", "txxxx"),
              "msg/1-pxx-n1": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 1", "pxx")}
    alias = {"peer-ab12": {"p-pxx", "p-txxxx"}}
    got, _ = _run(hits, bodies, seats=("pxx", "txxxx"), max_reads=2, author_peers=alias)
    assert got["posted"]["txxxx"]["key"] == "msg/3-txxxx-n7", "(a): txxxx stays open until its own N7 is read"
    assert [r["key"] for r in got["superseded"]] == ["msg/2-alias-n6"] and got["not_settled"] == ["pxx"]
    got, _ = _run(hits, bodies, seats=("pxx", "txxxx"), max_reads=1, author_peers=alias)
    assert got["posted"]["txxxx"]["key"] == "msg/2-alias-n6" and got["posted"]["txxxx"]["newer_unread"] == 1
    assert sorted(got["not_settled"]) == ["pxx", "txxxx"], "(b): the cap ran out with txxxx's N7 unread"
    assert "(1 newer post(s) unread)" in rounds.render(got, "allhands-r1")


def test_one_seats_edited_posts_cannot_spend_the_cap_before_another_seats_reply_is_read():
    """GitHub Codex on e8f42783 (rounds.py:104) and pxx c2f454: every edited post's version 1 was read before the fair
    scheduler, out of the same cap, so 60 edited posts from txxxx spent the default 60 reads and pxx's reply was
    never read ("not settled"). Version 1 is now read in the seat's own turn, one read under the scheduler.
    MUTANT: place every edited post before phase 1; pxx's reply is not read. MUTANT: a free placement read; 120 reads
    are made under a cap of 60."""
    hits = [{**_hit(f"msg/{i:02d}-txxxx-edit", "txxxx", f"15:{i:02d}:00"), "version": "2"} for i in range(60)]
    hits.append(_hit("msg/99-pxx-reply", "pxx", "13:00:00"))
    bodies = {h["key"]: _msg("txxxx to oxxxxxx, on 397e4f: done", "txxxx", version=2) for h in hits[:60]}
    bodies["msg/99-pxx-reply"] = _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 6", "pxx")
    firsts = {h["key"]: f"2026-11-07T14:{i:02d}:00+00:00" for i, h in enumerate(hits[:60])}
    got, reads = _run(hits, bodies, seats=("txxxx", "pxx"), firsts=firsts)
    assert got["posted"]["pxx"]["key"] == "msg/99-pxx-reply" and got["posted"]["pxx"]["n"] == "6"
    assert reads[:2] == ["msg/59-txxxx-edit@v1", "msg/99-pxx-reply"], "one read for each seat, in turn"
    assert got["reads"] == len(reads) == 60 and got["not_settled"] == ["txxxx"] and got["missing"] == []


def test_an_edited_post_two_seats_share_is_placed_once_and_moves_in_both_queues():
    """The lazy placement and P2-3 together: an edited post under a label both seats' peers hold sits in both queues
    by its head time (15:00). pxx's turn places it at its version 1 (13:00), and it moves in txxxx's queue too, so at
    a cap of 2 txxxx counts its own 14:00 reply, never the older shared post. MUTANT: move it only in the queue of
    the seat whose turn placed it; txxxx counts the 13:00 post."""
    hits = [{**_hit("msg/3-alias-n5", "peer-ab12", "15:00:00"), "version": "2"},
            _hit("msg/2-pxx-n1", "pxx", "14:30:00"), _hit("msg/1-txxxx-n6", "txxxx", "14:00:00")]
    bodies = {"msg/3-alias-n5": _msg("VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 5", "txxxx", version=2),
              "msg/2-pxx-n1": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 1", "pxx"),
              "msg/1-txxxx-n6": _msg("VERDICT: INFO; ITEM: r1; SEAT: txxxx; N: 6", "txxxx")}
    alias, firsts = {"peer-ab12": {"p-pxx", "p-txxxx"}}, {"msg/3-alias-n5": "2026-11-05T12:19:00+00:00"}
    got, reads = _run(hits, bodies, seats=("pxx", "txxxx"), max_reads=2, author_peers=alias, firsts=firsts)
    assert reads == ["msg/3-alias-n5@v1", "msg/1-txxxx-n6"], reads
    assert got["posted"]["txxxx"]["key"] == "msg/1-txxxx-n6" and "newer_unread" not in got["posted"]["txxxx"]
    assert got["not_settled"] == ["pxx"] and got["not_read"] == ["msg/2-pxx-n1", "msg/3-alias-n5"]
    got, reads = _run(hits, bodies, seats=("pxx", "txxxx"), author_peers=alias, firsts=firsts)
    assert reads.count("msg/3-alias-n5@v1") == 1 and reads.count("msg/3-alias-n5") == 1
    assert [r["key"] for r in got["superseded"]] == ["msg/3-alias-n5"] and sorted(got["posted"]) == ["pxx", "txxxx"]


def test_an_edited_post_whose_first_version_cannot_be_read_keeps_the_edits_place_and_is_named():
    """pxx c2f454: keep order_unchecked for a version 1 that can't be read. That read still costs one read in the
    seat's turn; the post keeps its place by the edit's time, is read and judged, and is named.
    MUTANT: drop the name; order_unchecked is empty."""
    hits = [{**_hit("msg/1-pxx-a", "pxx", "15:00:00"), "version": "2"}, _hit("msg/2-pxx-b", "pxx", "14:00:00")]
    bodies = {"msg/1-pxx-a": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 5", "pxx", version=2),
              "msg/2-pxx-b": _msg("VERDICT: INFO; ITEM: r1; SEAT: pxx; N: 6", "pxx")}
    got, reads = _run(hits, bodies, firsts={})                 # the version-1 read of msg/1-pxx-a raises
    assert got["order_unchecked"] == ["msg/1-pxx-a"] and got["posted"]["pxx"]["key"] == "msg/1-pxx-a"
    assert reads == ["msg/1-pxx-a@v1", "msg/1-pxx-a", "msg/2-pxx-b"] and got["reads"] == 3
    assert "its first version could not be read): msg/1-pxx-a" in rounds.render(got, "allhands-r1")
