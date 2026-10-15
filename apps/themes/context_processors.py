"""Expose the resolved theme to templates (once per request, no queries in templates)."""

from __future__ import annotations

from django.db import DatabaseError
from django.http import HttpRequest

from .constants import Appearance
from .resolution import default_resolved, resolve_theme


def theme_context(request: HttpRequest) -> dict[str, object]:
    """``ui_theme`` is the request's :class:`~apps.themes.resolution.ResolvedTheme`.

    A failing theme lookup must never take a page down (the error pages render through
    here too): fall back to the built-in defaults instead.
    """
    try:
        resolved = resolve_theme(request)
    except DatabaseError:  # pragma: no cover - exercised by the 500 page when the DB is down
        resolved = default_resolved()
    return {"ui_theme": resolved, "appearance_choices": Appearance.choices}
