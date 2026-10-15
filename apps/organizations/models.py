"""Multi-organization foundation.

The platform is multi-tenant from the ground up: every domain object that
belongs to a customer is scoped to an :class:`Organization`.  The organization is
the tenant root; locations, checklists, executions and so on all hang off it.

Lifecycle is modelled with a single authoritative ``status`` field.  There is no
separate ``is_active`` column: a boolean plus a status would be two sources of
truth, and ``is_active`` is exposed as a derived property instead (ADR 0011).
Suspended/inactive organizations keep all of their data - nothing is deleted -
they simply cannot perform normal operations.

Identity: ``id`` (UUID) is what URLs and foreign keys use, so renaming an
organization never breaks a reference.  ``code`` is the stable, human-friendly
business key (used by imports and integrations) and is immutable once the
organization exists; ``slug`` is a unique, readable handle.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel
from apps.core.validators import (
    validate_country_code,
    validate_currency_code,
    validate_hex_color,
    validate_language_code,
    validate_timezone,
)

from .validators import validate_branding_image

_CODE_SANITIZER = re.compile(r"[^A-Za-z0-9]+")


def normalize_organization_code(value: str) -> str:
    """Return a canonical, upper-case organization code (``acme-co`` → ``ACME_CO``)."""
    return _CODE_SANITIZER.sub("_", (value or "").strip()).strip("_").upper()


def _branding_path(instance: "Organization", filename: str, folder: str) -> str:
    """Store uploads under a server-generated name.

    The client-supplied filename is never used: only its (already validated)
    extension survives, so path tricks, odd unicode and double extensions in the
    original name cannot reach the storage backend.
    """
    extension = Path(filename).suffix.lower()
    return f"organizations/{instance.pk}/{folder}/{uuid.uuid4().hex}{extension}"


def organization_logo_path(instance: "Organization", filename: str) -> str:
    return _branding_path(instance, filename, "logo")


def organization_favicon_path(instance: "Organization", filename: str) -> str:
    return _branding_path(instance, filename, "favicon")


class OrganizationQuerySet(models.QuerySet):
    """Query helpers for organizations."""

    def active(self) -> "OrganizationQuerySet":
        """Only organizations that are in the ``active`` state."""
        return self.filter(status=Organization.Status.ACTIVE)


class Organization(BaseModel):
    """The tenant root object."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        INACTIVE = "inactive", _("Inactive")
        SUSPENDED = "suspended", _("Suspended")

    class DateFormat(models.TextChoices):
        ISO = "Y-m-d", "YYYY-MM-DD"
        DAY_FIRST = "d/m/Y", "DD/MM/YYYY"
        MONTH_FIRST = "m/d/Y", "MM/DD/YYYY"
        DOTTED = "d.m.Y", "DD.MM.YYYY"
        TEXTUAL = "d M Y", "DD Mon YYYY"

    # --- Identity ---------------------------------------------------------
    name = models.CharField(max_length=255)
    #: Stable, human-friendly unique business key. Immutable after creation.
    code = models.CharField(
        max_length=32,
        unique=True,
        blank=True,
        help_text=_("Stable unique code. Derived from the slug when left empty; cannot change later."),
    )
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    legal_name = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)

    # --- Lifecycle --------------------------------------------------------
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    # --- Branding & contact ----------------------------------------------
    logo = models.ImageField(
        upload_to=organization_logo_path,
        blank=True,
        null=True,
        validators=[validate_branding_image],
    )
    favicon = models.ImageField(
        upload_to=organization_favicon_path,
        blank=True,
        null=True,
        validators=[validate_branding_image],
    )
    primary_color = models.CharField(
        max_length=7,
        blank=True,
        validators=[validate_hex_color],
        help_text=_("Brand colour as #RRGGBB."),
    )
    website = models.URLField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)

    # --- Address ----------------------------------------------------------
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(
        max_length=2,
        blank=True,
        validators=[validate_country_code],
        help_text=_("ISO 3166-1 alpha-2 country code, e.g. 'GB'."),
    )

    # --- Regional preferences (overridable per tenant; blank = platform default) ---
    timezone = models.CharField(
        max_length=64,
        blank=True,
        validators=[validate_timezone],
        help_text=_("IANA time zone, e.g. 'Europe/London'."),
    )
    currency = models.CharField(
        max_length=3,
        blank=True,
        validators=[validate_currency_code],
        help_text=_("ISO 4217, e.g. 'USD'."),
    )
    language = models.CharField(
        max_length=10,
        blank=True,
        validators=[validate_language_code],
        help_text=_("e.g. 'en'."),
    )
    date_format = models.CharField(max_length=10, blank=True, choices=DateFormat.choices)

    history = HistoricalRecords()
    objects = OrganizationQuerySet.as_manager()

    class Meta:
        ordering = ("name",)
        verbose_name = _("organization")
        verbose_name_plural = _("organizations")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(code__regex=r"^[A-Z0-9][A-Z0-9_]*$"),
                name="organization_code_format",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["active", "inactive", "suspended"]),
                name="organization_status_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(slug=""),
                name="organization_slug_not_empty",
            ),
            models.CheckConstraint(
                condition=models.Q(country="") | models.Q(country__regex=r"^[A-Z]{2}$"),
                name="organization_country_format",
            ),
        ]
        indexes = [
            # Serves "organizations in state X, by name" (admin + list filters).
            models.Index(fields=["status", "name"], name="organizatio_status_7242eb_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    # --- Normalisation / validation --------------------------------------
    def normalize(self) -> None:
        """Bring identity and regional fields to canonical form.

        Runs *before* field validators (see :meth:`clean_fields`) so that, for
        example, ``gb`` is accepted as ``GB`` rather than rejected.
        """
        self.name = (self.name or "").strip()
        self.slug = (self.slug or "").strip().lower() or slugify(self.name)
        self.code = normalize_organization_code(self.code or self.slug or self.name)
        self.country = (self.country or "").strip().upper()
        self.currency = (self.currency or "").strip().upper()
        self.language = (self.language or "").strip().lower()
        self.primary_color = (self.primary_color or "").strip().upper()
        self.timezone = (self.timezone or "").strip()

    def clean_fields(self, exclude=None) -> None:  # noqa: ANN001
        self.normalize()
        super().clean_fields(exclude=exclude)

    def save(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.normalize()
        super().save(*args, **kwargs)

    # --- Derived state ----------------------------------------------------
    @property
    def is_active(self) -> bool:
        """``True`` only when the organization may perform normal operations."""
        return self.status == self.Status.ACTIVE

    @property
    def is_suspended(self) -> bool:
        return self.status == self.Status.SUSPENDED

    # --- Effective regional settings (fall back to platform defaults) -----
    @property
    def effective_timezone(self) -> str:
        return self.timezone or settings.TIME_ZONE

    @property
    def effective_language(self) -> str:
        return self.language or settings.LANGUAGE_CODE

    @property
    def effective_currency(self) -> str:
        return self.currency or "USD"

    @property
    def effective_date_format(self) -> str:
        return self.date_format or self.DateFormat.ISO

    @property
    def effective_date_format_label(self) -> str:
        """Human-readable form of the effective date format (``YYYY-MM-DD``)."""
        return dict(self.DateFormat.choices).get(self.effective_date_format, self.effective_date_format)

    def get_absolute_url(self) -> str:
        return reverse("organizations:detail", kwargs={"pk": self.pk})


class OrganizationMembership(BaseModel):
    """Links a user to an organization.

    Membership is deliberately narrow (who belongs where).  Roles and
    permissions live in ``apps.permissions`` and are attached separately.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    is_default = models.BooleanField(
        default=False,
        help_text=_("Preferred organization for the user."),
    )
    joined_at = models.DateTimeField(auto_now_add=True, editable=False)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("organization membership")
        verbose_name_plural = _("organization memberships")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="unique_user_organization_membership",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization}"
