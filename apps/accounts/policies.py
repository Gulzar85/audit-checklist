"""Authorization policy for the identity domain.

A policy answers *"may this user do this to this object?"* and nothing else.  It
is isolated from business operations so the web views, the API and the admin all
share one rule set (ADR 0012).  Callers never branch on role names; the future
data-driven RBAC layer will refine these bodies without changing call sites.

Rule set (Phase 2 - the *identity boundary*, not the full authorization engine):

* **View a user** - yourself, anyone platform staff may see, or anyone who
  shares one of your visible organizations.
* **Manage a user** (lifecycle, identity) - platform staff holding
  ``accounts.change_user``.  Nobody manages their own lifecycle, so an account
  can never activate itself.
* **Manage a membership** - whoever may manage the organization.
* **Switch organization** - a member of an active organization they can see.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user, is_platform_staff
from apps.organizations.models import Organization
from apps.organizations.policies import can_view_organization
from apps.organizations.policies import can_manage_organization as _can_manage_organization

from .selectors import users_visible_to


def can_view_user(actor: Any, target: Any) -> bool:
    """Whether ``actor`` may see ``target``'s identity/profile."""
    if not is_active_user(actor):
        return False
    target_pk = getattr(target, "pk", target)
    if target_pk is not None and actor.pk == target_pk:
        return True
    if target_pk is None:
        return False
    return users_visible_to(actor).filter(pk=target_pk).exists()


def can_manage_user(actor: Any, target: Any) -> bool:
    """Whether ``actor`` may change ``target``'s lifecycle or identity link."""
    if not is_active_user(actor):
        return False
    # An account may never manage itself: no self-activation / self-escalation.
    if actor.pk == getattr(target, "pk", target):
        return False
    return is_platform_staff(actor) and actor.has_perm("accounts.change_user")


def can_manage_identity(actor: Any) -> bool:
    """Whether ``actor`` may perform platform identity operations (link, provision)."""
    return is_platform_staff(actor) and actor.has_perm("accounts.change_user")


def can_manage_membership(actor: Any, organization: Any) -> bool:
    """Whether ``actor`` may add/remove members of ``organization``."""
    return _can_manage_organization(actor, organization)


def can_switch_organization(actor: Any, organization: Any) -> bool:
    """Whether ``actor`` may make ``organization`` their active organization."""
    if not is_active_user(actor):
        return False
    if getattr(organization, "status", None) != Organization.Status.ACTIVE:
        return False
    return can_view_organization(actor, organization)
