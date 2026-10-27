"""Connects the existing modules' domain events to the notification outbox - the business modules never import this app and never send anything themselves.

Every receiver runs after the business transaction committed (the modules send their signals with ``transaction.on_commit``), turns the signal into one validated
``publish(...)`` call with a discriminator that is the same for a duplicate delivery of the same signal and different for a genuinely new occurrence, and **never raises** into the
business flow: a notification problem is logged, the business change stands.  To subscribe another module: register an event in ``registry.py`` and connect its signal here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def _stamp(obj: Any) -> str:
    value = getattr(obj, "updated_at", None)
    return value.isoformat() if value else ""


def _safe(event_type: str, source_of: Callable[[dict[str, Any]], Any], discriminator: Callable[[Any], str], metadata: Callable[[Any], dict[str, Any]] | None = None) -> Callable[..., None]:
    def receiver(sender: Any, actor: Any = None, **kwargs: Any) -> None:
        try:
            from .services import events

            source = source_of(kwargs)
            events.publish(event_type, source, actor=actor, discriminator=discriminator(source), metadata=metadata(source) if metadata else None)
        except Exception:  # noqa: BLE001 - a notification problem must never break (or fail) the business flow that raised the event
            logger.exception("Could not publish notification event %s", event_type)

    return receiver


def _connect() -> None:
    from apps.assignments import events as assignment_events
    from apps.corrective_actions import events as action_events
    from apps.evaluation import events as evaluation_events
    from apps.executions import events as execution_events
    from apps.findings import events as finding_events
    from apps.workflows import events as workflow_events

    table = [
        (assignment_events.assignment_issued, "assignment.issued", lambda kw: kw["assignment"], lambda s: f"issued:{_stamp(s)}"),
        (assignment_events.assignment_assignee_changed, "assignment.assignee_changed", lambda kw: kw["assignment"], lambda s: f"to:{s.assigned_to_id}:{_stamp(s)}"),
        (assignment_events.assignment_cancelled, "assignment.cancelled", lambda kw: kw["assignment"], lambda s: f"cancelled:{_stamp(s)}"),
        (execution_events.execution_submitted, "execution.submitted", lambda kw: kw["execution"], lambda s: "submitted"),
        (finding_events.finding_raised, "finding.raised", lambda kw: kw["finding"], lambda s: "raised"),
        (finding_events.finding_assigned, "finding.assigned", lambda kw: kw["finding"], lambda s: f"to:{s.assigned_to_id}:{_stamp(s)}"),
        (action_events.action_assigned, "corrective_action.assigned", lambda kw: kw["action"], lambda s: f"to:{s.assigned_to_id}:{_stamp(s)}"),
        (action_events.action_submitted, "corrective_action.submitted", lambda kw: kw["action"], lambda s: f"submission:{s.submission_count}"),
        (action_events.action_rejected, "corrective_action.rejected", lambda kw: kw["action"], lambda s: f"rejected:{_stamp(s)}"),
        (action_events.action_verified, "corrective_action.verified", lambda kw: kw["action"], lambda s: f"verified:{_stamp(s)}"),
        (action_events.action_closed, "corrective_action.closed", lambda kw: kw["action"], lambda s: f"closed:{_stamp(s)}"),
        (workflow_events.task_created, "workflow.task_assigned", lambda kw: kw["task"], lambda s: "created"),
        (workflow_events.instance_finished, "workflow.completed", lambda kw: kw["instance"], lambda s: f"finished:{s.revision}"),
        (evaluation_events.evaluation_recorded, "evaluation.recorded", lambda kw: kw["run"], lambda s: "recorded"),
    ]
    for signal, event_type, source_of, discriminator in table:
        signal.connect(_safe(event_type, source_of, discriminator), dispatch_uid=f"notifications.{event_type}", weak=False)


if getattr(settings, "NOTIFICATIONS_CONNECT_SIGNALS", True):
    _connect()
