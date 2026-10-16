"""Mirror the permission registry into the database (idempotent)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.permissions import services


class Command(BaseCommand):
    help = "Create/update permission rows from apps/permissions/registry.py. Safe to run repeatedly."

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        result = services.sync_registry()
        self.stdout.write(self.style.SUCCESS(
            f"Permissions: {result['created']} created, {result['updated']} updated, {result['deactivated']} deactivated."
        ))