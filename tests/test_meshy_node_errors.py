"""Contract tests for additive CLI error metadata."""
import errno
import json
import socket
import ssl
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, errors, node


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_transient_http_statuses(status):
    assert errors.http_metadata(status) == {"code": "E_HTTP", "retryable": True}


@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 409, 422, 501, True, "503", None])
def test_other_http_statuses_are_not_retryable(status):
    assert errors.http_metadata(status) == {"code": "E_HTTP", "retryable": False}


@pytest.mark.parametrize("exc,code,retryable", [
    (TimeoutError("timeout"), "E_UNREACHABLE", True),
    (ConnectionRefusedError("refused"), "E_UNREACHABLE", True),
    (ConnectionResetError("reset"), "E_UNREACHABLE", True),
    (OSError(errno.ENETUNREACH, "network"), "E_UNREACHABLE", True),
    (PermissionError("denied"), "E_LOCAL_IO", False),
    (FileNotFoundError("missing"), "E_LOCAL_IO", False),
    (OSError("unknown"), "E_LOCAL_IO", False),
    (ssl.SSLError("TLS"), "E_UNREACHABLE", False),
    (ssl.SSLCertVerificationError("certificate"), "E_UNREACHABLE", False),
    (socket.gaierror(socket.EAI_AGAIN, "temporary DNS"), "E_UNREACHABLE", True),
    (socket.gaierror(socket.EAI_NONAME, "unknown host"), "E_UNREACHABLE", False),
    (urllib.error.URLError(TimeoutError("timeout")), "E_UNREACHABLE", True),
    (urllib.error.URLError(PermissionError("denied")), "E_UNREACHABLE", False),
    (urllib.error.URLError("unknown cause"), "E_UNREACHABLE", False),
])
def test_transport_and_local_io_are_distinguished(exc, code, retryable):
    assert errors.io_metadata(exc) == {"code": code, "retryable": retryable}


@pytest.mark.parametrize("name", ["WSAENETUNREACH", "WSAEHOSTUNREACH", "WSAECONNRESET"])
def test_windows_network_errnos(name):
    if not hasattr(errno, name):
        pytest.skip("Windows socket errno is unavailable on this platform")
    assert errors.io_metadata(OSError(getattr(errno, name), "network")) == {
        "code": "E_UNREACHABLE", "retryable": True,
    }


@pytest.mark.parametrize("payload,retryable", [
    ({"error": "E_READ_POLL_TIMEOUT"}, True),
    ({"error": "E_READ_HTTP", "http_status": 503}, True),
    ({"error": "E_READ_HTTP", "http_status": 403}, False),
    ({"error": "E_READ_HTTP", "http_status": "503"}, False),
    ({"error": "E_READ_REFUSED", "status": "timeout"}, True),
    ({"error": "E_READ_REFUSED", "error_code": "E_UNAVAILABLE"}, True),
    ({"error": "E_READ_REFUSED", "error_code": "E_PERMISSION_DENIED"}, False),
    ({"error": "E_READ_REFUSED", "error_code": "E_UNKNOWN_KEY"}, False),
    ({"error": "E_READ_REFUSED", "error_code": []}, False),
    ({"error": "E_READ_UNVERIFIED"}, False),
    ({"error": "E_HIT_HAS_NO_VERSION"}, False),
])
def test_read_metadata_preserves_details_and_input(payload, retryable):
    original = dict(payload)
    result = errors.read_metadata(payload)
    assert result == {**original, "code": original["error"], "retryable": retryable}
    assert payload == original


@pytest.mark.parametrize("exc,code,retryable,legacy", [
    (TimeoutError("slow"), "E_UNREACHABLE", True, "unreachable"),
    (PermissionError("denied"), "E_LOCAL_IO", False, "unreachable"),
])
def test_cli_keeps_exit_and_legacy_detail_without_retry(monkeypatch, capsys, tmp_path,
                                                       exc, code, retryable, legacy):
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise exc

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["inbox", "--base-url", "http://127.0.0.1:1",
                     "--state-file", str(tmp_path / "state.json")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["code"] == code
    assert payload["retryable"] is retryable
    assert str(exc) in payload[legacy]
    assert len(calls) == 1


@pytest.mark.parametrize("exc,accepted,retryable,unknown", [
    (TimeoutError("answer lost"), False, False, True),
    (OSError(errno.ECONNREFUSED, "refused"), False, True, False),
    (urllib.error.URLError(ConnectionRefusedError("refused")), False, True, False),
    (OSError(errno.ECONNREFUSED, "poll refused"), True, False, True),
    (ConnectionResetError("reset"), False, False, True),
])
def test_send_transport_retry_contract(monkeypatch, capsys, tmp_path,
                                       exc, accepted, retryable, unknown):
    calls = []

    def fail(args):
        calls.append(args)
        if accepted:
            errors.note_call_accepted()
        raise exc

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["send", "--base-url", "http://127.0.0.1:1",
                     "--state-file", str(tmp_path / "state.json"),
                     "--to", "b", "--thread", "t", "--body", "hello"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert captured.out == ""
    assert payload["retryable"] is retryable
    assert payload.get("outcome") == ("unknown" if unknown else None)
    assert len(calls) == 1


@pytest.mark.parametrize("status,body,accepted,retryable,unknown", [
    (500, {}, False, False, True),
    (502, {}, False, False, True),
    (504, {}, False, False, True),
    (408, {}, False, False, True),
    (429, {}, False, True, False),
    (503, {"message": "busy"}, False, True, False),
    (503, {"call_id": "accepted"}, False, False, True),
    (503, {"message": "poll busy"}, True, False, True),
    (429, {}, True, False, True),
    (403, {}, False, False, False),
])
def test_write_http_retry_contract(monkeypatch, capsys, tmp_path,
                                   status, body, accepted, retryable, unknown):
    def fail(args):
        if accepted:
            errors.note_call_accepted()
        raise node.ServerSaid(status, body)

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["card", "commit", "--base-url", "http://127.0.0.1:1",
                     "--state-file", str(tmp_path / "state.json"),
                     "--thread", "t", "--done-when", "checked", "work"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["http_status"] == status
    assert payload["body"] == body
    assert payload["retryable"] is retryable
    assert payload.get("outcome") == ("unknown" if unknown else None)


@pytest.mark.parametrize("command", ["invite", "pair"])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("failure", ["refused", 429, 503])
def test_pairing_code_retry_contract_after_partial_progress(
        monkeypatch, capsys, tmp_path, command, partial, failure):
    """A later rejection must not invite repetition of already-created codes."""
    state = {"base_url": "http://127.0.0.1:1", "peer_id": "test-peer"}
    monkeypatch.setattr(node, "load_state", lambda *args: state)
    monkeypatch.setattr(node, "require_peer", lambda *args: None)
    monkeypatch.setattr(node, "fresh_jwt", lambda *args: "test-jwt")
    monkeypatch.setattr(node, "_proved_headers", lambda *args: {})
    monkeypatch.setattr(node, "owner_session", lambda *args: {
        "cookie_name": "test-session", "cookie": "test-cookie",
        "csrf": "test-csrf", "owner_id": "test-owner"})
    requests = []

    def respond(method, base_url, route, **kwargs):
        requests.append((method, route))
        if partial and len(requests) == 1:
            return 201, {"code": "first-code"}
        if failure == "refused":
            raise ConnectionRefusedError("next request refused")
        return failure, {"message": "busy"}

    monkeypatch.setattr(node, "http_json", respond)
    assert cli.main([command, "--base-url", state["base_url"], "--state-file",
                     str(tmp_path / "state.json"), "--count", "2"]) == (
                         2 if failure == "refused" else 1)
    captured = capsys.readouterr()
    payload = json.loads(captured.err if failure == "refused" else captured.out)
    assert payload["retryable"] is (not partial)
    assert payload.get("outcome") == ("unknown" if partial else None)
    route = "/v1/auth/onetime" if command == "pair" else node.PEER_PAIRING_CODES
    assert requests == [("POST", route)] * (2 if partial else 1)


@pytest.mark.parametrize("command", ["invite", "pair"])
def test_pairing_code_success_with_malformed_body_is_not_safe_to_repeat(
        monkeypatch, capsys, tmp_path, command):
    monkeypatch.setattr(node, "load_state", lambda *args: {"base_url": "http://127.0.0.1:1"})
    monkeypatch.setattr(node, "require_peer", lambda *args: None)
    monkeypatch.setattr(node, "fresh_jwt", lambda *args: "test-jwt")
    monkeypatch.setattr(node, "_proved_headers", lambda *args: {})
    monkeypatch.setattr(node, "owner_session", lambda *args: {
        "cookie_name": "test-session", "cookie": "test-cookie", "csrf": "test-csrf"})
    requests = []

    def respond(*args, **kwargs):
        requests.append(args)
        return 201, {}

    monkeypatch.setattr(node, "http_json", respond)
    assert cli.main([command, "--base-url", "http://127.0.0.1:1", "--state-file",
                     str(tmp_path / "state.json"), "--count", "2"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["http_status"] == 201
    assert payload["retryable"] is False
    assert payload["outcome"] == "unknown"
    assert len(requests) == 1


def test_send_status_peek_is_the_one_read_only_status():
    """#549: `send-status --peek` reads the local journal only, so it is the one status form outside the write
    contract; plain send-status keeps it, since it can present an op again. MUTANTS: --peek classified as mutating;
    plain send-status as a read."""
    parser = cli.build_parser()
    peek = parser.parse_args(["send-status", "--peek", "--base-url", "http://x", "--state-file", "s.json", "op"])
    plain = parser.parse_args(["send-status", "--base-url", "http://x", "--state-file", "s.json", "op"])
    assert cli._mutating_command(peek) is False and cli._mutating_command(plain) is True


@pytest.mark.parametrize("command,expected_cmd", [
    (["send", "status", "existing-op"], "send-status"),
    (["send-status", "existing-op"], "send-status"),
    (["send", "drain"], "send-drain"),
    (["send-drain"], "send-drain"),
])
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "refused", 500, 429])
def test_send_reconciliation_commands_keep_write_retry_contract(
        monkeypatch, capsys, tmp_path, command, expected_cmd, accepted, failure):
    """Status can resubmit an unresolved write; its spelling cannot make it a read."""
    calls = []

    def fail(args):
        calls.append(args.cmd)
        if accepted:
            errors.note_call_accepted()
        if failure == "timeout":
            raise TimeoutError("lost response")
        if failure == "refused":
            raise ConnectionRefusedError("refused")
        raise node.ServerSaid(failure, {})

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main([*command, "--base-url", "http://127.0.0.1:1",
                     "--state-file", str(tmp_path / "state.json")]) == (
                         2 if isinstance(failure, str) else 1)
    captured = capsys.readouterr()
    payload = json.loads(captured.err if isinstance(failure, str) else captured.out)
    retryable = not accepted and failure in ("refused", 429)
    assert payload["retryable"] is retryable
    assert payload.get("outcome") == (None if retryable else "unknown")
    assert calls == [expected_cmd]


def test_accepted_call_tracking_does_not_leak_to_next_command():
    with errors.track_accepted_calls() as first:
        errors.note_call_accepted()
        assert first["accepted"] is True
    with errors.track_accepted_calls() as second:
        assert second["accepted"] is False


@pytest.mark.parametrize("exc,exit_code,stream,expected", [
    (node.ServerSaid(503, {"message": "busy"}), 1, "out",
     {"http_status": 503, "body": {"message": "busy"}, "code": "E_HTTP", "retryable": True}),
    (node.ServerSaid(403, {"message": "denied"}), 1, "out",
     {"http_status": 403, "body": {"message": "denied"}, "code": "E_HTTP", "retryable": False}),
    (node.Refused("unsafe"), 2, "err",
     {"refused": "unsafe", "code": "E_CLIENT_REFUSED", "retryable": False}),
])
def test_cli_http_and_refusal_contracts(monkeypatch, capsys, tmp_path,
                                      exc, exit_code, stream, expected):
    calls = []

    def fail(args):
        calls.append(args)
        raise exc

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["inbox", "--base-url", "http://127.0.0.1:1",
                     "--state-file", str(tmp_path / "state.json")]) == exit_code
    captured = capsys.readouterr()
    assert json.loads(getattr(captured, stream)) == expected
    assert getattr(captured, "err" if stream == "out" else "out") == ""
    assert len(calls) == 1
