"""The authentication surface: signature sign-in, owner sign-in, refresh rotation, JWT
issuance, JWKS, and minting a new node (pairing codes, onboarding, the first owner).

The modules implement the route payloads, JWT claim set, validation policy, server
key identifier and JWKS, key history, and first-owner bootstrap.

`bootstrap.py` and `manage.py bootstrap_owner` provide the first-owner flow
(docs/DIVERGENCES.md 016).

Read `tokens.py` first: it is the only module in here that is security-load-bearing on
its own, and it is the one a caller outside this package will import.
"""
