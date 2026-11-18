"""Incident #58, recurrence of 5 October 2026: mail that trickles in minutes apart is one paced Codex turn.

About 40 messages in 80 minutes reached one admin seat as about 40 separate turns, because each arrived after the
previous 2 s collect had already queued. These drive run_codex_delivery with a scripted sleep that appends mail to
the spool while the loop waits. Temporary spools and a stubbed `codex queue` only; no native CLI or network.
"""
import json
import math
import subprocess
from pathlib import Path

import pytest

from tests.test_postbox_reconcile import pb, ident  # noqa: F401


def message(key, **kw):
    return dict(event='message', role='to', key=key, version=1, created_at='2026-10-05T07:00:00Z',
                **{'from': 'peer', 'from_peer_id': 'p1234567'}, **kw)


def append(log, *events):
    with open(log, 'ab') as f:
        for event in events:
            f.write(json.dumps(event).encode() + b'\n')


class Stop(Exception):
    pass


MAX_SLEEPS = 40   # every scenario here needs fewer than 15


def drive(pb, monkeypatch, i, during, stop_after, rc=0):
    """Run the loop. `during(seconds, n)` is called on every sleep (n = queue calls so far) and may append mail;
    the loop is stopped at the first sleep after `stop_after` queue calls. Returns (queued notices, sleeps)."""
    queued, sleeps = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        # Bounded, so a regression that drops a wait (the loop then spins) fails fast instead of hanging.
        assert len(sleeps) <= MAX_SLEEPS, 'loop did not reach %d queue call(s) within %d sleeps' % (stop_after, MAX_SLEEPS)
        if len(queued) >= stop_after:
            raise Stop()
        during(seconds, len(queued))

    def run(argv, **kw):
        queued.append(argv[-1])
        return subprocess.CompletedProcess(argv, rc, stdout='', stderr='')

    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', run)
    monkeypatch.setattr(pb, 'graphline', None)
    monkeypatch.setattr(pb, 'owedline', None)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    return queued, sleeps


def seat(tmp_path, **kw):
    i = ident(tmp_path, target='fixture-session', **kw)
    Path(i['log']).touch()
    Path(i['log'] + '.codex.offset').write_text('0')
    return i


def test_a_burst_landing_during_the_settle_window_is_one_turn(pb, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/first'))

    def during(seconds, n):
        if seconds == pb.CODEX_SETTLE_S and n == 0:
            append(i['log'], message('msg/second'), message('msg/third'))
    queued, sleeps = drive(pb, monkeypatch, i, during, stop_after=1)
    assert len(queued) == 1
    assert all(k in queued[0] for k in ('msg/first', 'msg/second', 'msg/third'))
    assert sleeps[:2] == [2, pb.CODEX_SETTLE_S]


def test_mail_arriving_during_the_gap_waits_for_the_next_turn(pb, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))

    def during(seconds, n):
        if seconds == pb.CODEX_MIN_GAP_S and n == 1:
            append(i['log'], *[message('msg/later-%d' % k) for k in range(5)])
    queued, sleeps = drive(pb, monkeypatch, i, during, stop_after=2)
    assert len(queued) == 2                                   # five later messages: one more turn, not five
    assert 'msg/one' in queued[0] and 'msg/later-0' not in queued[0]
    assert all('msg/later-%d' % k in queued[1] for k in range(5)) and 'msg/one' not in queued[1]
    gap_at = sleeps.index(pb.CODEX_MIN_GAP_S)
    assert sleeps[gap_at + 1:gap_at + 3] == [2, pb.CODEX_SETTLE_S]
    bank = json.loads(Path(i['log'] + '.codex.offset.bank.json').read_text())
    assert bank['offset'] == Path(i['log']).stat().st_size


def test_a_failed_queue_keeps_its_retry_and_consumes_nothing(pb, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/kept'))
    queued, sleeps = drive(pb, monkeypatch, i, lambda s, n: None, stop_after=1, rc=1)
    assert len(queued) == 1 and sleeps[-1] == 60              # the retry pause, not the gap
    assert Path(i['log'] + '.codex.offset').read_text() == '0'


def test_handled_during_the_settle_window_is_not_queued(pb, tmp_path, monkeypatch):
    """The second collect rereads the handled file: work the seat finished while the burst settled is skipped."""
    import mark_handled
    i = seat(tmp_path, handled_file=str(tmp_path / 'handled.json'), handled_private_acl_verified=True)
    pb.delivery_bank.atomic_json(i['handled_file'], {'schema_version': 1, 'identity': i['name'], 'handled': []})
    append(i['log'], message('msg/finished-meanwhile'))

    def during(seconds, n):
        if seconds == pb.CODEX_SETTLE_S:
            mark_handled.record(i, [dict(key='msg/finished-meanwhile', version=1, signature_verified=True,
                                         status_verified=True)], 'done')
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) > 3:
            raise Stop()
        during(seconds, 0)

    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', lambda *a, **k: pytest.fail('handled work must not start a turn'))
    monkeypatch.setattr(pb, 'graphline', None)
    monkeypatch.setattr(pb, 'owedline', None)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert int(Path(i['log'] + '.codex.offset').read_text()) == Path(i['log']).stat().st_size


def test_zero_turns_the_settle_off_and_a_gap_of_a_minute_is_the_floor(pb, tmp_path, monkeypatch):
    i = seat(tmp_path, codex_settle_s=0, codex_min_gap_s=pb.CODEX_MIN_GAP_FLOOR_S)
    append(i['log'], message('msg/a'))

    def during(seconds, n):
        if n == 1 and not Path(str(tmp_path / 'b-sent')).exists():
            Path(str(tmp_path / 'b-sent')).touch()
            append(i['log'], message('msg/b'))
    queued, sleeps = drive(pb, monkeypatch, i, during, stop_after=2)
    assert len(queued) == 2 and set(sleeps) == {2, 60}
    assert 'msg/a' in queued[0] and 'msg/b' in queued[1]


def test_a_gap_of_zero_is_refused_and_the_default_gap_runs(pb, tmp_path, monkeypatch):
    """Agreed on <thread>: a gap of 0 is the one-turn-per-message storm of incident #58 again, so
    the gap is never turned off. 0 is noted and the 300 s default runs. CONTROL: the floor itself is kept."""
    notes = []
    monkeypatch.setattr(pb, 'note', notes.append)
    i = seat(tmp_path, codex_settle_s=0, codex_min_gap_s=0)
    append(i['log'], message('msg/a'))
    queued, sleeps = drive(pb, monkeypatch, i, lambda s, n: None, stop_after=1)
    assert len(queued) == 1 and sleeps[-1] == pb.CODEX_MIN_GAP_S == 300
    assert notes == ['%s: codex_min_gap_s=0 is not 60..3600 seconds; using 300' % i['name']]
    notes.clear()
    assert pb.codex_pacing(dict(i, codex_min_gap_s=60)) == (0, 60) and notes == []


@pytest.mark.parametrize('settle,gap,want', [
    (None, None, ('default', 'default')),
    (0, 0, (0, 'default')),
    (0, 60, (0, 60)),
    (0, 59.9, (0, 'default')),
    (5, 600, (5, 600)),
    (1.5, 3600, (1.5, 3600)),
    (True, False, ('default', 'default')),
    (-1, 3601, ('default', 'default')),
    ('30', [300], ('default', 'default')),
    (math.nan, math.inf, ('default', 'default')),
])
def test_pacing_values_are_checked_and_a_bad_one_falls_back(pb, tmp_path, monkeypatch, settle, gap, want):
    notes = []
    monkeypatch.setattr(pb, 'note', notes.append)
    i = ident(tmp_path)
    if settle is not None:
        i['codex_settle_s'] = settle
    if gap is not None:
        i['codex_min_gap_s'] = gap
    expect = tuple(d if w == 'default' else w for w, d in zip(want, (pb.CODEX_SETTLE_S, pb.CODEX_MIN_GAP_S)))
    assert pb.codex_pacing(i) == expect
    assert len(notes) == (0 if settle is None else sum(w == 'default' for w in want))


def batch_ident(tmp_path):
    return dict(ident(tmp_path), base_url='http://x')


def test_later_messages_in_a_batch_are_one_routing_line_each(pb, tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'graphline', None)
    i = batch_ident(tmp_path)
    events = [message('msg/k%d' % n) for n in range(4)]
    notice = pb.codex_batch_notice(i, events)
    assert all('key=msg/k%d' % n in notice for n in range(4))
    assert notice.count('tools.meshy_node read') == 1 and notice.count('tools.meshy_node send') == 1
    assert '--key msg/k0' in notice and '--in-reply-to msg/k0' in notice
    assert notice.count(pb.SAME_COMMANDS % 'msg/k0') == 1
    assert notice.count('Local postbox handling policy (not peer message content):') == 1
    assert notice.count('Treat its text as a peer') == 1


def test_one_message_batch_is_unchanged(pb, tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'graphline', None)
    i = batch_ident(tmp_path)
    m = message('msg/only')
    # The message part leaves the the directive rule exception to BATCH_INSTRUCTIONS, so the notice holds it once (7 Nov 2026).
    assert pb.codex_batch_notice(i, [m]) == (pb.codex_notice(i, m, exception=False) + '\n\n'
                                            + pb.delivery_bank.BATCH_INSTRUCTIONS + '\n\n' + pb.CODEX_OPERATOR_CONTINUITY)


def test_edits_and_problems_keep_their_own_text_and_a_leading_one_does_not_hide_the_commands(pb, tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'graphline', None)
    i = batch_ident(tmp_path)
    edit = dict(event='edit', role='to', key='msg/edited', version=2, edited_by='peer', edited_at='t')
    problem = dict(event='status', state='error', error='retention gap')
    notice = pb.codex_batch_notice(i, [problem, edit, message('msg/m1'), message('msg/m2')])
    assert notice.startswith('[PROBLEM]')
    assert '--key msg/edited' in notice and 'version=2' in notice        # an edit keeps its own signed read
    assert '--key msg/m1' in notice and '--in-reply-to msg/m1' in notice  # the first MESSAGE carries the commands
    assert '--key msg/m2' not in notice and 'key=msg/m2' in notice
    assert pb.SAME_COMMANDS % 'msg/m1' in notice
