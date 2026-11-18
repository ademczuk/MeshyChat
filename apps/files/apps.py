import logging

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

log = logging.getLogger("meshychat.files")


class FilesConfig(AppConfig):
    """Reads the blob configuration and builds the store ONCE, at startup (status item
    B11). Carries no models and adds no Django migrations.

    ready() runs once per process inside django.setup(): daphne and uvicorn reach it
    through meshychat.asgi, and every manage.py command through its own setup. A
    MESHYCHAT_BLOB_BACKEND that is set and unusable raises ImproperlyConfigured there, so the
    process does not start. An unset value starts without blob storage and says so once;
    apps/files/config.py documents that exception. The label is prefixed, as apps/jobs/apps.py's
    is.
    """

    name = "apps.files"
    label = "meshy_files"

    def ready(self) -> None:
        from . import blob, config
        try:
            cfg = config.load_at_startup()
            blob.activate(cfg)
        except (config.ConfigError, blob.BlobError) as exc:
            raise ImproperlyConfigured(
                f"{exc}. The server does not start with a blob configuration it cannot "
                "use: starting without a usable blob configuration would answer 503 on "
                "every /v1/files route (status item B11). Fix the MESHYCHAT_BLOB_* / MESHYCHAT_R2_* "
                "variables, or unset MESHYCHAT_BLOB_BACKEND to run without blob storage.") \
                from exc
        if cfg is None:
            log.warning("MESHYCHAT_BLOB_BACKEND is unset: no blob storage, and every "
                        "/v1/files route answers 503 E_BLOB_BACKEND_UNAVAILABLE "
                        "(apps/files/config.py)")
        else:
            log.info("blob backend: %s", cfg.backend)
