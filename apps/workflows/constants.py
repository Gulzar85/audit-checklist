"""Vocabulary of the workflow engine: version / state / instance / task statuses and the controlled policy options.

Everything a designer can choose is an enumeration or a registry entry (target types, business actions, permissions, separation
options, assignee policy types).  Nothing is free text that the server would interpret, and nothing is executable.
"""

from __future__ import annotations

from django.db import models


class VersionStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    SUPERSEDED = "superseded", "Superseded"
    ARCHIVED = "archived", "Archived"


class DefinitionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


class StateKind(models.TextChoices):
    INITIAL = "initial", "Initial"
    INTERMEDIATE = "intermediate", "Intermediate"
    SUCCESS = "success", "Completed successfully"
    FAILURE = "failure", "Ended unsuccessfully"


TERMINAL_KINDS = (StateKind.SUCCESS, StateKind.FAILURE)


class InstanceStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class Outcome(models.TextChoices):
    SUCCESS = "success", "Success"
    FAILURE = "failure", "Failure"


class TaskStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CLAIMED = "claimed", "Claimed"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


OPEN_TASK_STATUSES = (TaskStatus.PENDING, TaskStatus.CLAIMED)


class EventKind(models.TextChoices):
    STARTED = "started", "Started"
    TRANSITION = "transition", "Transition"
    APPROVAL = "approval", "Approval recorded"
    TASK_CREATED = "task_created", "Task created"
    TASK_CLAIMED = "task_claimed", "Task claimed"
    TASK_RELEASED = "task_released", "Task released"
    TASK_REASSIGNED = "task_reassigned", "Task reassigned"
    ATTENTION = "attention", "Needs administrative attention"
    CANCELLED = "cancelled", "Cancelled"


class ActorRule(models.TextChoices):
    """Who may perform a transition."""

    ASSIGNEE = "assignee", "A task assignee of the current state"
    STARTER = "starter", "The person who started the workflow (or a manager)"
    AUTHORIZED = "authorized", "Anyone holding the workflow transition permission"


class Decision(models.TextChoices):
    """How a transition counts: approvals accumulate (per the state's approval mode); everything else fires at once."""

    APPROVE = "approve", "Approve"
    REJECT = "reject", "Reject"
    OTHER = "other", "Other"


class ApprovalMode(models.TextChoices):
    ONE = "one", "One approval"
    ALL = "all", "Every assigned approver"
    MIN = "min", "A minimum number of approvers"


class OnNoAssignee(models.TextChoices):
    BLOCK = "block", "Block the transition"
    ATTENTION = "attention", "Flag the workflow for administrative attention"


class AssigneeType(models.TextChoices):
    USER = "user", "Specific users"
    PERMISSION = "permission", "Anyone holding a permission"
    ROLE = "role", "Anyone holding a role"


#: Separation-of-duties options a state may enable (a designer chooses from these; none can weaken a platform rule).
SEPARATION_OPTIONS = {
    "not_starter": "The actor is not the person who started the workflow",
    "not_target_owner": "The actor is not the responsible user of the business record",
    "not_target_creator": "The actor is not the creator of the business record",
    "not_previous_actor": "The actor has not acted earlier in this workflow",
}

#: Capabilities (the ``workflow.*`` permission codes) are in the permission registry; these are only the choices a designer sees.
MAX_STATES = 40
MAX_TRANSITIONS = 120
