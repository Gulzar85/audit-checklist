"""Retention: how long notification data is kept (ADR 0081).

* An in-app notification is *archived* by its owner or expires; archived / expired copies are hidden from the inbox but stay in the delivery history.
* The retention command removes notifications **older than ``NOTIFICATION_RETENTION_DAYS`` (default 180) that have no open delivery** - with their deliveries, attempts and processed events - in
  one bounded transaction that raises the database setting ``notifications.purge`` (the only way the append-only triggers allow a delete).  The business objects, the activity log and
  the audit history are never touched.
* Users are deactivated, not deleted, so their notification history stays attached to them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .. import constants as k
from ..models import DeliveryAttempt, Notification, NotificationDelivery, NotificationEvent
from .base import VERBS, audit

OPEN = (k.DeliveryStatus.PENDING, k.DeliveryStatus.PROCESSING, k.DeliveryStatus.RETRY_PENDING, k.DeliveryStatus.SENT)


def purge(*, days: int | None = None, limit: int = 1000, now: Any = None) -> dict[str, int]:
    days = int(days if days is not None else getattr(settings, "NOTIFICATION_RETENTION_DAYS", 180))
    cutoff = (now or timezone.now()) - timedelta(days=max(1, days))
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, 'on', true)", [k.PURGE_SETTING])
        ids = list(Notification.objects.filter(created_at__lt=cutoff).exclude(deliveries__status__in=OPEN).values_list("pk", flat=True).distinct()[:limit])
        attempts = DeliveryAttempt.objects.filter(delivery__notification_id__in=ids).delete()[0]
        NotificationDelivery.objects.filter(notification_id__in=ids, resend_of__isnull=False).delete()
        deliveries = NotificationDelivery.objects.filter(notification_id__in=ids).delete()[0]
        notifications = Notification.objects.filter(pk__in=ids).delete()[0]
        events = NotificationEvent.objects.filter(status=k.EventStatus.PROCESSED, created_at__lt=cutoff, notifications__isnull=True).delete()[0]
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, 'off', true)", [k.PURGE_SETTING])
    if notifications or events:
        audit(VERBS["purged"], None, None, None, f"Retention removed {notifications} notification(s)", notifications=notifications, deliveries=deliveries, attempts=attempts, events=events, days=days)
    return {"notifications": notifications, "deliveries": deliveries, "attempts": attempts, "events": events}
