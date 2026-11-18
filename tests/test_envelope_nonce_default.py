"""The envelope replay ledger is ON unless an operator spells the opt-out (divergence 036).

WHAT THIS CLOSES (STATUS B08). `MESHY_ENVELOPE_NONCE_ENFORCE` was read in two places, each
with its own `getattr(settings, ..., False)`, and `CallsContext` carried a third default,
also False. Nothing in settings.py defined the setting, so the gate was off in every
deployment, and the HTTP test files pinned it False as well: hard-wiring it off survived
every one of their tests. One environment variable, parsed once by settings.py, and one
reader (`nonce_ledger.enforce_from_settings`) that every transport calls replace all three.

The HTTP behaviour with the default in force is proved where the routes are:
tests/test_calls_http.py (section 12) and tests/test_a2a.py (the message:send replay
tests). This file holds what needs no socket: the parser, the setting it feeds (in this
process and in a fresh one that loads settings.py from a chosen environment), the one
reader, and the absence of any other reader or default.

Run:  .venv/Scripts/python.exe -m pytest tests/test_envelope_nonce_default.py -q -p no:cacheprovider
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

django.setup()

from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.test import override_settings  # noqa: E402

from apps.calls import nonce_ledger, pipeline  # noqa: E402

VAR = "MESHYCHAT_ENVELOPE_NONCE_ENFORCE"
SETTING = "MESHY_ENVELOPE_NONCE_ENFORCE"


def test_the_flag_defaults_on_and_only_an_explicit_false_turns_it_off():
    parse = nonce_ledger.enforce_from_env
    assert parse({}) is True
    assert parse({VAR: ""}) is True
    assert parse({VAR: "   "}) is True
    for on in ("1", "true", "TRUE", "yes", "on", " On "):
        assert parse({VAR: on}) is True, on
    # The opt-out, which is also the control that this switch can turn the gate off at
    # all: a parser that always answered True would pass every assertion above.
    for off in ("0", "false", "False", "no", "off", " OFF "):
        assert parse({VAR: off}) is False, off
    # A typo in a DEFAULT-ON security switch must not read as "off" (the rule
    # MESHYCHAT_DPOP_ENFORCE already follows, apps/authapi/config.py): it refuses to load,
    # naming the variable and the spelling that works.
    for typo in ("flase", "2", "disabled", "nope"):
        with pytest.raises(nonce_ledger.NonceConfigError, match=VAR):
            parse({VAR: typo})
    # The Django SETTING's name in the environment is not the switch (the same trap as
    # MESHY_STATE_SECRET, which settings.py's docstring calls out).
    assert parse({SETTING: "false"}) is True


def test_the_setting_is_the_parsed_environment_and_it_is_on_in_this_run():
    assert os.environ.get(VAR) is None, (
        f"{VAR} is set in this run's environment, so every default-in-force test in the "
        f"suite is measuring the operator's choice instead of the default")
    assert getattr(dj_settings, SETTING) is True
    assert getattr(dj_settings, SETTING) is nonce_ledger.enforce_from_env(os.environ)


def _load_settings_with(value: str | None) -> subprocess.CompletedProcess:
    """A FRESH interpreter loading meshychat/settings.py from an environment where only
    the switch differs: the real parse at the real load, which an override in this
    process cannot show."""
    env = {k: v for k, v in os.environ.items() if k != VAR}
    if value is not None:
        env[VAR] = value
    env["DJANGO_SETTINGS_MODULE"] = "meshychat.settings"
    code = ("from django.conf import settings; "
            f"print(repr(settings.{SETTING}))")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_settings_py_parses_the_switch_when_it_loads():
    unset = _load_settings_with(None)
    assert unset.returncode == 0, unset.stderr
    assert unset.stdout.strip() == "True"
    # Control: the same load with the opt-out spelled is OFF, so the True above is the
    # parse and not a constant.
    opted_out = _load_settings_with("false")
    assert opted_out.returncode == 0, opted_out.stderr
    assert opted_out.stdout.strip() == "False"
    # A typo refuses to load at all, naming the variable, rather than choosing for the
    # operator.
    typo = _load_settings_with("flase")
    assert typo.returncode != 0
    assert VAR in typo.stderr and "NonceConfigError" in typo.stderr, typo.stderr[-600:]


def test_the_one_reader_reads_the_setting_and_has_no_default_of_its_own():
    read = nonce_ledger.enforce_from_settings
    assert read() is True
    with override_settings(**{SETTING: False}):
        assert read() is False
    with override_settings(**{SETTING: True}):
        assert read() is True
    # No fallback: settings that lost the name must fail, not silently choose a value
    # (the fallback was False, three times over).
    with override_settings():
        delattr(dj_settings, SETTING)
        with pytest.raises(AttributeError):
            read()
    # Only a real boolean: the STRING "false" is truthy under bool() and would read as
    # ON while looking like OFF to whoever wrote it.
    with override_settings(**{SETTING: "false"}):
        with pytest.raises(ImproperlyConfigured):
            read()


def test_a_calls_context_must_state_the_flag():
    """The third default is gone: a transport that builds a context without saying what
    the gate is fails at construction instead of getting False."""
    with pytest.raises(TypeError):
        pipeline.CallsContext(conn=None, memory_peer_id=uuid.uuid4())
    ctx = pipeline.CallsContext(conn=None, envelope_nonce_enforce=True,
                                memory_peer_id=uuid.uuid4())
    assert ctx.envelope_nonce_enforce is True


def test_nothing_but_the_one_reader_and_settings_names_the_setting():
    """Every other mention of the setting's name in served code is a second reader with
    its own default, which is how the gate came to be off in three places. Positive
    control: the scan finds the two places that SHOULD name it."""
    found = {}
    for top in ("apps", "meshychat"):
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            hits = [n for n, line in enumerate(text.splitlines(), 1)
                    if SETTING in line and not line.lstrip().startswith("#")]
            if hits:
                found[path.relative_to(ROOT).as_posix()] = hits
    assert "meshychat/settings.py" in found and "apps/calls/nonce_ledger.py" in found, (
        f"positive control: the scan did not find the setting and its reader: {found}")
    others = {k: v for k, v in found.items()
              if k not in ("meshychat/settings.py", "apps/calls/nonce_ledger.py")}
    assert others == {}, f"code outside the one reader names {SETTING}: {others}"
