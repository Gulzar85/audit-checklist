"""Generic location model.

Locations are a *self-referencing tree* rather than a hard-coded hierarchy such
as "Region → Area → Restaurant".  A :class:`LocationType` describes what a node
is (country, region, city, restaurant, warehouse, office, department, ...), and
``parent`` builds arbitrary depths.  This keeps the platform generic across
industries: the same schema models

    Org A: Country → Region → Area → Branch → Store
    Org B: Business Unit → Division → Facility → Department

with no schema change.

Integrity rules (self-parent, cross-organization parent/type, cycles) are
enforced at the application layer via :meth:`Location.clean` and the service
layer.  A ``CheckConstraint`` also rejects self-parenting in the database; the
cross-row rules cannot be expressed as a simple database constraint and are
covered by validation plus tests.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import QuerySet
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel
from apps.core.scoping import OrganizationScopedManager, OrganizationScopedQuerySet


class LocationTypeQuerySet(OrganizationScopedQuerySet):
    """Query helpers for location types."""


class LocationType(BaseModel):
    """A configurable classification of locations.

    ``organization`` may be left empty to define a *platform/system* type that
    is available to every organization (mirroring the role model).  Otherwise a
    type belongs to exactly one organization.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="location_types",
        null=True,
        blank=True,
        help_text=_("Leave empty for a platform-defined type available to all organizations."),
    )
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=60)
    description = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0, help_text=_("Display order within a level."))
    is_active = models.BooleanField(default=True, db_index=True)

    history = HistoricalRecords()
    objects = OrganizationScopedManager()

    class Meta:
        ordering = ("organization", "sort_order", "name")
        verbose_name = _("location type")
        verbose_name_plural = _("location types")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                condition=models.Q(organization__isnull=False),
                name="unique_location_type_code_per_organization",
            ),
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(organization__isnull=True),
                name="unique_global_location_type_code",
            ),
        ]

    def __str__(self) -> str:
        scope = self.organization.slug if self.organization_id else "platform"
        return f"{self.name} ({scope})"


class LocationQuerySet(OrganizationScopedQuerySet):
    """Query helpers for the location tree."""

    def roots(self) -> "LocationQuerySet":
        return self.filter(parent__isnull=True)

    def children_of(self, parent) -> "LocationQuerySet":
        return self.filter(parent_id=getattr(parent, "pk", parent))


class Location(BaseModel):
    """A generic, nestable organization location."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="locations",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        help_text=_("Parent in the hierarchy. Empty for a root location."),
    )
    location_type = models.ForeignKey(
        LocationType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locations",
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=80, help_text=_("Unique within the organization."))
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    # --- Address ----------------------------------------------------------
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(
        max_length=2,
        blank=True,
        help_text=_("ISO 3166-1 alpha-2 country code."),
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))],
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))],
    )
    timezone = models.CharField(max_length=64, blank=True)

    #: Extension point for future non-queryable attributes.
    metadata = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()
    objects = LocationQuerySet.as_manager()

    class Meta:
        ordering = ("organization", "name")
        verbose_name = _("location")
        verbose_name_plural = _("locations")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="unique_location_code_per_organization",
            ),
            models.CheckConstraint(
                condition=~models.Q(id=models.F("parent_id")),
                name="location_cannot_be_its_own_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"]),
            models.Index(fields=["organization", "parent"]),
            models.Index(fields=["organization", "location_type"]),
            models.Index(fields=["parent"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} [{self.code}]"

    # --- Validation -------------------------------------------------------
    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}

        if self.parent_id:
            if self.pk and self.parent_id == self.pk:
                errors["parent"] = _("A location cannot be its own parent.")
            else:
                try:
                    parent = self.parent
                except Location.DoesNotExist:
                    errors["parent"] = _("The selected parent location does not exist.")
                else:
                    if parent.organization_id != self.organization_id:
                        errors["parent"] = _(
                            "The parent location must belong to the same organization."
                        )
                    elif self.pk and self._is_in_subtree(parent.pk):
                        errors["parent"] = _(
                            "A location cannot be moved beneath one of its own descendants."
                        )

        if self.location_type_id:
            location_type = self.location_type
            if (
                location_type.organization_id
                and location_type.organization_id != self.organization_id
            ):
                errors["location_type"] = _(
                    "The location type must belong to the same organization "
                    "(or be a platform-defined type)."
                )

        if errors:
            raise ValidationError(errors)

    def _is_in_subtree(self, candidate_id) -> bool:  # noqa: ANN001
        """Return ``True`` if ``candidate_id`` is this location or one of its descendants."""
        if not self.pk:
            return False
        # Imported lazily to avoid a circular import at module load time.
        from .selectors import descendant_ids

        return candidate_id in descendant_ids(self, include_self=True)

    # --- Tree helpers (delegate to the selector layer) --------------------
    def get_ancestors(self) -> list["Location"]:
        from .selectors import ancestors_of

        return list(ancestors_of(self))

    def get_descendants(self) -> QuerySet["Location"]:
        from .selectors import descendants_of

        return descendants_of(self)

    @property
    def depth(self) -> int:
        """Number of ancestors (a root location has depth 0)."""
        return len(self.get_ancestors())

    @property
    def display_path(self) -> str:
        """Human-readable path from the root to this location."""
        return " / ".join([ancestor.name for ancestor in self.get_ancestors()] + [self.name])

    def get_absolute_url(self) -> str:
        return reverse("locations:detail", kwargs={"pk": self.pk})
