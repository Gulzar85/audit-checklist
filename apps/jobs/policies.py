"""Who may do what with schedules and jobs (Phase 18).

Organization schedules need the organization-wide ``jobs.*`` permission in *that* organization.  Platform-wide ones (``organization is None``) - and the
tasks that are platform-wide by nature (outbox processing, retention) - are limited to superusers: a tenant administrator must never be able to run something
that touches other tenants.  A task may add a stricter permission of its own (``Task.permission``).
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

from . import registry


def _is_superuser(user: Any) -> bool:
    return bool(is_active_user(user) and getattr(user, "is_superuser", False))


def _allowed(user: Any, code: str, organization: Any) -> bool:
    if not is_active_user(user):
        return False
    if organization is None:
        return _is_superuser(user)
    return _is_superuser(user) or has_permission(user, code, organization=getattr(organization, "pk", organization))


def can_view(user: Any, organization: Any) -> bool:
    return _allowed(user, P.JOBS_VIEW, organization)


def can_manage(user: Any, organization: Any) -> bool:
    return _allowed(user, P.JOBS_MANAGE, organization)


def can_run(user: Any, organization: Any) -> bool:
    return _allowed(user, P.JOBS_RUN, organization)


def can_trigger(user: Any, task: registry.Task, organization: Any) -> bool:
    """May ``user`` start ``task`` now for ``organization``?  (The scope rule, the task's own permission and the organization.)"""
    if task.scope == registry.SCOPE_GLOBAL:
        return organization is None and _is_superuser(user) and task.manual
    if task.scope == registry.SCOPE_ORGANIZATION and organization is None:
        return False
    return task.manual and _allowed(user, task.permission, organization)


def can_schedule(user: Any, task: registry.Task, organization: Any) -> bool:
    if not task.schedulable:
        return False
    if task.scope == registry.SCOPE_GLOBAL:
        return organization is None and _is_superuser(user)
    if task.scope == registry.SCOPE_ORGANIZATION and organization is None:
        return False
    return can_manage(user, organization)


def can_view_anywhere(user: Any) -> bool:
    from apps.permissions.authorization import get_accessible_organizations

    return _is_superuser(user) or (is_active_user(user) and get_accessible_organizations(user, P.JOBS_VIEW).exists())
