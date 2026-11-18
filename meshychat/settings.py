"""Django settings for the meshychat ASGI service.

The defaults follow MeshyChat's wire and storage behavior rather than Django's:

  * no `django.contrib.admin`, `auth`, `sessions` or `messages`. The schema is the 104
    SQL migrations applied verbatim by apps/core/sqlmigrate.py; there are no Django
    models and there is no Django auth. Installing those apps would invite
    `manage.py migrate` to create `auth_*`/`django_*` tables in the service schema.
  * `APPEND_SLASH = False`. Django's default would answer `/v1/health/` with a 301 to
    `/v1/health`; the route returns 404. A redirect would be a wire difference.
  * DEBUG defaults to OFF. Django's `startproject` default is True and would leak
    tracebacks from a route that answers with a JSON error envelope.

Environment (each variable has one name, read as written):

  MESHY_DSN / DATABASE_URL   Postgres DSN                 (default: the dev stack)
  MESHY_ALLOWED_HOSTS        comma-separated Host allow-list
  MESHY_DEBUG                1/true/yes to turn DEBUG on
  MESHY_SECRET_KEY           Django signing key
  MESHY_LOG_LEVEL            root log level
  MESHY_IDENTITY_RESOLVER    dotted path the auth app registers (optional)
  MESHY_REDIS_URL            Redis URL for the health probe
  MESHYCHAT_BLOB_BACKEND     "fs" | "r2"; no default. Read once at
                             startup (apps/files/apps.py): set and unusable, the server
                             refuses to start; unset, it starts without blob storage
  MESHYCHAT_BLOB_DIR         blob root when backend=fs
  MESHYCHAT_GIT_SHA/MESHYCHAT_GIT_DIRTY/MESHY_VERSION   build identity reported by /v1/health
  MESHYCHAT_JWT_ISSUER, MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH, MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH,
  MESHYCHAT_JWT_PUBLIC_KEY_HISTORY_PEM_PATHS, MESHYCHAT_DPOP_ENFORCE
                             the JWT/DPoP configuration, parsed by
                             apps/authapi/config.py:jwt_material_from_env - the SAME call
                             the minter makes (docs/DIVERGENCES.md 015)
  MESHY_WS_REVOCATION_SWEEP_SECS
                             how often each serving process re-reads the standing of
                             the peers holding its WebSockets (default 10; divergence 030)
  MESHY_MEMORY_PEER_ID       the peer whose memory.* skills execute in-process
                             (default 29d6a9f7-..., the id node clients address;
                             docs/DIVERGENCES.md 017)
  MESHY_FLEET_ROSTER_PATH    the fleet roster JSON, read once at startup; unset, no
                             roster; unreadable or malformed, the server refuses to
                             start (docs/DIVERGENCES.md 034)
  MESHYCHAT_ENVELOPE_NONCE_ENFORCE
                             the envelope replay ledger (gate 8). ON when unset; only
                             false/0/no/off turns it off, and any other value refuses to
                             load (docs/DIVERGENCES.md 036). Exporting the setting's name,
                             MESHY_ENVELOPE_NONCE_ENFORCE, does NOT set it.
  MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE
                              publisher manifest attestations required at publish and at a
                              version pin. Exactly `true` or `false` are accepted; unset
                              is false, and ANY other value (1, yes, TRUE, empty) refuses to
                              load (apps/core/attestation_flag.py,
                             B09). A PRESENT signature is verified at a pin either way.
  MESHY_EGRESS_ALLOW_LOOPBACK
                             TEST AND DEVELOPMENT ONLY: lets a peer-controlled URL reach
                             loopback through the SSRF guard (apps/core/egress.py). With
                              DEBUG off the server refuses to start while it is on
                              (docs/DIVERGENCES.md 041 S8).
  MESHY_INVOKE_WEBHOOK_KEY_PATH
                             the file holding THIS host's invocation doorbell key, a
                             dedicated Ed25519 key that run_call_worker creates there at its
                             first start and the JWKS publishes beside the token key. Unset,
                             doorbells are off (docs/DIVERGENCES.md 041 S9). Never the token
                             key's file; never copied between hosts; rotate by deleting it.
  MESHY_CAPABILITIES         object-capability tokens (B07, docs/DIVERGENCES.md 040). OFF when
                             unset; MESHYCHAT_DPOP_ENFORCE's spellings, and any other value refuses
                             to load. ON: the root key below is loaded once at startup, and
                             the server AND every manage.py command refuse to start without
                             a usable one: unset MESHY_CAPABILITIES to run commands while the
                             key is repaired
  MESHY_CAPABILITY_ROOT_KEY_PATH
                             a FILE holding the hex Ed25519 root private key. The key itself
                              is never read from the environment, and MESHYCHAT_BISCUIT_ROOT_SK
                              (reading it would make `issue_grant` refuse)
                              is NOT read.
                             Make one: python -m apps.memoryskills.capability_keys create PATH
                             --kid N, with PATH outside the checkout (the command refuses one
                             inside it: the key never lives in the repository)
  MESHY_CAPABILITY_ROOT_KID  the active root key's integer id (0 to 2^32-1)
  MESHY_CAPABILITY_ROOT_PUBKEYS
                             the verify set, kid=hex;kid=hex; must hold the active kid with
                             the key file's public half. Read once: a change needs a restart
  MESHY_CAPABILITY_DEV_EPHEMERAL
                             true for a fresh root key per process when no key file is named;
                             read ONLY when MESHY_DEBUG is on, never in a deploy
  MESHY_METRICS_TOKEN        the bearer token GET /v1/metrics requires. Unset, the route answers 503
                             E_METRICS_DISABLED and emits nothing: metrics are off until a
                             token is named (apps/core/metrics.py, E01)
  MESHY_MESH_ACCEPT_UNVERSIONED
                             TRANSITIONAL, OFF when unset; MESHY_CAPABILITIES' spellings, and
                             any other value refuses to load. ON: memory.write accepts a
                              shared/mesh-chat msg/ object with NO schema_version unchecked, for
                              nodes still transitioning; a value
                             that declares schema_version is still validated, and the key and
                             authorship rules still apply. Each acceptance is counted
                             (meshy_mesh_unversioned_accepted_total). Turn it off once the nodes
                             send with tools.meshy_node (docs/DIVERGENCES.md 011, 2026-09-27)

A fresh install needs these before a node can reach the mesh (docs/DIVERGENCES.md 024;
tests/test_settings_documents_the_fresh_install.py holds this list). README.md "Fresh
install" is the runbook that sets them, in order (divergence 025):

  MESHYCHAT_STATE_SECRET     the CSRF/OIDC-state HMAC key.
                             Unset, /v1/auth/me returns csrf_token: null and no pairing
                             code can be minted outside DEBUG.
                             Exporting MESHY_STATE_SECRET does NOT set it: that name is
                             only the Django SETTING the same value can be given as.
  MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID
                             the one peer allowed to namespace.claim (MUST-1). Set it to
                             the signer peer `manage.py bootstrap_owner --plane dev`
                             printed; unset, every claim is refused.
  MESHY_MODEL_PROVIDERS      JSON array, the model registry (apps/modelhost/registry.py).
                             On HQD the providers are HQB's (192.168.50.10:7875 embed,
                             :7876 rerank, :7872 chat); HQD runs no model servers.
  MESHY_EMBED_FORBIDDEN_ROWS true to embed rows marked embed_policy='forbidden', which is
                             every row written today; closed by default (divergence 019)
  MESHY_CTX_INDEX_BUDGET_BYTES  the transcript index's resident shard budget per server
                             process, whole bytes. Unset, 2 GiB; a value that is not a whole
                             number or is under 256 MiB keeps 2 GiB, with a warning. HQD sets
                             1073741824 (divergence 108)
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent

from apps.core.env import lookup as _lookup  # noqa: E402


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- core
SECRET_KEY = os.environ.get(
    "MESHY_SECRET_KEY",
    # Dev-only. Nothing on the request path signs with it today (there are no Django
    # sessions and no CSRF middleware); it exists because Django refuses to start
    # without one. A deploy sets MESHY_SECRET_KEY.
    "django-insecure-meshychat-dev-only-key-replace-in-any-deploy",
)

DEBUG = _env_bool("MESHY_DEBUG", False)

ALLOWED_HOSTS = _env_list("MESHY_ALLOWED_HOSTS", ["127.0.0.1", "localhost"])

APPEND_SLASH = False

ROOT_URLCONF = "meshychat.urls"

ASGI_APPLICATION = "meshychat.asgi.application"
WSGI_APPLICATION = "meshychat.wsgi.application"

INSTALLED_APPS = [
    "channels",
    "apps.core",
    # Registered so Django discovers `manage.py run_jobs`. It declares no models and adds
    # no Django migrations: the jobs schema lives in sql_meshychat/ like every other
    # table here (see apps/core/sqlmigrate.py on why this project applies raw SQL).
    "apps.jobs",
    # Registered so Django discovers `manage.py bootstrap_owner` (the first owner, from a
    # shell - the project's design log (not part of this release) 016). Declares no models, adds no Django migrations.
    "apps.authapi",
    # Registered so Django discovers `manage.py bootstrap_memory_peer`, which creates the
    # memory-authority peer a fresh database needs before step 9 can route to it
    # (the project's design log (not part of this release) 017). No models, no Django migrations, as for apps.jobs.
    "apps.calls",
    # Registered so its ready() loads the fleet roster named by MESHY_FLEET_ROSTER_PATH,
    # once, at startup, and refuses to start on one it cannot use (the project's design log (not part of this release)
    # 034). No models, no Django migrations.
    "apps.memoryskills",
    # Registered so its ready() reads the blob configuration and builds the store once, at
    # startup, and refuses to start on a MESHYCHAT_BLOB_BACKEND it cannot use (status item B11;
    # apps/files/config.py). No models, no Django migrations.
    "apps.files",
]

# ORDER IS OUTERMOST FIRST. Django wraps each entry around the ones below it, so the
# security headers must come before the error envelope: an error RESPONSE is still a
# response a browser will sniff, and a header set only on the happy path is not a control.
MIDDLEWARE = [
    # FIRST, so it is the outermost layer and counts the status the client is actually sent,
    # the error envelope's and the security refusals' included: meshy_http_requests_total on
    # GET /v1/metrics (apps/core/metrics.py).
    "apps.core.metrics.MetricsMiddleware",
    # `nosniff` and `Referrer-Policy` on every response. apps/files serves opaque bytes as
    # application/octet-stream and argues a browser must not sniff them as text/html -
    # that argument is only true with this header, and there was none.
    "apps.core.websec.SecurityHeadersMiddleware",
    # `Meshychat-Capability` is REFUSED with E_CAPABILITY_UNAVAILABLE while this server has no
    # capability-token verifier (B07 slice 1, the project's design log (not part of this release) 040). Ignored, the header
    # let a client believe a token narrowed a request its grants alone decided. Before the
    # database middleware, so a refused request reaches no identity, no view and no
    # database; inside the security headers, so the refusal carries them too.
    "apps.core.capability_header.CapabilityHeaderMiddleware",
    # Any exception escaping a view leaves as {"error":"E_INTERNAL"}, never as Django's
    # HTML page. Four reviewers found the same hole on four surfaces: each app's `_route`
    # catches ApiError and nothing else, so a CheckViolation, a UniqueViolation, a KeyError
    # or a TypeError reached the default handler - and with MESHY_DEBUG=1 that page carries
    # the DSN. A middleware rather than a decorator, because apps/skillsreg/views.py:436 is
    # a route that bypasses its own wrapper entirely.
    "apps.core.jsonerrors.JsonErrorEnvelopeMiddleware",
    # A JSON body nested deeper than 256 levels is 400 E_VALIDATION before any view (#278):
    # the recursive walkers after json.loads ran out of stack near 940 levels, a 500. Inside
    # the error envelope, before the database middleware, so a refused body reaches neither.
    "apps.core.jsondepth.JsonDepthMiddleware",
    # Django's own CommonMiddleware (APPEND_SLASH, ETag), CsrfViewMiddleware and
    # SessionMiddleware stay OUT: each changes the wire in ways this server's wire does not.
    # Note what that means for CSRF - there is no framework check, so apps/core/websec.py
    # `csrf_ok` is the only one, and every cookie-authenticated write must call it.
    "apps.core.middleware.DatabaseConnectionMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": False,
        "OPTIONS": {"context_processors": []},
    },
]

DEFAULT_CHARSET = "utf-8"
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------- db
#: The single source of truth for the database. Every request-path connection is
#: opened by apps/core/dbconn.py with psycopg directly (autocommit + SET ROLE
#: meshychat_app); DATABASES below exists so `manage.py dbshell`, `inspectdb` and any
#: Django-side tooling point at the same place, NOT because the ORM serves requests.
MESHY_DSN = os.environ.get("MESHY_DSN") or os.environ.get("DATABASE_URL") or (
    "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django"
)


def _dsn_to_django(dsn: str) -> dict:
    parts = urlsplit(dsn)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port) if parts.port else "",
        "CONN_MAX_AGE": 0,
        "OPTIONS": {},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
    }


DATABASES = {"default": _dsn_to_django(MESHY_DSN)}

# --------------------------------------------------------------------------- channels
#: In-memory layer: correct for a single process and for tests. It is per-process, so a
#: multi-worker deployment needs a real cross-process layer.
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}

# --------------------------------------------------------------------------- auth seam
#: Dotted path to `(request) -> Identity | None`. Left unset until the auth app lands;
#: apps/core/middleware.py then treats every request as anonymous.
MESHY_IDENTITY_RESOLVER = os.environ.get("MESHY_IDENTITY_RESOLVER") or None

# --------------------------------------------------------------------------- JWT + DPoP
# ONE CONFIGURATION DRIVES MINT AND VERIFY (the project's design log (not part of this release) 015). Every verifier -
# apps/peers/auth.py (all peer routes), apps/dashboard/auth.py, apps/grants/owner_auth.py
# and the WS gateway (apps/wsgw/consumer.py:config_from_env) - reads the three settings
# below, and the minter (apps/authapi/config.py:AuthConfig.from_env) builds its own from
# the same `jwt_material_from_env` call over the same environment. Before this block
# existed nothing defined the first two, so a deploy minted tokens at /v1/auth/sign-in
# that every peer route then refused with 500 E_INTERNAL; only tests, which inject keys
# with override_settings, ever saw a peer route answer.
#
# A boot refusal would break every test module that imports these settings with no key
# configured, so an unset value is None and each consumer fails closed on use (verifiers 500,
# minter 503, WS upgrade refused). A configured-but-broken
# value (unreadable path, non-Ed25519 key, private key that is not the public key's
# partner) still raises right here, at load.
from apps.authapi.config import jwt_material_from_env  # noqa: E402

_JWT = jwt_material_from_env(os.environ)

#: `iss` pinned by every verifier and stamped by the minter. No default.
MESHY_JWT_ISSUER = _JWT.issuer
#: The CURRENT verify key only; history keys are for invocation frames
#: (apps/authapi/tokens.py:verify).
MESHY_JWT_PUBLIC_KEY_PEM = _JWT.public_key_pem
#: DPoP proof-of-possession on peer WRITES and on the WS upgrade. Defaults ON.
#: MESHYCHAT_DPOP_ENFORCE=false is the emergency kill-switch.
MESHY_DPOP_ENFORCE = _JWT.dpop_enforce

# ---------------------------------------------------------------------- memory authority
#: The peer id whose `memory.*` skills this server EXECUTES IN-PROCESS
#: (the project's design log (not part of this release) 017). Rather than being routed to a separate memory service, a signed
#: call addressed to THIS peer id is dispatched to apps/memoryskills, and a call addressed to any other peer is failed with a typed
#: error, because this server has no delivery path to anyone else (apps/calls/route.py).
#:
#: The default is the id the existing node clients already address:
#: tools/memory/fleet_roster.json labels 29d6a9f7-... `meshy-mem-daemon`, and
#: mem_client._resolve_daemon_peer falls back to that roster entry. A different id here
#: would 404 every memory call a node sends. Validated at load: a malformed id must not
#: reach a request as a comparison that never
#: matches.
MESHY_MEMORY_PEER_ID = str(uuid.UUID(
    (os.environ.get("MESHY_MEMORY_PEER_ID") or "").strip()
    or "29d6a9f7-ed27-5153-a9f2-f6feb3175e77"))

# ------------------------------------------------------------------------- fleet roster
#: The fleet roster JSON (tools/memory/fleet_roster.json shape: `"peers"` maps a peer id to its
#: label), read ONCE at startup by apps/memoryskills/apps.py into the label
#: map divergence 028's authorship check consults (the project's design log (not part of this release) 034). Unset: no
#: roster, and a caller's labels are its peers.name and peer-<id8>. Set to a file that
#: cannot be read or parsed: the process refuses to start.
MESHY_FLEET_ROSTER_PATH = (os.environ.get("MESHY_FLEET_ROSTER_PATH") or "").strip() or None

# ----------------------------------------------------------------------- envelope replay
#: Gate 8, the envelope nonce ledger (apps/calls/nonce_ledger.py). ON unless the
#: environment spells the opt-out (the project's design log (not part of this release) 036).
#: Parsed here once, from MESHYCHAT_ENVELOPE_NONCE_ENFORCE, and read per request by
#: `nonce_ledger.enforce_from_settings`, the only reader. A value that is not a boolean
#: refuses to load, as a malformed MESHYCHAT_DPOP_ENFORCE already does.
from apps.calls.nonce_ledger import enforce_from_env as _nonce_enforce_from_env  # noqa: E402

MESHY_ENVELOPE_NONCE_ENFORCE = _nonce_enforce_from_env(os.environ)

# ------------------------------------------------------------ manifest attestation flag
#: MESHYCHAT_MANIFEST_ATTESTATION_ENFORCE, parsed ONCE here: exactly `true`/`false`, unset false,
#: and any other value refuses to load. `attestation_flag.enforce_from_settings` is the one
#: reader, for the grant pin gate and the skill publish gate alike.
from apps.core.attestation_flag import (  # noqa: E402
    enforce_from_env as _attestation_from_env,
)

MESHY_MANIFEST_ATTESTATION_ENFORCE = _attestation_from_env(os.environ)

# ------------------------------------------------------------------------ egress hatch
#: The SSRF guard's test hatch (apps/core/egress.py, the project's design log (not part of this release) 041 S8): on, a
#: peer-controlled URL may reach LOOPBACK, so a test can aim a webhook at a local recorder.
#: Parsed once, here, after DEBUG: a process with DEBUG off is a production process, and one
#: that sets the hatch refuses to load (the deploy check). `egress.loopback_allowed` is the
#: one reader, and it also requires DEBUG.
from apps.core.egress import allow_loopback_from_env as _egress_hatch_from_env  # noqa: E402

MESHY_EGRESS_ALLOW_LOOPBACK = _egress_hatch_from_env(os.environ, debug=DEBUG)

# ------------------------------------------------------------------ invocation doorbells
#: The file holding this host's invocation doorbell key (apps/calls/webhook_key.py,
#: the project's design log (not part of this release) 041 S9): a dedicated Ed25519 key, never the token key, created by
#: run_call_worker's first start with an exclusive create and published by the JWKS under its
#: own kid. Unset (the default), no doorbell is verified or rung: setting it enables S9 on a
#: host. `webhook_key.configured_path` is the one reader.
MESHY_INVOKE_WEBHOOK_KEY_PATH = ((os.environ.get("MESHY_INVOKE_WEBHOOK_KEY_PATH") or "").strip()
                                 or None)

# --------------------------------------------------------------------- capability tokens
#: Object-capability tokens (B07, the project's design log (not part of this release) 040), OFF unless the environment says on.
#: Parsed here once, as strictly as MESHYCHAT_DPOP_ENFORCE: a malformed value refuses to load. The
#: three key settings are kept as written; apps/memoryskills/apps.py ready() parses them ONLY
#: when tokens are on (apps/memoryskills/capability_keys.py), and refuses to start on a key it
#: cannot use. With tokens off nothing parses them and biscuit_auth is never imported, so a
#: CPython 3.14 deploy is unaffected. The key is a FILE: no setting here holds key material.
#: The parser is apps.core's, not apps.memoryskills': importing that package here would load
#: its handlers while settings is half loaded (040 slice 4, review F6).
from apps.core.capability_flag import (  # noqa: E402
    enabled_from_env as _capabilities_from_env,
)

MESHY_CAPABILITIES = _capabilities_from_env(os.environ)
MESHY_CAPABILITY_ROOT_KEY_PATH = (
    (os.environ.get("MESHY_CAPABILITY_ROOT_KEY_PATH") or "").strip() or None)
MESHY_CAPABILITY_ROOT_KID = (os.environ.get("MESHY_CAPABILITY_ROOT_KID") or "").strip() or None
MESHY_CAPABILITY_ROOT_PUBKEYS = (
    (os.environ.get("MESHY_CAPABILITY_ROOT_PUBKEYS") or "").strip() or None)

# ------------------------------------------------------------ optional route surfaces
#: Which OPTIONAL surfaces meshychat/urls.py mounts (the project's design log (not part of this release) 054): none unless the
#: environment names them. Parsed once, here; an unknown name refuses to load.
from apps.core.surfaces import from_env as _surfaces_from_env  # noqa: E402

MESHY_SURFACES = _surfaces_from_env(os.environ)

# ------------------------------------------------------------ legacy mesh senders
#: TRANSITIONAL (the project's design log (not part of this release) 011, dated paragraph 2026-09-27), OFF unless the environment
#: says on. On, memory.write accepts a shared/mesh-chat message with no schema_version and
#: permits senders using tools/memory/mesh_send.py, which sends no `schema_version`, to keep
#: operating during the cutover.
#: Parsed here once, as strictly as MESHY_CAPABILITIES (a malformed value refuses to load), by a
#: parser whose only project import is apps.core.capability_flag, which imports none;
#: `mesh_unversioned.accept_from_settings` is the one reader.
from apps.core.mesh_unversioned import (  # noqa: E402
    accept_from_env as _mesh_unversioned_from_env,
)

MESHY_MESH_ACCEPT_UNVERSIONED = _mesh_unversioned_from_env(os.environ)

# --------------------------------------------------------------------------- metrics
#: The bearer token GET /v1/metrics requires (apps/core/metrics.py, E01). Unset, the route is
#: OFF, 503 E_METRICS_DISABLED: gates default closed.
MESHY_METRICS_TOKEN = ((_lookup("MESHY_METRICS_TOKEN") or "").strip() or None)

# --------------------------------------------------------------------------- build id
#: Reported verbatim by /v1/health; runtime environment values are set by the image build.
MESHY_VERSION = os.environ.get("MESHY_VERSION", "0.1.0")  # Build image version reported by health.

# --------------------------------------------------------------------------- error reports
#: Django's reporter masks HTTP_AUTHORIZATION and showed Meshychat-Capability as sent; this one hides
#: both (divergence 040, slice 6; apps/core/error_reports.py).
DEFAULT_EXCEPTION_REPORTER_FILTER = "apps.core.error_reports.CapabilityReporterFilter"

# --------------------------------------------------------------------------- logging
LOG_LEVEL = _lookup("MESHY_LOG_LEVEL", "info").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        # stdout, not stderr: a container's log collector reads stdout, and Django's
        # default console handler writes to stderr.
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "formatter": "plain",
        },
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        "meshychat": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
    },
}
