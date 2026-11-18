"""mesh_edges_catch_up ticks on a lane of its own (15 Nov 2026). Database-free.

Every ask's owed state is read from mesh_edges (apps/mesh/graph.py owed_acks), and mesh_edges_catch_up is the only
writer of that table (apps/mesh/projection.py). In `run_jobs --loop`'s shared serial pass a slow job held it: on
production it ran every 2 to 5 minutes (jobs.out.log gaps up to 309 s on 10 Nov 2026), so a seat that had answered
an ask was told minutes later that it still owed the reply. On its own lane no other job can hold it.

  1. The job is registered own_lane, with its interval, timeout and batch unchanged.
  2. `run_jobs --loop` itself (Command.handle, not a lane built by the test) ticks the job on a thread of its own
     while a job in the shared pass is stalled. It fails if handle does not start the lane, and if the job is in the
     shared pass. CONTROLS: the stalled shared job did start, it is not on a lane, and it never ticked with this job.
"""
import contextlib
import dataclasses
import threading

from apps.jobs import registry
from apps.jobs.management.commands import run_jobs as cmd
from apps.mesh import projection as mesh_projection

JOB = "mesh_edges_catch_up"


def test_1_the_projection_job_has_its_own_lane_and_its_schedule_is_unchanged():
    job = registry.by_name(JOB)
    assert job.own_lane is True
    assert (job.interval_secs, job.timeout_secs, job.jitter_frac) == (60, 20, 0.2)
    assert job.max_batch == mesh_projection.DEFAULT_BATCH
    assert job.run is mesh_projection.run_catch_up_job


def test_2_run_jobs_loop_ticks_the_projection_on_its_own_thread_while_the_shared_pass_is_stalled(monkeypatch):
    edges = registry.by_name(JOB)
    slow = dataclasses.replace(registry.by_name("envelope_nonce_sweep"), name="slow_shared_job")
    assert slow.own_lane is False                      # control: the stalled job is in the shared pass
    monkeypatch.setattr(registry, "JOBS", (slow, edges))
    monkeypatch.delenv("MESHY_JOBS_EXCLUDE", raising=False)

    release, slow_started, edges_ticked = threading.Event(), threading.Event(), threading.Event()
    ticks = []                                         # (job names of one tick, the thread it ran on)

    def fake_tick(self, jobs, *, as_json):
        names = tuple(j.name for j in jobs)
        ticks.append((names, threading.current_thread().name))
        if "slow_shared_job" in names:                 # the shared pass, whatever else it holds
            slow_started.set()
            release.wait(10)                           # a slow body holding the pass
            return 0
        edges_ticked.set()
        return 0

    real_sleep = cmd.time.sleep

    def stop_after_the_stall(secs):                    # the loop's own sleep between shared passes
        if threading.current_thread().name == "the-loop":
            raise KeyboardInterrupt
        real_sleep(secs)                               # any other thread sleeps as usual

    @contextlib.contextmanager
    def no_database():
        yield None

    monkeypatch.setattr(cmd.Command, "_tick", fake_tick)
    monkeypatch.setattr(cmd.dbconn, "serving_connection", no_database)
    monkeypatch.setattr(cmd.schema_sets, "assert_schema_complete", lambda conn: None)
    monkeypatch.setattr(cmd.time, "sleep", stop_after_the_stall)

    ended = []

    def run_the_loop():
        try:
            cmd.Command().handle(job=None, exclude=None, json=False, run_now=False, release_lease=False,
                                 explain=False, loop=True, interval=0.05)
        except SystemExit as exc:                      # the loop's exit after the KeyboardInterrupt
            ended.append(exc.code)

    loop = threading.Thread(target=run_the_loop, name="the-loop", daemon=True)
    loop.start()
    try:
        assert slow_started.wait(5), "the shared pass never started"
        assert edges_ticked.wait(5), "run_jobs --loop did not tick the projection while the shared pass was stalled"
        assert not release.is_set()                    # the shared job is still stalled at that moment
    finally:
        release.set()
        loop.join(10)
    assert not loop.is_alive() and ended == [0]
    lane_ticks = [t for t in ticks if t[0] == (JOB,)]
    assert lane_ticks and all(thread == "run_jobs-" + JOB for _, thread in lane_ticks)
    assert all(JOB not in names for names, _ in ticks if "slow_shared_job" in names)   # never in the shared pass
    assert [thread for names, thread in ticks if "slow_shared_job" in names] == ["the-loop"]
