"""The blob configuration is read ONCE, at startup, and a wrong one refuses to start (B11).

WHY. `MESHYCHAT_BLOB_BACKEND` and its fields are parsed at boot, and a bad value refuses to
start: a wrong backend is a silent failure mode for files because the server boots while
uploads fail with confusing errors. Reading the environment on every request allowed a
set-but-wrong configuration to boot a server that answered 503 on every file route.

THE CONTRACT, each pinned below:
  * MESHYCHAT_BLOB_BACKEND set and anything wrong with it - an unknown backend, a required field
    missing, a token secret that is not hex, a size or window that is not a non-negative
    integer - and the process does NOT start, naming the variable.
  * A complete configuration starts (the control beside every refusal).
  * MESHYCHAT_BLOB_BACKEND unset starts, and says so once. This is the divergence that remains
    (apps/files/config.py; docs/DIVERGENCES.md 035): a node without blob storage serves
    chat, and the fresh-install runbook produces one.
  * The routes use the configuration read at startup. Changing the environment afterwards
    changes nothing: a token signed with the startup secret still verifies, and one signed
    with the secret the environment now names is refused.

Each case is a real startup in a subprocess (`import meshychat.asgi`, the served
entrypoint; daphne and uvicorn reach the same django.setup()). A refused startup is that
process exiting non-zero with the error on stderr, as a server's would. No database: the
blob configuration is read before anything connects, and the raw-token route opens none.

Run:  pytest tests/test_files_startup.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
OTHER_HEX = "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"
BASE = "https://meshy.test"

#: Every variable the blob configuration reads, cleared before each case so the parent's
#: environment cannot decide a result.
BLOB_VARS = ("MESHYCHAT_BLOB_BACKEND", "MESHYCHAT_BLOB_DIR", "MESHYCHAT_BLOB_TOKEN_SECRET",
             "MESHYCHAT_PUBLIC_BASE_URL", "MESHYCHAT_R2_ACCOUNT_ID", "MESHYCHAT_R2_BUCKET",
             "MESHYCHAT_R2_ACCESS_KEY_ID", "MESHYCHAT_R2_SECRET_ACCESS_KEY", "MESHYCHAT_R2_ENDPOINT",
             "MESHYCHAT_UPLOAD_WINDOW_SECS", "MESHYCHAT_PRESIGN_TTL_SECS", "MESHYCHAT_MAX_FILE_BYTES")

BOOT = "import meshychat.asgi\nprint('BOOTED', flush=True)\n"


def _env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in BLOB_VARS}
    env["DJANGO_SETTINGS_MODULE"] = "meshychat.settings"
    env["PYTHONPATH"] = str(ROOT)
    env.pop("MESHY_FLEET_ROSTER_PATH", None)       # another app's startup check
    env.update(extra)
    return env


def _boot(extra: dict, code: str = BOOT) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=_env(extra),
                          capture_output=True, text=True, timeout=120)


def _fs(tmp_path, **over) -> dict:
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(exist_ok=True)
    env = {"MESHYCHAT_BLOB_BACKEND": "fs", "MESHYCHAT_BLOB_DIR": str(blob_dir),
           "MESHYCHAT_BLOB_TOKEN_SECRET": HEX, "MESHYCHAT_PUBLIC_BASE_URL": BASE}
    for k, v in over.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def _refused(proc) -> bool:
    return proc.returncode != 0 and "BOOTED" not in proc.stdout


REFUSALS = [
    ("an unknown backend", {"MESHYCHAT_BLOB_BACKEND": "s3"}, "MESHYCHAT_BLOB_BACKEND"),
    ("fs without MESHYCHAT_BLOB_DIR", {"MESHYCHAT_BLOB_DIR": None}, "MESHYCHAT_BLOB_DIR"),
    ("fs without a token secret", {"MESHYCHAT_BLOB_TOKEN_SECRET": None},
     "MESHYCHAT_BLOB_TOKEN_SECRET"),
    ("fs with a token secret that is not hex", {"MESHYCHAT_BLOB_TOKEN_SECRET": "not-hex!"},
     "hex"),
    ("fs without a public base url", {"MESHYCHAT_PUBLIC_BASE_URL": None},
     "MESHYCHAT_PUBLIC_BASE_URL"),
    ("a max file size that is not an integer", {"MESHYCHAT_MAX_FILE_BYTES": "ten"},
     "MESHYCHAT_MAX_FILE_BYTES"),
    ("a negative presign ttl", {"MESHYCHAT_PRESIGN_TTL_SECS": "-1"}, "MESHYCHAT_PRESIGN_TTL_SECS"),
    ("r2 without a bucket", {"MESHYCHAT_BLOB_BACKEND": "r2", "MESHYCHAT_BLOB_DIR": None,
                             "MESHYCHAT_BLOB_TOKEN_SECRET": None, "MESHYCHAT_PUBLIC_BASE_URL": None,
                             "MESHYCHAT_R2_ACCOUNT_ID": "acct", "MESHYCHAT_R2_ACCESS_KEY_ID": "akid",
                             "MESHYCHAT_R2_SECRET_ACCESS_KEY": "NEVER-PRINT-THIS-VALUE"},
     "MESHYCHAT_R2_BUCKET"),
]


@pytest.mark.parametrize("over,needle", [(o, n) for _, o, n in REFUSALS],
                         ids=[name for name, _, _ in REFUSALS])
def test_a_set_but_unusable_blob_configuration_refuses_to_start(tmp_path, over, needle):
    """Each differs from the complete configuration (the control, below and asserted
    here too) in ONE variable, so the refusal is about that variable."""
    control = _boot(_fs(tmp_path))
    assert control.returncode == 0 and "BOOTED" in control.stdout, (
        f"control: the complete configuration must start\n{control.stderr[-2000:]}")

    proc = _boot(_fs(tmp_path, **over))
    assert _refused(proc), (
        f"started with {over}: exit {proc.returncode}, stdout tail {proc.stdout[-300:]!r}")
    assert "ImproperlyConfigured" in proc.stderr, proc.stderr[-2000:]
    assert needle in proc.stderr, proc.stderr[-2000:]
    # The refusal names the variable, never a secret's value.
    assert "NEVER-PRINT-THIS-VALUE" not in proc.stderr + proc.stdout


def test_an_unset_backend_starts_and_says_so_once(tmp_path):
    """The remaining divergence, measured beside a refusal that differs from it only in
    the backend's VALUE: blank is unset and starts; a wrong value refuses."""
    proc = _boot({})
    assert proc.returncode == 0 and "BOOTED" in proc.stdout, proc.stderr[-2000:]
    said = (proc.stdout + proc.stderr).count("MESHYCHAT_BLOB_BACKEND is unset")
    assert said == 1, (said, proc.stdout[-1500:], proc.stderr[-1500:])

    blank = _boot({"MESHYCHAT_BLOB_BACKEND": "  "})
    assert blank.returncode == 0 and "BOOTED" in blank.stdout, blank.stderr[-2000:]
    assert _refused(_boot({"MESHYCHAT_BLOB_BACKEND": "fss"}))


FROZEN = r'''
import datetime, os, sys
import meshychat.asgi
from django.test import Client
from apps.files import blob
now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
def token(secret_hex):
    return blob.sign_token(bytes.fromhex(secret_hex), blob.TokenPayload(
        key="o/f", op="GET", exp_unix=now + 300))
startup_secret = os.environ["MESHYCHAT_BLOB_TOKEN_SECRET"]
# AFTER startup, the environment names a different secret, directory and backend.
os.environ["MESHYCHAT_BLOB_TOKEN_SECRET"] = sys.argv[1]
os.environ["MESHYCHAT_BLOB_DIR"] = sys.argv[2]
c = Client()
a = c.get("/v1/files/raw/" + token(startup_secret))
b = c.get("/v1/files/raw/" + token(sys.argv[1]))
os.environ.pop("MESHYCHAT_BLOB_BACKEND")
d = c.get("/v1/files/raw/" + token(startup_secret))
import json
print("RESULT " + json.dumps([a.status_code, a.content.decode(), b.status_code,
                              d.status_code]), flush=True)
'''


def test_the_routes_use_the_configuration_read_at_startup(tmp_path):
    """A token signed with the STARTUP secret reads the object from the STARTUP directory
    after the environment has been changed to name another secret and another directory,
    and after the backend variable is gone. The control: a token signed with the secret
    the environment now names is refused 401, so the 200 is not "any token works"."""
    env = _fs(tmp_path)
    obj = Path(env["MESHYCHAT_BLOB_DIR"]) / "o" / "f"
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"startup bytes")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "o").mkdir(parents=True)
    (elsewhere / "o" / "f").write_bytes(b"later bytes")
    env["MESHY_ALLOWED_HOSTS"] = "testserver"

    proc = subprocess.run([sys.executable, "-c", FROZEN, OTHER_HEX, str(elsewhere)],
                          cwd=str(ROOT), env=_env(env), capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, proc.stdout[-1500:]
    a, body, b, d = json.loads(line[-1][len("RESULT "):])
    assert (a, body) == (200, "startup bytes"), line
    assert b == 401, line
    assert d == 200, line


HEALTH = r'''
import json, os, sys
import meshychat.asgi
from django.test import Client
c = Client()
def blob():
    return json.loads(c.get("/v1/health").content)["blob"]
out = [blob()]
# AFTER startup: the backend variable gone, the directory variable naming a real
# directory that is not the store's (sys.argv[1]), or both set on a server that started
# without a store (sys.argv[2] == "set").
if sys.argv[2] == "set":
    os.environ["MESHYCHAT_BLOB_BACKEND"] = "fs"
    os.environ["MESHYCHAT_BLOB_DIR"] = sys.argv[1]
else:
    os.environ.pop("MESHYCHAT_BLOB_BACKEND")
    os.environ["MESHYCHAT_BLOB_DIR"] = sys.argv[1]
out.append(blob())
if sys.argv[2] != "set":
    startup_dir = sys.argv[3]
    os.rename(startup_dir, startup_dir + "-gone")     # the STORE's directory goes away
    out.append(blob())
print("RESULT " + json.dumps(out), flush=True)
'''


def _health(env, *argv) -> list:
    proc = subprocess.run([sys.executable, "-c", HEALTH, *argv], cwd=str(ROOT),
                          env=_env(env), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, proc.stdout[-1500:]
    return json.loads(line[-1][len("RESULT "):])


def test_health_reports_the_store_built_at_startup_not_the_environment(tmp_path):
    """Review of c007c45 (LOW): /v1/health's blob probe read MESHYCHAT_BLOB_* on every request,
    so with the routes serving from the startup store it could report `blob: false` while
    every file route answered 200 - or `true` on a server that has no store. The health
    probe checks the startup store.

      * started with fs: true; still true after the environment drops the backend;
        false once the STORE's directory is gone (the control that the probe is asked of
        the store, not a constant);
      * started without a store: false, and still false after the environment names a
        complete fs configuration.

    The store's directory holds a file for every ready row in the run's database first
    (tests/blob_ready_files.py): since divergence 047 the fs probe also looks for them, and
    modules that ran earlier leave ready rows whose files are elsewhere."""
    from tests.blob_ready_files import place_ready_files

    env = _fs(tmp_path)
    place_ready_files(env["MESHYCHAT_BLOB_DIR"])
    env["MESHY_ALLOWED_HOSTS"] = "testserver"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _health(env, str(elsewhere), "drop", env["MESHYCHAT_BLOB_DIR"]) == [True, True, False]

    bare = {"MESHY_ALLOWED_HOSTS": "testserver"}
    assert _health(bare, str(elsewhere), "set") == [False, False]

def test_a_complete_r2_configuration_starts():
    """F25 beside B11: before the object-store backend existed, `MESHYCHAT_BLOB_BACKEND=r2`
    could only fail, and a startup check would have refused every r2 deployment. The
    four required variables, and nothing else, start. Control: without the bucket, the
    refusal above."""
    proc = _boot({"MESHYCHAT_BLOB_BACKEND": "r2", "MESHYCHAT_R2_ACCOUNT_ID": "acct",
                  "MESHYCHAT_R2_BUCKET": "bucket", "MESHYCHAT_R2_ACCESS_KEY_ID": "akid",
                  "MESHYCHAT_R2_SECRET_ACCESS_KEY": "NEVER-PRINT-THIS-VALUE"})
    assert proc.returncode == 0 and "BOOTED" in proc.stdout, proc.stderr[-2000:]
    assert "NEVER-PRINT-THIS-VALUE" not in proc.stdout + proc.stderr
