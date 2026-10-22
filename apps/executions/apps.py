"""Application configuration for executions."""

from __future__ import annotations

from django.apps import AppConfig


class ExecutionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.executions"
    label = "executions"
    verbose_name = "Executions"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from apps.assignments import hooks

        from .services.lifecycle import cancel_for_assignment

        post_migrate.connect(_install_guard, sender=self, dispatch_uid="executions_install_guard")
        hooks.register_before_cancel(cancel_for_assignment)


def _install_guard(sender, using="default", **kwargs) -> None:  # noqa: ANN001, ANN003
    """(Re)create the integrity triggers after every ``migrate`` (idempotent; independent of migration files)."""
    from .guards import install_database_guard

    install_database_guard(using)
