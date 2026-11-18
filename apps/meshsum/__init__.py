"""Summaries of a mesh thread, or of a node's unread backlog, by HQB's chat model.

Divergence 037; the design is docs/design/thread-summaries.md. Propose-only and labelled:
every 200 says `tier: "generated"` and `unverified: true`, every sentence cites the
messages it rests on, obligations come from the declared graph and never from the model,
and nothing here writes an edge, a verdict or any stored summary text. Served only for a
(summary_version, model_pin) that passed its measurement (apps/meshsum/record.py).
"""
