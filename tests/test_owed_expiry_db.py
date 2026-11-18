"""owed_acks reads an ask's expiry from the head row, through the real SQL against the test Postgres (pxx's probe for
#588, 8e09c9; #588 P3-1: the database-free test checked only the query's text). Five asks: a directive, relayed words
with a window, relayed words without one, a plain ask, and a window with no relay."""
import uuid
from apps.mesh import projection, graph
from tests.test_call_routing import (owner_conn, world)  # noqa:F401
from tests.test_mesh_graphread import _mesh_claimant, _row


def test_owed_acks_reads_expiry_from_the_head_row(owner_conn, world):
    claimant = _mesh_claimant(owner_conn)
    label = "probe-" + uuid.uuid4().hex[:8]
    thread = "owed-exp-" + uuid.uuid4().hex[:8]
    me = claimant | {"label": "asker"}
    base = {"to": label, "thread_id": thread, "requires_ack": True, "type": "question", "body": "b"}
    d = _row(owner_conn, me, dict(base, directive={"v": 1, "words": ["go"], "target": label,
                                                   "target_peer": str(uuid.uuid4()),
                                                   "expires_at": "2026-11-11T11:00:00Z"}))["key"]
    r = _row(owner_conn, me, dict(base, operator_words=["go"], window_end="2026-11-11T12:00:00Z"))["key"]
    rn = _row(owner_conn, me, dict(base, operator_words=["go"]))["key"]
    plain = _row(owner_conn, me, dict(base))["key"]
    wn = _row(owner_conn, me, dict(base, window_end="2026-11-11T12:00:00Z"))["key"]
    owner_conn.commit(); projection.rebuild(owner_conn); owner_conn.commit()
    got = {o.hit.key: o.expires_at for o in graph.owed_acks(owner_conn, [label])}
    assert got[d] == "2026-11-11T11:00:00Z"
    assert got[r] == "2026-11-11T12:30:00Z"
    assert got[rn] is None and got[plain] is None and got[wn] is None
