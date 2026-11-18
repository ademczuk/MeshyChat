"""Read-only held-mail peek for meshy-mod's command Stop hook (held_stop.cjs).

argv: <client checkout> <postbox config path> <identity name> [<consumer>[,<consumer>...]]
Prints one JSON line: {"held": [{"key", "from", "held_at"}], "read": [keys], "error": str|null}.

Keys, sender labels and held times only, never preview text. It never takes a lock or writes a file:
delivery_bank.peek_bank is lock-free, and the identity's whole config entry goes to it, so the
handled receipts (handled_file) are subtracted (antigravity 2). The keys the seat already read come back
in "read" for the hook to subtract, from delivery_bank.read_ledger: the bank's mirror of the client's rule,
which drops a ledger written for another peer (pxx P2-2, GitHub Codex at held_peek.py:38: a state file
replaced by a new identity must not inherit the old one's reads). read_ledger opens the state file only for
its peer_id, inside this process; only keys are printed, and its fault reports are discarded, not printed.
"""
import json
import os
import sys

CONFIG_MAX_BYTES = 1_000_000


def _bounded_json(path, limit):
    with open(path, 'rb') as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('over %d bytes' % limit)
    return json.loads(raw.decode('utf-8'))


def _read_keys(delivery_bank, ident):
    records = delivery_bank.read_ledger(ident, report=lambda _line: None)
    return sorted(k for k in records if isinstance(k, str))


def main(argv):
    client, config_path, name = argv[1], argv[2], argv[3]
    consumers = [c for c in (argv[4] if len(argv) > 4 else 'claude-channel').split(',') if c]
    sys.path.insert(0, os.path.join(client, 'tools', 'postbox'))
    import delivery_bank  # noqa: E402  (after the path insert, by design)

    cfg = _bounded_json(config_path, CONFIG_MAX_BYTES)
    idents = cfg.get('identities') if isinstance(cfg, dict) else None
    if not isinstance(idents, list):
        raise ValueError('the postbox config has no identities list')
    ident = next((i for i in idents if isinstance(i, dict) and i.get('name') == name), None)
    if ident is None or not isinstance(ident.get('log'), str):
        raise ValueError('identity not in the postbox config')
    # Every named consumer's bank, held rows joined by key (pxx P2-3). One bank that errors means the
    # identity is not judged: the error is returned and the hook fails open.
    held, errors = {}, []
    for consumer in consumers:
        info = delivery_bank.peek_bank(ident['log'], consumer=consumer, ident=ident)
        if info.get('error'):
            errors.append('%s: %s' % (consumer, info['error']))
            continue
        for h in info.get('held') or []:
            key = h.get('key')
            if isinstance(key, str) and key not in held:
                held[key] = {'key': key, 'from': h.get('from'), 'held_at': h.get('held_at')}
    return {'held': list(held.values()), 'read': _read_keys(delivery_bank, ident),
            'error': '; '.join(errors) if errors else None}


if __name__ == '__main__':
    try:
        out = main(sys.argv)
    except Exception as err:  # the hook fails open on any error; say which
        out = {'held': [], 'read': [], 'error': (type(err).__name__ + ': ' + str(err))[:120]}
    sys.stdout.write(json.dumps(out) + '\n')
