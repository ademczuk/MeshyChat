"""The deploy-shaped half of tests/test_jwt_seam.py. Run ONLY as a subprocess.

It boots `meshychat.settings` from the environment the parent hands it - the MESHYCHAT_JWT_*
variables a deploy sets and nothing else. No override_settings, no override_config, no
test runner: whatever the settings module and the minter derive from that environment is
exactly what they would derive in production. Then it drives the REAL urlconf with
django.test.Client and prints one line, `SEAM_RESULT <json>`, for the parent to assert on.

Every step writes its own entry, so the parent proves each step RAN (V1 rule C6) instead
of inferring it from an absent failure. The line is prefixed because meshychat/settings.py
logs to stdout, and a parser that took "the last line" would one day read a log line.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.test import Client  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.wsgw.consumer import config_from_env  # noqa: E402

PRESENCE = "/v1/peers/me/presence"    # a real peer WRITE route
INBOX = "/v1/mesh/inbox"              # a real peer READ route


def _status(response) -> dict:
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    return {"status": response.status_code, "error": error}


def main() -> dict:
    peer_id = os.environ["SEAM_PEER_ID"]
    seed = bytes.fromhex(os.environ["SEAM_PEER_SEED_HEX"])
    out: dict = {}

    minter = authconfig.get_config()
    hub = config_from_env()
    pub = getattr(settings, "MESHY_JWT_PUBLIC_KEY_PEM", None)
    out["config"] = {
        "settings_issuer": getattr(settings, "MESHY_JWT_ISSUER", None),
        "minter_issuer": minter.jwt_issuer,
        "ws_issuer": hub.jwt_issuer,
        "settings_has_public_key": bool(pub),
        "settings_key_is_minters_key": bool(pub) and pub == minter.jwt_public_key_pem,
        "ws_key_is_settings_key": bool(pub) and hub.jwt_public_key_pem == pub,
        "minter_can_sign": bool(minter.jwt_signing_key_pem),
        "settings_dpop_enforce": getattr(settings, "MESHY_DPOP_ENFORCE", None),
        "ws_dpop_enforce": hub.dpop_enforce,
    }

    client = Client()

    def post(path, payload, **extra):
        return client.post(path, data=json.dumps(payload), content_type="application/json",
                           **extra)

    r = post("/v1/auth/challenge", {"peer_id": peer_id})
    out["challenge"] = _status(r)
    if r.status_code != 200:
        return out
    nonce = r.json()["nonce"]
    signature = P.b64u(P.sign(seed, P.jcs({
        "aud": "peer", "nonce": nonce, "peer_id": peer_id,
        "purpose": authconfig.SIGNIN_PURPOSE})))
    r = post("/v1/auth/sign-in", {"peer_id": peer_id, "nonce": nonce,
                                  "signature_b64": signature})
    out["sign_in"] = _status(r)
    if r.status_code != 200:
        return out
    jwt = r.json()["jwt"]
    claims = json.loads(P.b64u_decode(jwt.split(".")[1]))
    out["claims"] = {"iss": claims.get("iss"), "aud": claims.get("aud"),
                     "cnf": claims.get("cnf"), "act_sub": (claims.get("act") or {}).get("sub")}
    out["peer_did_key"] = os.environ["SEAM_PEER_DID_KEY"]
    bearer = {"HTTP_AUTHORIZATION": f"Bearer {jwt}"}

    # The seam itself: the token sign-in just minted, used unmodified on a peer route.
    out["inbox"] = _status(client.get(INBOX, **bearer))

    # One byte of the signature flipped: the only difference from the request above.
    head, body, sig = jwt.split(".")
    raw = bytearray(P.b64u_decode(sig))
    raw[0] ^= 0x01
    flipped = f"{head}.{body}.{P.b64u(bytes(raw))}"
    out["inbox_flipped_byte"] = _status(client.get(INBOX, HTTP_AUTHORIZATION=f"Bearer {flipped}"))

    # DPoP on a write, under whatever default the settings chose from this environment.
    out["write_without_proof"] = _status(post(PRESENCE, {}, **bearer))
    wire = P.dpop_wire(seed, "POST", PRESENCE, jwt)
    out["write_with_proof"] = _status(post(PRESENCE, {}, HTTP_DPOP=wire, **bearer))
    out["write_replayed_proof"] = _status(post(PRESENCE, {}, HTTP_DPOP=wire, **bearer))
    return out


if __name__ == "__main__":
    print("SEAM_RESULT " + json.dumps(main()), flush=True)
