"""Generic location model.

Locations are a *self-referencing tree* rather than a hard-coded hierarchy such
as "Region → Area → Restaurant".  A :class:`LocationType` describes what a node
is (country, region, restaurant, warehouse, office, department, ...), and
``parent`` builds arbitrary depths.  The same schema models

    Org A: Country → Region → Area → Branch → Location
    Org B: Business Unit → Division → Facility → Department

with no schema change (ADR 0010).

Integrity rules are enforced in three layers:

1. :meth:`Location.clean` - friendly, field-level errors for forms, the admin,
   imports and the service layer;
2. the service layer - transactions, locking and activity logging;
3. PostgreSQL itself - ``CHECK`` constraints plus triggers (migration
   ``0004_integrity_triggers``) so a direct ``UPDATE``, a bulk write or a race
   can never leave a cross-organization link or a cycle behind (ADR 0013).
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Count, OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel
from apps.core.scoping import OrganizationScopedQuerySet
from apps.core.validators import validate_country_code, validate_timezone

#: Codes are short, upper-case business keys: ``KHI-001``, ``REGION_NORTH``.
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_.-]*$"
_CODE_RE = re.compile(CODE_PATTERN)


def normalize_code(value: str) -> str:
    """Canonical form of a location / location-type code (trim, spaces→``-``, upper)."""
    return re.sub(r"\s+", "-", (value or "").strip()).upper()


def validate_code(value: str) -> None:
    if not _CODE_RE.match(value or ""):
        raise ValidationError(
            _("Codes may contain letters, digits, “-”, “_” and “.”, and must start with a letter or digit."),
            code="invalid_code",
        )


class FieldLevelConstraintErrors:
    """Report coded constraint violations against a field, not ``__all__``.

    Django reports a conditional ``UniqueConstraint`` as a non-field error.  Forms
    and API clients are better served by an error on the offending field, so the
    models map each constraint's ``violation_error_code`` to the field to flag.
    """

    constraint_error_fields: dict[str, str] = {}

    def validate_constraints(self, exclude=None) -> None:  # noqa: ANN001
        try:
            super().validate_constraints(exclude=exclude)  # type: ignore[misc]
        except ValidationError as exc:
            if not hasattr(exc, "error_dict"):
                raise
            remapped: dict[str, list] = {}
            for field, errors in exc.error_dict.items():
                for error in errors:
                    target = self.constraint_error_fields.get(getattr(error, "code", None), field)
                    remapped.setdefault(target, []).append(error)
            raise ValidationError(remapped) from exc


# ---------------------------------------------------------------------------
# Location types
# ---------------------------------------------------------------------------
class LocationTypeQuerySet(OrganizationScopedQuerySet):
    """Query helpers for location types."""

    def available_to(self, organization) -> "LocationTypeQuerySet":  # noqa: ANN001
        """Types an organization may use: its own plus platform-defined ones."""
        return self.filter(
            models.Q(organization_id=getattr(organization, "pk", organization))
            | models.Q(organization__isnull=True)
        )


class LocationType(FieldLevelConstraintErrors, BaseModel):
    """A configurable classification of locations (what kind of node this is).

    ``organization`` is nullable by design: an empty organization is a
    *platform-defined* type that every organization may use (mirroring how
    roles work).  Otherwise the type belongs to exactly one organization.  Only
    platform staff may manage platform-defined types (see ADR 0016).
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="location_types",
        null=True,
        blank=True,
        help_text=_("Leave empty for a platform-defined type available to all organizations."),
    )
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=60, validators=[validate_code])
    description = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0, help_text=_("Display order within a level."))
    is_active = models.BooleanField(default=True)

    history = HistoricalRecords()
    objects = LocationTypeQuerySet.as_manager()

    constraint_error_fields = {
        "duplicate_location_type_code": "code",
        "duplicate_platform_location_type_code": "code",
    }

    class Meta:
        ordering = ("sort_order", "name")
        verbose_name = _("location type")
        verbose_name_plural = _("location types")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                condition=models.Q(organization__isnull=False),
                name="unique_location_type_code_per_organization",
                violation_error_code="duplicate_location_type_code",
                violation_error_message=_("A location type with this code already exists."),
            ),
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(organization__isnull=True),
                name="unique_global_location_type_code",
                violation_error_code="duplicate_platform_location_type_code",
                violation_error_message=_("A platform location type with this code already exists."),
            ),
            models.CheckConstraint(
                condition=models.Q(code__regex=CODE_PATTERN),
                name="location_type_code_format",
            ),
        ]

    def __str__(self) -> str:
        scope = self.organization.code if self.organization_id else "platform"
        return f"{self.name} ({scope})"

    @property
    def is_platform_type(self) -> bool:
        return self.organization_id is None

    def get_absolute_url(self) -> str:
        # Location types have no detail page; the edit form is their home.
        return reverse("locations:type_update", kwargs={"pk": self.pk})

    def clean_fields(self, exclude=None) -> None:  # noqa: ANN001
        self.name = (self.name or "").strip()
        self.code = normalize_code(self.code)
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        # A type that locations of *other* organizations already use cannot be
        # re-homed to a single organization.  (Enforced in PostgreSQL too.)
        if self.pk and not self._state.adding and self.organization_id:
            in_use_elsewhere = (
                Location.objects.filter(location_type_id=self.pk)
                .exclude(organization_id=self.organization_id)
                .exists()
            )
            if in_use_elsewhere:
                raise ValidationError(
                    {"organization": _("Locations of other organizations already use this type.")}
                )


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
class LocationQuerySet(OrganizationScopedQuerySet):
    """Query helpers for the location tree."""

    def roots(self) -> "LocationQuerySet":
        return self.filter(parent__isnull=True)

    def children_of(self, parent) -> "LocationQuerySet":  # noqa: ANN001
        return self.filter(parent_id=getattr(parent, "pk", parent))

    def with_children_count(self) -> "LocationQuerySet":
        """Annotate ``children_count`` with a correlated subquery (no GROUP BY, no N+1)."""
        children = (
            Location.objects.filter(parent=OuterRef("pk"))
            .order_by()
            .values("parent")
            .annotate(total=Count("pk"))
            .values("total")
        )
        return self.annotate(children_count=Coalesce(Subquery(children), 0))


class Location(FieldLevelConstraintErrors, BaseModel):
    """A generic, nestable organization location."""

    constraint_error_fields = {"duplicate_location_code": "code"}

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
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
        on_delete=models.PROTECT,
        related_name="locations",
    )
    name = models.CharField(max_length=255)
    code = models.CharField(
        max_length=80,
        validators=[validate_code],
        help_text=_("Unique within the organization."),
    )
    description = models.TextField(blank=True)
    #: The single lifecycle flag. Locations are deactivated, never deleted (ADR 0014).
    is_active = models.BooleanField(default=True)

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
    timezone = models.CharField(
        max_length=64,
        blank=True,
        validators=[validate_timezone],
        help_text=_("IANA time zone. Blank inherits the organization's."),
    )

    #: Free-form, non-queryable attributes (e.g. opening hours, external ids).
    metadata = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()
    objects = LocationQuerySet.as_manager()

    class Meta:
        ordering = ("name", "code")
        verbose_name = _("location")
        verbose_name_plural = _("locations")
        constraints = [
            # Also the index serving "find a location by code within an organization".
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="unique_location_code_per_organization",
                violation_error_code="duplicate_location_code",
                violation_error_message=_("A location with this code already exists in this organization."),
            ),
            models.CheckConstraint(
                condition=~models.Q(id=models.F("parent_id")),
                name="location_cannot_be_its_own_parent",
            ),
            models.CheckConstraint(
                condition=models.Q(code__regex=CODE_PATTERN),
                name="location_code_format",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__isnull=True, longitude__isnull=True)
                | models.Q(latitude__isnull=False, longitude__isnull=False),
                name="location_coordinates_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(country="") | models.Q(country__regex=r"^[A-Z]{2}$"),
                name="location_country_format",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"], name="loc_org_active_idx"),
            # Roots of an organization / children within an organization.
            models.Index(fields=["organization", "parent"], name="loc_org_parent_idx"),
            models.Index(fields=["organization", "location_type"], name="loc_org_type_idx"),
            # Name-ordered listings and prefix lookups within an organization.
            models.Index(fields=["organization", "name"], name="loc_org_name_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} [{self.code}]"

    # --- Normalisation / validation --------------------------------------
    def clean_fields(self, exclude=None) -> None:  # noqa: ANN001
        self.name = (self.name or "").strip()
        self.code = normalize_code(self.code)
        self.country = (self.country or "").strip().upper()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        errors: dict[str, ValidationError | str] = {}

        if self.pk and not self._state.adding:
            stored_org = (
                Location.objects.filter(pk=self.pk).values_list("organization_id", flat=True).first()
            )
            if stored_org is not None and stored_org != self.organization_id:
                errors["organization"] = _("A location cannot be moved to another organization.")

        parent = None
        if self.parent_id:
            try:
                parent = Location.objects.get(pk=self.parent_id)
            except Location.DoesNotExist:
                errors["parent"] = _("The selected parent location does not exist.")
            else:
                if self.pk and parent.pk == self.pk:
                    errors["parent"] = _("A location cannot be its own parent.")
                elif parent.organization_id != self.organization_id:
                    errors["parent"] = _("The parent location must belong to the same organization.")
                elif self.pk and not self._state.adding and self._is_in_subtree(parent.pk):
                    errors["parent"] = _("A location cannot be moved beneath one of its own descendants.")
                elif self.is_active and not parent.is_active:
                    errors["parent"] = _("An active location cannot sit beneath an inactive parent.")

        if self.location_type_id:
            location_type = LocationType.objects.filter(pk=self.location_type_id).first()
            if location_type is None:
                errors["location_type"] = _("The selected location type does not exist.")
            elif (
                location_type.organization_id
                and location_type.organization_id != self.organization_id
            ):
                errors["location_type"] = _(
                    "The location type must belong to the same organization "
                    "(or be a platform-defined type)."
                )

        if (
            not self.is_active
            and self.pk
            and not self._state.adding
            and Location.objects.filter(parent_id=self.pk, is_active=True).exists()
        ):
            errors["is_active"] = _(
                "This location still has active child locations. Deactivate them first, "
                "or deactivate this location together with its descendants."
            )

        if errors:
            raise ValidationError(errors)

    def _is_in_subtree(self, candidate_id) -> bool:  # noqa: ANN001
        """Return ``True`` if ``candidate_id`` is this location or one of its descendants."""
        # Imported lazily to avoid a circular import at module load time.
        from .selectors import get_descendant_ids

        return candidate_id in get_descendant_ids([self.pk], include_roots=True)

    # --- Tree helpers (delegate to the selector layer) --------------------
    # These are conveniences for a *single* instance (one query each).  Do not
    # call them per row in a loop; use the tree selectors for bulk reads.
    def get_ancestors(self) -> list["Location"]:
        from .selectors import get_ancestors

        return get_ancestors(self)

    def get_descendants(self) -> QuerySet["Location"]:
        from .selectors import get_descendants

        return get_descendants(self)

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


class MembershipLocationScope(BaseModel):
    """A location root a membership is *intended* to cover (Phase 2 foundation, data only).

    Records ``User -> Organization -> allowed location (subtree)`` so the later scope phase
    has somewhere to attach.  **Nothing enforces it yet**: no selector, policy or API reads
    it to restrict access, and an organization member with no rows is not restricted (ADR 0036).
    The location must belong to the membership's organization (service, ``clean`` and a
    PostgreSQL trigger all enforce it).
    """

    membership = models.ForeignKey(
        "organizations.OrganizationMembership",
        on_delete=models.PROTECT,
        related_name="location_scopes",
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, related_name="membership_scopes"
    )
    include_descendants = models.BooleanField(
        default=True, help_text=_("Cover the whole subtree below this location.")
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("membership location scope")
        verbose_name_plural = _("membership location scopes")
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "location"], name="unique_membership_location_scope"
            ),
        ]
        indexes = [
            # "Which memberships are scoped to this location?" (reverse lookup, later phases).
            models.Index(fields=["location"], name="locscope_location_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.membership} -> {self.location}"

    def clean(self) -> None:
        super().clean()
        if self.membership_id and self.location_id:
            if self.membership.organization_id != self.location.organization_id:
                raise ValidationError(
                    {"location": _("The location must belong to the membership's organization.")}
                )
