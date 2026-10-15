"""Theme resolution: the one place that decides which look a request gets.

Precedence (lowest to highest; ADR 0024, ``docs/themes/theme-resolution.md``)::

    1. System defaults        built-in tokens (always present)
    2. Application theme      the active system theme, if any
    3. Organization theme     the active theme of the user's organization
    4. Temporary preview      a theme the user is trying out (session-scoped, never saved)

Token layers *merge* (a later layer overrides only the tokens it sets).  The colour **mode**
is resolved separately and does not come from the layers' colours:

    user's explicit choice (light / dark / system)  ->  theme default appearance  ->  system

so "user chose Dark" beats an organization whose theme prefers Light, and "user chose System"
follows the browser/OS.  Anonymous visitors have no organization context: they get the system
theme and their cookie (or the system theme's default appearance).

Views, templates, the API, the stylesheet endpoint and the PWA manifest all call
:func:`resolve_theme`; nothing else decides.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.urls import reverse

from apps.core.policies import is_active_user
from apps.organizations.models import Organization

from . import css as css_compiler
from . import tokens as tk
from .constants import APPEARANCE_COOKIE, PREVIEW_SESSION_KEY, Appearance, ThemeScope
from .models import Theme, UserAppearance
from .policies import can_preview_theme
from .selectors import get_active_themes, themes_visible_to, user_organization

_MEMO = "_resolved_theme"


@lru_cache(maxsize=1)
def registry_fingerprint() -> str:
    """Changes whenever the token registry (and so the generated CSS) changes."""
    return css_compiler.css_hash(css_compiler.compile_css(tk.default_tokens()))


@dataclass(frozen=True)
class ResolvedTheme:
    mode: str  # light | dark | system - becomes <html data-theme>
    tokens: dict[str, dict[str, Any]]  # complete, validated
    layers: tuple[Theme, ...]  # themes contributing, lowest first
    source: str  # default | system | organization | preview
    organization: Organization | None
    is_preview: bool
    version: str  # content-addressed; the cache key of the stylesheet

    @property
    def theme(self) -> Theme | None:
        return self.layers[-1] if self.layers else None

    @property
    def css_url(self) -> str:
        return f"{reverse('themes:tokens')}?v={self.version}"

    @property
    def color_scheme(self) -> str:
        """Value of ``<meta name="color-scheme">``."""
        return {Appearance.LIGHT: "light", Appearance.DARK: "dark"}.get(self.mode, "light dark")

    def pwa(self, mode: str | None = None) -> dict[str, str]:
        """Manifest colours for ``mode`` (the manifest cannot follow the OS, so system -> light)."""
        chosen = mode or ("dark" if self.mode == Appearance.DARK else "light")
        return tk.pwa_colors(self.tokens, chosen)

    @property
    def theme_color_light(self) -> str:
        return tk.pwa_colors(self.tokens, "light")["theme_color"]

    @property
    def theme_color_dark(self) -> str:
        return tk.pwa_colors(self.tokens, "dark")["theme_color"]

    @property
    def theme_color(self) -> str:
        """Single ``theme-color`` when the mode is fixed; ``""`` when it follows the OS."""
        if self.mode == Appearance.SYSTEM:
            return ""
        return self.theme_color_dark if self.mode == Appearance.DARK else self.theme_color_light


def _version(layers: tuple[Theme, ...]) -> str:
    digest = hashlib.sha256(registry_fingerprint().encode())
    for theme in layers:
        digest.update(f"|{theme.pk}:{theme.updated_at.isoformat()}".encode())
    return digest.hexdigest()[:16]


def effective_tokens_for(theme: Theme) -> dict[str, dict[str, Any]]:
    """The complete token set ``theme`` would produce *on its own* (used to vet it)."""
    system_theme = None
    if theme.scope == ThemeScope.ORGANIZATION:
        system_theme = get_active_themes(None)[0]
    layers = [system_theme.tokens if system_theme and system_theme.pk != theme.pk else None, theme.tokens]
    return tk.merge_tokens(*layers)


def _preview_theme(request: HttpRequest, user: Any) -> Theme | None:
    session = getattr(request, "session", None)
    if session is None or not is_active_user(user):
        return None
    raw = session.get(PREVIEW_SESSION_KEY)
    if not raw:
        return None
    try:
        theme = themes_visible_to(user).filter(pk=raw).first()
    except (ValueError, TypeError, ValidationError):  # a malformed id in the session: ignore, never fail the page
        theme = None
    if theme is None or not can_preview_theme(user, theme):
        session.pop(PREVIEW_SESSION_KEY, None)
        return None
    return theme


def _mode(request: HttpRequest, user: Any, theme: Theme | None) -> str:
    if is_active_user(user):
        stored = UserAppearance.objects.filter(user=user).values_list("mode", flat=True).first()
        if stored:
            return stored
    else:
        cookie = request.COOKIES.get(APPEARANCE_COOKIE, "")
        if cookie in Appearance.values:
            return cookie
    return theme.default_appearance if theme else Appearance.SYSTEM


def stylesheet_for(resolved: ResolvedTheme) -> str:
    """The runtime stylesheet of a resolved theme, memoised in Django's cache.

    The key is *content-addressed* (``resolved.version`` hashes the contributing themes' ids and
    ``updated_at``), so publishing, activating or rolling back yields a new key and a stale
    stylesheet can never be served - there is nothing to invalidate.  Any Django cache backend
    works (the default local-memory cache needs no extra infrastructure).
    """
    key = f"themes:css:{resolved.version}"
    css = cache.get(key)
    if css is None:
        css = css_compiler.compile_css(resolved.tokens)
        cache.set(key, css, 60 * 60)
    return css


def default_resolved() -> ResolvedTheme:
    """The built-in look (no database involved) - the safe fallback."""
    return ResolvedTheme(
        mode=Appearance.SYSTEM,
        tokens=tk.default_tokens(),
        layers=(),
        source="default",
        organization=None,
        is_preview=False,
        version=_version(()),
    )


def resolve_theme(request: HttpRequest) -> ResolvedTheme:
    """Resolve (once per request) the theme and colour mode for ``request``."""
    cached = getattr(request, _MEMO, None)
    if cached is not None:
        return cached

    user = getattr(request, "user", None)
    organization = user_organization(user) if is_active_user(user) else None
    system_theme, organization_theme = get_active_themes(organization)

    preview = _preview_theme(request, user)
    if preview is not None:
        layers = tuple(
            t for t in (system_theme if preview.scope == ThemeScope.ORGANIZATION else None, preview) if t
        )
        source = "preview"
    else:
        layers = tuple(t for t in (system_theme, organization_theme) if t)
        source = "organization" if organization_theme else "system" if system_theme else "default"

    resolved = ResolvedTheme(
        mode=_mode(request, user, layers[-1] if layers else None),
        tokens=tk.merge_tokens(*(t.tokens for t in layers)),
        layers=layers,
        source=source,
        organization=organization,
        is_preview=preview is not None,
        version=_version(layers),
    )
    setattr(request, _MEMO, resolved)
    return resolved
