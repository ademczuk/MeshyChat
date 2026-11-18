"""ASGI entrypoint.

MeshyChat serves HTTP *and* the WebSocket gateway in one process, so it must be ASGI end
to end, not WSGI with a WS sidecar. This module
is the single callable both `uvicorn meshychat.asgi:application` and the test harness
load.

WebSocket routing is deliberately absent rather than stubbed: `apps/wsgw/` is another
agent's file set. When it lands it registers its own `URLRouter` and this module grows a
`"websocket"` key. A stub here would answer a WS upgrade with a *successful* handshake
that then goes silent, which is worse than the 404 an unmapped protocol gets today -
and it would sit in the one file the WS owner does not own.
"""
from __future__ import annotations

import io

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

# get_asgi_application() calls django.setup() itself, and must run before any module
# that touches settings is imported at module scope.
from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_app = get_asgi_application()

import psycopg  # noqa: E402

from apps.core import dbconn, schema_sets  # noqa: E402

#: Seconds the boot waits to REACH the database. Without it a black-holed host holds the boot
#: at libpq's default, which is no timeout at all.
BOOT_CONNECT_TIMEOUT_S = 10


class SchemaUnverified(RuntimeError):
    """The boot could not ASK whether the schema is complete: the database did not answer.

    Distinct from `schema_sets.SchemaIncomplete` (rule E1): "the database is missing
    migration 021" and "the database is not reachable" have different fixes, and an operator
    reading a crash loop must be told which one it is. For the same reason it is raised for
    `psycopg.OperationalError` only (no connection, a connect or statement timeout, a refused
    login). Anything else is a different fix and propagates as itself: a migration file
    `discover()` refuses (ValueError, CRLF or a duplicate version), a role the database REFUSES
    the ledger read (InsufficientPrivilege), a bypassing serving role (RlsBypassError).
    """


def require_complete_schema(open_connection=None) -> None:
    """Refuse to serve unless every declared migration set is applied (status item E14).

    `docs/V1-DESIGN-RULES.md` E6: "the deploy gate asserts schema is a superset of code, not
    'the process started'". `run_jobs` and `run_call_worker` already refused an incomplete
    schema before their first tick; the process that serves requests did not, so a deploy
    whose migrate step failed or was skipped booted, answered /v1/health, and then failed
    request by request on whatever the missing migration created. Asked HERE, once per process,
    because this module is what daphne and uvicorn
    import: nothing is served before the answer.

    NOT on /v1/health or /v1/livez: `schema_sets.assert_schema_complete` says why, and a
    liveness route must answer while Postgres is down. A database that cannot be reached AT
    BOOT is refused too: an unverified schema is not a verified one. `open_connection` is for tests; the default is the guarded
    request connection, so the question is asked as the role that serves.
    """
    opener = open_connection or (
        lambda: dbconn.serving_connection(connect_timeout=BOOT_CONNECT_TIMEOUT_S))
    try:
        with opener() as conn:
            schema_sets.assert_schema_complete(conn)
    except psycopg.OperationalError as exc:
        raise SchemaUnverified(
            "refusing to serve: the database did not answer the boot's schema check "
            f"({type(exc).__name__}: {exc}). The server does not start on a schema it cannot "
            "verify (docs/V1-DESIGN-RULES.md E6); start it once the database is reachable "
            "and accepts the serving login."
        ) from exc


require_complete_schema()


def disable_daphne_post_form_parsing() -> None:
    """Turn off Twisted's unused POST body parser on Daphne's `WebRequest` (#290).

    `daphne.http_protocol.WebRequest` subclasses `twisted.web.http.Request` and calls
    `http.Request.__init__` with its default `parsePOSTFormSubmission=True`. For any `POST`
    above 100 KB (`twisted.web.http._getContentFile`), Twisted spools the incoming body to a
    temporary file (`self.content`), and then `http.Request.requestReceived` reads the ENTIRE
    temporary file back into memory (`self.content.read()`) and runs `_getMultiPartArgs` or
    `parse_qs` into `self.args` whenever `Content-Type` is `multipart/form-data` or
    `application/x-www-form-urlencoded` -- before `WebRequest.process()` even builds the ASGI
    scope. `WebRequest` never reads `self.args` (it streams `self.content` into
    `self.application_queue` for Django's ASGI handler).

    Measured on `daphne 4.2.3` / `twisted 26.4.0` (#290): an unauthenticated 100 MB
    `multipart/form-data` POST drove the worker's peak working set from 85.3 MB to 1,558.5 MB
    (+1,473.2 MB, ~14.7x amplification) inside `_getMultiPartArgs`. Pinning
    `_parsePOSTFormSubmission` to `False` on `WebRequest` skips that dead parse completely.
    Requires `Twisted>=25.5.0` (where `parsePOSTFormSubmission` was added to `http.Request`).
    """
    import inspect
    from daphne.http_protocol import WebRequest
    from twisted.web import http

    if "parsePOSTFormSubmission" not in inspect.signature(http.Request.__init__).parameters:
        raise RuntimeError(
            "refusing to serve: installed Twisted does not support parsePOSTFormSubmission "
            "on twisted.web.http.Request (requires Twisted>=25.5.0; #290)."
        )

    WebRequest._parsePOSTFormSubmission = property(
        lambda self: False,
        lambda self, _value: None,
    )


disable_daphne_post_form_parsing()

#: The largest request body accepted before authentication, for every route but a raw file upload (#306).
#: Django refuses a body over DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB) only after it has arrived; this refuses it
#: from the declared length, before a byte is spooled. A mesh message body is at most 64 KB, a card or an edit a
#: few KB, so 4 MB leaves headroom over Django's own limit without admitting anything it would accept.
PREAUTH_BODY_MAX_BYTES = 4 * 1024 * 1024
_TOO_LARGE = b"HTTP/1.1 413 Content Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def preauth_body_cap(path: bytes) -> int:
    """The body cap for a request line's path. A raw file upload (`PUT /v1/files/raw/<token>`, the one route that
    carries large bodies) is capped at the file limit the files app READ AT STARTUP (`blob.active()`), so the two
    never disagree: MESHYCHAT_MAX_FILE_BYTES=0 refuses every non-empty upload in both, and a value changed in the
    environment after boot changes neither (#313 review). With no blob backend the route answers 503 whatever the
    body, so it gets the ordinary cap. Every other route gets PREAUTH_BODY_MAX_BYTES (MESHY_PREAUTH_BODY_MAX_BYTES
    overrides it)."""
    from apps.core.jsondepth import RAW_UPLOAD_PREFIX
    if (path or b"").split(b"?", 1)[0].startswith(RAW_UPLOAD_PREFIX.encode("ascii")):
        from apps.files import blob
        try:
            return blob.active()[0].max_file_bytes
        except blob.BlobError:
            pass
    return _positive_env_int("MESHY_PREAUTH_BODY_MAX_BYTES", PREAUTH_BODY_MAX_BYTES)


def cap_request_bodies() -> None:
    """Refuse an oversized request body with 413 before Twisted spools it (#306, the residual of #290).

    Twisted's `HTTPChannel.allHeadersReceived` calls `Request.gotLength(Content-Length)` before any body byte is
    read, and the request line's path is still on the channel (`_path`). A declared length over the route's cap
    gets a 413 and the connection closes: nothing is spooled to a temporary file and the app never runs. A body
    with no declared length (chunked) is counted in `handleContentChunk` and cut off at the same cap. A refused
    request never reaches `requestReceived`, so bytes already buffered on the connection cannot complete it into
    an empty-body call of the app. Measured before this (#290 comment): a body of any size was accepted at about
    1x before authentication."""
    from daphne.http_protocol import WebRequest
    from twisted.web import http

    base_got_length = http.Request.gotLength
    base_chunk = http.Request.handleContentChunk
    base_received = WebRequest.requestReceived

    def _refuse(request) -> None:
        request._meshy_body_refused = True
        channel = request.channel
        if channel is not None and getattr(channel, "transport", None) is not None:
            channel.transport.write(_TOO_LARGE)
            channel.loseConnection()

    def gotLength(self, length):
        self._meshy_body_cap = preauth_body_cap(getattr(self.channel, "_path", b""))
        self._meshy_body_seen = 0
        self._meshy_body_refused = False
        if length is not None and length > self._meshy_body_cap:
            self.content = io.BytesIO()
            # HTTPChannel.allHeadersReceived sends "100 Continue" for `Expect: 100-continue` right after gotLength
            # returns, which put a 100 on the wire after the 413 (#313 review). Without the header it sends none.
            self.requestHeaders.removeHeader(b"Expect")
            _refuse(self)
            return
        base_got_length(self, length)

    def handleContentChunk(self, data):
        if getattr(self, "_meshy_body_refused", False):
            return
        self._meshy_body_seen = getattr(self, "_meshy_body_seen", 0) + len(data)
        if self._meshy_body_seen > getattr(self, "_meshy_body_cap", PREAUTH_BODY_MAX_BYTES):
            _refuse(self)
            return
        base_chunk(self, data)

    def requestReceived(self, command, path, version):
        if getattr(self, "_meshy_body_refused", False):
            return
        return base_received(self, command, path, version)

    WebRequest.gotLength = gotLength
    WebRequest.handleContentChunk = handleContentChunk
    WebRequest.requestReceived = requestReceived


cap_request_bodies()

# A wedged server leaves its thread stacks when the operator's livez watcher asks (off unless MESHY_STACKDUMP_DIR
# is set; apps/core/stackdump.py says why: the 10 Nov 2026 outage left no record of what was waiting).
from apps.core import stackdump  # noqa: E402

stackdump.install()


from channels.routing import ProtocolTypeRouter  # noqa: E402

from channels.routing import URLRouter  # noqa: E402
from apps.core.capability_header import RefuseCapabilityUpgrade  # noqa: E402
from apps.wsgw.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # MOUNTED 2026-09-21: the hub was unreachable. apps/wsgw defined its consumer and its
    # routing and nothing referenced them, so the server answered no WebSocket at all
    # while the module's own tests passed against the consumer directly.
    # An upgrade carrying `Meshychat-Capability` is closed before accept, outside the router, so
    # no route or consumer sees it (B07 slice 1, the project's design log (not part of this release) 040). Django middleware
    # never runs for a WebSocket scope, so the HTTP refusal cannot cover this.
    "websocket": RefuseCapabilityUpgrade(URLRouter(websocket_urlpatterns)),
    # "websocket": ... owned by apps/wsgw/.
})
