"""Who cites an artifact (divergence 090, issue #159 phase 2): graph.mentions' hits grouped by the peer that signed
each row. This module is the pure half, `group_by_signer`, with no database; the read itself is in
tests/test_mesh_projection.py and the route in tests/test_mesh_routes.py.

House rules: C1, every negative carries a positive control in the same test.

Run:  pytest tests/test_mesh_custody.py -v
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.mesh import graph as G  # noqa: E402

_T0 = _dt.datetime(2026, 10, 7, 12, 0, tzinfo=_dt.timezone.utc)


def _hit(key: str, author: str | None, minutes: int, *, ambiguous: bool = False) -> G.MentionHit:
    return G.MentionHit(G.Hit(key=key, version=1, edge_type="mentions", dst="hash:09cb6def", src_field="body",
                              recorded_at=_T0 + _dt.timedelta(minutes=minutes), author=author), ambiguous)


def test_one_seat_under_two_labels_is_one_holder_and_the_latest_citer_comes_first():
    """MUTANT: group by label; the aliased seat splits into two holders. MUTANT: order holders by first citation;
    the seat that cited it last is no longer first."""
    # dxxxxx cited it once, at 25; oxxxxxx first at 10 and last at 30, so it is the latest citer.
    hits = [_hit("msg/3", "oxxxxxx", 30), _hit("msg/4", "dxxxxx", 25), _hit("msg/2", "oxxxxxx-builder", 20),
            _hit("msg/1", "oxxxxxx", 10, ambiguous=True)]
    signers = {"msg/4": ("peer-b", "answer"), "msg/3": ("peer-m", "status"), "msg/2": ("peer-m", None),
               "msg/1": ("peer-m", "status")}
    oxxxxxx, dxxxxx = G.group_by_signer(hits, signers)
    assert dxxxxx.peer_id == "peer-b" and dxxxxx.labels == ("dxxxxx",) and dxxxxx.mentions == 1
    assert oxxxxxx.peer_id == "peer-m" and oxxxxxx.labels == ("oxxxxxx", "oxxxxxx-builder")
    assert oxxxxxx.mentions == 3 and oxxxxxx.ambiguous == 1
    assert oxxxxxx.keys == ("msg/3", "msg/2", "msg/1")
    assert oxxxxxx.first_at == _T0 + _dt.timedelta(minutes=10) and oxxxxxx.last_at == _T0 + _dt.timedelta(minutes=30)
    assert oxxxxxx.types == {"status": 2, "untyped": 1}


def test_a_row_without_a_signer_is_grouped_never_dropped():
    """MUTANT: skip hits whose key has no signer; a citation disappears from the count."""
    hits = [_hit("msg/2", "pxx", 20), _hit("msg/1", None, 10)]
    held = G.group_by_signer(hits, {"msg/2": ("peer-n", "request"), "msg/1": (None, None)})
    assert [(h.peer_id, h.mentions) for h in held] == [("peer-n", 1), (None, 1)]
    assert held[1].labels == () and held[1].types == {"untyped": 1}
    assert G.group_by_signer([_hit("msg/9", "pxx", 1)], {})[0].peer_id is None, "a key the lookup missed"


def test_a_holder_lists_its_newest_keys_and_no_more_than_the_bound():
    """MUTANT: keep the oldest keys; the reader is pointed at stale citations. MUTANT: drop the bound; a prolific
    citer's whole history goes on the wire."""
    hits = [_hit(f"msg/{i}", "kxxx", i) for i in range(12, 0, -1)]
    (kxxx,) = G.group_by_signer(hits, {f"msg/{i}": ("peer-j", "status") for i in range(1, 13)})
    assert kxxx.mentions == 12 and kxxx.keys == tuple(f"msg/{i}" for i in range(12, 12 - G.HOLDER_KEYS, -1))
    assert G.group_by_signer([], {}) == ()
