"""Scoped reads of notification data (the choke point shared by the UI, the API and the admin).

A user's inbox is filtered by ``recipient`` in the query itself, so another person's notification cannot even be fetched.  Administrative reads (rules, templates,
deliveries) are limited to the organizations where the user holds the matching organization-wide permission.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.core.policies import is_active_user
from apps.core.utils import parse_uuid
from apps.permissions.authorization import get_accessible_organizations
from apps.permissions.registry import P

from . import constants as k
from .models import DeliveryAttempt, Notification, NotificationDelivery, NotificationRule, NotificationTemplate, UserNotificationPreference


# -- the inbox --------------------------------------------------------------------------------------------------------------------
def inbox(user: Any, *, organization: Any = None, unread_only: bool = False, category: str = "", include_archived: bool = False) -> QuerySet[Notification]:
    if not is_active_user(user):
        return Notification.objects.none()
    qs = Notification.objects.filter(recipient=user, in_app_visible=True).select_related("organization")
    now = timezone.now()
    qs = qs.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    if not include_archived:
        qs = qs.filter(archived_at__isnull=True)
    if organization is not None:
        qs = qs.filter(organization=organization)
    if unread_only:
        qs = qs.filter(is_read=False)
    if category in k.Category.values:
        qs = qs.filter(category=category)
    return qs


def unread_count(user: Any, *, organization: Any = None) -> int:
    return inbox(user, organization=organization, unread_only=True).count()


def get_notification(user: Any, pk: Any) -> Notification | None:
    parsed = parse_uuid(pk)
    return inbox(user, include_archived=True).filter(pk=parsed).first() if parsed else None


# -- administration -----------------------------------------------------------------------------------------------------------------
def _org_ids(user: Any, code: str) -> list[Any]:
    return list(get_accessible_organizations(user, code).values_list("pk", flat=True)) if is_active_user(user) else []


def rules_visible_to(user: Any) -> QuerySet[NotificationRule]:
    return NotificationRule.objects.filter(organization_id__in=_org_ids(user, P.NOTIFICATION_VIEW)).select_related("organization")


def get_rule(user: Any, pk: Any) -> NotificationRule | None:
    parsed = parse_uuid(pk)
    return rules_visible_to(user).filter(pk=parsed).first() if parsed else None


def templates_visible_to(user: Any) -> QuerySet[NotificationTemplate]:
    ids = _org_ids(user, P.NOTIFICATION_VIEW)
    return NotificationTemplate.objects.filter(Q(organization_id__in=ids) | Q(organization__isnull=True)).select_related("organization") if ids else NotificationTemplate.objects.none()


def get_template(user: Any, pk: Any) -> NotificationTemplate | None:
    parsed = parse_uuid(pk)
    return templates_visible_to(user).filter(pk=parsed).first() if parsed else None


def deliveries_visible_to(user: Any) -> QuerySet[NotificationDelivery]:
    return NotificationDelivery.objects.filter(organization_id__in=_org_ids(user, P.NOTIFICATION_DELIVERIES)).select_related("notification", "organization")


def get_delivery(user: Any, pk: Any) -> NotificationDelivery | None:
    parsed = parse_uuid(pk)
    return deliveries_visible_to(user).filter(pk=parsed).first() if parsed else None


def attempts_of(delivery: NotificationDelivery) -> QuerySet[DeliveryAttempt]:
    return DeliveryAttempt.objects.filter(delivery=delivery).order_by("number")


def delivery_summary(user: Any) -> dict[str, int]:
    rows = deliveries_visible_to(user).values("status").annotate(n=Count("pk"))
    return {r["status"]: r["n"] for r in rows}


def preferences_of(user: Any, organization: Any) -> dict[str, UserNotificationPreference]:
    return {p.category: p for p in UserNotificationPreference.objects.filter(user=user, organization=organization)}


# -- dispatch interfaces for the future scheduler (Phase 18) ------------------------------------------------------------------------
def dispatchable_deliveries(now: Any = None, limit: int = 50) -> QuerySet[NotificationDelivery]:
    """Email deliveries a dispatcher may pick up now: pending, retry-eligible, or stuck in ``processing`` past the lock timeout.  A read-only view - claiming is ``delivery.claim``."""
    now = now or timezone.now()
    stale = now - timedelta(seconds=k.LOCK_TIMEOUT_SECONDS)
    return (NotificationDelivery.objects.filter(channel=k.Channel.EMAIL)
            .filter(Q(status=k.DeliveryStatus.PENDING) | Q(status=k.DeliveryStatus.RETRY_PENDING, next_attempt_at__lte=now) | Q(status=k.DeliveryStatus.PROCESSING, locked_at__lt=stale))
            .order_by("created_at")[:limit])
