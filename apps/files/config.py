"""Blob configuration read from the environment.

  MESHYCHAT_BLOB_BACKEND   "fs" | "r2"   REQUIRED, no default: a wrong backend is a
                           silent failure mode for files.
  MESHYCHAT_BLOB_DIR       fs only, required when backend=fs
  MESHYCHAT_BLOB_TOKEN_SECRET
                           fs only, required, HEX-encoded HMAC key
  MESHYCHAT_PUBLIC_BASE_URL
                           fs only, required; presigned URLs are built as
                           `{public_base_url}/v1/files/raw/{token}`
  MESHYCHAT_R2_ACCOUNT_ID / _BUCKET / _ACCESS_KEY_ID / _SECRET_ACCESS_KEY / _ENDPOINT
  MESHYCHAT_UPLOAD_WINDOW_SECS
                           default 3600
  MESHYCHAT_PRESIGN_TTL_SECS
                           default 900
  MESHYCHAT_MAX_FILE_BYTES
                           default 100 MiB = 104857600

READ ONCE, AT STARTUP (status item B11, 2026-09-25). `apps/files/apps.py` calls
`load_at_startup()` from the AppConfig's ready(), which daphne and uvicorn reach through
meshychat.asgi and every manage.py command through its own django.setup(). The store is
built there too (`blob.activate`), and the routes use that one configuration for the life
of the process (`blob.active`). A MESHYCHAT_BLOB_BACKEND that is SET and wrong - an unknown
backend, a required field missing, a secret that is not hex, a size or window that is not
a non-negative integer - raises ImproperlyConfigured, so a partial configuration never
    serves requests. Before this the values were parsed per request, so the same mistake
    booted a server that answered 503 on every file route.

ONE EXCEPTION REMAINS: MESHYCHAT_BLOB_BACKEND UNSET starts with a warning, and every
/v1/files route answers 503 E_BLOB_BACKEND_UNAVAILABLE. A node without blob storage still
serves chat (README "Fresh install", docs/DIVERGENCES.md 035), so refusing startup would
stop every fresh install; that is an operator decision, not a code fix.

Tests change the environment between cases and re-read it with `blob.activate(...)`;
nothing else re-reads it.
"""
from __future__ import annotations

from dataclasses import dataclass
from apps.core.env import lookup

#: Named constants so a test asserts against the same value the code uses
#: rather than against a number retyped in two places.
DEFAULT_UPLOAD_WINDOW_SECS = 3600
DEFAULT_PRESIGN_TTL_SECS = 900
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024

BACKEND_FS = "fs"
BACKEND_R2 = "r2"


class ConfigError(RuntimeError):
    """Raised, never defaulted: a missing MESHYCHAT_BLOB_DIR must not silently become the
    process working directory."""


@dataclass(frozen=True)
class FsConfig:
    blob_dir: str
    token_secret: str          # hex; `FsBlobStore` decodes it
    public_base_url: str

    def __repr__(self) -> str:  # The secret never renders.
        return (f"FsConfig(blob_dir={self.blob_dir!r}, token_secret=<redacted>, "
                f"public_base_url={self.public_base_url!r})")


@dataclass(frozen=True)
class R2Config:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    endpoint: str | None = None

    def __repr__(self) -> str:  # The secret never renders.
        return (f"R2Config(account_id={self.account_id!r}, bucket={self.bucket!r}, "
                f"access_key_id={self.access_key_id!r}, secret_access_key=<redacted>, "
                f"endpoint={self.endpoint!r})")


@dataclass(frozen=True)
class BlobConfig:
    backend: str
    fs: FsConfig | None
    r2: R2Config | None
    upload_window_secs: int
    presign_ttl_secs: int
    max_file_bytes: int


def _int_env(name: str, default: int) -> int:
    raw = lookup(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} is not an integer: {raw!r}") from exc
    if value < 0:
        # A negative window or TTL would presign URLs that expired before they were issued.
        raise ConfigError(f"{name} must be a non-negative integer: {raw!r}")
    return value


def _require(name: str, backend: str) -> str:
    value = lookup(name)
    if not value:
        raise ConfigError(f"MESHYCHAT_BLOB_BACKEND={backend} but {name} is unset")
    return value


def build_blob_config() -> BlobConfig:
    backend = (lookup("MESHYCHAT_BLOB_BACKEND") or "").strip().lower()
    if backend not in (BACKEND_FS, BACKEND_R2):
        raise ConfigError(
            f"MESHYCHAT_BLOB_BACKEND must be 'fs' or 'r2'; it has intentionally no default "
            f"(the configuration requires an explicit backend). Got {backend!r}.")

    fs = r2 = None
    if backend == BACKEND_FS:
        fs = FsConfig(
            blob_dir=_require("MESHYCHAT_BLOB_DIR", backend),
            token_secret=_require("MESHYCHAT_BLOB_TOKEN_SECRET", backend),
            public_base_url=_require("MESHYCHAT_PUBLIC_BASE_URL", backend),
        )
    else:
        r2 = R2Config(
            account_id=_require("MESHYCHAT_R2_ACCOUNT_ID", backend),
            bucket=_require("MESHYCHAT_R2_BUCKET", backend),
            access_key_id=_require("MESHYCHAT_R2_ACCESS_KEY_ID", backend),
            secret_access_key=_require("MESHYCHAT_R2_SECRET_ACCESS_KEY", backend),
            endpoint=lookup("MESHYCHAT_R2_ENDPOINT") or None,
        )

    return BlobConfig(
        backend=backend,
        fs=fs,
        r2=r2,
        upload_window_secs=_int_env("MESHYCHAT_UPLOAD_WINDOW_SECS", DEFAULT_UPLOAD_WINDOW_SECS),
        presign_ttl_secs=_int_env("MESHYCHAT_PRESIGN_TTL_SECS", DEFAULT_PRESIGN_TTL_SECS),
        max_file_bytes=_int_env("MESHYCHAT_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES),
    )


def load_at_startup() -> BlobConfig | None:
    """The startup read (B11). None when MESHYCHAT_BLOB_BACKEND is unset or blank: the one
    case that starts without blob storage (the divergence in this module's docstring).
    Anything else is `build_blob_config()`, whose ConfigError the AppConfig turns into a
    refusal to start."""
    if not (lookup("MESHYCHAT_BLOB_BACKEND") or "").strip():
        return None
    return build_blob_config()
