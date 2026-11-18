"""tools/postbox run_kimi_delivery: a kimi seat gets one headless turn per paced batch (24 Oct 2026).

nxxxxx's kimi TUI handled no mail from 20 Oct: kimi-code takes no outside input into a running TUI, and a resumed
session has no waiter until someone types (thread <thread>). The postbox now runs
`kimi -S <session> -p <notice>` per batch. The loop tests drive it with injected seams (turn, sessions_open, wire,
sleep, reap), never the shared time module, so POSIX subprocess waits are untouched (pxx eac28e). Temporary spools
only, no kimi CLI, no network. The tests marked REAL run stand-in subprocesses: the timeout tree kill, the process
scan, the leftover-turn reaper and a daemon killed mid-turn.

kxxx's conditions (b9e7c5, bfda53): the turn refuses while any process has the session open; nothing is marked
delivered on a turn that didn't complete (exit 0 is not proof: the session's wire log must gain a completed turn);
a nonzero exit or a timeout leaves the batch banked; a daemon killed mid-turn leaves nothing wedged.
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_postbox_reconcile import pb, ident  # noqa: F401

SESSION = "session_a395661f-e3e4-4cc8-84c3-40e96c290f8c"
BASE_PY = getattr(sys, "_base_executable", sys.executable)   # a venv's python.exe is a launcher with a second process


def message(key, **kw):
    return dict(event='message', role='to', key=key, version=1, created_at='2026-10-24T07:00:00Z',
                **{'from': 'peer', 'from_peer_id': 'p1234567'}, **kw)


def append(log, *events):
    with open(log, 'ab') as f:
        for event in events:
            f.write(json.dumps(event).encode() + b'\n')


class Stop(Exception):
    pass


@pytest.fixture
def here(pb, tmp_path, monkeypatch):
    """The postbox folder (session locks and turn records) is a temp folder, never the repo's tools/postbox."""
    d = tmp_path / 'postbox'
    d.mkdir()
    # POSIX wraps a turn in HERE/postbox_child.py (postbox_os.listener_argv), so the temp folder needs it (pxx 5341f1 #6)
    (d / 'postbox_child.py').write_bytes((Path(pb.__file__).parent / 'postbox_child.py').read_bytes())
    monkeypatch.setattr(pb, 'HERE', str(d))
    return d


def seat(tmp_path, **kw):
    work = tmp_path / 'work'
    work.mkdir(exist_ok=True)
    fields = dict(cli='kimi', kimi_session=SESSION, kimi_cwd=str(work), kimi_argv=['node', 'main.mjs'],
                  codex_settle_s=0, codex_min_gap_s=60)
    fields.update(kw)
    i = ident(tmp_path, **fields)
    Path(i['log']).touch()
    Path(i['log'] + '.kimi.offset').write_text('0')
    return i


def psutil_present(monkeypatch):
    """kimi_config_error requires psutil; the test venv may not have it, and the loop tests never scan for real."""
    try:
        import psutil  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace())


class FakeWire:
    """completed() answers from `results` in order (default: a completed turn)."""
    def __init__(self, results=()):
        self.results, self.marks = list(results), 0

    def mark(self):
        self.marks += 1
        return ("wire", self.marks)

    def completed(self, mark):
        return self.results.pop(0) if self.results else (True, None)


def drive(pb, i, turns, open_by=lambda n: [], during=lambda seconds, n: None, max_sleeps=30, wire=None,
          reap=lambda session: "none", monkeypatch=None):
    """Run the loop until every scripted turn result in `turns` is used. Returns (calls, statuses)."""
    calls, sleeps, statuses = [], [], []
    results = list(turns)

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) <= max_sleeps, 'loop spun: %r' % sleeps
        if not results and len(calls) == len(turns):
            raise Stop()
        during(seconds, len(calls))

    def turn(argv, cwd, env, timeout, on_start=None):
        calls.append(dict(argv=argv, cwd=cwd, env=env, timeout=timeout))
        return results.pop(0)

    def sessions_open(text):
        r = open_by(len(calls))
        if isinstance(r, Exception):
            raise r
        return r

    psutil_present(monkeypatch)
    monkeypatch.setattr(pb, 'graphline', None)
    monkeypatch.setattr(pb, 'owedline', None)
    real_status = pb.set_status
    monkeypatch.setattr(pb, 'set_status', lambda name, **kw: (statuses.append(kw), real_status(name, **kw)))
    with pytest.raises(Stop):
        pb.run_kimi_delivery(i, turn=turn, sessions_open=sessions_open, wire=wire or FakeWire(), sleep=sleep,
                             reap=reap)
    return calls, statuses


def errors(statuses):
    return [s.get('kimi_error') or '' for s in statuses if s.get('kimi') == 'retrying']


# ---- the loop ---------------------------------------------------------------------------------------------------

def test_one_batch_is_one_headless_turn_on_the_session(pb, here, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/one'), message('msg/two'))
    calls, statuses = drive(pb, i, [(0, 'ok')], monkeypatch=monkeypatch)
    assert len(calls) == 1
    c = calls[0]
    assert c['argv'][:5] == ['node', 'main.mjs', '-S', SESSION, '-p'] and c['argv'][-1] == '--yolo'
    notice = c['argv'][5]
    assert notice.startswith(pb.KIMI_HEADLESS_NOTE) and 'msg/one' in notice and 'msg/two' in notice
    assert c['env']['MESHY_KIMI_HEADLESS'] == SESSION and c['cwd'] == i['kimi_cwd']
    assert c['timeout'] == pb.KIMI_TURN_TIMEOUT_S
    assert any(s.get('kimi') == 'ok' and s.get('kimi_batch_count') == 2 for s in statuses)


def test_a_headless_turn_carries_the_act_policy_and_none_of_the_old_defer_lines(pb, here, tmp_path, monkeypatch):
    """#446 (nxxxxx's review): the headless kimi turn shares the Codex policy, so the real run_kimi_delivery notice is
    checked for the new act rules and for the absence of the defer/hold lines that stalled Codex seats."""
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    calls, _ = drive(pb, i, [(0, 'ok')], monkeypatch=monkeypatch)
    notice = ' '.join(calls[0]['argv'][5].split())
    assert 'Act on each authenticated peer request in this turn' in notice
    assert 'Recording a request as deferred is not handling it' in notice
    for gone in ('Defer unrelated peer work', 'If no active operator task remains, hold',
                 'If waiting for operator input, restate the outstanding operator question'):
        assert gone not in notice, gone


def test_exit_0_without_a_completed_turn_is_not_delivered(pb, here, tmp_path, monkeypatch):
    # pxx eac28e #1, kxxx b9e7c5: kimi_argv [python, -c, "raise SystemExit(0)"] used to count as delivered
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    wire = FakeWire([(False, 'the turn exited 0 but the session gained no turn.prompt'), (True, None)])
    calls, statuses = drive(pb, i, [(0, ''), (0, 'ok')], wire=wire, monkeypatch=monkeypatch)
    assert len(calls) == 2 and all('msg/one' in c['argv'][5] for c in calls)        # delivered again
    assert any('gained no turn.prompt' in e for e in errors(statuses))


def test_a_failed_turn_leaves_the_batch_banked_and_is_retried(pb, here, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    calls, statuses = drive(pb, i, [(1, 'boom'), (0, 'ok')], monkeypatch=monkeypatch)
    assert len(calls) == 2 and 'msg/one' in calls[1]['argv'][5]
    assert any('rc=1' in e for e in errors(statuses))


def test_a_timed_out_turn_leaves_the_batch_banked(pb, here, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    calls, statuses = drive(pb, i, [(None, ''), (0, 'ok')], monkeypatch=monkeypatch)
    assert len(calls) == 2 and 'msg/one' in calls[1]['argv'][5]
    assert any('timed out' in e for e in errors(statuses))


def test_nothing_runs_while_another_process_has_the_session_open(pb, here, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    scans = []

    def open_by(n):
        scans.append(n)
        return [(24816, 'node.exe')] if len(scans) <= 2 else []
    calls, statuses = drive(pb, i, [(0, 'ok')], open_by=open_by, monkeypatch=monkeypatch)
    assert len(calls) == 1 and len(scans) == 3
    assert len(errors(statuses)) == 2 and 'pid 24816 (node.exe)' in errors(statuses)[0]


def test_an_unreadable_process_table_refuses_instead_of_assuming_free(pb, here, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    scans = []

    def open_by(n):
        scans.append(n)
        return RuntimeError('cannot read the command line of pid 7 (node.exe)') if len(scans) == 1 else []
    calls, statuses = drive(pb, i, [(0, 'ok')], open_by=open_by, monkeypatch=monkeypatch)
    assert len(calls) == 1 and len(scans) == 2
    assert any('could not read the process table' in e for e in errors(statuses))


def test_a_missing_wire_log_refuses(pb, here, tmp_path, monkeypatch):
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))

    class NoWire(FakeWire):
        def mark(self):
            self.marks += 1
            if self.marks == 1:
                raise RuntimeError('found 0 wire logs')
            return ('wire', self.marks)
    calls, statuses = drive(pb, i, [(0, 'ok')], wire=NoWire(), monkeypatch=monkeypatch)
    assert len(calls) == 1 and any("cannot find the session's wire log" in e for e in errors(statuses))


def test_the_session_lock_is_shared_by_every_identity_naming_the_session(pb, here, tmp_path, monkeypatch):
    # pxx eac28e #3: the lock is keyed to the session in the postbox folder, not to an identity's log
    from tests.test_postbox_kimi_arm_hook import holder
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    assert pb.kimi_session_lock_path(SESSION) == os.path.join(str(here), 'kimi-%s.lock' % SESSION)
    p = holder(Path(pb.kimi_session_lock_path(SESSION)))
    released = []

    def during(seconds, n):
        if seconds == 60 and not released:
            p.kill(); p.wait(timeout=10); released.append(1)
    try:
        calls, statuses = drive(pb, i, [(0, 'ok')], during=during, monkeypatch=monkeypatch)
    finally:
        if p.poll() is None:
            p.kill(); p.wait(timeout=10)
    assert len(calls) == 1 and any('kimi-%s.lock' % SESSION in e for e in errors(statuses))


def test_two_identities_naming_one_session_get_no_delivery(pb):
    ids = [dict(name='a', cli='kimi', kimi_session=SESSION), dict(name='b', cli='kimi', kimi_session=SESSION),
           dict(name='c', cli='kimi', kimi_session=SESSION.replace('a3', 'b4')), dict(name='d', cli='codex')]
    assert pb.duplicate_kimi_sessions(ids) == {SESSION: ['a', 'b']}
    src = Path(pb.__file__).read_text(encoding='utf-8')
    assert 'kimi_dups = duplicate_kimi_sessions(cfg["identities"])' in src and 'in kimi_dups:' in src


def test_a_leftover_turn_is_reaped_before_the_scan(pb, here, tmp_path, monkeypatch):
    # kxxx bfda53: a leftover turn past its timeout still names the session, so it must be ended before the scan
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    order, answers = [], iter(["none", "running", "killed"])

    def reap(session):
        order.append('reap')
        return next(answers, "none")

    def open_by(n):
        order.append('scan')
        return []
    calls, statuses = drive(pb, i, [(0, 'ok')], open_by=open_by, reap=reap, monkeypatch=monkeypatch)
    assert order[:4] == ['reap', 'reap', 'reap', 'scan']        # start, refused while running, killed, then scan
    assert any('still inside its timeout' in e for e in errors(statuses)) and len(calls) == 1


@pytest.mark.parametrize('field,value,why', [
    ('kimi_session', 'nxxxxx', 'not a kimi-code session id'),
    ('kimi_cwd', '/no/such/folder', 'not a folder'),
    ('kimi_argv', [], 'non-empty list'),
    ('kimi_timeout_s', 5, 'not 60..7200'),
])
def test_a_bad_config_starts_no_delivery(pb, here, tmp_path, monkeypatch, field, value, why):
    psutil_present(monkeypatch)                                       # so the field under test decides
    i = seat(tmp_path)
    i[field] = value
    statuses = []
    monkeypatch.setattr(pb, 'set_status', lambda name, **kw: statuses.append(kw))
    pb.run_kimi_delivery(i, turn=lambda *a, **k: pytest.fail('ran a turn'), sessions_open=lambda t: [],
                         wire=FakeWire(), sleep=lambda s: pytest.fail('looped'), reap=lambda s: 'none')
    assert statuses and statuses[0]['kimi'] == 'config error' and why in statuses[0]['kimi_error']


def test_a_cmd_shim_is_refused_on_windows(pb, tmp_path, monkeypatch):
    psutil_present(monkeypatch)
    i = seat(tmp_path, kimi_argv=['kimi'])
    monkeypatch.setattr(pb.os, 'name', 'nt')
    why = pb.kimi_config_error(i, which=lambda p: r'C:\Users\x\AppData\Roaming\npm\kimi.CMD')
    assert why and '8191' in why
    assert pb.kimi_config_error(i, which=lambda p: r'C:\Program Files\nodejs\node.exe') is None


def test_main_starts_kimi_delivery_only_with_a_session(pb):
    src = Path(pb.__file__).read_text(encoding='utf-8')
    assert 'if ident["cli"] == "kimi" and ident.get("kimi_session"):' in src


# ---- the wire-log evidence (real files) -------------------------------------------------------------------------

def wire_home(tmp_path):
    d = tmp_path / 'kimi-home' / 'sessions' / 'wd_x_123' / SESSION / 'agents' / 'main'
    d.mkdir(parents=True)
    w = d / 'wire.jsonl'
    w.write_text('{"type":"turn.prompt","turnId":0}\n{"type":"turn.ended","turnId":0,"reason":"completed"}\n')
    return tmp_path / 'kimi-home', w


def test_the_wire_check_needs_a_new_prompt_and_a_completed_end(pb, tmp_path):
    home, w = wire_home(tmp_path)
    kw = pb.KimiWire(dict(kimi_session=SESSION, kimi_home=str(home)))
    for appended, ok in (('', False),
                         ('{"type":"turn.ended","turnId":1,"reason":"completed"}\n', False),     # no new prompt
                         ('{"type":"turn.prompt","turnId":1}\n', False),
                         ('{"type":"turn.prompt","turnId":1}\n{"type":"turn.ended","turnId":1,"reason":"cancelled"}\n',
                          False),
                         ('{"type":"turn.prompt","turnId":1}\n{"type":"turn.ended","turnId":1,"reason":"completed"}\n',
                          True)):
        w.write_text('{"type":"turn.prompt","turnId":0}\n{"type":"turn.ended","turnId":0,"reason":"completed"}\n')
        mark = kw.mark()
        with open(w, 'a') as f:
            f.write(appended)
        assert kw.completed(mark)[0] is ok, appended
    mark = kw.mark()
    w.write_text('')
    assert kw.completed(mark) == (False, 'the wire log shrank during the turn (%d to 0 bytes)' % mark[1])


def test_the_wire_log_must_be_found_exactly_once(pb, tmp_path):
    home, w = wire_home(tmp_path)
    kw = pb.KimiWire(dict(kimi_session=SESSION, kimi_home=str(home)))
    other = home / 'sessions' / 'wd_y_456' / SESSION / 'agents' / 'main'
    other.mkdir(parents=True)
    (other / 'wire.jsonl').write_text('')
    with pytest.raises(RuntimeError, match='found 2 wire logs'):
        kw.mark()
    with pytest.raises(RuntimeError, match='found 0 wire logs'):
        pb.KimiWire(dict(kimi_session=SESSION, kimi_home=str(tmp_path / 'nowhere'))).mark()


# ---- REAL: timeout kill, process scan, reaper, daemon killed mid-turn -------------------------------------------

def pid_alive(pid):
    if os.name == 'nt':
        r = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_dead(pid, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.5)
    return not pid_alive(pid)


def needs_psutil_on_posix():
    """The kill and reaper tests below rely on psutil off Windows. Production always has it there: kimi_config_error
    refuses a kimi seat without psutil, and the shim runs under the daemon's own interpreter (listener_argv). Without
    it, POSIX falls back to a process-group kill, which leaves a zombie listed, and to whole-second ps start times,
    which miss the 0.01 s tolerance; both are weaker by design and unused by the kimi delivery."""
    if os.name != 'nt':
        pytest.importorskip('psutil', reason='POSIX without psutil: the kimi delivery refuses to run there')


def test_real_a_timed_out_turn_kills_its_whole_tree(pb, here, tmp_path):
    needs_psutil_on_posix()
    marker = tmp_path / 'child.pid'
    code = ("import subprocess, sys, time; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
            "open(sys.argv[1], 'w').write(str(c.pid)); time.sleep(120)")
    started = []
    t0 = time.monotonic()
    rc, out = pb.kimi_turn([BASE_PY, '-c', code, str(marker)], str(tmp_path), dict(os.environ), 3,
                           on_start=started.append)
    assert rc is None and time.monotonic() - t0 < 60 and len(started) == 1
    assert 'NOT confirmed killed' not in out
    assert wait_dead(int(marker.read_text())), 'the grandchild outlived the timed-out turn'


@pytest.mark.parametrize('use_psutil', [True, False])
def test_real_the_process_scan_finds_the_session_and_never_itself(pb, monkeypatch, use_psutil):
    import postbox_os
    if not use_psutil:
        monkeypatch.setitem(sys.modules, 'psutil', None)
    else:
        pytest.importorskip('psutil')
    token = 'session_00000000-0000-4000-8000-%012d' % os.getpid()
    p = subprocess.Popen([BASE_PY, '-c', 'import time; time.sleep(60)', token])
    try:
        time.sleep(1)
        # without psutil the CIM fallback cannot read some node command lines on HQB, so the strict names are
        # left out here; the kimi delivery itself refuses to start without psutil (next test)
        strict = postbox_os.SCAN_STRICT_NAMES if use_psutil else ()
        assert [pid for pid, _ in postbox_os.processes_naming(token, strict_names=strict)] == [p.pid]
    finally:
        p.kill(); p.wait(timeout=10)
    assert postbox_os.processes_naming(token, strict_names=strict) == []


def test_kimi_delivery_needs_psutil(pb, tmp_path, monkeypatch):
    i = seat(tmp_path)
    monkeypatch.setitem(sys.modules, 'psutil', None)
    assert 'needs psutil' in pb.kimi_config_error(i)


def test_an_unreadable_node_command_line_refuses_but_another_name_does_not(pb, monkeypatch):
    # pxx eac28e #4: psutil gives cmdline None for access denied or a zombie
    import postbox_os

    def fake(rows):
        procs = [SimpleNamespace(info={'pid': 9000 + n, 'name': name, 'cmdline': argv})
                 for n, (name, argv) in enumerate(rows)]
        return SimpleNamespace(process_iter=lambda attrs: iter(procs))
    monkeypatch.setitem(sys.modules, 'psutil', fake([('node.exe', None), ('python.exe', ['x'])]))
    with pytest.raises(RuntimeError, match=r'pid 9000 \(node.exe\)'):
        postbox_os.processes_naming('session_x')
    monkeypatch.setitem(sys.modules, 'psutil', fake([('svchost.exe', None), ('node.exe', ['node', 'session_x'])]))
    assert postbox_os.processes_naming('session_x') == [(9001, 'node.exe')]


def loose_scan(session):
    """The real scan without the strict node rule: the repo venv has no psutil, and its CIM fallback cannot read some
    node command lines on HQB. Production requires psutil (kimi_config_error), so only tests use this."""
    import postbox_os
    return postbox_os.processes_naming(session, strict_names=())


def sleeper():
    return subprocess.Popen([BASE_PY, '-c', 'import time; time.sleep(120)'])


def test_real_the_reaper_kills_past_its_timeout_and_leaves_a_running_turn(pb, here):
    needs_psutil_on_posix()
    import postbox_os
    p = sleeper()
    try:
        created = postbox_os.process_create_time(p.pid)
        record = Path(pb.kimi_turn_record_path(SESSION))
        record.write_text(json.dumps({'pid': p.pid, 'create_time': created, 'started_at': time.time(),
                                      'timeout_s': 1000}))
        assert pb.reap_leftover_turn(SESSION) == 'running' and record.exists() and pid_alive(p.pid)
        record.write_text(json.dumps({'pid': p.pid, 'create_time': created + 50, 'started_at': 0, 'timeout_s': 60}))
        assert pb.reap_leftover_turn(SESSION, sessions_open=loose_scan) == 'stale'
        assert not record.exists() and pid_alive(p.pid)
        record.write_text(json.dumps({'pid': p.pid, 'create_time': created, 'started_at': time.time() - 120,
                                      'timeout_s': 60}))
        assert pb.reap_leftover_turn(SESSION) == 'killed' and not record.exists()
        assert wait_dead(p.pid)
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)
    assert pb.reap_leftover_turn(SESSION) == 'none'
    Path(pb.kimi_turn_record_path(SESSION)).write_text('not json')
    assert pb.reap_leftover_turn(SESSION) == 'unknown'


DAEMON = textwrap.dedent("""
    import importlib.util, json, os, sys, time
    spec = importlib.util.spec_from_file_location('pb_daemon', sys.argv[1])
    pb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pb)
    pb.HERE = sys.argv[2]
    grand = ("import subprocess, sys, time; "
             "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], **({'creationflags': 0x00000008} "
             "if sys.platform == 'win32' else {'start_new_session': True})); "
             "open(sys.argv[1], 'w').write(str(g.pid)); time.sleep(120)")
    turn_argv = [sys.argv[3], '-c', grand, os.path.join(sys.argv[2], 'grandchild.pid')]
    def on_start(pid):
        pb.write_turn_record(sys.argv[4], pid, 600)
        print('TURN', pid, flush=True)
    pb.kimi_turn(turn_argv, sys.argv[2], dict(os.environ), 600, on_start=on_start)
""")


def test_real_a_daemon_killed_mid_turn_leaves_nothing_wedged(pb, here):
    # kxxx bfda53: kill the daemon mid-turn with a stand-in; the turn must not outlive it, and its record is reaped
    script = here / 'daemon.py'
    script.write_text(DAEMON)
    d = subprocess.Popen([BASE_PY, str(script), pb.__file__, str(here), BASE_PY, SESSION],
                         stdout=subprocess.PIPE, text=True)
    try:
        line = d.stdout.readline().split()
        assert line and line[0] == 'TURN', line
        turn_pid = int(line[1])
        deadline = time.monotonic() + 15
        while not (here / 'grandchild.pid').exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert (here / 'grandchild.pid').exists(), 'the stand-in turn never started its grandchild'
        assert pid_alive(turn_pid) and Path(pb.kimi_turn_record_path(SESSION)).exists()
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/PID', str(d.pid)], capture_output=True)   # no tree: the daemon only
        else:
            d.kill()
        d.wait(timeout=30)
        assert wait_dead(turn_pid), 'the turn outlived its daemon'
        grand_file = here / 'grandchild.pid'
        if grand_file.exists():
            assert wait_dead(int(grand_file.read_text()), 30), "the turn's detached grandchild outlived its daemon"
    finally:
        if d.poll() is None:
            d.kill()
    assert pb.reap_leftover_turn(SESSION, sessions_open=loose_scan) == 'stale'   # its process is gone: record removed
    assert not Path(pb.kimi_turn_record_path(SESSION)).exists()


def test_real_a_turn_that_survived_its_kill_keeps_its_record(pb, here, tmp_path, monkeypatch):
    # the record is cleared only once the turn's process is gone, so a failed kill leaves something to reap
    i = seat(tmp_path)
    append(i['log'], message('msg/one'))
    survivor = sleeper()
    results = [(None, 'timed out, kill failed')]

    def turn(argv, cwd, env, timeout, on_start=None):
        on_start(survivor.pid)
        return results.pop(0)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if not results:
            raise Stop()
    monkeypatch.setattr(pb, 'kimi_config_error', lambda ident: None)   # real process lookups here: no psutil stub
    monkeypatch.setattr(pb, 'graphline', None)
    monkeypatch.setattr(pb, 'owedline', None)
    try:
        with pytest.raises(Stop):
            pb.run_kimi_delivery(i, turn=turn, sessions_open=lambda t: [], wire=FakeWire(), sleep=sleep,
                                 reap=lambda s: 'none')
        record = json.loads(Path(pb.kimi_turn_record_path(SESSION)).read_text())
        assert record['pid'] == survivor.pid and record['timeout_s'] == pb.KIMI_TURN_TIMEOUT_S
    finally:
        survivor.kill(); survivor.wait(timeout=10)


# ---- round 3 (pxx 5341f1) ------------------------------------------------------------------------------------------

def test_the_wire_check_binds_one_completed_end_to_one_prompt_in_the_same_file(pb, tmp_path):
    home, w = wire_home(tmp_path)
    kw = pb.KimiWire(dict(kimi_session=SESSION, kimi_home=str(home)))
    P = '{"type":"turn.prompt","turnId":1}' + chr(10)
    E = lambda reason: '{"type":"turn.ended","turnId":1,"reason":"%s"}' % reason + chr(10)
    for appended, ok in ((P + E('completed') + P, False),             # the last prompt never ended
                         (E('completed') + P, False),                 # the end came before its prompt
                         (P + E('aborted') + P + E('completed'), False),  # two turns, one aborted
                         (P + E('completed') + P + E('completed'), False),  # two turns
                         (P + E('completed'), True)):
        mark = kw.mark()
        with open(w, 'a') as f:
            f.write(appended)
        assert kw.completed(mark)[0] is ok, appended
    mark = kw.mark()
    original = w.read_text()
    # hold the old file open on POSIX so its inode number cannot go straight to the new file (ext4 reuses it, which
    # the check cannot see: see KimiWire.completed); Windows refuses to unlink an open file, and NTFS does not reuse
    held = open(w, 'rb') if os.name != 'nt' else None
    try:
        w.unlink()
        w.write_text(original + P + E('completed'))                 # a NEW file, same path, same prefix
        ok, why = kw.completed(mark)
    finally:
        if held:
            held.close()
    assert ok is False and 'replaced' in why


def test_the_wire_check_refuses_a_log_truncated_in_place_and_regrown(pb, tmp_path):
    # pxx d78b70: same inode, size past the mark, but the bytes after the mark belong to another history
    home, w = wire_home(tmp_path)
    kw = pb.KimiWire(dict(kimi_session=SESSION, kimi_home=str(home)))
    P = '{"type":"turn.prompt","turnId":9}' + chr(10)
    E = '{"type":"turn.ended","turnId":9,"reason":"completed"}' + chr(10)
    filler = lambda c, n: ''.join('{"type":"note","n":%d,"c":"%s"}' % (i, c) + chr(10) for i in range(n))
    for before in (filler('a', 3), filler('a', 400)):       # under and over TAIL_BYTES of history before the mark
        w.write_text(before)
        mark = kw.mark()
        with open(w, 'r+') as f:                            # in place: the same file, so the inode is kept
            f.truncate(0)
            f.write(filler('b', len(before.splitlines()) + 5) + P + E)
        assert os.stat(w).st_size > mark[1]
        ok, why = kw.completed(mark)
        assert ok is False and 'changed' in why, why
        w.write_text(before)                                # control: an honest append on the same history passes
        mark = kw.mark()
        with open(w, 'a') as f:
            f.write(P + E)
        assert kw.completed(mark) == (True, None)


def test_real_a_record_that_cannot_be_written_ends_the_turn(pb, here, tmp_path):
    marker = tmp_path / 'started.pid'
    code = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(120)"

    def on_start(pid):
        raise OSError('disk full')
    t0 = time.monotonic()
    rc, out = pb.kimi_turn([BASE_PY, '-c', code, str(marker)], str(tmp_path), dict(os.environ), 600, on_start=on_start)
    assert rc == -1 and 'could not be written' in out and time.monotonic() - t0 < 60
    deadline = time.monotonic() + 15        # a process killed before it wrote its pid never ran; one that did must die
    while not (marker.exists() and marker.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.2)                     # wait for the pid itself: the file exists before its write lands
    if marker.exists() and marker.read_text().strip():
        assert wait_dead(int(marker.read_text())), 'the turn outlived its failed record'


def test_real_the_reaper_keeps_the_record_when_it_cannot_be_sure(pb, here):
    import postbox_os
    p = sleeper()
    record = Path(pb.kimi_turn_record_path(SESSION))
    try:
        created = postbox_os.process_create_time(p.pid)
        past = {'pid': p.pid, 'create_time': created, 'started_at': time.time() - 120, 'timeout_s': 60}
        record.write_text(json.dumps(past))
        assert pb.reap_leftover_turn(SESSION, kill=lambda pid: False) == 'unknown' and record.exists()  # #3
        assert pid_alive(p.pid)
        record.write_text(json.dumps(dict(past, create_time=created + 0.5)))   # #5: half a second is another process
        assert pb.reap_leftover_turn(SESSION, sessions_open=lambda s: []) == 'stale' and pid_alive(p.pid)
        record.write_text(json.dumps(dict(past, create_time='bad')))            # #4: no TypeError out of the loop
        assert pb.reap_leftover_turn(SESSION) == 'unknown' and record.exists()
    finally:
        p.kill(); p.wait(timeout=10)
    gone = {'pid': p.pid, 'create_time': created, 'started_at': 0, 'timeout_s': 60}
    record.write_text(json.dumps(gone))                                          # #2: the turn is gone, a survivor not
    assert pb.reap_leftover_turn(SESSION, sessions_open=lambda s: [(4242, 'node.exe')]) == 'unknown'
    assert record.exists()
    assert pb.reap_leftover_turn(SESSION, sessions_open=lambda s: []) == 'stale' and not record.exists()
    record.write_text(json.dumps(dict(gone, create_time=None)))                  # exited before its record was written
    assert pb.reap_leftover_turn(SESSION, sessions_open=lambda s: []) == 'stale'


SHIM_DAEMON = textwrap.dedent("""
    import os, subprocess, sys, time
    child = ("import subprocess, sys, time; "
             "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], **({'start_new_session': True} "
             "if sys.platform != 'win32' else {'creationflags': 0x00000008})); "
             "print('GRANDCHILD', g.pid, flush=True); time.sleep(120)")
    kw = {'start_new_session': True} if sys.platform != 'win32' else {}
    p = subprocess.Popen([sys.executable, sys.argv[1], str(os.getpid()), '--', sys.executable, '-c', child],
                         stdout=subprocess.PIPE, text=True, **kw)
    print(p.stdout.readline().strip(), flush=True)
    time.sleep(120)
""")


def test_real_postbox_child_ends_a_detached_grandchild_when_its_daemon_dies(pb, tmp_path):
    # pxx 5341f1 #2: postbox_child used to kill only its direct child
    if os.name == 'nt':
        # on Windows the kill-on-close job ends a turn and its descendants and no shim is started; that path is
        # test_real_a_daemon_killed_mid_turn_leaves_nothing_wedged
        pytest.skip('POSIX only: Windows ends a turn and its descendants by the kill-on-close job')
    script = tmp_path / 'shim_daemon.py'
    script.write_text(SHIM_DAEMON)
    shim = str(Path(pb.__file__).parent / 'postbox_child.py')
    d = subprocess.Popen([BASE_PY, str(script), shim], stdout=subprocess.PIPE, text=True)
    try:
        line = d.stdout.readline().split()
        assert line and line[0] == 'GRANDCHILD', line
        grandchild = int(line[1])
        assert pid_alive(grandchild)
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/PID', str(d.pid)], capture_output=True)
        else:
            d.kill()
        d.wait(timeout=30)
        assert wait_dead(grandchild, 30), 'the detached grandchild outlived its daemon'
    finally:
        if d.poll() is None:
            d.kill()


def test_the_shim_reads_the_whole_tree_from_the_process_table(pb):
    # pxx 07fbaa: without psutil the shim found no descendants, so a setsid grandchild outlived its daemon
    import importlib.util
    spec = importlib.util.spec_from_file_location('postbox_child_t', Path(pb.__file__).parent / 'postbox_child.py')
    child = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(child)
    table = '\n'.join(['  PID  PPID', '    1     0', '  100     1', '  200   100', '  300   200', '  301   200',
                       '  400   300', '  500     1', 'garbage line here', '  600   500', '  100   100'])
    assert sorted(child.tree_below(100, table)) == [200, 300, 301, 400]
    assert child.tree_below(500, table) == [600] and child.tree_below(999, table) == []


def test_parse_etime_reads_every_ps_form(pb):
    import postbox_os
    assert [postbox_os.parse_etime(v) for v in ('05', '01:05', '02:01:05', '3-02:01:05', ' 12:00 ')] == \
        [5, 65, 7265, 266465, 720]
