"""Issue #58 directions (a) and (b), kxxx's answers on the PR 1 plan, in the Codex delivery path.

(a) A notice the seat already READ (its own read ledger, the same key at the spooled version or later, whenever it was
read) wakes nobody, and each such skip is one line of <offset-file>.skips.jsonl. The 4 October HZ shape: three
notices spooled within a second of signing, all three read inside a four-hour turn, and each then started a turn of
its own when Codex's queue drained. The first version also required the read to come after the spool receipt; the
review of PR 1 (finding 1) showed that woke the seat for every edit, since production edit lines carry no rx_at.

(b) While the target session's turn runs (its rollout's last turn event a task_started), mail stays banked and goes as
one bounded notice when the turn ends. A missing, unreadable or ambiguous rollout falls back to the CODEX_MIN_GAP_S
pacing exactly as before.

Temporary spools, ledgers and rollouts, and a stubbed `codex queue`; no native CLI, network or real Codex sessions.
"""
import datetime
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_postbox_reconcile import pb, ident  # noqa: F401
from tests.test_postbox_delivery_bank import (bank, append, setup, wanted, copy_message, collect_held, keys,  # noqa: F401
                                              postbox_os)

PEER = 'p-this-seat'
T0 = datetime.datetime(2026, 10, 4, 14, 45, 0, tzinfo=datetime.timezone.utc)


def z(seconds):
    """The listener's spelling, '...Z' (rx_at)."""
    return (T0 + datetime.timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def plus(seconds):
    """The client's spelling, '+00:00' (the read ledger's `at`)."""
    return (T0 + datetime.timedelta(seconds=seconds)).isoformat()


def spooled(key, at, version=1, event='message', role='to', **kw):
    """A spool line as postbox.record_listener_line writes it: rx_at on a message line only, never on an edit."""
    receipt = {} if event == 'edit' else {'rx_at': z(at)}
    return dict(event=event, role=role, key=key, version=version, created_at=z(at - 1), **receipt,
                **{'from': 'peer', 'from_peer_id': 'p1234567'}, **kw)


def ledger(i, reads, peer=PEER, state_peer=PEER):
    """This identity's state file (its peer_id only) and its read ledger, as tools/meshy_node/node.py writes them."""
    Path(i['state']).write_text(json.dumps({'peer_id': state_peer}))
    Path(i['state'] + '.read.json').write_text(json.dumps(
        {'schema_version': 1, 'peer_id': peer,
         'reads': [{'key': k, 'version': v, 'at': plus(at)} for k, v, at in reads]}))


def fetched(i):
    return lambda: bank.read_ledger(i)


def skip_lines(i, b):
    path = Path(b.offset_file + bank.SKIPS_SUFFIX)
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def four_october(i):
    """Three notices, an unfetched request between them, and an edit to version 2 of one of the three."""
    append(i['log'], spooled('msg/one', 0), spooled('msg/two', 1), spooled('msg/request', 2),
           spooled('msg/three', 3), spooled('msg/two', 400, version=2, event='edit', edited_by='peer',
                                            edited_at=z(399)))
    # the seat read all three inside its long turn, after each was spooled; never the request, never version 2
    ledger(i, [('msg/one', 1, 53), ('msg/two', 1, 151), ('msg/three', 1, 220)])


# --- (a) in the bank ------------------------------------------------------------------------------------------------

def test_the_4_october_shape_wakes_only_for_the_request_and_the_edit(tmp_path):
    i, b = setup(tmp_path)
    four_october(i)
    end, events = b.collect(wanted, fetched=fetched(i))
    assert [(d['event'], d['key'], d['version']) for d in events] == [('message', 'msg/request', 1),
                                                                      ('edit', 'msg/two', 2)]
    assert end == Path(i['log']).stat().st_size
    assert skip_lines(i, b) == []                               # nothing is written before the commit
    b.commit(end, events)
    skips = skip_lines(i, b)
    assert [(s['key'], s['version'], s['reason']) for s in skips] == [
        ('msg/one', 1, 'already_read'), ('msg/two', 1, 'already_read'), ('msg/three', 1, 'already_read')]
    assert skips[0]['spooled_at'] == z(0) and skips[0]['fetched_at'] == plus(53)
    assert all(set(s) == {'key', 'version', 'reason', 'spooled_at', 'fetched_at', 'at'} for s in skips)
    assert all(isinstance(s['at'], str) and s['at'].endswith('Z') for s in skips)
    # a skip is a dropped WAKE, never handled work: no handled file is written, the dispatch log holds only the two
    assert 'handled_file' not in i and not list(tmp_path.glob('*handled*'))
    dispatched = json.loads(Path(b.offset_file + '.dispatch.jsonl').read_text())
    assert [e['key'] for e in dispatched['events']] == ['msg/request', 'msg/two']
    # and it is not delivered later either, nor counted twice
    end, events = b.collect(wanted, fetched=fetched(i))
    assert events == [] and end == b.offset
    assert len(skip_lines(i, b)) == 3


def test_a_ledger_written_for_another_peer_suppresses_nothing(tmp_path):
    i, b = setup(tmp_path)
    four_october(i)
    ledger(i, [('msg/one', 1, 53), ('msg/two', 1, 151), ('msg/three', 1, 220)], peer='p-another-seat')
    end, events = b.collect(wanted, fetched=fetched(i))
    assert keys(events) == ['msg/one', 'msg/two', 'msg/request', 'msg/three', 'msg/two']
    b.commit(end, events)
    assert skip_lines(i, b) == []


@pytest.mark.parametrize('reads,why', [
    ([('msg/one', 1, 53)], 'version 1 read, version 2 spooled'),
    ([('msg/other', 2, 53)], 'another key'),
])
def test_an_older_version_or_another_key_still_wakes(tmp_path, reads, why):
    i, b = setup(tmp_path)
    append(i['log'], spooled('msg/one', 0, version=2))
    ledger(i, reads)
    end, events = b.collect(wanted, fetched=fetched(i))
    assert keys(events) == ['msg/one'], why


@pytest.mark.parametrize('at,why', [(1, 'after the spool'), (0, 'at the spool time'), (-5, 'before the spool')])
def test_control_a_covering_read_suppresses_whenever_it_happened(tmp_path, at, why):
    """CONTROL for the cases above: the same line, read at version 3. kxxx's rule (a) is the key at a covering version
    read by this identity, with no time condition (finding 1): a read before the spool, a reconcile after a listener
    outage say, is still a read, and the client keeps the FIRST read time, so rereading would never have repaired it."""
    i, b = setup(tmp_path)
    append(i['log'], spooled('msg/one', 0, version=2))
    ledger(i, [('msg/one', 3, at)])
    end, events = b.collect(wanted, fetched=fetched(i))
    assert events == [], why
    b.commit(end, events)
    assert [(x['spooled_at'], x['fetched_at']) for x in skip_lines(i, b)] == [(z(0), plus(at))]


def test_a_read_before_reconcile_spooled_the_message_drops_its_wake(tmp_path):
    """The shape finding 1 named: the seat read a message while the listener was down, and reconcile spooled it later
    (postbox.reconcile writes rx_at at that moment and "recovered": true)."""
    i, b = setup(tmp_path)
    append(i['log'], dict(spooled('msg/missed', 600), recovered=True))
    ledger(i, [('msg/missed', 1, 30)])
    end, events = b.collect(wanted, fetched=fetched(i))
    assert events == []
    b.commit(end, events)
    assert [(x['key'], x['reason']) for x in skip_lines(i, b)] == [('msg/missed', 'already_read')]


def test_an_edit_the_seat_already_read_drops_its_wake(tmp_path):
    """Finding 1: record_listener_line stamps rx_at on message lines only, so with the old after-the-spool condition
    no real edit could ever be skipped. An edit read at its own version is skipped, with no receipt time to record;
    one read only at the older version still wakes."""
    i, b = setup(tmp_path)
    edit = spooled('msg/one', 400, version=2, event='edit', edited_by='peer', edited_at=z(399))
    assert 'rx_at' not in edit
    append(i['log'], edit)
    ledger(i, [('msg/one', 2, 420)])
    end, events = b.collect(wanted, fetched=fetched(i))
    assert events == []
    b.commit(end, events)
    assert [(x['key'], x['version'], x['spooled_at']) for x in skip_lines(i, b)] == [('msg/one', 2, None)]
    (tmp_path / 'older').mkdir()
    i2, b2 = setup(tmp_path / 'older')
    append(i2['log'], edit)
    ledger(i2, [('msg/one', 1, 420)])
    assert keys(b2.collect(wanted, fetched=fetched(i2))[1]) == ['msg/one']   # CONTROL: version 1 does not cover 2


@pytest.mark.parametrize('damage', ['no ledger', 'no state', 'state without peer', 'not json', 'schema 2'])
def test_a_ledger_that_cannot_be_read_suppresses_nothing(tmp_path, damage):
    i, b = setup(tmp_path)
    append(i['log'], spooled('msg/one', 0))
    ledger(i, [('msg/one', 1, 53)])
    if damage == 'no ledger':
        os.remove(i['state'] + '.read.json')
    elif damage == 'no state':
        os.remove(i['state'])
    elif damage == 'state without peer':
        Path(i['state']).write_text('{}')
    elif damage == 'not json':
        Path(i['state'] + '.read.json').write_text('{"schema_version": 1, "peer_id"')
    else:
        body = json.loads(Path(i['state'] + '.read.json').read_text())
        Path(i['state'] + '.read.json').write_text(json.dumps(dict(body, schema_version=2)))
    assert keys(b.collect(wanted, fetched=fetched(i))[1]) == ['msg/one']


def test_without_the_reader_the_channel_and_waiter_wake_as_before(tmp_path):
    """Only the Codex delivery passes `fetched`; every other consumer's collect is unchanged."""
    i, b = setup(tmp_path)
    four_october(i)
    assert keys(b.collect(wanted)[1]) == ['msg/one', 'msg/two', 'msg/request', 'msg/three', 'msg/two']


def test_a_repeated_collect_logs_each_skip_once(tmp_path):
    """The settle window collects twice before one commit; a skip is counted at the commit, once."""
    i, b = setup(tmp_path)
    four_october(i)
    b.collect(wanted, fetched=fetched(i))
    end, events = b.collect(wanted, fetched=fetched(i))
    b.commit(end, events)
    assert len(skip_lines(i, b)) == 3 and len(b.last_skipped) == 3


def test_a_pass_that_only_skips_commits_its_skips(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], spooled('msg/one', 0))
    ledger(i, [('msg/one', 1, 53)])
    end, events = b.collect(wanted, fetched=fetched(i))
    assert events == [] and end == Path(i['log']).stat().st_size and end != b.offset
    b.commit(end, events)
    assert [s['key'] for s in skip_lines(i, b)] == ['msg/one'] and b.offset == end


def test_a_held_copy_read_after_it_was_spooled_is_dropped_from_the_hold(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], dict(copy_message('msg/copy'), rx_at=z(0)))
    ledger(i, [])
    end, events = collect_held(b, fetched=fetched(i))
    assert events == []
    b.commit(end, events)
    assert [h['routing']['key'] for h in b.held] == ['msg/copy']
    ledger(i, [('msg/copy', 1, 30)])
    append(i['log'], spooled('msg/wake', 40))
    end, events = collect_held(b, fetched=fetched(i))
    assert keys(events) == ['msg/wake']                         # the read copy no longer rides along
    b.commit(end, events)
    assert b.held == [] and [s['key'] for s in skip_lines(i, b)] == ['msg/copy']


def test_a_held_copy_dropped_as_read_is_not_brought_back_by_a_restart(tmp_path):
    """The hold is part of the checkpoint (#199): once a read copy left it, a bank loaded from the same checkpoint
    holds nothing and logs no second skip."""
    i, b = setup(tmp_path)
    append(i['log'], dict(copy_message('msg/copy'), rx_at=z(0)))
    ledger(i, [])
    end, events = collect_held(b, fetched=fetched(i))
    b.commit(end, events)
    assert [h['routing']['key'] for h in b.held] == ['msg/copy']
    ledger(i, [('msg/copy', 1, 30)])
    end, events = collect_held(b, fetched=fetched(i))
    b.commit(end, events)
    assert b.held == [] and len(skip_lines(i, b)) == 1
    again = bank.Bank(i, b.offset_file, 0, b.consumer)
    assert again.held == []
    end, events = collect_held(again, fetched=fetched(i))
    assert events == [] and end == again.offset
    again.commit(end, events)
    assert len(skip_lines(i, b)) == 1


def test_the_ledger_reader_agrees_with_the_client(tmp_path):
    """read_ledger mirrors tools/meshy_node/node.py _read_ledger_records rather than importing it (the postbox and the
    client are separate installs); this holds the two to one answer on every record shape they judge."""
    node = pytest.importorskip('tools.meshy_node.node')
    i = ident(tmp_path)
    reads = [{'key': 'msg/ok', 'version': 2, 'at': plus(1)}, {'key': 'card/x', 'version': 1, 'at': plus(1)},
             {'key': 'msg/v0', 'version': 0, 'at': plus(1)}, {'key': 'msg/vb', 'version': True, 'at': plus(1)},
             {'key': 'msg/vs', 'version': '1', 'at': plus(1)}, {'key': 'msg/noat', 'version': 1},
             {'key': 7, 'version': 1, 'at': plus(1)}, 'not a record', {'key': 'msg/z', 'version': 1, 'at': z(2)}]
    for peer in (PEER, 'p-another-seat'):
        Path(i['state']).write_text(json.dumps({'peer_id': PEER}))
        Path(i['state'] + '.read.json').write_text(json.dumps({'schema_version': 1, 'peer_id': peer, 'reads': reads}))
        theirs = {(r['key'], r['version'], r['at']) for r in node.ledger_records(i['state'], {'peer_id': PEER})}
        ours = {(k, v, at) for k, rows in bank.read_ledger(i).items() for v, _, at in rows}
        assert ours == theirs
    assert theirs == set() and len(ours) == 0                       # the second pass, another peer, is empty
    Path(i['state'] + '.read.json').write_text(json.dumps({'schema_version': 1, 'peer_id': PEER, 'reads': reads}))
    assert {k for k in bank.read_ledger(i)} == {'msg/ok', 'msg/z'}  # CONTROL: the first pass was not empty


def test_the_ledger_reader_agrees_with_the_client_on_whole_files_and_repeats(tmp_path):
    """The same pin as above for what judges the whole file, and as a multiset: a repeated record counts as often in
    both (kxxx 264c2a asked for one fixture holding both readers to one answer)."""
    node = pytest.importorskip('tools.meshy_node.node')
    i = ident(tmp_path)
    Path(i['state']).write_text(json.dumps({'peer_id': PEER}))
    good = {'key': 'msg/ok', 'version': 2, 'at': plus(1)}
    extra = {'key': 'msg/x', 'version': 1, 'at': plus(2), 'thread': 't-1', 'kind': 'read'}   # older client's fields
    bodies = {
        'repeats and extra fields': json.dumps({'schema_version': 1, 'peer_id': PEER, 'reads': [good, good, extra]}),
        'schema 2': json.dumps({'schema_version': 2, 'peer_id': PEER, 'reads': [good]}),
        'schema as text': json.dumps({'schema_version': '1', 'peer_id': PEER, 'reads': [good]}),
        'no peer': json.dumps({'schema_version': 1, 'reads': [good]}),
        'reads not a list': json.dumps({'schema_version': 1, 'peer_id': PEER, 'reads': {'0': good}}),
        'not an object': json.dumps([good]),
        'damaged': '{"schema_version": 1, "peer_id": ',
        'missing': None,
    }
    seen = {}
    for name, text in bodies.items():
        ledger_path = Path(i['state'] + '.read.json')
        if text is None:
            ledger_path.unlink(missing_ok=True)
        else:
            ledger_path.write_text(text)
        theirs = sorted((r['key'], r['version'], r['at']) for r in node.ledger_records(i['state'], {'peer_id': PEER}))
        ours = sorted((k, v, at) for k, rows in bank.read_ledger(i).items() for v, _, at in rows)
        assert ours == theirs, name
        seen[name] = ours
    assert seen['repeats and extra fields'] == sorted([('msg/ok', 2, plus(1))] * 2 + [('msg/x', 1, plus(2))])  # CONTROL
    assert all(not rows for name, rows in seen.items() if name != 'repeats and extra fields')


# --- the Codex delivery loop: (a) end to end, and (b) ---------------------------------------------------------------

class Stop(Exception):
    pass


def message(key, **kw):
    return dict(event='message', role='to', key=key, version=1, created_at='2026-10-05T07:00:00Z',
                **{'from': 'peer', 'from_peer_id': 'p1234567'}, **kw)


def seat(tmp_path, **kw):
    i = ident(tmp_path, target='fixture-session', codex_sessions_dir=str(tmp_path / 'sessions'), **kw)
    Path(i['log']).touch()
    Path(i['log'] + '.codex.offset').write_text('0')
    return i


class Clock:
    """The loop's wall clock (pb._codex_clock), moved by every scripted sleep."""
    def __init__(self):
        self.t = 1791100000.0

    def __call__(self):
        return self.t

    def iso(self, ahead=0.0):
        return datetime.datetime.fromtimestamp(self.t + ahead, datetime.timezone.utc).isoformat()


def rollout(i, clock, *kinds, garbage=False):
    """Append turn events, timestamped by the clock, to the session's rollout (created on first use)."""
    day = Path(i['codex_sessions_dir']) / '2026' / '10' / '04'
    day.mkdir(parents=True, exist_ok=True)
    path = day / ('rollout-2026-10-04T14-00-00-%s.jsonl' % i['target'])
    with open(path, 'a', encoding='utf-8') as f:
        if garbage:
            f.write('{"timestamp": "%s", "type": "response_item", "payload": {"type": "message"}}\n' % clock.iso())
        for kind in kinds:
            f.write(json.dumps({'timestamp': clock.iso(), 'type': 'event_msg', 'payload': {'type': kind}}) + '\n')
    os.utime(path, (clock.t, clock.t))
    return path


def drive(pb, monkeypatch, i, during, max_sleeps=60, stop_after=None, rc=0):
    """Run the loop with scripted sleeps: `during(seconds, n_sleeps, queued)` may append mail or rollout events. Stops
    after `max_sleeps` sleeps, or at the first sleep after `stop_after` queue calls. Returns (queued, sleeps, clock)."""
    queued, sleeps, clock = [], [], Clock()
    drive.times = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.t += seconds
        if len(sleeps) >= max_sleeps or (stop_after is not None and len(queued) >= stop_after):
            raise Stop()
        during(seconds, len(sleeps), queued)

    def run(argv, **kw):
        queued.append(argv[-1])
        drive.times.append(clock.t)
        return subprocess.CompletedProcess(argv, rc, stdout='', stderr='')

    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', run)
    monkeypatch.setattr(pb, '_codex_clock', clock)
    monkeypatch.setattr(pb, 'graphline', None)
    monkeypatch.setattr(pb, 'owedline', None)
    drive.clock = clock
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    return queued, sleeps, clock


def status(pb):
    return json.load(open(pb.STATUS, encoding='utf-8'))['seat-prod']


def test_the_4_october_shape_in_the_codex_loop_hands_on_none_of_the_read_three(pb, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], spooled('msg/one', 0), spooled('msg/two', 1), spooled('msg/three', 3))
    ledger(i, [('msg/one', 1, 53), ('msg/two', 1, 151), ('msg/three', 1, 220)])
    queued, sleeps, _ = drive(pb, monkeypatch, i, lambda s, n, q: None, max_sleeps=10)
    assert queued == []
    skips = [json.loads(x) for x in Path(i['log'] + '.codex.offset.skips.jsonl').read_text().splitlines()]
    assert [(s['key'], s['reason']) for s in skips] == [(k, 'already_read')
                                                        for k in ('msg/one', 'msg/two', 'msg/three')]
    assert status(pb)['codex_wake_skips'] == 3 and status(pb)['last_wake_skip'] == 'msg/three'
    assert int(Path(i['log'] + '.codex.offset').read_text()) == Path(i['log']).stat().st_size
    # CONTROL: the same spool with an empty ledger is handed on
    (tmp_path / 'control').mkdir()
    i2 = seat(tmp_path / 'control')
    append(i2['log'], spooled('msg/one', 0), spooled('msg/two', 1), spooled('msg/three', 3))
    ledger(i2, [])
    queued, _, _ = drive(pb, monkeypatch, i2, lambda s, n, q: None, stop_after=1)
    assert len(queued) == 1 and all(k in queued[0] for k in ('msg/one', 'msg/two', 'msg/three'))


def test_six_notices_during_a_running_turn_are_one_hand_off_when_it_ends(pb, tmp_path, monkeypatch):
    i = seat(tmp_path)
    ledger(i, [])
    path = {}

    def during(seconds, n, queued):
        if n == 1:
            path['p'] = rollout(i, drive.clock, 'task_started')
        if 2 <= n <= 7:                                     # one notice per poll, minutes into the turn
            append(i['log'], message('msg/n%d' % n))
        if n == 30:
            rollout(i, drive.clock, 'task_complete')
    queued, sleeps, _ = drive(pb, monkeypatch, i, during, stop_after=1)
    assert len(queued) == 1
    assert all('msg/n%d' % n in queued[0] for n in range(2, 8))
    assert sleeps.index(pb.CODEX_SETTLE_S) > 29             # nothing was settled or queued while the turn ran
    assert pb.CODEX_MIN_GAP_S not in sleeps                 # the rollout paces it, not the gap
    st = status(pb)
    assert st['codex_hold'] is None and st['codex_released_reason'] == 'turn running'
    assert st['codex_rollout'] == str(path['p'])
    log = Path(pb.ERR).read_text()
    assert 'codex delivery holding (turn running)' in log and 'codex delivery released' in log


def test_after_a_hand_off_the_next_waits_for_codex_to_take_it_up_and_finish(pb, tmp_path, monkeypatch):
    """And never sooner than the gap: Codex took the first notice up and finished it 40 s later, and the second still
    waited out codex_min_gap_s from the first hand-off (finding 4: no turn per message on an idle seat)."""
    i = seat(tmp_path, codex_settle_s=0)
    ledger(i, [])
    marks = {}

    def during(seconds, n, queued):
        if n == 1:
            rollout(i, drive.clock, 'task_started', 'task_complete')     # an idle session
            append(i['log'], message('msg/first'))
        if queued and 'q' not in marks:
            marks['q'] = n
            append(i['log'], message('msg/second'))                      # arrives just after the hand-off
        if marks.get('q') and n == marks['q'] + 5:
            rollout(i, drive.clock, 'task_started')                      # Codex takes the first notice up
        if marks.get('q') and n == marks['q'] + 20:
            marks['before'] = len(queued)                                # still one, through the whole turn
            rollout(i, drive.clock, 'task_complete')
    queued, sleeps, _ = drive(pb, monkeypatch, i, during, max_sleeps=400, stop_after=2)
    assert len(queued) == 2 and 'msg/first' in queued[0] and 'msg/second' in queued[1]
    assert 'msg/second' not in queued[0]
    assert marks['before'] == 1                             # the second went only after that turn completed
    assert pb.CODEX_MIN_GAP_S <= drive.times[1] - drive.times[0] <= pb.CODEX_MIN_GAP_S + 4
    assert pb.CODEX_MIN_GAP_S not in sleeps                 # waited out by polls, not one long sleep
    st = status(pb)
    assert st['codex_released_reason'] == 'gap since the last hand-off'
    assert 'codex delivery holding (turn running)' in Path(pb.ERR).read_text()


def test_a_notice_codex_never_takes_up_is_followed_after_the_gap(pb, tmp_path, monkeypatch):
    """The session stays idle after the hand-off (no thread loaded, say): the gap is the bound, as before."""
    i = seat(tmp_path, codex_settle_s=0)
    ledger(i, [])

    def during(seconds, n, queued):
        if n == 1:
            rollout(i, drive.clock, 'task_started', 'task_complete')
            append(i['log'], message('msg/first'))
        if queued and n == 3:
            append(i['log'], message('msg/second'))
    queued, sleeps, clock = drive(pb, monkeypatch, i, during, max_sleeps=400, stop_after=2)
    assert len(queued) == 2 and 'msg/second' in queued[1]
    assert pb.CODEX_MIN_GAP_S <= drive.times[1] - drive.times[0] <= pb.CODEX_MIN_GAP_S + 4
    assert pb.CODEX_MIN_GAP_S not in sleeps                 # waited out by polls, not one long sleep
    assert status(pb)['codex_released_reason'] == 'previous notice not yet taken up'


@pytest.mark.parametrize('shape', ['no rollout', 'no turn event', 'stale running turn'])
def test_an_unknown_turn_falls_back_to_the_gap(pb, tmp_path, monkeypatch, shape):
    i = seat(tmp_path, codex_settle_s=0)
    ledger(i, [])

    def during(seconds, n, queued):
        if n == 1:
            if shape == 'no turn event':
                rollout(i, drive.clock, garbage=True)
            elif shape == 'stale running turn':
                p = rollout(i, drive.clock, 'task_started')
                old = drive.clock.t - pb.CODEX_TURN_STALE_S - 10
                os.utime(p, (old, old))
            append(i['log'], message('msg/a'))
        if n == 3:
            append(i['log'], message('msg/b'))
    queued, sleeps, _ = drive(pb, monkeypatch, i, during, stop_after=2)
    assert len(queued) == 2 and 'msg/a' in queued[0] and 'msg/b' in queued[1]
    assert sleeps[sleeps.index(pb.CODEX_MIN_GAP_S) - 1] == 2 and sleeps.count(pb.CODEX_MIN_GAP_S) >= 1


def test_control_a_fresh_running_turn_holds(pb, tmp_path, monkeypatch):
    """CONTROL for 'stale running turn': the same rollout, written just now, holds the mail."""
    i = seat(tmp_path, codex_settle_s=0)
    ledger(i, [])

    def during(seconds, n, queued):
        if n == 1:
            rollout(i, drive.clock, 'task_started')
            append(i['log'], message('msg/a'))
    queued, sleeps, _ = drive(pb, monkeypatch, i, during, max_sleeps=20)
    assert queued == [] and status(pb)['codex_hold'] == 'turn running'


def test_turn_aborted_ends_a_turn(pb, tmp_path):
    clock = Clock()
    i = seat(tmp_path)
    p = rollout(i, clock, 'task_started', 'turn_aborted')
    assert pb.codex_turn(str(p), clock=clock)[0] is False
    p = rollout(i, clock, 'task_started')
    assert pb.codex_turn(str(p), clock=clock)[0] is True


def test_a_turn_event_behind_more_than_8_mb_of_long_lines_is_found(pb, tmp_path, monkeypatch):
    """Review of PR 1, finding 2: the first reader stopped 8 MB back from the end and called the turn unknown, and the
    longest single turns measured in HQB's largest rollouts ran 10.5 and 9.1 MB. The rollout is now read forward in
    full, so a task_started behind 10 MB of tool output still holds the mail. Lines longer than a chunk included."""
    monkeypatch.setattr(pb, 'CODEX_ROLLOUT_CHUNK', 65536)
    clock = Clock()
    i = seat(tmp_path)
    p = rollout(i, clock, 'task_started')
    line = json.dumps({'timestamp': clock.iso(), 'type': 'response_item', 'payload': {'x': 'y' * 200000}}) + '\n'
    with open(p, 'a', encoding='utf-8') as f:
        f.write(line * 54)                                          # about 10.8 MB, each line about three chunks long
    assert os.path.getsize(p) > 10 * 1048576
    assert pb.codex_turn(str(p), clock=clock) == (True, pytest.approx(clock.t, abs=1))


def test_after_the_first_read_only_what_was_appended_is_read(pb, tmp_path):
    """The other half of finding 2: every 2 s poll of a held turn read up to 8 MB again. An update now reads only the
    bytes appended since the last one, and a line still being written is read whole once it is finished."""
    clock = Clock()
    i = seat(tmp_path)
    p = rollout(i, clock, 'task_started')
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'type': 'response_item', 'payload': {'x': 'y' * 500000}}) + '\n')
    t = pb.CodexTurn(str(p))
    assert t.update(clock=clock)[0] is True
    first = t.bytes_read
    assert first == os.path.getsize(p)
    assert t.update(clock=clock)[0] is True and t.bytes_read == first          # nothing appended, nothing read
    done = json.dumps({'timestamp': clock.iso(), 'type': 'event_msg', 'payload': {'type': 'task_complete'}})
    with open(p, 'a', encoding='utf-8') as f:
        f.write(done[:20])                                                      # the writer is mid-line
    assert t.update(clock=clock)[0] is True and t.bytes_read == first + 20
    with open(p, 'a', encoding='utf-8') as f:
        f.write(done[20:] + '\n')
    assert t.update(clock=clock)[0] is False
    assert t.bytes_read == os.path.getsize(p) + 20                              # the cut line, read once more whole


@pytest.mark.parametrize('change', ['truncated', 'replaced'])
def test_a_rollout_that_shrank_or_was_replaced_is_read_again_from_the_start(pb, tmp_path, change):
    clock = Clock()
    i = seat(tmp_path)
    p = rollout(i, clock, 'task_started')
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'type': 'response_item', 'payload': {'x': 'y' * 5000}}) + '\n')
    t = pb.CodexTurn(str(p))
    assert t.update(clock=clock)[0] is True
    event = lambda k: json.dumps({'timestamp': clock.iso(), 'type': 'event_msg', 'payload': {'type': k}})  # noqa: E731
    if change == 'truncated':
        p.write_text(event('task_complete') + '\n')
    else:
        fresh = p.with_name('fresh')
        fresh.write_text(event('task_started') + '\n' + event('task_complete') + '\n' + 'z' * 9000 + '\n')
        os.replace(fresh, p)                                        # larger than the old file: only its identity moved
    assert t.update(clock=clock)[0] is False


def test_two_rollouts_for_one_thread_are_unknown_and_paced_by_the_gap(pb, tmp_path, monkeypatch):
    """Finding 10: the day walk stopped at the first (newest) day holding a rollout. With two for the thread, the newer
    one's running turn held the mail; which of the two is live cannot be told, so the turn is now unknown and the gap
    paces delivery as before."""
    i = seat(tmp_path, codex_settle_s=0)
    ledger(i, [])
    other = Path(i['codex_sessions_dir']) / '2026' / '10' / '05'
    other.mkdir(parents=True)

    def during(seconds, n, queued):
        if n == 1:
            rollout(i, drive.clock)                                 # an older day's file, no turn event in it
            (other / ('rollout-2026-10-05T09-00-00-%s.jsonl' % i['target'])).write_text(json.dumps(
                {'timestamp': drive.clock.iso(), 'type': 'event_msg', 'payload': {'type': 'task_started'}}) + '\n')
            append(i['log'], message('msg/a'))
    queued, sleeps, _ = drive(pb, monkeypatch, i, during, stop_after=1)
    assert len(queued) == 1                                         # not held behind the newer file's task_started
    assert status(pb)['codex_rollouts_found'] == 2
    assert 'codex rollouts for thread' in Path(pb.ERR).read_text()
    assert len(pb.codex_rollouts(i)) == 2 and pb.codex_rollout(i) is None


def test_a_second_rollout_beside_the_cached_one_is_seen_within_the_resolve_interval(pb, tmp_path, monkeypatch):
    """Finding 10's other half: the path was kept until its file went away, so a second rollout appearing beside it
    was never seen. The thread is looked up again every CODEX_ROLLOUT_RESOLVE_S, and two rollouts make it unknown."""
    i = seat(tmp_path, codex_settle_s=0)
    ledger(i, [])
    marks = {}

    def during(seconds, n, queued):
        if n == 1:
            rollout(i, drive.clock, 'task_started')
            append(i['log'], message('msg/a'))
        if n == 3:
            day = Path(i['codex_sessions_dir']) / '2026' / '10' / '06'
            day.mkdir(parents=True)
            (day / ('rollout-2026-10-06T08-00-00-%s.jsonl' % i['target'])).write_text('')
            marks['t'] = drive.clock.t
    queued, sleeps, clock = drive(pb, monkeypatch, i, during, stop_after=1)
    assert len(queued) == 1 and 'msg/a' in queued[0]               # released once the second rollout was seen
    assert drive.times[0] - marks['t'] <= pb.CODEX_ROLLOUT_RESOLVE_S + 4
    assert status(pb)['codex_rollouts_found'] == 2


@pytest.mark.parametrize('kind', [['task_started'], {'type': 'task_started'}, 7, None])
def test_a_turn_event_whose_type_is_not_a_string_is_passed_over(pb, tmp_path, kind):
    """Review of PR 1 (2026-10-23): a list or dict payload type raised TypeError out of codex_turn and into guarded(),
    which restarted the loop into the same line. It is passed over, and the turn event before it decides."""
    clock = Clock()
    i = seat(tmp_path)
    p = rollout(i, clock, 'task_started')
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'timestamp': clock.iso(), 'type': 'event_msg', 'payload': {'type': kind}}) + '\n')
    assert pb.codex_turn(str(p), clock=clock) == (True, pytest.approx(clock.t, abs=1))


def test_an_idle_seat_that_clears_each_notice_quickly_still_gets_them_batched_by_the_gap(pb, tmp_path, monkeypatch):
    """Finding 4: with the first version, an idle Codex finishing each notice in seconds got one turn per message, 30 s
    apart here. The gap is a minimum between hand-offs again, so the four later messages go as one notice."""
    i = seat(tmp_path, codex_settle_s=0)
    ledger(i, [])
    taken = {}

    def during(seconds, n, queued):
        if n == 1:
            rollout(i, drive.clock, 'task_started', 'task_complete')
            append(i['log'], message('msg/m1'))
        if n in (15, 30, 45, 60):
            append(i['log'], message('msg/m%d' % (n // 15 + 1)))
        if len(queued) > len(taken):                                # Codex takes each notice up and finishes in 10 s
            taken[len(queued)] = n
            rollout(i, drive.clock, 'task_started')
        for q, at in taken.items():
            if n == at + 5:
                rollout(i, drive.clock, 'task_complete')
    queued, sleeps, _ = drive(pb, monkeypatch, i, during, max_sleeps=400, stop_after=2)
    assert len(queued) == 2 and 'msg/m1' in queued[0]
    assert all('msg/m%d' % k in queued[1] for k in (2, 3, 4, 5))
    assert drive.times[1] - drive.times[0] >= pb.CODEX_MIN_GAP_S


def test_a_skip_line_that_cannot_be_written_costs_the_commit_nothing(tmp_path):
    """Finding 9: skip lines are written after the checkpoint, best effort, like the dispatch line. One that cannot be
    written (here its path is a directory) neither raises nor holds the offset back, and the read line is not
    handed on later: what is lost is the report's count of it, never the skip. Accepted rather than moved into the
    checkpoint, since the line is a count for latency.py and the offset already carries the skip."""
    i, b = setup(tmp_path)
    append(i['log'], spooled('msg/one', 0))
    ledger(i, [('msg/one', 1, 53)])
    os.mkdir(b.offset_file + bank.SKIPS_SUFFIX)
    end, events = b.collect(wanted, fetched=fetched(i))
    b.commit(end, events)
    assert b.offset == end == Path(i['log']).stat().st_size
    assert [r['key'] for r in b.last_skipped] == ['msg/one']
    assert bank.Bank(i, b.offset_file, 0, b.consumer).collect(wanted, fetched=fetched(i))[1] == []
