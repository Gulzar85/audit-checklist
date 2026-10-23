"""Authorization for scoring (ADR 0073-0075).  Every decision calls the central permission service; no role name is tested here.

* The **configuration** belongs to a checklist version: it is organization-wide (``scoring.view`` / ``scoring.configure``) and editable
  only while the version is a draft of a live checklist.  Publishing stays ``checklist.publish`` (scoring is part of the version).
* A **score** follows its execution: the caller must be able to see the execution (``executions.policies.can_view``) *and* hold the
  scoring permission at the execution's location.  Seeing an execution never implies seeing its score or its breakdown.
"""

from __future__ import annotations

from typing import Any

from apps.checklists import policies as checklist_policies
from apps.checklists.constants import TemplateStatus, VersionStatus
from apps.core.policies import is_active_user
from apps.executions import policies as execution_policies
from apps.executions.constants import ExecutionStatus
from apps.permissions.authorization import has_permission, has_permission_anywhere
from apps.permissions.registry import P


def _org(user: Any, code: str, version: Any) -> bool:
    return is_active_user(user) and has_permission(user, code, organization=version.template.organization_id)


def _at(user: Any, code: str, execution: Any) -> bool:
    return is_active_user(user) and has_permission(user, code, location=execution.assignment.location)


def can_view_config(user: Any, version: Any) -> bool:
    return checklist_policies.can_view_version(user, version) and _org(user, P.SCORING_VIEW, version)


def can_configure(user: Any, version: Any) -> bool:
    return (version.status == VersionStatus.DRAFT and version.template.status != TemplateStatus.ARCHIVED
            and _org(user, P.SCORING_CONFIGURE, version))


def can_configure_at(user: Any, version: Any) -> bool:
    """Permission without the draft rule (so a state error can be reported after authorization)."""
    return _org(user, P.SCORING_CONFIGURE, version)


def can_view_score(user: Any, execution: Any) -> bool:
    return execution_policies.can_view(user, execution) and _at(user, P.SCORING_SCORE_VIEW, execution)


def can_view_breakdown(user: Any, execution: Any) -> bool:
    return execution_policies.can_view(user, execution) and _at(user, P.SCORING_BREAKDOWN_VIEW, execution)


def can_preview(user: Any, execution: Any) -> bool:
    """The live (provisional) score of an execution that is still in progress."""
    return (execution.status == ExecutionStatus.IN_PROGRESS and execution_policies.can_view(user, execution)
            and _at(user, P.SCORING_RECALCULATE, execution))


def can_finalize_recalculate(user: Any, execution: Any) -> bool:
    return (execution.status == ExecutionStatus.SUBMITTED and execution_policies.can_view(user, execution)
            and _at(user, P.SCORING_FINALIZE_RECALCULATE, execution))


def can_manage_anywhere(user: Any) -> bool:
    return has_permission_anywhere(user, P.SCORING_CONFIGURE)
