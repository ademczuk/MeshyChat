"""The egress test hatch, MESHY_EGRESS_ALLOW_LOOPBACK, and the deploy check (divergence 041 S8).

WHAT THE HATCH IS. A test or development box can use MESHY_EGRESS_ALLOW_LOOPBACK to let a
peer-controlled URL reach LOOPBACK, so a test can point a
webhook at a local recorder. tests/test_egress.py proves what it opens (loopback, and http
and any port for a loopback destination only) and what it does not.

WHAT THIS FILE HOLDS: the parse, the refusal to load a PRODUCTION process with it on, and
the one reader. "Production" is a process whose DEBUG is off: meshychat/settings.py is the
only settings module, DEBUG defaults off there, and nothing else in the repository marks a
deployment. So settings.py refuses to load with the hatch on and DEBUG off, and the reader
answers False unless DEBUG is on as well, so an override that sets only the hatch opens
nothing either.

Run:  .venv/Scripts/python.exe -m pytest tests/test_egress_hatch.py -q -p no:cacheprovider
"""
from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
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

VAR = "MESHY_EGRESS_ALLOW_LOOPBACK"


@pytest.fixture(scope="module")
def eg():
    return importlib.import_module("apps.core.egress")


def test_the_parse(eg):
    parse = eg.allow_loopback_from_env
    for debug in (False, True):
        assert parse({}, debug=debug) is False
        assert parse({VAR: ""}, debug=debug) is False
        assert parse({VAR: "  "}, debug=debug) is False
        for off in ("0", "false", "False", "no", "off", " OFF "):
            assert parse({VAR: off}, debug=debug) is False, off
    for on in ("1", "true", "TRUE", "yes", "on", " On "):
        assert parse({VAR: on}, debug=True) is True, on
        # The deploy check: a production process (DEBUG off) refuses the hatch.
        with pytest.raises(eg.EgressConfigError, match=VAR):
            parse({VAR: on}, debug=False)
    # A typo refuses rather than choosing, with or without DEBUG.
    for typo in ("2", "tru", "enabled"):
        for debug in (False, True):
            with pytest.raises(eg.EgressConfigError, match=VAR):
                parse({VAR: typo}, debug=debug)
    assert issubclass(eg.EgressConfigError, ImproperlyConfigured)


def test_the_setting_in_this_run_is_the_parsed_environment_and_off(eg):
    assert os.environ.get(VAR) is None, f"{VAR} is set in this run's environment"
    assert getattr(dj_settings, VAR) is False
    assert eg.loopback_allowed() is False


def _boot(env_changes: dict) -> subprocess.CompletedProcess:
    """A FRESH interpreter booting the served app (meshychat.asgi runs django.setup(), as
    daphne and uvicorn do) from an environment where only these variables differ."""
    env = {k: v for k, v in os.environ.items()
           if k not in (VAR, "MESHY_DEBUG")}
    env.update(env_changes)
    env["DJANGO_SETTINGS_MODULE"] = "meshychat.settings"
    code = ("import meshychat.asgi; from django.conf import settings; "
            f"print('HATCH', repr(settings.{VAR}), repr(settings.DEBUG))")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


def _line(proc) -> str:
    return next((ln for ln in proc.stdout.splitlines() if ln.startswith("HATCH ")), "")


def test_a_production_process_refuses_to_boot_with_the_hatch_on():
    refused = _boot({VAR: "1"})
    assert refused.returncode != 0, refused.stdout[-400:]
    assert VAR in refused.stderr and "EgressConfigError" in refused.stderr, refused.stderr[-800:]
    assert _line(refused) == ""
    # Control: the same boot with DEBUG on loads, with the hatch on.
    dev = _boot({VAR: "1", "MESHY_DEBUG": "1"})
    assert dev.returncode == 0, dev.stderr[-800:]
    assert _line(dev) == "HATCH True True"
    # Control: unset, a production process loads with the hatch off.
    unset = _boot({})
    assert unset.returncode == 0, unset.stderr[-800:]
    assert _line(unset) == "HATCH False False"
    typo = _boot({VAR: "tru", "MESHY_DEBUG": "1"})
    assert typo.returncode != 0 and VAR in typo.stderr, typo.stderr[-800:]


def test_the_one_reader_needs_the_setting_and_debug(eg):
    read = eg.loopback_allowed
    with override_settings(**{VAR: True, "DEBUG": True}):
        assert read() is True
    with override_settings(**{VAR: True, "DEBUG": False}):
        assert read() is False
    with override_settings(**{VAR: False, "DEBUG": True}):
        assert read() is False
    # Only a real boolean: the string "false" is truthy.
    with override_settings(**{VAR: "false", "DEBUG": True}):
        with pytest.raises(ImproperlyConfigured):
            read()
    # No default of its own: settings that lost the name fail.
    with override_settings():
        delattr(dj_settings, VAR)
        with pytest.raises(AttributeError):
            read()


def _mentions(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and node.value == VAR:
            return True
        if isinstance(node, ast.Attribute) and node.attr == VAR:
            return True
        if isinstance(node, ast.Name) and node.id == VAR:
            return True
    return False


def test_nothing_but_the_parse_the_setting_and_the_reader_names_the_hatch():
    """A second reader would carry a default of its own (divergence 036's lesson). Docstrings
    do not count: they are string constants that CONTAIN the name. Positive control: the two
    files that should name it are found."""
    found = set()
    for top in ("apps", "meshychat"):
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" not in path.parts and _mentions(path):
                found.add(path.relative_to(ROOT).as_posix())
    assert found == {"apps/core/egress.py", "meshychat/settings.py"}, found
