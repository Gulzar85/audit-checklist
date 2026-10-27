"""Due-date reminder *events* - emitted only when an authorized process invokes this (Phase 18 will schedule it; Phase 17 does not).

Nothing here sends anything: it publishes ``corrective_action.due_soon`` / ``corrective_action.overdue`` events into the outbox, idempotently per action and **per due date** (an extended
due date earns a fresh reminder; calling it twice for the same state changes nothing).  The notification rules, recipients, preferences and delivery run exactly as for every other event.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from . import events


def emit_due_reminders(*, within_hours: int = 48, now: Any = None, organization: Any = None, limit: int = 500) -> dict[str, int]:
    """Publish due-soon and overdue events for open corrective actions.  Returns the number of *new* events."""
    from apps.corrective_actions.constants import TERMINAL_STATUSES
    from apps.corrective_actions.models import CorrectiveAction

    now = now or timezone.now()
    horizon = now + timedelta(hours=max(1, int(within_hours)))
    base = CorrectiveAction.objects.exclude(status__in=TERMINAL_STATUSES).filter(due_at__isnull=False, assigned_to__isnull=False).select_related("organization", "location", "finding")
    if organization is not None:
        base = base.filter(organization=organization)
    soon = overdue = 0
    for action in base.filter(due_at__gt=now, due_at__lte=horizon).order_by("due_at")[:limit]:
        row = events.publish("corrective_action.due_soon", action, discriminator=f"due:{action.due_at.isoformat()}")
        soon += 1 if row.created_now else 0
    for action in base.filter(due_at__lte=now).order_by("due_at")[:limit]:
        row = events.publish("corrective_action.overdue", action, discriminator=f"due:{action.due_at.isoformat()}")
        overdue += 1 if row.created_now else 0
    return {"due_soon": soon, "overdue": overdue}
