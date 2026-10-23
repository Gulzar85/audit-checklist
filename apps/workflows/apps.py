"""Application configuration for workflows."""

from __future__ import annotations

from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workflows"
    label = "workflows"
    verbose_name = "Workflows"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        post_migrate.connect(_install_guard, sender=self, dispatch_uid="workflows_install_guard")
        from . import integration  # noqa: F401  (registers the target types, business actions, domain guards and auto-start receivers)


def _install_guard(sender, using="default", **kwargs) -> None:  # noqa: ANN001, ANN003
    """(Re)create the integrity triggers after every ``migrate`` (idempotent; independent of migration files)."""
    from .guards import install_database_guard

    install_database_guard(using)
