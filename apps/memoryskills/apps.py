from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class MemorySkillsConfig(AppConfig):
    """Loads the fleet roster (docs/DIVERGENCES.md 034) and, with MESHY_CAPABILITIES on, the
    capability root key store (divergence 040, slice 4) at startup. Carries no models.

    ready() runs once per process, inside django.setup(): daphne and uvicorn reach it
    through meshychat.asgi, and every manage.py command through its own setup. A roster
    that cannot be read or parsed raises ImproperlyConfigured there, so the process does
    not start (apps/memoryskills/roster.py says why). So does, with tokens on, a missing or
    invalid root key file, a verify set without the active kid, and an `import biscuit_auth`
    that fails (apps/memoryskills/capability_keys.py). That stops every manage.py command
    too, so the refusal carries the runbook line: unset MESHY_CAPABILITIES to run commands
    while the key is repaired. With tokens off nothing is loaded and biscuit_auth is not
    imported. The label is prefixed, as apps/jobs/apps.py's is, so it cannot collide with a
    third-party app.
    """

    name = "apps.memoryskills"
    label = "meshy_memoryskills"

    def ready(self) -> None:
        from . import roster
        try:
            roster.load(getattr(settings, "MESHY_FLEET_ROSTER_PATH", None))
        except roster.RosterRefused as exc:
            raise ImproperlyConfigured(
                f"{exc}. The server does not start with a fleet roster it cannot use: an "
                "empty one would refuse every fleet peer whose from or author is its roster "
                "label (docs/DIVERGENCES.md 028, 034). Fix the file, or unset the variable "
                "to run with no roster.") from exc

        from . import capability_keys
        try:
            capability_keys.load_at_startup(settings)
        except capability_keys.CapabilityKeyRefused as exc:
            raise ImproperlyConfigured(
                f"{exc}. The server does not start with capability tokens on and a root key "
                "it cannot use: every token would be refused, or a token minted under another "
                f"key accepted (docs/DIVERGENCES.md 040). Fix the key, or "
                f"{capability_keys.RUNBOOK}.") from exc
