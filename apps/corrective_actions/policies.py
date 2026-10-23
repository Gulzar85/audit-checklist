"""Authorization for corrective actions (ADR 0067): organization, role permission, *location scope*, ownership and separation of duties.

Every decision calls the central ``apps.permissions.authorization`` service - no role name is tested here.  Permissions are
location-scoped (the action's location is its finding's).  Three kinds of actor are distinguished:

* **worker** actions (start, block, resume, submit, progress, plan notes): the responsible user *or* a manager holding
  ``corrective_action.update`` - and in both cases the specific permission at the location;
* **verifier** actions (verify, reject): the permission *plus* separation of duties - where the action requires independent
  verification the verifier is neither the owner nor the submitter (and, by setting, not the creator);
* **manager** actions (issue, assign, reassign, edit, close, cancel, reopen): the permission alone.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.core.policies import is_active_user
from apps.permissions.authorization import has_permission
from apps.permissions.registry import P

from .constants import PLANNING_STATUSES, TRANSITIONS, ActionStatus, Purpose, Transition, VERIFIER_PURPOSES, WORKER_PURPOSES


def _at(user: Any, code: str, location: Any) -> bool:
    return is_active_user(user) and location is not None and has_permission(user, code, location=location)


def is_owner(user: Any, action: Any) -> bool:
    return bool(is_active_user(user) and action.assigned_to_id is not None and action.assigned_to_id == user.pk)


def can_view(user: Any, action: Any) -> bool:
    return _at(user, P.CORRECTIVE_ACTION_VIEW, action.location) or is_owner(user, action)


def is_worker(user: Any, action: Any) -> bool:
    """The responsible user, or a manager who may act for them."""
    return is_owner(user, action) or _at(user, P.CORRECTIVE_ACTION_UPDATE, action.location)


def can_create_for(user: Any, finding: Any) -> bool:
    from apps.findings.constants import EDITABLE_STATUSES

    return finding.status in EDITABLE_STATUSES and _at(user, P.CORRECTIVE_ACTION_CREATE, finding.location)


def can_edit(user: Any, action: Any) -> bool:
    """Open the plan form: a manager, or the responsible user (who may only change the plan text, not due date/priority/owner)."""
    return action.status in PLANNING_STATUSES and is_worker(user, action) and (
        _at(user, P.CORRECTIVE_ACTION_UPDATE, action.location) or _at(user, P.CORRECTIVE_ACTION_UPDATE_PROGRESS, action.location))


def can_manage(user: Any, action: Any) -> bool:
    """May change title, due date, priority and the evidence requirement."""
    return action.status in PLANNING_STATUSES and _at(user, P.CORRECTIVE_ACTION_UPDATE, action.location)


def assign_permission(action: Any) -> str:
    """First assignment needs ``assign``; changing an existing owner needs ``reassign`` (the owner cannot hand their own work away)."""
    return P.CORRECTIVE_ACTION_ASSIGN if action.assigned_to_id is None else P.CORRECTIVE_ACTION_REASSIGN


def can_assign(user: Any, action: Any) -> bool:
    return action.status in PLANNING_STATUSES and _at(user, assign_permission(action), action.location)


def can_add_update(user: Any, action: Any) -> bool:
    return action.status in (ActionStatus.IN_PROGRESS, ActionStatus.BLOCKED) and is_worker(user, action) and _at(user, P.CORRECTIVE_ACTION_UPDATE_PROGRESS, action.location)


def can_be_assigned(user: Any, location: Any) -> bool:
    """May this user be made responsible at the location?  Active, and able to carry out actions there."""
    return _at(user, P.CORRECTIVE_ACTION_START, location)


# -- verification -------------------------------------------------------------------------------------------------------------
def separation_ok(user: Any, action: Any) -> bool:
    """Separation of duties: an independently verified action is never decided by its owner, its submitter (or, by setting, its creator)."""
    if not action.independent_verification:
        return True
    if is_owner(user, action) or (action.submitted_by_id is not None and action.submitted_by_id == user.pk):
        return False
    if getattr(settings, "CORRECTIVE_ACTION_VERIFIER_NOT_CREATOR", False) and action.created_by_id == user.pk:
        return False
    return True


def can_decide(user: Any, action: Any, permission: str) -> bool:
    return action.status == ActionStatus.SUBMITTED and _at(user, permission, action.location) and separation_ok(user, action)


# -- transitions --------------------------------------------------------------------------------------------------------------
def can_act(user: Any, action: Any, name: str) -> bool:
    """Is the *permission* (and ownership / separation rule) held for this lifecycle action?  State is checked by the service."""
    spec = TRANSITIONS.get(name)
    if spec is None or not _at(user, spec.permission, action.location):
        return False
    if spec.actor == "owner":
        return is_worker(user, action)
    if spec.actor == "verifier":
        return separation_ok(user, action)
    return True


def available_actions(user: Any, action: Any) -> list[Transition]:
    """The lifecycle actions the user may perform on the action *right now* (permission, ownership and state)."""
    return [t for t in TRANSITIONS.values() if action.status in t.sources and can_act(user, action, t.action)]


# -- evidence stages ----------------------------------------------------------------------------------------------------------
def can_attach_evidence(user: Any, action: Any, purpose: str) -> bool:
    if purpose in VERIFIER_PURPOSES:
        perm = P.CORRECTIVE_ACTION_VERIFY if purpose == Purpose.VERIFICATION else P.CORRECTIVE_ACTION_REJECT
        return action.status in VERIFIER_PURPOSES[purpose] and can_decide(user, action, perm)
    if purpose in WORKER_PURPOSES:
        return action.status in WORKER_PURPOSES[purpose] and is_worker(user, action) and (
            _at(user, P.CORRECTIVE_ACTION_UPDATE_PROGRESS, action.location) or _at(user, P.CORRECTIVE_ACTION_UPDATE, action.location))
    return False
