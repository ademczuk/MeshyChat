"""Request plumbing: a guarded connection per request, bound to the caller's identity.

This module owns the SEAM between authentication and the database. The auth app decides
*who* the caller is; this middleware decides *what the database is allowed to show them*
and it does so by binding `app.current_peer_id` / `app.current_owner_id` for exactly the
duration of the view, on a connection that has passed `dbconn.assert_serving_connection`.

THE INTERFACE THE AUTH MODULE FILLS
-----------------------------------
    from apps.core.middleware import Identity, set_identity_resolver

    def resolve(request) -> Identity | None:
        ...                       # None  => anonymous; no peer context is bound
        return Identity(peer_id=..., owner_id=...)

    set_identity_resolver(resolve)          # or settings.MESHY_IDENTITY_RESOLVER,
                                            # a dotted path, which wins if set.

A resolver raises `Unauthenticated` to reject a request outright (401); returning None
means "no identity, and that is allowed here". `/v1/health`, `/v1/agent-card`,
`/.well-known/jwks.json`, and the auth routes permit anonymous requests, so anonymous is
a real, routine case, not an error. Which paths *require* an identity is a routing decision and belongs to the
route modules, not here.

WHY THE CONNECTION IS LAZY
--------------------------
`/v1/health` must answer when Postgres is down and say so: a check that always returned 200
could never evict a dead instance. If this
middleware connected eagerly, a dead database would turn the liveness probe into a 500
and the deep probe could never report `db:false`. So the connection is opened on first
use and closed in a `finally`. An anonymous request that touches no database opens no
connection at all.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from apps.core import dbconn

log = logging.getLogger("meshychat.request")


class Unauthenticated(Exception):
    """Raised by an identity resolver to reject the request before it reaches a view."""

    def __init__(self, code: str = "E_AUTH_MISSING", message: str = "missing auth"):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Identity:
    """The authenticated caller, reduced to what the DATABASE keys on.

    `peer_id` -> app.current_peer_id   (peer-scoped policies: skill_calls, local_memory)
    `owner_id` -> app.current_owner_id (tenant-scoped policies: meshy_memory)
    Server-internal (`app.server_internal='1'`) is deliberately NOT reachable from an
    identity: it is a privileged context entered for one operation via
    `dbconn.request_server_internal`, never for the length of a request (rls.py).

    `claims` carries whatever the auth module wants downstream (sub, aud, kid, jti,
    DPoP thumbprint...). Nothing in this module reads it.
    """

    peer_id: Optional[str] = None
    owner_id: Optional[str] = None
    claims: dict[str, Any] = field(default_factory=dict)

    def binds_context(self) -> bool:
        return self.peer_id is not None or self.owner_id is not None


IdentityResolver = Callable[[Any], Optional[Identity]]


def anonymous_resolver(request) -> Optional[Identity]:
    """The default: every request is anonymous until the auth module registers itself."""
    return None


_resolver: IdentityResolver = anonymous_resolver


def set_identity_resolver(fn: IdentityResolver) -> None:
    """Called once by the auth app (e.g. from its AppConfig.ready)."""
    global _resolver
    _resolver = fn


def get_identity_resolver() -> IdentityResolver:
    """settings.MESHY_IDENTITY_RESOLVER (dotted path) wins over a registered callable."""
    from django.conf import settings
    from django.utils.module_loading import import_string

    path = getattr(settings, "MESHY_IDENTITY_RESOLVER", None)
    if path:
        return import_string(path)
    return _resolver


class RequestDatabase:
    """Lazily opens ONE guarded connection and keeps it for the life of the request."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._conn = None

    @property
    def opened(self) -> bool:
        return self._conn is not None

    @property
    def connection(self):
        if self._conn is None:
            conn = self._factory()
            try:
                # The refusal happens HERE, on the request path, not at connect time:
                # a factory can hand back any connection (a pool, a test double), and
                # the guard is what makes "serves a request" imply "subject to RLS".
                dbconn.assert_serving_connection(conn)
            except Exception:
                # A connection that failed the serving guard is closed for real. A pooled one has `discard`; its
                # close() would return the session to the pool for the next request to meet.
                getattr(conn, "discard", conn.close)()
                raise
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


class DatabaseConnectionMiddleware:
    """Attach `request.db` (lazy, guarded) and bind the peer context around the view.

    Sync-only: psycopg connections here are blocking, and Django runs a sync middleware
    in a threadpool under ASGI. Declaring it rather than letting Django guess keeps one
    connection on one thread for one request.
    """

    sync_capable = True
    async_capable = False

    #: Overridable so tests can inject a connection without a live pool. Production
    #: never sets it; the default is the guarded autocommit + SET ROLE meshychat_app path.
    connection_factory: Optional[Callable[[], Any]] = None

    def __init__(self, get_response):
        self.get_response = get_response

    def _factory(self):
        f = self.connection_factory
        return f() if f is not None else dbconn.connect()

    def __call__(self, request):
        request.db = RequestDatabase(self._factory)
        request.identity = None
        try:
            try:
                identity = get_identity_resolver()(request)
            except Unauthenticated as exc:
                return self._unauthenticated(exc)
            request.identity = identity

            if identity is not None and identity.binds_context():
                # peer_context is a TRANSACTION. Holding it open across the view is
                # what makes the view's reads and writes atomic AND scoped; ending the
                # block is what clears the setting, so it cannot be inherited by the
                # next request on a pooled connection.
                with dbconn.request_peer_context(
                    request.db.connection,
                    peer_id=identity.peer_id,
                    owner_id=identity.owner_id,
                ):
                    return self.get_response(request)
            return self.get_response(request)
        finally:
            request.db.close()

    @staticmethod
    def _unauthenticated(exc: Unauthenticated):
        # Imported here, where the JsonResponse import it replaces was: this module
        # imports no Django at module level, and the envelope module imports django.http.
        from apps.core.jsonerrors import envelope

        log.info("rejected unauthenticated request: %s", exc.code)
        # `envelope` emits exactly these two keys. Its compact separators keep the 401 body
        # in the same wire form as other compact JSON responses.
        return envelope(exc.code, exc.message, status=401)
