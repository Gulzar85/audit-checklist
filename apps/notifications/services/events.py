"""Event ingestion (the outbox), rule matching and notification creation.

The publication contract (ADR 0080):

1. ``publish(event_type, source, ...)`` validates the event against the fixed registries and writes one ``NotificationEvent`` row, idempotent per ``(organization, dedupe_key)``.
   Called inside a business transaction the row commits (or rolls back) **with** the business change; the domain signals of the existing modules are already sent after commit, and
   ``integration.py`` publishes from them.  Processing is scheduled with ``transaction.on_commit``, so a rolled-back change never produces a notification.
2. ``process_event`` claims the row (``SELECT ... FOR UPDATE SKIP LOCKED``), matches rules, resolves recipients, writes the notifications and their deliveries in **one** short transaction,
   and marks the event processed.  A failure marks it ``failed`` and never touches the business transaction; ``process_pending`` (``manage.py process_notification_events``) retries it.
3. Email is **never** sent inside these transactions: ``delivery.dispatch_*`` runs afterwards, inline right after commit (``NOTIFICATION_DISPATCH_INLINE``) or from the command.

Honest limit: a crash *between* the business commit and the outbox insert (domain signals) loses the event; publishing from inside the business transaction (this function) closes that gap.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .. import constants as k
from .. import filters, preferences_policy, recipients, registry
from ..models import Notification, NotificationDelivery, NotificationEvent, NotificationRule
from . import config, delivery
from .base import VERBS, audit, mask_email
from .errors import NotificationError

logger = logging.getLogger(__name__)
MAX_EVENT_ATTEMPTS = 5


def _clean_metadata(raw: Any) -> dict[str, Any]:
    """Minimal validated facts only: up to 10 short scalars (never free text of users, answers or evidence)."""
    if not raw:
        return {}
    if not isinstance(raw, dict) or len(raw) > 10:
        raise NotificationError("Event metadata is at most 10 short key/value pairs.", "invalid_metadata")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.replace("_", "").isalnum() or len(key) > 40:
            raise NotificationError("Event metadata keys are short identifiers.", "invalid_metadata")
        if isinstance(value, bool) or isinstance(value, int):
            out[key] = value
        elif isinstance(value, str) and len(value) <= 100:
            out[key] = value
        else:
            raise NotificationError("Event metadata values are short strings, whole numbers or booleans.", "invalid_metadata")
    return out


def publish(event_type: str, source: Any, *, actor: Any = None, discriminator: str = "", metadata: dict[str, Any] | None = None, process: bool = True) -> NotificationEvent:
    """Record a business event (idempotent) and schedule its processing after the surrounding transaction commits."""
    event = registry.get_event(event_type)
    if event is None:
        raise NotificationError(f"Unknown event type “{event_type}”.", "unknown_event")
    source_def = registry.get_source(event.source_type)
    if source_def is None or not hasattr(source, "pk") or type(source).__name__ != _model_name(source_def):
        raise NotificationError(f"The source of “{event_type}” must be a {event.source_type}.", "invalid_source")
    organization = source_def.organization(source)
    clean = _clean_metadata(metadata)
    key = f"{event_type}:{source.pk}:{discriminator}"[:200]
    now = timezone.now()
    try:
        with transaction.atomic():
            row, created = NotificationEvent.objects.get_or_create(
                organization=organization, dedupe_key=key,
                defaults={"event_type": event_type, "source_type": event.source_type, "source_id": source.pk, "actor": actor if getattr(actor, "is_authenticated", False) else None,
                          "occurred_at": now, "metadata": clean})
    except IntegrityError:  # a concurrent publisher won the race for the same key
        row, created = NotificationEvent.objects.get(organization=organization, dedupe_key=key), False
    row.created_now = created  # (a plain attribute: callers such as the reminder service count genuinely new events)
    if created and process:
        transaction.on_commit(lambda pk=row.pk: process_event(pk, dispatch=bool(getattr(settings, "NOTIFICATION_DISPATCH_INLINE", True))))
    return row


def _model_name(source_def: registry.Source) -> str:
    return {"assignment": "ChecklistAssignment", "execution": "ChecklistExecution", "finding": "Finding", "corrective_action": "CorrectiveAction", "workflow_task": "WorkflowTask",
            "workflow_instance": "WorkflowInstance", "evaluation_run": "EvaluationRun", "notification_delivery": "NotificationDelivery"}[source_def.type]


# ---------------------------------------------------------------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------------------------------------------------------------
def _claim(pk: Any) -> NotificationEvent | None:
    stale = timezone.now() - timedelta(seconds=k.LOCK_TIMEOUT_SECONDS)
    with transaction.atomic():
        event = (NotificationEvent.objects.select_for_update(skip_locked=True, of=("self",)).select_related("organization", "actor")
                 .filter(pk=pk).filter(Q(status__in=[k.EventStatus.PENDING, k.EventStatus.FAILED]) | Q(status=k.EventStatus.PROCESSING, locked_at__lt=stale)).first())
        if event is None or (event.status == k.EventStatus.FAILED and event.attempts >= MAX_EVENT_ATTEMPTS):
            return None
        event.status, event.attempts, event.locked_at = k.EventStatus.PROCESSING, event.attempts + 1, timezone.now()
        event.save(update_fields=["status", "attempts", "locked_at"])
        return event


def process_event(pk: Any, *, dispatch: bool = True) -> dict[str, Any]:
    """Process one outbox event.  Idempotent: an already processed event is left alone.  Never raises."""
    event = _claim(pk)
    if event is None:
        return {"processed": False}
    try:
        with transaction.atomic():
            summary = _create_notifications(event)
            NotificationEvent.objects.filter(pk=event.pk).update(status=k.EventStatus.PROCESSED, processed_at=timezone.now(), locked_at=None, last_error=summary.get("note", "")[:200])
    except Exception as exc:  # noqa: BLE001 - recorded; the business transaction is long finished
        logger.exception("Notification event %s failed", event.pk)
        NotificationEvent.objects.filter(pk=event.pk).update(status=k.EventStatus.FAILED, locked_at=None, last_error=type(exc).__name__[:200])
        audit(VERBS["event_failed"], event.organization, None, event, f"Processing of {event.event_type} failed", event_type=event.event_type, error=type(exc).__name__, attempts=event.attempts)
        return {"processed": False, "error": type(exc).__name__}
    if dispatch and summary.get("deliveries"):
        try:
            delivery.dispatch_for_event(event.pk)
        except Exception:  # noqa: BLE001 - delivery problems are recorded per delivery; nothing may escape into the caller
            logger.exception("Dispatch after event %s failed", event.pk)
    return {"processed": True, **summary}


def process_pending(limit: int = 50, *, dispatch: bool = True) -> dict[str, int]:
    """Process a bounded batch of pending / retry-eligible / stuck events (the command and the Phase 18 scheduler call this)."""
    stale = timezone.now() - timedelta(seconds=k.LOCK_TIMEOUT_SECONDS)
    ids = list(NotificationEvent.objects.filter(Q(status=k.EventStatus.PENDING) | Q(status=k.EventStatus.FAILED, attempts__lt=MAX_EVENT_ATTEMPTS)
                                                | Q(status=k.EventStatus.PROCESSING, locked_at__lt=stale)).order_by("created_at").values_list("pk", flat=True)[:limit])
    done = failed = 0
    for pk in ids:
        result = process_event(pk, dispatch=dispatch)
        done, failed = done + (1 if result.get("processed") else 0), failed + (0 if result.get("processed") else 1)
    return {"found": len(ids), "processed": done, "failed": failed}


# ---------------------------------------------------------------------------------------------------------------------------------
# Rule matching and notification creation
# ---------------------------------------------------------------------------------------------------------------------------------
def _display(user: Any) -> str:
    if user is None:
        return "The system"
    name = getattr(user, "get_short_name", lambda: "")() or getattr(user, "display_name", "") or str(getattr(user, "email", "")).split("@")[0]
    return " ".join(str(name).split())[:80] or "Someone"


def _base_context(event: registry.Event, source_def: registry.Source, obj: Any, ev: NotificationEvent) -> dict[str, str]:
    location = source_def.location(obj)
    context = {"organization_name": ev.organization.name, "reference": registry._s(source_def.reference(obj)), "title": registry._s(source_def.title(obj)),
               "location_name": registry._s(getattr(location, "name", "")), "actor_name": _display(ev.actor), "link": registry.absolute_link(source_def.url(obj)),
               "event_label": event.label}
    context.update(event.context_of(obj, ev.metadata))
    return {name: context.get(name, "") for name in event.variables if name != "recipient_name"}


def _create_notifications(ev: NotificationEvent) -> dict[str, Any]:
    from .. import templating

    event = registry.get_event(ev.event_type)
    source_def = registry.get_source(ev.source_type)
    if event is None or source_def is None:
        return {"note": "unknown event type", "deliveries": 0, "notifications": 0}
    obj = source_def.load(ev.source_id)
    if obj is None:
        return {"note": "source no longer exists", "deliveries": 0, "notifications": 0}
    organization = ev.organization
    if source_def.organization(obj).pk != organization.pk:
        return {"note": "organization mismatch", "deliveries": 0, "notifications": 0}
    config.ensure_default_rule(organization, event)
    rules = list(NotificationRule.objects.filter(organization=organization, event_type=event.code, enabled=True).order_by("precedence", "code", "pk"))
    facts = event.facts_of(obj, ev.metadata)
    base = _base_context(event, source_def, obj, ev)
    plan: dict[Any, dict[str, Any]] = {}  # recipient pk -> {"user", "rule", "channels": set}
    for rule in rules:
        if not filters.matches(event, rule.filters, facts):
            continue
        for user in recipients.resolve(rule.recipient_strategies, source_def, obj, organization=organization, actor=ev.actor, exclude_actor=rule.exclude_actor):
            entry = plan.setdefault(user.pk, {"user": user, "rule": rule, "channels": set()})
            entry["channels"].update(rule.channels)  # rules combine their channels; the lowest precedence number (first) decides template, priority and policy
    made = deliveries = 0
    for entry in plan.values():
        created, count = _notify(ev, event, source_def, obj, entry["user"], entry["rule"], entry["channels"], base, templating)
        made, deliveries = made + int(created), deliveries + count
    audit(VERBS["generated"], organization, None, ev, f"{event.label}: {made} notification(s), {deliveries} email(s) queued", event_type=event.code, notifications=made, rules=len(rules),
          email_deliveries=deliveries)
    return {"notifications": made, "deliveries": deliveries, "note": ""}


def _notify(ev: NotificationEvent, event: registry.Event, source_def: registry.Source, obj: Any, user: Any, rule: NotificationRule, channels: set[str], base: dict[str, str],
            templating: Any) -> tuple[bool, int]:
    key = hashlib.sha256(f"{ev.pk}|{user.pk}".encode()).hexdigest()
    if Notification.objects.filter(idempotency_key=key).exists():
        return False, 0
    template = config.active_template(ev.organization, rule.template_code, event.code)
    fields = config.fields_for(event, template)
    context = {**base, "recipient_name": _display(user)}
    rendered = templating.render(fields, context)
    decision = preferences_policy.decide(user, ev.organization, event.category, rule.preference_policy, channels)
    in_app = k.Channel.IN_APP in channels and decision[k.Channel.IN_APP] is None
    notification = Notification.objects.create(
        organization=ev.organization, recipient=user, event=ev, rule=rule, rule_code=rule.code, rule_revision=rule.revision, template_code=template.code if template else "",
        template_version=template.version if template else 0, event_type=event.code, category=event.category, priority=rule.priority, preference_policy=rule.preference_policy,
        title=rendered["in_app_title"][:160] or event.label, message=rendered["in_app_message"][:500], target_type=source_def.type, target_id=ev.source_id, in_app_visible=in_app,
        idempotency_key=key)
    now = timezone.now()
    queued = 0
    if k.Channel.IN_APP in channels:
        reason = decision[k.Channel.IN_APP]
        NotificationDelivery.objects.create(
            organization=ev.organization, notification=notification, channel=k.Channel.IN_APP, provider="in_app", idempotency_key=f"{notification.pk}|in_app|0",
            status=k.DeliveryStatus.SUPPRESSED if reason else k.DeliveryStatus.DELIVERED, suppressed_reason=reason or "", accepted_at=None if reason else now, delivered_at=None if reason else now)
    if k.Channel.EMAIL in channels:
        reason = decision[k.Channel.EMAIL]
        address = str(getattr(user, "email", "") or "")
        NotificationDelivery.objects.create(
            organization=ev.organization, notification=notification, channel=k.Channel.EMAIL, provider="", idempotency_key=f"{notification.pk}|email|0", to_masked=mask_email(address),
            subject=rendered["subject"], body_text=rendered["body_text"], body_html=rendered["body_html"], max_attempts=int(getattr(settings, "NOTIFICATION_MAX_ATTEMPTS", k.MAX_ATTEMPTS_DEFAULT)),
            status=k.DeliveryStatus.SUPPRESSED if reason else k.DeliveryStatus.PENDING, suppressed_reason=reason or "")
        queued = 0 if reason else 1
    return True, queued
