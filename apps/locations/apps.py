"""Application configuration for locations."""

from __future__ import annotations

from django.apps import AppConfig


class LocationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.locations"
    label = "locations"
    verbose_name = "Locations"
