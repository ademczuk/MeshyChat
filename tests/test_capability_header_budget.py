"""T20: the header budget, measured on the served stack (B07 slice 6; docs/DIVERGENCES.md 040;
design docs/design/biscuit-capabilities.md 3.1, critique C7, inference 11).

daphne serves through Twisted's `HTTPChannel`, whose `totalHeadersSize = 16384` bounds the request
line plus ALL headers and answers anything larger with a bare 400 before Django runs. The design
caps `Meshychat-Capability` at 8 KiB so a token, a real JWT and a DPoP proof fit together and the VIEW
decides the request. Inference 11 said a token "delegated to the 10-block cap" stays far below
8 KiB. This measures it, through daphne run as a SUBPROCESS on an ephemeral port (never 17070 or
mcd-serve; Twisted's reactor cannot be restarted in-process), against this run's database, with
DPoP ENFORCED, a real EdDSA JWT whose `cnf` is the caller's did:key, a real proof for the POST, and
a signed envelope binding the token's sha256:

  1. the LARGEST authority the mint route can produce (16 rights, the longest prefix the route
     still mints, found by bisecting through the route itself) is served and decided: the call
     completes;
  2. that token delegated to the 10-block cap (4 delegation hops and 5 restriction blocks) is
     over 8 KiB. The client refuses it before any socket (`tools/meshy_node/wire.py`
     `capability_problem`); sent anyway, daphne still serves it and the VIEW refuses it,
     403 E_CAPABILITY_INVALID, in the JSON envelope, not Twisted's bare 400. So inference 11 is
     false at the extreme: the mint's 8 KiB bounds the authority alone, and a maximal authority
     leaves no room for the blocks a holder may add;
  3. the largest 10-block chain under 8 KiB (16 rights, the prefix bisected so the whole chain
     fits) is served and allowed for its final holder;
  4. CONTROL for the budget itself, measured on daphne 4.2.3 / Twisted 26.4.0: headers whose
     lines each fit but whose total passes 16384 bytes get Twisted's bare `HTTP/1.1 400 Bad
     Request` and no JSON envelope; ONE header line past 16384 bytes gets no bytes at all, the
     connection closed (LineReceiver's own line cap, before the total is counted). Neither
     reaches Django: what the 8 KiB cap exists to prevent. An 8 KiB header on the same route
     does reach it (the middleware's JSON 403 on /v1/livez).

Run:  pytest tests/test_capability_header_budget.py -q
"""
from __future__ import annotations

import datetime as _dt
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402

from tests.capability_invoke import invoke_context, seed_of  # noqa: E402
from tests.capability_routes import (JWT_ISSUER, JWT_PRIVATE_PEM,  # noqa: E402
                                     JWT_PUBLIC_PEM, peer_jwt)
from tests.capability_world import ROOT_KID, utcnow  # noqa: E402
from tools.meshy_node import wire  # noqa: E402

CAP = 8192
TWISTED_TOTAL = 16384
RIGHTS = 16


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Daphne:
    """daphne as a subprocess on 127.0.0.1:<ephemeral>, its output to a file (never a pipe)."""

    def __init__(self, env: dict, log: Path):
        self.port = _free_port()
        self.log = log
        self.out = open(log, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "daphne", "-b", "127.0.0.1", "-p", str(self.port),
             "meshychat.asgi:application"],
            cwd=str(ROOT), env=env, stdout=self.out, stderr=subprocess.STDOUT)

    def wait_ready(self, seconds: float = 90.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"daphne exited {self.proc.returncode}: "
                                     f"{self.log.read_text(errors='replace')[-3000:]}")
            try:
                status, _body, _raw = self.request("GET", "/v1/livez", {})
                if status in (200, 503):
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise AssertionError("daphne did not answer /v1/livez: "
                             f"{self.log.read_text(errors='replace')[-3000:]}")

    def request(self, method: str, path: str, headers: dict, body: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return response.status, parsed, raw

    def raw(self, request: bytes, seconds: float = 20.0) -> bytes:
        """`request` as bytes on a plain socket, and every byte the server sends back until it
        closes: what a client sees when Twisted refuses before any HTTP framing Django adds."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=seconds) as sock:
            try:
                sock.sendall(request)
            except OSError:
                pass                              # it may close while the headers still flow
            got = b""
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    got += chunk
            except OSError:
                pass
            return got

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(20)
        self.out.close()


def _child_env(tmp: Path, world, memory_peer: str) -> dict:
    key = tmp / "root.key"
    key.write_text(bytes(world.root.private_key.to_bytes()).hex() + "\n", encoding="ascii")
    public = tmp / "jwt_public.pem"
    private = tmp / "jwt_private.pem"
    public.write_text(JWT_PUBLIC_PEM, encoding="ascii")
    private.write_text(JWT_PRIVATE_PEM, encoding="ascii")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MESHY_", "MESHYCHAT_", "MESHY_", "DJANGO_"))}
    env.update({
        "DJANGO_SETTINGS_MODULE": "meshychat.settings", "PYTHONPATH": str(ROOT),
        "MESHY_DSN": os.environ["MESHY_DSN"], "MESHY_ALLOWED_HOSTS": "127.0.0.1",
        "MESHY_LOG_LEVEL": "warning", "MESHY_MEMORY_PEER_ID": memory_peer,
        "MESHY_CAPABILITIES": "on", "MESHY_CAPABILITY_ROOT_KEY_PATH": str(key),
        "MESHY_CAPABILITY_ROOT_KID": str(ROOT_KID),
        "MESHY_CAPABILITY_ROOT_PUBKEYS":
            f"{ROOT_KID}={bytes(world.root.public_key.to_bytes()).hex()}",
        "MESHYCHAT_JWT_ISSUER": JWT_ISSUER, "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": str(public),
        "MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": str(private), "MESHYCHAT_DPOP_ENFORCE": "on",
    })
    return env


def _jwt_with_cnf(peer) -> str:
    from apps.authapi.didkey import to_did_key
    now = int(utcnow().timestamp())
    return pyjwt.encode({"sub": peer.id, "aud": "peer", "iss": JWT_ISSUER, "iat": now,
                         "exp": now + 300, "scopes": [], "cnf": to_did_key(peer.public_bytes)},
                        JWT_PRIVATE_PEM, algorithm="EdDSA")


def _invoke(server: Daphne, inv, peer, token: str, args: dict):
    """A signed memory.query over the real socket: the JWT with its cnf, a fresh DPoP proof for
    this POST and path, and the envelope binding the token's hash. Returns (status, body, the
    total bytes of the request line and headers)."""
    envelope = inv.envelope(peer, "memory.query", args, token=token)
    path = f"/v1/agents/{inv.memory_peer}/skills/memory.query/invoke"
    jwt = _jwt_with_cnf(peer)
    headers = {"Host": f"127.0.0.1:{server.port}", "Authorization": f"Bearer {jwt}",
               "DPoP": wire.dpop_proof(seed_of(peer), "POST", path, jwt),
               "Content-Type": "application/json", "Meshychat-Capability": token}
    body = json.dumps({"envelope": envelope, "mode_override": "sync",
                       "call_id": envelope["payload"]["call_id"]}).encode("utf-8")
    head = len(f"POST {path} HTTP/1.1\r\n") + sum(
        len(f"{k}: {v}\r\n") for k, v in {**headers, "Content-Length": str(len(body))}.items())
    status, parsed, _raw = server.request("POST", path, headers, body)
    return status, parsed, head


def _mint_body(subject, ns: str, length: int, depth: int) -> dict:
    return {"subject": subject.id, "namespace": ns, "depth_cap": depth,
            "rights": [{"prefix": f"{i:02d}/" + "x" * (length - 3), "action": "read"}
                       for i in range(RIGHTS)],
            "expires_at": (utcnow() + _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}


def _mint(inv, issuer, subject, ns: str, length: int, depth: int = 4):
    response = inv.client.post("/v1/memory/capabilities",
                               data=json.dumps(_mint_body(subject, ns, length, depth)),
                               content_type="application/json",
                               headers={"authorization": f"Bearer {peer_jwt(issuer.id)}"})
    return response.status_code, (json.loads(response.content) if response.content else None)


def _largest(inv, issuer, subject, ns: str, fits) -> tuple[int, str]:
    """The longest prefix length (16 rights) the MINT ROUTE mints and `fits` accepts, found by
    bisection through the route: (length, token)."""
    lo, hi, best = 3, 512, None
    while lo <= hi:
        mid = (lo + hi) // 2
        status, body = _mint(inv, issuer, subject, ns, mid)
        if status == 201 and fits(body["token"]):
            best, lo = (mid, body["token"]), mid + 1
        else:
            assert status in (201, 422), (status, body)
            hi = mid - 1
    assert best is not None
    return best


def _ten_blocks(inv, token: str, chain) -> str:
    """`token` delegated along `chain` (four hops) and restricted by five first-party blocks:
    ten blocks, the verifier's cap. Every hop within 300 s."""
    w = inv.w
    tok = w.b.Biscuit.from_base64(token, w.store.provider)
    for frm, to in zip(chain, chain[1:]):
        tok = w.delegate(tok, frm, to, ttl=240)
    sooner = (utcnow() + _dt.timedelta(minutes=30)).replace(microsecond=0)
    for code, params in (('check if action("read");', {}),
                         ('check if operation($op), ["memory.query", "memory.read"]'
                          '.contains($op);', {}),
                         ("check if time($t), $t < {s};", {"s": sooner}),
                         ('check if resource($ns, $r), $r.starts_with("00/");', {}),
                         ('check if action("read");', {})):
        tok = w.restrict(tok, code, params)
    assert tok.block_count() == 10
    return tok.to_base64()


def test_t20_the_largest_tokens_through_daphne_with_a_real_jwt_and_dpop_proof(tmp_path):
    with invoke_context() as inv:
        o, s, b, c, d, e = (inv.peer(x) for x in ("O", "S", "B", "C", "D", "E"))
        ns = inv.w.claim(o)

        biggest_len, biggest = _largest(inv, o, s, ns, lambda t: True)
        chain_len, fitting = _largest(
            inv, o, s, ns, lambda t: len(_ten_blocks(inv, t, [s, b, c, d, e])) <= CAP)
        over = _ten_blocks(inv, biggest, [s, b, c, d, e])
        fitting10 = _ten_blocks(inv, fitting, [s, b, c, d, e])
        prefix = lambda n: "00/" + "x" * (n - 3)  # noqa: E731

        server = Daphne(_child_env(tmp_path, inv.w, inv.memory_peer), tmp_path / "daphne.log")
        try:
            server.wait_ready()
            one = _invoke(server, inv, s, biggest, {"namespace": ns,
                                                    "key_prefix": prefix(biggest_len)})
            ten_over = _invoke(server, inv, e, over, {"namespace": ns,
                                                      "key_prefix": prefix(biggest_len)})
            ten_fits = _invoke(server, inv, e, fitting10, {"namespace": ns,
                                                           "key_prefix": prefix(chain_len)})
            no_proof = dict(zip(("status", "body"), server.request(
                "POST", f"/v1/agents/{inv.memory_peer}/skills/memory.query/invoke",
                {"Host": f"127.0.0.1:{server.port}",
                 "Authorization": f"Bearer {_jwt_with_cnf(e)}",
                 "Content-Type": "application/json", "Meshychat-Capability": fitting10},
                b"{}")[:2]))
            crlf = "\r\n"
            budget = server.raw(
                (f"GET /v1/livez HTTP/1.1{crlf}Host: 127.0.0.1:{server.port}{crlf}"
                 f"Authorization: Bearer {_jwt_with_cnf(e)}{crlf}"
                 f"Meshychat-Capability: {'A' * (TWISTED_TOTAL - 200)}{crlf}{crlf}")
                .encode("ascii"))
            one_line = server.raw(
                (f"GET /v1/livez HTTP/1.1{crlf}Host: 127.0.0.1:{server.port}{crlf}"
                 f"Meshychat-Capability: {'A' * TWISTED_TOTAL}{crlf}{crlf}").encode("ascii"))
            under = server.raw(
                (f"GET /v1/livez HTTP/1.1{crlf}Host: 127.0.0.1:{server.port}{crlf}"
                 f"Connection: close{crlf}Meshychat-Capability: {'A' * CAP}{crlf}{crlf}")
                .encode("ascii"))
        finally:
            server.stop()

    record = {"authority bytes": len(biggest), "authority prefix": biggest_len,
              "ten over bytes": len(over), "ten fitting bytes": len(fitting10),
              "ten fitting prefix": chain_len, "headers (one)": one[2],
              "headers (ten over)": ten_over[2], "headers (ten fitting)": ten_fits[2]}
    print("T20 MEASURED", json.dumps(record))

    # 1. The largest authority the mint route produces: under 8 KiB, served, decided, allowed.
    assert len(biggest) <= CAP and one[2] < TWISTED_TOTAL, record
    assert one[0] == 200 and one[1]["status"] == "completed", (one[:2], record)
    assert one[1]["result"]["authz"]["holder_chain"] == [s.id], one[1]["result"]
    # 2. Delegated to ten blocks it is over 8 KiB: the client refuses it; the view refuses it.
    assert len(over) > CAP, record
    assert wire.capability_problem(over) is not None
    assert wire.capability_problem(fitting10) is None
    assert ten_over[2] < TWISTED_TOTAL, "the request still fits Twisted's budget"
    assert (ten_over[0], (ten_over[1] or {}).get("error")) == (403, "E_CAPABILITY_INVALID"), (
        ten_over[:2], record)
    # 3. The largest ten-block chain under 8 KiB: served and allowed for its final holder.
    assert (ten_fits[0], ten_fits[1]["status"]) == (200, "completed"), (ten_fits[:2], record)
    assert ten_fits[1]["result"]["authz"]["holder_chain"] == [s.id, b.id, c.id, d.id, e.id]
    assert ten_fits[1]["result"]["authz"]["hops"] == 4
    # DPoP is enforced on the served stack: the same token without a proof is refused, by
    # authentication, before the view reads the header.
    assert (no_proof["status"], (no_proof["body"] or {}).get("error")) == (403, "E_FORBIDDEN"), (
        no_proof)
    # 4. The control: past Twisted's 16 KiB, a bare 400 (or, for one line past it, a closed
    #    connection) before Django, no JSON envelope; an 8 KiB header on the same route reaches
    #    Django, whose middleware answers in JSON.
    print("T20 BUDGET", budget[:200], one_line[:200], under[:80])
    assert budget == b"HTTP/1.1 400 Bad Request" + b"\r\n" * 2, budget[:300]
    assert one_line == b"", one_line[:300]
    assert under.startswith(b"HTTP/1.1 403") and b'"E_CAPABILITY_NOT_ACCEPTED"' in under, (
        under[:300])
