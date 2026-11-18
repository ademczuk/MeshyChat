"""Recognize fully handled queued notices without mutating queue or task state.

The consumer supplies its installed batch renderer and protected handled reader.
An exact rendering match is required: routing-looking operator text is not enough.
Versionless message notices can prove only original version 1. Edits, warnings,
mixed batches and unrecognized input stay queued for owner reconciliation.
"""
import re
import sys

MAX_EVENTS = 12
MAX_BYTES = 24000


def _stderr(msg):
    sys.stderr.write(msg + '\n')


def _report(note, msg):
    """Best effort, like postbox.note: a failing log must not turn the answer into a raise."""
    try:
        (note or _stderr)(msg)
    except Exception:
        pass


def handled_keys(item, ident, rows, render_batch, is_handled, note=None):
    """Return a tuple of original message keys safe to reconcile, or None.

    rows is this identity's own latest spool row per key. render_batch(ident,
    events) must reproduce its installed notice verbatim; is_handled(ident,
    event) must validate its own protected exact-version handling evidence.
    Re-run this predicate immediately before a supported queue deletion. Never
    turn queue acceptance, a peer preview, or an unresolved task into a receipt.

    It never raises for a callback: whatever Exception render_batch or
    is_handled raises (an AttributeError on an unexpected row as much as an
    OSError), the item is kept (None) and one line naming the callback and the
    exception's class, never its text or any message text, goes to note (one
    str argument; stderr when None). KeyboardInterrupt and SystemExit propagate.
    """
    if not isinstance(item, dict) or not isinstance(ident, dict) or not isinstance(rows, dict):
        return None
    inputs = item.get('input')
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict):
        return None
    value = inputs[0]
    text = value.get('text')
    if value.get('type') != 'text' or not isinstance(text, str):
        return None
    try:
        if len(text.encode('utf-8')) > MAX_BYTES:
            return None
    except UnicodeError:
        return None
    name = ident.get('name')
    if not isinstance(name, str) or not name:
        return None
    # Legacy headers start with MeshyChat; current headers have a routing tag
    # and sender before it. Exact installed rendering below validates all text.
    pattern = (r'^[^\n]*?MeshyChat message for you \(' + re.escape(name)
               + r'\)\. role=(to|cc) key=(msg/\S+) ')
    refs = re.findall(pattern, text, re.MULTILINE)
    if not refs or len(refs) > MAX_EVENTS:
        return None
    keys = tuple(key for _, key in refs)
    if len(set(keys)) != len(keys):
        return None
    events = []
    for role, key in refs:
        event = rows.get(key)
        if not isinstance(event, dict):
            return None
        version = event.get('version', 1)
        if (event.get('event') != 'message' or event.get('key') != key
                or event.get('role') != role or type(version) is not int or version != 1):
            return None
        events.append(event)
    stage = 'render_batch'
    try:
        if text != render_batch(ident, events):
            return None
        stage = 'is_handled'
        if not all(is_handled(ident, event) is True for event in events):
            return None
    except Exception as e:
        _report(note, 'queued_notices: %s raised %s for %s; %d notice key(s) kept queued'
                % (stage, type(e).__name__, name, len(keys)))
        return None
    return keys
