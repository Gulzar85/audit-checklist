"""Authorization for workflows (ADR 0072): organization membership, role permission and location scope.

Design permissions (``workflow.view|create|update_draft|validate|publish|archive``) are **organization-wide**: a definition belongs to an organization, not to a
location.  A *system* workflow (no organization) is read-only for organizations and changed only by platform superusers.  Runtime permissions
(``start|transition|cancel|instance_view|task_view|task_claim|task_reassign``) are location-scoped at the instance's location.

**A task assignment is not an authorization grant**: being assigned a task lets a user see and act on *that task*; the business record must still be visible
through its own policy, and the domain service rechecks the actor's own permission when the transition invokes it.
"""

from __future__ import annotations

from typing import Any

from apps.core.policies import is_active_user
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

from .constants import InstanceStatus, VersionStatus


def _org(user: Any, code: str, organization: Any) -> bool:
    return is_active_user(user) and organization is not None and has_permission(user, code, organization=organization)


def _at(user: Any, code: str, location: Any) -> bool:
    return is_active_user(user) and location is not None and has_permission(user, code, location=location)


# -- design -------------------------------------------------------------------------------------------------------------------------
def can_view_definition(user: Any, definition: Any, organization: Any = None) -> bool:
    org = definition.organization if definition.organization_id else organization
    return _org(user, P.WORKFLOW_VIEW, org)


def _design(user: Any, definition: Any, code: str) -> bool:
    if definition.organization_id is None:
        return bool(is_active_user(user) and getattr(user, "is_superuser", False))
    return _org(user, code, definition.organization)


def can_create(user: Any, organization: Any) -> bool:
    return _org(user, P.WORKFLOW_CREATE, organization)


def can_create_system(user: Any) -> bool:
    return bool(is_active_user(user) and getattr(user, "is_superuser", False))


def can_edit_draft(user: Any, version: Any) -> bool:
    return version.status == VersionStatus.DRAFT and _design(user, version.definition, P.WORKFLOW_UPDATE_DRAFT)


def can_validate(user: Any, version: Any) -> bool:
    return _design(user, version.definition, P.WORKFLOW_VALIDATE)


def can_publish(user: Any, version: Any) -> bool:
    return version.status == VersionStatus.DRAFT and _design(user, version.definition, P.WORKFLOW_PUBLISH)


def can_archive(user: Any, definition: Any) -> bool:
    return _design(user, definition, P.WORKFLOW_ARCHIVE)


def can_create_version(user: Any, definition: Any) -> bool:
    return _design(user, definition, P.WORKFLOW_UPDATE_DRAFT)


# -- runtime ------------------------------------------------------------------------------------------------------------------------
def can_start(user: Any, location: Any) -> bool:
    return _at(user, P.WORKFLOW_START, location)


def can_view_instance(user: Any, instance: Any, *, has_task: bool = False) -> bool:
    if _at(user, P.WORKFLOW_INSTANCE_VIEW, instance.location):
        return True
    return bool(is_active_user(user) and (has_task or instance.started_by_id == user.pk))


def can_transition_authorized(user: Any, instance: Any) -> bool:
    return _at(user, P.WORKFLOW_TRANSITION, instance.location)


def can_cancel(user: Any, instance: Any) -> bool:
    return instance.status == InstanceStatus.ACTIVE and _at(user, P.WORKFLOW_CANCEL, instance.location)


def can_view_tasks_at(user: Any, location: Any) -> bool:
    return _at(user, P.WORKFLOW_TASK_VIEW, location)


def can_claim_at(user: Any, location: Any) -> bool:
    return _at(user, P.WORKFLOW_TASK_CLAIM, location)


def can_reassign_at(user: Any, location: Any) -> bool:
    return _at(user, P.WORKFLOW_TASK_REASSIGN, location)
