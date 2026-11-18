"""`MESHY_MESH_ACCEPT_UNVERSIONED`: a TRANSITIONAL switch, OFF by default, that accepts a
shared/mesh-chat message with no `schema_version` (docs/DIVERGENCES.md 011, dated paragraph
2026-09-27).

WHY IT EXISTS. The operator decided on 2026-09-27 that the fleet's nodes keep their identities
while their senders are repointed at this server for a same-day cutover. The legacy sender at
`tools/memory/mesh_send.py` sets no `schema_version`, and divergence 011 refuses such a write,
so after the repoint every node's default send would be refused. Production accepts it until
TASK-616 completes the fleet migration.

WHAT IT OPENS, and nothing more: `validation.validate_mesh_message_v1` returns early for a
`msg/` value in shared/mesh-chat that is an object with NO `schema_version` key. A value that
declares one is still validated in full; a non-object is still refused; the key rules and
divergence 028's authorship binding run as before. It is to be turned OFF once the nodes send
through `tools.meshy_node`, which always sends v1.

HOW IT IS SEEN. `note_accepted` runs once per row memory.write actually wrote under it (never
for a refusal or a replay): it moves `meshychat_mesh_unversioned_accepted_total` on GET /v1/metrics
(apps/core/metrics.py) and logs one INFO line naming the caller's label, never the value, at
most once per label per LOG_INTERVAL_S.

meshychat/settings.py parses the variable while it loads, so at import time this module's only
project import is apps.core.capability_flag, which imports none (that module's reason: no
apps.memoryskills while settings is half loaded); `note_accepted` imports the metrics module
when it runs. The vocabulary is capability_flag's, MESHYCHAT_DPOP_ENFORCE's spellings, so one
spelling means one thing across the project.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from apps.core.capability_flag import FLAG_FALSE, FLAG_TRUE, parse_flag

ENV_VAR = "MESHY_MESH_ACCEPT_UNVERSIONED"

log = logging.getLogger("meshychat.mesh_unversioned")

#: At most one INFO line per label in this many seconds.
LOG_INTERVAL_S = 300.0
#: The limiter remembers at most this many labels. Past it, it forgets them all and starts
#: again: at worst one extra line per label, never unbounded memory.
LOG_MAX_LABELS = 256
#: A label longer than this is cut in the log line.
LOG_LABEL_CHARS = 64


class UnversionedConfigError(ValueError):
    """MESHY_MESH_ACCEPT_UNVERSIONED is not a boolean. Raised while settings.py loads."""


def accept_from_env(env) -> bool:
    """`MESHY_MESH_ACCEPT_UNVERSIONED`, parsed. Unset or blank is OFF; the true and false
    spellings of MESHYCHAT_DPOP_ENFORCE; anything else refuses to load. meshychat/settings.py calls
    this once."""
    raw = env.get(ENV_VAR)
    value = parse_flag(raw)
    if value is None:
        raise UnversionedConfigError(
            f"{ENV_VAR}={raw!r} is not a boolean. It is a transitional switch, off unless it is "
            f"one of {', '.join(FLAG_TRUE)}; to leave it off, unset it or set it to 'off'.")
    return value


def accept_from_settings() -> bool:
    """The ONE reader of settings.MESHY_MESH_ACCEPT_UNVERSIONED, read per write so
    `override_settings` takes effect. No getattr default: settings.py always defines the name,
    and only the bool True opens it.

    A process with NO Django settings (the memory skills are driven over a bare connection by
    tests/test_memory_skills.py, which never configures Django) has no setting to read, and
    touching one there raises ImproperlyConfigured. There the same variable is parsed from the
    environment by the same strict parser, so one variable decides wherever memory.write runs."""
    from django.conf import settings
    if not settings.configured and not os.environ.get("DJANGO_SETTINGS_MODULE"):
        return accept_from_env(os.environ)
    return settings.MESHY_MESH_ACCEPT_UNVERSIONED is True


class LabelLimiter:
    """Admits a label at most once per `interval_s`, remembering at most `max_labels`."""

    def __init__(self, interval_s: float, max_labels: int, *, clock=time.monotonic):
        self._interval = float(interval_s)
        self._max = int(max_labels)
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def admit(self, label: str) -> bool:
        now = self._clock()
        with self._lock:
            last = self._last.get(label)
            if last is not None and now - last < self._interval:
                return False
            if last is None and len(self._last) >= self._max:
                self._last.clear()
            self._last[label] = now
            return True


LIMITER = LabelLimiter(LOG_INTERVAL_S, LOG_MAX_LABELS)


def shown_label(label) -> str:
    """The label as the log line carries it: text, at most LOG_LABEL_CHARS characters."""
    return str(label)[:LOG_LABEL_CHARS]


def note_accepted(label) -> None:
    """One unversioned mesh message was WRITTEN by `label`'s memory.write. Counts it, and logs
    it at most once per label per LOG_INTERVAL_S. The value is never passed here, so it cannot
    be logged."""
    from apps.core import metrics
    metrics.MESH_UNVERSIONED_ACCEPTED.inc()
    shown = shown_label(label)
    if LIMITER.admit(shown):
        # %r: a label is caller-influenced text, and repr keeps a newline in it on one line.
        log.info("memory.write accepted an unversioned shared/mesh-chat message from %r "
                 "(%s is on; %d accepted by this process; docs/DIVERGENCES.md 011)",
                 shown, ENV_VAR, metrics.MESH_UNVERSIONED_ACCEPTED.value())


__all__ = ["ENV_VAR", "FLAG_FALSE", "FLAG_TRUE", "LIMITER", "LOG_INTERVAL_S", "LOG_LABEL_CHARS",
           "LOG_MAX_LABELS", "LabelLimiter", "UnversionedConfigError", "accept_from_env",
           "accept_from_settings", "note_accepted", "shown_label"]
