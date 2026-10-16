"""Seed (or refresh) the platform's system roles (idempotent)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.permissions import services


class Command(BaseCommand):
    help = "Create/refresh system roles and their permissions (also syncs permissions). Safe to run repeatedly."

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        result = services.seed_system_roles()
        self.stdout.write(self.style.SUCCESS(
            f"System roles: {result['created']} created, {result['refreshed']} refreshed."
        ))