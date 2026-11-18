"""A slow job body is named in the loop's log (10 Nov 2026).

On production, mesh_edges_catch_up (registered at 60 s) ran every 2 to 5 minutes, and jobs.out.log could not say
which job held the serial pass: its lines carry rows, never time. runner.Outcome now records how long the body ran,
and run_jobs prints it for a body of SLOW_BODY_SECS or more; a fast body keeps today's line exactly. Pure: the lease
store and the connection are fakes, no database.
"""
import dataclasses
import types

from apps.jobs import leases, runner
from apps.jobs.management.commands import run_jobs


class _Conn:
    def close(self):
        pass


def _job(name, body):
    return types.SimpleNamespace(name=name, lease_secs=60, timeout_secs=30, interval_secs=60, jitter_frac=0.1,
                                 max_batch=100, run=body)


def _fake_leases(monkeypatch):
    monkeypatch.setattr(runner.leases, "register", lambda conn, name: None)
    monkeypatch.setattr(runner.leases, "claim",
                        lambda conn, name, holder, secs: leases.Claim(acquired=True))
    monkeypatch.setattr(runner.leases, "read_state", lambda conn, name: {})
    monkeypatch.setattr(runner.leases, "release", lambda conn, name, holder, **kw: True)


def test_the_outcome_carries_how_long_the_body_ran(monkeypatch):
    _fake_leases(monkeypatch)
    clock = iter([100.0, 112.5, 200.0, 200.2])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    out = runner.run_due(lambda **kw: _Conn(), jobs=[_job("slow", lambda c, j: 3), _job("fast", lambda c, j: 1)],
                         holder="h")
    assert [(o.job, o.elapsed_s) for o in out] == [("slow", 12.5), ("fast", 0.2)]


def test_a_failed_body_carries_its_time_too(monkeypatch):
    _fake_leases(monkeypatch)
    clock = iter([10.0, 14.0])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))

    def boom(c, j):
        raise RuntimeError("statement timeout")
    out = runner.run_due(lambda **kw: _Conn(), jobs=[_job("bad", boom)], holder="h")
    assert out[0].result == "error" and out[0].elapsed_s == 4.0


def test_the_line_shows_the_time_only_for_a_slow_body():
    def o(elapsed):
        return runner.Outcome(job="j", claimed=True, result="ok", rows=1, elapsed_s=elapsed)
    assert run_jobs._slow(o(12.46)) == " in 12.5s"
    assert run_jobs._slow(o(run_jobs.SLOW_BODY_SECS)) == " in 1.0s"
    assert run_jobs._slow(o(0.99)) == ""                       # a fast body keeps today's line exactly
    assert run_jobs._slow(o(None)) == ""
    assert run_jobs._slow(runner.Outcome(job="j", claimed=False)) == ""


def test_an_unclaimed_outcome_has_no_time():
    assert runner.Outcome(job="j", claimed=False).elapsed_s is None
    assert "elapsed_s" in {f.name for f in dataclasses.fields(runner.Outcome)}
