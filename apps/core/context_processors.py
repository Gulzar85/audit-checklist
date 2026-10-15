"""Template context shared across all pages."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest

from .navigation import build_navigation

#: Cookie remembering the sidebar mode so the *server* renders it on first paint (no flash).
SIDEBAR_COOKIE = "cp_sidebar"
SIDEBAR_STATES = ("expanded", "collapsed")


def site_context(request: HttpRequest) -> dict[str, object]:
    """Expose branding/version information to templates."""
    return {
        "APP_NAME": getattr(settings, "APP_NAME", "Checklist Platform"),
        "APP_VERSION": getattr(settings, "APP_VERSION", "0.1.0"),
        "APP_ENVIRONMENT": getattr(settings, "ENVIRONMENT", "development"),
        "debug": settings.DEBUG,
    }


def shell_context(request: HttpRequest) -> dict[str, object]:
    """What the application shell needs.  Cheap by design: no query unless the template asks.

    * ``nav_sections`` - the configured navigation, filtered for the user (presentation only).
    * ``sidebar_state`` - ``expanded`` / ``collapsed`` from the cookie.
    * ``switchable_organizations`` - evaluated lazily (one query, capped) only when the switcher
      renders; the switcher shows the first eight and links to the full selection page.
    """
    state = request.COOKIES.get(SIDEBAR_COOKIE, "expanded")
    cache: dict[str, list] = {}

    def switchable_organizations() -> list:
        if "orgs" not in cache:
            from apps.organizations.selectors import organizations_visible_to

            user = getattr(request, "user", None)
            cache["orgs"] = list(organizations_visible_to(user).filter(status="active").order_by("name")[:9])
        return cache["orgs"]

    return {
        "nav_sections": build_navigation(request),
        "sidebar_state": state if state in SIDEBAR_STATES else "expanded",
        "switchable_organizations": switchable_organizations,
    }
