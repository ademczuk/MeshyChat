"""Delivery latency report: where each message spent its time, from signing to handled (#58, PR 1).

    python latency.py --identity seat-prod [--since 2026-10-20T00:00:00Z] [--json]
    python -m tools.postbox.latency --identity seat-prod ...      (from a client checkout's root)

Read-only and offline: no network call, no write, no lock taken. Per exact (key, version) it joins five times
that already exist on disk and that nothing joined before:

  created   the server's created_at (edited_at for an edit), as the spool line carries it
  spooled   the postbox's rx_at, stamped when the line reached the identity's log (postbox.record_listener_line)
  handed    `at` of the first committed batch that carried it, from every `<log>.*.offset.dispatch.jsonl`
            (and its rotated `.1`): codex queue accepted, channel pipe flushed, or waiter printed
  read      the client's read ledger, `<state file>.read.json`, through tools.meshy_node.node.ledger_records,
            which drops a ledger written for another peer id (the peer binding is that function's, not ours)
  handled   handled_at from the identity's handled_file (mark_handled.py), when it has one

and prints p50 and p95 for each of the four stages between them, the messages handed on but never read, and the
wakes builder A's suppression skipped, by reason, from `<log>.*.offset.skips.jsonl` (and its rotated `.1`).

WHY: diagnosing #58 took 13 days because no single record held enqueue and consume times. The dispatch log
(2026-10-06), the read ledger and handled_at all existed by mid October; the 15 October root cause still had to be
read by hand out of a Codex rollout. Joined, the HZ shape is one line: three notices spooled within a second of
signing, read inside a 3 h 59 min turn, and handed on hours AFTER they were read.

Rules that are choices, so they are written down (2026-10-23):
  * "Read" means a ledger record for the SAME key at a version >= the spooled one. That is kxxx's rule (a) for
    suppressing a wake (msg on PR 1's open points), so this report and the suppression agree on what fetched
    means; a read of version 1 does not cover an edit to version 2.
  * A stage is never clamped. hand-off to read goes NEGATIVE exactly when the seat read the message before the
    postbox handed it on, which is the #58 shape and the signal PR 1 exists to measure; `read_before_handoff`
    counts those rows.
  * Delivered-but-never-read lists only role `to`: a cc asks nothing of the seat (fetch_gap's rule). The ledger
    keeps the newest READ_LEDGER_MAX records, so when it is full a message that existed before its oldest record is
    reported as `outside_read_window`, not as unread: its read may simply have been dropped. Judged by when the
    message was created (an edit's edited_at), then spooled, never by its hand-off: a read can come before the
    hand-off, the #58 shape, so a message read at 10:00 and handed on at 13:00, with the oldest kept read at 12:00,
    would otherwise be listed as never read (review of PR 1, finding 7).
  * An edit line carries edited_at and no rx_at (record_listener_line stamps only message lines), so an edit's
    created-to-spooled and spooled-to-handed stages are unmeasured; its read and handled stages still count.
  * Percentiles are nearest-rank on the sorted deltas (no numpy here): p50 of [1, 2, 3, 4] is 2.

Never prints message text. Every output row is built field by field from a whitelist (key, version, role,
consumer, times, counts); a spool line carries `preview_unverified`, and no raw line is ever passed through.

Exit 0 with a report; 2 when the identity or config cannot be read (mark_handled.py's convention). A source
that is missing or unreadable is reported in `sources`, never fatal: a seat with no handled_file still gets the
first three stages.
"""
import argparse
import datetime
import glob
import itertools
import json
import math
import os
import sys

# Before any local import (review of PR 1, finding 12): set inside import_node only, it came after `import
# delivery_bank` had already written bytecode beside it, so a "read-only" report wrote files.
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import delivery_bank  # noqa: E402

STAGES = (('created_to_spooled', 'created', 'spooled'),
          ('spooled_to_handed', 'spooled', 'handed'),
          ('handed_to_read', 'handed', 'read'),
          ('read_to_handled', 'read', 'handled'))
#: The skip-log contract shared with builder A (PR 1 plan, 2026-10-23): one JSON line per skipped wake, appended to
#: `<offset-file>.skips.jsonl`, as {"key", "version", "reason", "spooled_at", "fetched_at", "at"}.
SKIPS_SUFFIX = '.skips.jsonl'
DISPATCH_SUFFIX = '.dispatch.jsonl'
_DISPATCH_ARCHIVES = tuple('%s.%d' % (DISPATCH_SUFFIX, i) for i in range(1, delivery_bank.DISPATCH_MAX_ARCHIVES + 1))
_SKIPS_ARCHIVES = tuple('%s.%d' % (SKIPS_SUFFIX, i) for i in range(1, delivery_bank.DISPATCH_MAX_ARCHIVES + 1))
MAX_STATE_BYTES = 1048576
MAX_CONFIG_BYTES = 1048576
MAX_REPORT_MESSAGES = 50000
MAX_JSONL_RECORDS = MAX_REPORT_MESSAGES


def when(text):
    """An ISO time as an aware datetime, or None. The spool writes "...Z" (postbox.now) and the inbox "+00:00"
    (reconcile copies recorded_at), so values are parsed and compared, never the strings. A naive time is UTC."""
    if not isinstance(text, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def iso(t):
    return t.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if t else None


def percentile(values, p):
    """Nearest-rank percentile of `values`, or None for none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100.0 * len(ordered)) - 1)]


def iter_jsonl(path, max_line=delivery_bank.MAX_LINE):
    """Stream JSON objects from `path` in 64 KiB chunks, draining lines over `max_line` bytes without buffering."""
    try:
        fd, _ = delivery_bank.open_regular_read(path)
    except FileNotFoundError:
        return

    def _parse(b):
        try:
            d = json.loads(b)
            return d if isinstance(d, dict) else None
        except ValueError:
            return None

    buf, skipping = bytearray(), False
    with os.fdopen(fd, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                d = _parse(buf) if (buf and not skipping and len(buf) <= max_line) else None
                if d is not None:
                    yield d
                return
            pos, view = 0, memoryview(chunk)
            while pos < len(chunk):
                nl = chunk.find(b'\n', pos)
                if skipping:
                    if nl < 0:
                        break
                    skipping, pos = False, nl + 1
                    continue
                end = len(chunk) if nl < 0 else nl + 1
                if len(buf) + (end - pos) > max_line:
                    buf.clear()
                    skipping, pos = (nl < 0), nl + 1
                    if skipping:
                        break
                    continue
                buf.extend(view[pos:end])
                if nl < 0:
                    break
                d, pos = _parse(buf), nl + 1
                buf.clear()
                if d is not None:
                    yield d


def jsonl(path, max_records=MAX_JSONL_RECORDS):
    """The JSON objects of a line file, skipping lines that are not objects, capped at `max_records`. [] when it is
    absent; a file that exists and cannot be opened raises, so the caller can name it as unreadable rather than empty."""
    stream = iter_jsonl(path)
    return list(stream if max_records is None else itertools.islice(stream, max(0, int(max_records))))


def offset_files(log):
    """Every delivery consumer's offset file this identity has: the Codex queue's `.codex.offset`, the channel's
    `.channel.offset`, a waiter's `.wait-<consumer>.offset` and a direct `.<consumer>.offset` (delivery_bank.peek_bank
    names all four). Found from the offset files AND the dispatch and skip logs beside them, so a consumer whose
    offset file was removed still has its history counted. Escaped, so a log path holding [ or ] globs literally."""
    base = glob.escape(log)
    found = set(glob.glob(base + '.*.offset'))
    for suffix in (DISPATCH_SUFFIX,) + _DISPATCH_ARCHIVES + (SKIPS_SUFFIX,) + _SKIPS_ARCHIVES:
        found |= {p[:-len(suffix)] for p in glob.glob(base + '.*.offset' + suffix)}
    return sorted(found)


def consumer_name(log, off_file):
    """`codex`, `channel`, `wait-session`: the part of the offset file's name between the log and `.offset`."""
    return off_file[len(log) + 1:-len('.offset')]


def load_identity(config, name):
    cfg = delivery_bank._bounded_json_file(config, MAX_CONFIG_BYTES)
    if not isinstance(cfg, dict):
        raise ValueError('config not dict')
    return next(i for i in cfg['identities'] if i['name'] == name)


def import_node(ident):
    """tools.meshy_node.node (for ledger_records and READ_LEDGER_MAX), from the identity's own client checkout first (it
    wrote the ledger, so its reader matches the format), then the checkout this file sits in. No bytecode is written:
    the client checkout is integrity-pinned (postbox.py sets PYTHONDONTWRITEBYTECODE for its children for the same
    reason). Run as `python -m tools.postbox.latency`, the `tools` package is already the current checkout's, so its
    reader is used whatever `client` says (finding 12); node.py imports its siblings relatively, so it cannot be loaded
    from another checkout by path. read_ledger names the checkout whose reader ran in `sources`, and the README gives
    the `python latency.py` form for reading a seat's own client."""
    roots = [ident.get('client'), os.path.dirname(os.path.dirname(HERE))]
    for root in [r for r in roots if r and os.path.isdir(os.path.join(r, 'tools', 'meshy_node'))]:
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from tools.meshy_node import node
            return node
        except ImportError:
            continue
    return None


def read_ledger(ident, sources):
    """({key: [(version, at), ...]} of this identity's reads or None when unknown, whether the ledger is full). The peer binding is
    ledger_records': a ledger whose peer_id is not the state file's yields nothing (kxxx's rule (a): a fetch by
    another identity is not this seat's fetch). We only tell that case apart from "no reads" for the report."""
    state_path = ident.get('state')
    if not isinstance(state_path, str) or not state_path:
        sources['reads'] = 'unavailable: state file unreadable'
        return None, False
    try:
        state = delivery_bank._bounded_json_file(state_path, MAX_STATE_BYTES)
        if not isinstance(state, dict):
            raise ValueError('state not dict')
    except delivery_bank._OversizeJsonFile:
        sources['reads'] = 'unavailable: state file exceeds %d-byte limit' % MAX_STATE_BYTES
        return None, False
    except (OSError, ValueError, TypeError):
        sources['reads'] = 'unavailable: state file unreadable'
        return None, False
    node = import_node(ident)
    if node is None:
        sources['reads'] = 'unavailable: tools.meshy_node not importable'
        return None, False
    suffix = getattr(node, 'READ_LEDGER_SUFFIX', None)
    if not isinstance(suffix, str) or not suffix:
        sources['reads'] = 'unsupported: client has no READ_LEDGER_SUFFIX'
        return None, False
    if hasattr(node, 'ledger_records'):
        ok_label = 'ok'
    elif hasattr(node, '_read_ledger_records'):
        ok_label = 'ok (compat _read_ledger_records)'
    else:
        sources['reads'] = 'unsupported: client has no ledger_records'
        return None, False
    ledger = state_path + suffix
    try:
        raw = delivery_bank._bounded_json_file(ledger, delivery_bank.MAX_LEDGER)
    except FileNotFoundError:
        sources['reads'] = 'missing'
        return None, False
    except delivery_bank._OversizeJsonFile:
        sources['reads'] = 'unavailable: ledger exceeds %d-byte limit' % delivery_bank.MAX_LEDGER
        return None, False
    except (OSError, ValueError, TypeError):
        sources['reads'] = 'unavailable: ledger damaged or another schema'
        return None, False
    if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw.get('schema_version') != 1:
        sources['reads'] = 'unavailable: ledger damaged or another schema'
        return None, False
    peer = str(state.get('peer_id') or '')
    if not peer or raw.get('peer_id') != peer:
        sources['reads'] = ('ignored: ledger written for another peer id'
                            if raw.get('peer_id') != state.get('peer_id') else ok_label)
        return {}, False
    records = []
    for r in raw.get('reads') if isinstance(raw.get('reads'), list) else []:
        if (isinstance(r, dict) and isinstance(r.get('key'), str) and r['key'].startswith('msg/')
                and type(r.get('version')) is int and r['version'] >= 1 and isinstance(r.get('at'), str)):
            records.append({'key': r['key'], 'version': r['version'], 'at': r['at']})
    if not records:
        sources['reads'] = ok_label
        return {}, False
    sources['reads'] = ok_label
    node_file = getattr(node, '__file__', None)
    if isinstance(node_file, str) and node_file:
        via = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(node_file))))
        client = ident.get('client')
        if client and os.path.normcase(os.path.abspath(client)) != os.path.normcase(via):
            sources['reads_via'] = "this checkout, not the identity's client: " + via
    reads = {}
    for r in records:
        t = when(r['at'])
        if t:
            reads.setdefault(r['key'], []).append((r['version'], t))
    # Full means the client has started dropping its oldest records (node.READ_LEDGER_MAX), so absence of a read
    # older than the oldest kept one proves nothing.
    return reads, len(records) >= getattr(node, 'READ_LEDGER_MAX', 2000)


def handled_times(ident, sources):
    """{(key, version): handled_at} from the handled file, read directly. delivery_bank.handled() is not used: it
    returns pairs without handled_at. Its FILE checks are applied here too (review of PR 1, finding 11): a file of
    another schema or written for another identity yields nothing, since delivery refuses it whole. Its Windows rule,
    a private ACL attested in the config, is reported rather than enforced: a file delivery would refuse for want of
    it still gives its times, and `sources` says delivery refuses it. Entries it would skip (entry_faults) are
    skipped."""
    path = ident.get('handled_file')
    if not path:
        sources['handled'] = 'not configured'
        return {}
    try:
        data = delivery_bank.private_json(path)
    except (OSError, ValueError) as e:
        sources['handled'] = 'unavailable: %s' % type(e).__name__
        return {}
    if (not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('identity') != ident.get('name')
            or not isinstance(data.get('handled'), list)):
        sources['handled'] = 'unavailable: another schema or identity (delivery refuses this file)'
        return {}
    out = {}
    for item in data['handled']:
        if not delivery_bank.entry_faults(item):
            t = when(item.get('handled_at'))      # entries written before #58's handled_at have none
            if t:
                out[(item['key'], item['version'])] = t
    sources['handled'] = ('ok' if os.name != 'nt' or ident.get('handled_private_acl_verified') is True
                          else 'ok, but its private ACL is not attested, so delivery refuses this file')
    return out


def report(ident, since=None, max_messages=MAX_REPORT_MESSAGES):
    log = ident['log']
    sources = {}
    rows = {}
    capped = False
    # Spool: the message and edit lines, keyed by exact version (a line without one is version 1, as delivery reads it).
    spool, sources['spool'] = (iter_jsonl(log), 'ok') if os.path.lexists(log) else ((), 'missing')
    try:
        for d in spool:
            v = delivery_bank.version(d)
            if d.get('event') not in ('message', 'edit') or not isinstance(d.get('key'), str) or v is None:
                continue
            spooled = when(d.get('rx_at'))
            created = when(d.get('edited_at') if d.get('event') == 'edit' else d.get('created_at')) or when(d.get('created_at'))
            if since and (spooled or created) and (spooled or created) < since:
                continue
            if (d['key'], v) not in rows and max_messages is not None and len(rows) >= max_messages:
                capped = True
                if max_messages <= 0:
                    continue
                rows.pop(next(iter(rows)))
            row = rows.setdefault((d['key'], v), {'role': None, 'created': None, 'spooled': None, 'handed': None,
                                                   'consumers': [], 'read': None, 'handled': None})
            row['role'] = row['role'] or (d.get('role') if d.get('role') in ('to', 'cc') else None)
            row['created'] = row['created'] or created
            if spooled and (row['spooled'] is None or spooled < row['spooled']):
                row['spooled'] = spooled
        if capped:
            sources['spool'] = 'ok (capped at %d messages)' % max_messages
    except (OSError, ValueError) as e:
        rows.clear()
        sources['spool'] = 'unavailable: %s' % type(e).__name__
    # Dispatch: the earliest hand-off of each pair over every consumer; a pair two consumers handed on counts once.
    consumers = {}
    skips = {}
    for off in offset_files(log):
        name = consumer_name(log, off)
        handed = 0
        for suffix in _DISPATCH_ARCHIVES + (DISPATCH_SUFFIX,):
            try:
                for batch in iter_jsonl(off + suffix):
                    at = when(batch.get('at'))
                    for e in batch.get('events') if isinstance(batch.get('events'), list) else []:
                        if not isinstance(e, dict) or e.get('event') not in ('message', 'edit') or not isinstance(e.get('key'), str):
                            continue
                        row = rows.get((e['key'], delivery_bank.version(e)))
                        if row is None or at is None:
                            continue
                        handed += 1
                        if name not in row['consumers']:
                            row['consumers'].append(name)
                        if row['handed'] is None or at < row['handed']:
                            row['handed'] = at
            except (OSError, ValueError):
                sources['dispatch:' + name] = 'unavailable'
        consumers[name] = handed
        # Skips: builder A's contract. Absent means nothing was skipped (or A's change is not installed): 0. The skip
        # log rotates like the dispatch log (delivery_bank.dispatch_line renames it to `.1` .. `.16` past
        # DISPATCH_LOG_MAX), so every archive plus the active log is read in chronological order.
        for suffix in _SKIPS_ARCHIVES + (SKIPS_SUFFIX,):
            try:
                for s in iter_jsonl(off + suffix):
                    at = when(s.get('at'))
                    if since and at and at < since:
                        continue
                    reason = s.get('reason') if isinstance(s.get('reason'), str) else 'unknown'
                    skips[reason] = skips.get(reason, 0) + 1
            except (OSError, ValueError):
                sources['skips:' + name] = 'unavailable'
    reads, ledger_full = read_ledger(ident, sources)
    handled = handled_times(ident, sources)
    oldest_read = None
    if reads:
        oldest_read = min(t for versions in reads.values() for _, t in versions)
    for (key, v), row in rows.items():
        if reads is not None:
            covering = [t for ver, t in reads.get(key, ()) if ver >= v]
            row['read'] = min(covering) if covering else None
        row['handled'] = handled.get((key, v))
    deltas = {name: [] for name, _, _ in STAGES}
    for row in rows.values():
        for name, a, b in STAGES:
            if row[a] and row[b]:
                deltas[name].append(round((row[b] - row[a]).total_seconds(), 3))
    unread, outside = [], []
    if reads is not None:
        for (key, v), row in sorted(rows.items(), key=lambda kv: (kv[1]['handed'] or kv[1]['spooled'] or
                                                                     datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), kv[0])):
            if row['role'] != 'to' or row['handed'] is None or row['read'] is not None:
                continue
            entry = {'key': key, 'version': v, 'handed_at': iso(row['handed'])}
            existed = row['created'] or row['spooled'] or row['handed']    # a read cannot come before the message
            (outside if ledger_full and oldest_read and existed < oldest_read else unread).append(entry)
    return {
        'identity': ident['name'],
        'since': iso(since),
        'sources': sources,
        'messages': len(rows),
        'handed_on': sum(1 for r in rows.values() if r['handed']),
        'read': sum(1 for r in rows.values() if r['read']),
        'handled': sum(1 for r in rows.values() if r['handled']),
        'read_before_handoff': sum(1 for x in deltas['handed_to_read'] if x < 0),
        'stages': {name: {'count': len(xs), 'p50_s': percentile(xs, 50), 'p95_s': percentile(xs, 95)}
                   for name, xs in deltas.items()},
        'consumers': consumers,
        'delivered_never_read': unread if reads is not None else None,
        'outside_read_window': outside,
        'skips': dict(sorted(skips.items())),
        'per_key': [{'key': key, 'version': v, 'role': row['role'], 'created_at': iso(row['created']),
                     'spooled_at': iso(row['spooled']), 'handed_at': iso(row['handed']),
                     'consumers': sorted(row['consumers']), 'read_at': iso(row['read']),
                     'handled_at': iso(row['handled'])}
                    for (key, v), row in sorted(rows.items())],
    }


def text(rep):
    out = ['latency report for %s%s' % (rep['identity'], (' since ' + rep['since']) if rep['since'] else '')]
    out.append('sources: ' + ', '.join('%s=%s' % kv for kv in sorted(rep['sources'].items())))
    out.append('messages %d, handed on %d, read %d, handled %d, read before hand-off %d'
               % (rep['messages'], rep['handed_on'], rep['read'], rep['handled'], rep['read_before_handoff']))
    for name, s in rep['stages'].items():
        out.append('  %-20s n=%-5d p50=%-12s p95=%s' % (name, s['count'], fmt(s['p50_s']), fmt(s['p95_s'])))
    if rep['consumers']:
        out.append('hand-offs by consumer: ' + ', '.join('%s=%d' % kv for kv in sorted(rep['consumers'].items())))
    if rep['delivered_never_read'] is None:
        out.append('delivered but never read: unknown (no usable read ledger)')
    else:
        out.append('delivered but never read (to only): %d' % len(rep['delivered_never_read']))
        out.extend('  %s v%d handed %s' % (e['key'], e['version'], e['handed_at']) for e in rep['delivered_never_read'])
    if rep['outside_read_window']:
        out.append('handed on before the read ledger\'s oldest record (read unknown): %d' % len(rep['outside_read_window']))
    total = sum(rep['skips'].values())
    out.append('skipped wakes: %d%s' % (total, (' (' + ', '.join('%s=%d' % kv for kv in rep['skips'].items()) + ')')
                                        if total else ''))
    return '\n'.join(out)


def fmt(seconds):
    return '-' if seconds is None else '%.3fs' % seconds


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--identity', required=True)
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--since', help='ISO time; only messages spooled (or created) at or after it')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    since = when(args.since) if args.since else None
    if args.since and since is None:
        ap.error('--since is not an ISO time')
    rep = report(load_identity(args.config, args.identity), since)
    print(json.dumps(rep, indent=2) if args.json else text(rep))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, StopIteration):
        print('latency: config or identity unavailable', file=sys.stderr)
        sys.exit(2)
