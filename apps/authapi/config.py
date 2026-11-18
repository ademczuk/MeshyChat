"""Runtime configuration for the auth surface, read from the environment and
overridable in-process for tests.

  MESHYCHAT_JWT_ISSUER                      -> jwt_issuer. REQUIRED, NO default
  MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH        -> the PKCS#8 PEM used to mint.
                                               MESHYCHAT_JWT_SIGNING_KEY_PEM_PATH, an earlier
                                               spelling of the same setting, is still read
                                               when that one is unset.
  MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH         -> the current verify key
  MESHYCHAT_JWT_PUBLIC_KEY_HISTORY_PEM_PATHS
                                            -> ';' or ',' separated rotated-out keys
                                               (both separators)
  MESHYCHAT_DPOP_ENFORCE                    -> DPoP on peer writes and the WS upgrade.
                                               ABSENT MEANS ON

ONE PARSER FOR MINT AND VERIFY (docs/DIVERGENCES.md 015). `jwt_material_from_env` is the
only reader of these variables. `AuthConfig.from_env` (the minter) and
meshychat/settings.py (`MESHY_JWT_ISSUER`, `MESHY_JWT_PUBLIC_KEY_PEM`,
`MESHY_DPOP_ENFORCE`, which every verifier reads) both call it. Before that, the minter
read these variables and the verifiers read settings that settings.py never defined, so
every peer route answered 500 in a real deploy while each side's tests, which injected
their own keys, stayed green.

WHY THE HISTORY LIST EXISTS AND WHY ITS ABSENCE IS DANGEROUS: a server with no history
configured boots perfectly clean. Nothing fails until an invocation frame signed under the
previous key arrives (token authentication deliberately uses only the current key,
tokens.py `verify_token`). The `AuthConfig.verify_keys` method returns the current key plus
every history path, and an unreadable or unparseable history file raises at settings load, so a
mistyped path surfaces at boot, not at 3am.
`AuthConfig.from_env` keeps that property: it raises on an unreadable history path.

Boot validation rejects a public key that is not an Ed25519 PEM, a private key that is
not one, and a private key that is not the keypair partner of the public key. Each raises
`JwtConfigError` when settings load. The last is a quiet failure: a mismatched pair mints
tokens that verify nowhere, and nothing says so until the first request.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from apps.core.env import lookup

#: Challenge lifetime.
CHALLENGE_TTL_SECS = 120
#: Sign-in JWT lifetime.
SIGNIN_JWT_TTL_SECS = 3600
#: Peer JWT lifetime on refresh.
PEER_JWT_TTL_SECS = 3600
#: Rotated refresh-token lifetime.
REFRESH_TOKEN_TTL_DAYS = 30
#: Domain separation for the sign-in signature.
SIGNIN_PURPOSE = "meshy-signin-v1"
#: The owner/dashboard variant. Different payload shape, so a
#: sign-in signature can never be replayed as an owner-session mint or vice versa.
OWNER_SIGNIN_PURPOSE = "meshy-owner-signin-v1"
#: Owner-session lifetime used when no SSO service is configured.
OWNER_SESSION_TTL_SECS = 86_400

# Test-only. Never a production fallback: this user is rolsuper and rolbypassrls, so a
# silent fallback to it disables row-level security for the whole auth surface.
DEFAULT_TEST_DSN = "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django"


def _require_dsn(env):
    """MESHY_DSN, or MESHY_TEST_DSN only when explicitly running tests. Otherwise raise."""
    dsn = env.get("MESHY_DSN")
    if dsn:
        return dsn
    if env.get("MESHY_ALLOW_TEST_DSN") == "1" or "PYTEST_CURRENT_TEST" in env:
        return env.get("MESHY_TEST_DSN", DEFAULT_TEST_DSN)
    raise RuntimeError(
        "MESHY_DSN is not set. Refusing to fall back to the development DSN: that user "
        "bypasses row-level security, so the fallback would serve every tenant's rows "
        "without raising anything.")


def _split_path_list(raw: str | None) -> tuple[str, ...]:
    """Split on ';' or ',', trim, and drop empty items."""
    if not raw:
        return ()
    parts = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return tuple(parts)


#: The spellings a human writes into a shell for a boolean switch. Anything else is a
#: typo, and a typo in the value of a DEFAULT-ON security switch must not read as "off".
_FLAG_TRUE = ("1", "true", "yes", "on")
_FLAG_FALSE = ("0", "false", "no", "off")


class JwtConfigError(RuntimeError):
    """A JWT/DPoP configuration that prevents the server from starting."""


@dataclass(frozen=True)
class JwtMaterial:
    """Everything mint and verify need, parsed ONCE from one environment."""

    #: None = not configured. There is no fallback default: a wrong issuer is a silent
    #: failure mode. A default here is how the gateway ended up expecting "meshy" while the
    #: minter stamped "meshy-server" - two defaults, each plausible, never compared.
    issuer: str | None
    public_key_pem: bytes | None
    signing_key_pem: bytes | None
    public_key_history_pem: tuple[bytes, ...]
    #: DPoP enforcement defaults to true and applies to the HTTP extractor and WebSocket
    #: upgrade. Default-on behavior is covered by configuration tests.
    dpop_enforce: bool


def _dpop_enforce(env) -> bool:
    raw = lookup("MESHYCHAT_DPOP_ENFORCE", source=env)
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value in _FLAG_TRUE:
        return True
    if value in _FLAG_FALSE:
        return False
    raise JwtConfigError(
        f"MESHYCHAT_DPOP_ENFORCE={raw!r} is not a boolean. It defaults ON; to turn DPoP "
        f"enforcement off set it to 'false' explicitly.")


def _signing_key_path(env) -> str | None:
    primary_path = (lookup("MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH", source=env) or "").strip() or None
    legacy = (lookup("MESHYCHAT_JWT_SIGNING_KEY_PEM_PATH", source=env) or "").strip() or None
    if primary_path and legacy and primary_path != legacy:
        raise JwtConfigError(
            "MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH and MESHYCHAT_JWT_SIGNING_KEY_PEM_PATH name different "
            "files. They are two spellings of ONE setting; set only MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH.")
    return primary_path or legacy


def jwt_material_from_env(env=None) -> JwtMaterial:
    """The single reader of the JWT/DPoP environment.

    Raises `JwtConfigError` (or OSError for an unreadable path) on an invalid boot
    configuration. Returns None fields for what is simply NOT configured, and every
    consumer fails CLOSED on a None: the minter answers 503, the verifiers 500 or a
    refused upgrade.
    """
    # Imported here so meshychat/settings.py can import this module without pulling the
    # JWT stack in at module scope.
    from cryptography.hazmat.primitives import serialization

    from apps.authapi import tokens

    env = os.environ if env is None else env

    def read(path: str | None) -> bytes | None:
        return Path(path).read_bytes() if path else None

    def public(pem: bytes, what: str) -> bytes:
        try:
            tokens.load_public_key(pem)
        except tokens.JwtError as exc:
            raise JwtConfigError(f"{what} is not an Ed25519 public key PEM: {exc}") from exc
        return pem

    issuer = (lookup("MESHYCHAT_JWT_ISSUER", source=env) or "").strip() or None
    public_pem = read((lookup("MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH", source=env) or "").strip() or None)
    if public_pem is not None:
        public(public_pem, "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH")
    history = tuple(
        public(Path(p).read_bytes(), f"history key {p}")
        for p in _split_path_list(lookup("MESHYCHAT_JWT_PUBLIC_KEY_HISTORY_PEM_PATHS", source=env))
    )
    signing_pem = read(_signing_key_path(env))
    if signing_pem is not None:
        try:
            partner = tokens.load_signing_key(signing_pem).public_key()
        except tokens.JwtError as exc:
            raise JwtConfigError(f"the JWT private key is not an Ed25519 PEM: {exc}") from exc
        if public_pem is None:
            raise JwtConfigError(
                "a JWT private key is configured but MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH is not: "
                "this server would mint tokens it cannot verify")
        raw = partner.public_bytes(encoding=serialization.Encoding.Raw,
                                   format=serialization.PublicFormat.Raw)
        if raw != tokens.public_key_raw(public_pem):
            # The configured private and public keys must be a matching pair.
            raise JwtConfigError(
                "the JWT private key is not the keypair partner of "
                "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH: every token it minted would fail verification")
    return JwtMaterial(
        issuer=issuer,
        public_key_pem=public_pem,
        signing_key_pem=signing_pem,
        public_key_history_pem=history,
        dpop_enforce=_dpop_enforce(env),
    )


@dataclass(frozen=True)
class AuthConfig:
    #: None = not configured: this server then neither mints (sign-in and refresh answer
    #: 503) nor verifies (peer routes answer 500). No default - see JwtMaterial.issuer.
    jwt_issuer: str | None = None
    #: None = this server cannot mint. Both sign-in and refresh answer 503
    #: E_REDEEM_FAILED in that case.
    jwt_signing_key_pem: bytes | None = None
    jwt_public_key_pem: bytes | None = None
    jwt_public_key_history_pem: tuple[bytes, ...] = ()
    dsn: str = DEFAULT_TEST_DSN
    #: Proxy headers are consulted only when the operator
    #: asserts this deployment is behind a trusted proxy. Default false, so a forged
    #: X-Forwarded-For cannot be used to pick a fresh limiter key per request.
    behind_trusted_proxy: bool = False
    challenge_ttl_secs: int = CHALLENGE_TTL_SECS
    signin_jwt_ttl_secs: int = SIGNIN_JWT_TTL_SECS
    peer_jwt_ttl_secs: int = PEER_JWT_TTL_SECS
    refresh_token_ttl_days: int = REFRESH_TOKEN_TTL_DAYS

    def verify_keys(self) -> tuple[bytes, ...]:
        """Current key first, then history — the order `verify` tries them in."""
        keys = []
        if self.jwt_public_key_pem:
            keys.append(self.jwt_public_key_pem)
        keys.extend(self.jwt_public_key_history_pem)
        return tuple(keys)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AuthConfig":
        env = os.environ if env is None else env
        jwt = jwt_material_from_env(env)
        return cls(
            jwt_issuer=jwt.issuer,
            jwt_signing_key_pem=jwt.signing_key_pem,
            jwt_public_key_pem=jwt.public_key_pem,
            jwt_public_key_history_pem=jwt.public_key_history_pem,
            # FAIL CLOSED, and read MESHY_DSN FIRST. Two defects found in review:
            # (1) MESHY_TEST_DSN was consulted BEFORE MESHY_DSN, so a stray test variable
            #     silently redirected live auth traffic to the test database;
            # (2) DEFAULT_DSN is the dev superuser (rolsuper=t, rolbypassrls=t), so with
            #     MESHY_DSN unset the auth surface ran as an RLS-BYPASSING superuser and
            #     nothing said so. An unconfigured server must refuse to start.
            dsn=_require_dsn(env),
            behind_trusted_proxy=lookup("MESHYCHAT_BEHIND_TRUSTED_PROXY", "", source=env).lower()
            in ("1", "true", "yes"),
        )


_config: AuthConfig | None = None


def get_config() -> AuthConfig:
    global _config
    if _config is None:
        _config = AuthConfig.from_env()
    return _config


def set_config(config: AuthConfig) -> None:
    global _config
    _config = config


@contextlib.contextmanager
def override_config(**kwargs):
    """Swap configuration for the duration of a block. Used by tests to install a
    freshly generated key pair without writing PEMs to disk."""
    global _config
    previous = get_config()
    set_config(replace(previous, **kwargs))
    try:
        yield get_config()
    finally:
        set_config(previous)
