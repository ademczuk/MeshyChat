"""A blob directory consistent with the test run's database (divergence 047, ASDLC OI-48).

Since 047, /v1/health's `blob` is true for an fs store only when its blob_dir holds a file for
each of the newest ready `file_artifacts` rows (`meshychat.urls.probe_blob`). A pytest run has
ONE database (tests/conftest.py), and every module builds its fs store on a temporary directory
of its own, so the ready rows earlier modules leave behind have no file in a later module's
directory: tests/test_a2a.py seeds one directly, and tests/test_files.py uploads through the
routes into directories that are gone by the time a health test runs. In the local gate's
order (alphabetical, tests/test_a2a.py first) a store that passes every other check would then
read `blob: false`, and a test that means "every dependency passes" would fail for running
after them.

So a fixture that means HEALTHY makes its directory hold those files, as a real server's does.
Zero-byte files are enough: the check stats a path and never opens it.
"""
from __future__ import annotations

import os

import psycopg

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


def place_ready_files(blob_dir) -> int:
    """Create an empty file under `blob_dir` at every ready row's `r2_key`, through the fs
    store's own `put_bytes` (its `safe_path`), so each lands exactly where the probe looks.
    Returns how many ready rows there were. Read as the seeding role; `file_artifacts` has no
    row-level security (apps/files/repository.py, measured), so every role sees every row."""
    from apps.files import blob as blobmod, config as cfgmod

    store = blobmod.FsBlobStore(cfgmod.FsConfig(
        blob_dir=str(blob_dir), token_secret="00" * 32, public_base_url="https://meshy.test"))
    with psycopg.connect(os.environ.get("MESHY_TEST_DSN", DSN), autocommit=True) as conn:
        keys = [r[0] for r in conn.execute(
            "SELECT r2_key FROM file_artifacts WHERE status = 'ready'").fetchall()]
    for key in keys:
        store.put_bytes(key, b"")
    return len(keys)
