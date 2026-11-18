"""GemmaSwarm auditor: a model PROPOSES edges for legacy mesh rows; facts decide.

Divergence 021 (`docs/DIVERGENCES.md`), migration `sql_meshychat/007`. The declared
projection (apps/mesh, divergence 014) cannot place a row that never said who it was to or
which thread it was in. This package asks a LAN chat model to read such a row and SELECT
among candidates, then refuses every proposal that is not an existing fact, and keeps what
survives in `mesh_inferred_edges`, a table the declared projection never reads.
"""
