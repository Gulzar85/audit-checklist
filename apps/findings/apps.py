"""Application configuration for findings."""

from __future__ import annotations

from django.apps import AppConfig


class FindingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.findings"
    label = "findings"
    verbose_name = "Findings"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        post_migrate.connect(_install_guard, sender=self, dispatch_uid="findings_install_guard")


def _install_guard(sender, using="default", **kwargs) -> None:  # noqa: ANN001, ANN003
    """(Re)create the integrity trigger after every ``migrate`` (idempotent; independent of migration files)."""
    from .guards import install_database_guard

    install_database_guard(using)
