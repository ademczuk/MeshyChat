"""Mutants for the asker's own close (30 Oct): a `closes` by the ask's author settles it in owed and awaiting.

oxxxxxx could not retire 75 stale asks, because only a close authored by the owing label counted. apps/mesh/graph.py
now also takes a close by the ask's own author (`a.dst`, the head join's authored_by edge), in owed's shared predicate
and in awaiting's copy of it. These remove each half, or widen it to any author:
- C1 drops the asker from owed's predicate: the withdrawn ask stays open in owed_acks and owed_counts.
- C2 drops it from awaiting's: the withdrawn ask stays open in awaiting.
- C3 and C4 let any author close: a third peer's close settles an ask it has nothing to do with.
"""
GRAPH = "apps/mesh/graph.py"
T = ["tests/test_mesh_projection.py::test_the_asker_withdraws_its_own_ask_with_closes_and_nobody_else_can"]
MUTANTS = [
    ("mesh-asker-close:C1 owed ignores the asker's close", GRAPH,
     "AND cc.dst = e.src_key AND (ca.dst = ANY(%s) OR ca.dst = a.dst))",
     "AND cc.dst = e.src_key AND (ca.dst = ANY(%s)))",
     T),
    ("mesh-asker-close:C2 awaiting ignores the asker's close", GRAPH,
     "AND cc.dst = e.src_key AND (ca.dst = ANY(lg.labels) OR ca.dst = a.dst))",
     "AND cc.dst = e.src_key AND (ca.dst = ANY(lg.labels)))",
     T),
    ("mesh-asker-close:C3 owed takes anyone's close", GRAPH,
     "AND cc.dst = e.src_key AND (ca.dst = ANY(%s) OR ca.dst = a.dst))",
     "AND cc.dst = e.src_key AND (ca.dst = ANY(%s) OR TRUE))",
     T),
    ("mesh-asker-close:C4 awaiting takes anyone's close", GRAPH,
     "AND cc.dst = e.src_key AND (ca.dst = ANY(lg.labels) OR ca.dst = a.dst))",
     "AND cc.dst = e.src_key AND (ca.dst = ANY(lg.labels) OR TRUE))",
     T),
]
