"""apps.context.validation and screened_outbox import without server-only packages.

Client nodes such as HZ run tools/transcript_cards/context_upload.py in a lightweight
virtual environment that does not install Django, psycopg, PyNaCl, or rfc8785. Before
this fix, importing apps.context.validation failed at two eager server imports:

  * apps/core/env.py imported django.conf.settings and ImproperlyConfigured at top level;
  * apps/memoryskills/__init__.py imported .handlers (which imports psycopg and nacl), and
    apps/memoryskills/validation.py imported apps.crypto.primitives (nacl and rfc8785) even
    though only jcs_canonicalize uses it.

screened_outbox.freeze_screened caught that ImportError and refused every transcript batch
with E_CONTEXT_SCREEN_UNAVAILABLE. This test builds a real isolated venv without django,
psycopg, nacl, or rfc8785 and proves apps.context.validation, apps.context.screens,
screened_outbox.freeze_screened, and context_upload.run_pass run cleanly inside it.

Run: .venv/Scripts/python.exe -m pytest tests/test_context_validation_client_venv.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_site_packages(py: Path) -> Path:
    out = subprocess.check_output(
        [str(py), "-c", "import sysconfig; print(sysconfig.get_path('platlib'))"],
        text=True,
        timeout=30,
    ).strip()
    return Path(out)


def _copy_cryptography_only(dst_site: Path) -> None:
    """Copy cryptography (and its CFFI/auditwheel runtime files, nothing else) into dst_site."""
    code = (
        "import json, pathlib\n"
        "import cryptography\n"
        "crypto_dir = pathlib.Path(cryptography.__file__).resolve().parent\n"
        "site_dir = crypto_dir.parent\n"
        "out = {'crypto': str(crypto_dir), 'dirs': [], 'files': []}\n"
        "for name in ('cryptography.libs', 'cffi', 'cffi.libs'):\n"
        "    cand = site_dir / name\n"
        "    if cand.is_dir():\n"
        "        out['dirs'].append(str(cand))\n"
        "try:\n"
        "    import _cffi_backend\n"
        "    out['files'].append(str(pathlib.Path(_cffi_backend.__file__).resolve()))\n"
        "except ImportError:\n"
        "    pass\n"
        "print(json.dumps(out))\n"
    )
    info = subprocess.check_output([sys.executable, "-c", code], text=True, timeout=30).strip()
    import json

    parsed = json.loads(info)
    dst_site.mkdir(parents=True, exist_ok=True)
    shutil.copytree(parsed["crypto"], dst_site / "cryptography")
    for d in parsed["dirs"]:
        src_d = Path(d)
        shutil.copytree(src_d, dst_site / src_d.name)
    for f in parsed["files"]:
        src_f = Path(f)
        shutil.copy2(src_f, dst_site / src_f.name)


_CLIENT_VENV_PROBE = """\
import hashlib
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
tmp_work = Path(sys.argv[2])

for pkg in ("django", "psycopg", "nacl", "rfc8785"):
    try:
        importlib.import_module(pkg)
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError(f"{pkg} must not be installed in client venv")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

assert len(Ed25519PrivateKey.generate().public_key().public_bytes_raw()) == 32

sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tools" / "transcript_cards"))

import apps.context.validation as cv
from apps.core import env
from apps.memoryskills.errors import HandlerError

for forbidden in ("django", "psycopg", "nacl", "rfc8785", "apps.memoryskills.handlers"):
    assert forbidden not in sys.modules, f"{forbidden} loaded unexpectedly"

# 1. Pure item + manifest validation and typed refusal.
seat = "oxxxxxx"
src = "a" * 32
text = "hello from client venv"
digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
now = datetime(2026, 11, 7, 6, 30, 0, tzinfo=timezone.utc)
item_key = f"ctx/{seat}/{src}/0001/000001/0000000000"
item_val = {
    "schema": "context.v1",
    "kind": "item",
    "seat": seat,
    "src": src,
    "gen": 1,
    "batch": 1,
    "ordinal": 0,
    "dialect": "claude",
    "adapter_version": "1.0",
    "time": "2026-11-07T06:29:00.000Z",
    "item": "user_text",
    "role": "user",
    "text": text,
    "text_sha256": digest,
}
assert cv.validate_context("shared/transcript-context", item_key, item_val, now=now) == "item"

bad_val = dict(item_val, text_sha256="0" * 64)
try:
    cv.validate_context("shared/transcript-context", item_key, bad_val, now=now)
except HandlerError as exc:
    assert exc.code == "E_CONTEXT_DIGEST", exc.code
else:
    raise AssertionError("expected E_CONTEXT_DIGEST")

manifest_key = f"ctxm/{seat}/{src}/0001/000001"
manifest_val = {
    "schema": "context.v1",
    "kind": "manifest",
    "seat": seat,
    "src": src,
    "gen": 1,
    "batch": 1,
    "first": 0,
    "last": 0,
    "items": [[0, digest]],
    "counts": {"written": 1, "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 0},
}
assert cv.validate_context("shared/transcript-context", manifest_key, manifest_val, now=now) == "manifest"

# 2. apps.context.screens + screened_outbox.freeze_screened + context_upload.run_pass end-to-end.
tmp_work.mkdir(parents=True, exist_ok=True)
denylist = tmp_work / "denylist.json"
denylist.write_text('["ZorblaxCorp"]', encoding="utf-8")
os.environ["MESHY_CONTEXT_DENYLIST"] = str(denylist)
try:
    cv.screens.screen("ordinary engineering note")
    try:
        cv.screens.screen("AKIA" + "IOSFODNN7EXAMPLE")
    except HandlerError as exc:
        assert exc.code == "E_CONTEXT_SECRET", exc.code
    else:
        raise AssertionError("expected E_CONTEXT_SECRET from screens.screen")
    try:
        cv.screens.screen("confidential note for Zorblax-Corp")
    except HandlerError as exc:
        assert exc.code == "E_CONTEXT_CLIENT_NAME", exc.code
    else:
        raise AssertionError("expected E_CONTEXT_CLIENT_NAME from screens.screen")
finally:
    os.environ.pop("MESHY_CONTEXT_DENYLIST", None)

import context_upload
from screened_outbox import AdmissionError, freeze_screened

transcript = tmp_work / "session.jsonl"
lines = [
    json.dumps({
        "type": "user",
        "sessionId": "11111111-2222-3333-4444-555555555555",
        "uuid": "msg-user-1",
        "timestamp": "2026-11-07T06:20:00.000Z",
        "message": {"role": "user", "content": "Check client preflight."},
    }),
    json.dumps({
        "type": "assistant",
        "sessionId": "11111111-2222-3333-4444-555555555555",
        "uuid": "msg-asst-1",
        "timestamp": "2026-11-07T06:20:05.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Client preflight works."}]},
    }),
]
transcript.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
old_ts = datetime(2026, 11, 7, 6, 20, 10, tzinfo=timezone.utc).timestamp()
os.utime(transcript, (old_ts, old_ts))


class StubTransport:
    def __init__(self):
        self.written = {}

    def write(self, key, value):
        self.written[key] = value
        return {"status": "completed"}

    def reconcile(self, outbox, batch_key):
        act = outbox.next_action(batch_key)
        assert act.kind == "read", act.kind
        proof = b"{}"
        return outbox.observe(
            act.attempt,
            value=act.record.value,
            evidence_sha256=hashlib.sha256(proof).hexdigest(),
            evidence_artifact=proof,
        )


cfg = context_upload.Config(
    seat=seat,
    dialect="claude",
    store_dir=tmp_work / "store",
    sources=[str(transcript)],
)
transport = StubTransport()
report = context_upload.run_pass(cfg, transport, now=now)
assert report.batches_committed == 1, report.as_json()
assert report.items_committed == 2, report.as_json()
assert report.context_held == [], report.as_json()

# 3. apps.core.env contract without Django installed (even if DJANGO_SETTINGS_MODULE is set).
os.environ["DJANGO_SETTINGS_MODULE"] = "meshychat.settings"
os.environ["MESHYCHAT_CLIENT_PROBE_FLAG"] = "env-val"
assert env.setting("MESHY_CLIENT_PROBE_FLAG", "MESHYCHAT_CLIENT_PROBE_FLAG", default="dflt") == "env-val"
assert env.lookup("MESHYCHAT_BLOB_DIR", source={"MESHYCHAT_BLOB_DIR": "/a"}) == "/a"
assert env.lookup("MESHYCHAT_BLOB_DIR", "dflt", source={}) == "dflt"

try:
    env.number("MESHY_TEST_LIMIT", "bad", int)
except Exception as exc:
    assert str(exc) == "MESHY_TEST_LIMIT must be a int, got 'bad'", str(exc)
else:
    raise AssertionError("expected number() refusal")

for forbidden in ("django", "psycopg", "nacl", "rfc8785", "apps.memoryskills.handlers"):
    assert forbidden not in sys.modules, f"{forbidden} loaded after run_pass"

print("CLIENT_VENV_OK")
"""


def test_context_validation_and_upload_preflight_in_venv_without_server_deps(tmp_path):
    venv_dir = tmp_path / "client_venv"
    venv.EnvBuilder(with_pip=False, system_site_packages=False).create(venv_dir)
    py = _venv_python(venv_dir)
    site_dir = _venv_site_packages(py)
    _copy_cryptography_only(site_dir)

    probe_file = tmp_path / "probe.py"
    probe_file.write_text(_CLIENT_VENV_PROBE, encoding="utf-8")

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "DJANGO_SETTINGS_MODULE", "PYTHONHOME")
    }
    proc = subprocess.run(
        [str(py), "-I", str(probe_file), str(ROOT), str(tmp_path / "work")],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "CLIENT_VENV_OK" in proc.stdout


def test_server_interpreter_keeps_django_and_memoryskills_handlers_lazy():
    code = (
        "import sys\n"
        "import apps.context.validation as cv\n"
        "import apps.memoryskills as ms\n"
        "for mod in ('django', 'psycopg', 'nacl', 'rfc8785', 'apps.memoryskills.handlers'):\n"
        "    assert mod not in sys.modules, f'{mod} imported eagerly'\n"
        "assert 'check_permission' in vars(ms)\n"
        "assert callable(ms.handle_write)\n"
        "assert 'apps.memoryskills.handlers' in sys.modules\n"
        "try:\n"
        "    _ = ms.no_such_handler_symbol\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('expected AttributeError')\n"
        "print('LAZY_SERVER_OK')\n"
    )
    clean_env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    clean_env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=clean_env,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "LAZY_SERVER_OK" in proc.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
