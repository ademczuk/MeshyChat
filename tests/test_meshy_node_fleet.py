"""`meshy_node fleet` (tools/meshy_node/fleet.py): one table from GET /v1/mesh/recipients, from fixtures only."""
from __future__ import annotations

import pytest

from tools.meshy_node import fleet


def _peer(pid, state="active", beat=30, unread=0, oldest=None, yours=0, note=None):
    return {"peer_id": pid, "effective_state": state, "beat_age_secs": beat, "unread_count": unread,
            "unread_oldest_age_secs": oldest, "unanswered_from_you": yours, "note": note, "state_expires_at": None}


def _fn(table, calls=None, fail_on=None):
    def f(labels):
        if calls is not None:
            calls.append(list(labels))
        if fail_on and fail_on in labels:
            raise RuntimeError("HTTP 429 E_RATE_LIMITED")
        return {"as_of": "2026-11-02T09:47:12Z",
                "recipients": [{"label": lb, "peers": table.get(lb, [])} for lb in labels]}
    return f


def test_rows_render_state_beat_and_counts():
    rows, as_of = fleet.collect(["txxxx", "pxx"], _fn({"txxxx": [_peer("2d6d1628aaaa", beat=32, yours=1)],
                                                       "pxx": [_peer("717734fbbbbb", beat=120, unread=5, oldest=6 * 86400)]}))
    out = fleet.render(rows, as_of)
    assert "txxxx              2d6d1628 ?    ?          active     32s      0      -     1" in out
    assert "pxx                717734fb ?    ?          active      2m      5     6d     0" in out
    assert "as of 2026-11-02T09:47:12Z" in out
    assert "sockets not reported by this server (before #484)" in out


def test_the_measured_socket_is_its_own_column_and_count():
    """#484: `online` is measured (a websocket within 30 s). A stale `active` with no socket is not there; a seat with
    a socket and no presence row is there. Only real bools count; a server before #484 (no field) reads "?"."""
    rows, as_of = fleet.collect(["uxxx", "txxxx", "old"], _fn({
        "uxxx": [{**_peer("f123e60d", beat=75 * 86400), "online": False}],
        "txxxx": [{**_peer("2d6d1628", state="unknown", beat=None), "online": True}],
        "old": [{**_peer("11111111"), "online": "yes"}],  # not a bool: never trusted
    }))
    out = fleet.render(rows, as_of)
    assert "uxxx               f123e60d no   ?          active*    75d" in out
    assert "txxxx              2d6d1628 yes  ?          unknown      -" in out
    assert "old                11111111 ?    ?          active" in out
    assert "1 of 2 with a live socket" in out


def test_a_state_older_than_the_hook_ttl_is_starred_never_live():
    """Production 2 Nov: uxxx read `active` 75 days after its last beat (a beat published without a ttl)."""
    rows, as_of = fleet.collect(["uxxx", "txxxx"], _fn({"uxxx": [_peer("f123e60d", beat=75 * 86400)],
                                                        "txxxx": [_peer("2d6d1628", beat=32)]}))
    uxxx = next(r for r in rows if r["seat"] == "uxxx")
    txxxx = next(r for r in rows if r["seat"] == "txxxx")
    assert uxxx["stale"] is True and txxxx["stale"] is False
    out = fleet.render(rows, as_of)
    assert "uxxx               f123e60d ?    ?          active*    75d" in out
    assert "1 stale (*: published without a ttl" in out
    # CONTROL: an `unknown` state is never starred (it already says nothing), and a missing beat age is stale
    rows, _ = fleet.collect(["a", "b"], _fn({"a": [_peer("1", state="unknown", beat=None)],
                                             "b": [_peer("2", beat=None)]}))
    assert [r["stale"] for r in rows] == [False, True]


def test_a_state_with_an_unexpired_ttl_is_never_starred():
    """Codex on #483: a beat published with ttl_secs up to 86400 has state_expires_at, and the server already
    expires it; 7 h since that beat is inside the publisher's own bound, so it is not stale."""
    rows, _ = fleet.collect(["ttl", "nottl"], _fn({
        "ttl": [{**_peer("1", beat=7 * 3600), "state_expires_at": "2026-11-03T05:00:00Z"}],
        "nottl": [_peer("2", beat=7 * 3600)]}))
    assert [r["stale"] for r in rows] == [False, True]
    assert [r["ttl"] for r in rows] == [True, False]            # the dispatcher's plan reads this (board_dispatch)
    fresh, _ = fleet.collect(["nottl"], _fn({"nottl": [{**_peer("2", beat=30), "state_expires_at": ""}]}))
    assert fresh[0]["stale"] is False and fresh[0]["ttl"] is False     # a fresh beat with no TTL: live, but no TTL


def test_a_failed_chunk_or_a_missing_label_is_said_never_zero():
    seats = [f"s{i}" for i in range(12)]
    calls = []
    rows, _ = fleet.collect(seats, _fn({}, calls=calls, fail_on="s11"))
    assert [len(c) for c in calls] == [5, 5, 2]  # asked fleet.CHUNK (5) at a time, under RECIPIENTS_MAX
    by = {r["seat"]: r for r in rows}
    assert by["s0"]["status"] == "no peer answers this label"
    assert by["s11"]["status"].startswith("unavailable (RuntimeError: HTTP 429 E_RATE_LIMITED")
    assert "unread" not in by["s11"]
    out = fleet.render(rows, [])
    assert "s10                unavailable (RuntimeError" in out
    assert "2 without data" in out

    def short(labels):  # the server's answer omits a label it was asked for
        return {"recipients": [{"label": "x", "peers": [_peer("p")]}]}
    rows, _ = fleet.collect(["x", "y"], short)
    assert rows[-1] == {"seat": "y", "status": "unavailable (the server's answer did not name this label)"}


def test_a_peer_note_is_quoted_cleaned_and_capped():
    note = 'ok\x1b[31m red‮ evil "quoted" ' + "x" * 80
    rows, _ = fleet.collect(["s"], _fn({"s": [_peer("p", note=note)]}))
    shown = rows[0]["note"]
    assert shown.startswith('"ok [31m red evil \'quoted\'') and shown.endswith('..."')
    assert "\x1b" not in shown and "‮" not in shown and len(shown) <= fleet.NOTE_MAX + 2


def test_one_label_with_two_peers_gives_two_rows():
    rows, _ = fleet.collect(["kxxx"], _fn({"kxxx": [_peer("aaaa1111"), _peer("bbbb2222", state="idle")]}))
    assert [(r["peer"], r["state"]) for r in rows] == [("aaaa1111", "active"), ("bbbb2222", "idle")]


@pytest.mark.parametrize("raw, ok", [("a,b,a", ["a", "b"]), ("txxxx , uxxx", ["txxxx", "uxxx"])])
def test_seats_parse(raw, ok):
    assert fleet.parse_seats(raw) == ok


@pytest.mark.parametrize("raw", ["", " , ", "a,../b", "x" * 70])
def test_bad_seats_are_refused(raw):
    with pytest.raises(ValueError):
        fleet.parse_seats(raw)


def test_the_cli_route_is_read_only_and_wired(monkeypatch, capsys, tmp_path):
    from tools.meshy_node import cli, node
    assert not cli._mutating_command(cli.build_parser().parse_args(
        ["fleet", "--base-url", "http://x", "--state-file", str(tmp_path / "s.json"), "--seats", "a"]))
    asked = []
    monkeypatch.setattr(node, "recipients", lambda path, state, labels: asked.append(labels) or {
        "as_of": "t", "recipients": [{"label": "a", "peers": [_peer("abcdef12")]}]})
    monkeypatch.setattr(node, "load_state", lambda path, base_url, max_bytes=None: {"peer_id": "p", "base_url": base_url})
    rc = cli.main(["fleet", "--base-url", "http://x", "--state-file", str(tmp_path / "s.json"), "--seats", "a"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "a                  abcdef12 ?    ?          active" in out and asked == [["a"]]


def test_measured_activity_is_its_own_column_and_count():
    """f38d28 item 5: a seat that SENT within 30 min reads active with the age of that send, whatever it published;
    a published active with a live ttl reads "yes beat"; a server before the field reads "?" and is never counted."""
    rows, as_of = fleet.collect(["memory-bxxxxx", "txxxx", "idle", "old"], _fn({
        "memory-bxxxxx": [{**_peer("22222222", state="unknown", beat=None), "online": False,
                           "active": True, "active_sent_at": "2026-11-02T09:43:12Z"}],
        "txxxx": [{**_peer("2d6d1628", beat=60), "online": True, "active": True, "active_sent_at": None,
                   "state_expires_at": "2026-11-02T10:47:12Z"}],
        "idle": [{**_peer("44444444", state="idle"), "online": True, "active": False, "active_sent_at": None}],
        "old": [{**_peer("11111111"), "active": "yes"}],          # not a bool: never trusted
    }))
    out = fleet.render(rows, as_of)
    assert "memory-bxxxxx      22222222 no   yes 4m     unknown" in out      # DOWN by socket, active by its send
    assert "txxxx              2d6d1628 yes  yes beat   active" in out
    assert "idle               44444444 yes  no         idle" in out
    assert "old                11111111 ?    ?          active" in out
    assert "2 of 3 active; " in out
