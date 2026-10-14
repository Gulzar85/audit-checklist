"""Organization-scoping conventions.

The platform is multi-tenant: almost every domain object belongs to exactly one
:class:`~apps.organizations.models.Organization`.  Rather than repeat
``filter(organization=...)`` (and risk forgetting it, which would leak data
across tenants), organization-owned models use the queryset/manager helpers in
this module.

These are deliberately small.  They solve one real, repeated problem — scoping
reads to a tenant — and nothing more.
"""

from __future__ import annotations

from typing import Any

from django.db import models


class OrganizationScopedQuerySet(models.QuerySet):
    """Query helpers for models with an ``organization`` foreign key."""

    def for_organization(self, organization: Any) -> "OrganizationScopedQuerySet":
        """Restrict results to a single organization (accepts an instance or pk)."""
        return self.filter(organization_id=getattr(organization, "pk", organization))

    def for_organizations(self, organizations: Any) -> "OrganizationScopedQuerySet":
        """Restrict results to a collection of organizations (instances or pks)."""
        ids = [getattr(org, "pk", org) for org in organizations]
        return self.filter(organization_id__in=ids)

    def active(self) -> "OrganizationScopedQuerySet":
        """Restrict results to active rows.

        Requires the model to expose an ``is_active`` boolean.
        """
        return self.filter(is_active=True)


class OrganizationScopedManager(models.Manager.from_queryset(OrganizationScopedQuerySet)):
    """Default manager for organization-scoped models."""
