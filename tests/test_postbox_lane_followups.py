"""pxx's three P3 follow-ups to the priority lane (#579, GO 64aee6, Tue 10 Nov 2026). Temporary spools and a stubbed
`codex queue` only, as in tests/test_postbox_priority_lane.py.

- ac7d06: a batch made only of lane-sent lines no longer sleeps the settle or resets the streak, so lane-sent rows
  ahead of ordinary mail don't push it past the gap-plus-streak bound.
- 9833f6: an empty collect() disarms the lane gap only when the bank is really drained.
- 73005e: status and the dispatch record count what a notice SHOWED; lane-sent lines are marked, not counted.
"""
import json
import re
from pathlib import Path

from tests.test_postbox_reconcile import pb, ident  # noqa: F401
from tests.test_postbox_codex_pacing import append
from tests.test_postbox_priority_lane import BOSS, THREAD, message, run_until, seat


def _keys(notice):
    return re.findall(r'key=(msg/[a-z0-9-]+)', notice)


def test_lane_sent_rows_ahead_of_ordinary_mail_add_no_settle(pb, tmp_path, monkeypatch):
    """ac7d06's reproduction: 24 priority lines, then one ordinary line. Before the fix each full batch of lane-sent
    rows cost one extra settle (about 30 s) on top of the gap, so ordinary waited 424 s against a 342 s bound."""
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_from=[BOSS])
    append(i['log'], *[message('msg/p-%02d' % n, from_peer_id=BOSS) for n in range(24)], message('msg/ordinary'))
    queued, sleeps = run_until(pb, monkeypatch, i, lambda *a: None,
                               done=lambda q: any('msg/ordinary' in n for n in q), max_sleeps=1000)
    sent = [k for n in queued for k in _keys(n)]
    assert sorted(set(sent)) == sorted(sent), 'a key went twice'
    assert 'msg/ordinary' in queued[-1] and not any(k.startswith('msg/p-') for k in _keys(queued[-1]))
    waited = sum(sleeps[:-1])
    bound = pb.CODEX_MIN_GAP_S + pb.CODEX_SETTLE_S + 2 * pb.CODEX_PRIORITY_STREAK_MAX + 2
    assert waited <= bound, (waited, bound)


def test_a_long_lane_sent_backlog_adds_no_2s_poll_per_batch(pb, tmp_path, monkeypatch):
    """#587 P3-2 (pxx e9843e): each batch made only of lane-sent lines used to cost one 2 s loop poll, so the wait
    grew by 2 s per batch of lane-sent rows ahead of ordinary mail. 120 priority lines then one ordinary line: the
    ordinary line waits no longer than the same bound as the 24-line case above, which a 2 s poll per lane-sent batch
    breaks once there are more than a few such batches."""
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_from=[BOSS])
    append(i['log'], *[message('msg/p-%03d' % n, from_peer_id=BOSS) for n in range(120)], message('msg/ordinary'))
    queued, sleeps = run_until(pb, monkeypatch, i, lambda *a: None,
                               done=lambda q: any('msg/ordinary' in n for n in q), max_sleeps=2000)
    sent = [k for n in queued for k in _keys(n)]
    assert sorted(set(sent)) == sorted(sent), 'a key went twice'
    assert len({k for k in sent if k.startswith('msg/p-')}) == 120          # control: every priority line went
    waited = sum(sleeps[:-1])
    bound = pb.CODEX_MIN_GAP_S + pb.CODEX_SETTLE_S + 2 * pb.CODEX_PRIORITY_STREAK_MAX + 2
    assert waited <= bound, (waited, bound)


def test_drained_needs_the_spool_end_and_nothing_held(pb, tmp_path):
    log = tmp_path / 'spool.log'
    log.write_bytes(b'x' * 100)

    class Bank:
        rows = []

        def held_after(self):
            return list(self.rows)
    bank = Bank()
    assert pb.codex_bank_drained(bank, 100, str(log)) is True
    assert pb.codex_bank_drained(bank, 99, str(log)) is False          # a bounded scan stopped short of the end
    bank.rows = [{'key': 'msg/x'}]
    assert pb.codex_bank_drained(bank, 100, str(log)) is False         # something is still held, or held this poll
    assert pb.codex_bank_drained(bank, 100, str(tmp_path / 'missing.log')) is False


def test_status_and_dispatch_count_what_was_shown(pb, tmp_path, monkeypatch):
    """73005e: lane on, spool [ordinary, directive]. The directive goes as [PRIORITY]; the ordinary batch then shows
    only the ordinary line. Its status counts 1 shown and 1 lane-sent, and the dispatch line marks the directive."""
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_lane=True)
    append(i['log'], message('msg/ordinary'), message('msg/urgent', directive=True))
    statuses = []
    monkeypatch.setattr(pb, 'set_status', lambda name, **kw: statuses.append(kw))
    queued, _ = run_until(pb, monkeypatch, i, lambda *a: None, done=lambda q: len(q) >= 2, max_sleeps=400)
    assert queued[0].startswith('[PRIORITY] ') and 'msg/ordinary' in queued[1]
    ok = [s for s in statuses if s.get('codex') == 'ok' and 'codex_batch_count' in s]
    assert ok and ok[-1]['codex_batch_count'] == 1 and ok[-1]['codex_batch_lane_sent'] == 1
    assert ok[-1]['codex_last'] == 'msg/ordinary'
    rows = [json.loads(x) for x in Path(i['log'] + '.codex.offset.dispatch.jsonl').read_text().splitlines()]
    marked = {(e.get('key'), e.get('lane_sent', False)) for r in rows for e in r['events'] if e.get('key')}
    assert ('msg/urgent', True) in marked and ('msg/ordinary', False) in marked, rows


def test_a_commit_without_shown_marks_nothing(pb, tmp_path):
    """The bank's default is unchanged: every caller that passes no `shown` gets the old dispatch lines."""
    import inspect
    from tools.postbox import delivery_bank
    assert inspect.signature(delivery_bank.Bank.commit).parameters['shown'].default is None


def test_a_line_held_by_this_very_collect_keeps_the_bank_undrained(pb, tmp_path):
    """pxx e9843e P3-1, with the real Bank: a collect that holds a line keeps it pending until commit, and the drained
    check must see it (bank.held, the committed list, is still empty then)."""
    from tools.postbox import delivery_bank
    i = seat(tmp_path, target=THREAD)
    bank = delivery_bank.Bank(i, i['log'] + '.codex.offset', 0, 'codex')
    bank._pending_held = ([{'key': 'msg/held', 'version': 1}], False)
    assert bank.held == [] and len(bank.held_after()) == 1
    import os as _os
    assert pb.codex_bank_drained(bank, _os.path.getsize(i['log']), i['log']) is False
