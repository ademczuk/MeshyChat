"""Authentication route table.

Paths are absolute and version-prefixed. `/.well-known/jwks.json` has no /v1 prefix
because the well-known path is fixed by RFC 8615.

Included from the project URLconf with `path("", include("apps.authapi.urls"))` — no
prefix, or every path above moves.
"""
from django.urls import path

from apps.authapi import onboard, views

urlpatterns = [
    path("v1/auth/challenge", views.challenge, name="auth-challenge"),
    path("v1/auth/sign-in", views.sign_in, name="auth-sign-in"),
    path("v1/auth/owner/challenge", views.owner_challenge, name="auth-owner-challenge"),
    path("v1/auth/owner/sign-in", views.owner_sign_in, name="auth-owner-sign-in"),
    path("v1/auth/refresh", views.refresh, name="auth-refresh"),
    # How a cookie session learns its CSRF token; without it a
    # headless owner cannot mint a pairing code (the project's design log (not part of this release) 016).
    path("v1/auth/me", views.me, name="auth-me"),
    # Pairing-code bootstrap. Mounted through this module so the
    # project URLconf needs no new include.
    path("v1/auth/onetime", onboard.mint_onetime, name="auth-onetime"),
    path("v1/onboard/peers", onboard.create_peer, name="onboard-peers"),
    # Divergence 062: any active peer mints a code for its own owner.
    path("v1/peers/me/pairing-codes", onboard.mint_peer_code, name="peer-pairing-codes"),
    # The dashboard wizard calls this path. It uses the same table, 300 s TTL, scopes,
    # and 201 {code, expires_at, deep_link} response as mint_onetime, so one view owns
    # the behavior (F05).
    path("v1/dashboard/pairing/generate", onboard.mint_onetime,
         name="dashboard-pairing-generate"),
    # No auth: the invitation code is the authorisation (F05).
    path("v1/auth/redeem-invitation", views.redeem_invitation, name="auth-redeem-invitation"),
    # Owner-sign-in allowlist (F05). require_human on all handlers.
    path("v1/owner/human-keys", views.human_keys, name="owner-human-keys"),
    path("v1/owner/human-keys/<str:kid>", views.revoke_human_key,
         name="owner-human-key-revoke"),
    path(".well-known/jwks.json", views.server_jwks, name="server-jwks"),
    # Invocation doorbell key set, separate from the token key (divergence 041,
    # operator decision 2026-09-25).
    path(".well-known/meshy-invoke-jwks.json", views.invoke_webhook_jwks,
         name="invoke-webhook-jwks"),
]
