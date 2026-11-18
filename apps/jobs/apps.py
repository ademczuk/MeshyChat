from django.apps import AppConfig


class JobsConfig(AppConfig):
    """Registered so Django discovers `manage.py run_jobs`. Carries no models.

    The schema lives in sql_meshychat/, applied by apps/core/sqlmigrate.py, because this
    project deliberately does not use Django's migration framework (see that module's
    docstring). An AppConfig with no models does not change that.
    """

    name = "apps.jobs"
    label = "meshy_jobs"
