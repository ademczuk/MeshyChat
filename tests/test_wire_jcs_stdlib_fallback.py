"""Verify `tools.meshy_node.wire` pure-stdlib RFC 8785 (JCS) fallback parity and client-venv isolation."""
from __future__ import annotations

from enum import Enum, IntEnum, StrEnum
import json
import math
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import sys
import sysconfig
import uuid
import venv

import pytest
import rfc8785

from apps.crypto import primitives as S
from tools.meshy_node import wire

REPO_ROOT = Path(__file__).resolve().parent.parent

KAT_DPOP = (
    "AARQT1NUAAUvdjEveAAAAAA7msoAMAomZLy6qQuerO_7t-QPAZosybBQt7yNFI1M18KN9dkRERERERERERERE"
    "RERERERL6PfumYqeRVOONEmgaoGEgLBgXLlh1NgJFOhITRGJzTbrCFeNuM4kXysoVuLbZORGx_BKRqz-hmmXZ_oDdtrAA"
)


class _Color(str, Enum):
    RED = "red"


class _SColor(StrEnum):
    BLUE = "blue"


class _Num(IntEnum):
    SEVEN = 7


class _CustomStr(str):
    def __str__(self) -> str:
        return "WRONG-STR-REPR"


class _CustomInt(int):
    def __str__(self) -> str:
        return "WRONG-INT-REPR"


class _CustomFloat(float):
    def __str__(self) -> str:
        return "WRONG-FLOAT-REPR"


def test_rfc8785_official_vectors_and_edge_cases_match_byte_for_byte(monkeypatch):
    # RFC 8785 Section 3.2.3 property sorting vector: U+1F600 (UTF-16BE d83d de00)
    # must sort BEFORE U+FB33 (UTF-16BE fb33), unlike Unicode code point order.
    rfc_sorting_obj = {
        "\u20ac": "Euro Sign",
        "\r": "Carriage Return",
        "\ufb33": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "\u00f6": "Latin Small Letter O With Diaeresis",
    }
    expected_sorting = (
        b'{"\\r":"Carriage Return","1":"One","\xc2\x80":"Control",'
        b'"\xc3\xb6":"Latin Small Letter O With Diaeresis",'
        b'"\xe2\x82\xac":"Euro Sign",'
        b'"\xf0\x9f\x98\x80":"Emoji: Grinning Face",'
        b'"\xef\xac\xb3":"Hebrew Letter Dalet With Dagesh"}'
    )
    assert rfc8785.dumps(rfc_sorting_obj) == expected_sorting
    assert wire._stdlib_jcs(rfc_sorting_obj) == expected_sorting

    # Direct UTF-16BE vs UTF-8/codepoint boundary check: U+10000 (d800 dc00) < U+E000 (e000).
    bmp_vs_supp = {"\ue000": "bmp-high", "\U00010000": "supplementary", "\uffff": "bmp-max"}
    assert wire._stdlib_jcs(bmp_vs_supp) == rfc8785.dumps(bmp_vs_supp)
    assert wire._stdlib_jcs(bmp_vs_supp).index(b"supplementary") < wire._stdlib_jcs(bmp_vs_supp).index(b"bmp-high")

    # Negative zero and zero variants must all canonicalize to b"0".
    for zero in (0, -0, 0.0, -0.0, _CustomInt(0), _CustomFloat(-0.0)):
        assert wire._stdlib_jcs(zero) == b"0" == rfc8785.dumps(zero)

    edge_cases = [
        None,
        True,
        False,
        1,
        -1,
        2**53 - 1,
        -(2**53) + 1,
        1.0,
        -1.0,
        0.5,
        -0.5,
        1e-7,
        -1e-7,
        1e-6,
        -1e-6,
        9.999999999999999e-7,
        1.0000000000000002e-6,
        1e15,
        1e16,
        1e16 + 2.0,
        1e20,
        -1e20,
        9.999999999999999e20,
        1e21,
        -1e21,
        float(2**53),
        -float(2**53),
        5e-324,
        -5e-324,
        sys.float_info.min,
        -sys.float_info.min,
        sys.float_info.max,
        -sys.float_info.max,
        1 / 3,
        math.pi,
        -math.e,
        "".join(chr(c) for c in range(32)) + '"\\/hello\x7f\u0080\u2028\u2029\U0010ffff',
        _Color.RED,
        _SColor.BLUE,
        _Num.SEVEN,
        _CustomStr("actual-str"),
        _CustomInt(42),
        _CustomFloat(3.25),
        (1, "two", (True, False, None), -0.0),
        {_Color.RED: _Num.SEVEN, _SColor.BLUE: _CustomStr("v"), _CustomStr("k"): [1e20, 1e21, 1e-6, 1e-7]},
    ]
    for case in edge_cases:
        assert wire._stdlib_jcs(case) == rfc8785.dumps(case)

    with monkeypatch.context() as mp:
        mp.setattr(wire, "_rfc8785", None)
        for case in edge_cases:
            assert wire.jcs(case) == rfc8785.dumps(case)


def _random_finite_float(rng: random.Random) -> float:
    choice = rng.randrange(6)
    if choice == 0:
        return -0.0 if rng.randrange(2) else 0.0
    if choice == 1:
        exp = rng.randint(-323, 307)
        mant = rng.uniform(1.0, 9.999999999999998)
        val = mant * (10.0 ** exp)
        if math.isfinite(val):
            return -val if rng.randrange(2) else val
    if choice == 2:
        exp = rng.choice([-8, -7, -6, -5, -1, 0, 1, 15, 16, 19, 20, 21, 22])
        val = (1.0 + rng.random() * 8.999999999999) * (10.0 ** exp)
        return -val if rng.randrange(2) else val
    while True:
        raw = struct.pack(">Q", rng.getrandbits(64))
        val = struct.unpack(">d", raw)[0]
        if math.isfinite(val):
            return val


def _random_valid_string(rng: random.Random, max_len: int = 24) -> str:
    alphabet = [
        "\x00", "\x01", "\x08", "\x09", "\x0a", "\x0c", "\x0d", "\x1f",
        '"', "\\", "/", "a", "Z", "0", " ",
        "\u0080", "\u00ff", "\u07ff", "\u20ac", "\ue000", "\ufb33", "\uffff",
        "\U00010000", "\U0001f600", "\U0010ffff",
    ]
    length = rng.randint(0, max_len)
    return "".join(rng.choice(alphabet) for _ in range(length))


def _random_jcs_value(rng: random.Random, depth: int = 0):
    if depth >= 3:
        kind = rng.randrange(5)
    else:
        kind = rng.randrange(7)
    if kind == 0:
        return None
    if kind == 1:
        return bool(rng.randrange(2))
    if kind == 2:
        if rng.randrange(4) == 0:
            return rng.choice([-(2**53) + 1, -(2**31), -1, 0, 1, 2**31 - 1, 2**53 - 1])
        return rng.randint(-(2**53) + 1, 2**53 - 1)
    if kind == 3:
        return _random_finite_float(rng)
    if kind == 4:
        return _random_valid_string(rng)
    if kind == 5:
        n = rng.randint(0, 6)
        seq = [_random_jcs_value(rng, depth + 1) for _ in range(n)]
        return tuple(seq) if rng.randrange(3) == 0 else seq
    n = rng.randint(0, 8)
    return {_random_valid_string(rng, 10): _random_jcs_value(rng, depth + 1) for _ in range(n)}


def test_fuzz_600_cases_match_rfc8785_dumps_exactly(monkeypatch):
    rng = random.Random(20261107)
    cases = [_random_jcs_value(rng) for _ in range(600)]
    assert len(cases) == 600
    for idx, item in enumerate(cases):
        got = wire._stdlib_jcs(item)
        want = rfc8785.dumps(item)
        assert got == want, f"mismatch at fuzz case {idx}: {got!r} != {want!r}"

    with monkeypatch.context() as mp:
        mp.setattr(wire, "_rfc8785", None)
        for item in cases[:100]:
            assert wire.jcs(item) == rfc8785.dumps(item)


@pytest.mark.parametrize(
    "bad_input",
    [
        math.nan,
        math.inf,
        -math.inf,
        struct.unpack(">d", b"\x7f\xf8\x00\x00\x00\x00\x00\x01")[0],
        2**53,
        -(2**53),
        2**64,
        -(2**64),
        [1, 2**53],
        {"nested": -(2**53)},
        "\ud800",
        "\udbff",
        "\udc00",
        "\udfff",
        "ok-\ud83d-bad",
        {"\ud800": 1},
        {"ok": "\udfff"},
        {1: "int-key"},
        {None: "none-key"},
        {True: "bool-key"},
        {(1, 2): "tuple-key"},
        {b"bytes": "bytes-key"},
        b"raw-bytes",
        bytearray(b"abc"),
        {1, 2, 3},
        frozenset({"a"}),
        object(),
        1 + 2j,
    ],
)
def test_fail_closed_refusals_match_rfc8785(monkeypatch, bad_input):
    with pytest.raises(ValueError):
        rfc8785.dumps(bad_input)
    with pytest.raises(ValueError):
        wire._stdlib_jcs(bad_input)
    with monkeypatch.context() as mp:
        mp.setattr(wire, "_rfc8785", None)
        with pytest.raises(ValueError):
            wire.jcs(bad_input)


def _venv_paths(venv_dir: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        py = venv_dir / "Scripts" / "python.exe"
        site_pkgs = venv_dir / "Lib" / "site-packages"
    else:
        py = venv_dir / "bin" / "python"
        ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        site_pkgs = venv_dir / "lib" / ver / "site-packages"
    return py, site_pkgs


def _install_cryptography_only(dst_site: Path) -> None:
    """Copy only `cryptography` and its runtime CFFI modules into `dst_site` (never `rfc8785`)."""
    import cryptography

    src_site = Path(cryptography.__file__).resolve().parent.parent
    dst_site.mkdir(parents=True, exist_ok=True)
    allowed_prefixes = ("cryptography", "cffi", "pycparser", "_cffi_backend")
    for entry in src_site.iterdir():
        name = entry.name
        if "rfc8785" in name.lower():
            continue
        if name.startswith(allowed_prefixes):
            target = dst_site / name
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
    assert not list(dst_site.glob("*rfc8785*"))


def test_isolated_venv_with_stdlib_and_cryptography_only_signs_and_verifies(tmp_path):
    venv_dir = tmp_path / "client_only_venv"
    venv.EnvBuilder(with_pip=False, clear=True).create(venv_dir)
    venv_python, venv_site = _venv_paths(venv_dir)
    assert venv_python.is_file(), f"missing venv python at {venv_python}"
    _install_cryptography_only(venv_site)
    assert sysconfig.get_path("purelib") != str(venv_site)

    peer_id = str(uuid.UUID("12345678-1234-5678-9abc-def012345678"))
    script = """
import importlib.util
import json
import sys
import uuid

assert importlib.util.find_spec("rfc8785") is None, "rfc8785 must not be installed in isolated venv"
try:
    import rfc8785  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    raise AssertionError("rfc8785 unexpectedly imported")

from tools.meshy_node import wire
import tools.meshy_node.node  # noqa: F401
from tools.transcript_cards.context_upload import NodeTransport  # noqa: F401

assert wire._rfc8785 is None

seed = bytes([7] * 32)
pub = wire.public_key_of(seed)
kid = wire.peer_kid(pub)
peer_id = sys.argv[1]

# 1. DPoP proof KAT parity
kat_proof = wire.dpop_proof(
    seed, "POST", "/v1/x", "h.p.s",
    timestamp=1_000_000_000, jti=bytes([0x11] * 16),
)

# 2. Sign-in and owner sign-in JCS messages + Ed25519 verify
signin_bytes = wire.signin_message(peer_id, "nonce-abc-123")
signin_sig = wire.sign(seed, signin_bytes)
assert wire.verify(pub, signin_bytes, signin_sig)

owner_bytes = wire.owner_signin_message(peer_id, "nonce-owner-456")
owner_sig = wire.sign(seed, owner_bytes)
assert wire.verify(pub, owner_bytes, owner_sig)

# 3. Signed skill.call envelope + author_signature_ok + result_signature_ok
args = {
    "namespace": "shared/mesh-chat",
    "key": "msg/1790000000000-test-abcdef",
    "value": {
        "schema_version": 1,
        "to": ["peer-b"],
        "from": "peer-a",
        "type": "note",
        "thread_id": "thread-1",
        "ts": "2026-11-07T08:00:00Z",
        "body": "hello \\n\\t \u20ac \U0001f600 \ufb33",
        "author_family": "test",
        "extra_order": {"\ue000": 1e20, "\U00010000": 1e-6, "zero": -0.0},
    },
}
env = wire.call_envelope(
    seed=seed,
    kid=kid,
    caller_peer_id=peer_id,
    target_peer_id=peer_id,
    skill="memory.write",
    args=args,
    call_id="00000000-0000-4000-8000-000000000099",
)
jwks = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": kid, "x": wire.b64u(pub)}]}
memo = {
    "namespace": args["namespace"],
    "key": args["key"],
    "value": args["value"],
    "created_by_peer_id": peer_id,
    "provenance": env,
}
assert wire.author_signature_ok(memo, lambda _writer: (jwks, None)) == (True, None)

res_payload = {
    "v": 1,
    "kind": "skill.result",
    "call_id": env["payload"]["call_id"],
    "status": "succeeded",
    "result": {"ok": True, "version": 1},
}
call_row = {
    "call_id": env["payload"]["call_id"],
    "status": "succeeded",
    "result": {"ok": True, "version": 1},
    "result_payload": res_payload,
    "result_signing_kid": kid,
    "result_signature": wire.b64u(wire.sign(seed, wire.jcs(res_payload))),
}
assert wire.result_signature_ok(call_row, jwks) == (True, "ok")

print(json.dumps({
    "kat_proof": kat_proof,
    "pub_b64u": wire.b64u(pub),
    "signin_hex": signin_bytes.hex(),
    "signin_sig_b64u": wire.b64u(signin_sig),
    "envelope": env,
}))
"""
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(("PYTHON", "VIRTUAL_ENV"))
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [str(venv_python), "-c", script, peer_id],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    out = json.loads(proc.stdout.strip())
    assert out["kat_proof"] == KAT_DPOP
    pub = wire.b64u_decode(out["pub_b64u"])
    assert bytes.fromhex(out["signin_hex"]) == rfc8785.dumps({
        "aud": "peer",
        "nonce": "nonce-abc-123",
        "peer_id": peer_id,
        "purpose": wire.SIGNIN_PURPOSE,
    })
    assert S.verify_strict(pub, bytes.fromhex(out["signin_hex"]), wire.b64u_decode(out["signin_sig_b64u"]))
    env_payload = out["envelope"]["payload"]
    env_sig = wire.b64u_decode(out["envelope"]["signature"])
    assert S.verify_strict(pub, rfc8785.dumps(env_payload), env_sig)


def test_stdlib_jcs_rejects_duplicate_canonical_object_keys():
    class _DupKey(str):
        __hash__ = object.__hash__

        def __eq__(self, other: object) -> bool:
            return self is other

    with pytest.raises(ValueError, match="duplicate object key"):
        wire._stdlib_jcs({"a": 1, _DupKey("a"): 2})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
