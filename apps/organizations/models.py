"""Multi-organization foundation.

The platform is multi-tenant from the ground up: every domain object that
belongs to a customer is scoped to an :class:`Organization`.  The organization is
the tenant root; locations, checklists, executions and so on all hang off it.

Lifecycle is modelled with a single authoritative ``status`` field.  There is no
separate ``is_active`` column: a boolean plus a status would be two sources of
truth, and ``is_active`` is exposed as a derived property instead (see ADR
0011).  Suspended/inactive organizations keep all of their data — nothing is
deleted — they simply cannot perform normal operations.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel

from .validators import validate_branding_image

_CODE_SANITIZER = re.compile(r"[^A-Za-z0-9]+")


def normalize_organization_code(value: str) -> str:
    """Return a canonical, upper-case organization code (``acme-co`` → ``ACME_CO``)."""
    return _CODE_SANITIZER.sub("_", (value or "").strip()).strip("_").upper()


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

    # --- Identity ---------------------------------------------------------
    name = models.CharField(max_length=255)
    #: Stable, human-friendly unique code used for references/imports.
    code = models.CharField(
        max_length=32,
        unique=True,
        blank=True,
        help_text=_("Stable unique code (auto-derived from the slug when left empty)."),
    )
    slug = models.SlugField(max_length=255, unique=True)
    legal_name = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)

    # --- Lifecycle --------------------------------------------------------
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    # --- Branding & contact ----------------------------------------------
    logo = models.ImageField(
        upload_to="organizations/logos/",
        blank=True,
        null=True,
        validators=[validate_branding_image],
    )
    favicon = models.ImageField(
        upload_to="organizations/favicons/",
        blank=True,
        null=True,
        validators=[validate_branding_image],
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
        help_text=_("ISO 3166-1 alpha-2 country code, e.g. 'GB'."),
    )

    # --- Regional preferences (overridable per tenant) --------------------
    timezone = models.CharField(max_length=64, blank=True)
    currency = models.CharField(max_length=3, blank=True, help_text=_("ISO 4217, e.g. 'USD'."))
    language = models.CharField(max_length=10, blank=True, help_text=_("e.g. 'en'."))
    date_format = models.CharField(max_length=32, blank=True, help_text=_("e.g. 'Y-m-d'."))

    #: Extension point for future non-queryable settings. Typed fields are
    #: preferred; keep this small.
    configuration = models.JSONField(default=dict, blank=True)

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
        ]
        indexes = [
            models.Index(fields=["status", "name"]),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        # Keep the code canonical; derive it from the slug when omitted.
        self.code = normalize_organization_code(self.code or self.slug or self.name)
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        # Derive a code when omitted so the uniqueness/format constraints hold
        # even before ``save()`` runs (``full_clean`` validates constraints).
        self.code = normalize_organization_code(self.code or self.slug or self.name)

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
        return self.date_format or "Y-m-d"

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
