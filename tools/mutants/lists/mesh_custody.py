"""Mutants of the holders of a mention (apps/mesh/graph.py) and of `owed --by-peer` (tools/meshy_node).

Each mutant name is `<set>:<id> <description>`; `<set>` is its mutation-set name. A mutant held
by more than one set is listed once under the first. The before and after texts are
literals, unchanged, except where a comment says the text was BLIND at 1a1ea9f and gives the
same mutation on today's code. Run: python tools/mutants/run.py --list mesh_custody
"""

GRAPH = "apps/mesh/graph.py"
NODE = "tools/meshy_node/node.py"
CLI = "tools/meshy_node/cli.py"

T_CUSTODY = ["tests/test_mesh_custody.py", "tests/test_meshy_node_owed.py::test_mentions_by_peer_asks_for_the_holders_and_only_when_asked"]

MUTANTS = [
    ('mutate3:M1 group by label', GRAPH,
     "        groups.setdefault(signers.get(h.hit.key, (None, None))[0], []).append(h)",
     "        groups.setdefault(h.hit.author, []).append(h)",
     T_CUSTODY),
    ('mutate3:M2 order by first', GRAPH,
     "    return tuple(sorted(holders, key=lambda x: (x.last_at, x.peer_id or \"\"), reverse=True))",
     "    return tuple(sorted(holders, key=lambda x: (x.first_at, x.peer_id or \"\"), reverse=True))",
     T_CUSTODY),
    ('mutate3:M3 drop unsigned', GRAPH,
     "    for h in hits:\n        groups.setdefault(",
     "    for h in hits:\n        if signers.get(h.hit.key, (None, None))[0] is None:\n            continue\n        groups.setdefault(",
     T_CUSTODY),
    ('mutate3:M4 oldest keys', GRAPH,
     "            keys=tuple(h.hit.key for h in rows[:HOLDER_KEYS]),",
     "            keys=tuple(h.hit.key for h in rows[-HOLDER_KEYS:]),",
     T_CUSTODY),
    ('mutate3:M5 no key bound', GRAPH,
     "            keys=tuple(h.hit.key for h in rows[:HOLDER_KEYS]),",
     "            keys=tuple(h.hit.key for h in rows),",
     T_CUSTODY),
    ('mutate3:HZ no ambiguous count', GRAPH,
     "ambiguous=sum(1 for h in rows if h.ambiguous)",
     "ambiguous=0",
     T_CUSTODY),
    ('mutate3:C1 drop by', NODE,
     "    if by is not None:\n        params[\"by\"] = by\n",
     "",
     T_CUSTODY),
    ('mutate3:C2 always by', CLI,
     "by=\"peer\" if args.by_peer else None",
     "by=\"peer\"",
     T_CUSTODY),
]
