"""`AuthenticatedOwner` - the owner identity every route on this surface acts as.

The RESOLUTION ORDER is the security property, not an implementation detail:

  1. COOKIE `meshy_session` -> owner, FULL authority. A cookie that is PRESENT but does
     not resolve to an active row is `E_SESSION_EXPIRED` (401) and DOES NOT fall
     through: a stale cookie must never escalate to another
     scheme. No cookie at all -> continue.
     Cookie auth is the only AMBIENT credential (the browser attaches it by itself), so
     it is the only path that carries a CSRF requirement on state-changing methods.
  2. PAT `Authorization: Bearer meshypat_…` -> resolved by SHA-256 hash lookup against
     `owner_api_keys`, ALWAYS ON. A meshypat_-prefixed token is resolved here or
     REJECTED here (`E_API_KEY_INVALID`, 401); it never falls through to the JWT path.
     A PAT is constrained to its scopes.
  3. Bearer JWT with `aud = "dashboard"` -> owner, full authority, gated by
     `apps.dashboard.auth.bearer_jwt_allowed` (every off switch closes it everywhere).
  4. No Authorization header at all -> `E_AUTH_MISSING` (401).

TWO CHECKS THAT LOOK REDUNDANT AND ARE NOT:

  * the JWT path re-checks that a `dashboard_users` row EXISTS for the sub. Without it
    a forged-sub dev JWT resolves to a
    non-existent owner and surfaces as a 500 from a foreign-key violation deep in a
    write, instead of a clean 401 here.
  * `require_scope` passes unconditionally for Cookie/DevJwt and checks membership for
    a PAT. Applying the scope check to a cookie owner would
    lock the dashboard out of its own routes; skipping it for a PAT would make every
    PAT full authority.

The session token and the PAT are both stored as HEX SHA-256 of the raw token - a fast hash,
correct here
because both are 256-bit CSPRNG values rather than passwords.

ONE EXTRACTOR (REFACTOR-QUEUE-2026-11 B1). Everything above is implemented ONCE, in
`apps/dashboard/auth.py`, which owner sign-in, `/v1/auth/me` and `/v1/peers` already use.
This module was a step-for-step copy of it, and three pieces had already been folded back
into re-exports (the cookie name, the CSRF secret, the JWT verifier: STATUS B10 and B13),
each after the copies had drifted. This is the last step: the names below are bound one by
one to the dashboard's, so this surface cannot grow a second opinion again. What stays here
is what only this surface has: its two scopes. The one wire difference the copy had went
with it: a dev JWT whose `sub` is not a uuid is now refused with "token subject is not a
uuid", as on the dashboard, where this copy said "unknown owner" (both 401 E_AUTH_INVALID;
docs/DIVERGENCES.md 113).

WHAT THE OWNER OBJECT CARRIES. `AuthenticatedOwner` here is the dashboard's class: `owner_id`,
`method`, `key_id`, `scopes` (a tuple, not a list) and `claims`. A cookie session's id is
`claims["session_id"]`. There is no `session_id` or `expires_at` attribute, and the session's
expiry is not carried at all: a route that needs it reads `owner_sessions` itself.
"""
from __future__ import annotations

from apps.core import websec
from apps.dashboard import auth as _dash

#: Prefix for owner personal access tokens.
PAT_PREFIX = _dash.PAT_PREFIX
#: The two scopes that make /v1/grants/* callable headless.
GRANTS_READ = "grants:read"
GRANTS_WRITE = "grants:write"

#: The values of `AuthenticatedOwner.method`.
COOKIE = "cookie"
PAT = "pat"
DEV_JWT = "dev_jwt"

AuthenticatedOwner = _dash.AuthenticatedOwner
authenticate_owner = _dash.authenticate_owner
session_cookie_name = _dash.session_cookie_name
state_secret = _dash.state_secret
sha256_hex = _dash._sha256_hex
_bearer = _dash._bearer
_verify_dashboard_jwt = _dash.verify_dashboard_jwt
#: The one implementation (apps/core/websec.py).
csrf_token_for = websec.csrf_token_for
