"""Authorization policy for the organization domain.

A policy answers *"may this user do this to this object?"* and nothing else.  It
is isolated from business operations so the HTML views, the API and imports all
share one rule set (ADR 0012).  Callers never branch on role names; they call
these functions.

Rule set (Phase 1):

* **View** - exactly the organizations returned by
  :func:`~apps.organizations.selectors.organizations_visible_to` (platform staff:
  all; members: their active organizations).
* **Create** - a platform operation: platform staff holding
  ``organizations.add_organization``.
* **Edit** - someone who can view the organization *and* holds
  ``organizations.change_organization``.
* **Change status** (activate / deactivate / suspend) - platform staff only.
  If members could deactivate their own organization they would instantly lose
  the ability to see it, and so to undo it.

Editing goes through :func:`apps.permissions.authorization.has_permission` (RBAC, Phase 3):
the member must hold ``organization.update`` in that organization.  Creating and changing the
status of organizations are *platform* operations and stay with platform staff.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user, is_platform_staff
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

from .models import OrganizationMembership
from .selectors import organizations_visible_to


def is_organization_member(user: Any, organization: Any) -> bool:
    """Return ``True`` if ``user`` is an active member of ``organization``."""
    if not is_active_user(user):
        return False
    return OrganizationMembership.objects.filter(
        user=user,
        organization_id=getattr(organization, "pk", organization),
        is_active=True,
    ).exists()


def can_view_organization(user: Any, organization: Any) -> bool:
    """Whether ``user`` may see ``organization`` at all."""
    if not is_active_user(user):
        return False
    return organizations_visible_to(user).filter(pk=getattr(organization, "pk", organization)).exists()


def can_create_organization(user: Any) -> bool:
    """Whether ``user`` may create a new organization (a platform operation)."""
    return is_platform_staff(user) and user.has_perm("organizations.add_organization")


def can_manage_organization(user: Any, organization: Any) -> bool:
    """Whether ``user`` may edit ``organization`` (details and branding)."""
    return can_view_organization(user, organization) and has_permission(
        user, P.ORGANIZATION_UPDATE, organization=organization
    )


def can_change_organization_status(user: Any, organization: Any) -> bool:  # noqa: ARG001
    """Whether ``user`` may activate, deactivate or suspend ``organization``."""
    return is_platform_staff(user) and user.has_perm("organizations.change_organization")
