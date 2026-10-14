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
