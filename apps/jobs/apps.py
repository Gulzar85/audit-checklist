"""Application configuration for scheduling."""

from __future__ import annotations

from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.jobs"
    label = "jobs"
    verbose_name = "Scheduling"

    def ready(self) -> None:
        from . import handlers  # noqa: F401  (registers the task types)
