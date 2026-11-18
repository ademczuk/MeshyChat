# Local phase B fixture evidence

Thursday, 5 November 2026, HQD. Source-only candidate; no installation or live
notice challenge. All runs were serial, one pure unittest file at a time, via
`.cmd` launchers using `start /low /b /wait`. No database tests or full gate.
Production `/v1/livez` returned HTTP 200 and `status: ok` before/after each file;
the mutation driver checked it around each case and would stop on failure.

| Command | Result |
| --- | --- |
| `python -m unittest tools.postbox.handover.test_watch -v` | 36 tests, OK |
| `python -m unittest tools.postbox.handover.test_handover -v` | 41 tests, OK |
| `python -m tools.postbox.handover.check_watch_mutants --check-hqd-livez` | 12/12 assertion-detected |
| `git diff --check` | Passed |
| `python -m unittest discover -s tests -p test_handover_gate.py -v` | 3 wrapper tests, OK; serially executed 41 core + 36 runner cases and 12 mutants |

The wrapper check used a `.cmd` launcher with `start /low /b /wait`, with livez
HTTP 200/status ok before and after. It did not invoke pytest, its conftest,
a database or the full gate on HQD. This reran the unchanged core and current
runner/mutants through the same adapter the HQB `tests/` gate will collect.
Full gate collection and certification remain HQB's work. Nested skips are
failures, not hidden successes. The mutation subprocess deliberately has no
HQD health flag; that flag only adds health probes, not different mutations.

Mutants: replay a delivered notice; accept another session's metadata; omit the
pre-allocation route recheck; accept an unverified signed-result flag; accept a
wrong recipient; invent accounting qualification; retry an expired operation;
dispatch while disabled; accept another storage receipt; replay an unresolved
old generation. Every selected control produced an `AssertionError` failure.
No import/runtime error or timeout was counted as mutant detection.

Fix round 1 closes the two reviewed P2 defects: A-to-B-to-A route resurrection
and starvation by a long combined degraded reason. Both were reproduced before
the fix, then covered by regression tests and two additional assertion-detected
mutants. The pre-fix regression run had one assertion failure and one uncaught
HOLD; that run was not counted as mutant evidence. The successful mutation
controls both fail on assertions. Test temporary roots resolve the macOS /tmp
link before applying the production no-reparse policy. Missing enabled fields
now follow the installed postbox default (enabled), while explicit false is
still refused. Conservative HOLD for never-sent old prepared entries remains
an owner-reconciliation limitation, not an automatic journal rewrite.

The real-observer fixtures independently exercise two high post-compaction
floors with accounting still unqualified, and a three-compaction same-task
REQUEST with no ratio qualification. Crash fixtures cover before dispatch,
after remote delivery, and failure committing the delivery record. They prove
reuse of the same frozen operation/body, not actual network exactly-once
delivery or Windows power-loss durability. Sender subprocess arguments are
checked with a fake process; no real sender is invoked by fixture tests.

Source bytes tested (SHA256, before their source commit; wrapper added in the coverage round):

```text
559e29b144048a917ebf648ab9c4f09c1e54e838259748d973ba471f522e3a0d  tests/test_handover_gate.py
b23368572c54cc34ad3d0170cd3b7b683e63c96dad2f203d1cdad7cd73c859b8  watch.py
fc9dbbe28e458679afcbde5a68d8c473dab08b8688a303a84b8731d67f6cab51  test_watch.py
35f705d7b5c7b0686ae943b510e8b2b269f911db754803c95254065dd1438f46  check_watch_mutants.py
8f450ef14d901a6625e290a5536345b48de2d71479278d13b61f4c282332af59  __main__.py
```

Durable HQD logs are under `C:/Users/user/meshychat/staged-20261105/`:
`phase-b-watch-tests.log`, `phase-b-core-tests.log`, and
`phase-b-watch-mutants.log`. The first fixture run caught a fixture metadata
timestamp omission; it was fixed before the passing runs reported above.
This is not the HQB gate. Reviewer/gate results and live qualification remain
outstanding; see the installation proposal for the exact limits.
