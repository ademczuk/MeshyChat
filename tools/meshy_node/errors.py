"""Additive error metadata for CLI callers; never retries an operation itself."""
from __future__ import annotations

import errno
import contextvars
from contextlib import contextmanager
import socket
import ssl
import urllib.error


RETRYABLE_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_CALL = frozenset({"E_INTERNAL", "E_UNAVAILABLE", "E_TIMEOUT", "E_RATE_LIMITED"})
NETWORK_ERRNOS = frozenset(getattr(errno, name) for name in (
    "ECONNABORTED", "ECONNREFUSED", "ECONNRESET", "ENETDOWN", "ENETRESET",
    "ENETUNREACH", "EHOSTDOWN", "EHOSTUNREACH", "ETIMEDOUT", "EPIPE",
    "WSAECONNABORTED", "WSAECONNREFUSED", "WSAECONNRESET", "WSAENETDOWN",
    "WSAENETRESET", "WSAENETUNREACH", "WSAEHOSTDOWN", "WSAEHOSTUNREACH",
    "WSAETIMEDOUT",
) if hasattr(errno, name))
REFUSED_ERRNOS = frozenset(getattr(errno, name) for name in
                          ("ECONNREFUSED", "WSAECONNREFUSED") if hasattr(errno, name))
_ACCEPTED_CALL = contextvars.ContextVar("meshy_error_accepted_call", default=None)


@contextmanager
def track_accepted_calls():
    """Keep partial command progress from looking like a rejected first request."""
    tracked = {"accepted": False}
    token = _ACCEPTED_CALL.set(tracked)
    try:
        yield tracked
    finally:
        _ACCEPTED_CALL.reset(token)


def note_call_accepted() -> None:
    tracked = _ACCEPTED_CALL.get()
    if tracked is not None:
        tracked["accepted"] = True


def http_metadata(status: int, *, mutating: bool = False, body=None,
                  accepted_call: bool = False) -> dict:
    retryable = type(status) is int and status in RETRYABLE_HTTP
    if mutating:
        rejected = (not accepted_call and isinstance(body, dict) and not body.get("call_id")
                    and status in (429, 503))
        if accepted_call or (retryable and not rejected) or (type(status) is int and status >= 500
                                                           and not rejected):
            return {"code": "E_HTTP", "retryable": False, "outcome": "unknown"}
        retryable = rejected
    return {"code": "E_HTTP", "retryable": retryable}


def io_metadata(exc: OSError, *, mutating: bool = False, accepted_call: bool = False) -> dict:
    """Distinguish transient transport failures from local I/O and configuration."""
    if mutating:
        metadata = io_metadata(exc)
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        refused = (isinstance(cause, ConnectionRefusedError)
                   or isinstance(cause, OSError) and cause.errno in REFUSED_ERRNOS)
        if accepted_call or (metadata["code"] == "E_UNREACHABLE" and not refused):
            return {**metadata, "retryable": False, "outcome": "unknown"}
        return {**metadata, "retryable": bool(refused)}
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        retryable = io_metadata(reason)["retryable"] if isinstance(reason, OSError) else False
        return {"code": "E_UNREACHABLE", "retryable": retryable}
    if isinstance(exc, ssl.SSLError):
        return {"code": "E_UNREACHABLE", "retryable": False}
    if isinstance(exc, socket.gaierror):
        return {"code": "E_UNREACHABLE", "retryable": exc.errno == socket.EAI_AGAIN}
    if isinstance(exc, (TimeoutError, ConnectionError)) or exc.errno in NETWORK_ERRNOS:
        return {"code": "E_UNREACHABLE", "retryable": True}
    return {"code": "E_LOCAL_IO", "retryable": False}


def read_metadata(payload: dict, *, retryable: bool | None = None) -> dict:
    """Keep the existing read error and details, without mutating the caller's record."""
    code = payload["error"]
    status = payload.get("http_status")
    call_code = payload.get("error_code")
    if retryable is None:
        retryable = (
            code == "E_READ_POLL_TIMEOUT"
            or (code == "E_READ_HTTP" and type(status) is int and status in RETRYABLE_HTTP)
            or (code == "E_READ_REFUSED" and (
                payload.get("status") == "timeout"
                or (isinstance(call_code, str) and call_code in RETRYABLE_CALL)
            ))
        )
    return {**payload, "code": code, "retryable": retryable}
