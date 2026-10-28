"""Application configuration for reporting."""

from __future__ import annotations

from django.apps import AppConfig


class ReportingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reporting"
    label = "reporting"
    verbose_name = "Reporting"

    def ready(self) -> None:
        from . import jobs  # noqa: F401  (registers the Phase 18 task types)
        from .reports import definitions  # noqa: F401  (registers the reports)
