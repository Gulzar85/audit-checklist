"""Application configuration for workflows."""

from __future__ import annotations

from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workflows"
    label = "workflows"
    verbose_name = "Workflows"
