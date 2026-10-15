"""Theme engine data model (three small tables, no more).

* :class:`Theme` - one reusable model for every ownership level (``scope``): a *system*
  theme (``organization`` is NULL) or an *organization* theme.  Its look is ``tokens``:
  a validated override document (see :mod:`apps.themes.tokens`) - never CSS.
* :class:`ThemeActivation` - the append-only activation ledger.  It answers "what was
  active when?" and powers rollback.
* :class:`UserAppearance` - a user's light/dark/system choice.  The *user* level of the
  hierarchy is a preference, deliberately not a theme (ADR 0023).

Lifecycle (ADR 0023): ``draft -> published -> active``, ``active -> published`` when
replaced, ``draft|published -> archived``.  ``active`` is a *status* (one source of truth,
no ``is_active`` boolean that could contradict it) and the database guarantees at most one
active theme per organization (and one system-wide).

Published and active themes are **immutable**: their tokens never change, so a rollback
restores exactly what ran before.  A change is a new *version* of the same ``code``.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel, UUIDModel

from .constants import Appearance, ThemeScope, ThemeStatus
from .tokens import validate_tokens

_CODE_SANITIZER = re.compile(r"[^A-Za-z0-9]+")


def normalize_theme_code(value: str) -> str:
    """Canonical upper-case code (``Corporate Dark`` -> ``CORPORATE_DARK``)."""
    return _CODE_SANITIZER.sub("_", (value or "").strip()).strip("_").upper()[:48]


class ThemeQuerySet(models.QuerySet):
    def active(self) -> "ThemeQuerySet":
        return self.filter(status=ThemeStatus.ACTIVE)

    def system(self) -> "ThemeQuerySet":
        return self.filter(scope=ThemeScope.SYSTEM)


class Theme(BaseModel):
    """A named, versioned set of design tokens owned by the platform or an organization."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="themes",
        help_text=_("Empty for a system (application-wide) theme."),
    )
    name = models.CharField(max_length=120)
    #: Stable identifier (the display name is not one).  Immutable; shared by all versions.
    code = models.CharField(max_length=48)
    description = models.CharField(max_length=500, blank=True)
    scope = models.CharField(max_length=16, choices=ThemeScope.choices, default=ThemeScope.ORGANIZATION)
    status = models.CharField(max_length=16, choices=ThemeStatus.choices, default=ThemeStatus.DRAFT)
    version = models.PositiveIntegerField(default=1)
    default_appearance = models.CharField(
        max_length=8,
        choices=Appearance.choices,
        default=Appearance.SYSTEM,
        help_text=_("Used for users who have not chosen light, dark or system themselves."),
    )
    source_preset = models.CharField(max_length=32, blank=True, editable=False)
    tokens = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    activated_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", editable=False,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", editable=False,
    )

    history = HistoricalRecords()
    objects = ThemeQuerySet.as_manager()

    class Meta:
        ordering = ("scope", "name", "-version")
        verbose_name = _("theme")
        verbose_name_plural = _("themes")
        permissions = [
            ("publish_theme", "Can publish themes"),
            ("activate_theme", "Can activate and roll back themes"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(scope="system", organization__isnull=True)
                | models.Q(scope="organization", organization__isnull=False),
                name="theme_scope_matches_organization",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["draft", "published", "active", "archived"]),
                name="theme_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(default_appearance__in=["light", "dark", "system"]),
                name="theme_default_appearance_valid",
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="theme_version_positive"),
            models.CheckConstraint(
                condition=models.Q(code__regex=r"^[A-Z0-9][A-Z0-9_]*$"), name="theme_code_format"
            ),
            # An active/published theme has been through publishing; a draft has not.
            models.CheckConstraint(
                condition=models.Q(status__in=["published", "active"], published_at__isnull=False)
                | models.Q(status__in=["draft", "archived"]),
                name="theme_published_has_timestamp",
            ),
            models.CheckConstraint(
                condition=models.Q(status="active", activated_at__isnull=False) | ~models.Q(status="active"),
                name="theme_active_has_timestamp",
            ),
            models.UniqueConstraint(
                fields=["organization", "code", "version"],
                nulls_distinct=False,
                name="theme_unique_code_version",
            ),
            # Exactly one active theme per organization ...
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(status="active", organization__isnull=False),
                name="theme_one_active_per_organization",
            ),
            # ... and one active system theme.
            models.UniqueConstraint(
                fields=["scope"],
                condition=models.Q(status="active", scope="system"),
                name="theme_one_active_system",
            ),
        ]
        indexes = [
            # Serves the theme list of an organization filtered by status.
            models.Index(fields=["organization", "status"], name="theme_org_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"

    # --- derived state ---------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.status == ThemeStatus.ACTIVE

    @property
    def is_draft(self) -> bool:
        return self.status == ThemeStatus.DRAFT

    @property
    def is_editable(self) -> bool:
        """Only drafts change; published/active/archived themes are immutable."""
        return self.status == ThemeStatus.DRAFT

    def get_absolute_url(self) -> str:
        return reverse("themes:detail", kwargs={"pk": self.pk})

    # --- validation --------------------------------------------------------------------
    def normalize(self) -> None:
        self.name = (self.name or "").strip()
        self.code = normalize_theme_code(self.code or self.name)
        self.scope = ThemeScope.ORGANIZATION if self.organization_id else ThemeScope.SYSTEM

    def clean_fields(self, exclude=None) -> None:  # noqa: ANN001
        self.normalize()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        self.normalize()
        if not self.name:
            raise ValidationError({"name": _("A theme needs a name.")})
        if not self.code:
            raise ValidationError({"code": _("A theme needs a code.")})
        self.tokens = validate_tokens(self.tokens)  # raises ValidationError({"tokens": [...]})

    def save(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.normalize()
        super().save(*args, **kwargs)


class ThemeActivation(UUIDModel):
    """One row per period a theme was the active one for its scope (organization/system)."""

    class Via(models.TextChoices):
        ACTIVATE = "activate", _("Activated")
        ROLLBACK = "rollback", _("Rolled back")

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, null=True, blank=True,
        related_name="theme_activations",
    )
    theme = models.ForeignKey(Theme, on_delete=models.PROTECT, related_name="activations")
    via = models.CharField(max_length=10, choices=Via.choices, default=Via.ACTIVATE)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    activated_at = models.DateTimeField(auto_now_add=True, db_index=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-activated_at",)
        verbose_name = _("theme activation")
        verbose_name_plural = _("theme activations")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(deactivated_at__isnull=True)
                | models.Q(deactivated_at__gte=models.F("activated_at")),
                name="themeactivation_period_ordered",
            ),
            # At most one *open* period per scope.
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(deactivated_at__isnull=True),
                nulls_distinct=False,
                name="themeactivation_one_open_per_scope",
            ),
        ]
        indexes = [
            # "What was active for this organization, most recent first?" (rollback, audit).
            models.Index(fields=["organization", "-activated_at"], name="themeact_org_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.theme} ({self.get_via_display()})"


class UserAppearance(BaseModel):
    """A user's explicit light/dark/system choice.  No row means "inherit the theme's default"."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="appearance")
    mode = models.CharField(max_length=8, choices=Appearance.choices)

    class Meta:
        verbose_name = _("user appearance")
        verbose_name_plural = _("user appearances")
        constraints = [
            models.CheckConstraint(condition=models.Q(mode__in=["light", "dark", "system"]), name="userappearance_mode_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.user}: {self.mode}"
