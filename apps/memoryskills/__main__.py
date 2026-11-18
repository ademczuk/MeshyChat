"""CLI for the legacy verdicts of divergence 039 (apps/memoryskills/legacy_verdict.py).

    python -m apps.memoryskills census [--dsn DSN]        # measurement 3.5.2: counts only,
                                                          # read-only, refuses a standby
    python -m apps.memoryskills counts [--dsn DSN]        # the stored verdicts, counted
    python -m apps.memoryskills catch-up [--max-batch N]  # one tick of the job

A package entry point rather than `-m apps.memoryskills.legacy_verdict`: the package
imports that module (handlers.py reads verdicts), so running it as __main__ would load it
twice. Exit codes: 0 done, 2 could not parse the arguments.
"""
from apps.memoryskills.legacy_verdict import main

raise SystemExit(main())
