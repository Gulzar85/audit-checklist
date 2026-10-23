"""Application configuration for the evaluation engine."""

from __future__ import annotations

from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _install_guards(sender: Any, using: str = "default", **kwargs: Any) -> None:
    from .guards import install_database_guards

    install_database_guards(using)


class EvaluationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.evaluation"
    label = "evaluation"
    verbose_name = "Evaluation"

    def ready(self) -> None:
        post_migrate.connect(_install_guards, sender=self, dispatch_uid="evaluation.install_database_guards")
        from . import integration  # noqa: F401  (registers the version-lifecycle and execution hooks)
