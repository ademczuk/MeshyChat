"""ASGI routing for the hub.

The socket route is `/v1/ws` for every method. The path is part of the wire
contract twice over: a client hard-codes it, and the DPoP proof is bound to it,
so moving this route silently invalidates every proof.
"""
from __future__ import annotations

from django.urls import path

from .constants import WS_PATH
from .consumer import MeshyWebSocketConsumer

#: `path()` patterns are relative, so the leading slash is stripped here and put
#: back by whatever mounts this list.
websocket_urlpatterns = [
    path(WS_PATH.lstrip("/"), MeshyWebSocketConsumer.as_asgi()),
]
