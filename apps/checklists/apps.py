"""Application configuration for checklists."""

from __future__ import annotations

from django.apps import AppConfig


class ChecklistsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.checklists"
    label = "checklists"
    verbose_name = "Checklists"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        post_migrate.connect(_install_guards, sender=self, dispatch_uid="checklists_install_guards")


def _install_guards(sender, using="default", **kwargs) -> None:  # noqa: ANN001, ANN003
    """(Re)create the immutability triggers after every ``migrate`` - idempotent, independent of migration files (ADR 0049)."""
    from .guards import install_database_guards

    install_database_guards(using)
