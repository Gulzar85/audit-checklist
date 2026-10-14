"""Template context shared across all pages."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest


def site_context(request: HttpRequest) -> dict[str, object]:
    """Expose branding/version information to templates."""
    return {
        "APP_NAME": getattr(settings, "APP_NAME", "Checklist Platform"),
        "APP_VERSION": getattr(settings, "APP_VERSION", "0.1.0"),
        "APP_ENVIRONMENT": getattr(settings, "ENVIRONMENT", "development"),
        "debug": settings.DEBUG,
    }
