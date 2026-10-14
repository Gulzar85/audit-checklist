"""Business operations for the organization domain.

These are the only sanctioned way to create/update/transition an organization.
Views, API views, admin actions and import/export call into here so that
validation, transactions and activity logging happen exactly once.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.services import log_activity

from .models import Organization

_STATUS_VERBS = {
    Organization.Status.ACTIVE: "organization.activated",
    Organization.Status.INACTIVE: "organization.deactivated",
    Organization.Status.SUSPENDED: "organization.suspended",
}


@transaction.atomic
def create_organization(*, actor: Any = None, **data: Any) -> Organization:
    """Validate, persist and log a new organization."""
    organization = Organization(**data)
    organization.full_clean()
    organization.save()
    log_activity(
        verb="organization.created",
        organization=organization,
        actor=actor,
        description=f"Created organization {organization.name} ({organization.code})",
        target=organization,
    )
    return organization


@transaction.atomic
def update_organization(organization: Organization, *, actor: Any = None, **data: Any) -> Organization:
    """Apply changes to an organization, validating and logging the write."""
    changed_fields: list[str] = []
    for field, value in data.items():
        if getattr(organization, field) != value:
            setattr(organization, field, value)
            changed_fields.append(field)

    if not changed_fields:
        return organization

    organization.full_clean()
    organization.save()
    log_activity(
        verb="organization.updated",
        organization=organization,
        actor=actor,
        description=f"Updated organization {organization.code}",
        target=organization,
        metadata={"changed_fields": sorted(changed_fields)},
    )
    return organization


@transaction.atomic
def set_organization_status(
    organization: Organization,
    status: str,
    *,
    actor: Any = None,
) -> Organization:
    """Transition an organization's lifecycle state.

    Data is never deleted when an organization becomes inactive/suspended.
    """
    valid = {choice.value for choice in Organization.Status}
    if status not in valid:
        raise ValidationError({"status": f"Unknown status {status!r}."})
    if organization.status == status:
        return organization

    organization.status = status
    organization.save(update_fields=["status", "updated_at"])
    log_activity(
        verb=_STATUS_VERBS[status],
        organization=organization,
        actor=actor,
        description=f"Organization {organization.code} is now {status}",
        target=organization,
        metadata={"status": status},
    )
    return organization


def activate_organization(organization: Organization, *, actor: Any = None) -> Organization:
    return set_organization_status(organization, Organization.Status.ACTIVE, actor=actor)


def deactivate_organization(organization: Organization, *, actor: Any = None) -> Organization:
    return set_organization_status(organization, Organization.Status.INACTIVE, actor=actor)


def suspend_organization(organization: Organization, *, actor: Any = None) -> Organization:
    return set_organization_status(organization, Organization.Status.SUSPENDED, actor=actor)
