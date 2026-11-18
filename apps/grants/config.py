"""Runtime flags this surface reads.

  MESHYCHAT_MANDATE_REQUIRE_AT_ISSUANCE
                                      Default OFF. When ON, a grant must carry a
                                      verified owner-signed mandate.
  MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE
                                      Default OFF. A version pin
                                     verifies a PRESENT publisher attestation either
                                     way; ON also refuses an ABSENT one. Parsed once by
                                      settings.py (exactly true/false; anything else
                                      refuses to start), and
                                     read through apps/core/attestation_flag.py, the
                                     reader apps/skillsreg shares (B09).
  MESHYCHAT_BISCUIT_ROOT_SK          the deploy-dark capability-minting root key. When
                                     UNSET the response is byte-identical to a bare
                                      grant. See service.py: capability tokens are not
                                      minted.
  MESHYCHAT_ALLOW_DEV_JWT            Whether a non-PAT Bearer JWT may authenticate an
                                      owner. Read by `apps.dashboard.auth.bearer_jwt_allowed`,
                                      the one switch for every owner surface (divergence 113).

The session cookie NAME and the CSRF STATE SECRET are deliberately not read here any more.
This module read them from the environment only, while the dashboard extractor (and owner
sign-in, and /v1/auth/me) read a Django setting first, so the two surfaces could look for
different cookies and hold different secrets (STATUS B10). apps/grants/owner_auth.py uses
`apps.dashboard.auth.session_cookie_name` and `state_secret`, which read both sources.

Values are read at CALL time, never at import: reading at import would freeze whichever
value existed when the first module imported this one, and every test that sets one
would depend on collection order. The attestation flag is read at call time too, from the
setting settings.py parsed at boot, so `override_settings` switches it.
"""
from __future__ import annotations

from apps.core.env import lookup


def _flag(name: str, default: bool = False) -> bool:
    raw = lookup(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def mandate_require_at_issuance() -> bool:
    return _flag("MESHYCHAT_MANDATE_REQUIRE_AT_ISSUANCE", False)


def manifest_attestation_enforce() -> bool:
    """The one reader apps/skillsreg/service.py shares (B09).

    Not `_flag`: `_flag` accepts 1/true/yes/on in any case, whereas the setting read
    here is parsed once at boot as exactly `true` or `false` (anything else refuses to
    start).
    """
    from apps.core import attestation_flag

    return attestation_flag.enforce_from_settings()


def biscuit_minting_enabled() -> bool:
    """True when a root key is configured.

    `service.issue_grant` refuses this configuration because capability-token minting is
    unavailable; the supported configuration leaves the key unset.
    """
    return bool((lookup("MESHYCHAT_BISCUIT_ROOT_SK") or "").strip())

