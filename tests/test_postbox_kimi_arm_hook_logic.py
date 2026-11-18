"""kimi_arm_hook.py logic that needs no Windows lock: the headless binding and --resolve (pxx eac28e).

test_postbox_kimi_arm_hook.py skips whole off Windows (it takes real msvcrt locks), so these load the hook with a
stand-in msvcrt and test only what never touches a lock, so a macOS run has evidence too.

Run:  python -m pytest tests/test_postbox_kimi_arm_hook_logic.py -q -p no:cacheprovider
"""
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

PB = Path(__file__).resolve().parent.parent / "tools" / "postbox"


@pytest.fixture
def hook(monkeypatch, tmp_path):
    if "msvcrt" not in sys.modules:
        monkeypatch.setitem(sys.modules, "msvcrt", types.ModuleType("msvcrt"))
    spec = importlib.util.spec_from_file_location("kimi_arm_hook_logic", PB / "kimi_arm_hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "HERE", str(tmp_path))
    logged = []
    monkeypatch.setattr(mod, "log_line", lambda sid, cwd, entry: logged.append(entry))
    mod.logged = logged
    (tmp_path / "kimi-seats.json").write_text(json.dumps({"s1": "nxxxxx-prod"}), encoding="utf-8")
    return mod


def run(hook, monkeypatch, sid):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": sid, "cwd": "/nowhere"})))
    monkeypatch.setattr(hook, "lock_held", lambda path: False)          # no postbox: the hook would return 0
    return hook.main()


def test_headless_exact_session_ends_the_turn_before_any_seat_lookup(hook, monkeypatch):
    monkeypatch.setenv("MESHY_KIMI_HEADLESS", "s1")
    assert run(hook, monkeypatch, "s1") == 0 and hook.logged == ["headless"]


@pytest.mark.parametrize("value", ["", "1", "s2", "S1", " s1"])
def test_any_other_value_goes_on_to_the_seat(hook, monkeypatch, value):
    monkeypatch.setenv("MESHY_KIMI_HEADLESS", value)
    run(hook, monkeypatch, "s1")
    assert hook.logged == ["nxxxxx-prod"], value                      # the normal path, not the headless exit


def test_resolve_prod_test_none_and_unreadable(hook, tmp_path):
    prod, test = tmp_path / "p", tmp_path / "t"
    (prod / "sub").mkdir(parents=True)
    test.mkdir()
    (tmp_path / "kimi-seats.json").write_text(json.dumps({"cwd:" + str(prod): "nxxxxx-prod:kimitest",
                                                          "cwd:" + str(test): "kimi-delivery-test"}),
                                              encoding="utf-8")
    for cwd, rc in ((prod, 3), (prod / "sub", 3), (test, 0), (tmp_path / "elsewhere", 0)):
        assert hook.resolve_cli([str(cwd)], here=str(tmp_path), out=io.StringIO()) == rc, cwd
    assert hook.resolve_cli(["--here", str(tmp_path / "nomap"), str(prod)], out=io.StringIO()) == 2
