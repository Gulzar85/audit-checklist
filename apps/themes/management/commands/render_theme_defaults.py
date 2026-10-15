"""Regenerate ``tailwind/theme-defaults.css`` from the token registry.

The file is the built-in fallback under every theme stylesheet (and what Tailwind reads to
generate ``bg-primary`` & co.).  Run this after changing ``apps/themes/tokens.py``, then
``npm run css:build``.  ``--check`` fails (exit 1) when the committed file is stale - CI and
``tests/test_theme_defaults.py`` use it.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.themes.css import compile_defaults_for_tailwind

TARGET = settings.BASE_DIR / "tailwind" / "theme-defaults.css"


class Command(BaseCommand):
    help = "Write tailwind/theme-defaults.css from the design-token registry."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument("--check", action="store_true", help="Fail if the file is out of date.")

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        expected = compile_defaults_for_tailwind()
        if options["check"]:
            current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
            if current != expected:
                raise CommandError("tailwind/theme-defaults.css is stale. Run render_theme_defaults.")
            self.stdout.write("theme-defaults.css is up to date.")
            return
        TARGET.write_text(expected, encoding="utf-8", newline="\n")
        self.stdout.write(self.style.SUCCESS(f"Wrote {TARGET}"))
