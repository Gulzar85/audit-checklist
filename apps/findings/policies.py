"""Authorization for findings (ADR 0063): organization membership, role permission and *location scope*.

Every decision calls the central ``apps.permissions.authorization`` service - no role name is tested here.  Finding permissions are
location-scoped: a grant at a location covers the findings *at* that location and below.  The responsible user may always see
(and work on, if they hold the permission) a finding assigned to them.  Web views, API, admin and services share these functions.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

from .constants import EDITABLE_STATUSES, TRANSITIONS, Transition


def _at(user: Any, code: str, location: Any) -> bool:
    return is_active_user(user) and location is not None and has_permission(user, code, location=location)


def can_view(user: Any, finding: Any) -> bool:
    if _at(user, P.FINDING_VIEW, finding.location):
        return True
    return bool(is_active_user(user) and finding.assigned_to_id == user.pk)


def can_create_at(user: Any, location: Any) -> bool:
    return _at(user, P.FINDING_CREATE, location)


def can_update(user: Any, finding: Any) -> bool:
    return finding.status in EDITABLE_STATUSES and _at(user, P.FINDING_UPDATE, finding.location)


def can_assign(user: Any, finding: Any) -> bool:
    return finding.status in EDITABLE_STATUSES and _at(user, P.FINDING_ASSIGN, finding.location)


def can_act(user: Any, finding: Any, action: str) -> bool:
    """Is the *permission* held for this lifecycle action (state is checked separately by the service)?"""
    spec = TRANSITIONS.get(action)
    return spec is not None and _at(user, spec.permission, finding.location)


def available_actions(user: Any, finding: Any) -> list[Transition]:
    """The lifecycle actions the user may perform on the finding *right now* (permission and state)."""
    return [t for t in TRANSITIONS.values() if finding.status in t.sources and can_act(user, finding, t.action)]


def can_be_assigned(user: Any, location: Any) -> bool:
    """May this user be made responsible at the location?  Active, and able to work findings there."""
    return _at(user, P.FINDING_RESOLVE, location)


def can_manage_config(user: Any, organization: Any) -> bool:
    return is_active_user(user) and has_permission(user, P.FINDING_MANAGE, organization=organization)
