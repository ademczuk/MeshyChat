"""Record terminal handling from this seat's saved successful signed reads.

Run only after doing the requested work, or deciding it needs no action. A
delivered notice is NOT a receipt. Input is the client's verified
read JSON saved locally; never a spool preview or a peer-supplied attestation.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import delivery_bank  # noqa: E402
import postbox_os  # noqa: E402


def record(ident, reads, disposition):
    if disposition not in delivery_bank.TERMINAL:
        raise ValueError('only terminal decisions can be recorded')
    path = ident['handled_file']
    fd = os.open(path + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not postbox_os.try_lock(fd):
            raise ValueError('another handled writer is active; retry')
        current = delivery_bank.handled(ident)
        for read in reads:
            if (not isinstance(read, dict) or read.get('signature_verified') is not True or
                    read.get('status_verified') is not True or not isinstance(read.get('key'), str) or
                    'version' not in read or delivery_bank.version(read) is None):
                raise ValueError('a saved successful signed read with exact key/version is required')
        data = delivery_bank.private_json(path)
        entries = {(item['key'], item['version']): item for item in data['handled']}
        # handled_at (#58, 2026-10-06): when this seat recorded the decision, to set against the dispatch log's
        # times. Re-recording the same decision keeps the first time; a new disposition gets a new one. Entries
        # written before this field have none, and the delivery bank does not read it.
        stamp = delivery_bank.utc_now()
        for read in reads:
            old = entries.get((read['key'], read['version'])) or {}
            same = old.get('disposition') == disposition and isinstance(old.get('handled_at'), str)
            entries[(read['key'], read['version'])] = {
                'key': read['key'], 'version': read['version'], 'disposition': disposition,
                'signature_verified': True, 'status_verified': True,
                'handled_at': old['handled_at'] if same else stamp}
        data['handled'] = list(entries.values())
        delivery_bank.atomic_json(path, data)
        return len(set(entries) - current)
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('identity')
    ap.add_argument('--config', default=os.path.join(os.path.dirname(__file__), 'config.json'))
    ap.add_argument('--init', action='store_true', help='create an empty owner-only handled receipt; never replace one')
    ap.add_argument('--read-file', action='append', default=[])
    ap.add_argument('--disposition', choices=delivery_bank.TERMINAL, default='done')
    args = ap.parse_args()
    cfg = postbox_os.read_bounded_json(args.config, max_bytes=postbox_os.MAX_CONFIG_BYTES)
    ident = next(i for i in cfg['identities'] if i['name'] == args.identity)
    path = ident['handled_file']
    if args.init:
        parent = os.path.dirname(os.path.abspath(path))
        if delivery_bank._is_link_or_reparse(os.lstat(parent)):
            raise ValueError('receipt path is a symlink')
        # Exclusive create: an existing receipt/lock owner must not be erased.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'schema_version': 1, 'identity': ident['name'], 'handled': []}, f)
            f.flush()
            os.fsync(f.fileno())
    if args.read_file:
        reads = [postbox_os.read_bounded_json(name, max_bytes=delivery_bank.MAX_LEDGER) for name in args.read_file]
        count = record(ident, reads, args.disposition)
        print(json.dumps({'new_handled_versions': count}))
    elif not args.init:
        ap.error('supply saved signed reads or --init')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, StopIteration):
        print('mark_handled: receipt unavailable or input not a verified read', file=sys.stderr)
        sys.exit(2)
