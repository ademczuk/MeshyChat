"""T11: the capability root key is a FILE, read once at startup, and a broken one stops the
process (B07 slice 4; docs/design/biscuit-capabilities.md 2.2, Q10; divergence 040).

THE CONTRACT, each pinned below:

  * `MESHY_CAPABILITIES` is parsed once, by settings.py, as strictly as MESHYCHAT_DPOP_ENFORCE:
    unset or blank is OFF, the same true and false spellings, anything else refuses to load.
  * OFF: `biscuit_auth` is never imported, and none of the key variables is parsed, so a
    3.14 deploy and a half-configured one start exactly as before.
  * ON: the key store (apps/memoryskills/capability_keys.py) is loaded ONCE by
    apps/memoryskills/apps.py ready(), beside the 034 roster loader. The process refuses to
    start (ImproperlyConfigured) on a missing or invalid key file, on a verify set
    (`MESHY_CAPABILITY_ROOT_PUBKEYS`, `kid=hex;kid=hex`) that lacks the active kid or holds
    another key under it, and on an `import biscuit_auth` that fails. That stops daphne AND
    every manage.py command, so the refusal names the runbook line: unset MESHY_CAPABILITIES
    to run commands while the key is repaired.
  * Key material is read from the file only: never from an environment variable, including
    MESHYCHAT_BISCUIT_ROOT_SK, which makes issue_grant refuse every grant
    (tests/test_grants.py:1330-1336). Hex is required by the key parser: divergence (d) of
    the design's 5.3.
  * A dev-only ephemeral key only behind MESHY_CAPABILITY_DEV_EPHEMERAL, and that flag is
    READ only under DEBUG.
  * The key file is created exclusively (O_EXCL, owner-only mode from the first byte:
    23ca808's rule) by a command that runs without Django, because ready() refuses when
    the flag is on and no key exists.

The startup tests boot the served app in a subprocess (tests/fixtures/
capability_startup_child.py imports meshychat.asgi, as daphne does). Every refusal sits
beside the control that differs from it in the one variable under test.

Run:  pytest tests/test_capability_keys.py -q
Needs no database.
"""
from __future__ import annotations

import ast
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CHILD = ROOT / "tests" / "fixtures" / "capability_startup_child.py"

FLAG = "MESHY_CAPABILITIES"
KEY_PATH = "MESHY_CAPABILITY_ROOT_KEY_PATH"
KID = "MESHY_CAPABILITY_ROOT_KID"
PUBKEYS = "MESHY_CAPABILITY_ROOT_PUBKEYS"
DEV = "MESHY_CAPABILITY_DEV_EPHEMERAL"
LEGACY_ROOT_KEY_ENV = "MESHYCHAT_BISCUIT_ROOT_SK"
RUNBOOK = "unset MESHY_CAPABILITIES"

#: RFC 8032 test vector 1: a fixed private key and its public half, so a mismatch is a fact
#: of the fixture and not of a random draw.
SK1 = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
PK1 = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
#: RFC 8032 test vector 2.
SK2 = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
PK2 = "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
#: A small-order point (apps/crypto/primitives.py `_SMALL_ORDER`): never a verify key.
SMALL_ORDER = "0100000000000000000000000000000000000000000000000000000000000000"


def _keys():
    """Imported per test, so a tree without the module fails each test by name."""
    from apps.memoryskills import capability_keys
    return capability_keys


@pytest.fixture()
def key_file(tmp_path):
    path = tmp_path / "root.key"
    path.write_text(SK1 + "\n", encoding="ascii")
    return path


def _by_why(cases):
    """parametrize's cases, each id its `why`: no key material in a test id."""
    return [pytest.param(*case, id=case[-1]) for case in cases]


def _load(k, *, enabled=True, key_path=None, kid="7", pubkeys=f"7={PK1}", debug=False,
          env=None):
    return k.load(enabled=enabled, key_path=None if key_path is None else str(key_path),
                  kid=kid, pubkeys=pubkeys, debug=debug, env={} if env is None else env)


# ============================================================================ the pin

def test_requirements_pin_biscuit_python_exactly_and_that_is_what_runs():
    """Its first import is this slice's. An exact pin, not a lower bound: the verifier's
    behaviour (strict third-party signatures, the default limits) was read in THIS version's
    crate (biscuit-auth 6.0.0), and 0.4.0 has no cp314 wheel, so from here a 3.14 install
    fails at `pip install -r requirements.txt`: the pin of divergence 040 in force."""
    lines = [ln for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if ln.lower().startswith("biscuit")]
    assert len(lines) == 1, lines
    spec, _, comment = lines[0].partition("#")
    assert spec.split() == ["biscuit-python==0.4.0"], lines[0]
    assert "proven on" in comment, lines[0]
    from importlib.metadata import version
    assert version("biscuit-python") == "0.4.0"


# ============================================================================ the flag

def test_the_flag_is_off_unless_spelled_on_and_a_malformed_value_refuses():
    """The vocabulary is MESHYCHAT_DPOP_ENFORCE's (apps/authapi/config.py), the default is OFF."""
    k = _keys()
    from apps.authapi import config as authconfig

    assert k.FLAG_TRUE == authconfig._FLAG_TRUE and k.FLAG_FALSE == authconfig._FLAG_FALSE
    assert k.enabled_from_env({}) is False
    for raw in ("", "   "):
        assert k.enabled_from_env({FLAG: raw}) is False, raw
    for raw in ("1", "true", "yes", "on", " ON ", "True"):
        assert k.enabled_from_env({FLAG: raw}) is True, raw
    for raw in ("0", "false", "no", "off", "Off"):
        assert k.enabled_from_env({FLAG: raw}) is False, raw
    for raw in ("maybe", "enabled", "2", "on,off", "tru"):
        with pytest.raises(k.CapabilityConfigError, match=FLAG):
            k.enabled_from_env({FLAG: raw})


def test_parse_flag_is_the_one_strict_parse_behind_the_three_switches():
    """`capability_flag.parse_flag` replaced the parse that MESHY_CAPABILITIES, the mesh
    unversioned switch (apps/core/mesh_unversioned.py) and the egress hatch
    (apps/core/egress.py) each spelled out. Its contract: unset or blank is False, a
    FLAG_TRUE or FLAG_FALSE spelling (stripped, any case) is that bool, anything else is
    None, so each caller refuses with its own error class and its own message. And the three
    callers agree on every value: the same bool, or each its own refusal naming its own
    variable, so the shared parse changed none of their verdicts."""
    from apps.core import capability_flag, egress, mesh_unversioned

    parse = capability_flag.parse_flag
    assert parse(None) is False
    for raw in ("", "   ", "\t"):
        assert parse(raw) is False, repr(raw)
    for raw in ("1", "true", "yes", "on", " ON ", "True"):
        assert parse(raw) is True, raw
    for raw in ("0", "false", "no", "off", "Off", " OFF "):
        assert parse(raw) is False, raw
    for raw in ("maybe", "enabled", "2", "on,off", "tru", "yes please"):
        assert parse(raw) is None, raw

    callers = [
        (FLAG, capability_flag.enabled_from_env, capability_flag.CapabilityConfigError),
        (mesh_unversioned.ENV_VAR, mesh_unversioned.accept_from_env,
         mesh_unversioned.UnversionedConfigError),
        (egress.ENV_VAR, lambda env: egress.allow_loopback_from_env(env, debug=True),
         egress.EgressConfigError),
    ]
    for raw in (None, "", "  ", "1", " On ", "TRUE", "off", "0", " no ", "maybe", "2", "tru"):
        expected = parse(raw)
        for var, read, error in callers:
            env = {} if raw is None else {var: raw}
            if expected is None:
                with pytest.raises(error, match=var):
                    read(env)
            else:
                assert read(env) is expected, (var, raw)


def test_settings_parse_the_flag_once_and_a_malformed_value_refuses_to_load():
    """settings.py holds a bool, parsed by `enabled_from_env`; a malformed value stops the
    settings module from loading at all. Control: the same child with the flag unset loads."""
    tree = ast.parse((ROOT / "meshychat" / "settings.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and "capabilit" in n.func.id.lower()]
    assert calls, "settings.py does not call the capability flag's parser"

    code = "import meshychat.settings as s; print('FLAG', repr(s.MESHY_CAPABILITIES))"
    env = _child_env({})
    ok = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                        capture_output=True, text=True, timeout=120)
    assert ok.returncode == 0 and "FLAG False" in ok.stdout, (ok.stdout, ok.stderr[-1500:])
    bad = subprocess.run([sys.executable, "-c", code], env={**env, FLAG: "maybe"},
                         cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert bad.returncode != 0 and "CapabilityConfigError" in bad.stderr, (
        bad.returncode, bad.stdout, bad.stderr[-1500:])


def test_loading_settings_imports_no_memory_skills_module():
    """Review F6. settings.py parses the flag while it loads; importing the parser from
    apps.memoryskills ran that package's __init__, which imports handlers, permissions,
    validation and psycopg while settings is half loaded, so any module-level settings read
    in that chain would become a circular import at every startup. The parser lives outside
    the package. POSITIVE CONTROL: the same probe sees a memory-skills module that IS
    imported."""
    probe = ("import sys, meshychat.settings{extra}; "
             "print('LOADED', sorted(m for m in sys.modules "
             "if m == 'apps.memoryskills' or m.startswith('apps.memoryskills.')))")
    env = _child_env({})
    got = subprocess.run([sys.executable, "-c", probe.format(extra="")], env=env,
                         cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert got.returncode == 0, got.stderr[-1500:]
    assert "LOADED []" in got.stdout, got.stdout
    control = subprocess.run(
        [sys.executable, "-c", probe.format(extra=", apps.memoryskills.errors")], env=env,
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert control.returncode == 0 and "'apps.memoryskills.errors'" in control.stdout, (
        control.stdout, control.stderr[-1500:])


# ============================================================================ the store

def test_a_valid_key_file_loads_with_its_verify_set(key_file):
    k = _keys()
    store = _load(k, key_path=key_file, pubkeys=f"7={PK1};9={PK2}")
    assert store.active_kid == 7 and sorted(store.kids) == [7, 9]
    assert store.public_key_bytes(7).hex() == PK1 and store.public_key_bytes(9).hex() == PK2
    # The provider biscuit calls with the token's kid: a known kid is its key, an unknown
    # kid and a token with none are refused.
    assert bytes(store.provider(9).to_bytes()).hex() == PK2
    for bad in (8, None, -1):
        with pytest.raises(Exception):
            store.provider(bad)
    # The private half never appears in a repr.
    assert SK1 not in repr(store) and SK1[:16] not in repr(store)


def test_a_key_file_may_end_in_one_newline_and_nothing_else(tmp_path):
    k = _keys()
    for text in (SK1, SK1 + "\n", SK1 + "\r\n", SK1.upper() + "\n"):
        p = tmp_path / "k"
        p.write_bytes(text.encode("ascii"))
        assert _load(k, key_path=p).active_kid == 7, repr(text)
        p.unlink()


@pytest.mark.parametrize("content,why", _by_why([
    (b"", "empty"),
    (SK1[:-2].encode(), "31 bytes"),
    ((SK1 + "00").encode(), "33 bytes"),
    (("zz" + SK1[2:]).encode(), "not hex"),
    (b"\xef\xbb\xbf" + SK1.encode(), "a byte order mark"),
    ((SK1 + "\n\n").encode(), "two newlines"),
    ((" " + SK1).encode(), "leading space"),
    (('{"sk": "' + SK1 + '"}').encode(), "JSON"),
]))
def test_a_key_file_that_is_not_one_hex_key_is_refused(tmp_path, content, why):
    k = _keys()
    p = tmp_path / "root.key"
    p.write_bytes(content)
    with pytest.raises(k.CapabilityKeyRefused, match=KEY_PATH) as err:
        _load(k, key_path=p)
    # No run of the key is repeated in the refusal.
    assert SK1[2:18] not in str(err.value).lower(), why


def test_a_missing_or_unset_key_file_is_refused(tmp_path):
    k = _keys()
    with pytest.raises(k.CapabilityKeyRefused, match=f"{KEY_PATH} is not set"):
        _load(k, key_path=None)
    with pytest.raises(k.CapabilityKeyRefused, match=KEY_PATH):
        _load(k, key_path=tmp_path / "absent.key")
    with pytest.raises(k.CapabilityKeyRefused, match=KEY_PATH):
        _load(k, key_path=tmp_path)                          # a directory, not a file


def test_key_material_in_the_path_variable_is_refused_and_not_repeated():
    """Key material is never accepted from the environment. A private key pasted into the
    PATH variable is refused as such, and the refusal (which reaches a log) does not repeat
    it."""
    k = _keys()
    for pasted in (SK1, SK1.upper(), f"  {SK1}  "):
        with pytest.raises(k.CapabilityKeyRefused, match="not a path") as err:
            _load(k, key_path=pasted)
        assert SK1[:16] not in str(err.value).lower(), str(err.value)


#: Review F1: the key pasted into the PATH variable in encodings other than 64 hex digits. The
#: 64-hex check above names only that one form, so each of these fell through to the read of
#: a file that does not exist, whose refusal repeated the value. Base64 is the encoding operators
#: are most likely to paste; `ed25519-private/<hex>` is biscuit's own rendering.
_SK1_RAW = bytes.fromhex(SK1)
PASTED = _by_why([
    (base64.b64encode(_SK1_RAW).decode("ascii"), "base64"),
    (base64.urlsafe_b64encode(_SK1_RAW).decode("ascii").rstrip("="), "base64url"),
    (f"ed25519-private/{SK1}", "biscuit's own form"),
    (f"0x{SK1}", "0x and hex"),
    (SK1 + PK1, "128 hex, seed and public"),
    (f"{SK1[:32]} {SK1[32:]}", "hex with a space"),
])


def _runs(value: str, n: int = 8) -> set[str]:
    """Every run of `n` characters of `value`, lowercased: what a refusal must not hold. Eight, not
    T14's sixteen: 32 bits of a key is already a leak."""
    value = value.lower()
    return {value[i:i + n] for i in range(max(1, len(value) - n + 1))}


def _repeated(value: str, text: str) -> list[str]:
    return sorted(run for run in _runs(value) if run in text.lower())


@pytest.mark.parametrize("pasted,why", PASTED)
def test_a_path_variable_naming_no_readable_file_is_refused_without_repeating_it(pasted, why):
    """Review F1. Whatever the variable holds, a refusal never repeats it, nor any 8
    characters of it: the refusal reaches stderr through ready() and every manage.py
    command. POSITIVE CONTROL: the scan finds the value in a text that holds it."""
    k = _keys()
    with pytest.raises(k.CapabilityKeyRefused, match=KEY_PATH) as err:
        _load(k, key_path=pasted)
    assert not _repeated(pasted, str(err.value)), (why, _repeated(pasted, str(err.value))[:3])
    assert _repeated(pasted, f"x {pasted!r} y"), "control: the scan finds a planted value"


def test_a_refusal_never_repeats_the_path_variable_even_when_the_file_exists(tmp_path):
    """One rule, not a guess about which values are keys: the malformed-file refusal names
    the variable and what it read, never the value. The file's content is never repeated
    either (the older test above holds that)."""
    k = _keys()
    p = tmp_path / "a-distinctive-key-file-name.key"
    p.write_bytes(b"not-a-key\n")
    with pytest.raises(k.CapabilityKeyRefused, match=KEY_PATH) as err:
        _load(k, key_path=p)
    assert "a-distinctive-key-file-name" not in str(err.value), str(err.value)
    assert "read 10 bytes" in str(err.value), str(err.value)


@pytest.mark.parametrize("kid,why", _by_why([
    (None, "unset"), ("", "blank"), ("seven", "not an integer"), ("-1", "negative"),
    ("4294967296", "past u32"), ("7.0", "a float"), ("0x7", "hex"), ("+7", "signed"),
]))
def test_a_kid_that_is_not_a_u32_integer_is_refused(key_file, kid, why):
    k = _keys()
    with pytest.raises(k.CapabilityKeyRefused, match=KID):
        _load(k, key_path=key_file, kid=kid)


@pytest.mark.parametrize("pubkeys,why", _by_why([
    (None, "unset"),
    ("", "blank"),
    (f"9={PK2}", "lacks the active kid"),
    (f"7={PK2}", "another key under the active kid"),
    (f"7:{PK1}", "no ="),
    (f"7={PK1};7={PK1}", "a kid twice"),
    (f"7={PK1};;9={PK2}", "an empty entry"),
    (f"7={PK1[:-2]}", "31 bytes"),
    (f"7={PK1};9=zz{PK2[2:]}", "not hex"),
    (f"7={PK1};x={PK2}", "a kid that is not an integer"),
    (f"7={PK1};9={SMALL_ORDER}", "a small-order point"),
]))
def test_a_verify_set_that_is_malformed_or_lacks_the_active_key_is_refused(key_file, pubkeys,
                                                                          why):
    k = _keys()
    with pytest.raises(k.CapabilityKeyRefused, match=PUBKEYS):
        _load(k, key_path=key_file, pubkeys=pubkeys)
    # CONTROL: the same file with the matching verify set loads.
    assert _load(k, key_path=key_file).active_kid == 7


def test_a_failed_biscuit_import_is_refused_in_process(key_file, monkeypatch):
    k = _keys()
    monkeypatch.setitem(sys.modules, "biscuit_auth", None)
    with pytest.raises(k.CapabilityKeyRefused, match="biscuit_auth"):
        _load(k, key_path=key_file)


def test_off_loads_nothing_and_parses_nothing(tmp_path):
    """With the flag off even garbage in every key variable is inert: the store is None."""
    k = _keys()
    assert k.load(enabled=False, key_path=str(tmp_path / "absent"), kid="seven",
                  pubkeys="garbage", debug=False, env={}) is None


# ============================================================================ the dev key

class RecordingEnv(dict):
    """An environment mapping that writes down every name read from it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reads: list[str] = []

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.reads.append(key)
        return super().__contains__(key)


def test_the_dev_ephemeral_flag_is_read_only_under_debug():
    """Outside DEBUG the flag's name is never read, so setting it cannot turn anything on;
    the load refuses as for any missing key. Under DEBUG it gives an ephemeral key whose
    public half is the verify set's entry for its kid (the positive control)."""
    k = _keys()
    env = RecordingEnv({DEV: "1"})
    with pytest.raises(k.CapabilityKeyRefused, match=KEY_PATH):
        k.load(enabled=True, key_path=None, kid=None, pubkeys=None, debug=False, env=env)
    assert DEV not in env.reads, f"the dev flag was read outside DEBUG: {env.reads}"

    env = RecordingEnv({DEV: "1"})
    store = k.load(enabled=True, key_path=None, kid=None, pubkeys=None, debug=True, env=env)
    assert DEV in env.reads
    assert store is not None and store.active_kid in store.kids
    assert store.public_key_bytes(store.active_kid) == k.public_of(store._private_bytes())

    # Under DEBUG with the flag off, unset or malformed: still refused.
    for value in ({}, {DEV: "0"}, {DEV: "maybe"}):
        with pytest.raises(k.CapabilityKeyRefused):
            k.load(enabled=True, key_path=None, kid=None, pubkeys=None, debug=True,
                   env=RecordingEnv(value))


def _docstring_ids(tree) -> set[int]:
    return {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)}


def test_only_the_key_store_names_the_dev_flag():
    """A second reader could read it outside DEBUG. AST string constants under apps/ and
    meshychat/, docstrings excluded (settings.py's table may describe it)."""
    found = []
    for sub in ("apps", "meshychat"):
        for p in (ROOT / sub).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            docs = _docstring_ids(tree)
            for n in ast.walk(tree):
                if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and DEV in n.value and id(n) not in docs):
                    found.append(str(p.relative_to(ROOT)).replace("\\", "/"))
    assert sorted(set(found)) == ["apps/memoryskills/capability_keys.py"], found


# ============================================================================ never from env

def test_key_material_in_any_environment_variable_is_never_used(key_file):
    """The store's key is the file's, whatever other key an environment variable holds, and
    the key variables are not read."""
    k = _keys()
    env = RecordingEnv({LEGACY_ROOT_KEY_ENV: SK2, "MESHY_CAPABILITY_ROOT_SK": SK2,
                        "MESHY_CAPABILITY_ROOT_KEY": SK2, DEV: "1"})
    store = k.load(enabled=True, key_path=str(key_file), kid="7", pubkeys=f"7={PK1}",
                   debug=True, env=env)
    assert store.public_key_bytes(7).hex() == PK1
    assert k.public_of(store._private_bytes()).hex() == PK1
    assert not [n for n in env.reads if n in (LEGACY_ROOT_KEY_ENV, "MESHY_CAPABILITY_ROOT_SK",
                                              "MESHY_CAPABILITY_ROOT_KEY")], env.reads


def test_the_capability_code_never_names_legacy_root_key_variable():
    """Outside docstrings and comments, no capability module holds the string."""
    for rel in ("apps/memoryskills/capability_keys.py", "apps/memoryskills/apps.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        bad = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
               and isinstance(n.value, str) and LEGACY_ROOT_KEY_ENV in n.value and id(n) not in docs]
        assert not bad, (rel, bad)


def test_setting_the_capability_variables_never_makes_issue_grant_refuse(monkeypatch,
                                                                         key_file):
    """issue_grant refuses when MESHYCHAT_BISCUIT_ROOT_SK is set (apps/grants/config.py
    `biscuit_minting_enabled`); the capability variables must not trip it."""
    from apps.grants import config as gconfig

    for name, value in ((FLAG, "on"), (KEY_PATH, str(key_file)), (KID, "7"),
                        (PUBKEYS, f"7={PK1}"), (DEV, "1")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(LEGACY_ROOT_KEY_ENV, raising=False)
    assert gconfig.biscuit_minting_enabled() is False
    # CONTROL: MESHYCHAT_BISCUIT_ROOT_SK still switches it, so the check reads what it should.
    monkeypatch.setenv(LEGACY_ROOT_KEY_ENV, SK1)
    assert gconfig.biscuit_minting_enabled() is True


# ============================================================================ startup

def _child_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_") and not k.startswith("MESHY_CAPABILIT")
           and k not in (LEGACY_ROOT_KEY_ENV, "MESHY_DEBUG", "CAPABILITY_CHILD_RECORD",
                         "CAPABILITY_CHILD_BREAK_BISCUIT")}
    env.update({"DJANGO_SETTINGS_MODULE": "meshychat.settings", **extra})
    return env


def _boot(extra: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHILD)], env=_child_env(extra), cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180)


def _result(proc) -> dict:
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("CAPABILITY_RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (
        f"child exit {proc.returncode}\nstdout:\n{proc.stdout[-1500:]}\n"
        f"stderr:\n{proc.stderr[-2500:]}")
    return json.loads(lines[0][len("CAPABILITY_RESULT "):])


def _refused(proc, *needles) -> str:
    assert proc.returncode != 0, (
        f"the server started: {proc.stdout[-800:]}\nstderr:\n{proc.stderr[-800:]}")
    assert "ImproperlyConfigured" in proc.stderr, proc.stderr[-2500:]
    for needle in (RUNBOOK, *needles):
        assert needle in proc.stderr, (needle, proc.stderr[-2500:])
    return proc.stderr


def _on(key_file, **over) -> dict:
    return {FLAG: "on", KEY_PATH: str(key_file), KID: "7", PUBKEYS: f"7={PK1}", **over}


def test_startup_with_a_valid_key_loads_the_store_once(key_file):
    got = _result(_boot(_on(key_file)))
    assert got["kid"] == 7 and got["kids"] == [7] and got["biscuit"] is True, got


def test_startup_refuses_a_missing_key_file(key_file, tmp_path):
    stderr = _refused(_boot(_on(key_file, **{KEY_PATH: str(tmp_path / "absent.key")})),
                      KEY_PATH)
    assert SK1[:16] not in stderr
    # CONTROL: the same environment with the file present starts (the test above).


def test_startup_refuses_an_invalid_key_file(tmp_path):
    bad = tmp_path / "bad.key"
    bad.write_text("not-a-key\n", encoding="ascii")
    _refused(_boot(_on(bad)), KEY_PATH)


def test_startup_refuses_a_pasted_base64_key_without_printing_it(key_file):
    """Review F1, end to end: the key itself, base64-encoded, set as
    the PATH variable. ready() re-raises the refusal as ImproperlyConfigured on stderr, and
    stderr holds no 8 characters of the value. CONTROL: the same boot with the variable
    naming the key file starts (the valid-key startup test)."""
    pasted = base64.b64encode(bytes.fromhex(SK1)).decode("ascii")
    stderr = _refused(_boot(_on(key_file, **{KEY_PATH: pasted})), KEY_PATH)
    assert not _repeated(pasted, stderr), _repeated(pasted, stderr)[:3]


def test_startup_refuses_a_verify_set_without_the_active_kid(key_file):
    _refused(_boot(_on(key_file, **{PUBKEYS: f"9={PK2}"})), PUBKEYS)


def test_startup_refuses_when_biscuit_cannot_be_imported(key_file):
    _refused(_boot({**_on(key_file), "CAPABILITY_CHILD_BREAK_BISCUIT": "1"}), "biscuit_auth")


def test_a_manage_py_command_is_refused_too(key_file, tmp_path):
    """ready() runs for every manage.py command (critique C15), so the runbook line is real.
    CONTROL: the same command with the flag unset runs."""
    env = _child_env(_on(key_file, **{KEY_PATH: str(tmp_path / "absent.key")}))
    cmd = [sys.executable, "manage.py", "check"]
    refused = subprocess.run(cmd, env=env, cwd=str(ROOT), capture_output=True, text=True,
                             timeout=180)
    assert refused.returncode != 0 and RUNBOOK in refused.stderr, refused.stderr[-2000:]
    env.pop(FLAG)
    ok = subprocess.run(cmd, env=env, cwd=str(ROOT), capture_output=True, text=True,
                        timeout=180)
    assert ok.returncode == 0, ok.stderr[-2000:]


def test_off_never_imports_biscuit_even_with_every_key_variable_broken(tmp_path):
    got = _result(_boot({KEY_PATH: str(tmp_path / "absent.key"), KID: "seven",
                         PUBKEYS: "garbage", DEV: "1"}))
    assert got["biscuit"] is False and got["kid"] is None, got
    got = _result(_boot({FLAG: "off", KEY_PATH: str(tmp_path / "absent.key")}))
    assert got["biscuit"] is False and got["kid"] is None, got


def test_startup_never_reads_legacy_variable_and_takes_the_key_from_the_file(key_file):
    """The whole startup, settings.py and every ready() included, through a recording
    os.environ: MESHYCHAT_BISCUIT_ROOT_SK is set to ANOTHER valid key and is never read, and the
    store holds the file's key. CONTROL: the recorder saw this server's own variables."""
    got = _result(_boot({**_on(key_file), LEGACY_ROOT_KEY_ENV: SK2, "CAPABILITY_CHILD_RECORD": "1"}))
    assert got["kid"] == 7 and got["biscuit"] is True, got
    assert LEGACY_ROOT_KEY_ENV not in got["reads"], got["reads"]
    for name in (FLAG, KEY_PATH, KID, PUBKEYS):
        assert name in got["reads"], (name, got["reads"])
    assert DEV not in got["reads"], "the dev flag was read with DEBUG off"


# ============================================================================ the key file

def test_the_key_file_is_created_exclusively(tmp_path):
    """On every platform: a new key its creator can load, and a second create on the same
    path refused with the first key untouched (creation is the test, 23ca808)."""
    k = _keys()
    path = tmp_path / "keys" / "root.key"
    public_hex = k.create_key_file(path)
    text = path.read_text(encoding="ascii")
    assert len(text) == 65 and text.endswith("\n") and int(text[:64], 16) >= 0
    store = _load(k, key_path=path, kid="3", pubkeys=f"3={public_hex}")
    assert store.public_key_bytes(3).hex() == public_hex

    # A second create on the same path is refused and the first key is untouched.
    with pytest.raises(k.CapabilityKeyRefused, match="exists"):
        k.create_key_file(path)
    assert path.read_text(encoding="ascii") == text


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; the Windows ACL is the next test")
def test_on_posix_the_key_file_is_owner_only_from_its_first_byte(tmp_path, monkeypatch):
    """O_EXCL and mode 0o600 in the one open call, and the file's mode after it, with the
    umask cleared so the mode is the call's and not the process's."""
    import stat

    k = _keys()
    path = tmp_path / "root.key"
    opened = []
    real_open = os.open

    def spy(p, flags, mode=0o777, *a, **kw):
        opened.append((str(p), flags, mode))
        return real_open(p, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    previous = os.umask(0)
    try:
        k.create_key_file(path)
    finally:
        os.umask(previous)
        monkeypatch.setattr(os, "open", real_open)
    assert [(p, m) for p, _f, m in opened] == [(str(path), 0o600)], opened
    flags = opened[0][1]
    assert flags & os.O_EXCL and flags & os.O_CREAT, flags
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, oct(os.stat(path).st_mode)


def test_the_key_file_is_never_created_inside_the_checkout(tmp_path, monkeypatch):
    """Review F5, Q10 ("never in the repository"). The key command runs from the checkout,
    so a relative PATH landed in the working tree, one `git add -A` from a commit; `secrets/`
    is ignored but still inside it. A path inside the checkout, relative or absolute, is
    refused and nothing is created. CONTROL: the same name outside the checkout is made."""
    import uuid

    k = _keys()
    name = f"capability-root-{uuid.uuid4().hex[:8]}.key"
    targets = [ROOT / name, ROOT / "secrets" / name, ROOT / "tests" / "fixtures" / name]
    secrets_existed = (ROOT / "secrets").exists()
    try:
        for target in targets:
            with pytest.raises(k.CapabilityKeyRefused, match="checkout"):
                k.create_key_file(target)
            assert not target.exists(), target
        # Refused before anything is made: not even the parent directory.
        assert (ROOT / "secrets").exists() == secrets_existed
        # CONTROL for the comparison, on names only (nothing is created): a sibling folder
        # whose name begins with the checkout's is outside it.
        assert k._inside_checkout(ROOT / "apps" / name)
        assert not k._inside_checkout(Path(str(ROOT) + "-sibling") / name)
        monkeypatch.chdir(ROOT)
        assert k.main(["create", name, "--kid", "3"]) == 2
        assert not (ROOT / name).exists()
        assert k.create_key_file(tmp_path / name) and (tmp_path / name).exists()
    finally:
        for target in targets:
            target.unlink(missing_ok=True)
        if not secrets_existed and (ROOT / "secrets").is_dir():
            (ROOT / "secrets").rmdir()


def test_the_repository_ignores_key_files():
    """Review F5's second half: a key file that reaches the working tree anyway is ignored.
    `git check-ignore --no-index` reads the ignore rules, not the index. CONTROL: a source
    file is not ignored."""
    import shutil

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not on PATH")

    def ignored(rel: str) -> bool:
        proc = subprocess.run([git, "check-ignore", "-q", "--no-index", rel], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode in (0, 1), proc.stderr
        return proc.returncode == 0

    for rel in ("root.key", "keys/root.key", "capability_root.key"):
        assert ignored(rel), rel
    assert not ignored("apps/memoryskills/capability_keys.py")


def _aces(path) -> list[str]:
    """icacls's access entries for `path`, one per line: an instrument that shares nothing
    with the code under test."""
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    body = out.stdout.replace(str(path), "", 1)
    return [line.strip() for line in body.splitlines() if ":(" in line]


@pytest.mark.skipif(os.name != "nt", reason="the Windows ACL; POSIX modes are the test above")
def test_on_windows_the_key_file_is_owner_only_from_creation(tmp_path):
    """Review F4. os.open's 0o600 sets no ACL on Windows, so the key file took its folder's
    inherited entries (the reviewer measured INTERACTIVE, SERVICE and BATCH with Modify
    under C:/Users/Public). The file must be created with a protected ACL whose one entry
    is the creating user, full control, nothing inherited. CONTROL: a file made the
    ordinary way in the same folder shows inherited entries, so the instrument sees what
    the key file must not have.

    The folder is the test's own, with an ACL it sets: inheritance from above cut, and three
    entries every child inherits (SYSTEM, Administrators and the user). The base temp folder
    cannot serve: on HQB it hands its files explicit entries, not inherited ones (#200)."""
    k = _keys()
    me = subprocess.run(["whoami"], capture_output=True, text=True, timeout=60).stdout.strip()
    assert me
    # The user by SID, not by name: a name with a non-ASCII letter is decoded in the wrong code page and then
    # names no account (Codex review of #241). A SID is ASCII. whoami /user /fo csv /nh: "<name>","<SID>".
    who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=60)
    sid = who.stdout.strip().rsplit(",", 1)[-1].strip('"')
    assert who.returncode == 0 and sid.startswith("S-1-"), who.stdout + who.stderr
    home = tmp_path / "acl"
    home.mkdir()
    made = subprocess.run(["icacls", str(home), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)(F)",
                           "*S-1-5-32-544:(OI)(CI)(F)", f"*{sid}:(OI)(CI)(F)"],
                          capture_output=True, text=True, timeout=60)
    assert made.returncode == 0, made.stdout + made.stderr
    tmp_path = home
    path = tmp_path / "root.key"
    k.create_key_file(path)
    assert [a.lower() for a in _aces(path)] == [f"{me.lower()}:(f)"], (me, _aces(path))
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="ascii")
    inherited = _aces(plain)
    assert len(inherited) >= 2 and all("(I)" in a for a in inherited), inherited

    # PROTECTED, not only explicit: a later inheritable entry on the folder (here BUILTIN
    # Users, read, by SID) propagates to every child whose ACL is not protected. The key
    # file keeps its one entry; the plain file (the control) takes the new one.
    grant = subprocess.run(["icacls", str(tmp_path), "/grant", "*S-1-5-32-545:(OI)(R)"],
                           capture_output=True, text=True, timeout=60)
    assert grant.returncode == 0, grant.stdout + grant.stderr
    assert [a.lower() for a in _aces(path)] == [f"{me.lower()}:(f)"], _aces(path)
    after = _aces(plain)
    assert len(after) == len(inherited) + 1 and all("(I)" in a for a in after), after


def test_the_key_command_runs_without_django_and_never_prints_the_private_key(tmp_path):
    path = tmp_path / "root.key"
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    proc = subprocess.run([sys.executable, "-m", "apps.memoryskills.capability_keys",
                           "create", str(path), "--kid", "3"],
                          env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr[-1500:])
    private = path.read_text(encoding="ascii").strip()
    public = _keys().public_of(bytes.fromhex(private)).hex()
    assert f"{PUBKEYS}=3={public}" in proc.stdout, proc.stdout
    assert private not in proc.stdout + proc.stderr
    assert private[:16] not in proc.stdout + proc.stderr
    again = subprocess.run([sys.executable, "-m", "apps.memoryskills.capability_keys",
                            "create", str(path), "--kid", "3"],
                           env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert again.returncode != 0 and "exists" in again.stderr, (again.stdout, again.stderr)
    assert path.read_text(encoding="ascii").strip() == private


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
