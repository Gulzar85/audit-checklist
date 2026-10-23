"""Vocabulary of the findings engine: statuses, controlled sources, the transition matrix and the default configuration.

Nothing here is tied to an industry: categories and severity levels are *organization data* (``FindingCategory`` /
``FindingSeverity``).  ``DEFAULT_CATEGORIES`` / ``DEFAULT_SEVERITIES`` are only the starting set copied into an organization the
first time it needs them; an administrator may rename, add, reorder or deactivate them.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models

from apps.permissions.registry import P


class FindingStatus(models.TextChoices):
    OPEN = "open", "Open"
    ACKNOWLEDGED = "acknowledged", "Acknowledged"
    IN_PROGRESS = "in_progress", "In progress"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"
    REOPENED = "reopened", "Reopened"
    CANCELLED = "cancelled", "Cancelled"


#: Work still needed: these statuses can be overdue and count as "open" on the dashboard.
ACTIVE_STATUSES = (FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED, FindingStatus.IN_PROGRESS, FindingStatus.REOPENED)
#: Resolved is *not* closed: the fix was reported but nothing has verified it (ADR 0063).
AWAITING_CLOSURE = (FindingStatus.RESOLVED,)
TERMINAL_STATUSES = (FindingStatus.CLOSED, FindingStatus.CANCELLED)
OVERDUE = "overdue"  # a derived, never persisted, filter value


class SourceType(models.TextChoices):
    """The controlled registry of where a finding may come from.  A client never names a model; it can only pick a *registered* source
    through a service that knows how to validate it (today: a rule on a submitted checklist, or an authorized manual report)."""

    CHECKLIST_RULE = "checklist_rule", "Checklist rule"
    MANUAL = "manual", "Manual report"


@dataclass(frozen=True)
class Transition:
    action: str
    sources: tuple[str, ...]
    target: str
    permission: str
    #: A reason / resolution text is mandatory.
    reason_required: bool = False
    label: str = ""


TRANSITIONS: dict[str, Transition] = {
    t.action: t
    for t in (
        Transition("acknowledge", (FindingStatus.OPEN, FindingStatus.REOPENED), FindingStatus.ACKNOWLEDGED, P.FINDING_ACKNOWLEDGE, False, "Acknowledge"),
        Transition("start", (FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED, FindingStatus.REOPENED), FindingStatus.IN_PROGRESS, P.FINDING_RESOLVE, False, "Start work"),
        Transition("resolve", (FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED, FindingStatus.IN_PROGRESS, FindingStatus.REOPENED), FindingStatus.RESOLVED,
                   P.FINDING_RESOLVE, True, "Resolve"),
        Transition("close", (FindingStatus.RESOLVED,), FindingStatus.CLOSED, P.FINDING_CLOSE, False, "Close"),
        Transition("reopen", (FindingStatus.RESOLVED, FindingStatus.CLOSED), FindingStatus.REOPENED, P.FINDING_REOPEN, True, "Reopen"),
        Transition("cancel", (*ACTIVE_STATUSES, FindingStatus.RESOLVED), FindingStatus.CANCELLED, P.FINDING_CANCEL, True, "Cancel"),
    )
}

#: Statuses in which title, description, category, severity, owner and due date may still be changed.
EDITABLE_STATUSES = (*ACTIVE_STATUSES, FindingStatus.RESOLVED)

#: Organization-level starting data (code, name, description).
DEFAULT_CATEGORIES = (
    ("safety", "Safety", "Risks to people or property."),
    ("quality", "Quality", "Deviations from a quality standard."),
    ("equipment", "Equipment", "Faulty, missing or unsafe equipment."),
    ("process", "Process compliance", "A required procedure was not followed."),
    ("documentation", "Documentation", "Missing or incorrect records."),
    ("other", "Other", "Anything that fits no other category."),
)
#: ``(code, name, rank, requires_owner, default_due_days, is_escalation, is_default)``; a higher ``rank`` is more severe.
DEFAULT_SEVERITIES = (
    ("low", "Low", 10, False, None, False, False),
    ("medium", "Medium", 20, False, None, False, True),
    ("high", "High", 30, True, None, False, False),
    ("critical", "Critical", 40, True, None, True, False),
)
