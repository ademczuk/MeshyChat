"""Incident #58: bursts are one turn, handling is exact-version, failures retain work.

Temporary spools and stubbed CLI/pipe delivery only; no production state or sockets.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from tests.test_postbox_reconcile import pb, ident  # noqa: F401
from tests.test_postbox_wait_next_once import wn  # noqa: F401

PB = Path(__file__).resolve().parent.parent / 'tools/postbox'
sys.path.insert(0, str(PB))
import delivery_bank as bank  # noqa: E402
import mark_handled  # noqa: E402
import postbox_os  # noqa: E402


def message(key='msg/fixture', version=1, **kw):
    return dict(event='message', role='to', key=key, version=version, **kw)


def append(log, *events):
    with open(log, 'ab') as f:
        for event in events:
            f.write(json.dumps(event).encode() + b'\n')


def setup(tmp_path, **kw):
    i = ident(tmp_path, **kw)
    Path(i['log']).touch()
    offset = i['log'] + '.test.offset'
    return i, bank.Bank(i, offset, 0, 'fixture')


def wanted(d):
    return d.get('event') in ('message', 'edit', 'status', 'supervisor')


def receipt(i, entries):
    path = str(Path(i['log']).with_suffix('.handled.json'))
    bank.atomic_json(path, {'schema_version': 1, 'identity': i['name'], 'handled': entries})
    i['handled_file'] = path
    i['handled_private_acl_verified'] = True


def handled(key='msg/fixture', version=1):
    return dict(key=key, version=version, disposition='done', signature_verified=True, status_verified=True)


def test_distinct_multipart_keys_banked_once(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], *[message('msg/part-%s' % n) for n in range(7)])
    end, events = b.collect(wanted)
    assert len(events) == 7
    b.commit(end, events)
    assert b.collect(wanted)[1] == []


def test_handled_parts_do_not_hide_interleaved_unknown_request(tmp_path):
    i, b = setup(tmp_path)
    receipt(i, [handled('msg/part1'), handled('msg/part2')])
    append(i['log'], message('msg/part1'), message('msg/action'), message('msg/part2'))
    end, events = b.collect(wanted)
    assert [d['key'] for d in events] == ['msg/action']
    b.commit(end, events)
    assert end == Path(i['log']).stat().st_size


def test_handled_record_is_exact_version_not_bare_key(tmp_path):
    i, b = setup(tmp_path)
    receipt(i, [handled()])
    append(i['log'], message(), message(version=2), dict(message(version=3), event='edit'))
    assert [d['version'] for d in b.collect(wanted)[1]] == [2, 3]


def test_dispatch_dedup_preserves_new_version_and_edit(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], message(), message(), message(version=2), dict(message(version=2), event='edit'))
    end, events = b.collect(wanted)
    assert [(d['event'], d['version']) for d in events] == [('message', 1), ('message', 2), ('edit', 2)]
    b.commit(end, events)
    append(i['log'], message(), message(version=3))
    fresh = bank.Bank(i, b.offset_file, 0, 'fixture')
    assert [d['version'] for d in fresh.collect(wanted)[1]] == [3]


def test_commit_failure_does_not_mutate_same_memory_or_disk(tmp_path, monkeypatch):
    i, b = setup(tmp_path)
    append(i['log'], message())
    end, events = b.collect(wanted)
    def fail(*a):
        raise OSError('fixture disk failure')
    monkeypatch.setattr(bank, 'atomic_json', fail)
    with pytest.raises(OSError):
        b.commit(end, events)
    assert b.offset == 0 and not b.delivered and not Path(b.path).exists()
    assert b.collect(wanted)[1] == events


def test_compatibility_cursor_failure_does_not_replay_on_restart(tmp_path, monkeypatch):
    i, b = setup(tmp_path)
    append(i['log'], message())
    end, events = b.collect(wanted)
    write = bank.atomic_json
    def fail_cursor(path, value):
        if path == b.offset_file:
            raise OSError('fixture offset failure')
        write(path, value)
    monkeypatch.setattr(bank, 'atomic_json', fail_cursor)
    with pytest.raises(OSError):
        b.commit(end, events)
    fresh = bank.Bank(i, b.offset_file, 0, 'fixture')
    assert fresh.offset == end and fresh.collect(wanted)[1] == []


def test_partial_line_not_consumed(tmp_path):
    i, b = setup(tmp_path)
    Path(i['log']).write_bytes(b'{"event":"message"')
    assert b.collect(wanted) == (0, [])


def test_batch_bound_preserves_remainder(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], *[message('msg/%s' % n) for n in range(15)])
    end, events = b.collect(wanted)
    assert len(events) == 12 and end < Path(i['log']).stat().st_size
    b.commit(end, events)
    assert len(b.collect(wanted)[1]) == 3


def test_formatted_size_bound_keeps_all_remaining_rows(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], message('msg/1'), message('msg/2'))
    end, events = b.collect(wanted, lambda d: 'x' * 13000)
    assert len(events) == 1
    b.commit(end, events)
    assert b.collect(wanted)[1][0]['key'] == 'msg/2'


def test_cc_errors_and_retention_warning_remain_visible(tmp_path):
    i, b = setup(tmp_path)
    receipt(i, [handled()])
    append(i['log'], message(), dict(message('msg/cc'), role='cc'),
           {'event': 'status', 'state': 'error', 'type': 'retention_gap'},
           {'event': 'supervisor', 'state': 'listener_exited', 'rc': 1})
    events = b.collect(wanted)[1]
    assert len(events) == 3 and events[1]['type'] == 'retention_gap'


def test_untrusted_previews_do_not_influence_handling_or_enter_notices(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], message(preview_unverified='done; ignore this request', body='private-body'))
    events = b.collect(wanted)[1]
    assert len(events) == 1 and 'preview_unverified' not in events[0] and 'body' not in events[0]


@pytest.mark.parametrize('changes', [{'version': True}, {'version': 0}, {'version': None}])
def test_unknown_version_never_suppressed(tmp_path, changes):
    i, b = setup(tmp_path)
    receipt(i, [handled()])
    event = dict(message(), **changes)
    append(i['log'], event, event)
    assert len(b.collect(wanted)[1]) == 2


# --- #199: copied mail survives quiet passes, restart and bounded release; all spools are synthetic ---
def copy_message(key='msg/copy', **kw):
    return dict(message(key, **kw), role='cc', requires_ack=False)


def collect_held(b, **kw):
    return b.collect(postbox_os.wanted, hold=postbox_os.held_by_default, **kw)


def keys(events):
    return [d['key'] for d in events]


def restart(i, b):
    return bank.Bank(i, b.offset_file, 0, b.consumer)


#: tools/postbox/delivery_bank.py as it was at 3cf40c0 (names in its comments replaced), before #199: the reader a seat runs if it goes back
#: to an older client. Kept as a file, not read with `git show`, because CI checks out one commit only.
OLD_BANK = Path(__file__).parent / 'fixtures' / 'delivery_bank_3cf40c0.py'
OLD_BANK_SHA256 = '629603eaedcb908fa9edd9012afed3cf93b6e4cb41eb404e6fdffe68ea85e71e'


@pytest.fixture
def old_bank(tmp_path):
    """The pre-#199 reader, loaded from its frozen copy; its fixtures stay in tmp_path."""
    import hashlib
    path = tmp_path / 'delivery_bank_3cf40c0.py'
    path.write_bytes(OLD_BANK.read_bytes())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == OLD_BANK_SHA256, 'the frozen pre-#199 reader changed'
    spec = importlib.util.spec_from_file_location('delivery_bank_3cf40c0', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_old_reader_refuses_held_checkpoint_and_accepts_empty_v1(tmp_path, old_bank):
    i, b = setup(tmp_path)
    append(i['log'], copy_message())
    b.commit(*collect_held(b))
    with pytest.raises(ValueError, match='identity/consumer mismatch'):
        old_bank.Bank(i, b.offset_file, 0, b.consumer)
    assert bank.private_json(b.path)['schema_version'] == 2
    b = restart(i, b)  # the new reader accepts v2 and preserves its held work
    b.commit(*b.collect(wanted, hold=None))
    assert bank.private_json(b.path)['schema_version'] == 1
    old = old_bank.Bank(i, b.offset_file, 0, b.consumer)
    assert old.offset == b.offset and old.delivered == b.delivered
    before = Path(b.path).read_bytes()
    old.commit(old.offset, [])
    assert Path(b.path).read_bytes() == before  # exact legacy bytes, not just equivalent JSON


@pytest.mark.parametrize('second_budget', [200, 300])
def test_stale_collect_commit_refused_without_losing_held_mail(tmp_path, second_budget):
    i, b = setup(tmp_path)
    copies = [copy_message('msg/copy-1'), copy_message('msg/copy-2')]
    append(i['log'], *copies)
    b.commit(*collect_held(b))
    before = Path(b.path).read_bytes()
    append(i['log'], message('msg/wake'))
    render = lambda d: 'x' * 100
    first = collect_held(b, render=render, max_bytes=100)
    latest = collect_held(b, render=render, max_bytes=second_budget)
    assert keys(first[1]) == keys(copies[:1])
    assert keys(latest[1])[:2] == keys(copies)
    assert (first[0] == latest[0]) is (second_budget == 200)
    with pytest.raises(ValueError, match='latest collected batch'):
        b.commit(*first)
    with pytest.raises(ValueError, match='latest collected batch'):
        b.commit(latest[0], list(latest[1]))  # equal contents are not the returned events object
    with pytest.raises(ValueError, match='latest collected batch'):
        b.commit(latest[0] + 1, latest[1])  # the events object alone is not enough
    assert Path(b.path).read_bytes() == before and not b.delivered and len(b.held) == 2
    seen = keys(latest[1])
    b.commit(*latest)  # rejecting a stale batch must leave this result usable
    b = restart(i, b)
    end, events = collect_held(b)
    seen += keys(events)
    b.commit(end, events)
    assert seen == keys(copies) + ['msg/wake']
    assert not b.held and collect_held(restart(i, b))[1] == []


@pytest.mark.parametrize('then_wake', [True, False])
def test_stale_quiet_commit_cannot_move_the_cursor_past_held_mail(tmp_path, then_wake):
    """Round 2 of the local review: a quiet result committed after a later collect put the cursor past a copy
    that was held only in memory, so after a restart it was neither delivered nor held."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message('msg/copy-1'))
    stale = collect_held(b)  # quiet: copy-1 held in memory, cursor past it
    assert stale[1] == [] and stale[0] > b.offset
    append(i['log'], message('msg/wake') if then_wake else copy_message('msg/copy-2'))
    latest = collect_held(b)
    before = Path(b.path).read_bytes() if Path(b.path).exists() else None
    with pytest.raises(ValueError, match='latest collected batch'):
        b.commit(*stale)
    assert (Path(b.path).read_bytes() if Path(b.path).exists() else None) == before
    assert b.offset == 0 and not b.delivered and b.held == []
    seen = keys(latest[1])
    b.commit(*latest)
    b = restart(i, b)
    if not then_wake:
        append(i['log'], message('msg/wake'))
    end, events = collect_held(b)
    seen += keys(events)
    b.commit(end, events)
    expected = ['msg/copy-1', 'msg/wake'] if then_wake else ['msg/copy-1', 'msg/copy-2', 'msg/wake']
    assert seen == expected
    assert not b.held and collect_held(restart(i, b))[1] == []


def test_unmatched_empty_commit_preserves_held_state(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], copy_message('msg/copy-1'), copy_message('msg/copy-2'))
    collect_held(b)
    b.commit(b.offset, [])  # startup save does not commit the advanced quiet result
    assert b.offset == 0 and b.held == []
    b.commit(*collect_held(b))
    before = list(b.held)
    append(i['log'], message('msg/wake'))
    end, events = collect_held(b, render=lambda d: 'x' * 100, max_bytes=100)
    assert end == b.offset and len(events) == 1
    b.commit(end, [])  # same end, but this was not a quiet collect
    assert b.held == before and not b.delivered
    assert keys(collect_held(restart(i, b))[1]) == ['msg/copy-1', 'msg/copy-2', 'msg/wake']


def test_held_only_pass_saves_cursor_and_held_on_empty_commit(tmp_path):
    """#199 mutant H2: omitting held from the no-event checkpoint loses the consumed copy on restart."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message(body='private fixture body', preview_unverified='private preview'))
    end, events = collect_held(b)
    assert events == [] and end == Path(i['log']).stat().st_size and end > 0
    assert b.offset == 0 and b.held == []             # collect has not committed anything
    b.commit(end, [])  # a fresh empty list is accepted for the matching quiet pass
    saved = bank.private_json(b.path)
    assert saved['offset'] == end and saved['delivered'] == []
    [h] = saved['held']
    assert h['token'] == bank.token(copy_message()) and h['routing'] == bank.routing(copy_message())
    assert h['requires_ack'] is False and 'requires_ack' not in h['routing']
    assert bank._epoch(h['held_at']) is not None
    assert 'private' not in Path(b.path).read_text()
    fresh = restart(i, b)
    assert fresh.held == saved['held'] and collect_held(fresh) == (end, [])
    assert dispatch_lines(b.offset_file) == []


def test_held_restart_releases_once_before_waking_line(tmp_path):
    """#199 mutant H1: ignoring the released list consumes copies instead of delivering them."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message('msg/first'), copy_message('msg/second'))
    b.commit(*collect_held(b))
    b = restart(i, b)
    append(i['log'], message('msg/wake'))
    end, events = collect_held(b)
    assert keys(events) == ['msg/first', 'msg/second', 'msg/wake']
    b.commit(end, events)
    append(i['log'], copy_message('msg/first'), message('msg/wake'))
    b = restart(i, b)
    assert collect_held(b)[1] == [] and b.held == []
    assert set(bank.private_json(b.path)['delivered']) == {bank.token(d) for d in events}


@pytest.mark.parametrize('d', [message(requires_ack=False), dict(copy_message(), requires_ack=True),
                              dict(message(), role='cc')])
def test_to_ack_copy_and_legacy_copy_wake_immediately(tmp_path, d):
    """#199 mutant H4: treating a missing requires_ack field as false changes today's server-frame behavior."""
    i, b = setup(tmp_path)
    append(i['log'], d)
    end, events = collect_held(b)
    assert events == [bank.routing(d)] and end == Path(i['log']).stat().st_size
    b.commit(end, events)
    assert not b.held and 'held' not in bank.private_json(b.path)


def copy_edit(key='msg/copy', version=2, role='cc'):
    """An edit line as monitor.py writes it: no requires_ack field, so the hold predicate alone cannot hold it."""
    return dict(event='edit', key=key, version=version, edited_at='2026-10-19T17:35:06Z', edited_by='pxx', role=role)


def test_edit_of_a_held_copy_is_held_with_it(tmp_path):
    """#199 follow-up (oxxxxxx a44fde, 19 Oct 17:35:26Z): a cc edit of a held no-ack copy arrived unheld, released the
    copy with it, and woke the seat with nothing addressed to it. The edit now waits with the copy it edits, and both
    are delivered, in spool order, by the next waking line."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message(), copy_edit())
    end, events = collect_held(b)
    assert events == [] and end == Path(i['log']).stat().st_size
    b.commit(end, events)
    assert [h['routing']['event'] for h in b.held] == ['message', 'edit']
    append(i['log'], message('msg/wake'))
    end, events = collect_held(b)
    assert [(d['event'], d['key']) for d in events] == [('message', 'msg/copy'), ('edit', 'msg/copy'),
                                                         ('message', 'msg/wake')]


@pytest.mark.parametrize('line', [copy_edit(key='msg/not-held'), copy_edit(role='to'), copy_edit(role='replied')])
def test_an_edit_that_follows_no_held_copy_still_wakes(tmp_path, line):
    """The edit rule of docs/design/edit.md stands: only a cc edit of a copy that is held right now waits. A cc edit of
    anything else, and any to or replied edit, wakes the seat and releases what is held."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message(), line)
    end, events = collect_held(b)
    assert [(d['event'], d['key']) for d in events] == [('message', 'msg/copy'), ('edit', line['key'])]


def test_a_keyless_cc_edit_does_not_match_a_keyless_held_copy(tmp_path):
    """a reviewer e27164 P3: a keyless no-ack copy is held under a spool-position token, and a keyless cc edit then
    matched it through None == None and was held too. Two missing keys are not the same message: the edit wakes."""
    i, b = setup(tmp_path)
    keyless_copy = {k: v for k, v in copy_message().items() if k != 'key'}
    keyless_edit = {k: v for k, v in copy_edit().items() if k != 'key'}
    append(i['log'], keyless_copy)
    b.commit(*collect_held(b))
    assert len(b.held) == 1
    append(i['log'], keyless_edit)
    assert [d['event'] for d in collect_held(b)[1]] == ['message', 'edit']


def test_an_edit_of_a_held_copy_already_handled_wakes(tmp_path):
    """GitHub Codex r4321999589 on d44a54ef, reproduced by codex-pxx-cli (0576d8): a held copy whose exact version is
    already in the handled receipt is not waiting any more, so its cc edit must wake rather than wait with it. Checked
    in the same Bank and in a fresh one built from the durable checkpoint."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message())
    b.commit(*collect_held(b))
    assert [h['routing']['key'] for h in b.held] == ['msg/copy']
    receipt(i, [handled('msg/copy')])
    append(i['log'], copy_edit())
    for bank_ in (b, restart(i, b)):
        assert ('edit', 'msg/copy') in [(d['event'], d['key']) for d in collect_held(bank_)[1]]


def test_a_second_edit_wakes_once_the_copy_is_handled_although_the_first_edit_is_held(tmp_path):
    """GitHub Codex r4322906598 on 719880c0 (pxx cd1630): held lines include edits. Copy v1 held, its edit v2 held with
    it, then a receipt for v1, then edit v3: the held v2 edit must not stand in for the copy, so v3 wakes. Checked in the
    same Bank and in a fresh one from the durable checkpoint."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message(), copy_edit(version=2))
    b.commit(*collect_held(b))
    assert [(h['routing']['event'], h['routing'].get('version', 1)) for h in b.held] == [('message', 1), ('edit', 2)]
    receipt(i, [handled('msg/copy')])
    append(i['log'], copy_edit(version=3))
    for bank_ in (b, restart(i, b)):
        assert ('edit', 3) in [(d['event'], d.get('version')) for d in collect_held(bank_)[1]]


def test_an_edit_of_a_held_copy_handled_at_another_version_still_waits(tmp_path):
    """Control for the test above: a receipt for a different version of the copy does not count as handled."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message())
    b.commit(*collect_held(b))
    receipt(i, [handled('msg/copy', version=7)])
    append(i['log'], copy_edit())
    assert collect_held(b)[1] == []


def test_without_the_hold_option_a_cc_edit_wakes_as_before(tmp_path):
    """An identity without hold_cc_without_ack holds nothing, edits included."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message(), copy_edit())
    assert [d['event'] for d in b.collect(postbox_os.wanted)[1]] == ['message', 'edit']


def test_pre_prune_evicts_handled_and_delivered_before_cap_and_age_checks(tmp_path, monkeypatch):
    """Proactively handled or delivered items are pruned from held_lines before evaluating HOLD_MAX_LINES or
    HOLD_MAX_AGE_S, and bank.held_changed is True on the quiet pass so commit() persists the cleaned bank."""
    i, b = setup(tmp_path)
    receipt(i, [])
    old_stamp = '2026-10-13T10:00:00.000Z'
    young_stamp = '2026-10-13T12:00:00.000Z'
    clock = bank._epoch(old_stamp)
    monkeypatch.setattr(bank.time, 'time', lambda: clock)
    monkeypatch.setattr(bank, 'utc_now', lambda: old_stamp)
    append(i['log'], copy_message('msg/handled-old'))
    b.commit(*collect_held(b))

    clock = bank._epoch(young_stamp)
    monkeypatch.setattr(bank, 'utc_now', lambda: young_stamp)
    append(i['log'], copy_message('msg/still-young'))
    b.commit(*collect_held(b))
    assert len(b.held) == 2

    # Advance clock past HOLD_MAX_AGE_S for msg/handled-old, but mark msg/handled-old handled first.
    clock = bank._epoch(old_stamp) + bank.HOLD_MAX_AGE_S + 10
    receipt(i, [handled('msg/handled-old')])
    end, events = collect_held(b)
    # Because msg/handled-old was pre-pruned before the age check, msg/still-young is NOT prematurely released!
    assert events == []
    assert b.held_changed is True
    b.commit(end, events)
    assert [h['routing']['key'] for h in b.held] == ['msg/still-young']
    assert [h['routing']['key'] for h in bank.private_json(b.path)['held']] == ['msg/still-young']


def test_turn_boundary_flushes_held_and_uncollected_cc_items(tmp_path):
    """When at_turn_boundary=True, Bank.collect drains already-held cc items and delivers newly spooled cc items."""
    i, b = setup(tmp_path)
    append(i['log'], copy_message('msg/held-1'), copy_message('msg/held-2'))
    b.commit(*collect_held(b))
    assert len(b.held) == 2

    # Turn-boundary flush with no new spool bytes releases the 2 held items immediately
    end, events = collect_held(b, at_turn_boundary=True)
    assert keys(events) == ['msg/held-1', 'msg/held-2']
    b.commit(end, events)
    assert b.held == []

    # Turn-boundary flush when held is empty but a new cc item is in the spool delivers it immediately
    append(i['log'], copy_message('msg/uncollected-cc'))
    end, events = collect_held(b, at_turn_boundary=True)
    assert keys(events) == ['msg/uncollected-cc']
    b.commit(end, events)
    assert b.held == []

    # Quiet turn-boundary pass with nothing held and nothing in spool stays quiet
    end2, events2 = collect_held(b, at_turn_boundary=True)
    assert events2 == [] and end2 == end and not b.held_changed


def test_peek_bank_is_lock_free_non_mutating_and_filters_handled_and_delivered(tmp_path, monkeypatch):
    """peek_bank inspects held cc items and uncollected tail bytes without opening locks or mutating disk state."""
    i, b = setup(tmp_path)
    receipt(i, [])
    stamp = '2026-10-20T07:00:00.000Z'
    now_ts = bank._epoch(stamp) + 125.5
    monkeypatch.setattr(bank, 'utc_now', lambda: stamp)

    # Create a consumer bank at <log>.wait-session.offset.bank.json
    wait_off = i['log'] + '.wait-session.offset'
    wb = bank.Bank(i, wait_off, 0, 'wait:session')
    append(i['log'],
           dict(copy_message('msg/c1'), **{'from': 'pxx', 'created_at': '2026-10-20T06:59:00Z'}),
           dict(copy_message('msg/c2'), **{'from': 'kxxx', 'created_at': '2026-10-20T06:59:30Z'}))
    wb.commit(*collect_held(wb))
    bank_bytes_before = Path(wb.path).read_bytes()

    # Append an uncollected message to the spool without collecting it
    append(i['log'], message('msg/uncollected'))
    receipt(i, [handled('msg/c1')])

    info = bank.peek_bank(i['log'], consumer='session', ident=i, now=now_ts)
    assert info['ident'] == i['name']
    assert info['consumer'] == 'session'
    assert info['offset'] == wb.offset
    assert info['log_size'] > wb.offset
    assert info['uncollected_bytes'] == info['log_size'] - wb.offset
    assert info['held_count'] == 1
    assert info['oldest_held_age_s'] == 125.5
    assert info['held'] == [{
        'key': 'msg/c2',
        'from': 'kxxx',
        'created_at': '2026-10-20T06:59:30Z',
        'held_at': stamp,
    }]
    # Disk state is completely untouched and no .lock file was created
    assert Path(wb.path).read_bytes() == bank_bytes_before
    assert not Path(i['log'] + '.wait-session.lock').exists()

    # Fallback to <log>.<consumer>.offset.bank.json (e.g. channel / claude-channel consumer)
    ch_off = i['log'] + '.channel.offset'
    cb = bank.Bank(i, ch_off, 0, 'claude-channel')
    cb.commit(*collect_held(cb))
    append(i['log'], dict(copy_message('msg/c3'), **{'from': 'txxxx', 'created_at': '2026-10-20T07:00:00Z'}))
    cb.commit(*collect_held(cb))
    ch_info = bank.peek_bank(i['log'], consumer='channel', ident=i['name'], now=now_ts)
    assert ch_info['ident'] == i['name']
    assert ch_info['consumer'] == 'channel'
    assert ch_info['offset'] == cb.offset
    cch_info = bank.peek_bank(i['log'], consumer='claude-channel', ident=i['name'], now=now_ts)
    assert cch_info['ident'] == i['name']
    assert cch_info['consumer'] == 'claude-channel'
    assert cch_info['offset'] == cb.offset
    assert cch_info['held_count'] == 1
    assert cch_info['held'][0]['key'] == 'msg/c3'


@pytest.mark.parametrize('changes', [{'event': 'edit'}, {'role': 'to'}, {'role': 'broadcast'},
                                   {'requires_ack': True}, {'requires_ack': None}, {'requires_ack': 0},
                                   {'requires_ack': 'false'}])
def test_hold_predicate_requires_message_copy_and_exact_false(changes):
    assert postbox_os.held_by_default(copy_message()) is True
    assert postbox_os.held_by_default(dict(copy_message(), **changes)) is False


def test_held_age_bound_releases_everything_without_new_mail(tmp_path, monkeypatch):
    """#199 mutant H3: without the age check quiet copies can wait forever."""
    i, b = setup(tmp_path)
    stamp = '2026-10-13T10:00:00.000Z'
    monkeypatch.setattr(bank, 'utc_now', lambda: stamp)
    clock = bank._epoch(stamp)
    monkeypatch.setattr(bank.time, 'time', lambda: clock)
    append(i['log'], copy_message('msg/old'))
    b.commit(*collect_held(b))
    clock += bank.HOLD_MAX_AGE_S
    assert collect_held(b)[1] == []                     # strictly older than the bound
    append(i['log'], copy_message('msg/young'))
    monkeypatch.setattr(bank, 'utc_now', lambda: '2026-10-13T12:00:00.000Z')
    b.commit(*collect_held(b))
    clock += 1
    b = restart(i, b)
    end, events = collect_held(b)
    assert keys(events) == ['msg/old', 'msg/young']
    b.commit(end, events)
    assert b.held == [] and collect_held(restart(i, b))[1] == []


@pytest.mark.parametrize('ahead, released', [(0, False), (bank.HOLD_CLOCK_SKEW_S, False),
                                              (bank.HOLD_CLOCK_SKEW_S + 1, True), (86400, True),
                                              (60, False), (61, True)])
def test_held_at_in_the_future_counts_as_expired(tmp_path, monkeypatch, ahead, released):
    """#199 follow-up (mutant H6): after the clock goes back, waiting for it would hold a copy past the age bound."""
    i, b = setup(tmp_path)
    stamp = '2026-10-13T10:00:00.000Z'
    monkeypatch.setattr(bank, 'utc_now', lambda: stamp)
    monkeypatch.setattr(bank.time, 'time', lambda: bank._epoch(stamp))
    append(i['log'], copy_message('msg/ahead'))
    b.commit(*collect_held(b))
    assert len(b.held) == 1
    monkeypatch.setattr(bank.time, 'time', lambda: bank._epoch(stamp) - ahead)
    end, events = collect_held(restart(i, b))
    assert keys(events) == (['msg/ahead'] if released else [])


def test_hold_options_reads_only_true_and_reports_any_other_value():
    """#199 follow-up (mutant H7): a typo such as "true" holds nothing, and says so once."""
    seen = []
    assert postbox_os.hold_options({'name': 's'}, seen.append) == {}
    assert postbox_os.hold_options({'name': 's', 'hold_cc_without_ack': False}, seen.append) == {}
    assert postbox_os.hold_options({'name': 's', 'hold_cc_without_ack': True}, seen.append) == {
        'hold': postbox_os.held_by_default}
    assert seen == []
    for value in ('true', 1, None, 'yes'):
        assert postbox_os.hold_options({'name': 's', 'hold_cc_without_ack': value}, seen.append) == {}
    assert seen == ["s: hold_cc_without_ack=%r is not true or false; nothing is held" % v for v in ('true', 1, None, 'yes')]
    assert postbox_os.hold_options({'name': 's', 'hold_cc_without_ack': 'true'}) == {}   # no report callback: silent

    def broken(text):                    # Codex r1 on a05d949: postbox.note catches only OSError
        raise UnicodeEncodeError('utf-8', text, 0, 1, 'the log cannot take it')
    assert postbox_os.hold_options({'name': 's', 'hold_cc_without_ack': 'true'}, broken) == {}


def test_held_count_bound_releases_all_across_budgets_and_restarts(tmp_path):
    i, b = setup(tmp_path)
    copies = [copy_message('msg/copy-%03d' % n) for n in range(bank.HOLD_MAX_LINES)]
    append(i['log'], *copies[:-1])
    b.commit(*collect_held(b))
    assert len(b.held) == bank.HOLD_MAX_LINES - 1
    append(i['log'], copies[-1])
    seen = []
    for _ in range(bank.HOLD_MAX_LINES // bank.MAX_EVENTS + 2):
        end, events = collect_held(b)
        assert 0 < len(events) <= bank.MAX_EVENTS
        seen += keys(events)
        b.commit(end, events)
        b = restart(i, b)
        if not b.held:
            break
        assert bank.private_json(b.path)['held_releasing'] is True
    assert seen == keys(copies) and b.held == [] and collect_held(b)[1] == []


def test_held_release_drops_exact_handled_and_duplicate_tokens(tmp_path):
    i, b = setup(tmp_path)
    receipt(i, [])
    append(i['log'], copy_message('msg/done'), copy_message('msg/done', version=2),
           copy_message('msg/keep'), copy_message('msg/keep'))
    b.commit(*collect_held(b))
    receipt(i, [handled('msg/done')])
    append(i['log'], message('msg/wake'), copy_message('msg/keep'))
    end, events = collect_held(restart(i, b))
    assert keys(events) == ['msg/done', 'msg/keep', 'msg/wake'] and events[0]['version'] == 2


def test_held_release_drops_already_delivered_checkpoint_token(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], copy_message('msg/delivered'), copy_message('msg/keep'))
    b.commit(*collect_held(b))
    saved = bank.private_json(b.path)
    saved['delivered'] = [bank.token(copy_message('msg/delivered'))]
    saved['held'].append(saved['held'][1])              # a duplicated held entry cannot repeat a dispatch either
    bank.atomic_json(b.path, saved)
    append(i['log'], message('msg/wake'))
    assert keys(collect_held(restart(i, b))[1]) == ['msg/keep', 'msg/wake']


@pytest.mark.parametrize('budget', ['events', 'bytes'])
def test_budget_limited_release_keeps_remainder_held(tmp_path, budget):
    i, b = setup(tmp_path)
    copies = [copy_message('msg/copy-%02d' % n) for n in range(15)]
    append(i['log'], *copies)
    b.commit(*collect_held(b))
    held_end = b.offset
    append(i['log'], message('msg/wake'))
    kw = {'render': lambda d: 'x' * 100, 'max_bytes': 250} if budget == 'bytes' else {}
    end, events = collect_held(b, **kw)
    limit = 2 if budget == 'bytes' else bank.MAX_EVENTS
    assert keys(events) == keys(copies[:limit]) and end == held_end
    b.commit(end, events)
    assert keys([h['routing'] for h in b.held]) == keys(copies[limit:])
    b = restart(i, b)
    remaining = []
    for _ in range(20):
        end, events = collect_held(b, **kw)
        if not events:
            break
        remaining += keys(events)
        b.commit(end, events)
        b = restart(i, b)
    assert remaining == keys(copies[limit:]) + ['msg/wake'] and b.held == []


def test_held_dispatch_records_role_ack_hold_and_receipt_time_on_restart(tmp_path):
    i, b = setup(tmp_path)
    rx = '2026-10-13T10:00:00.000Z'
    append(i['log'], copy_message(rx_at=rx))
    b.commit(*collect_held(b))
    held_at = b.held[0]['held_at']
    b = restart(i, b)
    append(i['log'], message('msg/wake', requires_ack=True))
    b.commit(*collect_held(b))
    [record] = dispatch_lines(b.offset_file)
    copy, wake = record['events']
    assert copy['role'] == 'cc' and copy['requires_ack'] is False and copy['held_at'] == held_at
    assert copy['rx_at'] == rx
    assert wake['role'] == 'to' and wake['requires_ack'] is True and 'held_at' not in wake


def test_no_hold_predicate_preserves_legacy_checkpoint(tmp_path, old_bank):
    i, b = setup(tmp_path)
    append(i['log'], copy_message())
    end, events = b.collect(postbox_os.wanted, hold=None)
    assert events == [bank.routing(copy_message())]
    b.commit(end, events)
    assert bank.private_json(b.path) == {'schema_version': 1, 'identity': i['name'], 'consumer': 'fixture',
                                       'offset': end, 'delivered': [bank.token(copy_message())]}
    before = Path(b.path).read_bytes()
    old = old_bank.Bank(i, b.offset_file, 0, b.consumer)
    old.commit(end, [])
    assert Path(b.path).read_bytes() == before


@pytest.mark.parametrize('role', ['to', 'cc'])
@pytest.mark.parametrize('ack', [{}, {'requires_ack': False}, {'requires_ack': True}])
@pytest.mark.parametrize('budget', ['default', 'exact'])
@pytest.mark.parametrize('v', [1, None])
def test_no_hold_notices_match_old_routing_bytes_and_budget(tmp_path, old_bank, role, ack, budget, v):
    i, b = setup(tmp_path)
    d = dict(message('msg/fixture', version=v, rx_at='2026-10-13T10:00:00.000Z',
                     body='private fixture', preview_unverified='private preview'), role=role, **ack)
    append(i['log'], d)
    expected = json.dumps(old_bank.routing(d)).encode('utf-8')
    kw = {'max_bytes': len(expected)} if budget == 'exact' else {}
    end, events = b.collect(wanted, hold=None, **kw)
    assert len(events) == 1 and json.dumps(events[0]).encode('utf-8') == expected
    assert end == Path(i['log']).stat().st_size
    b.commit(end, events)
    [dispatched] = dispatch_lines(b.offset_file)[0]['events']
    assert dispatched['role'] == role
    if v is not None:
        assert dispatched['rx_at'] == d['rx_at']
    if ack:
        assert dispatched['requires_ack'] is ack['requires_ack']
    else:
        assert 'requires_ack' not in dispatched


def test_disabling_hold_releases_previously_held_work(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], copy_message())
    b.commit(*collect_held(b))
    assert keys(restart(i, b).collect(postbox_os.wanted)[1]) == ['msg/copy']


def test_held_collection_and_failed_commit_can_be_retried(tmp_path, monkeypatch):
    i, b = setup(tmp_path)
    append(i['log'], copy_message())
    b.commit(*collect_held(b))
    before = Path(b.path).read_bytes()
    append(i['log'], message('msg/wake'))
    first = collect_held(b)
    end, events = collect_held(b)                      # Codex settles by collecting again before commit
    assert first == (end, events)
    def fail(*a):
        raise OSError('fixture disk failure')
    monkeypatch.setattr(bank, 'atomic_json', fail)
    with pytest.raises(OSError):
        b.commit(end, events)
    assert Path(b.path).read_bytes() == before and len(b.held) == 1 and not b.delivered
    assert collect_held(b) == (end, events) and collect_held(restart(i, b)) == (end, events)


def test_copies_met_in_pass_keep_their_spool_order(tmp_path):
    i, b = setup(tmp_path)
    append(i['log'], message('msg/wake-1'), copy_message('msg/copy-1'),
           message('msg/wake-2'), copy_message('msg/copy-2'))
    assert keys(collect_held(b)[1]) == ['msg/wake-1', 'msg/copy-1', 'msg/wake-2', 'msg/copy-2']


@pytest.mark.parametrize('order', ['W,C1,C2', 'C1,W,C2'])
@pytest.mark.parametrize('budget', ['bytes-one', 'bytes-two', 'events'])
def test_in_pass_spool_order_across_budget_commit_and_restart(tmp_path, old_bank, order, budget):
    i, b = setup(tmp_path)
    lines = {'W': message('msg/wake'), 'C1': copy_message('msg/copy-1'),
             'C2': copy_message('msg/copy-2')}
    rows = [lines[name] for name in order.split(',')]
    if budget == 'events':
        # C2 falls just outside the real twelve-event batch, after W and C1 in either order.
        rows[2:2] = [message('msg/fill-%02d' % n) for n in range(10)]
        kw, limit = {}, bank.MAX_EVENTS
    else:
        limit = 1 if budget == 'bytes-one' else 2
        kw = {'render': lambda d: 'x' * 100, 'max_bytes': limit * 100}
    append(i['log'], *rows)
    seen = []
    for n in range(len(rows) + 1):
        end, events = collect_held(b, **kw)
        if not events:
            break
        assert len(events) <= limit
        seen += keys(events)
        b.commit(end, events)
        if n == 0:
            saved = bank.private_json(b.path)
            assert saved['schema_version'] == 2 and saved['held_releasing'] is True
            if budget != 'bytes-one' or order == 'W,C1,C2':
                assert saved['held'] == []  # the remaining copies are still behind the spool cursor
            with pytest.raises(ValueError, match='identity/consumer mismatch'):
                old_bank.Bank(i, b.offset_file, 0, b.consumer)
        b = restart(i, b)
    assert seen == keys(rows)  # exact order and multiplicity, including every waking/filler line
    assert b.offset == Path(i['log']).stat().st_size and b.held == []
    assert len(b.delivered) == len(rows) and not b.held_releasing
    assert bank.private_json(b.path)['schema_version'] == 1
    assert collect_held(restart(i, b))[1] == []


def test_age_release_drains_young_remainder_after_oldest_was_dispatched(tmp_path, monkeypatch):
    i, b = setup(tmp_path)
    old, young = '2026-10-13T10:00:00.000Z', '2026-10-13T12:00:00.000Z'
    clock = bank._epoch(old)
    monkeypatch.setattr(bank.time, 'time', lambda: clock)
    monkeypatch.setattr(bank, 'utc_now', lambda: old)
    append(i['log'], copy_message('msg/old'))
    b.commit(*collect_held(b))
    clock = bank._epoch(young)
    monkeypatch.setattr(bank, 'utc_now', lambda: young)
    copies = [copy_message('msg/young-%02d' % n) for n in range(bank.MAX_EVENTS)]
    append(i['log'], *copies)
    b.commit(*collect_held(b))
    clock += 1
    end, events = collect_held(b)
    assert keys(events) == ['msg/old'] + keys(copies[:-1])
    b.commit(end, events)
    b = restart(i, b)
    assert keys(collect_held(b)[1]) == keys(copies[-1:])


@pytest.mark.parametrize('line', [{'event': 'status', 'state': 'error'},
                                dict(copy_message(), key=['unknown'], version=None)])
def test_general_hold_predicate_preserves_tokenless_lines_by_spool_position(tmp_path, line):
    i, b = setup(tmp_path)
    append(i['log'], line, line)
    hold = lambda d: d['event'] == line['event']
    end, events = b.collect(wanted, hold=hold)
    assert events == []
    b.commit(end, events)
    assert len({h['token'] for h in b.held}) == 2
    b = restart(i, b)
    append(i['log'], dict(message('msg/wake'), event='edit'))
    end, events = b.collect(wanted, hold=hold)
    assert events[:2] == [bank.routing(line)] * 2 and events[2]['key'] == 'msg/wake'
    b.commit(end, events)
    assert len(b.delivered) == 3 and b.held == []
    assert all('held_at' in d for d in dispatch_lines(b.offset_file)[0]['events'][:2])
    assert restart(i, b).collect(wanted, hold=hold)[1] == []


def test_truncated_spool_is_refused_even_with_a_budget_full_of_held_work(tmp_path, monkeypatch):
    i, b = setup(tmp_path)
    append(i['log'], *[copy_message('msg/%d' % n) for n in range(bank.MAX_EVENTS + 1)])
    b.commit(*collect_held(b))
    Path(i['log']).write_bytes(b'')
    monkeypatch.setattr(bank, 'HOLD_MAX_AGE_S', -1)
    with pytest.raises(ValueError, match='spool truncated'):
        collect_held(restart(i, b))


@pytest.mark.parametrize('schema', [1, 2])
@pytest.mark.parametrize('held_lines', [None, {}, ['bad'], [{}], [{'token': 1, 'routing': {}}],
                                      [{'token': 't', 'routing': []}],
                                      [{'token': 't', 'routing': {}, 'held_at': 'bad'}]])
def test_invalid_held_checkpoint_is_refused(tmp_path, held_lines, schema):
    i, b = setup(tmp_path)
    b.commit(0, [])
    saved = bank.private_json(b.path)
    saved['schema_version'] = schema
    saved['held'] = held_lines
    bank.atomic_json(b.path, saved)
    with pytest.raises(ValueError, match='invalid held'):
        restart(i, b)


def test_schema_two_requires_a_held_list(tmp_path):
    i, b = setup(tmp_path)
    b.commit(0, [])
    saved = bank.private_json(b.path)
    saved['schema_version'] = 2
    bank.atomic_json(b.path, saved)
    with pytest.raises(ValueError, match='invalid held'):
        restart(i, b)


@pytest.mark.parametrize('consumer', ['codex', 'channel'])
@pytest.mark.parametrize('setting', [None, False, True, 'true', 1])
def test_consumers_opt_into_hold_only_on_config_true(pb, tmp_path, monkeypatch, consumer, setting):
    i = ident(tmp_path, target='fixture-session', codex_settle_s=0)
    if setting is not None:
        i['hold_cc_without_ack'] = setting
    append(i['log'], copy_message())
    predicates, outputs = [], []
    real_collect = bank.Bank.collect
    def collect(self, *args, **kw):
        predicates.append(kw.get('hold'))
        return real_collect(self, *args, **kw)
    monkeypatch.setattr(bank.Bank, 'collect', collect)
    if consumer == 'channel':
        spec = importlib.util.spec_from_file_location('held_channel_under_test', PB / 'meshy_channel.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, 'push', lambda *a, **kw: outputs.append(a))
        offset = i['log'] + '.channel.offset'
        b = bank.Bank(i, offset, 0, 'claude-channel')
        run = lambda: module._deliver_once(i, b)
    else:
        module = pb
        offset = i['log'] + '.codex.offset'
        Path(offset).write_text('0')
        class Stop(Exception):
            pass
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 1:
                raise Stop()
        def queue(argv, **kw):
            outputs.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        monkeypatch.setattr(pb.time, 'sleep', sleep)
        monkeypatch.setattr(pb.subprocess, 'run', queue)
        def run():
            with pytest.raises(Stop):
                pb.run_codex_delivery(i)
    monkeypatch.setattr(module, 'graphline', None)
    monkeypatch.setattr(module, 'owedline', None)
    run()
    enabled = setting is True
    assert predicates == [postbox_os.held_by_default if enabled else None]
    if consumer == 'codex':
        # #199 follow-up: a malformed setting is noted once in postbox.err.log; true, false and absent say nothing
        err = Path(pb.ERR).read_text(encoding='utf-8') if Path(pb.ERR).exists() else ''
        malformed = not (setting is None or setting is True or setting is False)   # `True in (1,)` is true
        assert err.count('hold_cc_without_ack=') == (1 if malformed else 0)
    saved = bank.private_json(offset + '.bank.json')
    assert saved['offset'] == Path(i['log']).stat().st_size
    assert bool(saved.get('held')) is enabled
    assert len(outputs) == (0 if enabled else 1)
    if enabled:
        # #199: an expired copy since handled is dropped without a wake or cursor movement; that removal is durable.
        receipt(i, [handled('msg/copy')])
        monkeypatch.setattr(bank, 'HOLD_MAX_AGE_S', -1)
        if consumer == 'codex':
            sleeps.clear()
        run()
        assert 'held' not in bank.private_json(offset + '.bank.json') and outputs == []


# --- #154 (2026-10-10): a bad handled ENTRY is skipped and reported once; a bad FILE still stops delivery ---
# Mutants, each run red against the tests that name it:
#   B1 the raise is back: handled()'s skip branch raises 'handled receipt is not authenticated terminal work'
#   B2 delivery reads strictly again: Bank.collect calls handled(self.ident) without skip_bad
#   B3 no memory of what was reported: _report_skipped reports on every load
#   B4 the report prints the whole entry (json.dumps(item)) in place of its key
#   B5 the writer turns lenient: handled()'s skip_bad defaults to True
#   B6 the Codex loop drops report=note, so its line goes to stderr and not postbox.err.log
#   B7 a report that raises is not caught
#   B8 the identity check of the file is dropped
#   B9 the default report writes nothing
BODY = 'BODY-SENTINEL never printed'
BAD_ENTRIES = {
    'free-text disposition': (dict(handled('msg/bad'), disposition='responded_blocked', note=BODY), 'disposition'),
    'signature not verified': (dict(handled('msg/bad'), signature_verified=False, note=BODY), 'signature_verified'),
    'status not verified': (dict(handled('msg/bad'), status_verified='true', note=BODY), 'status_verified'),
    'version missing': ({k: v for k, v in dict(handled('msg/bad'), note=BODY).items() if k != 'version'}, 'version'),
    'version not whole': (dict(handled('msg/bad'), version=True, note=BODY), 'version'),
    'key not a string': (dict(handled(), key=['msg/bad', BODY]), 'key'),
    'not an object': (['msg/bad', BODY], 'is not an object'),
}


@pytest.fixture
def lines(monkeypatch):
    """A fresh once-per-process memory for each test, and the lines a Bank reports."""
    monkeypatch.setattr(bank, '_reported', set())
    return []


@pytest.mark.parametrize('name', sorted(BAD_ENTRIES))
def test_a_bad_handled_entry_is_skipped_and_the_rest_still_counts(tmp_path, lines, name):
    """MUTANTS B1, B2, B4 (#154). One bad entry among good ones: the good ones still suppress their messages, the bad
    one's message is delivered once (the delivered list keeps it to once), and one line names its position, key and
    version and the failed check, never anything else the entry holds."""
    entry, fault = BAD_ENTRIES[name]
    i, _ = setup(tmp_path)
    b = bank.Bank(i, i['log'] + '.test.offset', 0, 'fixture', report=lines.append)
    receipt(i, [handled('msg/good-1'), entry, handled('msg/good-2')])
    append(i['log'], message('msg/good-1'), message('msg/bad'), message('msg/good-2'), message('msg/new'))
    end, events = b.collect(wanted)
    assert [d['key'] for d in events] == ['msg/bad', 'msg/new']
    b.commit(end, events)
    append(i['log'], message('msg/bad'))                  # the same version again: delivered once, not twice
    assert b.collect(wanted)[1] == []
    assert len(lines) == 1, lines
    line = lines[0]
    assert 'handled entry 1 ' in line and fault in line and i['handled_file'] in line
    assert BODY not in line and 'responded_blocked' not in line
    if isinstance(entry, dict) and isinstance(entry.get('key'), str):
        assert 'key "msg/bad"' in line
    else:
        assert 'key (none)' in line
    assert ('version 1' in line) == (fault not in ('version', 'is not an object'))


def test_a_skipped_entry_is_reported_once_however_often_the_file_is_reloaded(tmp_path, lines):
    """MUTANT B3. Delivery rereads the handled file every pass (the channel each second); a line per pass would fill
    the log. Each bad entry is reported once, and an entry that turns bad later gets its own one line."""
    i, _ = setup(tmp_path)
    b = bank.Bank(i, i['log'] + '.test.offset', 0, 'fixture', report=lines.append)
    receipt(i, [dict(handled('msg/bad-1'), disposition='superseded_no_build_action'), handled('msg/good')])
    for _ in range(3):
        b.collect(wanted)
    assert len(lines) == 1 and '"msg/bad-1"' in lines[0]
    receipt(i, [handled('msg/good'), dict(handled('msg/bad-1'), disposition='superseded_no_build_action'),
                dict(handled('msg/bad-2'), status_verified=False)])
    for _ in range(3):
        b.collect(wanted)
    assert len(lines) == 2 and '"msg/bad-2"' in lines[1]


@pytest.mark.parametrize('changes', [{'identity': 'another-seat'}, {'schema_version': 2}, {'handled': {'msg/x': 1}}])
def test_a_wrong_handled_file_still_stops_delivery_without_progress(tmp_path, lines, changes):
    """MUTANT B8. The whole-file refusals stand: nothing in a file of another identity or schema can be trusted."""
    i, b = setup(tmp_path)
    receipt(i, [handled()])
    data = dict(bank.private_json(i['handled_file']), **changes)
    bank.atomic_json(i['handled_file'], data)
    append(i['log'], message())
    with pytest.raises(ValueError):
        b.collect(wanted)
    assert b.offset == 0 and lines == []


@pytest.mark.parametrize('changes', [{'disposition': 'wait'}, {'signature_verified': False}, {'status_verified': False}])
def test_the_writer_still_refuses_a_file_holding_a_bad_entry(tmp_path, changes):
    """MUTANT B5. mark_handled.py is the seat's writer and is unchanged (#154 leaves it to its owner): it reads the
    file strictly and adds nothing to one holding a bad entry. Only delivery skips."""
    i, _ = setup(tmp_path)
    receipt(i, [dict(handled(), **changes)])
    before = Path(i['handled_file']).read_bytes()
    with pytest.raises(ValueError):
        bank.handled(i)
    with pytest.raises(ValueError):
        mark_handled.record(i, [dict(key='msg/other', version=1, signature_verified=True, status_verified=True)], 'done')
    assert Path(i['handled_file']).read_bytes() == before


def test_a_report_that_fails_never_stops_delivery(tmp_path, lines):
    """MUTANT B7. The report is bookkeeping; it must not become the outage it replaces."""
    def broken(_line):
        raise OSError('fixture: no space left on device')
    i, _ = setup(tmp_path)
    b = bank.Bank(i, i['log'] + '.test.offset', 0, 'fixture', report=broken)
    receipt(i, [dict(handled('msg/bad'), disposition='responded_blocked')])
    append(i['log'], message('msg/bad'))
    assert [d['key'] for d in b.collect(wanted)[1]] == ['msg/bad']


def test_the_channel_and_the_waiter_report_on_stderr_and_survive_without_one(tmp_path, lines, capsys, monkeypatch):
    """MUTANT B9. A Bank given no report (the channel, the waiter) writes the line to stderr; under pythonw there is
    no stderr at all, and delivery still goes on."""
    i, b = setup(tmp_path)
    receipt(i, [dict(handled('msg/bad'), disposition='responded_blocked')])
    append(i['log'], message('msg/bad'))
    assert len(b.collect(wanted)[1]) == 1
    err = capsys.readouterr().err
    assert err.count('\n') == 1 and 'key "msg/bad"' in err
    receipt(i, [dict(handled('msg/bad'), disposition='responded_blocked'), dict(handled('msg/bad-2'), status_verified=False)])
    with monkeypatch.context() as m:                     # undone here, before capsys puts its own stream back
        m.setattr(sys, 'stderr', None)
        assert len(bank.Bank(i, i['log'] + '.other.offset', 0, 'fixture').collect(wanted)[1]) == 1


def test_codex_loop_delivers_past_a_bad_entry_and_notes_it_in_its_error_log(pb, tmp_path, lines, monkeypatch):
    """MUTANTS B1, B6. The #154 seat: run_codex_delivery with a free-text disposition in its handled file queues the
    message that entry named, keeps suppressing the good entry's, and notes the skipped entry in postbox.err.log,
    where its 398 crashes were."""
    i = ident(tmp_path, target='fixture-session')
    Path(i['log']).touch()
    offset = Path(i['log'] + '.codex.offset')
    offset.write_text('0')
    receipt(i, [handled('msg/handled'), dict(handled('msg/blocked'), disposition='responded_blocked', note=BODY)])
    append(i['log'], message('msg/handled'), message('msg/blocked'))
    calls = []

    class Stop(Exception):
        pass

    def sleep(_seconds):
        if calls:
            raise Stop()

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', run)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert len(calls) == 1
    notice = calls[0][-1]
    assert 'msg/blocked' in notice and 'msg/handled' not in notice
    assert bank.private_json(str(offset) + '.bank.json')['offset'] == Path(i['log']).stat().st_size
    noted = [ln for ln in Path(pb.ERR).read_text(encoding='utf-8').splitlines() if 'handled entry' in ln]
    assert len(noted) == 1 and 'key "msg/blocked"' in noted[0] and 'disposition' in noted[0]
    assert BODY not in noted[0] and 'responded_blocked' not in noted[0]


def test_handled_file_is_refreshed_between_batches(tmp_path):
    i, b = setup(tmp_path)
    receipt(i, [])
    append(i['log'], message())
    assert len(b.collect(wanted)[1]) == 1
    receipt(i, [handled()])
    assert b.collect(wanted)[1] == []


def test_checkpoint_target_mismatch_refused(tmp_path):
    i, b = setup(tmp_path)
    b.commit(0, [])
    with pytest.raises(ValueError):
        bank.Bank(i, b.offset_file, 0, 'different-session')


@pytest.mark.skipif(os.name != 'posix', reason='POSIX receipt modes')
def test_receipts_are_owner_only_and_shared_receipts_are_refused(tmp_path):
    i, b = setup(tmp_path)
    receipt(i, [handled()])
    assert Path(i['handled_file']).stat().st_mode & 0o777 == 0o600
    Path(i['handled_file']).chmod(0o644)
    with pytest.raises(ValueError):
        b.collect(wanted)


def test_symlink_receipt_refused(tmp_path):
    i, b = setup(tmp_path)
    target = tmp_path / 'target.json'
    bank.atomic_json(str(target), {})
    link = tmp_path / 'linked.json'
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('this account cannot create a symlink without elevation')
    i['handled_file'] = str(link)
    i['handled_private_acl_verified'] = True
    with pytest.raises((ValueError, OSError)):
        b.collect(wanted)


def test_codex_loop_queues_burst_once_and_failure_consumes_nothing(pb, tmp_path, monkeypatch):
    i = ident(tmp_path, target='fixture-session')
    append(i['log'], *[message('msg/part-%s' % n) for n in range(7)])
    Path(i['log'] + '.codex.offset').write_text('0')
    calls = []
    class Stop(Exception):
        pass
    def sleep(_seconds):
        if calls:
            raise Stop()
    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout='', stderr='fixture failure')
    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', run)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert len(calls) == 1 and all('msg/part-%s' % n in calls[0][-1] for n in range(7))
    assert Path(i['log'] + '.codex.offset').read_text() == '0'
    calls.clear()
    monkeypatch.setattr(pb.subprocess, 'run', lambda argv, **kw: (calls.append(argv) or subprocess.CompletedProcess(argv, 0, stdout='', stderr='')))
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert len(calls) == 1 and int(Path(i['log'] + '.codex.offset').read_text()) == Path(i['log']).stat().st_size


def test_waiter_skips_handled_parts_but_delivers_new_version(wn):
    run, log, offset = wn
    offset.write_text('0')
    config = log.parent / 'config.json'
    data = json.loads(config.read_text())
    i = data['identities'][0]
    receipt(i, [handled()])
    config.write_text(json.dumps(data))
    append(log, message(), message(version=2))
    events = run('--consumer', 'cron', '--once')
    assert [d['version'] for d in events if d.get('event') == 'message'] == [2]


def test_listener_spool_dedup_does_not_drop_a_new_version(pb, tmp_path):
    i = ident(tmp_path)
    with open(i['log'], 'ab') as out:
        for d in [message(), message(), message(version=2)]:
            pb.record_listener_line(i, out, json.dumps(d).encode() + b'\n')
    lines = [json.loads(line) for line in Path(i['log']).read_bytes().splitlines()]
    assert [d['version'] for d in lines] == [1, 2]


def test_reconcile_carries_new_version_into_dispatch(pb, tmp_path, monkeypatch):
    i = ident(tmp_path)
    append(i['log'], message())
    reply = {'label': 'seat', 'hits': [{'key': 'msg/fixture', 'version': 2, 'author': 'peer',
                                      'recorded_at': '2026-10-02T00:00:00Z'}]}
    monkeypatch.setattr(pb.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(reply), stderr=''))
    assert pb.reconcile(i, 'http://fixture') == 1
    assert json.loads(Path(i['log']).read_bytes().splitlines()[-1])['version'] == 2


def test_handler_records_many_authenticated_parts_once(tmp_path):
    i, b = setup(tmp_path)
    receipt(i, [])
    reads = [dict(key='msg/part%s' % n, version=1, signature_verified=True, status_verified=True,
                  body='not saved in terminal receipt') for n in range(7)]
    assert mark_handled.record(i, reads, 'done') == 7
    assert mark_handled.record(i, reads, 'done') == 0
    assert len(bank.handled(i)) == 7
    assert 'not saved' not in Path(i['handled_file']).read_text()
    append(i['log'], *[message(r['key']) for r in reads])
    assert b.collect(wanted)[1] == []


def test_codex_late_notices_after_inbox_handling_preserve_new_work(pb, tmp_path, monkeypatch):
    """A signed inbox read may finish before the corresponding wake is consumed."""
    i = ident(tmp_path, target='fixture-session')
    Path(i['log']).touch()
    offset = Path(i['log'] + '.codex.offset')
    offset.write_text('0')
    receipt(i, [])
    reads = [dict(key='msg/previously-handled-%s' % n, version=1,
                  signature_verified=True, status_verified=True) for n in range(3)]
    mark_handled.record(i, reads, 'done')
    append(i['log'], *[message(r['key']) for r in reads],
           message('msg/unresolved-request'),
           dict(message(reads[0]['key'], version=2), event='edit', edited_by='fixture-peer'))
    calls = []

    class Stop(Exception):
        pass

    def sleep(_seconds):
        if calls:
            raise Stop()

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', run)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert len(calls) == 1
    notice = calls[0][-1]
    assert 'msg/unresolved-request' in notice and 'version=2' in notice
    assert reads[1]['key'] not in notice and reads[2]['key'] not in notice
    assert bank.private_json(str(offset) + '.bank.json')['offset'] == Path(i['log']).stat().st_size


def test_codex_only_late_handled_notices_advance_without_a_wake(pb, tmp_path, monkeypatch):
    i = ident(tmp_path, target='fixture-session')
    Path(i['log']).touch()
    offset = Path(i['log'] + '.codex.offset')
    offset.write_text('0')
    receipt(i, [])
    mark_handled.record(i, [dict(key='msg/already-complete', version=1,
                               signature_verified=True, status_verified=True)], 'no_action')
    append(i['log'], message('msg/already-complete'))
    sleeps = []

    class Stop(Exception):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Stop()

    def unexpected_queue(*args, **kwargs):
        pytest.fail('authenticated terminal work must not start another Codex turn')

    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', unexpected_queue)
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert int(offset.read_text()) == Path(i['log']).stat().st_size


def test_unverified_read_does_not_partially_commit(tmp_path):
    i, b = setup(tmp_path)
    receipt(i, [])
    before = Path(i['handled_file']).read_bytes()
    reads = [dict(key='msg/ok', version=1, signature_verified=True, status_verified=True),
             dict(key='msg/bad', version=1, signature_verified=False, status_verified=True)]
    with pytest.raises(ValueError):
        mark_handled.record(i, reads, 'done')
    assert Path(i['handled_file']).read_bytes() == before


@pytest.mark.parametrize('fail_output', [False, True])
def test_claude_push_is_one_burst_and_pipe_failure_preserves_cursor(tmp_path, monkeypatch, fail_output):
    spec = importlib.util.spec_from_file_location('banked_channel_under_test', PB / 'meshy_channel.py')
    channel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(channel)
    i = ident(tmp_path)
    append(i['log'], *[message('msg/part%s' % n) for n in range(7)])
    offset = i['log'] + '.channel.offset'
    Path(offset).write_text('0')
    calls = []
    class Stop(Exception):
        pass
    def push(content, **meta):
        calls.append((content, meta))
        if fail_output and meta.get('kind') == 'batch':
            raise BrokenPipeError('fixture pipe failure')
    def sleep(_seconds):
        raise Stop()
    monkeypatch.setattr(channel, 'push', push)
    monkeypatch.setattr(channel.time, 'sleep', sleep)
    # A failed pass no longer ends the thread (#261): it is reported once and retried after a wait, which this
    # fixture's sleep turns into Stop. The cursor still stays where it was, so the batch is delivered again.
    with pytest.raises(Stop):
        channel.tail(i)
    assert calls[1][1]['kind'] == 'batch' and calls[1][1]['count'] == 7
    # SD 255 (c), #546: a batch of several opens with its first message's brief line, key first, as meshy-mod's
    # BRIEF_LINE reads it; the read template follows once (pxx ac2f5d: this assertion still expected the old opening).
    assert calls[1][0].splitlines()[0] == '[STEERING] msg/part0 from unknown (to)'
    if fail_output:
        assert len(calls) == 3 and calls[2][1]['kind'] == 'channel_error' and 'BrokenPipeError' in calls[2][0]
    else:
        assert len(calls) == 2
    assert int(Path(offset).read_text()) == (0 if fail_output else Path(i['log']).stat().st_size)


def test_quiet_channel_saves_initial_position_before_open_push(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('quiet_channel_under_test', PB / 'meshy_channel.py')
    channel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(channel)
    i = ident(tmp_path)
    append(i['log'], message('msg/old'))
    size = Path(i['log']).stat().st_size
    class Stop(BaseException):
        pass
    def stop_at_open(content, **meta):
        assert int(Path(i['log'] + '.channel.offset').read_text()) == size
        assert bank.private_json(i['log'] + '.channel.offset.bank.json')['offset'] == size
        raise Stop()
    monkeypatch.setattr(channel, 'push', stop_at_open)
    with pytest.raises(Stop):
        channel.tail(i)


def dispatch_lines(offset_file):
    path = Path(offset_file + '.dispatch.jsonl')
    return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def test_dispatch_log_times_each_committed_batch(tmp_path):
    """#58: the checkpoint says WHICH tokens went out; the dispatch line says WHEN, beside each spool receipt."""
    i, b = setup(tmp_path)
    append(i['log'],
           message('msg/a', rx_at='2026-10-04T14:45:01.000Z', created_at='2026-10-04T14:45:00Z',
                   preview_unverified='fixture body text'),
           message('msg/b', version=2, rx_at='2026-10-04T14:46:01.000Z'),
           dict(event='status', state='listening'))
    end, events = b.collect(wanted)
    assert dispatch_lines(b.offset_file) == []           # collecting is not dispatching
    b.commit(end, events)
    [line] = dispatch_lines(b.offset_file)
    assert (line['identity'], line['consumer'], line['from'], line['to']) == (i['name'], 'fixture', 0, end)
    assert line['events'] == [
        {'event': 'message', 'key': 'msg/a', 'version': 1, 'created_at': '2026-10-04T14:45:00Z',
         'rx_at': '2026-10-04T14:45:01.000Z', 'role': 'to'},
        {'event': 'message', 'key': 'msg/b', 'version': 2, 'rx_at': '2026-10-04T14:46:01.000Z', 'role': 'to'},
        {'event': 'status', 'state': 'listening'}]
    assert re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', line['at'])
    assert 'fixture body text' not in Path(b.offset_file + '.dispatch.jsonl').read_text(encoding='utf-8')
    b.commit(end, [])                                    # an offset-only commit dispatched nothing
    assert len(dispatch_lines(b.offset_file)) == 1
    if os.name == 'posix':
        assert Path(b.offset_file + '.dispatch.jsonl').stat().st_mode & 0o777 == 0o600


def test_dispatch_log_failure_never_blocks_or_undoes_a_commit(tmp_path):
    i, b = setup(tmp_path)
    os.mkdir(b.offset_file + '.dispatch.jsonl')          # a directory where the log goes: every append fails
    append(i['log'], message('msg/a'))
    end, events = b.collect(wanted)
    b.commit(end, events)
    fresh = bank.Bank(i, b.offset_file, 0, 'fixture')
    assert b.offset == fresh.offset == end and fresh.collect(wanted) == (end, [])


def test_dispatch_log_rotates_into_sixteen_non_overwriting_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(bank, 'DISPATCH_LOG_MAX', 10)
    i, b = setup(tmp_path)
    for n in range(18):
        append(i['log'], message('msg/r%d' % n))
        end, events = b.collect(wanted)
        b.commit(end, events)
    for slot in range(1, 17):
        rows = [json.loads(x) for x in Path('%s.dispatch.jsonl.%d' % (b.offset_file, slot)).read_text().splitlines()]
        assert [[e['key'] for e in x['events']] for x in rows] == [['msg/r%d' % (slot - 1)]]
    # Slot 17 (n == 16) filled the active file; n == 17 hit archive exhaustion without overwriting .1..16 or blocking commit.
    assert [[e['key'] for e in x['events']] for x in dispatch_lines(b.offset_file)] == [['msg/r16']]
    assert b.offset == end and b.collect(wanted) == (end, [])


def test_codex_dispatch_line_only_once_the_queue_accepts(pb, tmp_path, monkeypatch):
    i = ident(tmp_path, target='fixture-session')
    append(i['log'], message('msg/q', rx_at='2026-10-06T10:00:00.000Z'))
    off_file = i['log'] + '.codex.offset'
    Path(off_file).write_text('0')
    calls = []
    class Stop(Exception):
        pass
    def sleep(_seconds):
        if calls:
            raise Stop()
    def queue(rc):
        return lambda argv, **kw: (calls.append(argv) or subprocess.CompletedProcess(argv, rc, stdout='', stderr=''))
    monkeypatch.setattr(pb.time, 'sleep', sleep)
    monkeypatch.setattr(pb.subprocess, 'run', queue(1))
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    assert len(calls) == 1 and dispatch_lines(off_file) == []    # a refused queue item was not dispatched
    calls.clear()
    monkeypatch.setattr(pb.subprocess, 'run', queue(0))
    with pytest.raises(Stop):
        pb.run_codex_delivery(i)
    [line] = dispatch_lines(off_file)
    assert line['consumer'] == 'codex:fixture-session'
    assert line['events'] == [{'event': 'message', 'key': 'msg/q', 'version': 1, 'role': 'to',
                               'rx_at': '2026-10-06T10:00:00.000Z'}]


def test_handled_at_keeps_the_first_time_and_renews_on_a_new_decision(tmp_path, monkeypatch):
    i, _ = setup(tmp_path)
    receipt(i, [handled('msg/old')])                      # written before handled_at existed
    read = dict(key='msg/fixture', version=1, signature_verified=True, status_verified=True)
    times = iter(['2026-10-06T10:00:00.000Z', '2026-10-06T11:00:00.000Z', '2026-10-06T12:00:00.000Z'])
    monkeypatch.setattr(bank, 'utc_now', lambda: next(times))
    def entries():
        return {(e['key'], e['version']): e for e in bank.private_json(i['handled_file'])['handled']}
    assert mark_handled.record(i, [read], 'no_action') == 1
    assert entries()[('msg/fixture', 1)]['handled_at'] == '2026-10-06T10:00:00.000Z'
    mark_handled.record(i, [read], 'no_action')           # the same decision again keeps its first time
    assert entries()[('msg/fixture', 1)]['handled_at'] == '2026-10-06T10:00:00.000Z'
    mark_handled.record(i, [read], 'done')                # a new decision gets its own
    assert entries()[('msg/fixture', 1)]['handled_at'] == '2026-10-06T12:00:00.000Z'
    assert 'handled_at' not in entries()[('msg/old', 1)]
    assert bank.handled(i) == {('msg/fixture', 1), ('msg/old', 1)}


def test_an_oversize_spool_line_is_skipped_reported_once_and_the_rest_is_delivered(tmp_path, monkeypatch, lines):
    """#254, MUTANT S1 (raise again on an over-size line). One over-size line between two good messages: collect
    delivers both good ones, moves the cursor past the bad one, and reports it once however often it rereads.
    Before, it raised 'spool line size limit' with the cursor on that line, so every later pass raised again and the
    identity got no delivery at all. CONTROL: a line of exactly MAX_LINE bytes is a line, and is delivered."""
    monkeypatch.setattr(bank, 'MAX_LINE', 300)
    i = ident(tmp_path)
    Path(i['log']).touch()
    b = bank.Bank(i, i['log'] + '.test.offset', 0, 'fixture', report=lines.append)
    append(i['log'], message('msg/before'))
    with open(i['log'], 'ab') as f:
        f.write(json.dumps(message('msg/huge', pad='x' * 600)).encode() + b'\n')
    append(i['log'], message('msg/after'))
    end, events = b.collect(wanted)
    assert [e['key'] for e in events] == ['msg/before', 'msg/after']
    assert end == os.path.getsize(i['log'])
    assert len(lines) == 1 and 'over the 300-byte limit' in lines[0] and 'msg/huge' not in lines[0], lines
    bank.Bank(i, i['log'] + '.other.offset', 0, 'fixture', report=lines.append).collect(wanted)
    assert len(lines) == 1, lines                                  # once per process, not once per pass
    exact = message('msg/exact')
    body = json.dumps(exact)
    exact['pad'] = 'y' * (300 - len(body) - len(', "pad": ""') - 1)
    raw = json.dumps(exact).encode() + b'\n'
    assert len(raw) == 300
    with open(i['log'], 'ab') as f:
        f.write(raw)
    fresh = bank.Bank(i, i['log'] + '.third.offset', end, 'fixture', report=lines.append)
    assert [e['key'] for e in fresh.collect(wanted)[1]] == ['msg/exact']    # control: MAX_LINE bytes is a line


def test_an_oversize_line_still_being_written_is_left_for_the_next_pass(tmp_path, monkeypatch, lines):
    """#254, MUTANT S2 (advance past a line with no newline yet). An over-size line with no newline yet may still be
    being written: the cursor stays before it, as replay_start's rule has it, and nothing is reported until it ends."""
    monkeypatch.setattr(bank, 'MAX_LINE', 300)
    i = ident(tmp_path)
    Path(i['log']).touch()
    b = bank.Bank(i, i['log'] + '.test.offset', 0, 'fixture', report=lines.append)
    append(i['log'], message('msg/before'))
    edge = os.path.getsize(i['log'])
    with open(i['log'], 'ab') as f:
        f.write(b'{"event": "message", "key": "msg/partial", "pad": "' + b'z' * 600)
    end, events = b.collect(wanted)
    assert [e['key'] for e in events] == ['msg/before'] and end == edge and lines == []


def test_a_different_oversize_line_at_the_same_offset_is_reported_again(tmp_path, monkeypatch, lines):
    """MUTANT S3 (the size dropped from the once-per-process key). After the spool is rewritten, a different over-size
    line can sit at the offset an earlier one held. It is a new line, so it is reported. The same line reread is not."""
    monkeypatch.setattr(bank, 'MAX_LINE', 300)
    i = ident(tmp_path)
    Path(i['log']).touch()
    with open(i['log'], 'wb') as f:
        f.write(json.dumps(message('msg/big', pad='x' * 600)).encode() + b'\n')
    bank.Bank(i, i['log'] + '.a.offset', 0, 'fixture', report=lines.append).collect(wanted)
    bank.Bank(i, i['log'] + '.b.offset', 0, 'fixture', report=lines.append).collect(wanted)
    assert len(lines) == 1, lines                                  # the same line, reread: one report
    with open(i['log'], 'wb') as f:                                # rewritten: another size at the same offset
        f.write(json.dumps(message('msg/bigger', pad='y' * 900)).encode() + b'\n')
    bank.Bank(i, i['log'] + '.c.offset', 0, 'fixture', report=lines.append).collect(wanted)
    assert len(lines) == 2 and 'byte 0' in lines[1], lines


def test_an_oversize_listener_message_is_written_short_and_still_delivered(pb, tmp_path, monkeypatch, lines):
    """#311 point 2, MUTANT W1 (the bound removed: written verbatim). A listener line over MAX_LINE used to be written
    as is: its key claimed, and the line then skipped by delivery (#254), so the message was never delivered. It is
    now written as a pointer line under the limit (key, version, from, thread, and `oversize_bytes`), which delivery
    wakes the seat with, and the seat reads the message by key. CONTROL: a line of exactly MAX_LINE bytes is written
    whole."""
    monkeypatch.setattr(bank, 'MAX_LINE', 400)
    i = ident(tmp_path)
    huge = message('msg/huge', **{'from': 'peer', 'thread_id': 't1', 'preview_unverified': 'x' * 900})
    with open(i['log'], 'ab') as out:
        pb.record_listener_line(i, out, json.dumps(huge).encode() + b'\n')
    raw = Path(i['log']).read_bytes()
    assert len(raw) <= 400, len(raw)
    line = json.loads(raw)
    assert line['key'] == 'msg/huge' and line['version'] == 1 and line['from'] == 'peer' and line['thread_id'] == 't1'
    assert line['oversize_bytes'] > 900 and 'preview_unverified' not in line and line.get('rx_at')
    b = bank.Bank(i, i['log'] + '.test.offset', 0, 'fixture', report=lines.append)
    assert [e['key'] for e in b.collect(wanted)[1]] == ['msg/huge'] and lines == []
    assert 'over the 400-byte spool limit' in Path(pb.ERR).read_text(encoding='utf-8')

    exact = message('msg/exact', preview_unverified='')
    exact['rx_at'] = pb.now()                 # record_listener_line stamps rx_at; measure the line it will write
    pad = 400 - len(json.dumps(exact).encode()) - 1
    exact['preview_unverified'] = 'y' * pad
    i2 = ident(tmp_path, log=str(tmp_path / 'exact.log'))
    monkeypatch.setattr(pb, 'now', lambda: exact['rx_at'])
    with open(i2['log'], 'ab') as out:
        pb.record_listener_line(i2, out, json.dumps({k: v for k, v in exact.items() if k != 'rx_at'}).encode() + b'\n')
    whole = Path(i2['log']).read_bytes()
    assert len(whole) == 400 and json.loads(whole)['preview_unverified'] == 'y' * pad   # control: whole at the limit


def test_an_oversize_listener_line_of_another_event_becomes_a_short_record(pb, tmp_path, monkeypatch):
    """#311 point 2: a status (or any non-message) line over MAX_LINE is not written verbatim either; an
    `oversize_line` record names its event and size, so the spool never holds a line delivery must skip."""
    monkeypatch.setattr(bank, 'MAX_LINE', 300)
    i = ident(tmp_path)
    with open(i['log'], 'ab') as out:
        pb.record_listener_line(i, out, json.dumps({'event': 'status', 'detail': 'z' * 700}).encode() + b'\n')
        pb.record_listener_line(i, out, b'not json at all ' + b'q' * 700 + b'\n')
    rows = [json.loads(r) for r in Path(i['log']).read_bytes().splitlines()]
    assert [(r['event'], r['of'], r['bytes'] > 700) for r in rows] == [('oversize_line', 'status', True),
                                                                        ('oversize_line', None, True)]


def test_a_delivery_skip_is_counted_in_status_for_a_monitor(pb, tmp_path, monkeypatch):
    """#311 point 1, MUTANT R1 (the report only notes). An over-size spool line delivery skips, and a bad handled
    entry it skips, used to be visible only in postbox.err.log. The codex delivery's report now also counts them
    in status.json, where a monitor reads a seat's state."""
    monkeypatch.setattr(bank, 'MAX_LINE', 300)
    i = ident(tmp_path)
    Path(i['log']).touch()
    with open(i['log'], 'ab') as f:
        f.write(json.dumps(message('msg/big', pad='x' * 600)).encode() + b'\n')
    append(i['log'], message('msg/after'))
    b = bank.Bank(i, i['log'] + '.test.offset', 0, 'fixture', report=pb.delivery_skip_report(i))
    assert [e['key'] for e in b.collect(wanted)[1]] == ['msg/after']
    entry = json.loads(Path(pb.STATUS).read_text(encoding='utf-8'))[i['name']]
    assert entry['delivery_skips'] == 1 and 'over the 300-byte limit' in entry['last_delivery_skip'], entry
    assert entry['last_delivery_skip_at']
    assert 'over the 300-byte limit' in Path(pb.ERR).read_text(encoding='utf-8')     # still noted in the log
    assert 'report=delivery_skip_report(ident)' in Path(pb.__file__).read_text(encoding='utf-8')  # the codex Bank uses it


def test_an_oversize_edit_line_keeps_its_key_so_a_restart_still_sees_its_version(pb, tmp_path, monkeypatch):
    """#311 with #312: an edit line is claimed by its key and version, and `seen_keys` rebuilds that claim from the log
    after a restart. An over-size edit written as a bare `oversize_line` record would drop its key, so reconcile would
    re-emit the edit as a message after the next start. It is written short with its key and version instead."""
    monkeypatch.setattr(bank, 'MAX_LINE', 300)
    i = ident(tmp_path)
    edit = {'event': 'edit', 'key': 'msg/edited', 'version': 2, 'from': 'peer', 'preview_unverified': 'e' * 700}
    with open(i['log'], 'ab') as out:
        pb.record_listener_line(i, out, json.dumps(edit).encode() + b'\n')
    raw = Path(i['log']).read_bytes()
    assert len(raw) <= 300, len(raw)
    line = json.loads(raw)
    assert (line['event'], line['key'], line['version'], line['oversize_bytes'] > 700) == ('edit', 'msg/edited', 2, True)
    assert 'preview_unverified' not in line
    pb._seen.pop(i['name'], None)                       # a restart: the claim is rebuilt from the log
    assert ('msg/edited', 2) in pb.seen_keys(i)


def _short_of(pb, tmp_path, monkeypatch, d, limit, name):
    monkeypatch.setattr(bank, 'MAX_LINE', limit)
    i = ident(tmp_path, log=str(tmp_path / (name + '.log')))
    with open(i['log'], 'ab') as out:
        pb.record_listener_line(i, out, json.dumps(d).encode() + b'\n')
    raw = Path(i['log']).read_bytes()
    assert len(raw) <= limit, len(raw)
    return i, json.loads(raw)


@pytest.mark.parametrize('event,role,requires_ack', [('message', 'cc', False), ('message', 'to', True),
                                                     ('edit', 'to', None)])
def test_an_oversize_line_answers_every_consumer_as_the_whole_line_does(pb, tmp_path, monkeypatch, event, role,
                                                                        requires_ack):
    """#317 review (pxx; GitHub Codex at :233 and :251). The short form of an over-size message or edit keeps every
    field delivery_bank.routing lists, and requires_ack, so wanted, held_by_default, sender_text and the channel's edit
    notice give the same answers for it as for the whole line, in both short forms: with its address, and with a to/cc
    too long to keep. MUTANT K1 (the old fixed field list) fails: from_peer_id, edited_by, edited_at, requires_ack and,
    in the fallback, role are lost."""
    import meshy_channel
    whole = {'event': event, 'type': 'request', 'key': 'msg/k', 'version': 2, 'role': role, 'from': 'oxxxxxx',
             'from_peer_id': '610170d5-0000-4000-8000-000000000000', 'created_at': '2026-10-15T05:00:00Z',
             'edited_by': 'pxx', 'edited_at': '2026-10-15T05:01:00Z', 'thread_id': 't1', 'via': 'live',
             'preview_unverified': 'p' * 900}
    if requires_ack is not None:
        whole['requires_ack'] = requires_ack
    for name, to, limit in (('addressed', ['kxxx'], 800), ('vast-to', ['seat%03d' % n for n in range(120)], 800)):
        d = dict(whole, key='msg/k-' + name, to=to, cc=['nxxxxx'])
        i, short = _short_of(pb, tmp_path, monkeypatch, d, limit, name + event + role)
        assert 'preview_unverified' not in short and short['oversize_bytes'] > 900, short
        for k in list(bank.routing(d)) + (['requires_ack'] if requires_ack is not None else []):
            assert short.get(k) == d[k], (name, k, short)
        assert (name == 'addressed') == ('to' in short), (name, short)
        assert postbox_os.wanted(short) == postbox_os.wanted(d) is True
        assert postbox_os.held_by_default(short) == postbox_os.held_by_default(d)
        assert postbox_os.sender_text(short) == postbox_os.sender_text(d) == 'oxxxxxx (peer 610170d5)'
        if event == 'edit':
            assert meshy_channel.edit_event(i, short) == meshy_channel.edit_event(i, d)


def test_delivery_skips_count_across_a_restart_of_the_delivery_loop(pb, tmp_path, monkeypatch):
    """#317 review (GitHub Codex at :266, pxx): guarded() restarts run_codex_delivery after a crash, which builds a new
    report. The count is per identity since the postbox started, so it carries on rather than going back to 1.
    MUTANT K2 (a counter per report) fails."""
    i = ident(tmp_path)
    first, second = pb.delivery_skip_report(i), pb.delivery_skip_report(i)
    first('a skipped line')
    second('another skipped line')
    entry = json.loads(Path(pb.STATUS).read_text(encoding='utf-8'))[i['name']]
    assert entry['delivery_skips'] == 2 and entry['last_delivery_skip'] == 'another skipped line', entry
