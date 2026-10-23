"""Application configuration for corrective actions."""

from __future__ import annotations

from django.apps import AppConfig


class CorrectiveActionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.corrective_actions"
    label = "corrective_actions"
    verbose_name = "Corrective Actions"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        post_migrate.connect(_install_guard, sender=self, dispatch_uid="corrective_actions_install_guard")
        from . import integration  # noqa: F401  (registers the evidence target and the finding hooks)


def _install_guard(sender, using="default", **kwargs) -> None:  # noqa: ANN001, ANN003
    """(Re)create the integrity triggers after every ``migrate`` (idempotent; independent of migration files)."""
    from .guards import install_database_guard

    install_database_guard(using)
