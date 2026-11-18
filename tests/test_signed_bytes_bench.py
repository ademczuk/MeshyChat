"""What the agree-trigger costs per insert, at the two sizes design 4.1 names. Divergence 039.

The trigger does one SHA-256 and one JSONB parse of the payload, plus the derivation
functions. The first draft bounded that by a mesh body (validation.MESH_MAX_BODY_BYTES,
65,536 bytes), which holds only for mesh v1 messages; any other namespace's value is bounded
on HTTP by Django's DATA_UPLOAD_MAX_MEMORY_SIZE, 2.5 MB by default. So both are measured:
the same direct insert as the owner role, with the trigger enabled and then disabled inside
a transaction that is rolled back, the median of REPS each.

The assertion is deliberately loose (a regression that makes the trigger quadratic, not a
performance contract): what matters is the number printed, which divergence 039 records.

Run:  .venv/Scripts/python.exe -m pytest tests/test_signed_bytes_bench.py -q -s
"""
from __future__ import annotations

import os
import statistics
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.memoryskills.validation import MESH_MAX_BODY_BYTES  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
REPS = 5
DJANGO_BODY_LIMIT = 2_621_440          # DATA_UPLOAD_MAX_MEMORY_SIZE's default, 2.5 MiB


@pytest.fixture()
def world():
    with psycopg.connect(DSN) as conn:
        owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
        conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                     "VALUES (%s,%s,'dev',%s)",
                     (owner_id, f"bench-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
        conn.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                     "VALUES (%s,%s,%s,'bench')", (peer_id, owner_id, f"bench-{peer_id.hex[:8]}"))
        ns = f"bench/{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                     " owner_owner_id) VALUES (%s,%s,%s)", (ns, peer_id, owner_id))
        conn.commit()
        yield conn, {"peer_id": peer_id, "owner_id": owner_id}, ns


def _timed_inserts(conn, peer, ns, body_bytes: int, *, trigger: bool) -> list[float]:
    out = []
    with conn.transaction(force_rollback=True):
        if not trigger:
            conn.execute("ALTER TABLE shared_memory DISABLE TRIGGER "
                         "shared_memory_signed_bytes_agree_trg")
        for _ in range(REPS):
            p = sr.payload(peer["peer_id"], ns, f"k/{uuid.uuid4().hex}",
                           {"body": "x" * body_bytes})
            cols = sr.columns(p, peer_id=peer["peer_id"], owner_id=peer["owner_id"])
            t0 = time.perf_counter()
            sr.insert(conn, cols)
            out.append(time.perf_counter() - t0)
    return out


@pytest.mark.parametrize("body_bytes", [MESH_MAX_BODY_BYTES, DJANGO_BODY_LIMIT],
                         ids=["64KiB-mesh-body", "2.5MB-value"])
def test_the_agree_trigger_cost_at_both_bounds(world, body_bytes):
    conn, peer, ns = world
    on = statistics.median(_timed_inserts(conn, peer, ns, body_bytes, trigger=True))
    off = statistics.median(_timed_inserts(conn, peer, ns, body_bytes, trigger=False))
    print(f"\nagree-trigger bench, {body_bytes} byte body, median of {REPS}: "
          f"with {on * 1000:.1f} ms, without {off * 1000:.1f} ms, "
          f"trigger {max(on - off, 0) * 1000:.1f} ms per insert")
    assert on < 5.0, f"one insert of a {body_bytes} byte payload took {on:.2f} s"
