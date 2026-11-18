"""Priority lane for Codex seats (oxxxxxx f38d28, Tue 10 Nov 2026, item 1).

A directive reached the HZ builder's Codex queue behind three ordinary notices and had not started after 7 minutes.
The postbox now holds while Codex's own queue still has items for the thread (read-only count), and a priority
message (role "to", from a peer in "priority_from" or carrying the server's `directive` flag) goes past every hold
and the settle window, first in its notice. Temporary spools, a temporary SQLite file and a stubbed `codex queue`
only; no native CLI or network.
"""
import json
import os
import types
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.test_postbox_reconcile import pb, ident  # noqa: F401
from tests.test_postbox_codex_pacing import Stop, append


def message(key, **kw):
    d = dict(event='message', role='to', key=key, version=1, created_at='2026-11-10T06:00:00Z',
             **{'from': 'peer', 'from_peer_id': 'p1234567'})
    d.update(kw)
    return d


THREAD = '0f0f0f0f-0000-4000-8000-000000000001'   # no real session: hermetic
BOSS = '610170d5-09fb-5385-9ea4-fc3df696b431'


def seat(tmp_path, **kw):
    """A Codex identity whose target is a session id, as on every live seat (the shared helper's is a name)."""
    kw.setdefault('target', THREAD)
    kw.setdefault('codex_sessions_dir', str(tmp_path / 'sessions'))   # never this box's real rollouts
    i = ident(tmp_path, **kw)
    Path(i['log']).touch()
    Path(i['log'] + '.codex.offset').write_text('0')
    return i


def queue_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute('CREATE TABLE queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, '
                'payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, '
                'updated_at_ms INTEGER NOT NULL)')
    for n, thread in enumerate(rows):
        con.execute('INSERT INTO queued_items VALUES (?, ?, ?, ?, 0, 0)', ('i%d' % n, thread, '{}', n))
    con.commit()
    con.close()
    return str(path)


def run_loop(pb, monkeypatch, i, during, stop_after, max_sleeps=60):
    queued, sleeps = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) <= max_sleeps, 'no queue call within %d sleeps; sleeps=%r' % (max_sleeps, sleeps)
        if len(queued) >= stop_after:
            raise Stop()
        during(seconds, len(queued), len(sleeps))

    def run(argv, **kw):
        queued.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    _fake_clock_unless_set(pb, monkeypatch, sleeps)
    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', run)
    monkeypatch.setattr(pb, 'graphline', None)
    monkeypatch.setattr(pb, 'owedline', None)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    return queued, sleeps


def _fake_clock_unless_set(pb, monkeypatch, sleeps):
    """The pacing clock advances with the scripted sleeps, so a gap can elapse inside a test. A test that installs its own
    clock (any _codex_clock not defined in the module under test) keeps it."""
    if pb._codex_clock is not pb.time.time:             # the module's own clock is time.time itself
        return
    base = [1000.0]
    monkeypatch.setattr(pb, '_codex_clock', lambda: base[0] + sum(sleeps))


# --- which lines are priority -------------------------------------------------------------------------------------

@pytest.mark.parametrize('event, want', [
    (dict(event='message', role='to', from_peer_id=BOSS), True),
    (dict(event='message', role='cc', from_peer_id=BOSS), False),            # a copy never jumps the queue
    (dict(event='message', role='broadcast', from_peer_id=BOSS), False),
    (dict(event='message', role='to', from_peer_id='someone-else'), False),
    (dict(event='message', role='to', from_peer_id='someone-else', directive=True), True),
    (dict(event='message', role='cc', from_peer_id='someone-else', directive=True), False),
    (dict(event='message', role='to', from_peer_id='someone-else', directive='true'), False),   # only a real true
    (dict(event='edit', role='to', from_peer_id=BOSS), False),
    (dict(event='status', state='auth_refreshed'), False),
])
def test_priority_is_role_to_from_a_listed_peer_or_a_server_directive(pb, event, want):
    assert pb.codex_priority({'priority_from': [BOSS]}, event) is want


def test_priority_from_that_is_not_a_list_grants_nothing(pb):
    d = dict(event='message', role='to', from_peer_id=BOSS)
    assert pb.codex_priority({'priority_from': BOSS}, d) is False            # a string is not a list of peers
    assert pb.codex_priority({}, d) is False


# --- Codex's own queue depth --------------------------------------------------------------------------------------

def test_queue_depth_counts_only_this_thread(pb, tmp_path):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD, THREAD, 'other-thread'])
    assert pb.codex_queue_depth({'target': THREAD, 'codex_queue_db': db}) == 2
    assert pb.codex_queue_depth({'target': '0f0f0f0f-0000-4000-8000-000000000002', 'codex_queue_db': db}) == 0


@pytest.mark.parametrize('case', ['missing', 'name-target', 'wrong-schema', 'not-sqlite'])
def test_queue_depth_unknown_is_none_so_pacing_stays_as_before(pb, tmp_path, case):
    db = tmp_path / 'q.sqlite'
    target = THREAD
    if case == 'name-target':
        queue_db(db, [THREAD])
        target = 'fixture-session'
    elif case == 'wrong-schema':
        con = sqlite3.connect(str(db))
        con.execute('CREATE TABLE something_else (x)')
        con.commit()
        con.close()
    elif case == 'not-sqlite':
        db.write_bytes(b'this is not a database file at all, just text' * 20)
    assert pb.codex_queue_depth({'target': target, 'codex_queue_db': str(db)}) is None


def test_queue_depth_never_writes_the_store(pb, tmp_path):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    before = Path(db).read_bytes()
    assert pb.codex_queue_depth({'target': THREAD, 'codex_queue_db': db}) == 1     # it really read the store
    assert Path(db).read_bytes() == before


# --- the loop -----------------------------------------------------------------------------------------------------

def test_ordinary_mail_holds_while_codex_still_has_a_queued_item(pb, tmp_path, monkeypatch):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db)
    append(i['log'], message('msg/ordinary'))

    def during(seconds, n, k):
        if k == 10:                                       # Codex took its item: the store drains
            con = sqlite3.connect(db)
            con.execute('DELETE FROM queued_items')
            con.commit()
            con.close()
    queued, sleeps = run_loop(pb, monkeypatch, i, during, stop_after=1)
    assert len(queued) == 1 and 'msg/ordinary' in queued[0]
    assert sleeps.index(pb.CODEX_SETTLE_S) >= 10          # nothing was queued while the store held an item


def test_a_priority_message_goes_past_the_queue_hold_in_a_notice_of_its_own(pb, tmp_path, monkeypatch):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD, THREAD, THREAD])   # three ordinary notices ahead, as on 9 Nov
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    append(i['log'], message('msg/ordinary-1'), message('msg/ordinary-2'))

    def during(seconds, n, k):
        if k == 5:
            append(i['log'], message('msg/directive', from_peer_id=BOSS))
    queued, sleeps = run_loop(pb, monkeypatch, i, during, stop_after=1)
    assert len(queued) == 1
    notice = queued[0]
    assert notice.startswith('[PRIORITY] ') and 'msg/directive' in notice
    assert 'msg/ordinary-1' not in notice and 'msg/ordinary-2' not in notice     # the bank's batch is untouched
    assert pb.CODEX_SETTLE_S not in sleeps                # it did not wait out the settle window
    assert len(sleeps) == 6                               # queued on the first poll after it landed
    bank_file = Path(i['log'] + '.codex.offset.bank.json')
    assert not bank_file.exists() or json.loads(bank_file.read_text())['offset'] == 0   # nothing committed


def test_the_server_directive_flag_is_priority_without_any_config(pb, tmp_path, monkeypatch):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db)
    append(i['log'], message('msg/flagged', directive=True))
    queued, sleeps = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1)
    assert 'msg/flagged' in queued[0] and sleeps == [2, 2]       # first poll, past the queue hold, no settle


def test_a_priority_message_behind_a_full_ordinary_batch_is_still_found(pb, tmp_path, monkeypatch):
    # 1fa038 P2-1: the bank collects at most MAX_EVENTS; a priority line at position MAX_EVENTS + 1 must not wait
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    n = pb.delivery_bank.MAX_EVENTS
    append(i['log'], *[message('msg/ordinary-%02d' % k) for k in range(n)])
    append(i['log'], message('msg/directive-late', from_peer_id=BOSS))
    queued, sleeps = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=5)
    assert 'msg/directive-late' in queued[0] and 'msg/ordinary-00' not in queued[0]


def test_a_priority_message_landing_during_the_settle_is_first_in_that_batch(pb, tmp_path, monkeypatch):
    # 1fa038 P2-2: urgency is re-read after the settle; the batch that goes at once lists the priority line first
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_from=[BOSS])
    append(i['log'], message('msg/ordinary'))

    def during(seconds, n, k):
        if seconds == pb.CODEX_SETTLE_S and n == 0:
            append(i['log'], message('msg/directive', from_peer_id=BOSS))
    queued, sleeps = run_loop(pb, monkeypatch, i, during, stop_after=1)
    assert len(queued) == 1
    assert queued[0].index('msg/directive') < queued[0].index('msg/ordinary')


def test_a_priority_message_landing_during_the_settle_while_a_hold_appears_goes_next_poll(pb, tmp_path, monkeypatch):
    db = str(tmp_path / 'q.sqlite')
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    append(i['log'], message('msg/ordinary'))

    def during(seconds, n, k):
        if seconds == pb.CODEX_SETTLE_S and n == 0:
            queue_db(db, [THREAD])                         # Codex's queue fills during the settle: a hold
            append(i['log'], message('msg/directive', from_peer_id=BOSS))
    queued, sleeps = run_loop(pb, monkeypatch, i, during, stop_after=1, max_sleeps=6)
    assert queued[0].startswith('[PRIORITY] ') and 'msg/directive' in queued[0] and 'msg/ordinary' not in queued[0]


def test_a_priority_message_is_handed_on_once(pb, tmp_path, monkeypatch):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])                    # the bank stays held throughout
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    append(i['log'], message('msg/directive', from_peer_id=BOSS))
    with pytest.raises(AssertionError, match='no queue call within'):
        run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=2, max_sleeps=20)
    sent = json.loads(Path(i['log'] + '.codex.priority.json').read_text())['sent']
    assert sent == [['msg/directive', 1, 0]]                            # with the line's byte offset (d03888)


def test_a_priority_message_the_seat_already_read_wakes_nobody(pb, tmp_path, monkeypatch):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    append(i['log'], message('msg/directive', from_peer_id=BOSS))
    monkeypatch.setattr(pb.delivery_bank, 'read_ledger',
                        lambda *a, **k: {'msg/directive': [(1, 0.0, '2026-11-10T06:00:00Z')]})
    with pytest.raises(AssertionError, match='no queue call within'):
        run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=10)


def test_a_queue_that_never_drains_stops_holding_after_the_stale_bound(pb, tmp_path, monkeypatch):
    # 1fa038 P2-3: an orphaned native item must not bank ordinary mail forever
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db)
    append(i['log'], message('msg/ordinary'))
    clock = [1000.0]
    monkeypatch.setattr(pb, '_codex_clock', lambda: clock[0])
    statuses = []
    monkeypatch.setattr(pb, 'set_status', lambda name, **kw: statuses.append(kw))

    def during(seconds, n, k):
        clock[0] += 60 if seconds == 2 else seconds         # each poll stands for a minute
    queued, sleeps = run_loop(pb, monkeypatch, i, during, stop_after=1, max_sleeps=40)
    assert len(queued) == 1 and 'msg/ordinary' in queued[0]
    polls_before = sleeps.index(pb.CODEX_SETTLE_S)
    assert pb.CODEX_QUEUE_STALE_S // 60 <= polls_before <= pb.CODEX_QUEUE_STALE_S // 60 + 2
    assert any(s.get('codex_queue_stale') == 1 for s in statuses)


def test_a_queue_that_makes_progress_keeps_holding(pb, tmp_path, monkeypatch):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD, THREAD])
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE queued_thread_revisions (revision INTEGER PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE)')
    con.commit()
    con.close()
    i = seat(tmp_path, target=THREAD, codex_queue_db=db)
    append(i['log'], message('msg/ordinary'))
    clock = [1000.0]
    monkeypatch.setattr(pb, '_codex_clock', lambda: clock[0])

    def during(seconds, n, k):
        clock[0] += 60 if seconds == 2 else seconds
        if k % 8 == 0:                                       # Codex adds and takes items: the revision moves
            c = sqlite3.connect(db)
            c.execute("INSERT OR REPLACE INTO queued_thread_revisions (revision, thread_id) VALUES (?, ?)", (k, THREAD))
            c.commit()
            c.close()
    with pytest.raises(AssertionError, match='no queue call within'):
        run_loop(pb, monkeypatch, i, during, stop_after=1, max_sleeps=30)


def test_a_cc_copy_from_the_coordinator_still_waits(pb, tmp_path, monkeypatch):
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    append(i['log'], message('msg/copy', from_peer_id=BOSS, role='cc'))
    with pytest.raises(AssertionError, match='no queue call'):
        run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=30)


def test_with_priority_from_the_unknown_turn_gap_is_polled_and_a_priority_line_ends_it(pb, tmp_path, monkeypatch):
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_from=[BOSS])
    append(i['log'], message('msg/first'))
    clock = [1000.0]
    monkeypatch.setattr(pb, '_codex_clock', lambda: clock[0])

    def during(seconds, n, k):
        clock[0] += seconds
        if n == 1 and seconds == 2 and clock[0] < 1000 + pb.CODEX_SETTLE_S + 60:
            append(i['log'], message('msg/urgent', from_peer_id=BOSS))
    queued, sleeps = run_loop(pb, monkeypatch, i, during, stop_after=2)
    assert len(queued) == 2 and 'msg/urgent' in queued[1]
    assert pb.CODEX_MIN_GAP_S not in sleeps               # the 5-minute blind sleep is gone for this seat


def test_with_the_lane_on_a_directive_flag_line_ends_the_unknown_turn_gap(pb, tmp_path, monkeypatch):
    # bce2c7 P2-2: "priority_lane": true (no priority_from) polls the gap, so the server's flag alone goes next
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_lane=True)
    append(i['log'], message('msg/first'))
    clock = [1000.0]
    monkeypatch.setattr(pb, '_codex_clock', lambda: clock[0])
    landed = []

    def during(seconds, n, k):
        clock[0] += seconds
        if n == 1 and not landed:
            landed.append(clock[0])
            append(i['log'], message('msg/flagged', directive=True))
    queued, sleeps = run_loop(pb, monkeypatch, i, during, stop_after=2)
    assert queued[1].startswith('[PRIORITY] ') and 'msg/flagged' in queued[1]
    assert pb.CODEX_MIN_GAP_S not in sleeps and sleeps.count(2) <= 6     # not held for the 300 s gap


def run_until(pb, monkeypatch, i, during, done, max_sleeps=400):
    """Like run_loop, but stops when done(queued) is true."""
    queued, sleeps = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) <= max_sleeps, 'not done within %d sleeps; %d queued' % (max_sleeps, len(queued))
        if done(queued):
            raise Stop()
        during(seconds, len(queued), len(sleeps))

    def run(argv, **kw):
        queued.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    _fake_clock_unless_set(pb, monkeypatch, sleeps)
    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', run)
    monkeypatch.setattr(pb, 'graphline', None)
    monkeypatch.setattr(pb, 'owedline', None)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    return queued, sleeps


def test_a_long_priority_burst_is_handed_on_once_each_and_never_starves_the_bank(pb, tmp_path, monkeypatch):
    # bce2c7 P2-1: 257 unread priority lines once made an endless resubmission cycle past a 256-pair ledger
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_from=[BOSS])
    append(i['log'], message('msg/ordinary'))
    append(i['log'], *[message('msg/p-%03d' % k, from_peer_id=BOSS) for k in range(257)])
    import re as _re

    def all_out(q):
        return len(set(k for n in q for k in _re.findall(r'key=(msg/p-\d{3})', n))) == 257 and any(
            'msg/ordinary' in n for n in q)
    queued, _ = run_until(pb, monkeypatch, i, lambda *a: None, done=all_out, max_sleeps=2000)
    keys = [k for n in queued for k in _re.findall(r'key=(msg/p-\d{3})', n)]
    assert len(keys) == 257 and len(set(keys)) == 257                   # every one once, lane or batch, none twice
    assert any('msg/ordinary' in n for n in queued)                     # the bank was not starved (220cb6 P3)


def test_a_priority_line_past_one_window_is_found_by_the_continuation_window(pb, tmp_path, monkeypatch):
    # d03888 P2-2: past the head window, a continuation window walks the region, so the line is reached while held
    monkeypatch.setattr(pb, 'CODEX_PRIORITY_SCAN_BYTES', 2000)
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])                       # the bank stays held
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    append(i['log'], *[message('msg/ordinary-%03d' % k) for k in range(120)])     # about 13 windows
    append(i['log'], message('msg/directive-deep', from_peer_id=BOSS))
    assert Path(i['log']).stat().st_size > 5 * 2000                     # several windows past the head
    queued, sleeps = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=30)
    assert queued[0].startswith('[PRIORITY] ') and 'msg/directive-deep' in queued[0]
    assert len(sleeps) <= 16                                             # within (region / window) polls


def test_an_old_ledger_entry_without_an_offset_stays_until_a_whole_view(pb, tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'CODEX_PRIORITY_SCAN_BYTES', 2000)
    i = seat(tmp_path, target=THREAD, priority_from=[BOSS])
    Path(i['log'] + '.codex.priority.json').write_text(json.dumps({'schema_version': 1, 'sent': [['msg/old', 1]]}))
    append(i['log'], *[message('msg/ordinary-%03d' % k) for k in range(60)])
    bank = types.SimpleNamespace(offset=0, held=[], delivered=set())
    pending, scanned, size = pb.codex_pending_priority(i, bank, {}, completed=set(), cursor={'pos': None})
    keep = pb.codex_priority_prune(pb.codex_priority_sent(i), offset=0, size=size, scanned=scanned, examined={},
                                   ident=i)
    assert keep == {('msg/old', 1): None}                                # a partial view keeps it
    keep = pb.codex_priority_prune(keep, offset=0, size=size, scanned=[(0, size)], examined={}, ident=i)
    assert keep == {}                                                    # a whole view lets it go


def _spool_with(i, keys):
    """Write routing lines for `keys` and return each key's byte offset."""
    offs = {}
    for k in keys:
        offs[k] = Path(i['log']).stat().st_size if Path(i['log']).exists() else 0
        append(i['log'], message(k))
    return offs


def test_the_ledger_is_pruned_on_affirmative_evidence_only(pb, tmp_path):
    # d03888 P2-1 and 1c2ca4 P2: bounded by what is really pending, and never pruned on a stale position
    i = seat(tmp_path, target=THREAD)
    Path(i['log']).write_bytes(b'')
    offs = _spool_with(i, ['msg/a', 'msg/b', 'msg/c', 'msg/d'])
    size = Path(i['log']).stat().st_size
    bank_at = offs['msg/c']                                             # a and b are behind the bank
    sent = {('msg/a', 1): offs['msg/a'],          # behind the bank, same line there: consumed, goes
            ('msg/x', 1): offs['msg/b'],          # behind the bank but ANOTHER line there now: stale, kept, no offset
            ('msg/c', 1): offs['msg/c'],          # inside a scanned window, not pending, same line: handled, goes
            ('msg/d', 1): offs['msg/d'],          # pending now at the same place: stays
            ('msg/e', 1): size + 10,              # past the size: a reset, kept with no offset
            ('msg/f', 1): offs['msg/d'] + 5}      # mid-line, outside every scanned window: no evidence, kept
    out = pb.codex_priority_prune(sent, offset=bank_at, size=size, scanned=[(bank_at, offs['msg/d'])],
                                  examined={('msg/d', 1): offs['msg/d']}, ident=i)
    assert ('msg/a', 1) not in out and ('msg/c', 1) not in out
    assert out[('msg/x', 1)] is None and out[('msg/e', 1)] is None
    assert out[('msg/d', 1)] == offs['msg/d'] and out[('msg/f', 1)] == offs['msg/d'] + 5


def test_a_pending_pair_keeps_its_entry_at_its_new_offset_after_a_rewrite(pb, tmp_path, monkeypatch):
    # 1c2ca4 P2: [urgent, ordinary, tail] rewritten to the same size as [ordinary, urgent, tail]; the bank takes the
    # ordinary prefix; urgent is still pending at its NEW offset and must not be handed on again
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    urgent, ordinary, tail = (message('msg/urgent', from_peer_id=BOSS), message('msg/ordin'), message('msg/tail0'))
    Path(i['log']).write_bytes(b'')
    append(i['log'], urgent, ordinary, tail)
    queued, _ = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=10)
    assert 'msg/urgent' in queued[0]
    size = Path(i['log']).stat().st_size
    Path(i['log']).write_bytes(b'')
    append(i['log'], ordinary, urgent, tail)
    assert Path(i['log']).stat().st_size == size
    first_len = len(json.dumps(ordinary).encode()) + 1
    bank = types.SimpleNamespace(offset=first_len, held=[], delivered=set())   # the bank consumed the ordinary line
    pending, scanned, size = pb.codex_pending_priority(i, bank, {}, completed=set(), cursor={'pos': None})
    assert ('msg/urgent', 1) in pending
    keep = pb.codex_priority_prune(pb.codex_priority_sent(i), offset=bank.offset, size=size, scanned=scanned, ident=i,
                                   examined={p: off for p, (_d, off) in pending.items()})
    assert keep.get(('msg/urgent', 1)) == first_len                      # kept, at its new place: not handed on again


def test_two_adjacent_windows_are_a_whole_view(pb):
    # 1c2ca4 P3: merged coverage, not one window spanning everything
    assert pb.codex_covered([(0, 600), (600, 750)], 0, 750) is True
    assert pb.codex_covered([(0, 600), (601, 750)], 0, 750) is False       # a real gap
    assert pb.codex_covered([(100, 600)], 0, 600) is False
    out = pb.codex_priority_prune({('old', 1): None}, offset=0, size=750, scanned=[(0, 600), (600, 750)],
                                  examined={}, ident={'log': 'unused'})
    assert out == {}


def test_a_sustained_partial_view_does_not_grow_the_ledger(pb, tmp_path, monkeypatch):
    # d03888 P2-1: the bank advances past handed-on lines while the view stays partial; their entries go
    monkeypatch.setattr(pb, 'CODEX_PRIORITY_SCAN_BYTES', 2000)
    i = seat(tmp_path, target=THREAD, priority_from=[BOSS])
    sizes = []
    for round_ in range(6):
        append(i['log'], message('msg/p-%02d' % round_, from_peer_id=BOSS),
               *[message('msg/o-%02d-%02d' % (round_, k)) for k in range(30)])
    bank = types.SimpleNamespace(offset=0, held=[], delivered=set())
    cursor = {'pos': None}
    size = Path(i['log']).stat().st_size
    sent = {}
    for step in range(12):
        pending, scanned, size = pb.codex_pending_priority(i, bank, {}, completed=set(), cursor=cursor)
        sent = pb.codex_priority_prune(sent, offset=bank.offset, size=size, scanned=scanned, ident=i,
                                       examined={p: off for p, (_d, off) in pending.items()})
        sent.update({p: off for p, (_d, off) in pending.items()})        # handed on
        sizes.append(len(sent))
        bank.offset = min(size, bank.offset + size // 8)                 # the bank consumes about an eighth a step
    assert max(sizes) <= 6 and sizes[-1] <= 1                            # never more than what is really pending


def test_a_restart_never_hands_a_sent_line_on_again(pb, tmp_path, monkeypatch):
    # c0f756 P2-1: the ledger is pruned only from a COMPLETE view; a fresh run that sees part of the region forgets
    # nothing, so it never hands a sent line on again
    monkeypatch.setattr(pb, 'CODEX_PRIORITY_SCAN_BYTES', 2000)
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])                       # the bank stays held
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    append(i['log'], message('msg/early', from_peer_id=BOSS))
    append(i['log'], *[message('msg/ordinary-%03d' % k) for k in range(40)])
    append(i['log'], message('msg/late', from_peer_id=BOSS))
    queued, _ = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=2, max_sleeps=20)
    sent_keys = [k for n in queued for k in ('msg/early', 'msg/late') if k in n]
    assert sorted(sent_keys) == ['msg/early', 'msg/late']               # each once, early from the head window
    with pytest.raises(AssertionError, match='no queue call within'):  # the "restart": a fresh loop, same files
        run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=20)


def test_a_spool_rewritten_to_the_same_size_is_read_as_it_is_now(pb, tmp_path):
    # c0f756 P2-2: nothing is cached between polls, so an equal-size rewrite cannot hide new mail
    i = seat(tmp_path, target=THREAD, priority_from=[BOSS])
    bank = types.SimpleNamespace(offset=0, held=[], delivered=set())
    append(i['log'], message('msg/old', from_peer_id=BOSS))
    size = Path(i['log']).stat().st_size
    assert list(pb.codex_pending_priority(i, bank, {}, completed=set())[0]) == [('msg/old', 1)]
    Path(i['log']).write_bytes(b'')
    append(i['log'], message('msg/new', from_peer_id=BOSS))
    assert Path(i['log']).stat().st_size == size
    assert list(pb.codex_pending_priority(i, bank, {}, completed=set())[0]) == [('msg/new', 1)]


def test_handled_lines_never_take_room_from_a_new_one(pb, tmp_path):
    # c0f756 P2-3: there is no discovery cache to fill; 1,100 handled lines and one new one
    i = seat(tmp_path, target=THREAD, priority_from=[BOSS])
    append(i['log'], *[message('msg/done-%04d' % k, from_peer_id=BOSS) for k in range(1100)])
    append(i['log'], message('msg/fresh', from_peer_id=BOSS))
    bank = types.SimpleNamespace(offset=0, held=[], delivered=set())
    done = {('msg/done-%04d' % k, 1) for k in range(1100)}
    pending, scanned, size = pb.codex_pending_priority(i, bank, {}, completed=done)
    assert scanned == [(0, size)] and [d['key'] for d, _off in pending.values()] == ['msg/fresh']


@pytest.mark.parametrize('extra, want', [
    ({}, None),
    ({'codex_home': 'X/home'}, 'X/home/queue_1.sqlite'),
    ({'codex_sessions_dir': 'X/home2/sessions'}, 'X/home2/queue_1.sqlite'),
    ({'codex_home': 'X/h', 'codex_queue_db': 'X/explicit.sqlite'}, 'X/explicit.sqlite'),
])
def test_the_queue_store_is_the_seats_own_codex_homes(pb, tmp_path, extra, want):
    # Codex on #579: a seat with its own codex_home or codex_sessions_dir reads ITS queue, not ~/.codex's
    ident = {k: v.replace('X', str(tmp_path)) for k, v in extra.items()}
    got = pb.codex_queue_db_path(ident)
    if want is None:
        assert got == pb.CODEX_QUEUE_DB
    else:
        assert os.path.normpath(got) == os.path.normpath(want.replace('X', str(tmp_path)))


def test_a_priority_line_the_seat_handled_is_not_handed_on_but_a_newer_version_is(pb, tmp_path, monkeypatch):
    # bce2c7 P3-1: the bank's exact handled set applies to the lane; v1 handled does not hide v2
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db, priority_from=[BOSS])
    monkeypatch.setattr(pb.delivery_bank, 'handled', lambda *a, **k: {('msg/directive', 1)})
    append(i['log'], message('msg/directive', from_peer_id=BOSS))
    with pytest.raises(AssertionError, match='no queue call within'):
        run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=10)
    append(i['log'], message('msg/directive', from_peer_id=BOSS, version=2))
    queued, _ = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=10)
    assert queued[0].startswith('[PRIORITY] ') and 'msg/directive' in queued[0]


def test_a_stale_queue_status_clears_when_progress_resumes(pb, tmp_path, monkeypatch):
    # bce2c7 P3-2: stale -> (same count, new revision) clears the published degradation, not only a drain
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE queued_thread_revisions (revision INTEGER PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE)')
    con.execute('INSERT INTO queued_thread_revisions VALUES (1, ?)', (THREAD,))
    con.commit()
    con.close()
    i = seat(tmp_path, target=THREAD, codex_queue_db=db)
    append(i['log'], message('msg/ordinary'))
    clock = [1000.0]
    monkeypatch.setattr(pb, '_codex_clock', lambda: clock[0])
    statuses = []
    monkeypatch.setattr(pb, 'set_status', lambda name, **kw: statuses.append(kw))
    moved = []

    def during(seconds, n, k):
        clock[0] += 60 if seconds == 2 else seconds
        if any(s.get('codex_queue_stale') == 1 for s in statuses) and not moved:
            moved.append(True)                                           # Codex takes an item: the revision moves
            c = sqlite3.connect(db)
            c.execute('UPDATE queued_thread_revisions SET revision = 2')
            c.commit()
            c.close()
    with pytest.raises(AssertionError, match='no queue call within'):
        run_loop(pb, monkeypatch, i, during, stop_after=5, max_sleeps=25)
    went_stale = next(n for n, s in enumerate(statuses) if s.get('codex_queue_stale') == 1)
    assert any('codex_queue_stale' in s and s['codex_queue_stale'] is None for s in statuses[went_stale + 1:])


def test_with_the_lane_off_the_unknown_turn_gap_is_the_old_blind_sleep(pb, tmp_path, monkeypatch):
    # CONTROL: a seat with neither "priority_lane" nor "priority_from" keeps today's pacing exactly
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'))
    append(i['log'], message('msg/first'))
    queued, sleeps = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1)
    assert len(queued) == 1 and sleeps[-1] == pb.CODEX_MIN_GAP_S


@pytest.mark.parametrize('ident, on', [({}, False), ({'priority_lane': True}, True), ({'priority_lane': 'yes'}, False),
                                       ({'priority_from': ['x']}, True), ({'priority_from': []}, False)])
def test_the_lane_switch(pb, ident, on):
    assert pb.codex_priority_lane_on(ident) is on


def test_time_behind_a_running_turn_does_not_count_toward_a_stale_queue(pb, tmp_path, monkeypatch):
    # Codex on #579 (ba6665c4): an unchanged one-row queue seen before a 14-minute turn must not read stale the
    # moment the turn ends; the timer restarts when the turn does
    db = queue_db(tmp_path / 'q.sqlite', [THREAD])
    i = seat(tmp_path, target=THREAD, codex_queue_db=db)
    append(i['log'], message('msg/ordinary'))
    clock = [1000.0]
    monkeypatch.setattr(pb, '_codex_clock', lambda: clock[0])
    statuses = []
    monkeypatch.setattr(pb, 'set_status', lambda name, **kw: statuses.append(kw))
    monkeypatch.setattr(pb, 'codex_rollouts', lambda ident: ['fake-rollout'])
    turn_window = (1100.0, 1100.0 + 14 * 60)                              # a turn runs 14 minutes

    class FakeTurn:
        def __init__(self, path):
            pass

        def update(self):
            running = turn_window[0] <= clock[0] < turn_window[1]
            return running, (clock[0] if not running else None)
    monkeypatch.setattr(pb, 'CodexTurn', FakeTurn)

    def during(seconds, n, k):
        clock[0] += 60 if seconds == 2 else seconds                       # each poll stands for a minute
        if clock[0] > turn_window[1] + 8 * 60:                           # 8 minutes after the turn ended
            raise Stop()
    queued, _ = run_loop(pb, monkeypatch, i, during, stop_after=1, max_sleeps=60)   # during() stops it
    assert queued == []                                                  # the queue hold still holds
    assert not any(s.get('codex_queue_stale') for s in statuses)         # still a healthy queue being waited on


def test_a_truncated_spool_never_erases_the_ledger_and_no_line_goes_twice(pb, tmp_path, monkeypatch):
    # 83df56 P2: a checkpoint past the end (truncation) is no evidence anything is done; when the spool comes back the
    # same urgent line must not be handed on again
    i = seat(tmp_path, target=THREAD, priority_from=[BOSS])
    Path(i['log']).write_bytes(b'')
    append(i['log'], message('msg/ordinary-1'), message('msg/ordinary-2'))
    urgent_at = Path(i['log']).stat().st_size
    append(i['log'], message('msg/urgent', from_peer_id=BOSS))
    original = Path(i['log']).read_bytes()
    pb.codex_priority_record(i, {('msg/urgent', 1): urgent_at})          # handed on earlier
    Path(i['log'] + '.codex.offset').write_text(str(urgent_at))           # the bank consumed the two ordinary lines
    Path(i['log']).write_bytes(json.dumps(message('msg/urgent', from_peer_id=BOSS)).encode() + b'\n')
    assert Path(i['log']).stat().st_size < urgent_at                     # the checkpoint now lies past the end
    try:
        run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=6)
    except Exception:                                                    # the bank may refuse the checkpoint: fine
        pass
    assert pb.codex_priority_sent(i) == {('msg/urgent', 1): urgent_at}   # untouched
    Path(i['log']).write_bytes(original)                                 # the spool comes back
    queued = []
    try:
        queued, _ = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=1, max_sleeps=8)
    except AssertionError:
        pass
    assert not any('[PRIORITY]' in n and 'msg/urgent' in n for n in queued)   # never handed on twice


def test_at_the_end_of_the_spool_the_ledger_goes_only_on_evidence(pb, tmp_path):
    i = seat(tmp_path, target=THREAD, priority_from=[BOSS])
    Path(i['log']).write_bytes(b'')
    append(i['log'], message('msg/urgent', from_peer_id=BOSS))
    size = Path(i['log']).stat().st_size
    out = pb.codex_priority_prune({('msg/urgent', 1): 0, ('msg/gone', 1): 0}, offset=size, size=size,
                                  scanned=[(size, size)], examined={}, ident=i)
    assert out == {('msg/gone', 1): None}          # urgent: the line at 0 is it, consumed; gone: another line there


def test_the_servers_directive_flag_survives_the_monitor_into_the_priority_lane(pb, tmp_path):
    # GitHub Codex on 73250b84: the flag must reach the lane through the real delivery path. The server half (#581)
    # puts directive: true on a `to` frame; the monitor copies every frame field but event/via onto the spool line
    # (tools/meshy_node/monitor.py _message); delivery_bank.routing keeps it; codex_priority reads it.
    from tests.test_meshy_node_monitor import _frame, _unit_monitor
    m, out = _unit_monitor(tmp_path / 'mon')
    assert m._message(_frame("msg/1-a-000001", to=["seat"], role="to", directive=True), "live") == 1
    assert m._message(_frame("msg/1-a-000002", to=["seat"], role="to"), "live") == 1
    lines = [json.loads(x) for x in out.getvalue().splitlines() if '"event": "message"' in x]
    flagged, plain = (pb.delivery_bank.routing(lines[0]), pb.delivery_bank.routing(lines[1]))
    assert flagged.get('directive') is True and 'directive' not in plain
    assert pb.codex_priority({}, flagged) is True and pb.codex_priority({}, plain) is False   # no config needed


def test_a_line_the_lane_handed_on_is_not_handed_on_again_by_the_ordinary_batch(pb, tmp_path, monkeypatch):
    # pxx 220cb6 P2-1 (its probe test_d_no_duplicate_between_priority_and_normal_bank_delivery): lane on, turn unknown,
    # the Codex queue store unreadable, no read or handled receipt yet, spool [ordinary, directive]. The directive goes
    # once as [PRIORITY]; the ordinary batch then carries only the ordinary line
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_lane=True)
    append(i['log'], message('msg/ordinary'), message('msg/urgent', directive=True))
    queued, _ = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=2, max_sleeps=400)   # ordinary waits the gap
    import re as _re
    keys = [k for n in queued for k in _re.findall(r'key=(msg/[a-z]+)', n)]
    assert keys.count('msg/urgent') == 1 and keys.count('msg/ordinary') == 1, queued
    assert queued[0].startswith('[PRIORITY] ') and 'msg/urgent' in queued[0]


def test_a_batch_made_only_of_lane_sent_lines_queues_nothing_but_still_commits(pb, tmp_path, monkeypatch):
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_lane=True)
    append(i['log'], message('msg/urgent', directive=True))
    with pytest.raises(AssertionError, match='no queue call within'):
        run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=2, max_sleeps=400)    # past the gap
    bank = json.loads(Path(i['log'] + '.codex.offset.bank.json').read_text())
    assert bank['offset'] == Path(i['log']).stat().st_size              # consumed, not re-sent


def test_ordinary_mail_gets_a_turn_after_a_streak_of_priority_notices(pb, tmp_path, monkeypatch):
    # pxx 220cb6 P3: continuous priority arrivals cannot starve ordinary delivery
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_from=[BOSS])
    append(i['log'], message('msg/ordinary'))
    n = [0]

    def during(seconds, q, k):
        if seconds == 2:                                                 # a new priority line before every poll
            n[0] += 1
            append(i['log'], message('msg/p-%03d' % n[0], from_peer_id=BOSS))
    limit = pb.CODEX_MIN_GAP_S // 2 + pb.CODEX_PRIORITY_STREAK_MAX + 2   # polls of 2 s: the gap, then one streak
    queued, _ = run_until(pb, monkeypatch, i, during, done=lambda q: any('msg/ordinary' in n for n in q),
                          max_sleeps=4 * limit)
    ordinary_at = next(k for k, notice in enumerate(queued) if 'msg/ordinary' in notice)
    assert ordinary_at <= limit, ordinary_at              # bounded: the gap is NOT re-armed by every priority notice


def test_a_priority_hand_off_restarts_the_gap_for_ordinary_mail(pb, tmp_path, monkeypatch):
    # GitHub Codex on 684784fc: unknown turn, unreadable queue store, ordinary mail banked. The priority notice goes at
    # once, but the ordinary one must wait the configured gap after it, not ride in 30 s later behind it
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_lane=True)
    append(i['log'], message('msg/ordinary'), message('msg/urgent', from_peer_id=BOSS, directive=True))
    queued, sleeps = run_loop(pb, monkeypatch, i, lambda *a: None, stop_after=2, max_sleeps=400)
    assert queued[0].startswith('[PRIORITY] ') and 'msg/ordinary' in queued[1]
    # the first notice went after the first 2 s poll; the second just before the last sleep
    assert sum(sleeps[:-1]) - sleeps[0] >= pb.CODEX_MIN_GAP_S             # the gap held the ordinary notice back


def test_a_fresh_burst_after_a_drained_idle_starts_its_own_ordinary_gap(pb, tmp_path, monkeypatch):
    # codex-kxxx-cli 2cdc8d: after a priority-only burst drains and the seat idles, a NEW burst's ordinary mail must wait
    # the gap after the new priority notice, not follow it at once on the old, expired gap
    i = seat(tmp_path, target=THREAD, codex_queue_db=str(tmp_path / 'absent.sqlite'), priority_from=[BOSS],
             codex_min_gap_s=60, codex_settle_s=0)
    append(i['log'], message('msg/early', from_peer_id=BOSS))
    state = {'poll': 0, 'fresh': None}

    def during(seconds, n, k):
        state['poll'] += 1
        if state['fresh'] is None and n >= 1 and state['poll'] > 1 + 120 // 2:   # 120 s of idle after the first notice
            append(i['log'], message('msg/ordinary'), message('msg/late', from_peer_id=BOSS))
            state['fresh'] = state['poll']
    queued, sleeps = run_loop(pb, monkeypatch, i, during, stop_after=3, max_sleeps=400)
    assert 'msg/early' in queued[0] and 'msg/late' in queued[1] and 'msg/ordinary' in queued[2], [q[:30] for q in queued]
    late_at = sum(sleeps[:state['fresh'] + 1])          # the fresh priority notice went on the poll after the append
    ordinary_at = sum(sleeps[:-1])
    assert ordinary_at - late_at >= 60 - 2, (late_at, ordinary_at)      # its own gap, one 2 s poll of slack


def test_a_delivered_line_replayed_into_the_spool_is_not_handed_on_again(pb, tmp_path):
    # GitHub Codex on 2c853e08 (pxx c0cc76): scanned lines check the bank's delivery receipts as held lines do, so a
    # frame replayed after a listener reconnect, already delivered by the bank, gets no second turn from the lane
    i = seat(tmp_path, target=THREAD, priority_from=[BOSS])
    Path(i['log']).write_bytes(b'')
    replayed = message('msg/replayed', from_peer_id=BOSS, directive=True)
    fresh = message('msg/fresh', from_peer_id=BOSS, directive=True)
    append(i['log'], replayed, fresh)
    delivered = {pb.delivery_bank.token(pb.delivery_bank.routing(replayed))}
    bank = types.SimpleNamespace(offset=0, held=[], delivered=delivered)
    pending, scanned, size = pb.codex_pending_priority(i, bank, {}, completed=set(), cursor={'pos': None})
    assert set(pending) == {('msg/fresh', 1)}                            # the replay is the bank's, already delivered
    newer = message('msg/replayed', from_peer_id=BOSS, directive=True, version=2)
    append(i['log'], newer)
    pending, _s, _z = pb.codex_pending_priority(i, bank, {}, completed=set(), cursor={'pos': None})
    assert ('msg/replayed', 2) in pending                               # a NEW version still goes: exact identity
