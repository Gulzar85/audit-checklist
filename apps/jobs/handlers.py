"""Task handlers: every one calls an existing, reviewed service - nothing here sends email, resolves recipients or implements a business rule itself.

Registered tasks (the complete list; ``docs/scheduling-task-handlers.md``):

* ``notifications.process_pending``  - Phase 17: outbox events -> notifications, then due email deliveries (``services.process_pending`` + ``services.dispatch_batch``).
* ``notifications.dispatch_retries`` - Phase 17: send deliveries that are due (first attempts and bounded retries) (``services.dispatch_batch``).
* ``notifications.purge_history``    - Phase 17 retention (``services.purge``).
* ``corrective_actions.generate_due_reminders`` - publishes the existing ``corrective_action.due_soon`` / ``overdue`` events (``services.emit_due_reminders``); the notification engine
  then applies its rules, recipients, preferences and idempotency.  Eligibility (open, assigned, with a due date) is re-evaluated by that service every time it runs.
* ``jobs.cleanup_old_execution_history`` - this module's own retention.

Not registered, on purpose: checklist-assignment and workflow-task reminders.  The platform has due dates for them but no reminder *events* in the notification registry; inventing
those lifecycle events is outside this phase (see docs/scheduling-architecture.md, "Gaps").
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from . import registry
from .errors import PermanentError
from .registry import Context, Field, Task


def _ints(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if isinstance(value, (int, bool))}


def process_pending(ctx: Context) -> dict[str, Any]:
    from apps.notifications import services

    limit = ctx.payload["limit"]
    events = services.process_pending(limit, dispatch=False)
    deliveries = services.dispatch_batch(limit)
    return {**{f"events_{key}": value for key, value in _ints(events).items()}, **{f"deliveries_{key}": value for key, value in _ints(deliveries).items()}}


def dispatch_retries(ctx: Context) -> dict[str, Any]:
    from apps.notifications import services

    return _ints(services.dispatch_batch(ctx.payload["limit"]))


def purge_history(ctx: Context) -> dict[str, Any]:
    from apps.notifications import services

    days = ctx.payload["days"] or int(getattr(settings, "NOTIFICATION_RETENTION_DAYS", 180))
    try:
        return _ints(services.purge(days=days, limit=ctx.payload["limit"]))
    except services.NotificationError as exc:
        raise PermanentError(exc.message) from exc


def corrective_action_reminders(ctx: Context) -> dict[str, Any]:
    from apps.notifications import services

    return _ints(services.emit_due_reminders(within_hours=ctx.payload["within_hours"], organization=ctx.organization, limit=ctx.payload["limit"]))


def cleanup_history(ctx: Context) -> dict[str, Any]:
    from .services import retention

    return retention.cleanup(days=ctx.payload["days"] or None, limit=ctx.payload["limit"])


LIMIT = Field("int", 100, 1, 500, help="Maximum items handled in one run.")

registry.register(Task("notifications.process_pending", "Process pending notifications", "Turns published events into notifications and sends the deliveries that are due.", registry.SCOPE_GLOBAL,
                       process_pending, {"limit": LIMIT}, max_attempts=3, base_delay_seconds=60))
registry.register(Task("notifications.dispatch_retries", "Dispatch notification deliveries and retries", "Sends pending deliveries and those whose retry time has come.", registry.SCOPE_GLOBAL,
                       dispatch_retries, {"limit": LIMIT}, max_attempts=3, base_delay_seconds=60))
registry.register(Task("notifications.purge_history", "Purge old notification history", "Retention for finished notification events, notifications and deliveries.", registry.SCOPE_GLOBAL,
                       purge_history, {"days": Field("int", 0, 0, 3650, help="0 = the configured retention."), "limit": Field("int", 1000, 1, 10000)}, max_attempts=2, base_delay_seconds=300))
registry.register(Task("corrective_actions.generate_due_reminders", "Corrective action reminders", "Publishes due-soon and overdue events for open, assigned corrective actions.", registry.SCOPE_BOTH,
                       corrective_action_reminders, {"within_hours": Field("int", 48, 1, 720, help="Due-soon horizon in hours."), "limit": Field("int", 500, 1, 5000)}, max_attempts=3,
                       base_delay_seconds=120))
registry.register(Task("jobs.cleanup_old_execution_history", "Clean up scheduling history", "Deletes finished jobs and their execution history past the retention period.", registry.SCOPE_GLOBAL,
                       cleanup_history, {"days": Field("int", 0, 0, 3650, help="0 = the configured retention (minimum 7)."), "limit": Field("int", 1000, 1, 10000)}, max_attempts=2, base_delay_seconds=300))
