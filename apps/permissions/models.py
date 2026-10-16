"""RBAC foundation.

Roles are *data*, not code.  An organization can define its own roles; a role
with ``organization`` unset is a platform-wide template.  A ``RoleScope``
describes the boundary a role applies within (platform / organization /
location), and ``UserRole`` grants a role to a user within a scope.

Django's built-in ``Permission`` and ``Group`` continue to model fine-grained
model permissions; django-guardian provides object-level checks where required.
The authorization entrypoints live in :mod:`apps.permissions.authorization`.
"""

from __future__ import annotations

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import BaseModel


class Role(BaseModel):
    """A named collection of capabilities, optionally organization-scoped."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="roles",
        help_text="Leave empty for a platform-wide role template.",
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    description = models.CharField(max_length=255, blank=True)
    is_system = models.BooleanField(
        default=False,
        help_text="System roles are managed by the platform and cannot be deleted.",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ("name",)
        verbose_name = "role"
        verbose_name_plural = "roles"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(is_system=True) | models.Q(organization__isnull=True),
                name="system_role_has_no_organization",
            ),
            models.UniqueConstraint(
                fields=["organization", "slug"],
                condition=models.Q(organization__isnull=False),
                name="unique_role_slug_per_organization",
            ),
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(organization__isnull=True),
                name="unique_global_role_slug",
            ),
        ]

    def __str__(self) -> str:
        scope = self.organization.slug if self.organization_id else "platform"
        return f"{self.name} ({scope})"


class RoleScope(BaseModel):
    """The boundary within which a role assignment is valid."""

    class ScopeType(models.TextChoices):
        PLATFORM = "platform", "Platform"
        ORGANIZATION = "organization", "Organization"
        LOCATION = "location", "Location"

    scope_type = models.CharField(
        max_length=20,
        choices=ScopeType.choices,
        default=ScopeType.ORGANIZATION,
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="role_scopes",
    )
    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="role_scopes",
    )
    name = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "role scope"
        verbose_name_plural = "role scopes"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(scope_type="location", location__isnull=False)
                | ~models.Q(scope_type="location"),
                name="role_scope_location_requires_location",
            ),
        ]
        indexes = [
            models.Index(fields=["scope_type", "organization"]),
        ]

    def __str__(self) -> str:
        target = self.location or self.organization or "platform"
        return f"{self.get_scope_type_display()}: {target}"


class UserRole(BaseModel):
    """Grants a :class:`Role` to a user within an optional :class:`RoleScope`."""

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="user_assignments",
    )
    scope = models.ForeignKey(
        RoleScope,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="user_assignments",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    granted_at = models.DateTimeField(auto_now_add=True, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "user role"
        verbose_name_plural = "user roles"
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["role", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.user} -> {self.role}"


class Permission(BaseModel):
    """A mirror row of one :mod:`apps.permissions.registry` entry (``resource.action``).

    The registry in code is authoritative; this table exists so roles can reference
    permissions by foreign key and so administrators can browse the catalogue.  Rows are
    created and updated only by ``sync_registry``/``seed_permissions``.
    """

    code = models.CharField(max_length=80, unique=True)
    resource = models.CharField(max_length=40, db_index=True)
    action = models.CharField(max_length=40)
    label = models.CharField(max_length=160)
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ("resource", "action")
        verbose_name = "permission"
        verbose_name_plural = "permissions"

    def __str__(self) -> str:
        return self.code


class RolePermission(BaseModel):
    """Grants one :class:`Permission` to one :class:`Role` (additive; there are no denies)."""

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="role_permissions")

    history = HistoricalRecords()

    class Meta:
        verbose_name = "role permission"
        verbose_name_plural = "role permissions"
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="unique_role_permission"),
        ]

    def __str__(self) -> str:
        return f"{self.role.slug}: {self.permission.code}"


class MembershipRole(BaseModel):
    """Grants a role to an organization *membership* within a scope.

    The membership is the access boundary (who belongs where); this row says *what* the member
    may do (the role) and *where* (the scope).  A role grants its permissions inside the
    scope only: the whole organization, one location, or a location and its descendants.
    """

    class ScopeType(models.TextChoices):
        ORGANIZATION = "organization", "Entire organization"
        LOCATION = "location", "A single location"
        LOCATION_DESCENDANTS = "location_descendants", "A location and everything beneath it"

    membership = models.ForeignKey(
        "organizations.OrganizationMembership",
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="membership_assignments")
    scope_type = models.CharField(max_length=24, choices=ScopeType.choices, default=ScopeType.ORGANIZATION)
    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="membership_role_assignments",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    assigned_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    assigned_at = models.DateTimeField(auto_now_add=True, editable=False)

    history = HistoricalRecords()

    class Meta:
        verbose_name = "membership role"
        verbose_name_plural = "membership roles"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(scope_type="organization", location__isnull=True)
                | (models.Q(scope_type__in=["location", "location_descendants"]) & models.Q(location__isnull=False)),
                name="membership_role_scope_matches_location",
            ),
            models.UniqueConstraint(
                fields=["membership", "role"],
                condition=models.Q(location__isnull=True),
                name="unique_membership_role_org_scope",
            ),
            models.UniqueConstraint(
                fields=["membership", "role", "scope_type", "location"],
                condition=models.Q(location__isnull=False),
                name="unique_membership_role_location_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["membership", "is_active"]),
            models.Index(fields=["role", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.membership} -> {self.role.name} ({self.scope_type})"

    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        errors: dict[str, str] = {}
        if self.scope_type == self.ScopeType.ORGANIZATION:
            if self.location_id:
                errors["location"] = "An organization-wide assignment has no location."
        elif not self.location_id:
            errors["location"] = "Choose the location this assignment is scoped to."
        elif self.location.organization_id != self.membership.organization_id:
            errors["location"] = "The location must belong to the member's organization."
        if self.role_id and self.role.organization_id not in (None, self.membership.organization_id):
            errors["role"] = "This role belongs to a different organization."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.clean()  # the integrity rules hold for every writer, not just forms
        super().save(*args, **kwargs)