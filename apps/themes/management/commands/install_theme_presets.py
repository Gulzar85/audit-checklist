"""Install the built-in presets as *published* system themes (idempotent).

Organizations then clone a system theme into their own scope (or create one from a preset
directly).  Nothing is activated: choosing the application-wide look is a deliberate act.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.themes import services
from apps.themes.constants import ThemeStatus
from apps.themes.models import Theme
from apps.themes.presets import PRESETS


class Command(BaseCommand):
    help = "Create a published system theme for every built-in preset that is missing."

    @transaction.atomic
    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        created = 0
        for preset in PRESETS.values():
            code = preset.key.upper()
            if Theme.objects.filter(organization__isnull=True, code=code).exists():
                continue
            theme = services.create_theme(
                name=preset.name,
                code=code,
                description=preset.description,
                preset=preset.key,
            )
            services.publish_theme(theme)
            created += 1
            self.stdout.write(f"  + {preset.name} ({ThemeStatus.PUBLISHED})")
        self.stdout.write(self.style.SUCCESS(f"{created} preset theme(s) installed."))
