"""Application configuration for notifications."""

from __future__ import annotations

from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _install_guards(sender: Any, using: str = "default", **kwargs: Any) -> None:
    from .guards import install_database_guards

    install_database_guards(using)


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    label = "notifications"
    verbose_name = "Notifications"

    def ready(self) -> None:
        post_migrate.connect(_install_guards, sender=self, dispatch_uid="notifications.install_database_guards")
        from . import integration  # noqa: F401  (connects the domain events - after commit - to the notification outbox)
