"""Vocabulary shared by the theme engine (kept import-light: no models, no Django apps)."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class ThemeScope(models.TextChoices):
    """Who owns a theme.  The *user* level is a preference (``Appearance``), not a theme."""

    SYSTEM = "system", _("System")
    ORGANIZATION = "organization", _("Organization")


class ThemeStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PUBLISHED = "published", _("Published")
    ACTIVE = "active", _("Active")
    ARCHIVED = "archived", _("Archived")


class Appearance(models.TextChoices):
    """The three user-facing colour modes."""

    LIGHT = "light", _("Light")
    DARK = "dark", _("Dark")
    SYSTEM = "system", _("System")


#: Modes a preview can render (``system`` is not a palette: it picks light or dark).
PREVIEW_MODES = (("light", "Light"), ("dark", "Dark"))

#: Cookie mirroring the appearance so the *server* can render the right ``data-theme``
#: on the first byte (no flash) - also for anonymous visitors.  It holds an enum value only.
APPEARANCE_COOKIE = "cp_appearance"
APPEARANCE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

#: Session key for the temporary, per-session theme preview.
PREVIEW_SESSION_KEY = "theme_preview_id"

# Percentages used by the derived status colours.  The *same* numbers drive the CSS
# (``color-mix``) and the Python contrast checks, so what we measure is what is shown.
STATUS_SOFT_PERCENT = 14  # status colour mixed into the surface -> tinted background
STATUS_BORDER_PERCENT = 35  # ... -> tinted border
STATUS_TEXT_PERCENT = 60  # status colour mixed into the foreground -> readable text

#: WCAG 2.2 success criteria the editor measures against.
WCAG_TEXT_AA = 4.5  # 1.4.3 Contrast (Minimum), normal text
WCAG_NON_TEXT_AA = 3.0  # 1.4.11 Non-text Contrast (focus indicators, UI components)

ACTIVITY_VERBS = {
    "created": "theme.created",
    "updated": "theme.updated",
    "published": "theme.published",
    "activated": "theme.activated",
    "archived": "theme.archived",
    "cloned": "theme.cloned",
    "rolled_back": "theme.rolled_back",
    "versioned": "theme.versioned",
    "imported": "theme.imported",
    "appearance": "appearance.preference_changed",
}

#: Format version of exported theme documents.
EXPORT_FORMAT = "checklist-platform.theme/1"
