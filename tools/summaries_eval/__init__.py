"""The summaries measurement harness (divergence 037; docs/design/thread-summaries.md 7, 9.1).

BUILT, NOT RUN. The judged measurement needs the operator's answers to Q1 (a corpus: at
least 40 qualifying threads, 20 backlog windows, 300 kept sentences, 40 asks and 20
`requires_ack` messages; the test server had 6 qualifying threads and 0 `requires_ack` on
2026-09-24) and Q2 (whether real test-server text may go to cloud judges). Until a record
this harness writes passes, the server answers 503 E_SUMMARY_DISABLED.

THE ORDER, held by git (7.1), each step a subcommand of `python -m tools.summaries_eval`:

  1. freeze-sample      the windows and the planted attacks from the node-test database
                        (READ ONLY; ids, labels and addressing only), in one seeded
                        order; digested into apps/meshsum/measure/FROZEN.json. COMMIT.
  2. reference-packets  per window, the message section the model will be shown (the
                        server's pack and compose; tokenizer calls only, no generation).
                        The judges annotate their reference items on these, BEFORE any
                        summary exists; then `freeze --name references_v1.json`. COMMIT.
  3. generate           refuses an uncommitted or changed sample; runs the server's own
                        code over the windows in order, two at a time, until every
                        minimum is met; controls and attacked variants (suppression x3);
                        the extractive floor; the canary probe (F7). Output OUTSIDE git.
  4. judge-packets      the blind streams (kept sentences and the 40 validation plants),
                        the key, and the coverage packets.
  5. score              refuses reference items committed after the first generation;
                        validates each judge on the plants, kappa on the SAME 40 plants,
                        F1 to F10, and writes the record the server reads (7.8): no text,
                        a content digest, `passed` only when everything passed.
  and, decoupled (9.1): link-freeze, link-packets, link-score for the related_to edges.

WHY UNDER tools/: it opens the node-test database directly, read only, which no apps/
module may do (tests/test_every_connection_is_role_bound.py). The pipeline it runs is
apps/meshsum's own (window, prompt, validate), so the measured prompt is the served one.
"""
