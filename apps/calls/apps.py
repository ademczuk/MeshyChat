from django.apps import AppConfig


class CallsConfig(AppConfig):
    """Registered so Django discovers `manage.py bootstrap_memory_peer`. Carries no models.

    Same arrangement as apps/jobs/apps.py: the schema lives in sql/ and sql_meshychat/,
    applied by apps/core/sqlmigrate.py, and an AppConfig with no models does not change
    that. The label is prefixed so it cannot collide with a third-party `calls` app.
    """

    name = "apps.calls"
    label = "meshy_calls"

    def ready(self) -> None:
        # Rule B9 (sql_meshychat/022): fix this process's code hash at start, so a worker
        # that outlives a change to the tree still reports the code it started with.
        from apps.core import code_fingerprint
        code_fingerprint.running()
