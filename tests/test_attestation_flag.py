"""`MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE`: one parser and a refusal to boot (B09).

Settings parse the flag once at boot. Only "true" and "false", exactly, are accepted; unset
is false; any other value, including "1", "yes", "TRUE", and the empty string, is refused with
an `ImproperlyConfigured` error and the server does not start. Before B09, apps/grants/config.py accepted
1/true/yes/on in any case, apps/skillsreg/service.py accepted 1/true/TRUE, and neither refused
anything, so `=yes` enforced at the pin and not at publish.

Now settings.py parses it once (`apps.core.attestation_flag.enforce_from_env`) and both
readers read that one setting. MESHYCHAT_RUNTIME_VOCAB_ENFORCE deliberately uses a separate
per-call parser that accepts exactly 1/true/TRUE; tests/test_skills_registry.py pins it.

The boot tests run the production entrypoints as subprocesses: `import meshychat.asgi` (what
daphne and uvicorn load), daphne itself, and two manage.py commands (a core one and one of this
project's), because a refusal that only a unit test sees is not a refusal to boot.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.test import override_settings  # noqa: E402

from apps.grants import config as gconfig  # noqa: E402
from apps.skillsreg import service as sservice  # noqa: E402

ENV = "MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE"


def _flag():
    """Imported per test, so the subprocess tests below run (and fail RED) on a tree where the
    module does not exist yet, instead of the whole file failing to collect."""
    from apps.core import attestation_flag
    return attestation_flag


# ------------------------------------------------------------------------- the parser

def test_the_parser_takes_exactly_true_and_false_and_unset_is_false():
    flag = _flag()
    assert flag.enforce_from_env({}) is False
    assert flag.enforce_from_env({ENV: "true"}) is True
    assert flag.enforce_from_env({ENV: "false"}) is False
    for raw in ("1", "0", "yes", "no", "on", "off", "TRUE", "True", "FALSE", "",
                " true", "true ", "true\n", "tru"):
        with pytest.raises(flag.AttestationFlagError) as exc:
            flag.enforce_from_env({ENV: raw})
        assert isinstance(exc.value, ImproperlyConfigured), raw
        # The exact wording is pinned so the refusal text operators see stays stable.
        assert (f"provided string was not `true` or `false` while parsing value '{raw}' "
                f"provided by {ENV}") in str(exc.value), raw
    # An unrelated variable is not the flag (the control that the refusals above are about
    # the value, not about the dictionary having a key).
    assert flag.enforce_from_env({"MESHY_MANIFEST_PIN_ENFORCE": "yes"}) is False


def test_the_name_is_matched_case_insensitively_and_two_spellings_refuse():
    """The parser lowercases each variable name before matching it to the field, so on a
    platform with case-sensitive environments a lower-case spelling is the same field, and
    two spellings are a duplicate-field error: the server does not start."""
    flag = _flag()
    assert flag.enforce_from_env({ENV.lower(): "true"}) is True
    with pytest.raises(flag.AttestationFlagError, match="duplicate field"):
        flag.enforce_from_env({ENV: "false", ENV.lower(): "false"})


# ------------------------------------------------------------------------ the readers

def test_both_readers_read_the_one_setting_parsed_at_boot(monkeypatch):
    """The grants pin gate and the skills publish gate answer from the same setting, through
    the same function; the environment after boot is not consulted."""
    flag = _flag()
    for value in (True, False):
        with override_settings(MESHY_MANIFEST_ATTESTATION_ENFORCE=value):
            assert gconfig.manifest_attestation_enforce() is value
            assert sservice.manifest_attestation_enforce() is value
    # The environment says on and the setting says off: off. The flag is parsed once at boot.
    monkeypatch.setenv(ENV, "true")
    with override_settings(MESHY_MANIFEST_ATTESTATION_ENFORCE=False):
        assert gconfig.manifest_attestation_enforce() is False
        assert sservice.manifest_attestation_enforce() is False
    # ONE function: replacing it changes both readers.
    sentinel = object()
    monkeypatch.setattr(flag, "enforce_from_settings", lambda: sentinel)
    assert gconfig.manifest_attestation_enforce() is sentinel
    assert sservice.manifest_attestation_enforce() is sentinel


def test_a_setting_that_is_not_a_bool_is_refused_rather_than_read_as_truthy():
    flag = _flag()
    with override_settings(MESHY_MANIFEST_ATTESTATION_ENFORCE="false"):
        with pytest.raises(flag.AttestationFlagError):
            gconfig.manifest_attestation_enforce()
    # CONTROL: the bool is read.
    with override_settings(MESHY_MANIFEST_ATTESTATION_ENFORCE=False):
        assert gconfig.manifest_attestation_enforce() is False


def test_settings_py_parses_the_flag_with_the_shared_parser():
    tree = ast.parse((ROOT / "meshychat" / "settings.py").read_text(encoding="utf-8"))
    targets = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "MESHY_MANIFEST_ATTESTATION_ENFORCE"
                       for t in n.targets)]
    assert len(targets) == 1, "settings.py must assign the setting exactly once"
    call = targets[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name), ast.dump(call)
    assert call.func.id == "_attestation_from_env", ast.dump(call)
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and n.module == "apps.core.attestation_flag"]
    assert any(a.name == "enforce_from_env" and a.asname == "_attestation_from_env"
               for n in imports for a in n.names)


# -------------------------------------------------------------------- refusing to boot

def _env(value) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_") and k.upper() != ENV}
    env["DJANGO_SETTINGS_MODULE"] = "meshychat.settings"
    if value is not None:
        env[ENV] = value
    return env


_PROBE = ("import meshychat.asgi; from django.conf import settings; "
          "print('ATTEST', repr(settings.MESHY_MANIFEST_ATTESTATION_ENFORCE))")


def _run(cmd, value, timeout=120):
    return subprocess.run(cmd, env=_env(value), cwd=str(ROOT), capture_output=True,
                          text=True, timeout=timeout)


def _refused(proc, raw):
    assert proc.returncode != 0, (
        f"started with {ENV}={raw!r}\nstdout:\n{proc.stdout[-800:]}\n"
        f"stderr:\n{proc.stderr[-800:]}")
    assert "AttestationFlagError" in proc.stderr, proc.stderr[-2500:]
    assert f"while parsing value '{raw}' provided by {ENV}" in proc.stderr, proc.stderr[-2500:]


@pytest.mark.parametrize("raw", ["yes", "1", "TRUE"])
def test_a_malformed_value_stops_the_served_app_from_loading(raw):
    _refused(_run([sys.executable, "-c", _PROBE], raw), raw)


def test_the_served_app_loads_with_each_legal_value():
    """CONTROLS for the refusals: the same probe, the same environment, a legal value."""
    for value, expected in ((None, "False"), ("false", "False"), ("true", "True")):
        proc = _run([sys.executable, "-c", _PROBE], value)
        assert proc.returncode == 0 and f"ATTEST {expected}" in proc.stdout, (
            value, proc.returncode, proc.stdout[-800:], proc.stderr[-1500:])


def test_manage_py_commands_refuse_to_start_and_start_with_a_legal_value():
    """Django's ManagementUtility swallows the first ImproperlyConfigured from settings and
    re-raises it on the next access, for a core command and for this project's own."""
    _refused(_run([sys.executable, "manage.py", "check"], "1"), "1")
    _refused(_run([sys.executable, "manage.py", "run_jobs", "--explain"], "yes"), "yes")
    # CONTROL: the same core command with a legal value.
    ok = _run([sys.executable, "manage.py", "check"], "false")
    assert ok.returncode == 0, (ok.stdout[-800:], ok.stderr[-1500:])


def test_daphne_refuses_to_start():
    """The production server command (README "Run it"). A daphne that started would serve
    until the timeout, so a pass here is the process exiting on its own with the refusal."""
    proc = _run([sys.executable, "-m", "daphne", "-b", "127.0.0.1", "-p", "0",
                 "meshychat.asgi:application"], "TRUE", timeout=90)
    _refused(proc, "TRUE")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
