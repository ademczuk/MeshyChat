"""`did:key` <-> raw Ed25519 public key, for the WS gateway's DPoP check.

The implementation is apps/authapi/didkey.py; this module re-exports it so the gateway's
imports (auth.py, consumer.py) keep their names. There used to be a second copy here that
differed only in its exception class and message text, which is how two parsers of a
security-relevant input drift apart. The shared one raises this module's `DidKeyError` with
this module's messages, so the gateway's upgrade-rejected log line reads as it did.
"""
from __future__ import annotations

from apps.authapi.didkey import (  # noqa: F401 - re-exported
    ED25519_MULTICODEC,
    DidKeyError,
    b58_decode,
    b58_encode,
    from_did_key,
    to_did_key,
)
