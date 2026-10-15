"""Business operations for the organization domain.

These are the sanctioned way to create, change and transition an organization.
The web views, the API, the admin actions and imports all call into here, so
validation, transactions, locking and activity logging happen exactly once.

Authorization is *not* decided here: entry points ask the policy layer
(``policies.py``) first, then call a service.  Services own the business rules
that must hold no matter who is calling.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.services import apply_changes, log_activity, stamp_history

from .models import Organization, normalize_organization_code

_STATUS_VERBS = {
    Organization.Status.ACTIVE: "organization.activated",
    Organization.Status.INACTIVE: "organization.deactivated",
    Organization.Status.SUSPENDED: "organization.suspended",
}


@transaction.atomic
def create_organization(*, actor: Any = None, **data: Any) -> Organization:
    """Validate, persist and log a new organization.

    A new organization always starts ``active``; use :func:`set_organization_status`
    for lifecycle changes so they are logged as business events.
    """
    data.pop("status", None)
    organization = Organization(**data)
    organization.full_clean()
    stamp_history(organization, actor, "organization.created")
    organization.save()
    log_activity(
        verb="organization.created",
        organization=organization,
        actor=actor,
        description=f"Created organization {organization.name} ({organization.code})",
        target=organization,
        metadata={"code": organization.code},
    )
    return organization


@transaction.atomic
def update_organization(organization: Organization, *, actor: Any = None, **data: Any) -> Organization:
    """Apply changes to an organization, validating and logging the write.

    ``code`` is the stable business key and cannot change once the organization
    exists; ``status`` changes only through :func:`set_organization_status`.
    """
    if "status" in data:
        raise ValueError("Use set_organization_status() to change an organization's status.")

    current = Organization.objects.select_for_update().get(pk=organization.pk)

    new_code = data.pop("code", None)
    if new_code and normalize_organization_code(new_code) != current.code:
        raise ValidationError({"code": _("An organization's code cannot be changed once created.")})

    changes = apply_changes(current, data)
    if not changes:
        return current

    current.full_clean()
    stamp_history(current, actor, "organization.updated")
    current.save()
    log_activity(
        verb="organization.updated",
        organization=current,
        actor=actor,
        description=f"Updated organization {current.code}",
        target=current,
        metadata={"changes": changes},
    )
    return current


@transaction.atomic
def set_organization_status(
    organization: Organization,
    status: str,
    *,
    actor: Any = None,
) -> Organization:
    """Transition an organization's lifecycle state.

    Nothing is deleted when an organization becomes inactive or suspended: its
    locations, types and history are untouched.  The organization simply stops
    being visible to ordinary users and refuses writes inside it (ADR 0011).
    """
    if status not in Organization.Status.values:
        raise ValidationError({"status": _("Unknown status “%(status)s”.") % {"status": status}})

    current = Organization.objects.select_for_update().get(pk=organization.pk)
    if current.status == status:
        return current

    previous = current.status
    current.status = status
    stamp_history(current, actor, _STATUS_VERBS[status])
    current.save(update_fields=["status", "updated_at"])
    log_activity(
        verb=_STATUS_VERBS[status],
        organization=current,
        actor=actor,
        description=f"Organization {current.code} is now {status}",
        target=current,
        metadata={"from": previous, "to": status},
    )
    return current


def activate_organization(organization: Organization, *, actor: Any = None) -> Organization:
    return set_organization_status(organization, Organization.Status.ACTIVE, actor=actor)


def deactivate_organization(organization: Organization, *, actor: Any = None) -> Organization:
    return set_organization_status(organization, Organization.Status.INACTIVE, actor=actor)


def suspend_organization(organization: Organization, *, actor: Any = None) -> Organization:
    return set_organization_status(organization, Organization.Status.SUSPENDED, actor=actor)
