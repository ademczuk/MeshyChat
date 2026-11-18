"""The mesh as a graph: a rebuildable edge projection over shared/mesh-chat.

`shared_memory` is an event store. This package is its read model. `normalize.py` turns
one row into typed edges (pure, no database), `projection.py` writes them and can rebuild
from scratch, `graph.py` answers the questions the projection exists for. See
docs/DIVERGENCES.md 014 for the measurements that motivated it.
"""
