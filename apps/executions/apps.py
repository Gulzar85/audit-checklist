"""Application configuration for executions."""

from __future__ import annotations

from django.apps import AppConfig


class ExecutionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.executions"
    label = "executions"
    verbose_name = "Executions"
