"""Application configuration for evidence and attachments (Phase 11)."""

from __future__ import annotations

from django.apps import AppConfig


class EvidenceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.evidence"
    label = "evidence"
    verbose_name = "Evidence"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        post_migrate.connect(_install_guard, sender=self, dispatch_uid="evidence_install_guard")


def _install_guard(sender, using="default", **kwargs) -> None:  # noqa: ANN001, ANN003
    from .guards import install_database_guard

    install_database_guard(using)
