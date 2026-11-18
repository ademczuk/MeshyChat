"""The fleet roster: read ONCE at startup into the label map divergence 028 consults
(docs/DIVERGENCES.md 034).

`MESHY_FLEET_ROSTER_PATH` names the roster file. Unset: no roster, said once in the log. Set:
the bytes are read once, parsed strictly, and installed with `handlers.set_roster_labels`; a
file that cannot be read or parsed REFUSES STARTUP. The reason is 028: a mesh message's `from`
and author must be one of the caller's labels, the roster label among them, so a roster that
silently loaded empty would refuse every fleet peer that sends as its roster label, which is
the cutover failure loading the roster exists to prevent.

THE SHAPE is a JSON object whose "peers" maps a peer id to its label. The other top-level
fields (version, updated, notes, operator, operator_peer_id, provisional_peers) are ignored.
Refused, each by name: bytes that are not UTF-8 or not JSON (a byte order mark included), a
top level that is not an object, "peers" absent, not an
object or empty, a key that appears twice in any object, a peer id that is not a uuid or not
in the canonical lower-case form (labels are looked up by `str(peer_id)`, so any other
spelling would never match), a label that is not a non-empty string without surrounding
whitespace (labels are compared byte for byte).

THE LABEL RULES 028 DEPENDS ON (review F3 of fin/r1-small), also refused by name, because each
lets one peer send as, or be delivered as, another: a label outside the v1 label alphabet in
any case (1 to 64 ASCII letters, digits, '_', '.', '-', starting with a letter or digit:
validation.MESH_LABEL_RE with upper case allowed; so no zero-width space, no Kelvin sign that
lower-cases to 'k'); the same label for two peers; a label shaped like another peer's
fallback, `peer-<8 hex>` in any case (handle_write would stamp it as that peer's author); and a
broadcast label, `all` or `both` in any case.

LOADED BUT WARNED (review F2): an upper-case label, and two labels that differ only by case.
Fleet rosters carry both (two upper-case labels of 21, one the case-twin of another peer's
label), so refusing either would stop the server on a real roster file. 028 and inbox/owed
compare labels exactly; the push and /v1/mesh/wait lower-case them
(mesh_notify.peer_labels). So an upper-case label can be a peer's author but never its v1
`from` or a `to`, and a case pair is two labels to 028 and one to the push. Whether to
case-fold is the operator's decision (docs/DIVERGENCES.md 034); `warnings` names each at
startup. Nothing aliases one label to another: a `<label>-cc` seat is not `<label>` (operator
ruling, 028). Not checked here, because it needs the database: a roster label that is another
peer's `peers.name` (the same exposure two peers sharing a peers.name have, 028).

This module does not import Django: `apps/memoryskills/apps.py` calls `load` from ready() and
turns a refusal into ImproperlyConfigured.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path

from . import handlers
from .validation import MESH_LABEL_RE

ENV = "MESHY_FLEET_ROSTER_PATH"
log = logging.getLogger("meshychat.fleet_roster")

#: apps/wsgw/mesh_notify.BROADCAST_LABELS (a row to one of these is for everyone), held here
#: so this module stays free of the gateway's imports; tests/test_fleet_roster_startup.py pins
#: the two equal.
BROADCAST_LABELS = frozenset({"all", "both"})


class RosterRefused(ValueError):
    """The roster named by MESHY_FLEET_ROSTER_PATH cannot be used. The message names why."""


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise RosterRefused(f"key {key!r} appears twice in one object")
        out[key] = value
    return out


def parse(raw: bytes) -> dict[str, str]:
    """peer id -> label from the roster's bytes, or RosterRefused. Never empty."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RosterRefused(f"is not UTF-8 ({exc.reason} at byte {exc.start})") from None
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicates)
    except RosterRefused:
        raise
    except ValueError as exc:
        raise RosterRefused(f"is not JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise RosterRefused(f"is not a JSON object (its top level is a {type(doc).__name__})")
    peers = doc.get("peers")
    if not isinstance(peers, dict):
        raise RosterRefused('has no "peers" object mapping each peer id to its label')
    if not peers:
        raise RosterRefused('"peers" is empty: a roster that names no peer is the empty '
                            'roster this refusal exists to prevent')
    labels: dict[str, str] = {}
    for peer_id, label in peers.items():
        try:
            canonical = str(uuid.UUID(peer_id))
        except ValueError:
            raise RosterRefused(f"peer id {peer_id!r} is not a uuid") from None
        if canonical != peer_id:
            raise RosterRefused(f"peer id {peer_id!r} is not in canonical form "
                                f"{canonical!r}, so it would never match a peer")
        if not isinstance(label, str) or not label or label != label.strip():
            raise RosterRefused(f"the label of {peer_id} is {label!r}: a label is a "
                                "non-empty string with no surrounding whitespace")
        _check_label(peer_id, label, labels)
        labels[peer_id] = label
    return labels


def _check_label(peer_id: str, label: str, earlier: dict[str, str]) -> None:
    """The label rules 028 depends on (module docstring), or RosterRefused naming the rule."""
    folded = label.lower()
    if not (label.isascii() and MESH_LABEL_RE.fullmatch(folded)):
        raise RosterRefused(
            f"the label of {peer_id} is {label!r}, which is not a label: 1 to 64 ASCII "
            "letters, digits, '_', '.' or '-', starting with a letter or digit (the v1 label "
            "rule, in any case)")
    if folded in BROADCAST_LABELS:
        raise RosterRefused(
            f"the label of {peer_id} is {label!r}, a broadcast address: a message to "
            "everyone would read as addressed to this one peer")
    if handlers.FALLBACK_LABEL_RE.match(folded) and label != handlers.fallback_label(peer_id):
        raise RosterRefused(
            f"the label of {peer_id} is {label!r}, the shape of the server's fallback "
            "label for ANOTHER peer (peer-<the first 8 characters of its id>): writes by "
            f"{peer_id} would be attributed to that peer")
    for other, taken in earlier.items():
        if taken == label:
            raise RosterRefused(
                f"the label {label!r} of {peer_id} is also the label of {other}: two peers "
                "under one label could each send as the other")


def warnings(labels: dict[str, str]) -> list[str]:
    """What a parsed roster loads but cannot do, one message each (review F2 of
    fin/r1-small): every upper-case label, in roster order, then every group of labels that
    differ only by case. Empty for a roster of lower-case, case-distinct labels."""
    out = []
    for peer_id, label in labels.items():
        if label != label.lower():
            out.append(
                f"fleet roster: the label {label!r} of {peer_id} has upper-case letters. A v1 "
                "from, to or cc is lower-case, so it can be this peer's author but never its "
                "from, and inbox and owed (which compare labels exactly) never match, through "
                "this label, a message addressed to its lower-case form, while the push and "
                "/v1/mesh/wait (lower-cased) deliver it. The server does not case-fold: an "
                "operator decision (docs/DIVERGENCES.md 034)")
    groups: dict[str, list[str]] = {}
    for peer_id, label in labels.items():
        groups.setdefault(label.lower(), []).append(peer_id)
    for folded, peers in groups.items():
        if len(peers) > 1:
            named = ", ".join(f"{labels[p]!r} ({p})" for p in peers)
            out.append(
                f"fleet roster: the labels {named} differ only by case. 028 and inbox compare "
                "labels exactly, so they are separate labels there and neither peer can send "
                "as another; the push and /v1/mesh/wait lower-case them, so each of these "
                f"peers is nudged for a message addressed to {folded!r}. An operator decision "
                "(docs/DIVERGENCES.md 034)")
    return out


def load(path: str | None) -> int | None:
    """Called ONCE, from MemorySkillsConfig.ready(). None when no roster is configured;
    else the number of labels installed. RosterRefused, naming the variable and the path,
    when the file cannot be read or parsed: the caller refuses to start."""
    if not path:
        log.info("no fleet roster: %s is unset, so a caller's labels are its peers.name "
                 "and peer-<id8> (docs/DIVERGENCES.md 028, 034)", ENV)
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise RosterRefused(f"{ENV}={path} cannot be read: {type(exc).__name__}: "
                            f"{exc.strerror or exc}") from None
    try:
        labels = parse(raw)
    except RosterRefused as exc:
        raise RosterRefused(f"{ENV}={path} {exc}") from None
    count = handlers.set_roster_labels(labels)
    log.info("fleet roster loaded from %s: %d peers, sha256 %s (read once; a later edit "
             "takes effect at the next start)", path, count, hashlib.sha256(raw).hexdigest())
    for said in warnings(labels):
        log.warning("%s", said)
    return count
