"""The in-app experience: read / unread, archive, safe destinations and the user's preferences.

Everything is scoped to the acting user (a query filter, not a check afterwards).  The destination of a notification is *computed* from its registered object reference when it is
shown - never a stored URL - and only if the **viewer** may open that object under the owning module's own policy; otherwise the notification is still shown, without a link.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from .. import constants as k
from .. import registry, selectors
from ..models import Notification, UserNotificationPreference
from .base import VERBS, audit
from .errors import NotificationError


def destination(user: Any, notification: Notification) -> str:
    """The safe internal path for ``notification`` if ``user`` may open the object, else an empty string."""
    source = registry.get_source(notification.target_type)
    if source is None:
        return ""
    try:
        obj = source.load(notification.target_id)
        if obj is None or source.organization(obj).pk != notification.organization_id or not source.can_view(user, obj):
            return ""
        path = source.url(obj)
    except Exception:  # noqa: BLE001 - a broken destination never breaks the list
        return ""
    return path if path.startswith("/") and not path.startswith("//") else ""


def mark_read(user: Any, pk: Any) -> Notification:
    notification = selectors.get_notification(user, pk)
    if notification is None:
        raise NotificationError("Notification not found.", "not_found")
    if not notification.is_read:
        Notification.objects.filter(pk=notification.pk, recipient=user, is_read=False).update(is_read=True, read_at=timezone.now())
        notification.refresh_from_db()
    return notification


def mark_many(user: Any, ids: list[Any]) -> int:
    """Mark up to ``BULK_LIMIT`` of the user's own notifications as read.  Other people's ids are silently not found (never an error that confirms they exist)."""
    if not isinstance(ids, list) or len(ids) > k.BULK_LIMIT:
        raise NotificationError(f"At most {k.BULK_LIMIT} notifications can be marked at once.", "too_many")
    return Notification.objects.filter(pk__in=[i for i in ids if i], recipient=user, is_read=False).update(is_read=True, read_at=timezone.now())


def mark_all(user: Any, *, organization: Any = None, category: str = "") -> int:
    """Mark every unread notification of the user (optionally of one organization / category) as read."""
    qs = selectors.inbox(user, organization=organization, unread_only=True, category=category)
    return Notification.objects.filter(pk__in=list(qs.values_list("pk", flat=True)[:5000]), recipient=user).update(is_read=True, read_at=timezone.now())


def archive(user: Any, pk: Any) -> Notification:
    notification = selectors.get_notification(user, pk)
    if notification is None:
        raise NotificationError("Notification not found.", "not_found")
    if notification.archived_at is None:
        Notification.objects.filter(pk=notification.pk, recipient=user).update(archived_at=timezone.now(), is_read=True, read_at=notification.read_at or timezone.now())
        notification.refresh_from_db()
    return notification


# ---------------------------------------------------------------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def set_preference(user: Any, organization: Any, category: str, *, in_app_enabled: bool | None = None, email_enabled: bool | None = None, actor: Any = None) -> UserNotificationPreference:
    """Change the user's own preference (``actor`` defaults to the user; changing someone else's is not supported here)."""
    if actor is not None and getattr(actor, "pk", None) != user.pk:
        raise NotificationError("You can change only your own notification preferences.", "own_only")
    if category not in k.Category.values:
        raise NotificationError(f"category must be one of: {', '.join(k.Category.values)}.", "invalid_category")
    for label, value in (("in_app_enabled", in_app_enabled), ("email_enabled", email_enabled)):
        if value is not None and not isinstance(value, bool):
            raise NotificationError(f"{label} must be true or false.", "invalid_value")
    from apps.organizations.models import OrganizationMembership

    if not OrganizationMembership.objects.filter(user=user, organization=organization, is_active=True).exists():
        raise NotificationError("You are not a member of this organization.", "not_member")
    pref, _created = UserNotificationPreference.objects.select_for_update().get_or_create(user=user, organization=organization, category=category)
    before = (pref.in_app_enabled, pref.email_enabled)
    if in_app_enabled is not None:
        pref.in_app_enabled = in_app_enabled
    if email_enabled is not None:
        pref.email_enabled = email_enabled
    if (pref.in_app_enabled, pref.email_enabled) != before or _created:
        from apps.core.services import stamp_history

        stamp_history(pref, user, VERBS["preference_changed"])
        pref.save()
        audit(VERBS["preference_changed"], organization, user, pref, f"Notification preference for {category} changed", category=category, in_app=pref.in_app_enabled, email=pref.email_enabled)
    return pref

