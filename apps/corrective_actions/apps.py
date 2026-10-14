"""Application configuration for corrective actions."""

from __future__ import annotations

from django.apps import AppConfig


class CorrectiveActionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.corrective_actions"
    label = "corrective_actions"
    verbose_name = "Corrective Actions"
