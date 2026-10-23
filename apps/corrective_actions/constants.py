"""Vocabulary of the corrective-action engine: statuses, priorities, the transition matrix and the evidence stages.

``overdue`` is a *derived* indicator, never a status (ADR 0066).  Completion by the owner (``submitted``), independent verification
(``verified``) and closure (``closed``) are three separate business events.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models

from apps.permissions.registry import P


class ActionStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In progress"
    BLOCKED = "blocked", "Blocked"
    SUBMITTED = "submitted", "Awaiting verification"
    REJECTED = "rejected", "Rejected"
    VERIFIED = "verified", "Verified"
    CLOSED = "closed", "Closed"
    CANCELLED = "cancelled", "Cancelled"


class Priority(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


PRIORITY_RANK = {Priority.LOW: 1, Priority.NORMAL: 2, Priority.HIGH: 3, Priority.CRITICAL: 4}

#: The owner still has work to do: these statuses can be overdue.  ``submitted`` is excluded on purpose - the owner has done their part and
#: the delay (if any) is the verifier's, which is a different measure.
WORKING_STATUSES = (ActionStatus.OPEN, ActionStatus.IN_PROGRESS, ActionStatus.BLOCKED, ActionStatus.REJECTED)
#: Everything that still stands between a finding and "all actions done" (cancelled and closed/verified do not).
OUTSTANDING_STATUSES = (ActionStatus.DRAFT, ActionStatus.OPEN, ActionStatus.IN_PROGRESS, ActionStatus.BLOCKED, ActionStatus.SUBMITTED, ActionStatus.REJECTED)
DONE_STATUSES = (ActionStatus.VERIFIED, ActionStatus.CLOSED)
TERMINAL_STATUSES = (ActionStatus.CLOSED, ActionStatus.CANCELLED)
#: The plan, due date, priority and owner may still change.
PLANNING_STATUSES = (ActionStatus.DRAFT, ActionStatus.OPEN, ActionStatus.IN_PROGRESS, ActionStatus.BLOCKED, ActionStatus.REJECTED)
OVERDUE = "overdue"  # derived filter value


class Decision(models.TextChoices):
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class UpdateKind(models.TextChoices):
    PROGRESS = "progress", "Progress"
    BLOCKER = "blocker", "Blocker reported"
    BLOCKER_CLEARED = "blocker_cleared", "Blocker cleared"
    SUBMISSION = "submission", "Submitted for verification"
    CORRECTION = "correction", "Correction"


class Purpose(models.TextChoices):
    """The stage an evidence file belongs to (``EvidenceAttachment.purpose``)."""

    PLAN = "plan", "Plan"
    PROGRESS = "progress", "Work in progress"
    COMPLETION = "completion", "Completion"
    VERIFICATION = "verification", "Verification"
    REJECTION = "rejection", "Rejection"


#: Which evidence purposes may be attached while the action is in each status, and by whom.
WORKER_PURPOSES = {
    Purpose.PLAN: (ActionStatus.DRAFT, ActionStatus.OPEN, ActionStatus.IN_PROGRESS, ActionStatus.BLOCKED, ActionStatus.REJECTED),
    Purpose.PROGRESS: (ActionStatus.IN_PROGRESS, ActionStatus.BLOCKED),
    Purpose.COMPLETION: (ActionStatus.IN_PROGRESS, ActionStatus.BLOCKED),
}
VERIFIER_PURPOSES = {Purpose.VERIFICATION: (ActionStatus.SUBMITTED,), Purpose.REJECTION: (ActionStatus.SUBMITTED,)}


@dataclass(frozen=True)
class Transition:
    action: str
    sources: tuple[str, ...]
    target: str
    permission: str
    #: ``owner``: the responsible user or a manager (``corrective_action.update``); ``anyone``: the permission alone; ``verifier``: separation of duties applies.
    actor: str = "anyone"
    reason_required: bool = False
    label: str = ""


_S = ActionStatus
TRANSITIONS: dict[str, Transition] = {
    t.action: t
    for t in (
        Transition("issue", (_S.DRAFT,), _S.OPEN, P.CORRECTIVE_ACTION_CREATE, "anyone", False, "Issue"),
        Transition("start", (_S.OPEN,), _S.IN_PROGRESS, P.CORRECTIVE_ACTION_START, "owner", False, "Start work"),
        Transition("block", (_S.IN_PROGRESS,), _S.BLOCKED, P.CORRECTIVE_ACTION_BLOCK, "owner", True, "Report a blocker"),
        Transition("resume", (_S.BLOCKED, _S.REJECTED), _S.IN_PROGRESS, P.CORRECTIVE_ACTION_START, "owner", False, "Resume work"),
        Transition("submit", (_S.IN_PROGRESS, _S.BLOCKED), _S.SUBMITTED, P.CORRECTIVE_ACTION_SUBMIT, "owner", True, "Submit for verification"),
        Transition("verify", (_S.SUBMITTED,), _S.VERIFIED, P.CORRECTIVE_ACTION_VERIFY, "verifier", False, "Approve"),
        Transition("reject", (_S.SUBMITTED,), _S.REJECTED, P.CORRECTIVE_ACTION_REJECT, "verifier", True, "Reject"),
        Transition("close", (_S.VERIFIED,), _S.CLOSED, P.CORRECTIVE_ACTION_CLOSE, "anyone", False, "Close"),
        Transition("cancel", (_S.DRAFT, _S.OPEN, _S.IN_PROGRESS, _S.BLOCKED, _S.REJECTED), _S.CANCELLED, P.CORRECTIVE_ACTION_CANCEL, "anyone", True, "Cancel"),
        Transition("reopen", (_S.VERIFIED, _S.CLOSED), _S.IN_PROGRESS, P.CORRECTIVE_ACTION_REOPEN, "anyone", True, "Reopen"),
    )
}
