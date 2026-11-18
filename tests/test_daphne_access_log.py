"""The documented daphne command writes its access log as each request is served (E08).

THE DEFECT, from the live node-test server. README.md step 4 started the server with
`daphne -b 127.0.0.1 -p 8765 meshychat.asgi:application`. With no `--access-log`, daphne
(cli.py, 4.2.3) sends the access log to `sys.stdout`, and a Python process whose stdout is a
FILE or a pipe block-buffers it: under a supervisor that redirects output, a request's line
reached the log minutes later, or never if the process was killed. A log that lags by minutes
cannot be read beside the event it records.

WHAT THIS MEASURES, as behaviour rather than as text: it takes the daphne line out of the
README's step-4 block, runs exactly those flags (only the port and the application are
swapped for a free port and a one-route app in a temporary directory) with stdout and stderr
redirected to files the way a supervisor does, serves one request, and requires the request's
line to be readable within a few seconds WHILE THE PROCESS IS STILL RUNNING.

POSITIVE CONTROL, in the same test: the same harness with `python -u` (unbuffered by
construction) must see its line. Without it, "no line appeared" could be the harness failing
to find the log rather than the log lagging.

Run:  .venv/Scripts/python.exe -m pytest tests/test_daphne_access_log.py -q
"""
from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

APP = '''
async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})
'''

#: How long a line may take to reach its file. A line-buffered or unbuffered stream writes
#: it before the next request; a block-buffered one holds it until 8 KiB accumulate.
DEADLINE_S = 5.0


def _readme_daphne_line() -> str:
    text = README.read_text(encoding="utf-8")
    section = text.split("### 4. Start the server", 1)[1].split("\n### ", 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", section, flags=re.S)
    lines = [ln.strip() for b in blocks for ln in b.splitlines()
             if re.search(r"(^|\s)daphne\s", ln.strip() + " ")]
    assert len(lines) == 1, f"expected one daphne command in step 4, found {lines}"
    return lines[0]


def _split(line: str) -> tuple[dict[str, str], list[str]]:
    """`A=1 B=2 daphne ARGS` -> ({A: 1, B: 2}, ARGS)."""
    words = shlex.split(line)
    env: dict[str, str] = {}
    while words and re.match(r"^[A-Z_][A-Z0-9_]*=", words[0]):
        k, v = words.pop(0).split("=", 1)
        env[k] = v
    assert words and words[0] == "daphne", f"not a daphne command: {line!r}"
    return env, words[1:]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _retarget(args: list[str], port: int) -> list[str]:
    """Swap the port and the application; keep every other flag the README gives."""
    out = list(args)
    for flag in ("-p", "--port"):
        if flag in out:
            out[out.index(flag) + 1] = str(port)
    apps = [i for i, a in enumerate(out) if re.match(r"^[\w.]+:\w+$", a)]
    assert len(apps) == 1, f"cannot find the application argument in {args}"
    out[apps[0]] = "hello_asgi:app"
    return out


def _access_log_path(args: list[str], cwd: Path) -> Path | None:
    if "--access-log" in args:
        value = args[args.index("--access-log") + 1]
        return None if value == "-" else (cwd / value)
    return None


def _serve_and_probe(python_flags: list[str], env_extra: dict[str, str], args: list[str],
                     cwd: Path) -> tuple[bool, str]:
    """Run daphne, GET one unique path, return (line seen before the deadline, where)."""
    (cwd / "hello_asgi.py").write_text(APP, encoding="utf-8")
    port = _free_port()
    run_args = _retarget(args, port)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env.update(env_extra)
    out_path, err_path = cwd / "stdout.log", cwd / "stderr.log"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen([sys.executable, *python_flags, "-m", "daphne", *run_args],
                                cwd=cwd, env=env, stdout=out, stderr=err)
    try:
        started = time.monotonic()
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                assert proc.poll() is None, (
                    f"daphne exited rc={proc.returncode}: {err_path.read_text(errors='replace')}")
                assert time.monotonic() - started < 30, "daphne never listened"
                time.sleep(0.2)
        probe = f"/probe-{uuid.uuid4().hex}"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{probe}", timeout=10) as r:
            assert r.status == 200 and r.read() == b"ok"
        where = _access_log_path(run_args, cwd) or out_path
        deadline = time.monotonic() + DEADLINE_S
        while time.monotonic() < deadline:
            assert proc.poll() is None, "daphne exited before the line was checked"
            if where.exists() and probe in where.read_text(encoding="utf-8", errors="replace"):
                return True, where.name
            time.sleep(0.1)
        return False, where.name
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_the_documented_daphne_command_logs_each_request_while_it_runs(tmp_path):
    env_extra, args = _split(_readme_daphne_line())

    # Positive control: an unbuffered interpreter and the README's flags minus any
    # access-log file, so the line goes to the redirected stdout the harness reads.
    bare = [a for i, a in enumerate(args)
            if a != "--access-log" and (i == 0 or args[i - 1] != "--access-log")]
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    seen, where = _serve_and_probe(["-u"], {}, bare, control_dir)
    assert seen, f"control: an unbuffered daphne's access line never reached {where}"

    run_dir = tmp_path / "readme"
    run_dir.mkdir()
    seen, where = _serve_and_probe([], env_extra, args, run_dir)
    assert seen, (
        f"README.md step 4's daphne command, run as documented with its output redirected, "
        f"did not write the request's access line to {where} within {DEADLINE_S} s. daphne "
        "sends the access log to stdout unless --access-log names a file, and a redirected "
        "stdout is block-buffered: set PYTHONUNBUFFERED=1 or give daphne --access-log FILE.")


def test_the_readme_says_where_the_access_log_goes():
    """The fix is only useful if the reader knows which file to open, and the other output
    (Django's and the application's own prints) is not left buffered either."""
    env_extra, args = _split(_readme_daphne_line())
    assert env_extra.get("PYTHONUNBUFFERED") == "1", (
        "the daphne command does not run unbuffered; the server's own stdout still lags", args)
    assert "--access-log" in args, "the access log still goes to stdout"
    target = args[args.index("--access-log") + 1]
    assert target != "-", "--access-log - is stdout again"
    section = README.read_text(encoding="utf-8").split("### 4. Start the server", 1)[1]
    prose = re.sub(r"```.*?```", "", section.split("\n### ", 1)[0], flags=re.S)
    assert target in prose, f"the README never says the access log is in {target}"
