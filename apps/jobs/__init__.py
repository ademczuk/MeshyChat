"""Background jobs: the scheduler docs/STATUS.md records as absent.

Background work (janitors, nonce sweeps, frame audit, session expiry) runs as
lease-claimed jobs, so several instances can tick without double-running a job; see
docs/DIVERGENCES.md 002.
"""
