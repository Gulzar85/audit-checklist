"""Application configuration for checklists."""

from __future__ import annotations

from django.apps import AppConfig


class ChecklistsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.checklists"
    label = "checklists"
    verbose_name = "Checklists"
