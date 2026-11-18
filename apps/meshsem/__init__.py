"""Semantic retrieval over shared/mesh-chat, backed by LAN GPU models (divergence 019).

`spaces.py` defines embedding SPACES and the canary admission test, `providers.py` is the
one place mesh content leaves the process (MUST-8 guarded), `embedjob.py` fills
mesh_embeddings on a background job, `store.py` reads and writes those tables, and
`search.py` + `views.py` answer GET /v1/mesh/search and /v1/mesh/context. README.md has
the configuration for this LAN.
"""
