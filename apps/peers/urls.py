"""URL map for peers, presence and agent cards.

Paths use Django's `<str:peer_id>` for a `{peer_id}` segment. There is no prefix, so
every path below is absolute exactly as written.

`<str:peer_id>` rather than `<uuid:peer_id>` is deliberate. Django's `uuid` converter
would not match a malformed id, so the route would fall through to a 404 produced by
the URL resolver with an HTML body. A malformed id must answer 400 with the JSON error
body, so the id is parsed inside the view where the status and error shape are under this
app's control.

`jwks.json` and `agent-card.json` are literal path segments, not a `.json` suffix
convention - the dot is part of the route.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("v1/peers/me/turn", views.turn_report, name="peer-turn-report"),
    path("v1/peers/me/presence", views.presence_beat, name="presence-beat"),
    path("v1/peers/presence", views.presence_list, name="presence-list"),

    path("v1/peers/me/keys", views.my_keys, name="my-keys"),
    path("v1/peers/me/keys/<str:kid>", views.revoke_my_key, name="revoke-my-key"),
    path("v1/peers/<str:peer_id>/keys/active", views.peer_active_key, name="peer-active-key"),

    # OWNER-authenticated (F05). DELETE /v1/peers/{peer_id} (F35, OI-67) also revokes
    # the peer's live sessions.
    path("v1/peers", views.peers_collection, name="peers-collection"),
    # DELETE only; every other method is 405. After the two literal routes above
    # (`v1/peers/presence`, `v1/peers`), static segments win over `{peer_id}`.
    path("v1/peers/<str:peer_id>", views.delete_peer, name="peer-delete"),
    # PEER-authenticated; the mint for redeem-invitation (F35, OI-67).
    path("v1/invites", views.mint_invite, name="invite-mint"),
    path("v1/peers/<str:peer_id>/keys", views.publish_peer_key, name="peer-publish-key"),

    # OWNER-authenticated (F05).
    path("v1/peers/<str:peer_id>/tokens", views.mint_peer_token, name="peer-mint-token"),

    # The first two are PUBLIC, the third is peer-authenticated.
    path("agents/<str:peer_id>/jwks.json", views.jwks, name="peer-jwks"),
    path("agents/<str:peer_id>/agent-card.json", views.serve_card, name="peer-agent-card"),
    path("v1/peers/me/agent-card", views.publish_card, name="publish-agent-card"),

    # Divergence 041 S9 (design 4.11, Q9). PUT and DELETE, peer-authenticated.
    path("v1/peers/me/invocation-webhook", views.invocation_webhook,
         name="invocation-webhook"),
]
