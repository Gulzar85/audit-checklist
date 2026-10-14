"""Application configuration for permissions."""

from __future__ import annotations

from django.apps import AppConfig


class PermissionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.permissions"
    label = "permissions"
    verbose_name = "Roles & Permissions"
