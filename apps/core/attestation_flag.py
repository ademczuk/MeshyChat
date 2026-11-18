"""`MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE`: one parser for both readers (B09).

The flag is loaded once at boot. Its parsing rules are:

  * unset is false;
  * exactly "true" and exactly "false" are the only values. "1", "yes", "TRUE", " true" and
    the empty string are parse errors and settings do not load;
  * variable names are matched case-insensitively, so a lower-case spelling is the same
    field where the environment is case-sensitive, and two spellings are a duplicate-field
    error.

meshychat/settings.py calls `enforce_from_env` once while it loads, so a malformed value
stops settings from loading and nothing starts: daphne and uvicorn (meshychat/asgi.py runs
django.setup()) and every manage.py command (tests/test_attestation_flag.py runs each). The
two readers, the grant pin gate (apps/grants/config.py) and the skill publish gate
(apps/skillsreg/service.py), both call `enforce_from_settings`, so they cannot disagree again.
Before B09 they did: grants took 1/true/yes/on in any case, skillsreg 1/true/TRUE, neither
refused anything, and `=yes` enforced at the pin but not at publish.

NOT the parser for MESHYCHAT_RUNTIME_VOCAB_ENFORCE: it is read per call as exactly 1/true/TRUE
by apps/skillsreg/runtime.py.

This module imports nothing of the project but apps/core/env.py, which imports nothing of the project
either, because settings.py imports it half loaded.
"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

ENV_VAR = "MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE"
#: The Django setting settings.py stores the parsed value in.
SETTING = "MESHY_MANIFEST_ATTESTATION_ENFORCE"


class AttestationFlagError(ImproperlyConfigured):
    """Invalid flag value. Raised while settings.py loads, so nothing starts."""


def enforce_from_env(env) -> bool:
    """Parse the flag as described in the module docstring. `env` is a mapping of variable
    names to values (os.environ at boot)."""
    names = sorted(name for name in env if name.lower() == ENV_VAR.lower())
    if len(names) > 1:
        raise AttestationFlagError(
            f"env parse failed: duplicate field `{ENV_VAR.lower()}` (set by "
            f"{', '.join(names)}). The server refuses to start with duplicate settings. "
            f"Set {ENV_VAR} once.")
    if not names:
        return False
    name = names[0]
    raw = env[name]
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise AttestationFlagError(
        f"env parse failed: provided string was not `true` or `false` while parsing value "
        f"'{raw}' provided by {name}. The server refuses to start with this value. "
        f"The only values are true and false, lower case; unset is false.")


def enforce_from_settings() -> bool:
    """The ONE reader of the parsed flag, for the grant pin gate and the skill publish gate.
    Read per call, so `override_settings` takes effect without an import-order race.

    A settings module that is not meshychat.settings (a test module that configured Django
    itself) has no such setting; then the environment is parsed here, by the same parser,
    rather than guessing a default. A setting that is not a bool is refused: the string
    "false" is truthy and would read as ON while looking like OFF.
    """
    from django.conf import settings
    if not hasattr(settings, SETTING):
        import os
        return enforce_from_env(os.environ)
    value = getattr(settings, SETTING)
    if not isinstance(value, bool):
        raise AttestationFlagError(
            f"settings.{SETTING} must be a bool, got {value!r}; set {ENV_VAR} in the "
            "environment instead")
    return value


__all__ = ["AttestationFlagError", "ENV_VAR", "SETTING", "enforce_from_env",
           "enforce_from_settings"]
